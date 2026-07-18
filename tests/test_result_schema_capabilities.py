"""Result-schema capability preflight coverage."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.adaptor.cursor import CursorAdapter
from recollect_lines.models import TaskRequest, TaskState
from recollect_lines.service import Broker


FIXTURE = Path(__file__).parent / "fixtures" / "fake_cursor.py"


class SpyCursorAdapter(CursorAdapter):
    start_calls = 0

    def start(self, *args, **kwargs):
        type(self).start_calls += 1
        return super().start(*args, **kwargs)


class ResultSchemaCapabilityPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        SpyCursorAdapter.start_calls = 0
        self.broker = Broker(
            self.home,
            cursor_adapter=SpyCursorAdapter(command_prefix=(sys.executable, str(FIXTURE))),
        )

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_cursor_rejects_strict_schema_before_subprocess_launch(self):
        record = self.broker.create(TaskRequest(
            "Investigate the repository",
            str(self.workspace),
            profile="cursor",
            result_schema="verified-investigation-report",
        ))

        rejected = self.broker.start(record.id)

        self.assertEqual(rejected.state, TaskState.REJECTED)
        self.assertEqual(SpyCursorAdapter.start_calls, 0)
        event = self.broker.store.events(record.id)[-1]
        self.assertEqual(event["metadata"]["reason"], "unsupported_result_schema")
        self.assertEqual(
            event["metadata"]["requested"],
            {"runtime": "cursor", "result_schema": "verified-investigation-report"},
        )
        self.assertEqual(event["metadata"]["supported_result_schemas"], ["plain-summary"])

    def test_cursor_plain_summary_remains_accepted(self):
        record = self.broker.create(TaskRequest(
            "Summarize the repository",
            str(self.workspace),
            profile="cursor",
            result_schema="plain-summary",
        ))

        started = self.broker.start(record.id)

        self.assertEqual(started.state, TaskState.RUNNING)
        self.assertEqual(SpyCursorAdapter.start_calls, 1)
        self.broker.cancel(record.id, reason="test cleanup")

    def test_non_cursor_strict_schema_remains_accepted(self):
        record = self.broker.create(TaskRequest(
            "Investigate the repository",
            str(self.workspace),
            profile="mock",
            result_schema="verified-investigation-report",
        ))

        started = self.broker.start(record.id)

        self.assertNotEqual(started.state, TaskState.REJECTED)
        self.assertNotEqual(self.broker.store.events(record.id)[-1]["metadata"].get("reason"), "unsupported_result_schema")


if __name__ == "__main__":
    unittest.main()
