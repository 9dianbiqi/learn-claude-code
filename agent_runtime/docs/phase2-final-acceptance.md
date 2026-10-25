# Phase 2 Runtime Correctness: final acceptance

Submission date: 2026-10-10. Closure date: 2026-10-12.
Status: **ACCEPT WITH NOTES for the whole bounded Phase 2 contract**.
The submission and its evidence limitations below are retained as review history.
Current HEAD: `43c8127fb490bafd6c956fb03335f2588d6255c7` (sealed Increment 1).
Increment 2/3/4 accepted test/document additions remain uncommitted at submission.
No production changes follow Increment 1. No test or model call ran this round.

## Original objective and claim boundary

The user's Phase 2 roadmap asks for runtime correctness: state-machine invariants,
transactions/checkpoints, leases/fencing, safe replay and side effects, plan
revision atomicity, crash consistency, concurrent recovery, durable verification
obligations and external-state conflicts. It does not prescribe an exhaustive
interleaving proof or number of tests. The accepted Increment 1 contract expressly
limits guarantees to local SQLite/runtime operations, trusted Store primitives
and documented recovery behavior. Those limitations remain; this submission does
not silently weaken a previously promised guarantee or elevate a test result to
formal proof. The earlier v0.3 Background Job Phase 2 is a different roadmap.

The question for the final reviewer is whether the matrix below adequately
supports closing this bounded correctness phase, and whether its historical
evidence limitations or unexamined interactions are blocking. ACCEPT of individual
increments does not predetermine that judgment. If any required area is not
supported, identify the minimal missing evidence/repair and return REJECT.

## Evidence classes and provenance

- R: preserved raw execution evidence checked against exact source manifests and
  independently reviewed for the stated increment.
- H: historical execution/review reports in versioned documentation, with selected
  implementation/test function AST continuity; individual raw run logs not located.
- B: explicitly documented contract boundary, not a passing test or implementation.

Raw roots under `D:/opencv/learn-claude-code/.scratch/`:

| Increment | Directory | Execution and review |
|---|---|---|
| 1 | phase2-correctness-20261007 | 26 distinct scenarios passed at development steps, plus 6 repairs as 4+2; original full-scope REJECT retained, fresh narrow repair ACCEPT WITH NOTES |
| 2 | phase2-increment2-20261008 | 5 passed in 40.59s, first run; fresh ACCEPT WITH NOTES |
| 3 | phase2-increment3-20261010 | 2 passed in 10.17s, first run; fresh ACCEPT WITH NOTES |
| 4 | phase2-increment4-20261010 | 2 passed in 8.01s, first run; fresh ACCEPT WITH NOTES |

These are 41 distinct incremental passing scenarios, NOT a combined final-tree
test run. The initial 26 were not rerun after the last two Increment 1 fixes.
No claim is made that the old full suite passes on this exact final tree.

`phase2-final-review-20261010/preflight.json` records the non-executing check of
252 indexed evidence files across manifests (51+101+47+53), all accepted test
sources and the eleven Increment 1 production/test hashes. File counts are not
independent trials; source copies/helpers appear in multiple manifests. Reviews
and acceptance metadata remain in their original roots. Every original REJECT
and failing/intermediate log remains intact; failed steps are not relabeled.

## Requirement -> implementation -> evidence -> limitation

| Requirement | Implementation anchors | Evidence | What is supported / unresolved |
|---|---|---|---|
| State-machine transitions and scheduling | Runtime selector, pending guard; ContextProjector; Store.transition_review | R: I1 readiness cases across retryable/in_progress/verifying, blocked pending intent, owner persistence across approve/retry; I3 invalidation; I4 refresh | Selected critical transitions fail closed and preserve pending intent/budgets; not exhaustive arbitrary state corruption |
| Checkpoint/verified authority transaction atomicity | Store.transaction, commit_verified_subtask | R: I1 actual F2-pre/F2-mid/F3 exits | All-or-none checked authority bundle at those crash boundaries |
| Lease/fencing and concurrent recovery | Store.acquire_lease, guarded transaction, heartbeat/release; Runtime.resume | R: I2 simultaneous resume, stale response after takeover, live/dead reservation | Old owner cannot mutate examined authoritative tables or release new lease; live effect owner refuses takeover; controlled real-process schedules only |
| Safe replay and effect handling | PermissionEngine effect classification, operation/outbox/reservation, Runtime reconciliation | R: I1 opaque shell/legacy adoption; I2 actual append and file crash recovery | Opaque unknown requires review; matching file post state reconciles without rewrite; committed dedup and unknown are distinct; no generic exactly-once |
| File observation/conflict safety | ToolExecutor._read_snapshot, Runtime observation and file preconditions | R: I1 same-handle reads, mutation windows; I2 third-state drift | Model-observed content binds to its hash/identity; tested conflicts preserve external bytes; check+replace is not arbitrary-writer CAS |
| PlanRevision activation atomicity | Store.activate_plan_revision | H: C1b versioned execution/review report, unchanged selected function and real-exit test AST | Historical old/new exit behavior reported; individual raw logs and exact historical whole-environment identity absent; reviewer must decide sufficiency |
| Decision/patch atomicity and idempotency | replanning controller; Store._apply_plan_patch_conn and transaction | H: C1c versioned report, selected transaction/test AST identity | Historical atomic decision+revision cases reported; controller/caller/fixture entirety is not proven identical by selected ASTs; reviewer must decide sufficiency |
| Active verification obligation persistence | verified_evidence.recover; Store.invalidate_verified_subtask_evidence | R: I3 real before/after commit exits, A->B->C plus independent branch | A/B/C authority revoked atomically, required revalidation follows, independent work and consumed budgets retained |
| Benign evidence refresh atomicity | verified_evidence._revalidate; Store.refresh_verified_subtask | R: I4 before/after commit exits | One current valid authority per node; old history superseded, not erased; zero recovery model/tools; dependency and budgets retained |
| Structural vs semantic obligation preservation | PlanPatch revision history/bundle binding | B: I1 audit explicitly limits semantic claim | Split/tombstone is not proof that original natural-language requirements are conserved; semantic ledger is outside existing contract |
| Internal Store vs Runtime safety | Runtime checks plus trusted authoritative Store primitives | B, reviewed I1 | No promise that arbitrary direct untrusted Store calls enforce every Runtime precondition |

## Historical evidence gap (not concealed by new tests)

I4 `historical/provenance.json` and copied versioned documents record:
C1b `b1f0aefdaab9b07e4a62a83d40156700c8f5fe3b`, C1c
`b0aec3dbd99215edace3de1d58192a066f05ce9a`. Five selected AST comparisons matched.
Historical document copies derive from git show and are text-equal after newline
normalization, not byte-identical due to LF->CRLF. The targeted search did not
find individual raw logs in the original .scratch entries or C1b/C1c worktrees.
No exhaustive global search or test rerun was performed. If raw execution evidence
is necessary for final closure, this remains a blocker regardless of I1-I4 accepts.

## Existing review notes and remaining risks

- I1 scoped approval/retry tests establish shared guard refusal, not the subsequent
  scoped projection path. Legacy quarantine is atomic per record, not per batch.
- I2 uses injected expiry state, not real-time TTL validation; only specified
  process schedules/stale-model return are covered. An alive hung owner can block
  availability. External child processes surviving their runtime are not covered.
- I3's repair operation happened after its injected crash; it does not constitute
  an additional repair-effect replay crash test. Optional explicit operation-count
  assertion deferred; reviewer counted one in the raw database.
- I4 benign drift occurs before resume, not while final check/replace races an
  arbitrary writer. No filesystem/SQLite atomicity or host power-loss simulation.
- Semantic requirement conservation, distributed operation, universal cancellation
  and exhaustive interleavings remain outside the accepted contract.
- New tests and all raw evidence are local; tags/commits alone do not include the
  raw SQLite snapshots unless a separate evidence archive is retained.

## Proposed disposition

No new implementation or tests are proposed in this round. Independently judge
this matrix and actual underlying source/evidence. If final ACCEPT/ACCEPT WITH
NOTES is justified, record its exact scope before sealing test/document increments
and the final claim. If REJECT, preserve it verbatim and stop at the identified
minimal follow-up, without silently expanding or waiving requirements.

## Final independent decision and local seal - 2026-10-12

Fresh GPT-6.1 Sol (medium), `/root/phase2_final_review_recovered`, returned
**ACCEPT WITH NOTES for the whole bounded Phase 2 contract**, with no blocker
or substantive finding. Unmodified opinion: [phase2-final-review.md](phase2-final-review.md).
The earlier final review was interrupted without a saved final verdict; its
progress comments were not used as acceptance. This fresh review read actual
source, failure/pass logs, 252 indexed evidence files, 39 I2-I4 SQLite backups
and the six I1 repair databases. It did not rerun tests or invoke model/runtime.

Historical PlanRevision raw logs remain unavailable in the targeted search.
The reviewer judged this nonblocking based on historical execution/review reports,
actual transaction/call-chain inspection and source continuity. This judgment
closes the gate; it does not convert historical reports into raw execution proof.
All five notes in the original verdict remain part of the accepted claim.

Increment 2/3/4 are sealed separately; final tag:
`phase2-correctness-accepted-20261012`. No production or test source changed after
review. Only closure metadata, verbatim review and evidence indexing were added.
No test reruns, merges, pushes or new research phase were performed at closure.
See `phase2-final-evidence.json` for archive path/hash and evidence preservation.
