"""Runtime/provider capability discovery and parent-directed selection (Phase 6D).

Exposes factual declared and observed capability inventory for subprocess CLI
adapters and named direct-API provider configurations. Selection is deterministic
filtering with eligibility/exclusion evidence — never opaque scoring or an
autonomous "best" pick.
"""

from __future__ import annotations

import subprocess
from typing import Any
from urllib.parse import urlparse

from .adapters import AdapterCapabilities
from .direct_api_runtime import DIRECT_API_PROFILE, OpenAiCompatibleDirectRuntime
from .models import DEFAULT_PROFILES, ProfilePolicy
from .providers import MissingCredentialReference, ProviderCapabilities, resolve_api_key

MOCK_PROFILE = "mock"
SUBPROCESS_LIMITATIONS = (
    "no_durable_session_reattachment_after_broker_restart",
    "no_live_mid_task_steering",
    "broker_observed_cancellation_not_runtime_self_report",
)
DIRECT_API_LIMITATIONS = (
    "read_only_execution_only",
    "no_subprocess_supervision",
    "no_isolated_worktree",
    "no_process_group_cancellation",
    "cooperative_http_abort_only",
    "no_live_mid_task_steering",
    "no_durable_session_reattachment_after_broker_restart",
    "no_agent_tool_loop",
)


def probe_cli_version(command_prefix: tuple[str, ...], timeout: float = 10.0) -> dict[str, Any]:
    """Side-effect-free CLI presence probe shared by discovery and adapters."""
    if not command_prefix:
        return {"available": False, "reason": "empty_command_prefix", "detail": "adapter has no command prefix"}
    try:
        completed = subprocess.run(
            [*command_prefix, "--version"], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return {"available": False, "reason": "cli_not_found", "detail": f"{command_prefix[0]!r} was not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "version_check_timed_out", "detail": f"--version did not return within {timeout}s"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:500]
        return {"available": False, "reason": "version_check_failed", "detail": detail}
    return {"available": True, "version": (completed.stdout or completed.stderr).strip()}


def _endpoint_summary(base_url: str) -> dict[str, str]:
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    host_class = "loopback" if host in {"127.0.0.1", "localhost", "::1"} else "remote"
    return {"scheme": parsed.scheme or "unknown", "host_class": host_class}


def _subprocess_declared_capabilities(policy: ProfilePolicy, adapter_caps: AdapterCapabilities) -> dict[str, bool]:
    modes = policy.allowed_modes
    return {
        "subprocess_supervision": adapter_caps.requires_subprocess,
        "process_group_cancellation": adapter_caps.supports_process_group_cancellation,
        "read_only_execution": "read_only" in modes,
        "isolated_worktree": "isolated_worktree" in modes,
        "workspace_mutation": "isolated_worktree" in modes,
        "live_steering": False,
        "session_reattachment": False,
        "broker_verified_tests": adapter_caps.reports_broker_verified_tests,
    }


def _mock_declared_capabilities(policy: ProfilePolicy) -> dict[str, bool]:
    modes = policy.allowed_modes
    return {
        "subprocess_supervision": False,
        "process_group_cancellation": False,
        "read_only_execution": "read_only" in modes,
        "isolated_worktree": "isolated_worktree" in modes,
        "workspace_mutation": "isolated_worktree" in modes,
        "live_steering": False,
        "session_reattachment": False,
        "broker_verified_tests": False,
        "synthetic_runtime": True,
    }


def _provider_declared_capabilities(caps: ProviderCapabilities) -> dict[str, bool]:
    return {
        "chat_completions": caps.chat_completions,
        "structured_output": caps.structured_output,
        "streaming": caps.streaming,
        "tool_calls": caps.tool_calls,
        "workspace_access": caps.workspace_access,
        "process_cancellation": caps.process_cancellation,
    }


def _observed_adapter_availability(adapter: object) -> dict[str, Any]:
    probe = getattr(adapter, "check_availability", None)
    if callable(probe):
        return probe()
    command_prefix = getattr(adapter, "command_prefix", None)
    if isinstance(command_prefix, tuple):
        return probe_cli_version(command_prefix)
    return {"available": None, "reason": "not_probed", "detail": "no side-effect-free probe implemented"}


def _observed_provider_availability(runtime: OpenAiCompatibleDirectRuntime | None, name: str, environ: dict[str, str]) -> dict[str, Any]:
    if runtime is None:
        return {"available": False, "reason": "providers_not_configured", "detail": "broker has no provider configuration loaded"}
    try:
        config = runtime.get_provider(name)
    except Exception as error:
        return {"available": False, "reason": "unknown_provider", "detail": str(error)}
    try:
        resolve_api_key(config, environ)
    except MissingCredentialReference as error:
        return {"available": False, "reason": "missing_credential_reference", "detail": str(error)}
    return {"available": True}


def discover_runtimes(
    *,
    profiles: dict[str, ProfilePolicy],
    subprocess_adapters: dict[str, object],
    mock_adapter: object,
    direct_api_runtime: OpenAiCompatibleDirectRuntime | None,
) -> list[dict[str, Any]]:
    """Machine-readable inventory of registered runtime profiles."""
    entries: list[dict[str, Any]] = []
    for profile_name in sorted(profiles):
        policy = profiles[profile_name]
        if profile_name == MOCK_PROFILE:
            entries.append({
                "name": profile_name,
                "kind": "synthetic",
                "execution_modes": sorted(policy.allowed_modes),
                "declared_capabilities": _mock_declared_capabilities(policy),
                "observed_availability": {"available": True, "reason": "synthetic_fixture"},
                "policy": {
                    "max_timeout_seconds": policy.max_timeout_seconds,
                    "max_concurrency": policy.max_concurrency,
                },
                "limitations": list(SUBPROCESS_LIMITATIONS),
            })
            continue
        if profile_name == DIRECT_API_PROFILE:
            caps = direct_api_runtime.capabilities if direct_api_runtime else AdapterCapabilities(False, False, False)
            entries.append({
                "name": profile_name,
                "kind": "direct_api",
                "execution_modes": sorted(policy.allowed_modes),
                "declared_capabilities": _subprocess_declared_capabilities(policy, caps),
                "observed_availability": {
                    "available": direct_api_runtime is not None,
                    "reason": "providers_configured" if direct_api_runtime else "providers_not_configured",
                },
                "policy": {
                    "max_timeout_seconds": policy.max_timeout_seconds,
                    "max_concurrency": policy.max_concurrency,
                },
                "limitations": list(DIRECT_API_LIMITATIONS),
                "requires_named_provider": True,
            })
            continue
        adapter = subprocess_adapters.get(profile_name)
        if adapter is None:
            continue
        entries.append({
            "name": profile_name,
            "kind": "subprocess_cli",
            "adapter_name": getattr(adapter, "name", profile_name),
            "runtime_label": getattr(adapter, "runtime_label", profile_name),
            "execution_modes": sorted(policy.allowed_modes),
            "declared_capabilities": _subprocess_declared_capabilities(policy, adapter.capabilities),
            "observed_availability": _observed_adapter_availability(adapter),
            "policy": {
                "max_timeout_seconds": policy.max_timeout_seconds,
                "max_concurrency": policy.max_concurrency,
            },
            "limitations": list(SUBPROCESS_LIMITATIONS),
        })
    return entries


def discover_providers(
    *,
    direct_api_runtime: OpenAiCompatibleDirectRuntime | None,
    environ: dict[str, str],
) -> list[dict[str, Any]]:
    if direct_api_runtime is None:
        return []
    entries: list[dict[str, Any]] = []
    for name in sorted(direct_api_runtime.providers):
        config = direct_api_runtime.providers[name]
        entries.append({
            "name": name,
            "kind": config.kind,
            "runtime_profile": DIRECT_API_PROFILE,
            "credential_reference": config.api_key_env,
            "default_model": config.default_model,
            "endpoint_summary": _endpoint_summary(config.base_url),
            "declared_capabilities": _provider_declared_capabilities(config.capabilities),
            "observed_availability": _observed_provider_availability(direct_api_runtime, name, environ),
            "request_timeout_seconds": config.request_timeout_seconds,
            "limitations": list(DIRECT_API_LIMITATIONS),
        })
    return entries


def _capability_match(declared: dict[str, bool], required: dict[str, bool]) -> list[str]:
    reasons: list[str] = []
    for key, needed in sorted(required.items()):
        if not isinstance(needed, bool):
            reasons.append(f"required_capabilities.{key} must be a boolean")
            continue
        if not needed:
            continue
        if key not in declared:
            reasons.append(f"declared_capabilities missing key {key!r}")
        elif not declared[key]:
            reasons.append(f"declared_capabilities.{key} is false")
    return reasons


def select_candidates(
    *,
    profiles: dict[str, ProfilePolicy],
    subprocess_adapters: dict[str, object],
    direct_api_runtime: OpenAiCompatibleDirectRuntime | None,
    environ: dict[str, str],
    execution_mode: str,
    required_runtime_capabilities: dict[str, bool] | None = None,
    required_provider_capabilities: dict[str, bool] | None = None,
    allowed_runtimes: list[str] | None = None,
    allowed_providers: list[str] | None = None,
    require_available: bool = True,
) -> dict[str, Any]:
    """Deterministic capability filtering with auditable exclusion evidence."""
    if not execution_mode:
        raise ValueError("execution_mode must be a non-empty string")
    runtime_inventory = {entry["name"]: entry for entry in discover_runtimes(
        profiles=profiles,
        subprocess_adapters=subprocess_adapters,
        mock_adapter=None,
        direct_api_runtime=direct_api_runtime,
    )}
    provider_inventory = {entry["name"]: entry for entry in discover_providers(
        direct_api_runtime=direct_api_runtime,
        environ=environ,
    )}
    runtime_required = required_runtime_capabilities or {}
    provider_required = required_provider_capabilities or {}
    evaluate_providers = (
        allowed_providers is not None
        or bool(provider_required)
    )
    excluded: list[dict[str, Any]] = []
    eligible_runtimes: list[str] = []
    eligible_providers: list[str] = []

    runtime_candidates = sorted(allowed_runtimes) if allowed_runtimes is not None else sorted(runtime_inventory)
    for name in runtime_candidates:
        entry = runtime_inventory.get(name)
        if entry is None:
            excluded.append({"candidate": name, "kind": "runtime", "reasons": ["unknown_runtime"]})
            continue
        reasons: list[str] = []
        if execution_mode not in entry["execution_modes"]:
            reasons.append(f"execution_mode {execution_mode!r} not in {entry['execution_modes']}")
        reasons.extend(_capability_match(entry["declared_capabilities"], runtime_required))
        availability = entry["observed_availability"]
        if require_available and availability.get("available") is not True:
            reasons.append(f"unavailable: {availability.get('reason', 'unknown')}")
        if reasons:
            excluded.append({"candidate": name, "kind": "runtime", "reasons": reasons})
        else:
            eligible_runtimes.append(name)

    provider_candidates = sorted(allowed_providers) if allowed_providers is not None else sorted(provider_inventory)
    if evaluate_providers:
        for name in provider_candidates:
            entry = provider_inventory.get(name)
            if entry is None:
                excluded.append({"candidate": name, "kind": "provider", "reasons": ["unknown_provider"]})
                continue
            reasons: list[str] = []
            if execution_mode not in runtime_inventory[DIRECT_API_PROFILE]["execution_modes"]:
                reasons.append(f"execution_mode {execution_mode!r} not supported by {DIRECT_API_PROFILE!r} runtime")
            reasons.extend(_capability_match(entry["declared_capabilities"], provider_required))
            availability = entry["observed_availability"]
            if require_available and availability.get("available") is not True:
                reasons.append(f"unavailable: {availability.get('reason', 'unknown')}")
            if reasons:
                excluded.append({"candidate": name, "kind": "provider", "reasons": reasons})
            else:
                eligible_providers.append(name)

    needs_runtimes = allowed_runtimes is not None or bool(runtime_required)
    needs_providers = evaluate_providers and (allowed_providers is not None or bool(provider_required))
    if needs_runtimes and not eligible_runtimes:
        raise ValueError("No runtime candidates meet the declared requirements")
    if needs_providers and not eligible_providers:
        raise ValueError("No provider candidates meet the declared requirements")
    if not needs_runtimes and not needs_providers and not eligible_runtimes and not eligible_providers:
        raise ValueError("No candidates meet the declared requirements")

    return {
        "execution_mode": execution_mode,
        "eligible_runtimes": eligible_runtimes,
        "eligible_providers": eligible_providers,
        "excluded": excluded,
        "note": "Selection returns eligible candidates only; the broker does not choose a winner.",
    }
