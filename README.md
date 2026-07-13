# Recollect Lines

> **Delegate work. Recollect the signal.**

Recollect Lines is a local-first, **provider- and host-neutral** broker for
delegating bounded agent work to heterogeneous side agents and recollecting
concise, evidence-backed results. It is not tied to any one parent-agent
host or model provider — see [`docs/PRD.md`](docs/PRD.md) §1 and §3.1.
Hermes is one possible operator/host environment among many that can drive
this broker over its documented CLI/MCP interfaces; it is never a required
dependency.

## Documentation

- [`docs/PRD.md`](docs/PRD.md) — canonical, provider-neutral product requirements.
- [`docs/RFC-001.md`](docs/RFC-001.md) — current implementation RFC (architecture, evidence, known limitations).
- [`docs/PHASE-5.md`](docs/PHASE-5.md) — roadmap for the next planned work.
- [`docs/phase-5c.md`](docs/phase-5c.md) — verification-gate policy, timeout liveness safety, generic MCP-host acceptance.
- `docs/phase-{1,2,3,4,5b}.md` — per-phase scope and test evidence as each was implemented.

## Implementation status

Phases 1-5C are implemented: durable SQLite task/event storage, explicit
task-state transitions, local artifact directories with integrity
manifests, profile-policy validation, timeout/cancellation lifecycle
handling with process-group liveness classification, durable
restart recovery and idempotent collection, an opt-in per-task
verification-gate policy, a deterministic mock adapter, an experimental
OpenCode runtime adapter, Git worktree workspace isolation with
broker-side verification, and a local stdio MCP interface — plus the CLI.
See [`docs/RFC-001.md`](docs/RFC-001.md) for the full architecture and
known limitations, and `docs/phase-*.md` for per-phase evidence.

**Honest gap against the product PRD:** the MVP boundary calls for at
least two heterogeneous runtime adapters, with Claude Code CLI and Codex
CLI as the preferred initial pair. Only one adapter — OpenCode, marked
experimental — is implemented today. A post-Phase-5C roadmap decision has
since sequenced Claude Code CLI (Phase 6A), Codex CLI (Phase 6B), and
Cursor CLI (Phase 6B.5) as the next adapters, plus a separately scheduled
plural OpenAI-compatible provider fabric (Phase 6C) and capability
discovery/routing/bounded model-council patterns (Phase 6D) — none
implemented yet. See [`docs/PRD.md`](docs/PRD.md) §9,
[`docs/RFC-001.md`](docs/RFC-001.md) §8/§10, and
[`docs/PHASE-5.md`](docs/PHASE-5.md) for the full capability accounting
and roadmap.

## CI

`.github/workflows/ci.yml` runs on every pull request and push to `master`,
against Python 3.11 and 3.13: the full unittest suite (`PYTHONPATH=src`),
the generic MCP-host acceptance harness (`scripts/mcp_acceptance.py`),
`compileall`, and a whitespace check. On one matrix leg it also installs the
package and smoke-tests the `recollect`/`recollect-mcp` console entry
points. It runs no external services, network calls, or model credentials.

## Run tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Run the generic MCP-host acceptance harness

`scripts/mcp_acceptance.py` drives a real `recollect-mcp` subprocess over
its stdio JSON-RPC transport — exactly what any MCP-compatible host does —
against a disposable local Git fixture, with no network access or model
credentials required:

```bash
python3 scripts/mcp_acceptance.py
```

See [`docs/phase-5c.md`](docs/phase-5c.md) for what it proves.

## Try the CLI

```bash
PYTHONPATH=src python3 -m recollect_lines --home /tmp/recollect-demo create \
  --task 'Investigate a flaky test' --workspace /tmp/repo
PYTHONPATH=src python3 -m recollect_lines --home /tmp/recollect-demo list
```

## Configure an MCP host

Any MCP-compatible host can launch `recollect-mcp` as a local stdio
server. A generic client configuration (after `pip install .`, so the
`recollect-mcp` console script is on `PATH`) looks like:

```json
{
  "mcpServers": {
    "recollect-lines": {
      "command": "recollect-mcp",
      "args": ["--home", "/path/to/.recollect"]
    }
  }
}
```

This is not specific to any one client — it's the same shape most
MCP-stdio hosts expect. Nothing about Recollect Lines requires a
particular host to be present or configured.

**Optional, illustrative only:** an operator running Hermes as their
parent-agent environment could add the same server under Hermes's own MCP
configuration surface, e.g. an entry equivalent to:

```json
{
  "mcpServers": {
    "recollect-lines": {
      "command": "recollect-mcp",
      "args": ["--home", "~/.recollect"]
    }
  }
}
```

This is one possible operator configuration among many, never a required
integration or an acceptance criterion for this project.
