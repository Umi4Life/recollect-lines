# Claim-versus-capability diagnostics

Advisory guardrail that flags when a runtime's final summary plausibly
claims an external verification path that structured runtime metadata says
was denied — e.g. a summary saying "verified via GitHub" while the runtime's
own permission-denial metadata shows a `mcp__github__*` tool was denied for
that task.

This is **not** semantic fact-checking. It does not judge whether the
underlying claim is true, does not read model or provider names, and never
touches lifecycle state, `capability_contract` status, evidence provenance,
review status, or cost policy.

## Trust boundary

A diagnostic is advisory evidence of a *possible* mismatch between what a
summary says and what the runtime's own structured denials record — nothing
more:

- It is **not proof of deception**. The runtime may have verified the same
  fact through a different, non-denied path (a different tool, cached
  knowledge, a prior turn) and the wording only coincidentally resembles the
  cue vocabulary.
- It is **not proof of an external-world falsehood**. The broker never
  contacts the claimed provider to check.
- It **never fails, downgrades, or blocks a task**, and never changes
  `state`, `parser.contract_status`, `capability_contract`, evidence
  provenance, `review_summary`, or `cost_policy_status`. Diagnostics are
  computed after all of those and read none of them back.

## Recommended parent response

A parent that observes `has_claim_capability_diagnostic: true` should:

1. **Inspect the compact evidence already present** — `capability_warning`
   / `capability_observations` (which tools were actually denied) and, if
   declared, `capability_contract` (which required capabilities are
   unsatisfied). The diagnostic's `tool_family` and
   `denied_tool_identifiers` point at exactly the structured evidence that
   triggered it.
2. **Check task policy** for whether this class of task requires escalation
   (e.g. `verification_policy`, an operator runbook, or a review task with
   `result_schema: review-report`). The diagnostic does not decide this for
   you.
3. **Escalate only when policy requires it** — for example, by requesting a
   `review-report` task against the summary and the denial evidence, or by
   asking the worker to re-run with the needed tool access granted. The
   broker does not do this automatically.

## What triggers a diagnostic

Both of these must hold; either one alone never produces a diagnostic:

1. The runtime's final summary contains a conservative, broker-curated
   verification-claim cue: `verified via`, `checked via`, or `queried`.
2. A denied tool's family label — read structurally off an already-denied
   `mcp__<server>__<tool>` identifier's server segment, never guessed from a
   model/provider name — appears within a small bounded character window of
   that cue in the same summary.

Multiple denials of the same family, or repeated cue/family pairs, aggregate
deterministically into one diagnostic entry per distinct (cue, family) pair.

## Compact shape

```json
{
  "diagnostic_count": 1,
  "diagnostics": [
    {
      "category": "possible_claim_capability_mismatch",
      "cue": "verified via",
      "tool_family": "github",
      "denied_tool_identifiers": ["mcp__github__search_issues"]
    }
  ]
}
```

Every field is bounded and privacy-safe: `cue` is one of the three fixed
literal strings, `tool_family` is a structural label already implied by an
existing denied tool identifier, and `denied_tool_identifiers` reuses
identifiers already exposed via `capability_warning`. No prompt text, tool
arguments, source paths, credentials, or raw runtime output ever appear in a
diagnostic entry — including the surrounding summary text the cue/family
were matched in.

This shape (and the `has_claim_capability_diagnostic` flag) is surfaced in
`concise_normalized_view`, `status`, `collect`'s `normalized_summary`, and
completion events, alongside — never inside — `capability_warning` and
`capability_contract`.

## Non-goals

- No claim-truth scoring, LLM-as-judge, or semantic rewrite of the summary.
- No fuzzy model-name/provider-name guessing, broad NLP, or external lookup;
  the family label is read structurally off a structured tool identifier.
- No generic keyword scan divorced from structured denial context: a cue
  with no matching denied family, or a denial with no matching cue, never
  produces a diagnostic.
- No automatic rework, review dispatch, task-state mutation, or
  capability/tool/credential change. Escalation is always an explicit,
  policy-driven decision made outside this module.

## Combining with other contracts

Independent of `capability_contract` (required-capability status),
`verified_investigation_summary`, `review_summary`, and `cost_policy_status`
— all are computed and projected separately and never rewrite one another.
A worker can report `verified-investigation-report` or `review-report`
output and still carry a `claim_capability_diagnostics` entry alongside it.
