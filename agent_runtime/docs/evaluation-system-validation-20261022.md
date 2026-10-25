# Evaluation system v1 — local validation

> Publication follow-up (2026-10-23): this report preserves its original validation-time status. See [project closeout](project-closeout-20261023.md) for published commit/hosted CI status and the subsequent uncommitted integration. Historical “not committed/pushed” wording below is not the current publication state.

Status: Local evaluation-system milestone ACCEPT WITH NOTES, 2026-10-22.
Branch: `codex/evaluation-system`; unchanged base
`ba5a242019ec411e5138fa5ba028b529dd66c546`. No commit or push was performed.
This is the offline execution/import milestone defined in
[the acceptance contract](evaluation-system-v1.md), not acceptance of Agent repair
competence or remote release.

## Implemented behavior

`python -m agent_runtime.evaluation` now executes bounded new scripted fixtures,
imports byte-pinned historical Agent/official evidence, and writes sealed output
bundles with JSON/Markdown reports, raw inputs, traces, model calls, workspace
changes and SQLite execution history. It never starts a provider or harness.

Runtime regression oracles, Agent repair outcomes and gold-control official
results have separate denominators. Unknowns remain null. Input validation occurs
before tools run; processing errors remain in planned totals. A missed injection
point cannot start a second task. A stopped/completed Runtime or nonempty patch
cannot establish semantic success. Contradictory official success evidence is
rejected rather than regraded from test output.

## Validation evidence

Evidence root (D drive):
`D:/opencv/learn-claude-code/.scratch/evaluation-system-evidence-20261022`.

| Check | Windows | Linux / WSL |
| --- | --- | --- |
| Final evaluation tests + existing trace/eval integration | 44 passed, 1 skipped | 45 passed |
| Additional artifact-shape CLI cases, disjoint selection | 3 passed | 3 passed |
| Combined unique test cases | 47 passed, 1 skipped | 48 passed |
| Executable three-case example | 3/3 passed, earlier implementation | 3/3 passed, final implementation |

The Windows skip is the unavailable symlink privilege; Linux executes that case.
Linux fixture temporary files used native tmpfs; logs and JUnit are on D. These
are targeted checks of the new evaluator and its trace integration, not a rerun
of the previously accepted full Runtime suite or container/model/harness tests.

The first test attempt was 3 failed / 26 passed / 1 skipped because the new happy
path fixture wrote an existing file without reading it. The Runtime correctly
required review. Fixtures were corrected to read before modifying; permissions
and write safety were not relaxed. Those first-attempt outputs remain preserved.

The final historical import probe processed both real archives without running
either experiment again:

| Historical evidence | Result |
| --- | --- |
| readonly Agent, sympy__sympy-19346 | Runtime completed; Agent task false; empty patch; official resolved unknown |
| Provider usage from three explicit archived responses | 1,363 input / 128 output tokens; historical, not new consumption |
| gold-control run02, sympy__sympy-20590 | official resolved true; environment denominator only; Agent success unknown |
| New provider requests / official evaluations | 0 / 0 |

The old empty_patch report was not imported as a new official result. A receipt
binds the successful gold run02 execution, report and prediction. The older frozen
plan's run01 identity is retained as supporting metadata and explicitly disclosed;
it is not substituted for run02 execution evidence.

## Independent review

**Standards:** 0 hard violations; 1 nonblocking Possible Repeated Switches
maintenance note for the three mode branches. No new Runtime/Verifier authority.

**Spec:** initially rejected for contradictory official success acceptance and
incomplete input-shape checks. Both were repaired and covered by regressions.
Final limited code re-review: ACCEPT WITH NOTES, no identified code blockers.
A subsequent claim about missing artifacts-object validation was withdrawn after
the reviewer re-read the existing guard; exact CLI cases were still added.
The full milestone decision additionally consumes the final archive probe and
sealed evidence; it does not extend the reviewer verdict to Agent competence.

## Preserved boundaries and risk

- Frozen Phase 2 HEAD remains `68e4e51ca9cc4049f575d617762f89934e443baa`,
  with no tracked diff. The prior read-budget archive still hashes to
  `a60acd238a9e0924a688139546c228852064deb4230ab30f1b0def54786a6042`.
- The separate accepted read-budget candidate is untouched. This worktree starts
  from the published base and does not silently include that uncommitted feature.
- Hash pins and evaluator-owned receipts protect against accidental mutation and
  provenance mixups, not a fully fabricated internally consistent archive or
  concurrent filesystem attack. Archives are sensitive evaluator inputs; gold
  patches remain outside solver workspaces and outside the tracked release files.
- Legacy eval behavior is unchanged. The new CLI's exit 0 means processing
  succeeded; CI consumers must inspect the intended outcome field/aggregate to
  enforce a task-success threshold. Unknown cases prevent a complete rate.
- This milestone provides no new autonomous code-repair success and no statistical
  SWE-bench claim. Live provider/container/harness orchestration remains a
  separately authorized experiment stage; no new paid call or official run was
  authorized here. The earlier remote Windows fencing CI failure is not repaired
  or superseded by these evaluator tests.

Local acceptance is limited to this executable offline evaluation system. Remote
release acceptance, general Phase 2 correctness and exactly-once claims remain
outside this decision.
