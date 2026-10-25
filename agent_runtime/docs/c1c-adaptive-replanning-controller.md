# C1c — Adaptive Replanning / Plan Revision Controller

Status: **implementation complete; independent Standards/Spec review passed; product acceptance pending**.
Tracking Issue: [#18](https://github.com/9dianbiqi/learn-claude-code/issues/18),
under Epic [#13](https://github.com/9dianbiqi/learn-claude-code/issues/13).

Product line: `codex/long-horizon-agent-v1` at
`b1f0aefdaab9b07e4a62a83d40156700c8f5fe3b`, frozen by
`long-horizon-agent-v1-c1b-accepted-2026-09-21`.
C1c is a new, narrow phase between accepted C1b and the Epic's C3 automatic
decomposition/replan proposals. It is not an existing roadmap ticket.

## Goal and seam

Given a durable verifier or execution failure signal, decide whether to keep
the current Plan, retry within its existing budget, apply one typed PlanPatch,
or fail. The control path is:

```text
durable signal → verifier/failure interpretation → replan decision
→ optional typed PlanPatch → C1a/C1b persistence → C1b activation/resume
→ verifier
```

The proposed `RecoveryPlanRevisionController` owns decision eligibility,
idempotency and dispatch. Its small public interface accepts `task_id` and a
durable `signal_id`, and returns a structured decision result with current and
resulting revision IDs. A deterministic policy is injected at this seam so
fixed evaluation fixtures can exercise every decision. The policy proposes a
decision; the controller checks it against the authoritative PlanRevision,
PlanItem state and verifier capabilities before any mutation.

The controller does not execute tools, create verifier implementations,
rewrite checkpoints, select the next item, or create its own revision store.
It reuses the validation and persistence behind the public
`Runtime.apply_plan_patch(task_id, expected_revision_id, patch)` seam. A small
internal store helper may let the controller pair that existing patch operation
with its decision record in one transaction; the public seam and its C1a
semantics stay intact. `Runtime.resume(task_id)` retains responsibility for exact
current DAG/verifier configuration, atomic execution activation, evidence
recovery and frontier selection.

## Signal and decision contract

Initially accept only durable, task-owned signals: a non-authoritative
`verifier_runs` row (`fail` or `uncertain`), an existing PlanItem/task failure
event, or a recorded stale-evidence signal. An operator observation must first
be recorded with a stable ID and evidence reference. In-memory text alone is
not a replay-safe trigger. A signal is bound to the revision and PlanItem state
observed when the decision starts; stale signals cannot patch a newer Plan.

| Decision | Required behavior | PlanRevision created? |
|---|---|---|
| `KEEP` | Continue the current eligible execution path without changing structure. | No |
| `RETRY` | Retry the current eligible item through its existing retry path and remaining turn budget. A terminal failed task or exhausted item is ineligible; no budget reset. | No |
| `SPLIT` | Replace an active failed/retryable item with at least two approved drafts using C1a `SplitPlanItem`; preserve dependencies and historical tombstone. | Yes, one |
| `ADD_ITEM` | Add one approved pending `PlanItemDraft` via `AddPlanItem`. | Yes, one |
| `CHANGE_DEPENDENCY` | Change edges of one non-completed active item via `UpdatePlanItemDependencies`; retain acyclic, satisfied-edge semantics. | Yes, one |
| `TOMBSTONE_PENDING` | Tombstone one eligible pending item via `TombstonePlanItem`. | Yes, one |
| `FAIL` | Use the existing task/item failure transition, or report an already terminal state, with a reason and signal link. | No |

`KEEP`, `RETRY` and `FAIL` must never manufacture a no-op PlanPatch. An invalid
or redundant structural proposal is rejected without creating a revision.
`SPLIT` and `ADD_ITEM` require caller-supplied verifier configuration for every
new active item, including a bundle hash and executable verifier capability.
The controller must fail closed if the exact proposed DAG cannot be supplied
to C1b resume. It may not synthesize a verifier from a failure summary.

The decision policy in C1c is deterministic and explicit. It may consume a
curated set of approved patch drafts and decision rules; it does not ask a model
to invent subtasks or propose arbitrary graph rewrites. That belongs to C3.

## Durable and concurrency rules

1. Read the task-owned signal, current revision and PlanItem state under the
   existing repository lease. Reject missing, foreign, stale and duplicate
   signals. Preserve C1a completed-item and immutable-history invariants.
2. Identify each decision by `(task_id, signal_id, base_revision_id)` and
   persist its outcome. This key prevents a crash/restart from applying the
   same signal twice or inflating decision trace counts. The exact storage
   shape is an implementation choice; a minimal durable decision ledger is
   preferred if existing events cannot enforce uniqueness atomically.
3. For structural decisions, validate a typed patch and the proposed verifier
   capabilities before mutation. Use the expected-revision CAS in the existing
   patch seam. A duplicate after commit returns the previously accepted
   result; a different patch against a newer revision fails closed.
4. A crash before a decision/patch commit leaves the old decision and Plan
   state; a crash after commit exposes one complete decision and revision.
   Avoid a durable "accepted decision, missing revision" state. If this
   requires a small internal transaction seam in the store, preserve the
   public `Runtime.apply_plan_patch` behavior and existing C1a/C1b tests.
5. Activation is a separate, already durable C1b step. A crash after the
   patch and before activation leaves the new Plan pointer with the old
   execution binding; restart must validate the exact new configuration and
   activate it once before selecting work.
6. `KEEP`/`RETRY` preserve the current revision identity. Completed,
   verified work and tool/effect-ledger state remain authoritative; the
   controller must not reset `consumed_turns` or replay a completed side effect.
7. A rejected decision must retain the previous current Plan pointer,
   execution binding, PlanItem statuses and checkpoint. Record a bounded
   rejection reason for trace without hiding the original failure.

No new generic workflow engine, scheduler, model router or multi-agent runtime
is implied by these rules.

## Acceptance contract

Each line must have an executable test through the controller interface and
trace evidence; a green full suite alone does not satisfy the contract.

| ID | Acceptance behavior |
|---|---|
| C1c-01 | A persisted verifier `fail`/`uncertain` or eligible execution failure reaches one deterministic decision linked to its signal and base revision. |
| C1c-02 | `KEEP`, eligible `RETRY` and `FAIL` change no PlanRevision; duplicate signal delivery is idempotent. |
| C1c-03 | `SPLIT`, `ADD_ITEM`, `CHANGE_DEPENDENCY` and `TOMBSTONE_PENDING` each generate the expected C1a typed operation and exactly one immutable PlanRevision on acceptance. |
| C1c-04 | Missing verifier capability, stale expected revision, invalid graph, completed-item rewrite, ineligible tombstone, or budget-reset attempt fail closed with no partial Plan state. |
| C1c-05 | C1b activation resumes the latest accepted revision and selects the correct ready replacement/current item; old tombstones stay historical. |
| C1c-06 | Completed verified items are neither executed nor verified again because of a decision or restart; unresolved tool effects retain the existing reconciliation barrier. |
| C1c-07 | Crash before/after decision/patch commit and before/after C1b activation has old-or-new durable state; restart handles each signal once and produces a stable frontier. |
| C1c-08 | Existing frozen-DAG runs, C1a PlanPatch, C1b resume, schema migration and full repository tests remain green. |
| C1c-09 | A fixed evaluation suite covers all seven decision types, at least one invalid proposal, no-op avoidance and crash/restart. Trace exposes signal, decision, reason, base/result revision, outcome and rejection. |
| C1c-10 | Independent Standards and Spec reviews find no unresolved P0/P1; acceptance freezes an exact SHA and tag only after review. |

## Fixed evaluation and trace

Use deterministic `ScriptedModel` and fixed verifier results with disposable
repositories. Extend the existing `eval_runner` only as needed to run a fixed
controller scenario. Freeze fixture inputs and expected outcomes in a new
C1c suite; do not revise the existing MVP/Hardening suites to make a score
pass. Required cases: KEEP, RETRY, each of four typed structural decisions,
FAIL, invalid patch, missing verifier, repeated signal, and crash/restart
around decision and activation.

Record per-case status and the following trace dimensions: signal ID/type,
decision type and reason, base/result revision IDs, accepted/rejected/no-op
outcome, activation count, completed-item re-execution/reverification count,
effect duplication count, and recovery outcome. Aggregate at least decision
distribution, unnecessary-revision rate (must be zero for KEEP/RETRY/FAIL),
invalid-patch acceptance count (must be zero), recovery pass rate, and task
completion rate. Report measured results; do not promise an improvement over
C1b until fixed scenarios demonstrate one.

## Scope, branch and Issue plan

- Keep `codex/long-horizon-agent-v1` and its accepted C1b tag frozen as the
  product baseline. The existing clean `codex/c1c-implementation` worktree
  points exactly at `b1f0aef`; place this design and later implementation
  there, without editing the product worktree before acceptance.
- Create one C1c child Issue under Epic #13 with this contract, linked to
  completed #17. The Issue starts in **design review** state; do not mark it
  `ready-for-agent` until this design is approved. Add C1c between C1b and C3
  in the Epic's sequence. C3 retains model-driven automatic decomposition and
  replan proposals.
- Develop implementation on the C1c branch only after design review. Then
  run targeted and full tests, two independent review axes, integrate the
  accepted commit into the product branch and tag that SHA.

## Implementation plan after design approval

1. Define the typed `RecoverySignal`, `ReplanDecision` and result objects, plus
   one deterministic decision policy. Keep caller-visible configuration small.
2. Add a controller that reads durable signals and current revision, checks
   eligibility and verifier capabilities, and maps structural choices onto
   existing PlanPatch operations. Reuse C1a validation and CAS.
3. Implement the smallest durable decision identity/audit mechanism that can
   atomically pair an accepted structural decision with its revision and
   deduplicate restarts. Add a schema migration only if this cannot be met by
   existing tables and events; never treat trace-only records as Plan state.
4. Connect the controller at the post-verifier/failure seam and hand accepted
   structural decisions to C1b resume. Preserve current checkpoint/effect
   recovery and existing execution selector.
5. Add table-driven decision tests, invalid/stale cases, crash injection,
   fixed evaluation scenarios and trace fields. First run targeted C1a/C1b
   regression; then full repository tests.
6. Freeze a review SHA, run independent Standards and Spec reviews, fix only
   confirmed C1c blockers, and repeat fresh review after a fix.

## Non-goals

No model-generated task decomposition, open-ended patch proposal, learned
policy, automatic verifier implementation, graph-wide optimization, generic
`REORDER` operation, recovery diagnosis/router, workspace recovery, stuck
detection, model routing, human escalation, or redesign of C1b persistence.
`REORDER` is not a C1a typed operation and is therefore not silently added to
the first C1c increment. The controller may change dependencies only through
the accepted `UpdatePlanItemDependencies` operation.
