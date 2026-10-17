# C-time engineering screening v1

Scope: 8 synthetic tasks × C/C-time × 2 repetitions = **32 live continuations**.
The earlier 12-task/120-cell method is deferred. Receipt remains inactive.
No runtime changes, no TIME_NOTE changes, no new shell or model-visible tools.
No previously valid model runs or passed old offline tests are repeated.

## Task coverage and prefixes

| ID | Category | Remaining task |
|---|---|---|
| S1 | single-file | closed-interval merging, invalid boundaries and immutability |
| S2 | single-file | config/environment flag precedence and strict types |
| M1 | multi-file | unit conversion plus a consumer with dynamic module binding |
| M2 | multi-file | config validation plus endpoint construction |
| P1 | multi-stage | implement parser → marker/verifier → implement aggregation → verify |
| P2 | multi-stage | implement codec → marker/verifier → wire an API → verify |
| U1 | external-state | repair a previously verified division dependency changed before resume, then implement mean |
| U2 | external-state | refresh evidence after external factor configuration changes, then implement batch scaling |

These are new authored fixtures, not real-repository or SWE-bench tasks. Single-
file tasks modify one application file; a ready.txt fixture marker supplies the
verified preparation boundary. P tasks have two remaining verified stages. No
model is forced to make a mistake; natural failures are recorded, not injected.

To minimize cost, one deterministic scripted prefix per task creates the common
preparation history and an actual `os._exit(73)` at F3. There are no model API
calls in prefixes. The script reads and writes only prep/dependency files,
never solves the remaining target. Reference implementations and both checker
programs remain outside the agent workspace. This is a recovery-state screen,
not a measurement of autonomous prefix generation or long-horizon planning.

All initial files are explicitly UTF-8/LF before freeze (not normalized after
seeing outcomes). Same frozen prefix, original absolute path, task, tool schema,
budgets and verifier are used for both conditions. Retain identity-preserving
file hard links and independent DB copies; only atomic-replace tools are exposed.

U1 replaces arithmetic.py with a deterministic wrong implementation. U2 changes
config.json factor 2→5. Events occur after restore/before resume, use atomic
replacement to detach the link, and record before/after hashes and time.
No other tasks receive an external event. Snapshot contents must remain unchanged.
Report framework evidence refresh separately; do not ascribe pre-model recovery
actions to a model's improved reasoning. Opaque unknown effects are out of scope.

## Five main outcomes

1. **Task success**: Runtime completed AND an independent final semantic checker
   passes AND no verification obligation is missing. Not completed alone.
2. **Recovery input tokens**: provider input plus cache read/creation, all live
   continuation requests. Missing usage is unknown, not zero. Prefix API cost is
   zero because the prefix is scripted; prefix harness turns are distinguished.
3. **Model calls**: actual continuation attempts, including failed requests.
4. **Post-write reobservation**: same committed-effect window rule as the accepted
   receipt pilot, runtime-available evidence only. No receipts exposed. Report
   requests, eligible writes and intensity; “redundant” is a candidate label,
   not a finding that a disk observation is useless.
5. **Skipped-required-verification**: independent evidence audit at completed
   termination checks every frozen node has a pass from the correct checker
   version/implementation, with a full manifest matching actual covered files,
   after the latest content-changing write or external event on those files.
   A same-content rewrite does not invalidate deterministic content evidence.

The semantic checker uses separate assertions from the model-triggered stage
verifiers. The obligation audit reads exported raw records, actual file hashes
and external events, not runtime completed as proof of verification. A run_id
alone or fail/uncertain result does not satisfy an obligation. It separately
reports check_executed and valid_pass. Unresolved obligations on failed/stopped
runs are not counted as a completed-run skip; skip rate is N/A with no completions.
The model's completion marker requests checking, so it is not itself a skip.
The frozen gate may make violations zero; that proves the system retained its
gate in this screen, not that the model independently chose adequate checking.

Stale evidence, exact-edit mismatch, checker failure, budget exhaustion and
infrastructure errors are failure reasons rather than a new metric framework.
No general duplicate-side-effect claim: these fixtures do not instrument opaque
external services. Correctness and obligation audit results are stored separately.

## Budget, order and missing data

32 total harness model turns including three scripted prefix turns; 150,000
recorded-token stopping threshold (one response may cross it); 16,000 estimated
context budget; 16 turns per node; 90-second provider request timeout; max output
8,000 tokens; SDK retries disabled; each worker capped at 600 seconds. Same
configured model alias/default temperature for both conditions. No tuning after
examining differences. C-time adds only the already frozen temporal note.

Order alternates by task and flips in repeat 2. Schedule, hashes, events and
provider configuration are saved before live calls. All 32 planned cells remain
in results. Prefix failure produces prefix_unavailable for each planned cell of
that task, with unknown recovery cost; do not silently replace the task or count
those missing costs as zero. Report both planned-cell completion and recovery-
eligible-cell completion. No selective reruns or success-only primary cost result.

If results lack usage/evidence or encounter infrastructure faults, record the
missingness and pause expansion; do not use low partial cost as an efficiency
win. Fully observed paired task costs use the mean of the two continuations per
condition, with per-cell terminal states alongside them. Report category and
equal-task-weight summaries; eight tasks, not 32 independent tasks.

## Screening decision, fixed before results

Engineering continuation rule (not a significance test): consider expanding only
if all planned data are interpretable, observed C-time successes are no fewer
than C, there is no confirmed new skipped-verification failure, and C-time has
lower mean recovery input on at least 5 of 8 tasks. Also require lower pooled
post-write reobservation intensity on a strict majority of comparable tasks
(both conditions have eligible writes); ties are not improvements. If too few
observation opportunities exist, the behavior signal is inconclusive, not zero.

Otherwise do not launch an expansion automatically: inconsistent/negative signal
means pause C-time advancement, retaining the candidate and logs for explanation.
A safety finding is investigated before any efficiency claim. Small-sample
success parity cannot establish formal non-inferiority. Do not target a desired
10–20% saving or optimize the prompt on this screen.

If the gate is met, a separately approved stage 2 may append repeats 3–5 on the
same frozen tasks/snapshots/settings: reuse the existing 32, add 48 = 80. Any
material runtime/task/model configuration change creates a new study version,
not an unqualified pooled extension. Twelve tasks remain an optional later step.

## Gates

New fixture reference/stub checks and obligation-auditor negative controls must
pass. A deterministic offline pass through all 8 fixtures and both conditions
must demonstrate the F3 and U-state paths, without model API usage. Do not rerun
old passed tests merely for this screen. Save original outputs.

A fresh GPT-6.1 Sol medium reviewer accepts the concrete implementation/protocol
before live calls. A different fresh reviewer validates all raw results afterward.
No reviewer replaces the orchestrator by running experiments. Findings and raw
verdicts are retained unchanged.
