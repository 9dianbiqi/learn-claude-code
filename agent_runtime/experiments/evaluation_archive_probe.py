"""Freeze two known historical archives and exercise the offline evaluator.

This probe has no provider, Docker or harness dependency. The gold patch remains
an evaluator-only artifact; it is never passed to Runtime or a solver.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_runtime.evaluation import _dump, _hash, _read, run


def probe(readonly_archive: Path, gold_archive: Path, output: Path) -> dict:
    # Fresh outputs only. Refuse links before copying any historical bytes.
    from agent_runtime.eval_runner import _unsafe_reparse_chain

    if output.exists() or _unsafe_reparse_chain(output):
        raise ValueError("probe output must be fresh without reparse parents")
    source = readonly_archive / "swebench-agent-pilot-readonly-20261017-01"
    gold_run = gold_archive / "official-gold-run02"
    official_run_id = "sympy20590-gold-control-20261015-02"
    files = {
        "agent-summary.json": source / "runtime-summary.json",
        "agent-calls.json": source / "raw-model-call-records.json",
        "agent-prediction.jsonl": source / "predictions.jsonl",
        "gold-report.json": gold_run / "logs" / "run_evaluation" / official_run_id /
            "gold-control-no-model" / "sympy__sympy-20590" / "report.json",
        "gold-execution.json": gold_run / "execution.json",
        "gold-prediction.jsonl": gold_archive / "prediction-gold.jsonl",
        "gold-command.json": gold_run / "command.json",
        "gold-original-manifest.json": gold_archive / "frozen-run-manifest.json",
    }
    data = {name: _read(path) for name, path in files.items()}
    manifest = json.loads(data["gold-original-manifest.json"])
    command = json.loads(data["gold-command.json"])
    argv = command["argv"]
    if argv[argv.index("--run_id") + 1] != official_run_id:
        raise ValueError("historical command does not match successful run02")
    # Immutable values verified against the archived environment acceptance report.
    revision = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
    base = "cffd4e0f86fefd4802349a9f9b19ed70934ea354"
    harness = "3f01bd622c0a22c00406139f69a234ef08225f22"
    if manifest["dataset"]["revision"] != revision or manifest["base_commit"] != base:
        raise ValueError("gold archive dataset/base identity mismatch")
    if harness not in json.dumps(manifest["harness"]):
        raise ValueError("gold archive harness identity mismatch")
    summary = json.loads(data["agent-summary.json"])
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    inputs.mkdir()
    for name, payload in data.items():
        (inputs / name).write_bytes(payload)
    _dump(output / "historical-source-manifest.json", [
        {"source_path": str(files[name].absolute()), "copy": "inputs/" + name,
         "sha256": _hash(payload), "historical": True} for name, payload in data.items()])

    def ref(name):
        return {"path": "inputs/" + name, "sha256": _hash(data[name])}

    gold = {"id": "historical-gold-control", "mode": "official_archive", "scope": "gold_control",
            "source_run_id": official_run_id, "instance_id": "sympy__sympy-20590",
            "dataset_revision": revision, "base_commit": base, "harness_commit": harness}
    receipt = {key: gold[key] for key in ("source_run_id", "instance_id", "scope",
                                        "dataset_revision", "base_commit", "harness_commit")}
    receipt.update({role + "_sha256": _hash(data[name]) for role, name in (
        ("report", "gold-report.json"), ("prediction", "gold-prediction.jsonl"),
        ("execution", "gold-execution.json"))})
    receipt["provenance_note"] = (
        "Evaluator import receipt created 2026-10-22; source run02 is historical. "
        "Original frozen plan names run01 and is supporting metadata, not run02 execution proof.")
    _dump(inputs / "gold-import-receipt.json", receipt)
    gold["artifacts"] = {"report": ref("gold-report.json"), "prediction": ref("gold-prediction.jsonl"),
                         "execution": ref("gold-execution.json"),
                         "receipt": {"path": "inputs/gold-import-receipt.json",
                                     "sha256": _hash((inputs / "gold-import-receipt.json").read_bytes())}}
    plan = {"schema_version": "evaluation-plan-v1", "run_id": "historical-import-20261022", "cases": [
        {"id": "historical-agent-empty-patch", "mode": "agent_archive", "model_mode": "live",
         "source_run_id": source.name, "instance_id": "sympy__sympy-19346", "task_id": summary["task_id"],
         "artifacts": {"summary": ref("agent-summary.json"), "model_calls": ref("agent-calls.json"),
                       "prediction": ref("agent-prediction.jsonl")}}, gold]}
    _dump(output / "plan.json", plan)
    return run(output / "plan.json", output / "evaluation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("readonly_archive", type=Path)
    parser.add_argument("gold_archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = probe(args.readonly_archive, args.gold_archive, args.output)
    print(json.dumps({"processing_ok": report["processing_ok"], "aggregates": report["aggregates"]}))
    return 0 if report["processing_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
