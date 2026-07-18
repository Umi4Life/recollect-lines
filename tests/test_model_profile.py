"""Model-profile and resource metadata (RFC-003 foundation)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recollect_lines.models import ProfilePolicy, TaskRequest, TaskState
from recollect_lines.mcp_server import _build_task_request
from recollect_lines.model_profile import (
    COST_CLASSES,
    ModelProfileConfigError,
    ModelProfileValidationError,
    RESOLUTION_UNCONFIGURED,
    build_model_profile_registry,
    evaluate_model_profile_preflight,
    model_profile_public_projection,
    normalize_model_profile,
    parse_model_profiles_document,
    resolve_model_profile_snapshot,
    unconfigured_model_profile_snapshot,
)
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
}


def model_profile_registry():
    return build_model_profile_registry(configured=parse_model_profiles_document(SAMPLE_PROFILES))


class ModelProfileParsingTests(unittest.TestCase):
    def test_valid_document_parses(self):
        profiles = parse_model_profiles_document(SAMPLE_PROFILES)
        self.assertIn("dev-mock-low", profiles)
        self.assertEqual(profiles["dev-mock-low"].cost_class, "low")

    def test_unknown_cost_class_rejected(self):
        with self.assertRaises(ModelProfileConfigError):
            parse_model_profiles_document({
                "bad": {**SAMPLE_PROFILES["dev-mock-low"], "cost_class": "cheap"},
            })

    def test_invalid_usage_bucket_rejected(self):
        with self.assertRaises(ModelProfileConfigError):
            parse_model_profiles_document({
                "bad": {**SAMPLE_PROFILES["dev-mock-low"], "usage_bucket": "UPPER"},
            })

    def test_missing_resource_dimension_rejected(self):
        broken = dict(SAMPLE_PROFILES["dev-mock-low"])
        broken["resources"] = {"monetary_cost": "low"}
        with self.assertRaises(ModelProfileConfigError):
            parse_model_profiles_document({"bad": broken})

    def test_openai_profile_requires_provider(self):
        with self.assertRaises(ModelProfileConfigError):
            parse_model_profiles_document({
                "api-only": {
                    "runtime": "openai_compatible",
                    "cost_class": "standard",
                    "usage_bucket": "batch",
                    "resources": SAMPLE_RESOURCES,
                },
            })


class ModelProfileResolutionTests(unittest.TestCase):
    def setUp(self):
        self.registry = model_profile_registry()

    def test_absent_means_no_selection(self):
        self.assertIsNone(normalize_model_profile(None))

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ModelProfileValidationError):
            normalize_model_profile("missing", registry=self.registry)

    def test_unconfigured_snapshot_is_explicit_unknown(self):
        snapshot = unconfigured_model_profile_snapshot()
        self.assertEqual(snapshot["resolution"], RESOLUTION_UNCONFIGURED)
        self.assertEqual(snapshot["cost_class"], "unknown")
        self.assertIsNone(snapshot["model_profile"])

    def test_compatible_profile_resolves(self):
        snapshot = resolve_model_profile_snapshot(
            runtime="mock",
            provider=None,
            effective_model=None,
            requested_profile="dev-mock-low",
            registry=self.registry,
        )
        self.assertEqual(snapshot["model_profile"], "dev-mock-low")
        self.assertEqual(snapshot["cost_class"], "low")

    def test_incompatible_runtime_rejected(self):
        rejection = evaluate_model_profile_preflight(
            runtime="codex",
            provider=None,
            effective_model=None,
            requested_profile="dev-mock-low",
            registry=self.registry,
        )
        self.assertEqual(rejection["reason"], "incompatible_model_profile")

    def test_public_projection_hides_binding_details(self):
        snapshot = resolve_model_profile_snapshot(
            runtime="mock",
            provider=None,
            effective_model=None,
            requested_profile="dev-mock-low",
            registry=self.registry,
        )
        projection = model_profile_public_projection(snapshot)
        assert projection is not None
        self.assertNotIn("runtime", projection)
        self.assertNotIn("provider", projection)
        self.assertNotIn("content_hash", projection)
        self.assertEqual(projection["cost_class"], "low")


class BrokerModelProfileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(
            self.home,
            profiles={"mock": MOCK_POLICY},
            model_profile_registry=model_profile_registry(),
        )

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_legacy_task_without_profile_is_unconfigured(self):
        record = self.broker.create(TaskRequest("work", str(self.workspace)))
        self.broker.start(record.id)
        projection = self.broker._read_model_profile_projection(record.id)
        assert projection is not None
        self.assertEqual(projection["resolution"], RESOLUTION_UNCONFIGURED)
        self.assertEqual(projection["cost_class"], "unknown")

    def test_profile_snapshot_persisted_at_start(self):
        record = self.broker.create(
            TaskRequest("work", str(self.workspace), model_profile="dev-mock-low"),
        )
        self.broker.start(record.id)
        path = self.broker.store.artifacts / record.id / "model_profile_resolution.json"
        self.assertTrue(path.is_file())
        snapshot = json.loads(path.read_text())
        self.assertEqual(snapshot["model_profile"], "dev-mock-low")

    def test_unknown_profile_rejected_at_create(self):
        with self.assertRaises(ValueError):
            self.broker.create(
                TaskRequest("work", str(self.workspace), model_profile="missing"),
            )

    def test_incompatible_profile_rejected_before_launch(self):
        record = self.broker.create(
            TaskRequest("work", str(self.workspace), runtime="mock", model_profile="dev-mock-low"),
        )
        # Force incompatible effective model after create by tampering request — broker re-reads request.
        # Instead use a profile with model pin that won't match mock default.
        registry = build_model_profile_registry(configured=parse_model_profiles_document({
            "pinned": {
                "runtime": "mock",
                "model": "not-the-default",
                "cost_class": "standard",
                "usage_bucket": "pinned",
                "resources": SAMPLE_RESOURCES,
            },
        }))
        broker = Broker(self.home, profiles={"mock": MOCK_POLICY}, model_profile_registry=registry)
        try:
            record = broker.create(TaskRequest("work", str(self.workspace), model_profile="pinned"))
            started = broker.start(record.id)
            self.assertEqual(started.state, TaskState.REJECTED)
            self.assertEqual(started.id, record.id)
            launch = broker.store.get_launch(record.id)
            self.assertIsNone(launch)
        finally:
            broker.close()

    def test_graph_role_does_not_inherit_parent_classification(self):
        parent = self.broker.create(TaskRequest("parent", str(self.workspace)))
        child = self.broker.create(
            TaskRequest(
                "child",
                str(self.workspace),
                parent_task_id=parent.id,
                model_profile="mock-premium-child",
            ),
        )
        self.broker.start(parent.id)
        self.broker.start(child.id)
        parent_projection = self.broker._read_model_profile_projection(parent.id)
        child_projection = self.broker._read_model_profile_projection(child.id)
        assert parent_projection is not None and child_projection is not None
        self.assertEqual(parent_projection["resolution"], RESOLUTION_UNCONFIGURED)
        self.assertEqual(child_projection["cost_class"], "premium")

    def test_request_artifact_persists_model_profile(self):
        record = self.broker.create(
            TaskRequest("work", str(self.workspace), model_profile="dev-mock-low"),
        )
        payload = json.loads(
            (self.broker.store.artifacts / record.id / "request.json").read_text(),
        )
        self.assertEqual(payload["model_profile"], "dev-mock-low")

    def test_normalized_and_concise_views_expose_safe_projection(self):
        record = self.broker.create(
            TaskRequest("work", str(self.workspace), model_profile="dev-mock-low"),
        )
        self.broker.start(record.id)
        finished = self.broker.complete(record.id, "done")
        projection = self.broker._read_model_profile_projection(record.id)
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
            model_profile_resource=projection,
        )
        self.assertEqual(
            envelope["broker_observed"]["model_profile_resource"]["cost_class"],
            "low",
        )
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertEqual(view["model_profile_resource"]["model_profile"], "dev-mock-low")
        self.assertNotIn("api_key", json.dumps(view))


class McpModelProfileTests(unittest.TestCase):
    def test_round_trip_model_profile(self):
        registry = model_profile_registry()
        request, _ = _build_task_request(
            {
                "task": "work",
                "workspace": "/repo",
                "runtime": "mock",
                "model_profile": "dev-mock-low",
            },
            model_profile_registry=registry,
        )
        self.assertEqual(request.model_profile, "dev-mock-low")


class ConfigExampleValidationTests(unittest.TestCase):
    def test_example_yaml_still_validates(self):
        from recollect_lines.providers import load_providers_config

        root = Path(__file__).resolve().parent.parent
        providers = load_providers_config(root / "config" / "providers.example.yaml")
        self.assertIn("local_gateway", providers)

    def test_schema_lists_cost_classes(self):
        schema = json.loads(
            (Path(__file__).resolve().parent.parent / "config" / "providers.schema.json").read_text(),
        )
        cost_enum = schema["$defs"]["model_profile"]["properties"]["cost_class"]["enum"]
        self.assertEqual(set(cost_enum), set(COST_CLASSES))


if __name__ == "__main__":
    unittest.main()
