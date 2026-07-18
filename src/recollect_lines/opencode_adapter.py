"""Deprecated compatibility shim — import from ``recollect_lines.adaptor.opencode`` instead."""

from __future__ import annotations

from recollect_lines.adaptor.opencode import (
    DEFAULT_COMMAND_PREFIX,
    DEFAULT_GRACE_PERIOD_SECONDS,
    OpenCodeAdapter,
    ProcessHandle,
    cancel_process_group,
    group_alive,
    group_dead_within,
    redact_command,
)

__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "DEFAULT_GRACE_PERIOD_SECONDS",
    "OpenCodeAdapter",
    "ProcessHandle",
    "cancel_process_group",
    "group_alive",
    "group_dead_within",
    "redact_command",
]
