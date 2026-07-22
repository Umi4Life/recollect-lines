# Recollect Lines documentation

Recollect Lines is a **local-first delegation broker**: a parent agent (or human operator) delegates bounded work to an **existing** AI coding runtime and receives an attributable, evidence-backed result. MCP and CLI are interfaces to the broker; Codex, Cursor, Claude Code, OpenCode, and HTTP providers are **runtime backends** — not hosts Recollect Lines replaces.

## Start here

| Document | Purpose |
|----------|---------|
| [operator-guide.md](operator-guide.md) | Product orientation: roles, boundaries, security, five-minute path |
| [getting-started.md](getting-started.md) | Install, clean-operator acceptance, mock quick start |
| [user-flows.md](user-flows.md) | Operator CLI, parent-agent MCP, and runtime-backend roles |
| [cli.md](cli.md) | `recollect-lines` commands and honest CLI limitations |
| [mcp.md](mcp.md) | `recollect-mcp` tools, schemas, host configuration |
| [demos/](demos/) | Recorded end-to-end proofs (fixture side-agent tree, live Codex, opt-in live dogfood runbook) |

## Design reference

| Document | Purpose |
|----------|---------|
| [design/PRD.md](design/PRD.md) | Canonical product requirements |
| [design/RFC-001.md](design/RFC-001.md) | Implementation architecture, evidence model, known limits |
| [design/RFC-002.md](design/RFC-002.md) | Completion integrity and capability-warning visibility (accepted) |
| [design/RFC-003.md](design/RFC-003.md) | Resource metadata and bounded rework/escalation governance (accepted) |
| [design/RFC-004.md](design/RFC-004.md) | Unified durable supervision for CLI runtimes (proposed) |
| [model-profile-resource-metadata.md](model-profile-resource-metadata.md) | Operator guide: model profiles and resource tiers |
| [cost-rework-policy.md](cost-rework-policy.md) | Operator guide: bounded rework and escalation policy |
| [review-report.md](review-report.md) | Operator guide: opt-in bounded-review result contract |
| [verified-investigation-report.md](verified-investigation-report.md) | Operator guide: strict investigation result contract |
| [claim-capability-diagnostics.md](claim-capability-diagnostics.md) | Operator guide: advisory claim-vs-capability diagnostics, trust boundary, recommended parent response |

## History (not the user guide)

| Location | Purpose |
|----------|---------|
| [history/phases/](history/phases/) | Per-phase implementation records and test evidence |
