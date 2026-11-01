"""Acceptance composition with simulated transport; no official evaluation."""
from contextlib import contextmanager
import json
from unittest.mock import Mock

import pytest

pytest.importorskip("inspect_ai")
from agent_runtime import swe_acceptance as acceptance
from agent_runtime.tests.test_inspect_swe_runner import setup, arguments
from agent_runtime.tests.test_swe_environment import archive
from agent_runtime.docker_workspace import CommandProbe
import tarfile


def prepare(tmp_path, monkeypatch):
    dataset, pin, backend, patch = setup(tmp_path, monkeypatch)
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base = base_dir / "base.tar"
    sha = archive(base, [("app.py", tarfile.REGTYPE)])
    calls = []
    @contextmanager
    def provision(*a, **kw):
        calls.append("created")
        try:
            yield backend
        except Exception:
            calls.append("retained")
            raise
        else:
            calls.append("removed")
    monkeypatch.setattr(acceptance, "provision", provision)
    kwargs = dict(client=Mock(), image="sha256:" + "a" * 64, base_archive=base,
        base_archive_sha256=sha, probes=[CommandProbe(("python", "-V"), "0" * 64)],
        responses=[{"tool": "container_read_file", "id": "read", "input": {"path": "app.py"}},
                   {"tool": "container_edit_file", "id": "edit", "input": arguments()}, {"text": "Fixed"}],
        max_turns=4)
    return (dataset, pin, backend.instance_id, tmp_path / "out"), kwargs, calls, patch


def test_real_inspect_mock_candidate_composes_with_lifecycle(tmp_path, monkeypatch):
    args, kwargs, calls, patch = prepare(tmp_path, monkeypatch)
    result = acceptance.run(*args, **kwargs, execute=True)
    assert result["status"] == "CANDIDATE_FINISHED", result
    assert result["candidate_validated"] and calls == ["created", "removed"]
    assert json.loads((args[-1] / "predictions.jsonl").read_text())["model_patch"] == patch
    assert result["new_provider_requests"] == 0 and result["official_resolved"] is None


def test_preflight_never_creates_environment(tmp_path, monkeypatch):
    args, kwargs, calls, _ = prepare(tmp_path, monkeypatch)
    assert acceptance.run(*args, **kwargs)["status"] == "PREFLIGHT_ONLY"
    assert calls == [] and not args[-1].exists()


@pytest.mark.parametrize("stage", ["provision", "binding", "review", "cleanup"])
def test_failure_retains_diagnostics_and_skips_grading(tmp_path, monkeypatch, stage):
    args, kwargs, calls, _ = prepare(tmp_path, monkeypatch)
    original = acceptance.run_instance
    def candidate(*a, **kw):
        if not kw.get("execute"):
            return original(*a, **kw)
        if stage == "binding":
            raise RuntimeError("binding startup failed")
        if stage == "review":
            return {"processing_ok": True, "manual_recovery_required": True}
        path = a[4]
        path.mkdir(parents=True)
        raw = json.dumps(dict(instance_id=args[2], model_name_or_path="mockllm/runtime-bridge", model_patch=""))
        (path / "predictions.jsonl").write_text(raw)
        return dict(processing_ok=True, manual_recovery_required=False, candidate_validated=False,
                    prediction_sha256=acceptance._hash(raw.encode()))
    monkeypatch.setattr(acceptance, "run_instance", candidate)
    if stage in {"provision", "cleanup"}:
        @contextmanager
        def broken(*a, **kw):
            if stage == "provision": raise RuntimeError("engine unavailable")
            yield object()
            raise RuntimeError("cleanup uncertain")
        monkeypatch.setattr(acceptance, "provision", broken)
    monkeypatch.setattr(acceptance, "harness_identity", Mock())
    grader = Mock(side_effect=AssertionError("must not grade"))
    monkeypatch.setattr(acceptance, "grade", grader)
    result = acceptance.run(*args, **kwargs, execute=True, official=True, harness=tmp_path / "harness")
    assert result["status"] == "BLOCKED" and result["error"]
    assert json.loads((args[-1] / "predictions.jsonl").read_text())["model_patch"] == ""
    assert result["official_resolved"] is None
    grader.assert_not_called()
    if stage in {"binding", "review"}: assert calls == ["created", "retained"]


def test_failed_task_empty_prediction_can_be_scored_after_cleanup(tmp_path, monkeypatch):
    args, kwargs, calls, _ = prepare(tmp_path, monkeypatch)
    kwargs["responses"] = [{"text": "TASK_GAVE_UP"}]
    monkeypatch.setattr(acceptance, "harness_identity", Mock())
    def grade(*a, **kw):
        assert calls[-1] == "removed"
        assert a[2].parent != a[5].parent  # grading inputs and output are disjoint
        assert json.loads(a[2].read_text())["model_patch"] == ""
        return dict(status="REPORTED", official_resolved={args[2]: False},
                    new_official_evaluations=1, manual_recovery_required=False, error=None)
    monkeypatch.setattr(acceptance, "grade", grade)
    result = acceptance.run(*args, **kwargs, execute=True, official=True, harness=tmp_path / "harness")
    assert result["status"] == "REPORTED", result
    assert result["official_resolved"] == {args[2]: False}


def test_existing_output_never_reexecutes(tmp_path, monkeypatch):
    args, kwargs, calls, _ = prepare(tmp_path, monkeypatch)
    args[-1].mkdir()
    with pytest.raises(ValueError, match="fresh"):
        acceptance.run(*args, **kwargs, execute=True)
    assert not calls


def test_preflight_rejects_mutable_image(tmp_path, monkeypatch):
    args, kwargs, calls, _ = prepare(tmp_path, monkeypatch)
    kwargs["image"] = "image:latest"
    with pytest.raises(ValueError, match="immutable"):
        acceptance.run(*args, **kwargs)
    assert not calls and not args[-1].exists()


def test_runner_propagates_review_even_with_processed_evidence(tmp_path, monkeypatch):
    from agent_runtime import inspect_swe_runner as runner
    args, kwargs, calls, _ = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "model_run", lambda *a, **kw: {
        "processing_ok": True, "new_provider_requests": 0,
        "fixture_outcomes": {"passed": 0}, "results": [{"id": "repair", "runtime_status": "needs_review"}]})
    result = acceptance.run(*args, **kwargs, execute=True)
    assert result["status"] == "BLOCKED" and result["manual_recovery_required"]
    assert calls == ["created", "retained"]
    candidate = json.loads((args[-1] / "candidate/result.json").read_text())
    assert candidate["processing_ok"] and candidate["runtime_status"] == "needs_review"
    assert candidate["manual_recovery_required"]


@pytest.mark.parametrize("execute", [False, True])
def test_invalid_script_rejected_before_environment_creation(tmp_path, monkeypatch, execute):
    args, kwargs, calls, _ = prepare(tmp_path, monkeypatch)
    kwargs["responses"] = [{"tool": "bash", "id": "shell", "input": {"command": "whoami"}}]
    with pytest.raises(ValueError, match="native tools"):
        acceptance.run(*args, **kwargs, execute=execute)
    assert not calls and not args[-1].exists()
