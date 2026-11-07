"""Offline orchestration, not a real SWE-bench repair or official evaluation."""
from contextlib import nullcontext
import json

import pytest

pytest.importorskip("inspect_ai")
from agent_runtime import inspect_swe_runner as runner
from agent_runtime.benchmark_dataset import prepare
from agent_runtime.evaluation import _hash, _dump
from agent_runtime.tests.test_benchmark_dataset import fixture as dataset_fixture
from agent_runtime.tests.test_container_edits import setup_bridge, arguments, sha
from agent_runtime.workspace_verifier import WorkspaceVerifier, WorkspaceSnapshot, ValidationEvidence


def setup(tmp_path, monkeypatch):
    config, dataset = dataset_fixture(tmp_path)
    prepare(config, dataset)
    pin = _hash((dataset / "manifest.json").read_bytes())
    workspace, bridge = setup_bridge(tmp_path)
    backend = bridge.backend
    backend.instance_id = "repo__issue-1"
    backend.validator_id = "offline-candidate-probe"
    # Rebuild identities after selecting the fixture instance.
    for part in (bridge.reads, bridge.edits):
        part.identity["instance"] = backend.instance_id
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    def capture():
        data = (workspace / "app.py").read_bytes()
        return WorkspaceSnapshot(backend.source_id, backend.instance_id, backend.base_commit,
                                 sha(data), patch if data == b"value = 2\n" else "", data == b"value = 2\n")
    verifier = WorkspaceVerifier(goal="candidate_patch", workspace_id=backend.source_id,
        instance_id=backend.instance_id, base_commit=backend.base_commit, validator_id=backend.validator_id,
        freeze=nullcontext, capture=capture,
        validate=lambda snap: ValidationEvidence(snap.revision, snap.patch_sha256, backend.validator_id, 0, 1))
    backend.verifier = lambda: verifier
    import agent_runtime.container_edits as edits
    monkeypatch.setattr(edits, "DockerRepairBridge", lambda *a, **k: bridge)
    return dataset, pin, backend, patch


def test_pinned_instance_to_inspect_repair_and_prediction(tmp_path, monkeypatch):
    dataset, pin, backend, patch = setup(tmp_path, monkeypatch)
    responses = [{"tool": "container_read_file", "id": "read", "input": {"path": "app.py"}},
                 {"tool": "container_edit_file", "id": "edit", "input": arguments()}, {"text": "Fixed"}]
    report = runner.run_instance(dataset, pin, backend.instance_id, backend, tmp_path / "out",
        model="mockllm/runtime-bridge", mode="mock", max_turns=4, responses=responses, execute=True)
    assert report["processing_ok"] and report["candidate_validated"], report
    prediction = json.loads((tmp_path / "out/predictions.jsonl").read_text())
    assert prediction["model_patch"] == patch
    assert report["official_resolved"] is None and report["new_provider_requests"] == 0
    model_receipts = "".join(p.read_text() for p in (tmp_path / "out/model-run/repair/bridge-calls").glob("*.json"))
    assert "PRIVATE_GOLD_SENTINEL" not in model_receipts
    assert "PRIVATE_TEST_SENTINEL" not in model_receipts


def test_backend_mismatch_rejected_without_output(tmp_path, monkeypatch):
    dataset, pin, backend, _ = setup(tmp_path, monkeypatch)
    backend.base_commit = "f" * 40
    with pytest.raises(ValueError, match="instance/base"):
        runner.run_instance(dataset, pin, backend.instance_id, backend, tmp_path / "out", model="anthropic/explicit")
    assert not (tmp_path / "out").exists()


def test_preflight_does_not_create_binding_or_model(tmp_path, monkeypatch):
    dataset, pin, backend, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "repair_binding", lambda *a, **k: pytest.fail("binding created"))
    monkeypatch.setattr(runner, "model_run", lambda *a, **k: pytest.fail("model executed"))
    result = runner.run_instance(dataset, pin, backend.instance_id, backend, tmp_path / "out", model="anthropic/explicit")
    assert result["status"] == "PREFLIGHT_ONLY"
    assert not (tmp_path / "out").exists()


def test_failed_instance_keeps_empty_prediction(tmp_path, monkeypatch):
    dataset, pin, backend, _ = setup(tmp_path, monkeypatch)
    result = runner.run_instance(dataset, pin, backend.instance_id, backend, tmp_path / "out",
        model="mockllm/runtime-bridge", mode="mock", max_turns=2,
        responses=[{"text": "TASK_GAVE_UP"}], execute=True)
    assert result["processing_ok"] and not result["candidate_validated"]
    assert json.loads((tmp_path / "out/predictions.jsonl").read_text())["model_patch"] == ""


def test_model_startup_failure_preserves_instance_and_diagnostics(tmp_path, monkeypatch):
    dataset, pin, backend, _ = setup(tmp_path, monkeypatch)
    def fail(*a, **k):
        raise RuntimeError("log startup failed")
    monkeypatch.setattr(runner, "model_run", fail)
    result = runner.run_instance(dataset, pin, backend.instance_id, backend, tmp_path / "out",
        model="mockllm/runtime-bridge", mode="mock", responses=[{"text": "stop"}], execute=True)
    assert not result["processing_ok"] and "log startup failed" in result["error"]
    assert json.loads((tmp_path / "out/predictions.jsonl").read_text())["model_patch"] == ""
    assert (tmp_path / "out/seal.json").exists()
    with pytest.raises(ValueError, match="fresh output"):
        runner.run_instance(dataset, pin, backend.instance_id, backend, tmp_path / "out",
            model="mockllm/runtime-bridge", mode="mock", responses=[{"text": "stop"}], execute=True)


@pytest.mark.parametrize("mutation", ["base", "hash", "tests", "exit"])
def test_prediction_rejects_inconsistent_candidate_evidence(tmp_path, mutation):
    from types import SimpleNamespace
    backend = SimpleNamespace(source_id="source", instance_id="instance", base_commit="a" * 40, validator_id="validator")
    evidence = {"goal": "candidate_patch", "workspace": {"workspace_id": "source", "instance_id": "instance",
        "base_commit": backend.base_commit, "revision": "revision", "patch": "patch", "patch_applies": True},
        "validation": {"revision": "revision", "patch_sha256": sha(b"patch"), "validator_id": "validator", "exit_code": 0, "tests_run": 1}}
    if mutation == "base": evidence["workspace"]["base_commit"] = "b" * 40
    elif mutation == "hash": evidence["validation"]["patch_sha256"] = "0" * 64
    elif mutation == "tests": evidence["validation"]["tests_run"] = 0
    else: evidence["validation"]["exit_code"] = True
    path = tmp_path / "repair/workspace/.agent_runtime/verification/completion.json"
    path.parent.mkdir(parents=True)
    _dump(path, evidence)
    with pytest.raises(ValueError): runner.candidate_patch(tmp_path, backend)


@pytest.mark.parametrize("responses", [
    [{"text": 1}],
    [{"tool": "bash", "id": "shell", "input": {"command": "whoami"}}],
    [{"text": "ok", "extra": "unexpected"}],
    [{"text": "ok", "usage": {"input_tokens": True, "output_tokens": 0}}],
    [{"text": "x" * 65537}],
    [{"text": "ok"}] * 17,
])
@pytest.mark.parametrize("execute", [False, True])
def test_invalid_script_rejected_before_binding_or_output(tmp_path, monkeypatch, responses, execute):
    dataset, pin, backend, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "repair_binding", lambda *a, **k: pytest.fail("binding created"))
    monkeypatch.setattr(runner, "model_run", lambda *a, **k: pytest.fail("model executed"))
    with pytest.raises(ValueError):
        runner.run_instance(dataset, pin, backend.instance_id, backend, tmp_path / "out",
            model="mockllm/runtime-bridge", mode="mock", responses=responses, execute=execute)
    assert not (tmp_path / "out").exists()
