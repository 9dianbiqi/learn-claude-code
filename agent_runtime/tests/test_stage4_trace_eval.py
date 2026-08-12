from pathlib import Path

import pytest
import yaml

from agent_runtime.eval_runner import run_suite
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import Runtime
from agent_runtime.trace import TraceReporter


def test_trace_summary_and_jsonl_export_include_usage_and_permissions(tmp_path: Path):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(
                tool_calls=[ToolCall("read", "read_file", {"path": "note.txt"})],
                usage={"input_tokens": 10, "output_tokens": 3},
            ),
            ModelResponse(text="done", usage={"input_tokens": 4, "output_tokens": 2}),
        ]),
    )
    result = runtime.run("read")
    reporter = TraceReporter(runtime.store)

    summary = reporter.summary(result.task_id)
    assert summary["status"] == "completed"
    assert summary["total_input_tokens"] == 14
    assert summary["total_output_tokens"] == 5
    assert summary["tool_calls"] == 1
    assert summary["permission_counts"]["allow"] == 1
    assert summary["duration_seconds"] >= 0

    output = tmp_path / "trace.jsonl"
    count = reporter.export_jsonl(result.task_id, output)
    assert count == summary["event_count"]
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines
    assert '"type": "tool_succeeded"' in output.read_text(encoding="utf-8")


def test_mvp_eval_runner_executes_deterministic_suite(tmp_path: Path):
    suite = {
        "version": 1,
        "name": "test-suite",
        "tasks": [
            {
                "id": "read-task",
                "prompt": "Read note.txt",
                "fixture": {"files": {"note.txt": "hello"}},
                "model": [
                    {"tool": "read_file", "id": "read-1", "input": {"path": "note.txt"}},
                    {"text": "hello"},
                ],
                "checks": {"status": "completed", "files": {"note.txt": "hello"}},
            },
            {
                "id": "write-task",
                "prompt": "Read then edit note.txt",
                "fixture": {"files": {"note.txt": "old"}},
                "model": [
                    {"tool": "read_file", "id": "read-2", "input": {"path": "note.txt"}},
                    {"tool": "edit_file", "id": "edit-2", "input": {"path": "note.txt", "old_text": "old", "new_text": "new"}},
                    {"text": "edited"},
                ],
                "checks": {"status": "completed", "files": {"note.txt": "new"}},
            },
        ],
    }
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")

    report = run_suite(suite_path, tmp_path / "runs")

    assert report["suite"] == "test-suite"
    assert report["total"] == 2
    assert report["passed"] == 2
    assert report["completion_rate"] == 1.0
    assert all(item["passed"] for item in report["tasks"])


def test_eval_task_id_cannot_escape_run_root(tmp_path: Path):
    suite_path = tmp_path / "unsafe.yaml"
    suite_path.write_text(
        yaml.safe_dump({"tasks": [{"id": "../outside", "model": [{"text": "x"}]}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid eval task id"):
        run_suite(suite_path, tmp_path / "runs")
