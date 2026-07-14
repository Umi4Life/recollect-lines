# Demos

Recorded end-to-end proofs that Recollect Lines delegates to real runtimes and returns evidence-backed results.

## Codex marker identification (live)

**Script:** `scripts/run_codex_demo.py`

**Default:** dry-run plan only (no provider call).

**Live run:**

```bash
python3 scripts/run_codex_demo.py --execute-live --acknowledge-provider-call
```

**Evidence:** [codex-marker-evidence.json](codex-marker-evidence.json)

**Cancellation (offline, no provider call):**

```bash
python3 scripts/run_codex_demo.py --demo-cancel-fixture
```

## What the live demo proves

- A parent-style MCP client can `delegate` bounded read-only work to the real **Codex CLI** through `recollect-mcp`
- The broker supervises the subprocess, records lifecycle transitions, and `collect` returns a runtime-reported summary (`alpha.txt` for the MARKER_ALPHA fixture)
- Artifacts (events JSONL, manifests) are stored under the broker home

## What it does not prove

- Separate one-shot `recollect-lines start` / `collect` shell invocations (process handle is per broker instance)
- Post-restart session resume
- USD cost accounting (Codex subscription quota path does not expose a dollar amount to this harness)

See [../user-flows.md](../user-flows.md) for role boundaries.
