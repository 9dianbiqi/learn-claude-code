from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import InjectedCrash, Runtime, canonical_args_hash
from agent_runtime.store import EventStore


def _seed_unknown_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[EventStore, str, str]:
    repo = str(tmp_path.resolve())
    store = EventStore(tmp_path / ".agent_runtime" / "runtime.db")
    task_a = "task-a"
    task_b = "task-b"
    messages = [
        {"role": "user", "content": "effect"},
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "old-shell", "name": "bash",
            "input": {"command": "echo old > old-marker.txt"},
        }]},
    ]
    store.create_task(task_a, repo, "effect", "fake")
    store.create_task(task_b, repo, "other", "fake")
    store.save_checkpoint(task_a, "model_responded", messages, {"turn": 0})
    store.create_tool_call(
        task_a,
        "old-shell",
        0,
        "bash",
        {"command": "echo old > old-marker.txt"},
        canonical_args_hash("bash", {"command": "echo old > old-marker.txt"}),
    )
    token_a = store.acquire_lease(repo, task_a, "owner-a", ttl=1)
    assert token_a is not None
    store.bind_lease(repo, "owner-a", token_a)
    store.start_tool_call(task_a, "old-shell", "unknown_write")
    reservation_id = store.reserve_effect(
        task_a, "old-shell", "owner-a", token_a, "unknown_write", owner_pid=999999999
    )
    store.clear_lease()
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE leases SET expires_at = 0 WHERE repo_root = ?", (repo,))
    monkeypatch.setattr(EventStore, "_process_alive", staticmethod(lambda _pid: False))
    token_b = store.acquire_lease(repo, task_b, "owner-b", ttl=1)
    assert token_b is not None and token_b > token_a
    reservation = store.get_effect_reservation(task_a, "old-shell")
    assert reservation is not None and reservation["reservation_id"] == reservation_id
    assert reservation["state"] == "unknown"
    store.release_lease(repo, "owner-b", fencing_token=token_b)
    return store, task_a, task_b


def test_active_owner_blocks_cross_task_takeover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = str(tmp_path.resolve())
    store = EventStore(tmp_path / "runtime.db")
    store.create_task("task-a", repo, "a", "fake")
    store.create_task("task-b", repo, "b", "fake")
    store.create_tool_call("task-a", "call", 0, "bash", {"command": "echo x"}, "hash")
    token_a = store.acquire_lease(repo, "task-a", "owner-a", ttl=1)
    assert token_a is not None
    store.bind_lease(repo, "owner-a", token_a)
    store.start_tool_call("task-a", "call", "unknown_write")
    store.reserve_effect("task-a", "call", "owner-a", token_a, "unknown_write", owner_pid=123456789)
    monkeypatch.setattr(EventStore, "_process_alive", staticmethod(lambda _pid: True))
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE leases SET expires_at = 0 WHERE repo_root = ?", (repo,))
    assert store.acquire_lease(repo, "task-b", "owner-b", ttl=1) is None


def test_dead_owner_becomes_unknown_and_second_task_cannot_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, task_a, _ = _seed_unknown_shell(tmp_path, monkeypatch)
    marker = tmp_path / "new-marker.txt"
    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(tool_calls=[ToolCall("new-shell", "bash", {"command": "echo new > new-marker.txt"})]),
            ModelResponse(text="done"),
        ]),
        store=EventStore(store.path),
        approval_callback=lambda *_: True,
    )
    result = runtime.run("new task effect")
    assert result.status == "needs_review"
    assert not marker.exists()
    new_task = runtime.store.list_tasks()[-1]["task_id"]
    call = runtime.store.get_tool_call(new_task, "new-shell")
    assert call is not None and call["status"] == "needs_review"
    assert runtime.store.get_effect_reservation(new_task, "new-shell") is None
    assert runtime.store.get_effect_reservation(task_a, "old-shell")["state"] == "unknown"


def test_cross_process_unknown_reservation_blocks_new_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, _, task_b = _seed_unknown_shell(tmp_path, monkeypatch)
    code = "\n".join([
        "import json, sys",
        "from agent_runtime.store import EffectBlocked, EventStore",
        "db, repo, task = sys.argv[1:]",
        "s = EventStore(db)",
        "token = s.acquire_lease(repo, task, 'owner-child', ttl=5)",
        "result = {'lease': token is not None, 'blocked': False}",
        "if token is not None:",
        "    s.bind_lease(repo, 'owner-child', token)",
        "    s.create_tool_call(task, 'child-shell', 0, 'bash', {'command': 'echo child'}, 'hash')",
        "    s.start_tool_call(task, 'child-shell', 'unknown_write')",
        "    try:",
        "        s.reserve_effect(task, 'child-shell', 'owner-child', token, 'unknown_write')",
        "    except EffectBlocked:",
        "        result['blocked'] = True",
        "    s.release_lease(repo, 'owner-child', fencing_token=token)",
        "    s.clear_lease()",
        "print(json.dumps(result))",
    ])
    completed = subprocess.run(
        [sys.executable, "-c", code, str(store.path), str(tmp_path.resolve()), task_b],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout.strip())
    assert result == {"lease": True, "blocked": True}
    assert store.get_effect_reservation(task_b, "child-shell") is None


def test_original_task_recovery_is_reconciliation_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, task_a, _ = _seed_unknown_shell(tmp_path, monkeypatch)

    class NoModelCall:
        name = "no-model"

        def complete(self, *_args, **_kwargs):
            raise AssertionError("reconciliation-only recovery called the model")

    recovery = Runtime(tmp_path, NoModelCall(), store=EventStore(store.path), approval_callback=lambda *_: True)
    result = recovery.resume(task_a)
    assert result.status == "needs_review"
    call = recovery.store.get_tool_call(task_a, "old-shell")
    assert call is not None and call["status"] == "needs_review"
    reservation = recovery.store.get_effect_reservation(task_a, "old-shell")
    assert reservation is not None and reservation["state"] == "unknown"
    assert recovery.store.scan_invariants(task_a) == []


@pytest.mark.parametrize("action, expected_state", [("complete", "completed"), ("retry", "completed"), ("abort", "cancelled")])
def test_explicit_resolution_terminates_reservation_and_unblocks_other_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, expected_state: str
):
    store, task_a, _ = _seed_unknown_shell(tmp_path, monkeypatch)
    recovery = Runtime(tmp_path, ScriptedModel([]), store=EventStore(store.path), approval_callback=lambda *_: True)
    assert recovery.resume(task_a).status == "needs_review"

    resolver_model = ScriptedModel([ModelResponse(text="done")])
    resolver = Runtime(tmp_path, resolver_model, store=EventStore(store.path), approval_callback=lambda *_: True)
    result = resolver.resolve_call("old-shell", action)
    assert result.status in {"completed", "aborted"}
    reservations = resolver.store.list_effect_reservations(task_a)
    assert reservations[0]["state"] == expected_state if action != "retry" else reservations[0]["state"] == "cancelled"
    if action == "retry":
        assert len(reservations) == 2 and reservations[-1]["state"] == "completed"

    marker = tmp_path / "after-resolution.txt"
    other = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(tool_calls=[ToolCall("other-shell", "bash", {"command": "echo other > after-resolution.txt"})]),
            ModelResponse(text="done"),
        ]),
        store=EventStore(store.path),
        approval_callback=lambda *_: True,
    )
    other_result = other.run("other effect")
    assert other_result.status == "completed"
    assert marker.exists()


def test_stale_owner_cannot_change_state_after_unknown_takeover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, task_a, _ = _seed_unknown_shell(tmp_path, monkeypatch)
    old_store = EventStore(store.path)
    repo = str(tmp_path.resolve())
    old_store.bind_lease(repo, "owner-a", 1)
    with pytest.raises(Exception):
        old_store.update_task(task_a, status="running")
    assert old_store.get_task(task_a)["status"] == "created"
    assert not any(event["type"] == "stale" for event in old_store.list_events(task_a))


def test_windows_process_liveness_probe_does_not_signal_current_process():
    code = (
        "import json, os, time; "
        "from agent_runtime.store import EventStore; "
        "ok = EventStore._process_alive(os.getpid()); "
        "time.sleep(11.2); "
        "print(json.dumps({'alive': ok}))"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout.strip())["alive"] is True
