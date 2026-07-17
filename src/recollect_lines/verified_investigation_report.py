"""Strict ``verified-investigation-report`` result contract (Completion Integrity).

Makes a worker's claims, evidence references, provenance, blocked capabilities,
and uncertainty explicit and schema-validatable. This is not semantic fact-checking:
the broker validates structure, referential integrity, provenance policy, and safe
source handling only.

``blocked_capabilities`` is runtime-reported contract output (what the worker says
it could not use). Broker-normalized ``capability_observations`` from structured
permission denials are a separate metadata dimension and must not be conflated.
"""

from __future__ import annotations

import re
from typing import Any

from .claude_code_adapter import redact_secrets

VERIFIED_INVESTIGATION_REPORT_SCHEMA = "verified-investigation-report"

PROVENANCE_VALUES = frozenset({
    "orchestrator_supplied",
    "runtime_reported",
    "broker_observed",
    "broker_verified",
    "unresolved",
})

# Runtime JSON may only self-label with these; broker_* labels are reserved.
RUNTIME_ALLOWED_PROVENANCE = frozenset({
    "orchestrator_supplied",
    "runtime_reported",
    "unresolved",
})

CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0
MAX_SOURCE_LEN = 512
EVIDENCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Heuristics for rejecting raw dumps disguised as locators (not exhaustive).
_RAW_DUMP_MARKERS = (
    re.compile(r"^\s*\{"),  # JSON object
    re.compile(r"^\s*\["),  # JSON array
    re.compile(r"(?m)^.+\n.+"),  # multi-line
)


def normalize_evidence_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if not candidate or not EVIDENCE_ID_RE.fullmatch(candidate):
        return None
    return candidate


def sanitize_verified_source(value: Any) -> tuple[str | None, str | None]:
    """Return (sanitized_source, error). Never persists secrets or raw dumps."""
    if not isinstance(value, str):
        return None, "source must be a string"
    text = value.strip()
    if not text:
        return None, "source must be non-empty"
    if len(text) > MAX_SOURCE_LEN:
        return None, f"source exceeds {MAX_SOURCE_LEN} characters"
    for pattern in _RAW_DUMP_MARKERS:
        if pattern.search(text):
            return None, "source must be a bounded locator/label, not raw content"
    sanitized = redact_secrets(text)
    if sanitized != text and "***REDACTED***" in sanitized:
        # Accept redacted locators but record that scrubbing occurred.
        return sanitized[:MAX_SOURCE_LEN], None
    return sanitized[:MAX_SOURCE_LEN], None


def validate_verified_investigation_report(structured: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any] | None]:
    """Validate runtime-reported JSON for the strict contract.

    Returns (ok, warnings, normalized_payload). ``normalized_payload`` is the
    broker-sanitized shape stored under ``runtime_reported.verified_investigation``.
    """
    warnings: list[str] = []
    if not isinstance(structured, dict):
        return False, ["verified-investigation-report payload must be an object"], None

    summary = structured.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False, ["summary must be a non-empty string"], None

    for field in ("findings", "evidence", "unverified_claims", "blocked_capabilities"):
        if field not in structured:
            return False, [f"missing required field: {field}"], None
        if not isinstance(structured[field], list):
            return False, [f"{field} must be an array"], None

    findings_in = structured["findings"]
    evidence_in = structured["evidence"]
    unverified_in = structured["unverified_claims"]
    blocked_in = structured["blocked_capabilities"]

    normalized_evidence: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(evidence_in):
        prefix = f"evidence[{index}]"
        if not isinstance(item, dict):
            return False, [f"{prefix} must be an object"], None
        raw_id = item.get("id")
        evidence_id = normalize_evidence_id(raw_id)
        if evidence_id is None:
            return False, [f"{prefix}.id is missing or not a valid stable identifier"], None
        if evidence_id in normalized_evidence:
            return False, [f"duplicate evidence id: {evidence_id!r}"], None

        provenance = item.get("provenance")
        if provenance not in PROVENANCE_VALUES:
            return False, [f"{prefix}.provenance must be one of: {', '.join(sorted(PROVENANCE_VALUES))}"], None
        if provenance not in RUNTIME_ALLOWED_PROVENANCE:
            return False, [
                f"{prefix}.provenance={provenance!r} is reserved for broker-assigned evidence; "
                "runtime output may only use orchestrator_supplied, runtime_reported, or unresolved"
            ], None

        source_type = item.get("source_type")
        if not isinstance(source_type, str) or not source_type.strip():
            return False, [f"{prefix}.source_type must be a non-empty string"], None

        claim_supported = item.get("claim_supported")
        if not isinstance(claim_supported, str) or not claim_supported.strip():
            return False, [f"{prefix}.claim_supported must be a non-empty string"], None

        source, source_error = sanitize_verified_source(item.get("source"))
        if source_error is not None:
            return False, [f"{prefix}.source: {source_error}"], None

        normalized_evidence[evidence_id] = {
            "id": evidence_id,
            "provenance": provenance,
            "source_type": source_type.strip(),
            "source": source,
            "claim_supported": claim_supported.strip(),
        }

    normalized_findings: list[dict[str, Any]] = []
    for index, item in enumerate(findings_in):
        prefix = f"findings[{index}]"
        if not isinstance(item, dict):
            return False, [f"{prefix} must be an object"], None
        claim = item.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            return False, [f"{prefix}.claim must be a non-empty string"], None

        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return False, [f"{prefix}.confidence must be a number"], None
        confidence_value = float(confidence)
        if confidence_value < CONFIDENCE_MIN or confidence_value > CONFIDENCE_MAX:
            return False, [f"{prefix}.confidence must be between {CONFIDENCE_MIN} and {CONFIDENCE_MAX}"], None

        refs_in = item.get("evidence_refs")
        if not isinstance(refs_in, list) or not refs_in:
            return False, [f"{prefix}.evidence_refs must be a non-empty array"], None

        refs: list[str] = []
        for ref_index, ref in enumerate(refs_in):
            evidence_id = normalize_evidence_id(ref)
            if evidence_id is None:
                return False, [f"{prefix}.evidence_refs[{ref_index}] is not a valid evidence id"], None
            if evidence_id not in normalized_evidence:
                return False, [f"{prefix}.evidence_refs references unknown evidence id: {evidence_id!r}"], None
            refs.append(evidence_id)

        normalized_findings.append({
            "claim": claim.strip(),
            "confidence": confidence_value,
            "evidence_refs": refs,
        })

    normalized_unverified: list[str] = []
    for index, item in enumerate(unverified_in):
        if not isinstance(item, str) or not item.strip():
            return False, [f"unverified_claims[{index}] must be a non-empty string"], None
        normalized_unverified.append(item.strip())

    normalized_blocked: list[str] = []
    for index, item in enumerate(blocked_in):
        if not isinstance(item, str) or not item.strip():
            return False, [f"blocked_capabilities[{index}] must be a non-empty string"], None
        normalized_blocked.append(item.strip())

    payload = {
        "findings": normalized_findings,
        "evidence": list(normalized_evidence.values()),
        "unverified_claims": normalized_unverified,
        "blocked_capabilities": normalized_blocked,
    }
    return True, warnings, payload


def provenance_counts(evidence: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in sorted(PROVENANCE_VALUES)}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        provenance = item.get("provenance")
        if provenance in counts:
            counts[provenance] += 1
    return {key: value for key, value in counts.items() if value > 0}


def verified_investigation_summary(
    *,
    contract_status: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compact, count-only projection for concise views and completion events."""
    summary: dict[str, Any] = {
        "contract": VERIFIED_INVESTIGATION_REPORT_SCHEMA,
        "contract_status": contract_status,
    }
    if payload is None:
        summary.update({
            "findings_count": 0,
            "evidence_count": 0,
            "provenance_counts": {},
            "unverified_claims_count": 0,
            "blocked_capabilities_count": 0,
        })
        return summary

    evidence = payload.get("evidence")
    findings = payload.get("findings")
    unverified = payload.get("unverified_claims")
    blocked = payload.get("blocked_capabilities")
    evidence_list = evidence if isinstance(evidence, list) else []
    summary.update({
        "findings_count": len(findings) if isinstance(findings, list) else 0,
        "evidence_count": len(evidence_list),
        "provenance_counts": provenance_counts(evidence_list),
        "unverified_claims_count": len(unverified) if isinstance(unverified, list) else 0,
        "blocked_capabilities_count": len(blocked) if isinstance(blocked, list) else 0,
    })
    return summary
