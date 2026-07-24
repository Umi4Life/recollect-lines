"""Generic, broker-owned launch/collect/cancel orchestration for LaunchSpec-based
CLI adapters running under the durable subprocess supervisor (RFC-004).

An adapter that only builds a `LaunchSpec` (adaptor.contracts.LaunchSpec) and
parses a provider-specific result never touches `subprocess.Popen`, never
waits/polls/kills a process group, and never creates stdout/stderr files
itself -- all of that lifecycle is owned here and in
`durable_runner.DurableSubprocessRunner`, which the broker constructs and
injects into the adapter. Adapters must not duplicate this logic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adaptor.contracts import LaunchSpec
from .durable_reconciliation import LAUNCH_KIND_DURABLE, wait_for_durable_running
from .durable_runner import (
    DurableLaunchHandle,
    DurableSubprocessRunner,
    STATE_CANCELLED,
    STATE_EXITED,
    STATE_FAILED,
    STATE_TIMED_OUT,
    load_launch_record,
    stdout_artifact_name_from_record,
)
from .models import TaskRecord

TERMINAL_LAUNCH_STATES = frozenset({STATE_EXITED, STATE_TIMED_OUT, STATE_CANCELLED, STATE_FAILED})


@dataclass
class DurableCliHandle:
    """Opaque handle returned to the broker; carries no Popen object."""

    task_id: str
    launch_id: str
    pid: int
    pgid: int
    durable: DurableLaunchHandle
    runner: DurableSubprocessRunner


def start_durable_cli_launch(
    runner: DurableSubprocessRunner,
    *,
    record: TaskRecord,
    adapter_id: str,
    spec: LaunchSpec,
    artifacts_dir: Path,
) -> tuple[dict[str, Any], DurableCliHandle]:
    """Launch `spec` under the durable supervisor and wait for launch proof.

    Returns the same `(metadata, handle)` shape every subprocess adapter's
    `start()` returns to the broker; `metadata["launch_kind"]` is always
    `durable_subprocess` and `metadata["durable_launch_id"]` is always set,
    so `Broker.start()`'s existing generic dispatch needs no adapter-specific
    branching to persist it.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    durable = runner.launch(
        task_id=record.id,
        adapter_id=adapter_id,
        command=list(spec.argv),
        cwd=spec.cwd,
        stdout_artifact_name=spec.stdout_artifact_name,
    )
    launch_record = wait_for_durable_running(durable.manifest_path, supervisor=durable.supervisor)
    pid = launch_record.process["pid"]
    pgid = launch_record.process["pgid"]
    handle = DurableCliHandle(
        task_id=record.id,
        launch_id=durable.launch_id,
        pid=pid,
        pgid=pgid,
        durable=durable,
        runner=runner,
    )
    metadata = {
        "adapter": adapter_id,
        "durable_launch_id": durable.launch_id,
        "launch_kind": LAUNCH_KIND_DURABLE,
        "pid": pid,
        "pgid": pgid,
        "events_artifact": spec.stdout_artifact_name,
        "stderr_artifact": "stderr.log",
        "workspace": spec.cwd,
    }
    return metadata, handle


def is_durable_launch_terminal(handle: DurableCliHandle) -> bool:
    """Nonblocking: read-only manifest check, never a process wait/signal."""
    return load_launch_record(handle.durable.manifest_path).lifecycle_state in TERMINAL_LAUNCH_STATES


def wait_for_durable_launch_terminal(handle: DurableCliHandle, *, timeout: float, interval: float = 0.05) -> bool:
    """Bounded, nonblocking-safe poll for a terminal manifest state.

    Never calls DurableSubprocessRunner.wait()/cancel() -- those can
    terminate a genuinely live payload on timeout, which collect() must
    never do to a task that is still legitimately running (see PR #67).
    Returns False (still running) if `timeout` elapses first.
    """
    deadline = time.monotonic() + timeout
    while not is_durable_launch_terminal(handle):
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def cancel_durable_cli_launch(handle: DurableCliHandle) -> dict[str, Any]:
    record = handle.runner.cancel(handle.durable)
    terminated = record.lifecycle_state in TERMINAL_LAUNCH_STATES
    return {
        "signals_sent": ["SIGTERM", "SIGKILL"] if terminated else [],
        "group_terminated": terminated,
        "exit_code": record.exit_status.get("code") if record.exit_status else None,
    }


def collect_durable_cli_launch(
    handle: DurableCliHandle,
    *,
    parse_result: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Read bounded durable artifacts and hand them to the adapter's
    provider-specific `parse_result(stdout_text=, stderr_text=,
    process_exit_code=)` -- the only place Cursor-shaped (or any other
    provider-shaped) parsing happens.
    """
    record = load_launch_record(handle.durable.manifest_path)
    if record.lifecycle_state not in TERMINAL_LAUNCH_STATES:
        raise ValueError("durable payload still running; collect refused until terminal")
    stdout_path = handle.durable.launch_dir / stdout_artifact_name_from_record(record)
    stderr_path = handle.durable.launch_dir / "stderr.log"
    stdout_text = stdout_path.read_text(errors="replace") if stdout_path.is_file() else ""
    stderr_text = stderr_path.read_text(errors="replace") if stderr_path.is_file() else ""
    exit_code = record.exit_status.get("code") if record.exit_status else None
    process_exit_code = exit_code if exit_code is not None else 1
    return parse_result(stdout_text=stdout_text, stderr_text=stderr_text, process_exit_code=process_exit_code)
