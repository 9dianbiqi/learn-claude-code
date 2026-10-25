# Completion contracts and deterministic recovery advice — successor prototype

Date: 2026-10-17. This is an uncommitted successor to accepted Phase 2 commit
`68e4e51ca9cc4049f575d617762f89934e443baa`, on branch
`codex/completion-recovery-contract`. The accepted checkout, old evidence and
benchmark results are unchanged. This document is not a new acceptance decision.

## Scope and order

1. Add an opt-in task contract and a shared model-stop decision seam.
2. Adapt existing Verifier authority to a separate workspace's evidence.
3. Add deterministic recovery advice for trusted typed read errors.
4. Verify offline execution, resume and real-process fail-stop behavior.
5. Specify A/B/C/D live comparison; do not dispatch it in this implementation.

No real model, Docker instance, official SWE-bench harness or paid API is called
by these tests. Fixture official reports are synthetic inputs, not SWE-bench scores.

## Three meanings of completion

* Model turn finished: the provider produced a text-only stop.
* Execution terminated: the durable Runtime Task reached a terminal state.
* Goal verified: the task's frozen Verifier passed with current evidence.

Legacy unconfigured Runtime instances keep their current behavior, including
ordinary chat stops and existing verified-subtask protocols. New contracts are
explicit opt-ins. They require exactly one final frozen DAG node and exclude
replanning/child-agent tools in this first version. A normal text stop asks the
existing Verifier to decide; a completion marker is not required for this path.

`Runtime._finish_model_turn` handles both a new stop and a recovered stop.
Success still goes exclusively through `_handle_verified_marker` and
`EventStore.commit_verified_subtask`: verifier run, PlanItem completion,
verified-subtask checkpoint, execution checkpoint and final Runtime Task state
retain their existing crash-consistent commit. There is no second success ledger
or post-commit call to `complete_task`.

The bootstrap execution checkpoint stores the completion/recovery profile,
contract version, DAG hash, workspace/validator identity and advice limits.
Resume and execution entry check that profile before model/tool dispatch. Removing
or changing it cannot restore the ordinary unverified completion shortcut.
Implementation versions and the existing verifier implementation/bundle hashes
must be updated when deploying changed semantics; this prototype does not provide
an automatic cross-version migration of already-created contract tasks.

## Contracts and finite termination

`CompletionContract` supports:

| Type | Success authority |
|---|---|
| verified_task | Existing task-specific Verifier; no universal file-change requirement |
| candidate_patch | A scoped WorkspaceVerifier: nonempty applicable patch and trusted test evidence for that exact revision |
| swebench_repair | The same adapter plus matching official dataset/harness identities and official_resolved=true |

Local test success never satisfies `swebench_repair`. No authorization or no
official result yields `uncertain`, not a substituted local pass.

The existing database status enum is unchanged. Non-success executions use the
existing atomic PlanItem/Plan/Runtime Task failure transaction. A JSON error and
checkpoint cursor carry distinct reasons:

* `AGENT_GAVE_UP`: final standalone `TASK_GAVE_UP` declaration.
* `NEEDS_INPUT`: final standalone `TASK_NEEDS_INPUT` declaration.
* `UNRESOLVED_STOP_LIMIT`: bounded failed completion checks exhausted.
* `VERIFICATION_UNCERTAIN`: unavailable, malformed or mismatched validation.
* `BUDGET_EXHAUSTED`: existing durable DAG model-turn budget exhausted.
* `EVIDENCE_STALE`: recovery invalidated previously committed workspace evidence.

These codes are persisted with the failure checkpoint/state/event, not in an
independent mutable outcome file. `NEEDS_INPUT` is a distinguishable terminal
request for clarification in v1; a nonterminal waiting-input workflow would need
separate schema/UI design. It must not be confused with opaque-effect needs_review.
Ordinary chat is not forced into a patch/test contract.

There are at most 1–3 completion checks (default 2), and the frozen DAG's
`max_turns` bounds all model responses. The stop-check count uses persisted
execution-checkpoint identities, so restart cannot replenish feedback. Failed
verification already recorded for a recovered stop is reused rather than
dispatched again. `uncertain`, explicit give-up/input and exhausted limits stop
without forcing another model response. Non-success must not be counted as a
repair failure verdict when the actual reason is unavailable validation/input.

## Workspace evidence seam

Current pilots place the Runtime database under a controller root and execute
code in a separate container. Hashing only the controller directory cannot prove
that a test report covers the current solver code.

`WorkspaceVerifier` implements the existing callable Verifier interface using
three **trusted backend** operations:

* `freeze()` returns a scope that prevents participating workspace writers.
* `capture()` returns workspace ID, instance, base, current revision, exact patch
  and an applicability result against that base.
* `validate(snapshot)` returns trusted command/evaluation evidence bound to the
  captured revision and patch hash, with the frozen validator identity.

The Runtime keeps the verification scope open through evidence capture, SQLite
commit and post-commit recovery check, not merely through the verifier callback.
The adapter compares capture before/after validation, checks identity/revision,
patch/applicability, executed-test count/exit and official provenance where needed.
It atomically writes proof under the model-denied
`.agent_runtime/verification/completion.json`, then returns its manifest through
the existing `VerifierResult` type.

`VerifiedEvidenceRecovery` also asks this adapter whether the external workspace
still matches the proof before returning cached completion. Controller hashes
alone are insufficient. Changed external state triggers the existing refresh or
invalidation path; failed revalidation becomes `EVIDENCE_STALE` without another
model call in this v1 contract policy. Missing or changed backend identity cannot
silently attach to a previously completed contract.

### Deployment limit

The implementation supplies the verification adapter and backend seam, **not a
new production Docker freeze/export/test worker**. Tests use real files outside
the controller plus a cooperative freeze and synthetic validation records.
The cooperative fixture is not an OS sandbox or proof against arbitrary writers.

Follow-up: a concrete offline Docker backend and five real-container fixture
scenarios now exist; see [the backend round](docker-workspace-backend-20261017.md).
That round retains deployment limits and does not certify a production SWE runner.

Before a live rollout, the backend must demonstrate actual container ownership,
quiescence through commit, a revision covering all relevant changes/new files,
real patch applicability, trusted bounded test execution and resume behavior when
the container is lost. Its script/command identities belong in the frozen verifier
bundle. It must not read a model-written "passed" JSON as authority.
Long-running external validation also needs bounded cancellation and lease
heartbeat/job coordination; this synchronous seam does not stop a malicious or
unbounded Python callback. Lease/fencing still prevents a stale owner committing.

## Recovery Advisor

`RecoveryAdvisor` is opt-in and has no model client. It responds to actual
`FileNotFoundError` from built-in read_file or a trusted read-only adapter's
`RecoverableReadError`. An arbitrary Shell stderr containing "not found" is not
trusted and keeps the normal opaque-effect review behavior.

Feedback includes PATH_NOT_FOUND, canonical relative failed_path,
same_arguments_retryable=false, task_recoverable=true and abstract suggestions
to inspect repository structure, search filenames and search symbols. It never
executes those suggestions or retries the failed tool itself.

Advice is limited to 1–3 unique paths (default 2). Repeated paths are identified;
their suggestions are not re-emitted. Counters derive only from trusted persisted
failed read records, not JSON content read from model-controlled files. A cached
read-failure output can be replayed after a crash without executing the read again.
Subsequent Agent calls still consume the original model budget.

The old pilot exposes only bash. A live bash bridge needs to produce typed read
errors from a trusted path observation before it can use this advisor; this
prototype deliberately does not upgrade opaque Shell errors by parsing stderr.

## Validation and known compatibility findings

New offline tests cover:

* Ordinary chat and verified no-edit tasks; text-only stop without proof.
* Finite correction, distinct give-up/input/uncertain/budget termination.
* Repeated paths, advice quota, forged JSON and opaque write/Shell review.
* Missing validation, wrong revision/patch/validator, external workspace drift,
  protected proof files and source configuration mismatch on resume.
* Lease takeover during verification; workspace scope held through commit.
* Real os._exit at three existing successful authority boundaries and three new
  failure-transaction boundaries; failed advice persisted before the user checkpoint.

Affected compatibility checks target existing verified-subtask, DAG, stale-evidence
and F3 message tests, not old model/benchmark experiments. Two legacy DAG cases
also fail on the unchanged accepted source in a read-only two-case control:

* test_blocked_completion_marker_gets_fixed_feedback_without_verifier_run
* test_blocked_marker_can_continue_after_dependency_becomes_complete

Both encounter `in_progress has a current valid checkpoint` after their fault
hook directly rewrites a previously completed dependency to pending. They are
reported, not changed, skipped or relabeled as passes. Thus no all-suite-green
or new independent acceptance claim is made. Final counts and code hashes belong
to the saved test evidence accompanying this checkout.

Final implementation-tree result: **31 new tests passed**, including **7 real
process-exit cases**. Selected compatibility tests: **60 passed, 2 failed**;
the same two cases failed in the unchanged baseline control. Full new/compat
logs and JUnit XML are saved; the earlier baseline-control console observation
is recorded as a derived summary, not misrepresented as a newly captured raw log.

## Deferred live A/B/C/D comparison

| Group | Advisor | Completion contract |
|---|---|---|
| A | off | off, legacy model-stop semantics |
| B | on | off |
| C | off | on, shared Verifier authority |
| D | on | on |

Keep the same task, clean base, model/version, temperature, initial shared problem
context, tool permissions and total per-run token/request/time budget. Keep the
already-built read-only error recovery capability enabled identically across
groups. Record necessary contract instructions as part of each frozen condition;
do not claim all prompt bytes are equal when the intervention adds feedback.

First use deterministic traces to check state/feedback behavior; replayed model
responses cannot establish improved model competence. Only after review should
new matched live attempts be budgeted. Repetitions/model capacity and total
budget require a separate freeze; do not repurpose historical A runs as matched
controls or reset earlier reservations silently.

Metrics: first valid implementation-file read, actual strategy switch after an
error (a second command on the same absent path is not a switch), unsupported
success termination, applicable nonempty candidate rate, agent-initiated tests,
verifier-initiated tests separately, targeted repair after test failure, true
official resolved, tokens/tool calls, give-up/uncertain/infrastructure/missing cells.
Nonempty patch and local test pass remain intermediate evidence. Actual versus
cached official results must remain distinct. Do not dispatch live model calls or
official grading from the offline test suite or this document.
