# SWE environment lifecycle and grading adapter — 2026-10-30

Local successor to the bound instance runner. No new real Docker, provider or
official benchmark result is claimed. No previous experiments were rerun.

## Environment preparation

`agent_runtime.swe_environment.provision` is a context manager accepting the
prepared dataset, trusted manifest hash, instance ID, dedicated Docker client
(HTTP timeout <= 5 seconds), existing immutable local image ID, trusted base tar
and hash, frozen CommandProbes, and a fresh output directory.

It validates the dataset/instance and archive, freezes the archive bytes, checks
the Linux engine and local image, creates an owned isolated source container,
binds DockerWorkspaceBackend, and uploads the source into `/workspace` tmpfs.
The yielded backend can be passed directly to `inspect_swe_runner.run_instance`.
Use separate fresh directories for provisioning and the candidate runner.

Normal exit rechecks engine/source identity, refuses paused sources, removes
only the owned source and requires a typed Docker 404 observation. Exceptions
retain the source and lifecycle diagnostics for explicit recovery. A create
timeout retains its session label even if no container ID was returned; it is
never automatically retried. Callers must raise/retain the environment when a
returned Runtime outcome requires review rather than treating that as clean exit.

Lifecycle events are immutable numbered JSON files, with an atomically replaced
`lifecycle.json` view. This is a diagnostic lifecycle record, not a new fenced
cleanup authority or a claim of crash-durable filesystem transactions.

This automates provisioning from prepared assets. It does not download images,
clone repositories, install project dependencies or discover validation probes.
Image/base/probe selection remains a trusted controller responsibility.

## Official grading entry point

`agent_runtime.swe_grading.grade(bundle, manifest_sha256, predictions,
prediction_sha256, harness, output, execute=False)` validates the complete
prediction denominator and requires a clean Git harness checkout whose HEAD
matches the prepared dataset's harness commit. One model identity is supported.
The adapter targets the v3 report schema; compatibility with another harness
version must be checked before use. Default preflight creates no output and does
not invoke Docker, the harness, or any model.

With `execute=True`, use a prepared Linux host with Docker and harness Python
dependencies already installed. The adapter freezes dataset/prediction inputs,
launches the pinned checkout through isolated Python, sets one worker and finite
test/wall timeouts, and keeps the invocation, console log and result. The official
harness itself may build images/download dependencies; that execution was NOT
performed in this round. A timed-out harness may leave Docker resources; inspect
the recorded run ID and diagnose them rather than blindly rerunning the output.

Only a zero-exit invocation with an internally consistent schema-v2 report can
populate per-instance `official_resolved`. Missing/error instances stay null;
official empty-patch instances remain in the denominator and are false. Report
IDs, counts, partitions and empty-patch membership must agree with predictions.
Candidate validation is never used as official grading evidence. Failed execution
keeps `new_official_evaluations=null` because execution may have partially occurred.

The v3 adapter was checked against the locally archived official v3.0.17 source
(`.scratch/swebench-single-20261013/harness-v3.0.17` at repository root). That old
archive is API reference only, not evidence of a new run or a replacement for the
clean pinned checkout required by this adapter.

## Incremental verification

27 distinct new offline checks passed: 16 environment/archive checks and 11
grading checks. Docker transport/backend and official subprocess/report are
simulated. This does not establish real image readiness or official resolution.

Evidence at repository-root `.scratch/`:

- `swe-env-tests-20261030c.xml`: 5 archive checks passed; 10 lifecycle checks
  initially failed because the existing `_dump` helper is exclusive-create.
- `swe-env-tests-20261030d.xml`: those 10 lifecycle checks passed after switching
  to numbered events and an atomic current-state replacement.
- `swe-grade-tests-20261030.xml`: 11 grading checks passed.
- `swe-final-tests-20261030.xml`: 1 new frozen-archive check and 3 affected checks
  passed after the final changes.

Initial collection attempts lacked `mcp`. Existing dependency directories were
reused through process-local PYTHONPATH; no packages were installed. No full
suite, accepted experiment, paid model or official run was repeated.

Docker preflight still failed: config access denied and missing `docker_engine`
named pipe. The host had approximately 1 GB free on C: at the beginning of this
round, and command startup once failed while writing a sandbox log. No disk
cleanup, Docker configuration changes, Git ref mutation or publication occurred.

Next acceptance: prepare/recover the Linux Docker host; select matching existing
image/base/probes; run the new lifecycle with a bounded mock repair through the
real backend; then explicitly run the pinned official harness against the new
prediction and inspect its report. General image/dependency preparation remains
open, as does real-container acceptance of these new interfaces.
