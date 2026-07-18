"""Regression tests for adaptor package layout and legacy import compatibility."""

from __future__ import annotations

import importlib
import unittest

import recollect_lines.adaptor as adaptor_pkg
from recollect_lines.adaptor import (
    AdapterCapabilities,
    ClaudeCodeAdapter,
    CodexAdapter,
    CursorAdapter,
    FixtureDurableAdapter,
    OpenCodeAdapter,
    RuntimeAdapter,
    cancel_process_group,
    group_alive,
    redact_command,
)
from recollect_lines.runtime_registry import DEFAULT_RUNTIME_REGISTRY


class AdaptorImportCompatibilityTests(unittest.TestCase):
    def test_legacy_and_new_contract_imports_are_identical(self):
        legacy = importlib.import_module("recollect_lines.adapters")
        self.assertIs(legacy.AdapterCapabilities, AdapterCapabilities)
        self.assertIs(legacy.RuntimeAdapter, RuntimeAdapter)

    def test_legacy_and_new_adapter_imports_are_identical(self):
        pairs = (
            ("recollect_lines.opencode_adapter", OpenCodeAdapter),
            ("recollect_lines.claude_code_adapter", ClaudeCodeAdapter),
            ("recollect_lines.codex_adapter", CodexAdapter),
            ("recollect_lines.cursor_adapter", CursorAdapter),
            ("recollect_lines.fixture_durable_adapter", FixtureDurableAdapter),
        )
        for module_name, expected_cls in pairs:
            with self.subTest(module=module_name):
                legacy = importlib.import_module(module_name)
                self.assertIs(getattr(legacy, expected_cls.__name__), expected_cls)

    def test_legacy_opencode_process_helpers_match_package(self):
        legacy = importlib.import_module("recollect_lines.opencode_adapter")
        self.assertIs(legacy.cancel_process_group, cancel_process_group)
        self.assertIs(legacy.group_alive, group_alive)
        self.assertIs(legacy.redact_command, redact_command)

    def test_registered_runtimes_resolve_to_expected_implementations(self):
        expected = {
            "opencode": OpenCodeAdapter,
            "claude_code": ClaudeCodeAdapter,
            "codex": CodexAdapter,
            "cursor": CursorAdapter,
        }
        for runtime_name, adapter_cls in expected.items():
            with self.subTest(runtime=runtime_name):
                descriptor = DEFAULT_RUNTIME_REGISTRY.get(runtime_name)
                self.assertIs(descriptor.adapter_capabilities, adapter_cls.capabilities)

    def test_no_duplicate_runtime_registrations(self):
        names = DEFAULT_RUNTIME_REGISTRY.names()
        self.assertEqual(len(names), len(set(names)))

    def test_adaptor_package_exports_stable_surface(self):
        expected = {
            "AdapterCapabilities",
            "RuntimeAdapter",
            "OpenCodeAdapter",
            "ClaudeCodeAdapter",
            "CodexAdapter",
            "CursorAdapter",
            "FixtureDurableAdapter",
            "cancel_process_group",
            "group_alive",
            "redact_command",
        }
        self.assertTrue(expected.issubset(set(adaptor_pkg.__all__)))

    def test_adaptor_submodules_import_without_circular_dependency(self):
        for module_name in (
            "recollect_lines.adaptor.contracts",
            "recollect_lines.adaptor.process",
            "recollect_lines.adaptor.cli_base",
            "recollect_lines.adaptor.opencode",
            "recollect_lines.adaptor.claude_code",
            "recollect_lines.adaptor.codex",
            "recollect_lines.adaptor.cursor",
            "recollect_lines.adaptor.fixture_durable",
        ):
            with self.subTest(module=module_name):
                importlib.import_module(module_name)


if __name__ == "__main__":
    unittest.main()
