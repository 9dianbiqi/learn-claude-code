from pathlib import Path

import pytest

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import InjectedCrash, Runtime


def _read_then_finish():
    return [
        ModelResponse(tool_calls=[ToolCall("read-1", "read_file", {"path": "note.txt"})]),
        ModelResponse(text="finished"),
    ]


def _finish_only():
    return [ModelResponse(text="finished")]


def test_resume_retries_read_only_call_left_running(tmp_path: Path):
    (tmp_path / "note.txt").write_text("safe", encoding="utf-8")
    first = Runtime(
        tmp_path,
        model=ScriptedModel(_read_then_finish()),
        fault_injector=lambda point, **_: (_ for _ in ()).throw(InjectedCrash(point))
        if point == "after_tool_effect_before_persist"
        else None,
    )

    with pytest.raises(InjectedCrash):
        first.run("Read and finish")

    task_id = first.store.list_tasks()[0]["task_id"]

    second = Runtime(tmp_path, model=ScriptedModel(_finish_only()))
    result = second.resume(task_id)

    assert result.status == "completed"
    events = second.store.list_events(task_id)
    assert sum(event["type"] == "tool_succeeded" for event in events) == 1
    assert sum(event["type"] == "tool_started" for event in events) == 2


def test_resume_deduplicates_call_persisted_before_checkpoint(tmp_path: Path):
    (tmp_path / "note.txt").write_text("safe", encoding="utf-8")

    def crash_after_persist(point, **_):
        if point == "after_tool_persist":
            raise InjectedCrash(point)

    first = Runtime(tmp_path, ScriptedModel(_read_then_finish()), fault_injector=crash_after_persist)
    with pytest.raises(InjectedCrash):
        first.run("Read and finish")
    task_id = first.store.list_tasks()[0]["task_id"]

    second = Runtime(tmp_path, ScriptedModel(_finish_only()))
    result = second.resume(task_id)

    assert result.status == "completed"
    events = second.store.list_events(task_id)
    assert sum(event["type"] == "tool_succeeded" for event in events) == 1
    assert sum(event["type"] == "tool_deduplicated" for event in events) == 1
    call = second.store.get_tool_call(task_id, "read-1")
    assert call["status"] == "succeeded"


def test_reused_tool_id_with_different_arguments_fails_closed(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    first_model = ScriptedModel(
        [ModelResponse(tool_calls=[ToolCall("same-id", "read_file", {"path": "a.txt"})])]
    )
    first = Runtime(tmp_path, first_model)

    # Persist the first call, then interrupt before the tool result checkpoint.
    def crash_after_persist(point, **_):
        if point == "after_tool_persist":
            raise InjectedCrash(point)

    first.fault_injector = crash_after_persist
    with pytest.raises(InjectedCrash):
        first.run("read")
    task_id = first.store.list_tasks()[0]["task_id"]

    second = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(tool_calls=[ToolCall("same-id", "read_file", {"path": "other.txt"})])]),
    )
    result = second.resume(task_id)
    assert result.status == "failed"
    assert "different arguments" in (result.error or "")


def test_active_repository_lease_blocks_second_runtime(tmp_path: Path):
    first = Runtime(tmp_path, ScriptedModel([]), owner_id="owner-a")
    task_id = "task-held"
    first.store.create_task(task_id, str(tmp_path.resolve()), "held", "fake")
    assert first.store.acquire_lease(str(tmp_path.resolve()), task_id, "owner-a", ttl=60)

    second = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]), owner_id="owner-b")
    with pytest.raises(RuntimeError, match="leased"):
        second.run("must wait")
