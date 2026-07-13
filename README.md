# Sidecar

Working codename for a local-first side-agent delegation broker.

## Phase 1 status

This repository currently contains the runtime-neutral broker core only:

- durable SQLite task and event storage;
- explicit task-state transitions;
- local artifact directories;
- a deterministic mock adapter;
- a local CLI for lifecycle operations.

OpenCode integration and MCP are intentionally deferred to later phases.

## Run tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Try the CLI

```bash
PYTHONPATH=src python3 -m sidecar --home /tmp/sidecar-demo create \
  --task 'Investigate a flaky test' --workspace /tmp/repo
PYTHONPATH=src python3 -m sidecar --home /tmp/sidecar-demo list
```
