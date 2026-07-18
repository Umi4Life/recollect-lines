"""Required-capability declarations and static preflight rejection."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.claude_code_adapter import ClaudeCodeAdapter
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.mcp_server import _build_task_request
from recollect_lines.required_capabilities import (
    REPOSITORY_REMOTE_READ,
    WORKSPACE_READ,
    CapabilityPreflightContext,
    RequiredCapabilityValidationError,
    advertised_semantic_capabilities,
    evaluate_capability_preflight,
    normalize_required_capabilities,
)
from recollect_lines.service import Broker

FIXTURE = Path(__file__).parent / "fixtures" / "fake_claude.py"


class SpyClaudeAdapter(ClaudeCodeAdapter):
    start_calls = 0

    def start(self, *args, **kwargs):
        SpyClaudeAdapter.start_calls += 1
        return super().start(*args, **kwargs)


def fake_claude_adapter(**kwargs):
    return SpyClaudeAdapter(command_prefix=(sys.executable, str(FIXTURE)), **kwargs)


class NormalizationTests(unittest.TestCase):
    def test_absent_means_no_requirements(self):
        self.assertEqual(normalize_required_capabilities(None), ())

    def test_deduplicates_and_sorts_deterministically(self):
        self.assertEqual(
            normalize_required_capabilities(["repository.remote.read", "workspace.read", "workspace.read"]),
            (REPOSITORY_REMOTE_READ, WORKSPACE_READ),
        )

    def test_unknown_capability_rejected(self):
        with self.assertRaises(RequiredCapabilityValidationError) as ctx:
            normalize_required_capabilities(["workspace.mutate"])
        self.assertIn("unknown capability id", str(ctx.exception))

    def test_empty_array_rejected(self):
        with self.assertRaises(RequiredCapabilityValidationError):
            normalize_required_capabilities([])

    def test_non_string_entry_rejected(self):
        with self.assertRaises(RequiredCapabilityValidationError):
            normalize_required_capabilities(["workspace.read", 1])


class AdvertisementTests(unittest.TestCase):
    def test_claude_read_only_advertises_workspace_read_only(self):
        ctx = CapabilityPreflightContext(runtime="claude_code", execution_mode="read_only")
        self.assertEqual(advertised_semantic_capabilities(ctx), frozenset({WORKSPACE_READ}))

    def test_claude_isolated_worktree_advertises_workspace_read_only(self):
        ctx = CapabilityPreflightContext(runtime="claude_code", execution_mode="isolated_worktree")
        self.assertEqual(advertised_semantic_capabilities(ctx), frozenset({WORKSPACE_READ}))

    def test_unmapped_runtime_advertises_nothing(self):
        ctx = CapabilityPreflightContext(runtime="mock", execution_mode="read_only")
        self.assertEqual(advertised_semantic_capabilities(ctx), frozenset())

    def test_preflight_reports_missing_remote_read_for_claude_read_only(self):
        required = (REPOSITORY_REMOTE_READ,)
        ctx = CapabilityPreflightContext(runtime="claude_code", execution_mode="read_only")
        rejection = evaluate_capability_preflight(required, ctx)
        assert rejection is not None
        self.assertEqual(rejection["reason"], "missing_required_capabilities")
        self.assertEqual(rejection["missing_capabilities"], [REPOSITORY_REMOTE_READ])
        self.assertEqual(rejection["required_capabilities"], [REPOSITORY_REMOTE_READ])
        self.assertEqual(rejection["advertised_capabilities"], [WORKSPACE_READ])


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

    def test_legacy_request_without_requirements_still_launches(self):
        record = self.broker.create(TaskRequest("legacy", str(self.workspace), profile="claude_code"))
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        self.assertEqual(SpyClaudeAdapter.start_calls, 1)

    def test_workspace_read_passes_preflight_and_launches(self):
        record = self.broker.create(TaskRequest(
            "inspect",
            str(self.workspace),
            profile="claude_code",
            required_capabilities=(WORKSPACE_READ,),
        ))
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        self.assertEqual(SpyClaudeAdapter.start_calls, 1)

    def test_repository_remote_read_rejected_before_launch(self):
        record = self.broker.create(TaskRequest(
            "check remote pr",
            str(self.workspace),
            profile="claude_code",
            required_capabilities=(REPOSITORY_REMOTE_READ,),
        ))
        rejected = self.broker.start(record.id)
        self.assertEqual(rejected.state, TaskState.REJECTED)
        self.assertEqual(SpyClaudeAdapter.start_calls, 0)
        events = self.broker.store.events(record.id)
        rejection_event = next(event for event in events if event["type"] == "task.rejected")
        metadata = rejection_event["metadata"]
        self.assertEqual(metadata["reason"], "missing_required_capabilities")
        self.assertEqual(metadata["missing_capabilities"], [REPOSITORY_REMOTE_READ])
        self.assertNotIn("launch", json.dumps(events))

    def test_unknown_capability_rejected_at_create(self):
        with self.assertRaises(ValueError) as ctx:
            self.broker.create(TaskRequest(
                "nope",
                str(self.workspace),
                profile="claude_code",
                required_capabilities=("workspace.mutate",),
            ))
        self.assertIn("unknown capability id", str(ctx.exception))
        self.assertEqual(SpyClaudeAdapter.start_calls, 0)

    def test_request_artifact_persists_required_capabilities(self):
        record = self.broker.create(TaskRequest(
            "inspect",
            str(self.workspace),
            profile="claude_code",
            required_capabilities=(WORKSPACE_READ, REPOSITORY_REMOTE_READ),
        ))
        payload = json.loads((self.broker.store.artifacts / record.id / "request.json").read_text())
        self.assertEqual(payload["required_capabilities"], [WORKSPACE_READ, REPOSITORY_REMOTE_READ])

    def test_mcp_request_builder_round_trips_required_capabilities(self):
        request, _verify = _build_task_request({
            "task": "inspect",
            "workspace": str(self.workspace),
            "runtime": "claude_code",
            "required_capabilities": ["repository.remote.read", "workspace.read"],
        })
        self.assertEqual(request.required_capabilities, (REPOSITORY_REMOTE_READ, WORKSPACE_READ))


if __name__ == "__main__":
    unittest.main()
