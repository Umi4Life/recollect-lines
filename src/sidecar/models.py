from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class TaskState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    COLLECTING = "collecting"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"


TERMINAL_STATES = frozenset({
    TaskState.SUCCEEDED,
    TaskState.SUCCEEDED_WITH_WARNINGS,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.TIMED_OUT,
    TaskState.REJECTED,
})

ALLOWED_TRANSITIONS = {
    TaskState.CREATED: {TaskState.QUEUED, TaskState.REJECTED},
    TaskState.QUEUED: {TaskState.PREPARING, TaskState.CANCELLED, TaskState.REJECTED},
    TaskState.PREPARING: {TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RUNNING: {TaskState.COLLECTING, TaskState.CANCELLING, TaskState.FAILED, TaskState.TIMED_OUT},
    TaskState.CANCELLING: {TaskState.CANCELLED, TaskState.FAILED},
    TaskState.COLLECTING: {TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS, TaskState.FAILED},
}


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TaskRequest:
    task: str
    workspace: str
    execution_mode: str = "read_only"
    profile: str = "mock"
    timeout_seconds: int = 1800


@dataclass(frozen=True)
class TaskRecord:
    id: str
    task: str
    workspace: str
    execution_mode: str
    profile: str
    timeout_seconds: int
    state: TaskState
    created_at: str
    updated_at: str

    @classmethod
    def new(cls, request: TaskRequest) -> "TaskRecord":
        timestamp = now()
        return cls(
            id=f"tsk_{uuid4().hex}",
            task=request.task,
            workspace=request.workspace,
            execution_mode=request.execution_mode,
            profile=request.profile,
            timeout_seconds=request.timeout_seconds,
            state=TaskState.CREATED,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def json(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


class InvalidTransition(ValueError):
    pass
