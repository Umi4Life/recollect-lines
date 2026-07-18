# Bounded rework and escalation policy

Operator-configured **cost/rework policies** cap premium work and require
explicit rework metadata. The broker never infers rework intent from task
text, graph position, provider name, or model prose.

## Anti-rework invariant

Every bounded rework or escalation requires all three:

1. **Explicit policy** — `cost_rework_policy` names an operator policy.
2. **Explicit reason** — `escalation_reason` when the policy requires it.
3. **Budgeted preflight** — caps are evaluated from persisted workflow history
   before adapter launch (no quota spent on rejection).

Omitting `cost_rework_policy` preserves legacy behavior.

## Configuration

Add `cost_rework_policies` to the operator configuration file:

```yaml
cost_rework_policies:
  strict-review:
    max_premium_tasks: 2
    max_premium_retries_per_task: 1
    max_escalations_per_workflow: 1
    allow_higher_cost_reexecution: false
    require_escalation_reason: true
```

| Field | Meaning |
|-------|---------|
| `max_premium_tasks` | Premium tasks allowed per workflow (`root_task_id` tree) |
| `max_premium_retries_per_task` | Premium rework attempts per `rework_prior_task_id` |
| `max_escalations_per_workflow` | Full re-executions (`rework_scope: full`) per workflow |
| `allow_higher_cost_reexecution` | Allow full rework at higher `cost_class` over a satisfied prior task (default `false`) |
| `require_escalation_reason` | Require non-empty `escalation_reason` for rework (default `true`) |

## Requesting targeted continuation vs full rework

Pass explicit rework metadata at create/delegate time:

```bash
# Targeted continuation on a prior task (counts premium budget; not an escalation)
recollect-lines create --task "Fix finding #2 only" --workspace "$PWD" \
  --runtime mock --model-profile dev-mock-low \
  --cost-rework-policy strict-review \
  --rework-prior-task-id tsk_abc123 \
  --rework-scope targeted \
  --escalation-reason "address review finding #2"

# Full re-execution (counts as escalation; may be denied if prior succeeded)
recollect-lines create --task "Re-run entire investigation" --workspace "$PWD" \
  --runtime mock --model-profile mock-premium-child \
  --cost-rework-policy strict-review \
  --rework-prior-task-id tsk_abc123 \
  --rework-scope full \
  --escalation-reason "prior output missed blocking issue"
```

| Field | Meaning |
|-------|---------|
| `cost_rework_policy` | Named policy id from operator config |
| `rework_prior_task_id` | Prior task in the same workflow root |
| `rework_scope` | `targeted` (continuation) or `full` (re-execution / escalation) |
| `escalation_reason` | Bounded operator reason (presence surfaced; text not echoed on all surfaces) |

`model_profile` must resolve to a configured `cost_class` when a policy is
selected. Premium budget is charged from the **child task's** profile, never
inherited from the parent.

## Preflight rejection

Invalid rework metadata, cross-workflow references, unknown profiles under an
opted-in policy, or exhausted budgets reject at **start** preflight with
machine-readable metadata and **no adapter launch**.

Full rework of a satisfied prior task at higher cost is denied unless
`allow_higher_cost_reexecution` is true and a reason is supplied.

## Surfaces

`cost_policy_status` appears on task summaries, normalized results,
`concise_normalized_view`, and completion events. Projections include policy
id, usage/remaining counters, rework scope, and `escalation_reason_present` —
never credentials, pricing, task prompts, or raw artifacts.
