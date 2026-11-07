# Unpublished Runtime successor review — 2026-11-01

Status: three concrete findings fixed; affected offline checks passed. Real
Docker/official grading and hosted validation remain open. This is a local code
review, not renewed benchmark acceptance or authorization to publish.

## Baseline and inventory

Active worktree: `.scratch/inspect-runtime-solver-worktree`, local HEAD
`c3e1192d0a83038d7d21d2ca92fd1935d01a1a3e`.

The local 2026-10-25 publication receipt records remote candidate HEAD
`4b872495a72a88f17d44acc532183bc0b46fac72`, tested source
`f9eaa771ad93dd201127a535866d4253cd6ca713`, and 55 historical incremental
passes. No remote refresh was performed in this review; those are historical
receipt facts, not a statement about GitHub's current branch state.

At review start there were 47 modified/untracked files: 10 are content-equivalent
to the published candidate ZIP after CRLF normalization; 37 are successor or
changed files. The ZIP predates publication-only documentation edits, so the
37-file group is a review queue, not an authoritative diff against remote HEAD.
Untracked locally does not imply unpublished remotely. No files were moved,
deleted, committed or uploaded. Teaching and research tracks were not changed.

Machine-readable paths, exact byte hashes and classifications are recorded in
repository-root `.scratch/unpublished-review-20261101/inventory.json`. This report
is an additional file beyond that initial inventory.

## Findings and fixes

| Priority | Finding and trigger | Fix | Validation |
| --- | --- | --- | --- |
| P2 | `inspect_swe_runner.run_instance` preflight substituted a placeholder for the real mock script. A native tool, malformed response, invalid usage or oversized script passed preflight and failed only after provisioning/binding. | Extracted the existing response validator into `validate_mock_responses`; apply it to the actual script and container tool allowlist before creating output or a capability. Full bound-plan validation remains in place. | 12 new runner cases cover preflight and execute; 2 new orchestrator cases confirm no provisioning occurs. |
| P2 | `swe_environment.checked_archive` advertised a 128 MiB archive limit but called the JSON/evidence reader, which rejects above 16 MiB. Legitimate larger bases failed before Docker. | Use a dedicated bounded binary read of at most 128 MiB + 1, retaining regular-file/reparse checks, digest checks and tar member/content limits. | A valid 17 MiB member is accepted; an archive above 128 MiB is rejected before reading. |
| P2 | Manual incremental CI omitted `test_swe_environment.py`, `test_swe_grading.py`, and `test_swe_acceptance.py`. A green run would not check the newest lifecycle/scoring/composition modules. | Added all three modules to the existing manual-only workflow. | Referenced paths and YAML were statically checked. Workflow has not run remotely; no hosted success is claimed. |

The first fix shares validation rules rather than maintaining two divergent
response schemas. It rejects malformed script envelopes; Runtime tool adapters
still validate actual tool arguments at execution time.

## Review focus

Source inspection covered evaluation suite receipt reuse and source/environment
fingerprints; dataset solver/evaluator field separation; bounded model call/audit
handling; container read-before-edit, CAS and unknown-write treatment; candidate
proof-to-prediction export; lifecycle ownership/retention; and official report
identity, denominator and count checks. Runtime/Store/read-budget integration was
compared with the published candidate, and its newer controller-write declaration
was inspected. The archived v3.0.17 harness reporting implementation was used as
local API reference only, without executing it.

The inspected failure paths preserve empty predictions for planned failures,
retain environments when Runtime needs review, and do not translate candidate
validation into official resolution. These observations plus targeted tests do
not establish correctness against a real Docker Engine or official harness.

## Validation evidence

One fresh targeted invocation: **61 passed, 0 failed, 0 skipped**, 55.98 seconds.
There are 16 newly added regression cases and 45 affected existing cases.
The only warnings were 8 upstream Inspect deprecation warnings.

Selected checks:

- `agent_runtime/tests/test_inspect_swe_runner.py`
- `agent_runtime/tests/test_swe_acceptance.py`
- `agent_runtime/tests/test_swe_environment.py`
- `agent_runtime/tests/test_inspect_runtime_solver.py::test_invalid_plan_rejected_before_output_or_tools`

The real Inspect/Runtime mock integrations used simulated container transport.
Dependencies were reused through a process-local overlay. Supported app-data
and temp overrides confined logs to the fresh review evidence directory.

Evidence at repository-root `.scratch/unpublished-review-20261101/`:
`junit.xml`, `pytest.log`, `validation.json`, `inventory.json`. No historical full
suite, accepted experiment, official grader, real Docker or paid provider ran.
Historical 38/16/16/27/10-check milestones were not added to the 61 as if they
were fresh or disjoint verification of this source version.

## Suggested publication units and remaining gates

| Unit | Scope | Remaining gate |
| --- | --- | --- |
| Published closeout | Read budget, fencing fixture, earlier Inspect integration | Preserve recorded scope; do not republish unchanged files merely because local HEAD is older. |
| Evaluation successor | Dataset, suite, bounded model evaluation, fixtures | Review exact diff against remote target; new manual hosted CI at the final source SHA. |
| Container repair | Edit bridge, controller-write registration, Inspect binding | Real container read/edit/verification acceptance. |
| SWE orchestration | Instance runner, environment, grader and acceptance | Matching image/base/probes; real lifecycle; clean pinned Linux harness and official report. |
| Review corrections | Shared script preflight, archive reader, CI coverage and regressions | Included in the local snapshot and 61 affected checks; hosted CI is still pending. |

General image/dependency installation remains outside provisioning from prepared
assets. Autonomous live model repair is not demonstrated by scripted mock edits.
No merge, branch cleanup, release acceptance or benchmark score is implied.

## File catalog at review start

### Equivalent to the published candidate archive (CRLF-normalized)

- `.github/workflows/runtime-closeout.yml`
- `agent_runtime/docs/container-read-budget-20261019.md`
- `agent_runtime/docs/evaluation-system-validation-20261022.md`
- `agent_runtime/docs/inspect-adapter-validation-20261022.md`
- `agent_runtime/docs/inspect-runtime-validation-20261023.md`
- `agent_runtime/docs/release-readiness-20261017.md`
- `agent_runtime/experiments/container_read_budget_probe.py`
- `agent_runtime/read_budget.py`
- `agent_runtime/tests/test_controller_read_budget.py`
- `agent_runtime/tests/test_phase1_p0.py`

### Successor or changed relative to that archive

- `.github/workflows/evaluation-system.yml`
- `agent_runtime/README.md`
- `agent_runtime/benchmark_dataset.py`
- `agent_runtime/container_edits.py`
- `agent_runtime/container_reads.py`
- `agent_runtime/docs/container-repair-20261026.md`
- `agent_runtime/docs/evaluation-system-20261026.md`
- `agent_runtime/docs/inspect-swe-runner-20261027.md`
- `agent_runtime/docs/project-closeout-20261023.md`
- `agent_runtime/docs/swe-acceptance-20261030.md`
- `agent_runtime/docs/swe-host-access-recheck-20261030.md`
- `agent_runtime/docs/swe-host-blocker-20261030.md`
- `agent_runtime/docs/swe-lifecycle-grading-20261030.md`
- `agent_runtime/evals/model-fixtures-v1.json`
- `agent_runtime/evals/system-core-v1.json`
- `agent_runtime/evals/system-inspect-v1.json`
- `agent_runtime/evals/system-suite-v1.json`
- `agent_runtime/evaluation_suite.py`
- `agent_runtime/inspect_model_bridge.py`
- `agent_runtime/inspect_model_eval.py`
- `agent_runtime/inspect_runtime_solver.py`
- `agent_runtime/inspect_swe_runner.py`
- `agent_runtime/runtime.py`
- `agent_runtime/store.py`
- `agent_runtime/swe_acceptance.py`
- `agent_runtime/swe_environment.py`
- `agent_runtime/swe_grading.py`
- `agent_runtime/tests/test_benchmark_dataset.py`
- `agent_runtime/tests/test_container_edits.py`
- `agent_runtime/tests/test_evaluation_suite.py`
- `agent_runtime/tests/test_inspect_model_container.py`
- `agent_runtime/tests/test_inspect_model_eval.py`
- `agent_runtime/tests/test_inspect_swe_runner.py`
- `agent_runtime/tests/test_swe_acceptance.py`
- `agent_runtime/tests/test_swe_environment.py`
- `agent_runtime/tests/test_swe_grading.py`
- `agent_runtime/tool_registry.py`
