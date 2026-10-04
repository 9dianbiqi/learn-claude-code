# C1c implementation handoff — Issue #18

Status: **implementation complete; independent Standards/Spec review passed; product acceptance pending**.
Baseline: accepted C1b commit `b1f0aefdaab9b07e4a62a83d40156700c8f5fe3b`.
Implementation branch: `codex/c1c-implementation`.

## Resulting modules and behavior

- `replanning.py`: typed durable signal, decision and outcome; deterministic
  policy seam; controller eligibility, verifier-capability check, typed
  PlanPatch dispatch, duplicate lookup and fail-closed rejection. A recorded
  observation carries evidence refs plus the observed revision and item version;
  a verifier failure must be that item's latest failed attempt.
- `store.py`: existing PlanPatch implementation is callable inside a
  caller-owned SQLite transaction. The public `apply_plan_patch` behavior and
  C1a fault hooks remain. The controller uses that same internal function to
  write a PlanRevision, decision row and audit event in one transaction.
- `migrations.py`: explicit, checksum-verified schema v15 with
  `replan_decisions` and a uniqueness key for `(task_id, signal_id,
  base_revision_id)`, plus a nullable observed PlanItem version on verifier
  runs. Fresh databases use v15; v14 databases require explicit `db-migrate`.
  Existing revision, execution pointer and checkpoints are not rewritten by
  this migration. Legacy unbound signals fail closed if explicitly presented.
  Migration inspection reads inventory and version from one SQLite snapshot,
  closing an inherited concurrent-reader race observed during C1c validation.
- `runtime.py`: optional deterministic policy at the post-verifier failure
  seam and budget-failure seam, plus an explicit entry point for other recorded
  failure/observation signals. Resume scans eligible undecided durable signals
  under the lease before terminal rejection or C1b selection. An accepted
  structural decision returns `replan_pending`; the caller
  supplies the proposed exact DAG/verifier configuration to C1b `resume`.
  `KEEP` and `RETRY` follow existing execution behavior, and `FAIL` uses an
  atomic terminal checkpoint transition.
- `trace.py` and `eval_runner.py`: decision ledger export and trace counters;
  a fixed C1c evaluation mode while existing suites retain their behavior.
- `evals/c1c-replanning.yaml`: frozen scenarios for all seven decisions,
  rejected proposals, duplicate delivery, commit/activation crash windows and
  completed-work preservation.

## Durable transaction contract

The controller acquires the existing repository lease and checks the durable
signal, current PlanRevision and PlanItem state in a `BEGIN IMMEDIATE`
transaction. It rejects stale or foreign signals before policy execution.
Structural proposals must carry exactly one matching C1a operation, a signal
evidence reference and the full proposed DAG with callable verifier
capabilities. C1a derivation validates item lifecycle and graph constraints.

The transaction then updates the PlanItem projection, appends the immutable
revision, CAS-switches the Plan pointer, inserts the decision row and appends
audit events. `replan_before_commit` rolls back all of these; after commit,
`replan_after_commit` exposes all of them. A repeated signal returns the row's
persisted outcome before attempting another patch. A structural decision does
not change `tasks.execution_plan_revision_id`; C1b `resume` validates and
activates that revision separately. Invalid proposals insert one rejected
decision and audit event without changing Plan or checkpoint state.

## Requirement mapping

| Contract | Evidence |
|---|---|
| C1c-01 durable verifier/failure signal | `test_verifier_signal_drives_patch_then_c1b_resume_without_repeating_work`; observation cases in the fixed suite |
| C1c-02 KEEP/RETRY/FAIL no revision | `test_non_structural_decisions_are_durable_without_revision`; exhausted/terminal RETRY tests |
| C1c-03 four typed structural decisions | `test_structural_decisions_commit_one_typed_revision_with_ledger` |
| C1c-04 invalid/stale/missing verifier rejection | Invalid patch, missing capability, stale/foreign signal, stale policy revision, completed signal and migration tests |
| C1c-05 C1b activation/frontier | Verifier-driven split/resume test; fixed suite's structural cases and activation crashes |
| C1c-06 completed work/effects not replayed | Fixed completed-preservation scenario; `test_replanning_preserves_committed_file_effect_across_restart` |
| C1c-07 crash/idempotency | Exception and hard-process-exit decision tests, activation crash cases in fixed suite, duplicate-delivery cases |
| C1c-08 existing behavior | C1a/C1b targeted regressions and full repository suite |
| C1c-09 fixed eval and trace | `evals/c1c-replanning.yaml`; trace summary/JSONL and ledger invariant tests |
| C1c-10 independent review | Both axes PASS on the final reviewed snapshot; product integration and accepted SHA/tag remain pending |

## Fixed evaluation result

The deterministic suite currently has 18 cases and covers all seven decision
types. It reports 18 passed, 6/6 recovery cases passed, completion rate 0.8333
(FAIL and rejected proposals intentionally do not complete), unnecessary
revision rate 0, invalid patch acceptance count 0, completed re-execution 0,
completed re-verification 0, one real file-effect attempt, and confirmed
duplicate side effects 0. Signal sources include 16 recorded observations,
one durable verifier failure and one PlanItem execution failure.

## Validation at handoff

- Before C1c changes: C1a/C1b revision/resume baseline, 38 passed.
- After the migration-read fix: targeted C1a/C1b/C1c and migration suite,
  **202 passed**. Its two concurrent migration cases passed five repeated
  runs. Earlier C1b-baseline migration races observed during this work were
  resolved by the consistent read snapshot.
- Final C1c controller tests, **41 passed**, including process exit and effect
  reconciliation.
- Final full repository suite after the migration-read fix: **410 passed,
  2 skipped**. The hard process-exit
  test also passed three repeated targeted runs (six subprocess cases).
- `git diff --check`: passed. The fixed evaluation suite was run again after
  the controller and trace changes, with the metrics above.

The first independent review identified one P1 stale-verifier-signal defect
and two P2 evidence/metric defects. The implementation now accepts only the
latest non-authoritative failure for a PlanItem, requires observation evidence
plus exact revision/item-state binding, and measures invalid proposal
acceptance from fixed negative cases. These fixes are included in the final
test results above. Fresh reviewers must validate them independently.

The next independent review found two additional P1 gaps: a failed task could
accept a structural patch that C1b could not activate, and an old PlanItem
failure event could drive a decision after a later attempt. The controller now
checks the proposed failed-task frontier before committing, and item failure
events carry their revision and post-failure item version. For a normal failure,
the one authorized `failed → retryable` transition remains eligible; later
state changes make that event stale. A legacy event without the binding fails
closed. Targeted tests cover rejected ADD_ITEM/CHANGE_DEPENDENCY/
TOMBSTONE_PENDING on a failed task, a successful failed-task SPLIT and C1b
resume, repeated same-revision failure events, and legacy-event rejection.
Fresh independent reviewers must validate the complete updated snapshot.

The third independent review identified a missing automatic controller hook
for budget exhaustion without a completion marker. `_fail_dag_budget` now
routes its persisted `plan_item_failed` event through the controller. A
terminal RETRY proposal remains rejected; an eligible SPLIT returns
`replan_pending` for C1b activation. Fixed evaluation cases now use real
verifier and execution failure signals and recover one committed file effect.

The fourth independent review found two more signal-to-controller gaps: a
verifier failure could become stale after another item attempt without a new
verifier run, and a crash after a failure signal commit but before controller
entry could leave the signal undecided. The verifier run now carries the
post-failure item version. Resume drains eligible undecided signals while
holding the lease, before C1b terminal/frontier logic. Dedicated injection
tests cover both interruption points. Legacy v14 failures lack that version;
they cannot drive a new decision, while completed C1b tasks still resume.

The fifth independent Spec review passed with one non-blocking P2: generic
`task_failed` signals did not carry revision/task-version identity. These
events now include both fields; legacy events without them fail closed.
Targeted tests verify current and stale task-failure signals. The final fixed
evaluation remains 18/18, with six recovery cases and one real file effect.

Another independent Standards review found that restart scanning omitted
`task_failed`, stale-evidence and recorded observation events. Resume now
scans all supported, state-bound signal sources and checks again after C1b
evidence recovery. Tests restart a new Runtime after each event type was
persisted but before its decision. The migration-read race surfaced again in
the larger regression suite, so inspection now pins table inventory and schema
rows in a single read transaction with a forced interleaving regression test.

## Final independent review (2026-10-04)

Two fresh reviewers independently inspected the complete implementation against
the accepted C1b baseline. Reviewed manifest SHA-256:
`60850ab58199583c6091050fec533ba19a6d09df823df42742e23599e87272bf`.

- Standards: PASS; no confirmed P0/P1/P2/P3 findings.
- Spec: PASS; C1c-01 through C1c-09 map to implementation and meaningful tests.
- Both reviews were read-only; neither repeated the parent-run full suite.
- Final verification evidence: 202 targeted passed, 410 passed / 2 skipped
  full repository, and 18/18 fixed evaluation cases.

Only documentation status and this review record changed after review;
runtime, migration, test and evaluation files retain their reviewed contents.

## Remaining scope and product gate

The controller uses a caller-supplied deterministic policy and approved
verifier capabilities. It does not generate tasks, synthesize verifiers, route
models, alter C1b activation or claim that these fixed tasks establish an
improvement in real long-horizon completion. The accepted C1b tag and product
branch remain unchanged. This handoff is not C1c acceptance.
