"""Simulated official process/report integration, not official benchmark results."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_runtime import swe_grading as grading
from agent_runtime.benchmark_dataset import prepare
from agent_runtime.evaluation import _dump, _hash
from agent_runtime.tests.test_benchmark_dataset import fixture


def report():
    return dict(schema_version=2, total_instances=1, submitted_instances=1,
        completed_instances=1, resolved_instances=1, unresolved_instances=0,
        empty_patch_instances=0, error_instances=0, submitted_ids=["repo__issue-1"],
        completed_ids=["repo__issue-1"], resolved_ids=["repo__issue-1"],
        unresolved_ids=[], empty_patch_ids=[], error_ids=[], incomplete_ids=[])


def setup(tmp_path, monkeypatch):
    config, dataset = fixture(tmp_path)
    prepare(config, dataset)
    predictions = config.parent / "predictions.jsonl"
    predictions.write_text(json.dumps(dict(instance_id="repo__issue-1", model_name_or_path="mock/model", model_patch="patch")), encoding="utf-8")
    harness = tmp_path / "harness"
    harness.mkdir()
    monkeypatch.setattr(grading, "harness_identity", Mock())
    monkeypatch.setattr(grading.sys, "platform", "linux")
    args = (dataset, _hash((dataset / "manifest.json").read_bytes()), predictions,
            _hash(predictions.read_bytes()), harness, tmp_path / "out")
    return args


def test_preflight_never_invokes_grader(tmp_path, monkeypatch):
    args = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(grading.subprocess, "run", Mock(side_effect=AssertionError("invoked")))
    assert grading.grade(*args)["status"] == "PREFLIGHT_ONLY"
    assert not args[-1].exists()


def test_simulated_report_is_bound_to_invocation(tmp_path, monkeypatch):
    args = setup(tmp_path, monkeypatch)
    def run(command, **kwargs):
        assert "-I" in command and "--instance_ids" in command
        assert kwargs["cwd"] == args[-1]
        _dump(args[-1] / "mock__model.dataset.json", report())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(grading.subprocess, "run", run)
    result = grading.grade(*args, execute=True)
    assert result["status"] == "REPORTED"
    assert result["official_resolved"] == {"repo__issue-1": True}
    assert result["new_provider_requests"] == 0
    assert grading.harness_identity.call_count == 2


@pytest.mark.parametrize("failure", ["exit", "missing", "timeout"])
def test_failed_invocation_never_claims_resolved(tmp_path, monkeypatch, failure):
    args = setup(tmp_path, monkeypatch)
    def run(*a, **kw):
        if failure == "timeout":
            raise grading.subprocess.TimeoutExpired("harness", 1)
        return SimpleNamespace(returncode=1 if failure == "exit" else 0)
    monkeypatch.setattr(grading.subprocess, "run", run)
    result = grading.grade(*args, execute=True)
    assert result["status"] == "FAILED" and result["official_resolved"] is None
    assert result["manual_recovery_required"] and result["new_official_evaluations"] is None
    assert (args[-1] / "result.json").exists()
    assert (args[-1] / "predictions.jsonl").read_bytes() == args[2].read_bytes()


@pytest.mark.parametrize("mutation", ["duplicate", "foreign", "count", "overlap"])
def test_report_inconsistency_rejected(tmp_path, mutation):
    value = report()
    if mutation == "duplicate": value["submitted_ids"] *= 2
    if mutation == "foreign": value["resolved_ids"] = ["other"]
    if mutation == "count": value["resolved_instances"] = True
    if mutation == "overlap": value["error_ids"] = ["repo__issue-1"]
    path = tmp_path / "report.json"
    _dump(path, value)
    with pytest.raises(ValueError):
        grading.read_report(path, ["repo__issue-1"])


def test_empty_prediction_remains_in_denominator(tmp_path):
    value = report()
    value.update(completed_instances=0, resolved_instances=0, empty_patch_instances=1,
                 completed_ids=[], resolved_ids=[], empty_patch_ids=["repo__issue-1"])
    path = tmp_path / "report.json"
    _dump(path, value)
    assert grading.read_report(path, ["repo__issue-1"]) == {"repo__issue-1": False}


def test_pin_mismatch_rejected(tmp_path, monkeypatch):
    mock = Mock(return_value=SimpleNamespace(stdout="wrong\n"))
    monkeypatch.setattr(grading.subprocess, "run", mock)
    with pytest.raises(ValueError, match="pinned"):
        grading.harness_identity(tmp_path, "a" * 40)
