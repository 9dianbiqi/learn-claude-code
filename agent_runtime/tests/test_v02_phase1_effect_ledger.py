from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.effects import EffectSemantics, OperationSpec, ReconcileEvidence
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import Runtime
from agent_runtime.store import EventStore, LeaseLost, StaleState
from agent_runtime.trace import TraceReporter


def _seed(tmp_path: Path, *, tool_use_id: str = "write") -> tuple[EventStore, str, str]:
    store = EventStore(tmp_path / "runtime.db")
    repo = str(tmp_path.resolve())
    task_id = "task-ledger"
    store.create_task(task_id, repo, "ledger", "fake")
    store.save_checkpoint(task_id, "input_ready", [{"role": "user", "content": "ledger"}], {"turn": 0})
    args = {"path": "note.txt", "content": "new"}
    store.create_tool_call(
        task_id,
        tool_use_id,
        0,
        "write_file",
        args,
        "args-hash",
        effect="file_write",
        effect_key="effect-key",
    )
    token = store.acquire_lease(repo, task_id, "owner", ttl=30)
    assert token is not None
    store.bind_lease(repo, "owner", token)
    return store, task_id, repo


def _spec(task_id: str, repo: str, tool_use_id: str = "write", *, key: str = "idem") -> OperationSpec:
    return OperationSpec(
        task_id=task_id,
        tool_use_id=tool_use_id,
        adapter="file",
        semantics=EffectSemantics.RECONCILABLE,
        effect_scope=repo,
        dedupe_key=f"dedupe-{tool_use_id}",
        idempotency_key=key,
        args_hash="args-hash",
        request={"name": "write_file", "input": {"path": "note.txt", "content": "new"}},
    )


def test_prepare_claim_dispatch_commit_is_one_effect_ledger(tmp_path: Path):
    store, task_id, repo = _seed(tmp_path)
    prepared = store.prepare_operation(_spec(task_id, repo))
    assert prepared["state"] == "prepared"
    assert len(store.list_effect_reservations(task_id)) == 1
    assert store.list_pending_operations(task_id)[0]["outbox_state"] == "pending"

    claimed = store.claim_operation(prepared["operation_id"])
    assert claimed["state"] == "prepared"
    dispatched = store.mark_operation_dispatched(prepared["operation_id"])
    assert dispatched["state"] == "dispatched"
    assert dispatched["attempt_count"] == 1

    committed = store.commit_operation(
        prepared["operation_id"],
        result="ok",
        evidence=ReconcileEvidence("post_hash_match", "file matched", {"sha256": "after"}),
        tool_fields={"output": "ok", "effect_confirmed": 1},
        observation={"path": str(tmp_path / "note.txt"), "exists_now": True, "sha256": "after"},
    )
    assert committed["state"] == "committed"
    assert store.list_pending_operations(task_id) == []
    assert store.get_tool_call(task_id, "write")["status"] == "succeeded"
    assert store.get_effect_reservation(task_id, "write")["state"] == "completed"
    assert [event["type"] for event in store.list_events(task_id) if event["type"].startswith("operation_")] == [
        "operation_prepared",
        "operation_claimed",
        "operation_dispatched",
        "operation_committed",
    ]


def test_dedupe_and_idempotency_keys_are_stable_and_scoped(tmp_path: Path):
    store, task_id, repo = _seed(tmp_path)
    first = store.prepare_operation(_spec(task_id, repo))
    second = store.prepare_operation(_spec(task_id, repo))
    assert first["operation_id"] == second["operation_id"]

    other_task = "task-other"
    store.create_task(other_task, repo, "other", "fake")
    store.save_checkpoint(other_task, "input_ready", [{"role": "user", "content": "other"}], {"turn": 0})
    store.create_tool_call(
        other_task, "other-write", 0, "write_file",
        {"path": "note.txt", "content": "new"}, "args-hash", effect="file_write",
    )
    # Once the first operation is committed, the same idempotency key in the
    # same scope is rejected.
    store.claim_operation(first["operation_id"])
    store.mark_operation_dispatched(first["operation_id"])
    store.commit_operation(first["operation_id"], result="ok", tool_fields={"output": "ok"})
    store.release_lease(repo, "owner", fencing_token=store._lease_context[2])  # type: ignore[index]
    store.clear_lease()
    token = store.acquire_lease(repo, other_task, "other-owner", ttl=30)
    assert token is not None
    store.bind_lease(repo, "other-owner", token)
    with pytest.raises(StaleState):
        store.prepare_operation(
            OperationSpec(
                task_id=other_task,
                tool_use_id="other-write",
                adapter="file",
                semantics=EffectSemantics.RECONCILABLE,
                effect_scope=repo,
                dedupe_key="different-dedupe",
                idempotency_key="idem",
                args_hash="args-hash",
                request={},
            )
        )


def test_unknown_opaque_operation_requires_resolution_and_retry_creates_attempt(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    repo = str(tmp_path.resolve())
    task_id = "task-shell"
    store.create_task(task_id, repo, "shell", "fake")
    store.save_checkpoint(task_id, "input_ready", [{"role": "user", "content": "shell"}], {"turn": 0})
    store.create_tool_call(task_id, "shell", 0, "bash", {"command": "echo x"}, "shell-hash", effect="unknown_write")
    token = store.acquire_lease(repo, task_id, "owner", ttl=30)
    assert token is not None
    store.bind_lease(repo, "owner", token)
    spec = OperationSpec(
        task_id, "shell", "legacy", EffectSemantics.OPAQUE, repo, "shell-dedupe", None,
        "shell-hash", {"name": "bash", "input": {"command": "echo x"}},
    )
    operation = store.prepare_operation(spec)
    store.claim_operation(operation["operation_id"])
    store.mark_operation_dispatched(operation["operation_id"])
    store.mark_operation_unknown(operation["operation_id"], "shell outcome unknown")
    assert store.list_pending_operations(task_id)[0]["outbox_state"] == "blocked"
    with pytest.raises(ValueError):
        store.resolve_operation(
            operation["operation_id"],
            "retry",
            ReconcileEvidence("unknown", "not enough evidence", {}),
        )
    retried = store.resolve_operation(
        operation["operation_id"],
        "retry",
        ReconcileEvidence("not_happened", "operator checked marker", {"source": "operator"}),
    )
    assert retried["state"] == "prepared"
    assert len(store.list_effect_reservations(task_id)) == 2
    assert store.list_effect_reservations(task_id)[0]["state"] == "cancelled"


def test_runtime_creates_only_effect_operations_and_trace_exposes_v02_metrics(tmp_path: Path):
    (tmp_path / "note.txt").write_text("old", encoding="utf-8")
    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "note.txt"})]),
            ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "note.txt", "old_text": "old", "new_text": "new"})]),
            ModelResponse(text="done"),
        ]),
        approval_callback=lambda *_: True,
    )
    result = runtime.run("edit")
    assert result.status == "completed"
    assert len(runtime.store.list_operations(result.task_id)) == 1
    summary = TraceReporter(runtime.store).summary(result.task_id)
    for key in (
        "operation_count",
        "operation_attempts",
        "committed_operations",
        "unknown_operations",
        "reconciled_operations",
        "operation_deduplications",
        "idempotency_conflicts",
        "blocked_outbox_count",
    ):
        assert key in summary
    assert summary["operation_count"] == 1
    assert summary["committed_operations"] == 1
    assert summary["invariant_violation_count"] == 0


def test_stale_fencing_token_cannot_transition_operation(tmp_path: Path):
    store, task_id, repo = _seed(tmp_path)
    operation = store.prepare_operation(_spec(task_id, repo))
    token = store._lease_context[2]  # type: ignore[index]
    store.bind_lease(repo, "owner", int(token) + 1)
    with pytest.raises(LeaseLost):
        store.claim_operation(operation["operation_id"])
    store.bind_lease(repo, "owner", token)
    assert store.get_operation(operation["operation_id"])["state"] == "prepared"


def test_dispatched_unknown_file_requires_explicit_retry_when_before_hash_remains(tmp_path: Path):
    store, task_id, repo = _seed(tmp_path)
    operation = store.prepare_operation(_spec(task_id, repo))
    store.claim_operation(operation["operation_id"])
    store.mark_operation_dispatched(operation["operation_id"])
    store.mark_operation_unknown(operation["operation_id"], "api_key=do-not-copy")
    assert store.get_tool_call(task_id, "write")["status"] == "needs_review"
    with pytest.raises(StaleState):
        store.commit_operation(operation["operation_id"], result="unsafe")
    retried = store.resolve_operation(
        operation["operation_id"],
        "retry",
        ReconcileEvidence("before_hash", "operator confirmed the before hash", {"source": "operator"}),
    )
    assert retried["state"] == "prepared"
    assert retried["attempt_count"] == 1
    assert len(store.list_effect_reservations(task_id)) == 2
    assert all("do-not-copy" not in json.dumps(event["payload"]) for event in store.list_events(task_id))
