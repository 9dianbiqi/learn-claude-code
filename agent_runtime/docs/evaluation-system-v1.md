# Offline evaluation system v1 — acceptance contract

Date: 2026-10-22. Base: ba5a242019ec411e5138fa5ba028b529dd66c546.
This successor experiment does not change the frozen Phase 2 source, accepted
read-budget candidate, Runtime completion/Verifier semantics, or historical runs.

## Scope and acceptance

The executable entry point is:

```sh
python -m agent_runtime.evaluation PLAN.json NEW_OUTPUT_DIRECTORY
```

This milestone must:

1. Freeze a JSON plan and digest-pinned inputs before any tools run. Reject empty
   plans, duplicate cases/archived subjects, corrupt hashes, unsafe paths, links,
   unsupported modes and overwrite of prior output. Plan authors are trusted.
2. Execute bounded, new scripted Runtime cases with explicit status/file oracles,
   preserving traces, model-call records and fresh invariant-scan results. Restrict
   scripts to relative local read/write/edit tools. Keep write safety and lease
   rules. Inject a crash and resume once; a missed fault must fail its oracle
   without starting a second task. No model provider or shell is available here.
3. Import historical Agent archives without executing Runtime or scanning their
   invariants. Validate task/call identity and counts. Preserve missing metrics as
   null, including partial/absent provider usage; scripted usage is never provider
   usage. Failed/abandoned model calls remain counted. Empty repair patches imply
   task failure; a nonempty patch alone leaves success unknown.
4. Import pinned official per-instance reports through an evaluator-owned receipt
   binding source run, instance, scope, dataset revision, base/harness commits,
   report, prediction and execution digests. Execution run identity must match;
   an old empty-patch report cannot become a new evaluation. Never recalculate
   resolved from pytest output. Missing or inconsistent evidence stays unknown.
5. Report Runtime oracle results, Agent outcomes and historical gold controls in
   separate denominators. No cases yields null, not 100%; any unknown prevents a
   complete success-rate assertion. Processing errors stay in planned totals.
6. Write raw evidence copies, structured JSON, readable Markdown, and a SHA-256
   seal of output files. Never rerun or replace an existing bundle. CLI exit 0
   means evidence processed, not that the Agent passed; exit 1 means case evidence
   processing failed; invalid plan/input is exit 2.
7. Verify with integrity/fault-injection regression tests, an executable local
   example, real historical Agent and gold-control imports, and independent
   Standards/Spec review. No new paid calls or official evaluations are authorized.

The implementation adapts concepts from the historical trace-metrics,
failure-taxonomy and SWE-bench adapter contracts. It does not import teaching
scripts or install another Verifier. Existing `eval_runner` CLI/suites retain
their historical semantics; the new entry point is deliberately separate.

## Plan format

Top-level fields: `schema_version: evaluation-plan-v1`, `run_id`, `cases` (1–64).
The repository example `agent_runtime/evals/evaluation-v1.json` is directly runnable.

Each case has a portable identifier and one mode:

| Mode | Required additional fields | Evidence |
| --- | --- | --- |
| scripted_runtime | model, checks.status | optional files, checks.files, fault_point, recovery_model |
| agent_archive | source_run_id, instance_id, task_id, model_mode live/scripted | artifacts.summary, model_calls, prediction |
| official_archive | source_run_id, instance_id, scope gold_control/agent, dataset_revision, base_commit, harness_commit | artifacts.report, prediction, execution, receipt |

Artifact references are `{ "path": "relative/file", "sha256": "64 hex digits" }`,
relative to the plan directory. Evidence is evaluator input only; gold patches
are never copied into a scripted solver workspace. JSON inputs reject duplicate
keys and nonfinite constants. A file is at most 16 MiB; combined archive inputs
are at most 64 MiB. Each scripted case is at most 64 KiB of serialized JSON.
Scripts have at most 32
configured responses including recovery; exhausted scripts terminate through the
existing Runtime failure path, not by generating new responses. No automatic
recovery beyond the single injected crash is performed.

The receipt repeats the six official identity fields, plus `report_sha256`,
`prediction_sha256`, `execution_sha256`. Its origin is a trusted evaluator, never
solver self-attestation. `execution` must include `run_id` and integer `exit_code`
(or legacy `process_exit`). Successful per-instance reports require official
boolean resolved, patch application fields, and tests_status. Official aggregate
empty_patch_ids establishes empty-patch handling only; it leaves resolved null.

Raw archive copies may contain issue text, code or provider responses. Supply
sanitized, evaluator-owned exports, never authentication material. Digest pins
detect mutation, not deliberate fabrication of a complete consistent archive.
The evaluator is an offline tool for a trusted local owner, not a hostile-input
sandbox or signature/notarization service; concurrent filesystem tampering is
outside its threat model. Output is append-only by policy, not filesystem WORM.

## Result interpretation and remaining boundaries

`processing_ok` means parsing and evidence checks succeeded. A properly recorded
Agent failure is a valid evaluated result. `runtime_case_pass` only describes a
scripted regression oracle. `task_success` and `official_resolved` are independent
of Runtime status. Gold-control success is environment evidence, never an Agent
score. Imported runs are always historical; this command reports zero new model
requests and zero new official evaluations.

An official Agent archive and a raw Agent archive for the same source run/instance
must not be separate cases; choose the strongest complete evidence for that
subject to avoid inflating the denominator. This v1 does not merge partial cases,
schedule live models, run Docker/harness, estimate money, or claim a statistically
meaningful resolved rate from a single task. Live experiments remain a separately
authorized adapter stage with frozen budgets and input isolation.

Failure categories in v1 are `runtime_oracle`, `task_semantic`,
`environment_setup`, `evidence_invalid`, and `none`. These describe available
evidence only; transport/rate-limit failures cannot be inferred from absent data.
No unknown legacy token, latency, invariant or test count is replaced with zero.

Local system acceptance does not supersede the previously published Windows CI
failure or imply remote release acceptance, Phase 2 universal correctness,
exactly-once execution, or demonstrated autonomous SWE-bench repair competence.
