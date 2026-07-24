"""Runtime adapter boundary: shared types and capability reporting for task execution backends."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..durable_runner import STDOUT_NAME, validate_artifact_basename
from ..recovery_contract import RecoveryControlContract


@dataclass(frozen=True)
class LaunchSpec:
    """Provider-neutral, immutable description of a single CLI launch (RFC-004).

    Carries only what the broker/durable supervisor needs to launch a process
    safely and later find its structured result: argv, the working
    directory, an optional sanitized environment override, and a hint for
    where the structured result lives. Never a process handle, Popen,
    wait/poll/reap, worktree deletion, persistence, or recovery policy --
    those belong to the broker and durable_runner.DurableSubprocessRunner,
    never to an adapter.

    `stdout_artifact_name` is the durable-captured filename the generic
    supervisor redirects the payload's stdout into (see
    durable_runner.DurableSubprocessRunner.launch and
    durable_cli_launch.start_durable_cli_launch, which reports it back as
    `events_artifact`). It defaults to the generic "stdout.log" every other
    provider uses; an adapter only overrides it to preserve an established
    public artifact name for its own terminal-stdout format -- e.g. Codex's
    `--json` NDJSON stream, whose historical/documented artifact name is
    `events.jsonl` (RFC-001), not the generic durable default. Validated as a
    safe basename with no path traversal.
    """

    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str] | None = None
    result_stream: str = "stdout"
    stdout_artifact_name: str = STDOUT_NAME

    def __post_init__(self) -> None:
        validate_artifact_basename(self.stdout_artifact_name)


@dataclass(frozen=True)
class AdapterCapabilities:
    requires_subprocess: bool
    supports_process_group_cancellation: bool
    reports_broker_verified_tests: bool
    recovery_control: RecoveryControlContract
    uses_durable_subprocess_runner: bool = False
    # None preserves legacy behavior: this adapter does not restrict schemas.
    # A set is an explicit pre-launch schema allowlist.
    supported_result_schemas: frozenset[str] | None = None


@runtime_checkable
class RuntimeAdapter(Protocol):
    name: str
    capabilities: AdapterCapabilities
