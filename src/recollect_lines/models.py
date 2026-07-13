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
    # Non-terminal, actionable: a durable runtime launch record exists for this
    # task but the broker has no in-memory process handle (e.g. after a
    # restart) and cannot confirm the process group is dead. The broker never
    # fabricates a result from here; an operator must reconcile (which may
    # resolve it to failed, or attempt a persisted-pgid cancellation).
    RECOVERY_REQUIRED = "recovery_required"
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
    # CANCELLING/RECOVERY_REQUIRED are reachable from PREPARING too: a broker
    # can crash between recording an opencode launch and the later RUNNING
    # transition, leaving a durable launch record for a task still at
    # PREPARING (see Broker.reconcile()).
    TaskState.PREPARING: {
        TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED, TaskState.CANCELLING, TaskState.RECOVERY_REQUIRED,
    },
    TaskState.RUNNING: {
        TaskState.COLLECTING, TaskState.CANCELLING, TaskState.FAILED, TaskState.TIMED_OUT, TaskState.RECOVERY_REQUIRED,
    },
    TaskState.CANCELLING: {TaskState.CANCELLED, TaskState.FAILED, TaskState.RECOVERY_REQUIRED},
    TaskState.COLLECTING: {TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS, TaskState.FAILED},
    TaskState.RECOVERY_REQUIRED: {TaskState.CANCELLING, TaskState.FAILED},
}


@dataclass(frozen=True)
class ProfilePolicy:
    name: str
    allowed_modes: frozenset[str]
    max_timeout_seconds: int
    max_concurrency: int


DEFAULT_PROFILES = {
    "mock": ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 2),
    "opencode": ProfilePolicy("opencode", frozenset({"read_only", "isolated_worktree"}), 3600, 2),
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


class WorkspaceLeaseConflict(ValueError):
    pass


class RecoveryRequired(ValueError):
    """Raised by `Broker.collect()` when a task needs explicit reconciliation.

    Subclasses ValueError so existing CLI/MCP error handling (which already
    treats ValueError as a reportable business error, not an internal fault)
    surfaces it without any new plumbing.
    """

    def __init__(self, task_id: str, state: TaskState):
        self.task_id = task_id
        self.state = state
        super().__init__(
            f"Task {task_id} is {state.value} and requires reconciliation "
            "(call Broker.reconcile()/`recollect reconcile`) before it can be collected"
        )


def validate_result(result: dict[str, Any], task_id: str) -> None:
    required = {"task_id", "state", "summary", "runtime"}
    missing = required - result.keys()
    if missing:
        raise ValueError(f"Result missing required fields: {', '.join(sorted(missing))}")
    if result["task_id"] != task_id:
        raise ValueError("Result task_id does not match the task")
    if result["state"] not in {TaskState.SUCCEEDED.value, TaskState.SUCCEEDED_WITH_WARNINGS.value}:
        raise ValueError("Result state must be a successful terminal state")
    if not isinstance(result["summary"], str) or not result["summary"].strip():
        raise ValueError("Result summary must be a non-empty string")
    if not isinstance(result["runtime"], dict) or not isinstance(result["runtime"].get("adapter"), str):
        raise ValueError("Result runtime.adapter must be a string")
