"""Semantic required-capability declarations and static preflight (RFC-002 PR 2).

Capability IDs are broker-level semantic requirements, distinct from adapter
tool names and from discovery's boolean runtime flags. Preflight is conservative:
a capability is advertised only when the selected runtime plus current
execution/policy configuration can deterministically satisfy it before launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .claude_permission_mode_policy import resolve_claude_permission_mode
from .tool_access_profile import (
    ToolAccessProfileRegistry,
    ToolAccessProfileValidationError,
    default_tool_access_profile_registry,
    resolve_tool_access_profile,
)

WORKSPACE_READ = "workspace.read"
REPOSITORY_REMOTE_READ = "repository.remote.read"

KNOWN_SEMANTIC_CAPABILITY_IDS = frozenset({
    WORKSPACE_READ,
    REPOSITORY_REMOTE_READ,
})


class RequiredCapabilityValidationError(ValueError):
    """Invalid required_capabilities input at delegate/create boundaries."""


@dataclass(frozen=True)
class CapabilityPreflightContext:
    runtime: str
    execution_mode: str
    result_schema: str | None = None
    agent_profile: str | None = None
    task_category: str | None = None
    claude_permission_mode: str | None = None
    tool_access_profile: str | None = None
    tool_access_profile_registry: ToolAccessProfileRegistry | None = None


def normalize_required_capabilities(raw: Any) -> tuple[str, ...]:
    """Validate, deduplicate, and return capability IDs in deterministic order."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise RequiredCapabilityValidationError(
            "required_capabilities must be a non-empty array of capability id strings when provided"
        )
    seen: set[str] = set()
    normalized: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise RequiredCapabilityValidationError(
                f"required_capabilities[{index}] must be a non-empty string"
            )
        capability_id = item.strip()
        if capability_id not in KNOWN_SEMANTIC_CAPABILITY_IDS:
            raise RequiredCapabilityValidationError(
                f"required_capabilities[{index}] unknown capability id {capability_id!r}; "
                f"known ids: {sorted(KNOWN_SEMANTIC_CAPABILITY_IDS)}"
            )
        if capability_id not in seen:
            seen.add(capability_id)
            normalized.append(capability_id)
    return tuple(sorted(normalized))


def _claude_code_advertised_capabilities(ctx: CapabilityPreflightContext) -> frozenset[str]:
    if ctx.execution_mode not in ("read_only", "isolated_worktree"):
        return frozenset()
    try:
        resolve_claude_permission_mode(
            execution_mode=ctx.execution_mode,
            result_schema=ctx.result_schema,
            agent_profile=ctx.agent_profile,
            task_category=ctx.task_category,
            claude_permission_mode=ctx.claude_permission_mode,
        )
    except ValueError:
        return frozenset()
    try:
        profile = resolve_tool_access_profile(
            runtime=ctx.runtime,
            execution_mode=ctx.execution_mode,
            requested_profile=ctx.tool_access_profile,
            registry=ctx.tool_access_profile_registry or default_tool_access_profile_registry(),
        )
    except ToolAccessProfileValidationError:
        return frozenset()
    if profile is None:
        return frozenset()
    return profile.semantic_capabilities


_RUNTIME_ADVERTISERS = {
    "claude_code": _claude_code_advertised_capabilities,
}


def advertised_semantic_capabilities(ctx: CapabilityPreflightContext) -> frozenset[str]:
    advertiser = _RUNTIME_ADVERTISERS.get(ctx.runtime)
    if advertiser is None:
        return frozenset()
    return advertiser(ctx)


def evaluate_capability_preflight(
    required: tuple[str, ...],
    ctx: CapabilityPreflightContext,
) -> dict[str, Any] | None:
    """Return machine-readable rejection metadata when requirements are unsatisfied."""
    if not required:
        return None
    advertised = advertised_semantic_capabilities(ctx)
    missing = tuple(cap for cap in required if cap not in advertised)
    if not missing:
        return None
    return {
        "reason": "missing_required_capabilities",
        "required_capabilities": list(required),
        "missing_capabilities": list(missing),
        "advertised_capabilities": sorted(advertised),
        "runtime": ctx.runtime,
        "execution_mode": ctx.execution_mode,
    }
