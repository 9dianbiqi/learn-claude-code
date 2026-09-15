# C1b — Durable Plan Revision Resume

Issue: [#17](https://github.com/9dianbiqi/learn-claude-code/issues/17)

Baseline: `60fd072b5d3509d851a3ea384f6a0aa04812f60e` (C1a accepted).
Development branch: `codex/c1b-durable-plan-revision-resume`.
Package: `0.3.0.dev10`; schema: v14.
Independent Standards/Spec review passed on 2026-09-15 with no C1b blockers.

## Investigation and gap analysis

C1a already appends immutable PlanRevision snapshots, updates the current
PlanItem projection, and CAS-switches `plans.current_revision_id` together with
`plan_revision_created` in one leased SQLite transaction. Dependency completion
satisfies edges without deleting them. Completed items cannot be structurally
rewritten; tombstones remain in historical snapshots and raw PlanItems.

C1a does not acknowledge revised execution. Its `Runtime.resume` compares the
caller DAG with the original `plans.dag_hash` and rejects failed tasks before
recovery. Thus an accepted split can leave durable replacement items that the
old resume path cannot execute. This is an explicit C1b gap, not a C1a defect.

Example: R1 contains completed A/B and pending C. A patch produces R2, then the
process exits. R2's correct caller configuration fails the old hash comparison.
In the budget-failure variant, C fails and is split into C1/C2; the task and Plan
also remain failed. C1b validates R2, acknowledges it atomically, and selects its
ready work without executing or verifying unchanged completed A/B again.

## Durable authorities and recovery path

| Information | Authority |
|---|---|
| Latest accepted structure | `plans.current_revision_id` and immutable `plan_revisions` snapshot |
| Acknowledged execution revision | `tasks.execution_plan_revision_id` |
| Current progress and budget | `plan_items.status`, `consumed_turns`, completion/evidence fields |
| Active versus historical items | Current snapshot and matching `plan_items.tombstoned` projection |
| Verified completion | Existing verifier runs, semantic checkpoints and lifecycle records |
| Messages and pending tool calls | `tasks.checkpoint_id` and full execution checkpoint |
| Effect replay/reconciliation | Existing tool calls, operations, reservations and outbox |
| Ownership | Repository lease and fencing token checked by store transactions |
| Frontier / next item | Derived by the existing deterministic selector from active items and dependencies |
| Events / trace | Audit only; not a substitute for any authority above |
| Verifier callables / projected model context | Caller-supplied process-local capability / derived context |

The call path is `Runtime.run` → task bootstrap → `create_plan` (including
revision 0 binding) → deterministic selection → model/tool execution → verified
completion/checkpoint. On restart, `Runtime.resume` → lease → current DAG
configuration validation → `activate_plan_revision` → existing evidence
recovery → model-checkpoint recovery → pending-call reconciliation → selector.

The restart caller supplies the full current `VerifiedSubtaskDAGConfig`; the
runtime does not generate a patch, construct verifier implementations, or infer
missing configuration. Hash and active-item validation cover ordered IDs,
dependency edges, verifier bundles and budgets, using the existing C1a hash
semantics. Descriptions are not newly added to that hash contract.

## Implementation boundaries

- `migrations.py`: v14 adds nullable execution binding. A task without a Plan
  remains unbound. Existing tasks bind revision 0 of their latest Plan, even
  when later accepted revisions exist. Migration cannot implicitly acknowledge
  those revisions. Explicit migration, backup, checksums and rollback remain.
- `store.py`: new Plans bind revision 0 in the creation transaction. Patch
  behavior is unchanged. Activation rechecks the expected current revision,
  DAG hash, projection and binding invariants under the transaction's lease
  guard, then CAS-updates the task and appends `plan_revision_activated`.
- `runtime.py`: current configuration is validated while holding the lease,
  before activation or verifier/model execution. Existing selector, evidence
  recovery, scoped recovery and tool ledger are reused without new algorithms.
- `trace.py`: summary exposes both revision pointers and activation count;
  existing event/task export includes activation audit and the execution field.

An already acknowledged revision is an activation no-op. A failed task reopens
only with a failed Plan, a valid failed checkpoint, no active failed items, and
ready work with remaining budget. Exhausted items receive no budget reset.
Split replacements have their own new budgets. Reopening appends an
`input_ready` checkpoint with the same messages and turn, dropping the obsolete
active-item cursor; it never rewrites the failed checkpoint. Task state, Plan
state, checkpoint pointer, execution binding and audit commit together.

Completed and aborted tasks are not reopened by activation. Unpatched failed
tasks retain terminal behavior. Completed evidence still undergoes the existing
staleness check; C1b does not disable baseline evidence invalidation when files
actually change. Non-DAG Plan revision execution is outside this increment.

## Crash boundaries

| Boundary | Observable recovery state |
|---|---|
| Before revision persistence / during replacement transaction | C1a rollback retains the complete old revision and projection |
| After patch commit, before execution activation | New Plan pointer; old execution binding; exact current configuration required |
| During activation before commit | Old binding and old task/Plan/checkpoint state, no activation event |
| After activation commit, before next item | New binding and complete reopened state; repeat activation adds nothing |
| After ordinary checkpoint, before next item | Existing checkpoint recovery and deterministic selection |
| After tool side effect, before result/checkpoint | Existing effect reconciliation or durable deduplication; no cleared ledger |
| Another crash during resumed execution | Persisted item completion and binding remain authoritative on the next restart |

`plan_revision_activation_before_commit` and
`plan_revision_activation_after_commit` support exception and hard process-exit
tests. No checkpoint is appended for ordinary active-task activation, preserving
the timestamp boundary used by durable model-response recovery.

## Requirement → implementation → test matrix

Test names below are in `tests/test_v14_plan_revision_resume.py` unless stated.
PASS records implementation self-check, not independent acceptance.

| Requirement | Implementation | Test | Status |
|---|---|---|---|
| v14 binding and backfill | `_apply_v14_plan_revision_resume` | `test_v13_migration_backfills_revision_zero_and_preserves_pending_activation` | PASS |
| New Plan binds revision 0; patch advances Plan only | `create_plan`; unchanged `apply_plan_patch` | `_failed_revision` setup assertions; migration unpatched/no-Plan cases | PASS |
| Unpatched frozen-DAG behavior unchanged | Existing recovery/selector retained | `test_v11_frozen_dag.py`, `test_v12_stale_evidence.py`, `test_v13_scoped_resume.py` | PASS |
| Exact current DAG/verifier required | `_assert_dag_config`, transaction recheck | `test_revised_resume_rejects_inexact_configuration_without_execution` | PASS |
| Resume replacements/current ready item | `activate_plan_revision`, existing selector | `test_failed_split_resume_preserves_completed_work_and_immutable_history`; `test_active_patch_resume_ignores_old_completion_marker_and_tombstoned_pending` | PASS |
| No completed replay/reverification | Existing evidence/selector/marker guards | Same tests assert exact model/verifier counts and unchanged evidence/history | PASS |
| Idempotent atomic activation | Lease-guarded transaction, CAS and audit | `test_activation_crash_is_old_or_new_and_repeated_restart_is_idempotent`; `test_activation_process_exit_reopens_complete_old_or_new_state` | PASS |
| Preserve budgets and fencing | Eligibility guard; transaction lease check | `test_unrelated_patch_does_not_reset_exhausted_active_item`; `test_stale_activation_and_lost_lease_cannot_advance_execution` | PASS |
| Preserve effect safety | Existing effect ledger path | `test_revised_resume_preserves_tool_effect_reconciliation` | PASS |
| Invariants and trace | Execution binding ownership/audit scanner; trace summary | `test_execution_binding_invariants_fail_closed`; split-resume trace assertions | PASS |
| Independent review has no unresolved P0/P1 | Separate Luna Max reviewers | Standards and Spec reports, 2026-09-15 | PASS |

## Explicit non-goals

No C1c or later ticket; no automatic decomposition/patch proposal; no new
verifiers; no multi-agent architecture; no Planner or DAG redesign; no recovery
router, workspace recovery, stuck detection, model routing, or human escalation;
no unrelated provider/search/context refactor or cosmetic cleanup. C1a semantics
and accepted history remain unchanged.

## Validation evidence (2026-09-14)

Commands ran from the C1b worktree. Before implementation, the C1a/frozen-DAG/
stale-evidence/scoped-resume subset passed 79 tests.

Final targeted command:

```text
python -m pytest agent_runtime/tests/test_v14_plan_revision_resume.py agent_runtime/tests/test_v13_plan_revisions.py agent_runtime/tests/test_v13_scoped_resume.py agent_runtime/tests/test_v12_stale_evidence.py agent_runtime/tests/test_v11_frozen_dag.py agent_runtime/tests/test_v10_verified_subtask.py agent_runtime/tests/test_v02_phase1_migrations.py agent_runtime/tests/test_stage2_recovery.py -q --tb=short
```

Result: **161 passed, 0 failed, 0 skipped**.

New C1b tests separately:

```text
python -m pytest agent_runtime/tests/test_v14_plan_revision_resume.py -q --tb=short
```

Result: **22 passed, 0 failed, 0 skipped**.

Full repository:

```text
python -m pytest -q --tb=short
```

Result: **368 passed, 0 failed, 2 skipped**, compared with the accepted baseline
of 346 passed, 2 skipped. Existing tests were retained; schema assertions were
updated to the exact v14 migration sequence, checksum, count and future-version
boundary. No test was deleted, skipped or weakened to obtain a passing result.

`git diff --check` passed. Scope inspection includes tracked changes relative to
`60fd072` and the two new files (this document and the C1b tests). No implementation
commit was created at the implementation handoff: starting and handoff HEAD were
`60fd072b5d3509d851a3ea384f6a0aa04812f60e`; changes were in the working tree.

Remaining concerns: none identified during implementation self-check.
The subsequent independent review is recorded below.

## Independent review (2026-09-15)

Two fresh Luna Max reviewers used independent contexts and reviewed the full
uncommitted implementation against `60fd072`, including both new files.
Reviewed file-manifest SHA-256:
`0e5a351fa80cd89ce9261e54e84d0fceb1b484e2f17aeb9a0a4eaeeabe011595`.

- Standards: PASS, zero findings; reviewer independently passed 122 targeted tests.
- Spec: PASS, all Issue #17 requirements covered; no C1b P0/P1 blockers.
- Parent full-suite verification on the same snapshot: 368 passed, 2 skipped.
- SPEC-01: non-blocking P2 observation about concurrent migration metadata reads.
  A reader can combine an old table inventory with newly committed schema
  metadata and report missing tables. The parent deterministically reproduced
  this on both baseline v13 and current v14; both final databases remained
  intact. This is a pre-existing issue, not a C1b regression, and remains unfixed.

**INDEPENDENT STANDARDS/SPEC REVIEW PASSED — READY FOR COMMIT**

Do not close Issue #17, create an accepted tag, or start the next ticket on the
basis of this report alone. Product integration and accepted tagging are separate actions.
