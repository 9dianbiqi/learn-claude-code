# M1 multi-record fixes: target ownership and partial bundle evidence

Date: 2026-11-08 (Asia/Shanghai). Fix base:
`c27b6b383e1faed53cb7de02d8f7ba64a0921621`.
Branch: `codex/evaluation-system-v2-m1`. Scope remains M1, not M2.

## Corrections

| Review finding | Correction | Regression coverage |
| --- | --- | --- |
| Other subtask's success covered fixture failure | Validate every explicit subtask and plan-item association before selecting the latest invocation. Foreign subtask or multiple different plan-item IDs block projection. Missing associations remain reported/PARTIAL. | Foreign subtask, foreign item, conflicting labels, missing fields, invalid ID types, normal same-item retry |
| Missing hash masked known bundle conflict | Validate all supplied non-null hashes first. Multiple known hashes or malformed known hashes block even when another record has no hash. Only then assess completeness. | Missing value in every position, malformed known value, consistent partial hashes, all missing, cross-import refinement with distinct invocation IDs |

The supported Inspect producer requires its container verifier node to have
subtask_id `fixture` (`inspect_runtime_solver.validate_plan`, and the existing
`container_edits.repair_binding`). M1 uses that producer contract; it does not
guess that the external case label (for example `repair`) is the subtask ID.

Plan-item IDs are scoped by the already validated task and fixture subtask. An
explicit ID must be a positive integer; booleans, strings and nonpositive values
are not silently coerced. All known IDs in the per-case export must agree. M1
does not hardcode SQLite row ID 1 or open the Runtime DB to reconstruct missing
values. A foreign explicit association is a contradiction, not a record to
silently discard before choosing a convenient success.

Successful model fixture goals still require a latest scoped authoritative pass.
If the latest record lacks its association, it cannot establish validated
success. Missing legacy associations are not filled. Export-level incomplete
associations keep oracle authority reported; known identities, raw outcomes and
evidence references remain present. This does not reclassify expected negative
Runtime regressions as failed Agent tasks.

For a bound verifier, every known bundle SHA-256 is checked before handling
missing values. A single consistent known hash is retained in the Oracle's
explicit identity even when other invocations omit theirs. The original omitted
values remain omitted/null in the source and raw attempt records. Authority is
reported until the evidence is complete. If all hashes are absent, no hash is
invented. A missing hash can no longer hide C versus D, or a malformed known
value. Retained C also prevents a different imported bundle D from being
accepted as a harmless refinement.

## Tests and evidence

`test_evaluation_m1_multirecord.py` adds 21 checks. The final pre-fix batch was
**19 failed, 2 passed**; it is preserved as red.xml. The two passing controls
were a legitimate same-item retry and the all-hashes-missing partial case.
Cross-import tests use distinct verifier invocation IDs so they exercise oracle
identity reconciliation rather than accidentally passing on an attempt-ID
collision. Earlier exploratory batches overlap and are not summed.

The synthetic positive `container_entry` test fixture now includes the subtask
and plan-item fields present in real Runtime exports. Its success expectations
were not relaxed. Separate missing-association tests explicitly remove those
fields and assert reported/PARTIAL. No historical fixture outputs, JUnit files,
report or seal was edited.

Final four-module contract/regression batch: **126 passed, no skips** (previous
105 plus new 21). Nine related original offline modules: **165 passed, 2
skipped**, with 43 upstream Inspect deprecation warnings. The skips require
Windows symlink privileges. This is bounded regression validation, not full
Runtime/all Evaluation acceptance. Exact commands and JUnit hashes are stored
in `evidence/evaluation-v2-m1-record-scope-20261108/checks.json`.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:TEMP='D:\CodexTemp\evaluation-m1-fix-review-20261108-native'
$env:TMP=$env:TEMP
python -m pytest agent_runtime/tests/test_evaluation_contract.py `
  agent_runtime/tests/test_evaluation_m1_fixes.py `
  agent_runtime/tests/test_evaluation_m1_authority.py `
  agent_runtime/tests/test_evaluation_m1_multirecord.py -q --tb=short -p no:cacheprovider `
  --basetemp=<fresh-D-directory> --junitxml=<new-D-junit.xml>

python -m agent_runtime.evaluation_v2 `
  agent_runtime/docs/evaluation-v2-m1-record-scope-input-20261108.json <fresh-output>

python agent_runtime/docs/evidence/evaluation-v2-m1-record-scope-20261108/verify.py
```

## Append-only report and limits

The new report is `../examples/evaluation-v2-m1-record-scope-20261108/report.md`
and report.json, generated only from the same 13 pre-existing evidence entries.
The existing real-container model-run records already include the proper
fixture/plan-item association and consistent bundle hashes. No model, container,
Runtime task or official Harness was executed to generate the report.

The report retains 11 objective-specific observations and 53 unique persisted
attempt records. Doubling the complete input batch does not add samples.
Eight supplied sealed bundles require 146 member checks but cover only 129
distinct source files because one bundle is nested. The original M1, first-fix
and second-fix aggregate results are preserved. Their artifacts are byte-checked
against the fix base; their producer metadata and seals are not overwritten.

Original M1 limitations remain: hashes do not authenticate authors or physical
execution; insufficient identity/comparison data is not reconstructed; unsupported
schemas are rejected; missing cost/latency/usage stays missing. This patch does
not add a DB migration, rerun old experiments, clean evidence, push/merge PRs or
start M2. The initial P3 maintainability suggestions remain deferred. The patch
is submitted for review, without claiming independent acceptance.
