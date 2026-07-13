# Phase 5 — Contract, CI, and the road to a lifecycle/verification gate

## Phase 5A (this PR)

Scope: documentation and CI only, no runtime/product behavior change.

- [`docs/PRD.md`](PRD.md): canonical, provider-neutral product requirements.
- [`docs/RFC-001.md`](RFC-001.md): the current implementation RFC, consolidating
  Phase 1–4 evidence and known limitations.
- `.github/workflows/ci.yml`: automated enforcement of the test suite,
  compileall, and whitespace hygiene already used as manual quality evidence
  in Phase 1–4.

This is a plan for what comes next, not an assertion that 5B/5C are
implemented. Both remain open work.

## Phase 5B (planned) — Lifecycle recovery / idempotent collection

Problem: a broker restart mid-task loses the in-memory process handle for a
running OpenCode task (RFC-001 §8). Current mitigation is fail-safe cleanup
(never delete a possibly-live process's worktree/lease); there is no actual
re-attachment, and a second `collect()` call on an already-terminal task
raises `InvalidTransition` rather than idempotently returning the prior
result (phase-4.md known limitations).

Planned scope:

- Durable recovery path: on broker startup, reconcile any task left in a
  non-terminal state against its last known process-group metadata, rather
  than only discovering it at the next cleanup attempt.
- Idempotent `collect()`: a second collect call on an already-terminal task
  should return the stored result rather than raising.
- Explicit decision on whether true re-attachment (resuming supervision of a
  still-running adapter process) is in scope, or whether "detect and fail
  closed without data loss" is the target bar.

## Phase 5C (planned) — Verification gate + real Hermes field acceptance

Problem: verification is caller-invoked, not enforced (PRD §9, RFC-001 §9).
Nothing in the lifecycle graph currently requires a broker-verified check
before a workspace-writable task can report success.

Planned scope:

- A lifecycle option that requires a passing `Broker.verify()` pass before a
  task can reach `succeeded`, with a clearly labeled distinct terminal state
  or reason when verification is required but not supplied or fails.
- A real field acceptance run driven by Hermes (or an equivalent operator)
  exercising the full delegate → observe → collect → cancel flow against a
  real workspace and a real adapter, with raw results reported (not just
  unit-test evidence).

Both items are unimplemented as of Phase 5A; this document records intent
and sequencing, not completed work.
