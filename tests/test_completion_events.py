"""Durable global completion-event cursor.

Event-driven completion collection extends this with a
non-blocking pump (Broker._pump_finished_handles(), exercised only through
completion_events_since()) that lets a parent observe a real child process
finishing without ever calling collect() first and without a guessed sleep.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from recollect_lines.models import ProfilePolicy, TaskRequest, TaskState, TERMINAL_STATES
from recollect_lines.adaptor.opencode import OpenCodeAdapter
from recollect_lines.durable_cli_launch import wait_for_durable_launch_terminal
from recollect_lines.service import Broker

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
FAKE_OPENCODE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def fake_opencode_adapter(grace_period_seconds: float = 2.0) -> OpenCodeAdapter:
    """Deterministic stand-in CLI (tests/fixtures/fake_opencode.py) so these tests
    exercise a real OS child process lifecycle, not the synchronous mock adapter.
    """
    return OpenCodeAdapter(command_prefix=(sys.executable, str(FAKE_OPENCODE)), grace_period_seconds=grace_period_seconds)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Tight bounded poll loop -- the hermetic-test analogue of the no-guessed-sleep
    pattern real callers use: keep checking, never sleep for a guessed
    task duration and check once.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class CompletionEventsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        mock_policy = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)
        self.broker = Broker(self.home, profiles={"mock": mock_policy})

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self, task: str = "work", **kwargs):
        return self.broker.create(TaskRequest(task, str(self.workspace), **kwargs))

    def complete_task(self, summary: str = "done"):
        record = self.create()
        self.broker.start(record.id)
        return self.broker.complete(record.id, summary)

    def test_empty_page_preserves_cursor_and_reports_high_water(self):
        page = self.broker.completion_events_since(0)
        self.assertEqual(page["events"], [])
        self.assertEqual(page["next_cursor"], 0)
        self.assertEqual(page["after_event_id"], 0)
        self.assertFalse(page["has_more"])
        self.assertGreaterEqual(page["high_water_mark"], 0)

    def test_terminal_completion_is_observed_in_order(self):
        first = self.complete_task("first")
        second = self.complete_task("second")
        page = self.broker.completion_events_since(0)
        states = [event["state"] for event in page["events"]]
        self.assertEqual(states, ["succeeded", "succeeded"])
        self.assertEqual(page["events"][0]["task_id"], first.id)
        self.assertEqual(page["events"][1]["task_id"], second.id)
        self.assertEqual(page["events"][0]["event_id"], page["events"][0]["event_id"])
        self.assertLess(page["events"][0]["event_id"], page["events"][1]["event_id"])

    def test_duplicate_poll_is_idempotent(self):
        self.complete_task()
        first = self.broker.completion_events_since(0)
        second = self.broker.completion_events_since(0)
        self.assertEqual(first, second)

    def test_advancing_cursor_does_not_re_emit_older_events(self):
        self.complete_task("one")
        self.complete_task("two")
        first_page = self.broker.completion_events_since(0, limit=1)
        self.assertEqual(len(first_page["events"]), 1)
        self.assertTrue(first_page["has_more"])
        second_page = self.broker.completion_events_since(first_page["next_cursor"])
        self.assertEqual(len(second_page["events"]), 1)
        self.assertNotEqual(first_page["events"][0]["task_id"], second_page["events"][0]["task_id"])

    def test_pagination_high_water_and_no_missed_terminal_event(self):
        completed = [self.complete_task(f"task-{index}") for index in range(5)]
        cursor = 0
        seen: list[str] = []
        while True:
            page = self.broker.completion_events_since(cursor, limit=2)
            seen.extend(event["task_id"] for event in page["events"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(seen, [record.id for record in completed])

    def test_concurrent_completions_remain_totally_ordered_by_event_id(self):
        def finish(index: int) -> str:
            local_home = self.home
            workspace = str(self.workspace)
            mock_policy = ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)
            broker = Broker(local_home, profiles={"mock": mock_policy})
            try:
                record = broker.create(TaskRequest(f"parallel-{index}", workspace))
                broker.start(record.id)
                broker.complete(record.id, f"summary-{index}")
                return record.id
            finally:
                broker.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            task_ids = list(pool.map(finish, range(6)))
        page = self.broker.completion_events_since(0, limit=64)
        observed = [event["task_id"] for event in page["events"] if event["task_id"] in task_ids]
        event_ids = [event["event_id"] for event in page["events"] if event["task_id"] in task_ids]
        self.assertEqual(len(observed), len(task_ids))
        self.assertEqual(event_ids, sorted(event_ids))
        self.assertEqual(set(observed), set(task_ids))

    def test_cancelled_timed_out_and_recovery_states_map_honestly(self):
        running = self.create("running")
        self.broker.start(running.id)
        cancelled = self.broker.cancel(running.id, "stop")
        self.assertEqual(cancelled.state, TaskState.CANCELLED)

        sleeper = self.create("sleep")
        self.broker.start(sleeper.id)
        timed_out = self.broker.timeout(sleeper.id)
        self.assertEqual(timed_out.state, TaskState.TIMED_OUT)

        recovery = self.create("recovery")
        self.broker.start(recovery.id)
        self.broker.store.transition(
            recovery.id,
            TaskState.RECOVERY_REQUIRED,
            "needs operator reconciliation",
            {"reason": "test"},
        )

        page = self.broker.completion_events_since(0)
        by_state = {event["state"]: event for event in page["events"]}
        self.assertEqual(by_state["cancelled"]["event_type"], "task.cancelled")
        self.assertEqual(by_state["timed_out"]["event_type"], "task.timed_out")
        self.assertEqual(by_state["recovery_required"]["event_type"], "task.recovery_required")

    def test_restart_persistence_allows_cursor_resume(self):
        completed = self.complete_task("survives restart")
        event_id = self.broker.store.events(completed.id)[-1]["id"]
        self.broker.close()
        reloaded = Broker(self.home, profiles={"mock": ProfilePolicy("mock", frozenset({"read_only", "isolated_worktree"}), 3600, 16)})
        try:
            page = reloaded.completion_events_since(event_id - 1, limit=10)
            self.assertEqual(len(page["events"]), 1)
            self.assertEqual(page["events"][0]["task_id"], completed.id)
            self.assertEqual(page["events"][0]["state"], "succeeded")
        finally:
            reloaded.close()

    def test_root_task_filter_and_lineage_fields(self):
        parent = self.create("parent")
        child = self.create("child", parent_task_id=parent.id, external_root_id="host-session-1")
        self.broker.start(child.id)
        self.broker.complete(child.id, "child done")
        page = self.broker.completion_events_since(0, root_task_id=parent.id)
        self.assertEqual(len(page["events"]), 1)
        event = page["events"][0]
        self.assertEqual(event["task_id"], child.id)
        self.assertEqual(event["root_task_id"], parent.id)
        self.assertEqual(event["parent_task_id"], parent.id)
        self.assertEqual(event["external_root_id"], "host-session-1")

    def test_task_filter_limits_to_one_task(self):
        first = self.complete_task("one")
        self.complete_task("two")
        page = self.broker.completion_events_since(0, task_id=first.id)
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["events"][0]["task_id"], first.id)

    def test_payload_is_compact_without_raw_output_leakage(self):
        completed = self.complete_task("compact summary only")
        page = self.broker.completion_events_since(0)
        event = page["events"][-1]
        encoded = json.dumps(event)
        self.assertNotIn("malformed_event_lines", encoded)
        self.assertNotIn("events.jsonl", encoded)
        self.assertIn("result_summary", event)
        self.assertEqual(event["result_summary"]["summary"], "compact summary only")
        self.assertIn("artifact_count", event)
        self.assertGreaterEqual(event["artifact_count"], 2)

    def test_verification_gate_label_surfaces_on_terminal_collect(self):
        record = self.broker.create(
            TaskRequest("verified", str(self.workspace), verification_policy="required"),
            verify_commands=[[sys.executable, "-c", "print('ok')"]],
        )
        self.broker.start(record.id)
        self.broker.complete(record.id, "verified summary")
        page = self.broker.completion_events_since(0, task_id=record.id)
        gate = page["events"][0]["metadata"]["verification_gate"]
        self.assertEqual(gate["policy"], "required")
        self.assertEqual(gate["label"], "required_verified")

    def test_cli_completion_events_shape(self):
        self.complete_task("cli")
        proc = subprocess.run(
            [sys.executable, "-m", "recollect_lines", "--home", str(self.home), "completion-events", "--limit", "1"],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(SRC_DIR)},
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("next_cursor", payload)
        self.assertIn("high_water_mark", payload)
        self.assertEqual(len(payload["events"]), 1)

    def test_mcp_completion_events_tool(self):
        from tests.test_mcp_server import McpStdioClient

        client = McpStdioClient(self.home)
        try:
            client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
            client.notify("notifications/initialized")
            listed = client.request("tools/list")
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertIn("completion_events", names)
            self.complete_task("mcp")
            response = client.call_tool("completion_events", {"after_event_id": 0, "limit": 5})
            body = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(body["ok"])
            data = body["data"]
            self.assertIn("events", data)
            self.assertTrue(any(event["state"] == "succeeded" for event in data["events"]))
        finally:
            client.close()

    def test_mcp_delegate_and_delegate_batch_return_completion_cursor(self):
        """delegate/delegate_batch fold "record the cursor" into dispatch itself:
        the returned completion_cursor is exactly the baseline a parent should
        pass to completion_events(after_event_id=...) to observe this dispatch's
        tasks finish, no separate round-trip needed.
        """
        from tests.test_mcp_server import McpStdioClient

        client = McpStdioClient(self.home)
        try:
            client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
            client.notify("notifications/initialized")

            delegated = client.call_tool("delegate", {"task": "one", "workspace": str(self.workspace)})
            body = json.loads(delegated["result"]["content"][0]["text"])
            self.assertTrue(body["ok"])
            self.assertIn("completion_cursor", body["data"])
            single_cursor = body["data"]["completion_cursor"]
            self.assertIsInstance(single_cursor, int)

            batched = client.call_tool(
                "delegate_batch",
                {"tasks": [{"task": "two", "workspace": str(self.workspace)}, {"task": "three", "workspace": str(self.workspace)}]},
            )
            batch_body = json.loads(batched["result"]["content"][0]["text"])
            self.assertTrue(batch_body["ok"])
            self.assertIn("completion_cursor", batch_body["data"])
            batch_cursor = batch_body["data"]["completion_cursor"]
            self.assertIsInstance(batch_cursor, int)
            self.assertGreaterEqual(batch_cursor, single_cursor)

            # Nothing dispatched here has completed yet (mock tasks require an
            # explicit complete() call this MCP surface never makes on delegate),
            # so polling from the freshly recorded cursor must be empty, not an
            # error and not a fabricated completion.
            page = client.call_tool("completion_events", {"after_event_id": batch_cursor, "limit": 5})
            page_body = json.loads(page["result"]["content"][0]["text"])
            self.assertTrue(page_body["ok"])
            self.assertEqual(page_body["data"]["events"], [])
        finally:
            client.close()


def kill_pgid(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class CompletionEventsPumpTests(unittest.TestCase):
    """Event-driven completion collection.

    completion_events_since() opportunistically finalizes (non-blocking) any
    real child process this broker instance itself launched and still holds a
    handle for. These tests exercise that pump against tests/fixtures/fake_opencode.py
    -- a real, bounded set of OS child-process lifecycle transitions -- never
    the synchronous mock adapter, and never a fixed sleep-then-check-once.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _broker(self, grace_period_seconds: float = 2.0) -> Broker:
        return Broker(self.home, opencode_adapter=fake_opencode_adapter(grace_period_seconds))

    def test_real_child_process_completion_observed_without_ever_calling_collect(self):
        broker = self._broker()
        try:
            record = broker.create(TaskRequest("quick task", str(self.workspace), execution_mode="read_only", profile="opencode"))
            broker.start(record.id)
            observed = wait_until(
                lambda: any(event["task_id"] == record.id for event in broker.completion_events_since(0)["events"])
            )
            self.assertTrue(observed, "completion_events never observed the finished child process")
            self.assertIn(broker.store.get(record.id).state, TERMINAL_STATES)
        finally:
            broker.close()

    def test_pump_never_blocks_on_a_still_running_task(self):
        broker = self._broker()
        record = broker.create(TaskRequest("SLEEP", str(self.workspace), execution_mode="read_only", profile="opencode"))
        broker.start(record.id)
        handle = broker._process_handles[record.id]
        pgid = handle.pgid
        try:
            started_at = time.monotonic()
            page = broker.completion_events_since(0)
            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 2.0, "completion_events_since blocked behind a still-running task")
            self.assertEqual(page["events"], [])
            self.assertEqual(broker.store.get(record.id).state, TaskState.RUNNING)
        finally:
            kill_pgid(pgid)
            # The durable supervisor keeps writing (finalizing artifacts, then the
            # terminal manifest) for a moment after the payload's pgid is killed;
            # wait for that write to land so tearDown's TemporaryDirectory.cleanup()
            # can't race a still-running writer inside durable_launches/.
            try:
                supervisor_reached_terminal = wait_for_durable_launch_terminal(handle, timeout=5)
            finally:
                broker.close()
            self.assertTrue(supervisor_reached_terminal, "durable supervisor did not reach a terminal state before teardown")

    def test_pump_is_idempotent_across_repeated_polls(self):
        broker = self._broker()
        try:
            record = broker.create(TaskRequest("quick", str(self.workspace), execution_mode="read_only", profile="opencode"))
            broker.start(record.id)

            def finished() -> bool:
                broker.completion_events_since(0)  # each check is itself a pumping poll, never a blind sleep
                return broker.store.get(record.id).state in TERMINAL_STATES

            self.assertTrue(wait_until(finished), "task never reached a terminal state via polling")
            event_count_after_first_terminal = len(broker.store.events(record.id))

            first = broker.completion_events_since(0)
            second = broker.completion_events_since(0)
            self.assertEqual(first, second)
            self.assertEqual(
                len(broker.store.events(record.id)),
                event_count_after_first_terminal,
                "repeated polling must not append duplicate events for an already-terminal task",
            )
        finally:
            broker.close()

    def test_dispatch_record_cursor_poll_collect_multiple_real_tasks_no_sleep_guessing(self):
        """The exact round-trip real callers use: dispatch several tasks, record
        the cursor, poll completion_events until every task id has appeared,
        collect each -- no fixed sleep between dispatch and collection.
        """
        broker = self._broker()
        try:
            records = [
                broker.create(TaskRequest(f"round-1-task-{i}", str(self.workspace), execution_mode="read_only", profile="opencode"))
                for i in range(2)  # default opencode policy max_concurrency is 2
            ]
            cursor = broker.store.event_high_water_mark()
            for record in records:
                broker.start(record.id)
            expected_ids = {record.id for record in records}
            seen_ids: set[str] = set()

            def poll() -> bool:
                nonlocal cursor
                page = broker.completion_events_since(cursor)
                seen_ids.update(event["task_id"] for event in page["events"] if event["task_id"] in expected_ids)
                cursor = page["next_cursor"]
                return seen_ids == expected_ids

            self.assertTrue(wait_until(poll, timeout=5.0), "not all dispatched tasks were observed via completion_events")
            for record in records:
                collected = broker.collect(record.id)
                self.assertIn(collected.state, TERMINAL_STATES)
        finally:
            broker.close()

    def test_restart_never_fabricates_a_completion_for_a_lost_in_memory_handle(self):
        broker1 = self._broker()
        record = broker1.create(TaskRequest("SLEEP", str(self.workspace), execution_mode="read_only", profile="opencode"))
        broker1.start(record.id)
        pgid = broker1._process_handles[record.id].pgid
        try:
            broker1.close()  # models the broker process disappearing; the detached child keeps running

            broker2 = self._broker()
            try:
                page = broker2.completion_events_since(0)
                self.assertEqual(page["events"], [])
                self.assertEqual(broker2.store.get(record.id).state, TaskState.RUNNING)
            finally:
                broker2.close()
        finally:
            kill_pgid(pgid)


if __name__ == "__main__":
    unittest.main()
