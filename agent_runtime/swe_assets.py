"""Freeze one real local dataset row and assemble explicitly supplied assets.

No Docker/model/grader calls. Missing assets produce a draft, never a runnable
acceptance config. Historical image IDs are hints until the host rechecks them.
"""
import argparse
import io
import json
from pathlib import Path
import re

from .benchmark_dataset import _fresh, _sha, prepare, verify
from .docker_workspace import CommandProbe
from .evaluation import _dump, _hash, _identifier, _json, _read
from .swe_environment import checked_archive


def dataset_row(data, suffix, instance_id):
    if suffix == ".parquet":
        import pyarrow.parquet as pq
        # The evaluator is controller-owned. Never print gold/test columns or
        # pass them to model-facing fields; benchmark_dataset applies its allowlist.
        rows = pq.read_table(io.BytesIO(data)).to_pylist()
    else:
        rows = _json(data)
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("bounded dataset records required")
    matches = [row for row in rows if isinstance(row, dict) and row.get("instance_id") == instance_id]
    if len(matches) != 1:
        raise ValueError("unique selected instance required")
    return matches[0]


def assemble(spec_path, output):
    spec_path, output = Path(spec_path).absolute(), Path(output).absolute()
    spec_data = _read(spec_path)
    spec = _json(spec_data)
    required = {"schema_version", "run_id", "snapshot", "sha256", "dataset_revision", "harness_commit",
                "instance_id", "repo", "base_commit", "image", "probes"}
    if (not isinstance(spec, dict) or not required <= set(spec)
            or set(spec) - required - {"base_export", "responses", "harness"}
            or spec["schema_version"] != "swe-local-assets-v1"):
        raise ValueError("invalid local asset specification")
    _identifier(spec["run_id"])
    _identifier(spec["instance_id"])
    for key in ("dataset_revision", "harness_commit", "base_commit"):
        _sha(spec[key], 40)
    if not isinstance(spec["image"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", spec["image"]):
        raise ValueError("immutable historical image identity required")
    snapshot = Path(spec["snapshot"])
    if not snapshot.is_absolute():
        raise ValueError("absolute snapshot path required")
    raw = _read(snapshot)
    if _hash(raw) != _sha(spec["sha256"]):
        raise ValueError("source snapshot digest mismatch")
    row = dataset_row(raw, snapshot.suffix, spec["instance_id"])
    if row.get("repo") != spec["repo"] or row.get("base_commit") != spec["base_commit"]:
        raise ValueError("selected repository/base differs from asset specification")
    # Prevalidate the row with the existing preparer before any output writes.
    for key in ("problem_statement", "version", "patch", "test_patch"):
        if not isinstance(row.get(key), str) or (key not in {"patch", "test_patch"} and not row[key].strip()):
            raise ValueError("missing dataset field: " + key)
    for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        values = row.get(key)
        if isinstance(values, str):
            values = _json(values.encode())
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError("invalid dataset test list")
    if not isinstance(spec["probes"], list) or not spec["probes"]:
        raise ValueError("explicit controller probes required")
    for probe in spec["probes"]:
        if (not isinstance(probe, dict) or not {"argv", "stdout_sha256"} <= set(probe)
                or set(probe) - {"argv", "stdout_sha256", "timeout_seconds"}
                or not isinstance(probe["argv"], list)):
            raise ValueError("invalid probe specification")
        CommandProbe(tuple(probe["argv"]), probe["stdout_sha256"], probe.get("timeout_seconds", 5))
    sources = [spec_path.parent, snapshot.parent]
    archive, archive_sha, base_receipt = None, None, None
    if "base_export" in spec:
        pin = spec["base_export"]
        if not isinstance(pin, dict) or set(pin) != {"path", "sha256"} or not Path(pin["path"]).is_absolute():
            raise ValueError("pinned absolute base export receipt required")
        receipt_path = Path(pin["path"])
        receipt_raw = _read(receipt_path)
        if _hash(receipt_raw) != _sha(pin["sha256"]):
            raise ValueError("base export receipt digest mismatch")
        base_receipt = _json(receipt_raw)
        if (base_receipt.get("schema_version") != "swe-base-export-v1"
                or base_receipt.get("status") != "EXPORTED_REMOVED"
                or base_receipt.get("manual_recovery_required") is not False
                or any(base_receipt.get(k) != spec[k] for k in ("instance_id", "base_commit", "image"))):
            raise ValueError("base export identity/status mismatch")
        archive = Path(base_receipt["archive_path"])
        if not archive.is_absolute():
            raise ValueError("absolute exported archive required")
        archive_sha = base_receipt["archive_sha256"]
        checked_archive(archive, archive_sha)
        sources += [receipt_path.parent, archive.parent]
    responses = None
    if "responses" in spec:
        pin = spec["responses"]
        if not isinstance(pin, dict) or set(pin) != {"path", "sha256"} or not Path(pin["path"]).is_absolute():
            raise ValueError("pinned absolute response file required")
        source = Path(pin["path"])
        data = _read(source)
        if _hash(data) != _sha(pin["sha256"]):
            raise ValueError("response digest mismatch")
        responses = _json(data)
        from .inspect_runtime_solver import validate_mock_responses
        from .container_edits import DockerRepairBridge
        validate_mock_responses(responses, DockerRepairBridge.names)
        sources.append(source.parent)
    if spec.get("harness"):
        harness = Path(spec["harness"])
        if not harness.is_absolute():
            raise ValueError("absolute harness path required")
        sources.append(harness)
    _fresh(output, sources)
    output.mkdir(parents=True)
    inputs = output / "inputs"
    inputs.mkdir()
    selected = inputs / "selected.json"
    _dump(selected, [row])
    config = {"schema_version": "swebench-dataset-v1", "run_id": spec["run_id"],
              "dataset_revision": spec["dataset_revision"], "harness_commit": spec["harness_commit"],
              "snapshot": selected.name, "sha256": _hash(_read(selected)), "instance_ids": [spec["instance_id"]]}
    _dump(inputs / "dataset-config.json", config)
    prepared = output / "dataset"
    prepare(inputs / "dataset-config.json", prepared)
    manifest_sha = _hash(_read(prepared / "manifest.json"))
    verify(prepared, manifest_sha)
    draft = {"bundle": str(prepared), "manifest_sha256": manifest_sha,
             "instance_id": spec["instance_id"], "image": spec["image"],
             "base_archive": str(archive) if archive else None, "base_archive_sha256": archive_sha,
             "probes": spec["probes"], "responses": responses, "max_turns": 8}
    if spec.get("harness"):
        draft["harness"] = spec["harness"]
    missing = [name for name, available in (("base_export", archive is not None), ("responses", responses is not None)) if not available]
    _dump(output / "draft.json", {"schema_version": "swe-asset-draft-v1", "missing": missing, "config": draft})
    if not missing:
        _dump(output / "acceptance.json", draft)
    result = {"schema_version": "swe-assets-v1", "status": "ASSET_CONFIG_WRITTEN" if not missing else "ASSETS_INCOMPLETE",
              "spec_sha256": _hash(spec_data), "source_snapshot_sha256": spec["sha256"],
              "dataset_revision": spec["dataset_revision"], "dataset_manifest_sha256": manifest_sha,
              "instance_id": spec["instance_id"], "repo": spec["repo"], "base_commit": spec["base_commit"],
              "image_hint": spec["image"], "current_image_verified": False,
              "probes_executed": False, "missing": missing, "harness_verified": False,
              "new_provider_requests": 0, "new_official_evaluations": 0,
              "note": "Controller pins bind local bytes; they do not authenticate provenance. No execution readiness is claimed."}
    _dump(output / "assets.json", result)
    _dump(output / "spec.json", spec)
    lines = ["# Local SWE asset preparation", "", f"Instance: `{spec['instance_id']}`; base: `{spec['base_commit']}`.",
             f"Status: **{result['status']}**. Current image and probes remain unverified.", "",
             "| Asset | State |", "| --- | --- |", "| Prepared dataset | pinned and solver/evaluator separated |",
             "| Image | historical immutable identity hint; host check required |",
             f"| Base archive | {'receipt/hash checked' if archive else 'missing; export the exact base from the pinned image'} |",
             f"| Script responses | {'frozen' if responses else 'missing; inspect the actual base and freeze read/edit responses'} |",
             "| Candidate probes | controller supplied, not executed |", "| Official harness | not verified |", "",
             "Only a complete asset set emits acceptance.json. draft.json is not a runnable config.",
             "No model, container or benchmark executed; no prediction or official outcome was generated."]
    (output / "assets.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec")
    parser.add_argument("output")
    args = parser.parse_args()
    try:
        result = assemble(args.spec, args.output)
    except Exception as exc:
        parser.exit(2, f"asset preparation error: {type(exc).__name__}: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ASSET_CONFIG_WRITTEN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
