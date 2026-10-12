# Phase 2 Increment 3: evidence invalidation crash consistency

Date: 2026-10-10. Status: ACCEPT WITH NOTES for Increment 3 only; locally sealed 2026-10-12.
Baseline HEAD remains `43c8127fb490bafd6c956fb03335f2588d6255c7`.
Increment 2's accepted uncommitted test/document were retained unchanged. Its
source, raw acceptance and all 101 evidence hashes were checked before this work.
No older tests or model evaluations were repeated. No commit, merge or push.

## Selected gap and unchanged contract

The remaining-work inventory calls for active verification obligation persistence
and a requirement-to-evidence map. Existing V12 tests cover normal evidence
invalidation and revalidation; V14/V15 already contain real process exits for
revision activation and controller decisions. This increment adds only the
missing hard-exit boundary for destructive evidence invalidation, not a new
semantic obligation ledger, alternate scheduler or recovery policy.

Public seam: Runtime.run/resume with a frozen DAG A -> B -> C and an independent
node. The verifier reads real file bytes and passes only GOOD. After initial
completion, A is externally changed to BAD. A child Runtime.resume revalidates
A, then follows the existing invalidate_verified_subtask_evidence transaction.

Test-only wrappers arm the transaction boundary only while that method runs.
The wrapper delegates to the original transaction manager and all production
SQL. Before-commit exits occur after the entire body and before original commit;
the recorded connection is still in_transaction and shows all pending changes.
After-commit exits occur after the original manager returns and before the
invalidation method can return to recovery. os._exit(86) avoids normal rollback
or lease cleanup. No production fault hooks or algorithms were changed.

## Results and evidence

`tests/test_phase2_evidence_crash.py`: two new parameterized cases, first run
**2 passed in 10.17s**, pytest exit 0. Each real child exit code is **86**.
No failed run occurred, so no defect fix was needed. Production changes: none.

```text
C:\Python311\python.exe -m pytest agent_runtime/tests/test_phase2_evidence_crash.py -q --tb=short
```

PHASE2_I3_EVIDENCE pointed to a new `run01/cases` directory under
`D:/opencv/learn-claude-code/.scratch/phase2-increment3-20261010/`.
There are raw stdout/stderr, exit codes, verifier PID/status logs, boundary JSON,
SQLite backups, 12-table dumps, file hashes, recovered results and source copies.
`manifest.json` binds the tested source and 47 raw evidence files.

| Invariant | Before-commit crash | After-commit crash |
|---|---|---|
| Complete invalidation transaction, never a partial durable projection | Uncommitted snapshot shows A retryable, B/C pending, task running; reopen retains old completed plan/items/lifecycle/verifier rows | Reopen shows A retryable, B/C pending, independent completed; task running and plan active |
| Original authority and immutable history distinguished | Original semantic checkpoint rows unchanged; no invalidation audit persisted | Same original rows unchanged; old A/B/C lifecycle stale, independent valid; one failure/audit bundle persisted |
| Required verification survives restart | Recovery detects BAD again, invalidates once durably, repairs A and verifies A/B/C | Recovery uses committed invalidation, repairs A and verifies A/B/C without another failure bundle |
| Unaffected branch and budgets preserved | Independent verifier total one pass; consumed turns A=3, B=2, C=2, independent=1 | Same |
| Completion cannot rely on old markers/authority | Four new scripted model calls; three new semantic checkpoints; old affected checkpoints stay stale; all current nodes complete | Same |
| Recovery creates one committed repair operation | Raw database confirms one operation; test checks its committed state and attempt_count=1; file GOOD | Same |

Store invariant scans pass for both final tasks. The lease expires through an
explicit test-only update after confirmed child death, then recovery obtains a
higher fencing token. This is not a real-time TTL test. Recovery uses a new
Runtime in the parent process, distinct from the crashed worker.

## Phase 2 requirement-to-evidence matrix (not final acceptance)

Evidence levels: accepted bounded evidence; current new execution pending review;
historical documented execution; source inspection only. They must not be merged
into a claim that the whole final tree passed a new full suite.

| Requirement | Implementation / evidence anchor | Status and remaining boundary |
|---|---|---|
| Opaque effect safe replay and legacy quarantine | permissions classification; Runtime acquire/reconcile; Increment 1 original REJECT + repair ACCEPT; Increment 2 live/dead owner case | Accepted within specified shapes; no generic exactly-once or surviving child cancellation |
| Read observations and external-state conflict | tools._read_snapshot, Runtime read observation; Increment 1 read races; Increment 2 file drift case | Accepted bounds; no arbitrary external-writer CAS |
| Dependency scheduling and pending owner | Runtime selector/pending guard; projector readiness; Increment 1 26+6 incremental cases | Accepted bounded states; no automatic reconciliation of conflicting pending intent |
| Verified completion atomicity | store.commit_verified_subtask; Increment 1 F2-pre/F2-mid/F3 real exits | Accepted bounded transaction; host power loss not covered |
| Lease/fencing and concurrent resume | store.transaction/acquire_lease; Increment 2 five real-process cases and raw ACCEPT | Accepted controlled schedules, not all interleavings or hung-owner availability |
| Active verification obligations after external drift | verified_evidence.recover; store.invalidate_verified_subtask_evidence; these two process exits | Independently accepted bounded evidence: destructive transaction old/new consistency and mandatory revalidation of transitive dependents |
| Benign evidence refresh transaction | store.refresh_verified_subtask; V12 normal benign-refresh tests | Historical documented tests and source inspection; no new real-process refresh-boundary evidence; consider only if necessary for final contract |
| PlanRevision activation atomicity | store.activate_plan_revision; V14 test_activation_process_exit_reopens_complete_old_or_new_state; c1b-durable-plan-revision-resume.md | Historical documentation reports executed pass and prior review; not rerun. Original individual raw logs not located in this targeted inventory, so do not claim newly raw-verified evidence |
| Replanning decision/patch atomicity | replanning controller transaction; V15 test_real_process_exit_preserves_atomic_decision_and_patch; c1c-implementation-review.md | Historical documented executed pass, prior review and source inspection; no new raw-log validation or full current-tree run |
| Semantic obligation conservation across split/tombstone | PlanPatch structural/history checks only | Explicitly outside existing guarantee; no semantic proof added |
| Trusted Store vs public Runtime boundary | Authoritative commit methods are internal primitives; all new scenarios enter via Runtime | Preserve boundary; independently safe public Store API is an enhancement |
| Final Phase 2 acceptance | Requirement matrix plus bounded reviews | NOT performed; historical evidence provenance and remaining accepted claim boundaries still require final independent review |

V12/V14/V15 test files and verified_evidence/replanning/plan_revisions source are
unchanged relative to the sealed Increment 1 baseline. This establishes source
continuity from that baseline only, not identity to every earlier historical
test run. No historical counts are attributed to the new run.

## Limits and next gate

Only destructive invalidation at the end of its transaction body and after commit
is exercised. These cases are not exhaustive SQL-statement failure injection,
concurrent PlanPatch/evidence races, benign refresh crash tests, power-loss tests,
or semantic conservation across revisions. ScriptedModel and deterministic
file verifiers make no claims about real model behavior. The fixture initially
completes four nodes as setup; it does not rerun the existing V12 test suite.

Before final Phase 2 acceptance, locate/reconcile historical revision transaction
evidence (or explicitly accept its evidence level), decide whether benign refresh
needs a new crash-boundary case, and obtain fresh independent review of the final
requirement matrix. This increment does not automatically start that work.

## Independent review

Fresh GPT-6.1 Sol (medium), `/root/phase2_increment3_review`, returned
**ACCEPT WITH NOTES**. No blocker or substantive finding. The reviewer checked
all 47 hashes, actual logs, SQLite/JSON agreement, original transaction boundary
code and post-recovery verification history without running tests or models.
Raw verdict: `review-increment3-accept.md` in the evidence directory.

The first note is addressed by the narrower repair-operation wording above:
the crash precedes repair, so this is not a repair-effect replay crash test.
The optional explicit operation-count assertion is deferred; the reviewer
confirmed the count directly in raw SQLite. The accepted test source is unchanged
and was not rerun. Only document status, wording and this review record changed
after review. This acceptance is not whole-Phase-2 acceptance.

## Local seal - 2026-10-12

Tag: `phase2-increment3-accepted-20261012`. Test source remains byte-identical
to its reviewed source manifest. Only document closure metadata changed. See
`phase2-final-acceptance.md` for the subsequent whole-phase bounded verdict.
