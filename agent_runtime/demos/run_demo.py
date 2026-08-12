from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import InjectedCrash, Runtime
from agent_runtime.trace import TraceReporter


def _fresh_directory(path: Path) -> Path:
    path = path.resolve()
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"Demo output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_artifacts(runtime: Runtime, task_id: str, directory: Path, extra: dict[str, Any]) -> dict[str, Any]:
    reporter = TraceReporter(runtime.store)
    trace = reporter.summary(task_id)
    reporter.export_jsonl(task_id, directory / "trace.jsonl")
    summary = {**extra, "task_id": task_id, "trace": trace}
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def normal_edit(root: Path) -> dict[str, Any]:
    directory = _fresh_directory(root / "normal-edit")
    note = directory / "note.txt"
    note.write_text("OLD_VALUE", encoding="utf-8")
    runtime = Runtime(
        directory,
        ScriptedModel([
            ModelResponse(tool_calls=[ToolCall("normal-read", "read_file", {"path": "note.txt"})]),
            ModelResponse(tool_calls=[ToolCall(
                "normal-edit", "edit_file",
                {"path": "note.txt", "old_text": "OLD_VALUE", "new_text": "NEW_VALUE"},
            )]),
            ModelResponse(text="normal edit complete"),
        ]),
        approval_callback=lambda *_: True,
    )
    result = runtime.run("Read note.txt and replace OLD_VALUE with NEW_VALUE.")
    assert result.status == "completed"
    assert note.read_text(encoding="utf-8") == "NEW_VALUE"
    return _write_artifacts(runtime, result.task_id, directory, {
        "scenario": "normal_edit",
        "status": result.status,
        "final_content": note.read_text(encoding="utf-8"),
    })


def crash_recovery(root: Path) -> dict[str, Any]:
    directory = _fresh_directory(root / "crash-recovery")
    note = directory / "note.txt"
    note.write_text("OLD_VALUE", encoding="utf-8")

    def fault(point: str, **context: Any) -> None:
        if point == "after_tool_effect_before_persist" and context.get("tool_use_id") == "recovery-edit":
            raise InjectedCrash(point)

    first = Runtime(
        directory,
        ScriptedModel([
            ModelResponse(tool_calls=[ToolCall("recovery-read", "read_file", {"path": "note.txt"})]),
            ModelResponse(tool_calls=[ToolCall(
                "recovery-edit", "edit_file",
                {"path": "note.txt", "old_text": "OLD_VALUE", "new_text": "RECOVERED_VALUE"},
            )]),
        ]),
        approval_callback=lambda *_: True,
        fault_injector=fault,
    )
    try:
        first.run("Read and recover note.txt.")
    except InjectedCrash:
        pass
    else:  # pragma: no cover - a missing fault is an invalid demonstration
        raise AssertionError("Expected the recovery fault to trigger")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert note.read_text(encoding="utf-8") == "RECOVERED_VALUE"
    recovered = Runtime(
        directory,
        ScriptedModel([ModelResponse(text="recovery complete")]),
        approval_callback=lambda *_: True,
    )
    result = recovered.resume(task_id)
    call = recovered.store.get_tool_call(task_id, "recovery-edit")
    assert result.status == "completed"
    assert int(call["effect_attempts"]) == 1
    return _write_artifacts(recovered, task_id, directory, {
        "scenario": "crash_recovery",
        "status": result.status,
        "final_content": note.read_text(encoding="utf-8"),
        "effect_attempts": int(call["effect_attempts"]),
    })


class _ConflictModel:
    name = "deterministic-conflict"

    def __init__(self, note: Path):
        self.note = note
        self.call_count = 0

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        del messages, tools
        self.call_count += 1
        if self.call_count == 1:
            return ModelResponse(tool_calls=[ToolCall("conflict-read", "read_file", {"path": "note.txt"})])
        if self.call_count == 2:
            self.note.write_text("VERSION_EXTERNAL", encoding="utf-8")
            return ModelResponse(tool_calls=[ToolCall(
                "conflict-edit", "edit_file",
                {"path": "note.txt", "old_text": "VERSION_ONE", "new_text": "VERSION_AGENT"},
            )])
        return ModelResponse(text="unexpected continuation")


def hash_conflict(root: Path) -> dict[str, Any]:
    directory = _fresh_directory(root / "hash-conflict")
    note = directory / "note.txt"
    note.write_text("VERSION_ONE", encoding="utf-8")
    runtime = Runtime(directory, _ConflictModel(note), approval_callback=lambda *_: True)
    result = runtime.run("Read note.txt and replace VERSION_ONE with VERSION_AGENT.")
    assert result.status == "needs_review"
    assert note.read_text(encoding="utf-8") == "VERSION_EXTERNAL"
    return _write_artifacts(runtime, result.task_id, directory, {
        "scenario": "hash_conflict",
        "status": result.status,
        "final_content": note.read_text(encoding="utf-8"),
        "error": result.error,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic Agent Runtime demonstrations.")
    parser.add_argument("--output-root", required=True, help="New or empty directory for demo artifacts.")
    parser.add_argument(
        "--scenario", choices=("all", "normal", "recovery", "conflict"), default="all"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _fresh_directory(Path(args.output_root))
    runners = {
        "normal": normal_edit,
        "recovery": crash_recovery,
        "conflict": hash_conflict,
    }
    selected = list(runners) if args.scenario == "all" else [args.scenario]
    results = [runners[name](root) for name in selected]
    print("scenario           status          evidence")
    print("-----------------  --------------  ------------------------------")
    for item in results:
        evidence = f"final={item['final_content']}"
        if "effect_attempts" in item:
            evidence += f" effect_attempts={item['effect_attempts']}"
        print(f"{item['scenario']:<17}  {item['status']:<14}  {evidence}")
    print(f"\nArtifacts: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
