"""Wave 4 / PR 11: deterministic pre-delegate schema/prose conflict warning."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recollect_lines.contract_conflict import detect_schema_prose_conflict
from recollect_lines.models import TaskRequest
from recollect_lines.service import Broker


class DetectSchemaProseConflictTests(unittest.TestCase):
    def test_no_conflict_for_plain_summary_regardless_of_prose(self):
        self.assertIsNone(detect_schema_prose_conflict("let's debate the merits of tabs vs spaces", None))
        self.assertIsNone(detect_schema_prose_conflict("let's debate the merits of tabs vs spaces", "plain-summary"))

    def test_conflict_for_debate_prose_with_review_findings(self):
        # The exact example from the PR 11 requirements.
        warning = detect_schema_prose_conflict("Debate the merits of microservices vs a monolith", "review-findings")
        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], "prose_genre_vs_structured_schema")
        self.assertEqual(warning["requested_schema"], "review-findings")
        self.assertEqual(warning["matched_signal"], "debate")

    def test_conflict_detected_for_each_structured_schema(self):
        for schema in ("evidence-report", "review-findings", "implementation-report"):
            with self.subTest(schema=schema):
                warning = detect_schema_prose_conflict("write a short story about a dragon", schema)
                self.assertIsNotNone(warning)
                self.assertEqual(warning["matched_signal"], "story")

    def test_ambiguous_valid_task_is_never_flagged(self):
        # A perfectly ordinary review task must not be flagged just because
        # review-findings was requested — only the closed prose-genre
        # vocabulary should ever trigger a warning.
        self.assertIsNone(detect_schema_prose_conflict("Review the auth module for race conditions", "review-findings"))
        self.assertIsNone(detect_schema_prose_conflict("Investigate why login fails intermittently", "evidence-report"))
        self.assertIsNone(detect_schema_prose_conflict("Fix the off-by-one bug in the paginator", "implementation-report"))

    def test_warning_never_contains_the_raw_task_text(self):
        task_text = "Debate SECRET_TOKEN=abc123 versus the alternative approach"
        warning = detect_schema_prose_conflict(task_text, "review-findings")
        self.assertIsNotNone(warning)
        serialized = json.dumps(warning)
        self.assertNotIn("SECRET_TOKEN", serialized)
        self.assertNotIn(task_text, serialized)


class BrokerSchemaConflictWarningTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.home = Path(self.tempdir.name) / "broker"
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir()
        self.broker = Broker(self.home)
        self.addCleanup(self.broker.close)

    def test_create_persists_and_surfaces_warning_without_blocking(self):
        record = self.broker.create(TaskRequest(
            "Debate whether microservices are worth the complexity",
            str(self.workspace),
            runtime="mock",
            result_schema="review-findings",
            explicit_fields=frozenset({"result_schema"}),
        ))
        # Advisory only: task creation must never be blocked or rejected.
        warning = self.broker.schema_conflict_warning(record.id)
        self.assertIsNotNone(warning)
        self.assertEqual(warning["matched_signal"], "debate")

        status = self.broker.status(record.id)
        self.assertEqual(status["schema_conflict_warning"], warning)

    def test_no_warning_artifact_written_for_compatible_task(self):
        record = self.broker.create(TaskRequest(
            "Review the auth module for race conditions",
            str(self.workspace),
            runtime="mock",
            result_schema="review-findings",
            explicit_fields=frozenset({"result_schema"}),
        ))
        self.assertIsNone(self.broker.schema_conflict_warning(record.id))
        status = self.broker.status(record.id)
        self.assertNotIn("schema_conflict_warning", status)
        artifact_path = self.broker.store.artifacts / record.id / "schema_conflict_warning.json"
        self.assertFalse(artifact_path.is_file())

    def test_no_warning_for_default_plain_summary_schema(self):
        record = self.broker.create(TaskRequest(
            "Let's debate this topic informally",
            str(self.workspace),
            runtime="mock",
        ))
        self.assertIsNone(self.broker.schema_conflict_warning(record.id))


if __name__ == "__main__":
    unittest.main()
