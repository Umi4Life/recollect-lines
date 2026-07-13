from __future__ import annotations

import json
from pathlib import Path

from .models import TaskRecord, TaskRequest, TaskState
from .store import TaskStore


class MockAdapter:
    name = "mock"

    def start_metadata(self, record: TaskRecord) -> dict[str, str]:
        return {"adapter": self.name, "mode": record.execution_mode}


class Broker:
    def __init__(self, home: Path):
        self.store = TaskStore(home)
        self.adapter = MockAdapter()

    def close(self) -> None:
        self.store.close()

    def create(self, request: TaskRequest) -> TaskRecord:
        if not request.task.strip():
            raise ValueError("Task must not be empty")
        if not request.workspace.strip():
            raise ValueError("Workspace must not be empty")
        record = self.store.create(TaskRecord.new(request))
        self.store.write_artifact(record.id, "request.json", json.dumps(record.json(), indent=2) + "\n")
        return self.store.transition(record.id, TaskState.QUEUED, "Task queued", {})

    def start(self, task_id: str) -> TaskRecord:
        record = self.store.transition(task_id, TaskState.PREPARING, "Preparing mock execution", {})
        return self.store.transition(record.id, TaskState.RUNNING, "Mock adapter started", self.adapter.start_metadata(record))

    def complete(self, task_id: str, summary: str) -> TaskRecord:
        record = self.store.transition(task_id, TaskState.COLLECTING, "Collecting mock result", {})
        result = {"task_id": record.id, "state": "succeeded", "summary": summary, "runtime": {"adapter": "mock"}}
        self.store.write_artifact(record.id, "result.json", json.dumps(result, indent=2) + "\n")
        return self.store.transition(record.id, TaskState.SUCCEEDED, "Mock task completed", {"result_artifact": "result.json"})

    def cancel(self, task_id: str, reason: str) -> TaskRecord:
        record = self.store.get(task_id)
        if record.state is TaskState.QUEUED:
            return self.store.transition(task_id, TaskState.CANCELLED, reason, {"reason": reason})
        record = self.store.transition(task_id, TaskState.CANCELLING, reason, {"reason": reason})
        return self.store.transition(record.id, TaskState.CANCELLED, "Mock cancellation confirmed", {"reason": reason})

    def status(self, task_id: str) -> dict:
        record = self.store.get(task_id)
        return {**record.json(), "events": self.store.events(task_id)}
