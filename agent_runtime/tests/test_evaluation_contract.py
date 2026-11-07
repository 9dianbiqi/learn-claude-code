"""Deterministic result-only M1 checks: no model, Runtime, Docker or Harness runs."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pytest

from agent_runtime.evaluation_adapters import adapt, load_source
from agent_runtime.evaluation_contract import (
    ATTEMPT_KINDS, Attempt, ComparisonIdentity, EvaluationCase, EvaluationRun,
    ExecutionScope, Metric, Oracle, SourceExecutionIdentity, aggregate, outcome_counts,
)
from agent_runtime.evaluation_v2 import project_manifest, render


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def source(tmp_path, report, *, name="input", files=None, sealed=False, **context):
    root = tmp_path / name
    root.mkdir()
    path = root / "report.json"
    dump(path, report)
    for key, value in (files or {}).items():
        dump(root / key, value)
    entry = {"id": name, "path": str(path), "sha256": sha(path), **context}
    if sealed:
        dump(root / "seal.json", [{"path": p.relative_to(root).as_posix(), "sha256": sha(p)}
                                  for p in sorted(root.rglob("*")) if p.is_file()])
        entry.update(bundle=str(root), seal_sha256=sha(root / "seal.json"))
    return entry


def manifest(tmp_path, entries):
    path = tmp_path / "manifest" / "input.json"
    dump(path, {"schema_version": "evaluation-v2-input-v1", "run_id": "m1-tests", "entries": entries})
    return path


def runtime_report(value=True, task="original-task", **changes):
    report = {"schema_version": "inspect-runtime-result-v1", "run_id": "fixed-plan",
              "processing_ok": True, "runtime_cases": {"total": 1, "passed": int(value is True)},
              "results": [{"id": "edit", "task_id": task, "runtime_status": "completed",
                           "runtime_case_pass": value, "task_success": None, "official_resolved": None}]}
    report.update(changes)
    return report


def evaluation_row(value=True, **changes):
    row = {"id": "edit", "mode": "scripted_runtime", "runtime_case_pass": value,
           "runtime_status": "completed", "task_success": None, "official_resolved": None,
           "processing": "ok"}
    row.update(changes)
    return {"schema_version": "evaluation-report-v1", "run_id": "fixed-plan", "results": [row]}


@pytest.mark.parametrize("report,cases,objective", [
    (evaluation_row(), 1, "runtime_regression"),
    ({"schema_version": "evaluation-suite-result-v1", "jobs": [{"id": "j", "accepted": True}]}, 0, None),
    (runtime_report(), 1, "runtime_regression"),
    ({"schema_version": "inspect-model-result-v1", "model_mode": "mock", "planned_cases": 1,
      "results": [{"id": "repair", "task_id": "t", "fixture_success": True}]}, 1, "fixture_goal"),
    ({"schema_version": "inspect-offline-result-v1", "source_aggregates": {"runtime": {"total": 9}}}, 0, None),
    ({"schema_version": "inspect-swe-candidate-v1", "instance_id": "repo__issue-1",
      "candidate_validated": True}, 1, "candidate_validation"),
    ({"schema_version": "swe-official-run-v1", "official_resolved": {"repo__issue-1": True}}, 1, "official_resolution"),
])
def test_all_supported_v1_schemas_preserve_original_fields(tmp_path, report, cases, objective):
    before = deepcopy(report)
    run = adapt(load_source(source(tmp_path, report)))
    assert run.schema_version == "evaluation-run-v2"
    assert run.raw == before == report
    assert len(run.cases) == cases
    if cases:
        assert run.cases[0].objective == objective
        assert run.cases[0].oracle.oracle_id
    if report["schema_version"] == "swe-official-run-v1":
        assert run.cases[0].verdict == "unknown"
        assert run.cases[0].oracle.authority == "unavailable"
        assert run.cases[0].scope.grading == "unknown"


def test_four_verdicts_and_planned_denominator(tmp_path):
    reports = [evaluation_row(True), evaluation_row(False), evaluation_row(None),
               evaluation_row(None, processing="error", error="evidence unavailable")]
    runs = [adapt(load_source(source(tmp_path, r, name=f"s{i}"))) for i, r in enumerate(reports)]
    cohort, = aggregate(runs)["cohorts"]
    assert {k: cohort[k] for k in ("pass", "fail", "blocked", "unknown", "N")} == {
        "pass": 1, "fail": 1, "blocked": 1, "unknown": 1, "N": 4}
    assert cohort["success_lower_bound"] == .25
    assert cohort["known_fraction"] == .5
    assert cohort["strict_success_rate"] is None


def test_objective_isolation_and_no_cross_scope_rate(tmp_path):
    run = adapt(load_source(source(tmp_path, runtime_report())))
    case = run.cases[0]
    identity = replace(case.source_identity, objective="task_success")
    other = replace(case, observation_id="other-objective", objective="task_success",
                    verdict="fail", source_identity=identity)
    run.cases.append(other)
    cohorts = aggregate([run])
    assert len(cohorts["cohorts"]) == 2
    assert cohorts["overall_success_rate"] is None
    live = replace(case, observation_id="other-scope", source_identity=None,
                   scope=replace(case.scope, model_mode="live"))
    run.cases.append(live)
    assert len(aggregate([run])["cohorts"]) == 3


def test_empty_cohort_is_not_success():
    counts = outcome_counts(dict.fromkeys(("pass", "fail", "blocked", "unknown"), 0))
    assert counts["N"] == 0
    assert all(counts[k] is None for k in ("success_lower_bound", "strict_success_rate", "known_fraction"))
    assert aggregate([])["cohorts"] == []


@pytest.mark.parametrize("usage", [None, {"complete": False, "known_input_tokens": 19, "known_output_tokens": 2},
                                        {"complete": True, "input_tokens": 0, "output_tokens": 0}])
def test_usage_completeness_is_independent_of_task_verdict(tmp_path, usage):
    row = {"id": "fixture", "task_id": "t", "fixture_success": True}
    if usage is not None:
        row["usage"] = usage
    report = {"schema_version": "inspect-model-result-v1", "model_mode": "live", "planned_cases": 1, "results": [row]}
    case, = adapt(load_source(source(tmp_path, report))).cases
    metrics = {m.name: m for m in case.metrics}
    assert case.verdict == "pass"
    assert metrics["cost"].value is None and metrics["latency_seconds"].value is None
    if usage and usage["complete"]:
        assert metrics["provider_input_tokens"].value == 0
    else:
        assert metrics["provider_input_tokens"].value is None
        assert metrics["provider_input_tokens"].complete is False
    if usage and "known_input_tokens" in usage:
        assert metrics["provider_known_input_tokens"].value == 19
        assert not metrics["provider_known_input_tokens"].complete


def test_mock_usage_cannot_become_provider_usage(tmp_path):
    report = {"schema_version": "inspect-model-result-v1", "model_mode": "mock", "planned_cases": 1,
              "results": [{"id": "m", "fixture_success": True, "usage": {
                  "complete": True, "input_tokens": 15, "output_tokens": 3, "known_input_tokens": 15}}]}
    case = adapt(load_source(source(tmp_path, report))).cases[0]
    assert {m.name: m.value for m in case.metrics}["provider_input_tokens"] is None
    assert {m.name: m.value for m in case.metrics}["synthetic_known_input_tokens"] == 15


def test_duplicate_import_and_inspect_projection_add_no_executions(tmp_path):
    entry = source(tmp_path, runtime_report(), name="original")
    original = adapt(load_source(entry))
    duplicate = adapt(load_source({**entry, "id": "another-import"}))
    projection = adapt(load_source(source(tmp_path, {"schema_version": "inspect-offline-result-v1",
        "source_run_id": "fixed-plan", "source_aggregates": {"runtime": {"total": 1}}}, name="projection")))
    result = aggregate([original, duplicate, projection])
    assert result["unique_case_observations"] == 1
    assert result["unique_attempt_records"] == 1
    assert projection.cases == [] and projection.attempts == []


def test_unidentified_different_reports_are_not_forced_together(tmp_path):
    first = evaluation_row()
    second = evaluation_row()
    second["annotation"] = "different observation, no execution identity"
    runs = [adapt(load_source(source(tmp_path, r, name=f"r{i}"))) for i, r in enumerate((first, second))]
    assert all(c.source_identity is None for r in runs for c in r.cases)
    assert aggregate(runs)["cohorts"][0]["N"] == 2
    assert not any(r.attempts for r in runs)


def test_independent_repeats_preserve_both_executions(tmp_path):
    config = {"task_version": "v1", "condition": "candidate", "model": "mock/fixed", "budget": 4,
              "fault": None, "seed": 0, "repeat": 0}
    runs = [adapt(load_source(source(tmp_path, runtime_report(task=f"task-{i}"), name=f"repeat{i}",
                                     comparison={**config, "repeat": i}))) for i in range(2)]
    assert runs[0].cases[0].comparison.key != runs[1].cases[0].comparison.key
    assert aggregate(runs)["cohorts"][0]["N"] == 2
    assert ComparisonIdentity({"model": "x"}).key is None
    assert "task_version" in ComparisonIdentity({"model": "x"}).missing


@pytest.mark.parametrize("change", ["verdict", "scope", "comparison", "raw"])
def test_same_identity_conflicts_block_before_output(tmp_path, change):
    first = source(tmp_path, runtime_report(), name="first")
    report = runtime_report(value=False) if change == "verdict" else runtime_report()
    if change == "raw":
        report["results"][0]["runtime_status"] = "failed"
    context = {"scope": {"environment": "real_container"}} if change == "scope" else {}
    if change == "comparison":
        context["comparison"] = {"repeat": 2}
    second = source(tmp_path, report, name="second", **context)
    output = tmp_path / "new-output"
    with pytest.raises(ValueError, match="conflicting evidence"):
        render(manifest(tmp_path, [first, second]), output)
    assert not output.exists()


def test_retry_attempt_records_do_not_increase_case_denominator(tmp_path):
    files = {"edit/model-calls.json": [
        {"task_id": "original-task", "model_call_id": i, "status": status}
        for i, status in enumerate(("failed", "abandoned", "succeeded"))],
        "edit/tool-calls.json": [{"task_id": "original-task", "tool_use_id": "edit", "execution_attempts": 2}],
        "edit/verifier-runs.json": [{"task_id": "original-task", "verifier_run_id": "v1", "status": "fail"},
                                    {"task_id": "original-task", "verifier_run_id": "v2", "status": "pass"}]}
    run = adapt(load_source(source(tmp_path, runtime_report(), files=files, sealed=True)))
    assert len(run.attempts) == 7
    assert {a.kind for a in run.attempts} == ATTEMPT_KINDS - {"suite_invocation"}
    assert aggregate([run])["cohorts"][0]["N"] == 1
    assert all(a.parent_id for a in run.attempts if a.kind != "case_execution")


@pytest.mark.parametrize("mutation", ["report", "member", "reseal", "extra"])
def test_tampering_blocks_before_output(tmp_path, mutation):
    entry = source(tmp_path, runtime_report(), sealed=True, files={"record.json": {"original": True}})
    root = Path(entry["bundle"])
    if mutation == "report":
        dump(root / "report.json", runtime_report(False))
    elif mutation in {"member", "reseal"}:
        dump(root / "record.json", {"tampered": True})
        if mutation == "reseal":
            dump(root / "seal.json", [{"path": p.name, "sha256": sha(p)}
                                      for p in root.iterdir() if p.name != "seal.json"])
    else:
        dump(root / "new-file.json", {})
    with pytest.raises(ValueError):
        render(manifest(tmp_path, [entry]), tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_expected_review_is_regression_pass_not_task_success(tmp_path):
    row = evaluation_row(True, runtime_status="needs_review")
    case, = adapt(load_source(source(tmp_path, row))).cases
    assert case.verdict == "pass" and case.objective == "runtime_regression"
    assert case.raw["task_success"] is None


def test_forged_official_field_cannot_promote_candidate(tmp_path):
    report = {"schema_version": "inspect-swe-candidate-v1", "instance_id": "x",
              "candidate_validated": True, "official_resolved": True}
    entry = source(tmp_path, report)
    case, = adapt(load_source(entry)).cases
    assert case.objective == "candidate_validation"
    assert case.scope.grading == "controller_verifier" and case.raw["official_resolved"] is True
    with pytest.raises(ValueError, match="scope"):
        adapt(load_source({**entry, "scope": {"grading": "official_harness"}}))


def test_summary_only_schema_does_not_invent_cases_or_attempts(tmp_path):
    report = {"schema_version": "evaluation-suite-result-v1", "run_id": "reused-logical-run",
              "jobs": [{"id": "j", "accepted": True, "metrics": {"total": 100, "passed": 100}}]}
    run = adapt(load_source(source(tmp_path, report)))
    assert run.cases == [] and run.attempts == []
    assert run.raw["jobs"][0]["metrics"]["total"] == 100
    assert any("PARTIAL" in note for note in run.limitations)


def test_missing_planned_ids_are_unknown_counts_not_fabricated_cases(tmp_path):
    report = runtime_report(runtime_cases={"total": 4, "passed": 1})
    run = adapt(load_source(source(tmp_path, report)))
    assert len(run.cases) == 1
    cohort, = aggregate([run, run])["cohorts"]
    assert cohort["N"] == 4 and cohort["unknown"] == 3
    assert cohort["unidentified_planned"] == 3
    assert cohort["strict_success_rate"] is None


def test_named_missing_case_uses_plan_identity_without_inventing_attempt(tmp_path):
    plan = {"run_id": "fixed-plan", "cases": [{"id": "edit", "checks": {"status": "completed"}},
                                               {"id": "missing", "checks": {"status": "completed"}}]}
    plan_bytes = json.dumps(plan, ensure_ascii=False, allow_nan=False).encode()
    report = runtime_report(runtime_cases={"total": 2, "passed": 1}, plan_sha256=hashlib.sha256(plan_bytes).hexdigest())
    run = adapt(load_source(source(tmp_path, report, files={"plan.json": plan}, sealed=True)))
    missing = next(c for c in run.cases if c.case_id == "missing")
    assert missing.verdict == "unknown" and missing.source_identity is None and not missing.attempt_refs
    assert aggregate([run])["cohorts"][0]["N"] == 2


def test_v1_bytes_hashes_and_output_unchanged_with_independent_v2_seal(tmp_path):
    entry = source(tmp_path, runtime_report(), sealed=True, files={"v1-report.md": "original"})
    root = Path(entry["bundle"])
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    path = manifest(tmp_path, [entry])
    output = tmp_path / "v2"
    report = render(path, output)
    assert before == {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert (output / "sources/input.bin").read_bytes() == (root / "report.json").read_bytes()
    assert report["new_runtime_executions"] == report["new_provider_requests"] == report["new_official_evaluations"] == 0
    for row in json.loads((output / "seal.json").read_text()):
        assert sha(output / row["path"]) == row["sha256"]
    assert sha(output / "seal.json") != entry["seal_sha256"]
    with pytest.raises(ValueError):
        render(path, output)
    text = (output / "report.md").read_text()
    assert "Evidence references" in text and "Metric completeness" in text


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_unavailable_oracle_cannot_establish_known_verdict(verdict):
    with pytest.raises(ValueError, match="unavailable oracle"):
        EvaluationCase("o", "c", "task_success", Oracle("missing", "unknown", "unavailable"),
                       verdict, "unsupported claim", ExecutionScope())


def test_identity_binds_objective_and_oracle():
    with pytest.raises(ValueError, match="bind"):
        EvaluationCase("o", "c", "task_success", Oracle("v", "verifier"), "pass", "ok", ExecutionScope(),
                       SourceExecutionIdentity("t", "c", "runtime_regression", "v"))
    with pytest.raises(ValueError):
        Attempt("a", "retry", "x", ())
    with pytest.raises(ValueError):
        Metric("cost", None, "usd", True)


def official_entry(tmp_path, *, environment="real_container"):
    prediction = {"instance_id": "repo__issue-1", "model_name_or_path": "mock/scripted", "model_patch": "diff"}
    raw_prediction = json.dumps(prediction)
    prediction_sha = hashlib.sha256(raw_prediction.encode()).hexdigest()
    official = {"schema_version": 2, "total_instances": 1, "submitted_instances": 1,
        "completed_instances": 1, "resolved_instances": 1, "unresolved_instances": 0,
        "empty_patch_instances": 0, "error_instances": 0, "submitted_ids": ["repo__issue-1"],
        "completed_ids": ["repo__issue-1"], "resolved_ids": ["repo__issue-1"],
        "unresolved_ids": [], "empty_patch_ids": [], "error_ids": [], "incomplete_ids": []}
    report_sha = hashlib.sha256(json.dumps(official, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    pins = {"dataset_manifest_sha256": "a" * 64, "prediction_sha256": prediction_sha, "harness_commit": "b" * 40}
    report = {"schema_version": "swe-official-run-v1", "status": "REPORTED", "exit_code": 0,
              **pins, "report_sha256": report_sha, "official_resolved": {"repo__issue-1": True}}
    entry = source(tmp_path, report, files={"official.json": official,
        "invocation.json": {**pins, "argv": ["python", "--run_id", "original-official-run"]}},
        scope={"environment": environment, "grading": "official_harness", "model_mode": "scripted",
               "subject_role": "scripted_candidate"})
    root = Path(entry["path"]).parent
    (root / "predictions.jsonl").write_text(raw_prediction, encoding="utf-8")
    dump(root / "seal.json", [{"path": p.name, "sha256": sha(p)} for p in sorted(root.iterdir()) if p.is_file()])
    entry.update(bundle=str(root), seal_sha256=sha(root / "seal.json"),
                 official_artifacts={"invocation": "invocation.json", "predictions": "predictions.jsonl", "report": "official.json"})
    return entry


def test_official_chain_validates_without_any_subprocess(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not execute a process"))
    run = adapt(load_source(official_entry(tmp_path)))
    case, = run.cases
    assert case.verdict == "pass" and case.objective == "official_resolution"
    assert case.oracle.authority == "source_validated"
    assert case.scope.subject_role == "scripted_candidate" and case.scope.model_mode == "scripted"
    assert case.source_identity.execution_id == "original-official-run"
    assert len(run.attempts) == 1


def test_consistent_simulated_official_fields_do_not_establish_real_official_scope(tmp_path):
    case, = adapt(load_source(official_entry(tmp_path, environment="simulated_container"))).cases
    assert case.verdict == "unknown" and case.scope.grading == "unknown"
    assert case.oracle.authority == "unavailable"


def test_official_claim_conflicting_with_report_is_rejected(tmp_path):
    entry = official_entry(tmp_path)
    report = json.loads(Path(entry["path"]).read_text())
    report["official_resolved"]["repo__issue-1"] = False
    dump(Path(entry["path"]), report)
    entry["sha256"] = sha(Path(entry["path"]))
    root = Path(entry["bundle"])
    dump(root / "seal.json", [{"path": p.name, "sha256": sha(p)} for p in sorted(root.iterdir()) if p.name != "seal.json"])
    entry["seal_sha256"] = sha(root / "seal.json")
    with pytest.raises(ValueError, match="disagree"):
        adapt(load_source(entry))


def test_gold_control_receipt_and_agent_candidate_have_separate_denominators(tmp_path):
    # Reuse V1's pure archive fixture and parsers; never invoke its run() function.
    from agent_runtime.tests.test_evaluation import official
    from agent_runtime.evaluation import _official, _aggregate
    root = tmp_path / "gold"
    root.mkdir()
    config = official(root)
    inputs = {key: (root / spec["path"]).read_bytes() for key, spec in config["artifacts"].items()}
    row = _official(config, inputs)
    mappings = []
    for role, spec in config["artifacts"].items():
        frozen = config["id"] + "/" + role + ".input"
        target = root / frozen
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(inputs[role])
        mappings.append({"path": frozen, "sha256": spec["sha256"], "source": spec["path"]})
        (root / spec["path"]).unlink()  # Only this synthetic tmp_path fixture.
    dump(root / "inputs.json", mappings)
    plan = {"schema_version": "evaluation-plan-v1", "run_id": "gold-source", "cases": [config]}
    dump(root / "plan.json", plan)
    report = {"schema_version": "evaluation-report-v1", "run_id": "gold-source", "plan_sha256": sha(root / "plan.json"),
              "processing_ok": True, "new_provider_requests": 0, "new_official_evaluations": 0,
              "results": [row], "aggregates": _aggregate([row])}
    dump(root / "report.json", report)
    dump(root / config["id"] / "result.json", row)
    dump(root / "seal.json", [{"path": p.relative_to(root).as_posix(), "sha256": sha(p)}
                              for p in sorted(root.rglob("*")) if p.is_file()])
    entry = {"id": "gold", "path": str(root / "report.json"), "sha256": sha(root / "report.json"),
             "bundle": str(root), "seal_sha256": sha(root / "seal.json"),
             "scope": {"grading": "official_harness"}}
    gold = adapt(load_source(entry))
    assert gold.cases[0].scope.subject_role == "gold_control"
    assert gold.cases[0].raw["task_success"] is None and gold.cases[0].verdict == "pass"
    candidate = adapt(load_source(official_entry(tmp_path)))
    cohorts = aggregate([gold, candidate])["cohorts"]
    assert len(cohorts) == 2
    assert {c["scope"]["subject_role"] for c in cohorts} == {"gold_control", "scripted_candidate"}


def test_conflicting_retry_record_blocks_aggregation(tmp_path):
    records = [{"task_id": "original-task", "model_call_id": 1, "status": "succeeded"}]
    first = adapt(load_source(source(tmp_path, runtime_report(), name="first", sealed=True,
                                     files={"model-calls.json": records})))
    records[0]["status"] = "failed"
    second = adapt(load_source(source(tmp_path, runtime_report(), name="second", sealed=True,
                                      files={"model-calls.json": records})))
    with pytest.raises(ValueError, match="attempt identity"):
        aggregate([first, second])


def test_junit_execution_is_never_agent_task_success(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('<testsuite><testcase name="ok"/><testcase name="fail"><failure>bad</failure></testcase>'
                    '<testcase name="error"><error>setup</error></testcase>'
                    '<testcase name="skip"><skipped/></testcase></testsuite>', encoding="utf-8")
    run = adapt(load_source({"id": "junit", "path": str(path), "sha256": sha(path), "format": "junit"}))
    assert {c.verdict for c in run.cases} == {"pass", "fail", "blocked", "unknown"}
    assert all(c.objective == "test_execution" and c.scope.subject_role == "test_execution" for c in run.cases)
    assert not run.attempts


def test_suite_invocation_identity_requires_persisted_attempt_filename(tmp_path):
    entry = source(tmp_path, {"schema_version": "evaluation-suite-result-v1", "jobs": []})
    old = Path(entry["path"])
    named = old.with_name("a" * 32 + ".json")
    old.rename(named)
    entry["path"] = str(named)
    run = adapt(load_source(entry))
    assert len(run.attempts) == 1 and run.attempts[0].kind == "suite_invocation"


def test_mismatched_projection_origin_is_blocked(tmp_path):
    entry = source(tmp_path, evaluation_row(), name="origin")
    projection = source(tmp_path, {"schema_version": "inspect-offline-result-v1", "source_run_id": "wrong"},
                        name="projection", projection_of="origin")
    with pytest.raises(ValueError, match="projection origin mismatch"):
        project_manifest(manifest(tmp_path, [entry, projection]))


def test_contradictory_legacy_aggregate_is_rejected(tmp_path):
    entry = source(tmp_path, runtime_report(False, runtime_cases={"total": 1, "passed": 1}))
    with pytest.raises(ValueError, match="aggregate outcome mismatch"):
        adapt(load_source(entry))


def test_claimed_complete_usage_with_missing_output_is_incomplete(tmp_path):
    report = {"schema_version": "inspect-model-result-v1", "model_mode": "live", "planned_cases": 1,
              "results": [{"id": "m", "fixture_success": True, "usage": {"complete": True, "input_tokens": 4}}]}
    case, = adapt(load_source(source(tmp_path, report))).cases
    assert case.verdict == "pass"
    assert all(m.value is None and not m.complete for m in case.metrics if m.name.startswith("provider_"))


def test_different_recorded_models_are_never_pooled(tmp_path):
    runs = []
    for i in range(2):
        report = {"schema_version": "inspect-model-result-v1", "model_mode": "live", "model": f"provider/model{i}",
                  "planned_cases": 1, "results": [{"id": "m", "fixture_success": True}]}
        runs.append(adapt(load_source(source(tmp_path, report, name=f"model{i}"))))
    assert len(aggregate(runs)["cohorts"]) == 2
    assert all(c["N"] == 1 for c in aggregate(runs)["cohorts"])


def test_candidate_requiring_manual_recovery_remains_blocked(tmp_path):
    report = {"schema_version": "inspect-swe-candidate-v1", "instance_id": "x", "processing_ok": True,
              "candidate_validated": False, "manual_recovery_required": True, "runtime_status": "needs_review"}
    case, = adapt(load_source(source(tmp_path, report))).cases
    assert case.verdict == "blocked" and case.raw["candidate_validated"] is False


def test_null_comparison_config_does_not_claim_comparability():
    identity = ComparisonIdentity({"task_version": "v1", "condition": "a", "model": None, "budget": None,
                                   "fault": None, "seed": None, "repeat": 0})
    assert identity.key is None and identity.missing == ["budget", "model"]


def test_conflicting_unallocated_denominator_blocks_aggregation(tmp_path):
    report = runtime_report(results=[], runtime_cases={"total": 2, "passed": 0})
    entry = source(tmp_path, report)
    first = adapt(load_source(entry))
    second = adapt(load_source({**entry, "scope": {"environment": "real_container"}}))
    with pytest.raises(ValueError, match="planned denominator identity"):
        aggregate([first, second])


def test_archived_model_attempt_records_are_projected_without_execution(tmp_path):
    entry = source(tmp_path, runtime_report(), sealed=True, files={"edit/model_calls.input": [
        {"task_id": "original-task", "model_call_id": 1, "status": "succeeded"}]})
    run = adapt(load_source(entry))
    assert len(run.attempts) == 2 and run.attempts[1].kind == "model_generation"
    assert run.attempts[1].parent_id == run.attempts[0].attempt_id
