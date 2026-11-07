import json

import pytest

from agent_runtime.benchmark_dataset import prepare, predictions, verify
from agent_runtime.evaluation import _dump, _hash


def fixture(tmp_path):
    source = tmp_path / "inputs"
    source.mkdir()
    row = {"instance_id": "repo__issue-1", "repo": "org/repo", "base_commit": "a" * 40,
           "problem_statement": "Fix the bug", "patch": "PRIVATE_GOLD_SENTINEL",
           "test_patch": "PRIVATE_TEST_SENTINEL", "FAIL_TO_PASS": '["test_bug"]',
           "PASS_TO_PASS": ["test_old"], "version": "1"}
    _dump(source / "snapshot.json", [row])
    config = {"schema_version": "swebench-dataset-v1", "run_id": "dataset",
              "dataset_revision": "b" * 40, "harness_commit": "c" * 40,
              "snapshot": "snapshot.json", "sha256": _hash((source / "snapshot.json").read_bytes()),
              "instance_ids": [row["instance_id"]]}
    _dump(source / "config.json", config)
    return source / "config.json", tmp_path / "prepared"


def test_allowlist_keeps_gold_and_tests_out_of_solver(tmp_path):
    config, output = fixture(tmp_path)
    report = prepare(config, output)
    public = (output / "solver/dataset.json").read_text()
    assert "PRIVATE" not in public and "test_bug" not in public
    assert "PRIVATE_GOLD_SENTINEL" in (output / "evaluator/dataset.json").read_text()
    assert report["new_provider_requests"] == report["new_official_evaluations"] == 0
    verify(output, _hash((output / "manifest.json").read_bytes()))


def test_empty_patch_retained_and_official_success_unknown(tmp_path):
    config, output = fixture(tmp_path)
    prepare(config, output)
    result = config.parent / "results.json"
    _dump(result, [{"instance_id": "repo__issue-1", "model_name_or_path": "explicit-model", "model_patch": ""}])
    report = predictions(output, _hash((output / "manifest.json").read_bytes()), result, tmp_path / "export")
    assert report["empty_patches"] == report["total"] == 1
    assert report["official_resolved"] is None and report["new_official_evaluations"] == 0
    assert json.loads((tmp_path / "export/predictions.jsonl").read_text())["model_patch"] == ""


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign", "wrong_type"])
def test_prediction_denominator_cannot_be_filtered(tmp_path, mutation):
    config, output = fixture(tmp_path)
    prepare(config, output)
    row = {"instance_id": "repo__issue-1", "model_name_or_path": "model", "model_patch": "patch"}
    rows = [row]
    if mutation == "missing":
        rows = []
    elif mutation == "duplicate":
        rows = [row, row]
    elif mutation == "foreign":
        row["instance_id"] = "other"
    else:
        row["model_patch"] = None
    results = config.parent / "results.json"
    _dump(results, rows)
    with pytest.raises(ValueError):
        predictions(output, _hash((output / "manifest.json").read_bytes()), results, tmp_path / "export")
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("mutation", ["snapshot", "selected", "commit"])
def test_invalid_provenance_fails_before_output(tmp_path, mutation):
    config, output = fixture(tmp_path)
    data = json.loads(config.read_text())
    if mutation == "snapshot":
        data["sha256"] = "0" * 64
    elif mutation == "selected":
        data["instance_ids"] = ["absent"]
    else:
        data["harness_commit"] = "main"
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        prepare(config, output)
    assert not output.exists()


def test_prepared_gold_tamper_rejected(tmp_path):
    config, output = fixture(tmp_path)
    prepare(config, output)
    sha = _hash((output / "manifest.json").read_bytes())
    (output / "evaluator/dataset.json").write_text("[]")
    with pytest.raises(ValueError, match="changed"):
        verify(output, sha)
