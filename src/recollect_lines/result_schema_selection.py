"""Capability-gated result-schema selection (runtime adapter preflight)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .adapters import ResultSchemaPolicy
from .result_normalization import DEFAULT_RESULT_SCHEMA, SUPPORTED_RESULT_SCHEMAS
from .runtime_registry import RuntimeDescriptor


class UnsupportedResultSchemaError(ValueError):
    """Requested result_schema is globally known but refused by the runtime adapter."""

    CODE = "unsupported_result_schema"

    def __init__(
        self,
        runtime: str,
        requested_schema: str,
        *,
        policy: ResultSchemaPolicy,
        supported_schemas: frozenset[str],
    ):
        self.runtime = runtime
        self.requested_schema = requested_schema
        self.policy = policy
        self.supported_schemas = supported_schemas
        super().__init__(
            f"runtime {runtime!r} does not support result_schema {requested_schema!r} "
            f"(policy={policy.value}; supported schemas: {sorted(supported_schemas)})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.CODE,
            "message": str(self),
            "runtime": self.runtime,
            "requested_schema": self.requested_schema,
            "policy": self.policy.value,
            "supported_schemas": sorted(self.supported_schemas),
        }


def supported_result_schemas(descriptor: RuntimeDescriptor) -> frozenset[str]:
    policy = descriptor.adapter_capabilities.result_schema_policy
    if policy is ResultSchemaPolicy.PLAIN_SUMMARY_ONLY:
        return frozenset({DEFAULT_RESULT_SCHEMA})
    return SUPPORTED_RESULT_SCHEMAS


def validate_requested_result_schema(descriptor: RuntimeDescriptor, requested_schema: str) -> None:
    supported = supported_result_schemas(descriptor)
    if requested_schema in supported:
        return
    raise UnsupportedResultSchemaError(
        descriptor.name,
        requested_schema,
        policy=descriptor.adapter_capabilities.result_schema_policy,
        supported_schemas=supported,
    )
