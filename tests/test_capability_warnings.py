"""RFC-002 PR 1: capability-warning observations from structured permission denials."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.claude_code_adapter import ClaudeCodeAdapter
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.result_normalization import (
    CAPABILITY_ENVELOPE_VERSION,
    CAPABILITY_OBSERVATION_SOURCE,
    NORMALIZED_RESULT_ARTIFACT,
    build_normalized_envelope,
    concise_normalized_view,
    normalize_permission_denials,
)
from recollect_lines.service import Broker

FIXTURE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(
        command_prefix=(sys.executable, str(FIXTURE_CLAUDE)),
        grace_period_seconds=2.0,
        **kwargs,
    )


def denial(tool_name: str, **tool_input) -> dict:
    return {
        "tool_name": tool_name,
        "tool_use_id": f"tu_{tool_name}",
        "tool_input": tool_input or {"path": "/secret/repo/.env", "token": "sk-ant-leaked"},
    }


class NormalizePermissionDenialsTests(unittest.TestCase):
    def test_empty_or_absent_denials_produce_no_warning(self):
        for payload in (None, []):
            with self.subTest(payload=payload):
                observations, warning, warnings = normalize_permission_denials(
                    payload, adapter="claude_code",
                )
                self.assertEqual(observations, [])
                self.assertIsNone(warning)
                self.assertEqual(warnings, [])

    def test_one_denied_tool(self):
        observations, warning, warnings = normalize_permission_denials(
            [denial("mcp__host__get_pull_request")],
            adapter="claude_code",
        )
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["tool_identifier"], "mcp__host__get_pull_request")
        self.assertEqual(observations[0]["source"], CAPABILITY_OBSERVATION_SOURCE)
        self.assertEqual(observations[0]["adapter"], "claude_code")
        self.assertEqual(warning["denial_attempt_count"], 1)
        self.assertEqual(warning["distinct_denied_tool_count"], 1)
        self.assertEqual(warning["denied_tool_identifiers"], ["mcp__host__get_pull_request"])
        self.assertFalse(warning["truncated"])
        self.assertEqual(warnings, [])

    def test_repeated_denial_attempts_count_all(self):
        tool = "mcp__host__get_pull_request"
        entries = [denial(tool), denial(tool), denial(tool)]
        observations, warning, _ = normalize_permission_denials(entries, adapter="claude_code")
        self.assertEqual(len(observations), 3)
        self.assertEqual(warning["denial_attempt_count"], 3)
        self.assertEqual(warning["distinct_denied_tool_count"], 1)

    def test_multiple_tools_are_sorted_deterministically(self):
        entries = [
            denial("mcp__z__last"),
            denial("mcp__a__first"),
            denial("mcp__m__middle"),
        ]
        _, warning, _ = normalize_permission_denials(entries, adapter="claude_code")
        self.assertEqual(
            warning["denied_tool_identifiers"],
            ["mcp__a__first", "mcp__m__middle", "mcp__z__last"],
        )
        self.assertEqual(warning["distinct_denied_tool_count"], 3)

    def test_case_sensitive_deduplication(self):
        entries = [denial("ToolA"), denial("toola"), denial("ToolA")]
        observations, warning, _ = normalize_permission_denials(entries, adapter="claude_code")
        self.assertEqual(warning["denial_attempt_count"], 3)
        self.assertEqual(warning["distinct_denied_tool_count"], 2)
        self.assertEqual(warning["denied_tool_identifiers"], ["ToolA", "toola"])
        self.assertEqual(len(observations), 3)

    def test_more_than_sixteen_distinct_tools_truncates_display(self):
        entries = [denial(f"mcp__host__tool_{index:02d}") for index in range(20)]
        observations, warning, _ = normalize_permission_denials(entries, adapter="claude_code")
        self.assertEqual(warning["denial_attempt_count"], 20)
        self.assertEqual(warning["distinct_denied_tool_count"], 20)
        self.assertEqual(len(warning["denied_tool_identifiers"]), 16)
        self.assertTrue(warning["truncated"])
        self.assertEqual(len(observations), 20)

    def test_malformed_list_warns_without_observations(self):
        observations, warning, warnings = normalize_permission_denials(
            {"not": "a list"},
            adapter="claude_code",
        )
        self.assertEqual(observations, [])
        self.assertIsNone(warning)
        self.assertEqual(len(warnings), 1)
        self.assertIn("not a list", warnings[0])

    def test_malformed_entries_preserve_valid_siblings(self):
        entries = [
            denial("mcp__host__ok"),
            "not-an-object",
            {"tool_use_id": "missing-name"},
            denial("mcp__host__also_ok"),
        ]
        observations, warning, warnings = normalize_permission_denials(entries, adapter="claude_code")
        self.assertEqual(len(observations), 2)
        self.assertEqual(warning["denial_attempt_count"], 2)
        self.assertIn("2 malformed entries", warnings[0])


class ConciseViewCapabilityWarningTests(unittest.TestCase):
    def test_concise_view_includes_warning_flag_and_compact_data(self):
        envelope = {
            "envelope_version": CAPABILITY_ENVELOPE_VERSION,
            "state": "succeeded",
            "runtime_reported": {
                "summary": "done",
                "capability_observations": [
                    {
                        "tool_identifier": "mcp__host__tool",
                        "source": CAPABILITY_OBSERVATION_SOURCE,
                        "adapter": "claude_code",
                    }
                ],
            },
            "parser": {"requested_schema": "plain-summary", "parse_status": "ok", "warnings": []},
            "broker_observed": {},
        }
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertTrue(view["has_capability_warning"])
        self.assertEqual(view["capability_warning"]["denial_attempt_count"], 1)
        self.assertNotIn("tool_input", json.dumps(view))


class BrokerCapabilityWarningIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, claude_code_adapter=fake_claude_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def _collect_with_denials(self, denials: list) -> str:
        payload = json.dumps(denials, separators=(",", ":"))
        record = self.broker.create(TaskRequest(
            f"PERMISSION_DENIALS_JSON {payload}",
            str(self.workspace),
            profile="claude_code",
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        return record.id

    def _normalized(self, task_id: str) -> dict:
        path = self.broker.store.artifacts / task_id / NORMALIZED_RESULT_ARTIFACT
        return json.loads(path.read_text())

    def test_no_denials_keep_envelope_version_one(self):
        record = self.broker.create(TaskRequest("plain task", str(self.workspace), profile="claude_code"))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["envelope_version"], 1)
        self.assertNotIn("capability_observations", envelope["runtime_reported"])
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertNotIn("capability_warning", view)
        self.assertNotIn("has_capability_warning", view)

    def test_claude_adapter_integration_surfaces_warning_without_state_downgrade(self):
        task_id = self._collect_with_denials([denial("mcp__host__get_pull_request")])
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["envelope_version"], CAPABILITY_ENVELOPE_VERSION)
        self.assertEqual(envelope["state"], TaskState.SUCCEEDED.value)
        observations = envelope["runtime_reported"]["capability_observations"]
        self.assertEqual(len(observations), 1)
        self.assertIn("permission_denials", envelope["runtime_reported"]["session_metadata"])

        status = self.broker.status(task_id)
        compact = status["normalized_result"]
        self.assertTrue(compact["has_capability_warning"])
        self.assertEqual(compact["capability_warning"]["denial_attempt_count"], 1)

    def test_sensitive_tool_input_absent_from_compact_views(self):
        denials = [denial("mcp__host__read_file", path="/secret/repo/.env", token="sk-ant-leaked")]
        task_id = self._collect_with_denials(denials)
        envelope = self._normalized(task_id)
        compact = concise_normalized_view(envelope)
        encoded = json.dumps(compact)
        self.assertIn("mcp__host__read_file", encoded)
        self.assertNotIn("/secret/repo/.env", encoded)
        self.assertNotIn("sk-ant-leaked", encoded)
        self.assertNotIn("tool_input", encoded)

        page = self.broker.completion_events_since(0, task_id=task_id)
        event = page["events"][0]
        summary = event["result_summary"]
        summary_encoded = json.dumps(summary)
        self.assertIn("capability_warning", summary)
        self.assertNotIn("/secret/repo/.env", summary_encoded)
        self.assertNotIn("sk-ant-leaked", summary_encoded)

    def test_malformed_metadata_with_valid_sibling_collects_successfully(self):
        denials = [
            denial("mcp__host__ok"),
            "bad-entry",
            denial("mcp__host__ok"),
        ]
        task_id = self._collect_with_denials(denials)
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["state"], TaskState.SUCCEEDED.value)
        self.assertEqual(len(envelope["runtime_reported"]["capability_observations"]), 2)
        self.assertTrue(any("malformed" in w for w in envelope["parser"]["warnings"]))

    def test_completion_event_capability_warning_matches_concise_view(self):
        denials = [
            denial("mcp__host__b"),
            denial("mcp__host__a"),
            denial("mcp__host__a"),
        ]
        task_id = self._collect_with_denials(denials)
        status = self.broker.status(task_id)
        page = self.broker.completion_events_since(0, task_id=task_id)
        event_summary = page["events"][0]["result_summary"]
        self.assertEqual(
            event_summary["capability_warning"],
            status["normalized_result"]["capability_warning"],
        )


if __name__ == "__main__":
    unittest.main()
