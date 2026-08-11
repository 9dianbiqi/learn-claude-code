from pathlib import Path

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import Runtime


def test_stage1_read_task_completes_and_persists_trace(tmp_path: Path):
    (tmp_path / "note.txt").write_text("hello runtime", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-read-1",
                        name="read_file",
                        input={"path": "note.txt"},
                    )
                ],
                usage={"input_tokens": 12, "output_tokens": 4},
            ),
            ModelResponse(text="The file says hello runtime.", usage={"input_tokens": 8, "output_tokens": 6}),
        ]
    )

    runtime = Runtime(tmp_path, model=model)
    result = runtime.run("Read note.txt and report its contents.")

    assert result.status == "completed"
    assert result.task_id
    assert model.call_count == 2
    task = runtime.store.get_task(result.task_id)
    assert task["status"] == "completed"
    assert task["checkpoint_id"] is not None

    tool_call = runtime.store.get_tool_call(result.task_id, "call-read-1")
    assert tool_call["status"] == "succeeded"
    assert "hello runtime" in tool_call["output"]
    assert tool_call["permission"] == "allow"

    event_types = [event["type"] for event in runtime.store.list_events(result.task_id)]
    assert "model_response" in event_types
    assert "tool_started" in event_types
    assert "tool_succeeded" in event_types
    assert "task_completed" in event_types


def test_stage1_runtime_can_reopen_existing_task(tmp_path: Path):
    model = ScriptedModel([ModelResponse(text="done")])
    first = Runtime(tmp_path, model=model)
    result = first.run("No tools needed.")

    reopened = Runtime(tmp_path, model=ScriptedModel([]))
    task = reopened.store.get_task(result.task_id)
    assert task["status"] == "completed"
    assert reopened.store.get_checkpoint(task["checkpoint_id"])["task_id"] == result.task_id


def test_stage1_glob_tool_is_available(tmp_path: Path):
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-glob-1",
                        name="glob",
                        input={"pattern": "*.py"},
                    )
                ]
            ),
            ModelResponse(text="found"),
        ]
    )
    runtime = Runtime(tmp_path, model=model)
    result = runtime.run("Find Python files.")

    assert result.status == "completed"
    output = runtime.store.get_tool_call(result.task_id, "call-glob-1")["output"]
    assert "a.py" in output
    assert "b.txt" not in output
