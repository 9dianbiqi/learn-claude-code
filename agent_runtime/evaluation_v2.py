"""Opt-in, result-only V2 report CLI: python -m agent_runtime.evaluation_v2 INPUT NEW_OUTPUT.

Sources require explicit external SHA-256 pins. Every supplied seal member is
checked before any output is created. This module never invokes an evaluator.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .benchmark_dataset import _fresh
from .evaluation import _dump, _hash, _identifier, _json, _read
from .evaluation_adapters import adapt, load_source
from .evaluation_contract import aggregate

INPUT_SCHEMA = "evaluation-v2-input-v1"


def project_manifest(path: str | Path):
    path = Path(path).absolute()
    raw = _read(path)
    manifest = _json(raw)
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "run_id", "entries"}
            or manifest["schema_version"] != INPUT_SCHEMA
            or not isinstance(manifest["entries"], list) or not 1 <= len(manifest["entries"]) <= 64):
        raise ValueError("invalid V2 input manifest")
    _identifier(manifest["run_id"])
    names, sources = set(), []
    for entry in manifest["entries"]:
        ident = _identifier(entry.get("id"))
        if ident.casefold() in names:
            raise ValueError("duplicate input entry identity")
        names.add(ident.casefold())
        sources.append(load_source(entry))
    by_id = {s.entry["id"]: s for s in sources}
    for source in sources:
        target = source.entry.get("projection_of")
        if target is not None:
            origin = by_id.get(target)
            if (source.report["schema_version"] != "inspect-offline-result-v1" or origin is None
                    or origin.report["schema_version"] != "evaluation-report-v1"
                    or not origin.evidence.seal_sha256
                    or source.report.get("source_seal_sha256") != origin.evidence.seal_sha256
                    or source.report.get("source_run_id") != origin.report.get("run_id")
                    or source.report.get("source_processing_ok") != origin.report.get("processing_ok")
                    or source.report.get("source_aggregates") != origin.report.get("aggregates")):
                raise ValueError("Inspect projection origin mismatch")
    runs = [adapt(s) for s in sources]
    cohorts = aggregate(runs)  # Conflicts abort before output, never last-writer-wins.
    result = {"schema_version": "evaluation-report-v2", "run_id": manifest["run_id"],
              "producer": {p: _hash(_read(Path(__file__).with_name(p))) for p in
                           ("evaluation_contract.py", "evaluation_adapters.py", "evaluation_v2.py")},
              "manifest_sha256": _hash(raw), "runs": [r.as_dict() for r in runs],
              "metrics": cohorts, "new_runtime_executions": 0, "new_provider_requests": 0,
              "new_official_evaluations": 0,
              "scope_semantics": "source-constrained controller declarations; hashes do not authenticate authors",
              "limitations": sorted({note for run in runs for note in run.limitations})}
    return raw, sources, result


def markdown(result: dict) -> str:
    def cell(value):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = ["# Evaluation V2: " + result["run_id"], "",
             "Read-only import of existing evidence. New Runtime/model/Harness executions: 0/0/0.",
             "Scopes are source-constrained controller declarations; hashes verify bytes, not authors.",
             "JUnit, regression, fixture goals, candidate validation and official resolution have separate objectives.", "",
             "| Objective | Execution scope | Frozen comparison context | N | Pass | Fail | Blocked | Unknown | Lower bound | Strict rate | Known fraction |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in result["metrics"]["cohorts"]:
        lines.append("| " + " | ".join(cell(row[k]) for k in
            ("objective", "scope", "comparison_context", "N", "pass", "fail", "blocked", "unknown", "success_lower_bound",
             "strict_success_rate", "known_fraction")) + " |")
    lines += ["", "## Evidence observations and mapping", "",
              "Duplicate imports below remain visible; cohort denominators deduplicate their original executions/observations.", "",
              "| Source | Case / objective | Verdict | Reason | Execution identity / attempts | Evidence | Metric completeness |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for run in result["runs"]:
        if not run["cases"]:
            lines.append("| " + cell(run["run_id"]) + " | summary/projection only | unknown | no task cases inferred | none inferred | "
                         + cell([e["evidence_id"] for e in run["evidence"]]) + " | not supplied |")
        for case in run["cases"]:
            lines.append("| " + " | ".join(map(cell, (run["run_id"], case["case_id"] + " / " + case["objective"],
                case["verdict"], case["reason"], {"source": case["source_identity"], "attempt_refs": case["attempt_refs"]},
                case["evidence_refs"], {m["name"]: {"complete": m["complete"], "missing_reason": m["missing_reason"]}
                                         for m in case["metrics"]}))) + " |")
    lines += ["", "## Evidence references", ""]
    for run in result["runs"]:
        for evidence in run["evidence"]:
            lines.append("- " + cell(evidence["evidence_id"]) + ": `" + cell(evidence["path"]) + "`; SHA-256 `"
                         + evidence["sha256"] + "`; provenance `" + evidence["provenance"] + "`.")
    lines += ["", "## Completeness and limits", ""]
    lines.extend("- " + cell(note) for note in result["limitations"])
    lines += ["", "No cross-scope overall success rate. Missing measurements remain null. Original fields are retained in report.json."]
    return "\n".join(lines) + "\n"


def render(manifest_path: str | Path, output: str | Path) -> dict:
    path, output = Path(manifest_path).absolute(), Path(output).absolute()
    raw, sources, result = project_manifest(path)
    inputs = [path.parent] + [Path(s.entry.get("bundle", Path(s.evidence.path).parent)) for s in sources]
    _fresh(output, inputs)
    # mkdir is exclusive even if another writer appears after the fresh-path check.
    output.mkdir(parents=True, exist_ok=False)
    (output / "sources").mkdir()
    (output / "input.json").write_bytes(raw)
    for source in sources:
        content = _read(Path(source.evidence.path))
        if _hash(content) != source.evidence.sha256:
            raise ValueError("source changed during report generation; partial output preserved")
        (output / "sources" / (source.entry["id"] + ".bin")).write_bytes(content)
    _dump(output / "report.json", result)
    (output / "report.md").write_text(markdown(result), encoding="utf-8", newline="\n")
    _dump(output / "seal.json", [{"path": p.relative_to(output).as_posix(), "sha256": _hash(_read(p))}
                                 for p in sorted(output.rglob("*")) if p.is_file()])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("output")
    args = parser.parse_args()
    try:
        result = render(args.manifest, args.output)
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
        parser.exit(2, f"V2 evidence input error: {exc}\n")
    print(json.dumps({"schema_version": result["schema_version"], "metrics": result["metrics"],
                      "seal_sha256": _hash(_read(Path(args.output) / "seal.json"))}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
