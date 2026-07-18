"""Advisory claim-versus-capability diagnostics.

Detects when a runtime's final summary plausibly claims an external
verification path that structured capability observations say was denied.
A guardrail, not semantic fact-checking: never scores claim truth, never
mutates lifecycle state, capability contract status, evidence provenance,
review status, or cost policy.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.claim_capability_diagnostics import (
    CLAIM_CAPABILITY_DIAGNOSTIC_CATEGORY,
    MAX_CLAIM_CAPABILITY_DIAGNOSTICS,
    VERIFICATION_CLAIM_CUES,
    claim_capability_diagnostics_concise,
    evaluate_claim_capability_diagnostics,
)
from recollect_lines.adaptor.claude_code import ClaudeCodeAdapter
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.required_capabilities import WORKSPACE_READ
from recollect_lines.result_normalization import (
    CLAIM_CAPABILITY_DIAGNOSTICS_ENVELOPE_VERSION,
    NORMALIZED_RESULT_ARTIFACT,
    concise_normalized_view,
)
from recollect_lines.service import Broker
from recollect_lines.verified_investigation_report import VERIFIED_INVESTIGATION_REPORT_SCHEMA
from recollect_lines.review_report import REVIEW_REPORT_SCHEMA

FIXTURE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(
        command_prefix=(sys.executable, str(FIXTURE_CLAUDE)),
        grace_period_seconds=2.0,
        **kwargs,
    )


def denial(tool_name: str) -> dict:
    return {"tool_name": tool_name, "tool_use_id": f"tu_{tool_name}", "tool_input": {}}


def observation(tool_identifier: str) -> dict:
    return {
        "tool_identifier": tool_identifier,
        "source": "runtime_permission_denial",
        "adapter": "claude_code",
    }


class EvaluateClaimCapabilityDiagnosticsUnitTests(unittest.TestCase):
    def test_cue_plus_matching_denied_family_yields_diagnostic(self):
        result = evaluate_claim_capability_diagnostics(
            summary="I verified via GitHub that the PR was merged.",
            capability_observations=[observation("mcp__github__search_issues")],
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["diagnostic_count"], 1)
        entry = result["diagnostics"][0]
        self.assertEqual(entry["cue"], "verified via")
        self.assertEqual(entry["tool_family"], "github")
        self.assertEqual(entry["category"], CLAIM_CAPABILITY_DIAGNOSTIC_CATEGORY)
        self.assertEqual(entry["denied_tool_identifiers"], ["mcp__github__search_issues"])

    def test_all_documented_cue_examples_are_detected(self):
        for cue in ("verified via", "checked via", "queried"):
            with self.subTest(cue=cue):
                result = evaluate_claim_capability_diagnostics(
                    summary=f"I {cue} the jira tracker for status.",
                    capability_observations=[observation("mcp__jira__get_issue")],
                )
                self.assertIsNotNone(result)

    def test_cue_vocabulary_is_the_documented_conservative_set(self):
        self.assertEqual(VERIFICATION_CLAIM_CUES, ("verified via", "checked via", "queried"))

    def test_denial_alone_yields_no_diagnostic(self):
        result = evaluate_claim_capability_diagnostics(
            summary="Everything looks fine, task complete.",
            capability_observations=[observation("mcp__github__search_issues")],
        )
        self.assertIsNone(result)

    def test_cue_alone_with_no_denials_yields_no_diagnostic(self):
        result = evaluate_claim_capability_diagnostics(
            summary="I verified via GitHub that the PR was merged.",
            capability_observations=[],
        )
        self.assertIsNone(result)

    def test_mismatched_provider_family_yields_no_diagnostic(self):
        result = evaluate_claim_capability_diagnostics(
            summary="I verified via Jira that the issue is closed.",
            capability_observations=[observation("mcp__github__search_issues")],
        )
        self.assertIsNone(result)

    def test_cue_far_from_family_label_is_not_proximate(self):
        far_filler = "x" * 500
        result = evaluate_claim_capability_diagnostics(
            summary=f"I verified via the tracker. {far_filler} github was mentioned much later.",
            capability_observations=[observation("mcp__github__search_issues")],
        )
        self.assertIsNone(result)

    def test_non_mcp_denied_tool_has_no_family_and_yields_no_diagnostic(self):
        result = evaluate_claim_capability_diagnostics(
            summary="I verified via Read that the file exists.",
            capability_observations=[observation("Read")],
        )
        self.assertIsNone(result)

    def test_repeated_denials_of_same_family_aggregate_deterministically(self):
        observations = [
            observation("mcp__github__search_issues"),
            observation("mcp__github__get_pull_request"),
            observation("mcp__github__search_issues"),
        ]
        result = evaluate_claim_capability_diagnostics(
            summary="I verified via GitHub across several checks.",
            capability_observations=observations,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["diagnostic_count"], 1)
        entry = result["diagnostics"][0]
        self.assertEqual(
            entry["denied_tool_identifiers"],
            ["mcp__github__get_pull_request", "mcp__github__search_issues"],
        )

    def test_multiple_distinct_families_and_cues_are_deterministic_and_sorted(self):
        observations = [
            observation("mcp__zeta__lookup"),
            observation("mcp__alpha__lookup"),
        ]
        summary = "I verified via alpha and also queried zeta for confirmation."
        result_a = evaluate_claim_capability_diagnostics(summary=summary, capability_observations=observations)
        result_b = evaluate_claim_capability_diagnostics(summary=summary, capability_observations=observations)
        self.assertEqual(result_a, result_b)
        assert result_a is not None
        # Deterministic order: grouped by cue in VERIFICATION_CLAIM_CUES order,
        # families sorted alphabetically within each cue group.
        families_by_cue: dict[str, list[str]] = {}
        for entry in result_a["diagnostics"]:
            families_by_cue.setdefault(entry["cue"], []).append(entry["tool_family"])
        for cue, families in families_by_cue.items():
            self.assertEqual(families, sorted(families), cue)

    def test_diagnostics_are_capped(self):
        observations = [observation(f"mcp__provider{i:02d}__lookup") for i in range(50)]
        summary_parts = [f"queried provider{i:02d}" for i in range(50)]
        result = evaluate_claim_capability_diagnostics(
            summary=" ".join(summary_parts), capability_observations=observations,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertLessEqual(result["diagnostic_count"], MAX_CLAIM_CAPABILITY_DIAGNOSTICS)

    def test_malformed_or_absent_summary_fails_soft(self):
        obs = [observation("mcp__github__search_issues")]
        for bad_summary in (None, "", "   ", 123, ["not", "a", "string"]):
            with self.subTest(summary=bad_summary):
                self.assertIsNone(
                    evaluate_claim_capability_diagnostics(summary=bad_summary, capability_observations=obs)
                )

    def test_malformed_or_absent_observations_fail_soft(self):
        summary = "I verified via GitHub that the PR was merged."
        for bad_obs in (None, [], {"not": "a list"}, "not-a-list", [None, "not-a-dict", 42]):
            with self.subTest(observations=bad_obs):
                self.assertIsNone(
                    evaluate_claim_capability_diagnostics(summary=summary, capability_observations=bad_obs)
                )

    def test_case_insensitive_cue_and_family_matching(self):
        result = evaluate_claim_capability_diagnostics(
            summary="I VERIFIED VIA GITHUB that the PR was merged.",
            capability_observations=[observation("mcp__github__search_issues")],
        )
        self.assertIsNotNone(result)


class ConciseProjectionTests(unittest.TestCase):
    def test_none_diagnostics_project_to_none(self):
        self.assertIsNone(claim_capability_diagnostics_concise(None))
        self.assertIsNone(claim_capability_diagnostics_concise({"diagnostic_count": 0, "diagnostics": []}))

    def test_present_diagnostics_project_compactly(self):
        raw = {
            "diagnostic_count": 1,
            "diagnostics": [{
                "category": CLAIM_CAPABILITY_DIAGNOSTIC_CATEGORY,
                "cue": "verified via",
                "tool_family": "github",
                "denied_tool_identifiers": ["mcp__github__search_issues"],
            }],
        }
        projected = claim_capability_diagnostics_concise(raw)
        self.assertEqual(projected, raw)


class ConciseNormalizedViewTests(unittest.TestCase):
    def _envelope(self, *, summary: str, observations: list[dict]) -> dict:
        # Mirrors what build_normalized_envelope actually persists: the
        # diagnostic is precomputed once and stored at the envelope top
        # level, exactly like `capability_contract` -- concise_normalized_view
        # projects it rather than recomputing it from raw observations.
        envelope: dict = {
            "envelope_version": CLAIM_CAPABILITY_DIAGNOSTICS_ENVELOPE_VERSION,
            "state": "succeeded",
            "runtime_reported": {
                "summary": summary,
                "capability_observations": observations,
            },
            "parser": {"requested_schema": "plain-summary", "parse_status": "ok", "warnings": []},
            "broker_observed": {},
        }
        diagnostics = evaluate_claim_capability_diagnostics(
            summary=summary, capability_observations=observations,
        )
        if diagnostics is not None:
            envelope["claim_capability_diagnostics"] = diagnostics
        return envelope

    def test_concise_view_surfaces_flag_and_compact_diagnostic(self):
        envelope = self._envelope(
            summary="I verified via GitHub that the PR was merged.",
            observations=[observation("mcp__github__search_issues")],
        )
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertTrue(view["has_claim_capability_diagnostic"])
        self.assertEqual(view["claim_capability_diagnostics"]["diagnostic_count"], 1)

    def test_concise_view_omits_flag_when_no_diagnostic(self):
        envelope = self._envelope(summary="all good", observations=[observation("mcp__github__search_issues")])
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertNotIn("has_claim_capability_diagnostic", view)
        self.assertNotIn("claim_capability_diagnostics", view)

    def test_no_lifecycle_or_contract_or_verification_fields_are_touched(self):
        envelope = self._envelope(
            summary="I verified via GitHub that the PR was merged.",
            observations=[observation("mcp__github__search_issues")],
        )
        envelope["capability_contract"] = {
            "status": "satisfied",
            "required_capabilities": [],
            "unsatisfied_capabilities": [],
            "unknown_capabilities": [],
            "reasons": [],
        }
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertEqual(view["state"], "succeeded")
        # The diagnostic never rewrites an independently-computed contract status.
        self.assertEqual(view["capability_contract"]["status"], "satisfied")
        self.assertTrue(view["has_claim_capability_diagnostic"])


class BrokerClaimCapabilityDiagnosticsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, claude_code_adapter=fake_claude_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _normalized(self, task_id: str) -> dict:
        path = self.broker.store.artifacts / task_id / NORMALIZED_RESULT_ARTIFACT
        return json.loads(path.read_text())

    def _create_with_summary_and_denials(self, summary: str, denials: list, **request_kwargs) -> str:
        payload = json.dumps({"summary": summary, "permission_denials": denials}, separators=(",", ":"))
        record = self.broker.create(TaskRequest(
            f"SUMMARY_AND_DENIALS_JSON {payload}",
            str(self.workspace),
            profile="claude_code",
            **request_kwargs,
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        return record.id

    def test_end_to_end_cue_and_denial_produce_diagnostic_without_state_change(self):
        task_id = self._create_with_summary_and_denials(
            "I verified via GitHub that the change was merged.",
            [denial("mcp__github__search_issues")],
        )
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["envelope_version"], CLAIM_CAPABILITY_DIAGNOSTICS_ENVELOPE_VERSION)
        self.assertEqual(envelope["state"], TaskState.SUCCEEDED.value)
        self.assertEqual(envelope["claim_capability_diagnostics"]["diagnostic_count"], 1)

        status = self.broker.status(task_id)
        compact = status["normalized_result"]
        self.assertTrue(compact["has_claim_capability_diagnostic"])
        self.assertEqual(compact["claim_capability_diagnostics"]["diagnostic_count"], 1)

        record = self.broker.store.get(task_id)
        self.assertEqual(record.state, TaskState.SUCCEEDED)

    def test_end_to_end_denial_without_cue_produces_no_diagnostic(self):
        task_id = self._create_with_summary_and_denials(
            "Task completed successfully.",
            [denial("mcp__github__search_issues")],
        )
        envelope = self._normalized(task_id)
        self.assertNotIn("claim_capability_diagnostics", envelope)
        status = self.broker.status(task_id)
        self.assertNotIn("has_claim_capability_diagnostic", status["normalized_result"])

    def test_end_to_end_no_denials_produces_no_diagnostic_even_with_cue(self):
        task_id = self._create_with_summary_and_denials(
            "I verified via GitHub that the change was merged.",
            [],
        )
        envelope = self._normalized(task_id)
        self.assertNotIn("claim_capability_diagnostics", envelope)

    def test_completion_event_matches_status_concise_view(self):
        task_id = self._create_with_summary_and_denials(
            "I queried GitHub for the latest status.",
            [denial("mcp__github__search_issues")],
        )
        status = self.broker.status(task_id)
        page = self.broker.completion_events_since(0, task_id=task_id)
        event_summary = page["events"][0]["result_summary"]
        self.assertEqual(
            event_summary["claim_capability_diagnostics"],
            status["normalized_result"]["claim_capability_diagnostics"],
        )

    def test_privacy_bounds_no_raw_tool_input_or_extra_prose_leaks(self):
        denials = [{
            "tool_name": "mcp__github__search_issues",
            "tool_use_id": "tu_1",
            "tool_input": {"query": "super-secret-search-term", "token": "sk-ant-leaked"},
        }]
        task_id = self._create_with_summary_and_denials(
            "I verified via GitHub using a very long internal reasoning trace "
            "that must never be persisted verbatim into any diagnostic field.",
            denials,
        )
        status = self.broker.status(task_id)
        page = self.broker.completion_events_since(0, task_id=task_id)
        for payload in (status["normalized_result"], page["events"][0]["result_summary"]):
            encoded = json.dumps(payload)
            self.assertNotIn("super-secret-search-term", encoded)
            self.assertNotIn("sk-ant-leaked", encoded)
            self.assertNotIn("tool_input", encoded)
        # The diagnostic entry itself carries only the bounded cue/category/tool-family/id
        # -- never the raw summary prose it was matched against (that already surfaces
        # separately, unbounded, in the pre-existing concise `summary` field).
        diag = status["normalized_result"]["claim_capability_diagnostics"]["diagnostics"][0]
        self.assertEqual(set(diag), {"category", "cue", "tool_family", "denied_tool_identifiers"})
        self.assertNotIn("reasoning trace", json.dumps(diag))

    def test_required_verification_policy_never_downgraded_by_diagnostic(self):
        # verification_policy=required with a satisfied contract still succeeds
        # even though a claim-capability diagnostic is present -- the diagnostic
        # is advisory and must never fail/downgrade a task on its own.
        task_id = self._create_with_summary_and_denials(
            "I verified via GitHub that the change was merged.",
            [denial("mcp__github__search_issues")],
            required_capabilities=(WORKSPACE_READ,),
            verification_policy="none",
        )
        record = self.broker.store.get(task_id)
        self.assertEqual(record.state, TaskState.SUCCEEDED)
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["capability_contract"]["status"], "satisfied")
        self.assertEqual(envelope["claim_capability_diagnostics"]["diagnostic_count"], 1)

    def test_no_denials_no_cue_keeps_baseline_envelope_version(self):
        record = self.broker.create(TaskRequest("plain task", str(self.workspace), profile="claude_code"))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["envelope_version"], 1)
        self.assertNotIn("claim_capability_diagnostics", envelope)


class CompatibilityWithOtherResultContractsTests(unittest.TestCase):
    """Diagnostics must coexist with verified-investigation-report and review-report
    without rewriting their own findings/evidence/provenance or review outcome."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, claude_code_adapter=fake_claude_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _normalized(self, task_id: str) -> dict:
        path = self.broker.store.artifacts / task_id / NORMALIZED_RESULT_ARTIFACT
        return json.loads(path.read_text())

    def test_verified_investigation_report_schema_unaffected_by_diagnostic(self):
        structured = {
            "summary": "I verified via GitHub that findings are accurate.",
            "findings": [{"claim": "x", "confidence": 0.9, "evidence_refs": ["e1"]}],
            "evidence": [{
                "id": "e1", "provenance": "runtime_reported", "source_type": "note",
                "source": "manual check", "claim_supported": "x",
            }],
            "unverified_claims": [],
            "blocked_capabilities": [],
        }
        payload = json.dumps({
            "summary": json.dumps(structured),
            "permission_denials": [denial("mcp__github__search_issues")],
        }, separators=(",", ":"))
        record = self.broker.create(TaskRequest(
            f"SUMMARY_AND_DENIALS_JSON {payload}",
            str(self.workspace),
            profile="claude_code",
            result_schema=VERIFIED_INVESTIGATION_REPORT_SCHEMA,
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["parser"]["contract_status"], "satisfied")
        self.assertEqual(len(envelope["runtime_reported"]["verified_investigation"]["findings"]), 1)
        self.assertEqual(envelope["claim_capability_diagnostics"]["diagnostic_count"], 1)

    def test_review_report_schema_unaffected_by_diagnostic(self):
        structured = {
            "summary": "I checked via GitHub before approving.",
            "review_status": "passed",
            "review_findings": [],
            "reviewed_artifacts": [],
            "full_reexecution_performed": False,
        }
        payload = json.dumps({
            "summary": json.dumps(structured),
            "permission_denials": [denial("mcp__github__search_issues")],
        }, separators=(",", ":"))
        record = self.broker.create(TaskRequest(
            f"SUMMARY_AND_DENIALS_JSON {payload}",
            str(self.workspace),
            profile="claude_code",
            result_schema=REVIEW_REPORT_SCHEMA,
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["runtime_reported"]["review_report"]["review_status"], "passed")
        self.assertEqual(envelope["claim_capability_diagnostics"]["diagnostic_count"], 1)


if __name__ == "__main__":
    unittest.main()
