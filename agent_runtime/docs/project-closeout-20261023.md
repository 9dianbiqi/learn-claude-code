# Project closeout — 2026-10-23

> Update 2026-10-25: LOCAL INCREMENTAL VALIDATION PASSED. There are 55 distinct passing checks across two attempts: budget 22; Inspect 32; fencing 1. Initial Inspect results were 27 passed/5 failed on a restricted default log directory; only those five and the previously unrun fencing test were executed next (6 passed). No accepted full suite, Docker experiment or backup was rerun. Older blocker descriptions below remain historical.
>
> CFFI 2.0.0 and pycparser 3.0 were recovered from a compatible local Windows/Python 3.11 environment with RECORD hash validation. The isolated overlay uses MCP 2.3.0, Inspect 0.3.283, Pydantic 2.13.4 and pydantic-core 2.46.4. Inspect uses platformdirs' supported WIN_PD_OVERRIDE_LOCAL_APPDATA to keep logs on D:. Tests remain mock/offline; no provider or official Harness calls.
>
> Remote branch codex/runtime-closeout-20261023 was created at c3e1192, but the subsequent source-tree upload was rejected by automatic approval review for lack of explicit authorization to export the source/document payload to GitHub. No candidate code has been published. Local Git metadata remains read-only. Publication requires explicit user approval of the exact candidate.
>
> A separate Linux-only incremental workflow is prepared locally. Publication plan: candidate source/document commit with [skip ci], followed by a workflow-only commit. Its path filter triggers only the new narrow workflow; old full workflows are not rerun. No PR/main merge or branch deletion is included in this publication step. This is a proposed sequence, not an executed remote validation.

Status: local integration prepared; Git publication and physical branch cleanup remain blocked by read-only Git metadata in this session. No branch was deleted, moved or merged. No paid model calls or official Harness runs were started.

## Development entry

Use `codex/inspect-runtime-solver` at `D:/opencv/learn-claude-code/.scratch/inspect-runtime-solver-worktree`. Its committed base is `c3e1192d0a83038d7d21d2ca92fd1935d01a1a3e`; this closeout adds uncommitted integration changes. The repository root remains the historical v0.3 checkout with research edits. Local `main` tracks `upstream/main`, not `origin/main`; do not silently fast-forward or repoint it.

## Remote publication verified

The GitHub connector verified the published branch and push workflows for c3e1192:

- [Inspect workflow 39823245184](https://github.com/9dianbiqi/learn-claude-code/actions/runs/39823245184): success.
- [Runtime workflow 39823244953](https://github.com/9dianbiqi/learn-claude-code/actions/runs/39823244953): failure. Linux job passed. Windows full tests passed (717 passed, 9 skipped), B/C audit passed, then fencing stress failed on iteration 4 with owner-b/fence2 LeaseLost at finish_model_call. Subsequent Windows eval/demo steps were skipped.
- No open PR was returned by the connected repository search at audit time.

This supersedes publication-time wording such as “not committed/pushed” in the historical validation reports. It does not overwrite the original local evidence or relabel failed hosted runs.

## Read budget integration

The seven-file candidate from `codex/container-read-budget` was verified against every source hash in its sealed node-state manifest. Its archive SHA-256 is `a60acd238a9e0924a688139546c228852064deb4230ab30f1b0def54786a6042`.

All three previously tracked target files were byte-identical in Git between ba5a242 and c3e1192. The seven candidate files were copied exactly into this latest worktree; the original candidate remains unchanged. This adds opt-in durable task-local read-attempt limits and typed budget-denial recovery. It also incorporates the already-reviewed test-only fixed Store clock for the stale-owner fencing fixture. Production clocks, lease durations and fencing assertions are unchanged.

Local integration test results are recorded below after execution. Prior full-suite/container results keep their original source-stage scope. New integration tests do not establish Agent competence or official SWE-bench success.

## Preservation

Before editing, 33 worktrees were inventoried and 119 source/document files were archived byte-for-byte. Every ZIP member was read back and hash checked. Evidence: `.scratch/closeout-20261023/uncommitted-sources.zip` and `worktree-inventory.json` under the repository root. This source backup does not replace existing raw experiment archives or include every ignored/untracked runtime artifact. Those remain in their original worktrees.

The inventory distinguishes files already matching c3e1192 (ignoring CRLF) from unpublished or different files. In particular, FSE01 and SWE-bench standardization materials remain unpublished experimental artifacts; research proposals, glossary and ADRs remain separate from the Runtime release snapshot. Do not remove their worktrees.

The clean release at 74ef118 intentionally imported an accepted Runtime snapshot from the old experimental line. Old commits not being ancestors of the new release does not imply missing functionality. The divergent ticket-7-review-fixes branch still needs a dedicated patch-level absorption audit before deletion; no blanket merge is proposed.

## Branch registry

Classification is logical only. No physical cleanup was performed. Preserve tags and source/evidence ownership before any later removal.

| Branch | HEAD | Classification |
|---|---|---|
| `codex/agent-runtime-ci-stability` | `f2288f7` | HISTORICAL: included by ancestry in latest |
| `codex/agent-runtime-v0.1.1` | `9e5ab39` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/agent-runtime-v0.2` | `cff9b7f` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/agent-runtime-v0.2-next` | `647cb6d` | HISTORICAL: included by ancestry in latest |
| `codex/agent-runtime-v0.2-phase2` | `1105861` | HISTORICAL: included by ancestry in latest |
| `codex/agent-runtime-v0.3` | `a328e1a` | HOLD: root research edits and glossary/ADRs |
| `codex/bc-artifact-audit` | `ba5a242` | HISTORICAL: included by ancestry in latest |
| `codex/bc-pilot-ci-evidence` | `a1d3815` | HISTORICAL: included by ancestry in latest |
| `codex/bc-recovery-pilot` | `3b0e566` | HOLD: frozen Phase 1 plus unpublished experiment materials |
| `codex/c1a-immutable-plan-revisions` | `60fd072` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/c1b-durable-plan-revision-resume` | `b1f0aef` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/c1c-implementation` | `b0aec3d` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/completion-recovery-contract` | `68e4e51` | HOLD: dirty experiment sources; archived, do not remove |
| `codex/container-read-budget` | `ba5a242` | PRESERVE: source candidate integrated byte-for-byte; still uncommitted |
| `codex/container-read-recovery` | `df01281` | HOLD: unpublished documentation edit |
| `codex/container-read-resume` | `e47621f` | HISTORICAL: included by ancestry in latest |
| `codex/evaluation-system` | `cfd4017` | HISTORICAL: included by ancestry in latest |
| `codex/inspect-offline-adapter` | `de858a1` | HISTORICAL: included by ancestry in latest |
| `codex/inspect-runtime-solver` | `c3e1192` | ACTIVE: integration workspace |
| `codex/long-horizon-agent-v1` | `b0aec3d` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/long-horizon-agent-v1-bootstrap` | `b66e440` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/provider-tool-name-compat` | `ee65d42` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/runtime-completion-release` | `74ef118` | HISTORICAL: included by ancestry in latest |
| `codex/runtime-correctness-phase2` | `68e4e51` | FROZEN: Phase 2 accepted baseline |
| `codex/ticket-10-fixed-budget-scoped-resume` | `bc886fc` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/ticket-6-freeze-v03-baseline` | `a328e1a` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/ticket-7-atomic-verified-checkpoint` | `9a18281` | HOLD: research edits and glossary/ADRs |
| `codex/ticket-7-review-fixes` | `a2196a0` | HOLD: divergent review history; patch-level absorption not established |
| `codex/ticket-8-frozen-dag-orchestration` | `2db9c8a` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `codex/ticket-9-stale-evidence-refresh` | `1643a05` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `main` | `cff9b7f` | HOLD: tracks upstream/main; origin/main is a different ref |
| `wt/task1-worktree` | `06ac34e` | HISTORICAL: old experimental line, included in Phase 2 ancestry |
| `wt/task2-worktree` | `06ac34e` | HISTORICAL: old experimental line, included in Phase 2 ancestry |

## Remaining release gates

1. Commit the reviewed integration and closeout documents from the latest worktree in an environment with writable Git metadata. Keep research sources and private oracles out of the publication.
2. Push that new commit and verify both hosted workflows at its exact SHA. Do not rerun the old SHA and claim the local fix was tested remotely.
3. Review the Runtime-only diff against origin/main before a PR/main merge. Do not merge the old experimental history just to change ancestry.
4. Archive historical branch refs and remove only clean, confirmed redundant worktrees after resolving the HOLD rows. Frozen Phase 2 and original evidence stay intact.

Git refs/index/config are read-only in this session, so commit, switch, upstream correction and branch/worktree removal were not attempted through alternate paths. These publication/cleanup gates remain pending, not accepted.

## Final local validation and blockers

- Seven integrated candidate files still match the accepted source manifest exactly; the old source worktree is unchanged.
- The new targeted pytest attempt did not execute tests: six collection errors from missing `mcp`, with three optional Inspect modules skipped. Direct Inspect import also fails on missing `attr`. Original result: `integration-junit.xml`.
- The existing Inspect virtual environment was reused, not recreated or edited. An isolated `pip install --target .../dependencies mcp==2.0.0 attrs` attempt exited 1 because its package source offered no matching mcp==2.0.0 distribution. No local integration PASS is claimed.
- Static compilation and reverse patch applicability are checked when packaging below; these cannot replace Runtime/Inspect execution.
- Re-run the selected budget/container/completion/fencing/evaluation/Inspect suites in a repaired dependency environment before committing or publishing this integration. Retain the failed collection result separately.

The current outcome is PREPARED / VALIDATION BLOCKED, not release acceptance. The c3e1192 hosted failure remains the recorded remote state. Physical branch cleanup and Git publication are pending read-write Git access; no approval request can override this session's read-only Git policy.

## Pending-only continuation (2026-10-23)

No accepted full-suite, Docker experiment, source backup or historical evaluation was rerun in this continuation. Missing pure-Python distributions and compatible Windows pydantic/pydantic-core/cryptography packages were copied into an isolated dependency overlay with RECORD hash checks. The normal system/other project environments were not modified.

The remaining import blocker is Python 3.11's native _cffi_backend. The available other-project backend targets Python 3.10 and was not copied. The package-source install for cffi>=2.0 returned no matching distribution. WSL startup returned E_ACCESSDENIED. C: was measured at approximately 10 MB free after an exec setup failure with OS error 112. These are environment blockers, not passing validation results.

Pending verification runner: repository-root .scratch/closeout-20261023/verify_pending_integration.py. It runs only the new budget integration, Inspect bridge/solver integration, and one fencing-fix check. It preflights imports, puts temporary files/evidence on D:, preserves failed attempts, and reuses PASS receipts only for identical source and environment fingerprints. No paid calls, old full suites, Docker runs, Git writes or cleanup are included. The runner has been syntax-checked; its test stages remain unexecuted until dependencies are repaired.

The earlier integration.patch / integration-candidate.zip preserve the earlier candidate; this continuation adds this documentation note and the standalone pending verifier. No new publication or release acceptance is claimed.
