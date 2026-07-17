"""RFC-002 PR 4: tool-access-profile model, separate from execution_mode."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.claude_code_adapter import ClaudeCodeAdapter, ClaudeCodeUnsupportedPolicy
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.mcp_server import _build_task_request
from recollect_lines.required_capabilities import (
    WORKSPACE_READ,
    CapabilityPreflightContext,
    advertised_semantic_capabilities,
)
from recollect_lines.service import Broker
from recollect_lines.tool_access_profile import (
    LOCAL_WORKSPACE_READ_ONLY,
    LOCAL_WORKSPACE_STANDARD,
    ToolAccessProfileValidationError,
    evaluate_tool_access_profile_preflight,
    normalize_tool_access_profile,
    resolve_tool_access_profile,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_claude.py"


class SpyClaudeAdapter(ClaudeCodeAdapter):
    start_calls = 0

    def start(self, *args, **kwargs):
        SpyClaudeAdapter.start_calls += 1
        return super().start(*args, **kwargs)


def fake_claude_adapter(**kwargs):
    return SpyClaudeAdapter(command_prefix=(sys.executable, str(FIXTURE)), **kwargs)


class NormalizationTests(unittest.TestCase):
    def test_absent_means_no_selection(self):
        self.assertIsNone(normalize_tool_access_profile(None))

    def test_known_profile_passes_through(self):
        self.assertEqual(normalize_tool_access_profile(LOCAL_WORKSPACE_READ_ONLY), LOCAL_WORKSPACE_READ_ONLY)

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ToolAccessProfileValidationError) as ctx:
            normalize_tool_access_profile("yolo_mode")
        self.assertIn("Unknown tool_access_profile", str(ctx.exception))

    def test_non_string_rejected(self):
        with self.assertRaises(ToolAccessProfileValidationError):
            normalize_tool_access_profile(123)

    def test_empty_string_rejected(self):
        with self.assertRaises(ToolAccessProfileValidationError):
            normalize_tool_access_profile("   ")


class ResolutionDeterminismTests(unittest.TestCase):
    def test_omitted_profile_resolves_to_read_only_default_for_claude_code(self):
        profile = resolve_tool_access_profile(
            runtime="claude_code", execution_mode="read_only", requested_profile=None,
        )
        self.assertEqual(profile.name, LOCAL_WORKSPACE_READ_ONLY)
        self.assertEqual(profile.allowed_tools, ("Read", "Grep", "Glob"))
        self.assertEqual(profile.disallowed_tools, ("Edit", "Write", "NotebookEdit"))

    def test_omitted_profile_resolves_to_standard_default_for_isolated_worktree(self):
        profile = resolve_tool_access_profile(
            runtime="claude_code", execution_mode="isolated_worktree", requested_profile=None,
        )
        self.assertEqual(profile.name, LOCAL_WORKSPACE_STANDARD)
        self.assertIsNone(profile.allowed_tools)
        self.assertEqual(profile.disallowed_tools, ())

    def test_explicit_matching_selection_is_identical_to_omitted(self):
        omitted = resolve_tool_access_profile(runtime="claude_code", execution_mode="read_only", requested_profile=None)
        explicit = resolve_tool_access_profile(
            runtime="claude_code", execution_mode="read_only", requested_profile=LOCAL_WORKSPACE_READ_ONLY,
        )
        self.assertEqual(omitted, explicit)

    def test_omitted_profile_for_unsupported_runtime_is_none(self):
        self.assertIsNone(
            resolve_tool_access_profile(runtime="mock", execution_mode="read_only", requested_profile=None)
        )
        self.assertIsNone(
            resolve_tool_access_profile(runtime="opencode", execution_mode="isolated_worktree", requested_profile=None)
        )

    def test_resolution_is_deterministic_across_repeated_calls(self):
        results = {
            resolve_tool_access_profile(runtime="claude_code", execution_mode="read_only", requested_profile=None)
            for _ in range(5)
        }
        self.assertEqual(len(results), 1)

    def test_incompatible_explicit_selection_raises(self):
        with self.assertRaises(ToolAccessProfileValidationError):
            resolve_tool_access_profile(
                runtime="claude_code", execution_mode="read_only", requested_profile=LOCAL_WORKSPACE_STANDARD,
            )

    def test_unavailable_explicit_selection_raises(self):
        with self.assertRaises(ToolAccessProfileValidationError):
            resolve_tool_access_profile(
                runtime="mock", execution_mode="read_only", requested_profile=LOCAL_WORKSPACE_READ_ONLY,
            )


class PreflightEvaluationTests(unittest.TestCase):
    def test_omitted_profile_never_rejected(self):
        self.assertIsNone(
            evaluate_tool_access_profile_preflight(runtime="claude_code", execution_mode="read_only", requested_profile=None)
        )

    def test_compatible_explicit_profile_not_rejected(self):
        self.assertIsNone(
            evaluate_tool_access_profile_preflight(
                runtime="claude_code", execution_mode="isolated_worktree", requested_profile=LOCAL_WORKSPACE_STANDARD,
            )
        )

    def test_incompatible_execution_mode_rejected_with_machine_readable_reason(self):
        rejection = evaluate_tool_access_profile_preflight(
            runtime="claude_code", execution_mode="read_only", requested_profile=LOCAL_WORKSPACE_STANDARD,
        )
        assert rejection is not None
        self.assertEqual(rejection["reason"], "incompatible_tool_access_profile")
        self.assertEqual(rejection["tool_access_profile"], LOCAL_WORKSPACE_STANDARD)
        self.assertEqual(rejection["compatible_execution_modes"], ["isolated_worktree"])

    def test_unavailable_runtime_rejected_with_machine_readable_reason(self):
        rejection = evaluate_tool_access_profile_preflight(
            runtime="mock", execution_mode="read_only", requested_profile=LOCAL_WORKSPACE_READ_ONLY,
        )
        assert rejection is not None
        self.assertEqual(rejection["reason"], "unavailable_tool_access_profile")
        self.assertEqual(rejection["runtime"], "mock")


class AdvertisementIntegrationTests(unittest.TestCase):
    """Static capability advertisement (PR #50) now derives from the resolved profile."""

    def test_claude_read_only_advertises_workspace_read_via_default_profile(self):
        ctx = CapabilityPreflightContext(runtime="claude_code", execution_mode="read_only")
        self.assertEqual(advertised_semantic_capabilities(ctx), frozenset({WORKSPACE_READ}))

    def test_claude_isolated_worktree_advertises_workspace_read_via_default_profile(self):
        ctx = CapabilityPreflightContext(runtime="claude_code", execution_mode="isolated_worktree")
        self.assertEqual(advertised_semantic_capabilities(ctx), frozenset({WORKSPACE_READ}))

    def test_incompatible_explicit_profile_advertises_nothing(self):
        ctx = CapabilityPreflightContext(
            runtime="claude_code", execution_mode="read_only", tool_access_profile=LOCAL_WORKSPACE_STANDARD,
        )
        self.assertEqual(advertised_semantic_capabilities(ctx), frozenset())


class ClaudeCodeAdapterCommandEquivalenceTests(unittest.TestCase):
    """Omitted tool_access_profile must reproduce today's command byte-for-byte."""

    def _command(self, adapter, *args, **kwargs):
        command, _decision = adapter.build_command(*args, **kwargs)
        return command

    def test_omitted_profile_matches_explicit_read_only_profile(self):
        adapter = ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE)))
        omitted = self._command(adapter, "inspect", "read_only")
        explicit = self._command(adapter, "inspect", "read_only", tool_access_profile=LOCAL_WORKSPACE_READ_ONLY)
        self.assertEqual(omitted, explicit)
        self.assertIn("--tools", omitted)
        self.assertEqual(omitted[omitted.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(omitted[omitted.index("--disallowedTools") + 1], "Edit,Write,NotebookEdit")

    def test_omitted_profile_matches_explicit_standard_profile_for_isolated_worktree(self):
        adapter = ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE)))
        omitted = self._command(adapter, "edit stuff", "isolated_worktree")
        explicit = self._command(adapter, "edit stuff", "isolated_worktree", tool_access_profile=LOCAL_WORKSPACE_STANDARD)
        self.assertEqual(omitted, explicit)
        self.assertNotIn("--tools", omitted)
        self.assertNotIn("--disallowedTools", omitted)

    def test_incompatible_profile_fails_closed_before_launch(self):
        adapter = ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE)))
        with self.assertRaises(ClaudeCodeUnsupportedPolicy):
            adapter.build_command("inspect", "read_only", tool_access_profile=LOCAL_WORKSPACE_STANDARD)

    def test_unknown_profile_fails_closed_before_launch(self):
        adapter = ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE)))
        with self.assertRaises(ClaudeCodeUnsupportedPolicy):
            adapter.build_command("inspect", "read_only", tool_access_profile="yolo_mode")


class BrokerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        SpyClaudeAdapter.start_calls = 0
        self.broker = Broker(self.home, claude_code_adapter=fake_claude_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_omitted_profile_still_launches_read_only(self):
        record = self.broker.create(TaskRequest("inspect", str(self.workspace), profile="claude_code"))
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        self.assertEqual(SpyClaudeAdapter.start_calls, 1)

    def test_explicit_matching_profile_launches_identically(self):
        record = self.broker.create(TaskRequest(
            "inspect", str(self.workspace), profile="claude_code",
            tool_access_profile=LOCAL_WORKSPACE_READ_ONLY,
        ))
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        self.assertEqual(SpyClaudeAdapter.start_calls, 1)

    def test_incompatible_profile_rejected_before_launch(self):
        record = self.broker.create(TaskRequest(
            "inspect", str(self.workspace), profile="claude_code",
            tool_access_profile=LOCAL_WORKSPACE_STANDARD,
        ))
        rejected = self.broker.start(record.id)
        self.assertEqual(rejected.state, TaskState.REJECTED)
        self.assertEqual(SpyClaudeAdapter.start_calls, 0)
        events = self.broker.store.events(record.id)
        rejection_event = next(event for event in events if event["type"] == "task.rejected")
        metadata = rejection_event["metadata"]
        self.assertEqual(metadata["reason"], "incompatible_tool_access_profile")
        self.assertNotIn("launch", json.dumps(events))

    def test_unavailable_profile_for_runtime_rejected_before_launch(self):
        record = self.broker.create(TaskRequest(
            "inspect", str(self.workspace), profile="mock",
            tool_access_profile=LOCAL_WORKSPACE_READ_ONLY,
        ))
        rejected = self.broker.start(record.id)
        self.assertEqual(rejected.state, TaskState.REJECTED)
        events = self.broker.store.events(record.id)
        rejection_event = next(event for event in events if event["type"] == "task.rejected")
        self.assertEqual(rejection_event["metadata"]["reason"], "unavailable_tool_access_profile")

    def test_unknown_profile_rejected_at_create_before_launch(self):
        with self.assertRaises(ValueError) as ctx:
            self.broker.create(TaskRequest(
                "inspect", str(self.workspace), profile="claude_code",
                tool_access_profile="yolo_mode",
            ))
        self.assertIn("Unknown tool_access_profile", str(ctx.exception))
        self.assertEqual(SpyClaudeAdapter.start_calls, 0)

    def test_request_artifact_persists_tool_access_profile(self):
        record = self.broker.create(TaskRequest(
            "inspect", str(self.workspace), profile="claude_code",
            tool_access_profile=LOCAL_WORKSPACE_READ_ONLY,
        ))
        payload = json.loads((self.broker.store.artifacts / record.id / "request.json").read_text())
        self.assertEqual(payload["tool_access_profile"], LOCAL_WORKSPACE_READ_ONLY)

    def test_request_artifact_omits_key_when_not_provided(self):
        record = self.broker.create(TaskRequest("inspect", str(self.workspace), profile="claude_code"))
        payload = json.loads((self.broker.store.artifacts / record.id / "request.json").read_text())
        self.assertNotIn("tool_access_profile", payload)

    def test_mcp_request_builder_round_trips_tool_access_profile(self):
        request, _verify = _build_task_request({
            "task": "inspect",
            "workspace": str(self.workspace),
            "runtime": "claude_code",
            "tool_access_profile": LOCAL_WORKSPACE_READ_ONLY,
        })
        self.assertEqual(request.tool_access_profile, LOCAL_WORKSPACE_READ_ONLY)

    def test_mcp_request_builder_rejects_unknown_tool_access_profile(self):
        with self.assertRaises(ValueError):
            _build_task_request({
                "task": "inspect",
                "workspace": str(self.workspace),
                "runtime": "claude_code",
                "tool_access_profile": "yolo_mode",
            })


if __name__ == "__main__":
    unittest.main()
