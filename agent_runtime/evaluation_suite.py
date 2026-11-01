"""Sequential, resumable orchestration of the three existing offline evaluators.

Completed evidence is verified and reused, never re-executed. A partial child
directory is deliberately a blocker: choosing a fresh output is explicit.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform

from .eval_runner import _unsafe_reparse_chain
from .evaluation import _dump, _hash, _identifier, _json, _read, _relative
from .evaluation_bundle import _inventory, _sealed_path, verify_bundle

SCHEMA = "evaluation-suite-v1"
ENGINES = {"evaluation", "inspect_runtime", "inspect_archive", "inspect_model"}
REPORTS = {"evaluation": "report.json", "inspect_runtime": "report.json",
           "inspect_archive": "adapter-result.json", "inspect_model": "report.json"}
RESULT_SCHEMAS = {"evaluation": "evaluation-report-v1",
                  "inspect_runtime": "inspect-runtime-result-v1",
                  "inspect_archive": "inspect-offline-result-v1", "inspect_model": "inspect-model-result-v1"}


def identity() -> dict:
    """Bind reuse to source and installed distributions, without reading secrets."""
    root = Path(__file__).parent
    sources = {p.relative_to(root).as_posix(): _hash(_read(p))
               for p in sorted(root.rglob("*.py")) if "tests" not in p.parts}
    packages = sorted((d.metadata["Name"], d.version) for d in importlib.metadata.distributions()
                      if d.metadata["Name"])
    return {"python": platform.python_version(), "platform": platform.system(),
            "source_sha256": _hash(json.dumps(sources, sort_keys=True).encode()),
            "packages_sha256": _hash(json.dumps(packages).encode())}


def sealed_report(root: Path, digest: str, engine: str) -> dict:
    """Verify every sealed byte before trusting a completed execution receipt."""
    raw = _read(root / "seal.json")
    if _hash(raw) != digest:
        raise ValueError("seal digest mismatch")
    entries = _json(raw)
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2048:
        raise ValueError("invalid seal inventory")
    names, portable, size = set(), set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("invalid seal entry")
        name = _sealed_path(entry["path"])
        if name == "seal.json" or name.casefold() in portable:
            raise ValueError("duplicate seal path")
        names.add(name)
        portable.add(name.casefold())
        data = _read(root / name)
        size += len(data)
        if size > 128 * 1024 * 1024 or _hash(data) != entry["sha256"]:
            raise ValueError("sealed evidence mismatch or size exceeded")
    if _inventory(root) != names | {"seal.json"} or REPORTS[engine] not in names:
        raise ValueError("sealed inventory mismatch")
    report = _json(_read(root / REPORTS[engine]))
    if report.get("schema_version") != RESULT_SCHEMAS[engine]:
        raise ValueError("unexpected child report schema")
    if type(report.get("processing_ok")) is not bool:
        raise ValueError("invalid child processing status")
    for field in ("new_provider_requests", "new_official_evaluations"):
        if type(report.get(field)) is not int or report[field] != 0:
            raise ValueError("offline suite received live execution evidence")
    if engine == "evaluation":
        verify_bundle(root, digest)
    return report


def prepare(path: Path) -> tuple[bytes, dict, dict]:
    raw = _read(path)
    plan = _json(raw)
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "run_id", "jobs"} or plan["schema_version"] != SCHEMA:
        raise ValueError("invalid suite schema")
    _identifier(plan["run_id"])
    jobs = plan["jobs"]
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 32:
        raise ValueError("suite requires 1..32 jobs")
    seen, inputs = {}, {}
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("job must be an object")
        engine = job.get("engine")
        if engine not in ENGINES:
            raise ValueError("unsupported engine")
        fields = ({"id", "engine", "bundle", "seal_sha256"} if "bundle" in job else
                  {"id", "engine", "source_job"} if engine == "inspect_archive" else
                  {"id", "engine", "plan", "sha256"})
        if set(job) != fields:
            raise ValueError("unexpected job fields")
        ident = _identifier(job["id"])
        if ident.casefold() in {x.casefold() for x in seen}:
            raise ValueError("duplicate job")
        if "bundle" in job:
            if not isinstance(job["bundle"], str) or not Path(job["bundle"]).is_absolute():
                raise ValueError("imported bundle requires an explicit absolute path")
            sealed_report(Path(job["bundle"]), job["seal_sha256"], engine)
        elif engine == "inspect_archive":
            if seen.get(job["source_job"]) != "evaluation":
                raise ValueError("archive source must be an earlier evaluation job")
        else:
            data = _read(path.parent / _relative(job["plan"]))
            if _hash(data) != job["sha256"]:
                raise ValueError("child plan digest mismatch")
            child = _json(data)
            expected = {"evaluation": "evaluation-plan-v1", "inspect_runtime": "inspect-runtime-plan-v1",
                        "inspect_model": "inspect-model-eval-v1"}[engine]
            if not isinstance(child, dict) or child.get("schema_version") != expected:
                raise ValueError("child plan schema mismatch")
            # This suite executes only self-contained fixtures. Historical archives
            # retain the existing evaluator CLI and externally pinned bundle adapter.
            if engine == "evaluation":
                from .evaluation import _validate_script
                cases = child.get("cases")
                if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
                    raise ValueError("invalid evaluation cases")
                ids = set()
                for case in cases:
                    if not isinstance(case, dict) or case.get("artifacts", {}) != {}:
                        raise ValueError("scripted suite cases cannot have archive artifacts")
                    cid = _identifier(case.get("id"))
                    if cid.casefold() in ids or case.get("mode") != "scripted_runtime":
                        raise ValueError("unique scripted cases required")
                    ids.add(cid.casefold())
                    _validate_script(case)
            elif engine == "inspect_model":
                from .inspect_model_eval import validate
                validate(child)
                if child["mode"] != "mock":
                    raise ValueError("offline suite executes only mock model plans; live CLI is separate")
            else:
                from .inspect_runtime_solver import validate_plan
                validate_plan(child)
            _identifier(child.get("run_id"))
            inputs[ident] = data
        seen[ident] = engine
    return raw, plan, inputs


def acceptance(report: dict, engine: str) -> bool | None:
    if not report["processing_ok"]:
        return False
    if engine == "inspect_archive":
        # Fidelity is separately checked; a replay is not another task outcome.
        return report.get("source_processing_ok") is True
    if engine == "inspect_model":
        metrics = report["fixture_outcomes"]
        if metrics["failed"]:
            return False
        return None if metrics["unknown"] or not metrics["planned"] else True
    if engine == "inspect_runtime":
        metrics = report.get("runtime_cases", {})
        total, passed = metrics.get("total"), metrics.get("passed")
        return type(total) is int and total > 0 and type(passed) is int and passed == total
    metrics = report["aggregates"]
    groups = [m for m in metrics.values() if m["total"]]
    if any(m["failed"] for m in groups):
        return False
    if not groups or any(m["unknown"] for m in groups):
        return None
    return True


def execute(engine: str, plan: Path | None, output: Path, source: Path | None, digest: str | None) -> dict:
    if engine == "evaluation":
        from .evaluation import run
        return run(plan, output)
    if engine == "inspect_model":
        from .inspect_model_eval import run
        return run(plan, output, execute=True)
    if engine == "inspect_runtime":
        from .inspect_runtime_solver import run
        return run(plan, output)
    from .inspect_adapter import run
    return run(source, output, digest)


def run(plan_path: str | Path, output: str | Path) -> dict:
    path, root = Path(os.path.abspath(plan_path)), Path(os.path.abspath(output))
    raw, plan, inputs = prepare(path)
    imported = [Path(job["bundle"]) for job in plan["jobs"] if "bundle" in job]
    if (_unsafe_reparse_chain(root) or root.is_relative_to(path.parent)
            or any(root.is_relative_to(p) or p.is_relative_to(root) for p in imported)):
        raise ValueError("output must be outside source plans without reparse parents")
    frozen = {"suite_sha256": _hash(raw), "identity": identity()}
    if root.exists():
        if _json(_read(root / "frozen.json")) != frozen or _read(root / "suite.json") != raw:
            raise ValueError("suite or implementation/environment changed; choose a new output")
    else:
        root.mkdir(parents=True)
        _dump(root / "frozen.json", frozen)
        (root / "suite.json").write_bytes(raw)
        (root / "inputs").mkdir()
        (root / "jobs").mkdir()
        (root / "receipts").mkdir()
        for ident, data in inputs.items():
            (root / "inputs" / (ident + ".json")).write_bytes(data)
    for ident, data in inputs.items():
        if _read(root / "inputs" / (ident + ".json")) != data:
            raise ValueError("frozen child plan mismatch")
    if any(_unsafe_reparse_chain(root / name) for name in
           ("inputs", "jobs", "receipts", "attempts", "running.lock")):
        raise ValueError("suite state contains reparse paths")
    # Exclusive ownership prevents duplicate execution from concurrent invocations.
    lock = root / "running.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    rows, reports = [], {}
    try:
        for job in plan["jobs"]:
            ident, engine = job["id"], job["engine"]
            child_root, receipt_path = root / "jobs" / ident, root / "receipts" / (ident + ".json")
            try:
                if "bundle" in job:
                    report = sealed_report(Path(job["bundle"]), job["seal_sha256"], engine)
                    disposition = "imported"
                elif receipt_path.exists():
                    receipt = _json(_read(receipt_path))
                    if receipt.get("job") != job or receipt.get("suite_sha256") != frozen["suite_sha256"]:
                        raise ValueError("receipt identity mismatch")
                    report = sealed_report(child_root, receipt["seal_sha256"], engine)
                    disposition = "reused"
                else:
                    if child_root.exists():
                        raise ValueError("partial child output preserved; automatic rerun forbidden")
                    source_id = job.get("source_job")
                    if source_id and source_id not in reports:
                        raise ValueError("source job unavailable")
                    source_job = next((j for j in plan["jobs"] if j["id"] == source_id), {})
                    source = (Path(source_job["bundle"]) if "bundle" in source_job else root / "jobs" / source_id) if source_id else None
                    digest = _hash(_read(source / "seal.json")) if source else None
                    execute(engine, root / "inputs" / (ident + ".json") if ident in inputs else None,
                            child_root, source, digest)
                    digest = _hash(_read(child_root / "seal.json"))
                    report = sealed_report(child_root, digest, engine)
                    _dump(receipt_path, {"job": job, "suite_sha256": frozen["suite_sha256"], "seal_sha256": digest})
                    disposition = "executed"
                reports[ident] = report
                rows.append({"id": ident, "engine": engine, "disposition": disposition,
                             "processing_ok": report["processing_ok"],
                             "accepted": acceptance(report, engine),
                             "metrics": report.get("aggregates", report.get("runtime_cases",
                                         report.get("source_aggregates", report.get("fixture_outcomes"))))})
            except Exception as exc:
                rows.append({"id": ident, "engine": engine, "processing_ok": False, "accepted": False,
                             "disposition": "blocked", "error": f"{type(exc).__name__}: {exc}"})
        result = {"schema_version": "evaluation-suite-result-v1", "run_id": plan["run_id"],
                  "processing_ok": all(r["processing_ok"] for r in rows), "jobs": rows,
                  "accepted": False if any(r["accepted"] is False for r in rows) else
                              None if any(r["accepted"] is None for r in rows) else True,
                  "new_provider_requests": 0, "new_official_evaluations": 0,
                  "note": "Job metrics remain separate: Inspect archive projections do not add subjects."}
        attempts = root / "attempts"
        attempts.mkdir(exist_ok=True)
        from uuid import uuid4
        attempt_id = uuid4().hex
        _dump(attempts / (attempt_id + ".json"), result)
        lines = [f"# Evaluation suite: {plan['run_id']}", "",
                 f"Evidence processing: {result['processing_ok']}; all recorded outcomes accepted: {result['accepted']}",
                 "", "| Job | Engine | Execution | Processing | Outcome | Metrics |",
                 "| --- | --- | --- | --- | --- | --- |"]
        for row in rows:
            lines.append("| " + " | ".join(str(row.get(key, "unknown")).replace("|", "\\|") for key in
                         ("id", "engine", "disposition", "processing_ok", "accepted", "metrics")) + " |")
        lines += ["", result["note"], "Historical imports preserve their original source scope.",
                  "Processing success is not Agent success. No new provider or official evaluations."]
        (attempts / (attempt_id + ".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return result
    finally:
        lock.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        result = run(args.plan, args.output)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"suite input error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["processing_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
