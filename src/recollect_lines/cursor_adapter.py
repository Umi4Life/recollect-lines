"""Deprecated compatibility shim — import from ``recollect_lines.adaptor.cursor`` instead."""

from __future__ import annotations

from recollect_lines.adaptor.cursor import (
    DEFAULT_COMMAND_PREFIX,
    DEFAULT_GRACE_PERIOD_SECONDS,
    RUNTIME_DESCRIPTION,
    CursorAdapter,
    CursorUnsupportedPolicy,
    ProcessHandle,
    redact_secrets,
)

__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "DEFAULT_GRACE_PERIOD_SECONDS",
    "RUNTIME_DESCRIPTION",
    "CursorAdapter",
    "CursorUnsupportedPolicy",
    "ProcessHandle",
    "redact_secrets",
]
