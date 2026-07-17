# Bounded debate reference workflow (Wave 5 / PR 15)

This example runs the **reference** bounded debate helper against fixture
runtimes only. It is not a workflow engine and makes no provider network calls
when the plan uses `mock` / fixture CLI profiles.

## Pattern

```text
opening positions → rebuttals → synthesis → validation → optional materialization
```

The parent (this script) remains in control: one explicit invocation, bounded
phases, completion-event cursor polling between dispatches — never a fixed
sleep for task duration.

## Run (offline)

```bash
python3 examples/bounded-debate-workflow/run_fixture_debate.py
```

Exit `0` when the fixture debate completes; `1` on failure.

## What this proves

- Shared `external_root_id` and parent/child lineage under a host anchor
- `completion_events` cursor advancement until each phase is terminal
- Terminal `collect` before the next phase starts
- `openai_compatible` synthesis is parent-owned text (fixture HTTP server)
- Validation gates optional parent materialization (disabled in the default plan)

## Boundaries

See [docs/bounded-debate-workflow.md](../../docs/bounded-debate-workflow.md) for
observability, retry responsibility, and why this is a reference — not an
autonomous council or auto-debate loop.
