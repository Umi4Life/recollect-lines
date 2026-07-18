"""Opt-in ``review-report`` bounded-review result contract.

Lets a reviewer return a structured, auditable review outcome — status,
bounded findings, which supplied artifacts it looked at, and whether it
performed a full re-execution — without the broker performing semantic fact
checking, dispatching review work, or replaying the worker's task itself.
The broker validates structure, closed vocabulary, bounded lists, and safe
artifact references only.

``full_reexecution_performed`` is runtime-reported contract output (what the
reviewer says it did): the default workflow posture is a bounded review of
supplied artifacts, not a replay of the delegated task, and this field only
records that fact — it is never used to claim or compute a cost saving. See
docs/review-report.md for the non-goal that the broker never infers this
value or schedules reviews itself.
"""

from __future__ import annotations

from typing import Any

from .verified_investigation_report import sanitize_verified_source

REVIEW_REPORT_SCHEMA = "review-report"

REVIEW_STATUS_VALUES = frozenset({"passed", "needs_changes", "blocked"})

FINDING_SEVERITY_VALUES = frozenset({"blocking", "major", "minor", "info"})
DEFAULT_FINDING_SEVERITY = "info"

REVIEWED_ARTIFACT_CATEGORIES = frozenset({
    "diff",
    "test_result",
    "normalized_result",
    "verification_output",
    "task_summary",
    "other",
})

MAX_REVIEW_FINDINGS = 50
MAX_REVIEWED_ARTIFACTS = 50
MAX_FINDING_LEN = 2000


def _bounded_text(value: Any, *, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_len:
        return None
    return text


def validate_review_report(structured: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any] | None]:
    """Validate runtime-reported JSON for the review-report contract.

    Returns (ok, warnings, normalized_payload), the same shape as
    ``validate_verified_investigation_report`` so callers reuse the same
    partial/unsatisfied_malformed failure mechanics.
    """
    if not isinstance(structured, dict):
        return False, ["review-report payload must be an object"], None

    summary = structured.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False, ["summary must be a non-empty string"], None

    review_status = structured.get("review_status")
    if review_status not in REVIEW_STATUS_VALUES:
        return False, [f"review_status must be one of: {', '.join(sorted(REVIEW_STATUS_VALUES))}"], None

    full_reexecution_performed = structured.get("full_reexecution_performed")
    if not isinstance(full_reexecution_performed, bool):
        return False, ["full_reexecution_performed must be a boolean"], None

    findings_in = structured.get("review_findings")
    if not isinstance(findings_in, list):
        return False, ["review_findings must be an array"], None
    if len(findings_in) > MAX_REVIEW_FINDINGS:
        return False, [f"review_findings exceeds {MAX_REVIEW_FINDINGS} items"], None

    normalized_findings: list[dict[str, Any]] = []
    for index, item in enumerate(findings_in):
        prefix = f"review_findings[{index}]"
        if not isinstance(item, dict):
            return False, [f"{prefix} must be an object"], None
        finding_text = _bounded_text(item.get("finding"), max_len=MAX_FINDING_LEN)
        if finding_text is None:
            return False, [
                f"{prefix}.finding must be a non-empty string up to {MAX_FINDING_LEN} characters"
            ], None
        severity = item.get("severity", DEFAULT_FINDING_SEVERITY)
        if severity not in FINDING_SEVERITY_VALUES:
            return False, [
                f"{prefix}.severity must be one of: {', '.join(sorted(FINDING_SEVERITY_VALUES))}"
            ], None
        normalized_findings.append({"finding": finding_text, "severity": severity})

    artifacts_in = structured.get("reviewed_artifacts")
    if not isinstance(artifacts_in, list):
        return False, ["reviewed_artifacts must be an array"], None
    if len(artifacts_in) > MAX_REVIEWED_ARTIFACTS:
        return False, [f"reviewed_artifacts exceeds {MAX_REVIEWED_ARTIFACTS} items"], None

    normalized_artifacts: list[dict[str, Any]] = []
    for index, item in enumerate(artifacts_in):
        prefix = f"reviewed_artifacts[{index}]"
        if not isinstance(item, dict):
            return False, [f"{prefix} must be an object"], None
        category = item.get("category")
        if category not in REVIEWED_ARTIFACT_CATEGORIES:
            return False, [
                f"{prefix}.category must be one of: {', '.join(sorted(REVIEWED_ARTIFACT_CATEGORIES))}"
            ], None
        reference, reference_error = sanitize_verified_source(item.get("reference"))
        if reference_error is not None:
            return False, [f"{prefix}.reference: {reference_error}"], None
        normalized_artifacts.append({"category": category, "reference": reference})

    payload = {
        "review_status": review_status,
        "review_findings": normalized_findings,
        "reviewed_artifacts": normalized_artifacts,
        "full_reexecution_performed": full_reexecution_performed,
    }
    return True, [], payload


def reviewed_artifact_category_counts(artifacts: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if isinstance(category, str):
            counts[category] = counts.get(category, 0) + 1
    return counts


def review_summary(*, contract_status: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Compact, count-only projection for concise views, completion events, and status.

    Never includes finding text, artifact references, or any other raw prose.
    """
    summary: dict[str, Any] = {
        "contract": REVIEW_REPORT_SCHEMA,
        "contract_status": contract_status,
    }
    if payload is None:
        summary.update({
            "review_status": None,
            "finding_count": 0,
            "reviewed_artifact_category_counts": {},
            "full_reexecution_performed": None,
        })
        return summary

    findings = payload.get("review_findings")
    artifacts = payload.get("reviewed_artifacts")
    findings_list = findings if isinstance(findings, list) else []
    artifacts_list = artifacts if isinstance(artifacts, list) else []
    summary.update({
        "review_status": payload.get("review_status"),
        "finding_count": len(findings_list),
        "reviewed_artifact_category_counts": reviewed_artifact_category_counts(artifacts_list),
        "full_reexecution_performed": payload.get("full_reexecution_performed"),
    })
    return summary
