"""Provenance-aware structured result normalization (MR 8.6, extended Wave 4 / PR 11).

Parses runtime-reported output into a versioned envelope with explicit trust
zones. Unknown result_schema values are rejected before launch; they are never
silently reinterpreted.

Three outcome dimensions are kept deliberately distinct and never collapsed
into one another (see the Wave 0 dogfood incident this module exists to make
un-repeatable: a `claude -p` run exited 0 with a clean meta-response asking
which output format to use, and the only signal available was a buried
`parse_status: fallback` next to a top-level task success):

- Execution (`TaskState` / `broker_observed.exit_code`): did the child
  process/runtime actually run and exit successfully? Purely a function of
  the runtime's exit code and broker-observed process lifecycle. Never
  downgraded because parsing or contract satisfaction failed.
- Parsing (`parser.parse_status`): could the broker extract a summary, and
  (if structured JSON was expected) did it parse? One of "ok", "partial",
  "fallback", "failed" — unchanged, existing semantics.
- Contract (`parser.contract_status`): did the *requested* result_schema
  contract actually get satisfied? A deterministic function of
  (requested schema, parse_status, execution outcome) — see
  `CONTRACT_STATUS_VALUES` below. This is the field a parent should check
  before trusting `runtime_reported.findings` etc.; `state: succeeded` alone
  never implies it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .capability_contract_result import STATUS_NO_REQUIREMENTS, evaluate_capability_contract
from .models import TaskRecord, TaskState
from .verified_investigation_report import (
    VERIFIED_INVESTIGATION_REPORT_SCHEMA,
    validate_verified_investigation_report,
    verified_investigation_summary,
)

NORMALIZED_RESULT_ARTIFACT = "normalized_result.json"
RAW_OUTPUT_ARTIFACT = "runtime_raw_output.txt"
ENVELOPE_VERSION = 1
CAPABILITY_ENVELOPE_VERSION = 2
CAPABILITY_CONTRACT_ENVELOPE_VERSION = 3

CAPABILITY_OBSERVATION_SOURCE = "runtime_permission_denial"
DISPLAYED_DENIED_TOOLS_CAP = 16

# Adapter-specific structured-denial field carrying the exact tool identifier.
_ADAPTER_DENIAL_TOOL_FIELDS: dict[str, str] = {
    "claude_code": "tool_name",
}

SUPPORTED_RESULT_SCHEMAS = frozenset({
    "plain-summary",
    "evidence-report",
    "review-findings",
    "implementation-report",
    VERIFIED_INVESTIGATION_REPORT_SCHEMA,
})
DEFAULT_RESULT_SCHEMA = "plain-summary"

SCHEMA_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "plain-summary": frozenset({"summary"}),
    "evidence-report": frozenset({"summary"}),
    "review-findings": frozenset({"summary", "findings"}),
    "implementation-report": frozenset({"summary"}),
    VERIFIED_INVESTIGATION_REPORT_SCHEMA: frozenset({
        "summary",
        "findings",
        "evidence",
        "unverified_claims",
        "blocked_capabilities",
    }),
}

# Stable, documented values for the third outcome dimension (contract
# satisfaction). Backward compatible: this is an additive field alongside the
# pre-existing `state` and `parse_status`, never a replacement for either.
#   "not_requested"          — effective schema is plain-summary (no
#                               structured contract was asked for); there is
#                               nothing to satisfy.
#   "satisfied"               — a structured schema was requested and the
#                               runtime's output fully satisfied it.
#   "unsatisfied_fallback"     — a structured schema was requested but the
#                               runtime returned plain prose instead (no JSON
#                               payload at all) — the exact dogfood incident.
#   "unsatisfied_malformed"    — a structured schema was requested and some
#                               JSON/summary was returned, but it was
#                               malformed or missing required fields.
#   "unavailable"              — the child process/runtime did not reach a
#                               successful terminal state, so there is no
#                               result to evaluate against any contract.
CONTRACT_STATUS_VALUES = frozenset({
    "not_requested",
    "satisfied",
    "unsatisfied_fallback",
    "unsatisfied_malformed",
    "unavailable",
})


class UnknownResultSchemaError(ValueError):
    def __init__(self, schema: str):
        self.schema = schema
        super().__init__(
            f"Unknown result_schema {schema!r}: supported schemas are {sorted(SUPPORTED_RESULT_SCHEMAS)}"
        )


def effective_result_schema(record: TaskRecord) -> str:
    return record.result_schema or DEFAULT_RESULT_SCHEMA


def validate_result_schema(schema: str | None) -> None:
    if schema is not None and schema not in SUPPORTED_RESULT_SCHEMAS:
        raise UnknownResultSchemaError(schema)


def _artifact_refs(
    manifest: dict[str, Any],
    *,
    exclude: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    skip = exclude or frozenset()
    return [
        {"name": item["name"], "sha256": item["sha256"], "bytes": item["bytes"]}
        for item in manifest.get("files", [])
        if isinstance(item, dict) and "name" in item and item["name"] not in skip
    ]


def _try_parse_structured_text(text: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return None, warnings
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None, warnings
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        warnings.append(f"summary looked like JSON but failed to parse: {error.msg}")
        return None, warnings
    if not isinstance(parsed, dict):
        warnings.append("structured summary JSON must be an object")
        return None, warnings
    return parsed, warnings


def _runtime_reported_from_structured(
    structured: dict[str, Any] | None,
    *,
    schema: str,
    summary: str | None,
    collected: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": summary,
        "findings": [],
        "claimed_evidence": [],
        "claimed_commands": [],
        "claimed_tests": [],
        "unresolved_questions": [],
    }
    session_metadata: dict[str, Any] = {}
    for key in ("session_id", "thread_id", "num_turns", "usage", "permission_denials"):
        if collected.get(key) is not None:
            session_metadata[key] = collected[key]
    if session_metadata:
        payload["session_metadata"] = session_metadata
    if structured is None:
        return payload

    if isinstance(structured.get("summary"), str) and structured["summary"].strip():
        payload["summary"] = structured["summary"].strip()

    if schema == VERIFIED_INVESTIGATION_REPORT_SCHEMA:
        return payload

    for key, target in (
        ("findings", "findings"),
        ("claimed_evidence", "claimed_evidence"),
        ("evidence", "claimed_evidence"),
        ("unresolved_questions", "unresolved_questions"),
    ):
        value = structured.get(key)
        if isinstance(value, list):
            payload[target] = value
    commands = structured.get("commands_executed")
    if commands is None:
        commands = structured.get("claimed_commands")
    if isinstance(commands, list):
        payload["claimed_commands"] = commands
    tests = structured.get("tests_reported")
    if tests is None:
        tests = structured.get("claimed_tests")
    if isinstance(tests, list):
        payload["claimed_tests"] = tests
    return payload


def _parse_status_and_warnings(
    schema: str,
    runtime_reported: dict[str, Any],
    *,
    structured: dict[str, Any] | None,
    parse_warnings: list[str],
    collected: dict[str, Any],
) -> tuple[str, list[str]]:
    warnings = list(parse_warnings)
    malformed = collected.get("malformed_output_lines", 0) or collected.get("malformed_event_lines", 0)
    if malformed:
        warnings.append(f"runtime emitted {malformed} malformed structured-output line(s)")
    summary = runtime_reported.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        if schema == "plain-summary":
            return "partial", warnings + ["no runtime summary text available"]
        return "failed", warnings + ["required summary missing"]

    if structured is None and schema != "plain-summary":
        warnings.append(f"no structured JSON payload for schema {schema!r}; using plain summary fallback")
        return "fallback", warnings

    required = SCHEMA_REQUIRED_FIELDS[schema]
    missing = [field for field in required if not _field_present(field, runtime_reported, structured, schema)]
    if missing:
        warnings.append(f"missing required field(s) for {schema}: {', '.join(sorted(missing))}")
        if schema == "plain-summary":
            return "partial", warnings
        return "partial" if structured is not None else "fallback", warnings

    if schema == VERIFIED_INVESTIGATION_REPORT_SCHEMA:
        assert structured is not None
        ok, contract_warnings, normalized = validate_verified_investigation_report(structured)
        warnings.extend(contract_warnings)
        if not ok or normalized is None:
            if not any("verified-investigation-report" in w for w in warnings):
                warnings.append("verified-investigation-report contract validation failed")
            return "partial", warnings
        runtime_reported["verified_investigation"] = normalized
        return "ok", warnings

    return "ok", warnings


def _contract_status(schema: str, parse_status: str, final_state: TaskState) -> str:
    """Deterministic third outcome dimension — see CONTRACT_STATUS_VALUES.

    Never invents information: it is purely a function of values already
    computed for `state` and `parse_status`, so it can never disagree with
    them or require its own heuristics.
    """
    if final_state not in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS):
        return "unavailable"
    if schema == DEFAULT_RESULT_SCHEMA:
        return "not_requested"
    if parse_status == "ok":
        return "satisfied"
    if parse_status == "fallback":
        return "unsatisfied_fallback"
    return "unsatisfied_malformed"


def _denied_tool_identifier(entry: Any, *, adapter: str) -> str | None:
    field = _ADAPTER_DENIAL_TOOL_FIELDS.get(adapter)
    if field is None or not isinstance(entry, dict):
        return None
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def normalize_permission_denials(
    permission_denials: Any,
    *,
    adapter: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    """Map structured runtime permission denials to capability observations.

    Returns (observations, compact_capability_warning, parser_warnings).
    Malformed metadata fails soft: valid sibling entries are preserved.
    """
    warnings: list[str] = []
    if permission_denials is None:
        return [], None, warnings
    if not isinstance(permission_denials, list):
        warnings.append(
            "permission_denials was not a list; partial capability observations preserved"
        )
        return [], None, warnings

    observations: list[dict[str, Any]] = []
    malformed = 0
    for entry in permission_denials:
        tool_identifier = _denied_tool_identifier(entry, adapter=adapter)
        if tool_identifier is None:
            malformed += 1
            continue
        observations.append({
            "tool_identifier": tool_identifier,
            "source": CAPABILITY_OBSERVATION_SOURCE,
            "adapter": adapter,
        })

    if malformed:
        entry_word = "entry" if malformed == 1 else "entries"
        warnings.append(
            f"permission_denials contained {malformed} malformed {entry_word}; "
            "partial capability observations preserved"
        )

    if not observations:
        return [], None, warnings

    identifiers = [item["tool_identifier"] for item in observations]
    distinct = sorted(set(identifiers))
    truncated = len(distinct) > DISPLAYED_DENIED_TOOLS_CAP
    capability_warning = {
        "denial_attempt_count": len(identifiers),
        "distinct_denied_tool_count": len(distinct),
        "denied_tool_identifiers": distinct[:DISPLAYED_DENIED_TOOLS_CAP],
        "truncated": truncated,
    }
    return observations, capability_warning, warnings


def _field_present(
    field: str,
    runtime_reported: dict[str, Any],
    structured: dict[str, Any] | None,
    schema: str,
) -> bool:
    if field == "summary":
        value = runtime_reported.get("summary")
        return isinstance(value, str) and bool(value.strip())
    if field == "findings":
        if schema == VERIFIED_INVESTIGATION_REPORT_SCHEMA:
            return isinstance(structured, dict) and isinstance(structured.get("findings"), list)
        findings = runtime_reported.get("findings")
        return isinstance(findings, list) and len(findings) > 0
    if schema == VERIFIED_INVESTIGATION_REPORT_SCHEMA and structured is not None:
        value = structured.get(field)
        return isinstance(value, list)
    return False


def resolve_raw_output_artifact(
    artifacts_dir: Path,
    launch: dict[str, Any] | None,
    collected: dict[str, Any],
) -> str | None:
    if launch is not None:
        for key in ("events_artifact", "stderr_artifact"):
            name = launch.get(key)
            if isinstance(name, str) and (artifacts_dir / name).is_file():
                return name
    for key in ("events_artifact", "stderr_artifact"):
        name = collected.get(key)
        if isinstance(name, str) and (artifacts_dir / name).is_file():
            return name
    if (artifacts_dir / RAW_OUTPUT_ARTIFACT).is_file():
        return RAW_OUTPUT_ARTIFACT
    return None


def read_raw_runtime_output(
    artifacts_dir: Path,
    launch: dict[str, Any] | None,
    collected: dict[str, Any],
) -> str:
    ref = resolve_raw_output_artifact(artifacts_dir, launch, collected)
    if ref is None:
        summary = collected.get("summary")
        return summary if isinstance(summary, str) else ""
    return (artifacts_dir / ref).read_text(errors="replace")


def persist_raw_runtime_output_if_needed(
    store: Any,
    task_id: str,
    *,
    launch: dict[str, Any] | None,
    collected: dict[str, Any],
) -> str | None:
    artifacts_dir = store.artifacts / task_id
    existing = resolve_raw_output_artifact(artifacts_dir, launch, collected)
    if existing is not None:
        return existing
    summary = collected.get("summary")
    if not isinstance(summary, str) or not summary:
        return None
    store.write_artifact(task_id, RAW_OUTPUT_ARTIFACT, summary if summary.endswith("\n") else summary + "\n")
    return RAW_OUTPUT_ARTIFACT


def build_normalized_envelope(
    *,
    record: TaskRecord,
    result: dict[str, Any],
    collected: dict[str, Any],
    gate: dict[str, Any],
    verification: dict[str, Any] | None,
    manifest: dict[str, Any],
    launch: dict[str, Any] | None,
    raw_output_artifact: str | None,
    final_state: TaskState,
    required_capabilities: tuple[str, ...] = (),
    tool_access_profile_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = effective_result_schema(record)
    summary = collected.get("summary")
    if summary is None and isinstance(result.get("summary"), str):
        summary = result["summary"]
    structured, parse_warnings = _try_parse_structured_text(summary if isinstance(summary, str) else None)
    runtime_reported = _runtime_reported_from_structured(
        structured,
        schema=schema,
        summary=summary,
        collected=collected,
    )
    parse_status, warnings = _parse_status_and_warnings(
        schema,
        runtime_reported,
        structured=structured,
        parse_warnings=parse_warnings,
        collected=collected,
    )

    adapter = collected.get("adapter")
    capability_warning = None
    capability_contract = None
    if isinstance(adapter, str) and adapter:
        observations, capability_warning, denial_warnings = normalize_permission_denials(
            collected.get("permission_denials"),
            adapter=adapter,
        )
        if observations:
            runtime_reported["capability_observations"] = observations
        warnings.extend(denial_warnings)
        if required_capabilities:
            capability_contract = evaluate_capability_contract(
                required_capabilities,
                adapter=adapter,
                capability_observations=observations,
                denial_metadata_malformed=bool(denial_warnings),
            )
    elif required_capabilities:
        capability_contract = evaluate_capability_contract(
            required_capabilities,
            adapter=None,
            capability_observations=[],
            denial_metadata_malformed=False,
        )

    envelope_version = ENVELOPE_VERSION
    if capability_warning is not None:
        envelope_version = CAPABILITY_ENVELOPE_VERSION
    if capability_contract is not None:
        envelope_version = CAPABILITY_CONTRACT_ENVELOPE_VERSION

    broker_verification = None
    if verification is not None:
        broker_verification = {
            "broker_verified": True,
            "scope": verification.get("scope"),
            "commands": verification.get("commands"),
        }

    envelope: dict[str, Any] = {
        "envelope_version": envelope_version,
        "task_id": record.id,
        "state": final_state.value,
        "runtime_reported": runtime_reported,
        "broker_observed": {
            "terminal_state": final_state.value,
            "exit_code": collected.get("exit_code"),
            "process_exit_code": collected.get("process_exit_code"),
            "artifact_manifest_ref": "manifest.json",
            "artifact_refs": _artifact_refs(manifest, exclude=frozenset({NORMALIZED_RESULT_ARTIFACT})),
            "verification": broker_verification,
            "verification_gate": {
                "policy": gate.get("policy"),
                "outcome": gate.get("outcome"),
            },
        },
        "parser": {
            "requested_schema": schema,
            "parse_status": parse_status,
            "contract_status": _contract_status(schema, parse_status, final_state),
            "warnings": warnings,
            "raw_output_artifact": raw_output_artifact,
            "malformed_output_lines": collected.get("malformed_output_lines", 0)
            or collected.get("malformed_event_lines", 0),
        },
    }
    if capability_contract is not None:
        envelope["capability_contract"] = capability_contract
    if tool_access_profile_audit is not None:
        envelope["broker_observed"]["tool_access_profile_audit"] = tool_access_profile_audit
    return envelope


def concise_normalized_view(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    if envelope is None:
        return None
    runtime = envelope.get("runtime_reported") or {}
    parser = envelope.get("parser") or {}
    broker = envelope.get("broker_observed") or {}
    summary = runtime.get("summary")
    view: dict[str, Any] = {
        "envelope_version": envelope.get("envelope_version"),
        "state": envelope.get("state"),
        "requested_schema": parser.get("requested_schema"),
        "parse_status": parser.get("parse_status"),
        "contract_status": parser.get("contract_status"),
        "summary": summary if isinstance(summary, str) else None,
        "warnings": parser.get("warnings") or [],
        "raw_output_artifact": parser.get("raw_output_artifact"),
        "artifact_manifest_ref": broker.get("artifact_manifest_ref"),
    }
    if parser.get("warnings"):
        view["has_parser_warnings"] = True
    verification = broker.get("verification")
    if verification is not None:
        view["broker_verification_present"] = True
    observations = runtime.get("capability_observations")
    if isinstance(observations, list) and observations:
        capability_warning = _capability_warning_from_observations(observations)
        if capability_warning is not None:
            view["has_capability_warning"] = True
            view["capability_warning"] = capability_warning
    contract = envelope.get("capability_contract")
    if isinstance(contract, dict) and contract.get("status") != STATUS_NO_REQUIREMENTS:
        view["has_capability_contract"] = True
        view["capability_contract"] = {
            "status": contract.get("status"),
            "required_capabilities": contract.get("required_capabilities", []),
            "unsatisfied_capabilities": contract.get("unsatisfied_capabilities", []),
            "unknown_capabilities": contract.get("unknown_capabilities", []),
            "reasons": contract.get("reasons", []),
        }
    audit = broker.get("tool_access_profile_audit")
    if isinstance(audit, dict) and audit:
        view["tool_access_profile_audit"] = {
            "tool_access_profile": audit.get("tool_access_profile"),
            "external_tool_count": audit.get("external_tool_count"),
        }
        if audit.get("advertises_repository_remote_read"):
            view["tool_access_profile_audit"]["advertises_repository_remote_read"] = True
    verified = runtime.get("verified_investigation")
    if parser.get("requested_schema") == VERIFIED_INVESTIGATION_REPORT_SCHEMA:
        view["verified_investigation_summary"] = verified_investigation_summary(
            contract_status=str(parser.get("contract_status") or "unavailable"),
            payload=verified if isinstance(verified, dict) else None,
        )
    return view


def _capability_warning_from_observations(
    observations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    identifiers = [
        item["tool_identifier"]
        for item in observations
        if isinstance(item, dict)
        and isinstance(item.get("tool_identifier"), str)
        and item["tool_identifier"]
    ]
    if not identifiers:
        return None
    distinct = sorted(set(identifiers))
    truncated = len(distinct) > DISPLAYED_DENIED_TOOLS_CAP
    return {
        "denial_attempt_count": len(identifiers),
        "distinct_denied_tool_count": len(distinct),
        "denied_tool_identifiers": distinct[:DISPLAYED_DENIED_TOOLS_CAP],
        "truncated": truncated,
    }
