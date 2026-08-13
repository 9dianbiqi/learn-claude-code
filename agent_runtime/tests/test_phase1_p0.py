from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest
import yaml

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.permissions import PermissionEngine
from agent_runtime.runtime import InjectedCrash, Runtime, canonical_args_hash
from agent_runtime.store import EventStore
import agent_runtime.store as store_module

LeaseLost = getattr(store_module, "LeaseLost", RuntimeError)


def _crash_at(expected: str):
    def inject(point: str, **_):
        if point == expected:
            raise InjectedCrash(point)

    return inject


def _expire_lease(runtime: Runtime) -> None:
    """Inject lease expiry without making a test depend on wall-clock sleep."""
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = 0 WHERE repo_root = ?",
            (str(runtime.repo_root),),
        )


@pytest.mark.parametrize(
    "point",
    [
        "bootstrap_after_task_insert",
        "bootstrap_after_checkpoint_insert",
        "bootstrap_after_task_event",
    ],
)
def test_bootstrap_boundaries_roll_back_as_one_transaction(tmp_path: Path, point: str):
    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done")]),
        fault_injector=_crash_at(point),
    )

    with pytest.raises(InjectedCrash):
        runtime.run("bootstrap crash")

    assert runtime.store.list_tasks() == []

    recovered = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]))
    result = recovered.run("retry bootstrap")
    assert result.status == "completed"
    tasks = recovered.store.list_tasks()
    assert len(tasks) == 1
    events = recovered.store.list_events(result.task_id)
    assert [event["type"] for event in events].count("task_created") == 1
    assert [event["type"] for event in events].count("checkpoint_saved") == 3


def _recover_interrupted_shell(tmp_path: Path) -> tuple[Runtime, str, Path]:
    marker = tmp_path / "marker.txt"
    first = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            "shell-abort",
                            "bash",
                            {"command": "echo x >> marker.txt"},
                        )
                    ]
                )
            ]
        ),
        approval_callback=lambda *_: True,
        fault_injector=_crash_at("after_tool_effect_before_persist"),
    )
    with pytest.raises(InjectedCrash):
        first.run("shell then abort")

    task_id = first.store.list_tasks()[0]["task_id"]
    recovery = Runtime(tmp_path, ScriptedModel([]), approval_callback=lambda *_: True)
    result = recovery.resume(task_id)
    assert result.status == "needs_review"
    return recovery, task_id, marker


def test_abort_is_terminal_and_does_not_replay_shell_effect(tmp_path: Path):
    runtime, task_id, marker = _recover_interrupted_shell(tmp_path)

    result = runtime.resolve_call("shell-abort", "abort")
    assert result.status == "aborted"
    assert runtime.store.get_task(task_id)["status"] == "aborted"
    assert runtime.store.get_tool_call(task_id, "shell-abort")["status"] == "aborted"
    assert runtime.store.get_checkpoint(runtime.store.get_task(task_id)["checkpoint_id"])["phase"] == "aborted"

    with pytest.raises(RuntimeError, match="terminal"):
        Runtime(tmp_path, ScriptedModel([]), approval_callback=lambda *_: True).resume(task_id)
    assert [line.rstrip() for line in marker.read_text(encoding="utf-8").splitlines()] == ["x"]


def test_completed_resume_is_rejected(tmp_path: Path):
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]))
    result = runtime.run("finish")

    with pytest.raises(RuntimeError, match="terminal"):
        runtime.resume(result.task_id)


def test_file_abort_is_terminal_and_does_not_change_effect_counter(tmp_path: Path):
    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")
    first = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "note.txt"})]),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            "edit-abort",
                            "edit_file",
                            {"path": "note.txt", "old_text": "old", "new_text": "new"},
                        )
                    ]
                ),
            ]
        ),
        approval_callback=lambda *_: True,
        fault_injector=lambda point, tool_use_id=None, **_: (
            (_ for _ in ()).throw(InjectedCrash(point))
            if point == "after_tool_effect_before_persist" and tool_use_id == "edit-abort"
            else None
        ),
    )
    with pytest.raises(InjectedCrash):
        first.run("read and edit")
    task_id = first.store.list_tasks()[0]["task_id"]

    target.write_text("external", encoding="utf-8")
    recovery = Runtime(tmp_path, ScriptedModel([]), approval_callback=lambda *_: True)
    assert recovery.resume(task_id).status == "needs_review"
    assert target.read_text(encoding="utf-8") == "external"
    assert recovery.resolve_call("edit-abort", "abort").status == "aborted"
    with pytest.raises(RuntimeError, match="terminal"):
        Runtime(tmp_path, ScriptedModel([]), approval_callback=lambda *_: True).resume(task_id)
    assert target.read_text(encoding="utf-8") == "external"


def test_aborted_tool_call_cannot_enter_execute_path(tmp_path: Path):
    runtime = Runtime(tmp_path, ScriptedModel([]))
    task_id = "task-aborted-call"
    runtime.store.create_task(task_id, str(tmp_path.resolve()), "aborted", "fake")
    runtime.store.save_checkpoint(task_id, "needs_review", [{"role": "user", "content": "x"}], {"turn": 0})
    args = {"command": "echo x"}
    runtime.store.create_tool_call(task_id, "aborted-call", 0, "bash", args, canonical_args_hash("bash", args))
    runtime.store.update_tool_call(task_id, "aborted-call", status="aborted")
    with pytest.raises(RuntimeError, match="Terminal tool call"):
        runtime._execute_call(task_id, 0, ToolCall("aborted-call", "bash", {"command": "echo x"}))


def test_fencing_rejects_stale_owner_writes_and_tool_execution(tmp_path: Path):
    started = threading.Event()
    release_model = threading.Event()

    class SlowModel:
        name = "slow"
        timeout = 0.05

        def complete(self, messages, tools):
            started.set()
            release_model.wait(10)
            return ModelResponse(text="old-owner")

    first = Runtime(tmp_path, SlowModel(), owner_id="owner-a", lease_ttl=0.1)
    task_id = "task-stale-model-owner"
    messages = [{"role": "user", "content": "first"}]
    first.store.bootstrap_task(
        task_id,
        str(first.repo_root),
        "first",
        first.model_name,
        messages,
        {"turn": 0},
    )
    first._acquire(task_id)
    first_result: dict[str, object] = {}

    def run_first():
        try:
            first_result["result"] = first._run_task(task_id, messages, turn=0, lease_acquired=True)
        except Exception as exc:  # noqa: BLE001 - assertion covers stale-owner failure
            first_result["error"] = exc

    thread = threading.Thread(target=run_first)
    thread.start()
    assert started.wait(10)
    _expire_lease(first)

    second = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="new-owner")]),
        owner_id="owner-b",
        lease_ttl=0.5,
    )
    try:
        second_result = second.run("second")
    finally:
        release_model.set()
    thread.join(timeout=10)

    assert second_result.status == "completed"
    assert not thread.is_alive()
    assert isinstance(first_result.get("error"), LeaseLost)


def test_stale_owner_is_fenced_around_tool_effect(tmp_path: Path):
    (tmp_path / "note.txt").write_text("safe", encoding="utf-8")
    effect_started = threading.Event()
    release_effect = threading.Event()

    def mark_effect_boundary(point: str, **_: object) -> None:
        if point == "after_tool_started":
            effect_started.set()

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(tool_calls=[ToolCall("slow-read", "read_file", {"path": "note.txt"})])]),
        owner_id="owner-a",
        lease_ttl=0.5,
        fault_injector=mark_effect_boundary,
    )
    task_id = "task-stale-effect-owner"
    messages = [{"role": "user", "content": "slow read"}]
    first.store.bootstrap_task(
        task_id,
        str(first.repo_root),
        "slow read",
        first.model_name,
        messages,
        {"turn": 0},
    )
    first._acquire(task_id)
    original_execute = first.tools.execute

    def slow_execute(name: str, args: dict):
        release_effect.wait(10)
        return original_execute(name, args)

    first.tools.execute = slow_execute
    first_result: dict[str, object] = {}

    def run_first():
        try:
            first_result["result"] = first._run_task(task_id, messages, turn=0, lease_acquired=True)
        except Exception as exc:  # noqa: BLE001 - assertion covers stale-owner failure
            first_result["error"] = exc

    thread = threading.Thread(target=run_first)
    thread.start()
    assert effect_started.wait(10)
    _expire_lease(first)

    second = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="new-owner")]),
        owner_id="owner-b",
        lease_ttl=0.5,
    )
    try:
        assert second.run("take over").status == "completed"
    finally:
        release_effect.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(first_result.get("error"), LeaseLost)
    assert first.store.get_tool_call(task_id, "slow-read")["status"] == "running"


def test_stale_heartbeat_and_release_cannot_touch_new_lease(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    task_a = "task-a"
    task_b = "task-b"
    store.create_task(task_a, str(tmp_path.resolve()), "a", "fake")
    store.create_task(task_b, str(tmp_path.resolve()), "b", "fake")
    repo = str(tmp_path.resolve())
    token_a = store.acquire_lease(repo, task_a, "owner-a", ttl=0.05)
    assert token_a
    time.sleep(0.08)
    token_b = store.acquire_lease(repo, task_b, "owner-b", ttl=1)
    assert token_b and token_b != token_a

    assert not store.heartbeat_lease(repo, "owner-a", ttl=1, fencing_token=token_a)
    store.release_lease(repo, "owner-a", fencing_token=token_a)
    lease = store._fetchone("SELECT * FROM leases WHERE repo_root = ?", (repo,))
    assert lease["owner_id"] == "owner-b"
    assert lease["fencing_token"] == token_b


def test_invalid_fencing_context_fails_closed_for_state_writes(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    task_id = "task-fence"
    repo = str(tmp_path.resolve())
    store.create_task(task_id, repo, "fence", "fake")
    token = store.acquire_lease(repo, task_id, "owner", ttl=1)
    assert token
    store.bind_lease(repo, "owner", token + 1)
    with pytest.raises(LeaseLost):
        store.update_task(task_id, status="running")
    store.clear_lease()
    store.release_lease(repo, "owner", fencing_token=token)


def test_model_timeout_must_leave_lease_margin(tmp_path: Path):
    class ConfiguredModel:
        name = "configured"
        timeout = 1.0

        def complete(self, messages, tools):
            return ModelResponse(text="done")

    with pytest.raises(ValueError, match="smaller than lease TTL"):
        Runtime(tmp_path, ConfiguredModel(), lease_ttl=1.0)


def _write_policy(tmp_path: Path, rules: list[dict]) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"rules": rules}), encoding="utf-8")
    return path


def test_path_only_rule_is_rejected_instead_of_allowing_shell(tmp_path: Path):
    policy = _write_policy(
        tmp_path,
        [{"id": "path-allow", "effect": "allow", "paths": ["src/**"]}],
    )
    with pytest.raises(ValueError, match="tools"):
        PermissionEngine(tmp_path, policy)


def test_command_only_rule_is_rejected_instead_of_affecting_files(tmp_path: Path):
    policy = _write_policy(
        tmp_path,
        [{"id": "shell-allow", "effect": "allow", "command_regex": "echo safe"}],
    )
    with pytest.raises(ValueError, match="tools"):
        PermissionEngine(tmp_path, policy)


def test_glob_path_restriction_is_effective_and_fail_closed(tmp_path: Path):
    policy = _write_policy(
        tmp_path,
        [{"id": "src-only", "effect": "allow", "tools": ["glob"], "paths": ["src/**"]}],
    )
    engine = PermissionEngine(tmp_path, policy)
    assert engine.evaluate("glob", {"pattern": "src/*.py"}).effect == "allow"
    assert engine.evaluate("glob", {"pattern": "*.py"}).effect == "deny"


def test_explicit_wildcard_rule_is_allowed_but_empty_selector_is_not(tmp_path: Path):
    wildcard = _write_policy(
        tmp_path,
        [{"id": "all-read", "effect": "allow", "tools": ["*"]}],
    )
    assert PermissionEngine(tmp_path, wildcard).evaluate("bash", {"command": "echo x"}).effect == "allow"

    empty = _write_policy(
        tmp_path,
        [{"id": "empty", "effect": "allow", "tools": []}],
    )
    with pytest.raises(ValueError, match="tools"):
        PermissionEngine(tmp_path, empty)


def test_internal_namespace_is_denied_for_file_and_glob_tools(tmp_path: Path):
    (tmp_path / ".agent_runtime").mkdir()
    (tmp_path / ".agent_runtime" / "runtime.db").write_text("secret", encoding="utf-8")
    engine = PermissionEngine(tmp_path)

    assert engine.evaluate("read_file", {"path": ".agent_runtime/runtime.db"}).effect == "deny"
    assert engine.evaluate("write_file", {"path": ".agent_runtime/runtime.db", "content": "x"}).effect == "deny"
    assert engine.evaluate("glob", {"pattern": ".agent_runtime/**"}).effect == "deny"
    assert engine.evaluate("glob", {"pattern": "**/*.db"}).effect == "deny"
    assert engine.evaluate("bash", {"command": "del .agent_runtime\\runtime.db"}).effect == "deny"


def test_runtime_cannot_read_internal_db_or_cross_task_state(tmp_path: Path):
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall("internal-read", "read_file", {"path": ".agent_runtime/runtime.db"})
                ]
            ),
            ModelResponse(text="blocked"),
        ]
    )
    runtime = Runtime(tmp_path, model=model)
    result = runtime.run("attempt internal read")

    assert result.status == "completed"
    call = runtime.store.get_tool_call(result.task_id, "internal-read")
    assert call["status"] == "denied"
    assert call["permission"] == "deny"
