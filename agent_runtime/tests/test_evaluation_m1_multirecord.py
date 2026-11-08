"""Multi-invocation M1 boundaries, using result files only."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_runtime.evaluation_adapters import adapt, load_source
from agent_runtime.evaluation_contract import aggregate
from agent_runtime.tests.test_evaluation_bundle import dump, pin
from agent_runtime.tests.test_evaluation_m1_fixes import container_entry


def with_records(tmp_path, records):
    entry = container_entry(tmp_path)
    path = Path(entry["bundle"]) / "repair/verifier-runs.json"
    template = json.loads(path.read_text())[0]
    rows = [{**template, "subtask_id": "fixture", "plan_item_id": 1,
             "verifier_run_id": f"v{i}", **changes} for i, changes in enumerate(records)]
    dump(path, rows)
    entry["seal_sha256"] = pin(Path(entry["bundle"]))
    return entry, rows


@pytest.mark.parametrize("subtask,item", [("other-item", 2), ("other-item", 1), ("fixture", 2)])
def test_foreign_item_success_cannot_cover_fixture_failure(tmp_path, subtask, item):
    entry, _ = with_records(tmp_path, [
        {"status": "fail", "authoritative": False},
        {"subtask_id": subtask, "plan_item_id": item, "status": "pass", "authoritative": True}])
    with pytest.raises(ValueError, match="subtask|plan.item"):
        adapt(load_source(entry))


@pytest.mark.parametrize("missing", [("subtask_id",), ("plan_item_id",), ("subtask_id", "plan_item_id")])
def test_missing_target_association_stays_partial(tmp_path, missing):
    entry, rows = with_records(tmp_path, [{}])
    for key in missing:
        rows[0].pop(key)
    dump(Path(entry["bundle"]) / "repair/verifier-runs.json", rows)
    entry["seal_sha256"] = pin(Path(entry["bundle"]))
    case, = adapt(load_source(entry)).cases
    assert case.oracle.authority == "reported"
    assert case.oracle.identity_components["bundle_sha256"] == "c" * 64


def test_same_item_retry_failure_then_success_remains_valid(tmp_path):
    entry, _ = with_records(tmp_path, [{"status": "fail", "authoritative": False}, {}])
    case, = adapt(load_source(entry)).cases
    assert case.verdict == "pass" and case.oracle.authority == "source_validated"


@pytest.mark.parametrize("invalid", [True, 0, -1, "1", 1.5])
def test_invalid_plan_item_identity_is_not_treated_as_missing(tmp_path, invalid):
    entry, _ = with_records(tmp_path, [{"plan_item_id": invalid}])
    with pytest.raises(ValueError, match="plan.item"):
        adapt(load_source(entry))


@pytest.mark.parametrize("missing_position", [0, 1, 2])
def test_missing_hash_does_not_hide_known_bundle_conflicts(tmp_path, missing_position):
    hashes = ["c" * 64, "d" * 64]
    hashes.insert(missing_position, None)
    entry, _ = with_records(tmp_path, [{"verifier_bundle_hash": value} for value in hashes])
    with pytest.raises(ValueError, match="bundle.*conflict"):
        adapt(load_source(entry))


def test_missing_hash_does_not_hide_malformed_known_hash(tmp_path):
    entry, _ = with_records(tmp_path, [{"verifier_bundle_hash": "bad"}, {"verifier_bundle_hash": None}])
    with pytest.raises(ValueError, match="SHA-256"):
        adapt(load_source(entry))


@pytest.mark.parametrize("hashes", [(None, "c" * 64), ("c" * 64, None)])
def test_partial_consistent_hashes_retain_known_identity_without_filling_missing(tmp_path, hashes):
    entry, rows = with_records(tmp_path, [{"verifier_bundle_hash": value} for value in hashes])
    before = deepcopy(rows)
    run = adapt(load_source(entry))
    case, = run.cases
    assert case.oracle.authority == "reported"
    assert case.oracle.identity_components["bundle_sha256"] == "c" * 64
    assert json.loads((Path(entry["bundle"]) / "repair/verifier-runs.json").read_text()) == before


def test_all_bundle_hashes_missing_does_not_invent_identity(tmp_path):
    entry, _ = with_records(tmp_path, [{"verifier_bundle_hash": None}])
    case, = adapt(load_source(entry)).cases
    assert case.oracle.authority == "reported" and "bundle_sha256" not in case.oracle.identity_components


@pytest.mark.parametrize("reverse", [False, True])
def test_partial_known_bundle_cannot_be_refined_to_a_different_bundle(tmp_path, reverse):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    weak, _ = with_records(left, [{"verifier_bundle_hash": "c" * 64}, {"verifier_bundle_hash": None}])
    strong, _ = with_records(right, [{"verifier_bundle_hash": "d" * 64}])
    # Distinct invocation IDs isolate oracle refinement from attempt-ID conflicts.
    for entry, prefix in ((weak, "weak"), (strong, "strong")):
        path = Path(entry["bundle"]) / "repair/verifier-runs.json"
        records = json.loads(path.read_text())
        for i, record in enumerate(records):
            record["verifier_run_id"] = f"{prefix}-{i}"
        dump(path, records)
        entry["seal_sha256"] = pin(Path(entry["bundle"]))
    runs = [adapt(load_source(entry)) for entry in (weak, strong)]
    with pytest.raises(ValueError, match="conflict"):
        aggregate(list(reversed(runs)) if reverse else runs)
