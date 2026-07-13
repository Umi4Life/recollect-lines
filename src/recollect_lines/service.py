from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .adapters import AdapterCapabilities
from .models import DEFAULT_PROFILES, ProfilePolicy, TaskRecord, TaskRequest, TaskState, WorkspaceLeaseConflict, now, validate_result
from .opencode_adapter import OpenCodeAdapter
from .store import TaskStore
from .workspace import WorkspaceError, WorkspaceManager, canonical_source, capture_head

ISOLATED_WORKTREE = "isolated_worktree"


class MockAdapter:
    name = "mock"
    capabilities = AdapterCapabilities(
        requires_subprocess=False,
        supports_process_group_cancellation=False,
        reports_broker_verified_tests=False,
    )

    def start_metadata(self, record: TaskRecord, workspace: str) -> dict[str, str]:
        return {"adapter": self.name, "mode": record.execution_mode, "workspace": workspace}


class Broker:
    def __init__(self, home: Path, profiles: dict[str, ProfilePolicy] | None = None, opencode_adapter: OpenCodeAdapter | None = None):
        self.store = TaskStore(home)
        self.adapter = MockAdapter()
        self.opencode_adapter = opencode_adapter or OpenCodeAdapter()
        self.profiles = profiles or DEFAULT_PROFILES
        self.workspaces = WorkspaceManager(self.store.home)
        # ponytail: in-memory only, one broker process per running task; a restart
        # loses the handle. Durable re-attach remains out of scope (see docs/phase-2.md).
        self._process_handles: dict[str, object] = {}

    def close(self) -> None:
        self.store.close()

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
        if self.store.active_count(policy.name) >= policy.max_concurrency:
            raise ValueError(f"Profile {policy.name} concurrency limit reached")
        return policy

    def create(self, request: TaskRequest) -> TaskRecord:
        self._validate_request(request)
        record = self.store.create(TaskRecord.new(request))
        self.store.write_artifact(record.id, "request.json", json.dumps(record.json(), indent=2) + "\n")
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
        if record.profile == "opencode":
            try:
                metadata, handle = self.opencode_adapter.start(record, self.store.artifacts / record.id, workspace=effective_workspace)
            except Exception:
                # A losing writer never allocates, but a *successful* allocation
                # whose adapter then fails to launch must still give up its lease —
                # otherwise this source stays blocked for every future writer.
                if record.execution_mode == ISOLATED_WORKTREE:
                    self.workspaces.release(source, worktree_path)
                    self.store.release_lease(record.id)
                raise
            self._process_handles[record.id] = handle
            self.store.refresh_manifest(record.id)
            return self.store.transition(record.id, TaskState.RUNNING, "OpenCode adapter started", metadata)
        return self.store.transition(record.id, TaskState.RUNNING, "Mock adapter started", self.adapter.start_metadata(record, effective_workspace))

    def complete(self, task_id: str, summary: str) -> TaskRecord:
        record = self.store.transition(task_id, TaskState.COLLECTING, "Collecting mock result", {})
        result = {"task_id": record.id, "state": "succeeded", "summary": summary, "runtime": {"adapter": "mock"}}
        validate_result(result, record.id)
        self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
        self._finalize_workspace(record.id)
        return self.store.transition(record.id, TaskState.SUCCEEDED, "Mock task completed", {"result_artifact": "result.json"})

    def collect(self, task_id: str) -> TaskRecord:
        handle = self._process_handles.pop(task_id, None)
        if handle is None:
            # A broker restart loses the handle but not the OS process (it was
            # started with start_new_session=True). Only clean up the worktree
            # if the last known process group is confirmed dead — never delete
            # files out from under a group that might still be writing to them.
            if not self._last_known_process_group_alive(task_id):
                self._finalize_workspace(task_id)
            return self.store.transition(
                task_id,
                TaskState.FAILED,
                "No running OpenCode process handle for task (broker restart or already collected)",
                {"reason": "missing_process_handle"},
            )
        record = self.store.transition(task_id, TaskState.COLLECTING, "Collecting OpenCode result", {})
        collected = self.opencode_adapter.collect(handle)
        self.store.refresh_manifest(record.id)
        runtime = {"adapter": "opencode", **collected}
        if collected["exit_code"] != 0:
            result = {"task_id": record.id, "state": TaskState.FAILED.value, "summary": collected["summary"] or "OpenCode exited with a non-zero status", "runtime": runtime}
            self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
            self._finalize_workspace(record.id)
            return self.store.transition(record.id, TaskState.FAILED, "OpenCode task failed", {"result_artifact": "result.json", "exit_code": collected["exit_code"]})
        state = TaskState.SUCCEEDED if collected["summary"] else TaskState.SUCCEEDED_WITH_WARNINGS
        result = {"task_id": record.id, "state": state.value, "summary": collected["summary"] or "OpenCode run produced no text result", "runtime": runtime}
        validate_result(result, record.id)
        self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
        self._finalize_workspace(record.id)
        return self.store.transition(record.id, state, "OpenCode task completed", {"result_artifact": "result.json", "exit_code": collected["exit_code"]})

    def timeout(self, task_id: str, reason: str = "Task exceeded configured timeout") -> TaskRecord:
        self._finalize_workspace(task_id)
        return self.store.transition(task_id, TaskState.TIMED_OUT, reason, {"reason": reason})

    def cancel(self, task_id: str, reason: str) -> TaskRecord:
        record = self.store.get(task_id)
        if record.state is TaskState.QUEUED:
            self._finalize_workspace(task_id)
            return self.store.transition(task_id, TaskState.CANCELLED, reason, {"reason": reason})
        record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
        handle = self._process_handles.pop(record.id, None)
        if handle is not None:
            cancellation = self.opencode_adapter.cancel(handle)
            target = TaskState.CANCELLED if cancellation["group_terminated"] else TaskState.FAILED
            message = "OpenCode process group terminated" if cancellation["group_terminated"] else "OpenCode process group termination unconfirmed"
            if cancellation["group_terminated"]:
                self._finalize_workspace(record.id)
            return self.store.transition(record.id, target, message, {"reason": reason, "cancellation": cancellation})
        self._finalize_workspace(record.id)
        return self.store.transition(record.id, TaskState.CANCELLED, "Mock cancellation confirmed", {"reason": reason})

    def status(self, task_id: str) -> dict:
        record = self.store.get(task_id)
        return {**record.json(), "events": self.store.events(task_id), "artifacts": self.store.artifact_manifest(task_id)}

    def _last_known_process_group_alive(self, task_id: str) -> bool:
        run_event = next((e for e in reversed(self.store.events(task_id)) if e["type"] == "task.running"), None)
        pgid = run_event["metadata"].get("pgid") if run_event else None
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

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
        """
        record = self.store.get(task_id)
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
