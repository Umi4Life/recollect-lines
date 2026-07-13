# Recollect Lines

> **Delegate work. Recollect the signal.**

Recollect Lines is a local-first broker for delegating bounded agent work and recollecting concise, evidence-backed results.

## Phase 1 status

This repository currently contains the runtime-neutral broker core only:

- durable SQLite task and event storage;
- explicit task-state transitions;
- local artifact directories with integrity manifests;
- profile-policy validation;
- timeout and cancellation lifecycle handling;
- a deterministic mock adapter;
- a local CLI for lifecycle operations.

OpenCode integration and MCP are intentionally deferred to later phases.

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
