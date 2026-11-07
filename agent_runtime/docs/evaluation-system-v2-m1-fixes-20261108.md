# Evaluation System V2 / M1 review fixes

Date: 2026-11-08 (Asia/Shanghai). Fix base:
`0b0c021f995ba416bdb2be884cb4376d870fc039`, itself directly based on candidate
`71a605aa06de774700890b77fbe2dc2d4b1f34b0`.
Branch/worktree: `codex/evaluation-system-v2-m1`.

This follow-up fixes the four correctness findings from M1 review. It does not
start M2. The original M1 acceptance report, example, test artifacts and seals
are preserved as historical records, rather than revised to claim they covered
these failures. V1 implementations and output defaults remain unchanged.

## Fixes and regression evidence

| Finding | Correction | Regression seam |
| --- | --- | --- |
| P1: FAILED official result omitted known planned instances | Recover planned IDs from externally sealed, prediction-hash-bound predictions and matching invocation, independently of grading report availability. FAILED -> blocked; STARTED without verdict -> unknown. No resolved is inferred. | FAILED/STARTED, conventional and explicit paths, two planned instances; foreign prediction pins rejected |
| P2: conflicting Inspect planned counts accepted | Check all present declarations: fixture_outcomes.planned, planned_cases, and frozen plan case count, including explicit zero. Disagreement blocks projection. | 5 vs 1, 0 vs 1, 1 vs 5, and an explicit empty plan conflicting with results |
| P1: mixed sealed/file imports double counted | Reconcile exact report-row observations with their richer execution/oracle identity in either input order. Preserve distinct strong task IDs; refuse ambiguous weak aliases. Deduplicate named and unallocated missing samples and case-execution aliases. | Core, Inspect and Agent archive mixed views, opposite ordering, conflict/ambiguity, independent executions, missing cases |
| P2: container repair used hash of empty goal_files as oracle | For container_repair, read bindings and matching same-task verifier records. Oracle identity contains the actual verifier ID and exact bundle hash; evidence refs point to those sealed files. Missing proof remains reported; conflicting IDs/hash are rejected. | Actual binding/record, missing binding/invocation/hash, foreign task/verifier/hash |

Missing attempt parent links may be enriched by a matching record with the same
kind, source ID and raw payload. Two different known parents or conflicting raw
records still block aggregation. A weaker wrapper remains in the report for
traceability but does not create a second case execution.

Identity reconciliation is restricted to the same report-byte digest and row.
It does not merge unrelated unidentified reports. Two independently recorded
task IDs remain two samples even if an old report format produced identical
bytes. An unidentified import matching both is blocked as ambiguous. A known
oracle is not overwritten by a placeholder oracle from a file-only import.

Official plan recovery is not official grading. The adapter checks matching
dataset/Harness/prediction pins, standard prediction subjects, and invocation
instance selection when present. It accepts plan-only `official_artifacts`
with invocation/predictions; a grading report is optional for preserving unknown
subjects but mandatory for a validated resolved verdict. Preflight does not
invent a case execution attempt. Without bound plan IDs, a summary remains
PARTIAL as before. Neither a physical execution nor author identity is
authenticated by matching hashes.

## Tests

`test_evaluation_m1_fixes.py` adds 28 checks. Together with the original 50
contract checks: **78 passed, no skips**. Nine selected V1/Inspect/SWE offline
test modules: **165 passed, 2 skipped** (Windows symlink privilege), with 43
upstream Inspect deprecation warnings. These are limited regression batches,
not full Runtime or all Evaluation acceptance.

The first pre-fix test batch contained 15 checks: 14 failed and one independent
execution control passed. Its JUnit is preserved as `red.xml`. Later incremental
batches overlap and are not summed. Exact final commands and JUnit SHA-256s are
recorded in `evidence/evaluation-v2-m1-fixes-20261108/checks.json`.

The end-to-end report test installs tripwires on Runtime run/resume, Runtime DB
initialization, Anthropic model initialization, official grade(), and subprocess
execution. Projection of core and failed-official evidence passes without
invoking any of them. All other new tests use synthetic result files only.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest agent_runtime/tests/test_evaluation_contract.py `
  agent_runtime/tests/test_evaluation_m1_fixes.py -q --tb=short -p no:cacheprovider `
  --basetemp=<fresh-temp> --junitxml=<new-unit.xml>

python -m agent_runtime.evaluation_v2 `
  agent_runtime/docs/evaluation-v2-m1-fixes-input-20261108.json <fresh-output>

python agent_runtime/docs/evidence/evaluation-v2-m1-fixes-20261108/verify.py
```

## New frozen-evidence example

Input: `evaluation-v2-m1-fixes-input-20261108.json` (absolute local source paths).
Output: `../examples/evaluation-v2-m1-fixes-20261108/report.md` and `report.json`,
with a new independent seal. The 13 input entries include the original M1
batch, file-only aliases of core/Inspect reports, and the existing real-container
model-run evidence plus its file-only alias. The model-run bundle is nested in
the already used candidate bundle; no new repair/model/container work occurred.

The result has **11 distinct objective-specific case observations and 53 unique
persisted attempt records**. Added aliases do not enlarge denominators; the
additional container fixture-goal observation comes from explicitly supplying
that existing result. It remains separate from candidate_validation. There is
no combined success rate. Duplicating the entire input list gives identical
aggregates. The actual container verifier is `docker-command-probes-v1:71231…`,
bound to its recorded `42012573…` verifier bundle, rather than a file-goal hash.

New projection of the original nine-entry M1 input preserves its original
aggregate results (10 observations / 52 attempts). Original sealed files are
byte-checked against the M1 Git commit as well as their inventory. The old
verify.py compares producer fingerprints too and belongs with its original
implementation commit; the new verifier explicitly checks the old seal and
aggregate compatibility without replacing historical producer metadata.

## Remaining boundaries

- Prior M1 schema/identity/comparison limits remain. Summary-only Suite/Inspect
  archives still do not fabricate cases. Missing latency/cost/usage stays null.
- No live model, real container execution, image build or official Harness run
  was performed. Official failure/contradiction tests are synthetic and are not
  new official evaluation results. Real container evidence is historical import.
- No Runtime DB migration, historical cleanup, PR merge or publication. Original
  user workspace changes and Draft PRs #19/#20 are preserved.
- The two P3 maintainability suggestions from review (shared seal-reading layer
  and shorter case construction) are not bundled into this correctness patch.

The four review findings are fixed with focused regressions. This is a corrected
M1 submission for acceptance; it does not claim M2 or expanded model competence.
