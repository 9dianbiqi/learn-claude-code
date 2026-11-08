"""Second-review regressions: oracle conflicts, verdict proof and cohort scope."""
from pathlib import Path
import json

import pytest

from agent_runtime.evaluation_adapters import adapt, load_source
from agent_runtime.evaluation_contract import aggregate
from agent_runtime.tests.test_evaluation_bundle import dump, pin
from agent_runtime.tests.test_evaluation_contract import official_entry, source, sha
from agent_runtime.tests.test_evaluation_m1_fixes import container_entry, failed_official_entry


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("verifier_id", ["explicit-other-verifier", "v1-reported-container-verifier"])
def test_explicit_conflicting_container_oracles_are_not_refinements(tmp_path, reverse, verifier_id):
    weak_root, strong_root = tmp_path / "weak", tmp_path / "strong"
    weak_root.mkdir()
    strong_root.mkdir()
    weak = container_entry(weak_root, records=False)
    strong = container_entry(strong_root)
    dump(Path(weak["bundle"]) / "bindings.json", {"repair": {"verifier_id": verifier_id}})
    weak["seal_sha256"] = pin(Path(weak["bundle"]))
    assert weak["sha256"] == strong["sha256"]
    runs = [adapt(load_source(entry)) for entry in (weak, strong)]
    with pytest.raises(ValueError, match="conflict"):
        aggregate(list(reversed(runs)) if reverse else runs)


@pytest.mark.parametrize("status,authoritative", [("fail", False), ("uncertain", False), ("pass", False)])
def test_container_goal_pass_requires_authoritative_success(tmp_path, status, authoritative):
    entry = container_entry(tmp_path)
    path = Path(entry["bundle"]) / "repair/verifier-runs.json"
    records = json.loads(path.read_text())
    records[0].update(status=status, authoritative=authoritative)
    dump(path, records)
    entry["seal_sha256"] = pin(Path(entry["bundle"]))
    with pytest.raises(ValueError, match="verifier.*success|success.*verifier"):
        adapt(load_source(entry))


@pytest.mark.parametrize("status", ["FAILED", "STARTED"])
def test_bound_official_failures_remain_in_official_cohort(tmp_path, status):
    positive = official_entry(tmp_path)
    negative = failed_official_entry(tmp_path, status=status)
    negative["scope"] = dict(positive["scope"])
    runs = [adapt(load_source(e)) for e in (positive, negative)]
    cohort, = aggregate(runs)["cohorts"]
    assert cohort["scope"]["grading"] == "official_harness"
    assert cohort["N"] == 3 and cohort["pass"] == 1
    assert cohort["blocked" if status == "FAILED" else "unknown"] == 2
    assert cohort["strict_success_rate"] is None
    assert cohort["success_lower_bound"] == pytest.approx(1 / 3)
    assert all(c.oracle.authority == "unavailable" for c in runs[1].cases)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("view", ["file_only", "binding_only"])
def test_unknown_or_same_explicit_identity_can_be_enriched(tmp_path, reverse, view):
    root1, root2 = tmp_path / "weak", tmp_path / "strong"
    root1.mkdir()
    root2.mkdir()
    strong = container_entry(root2)
    if view == "file_only":
        weak = {k: v for k, v in strong.items() if k not in {"bundle", "seal_sha256"}}
    else:
        weak = container_entry(root1, records=False)
    runs = [adapt(load_source(e)) for e in (weak, strong)]
    result = aggregate(list(reversed(runs)) if reverse else runs)
    assert result["unique_case_observations"] == 1 and result["cohorts"][0]["N"] == 1
    oracle = runs[1].cases[0].oracle
    assert oracle.identity_status == "explicit"
    assert oracle.identity_components == {"verifier_id": "frozen-container-probes", "bundle_sha256": "c" * 64}


@pytest.mark.parametrize("last_status,last_authority,passes", [("pass", True, True), ("fail", False, False), ("uncertain", False, False)])
def test_container_verdict_uses_latest_invocation_not_best(tmp_path, last_status, last_authority, passes):
    entry = container_entry(tmp_path)
    path = Path(entry["bundle"]) / "repair/verifier-runs.json"
    first = json.loads(path.read_text())[0]
    first.update(status="fail" if passes else "pass", authoritative=not passes)
    last = {**first, "verifier_run_id": "v2", "status": last_status, "authoritative": last_authority}
    dump(path, [first, last])
    entry["seal_sha256"] = pin(Path(entry["bundle"]))
    if passes:
        assert adapt(load_source(entry)).cases[0].verdict == "pass"
    else:
        with pytest.raises(ValueError, match="verifier.*success|success.*verifier"):
            adapt(load_source(entry))


@pytest.mark.parametrize("missing", ["status", "authoritative"])
def test_missing_invocation_outcome_preserves_identity_but_stays_reported(tmp_path, missing):
    entry = container_entry(tmp_path)
    path = Path(entry["bundle"]) / "repair/verifier-runs.json"
    records = json.loads(path.read_text())
    records[0].pop(missing)
    dump(path, records)
    entry["seal_sha256"] = pin(Path(entry["bundle"]))
    case, = adapt(load_source(entry)).cases
    assert case.oracle.authority == "reported" and case.oracle.identity_status == "explicit"
    assert case.oracle.identity_components["bundle_sha256"] == "c" * 64


@pytest.mark.parametrize("status", ["fail", "uncertain"])
def test_expected_failed_runtime_regression_is_still_a_pass(tmp_path, status):
    entry = container_entry(tmp_path)
    root = Path(entry["bundle"])
    plan = json.loads((root / "plan.json").read_text())
    plan["cases"][0]["checks"] = {"status": "failed"}
    dump(root / "plan.json", plan)
    report = json.loads(Path(entry["path"]).read_text())
    report.update(schema_version="inspect-runtime-result-v1", plan_sha256=sha(root / "plan.json"),
                  runtime_cases={"total": 1, "passed": 1})
    report["results"][0].pop("fixture_success")
    report["results"][0]["runtime_status"] = "failed"
    dump(Path(entry["path"]), report)
    records = json.loads((root / "repair/verifier-runs.json").read_text())
    records[0].update(status=status, authoritative=False)
    dump(root / "repair/verifier-runs.json", records)
    entry.update(sha256=sha(Path(entry["path"])), seal_sha256=pin(root))
    case, = adapt(load_source(entry)).cases
    assert case.objective == "runtime_regression" and case.verdict == "pass"


def test_preflight_scope_and_bare_official_claim_do_not_create_authority(tmp_path):
    preflight = failed_official_entry(tmp_path, status="PREFLIGHT_ONLY")
    scope = {"model_mode": "scripted", "environment": "real_container", "grading": "official_harness",
             "subject_role": "scripted_candidate"}
    preflight["scope"] = scope
    run = adapt(load_source(preflight))
    assert not run.attempts and all(not c.attempt_refs for c in run.cases)
    assert all(c.scope.grading == "unknown" and c.verdict == "unknown" for c in run.cases)
    bare = source(tmp_path, {"schema_version": "swe-official-run-v1", "status": "REPORTED",
                            "official_resolved": {"instance": True}}, scope=scope)
    case, = adapt(load_source(bare)).cases
    assert case.verdict == "unknown" and case.oracle.authority == "unavailable"
    assert case.scope.grading == "unknown"


@pytest.mark.parametrize("authority,components", [("source_validated", {}), ("reported", {"verifier_id": "known"})])
def test_placeholder_oracle_cannot_claim_explicit_proof(authority, components):
    from agent_runtime.evaluation_contract import Oracle
    with pytest.raises(ValueError, match="placeholder"):
        Oracle("missing", "controller_verifier", authority, identity_status="placeholder", identity_components=components)


@pytest.mark.parametrize("reverse", [False, True])
def test_known_bundle_conflict_is_not_hidden_by_missing_outcome(tmp_path, reverse):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    weak, strong = container_entry(left), container_entry(right)
    path = Path(weak["bundle"]) / "repair/verifier-runs.json"
    records = json.loads(path.read_text())
    records[0].pop("authoritative")
    dump(path, records)
    weak["seal_sha256"] = pin(Path(weak["bundle"]))
    path = Path(strong["bundle"]) / "repair/verifier-runs.json"
    records = json.loads(path.read_text())
    records[0]["verifier_bundle_hash"] = "d" * 64
    dump(path, records)
    strong["seal_sha256"] = pin(Path(strong["bundle"]))
    runs = [adapt(load_source(e)) for e in (weak, strong)]
    with pytest.raises(ValueError, match="conflict"):
        aggregate(list(reversed(runs)) if reverse else runs)


@pytest.mark.parametrize("reverse", [False, True])
def test_reported_identity_can_gain_bundle_without_authority_upgrade(tmp_path, reverse):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    weak, richer = container_entry(left, records=False), container_entry(right)
    path = Path(richer["bundle"]) / "repair/verifier-runs.json"
    records = json.loads(path.read_text())
    records[0].pop("authoritative")
    dump(path, records)
    richer["seal_sha256"] = pin(Path(richer["bundle"]))
    runs = [adapt(load_source(e)) for e in (weak, richer)]
    assert all(r.cases[0].oracle.authority == "reported" for r in runs)
    result = aggregate(list(reversed(runs)) if reverse else runs)
    assert result["unique_case_observations"] == 1 and result["cohorts"][0]["N"] == 1
