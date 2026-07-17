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
    REPOSITORY_REMOTE_READ,
    WORKSPACE_READ,
    CapabilityPreflightContext,
    advertised_semantic_capabilities,
    evaluate_capability_preflight,
)
from recollect_lines.result_normalization import build_normalized_envelope, concise_normalized_view
from recollect_lines.service import Broker
from recollect_lines.tool_access_profile import (
    LOCAL_WORKSPACE_READ_ONLY,
    LOCAL_WORKSPACE_STANDARD,
    OPERATOR_APPROVED_REPOSITORY_READ,
    ToolAccessProfileConfigError,
    ToolAccessProfileValidationError,
    build_tool_access_profile_registry,
    evaluate_tool_access_profile_preflight,
    normalize_tool_access_profile,
    parse_tool_access_profiles_document,
    resolve_tool_access_profile,
    tool_access_profile_audit_payload,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_claude.py"
SAMPLE_EXTERNAL_TOOLS = (
    "mcp__github__get_pull_request",
    "mcp__github__list_commits",
)


def repository_read_registry(*, profile_name: str = OPERATOR_APPROVED_REPOSITORY_READ) -> object:
    configured = parse_tool_access_profiles_document({
        profile_name: {"allowed_external_tools": list(SAMPLE_EXTERNAL_TOOLS)},
    })
    return build_tool_access_profile_registry(configured=configured)


class SpyClaudeAdapter(ClaudeCodeAdapter):
    start_calls = 0

    def start(self, *args, **kwargs):
        SpyClaudeAdapter.start_calls += 1
        return super().start(*args, **kwargs)


def fake_claude_adapter(**kwargs):
    registry = kwargs.pop("tool_access_profile_registry", None)
    adapter = SpyClaudeAdapter(command_prefix=(sys.executable, str(FIXTURE)), **kwargs)
    if registry is not None:
        adapter.tool_access_profile_registry = registry
    return adapter


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


class RepositoryReadProfileTests(unittest.TestCase):
    def test_config_parses_finite_exact_external_tools(self):
        configured = parse_tool_access_profiles_document({
            OPERATOR_APPROVED_REPOSITORY_READ: {
                "allowed_external_tools": list(SAMPLE_EXTERNAL_TOOLS),
            },
        })
        profile = configured[OPERATOR_APPROVED_REPOSITORY_READ]
        self.assertEqual(profile.external_tools, SAMPLE_EXTERNAL_TOOLS)

    def test_wildcard_external_tool_rejected_at_config_parse(self):
        with self.assertRaises(ToolAccessProfileConfigError) as ctx:
            parse_tool_access_profiles_document({
                OPERATOR_APPROVED_REPOSITORY_READ: {
                    "allowed_external_tools": ["mcp__github__*"],
                },
            })
        self.assertIn("wildcard", str(ctx.exception).lower())

    def test_duplicate_external_tool_rejected_at_config_parse(self):
        with self.assertRaises(ToolAccessProfileConfigError):
            parse_tool_access_profiles_document({
                OPERATOR_APPROVED_REPOSITORY_READ: {
                    "allowed_external_tools": [
                        "mcp__github__get_pull_request",
                        "mcp__github__get_pull_request",
                    ],
                },
            })

    def test_empty_external_allowlist_rejected_at_config_parse(self):
        with self.assertRaises(ToolAccessProfileConfigError):
            parse_tool_access_profiles_document({
                OPERATOR_APPROVED_REPOSITORY_READ: {"allowed_external_tools": []},
            })

    def test_unconfigured_repository_read_profile_rejected_before_launch(self):
        rejection = evaluate_tool_access_profile_preflight(
            runtime="claude_code",
            execution_mode="read_only",
            requested_profile=OPERATOR_APPROVED_REPOSITORY_READ,
        )
        assert rejection is not None
        self.assertEqual(rejection["reason"], "unconfigured_tool_access_profile")

    def test_approved_profile_builds_expected_claude_allowlist(self):
        registry = repository_read_registry()
        adapter = fake_claude_adapter(tool_access_profile_registry=registry)
        command, _decision = adapter.build_command(
            "inspect remote",
            "read_only",
            tool_access_profile=OPERATOR_APPROVED_REPOSITORY_READ,
        )
        tools = command[command.index("--tools") + 1]
        self.assertEqual(tools.split(","), ["Read", "Grep", "Glob", *SAMPLE_EXTERNAL_TOOLS])
        self.assertEqual(command[command.index("--disallowedTools") + 1], "Edit,Write,NotebookEdit")

    def test_approved_profile_advertises_repository_remote_read(self):
        registry = repository_read_registry()
        ctx = CapabilityPreflightContext(
            runtime="claude_code",
            execution_mode="read_only",
            tool_access_profile=OPERATOR_APPROVED_REPOSITORY_READ,
            tool_access_profile_registry=registry,
        )
        self.assertEqual(
            advertised_semantic_capabilities(ctx),
            frozenset({WORKSPACE_READ, REPOSITORY_REMOTE_READ}),
        )

    def test_repository_remote_read_preflight_succeeds_for_approved_profile(self):
        registry = repository_read_registry()
        ctx = CapabilityPreflightContext(
            runtime="claude_code",
            execution_mode="read_only",
            tool_access_profile=OPERATOR_APPROVED_REPOSITORY_READ,
            tool_access_profile_registry=registry,
        )
        self.assertIsNone(evaluate_capability_preflight((REPOSITORY_REMOTE_READ,), ctx))

    def test_local_only_profile_still_rejects_repository_remote_read(self):
        ctx = CapabilityPreflightContext(runtime="claude_code", execution_mode="read_only")
        rejection = evaluate_capability_preflight((REPOSITORY_REMOTE_READ,), ctx)
        assert rejection is not None
        self.assertEqual(rejection["reason"], "missing_required_capabilities")

    def test_audit_payload_exposes_count_not_tool_identifiers(self):
        registry = repository_read_registry()
        profile = resolve_tool_access_profile(
            runtime="claude_code",
            execution_mode="read_only",
            requested_profile=OPERATOR_APPROVED_REPOSITORY_READ,
            registry=registry,
        )
        audit = tool_access_profile_audit_payload(profile)
        assert audit is not None
        self.assertEqual(audit["external_tool_count"], len(SAMPLE_EXTERNAL_TOOLS))
        encoded = json.dumps(audit)
        for tool in SAMPLE_EXTERNAL_TOOLS:
            self.assertNotIn(tool, encoded)

    def test_operator_configured_instance_name_is_selectable(self):
        registry = repository_read_registry(profile_name="acme_repo_read")
        self.assertEqual(
            normalize_tool_access_profile("acme_repo_read", registry=registry),
            "acme_repo_read",
        )


class RepositoryReadBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.registry = repository_read_registry()
        SpyClaudeAdapter.start_calls = 0
        self.broker = Broker(
            self.home,
            claude_code_adapter=fake_claude_adapter(tool_access_profile_registry=self.registry),
            tool_access_profile_registry=self.registry,
        )

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_approved_profile_launches_with_required_capability(self):
        record = self.broker.create(TaskRequest(
            "check remote pr",
            str(self.workspace),
            profile="claude_code",
            tool_access_profile=OPERATOR_APPROVED_REPOSITORY_READ,
            required_capabilities=(REPOSITORY_REMOTE_READ,),
        ))
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        self.assertEqual(SpyClaudeAdapter.start_calls, 1)

    def test_unconfigured_repository_read_rejected_before_launch(self):
        broker = Broker(self.home, claude_code_adapter=fake_claude_adapter())
        record = broker.create(TaskRequest(
            "check remote pr",
            str(self.workspace),
            profile="claude_code",
            tool_access_profile=OPERATOR_APPROVED_REPOSITORY_READ,
        ))
        rejected = broker.start(record.id)
        self.assertEqual(rejected.state, TaskState.REJECTED)
        self.assertEqual(SpyClaudeAdapter.start_calls, 0)
        events = broker.store.events(record.id)
        rejection_event = next(event for event in events if event["type"] == "task.rejected")
        self.assertEqual(rejection_event["metadata"]["reason"], "unconfigured_tool_access_profile")
        broker.close()

    def test_resolution_artifact_and_normalized_audit_are_privacy_safe(self):
        record = self.broker.create(TaskRequest(
            "inspect",
            str(self.workspace),
            profile="claude_code",
            tool_access_profile=OPERATOR_APPROVED_REPOSITORY_READ,
        ))
        self.broker.start(record.id)
        resolution = json.loads(
            (self.broker.store.artifacts / record.id / "tool_access_profile_resolution.json").read_text()
        )
        self.assertEqual(resolution["tool_access_profile"], OPERATOR_APPROVED_REPOSITORY_READ)
        self.assertEqual(resolution["external_tool_count"], len(SAMPLE_EXTERNAL_TOOLS))
        encoded = json.dumps(resolution)
        for tool in SAMPLE_EXTERNAL_TOOLS:
            self.assertNotIn(tool, encoded)

        envelope = build_normalized_envelope(
            record=record,
            result={},
            collected={"adapter": "claude_code", "exit_code": 0},
            gate={"policy": "none", "outcome": "passed"},
            verification=None,
            manifest={"artifacts": []},
            launch=None,
            raw_output_artifact=None,
            final_state=TaskState.SUCCEEDED,
            tool_access_profile_audit=resolution,
        )
        view = concise_normalized_view(envelope)
        assert view is not None
        audit = view["tool_access_profile_audit"]
        self.assertEqual(audit["external_tool_count"], len(SAMPLE_EXTERNAL_TOOLS))
        self.assertNotIn("mcp__", json.dumps(audit))


if __name__ == "__main__":
    unittest.main()
