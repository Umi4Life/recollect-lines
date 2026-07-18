# Model profile and resource metadata

Operator-configured **model profiles** bind an explicit runtime/model
configuration to durable cost and resource classification. This is the
foundation for bounded rework and escalation policy (RFC-003); see
[cost-rework-policy.md](cost-rework-policy.md). It does
not implement routing, budgets, or retries.

## Invariants

- The broker **never infers** `cost_class` or resource dimensions from graph
  role, provider name, runtime type, or model-name folklore.
- A zero-monetary-cost model can still be quota-, latency-, or
  local-compute-expensive — inspect all resource dimensions, not only
  `monetary_cost`.
- A premium child task remains premium regardless of the parent task's cost
  class or profile selection.
- Omitting `model_profile` is explicit **unconfigured** (`cost_class:
  unknown`), not a silent default.

## Configuration

Add `model_profiles` to the operator configuration file (same resolution order
as providers — see [cli.md](cli.md)):

```yaml
model_profiles:
  dev-mock-low:
    runtime: mock
    cost_class: low
    usage_bucket: dev-sandbox
    resources:
      monetary_cost: negligible
      quota_scarcity: none
      latency: low
      local_compute_occupancy: low
      context_cost: low

  gateway-standard:
    runtime: openai_compatible
    provider: local_gateway
    cost_class: standard
    usage_bucket: batch-inference
    resources:
      monetary_cost: moderate
      quota_scarcity: moderate
      latency: moderate
      local_compute_occupancy: low
      context_cost: high
```

### Fields

| Field | Meaning |
|-------|---------|
| `runtime` | Required execution backend binding |
| `provider` | Required when `runtime` is `openai_compatible`; forbidden otherwise |
| `model` | Optional pin to a specific effective model at launch |
| `cost_class` | Closed enum: `low`, `standard`, `premium`, `unknown` |
| `usage_bucket` | Bounded operator identifier (`^[a-z][a-z0-9_-]{0,62}$`) |
| `resources` | Five required tier dimensions (see below) |

### Resource dimensions

Each dimension uses a documented closed tier enum:

| Dimension | Allowed tiers |
|-----------|----------------|
| `monetary_cost` | `negligible`, `low`, `moderate`, `high`, `unknown` |
| `quota_scarcity` | `none`, `low`, `moderate`, `high`, `unknown` |
| `latency` | `negligible`, `low`, `moderate`, `high`, `unknown` |
| `local_compute_occupancy` | `negligible`, `low`, `moderate`, `high`, `unknown` |
| `context_cost` | `negligible`, `low`, `moderate`, `high`, `unknown` |

## Task selection

Pass an explicit profile id at delegate/create time:

```bash
recollect-lines create --task "..." --workspace "$PWD" --runtime mock \
  --model-profile dev-mock-low
```

Validation:

- Unknown profile ids → rejected at **create** (no task queued).
- Incompatible runtime/provider/effective-model binding → rejected at
  **start** preflight before adapter launch (no quota spent).

## Persistence and surfaces

At launch the broker snapshots resolution to `model_profile_resolution.json`
and exposes a privacy-safe projection (`model_profile_resource`) on:

- task status / tree summaries
- `normalized_result.json` → `broker_observed.model_profile_resource`
- `concise_normalized_view` / completion events

Projections include profile identity and resource classification only — never
provider credentials, pricing secrets, token counts, raw configuration values,
or internal environment data.
