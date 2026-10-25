# Offline Docker fixture runners

These runners exercise controller/container correctness with ScriptedModel and
controller-authored toy patches. They do not call a model API or SWE-bench Harness.
Historical reports retain their original source versions; this directory's Python
runners target the current implementation.

Use Python 3.11 on Linux with a local Docker Engine, Git, repository requirements,
pytest, and `python -m pip install -r agent_runtime/experiments/requirements.txt`.
Run from the repository root so `agent_runtime` is importable. Docker fixtures are
explicit manual experiments, not part of ordinary Runtime CI or pytest discovery.

Supply a prebuilt local immutable image ID through `LCC_DOCKER_IMAGE`. The image
must contain `/bin/tar`, Git and isolated Python at `LCC_CONTAINER_PYTHON` (default
`/opt/miniconda3/envs/testbed/bin/python`). The backend currently uses that default
Python path for its trusted exporter/thaw/setup commands too, so a different probe
Python path alone does not make another image compatible. The historical local
image ID remains the default for provenance, not a downloadable dependency.
Preflight inspects the local image; it never pulls or builds one automatically.

Containers use UID/GID 65534, private PID namespaces, no network or host mounts,
readonly root, writable bounded tmpfs, dropped capabilities and an idle sleep init.
The image must permit these settings. Only trusted operators should control the
Docker endpoint. Experiments explicitly remove only their owned fixture containers.

Examples (fresh output directories are required):

```sh
python -m agent_runtime.experiments.workspace_cleanup_gate_probe --output /tmp/lcc-cleanup-gate
python -m agent_runtime.experiments.docker_release_probe --case unpause-response-lost --output /tmp/lcc-release
python -m agent_runtime.experiments.validator_cleanup_probe --output /tmp/lcc-validator
```

Other manual entry points are `docker_backend_probe` and
`docker_backend_crash_probe`; their `--help` lists bounded scenario selection.
Source and validator cleanup reservations are separate records. Source release
checks select the exact bound source record and require unrelated reservations
to be closed. Release recovery now expects the typed repository `EffectBlocked`,
not historical diagnostic text containing `quarantine`.

Accepted historical evidence ZIP: `phase2-successor-accepted-evidence-20261017.zip`,
SHA-256 `1dfeb86eef24e97d514434a8e7f21dfeecd2aeee39391c132e743bb985a9fa09`.
It is retained locally, not included in Git. The release-readiness report indexes
new executions separately; it does not relabel old logs as runs of current code.
