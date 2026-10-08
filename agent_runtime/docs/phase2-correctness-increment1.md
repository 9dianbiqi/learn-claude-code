# Phase 2 Runtime Correctness — increment 1

Date: 2026-10-08. Status: sealed with ACCEPT WITH NOTES for Increment 1 only; see the two-stage review scope below.

Phase 1 is frozen at `3b0e566` (runtime `ebb308e`). Its 2,032-file archive and
SHA-256 manifest are recorded in `experiments/PHASE1-FROZEN.md`. C remains the
research baseline, C-time is paused, receipt inactive. No model cohort was
expanded or repeated. This worktree is `codex/runtime-correctness-phase2`.

## Scope and independently identified defects

Two fresh GPT-6.1 Sol medium read-only entry audits returned REJECT in their
respective scopes. Their original conclusions are retained in the evidence
directory. Three concrete defects were reproduced without model APIs:

1. An approved shell with a read-looking prefix could be classified read_only,
   run an append, crash before persistence and automatically append again.
2. read_file returned one version but Runtime separately sampled a newer file
   hash/identity afterward, permitting overwrite of an unseen external change.
3. A legal PlanRevision could add a prerequisite to a retryable/in_progress/
   verifying item; scheduling kept selecting that blocked item instead of the
   ready prerequisite, consuming budget and failing the task.

The initial 9 regression cases failed before the changes and passed afterward.
Further new boundary cases exposed blocked pending-tool replay and inconsistent
projector selection; these were fixed in the same increment.

## Implemented contracts

### Shell execution and compatibility

Authorization and effect semantics are separate. All built-in shell execution
is opaque `unknown_write`, including apparently read-only commands: flags,
configuration, child programs and shell composition are not replay-safety proofs.
Existing permission allow/ask/deny policy remains authoritative for permission.
Committed results may be deduplicated; interrupted dispatched outcomes require
review. No generic shell exactly-once guarantee is made.

Old running/needs_review shell records marked read_only without an operation
cannot be automatically retried. Their owner task atomically adopts an UNKNOWN
legacy effect reservation and reclassifies the tool record. It then enters
needs_review without a model call or second effect. Other tasks cannot start
while such untracked legacy records remain; multiple owner tasks may each enter
quarantine so the guard does not deadlock them. Existing explicit resolve-call
semantics handle the legacy reservation. Operator completion/retry is an external
judgment, not runtime proof that the historical command did or did not run.

This compatibility path handles the specified interrupted legacy record shape;
it is not a general repair of arbitrary corrupted databases or permission for
mixed-version workers to keep executing during an upgrade.

### Bound read observations

Built-in read_file returns a string-compatible internal result carrying the full
file observation from the same open handle as its captured bytes. Full content
is hashed even when displayed output has a line limit. Runtime persists that
observation, never a subsequent re-read under the assumption the model saw it.

file_state uses the same handle-based sampler. It compares fstat before/after
and the path's final metadata to reject detected mutation/replacement during
sampling. Hash-only paths stream chunks instead of retaining whole large files.
A later external change is caught by existing pre-write hash/identity checks.

This is not filesystem/SQLite atomicity and not a lock against arbitrary external
writers. A non-cooperating writer can still race the final check and os.replace;
the increment does not claim to prevent every such lost update or to freeze files
after a verifier returns. These need a separately defined locking/cooperation
contract, not a stronger wording of the existing hash checks.

### Ready scheduling and pending intent

Scheduling and context projection both exclude nodes with incomplete dependencies,
including formerly active/retryable/verifying nodes. An eligible prerequisite runs
first. An old verifying item postponed by revised dependencies returns to progress
when ready and must supply a fresh completion request, using its remaining budget.
No turn budget is reset. Exhausted budgets still fail rather than receive free work.

If an already-persisted pending tool batch belongs to a now-blocked node, Runtime
raises `PendingPlanConflict` (a StaleState subtype), records the reason and releases
the lease. It preserves the pending checkpoint and does not dispatch, discard or
reassign those tools. The caller must reconcile the plan/pending intent; this is a
safe refusal, not a claim that all revised batches can autonomously continue.

### Verified bundle process-exit boundary

New tests use real `os._exit(73)` at F2-pre, F2-mid and F3. F2 exits expose no
authoritative verifier/checkpoint bundle; resume re-verifies without another model
call. F3 exposes the complete committed state and resumes without re-verification.
These tests add evidence for the existing transaction implementation; no new
verified-bundle schema or transaction mechanism was introduced.

## Evidence ledger

Directory: `D:/opencv/learn-claude-code/.scratch/phase2-correctness-20261007/`.
The directory date is the start of this increment; work continued on 2026-10-08.

| Evidence | Result | Meaning |
|---|---|---|
| repro-before.log → repro-after.log | 9 fail → 9 pass | Three entry defects, full/scoped and limited/full read cases |
| boundaries-before.log | 3 pass, 2 fail | Hard shell exit and in-read mutations pass; new pending-batch gap reproduced |
| projection-before.log | 3 fail | Projector selected blocked former active node |
| pending-and-projection-after.log | 5 pass | Pending intent refusal plus projector readiness |
| updated-frontier-contract.log | 1 pass | Updated obsolete blocked-first expectation; ready prerequisite now completes |
| verified-hard-exit.log | 3 pass | Real F2-pre/F2-mid/F3 exits, all-or-none authority |
| legacy-shell-before.log → legacy-shell-after-fixed-status.log | 2 fail → 2 pass | Upgrade-path quarantine and cross-task gate |
| legacy-quarantine-atomic-final.log | 1 pass | Rollback before quarantine commit; explicit complete without replay |
| legacy-multiple-before.log → legacy-multiple-final.log | 1 fail → 1 pass | Multiple historical owners do not mutually prevent quarantine |
| legacy-guard-order-before.log → legacy-guard-order-after.log | 1 fail → 1 pass | Admission inspects legacy effects after the fenced lease is acquired |

This represents 26 distinct new/updated scenarios passing at their relevant
development steps, not a claim that the entire old suite or all 26 were rerun
after every edit. Earlier unsuccessful attempts remain in the directory. Existing
Phase 1 tests/model runs were not rerun. The single old frontier test was updated
because it encoded the blocked-first behavior being corrected, then run under its
new contract. No API-backed model calls were used.

## Remaining Phase 2 work and claim limits

- Broader adversarial lease/worker interleavings and long-running hung-owner
  availability have not been newly evaluated in this increment. Existing code
  fences expired DB commits and prevents takeover while a running reservation's
  process is alive; it does not stop all filesystem effects at the instant TTL ends.
- PlanPatch preserves structural/history integrity, not semantic conservation of
  every original requirement across split/tombstone. Define that contract before
  adding an obligation-preservation mechanism.
- Store's authoritative commit methods are internal trusted primitives; Runtime
  adds marker/config/dependency checks. Do not claim arbitrary direct Store calls
  have all Runtime-level guarantees.
- This is the first correctness increment, not final acceptance of all Phase 2
  guarantees. New fault-injection evaluation follows accepted new contracts; the
  Phase 1 C/C-time cost experiment stays closed.

## Narrow follow-up to fresh review REJECT

The first increment reviewer returned REJECT on two specific omissions. Its
unaltered verdict is `review-increment1-reject.md` in the evidence directory.
The follow-up changes only Runtime/Store handling of those cases:

- `transition_review` preserves the pending batch's active_subtask_id inside
  the same transaction. Ownerless DAG pending batches now fail closed.
- Lease acquisition quarantines all eligible legacy records owned by the task
  before a needs_review fast return can bypass adoption.

The new `test_phase2_review_transitions.py` has six targeted cases. Initial
evidence: `review-transitions-before.log`, six failures. After the production
fixes, `review-transitions-after.log` has four passes and two retry-fixture
failures: Windows `/b` was interpreted by the unchanged path policy as an
external path, so those cases did not reach review. Only that test command was
corrected; `review-retry-final.log` then records two passes, four deselected.
The four passing cases and the prior 26 scenarios were not rerun.

These six cases cover approval/retry in full and scoped modes, missing owner,
and an existing legacy review checkpoint alongside another legacy owner with
idempotent repeated resume. No additional runtime scope or Phase 1 evaluation
was added. The next fresh reviewer will only accept/reject this bounded repair
and its interaction with the previously reviewed increment.

## Narrow review outcome — 2026-10-08

A new fresh GPT-6.1 Sol medium reviewer returned **ACCEPT WITH NOTES** for the
two REJECT repairs only. Original opinion: `review-reject-fix-accept.md` in the
evidence directory; no tests or model calls were run by the reviewer.

The reviewer checked the six archived SQLite cases and the exact narrow diff.
Approval/retry retains the owner and refuses tools before unmet dependencies;
legacy review records are adopted before the fast return, with no duplicate
reservation or execution. `scoped=True` cases prove the shared pre-dispatch
guard, not the scoped projection branch after approve/resolve. Quarantine is
atomic per record, not atomic for the entire multi-record batch.

The older increment scope was not revalidated as a whole. This is not completion
of Phase 2. Work stops here per the user's bounded request; no next repair round
or Phase 1 evaluation is started automatically.

## Seal - 2026-10-08

The initial review checked the complete increment and rejected two omissions;
the subsequent fresh review accepted only their narrow repairs. This seal relies
on that composed review record, not a new whole-increment review or Phase 2
acceptance. All eleven production/test file hashes match their applicable review
manifests, and the final narrow patch matches the saved patch exactly. Only this
document's status/closure metadata changed during sealing. No tests or real model
runs were executed during sealing. Original REJECT records remain unchanged.

The accepted tag is `phase2-increment1-accepted-20261008`; it names a local,
unmerged commit. Evidence hashes and local paths are recorded in
`phase2-increment1-evidence.json`. Evidence remains local; the tag alone does not
contain the raw databases or logs. The remaining-work inventory is a separate
planning document and is not independently accepted implementation.
