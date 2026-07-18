"""Runtime adapter result-schema capability preflight (Cursor plain-summary only)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from recollect_lines.adapters import ResultSchemaPolicy
from recollect_lines.cursor_adapter import CursorAdapter
from recollect_lines.discovery import discover_runtimes
from recollect_lines.mcp_server import _dispatch_tool_call
from recollect_lines.models import TaskRequest
from recollect_lines.result_schema_selection import (
    UnsupportedResultSchemaError,
    supported_result_schemas,
    validate_requested_result_schema,
)
from recollect_lines.runtime_registry import DEFAULT_RUNTIME_REGISTRY
from recollect_lines.service import Broker
from recollect_lines.verified_investigation_report import VERIFIED_INVESTIGATION_REPORT_SCHEMA

FIXTURE_CURSOR = Path(__file__).parent / "fixtures" / "fake_cursor.py"


def fake_cursor_adapter(**kwargs):
    return CursorAdapter(command_prefix=(sys.executable, str(FIXTURE_CURSOR)), grace_period_seconds=2.0, **kwargs)


class ResultSchemaSelectionTests(unittest.TestCase):
    def test_cursor_descriptor_advertises_plain_summary_only(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("cursor")
        self.assertEqual(
            descriptor.adapter_capabilities.result_schema_policy,
            ResultSchemaPolicy.PLAIN_SUMMARY_ONLY,
        )
        self.assertEqual(supported_result_schemas(descriptor), frozenset({"plain-summary"}))

    def test_codex_descriptor_allows_all_supported_schemas(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("codex")
        self.assertEqual(
            descriptor.adapter_capabilities.result_schema_policy,
            ResultSchemaPolicy.ALL_SUPPORTED,
        )
        self.assertIn("evidence-report", supported_result_schemas(descriptor))
        self.assertIn(VERIFIED_INVESTIGATION_REPORT_SCHEMA, supported_result_schemas(descriptor))

    def test_cursor_rejects_verified_investigation_report(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("cursor")
        with self.assertRaises(UnsupportedResultSchemaError) as ctx:
            validate_requested_result_schema(descriptor, VERIFIED_INVESTIGATION_REPORT_SCHEMA)
        error = ctx.exception
        self.assertEqual(error.CODE, "unsupported_result_schema")
        self.assertEqual(error.runtime, "cursor")
        self.assertEqual(error.requested_schema, VERIFIED_INVESTIGATION_REPORT_SCHEMA)
        self.assertEqual(error.policy, ResultSchemaPolicy.PLAIN_SUMMARY_ONLY)
        self.assertEqual(error.supported_schemas, frozenset({"plain-summary"}))
        payload = error.to_dict()
        self.assertEqual(payload["code"], "unsupported_result_schema")
        self.assertEqual(payload["requested_schema"], VERIFIED_INVESTIGATION_REPORT_SCHEMA)
        self.assertEqual(payload["supported_schemas"], ["plain-summary"])

    def test_cursor_accepts_plain_summary(self):
        validate_requested_result_schema(DEFAULT_RUNTIME_REGISTRY.get("cursor"), "plain-summary")

    def test_discovery_exposes_cursor_result_schema_policy(self):
        adapter = fake_cursor_adapter()
        inventory = {
            entry["name"]: entry
            for entry in discover_runtimes(
                subprocess_adapters={"cursor": adapter},
                direct_api_runtime=None,
            )
        }
        declared = inventory["cursor"]["declared_capabilities"]
        self.assertEqual(declared["result_schema_policy"], "plain_summary_only")
        self.assertEqual(declared["supported_result_schemas"], ["plain-summary"])


class CursorResultSchemaBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_cursor_verified_investigation_report_rejected_at_create_without_launch(self):
        broker = Broker(self.home, cursor_adapter=fake_cursor_adapter())
        with self.assertRaises(UnsupportedResultSchemaError) as ctx:
            broker.create(TaskRequest(
                "investigate",
                str(self.workspace),
                runtime="cursor",
                result_schema=VERIFIED_INVESTIGATION_REPORT_SCHEMA,
                explicit_fields=frozenset({"result_schema"}),
            ))
        self.assertEqual(ctx.exception.requested_schema, VERIFIED_INVESTIGATION_REPORT_SCHEMA)
        self.assertEqual(list(broker.store.list()), [])
        broker.close()

    def test_cursor_plain_summary_accepted_and_launches(self):
        broker = Broker(self.home, cursor_adapter=fake_cursor_adapter())
        record = broker.create(TaskRequest("summarize", str(self.workspace), runtime="cursor"))
        started = broker.start(record.id)
        self.assertEqual(started.state.value, "running")
        self.assertIn(record.id, broker._process_handles)
        broker.cancel(record.id, "test cleanup")
        broker.collect(record.id)
        broker.close()

    def test_cursor_rejects_profile_default_structured_schema(self):
        broker = Broker(self.home, cursor_adapter=fake_cursor_adapter())
        with self.assertRaises(UnsupportedResultSchemaError):
            broker.create(TaskRequest(
                "investigate",
                str(self.workspace),
                runtime="cursor",
                agent_profile="repository-investigator",
            ))
        broker.close()

    def test_codex_verified_investigation_report_still_accepted_at_create(self):
        broker = Broker(self.home)
        record = broker.create(TaskRequest(
            "investigate",
            str(self.workspace),
            runtime="mock",
            result_schema=VERIFIED_INVESTIGATION_REPORT_SCHEMA,
            explicit_fields=frozenset({"result_schema"}),
        ))
        self.assertEqual(record.result_schema, VERIFIED_INVESTIGATION_REPORT_SCHEMA)
        broker.close()


class CursorResultSchemaMcpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home, cursor_adapter=fake_cursor_adapter())

    def tearDown(self):
        self.broker.close()
        self.tempdir.cleanup()

    def test_delegate_rejects_cursor_strict_schema_with_structured_error(self):
        response = _dispatch_tool_call(self.broker, "delegate", {
            "task": "investigate",
            "workspace": str(self.workspace),
            "runtime": "cursor",
            "result_schema": VERIFIED_INVESTIGATION_REPORT_SCHEMA,
        })
        self.assertTrue(response["isError"])
        payload = json.loads(response["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "unsupported_result_schema")
        self.assertEqual(payload["error"]["requested_schema"], VERIFIED_INVESTIGATION_REPORT_SCHEMA)
        self.assertEqual(payload["error"]["runtime"], "cursor")
        self.assertEqual(payload["error"]["supported_schemas"], ["plain-summary"])


if __name__ == "__main__":
    unittest.main()
