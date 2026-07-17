"""Runtime capability contract (Wave 4 / PR 12).

One stable, queryable descriptor of what a runtime executes, mutates, and
materializes. Every `RuntimeDescriptor` carries exactly one of these so
capability checks read from a single source of truth instead of scattered
execution_mode/limitations conditionals duplicated across service.py,
discovery.py, certify.py, and direct_api_runtime.py.

Materialization is never automatic for *any* runtime: the broker never writes
a task's result into the caller's real workspace (workspace.py never mutates
the source repo, even in isolated_worktree mode -- see WorkspaceManager).
Worktree-capable runtimes leave their changes in a broker-owned git
worktree/branch; text-synthesis runtimes (openai_compatible) leave only
prose. Either way, the parent that delegated the task owns turning that
output into an applied, validated change -- this module exists so that fact
is advertised honestly instead of implied only by prose in one adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class OutputKind(StrEnum):
    WORKSPACE_MUTATION = "workspace_mutation"
    TEXT_SYNTHESIS = "text_synthesis"


class MaterializationOwner(StrEnum):
    PARENT_MERGES_BROKER_WORKTREE = "parent_merges_broker_worktree"
    PARENT_APPLIES_TEXT = "parent_applies_text"


@dataclass(frozen=True)
class RuntimeCapabilityContract:
    output_kind: OutputKind
    mutates_workspace: bool
    owns_worktree: bool
    materialization_owner: MaterializationOwner
    parent_materialization_required: bool
    materialization_note: str

    def as_dict(self) -> dict[str, object]:
        return {
            "output_kind": self.output_kind.value,
            "mutates_workspace": self.mutates_workspace,
            "owns_worktree": self.owns_worktree,
            "materialization_owner": self.materialization_owner.value,
            "parent_materialization_required": self.parent_materialization_required,
            "materialization_note": self.materialization_note,
        }


WORKTREE_CAPABLE_CONTRACT = RuntimeCapabilityContract(
    output_kind=OutputKind.WORKSPACE_MUTATION,
    mutates_workspace=True,
    owns_worktree=True,
    materialization_owner=MaterializationOwner.PARENT_MERGES_BROKER_WORKTREE,
    parent_materialization_required=True,
    materialization_note=(
        "isolated_worktree changes land in a broker-owned git worktree/branch; "
        "the broker never merges them into the source workspace. The parent "
        "must review and merge the worktree branch before treating the change as applied."
    ),
)

SYNTHETIC_CONTRACT = RuntimeCapabilityContract(
    output_kind=OutputKind.WORKSPACE_MUTATION,
    mutates_workspace=False,
    owns_worktree=True,
    materialization_owner=MaterializationOwner.PARENT_MERGES_BROKER_WORKTREE,
    parent_materialization_required=True,
    materialization_note=(
        "mock is a synthetic no-op stub: it never writes files, even inside its "
        "own broker-owned worktree. Treat any reported change as illustrative only."
    ),
)

TEXT_SYNTHESIS_CONTRACT = RuntimeCapabilityContract(
    output_kind=OutputKind.TEXT_SYNTHESIS,
    mutates_workspace=False,
    owns_worktree=False,
    materialization_owner=MaterializationOwner.PARENT_APPLIES_TEXT,
    parent_materialization_required=True,
    materialization_note=(
        "openai_compatible returns synthesized text only over HTTP; it never owns a "
        "git worktree or writes to any workspace. The parent that delegated this task "
        "must materialize (apply) and validate the returned text itself."
    ),
)


class _RuntimeLookup(Protocol):
    def get(self, name: str) -> object: ...
    def names(self) -> tuple[str, ...]: ...


def describe_unsupported_execution_mode(registry: _RuntimeLookup, runtime: str, requested_mode: str) -> str:
    """Actionable, honest diagnostic for a runtime whose policy rejects requested_mode.

    Names supported alternative runtimes and states who owns materialization for
    *runtime* itself, so a caller can tell whether this is a policy dial to flip
    or an architectural limit (see docs/operator-guide.md's materialize-validate-record
    workflow).
    """
    descriptor = registry.get(runtime)
    contract = descriptor.capability_contract
    alternatives = sorted(
        name for name in registry.names()
        if name != runtime and requested_mode in registry.get(name).policy.allowed_modes
    )
    alt_text = ", ".join(alternatives) if alternatives else "none currently registered"
    return (
        f"Profile {runtime} does not permit mode {requested_mode} "
        f"(supported execution_modes for {runtime!r}: {sorted(descriptor.policy.allowed_modes)}). "
        f"{contract.materialization_note} "
        f"Runtimes that support execution_mode {requested_mode!r}: {alt_text}."
    )


def materialization_prompt_notice(contract: RuntimeCapabilityContract) -> str | None:
    """Prompt-facing honesty notice for text-synthesis runtimes.

    Worktree-capable runtimes run their own CLI tool loop against real files, so
    the model already knows what it can do. Text-synthesis runtimes never touch
    a workspace at all -- the composed prompt must say so explicitly, or the
    model may claim implementation work it cannot perform (the dogfood failure
    this contract exists to prevent).
    """
    if contract.output_kind is not OutputKind.TEXT_SYNTHESIS:
        return None
    return f"Runtime notice: {contract.materialization_note}"
