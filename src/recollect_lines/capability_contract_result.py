"""Post-run required-capability contract.

`required_capabilities.py` is a *static* preflight gate: conservative, and
only ever able to reason about the runtime/policy combination selected
*before launch*. A task that passes preflight can still receive a structured
runtime denial for the concrete tool a declared capability actually depends
on. This module is the post-run counterpart: it turns the normalized
`capability_observations` (see `result_normalization.normalize_permission_denials`)
plus a narrow, explicit, policy-bound capability -> adapter-tool mapping into
a separate, machine-readable verdict per declared capability.

This is a fourth outcome dimension, kept distinct from the other three
already documented in `result_normalization.py` (execution / parse_status /
parser.contract_status) and — critically — distinct from `TaskState` itself:
there is no `unsatisfied_capability` lifecycle state. A task can succeed at
the process level while its capability contract is unsatisfied.

Conservative by construction: only a denial of a capability's *primary* tool
proves that capability itself is unsatisfied. Denials of auxiliary tools
(e.g. Grep/Glob for workspace.read) remain visible only as
capability-warning observations — they never escalate to "this whole
semantic capability failed" on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .required_capabilities import WORKSPACE_READ

STATUS_NO_REQUIREMENTS = "no_requirements"
STATUS_SATISFIED = "satisfied"
STATUS_UNSATISFIED = "unsatisfied"
STATUS_UNKNOWN = "unknown"

CAPABILITY_CONTRACT_STATUS_VALUES = frozenset({
    STATUS_NO_REQUIREMENTS,
    STATUS_SATISFIED,
    STATUS_UNSATISFIED,
    STATUS_UNKNOWN,
})


@dataclass(frozen=True)
class _ToolMapping:
    primary_tools: frozenset[str]
    auxiliary_tools: frozenset[str]


# Explicit, narrow, policy-bound map from a semantic capability to the
# concrete adapter tool identifier(s) that back it. Deliberately conservative
# starting point: only claude_code/workspace.read is mapped. Adding a new
# entry here is the only sanctioned way to teach the evaluator about another
# capability/adapter pair -- it is never inferred from model/provider names
# or scraped from prose.
_ADAPTER_CAPABILITY_TOOL_MAP: dict[str, dict[str, _ToolMapping]] = {
    "claude_code": {
        WORKSPACE_READ: _ToolMapping(
            primary_tools=frozenset({"Read"}),
            auxiliary_tools=frozenset({"Grep", "Glob"}),
        ),
    },
}


def evaluate_capability_contract(
    required: tuple[str, ...],
    *,
    adapter: str | None,
    capability_observations: list[dict[str, Any]],
    denial_metadata_malformed: bool,
) -> dict[str, Any]:
    """Deterministic post-run verdict for each declared required capability.

    `capability_observations` must already be normalized (a list of
    well-formed `{tool_identifier, source, adapter}` dicts) -- this function
    never sees raw `permission_denials`/`tool_input`. `denial_metadata_malformed`
    records whether normalize_permission_denials had to drop malformed
    sibling entries; a capability with no evidence of denial under that
    condition is reported unknown rather than falsely satisfied, since a
    dropped entry could have been the relevant denial.
    """
    if not required:
        return {
            "status": STATUS_NO_REQUIREMENTS,
            "required_capabilities": [],
            "unsatisfied_capabilities": [],
            "unknown_capabilities": [],
            "reasons": [],
        }

    denied_tools = frozenset(
        item["tool_identifier"] for item in capability_observations
        if isinstance(item, dict) and isinstance(item.get("tool_identifier"), str)
    )

    unsatisfied: list[str] = []
    unknown: list[str] = []
    reasons: list[str] = []
    for capability in required:
        mapping = _ADAPTER_CAPABILITY_TOOL_MAP.get(adapter or "", {}).get(capability)
        if mapping is None:
            unknown.append(capability)
            reasons.append(f"{capability}: no adapter tool mapping for adapter {adapter!r}; not determinable")
            continue
        primary_denied = sorted(denied_tools & mapping.primary_tools)
        if primary_denied:
            unsatisfied.append(capability)
            reasons.append(f"{capability}: primary tool(s) denied: {', '.join(primary_denied)}")
            continue
        if denial_metadata_malformed:
            unknown.append(capability)
            reasons.append(f"{capability}: permission-denial metadata was partially malformed; not determinable")
            continue
        # No mapped primary-tool denial and no ambiguity in the evidence: satisfied.

    if unsatisfied:
        status = STATUS_UNSATISFIED
    elif unknown:
        status = STATUS_UNKNOWN
    else:
        status = STATUS_SATISFIED

    return {
        "status": status,
        "required_capabilities": list(required),
        "unsatisfied_capabilities": sorted(set(unsatisfied)),
        "unknown_capabilities": sorted(set(unknown)),
        "reasons": reasons,
    }
