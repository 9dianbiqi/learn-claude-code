from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime import (
    AddPlanItem, PlanItemDraft, PlanPatch, Runtime, SplitPlanItem,
    TombstonePlanItem, UpdatePlanItemDependencies,
)
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.migrations import SchemaManager, V14_CHECKSUM
from agent_runtime.models import (
    ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult,
)
from agent_runtime.replanning import RecoveryPlanRevisionController, ReplanDecision
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import InvariantViolation, StaleState
from agent_runtime.trace import TraceReporter


def _node(name, blocked_by=(), max_turns=3, *, verdict="pass"):
    digest = hashlib.sha256(b"artifact").hexdigest()
    return VerifiedSubtaskConfig(
        subtask_id=name, description=f"Complete {name}",
        completion_criteria=f"{name} is complete", evidence_paths=(f"{name}.txt",),
        verifier_id=f"verify-{name}", verifier_version="1",
        verification_rule=f"rule-{name}", verifier_implementation_hash="b" * 64,
        verifier=lambda _: VerifierResult(
            verdict, f"{name}: {verdict}",
            [{"path": f"{name}.txt", "sha256": digest}] if verdict == "pass" else [],
        ),
        blocked_by=blocked_by, max_turns=max_turns,
    )


def _draft(node):
    return PlanItemDraft(node.subtask_id, node.description, node.blocked_by,
                         node.verifier_bundle_hash, node.max_turns)


def _setup(tmp_path, nodes):
    for name in {"a", "b", "a1", "a2", "extra"}:
        (tmp_path / f"{name}.txt").write_bytes(b"artifact")
    dag = VerifiedSubtaskDAGConfig(nodes)
    runtime = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=dag)
    task_id = "task-c1c"
    runtime.store.bootstrap_task(
        task_id, str(tmp_path.resolve()), "Build", "scripted",
        [{"role": "user", "content": "Build"}], {"turn": 0},
    )
    runtime.store.create_plan(task_id, [{
        "subtask_id": node.subtask_id, "description": node.description,
        "blocked_by": list(node.blocked_by),
        "verifier_bundle_hash": node.verifier_bundle_hash,
        "max_turns": node.max_turns,
    } for node in nodes], dag_hash=dag.dag_hash)
    return runtime, task_id


def _signal(runtime, task_id, subtask_id="a"):
    return RecoveryPlanRevisionController(runtime, _policy("KEEP")).record_observation(
        task_id, subtask_id, "fixed observation", (f"fixture:{task_id}:{subtask_id}",),
    )


def _policy(kind, operation=None, proposed_dag=None):
    def decide(signal, revision, item):
        patch = (PlanPatch(
            reason=f"{kind} after {signal.signal_id}", trigger="c1c_controller",
            operations=(operation,), evidence_refs=(signal.signal_id,),
        ) if operation is not None else None)
        return ReplanDecision(kind, f"fixed {kind}", revision.revision_id, patch, proposed_dag)
    return decide


@pytest.mark.parametrize("kind", ["KEEP", "RETRY", "FAIL"])
def test_non_structural_decisions_are_durable_without_revision(tmp_path, kind):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    if kind == "RETRY":
        item = runtime.store.get_plan_item(task_id, "a")
        runtime.store.start_plan_item(item["plan_item_id"])
        runtime.store.fail_plan_item(item["plan_item_id"], "try again")
        runtime.store.mark_plan_item_retryable(item["plan_item_id"])
    signal_id = _signal(runtime, task_id)
    revision = runtime.get_current_plan_revision(task_id)
    before = runtime.store.get_plan_item(task_id, "a")
    controller = RecoveryPlanRevisionController(runtime, _policy(kind))
    outcome = controller.process(task_id, signal_id)
    assert outcome.outcome == "accepted"
    assert outcome.result_revision_id is None
    assert controller.process(task_id, signal_id) == outcome
    assert runtime.get_current_plan_revision(task_id) == revision
    assert len(runtime.store.list_plan_revisions(task_id)) == 1
    assert len(runtime.store.list_replan_decisions(task_id)) == 1
    after = runtime.store.get_plan_item(task_id, "a")
    assert after["consumed_turns"] == before["consumed_turns"]
    assert after["status"] == before["status"]
    assert runtime.store.get_task(task_id)["status"] == ("failed" if kind == "FAIL" else "created")
    runtime.store.assert_invariants(task_id)


@pytest.mark.parametrize("kind", ["SPLIT", "ADD_ITEM", "CHANGE_DEPENDENCY", "TOMBSTONE_PENDING"])
def test_structural_decisions_commit_one_typed_revision_with_ledger(tmp_path, kind):
    a, b = _node("a"), _node("b")
    nodes = (a, b) if kind in {"CHANGE_DEPENDENCY", "TOMBSTONE_PENDING"} else (a,)
    runtime, task_id = _setup(tmp_path, nodes)
    if kind == "SPLIT":
        item = runtime.store.get_plan_item(task_id, "a")
        runtime.store.start_plan_item(item["plan_item_id"])
        runtime.store.fail_plan_item(item["plan_item_id"], "split")
        runtime.store.mark_plan_item_retryable(item["plan_item_id"])
    signal_id = _signal(runtime, task_id)
    if kind == "SPLIT":
        a1, a2 = _node("a1"), _node("a2")
        operation, current = SplitPlanItem("a", (_draft(a1), _draft(a2))), (a1, a2)
    elif kind == "ADD_ITEM":
        operation, current = AddPlanItem(_draft(b)), (a, b)
    elif kind == "CHANGE_DEPENDENCY":
        operation, current = UpdatePlanItemDependencies("b", ("a",)), (
            a, replace(b, blocked_by=("a",)),
        )
    else:
        operation, current = TombstonePlanItem("b"), (a,)
    policy = _policy(kind, operation, VerifiedSubtaskDAGConfig(current))
    initial = runtime.get_current_plan_revision(task_id)
    outcome = RecoveryPlanRevisionController(runtime, policy).process(task_id, signal_id)
    assert outcome.outcome == "accepted"
    assert outcome.result_revision_id == runtime.get_current_plan_revision(task_id).revision_id
    assert runtime.get_current_plan_revision(task_id).parent_revision_id == initial.revision_id
    assert runtime.store.get_task(task_id)["execution_plan_revision_id"] == initial.revision_id
    assert len(runtime.store.list_plan_revisions(task_id)) == 2
    assert runtime.store.list_replan_decisions(task_id)[0]["result_revision_id"] == outcome.result_revision_id
    assert RecoveryPlanRevisionController(runtime, policy).process(task_id, signal_id) == outcome
    assert len(runtime.store.list_plan_revisions(task_id)) == 2
    trace = TraceReporter(runtime.store).summary(task_id)
    assert trace["replan_decision_types"] == [kind]
    assert trace["replan_unnecessary_revision_count"] == 0
    runtime.store.assert_invariants(task_id)


def test_invalid_patch_and_missing_capability_reject_without_plan_change(tmp_path):
    a, b = _node("a"), _node("b")
    runtime, task_id = _setup(tmp_path, (a, b))
    invalid = _policy("TOMBSTONE_PENDING", TombstonePlanItem("a"),
                      VerifiedSubtaskDAGConfig((b,)))
    item = runtime.store.get_plan_item(task_id, "a")
    runtime.store.start_plan_item(item["plan_item_id"])
    first = _signal(runtime, task_id)
    revision = runtime.get_current_plan_revision(task_id)
    checkpoint = runtime.store.get_task(task_id)["checkpoint_id"]
    rejected = RecoveryPlanRevisionController(runtime, invalid).process(task_id, first)
    assert rejected.outcome == "rejected"
    assert rejected.result_revision_id is None
    assert runtime.get_current_plan_revision(task_id) == revision
    assert runtime.store.get_task(task_id)["checkpoint_id"] == checkpoint
    assert RecoveryPlanRevisionController(runtime, invalid).process(task_id, first) == rejected
    second = _signal(runtime, task_id)
    missing = _policy("ADD_ITEM", AddPlanItem(_draft(_node("extra"))))
    rejected2 = RecoveryPlanRevisionController(runtime, missing).process(task_id, second)
    assert rejected2.outcome == "rejected"
    assert len(runtime.store.list_plan_revisions(task_id)) == 1
    assert TraceReporter(runtime.store).summary(task_id)["replan_rejected_count"] == 2
    runtime.store.assert_invariants(task_id)


def test_stale_and_foreign_signals_fail_closed(tmp_path):
    a, b = _node("a"), _node("b")
    runtime, task_id = _setup(tmp_path, (a,))
    old = _signal(runtime, task_id)
    before = runtime.get_current_plan_revision(task_id)
    runtime.apply_plan_patch(task_id, before.revision_id, PlanPatch(
        "change before decision", "operator", (AddPlanItem(_draft(b)),),
    ))
    with pytest.raises(StaleState, match="predates"):
        RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(task_id, old)
    other_id = "task-foreign"
    runtime.store.bootstrap_task(other_id, str(tmp_path.resolve()), "Other", "scripted",
                                 [{"role": "user", "content": "Other"}], {"turn": 0})
    runtime.store.create_plan(other_id, [{
        "subtask_id": "a", "description": a.description,
        "verifier_bundle_hash": a.verifier_bundle_hash, "max_turns": a.max_turns,
    }], dag_hash=VerifiedSubtaskDAGConfig((a,)).dag_hash)
    foreign = _signal(runtime, other_id)
    with pytest.raises(ValueError, match="foreign"):
        RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(task_id, foreign)
    assert not runtime.store.list_replan_decisions(task_id)
    assert len(runtime.store.list_plan_revisions(task_id)) == 2


@pytest.mark.parametrize("terminal", [False, True])
def test_retry_rejects_exhausted_budget_and_terminal_task(tmp_path, terminal):
    a = _node("a", max_turns=1)
    runtime, task_id = _setup(tmp_path, (a,))
    item = runtime.store.get_plan_item(task_id, "a")
    runtime.store.start_plan_item(item["plan_item_id"])
    assert runtime.store.reserve_plan_item_turn(item["plan_item_id"]) == 1
    runtime.store.fail_plan_item(item["plan_item_id"], "budget")
    runtime.store.mark_plan_item_retryable(item["plan_item_id"])
    if terminal:
        checkpoint = runtime.store.get_checkpoint(runtime.store.get_task(task_id)["checkpoint_id"])
        runtime.store.fail_task(task_id, checkpoint["messages"], checkpoint["cursor"], "terminal")
    signal_id = _signal(runtime, task_id)
    before = runtime.store.get_plan_item(task_id, "a")
    result = RecoveryPlanRevisionController(runtime, _policy("RETRY")).process(task_id, signal_id)
    assert result.outcome == "rejected"
    assert runtime.store.get_plan_item(task_id, "a")["consumed_turns"] == before["consumed_turns"] == 1
    assert runtime.store.get_task(task_id)["status"] == ("failed" if terminal else "created")
    assert len(runtime.store.list_plan_revisions(task_id)) == 1
    runtime.store.assert_invariants(task_id)


def test_invalid_dependency_and_stale_policy_revision_cannot_commit(tmp_path):
    a, b = _node("a"), _node("b")
    runtime, task_id = _setup(tmp_path, (a, b))
    signal_id = _signal(runtime, task_id)
    cycle = _policy("CHANGE_DEPENDENCY",
                    UpdatePlanItemDependencies("a", ("a",)),
                    VerifiedSubtaskDAGConfig((a, b)))
    rejected = RecoveryPlanRevisionController(runtime, cycle).process(task_id, signal_id)
    assert rejected.outcome == "rejected"
    before = runtime.get_current_plan_revision(task_id)
    newer = runtime.apply_plan_patch(task_id, before.revision_id, PlanPatch(
        "add later", "operator", (AddPlanItem(_draft(_node("extra"))),),
    ))
    fresh_signal = _signal(runtime, task_id)
    def stale(signal, revision, item):
        return ReplanDecision("KEEP", "old revision", before.revision_id)
    with pytest.raises(StaleState, match="different PlanRevision"):
        RecoveryPlanRevisionController(runtime, stale).process(task_id, fresh_signal)
    assert runtime.get_current_plan_revision(task_id) == newer
    assert len(runtime.store.list_replan_decisions(task_id)) == 1
    runtime.store.assert_invariants(task_id)


def test_old_verifier_failure_cannot_replan_completed_work(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"artifact")
    count = 0
    base = _node("a", max_turns=2)
    def verify(context):
        nonlocal count
        count += 1
        if count == 1:
            return VerifierResult("fail", "first attempt failed", [])
        return VerifierResult("pass", "later attempt passed", [{
            "path": "a.txt", "sha256": hashlib.sha256(b"artifact").hexdigest(),
        }])
    node = replace(base, verifier=verify)
    runtime = Runtime(tmp_path, ScriptedModel([
        ModelResponse(text="a attempt 1\nSUBTASK_COMPLETE"),
        ModelResponse(text="a attempt 2\nSUBTASK_COMPLETE"),
    ]), verified_subtask_dag=VerifiedSubtaskDAGConfig((node,)))
    result = runtime.run("Build")
    assert result.status == "completed"
    old_failure = next(run for run in runtime.store.list_verifier_runs(result.task_id)
                       if run["status"] == "fail")
    with pytest.raises(StaleState, match="already completed"):
        RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(
            result.task_id, old_failure["verifier_run_id"],
        )
    assert not runtime.store.list_replan_decisions(result.task_id)
    assert len(runtime.store.list_plan_revisions(result.task_id)) == 1


def test_older_failed_verifier_attempt_is_stale_on_same_revision(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    item = runtime.store.get_plan_item(task_id, "a")
    checkpoint_id = runtime.store.get_task(task_id)["checkpoint_id"]
    failures = []
    for attempt in (1, 2):
        runtime.store.start_plan_item(item["plan_item_id"])
        runtime.store.submit_plan_item_for_verification(item["plan_item_id"])
        failures.append(runtime.store.record_non_authoritative_verifier_run(
            task_id=task_id, plan_item_id=item["plan_item_id"], subtask_id="a",
            status="fail", summary=f"attempt {attempt} failed",
            completion_summary=f"attempt {attempt}", evidence_manifest=[],
            verifier_id=a.verifier_id, verifier_version=a.verifier_version,
            verification_rule=a.verification_rule,
            verifier_bundle_hash=a.verifier_bundle_hash,
            verifier_implementation_hash=a.verifier_implementation_hash,
            execution_checkpoint_id=checkpoint_id,
        ))
    revision = runtime.get_current_plan_revision(task_id)
    with pytest.raises(StaleState, match="superseded by a later attempt"):
        RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(task_id, failures[0])
    assert not runtime.store.list_replan_decisions(task_id)
    accepted = RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(task_id, failures[1])
    assert accepted.outcome == "accepted"
    assert runtime.get_current_plan_revision(task_id) == revision
    runtime.store.assert_invariants(task_id)


def test_verifier_failure_stales_after_later_attempt_without_new_verifier_run(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    item = runtime.store.get_plan_item(task_id, "a")
    runtime.store.start_plan_item(item["plan_item_id"])
    runtime.store.submit_plan_item_for_verification(item["plan_item_id"])
    signal_id = runtime.store.record_non_authoritative_verifier_run(
        task_id=task_id, plan_item_id=item["plan_item_id"], subtask_id="a",
        status="fail", summary="first verifier failure", completion_summary="first",
        evidence_manifest=[], verifier_id=a.verifier_id,
        verifier_version=a.verifier_version, verification_rule=a.verification_rule,
        verifier_bundle_hash=a.verifier_bundle_hash,
        verifier_implementation_hash=a.verifier_implementation_hash,
        execution_checkpoint_id=runtime.store.get_task(task_id)["checkpoint_id"],
    )
    first_version = runtime.store.get_plan_item(task_id, "a")["version"]
    runtime.store.start_plan_item(item["plan_item_id"])
    runtime.store.fail_plan_item(item["plan_item_id"], "later execution failure")
    runtime.store.mark_plan_item_retryable(item["plan_item_id"])
    assert runtime.store.get_plan_item(task_id, "a")["status"] == "retryable"
    assert runtime.store.get_plan_item(task_id, "a")["version"] > first_version
    assert len(runtime.store.list_verifier_runs(task_id)) == 1
    with pytest.raises(StaleState, match="state has changed"):
        RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(task_id, signal_id)
    assert not runtime.store.list_replan_decisions(task_id)


def test_legacy_unbound_verifier_signal_does_not_block_completed_c1b_resume(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"artifact")
    attempts = 0
    a = _node("a", max_turns=2)
    def verifier(context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return VerifierResult("fail", "old failure", [])
        return VerifierResult("pass", "completed", [{
            "path": "a.txt", "sha256": hashlib.sha256(b"artifact").hexdigest(),
        }])
    a = replace(a, verifier=verifier)
    first = Runtime(tmp_path, ScriptedModel([
        ModelResponse(text="first\nSUBTASK_COMPLETE"),
        ModelResponse(text="second\nSUBTASK_COMPLETE"),
    ]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)))
    result = first.run("Build")
    assert result.status == "completed"
    old_run = next(row for row in first.store.list_verifier_runs(result.task_id)
                   if row["status"] == "fail")
    with sqlite3.connect(first.store.path) as conn:
        conn.execute("DROP TABLE replan_decisions")
        conn.execute("ALTER TABLE verifier_runs DROP COLUMN observed_plan_item_version")
        conn.execute("UPDATE schema_migrations SET version = 14, "
                     "name = 'v14_plan_revision_resume', checksum = ?", (V14_CHECKSUM,))
    assert SchemaManager(first.store.path).migrate().to_version == 15
    assert first.store.get_verifier_run(old_run["verifier_run_id"])["observed_plan_item_version"] is None
    resumed = Runtime(tmp_path, ScriptedModel([]),
                      verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                      replan_policy=_policy("KEEP"))
    assert resumed.resume(result.task_id).status == "completed"
    with pytest.raises(ValueError, match="lacks a durable PlanItem attempt binding"):
        RecoveryPlanRevisionController(resumed, _policy("KEEP")).process(
            result.task_id, old_run["verifier_run_id"],
        )


@pytest.mark.parametrize("fault_point", ["replan_after_verifier_signal",
                                        "replan_after_failure_signal"])
def test_restart_drains_signal_committed_before_controller_entry(tmp_path, fault_point):
    a = _node("a", max_turns=1 if fault_point.endswith("failure_signal") else 3)
    a1, a2 = _node("a1"), _node("a2")
    for name in ("a", "a1", "a2"):
        (tmp_path / f"{name}.txt").write_bytes(b"artifact")
    if fault_point == "replan_after_verifier_signal":
        a = replace(a, verifier=lambda _: VerifierResult("fail", "not ready", []))
        response = ModelResponse(text="a done\nSUBTASK_COMPLETE")
        expected_source = "verifier_run"
    else:
        response = ModelResponse(text="budget exhausted")
        expected_source = "plan_item_failed"
    proposed = VerifiedSubtaskDAGConfig((a1, a2))
    def policy(signal, revision, item):
        assert signal.signal_type == expected_source
        return ReplanDecision(
            "SPLIT", "durable failed work", revision.revision_id,
            PlanPatch("split after interrupted signal", "c1c_controller",
                      (SplitPlanItem("a", (_draft(a1), _draft(a2))),),
                      (signal.signal_id,)), proposed,
        )
    def crash(point, **_):
        if point == fault_point:
            raise InjectedCrash(point)
    first = Runtime(tmp_path, ScriptedModel([response]),
                    verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                    replan_policy=policy, fault_injector=crash)
    with pytest.raises(InjectedCrash, match=fault_point):
        first.run("Build")
    task_id = first.store.list_tasks()[0]["task_id"]
    assert not first.store.list_replan_decisions(task_id)
    old_config = Runtime(tmp_path, ScriptedModel([]),
                         verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                         replan_policy=policy)
    result = old_config.resume(task_id)
    assert result.status == "replan_pending"
    assert len(old_config.store.list_replan_decisions(task_id)) == 1
    assert old_config.store.list_replan_decisions(task_id)[0]["signal_type"] == expected_source
    resumed = Runtime(tmp_path, ScriptedModel([
        ModelResponse(text="a1 done\nSUBTASK_COMPLETE"),
        ModelResponse(text="a2 done\nSUBTASK_COMPLETE"),
    ]), verified_subtask_dag=proposed)
    assert resumed.resume(task_id).status == "completed"
    assert len(resumed.store.list_replan_decisions(task_id)) == 1
    resumed.store.assert_invariants(task_id)


def test_observation_requires_evidence_and_exact_item_version(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    controller = RecoveryPlanRevisionController(runtime, _policy("KEEP"))
    with pytest.raises(ValueError, match="evidence references"):
        controller.record_observation(task_id, "a", "unsupported", ())
    item = runtime.store.get_plan_item(task_id, "a")
    malformed = runtime.store.append_event(task_id, "replan_observation", {
        "plan_item_id": item["plan_item_id"], "subtask_id": "a",
        "reason": "unsupported", "observed_revision_id": runtime.get_current_plan_revision(task_id).revision_id,
        "observed_item_version": item["version"],
    })
    with pytest.raises(ValueError, match="lacks durable evidence"):
        controller.process(task_id, f"event:{malformed}")
    valid = _signal(runtime, task_id)
    event = next(e for e in runtime.store.list_events(task_id) if e["event_id"] == int(valid[6:]))
    assert event["payload"]["evidence_refs"] == [f"fixture:{task_id}:a"]
    runtime.store.start_plan_item(item["plan_item_id"])
    with pytest.raises(StaleState, match="state has changed"):
        controller.process(task_id, valid)
    assert runtime.store.list_replan_decisions(task_id) == []


def test_older_plan_item_failure_event_is_stale_on_same_revision(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    item = runtime.store.get_plan_item(task_id, "a")
    failures = []
    for attempt in (1, 2):
        runtime.store.start_plan_item(item["plan_item_id"])
        runtime.store.fail_plan_item(item["plan_item_id"], f"attempt {attempt}")
        event = next(event for event in reversed(runtime.store.list_events(task_id))
                     if event["type"] == "plan_item_failed")
        failures.append(f"event:{event['event_id']}")
        runtime.store.mark_plan_item_retryable(item["plan_item_id"])
    assert runtime.get_current_plan_revision(task_id).revision_number == 0
    with pytest.raises(StaleState, match="attempt has changed"):
        RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(task_id, failures[0])
    assert RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(
        task_id, failures[1]
    ).outcome == "accepted"
    assert len(runtime.store.list_replan_decisions(task_id)) == 1
    runtime.store.assert_invariants(task_id)


def test_legacy_failure_event_without_state_binding_fails_closed(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    item = runtime.store.get_plan_item(task_id, "a")
    old = runtime.store.append_event(task_id, "plan_item_failed", {
        "plan_item_id": item["plan_item_id"], "reason": "legacy failure",
    })
    with pytest.raises(ValueError, match="lacks a durable revision/item-state binding"):
        RecoveryPlanRevisionController(runtime, _policy("KEEP")).process(task_id, f"event:{old}")
    assert not runtime.store.list_replan_decisions(task_id)


def test_task_failure_signal_requires_revision_and_task_version(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    checkpoint = runtime.store.get_checkpoint(runtime.store.get_task(task_id)["checkpoint_id"])
    runtime.store.fail_task(task_id, checkpoint["messages"], checkpoint["cursor"], "fixed task failure")
    event = next(e for e in reversed(runtime.store.list_events(task_id))
                 if e["type"] == "task_failed")
    original = runtime.get_current_plan_revision(task_id)
    assert event["payload"]["observed_revision_id"] == original.revision_id
    assert event["payload"]["observed_task_version"] == runtime.store.get_task(task_id)["version"]
    signal_id = f"event:{event['event_id']}"
    runtime.store.update_task(task_id, status="failed")
    with pytest.raises(StaleState, match="task failure state has changed"):
        RecoveryPlanRevisionController(runtime, _policy("FAIL")).process(task_id, signal_id)
    legacy = runtime.store.append_event(task_id, "task_failed", {"error": "old schema"})
    with pytest.raises(ValueError, match="lacks a durable revision/task-state binding"):
        RecoveryPlanRevisionController(runtime, _policy("FAIL")).process(task_id, f"event:{legacy}")
    assert not runtime.store.list_replan_decisions(task_id)


def test_current_task_failure_signal_records_fail_without_revision(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    checkpoint = runtime.store.get_checkpoint(runtime.store.get_task(task_id)["checkpoint_id"])
    runtime.store.fail_task(task_id, checkpoint["messages"], checkpoint["cursor"], "fixed task failure")
    event = next(e for e in reversed(runtime.store.list_events(task_id))
                 if e["type"] == "task_failed")
    revision = runtime.get_current_plan_revision(task_id)
    result = RecoveryPlanRevisionController(runtime, _policy("FAIL")).process(
        task_id, f"event:{event['event_id']}",
    )
    assert result.outcome == "accepted" and result.result_revision_id is None
    assert runtime.get_current_plan_revision(task_id) == revision
    runtime.store.assert_invariants(task_id)


def test_restart_drains_pending_task_failure_signal(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    checkpoint = runtime.store.get_checkpoint(runtime.store.get_task(task_id)["checkpoint_id"])
    runtime.store.fail_task(task_id, checkpoint["messages"], checkpoint["cursor"], "durable failure")
    restarted = Runtime(tmp_path, ScriptedModel([]),
                        verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                        replan_policy=_policy("FAIL"))
    result = restarted.resume(task_id)
    assert result.status == "failed"
    decisions = restarted.store.list_replan_decisions(task_id)
    assert len(decisions) == 1 and decisions[0]["signal_type"] == "task_failed"
    assert decisions[0]["result_revision_id"] is None
    restarted.store.assert_invariants(task_id)


@pytest.mark.parametrize("signal_type", ["replan_observation", "verified_subtask_checkpoint_stale"])
def test_restart_drains_pending_observation_or_stale_evidence(tmp_path, signal_type):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    item = runtime.store.get_plan_item(task_id, "a")
    if signal_type == "replan_observation":
        signal_id = _signal(runtime, task_id)
    else:
        event_id = runtime.store.append_event(task_id, signal_type, {
            "plan_item_id": item["plan_item_id"], "subtask_id": "a",
            "observed_revision_id": runtime.get_current_plan_revision(task_id).revision_id,
            "observed_item_version": item["version"], "reason": "fixed stale evidence",
        })
        signal_id = f"event:{event_id}"
    restarted = Runtime(tmp_path, ScriptedModel([ModelResponse(text="a done\nSUBTASK_COMPLETE")]),
                        verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                        replan_policy=_policy("KEEP"))
    assert restarted.resume(task_id).status == "completed"
    decisions = restarted.store.list_replan_decisions(task_id)
    assert len(decisions) == 1
    assert decisions[0]["signal_id"] == signal_id
    assert decisions[0]["signal_type"] == signal_type
    assert len(restarted.store.list_plan_revisions(task_id)) == 1
    restarted.store.assert_invariants(task_id)


@pytest.mark.parametrize("kind", ["ADD_ITEM", "CHANGE_DEPENDENCY", "TOMBSTONE_PENDING"])
def test_failed_task_rejects_structural_patch_that_c1b_cannot_activate(tmp_path, kind):
    a, b, extra = _node("a"), _node("b"), _node("extra")
    runtime, task_id = _setup(tmp_path, (a, b))
    item = runtime.store.get_plan_item(task_id, "a")
    runtime.store.start_plan_item(item["plan_item_id"])
    checkpoint = runtime.store.get_checkpoint(runtime.store.get_task(task_id)["checkpoint_id"])
    runtime.store.fail_plan_item_and_task(task_id, item["plan_item_id"],
                                          checkpoint["messages"], checkpoint["cursor"], "exhausted")
    failure = next(e for e in reversed(runtime.store.list_events(task_id))
                   if e["type"] == "plan_item_failed")
    signal_id = f"event:{failure['event_id']}"
    if kind == "ADD_ITEM":
        op, proposed = AddPlanItem(_draft(extra)), (a, b, extra)
    elif kind == "CHANGE_DEPENDENCY":
        op, proposed = UpdatePlanItemDependencies("b", ("a",)), (
            a, replace(b, blocked_by=("a",)),
        )
    else:
        op, proposed = TombstonePlanItem("b"), (a,)
    old_revision = runtime.get_current_plan_revision(task_id)
    old_task = runtime.store.get_task(task_id)
    result = RecoveryPlanRevisionController(
        runtime, _policy(kind, op, VerifiedSubtaskDAGConfig(proposed))
    ).process(task_id, signal_id)
    assert result.outcome == "rejected"
    assert "active failed" in result.rejection_reason
    assert runtime.get_current_plan_revision(task_id) == old_revision
    assert runtime.store.get_task(task_id) == old_task
    assert len(runtime.store.list_plan_revisions(task_id)) == 1
    runtime.store.assert_invariants(task_id)


def test_failed_task_split_then_c1b_activation_resumes_replacements(tmp_path):
    a, a1, a2 = _node("a"), _node("a1"), _node("a2")
    runtime, task_id = _setup(tmp_path, (a,))
    item = runtime.store.get_plan_item(task_id, "a")
    runtime.store.start_plan_item(item["plan_item_id"])
    checkpoint = runtime.store.get_checkpoint(runtime.store.get_task(task_id)["checkpoint_id"])
    runtime.store.fail_plan_item_and_task(task_id, item["plan_item_id"],
                                          checkpoint["messages"], checkpoint["cursor"], "exhausted")
    failure = next(e for e in reversed(runtime.store.list_events(task_id))
                   if e["type"] == "plan_item_failed")
    signal_id = f"event:{failure['event_id']}"
    decision = _policy("SPLIT", SplitPlanItem("a", (_draft(a1), _draft(a2))),
                       VerifiedSubtaskDAGConfig((a1, a2)))
    result = RecoveryPlanRevisionController(runtime, decision).process(task_id, signal_id)
    assert result.outcome == "accepted"
    assert runtime.store.get_task(task_id)["status"] == "failed"
    resumed = Runtime(tmp_path, ScriptedModel([
        ModelResponse(text="a1 done\nSUBTASK_COMPLETE"),
        ModelResponse(text="a2 done\nSUBTASK_COMPLETE"),
    ]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a1, a2)))
    assert resumed.resume(task_id).status == "completed"
    assert resumed.store.get_task(task_id)["execution_plan_revision_id"] == result.result_revision_id
    assert resumed.store.get_plan_item(task_id, "a")["tombstoned"] == 1
    resumed.store.assert_invariants(task_id)


@pytest.mark.parametrize("point,committed", [("replan_before_commit", False),
                                             ("replan_after_commit", True)])
def test_crash_has_old_or_new_decision_and_revision(tmp_path, point, committed):
    a, b = _node("a"), _node("b")
    runtime, task_id = _setup(tmp_path, (a,))
    signal_id = _signal(runtime, task_id)
    original = runtime.get_current_plan_revision(task_id)
    def crash(actual, **_):
        if actual == point:
            raise InjectedCrash(point)
    runtime.fault_injector = crash
    policy = _policy("ADD_ITEM", AddPlanItem(_draft(b)), VerifiedSubtaskDAGConfig((a, b)))
    with pytest.raises(InjectedCrash, match=point):
        RecoveryPlanRevisionController(runtime, policy).process(task_id, signal_id)
    fresh = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a, b)))
    assert len(fresh.store.list_plan_revisions(task_id)) == (2 if committed else 1)
    assert len(fresh.store.list_replan_decisions(task_id)) == (1 if committed else 0)
    assert fresh.store.get_task(task_id)["execution_plan_revision_id"] == original.revision_id
    outcome = RecoveryPlanRevisionController(fresh, policy).process(task_id, signal_id)
    assert outcome.outcome == "accepted"
    assert len(fresh.store.list_replan_decisions(task_id)) == 1
    assert len(fresh.store.list_plan_revisions(task_id)) == 2
    fresh.store.assert_invariants(task_id)


def test_verifier_signal_drives_patch_then_c1b_resume_without_repeating_work(tmp_path):
    verified = []
    for name in ("a", "a1", "a2"):
        (tmp_path / f"{name}.txt").write_bytes(b"artifact")
    failed = _node("a", max_turns=1, verdict="fail")
    a1, a2 = _node("a1"), _node("a2")
    def policy(signal, revision, item):
        assert signal.signal_type == "verifier_run"
        patch = PlanPatch(
            "split failed verifier work", "c1c_controller",
            (SplitPlanItem("a", (_draft(a1), _draft(a2))),),
            (signal.signal_id,),
        )
        return ReplanDecision("SPLIT", "failed verifier", revision.revision_id,
                              patch, VerifiedSubtaskDAGConfig((a1, a2)))
    initial = Runtime(
        tmp_path, ScriptedModel([ModelResponse(text="a done\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=VerifiedSubtaskDAGConfig((failed,)), replan_policy=policy,
    )
    outcome = initial.run("Build")
    assert outcome.status == "replan_pending"
    assert len(initial.store.list_verifier_runs(outcome.task_id)) == 1
    assert len(initial.store.list_replan_decisions(outcome.task_id)) == 1
    assert initial.store.get_task(outcome.task_id)["execution_plan_revision_id"] != (
        initial.get_current_plan_revision(outcome.task_id).revision_id
    )
    resumed_model = ScriptedModel([
        ModelResponse(text="a1 done\nSUBTASK_COMPLETE"),
        ModelResponse(text="a2 done\nSUBTASK_COMPLETE"),
    ])
    resumed = Runtime(tmp_path, resumed_model,
                      verified_subtask_dag=VerifiedSubtaskDAGConfig((a1, a2)))
    assert resumed.resume(outcome.task_id).status == "completed"
    assert resumed_model.call_count == 2
    assert len(resumed.store.list_verifier_runs(outcome.task_id)) == 3
    assert resumed.store.get_plan_item(outcome.task_id, "a")["tombstoned"] == 1
    assert resumed.store.get_task(outcome.task_id)["execution_plan_revision_id"] == (
        resumed.get_current_plan_revision(outcome.task_id).revision_id
    )
    assert resumed.resume(outcome.task_id).status == "completed"
    assert resumed_model.call_count == 2
    resumed.store.assert_invariants(outcome.task_id)


def test_budget_failure_signal_automatically_splits_then_c1b_resumes(tmp_path):
    a, a1, a2 = _node("a", max_turns=1), _node("a1"), _node("a2")
    for name in ("a", "a1", "a2"):
        (tmp_path / f"{name}.txt").write_bytes(b"artifact")
    def policy(signal, revision, item):
        assert signal.signal_type == "plan_item_failed"
        return ReplanDecision(
            "SPLIT", "budget exhausted", revision.revision_id,
            PlanPatch("split budget failure", "c1c_controller",
                      (SplitPlanItem("a", (_draft(a1), _draft(a2))),), (signal.signal_id,)),
            VerifiedSubtaskDAGConfig((a1, a2)),
        )
    first = Runtime(tmp_path, ScriptedModel([ModelResponse(text="not done")]),
                    verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                    replan_policy=policy)
    result = first.run("Build")
    assert result.status == "replan_pending"
    assert first.store.get_task(result.task_id)["status"] == "failed"
    decision = first.store.list_replan_decisions(result.task_id)[0]
    assert decision["signal_type"] == "plan_item_failed"
    assert decision["decision_type"] == "SPLIT"
    assert first.store.get_plan_item(result.task_id, "a")["consumed_turns"] == 1
    resumed = Runtime(tmp_path, ScriptedModel([
        ModelResponse(text="a1 done\nSUBTASK_COMPLETE"),
        ModelResponse(text="a2 done\nSUBTASK_COMPLETE"),
    ]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a1, a2)))
    assert resumed.resume(result.task_id).status == "completed"
    assert len(resumed.store.list_replan_decisions(result.task_id)) == 1
    assert len(resumed.store.list_plan_revisions(result.task_id)) == 2
    resumed.store.assert_invariants(result.task_id)


def test_budget_failure_retry_policy_cannot_reopen_terminal_task(tmp_path):
    a = _node("a", max_turns=1)
    (tmp_path / "a.txt").write_bytes(b"artifact")
    first = Runtime(tmp_path, ScriptedModel([ModelResponse(text="not done")]),
                    verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                    replan_policy=_policy("RETRY"))
    result = first.run("Build")
    assert result.status == "failed"
    assert "replan rejected" in (result.error or "")
    assert first.store.get_task(result.task_id)["status"] == "failed"
    assert first.store.get_plan_item(result.task_id, "a")["consumed_turns"] == 1
    assert len(first.store.list_plan_revisions(result.task_id)) == 1
    assert first.store.list_replan_decisions(result.task_id)[0]["outcome"] == "rejected"
    first.store.assert_invariants(result.task_id)


def test_v14_to_v15_migration_preserves_revision_bindings(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    binding = runtime.store.get_task(task_id)["execution_plan_revision_id"]
    with sqlite3.connect(runtime.store.path) as conn:
        conn.execute("DROP TABLE replan_decisions")
        conn.execute("ALTER TABLE verifier_runs DROP COLUMN observed_plan_item_version")
        conn.execute("UPDATE schema_migrations SET version = 14, "
                     "name = 'v14_plan_revision_resume', checksum = ?",
                     (V14_CHECKSUM,))
    report = SchemaManager(runtime.store.path).migrate()
    assert report.applied == ("v15_replan_decisions",)
    fresh = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)))
    assert fresh.store.get_task(task_id)["execution_plan_revision_id"] == binding
    assert fresh.store.list_replan_decisions(task_id) == []
    fresh.store.assert_invariants(task_id)


def test_v14_to_v15_migration_rolls_back_before_commit(tmp_path):
    a = _node("a")
    runtime, task_id = _setup(tmp_path, (a,))
    with sqlite3.connect(runtime.store.path) as conn:
        conn.execute("DROP TABLE replan_decisions")
        conn.execute("ALTER TABLE verifier_runs DROP COLUMN observed_plan_item_version")
        conn.execute("UPDATE schema_migrations SET version = 14, "
                     "name = 'v14_plan_revision_resume', checksum = ?", (V14_CHECKSUM,))
    def fail(point, **_):
        if point == "before_migration_commit":
            raise InjectedCrash(point)
    with pytest.raises(InjectedCrash):
        SchemaManager(runtime.store.path, fault_injector=fail).migrate()
    assert SchemaManager(runtime.store.path).inspect().current_version == 14
    with sqlite3.connect(runtime.store.path) as conn:
        assert "replan_decisions" not in {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert SchemaManager(runtime.store.path).migrate().to_version == 15
    assert runtime.store.get_task(task_id)["execution_plan_revision_id"] is not None


@pytest.mark.parametrize("point,committed", [("replan_before_commit", False),
                                             ("replan_after_commit", True)])
def test_real_process_exit_preserves_atomic_decision_and_patch(tmp_path, point, committed):
    a, b = _node("a"), _node("b")
    runtime, task_id = _setup(tmp_path, (a,))
    signal_id = _signal(runtime, task_id)
    initial = runtime.get_current_plan_revision(task_id)
    script = r'''
import os,sys,hashlib
from agent_runtime import AddPlanItem, PlanPatch, Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult, PlanItemDraft
from agent_runtime.replanning import RecoveryPlanRevisionController, ReplanDecision
def node(name):
    return VerifiedSubtaskConfig(
        subtask_id=name, description=f'Complete {name}',
        completion_criteria=f'{name} is complete', evidence_paths=(f'{name}.txt',),
        verifier_id=f'verify-{name}', verifier_version='1',
        verification_rule=f'rule-{name}', verifier_implementation_hash='b'*64,
        verifier=lambda _: VerifierResult('pass', name, [{
            'path': f'{name}.txt', 'sha256': hashlib.sha256(b'artifact').hexdigest(),
        }]), blocked_by=(), max_turns=3,
    )
a,b=node('a'),node('b')
def _draft(n):
    return PlanItemDraft(n.subtask_id,n.description,n.blocked_by,n.verifier_bundle_hash,n.max_turns)
def inject(point,**_):
    if point==sys.argv[4]: os._exit(79)
runtime=Runtime(sys.argv[1],ScriptedModel([]),verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),fault_injector=inject)
def policy(signal,revision,item):
    patch=PlanPatch('hard exit','c1c_controller',(AddPlanItem(_draft(b)),),(signal.signal_id,))
    return ReplanDecision('ADD_ITEM','hard exit',revision.revision_id,patch,VerifiedSubtaskDAGConfig((a,b)))
RecoveryPlanRevisionController(runtime,policy).process(sys.argv[2],sys.argv[3])
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), task_id, signal_id, point],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 79, result.stderr
    assert len(runtime.store.list_replan_decisions(task_id)) == (1 if committed else 0)
    assert len(runtime.store.list_plan_revisions(task_id)) == (2 if committed else 1)
    assert runtime.store.get_task(task_id)["execution_plan_revision_id"] == initial.revision_id
    with sqlite3.connect(runtime.store.path) as conn:
        conn.execute("UPDATE leases SET expires_at = 0")
    fresh = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)))
    policy = _policy("ADD_ITEM", AddPlanItem(_draft(b)), VerifiedSubtaskDAGConfig((a, b)))
    outcome = RecoveryPlanRevisionController(fresh, policy).process(task_id, signal_id)
    assert outcome.outcome == "accepted"
    assert len(fresh.store.list_replan_decisions(task_id)) == 1
    assert len(fresh.store.list_plan_revisions(task_id)) == 2
    fresh.store.assert_invariants(task_id)


def test_replanning_preserves_committed_file_effect_across_restart(tmp_path):
    a, b = _node("a"), _node("b", ("a",))
    (tmp_path / "a.txt").write_bytes(b"artifact")
    (tmp_path / "b.txt").write_bytes(b"artifact")
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("rules:\n  - id: write\n    effect: allow\n    tools: [write_file]\n    paths: ['effect.txt']\n", encoding="utf-8")
    def fault(point, **_):
        if point == "after_tool_effect_before_persist":
            raise InjectedCrash(point)
    first = Runtime(
        tmp_path, ScriptedModel([ModelResponse(tool_calls=[
            ToolCall("effect", "write_file", {"path": "effect.txt", "content": "once"}),
        ])]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
        policy_path=policy_file, fault_injector=fault,
    )
    with pytest.raises(InjectedCrash):
        first.run("Build")
    task_id = first.store.list_tasks()[0]["task_id"]
    signal_id = _signal(first, task_id)
    first.fault_injector = None
    decision = _policy("ADD_ITEM", AddPlanItem(_draft(b)), VerifiedSubtaskDAGConfig((a, b)))
    assert RecoveryPlanRevisionController(first, decision).process(task_id, signal_id).outcome == "accepted"
    resumed = Runtime(
        tmp_path, ScriptedModel([ModelResponse(text="a done\nSUBTASK_COMPLETE"),
                                 ModelResponse(text="b done\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=VerifiedSubtaskDAGConfig((a, b)), policy_path=policy_file,
    )
    assert resumed.resume(task_id).status == "completed"
    assert (tmp_path / "effect.txt").read_text(encoding="utf-8") == "once"
    assert resumed.store.get_tool_call(task_id, "effect")["effect_attempts"] == 1
    assert TraceReporter(resumed.store).summary(task_id)["confirmed_duplicate_side_effects"] == 0
    resumed.store.assert_invariants(task_id)


def test_trace_export_and_invariant_detect_broken_decision_revision_link(tmp_path):
    a, b = _node("a"), _node("b")
    runtime, task_id = _setup(tmp_path, (a,))
    signal_id = _signal(runtime, task_id)
    decision = _policy("ADD_ITEM", AddPlanItem(_draft(b)), VerifiedSubtaskDAGConfig((a, b)))
    assert RecoveryPlanRevisionController(runtime, decision).process(task_id, signal_id).outcome == "accepted"
    output = tmp_path / "trace.jsonl"
    TraceReporter(runtime.store).export_jsonl(task_id, output)
    assert '"record_type": "replan_decision"' in output.read_text(encoding="utf-8")
    with sqlite3.connect(runtime.store.path) as conn:
        conn.execute("UPDATE replan_decisions SET result_revision_id = NULL WHERE task_id = ?", (task_id,))
    with pytest.raises(InvariantViolation, match="accepted patch has no matching PlanRevision"):
        runtime.store.assert_invariants(task_id)
