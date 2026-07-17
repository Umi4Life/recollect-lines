"""verified-investigation-report strict result contract."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.claude_code_adapter import ClaudeCodeAdapter
from recollect_lines.codex_adapter import CodexAdapter
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.result_normalization import (
    NORMALIZED_RESULT_ARTIFACT,
    build_normalized_envelope,
    concise_normalized_view,
)
from recollect_lines.service import Broker
from recollect_lines.verified_investigation_report import (
    PROVENANCE_VALUES,
    RUNTIME_ALLOWED_PROVENANCE,
    VERIFIED_INVESTIGATION_REPORT_SCHEMA,
    normalize_evidence_id,
    sanitize_verified_source,
    validate_verified_investigation_report,
    verified_investigation_summary,
)

FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
FIXTURE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"
SCHEMA = VERIFIED_INVESTIGATION_REPORT_SCHEMA


def fake_codex_adapter(**kwargs):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE_CODEX)), grace_period_seconds=2.0, **kwargs)


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE_CLAUDE)), grace_period_seconds=2.0, **kwargs)


def denial(tool_name: str) -> dict:
    return {"tool_name": tool_name, "tool_use_id": f"tu_{tool_name}", "tool_input": {}}


def minimal_payload(**overrides) -> dict:
    payload = {
        "summary": "investigation complete with no supported findings",
        "findings": [],
        "evidence": [],
        "unverified_claims": [],
        "blocked_capabilities": [],
    }
    payload.update(overrides)
    return payload


def multi_finding_payload() -> dict:
    return {
        "summary": "login timeout root cause identified",
        "findings": [
            {"claim": "handler blocks on DNS lookup", "confidence": 0.85, "evidence_refs": ["ev-log"]},
            {"claim": "retry loop is unbounded", "confidence": 0.7, "evidence_refs": ["ev-code", "ev-log"]},
        ],
        "evidence": [
            {
                "id": "ev-log",
                "provenance": "runtime_reported",
                "source_type": "log_file",
                "source": "logs/auth-service.log",
                "claim_supported": "repeated timeout entries at 12:04",
            },
            {
                "id": "ev-code",
                "provenance": "orchestrator_supplied",
                "source_type": "file_path",
                "source": "src/auth/handler.py:42",
                "claim_supported": "while-loop without max attempts",
            },
        ],
        "unverified_claims": ["external DNS latency spike"],
        "blocked_capabilities": ["repository.remote.read"],
    }


class VerifiedInvestigationReportUnitTests(unittest.TestCase):
    def test_minimal_valid_report(self):
        ok, warnings, normalized = validate_verified_investigation_report(minimal_payload())
        self.assertTrue(ok)
        self.assertEqual(warnings, [])
        assert normalized is not None
        self.assertEqual(normalized["findings"], [])
        self.assertEqual(normalized["evidence"], [])

    def test_multi_finding_valid_report(self):
        ok, _, normalized = validate_verified_investigation_report(multi_finding_payload())
        self.assertTrue(ok)
        assert normalized is not None
        self.assertEqual(len(normalized["findings"]), 2)
        self.assertEqual(len(normalized["evidence"]), 2)
        self.assertEqual(normalized["blocked_capabilities"], ["repository.remote.read"])

    def test_runtime_allowed_provenance_values(self):
        for provenance in sorted(RUNTIME_ALLOWED_PROVENANCE):
            with self.subTest(provenance=provenance):
                payload = minimal_payload(findings=[{
                    "claim": "sample claim",
                    "confidence": 0.5,
                    "evidence_refs": ["ev-1"],
                }], evidence=[{
                    "id": "ev-1",
                    "provenance": provenance,
                    "source_type": "note",
                    "source": "operator brief section 2",
                    "claim_supported": "context supplied at launch",
                }])
                ok, _, _ = validate_verified_investigation_report(payload)
                self.assertTrue(ok)

    def test_broker_provenance_labels_are_rejected_in_runtime_output(self):
        for provenance in ("broker_observed", "broker_verified"):
            with self.subTest(provenance=provenance):
                payload = minimal_payload(findings=[{
                    "claim": "broker claim",
                    "confidence": 0.9,
                    "evidence_refs": ["ev-1"],
                }], evidence=[{
                    "id": "ev-1",
                    "provenance": provenance,
                    "source_type": "note",
                    "source": "broker artifact",
                    "claim_supported": "verified externally",
                }])
                ok, warnings, _ = validate_verified_investigation_report(payload)
                self.assertFalse(ok)
                self.assertTrue(any("reserved for broker-assigned" in w for w in warnings))

    def test_unknown_provenance_fails(self):
        payload = minimal_payload(findings=[{
            "claim": "x",
            "confidence": 0.5,
            "evidence_refs": ["ev-1"],
        }], evidence=[{
            "id": "ev-1",
            "provenance": "agent_guessed",
            "source_type": "note",
            "source": "somewhere",
            "claim_supported": "maybe",
        }])
        ok, warnings, _ = validate_verified_investigation_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any("provenance must be one of" in w for w in warnings))

    def test_duplicate_evidence_id_fails(self):
        payload = multi_finding_payload()
        payload["evidence"].append(dict(payload["evidence"][0]))
        ok, warnings, _ = validate_verified_investigation_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any("duplicate evidence id" in w for w in warnings))

    def test_missing_evidence_id_fails(self):
        payload = multi_finding_payload()
        payload["evidence"][0].pop("id")
        ok, warnings, _ = validate_verified_investigation_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any(".id is missing" in w for w in warnings))

    def test_dangling_evidence_reference_fails(self):
        payload = multi_finding_payload()
        payload["findings"][0]["evidence_refs"] = ["ev-missing"]
        ok, warnings, _ = validate_verified_investigation_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any("unknown evidence id" in w for w in warnings))

    def test_malformed_confidence_fails(self):
        for bad in ("high", None, True, -0.1, 1.1):
            with self.subTest(confidence=bad):
                payload = multi_finding_payload()
                payload["findings"][0]["confidence"] = bad
                ok, warnings, _ = validate_verified_investigation_report(payload)
                self.assertFalse(ok)

    def test_required_field_failures(self):
        payload = multi_finding_payload()
        del payload["blocked_capabilities"]
        ok, warnings, _ = validate_verified_investigation_report(payload)
        self.assertFalse(ok)
        self.assertTrue(any("blocked_capabilities" in w for w in warnings))

    def test_evidence_id_is_normalized_to_lowercase(self):
        payload = minimal_payload(findings=[{
            "claim": "claim",
            "confidence": 0.4,
            "evidence_refs": ["EV-1"],
        }], evidence=[{
            "id": "EV-1",
            "provenance": "unresolved",
            "source_type": "note",
            "source": "unknown origin",
            "claim_supported": "limited context",
        }])
        ok, _, normalized = validate_verified_investigation_report(payload)
        self.assertTrue(ok)
        assert normalized is not None
        self.assertEqual(normalized["evidence"][0]["id"], "ev-1")
        self.assertEqual(normalized["findings"][0]["evidence_refs"], ["ev-1"])

    def test_safe_source_redacts_secrets(self):
        source, error = sanitize_verified_source("config at sk-ant-abcdefgh12345678")
        self.assertIsNone(error)
        assert source is not None
        self.assertIn("***REDACTED***", source)
        self.assertNotIn("sk-ant-abcdefgh12345678", source)

    def test_safe_source_rejects_multiline_and_raw_json(self):
        for bad in ("line one\nline two", '{"stdout": "full output"}', "[]"):
            with self.subTest(source=bad):
                _, error = sanitize_verified_source(bad)
                self.assertIsNotNone(error)

    def test_provenance_vocabulary_is_closed(self):
        self.assertEqual(
            PROVENANCE_VALUES,
            frozenset({
                "orchestrator_supplied",
                "runtime_reported",
                "broker_observed",
                "broker_verified",
                "unresolved",
            }),
        )

    def test_summary_projection_is_count_only(self):
        ok, _, normalized = validate_verified_investigation_report(multi_finding_payload())
        assert normalized is not None
        summary = verified_investigation_summary(contract_status="satisfied", payload=normalized)
        encoded = json.dumps(summary)
        self.assertNotIn("handler blocks", encoded)
        self.assertNotIn("logs/auth-service.log", encoded)
        self.assertEqual(summary["findings_count"], 2)
        self.assertEqual(summary["evidence_count"], 2)
        self.assertEqual(summary["provenance_counts"]["runtime_reported"], 1)
        self.assertEqual(summary["provenance_counts"]["orchestrator_supplied"], 1)
        self.assertEqual(summary["unverified_claims_count"], 1)
        self.assertEqual(summary["blocked_capabilities_count"], 1)

    def test_normalize_evidence_id_rules(self):
        self.assertEqual(normalize_evidence_id("Ev-1"), "ev-1")
        self.assertIsNone(normalize_evidence_id(""))
        self.assertIsNone(normalize_evidence_id("1bad"))


class BrokerVerifiedInvestigationIntegrationTests(unittest.TestCase):
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

    def test_valid_minimal_and_multi_reports_satisfy_contract(self):
        for payload in (minimal_payload(), multi_finding_payload()):
            with self.subTest(summary=payload["summary"]):
                task_id = self._collect_codex(payload)
                envelope = self._normalized(task_id)
                self.assertEqual(envelope["parser"]["requested_schema"], SCHEMA)
                self.assertEqual(envelope["parser"]["parse_status"], "ok")
                self.assertEqual(envelope["parser"]["contract_status"], "satisfied")
                self.assertIn("verified_investigation", envelope["runtime_reported"])

    def test_invalid_report_is_unsatisfied_malformed(self):
        payload = multi_finding_payload()
        payload["findings"][0]["evidence_refs"] = ["ev-missing"]
        task_id = self._collect_codex(payload)
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["parser"]["contract_status"], "unsatisfied_malformed")
        self.assertNotIn("verified_investigation", envelope["runtime_reported"])

    def test_legacy_evidence_report_contract_unchanged(self):
        payload = {
            "summary": "evidence gathered",
            "findings": [{"id": "f1", "detail": "race in handler"}],
            "claimed_evidence": ["logs/trace.txt"],
        }
        record = self.broker.create(TaskRequest(
            f"SCHEMA_evidence-report {json.dumps(payload)}",
            str(self.workspace),
            runtime="codex",
            result_schema="evidence-report",
            explicit_fields=frozenset({"result_schema"}),
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["parser"]["requested_schema"], "evidence-report")
        self.assertEqual(envelope["parser"]["contract_status"], "satisfied")
        self.assertNotIn("verified_investigation", envelope["runtime_reported"])

    def test_concise_and_completion_views_exclude_raw_evidence(self):
        task_id = self._collect_codex(multi_finding_payload())
        envelope = self._normalized(task_id)
        view = concise_normalized_view(envelope)
        assert view is not None
        summary = view["verified_investigation_summary"]
        encoded = json.dumps(view)
        self.assertNotIn("handler blocks", encoded)
        self.assertNotIn("logs/auth-service.log", encoded)
        self.assertEqual(summary["contract"], SCHEMA)
        self.assertEqual(summary["findings_count"], 2)

        page = self.broker.completion_events_since(0, task_id=task_id)
        event_summary = page["events"][0]["result_summary"]
        self.assertEqual(
            event_summary["verified_investigation_summary"],
            view["verified_investigation_summary"],
        )
        self.assertNotIn("handler blocks", json.dumps(event_summary))

    def test_blocked_capabilities_do_not_conflate_with_capability_observations(self):
        task_id = self._collect_codex(multi_finding_payload())
        record = self.broker.store.get(task_id)
        envelope = build_normalized_envelope(
            record=record,
            result={},
            collected={
                "adapter": "claude_code",
                "exit_code": 0,
                "summary": json.dumps(multi_finding_payload(), separators=(",", ":")),
                "permission_denials": [denial("Read")],
            },
            gate={"policy": "none", "outcome": "passed"},
            verification=None,
            manifest={"files": []},
            launch=None,
            raw_output_artifact=None,
            final_state=TaskState.SUCCEEDED,
        )
        self.assertIn("capability_observations", envelope["runtime_reported"])
        self.assertEqual(
            envelope["runtime_reported"]["verified_investigation"]["blocked_capabilities"],
            ["repository.remote.read"],
        )
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertTrue(view["has_capability_warning"])
        self.assertEqual(view["verified_investigation_summary"]["blocked_capabilities_count"], 1)
        self.assertNotIn("Read", json.dumps(view["verified_investigation_summary"]))


if __name__ == "__main__":
    unittest.main()
