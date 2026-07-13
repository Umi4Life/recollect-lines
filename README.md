# Recollect Lines

> **Delegate work. Recollect the signal.**

Recollect Lines is a local-first broker for delegating bounded agent work and recollecting concise, evidence-backed results.

## Documentation

- [`docs/PRD.md`](docs/PRD.md) — canonical, provider-neutral product requirements.
- [`docs/RFC-001.md`](docs/RFC-001.md) — current implementation RFC (architecture, evidence, known limitations).
- [`docs/PHASE-5.md`](docs/PHASE-5.md) — roadmap for the next planned work.
- `docs/phase-{1,2,3,4}.md` — per-phase scope and test evidence as each was implemented.

## Implementation status

Phases 1-4 are implemented: durable SQLite task/event storage, explicit
task-state transitions, local artifact directories with integrity
manifests, profile-policy validation, timeout/cancellation lifecycle
handling, a deterministic mock adapter, an experimental OpenCode runtime
adapter, Git worktree workspace isolation with broker-side verification,
and a local stdio MCP interface — plus the CLI. See
[`docs/RFC-001.md`](docs/RFC-001.md) for the full architecture and known
limitations, and `docs/phase-{1,2,3,4}.md` for per-phase evidence.

## CI

`.github/workflows/ci.yml` runs on every pull request and push to `master`,
against Python 3.11 and 3.13: the full unittest suite (`PYTHONPATH=src`),
`compileall`, and a whitespace check. On one matrix leg it also installs the
package and smoke-tests the `recollect`/`recollect-mcp` console entry
points. It runs no external services, network calls, or model credentials.

## Run tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Try the CLI

```bash
PYTHONPATH=src python3 -m recollect_lines --home /tmp/recollect-demo create \
  --task 'Investigate a flaky test' --workspace /tmp/repo
PYTHONPATH=src python3 -m recollect_lines --home /tmp/recollect-demo list
```
