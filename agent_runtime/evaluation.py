"""Offline, append-only evaluation bundles; never invokes a provider or harness.

The evaluator trusts the plan author and pinned archive provenance. Hashes detect
byte changes, not a malicious author fabricating an internally consistent archive.
Run with ``python -m agent_runtime.evaluation PLAN.json NEW_OUTPUT_DIRECTORY``.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .eval_runner import _response_from_entry, _unsafe_reparse_chain
from .fake_model import ScriptedModel
from .runtime import InjectedCrash, Runtime
from .trace import TraceReporter

SCHEMA = "evaluation-plan-v1"
MAX_BYTES = 16 * 1024 * 1024
SAFE_TOOLS = {"read_file", "write_file", "edit_file"}
KINDS = {"scripted_runtime": "runtime", "agent_archive": "agent", "official_archive": "environment"}


def _json(data: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _dump(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _relative(value: str) -> str:
    # One portable spelling, also excludes Windows devices/ADS and internal state.
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("expected portable relative path")
    parts = value.split("/")
    devices = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
               *(f"LPT{i}" for i in range(1, 10))}
    if any(not p or p.casefold() in {".", "..", ".git", ".agent_runtime"}
           or re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith((" ", "."))
           or p.split(".")[0].upper() in devices for p in parts):
        raise ValueError("unsafe relative path")
    return value


def _read(path: Path) -> bytes:
    if _unsafe_reparse_chain(path) or not path.is_file():
        raise ValueError("input must be a regular file without reparse parents")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("input exceeds 16 MiB limit")
    return data


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise ValueError("invalid identifier")
    _relative(value)
    return value


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _validate_script(case: dict) -> None:
    if len(json.dumps(case).encode("utf-8")) > 64 * 1024:
        raise ValueError("script exceeds 64 KiB limit")
    if any(not isinstance(case.get(key, []), list) for key in ("model", "recovery_model")):
        raise ValueError("model/recovery_model must be response lists")
    checks = case.get("checks")
    if not isinstance(checks, dict) or not isinstance(checks.get("status"), str):
        raise ValueError("explicit runtime status oracle required")
    entries = case.get("model", []) + case.get("recovery_model", [])
    if not 1 <= len(entries) <= 32:
        raise ValueError("script requires 1..32 responses including recovery")
    for mapping in (case.get("files", {}), case.get("checks", {}).get("files", {})):
        if not isinstance(mapping, dict):
            raise ValueError("files must be an object")
        for path, content in mapping.items():
            _relative(path)
            if not isinstance(content, str):
                raise ValueError("fixture/oracle content must be text")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("usage", {}), dict):
            raise ValueError("response and usage must be objects")
        if not isinstance(entry.get("text", ""), str):
            raise ValueError("response text must be a string")
        if any(not _count(v) for v in entry.get("usage", {}).values()):
            raise ValueError("scripted usage must be nonnegative integers")
        if "tool" in entry:
            if entry["tool"] not in SAFE_TOOLS:
                raise ValueError("only bounded local read/write/edit tools allowed")
            _identifier(entry["id"])
            if not isinstance(entry.get("input"), dict):
                raise ValueError("tool input must be an object")
            _relative(entry["input"]["path"])
            for key in {"write_file": ("content",), "edit_file": ("old_text", "new_text"), "read_file": ()}[entry["tool"]]:
                if not isinstance(entry["input"].get(key), str):
                    raise ValueError("tool text arguments must be strings")
        _response_from_entry(entry)


def _base(case: dict) -> dict:
    return {"id": case["id"], "mode": case["mode"], "kind": KINDS[case["mode"]],
            "source_run_id": case.get("source_run_id"), "instance_id": case.get("instance_id"),
            "historical": case["mode"] != "scripted_runtime", "processing": "ok",
            "runtime_status": None, "runtime_case_pass": None, "task_success": None,
            "official_resolved": None, "official_status": "not_evaluated",
            "provider_input_tokens": None, "provider_output_tokens": None,
            "failure_category": "none", "limitations": []}


def _script(case: dict, root: Path) -> dict:
    workspace = root / "workspace"
    workspace.mkdir()
    for name, content in case.get("files", {}).items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    triggered = False

    def inject(point, **_):
        nonlocal triggered
        if point == case.get("fault_point"):
            triggered = True
            raise InjectedCrash(point)

    def runtime(entries, fault=None):
        return Runtime(workspace, ScriptedModel([_response_from_entry(e) for e in entries]),
                       tool_scope=SAFE_TOOLS, approval_callback=lambda *_: True,
                       fault_injector=fault)

    engine = runtime(case.get("model", []), inject if case.get("fault_point") else None)
    try:
        outcome = engine.run(case.get("prompt", case["id"]))
    except InjectedCrash:
        tasks = engine.store.list_tasks()
        if len(tasks) != 1:
            raise ValueError("crash did not preserve exactly one task")
        engine = runtime(case.get("recovery_model", []))
        outcome = engine.resume(tasks[0]["task_id"])
    if len(engine.store.list_tasks()) != 1:
        raise ValueError("evaluation created more than one task")
    reporter = TraceReporter(engine.store)
    summary = reporter.summary(outcome.task_id)
    _dump(root / "runtime-summary.json", summary)
    _dump(root / "model-calls.json", engine.store.list_model_calls(outcome.task_id))
    reporter.export_jsonl(outcome.task_id, root / "trace.jsonl")
    paths = set(case.get("files", {})) | {
        e["input"]["path"] for e in case.get("model", []) + case.get("recovery_model", []) if "tool" in e}
    diff = []
    for name in sorted(paths):
        before = case.get("files", {}).get(name, "")
        path = workspace / name
        after = path.read_text(encoding="utf-8") if path.is_file() else ""
        diff.extend(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile="a/" + name, tofile="b/" + name))
    # Inspection artifact, not a prediction or evidence of semantic correctness.
    (root / "workspace-diff.txt").write_text("".join(diff), encoding="utf-8")
    checks = case["checks"]
    passed = outcome.status == checks["status"] and summary["invariant_violation_count"] == 0
    for name, content in checks.get("files", {}).items():
        path = workspace / name
        passed = passed and path.is_file() and path.read_text(encoding="utf-8") == content
    if case.get("fault_point") and not triggered:
        passed = False
    result = _base(case)
    result.update(runtime_status=outcome.status, runtime_case_pass=passed,
                  model_calls_recorded=summary["model_calls"],
                  tool_calls_recorded=summary["tool_calls"],
                  tool_execution_attempts=summary["tool_execution_attempts"],
                  invariant_violation_count=summary["invariant_violation_count"],
                  fault_triggered=triggered, failure_category="none" if passed else "runtime_oracle",
                  limitations=["scripted responses; no provider or Agent competence evidence"])
    return result


def _prediction(data: bytes, instance: str) -> dict:
    rows = [_json(line) for line in data.splitlines() if line.strip()]
    if len(rows) != 1 or rows[0].get("instance_id") != instance:
        raise ValueError("exactly one matching prediction required")
    row = rows[0]
    if not isinstance(row.get("model_patch"), str) or not isinstance(row.get("model_name_or_path"), str):
        raise ValueError("invalid official prediction schema")
    return row


def _agent(case: dict, inputs: dict[str, bytes]) -> dict:
    summary = _json(inputs["summary"])
    calls = _json(inputs["model_calls"])
    prediction = _prediction(inputs["prediction"], case["instance_id"])
    if (not isinstance(calls, list) or summary.get("task_id") != case["task_id"]
            or summary.get("model_calls") != len(calls)
            or any(c.get("task_id") != case["task_id"] for c in calls)
            or len({c["model_call_id"] for c in calls}) != len(calls)):
        raise ValueError("model-call/summary task identity or count mismatch")
    result = _base(case)
    result.update(runtime_status=summary.get("status"), model_calls_recorded=len(calls),
                  tool_calls_recorded=summary.get("tool_calls"),
                  tool_execution_attempts=summary.get("tool_execution_attempts"),
                  invariant_violation_count=summary.get("invariant_violation_count"),
                  patch_bytes=len(prediction["model_patch"].encode("utf-8")),
                  patch_sha256=_hash(prediction["model_patch"].encode("utf-8")))
    # Explicit response usage, not persistence columns whose defaults are zero.
    usages = [(c.get("response") or {}).get("usage") or {} for c in calls]
    complete = bool(calls) and all(_count(u.get(k)) for u in usages for k in ("input_tokens", "output_tokens"))
    if case.get("model_mode") == "live" and complete:
        result["provider_input_tokens"] = sum(u["input_tokens"] for u in usages)
        result["provider_output_tokens"] = sum(u["output_tokens"] for u in usages)
    result["usage_complete"] = complete if case.get("model_mode") == "live" else False
    result["limitations"].append("historical import; no new model call, invariant scan, or official evaluation")
    if not prediction["model_patch"].strip():
        result.update(task_success=False, failure_category="task_semantic")
        result["limitations"].append("empty repair patch; runtime status is not task success")
    else:
        result["limitations"].append("patch exists; no independent correctness oracle")
    return result


def _official(case: dict, inputs: dict[str, bytes]) -> dict:
    receipt = _json(inputs["receipt"])
    # A receipt is evaluator-owned provenance, not an assertion made by the solver.
    for key in ("source_run_id", "instance_id", "scope", "dataset_revision", "base_commit", "harness_commit"):
        if receipt.get(key) != case[key]:
            raise ValueError(f"official receipt {key} mismatch")
    for role in ("report", "prediction", "execution"):
        if receipt.get(role + "_sha256") != _hash(inputs[role]):
            raise ValueError(f"official receipt {role} digest mismatch")
    execution = _json(inputs["execution"])
    if execution.get("run_id") != case["source_run_id"]:
        raise ValueError("official execution run identity mismatch (reused report is not a new evaluation)")
    if execution.get("not_a_model_score") is True and case["scope"] != "gold_control":
        raise ValueError("environment control cannot be relabeled as an Agent score")
    exit_code = execution.get("exit_code", execution.get("process_exit"))
    if type(exit_code) is not int:
        raise ValueError("official execution exit code missing")
    prediction = _prediction(inputs["prediction"], case["instance_id"])
    report = _json(inputs["report"])
    result = _base(case)
    result["kind"] = "environment" if case["scope"] == "gold_control" else "agent"
    result["limitations"].append("historical official evidence; harness was not rerun")
    result["patch_bytes"] = len(prediction["model_patch"].encode("utf-8"))
    if exit_code != 0:
        result.update(official_status="execution_error", failure_category="environment_setup")
        return result
    detail = report.get(case["instance_id"])
    if detail is None:
        # Aggregate reports can establish empty-patch handling, not test success.
        if (case["instance_id"] in report.get("empty_patch_ids", [])
                and not prediction["model_patch"].strip()):
            result.update(official_status="empty_patch", failure_category="task_semantic")
            if result["kind"] == "agent":
                result["task_success"] = False
            return result
        raise ValueError("per-instance official result missing")
    if not isinstance(detail.get("resolved"), bool):
        raise ValueError("official resolved must be boolean")
    tests = detail.get("tests_status")
    if not isinstance(tests, dict) or not {"FAIL_TO_PASS", "PASS_TO_PASS"} <= set(tests):
        raise ValueError("official test-status groups missing")
    for group in tests.values():
        if (not isinstance(group, dict) or any(not isinstance(group.get(k), list)
                or any(not isinstance(item, str) for item in group[k]) for k in ("success", "failure"))):
            raise ValueError("invalid official test-status lists")
    resolved = detail["resolved"]
    if resolved and any(tests[k]["failure"] for k in ("FAIL_TO_PASS", "PASS_TO_PASS")):
        raise ValueError("official success contradicts required test failures")
    if resolved and (not prediction["model_patch"].strip()
                     or any(detail.get(k) is not True for k in ("patch_exists", "patch_successfully_applied"))
                     or detail.get("patch_is_None") is not False
                     or not isinstance(detail.get("tests_status"), dict)):
        raise ValueError("inconsistent official success evidence")
    result.update(official_resolved=resolved, official_status="resolved" if resolved else "unresolved",
                  failure_category="none" if resolved else "task_semantic")
    if result["kind"] == "agent":
        result["task_success"] = resolved
    return result


def _aggregate(rows: list[dict]) -> dict:
    result = {}
    for kind, field in (("runtime", "runtime_case_pass"), ("agent", "task_success"),
                        ("environment", "official_resolved")):
        subset = [r for r in rows if r["kind"] == kind]
        yes = sum(r[field] is True for r in subset)
        no = sum(r[field] is False for r in subset)
        unknown = len(subset) - yes - no
        result[kind] = {"total": len(subset), "passed": yes, "failed": no, "unknown": unknown,
                        "success_rate": yes / len(subset) if subset and not unknown else None}
    return result


def run(plan_path: str | Path, output: str | Path) -> dict:
    """Validate and freeze all inputs before any local scripted tool execution.

    Processing errors stay in the planned denominator. Exit status belongs to
    evidence processing, not to a claim that the experiments passed.
    """
    plan_path = Path(os.path.abspath(plan_path))
    plan_bytes = _read(plan_path)
    plan = _json(plan_bytes)
    if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA:
        raise ValueError("unsupported evaluation plan schema")
    _identifier(plan["run_id"])
    cases = plan["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
        raise ValueError("plan requires 1..64 cases")
    ids, subjects, prepared = set(), set(), []
    total_bytes = 0
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each case must be an object")
        ident = _identifier(case["id"])
        if ident.casefold() in ids or case["mode"] not in KINDS:
            raise ValueError("duplicate case or unsupported mode")
        ids.add(ident.casefold())
        if case["mode"] == "scripted_runtime":
            _validate_script(case)
        else:
            for field in ("source_run_id", "instance_id"):
                _identifier(case[field])
            subject = (case["source_run_id"], case["instance_id"])
            if subject in subjects:
                raise ValueError("duplicate archived subject would inflate denominator")
            subjects.add(subject)
            if case["mode"] == "agent_archive" and case.get("model_mode") not in {"live", "scripted"}:
                raise ValueError("explicit model_mode required")
            if case["mode"] == "official_archive":
                if case.get("scope") not in {"gold_control", "agent"}:
                    raise ValueError("official scope must be explicit")
                for key in ("dataset_revision", "base_commit", "harness_commit"):
                    if not re.fullmatch(r"[0-9a-f]{40}", case.get(key, "")):
                        raise ValueError("official provenance must pin immutable commits")
        roles = {"summary", "model_calls", "prediction"} if case["mode"] == "agent_archive" else {
            "report", "prediction", "execution", "receipt"} if case["mode"] == "official_archive" else set()
        if not isinstance(case.get("artifacts", {}), dict) or set(case.get("artifacts", {})) != roles:
            raise ValueError("exact artifact role set required")
        inputs = {}
        for role, reference in case.get("artifacts", {}).items():
            if not isinstance(reference, dict):
                raise ValueError("artifact reference must be an object")
            data = _read(plan_path.parent / _relative(reference["path"]))
            if reference.get("sha256") != _hash(data):
                raise ValueError("artifact digest mismatch")
            total_bytes += len(data)
            if total_bytes > 64 * 1024 * 1024:
                raise ValueError("combined archive inputs exceed 64 MiB")
            inputs[role] = data
        prepared.append((case, inputs))
    root = Path(os.path.abspath(output))
    if _unsafe_reparse_chain(root) or root.exists():
        raise ValueError("output must be a fresh directory without reparse parents")
    root.mkdir(parents=True, exist_ok=False)
    (root / "plan.json").write_bytes(plan_bytes)
    rows, manifest = [], []
    for case, inputs in prepared:
        case_root = root / case["id"]
        case_root.mkdir()
        for role, data in inputs.items():
            path = case_root / (role + ".input")
            path.write_bytes(data)
            manifest.append({"path": path.relative_to(root).as_posix(), "sha256": _hash(data),
                             "source": case["artifacts"][role]["path"]})
        try:
            if case["mode"] == "scripted_runtime":
                row = _script(case, case_root)
            elif case["mode"] == "agent_archive":
                row = _agent(case, inputs)
            else:
                row = _official(case, inputs)
        except Exception as exc:  # noqa: BLE001 - failures remain visible in planned denominator
            row = _base(case)
            if case["mode"] == "official_archive" and case["scope"] == "agent":
                row["kind"] = "agent"
            row.update(processing="error", failure_category="evidence_invalid",
                       error=f"{type(exc).__name__}: {exc}")
        _dump(case_root / "result.json", row)
        rows.append(row)
    report = {"schema_version": "evaluation-report-v1", "run_id": plan["run_id"],
              "plan_sha256": _hash(plan_bytes), "processing_ok": all(r["processing"] == "ok" for r in rows),
              "new_provider_requests": 0, "new_official_evaluations": 0,
              "results": rows, "aggregates": _aggregate(rows)}
    _dump(root / "inputs.json", manifest)
    _dump(root / "report.json", report)
    lines = [f"# Evaluation {plan['run_id']}", "", "No provider calls or official harness executions.", "",
             "| Case | Kind | Historical | Processing | Runtime oracle | Agent success | Official resolved |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    def cell(value):
        return "unknown" if value is None else str(value).lower()
    for row in rows:
        lines.append("| " + " | ".join(cell(row[k]) for k in (
            "id", "kind", "historical", "processing", "runtime_case_pass", "task_success", "official_resolved")) + " |")
    lines += ["", "Unknown values remain null in JSON. Historical evidence is not a new experiment.",
              "Gold controls do not enter the Agent denominator. Synthetic usage is not provider usage."]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Seal all outputs, including raw traces/DB files, after the evaluator closes them.
    seal = [{"path": p.relative_to(root).as_posix(), "sha256": _hash(_read(p))}
            for p in sorted(root.rglob("*")) if p.is_file()]
    _dump(root / "seal.json", seal)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        report = run(args.plan, args.output)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"evaluation input error: {exc}\n")
    print(json.dumps({"run_id": report["run_id"], "processing_ok": report["processing_ok"],
                      "aggregates": report["aggregates"]}, ensure_ascii=False))
    return 0 if report["processing_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
