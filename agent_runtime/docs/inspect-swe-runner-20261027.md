# Bound SWE instance execution — 2026-10-27

> 2026-11-01 successor: real mock response envelopes and container tool names
> are now checked during preflight before binding/provisioning. Later lifecycle
> and grading integration is described in [the operator guide](swe-operator-guide-20261101.md).
> The unfinished-integration statements below describe the original milestone.

This successor connects the previously separate bounded Inspect model runner,
container repair capability and prepared SWE dataset. It is local implementation
plus offline acceptance, not a new live-model/Docker/official benchmark result.

## Implemented flow

1. Verify the prepared dataset manifest and every pinned input. Select one explicit
   instance from the solver allowlist; match its base commit and instance id to
   the caller's already provisioned DockerWorkspaceBackend.
2. Preflight an explicit provider/model and finite request/output/timeout limits.
   By default no binding, output directory or model invocation is created.
3. With execute=True, create a repair binding backed by the existing frozen
   workspace candidate Verifier. The model gets only the problem statement and
   container read/discovery/edit tools; the controller retains gold/test inputs.
4. Run Runtime through the bounded Inspect model bridge. Persist binding metadata,
   model/tool receipts, checkpoints, traces and .eval logs; verify log linkage.
5. Read the candidate produced by the existing workspace Verifier. Check workspace,
   instance, base, validator, revision, patch hash, applicability and actual probe
   evidence before exporting the patch.
6. Always keep the planned instance in predictions.jsonl after a returned run,
   including an empty patch on failure. Preserve startup/log failures as processing
   errors with diagnostic output and a seal. A live request count is unknown if
   the model runner raised without returning; it is never replaced with zero.

## Programmatic entry point

```python
from agent_runtime.inspect_swe_runner import run_instance

result = run_instance(
    prepared_dataset, trusted_manifest_sha256, instance_id,
    backend, fresh_output,
    model="provider/explicit-model", mode="live",
    max_turns=8, max_output_tokens=2048, timeout=60,
    execute=False,  # preflight only; execution is an explicit caller decision
)
```

The caller supplies the backend, including source ownership, pinned image,
workspace path, clean base archive and trusted validation probes. It also owns
backend cleanup. The function does not download datasets/images, clone a repo,
choose credentials, create a Docker daemon, or invoke official scoring.

For offline execution use mode="mock", model="mockllm/runtime-bridge" and an
explicit bounded responses list, then execute=True. Scalar configuration is
checked during preflight; container response scripts are validated against the
bound capability before generation. No hidden provider fallback is permitted.

The lower-level model runner now accepts `bindings={case_id: binding}` and
`execution_kind: container_repair`; JSON alone cannot supply the controller
capability. Missing/foreign bindings, native host tools, unsupported execution
kinds and local file goals for a container case are rejected. Local file tasks
keep the previous default behavior.

## Outcome semantics

`processing_ok` is evidence processing, `candidate_validated` means the frozen
workspace candidate passed the configured local probes, and `official_resolved`
remains null. Predictions contain the standard instance_id, model_name_or_path
and model_patch fields. A candidate patch is not an official resolved score.
Source dataset revision, harness commit, instance/base and manifest digest are
recorded in inputs/provenance.json. A configured harness commit is not proof that
that harness executed.

The runner currently produces one instance per output. Existing dataset prediction
export validates complete multi-instance denominators when collecting results.
Automatic batch provisioning, container creation/deletion, official harness
execution and general file creation/deletion remain unfinished integration work.
Editing still assumes one trusted container writer and uses Runtime's existing
ambiguous-effect review rather than replaying uncertain writes.

## Offline evidence

16 new distinct checks passed incrementally:

- Seven in test_inspect_model_container.py: actual Inspect/Runtime repair wiring,
  host-sentinel isolation, invalid capabilities and provider-free live preflight.
- Eight initial checks in test_inspect_swe_runner.py: prepared synthetic instance
  to candidate prediction; empty prediction on failure; backend mismatch;
  preflight; inconsistent candidate evidence rejection.
- One additional startup-failure test: diagnostic/empty-prediction preservation
  and no overwrite/reexecution of its output directory.

JUnit files under repository-root `.scratch/`:
`model-container-tests-20261027.xml`, `inspect-swe-tests-20261027.xml`, and
`inspect-swe-startup-tests-20261027.xml`. The subprocess edit program and real
Inspect/Runtime were used with fake Docker transport and a synthetic validation
backend. This does not establish real Docker or real repository repair acceptance.
No previous full suite or accepted experiment was rerun. No provider requests,
official evaluations, Git ref changes or remote publication occurred.

The new Docker availability check returned no docker_engine named pipe and
access denied for the local Docker config. No container was started. Real
container validation is currently environment-blocked, not passed.
