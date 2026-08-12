from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .fake_model import ScriptedModel
from .models import ModelResponse, ToolCall
from .runtime import InjectedCrash, Runtime
from .trace import TraceReporter


_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_reparse_or_symlink(path: Path) -> bool:
    """Return true for symlinks and Windows reparse points without following them."""
    try:
        if path.is_symlink():
            return True
        stat = path.lstat()
    except FileNotFoundError:
        return False
    return bool(getattr(stat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _unsafe_reparse_chain(path: Path) -> bool:
    """Check the lexical root and every existing parent before any resolve()."""
    lexical = Path(os.path.abspath(os.fspath(path)))
    current = lexical
    while True:
        if _is_reparse_or_symlink(current):
            return True
        if current.exists() and current.is_dir() is False and current != lexical:
            return True
        if current.parent == current:
            break
        current = current.parent
    return False


def _identity(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    return (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)), int(getattr(stat, "st_file_attributes", 0)))


def _response_from_entry(entry: dict[str, Any]) -> ModelResponse:
    if "tool" in entry:
        return ModelResponse(
            tool_calls=[ToolCall(str(entry["id"]), str(entry["tool"]), dict(entry.get("input", {})))],
            usage=dict(entry.get("usage", {})),
        )
    return ModelResponse(
        text=str(entry.get("text", "")),
        stop_reason=entry.get("stop_reason"),
        usage=dict(entry.get("usage", {})),
    )


def _write_fixture(root: Path, files: dict[str, Any]) -> None:
    for relative, content in files.items():
        raw = str(relative).replace("\\", "/")
        if raw.startswith("//") or re.match(r"^[A-Za-z]:[^/]", raw) or any(part == ".." for part in raw.split("/")):
            raise ValueError(f"Fixture path escapes eval root: {relative}")
        if any(":" in part for index, part in enumerate(raw.split("/")) if not (index == 0 and re.match(r"^[A-Za-z]:$", part))):
            raise ValueError(f"Fixture path uses an alternate data stream: {relative}")
        path = (root / raw).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"Fixture path escapes eval root: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")


def run_suite(suite_path: str | Path, run_root: str | Path | None = None) -> dict[str, Any]:
    suite_path = Path(suite_path)
    document = yaml.safe_load(suite_path.read_text(encoding="utf-8")) or {}
    suite_name = str(document.get("name", suite_path.stem))
    tasks = document.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("Eval suite tasks must be a list")
    root = Path(run_root or (suite_path.parent / ".runs"))
    root_lexical = Path(os.path.abspath(os.fspath(root)))
    if _unsafe_reparse_chain(root_lexical):
        raise ValueError("Eval run root or parent is a symlink/reparse point")
    root_candidate = root_lexical.resolve()
    if (
        any(part.casefold() == ".agent_runtime" for part in root_candidate.parts)
        or root_candidate == Path.cwd().resolve()
        or (root_candidate / ".git").exists()
    ):
        raise ValueError("Eval run root cannot be the repository or .agent_runtime")
    marker = root / ".agent-runtime-eval-root"
    if root.exists():
        if not root.is_dir():
            raise ValueError("Eval run root must be a directory")
        if _is_reparse_or_symlink(root) or _is_reparse_or_symlink(marker) or not marker.is_file() or marker.read_text(encoding="utf-8") != "agent-runtime-eval-root-v1\n":
            raise ValueError("Eval run root marker is invalid")
    else:
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("agent-runtime-eval-root-v1\n", encoding="utf-8")
    root_identity = _identity(root)
    root_resolved = root.resolve()
    results: list[dict[str, Any]] = []
    for spec in tasks:
        task_id = str(spec["id"])
        if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", task_id) is None or task_id in {".", ".."}:
            raise ValueError(f"Invalid eval task id: {task_id!r}")
        task_root = root / task_id
        if _unsafe_reparse_chain(task_root):
            raise ValueError("Eval task root or parent is a symlink/reparse point")
        if not task_root.resolve().is_relative_to(root_resolved) or task_root.resolve() == root_resolved:
            raise ValueError(f"Eval task path escapes run root: {task_id!r}")
        if task_root.exists():
            if (
                not marker.exists()
                or not marker.is_file()
                or _is_reparse_or_symlink(task_root)
                or _is_reparse_or_symlink(root)
                or _identity(root) != root_identity
                or _is_reparse_or_symlink(marker)
                or marker.read_text(encoding="utf-8") != "agent-runtime-eval-root-v1\n"
                or not task_root.resolve().is_relative_to(root_resolved)
            ):
                raise ValueError("Refusing to remove an unmarked eval task directory")
            shutil.rmtree(task_root)
        task_root.mkdir(parents=True)
        _write_fixture(task_root, dict(spec.get("fixture", {}).get("files", {})))
        model = ScriptedModel([_response_from_entry(entry) for entry in spec.get("model", [])])
        fault = dict(spec.get("fault", {}))

        fault_state = {"triggered": False}

        def inject(point, tool_use_id=None, fault=fault, fault_state=fault_state, **_):
            if fault.get("point") == point and (not fault.get("tool_use_id") or fault["tool_use_id"] == tool_use_id):
                fault_state["triggered"] = True
                raise InjectedCrash(point)

        runtime = Runtime(
            task_root,
            model=model,
            approval_callback=lambda *_: True,
            fault_injector=inject if fault else None,
        )
        interrupted = False
        fault_triggered = False
        evaluation_error: str | None = None
        if fault:
            try:
                runtime.run(str(spec.get("prompt", task_id)))
            except InjectedCrash:
                interrupted = True
            fault_triggered = bool(fault_state["triggered"])
            if fault_triggered:
                tasks_in_store = runtime.store.list_tasks()
                recovery_model = ScriptedModel([_response_from_entry(entry) for entry in spec.get("recovery_model", [])])
                try:
                    if tasks_in_store:
                        runtime = Runtime(task_root, model=recovery_model, approval_callback=lambda *_: True)
                        result = runtime.resume(tasks_in_store[0]["task_id"])
                    else:
                        runtime = Runtime(task_root, model=recovery_model, approval_callback=lambda *_: True)
                        result = runtime.run(str(spec.get("prompt", task_id)))
                except Exception as exc:  # noqa: BLE001 - eval records failure rather than masking the suite
                    evaluation_error = f"{type(exc).__name__}: {exc}"
                    result = type("EvalResult", (), {"task_id": tasks_in_store[0]["task_id"] if tasks_in_store else "unknown", "status": "failed", "error": evaluation_error})()
            else:
                result = runtime.run(str(spec.get("prompt", task_id)))
                evaluation_error = "configured fault point was not triggered"
        else:
            result = runtime.run(str(spec.get("prompt", task_id)))
        checks = dict(spec.get("checks", {}))
        passed = result.status == checks.get("status", "completed")
        for relative, expected in dict(checks.get("files", {})).items():
            raw = str(relative).replace("\\", "/")
            path = (task_root / raw).resolve()
            if not path.is_relative_to(task_root.resolve()):
                passed = False
                continue
            passed = passed and path.exists() and path.read_text(encoding="utf-8") == str(expected)
        summary = TraceReporter(runtime.store).summary(result.task_id)
        if "interrupted" in checks:
            passed = passed and interrupted == bool(checks["interrupted"])
        if "fault_triggered" in checks:
            passed = passed and fault_triggered == bool(checks["fault_triggered"])
        if "model_calls" in checks:
            passed = passed and summary["model_calls"] == int(checks["model_calls"])
        if evaluation_error:
            passed = False
        results.append({
            "id": task_id,
            "task_id": result.task_id,
            "status": result.status,
            "passed": bool(passed),
            "error": result.error,
            "interrupted": interrupted,
            "fault_triggered": fault_triggered,
            "recovery_case": bool(fault and interrupted and fault_triggered),
            "evaluation_error": evaluation_error,
            "trace": summary,
        })
    total = len(results)
    completed = sum(item["status"] == "completed" for item in results)
    recovery_items = [item for item in results if item["recovery_case"]]
    recovery_passed = sum(item["passed"] for item in recovery_items)
    return {
        "suite": suite_name,
        "total": total,
        "passed": sum(item["passed"] for item in results),
        "completion_rate": completed / total if total else 1.0,
        "recovery_cases": len(recovery_items),
        "recovery_passed": recovery_passed,
        "recovery_success_rate": recovery_passed / len(recovery_items) if recovery_items else 1.0,
        "actual_interruption_count": sum(item["interrupted"] for item in results),
        "total_input_tokens": sum(item["trace"]["total_input_tokens"] for item in results),
        "total_output_tokens": sum(item["trace"]["total_output_tokens"] for item in results),
        "total_duration_seconds": sum(item["trace"]["duration_seconds"] for item in results),
        "duplicate_tool_calls": sum(item["trace"]["duplicate_tool_calls"] for item in results),
        "tool_execution_attempts": sum(item["trace"]["tool_execution_attempts"] for item in results),
        "effect_attempts": sum(item["trace"]["effect_attempts"] for item in results),
        "duplicate_effect_attempts": sum(item["trace"]["duplicate_effect_attempts"] for item in results),
        "confirmed_duplicate_side_effects": sum(item["trace"]["confirmed_duplicate_side_effects"] for item in results),
        "permission_bypass_count": sum(item["trace"]["permission_bypass_count"] for item in results),
        "invariant_violation_count": sum(item["trace"]["invariant_violation_count"] for item in results),
        "needs_review_correctness": (
            sum(item["trace"]["needs_review_correctness"] for item in results) / total if total else 1.0
        ),
        "stale_lease_execution_attempts": sum(item["trace"]["stale_lease_execution_attempts"] for item in results),
        "tasks": results,
    }


def write_report(report: dict[str, Any], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
