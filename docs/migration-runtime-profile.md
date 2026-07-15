# Migration: runtime vs profile

## Summary

Recollect Lines previously overloaded `profile` as the execution-backend selector
(`codex`, `claude_code`, `cursor`, …). Side-agent semantics split that meaning:

| Field | Meaning |
|-------|---------|
| `runtime` | Execution backend identifier |
| `agent_profile` | Optional behavioral role identifier (persisted; not yet composed into prompts) |
| `model` | Optional requested model identifier (persisted; not yet passed to adapters) |
| `result_schema` | Optional requested result-contract identifier (persisted; not yet normalized) |

The SQLite `profile` column remains as a compatibility bridge (`profile = runtime`).

## Callers

### Preferred (new)

```json
{
  "task": "Trace cancellation path",
  "workspace": "/path/to/repo",
  "runtime": "codex",
  "agent_profile": "architecture-reviewer"
}
```

CLI equivalent:

```bash
recollect-lines create --task "..." --workspace "$PWD" --runtime codex --agent-profile architecture-reviewer
```

### Deprecated (legacy)

```json
{
  "task": "Trace cancellation path",
  "workspace": "/path/to/repo",
  "profile": "codex"
}
```

Rules:

- `profile` is accepted **only** when its value is a known runtime identifier.
- Unknown values (for example `architecture-reviewer`) are rejected with guidance to use `agent_profile` plus an explicit `runtime`.
- If both `runtime` and `profile` are supplied and differ, the request fails with a stable conflict error.
- If both agree, the request is accepted and carries deprecation metadata (see below).
- `agent_profile` is never inferred from legacy `profile`.

## Deprecation metadata

Translated legacy requests persist secret-safe metadata:

```json
{
  "compatibility": {
    "legacy_profile_translated": true,
    "deprecated_fields": ["profile"]
  }
}
```

This appears in `request.json` and in MCP `delegate` / `delegate_batch` summaries when translation occurred. New `runtime=` requests are not marked deprecated.

## Runtime registry (Phase 8.2)

Execution backends are registered centrally in `RuntimeRegistry` as immutable
`RuntimeDescriptor` records. Each descriptor declares:

| Field | Meaning |
|-------|---------|
| `execution_strategy` | `subprocess_cli`, `direct_api`, `synthetic`, or `fixture` |
| `model_selection` | Honest model behavior: `not_supported`, `persisted_not_invoked`, or `provider_config_default` |
| `adapter_capabilities` | Reuses `AdapterCapabilities` / recovery contracts from adapters |
| `policy` | Timeout, concurrency, and allowed execution modes |

CLI, MCP, certification, council validation, and discovery read runtime names
from the registry instead of separately maintained lists. Test-only fixture
runtimes can register into a copied registry without editing broker lifecycle
code or interface constants.

## Database

Additive migration on the existing `tasks` table:

- `runtime` — backfilled from historical `profile`
- `model`, `agent_profile`, `result_schema` — nullable, inert until later increments

No destructive schema replacement. Re-running migration is idempotent.
