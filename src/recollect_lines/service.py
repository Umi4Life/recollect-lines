from __future__ import annotations

import json
from pathlib import Path

from .adapters import AdapterCapabilities
from .models import DEFAULT_PROFILES, ProfilePolicy, TaskRecord, TaskRequest, TaskState, validate_result
from .opencode_adapter import OpenCodeAdapter
from .store import TaskStore


class MockAdapter:
    name = "mock"
    capabilities = AdapterCapabilities(
        requires_subprocess=False,
        supports_process_group_cancellation=False,
        reports_broker_verified_tests=False,
    )

    def start_metadata(self, record: TaskRecord) -> dict[str, str]:
        return {"adapter": self.name, "mode": record.execution_mode}


class Broker:
    def __init__(self, home: Path, profiles: dict[str, ProfilePolicy] | None = None, opencode_adapter: OpenCodeAdapter | None = None):
        self.store = TaskStore(home)
        self.adapter = MockAdapter()
        self.opencode_adapter = opencode_adapter or OpenCodeAdapter()
        self.profiles = profiles or DEFAULT_PROFILES
        # ponytail: in-memory only, one broker process per running task; a restart
        # loses the handle. Durable re-attach is a Phase 3 concern (see docs/phase-2.md).
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
        if record.profile == "opencode":
            metadata, handle = self.opencode_adapter.start(record, self.store.artifacts / record.id)
            self._process_handles[record.id] = handle
            self.store.refresh_manifest(record.id)
            return self.store.transition(record.id, TaskState.RUNNING, "OpenCode adapter started", metadata)
        return self.store.transition(record.id, TaskState.RUNNING, "Mock adapter started", self.adapter.start_metadata(record))

    def complete(self, task_id: str, summary: str) -> TaskRecord:
        record = self.store.transition(task_id, TaskState.COLLECTING, "Collecting mock result", {})
        result = {"task_id": record.id, "state": "succeeded", "summary": summary, "runtime": {"adapter": "mock"}}
        validate_result(result, record.id)
        self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
        return self.store.transition(record.id, TaskState.SUCCEEDED, "Mock task completed", {"result_artifact": "result.json"})

    def collect(self, task_id: str) -> TaskRecord:
        record = self.store.transition(task_id, TaskState.COLLECTING, "Collecting OpenCode result", {})
        handle = self._process_handles.pop(record.id, None)
        if handle is None:
            raise ValueError(f"No running OpenCode process for task: {task_id}")
        collected = self.opencode_adapter.collect(handle)
        self.store.refresh_manifest(record.id)
        runtime = {"adapter": "opencode", **collected}
        if collected["exit_code"] != 0:
            result = {"task_id": record.id, "state": TaskState.FAILED.value, "summary": collected["summary"] or "OpenCode exited with a non-zero status", "runtime": runtime}
            self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
            return self.store.transition(record.id, TaskState.FAILED, "OpenCode task failed", {"result_artifact": "result.json", "exit_code": collected["exit_code"]})
        state = TaskState.SUCCEEDED if collected["summary"] else TaskState.SUCCEEDED_WITH_WARNINGS
        result = {"task_id": record.id, "state": state.value, "summary": collected["summary"] or "OpenCode run produced no text result", "runtime": runtime}
        validate_result(result, record.id)
        self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
        return self.store.transition(record.id, state, "OpenCode task completed", {"result_artifact": "result.json", "exit_code": collected["exit_code"]})

    def timeout(self, task_id: str, reason: str = "Task exceeded configured timeout") -> TaskRecord:
        return self.store.transition(task_id, TaskState.TIMED_OUT, reason, {"reason": reason})

    def cancel(self, task_id: str, reason: str) -> TaskRecord:
        record = self.store.get(task_id)
        if record.state is TaskState.QUEUED:
            return self.store.transition(task_id, TaskState.CANCELLED, reason, {"reason": reason})
        record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
        handle = self._process_handles.pop(record.id, None)
        if handle is not None:
            cancellation = self.opencode_adapter.cancel(handle)
            target = TaskState.CANCELLED if cancellation["group_terminated"] else TaskState.FAILED
            message = "OpenCode process group terminated" if cancellation["group_terminated"] else "OpenCode process group termination unconfirmed"
            return self.store.transition(record.id, target, message, {"reason": reason, "cancellation": cancellation})
        return self.store.transition(record.id, TaskState.CANCELLED, "Mock cancellation confirmed", {"reason": reason})

    def status(self, task_id: str) -> dict:
        record = self.store.get(task_id)
        return {**record.json(), "events": self.store.events(task_id), "artifacts": self.store.artifact_manifest(task_id)}
