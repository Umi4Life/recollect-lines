"""Named OpenAI-compatible provider configuration (Phase 6C).

Provider entries describe model endpoints — base URL, credential references,
models, timeouts, declared capabilities, and TLS policy. They are not runtime
adapters and never grant subprocess supervision, worktree access, or process-
group cancellation by themselves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cursor_adapter import redact_secrets

PROVIDER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
OPENAI_COMPATIBLE_KIND = "openai-compatible"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Declarable provider capabilities — validation data only, not routing (Phase 6D).
ALLOWED_CAPABILITY_KEYS = frozenset({
    "chat_completions",
    "structured_output",
    "streaming",
    "tool_calls",
    "workspace_access",
    "process_cancellation",
})


@dataclass(frozen=True)
class ProviderCapabilities:
    chat_completions: bool = True
    structured_output: bool = False
    streaming: bool = False
    tool_calls: bool = False
    workspace_access: bool = False
    process_cancellation: bool = False

    @classmethod
    def from_mapping(cls, raw: Any) -> "ProviderCapabilities":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("capabilities must be an object")
        unknown = set(raw) - ALLOWED_CAPABILITY_KEYS
        if unknown:
            raise ValueError(f"Unknown capability keys: {', '.join(sorted(unknown))}")
        values: dict[str, bool] = {}
        for key in ALLOWED_CAPABILITY_KEYS:
            if key not in raw:
                continue
            value = raw[key]
            if not isinstance(value, bool):
                raise ValueError(f"capabilities.{key} must be a boolean")
            values[key] = value
        if values.get("chat_completions") is False:
            raise ValueError("capabilities.chat_completions must be true for openai-compatible providers")
        return cls(**values)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    base_url: str
    api_key_env: str
    default_model: str
    request_timeout_seconds: int
    tls_verify: bool
    allow_insecure_http: bool
    ca_bundle: str | None
    capabilities: ProviderCapabilities
    estimated_cost_usd_upper_bound: float | None = None

    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


class ProviderConfigError(ValueError):
    pass


class MissingCredentialReference(ProviderConfigError):
    pass


def _validate_provider_name(name: str) -> None:
    if not isinstance(name, str) or not PROVIDER_NAME_PATTERN.match(name):
        raise ProviderConfigError(
            f"Invalid provider name {name!r}: must match {PROVIDER_NAME_PATTERN.pattern}"
        )


def _validate_base_url(base_url: str, *, allow_insecure_http: bool) -> None:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ProviderConfigError("base_url must be a non-empty string")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ProviderConfigError(f"base_url must use http or https scheme, got {parsed.scheme!r}")
    if not parsed.netloc:
        raise ProviderConfigError("base_url must include a host")
    if parsed.scheme == "http":
        host = parsed.hostname or ""
        if host not in LOOPBACK_HOSTS:
            raise ProviderConfigError(
                "http base_url is only permitted for loopback hosts (127.0.0.1, localhost, ::1); "
                "use https for remote endpoints"
            )
        if not allow_insecure_http:
            raise ProviderConfigError(
                "http base_url requires allow_insecure_http: true (explicit opt-in for loopback only)"
            )


def _parse_provider_entry(name: str, raw: Any) -> ProviderConfig:
    _validate_provider_name(name)
    if not isinstance(raw, dict):
        raise ProviderConfigError(f"Provider {name!r} must be an object")
    kind = raw.get("kind")
    if kind != OPENAI_COMPATIBLE_KIND:
        raise ProviderConfigError(f"Provider {name!r}: kind must be {OPENAI_COMPATIBLE_KIND!r}")
    base_url = raw.get("base_url")
    api_key_env = raw.get("api_key_env")
    default_model = raw.get("default_model")
    if not isinstance(api_key_env, str) or not api_key_env.strip():
        raise ProviderConfigError(f"Provider {name!r}: api_key_env must be a non-empty string")
    if not re.match(r"^[A-Z][A-Z0-9_]{0,126}$", api_key_env):
        raise ProviderConfigError(
            f"Provider {name!r}: api_key_env must be an uppercase environment-variable name"
        )
    if not isinstance(default_model, str) or not default_model.strip():
        raise ProviderConfigError(f"Provider {name!r}: default_model must be a non-empty string")
    timeout = raw.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise ProviderConfigError(f"Provider {name!r}: request_timeout_seconds must be a positive integer")
    tls_verify = raw.get("tls_verify", True)
    if not isinstance(tls_verify, bool):
        raise ProviderConfigError(f"Provider {name!r}: tls_verify must be a boolean")
    allow_insecure_http = raw.get("allow_insecure_http", False)
    if not isinstance(allow_insecure_http, bool):
        raise ProviderConfigError(f"Provider {name!r}: allow_insecure_http must be a boolean")
    ca_bundle = raw.get("ca_bundle")
    if ca_bundle is not None and (not isinstance(ca_bundle, str) or not ca_bundle.strip()):
        raise ProviderConfigError(f"Provider {name!r}: ca_bundle must be a non-empty string when set")
    _validate_base_url(base_url, allow_insecure_http=allow_insecure_http)
    capabilities = ProviderCapabilities.from_mapping(raw.get("capabilities"))
    estimated_cost = raw.get("estimated_cost_usd_upper_bound")
    if estimated_cost is not None:
        if not isinstance(estimated_cost, (int, float)) or isinstance(estimated_cost, bool) or estimated_cost <= 0:
            raise ProviderConfigError(
                f"Provider {name!r}: estimated_cost_usd_upper_bound must be a positive number when set"
            )
        estimated_cost = float(estimated_cost)
    return ProviderConfig(
        name=name,
        kind=kind,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        default_model=default_model.strip(),
        request_timeout_seconds=timeout,
        tls_verify=tls_verify,
        allow_insecure_http=allow_insecure_http,
        ca_bundle=ca_bundle.strip() if isinstance(ca_bundle, str) else None,
        capabilities=capabilities,
        estimated_cost_usd_upper_bound=estimated_cost,
    )


def validate_providers_document(data: Any) -> dict[str, ProviderConfig]:
    if not isinstance(data, dict):
        raise ProviderConfigError("Provider configuration must be a top-level object")
    providers_raw = data.get("providers")
    if not isinstance(providers_raw, dict) or not providers_raw:
        raise ProviderConfigError("'providers' must be a non-empty object")
    providers: dict[str, ProviderConfig] = {}
    for name, entry in providers_raw.items():
        if name in providers:
            raise ProviderConfigError(f"Duplicate provider name: {name!r}")
        providers[name] = _parse_provider_entry(name, entry)
    return providers


def load_providers_config(path: Path) -> dict[str, ProviderConfig]:
    try:
        raw_text = path.read_text()
    except OSError as error:
        raise ProviderConfigError(f"Cannot read provider configuration {path}: {error}") from error
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ProviderConfigError(f"Provider configuration {path} is not valid JSON: {error}") from error
    return validate_providers_document(data)


def resolve_api_key(config: ProviderConfig, environ: dict[str, str] | None = None) -> str:
    """Resolve a provider's credential from an environment-variable reference.

    Fail closed when the reference is missing, empty, or malformed.
    """
    env = environ if environ is not None else __import__("os").environ
    value = env.get(config.api_key_env)
    if value is None:
        raise MissingCredentialReference(
            f"Credential reference {config.api_key_env!r} is not set in the environment"
        )
    if not isinstance(value, str) or not value.strip():
        raise MissingCredentialReference(
            f"Credential reference {config.api_key_env!r} is set but empty"
        )
    return value.strip()


def redact_provider_error(message: str, secret: str | None = None) -> str:
    redacted = redact_secrets(message)
    if secret:
        redacted = redacted.replace(secret, "***REDACTED***")
    return redacted
