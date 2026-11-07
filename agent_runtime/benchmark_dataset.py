"""Freeze local SWE-bench snapshots and export predictions without running a model.

Solver inputs use an allowlist. Gold/test patches stay in evaluator-owned files.
The official harness remains the sole authority for resolved; this module never
invents a score or an evaluator receipt.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from .eval_runner import _unsafe_reparse_chain
from .evaluation import _dump, _hash, _identifier, _json, _read, _relative
from .evaluation_bundle import _inventory


def _sha(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value):
        raise ValueError("immutable digest/commit required")
    return value


def _fresh(path: Path, sources: list[Path]):
    if (_unsafe_reparse_chain(path) or path.exists()
            or any(path.is_relative_to(p) or p.is_relative_to(path) for p in sources)):
        raise ValueError("output must be fresh and disjoint from input directories")


def prepare(config_path: str | Path, output: str | Path) -> dict:
    config_path, root = Path(os.path.abspath(config_path)), Path(os.path.abspath(output))
    config = _json(_read(config_path))
    required = {"schema_version", "run_id", "dataset_revision", "harness_commit", "snapshot", "sha256", "instance_ids"}
    if not isinstance(config, dict) or set(config) != required or config["schema_version"] != "swebench-dataset-v1":
        raise ValueError("invalid dataset config")
    _identifier(config["run_id"])
    for key in ("dataset_revision", "harness_commit"):
        _sha(config[key], 40)
    data = _read(config_path.parent / _relative(config["snapshot"]))
    if _hash(data) != _sha(config["sha256"]):
        raise ValueError("dataset snapshot digest mismatch")
    ids = config["instance_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
        raise ValueError("select 1..500 explicit instances")
    for ident in ids:
        _identifier(ident)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate selected instance")
    text = data.decode("utf-8-sig").strip()
    records = _json(data) if text.startswith("[") else [_json(line.encode()) for line in text.splitlines() if line.strip()]
    if not isinstance(records, list) or len(records) > 10000:
        raise ValueError("invalid dataset records")
    by_id = {}
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("dataset rows must be objects")
        ident = _identifier(row.get("instance_id"))
        if ident in by_id:
            raise ValueError("duplicate snapshot instance")
        by_id[ident] = row
    if not set(ids) <= by_id.keys():
        raise ValueError("selected instance missing from snapshot")
    selected, solver = [], []
    for ident in ids:
        row = by_id[ident]
        _sha(row.get("base_commit"), 40)
        for key in ("repo", "problem_statement", "patch", "test_patch", "version"):
            if not isinstance(row.get(key), str) or (key not in {"patch", "test_patch"} and not row[key].strip()):
                raise ValueError("missing SWE-bench field: " + key)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["repo"]):
            raise ValueError("invalid repository identity")
        for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            tests = row.get(key)
            if isinstance(tests, str):
                tests = _json(tests.encode())
            if not isinstance(tests, list) or any(not isinstance(t, str) for t in tests):
                raise ValueError("invalid test list: " + key)
        selected.append(row)
        solver.append({key: row[key] for key in ("instance_id", "repo", "base_commit", "problem_statement")})
    _fresh(root, [config_path.parent])
    root.mkdir(parents=True)
    (root / "solver").mkdir()
    (root / "evaluator").mkdir()
    _dump(root / "config.json", config)
    _dump(root / "solver" / "dataset.json", solver)
    _dump(root / "evaluator" / "dataset.json", selected)
    manifest = {"schema_version": "swebench-prepared-v1", "run_id": config["run_id"],
                "dataset_revision": config["dataset_revision"], "harness_commit": config["harness_commit"],
                "snapshot_sha256": config["sha256"], "instance_ids": ids,
                "files": {p.relative_to(root).as_posix(): _hash(_read(p)) for p in sorted(root.rglob("*")) if p.is_file()},
                "new_provider_requests": 0, "new_official_evaluations": 0}
    _dump(root / "manifest.json", manifest)
    return manifest


def verify(root: Path, digest: str) -> dict:
    raw = _read(root / "manifest.json")
    if _hash(raw) != _sha(digest):
        raise ValueError("dataset manifest digest mismatch")
    manifest = _json(raw)
    if manifest.get("schema_version") != "swebench-prepared-v1":
        raise ValueError("unsupported prepared dataset")
    files = manifest["files"]
    if set(files) != {"config.json", "solver/dataset.json", "evaluator/dataset.json"}:
        raise ValueError("unexpected prepared inventory")
    if _inventory(root) != set(files) | {"manifest.json"}:
        raise ValueError("prepared inventory mismatch")
    for name, sha in files.items():
        if _hash(_read(root / name)) != sha:
            raise ValueError("prepared dataset changed")
    return manifest


def predictions(bundle: str | Path, manifest_sha256: str, results: str | Path, output: str | Path) -> dict:
    root, results, output = (Path(os.path.abspath(p)) for p in (bundle, results, output))
    manifest = verify(root, manifest_sha256)
    raw = _read(results)
    rows = _json(raw)
    if not isinstance(rows, list):
        raise ValueError("prediction results must be an array")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"instance_id", "model_name_or_path", "model_patch"}:
            raise ValueError("exact official prediction fields required")
        ident = _identifier(row["instance_id"])
        if ident in by_id or any(not isinstance(row[k], str) for k in ("model_patch", "model_name_or_path")):
            raise ValueError("invalid/duplicate prediction")
        if not row["model_name_or_path"].strip():
            raise ValueError("model identity required")
        by_id[ident] = row
    if set(by_id) != set(manifest["instance_ids"]):
        raise ValueError("all planned instances, including empty patches, must be recorded")
    _fresh(output, [root, results.parent])
    output.mkdir(parents=True)
    ordered = [by_id[i] for i in manifest["instance_ids"]]
    prediction_path = output / "predictions.jsonl"
    prediction_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered), encoding="utf-8")
    # A command recipe, not an invocation or proof of harness identity.
    command = ["python", "-m", "swebench.harness.run_evaluation", "--dataset_name",
               str(root / "evaluator/dataset.json"), "--predictions_path", str(prediction_path),
               "--max_workers", "1", "--run_id", manifest["run_id"], "--instance_ids", *manifest["instance_ids"]]
    report = {"schema_version": "swebench-predictions-v1", "dataset_manifest_sha256": manifest_sha256,
              "results_sha256": _hash(raw), "prediction_sha256": _hash(_read(prediction_path)),
              "total": len(ordered), "empty_patches": sum(not r["model_patch"].strip() for r in ordered),
              "official_resolved": None, "new_official_evaluations": 0,
              "required_harness_commit": manifest["harness_commit"], "command_recipe": command}
    _dump(output / "export.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("config")
    prepare_parser.add_argument("output")
    export_parser = sub.add_parser("predictions")
    export_parser.add_argument("bundle")
    export_parser.add_argument("results")
    export_parser.add_argument("output")
    export_parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        report = prepare(args.config, args.output) if args.command == "prepare" else predictions(
            args.bundle, args.manifest_sha256, args.results, args.output)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"benchmark input error: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
