# User flows

Three roles appear in every delegation: **operator/parent** (human or parent agent), **Recollect Lines broker**, and **runtime backend** (Codex CLI, Cursor, Claude Code, OpenCode, or HTTP provider). The broker owns task state, artifacts, timeouts, cancellation evidence, and optional broker-verified checks. Runtimes are adapters — not plugins inside Recollect Lines.

```text
Operator / parent agent
        |
        |  CLI or MCP (stdio JSON-RPC)
        v
 Recollect Lines broker  ---- supervise ---->  Runtime backend
        |                                        (codex, claude, …)
        |  durable SQLite + artifact dir
        v
 Evidence-backed result (summary + artifacts, optional verification)
```

## Human / operator CLI flow

The CLI exposes discrete lifecycle commands; there is **no** single `submit` command.

### Typical sequence

```bash
BROKER=~/.recollect

# 1. Create (queues task)
recollect-lines --home "$BROKER" create \
  --task 'Inspect failing test output' \
  --workspace /path/to/repo \
  --profile mock \
  --mode read_only \
  --timeout 600

TASK_ID=tsk_...   # from JSON

# 2. Start runtime
recollect-lines --home "$BROKER" start "$TASK_ID"

# 3. Observe (any time)
recollect-lines --home "$BROKER" status "$TASK_ID"

# 4. Terminal collection
#    mock: use complete (see getting-started.md)
#    subprocess runtimes: collect (see limitation below)
recollect-lines --home "$BROKER" collect "$TASK_ID"

# 5. Cancel while active (optional)
recollect-lines --home "$BROKER" cancel "$TASK_ID" --reason 'operator abort'

# 6. Operator recovery/control (explicit actions)
recollect-lines --home "$BROKER" control "$TASK_ID" --action status
recollect-lines --home "$BROKER" control "$TASK_ID" --action cancel
```

### CLI limitation: subprocess collection

`recollect-lines` is a **short-lived process**. Subprocess-backed tasks (`opencode`, `claude_code`, `codex`, `cursor`) hold an in-memory process handle in the broker instance that called `start`. If that process exits before `collect`, a **new** CLI invocation cannot attach to the running runtime; `collect` may reconcile to `failed` with `missing_process_handle` even when the runtime already finished.

**Practical options for real runtimes:**

1. **Parent-agent MCP** — keep `recollect-mcp` running; use `delegate` (create+start) then `collect` on the same server ([mcp.md](mcp.md)).
2. **Orchestration script** — one process drives create → start → poll → collect ([scripts/run_codex_demo.py](../scripts/run_codex_demo.py)).
3. **Mock / certification** — `complete` for mock; `certify` for integration checks.

This is intentional restart-safety semantics, not session resume. See [design/RFC-001.md](design/RFC-001.md).

## Parent-agent MCP flow

Any MCP stdio host launches `recollect-mcp` and calls tools by name.

### Minimal configuration

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

Host-specific wrappers (Cursor, Claude Desktop, custom agents) use the same `command` / `args` shape.

### Lifecycle (exact tool names)

1. **`initialize`** / **`notifications/initialized`** — standard MCP handshake.
2. **`tools/list`** — discover tools (includes `delegate`, `status`, `collect`, `cancel`, `reconcile`, `discover_capabilities`, …).
3. **`delegate`** — create and start one task. Required: `task`, `workspace`. Common: `profile`, `execution_mode`, `timeout_seconds`, `verification_policy`, `verify_commands`.
4. **`status`** — durable state, events, artifact manifest (`task_id`).
5. **`collect`** — runtime result + broker verification artifacts (`task_id`). Idempotent on terminal tasks.
6. **`cancel`** — request cancellation with evidence (`task_id`, optional `reason`).
7. **`reconcile`** — after broker restart, classify orphaned subprocess state (optional `task_id`).

Example `delegate` arguments for Codex read-only inspection:

```json
{
  "task": "Read alpha.txt and beta.txt. Reply with only the filename containing MARKER_ALPHA.",
  "workspace": "/path/to/fixture-repo",
  "profile": "codex",
  "execution_mode": "read_only",
  "timeout_seconds": 120
}
```

Offline proof without provider calls: `python3 scripts/mcp_acceptance.py`.

Full schemas: [mcp.md](mcp.md).

## Runtime / backend flow

Recollect Lines does **not** embed Codex, Cursor, or Claude. Each **profile** selects an adapter that supervises an external CLI or HTTP endpoint.

| Profile | Backend | How broker supervises | Notes |
|---------|---------|----------------------|-------|
| `mock` | In-process stub | Synchronous | Tests, quickstart |
| `opencode` | OpenCode CLI | `npx opencode-ai` subprocess | Experimental |
| `claude_code` | Claude Code CLI | `claude -p` subprocess | Experimental |
| `codex` | Codex CLI | `codex exec --json` subprocess | Experimental; ChatGPT subscription quota |
| `cursor` | Cursor CLI | `cursor-agent` subprocess | Experimental |
| `openai_compatible` | HTTP chat API | Direct HTTP runtime | Requires `--providers-config` |

### What works today

- Delegate with bounds (timeout, `read_only` / `isolated_worktree`)
- Durable task/event storage and artifact manifests
- Process-group cancellation with evidence
- Optional per-task verification gate (`none` / `advisory` / `required`)
- Post-restart **reconciliation** (truthful `failed` / `recovery_required`, not fabricated success)
- Capability discovery and parent-directed routing (`discover`, `select`, `council`)

### What does not work / is not claimed

- **Session resume** — no re-attachment to a running runtime after broker restart with full result recovery
- **In-flight steering** — `message` / `control --action message` always refuse
- **Continuous upstream CLI certification** — adapters are spike-tested, not pinned to every upstream release
- **PyPI install** — source install only until published

## Recorded demo

Live Codex marker identification through MCP: [demos/README.md](demos/README.md).
