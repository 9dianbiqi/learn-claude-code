import io
import json
import tarfile

import pytest

from agent_runtime.swe_assets import assemble
from agent_runtime.evaluation import _dump, _hash
from agent_runtime.tests.test_benchmark_dataset import fixture


def spec_file(tmp_path):
    config_path, _ = fixture(tmp_path)
    config = json.loads(config_path.read_text())
    path = config_path.parent / "assets-spec.json"
    spec = dict(schema_version="swe-local-assets-v1", run_id="assets", snapshot=str(config_path.parent / "snapshot.json"),
        sha256=config["sha256"], dataset_revision=config["dataset_revision"], harness_commit=config["harness_commit"],
        instance_id="repo__issue-1", repo="org/repo", base_commit="a" * 40, image="sha256:" + "b" * 64,
        probes=[dict(argv=["python", "-V"], stdout_sha256="0" * 64)])
    _dump(path, spec)
    return path, spec


def test_missing_assets_emit_only_draft_and_isolate_gold(tmp_path):
    path, _ = spec_file(tmp_path)
    output = tmp_path / "out"
    result = assemble(path, output)
    assert result["status"] == "ASSETS_INCOMPLETE" and result["missing"] == ["base_export", "responses"]
    assert not (output / "acceptance.json").exists()
    assert "PRIVATE" not in (output / "dataset/solver/dataset.json").read_text()
    assert "PRIVATE_GOLD_SENTINEL" in (output / "dataset/evaluator/dataset.json").read_text()
    assert "PRIVATE" not in json.dumps(result) and not result["current_image_verified"]


@pytest.mark.parametrize("mutation", ["hash", "base", "instance"])
def test_source_pin_or_selection_mismatch_never_creates_output(tmp_path, mutation):
    path, spec = spec_file(tmp_path)
    if mutation == "hash": spec["sha256"] = "0" * 64
    if mutation == "base": spec["base_commit"] = "f" * 40
    if mutation == "instance": spec["instance_id"] = "other"
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError):
        assemble(path, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def add_assets(tmp_path, spec):
    inputs = tmp_path / "exported"
    inputs.mkdir()
    archive = inputs / "base.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("app.py")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    receipt = inputs / "result.json"
    value = {k: spec[k] for k in ("instance_id", "base_commit", "image")}
    value.update(schema_version="swe-base-export-v1", status="EXPORTED_REMOVED", manual_recovery_required=False,
                 archive_path=str(archive), archive_sha256=_hash(archive.read_bytes()))
    _dump(receipt, value)
    responses = inputs / "responses.json"
    _dump(responses, [{"text": "TASK_GAVE_UP"}])
    spec["base_export"] = {"path": str(receipt), "sha256": _hash(receipt.read_bytes())}
    spec["responses"] = {"path": str(responses), "sha256": _hash(responses.read_bytes())}
    return receipt


def test_complete_supplied_assets_emit_loadable_config_without_running(tmp_path):
    from agent_runtime.swe_acceptance import load_config, run
    path, spec = spec_file(tmp_path)
    add_assets(tmp_path, spec)
    path.write_text(json.dumps(spec))
    out = tmp_path / "out"
    result = assemble(path, out)
    assert result["status"] == "ASSET_CONFIG_WRITTEN" and not result["probes_executed"]
    assert run(**load_config(out / "acceptance.json"), output=tmp_path / "future")["status"] == "PREFLIGHT_ONLY"
    assert not (tmp_path / "future").exists()


def test_foreign_or_uncertain_base_export_is_rejected(tmp_path):
    path, spec = spec_file(tmp_path)
    receipt = add_assets(tmp_path, spec)
    value = json.loads(receipt.read_text())
    value["base_commit"] = "c" * 40
    receipt.write_text(json.dumps(value))
    spec["base_export"]["sha256"] = _hash(receipt.read_bytes())
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="identity/status"):
        assemble(path, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_old_output_cannot_be_overwritten(tmp_path):
    path, _ = spec_file(tmp_path)
    out = tmp_path / "out"
    assemble(path, out)
    old = (out / "assets.json").read_bytes()
    with pytest.raises(ValueError, match="fresh"):
        assemble(path, out)
    assert (out / "assets.json").read_bytes() == old
