"""Documentation link resolution and demo safety checks."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    return sorted(ROOT.rglob("*.md"))


def resolve_link(source: Path, target: str) -> Path | None:
    if target.startswith(("http://", "https://", "mailto:")):
        return None
    if target.startswith("#"):
        return source
    path_part, _, _ = target.partition("#")
    if not path_part:
        return source
    return (source.parent / path_part).resolve()


class DocsLinkTests(unittest.TestCase):
    def test_relative_markdown_links_resolve(self):
        missing: list[str] = []
        for md in markdown_files():
            text = md.read_text(encoding="utf-8")
            for match in LINK_RE.finditer(text):
                target = match.group(1)
                resolved = resolve_link(md, target)
                if resolved is None:
                    continue
                if not resolved.exists():
                    missing.append(f"{md.relative_to(ROOT)} -> {target}")
        self.assertEqual(missing, [], "broken markdown links:\n" + "\n".join(missing))


class DemoScriptTests(unittest.TestCase):
    def test_codex_demo_defaults_to_dry_run(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_codex_demo.py")],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertIn("No provider call", result.stderr)

    def test_codex_demo_refuses_live_without_ack(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_codex_demo.py"), "--execute-live"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 2)

    def test_recorded_codex_evidence_present(self):
        evidence = ROOT / "docs" / "demos" / "codex-marker-evidence.json"
        self.assertTrue(evidence.is_file())
        payload = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertTrue(payload["provider_call_occurred"])
        self.assertEqual(payload["result"]["runtime_result"]["summary"], "alpha.txt")


if __name__ == "__main__":
    unittest.main()
