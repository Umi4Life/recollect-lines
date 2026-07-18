"""Tool-access-profile model, separate from execution_mode.

``execution_mode`` continues to govern workspace-mutation authority
(``read_only`` vs ``isolated_worktree``). ``tool_access_profile`` is an
orthogonal, explicit dimension governing which runtime tool identifiers a
launch may use. Profiles are named, validated, explicit allowlist/policy
descriptors -- never a boolean, never a broad "all MCP tools" switch.

Omitting ``tool_access_profile`` reproduces today's behavior exactly (see
``resolve_tool_access_profile``'s default table). The
``operator_approved_repository_read`` profile is opt-in only: it requires an
operator-curated finite allowlist of exact external/MCP read tool identifiers
in the broker configuration and never expands defaults on its own.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKSPACE_READ = "workspace.read"
REPOSITORY_REMOTE_READ = "repository.remote.read"

LOCAL_WORKSPACE_READ_ONLY = "local_workspace_read_only"
LOCAL_WORKSPACE_STANDARD = "local_workspace_standard"
OPERATOR_APPROVED_REPOSITORY_READ = "operator_approved_repository_read"

LOCAL_READ_TOOLS = ("Read", "Grep", "Glob")
LOCAL_READ_DISALLOWED = ("Edit", "Write", "NotebookEdit")

PROFILE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
EXTERNAL_MCP_TOOL_ID_PATTERN = re.compile(
    r"^mcp__[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}__[a-zA-Z0-9][a-zA-Z0-9_-]{0,126}$"
)
WILDCARD_TOOL_MARKERS = frozenset({"*", "?", "[", "]"})

STATIC_TOOL_ACCESS_PROFILE_IDS = frozenset({
    LOCAL_WORKSPACE_READ_ONLY,
    LOCAL_WORKSPACE_STANDARD,
    OPERATOR_APPROVED_REPOSITORY_READ,
})

# Backward-compatible alias for CLI/MCP schema surfaces.
KNOWN_TOOL_ACCESS_PROFILE_IDS = STATIC_TOOL_ACCESS_PROFILE_IDS

ALLOWED_CONFIG_ENTRY_KEYS = frozenset({"profile_kind", "allowed_external_tools"})
TOOL_ACCESS_PROFILES_CONFIG_KEY = "tool_access_profiles"


class ToolAccessProfileValidationError(ValueError):
    """Invalid, incompatible, or unavailable tool_access_profile selection."""


class ToolAccessProfileConfigError(ValueError):
    """Invalid operator tool_access_profiles configuration."""


@dataclass(frozen=True)
class ToolAccessProfile:
    name: str
    # None means no --tools allowlist is applied (native default CLI toolset).
    allowed_tools: tuple[str, ...] | None
    disallowed_tools: tuple[str, ...]
    compatible_execution_modes: frozenset[str]
    # Semantic capability ids (required_capabilities.py) this profile can ever advertise.
    semantic_capabilities: frozenset[str]
    # Exact external/MCP tool identifiers granted by operator configuration.
    external_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolAccessProfileRegistry:
    profiles: dict[str, ToolAccessProfile]

    def known_profile_ids(self) -> frozenset[str]:
        return frozenset(self.profiles)

    def configured_repository_read_ids(self) -> frozenset[str]:
        return frozenset(
            name
            for name, profile in self.profiles.items()
            if REPOSITORY_REMOTE_READ in profile.semantic_capabilities
        )


def _local_read_only_profile() -> ToolAccessProfile:
    return ToolAccessProfile(
        name=LOCAL_WORKSPACE_READ_ONLY,
        allowed_tools=LOCAL_READ_TOOLS,
        disallowed_tools=LOCAL_READ_DISALLOWED,
        compatible_execution_modes=frozenset({"read_only"}),
        semantic_capabilities=frozenset({WORKSPACE_READ}),
    )


def _local_standard_profile() -> ToolAccessProfile:
    return ToolAccessProfile(
        name=LOCAL_WORKSPACE_STANDARD,
        allowed_tools=None,
        disallowed_tools=(),
        compatible_execution_modes=frozenset({"isolated_worktree"}),
        semantic_capabilities=frozenset({WORKSPACE_READ}),
    )


def _repository_read_profile(name: str, external_tools: tuple[str, ...]) -> ToolAccessProfile:
    allowed = tuple(
        list(LOCAL_READ_TOOLS)
        + sorted(tool for tool in external_tools if tool not in LOCAL_READ_TOOLS)
    )
    return ToolAccessProfile(
        name=name,
        allowed_tools=allowed,
        disallowed_tools=LOCAL_READ_DISALLOWED,
        compatible_execution_modes=frozenset({"read_only"}),
        semantic_capabilities=frozenset({WORKSPACE_READ, REPOSITORY_REMOTE_READ}),
        external_tools=external_tools,
    )


def _builtin_profiles() -> dict[str, ToolAccessProfile]:
    return {
        LOCAL_WORKSPACE_READ_ONLY: _local_read_only_profile(),
        LOCAL_WORKSPACE_STANDARD: _local_standard_profile(),
    }


def default_tool_access_profile_registry() -> ToolAccessProfileRegistry:
    """Built-in local profiles only -- no operator-approved repository read."""
    return ToolAccessProfileRegistry(_builtin_profiles())


def build_tool_access_profile_registry(
    *, configured: dict[str, ToolAccessProfile],
) -> ToolAccessProfileRegistry:
    profiles = _builtin_profiles()
    profiles.update(configured)
    return ToolAccessProfileRegistry(profiles)


# Runtimes that implement per-tool restriction via a tool-access profile today.
_RUNTIME_PROFILE_AVAILABILITY: dict[str, frozenset[str]] = {
    "claude_code": STATIC_TOOL_ACCESS_PROFILE_IDS,
}

# Deterministic default per (runtime, execution_mode) when tool_access_profile
# is omitted -- this table is what makes omission byte-equivalent to today.
_DEFAULT_PROFILE_BY_RUNTIME_MODE: dict[tuple[str, str], str] = {
    ("claude_code", "read_only"): LOCAL_WORKSPACE_READ_ONLY,
    ("claude_code", "isolated_worktree"): LOCAL_WORKSPACE_STANDARD,
}


def validate_external_tool_identifier(raw: Any, *, index: int | None = None) -> str:
    prefix = f"allowed_external_tools[{index}]" if index is not None else "allowed_external_tools entry"
    if not isinstance(raw, str) or not raw.strip():
        raise ToolAccessProfileConfigError(f"{prefix} must be a non-empty exact tool identifier string")
    tool_id = raw.strip()
    if any(marker in tool_id for marker in WILDCARD_TOOL_MARKERS):
        raise ToolAccessProfileConfigError(
            f"{prefix} {tool_id!r} contains wildcard/prefix characters; only exact identifiers are permitted"
        )
    if not EXTERNAL_MCP_TOOL_ID_PATTERN.match(tool_id):
        raise ToolAccessProfileConfigError(
            f"{prefix} {tool_id!r} must match exact MCP tool id form mcp__<server>__<tool>"
        )
    if tool_id in LOCAL_READ_TOOLS:
        raise ToolAccessProfileConfigError(
            f"{prefix} {tool_id!r} duplicates a built-in local read tool; list external MCP tools only"
        )
    return tool_id


def _normalize_allowed_external_tools(raw: Any, *, profile_name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ToolAccessProfileConfigError(
            f"tool_access_profiles.{profile_name}.allowed_external_tools must be a non-empty array "
            "of exact external MCP tool identifiers"
        )
    seen: set[str] = set()
    normalized: list[str] = []
    for index, item in enumerate(raw):
        tool_id = validate_external_tool_identifier(item, index=index)
        if tool_id in seen:
            raise ToolAccessProfileConfigError(
                f"tool_access_profiles.{profile_name}.allowed_external_tools contains duplicate {tool_id!r}"
            )
        seen.add(tool_id)
        normalized.append(tool_id)
    return tuple(sorted(normalized))


def _parse_configured_profile(name: str, raw: Any) -> ToolAccessProfile:
    if not PROFILE_NAME_PATTERN.match(name):
        raise ToolAccessProfileConfigError(
            f"tool_access_profiles key {name!r} must match {PROFILE_NAME_PATTERN.pattern}"
        )
    if not isinstance(raw, dict):
        raise ToolAccessProfileConfigError(f"tool_access_profiles.{name} must be an object")
    unknown = set(raw) - ALLOWED_CONFIG_ENTRY_KEYS
    if unknown:
        raise ToolAccessProfileConfigError(
            f"tool_access_profiles.{name}: unknown key(s) {', '.join(sorted(unknown))}"
        )
    profile_kind = raw.get("profile_kind", OPERATOR_APPROVED_REPOSITORY_READ)
    if profile_kind != OPERATOR_APPROVED_REPOSITORY_READ:
        raise ToolAccessProfileConfigError(
            f"tool_access_profiles.{name}.profile_kind must be {OPERATOR_APPROVED_REPOSITORY_READ!r}"
        )
    external_tools = _normalize_allowed_external_tools(raw.get("allowed_external_tools"), profile_name=name)
    return _repository_read_profile(name, external_tools)


def parse_tool_access_profiles_document(data: Any) -> dict[str, ToolAccessProfile]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ToolAccessProfileConfigError("tool_access_profiles must be an object when provided")
    profiles: dict[str, ToolAccessProfile] = {}
    for name, entry in data.items():
        if name in profiles:
            raise ToolAccessProfileConfigError(f"Duplicate tool_access_profiles entry: {name!r}")
        profiles[name] = _parse_configured_profile(name, entry)
    return profiles


def load_tool_access_profiles_config(path: Path) -> dict[str, ToolAccessProfile]:
    try:
        raw_text = path.read_text()
    except OSError as error:
        raise ToolAccessProfileConfigError(f"Cannot read operator configuration {path}: {error}") from error
    from .providers import _parse_yaml_document, _sniff_config_format

    fmt = _sniff_config_format(path, raw_text)
    if fmt == "json":
        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as error:
            raise ToolAccessProfileConfigError(
                f"Operator configuration {path} is not valid JSON: {error}"
            ) from error
    else:
        document = _parse_yaml_document(path, raw_text)
        if document is None:
            return {}
    if not isinstance(document, dict):
        raise ToolAccessProfileConfigError(f"Operator configuration {path} must be a top-level object")
    return parse_tool_access_profiles_document(document.get(TOOL_ACCESS_PROFILES_CONFIG_KEY))


def known_tool_access_profile_ids(registry: ToolAccessProfileRegistry | None = None) -> frozenset[str]:
    if registry is None:
        return STATIC_TOOL_ACCESS_PROFILE_IDS
    return STATIC_TOOL_ACCESS_PROFILE_IDS | registry.known_profile_ids()


def normalize_tool_access_profile(
    raw: Any,
    *,
    registry: ToolAccessProfileRegistry | None = None,
) -> str | None:
    """Validate the request-level ``tool_access_profile`` field format."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ToolAccessProfileValidationError(
            "tool_access_profile must be a non-empty string when provided"
        )
    profile_id = raw.strip()
    allowed = known_tool_access_profile_ids(registry)
    if profile_id not in allowed:
        raise ToolAccessProfileValidationError(
            f"Unknown tool_access_profile {profile_id!r}; known ids: {sorted(allowed)}"
        )
    return profile_id


def evaluate_tool_access_profile_preflight(
    *,
    runtime: str,
    execution_mode: str,
    requested_profile: str | None,
    registry: ToolAccessProfileRegistry | None = None,
) -> dict[str, Any] | None:
    """Return machine-readable rejection metadata for an invalid profile selection."""
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
    effective_registry = registry or default_tool_access_profile_registry()
    profile = effective_registry.profiles.get(requested_profile)
    if profile is None:
        return {
            "reason": "unconfigured_tool_access_profile",
            "tool_access_profile": requested_profile,
            "detail": (
                "Profile requires operator configuration with a finite allowed_external_tools "
                "allowlist before launch"
            ),
        }
    if execution_mode not in profile.compatible_execution_modes:
        return {
            "reason": "incompatible_tool_access_profile",
            "tool_access_profile": requested_profile,
            "execution_mode": execution_mode,
            "compatible_execution_modes": sorted(profile.compatible_execution_modes),
        }
    if (
        REPOSITORY_REMOTE_READ in profile.semantic_capabilities
        and not profile.external_tools
    ):
        return {
            "reason": "unconfigured_tool_access_profile",
            "tool_access_profile": requested_profile,
            "detail": "Repository-read profile has no operator-approved external tool allowlist",
        }
    return None


def resolve_tool_access_profile(
    *,
    runtime: str,
    execution_mode: str,
    requested_profile: str | None,
    registry: ToolAccessProfileRegistry | None = None,
) -> ToolAccessProfile | None:
    """Deterministically resolve the effective tool-access profile."""
    rejection = evaluate_tool_access_profile_preflight(
        runtime=runtime,
        execution_mode=execution_mode,
        requested_profile=requested_profile,
        registry=registry,
    )
    if rejection is not None:
        raise ToolAccessProfileValidationError(str(rejection))
    effective_registry = registry or default_tool_access_profile_registry()
    if requested_profile is not None:
        return effective_registry.profiles[requested_profile]
    default_name = _DEFAULT_PROFILE_BY_RUNTIME_MODE.get((runtime, execution_mode))
    return effective_registry.profiles[default_name] if default_name is not None else None


def tool_access_profile_audit_payload(profile: ToolAccessProfile | None) -> dict[str, Any] | None:
    """Privacy-safe audit metadata: profile identity and external tool count only."""
    if profile is None:
        return None
    payload: dict[str, Any] = {
        "tool_access_profile": profile.name,
        "external_tool_count": len(profile.external_tools),
    }
    if REPOSITORY_REMOTE_READ in profile.semantic_capabilities:
        payload["advertises_repository_remote_read"] = True
    return payload
