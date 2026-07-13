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

## Phase 5B (this PR) — Lifecycle recovery / idempotent collection

Problem: a broker restart mid-task loses the in-memory process handle for a
running OpenCode task (RFC-001 §8). The Phase 2–5A mitigation was fail-safe
cleanup only (never delete a possibly-live process's worktree/lease, but
otherwise fail immediately on any lost handle); there was no durable launch
identity to reconcile against, and a second `collect()` call on an
already-terminal task raised `InvalidTransition` rather than idempotently
returning the prior result (phase-4.md known limitations).

Implemented in this PR — see [phase-5b.md](phase-5b.md) for the full design,
state table, and operator procedure:

- Durable launch identity (`store.runtime_launches`), persisted the moment an
  adapter subprocess actually exists.
- Explicit reconciliation (`Broker.reconcile()` / `reconcile_pending()`, CLI
  `reconcile`/`reconcile-all`, MCP `reconcile`): a fresh `Broker` instance can
  inspect and act on a durable launch record with no in-memory
  `ProcessHandle`, reaching a truthful `failed` when the process group is
  confirmed dead, or an explicit non-terminal `recovery_required` state when
  it's still alive or liveness can't be confirmed — never a fabricated
  success.
- Idempotent `collect()`: a second call on an already-terminal task returns
  the stored result with no re-transition, no duplicate cleanup, and (at the
  MCP layer) no re-run verification.
- Safer `cancel()`: an OpenCode task with a lost handle is no longer treated
  like a mock task; it's reconciled against its durable launch record first,
  and a confirmed-alive process group can be cancelled directly via its
  persisted pgid.

Explicitly not in scope (see phase-5b.md "What this is not"): transparent
re-attachment to a still-running OpenCode process's output stream. That
remains impossible for the same reason Phase 2/3 named it out of scope — a
new OS process cannot regain a `Popen`/child relationship with an orphaned
subprocess.

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
