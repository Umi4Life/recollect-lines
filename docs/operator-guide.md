# Operator guide

Product-first orientation for a fresh operator. For command syntax, see
[cli.md](cli.md) and [mcp.md](mcp.md).

## What Recollect Lines is

Recollect Lines is a **local-first delegation broker**. A parent agent (human or
software) hands **bounded** work to an **existing** AI coding runtime and receives
an attributable, evidence-backed result.

```text
Parent / operator
        |
        |  CLI (`recollect-lines`) or stdio MCP (`recollect-mcp`)
        v
 Recollect Lines broker  ---- supervises ---->  Runtime backend
        |                    (codex, claude, cursor, opencode, HTTP API)
        |  durable SQLite + artifact directory
        v
 Summary + artifact manifest (+ optional broker-verified checks)
```

The broker owns task state, timeouts, cancellation evidence, workspace policy,
and optional verification gates. It does **not** replace your editor, agent host,
or provider account.

## What it is not

| Is not | Why it matters |
|--------|----------------|
| A new IDE or coding agent | You keep Codex, Cursor, Claude Code, OpenCode, or your HTTP gateway |
| An OpenCode/Codex plugin | Runtimes stay external; Recollect Lines supervises them |
| “Just an MCP server” | MCP is one interface; the product is durable delegation + evidence |
| A secret store | Config holds **names** of environment variables, never credential values |
| Session resume after restart | Post-restart behavior is **reconcile**, not re-attach with full recovery |

## Bounded, parent-directed, multi-runtime role

Recollect Lines is designed for **parent-directed** delegation:

- The parent chooses runtime, execution mode, timeout, and optional verification.
- Work is **bounded** (time limits, read-only or isolated worktree modes, explicit refusal of in-flight steering).
- Multiple children can run under one host operation via `external_root_id`, `parent_task_id`, and `task_tree` — the parent polls `completion_events` (never a fixed sleep between rounds — see [Completion-driven orchestration](#completion-driven-orchestration-no-sleep-loops) below) and collects normalized results.
- `openai_compatible` is a **text/synthesis** runtime over HTTP; CLI adapters are **workspace-mutating supervisors** when not in `read_only` mode.

When mid-task steering is required, expect an explicit refusal — create a follow-up task with `relationship=continues` instead of session resume.

## Supported runtimes and parent hosts

### Runtime backends (what executes work)

| Runtime | Backend | Supervision | Typical use |
|---------|---------|-------------|-------------|
| `mock` | In-process stub | Synchronous | Tests, offline proofs |
| `opencode` | OpenCode CLI | Subprocess | Experimental workspace tasks |
| `claude_code` | Claude Code CLI | Subprocess | Experimental read-only / worktree tasks |
| `codex` | Codex CLI | Subprocess | Experimental read-only / worktree tasks |
| `cursor` | Cursor CLI | Subprocess | Experimental read-only / worktree tasks |
| `openai_compatible` | HTTP chat API | Direct HTTP | Text generation only — **no** subprocess/worktree supervision |

Claude Code launches use a **task-aware permission-mode policy** (see [cli.md](cli.md#claude-code-permission-mode-policy-claude_code-runtime-only)): prose/review read-only tasks avoid `--permission-mode plan` (which can meta-refuse debate-style work) while structural read-only safety still comes from `--tools` / `--disallowedTools`. Unknown categories default to `plan` conservatively; `isolated_worktree` uses `acceptEdits`.

### MCP parent hosts (where `recollect-mcp` can be registered)

`recollect-lines mcp install` supports hosts this project also supervises as runtimes:

| Host | Global config | Project config |
|------|---------------|----------------|
| `cursor` | `~/.cursor/mcp.json` | `.cursor/mcp.json` |
| `claude_code` | `~/.claude.json` | `.mcp.json` |
| `codex` | `~/.codex/config.toml` | `.codex/config.toml` |

Claude Desktop, VS Code, OpenCode-as-host, and other MCP parents are **not** claimed. Use `mcp print` to preview a registration before `mcp install`.

## `openai_compatible` vs workspace-mutating runtimes

| Dimension | `openai_compatible` | CLI runtimes (`codex`, `claude_code`, `cursor`, `opencode`) |
|-----------|---------------------|-------------------------------------------------------------|
| What it does | HTTP chat-completions to a configured endpoint | Launches and supervises an external CLI in a workspace |
| Workspace authority | **None** — synthesis only; cannot enforce git worktree isolation by itself | Broker enforces `read_only` or `isolated_worktree` around the adapter |
| Provider config | Required (`provider` name + YAML/JSON config) | Optional unless you also use HTTP synthesis |
| Credential model | `api_key_env` names an environment variable | Provider credentials irrelevant; runtime uses its own CLI auth |
| Collect path | In-process HTTP collect | Subprocess `collect` on the **same** broker/MCP instance that started the task |

**Parent-side materialization:** the parent (MCP host or orchestration script) must keep one long-lived `recollect-mcp` process for subprocess runtimes, call `delegate`, poll `completion_events` (a plain `status` call is a passive point read — it never on its own drives a task toward a terminal state), then `collect`. Short-lived `recollect-lines start` followed by a new-shell `collect` loses the process handle — see [user-flows.md](user-flows.md#cli-limitation-subprocess-collection).

For HTTP tasks, the parent supplies `runtime=openai_compatible` and `provider=<name>`; the broker validates config at startup and performs the HTTP call — still no inline secrets in task arguments.

### Completion-driven orchestration (no sleep loops)

The dogfood problem this closes: a parent running several
delegate rounds (e.g. a bounded model-council comparison, [PRD §3.1](design/PRD.md#31-delegation-shape-dynamic-not-fixed))
used to sleep a guessed duration between dispatching one round and checking
whether it finished. That guess is either too short (the parent checks too
early and has to guess again) or wastefully long. The fix is a poll loop, not
a bigger guess:

1. **Dispatch** — `delegate`/`delegate_batch`. The response includes
   `completion_cursor`, the global event high-water mark at that exact
   moment — the baseline for step 2, with no extra round-trip needed.
2. **Poll for completions** — call `completion_events` with
   `after_event_id=completion_cursor` (filtered by `task_id`/`root_task_id`
   for this round) on a short interval of the parent's own choosing. Each
   call opportunistically finalizes (non-blocking) any of this round's tasks
   that have already finished on the same long-lived `recollect-mcp`
   process, and returns immediately either way — it never blocks behind a
   task that is still running.
3. **Collect** — once a task id appears in a completion event, `collect` it
   for the full artifact-backed result (the event itself is a compact
   notification only — see [mcp.md](mcp.md#completion-events-polling-contract)).
4. **Advance** — start the next round once this round's expected task ids
   are all accounted for.

This is deliberately bounded observation, not a workflow engine: there is no
push notification (the parent always initiates each check), no built-in
retry/backoff or round scheduler, and no automatic winner selection or
recursive council scheduling — the parent still decides the graph, the round
count, and when a comparison is "enough" (see [council.py](../src/recollect_lines/council.py)'s
same non-goals for the bounded council primitive).

[bounded-debate-workflow.md](bounded-debate-workflow.md) adds a
copy-pasteable reference for the full dogfood debate pattern (opening
positions → rebuttals → synthesis → validation → optional parent
materialization)
— still caller-controlled, not an autonomous council.

### Tool-access profiles and operator-approved repository read

`execution_mode` (`read_only` / `isolated_worktree`) governs **workspace mutation
authority only**. `tool_access_profile` is a separate, explicit dimension for
which runtime tool identifiers a launch may use. Omitting `tool_access_profile`
keeps today's defaults unchanged: local read-only Claude Code tasks still get
`Read,Grep,Glob` only and **do not** advertise `repository.remote.read`.

The built-in `operator_approved_repository_read` profile is **opt-in** and
**configuration-backed**. It never expands defaults on its own. To use it:

1. Add a finite, exact allowlist of external/MCP read tool identifiers to your
   operator config (`.recollect/config.yaml` or resolved equivalent):

```yaml
tool_access_profiles:
  operator_approved_repository_read:
    allowed_external_tools:
      - mcp__github__get_pull_request
      - mcp__github__list_commits
```

2. Pass `tool_access_profile=operator_approved_repository_read` on `create` /
   MCP `delegate` together with `required_capabilities` that include
   `repository.remote.read` when the task needs remote evidence.

3. Keep MCP credentials and server configuration in the **runtime's** normal
   configuration (for example Claude Code's MCP host files). The broker does
   **not** forward generic credentials, copy environment secrets into task
   artifacts, or proxy arbitrary MCP tools.

**Threat model (explicit limits):**

| Trust boundary | What the broker does | What it does *not* do |
|----------------|----------------------|------------------------|
| Profile selection | Rejects unknown, incompatible, unconfigured, wildcard, or duplicate allowlists before launch | Infer profile choice from task prose |
| Tool allowlist | Passes resolved local read tools plus configured exact MCP ids into Claude `--tools` | Grant prefix/wildcard/server-wide MCP access |
| `repository.remote.read` preflight | Statically advertises the capability only when an approved profile with a non-empty allowlist is selected | Verify external source truth or MCP server responses |
| Audit metadata | Records profile id and `external_tool_count` in normalized results | Expose tool arguments, queries, paths, responses, or credentials |

The broker treats each configured MCP tool identifier as **operator-curated
read-only**. It does not infer safety from a tool name pattern alone.

Custom instance names are also supported when declared under
`tool_access_profiles` with `profile_kind: operator_approved_repository_read`.

Restart `recollect-mcp` (or start a fresh CLI invocation) after editing
`tool_access_profiles`; configuration is a startup snapshot.

### Runtime capability contract

Every runtime exposes one `capability_contract` (see `discover_capabilities` / `discover` output, `capability_contract.py`) instead of scattered per-adapter conditionals:

| Field | Meaning |
|-------|---------|
| `output_kind` | `workspace_mutation` (CLI runtimes, `mock`) or `text_synthesis` (`openai_compatible`) |
| `owns_worktree` | Whether this runtime's policy can ever get a broker-owned `isolated_worktree` |
| `mutates_workspace` | Whether the underlying tool actually writes files (`mock` never does, even in its own worktree) |
| `materialization_owner` | `parent_merges_broker_worktree` or `parent_applies_text` |
| `parent_materialization_required` | Always `true` — see below |
| `materialization_note` | Human-readable statement of what this runtime does and does not do |

Requesting an `execution_mode` a runtime's policy does not permit (e.g. `isolated_worktree` with
`openai_compatible`) is rejected at `create()`/delegate time, before the task is ever queued or
started — never a false success. The error names the runtime's `materialization_note` and any
other registered runtimes that do support the requested mode.

### Materialize → validate → record: the honest parent workflow

**The broker never writes a task's result into the caller's real workspace, for any runtime.**
`isolated_worktree` changes land in a broker-owned git worktree/branch under `<home>/worktrees/`,
never merged back automatically; `openai_compatible` produces prose with no worktree at all. The
parent that delegated the task always owns three steps after `collect`:

1. **Materialize** — for worktree-capable runtimes, review and merge (or cherry-pick) the
   broker-owned branch into the real repository yourself; for `openai_compatible`, apply the
   returned text yourself (there is no branch to merge).
2. **Validate** — run your own tests/review against the materialized change. A runtime-reported
   success or a `verify_commands` pass is evidence, not a substitute for this step.
3. **Record** — commit with a reference to the task id (and `root_task_id`/`external_root_id` for
   multi-child trees) so the change stays attributable to its delegated task.

## Data and workspace authority boundaries

| Asset | Authority | Notes |
|-------|-----------|-------|
| Broker home (`--home`, default `.recollect`) | Operator | SQLite task DB, artifact directories, operator config |
| Operator config (`.recollect/config.yaml`) | Operator | Plaintext endpoint metadata + `api_key_env` **names** only |
| Environment variables | Operator OS / secret manager | Actual API keys and tokens live here |
| Task workspace path | Parent declares; broker enforces mode | `read_only` or `isolated_worktree` — source repo is not mutated in isolated mode |
| Runtime CLI auth | External runtime | Codex/Claude/Cursor sessions are outside broker config |
| MCP host config | Parent host (Cursor/Claude/Codex) | `mcp install` writes registration only — no secrets |

## Security model

### Plaintext vs secret boundaries

- **Safe in config files:** provider name, `base_url`, `default_model`, `api_key_env` (the variable **name**), TLS flags, `ca_bundle` **path**, capability flags.
- **Never in config:** API keys, tokens, passwords, PEM blocks, `Authorization` headers, private keys. Unknown keys and secret-shaped field names are rejected at validation.
- **Never in CLI/MCP output:** `doctor`, `config validate`, `provider list/show/test`, and `init` redact credential values. Set env vars in your shell or secret manager, not in tracked files.

### Config precedence (fail-truthfully)

Highest precedence wins; configured sources do not silently fall through:

1. `--providers-config PATH`
2. `RECOLLECT_CONFIG` environment variable
3. `./.recollect/config.{yaml,yml,json}` (repo-local)
4. `~/.recollect/config.{yaml,yml,json}` (user-level)
5. Legacy `./providers.json` (backward compatibility only — prefer YAML operator config)

If tier 1 or 2 is set but missing/invalid, the command **fails** with that path's error.

Preferred operator path: `recollect-lines init` or `config init` → edit or `provider add` → `config validate` → `doctor`.

### TLS and CA bundles

- HTTPS endpoints verify TLS by default (`tls_verify: true`).
- `ca_bundle` must be a **filesystem path** to a CA bundle file — never inline certificate content.
- **Linux:** system trust store is used automatically; you usually do not set `ca_bundle`.
- **macOS (python.org builds):** run `Install Certificates.command` once, or install `certifi` and set `ca_bundle` to `python3 -c "import certifi; print(certifi.where())"`.
- Do not hard-code a single distro path (for example `/etc/ssl/cert.pem`) as a universal default.

### Restart and reload

Provider configuration is a **startup snapshot**. The broker/MCP process reads the resolved config file once at launch. Editing the file while the process is running has **no effect** until you restart `recollect-mcp` or start a new CLI invocation. `doctor` and MCP `discover_capabilities` report `restart_required_for_changes: true`.

## Five-minute success path (clean environment)

This path is **deterministic and offline**. CI runs the same sequence via
`scripts/five_minute_acceptance.py`.

**Requirements:** Python 3.11+, Git (for the fixture delegate ping).

```bash
git clone https://github.com/Umi4Life/recollect-lines.git
cd recollect-lines
python3 scripts/five_minute_acceptance.py
```

The script builds a disposable venv, installs from local artifacts, then:

1. **Install** — verifies `recollect-lines` and `recollect-mcp` entry points
2. **Init** — `recollect-lines init --json` creates `./.recollect` + starter `config.yaml`
3. **Validate** — `recollect-lines config validate --json`
4. **Provider** — `provider add` with `--api-key-env ACCEPTANCE_PROVIDER_API_KEY` only (no raw key)
5. **Doctor** — `recollect-lines doctor --json` (secrets redacted)
6. **MCP** — `mcp print` then `mcp install` to a temporary host config with post-install initialize ping
7. **Delegate ping** — bounded `delegate` + `collect` through `recollect-mcp` using the deterministic OpenCode fixture (no live provider)

For a human walkthrough with explanations, see [getting-started.md](getting-started.md#five-minute-clean-operator-path).

### After the five-minute path

| Goal | Next step |
|------|-----------|
| Register with Cursor / Claude Code / Codex | `recollect-lines mcp install --host <host> --scope global` |
| Add a real HTTP provider | `provider add` with your gateway URL and `api_key_env` name; export the env var; `provider test NAME` (add `--live` only when you accept billed calls) |
| Delegate real CLI work | Keep `recollect-mcp` running; use MCP `delegate` / `collect` or an orchestration script |
| Offline integrated proof | `PYTHONPATH=src python3 scripts/side_agent_fixture_acceptance.py` |

## Troubleshooting (from dogfood failures)

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| `collect` → `missing_process_handle` | New CLI shell after `start`; broker restart | Use long-lived `recollect-mcp` or one orchestration script for subprocess runtimes |
| Provider change ignored | Hot reload is not supported | Restart `recollect-mcp` after editing config |
| `PROVIDER_SECRET_REFERENCE_MISSING` in doctor | `api_key_env` not set in environment | `export YOUR_API_KEY_ENV=...` in the shell that launches the broker |
| TLS / certificate verify failed | macOS python.org CA bundle, corporate proxy | Follow [getting-started.md](getting-started.md#ca-bundles--custom-certificates); set `ca_bundle` to a real bundle path |
| `provider add` refuses to write | Active source is legacy `./providers.json` | Run `init` / `config init`, or pass `--path ./.recollect/config.yaml` |
| `delegate` rejected for `openai_compatible` | Missing `provider` or unset config | `config validate`; ensure named provider exists and env var is set |
| Child stuck `running` | Adapter hang or timeout | `status` events; `cancel` with reason; inspect artifact stderr |
| `message` / steering refused | By design | Create `relationship=continues` follow-up task |
| MCP install verification failed | Wrong `recollect-mcp` path or blocking doctor finding | `which recollect-mcp`; `doctor --json`; use `mcp print` first |

## Related documents

- [getting-started.md](getting-started.md) — install details and walkthrough
- [user-flows.md](user-flows.md) — CLI vs MCP flows and runtime matrix
- [cli.md](cli.md) — full command reference
- [mcp.md](mcp.md) — MCP tools and host configuration
- [config/providers.example.yaml](../config/providers.example.yaml) — annotated provider config
