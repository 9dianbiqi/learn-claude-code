from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime import AddPlanItem, PlanItemDraft, PlanPatch, Runtime, SplitPlanItem, TombstonePlanItem
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.migrations import SchemaManager, SchemaUpgradeRequired, V13_CHECKSUM
from agent_runtime.models import ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import InvariantViolation, LeaseLost, StaleState
from agent_runtime.trace import TraceReporter


def _node(name: str, verified: list[str], dependencies: tuple[str, ...] = (), budget: int = 2):
    def verify(context):
        verified.append(context.subtask_id)
        return VerifierResult("pass", f"{name} verified", [
            {"path": f"{name}.txt", "sha256": hashlib.sha256(b"artifact").hexdigest()},
        ])

    return VerifiedSubtaskConfig(
        subtask_id=name, description=f"Complete {name}", completion_criteria=f"{name} done",
        evidence_paths=(f"{name}.txt",), verifier_id=f"verify-{name}", verifier_version="1",
        verification_rule=f"rule-{name}", verifier=verify,
        verifier_implementation_hash="b" * 64, blocked_by=dependencies, max_turns=budget,
    )


def _draft(node):
    return PlanItemDraft(node.subtask_id, node.description, node.blocked_by,
                         node.verifier_bundle_hash, node.max_turns)


def _crash_at(point):
    def inject(actual, **context):
        if actual == point:
            raise InjectedCrash(point)
    return inject


def _failed_revision(tmp_path):
    verified = []
    for name in ("a", "b", "c", "c1", "c2", "extra"):
        (tmp_path / f"{name}.txt").write_bytes(b"artifact")
    a, b, c = (_node("a", verified), _node("b", verified, ("a",)),
               _node("c", verified, ("b",), 1))
    original = VerifiedSubtaskDAGConfig((a, b, c))
    runtime = Runtime(tmp_path, ScriptedModel([
        ModelResponse(text="a done\nSUBTASK_COMPLETE"),
        ModelResponse(text="b done\nSUBTASK_COMPLETE"),
        ModelResponse(text="c could not finish"),
    ]), verified_subtask_dag=original)
    result = runtime.run("Build")
    assert result.status == "failed"
    initial = runtime.get_current_plan_revision(result.task_id)
    assert runtime.store.get_task(result.task_id)["execution_plan_revision_id"] == initial.revision_id
    c1, c2 = _node("c1", verified, ("b",)), _node("c2", verified, ("b",))
    revised = runtime.apply_plan_patch(result.task_id, initial.revision_id, PlanPatch(
        reason="Replace exhausted c", trigger="operator",
        operations=(SplitPlanItem("c", (_draft(c1), _draft(c2))),),
    ))
    assert runtime.store.get_task(result.task_id)["execution_plan_revision_id"] == initial.revision_id
    assert runtime.store.get_latest_plan(result.task_id)["dag_hash"] == original.dag_hash
    current = VerifiedSubtaskDAGConfig((a, b, c1, c2))
    assert revised.dag_hash == current.dag_hash
    return runtime, result.task_id, initial, revised, original, current, verified


def _activation_events(runtime, task_id):
    return [event for event in runtime.store.list_events(task_id)
            if event["type"] == "plan_revision_activated"]


@pytest.mark.parametrize("scoped", [False, True])
def test_failed_split_resume_preserves_completed_work_and_immutable_history(tmp_path, scoped):
    old, task_id, initial, revised, _, current, verified = _failed_revision(tmp_path)
    old_checkpoints = old.store.list_checkpoints(task_id)
    evidence = old.store.list_verified_subtask_checkpoints(task_id)
    model = ScriptedModel([ModelResponse(text=f"{name} done\nSUBTASK_COMPLETE") for name in ("c1", "c2")])
    resumed = Runtime(tmp_path, model, verified_subtask_dag=current,
                      scoped_context_budget=10000 if scoped else None)
    assert resumed.resume(task_id).status == "completed"
    assert verified == ["a", "b", "c1", "c2"]
    assert model.call_count == 2
    assert resumed.store.get_task(task_id)["execution_plan_revision_id"] == revised.revision_id
    assert resumed.store.get_plan_item(task_id, "c")["tombstoned"] == 1
    assert resumed.store.get_plan_item(task_id, "c")["consumed_turns"] == 1
    assert resumed.store.list_plan_revisions(task_id) == [initial, revised]
    assert resumed.store.list_checkpoints(task_id)[:len(old_checkpoints)] == old_checkpoints
    assert resumed.store.list_verified_subtask_checkpoints(task_id)[:len(evidence)] == evidence
    assert len(_activation_events(resumed, task_id)) == 1
    assert resumed.resume(task_id).status == "completed"
    assert model.call_count == 2
    assert verified == ["a", "b", "c1", "c2"]
    trace = TraceReporter(resumed.store).summary(task_id)
    assert trace["current_plan_revision_id"] == revised.revision_id
    assert trace["execution_plan_revision_id"] == revised.revision_id
    assert trace["plan_revision_activation_count"] == 1
    resumed.store.assert_invariants(task_id)


@pytest.mark.parametrize("kind", ["missing", "old", "verifier", "order", "dependency", "budget"])
def test_revised_resume_rejects_inexact_configuration_without_execution(tmp_path, kind):
    old, task_id, initial, _, original, current, verified = _failed_revision(tmp_path)
    nodes = list(current.nodes)
    if kind == "verifier":
        nodes[-1] = replace(nodes[-1], verifier_implementation_hash="c" * 64)
    elif kind == "order":
        nodes[-2:] = reversed(nodes[-2:])
    elif kind == "dependency":
        nodes[-1] = replace(nodes[-1], blocked_by=("a",))
    elif kind == "budget":
        nodes[-1] = replace(nodes[-1], max_turns=3)
    config = None if kind == "missing" else original if kind == "old" else VerifiedSubtaskDAGConfig(tuple(nodes))
    before = old.store.get_task(task_id)
    checkpoints = old.store.list_checkpoints(task_id)
    model = ScriptedModel([])
    resumed = Runtime(tmp_path, model, verified_subtask_dag=config)
    with pytest.raises(RuntimeError, match="configuration|hash mismatch"):
        resumed.resume(task_id)
    assert resumed.store.get_task(task_id) == before
    assert resumed.store.list_checkpoints(task_id) == checkpoints
    assert resumed.store.get_task(task_id)["execution_plan_revision_id"] == initial.revision_id
    assert model.call_count == 0
    assert verified == ["a", "b"]
    assert not _activation_events(resumed, task_id)


@pytest.mark.parametrize("point", ["plan_revision_activation_before_commit", "plan_revision_activation_after_commit"])
def test_activation_crash_is_old_or_new_and_repeated_restart_is_idempotent(tmp_path, point):
    old, task_id, initial, revised, _, current, verified = _failed_revision(tmp_path)
    before = old.store.get_task(task_id)
    checkpoints = old.store.list_checkpoints(task_id)
    crashing = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=current,
                       fault_injector=_crash_at(point))
    with pytest.raises(InjectedCrash, match=point):
        crashing.resume(task_id)
    fresh = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=current)
    task = fresh.store.get_task(task_id)
    if point.endswith("before_commit"):
        assert task == before
        assert fresh.store.get_latest_plan(task_id)["status"] == "failed"
        assert fresh.store.list_checkpoints(task_id) == checkpoints
        assert not _activation_events(fresh, task_id)
    else:
        assert task["execution_plan_revision_id"] == revised.revision_id
        assert task["status"] == "running"
        assert fresh.store.get_latest_plan(task_id)["status"] == "active"
        assert len(fresh.store.list_checkpoints(task_id)) == len(checkpoints) + 1
        assert len(_activation_events(fresh, task_id)) == 1
    fresh.store.assert_invariants(task_id)
    # A second crash after completing c1 must not spend its budget or verify it twice.
    second = Runtime(tmp_path, ScriptedModel([ModelResponse(text="c1 done\nSUBTASK_COMPLETE")]),
                     verified_subtask_dag=current, fault_injector=_crash_at("verified_subtask_f3"))
    with pytest.raises(InjectedCrash):
        second.resume(task_id)
    last_model = ScriptedModel([ModelResponse(text="c2 done\nSUBTASK_COMPLETE")])
    last = Runtime(tmp_path, last_model, verified_subtask_dag=current)
    assert last.resume(task_id).status == "completed"
    assert last_model.call_count == 1
    assert verified == ["a", "b", "c1", "c2"]
    assert len(_activation_events(last, task_id)) == 1
    assert last.store.list_plan_revisions(task_id) == [initial, revised]
    last.store.assert_invariants(task_id)


def test_active_patch_resume_ignores_old_completion_marker_and_tombstoned_pending(tmp_path):
    verified = []
    for name in ("a", "c"):
        (tmp_path / f"{name}.txt").write_bytes(b"artifact")
    a, b, c = _node("a", verified), _node("b", verified, ("a",)), _node("c", verified, ("a",))
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="a done\nSUBTASK_COMPLETE")]),
                      verified_subtask_dag=VerifiedSubtaskDAGConfig((a, b)),
                      fault_injector=_crash_at("verified_subtask_f3"))
    with pytest.raises(InjectedCrash):
        runtime.run("Build")
    task_id = runtime.store.list_tasks()[0]["task_id"]
    runtime.fault_injector = None
    original = runtime.get_current_plan_revision(task_id)
    revised = runtime.apply_plan_patch(task_id, original.revision_id, PlanPatch(
        reason="Replace pending work", trigger="operator",
        operations=(TombstonePlanItem("b"), AddPlanItem(_draft(c))),
    ))
    model = ScriptedModel([ModelResponse(text="c done\nSUBTASK_COMPLETE")])
    resumed = Runtime(tmp_path, model, verified_subtask_dag=VerifiedSubtaskDAGConfig((a, c)))
    assert resumed.resume(task_id).status == "completed"
    assert verified == ["a", "c"]
    assert model.call_count == 1
    assert resumed.store.get_plan_item(task_id, "b")["status"] == "pending"
    assert resumed.store.get_plan_item(task_id, "b")["tombstoned"] == 1
    assert resumed.store.get_task(task_id)["execution_plan_revision_id"] == revised.revision_id


@pytest.mark.parametrize("binding", [None, "foreign", "unaudited"])
def test_execution_binding_invariants_fail_closed(tmp_path, binding):
    runtime, task_id, _, revised, _, current, _ = _failed_revision(tmp_path)
    if binding == "foreign":
        runtime.store.bootstrap_task("other", str(tmp_path), "Other", "scripted", [], {"turn": 0})
        runtime.store.create_plan("other", [{"subtask_id": "other"}])
        binding = runtime.get_current_plan_revision("other").revision_id
    elif binding == "unaudited":
        binding = revised.revision_id
    with sqlite3.connect(runtime.store.path) as conn:
        conn.execute("UPDATE tasks SET execution_plan_revision_id = ? WHERE task_id = ?", (binding, task_id))
    resumed = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=current)
    with pytest.raises(InvariantViolation, match="execution revision"):
        resumed.resume(task_id)


def _downgrade_fixture_to_v13(runtime):
    # The production migration remains forward-only; remove v14 from this
    # isolated test database to exercise a real v13 startup and migration.
    with sqlite3.connect(runtime.store.path) as conn:
        conn.execute("DROP TABLE replan_decisions")
        conn.execute("ALTER TABLE verifier_runs DROP COLUMN observed_plan_item_version")
        conn.execute("ALTER TABLE tasks DROP COLUMN execution_plan_revision_id")
        conn.execute("UPDATE schema_migrations SET version = 13, name = 'v13_plan_revisions', checksum = ?",
                     (V13_CHECKSUM,))


@pytest.mark.parametrize("crash", [False, True])
def test_v13_migration_backfills_revision_zero_and_preserves_pending_activation(tmp_path, crash):
    runtime, task_id, initial, revised, _, current, _ = _failed_revision(tmp_path)
    runtime.store.bootstrap_task("no-plan", str(tmp_path), "Other", "scripted", [], {"turn": 0})
    runtime.store.bootstrap_task("unpatched", str(tmp_path), "Other", "scripted", [], {"turn": 0})
    runtime.store.create_plan("unpatched", [{"subtask_id": "only"}])
    unpatched = runtime.get_current_plan_revision("unpatched")
    checkpoints = runtime.store.list_checkpoints(task_id)
    _downgrade_fixture_to_v13(runtime)
    with pytest.raises(SchemaUpgradeRequired):
        Runtime(tmp_path, ScriptedModel([]))
    if crash:
        with pytest.raises(InjectedCrash):
            SchemaManager(runtime.store.path, fault_injector=_crash_at("before_migration_commit")).migrate()
        assert SchemaManager(runtime.store.path).inspect().current_version == 13
        with sqlite3.connect(runtime.store.path) as conn:
            assert "execution_plan_revision_id" not in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    report = SchemaManager(runtime.store.path).migrate()
    assert report.applied == ("v14_plan_revision_resume", "v15_replan_decisions")
    assert report.backup is not None
    fresh = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=current)
    assert fresh.store.get_task(task_id)["execution_plan_revision_id"] == initial.revision_id
    assert fresh.get_current_plan_revision(task_id) == revised
    assert fresh.store.get_task("no-plan")["execution_plan_revision_id"] is None
    assert fresh.store.get_task("unpatched")["execution_plan_revision_id"] == unpatched.revision_id
    assert fresh.store.list_checkpoints(task_id) == checkpoints
    assert not SchemaManager(runtime.store.path).migrate().applied
    fresh.store.assert_invariants()


def test_unrelated_patch_does_not_reset_exhausted_active_item(tmp_path):
    verified = []
    node = _node("c", verified, budget=1)
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="not done")]),
                      verified_subtask_dag=VerifiedSubtaskDAGConfig((node,)))
    result = runtime.run("Build")
    assert result.status == "failed"
    initial = runtime.get_current_plan_revision(result.task_id)
    extra = _node("extra", verified)
    runtime.apply_plan_patch(result.task_id, initial.revision_id, PlanPatch(
        reason="Unrelated additional work", trigger="operator", operations=(AddPlanItem(_draft(extra)),),
    ))
    model = ScriptedModel([])
    resumed = Runtime(tmp_path, model, verified_subtask_dag=VerifiedSubtaskDAGConfig((node, extra)))
    before = resumed.store.get_task(result.task_id)
    with pytest.raises(RuntimeError, match="not eligible"):
        resumed.resume(result.task_id)
    assert resumed.store.get_task(result.task_id) == before
    assert resumed.store.get_plan_item(result.task_id, "c")["consumed_turns"] == 1
    assert not _activation_events(resumed, result.task_id)
    assert model.call_count == 0


def test_stale_activation_and_lost_lease_cannot_advance_execution(tmp_path):
    runtime, task_id, initial, revised, _, current, _ = _failed_revision(tmp_path)
    with pytest.raises(StaleState, match="changed before activation"):
        runtime.store.activate_plan_revision(task_id, initial.revision_id, current.dag_hash)
    runtime._acquire(task_id)
    try:
        with sqlite3.connect(runtime.store.path) as conn:
            conn.execute("UPDATE leases SET expires_at = 0")
        with pytest.raises(LeaseLost):
            runtime.store.activate_plan_revision(task_id, revised.revision_id, current.dag_hash)
    finally:
        runtime._release()
    assert runtime.store.get_task(task_id)["execution_plan_revision_id"] == initial.revision_id
    assert not _activation_events(runtime, task_id)


@pytest.mark.parametrize("point", ["plan_revision_activation_before_commit", "plan_revision_activation_after_commit"])
def test_activation_process_exit_reopens_complete_old_or_new_state(tmp_path, point):
    runtime, task_id, initial, revised, _, current, _ = _failed_revision(tmp_path)
    script = """
import os
import sys
from agent_runtime.store import EventStore
store = EventStore(sys.argv[1])
def fault(point, **context):
    if point == sys.argv[5]:
        os._exit(79)
store.activate_plan_revision(sys.argv[2], sys.argv[3], sys.argv[4], fault_injector=fault)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(runtime.store.path), task_id,
         revised.revision_id, current.dag_hash, point],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 79, result.stderr
    fresh = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=current)
    before_commit = point.endswith("before_commit")
    task = fresh.store.get_task(task_id)
    assert task["execution_plan_revision_id"] == (initial.revision_id if before_commit else revised.revision_id)
    assert task["status"] == ("failed" if before_commit else "running")
    assert len(_activation_events(fresh, task_id)) == (0 if before_commit else 1)
    fresh.store.assert_invariants(task_id)


@pytest.mark.parametrize("point", ["after_tool_effect_before_persist", "after_tool_persist"])
def test_revised_resume_preserves_tool_effect_reconciliation(tmp_path, point):
    verified = []
    for name in ("a", "b"):
        (tmp_path / f"{name}.txt").write_bytes(b"artifact")
    policy = tmp_path / "policy.yaml"
    policy.write_text("rules:\n  - id: write\n    effect: allow\n    tools: [write_file]\n    paths: ['effect.txt']\n", encoding="utf-8")
    a, b = _node("a", verified, budget=3), _node("b", verified, ("a",))
    first = Runtime(tmp_path, ScriptedModel([ModelResponse(tool_calls=[
        ToolCall("effect", "write_file", {"path": "effect.txt", "content": "once"}),
    ])]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)), policy_path=policy,
                    fault_injector=_crash_at(point))
    with pytest.raises(InjectedCrash):
        first.run("Build")
    task_id = first.store.list_tasks()[0]["task_id"]
    first.fault_injector = None
    first.apply_plan_patch(task_id, first.get_current_plan_revision(task_id).revision_id, PlanPatch(
        reason="Add dependent work", trigger="operator", operations=(AddPlanItem(_draft(b)),),
    ))
    assert (tmp_path / "effect.txt").read_text(encoding="utf-8") == "once"
    resumed = Runtime(tmp_path, ScriptedModel([
        ModelResponse(text="a done\nSUBTASK_COMPLETE"), ModelResponse(text="b done\nSUBTASK_COMPLETE"),
    ]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a, b)), policy_path=policy)
    assert resumed.resume(task_id).status == "completed"
    assert verified == ["a", "b"]
    call = resumed.store.get_tool_call(task_id, "effect")
    assert call["status"] == "succeeded"
    assert call["effect_attempts"] == 1
    assert TraceReporter(resumed.store).summary(task_id)["confirmed_duplicate_side_effects"] == 0
    resumed.store.assert_invariants(task_id)
