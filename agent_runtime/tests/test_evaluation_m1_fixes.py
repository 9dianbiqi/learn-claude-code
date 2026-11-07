"""Regression seams for the four M1 review findings; result-only fixtures."""
import json
import hashlib

import pytest

from agent_runtime.evaluation_adapters import adapt, load_source
from agent_runtime.evaluation_contract import aggregate
from agent_runtime.tests.test_evaluation_contract import source, sha, runtime_report
from agent_runtime.tests.test_evaluation_bundle import bundle, row, dump, pin


def core_entry(tmp_path, name="core", task="task-original"):
    root = tmp_path / name
    bundle(root, [row("regression", "runtime", True)])
    plan = json.loads((root / "plan.json").read_text())
    plan["cases"][0]["checks"] = {"status": "completed"}
    dump(root / "plan.json", plan)
    report = json.loads((root / "report.json").read_text())
    report["plan_sha256"] = sha(root / "plan.json")
    dump(root / "report.json", report)
    dump(root / "regression/runtime-summary.json", {"task_id": task})
    return {"id": name, "path": str(root / "report.json"), "sha256": sha(root / "report.json"),
            "bundle": str(root), "seal_sha256": pin(root)}


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_sealed_and_file_import_is_one_observation(tmp_path, reverse):
    entry = core_entry(tmp_path)
    plain = {k: v for k, v in entry.items() if k not in {"bundle", "seal_sha256"}}
    plain["id"] = "file-only"
    runs = [adapt(load_source(entry)), adapt(load_source(plain))]
    assert runs[0].cases[0].observation_id == runs[1].cases[0].observation_id
    result = aggregate(list(reversed(runs)) if reverse else runs)
    assert result["unique_case_observations"] == 1
    assert result["cohorts"][0]["N"] == 1


def test_mixed_import_conflicting_frozen_config_still_blocks(tmp_path):
    entry = core_entry(tmp_path)
    plain = {k: v for k, v in entry.items() if k not in {"bundle", "seal_sha256"}}
    plain["comparison"] = {"condition": "conflicting"}
    first = {**entry, "comparison": {"condition": "original"}}
    with pytest.raises(ValueError, match="conflict"):
        aggregate([adapt(load_source(first)), adapt(load_source(plain))])


def test_identical_report_bytes_with_distinct_recorded_executions_stay_independent(tmp_path):
    entries = [core_entry(tmp_path, "first", "task-first"), core_entry(tmp_path, "second", "task-second")]
    assert entries[0]["sha256"] == entries[1]["sha256"]
    result = aggregate([adapt(load_source(e)) for e in entries])
    assert result["cohorts"][0]["N"] == 2


@pytest.mark.parametrize("top,nested", [(5, 1), (0, 1), (1, 5)])
def test_conflicting_inspect_planned_fields_block(tmp_path, top, nested):
    report = {"schema_version": "inspect-model-result-v1", "model_mode": "mock", "planned_cases": top,
        "fixture_outcomes": {"planned": nested, "passed": 1, "failed": 0, "unknown": nested - 1},
        "results": [{"id": "one", "fixture_success": True}]}
    with pytest.raises(ValueError, match="planned denominator"):
        adapt(load_source(source(tmp_path, report)))


def failed_official_entry(tmp_path, *, status="FAILED", explicit_refs=False):
    root = tmp_path / "official"
    root.mkdir()
    predictions = [{"instance_id": f"repo__issue-{i}", "model_name_or_path": "scripted/candidate", "model_patch": "patch"}
                   for i in range(2)]
    (root / "predictions.jsonl").write_text("\n".join(json.dumps(r) for r in predictions) + "\n", encoding="utf-8")
    pins = {"prediction_sha256": sha(root / "predictions.jsonl"), "dataset_manifest_sha256": "a" * 64,
            "harness_commit": "b" * 40}
    dump(root / "invocation.json", {**pins, "argv": ["python", "--run_id", "official-original",
         "--instance_ids", "repo__issue-0", "repo__issue-1"]})
    dump(root / "result.json", {"schema_version": "swe-official-run-v1", **pins, "status": status,
         "official_resolved": None, "exit_code": 7 if status == "FAILED" else None,
         "manual_recovery_required": True, "error": "grading interrupted"})
    entry = {"id": "official", "path": str(root / "result.json"), "sha256": sha(root / "result.json"),
             "bundle": str(root), "seal_sha256": pin(root)}
    if explicit_refs:
        entry["official_artifacts"] = {"invocation": "invocation.json", "predictions": "predictions.jsonl"}
    return entry


@pytest.mark.parametrize("status", ["FAILED", "STARTED"])
@pytest.mark.parametrize("explicit_refs", [False, True])
def test_official_failure_retains_pinned_planned_instances(tmp_path, status, explicit_refs):
    run = adapt(load_source(failed_official_entry(tmp_path, status=status, explicit_refs=explicit_refs)))
    assert {c.case_id for c in run.cases} == {"repo__issue-0", "repo__issue-1"}
    assert all(c.verdict == ("blocked" if status == "FAILED" else "unknown") for c in run.cases)
    assert all(c.oracle.authority == "unavailable" for c in run.cases)
    cohort, = aggregate([run])["cohorts"]
    assert cohort["N"] == 2 and cohort["strict_success_rate"] is None


def container_entry(tmp_path, *, binding=True, records=True):
    plan = {"run_id": "fixed-plan", "cases": [{"id": "repair", "execution_kind": "container_repair", "goal_files": {}}]}
    encoded = json.dumps(plan, ensure_ascii=False, allow_nan=False).encode()
    report = {"schema_version": "inspect-model-result-v1", "run_id": "fixed-plan", "model_mode": "mock",
        "plan_sha256": hashlib.sha256(encoded).hexdigest(), "planned_cases": 1,
        "results": [{"id": "repair", "task_id": "task-container", "execution_kind": "container_repair",
                     "fixture_success": True, "runtime_case_pass": True}]}
    files = {"plan.json": plan}
    if binding:
        files["bindings.json"] = {"repair": {"verifier_id": "frozen-container-probes"}}
    if records:
        files["repair/verifier-runs.json"] = [{"task_id": "task-container", "verifier_run_id": "v1",
            "verifier_id": "frozen-container-probes", "verifier_version": "1", "verifier_bundle_hash": "c" * 64,
            "authoritative": True, "status": "pass"}]
    return source(tmp_path, report, sealed=True, files=files)


def test_container_oracle_uses_sealed_binding_and_verifier_record(tmp_path):
    run = adapt(load_source(container_entry(tmp_path)))
    oracle = run.cases[0].oracle
    assert oracle.oracle_id.startswith("frozen-container-probes")
    assert oracle.authority == "source_validated" and oracle.kind == "controller_verifier"
    assert len(oracle.evidence_refs) >= 2


@pytest.mark.parametrize("binding,records", [(False, False), (True, False), (False, True)])
def test_container_empty_goals_do_not_invent_validated_oracle(tmp_path, binding, records):
    case, = adapt(load_source(container_entry(tmp_path, binding=binding, records=records))).cases
    assert case.oracle.authority == "reported"
    assert not case.oracle.oracle_id.startswith("fixture-goal:")


@pytest.mark.parametrize("reverse", [False, True])
def test_inspect_mixed_import_enriches_oracle_without_double_counting_missing_samples(tmp_path, reverse):
    plan = {"run_id": "fixed-plan", "cases": [{"id": "missing", "checks": {"status": "completed"}},
                                               {"id": "edit", "checks": {"status": "completed"}}]}
    encoded = json.dumps(plan, ensure_ascii=False, allow_nan=False).encode()
    report = runtime_report(runtime_cases={"total": 2, "passed": 1}, plan_sha256=hashlib.sha256(encoded).hexdigest())
    entry = source(tmp_path, report, sealed=True, files={"plan.json": plan})
    plain = {k: v for k, v in entry.items() if k not in {"bundle", "seal_sha256"}}
    runs = [adapt(load_source(entry)), adapt(load_source(plain))]
    result = aggregate(list(reversed(runs)) if reverse else runs)
    cohort, = result["cohorts"]
    assert cohort["N"] == 2 and cohort["pass"] == 1 and cohort["unknown"] == 1
    assert result["unique_case_observations"] == 2


def test_unidentified_alias_of_two_independent_executions_is_ambiguous(tmp_path):
    first, second = core_entry(tmp_path, "first", "t1"), core_entry(tmp_path, "second", "t2")
    plain = {k: v for k, v in first.items() if k not in {"bundle", "seal_sha256"}}
    with pytest.raises(ValueError, match="ambiguous"):
        aggregate([adapt(load_source(e)) for e in (first, second, plain)])


def test_official_failed_prediction_pin_mismatch_is_rejected(tmp_path):
    entry = failed_official_entry(tmp_path)
    from pathlib import Path
    root = Path(entry["bundle"])
    (root / "predictions.jsonl").write_text('{}\n', encoding="utf-8")
    entry["seal_sha256"] = pin(root)
    with pytest.raises(ValueError):
        adapt(load_source(entry))


@pytest.mark.parametrize("reverse", [False, True])
def test_agent_archive_task_and_run_aliases_add_no_case_execution(tmp_path, reverse):
    from agent_runtime.tests.test_evaluation import agent
    from agent_runtime.evaluation import _agent, _aggregate
    root = tmp_path / "archive"
    root.mkdir()
    config = agent(root)
    inputs = {role: (root / spec["path"]).read_bytes() for role, spec in config["artifacts"].items()}
    result = _agent(config, inputs)
    plan = {"schema_version": "evaluation-plan-v1", "run_id": "archive", "cases": [config]}
    dump(root / "plan.json", plan)
    dump(root / config["id"] / "result.json", result)
    report = {"schema_version": "evaluation-report-v1", "run_id": "archive", "plan_sha256": sha(root / "plan.json"),
              "processing_ok": True, "new_provider_requests": 0, "new_official_evaluations": 0,
              "results": [result], "aggregates": _aggregate([result])}
    dump(root / "report.json", report)
    entry = {"id": "archive", "path": str(root / "report.json"), "sha256": sha(root / "report.json"),
             "bundle": str(root), "seal_sha256": pin(root)}
    plain = {k: v for k, v in entry.items() if k not in {"bundle", "seal_sha256"}}
    runs = [adapt(load_source(entry)), adapt(load_source(plain))]
    combined = aggregate(list(reversed(runs)) if reverse else runs)
    assert combined["unique_case_observations"] == 1
    assert combined["unique_attempt_records"] == 1
    assert combined["cohorts"][0]["N"] == 1


@pytest.mark.parametrize("field,value", [("verifier_id", "foreign"), ("task_id", "foreign"), ("verifier_bundle_hash", "bad")])
def test_container_binding_conflicts_are_rejected(tmp_path, field, value):
    from pathlib import Path
    entry = container_entry(tmp_path)
    root = Path(entry["bundle"])
    records = json.loads((root / "repair/verifier-runs.json").read_text())
    records[0][field] = value
    dump(root / "repair/verifier-runs.json", records)
    entry["seal_sha256"] = pin(root)
    with pytest.raises(ValueError):
        adapt(load_source(entry))


def test_missing_container_bundle_hash_stays_reported(tmp_path):
    from pathlib import Path
    entry = container_entry(tmp_path)
    root = Path(entry["bundle"])
    records = json.loads((root / "repair/verifier-runs.json").read_text())
    records[0].pop("verifier_bundle_hash")
    dump(root / "repair/verifier-runs.json", records)
    entry["seal_sha256"] = pin(root)
    assert adapt(load_source(entry)).cases[0].oracle.authority == "reported"


def test_attempt_parent_link_can_be_enriched_without_conflict(tmp_path):
    from dataclasses import replace
    from agent_runtime.evaluation_contract import EvaluationRun
    entry = source(tmp_path, runtime_report(), sealed=True,
                   files={"edit/model-calls.json": [{"task_id": "original-task", "model_call_id": 1, "status": "succeeded"}]})
    strong = adapt(load_source(entry))
    generation = next(a for a in strong.attempts if a.kind == "model_generation")
    weak = EvaluationRun("wrapper", "summary", attempts=[replace(generation, parent_id=None)])
    assert aggregate([weak, strong])["unique_attempt_records"] == 2
    assert aggregate([strong, weak])["unique_attempt_records"] == 2


def test_explicit_empty_plan_conflicting_with_results_is_rejected(tmp_path):
    plan = {"run_id": "fixed-plan", "cases": []}
    encoded = json.dumps(plan, ensure_ascii=False, allow_nan=False).encode()
    entry = source(tmp_path, runtime_report(plan_sha256=hashlib.sha256(encoded).hexdigest()),
                   sealed=True, files={"plan.json": plan})
    with pytest.raises(ValueError, match="planned denominator"):
        adapt(load_source(entry))


def test_projection_has_executor_and_db_tripwires(tmp_path, monkeypatch):
    import subprocess
    from agent_runtime.runtime import Runtime
    from agent_runtime.store import EventStore
    from agent_runtime.providers import AnthropicModel
    from agent_runtime import swe_grading
    from agent_runtime.evaluation_v2 import render
    from agent_runtime.tests.test_evaluation_contract import manifest

    def forbidden(*args, **kwargs):
        pytest.fail("result projection invoked an executor or opened a Runtime DB")

    for owner, name in [(Runtime, "run"), (Runtime, "resume"), (EventStore, "__init__"),
                        (AnthropicModel, "__init__"), (swe_grading, "grade"), (subprocess, "run")]:
        monkeypatch.setattr(owner, name, forbidden)
    entries = [core_entry(tmp_path), failed_official_entry(tmp_path)]
    report = render(manifest(tmp_path, entries), tmp_path / "new-report")
    assert sum(c["N"] for c in report["metrics"]["cohorts"]) == 3
    assert report["new_runtime_executions"] == report["new_provider_requests"] == report["new_official_evaluations"] == 0
