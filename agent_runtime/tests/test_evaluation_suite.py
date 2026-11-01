"""New orchestration contracts; existing accepted evaluator suites are not rerun."""
import json
from pathlib import Path

import pytest

from agent_runtime import evaluation_suite as suite
from agent_runtime.evaluation import _dump, _hash


def manifest(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    child = {"schema_version": "evaluation-plan-v1", "run_id": "child", "cases": [
        {"id": "a", "mode": "scripted_runtime", "model": [{"text": "done"}],
         "checks": {"status": "completed"}}]}
    _dump(plans / "child.json", child)
    plan = {"schema_version": suite.SCHEMA, "run_id": "suite", "jobs": [
        {"id": "core", "engine": "evaluation", "plan": "child.json",
         "sha256": _hash((plans / "child.json").read_bytes())}]}
    _dump(plans / "suite.json", plan)
    return plans / "suite.json", tmp_path / "output"


def test_execute_then_reuse_without_invoking_runtime(tmp_path, monkeypatch):
    path, output = manifest(tmp_path)
    first = suite.run(path, output)
    assert first["processing_ok"]
    def forbidden(*args):
        pytest.fail("completed execution was rerun")
    monkeypatch.setattr(suite, "execute", forbidden)
    second = suite.run(path, output)
    assert second["processing_ok"]
    assert second["jobs"][0]["disposition"] == "reused"
    assert len(list((output / "attempts").glob("*.json"))) == 2


def test_tampered_evidence_blocks_without_reexecution(tmp_path, monkeypatch):
    path, output = manifest(tmp_path)
    assert suite.run(path, output)["processing_ok"]
    (output / "jobs/core/report.json").write_text("{}")
    monkeypatch.setattr(suite, "execute", lambda *a: pytest.fail("reran tampered bundle"))
    result = suite.run(path, output)
    assert not result["processing_ok"]
    assert "mismatch" in result["jobs"][0]["error"]


def test_partial_execution_preserved_and_never_retried(tmp_path, monkeypatch):
    path, output = manifest(tmp_path)
    def crash(engine, plan, root, source, digest):
        root.mkdir()
        (root / "diagnostic.txt").write_text("partial")
        raise RuntimeError("interrupted")
    monkeypatch.setattr(suite, "execute", crash)
    assert not suite.run(path, output)["processing_ok"]
    monkeypatch.setattr(suite, "execute", lambda *a: pytest.fail("retried partial task"))
    assert "partial child" in suite.run(path, output)["jobs"][0]["error"]
    assert (output / "jobs/core/diagnostic.txt").read_text() == "partial"


@pytest.mark.parametrize("mutation", ["digest", "duplicate", "forward", "traversal", "live"])
def test_invalid_manifest_fails_before_output(tmp_path, mutation):
    path, output = manifest(tmp_path)
    plan = json.loads(path.read_text())
    job = plan["jobs"][0]
    if mutation == "digest":
        job["sha256"] = "0" * 64
    elif mutation == "duplicate":
        plan["jobs"].append(dict(job, id="CORE"))
    elif mutation == "forward":
        plan["jobs"].insert(0, {"id": "archive", "engine": "inspect_archive", "source_job": "core"})
    elif mutation == "traversal":
        job["plan"] = "../child.json"
    else:
        job["engine"] = "live"
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError):
        suite.run(path, output)
    assert not output.exists()


def test_changed_environment_requires_new_output(tmp_path, monkeypatch):
    path, output = manifest(tmp_path)
    suite.run(path, output)
    monkeypatch.setattr(suite, "identity", lambda: {"changed": True})
    with pytest.raises(ValueError, match="environment changed"):
        suite.run(path, output)


def test_exclusive_lock_blocks_concurrent_invocation(tmp_path):
    path, output = manifest(tmp_path)
    suite.run(path, output)
    (output / "running.lock").write_text("owner")
    with pytest.raises(FileExistsError):
        suite.run(path, output)
    assert (output / "running.lock").read_text() == "owner"


def test_frozen_input_tamper_rejected(tmp_path):
    path, output = manifest(tmp_path)
    suite.run(path, output)
    (output / "inputs/core.json").write_text("{}")
    with pytest.raises(ValueError, match="frozen child"):
        suite.run(path, output)


def test_import_existing_bundle_never_executes(tmp_path, monkeypatch):
    path, output = manifest(tmp_path)
    suite.run(path, output)
    bundle = output / "jobs/core"
    document = {"schema_version": suite.SCHEMA, "run_id": "import", "jobs": [{
        "id": "old", "engine": "evaluation", "bundle": str(bundle),
        "seal_sha256": _hash((bundle / "seal.json").read_bytes())}]}
    imported = path.parent / "import.json"
    _dump(imported, document)
    monkeypatch.setattr(suite, "execute", lambda *a: pytest.fail("historical bundle executed"))
    result = suite.run(imported, tmp_path / "import-output")
    assert result["processing_ok"] and result["accepted"]
    assert result["jobs"][0]["disposition"] == "imported"


@pytest.mark.parametrize("passed,total,expected", [(0, 1, False), (1, 1, True), (0, 0, False)])
def test_processing_is_not_regression_acceptance(passed, total, expected):
    assert suite.acceptance({"processing_ok": True, "runtime_cases": {
        "total": total, "passed": passed}}, "inspect_runtime") is expected


def test_unknown_agent_outcome_prevents_acceptance():
    assert suite.acceptance({"processing_ok": True, "aggregates": {
        "agent": {"total": 1, "failed": 0, "unknown": 1}}}, "evaluation") is None


def test_model_engine_dispatches_with_explicit_execution(tmp_path, monkeypatch):
    from agent_runtime import inspect_model_eval
    calls = []
    monkeypatch.setattr(inspect_model_eval, "run", lambda *a, **k: calls.append((a, k)) or {"ok": True})
    plan, output = tmp_path / "plan", tmp_path / "out"
    assert suite.execute("inspect_model", plan, output, None, None) == {"ok": True}
    assert calls == [((plan, output), {"execute": True})]


def test_model_task_failure_remains_failure():
    assert suite.acceptance({"processing_ok": True, "fixture_outcomes": {
        "planned": 2, "passed": 1, "failed": 1, "unknown": 0}}, "inspect_model") is False


def test_archive_artifacts_rejected_before_any_job_runs(tmp_path):
    path, output = manifest(tmp_path)
    child_path = path.parent / "child.json"
    child = json.loads(child_path.read_text())
    child["cases"][0]["artifacts"] = {"summary": {"path": "unexpected"}}
    child_path.write_text(json.dumps(child))
    plan = json.loads(path.read_text())
    plan["jobs"][0]["sha256"] = _hash(child_path.read_bytes())
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="archive artifacts"):
        suite.run(path, output)
    assert not output.exists()
