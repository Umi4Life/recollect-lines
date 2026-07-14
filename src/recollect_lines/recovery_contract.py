"""Recovery/control capability contract and no-model-call compatibility evidence (Phase 7C.1).

Declares what runtime recovery and in-flight control the broker honestly supports
today. Help-text keyword hits alone never elevate provider-native session resume
or mid-task message steering.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import now

RECOVERY_CONTRACT_SCHEMA_VERSION = "1"
COMPATIBILITY_EVIDENCE_SCHEMA_VERSION = "1"
PROBE_TYPE_VERSION_HELP_ONLY = "version_help_only"

# Keywords recorded in help fingerprints only — never treated as capability proof.
_HELP_KEYWORDS = ("resume", "session", "continue")
_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._-]+)+")


class RecoveryLevel(StrEnum):
    NONE = "none"
    OBSERVE_AND_CANCEL = "observe_and_cancel"
    COLLECT_AFTER_RESTART = "collect_after_restart"
    SESSION_RESUME = "session_resume"


class ControlAction(StrEnum):
    STATUS = "status"
    CANCEL = "cancel"
    COLLECT = "collect"
    MESSAGE = "message"


ALL_CONTROL_ACTIONS = frozenset(ControlAction)
ALL_RECOVERY_LEVELS = frozenset(RecoveryLevel)


@dataclass(frozen=True)
class RecoveryControlContract:
    recovery_level: RecoveryLevel
    supported_control_actions: frozenset[ControlAction]

    def __post_init__(self) -> None:
        unknown = self.supported_control_actions - ALL_CONTROL_ACTIONS
        if unknown:
            raise ValueError(f"Unknown control actions: {', '.join(sorted(a.value for a in unknown))}")
        if self.recovery_level not in ALL_RECOVERY_LEVELS:
            raise ValueError(f"Unknown recovery level: {self.recovery_level!r}")

    @property
    def unsupported_control_actions(self) -> frozenset[ControlAction]:
        return ALL_CONTROL_ACTIONS - self.supported_control_actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovery_level": self.recovery_level.value,
            "supported_control_actions": sorted(action.value for action in self.supported_control_actions),
            "unsupported_control_actions": sorted(action.value for action in self.unsupported_control_actions),
        }


# Declared contracts for current runtime kinds — no global recoverable boolean.
SUBPROCESS_CLI_RECOVERY_CONTROL = RecoveryControlContract(
    recovery_level=RecoveryLevel.OBSERVE_AND_CANCEL,
    supported_control_actions=frozenset({
        ControlAction.STATUS,
        ControlAction.CANCEL,
        ControlAction.COLLECT,
    }),
)
SYNTHETIC_RECOVERY_CONTROL = RecoveryControlContract(
    recovery_level=RecoveryLevel.NONE,
    supported_control_actions=frozenset({
        ControlAction.STATUS,
        ControlAction.CANCEL,
        ControlAction.COLLECT,
    }),
)
DIRECT_API_RECOVERY_CONTROL = RecoveryControlContract(
    recovery_level=RecoveryLevel.NONE,
    supported_control_actions=frozenset({
        ControlAction.STATUS,
        ControlAction.CANCEL,
        ControlAction.COLLECT,
    }),
)

# Unproven conclusions when only offline help/version evidence exists.
UNPROVEN_PROVIDER_NATIVE_RESUME = "unproven"
UNPROVEN_IN_FLIGHT_MESSAGE = "unproven"


def parse_recovery_level(value: str) -> RecoveryLevel:
    try:
        return RecoveryLevel(value)
    except ValueError as error:
        raise ValueError(
            f"recovery_level must be one of: {', '.join(sorted(level.value for level in RecoveryLevel))}"
        ) from error


def parse_control_action(value: str) -> ControlAction:
    try:
        return ControlAction(value)
    except ValueError as error:
        raise ValueError(
            f"control action must be one of: {', '.join(sorted(action.value for action in ControlAction))}"
        ) from error


def recovery_control_from_mapping(raw: dict[str, Any]) -> RecoveryControlContract:
    if not isinstance(raw, dict):
        raise ValueError("recovery_control must be an object")
    level_raw = raw.get("recovery_level")
    actions_raw = raw.get("supported_control_actions")
    if not isinstance(level_raw, str):
        raise ValueError("recovery_control.recovery_level must be a string")
    if not isinstance(actions_raw, list) or not all(isinstance(item, str) for item in actions_raw):
        raise ValueError("recovery_control.supported_control_actions must be an array of strings")
    return RecoveryControlContract(
        recovery_level=parse_recovery_level(level_raw),
        supported_control_actions=frozenset(parse_control_action(item) for item in actions_raw),
    )


def _sanitize_fingerprint(text: str, *, max_len: int = 200) -> str:
    redacted = _PATH_RE.sub("<path>", text.strip())
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-<redacted>", redacted)
    redacted = re.sub(r"\s+", " ", redacted)
    return redacted[:max_len]


def help_keyword_hits(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(keyword for keyword in _HELP_KEYWORDS if keyword in lowered)


def help_text_fingerprint(text: str) -> str:
    """Deterministic, redacted digest — not a verbatim help dump."""
    sanitized = _sanitize_fingerprint(text, max_len=4000)
    return hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:16]


def offline_probe_conclusions(*, help_keyword_hits_found: tuple[str, ...]) -> dict[str, str]:
    conclusions = {
        "provider_native_session_resume": UNPROVEN_PROVIDER_NATIVE_RESUME,
        "in_flight_message_control": UNPROVEN_IN_FLIGHT_MESSAGE,
    }
    if help_keyword_hits_found:
        conclusions["help_keyword_note"] = (
            "help mentions "
            + ", ".join(help_keyword_hits_found)
            + " but that is not adoption proof"
        )
    return conclusions


def offline_probe_remediation(*, executable_available: bool, help_keyword_hits_found: tuple[str, ...]) -> tuple[str, ...]:
    steps: list[str] = []
    if not executable_available:
        steps.append("Install the runtime CLI on PATH or override the adapter command flag.")
    if help_keyword_hits_found:
        steps.append(
            "Obtain active-process proof (launch ID, task ownership, PID/PGID anti-reuse identity, "
            "durable runner artifacts) before declaring session_resume or message control."
        )
    if not steps:
        steps.append("No-model-call version/help probe completed; remote availability was not observed.")
    return tuple(steps)


def probe_version_help_only(command_prefix: tuple[str, ...], *, timeout: float = 10.0) -> dict[str, Any]:
    """Side-effect-free local probe: --version and --help only. Never invokes a model."""
    if not command_prefix:
        return {
            "executable_available": False,
            "reason": "empty_command_prefix",
            "version_fingerprint": None,
            "help_keyword_hits": [],
            "help_fingerprint": None,
        }
    version_fingerprint: str | None = None
    help_fingerprint: str | None = None
    keyword_hits: tuple[str, ...] = ()
    try:
        version_completed = subprocess.run(
            [*command_prefix, "--version"], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return {
            "executable_available": False,
            "reason": "cli_not_found",
            "version_fingerprint": None,
            "help_keyword_hits": [],
            "help_fingerprint": None,
        }
    except subprocess.TimeoutExpired:
        return {
            "executable_available": False,
            "reason": "version_check_timed_out",
            "version_fingerprint": None,
            "help_keyword_hits": [],
            "help_fingerprint": None,
        }
    if version_completed.returncode != 0:
        return {
            "executable_available": False,
            "reason": "version_check_failed",
            "version_fingerprint": _sanitize_fingerprint(
                (version_completed.stderr or version_completed.stdout or "").strip(),
            ) or None,
            "help_keyword_hits": [],
            "help_fingerprint": None,
        }
    version_fingerprint = _sanitize_fingerprint(
        (version_completed.stdout or version_completed.stderr or "").strip(),
    )
    try:
        help_completed = subprocess.run(
            [*command_prefix, "--help"], capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        help_completed = None
    if help_completed is not None and help_completed.returncode == 0:
        help_text = (help_completed.stdout or help_completed.stderr or "")
        keyword_hits = help_keyword_hits(help_text)
        help_fingerprint = help_text_fingerprint(help_text)
    return {
        "executable_available": True,
        "reason": "version_help_only",
        "version_fingerprint": version_fingerprint,
        "help_keyword_hits": list(keyword_hits),
        "help_fingerprint": help_fingerprint,
    }


@dataclass(frozen=True)
class CompatibilityEvidence:
    schema_version: str
    adapter_id: str
    runtime_kind: str
    probe_type: str
    observed_at: str
    executable_available: bool
    version_fingerprint: str | None
    help_keyword_hits: tuple[str, ...]
    help_fingerprint: str | None
    conclusions: dict[str, str]
    declared_recovery_level: str
    declared_control_actions: tuple[str, ...]
    unsupported_control_actions: tuple[str, ...]
    remediation: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adapter_id": self.adapter_id,
            "runtime_kind": self.runtime_kind,
            "probe_type": self.probe_type,
            "observed_at": self.observed_at,
            "executable_available": self.executable_available,
            "version_fingerprint": self.version_fingerprint,
            "help_keyword_hits": list(self.help_keyword_hits),
            "help_fingerprint": self.help_fingerprint,
            "conclusions": dict(sorted(self.conclusions.items())),
            "declared_recovery_level": self.declared_recovery_level,
            "declared_control_actions": list(self.declared_control_actions),
            "unsupported_control_actions": list(self.unsupported_control_actions),
            "remediation": list(self.remediation),
        }


def build_compatibility_evidence(
    *,
    adapter_id: str,
    runtime_kind: str,
    contract: RecoveryControlContract,
    probe: dict[str, Any],
    observed_at: str | None = None,
) -> CompatibilityEvidence:
    keyword_hits = tuple(probe.get("help_keyword_hits") or ())
    executable_available = bool(probe.get("executable_available"))
    declared = contract.to_dict()
    return CompatibilityEvidence(
        schema_version=COMPATIBILITY_EVIDENCE_SCHEMA_VERSION,
        adapter_id=adapter_id,
        runtime_kind=runtime_kind,
        probe_type=PROBE_TYPE_VERSION_HELP_ONLY,
        observed_at=observed_at or now(),
        executable_available=executable_available,
        version_fingerprint=_sanitize_fingerprint(probe.get("version_fingerprint") or "") or None,
        help_keyword_hits=keyword_hits,
        help_fingerprint=probe.get("help_fingerprint"),
        conclusions=offline_probe_conclusions(help_keyword_hits_found=keyword_hits),
        declared_recovery_level=declared["recovery_level"],
        declared_control_actions=tuple(declared["supported_control_actions"]),
        unsupported_control_actions=tuple(declared["unsupported_control_actions"]),
        remediation=offline_probe_remediation(
            executable_available=executable_available,
            help_keyword_hits_found=keyword_hits,
        ),
    )


def recovery_control_discovery_payload(
    contract: RecoveryControlContract,
    *,
    observed_local: dict[str, Any] | None = None,
    compatibility_evidence: CompatibilityEvidence | None = None,
) -> dict[str, Any]:
    """Discovery/doctor/MCP-facing bundle: declared vs observed vs unproven."""
    payload: dict[str, Any] = {
        "schema_version": RECOVERY_CONTRACT_SCHEMA_VERSION,
        "declared": contract.to_dict(),
        "provider_native_session_resume": UNPROVEN_PROVIDER_NATIVE_RESUME,
        "in_flight_message_control": UNPROVEN_IN_FLIGHT_MESSAGE,
        "note": (
            "Declared recovery/control is broker truth. Observed local availability "
            "is host-specific. Help keywords are not adoption proof."
        ),
    }
    if observed_local is not None:
        payload["observed_local"] = {
            key: observed_local[key]
            for key in ("available", "reason", "version", "version_fingerprint", "help_keyword_hits")
            if key in observed_local
        }
    if compatibility_evidence is not None:
        payload["compatibility_evidence"] = compatibility_evidence.to_dict()
    return payload
