"""Operator-configured model profiles with explicit resource metadata (RFC-003 foundation).

``model_profile`` binds a runtime/model configuration to durable cost and
resource classification. The broker never infers these from graph role,
provider name, runtime type, or model-name folklore.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .direct_api_runtime import DIRECT_API_PROFILE

PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
USAGE_BUCKET_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")

CostClass = Literal["low", "standard", "premium", "unknown"]
COST_CLASSES: frozenset[str] = frozenset({"low", "standard", "premium", "unknown"})

ResourceTier = Literal["negligible", "low", "moderate", "high", "unknown"]
RESOURCE_TIERS: frozenset[str] = frozenset({"negligible", "low", "moderate", "high", "unknown"})

QuotaScarcityTier = Literal["none", "low", "moderate", "high", "unknown"]
QUOTA_SCARCITY_TIERS: frozenset[str] = frozenset({"none", "low", "moderate", "high", "unknown"})

MODEL_PROFILES_CONFIG_KEY = "model_profiles"
ALLOWED_CONFIG_ENTRY_KEYS = frozenset({"runtime", "provider", "model", "cost_class", "usage_bucket", "resources"})
ALLOWED_RESOURCE_KEYS = frozenset({
    "monetary_cost",
    "quota_scarcity",
    "latency",
    "local_compute_occupancy",
    "context_cost",
})

RESOLUTION_CONFIGURED = "configured"
RESOLUTION_UNCONFIGURED = "unconfigured"


class ModelProfileValidationError(ValueError):
    """Invalid or incompatible model_profile selection."""


class ModelProfileConfigError(ValueError):
    """Invalid operator model_profiles configuration."""


@dataclass(frozen=True)
class ModelResourceDimensions:
    monetary_cost: ResourceTier
    quota_scarcity: QuotaScarcityTier
    latency: ResourceTier
    local_compute_occupancy: ResourceTier
    context_cost: ResourceTier


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    runtime: str
    provider: str | None
    model: str | None
    cost_class: CostClass
    usage_bucket: str
    resources: ModelResourceDimensions


@dataclass(frozen=True)
class ModelProfileRegistry:
    profiles: dict[str, ModelProfile]

    def known_profile_ids(self) -> frozenset[str]:
        return frozenset(self.profiles)


def profile_content_hash(profile: ModelProfile) -> str:
    payload = {
        "profile_id": profile.profile_id,
        "runtime": profile.runtime,
        "provider": profile.provider,
        "model": profile.model,
        "cost_class": profile.cost_class,
        "usage_bucket": profile.usage_bucket,
        "resources": {
            "monetary_cost": profile.resources.monetary_cost,
            "quota_scarcity": profile.resources.quota_scarcity,
            "latency": profile.resources.latency,
            "local_compute_occupancy": profile.resources.local_compute_occupancy,
            "context_cost": profile.resources.context_cost,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_cost_class(raw: Any, *, profile_id: str) -> CostClass:
    if raw not in COST_CLASSES:
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}.cost_class must be one of {sorted(COST_CLASSES)}"
        )
    return raw  # type: ignore[return-value]


def _parse_usage_bucket(raw: Any, *, profile_id: str) -> str:
    if not isinstance(raw, str) or not USAGE_BUCKET_PATTERN.match(raw):
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}.usage_bucket must match {USAGE_BUCKET_PATTERN.pattern}"
        )
    return raw


def _parse_resource_tier(raw: Any, *, profile_id: str, field: str) -> ResourceTier:
    if raw not in RESOURCE_TIERS:
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}.resources.{field} must be one of {sorted(RESOURCE_TIERS)}"
        )
    return raw  # type: ignore[return-value]


def _parse_quota_scarcity(raw: Any, *, profile_id: str) -> QuotaScarcityTier:
    if raw not in QUOTA_SCARCITY_TIERS:
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}.resources.quota_scarcity must be one of {sorted(QUOTA_SCARCITY_TIERS)}"
        )
    return raw  # type: ignore[return-value]


def _parse_resources(raw: Any, *, profile_id: str) -> ModelResourceDimensions:
    if not isinstance(raw, dict):
        raise ModelProfileConfigError(f"model_profiles.{profile_id}.resources must be an object")
    unknown = set(raw) - ALLOWED_RESOURCE_KEYS
    if unknown:
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}.resources: unknown key(s) {', '.join(sorted(unknown))}"
        )
    missing = ALLOWED_RESOURCE_KEYS - set(raw)
    if missing:
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}.resources missing required key(s) {', '.join(sorted(missing))}"
        )
    return ModelResourceDimensions(
        monetary_cost=_parse_resource_tier(raw["monetary_cost"], profile_id=profile_id, field="monetary_cost"),
        quota_scarcity=_parse_quota_scarcity(raw["quota_scarcity"], profile_id=profile_id),
        latency=_parse_resource_tier(raw["latency"], profile_id=profile_id, field="latency"),
        local_compute_occupancy=_parse_resource_tier(
            raw["local_compute_occupancy"], profile_id=profile_id, field="local_compute_occupancy",
        ),
        context_cost=_parse_resource_tier(raw["context_cost"], profile_id=profile_id, field="context_cost"),
    )


def _parse_configured_profile(profile_id: str, raw: Any) -> ModelProfile:
    if not PROFILE_ID_PATTERN.match(profile_id):
        raise ModelProfileConfigError(
            f"model_profiles key {profile_id!r} must match {PROFILE_ID_PATTERN.pattern}"
        )
    if not isinstance(raw, dict):
        raise ModelProfileConfigError(f"model_profiles.{profile_id} must be an object")
    unknown = set(raw) - ALLOWED_CONFIG_ENTRY_KEYS
    if unknown:
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}: unknown key(s) {', '.join(sorted(unknown))}"
        )
    runtime = raw.get("runtime")
    if not isinstance(runtime, str) or not runtime.strip():
        raise ModelProfileConfigError(f"model_profiles.{profile_id}.runtime must be a non-empty string")
    runtime = runtime.strip()
    provider = raw.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}.provider must be a non-empty string when set"
        )
    provider = provider.strip() if isinstance(provider, str) else None
    if runtime == DIRECT_API_PROFILE and provider is None:
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}: provider is required when runtime is {DIRECT_API_PROFILE!r}"
        )
    if runtime != DIRECT_API_PROFILE and provider is not None:
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}: provider must be omitted unless runtime is {DIRECT_API_PROFILE!r}"
        )
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ModelProfileConfigError(
            f"model_profiles.{profile_id}.model must be a non-empty string when set"
        )
    model = model.strip() if isinstance(model, str) else None
    cost_class = _parse_cost_class(raw.get("cost_class"), profile_id=profile_id)
    usage_bucket = _parse_usage_bucket(raw.get("usage_bucket"), profile_id=profile_id)
    resources = _parse_resources(raw.get("resources"), profile_id=profile_id)
    return ModelProfile(
        profile_id=profile_id,
        runtime=runtime,
        provider=provider,
        model=model,
        cost_class=cost_class,
        usage_bucket=usage_bucket,
        resources=resources,
    )


def parse_model_profiles_document(data: Any) -> dict[str, ModelProfile]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ModelProfileConfigError("model_profiles must be an object when provided")
    profiles: dict[str, ModelProfile] = {}
    for profile_id, entry in data.items():
        if profile_id in profiles:
            raise ModelProfileConfigError(f"Duplicate model_profiles entry: {profile_id!r}")
        profiles[profile_id] = _parse_configured_profile(profile_id, entry)
    return profiles


def load_model_profiles_config(path: Path) -> dict[str, ModelProfile]:
    try:
        raw_text = path.read_text()
    except OSError as error:
        raise ModelProfileConfigError(f"Cannot read operator configuration {path}: {error}") from error
    from .providers import _parse_yaml_document, _sniff_config_format

    fmt = _sniff_config_format(path, raw_text)
    if fmt == "json":
        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as error:
            raise ModelProfileConfigError(
                f"Operator configuration {path} is not valid JSON: {error}"
            ) from error
    else:
        document = _parse_yaml_document(path, raw_text)
        if document is None:
            return {}
    if not isinstance(document, dict):
        raise ModelProfileConfigError(f"Operator configuration {path} must be a top-level object")
    return parse_model_profiles_document(document.get(MODEL_PROFILES_CONFIG_KEY))


def build_model_profile_registry(*, configured: dict[str, ModelProfile]) -> ModelProfileRegistry:
    return ModelProfileRegistry(dict(configured))


def normalize_model_profile(
    raw: Any,
    *,
    registry: ModelProfileRegistry | None = None,
) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ModelProfileValidationError("model_profile must be a non-empty string when provided")
    profile_id = raw.strip()
    if registry is not None and profile_id not in registry.profiles:
        raise ModelProfileValidationError(
            f"Unknown model_profile {profile_id!r}; known ids: {sorted(registry.profiles)}"
        )
    return profile_id


def _profile_matches_launch(
    profile: ModelProfile,
    *,
    runtime: str,
    provider: str | None,
    effective_model: str | None,
) -> dict[str, Any] | None:
    if profile.runtime != runtime:
        return {
            "reason": "incompatible_model_profile",
            "model_profile": profile.profile_id,
            "detail": "profile runtime binding does not match task runtime",
            "expected_runtime": profile.runtime,
            "task_runtime": runtime,
        }
    if profile.provider is not None and profile.provider != provider:
        return {
            "reason": "incompatible_model_profile",
            "model_profile": profile.profile_id,
            "detail": "profile provider binding does not match task provider",
            "expected_provider": profile.provider,
            "task_provider": provider,
        }
    if profile.model is not None and profile.model != effective_model:
        return {
            "reason": "incompatible_model_profile",
            "model_profile": profile.profile_id,
            "detail": "profile model binding does not match resolved effective model",
            "expected_model": profile.model,
            "task_effective_model": effective_model,
        }
    return None


def evaluate_model_profile_preflight(
    *,
    runtime: str,
    provider: str | None,
    effective_model: str | None,
    requested_profile: str | None,
    registry: ModelProfileRegistry | None = None,
) -> dict[str, Any] | None:
    if requested_profile is None:
        return None
    effective_registry = registry or build_model_profile_registry(configured={})
    profile = effective_registry.profiles.get(requested_profile)
    if profile is None:
        return {
            "reason": "unknown_model_profile",
            "model_profile": requested_profile,
        }
    return _profile_matches_launch(
        profile,
        runtime=runtime,
        provider=provider,
        effective_model=effective_model,
    )


def resolve_model_profile_snapshot(
    *,
    runtime: str,
    provider: str | None,
    effective_model: str | None,
    requested_profile: str | None,
    registry: ModelProfileRegistry | None = None,
) -> dict[str, Any]:
    """Return a durable, privacy-safe resolution snapshot for persistence."""
    if requested_profile is None:
        return unconfigured_model_profile_snapshot()
    rejection = evaluate_model_profile_preflight(
        runtime=runtime,
        provider=provider,
        effective_model=effective_model,
        requested_profile=requested_profile,
        registry=registry,
    )
    if rejection is not None:
        raise ModelProfileValidationError(str(rejection))
    assert registry is not None
    profile = registry.profiles[requested_profile]
    return configured_model_profile_snapshot(profile)


def unconfigured_model_profile_snapshot() -> dict[str, Any]:
    return {
        "resolution": RESOLUTION_UNCONFIGURED,
        "model_profile": None,
        "cost_class": "unknown",
        "usage_bucket": None,
        "resources": {
            "monetary_cost": "unknown",
            "quota_scarcity": "unknown",
            "latency": "unknown",
            "local_compute_occupancy": "unknown",
            "context_cost": "unknown",
        },
    }


def configured_model_profile_snapshot(profile: ModelProfile) -> dict[str, Any]:
    return {
        "resolution": RESOLUTION_CONFIGURED,
        "model_profile": profile.profile_id,
        "content_hash": profile_content_hash(profile),
        "cost_class": profile.cost_class,
        "usage_bucket": profile.usage_bucket,
        "resources": {
            "monetary_cost": profile.resources.monetary_cost,
            "quota_scarcity": profile.resources.quota_scarcity,
            "latency": profile.resources.latency,
            "local_compute_occupancy": profile.resources.local_compute_occupancy,
            "context_cost": profile.resources.context_cost,
        },
    }


def model_profile_public_projection(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Safe broker-facing projection: identity and resource classification only."""
    if snapshot is None:
        return None
    projection: dict[str, Any] = {
        "resolution": snapshot.get("resolution", RESOLUTION_UNCONFIGURED),
        "cost_class": snapshot.get("cost_class", "unknown"),
    }
    if snapshot.get("model_profile") is not None:
        projection["model_profile"] = snapshot["model_profile"]
    usage_bucket = snapshot.get("usage_bucket")
    if isinstance(usage_bucket, str) and usage_bucket:
        projection["usage_bucket"] = usage_bucket
    resources = snapshot.get("resources")
    if isinstance(resources, dict) and resources:
        projection["resources"] = {
            key: resources[key]
            for key in sorted(ALLOWED_RESOURCE_KEYS)
            if key in resources
        }
    return projection
