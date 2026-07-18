"""review-report bounded review result contract."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.adaptor.claude_code import ClaudeCodeAdapter
from recollect_lines.adaptor.codex import CodexAdapter
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.result_normalization import (
    NORMALIZED_RESULT_ARTIFACT,
    build_normalized_envelope,
    concise_normalized_view,
)
from recollect_lines.review_report import (
    MAX_REVIEW_FINDINGS,
    MAX_REVIEWED_ARTIFACTS,
    REVIEW_REPORT_SCHEMA,
    REVIEW_STATUS_VALUES,
    REVIEWED_ARTIFACT_CATEGORIES,
    review_summary,
    validate_review_report,
)
from recollect_lines.service import Broker
from recollect_lines.verified_investigation_report import VERIFIED_INVESTIGATION_REPORT_SCHEMA

FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
FIXTURE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"
SCHEMA = REVIEW_REPORT_SCHEMA


def fake_codex_adapter(**kwargs):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE_CODEX)), grace_period_seconds=2.0, **kwargs)


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE_CLAUDE)), grace_period_seconds=2.0, **kwargs)


def denial(tool_name: str) -> dict:
    return {"tool_name": tool_name, "tool_use_id": f"tu_{tool_name}", "tool_input": {}}


def minimal_payload(**overrides) -> dict:
    payload = {
        "summary": "bounded review of the supplied diff and test output",
        "review_status": "passed",
        "review_findings": [],
        "reviewed_artifacts": [],
        "full_reexecution_performed": False,
    }
    payload.update(overrides)
    return payload


def complete_payload(**overrides) -> dict:
    payload = {
        "summary": "diff addresses the race but lacks a regression test",
        "review_status": "needs_changes",
        "review_findings": [
            {"finding": "no test covers the new lock ordering", "severity": "major"},
            {"finding": "docstring is stale", "severity": "minor"},
        ],
        "reviewed_artifacts": [
            {"category": "diff", "reference": "worker task tsk_abc123 diff"},
            {"category": "verification_output", "reference": "pytest -q summary, tsk_abc123"},
        ],
        "full_reexecution_performed": False,
    }
    payload.update(overrides)
    return payload


class ReviewReportUnitTests(unittest.TestCase):
    def test_minimal_valid_report(self):
        ok, warnings, normalized = validate_review_report(minimal_payload())
        self.assertTrue(ok)
        self.assertEqual(warnings, [])
        assert normalized is not None
        self.assertEqual(normalized["review_findings"], [])
        self.assertEqual(normalized["reviewed_artifacts"], [])
        self.assertIs(normalized["full_reexecution_performed"], False)

    def test_complete_valid_report(self):
        ok, _, normalized = validate_review_report(complete_payload())
        self.assertTrue(ok)
        assert normalized is not None
        self.assertEqual(len(normalized["review_findings"]), 2)
        self.assertEqual(len(normalized["reviewed_artifacts"]), 2)
        self.assertEqual(normalized["review_findings"][0]["severity"], "major")

    def test_each_review_status_is_valid(self):
        for status in sorted(REVIEW_STATUS_VALUES):
            with self.subTest(status=status):
                ok, _, normalized = validate_review_report(minimal_payload(review_status=status))
                self.assertTrue(ok)
                assert normalized is not None
                self.assertEqual(normalized["review_status"], status)

    def test_review_status_vocabulary_is_closed(self):
        self.assertEqual(REVIEW_STATUS_VALUES, frozenset({"passed", "needs_changes", "blocked"}))

    def test_unknown_review_status_fails(self):
        ok, warnings, _ = validate_review_report(minimal_payload(review_status="looks_fine"))
        self.assertFalse(ok)
        self.assertTrue(any("review_status must be one of" in w for w in warnings))

    def test_missing_required_fields_fail(self):
        for field in ("summary", "review_status", "review_findings", "reviewed_artifacts", "full_reexecution_performed"):
            with self.subTest(field=field):
                payload = minimal_payload()
                del payload[field]
                ok, _, _ = validate_review_report(payload)
                self.assertFalse(ok)

    def test_full_reexecution_performed_must_be_boolean(self):
        for bad in ("true", 1, None, "yes"):
            with self.subTest(value=bad):
                ok, warnings, _ = validate_review_report(minimal_payload(full_reexecution_performed=bad))
                self.assertFalse(ok)
                self.assertTrue(any("full_reexecution_performed must be a boolean" in w for w in warnings))

    def test_full_reexecution_performed_true_is_accepted(self):
        ok, _, normalized = validate_review_report(minimal_payload(full_reexecution_performed=True))
        self.assertTrue(ok)
        assert normalized is not None
        self.assertIs(normalized["full_reexecution_performed"], True)

    def test_review_findings_must_be_bounded(self):
        payload = minimal_payload(review_findings=[
            {"finding": f"finding {i}"} for i in range(MAX_REVIEW_FINDINGS + 1)
        ])
        ok, warnings, _ = validate_review_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any("review_findings exceeds" in w for w in warnings))

    def test_review_findings_at_bound_is_accepted(self):
        payload = minimal_payload(review_findings=[
            {"finding": f"finding {i}"} for i in range(MAX_REVIEW_FINDINGS)
        ])
        ok, _, normalized = validate_review_report(payload)
        self.assertTrue(ok)
        assert normalized is not None
        self.assertEqual(len(normalized["review_findings"]), MAX_REVIEW_FINDINGS)

    def test_reviewed_artifacts_must_be_bounded(self):
        payload = minimal_payload(reviewed_artifacts=[
            {"category": "other", "reference": f"artifact {i}"} for i in range(MAX_REVIEWED_ARTIFACTS + 1)
        ])
        ok, warnings, _ = validate_review_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any("reviewed_artifacts exceeds" in w for w in warnings))

    def test_finding_requires_non_empty_text(self):
        for bad in ("", "   ", None, 123):
            with self.subTest(finding=bad):
                payload = minimal_payload(review_findings=[{"finding": bad}])
                ok, _, _ = validate_review_report(payload)
                self.assertFalse(ok)

    def test_finding_severity_defaults_to_info(self):
        ok, _, normalized = validate_review_report(minimal_payload(review_findings=[{"finding": "note"}]))
        self.assertTrue(ok)
        assert normalized is not None
        self.assertEqual(normalized["review_findings"][0]["severity"], "info")

    def test_finding_severity_must_be_closed_vocabulary(self):
        payload = minimal_payload(review_findings=[{"finding": "note", "severity": "catastrophic"}])
        ok, warnings, _ = validate_review_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any("severity must be one of" in w for w in warnings))

    def test_reviewed_artifact_category_must_be_closed_vocabulary(self):
        self.assertEqual(
            REVIEWED_ARTIFACT_CATEGORIES,
            frozenset({"diff", "test_result", "normalized_result", "verification_output", "task_summary", "other"}),
        )
        payload = minimal_payload(reviewed_artifacts=[{"category": "raw_stdout_dump", "reference": "x"}])
        ok, warnings, _ = validate_review_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any("category must be one of" in w for w in warnings))

    def test_unsafe_reviewed_artifact_reference_rejected(self):
        for bad in ("line one\nline two", '{"stdout": "full output"}', "[]", "x" * 600):
            with self.subTest(reference=bad):
                payload = minimal_payload(reviewed_artifacts=[{"category": "other", "reference": bad}])
                ok, warnings, _ = validate_review_report(payload)
                self.assertFalse(ok)
                self.assertTrue(any("reference" in w for w in warnings))

    def test_reviewed_artifact_reference_secrets_are_redacted(self):
        payload = minimal_payload(reviewed_artifacts=[
            {"category": "other", "reference": "config at sk-ant-abcdefgh12345678"}
        ])
        ok, _, normalized = validate_review_report(payload)
        self.assertTrue(ok)
        assert normalized is not None
        reference = normalized["reviewed_artifacts"][0]["reference"]
        self.assertIn("***REDACTED***", reference)
        self.assertNotIn("sk-ant-abcdefgh12345678", reference)

    def test_review_summary_projection_is_count_only(self):
        ok, _, normalized = validate_review_report(complete_payload())
        assert normalized is not None
        summary = review_summary(contract_status="satisfied", payload=normalized)
        encoded = json.dumps(summary)
        self.assertNotIn("lock ordering", encoded)
        self.assertNotIn("tsk_abc123", encoded)
        self.assertEqual(summary["contract"], SCHEMA)
        self.assertEqual(summary["review_status"], "needs_changes")
        self.assertEqual(summary["finding_count"], 2)
        self.assertEqual(summary["reviewed_artifact_category_counts"], {"diff": 1, "verification_output": 1})
        self.assertIs(summary["full_reexecution_performed"], False)

    def test_review_summary_with_no_payload_reports_neutral_defaults(self):
        summary = review_summary(contract_status="unavailable", payload=None)
        self.assertEqual(summary["review_status"], None)
        self.assertEqual(summary["finding_count"], 0)
        self.assertEqual(summary["reviewed_artifact_category_counts"], {})
        self.assertIsNone(summary["full_reexecution_performed"])


class BrokerReviewReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, codex_adapter=fake_codex_adapter(), claude_code_adapter=fake_claude_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _collect_codex(self, payload: dict) -> str:
        record = self.broker.create(TaskRequest(
            f"SCHEMA_{SCHEMA} {json.dumps(payload, separators=(',', ':'))}",
            str(self.workspace),
            runtime="codex",
            result_schema=SCHEMA,
            explicit_fields=frozenset({"result_schema"}),
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        return record.id

    def _normalized(self, task_id: str) -> dict:
        path = self.broker.store.artifacts / task_id / NORMALIZED_RESULT_ARTIFACT
        return json.loads(path.read_text())

    def test_valid_minimal_and_complete_reports_satisfy_contract(self):
        for payload in (minimal_payload(), complete_payload()):
            with self.subTest(summary=payload["summary"]):
                task_id = self._collect_codex(payload)
                envelope = self._normalized(task_id)
                self.assertEqual(envelope["parser"]["requested_schema"], SCHEMA)
                self.assertEqual(envelope["parser"]["parse_status"], "ok")
                self.assertEqual(envelope["parser"]["contract_status"], "satisfied")
                self.assertIn("review_report", envelope["runtime_reported"])
                self.assertNotIn("verified_investigation", envelope["runtime_reported"])

    def test_invalid_report_is_unsatisfied_malformed(self):
        payload = complete_payload()
        payload["reviewed_artifacts"][0]["category"] = "raw_dump"
        task_id = self._collect_codex(payload)
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["parser"]["contract_status"], "unsatisfied_malformed")
        self.assertNotIn("review_report", envelope["runtime_reported"])

    def test_legacy_review_findings_contract_unchanged(self):
        payload = {
            "summary": "architecture review complete",
            "findings": [{"severity": "medium", "topic": "coupling"}],
        }
        record = self.broker.create(TaskRequest(
            f"SCHEMA_review-findings {json.dumps(payload)}",
            str(self.workspace),
            runtime="codex",
            result_schema="review-findings",
            explicit_fields=frozenset({"result_schema"}),
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["parser"]["requested_schema"], "review-findings")
        self.assertEqual(envelope["parser"]["contract_status"], "satisfied")
        self.assertNotIn("review_report", envelope["runtime_reported"])
        self.assertNotIn("review_summary", concise_normalized_view(envelope))

    def test_concise_and_completion_views_exclude_raw_findings_and_references(self):
        task_id = self._collect_codex(complete_payload())
        envelope = self._normalized(task_id)
        view = concise_normalized_view(envelope)
        assert view is not None
        summary = view["review_summary"]
        encoded = json.dumps(view)
        self.assertNotIn("lock ordering", encoded)
        self.assertNotIn("tsk_abc123", encoded)
        self.assertEqual(summary["contract"], SCHEMA)
        self.assertEqual(summary["review_status"], "needs_changes")
        self.assertEqual(summary["finding_count"], 2)

        page = self.broker.completion_events_since(0, task_id=task_id)
        event_summary = page["events"][0]["result_summary"]
        self.assertEqual(event_summary["review_summary"], view["review_summary"])
        self.assertNotIn("lock ordering", json.dumps(event_summary))

        status = self.broker.status(task_id)
        self.assertEqual(status["normalized_result"]["review_summary"], view["review_summary"])

    def test_full_reexecution_flag_is_surfaced_and_never_implies_cost_saved(self):
        for flag in (False, True):
            with self.subTest(full_reexecution_performed=flag):
                task_id = self._collect_codex(minimal_payload(full_reexecution_performed=flag))
                envelope = self._normalized(task_id)
                self.assertIs(
                    envelope["runtime_reported"]["review_report"]["full_reexecution_performed"], flag
                )
                view = concise_normalized_view(envelope)
                assert view is not None
                self.assertIs(view["review_summary"]["full_reexecution_performed"], flag)
                # The flag is the only fact recorded; it never changes cost/usage projections.
                self.assertEqual(view["cost_policy_status"]["resolution"], "unconfigured")
                self.assertNotIn("full_reexecution_performed", view["cost_policy_status"])

    def test_review_report_does_not_conflate_with_capability_or_cost_fields(self):
        task_id = self._collect_codex(complete_payload())
        record = self.broker.store.get(task_id)
        envelope = build_normalized_envelope(
            record=record,
            result={},
            collected={
                "adapter": "claude_code",
                "exit_code": 0,
                "summary": json.dumps(complete_payload(), separators=(",", ":")),
                "permission_denials": [denial("Read")],
            },
            gate={"policy": "none", "outcome": "passed"},
            verification=None,
            manifest={"files": []},
            launch=None,
            raw_output_artifact=None,
            final_state=TaskState.SUCCEEDED,
            required_capabilities=("workspace.read",),
            cost_policy_status={
                "resolution": "policy_selected",
                "policy_id": "strict-review",
                "preflight_status": "accepted",
            },
        )
        self.assertIn("capability_observations", envelope["runtime_reported"])
        self.assertIn("review_report", envelope["runtime_reported"])
        self.assertNotIn("verified_investigation", envelope["runtime_reported"])

        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertIn("review_summary", view)
        self.assertIn("capability_contract", view)
        self.assertIn("cost_policy_status", view)
        self.assertNotIn("verified_investigation_summary", view)
        # Distinct dimensions: none of the sibling projections leak into review_summary.
        self.assertNotIn("policy_id", view["review_summary"])
        self.assertNotIn("required_capabilities", view["review_summary"])
        self.assertNotIn("review_status", view["cost_policy_status"])
        self.assertNotIn("review_status", view["capability_contract"])
        # review findings never rewrite lifecycle state.
        self.assertEqual(envelope["state"], TaskState.SUCCEEDED.value)

    def test_review_report_is_distinct_from_verified_investigation_report(self):
        self.assertNotEqual(SCHEMA, VERIFIED_INVESTIGATION_REPORT_SCHEMA)
        task_id = self._collect_codex(complete_payload())
        envelope = self._normalized(task_id)
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertIn("review_summary", view)
        self.assertNotIn("verified_investigation_summary", view)


if __name__ == "__main__":
    unittest.main()
