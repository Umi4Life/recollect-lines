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
from recollect_lines.adaptor import opencode as opencode_module
from recollect_lines.adaptor.opencode import DEFAULT_COMMAND_PREFIX, OpenCodeAdapter
from recollect_lines.durable_cli_launch import is_durable_launch_terminal
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.service import Broker

FIXTURE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def fake_adapter(grace_period_seconds=2.0):
    return OpenCodeAdapter(command_prefix=(sys.executable, str(FIXTURE)), grace_period_seconds=grace_period_seconds)


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class OpenCodeAdapterUnitTests(unittest.TestCase):
    def test_default_command_matches_the_known_opencode_invocation(self):
        adapter = OpenCodeAdapter()
        self.assertEqual(adapter.command_prefix, DEFAULT_COMMAND_PREFIX)
        command = adapter.build_command("/repo", "do the thing")
        self.assertEqual(
            command,
            ["npx", "--yes", "opencode-ai@1.17.18", "run", "--pure", "--format", "json", "--dir", "/repo", "do the thing"],
        )

    def test_injected_command_prefix_still_requests_json_format_and_dir(self):
        adapter = fake_adapter()
        command = adapter.build_command("/some/workspace", "inspect tests")
        self.assertIn("--format", command)
        self.assertIn("json", command[command.index("--format") + 1 :])
        self.assertIn("--dir", command)
        self.assertEqual(command[command.index("--dir") + 1], "/some/workspace")
        self.assertEqual(command[-1], "inspect tests")

    def test_mock_and_opencode_adapters_report_distinct_capabilities(self):
        from recollect_lines.service import MockAdapter

        self.assertIsInstance(MockAdapter.capabilities, AdapterCapabilities)
        self.assertIsInstance(OpenCodeAdapter.capabilities, AdapterCapabilities)
        self.assertFalse(MockAdapter.capabilities.requires_subprocess)
        self.assertTrue(OpenCodeAdapter.capabilities.requires_subprocess)
        self.assertFalse(OpenCodeAdapter.capabilities.reports_broker_verified_tests)

    def test_capabilities_declare_only_what_is_actually_implemented(self):
        self.assertTrue(OpenCodeAdapter.capabilities.supports_process_group_cancellation)
        self.assertTrue(OpenCodeAdapter.capabilities.uses_durable_subprocess_runner)

    def test_default_adapter_has_no_durable_runner_until_the_broker_injects_one(self):
        adapter = OpenCodeAdapter()
        self.assertIsNone(adapter.durable_runner)  # bound only by the owning Broker

    def test_build_launch_spec_is_a_provider_neutral_launch_spec(self):
        from recollect_lines.models import TaskRecord

        adapter = fake_adapter()
        record = TaskRecord.new(TaskRequest("inspect tests", "/tmp/ws", profile="opencode", execution_mode="read_only"))
        spec = adapter.build_launch_spec(record, "/tmp/ws", "inspect tests")
        self.assertIsInstance(spec, LaunchSpec)
        self.assertEqual(spec.cwd, "/tmp/ws")
        self.assertEqual(spec.argv[-1], "inspect tests")
        self.assertIn("--dir", spec.argv)
        self.assertIsNone(spec.env)

    def test_build_launch_spec_requests_the_events_jsonl_stdout_artifact(self):
        # OpenCode's terminal stdout is its JSONL event stream, and this
        # codebase's established public artifact name for that stream is
        # `events.jsonl` (RFC-001), not the generic durable default of
        # `stdout.log` -- see adaptor/opencode.py's module docstring.
        from recollect_lines.models import TaskRecord

        adapter = fake_adapter()
        record = TaskRecord.new(TaskRequest("inspect tests", "/tmp/ws", profile="opencode", execution_mode="read_only"))
        spec = adapter.build_launch_spec(record, "/tmp/ws", "inspect tests")
        self.assertEqual(spec.stdout_artifact_name, "events.jsonl")

    def test_start_raises_without_an_injected_durable_runner(self):
        from recollect_lines.models import TaskRecord

        adapter = fake_adapter()
        record = TaskRecord.new(TaskRequest("inspect tests", "/tmp/ws", profile="opencode", execution_mode="read_only"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                adapter.start(record, Path(tmp))


class OpenCodeNoLegacyPopenLifecycleTests(unittest.TestCase):
    """Required evidence: the default (and only) OpenCodeAdapter lifecycle never
    owns a subprocess.Popen -- launch, wait/poll, killpg, and stdout/stderr
    file creation belong entirely to durable_cli_launch/DurableSubprocessRunner.
    Unlike Cursor, OpenCode has no gated legacy-Popen transition path at all
    (see the module docstring in adaptor/opencode.py for why none is needed).
    """

    def test_module_never_calls_subprocess_popen(self):
        source = inspect.getsource(opencode_module)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Popen"
            ):
                self.fail("adaptor.opencode must never call subprocess.Popen directly")

    def test_module_does_not_import_subprocess(self):
        self.assertNotIn("subprocess", dir(opencode_module))


class OpenCodeBrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, opencode_adapter=fake_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task="Inspect tests", **kwargs):
        return self.broker.create(TaskRequest(task, str(self.workspace), profile="opencode", **kwargs))

    def test_start_creates_durable_launch_metadata_and_events_stderr_artifacts(self):
        record = self.create()
        started = self.broker.start(record.id)
        self.assertEqual(started.state, TaskState.RUNNING)
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["adapter"], "opencode")
        self.assertEqual(launch["launch_kind"], "durable_subprocess")
        self.assertIsNotNone(launch["durable_launch_id"])
        # OpenCode's durable-captured stdout artifact must be the established
        # public `events.jsonl` name (RFC-001), never the generic durable
        # `stdout.log` default.
        self.assertEqual(launch["events_artifact"], "events.jsonl")
        launch_dir = self.home / "durable_launches" / launch["durable_launch_id"]
        events_path = launch_dir / "events.jsonl"
        stderr_path = launch_dir / "stderr.log"
        wait_until(lambda: events_path.exists() and events_path.stat().st_size > 0)
        self.assertTrue(stderr_path.exists())
        self.assertFalse((launch_dir / "stdout.log").exists())
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        self.assertIn("pid", run_event["metadata"])
        self.assertIn("pgid", run_event["metadata"])
        self.assertEqual(run_event["metadata"]["runtime_description"], "OpenCode via opencode run --pure --format json")
        self.assertEqual(run_event["metadata"]["events_artifact"], "events.jsonl")
        self.broker.collect(record.id)

    def test_collect_finds_last_text_event_as_summary_and_marks_runtime_reported(self):
        record = self.create()
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], f"All checks passed for {self.workspace}")
        self.assertFalse(result["runtime"]["verification"]["tests_broker_verified"])
        self.assertEqual(result["runtime"]["verification"]["source"], "runtime_reported")

    def test_collect_reads_summary_from_the_real_cli_nested_part_shape(self):
        record = self.create(task="NESTED_PART")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], f"nested summary for {self.workspace}")

    def test_collect_is_defensive_against_malformed_jsonl_lines(self):
        record = self.create(task="MALFORMED")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], "partial result despite malformed line")
        self.assertGreaterEqual(result["runtime"]["malformed_event_lines"], 1)

    def test_nonzero_exit_is_reported_as_failed_not_broker_verified(self):
        record = self.create(task="NONZERO_EXIT")
        self.broker.start(record.id)
        completed = self.broker.collect(record.id)
        self.assertEqual(completed.state, TaskState.FAILED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["runtime"]["exit_code"], 1)
        self.assertFalse(result["runtime"]["verification"]["tests_broker_verified"])

    def test_repeated_collect_on_a_terminal_task_is_idempotent(self):
        record = self.create()
        self.broker.start(record.id)
        first = self.broker.collect(record.id)
        second = self.broker.collect(record.id)
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.updated_at, second.updated_at)

    def test_cancel_terminates_process_group_for_a_ready_long_running_fixture(self):
        record = self.create(task="SLEEP")
        self.broker.start(record.id)
        launch = self.broker.store.get_launch(record.id)
        launch_dir = self.home / "durable_launches" / launch["durable_launch_id"]
        events_path = launch_dir / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))
        run_event = next(e for e in self.broker.store.events(record.id) if e["type"] == "task.running")
        pgid = run_event["metadata"]["pgid"]

        cancelled = self.broker.cancel(record.id, "no longer needed")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        cancel_event = self.broker.store.events(record.id)[-1]
        self.assertEqual(cancel_event["metadata"]["cancellation"]["group_terminated"], True)
        self.assertIn("SIGTERM", cancel_event["metadata"]["cancellation"]["signals_sent"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)

    def test_cancel_escalates_to_sigkill_when_process_ignores_sigterm(self):
        self.broker = Broker(self.home, opencode_adapter=fake_adapter(grace_period_seconds=0.3))
        record = self.create(task="SLEEP_IGNORE_TERM")
        self.broker.start(record.id)
        launch = self.broker.store.get_launch(record.id)
        launch_dir = self.home / "durable_launches" / launch["durable_launch_id"]
        events_path = launch_dir / "events.jsonl"
        self.assertTrue(wait_until(lambda: events_path.exists() and b"started" in events_path.read_bytes()))

        cancelled = self.broker.cancel(record.id, "force stop")

        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        cancel_event = self.broker.store.events(record.id)[-1]
        signals_sent = cancel_event["metadata"]["cancellation"]["signals_sent"]
        self.assertEqual(signals_sent, ["SIGTERM", "SIGKILL"])
        self.assertTrue(cancel_event["metadata"]["cancellation"]["group_terminated"])

    def test_collect_without_a_process_handle_reconciles_via_durable_adoption_not_uncollected(self):
        # A durable OpenCode launch's outcome is never `uncollected` after a
        # broker restart: the manifest is independent, durable proof a fresh
        # broker can adopt and safely collect from, exactly like the merged
        # Cursor/Claude Code/Codex durable migrations.
        record = self.create()
        self.broker.start(record.id)
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["launch_kind"], "durable_subprocess")
        handle = self.broker._process_handles[record.id]
        wait_until(lambda: is_durable_launch_terminal(handle))
        self.broker._process_handles.pop(record.id, None)  # simulate a broker restart losing in-memory state

        restarted = Broker(self.home, opencode_adapter=fake_adapter())
        try:
            reconciled = restarted.reconcile(record.id)
            detail = restarted.reconcile_detail(record.id)
            self.assertIn(detail["outcome"], {"adopted_running", "adopted_terminal_collectable"})
            self.assertEqual(detail["launch_id"], launch["durable_launch_id"])
            self.assertNotEqual(reconciled.state, TaskState.UNCOLLECTED)

            completed = restarted.collect(record.id)
            self.assertEqual(completed.state, TaskState.SUCCEEDED)
            result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
            self.assertEqual(result["runtime"]["adapter"], "opencode")
            self.assertIsNotNone(result["summary"])
        finally:
            restarted.close()

    def test_long_task_survives_broker_restart_is_reconciled_adopted_and_collected(self):
        record = self.broker.create(TaskRequest("SLEEP", str(self.workspace), profile="opencode", execution_mode="read_only"))
        self.broker.start(record.id)
        handle = self.broker._process_handles.pop(record.id)  # simulate a broker restart losing in-memory state
        launch = self.broker.store.get_launch(record.id)
        self.assertEqual(launch["launch_kind"], "durable_subprocess")

        restarted = Broker(self.home, opencode_adapter=fake_adapter())
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


if __name__ == "__main__":
    unittest.main()
