"""Actual optional Inspect package tests; no provider, shell, or harness."""
import asyncio
import hashlib
import json

import pytest

pytest.importorskip("inspect_ai", reason="install agent_runtime/requirements-inspect.txt for Inspect integration")

from inspect_ai.log import read_eval_log

from agent_runtime.inspect_adapter import guarded_model, main, run
from agent_runtime.tests.test_evaluation_bundle import bundle, row


def test_real_inspect_logs_round_trip_all_groups_and_unknown(tmp_path, monkeypatch):
    source = tmp_path / "source"
    trusted = bundle(source)
    # Explicit local model must prevail even when a user's environment selects a provider.
    monkeypatch.setenv("INSPECT_EVAL_MODEL", "nonexistent/never-contact")
    result = run(source, tmp_path / "out", trusted)
    assert result["processing_ok"] is True, result
    assert result["model_generation_attempts"] == result["new_provider_requests"] == 0
    assert result["new_provider_input_tokens"] == result["new_provider_output_tokens"] == 0
    logs = {record["kind"]: record for record in result["logs"]}
    assert set(logs) == {"runtime", "agent", "environment"}
    assert logs["agent"]["samples"] == 2
    assert logs["agent"]["metrics"]["unknown_fraction"] == 0.5
    assert logs["agent"]["metrics"]["failure_fraction"] == 0.5
    assert logs["agent"]["strict_source_aggregate"]["success_rate"] is None
    for receipt in logs.values():
        path = tmp_path / "out" / receipt["log"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt["sha256"]
        log = read_eval_log(str(path))
        assert "GOLD_PATCH_SECRET_SENTINEL" not in log.model_dump_json()
        assert not log.stats.model_usage
    seal = json.loads((tmp_path / "out" / "seal.json").read_text())
    for item in seal:
        assert hashlib.sha256((tmp_path / "out" / item["path"]).read_bytes()).hexdigest() == item["sha256"]


def test_processing_error_samples_not_dropped_by_inspect(tmp_path):
    source = tmp_path / "source"
    trusted = bundle(source, [row("bad-source", "agent", None, processing="error")])
    result = run(source, tmp_path / "out", trusted)
    assert result["processing_ok"] is True, result
    assert result["source_processing_ok"] is False
    assert len(result["logs"]) == 1
    assert result["logs"][0]["metrics"]["unknown_fraction"] == 1.0


def test_no_source_reexecution_or_output_overwrite(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("archived input must never execute Runtime")
    monkeypatch.setattr("agent_runtime.runtime.Runtime.run", forbidden)
    monkeypatch.setattr("agent_runtime.runtime.Runtime.resume", forbidden)
    source = tmp_path / "source"
    trusted = bundle(source)
    result = run(source, tmp_path / "out", trusted)
    assert result["processing_ok"] is True, result
    before = (tmp_path / "out" / "adapter-result.json").read_bytes()
    with pytest.raises(ValueError, match="fresh"):
        run(source, tmp_path / "out", trusted)
    assert (tmp_path / "out" / "adapter-result.json").read_bytes() == before


def test_guarded_mock_generation_fails_and_is_counted():
    attempts = {"count": 0}
    model = guarded_model(attempts)
    with pytest.raises(RuntimeError, match="forbids model generation"):
        asyncio.run(model.generate("never generate"))
    assert attempts["count"] == 1


def test_inspect_log_failure_keeps_diagnostics(tmp_path, monkeypatch):
    source = tmp_path / "source"
    trusted = bundle(source)
    monkeypatch.setattr("agent_runtime.inspect_adapter.inspect_eval", lambda *_args, **_kwargs: [])
    result = run(source, tmp_path / "out", trusted)
    assert result["processing_ok"] is False
    assert "missing Inspect group" in result["error"]
    assert (tmp_path / "out" / "projected-input.json").exists()
    assert (tmp_path / "out" / "seal.json").exists()


def test_bad_source_cli_exit_two_without_output(tmp_path, monkeypatch):
    source = tmp_path / "source"
    bundle(source)
    monkeypatch.setattr("sys.argv", ["inspect_adapter", str(source), str(tmp_path / "out"), "--seal-sha256", "0" * 64])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


def test_unexpected_generate_in_solver_stops_adapter_and_is_recorded(tmp_path, monkeypatch):
    from inspect_ai.solver import generate
    source = tmp_path / "source"
    trusted = bundle(source)
    monkeypatch.setattr("agent_runtime.inspect_adapter.archived_result", generate)
    result = run(source, tmp_path / "out", trusted)
    assert result["processing_ok"] is False
    assert result["model_generation_attempts"] >= 1
    assert result["new_provider_requests"] == 0
    assert "attempted model generation" in result["error"]


def test_output_inside_source_rejected_without_changing_bundle(tmp_path):
    source = tmp_path / "source"
    trusted = bundle(source)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="outside the source"):
        run(source, source / "inspect-output", trusted)
    assert not (source / "inspect-output").exists()
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before
