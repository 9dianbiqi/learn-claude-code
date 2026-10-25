"""Pure-core bundle integrity tests, usable without optional Inspect installed."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_runtime.evaluation import _aggregate
from agent_runtime.evaluation_bundle import score_counters, verify_bundle


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def pin(root):
    entries = [{"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
               for p in sorted(root.rglob("*")) if p.is_file() and p != root / "seal.json"]
    dump(root / "seal.json", entries)
    return hashlib.sha256((root / "seal.json").read_bytes()).hexdigest()


def row(ident, kind, value, processing="ok"):
    mode = {"runtime": "scripted_runtime", "agent": "agent_archive", "environment": "official_archive"}[kind]
    result = {"id": ident, "kind": kind, "mode": mode, "historical": kind != "runtime",
              "processing": processing, "runtime_status": "completed" if kind == "runtime" else None,
              "runtime_case_pass": None, "task_success": None, "official_resolved": None,
              "official_status": "not_evaluated", "failure_category": "none",
              "provider_input_tokens": None, "provider_output_tokens": None,
              "source_run_id": "original-" + ident if kind != "runtime" else None,
              "instance_id": "sympy__sympy-19346" if kind != "runtime" else None}
    result[{"runtime": "runtime_case_pass", "agent": "task_success", "environment": "official_resolved"}[kind]] = value
    if kind == "environment" and value is not None:
        result["official_status"] = "resolved" if value else "unresolved"
    return result


def bundle(root, rows=None):
    root.mkdir()
    if rows is None:
        rows = [row("regression", "runtime", True), row("agent-fail", "agent", False),
                row("agent-unknown", "agent", None), row("gold", "environment", True)]
    cases = [{k: r[k] for k in ("id", "mode", "source_run_id", "instance_id")} for r in rows]
    for case in cases:
        if case["mode"] == "official_archive":
            case["scope"] = "gold_control"
        elif case["mode"] == "agent_archive":
            case["model_mode"] = "live"
    plan = {"schema_version": "evaluation-plan-v1", "run_id": "source-offline", "cases": cases}
    dump(root / "plan.json", plan)
    for r in rows:
        dump(root / r["id"] / "result.json", r)
    # Deliberately sensitive evaluator-only bytes must never reach Inspect samples.
    (root / "gold-patch.input").write_text("GOLD_PATCH_SECRET_SENTINEL", encoding="utf-8")
    report = {"schema_version": "evaluation-report-v1", "run_id": plan["run_id"],
              "plan_sha256": hashlib.sha256((root / "plan.json").read_bytes()).hexdigest(),
              "results": rows, "aggregates": _aggregate(rows),
              "processing_ok": all(r["processing"] == "ok" for r in rows),
              "new_provider_requests": 0, "new_official_evaluations": 0}
    dump(root / "report.json", report)
    return pin(root)


def test_projection_separates_groups_preserves_null_and_omits_raw(tmp_path):
    root = tmp_path / "bundle"
    trusted = bundle(root)
    projected = verify_bundle(root, trusted)
    assert projected["source_aggregates"]["agent"] == {
        "total": 2, "passed": 0, "failed": 1, "unknown": 1, "success_rate": None}
    assert projected["results"][2]["task_success"] is None
    assert "GOLD_PATCH_SECRET_SENTINEL" not in json.dumps(projected)
    assert projected["results"][0]["provider_input_tokens"] is None


@pytest.mark.parametrize("value,expected", [(True, (1, 0, 0, 1)), (False, (0, 1, 0, 1)), (None, (0, 0, 1, 0))])
def test_counter_unknown_is_kept_in_denominator(value, expected):
    assert tuple(score_counters(row("a", "agent", value)).values()) == expected


def test_external_pin_rejects_resealed_changes(tmp_path):
    root = tmp_path / "bundle"
    trusted = bundle(root)
    (root / "gold-patch.input").write_text("changed")
    with pytest.raises(ValueError, match="file digest"):
        verify_bundle(root, trusted)
    pin(root)
    with pytest.raises(ValueError, match="external seal"):
        verify_bundle(root, trusted)


@pytest.mark.parametrize("change", ["extra", "missing", "duplicate", "traversal", "self"])
def test_inventory_mutations_fail(tmp_path, change):
    root = tmp_path / "bundle"
    trusted = bundle(root)
    if change == "extra":
        (root / "unsealed").write_text("extra")
    elif change == "missing":
        (root / "gold-patch.input").unlink()
    else:
        seal = json.loads((root / "seal.json").read_text())
        if change == "duplicate":
            seal.append(seal[0])
        else:
            seal[0]["path"] = "../outside" if change == "traversal" else "seal.json"
        dump(root / "seal.json", seal)
        trusted = hashlib.sha256((root / "seal.json").read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        verify_bundle(root, trusted)


@pytest.mark.parametrize("change", ["type", "aggregate", "row", "group", "processing", "order", "source", "model_count", "category"])
def test_inconsistent_but_resealed_bundle_rejected(tmp_path, change):
    root = tmp_path / "bundle"
    bundle(root)
    report = json.loads((root / "report.json").read_text())
    if change == "type":
        report["results"][0]["runtime_case_pass"] = 1
    elif change == "aggregate":
        report["aggregates"]["agent"]["success_rate"] = 1.0
    elif change == "row":
        dump(root / "regression" / "result.json", {})
    elif change == "group":
        report["results"][-1]["kind"] = "agent"
    elif change == "processing":
        report["processing_ok"] = False
    elif change == "order":
        report["results"].reverse()
    elif change == "source":
        report["results"][1]["source_run_id"] = "different"
    elif change == "model_count":
        report["results"][0]["model_calls_recorded"] = -1
    else:
        report["results"][0]["failure_category"] = "GOLD_PATCH_SECRET_SENTINEL"
    if change not in {"row", "aggregate", "processing", "order"}:
        for r in report["results"]:
            dump(root / r["id"] / "result.json", r)
    dump(root / "report.json", report)
    with pytest.raises(ValueError):
        verify_bundle(root, pin(root))


def test_processing_error_preserved_as_unknown(tmp_path):
    root = tmp_path / "bundle"
    trusted = bundle(root, [row("invalid", "agent", None, processing="error")])
    projected = verify_bundle(root, trusted)
    assert projected["source_processing_ok"] is False
    assert projected["source_aggregates"]["agent"]["unknown"] == 1
    assert score_counters(projected["results"][0])["unknown_fraction"] == 1


def test_symlink_inventory_rejected(tmp_path):
    root = tmp_path / "bundle"
    trusted = bundle(root)
    try:
        (root / "link").symlink_to(root / "report.json")
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ValueError, match="reparse"):
        verify_bundle(root, trusted)


def test_internal_runtime_state_paths_are_valid_archive_paths(tmp_path):
    root = tmp_path / "bundle"
    bundle(root)
    state = root / "regression" / "workspace" / ".agent_runtime" / "runtime.db"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"archive-only")
    assert verify_bundle(root, pin(root))["results"][0]["runtime_case_pass"] is True


@pytest.mark.parametrize("change", ["agent_success", "fake_provider", "official_status"])
def test_unjustified_success_or_provider_usage_rejected(tmp_path, change):
    root = tmp_path / "bundle"
    bundle(root)
    report = json.loads((root / "report.json").read_text())
    if change == "agent_success":
        report["results"][1]["task_success"] = True
    elif change == "fake_provider":
        report["results"][0]["provider_input_tokens"] = 10
    else:
        report["results"][-1]["official_status"] = "not_evaluated"
    for r in report["results"]:
        dump(root / r["id"] / "result.json", r)
    report["aggregates"] = _aggregate(report["results"])
    dump(root / "report.json", report)
    with pytest.raises(ValueError):
        verify_bundle(root, pin(root))


def test_directory_enumeration_error_is_not_silently_skipped(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    trusted = bundle(root)
    hidden = root / "unreadable"
    hidden.mkdir()
    (hidden / "unsealed.txt").write_text("must not be silently missed")
    original = os.scandir
    def fail_scandir(path):
        if Path(path) == hidden:
            raise PermissionError("injected directory enumeration failure")
        return original(path)
    monkeypatch.setattr(os, "scandir", fail_scandir)
    with pytest.raises(ValueError, match="enumeration failed"):
        verify_bundle(root, trusted)
