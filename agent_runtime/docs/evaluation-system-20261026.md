# Evaluation system — implementation and offline acceptance

Follow-up: [container repair capability](container-repair-20261026.md) now adds
bound-container CAS edits, explicit external-effect registration and Inspect
controlled integration. Its offline evidence is separate from this initial
milestone; general provisioning/live repair/official execution remain pending.

Date: 2026-10-26. Successor to PR #19's `4b87249` candidate.
The user selected implementation plus offline acceptance; no new provider or
official harness execution is included. Prior accepted tests/experiments retain
their original source scope.

## Components and authority

| Component | Implementation | Authority / boundary |
| --- | --- | --- |
| Scripted Runtime cases and historical results | `evaluation.py` | Separate Runtime, Agent and gold-control denominators |
| Verified archive input | `evaluation_bundle.py` | External seal digest, complete inventory, identity and aggregate validation |
| Inspect archived evaluation | `inspect_adapter.py` | Real Dataset/Solver/Scorer/.eval, no generation; projection adds no subjects |
| Controlled Runtime integration | `inspect_runtime_solver.py` | Real Runtime/tool/recovery execution through Inspect mock generation |
| Model task execution | `inspect_model_eval.py` | Explicit provider/model, request/output/time limits; local file-goal tasks |
| Dataset and prediction interchange | `benchmark_dataset.py` | Pinned SWE-bench snapshot, separate solver/grader views, complete prediction denominator |
| Suite orchestration | `evaluation_suite.py` | Sequential jobs, frozen input/source/environment, verified reuse, append-only attempts |
| Official grading | Existing `official_archive` evaluator mode | Requires official report and evaluator-owned receipt; model self-report never supplies resolved |

Inspect's custom Solver runs the existing Runtime on a worker thread. Runtime
owns execution, permissions, checkpoints and Verifier state. Inspect owns model
generation and .eval observation. The optional bridge factory and timeout/usage
attributes preserve the old controlled bridge's mock-only defaults. The new
bounded bridge chooses the explicit model and labels provider vs synthetic usage.

Reference research: [Inspect Solvers](https://inspect.aisi.org.uk/solvers.html),
[Inspect Eval Sets](https://inspect.aisi.org.uk/eval-sets.html), and
[SWE-bench evaluation](https://www.swebench.com/SWE-bench/guides/evaluation/).
The suite uses the existing runners rather than replacing Runtime recovery with
Inspect retry: controller-owned closures/Verifier goals and Runtime DBs require
their own frozen inputs and completion receipts. It never auto-retries a partial
Runtime job through Inspect.

## One command for the offline suite

Install `agent_runtime/requirements-inspect.txt` (Inspect 0.3.283). From repository root:

```sh
python -m agent_runtime.evaluation_suite agent_runtime/evals/system-suite-v1.json /absolute/new-output
```

The example runs one new two-file core task, archives that result through Inspect,
runs two new Inspect Runtime fixtures, and runs a model-interface fixture in mock
mode. Partial multi-file completion must fail; a persisted first edit must survive
recovery while the second file is completed.

Run the identical command again to verify and reuse completed child evidence.
Reuse requires the same suite/child bytes, implementation fingerprint and installed
distribution fingerprint. It does not invoke Runtime/model/harness again. Changed
source/environment requires a new output; explicitly import pinned old bundles
when their historical scope is desired. Partial child directories are preserved
and blocked, never silently removed or restarted. A lock prevents concurrent
invocations; after hard process death an operator must establish that the owner
has stopped before handling a stale lock. There is no automatic PID-only unlock.

`attempts/*.json` and matching Markdown show every planned job, including blocked
jobs. `processing_ok` means evidence was processed. `accepted` means the recorded
job outcomes all pass (for an archive projection it means import fidelity);
neither means autonomous coding competence. Unknown outcomes remain null. Metrics
remain per-job: archived projections and repeated experiments must not be added
to a combined success denominator.

An imported job has this shape:

```json
{"id":"prior","engine":"evaluation","bundle":"/absolute/old-bundle","seal_sha256":"<trusted SHA-256 of seal.json>"}
```

Engines: `evaluation`, `inspect_archive`, `inspect_runtime`, `inspect_model`.
An archive job can instead reference an earlier evaluation job using `source_job`.
Executable jobs pin a relative `plan` and `sha256`. The suite executes only offline
model plans. Live execution uses the explicit separate CLI below.

## Model evaluation, including preflight without calls

```sh
python -m agent_runtime.inspect_model_eval agent_runtime/evals/model-fixtures-v1.json /absolute/new-output
python -m agent_runtime.inspect_model_eval agent_runtime/evals/model-fixtures-v1.json /absolute/new-output --execute
```

Without `--execute`, the command only validates the plan/dependency pin and reports
the upper request bound. It does not construct a model or create output. The
checked-in example is mock-only. For a separately authorized live run, copy the
plan, set `mode` to `live`, specify an explicit `provider/model`, and remove the
per-case scripted `model` arrays. Configure credentials outside the plan; never
publish credentials or production inputs in fixtures/logs.

Each case has prompt, initial files, evaluator-owned `goal_files`, and `max_turns`.
Goals stay in the controller closure, not model context. The request/output-token
and timeout limits are explicit; provider retries are disabled. Request counts
are bridge generation attempts, not billing guarantees. Per-request timeout and
Inspect time limits are cooperative, not hard OS termination. Only native local
read/write/edit tools are exposed; this is not a hostile-code sandbox. Failed
file-goal checks remain failed, missing sample results remain unknown in the
planned denominator. Raw Runtime traces, requests, receipts and Inspect logs are
preserved and cross-checked. A mock positive result is not a provider result.

## SWE-bench preparation and prediction export

```sh
python -m agent_runtime.benchmark_dataset prepare CONFIG.json /absolute/prepared
python -m agent_runtime.benchmark_dataset predictions /absolute/prepared RESULTS.json /absolute/export --manifest-sha256 <digest>
```

CONFIG uses `schema_version: swebench-dataset-v1`, `run_id`, immutable 40-character
`dataset_revision` and `harness_commit`, a relative JSON/JSONL `snapshot`, its
SHA-256 `sha256`, and explicit `instance_ids`. No dataset is downloaded implicitly.
The snapshot is bounded and required SWE fields are validated before output.

`solver/dataset.json` exposes only instance_id, repo, base_commit and problem_statement.
`evaluator/dataset.json` retains scoring inputs. Directory separation is an input
contract, not a filesystem access-control boundary: never mount the evaluator
directory into a solver container. The manifest pins both views and config.

RESULTS is an array with exactly instance_id, model_name_or_path and model_patch
for every selected instance. Empty patches remain present. Export writes official
JSONL and a harness command recipe, but does not invoke that command, install a
harness, or claim its required commit is installed. After an actual separately
authorized official run, use the existing `official_archive` contract with a
trusted evaluator receipt binding execution, prediction, report and commits.

The model task runner currently evaluates local frozen file goals. It does **not**
automatically provision a SWE repository, expose container write/test tools, run
an autonomous SWE repair agent, or invoke the official harness. Those operations
remain a separate integration stage; data preparation and mock tests do not
establish that stage as implemented or accepted.

## Evidence and remaining work

New tests: suite orchestration 19 distinct checks, dataset/export 10 checks, model
evaluation 9 distinct checks. Tests were run incrementally; the final model-log
audit reran its affected integration test only. Existing full Runtime/Docker
experiments were not rerun. New scripted integration: one core multi-file case
and two Inspect Runtime cases passed. The model-interface negative control
correctly reports one pass and one failure across two mock tasks.

Local evidence (repository-root `.scratch/`):

- `evaluation-suite-tests-20261026-final.xml`: first 16 suite checks.
- `evaluation-suite-dispatch-20261026`: two additional model dispatch/outcome checks.
- `evaluation-suite-preflight-20261026`: artifact rejection before any execution.
- `benchmark-dataset-tests-20261026.xml`: 10 dataset/export checks.
- `model-eval-tests-20261026.xml`: first seven model checks.
- `model-eval-audit-tests-20261026.xml`: affected log integration plus two new checks.
- `evaluation-system-suite-20261026`: real new core/archive/Inspect fixture execution.
- `evaluation-system-consolidated-final-20261026`: six pinned historical/new bundles
  verified and imported, with no model execution. Historical Agent failure stays
  failure; historical gold control stays environment-only; mock negative remains
  failed. All evidence processing succeeded; all-task acceptance is correctly false.

The manual-only `Evaluation System Incremental` workflow is prepared for the three
new test modules on Linux/Windows; it has not run remotely. This successor is local
and has not been added to PR #19. No new live provider or official evaluation has
run. Remaining acceptance work is hosted platform verification and, if desired,
the separate containerized SWE Agent execution stage plus authorized real-model
trials. Do not call the entire autonomous SWE-bench system complete based on this
offline milestone.
