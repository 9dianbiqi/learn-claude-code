"""Evaluation integrity tests: task outcomes must not become processing claims."""
import hashlib
import json
from pathlib import Path

import pytest

from agent_runtime.evaluation import _aggregate, main, run


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def plan(tmp_path, cases):
    path = tmp_path / "plan.json"
    write_json(path, {"schema_version": "evaluation-plan-v1", "run_id": "test-evaluation", "cases": cases})
    return path


def scripted(**changes):
    case = {"id": "write", "mode": "scripted_runtime", "files": {"code.txt": "old"},
            "model": [{"tool": "read_file", "id": "read-1", "input": {"path": "code.txt"}},
                      {"tool": "write_file", "id": "write-1", "input": {"path": "code.txt", "content": "new"}},
                      {"text": "done", "usage": {"input_tokens": 10, "output_tokens": 3}}],
            "checks": {"status": "completed", "files": {"code.txt": "new"}}}
    case.update(changes)
    return case


def artifact(tmp_path, name, value, raw=False):
    path = tmp_path / name
    if raw:
        path.write_text(value, encoding="utf-8")
    else:
        write_json(path, value)
    return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def agent(tmp_path, patch="", usage=True, calls=1):
    prediction = {"instance_id": "sympy__sympy-19346", "model_name_or_path": "historical-model", "model_patch": patch}
    records = [{"model_call_id": f"call-{i}", "task_id": "task-old", "status": "succeeded",
                "response": {"usage": {"input_tokens": 5, "output_tokens": 2} if usage else {}}} for i in range(calls)]
    return {"id": "agent", "mode": "agent_archive", "source_run_id": "old-agent",
            "instance_id": prediction["instance_id"], "task_id": "task-old", "model_mode": "live",
            "artifacts": {
                "summary": artifact(tmp_path, "summary.json", {"task_id": "task-old", "status": "completed", "model_calls": calls}),
                "model_calls": artifact(tmp_path, "calls.json", records),
                "prediction": artifact(tmp_path, "prediction.jsonl", json.dumps(prediction) + "\n", raw=True)}}


def official(tmp_path, scope="gold_control", resolved=True):
    case = {"id": "gold", "mode": "official_archive", "source_run_id": "gold-old",
            "instance_id": "sympy__sympy-20590", "scope": scope,
            "dataset_revision": "a" * 40, "base_commit": "b" * 40, "harness_commit": "c" * 40}
    detail = {"resolved": resolved, "patch_exists": True, "patch_is_None": False,
              "patch_successfully_applied": True, "tests_status": {
                  "FAIL_TO_PASS": {"success": ["a"], "failure": []},
                  "PASS_TO_PASS": {"success": ["b"], "failure": []}}}
    refs = {
        "report": artifact(tmp_path, "official.json", {case["instance_id"]: detail}),
        "execution": artifact(tmp_path, "execution.json", {"run_id": case["source_run_id"], "exit_code": 0}),
        "prediction": artifact(tmp_path, "gold.jsonl", json.dumps({"instance_id": case["instance_id"],
            "model_name_or_path": "gold", "model_patch": "diff --git a/a b/a\n"}) + "\n", raw=True)}
    receipt = {k: case[k] for k in ("source_run_id", "instance_id", "scope", "dataset_revision", "base_commit", "harness_commit")}
    receipt.update({role + "_sha256": ref["sha256"] for role, ref in refs.items()})
    refs["receipt"] = artifact(tmp_path, "receipt.json", receipt)
    case["artifacts"] = refs
    return case


def test_new_scripted_execution_retains_trace_and_seal(tmp_path):
    report = run(plan(tmp_path, [scripted()]), tmp_path / "out")
    row = report["results"][0]
    assert row["runtime_case_pass"] is True
    assert row["task_success"] is None and row["provider_input_tokens"] is None
    assert row["model_calls_recorded"] == 3
    assert report["new_provider_requests"] == report["new_official_evaluations"] == 0
    root = tmp_path / "out"
    seal = json.loads((root / "seal.json").read_text())
    assert any(p["path"].endswith("trace.jsonl") for p in seal)
    assert any(p["path"].endswith("runtime.db") for p in seal)
    for entry in seal:
        assert hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]


def test_oracle_failure_not_processing_failure(tmp_path):
    case = scripted(checks={"status": "completed", "files": {"code.txt": "unmet"}})
    report = run(plan(tmp_path, [case]), tmp_path / "out")
    assert report["processing_ok"] is True
    assert report["aggregates"]["runtime"] == {"total": 1, "passed": 0, "failed": 1, "unknown": 0, "success_rate": 0.0}


def test_untriggered_fault_does_not_run_a_second_task(tmp_path):
    case = scripted(fault_point="nonexistent-point")
    row = run(plan(tmp_path, [case]), tmp_path / "out")["results"][0]
    assert row["model_calls_recorded"] == 3
    assert row["runtime_case_pass"] is False and row["fault_triggered"] is False


def test_actual_crash_recovers_once(tmp_path):
    case = scripted(fault_point="after_model_response", recovery_model=scripted()["model"][1:])
    row = run(plan(tmp_path, [case]), tmp_path / "out")["results"][0]
    assert row["processing"] == "ok", row
    assert row["fault_triggered"] is True and row["runtime_case_pass"] is True


@pytest.mark.parametrize("path", ["../escape", "C:/escape", "/escape", "x\\escape", ".agent_runtime/runtime.db", "a:ads", "CON", "x/../a"])
def test_reject_unsafe_script_paths_before_output(tmp_path, path):
    case = scripted(files={path: "no"})
    with pytest.raises(ValueError):
        run(plan(tmp_path, [case]), tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("tool", ["bash", "spawn_subagent", "container_bash", "glob"])
def test_reject_nonlocal_or_unbounded_script_tools(tmp_path, tool):
    case = scripted(model=[{"tool": tool, "id": "bad", "input": {"path": "a"}}])
    with pytest.raises(ValueError):
        run(plan(tmp_path, [case]), tmp_path / "out")


def test_empty_agent_patch_does_not_inherit_runtime_completed(tmp_path):
    report = run(plan(tmp_path, [agent(tmp_path)]), tmp_path / "out")
    row = report["results"][0]
    assert row["runtime_status"] == "completed" and row["task_success"] is False
    assert row["official_resolved"] is None and row["official_status"] == "not_evaluated"
    assert row["provider_input_tokens"] == 5 and row["provider_output_tokens"] == 2
    assert row["invariant_violation_count"] is None


@pytest.mark.parametrize("usage,calls,model_mode", [(False, 1, "live"), (True, 0, "live"), (True, 1, "scripted")])
def test_unknown_provider_usage_is_not_zero(tmp_path, usage, calls, model_mode):
    case = agent(tmp_path, patch="patch", usage=usage, calls=calls)
    case["model_mode"] = model_mode
    row = run(plan(tmp_path, [case]), tmp_path / "out")["results"][0]
    assert row["provider_input_tokens"] is None and row["task_success"] is None


def test_failed_abandoned_calls_remain_in_denominator_and_missing_usage(tmp_path):
    case = agent(tmp_path, calls=2)
    records = json.loads((tmp_path / "calls.json").read_text())
    records[1].update(status="abandoned", response=None)
    case["artifacts"]["model_calls"] = artifact(tmp_path, "calls.json", records)
    row = run(plan(tmp_path, [case]), tmp_path / "out")["results"][0]
    assert row["model_calls_recorded"] == 2 and row["provider_input_tokens"] is None


def test_gold_official_success_not_agent_success(tmp_path):
    report = run(plan(tmp_path, [official(tmp_path)]), tmp_path / "out")
    row = report["results"][0]
    assert row["official_resolved"] is True and row["task_success"] is None
    assert report["aggregates"]["agent"]["total"] == 0
    assert report["aggregates"]["agent"]["success_rate"] is None


def test_official_agent_failure_is_a_failure(tmp_path):
    report = run(plan(tmp_path, [official(tmp_path, scope="agent", resolved=False)]), tmp_path / "out")
    assert report["results"][0]["task_success"] is False
    assert report["aggregates"]["agent"]["success_rate"] == 0


def test_reused_official_execution_cannot_be_a_new_result(tmp_path):
    case = official(tmp_path, scope="agent")
    case["source_run_id"] = "new-run"
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    receipt["source_run_id"] = "new-run"
    case["artifacts"]["receipt"] = artifact(tmp_path, "receipt.json", receipt)
    report = run(plan(tmp_path, [case]), tmp_path / "out")
    row = report["results"][0]
    assert row["processing"] == "error" and row["official_resolved"] is None
    assert "reused report" in row["error"]
    assert report["aggregates"]["agent"]["unknown"] == 1


def test_invalid_receipt_remains_in_planned_denominator(tmp_path):
    case = official(tmp_path)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    receipt["report_sha256"] = "0" * 64
    case["artifacts"]["receipt"] = artifact(tmp_path, "receipt.json", receipt)
    report = run(plan(tmp_path, [case]), tmp_path / "out")
    assert report["processing_ok"] is False
    assert report["aggregates"]["environment"]["total"] == 1
    assert report["aggregates"]["environment"]["unknown"] == 1


def test_digest_corruption_prevents_any_execution(tmp_path):
    case = agent(tmp_path)
    (tmp_path / "summary.json").write_text("corrupt")
    with pytest.raises(ValueError, match="digest"):
        run(plan(tmp_path, [scripted(), case]), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_no_overwrite_of_previous_bundle(tmp_path):
    source = plan(tmp_path, [scripted()])
    run(source, tmp_path / "out")
    before = (tmp_path / "out" / "report.json").read_bytes()
    with pytest.raises(ValueError, match="fresh"):
        run(source, tmp_path / "out")
    assert before == (tmp_path / "out" / "report.json").read_bytes()


def test_no_empty_suite_success_or_duplicate_subjects(tmp_path):
    with pytest.raises(ValueError):
        run(plan(tmp_path, []), tmp_path / "out")
    a = agent(tmp_path)
    b = dict(a, id="another")
    with pytest.raises(ValueError, match="duplicate archived"):
        run(plan(tmp_path, [a, b]), tmp_path / "out")
    assert _aggregate([])["runtime"]["success_rate"] is None


def test_symlink_input_rejected(tmp_path):
    case = agent(tmp_path)
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(tmp_path / "summary.json")
    except OSError:
        pytest.skip("symlink privilege unavailable")
    case["artifacts"]["summary"]["path"] = "linked.json"
    with pytest.raises(ValueError, match="reparse"):
        run(plan(tmp_path, [case]), tmp_path / "out")


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text('{"schema_version":"evaluation-plan-v1","schema_version":"other"}')
    with pytest.raises(ValueError, match="duplicate JSON"):
        run(path, tmp_path / "out")


@pytest.mark.parametrize("value", [[], None, {"schema_version": "evaluation-plan-v1", "run_id": "x", "cases": [None]}])
def test_invalid_top_level_shapes_exit_two(tmp_path, monkeypatch, value):
    path = tmp_path / "invalid.json"
    write_json(path, value)
    monkeypatch.setattr("sys.argv", ["evaluation", str(path), str(tmp_path / "out")])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("changes", [{"model": ["bad"]}, {"checks": []}, {"model": {}},
    {"model": [{"text": "x", "usage": []}]}, {"model": [{"tool": "write_file", "id": "a", "input": []}]}])
def test_invalid_script_shapes_rejected_before_execution(tmp_path, changes):
    with pytest.raises(ValueError):
        run(plan(tmp_path, [scripted(**changes)]), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_contradictory_official_success_stays_unknown(tmp_path):
    case = official(tmp_path)
    report = json.loads((tmp_path / "official.json").read_text())
    report[case["instance_id"]]["tests_status"]["FAIL_TO_PASS"]["failure"] = ["still-failing"]
    case["artifacts"]["report"] = artifact(tmp_path, "official.json", report)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    receipt["report_sha256"] = case["artifacts"]["report"]["sha256"]
    case["artifacts"]["receipt"] = artifact(tmp_path, "receipt.json", receipt)
    result = run(plan(tmp_path, [case]), tmp_path / "out")
    row = result["results"][0]
    assert row["processing"] == "error" and row["official_resolved"] is None
    assert "contradicts" in row["error"]
    assert result["aggregates"]["environment"]["unknown"] == 1


def test_environment_flag_prevents_agent_score_relabel(tmp_path):
    case = official(tmp_path, scope="agent")
    case["artifacts"]["execution"] = artifact(tmp_path, "execution.json", {
        "run_id": case["source_run_id"], "exit_code": 0, "not_a_model_score": True})
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    receipt["execution_sha256"] = case["artifacts"]["execution"]["sha256"]
    case["artifacts"]["receipt"] = artifact(tmp_path, "receipt.json", receipt)
    row = run(plan(tmp_path, [case]), tmp_path / "out")["results"][0]
    assert row["processing"] == "error" and row["task_success"] is None


def test_cli_zero_means_processed_not_agent_passed(tmp_path, monkeypatch, capsys):
    path = plan(tmp_path, [agent(tmp_path)])
    monkeypatch.setattr("sys.argv", ["evaluation", str(path), str(tmp_path / "out")])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["aggregates"]["agent"]["failed"] == 1


def test_cli_one_for_bad_evidence_keeps_unknown(tmp_path, monkeypatch):
    case = official(tmp_path)
    case["source_run_id"] = "mismatch"
    path = plan(tmp_path, [case])
    monkeypatch.setattr("sys.argv", ["evaluation", str(path), str(tmp_path / "out")])
    assert main() == 1


@pytest.mark.parametrize("artifacts", [[], "", None])
def test_nonobject_artifacts_cli_exit_two_before_output(tmp_path, monkeypatch, artifacts):
    path = plan(tmp_path, [scripted(artifacts=artifacts)])
    monkeypatch.setattr("sys.argv", ["evaluation", str(path), str(tmp_path / "out")])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()
