"""Command redaction, plus a couple of `reconcile_pending()`/CLI smoke tests.

Phase 5B's original comprehensive legacy-Popen restart-reconciliation
coverage for OpenCode (dead-process reap, live-process recovery_required,
crash-during-PREPARING, idempotent collect, cancel-via-persisted-pgid) is
retired here: OpenCode is durable by default now (RFC-004 durable-opencode
slice, the last of the four production subprocess adapters to migrate), so
those scenarios' legacy-specific expected outcomes (unconditional `failed`/
`recovery_required`) no longer hold -- a durable launch is safely *adopted*
across a broker restart instead. That behavior now has its own coverage in
tests/test_opencode_adapter.py (durable metadata, restart adoption,
cancellation) and in tests/test_phase_7c3.py's provider-agnostic durable
reconciliation suite (FixtureDurableAdapter). `reconcile_pending()` and the
CLI `reconcile`/`reconcile-all` surfaces have no other test coverage, so a
trimmed pair of durable-adoption-aware smoke tests for those is kept below.

Every test that spawns a real OS process group cleans it up (SIGKILL) in a
finally/tearDown, whether the assertions above it passed or failed.
"""

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines import cli
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.adaptor.process import redact_command
from recollect_lines.service import Broker
from recollect_lines.durable_cli_launch import TERMINAL_LAUNCH_STATES, wait_for_durable_launch_terminal
from recollect_lines.durable_runner import load_launch_record

FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def fake_adapter(grace_period_seconds=2.0):
    from recollect_lines.adaptor.opencode import OpenCodeAdapter

    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE)), grace_period_seconds=grace_period_seconds)


def wait_until(predicate, timeout=5.0, interval=0.05):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def run_git(args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], cwd=path)
    run_git(["config", "user.email", "test@example.com"], cwd=path)
    run_git(["config", "user.name", "Test"], cwd=path)
    (path / "file.txt").write_text("original\n")
    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-q", "-m", "initial"], cwd=path)
    return path


def kill_pgid(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def durable_events_path(broker: Broker, task_id: str) -> Path:
    launch = broker.store.get_launch(task_id)
    return broker.store.home / "durable_launches" / launch["durable_launch_id"] / "events.jsonl"


def durable_manifest_path(broker: Broker, task_id: str) -> Path:
    return durable_events_path(broker, task_id).parent / "manifest.json"


class RedactCommandTests(unittest.TestCase):
    def test_redacts_only_the_value_following_a_secret_looking_flag(self):
        command = ["npx", "run", "--api-key", "sk-super-secret", "--dir", "/workspace", "do the thing"]
        redacted = redact_command(command)
        self.assertEqual(redacted[redacted.index("--api-key") + 1], "***REDACTED***")
        self.assertEqual(redacted[redacted.index("--dir") + 1], "/workspace")
        self.assertEqual(redacted[-1], "do the thing")
        self.assertEqual(command[command.index("--api-key") + 1], "sk-super-secret")  # original untouched

    def test_redacts_the_single_token_flag_equals_value_form(self):
        redacted = redact_command(["npx", "run", "--api-key=sk-super-secret", "--dir", "/workspace"])
        self.assertEqual(redacted[2], "--api-key=***REDACTED***")
        self.assertEqual(redacted[3], "--dir")
        self.assertEqual(redacted[4], "/workspace")

    def test_a_value_that_looks_like_a_flag_name_does_not_cascade_into_the_next_argument(self):
        # The redacted value itself, and any plain (non-dash) value that happens to
        # contain a marker word, must never be misread as a flag on the next pass.
        redacted = redact_command(["run", "--token", "abc-secret-token", "--dir", "/workspace"])
        self.assertEqual(redacted, ["run", "--token", "***REDACTED***", "--dir", "/workspace"])


class ReconcilePendingAndCliSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.source = init_repo(Path(self.tempdir.name) / "source")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_reconcile_pending_adopts_durable_opencode_tasks_and_leaves_mock_alone(self):
        broker1 = Broker(self.home, opencode_adapter=fake_adapter())
        mock_record = broker1.create(TaskRequest("mock work", str(self.source), execution_mode="read_only", profile="mock"))
        broker1.start(mock_record.id)  # legitimately RUNNING forever until complete() is called; not restart-affected

        dead_record = broker1.create(TaskRequest("Inspect tests", str(self.source), execution_mode="read_only", profile="opencode"))
        broker1.start(dead_record.id)
        dead_handle = broker1._process_handles[dead_record.id]
        self.assertTrue(wait_for_durable_launch_terminal(dead_handle, timeout=5))  # the default fixture finishes quickly on its own

        sleep_record = broker1.create(TaskRequest("SLEEP", str(self.source), execution_mode="read_only", profile="opencode"))
        broker1.start(sleep_record.id)
        events_path = durable_events_path(broker1, sleep_record.id)
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        pgid = broker1._process_handles[sleep_record.id].pgid
        broker1.close()

        broker2 = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            reconciled = {r.id: r for r in broker2.reconcile_pending()}
            self.assertNotIn(mock_record.id, reconciled)
            self.assertEqual(broker2.store.get(mock_record.id).state, TaskState.RUNNING)

            dead_detail = broker2.reconcile_detail(dead_record.id)
            self.assertEqual(dead_detail["outcome"], "adopted_terminal_collectable")
            sleep_detail = broker2.reconcile_detail(sleep_record.id)
            self.assertEqual(sleep_detail["outcome"], "adopted_running")
            self.assertEqual(reconciled[sleep_record.id].state, TaskState.RUNNING)

            collected = broker2.collect(dead_record.id)
            self.assertEqual(collected.state, TaskState.SUCCEEDED)
        finally:
            kill_pgid(pgid)
            manifest_path = durable_manifest_path(broker2, sleep_record.id)
            # The durable supervisor keeps writing (finalizing artifacts, then the
            # terminal manifest) for a moment after its payload's pgid is killed;
            # wait for that write to land so tearDown's TemporaryDirectory.cleanup()
            # can't race a still-running writer inside durable_launches/ (a bare
            # kill + immediate rmtree hit ENOTEMPTY here).
            try:
                supervisor_reached_terminal = wait_until(
                    lambda: load_launch_record(manifest_path).lifecycle_state in TERMINAL_LAUNCH_STATES,
                    timeout=5,
                )
            finally:
                broker2.close()
            self.assertTrue(supervisor_reached_terminal, "durable supervisor did not reach a terminal state before teardown")

    def test_cli_reconcile_and_reconcile_all_smoke(self):
        broker = Broker(self.home, opencode_adapter=fake_adapter())
        record = broker.create(TaskRequest("Inspect tests", str(self.source), execution_mode="read_only", profile="opencode"))
        broker.start(record.id)
        handle = broker._process_handles[record.id]
        self.assertTrue(wait_for_durable_launch_terminal(handle, timeout=5))  # the default fixture finishes quickly on its own
        broker.close()

        exit_code = cli.main(["--home", str(self.home), "reconcile", record.id])
        self.assertEqual(exit_code, 0)

        second = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            self.assertNotEqual(second.store.get(record.id).state, TaskState.UNCOLLECTED)
        finally:
            second.close()

        exit_code = cli.main(["--home", str(self.home), "reconcile-all"])
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
