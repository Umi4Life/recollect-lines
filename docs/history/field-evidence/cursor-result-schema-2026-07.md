# Field evidence: Cursor result-schema preflight (July 2026)

## Context

Bounded live validation delegated read-only work to real Cursor Agent CLI children
(`cursor-agent --output-format json`) with an explicit `--model composer-2.5`
(non-fast) pin and strict `result_schema` contracts (including
`verified-investigation-report`).

## Observed behavior

- Child processes exited successfully (`exit_code=0`, broker execution success).
- The CLI JSON envelope carried human prose in `result`, not a schema-valid
  structured payload matching the requested contract.
- Recollect Lines correctly separated execution outcome from contract satisfaction
  (`contract_status: unsatisfied_fallback`).

## Product decision

Do **not** add JSON coercion, runtime output rewriting, or automatic downgrade to
`plain-summary`. Reject incompatible `runtime` + `result_schema` pairs at adapter
preflight (task create / delegate) with code `unsupported_result_schema`.

## Cursor adapter policy (current)

- **Supported:** `plain-summary` only (`result_schema_policy: plain_summary_only`).
- **Unsupported until explicitly validated and advertised:** all structured schemas,
  including `verified-investigation-report`, `evidence-report`, `review-findings`,
  `review-report`, and `implementation-report`.

This does not claim Cursor can never emit JSON — only that strict broker contracts
are not supported by the current Cursor adapter under tested operational conditions.

## Non-goals

- Provider-native structured output APIs.
- Post-hoc normalization fabricating structured fields.
- Model-name-based capability inference.
