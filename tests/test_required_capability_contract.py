"""RFC-002 PR 3: post-run required-capability contract.

Preflight (test_required_capability_preflight.py) is a static, pre-launch
gate. This module covers the post-run counterpart: a task that *passed*
preflight but received a structured runtime denial for the concrete tool a
declared capability depends on must still surface that capability as
unsatisfied -- as a separate, machine-readable dimension from `TaskState`
and from `parser.contract_status`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.capability_contract_result import (
    STATUS_NO_REQUIREMENTS,
    STATUS_SATISFIED,
    STATUS_UNKNOWN,
    STATUS_UNSATISFIED,
    evaluate_capability_contract,
)
from recollect_lines.claude_code_adapter import ClaudeCodeAdapter
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.required_capabilities import WORKSPACE_READ
from recollect_lines.result_normalization import (
    CAPABILITY_CONTRACT_ENVELOPE_VERSION,
    NORMALIZED_RESULT_ARTIFACT,
    concise_normalized_view,
)
from recollect_lines.service import Broker

FIXTURE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"

PASSING_COMMAND = [sys.executable, "-c", "print('ok')"]


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(
        command_prefix=(sys.executable, str(FIXTURE_CLAUDE)),
        grace_period_seconds=2.0,
        **kwargs,
    )


def denial(tool_name: str) -> dict:
    return {"tool_name": tool_name, "tool_use_id": f"tu_{tool_name}", "tool_input": {}}


class EvaluateCapabilityContractUnitTests(unittest.TestCase):
    def test_no_required_capabilities_is_distinct_status(self):
        result = evaluate_capability_contract(
            (), adapter="claude_code", capability_observations=[], denial_metadata_malformed=False,
        )
        self.assertEqual(result["status"], STATUS_NO_REQUIREMENTS)
        self.assertEqual(result["unsatisfied_capabilities"], [])
        self.assertEqual(result["unknown_capabilities"], [])

    def test_no_denial_evidence_is_satisfied(self):
        result = evaluate_capability_contract(
            (WORKSPACE_READ,), adapter="claude_code", capability_observations=[], denial_metadata_malformed=False,
        )
        self.assertEqual(result["status"], STATUS_SATISFIED)
        self.assertEqual(result["unsatisfied_capabilities"], [])

    def test_primary_tool_denial_is_unsatisfied(self):
        observations = [{"tool_identifier": "Read", "source": "runtime_permission_denial", "adapter": "claude_code"}]
        result = evaluate_capability_contract(
            (WORKSPACE_READ,), adapter="claude_code", capability_observations=observations, denial_metadata_malformed=False,
        )
        self.assertEqual(result["status"], STATUS_UNSATISFIED)
        self.assertEqual(result["unsatisfied_capabilities"], [WORKSPACE_READ])
        self.assertTrue(any("Read" in reason for reason in result["reasons"]))

    def test_auxiliary_tool_denial_does_not_overclaim_unsatisfied(self):
        for tool in ("Grep", "Glob"):
            with self.subTest(tool=tool):
                observations = [{"tool_identifier": tool, "source": "runtime_permission_denial", "adapter": "claude_code"}]
                result = evaluate_capability_contract(
                    (WORKSPACE_READ,), adapter="claude_code", capability_observations=observations, denial_metadata_malformed=False,
                )
                self.assertEqual(result["status"], STATUS_SATISFIED)
                self.assertEqual(result["unsatisfied_capabilities"], [])

    def test_unmapped_adapter_is_unknown_not_satisfied(self):
        result = evaluate_capability_contract(
            (WORKSPACE_READ,), adapter="mock", capability_observations=[], denial_metadata_malformed=False,
        )
        self.assertEqual(result["status"], STATUS_UNKNOWN)
        self.assertEqual(result["unknown_capabilities"], [WORKSPACE_READ])

    def test_none_adapter_is_unknown(self):
        result = evaluate_capability_contract(
            (WORKSPACE_READ,), adapter=None, capability_observations=[], denial_metadata_malformed=False,
        )
        self.assertEqual(result["status"], STATUS_UNKNOWN)

    def test_malformed_denial_metadata_fails_safe_to_unknown(self):
        # No observations survived (e.g. permission_denials was entirely
        # malformed), so absence of a Read denial is not trustworthy evidence.
        result = evaluate_capability_contract(
            (WORKSPACE_READ,), adapter="claude_code", capability_observations=[], denial_metadata_malformed=True,
        )
        self.assertEqual(result["status"], STATUS_UNKNOWN)
        self.assertEqual(result["unsatisfied_capabilities"], [])

    def test_malformed_metadata_never_masks_real_denial_evidence(self):
        observations = [{"tool_identifier": "Read", "source": "runtime_permission_denial", "adapter": "claude_code"}]
        result = evaluate_capability_contract(
            (WORKSPACE_READ,), adapter="claude_code", capability_observations=observations, denial_metadata_malformed=True,
        )
        self.assertEqual(result["status"], STATUS_UNSATISFIED)

    def test_deterministic_ordering(self):
        result_a = evaluate_capability_contract(
            (WORKSPACE_READ,), adapter="claude_code", capability_observations=[], denial_metadata_malformed=False,
        )
        result_b = evaluate_capability_contract(
            (WORKSPACE_READ,), adapter="claude_code", capability_observations=[], denial_metadata_malformed=False,
        )
        self.assertEqual(result_a, result_b)


class BrokerCapabilityContractIntegrationTests(unittest.TestCase):
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

    def _create_with_denials(self, denials: list, **request_kwargs) -> str:
        payload = json.dumps(denials, separators=(",", ":"))
        record = self.broker.create(TaskRequest(
            f"PERMISSION_DENIALS_JSON {payload}",
            str(self.workspace),
            profile="claude_code",
            required_capabilities=(WORKSPACE_READ,),
            **request_kwargs,
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        return record.id

    def test_no_requirements_declared_keeps_compatible_envelope(self):
        record = self.broker.create(TaskRequest("plain task", str(self.workspace), profile="claude_code"))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["envelope_version"], 1)
        self.assertNotIn("capability_contract", envelope)
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertNotIn("capability_contract", view)
        self.assertNotIn("has_capability_contract", view)

    def test_workspace_read_satisfied_with_no_denial(self):
        task_id = self._create_with_denials([])
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["envelope_version"], CAPABILITY_CONTRACT_ENVELOPE_VERSION)
        self.assertEqual(envelope["state"], TaskState.SUCCEEDED.value)
        self.assertEqual(envelope["capability_contract"]["status"], STATUS_SATISFIED)

    def test_read_denial_makes_contract_unsatisfied_but_execution_separate(self):
        task_id = self._create_with_denials([denial("Read")])
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["capability_contract"]["status"], STATUS_UNSATISFIED)
        self.assertEqual(envelope["capability_contract"]["unsatisfied_capabilities"], [WORKSPACE_READ])
        # Execution outcome is unaffected by default (verification_policy="none"):
        # process-level success is preserved even though the contract is not.
        self.assertEqual(envelope["state"], TaskState.SUCCEEDED.value)

    def test_auxiliary_denial_warns_without_overclaiming_contract(self):
        task_id = self._create_with_denials([denial("Grep")])
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["capability_contract"]["status"], STATUS_SATISFIED)
        # The auxiliary denial is still visible as a capability-warning observation (PR 1).
        self.assertIn("capability_observations", envelope["runtime_reported"])
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertTrue(view["has_capability_warning"])

    def test_malformed_permission_denials_fail_safe_not_crash(self):
        record = self.broker.create(TaskRequest(
            "PERMISSION_DENIALS_JSON {}",
            str(self.workspace),
            profile="claude_code",
            required_capabilities=(WORKSPACE_READ,),
        ))
        self.broker.start(record.id)
        self.broker.collect(record.id)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["capability_contract"]["status"], STATUS_UNKNOWN)
        self.assertTrue(any("not a list" in w for w in envelope["parser"]["warnings"]))

    def test_concise_and_completion_views_are_bounded_and_privacy_safe(self):
        task_id = self._create_with_denials([denial("Read")])
        envelope = self._normalized(task_id)
        view = concise_normalized_view(envelope)
        assert view is not None
        self.assertTrue(view["has_capability_contract"])
        self.assertEqual(view["capability_contract"]["status"], STATUS_UNSATISFIED)
        encoded = json.dumps(view)
        self.assertNotIn("tool_input", encoded)

        page = self.broker.completion_events_since(0, task_id=task_id)
        summary = page["events"][0]["result_summary"]
        self.assertEqual(summary["capability_contract"], view["capability_contract"])
        self.assertNotIn("tool_input", json.dumps(summary))

    def test_default_policy_stays_visible_not_silent_on_unsatisfied_contract(self):
        task_id = self._create_with_denials([denial("Read")], verification_policy="none")
        record = self.broker.store.get(task_id)
        self.assertEqual(record.state, TaskState.SUCCEEDED)
        envelope = self._normalized(task_id)
        self.assertEqual(envelope["capability_contract"]["status"], STATUS_UNSATISFIED)

    def test_required_verification_policy_fails_task_on_unsatisfied_contract(self):
        payload = json.dumps([denial("Read")], separators=(",", ":"))
        record = self.broker.create(
            TaskRequest(
                f"PERMISSION_DENIALS_JSON {payload}",
                str(self.workspace),
                profile="claude_code",
                required_capabilities=(WORKSPACE_READ,),
                verification_policy="required",
            ),
            verify_commands=[PASSING_COMMAND],
        )
        self.broker.start(record.id)
        collected = self.broker.collect(record.id)
        self.assertEqual(collected.state, TaskState.FAILED)
        envelope = self._normalized(record.id)
        self.assertEqual(envelope["capability_contract"]["status"], STATUS_UNSATISFIED)
        events = self.broker.store.events(record.id)
        terminal = next(event for event in events if event["type"] == "task.failed")
        self.assertEqual(
            terminal["metadata"]["verification_gate"]["outcome"], "blocked_unsatisfied_capability",
        )

    def test_required_verification_policy_passes_when_contract_satisfied(self):
        record = self.broker.create(
            TaskRequest(
                "PERMISSION_DENIALS_JSON []",
                str(self.workspace),
                profile="claude_code",
                required_capabilities=(WORKSPACE_READ,),
                verification_policy="required",
            ),
            verify_commands=[PASSING_COMMAND],
        )
        self.broker.start(record.id)
        collected = self.broker.collect(record.id)
        self.assertEqual(collected.state, TaskState.SUCCEEDED)

    def test_repository_remote_read_still_rejected_before_launch(self):
        from recollect_lines.required_capabilities import REPOSITORY_REMOTE_READ

        record = self.broker.create(TaskRequest(
            "check remote pr",
            str(self.workspace),
            profile="claude_code",
            required_capabilities=(REPOSITORY_REMOTE_READ,),
        ))
        rejected = self.broker.start(record.id)
        self.assertEqual(rejected.state, TaskState.REJECTED)


if __name__ == "__main__":
    unittest.main()
