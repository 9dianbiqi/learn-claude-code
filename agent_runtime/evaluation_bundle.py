"""Read and verify sealed evaluation-v1 evidence without executing its plan."""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .eval_runner import _unsafe_reparse_chain
from .evaluation import _aggregate, _hash, _identifier, _json, _read

OUTCOME_FIELDS = {"runtime": "runtime_case_pass", "agent": "task_success", "environment": "official_resolved"}
PROJECTION_FIELDS = (
    "id", "mode", "kind", "source_run_id", "instance_id", "historical", "processing",
    "runtime_status", "runtime_case_pass", "task_success", "official_resolved", "official_status",
    "failure_category", "provider_input_tokens", "provider_output_tokens", "usage_complete",
    "model_calls_recorded", "tool_calls_recorded", "tool_execution_attempts", "invariant_violation_count",
    "patch_bytes", "patch_sha256",
)


def _sealed_path(value: Any) -> str:
    # Unlike solver paths, archive paths legitimately include .agent_runtime DBs.
    if (not isinstance(value, str) or not value or value.startswith("/") or ":" in value
            or "\\" in value or any(c in value for c in ("\0", "\r", "\n"))
            or PurePosixPath(value).as_posix() != value
            or any(part in {".", ".."} or part.endswith((".", " ")) for part in value.split("/"))):
        raise ValueError("unsafe sealed path")
    return value


def _inventory(root: Path) -> set[str]:
    def enumeration_error(error):
        raise ValueError("bundle enumeration failed") from error
    files: set[str] = set()
    visited = 0
    for base, dirs, names in os.walk(root, followlinks=False, onerror=enumeration_error):
        visited += 1 + len(dirs) + len(names)
        if visited > 4096:
            raise ValueError("bundle inventory too large")
        for name in dirs + names:
            path = Path(base) / name
            if _unsafe_reparse_chain(path):
                raise ValueError("bundle contains a symlink/reparse point")
        for name in names:
            files.add((Path(base) / name).relative_to(root).as_posix())
    return files


def verify_bundle(root: str | Path, seal_sha256: str) -> dict:
    """Require a trusted external digest; a rewritten local seal is insufficient."""
    root = Path(os.path.abspath(root))
    if not re.fullmatch(r"[0-9a-f]{64}", seal_sha256):
        raise ValueError("trusted seal SHA-256 required")
    if _unsafe_reparse_chain(root) or not root.is_dir():
        raise ValueError("bundle root must be a directory without reparse parents")
    seal_bytes = _read(root / "seal.json")
    if _hash(seal_bytes) != seal_sha256:
        raise ValueError("external seal digest mismatch")
    seal = _json(seal_bytes)
    if not isinstance(seal, list) or not 1 <= len(seal) <= 2048:
        raise ValueError("invalid seal inventory")
    pinned: dict[str, str] = {}
    portable = set()
    for entry in seal:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("invalid seal entry")
        name = _sealed_path(entry["path"])
        if name == "seal.json" or name.casefold() in portable:
            raise ValueError("duplicate or self-referencing seal path")
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ValueError("invalid sealed digest")
        pinned[name] = entry["sha256"]
        portable.add(name.casefold())
    if _inventory(root) != set(pinned) | {"seal.json"}:
        raise ValueError("bundle file inventory mismatch")
    total_bytes = 0
    # Stream one bounded artifact at a time, retaining only data needed by adapter.
    selected = {}
    for name, expected in pinned.items():
        data = _read(root / name)
        total_bytes += len(data)
        if total_bytes > 128 * 1024 * 1024:
            raise ValueError("bundle exceeds 128 MiB")
        if _hash(data) != expected:
            raise ValueError("sealed file digest mismatch")
        if name in {"plan.json", "report.json"} or name.endswith("/result.json"):
            selected[name] = data
    if not {"plan.json", "report.json"} <= selected.keys():
        raise ValueError("sealed plan/report missing")
    plan, report = _json(selected["plan.json"]), _json(selected["report.json"])
    if not isinstance(plan, dict) or not isinstance(report, dict):
        raise ValueError("plan/report must be objects")
    if plan.get("schema_version") != "evaluation-plan-v1" or report.get("schema_version") != "evaluation-report-v1":
        raise ValueError("unsupported bundle schema")
    _identifier(plan.get("run_id"))
    if report.get("run_id") != plan["run_id"] or report.get("plan_sha256") != _hash(selected["plan.json"]):
        raise ValueError("plan/report provenance mismatch")
    cases, rows = plan.get("cases"), report.get("results")
    if (not isinstance(cases, list) or not 1 <= len(cases) <= 64 or not isinstance(rows, list)
            or len(rows) != len(cases)):
        raise ValueError("planned result denominator mismatch")
    ids, subjects = set(), set()
    projected = []
    for case, row in zip(cases, rows):
        if not isinstance(case, dict) or not isinstance(row, dict):
            raise ValueError("cases/results must be objects")
        ident = _identifier(case.get("id"))
        if ident.casefold() in ids or row.get("id") != ident or row.get("mode") != case.get("mode"):
            raise ValueError("case/result identity mismatch")
        ids.add(ident.casefold())
        mode = case.get("mode")
        if mode == "scripted_runtime":
            kind, historical = "runtime", False
        elif mode == "agent_archive":
            kind, historical = "agent", True
        elif mode == "official_archive" and case.get("scope") in {"agent", "gold_control"}:
            kind, historical = ("agent" if case["scope"] == "agent" else "environment"), True
        else:
            raise ValueError("unsupported case mode/scope")
        if row.get("kind") != kind or row.get("historical") is not historical:
            raise ValueError("kind/historical provenance mismatch")
        if historical:
            for key in ("source_run_id", "instance_id"):
                _identifier(case.get(key))
                if row.get(key) != case[key]:
                    raise ValueError("archived subject mismatch")
            subject = (case["source_run_id"], case["instance_id"])
            if subject in subjects:
                raise ValueError("duplicate archived subject")
            subjects.add(subject)
        if row.get("processing") not in {"ok", "error"}:
            raise ValueError("invalid processing status")
        for field in OUTCOME_FIELDS.values():
            if field not in row or (row[field] is not None and type(row[field]) is not bool):
                raise ValueError("outcomes must be boolean or null")
        if row["processing"] == "error" and any(row[f] is not None for f in OUTCOME_FIELDS.values()):
            raise ValueError("processing error cannot carry a known outcome")
        if kind != "runtime" and row["runtime_case_pass"] is not None:
            raise ValueError("archive cannot carry a runtime oracle")
        if kind != "agent" and row["task_success"] is not None:
            raise ValueError("non-Agent result cannot carry Agent success")
        if mode != "official_archive" and row["official_resolved"] is not None:
            raise ValueError("official success without official archive")
        if mode == "agent_archive" and row["task_success"] is True:
            raise ValueError("unverified Agent archive cannot establish success")
        if mode == "agent_archive" and case.get("model_mode") not in {"live", "scripted"}:
            raise ValueError("archived model mode must be explicit")
        official = row["official_resolved"]
        if official is not None:
            if row.get("official_status") != ("resolved" if official else "unresolved"):
                raise ValueError("official status/outcome mismatch")
            if kind == "agent" and row["task_success"] is not official:
                raise ValueError("official Agent success mismatch")
        elif row.get("official_status") in {"resolved", "unresolved"}:
            raise ValueError("official status without a known verdict")
        expected_result = selected.get(ident + "/result.json")
        if expected_result is None or _json(expected_result) != row:
            raise ValueError("per-case result differs from report")
        for field in ("provider_input_tokens", "provider_output_tokens", "model_calls_recorded",
                      "tool_calls_recorded", "tool_execution_attempts", "invariant_violation_count", "patch_bytes"):
            value = row.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("counts must be nonnegative integers or null")
        if mode == "scripted_runtime" or (mode == "agent_archive" and case["model_mode"] == "scripted"):
            if row.get("provider_input_tokens") is not None or row.get("provider_output_tokens") is not None:
                raise ValueError("scripted usage cannot be reported as provider usage")
        enums = {
            "runtime_status": {None, "pending", "running", "completed", "failed", "needs_review", "aborted",
                               "unresolved", "budget_exhausted", "waiting_approval"},
            "official_status": {"not_evaluated", "execution_error", "empty_patch", "resolved", "unresolved"},
            "failure_category": {"none", "runtime_oracle", "task_semantic", "environment_setup", "evidence_invalid"},
        }
        for field, allowed in enums.items():
            if not isinstance(row.get(field), (str, type(None))) or row.get(field) not in allowed:
                raise ValueError("unsupported projected status/category")
        if row.get("usage_complete") is not None and type(row["usage_complete"]) is not bool:
            raise ValueError("usage completeness must be boolean or null")
        patch_hash = row.get("patch_sha256")
        if patch_hash is not None and (not isinstance(patch_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", patch_hash)):
            raise ValueError("invalid patch digest")
        # Deliberately omit arbitrary row fields, raw inputs, prompts and patches.
        projected.append({key: row.get(key) for key in PROJECTION_FIELDS})
    aggregates = _aggregate(rows)
    if report.get("aggregates") != aggregates:
        raise ValueError("source aggregates disagree with planned rows")
    if report.get("processing_ok") is not all(r["processing"] == "ok" for r in rows):
        raise ValueError("source processing summary mismatch")
    if any(type(report.get(k)) is not int or report[k] != 0 for k in ("new_provider_requests", "new_official_evaluations")):
        raise ValueError("not an offline evaluation-v1 bundle")
    return {"schema_version": "inspect-offline-input-v1", "source_run_id": report["run_id"],
            "source_seal_sha256": seal_sha256, "source_report_sha256": _hash(selected["report.json"]),
            "source_aggregates": aggregates, "source_processing_ok": report["processing_ok"],
            "results": projected}


def score_counters(row: dict) -> dict[str, int]:
    value = row[OUTCOME_FIELDS[row["kind"]]]
    return {"success_lower_bound": int(value is True), "failure_fraction": int(value is False),
            "unknown_fraction": int(value is None), "known_fraction": int(value is not None)}
