"""Static result-schema support policy evaluated before adapter launch."""

from __future__ import annotations

from .adapters import AdapterCapabilities


UNSUPPORTED_RESULT_SCHEMA = "unsupported_result_schema"


def evaluate_result_schema_preflight(
    *,
    runtime: str,
    result_schema: str,
    capabilities: AdapterCapabilities,
) -> dict | None:
    """Return a deterministic rejection when an adapter cannot satisfy a schema.

    ``None`` means the adapter has no schema restriction.  An explicit set is
    an allowlist: it avoids treating plain-text output as a strict structured
    contract after a subprocess has already run.
    """
    supported = capabilities.supported_result_schemas
    if supported is None or result_schema in supported:
        return None
    return {
        "reason": UNSUPPORTED_RESULT_SCHEMA,
        "requested": {
            "runtime": runtime,
            "result_schema": result_schema,
        },
        "supported_result_schemas": sorted(supported),
    }
