"""Tests for the central runtime registry (Phase 8.2)."""

from __future__ import annotations

import unittest

from recollect_lines.adapters import AdapterCapabilities
from recollect_lines.discovery import discover_runtimes
from recollect_lines.models import ProfilePolicy, translate_delegate_fields
from recollect_lines.recovery_contract import SYNTHETIC_RECOVERY_CONTROL
from recollect_lines.runtime_registry import (
    DEFAULT_RUNTIME_REGISTRY,
    DuplicateRuntimeRegistrationError,
    ExecutionStrategy,
    ModelSelectionSupport,
    RuntimeDescriptor,
    RuntimeRegistry,
    SUBPROCESS_LIMITATIONS,
    UnknownRuntimeError,
)


FIXTURE_TEST_PROFILE = ProfilePolicy(
    "fixture_test_runtime",
    frozenset({"read_only"}),
    3600,
    2,
)


class RuntimeRegistryTests(unittest.TestCase):
    def test_default_registry_lists_standard_runtimes(self):
        names = DEFAULT_RUNTIME_REGISTRY.names()
        self.assertIn("mock", names)
        self.assertIn("opencode", names)
        self.assertIn("claude_code", names)
        self.assertIn("codex", names)
        self.assertIn("cursor", names)
        self.assertIn("openai_compatible", names)

    def test_duplicate_registration_is_explicit(self):
        registry = RuntimeRegistry()
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("mock")
        registry.register(descriptor)
        with self.assertRaises(DuplicateRuntimeRegistrationError):
            registry.register(descriptor)

    def test_unknown_runtime_lookup_is_explicit(self):
        with self.assertRaises(UnknownRuntimeError):
            DEFAULT_RUNTIME_REGISTRY.get("not-a-runtime")

    def test_fixture_runtime_registers_and_discovers_without_interface_constants(self):
        registry = DEFAULT_RUNTIME_REGISTRY.copy()
        registry.register(RuntimeDescriptor(
            name=FIXTURE_TEST_PROFILE.name,
            execution_strategy=ExecutionStrategy.FIXTURE,
            policy=FIXTURE_TEST_PROFILE,
            adapter_capabilities=AdapterCapabilities(
                requires_subprocess=True,
                supports_process_group_cancellation=True,
                reports_broker_verified_tests=False,
                recovery_control=SYNTHETIC_RECOVERY_CONTROL,
            ),
            limitations=SUBPROCESS_LIMITATIONS,
            model_selection=ModelSelectionSupport.PERSISTED_NOT_INVOKED,
            runtime_label="fixture_test_runtime",
        ))
        runtime, _, _, _, _ = translate_delegate_fields(
            runtime=FIXTURE_TEST_PROFILE.name,
            runtime_registry=registry,
        )
        self.assertEqual(runtime, FIXTURE_TEST_PROFILE.name)
        class _FakeAdapter:
            name = FIXTURE_TEST_PROFILE.name
            capabilities = AdapterCapabilities(
                requires_subprocess=True,
                supports_process_group_cancellation=True,
                reports_broker_verified_tests=False,
                recovery_control=SYNTHETIC_RECOVERY_CONTROL,
            )

        inventory = {
            entry["name"]: entry
            for entry in discover_runtimes(
                registry=registry,
                subprocess_adapters={FIXTURE_TEST_PROFILE.name: _FakeAdapter()},
                direct_api_runtime=None,
            )
        }
        self.assertIn(FIXTURE_TEST_PROFILE.name, inventory)
        self.assertEqual(inventory[FIXTURE_TEST_PROFILE.name]["execution_strategy"], "fixture")

    def test_direct_api_execution_strategy_is_distinct(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("openai_compatible")
        self.assertEqual(descriptor.execution_strategy, ExecutionStrategy.DIRECT_API)
        self.assertFalse(descriptor.adapter_capabilities.requires_subprocess)

    def test_subprocess_execution_strategy_is_distinct(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("codex")
        self.assertEqual(descriptor.execution_strategy, ExecutionStrategy.SUBPROCESS_CLI)
        self.assertTrue(descriptor.adapter_capabilities.requires_subprocess)
        self.assertEqual(descriptor.model_selection, ModelSelectionSupport.PER_TASK_REQUEST)

    def test_opencode_model_selection_is_persisted_not_invoked(self):
        descriptor = DEFAULT_RUNTIME_REGISTRY.get("opencode")
        self.assertEqual(descriptor.model_selection, ModelSelectionSupport.PERSISTED_NOT_INVOKED)


if __name__ == "__main__":
    unittest.main()
