"""Explicit local SWE-bench v3 grading invocation; no model calls.

Requires a clean controller-owned harness Git checkout at the dataset's pin.
The execution host must be Linux with the harness dependencies and Docker ready.
"""
from pathlib import Path
import re
import subprocess
import sys

from .benchmark_dataset import verify, _fresh
from .evaluation import _dump, _hash, _json, _read


def harness_identity(root, commit):
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    if git("rev-parse", "HEAD") != commit or git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("clean harness checkout at pinned commit required")
    ignored = git("ls-files", "--others", "--ignored", "--exclude-standard", "swebench")
    if any(Path(p).suffix in {".py", ".pyc", ".so", ".pyd"} for p in ignored.splitlines()):
        raise ValueError("ignored executable harness files are not allowed")
    if not (root / "swebench/harness/run_evaluation.py").is_file():
        raise ValueError("official harness entry point missing")


def read_report(path, ids):
    report = _json(_read(path))
    expected = set(ids)
    def group(key):
        values = report[key]
        if (not isinstance(values, list) or any(not isinstance(v, str) for v in values)
                or len(values) != len(set(values)) or not set(values) <= expected):
            raise ValueError("invalid official report IDs")
        return set(values)
    if report.get("schema_version") != 2 or group("submitted_ids") != expected:
        raise ValueError("official report denominator mismatch")
    completed, resolved, unresolved = (group(k + "_ids") for k in ("completed", "resolved", "unresolved"))
    empty, errors, incomplete = (group(k + "_ids") for k in ("empty_patch", "error", "incomplete"))
    if resolved & unresolved or completed != resolved | unresolved:
        raise ValueError("inconsistent official outcomes")
    groups = [completed, empty, errors, incomplete]
    if set.union(*groups) != expected or sum(map(len, groups)) != len(expected):
        raise ValueError("official report does not partition planned instances")
    for key, count in {"total_instances": len(expected), "submitted_instances": len(expected),
                       "completed_instances": len(completed), "resolved_instances": len(resolved),
                       "unresolved_instances": len(unresolved), "empty_patch_instances": len(empty),
                       "error_instances": len(errors)}.items():
        if type(report.get(key)) is not int or report[key] != count:
            raise ValueError("official report count mismatch")
    return {i: True if i in resolved else False if i in unresolved or i in empty else None for i in ids}


def grade(bundle, manifest_sha256, predictions, prediction_sha256, harness, output,
          *, execute=False, timeout=600, wall_timeout=1800):
    bundle, predictions, harness, output = (Path(p).absolute() for p in (bundle, predictions, harness, output))
    manifest = verify(bundle, manifest_sha256)
    raw = _read(predictions)
    if _hash(raw) != prediction_sha256:
        raise ValueError("prediction digest mismatch")
    rows = [_json(line) for line in raw.splitlines() if line.strip()]
    if not rows or any(not isinstance(r, dict) or set(r) != {"instance_id", "model_name_or_path", "model_patch"}
                       or any(not isinstance(v, str) for v in r.values()) for r in rows):
        raise ValueError("invalid standard predictions")
    ids = [r["instance_id"] for r in rows]
    models = {r["model_name_or_path"] for r in rows}
    if len(set(ids)) != len(ids) or set(ids) != set(manifest["instance_ids"]):
        raise ValueError("all planned predictions required, including empty patches")
    if len(models) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,127}", next(iter(models))):
        raise ValueError("single path-safe model identity required")
    if type(timeout) is not int or type(wall_timeout) is not int or not 1 <= timeout <= wall_timeout <= 7200:
        raise ValueError("bounded timeouts required")
    _fresh(output, [bundle, predictions.parent, harness])
    harness_identity(harness, manifest["harness_commit"])
    result = {"schema_version": "swe-official-run-v1", "status": "PREFLIGHT_ONLY",
              "dataset_manifest_sha256": manifest_sha256, "prediction_sha256": prediction_sha256,
              "harness_commit": manifest["harness_commit"], "new_provider_requests": 0,
              "new_official_evaluations": 0, "official_resolved": None,
              "manual_recovery_required": False, "error": None}
    if not execute:
        return result
    if sys.platform != "linux":
        raise ValueError("execute on the prepared Linux Docker host")
    output.mkdir(parents=True)
    (output / "dataset.json").write_bytes(_read(bundle / "evaluator/dataset.json"))
    (output / "predictions.jsonl").write_bytes(raw)
    # Isolated Python ignores cwd/PYTHONPATH; use exactly the checked-out package.
    bootstrap = "import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('swebench.harness.run_evaluation',run_name='__main__')"
    command = [sys.executable, "-I", "-B", "-c", bootstrap, str(harness),
               "--dataset_name", str(output / "dataset.json"), "--predictions_path", str(output / "predictions.jsonl"),
               "--instance_ids", *ids, "--run_id", manifest["run_id"], "--max_workers", "1",
               "--timeout", str(timeout), "--cache_level", "instance", "--clean", "False",
               "--namespace", "", "--report_dir", str(output)]
    _dump(output / "invocation.json", {**result, "argv": command, "wall_timeout": wall_timeout})
    result.update(status="STARTED", manual_recovery_required=True, new_official_evaluations=None)
    try:
        with (output / "console.log").open("wb") as log:
            process = subprocess.run(command, cwd=output, stdout=log, stderr=subprocess.STDOUT, timeout=wall_timeout)
        result["exit_code"] = process.returncode
        if process.returncode:
            raise RuntimeError("official harness returned nonzero")
        harness_identity(harness, manifest["harness_commit"])
        report = output / (next(iter(models)).replace("/", "__") + "." + manifest["run_id"] + ".json")
        outcomes = read_report(report, ids)
        official = _json(_read(report))
        if set(official["empty_patch_ids"]) != {r["instance_id"] for r in rows if not r["model_patch"]}:
            raise ValueError("official empty-patch IDs disagree with submitted predictions")
        result.update(status="REPORTED", official_resolved=outcomes,
                      report_sha256=_hash(_read(report)), new_official_evaluations=1,
                      manual_recovery_required=any(v is None for v in outcomes.values()))
    except Exception as exc:
        result.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
    _dump(output / "result.json", result)
    return result
