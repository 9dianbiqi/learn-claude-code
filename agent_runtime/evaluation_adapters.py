"""Read-only V1 -> V2 adapters. No runner, model, container or grader invocation.

Byte integrity is checked before projection. Hashes do not authenticate authors;
execution scope remains a controller declaration constrained by source semantics.
Summary-only schemas stay summary-only. No synthetic task/attempt identities.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .evaluation import _hash, _json, _read, _official
from .evaluation_bundle import _inventory, _sealed_path, verify_bundle
from .evaluation_contract import (
    Attempt, ComparisonIdentity, EvaluationCase, EvaluationRun, Evidence,
    ExecutionScope, Metric, Oracle, SourceExecutionIdentity, digest, sha256,
)

SUPPORTED = {"evaluation-report-v1", "evaluation-suite-result-v1", "inspect-runtime-result-v1",
             "inspect-model-result-v1", "inspect-offline-result-v1", "inspect-swe-candidate-v1",
             "swe-official-run-v1", "junit"}


@dataclass
class Source:
    entry: dict
    report: dict
    evidence: Evidence
    files: dict[str, bytes]

    def json_file(self, name: str) -> dict | None:
        return _json(self.files[name]) if name in self.files else None


def _junit(raw: bytes) -> dict:
    root = ET.fromstring(raw)
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("JUnit testsuite required")
    cases = []
    for index, row in enumerate(root.iter("testcase")):
        reason = row.find("error")
        if reason is not None:
            verdict = "blocked"
        else:
            reason = row.find("failure")
            verdict = "fail" if reason is not None else "pass"
            if reason is None:
                reason = row.find("skipped")
                if reason is not None:
                    verdict = "unknown"
        cases.append({"id": row.get("name") or f"unnamed-test-index-{index}",
                      "classname": row.get("classname"), "verdict": verdict,
                      "reason": (reason.get("message") or reason.text or reason.tag) if reason is not None
                                else "JUnit test completed without failure/error/skip",
                      "attributes": dict(row.attrib)})
    return {"schema_version": "junit", "cases": cases, "raw_xml": raw.decode("utf-8-sig")}


def load_source(entry: dict) -> Source:
    """An external report digest is required, plus an external seal pin for bundles."""
    allowed = {"id", "path", "sha256", "bundle", "seal_sha256", "scope", "comparison",
               "official_artifacts", "projection_of", "format"}
    if (not isinstance(entry, dict) or not {"id", "path", "sha256"} <= entry.keys()
            or set(entry) - allowed or not isinstance(entry["id"], str) or not entry["id"]):
        raise ValueError("invalid V2 evidence entry")
    path = Path(entry["path"])
    if not path.is_absolute():
        raise ValueError("absolute evidence path required")
    raw = _read(path)
    if _hash(raw) != sha256(entry["sha256"]):
        raise ValueError("evidence digest mismatch")
    files = {}
    if ("bundle" in entry) != ("seal_sha256" in entry):
        raise ValueError("bundle requires an external seal pin")
    if "bundle" in entry:
        root = Path(entry["bundle"])
        if not root.is_absolute() or not path.is_relative_to(root):
            raise ValueError("report must be inside absolute bundle")
        seal = _read(root / "seal.json")
        if _hash(seal) != sha256(entry["seal_sha256"]):
            raise ValueError("external seal digest mismatch")
        entries = _json(seal)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 2048:
            raise ValueError("invalid seal inventory")
        names, total = set(), 0
        for row in entries:
            if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
                raise ValueError("invalid seal member")
            name = _sealed_path(row["path"])
            if name == "seal.json" or name.casefold() in names:
                raise ValueError("duplicate seal member")
            names.add(name.casefold())
            data = _read(root / name)
            total += len(data)
            if total > 128 * 1024 * 1024 or _hash(data) != sha256(row["sha256"]):
                raise ValueError("sealed evidence changed or too large")
            files[name] = data
        if _inventory(root) != set(files) | {"seal.json"}:
            raise ValueError("seal inventory mismatch")
        if files.get(path.relative_to(root).as_posix()) != raw:
            raise ValueError("report absent from seal")
    report = _junit(raw) if entry.get("format") == "junit" else _json(raw)
    if not isinstance(report, dict) or report.get("schema_version") not in SUPPORTED:
        raise ValueError("unsupported source schema")
    if report["schema_version"] == "evaluation-report-v1" and files:
        verify_bundle(entry["bundle"], entry["seal_sha256"])
    ExecutionScope(**entry.get("scope", {}))
    if not isinstance(entry.get("comparison", {}), dict):
        raise ValueError("comparison must be an explicit frozen configuration")
    # Force JSON validation without coercing, dropping or rewriting source fields.
    digest(report)
    return Source(entry, report, Evidence("evidence-" + _hash(raw), str(path), _hash(raw),
        report["schema_version"], report.get("source_run_id", report.get("run_id")),
        evidence_mode="projection" if report["schema_version"] == "inspect-offline-result-v1" else "imported",
        seal_sha256=entry.get("seal_sha256")), files)


def _scope(source: Source, *, allow_official=False, **known) -> ExecutionScope:
    declared = source.entry.get("scope", {})
    if declared.get("grading") == "official_harness" and known.get("grading") != "official_harness" and not allow_official:
        raise ValueError("official scope requires validated official evidence")
    for key, value in known.items():
        if value != "unknown" and declared.get(key, value) not in {value, "unknown"}:
            raise ValueError("scope contradicts source semantics: " + key)
    return ExecutionScope(**{**declared, **{k: v for k, v in known.items() if v != "unknown"}})


def _boolean(value: Any, name: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ValueError(name + " must be boolean or null")
    return value


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(name + " must be a nonnegative integer")
    return value


def _metric(name, value, unit, refs, *, complete=None, reason="not recorded by source"):
    valid = type(value) in {int, float} and value >= 0
    if value is not None and not valid:
        raise ValueError("invalid source metric: " + name)
    return Metric(name, value, unit, valid if complete is None else bool(complete and valid),
                  None if valid and complete is not False else reason, refs)


def _usage(row: dict, scope: ExecutionScope, refs) -> tuple[Metric, ...]:
    usage = row.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    provider = scope.model_mode == "live"
    values = {key: usage.get(key, row.get("provider_" + key)) for key in ("input_tokens", "output_tokens")}
    complete = (usage.get("complete", row.get("usage_complete")) is True
                and all(type(value) is int and value >= 0 for value in values.values()))
    result = []
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key, row.get("provider_" + key)) if provider else None
        # An incomplete usage total is not a full total, even if a subtotal exists.
        result.append(_metric("provider_" + key, value if complete else None, "tokens", refs,
                              reason="missing/incomplete provider usage" if provider else "not live provider usage"))
        if "known_" + key in usage:
            result.append(_metric(("provider_" if provider else "synthetic_") + "known_" + key,
                                  usage["known_" + key], "tokens", refs, complete=complete,
                                  reason="known subtotal; missing calls remain"))
    for name, unit in (("latency_seconds", "seconds"), ("cost", "currency_unspecified")):
        # No V1 adapter currently supplies a cost basis or per-call latency definition.
        result.append(_metric(name, None, unit, refs, reason="source does not define this measurement"))
    return tuple(result)


def _case(run, source, row, index, objective, oracle, scope, value, *, reason=None,
          execution_id=None, identity_case=None, metrics=(), verdict=None):
    value = _boolean(value, objective)
    verdict = verdict or ("pass" if value is True else "fail" if value is False else "unknown")
    refs = (source.evidence.evidence_id,)
    ident = row.get("id", row.get("instance_id"))
    if not isinstance(ident, str) or not ident:
        raise ValueError("source case identity required")
    identity = SourceExecutionIdentity(execution_id, identity_case or ident, objective, oracle.oracle_id) if execution_id else None
    attempts = ()
    if execution_id:
        # Objectives can share one execution. Oracle identity belongs on the case,
        # not on a newly fabricated execution attempt for each objective.
        attempt_id = "attempt-" + digest({"execution": execution_id, "case": identity_case or ident})
        attempts = (attempt_id,)
        if not any(a.attempt_id == attempt_id for a in run.attempts):
            run.attempts.append(Attempt(attempt_id, "case_execution", execution_id, refs))
    run.cases.append(EvaluationCase(digest({"report": source.evidence.sha256, "row": index, "objective": objective}),
        ident, objective, oracle, verdict,
        reason or row.get("error") or ("reported oracle satisfied" if value is True else
            "reported oracle not satisfied" if value is False else "outcome not recorded or unavailable"),
        scope, identity, ComparisonIdentity(_comparison(source)), attempts, refs,
        tuple(metrics), dict(row)))


def _oracle(source, name, kind, validated=False):
    return Oracle(name, kind, "source_validated" if validated else "reported", (source.evidence.evidence_id,))


def _comparison(source):
    frozen = dict(source.entry.get("comparison", {}))
    if source.report["schema_version"] == "inspect-model-result-v1" and source.report.get("model"):
        model = source.report["model"]
        if frozen.get("model", model) != model:
            raise ValueError("comparison model contradicts source model")
        frozen.setdefault("model", model)
    return frozen


def _evaluation(run: EvaluationRun, source: Source):
    report = source.report
    rows = report.get("results")
    if not isinstance(rows, list):
        raise ValueError("evaluation results required")
    plan = source.json_file("plan.json") or {}
    cases = {c["id"]: c for c in plan.get("cases", [])}
    if not plan:
        run.limitations.append("PARTIAL: evaluation plan/oracle configuration unavailable; row outcomes remain reported")
    for index, row in enumerate(rows):
        config = cases.get(row.get("id"), {})
        mode = row.get("mode")
        if mode == "scripted_runtime":
            objective, value = "runtime_regression", row.get("runtime_case_pass")
            scope = _scope(source, model_mode="scripted", environment="local", grading="runtime_oracle",
                           subject_role="runtime_regression")
            summary = source.json_file(row["id"] + "/runtime-summary.json") or {}
            execution = summary.get("task_id")
            oracle = _oracle(source, "runtime-checks:" + digest(config["checks"]) if "checks" in config else
                             "v1-reported-runtime-checks", "runtime_oracle", bool(config))
        elif mode == "agent_archive":
            objective, value = "task_success", row.get("task_success")
            # This schema has no independent positive task oracle.
            if value is True:
                raise ValueError("agent archive cannot establish unverified success")
            model_mode = config.get("model_mode", "unknown")
            scope = _scope(source, model_mode=model_mode,
                           subject_role="scripted_candidate" if model_mode == "scripted" else "agent_candidate")
            execution = config.get("task_id") or row.get("source_run_id")
            oracle = _oracle(source, "v1-agent-empty-patch-check", "artifact_check", bool(config))
        elif mode == "official_archive":
            objective, value = "official_resolution", None
            role = "gold_control" if config.get("scope") == "gold_control" else "unknown"
            scope = replace(_scope(source, allow_official=True, subject_role=role), grading="unknown")
            execution = row.get("source_run_id")
            oracle = Oracle("unavailable-official-receipt", "official_harness", "unavailable")
            if config and source.files:
                inputs = {}
                for name, spec in config.get("artifacts", {}).items():
                    frozen_name = row["id"] + "/" + name + ".input"
                    if frozen_name in source.files:
                        mappings = source.json_file("inputs.json") or []
                        matches = [m for m in mappings if m.get("path") == frozen_name]
                        if len(matches) != 1 or matches[0] != {
                                "path": frozen_name, "sha256": spec["sha256"], "source": spec["path"]}:
                            raise ValueError("archive frozen-input mapping mismatch")
                        content = source.files[frozen_name]
                    else:
                        content = source.files[_sealed_path(spec["path"])]
                    if _hash(content) != sha256(spec["sha256"]):
                        raise ValueError("official archive artifact pin mismatch")
                    inputs[name] = content
                # Pure evidence validation from V1, never its execution runner.
                if row.get("processing") == "ok":
                    validated = _official(config, inputs)
                    if validated.get("official_resolved") != row.get("official_resolved"):
                        raise ValueError("official archive outcome mismatch")
                    value = validated.get("official_resolved")
                    subject = "gold_control" if config["scope"] == "gold_control" else source.entry.get(
                        "scope", {}).get("subject_role", "agent_candidate")
                    if subject not in {"gold_control", "agent_candidate", "scripted_candidate"}:
                        raise ValueError("invalid official archive subject role")
                    scope = _scope(source, grading="official_harness", subject_role=subject)
                    oracle = _oracle(source, "official:" + config["harness_commit"] + ":" + config["dataset_revision"],
                                     "official_harness", True)
        else:
            raise ValueError("unsupported evaluation case mode")
        verdict = "blocked" if row.get("processing") == "error" else None
        _case(run, source, row, index, objective, oracle, scope, value,
              execution_id=execution, identity_case=row.get("instance_id") or row["id"],
              metrics=_usage(row, scope, (source.evidence.evidence_id,)), verdict=verdict)
    _unique_rows(rows)


def _unique_rows(rows):
    ids = [r.get("id") for r in rows]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("duplicate/missing source case IDs")


def _inspect_cases(run: EvaluationRun, source: Source):
    model = source.report["schema_version"] == "inspect-model-result-v1"
    report = source.report
    rows = report.get("results", [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("invalid Inspect results")
    _unique_rows(rows)
    plan = source.json_file("plan.json") or {}
    if plan:
        if report.get("plan_sha256") != _hash(source.files["plan.json"]) or report.get("run_id") != plan.get("run_id"):
            raise ValueError("Inspect plan/report identity mismatch")
    planned = plan.get("cases", [])
    if not isinstance(planned, list) or any(not isinstance(row, dict) for row in planned):
        raise ValueError("invalid planned Inspect cases")
    _unique_rows(planned)
    by_id = {r["id"]: r for r in rows}
    row_indexes = {r["id"]: i for i, r in enumerate(rows)}
    configs = {c["id"]: c for c in planned}
    if configs and not set(by_id) <= configs.keys():
        raise ValueError("foreign Inspect result")
    mode = report.get("model_mode", "unknown") if model else "mock"
    scope = _scope(source, model_mode=mode,
        grading="controller_verifier" if model else "runtime_oracle",
        subject_role=("agent_candidate" if mode == "live" else "scripted_candidate") if model else "runtime_regression")
    objective = "fixture_goal" if model else "runtime_regression"
    for index, ident in enumerate(configs or by_id):
        row = by_id.get(ident, {"id": ident})
        config = configs.get(ident, {})
        if model and row.get("fixture_success") is not None and row.get("runtime_case_pass") is not None:
            if _boolean(row["fixture_success"], "fixture_success") is not _boolean(row["runtime_case_pass"], "runtime_case_pass"):
                raise ValueError("contradictory fixture outcome")
        if (config.get("execution_kind") and row.get("execution_kind")
                and config["execution_kind"] != row["execution_kind"]):
            raise ValueError("Inspect execution kind contradicts plan")
        if config.get("execution_kind", row.get("execution_kind")) == "container_repair":
            oracle = _container_oracle(run, source, row, ident)
        else:
            oracle_spec = config.get("goal_files") if model else config.get("checks")
            oracle = _oracle(source, ("fixture-goal:" if model else "runtime-checks:") + digest(oracle_spec)
                             if oracle_spec is not None else "v1-reported-" + objective, objective, oracle_spec is not None)
        _case(run, source, row, row_indexes.get(ident, "planned:" + ident), objective, oracle, scope,
              row.get("fixture_success") if model else row.get("runtime_case_pass"),
              execution_id=row.get("task_id"), metrics=_usage(row, scope, (source.evidence.evidence_id,)),
              reason="planned case has no recorded result" if ident not in by_id else None)
    totals = (report.get("fixture_outcomes") or {}).get("planned") if model else (report.get("runtime_cases") or {}).get("total")
    declarations = [value for value in (totals, report.get("planned_cases"), len(configs) if "cases" in plan else None)
                    if value is not None]
    if declarations:
        declarations = [_count(value, "planned cases") for value in declarations]
        if len(set(declarations)) != 1:
            raise ValueError("Inspect planned denominator declarations disagree")
        totals = declarations[0]
    if totals is not None:
        total = _count(totals, "planned cases")
        observed = len(configs or by_id)
        if total < observed or (configs and total != len(configs)):
            raise ValueError("Inspect planned denominator mismatch")
        summary = report.get("fixture_outcomes") if model else report.get("runtime_cases")
        outcome_field = "fixture_success" if model else "runtime_case_pass"
        if summary:
            passed = sum(_boolean(r.get(outcome_field), outcome_field) is True for r in rows)
            failed = sum(r.get(outcome_field) is False for r in rows)
            expected = {"passed": passed, "failed": failed, "unknown": total - passed - failed}
            for key, value in expected.items():
                if key in summary and _count(summary[key], key) != value:
                    raise ValueError("Inspect aggregate outcome mismatch: " + key)
        if total > observed:
            run.unallocated.append({"identity": source.evidence.sha256 + ":planned-gap", "objective": objective,
                "scope": asdict(scope), "comparison": _comparison(source),
                "evidence_id": source.evidence.evidence_id, "observed_cases": observed,
                "count": total - observed, "reason": "planned IDs absent in old result"})
    else:
        run.limitations.append("PARTIAL: no complete planned denominator available")


def _container_oracle(run: EvaluationRun, source: Source, row: dict, ident: str) -> Oracle:
    bindings = source.json_file("bindings.json")
    if bindings is not None and not isinstance(bindings, dict):
        raise ValueError("container bindings must be an object")
    binding = (bindings or {}).get(ident, {})
    if not isinstance(binding, dict):
        raise ValueError("container binding must be an object")
    verifier = binding.get("verifier_id")
    records_path = ident + "/verifier-runs.json"
    records = source.json_file(records_path)
    records = [] if records is None else records
    if not isinstance(records, list):
        raise ValueError("container verifier records must be a list")
    relevant = [r for r in records if isinstance(r, dict) and row.get("task_id")
                and r.get("task_id") == row["task_id"]]
    if row.get("task_id") and len(relevant) != len(records):
        raise ValueError("container verifier record task identity conflict")
    if verifier and any(r.get("verifier_id") != verifier for r in relevant):
        raise ValueError("container binding/verifier identity conflict")
    refs = [source.evidence.evidence_id]
    for path in ("bindings.json", records_path):
        if path in source.files:
            data = source.files[path]
            evidence = Evidence("evidence-" + _hash(data), str(Path(source.entry["bundle"]) / path),
                                _hash(data), "unversioned-verifier-binding", seal_sha256=source.evidence.seal_sha256)
            run.evidence.append(evidence)
            refs.append(evidence.evidence_id)
    if not isinstance(verifier, str) or not verifier:
        run.limitations.append("PARTIAL: container verifier binding unavailable; empty file goals are not its oracle")
        return Oracle("v1-reported-container-verifier", "controller_verifier", "reported", tuple(refs))
    if not relevant:
        run.limitations.append("PARTIAL: container verifier invocation unavailable; binding identity is reported only")
        return Oracle(verifier, "controller_verifier", "reported", tuple(refs))
    if any(r.get("verifier_bundle_hash") is None for r in relevant):
        run.limitations.append("PARTIAL: exact container verifier bundle hash unavailable")
        return Oracle(verifier, "controller_verifier", "reported", tuple(refs))
    bundles = {sha256(r.get("verifier_bundle_hash")) for r in relevant}
    if len(bundles) != 1:
        raise ValueError("container verifier bundle identity conflict")
    return Oracle(verifier + ":bundle:" + next(iter(bundles)), "controller_verifier", "source_validated", tuple(refs))


def _candidate(run: EvaluationRun, source: Source):
    row = source.report
    _boolean(row.get("candidate_validated"), "candidate_validated")
    scope = _scope(source, grading="controller_verifier")
    # A candidate flag alone is a reported local result, never official resolved.
    _case(run, source, row, 0, "candidate_validation", _oracle(source, "v1-workspace-candidate-verifier",
          "controller_verifier"), scope, row.get("candidate_validated"),
          verdict="blocked" if row.get("processing_ok") is False or row.get("manual_recovery_required") is True else None,
          metrics=_usage(row, scope, (source.evidence.evidence_id,)))
    run.limitations.append("PARTIAL: candidate schema omits execution attempt and exact verifier identity; flag remains reported")


def _official_plan(source: Source, refs: dict) -> dict:
    """Recover planned subjects from pinned inputs, independently of grading."""
    if (not isinstance(refs, dict) or not {"invocation", "predictions"} <= refs.keys()
            or set(refs) - {"invocation", "predictions", "report"} or not source.files):
        raise ValueError("official provenance requires sealed invocation/predictions")
    data = {key: source.files[_sealed_path(path)] for key, path in refs.items()}
    invocation = _json(data["invocation"])
    predictions = [_json(line) for line in data["predictions"].splitlines() if line.strip()]
    if not predictions or any(not isinstance(r, dict) or any(not isinstance(r.get(key), str) or
            (key != "model_patch" and not r[key]) for key in ("instance_id", "model_name_or_path", "model_patch"))
            for r in predictions):
        raise ValueError("invalid official planned predictions")
    ids = [r["instance_id"] for r in predictions]
    if len(set(ids)) != len(ids):
        raise ValueError("official prediction denominator mismatch")
    for field in ("dataset_manifest_sha256", "prediction_sha256", "harness_commit"):
        if source.report.get(field) != invocation.get(field):
            raise ValueError("official invocation identity mismatch")
    sha256(source.report.get("dataset_manifest_sha256"))
    if re.fullmatch(r"[0-9a-f]{40}", source.report.get("harness_commit", "")) is None:
        raise ValueError("immutable Harness pin required")
    if _hash(data["predictions"]) != source.report.get("prediction_sha256"):
        raise ValueError("official prediction digest mismatch")
    argv = invocation.get("argv", [])
    if not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
        raise ValueError("invalid official invocation argv")
    execution = None
    if "--run_id" in argv:
        index = argv.index("--run_id") + 1
        if index >= len(argv) or not argv[index] or argv[index].startswith("--"):
            raise ValueError("official source run ID absent")
        execution = argv[index]
    if "--instance_ids" in argv:
        selected = []
        for arg in argv[argv.index("--instance_ids") + 1:]:
            if arg.startswith("--"):
                break
            selected.append(arg)
        if len(selected) != len(ids) or set(selected) != set(ids):
            raise ValueError("official invocation planned denominator mismatch")
    return {"data": data, "invocation": invocation, "predictions": predictions, "ids": ids, "execution": execution}


def _official_run(run: EvaluationRun, source: Source):
    report = source.report
    refs = source.entry.get("official_artifacts")
    outcomes, execution = None, None
    scope = _scope(source, allow_official=True)
    oracle = Oracle("unavailable-official-invocation", "official_harness", "unavailable")
    if refs is None and {"invocation.json", "predictions.jsonl"} <= source.files.keys():
        refs = {"invocation": "invocation.json", "predictions": "predictions.jsonl"}
    plan = _official_plan(source, refs) if refs is not None else None
    ids = plan["ids"] if plan else []
    if plan:
        execution = plan["execution"] if report.get("status") in {"STARTED", "FAILED", "REPORTED"} else None
        oracle = Oracle("official:" + report["harness_commit"] + ":" + report["dataset_manifest_sha256"],
                        "official_harness", "unavailable", (source.evidence.evidence_id,))
    if plan and "report" in plan["data"] and report.get("status") == "REPORTED":
        data, predictions = plan["data"], plan["predictions"]
        official = _json(data["report"])
        if (report.get("exit_code") != 0 or type(report.get("exit_code")) is not int
                or _hash(data["report"]) != report.get("report_sha256")):
            raise ValueError("official execution/report hash mismatch")
        from .swe_grading import read_report
        # read_report is a pure parser. A temporary file or Harness run is unnecessary.
        path = Path(source.entry["bundle"]) / refs["report"]
        outcomes = read_report(path, ids)
        empty = {r["instance_id"] for r in predictions if not r["model_patch"]}
        if set(official["empty_patch_ids"]) != empty or outcomes != report.get("official_resolved"):
            raise ValueError("official outcomes disagree with predictions/result")
        if not execution:
            raise ValueError("official source run ID absent")
        declared = source.entry.get("scope", {})
        if declared.get("grading") == "official_harness" and declared.get("environment") == "real_container":
            scope = _scope(source, grading="official_harness")
            oracle = _oracle(source, "official:" + report["harness_commit"] + ":" + report["dataset_manifest_sha256"],
                             "official_harness", True)
        else:
            outcomes = None
            scope = replace(scope, grading="unknown")
            run.limitations.append("PARTIAL: report chain is consistent but real official execution was not controller-declared")
    else:
        # Even a field named official_resolved does not grant official provenance.
        scope = replace(scope, grading="unknown")
        run.limitations.append("PARTIAL: official outcome is an unverified claim without sealed provenance bindings")
    claimed = report.get("official_resolved")
    if claimed is not None and not isinstance(claimed, dict):
        raise ValueError("official outcomes must be per-instance or null")
    if plan and claimed and not set(claimed) <= set(ids):
        raise ValueError("official claimed subjects are outside planned predictions")
    if ids or claimed:
        for index, ident in enumerate(ids or claimed):
            value = claimed.get(ident) if claimed else None
            _boolean(value, "official_resolved")
            _case(run, source, {"instance_id": ident, **{"error": report.get("error")}}, index,
                  "official_resolution", oracle, scope, outcomes[ident] if outcomes is not None else None,
                  execution_id=execution, verdict="blocked" if report.get("status") == "FAILED" else None,
                  reason="verified official report" if outcomes is not None else "official provenance unavailable")
    else:
        run.limitations.append("PARTIAL: official schema supplies no planned instance identities")


def adapt(source: Source) -> EvaluationRun:
    schema = source.report["schema_version"]
    run = EvaluationRun(source.entry["id"], schema, evidence=[source.evidence], raw=source.report)
    run.limitations.append("Hashes verify bytes; provenance and execution context depend on the controller, not authentication")
    if schema == "evaluation-report-v1":
        _evaluation(run, source)
    elif schema in {"inspect-model-result-v1", "inspect-runtime-result-v1"}:
        _inspect_cases(run, source)
    elif schema == "inspect-swe-candidate-v1":
        _candidate(run, source)
    elif schema == "swe-official-run-v1":
        _official_run(run, source)
    elif schema == "junit":
        scope = _scope(source, model_mode="none", grading="test_assertion", subject_role="test_execution")
        for index, row in enumerate(source.report["cases"]):
            _case(run, source, row, index, "test_execution", _oracle(source, "junit-test-assertions", "test_assertion"),
                  scope, None, verdict=row["verdict"], reason=row["reason"])
        run.limitations.append("JUnit describes test execution, not Agent task success; no Runtime attempt identity inferred")
    elif schema == "evaluation-suite-result-v1":
        jobs = source.report.get("jobs", [])
        if not isinstance(jobs, list):
            raise ValueError("suite jobs must be a list")
        run.metrics.append(Metric("planned_suite_jobs", len(jobs), "jobs", True,
                                  evidence_refs=(source.evidence.evidence_id,)))
        stem = Path(source.evidence.path).stem
        if re.fullmatch(r"[0-9a-f]{32}", stem):
            run.attempts.append(Attempt("suite-invocation:" + stem, "suite_invocation", stem,
                                        (source.evidence.evidence_id,), raw={"jobs": jobs}))
        run.limitations.append("PARTIAL: summary-only jobs retain raw metrics; no task cases or case attempts inferred")
    else:
        run.limitations.append("Projection only: no new cases or execution attempts; supply the original source bundle")
    for case in run.cases:
        if case.source_identity is None:
            message = "PARTIAL: some cases lack source execution identity; only identical evidence observations can be deduplicated"
            if message not in run.limitations:
                run.limitations.append(message)
    _record_attempts(run, source)
    return run


def _record_attempts(run: EvaluationRun, source: Source) -> None:
    """Project only persisted records; retries never create case observations."""
    specs = {"model-calls.json": ("model_generation", "model_call_id"),
             "model_calls.input": ("model_generation", "model_call_id"),
             "verifier-runs.json": ("verifier_invocation", "verifier_run_id"),
             "tool-calls.json": ("tool_operation", "tool_use_id")}
    seen = {a.attempt_id: a for a in run.attempts}
    for path, data in source.files.items():
        spec = specs.get(path.rsplit("/", 1)[-1])
        if not spec:
            continue
        records = _json(data)
        if not isinstance(records, list):
            raise ValueError("attempt records must be a list")
        kind, id_field = spec
        ev_id = "evidence-" + _hash(data)
        run.evidence.append(Evidence(ev_id, str(Path(source.entry["bundle"]) / path), _hash(data),
            "unversioned-" + kind + "-records", seal_sha256=source.evidence.seal_sha256))
        for row in records:
            if not isinstance(row, dict) or not row.get("task_id") or row.get(id_field) is None:
                run.limitations.append("PARTIAL: record without task/attempt identity preserved in source only: " + path)
                continue
            key = "attempt-" + digest({"task_id": row["task_id"], "kind": kind, "source_id": row[id_field]})
            parents = {a.attempt_id for a in run.attempts if a.kind == "case_execution" and a.source_id == row["task_id"]}
            attempt = Attempt(key, kind, str(row[id_field]), (ev_id,),
                              next(iter(parents)) if len(parents) == 1 else None, row)
            if key in seen and seen[key].raw != attempt.raw:
                raise ValueError("conflicting persisted attempt identity")
            if key not in seen:
                run.attempts.append(attempt)
                seen[key] = attempt
