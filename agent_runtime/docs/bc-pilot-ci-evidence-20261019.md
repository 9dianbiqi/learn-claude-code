# B/C worker failure evidence — next push contract

Base: `e47621f8b8f6ff8d2885679f57a2cf0e9bb6845f`.
Branch: `codex/bc-pilot-ci-evidence`. Initial status: **IN PROGRESS**.
Final disposition is recorded in the local gate below.

The prior Windows CI run `39138521201` reported 1 failed, 608 passed, 6 skipped.
The failed existing paired-process test showed duration/C exiting 2, with no child
error in the parent assertion. Linux had passed 615 tests; the failed test passed
alone on local Windows. None of this identifies the original remote root cause.
The original failed run/log and local diagnosis remain unchanged.

## Bounded milestone

Make future scripted worker failures independently diagnosable, without changing
lease TTL, deadlines, retry counts, Runtime correctness or completion semantics.
This is a failure-observability node, not a claim that the remote flake was fixed.

1. Runtime-construction and run/resume/report exceptions produce stage/task/phase
   metadata in `error.json`. Scripted fixtures also retain a bounded traceback.
2. Unexpected subprocess exit retains `process.json`, `process.log`, and bounded
   `diagnostic.json`. Parent scripted result rows surface worker evidence, including
   the prefix failure branch. Missing/malformed/oversized error JSON cannot hide the
   original exit code; an 8 KiB log tail is the fallback.
3. Live provider errors/log tails are not added to parent console summaries. The
   live model configuration and its existing local error behavior remain intact.
4. The paired protocol test opts into a unique per-attempt directory when CI sets
   `LCC_BC_TEST_ARTIFACTS`. The existing always-upload step includes it even if test
   assertions fail; no previous attempt is overwritten or deleted.
5. Prove deterministic negative cases first, then actual subprocess fallback and
   one fresh paired-process run using the new retained-evidence path on each OS.
   Previously passed Docker read windows, builds and Linux full suite are not rerun.
6. Freeze source/diff, new logs, fault results, evidence inventory and independent
   Standards/Spec review before calling this local node ready.

No model API, provider credentials, official SWE-bench Harness, image operations,
commit or push is authorized by this experiment. API/token/Harness counters stay 0.
CI workflow editing is confined to a test evidence environment variable. A later
authorized push is still needed to observe an actual hosted Windows failure with
the new diagnostic path; local injected errors are not the old remote failure.

## Initial red evidence

Two new tests failed against the base: Runtime construction escaped the worker
catch entirely, and a resume exception produced error JSON without stage/traceback.
These reproduce diagnostic loss at real worker seams. They do not reproduce the
prior hosted failure's underlying cause. Red log/JUnit are preserved externally.

## Executed evidence — 2026-10-19

The first seven new diagnostics tests passed on Windows. The fresh Windows paired
protocol plus those diagnostics passed **9 tests**, 56.54 seconds, using the new
retained-evidence environment path. The paired test includes one fresh F3 prefix
per task and a continuation from each original snapshot; the latter does not
rerun the prefix. Both result sets contain four passing B/C continuation rows.

The first Linux run recorded **1 failed, 8 passed**. It stopped before the paired
protocol: WSL Git interpreted the Windows worktree's `D:/...` gitdir as a Linux
relative path. The log is preserved. Only the runner environment was corrected:
GIT_DIR, GIT_COMMON_DIR and GIT_WORK_TREE point to the same repository/worktree
through `/mnt/d/`; `git rev-parse HEAD` confirmed the unchanged base SHA. No `.git`
file, application protocol or timeout was changed.

Review identified that reading process.log could itself raise an I/O exception.
A new red test reproduced that diagnostic failure. The helper now records a
log_artifact_failure (and similarly handles error JSON I/O) while retaining the
original child exit code. A new parent-prefix regression confirms the failure is
printed, recorded and returns nonzero. Final-source Windows diagnostics:
**9 passed**, 3.66 seconds. The earlier successful Windows paired run was not
repeated after this diagnostic-only guard; its source version is retained under
`bc-ci-evidence-before-log-guard-20261019`.

Final-source Linux paired plus diagnostics: **11 passed**, 97.85 seconds. These
results are separate batches, not a new full Runtime suite or identical-source
dual-platform paired replication. The previous Docker read cases and Linux full
suite were not rerun.

An additional parent-process probe reused the already preserved Windows F3
snapshots and injected four actual unhandled subprocess errors. All four failure
rows include their bounded traceback tail; the parent returned **1**, not success.
It made **0 new scripted model calls**, and checked that prefix/snapshot bytes
were unchanged. This tests the continuation failure path, complementary to the
prefix failure regression. It is not a reproduction of the original hosted cause.

Evidence directories under `.scratch/`:

- `bc-ci-evidence-windows-artifacts-20261019/<unique-attempt>/`: pilot/continued,
  per-worker logs, processes, requests, errors/results, snapshots and Trace.
- `bc-ci-evidence-linux-artifacts-20261019-02/<unique-attempt>/`: final-source
  Linux equivalent. The failed first Linux attempt is retained separately.
- `bc-ci-evidence-parent-failure-20261019/`: controlled continuation failures,
  diagnostic JSON, parent results and probe checks; its driver is kept alongside.
- Red/green/Windows/Linux JUnit and logs preserve every validation batch.

## Local gate

Standards final: zero hard violations and no unresolved standards blocker.
Spec final: **ACCEPT WITH NOTES**, no unresolved blocker. Current status:
**LOCAL NEXT-PUSH READY — ACCEPT WITH NOTES**, limited to the diagnostic node.
The sealed hash index is recorded in the external node report.
The gate covers the five-file diagnostic/evidence candidate only: workflow env,
B/C experiment, existing paired test, new diagnostic tests and this document.
It does not grant Remote Release Accepted to the previous branch.

The CI artifact environment is connected to the existing always-uploaded path;
actual hosted upload with this new configuration still requires an authorized
future push. No automatic retry was added; the 5-second scripted lease, 600-second
worker deadline and paired test's existing deadlines remain unchanged. No Runtime
core or accepted Phase 2 baseline change, commit or push was made. Actual model
API calls/tokens and official Harness runs remain **0**.

Final local package: `.scratch/bc-ci-evidence-ready-20261019.zip` with SHA-256
sidecar. It includes the five-file candidate patch/source, original failed CI
publication archive, red/green and platform evidence, pre-guard source, injected
parent-failure driver/output and both independent review reports. Archived fixture
copies are forensic data; original-path file identities are not recreated by ZIP
extraction, so the copied trees are not an authorized resume workspace.
