"""Runtime capability contract tests.

Hermetic: no subprocess CLIs or provider HTTP calls -- exercises the
descriptor registry, discovery serialization, delegate-time validation, and
launch-prompt honesty notice purely against in-process fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines.agent_profiles import AgentProfileError
from recollect_lines.capability_contract import (
    MaterializationOwner,
    OutputKind,
    SYNTHETIC_CONTRACT,
    TEXT_SYNTHESIS_CONTRACT,
    WORKTREE_CAPABLE_CONTRACT,
    describe_unsupported_execution_mode,
    materialization_prompt_notice,
)
from recollect_lines.discovery import discover_runtimes
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.runtime_registry import DEFAULT_RUNTIME_REGISTRY
from recollect_lines.service import Broker

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "fake_openai_server.py"
_spec = importlib.util.spec_from_file_location("fake_openai_server", FIXTURE_SERVER)
assert _spec and _spec.loader
_fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake)
FakeOpenAiServer = _fake.FakeOpenAiServer
provider_document = _fake.provider_document


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], cwd=path)
    _run_git(["config", "user.email", "test@example.com"], cwd=path)
    _run_git(["config", "user.name", "Test"], cwd=path)
    (path / "file.txt").write_text("original\n")
    _run_git(["add", "-A"], cwd=path)
    _run_git(["commit", "-q", "-m", "initial"], cwd=path)
    return path


class DescriptorContractTests(unittest.TestCase):
    """Every current runtime advertises an honest, queryable contract."""

    def test_openai_compatible_is_text_synthesis_and_never_owns_a_worktree(self):
        contract = DEFAULT_RUNTIME_REGISTRY.get("openai_compatible").capability_contract
        self.assertEqual(contract, TEXT_SYNTHESIS_CONTRACT)
        self.assertEqual(contract.output_kind, OutputKind.TEXT_SYNTHESIS)
        self.assertFalse(contract.owns_worktree)
        self.assertFalse(contract.mutates_workspace)
        self.assertEqual(contract.materialization_owner, MaterializationOwner.PARENT_APPLIES_TEXT)
        self.assertTrue(contract.parent_materialization_required)

    def test_cli_runtimes_own_worktrees_and_still_require_parent_materialization(self):
        for name in ("opencode", "claude_code", "codex", "cursor"):
            with self.subTest(runtime=name):
                contract = DEFAULT_RUNTIME_REGISTRY.get(name).capability_contract
                self.assertEqual(contract, WORKTREE_CAPABLE_CONTRACT)
                self.assertEqual(contract.output_kind, OutputKind.WORKSPACE_MUTATION)
                self.assertTrue(contract.owns_worktree)
                self.assertTrue(contract.mutates_workspace)
                self.assertEqual(contract.materialization_owner, MaterializationOwner.PARENT_MERGES_BROKER_WORKTREE)
                self.assertTrue(contract.parent_materialization_required)

    def test_mock_owns_a_worktree_mechanically_but_never_writes_files(self):
        contract = DEFAULT_RUNTIME_REGISTRY.get("mock").capability_contract
        self.assertEqual(contract, SYNTHETIC_CONTRACT)
        self.assertTrue(contract.owns_worktree)
        self.assertFalse(contract.mutates_workspace)

    def test_contract_is_json_serializable_dict(self):
        for descriptor in DEFAULT_RUNTIME_REGISTRY.descriptors():
            with self.subTest(runtime=descriptor.name):
                payload = descriptor.capability_contract.as_dict()
                json.dumps(payload)
                self.assertIn("materialization_note", payload)
                self.assertIsInstance(payload["output_kind"], str)


class DiagnosticMessageTests(unittest.TestCase):
    """Delegate-time rejection must name alternatives and materialization ownership."""

    def test_unsupported_mode_message_names_alternatives_and_ownership(self):
        message = describe_unsupported_execution_mode(
            DEFAULT_RUNTIME_REGISTRY, "openai_compatible", "isolated_worktree",
        )
        self.assertIn("does not permit mode isolated_worktree", message)
        self.assertIn("never owns a", message)
        self.assertIn("parent", message.lower())
        for runtime in ("claude_code", "codex", "cursor", "opencode"):
            self.assertIn(runtime, message)

    def test_no_alternatives_reported_honestly(self):
        message = describe_unsupported_execution_mode(
            DEFAULT_RUNTIME_REGISTRY, "mock", "not_a_real_mode",
        )
        self.assertIn("none currently registered", message)


class PromptHonestyTests(unittest.TestCase):
    def test_text_synthesis_contract_gets_a_prompt_notice(self):
        notice = materialization_prompt_notice(TEXT_SYNTHESIS_CONTRACT)
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("openai_compatible", notice)
        self.assertIn("never owns", notice)

    def test_worktree_capable_contract_gets_no_notice(self):
        self.assertIsNone(materialization_prompt_notice(WORKTREE_CAPABLE_CONTRACT))
        self.assertIsNone(materialization_prompt_notice(SYNTHETIC_CONTRACT))


class BrokerEarlyValidationTests(unittest.TestCase):
    """No false task success: an unsupported mode is rejected before the task is ever queued."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_raw_execution_mode_rejection_is_actionable(self):
        with self.assertRaises(ValueError) as ctx:
            self.broker.create(TaskRequest(
                "nope", str(self.workspace), execution_mode="isolated_worktree", profile="openai_compatible",
            ))
        message = str(ctx.exception)
        self.assertIn("does not permit mode isolated_worktree", message)
        self.assertIn("parent", message.lower())
        self.assertIn("claude_code", message)

    def test_agent_profile_resolution_rejects_incompatible_runtime_with_diagnostics(self):
        # implementation-worker defaults to execution_mode=isolated_worktree, which
        # openai_compatible's policy does not permit; no explicit override is given.
        with self.assertRaises(AgentProfileError) as ctx:
            self.broker.create(TaskRequest(
                "implement it",
                str(self.workspace),
                runtime="openai_compatible",
                agent_profile="implementation-worker",
            ))
        message = str(ctx.exception)
        self.assertIn("openai_compatible", message)
        self.assertIn("does not permit mode isolated_worktree", message)

    def test_no_task_is_persisted_for_a_rejected_request(self):
        try:
            self.broker.create(TaskRequest(
                "nope", str(self.workspace), execution_mode="isolated_worktree", profile="openai_compatible",
            ))
        except ValueError:
            pass
        self.assertEqual(self.broker.store.list(), [])


class NormalReadOnlySynthesisTests(unittest.TestCase):
    """openai_compatible in its one permitted mode: honest text synthesis, no workspace touched."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.server = FakeOpenAiServer()
        self.server.start()
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps(provider_document(self.server.base_url)) + "\n")
        self.broker = Broker(
            self.home, providers_config=config_path,
            environ={"TEST_OPENAI_API_KEY": "sk-testsecret1234567890"},
        )

    def tearDown(self):
        self.broker.close()
        self.server.stop()
        self.tempdir.cleanup()

    def test_read_only_task_succeeds_and_prompt_carries_an_honest_materialization_notice(self):
        record = self.broker.create(TaskRequest("What is 2+2?", "/repo", profile="openai_compatible", provider="local"))
        self.broker.start(record.id)
        composed = json.loads((self.broker.store.artifacts / record.id / "composed_prompt.json").read_text())
        self.assertIn("materialization_notice", composed)
        self.assertIn("never owns", composed["materialization_notice"])
        self.assertIn(composed["materialization_notice"], composed["composed_prompt"])
        time.sleep(0.3)
        collected = self.broker.collect(record.id)
        self.assertEqual(collected.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertIn("answer for:", result["summary"])


class CompatibleImplementationWorktreeRuntimeTests(unittest.TestCase):
    """A worktree-capable runtime (mock) actually gets a broker-owned worktree in
    isolated_worktree mode, matching its capability_contract, with no honesty
    notice injected (its own tool loop, not text synthesis)."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.source = _init_repo(Path(self.tempdir.name) / "source")
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_mock_isolated_worktree_task_gets_a_broker_owned_worktree(self):
        contract = self.broker.runtime_registry.get("mock").capability_contract
        self.assertTrue(contract.owns_worktree)
        record = self.broker.create(TaskRequest("edit files", str(self.source), execution_mode="isolated_worktree"))
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        expected_worktree = str(self.broker.workspaces.worktree_path(record.id))
        lease = self.broker.store.get_lease(record.id)
        self.assertEqual(lease["worktree_path"], expected_worktree)
        composed_path = self.broker.store.artifacts / record.id / "composed_prompt.json"
        if composed_path.is_file():
            composed = json.loads(composed_path.read_text())
            self.assertNotIn("Runtime notice", composed["composed_prompt"])
        self.broker.complete(record.id, "mock summary")


class DiscoveryAndRedactionTests(unittest.TestCase):
    """Capability data surfaces through discovery without leaking secrets."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.server = FakeOpenAiServer()
        self.server.start()
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps(provider_document(self.server.base_url)) + "\n")
        self.secret = "sk-testsecret1234567890"
        self.broker = Broker(self.home, providers_config=config_path, environ={"TEST_OPENAI_API_KEY": self.secret})

    def tearDown(self):
        self.broker.close()
        self.server.stop()
        self.tempdir.cleanup()

    def test_discover_capabilities_includes_capability_contract_per_runtime(self):
        payload = self.broker.discover_capabilities()
        by_name = {entry["name"]: entry for entry in payload["runtimes"]}
        self.assertEqual(
            by_name["openai_compatible"]["capability_contract"]["materialization_owner"],
            "parent_applies_text",
        )
        self.assertFalse(by_name["openai_compatible"]["capability_contract"]["owns_worktree"])
        for name in ("mock", "opencode", "claude_code", "codex", "cursor"):
            self.assertTrue(by_name[name]["capability_contract"]["owns_worktree"], name)

    def test_discovery_payload_never_leaks_the_configured_secret(self):
        payload = self.broker.discover_capabilities()
        serialized = json.dumps(payload)
        self.assertNotIn(self.secret, serialized)

    def test_discover_runtimes_entries_stay_json_serializable_with_contract(self):
        entries = discover_runtimes(
            subprocess_adapters=self.broker.subprocess_adapters,
            direct_api_runtime=self.broker.direct_api_runtime,
        )
        serialized = json.dumps(entries)
        self.assertGreater(len(entries), 0)
        self.assertIn("capability_contract", serialized)


if __name__ == "__main__":
    unittest.main()
