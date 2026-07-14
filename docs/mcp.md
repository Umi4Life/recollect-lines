# MCP reference

Program: `recollect-mcp` (stdio JSON-RPC MCP server).

## Launch

```bash
recollect-mcp --home /path/to/.recollect
```

Adapter override flags match `recollect-lines` (`--codex-command`, etc.).

## Protocol

- Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout
- Supported protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`
- Server: `recollect-lines-mcp` v0.1.0
- Diagnostics: stderr only

## Tools (exact names)

| Tool | Purpose |
|------|---------|
| `delegate` | Create + start one task |
| `delegate_batch` | Create + start many tasks independently |
| `status` | Task state, events, artifacts |
| `collect` | Runtime result + broker verification |
| `cancel` | Cancellation with evidence |
| `control` | Operator recovery (`action`: `status`, `cancel`, `collect`, `message`) |
| `message` | Always returns explicit unsupported (no steering) |
| `reconcile` | Post-restart subprocess reconciliation |
| `discover_capabilities` | Runtime/provider inventory |
| `select_candidates` | Policy-aware filtering (parent chooses) |
| `council_validate` | Validate council plan |
| `council_execute` | Execute bounded council plan |

## `delegate` input (schema summary)

Required:

- `task` (string)
- `workspace` (string)

Optional:

| Field | Default | Values |
|-------|---------|--------|
| `execution_mode` | `read_only` | `read_only`, `isolated_worktree` |
| `profile` | `mock` | `mock`, `opencode`, `claude_code`, `codex`, `cursor`, `openai_compatible` |
| `provider` | — | Required when `profile` is `openai_compatible` |
| `timeout_seconds` | `1800` | positive integer |
| `verification_policy` | `none` | `none`, `advisory`, `required` |
| `verify_commands` | — | array of argv arrays |

`delegate` returns `task_id`, `state`, `workspace`, `profile`, etc. — not a fabricated completion.

## Tool result envelope

Successful tool calls return MCP `content` with JSON:

```json
{
  "envelope_version": 1,
  "tool": "collect",
  "ok": true,
  "data": { }
}
```

Errors use `"ok": false` and `"error": { "code", "message" }` at the envelope level (business errors), distinct from JSON-RPC protocol errors.

## Host configuration example

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

Illustrative Hermes-style entry (optional, not required):

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

## Offline acceptance

```bash
python3 scripts/mcp_acceptance.py
```

Uses deterministic fake CLIs — no network or credentials.

## Parent-agent flow

See [user-flows.md](user-flows.md#parent-agent-mcp-flow).
