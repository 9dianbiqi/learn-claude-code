from __future__ import annotations

import threading
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

from agent_runtime import eval_runner
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.permissions import PermissionEngine
from agent_runtime.runtime import InjectedCrash, LeaseLost, Runtime
from agent_runtime.store import EventStore


def _crash_at(point: str):
    def inject(actual: str, **_: object) -> None:
        if actual == point:
            raise InjectedCrash(actual)

    return inject


def _policy(tmp_path: Path, rules: list[dict]) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"rules": rules}), encoding="utf-8")
    return path


def test_stale_owner_has_zero_task_store_writes_and_cannot_release_new_owner(tmp_path: Path):
    repo = str(tmp_path.resolve())
    store = EventStore(tmp_path / "runtime.db")
    task_a = "task-a"
    task_b = "task-b"
    store.create_task(task_a, repo, "a", "fake")
    store.create_task(task_b, repo, "b", "fake")
    store.save_checkpoint(task_a, "input_ready", [{"role": "user", "content": "a"}], {"turn": 0})
    store.create_tool_call(task_a, "call", 0, "read_file", {"path": "x"}, "hash", effect="read_only")
    token_a = store.acquire_lease(repo, task_a, "owner-a", ttl=0.01)
    assert token_a is not None
    store.bind_lease(repo, "owner-a", token_a)
    time.sleep(0.03)
    token_b = store.acquire_lease(repo, task_b, "owner-b", ttl=1)
    assert token_b is not None and token_b > token_a

    for operation in (
        lambda: store.update_task(task_a, status="running"),
        lambda: store.update_tool_call(task_a, "call", status="succeeded"),
        lambda: store.append_event(task_a, "stale"),
        lambda: store.save_checkpoint(task_a, "model_responded", [], {"turn": 1}),
    ):
        with pytest.raises(LeaseLost):
            operation()
    store.release_lease(repo, "owner-a", fencing_token=token_a)

    lease = store._fetchone("SELECT * FROM leases WHERE repo_root = ?", (repo,))
    assert lease is not None
    assert lease["owner_id"] == "owner-b"
    assert int(lease["fencing_token"]) == token_b
    assert not any(event["type"] == "stale" for event in store.list_events(task_a))


def test_takeover_is_blocked_while_effect_reservation_is_in_flight(tmp_path: Path):
    marker = tmp_path / "marker.txt"
    started = threading.Event()
    release_effect = threading.Event()
    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(tool_calls=[ToolCall("shell", "bash", {"command": "echo x"})])]),
        owner_id="owner-a",
        lease_ttl=5.0,
        approval_callback=lambda *_: True,
    )

    def blocked_execute(name: str, args: dict):
        started.set()
        assert release_effect.wait(10)
        marker.write_text(marker.read_text(encoding="utf-8") + "x\n" if marker.exists() else "x\n", encoding="utf-8")
        return "ok"

    first.tools.execute = blocked_execute
    result_box: dict[str, object] = {}

    def run_first() -> None:
        try:
            result_box["result"] = first.run("effect")
        except Exception as exc:  # noqa: BLE001 - stale owner is the assertion
            result_box["error"] = exc

    thread = threading.Thread(target=run_first, daemon=True)
    thread.start()
    assert started.wait(10)
    task_a = first.store.list_tasks()[0]["task_id"]
    second_store = EventStore(first.store.path)
    second_store.create_task("task-b", str(tmp_path.resolve()), "b", "fake")
    with sqlite3.connect(first.store.path) as conn:
        conn.execute("UPDATE leases SET expires_at = 0 WHERE repo_root = ?", (str(tmp_path.resolve()),))
    try:
        assert second_store.acquire_lease(str(tmp_path.resolve()), "task-b", "owner-b", ttl=1) is None
    finally:
        release_effect.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert isinstance(result_box.get("error"), LeaseLost)
    reservation = second_store.get_effect_reservation(task_a, "shell")
    assert reservation is not None
    assert reservation["state"] == "unknown"
    assert marker.read_text(encoding="utf-8").splitlines() == ["x"]
    token_b = second_store.acquire_lease(str(tmp_path.resolve()), "task-b", "owner-b", ttl=1)
    assert token_b is not None
    second_store.release_lease(str(tmp_path.resolve()), "owner-b", fencing_token=token_b)
    second_store.clear_lease()
    first.store.clear_lease()


@pytest.mark.parametrize(
    "command",
    [
        "echo x | cat",
        "echo x > marker.txt",
        "echo x && echo y",
        "echo x || echo y",
        "echo x; echo y",
        "echo $(whoami)",
        "powershell -Command \"echo x | cat\"",
        "type ..\\outside.txt",
    ],
)
def test_explicit_deny_is_not_downgraded_by_shell_safety(tmp_path: Path, command: str):
    policy = _policy(tmp_path, [{"id": "deny-all-shell", "effect": "deny", "tools": ["bash"], "command_regex": ".*"}])
    engine = PermissionEngine(tmp_path, policy)
    decision = engine.evaluate("bash", {"command": command})
    assert decision.effect == "deny"
    assert decision.rule_id == "deny-all-shell" or decision.rule_id.startswith("invariant.")

    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(tool_calls=[ToolCall("call", "bash", {"command": command})]), ModelResponse(text="done")]),
        policy_path=policy,
        approval_callback=lambda *_: True,
    )
    result = runtime.run("deny")
    assert result.status == "completed"
    call = runtime.store.get_tool_call(result.task_id, "call")
    assert call["status"] == "denied"
    assert call["execution_attempts"] == 0


@pytest.mark.parametrize(
    "command",
    [
        "type ../outside.txt",
        "type ./../outside.txt",
        "type foo/../outside.txt",
        "type a/../../outside.txt",
        r"type .\..\outside.txt",
        r"type foo\..\outside.txt",
        r"type a\..\..\outside.txt",
        'type "foo/../outside.txt"',
        "echo x > foo/../outside.txt",
        "echo x > .\\..\\outside.txt",
    ],
)
def test_shell_parent_segments_fail_closed_without_execution(tmp_path: Path, command: str):
    outside = tmp_path.parent / "outside.txt"
    outside.unlink(missing_ok=True)
    try:
        runtime = Runtime(
            tmp_path,
            ScriptedModel([ModelResponse(tool_calls=[ToolCall("call", "bash", {"command": command})]), ModelResponse(text="done")]),
            approval_callback=lambda *_: True,
        )
        result = runtime.run("escape")
        assert result.status == "completed"
        call = runtime.store.get_tool_call(result.task_id, "call")
        assert call["permission"] == "deny"
        assert call["execution_attempts"] == 0
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.parametrize("point", ["review_after_checkpoint", "review_after_status", "review_after_event"])
def test_review_transition_faults_roll_back_all_review_projection(tmp_path: Path, point: str):
    store = EventStore(tmp_path / "runtime.db")
    task_id = "task-review"
    store.create_task(task_id, str(tmp_path.resolve()), "review", "fake")
    checkpoint_id = store.save_checkpoint(task_id, "model_responded", [{"role": "user", "content": "x"}], {"turn": 0})
    store.create_tool_call(task_id, "call", 0, "bash", {"command": "echo x"}, "hash")
    store.bind_lease(str(tmp_path.resolve()), "owner", store.acquire_lease(str(tmp_path.resolve()), task_id, "owner", ttl=1))
    with pytest.raises(InjectedCrash):
        store.transition_review(
            task_id,
            "call",
            "needs_review",
            [{"role": "user", "content": "x"}],
            {"turn": 0},
            "unknown effect",
            fault_injector=_crash_at(point),
        )
    task = store.get_task(task_id)
    call = store.get_tool_call(task_id, "call")
    assert task["status"] == "created"
    assert task["checkpoint_id"] == checkpoint_id
    assert call["status"] == "planned"
    assert not any(event["type"] == "task_needs_review" for event in store.list_events(task_id))
    assert store.scan_invariants(task_id) == []


def test_eval_reparse_root_is_rejected_before_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"name": "reparse", "tasks": []}), encoding="utf-8")
    run_root = tmp_path / "runs"
    original = eval_runner._is_reparse_or_symlink

    def synthetic(path: Path) -> bool:
        return Path(path) == run_root or original(path)

    monkeypatch.setattr(eval_runner, "_is_reparse_or_symlink", synthetic)
    with pytest.raises(ValueError, match="reparse|symlink"):
        eval_runner.run_suite(suite, run_root)


def test_eval_symlink_root_is_rejected_when_platform_allows_it(tmp_path: Path):
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"name": "symlink", "tasks": []}), encoding="utf-8")
    real_root = tmp_path / "real-runs"
    real_root.mkdir()
    link_root = tmp_path / "linked-runs"
    try:
        link_root.symlink_to(real_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires unavailable platform privilege")
    with pytest.raises(ValueError, match="reparse|symlink"):
        eval_runner.run_suite(suite, link_root)


def test_dead_owner_reservation_is_marked_unknown_before_takeover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = str(tmp_path.resolve())
    store = EventStore(tmp_path / "runtime.db")
    store.create_task("task-a", repo, "a", "fake")
    store.create_task("task-b", repo, "b", "fake")
    store.create_tool_call("task-a", "call", 0, "bash", {"command": "echo x"}, "hash")
    token_a = store.acquire_lease(repo, "task-a", "owner-a", ttl=1)
    assert token_a is not None
    store.bind_lease(repo, "owner-a", token_a)
    store.start_tool_call("task-a", "call", "unknown_write")
    reservation_id = store.reserve_effect("task-a", "call", "owner-a", token_a, "unknown_write")
    store.clear_lease()
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE leases SET expires_at = 0 WHERE repo_root = ?", (repo,))
    monkeypatch.setattr(EventStore, "_process_alive", staticmethod(lambda _pid: False))
    token_b = store.acquire_lease(repo, "task-b", "owner-b", ttl=1)
    assert token_b is not None and token_b > token_a
    reservation = store.get_effect_reservation("task-a", "call")
    assert reservation is not None and reservation["reservation_id"] == reservation_id
    assert reservation["state"] == "unknown"
    assert reservation["details"]["reason"] == "owner_crashed"


def test_completed_file_reservation_never_retries_when_state_reverts(tmp_path: Path):
    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")
    first = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "note.txt"})]),
            ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "note.txt", "old_text": "old", "new_text": "new"})]),
        ]),
        approval_callback=lambda *_: True,
    )
    original_complete = first.store.complete_effect

    def crash_after_completion(reservation_id: int, task_id: str, tool_use_id: str, tool_fields: dict,
                               details=None, observation=None, event_payload=None):
        original_complete(reservation_id, task_id, tool_use_id, tool_fields, details, observation, event_payload)
        if tool_fields.get("status") == "succeeded":
            raise InjectedCrash("after_reservation_completed")
        return None

    first.store.complete_effect = crash_after_completion  # type: ignore[method-assign]
    with pytest.raises(InjectedCrash):
        first.run("read and edit")
    task_id = first.store.list_tasks()[0]["task_id"]
    target.write_text("old", encoding="utf-8")
    resumed = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]), approval_callback=lambda *_: True)
    assert resumed.resume(task_id).status == "completed"
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize("mutation", ["task_review_model", "running_review_checkpoint"])
def test_invariant_scan_detects_review_projection_mismatch(tmp_path: Path, mutation: str):
    store = EventStore(tmp_path / "runtime.db")
    task_id = "task-mismatch"
    store.create_task(task_id, str(tmp_path.resolve()), "x", "fake")
    model_checkpoint_id = store.save_checkpoint(task_id, "model_responded", [{"role": "user", "content": "x"}], {"turn": 0})
    store.create_tool_call(task_id, "call", 0, "bash", {"command": "echo x"}, "hash")
    if mutation == "task_review_model":
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE tasks SET status = 'needs_review', checkpoint_id = ? WHERE task_id = ?", (model_checkpoint_id, task_id))
    else:
        review_checkpoint_id = store.save_checkpoint(task_id, "needs_review", [{"role": "user", "content": "x"}], {"turn": 0})
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE tasks SET status = 'running', checkpoint_id = ? WHERE task_id = ?", (review_checkpoint_id, task_id))
    violations = store.scan_invariants(task_id)
    assert violations
    assert any("review" in item or "checkpoint" in item for item in violations)
