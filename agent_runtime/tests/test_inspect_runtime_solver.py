"""Real Inspect → Runtime → tools → Inspect regression. No paid model calls."""
import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai", reason="optional Inspect dependency")

from agent_runtime.inspect_runtime_solver import main, run, usage_summary, check_logs, fixture_model, runtime_solver

EXAMPLE = Path(__file__).parents[1] / "evals" / "inspect-runtime-v1.json"


def frozen_plan(tmp_path, document=None):
    path = tmp_path / "input" / "plan.json"
    path.parent.mkdir()
    if document is None:
        document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def simple_plan():
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    document["cases"] = document["cases"][:1]
    return document


def test_actual_inspect_runtime_suite_and_persisted_recovery(tmp_path):
    report = run(frozen_plan(tmp_path), tmp_path / "out")
    assert report["processing_ok"] is True, report
    assert report["runtime_cases"] == {"total": 8, "passed": 8}
    rows = {r["id"]: r for r in report["results"]}
    assert rows["edit-feedback"]["bridge_calls"] == 3
    assert rows["early-stop"]["runtime_status"] == "failed"
    assert json.loads(rows["early-stop"]["error"])["code"] == "UNRESOLVED_STOP_LIMIT"
    assert json.loads(rows["declined"]["error"])["code"] == "AGENT_GAVE_UP"
    assert rows["turn-budget"]["bridge_calls"] == rows["turn-budget"]["consumed_turns"] == 1
    assert json.loads(rows["turn-budget"]["error"])["code"] == "BUDGET_EXHAUSTED"
    assert rows["write-review"]["runtime_status"] == "needs_review"
    assert rows["error-feedback"]["bridge_calls"] == 4
    assert rows["lost-response"]["fault_triggered"] is True
    assert rows["lost-response"]["runtime_abandoned_records"] == 1
    assert rows["lost-response"]["synthetic_inspect_usage"]["input_tokens"] == 4
    confirmed = rows["lost-response"]["runtime_confirmed_synthetic_usage"]
    assert confirmed["input_tokens"] is None and confirmed["missing_calls"] == 1
    assert confirmed["known_input_tokens"] == 3
    assert rows["persisted-write"]["bridge_calls"] == 3
    tools = json.loads((tmp_path / "out" / "persisted-write" / "tool-calls.json").read_text())
    edit = next(t for t in tools if t["name"] == "edit_file")
    assert edit["execution_attempts"] == edit["effect_attempts"] == edit["effect_confirmed"] == 1
    assert report["new_provider_requests"] == report["new_official_evaluations"] == 0
    assert all(r["task_success"] is None and r["official_resolved"] is None for r in rows.values())
    assert all(r["inspect_model_events"] == r["bridge_calls"] == r["runtime_model_records"] for r in rows.values())
    verifier = json.loads((tmp_path / "out" / "edit-feedback" / "verifier-runs.json").read_text())
    assert verifier[-1]["authoritative"] and verifier[-1]["status"] == "pass"
    assert "before" in (tmp_path / "out" / "edit-feedback" / "workspace-diff.txt").read_text()


def test_hidden_oracle_not_forwarded_to_model_and_environment_ignored(tmp_path, monkeypatch):
    document = simple_plan()
    case = document["cases"][0]
    case["goal_files"] = {"answer.txt": "PRIVATE_ORACLE_SENTINEL"}
    case["prompt"] = "Read answer.txt and finish."
    case["model"] = [{"tool": "read_file", "id": "read", "input": {"path": "answer.txt"}}, {"text": "done"}]
    case["checks"] = {"status": "failed", "files": {"answer.txt": "before\n"}}
    monkeypatch.setenv("INSPECT_EVAL_MODEL", "nonexistent/never-contact")
    report = run(frozen_plan(tmp_path, document), tmp_path / "out")
    assert report["processing_ok"] and report["runtime_cases"]["passed"] == 1
    for receipt in (tmp_path / "out" / case["id"] / "bridge-calls").glob("*.json"):
        data = json.loads(receipt.read_text())
        assert "PRIVATE_ORACLE_SENTINEL" not in json.dumps(data["runtime_request"])


@pytest.mark.parametrize("change", ["empty", "duplicate", "path", "bool_budget", "oversize_budget", "shell", "usage", "collision", "unexpected", "shape"])
def test_invalid_plan_rejected_before_output_or_tools(tmp_path, change):
    document = simple_plan()
    case = document["cases"][0]
    if change == "empty": document["cases"] = []
    elif change == "duplicate": document["cases"].append(copy.deepcopy(case))
    elif change == "path": case["files"] = {"../outside": "no"}
    elif change == "bool_budget": case["max_turns"] = True
    elif change == "oversize_budget": case["max_turns"] = 17
    elif change == "shell": case["model"][0]["tool"] = "bash"
    elif change == "usage": case["model"][0]["usage"] = {"input_tokens": 129, "output_tokens": 1}
    elif change == "collision": case["files"]["answer.txt/child"] = "no"
    elif change == "unexpected": case["provider"] = "never-contact"
    else: document = []
    with pytest.raises(ValueError):
        run(frozen_plan(tmp_path, document), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_no_overwrite_or_output_in_input(tmp_path):
    path = frozen_plan(tmp_path, simple_plan())
    output = tmp_path / "existing"
    output.mkdir()
    (output / "sentinel").write_text("preserved")
    with pytest.raises(ValueError, match="fresh output"):
        run(path, output)
    with pytest.raises(ValueError, match="outside"):
        run(path, path.parent / "nested")
    assert (output / "sentinel").read_text() == "preserved"
    assert not (path.parent / "nested").exists()


def test_missing_fault_does_not_start_a_second_task(tmp_path):
    document = simple_plan()
    case = document["cases"][0]
    case["fault_point"] = "after_tool_persist"
    case["fault_tool_id"] = "not-requested"
    report = run(frozen_plan(tmp_path, document), tmp_path / "out")
    assert report["processing_ok"] is True
    row = report["results"][0]
    assert row["runtime_case_pass"] is False and row["fault_triggered"] is False
    assert row["bridge_calls"] == row["runtime_model_records"] == 3


def test_usage_missingness_keeps_known_subtotal():
    result = usage_summary([{"input_tokens": 0, "output_tokens": 1}, None])
    assert result["input_tokens"] is None and result["known_input_tokens"] == 0
    assert result["known_output_tokens"] == 1 and result["missing_calls"] == 1


def test_missing_logs_retains_runtime_and_inspect_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_runtime.inspect_runtime_solver.inspect_eval", lambda *_args, **_kw: [])
    report = run(frozen_plan(tmp_path, simple_plan()), tmp_path / "out")
    assert report["processing_ok"] is False and "missing Inspect" in report["error"]
    assert (tmp_path / "out" / "plan.json").exists() and (tmp_path / "out" / "seal.json").exists()


def test_cli_bad_input_exit_two(tmp_path, monkeypatch):
    path = frozen_plan(tmp_path, [])
    monkeypatch.setattr("sys.argv", ["inspect_runtime_solver", str(path), str(tmp_path / "out")])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and not (tmp_path / "out").exists()


def test_new_file_diff_and_raw_ledger_tampering_detected_without_regeneration(tmp_path):
    from inspect_ai.log import read_eval_log
    document = simple_plan()
    case = document["cases"][0]
    case["max_turns"] = 5
    case["model"].insert(-1, {"tool": "write_file", "id": "new", "input": {
        "path": "created.txt", "content": "NEW_FILE_EVIDENCE\n"}})
    case["checks"]["files"]["created.txt"] = "NEW_FILE_EVIDENCE\n"
    case["model"][-1].pop("expect_feedback", None)
    output = tmp_path / "out"
    report = run(frozen_plan(tmp_path, document), output)
    assert report["processing_ok"] is True, report
    assert report["runtime_cases"]["passed"] == 1
    diff = (output / case["id"] / "workspace-diff.txt").read_text()
    assert "created.txt" in diff and "+NEW_FILE_EVIDENCE" in diff
    logs = [read_eval_log(str(output / r["log"])) for r in report["results"]]
    path = output / case["id"] / "model-calls.json"
    original = path.read_bytes()
    records = json.loads(original)
    records[-1]["response"]["content"] = [{"type": "text", "text": "FORGED_RESPONSE"}]
    path.write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError, match="confirmed response differs"):
        check_logs(logs, document["cases"], output, report["mock_attempts"])
    records = json.loads(original)
    records[0]["request"]["messages"][0]["content"] = "FORGED_REQUEST"
    path.write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError, match="durable Runtime projection/request"):
        check_logs(logs, document["cases"], output, report["mock_attempts"])
    path.write_bytes(original)
    records = json.loads(original)
    records.append(copy.deepcopy(records[0]))
    path.write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate or foreign"):
        check_logs(logs, document["cases"], output, report["mock_attempts"])
    path.write_bytes(original)
    assert len(check_logs(logs, document["cases"], output, report["mock_attempts"])) == 1


def test_forged_out_of_scope_inspect_call_is_denied_by_runtime(tmp_path, monkeypatch):
    from inspect_ai.model import GenerateConfig, get_model
    from agent_runtime.inspect_model_bridge import mock_output
    from agent_runtime.models import ModelResponse, ToolCall
    document = simple_plan()
    document["cases"][0]["checks"] = {"status": "failed", "files": {"answer.txt": "before\n"}}
    shell_dispatches = []
    def forbidden_shell(*args, **kwargs):
        shell_dispatches.append(True)
        raise AssertionError("native shell must never execute")
    monkeypatch.setattr("agent_runtime.tools.ToolExecutor.run_bash", forbidden_shell)
    def forged_model(case, counts):
        def respond(*args):
            counts["calls"] += 1
            counts["outputs"] += 1
            return mock_output(ModelResponse(tool_calls=[ToolCall("forged", "bash", {"command": "MUST_NOT_EXECUTE"})]))
        model = get_model("mockllm/runtime-bridge", memoize=False, custom_outputs=respond,
                          config=GenerateConfig(max_retries=0))
        model.model_args.pop("custom_outputs", None)
        return model
    monkeypatch.setattr("agent_runtime.inspect_runtime_solver.fixture_model", forged_model)
    report = run(frozen_plan(tmp_path, document), tmp_path / "out")
    assert report["processing_ok"] is True, report
    row = report["results"][0]
    assert row["runtime_status"] == "failed" and "outside the active tool scope" in row["error"]
    assert shell_dispatches == []
    assert json.loads((tmp_path / "out" / row["id"] / "tool-calls.json").read_text()) == []


def test_worker_is_finished_before_cancelled_solver_returns(tmp_path, monkeypatch):
    import anyio
    import threading
    from types import SimpleNamespace
    from agent_runtime.runtime import Runtime
    case = simple_plan()["cases"][0]
    root = tmp_path / "case"
    root.mkdir()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_run = Runtime.run
    def paused_run(runtime, *args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test did not release bounded worker")
        try:
            return original_run(runtime, *args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(Runtime, "run", paused_run)
    model = fixture_model(case, {"calls": 0, "outputs": 0})
    monkeypatch.setattr("agent_runtime.inspect_runtime_solver.get_model", lambda: model)
    state = SimpleNamespace(metadata={})
    solve = runtime_solver(case, str(root))
    async def scenario():
        async def invoke():
            await solve(state, None)
        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            assert await anyio.to_thread.run_sync(lambda: entered.wait(10))
            group.cancel_scope.cancel()
            release.set()
        assert finished.is_set(), "cancelled solver left a live Runtime worker"
    anyio.run(scenario)
    assert finished.is_set()


def test_unsupported_anonymous_temp_storage_rejected_before_execution(tmp_path, monkeypatch):
    def unsupported(*_args, **_kwargs):
        raise FileNotFoundError("injected anonymous truncate filesystem failure")
    monkeypatch.setattr("agent_runtime.inspect_runtime_solver.tempfile.TemporaryFile", unsupported)
    with pytest.raises(ValueError, match="anonymous seek/truncate support"):
        run(frozen_plan(tmp_path, simple_plan()), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_container_capability_not_granted_by_plan_and_budget_must_match(tmp_path):
    from agent_runtime.inspect_runtime_solver import ContainerCaseBinding, validate_plan
    from agent_runtime.models import VerifiedSubtaskConfig, VerifierResult
    from agent_runtime.tests.test_container_reads import bridge
    document = simple_plan()
    case = document["cases"][0]
    case.update(execution_kind="container_read", goal_files={}, model=[{
        "tool": "container_read_file", "id": "read", "input": {"path": "answer.txt"}}])
    with pytest.raises(ValueError, match="trusted controller binding"):
        validate_plan(document)
    reads = bridge(tmp_path)
    node = VerifiedSubtaskConfig("fixture", "controller fixture", "trusted bytes", (),
        "frozen-container-verifier", "1", "frozen rule", lambda _: VerifierResult("pass", "read", []),
        "b" * 64, max_turns=case["max_turns"] + 1)
    with pytest.raises(ValueError, match="durable turn limit"):
        validate_plan(document, {case["id"]: ContainerCaseBinding(reads, node)})


def test_model_construction_failure_is_retained_and_sealed(tmp_path, monkeypatch):
    def failed_construction(*_args, **_kwargs):
        raise RuntimeError("injected model initialization failure")
    monkeypatch.setattr("agent_runtime.inspect_runtime_solver.fixture_model", failed_construction)
    result = run(frozen_plan(tmp_path, simple_plan()), tmp_path / "out")
    assert result["processing_ok"] is False and "initialization failure" in result["error"]
    assert result["mock_attempts"]["edit-feedback"]["calls"] == 0
    assert (tmp_path / "out" / "report.json").exists() and (tmp_path / "out" / "seal.json").exists()
