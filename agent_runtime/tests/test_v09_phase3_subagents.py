from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.cli import build_parser, main
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import MAX_SUBAGENT_DEPTH, Runtime
from agent_runtime.store import EventStore, StaleState


def _new_store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "runtime.db")


def _seed_task(store: EventStore, task_id: str) -> None:
    try:
        store.get_task(task_id)
        return
    except KeyError:
        pass
    repo = str(store.path.parent)
    store.bootstrap_task(
        task_id,
        repo,
        "prompt",
        "fake",
        [{"role": "user", "content": "prompt"}],
        {"turn": 0},
    )


def _seed_subagent_run(store: EventStore, *, run_id: str, child: str,
                       parent: str = "task-parent") -> dict:
    repo = str(store.path.parent)
    if parent is not None:
        _seed_task(store, parent)
    _seed_task(store, child)
    return store.create_subagent_run(
        run_id,
        parent,
        child,
        repo,
        "assistant",
        "owner-a",
        [{"role": "user", "content": "prompt"}],
    )


def test_subagent_run_transition_cas_guards(tmp_path: Path):
    store = _new_store(tmp_path)

    _seed_subagent_run(store, run_id="run-complete", child="task-child-complete")
    claimed = store.claim_subagent_run("run-complete", "owner-a", "tok-1")
    with pytest.raises(StaleState):
        store.claim_subagent_run("run-complete", "owner-b", "tok-other")
    with pytest.raises(StaleState):
        store.complete_subagent_run(
            "run-complete", "ok", [], int(claimed["version"]) + 1, "tok-1"
        )
    with pytest.raises(StaleState):
        store.complete_subagent_run(
            "run-complete", "ok", [], int(claimed["version"]), "wrong-token"
        )
    completed = store.complete_subagent_run(
        "run-complete", "ok", [], int(claimed["version"]), "tok-1"
    )
    assert completed["status"] == "completed"
    with pytest.raises(StaleState):
        store.complete_subagent_run(
            "run-complete", "again", [], int(completed["version"]), "tok-1"
        )

    _seed_subagent_run(store, run_id="run-fail", child="task-child-fail")
    claimed = store.claim_subagent_run("run-fail", "owner-a", "tok-2")
    failed = store.fail_subagent_run(
        "run-fail", "boom", int(claimed["version"]), "tok-2"
    )
    assert failed["status"] == "failed"
    with pytest.raises(StaleState):
        store.fail_subagent_run(
            "run-fail", "again", int(failed["version"]), "tok-2"
        )

    _seed_subagent_run(store, run_id="run-review", child="task-child-review")
    claimed = store.claim_subagent_run("run-review", "owner-a", "tok-3")
    reviewed = store.mark_subagent_needs_review(
        "run-review", "plan", int(claimed["version"]), "tok-3"
    )
    assert reviewed["status"] == "needs_review"
    with pytest.raises(StaleState):
        store.complete_subagent_run(
            "run-review", "ok", [], int(reviewed["version"]), "tok-3"
        )
    with pytest.raises(StaleState):
        store.fail_subagent_run(
            "run-review", "no", int(reviewed["version"]), "tok-3"
        )
    cancelled = store.cancel_subagent_run(
        "run-review", "rejected", int(reviewed["version"]), "tok-3"
    )
    assert cancelled["status"] == "cancelled"


def test_mailbox_send_read_archive_and_restart(tmp_path: Path):
    store = _new_store(tmp_path)
    _seed_task(store, "task-parent")
    _seed_task(store, "task-child")
    store.ensure_mailbox("task-child", "assistant", mailbox_id="mb-child")
    assert store.get_mailbox("task-child")["mailbox_id"] == "mb-child"

    payload = {"kind": "ping", "seq": 1}
    first = store.send_mailbox_message(
        "msg-1", "mb-child", "task-parent", "task-child", payload
    )
    second = store.send_mailbox_message(
        "msg-1", "mb-child", "task-parent", "task-child", payload
    )
    assert first["message_id"] == second["message_id"]
    assert len(store.list_mailbox_messages("mb-child")) == 1
    with pytest.raises(StaleState):
        store.send_mailbox_message(
            "msg-1", "mb-child", "task-parent", "task-child", {"seq": 2}
        )

    store.send_mailbox_message(
        "msg-2", "mb-child", "task-parent", "task-child", {"seq": 2}
    )
    read = store.read_mailbox_messages("mb-child", "task-child")
    assert [item["message_id"] for item in read] == ["msg-1", "msg-2"]
    assert all(item["status"] == "read" and item["read_at"] is not None for item in read)
    assert store.read_mailbox_messages("mb-child", "task-child") == []

    restarted = EventStore(store.path)
    mailbox_version = int(restarted.get_mailbox("task-child")["version"])
    with pytest.raises(StaleState):
        restarted.archive_mailbox_message("msg-1", expected_version=mailbox_version + 1)
    archived = restarted.archive_mailbox_message(
        "msg-1", expected_version=mailbox_version
    )
    assert archived["status"] == "archived"
    with pytest.raises(StaleState):
        restarted.archive_mailbox_message(
            "msg-1", expected_version=mailbox_version + 1
        )
    assert restarted.scan_invariants() == []


def test_plan_approval_supersedes_stale_requests(tmp_path: Path):
    store = _new_store(tmp_path)
    _seed_subagent_run(store, run_id="run-plan", child="task-child-plan")
    store.create_plan_approval("approval-1", "run-plan", "hash-1", "owner-a")
    second = store.create_plan_approval("approval-2", "run-plan", "hash-2", "owner-a")
    assert second["status"] == "requested"

    approved = store.transition_plan_approval(
        "approval-1", "approved", decided_by="operator", reason="ok"
    )
    assert approved["status"] == "approved"
    assert store.get_plan_approval("approval-2")["status"] == "superseded"
    with pytest.raises(StaleState):
        store.transition_plan_approval(
            "approval-1", "rejected", decided_by="operator"
        )
    with pytest.raises(StaleState):
        store.transition_plan_approval(
            "approval-2", "approved", decided_by="operator"
        )


def test_spawn_subagent_completes_persists_and_restart_reads_run(tmp_path: Path):
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="child done")]))
    _seed_task(runtime.store, "task-parent")
    result = runtime.spawn_subagent("do it", parent_task_id="task-parent")

    assert result["status"] == "completed"
    run = runtime.store.get_subagent_run(result["run_id"])
    assert run is not None
    assert run["child_task_id"] != "task-parent"
    assert run["parent_task_id"] == "task-parent"
    assert run["status"] == "completed"
    assert runtime.store.get_task(result["child_task_id"])["status"] == "completed"

    restarted = Runtime(tmp_path, ScriptedModel([]))
    recovered = restarted.run_subagent(result["run_id"])
    assert recovered.status == "completed"
    assert recovered.final_text == "child done"


def test_pending_subagent_with_null_parent_resumes_after_restart(tmp_path: Path):
    first = Runtime(tmp_path, ScriptedModel([]))
    spawned = first.spawn_subagent("later", wait=False)

    assert first.store.get_subagent_run(spawned["run_id"])["parent_task_id"] is None
    assert first.store.get_subagent_run(spawned["run_id"])["status"] == "pending"

    second = Runtime(tmp_path, ScriptedModel([ModelResponse(text="resumed")]))
    result = second.run_subagent(spawned["run_id"])
    assert result.status == "completed"
    assert second.store.get_subagent_run(spawned["run_id"])["status"] == "completed"


def test_running_subagent_resumes_after_owner_restart(tmp_path: Path):
    first = Runtime(tmp_path, ScriptedModel([]), owner_id="owner-a")
    spawned = first.spawn_subagent("later", wait=False)
    first.store.claim_subagent_run(spawned["run_id"], "owner-a", "old-token")

    second = Runtime(tmp_path, ScriptedModel([ModelResponse(text="recovered")]), owner_id="owner-b")
    result = second.resume_subagent(spawned["run_id"])

    assert result.status == "completed"
    assert result.final_text == "recovered"
    assert second.store.get_subagent_run(spawned["run_id"])["status"] == "completed"


def test_subagent_tool_scope_filters_schemas(tmp_path: Path):
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "x.txt"})]),
        ModelResponse(text="done"),
    ])
    runtime = Runtime(tmp_path, model)
    _seed_task(runtime.store, "task-parent")
    result = runtime.spawn_subagent(
        "read only", tool_scope={"read_file"}, parent_task_id="task-parent"
    )

    assert result["status"] == "completed"
    child_schemas = [
        schema
        for schema in model.tool_schemas[-1]
        if not schema["name"].startswith(".agent_runtime.")
    ]
    assert {schema["name"] for schema in child_schemas} == {"read_file"}


def test_subagent_tool_scope_rejects_excluded_tool(tmp_path: Path):
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall(
            "write",
            "write_file",
            {"path": "blocked.txt", "content": "no"},
        )]),
    ])
    runtime = Runtime(tmp_path, model, approval_callback=lambda *_: True)
    _seed_task(runtime.store, "task-parent")
    result = runtime.spawn_subagent(
        "do not write", tool_scope={"read_file"}, parent_task_id="task-parent"
    )

    assert result["status"] == "failed"
    assert not (tmp_path / "blocked.txt").exists()
    assert runtime.store.get_subagent_run(result["run_id"])["status"] == "failed"


def test_child_non_read_only_tool_uses_child_operation_ledger(tmp_path: Path):
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall(
            "write",
            "write_file",
            {"path": "out.txt", "content": "hi"},
        )]),
        ModelResponse(text="done"),
    ])
    runtime = Runtime(tmp_path, model, approval_callback=lambda *_: True)
    _seed_task(runtime.store, "task-parent")
    result = runtime.spawn_subagent(
        "write a file", tool_scope={"write_file"}, parent_task_id="task-parent"
    )

    assert result["status"] == "completed"
    child = result["child_task_id"]
    operations = runtime.store.list_operations(child)
    assert len(operations) == 1
    assert operations[0]["state"] == "committed"
    reservations = runtime.store.list_effect_reservations(child)
    assert any(item["state"] == "completed" for item in reservations)
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"


def test_plan_approval_full_flow(tmp_path: Path):
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall(
            "plan",
            ".agent_runtime.request_plan_approval",
            {"plan_text": "1. execute"},
        )]),
        ModelResponse(text="plan executed"),
    ])
    runtime = Runtime(tmp_path, model)
    _seed_task(runtime.store, "task-parent")
    result = runtime.spawn_subagent("plan it", parent_task_id="task-parent")

    assert result["status"] == "needs_review"
    run_id = result["run_id"]
    approvals = runtime.store.list_plan_approvals(run_id)
    assert len(approvals) == 1
    approval_id = approvals[0]["approval_id"]
    assert runtime.store.get_subagent_run(run_id)["status"] == "needs_review"

    plan_file = runtime._plan_file_path(approval_id)
    plan_text = plan_file.read_text(encoding="utf-8")
    assert "approval_id:" in plan_text
    assert "immutable: true" in plan_text
    assert "1. execute" in plan_text

    approved = runtime.approve_subagent_plan(approval_id, approve=True, reason="ok")
    assert approved.status == "completed"
    assert runtime.store.get_subagent_run(run_id)["status"] == "completed"
    assert runtime.store.get_plan_approval(approval_id)["status"] == "approved"


def test_stale_plan_approval_writer_cannot_resume_child(tmp_path: Path):
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall(
            "plan",
            ".agent_runtime.request_plan_approval",
            {"plan_text": "1. execute"},
        )]),
        ModelResponse(text="plan executed"),
    ])
    runtime = Runtime(tmp_path, model)
    _seed_task(runtime.store, "task-parent")
    spawned = runtime.spawn_subagent("plan it", parent_task_id="task-parent")
    approval_id = runtime.store.list_plan_approvals(spawned["run_id"])[0]["approval_id"]

    runtime.store.transition_plan_approval(
        approval_id,
        "approved",
        decided_by="another-operator",
        reason="already decided",
    )

    with pytest.raises(StaleState):
        runtime.approve_subagent_plan(approval_id, approve=True, reason="stale")

    child_task = runtime.store.get_task(spawned["child_task_id"])
    assert child_task["status"] == "needs_review"
    assert runtime.store.get_tool_call(spawned["child_task_id"], "plan")["status"] == "needs_review"
    assert runtime.store.get_subagent_run(spawned["run_id"])["status"] == "needs_review"


def test_plan_file_is_immutable_on_retry(tmp_path: Path):
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]))
    _seed_task(runtime.store, "task-parent")
    spawned = runtime.spawn_subagent("later", wait=False, parent_task_id="task-parent")
    approval = {
        "approval_id": "approval-file",
        "subagent_run_id": spawned["run_id"],
        "plan_hash": "abc",
    }
    path = runtime._write_plan_file(approval, "same plan")
    assert path == str(runtime._plan_file_path("approval-file"))
    runtime._write_plan_file(approval, "same plan")

    Path(path).write_text("tampered", encoding="utf-8")
    with pytest.raises(StaleState):
        runtime._write_plan_file(approval, "same plan")


def test_subagent_context_budget_fails_closed_on_spawn_and_resume(tmp_path: Path):
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]))
    _seed_task(runtime.store, "task-parent")
    with pytest.raises(RuntimeError, match="context budget"):
        runtime.spawn_subagent(
            "x" * 5000, context_window=10, parent_task_id="task-parent"
        )

    spawned = runtime.spawn_subagent(
        "short", context_window=100, wait=False, parent_task_id="task-parent"
    )
    child = spawned["child_task_id"]
    runtime.store.save_checkpoint(
        child,
        "input_ready",
        [{"role": "user", "content": "y" * 5000}],
        {"turn": 0},
    )
    restarted = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]))
    with pytest.raises(RuntimeError, match="context budget"):
        restarted.run_subagent(spawned["run_id"])


def test_subagent_depth_limit(tmp_path: Path):
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]))
    _seed_task(runtime.store, "root")
    first = runtime.spawn_subagent("l1", wait=False, parent_task_id="root")
    second = runtime.spawn_subagent("l2", wait=False, parent_task_id=first["child_task_id"])
    third = runtime.spawn_subagent("l3", wait=False, parent_task_id=second["child_task_id"])

    assert runtime._subagent_depth(third["child_task_id"]) == MAX_SUBAGENT_DEPTH
    with pytest.raises(RuntimeError, match="depth limit"):
        runtime.spawn_subagent(
            "l4", wait=False, parent_task_id=third["child_task_id"]
        )


def test_subagent_cli_parsers_cover_phase3_commands():
    parser = build_parser()
    cases = [
        (["subagent", "spawn", "--prompt", "x"], "spawn"),
        (["subagent", "run", "run-1"], "run"),
        (["subagent", "resume", "run-1"], "resume"),
        (["subagent", "list"], "list"),
        (["subagent", "approvals", "run-1"], "approvals"),
        (["subagent", "approve", "approval-1"], "approve"),
        (["subagent", "reject", "approval-1"], "reject"),
        (["subagent", "show-plan", "approval-1"], "show-plan"),
        (["subagent", "mailbox", "send", "--mailbox", "mb", "--sender", "s",
          "--recipient", "r", "--payload", "{}"], "mailbox"),
        (["subagent", "mailbox", "read", "--mailbox", "mb", "--recipient", "r"],
         "mailbox"),
    ]
    for argv, action in cases:
        args = parser.parse_args(argv)
        assert args.subagent_action == action


def test_cli_mailbox_send_uses_requested_mailbox_id(tmp_path: Path, capsys):
    db = tmp_path / ".agent_runtime" / "runtime.db"
    store = EventStore(db)
    _seed_task(store, "task-parent")
    _seed_task(store, "task-child")
    code = main([
        "subagent", "mailbox", "send", "--repo", str(tmp_path),
        "--mailbox", "mb-custom", "--sender", "task-parent",
        "--recipient", "task-child", "--payload", '{"kind": "ping"}',
    ])
    assert code == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["mailbox_id"] == "mb-custom"

    code = main([
        "subagent", "mailbox", "read", "--repo", str(tmp_path),
        "--mailbox", "mb-custom", "--recipient", "task-child",
    ])
    assert code == 0
    read = json.loads(capsys.readouterr().out)
    assert [item["message_id"] for item in read] == [sent["message_id"]]
