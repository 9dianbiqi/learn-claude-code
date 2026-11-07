"""Render pinned existing evidence without executing tests, models or containers."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .benchmark_dataset import _fresh, _sha
from .evaluation import _dump, _hash, _identifier, _json, _read


SCOPES = {"offline_simulated", "real_container", "official", "historical"}


def summarize(data, kind):
    if kind == "junit":
        root = ET.fromstring(data)
        if root.tag not in {"testsuite", "testsuites"}:
            raise ValueError("JUnit suite required")
        cases = list(root.iter("testcase"))
        if not cases:
            raise ValueError("JUnit contains no test cases")
        counts = {k: 0 for k in ("passed", "failed", "errors", "skipped")}
        for case in cases:
            key = ("errors" if case.find("error") is not None else
                   "failed" if case.find("failure") is not None else
                   "skipped" if case.find("skipped") is not None else "passed")
            counts[key] += 1
        return {"status": "FAILED" if counts["failed"] or counts["errors"] else
                          "PASSED" if counts["passed"] else "SKIPPED_ONLY",
                "metrics": counts, "error": None, "reported_official_resolved": None}
    value = _json(data)
    schemas = {"suite": "evaluation-suite-result-v1", "acceptance": "swe-acceptance-v1",
               "official": "swe-official-run-v1", "candidate": "inspect-swe-candidate-v1"}
    if kind not in schemas or not isinstance(value, dict) or value.get("schema_version") != schemas[kind]:
        raise ValueError("unsupported evidence schema/kind")
    return {"status": value.get("status", "PROCESSED" if value.get("processing_ok") else "BLOCKED"),
            "metrics": value.get("jobs", {k: value[k] for k in
                ("processing_ok", "candidate_validated", "manual_recovery_required") if k in value}),
            "error": value.get("error"),
            "reported_official_resolved": value.get("official_resolved")}


def render(manifest_path, output):
    path, output = Path(manifest_path).absolute(), Path(output).absolute()
    raw = _read(path)
    plan = _json(raw)
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "entries"}
            or plan["schema_version"] != "evaluation-report-input-v1"
            or not isinstance(plan["entries"], list) or not 1 <= len(plan["entries"]) <= 64):
        raise ValueError("explicit evidence manifest required")
    rows, inputs, names = [], [path.parent], set()
    for entry in plan["entries"]:
        if (not isinstance(entry, dict) or set(entry) != {"id", "kind", "scope", "path", "sha256"}
                or entry["scope"] not in SCOPES):
            raise ValueError("invalid evidence entry")
        ident = _identifier(entry["id"])
        if ident.casefold() in names:
            raise ValueError("duplicate evidence identity")
        names.add(ident.casefold())
        source = Path(entry["path"])
        if not source.is_absolute():
            raise ValueError("evidence path must be absolute")
        data = _read(source)
        if _hash(data) != _sha(entry["sha256"]):
            raise ValueError("evidence digest mismatch")
        summary = summarize(data, entry["kind"])
        inputs.append(source.parent)
        rows.append({**entry, **summary})
    # A scope is an explicit controller declaration, never inferred from PASS
    # or a synthetic official_resolved field. Hashes do not authenticate authors.
    result = {"schema_version": "evaluation-summary-v1", "manifest_sha256": _hash(raw),
              "rows": rows, "new_provider_requests": 0, "new_official_evaluations": 0,
              "scope_semantics": "controller-declared; bytes hash-checked; provenance not authenticated",
              "overall_success_rate": None}
    _fresh(output, inputs)
    output.mkdir(parents=True)
    _dump(output / "report.json", result)
    (output / "input.json").write_bytes(raw)
    def cell(value):
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    lines = ["# Evaluation evidence report", "",
             "Existing evidence only. No tests, model requests, containers or official evaluations were executed.",
             "Scopes are controller declarations; hashes check bytes, not the truth of a claimed execution context.",
             "Metrics retain separate denominators. Historical and replay evidence do not add new subjects.", "",
             "| Evidence | Declared scope | Reported status | Metrics | Failure / recovery detail |",
             "| --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append("| " + " | ".join(cell(row[k]) for k in
                     ("id", "scope", "status", "metrics", "error")) + " |")
    lines += ["", "## Execution evidence coverage", ""]
    for scope in ("offline_simulated", "real_container", "official", "historical"):
        matches = [r["id"] for r in rows if r["scope"] == scope]
        lines.append(f"- {scope}: " + (", ".join(matches) if matches else "no evidence supplied; unverified"))
    lines += ["", "## Source paths and SHA-256", ""]
    for row in rows:
        lines.append(f"- {cell(row['id'])}: `{cell(row['path'])}`; `{row['sha256']}`")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _dump(output / "seal.json", [{"path": p.name, "sha256": _hash(_read(p))}
                                  for p in sorted(output.iterdir()) if p.is_file()])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("output")
    args = parser.parse_args()
    try:
        result = render(args.manifest, args.output)
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
        parser.exit(2, f"report input error: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
