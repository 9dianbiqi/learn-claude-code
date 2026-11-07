"""New model/repair wiring; simulated Docker transport, real Inspect/Runtime."""
import json

import pytest

pytest.importorskip("inspect_ai")
from agent_runtime import inspect_model_eval
from agent_runtime.evaluation import _dump
from agent_runtime.inspect_runtime_solver import ContainerCaseBinding
from agent_runtime.models import VerifiedSubtaskConfig, VerifierResult
from agent_runtime.tests.test_container_edits import setup_bridge, arguments, sha


def fixture(tmp_path):
    workspace, bridge = setup_bridge(tmp_path)
    node = VerifiedSubtaskConfig("fixture", "Repair fixture", "Frozen file check", (), "repair", "1", sha(b"goal"),
        lambda ctx: VerifierResult("pass" if (workspace / "app.py").read_bytes() == b"value = 2\n" else "fail", "checked", []),
        sha(b"verifier"), max_turns=4)
    plan = {"schema_version": "inspect-model-eval-v1", "run_id": "container-model", "mode": "mock",
        "model": "mockllm/runtime-bridge", "limits": {"max_requests_per_case": 4, "max_output_tokens": 512,
        "request_timeout_seconds": 15}, "cases": [{"id": "repair", "execution_kind": "container_repair",
        "prompt": "Read app.py then change 1 to 2 and finish.", "max_turns": 4,
        "files": {"app.py": "HOST_SENTINEL"}, "goal_files": {}, "model": [
        {"tool": "container_read_file", "id": "read", "input": {"path": "app.py"}},
        {"tool": "container_edit_file", "id": "edit", "input": arguments()}, {"text": "Done"}]}]}
    return plan, {"repair": ContainerCaseBinding(bridge, node)}, workspace


def test_model_execution_reaches_bound_repair_verifier(tmp_path):
    plan, bindings, workspace = fixture(tmp_path)
    source = tmp_path / "inputs/plan.json"
    source.parent.mkdir()
    _dump(source, plan)
    report = inspect_model_eval.run(source, tmp_path / "out", execute=True, bindings=bindings)
    assert report["processing_ok"], report
    assert report["fixture_outcomes"] == {"planned": 1, "passed": 1, "failed": 0, "unknown": 0}
    assert report["new_provider_requests"] == report["new_official_evaluations"] == 0
    assert (workspace / "app.py").read_bytes() == b"value = 2\n"
    assert (tmp_path / "out/repair/workspace/app.py").read_text() == "HOST_SENTINEL"
    receipts = "".join(p.read_text() for p in (tmp_path / "out/repair/bridge-calls").glob("*.json"))
    assert "HOST_SENTINEL" not in receipts
    assert json.loads((tmp_path / "out/bindings.json").read_text())["repair"]["verifier_id"] == "repair"


@pytest.mark.parametrize("change", ["missing", "foreign", "host-goal", "native-tool", "wrong-kind"])
def test_invalid_binding_fails_before_any_execution(tmp_path, change):
    plan, bindings, _ = fixture(tmp_path)
    if change == "missing":
        bindings = None
    elif change == "foreign":
        bindings = {"other": bindings["repair"]}
    elif change == "host-goal":
        plan["cases"][0]["goal_files"] = {"app.py": "host goal"}
    elif change == "native-tool":
        plan["cases"][0]["model"][0]["tool"] = "read_file"
    else:
        plan["cases"][0]["execution_kind"] = "unknown"
    with pytest.raises(ValueError):
        inspect_model_eval.validate(plan, bindings)


def test_live_bound_preflight_never_constructs_provider(tmp_path, monkeypatch):
    plan, bindings, _ = fixture(tmp_path)
    plan["mode"], plan["model"] = "live", "anthropic/explicit-model"
    del plan["cases"][0]["model"]
    source = tmp_path / "plan.json"
    _dump(source, plan)
    monkeypatch.setattr(inspect_model_eval, "get_model", lambda *a, **k: pytest.fail("provider constructed"))
    report = inspect_model_eval.run(source, tmp_path / "out", bindings=bindings)
    assert report["status"] == "PREFLIGHT_ONLY"
    assert not (tmp_path / "out").exists()
