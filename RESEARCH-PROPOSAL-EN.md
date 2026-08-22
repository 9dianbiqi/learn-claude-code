# MSc Dissertation Research Proposal

## Crash-Consistent Checkpointing of Verified Subtasks for Interruption-Resilient Recovery in Long-Horizon Coding Agents

**Version:** v1.1  
**Status:** For supervisor review and scope approval

## Abstract

Long-horizon coding agents complete sequences of interdependent subtasks through repeated model reasoning and tool use. Existing durable runtimes can persist messages, tool calls, file effects, and low-level execution checkpoints. However, they do not necessarily atomically record that a meaningful subtask was objectively verified, the evidence supporting that transition, and the exact durable execution state to which it belongs. A fail-stop interruption between verification and persistence can therefore leave ambiguous task-level state, while full-history recovery may repeat already completed work or trust stale evidence.

This study proposes a minimal crash-consistent verified-subtask checkpoint layer for an existing local coding-agent runtime. After a predefined verifier passes, the verifier run, evidence digest, subtask-completion transition, verified-subtask checkpoint, runtime-checkpoint linkage, and audit event are committed atomically in one SQLite transaction. On restart, an authoritative bundle is visible either in full or not at all; incomplete transactions leave the subtask incomplete, and stale evidence triggers re-verification. A scoped-recovery condition supplies the model only with the next subtask, required dependency summaries, valid evidence, recent failures, and relevant file state.

The study will compare three conditions on a frozen suite of multi-step coding tasks under reproducible fault injection. A/B/C will use byte-identical static subtask DAGs, completion criteria, verifier implementations, versions and invocation schedules, and final acceptance tests. The expected primary effects are lower post-restart input-token cost, repeated work, and resume-to-completion latency. State-consistent final success is a pre-registered non-inferiority guardrail; no success-superiority effect is assumed. The intended contribution is a controlled empirical systems study, not a new task-decomposition algorithm, verified external task-state framework, foundation model, or multi-agent architecture.

## 1. Background

A basic coding-agent loop can be represented as:

```text
model reasoning → tool call → environment result → next model response
```

In longer tasks, this loop spans semantically meaningful and interdependent subtasks, such as understanding an existing codebase, modifying an implementation, adding tests, resolving integration failures, and updating documentation. Interruptions may occur after a model response, after a file effect, after a test run, or between subtask boundaries.

The existing `agent_runtime` already provides SQLite WAL persistence, execution checkpoints, an append-only event log, tool-call deduplication, file-effect reconciliation, fault injection, traces, and deterministic evaluation. The separate `s12_task_system` demonstrates a persistent subtask graph with `blockedBy` dependencies. These components do not yet form a unified semantic-recovery mechanism: the runtime knows where low-level execution stopped, but it does not directly know which subtasks have been objectively verified as complete.

The study therefore does not ask whether checkpoints should be persisted. It asks:

> How can objectively verified subtask progress be atomically bound to durable execution state so that a coding agent can recover efficiently from a fail-stop interruption without trusting partial or stale task-level state?

## 2. Problem Statement and Research Gap

Execution-level checkpoints are suitable for restoring messages, model calls, and tool calls, but they leave four task-level problems:

1. **Completion is ambiguous.** A model's completion claim does not prove that the code or environment satisfies the subtask requirements.
2. **Recovery scope is ambiguous.** Although the full conversation can be restored, the model must infer again what has been completed and what should happen next.
3. **Evidence may become stale.** A subtask may have passed a test earlier, but subsequent or external changes may invalidate that evidence.
4. **Verification and persistence may be torn by a crash.** A verifier may pass before its evidence, completion transition, and runtime-checkpoint linkage are durably committed.

[LongHorizon-Harness](https://arxiv.org/abs/2608.01964) is the strongest nearest work because it already maintains explicit external task state, advances it from independent environment audits, and uses fresh-context execution. Therefore, verified external task state, verifier-gated progress, independent auditing, and scoped or fresh-context execution are not claimed as novel here. The remaining systems-level question is narrower: atomically linking verifier evidence, a verified-subtask transition, and a durable runtime checkpoint under fail-stop interruption; rejecting stale evidence after restart; and measuring recovery cost without materially degrading final task correctness.

In this study, an **interruption** is a fault-injected single-process fail-stop event. **Crash consistency** means that authoritative semantic metadata in SQLite is visible after restart either as a complete committed bundle or not at all. The claim excludes power loss, storage or database corruption, network filesystems, distributed failures, and generic exactly-once semantics for external effects; file and tool effects continue to rely on the runtime's existing reconciliation layer.

## 3. Research Objectives

The study aims to:

1. define a crash-consistent verified-subtask checkpoint bundle linking verification evidence, subtask state, and durable runtime state;
2. implement an atomic verifier-gated commit whose authoritative state is visible either completely or not at all after restart;
3. implement evidence revalidation and scoped recovery from the latest valid committed bundle;
4. construct a reproducible task suite in which A/B/C use identical static DAGs, completion criteria, verifiers, and logical fault locations; and
5. evaluate recovery input tokens, repeated work, and latency as primary efficiency outcomes while treating state-consistent final success as a non-inferiority safety outcome.

## 4. Research Questions and Hypotheses

### 4.1 Primary Research Question

> Under controlled fail-stop process interruptions, does crash-consistent checkpointing of verified subtasks with scoped recovery reduce post-restart input-token cost, completed-work re-execution, and resume-to-completion time while remaining non-inferior to full-history execution checkpointing in state-consistent final success?

### 4.2 Secondary Questions

1. Does a durable verified-subtask ledger reduce completed-work re-execution and redundant tool activity relative to execution-only checkpointing?
2. With durable verified state held constant, does scoped recovery further reduce post-restart input tokens and latency relative to full-history recovery?
3. Do injected crashes around the verification/commit boundary ever expose a partially committed authoritative bundle?
4. Does restart reject or re-verify evidence that has become stale?
5. What overhead and new failures do verification, persistence, and context scoping add to uninterrupted execution?

### 4.3 Working Hypotheses

| ID | Hypothesis |
|---|---|
| H1: primary efficiency | Condition C reduces post-restart input-token cost, completed-work re-execution, and resume-to-completion latency relative to Condition A |
| H2: safety | Condition C is non-inferior to Condition A in state-consistent final success under a pre-specified absolute margin, provisionally Δ = 0.10 (criterion: C−A > −0.10); no success-superiority claim is assumed |
| H3: mechanism ablation | A→B primarily reduces repeated work, while B→C primarily reduces recovery input tokens and may further reduce latency |
| H4: integrity | Under pre-commit crashes and stale-evidence tests, B/C expose no partial authoritative bundle and never skip a subtask on invalid evidence |

## 5. Literature Foundation and Positioning

| Work | Concept adopted | Boundary of this study |
|---|---|---|
| [LongHorizon-Harness](https://arxiv.org/abs/2608.01964) | Explicit external task state, independent environment auditing, and fresh-context execution | **Strongest nearest work.** This study does not claim these mechanisms as novel; it focuses on atomic verifier-to-subtask-to-runtime-checkpoint linkage, fail-stop restart, stale-evidence validation, and recovery-efficiency evaluation |
| TDP | Static task DAGs, scoped context, graph maintenance, and local recovery | DAGs, context scoping, and local replanning are not claimed as novel; the DAG is frozen here for causal control |
| HiAgent | Detailed context for the active subgoal and summaries for completed subgoals | Used to design scoped recovery; the complete working-memory framework is not reproduced |
| δ-mem | Motivation for compact state that is updated over time | Used as online-memory context only; this study uses explicit external state and does not modify model parameters |
| APB | Diagnostic separation of planning, execution, and verification failures | Used for failure analysis; APB is not used as the experimental benchmark |

The novelty claim is limited to crash-consistent integration and controlled interruption-recovery evaluation. The project does not claim the novelty of verified external task state, independent auditing, fresh-context execution, task DAGs, or local replanning.

## 6. Proposed Method

### 6.1 Checkpoint Layers

| Checkpoint | Primary content | Question answered |
|---|---|---|
| Execution checkpoint | messages, phase, cursor, and model/tool state | Where did low-level execution stop? |
| Verified-subtask checkpoint | atomically committed subtask, dependencies, summary, evidence, and linked runtime checkpoint | Which subtask was verified and completely committed, and where should task-level recovery continue? |

### 6.2 Subtask Representation

The first version uses a frozen static subtask DAG to preserve experimental control; the same DAG is used in A/B/C:

```text
subtask = {
  id,
  goal,
  blocked_by,
  relevant_paths,
  completion_criteria,
  status,
  completion_summary
}
```

The state transition is restricted to:

```text
pending → in_progress → verifying → completed
                            └──────→ failed
```

A subtask may enter `completed` only after verifier success.

### 6.3 Verified-Subtask Checkpoint Record

```text
semantic_checkpoint_id
commit_transaction_id
agent_task_id
subtask_id
subtask_status
dag_hash
dependency_snapshot
completion_summary
verification_type
verification_rule
verification_result
verifier_id
verifier_version
verifier_bundle_hash
evidence_manifest
evidence_hash
runtime_checkpoint_id
created_at
```

The first implementation supports the following verifier evidence:

- unit or integration test results;
- lint or type-check results;
- file-existence and content assertions; and
- file SHA-256 or related state hashes.

### 6.4 Crash-Consistent Verifier-Gated Commit

```text
execute current subtask
  → run predefined verifier
      → pass: BEGIN transaction → write verifier run, evidence digest,
              completed transition, verified-subtask checkpoint,
              runtime-checkpoint link, and audit event → COMMIT
      → fail: keep subtask incomplete and return failure evidence
      → uncertain: fail closed; keep the subtask incomplete
```

A natural-language completion claim from the model is not sufficient evidence. If the process stops before `COMMIT`, none of the bundle becomes authoritative and the subtask is re-verified after restart. If it stops after `COMMIT`, the complete bundle is visible. The tested invariant is:

```text
completed subtask
  ⇒ matching successful verifier evidence
  ∧ linked runtime checkpoint
  ∧ one committed transaction for the authoritative bundle
```

### 6.5 Scoped Recovery

After interruption, the system:

1. restores the durable runtime checkpoint;
2. locates the latest committed verified-subtask checkpoint;
3. checks the verifier version and revalidates the evidence manifest and hash;
4. selects the next incomplete subtask whose dependencies are satisfied;
5. in Condition C only, loads that subtask, required dependency summaries, valid evidence, recent failures, and relevant file state; and
6. resumes execution while retaining the complete event log for audit.

## 7. Experimental Design

### 7.1 Experimental Conditions and Causal Control

All three conditions execute the same frozen DAG, follow the same subtask-selection order, and invoke the same verifier at the same boundaries. The verifier result is returned to the agent in every condition; only its authoritative persistence and the post-interruption context policy differ.

| Condition | Authoritative verified-subtask state | Post-interruption context | Purpose |
|---|---|---|---|
| A. Exec-Full | Verifier output remains ordinary persisted execution history; no first-class durable subtask ledger | Complete persisted message and tool state | Execution-checkpoint baseline |
| B. Verified-Full | The same verifier result is atomically linked to the runtime checkpoint in a durable ledger | Complete persisted message and tool state | Isolate durable verified state |
| C. Verified-Scoped | Identical durable ledger to B | Active subtask, required dependency summaries, valid evidence, recent failures, and relevant paths | Isolate scoped recovery |

The pre-registered primary comparison is A versus C. A→B isolates crash-consistent durable verified state, and B→C isolates context scoping; all three conditions are therefore required. Scoped context is enabled only after interruption in C, so normal execution context is not an additional treatment. The evaluation harness must not persist a queryable subtask-completion ledger for A.

Within each matched A/B/C triplet, the starting commit, task prompt, static DAG and dependency edges, subtask boundaries, completion criteria, verifier implementation/version/evidence inputs/invocation schedule, feedback content, final acceptance tests, model configuration, tools, budgets, and logical fault location are identical. Each run records `dag_hash` and `verifier_bundle_hash`; mismatched triplets are invalid.

### 7.2 Operational Definition of a Long-Horizon Coding Task

A task in this study must:

- contain 4–6 semantically meaningful dependent subtasks;
- modify or inspect at least two code or test files;
- require multiple model–tool interaction rounds;
- provide local verifiers and independent final acceptance tests; and
- require more than a single straightforward file write.

### 7.3 Task Suite

The study will freeze 8–12 small repository-level coding tasks covering:

- modifying an implementation and adding regression tests;
- adding configuration validation and integrating it with a CLI;
- repairing a parser and updating related tests;
- implementing a cross-file feature; and
- combined code, test, and documentation changes.

Before formal evaluation, each task will freeze its starting commit, task specification, static subtask DAG, allowed tools, turn limit, local verifier implementation and version, evidence inputs, invocation schedule, final acceptance tests, and logical fault-injection points.

### 7.4 Fault Injection

| Type | Interruption point | Purpose |
|---|---|---|
| F0 | No interruption | Measure uninterrupted-run overhead |
| F1 | A file effect occurred but its tool result is not persisted | Negative control for the existing effect-reconciliation mechanism |
| F2 | The verifier returned `pass`, but condition-specific persistence has not completed | Test the all-or-none crash-consistency invariant and re-verification |
| F3 | The matching verified boundary is durable, but the next subtask has not started | Measure the main recovery-efficiency and repeated-work effects |
| F4 | Evidence-related files change after persistence and before restart | Test stale-evidence rejection or re-verification |

For B/C, F2 covers hooks immediately before the transaction, after its writes but before `COMMIT`, and immediately after `COMMIT`; A uses the equivalent logical verifier boundary in its ordinary execution history. Existing model-response-boundary tests remain runtime regressions rather than dissertation main effects.

### 7.5 Metrics

**Primary efficiency outcomes:**

1. Post-Restart Input-Token Cost: model input tokens from resume to termination;
2. Repeated Work: repeated canonical tool calls, repeated or no-op file effects, and re-execution or re-verification of completed subtasks; and
3. Resume-to-Completion Latency: wall-clock time from restart to completion or failure.

**Non-inferiority safety outcome:**

4. State-Consistent Final Success: all independent final acceptance tests pass after interruption, with no unresolved effect, incorrect subtask state, or durable invariant violation. C versus A uses a provisional absolute non-inferiority margin Δ = 0.10.

**Integrity outcomes:** partial authoritative-bundle exposure (target: zero), stale-evidence detection or re-verification, and durable-state invariant violations.

**Secondary outcomes:** output tokens, model and tool calls, checkpoint count, verifier errors, and uninterrupted-run overhead.

### 7.6 Failure Taxonomy

- planning failure: incorrect subtasks or dependency relationships;
- execution failure: failed tool use or code modification;
- verification failure: false pass, false rejection, or insufficient evidence;
- recovery failure: incorrect resume point, repeated effect, or omitted unfinished work; and
- integration failure: local verifiers pass but final acceptance fails.

### 7.7 Analysis

- use matched A/B/C comparisons for each task and repetition;
- aggregate repetitions within task and treat the task, not individual runs, as the primary generalisation unit;
- report paired estimates and task-clustered bootstrap intervals for input tokens, repeated work, and latency;
- assess non-inferiority from the confidence interval for C−A: claim non-inferiority only if its lower bound exceeds −0.10; otherwise report that non-inferiority was not established;
- do not interpret a non-significant success difference as equivalence or superiority;
- report atomicity and stale-evidence invariants separately from statistical efficiency outcomes;
- retain positive, negative, and null results; and
- analyse representative successful and failed traces qualitatively.

## 8. Reproducibility and Contribution Boundaries

- Freeze starting commits, model version, temperature, prompts, turn limits, and fault points.
- Freeze the endpoint hierarchy and non-inferiority margin before formal runs.
- Assert matching `dag_hash` and `verifier_bundle_hash` for every A/B/C triplet.
- Keep pre-interruption execution behaviour identical; only authoritative persistence and post-restart context may differ.
- Freeze tasks and scoring rules before formal runs.
- Do not expose final acceptance tests directly to the model.
- Preserve raw traces, checkpoints, events, and processed results.
- Record the upstream `learn-claude-code` commit, pre-existing `agent_runtime`, and dissertation-specific additions separately.
- Do not selectively remove failed tasks.
- Do not present scripted-model results as real-model evidence.

## 9. Scope

### Included in the MVP

- a static subtask DAG;
- crash-consistent verified-subtask checkpoints;
- scoped recovery;
- reproducible fault injection;
- the required matched A/B/C design (A versus C primary; A→B and B→C mechanism ablations); and
- 8–12 multi-step coding tasks.

### Excluded from the MVP

- training, fine-tuning, or reinforcement learning;
- dynamic `MERGE`, `REMOVE`, or `REORDER` operations;
- multi-agent or parallel worktree execution;
- neural online memory;
- storage/database corruption, distributed failures, and transactional guarantees for arbitrary external side effects;
- distributed runtime and generic exactly-once guarantees; and
- a large general-purpose coding benchmark.

The only optional extension is `REVISE` of the active subtask after verifier failure. Condition B remains part of the core design because it separates durable verified state from context scoping.

## 10. Expected Contributions

1. A precise fail-stop crash model and all-or-none invariant for authoritative verified-subtask checkpoint bundles;
2. a prototype that atomically links verifier evidence, subtask completion, and durable runtime state and revalidates evidence after restart;
3. a matched A/B/C protocol that holds the DAG and verifier constant; and
4. empirical estimates of recovery-efficiency effects, non-inferiority in state-consistent final success, overhead, and failure boundaries.

The contribution will be stated as a controlled systems study, not as a universal planning algorithm or a model-capability breakthrough. It does not include the invention of verified external task state, independent auditing, fresh-context execution, DAG-based decomposition, or local replanning.

## 11. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| The contribution overlaps LongHorizon-Harness | Treat it as the strongest nearest work; exclude external verified state, auditing, and fresh context from novelty claims, and test only crash atomicity, restart, stale evidence, and recovery cost |
| Baseline success is already near a ceiling | Make recovery efficiency the expected primary effect and success a non-inferiority guardrail |
| A/B/C differ in DAGs or verifier strength | Use immutable identical DAG/verifier bundles and reject runs whose hashes differ |
| The task suite is too small for strong non-inferiority inference | Freeze the margin and repetitions, report task-level intervals, and label an insufficient interval as inconclusive |
| “Crash-consistent” overstates the tested guarantee | Limit the claim to injected single-process fail-stop faults and authoritative SQLite metadata; exclude storage corruption and external-effect atomicity |
| Self-constructed tasks favour the proposed method | Freeze tasks before formal runs, hide final tests, and report every task and failure |
| Predefined subtasks reduce open-endedness | Present this as causal control; model-generated DAGs are an optional extension only |
| Local verifiers overlap with final tests | Restrict verifiers to individual subtasks and reserve final tests for system integration |
| Scoped context omits required information | Record summary provenance and count context-omission failures |
| Real-model outcomes are unstable | Fix configuration and determine repetitions in advance from pilot variance and budget |
| Contribution boundaries are unclear | Record upstream, existing runtime, and dissertation additions by file and commit |

## 12. Indicative Work Plan

| Phase | Activity | Completion criterion |
|---|---|---|
| 1. Scope approval | Confirm research question, task scale, and conditions with the supervisor | Approved v1.1 proposal |
| 2. Baseline freeze | Record current runtime behaviour and tests | Condition A is reproducible |
| 3. MVP implementation | Add the verified-subtask ledger, verifier, and atomic checkpoint linkage | All-or-none invariants pass at pre-commit, mid-transaction, and post-commit hooks |
| 4. Scoped recovery | Build the local restart context | Pilot establishes measurable recovery-cost baselines and validates context construction |
| 5. Formal evaluation | Freeze tasks and run matched A/B/C | DAG/verifier hash equality is confirmed; raw traces and result tables are complete |
| 6. Analysis and writing | Statistical summary, cases, limitations, and chapters | Auditable results and chapter drafts |

## 13. Decisions Requested from the Supervisor

1. Is it appropriate to limit the dissertation to long-horizon coding-agent tasks?
2. Are the title and single-process fail-stop crash model sufficiently clear and suitable for an MSc scope?
3. Is the existing execution checkpoint an acceptable baseline?
4. Is recovery efficiency appropriate as the expected primary effect, with state-consistent final success as a non-inferiority guardrail?
5. Are 8–12 frozen repository-level tasks sufficient, or is an external benchmark required?
6. What proportion of the formal evaluation should use a real model?
7. Is a provisional absolute non-inferiority margin of 0.10 acceptable?
8. Does the required A/B/C design sufficiently isolate durable verified state from scoped recovery?
9. Does the positioning against LongHorizon-Harness clearly separate prior mechanisms from the proposed contribution, and should more direct coding-agent recovery literature be added?

## 14. Intended Meeting Outcome

Following supervisor review, the study should freeze:

- the final title and primary research question;
- the MVP A/B/C conditions;
- task count, complexity, and real-model scope;
- verifier types and fault-injection coverage;
- the fail-stop crash model and all-or-none invariant;
- the primary efficiency outcomes and non-inferiority margin;
- the A/B/C DAG/verifier equality rule; and
- required work and the single optional extension.

Large-scale implementation and formal evaluation will begin only after these decisions are confirmed.
