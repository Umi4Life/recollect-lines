#!/usr/bin/env python3
"""Fixture-only driver for the bounded debate reference workflow (Wave 5 / PR 15)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FIXTURE_OPENAI = ROOT / "tests" / "fixtures" / "fake_openai_server.py"
sys.path.insert(0, str(SRC))

import importlib.util  # noqa: E402

from recollect_lines.bounded_debate_workflow import run_bounded_debate_workflow  # noqa: E402
from recollect_lines.claude_code_adapter import ClaudeCodeAdapter  # noqa: E402
from recollect_lines.codex_adapter import CodexAdapter  # noqa: E402
from recollect_lines.models import ProfilePolicy  # noqa: E402
from recollect_lines.service import Broker  # noqa: E402

_spec = importlib.util.spec_from_file_location("fake_openai_server", FIXTURE_OPENAI)
assert _spec and _spec.loader
_fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake)


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=path)
    _git(["config", "user.email", "fixture@example.com"], cwd=path)
    _git(["config", "user.name", "Fixture Debate"], cwd=path)
    (path / "README.md").write_text("# fixture debate repo\n")
    _git(["add", "README.md"], cwd=path)
    _git(["commit", "-q", "-m", "init"], cwd=path)


def main() -> int:
    server = _fake.FakeOpenAiServer()
    server.start()
    try:
        with tempfile.TemporaryDirectory(prefix="recollect-bdw-fixture-") as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "broker"
            workspace = tmp_path / "repo"
            _init_repo(workspace)
            config_path = tmp_path / "providers.json"
            config_path.write_text(
                json.dumps(_fake.provider_document(server.base_url)) + "\n",
            )
            broker = Broker(
                home,
                profiles={"mock": ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)},
                providers_config=config_path,
                environ={"TEST_OPENAI_API_KEY": "sk-fixture"},
                codex_adapter=CodexAdapter(
                    command_prefix=(sys.executable, str(ROOT / "tests" / "fixtures" / "fake_codex.py")),
                    grace_period_seconds=2.0,
                ),
                claude_code_adapter=ClaudeCodeAdapter(
                    command_prefix=(sys.executable, str(ROOT / "tests" / "fixtures" / "fake_claude.py")),
                    grace_period_seconds=2.0,
                ),
            )
            try:
                plan = {
                    "workspace": str(workspace),
                    "external_root_id": "fixture-bounded-debate",
                    "acceptance_criteria": "reconciled recommendation",
                    "opening_positions": [
                        {"id": "alice", "profile": "claude_code", "task": "SLEEP_BRIEF opening alice"},
                        {"id": "bob", "profile": "codex", "task": "SLEEP_BRIEF opening bob"},
                    ],
                    "rebuttals": [
                        {
                            "id": "alice-rebuttal",
                            "profile": "claude_code",
                            "task": "SLEEP_BRIEF rebuttal",
                            "responds_to": "bob",
                            "relationship": "continues",
                        },
                    ],
                    "synthesis": {
                        "id": "synthesis",
                        "profile": "openai_compatible",
                        "provider": "local",
                        "task": "Write a reconciled recommendation from the debate.",
                    },
                    "materialization": {"enabled": False},
                    "bounds": {"poll_timeout_seconds": 15},
                }
                result = run_bounded_debate_workflow(broker, plan)
                print(json.dumps({"status": result["status"], "workflow_id": result["workflow_id"]}, indent=2))
                return 0 if result["status"] == "completed" else 1
            finally:
                broker.close()
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
