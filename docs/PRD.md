# Product Requirements Document — Recollect Lines

Status: canonical, living document. This PRD defines the product contract.
It is provider/runtime neutral: no runtime, language, protocol, or storage
choice named here is a permanent requirement. Where the current
implementation makes a specific choice (OpenCode, Python, MCP over stdio,
SQLite), that is documented as an implementation decision in
[RFC-001](RFC-001.md), not as a product requirement.

## 1. Problem statement

An agent ("parent agent") working on a task often needs to delegate a
bounded, well-scoped piece of work — investigate a failing test, draft a
patch, summarize a subsystem — to a separate agent process, without losing
track of what happened, without polluting its own context with the
delegate's full transcript, and without risking the delegate's actions
corrupting the parent's own workspace.

Today that delegation is ad hoc: manual copy-paste, informally supervised
subprocesses, or trusting a sub-agent's self-report at face value. There is
no durable record of what was asked, what ran, what changed, or whether
claimed results are actually true.

## 2. Target users

- **Parent agents** — the primary caller. An LLM-driven coding agent (or an
  operator's tooling standing in for one) that wants to delegate bounded work
  and get back a concise, trustworthy result instead of a raw transcript.
- **Operators** — humans running or configuring the parent agent's
  environment, who need delegated work to be safe by default (no source
  workspace corruption, no secret leakage) and inspectable after the fact.

Out of scope as a user: end users of a downstream product. This is
infrastructure for agents and the people operating them, not a
consumer-facing tool.

## 3. Product / job-to-be-done

When a parent agent has a bounded task that doesn't need to happen in its
own context, it should be able to:

1. **Delegate** the task with a clear instruction, target workspace, and
   bounds (timeout, isolation mode) — and get back a stable handle, not a
   blocking call.
2. **Observe** the task's status and event history at any time without
   affecting it.
3. **Collect** a concise result: a summary plus evidence, not a raw
   transcript — and know whether that evidence is the delegate's own claim
   or something independently checked.
4. **Cancel** the task and know, factually, whether the underlying work
   actually stopped.
5. Optionally let the task **write to a workspace** without risking the
   parent's own checkout — isolated by default, never touching the source
   until the parent asks for the result.

This is "recollect the signal": the parent gets back durable, evidence-backed
results, not a firehose of agent output it has to babysit or re-parse.

## 4. Goals and MVP success criteria

Goals:

- A parent agent can delegate bounded work and later retrieve a truthful,
  evidence-backed result without polling a live process itself.
- Delegated work that writes files never corrupts or blocks the parent's own
  workspace.
- Every claim of "this passed" is traceable to either the delegate's own
  report or an independently broker-run check — and the two are never
  conflated.
- The system runs entirely on the operator's own machine, with no required
  external service.

Measurable MVP success criteria:

- **Durability**: a task's state, event history, and artifacts survive a
  restart of the broker process (verified by test, not just by design).
- **Truthful verification**: any success result distinguishes
  runtime-reported evidence from broker-verified evidence in its returned
  data, with no code path that upgrades the former into the latter.
- **Workspace safety**: for an isolated task, the source workspace's working
  tree is provably unchanged (byte-identical before/after) whether the task
  succeeds, fails, times out, or is cancelled.
- **Cancellation truthfulness**: cancelling a task reports whether the
  underlying work was actually confirmed stopped, not merely "a signal was
  sent."
- **Bounded execution**: every task has an enforced timeout; no task can run
  unbounded by default.
- **Reviewable interfaces**: both the parent-facing interface (currently
  MCP) and a local operator-facing interface (currently a CLI) expose the
  same lifecycle without duplicating policy logic.

## 5. Core user flows

### 5.1 Delegate

Parent agent submits a task description, a target workspace, an execution
mode (read-only or workspace-writable), and a bound (timeout). The system
validates the request against policy (allowed modes, timeout ceiling,
concurrency limits) and either accepts it (returning a stable task handle)
or rejects it with a specific, machine-readable reason. Delegation is
non-blocking: the caller gets a handle back immediately, not a completed
result.

### 5.2 Observe / status

At any point, the parent (or an operator) can retrieve a task's current
state, its full append-only event history, and its artifact manifest. This
never mutates the task and never requires the task to be finished.

### 5.3 Collect evidence

Once a task reaches a terminal or collectible state, the parent retrieves
its result: a concise summary plus structured evidence. The result always
distinguishes what the delegate itself reported from what was independently
checked by the system running real commands and observing their raw
output/exit codes. A result is never fabricated on the caller's behalf —
absence of real evidence is reported as absence, not papered over.

### 5.4 Cancel

The parent can request cancellation of a running task. The system attempts
to stop the underlying work, observes whether it actually stopped (not just
whether a stop signal was sent), and reports that observation as the
outcome. A task that could not be confirmed stopped is reported as such
rather than being marked cleanly cancelled.

### 5.5 Workspace-safe writable task

When a task is allowed to write to a workspace, the system isolates that
writing from the parent's actual source checkout by default — the source is
never mutated by a delegated task, regardless of how that task ends
(success, failure, timeout, cancellation). The parent can later retrieve
what changed (a diff/status artifact) without the isolated copy being
merged back automatically. Concurrent writable tasks against the same
source are serialized or rejected, never silently interleaved.

## 6. Functional requirements

- **Durable tasks and events**: every task has a stable identifier, a
  validated state machine, and an append-only event log, all surviving
  process restarts.
- **Artifacts**: task-scoped output (requests, results, logs, diffs,
  verification output) is stored under a task-specific location with an
  integrity manifest (at minimum, size and content hash per file).
- **Truthful verification**: the system must be able to distinguish, in its
  returned data, between a delegate's self-reported outcome and an outcome
  the system itself independently executed and observed. Neither is ever
  silently promoted into the other.
- **Worktree/workspace isolation**: a workspace-writable task must not be
  able to mutate the parent-visible source workspace directly; isolation
  must hold across every task outcome, including crashes and restarts of
  the broker itself, without leaking or double-allocating a writable
  isolation slot for the same source.
- **Interfaces**: the system must expose its lifecycle (delegate, status,
  collect, cancel) both to a parent agent programmatically and to a human
  operator locally. The specific protocol and CLI shape are implementation
  choices (RFC-001), not requirements — but duplication of policy logic
  across interfaces is disallowed; interfaces are thin front doors onto one
  lifecycle implementation.

## 7. Nonfunctional requirements

- **Local-first**: the system must be fully operable on a single operator
  machine, with no required external network service, hosted database, or
  third-party account. Any given runtime adapter may itself require network
  access (e.g. to reach a model provider) — that is a property of the
  adapter, not of the broker.
- **Evidence-first**: every user-visible claim about a task's outcome must
  be traceable to a stored artifact or event, not held only in an
  in-process variable that disappears on restart.
- **Secret and output hygiene**: the system must not read, store, log, or
  echo credentials/tokens on the caller's behalf, and must not expose any
  filesystem path or artifact outside a task's own declared workspace and
  artifact directory.
- **Bounded execution**: every task must have an enforced upper bound on
  runtime; there is no supported "run forever" mode.
- **No shell injection surface**: any command the system itself executes on
  the caller's behalf (e.g. verification commands) must be an explicit
  argument list, never a caller-supplied shell string.

## 8. Explicitly out of scope

The following are deliberate non-goals for the product as currently
scoped, not oversights:

- **Remote workers** — all delegated execution happens as a local process
  under the broker's direct supervision; no distributed/remote execution
  fabric.
- **Multi-user authorization** — single local operator/parent agent per
  broker instance; no user accounts, roles, or permission model.
- **Web UI** — no browser-based dashboard or control surface.
- **Daemon / HTTP service** — no long-running network listener; interfaces
  are local (stdio/CLI), invoked per-call.
- **Provider lock-in as a requirement** — the product must not be defined in
  terms of one specific model runtime, language runtime, or storage engine;
  today's choices are implementation, not contract (see RFC-001).
- **Live mid-task steering** — no requirement that a parent be able to send
  a delegate agent additional input after it has started; a delegate runs to
  completion, timeout, or cancellation.

## 9. Risks and open questions

- **Runtime process durability**: resolved for Phase 5B as "safely fail
  closed on restart," not durable re-attachment — a fresh broker instance
  reconciles a durable launch record against the process group's actual
  liveness, reaching a truthful `failed` when it's confirmed dead or an
  explicit `recovery_required` state when it isn't, never a fabricated
  success (see [RFC-001](RFC-001.md) §8 and [phase-5b.md](phase-5b.md)).
  Whether *transparent* re-attachment is ever required for MVP remains open;
  nothing in Phase 5B attempts it.
- **Verification is opt-in, not enforced**: nothing in the lifecycle
  currently requires a verification pass before a task can report success.
  Should the product require verification for a workspace-writable task to
  be trusted? (Tracked for Phase 5C.)
- **Single-adapter compatibility evidence**: current truthful-verification
  and cancellation evidence has been exercised thoroughly against one real
  runtime. Confidence in these product requirements generalizing to a
  second real runtime is not yet established.

## 10. Acceptance checklist

- [ ] A parent agent can delegate, observe, collect, and cancel a task using
      only the documented interfaces, with no direct access to internal
      storage.
- [ ] A workspace-writable task's source workspace is unchanged after every
      possible task outcome (success, failure, timeout, cancellation).
- [ ] A collected result clearly labels each piece of evidence as
      runtime-reported or broker-verified.
- [ ] A cancelled task's report reflects an observed outcome, not merely a
      sent signal.
- [ ] All of the above hold after a restart of the broker process between
      steps, for at least the durable (non-in-memory) parts of task state.
- [ ] No required external network service exists for the core lifecycle.

## 11. Terminology

- **Parent agent**: the caller delegating work; consumer of the product.
- **Broker**: the local component owning task lifecycle, persistence, and
  policy enforcement (implementation name; see RFC-001).
- **Adapter**: the component translating the broker's generic "run this
  task" into a specific runtime's invocation (e.g. a specific CLI).
- **Runtime-reported evidence**: a claim (e.g. "tests passed") made by the
  delegate/runtime itself, taken at face value and labeled as such — never
  silently treated as independently confirmed.
- **Broker-verified evidence**: a claim backed by a command the broker
  itself executed and whose raw stdout/stderr/exit code it directly
  observed. Only this evidence is labeled verified.
- **Workspace isolation**: running a writable task against a private copy
  of a source workspace such that the source is never mutated directly by
  the task.
