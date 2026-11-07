# Evaluation System V2 / M1 acceptance report

Date: 2026-11-07 (Asia/Shanghai). Baseline:
`71a605aa06de774700890b77fbe2dc2d4b1f34b0`.
Branch: `codex/evaluation-system-v2-m1`, in an independent worktree.

M1 implements a result-only contract and compatibility projection. It does not
replace the Runtime, Inspect, Verifier, scheduler, or SWE-bench Harness. All V1
implementation files, output defaults and historical evidence remain unchanged.
No Runtime DB is opened or migrated by the V2 projection path. PRs #19/#20 remain
open Drafts at their original heads. No push, PR creation or merge is part of M1.

## Status

| Deliverable | Status | Evidence |
| --- | --- | --- |
| Versioned Run/Case/Attempt/Metric/Evidence contract | IMPLEMENTED | `evaluation_contract.py`; new contract tests |
| Seven requested V1 result adapters plus JUnit | IMPLEMENTED, fidelity limits below | `evaluation_adapters.py`; schema-parameterized tests |
| Objective/oracle binding and four-state verdict | IMPLEMENTED | contract validation; regression/goal/official/JUnit separation |
| Identity, import deduplication, independent repeats, conflict blocking | IMPLEMENTED where source identity exists | identity and conflict tests; repeated real-source import check |
| Planned denominator and unknown measurements | IMPLEMENTED | named missing cases and unallocated counts tested |
| Opt-in report with independent seal | IMPLEMENTED | `python -m agent_runtime.evaluation_v2`; checked-in example |
| Complete identities for every legacy result | PARTIAL | 8/10 example observations have source execution identity; missing identities are not invented |
| Complete frozen comparison configuration for historical runs | PARTIAL | missing fields and null comparison keys retained; no reconstructed seeds/budgets |
| Authoritative standalone official summary without its evidence chain | BLOCKED by design | example preserves claimed resolved in raw data but projects unknown |
| New live model/container/Harness acceptance | NOT RUN | prohibited for this milestone |

## Entry point and immutable inputs

```powershell
python -m agent_runtime.evaluation_v2 INPUT.json NEW_OUTPUT_DIRECTORY
```

Input envelope: `evaluation-v2-input-v1`, with `run_id` and explicit `entries`.
Each entry requires an `id`, absolute report `path`, and external `sha256`.
A bundle also requires absolute `bundle` and an external `seal_sha256`.
Optional fields: `scope`, `comparison`, `projection_of`, `official_artifacts`,
and `format: junit`. Unknown fields are rejected.

Every supplied seal member, complete inventory and report pin is checked before
creating output. Reparse/symlink inputs use the existing bounded reader and
inventory checks. A bad pin, conflicting identity, or invalid provenance aborts
projection with CLI exit 2 before output. Hashes establish byte integrity, not
author authentication or truth of a controller's claimed physical environment.

The output directory must be fresh and disjoint from input directories. The
renderer writes `input.json`, original report byte snapshots in `sources/`,
`report.json`, `report.md`, and its own `seal.json`. Existing output is never
overwritten or rerun. Partial outputs are preserved on post-creation failure.
Source implementation hashes are recorded in `producer`. Scoped Git attributes
preserve sealed bytes across Windows/Unix line-ending conversion.

The report snapshots do not copy all original databases or private evaluator
artifacts: full revalidation still needs the externally pinned source bundles.
No recursive search, automatic runner dispatch, repair, scoring or retry occurs.

## Contract and interpretation

`evaluation-run-v2` contains the five entities. The report envelope is
`evaluation-report-v2`; cohort calculations use `evaluation-cohorts-v2`.
Original result documents and rows remain in `raw` without changing their V1
meaning. Evidence mode (`imported`, `projection`, `reused`) is separate from
the original execution's four scope dimensions:

- model mode: scripted/mock/live/none/unknown;
- environment: local/simulated_container/real_container/unknown;
- grading: runtime_oracle/controller_verifier/official_harness/test_assertion/unknown;
- subject role: runtime_regression/agent_candidate/scripted_candidate/gold_control/test_execution/unknown.

Historical status is retained by the import/projection provenance and original
`historical` fields, rather than replacing the original model/environment scope.
An unknown dimension stays unknown. A controller declaration that contradicts
source semantics is rejected. No official scope is inferred from a boolean.

Every case verdict binds an objective and an Oracle. Authority is separately
`reported`, `source_validated`, or `unavailable`. Source validation checks a
frozen evidence chain; it is not a claim that M1 executed its oracle. An
unavailable oracle cannot produce a pass/fail verdict.

| Verdict | Interpretation |
| --- | --- |
| pass | Reported or source-validated oracle satisfies this specific objective |
| fail | Reported or source-validated oracle rejects this specific objective |
| blocked | Known processing/reconciliation obstacle prevents a valid outcome |
| unknown | No determinate outcome or sufficient provenance is available |

V1 Verifier `uncertain` remains unchanged in raw records. V2 does not alter its
fail-closed continuation treatment. A scripted case expecting `needs_review`
can pass `runtime_regression`; this never manufactures `task_success`. JUnit
pass/failure/error/skip maps to test_execution pass/fail/blocked/unknown only.

For each objective + execution scope + recorded frozen comparison context:

```text
N = pass + fail + blocked + unknown
success_lower_bound = pass / N                      (null when N = 0)
known_fraction = (pass + fail) / N                   (null when N = 0)
strict_success_rate = pass / N only if N > 0 and blocked = unknown = 0
```

Strict completeness refers to verdict completeness, not proof that comparison
configuration, physical context, or every measurement is known. Unknown legacy
configuration is explicitly PARTIAL and is not a frozen comparative baseline.
Different recorded models, budgets, conditions and task versions do not pool;
seed/repeat identify independent samples within that context. There is no
cross-scope overall success rate. No pass@k or best-attempt selection is inferred.

When a sealed plan names missing cases, those actual IDs receive unknown
observations with no invented execution attempts. When only a planned count is
available, the missing count is `unallocated`, contributes unknown to N, and
does not fabricate case IDs. Summary-only Suite/archive data does not become
task cases. Duplicate imports also deduplicate those unallocated counts.

Provider token totals require live mode and complete nonnegative usage for both
directions. Known subtotals remain separately labeled with incomplete status.
Mock/scripted usage never becomes provider usage. Cost and latency are null in
M1: these V1 result adapters lack an agreed measurement/billing basis. Usage
completeness is independent of task verdict. M1 does not read token values from
redacted Trace JSONL or change Trace redaction.

## Identity and attempt rules

Source Execution Identity binds explicit original execution ID, source
case/instance, objective and oracle. A persisted Runtime task ID or an original
archive run ID can establish it. Logical plan/run names alone do not establish a
new execution. Case execution attempts may be shared across objective verdicts.

Comparison Identity stores recorded task_version, condition, model, budget,
fault, seed and repeat. Absent fields remain missing; null model/budget/task
version/condition/repeat does not establish completeness. An explicit null
fault/seed can mean no injection/no controlled seed. The adapter also retains
an explicitly recorded Inspect model; it does not invent the other settings.

Identical imports deduplicate by source identity. Without such an identity,
only the exact report-digest/row/objective observation deduplicates; different
unidentified reports are never forced together. Inspect archive projections
contribute no new task observations. Independently identified repeats remain.
Same source identity with conflicting verdict, scope, comparison, metrics or raw
outcome blocks aggregation. Persisted attempt identity conflicts also block.

Attempt kinds: suite_invocation, case_execution, model_generation,
verifier_invocation, tool_operation. Persisted model/verifier/tool records keep
their source IDs and raw statuses. A tool record's retry count remains raw; no
per-retry record is invented. Retry records do not add case denominator. Parent
links are supplied only when the original case execution is unambiguously known.

## Original evidence to V2 mapping

| V1 schema | Case/objective mapping | Attempts / limitations |
| --- | --- | --- |
| evaluation-report-v1 scripted_runtime | runtime_case_pass -> runtime_regression; checks hash if plan available | Runtime summary task_id -> case_execution; no identity from logical run_id alone |
| evaluation-report-v1 agent_archive | task_success -> task_success; unverified positive archive rejected | Frozen task_id when recorded, otherwise original source_run_id; model_mode from frozen plan; raw runtime completed stays separate |
| evaluation-report-v1 official_archive | Official receipt inputs revalidated with existing pure V1 parser -> official_resolution | Original execution identity, Harness/dataset oracle, explicit gold/agent role |
| evaluation-suite-result-v1 | Raw job metrics, no inferred task cases | Persisted UUID attempt filename can identify suite invocation; logical run_id cannot |
| inspect-runtime-result-v1 | runtime_case_pass -> runtime_regression; source plan checks when available | task_id and sealed model/tool/verifier records; missing samples retain denominator |
| inspect-model-result-v1 | fixture_success -> fixture_goal, not official SWE resolution | Explicit model/mode and usage; fixture outcome consistency checked |
| inspect-offline-result-v1 | Summary/projection only; zero new cases | projection_of validates original source run/seal/aggregates when supplied |
| inspect-swe-candidate-v1 | candidate_validated -> reported candidate_validation; manual recovery -> blocked | Standalone schema omits exact verifier/run identity; raw sealed subrecords retained but no case_execution invented |
| swe-official-run-v1 | official_resolution only with sufficient provenance; otherwise unknown | Sealed invocation/predictions/report hashes, complete official partition, explicit real official scope required |
| JUnit | Test execution only | Does not infer Runtime/model/Agent attempts |

V1 evaluation archives relocate original artifacts into `<case>/<role>.input`.
M1 checks the `inputs.json` source/hash mapping before revalidating their receipt;
it never reads the original possibly absent path recorded in the plan instead.

For standalone official reports, `official_artifacts` names sealed `invocation`,
`predictions` and `report` members. The adapter reuses the pure `read_report`
parser and checks hashes, immutable Harness pin, exit status, original run ID,
prediction IDs, empty-patch partition and result agreement. It never calls
`grade()`. A simulated or undeclared physical official execution remains unknown
even if a synthetic report chain is internally consistent. A malicious author
can fabricate matching bytes and declarations; cryptographic authentication is
outside M1. Existing evaluator-owned official_archive receipts remain supported.

## Existing-evidence example and acceptance

Input: `evaluation-v2-m1-example-input.json`.
Output: `../examples/evaluation-v2-m1-final/report.md` and `report.json`.
The input uses absolute paths to the existing local archive; it is a reproducible
local manifest, not a claim those private/local bundles were uploaded.

Nine input entries include seven distinct existing sealed bundles (129 members),
an intentional repeated core import, and a separately SHA-pinned standalone
official summary. Read-only revalidation confirms the source pins and every
supplied member still match. No historical file is modified. Importing the entire
batch a second time produces identical aggregate results.

| Original scope/objective | Planned N after dedup | Pass | Fail | Unknown |
| --- | --- | --- | --- | --- |
| Scripted local Runtime regression | 1 | 1 | 0 | 0 |
| Inspect mock Runtime regression | 3 | 3 | 0 | 0 |
| Mock fixture goals | 2 | 1 | 1 | 0 |
| Historical live Agent task outcome | 1 | 0 | 1 | 0 |
| Historical official gold control | 1 | 1 | 0 | 0 |
| Existing real-container scripted/mock candidate validation | 1 | 1 | 0 | 0 |
| Standalone official summary with insufficient bindings | 1 | 0 | 0 | 1 |

These are separate cohorts, not one success rate. The report has 10 distinct
case observations, 8 source-identified executions, and 52 persisted attempt
records: 8 case executions, 26 model generations, 12 tool records and 6 Verifier
invocations. The archive projection adds none. The candidate and standalone
summary identities remain PARTIAL. The official summary's original resolved=true
is retained in raw data; its unified verdict is unknown without the specified
seal/provenance binding. This does not retract the historical official result.

Example generation executes no Runtime, model, container or Harness. The initial
development report was preserved outside the worktree in
`C:/Users/Lenovo/AppData/Local/Temp/evaluation-v2-m1-initial-report-20261107`.
Final example verification and byte preservation checks are recorded separately
in `evidence/evaluation-v2-m1/checks.json`, with independent JUnit files and seal.

## Tests and commands

First run the new deterministic checks, then the relevant original offline tests.
Use a fresh basetemp/output for a new execution; do not overwrite frozen outputs.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest agent_runtime/tests/test_evaluation_contract.py -q --tb=short -p no:cacheprovider `
  --basetemp=<new-unit-directory> --junitxml=<new-unit.xml>

# Existing environment: inspect-ai 0.3.283, pytest 9.0.3; no dependency installation.
& D:/opencv/learn-claude-code/.scratch/inspect-offline-venv/Scripts/python.exe -m pytest `
  agent_runtime/tests/test_evaluation.py agent_runtime/tests/test_evaluation_bundle.py `
  agent_runtime/tests/test_evaluation_report.py agent_runtime/tests/test_evaluation_suite.py `
  agent_runtime/tests/test_inspect_adapter.py agent_runtime/tests/test_inspect_runtime_solver.py `
  agent_runtime/tests/test_inspect_model_eval.py agent_runtime/tests/test_inspect_swe_runner.py `
  agent_runtime/tests/test_swe_grading.py -q --tb=short -p no:cacheprovider `
  --basetemp=<new-regression-directory> --junitxml=<new-regression.xml>

python -m agent_runtime.evaluation_v2 agent_runtime/docs/evaluation-v2-m1-example-input.json `
  agent_runtime/examples/evaluation-v2-m1-final

# Read-only revalidation, including a second import of every source; no reruns.
python agent_runtime/docs/evidence/evaluation-v2-m1/verify.py
```

The final new contract batch: 50 passed, no skips. The regression batch:
165 passed, 2 skipped (Windows symlink privilege), 43
upstream Inspect deprecation warnings. These are nine selected modules, not
full Runtime/all Evaluation acceptance. They exercise deterministic Runtime/mock
paths; no live provider, real Docker or official scoring execution is enabled.
The final new-test result and exact JUnit hashes are in `checks.json`. Earlier
incremental new-test batches overlap and are not summed.

## M1 limits and deferred work

- Unsupported result schemas: legacy eval_runner's unversioned YAML report,
  standalone c1c_eval/experiment manifests, swe-acceptance-v1, dataset/preparation
  manifests and raw DB/Trace-only input. They fail as unsupported rather than
  being heuristically converted. All seven requested schemas and JUnit are
  recognized, with summary-only/identity/provenance limits stated above.
- No arbitrary traversal of Suite child paths or reconstruction of archive
  samples. Supply child evidence explicitly. Summary-only jobs cannot establish
  an Agent task denominator.
- No live model validation of the Inspect model runner, real end-to-end
  grade(execute=True), or new official grading. Official chain parser acceptance
  is tested with controlled synthetic evidence; existing gold receipt validation
  uses historical evidence.
- No automatic repair of missing identities, exact candidate verifier identity,
  comparison config, temporal metadata, latency or billing records.
- No Trace-to-mechanism metric expansion, baseline A/B experiments, pass@k,
  dashboards or M2 scenarios. M1 stops at the contract/projection boundary.

M1 acceptance is IMPLEMENTED with explicit PARTIAL legacy fidelity. The frozen
batch is projected without re-execution, duplicate imports do not enlarge its
denominators, negative/unknown outcomes remain visible, and evidence remains
traceable. A complete legacy identity/comparison baseline is not claimed.
