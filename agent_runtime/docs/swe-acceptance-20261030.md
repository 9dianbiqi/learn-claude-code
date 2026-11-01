# Single-instance acceptance composition — 2026-10-30

> 2026-11-01 successor: CLI execution now requires `--docker-host` and
> `--expected-engine-id`; ordinary-user entry, preparation and result collection
> are documented in [the operator guide](swe-operator-guide-20261101.md).
> API callers can also pin the engine. This update does not renew historical
> acceptance or establish a real Docker/official result.

This round attempted the real-host preflight. WSL enumeration returned
`Wsl/EnumerateDistros/Service/E_ACCESSDENIED`; Docker reported config access denied
and no `docker_engine` named pipe. No real container or official evaluation was
started. The host access problem remains an external acceptance blocker.

## New entry point

`python -m agent_runtime.swe_acceptance CONFIG.json NEW_OUTPUT` performs preflight.
Add `--execute` to provision and run one mock repair. Add `--official` with
`--execute` to score after successful lifecycle cleanup on a prepared Linux host.
No live provider option exists in this acceptance runner.

The trusted controller JSON supplies these keyword arguments:

```json
{
  "bundle": "/absolute/prepared-single-instance",
  "manifest_sha256": "<trusted prepared manifest digest>",
  "instance_id": "<the single selected instance>",
  "image": "sha256:<existing local immutable image ID>",
  "base_archive": "/absolute/inputs/base.tar",
  "base_archive_sha256": "<trusted base archive digest>",
  "probes": [{"argv": ["/opt/miniconda3/envs/testbed/bin/python", "-V"],
              "stdout_sha256": "<frozen expected output digest>", "timeout_seconds": 5}],
  "responses": [{"text": "TASK_GAVE_UP"}],
  "max_turns": 4,
  "harness": "/absolute/clean-pinned-harness-checkout"
}
```

This is a schema illustration, not a runnable benchmark config. Real candidate
acceptance requires task-relevant probes and a bounded read/edit response script.
The example's give-up response can only exercise the empty-prediction path. A
scripted correct edit measures plumbing, not autonomous repair capability. Input
assets and output must be disjoint; output is fresh and never auto-retried.

The API accepts an existing Docker client. The CLI constructs one only after
input preflight. Missing Python Docker dependencies or failure constructing that
client are CLI startup errors, before a run directory exists. Once orchestration
starts, provision/binding/model/cleanup failures get a result plus prediction.

## Outcome and cleanup contract

`inspect_swe_runner` now propagates `runtime_status` and
`manual_recovery_required`. A processed Inspect log is not proof that the task
is safe to clean up: `needs_review`, missing status and processing errors require
retention. The orchestrator raises inside the provision context to preserve the
source, and skips scoring. An explicit terminal task failure may clean up normally
and be officially scored with an empty patch.

Candidate prediction hashes, identity and validation outcome are checked before
cleanup. Cleanup failure preserves already validated candidate bytes but blocks
grading. Official grading receives a frozen prediction copy in a disjoint input
directory. Top-level outcomes never translate candidate validation into resolved.
Failure reports preserve nested candidate diagnostics; existing detailed model
logs, environment journals and official reports remain in their child directories.

## Verification scope

Ten distinct new checks passed incrementally:

- Six initial preflight/failure/overwrite checks passed in
  `.scratch/swe-acceptance-tests-20261030.xml` at repository root.
- Two actual Inspect/Runtime mock integrations initially failed because Inspect
  could not write its default app-data trace directory. A process-local supported
  platformdirs override (`WIN_PD_OVERRIDE_LOCAL_APPDATA`, and APPDATA counterpart)
  pointed the logs into the writable workspace. Plain LOCALAPPDATA did not affect
  this platformdirs resolver. The two affected tests then passed in
  `.scratch/swe-acceptance-retry2-20261030.xml`.
- Two new mutable-image/review-propagation checks passed in
  `.scratch/swe-acceptance-final-20261030.xml`.

The integrations used the real Inspect/Runtime stack and edit subprocess, with
simulated Docker transport/backend/lifecycle; the grading process was simulated.
This is composition evidence, not real Docker or official scoring acceptance.
No previous accepted experiments/full suite were rerun, no paid model calls were
made, and no code was published. Existing dependency directories were reused.

Next action remains restoring access to a Linux Docker host, preparing one exact
instance's existing image/base/probes and clean harness checkout, then executing
this entry point and retaining the real lifecycle and scoring evidence. Do not
count synthetic resolved outcomes from these tests as benchmark subjects.
