"""Advisory claim-versus-capability diagnostics.

Detects when a runtime's final summary plausibly claims an external
verification path (e.g. "verified via", "checked via", "queried") that
structured runtime metadata says was denied. This is a guardrail, not
semantic fact-checking: it never scores claim truth, never runs an
LLM-as-judge, and never mutates lifecycle state, capability/tool/credential
grants, or any result contract. See docs/claim-capability-diagnostics.md for
the trust boundary and recommended parent response.

Inputs are bounded and already structured:
- `capability_observations` -- normalized denial observations (see
  `result_normalization.normalize_permission_denials`); never raw
  `permission_denials`/`tool_input`.
- `summary` -- the broker's already-collected final summary text; never raw
  stdout/stderr/event logs or tool arguments.

The "tool family/provider label" a claim is checked against is never
guessed from a model or provider name: it is read structurally off an
already-denied MCP tool identifier's `mcp__<server>__<tool>` shape (the same
exact-identifier convention `tool_access_profile.py` validates external
tools against). A denial alone, or a cue alone, never produces a diagnostic
-- only a cue found in bounded proximity to that denied tool's family label
does.
"""

from __future__ import annotations

import re
from typing import Any

CLAIM_CAPABILITY_DIAGNOSTIC_CATEGORY = "possible_claim_capability_mismatch"

# Conservative, closed vocabulary of external-verification-claim cues.
# Broker-curated, never user/request configurable -- adding a cue is a
# deliberate code change, not something a task or prompt can influence.
VERIFICATION_CLAIM_CUES: tuple[str, ...] = (
    "verified via",
    "checked via",
    "queried",
)

# Denied tool identifier -> family label extraction is structural only: the
# label is the MCP server segment of an already-validated mcp__<server>__<tool>
# identifier (see tool_access_profile.EXTERNAL_MCP_TOOL_ID_PATTERN). No other
# tool identifier shape ever contributes a family label -- there is no
# fuzzy/model-name/provider-name guessing.
_MCP_TOOL_FAMILY_RE = re.compile(
    r"^mcp__([a-zA-Z0-9][a-zA-Z0-9_-]{0,62})__[a-zA-Z0-9][a-zA-Z0-9_-]{0,126}$"
)

MAX_SCANNED_SUMMARY_CHARS = 4000
MAX_CLAIM_CAPABILITY_DIAGNOSTICS = 16
MAX_DENIED_TOOL_IDENTIFIERS_PER_DIAGNOSTIC = 16
_CUE_FAMILY_PROXIMITY_WINDOW = 60


def _tool_family_label(tool_identifier: str) -> str | None:
    match = _MCP_TOOL_FAMILY_RE.match(tool_identifier)
    return match.group(1).lower() if match else None


def _denied_families(capability_observations: list[dict[str, Any]]) -> dict[str, set[str]]:
    families: dict[str, set[str]] = {}
    for item in capability_observations:
        if not isinstance(item, dict):
            continue
        tool_identifier = item.get("tool_identifier")
        if not isinstance(tool_identifier, str) or not tool_identifier:
            continue
        family = _tool_family_label(tool_identifier)
        if family is None:
            continue
        families.setdefault(family, set()).add(tool_identifier)
    return families


def _cue_family_proximate(text: str, cue: str, family: str) -> bool:
    cue_spans = [m.start() for m in re.finditer(re.escape(cue), text, re.IGNORECASE)]
    if not cue_spans:
        return False
    family_spans = [
        m.start() for m in re.finditer(r"\b" + re.escape(family) + r"\b", text, re.IGNORECASE)
    ]
    if not family_spans:
        return False
    return any(
        abs(cue_index - family_index) <= _CUE_FAMILY_PROXIMITY_WINDOW
        for cue_index in cue_spans
        for family_index in family_spans
    )


def evaluate_claim_capability_diagnostics(
    *,
    summary: Any,
    capability_observations: Any,
) -> dict[str, Any] | None:
    """Deterministic advisory diagnostics, or None when nothing to report.

    Fails soft: a non-string summary or malformed/absent observations simply
    yields None, never an exception. Independent of lifecycle state,
    required-capability contract status, evidence provenance, review status,
    and cost policy -- this function reads none of them and its result is
    never fed back into any of them.
    """
    if not isinstance(summary, str) or not summary.strip():
        return None
    if not isinstance(capability_observations, list) or not capability_observations:
        return None

    families = _denied_families(capability_observations)
    if not families:
        return None

    text = summary[:MAX_SCANNED_SUMMARY_CHARS]
    diagnostics: list[dict[str, Any]] = []
    for cue in VERIFICATION_CLAIM_CUES:
        for family in sorted(families):
            if not _cue_family_proximate(text, cue, family):
                continue
            diagnostics.append({
                "category": CLAIM_CAPABILITY_DIAGNOSTIC_CATEGORY,
                "cue": cue,
                "tool_family": family,
                "denied_tool_identifiers": sorted(families[family])[
                    :MAX_DENIED_TOOL_IDENTIFIERS_PER_DIAGNOSTIC
                ],
            })
            if len(diagnostics) >= MAX_CLAIM_CAPABILITY_DIAGNOSTICS:
                return {"diagnostic_count": len(diagnostics), "diagnostics": diagnostics}

    if not diagnostics:
        return None
    return {"diagnostic_count": len(diagnostics), "diagnostics": diagnostics}


def claim_capability_diagnostics_concise(
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Compact, privacy-safe projection for concise/completion/status surfaces.

    Already bounded (closed-vocabulary cue, structural tool family, existing
    structured tool identifiers) so the full-envelope shape is reused as-is;
    this exists so call sites don't need to know that fact.
    """
    if not isinstance(diagnostics, dict) or not diagnostics.get("diagnostic_count"):
        return None
    return {
        "diagnostic_count": diagnostics.get("diagnostic_count", 0),
        "diagnostics": diagnostics.get("diagnostics", []),
    }
