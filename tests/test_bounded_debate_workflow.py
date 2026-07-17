"""Hermetic end-to-end tests for the bounded debate reference workflow (Wave 5 / PR 15)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.bounded_debate_workflow import (
    PHASE_MATERIALIZATION,
    PHASE_VALIDATION,
    apply_parent_materialization,
    parse_bounded_debate_plan,
    run_bounded_debate_workflow,
    validate_synthesis_output,
)
from recollect_lines.claude_code_adapter import ClaudeCodeAdapter
from recollect_lines.codex_adapter import CodexAdapter
from recollect_lines.models import ProfilePolicy, TaskRequest
from recollect_lines.service import Broker

FIXTURE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
FIXTURE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"
FIXTURE_OPENAI = Path(__file__).parent / "fixtures" / "fake_openai_server.py"
_spec = importlib.util.spec_from_file_location("fake_openai_server", FIXTURE_OPENAI)
assert _spec and _spec.loader
_fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake)
FakeOpenAiServer = _fake.FakeOpenAiServer
provider_document = _fake.provider_document


def fake_codex_adapter(**kwargs):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE_CODEX)), grace_period_seconds=2.0, **kwargs)


def fake_claude_adapter(**kwargs):
    return ClaudeCodeAdapter(command_prefix=(sys.executable, str(FIXTURE_CLAUDE)), grace_period_seconds=2.0, **kwargs)


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], cwd=path)
    _run_git(["config", "user.email", "test@example.com"], cwd=path)
    _run_git(["config", "user.name", "Bounded Debate Tests"], cwd=path)
    (path / "README.md").write_text("# fixture repo\n")
    _run_git(["add", "README.md"], cwd=path)
    _run_git(["commit", "-q", "-m", "init"], cwd=path)
    return path


def _base_plan(workspace: str, *, synthesis_task: str, acceptance: str, **overrides) -> dict:
    plan = {
        "workspace": workspace,
        "external_root_id": "bdw-test-session",
        "acceptance_criteria": acceptance,
        "opening_positions": [
            {"id": "alice", "profile": "claude_code", "task": "SLEEP_BRIEF opening alice"},
            {"id": "bob", "profile": "codex", "task": "SLEEP_BRIEF opening bob"},
        ],
        "rebuttals": [
            {
                "id": "alice-rebuttal",
                "profile": "claude_code",
                "task": "SLEEP_BRIEF rebuttal alice",
                "responds_to": "bob",
            },
        ],
        "synthesis": {
            "id": "synthesis",
            "profile": "openai_compatible",
            "provider": "local",
            "task": synthesis_task,
        },
        "materialization": {"enabled": False},
        "bounds": {"poll_timeout_seconds": 10},
    }
    plan.update(overrides)
    return plan


class BoundedDebateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = _init_repo(Path(self.tempdir.name) / "source")
        self.server = FakeOpenAiServer()
        self.server.start()
        config_path = Path(self.tempdir.name) / "providers.json"
        config_path.write_text(json.dumps(provider_document(self.server.base_url)) + "\n")
        mock_policy = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)
        self.broker = Broker(
            self.home,
            profiles={"mock": mock_policy},
            providers_config=config_path,
            environ={"TEST_OPENAI_API_KEY": "sk-testsecret1234567890"},
            codex_adapter=fake_codex_adapter(),
            claude_code_adapter=fake_claude_adapter(),
        )

    def tearDown(self):
        self.broker.close()
        self.server.stop()
        self.tempdir.cleanup()

    def test_successful_bounded_flow_materialization_disabled(self):
        plan = _base_plan(
            str(self.workspace),
            synthesis_task="Produce a reconciled recommendation for the debate.",
            acceptance="reconciled recommendation",
        )
        result = run_bounded_debate_workflow(self.broker, plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["external_root_id"], "bdw-test-session")
        self.assertFalse(result["materialization"]["enabled"])
        self.assertFalse(result["materialization"]["attempted"])
        phase_names = [phase["phase"] for phase in result["phases"]]
        self.assertEqual(
            phase_names,
            ["opening_positions", "rebuttals", "synthesis", PHASE_VALIDATION, PHASE_MATERIALIZATION],
        )
        self.assertIn("never owns", result["synthesis_capability_note"])
        tree = self.broker.task_tree_by_external_root("bdw-test-session")
        self.assertGreaterEqual(len(tree["tasks"]), 4)
        for participant_id in ("alice", "bob", "alice-rebuttal", "synthesis"):
            collected = result["participants_collected"][participant_id]
            self.assertEqual(collected["external_root_id"], "bdw-test-session")
            self.assertIn(collected["state"], {"succeeded", "succeeded_with_warnings"})

    def test_terminal_child_failure_stops_before_later_phases(self):
        plan = _base_plan(
            str(self.workspace),
            synthesis_task="reconciled recommendation",
            acceptance="reconciled recommendation",
        )
        plan["opening_positions"][0]["task"] = "NONZERO_EXIT opening alice"
        result = run_bounded_debate_workflow(self.broker, plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["phase"], "opening_positions")
        self.assertEqual(result["failure"]["status"], "failed")
        phase_names = [phase["phase"] for phase in result["phases"]]
        self.assertEqual(phase_names, ["opening_positions"])
        self.assertNotIn("synthesis", result["participants_collected"])

    def test_contract_failure_surfaces_on_validation(self):
        plan = _base_plan(
            str(self.workspace),
            synthesis_task="META_FORMAT_CHOICE synthesize debate",
            acceptance="reconciled recommendation",
        )
        plan["synthesis"]["result_schema"] = "review-findings"
        result = run_bounded_debate_workflow(self.broker, plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["phase"], PHASE_VALIDATION)
        self.assertEqual(result["failure"]["status"], "validation_failed")
        self.assertIn("unsatisfied_fallback", result["failure"]["validation"]["reasons"][0])

    def test_validation_failure_when_acceptance_criteria_missing(self):
        plan = _base_plan(
            str(self.workspace),
            synthesis_task="Produce a short synthesis.",
            acceptance="must-include-impossible-token-zzzz",
        )
        result = run_bounded_debate_workflow(self.broker, plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["status"], "validation_failed")
        self.assertFalse(result["materialization"])

    def test_parent_materialization_dry_run_is_bounded_and_parent_owned(self):
        report = apply_parent_materialization(
            self.workspace,
            "docs/synthesis.md",
            "parent-owned synthesis text",
            dry_run=True,
        )
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["applied"])
        self.assertFalse(report["provider_wrote_files"])
        self.assertEqual(report["materialization_owner"], "parent_applies_text")
        self.assertFalse((self.workspace / "docs" / "synthesis.md").exists())

    def test_materialization_enabled_applies_after_validation(self):
        plan = _base_plan(
            str(self.workspace),
            synthesis_task="reconciled recommendation final text",
            acceptance="reconciled recommendation",
        )
        plan["materialization"] = {
            "enabled": True,
            "relative_path": "docs/debate-synthesis.md",
            "dry_run": False,
        }
        result = run_bounded_debate_workflow(self.broker, plan)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["materialization"]["applied"])
        target = self.workspace / "docs" / "debate-synthesis.md"
        self.assertTrue(target.is_file())
        self.assertIn("reconciled recommendation", target.read_text())

    def test_parse_rejects_escape_path(self):
        with self.assertRaises(ValueError):
            apply_parent_materialization(self.workspace, "../escape.md", "text", dry_run=True)

    def test_validate_synthesis_output_unit(self):
        plan = parse_bounded_debate_plan(_base_plan("/repo", synthesis_task="x", acceptance="needle"))
        validation = validate_synthesis_output(plan, {"contract_status": "satisfied", "summary": "has needle here"})
        self.assertTrue(validation["passed"])
        bad = validate_synthesis_output(plan, {"contract_status": "unsatisfied_fallback", "summary": "nope"})
        self.assertFalse(bad["passed"])


if __name__ == "__main__":
    unittest.main()
