# Phase 2 Increment 4: benign refresh crash boundary and evidence provenance

Date: 2026-10-10. Status: ACCEPT WITH NOTES for Increment 4 only; locally sealed 2026-10-12.
Baseline HEAD: `43c8127fb490bafd6c956fb03335f2588d6255c7`.
Increment 2/3 uncommitted files are preserved, not changed or rerun. No production
code changes, real model evaluation, commits, merges or pushes in this increment.

## New validation scope

Increment 3 covered destructive invalidation. These two new cases cover the
other transaction: `refresh_verified_subtask`, when changed evidence still
satisfies the frozen verifier. Public entry remains Runtime.run/resume.
Initial DAG: A -> B, plus independent. Files contain GOOD; A changes to GOOD
with a trailing newline, which changes the SHA-256 but passes the same frozen
trimmed-content verifier. All initial nodes were already completed.

A real child Runtime resumes and enters the refresh transaction. Test-only
wrappers arm around refresh only, delegate all SQL/lease checks/commit behavior
to the original transaction manager, and exit via `os._exit(87)` either after
the body but before commit, or after commit but before refresh returns.
Before-commit JSON records the uncommitted connection state. After-commit
captures a read-only database snapshot. Neither path unwinds exception cleanup.
After confirmed child exit, lease expiry is explicitly injected; a new Runtime
in the parent process resumes the task. No sleep-based timing is used.

The new test imports four unchanged serialization/snapshot/lifecycle helpers
from the Increment 3 test module. This imports definitions only; it does not
collect or execute the old test cases. Both source files are bound by the new
evidence manifest because the helper dependency is part of the executed source.

## Results and assertions

Command:

```text
C:\Python311\python.exe -m pytest agent_runtime/tests/test_phase2_refresh_crash.py -q --tb=short
```

Result: **2 passed in 8.01s**, pytest exit **0**, first run. Both intentional child
exit codes are **87**. No failed run or production defect was observed.

| Invariant | Before-commit crash | After-commit crash |
|---|---|---|
| Atomic evidence replacement | Reopen retains original semantic checkpoints, verifier rows, plan/item and lifecycle state; no refresh audit persisted | New A semantic checkpoint, valid lifecycle, old A superseded with replacement pointer, updated item and audit all persist |
| One current authority per node | Resume re-verifies A and creates exactly one durable replacement | Resume reuses committed refresh; no additional verifier call |
| Append-only history | Original three checkpoint rows unchanged | Same |
| Correct current content evidence | Final A manifest hash equals GOOD plus newline; old A superseded; B and independent remain valid | Same |
| No unnecessary task execution | Zero recovery model calls, no tool calls or operations; model-call rows unchanged | Same |
| Preserve dependencies and budgets | All items remain completed, consumed_turns=1; B/independent verifier each runs only initially | Same |

Total verifier sequence is A/B/independent/A/A before-commit (one lost attempted
refresh) and A/B/independent/A after-commit. Exactly four final semantic checkpoints
and verifier rows exist: original three plus one refreshed A. A single superseded
link points to that new checkpoint; a single refresh audit is persisted. Final
Store invariant scans pass. These are bounded crash schedules, not a general
filesystem/SQLite transaction or arbitrary external-writer CAS guarantee.

Evidence root:
`D:/opencv/learn-claude-code/.scratch/phase2-increment4-20261010/`.
Raw logs, exit codes, verifier JSONL, boundary views, database backups, 12-table
JSON dumps, file hashes and results are under `run01/`. `manifest.json` records
source/helper hashes and all pre-review evidence hashes. No original files are
overwritten. Source snapshots and a narrow patch are preserved alongside them.

## Historical PlanRevision evidence provenance

Targeted read-only search covered the original project's immediate .scratch
entries and the C1b/C1c worktrees identified by `git worktree list`. Neither
worktree has a .scratch directory; a bounded log/manifest/review filename search
found no individual raw execution logs, only the C1c review document. This is
not an exhaustive claim that the logs do not exist elsewhere.

Recovered historical documents derive from `git show` at the commits below.
Saved copies converted LF to CRLF: text is identical after newline normalization,
but these are not byte-identical Git blob copies.

- C1b `b1f0aefdaab9b07e4a62a83d40156700c8f5fe3b`:
  `docs/c1b-durable-plan-revision-resume.md`, with historical execution/review records.
- C1c `b0aec3dbd99215edace3de1d58192a066f05ce9a`:
  `docs/c1c-implementation-review.md`, with historical execution/review records.

`historical/provenance.json` records comparisons of parsed Python ASTs (excluding
line positions) against the current tree. All five selected functions match:

| Historical origin | Exact function compared |
|---|---|
| C1b | store.activate_plan_revision |
| C1b | test_activation_process_exit_reopens_complete_old_or_new_state |
| C1c | store._apply_plan_patch_conn |
| C1c | store.transaction |
| C1c | test_real_process_exit_preserves_atomic_decision_and_patch |

This strengthens source continuity for those functions only. It does not prove
identical entire files, fixtures, caller paths or environments, and does not turn
historical documented passes into a new current-tree run. The copied documents
are retrospective reports, not substitutes for missing per-run raw logs.

## Updated remaining gate

| Area | Evidence status after this increment |
|---|---|
| Increment 1 runtime repairs and verified completion F2/F3 | Prior bounded independent acceptance; unchanged |
| Increment 2 cross-process fencing and effects | Prior bounded independent acceptance; unchanged |
| Increment 3 destructive evidence invalidation | Prior bounded independent acceptance; unchanged |
| Benign evidence refresh atomicity | Two new real-exit passes; independently ACCEPT WITH NOTES within this scope |
| Plan activation / decision atomicity | Historical reports plus selected AST identity; original per-run logs still not located |
| Final Phase 2 acceptance | Not performed; a fresh reviewer must assess the final evidence matrix and explicitly treat the historical evidence limitation |

Next step is evidence/claim closure and final scoped acceptance planning, not an
automatic additional feature increment. Existing exclusions remain: no universal
exactly-once, arbitrary-writer CAS, host power-loss simulation, full interleaving
proof, semantic obligation conservation or automatic hung-owner takeover.

## Independent review

Fresh GPT-6.1 Sol medium reviewer `/root/phase2_increment4_review` returned
**ACCEPT WITH NOTES** with no blocker or substantive finding. It read all 53
manifest-bound files, seven SQLite backups and corresponding JSON, actual logs,
and recomputed the five historical AST comparisons. No tests or models reran.
Raw verdict: `review-increment4-accept.md` in the evidence root.

Its only nonblocking note concerned historical-copy newline identity. The wording
above now distinguishes text equivalence from byte identity. No accepted test,
helper, production file or original evidence changed after review. This is not
final Phase 2 acceptance.

## Local seal - 2026-10-12

Tag: `phase2-increment4-accepted-20261012`. Test source remains byte-identical
to its reviewed source manifest. Only document closure metadata changed. See
`phase2-final-acceptance.md` for the subsequent whole-phase bounded verdict.
