# Runtime clean release preparation

Date: 2026-10-17. Status: **ready for a local commit and experimental-branch push**.
No commit, push, merge or remote CI execution was performed by this preparation.

## Publication scope

The user selected a clean release branch rather than publishing the 55 local
historical commits. Branch `codex/runtime-completion-release` starts directly at
remote `main`, `3702635261e2b46416d172796232a5a7f413415c`.
Only `agent_runtime/`, its Eval suites and required root dependency/workflow files
are imported. Top-level research proposals, teaching scripts, the learning web
platform and the unrelated historical Git commits are excluded.

The Runtime depends on the previously reviewed runtime implementation, not only
the recent completion changes. This branch therefore contains that dependency
snapshot, tests and Runtime-local history/provenance documentation. This is an
intentional Runtime snapshot diff, not a claim that the older dependency code is
newly implemented. Historical commit IDs in retained reports describe the source
working tree; they are not ancestors of this clean publication branch.

All top-level Runtime production Python modules match the accepted experimental
working tree byte for byte. The original Phase 2 checkout and original experiment
branch remain unchanged. Publication preparation changes only experiment runners,
tests and documentation, plus imports already accepted Runtime dependencies.

## Final local Runtime CI checks

| Check | Result |
|---|---|
| Windows, Python 3.11.5, full Runtime suite | **567 passed, 2 skipped**, 389.97 seconds |
| Linux/WSL, Python 3.11.17, full Runtime suite | **569 passed**, 190.67 seconds |
| Windows fencing regression stress | **30/30 passed** |
| Windows/Linux MVP Eval | **5/5 passed on each** |
| Windows/Linux Hardening Eval | **9/9 checks passed on each** |
| Windows/Linux deterministic demo | Correct completed edit, recovered effect once, external conflict needs review |
| Windows/Linux Runtime compileall | Passed |
| Revised real Docker cleanup-gate runner | Both ignored-error and process-crash paths passed |
| Revised real Docker release runner | Unpause-response-loss case passed with typed repository blocker |
| Revised real Docker backend fixture cleanup | Lease takeover passed; retained validator explicitly removed by fixture owner |
| Missing local image preflight | Rejected with ImageNotFound before container creation; no auto-pull |

The two Windows skips are explicit platform privilege limits for symlink creation;
the corresponding tests pass on Linux. No new skip or xfail was introduced.
Linux uses an isolated dependency environment and a source-hash-verified copy in
a native Linux Git checkout. Its venv bin directory is explicitly on PATH so
Shell subprocesses use the same Python, as setup-python does in CI.

These reproduce the Runtime workflow locally. They do not mean GitHub Actions
has run or that unrelated web checks have been executed. Remote CI remains the
normal follow-up after a real push. Hardening's 9/9 refers to expected assertions,
including review/failure outcomes, not nine completed tasks.

## Failures retained and repaired

The first Windows full run was **563 passed, 4 failed, 2 skipped**. Its raw JUnit
and logs are retained; the final results above do not erase that run.

* Two old DAG fixtures directly changed a completed dependency to pending while
  retaining its valid authority. This constructs a corrupt projection, not legal
  evidence invalidation. They now explicitly require InvariantViolation, no
  dependent verifier/tool execution and no extra model turn. Production invariant
  checks were not weakened, and the tests were not skipped or xfailed.
* An old CLI security assertion conflated Shell authorization with replay safety.
  It still checks that the ordinary source command is not denied, while requiring
  the accepted `unknown_write` classification.
* The offline B/C fixture used a one-second lease and legitimately hit LeaseLost
  during Windows I/O. Its scripted lease is now five seconds; actual os._exit,
  natural expiry, fencing and process timeouts remain. Live-mode budgets are
  unchanged. The revised protocol/security tests passed separately before the
  full final run.

The first Linux full run was **568 passed, 1 failed** because the launcher had
not activated the venv PATH for Shell's `python`. That environment issue was
corrected without changing Runtime code. Earlier missing pytest/ensurepip and a
vanished temporary environment are also retained as setup failures, not reported
as passing test executions.

The cleanup runners now select the exact source reservation rather than assuming
there is only one resource record. They still require other reservations closed.
Release checks expect EffectBlocked instead of a historical message substring.
Owned fixture cleanup now removes retained validators after stale-owner refusal,
and records before/after inventories. Local image preflight avoids implicit pulls.

## Review

### Standards

Independent release-preparation review: PASS, zero hard violations and no new
significant smells. The prior two maintenance suggestions remain nonblocking.

### Spec

Independent release-preparation review: ACCEPT WITH NOTES, no code blocker to
committing/pushing this experimental branch. It confirmed the test updates are
contract corrections rather than suppression of genuine failures. At review
time final platform checks were still running; their completed results are
recorded above by the main controller, not retroactively attributed to reviewers.

The earlier whole-successor ACCEPT WITH NOTES and original validator-cleanup
REJECT/repair are retained in the accepted evidence package. No acceptance claim
has been expanded to arbitrary daemon faults, power loss or separate controllers.

## Reproduce and publish

Ordinary checks require Python 3.11, Git, repository requirements and pytest:

```sh
python -m pip install -r requirements.txt pytest
python -m compileall -q agent_runtime
python -m pytest agent_runtime/tests -q --tb=short -p no:cacheprovider
python -m agent_runtime eval --suite evals/mvp.yaml --runs /tmp/lcc-mvp --output /tmp/lcc-mvp.json
python -m agent_runtime eval --suite evals/hardening.yaml --runs /tmp/lcc-hardening --output /tmp/lcc-hardening.json
python -m agent_runtime.demos.run_demo --output-root /tmp/lcc-demo
```

Activate the environment before these commands. Use fresh output directories and
platform-appropriate paths. Docker is optional for unit tests; manual Linux
fixtures require the compatible prebuilt image and optional dependencies described
in [DOCKER-FIXTURES.md](../experiments/DOCKER-FIXTURES.md). No image is exported,
downloaded or built automatically.

Suggested reviewable commit split: Runtime dependency snapshot; completion and
workspace contracts with tests; release runner/test corrections and evidence
documentation. The exact payload is available as the clean branch's unstaged diff.
Publish this experimental branch and inspect remote CI before any main merge.
No staging, commit, force push or merge is needed to inspect this preparation.

## Evidence

Portable provenance is in [release-evidence-index.json](release-evidence-index.json).
Raw archives are intentionally external to Git. The earlier accepted bundle is
`phase2-successor-accepted-evidence-20261017.zip`, SHA-256
`1dfeb86eef24e97d514434a8e7f21dfeecd2aeee39391c132e743bb985a9fa09`.

New evidence lives under the local `.scratch/release-readiness-20261017-01/`;
the prepared bundle is `runtime-release-ready-evidence-20261017.zip`, with an
external SHA-256 sidecar. It contains failure/pass logs, both final JUnit files,
reviews, source identity/scope inventories, Docker records and a release diff.
The bundle remains local until explicitly uploaded; a Git commit does not by
itself distribute the raw evidence.

Target-model API calls, paid experiment calls and official SWE-bench Harness runs
are **0**. Synthetic usage fields in deterministic Eval reports are fixture values,
not provider billing. Passing local checks establishes publication readiness for
this bounded Runtime snapshot, not Agent competence or SWE-bench success.
