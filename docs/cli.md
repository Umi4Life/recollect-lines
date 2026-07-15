# CLI reference

Program: `recollect-lines` (console script from `recollect_lines.cli:main`).

Global options:

| Flag | Default | Purpose |
|------|---------|---------|
| `--home` | `.recollect` | Broker data directory (SQLite + artifacts) |
| `--opencode-command` | built-in | JSON array overriding OpenCode CLI prefix |
| `--claude-command` | `claude` | JSON array overriding Claude Code CLI prefix |
| `--codex-command` | `codex` | JSON array overriding Codex CLI prefix |
| `--cursor-command` | `cursor-agent` | JSON array overriding Cursor CLI prefix |
| `--providers-config` | — | JSON file for `openai_compatible` profile |

There is **no** `recollect` executable (removed in field-readiness work).

## Task lifecycle commands

| Command | Purpose |
|---------|---------|
| `create` | Queue a task (`--task`, `--workspace`, `--runtime`, `--mode`, `--timeout`, …) |
| `start` | Launch the selected runtime |
| `status` | State, events, artifacts |
| `complete` | Finish **mock** tasks with `--summary` |
| `collect` | Finish **subprocess/API** tasks; gather `result.json` + verification |
| `cancel` | Cancel with recorded reason |
| `timeout` | Timeout with process-group liveness check |
| `reconcile` | Reconcile one task after restart |
| `reconcile-all` | Reconcile all pending subprocess tasks |
| `control` | Operator recovery (`--action status\|cancel\|collect\|message`) |
| `verify` | Run broker-verified commands on a task |
| `list` | List all tasks |

### `create` flags

```
--task TEXT            (required)
--workspace PATH       (required)
--mode read_only|isolated_worktree   (default read_only)
--runtime mock|opencode|claude_code|codex|cursor|openai_compatible   (preferred)
--profile NAME         (deprecated alias for --runtime)
--model NAME           (optional; persisted only)
--agent-profile NAME   (optional behavioral role; persisted only)
--result-schema NAME   (optional result contract; persisted only)
--provider NAME        (required for openai_compatible)
--timeout SECONDS      (default 1800)
--verification-policy none|advisory|required
--verify-command JSON  (repeatable argv array)
```

Runtimes and modes are validated against broker policy before queueing. Legacy `--profile` follows the same translation rules as MCP; see [migration-runtime-profile.md](migration-runtime-profile.md).

## Discovery and routing

| Command | Purpose |
|---------|---------|
| `discover` | Runtime/provider capability inventory (JSON) |
| `select` | Parent-directed candidate filtering |
| `council validate` | Validate a bounded council plan (`--plan JSON`) |
| `council execute` | Execute a validated council plan |

## Operations

| Command | Purpose |
|---------|---------|
| `doctor` | Offline diagnostics (`--json`, optional `--workspace`) |
| `certify` | Integration certification (`--profile` required; dry-run default) |

`certify` live execution requires `--execute-live --i-accept-billed-remote-calls` (HTTP providers). CLI adapter certification uses fixture or live modes documented in [history/phases/phase-7b.md](history/phases/phase-7b.md).

## Help output (verified)

```text
usage: recollect-lines [-h] [--home HOME] ...
  {create,start,status,complete,collect,cancel,timeout,reconcile,control,verify,list,reconcile-all,discover,select,council,doctor,certify} ...
```

## Subprocess collection note

See [user-flows.md](user-flows.md#cli-limitation-subprocess-collection): one-shot `start` then a later `collect` in a new shell loses the process handle. Use MCP or an orchestration script for real runtimes.
