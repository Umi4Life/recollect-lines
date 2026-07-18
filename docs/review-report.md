# review-report result contract

Opt-in structured result schema for a task that **reviews** artifacts produced
by another task — a diff, a normalized result, a test/verification output —
and needs to return a structured, auditable review outcome without the
broker inferring what the reviewer did or automatically reproducing the
worker's entire task.

Default and legacy result schemas (`plain-summary`, `evidence-report`,
`review-findings`, `implementation-report`, `verified-investigation-report`)
are unchanged unless `review-report` is requested explicitly.

## When to use

| Schema | Use when |
|--------|----------|
| `review-findings` | Legacy/runtime-reported review output with loose findings |
| `review-report` | Parent needs a closed review-status vocabulary, bounded findings, explicit reviewed-artifact references, and an explicit full-re-execution flag |

## Required JSON shape

```json
{
  "summary": "diff addresses the reported race but adds no regression test",
  "review_status": "needs_changes",
  "review_findings": [
    {"finding": "no test covers the new lock ordering", "severity": "major"}
  ],
  "reviewed_artifacts": [
    {"category": "diff", "reference": "worker task tsk_abc123 diff"},
    {"category": "verification_output", "reference": "pytest -q summary, tsk_abc123"}
  ],
  "full_reexecution_performed": false
}
```

### `review_status` vocabulary

Closed labels: `passed`, `needs_changes`, `blocked`.

### `review_findings`

Bounded array (up to 50 items). Each item requires `finding` (non-empty
string, up to 2000 characters) and may include `severity`
(`blocking` | `major` | `minor` | `info`; defaults to `info`).

### `reviewed_artifacts`

Bounded array (up to 50 items) of what the reviewer actually looked at. Each
item requires `category` — one of `diff`, `test_result`, `normalized_result`,
`verification_output`, `task_summary`, `other` — and `reference`, a bounded
locator/label validated with the same safe-source rules as
[`verified-investigation-report`](verified-investigation-report.md#safe-source-handling):
no multi-line text, no raw JSON dumps, credential-shaped material scrubbed
before persistence. Full artifact contents, tool inputs, source paths, and
raw stdout do not belong in this field.

### `full_reexecution_performed`

Required boolean. Runtime-reported contract output — what the reviewer says
it did, not something the broker verifies. The default workflow posture is a
**bounded review of the supplied artifacts**, not a replay of the delegated
task, so this is normally `false`. Setting it `true` records that the
reviewer chose to fully re-execute the original work instead.

This field states a fact about what happened; it is never used to compute,
claim, or imply that cost was saved. The broker does not compare it against
the prior task's cost, model profile, or `cost_policy_status` — see
[Non-goals](#non-goals).

## Concise projection

`concise_normalized_view`, `status`, `collect`, and completion events include
`review_summary` when this schema is selected:

- `contract` (`review-report`) and `contract_status`
- `review_status`
- `finding_count`
- `reviewed_artifact_category_counts` (counts per category, not the
  references themselves)
- `full_reexecution_performed`

Raw finding text, artifact references, and reviewer prose are never injected
into concise, completion, or status paths.

## Combining with other contracts and policies

- **`verified-investigation-report`**: a worker can produce a
  `verified-investigation-report` and a separate reviewer task can produce a
  `review-report` about it. The two contracts are validated and projected
  independently (`verified_investigation_summary` vs `review_summary`); a
  review outcome never rewrites, merges into, or overrides the
  investigation's own findings, evidence, or provenance.
- **Bounded rework and escalation policy** ([docs](cost-rework-policy.md)):
  a `needs_changes` or `blocked` review outcome does not by itself trigger
  rework, escalation, or budget changes. A parent that wants a rework task
  after a review still passes explicit `cost_rework_policy`,
  `rework_prior_task_id`, `rework_scope`, and `escalation_reason` at task
  create time, exactly as documented there. `review_summary` and
  `cost_policy_status` are surfaced side by side and must not be conflated.
- **Capability contracts and execution/verification state**: `review_summary`
  is additive alongside `capability_contract`, `broker_observed.verification`,
  and `state`/`parse_status`/`contract_status`. A review finding never
  rewrites task lifecycle state, execution outcome, or capability contract
  status.

### Non-goals

- The broker does not autonomously dispatch a reviewer, schedule a review,
  or infer what a review should cover. Every review is an explicit task the
  operator or parent creates with `result_schema: review-report`, same as
  any other task.
- The broker does not decide, infer, or enforce whether a reviewer performs
  a full re-execution; it only records what the reviewer self-reports in
  `full_reexecution_performed`.
- This is not semantic fact-checking of review content, and it changes no
  tool access, capability grant, or credential.

## CLI / MCP

```bash
recollect-lines create --task "review the diff and tests from tsk_abc123" --workspace . \
  --result-schema review-report
```

MCP `delegate` accepts the same `result_schema` enum value.
