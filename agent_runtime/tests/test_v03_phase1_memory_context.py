from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import InjectedCrash, Runtime
from agent_runtime.store import EventStore, InvariantViolation
from agent_runtime.trace import TraceReporter


def _seed(tmp_path: Path, *, with_plan: bool = True) -> tuple[EventStore, str, str, list[dict]]:
    """Seed a task with a checkpoint and optional durable plan/memory state."""
    store = EventStore(tmp_path / "runtime.db")
    repo = str(tmp_path.resolve())
    task_id = "task-phase1"
    messages = [{"role": "user", "content": "Build a small hello project."}]
    store.bootstrap_task(task_id, repo, "Build a small hello project.", "scripted-fake", messages, {"turn": 0})
    checkpoint_id = store.get_task(task_id)["checkpoint_id"]
    if with_plan:
        plan_id = store.create_plan(
            task_id,
            subtasks=[
                {"subtask_id": "s1", "description": "Scaffold project"},
                {"subtask_id": "s2", "description": "Wire tests", "blocked_by": ["s1"]},
            ],
        )
        store.start_plan_item(store.list_plan_items(plan_id)[0]["plan_item_id"])
        store.add_memory(task_id, "fact", "Use UTF-8 filenames", source_checkpoint_id=checkpoint_id)
    return store, task_id, repo, messages


def test_projection_is_read_only_and_keeps_full_checkpoint(tmp_path: Path):
    store, task_id, repo, messages = _seed(tmp_path)
    model = ScriptedModel([ModelResponse(text="done", usage={"input_tokens": 5, "output_tokens": 2})])
    runtime = Runtime(repo, model=model, store=store)

    result = runtime._run_task(task_id, messages, turn=0)

    assert result.status == "completed"
    # The model saw a projected view that includes plan/memory system blocks.
    projected_messages = model.calls[0]
    assert any("Current plan item" in str(message.get("content")) for message in projected_messages)
    assert any("Use UTF-8 filenames" in str(message.get("content")) for message in projected_messages)
    # The authoritative checkpoint is never replaced by the projected view.
    checkpoint = store.get_checkpoint(store.get_task(task_id)["checkpoint_id"])
    assert checkpoint["messages"] == messages
    assert all("Current plan item" not in str(message.get("content")) for message in checkpoint["messages"])


def test_plan_item_cannot_complete_without_verifier_evidence(tmp_path: Path):
    store, task_id, repo, _ = _seed(tmp_path)
    plan = store.get_active_plan(task_id)
    item_id = plan["items"][0]["plan_item_id"]

    store.submit_plan_item_for_verification(item_id, completion_summary="claimed by model text")
    with pytest.raises(InvariantViolation):
        store.verify_plan_item(item_id, evidence_hash=None)

    store.verify_plan_item(item_id, evidence_hash="sha256:abc123")
    plan = store.get_active_plan(task_id)
    assert plan["items"][0]["status"] == "completed"
    assert plan["items"][0]["evidence_hash"] == "sha256:abc123"


def test_runtime_persists_projection_source_and_metrics(tmp_path: Path):
    store, task_id, repo, messages = _seed(tmp_path)
    model = ScriptedModel([ModelResponse(text="done")])
    runtime = Runtime(repo, model=model, store=store)

    runtime._run_task(task_id, messages, turn=0)

    calls = store.list_model_calls(task_id)
    assert calls
    assert calls[0]["source_checkpoint_id"] is not None
    assert calls[0]["projection"]["basis"] == "projected"
    assert calls[0]["projection"]["projection_used"] is True
    assert calls[0]["projection"]["active_subtask_id"] == "s1"


def test_recovery_after_model_response_uses_full_checkpoint_and_dedups(tmp_path: Path):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    store, task_id, repo, messages = _seed(tmp_path)

    def crash_after_persist(point, **_):
        if point == "after_tool_persist":
            raise InjectedCrash("crashed after persist")

    first_model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="call-write-1", name="read_file", input={"path": "note.txt"})]),
            ModelResponse(text="finished"),
        ]
    )
    first = Runtime(repo, model=first_model, store=store, fault_injector=crash_after_persist)
    with pytest.raises(InjectedCrash):
        first._run_task(task_id, messages, turn=0)

    second = Runtime(repo, model=ScriptedModel([ModelResponse(text="finished")]), store=store)
    second.resume(task_id)

    events = second.store.list_events(task_id)
    assert sum(event["type"] == "tool_succeeded" for event in events) == 1
    assert sum(event["type"] == "tool_deduplicated" for event in events) == 1
    call = second.store.get_tool_call(task_id, "call-write-1")
    assert call["status"] == "succeeded"


def test_trace_export_includes_projection_and_plan_metrics(tmp_path: Path):
    store, task_id, repo, messages = _seed(tmp_path)
    model = ScriptedModel([ModelResponse(text="done")])
    runtime = Runtime(repo, model=model, store=store)
    runtime._run_task(task_id, messages, turn=0)

    reporter = TraceReporter(store)
    summary = reporter.summary(task_id)
    assert summary["projection_used_count"] >= 1
    assert summary["plan_item_count"] == 2

    output = tmp_path / "trace.jsonl"
    reporter.export_jsonl(task_id, output)
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    records = [line for line in lines if "record_type" in line]
    record_types = {line["record_type"] for line in records}
    assert "model_call" in record_types
    assert "plan" in record_types
    plan = next(line for line in records if line["record_type"] == "plan")
    assert len(plan["items"]) == 2


def test_memories_without_plan_are_still_projected(tmp_path: Path):
    store, task_id, repo, messages = _seed(tmp_path, with_plan=False)
    checkpoint_id = store.get_task(task_id)["checkpoint_id"]
    store.add_memory(task_id, "fact", "Memory without a plan", source_checkpoint_id=checkpoint_id)

    model = ScriptedModel([ModelResponse(text="done")])
    runtime = Runtime(repo, model=model, store=store)
    result = runtime._run_task(task_id, messages, turn=0)

    assert result.status == "completed"
    projected = model.calls[0]
    assert any("Memory without a plan" in str(message.get("content")) for message in projected)
    assert all("Current plan item" not in str(message.get("content")) for message in projected)
    call = store.list_model_calls(task_id)[0]
    assert call["projection"]["basis"] == "projected"
    assert call["projection"]["active_subtask_id"] is None
    checkpoint = store.get_checkpoint(store.get_task(task_id)["checkpoint_id"])
    assert checkpoint["messages"] == messages


def test_reprojection_matches_persisted_model_input(tmp_path: Path):
    store, task_id, repo, messages = _seed(tmp_path)
    model = ScriptedModel([ModelResponse(text="done")])
    runtime = Runtime(repo, model=model, store=store)
    runtime._run_task(task_id, messages, turn=0)

    call = store.list_model_calls(task_id)[0]
    persisted = call["projection"]
    assert "messages" in persisted
    assert persisted["basis"] == "projected"

    # Projection is deterministic: rebuilding from the same source checkpoint
    # reproduces the exact message list that was handed to the model.
    source_checkpoint_id = persisted["source_checkpoint_id"]
    checkpoint = store.get_checkpoint(source_checkpoint_id)
    rebuilt = runtime.projector.project(
        task_id, checkpoint["messages"], source_checkpoint_id
    )
    assert rebuilt.messages == persisted["messages"]
    assert json.dumps(rebuilt.messages, sort_keys=True) == \
        json.dumps(persisted["messages"], sort_keys=True)


def test_retryable_plan_item_can_restart(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    repo = str(tmp_path.resolve())
    task_id = "task-retry"
    store.bootstrap_task(
        task_id, repo, "Retry me", "fake",
        [{"role": "user", "content": "go"}], {"turn": 0},
    )
    plan_id = store.create_plan(task_id, [{"subtask_id": "a", "description": "A"}])
    item_id = store.list_plan_items(plan_id)[0]["plan_item_id"]

    store.start_plan_item(item_id)
    store.submit_plan_item_for_verification(item_id, completion_summary="claimed")
    store.fail_plan_item(item_id, reason="verification failed")
    assert store.list_plan_items(plan_id)[0]["status"] == "failed"

    store.mark_plan_item_retryable(item_id)
    assert store.list_plan_items(plan_id)[0]["status"] == "retryable"

    store.start_plan_item(item_id)
    assert store.list_plan_items(plan_id)[0]["status"] == "in_progress"
    assert store.scan_invariants(task_id) == []
    events = store.list_events(task_id)
    started = [event for event in events if event["type"] == "plan_item_started"]
    assert started and started[-1]["payload"].get("from") == "retryable"


def test_scan_invariants_detects_completed_plan_item_without_evidence(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    repo = str(tmp_path.resolve())
    task_id = "task-invariant"
    store.bootstrap_task(
        task_id, repo, "Invariant", "fake",
        [{"role": "user", "content": "go"}], {"turn": 0},
    )
    plan_id = store.create_plan(task_id, [{"subtask_id": "a", "description": "A"}])
    assert store.scan_invariants(task_id) == []

    # Bypass the store write path to inject an inconsistent row on purpose.
    with sqlite3.connect(store.path) as connection, connection:
        connection.execute(
            "UPDATE plan_items SET status = 'completed', evidence_hash = NULL "
            "WHERE plan_id = ?",
            (plan_id,),
        )
    violations = store.scan_invariants(task_id)
    assert any("completed without evidence_hash" in violation for violation in violations)
