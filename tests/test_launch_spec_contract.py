"""LaunchSpec's stdout_artifact_name contract (RFC-004 durable-codex blocker fix).

LaunchSpec is the provider-neutral handoff between an adapter and the
generic durable supervisor. `stdout_artifact_name` lets an adapter preserve
an established public artifact name (Codex's `events.jsonl`, RFC-001)
without the durable layer growing provider-specific branching -- see
adaptor/contracts.py and durable_cli_launch.py.
"""

import unittest

from recollect_lines.adaptor.contracts import LaunchSpec


class LaunchSpecStdoutArtifactNameTests(unittest.TestCase):
    def test_default_stdout_artifact_name_is_the_generic_stdout_log(self):
        spec = LaunchSpec(argv=("echo", "hi"), cwd="/tmp")
        self.assertEqual(spec.stdout_artifact_name, "stdout.log")

    def test_stdout_artifact_name_can_be_overridden(self):
        spec = LaunchSpec(argv=("codex",), cwd="/tmp", stdout_artifact_name="events.jsonl")
        self.assertEqual(spec.stdout_artifact_name, "events.jsonl")

    def test_rejects_path_traversal_in_stdout_artifact_name(self):
        with self.assertRaises(ValueError):
            LaunchSpec(argv=("codex",), cwd="/tmp", stdout_artifact_name="../evil.log")

    def test_rejects_a_path_separator_in_stdout_artifact_name(self):
        with self.assertRaises(ValueError):
            LaunchSpec(argv=("codex",), cwd="/tmp", stdout_artifact_name="sub/events.jsonl")


if __name__ == "__main__":
    unittest.main()
