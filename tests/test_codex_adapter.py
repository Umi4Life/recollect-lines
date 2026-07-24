import ast
import inspect
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recollect_lines.adaptor import AdapterCapabilities, LaunchSpec
from recollect_lines.adaptor import codex as codex_module
from recollect_lines.adaptor.codex import (
    DEFAULT_COMMAND_PREFIX,
    CodexAdapter,
    CodexUnsupportedPolicy,
    redact_secrets,
)
from recollect_lines.durable_cli_launch import is_durable_launch_terminal
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.service import Broker

FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex.py"


def fake_adapter(grace_period_seconds=2.0, model=None):
    return CodexAdapter(command_prefix=(sys.executable, str(FIXTURE)), grace_period_seconds=grace_period_seconds, model=model)


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class CodexAdapterUnitTests(unittest.TestCase):
    def test_default_command_prefix_is_the_bare_codex_binary(self):
        adapter = CodexAdapter()
        self.assertEqual(adapter.command_prefix, DEFAULT_COMMAND_PREFIX)

    def test_build_command_maps_read_only_to_read_only_sandbox(self):
        adapter = fake_adapter()
        command = adapter.build_command("inspect", "read_only", "/tmp/ws")
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--cd") + 1], "/tmp/ws")
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[-1], "inspect")

    def test_build_command_maps_isolated_worktree_to_workspace_write_sandbox(self):
        adapter = fake_adapter()
        command = adapter.build_command("edit stuff", "isolated_worktree", "/tmp/wt")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("read-only", command)

    def test_build_command_includes_json_flag(self):
        adapter = fake_adapter()
        command = adapter.build_command("inspect", "read_only", "/tmp/ws")
        self.assertIn("--json", command)

    def test_build_command_fails_closed_for_an_unmapped_execution_mode(self):
        adapter = fake_adapter()
        with self.assertRaises(CodexUnsupportedPolicy):
            adapter.build_command("do something", "shared_write", "/tmp/ws")

    def test_build_command_includes_model_when_configured(self):
        adapter = fake_adapter(model="gpt-5")
        command = adapter.build_command("inspect", "read_only", "/tmp/ws")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5")

    def test_build_command_omits_model_flag_by_default(self):
        adapter = fake_adapter()
        command = adapter.build_command("inspect", "read_only", "/tmp/ws")
        self.assertNotIn("--model", command)

    def test_redact_secrets_scrubs_api_key_like_tokens(self):
        text = "call failed with key sk-abcdefgh12345678 in the request"
        self.assertNotIn("sk-abcdefgh12345678", redact_secrets(text))
        self.assertIn("***REDACTED***", redact_secrets(text))

    def test_redact_secrets_scrubs_bearer_tokens(self):
        text = "Authorization: Bearer abcdEFGH12345678"
        self.assertIn("***REDACTED***", redact_secrets(text))

    def test_capabilities_declare_only_what_is_actually_implemented(self):
        self.assertIsInstance(CodexAdapter.capabilities, AdapterCapabilities)
        self.assertTrue(CodexAdapter.capabilities.requires_subprocess)
        self.assertTrue(CodexAdapter.capabilities.supports_process_group_cancellation)
        self.assertFalse(CodexAdapter.capabilities.reports_broker_verified_tests)
        self.assertTrue(CodexAdapter.capabilities.uses_durable_subprocess_runner)

    def test_default_adapter_has_no_durable_runner_until_the_broker_injects_one(self):
        adapter = CodexAdapter()
        self.assertIsNone(adapter.durable_runner)  # bound only by the owning Broker

    def test_build_launch_spec_is_a_provider_neutral_launch_spec(self):
        from recollect_lines.models import TaskRecord

        adapter = fake_adapter()
        record = TaskRecord.new(TaskRequest("inspect", "/tmp/ws", profile="codex", execution_mode="read_only"))
        spec = adapter.build_launch_spec(record, "/tmp/ws", "inspect")
        self.assertIsInstance(spec, LaunchSpec)
        self.assertEqual(spec.cwd, "/tmp/ws")
        self.assertEqual(spec.argv[-1], "inspect")
        self.assertIn("--sandbox", spec.argv)
        self.assertIsNone(spec.env)

    def test_start_raises_without_an_injected_durable_runner(self):
        from recollect_lines.models import TaskRecord

        adapter = fake_adapter()
        record = TaskRecord.new(TaskRequest("inspect", "/tmp/ws", profile="codex", execution_mode="read_only"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                adapter.start(record, Path(tmp))

    def test_check_availability_reports_missing_binary_without_raising(self):
        adapter = CodexAdapter(command_prefix=("definitely-not-a-real-codex-binary-xyz",))
        probe = adapter.check_availability()
        self.assertFalse(probe["available"])
        self.assertEqual(probe["reason"], "cli_not_found")

    def test_check_availability_reports_installed_version(self):
        adapter = fake_adapter()
        probe = adapter.check_availability()
        self.assertTrue(probe["available"])


class CodexNoLegacyPopenLifecycleTests(unittest.TestCase):
    """Required evidence: the default (and only) CodexAdapter lifecycle never
    owns a subprocess.Popen -- launch, wait/poll, killpg, and stdout/stderr
    file creation belong entirely to durable_cli_launch/DurableSubprocessRunner.
    Unlike Cursor, Codex has no gated legacy-Popen transition path at all (see
    the module docstring in adaptor/codex.py for why none is needed).
    """

    def test_module_never_calls_subprocess_popen(self):
        source = inspect.getsource(codex_module)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Popen"
            ):
                self.fail("adaptor.codex must never call subprocess.Popen directly")

    def test_module_does_not_import_subprocess(self):
        self.assertNotIn("subprocess", dir(codex_module))


class CodexBrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, codex_adapter=fake_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task="Inspect fact.txt", **kwargs):
        return self.broker.create(TaskRequest(task, str(self.workspace), profile="codex", **kwargs))

    def test_start_creates_durable_launch_metadata_and_stdout_stderr_artifacts(self):
        record = self.create()
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["adapter"], "codex")
        self.assertEqual(launch["launch_kind"], "durable_subprocess")
        self.assertIsNotNone(launch["durable_launch_id"])
        launch_dir = self.home / "durable_launches" / launch["durable_launch_id"]
        stdout_path = launch_dir / "stdout.log"
        stderr_path = launch_dir / "stderr.log"
        wait_until(lambda: stdout_path.exists() and stdout_path.stat().st_size > 0)
        self.assertTrue(stderr_path.exists())
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        self.assertIn("pid", run_event["metadata"])
        self.assertIn("pgid", run_event["metadata"])
        self.assertEqual(run_event["metadata"]["runtime_description"], "Codex via codex exec")
        self.broker.collect(record.id)

    def test_start_captures_the_jsonl_event_stream_in_the_durable_stdout_artifact(self):
        record = self.create(task="what is the magic number")
        self.broker.start(record.id)
        launch = self.broker.store.get_launch(record.id)
        launch_dir = self.home / "durable_launches" / launch["durable_launch_id"]
        stdout_path = launch_dir / "stdout.log"
        wait_until(lambda: stdout_path.exists() and stdout_path.stat().st_size > 0)
        self.broker.collect(record.id)
        lines = [line for line in stdout_path.read_text().splitlines() if line.strip()]
        events = [json.loads(line) for line in lines]
        self.assertIn("thread.started", {event.get("type") for event in events})
        self.assertIn("turn.completed", {event.get("type") for event in events})

    def test_cancel_terminates_process_group_read_only(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.workspace), profile="codex", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        stderr_path = handle.durable.launch_dir / "stderr.log"
        wait_until(lambda: stderr_path.exists() and b"started" in stderr_path.read_bytes())

        cancelled = self.broker.cancel(record.id, "no longer needed")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        cancel_event = self.broker.store.events(record.id)[-1]
        self.assertTrue(cancel_event["metadata"]["cancellation"]["group_terminated"])
        self.assertIn("SIGTERM", cancel_event["metadata"]["cancellation"]["signals_sent"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(handle.pgid, 0)

    def test_cancel_escalates_to_sigkill_when_process_ignores_sigterm(self):
        self.broker = Broker(self.home, codex_adapter=fake_adapter(grace_period_seconds=0.3))
        record = self.broker.create(TaskRequest("SLEEP_IGNORE_TERM", str(self.workspace), profile="codex", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles[record.id]
        stderr_path = handle.durable.launch_dir / "stderr.log"
        wait_until(lambda: stderr_path.exists() and b"started" in stderr_path.read_bytes())

        cancelled = self.broker.cancel(record.id, "force stop")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        signals_sent = self.broker.store.events(record.id)[-1]["metadata"]["cancellation"]["signals_sent"]
        self.assertEqual(signals_sent, ["SIGTERM", "SIGKILL"])

    def test_collect_parses_agent_message_as_summary(self):
        record = self.create(task="what is the magic number")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertIn("42", result["summary"])
        self.assertEqual(result["runtime"]["adapter"], "codex")
        self.assertEqual(result["runtime"]["thread_id"], "thread_fake")
        self.assertIsNotNone(result["runtime"]["usage"])

    def test_collect_parses_structured_agent_message_json(self):
        record = self.create(task="STRUCTURED output please")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertIn("codex-fixture-ok", result["summary"])

    def test_collect_is_defensive_against_a_malformed_leading_line(self):
        record = self.create(task="MALFORMED")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], "partial result despite a malformed line")
        self.assertGreaterEqual(result["runtime"]["malformed_event_lines"], 1)

    def test_collect_with_no_parseable_output_is_succeeded_with_warnings(self):
        record = self.create(task="EMPTY_OUTPUT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED_WITH_WARNINGS)

    def test_turn_failed_is_reported_as_failed(self):
        record = self.create(task="AUTH_ERROR")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["error_category"], "authentication_error")
        self.assertTrue(result["runtime"]["is_error"])

    def test_rate_limit_error_is_classified_distinctly_from_auth_error(self):
        record = self.create(task="RATE_LIMIT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["error_category"], "rate_limit_or_quota_error")

    def test_nonzero_exit_is_reported_as_failed(self):
        record = self.create(task="NONZERO_EXIT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)

    def test_a_clean_turn_completed_followed_by_a_nonzero_exit_is_still_categorized(self):
        record = self.create(task="KILLED_AFTER_RESULT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)
        self.assertIsNotNone(result["runtime"]["error_category"])

    def test_repeated_collect_on_a_terminal_task_is_idempotent(self):
        record = self.create()
        self.broker.start(record.id)
        first = self.broker.collect(record.id)
        second = self.broker.collect(record.id)
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.updated_at, second.updated_at)

    def test_collect_without_a_process_handle_reconciles_via_durable_adoption_not_uncollected(self):
        # Unlike a legacy Popen adapter, a durable Codex launch's outcome is
        # never `uncollected` after a broker restart: the manifest is
        # independent, durable proof a fresh broker can adopt and safely
        # collect from, exactly like Cursor/Claude Code's durable migrations.
        record = self.create(task="what is the magic number")
        self.broker.start(record.id)
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["launch_kind"], "durable_subprocess")
        handle = self.broker._process_handles[record.id]
        wait_until(lambda: is_durable_launch_terminal(handle))
        self.broker._process_handles.pop(record.id, None)  # simulate a broker restart losing in-memory state

        restarted = Broker(self.home, codex_adapter=fake_adapter())
        try:
            reconciled = restarted.reconcile(record.id)
            detail = restarted.reconcile_detail(record.id)
            self.assertIn(detail["outcome"], {"adopted_running", "adopted_terminal_collectable"})
            self.assertEqual(detail["launch_id"], launch["durable_launch_id"])
            self.assertNotEqual(reconciled.state, TaskState.UNCOLLECTED)

            completed = restarted.collect(record.id)
            self.assertEqual(completed.state, TaskState.SUCCEEDED)
            result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
            self.assertEqual(result["runtime"]["adapter"], "codex")
            self.assertIsNotNone(result["summary"])
        finally:
            restarted.close()

    def test_long_task_survives_broker_restart_is_reconciled_adopted_and_collected(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.workspace), profile="codex", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles.pop(record.id)  # simulate a broker restart losing in-memory state
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["launch_kind"], "durable_subprocess")

        restarted = Broker(self.home, codex_adapter=fake_adapter())
        try:
            reconciled = restarted.reconcile(record.id)
            self.assertEqual(reconciled.state, TaskState.RUNNING)
            detail = restarted.reconcile_detail(record.id)
            self.assertEqual(detail["outcome"], "adopted_running")
            self.assertEqual(detail["launch_id"], launch["durable_launch_id"])
            self.assertIn(record.id, restarted._adopted_durable_handles)

            cancelled = restarted.cancel(record.id, "test cleanup")
            self.assertEqual(cancelled.state, TaskState.CANCELLED)
            with self.assertRaises(ProcessLookupError):
                os.killpg(handle.pgid, 0)

            # cancel() already transitioned this task to a terminal state;
            # collect() on an already-terminal task is idempotent (returns the
            # same durable record) rather than re-running collection.
            completed = restarted.collect(record.id)
            self.assertEqual(completed.state, TaskState.CANCELLED)
        finally:
            try:
                os.killpg(handle.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            restarted.close()


class CodexUnsupportedPolicyBrokerTests(unittest.TestCase):
    def test_broker_start_propagates_unsupported_policy_rather_than_launching(self):
        adapter = fake_adapter()
        with self.assertRaises(CodexUnsupportedPolicy):
            adapter.build_command("do something", "shared_write", "/tmp/ws")


if __name__ == "__main__":
    unittest.main()
