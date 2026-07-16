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
| `--providers-config` | — | JSON or YAML file for `openai_compatible` profile |

There is **no** `recollect` executable (removed in field-readiness work).

### Provider configuration resolution order

`--providers-config` is one of several ways to point at a provider configuration
file. Exactly one file is selected per process, highest precedence first:

1. `--providers-config PATH` (explicit CLI flag / constructor argument)
2. `RECOLLECT_CONFIG` environment variable (a path to a specific file)
3. Repo-local operator config: `./.recollect/config.yaml` (or `.yml`/`.json`), relative to the current working directory
4. User-level operator config: `~/.recollect/config.yaml` (or `.yml`/`.json`)
5. Legacy default discovery: `./providers.json`, for zero-flag backward compatibility with existing JSON setups

Tiers 1 and 2 are **configured** sources: if either is set but the file is
missing or fails to parse, the command fails with that path's error — it
never silently falls back to a lower-precedence tier. Tiers 3–5 are
**discovery**: they're skipped (not an error) when absent, but once a file is
found there, the same rule applies — a malformed file at that tier fails
rather than falling through to the next one.

Both JSON and YAML are accepted at every tier (detected by extension, or by
content when the extension is absent/ambiguous). YAML is parsed with a safe
loader only — no arbitrary Python object construction, tags, or code
execution. Existing JSON configuration files continue to work unmodified;
`doctor` reports a non-blocking `PROVIDERS_CONFIG_LEGACY_JSON_FORMAT` info
finding when the resolved file is JSON, noting that YAML is also supported.

### Schema, examples, and strict validation

- [`config/providers.example.yaml`](../config/providers.example.yaml) — an
  annotated, illustrative starting point (placeholder `api_key_env` names,
  no real credentials).
- [`config/providers.schema.json`](../config/providers.schema.json) — a JSON
  Schema describing the contract (for editor/IDE integration; the
  authoritative validator is `recollect_lines.providers.validate_providers_document`).

Both the top-level document and each provider entry reject unknown keys —
a typo or a field that looks like a literal credential (`api_key`, `token`,
`secret`, `password`, `authorization`, …) fails fast with an actionable
error rather than being silently ignored. `ca_bundle` must be a filesystem
path, not inline certificate/key content. Credentials are always referenced
by `api_key_env` (an environment-variable *name*); no config field ever
holds a credential value.

Local/operator config files (`.recollect/config.{yaml,yml,json}` and the
legacy repo-root `providers.json`) are gitignored so they can't be
accidentally committed; the tracked example and fixture files under
`config/` and `examples/` are explicitly unaffected.

### init

`init` is the one-shot bootstrap for a fresh operator: it creates the
`--home` directory (default `./.recollect`) and a starter provider config
only if either is absent, then runs the same checks as `config validate` so
the reported status is truthful — it never claims a provider is configured
unless its file actually validates.

| Subcommand | Purpose |
|------------|---------|
| `init` | Create `--home` + a starter operator config if absent (mode `0600`/`0700` on POSIX), then validate; `--force` to deliberately overwrite an existing config |

```bash
recollect-lines init               # first run: creates ./.recollect/{,config.yaml}
recollect-lines init                # second run: idempotent no-op, reports "preserved"
recollect-lines init --force        # deliberately overwrite an existing operator config
recollect-lines init --json         # machine-readable report (home/config actions, diagnostics, next steps)
```

Idempotent and overwrite-safe: a config file already present under `--home`
(`config.yaml`, `.yml`, or `.json`) is left untouched unless `--force` is
passed. On non-POSIX platforms (no `os.chmod` semantics), the directory and
file are still created but the requested mode may not be enforced by the
OS — treat the generated config as sensitive regardless of platform.

`init` only establishes local state/config; it does not add or test a real
provider (planned for a later PR) or install an MCP host (also a later PR).

### config validate / config init

`config init` is the narrower primitive `init` (above) uses internally to
write the starter file; use it directly when you want a config file at a
custom `--path` without touching `--home` or running full diagnostics.

| Subcommand | Purpose |
|------------|---------|
| `config validate` | Validate the resolved provider configuration; reports source + result, secrets redacted (values never printed) |
| `config init` | Write a minimal starter config (default `./.recollect/config.yaml`); non-interactive, no real secrets, mode `0600` on POSIX |

```bash
recollect-lines config init                       # writes ./.recollect/config.yaml
recollect-lines config init --path my.yaml --force # overwrite an existing file
recollect-lines --providers-config my.yaml config validate --json
```

`config validate` is a focused subset of `doctor` (provider config only, no
CLI adapter probing); it shares the same `PROVIDERS_CONFIG_*` / `PROVIDER_*`
findings documented below.

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
| `children` | Direct child task summaries for a parent |
| `task-tree` | Bounded tree for a broker `root_task_id` |
| `completion-events` | Poll durable completion signals from the global event cursor |

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
--parent-task-id ID    (optional broker parent)
--external-root-id ID  (audit-only host grouping)
--relationship delegates|continues
--origin-kind host|side_agent   (default host; parent_task_id does not imply side_agent)
--origin-ref TEXT      (audit-only caller reference)
```

Runtimes and modes are validated against broker policy before queueing. Legacy `--profile` follows the same translation rules as MCP; see [migration-runtime-profile.md](migration-runtime-profile.md).

`root_task_id` and `delegation_depth` are broker-derived and cannot be supplied by callers. `external_root_id` groups host-side work without inventing a broker parent. Absent `origin_kind`, host-facing `create` defaults to `host` (including parented tasks); `side_agent` is reserved for a future explicit recursive callback path and is audit-only. Agent callback delegation remains unimplemented; host `create`/`delegate` calls are not conflated with it.

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
| `init` | One-shot local state/config bootstrap for a fresh operator — see [above](#init) |
| `doctor` | Offline diagnostics (`--json`, optional `--workspace`) |
| `config validate` / `config init` | Provider config validation (redacted) / local file generation — see [above](#config-validate--config-init) |
| `certify` | Integration certification (`--profile` required; dry-run default) |

`certify` live execution requires `--execute-live --i-accept-billed-remote-calls` (HTTP providers). CLI adapter certification uses fixture or live modes documented in [history/phases/phase-7b.md](history/phases/phase-7b.md).

### Provider configuration is a startup snapshot

The resolved provider configuration file (see [resolution order](#provider-configuration-resolution-order) above) is read **once**, when the broker/MCP process starts. Editing that file while a broker/MCP server is already running has no effect until you restart it. `doctor` reports the active snapshot as a `PROVIDER_CONFIG_LIFECYCLE` finding — the resolved source path (or `not_configured` if no file is in use), which precedence tier selected it (`source_origin`: `explicit`, `env`, `repo_local`, `user_level`, `legacy_default`, or `not_configured`), the UTC timestamp the running process loaded it, and a `restart_required_for_changes: true` flag with a reminder in `remediation`. The same data is available over MCP; see [mcp.md](mcp.md#provider-configuration-is-a-startup-snapshot).

## Help output (verified)

```text
usage: recollect-lines [-h] [--home HOME] ...
  {create,start,status,complete,collect,cancel,timeout,reconcile,control,verify,list,reconcile-all,discover,select,council,init,doctor,config,certify} ...
```

## Subprocess collection note

See [user-flows.md](user-flows.md#cli-limitation-subprocess-collection): one-shot `start` then a later `collect` in a new shell loses the process handle. Use MCP or an orchestration script for real runtimes.
