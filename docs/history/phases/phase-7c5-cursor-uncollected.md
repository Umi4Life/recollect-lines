# Phase 7C.5 — Cursor-only `uncollected` restart outcome

Status: **Implemented.** Fixes a live-field-verified false `failed` (and,
before that, permanently-stuck `recovery_required`) for Cursor tasks whose
broker restarted while a task was in flight. Cursor only; every other
subprocess adapter (OpenCode, Claude Code, Codex), the direct API path, and
the durable subprocess runner (7C.2/7C.3) are unchanged.

Related: [RFC-001.md](../../design/RFC-001.md), [phase-5b.md](phase-5b.md),
[phase-5c.md](phase-5c.md), [phase-7c-rfc.md](phase-7c-rfc.md).

## 1. What went wrong

Field evidence (Wave 3, reproduced on merged master `8bbd901`):

```text
supervised Cursor CLI leader exits
+ a reparented same-PGID Cursor helper survives for ~231 seconds
→ replacement broker sees the process group as alive
→ recovery_required (indefinitely, while the helper lingers)
→ helper eventually exits
→ legacy reconciliation (`Broker.reconcile()`) sees the group dead
→ labels the task `failed`
```

The `failed` label is a fabrication: the broker never ran `CursorAdapter.collect()`
on this leader — no in-memory `ProcessHandle` survived the restart — so it never
observed a real exit code or parsed output. Phase 5B's `_process_group_status()`
(`os.killpg(pgid, 0)`) answers "is *some* process still in this group?", not "is
the *leader I launched* still running?" — a Cursor helper that outlives its
leader in the same process group (`start_new_session=True` sets the leader's
pgid to its own pid; a child it spawns without its own `setsid()` inherits that
pgid by default) makes the two questions diverge for exactly the window this
incident hit.

## 2. Fix: leader identity, not group liveness, is the death proof (Cursor only)

`CursorAdapter.start()` now captures the leader's anti-PID-reuse identity —
`durable_runner.read_process_start_identity()`, the same Linux
`boot_id`+`/proc/<pid>/stat starttime` mechanism the durable subprocess runner
(7C.2) already uses for adopted-launch proof — at spawn time, and persists it
in `runtime_launches.leader_start_identity` (new nullable column; every other
adapter leaves it `NULL`).

`Broker.reconcile()` gains one adapter-scoped branch,
`_reconcile_cursor_legacy_subprocess()`, entered only when
`adapter_name == "cursor"` and the task isn't mid-`CANCELLING` (that path is
untouched — see §5). It classifies the persisted `(pid, leader_start_identity)`
pair via `durable_runner.classify_process_identity()`:

| `classify_process_identity()` | Meaning | Reconcile outcome |
|---|---|---|
| `"alive"` | pid live, current identity matches | `recovery_required` (unchanged posture) |
| `"unknown"` | missing pid/identity, permission error, or unreadable current identity | `recovery_required` — **never inferred as death** |
| `"dead"` | pid gone, or pid reused by a different process (identity mismatch) | proceeds to §3 |

Process-group liveness (`_process_group_status()`, unchanged, still
`os.killpg(pgid, 0)`) is still computed and recorded, but **only as compact
diagnostic metadata** (`process_group.state`, `process_group.helpers_may_linger`)
— never as the signal that decides the outcome. Reconciliation never polls or
waits for a lingering helper to exit.

## 3. New terminal state: `uncollected`

When the leader is proven dead and the broker never collected a terminal
result from it (no in-memory handle survived), the task reaches a new
terminal `TaskState.UNCOLLECTED` (`models.py`) instead of a fabricated
`failed`:

- Added to `TERMINAL_STATES` — inherits every existing terminal-state
  behavior for free: `collect()`/`cancel()` refuse further action,
  `operator_control` reports `recovery_posture: terminal`, and it is
  automatically included in `COMPLETION_CURSOR_STATES`
  (`completion_events.py`), so it shows up in the completion-event cursor
  and `status()`/`reconcile` output with zero additional plumbing.
- `ALLOWED_TRANSITIONS` gains `UNCOLLECTED` as a target from `preparing`,
  `running`, `collecting`, and `recovery_required` — the same source states
  the old dead-group `failed` transition could fire from.
- Transition metadata carries `reason: "leader_exited_uncollected"` and
  `outcome: "unknown"`, plus the `leader`/`process_group` compact facts
  described above. `completion_events._compact_metadata()` passes `outcome`,
  `leader`, and `process_group` through to the completion-cursor payload —
  bounded pid/state facts only, never raw stdout/stderr.
- `Broker.collect()`'s post-reconcile check changed from
  `if reconciled.state is not TaskState.FAILED` to
  `if reconciled.state not in TERMINAL_STATES`, so a caller that calls
  `collect()` directly (skipping an explicit `reconcile()` call) gets the
  same terminal `uncollected` record back instead of an incorrect
  `RecoveryRequired`.

No result is ever fabricated from partial stdout: this path never opens
`stdout.log`/`stderr.log`. No automatic redispatch, retry, or group
termination follows an `uncollected` transition.

## 4. Safety invariants

- **Cursor-only.** The new branch is gated on `adapter_name == "cursor"`;
  every other adapter's `reconcile()` path is byte-identical to before this
  change (see `tests/test_cursor_uncollected_reconciliation.py::test_non_cursor_legacy_reconciliation_still_uses_group_liveness`).
- **No PID-reuse false positive.** `classify_process_identity()` requires a
  captured `leader_start_identity` and a current-identity match to call a pid
  "alive"; a live pid with a *different* start identity is proof the original
  leader is gone (pid reuse), not proof it's still running.
- **Missing/unverifiable identity never becomes death.** No captured identity,
  a `PermissionError` probing the pid, or a failed current-identity read all
  classify as `"unknown"` and fall back to `recovery_required` — identical to
  the pre-existing "runtime metadata missing or invalid" conservative posture.
- **Already-collected results are untouched.** This branch only fires from
  `reconcile()`; a task with a real in-memory `ProcessHandle` or an
  already-terminal record short-circuits before it, exactly as before.
- **Cancellation authority is unchanged.** A task mid-`CANCELLING` when a
  broker restarts still resolves via the original, unmodified pgid-based
  branch.

## 5. Known limitations

- `leader_start_identity` is Linux-specific anti-reuse proof (boot_id +
  `/proc/<pid>/stat` starttime); other platforms fall back to a best-effort
  identity that cannot detect PID reuse, so `classify_process_identity()`
  will usually resolve to `"unknown"` (safe: `recovery_required`, never a
  false `"dead"`) rather than `"dead"` there. POSIX-only, matching every
  other process-group assumption in this codebase.
- `uncollected` is a dead end by design: no code path automatically retries,
  redispatches, or deletes the task's workspace lease differently than the
  old `failed` transition did (`_finalize_workspace()` still runs). An
  operator or parent agent decides what, if anything, to do next.
- This does not attempt to recover *any* information about what the Cursor
  leader actually did — that would require reading its stdout, which is
  exactly the fabrication this fix refuses to do.
