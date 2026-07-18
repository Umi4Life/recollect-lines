"""Versioned, provider-neutral result-schema output contracts for launch prompts.

Contracts are prompt-level guidance only. They do not enable provider-native
structured output APIs; the broker still parses runtime-reported text heuristically.
"""

from __future__ import annotations

from .agent_profiles import compose_task_prompt
from .result_normalization import DEFAULT_RESULT_SCHEMA, UnknownResultSchemaError, validate_result_schema

RESULT_SCHEMA_PROMPT_VERSION = 1
_CONTRACT_MARKER = f"recollect-lines result-schema contract v{RESULT_SCHEMA_PROMPT_VERSION}"

_SCHEMA_FIELD_GUIDANCE: dict[str, str] = {
    "evidence-report": (
        "Required JSON fields:\n"
        "- summary (string): concise investigation outcome.\n"
        "Optional JSON fields:\n"
        "- findings (array of objects): structured observations.\n"
        "- claimed_evidence (array of strings) or evidence (array): cited paths or artifacts.\n"
        "- commands_executed (array) or claimed_commands (array): commands you report running.\n"
        "- unresolved_questions (array of strings): open questions."
    ),
    "review-findings": (
        "Required JSON fields:\n"
        "- summary (string): concise review outcome.\n"
        "- findings (array of objects): each item should describe one review finding."
    ),
    "implementation-report": (
        "Required JSON fields:\n"
        "- summary (string): concise change outcome.\n"
        "Optional JSON fields:\n"
        "- commands_executed (array) or claimed_commands (array): commands you report running.\n"
        "- tests_reported (array of objects) or claimed_tests (array): tests you report running."
    ),
    "verified-investigation-report": (
        "Required JSON fields:\n"
        "- summary (string): concise investigation outcome.\n"
        "- findings (array): each object requires claim (string), confidence (number 0.0-1.0), "
        "and evidence_refs (non-empty array of evidence ids).\n"
        "- evidence (array): each object requires id (stable lowercase identifier), provenance "
        "(orchestrator_supplied | runtime_reported | unresolved only — never broker_observed or "
        "broker_verified), source_type (string), source (bounded locator/label, not raw content), "
        "and claim_supported (string).\n"
        "- unverified_claims (array of strings): claims you could not support.\n"
        "- blocked_capabilities (array of strings): runtime-reported tools/capabilities you could "
        "not use during the investigation (distinct from broker capability observations)."
    ),
    "review-report": (
        "Required JSON fields:\n"
        "- summary (string): concise review outcome.\n"
        "- review_status (string): one of passed | needs_changes | blocked.\n"
        "- review_findings (array): each object requires finding (string) and may include "
        "severity (blocking | major | minor | info; defaults to info).\n"
        "- reviewed_artifacts (array): each object requires category (diff | test_result | "
        "normalized_result | verification_output | task_summary | other) and reference (bounded "
        "locator/label, not raw content).\n"
        "- full_reexecution_performed (boolean): true only if you replayed the full delegated "
        "task rather than reviewing the supplied artifacts. Default review posture is bounded "
        "review of supplied artifacts, not full re-execution."
    ),
}


def result_schema_prompt(schema: str) -> str:
    """Return prompt-level output contract text for *schema*, or '' for plain-summary."""
    validate_result_schema(schema)
    if schema == DEFAULT_RESULT_SCHEMA:
        return ""
    guidance = _SCHEMA_FIELD_GUIDANCE.get(schema)
    if guidance is None:
        raise UnknownResultSchemaError(schema)
    return (
        f"[{_CONTRACT_MARKER}: {schema}]\n"
        "Your final response must be exactly one JSON object and nothing else.\n"
        "Do not wrap the JSON in Markdown code fences or add prose before or after it.\n"
        f"{guidance}"
    )


def compose_launch_prompt(
    *,
    prompt_prefix: str | None,
    task_text: str,
    result_schema: str,
    materialization_notice: str | None = None,
) -> tuple[str, str | None]:
    """Join profile prefix, task text, optional runtime honesty notice, and optional schema contract."""
    base = compose_task_prompt(prompt_prefix or "", task_text)
    if materialization_notice:
        base = f"{base}\n\n{materialization_notice}" if base else materialization_notice
    contract = result_schema_prompt(result_schema)
    if not contract:
        return base, None
    return f"{base}\n\n{contract}", contract
