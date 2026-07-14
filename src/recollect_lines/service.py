from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .adapters import AdapterCapabilities
from .durable_reconciliation import (
    AdoptedDurableHandle,
    LAUNCH_KIND_DURABLE,
    ReconcileDetail,
    ReconcileOutcome,
    adopt_durable_handle,
    adopted_cancel,
    adopted_collect,
    adopted_status,
    evaluate_durable_reconciliation,
    is_durable_launch_row,
    load_broker_identity,
    make_reconcile_detail,
)
from .fixture_durable_adapter import FixtureDurableAdapter
from .recovery_contract import SYNTHETIC_RECOVERY_CONTROL
from .claude_code_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter
from .cursor_adapter import CursorAdapter
from .direct_api_runtime import DIRECT_API_PROFILE, OpenAiCompatibleDirectRuntime
from .models import (
    DEFAULT_PROFILES,
    TERMINAL_STATES,
    VERIFICATION_POLICIES,
    ProfilePolicy,
    RecoveryRequired,
    TaskRecord,
    TaskRequest,
    TaskState,
    WorkspaceLeaseConflict,
    now,
    validate_result,
    validate_verify_commands,
)
from .opencode_adapter import OpenCodeAdapter, group_alive, group_dead_within, redact_command
from .providers import ProviderConfigError, load_providers_config
from .store import TaskStore
from .workspace import WorkspaceError, WorkspaceManager, canonical_source, capture_head

ISOLATED_WORKTREE = "isolated_worktree"


class MockAdapter:
    name = "mock"
    capabilities = AdapterCapabilities(
        requires_subprocess=False,
        supports_process_group_cancellation=False,
        reports_broker_verified_tests=False,
        recovery_control=SYNTHETIC_RECOVERY_CONTROL,
    )

    def start_metadata(self, record: TaskRecord, workspace: str) -> dict[str, str]:
        return {"adapter": self.name, "mode": record.execution_mode, "workspace": workspace}


class Broker:
    def __init__(
        self,
        home: Path,
        profiles: dict[str, ProfilePolicy] | None = None,
        opencode_adapter: OpenCodeAdapter | None = None,
        claude_code_adapter: ClaudeCodeAdapter | None = None,
        codex_adapter: CodexAdapter | None = None,
        cursor_adapter: CursorAdapter | None = None,
        fixture_durable_adapter: FixtureDurableAdapter | None = None,
        providers_config: Path | None = None,
        direct_api_runtime: OpenAiCompatibleDirectRuntime | None = None,
        environ: dict[str, str] | None = None,
    ):
        self.store = TaskStore(home)
        self.adapter = MockAdapter()
        self.opencode_adapter = opencode_adapter or OpenCodeAdapter()
        self.claude_code_adapter = claude_code_adapter or ClaudeCodeAdapter()
        self.codex_adapter = codex_adapter or CodexAdapter()
        self.cursor_adapter = cursor_adapter or CursorAdapter()
        self.fixture_durable_adapter = fixture_durable_adapter
        # Every subprocess-backed adapter, keyed by the profile name that selects
        # it — the one place profile-to-adapter dispatch lives, so start()/
        # collect()/cancel()/timeout()/reconcile() never hard-code a specific
        # adapter's name to decide whether a task has a supervised process.
        self.subprocess_adapters = {
            self.opencode_adapter.name: self.opencode_adapter,
            self.claude_code_adapter.name: self.claude_code_adapter,
            self.codex_adapter.name: self.codex_adapter,
            self.cursor_adapter.name: self.cursor_adapter,
        }
        if self.fixture_durable_adapter is not None:
            self.subprocess_adapters[self.fixture_durable_adapter.name] = self.fixture_durable_adapter
        self.profiles = profiles or DEFAULT_PROFILES
        self.workspaces = WorkspaceManager(self.store.home)
        self._environ = environ
        if direct_api_runtime is not None:
            self.direct_api_runtime = direct_api_runtime
        elif providers_config is not None:
            self.direct_api_runtime = OpenAiCompatibleDirectRuntime(load_providers_config(providers_config), environ=environ)
        else:
            self.direct_api_runtime = None
        # In-memory only, one broker process per running task; a restart loses
        # this dict. That's fine: `store.runtime_launches` is the durable record
        # a fresh Broker reconciles against (see reconcile()/reconcile_pending()
        # and docs/history/phases/phase-5b.md). Transparent re-attachment remains out of scope.
        self._process_handles: dict[str, object] = {}
        self._direct_api_handles: dict[str, object] = {}
        self._adopted_durable_handles: dict[str, AdoptedDurableHandle] = {}
        self._broker_identity = load_broker_identity(self.store.home)
        self._last_reconcile_details: dict[str, ReconcileDetail] = {}

    def close(self) -> None:
        for task_id in list(self._adopted_durable_handles):
            self.store.release_recovery_lease(task_id)
        self._adopted_durable_handles.clear()
        self.store.close()

    def reconcile_detail(self, task_id: str) -> dict | None:
        detail = self._last_reconcile_details.get(task_id)
        return detail.to_dict() if detail else None

    def _validate_request(self, request: TaskRequest) -> ProfilePolicy:
        if not request.task.strip():
            raise ValueError("Task must not be empty")
        if not request.workspace.strip():
            raise ValueError("Workspace must not be empty")
        if request.timeout_seconds <= 0:
            raise ValueError("Timeout must be positive")
        policy = self.profiles.get(request.profile)
        if policy is None:
            raise ValueError(f"Unknown profile: {request.profile}")
        if request.execution_mode not in policy.allowed_modes:
            raise ValueError(f"Profile {policy.name} does not permit mode {request.execution_mode}")
        if request.timeout_seconds > policy.max_timeout_seconds:
            raise ValueError(f"Profile {policy.name} maximum timeout is {policy.max_timeout_seconds} seconds")
        if request.verification_policy not in VERIFICATION_POLICIES:
            raise ValueError(f"Unknown verification_policy: {request.verification_policy}")
        if request.profile == DIRECT_API_PROFILE:
            if self.direct_api_runtime is None:
                raise ValueError(
                    f"Profile {DIRECT_API_PROFILE!r} requires a provider configuration (--providers-config)"
                )
            if not request.provider:
                raise ValueError(f"Profile {DIRECT_API_PROFILE!r} requires a named provider (--provider)")
            self.direct_api_runtime.get_provider(request.provider)
        elif request.provider is not None:
            raise ValueError(f"provider is only valid with profile {DIRECT_API_PROFILE!r}")
        if self.store.active_count(policy.name) >= policy.max_concurrency:
            raise ValueError(f"Profile {policy.name} concurrency limit reached")
        return policy

    def create(self, request: TaskRequest, verify_commands: list[list[str]] | None = None) -> TaskRecord:
        """Create a task, optionally declaring the broker-verified commands its
        verification_policy will gate on. Shared by the CLI and MCP surfaces so
        neither duplicates this policy check (PRD §6).
        """
        self._validate_request(request)
        if verify_commands is not None:
            validate_verify_commands(verify_commands)
        record = self.store.create(TaskRecord.new(request))
        self.store.write_artifact(record.id, "request.json", json.dumps(record.json(), indent=2) + "\n")
        if verify_commands is not None:
            self.store.write_artifact(record.id, "verify_commands.json", json.dumps(verify_commands, indent=2) + "\n")
        return self.store.transition(record.id, TaskState.QUEUED, "Task queued", {})

    def start(self, task_id: str) -> TaskRecord:
        record = self.store.transition(task_id, TaskState.PREPARING, "Preparing execution", {})
        effective_workspace = record.workspace
        if record.execution_mode == ISOLATED_WORKTREE:
            try:
                source = canonical_source(record.workspace)
            except WorkspaceError as error:
                return self.store.transition(
                    record.id, TaskState.FAILED, f"Workspace validation failed: {error}",
                    {"reason": "workspace_invalid", "error": str(error)},
                )
            branch = self.workspaces.branch_name(record.id)
            worktree_path = str(self.workspaces.worktree_path(record.id))
            base_sha = capture_head(source)
            try:
                # Lease acquisition (durable, atomic via a partial unique index)
                # gates worktree creation: a losing writer never touches the
                # filesystem at all.
                self.store.acquire_lease(record.id, source, worktree_path, branch, base_sha)
            except WorkspaceLeaseConflict as error:
                return self.store.transition(
                    record.id, TaskState.FAILED, str(error),
                    {"reason": "workspace_lease_conflict", "canonical_source": source},
                )
            try:
                self.workspaces.create_worktree(source, record.id, base_sha)
            except Exception as error:
                self.store.release_lease(record.id)
                return self.store.transition(
                    record.id, TaskState.FAILED, f"Workspace allocation failed: {error}",
                    {"reason": "workspace_allocation_failed", "error": str(error)},
                )
            effective_workspace = worktree_path
        if record.profile == DIRECT_API_PROFILE:
            runtime = self.direct_api_runtime
            assert runtime is not None
            try:
                metadata, handle = runtime.start(record, self.store.artifacts / record.id)
            except Exception as error:
                if record.execution_mode == ISOLATED_WORKTREE:
                    lease = self.store.get_lease(record.id)
                    if lease is not None and lease["status"] == "active":
                        self.workspaces.release(lease["canonical_source"], lease["worktree_path"])
                        self.store.release_lease(record.id)
                return self.store.transition(
                    record.id, TaskState.FAILED, f"Direct API runtime failed to start: {error}",
                    {"reason": "direct_api_start_failed", "error": str(error)},
                )
            self._direct_api_handles[record.id] = handle
            self.store.record_launch(
                record.id,
                adapter=runtime.name,
                adapter_label=runtime.runtime_label,
                pid=None,
                pgid=None,
                command=[f"provider={metadata['provider']}", f"model={metadata['model']}", f"base_url={metadata['base_url']}"],
                workspace=metadata["workspace"],
                events_artifact=metadata["events_artifact"],
                stderr_artifact=metadata["stderr_artifact"],
            )
            self.store.refresh_manifest(record.id)
            return self.store.transition(record.id, TaskState.RUNNING, f"{runtime.name} direct API request started", metadata)
        adapter = self.subprocess_adapters.get(record.profile)
        if adapter is not None:
            try:
                metadata, handle = adapter.start(record, self.store.artifacts / record.id, workspace=effective_workspace)
            except Exception:
                # A losing writer never allocates, but a *successful* allocation
                # whose adapter then fails to launch must still give up its lease —
                # otherwise this source stays blocked for every future writer.
                if record.execution_mode == ISOLATED_WORKTREE:
                    self.workspaces.release(source, worktree_path)
                    self.store.release_lease(record.id)
                raise
            self._process_handles[record.id] = handle
            # Durable launch identity is recorded as soon as the process actually
            # exists, before the task even reaches RUNNING — a fresh Broker must
            # be able to reconcile against this row even if this process crashes
            # on the very next line.
            stored_command = (
                ["<durable-subprocess>", adapter.name]
                if metadata.get("launch_kind") == LAUNCH_KIND_DURABLE
                else redact_command(metadata["command"])
            )
            self.store.record_launch(
                record.id,
                adapter=adapter.name,
                adapter_label=adapter.runtime_label,
                pid=handle.pid,
                pgid=handle.pgid,
                command=stored_command,
                workspace=metadata["workspace"],
                events_artifact=metadata["events_artifact"],
                stderr_artifact=metadata["stderr_artifact"],
                durable_launch_id=metadata.get("durable_launch_id"),
                launch_kind=metadata.get("launch_kind", "legacy_subprocess"),
            )
            self.store.refresh_manifest(record.id)
            event_metadata = metadata
            if metadata.get("launch_kind") == LAUNCH_KIND_DURABLE:
                event_metadata = {**metadata, "command": stored_command}
            return self.store.transition(record.id, TaskState.RUNNING, f"{adapter.name} adapter started", event_metadata)
        return self.store.transition(record.id, TaskState.RUNNING, "Mock adapter started", self.adapter.start_metadata(record, effective_workspace))

    def complete(self, task_id: str, summary: str) -> TaskRecord:
        record = self.store.transition(task_id, TaskState.COLLECTING, "Collecting mock result", {})
        result = {"task_id": record.id, "state": "succeeded", "summary": summary, "runtime": {"adapter": "mock"}}
        validate_result(result, record.id)
        self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
        final_state, gate = self._apply_verification_gate(record.id, record, TaskState.SUCCEEDED)
        self._write_gate_artifact(record.id, gate)
        self._finalize_workspace(record.id)
        message = "Mock task completed" if final_state is TaskState.SUCCEEDED else f"Mock task blocked by verification gate ({gate['outcome']})"
        return self.store.transition(record.id, final_state, message, {"result_artifact": "result.json", "verification_gate": gate})

    def collect(self, task_id: str) -> TaskRecord:
        """Collect a task's runtime-reported result.

        Idempotent: calling this again on an already-terminal task returns the
        same durable record without re-running verification, re-finalizing the
        workspace, or emitting a duplicate terminal event. A task that requires
        reconciliation (state recovery_required, or a fresh restart discovering
        a still-alive process group) raises RecoveryRequired rather than
        fabricating a result — see reconcile().
        """
        record = self.store.get(task_id)
        if record.state in TERMINAL_STATES:
            return record
        if task_id in self._direct_api_handles:
            handle = self._direct_api_handles.pop(task_id)
            runtime = self.direct_api_runtime
            assert runtime is not None
            record = self.store.transition(task_id, TaskState.COLLECTING, f"Collecting {runtime.name} result", {})
            collected = runtime.collect(handle)
            self.store.refresh_manifest(record.id)
            runtime_payload = {"adapter": runtime.name, **collected}
            if collected.get("exit_code") != 0:
                result = {
                    "task_id": record.id,
                    "state": TaskState.FAILED.value,
                    "summary": collected.get("summary") or collected.get("error_message") or f"{runtime.name} request failed",
                    "runtime": runtime_payload,
                }
                self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
                _, gate = self._apply_verification_gate(record.id, record, TaskState.FAILED)
                self._write_gate_artifact(record.id, gate)
                self._finalize_workspace(record.id)
                return self.store.transition(record.id, TaskState.FAILED, f"{runtime.name} task failed", {"result_artifact": "result.json", "exit_code": collected.get("exit_code"), "verification_gate": gate})
            state = TaskState.SUCCEEDED if collected.get("summary") else TaskState.SUCCEEDED_WITH_WARNINGS
            result = {
                "task_id": record.id,
                "state": state.value,
                "summary": collected.get("summary") or f"{runtime.name} run produced no text result",
                "runtime": runtime_payload,
            }
            validate_result(result, record.id)
            self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
            final_state, gate = self._apply_verification_gate(record.id, record, state)
            self._write_gate_artifact(record.id, gate)
            self._finalize_workspace(record.id)
            message = f"{runtime.name} task completed" if final_state is state else f"{runtime.name} task blocked by verification gate ({gate['outcome']})"
            return self.store.transition(record.id, final_state, message, {"result_artifact": "result.json", "exit_code": collected.get("exit_code"), "verification_gate": gate})
        if task_id in self._process_handles:
            handle = self._process_handles.pop(task_id)
            adapter = self.subprocess_adapters[record.profile]
            record = self.store.transition(task_id, TaskState.COLLECTING, f"Collecting {adapter.name} result", {})
            collected = adapter.collect(handle)
            self.store.refresh_manifest(record.id)
            runtime = {"adapter": adapter.name, **collected}
            if collected["exit_code"] != 0:
                result = {"task_id": record.id, "state": TaskState.FAILED.value, "summary": collected["summary"] or f"{adapter.name} exited with a non-zero status", "runtime": runtime}
                self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
                _, gate = self._apply_verification_gate(record.id, record, TaskState.FAILED)
                self._write_gate_artifact(record.id, gate)
                self._finalize_workspace(record.id)
                return self.store.transition(record.id, TaskState.FAILED, f"{adapter.name} task failed", {"result_artifact": "result.json", "exit_code": collected["exit_code"], "verification_gate": gate})
            state = TaskState.SUCCEEDED if collected["summary"] else TaskState.SUCCEEDED_WITH_WARNINGS
            result = {"task_id": record.id, "state": state.value, "summary": collected["summary"] or f"{adapter.name} run produced no text result", "runtime": runtime}
            validate_result(result, record.id)
            self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
            final_state, gate = self._apply_verification_gate(record.id, record, state)
            self._write_gate_artifact(record.id, gate)
            self._finalize_workspace(record.id)
            message = f"{adapter.name} task completed" if final_state is state else f"{adapter.name} task blocked by verification gate ({gate['outcome']})"
            return self.store.transition(record.id, final_state, message, {"result_artifact": "result.json", "exit_code": collected["exit_code"], "verification_gate": gate})
        if task_id in self._adopted_durable_handles:
            return self._collect_adopted_durable(task_id, record)
        if record.profile == DIRECT_API_PROFILE:
            reconciled = self.reconcile(task_id)
            if reconciled.state is not TaskState.FAILED:
                raise RecoveryRequired(task_id, reconciled.state)
            return reconciled
        if record.profile not in self.subprocess_adapters:
            # No subprocess was ever involved for this profile (mock tasks are
            # collected via complete(), not collect()); this is a caller/protocol
            # error, not a restart, and there is nothing to reconcile. Any
            # declared verify_commands still run here as evidence (matching
            # Phase 3/5B's MCP-level behavior) — they just can never rescue this
            # protocol error into a success, whatever the policy.
            _, gate = self._apply_verification_gate(task_id, record, TaskState.FAILED)
            self._write_gate_artifact(task_id, gate)
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id,
                TaskState.FAILED,
                "No running process handle for task (broker restart or already collected)",
                {"reason": "missing_process_handle", "verification_gate": gate},
            )
        reconciled = self.reconcile(task_id)
        if reconciled.state is not TaskState.FAILED:
            raise RecoveryRequired(task_id, reconciled.state)
        return reconciled

    def _collect_adopted_durable(self, task_id: str, record: TaskRecord) -> TaskRecord:
        adopted = self._adopted_durable_handles.pop(task_id)
        adapter_name = adopted.adapter_id
        record = self.store.transition(task_id, TaskState.COLLECTING, f"Collecting adopted {adapter_name} durable result", {})
        collected = adopted_collect(adopted)
        self.store.release_recovery_lease(task_id)
        self.store.refresh_manifest(record.id)
        runtime = {"adapter": adapter_name, **collected}
        if collected["exit_code"] != 0:
            result = {
                "task_id": record.id,
                "state": TaskState.FAILED.value,
                "summary": collected.get("summary") or f"{adapter_name} durable payload exited with a non-zero status",
                "runtime": runtime,
            }
            self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
            _, gate = self._apply_verification_gate(record.id, record, TaskState.FAILED)
            self._write_gate_artifact(record.id, gate)
            self._finalize_workspace(record.id)
            return self.store.transition(
                record.id, TaskState.FAILED, f"{adapter_name} task failed",
                {"result_artifact": "result.json", "exit_code": collected["exit_code"], "verification_gate": gate, "adopted_durable": True},
            )
        state = TaskState.SUCCEEDED if collected.get("summary") else TaskState.SUCCEEDED_WITH_WARNINGS
        result = {
            "task_id": record.id,
            "state": state.value,
            "summary": collected.get("summary") or f"{adapter_name} durable run produced no text result",
            "runtime": runtime,
        }
        validate_result(result, record.id)
        self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
        final_state, gate = self._apply_verification_gate(record.id, record, state)
        self._write_gate_artifact(record.id, gate)
        self._finalize_workspace(record.id)
        message = (
            f"{adapter_name} task completed from adopted durable artifacts"
            if final_state is state else
            f"{adapter_name} task blocked by verification gate ({gate['outcome']})"
        )
        return self.store.transition(
            record.id, final_state, message,
            {"result_artifact": "result.json", "exit_code": collected["exit_code"], "verification_gate": gate, "adopted_durable": True},
        )

    def _process_group_status(self, task_id: str) -> str:
        """Classify the durably-persisted process group for `task_id`.

        Returns "no_launch" (no durable runtime_launches row at all),
        "unknown" (a row exists but pid/pgid metadata is missing/invalid —
        handled conservatively, i.e. never treated as dead), "dead", or
        "alive". "alive" also covers PermissionError from killpg (a process
        group that exists but this broker doesn't own — still alive from our
        point of view).
        """
        launch = self.store.get_launch(task_id)
        if launch is None:
            return "no_launch"
        pgid = launch["pgid"]
        if not isinstance(pgid, int) or pgid <= 0:
            return "unknown"
        return "alive" if group_alive(pgid) else "dead"

    # States a durable launch record might be sitting under when no in-memory
    # handle exists for it: RUNNING/PREPARING from an ordinary restart (the
    # crash can land either just before or just after the RUNNING transition
    # — record_launch() happens first either way), CANCELLING from a crash
    # mid-signal, COLLECTING from a crash after the in-memory handle was
    # popped (runtime already reaped, or a verification gate already in
    # flight) but before the terminal transition (Phase 5C — see
    # docs/history/phases/phase-5c.md), and RECOVERY_REQUIRED from a previous reconciliation
    # pass.
    _RECONCILABLE_STATES = (
        TaskState.PREPARING, TaskState.RUNNING, TaskState.COLLECTING, TaskState.CANCELLING, TaskState.RECOVERY_REQUIRED,
    )

    def reconcile(self, task_id: str) -> TaskRecord:
        """Reconcile a task's durable runtime-launch record against reality.

        The one operation a freshly constructed Broker (no in-memory
        ProcessHandle) can use to inspect and act on a task whose last known
        state predates a previous broker process disappearing. Idempotent:
        re-running it while the process group is still alive just logs an
        audit event and makes no state change; it never asserts success and
        never touches a workspace/lease it cannot prove is safe to release.

        Durable subprocess launches (Phase 7C.3) may be adopted after proof
        and recovery-lease acquisition; legacy subprocess/direct paths remain
        fail-closed as before.
        """
        record = self.store.get(task_id)
        if record.state in TERMINAL_STATES or task_id in self._process_handles or task_id in self._direct_api_handles:
            return record
        if record.state not in self._RECONCILABLE_STATES:
            return record
        if record.profile not in self.subprocess_adapters and record.profile != DIRECT_API_PROFILE:
            return record  # mock tasks never hold a subprocess; nothing to reconcile
        launch = self.store.get_launch(task_id)
        if is_durable_launch_row(launch) and record.profile in self.subprocess_adapters:
            adapter = self.subprocess_adapters[record.profile]
            if getattr(adapter.capabilities, "uses_durable_subprocess_runner", False):
                return self._reconcile_durable_subprocess(task_id, record, launch, adapter.name)
        if record.profile == DIRECT_API_PROFILE:
            launch = self.store.get_launch(task_id)
            if launch is not None:
                self.store.mark_launch_reconciled(task_id)
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.FAILED,
                "Direct API request was in flight when the broker restarted; outcome could not be observed",
                {"reason": "direct_api_restart_no_reattachment", "provider": launch.get("command", [None])[0] if launch else None},
            )
        adapter_name = self.subprocess_adapters[record.profile].name
        status = self._process_group_status(task_id)
        launch = self.store.get_launch(task_id)
        if launch is not None:
            self.store.mark_launch_reconciled(task_id)
        was_cancelling = record.state is TaskState.CANCELLING
        if status in ("dead", "no_launch"):
            self._finalize_workspace(task_id)
            reason = "process_group_confirmed_dead" if status == "dead" else "missing_process_handle"
            target = TaskState.CANCELLED if was_cancelling else TaskState.FAILED
            if status == "dead":
                message = (
                    f"{adapter_name} process group is no longer present after a broker restart; the in-progress "
                    "cancellation is confirmed complete" if was_cancelling else
                    f"{adapter_name} process group is no longer present after a broker restart; the runtime outcome "
                    "could not be observed and is recorded as failed"
                )
            else:
                message = "No running process handle or durable launch record for this task"
            return self.store.transition(task_id, target, message, {"reason": reason, "pgid": launch["pgid"] if launch else None})
        reason = "process_group_alive_after_restart" if status == "alive" else "runtime_metadata_missing_or_invalid"
        message = (
            "Process group still appears active after a broker restart; task requires "
            "explicit operator reconciliation (reconcile again once it exits, or cancel it)"
            if status == "alive" else
            "Runtime launch metadata is missing or invalid; treating conservatively as possibly still active"
        )
        if record.state is not TaskState.RECOVERY_REQUIRED:
            return self.store.transition(task_id, TaskState.RECOVERY_REQUIRED, message, {"reason": reason, "pgid": launch["pgid"] if launch else None})
        self.store.event(task_id, "task.reconciliation_checked", record.state, record.state, message, {"reason": reason})
        return record

    def _reconcile_durable_subprocess(
        self,
        task_id: str,
        record: TaskRecord,
        launch: dict,
        adapter_name: str,
    ) -> TaskRecord:
        durable_launch_id = launch["durable_launch_id"]
        if task_id in self._adopted_durable_handles:
            detail = make_reconcile_detail(
                ReconcileOutcome.ALREADY_ADOPTED,
                "task already holds an adopted durable handle in this broker instance",
                launch_id=durable_launch_id,
                adapter_id=adapter_name,
            )
            self._last_reconcile_details[task_id] = detail
            self._emit_reconcile_event(task_id, record, detail)
            return record

        proof_outcome, inspection, reason = evaluate_durable_reconciliation(
            self.store.home,
            task_id=task_id,
            expected_adapter_id=adapter_name,
            durable_launch_id=durable_launch_id,
            launch_row_adapter=launch["adapter"],
        )
        if proof_outcome not in {ReconcileOutcome.ADOPTED_RUNNING, ReconcileOutcome.ADOPTED_TERMINAL_COLLECTABLE}:
            detail = make_reconcile_detail(
                proof_outcome,
                reason,
                launch_id=durable_launch_id,
                adapter_id=adapter_name,
                inspection=inspection,
            )
            self._last_reconcile_details[task_id] = detail
            self.store.mark_launch_reconciled(task_id)
            message = f"Durable reconciliation refused: {reason}"
            metadata = detail.to_dict()
            if record.state is not TaskState.RECOVERY_REQUIRED:
                return self.store.transition(task_id, TaskState.RECOVERY_REQUIRED, message, metadata)
            self.store.event(task_id, "task.reconciliation_checked", record.state, record.state, message, metadata)
            return record

        acquired = self.store.try_acquire_recovery_lease(
            task_id=task_id,
            durable_launch_id=durable_launch_id,
            broker_id=self._broker_identity.broker_id,
            broker_epoch=self._broker_identity.epoch,
        )
        if not acquired:
            detail = make_reconcile_detail(
                ReconcileOutcome.REFUSED_LEASE_CONTENDED,
                "another broker holds an active recovery lease for this launch",
                launch_id=durable_launch_id,
                adapter_id=adapter_name,
                inspection=inspection,
            )
            self._last_reconcile_details[task_id] = detail
            message = detail.reason
            metadata = detail.to_dict()
            if record.state is not TaskState.RECOVERY_REQUIRED:
                return self.store.transition(task_id, TaskState.RECOVERY_REQUIRED, message, metadata)
            self.store.event(task_id, "task.reconciliation_checked", record.state, record.state, message, metadata)
            return record

        terminal = proof_outcome is ReconcileOutcome.ADOPTED_TERMINAL_COLLECTABLE
        adopted = adopt_durable_handle(
            self.store.home,
            task_id=task_id,
            launch_id=durable_launch_id,
            adapter_id=adapter_name,
            terminal=terminal,
        )
        self._adopted_durable_handles[task_id] = adopted
        detail = make_reconcile_detail(
            proof_outcome,
            reason,
            launch_id=durable_launch_id,
            adapter_id=adapter_name,
            inspection=inspection,
        )
        self._last_reconcile_details[task_id] = detail
        self.store.mark_launch_reconciled(task_id)
        message = (
            "Adopted terminal durable launch; collect bounded artifacts when ready"
            if terminal else
            "Adopted running durable launch after broker-restart proof"
        )
        metadata = detail.to_dict()
        if record.state is TaskState.RECOVERY_REQUIRED or record.state is TaskState.PREPARING:
            return self.store.transition(task_id, TaskState.RUNNING, message, metadata)
        self.store.event(task_id, "task.durable_adopted", record.state, record.state, message, metadata)
        return record

    def _emit_reconcile_event(self, task_id: str, record: TaskRecord, detail: ReconcileDetail) -> None:
        self.store.event(
            task_id,
            "task.reconciliation_checked",
            record.state,
            record.state,
            detail.reason,
            detail.to_dict(),
        )

    def reconcile_pending(self) -> list[TaskRecord]:
        """Reconcile every subprocess-backed task (any profile in `subprocess_adapters`)
        this Broker instance can see is in a reconcilable non-terminal state
        without an in-memory handle — the operation a freshly started broker
        uses to inspect durable active runtime records after a restart,
        without waiting for a caller to happen to call collect()/cancel() on
        each task individually.
        """
        return [
            self.reconcile(record.id)
            for record in self.store.list()
            if (record.profile in self.subprocess_adapters or record.profile == DIRECT_API_PROFILE)
            and record.state in self._RECONCILABLE_STATES
            and record.id not in self._process_handles
            and record.id not in self._direct_api_handles
        ]

    def _cancel_by_pgid(self, pgid: int, grace_period_seconds: float = 10.0) -> dict:
        """Signal a process group known only by its durably persisted pgid — there is
        no live Popen/child relationship after a broker restart, so this cannot
        reap or read an exit code, only observe group liveness via killpg.

        ponytail: this is only ever called once `_process_group_status` has
        confirmed the group is alive via killpg(pgid, 0) immediately beforehand,
        never on bare unverified metadata. PID/PGID reuse is still a real
        residual risk (a killpg "alive" only proves *some* process group with
        this id exists right now, not that it's still the one we launched) —
        see docs/history/phases/phase-5b.md for the accepted threat-model tradeoff; there is no
        further escalation path (e.g. /proc start-time comparison) here.
        """
        signals_sent = []
        try:
            os.killpg(pgid, signal.SIGTERM)
            signals_sent.append("SIGTERM")
        except ProcessLookupError:
            pass
        terminated = group_dead_within(pgid, timeout=grace_period_seconds)
        if not terminated:
            try:
                os.killpg(pgid, signal.SIGKILL)
                signals_sent.append("SIGKILL")
            except ProcessLookupError:
                pass
            terminated = group_dead_within(pgid, timeout=grace_period_seconds)
        return {"signals_sent": signals_sent, "group_terminated": terminated, "exit_code": None}

    def timeout(self, task_id: str, reason: str = "Task exceeded configured timeout") -> TaskRecord:
        """Time out a task, but only after classifying whether its runtime process
        group (if any) is actually still alive — the same restart-safety
        classification `reconcile()`/`cancel()` use (Phase 5B), applied here to
        close the gap where a timeout clock alone used to finalize (and delete)
        a workspace a still-running process might still be writing to.

        Idempotent: an already-terminal task is returned unchanged. A
        confirmed-alive (or liveness-unconfirmed) process group is never
        treated as evidence the workspace is safe to finalize — it's driven
        through the same in-memory or pgid-based cancellation `cancel()` uses,
        and only a confirmed termination allows the workspace to be released.
        An unconfirmed group lands in `recovery_required` with the
        workspace/lease untouched, never `timed_out` on the caller's say-so
        alone.
        """
        record = self.store.get(task_id)
        if record.state in TERMINAL_STATES:
            return record

        direct_handle = self._direct_api_handles.get(task_id)
        if direct_handle is not None:
            self._direct_api_handles.pop(task_id, None)
            runtime = self.direct_api_runtime
            assert runtime is not None
            cancellation = runtime.cancel(direct_handle)
            if cancellation["group_terminated"]:
                self._finalize_workspace(task_id)
                return self.store.transition(task_id, TaskState.TIMED_OUT, reason, {"reason": reason, "cancellation": cancellation})
            return self.store.transition(
                task_id, TaskState.RECOVERY_REQUIRED,
                "Timeout fired but the direct API request could not be confirmed aborted",
                {"reason": reason, "cancellation": cancellation},
            )

        handle = self._process_handles.get(task_id)
        if handle is not None:
            self._process_handles.pop(task_id, None)
            cancellation = self.subprocess_adapters[record.profile].cancel(handle)
            if cancellation["group_terminated"]:
                self._finalize_workspace(task_id)
                return self.store.transition(task_id, TaskState.TIMED_OUT, reason, {"reason": reason, "cancellation": cancellation})
            return self.store.transition(
                task_id, TaskState.RECOVERY_REQUIRED,
                "Timeout fired but the runtime process group could not be confirmed terminated; workspace retained",
                {"reason": reason, "cancellation": cancellation},
            )

        if record.profile not in self.subprocess_adapters and record.profile != DIRECT_API_PROFILE:
            # Mock tasks never hold a subprocess; nothing to protect.
            self._finalize_workspace(task_id)
            return self.store.transition(task_id, TaskState.TIMED_OUT, reason, {"reason": reason})

        if record.profile == DIRECT_API_PROFILE:
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.TIMED_OUT, reason,
                {"reason": reason, "liveness": "direct_api_no_in_memory_handle"},
            )

        status = self._process_group_status(task_id)
        launch = self.store.get_launch(task_id)
        if launch is not None:
            self.store.mark_launch_reconciled(task_id)

        if status in ("dead", "no_launch"):
            self._finalize_workspace(task_id)
            detail = "process_group_confirmed_dead" if status == "dead" else "missing_process_handle"
            return self.store.transition(
                task_id, TaskState.TIMED_OUT, reason,
                {"reason": reason, "liveness": detail, "pgid": launch["pgid"] if launch else None},
            )

        if status == "alive":
            cancellation = self._cancel_by_pgid(launch["pgid"], grace_period_seconds=self.subprocess_adapters[record.profile].grace_period_seconds)
            if cancellation["group_terminated"]:
                self._finalize_workspace(task_id)
                return self.store.transition(
                    task_id, TaskState.TIMED_OUT, reason,
                    {"reason": reason, "liveness": "process_group_alive_then_terminated", "cancellation": cancellation},
                )
            return self.store.transition(
                task_id, TaskState.RECOVERY_REQUIRED,
                "Timeout fired while the persisted process group was still alive and could not be confirmed terminated; workspace retained",
                {"reason": reason, "liveness": "process_group_alive_after_restart", "cancellation": cancellation},
            )

        # status == "unknown": pgid missing/invalid — never treated as proof of death.
        return self.store.transition(
            task_id, TaskState.RECOVERY_REQUIRED,
            "Timeout fired but process group liveness could not be confirmed from persisted metadata; refusing to finalize",
            {"reason": reason, "liveness": "runtime_metadata_missing_or_invalid"},
        )

    def cancel(self, task_id: str, reason: str) -> TaskRecord:
        """Cancel a task, observing (never assuming) whether the work actually stopped.

        Idempotent for an already-terminal task. When an in-memory process
        handle exists this is the original same-process cancellation path. When
        it doesn't (mock task, or a subprocess-backed task whose handle was lost
        to a broker restart), this consults the durable launch record: a mock
        task (or a subprocess-backed task with no launch record at all — an
        anomaly with nothing to protect) is cancelled immediately; a
        subprocess-backed task with a confirmed-alive persisted process group is
        signalled via its pgid directly (never blindly declared cancelled);
        anything the broker can't confirm is dead is never assumed safe to
        clean up.
        """
        record = self.store.get(task_id)
        if record.state in TERMINAL_STATES:
            return record
        if record.state is TaskState.QUEUED:
            self._finalize_workspace(task_id)
            return self.store.transition(task_id, TaskState.CANCELLED, reason, {"reason": reason})
        direct_handle = self._direct_api_handles.get(task_id)
        if direct_handle is not None:
            runtime = self.direct_api_runtime
            assert runtime is not None
            record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            self._direct_api_handles.pop(record.id, None)
            cancellation = runtime.cancel(direct_handle)
            target = TaskState.CANCELLED if cancellation["group_terminated"] else TaskState.FAILED
            message = (
                "Direct API request aborted" if cancellation["group_terminated"]
                else "Direct API request abort unconfirmed"
            )
            if cancellation["group_terminated"]:
                self._finalize_workspace(record.id)
            return self.store.transition(record.id, target, message, {"reason": reason, "cancellation": cancellation})
        handle = self._process_handles.get(task_id)
        if handle is not None:
            adapter_name = self.subprocess_adapters[record.profile].name
            record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            self._process_handles.pop(record.id, None)
            cancellation = self.subprocess_adapters[record.profile].cancel(handle)
            target = TaskState.CANCELLED if cancellation["group_terminated"] else TaskState.FAILED
            message = f"{adapter_name} process group terminated" if cancellation["group_terminated"] else f"{adapter_name} process group termination unconfirmed"
            if cancellation["group_terminated"]:
                self._finalize_workspace(record.id)
            return self.store.transition(record.id, target, message, {"reason": reason, "cancellation": cancellation})

        adopted = self._adopted_durable_handles.get(task_id)
        if adopted is not None:
            adapter_name = adopted.adapter_id
            record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            cancellation = adopted_cancel(adopted)
            self._adopted_durable_handles.pop(task_id, None)
            self.store.release_recovery_lease(task_id)
            target = TaskState.CANCELLED if cancellation["group_terminated"] else TaskState.FAILED
            message = (
                f"{adapter_name} adopted durable process group terminated"
                if cancellation["group_terminated"] else
                f"{adapter_name} adopted durable cancellation unconfirmed"
            )
            if cancellation["group_terminated"]:
                self._finalize_workspace(record.id)
            return self.store.transition(record.id, target, message, {"reason": reason, "cancellation": cancellation, "adopted_durable": True})

        if record.profile not in self.subprocess_adapters and record.profile != DIRECT_API_PROFILE:
            self._finalize_workspace(task_id)
            if record.state is not TaskState.CANCELLING:
                self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            return self.store.transition(task_id, TaskState.CANCELLED, "Mock cancellation confirmed", {"reason": reason})

        if record.profile == DIRECT_API_PROFILE:
            if record.state is not TaskState.CANCELLING:
                self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.CANCELLED,
                "Direct API request marked cancelled; in-flight HTTP cannot be aborted after broker restart",
                {"reason": reason, "note": "no_session_reattachment"},
            )

        adapter_name = self.subprocess_adapters[record.profile].name
        status = self._process_group_status(task_id)
        launch = self.store.get_launch(task_id)
        if launch is not None:
            self.store.mark_launch_reconciled(task_id)

        if status == "no_launch":
            # Anomalous for a subprocess-backed task (start() always records a
            # launch before RUNNING) — nothing durable to signal or protect either way.
            self._finalize_workspace(task_id)
            if record.state is not TaskState.CANCELLING:
                self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
            return self.store.transition(task_id, TaskState.CANCELLED, "Mock cancellation confirmed", {"reason": reason})

        if status == "unknown":
            # Never signal a pgid we can't confirm liveness for.
            if record.state in (TaskState.PREPARING, TaskState.RUNNING):
                return self.store.transition(
                    task_id, TaskState.RECOVERY_REQUIRED,
                    "Cannot confirm process group liveness from persisted metadata; refusing to signal an unverified pgid",
                    {"reason": "runtime_metadata_missing_or_invalid"},
                )
            self.store.event(
                task_id, "task.reconciliation_checked", record.state, record.state,
                "Cancellation requested but process group liveness still cannot be confirmed",
                {"reason": "runtime_metadata_missing_or_invalid"},
            )
            return record

        if record.state is not TaskState.CANCELLING:
            self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})

        if status == "dead":
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.CANCELLED, f"{adapter_name} process group already terminated",
                {"reason": reason, "cancellation": {"signals_sent": [], "group_terminated": True, "note": "confirmed dead via persisted pgid before any signal was sent"}},
            )

        cancellation = self._cancel_by_pgid(launch["pgid"], grace_period_seconds=self.subprocess_adapters[record.profile].grace_period_seconds)
        if cancellation["group_terminated"]:
            self._finalize_workspace(task_id)
            return self.store.transition(
                task_id, TaskState.CANCELLED, f"{adapter_name} process group terminated via persisted pgid after broker restart",
                {"reason": reason, "cancellation": cancellation},
            )
        return self.store.transition(
            task_id, TaskState.RECOVERY_REQUIRED, "Cancellation signalled via persisted pgid but termination could not be confirmed",
            {"reason": reason, "cancellation": cancellation},
        )

    def operator_control(
        self,
        task_id: str,
        action: str,
        *,
        reason: str = "Cancelled by operator control",
        message_content: str | None = None,
    ) -> dict:
        """Bounded operator recovery/control: explicit action, fail-closed gates (Phase 7C.4)."""
        from .operator_control import execute_operator_control

        return execute_operator_control(
            self,
            task_id,
            action,
            reason=reason,
            message_content=message_content,
        )

    def operator_control_view(self, task_id: str) -> dict:
        from .operator_control import build_operator_control_view

        return build_operator_control_view(self, task_id)

    def status(self, task_id: str) -> dict:
        record = self.store.get(task_id)
        payload = {
            **record.json(),
            "events": self.store.events(task_id),
            "artifacts": self.store.artifact_manifest(task_id),
            "runtime_launch": self.store.get_launch(task_id),
        }
        detail = self.reconcile_detail(task_id)
        if detail is not None:
            payload["reconciliation"] = detail
        adopted = self._adopted_durable_handles.get(task_id)
        if adopted is not None:
            payload["adopted_durable"] = adopted_status(adopted)
        return payload

    def _finalize_workspace(self, task_id: str) -> None:
        """Capture diff/status evidence and release a task's worktree lease, if any.

        Idempotent: a lease that is missing or already released means a prior
        cleanup already ran (or none was ever allocated), so this is a no-op.
        Diff capture failures are recorded, not raised — a broken git command
        must never block the lease/worktree release that would otherwise
        block every future writer to that source.
        """
        lease = self.store.get_lease(task_id)
        if lease is None or lease["status"] != "active":
            return
        payload = {
            "task_id": task_id,
            "source": lease["canonical_source"],
            "worktree_path": lease["worktree_path"],
            "branch": lease["branch"],
            "base_sha": lease["base_sha"],
            "captured_at": now(),
        }
        try:
            status = self.workspaces.capture_status(lease["worktree_path"], lease["base_sha"])
            payload.update({
                "changed_paths": status["changed_paths"],
                "diff_status": status["diff_status"],
                "diff_artifact": "diff.patch",
            })
            diff_bytes = status["diff_bytes"]
        except WorkspaceError as error:
            payload.update({"changed_paths": [], "diff_status": "unknown", "diff_artifact": "diff.patch", "capture_error": str(error)})
            diff_bytes = b""
        self.store.write_artifact(task_id, "workspace_status.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        self.store.write_artifact(task_id, "diff.patch", diff_bytes)
        release_result = self.workspaces.release(lease["canonical_source"], lease["worktree_path"])
        self.store.release_lease(task_id)
        self.store.event(
            task_id, "task.workspace_released", None, None,
            "Broker released the isolated worktree", {"release": release_result},
        )

    def _load_verify_commands(self, task_id: str) -> list[list[str]] | None:
        path = self.store.artifacts / task_id / "verify_commands.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def _apply_verification_gate(self, task_id: str, record: TaskRecord, candidate_state: TaskState) -> tuple[TaskState, dict]:
        """Fold any declared broker-run verification into `candidate_state` per the
        task's verification_policy. Always called after the runtime has
        definitely finished (a runtime result/candidate_state already exists)
        and before `_finalize_workspace` releases the worktree lease, so
        verification still sees the same workspace the runtime task wrote to.

        Declared verify_commands always run (as broker-verified evidence)
        whenever present, independent of the runtime outcome — unconditional
        evidence collection, matching Phase 3/5B's existing behavior and the
        PRD's evidence-first pillar. Whether the *outcome* changes
        `candidate_state` depends on policy:
          - "none": never — evidence-only, the default, fully backward
            compatible with every pre-5C caller.
          - "advisory": a failure downgrades a runtime success to
            succeeded_with_warnings; it never blocks a success outright, and
            never touches an already-failed candidate.
          - "required": a failure (or missing/blocked verification) forces a
            runtime success to failed. A runtime failure is never "rescued"
            by passing verification either way.
        """
        policy = record.verification_policy
        commands = self._load_verify_commands(task_id)
        gate = {"policy": policy, "commands_declared": bool(commands)}
        is_candidate_success = candidate_state in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS)

        if not commands:
            gate["outcome"] = "not_configured"
            if policy == "required" and is_candidate_success:
                gate["outcome"] = "blocked_no_commands_declared"
                return TaskState.FAILED, gate
            return candidate_state, gate

        try:
            verification = self.verify(task_id, commands)
        except Exception as error:
            gate["outcome"] = "blocked_verification_error"
            gate["error"] = str(error)
            if policy == "required" and is_candidate_success:
                return TaskState.FAILED, gate
            return candidate_state, gate

        gate["verification_artifact"] = "verification.json"
        passed = bool(verification["commands"]) and all(command["passed"] for command in verification["commands"])
        gate["outcome"] = "passed" if passed else "failed"

        if passed or not is_candidate_success:
            return candidate_state, gate
        if policy == "required":
            return TaskState.FAILED, gate
        if policy == "advisory":
            return TaskState.SUCCEEDED_WITH_WARNINGS, gate
        return candidate_state, gate  # policy == "none": informational only

    def _write_gate_artifact(self, task_id: str, gate: dict) -> None:
        """Skip writing when nothing meaningful happened (policy=none, no commands
        declared) so a plain evidence-only task's artifact manifest is unchanged
        from pre-5C behavior.
        """
        if not gate["commands_declared"] and gate["policy"] != "required":
            return
        self.store.write_artifact(task_id, "verification_gate.json", json.dumps(gate, indent=2, sort_keys=True) + "\n")

    def verify(self, task_id: str, commands: list[list[str]]) -> dict:
        """Run broker-declared verification commands as argv arrays (never shell=True).

        An isolated_worktree task always runs verification in its worktree,
        never in the source it was allocated from — if that worktree has
        already been released (task finalized, or allocation never
        succeeded), verification is refused outright rather than silently
        falling back to the caller's real workspace. Only a read_only task
        (which never gets a worktree) runs directly against its workspace.
        `broker_verified` is always true here because this is, by
        construction, the broker's own subprocess execution — as opposed to
        whatever an adapter/agent merely reports about itself.

        Refuses outright on a recovery_required task: its worktree lease is
        still active (by design — reconciliation never releases a lease it
        can't prove is safe), but a persisted-alive process group may still be
        writing to it, so running commands there would race an unobserved
        process rather than verify a settled result.
        """
        record = self.store.get(task_id)
        if record.state is TaskState.RECOVERY_REQUIRED:
            raise ValueError(
                "Cannot run verification: task requires reconciliation before further action "
                "(state=recovery_required; its process group may still be alive)"
            )
        lease = self.store.get_lease(task_id)
        if record.execution_mode == ISOLATED_WORKTREE:
            if lease is None or lease["status"] != "active":
                raise ValueError(
                    "Cannot run verification: this task's isolated worktree is not currently active "
                    "(not yet allocated, or already released)"
                )
            working_dir, scope = lease["worktree_path"], "isolated_worktree"
        else:
            working_dir, scope = record.workspace, "source_workspace"
        for command in commands:
            if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
                raise ValueError("Verification commands must be non-empty argv arrays of strings")
        results = []
        for command in commands:
            started = time.monotonic()
            completed = subprocess.run(command, cwd=working_dir, capture_output=True, text=True)
            duration = time.monotonic() - started
            results.append({
                "command": command,
                "cwd": working_dir,
                "exit_code": completed.returncode,
                "passed": completed.returncode == 0,
                "duration_seconds": round(duration, 6),
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "broker_verified": True,
            })
        payload = {"task_id": task_id, "scope": scope, "commands": results, "captured_at": now()}
        self.store.write_artifact(task_id, "verification.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        self.store.event(
            task_id, "task.verified", record.state, record.state,
            "Broker executed verification commands",
            {"scope": scope, "count": len(results), "all_passed": all(r["passed"] for r in results)},
        )
        return payload

    def discover_capabilities(self) -> dict:
        from .discovery import discover_providers, discover_runtimes
        from .recovery_contract import RECOVERY_CONTRACT_SCHEMA_VERSION

        environ = self._environ if self._environ is not None else os.environ
        return {
            "recovery_contract_schema_version": RECOVERY_CONTRACT_SCHEMA_VERSION,
            "runtimes": discover_runtimes(
                profiles=self.profiles,
                subprocess_adapters=self.subprocess_adapters,
                mock_adapter=self.adapter,
                direct_api_runtime=self.direct_api_runtime,
            ),
            "providers": discover_providers(
                direct_api_runtime=self.direct_api_runtime,
                environ=environ,
            ),
        }

    def select_candidates(
        self,
        *,
        execution_mode: str,
        required_runtime_capabilities: dict[str, bool] | None = None,
        required_provider_capabilities: dict[str, bool] | None = None,
        allowed_runtimes: list[str] | None = None,
        allowed_providers: list[str] | None = None,
        require_available: bool = True,
    ) -> dict:
        from .discovery import select_candidates as _select_candidates

        environ = self._environ if self._environ is not None else os.environ
        return _select_candidates(
            profiles=self.profiles,
            subprocess_adapters=self.subprocess_adapters,
            direct_api_runtime=self.direct_api_runtime,
            environ=environ,
            execution_mode=execution_mode,
            required_runtime_capabilities=required_runtime_capabilities,
            required_provider_capabilities=required_provider_capabilities,
            allowed_runtimes=allowed_runtimes,
            allowed_providers=allowed_providers,
            require_available=require_available,
        )

    def validate_council(self, plan: dict) -> dict:
        from .council import validate_council_plan

        return validate_council_plan(self, plan)

    def execute_council(self, plan: dict) -> dict:
        from .council import execute_council as _execute_council

        return _execute_council(self, plan)
