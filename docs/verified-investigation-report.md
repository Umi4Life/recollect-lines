# verified-investigation-report result contract

Opt-in structured result schema for investigation tasks that need explicit,
machine-validatable claims, evidence references, provenance labels, blocked
capabilities, and unverified claims — without the broker performing semantic
fact-checking of every statement.

## When to use

| Schema | Use when |
|--------|----------|
| `evidence-report` | Legacy/runtime-reported investigation output with optional loose findings |
| `verified-investigation-report` | Parent needs referential integrity, provenance discipline, and count-only summaries |

Default behavior and existing schemas are unchanged unless this schema is
requested explicitly (task field or profile default).

## Required JSON shape

```json
{
  "summary": "concise investigation outcome",
  "findings": [
    {
      "claim": "handler blocks on DNS lookup",
      "confidence": 0.85,
      "evidence_refs": ["ev-log"]
    }
  ],
  "evidence": [
    {
      "id": "ev-log",
      "provenance": "runtime_reported",
      "source_type": "log_file",
      "source": "logs/auth-service.log",
      "claim_supported": "repeated timeout entries"
    }
  ],
  "unverified_claims": ["external DNS latency spike"],
  "blocked_capabilities": ["repository.remote.read"]
}
```

### Provenance vocabulary

Closed labels:

- `orchestrator_supplied` — context the parent provided at launch
- `runtime_reported` — the worker observed during execution
- `broker_observed` — reserved for broker-assigned evidence (not valid in runtime JSON)
- `broker_verified` — reserved for broker-assigned evidence (not valid in runtime JSON)
- `unresolved` — claim context exists but origin is unknown

Runtime output may only self-label with `orchestrator_supplied`, `runtime_reported`,
or `unresolved`. Agent prose cannot silently become `broker_verified`.

### Safe `source` handling

`source` must be a bounded human-useful locator or label (file path, log name,
config key, section reference). The broker rejects multi-line text, raw JSON
dumps, and scrubs credential-shaped material before persistence. Full file
contents, raw stdout, request arguments, and external response bodies do not
belong in this field.

### `blocked_capabilities` vs broker capability observations

`blocked_capabilities` is **runtime-reported contract output**: what the worker
claims it could not use. Broker-normalized `capability_observations` from
structured permission denials are separate metadata surfaced via
`capability_warning` / `capability_contract`. Parents should treat these as
independent dimensions.

## Concise projection

`concise_normalized_view` and completion-event `result_summary` include
`verified_investigation_summary` when this schema is selected:

- contract name and `contract_status`
- counts of findings, evidence, unverified claims, blocked capabilities
- provenance counts

Raw evidence bodies are never injected into concise paths.

## CLI / MCP

```bash
recollect-lines create --task "..." --workspace . \
  --result-schema verified-investigation-report
```

MCP `delegate` accepts the same `result_schema` enum value.
