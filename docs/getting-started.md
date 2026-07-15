# Getting started

## Product sentence

> Recollect Lines is a local-first delegation broker that lets a parent agent safely hand bounded work to existing AI coding runtimes and receive attributable, evidence-backed results.

## Requirements

- **Python 3.11+** (`requires-python` in `pyproject.toml`)
- **Git** (for `isolated_worktree` tasks and acceptance fixtures)
- A supported **runtime CLI** on `PATH` only if you delegate to that profile (e.g. `codex` for `--profile codex`)

Recollect Lines is **not published on PyPI** as of this writing. Install from a repository checkout.

## Install from source

```bash
git clone https://github.com/Umi4Life/recollect-lines.git
cd recollect-lines
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .
```

Verify console entry points (there is **no** legacy `recollect` executable):

```bash
recollect-lines --help
recollect-mcp --help
which recollect && echo "unexpected legacy binary" || echo "ok: no recollect alias"
```

### Contributor editable install

```bash
pip install -e .
PYTHONPATH=src python3 -m recollect_lines --help   # equivalent during development
```

### Offline / CI-style clean install proof

```bash
python3 scripts/clean_install_acceptance.py
```

This builds a disposable venv, installs the package from local artifacts, and checks `recollect-lines` / `recollect-mcp` help output.

## Five-minute quick start (mock, no provider)

Uses the deterministic **mock** profile — no external model or CLI.

```bash
export RECOLLECT_HOME=/tmp/recollect-quickstart
mkdir -p "$RECOLLECT_HOME"

# 1. Create and queue a task
recollect-lines --home "$RECOLLECT_HOME" create \
  --task 'Summarize the README' \
  --workspace "$(pwd)" \
  --profile mock

# Note the task id from JSON output, then:
TASK_ID=tsk_xxxxxxxx   # replace with your id

# 2. Start (mock runs synchronously in-process)
recollect-lines --home "$RECOLLECT_HOME" start "$TASK_ID"

# 3. Complete mock work (real runtimes use collect instead)
recollect-lines --home "$RECOLLECT_HOME" complete "$TASK_ID" \
  --summary 'Mock summary for quickstart'

# 4. Inspect
recollect-lines --home "$RECOLLECT_HOME" status "$TASK_ID"
recollect-lines --home "$RECOLLECT_HOME" list
```

For **real subprocess runtimes** (Codex, Claude Code, OpenCode, Cursor), use a **long-lived** `recollect-mcp` session or the [Codex demo script](../scripts/run_codex_demo.py) — see [user-flows.md](user-flows.md#cli-limitation-subprocess-collection).

### Integrated side-agent fixture (offline)

Proves heterogeneous concurrent children, completion-event polling, normalized results, task trees, steering refusal with `continues` follow-up, and writer isolation — without provider credentials:

```bash
PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py
```

Evidence: [demos/side-agent-fixture-evidence.json](demos/side-agent-fixture-evidence.json). Live multi-runtime dogfood is a separate opt-in runbook: [demos/live-two-runtime-dogfood-runbook.md](demos/live-two-runtime-dogfood-runbook.md).

## MCP quick start

Add to your MCP host (shape is generic; adjust paths):

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

Then call `delegate` → poll `status` → `collect`. Details: [mcp.md](mcp.md).

## Diagnostics

```bash
recollect-lines --home ~/.recollect doctor
recollect-lines --home ~/.recollect doctor --json
```

## Next steps

- [user-flows.md](user-flows.md) — roles, boundaries, runtime matrix
- [demos/side-agent-fixture-evidence.json](demos/side-agent-fixture-evidence.json) — offline integrated side-agent proof
- [demos/codex-marker-evidence.json](demos/codex-marker-evidence.json) — recorded live Codex-through-broker run
- [design/PRD.md](design/PRD.md) — full product contract
