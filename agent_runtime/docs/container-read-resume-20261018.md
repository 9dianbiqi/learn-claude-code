# Container read process recovery — experiment contract

Base: published `df012816ad46e6f4a128e33f61f5048387097e7b`.
Branch: `codex/container-read-resume`. Initial status: **IN PROGRESS**.
Current disposition is recorded in the dated closure below.

## Next push node

The last node established within-process bound Docker reads and deterministic
missing-path advice. This node adds process-death evidence, without extending the
tool capabilities or changing the accepted Runtime baseline.

Seven fresh cases use a controller-owned isolated Docker source and ScriptedModel.
The child controller calls `os._exit(73)` at a selected existing Runtime boundary.
The replacement controller waits for the dead owner's lease to expire, rebinds
the same source identity and resumes from the existing SQLite ledger.

| Case | Death boundary | Child read execs | Recovery read execs | Recovery scripted responses |
|---|---|---:|---:|---:|
| response_saved | persisted model response, before execution checkpoint | 0 | 1 | 1 |
| tool_started | durable running tool, before container execution | 0 | 1 | 1 |
| read_returned | container result returned, before tool result persisted | 1 | 1 | 1 |
| result_saved | tool result persisted, before tool-result message checkpoint | 1 | 0 | 1 |
| stop_saved | final model response persisted, before completion handling | 1 | 0 | 0 |
| advice_saved | missing-path feedback persisted, before feedback event/checkpoint | 1 | 1 | 2 |
| repair_stop_saved | final response persisted under a candidate-patch contract | 1 | 0 | 0 |

Unpersisted reads may execute again. Persisted read/error results must be reused.
This is replay-safe read recovery, not an exactly-once execution claim. The
process-local bridge call limit remains process-local; no durable quota is added.

The first case also attempts three incompatible restore configurations: changed
instance binding, absent adapter, narrowed tool scope. All must fail before lease
acquisition, model responses or container read dispatch. A same-named host sentinel
must never enter model requests, tool outputs or source operations.

Six completion goals concern a historical observation of fixture bytes. Their
existing Verifier checks the successful bound read record. They do not assert
that a later mutable workspace still has those bytes. The seventh uses the
existing Docker WorkspaceVerifier and candidate-patch CompletionContract: an
unchanged source must terminate UNRESOLVED_STOP_LIMIT with no authoritative
completion checkpoint and no validation command run.

## Evidence required

1. New synthetic persistence-boundary regressions and affected compatibility tests.
2. Seven real Linux/Docker process-exit cases, including replacement-owner fencing.
3. Per-phase model request/response snapshots, Runtime Trace/database, read nonce
   and exec records, pre-resume state, exact source hashes, errors and cleanup.
4. Incremental Standards and Spec review; no unresolved blocking finding.
5. Final-tree Windows/Linux compatibility checks if implementation changes warrant.

Only this experiment code, its regressions and this report are in scope initially.
No model network, dependency installation, image pull/build, SWE-bench Harness,
commit or push is part of the experiment. Real model tokens/API calls and official
Harness runs must remain **0**. No Agent patch or SWE-bench success is claimed.
Completed checks from the previous node are references, not rerun evidence.

## Execution and repair record — 2026-10-18

The first `response_saved` case, including three negative restore configurations,
passed in `container-read-resume-docker-20261018-01`. The next batch passed
`tool_started`, then stopped on `read_returned`: its replacement owner's two-second
lease expired before read reconciliation. Runtime raised LeaseLost before dispatch;
this is a preserved failed attempt, not a recovery success. Both sources were
cleaned. The batch did not execute its remaining cases.

Independent Spec review also found a report defect: a cleanup exception could save
an overall PASS before cleanup was confirmed. The runner now records protocol
status separately. Overall PASS requires deletion, ownership inventory and client
closure to succeed; failures record their stage/type/reason without hiding the
original protocol error. Ten new fault-injection cases cover remove failure,
inventory failure, remaining resources, close failure and normal cleanup, each
with both successful and failed protocol outcomes. All ten passed on Windows.

The new runner uses a ten-second experimental lease and waits at most fifteen
seconds for natural expiration. Production Runtime, tool bridge and permission
code were not changed. The five failed/unexecuted windows then passed in
`container-read-resume-docker-20261018-03`. Its source-index check completed.
The original successful two windows were not rerun. These are **seven covered
windows across two runner revisions/configurations**, not seven identical-version,
identical-TTL replications. The original failure and pre-repair source are retained
in `container-read-resume-docker-20261018-02` and
`container-read-resume-prerepair-source-20261018`.

Observed accepted-case totals: **15 ScriptedModel responses**, **8 Runtime tool
records**, **9 real container read execs**. The extra read is the deliberate replay
of a result not yet persisted. These totals exclude the failed short-lease attempt;
that attempt remains separately auditable. All accepted cases acquired a new
owner/fencing token, returned only bound-source bytes and cleaned their owned
containers. Final stops reused saved responses with zero replacement model calls.
Persisted missing-path feedback reached the resumed model without repeating the
failed container read. No Patch was generated. The repair-contract case failed
UNRESOLVED_STOP_LIMIT, with no authoritative completion and no validation command.

Windows before the report repair: **54 passed, 6 Linux-only skipped** across the
new seven replay cases and the existing bridge/contract tests. After the report
repair: **10 passed, 7 deselected** for the new cleanup injections. These are
separate runs, not a fabricated single full-suite result. Linux final-source new
regression and final review results are recorded below after completion.

The first code reviews returned Standards: zero hard violations and one
nonblocking maintenance suggestion (replace positional case tuples with named
fields). Spec: REJECT pending report repair; subsequent limited re-review accepted
the correction with notes. Neither was relabeled a complete runtime benchmark.

Evidence remains outside Git under `.scratch/`. This node adds only the experiment,
its tests and this report. No target-model API, real model token consumption or
official Harness runs occurred; all remain **0**. It is not evidence of autonomous
code repair, SWE-bench resolution, a durable global read-call quota or strict
exactly-once execution.

## Gate closure — 2026-10-18

Final-source Linux new regressions: **17 passed, 0 skipped**, 52.72 seconds.
The post-run ownership audit covers all eight attempt sessions, including the
failed short-lease attempt: **0 source containers remaining**. No image was
pulled/built and no dependency was installed.

Current status: **LOCAL NEXT-PUSH READY — ACCEPT WITH NOTES**.
Standards final: **PASS WITH NOTES**, zero hard violations, one nonblocking case
tuple maintenance suggestion. Spec final: **ACCEPT WITH NOTES**, the report
blocker is closed and no other blocker was identified. The sealed evidence
index is recorded in the external node-state/report. The accepted scope remains
these seven covered process-recovery windows across the explicitly recorded
runner revisions; no claim of a new complete final-tree Runtime CI suite is made.

Local candidate artifacts: `container-read-resume-ready-evidence-20261018.zip`
and its SHA-256 sidecar under `.scratch/`. The bundle preserves both runner
revisions, every run directory (including the failed attempt), per-phase request
and read records, databases/Trace, source indices, test logs/JUnit, review findings
and a candidate patch for precisely the three new files. Nothing was committed or
pushed during this experiment; previous published branches and Phase 2 baseline
remain unchanged. Remote acceptance requires a future authorized push and its CI.
