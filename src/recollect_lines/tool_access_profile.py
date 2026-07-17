"""Tool-access-profile model, separate from execution_mode (RFC-002 PR 4).

``execution_mode`` continues to govern workspace-mutation authority
(``read_only`` vs ``isolated_worktree``). ``tool_access_profile`` is an
orthogonal, explicit dimension governing which runtime tool identifiers a
launch may use. Profiles are named, validated, explicit allowlist/policy
descriptors -- never a boolean, never a broad "all MCP tools" switch -- so a
future safe read-only remote/MCP profile has an explicit seam to slot into
without touching execution_mode or widening either existing profile.

Both profiles defined here only reproduce Claude Code's pre-existing
``--tools``/``--disallowedTools`` mapping (see claude_code_adapter.py); no new
runtime tool identifiers, no MCP server, no network or credential access is
introduced. Omitting ``tool_access_profile`` reproduces today's behavior
exactly (see ``resolve_tool_access_profile``'s default table).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LOCAL_WORKSPACE_READ_ONLY = "local_workspace_read_only"
LOCAL_WORKSPACE_STANDARD = "local_workspace_standard"

KNOWN_TOOL_ACCESS_PROFILE_IDS = frozenset({LOCAL_WORKSPACE_READ_ONLY, LOCAL_WORKSPACE_STANDARD})


class ToolAccessProfileValidationError(ValueError):
    """Invalid, incompatible, or unavailable tool_access_profile selection."""


@dataclass(frozen=True)
class ToolAccessProfile:
    name: str
    # None means no --tools allowlist is applied (native default CLI toolset).
    allowed_tools: tuple[str, ...] | None
    disallowed_tools: tuple[str, ...]
    compatible_execution_modes: frozenset[str]
    # Semantic capability ids (required_capabilities.py) this profile can ever advertise.
    semantic_capabilities: frozenset[str]


_PROFILES: dict[str, ToolAccessProfile] = {
    LOCAL_WORKSPACE_READ_ONLY: ToolAccessProfile(
        name=LOCAL_WORKSPACE_READ_ONLY,
        allowed_tools=("Read", "Grep", "Glob"),
        disallowed_tools=("Edit", "Write", "NotebookEdit"),
        compatible_execution_modes=frozenset({"read_only"}),
        semantic_capabilities=frozenset({"workspace.read"}),
    ),
    LOCAL_WORKSPACE_STANDARD: ToolAccessProfile(
        name=LOCAL_WORKSPACE_STANDARD,
        allowed_tools=None,
        disallowed_tools=(),
        compatible_execution_modes=frozenset({"isolated_worktree"}),
        semantic_capabilities=frozenset({"workspace.read"}),
    ),
}

# Runtimes that implement per-tool restriction via a tool-access profile today.
# A runtime absent here has no profile concept: requesting one explicitly is
# "unavailable"; omitting one changes nothing (matches pre-existing behavior).
_RUNTIME_PROFILE_AVAILABILITY: dict[str, frozenset[str]] = {
    "claude_code": KNOWN_TOOL_ACCESS_PROFILE_IDS,
}

# Deterministic default per (runtime, execution_mode) when tool_access_profile
# is omitted -- this table is what makes omission byte-equivalent to today.
_DEFAULT_PROFILE_BY_RUNTIME_MODE: dict[tuple[str, str], str] = {
    ("claude_code", "read_only"): LOCAL_WORKSPACE_READ_ONLY,
    ("claude_code", "isolated_worktree"): LOCAL_WORKSPACE_STANDARD,
}


def normalize_tool_access_profile(raw: Any) -> str | None:
    """Validate the request-level ``tool_access_profile`` field format.

    Only checks vocabulary (non-empty string, known id) -- runtime/execution_mode
    compatibility is a separate preflight concern, mirroring
    required_capabilities.py's format/satisfaction split.
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ToolAccessProfileValidationError(
            "tool_access_profile must be a non-empty string when provided"
        )
    profile_id = raw.strip()
    if profile_id not in KNOWN_TOOL_ACCESS_PROFILE_IDS:
        raise ToolAccessProfileValidationError(
            f"Unknown tool_access_profile {profile_id!r}; known ids: {sorted(KNOWN_TOOL_ACCESS_PROFILE_IDS)}"
        )
    return profile_id


def evaluate_tool_access_profile_preflight(
    *, runtime: str, execution_mode: str, requested_profile: str | None,
) -> dict[str, Any] | None:
    """Return machine-readable rejection metadata for an explicit profile selection
    that is unavailable for ``runtime`` or incompatible with ``execution_mode``.

    None means resolution may proceed (profile omitted, or an explicit
    compatible one). Assumes ``requested_profile`` already passed
    ``normalize_tool_access_profile`` (or is None).
    """
    if requested_profile is None:
        return None
    available = _RUNTIME_PROFILE_AVAILABILITY.get(runtime, frozenset())
    if requested_profile not in available:
        return {
            "reason": "unavailable_tool_access_profile",
            "tool_access_profile": requested_profile,
            "runtime": runtime,
            "available_tool_access_profiles": sorted(available),
        }
    profile = _PROFILES[requested_profile]
    if execution_mode not in profile.compatible_execution_modes:
        return {
            "reason": "incompatible_tool_access_profile",
            "tool_access_profile": requested_profile,
            "execution_mode": execution_mode,
            "compatible_execution_modes": sorted(profile.compatible_execution_modes),
        }
    return None


def resolve_tool_access_profile(
    *, runtime: str, execution_mode: str, requested_profile: str | None,
) -> ToolAccessProfile | None:
    """Deterministically resolve the effective tool-access profile.

    Returns None when no tool-access-profile concept applies to ``runtime`` and
    none was requested -- callers fall back to their pre-existing behavior
    (today: every non-claude_code adapter, unchanged). Raises
    ToolAccessProfileValidationError if an explicit selection is unavailable or
    incompatible; launch paths must run evaluate_tool_access_profile_preflight
    first and reject before ever reaching this call in that case.
    """
    rejection = evaluate_tool_access_profile_preflight(
        runtime=runtime, execution_mode=execution_mode, requested_profile=requested_profile,
    )
    if rejection is not None:
        raise ToolAccessProfileValidationError(str(rejection))
    if requested_profile is not None:
        return _PROFILES[requested_profile]
    default_name = _DEFAULT_PROFILE_BY_RUNTIME_MODE.get((runtime, execution_mode))
    return _PROFILES[default_name] if default_name is not None else None
