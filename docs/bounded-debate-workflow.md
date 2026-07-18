# Bounded debate reference workflow

Parent-directed **reference helper** for the dogfood pattern:

```text
opening positions → rebuttals → synthesis → validation → optional materialization
```

This is deliberately **not** a workflow engine, daemon, webhook consumer, or
auto-debate loop. Callers invoke `run_bounded_debate_workflow()` explicitly,
supply a bounded plan, and remain responsible for retry policy, round counts,
and when a comparison is "enough".

Implementation: [`bounded_debate_workflow.py`](../src/recollect_lines/bounded_debate_workflow.py).
Fixture example: [`examples/bounded-debate-workflow/`](../examples/bounded-debate-workflow/).

## When to use it

Use this helper when you want a copy-pasteable orchestration sketch that:

- tags every task with the same `external_root_id` for audit lookup (`task_tree` by `external_root_id`)
- hangs phase tasks under a host anchor with `parent_task_id` / `relationship`
- polls the durable `completion_events` cursor between phases — **never** a fixed sleep for task duration
- collects terminal outputs before advancing
- validates synthesis and optionally materializes with explicit parent ownership

Do **not** use it when you need autonomous winner selection, recursive councils,
push notifications, or mid-flight steering — those are out of scope (same
non-goals as [`council.py`](../src/recollect_lines/council.py)).

## Plan shape

```json
{
  "workspace": "/path/to/git/repo",
  "external_root_id": "host-debate-session-1",
  "acceptance_criteria": "reconciled recommendation",
  "opening_positions": [
    {"id": "alice", "profile": "claude_code", "task": "State opening position …"}
  ],
  "rebuttals": [
    {"id": "alice-rebuttal", "profile": "claude_code", "task": "Rebut …", "responds_to": "bob", "relationship": "continues"}
  ],
  "synthesis": {
    "id": "synthesis",
    "profile": "openai_compatible",
    "provider": "local",
    "task": "Synthesize openings and rebuttals …"
  },
  "materialization": {
    "enabled": false,
    "relative_path": "docs/debate-synthesis.md",
    "dry_run": false
  },
  "bounds": {"poll_timeout_seconds": 30}
}
```

`parse_bounded_debate_plan()` validates profiles, provider requirements, unique
participant ids, and safe materialization paths before any task is dispatched.

## Phase behavior

| Phase | What happens |
|-------|----------------|
| `opening_positions` | Dispatch each participant as a child under the host anchor; poll `completion_events` until all task ids are terminal; `collect` each. |
| `rebuttals` | Same pattern. Task text may include upstream opening summaries. `responds_to` sets `parent_task_id` to the opening task; default `relationship` is `continues`. |
| `synthesis` | One task (typically `openai_compatible`) receives prior summaries in its prompt. Still polled + collected like other phases. |
| `validation` | Parent-side check: `contract_status` must be `satisfied` or `not_requested`, and `acceptance_criteria` must appear in the synthesis summary (case-insensitive). No silent pass on `unsatisfied_fallback`. |
| `materialization` | Optional. When `enabled: false`, the workflow reports that the parent retains workspace authority. When enabled, `apply_parent_materialization()` writes to an explicit workspace-relative path or `dry_run`s — always with `provider_wrote_files: false`. |

If any phase records a terminal child failure, later phases are **not** started.

## Observability and cursors

1. Record `broker.store.event_high_water_mark()` (or the `completion_cursor` from `delegate`) before dispatching a phase batch.
2. Poll `broker.completion_events_since(after_event_id=cursor, root_task_id=anchor_task_id)` until every expected task id appears.
3. Advance `cursor` to `page["next_cursor"]` each poll.
4. `collect` each terminal task before building the next phase's prompts.

The helper's `wait_for_task_completions()` implements steps 2–3. The poll loop
sleeps only between cursor checks (typically tens of milliseconds) — not for a
guessed task runtime.

Compact completion events carry `result_summary`, lineage fields
(`external_root_id`, `parent_task_id`, `root_task_id`), and artifact counts —
not raw log bodies. See [mcp.md](mcp.md#completion-events-polling-contract-wave-5--pr-13).

## Runtime capability contract

`openai_compatible` synthesis is **text only** (`materialization_owner:
parent_applies_text`). The broker never claims the provider wrote files. CLI
runtimes may mutate a broker-owned worktree in `isolated_worktree` mode, but
merging that worktree into the real repository is still parent-owned — this
helper's optional materialization path is for **applying synthesis text** with
an explicit relative path.

The workflow result includes `synthesis_capability_note` from the runtime
descriptor so callers can surface honest ownership in host UI or logs.

## Retry responsibility

This helper runs **one** bounded pass. It does not:

- reschedule failed children
- spawn `continues` follow-ups automatically (you may model those explicitly in `rebuttals`)
- implement exponential backoff between cursor polls beyond a single `poll_timeout_seconds` per phase

The parent that called the helper decides whether to retry a failed opening,
add another rebuttal round, or stop. That intentional gap is why this is a
**reference** implementation, not an autonomous council.

## Python API

```python
from recollect_lines.bounded_debate_workflow import run_bounded_debate_workflow

result = run_bounded_debate_workflow(broker, plan_dict)
# result["status"] == "completed" | "failed"
# result["phases"] — per-phase evidence
# result["participants_collected"] — terminal summaries + contract_status
```

Hermetic tests: [`tests/test_bounded_debate_workflow.py`](../tests/test_bounded_debate_workflow.py).

## Related docs

- [operator-guide.md](operator-guide.md#completion-driven-orchestration-no-sleep-loops) — completion-driven orchestration
- [operator-guide.md](operator-guide.md#materialize--validate--record-the-honest-parent-workflow) — materialize → validate → record
- [demos/live-two-runtime-dogfood-runbook.md](demos/live-two-runtime-dogfood-runbook.md) — opt-in live dogfood (not CI)
