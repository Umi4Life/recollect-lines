"""Bounded rework and escalation policy (RFC-003).

Explicit operator policy plus explicit per-task rework metadata. The broker
never infers rework intent from task text, graph position, or model prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .model_profile import COST_CLASSES, RESOLUTION_CONFIGURED, RESOLUTION_UNCONFIGURED
from .models import TaskRecord, TaskState

POLICY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
COST_REWORK_POLICIES_CONFIG_KEY = "cost_rework_policies"
ALLOWED_POLICY_KEYS = frozenset({
    "max_premium_tasks",
    "max_premium_retries_per_task",
    "max_escalations_per_workflow",
    "allow_higher_cost_reexecution",
    "require_escalation_reason",
})

VALID_REWORK_SCOPES = frozenset({"targeted", "full"})
REWORK_SCOPE_TARGETED = "targeted"
REWORK_SCOPE_FULL = "full"

MAX_ESCALATION_REASON_LEN = 500

COST_CLASS_RANK: dict[str, int] = {
    "low": 0,
    "standard": 1,
    "premium": 2,
    "unknown": -1,
}

TERMINAL_SUCCESS_STATES = frozenset({
    TaskState.SUCCEEDED,
    TaskState.SUCCEEDED_WITH_WARNINGS,
})

SATISFIED_CONTRACT_STATUSES = frozenset({"satisfied", "not_requested"})


class CostReworkPolicyValidationError(ValueError):
    """Invalid cost_rework_policy selection or rework metadata."""


class CostReworkPolicyConfigError(ValueError):
    """Invalid operator cost_rework_policies configuration."""


@dataclass(frozen=True)
class CostReworkPolicy:
    policy_id: str
    max_premium_tasks: int
    max_premium_retries_per_task: int
    max_escalations_per_workflow: int
    allow_higher_cost_reexecution: bool
    require_escalation_reason: bool


@dataclass(frozen=True)
class CostReworkPolicyRegistry:
    policies: dict[str, CostReworkPolicy]

    def known_policy_ids(self) -> frozenset[str]:
        return frozenset(self.policies)


@dataclass(frozen=True)
class ReworkMetadata:
    prior_task_id: str
    scope: str
    escalation_reason: str | None


@dataclass(frozen=True)
class WorkflowUsage:
    premium_tasks: int
    premium_retries_by_prior: dict[str, int]
    escalations: int


def _parse_positive_int(raw: Any, *, policy_id: str, field: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise CostReworkPolicyConfigError(
            f"cost_rework_policies.{policy_id}.{field} must be a non-negative integer"
        )
    return raw


def _parse_bool(raw: Any, *, policy_id: str, field: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise CostReworkPolicyConfigError(
            f"cost_rework_policies.{policy_id}.{field} must be a boolean when set"
        )
    return raw


def _parse_configured_policy(policy_id: str, raw: Any) -> CostReworkPolicy:
    if not POLICY_ID_PATTERN.match(policy_id):
        raise CostReworkPolicyConfigError(
            f"cost_rework_policies key {policy_id!r} must match {POLICY_ID_PATTERN.pattern}"
        )
    if not isinstance(raw, dict):
        raise CostReworkPolicyConfigError(f"cost_rework_policies.{policy_id} must be an object")
    unknown = set(raw) - ALLOWED_POLICY_KEYS
    if unknown:
        raise CostReworkPolicyConfigError(
            f"cost_rework_policies.{policy_id}: unknown key(s) {', '.join(sorted(unknown))}"
        )
    missing = {"max_premium_tasks", "max_premium_retries_per_task", "max_escalations_per_workflow"} - set(raw)
    if missing:
        raise CostReworkPolicyConfigError(
            f"cost_rework_policies.{policy_id} missing required key(s) {', '.join(sorted(missing))}"
        )
    return CostReworkPolicy(
        policy_id=policy_id,
        max_premium_tasks=_parse_positive_int(raw["max_premium_tasks"], policy_id=policy_id, field="max_premium_tasks"),
        max_premium_retries_per_task=_parse_positive_int(
            raw["max_premium_retries_per_task"], policy_id=policy_id, field="max_premium_retries_per_task",
        ),
        max_escalations_per_workflow=_parse_positive_int(
            raw["max_escalations_per_workflow"], policy_id=policy_id, field="max_escalations_per_workflow",
        ),
        allow_higher_cost_reexecution=_parse_bool(
            raw.get("allow_higher_cost_reexecution"),
            policy_id=policy_id,
            field="allow_higher_cost_reexecution",
            default=False,
        ),
        require_escalation_reason=_parse_bool(
            raw.get("require_escalation_reason"),
            policy_id=policy_id,
            field="require_escalation_reason",
            default=True,
        ),
    )


def parse_cost_rework_policies_document(data: Any) -> dict[str, CostReworkPolicy]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CostReworkPolicyConfigError("cost_rework_policies must be an object when provided")
    policies: dict[str, CostReworkPolicy] = {}
    for policy_id, entry in data.items():
        if policy_id in policies:
            raise CostReworkPolicyConfigError(f"Duplicate cost_rework_policies entry: {policy_id!r}")
        policies[policy_id] = _parse_configured_policy(policy_id, entry)
    return policies


def load_cost_rework_policies_config(path: Path) -> dict[str, CostReworkPolicy]:
    try:
        raw_text = path.read_text()
    except OSError as error:
        raise CostReworkPolicyConfigError(f"Cannot read operator configuration {path}: {error}") from error
    from .providers import _parse_yaml_document, _sniff_config_format

    fmt = _sniff_config_format(path, raw_text)
    if fmt == "json":
        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as error:
            raise CostReworkPolicyConfigError(
                f"Operator configuration {path} is not valid JSON: {error}"
            ) from error
    else:
        document = _parse_yaml_document(path, raw_text)
        if document is None:
            return {}
    if not isinstance(document, dict):
        raise CostReworkPolicyConfigError(f"Operator configuration {path} must be a top-level object")
    return parse_cost_rework_policies_document(document.get(COST_REWORK_POLICIES_CONFIG_KEY))


def build_cost_rework_policy_registry(*, configured: dict[str, CostReworkPolicy]) -> CostReworkPolicyRegistry:
    return CostReworkPolicyRegistry(dict(configured))


def normalize_cost_rework_policy(
    raw: Any,
    *,
    registry: CostReworkPolicyRegistry | None = None,
) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise CostReworkPolicyValidationError(
            "cost_rework_policy must be a non-empty string when provided"
        )
    policy_id = raw.strip()
    if registry is not None and policy_id not in registry.policies:
        raise CostReworkPolicyValidationError(
            f"Unknown cost_rework_policy {policy_id!r}; known ids: {sorted(registry.policies)}"
        )
    return policy_id


def normalize_rework_metadata(
    *,
    prior_task_id: Any,
    scope: Any,
    escalation_reason: Any,
) -> ReworkMetadata | None:
    fields = {
        "rework_prior_task_id": prior_task_id,
        "rework_scope": scope,
        "escalation_reason": escalation_reason,
    }
    present = {key: value for key, value in fields.items() if value is not None}
    if not present:
        return None
    if prior_task_id is None or scope is None:
        raise CostReworkPolicyValidationError(
            "rework_prior_task_id and rework_scope must both be provided for rework metadata"
        )
    if not isinstance(prior_task_id, str) or not prior_task_id.strip():
        raise CostReworkPolicyValidationError("rework_prior_task_id must be a non-empty string")
    if not isinstance(scope, str) or scope not in VALID_REWORK_SCOPES:
        raise CostReworkPolicyValidationError(
            f"rework_scope must be one of {sorted(VALID_REWORK_SCOPES)}, got {scope!r}"
        )
    reason: str | None
    if escalation_reason is None:
        reason = None
    elif not isinstance(escalation_reason, str) or not escalation_reason.strip():
        raise CostReworkPolicyValidationError(
            "escalation_reason must be a non-empty string when provided"
        )
    else:
        reason = escalation_reason.strip()
        if len(reason) > MAX_ESCALATION_REASON_LEN:
            raise CostReworkPolicyValidationError(
                f"escalation_reason must be at most {MAX_ESCALATION_REASON_LEN} characters"
            )
    return ReworkMetadata(
        prior_task_id=prior_task_id.strip(),
        scope=scope,
        escalation_reason=reason,
    )


def is_premium_cost_class(cost_class: str) -> bool:
    return cost_class == "premium"


def cost_class_rank(cost_class: str) -> int:
    return COST_CLASS_RANK.get(cost_class, -1)


def is_higher_cost_class(candidate: str, baseline: str) -> bool:
    return cost_class_rank(candidate) > cost_class_rank(baseline)


def unconfigured_cost_policy_snapshot() -> dict[str, Any]:
    return {"resolution": RESOLUTION_UNCONFIGURED, "policy_id": None}


def configured_cost_policy_limits(policy: CostReworkPolicy) -> dict[str, Any]:
    return {
        "max_premium_tasks": policy.max_premium_tasks,
        "max_premium_retries_per_task": policy.max_premium_retries_per_task,
        "max_escalations_per_workflow": policy.max_escalations_per_workflow,
        "allow_higher_cost_reexecution": policy.allow_higher_cost_reexecution,
        "require_escalation_reason": policy.require_escalation_reason,
    }


def _read_json_artifact(artifacts_dir: Path, task_id: str, name: str) -> dict[str, Any] | None:
    path = artifacts_dir / task_id / name
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _task_cost_class(artifacts_dir: Path, task_id: str) -> str:
    snapshot = _read_json_artifact(artifacts_dir, task_id, "model_profile_resolution.json")
    if snapshot is None:
        return "unknown"
    cost_class = snapshot.get("cost_class")
    return cost_class if isinstance(cost_class, str) and cost_class in COST_CLASSES else "unknown"


def _task_rework_from_request(artifacts_dir: Path, task_id: str) -> ReworkMetadata | None:
    payload = _read_json_artifact(artifacts_dir, task_id, "request.json")
    if payload is None:
        return None
    try:
        return normalize_rework_metadata(
            prior_task_id=payload.get("rework_prior_task_id"),
            scope=payload.get("rework_scope"),
            escalation_reason=payload.get("escalation_reason"),
        )
    except CostReworkPolicyValidationError:
        return None


def _task_started(artifacts_dir: Path, task: TaskRecord) -> bool:
    if task.state == TaskState.REJECTED:
        return False
    return _read_json_artifact(artifacts_dir, task.id, "model_profile_resolution.json") is not None


def compute_workflow_usage(
    *,
    root_task_id: str,
    tasks: list[TaskRecord],
    artifacts_dir: Path,
    exclude_task_id: str | None = None,
) -> WorkflowUsage:
    premium_tasks = 0
    premium_retries_by_prior: dict[str, int] = {}
    escalations = 0
    for task in tasks:
        if task.root_task_id != root_task_id:
            continue
        if exclude_task_id is not None and task.id == exclude_task_id:
            continue
        if not _task_started(artifacts_dir, task):
            continue
        cost_class = _task_cost_class(artifacts_dir, task.id)
        rework = _task_rework_from_request(artifacts_dir, task.id)
        if is_premium_cost_class(cost_class):
            premium_tasks += 1
            if rework is not None:
                premium_retries_by_prior[rework.prior_task_id] = (
                    premium_retries_by_prior.get(rework.prior_task_id, 0) + 1
                )
        if rework is not None and rework.scope == REWORK_SCOPE_FULL:
            escalations += 1
    return WorkflowUsage(
        premium_tasks=premium_tasks,
        premium_retries_by_prior=premium_retries_by_prior,
        escalations=escalations,
    )


def _prior_task_satisfied(
    *,
    prior: TaskRecord,
    artifacts_dir: Path,
) -> bool:
    if prior.state not in TERMINAL_SUCCESS_STATES:
        return False
    normalized = _read_json_artifact(artifacts_dir, prior.id, "normalized_result.json")
    if normalized is not None:
        parser = normalized.get("parser")
        if isinstance(parser, dict):
            contract_status = parser.get("contract_status")
            if contract_status not in SATISFIED_CONTRACT_STATUSES:
                return False
        broker = normalized.get("broker_observed")
        if isinstance(broker, dict):
            verification = broker.get("verification")
            if isinstance(verification, dict) and verification.get("outcome") == "failed":
                return False
        return True
    # Broker-complete path without normalized envelope: succeeded terminal is enough.
    return True


def evaluate_cost_rework_preflight(
    *,
    record: TaskRecord,
    policy: CostReworkPolicy,
    rework: ReworkMetadata | None,
    model_profile_snapshot: dict[str, Any],
    get_task: Callable[[str], TaskRecord],
    list_tree_tasks: Callable[[str, int], list[TaskRecord]],
    artifacts_dir: Path,
    tree_limit: int = 256,
) -> dict[str, Any] | None:
    """Return a machine-readable rejection dict, or None when launch may proceed."""
    root_task_id = record.root_task_id or record.id
    cost_class = model_profile_snapshot.get("cost_class", "unknown")
    if model_profile_snapshot.get("resolution") != RESOLUTION_CONFIGURED:
        return {
            "reason": "unknown_model_profile_under_policy",
            "detail": "cost_rework_policy requires a configured model_profile with known cost_class",
            "cost_class": cost_class,
        }
    if cost_class not in COST_CLASSES or cost_class == "unknown":
        return {
            "reason": "unknown_model_profile_under_policy",
            "detail": "model_profile cost_class must be configured when cost_rework_policy is selected",
            "cost_class": cost_class,
        }

    if rework is not None:
        if policy.require_escalation_reason and not rework.escalation_reason:
            return {
                "reason": "missing_escalation_reason",
                "detail": "escalation_reason is required by the selected cost_rework_policy",
            }
        try:
            prior = get_task(rework.prior_task_id)
        except KeyError:
            return {
                "reason": "unknown_rework_prior_task",
                "rework_prior_task_id": rework.prior_task_id,
            }
        prior_root = prior.root_task_id or prior.id
        if prior_root != root_task_id:
            return {
                "reason": "cross_workflow_rework_reference",
                "rework_prior_task_id": rework.prior_task_id,
                "prior_root_task_id": prior_root,
                "task_root_task_id": root_task_id,
            }
        if rework.scope == REWORK_SCOPE_FULL and _prior_task_satisfied(prior=prior, artifacts_dir=artifacts_dir):
            prior_cost_class = _task_cost_class(artifacts_dir, prior.id)
            if is_higher_cost_class(cost_class, prior_cost_class):
                if not policy.allow_higher_cost_reexecution:
                    return {
                        "reason": "higher_cost_duplicate_reexecution_denied",
                        "detail": (
                            "full rework would duplicate satisfied work at higher cost; "
                            "allow_higher_cost_reexecution is false"
                        ),
                        "rework_prior_task_id": rework.prior_task_id,
                        "prior_cost_class": prior_cost_class,
                        "task_cost_class": cost_class,
                    }
                if policy.require_escalation_reason and not rework.escalation_reason:
                    return {
                        "reason": "missing_escalation_reason",
                        "detail": "higher-cost reexecution requires an explicit escalation_reason",
                    }

    tasks = list_tree_tasks(root_task_id, tree_limit)
    usage = compute_workflow_usage(
        root_task_id=root_task_id,
        tasks=tasks,
        artifacts_dir=artifacts_dir,
        exclude_task_id=record.id,
    )

    projected_premium = usage.premium_tasks + (1 if is_premium_cost_class(cost_class) else 0)
    if projected_premium > policy.max_premium_tasks:
        return {
            "reason": "premium_task_budget_exceeded",
            "limit": policy.max_premium_tasks,
            "used": usage.premium_tasks,
            "projected": projected_premium,
        }

    if rework is not None and is_premium_cost_class(cost_class):
        prior_retries = usage.premium_retries_by_prior.get(rework.prior_task_id, 0)
        projected_retries = prior_retries + 1
        if projected_retries > policy.max_premium_retries_per_task:
            return {
                "reason": "premium_retry_budget_exceeded",
                "limit": policy.max_premium_retries_per_task,
                "rework_prior_task_id": rework.prior_task_id,
                "used": prior_retries,
                "projected": projected_retries,
            }

    projected_escalations = usage.escalations + (
        1 if rework is not None and rework.scope == REWORK_SCOPE_FULL else 0
    )
    if projected_escalations > policy.max_escalations_per_workflow:
        return {
            "reason": "escalation_budget_exceeded",
            "limit": policy.max_escalations_per_workflow,
            "used": usage.escalations,
            "projected": projected_escalations,
        }

    return None


def build_cost_policy_snapshot(
    *,
    policy: CostReworkPolicy,
    rework: ReworkMetadata | None,
    model_profile_snapshot: dict[str, Any],
    usage: WorkflowUsage,
    cost_class: str,
    escalation_decision: str,
) -> dict[str, Any]:
    limits = configured_cost_policy_limits(policy)
    premium_tasks_used = usage.premium_tasks + (1 if is_premium_cost_class(cost_class) else 0)
    prior_retries_used = (
        usage.premium_retries_by_prior.get(rework.prior_task_id, 0)
        if rework is not None and is_premium_cost_class(cost_class)
        else 0
    )
    if rework is not None and is_premium_cost_class(cost_class):
        prior_retries_used += 1
    escalations_used = usage.escalations + (
        1 if rework is not None and rework.scope == REWORK_SCOPE_FULL else 0
    )
    snapshot: dict[str, Any] = {
        "resolution": RESOLUTION_CONFIGURED,
        "policy_id": policy.policy_id,
        "limits": limits,
        "usage": {
            "premium_tasks": premium_tasks_used,
            "premium_retries_for_prior": prior_retries_used if rework is not None else 0,
            "escalations": escalations_used,
        },
        "remaining": {
            "premium_tasks": max(0, policy.max_premium_tasks - premium_tasks_used),
            "premium_retries_for_prior": (
                max(0, policy.max_premium_retries_per_task - prior_retries_used)
                if rework is not None
                else policy.max_premium_retries_per_task
            ),
            "escalations": max(0, policy.max_escalations_per_workflow - escalations_used),
        },
        "model_profile_cost_class": cost_class,
        "escalation_decision": escalation_decision,
        "preflight_status": "accepted",
    }
    if rework is not None:
        snapshot["rework"] = {
            "prior_task_id": rework.prior_task_id,
            "scope": rework.scope,
            "escalation_reason_present": rework.escalation_reason is not None,
        }
    return snapshot


def cost_policy_public_projection(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    projection: dict[str, Any] = {
        "resolution": snapshot.get("resolution", RESOLUTION_UNCONFIGURED),
        "preflight_status": snapshot.get("preflight_status"),
    }
    policy_id = snapshot.get("policy_id")
    if isinstance(policy_id, str) and policy_id:
        projection["policy_id"] = policy_id
    for key in ("usage", "remaining", "limits"):
        value = snapshot.get(key)
        if isinstance(value, dict) and value:
            projection[key] = dict(value)
    rework = snapshot.get("rework")
    if isinstance(rework, dict) and rework:
        projection["rework"] = {
            key: rework[key]
            for key in ("prior_task_id", "scope", "escalation_reason_present")
            if key in rework
        }
    escalation_decision = snapshot.get("escalation_decision")
    if isinstance(escalation_decision, str) and escalation_decision:
        projection["escalation_decision"] = escalation_decision
    cost_class = snapshot.get("model_profile_cost_class")
    if isinstance(cost_class, str) and cost_class:
        projection["model_profile_cost_class"] = cost_class
    return projection


def rework_request_payload(rework: ReworkMetadata | None) -> dict[str, Any]:
    if rework is None:
        return {}
    payload: dict[str, Any] = {
        "rework_prior_task_id": rework.prior_task_id,
        "rework_scope": rework.scope,
    }
    if rework.escalation_reason is not None:
        payload["escalation_reason"] = rework.escalation_reason
    return payload
