import json
import tempfile
import unittest
from pathlib import Path

from sidecar.models import InvalidTransition, TaskRequest, TaskState
from sidecar.service import Broker


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.broker = Broker(self.home)

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def create(self):
        return self.broker.create(TaskRequest("Inspect tests", "/repo"))

    def test_create_persists_queued_task_and_request_artifact(self):
        record = self.create()
        self.assertEqual(record.state, TaskState.QUEUED)
        payload = json.loads((self.home / "artifacts" / record.id / "request.json").read_text())
        self.assertEqual(payload["task"], "Inspect tests")
        self.assertEqual([event["type"] for event in self.broker.store.events(record.id)], ["task.created", "task.queued"])

    def test_complete_follows_lifecycle_and_writes_result(self):
        record = self.create()
        self.broker.start(record.id)
        completed = self.broker.complete(record.id, "Found no failures")
        self.assertEqual(completed.state, TaskState.SUCCEEDED)
        result = json.loads((self.home / "artifacts" / record.id / "result.json").read_text())
        self.assertEqual(result["summary"], "Found no failures")
        self.assertEqual(result["state"], "succeeded")

    def test_cancel_running_task_reaches_terminal_state(self):
        record = self.create()
        self.broker.start(record.id)
        cancelled = self.broker.cancel(record.id, "No longer needed")
        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        self.assertEqual(self.broker.store.events(record.id)[-1]["type"], "task.cancelled")

    def test_invalid_transition_is_rejected(self):
        record = self.create()
        with self.assertRaises(InvalidTransition):
            self.broker.complete(record.id, "too early")

    def test_records_survive_service_reconstruction(self):
        record = self.create()
        self.broker.close()
        self.broker = Broker(self.home)
        restored = self.broker.status(record.id)
        self.assertEqual(restored["id"], record.id)
        self.assertEqual(restored["state"], "queued")


if __name__ == "__main__":
    unittest.main()
