"""Bounded rework and escalation policy (RFC-003)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recollect_lines.completion_events import build_completion_event
from recollect_lines.cost_rework_policy import (
    CostReworkPolicyConfigError,
    CostReworkPolicyValidationError,
    build_cost_rework_policy_registry,
    normalize_cost_rework_policy,
    normalize_rework_metadata,
    parse_cost_rework_policies_document,
)
from recollect_lines.model_profile import build_model_profile_registry, parse_model_profiles_document
from recollect_lines.models import ProfilePolicy, TaskRequest, TaskState
from recollect_lines.result_normalization import build_normalized_envelope, concise_normalized_view
from recollect_lines.service import Broker

MOCK_POLICY = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)

SAMPLE_RESOURCES = {
    "monetary_cost": "negligible",
    "quota_scarcity": "none",
    "latency": "low",
    "local_compute_occupancy": "low",
    "context_cost": "low",
}

SAMPLE_PROFILES = {
    "dev-mock-low": {
        "runtime": "mock",
        "cost_class": "low",
        "usage_bucket": "dev-sandbox",
        "resources": SAMPLE_RESOURCES,
    },
    "mock-premium-child": {
        "runtime": "mock",
        "cost_class": "premium",
        "usage_bucket": "critical-review",
        "resources": {
            "monetary_cost": "high",
            "quota_scarcity": "high",
            "latency": "high",
            "local_compute_occupancy": "high",
            "context_cost": "high",
        },
    },
    "mock-standard": {
        "runtime": "mock",
        "cost_class": "standard",
        "usage_bucket": "batch",
        "resources": SAMPLE_RESOURCES,
    },
}

SAMPLE_POLICIES = {
    "strict-review": {
        "max_premium_tasks": 2,
        "max_premium_retries_per_task": 1,
        "max_escalations_per_workflow": 1,
        "allow_higher_cost_reexecution": False,
        "require_escalation_reason": True,
    },
    "allow-escalation": {
        "max_premium_tasks": 3,
        "max_premium_retries_per_task": 2,
        "max_escalations_per_workflow": 2,
        "allow_higher_cost_reexecution": True,
        "require_escalation_reason": True,
    },
    "tight-retries": {
        "max_premium_tasks": 5,
        "max_premium_retries_per_task": 1,
        "max_escalations_per_workflow": 5,
        "allow_higher_cost_reexecution": False,
        "require_escalation_reason": True,
    },
}


def model_profile_registry():
    return build_model_profile_registry(configured=parse_model_profiles_document(SAMPLE_PROFILES))


def cost_policy_registry():
    return build_cost_rework_policy_registry(configured=parse_cost_rework_policies_document(SAMPLE_POLICIES))


class CostReworkPolicyParsingTests(unittest.TestCase):
    def test_valid_document_parses(self):
        policies = parse_cost_rework_policies_document(SAMPLE_POLICIES)
        self.assertEqual(policies["strict-review"].max_premium_tasks, 2)

    def test_missing_limit_rejected(self):
        with self.assertRaises(CostReworkPolicyConfigError):
            parse_cost_rework_policies_document({
                "bad": {"max_premium_tasks": 1},
            })


class BrokerCostReworkPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(
            self.home,
            profiles={"mock": MOCK_POLICY},
            model_profile_registry=model_profile_registry(),
            cost_rework_policy_registry=cost_policy_registry(),
        )

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _finish_satisfied(self, record):
        self.broker.start(record.id)
        finished = self.broker.complete(record.id, "done")
        self.broker._persist_normalized_result(
            finished,
            result={
                "task_id": record.id,
                "state": "succeeded",
                "summary": "done",
                "runtime": {"adapter": "mock"},
            },
            collected={"summary": "done", "adapter": "mock"},
            gate={"policy": "none", "outcome": "not_configured"},
            final_state=TaskState.SUCCEEDED,
        )
        return self.broker.store.get(record.id)

    def test_legacy_without_policy_unchanged(self):
        record = self.broker.create(TaskRequest("work", str(self.workspace)))
        started = self.broker.start(record.id)
        self.assertIn(started.state, (TaskState.PREPARING, TaskState.RUNNING))
        projection = self.broker._read_cost_policy_projection(record.id)
        assert projection is not None
        self.assertEqual(projection["resolution"], "unconfigured")

    def test_policy_snapshot_persisted_at_start(self):
        record = self.broker.create(
            TaskRequest(
                "work",
                str(self.workspace),
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
            ),
        )
        self.broker.start(record.id)
        path = self.broker.store.artifacts / record.id / "cost_rework_policy_resolution.json"
        self.assertTrue(path.is_file())
        snapshot = json.loads(path.read_text())
        self.assertEqual(snapshot["policy_id"], "strict-review")
        self.assertEqual(snapshot["preflight_status"], "accepted")

    def test_unknown_policy_rejected_at_create(self):
        with self.assertRaises(ValueError):
            self.broker.create(
                TaskRequest("work", str(self.workspace), cost_rework_policy="missing"),
            )

    def test_unconfigured_profile_rejected_under_policy(self):
        record = self.broker.create(
            TaskRequest("work", str(self.workspace), cost_rework_policy="strict-review"),
        )
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.REJECTED)
        self.assertIsNone(self.broker.store.get_launch(record.id))

    def test_premium_child_budget_independent_of_parent(self):
        parent = self.broker.create(TaskRequest("parent", str(self.workspace)))
        self.broker.start(parent.id)
        child = self.broker.create(
            TaskRequest(
                "child",
                str(self.workspace),
                parent_task_id=parent.id,
                model_profile="mock-premium-child",
                cost_rework_policy="strict-review",
            ),
        )
        started = self.broker.start(child.id)
        self.assertIn(started.state, (TaskState.PREPARING, TaskState.RUNNING))
        projection = self.broker._read_cost_policy_projection(child.id)
        assert projection is not None
        self.assertEqual(projection["model_profile_cost_class"], "premium")

    def test_targeted_rework_accepted_when_budgeted(self):
        prior = self.broker.create(
            TaskRequest(
                "prior",
                str(self.workspace),
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
            ),
        )
        self.broker.start(prior.id)
        rework = self.broker.create(
            TaskRequest(
                "fix",
                str(self.workspace),
                parent_task_id=prior.id,
                model_profile="mock-premium-child",
                cost_rework_policy="strict-review",
                rework_prior_task_id=prior.id,
                rework_scope="targeted",
                escalation_reason="address finding #2",
            ),
        )
        started = self.broker.start(rework.id)
        self.assertIn(started.state, (TaskState.PREPARING, TaskState.RUNNING))
        snapshot = json.loads(
            (self.broker.store.artifacts / rework.id / "cost_rework_policy_resolution.json").read_text(),
        )
        self.assertEqual(snapshot["rework"]["scope"], "targeted")
        self.assertTrue(snapshot["rework"]["escalation_reason_present"])

    def test_missing_rework_fields_rejected_at_create(self):
        with self.assertRaises(ValueError):
            self.broker.create(
                TaskRequest(
                    "bad",
                    str(self.workspace),
                    cost_rework_policy="strict-review",
                    rework_prior_task_id="tsk_missing",
                ),
            )

    def test_cross_root_rework_rejected_before_launch(self):
        other_root = self.broker.create(
            TaskRequest("other", str(self.workspace), model_profile="dev-mock-low"),
        )
        self.broker.start(other_root.id)
        local = self.broker.create(
            TaskRequest(
                "local",
                str(self.workspace),
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
            ),
        )
        self.broker.start(local.id)
        rework = self.broker.create(
            TaskRequest(
                "cross",
                str(self.workspace),
                parent_task_id=local.id,
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
                rework_prior_task_id=other_root.id,
                rework_scope="targeted",
                escalation_reason="wrong root",
            ),
        )
        started = self.broker.start(rework.id)
        self.assertEqual(started.state, TaskState.REJECTED)

    def test_higher_cost_full_duplicate_rejected_by_default(self):
        prior = self.broker.create(
            TaskRequest(
                "prior",
                str(self.workspace),
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
            ),
        )
        self._finish_satisfied(prior)
        rework = self.broker.create(
            TaskRequest(
                "duplicate",
                str(self.workspace),
                parent_task_id=prior.id,
                model_profile="mock-premium-child",
                cost_rework_policy="strict-review",
                rework_prior_task_id=prior.id,
                rework_scope="full",
                escalation_reason="try premium model",
            ),
        )
        started = self.broker.start(rework.id)
        self.assertEqual(started.state, TaskState.REJECTED)
        events = self.broker.store.events(rework.id)
        rejection = next(event for event in events if event["state_after"] == TaskState.REJECTED.value)
        self.assertEqual(rejection["metadata"]["reason"], "higher_cost_duplicate_reexecution_denied")

    def test_higher_cost_full_duplicate_allowed_when_policy_permits(self):
        prior = self.broker.create(
            TaskRequest(
                "prior",
                str(self.workspace),
                model_profile="dev-mock-low",
                cost_rework_policy="allow-escalation",
            ),
        )
        self._finish_satisfied(prior)
        rework = self.broker.create(
            TaskRequest(
                "duplicate",
                str(self.workspace),
                parent_task_id=prior.id,
                model_profile="mock-premium-child",
                cost_rework_policy="allow-escalation",
                rework_prior_task_id=prior.id,
                rework_scope="full",
                escalation_reason="authorized premium rerun",
            ),
        )
        started = self.broker.start(rework.id)
        self.assertIn(started.state, (TaskState.PREPARING, TaskState.RUNNING))

    def test_premium_task_limit_boundary(self):
        first = self.broker.create(
            TaskRequest(
                "one",
                str(self.workspace),
                model_profile="mock-premium-child",
                cost_rework_policy="strict-review",
            ),
        )
        self.broker.start(first.id)
        second = self.broker.create(
            TaskRequest(
                "two",
                str(self.workspace),
                parent_task_id=first.id,
                model_profile="mock-premium-child",
                cost_rework_policy="strict-review",
            ),
        )
        self.broker.start(second.id)
        third = self.broker.create(
            TaskRequest(
                "three",
                str(self.workspace),
                parent_task_id=first.id,
                model_profile="mock-premium-child",
                cost_rework_policy="strict-review",
            ),
        )
        started = self.broker.start(third.id)
        self.assertEqual(started.state, TaskState.REJECTED)

    def test_premium_retry_limit_boundary(self):
        prior = self.broker.create(
            TaskRequest(
                "prior",
                str(self.workspace),
                model_profile="mock-premium-child",
                cost_rework_policy="tight-retries",
            ),
        )
        self.broker.start(prior.id)
        first_retry = self.broker.create(
            TaskRequest(
                "retry1",
                str(self.workspace),
                parent_task_id=prior.id,
                model_profile="mock-premium-child",
                cost_rework_policy="tight-retries",
                rework_prior_task_id=prior.id,
                rework_scope="targeted",
                escalation_reason="first retry",
            ),
        )
        self.broker.start(first_retry.id)
        second_retry = self.broker.create(
            TaskRequest(
                "retry2",
                str(self.workspace),
                parent_task_id=prior.id,
                model_profile="mock-premium-child",
                cost_rework_policy="tight-retries",
                rework_prior_task_id=prior.id,
                rework_scope="targeted",
                escalation_reason="second retry",
            ),
        )
        started = self.broker.start(second_retry.id)
        self.assertEqual(started.state, TaskState.REJECTED)
        events = self.broker.store.events(second_retry.id)
        rejection = next(event for event in events if event["state_after"] == TaskState.REJECTED.value)
        self.assertEqual(rejection["metadata"]["reason"], "premium_retry_budget_exceeded")

    def test_escalation_limit_boundary(self):
        prior = self.broker.create(
            TaskRequest(
                "prior",
                str(self.workspace),
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
            ),
        )
        self.broker.start(prior.id)
        first_full = self.broker.create(
            TaskRequest(
                "full1",
                str(self.workspace),
                parent_task_id=prior.id,
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
                rework_prior_task_id=prior.id,
                rework_scope="full",
                escalation_reason="first full",
            ),
        )
        self.broker.start(first_full.id)
        second_full = self.broker.create(
            TaskRequest(
                "full2",
                str(self.workspace),
                parent_task_id=prior.id,
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
                rework_prior_task_id=prior.id,
                rework_scope="full",
                escalation_reason="second full",
            ),
        )
        started = self.broker.start(second_full.id)
        self.assertEqual(started.state, TaskState.REJECTED)

    def test_concise_and_completion_projections_are_safe(self):
        prior = self.broker.create(
            TaskRequest("prior", str(self.workspace), model_profile="dev-mock-low", cost_rework_policy="strict-review"),
        )
        self.broker.start(prior.id)
        record = self.broker.create(
            TaskRequest(
                "work",
                str(self.workspace),
                parent_task_id=prior.id,
                model_profile="dev-mock-low",
                cost_rework_policy="strict-review",
                rework_prior_task_id=prior.id,
                rework_scope="targeted",
                escalation_reason="secret operator rationale",
            ),
        )
        self.broker.start(record.id)
        finished = self.broker.complete(record.id, "done")
        projection = self.broker._read_cost_policy_projection(record.id)
        envelope = build_normalized_envelope(
            record=finished,
            result={"task_id": record.id, "state": "succeeded", "summary": "done", "runtime": {"adapter": "mock"}},
            collected={"summary": "done", "adapter": "mock"},
            gate={"policy": "none", "outcome": "not_configured"},
            verification=None,
            manifest={"files": []},
            launch=None,
            raw_output_artifact=None,
            final_state=TaskState.SUCCEEDED,
            cost_policy_status=projection,
        )
        view = concise_normalized_view(envelope)
        assert view is not None
        status = view["cost_policy_status"]
        self.assertTrue(status["rework"]["escalation_reason_present"])
        self.assertNotIn("secret operator rationale", json.dumps(view))
        rows, _ = self.broker.store.events_since(0, limit=10, task_id=record.id)
        event = build_completion_event(self.broker.store, rows[-1])
        self.assertIn("cost_policy_status", event)
        self.assertNotIn("secret operator rationale", json.dumps(event))


class NormalizationTests(unittest.TestCase):
    def test_normalize_helpers(self):
        registry = cost_policy_registry()
        self.assertEqual(normalize_cost_rework_policy("strict-review", registry=registry), "strict-review")
        rework = normalize_rework_metadata(
            prior_task_id="tsk_abc",
            scope="targeted",
            escalation_reason="because",
        )
        assert rework is not None
        self.assertEqual(rework.scope, "targeted")
        with self.assertRaises(CostReworkPolicyValidationError):
            normalize_rework_metadata(prior_task_id=None, scope="targeted", escalation_reason="because")


if __name__ == "__main__":
    unittest.main()
