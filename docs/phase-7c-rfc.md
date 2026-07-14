# Phase 7C RFC — Recovery, control contract, and compatibility evidence

Status: Phase **7C.1** (this document) defines the typed recovery/control
contract and no-model-call compatibility evidence. It does **not** implement
durable subprocess reattachment, post-restart output collection, provider-native
session resume, or mid-task message injection.

Related: [RFC-001.md](RFC-001.md), [phase-5b.md](phase-5b.md),
[phase-6d.md](phase-6d.md), [PRD.md](PRD.md).

## 1. Problem statement

Operators and parent agents need honest answers to:

- What can the broker do after it restarts while a task was in flight?
- Can we resume a provider-native session or steer a running CLI?
- What is declared product capability vs what was observed on one host?

Phase 7C.1 answers these with a **small vocabulary**, **fail-closed validation**,
and **redacted compatibility evidence** — without changing task execution,
broker reconciliation, or cancellation semantics.

## 2. Terminology (do not conflate)

| Concept | Meaning today (7C.1) |
|---|---|
| **Process recovery** | Re-acquiring supervision of a surviving OS process after broker loss. **Not implemented.** |
| **Post-restart output collection** | Collecting stdout/stderr/result from a process that outlived the broker. **Not implemented** (Phase 7C.2 durable runner). |
| **Provider-native session resume** | CLI/provider re-opening its own persisted session (e.g. `resume` in help text). **Unproven** — help keywords are not adoption proof. |
| **Continuation task** | Starting a *new* broker task that references prior context. Out of 7C.1 scope; not claimed. |
| **Cancellation** | Broker-initiated stop via in-memory handle or persisted PGID reconciliation. **Supported** for subprocess CLIs within existing Phase 5B rules. |
| **Free-form mid-task steering** | `recollect.message` / stdin injection while running. **Explicitly unsupported** for all runtimes. |

## 3. Capability vocabulary

### 3.1 Recovery levels (`recovery_level`)

| Level | Semantics |
|---|---|
| `none` | No subprocess/durable launch recovery affordance (mock, direct API). |
| `observe_and_cancel` | Broker may observe task state and attempt cancellation via persisted launch metadata after restart; **cannot** reattach or collect in-flight output. Current subprocess CLIs. |
| `collect_after_restart` | Durable runner can collect output after broker restart. **Phase 7C.2+ — not declared today.** |
| `session_resume` | Provider-native session resume integrated with broker safety proof. **Not declared today.** |

There is **no** global `recoverable: true` flag. Differences between runtime kinds must remain visible.

### 3.2 Control actions

| Action | Subprocess CLI (7C.1) | Mock | Direct API |
|---|---|---|---|
| `status` | supported | supported | supported |
| `cancel` | supported | supported | supported (HTTP abort) |
| `collect` | supported while broker holds truth | supported | supported |
| `message` | **unsupported** | **unsupported** | **unsupported** |

After broker restart, `collect` on subprocess-backed tasks without reconciliation remains **fail-closed** (`RecoveryRequired`) — unchanged from Phase 5B.

## 4. State / control matrix (broker truth)

| Task state | `status` | `cancel` | `collect` | `message` | After broker restart |
|---|---|---|---|---|---|
| running (in-memory handle) | yes | yes | no (not terminal) | unsupported | N/A |
| running (no handle) | yes | via reconcile | fail-closed | unsupported | → `recovery_required` or reconcile |
| recovery_required | yes | via reconcile | fail-closed | unsupported | no fabricated success |
| terminal | yes | no-op | yes (if succeeded) | unsupported | unchanged |

## 5. Safety proof required before future adoption (7C.2–7C.4)

Before any runtime may declare `collect_after_restart` or `session_resume`, **all**
of the following must be present and consistent (fail-closed on missing/corrupt/conflict):

1. **Launch ID** tied to broker home and task ID.
2. **Task/workspace ownership** — lease and worktree policy still enforced.
3. **Adapter kind** — must match persisted launch record.
4. **PID/PGID plus anti-reuse identity** — detect PID reuse; never attach to arbitrary processes.
5. **Durable runner/artifact proof** — file-backed stdout/stderr offsets or equivalent.
6. **Broker recovery lease** — single writer / fencing token for reconciliation.
7. **Sanitized audit evidence** — no credentials, raw argv, or full help dumps in discovery.

Missing or conflicting proof → remain at `observe_and_cancel` or `none`; never optimistic upgrade.

## 6. Compatibility evidence (no-model-call)

Probe type: `version_help_only` — runs `--version` and `--help` only.

Recorded fields (redacted):

- adapter/runtime identity, schema version, timestamp
- executable availability (local observation)
- version fingerprint and help keyword hits (`resume`, `session`, `continue`)
- help digest fingerprint (SHA-256 prefix), **not** full help text
- conclusions: `provider_native_session_resume: unproven`, `in_flight_message_control: unproven`
- declared recovery/control values and remediation steps

**Worker observation (2026-07-14, no model calls):**

| Runtime | Local probe | Version fingerprint (sanitized) | Help keywords observed | Declared `session_resume` |
|---|---|---|---|---|
| Claude Code | available | 2.1.177 (Claude Code) | resume, session, continue | **unproven** |
| Codex CLI | available | 0.144.4 | resume, session | **unproven** |
| Cursor Agent | available | 2026.07.09-a3815c0 | resume, session, continue | **unproven** |
| OpenCode | **not observed on probe worker** | — | — | **unproven** (not globally unsupported) |
| Direct API | N/A (HTTP) | — | — | **none** |
| Mock/fixture | synthetic | — | — | **none** |

Help keywords alone **must not** elevate conclusions.

## 7. Phased roadmap

| Phase | Deliverable |
|---|---|
| **7C.1** (this PR) | Typed contract, discovery/doctor/MCP visibility, compatibility evidence model, RFC/matrix |
| **7C.2** | Durable subprocess runner, file-backed output |
| **7C.3** | Safe reconciliation with proof gate |
| **7C.4** | Operator surface (explicit commands/UI for recovery actions) |

## 8. Interfaces (unchanged execution)

- `recollect.message` — remains structured `unsupported`; no side effects.
- `Broker.reconcile()` / `reconcile_pending()` — behavior unchanged.
- Task dispatch, process launch, cancellation — unchanged.

Discovery additions:

- `recovery_control` on each runtime/provider inventory entry
- `recovery_contract_schema_version` on `discover_capabilities` payload

## 9. Known limitations (7C.1)

- Compatibility evidence is host-local; not a universal compatibility promise.
- No remote HTTP/provider reachability in evidence probes.
- OpenCode unavailability on one worker is an observation, not a product verdict.
- `collect_after_restart` and `session_resume` are reserved vocabulary only.
