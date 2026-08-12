from pathlib import Path

import pytest
import yaml

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.permissions import PermissionEngine
from agent_runtime.runtime import InjectedCrash, Runtime


def _allow_all(_tool_name, _args, _reason):
    return True


def test_write_requires_approval_and_path_escape_is_denied(tmp_path: Path):
    (tmp_path / "source.txt").write_text("old", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "source.txt"})]),
            ModelResponse(
                tool_calls=[ToolCall("escape", "write_file", {"path": "../outside.txt", "content": "bad"})]
            ),
            ModelResponse(text="blocked"),
        ]
    )
    runtime = Runtime(tmp_path, model=model, approval_callback=_allow_all)
    result = runtime.run("Read and then try an out of bounds write")

    assert result.status == "completed"
    call = runtime.store.get_tool_call(result.task_id, "escape")
    assert call["permission"] == "deny"
    assert call["status"] == "denied"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_noninteractive_ask_pauses_without_writing(tmp_path: Path):
    target = tmp_path / "new.txt"
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("write", "write_file", {"path": "new.txt", "content": "x"})])])
    runtime = Runtime(tmp_path, model=model, interactive=False)

    result = runtime.run("Create new.txt")

    assert result.status == "waiting_approval"
    assert not target.exists()
    call = runtime.store.get_tool_call(result.task_id, "write")
    assert call["status"] == "waiting_approval"


def test_existing_file_edit_requires_prior_read(tmp_path: Path):
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "existing.txt", "old_text": "old", "new_text": "new"})]),
        ]
    )
    runtime = Runtime(tmp_path, model=model, approval_callback=_allow_all)

    result = runtime.run("Edit existing.txt")

    assert result.status == "needs_review"
    assert target.read_text(encoding="utf-8") == "old"
    call = runtime.store.get_tool_call(result.task_id, "edit")
    assert call["status"] == "needs_review"
    assert "read-before-edit" in (call["error"] or "")


def test_hash_conflict_never_overwrites_external_change(tmp_path: Path):
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "existing.txt"})]),
            ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "existing.txt", "old_text": "old", "new_text": "agent"})]),
        ]
    )

    def mutate_after_read(point, tool_use_id=None, **_):
        if point == "after_tool_persist" and tool_use_id == "read":
            target.write_text("external", encoding="utf-8")

    runtime = Runtime(tmp_path, model=model, approval_callback=_allow_all, fault_injector=mutate_after_read)
    result = runtime.run("Read then edit")

    assert result.status == "needs_review"
    assert target.read_text(encoding="utf-8") == "external"
    call = runtime.store.get_tool_call(result.task_id, "edit")
    assert call["status"] == "needs_review"
    assert "hash" in (call["error"] or "").lower()


def test_dangerous_shell_is_denied_before_execution(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("danger", "bash", {"command": "Remove-Item -Recurse marker.txt"})]),
            ModelResponse(text="done"),
        ]
    )
    runtime = Runtime(tmp_path, model=model, approval_callback=_allow_all)
    result = runtime.run("Run command")

    assert result.status == "completed"
    call = runtime.store.get_tool_call(result.task_id, "danger")
    assert call["permission"] == "deny"
    assert call["status"] == "denied"
    assert not marker.exists()


def test_file_write_recovery_reconciles_post_hash_without_repeating_write(tmp_path: Path):
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    first_model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "existing.txt"})]),
            ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "existing.txt", "old_text": "old", "new_text": "new"})]),
        ]
    )

    def crash_after_write(point, tool_use_id=None, **_):
        if point == "after_tool_effect_before_persist" and tool_use_id == "edit":
            from agent_runtime.runtime import InjectedCrash

            raise InjectedCrash(point)

    first = Runtime(tmp_path, first_model, approval_callback=_allow_all, fault_injector=crash_after_write)
    with pytest.raises(InjectedCrash):
        first.run("Read and edit")
    task_id = first.store.list_tasks()[0]["task_id"]

    second = Runtime(tmp_path, ScriptedModel([ModelResponse(text="recovered")]), approval_callback=_allow_all)
    result = second.resume(task_id)

    assert result.status == "completed"
    assert target.read_text(encoding="utf-8") == "new"
    events = second.store.list_events(task_id)
    assert sum(event["type"] == "tool_recovered_succeeded" for event in events) == 1
    assert sum(event["type"] == "tool_succeeded" and event["payload"].get("tool_use_id") == "edit" for event in events) == 0
    observation = second.store.get_file_observation(task_id, str(target.resolve()))
    assert observation["sha256"] == second.tools.file_state("existing.txt")["sha256"]


def test_file_write_recovery_retries_when_before_hash_remains(tmp_path: Path):
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    first_model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "existing.txt"})]),
            ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "existing.txt", "old_text": "old", "new_text": "new"})]),
        ]
    )

    def crash_before_write(point, tool_use_id=None, **_):
        if point == "after_tool_started" and tool_use_id == "edit":
            from agent_runtime.runtime import InjectedCrash

            raise InjectedCrash(point)

    first = Runtime(tmp_path, first_model, approval_callback=_allow_all, fault_injector=crash_before_write)
    with pytest.raises(InjectedCrash):
        first.run("Read and edit")
    task_id = first.store.list_tasks()[0]["task_id"]

    second = Runtime(tmp_path, ScriptedModel([ModelResponse(text="retried")]), approval_callback=_allow_all)
    result = second.resume(task_id)

    assert result.status == "completed"
    assert target.read_text(encoding="utf-8") == "new"
    call = second.store.get_tool_call(task_id, "edit")
    assert call["status"] == "succeeded"
    events = second.store.list_events(task_id)
    assert sum(event["type"] == "tool_started" and event["payload"].get("tool_use_id") == "edit" for event in events) == 2


def test_interrupted_unknown_shell_is_not_replayed_automatically(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("shell", "bash", {"command": "echo x > marker.txt"})])])

    def crash_after_shell(point, tool_use_id=None, **_):
        if point == "after_tool_effect_before_persist" and tool_use_id == "shell":
            from agent_runtime.runtime import InjectedCrash

            raise InjectedCrash(point)

    runtime = Runtime(tmp_path, model, approval_callback=_allow_all, fault_injector=crash_after_shell)
    with pytest.raises(InjectedCrash):
        runtime.run("Write marker using shell")
    task_id = runtime.store.list_tasks()[0]["task_id"]

    resumed = Runtime(tmp_path, ScriptedModel([ModelResponse(text="review")]), approval_callback=_allow_all)
    result = resumed.resume(task_id)

    assert result.status == "needs_review"
    assert marker.read_text(encoding="utf-8").strip() == "x"
    events = resumed.store.list_events(task_id)
    assert sum(event["type"] == "tool_started" and event["payload"].get("tool_use_id") == "shell" for event in events) == 1


def test_policy_file_uses_deny_over_allow_and_rejects_invalid_effect(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {"id": "allow-echo", "effect": "allow", "tools": ["bash"], "command_regex": r"echo safe"},
                    {"id": "deny-echo", "effect": "deny", "tools": ["bash"], "command_regex": r"echo blocked"},
                ]
            }
        ),
        encoding="utf-8",
    )
    engine = PermissionEngine(tmp_path, policy)
    assert engine.evaluate("bash", {"command": "echo safe"}).effect == "allow"
    assert engine.evaluate("bash", {"command": "echo blocked"}).effect == "deny"

    bad = tmp_path / "bad-policy.yaml"
    bad.write_text("rules:\n  - id: bad\n    effect: maybe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid permission effect"):
        PermissionEngine(tmp_path, bad)


def test_file_change_between_precheck_and_write_is_blocked(tmp_path: Path):
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "existing.txt"})]),
            ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "existing.txt", "old_text": "old", "new_text": "agent"})]),
        ]
    )

    def mutate_after_start(point, tool_use_id=None, **_):
        if point == "after_tool_started" and tool_use_id == "edit":
            target.write_text("external", encoding="utf-8")

    result = Runtime(tmp_path, model, approval_callback=_allow_all, fault_injector=mutate_after_start).run("edit")
    assert result.status == "needs_review"
    assert target.read_text(encoding="utf-8") == "external"
