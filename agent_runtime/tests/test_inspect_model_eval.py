import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")
from agent_runtime import inspect_model_eval as model_eval
from agent_runtime.evaluation import _dump

EXAMPLE = Path(__file__).parents[1] / "evals/model-fixtures-v1.json"


def test_default_preflight_never_constructs_model(tmp_path, monkeypatch):
    monkeypatch.setattr(model_eval, "get_model", lambda *a, **k: pytest.fail("preflight called model"))
    result = model_eval.run(EXAMPLE, tmp_path / "output")
    assert result["status"] == "PREFLIGHT_ONLY"
    assert result["new_provider_requests"] == 0
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("mutation", ["budget", "turns", "mode", "script", "provider"])
def test_model_plan_rejects_ambiguous_or_unbounded_execution(mutation):
    plan = json.loads(EXAMPLE.read_text())
    if mutation == "budget":
        plan["limits"]["max_requests_per_case"] = True
    elif mutation == "turns":
        plan["cases"][0]["max_turns"] = 5
    elif mutation == "mode":
        plan["mode"] = "live"
    elif mutation == "script":
        plan["mode"], plan["model"] = "live", "anthropic/explicit-model"
    else:
        plan["model"] = "default"
    with pytest.raises(ValueError):
        model_eval.validate(plan)


def test_model_bridge_end_to_end_and_wrong_answer(tmp_path):
    plan = json.loads(EXAMPLE.read_text())
    negative = copy.deepcopy(plan["cases"][0])
    negative["id"] = "false-completion"
    negative["model"] = [{"text": "Everything is done."}]
    plan["cases"].append(negative)
    source = tmp_path / "input/plan.json"
    source.parent.mkdir()
    _dump(source, plan)
    result = model_eval.run(source, tmp_path / "output", execute=True)
    assert result["processing_ok"], result
    assert result["fixture_outcomes"] == {"planned": 2, "passed": 1, "failed": 1, "unknown": 0}
    assert result["new_provider_requests"] == result["new_official_evaluations"] == 0
    assert result["model_attempts"] == 4
    assert all(r["official_resolved"] is None for r in result["results"])


def test_live_preflight_has_no_network_or_model_construction(tmp_path, monkeypatch):
    plan = json.loads(EXAMPLE.read_text())
    plan["mode"], plan["model"] = "live", "anthropic/explicit-user-selected-model"
    del plan["cases"][0]["model"]
    source = tmp_path / "plan.json"
    _dump(source, plan)
    monkeypatch.setattr(model_eval, "get_model", lambda *a, **k: pytest.fail("live preflight called model"))
    assert model_eval.run(source, tmp_path / "output")["status"] == "PREFLIGHT_ONLY"


def test_explicit_request_budget_blocks_before_generation(tmp_path):
    class Model:
        def __str__(self):
            return "explicit/model"
    bridge = model_eval.BoundedModelBridge(Model(), tmp_path / "receipts", "case",
        model_eval.ModelPolicy("explicit/model", "live", 1, 15))
    bridge.calls = 1
    with pytest.raises(RuntimeError, match="budget exhausted"):
        bridge.complete([], [])
    assert not list((tmp_path / "receipts").iterdir())
