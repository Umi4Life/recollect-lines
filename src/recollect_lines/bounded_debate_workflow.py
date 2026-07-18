"""Reference bounded debate workflow.

Parent-directed helper for the dogfood pattern:

    opening positions → rebuttals → synthesis → validation → optional materialization

This is **not** a workflow engine, daemon, or auto-debate loop. Phases and
participants are explicit; the caller invokes ``run_bounded_debate_workflow``
once and remains responsible for retries, round counts, and when a comparison
is "enough". Completion observation uses the durable ``completion_events``
cursor — never a fixed sleep for task duration.

Lineage: every dispatched task shares the caller's ``external_root_id``; child
tasks hang under a host anchor with ``relationship=delegates`` (rebuttals may
use ``continues`` when ``responds_to`` names a prior participant). Terminal
outputs are collected before the next phase advances.

Runtime capability contracts are respected: ``openai_compatible`` synthesis
returns parent-owned text only; optional materialization is explicit,
workspace-bounded, and never attributes file authorship to a provider.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .capability_contract import MaterializationOwner
from .models import TaskRequest, TaskState, TERMINAL_STATES
from .runtime_registry import DEFAULT_RUNTIME_REGISTRY, ExecutionStrategy

PHASE_OPENING = "opening_positions"
PHASE_REBUTTAL = "rebuttals"
PHASE_SYNTHESIS = "synthesis"
PHASE_VALIDATION = "validation"
PHASE_MATERIALIZATION = "materialization"

VALID_PHASES = (
    PHASE_OPENING,
    PHASE_REBUTTAL,
    PHASE_SYNTHESIS,
    PHASE_VALIDATION,
    PHASE_MATERIALIZATION,
)

SUCCESS_CONTRACT_STATUSES = frozenset({"satisfied", "not_requested"})
TERMINAL_STATE_VALUES = {state.value for state in TERMINAL_STATES}


class BoundedDebateValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DebateParticipant:
    id: str
    profile: str
    task: str
    provider: str | None = None
    responds_to: str | None = None
    relationship: str | None = None
    result_schema: str | None = None


@dataclass(frozen=True)
class MaterializationOptions:
    enabled: bool
    relative_path: str
    dry_run: bool = False


@dataclass(frozen=True)
class BoundedDebatePlan:
    workflow_id: str
    workspace: str
    external_root_id: str
    execution_mode: str
    acceptance_criteria: str
    opening_positions: tuple[DebateParticipant, ...]
    rebuttals: tuple[DebateParticipant, ...]
    synthesis: DebateParticipant
    materialization: MaterializationOptions
    poll_timeout_seconds: float
    anchor_task: str


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoundedDebateValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _parse_participant(raw: Any, *, field: str) -> DebateParticipant:
    if not isinstance(raw, dict):
        raise BoundedDebateValidationError(f"{field} entries must be objects")
    participant_id = _require_non_empty_str(raw.get("id"), f"{field}.id")
    profile = _require_non_empty_str(raw.get("profile"), f"{field}.profile")
    task = _require_non_empty_str(raw.get("task"), f"{field}.task")
    provider = raw.get("provider")
    if provider is not None:
        provider = _require_non_empty_str(provider, f"{field}.provider")
    responds_to = raw.get("responds_to")
    if responds_to is not None:
        responds_to = _require_non_empty_str(responds_to, f"{field}.responds_to")
    relationship = raw.get("relationship")
    if relationship is not None:
        relationship = _require_non_empty_str(relationship, f"{field}.relationship")
        if relationship not in {"delegates", "continues"}:
            raise BoundedDebateValidationError(f"{field}.relationship must be 'delegates' or 'continues'")
    result_schema = raw.get("result_schema")
    if result_schema is not None:
        result_schema = _require_non_empty_str(result_schema, f"{field}.result_schema")
    if not DEFAULT_RUNTIME_REGISTRY.contains(profile):
        raise BoundedDebateValidationError(f"{field}.profile must be a registered runtime, got {profile!r}")
    descriptor = DEFAULT_RUNTIME_REGISTRY.get(profile)
    if descriptor.requires_named_provider and not provider:
        raise BoundedDebateValidationError(f"{field}.provider is required when profile is {profile!r}")
    if not descriptor.requires_named_provider and provider is not None:
        raise BoundedDebateValidationError(f"{field}.provider is only valid with openai_compatible profile")
    return DebateParticipant(
        participant_id, profile, task, provider, responds_to, relationship, result_schema,
    )


def parse_bounded_debate_plan(raw: dict[str, Any]) -> BoundedDebatePlan:
    if not isinstance(raw, dict):
        raise BoundedDebateValidationError("plan must be an object")
    workspace = _require_non_empty_str(raw.get("workspace"), "workspace")
    external_root_id = _require_non_empty_str(raw.get("external_root_id"), "external_root_id")
    execution_mode = raw.get("execution_mode", "read_only")
    if execution_mode not in ("read_only", "isolated_worktree"):
        raise BoundedDebateValidationError("execution_mode must be read_only or isolated_worktree")
    acceptance_criteria = _require_non_empty_str(raw.get("acceptance_criteria"), "acceptance_criteria")
    openings_raw = raw.get("opening_positions")
    if not isinstance(openings_raw, list) or not openings_raw:
        raise BoundedDebateValidationError("opening_positions must be a non-empty array")
    rebuttals_raw = raw.get("rebuttals", [])
    if not isinstance(rebuttals_raw, list):
        raise BoundedDebateValidationError("rebuttals must be an array")
    synthesis_raw = raw.get("synthesis")
    if not isinstance(synthesis_raw, dict):
        raise BoundedDebateValidationError("synthesis must be an object")
    openings = tuple(_parse_participant(item, field="opening_positions") for item in openings_raw)
    rebuttals = tuple(_parse_participant(item, field="rebuttals") for item in rebuttals_raw)
    synthesis = _parse_participant(synthesis_raw, field="synthesis")
    opening_ids = {participant.id for participant in openings}
    if len(opening_ids) != len(openings):
        raise BoundedDebateValidationError("opening_positions ids must be unique")
    rebuttal_ids = {participant.id for participant in rebuttals}
    if len(rebuttal_ids) != len(rebuttals):
        raise BoundedDebateValidationError("rebuttals ids must be unique")
    all_ids = opening_ids | rebuttal_ids | {synthesis.id}
    if len(all_ids) != len(openings) + len(rebuttals) + 1:
        raise BoundedDebateValidationError("participant ids must be unique across opening_positions, rebuttals, and synthesis")
    for participant in rebuttals:
        if participant.responds_to and participant.responds_to not in opening_ids:
            raise BoundedDebateValidationError(
                f"rebuttal {participant.id!r} responds_to unknown opening {participant.responds_to!r}"
            )
    materialization_raw = raw.get("materialization") or {}
    if not isinstance(materialization_raw, dict):
        raise BoundedDebateValidationError("materialization must be an object")
    enabled = bool(materialization_raw.get("enabled", False))
    relative_path = materialization_raw.get("relative_path", "")
    if enabled:
        relative_path = _require_non_empty_str(relative_path, "materialization.relative_path")
    elif relative_path:
        relative_path = str(relative_path).strip()
    dry_run = bool(materialization_raw.get("dry_run", False))
    if enabled and relative_path:
        if relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise BoundedDebateValidationError("materialization.relative_path must be a safe relative path")
    bounds = raw.get("bounds") or {}
    poll_timeout = bounds.get("poll_timeout_seconds", 30.0)
    if not isinstance(poll_timeout, (int, float)) or isinstance(poll_timeout, bool) or poll_timeout <= 0:
        raise BoundedDebateValidationError("bounds.poll_timeout_seconds must be a positive number")
    anchor_task = _require_non_empty_str(
        raw.get("anchor_task", "bounded debate host anchor"),
        "anchor_task",
    )
    return BoundedDebatePlan(
        workflow_id=raw.get("workflow_id") or f"bdw_{uuid4().hex}",
        workspace=workspace,
        external_root_id=external_root_id,
        execution_mode=execution_mode,
        acceptance_criteria=acceptance_criteria,
        opening_positions=openings,
        rebuttals=rebuttals,
        synthesis=synthesis,
        materialization=MaterializationOptions(enabled=enabled, relative_path=relative_path, dry_run=dry_run),
        poll_timeout_seconds=float(poll_timeout),
        anchor_task=anchor_task,
    )


def wait_for_task_completions(
    broker: object,
    *,
    task_ids: set[str],
    after_event_id: int,
    timeout_seconds: float,
    root_task_id: str | None = None,
    poll_interval_seconds: float = 0.02,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Poll ``completion_events_since`` until every task id appears or timeout.

    Returns a map of task_id → compact completion event and the advanced cursor.
    The poll loop sleeps only between cursor checks — never for a guessed task
    duration.
    """
    cursor = after_event_id
    seen: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page = broker.completion_events_since(cursor, limit=64, root_task_id=root_task_id)
        for event in page["events"]:
            task_id = event.get("task_id")
            if task_id in task_ids and task_id not in seen:
                seen[task_id] = event
        cursor = page["next_cursor"]
        if task_ids <= set(seen):
            break
        time.sleep(poll_interval_seconds)
    return seen, cursor


def _collect_payload(broker: object, task_id: str) -> dict[str, Any]:
    status = broker.status(task_id)
    collected = broker.collect(task_id)
    normalized = status.get("normalized_result") or {}
    result_path = broker.store.artifacts / task_id / "result.json"
    result_summary = None
    if result_path.is_file():
        result_summary = json.loads(result_path.read_text()).get("summary")
    return {
        "task_id": task_id,
        "state": collected.state.value if hasattr(collected.state, "value") else str(collected.state),
        "profile": status.get("profile"),
        "provider": status.get("provider"),
        "external_root_id": status.get("external_root_id"),
        "parent_task_id": status.get("parent_task_id"),
        "relationship": status.get("relationship"),
        "contract_status": normalized.get("contract_status"),
        "summary": result_summary or (normalized.get("summary") if isinstance(normalized.get("summary"), str) else None),
        "terminal": collected.state in TERMINAL_STATES,
    }


def _dispatch_participant(
    broker: object,
    *,
    plan: BoundedDebatePlan,
    anchor_task_id: str,
    participant: DebateParticipant,
    task_text: str,
    opening_by_id: dict[str, dict[str, Any]] | None = None,
) -> str:
    relationship = participant.relationship
    if relationship is None and participant.responds_to:
        relationship = "continues"
    if relationship is None:
        relationship = "delegates"
    parent_task_id = anchor_task_id
    if participant.responds_to and opening_by_id and participant.responds_to in opening_by_id:
        parent_task_id = opening_by_id[participant.responds_to]["task_id"]
    request = TaskRequest(
        task=task_text,
        workspace=plan.workspace,
        execution_mode=plan.execution_mode,
        profile=participant.profile,
        provider=participant.provider,
        parent_task_id=parent_task_id,
        external_root_id=plan.external_root_id,
        relationship=relationship,
        result_schema=participant.result_schema,
        explicit_fields=frozenset({"result_schema"}) if participant.result_schema else frozenset(),
    )
    record = broker.create(request)
    broker.start(record.id)
    return record.id


def _phase_failed(phase: str, collected: list[dict[str, Any]]) -> dict[str, Any] | None:
    failures = [
        item for item in collected
        if item.get("state") not in {TaskState.SUCCEEDED.value, TaskState.SUCCEEDED_WITH_WARNINGS.value}
    ]
    if not failures:
        return None
    return {
        "phase": phase,
        "status": "failed",
        "failed_tasks": failures,
        "message": f"phase {phase!r} had terminal child failure; later phases not started",
    }


def validate_synthesis_output(
  plan: BoundedDebatePlan,
  synthesis_collected: dict[str, Any],
  *,
    extra_check: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    contract_status = synthesis_collected.get("contract_status")
    summary = synthesis_collected.get("summary") or ""
    reasons: list[str] = []
    if contract_status not in SUCCESS_CONTRACT_STATUSES:
        reasons.append(f"contract_status={contract_status!r}")
    criteria = plan.acceptance_criteria.strip()
    if criteria and criteria.lower() not in summary.lower():
        reasons.append(f"acceptance_criteria not found in synthesis summary")
    if extra_check is not None and not extra_check(summary):
        reasons.append("extra_check rejected synthesis summary")
    return {
        "passed": not reasons,
        "contract_status": contract_status,
        "acceptance_criteria": criteria,
        "reasons": reasons,
    }


def apply_parent_materialization(
    workspace: Path,
    relative_path: str,
    content: str,
    *,
    dry_run: bool,
    materialization_owner: str = MaterializationOwner.PARENT_APPLIES_TEXT.value,
) -> dict[str, Any]:
    """Parent-owned, workspace-bounded write. Never attributes authorship to a provider."""
    if not relative_path or relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise BoundedDebateValidationError("materialization.relative_path must be a safe relative path")
    workspace_resolved = workspace.resolve()
    target = (workspace_resolved / relative_path).resolve()
    if target != workspace_resolved and workspace_resolved not in target.parents:
        raise BoundedDebateValidationError("materialization.relative_path escapes workspace")
    report = {
        "attempted": True,
        "applied": False,
        "dry_run": dry_run,
        "workspace": str(workspace_resolved),
        "relative_path": relative_path,
        "target_path": str(target),
        "bytes": len(content.encode("utf-8")),
        "materialization_owner": materialization_owner,
        "provider_wrote_files": False,
        "note": "Parent applied synthesis text; no runtime provider owns workspace writes.",
    }
    if dry_run:
        report["note"] = "Dry run only — parent would apply synthesis text here; no file written."
        return report
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    report["applied"] = True
    return report


def _synthesis_capability_note(broker: object, profile: str) -> str:
    contract = broker.runtime_registry.get(profile).capability_contract
    return contract.materialization_note


def run_bounded_debate_workflow(broker: object, raw_plan: dict[str, Any]) -> dict[str, Any]:
    plan = parse_bounded_debate_plan(raw_plan)
    cursor = broker.store.event_high_water_mark()
    phase_reports: list[dict[str, Any]] = []
    collected_by_participant: dict[str, dict[str, Any]] = {}

    anchor = broker.create(TaskRequest(
        plan.anchor_task,
        plan.workspace,
        external_root_id=plan.external_root_id,
        execution_mode="read_only",
        profile="mock",
    ))
    broker.start(anchor.id)
    broker.complete(anchor.id, f"anchor for {plan.workflow_id}")
    anchor_collected = _collect_payload(broker, anchor.id)
    collected_by_participant["__anchor__"] = anchor_collected

    def run_phase(
        phase: str,
        participants: tuple[DebateParticipant, ...],
        *,
        task_builder: Callable[[DebateParticipant], str] | None = None,
        opening_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        nonlocal cursor
        if not participants:
            phase_reports.append({"phase": phase, "status": "skipped", "task_ids": [], "collected": []})
            return None
        dispatched: list[dict[str, Any]] = []
        for participant in participants:
            task_text = task_builder(participant) if task_builder else participant.task
            task_id = _dispatch_participant(
                broker, plan=plan, anchor_task_id=anchor.id, participant=participant,
                task_text=task_text, opening_by_id=opening_by_id,
            )
            dispatched.append({"participant_id": participant.id, "task_id": task_id})
        expected = {item["task_id"] for item in dispatched}
        events, cursor = wait_for_task_completions(
            broker,
            task_ids=expected,
            after_event_id=cursor,
            timeout_seconds=plan.poll_timeout_seconds,
            root_task_id=anchor.id,
        )
        if events.keys() < expected:
            missing = expected - set(events)
            failure = {
                "phase": phase,
                "status": "timed_out",
                "missing_task_ids": sorted(missing),
                "message": f"completion_events did not observe all tasks in phase {phase!r} within timeout",
            }
            phase_reports.append({**failure, "dispatched": dispatched, "events_observed": list(events)})
            return failure
        collected = []
        for item in dispatched:
            payload = _collect_payload(broker, item["task_id"])
            payload["participant_id"] = item["participant_id"]
            collected.append(payload)
            collected_by_participant[item["participant_id"]] = payload
        phase_reports.append({
            "phase": phase,
            "status": "completed",
            "dispatched": dispatched,
            "events_observed": len(events),
            "collected": collected,
        })
        return _phase_failed(phase, collected)

    opening_by_id = {participant.id: {} for participant in plan.opening_positions}
    failure = run_phase(PHASE_OPENING, plan.opening_positions)
    if failure:
        return _workflow_result(broker, plan, anchor.id, phase_reports, failure=failure, collected_by_participant=collected_by_participant)

    for participant in plan.opening_positions:
        opening_by_id[participant.id] = collected_by_participant[participant.id]

    def rebuttal_task(participant: DebateParticipant) -> str:
        upstream = collected_by_participant.get(participant.responds_to or "", {})
        upstream_summary = upstream.get("summary") or "(no upstream summary)"
        return f"{participant.task}\n\nUpstream opening ({participant.responds_to}): {upstream_summary}"

    if plan.rebuttals:
        failure = run_phase(PHASE_REBUTTAL, plan.rebuttals, task_builder=rebuttal_task, opening_by_id=opening_by_id)
        if failure:
            return _workflow_result(broker, plan, anchor.id, phase_reports, failure=failure, collected_by_participant=collected_by_participant)

    def synthesis_task(_participant: DebateParticipant) -> str:
        lines = ["Prior debate outputs:"]
        for participant in (*plan.opening_positions, *plan.rebuttals):
            payload = collected_by_participant.get(participant.id, {})
            lines.append(f"- {participant.id}: {payload.get('summary') or '(missing)'}")
        lines.append(plan.synthesis.task)
        return "\n".join(lines)

    failure = run_phase(PHASE_SYNTHESIS, (plan.synthesis,), task_builder=synthesis_task)
    if failure:
        return _workflow_result(broker, plan, anchor.id, phase_reports, failure=failure, collected_by_participant=collected_by_participant)

    synthesis_collected = collected_by_participant[plan.synthesis.id]
    validation = validate_synthesis_output(plan, synthesis_collected)
    phase_reports.append({"phase": PHASE_VALIDATION, "status": "completed", **validation})
    if not validation["passed"]:
        return _workflow_result(
            broker,
            plan,
            anchor.id,
            phase_reports,
            failure={
                "phase": PHASE_VALIDATION,
                "status": "validation_failed",
                "validation": validation,
                "message": "synthesis validation failed; materialization not attempted",
            },
            collected_by_participant=collected_by_participant,
        )

    materialization_report: dict[str, Any]
    if not plan.materialization.enabled:
        materialization_report = {
            "attempted": False,
            "enabled": False,
            "note": "Materialization disabled by plan; parent retains ownership of any workspace change.",
        }
    else:
        synthesis_text = synthesis_collected.get("summary") or ""
        materialization_report = apply_parent_materialization(
            Path(plan.workspace),
            plan.materialization.relative_path,
            synthesis_text,
            dry_run=plan.materialization.dry_run,
        )
    phase_reports.append({"phase": PHASE_MATERIALIZATION, "status": "completed", **materialization_report})

    return _workflow_result(
        broker,
        plan,
        anchor.id,
        phase_reports,
        failure=None,
        collected_by_participant=collected_by_participant,
        materialization=materialization_report,
        validation=validation,
    )


def _workflow_result(
    broker: object,
    plan: BoundedDebatePlan,
    anchor_task_id: str,
    phase_reports: list[dict[str, Any]],
    *,
    failure: dict[str, Any] | None,
    collected_by_participant: dict[str, dict[str, Any]],
    materialization: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "workflow_id": plan.workflow_id,
        "external_root_id": plan.external_root_id,
        "anchor_task_id": anchor_task_id,
        "status": "failed" if failure else "completed",
        "failure": failure,
        "validation": validation,
        "materialization": materialization,
        "phases": phase_reports,
        "participants_collected": {
            key: value for key, value in collected_by_participant.items() if not key.startswith("__")
        },
        "synthesis_capability_note": _synthesis_capability_note(broker, plan.synthesis.profile),
        "limitations": [
            "reference_workflow_not_autonomous_council",
            "caller_owns_retry_and_round_decisions",
            "completion_events_cursor_only_no_push_notifications",
            "parent_materialization_required_for_text_synthesis",
        ],
        "note": (
            "Reference bounded debate workflow recorded broker-observed evidence only. "
            "The parent that invoked this helper owns validation, optional materialization, "
            "and any follow-up rounds — this helper does not schedule another debate."
        ),
    }
