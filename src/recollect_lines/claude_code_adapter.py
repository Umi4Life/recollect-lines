"""Deprecated compatibility shim — import from ``recollect_lines.adaptor.claude_code`` instead."""

from __future__ import annotations

from recollect_lines.adaptor.claude_code import (
    DEFAULT_COMMAND_PREFIX,
    DEFAULT_GRACE_PERIOD_SECONDS,
    RUNTIME_DESCRIPTION,
    ClaudeCodeAdapter,
    ClaudeCodeUnsupportedPolicy,
    ProcessHandle,
    redact_secrets,
)

__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "DEFAULT_GRACE_PERIOD_SECONDS",
    "RUNTIME_DESCRIPTION",
    "ClaudeCodeAdapter",
    "ClaudeCodeUnsupportedPolicy",
    "ProcessHandle",
    "redact_secrets",
]
