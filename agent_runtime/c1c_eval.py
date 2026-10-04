"""Fixed, credential-free C1c controller evaluation scenarios."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from .fake_model import ScriptedModel
from .models import (
    AddPlanItem, ModelResponse, PlanItemDraft, PlanPatch, SplitPlanItem, ToolCall,
    TombstonePlanItem, UpdatePlanItemDependencies, VerifiedSubtaskConfig,
    VerifiedSubtaskDAGConfig, VerifierResult,
)
from .replanning import RecoveryPlanRevisionController, ReplanDecision
from .runtime import InjectedCrash, Runtime
from .trace import TraceReporter


def _node(name: str, blocked_by: tuple[str, ...] = ()) -> VerifiedSubtaskConfig:
    return VerifiedSubtaskConfig(
        subtask_id=name, description=f"Complete {name}",
        completion_criteria=f"{name} done", evidence_paths=(f"{name}.txt",),
        verifier_id=f"fixed-{name}", verifier_version="1",
        verification_rule=f"fixed-{name}", verifier_implementation_hash="b" * 64,
        verifier=lambda _: VerifierResult(
            "pass", f"{name} verified", [{
                "path": f"{name}.txt", "sha256": hashlib.sha256(b"artifact").hexdigest(),
            }],
        ),
        blocked_by=blocked_by, max_turns=3,
    )


def _draft(node: VerifiedSubtaskConfig) -> PlanItemDraft:
    return PlanItemDraft(node.subtask_id, node.description, node.blocked_by,
                         node.verifier_bundle_hash, node.max_turns)


def _completed_replay_counts(runtime: Runtime, task_id: str) -> tuple[int, int]:
    runs = runtime.store.list_verifier_runs(task_id)
    first_pass: dict[str, float] = {}
    reverification = 0
    for run in runs:
        if not run["authoritative"] or run["status"] != "pass":
            continue
        subtask_id = str(run["subtask_id"])
        if subtask_id in first_pass:
            reverification += 1
        else:
            first_pass[subtask_id] = float(run["created_at"])
    reexecution = sum(
        call["request"].get("active_subtask_id") in first_pass
        and float(call["started_at"]) > first_pass[call["request"]["active_subtask_id"]]
        for call in runtime.store.list_model_calls(task_id)
        if call["request"].get("active_subtask_id") in first_pass
    )
    return reexecution, reverification


def _completed_preservation(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    for name in ("a", "b", "c"):
        (root / f"{name}.txt").write_bytes(b"artifact")
    a, b, c = _node("a"), _node("b", ("a",)), _node("c", ("a",))
    def fault(point, **_):
        if point == "verified_subtask_f3":
            raise InjectedCrash(point)
    initial = Runtime(
        root, ScriptedModel([ModelResponse(text="a done\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=VerifiedSubtaskDAGConfig((a, b)), fault_injector=fault,
    )
    try:
        initial.run("Fixed completed-work preservation")
        raise AssertionError("expected the fixed completion boundary")
    except InjectedCrash:
        pass
    task_id = initial.store.list_tasks()[0]["task_id"]
    signal_id = RecoveryPlanRevisionController(initial, lambda *_: None).record_observation(
        task_id, "a", "add current work", ("fixed:verified-a",),
    )
    def policy(signal, revision, item):
        patch = PlanPatch("add after verified a", "c1c_controller",
                          (AddPlanItem(_draft(c)),), (signal.signal_id,))
        return ReplanDecision("ADD_ITEM", "verified a", revision.revision_id,
                              patch, VerifiedSubtaskDAGConfig((a, b, c)))
    outcome = RecoveryPlanRevisionController(initial, policy).process(task_id, signal_id)
    resumed = Runtime(root, ScriptedModel([
        ModelResponse(text="b done\nSUBTASK_COMPLETE"),
        ModelResponse(text="c done\nSUBTASK_COMPLETE"),
    ]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a, b, c)))
    status = resumed.resume(task_id).status
    reexecution, reverification = _completed_replay_counts(resumed, task_id)
    trace = TraceReporter(resumed.store).summary(task_id)
    passed = (status == "completed" and outcome.outcome == "accepted"
              and reexecution == 0 and reverification == 0
              and sum(run["subtask_id"] == "a" for run in resumed.store.list_verifier_runs(task_id)) == 1
              and resumed.store.scan_invariants(task_id) == [])
    return {
        "id": str(spec["id"]), "task_id": task_id, "status": status,
        "passed": bool(passed), "error": None, "interrupted": True,
        "fault_triggered": True, "recovery_case": True, "evaluation_error": None,
        "decision": "ADD_ITEM", "signal_id": signal_id, "reason": outcome.reason,
        "base_revision_id": outcome.base_revision_id,
        "result_revision_id": outcome.result_revision_id,
        "decision_outcome": outcome.outcome, "rejection_reason": None,
        "expected_rejection": False,
        "revision_count": len(resumed.store.list_plan_revisions(task_id)),
        "completed_reexecution_count": reexecution,
        "completed_reverification_count": reverification, "trace": trace,
    }


def _real_failure_signal(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    scenario = str(spec["scenario"])
    for name in ("a", "b", "a1", "a2"):
        (root / f"{name}.txt").write_bytes(b"artifact")
    a, b, a1, a2 = _node("a"), _node("b"), _node("a1"), _node("a2")
    if scenario == "verifier_failure":
        attempts = 0
        def verify(context):
            nonlocal attempts
            attempts += 1
            return VerifierResult(
                "fail" if attempts == 1 else "pass", "fixed verifier result",
                [] if attempts == 1 else [{
                    "path": "a.txt", "sha256": hashlib.sha256(b"artifact").hexdigest(),
                }],
            )
        a = replace(a, verifier=verify)
        decision_type = "ADD_ITEM"
        proposed = VerifiedSubtaskDAGConfig((a, b))
        initial_response = ModelResponse(text="a first\nSUBTASK_COMPLETE")
        resumed_responses = [ModelResponse(text="a again\nSUBTASK_COMPLETE"),
                             ModelResponse(text="b done\nSUBTASK_COMPLETE")]
    else:
        a = replace(a, max_turns=1)
        decision_type = "SPLIT"
        proposed = VerifiedSubtaskDAGConfig((a1, a2))
        initial_response = ModelResponse(text="budget exhausted")
        resumed_responses = [ModelResponse(text="a1 done\nSUBTASK_COMPLETE"),
                             ModelResponse(text="a2 done\nSUBTASK_COMPLETE")]

    def policy(signal, revision, item):
        if decision_type == "ADD_ITEM":
            operation = AddPlanItem(_draft(b))
        else:
            operation = SplitPlanItem("a", (_draft(a1), _draft(a2)))
        return ReplanDecision(
            decision_type, f"fixed {scenario}", revision.revision_id,
            PlanPatch(f"fixed {scenario}", "c1c_controller", (operation,),
                      (signal.signal_id,)), proposed,
        )
    first = Runtime(root, ScriptedModel([initial_response]),
                    verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                    replan_policy=policy)
    first_result = first.run("Fixed durable failure")
    task_id = first_result.task_id
    decisions = first.store.list_replan_decisions(task_id)
    signal_type = decisions[0]["signal_type"] if decisions else None
    resumed = Runtime(root, ScriptedModel(resumed_responses), verified_subtask_dag=proposed)
    status = resumed.resume(task_id).status if first_result.status == "replan_pending" else first_result.status
    trace = TraceReporter(resumed.store).summary(task_id)
    reexecution, reverification = _completed_replay_counts(resumed, task_id)
    expected_source = "verifier_run" if scenario == "verifier_failure" else "plan_item_failed"
    passed = (first_result.status == "replan_pending" and status == "completed"
              and signal_type == expected_source and len(decisions) == 1
              and len(resumed.store.list_plan_revisions(task_id)) == 2
              and reexecution == 0 and reverification == 0
              and resumed.store.scan_invariants(task_id) == [])
    return {
        "id": str(spec["id"]), "task_id": task_id, "status": status,
        "passed": bool(passed), "error": None, "interrupted": False,
        "fault_triggered": False, "recovery_case": False, "evaluation_error": None,
        "decision": decision_type, "signal_id": decisions[0]["signal_id"] if decisions else None,
        "signal_type": signal_type, "reason": f"fixed {scenario}",
        "base_revision_id": decisions[0]["base_revision_id"] if decisions else None,
        "result_revision_id": decisions[0]["result_revision_id"] if decisions else None,
        "decision_outcome": decisions[0]["outcome"] if decisions else None,
        "rejection_reason": None, "expected_rejection": False,
        "revision_count": len(resumed.store.list_plan_revisions(task_id)),
        "completed_reexecution_count": reexecution,
        "completed_reverification_count": reverification, "trace": trace,
    }


def _committed_effect_recovery(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    a, b = _node("a"), _node("b", ("a",))
    for name in ("a", "b"):
        (root / f"{name}.txt").write_bytes(b"artifact")
    policy_path = root / "policy.yaml"
    policy_path.write_text(
        "rules:\n  - id: fixed-write\n    effect: allow\n"
        "    tools: [write_file]\n    paths: ['effect.txt']\n", encoding="utf-8",
    )
    def crash(point, **_):
        if point == "after_tool_effect_before_persist":
            raise InjectedCrash(point)
    first = Runtime(root, ScriptedModel([ModelResponse(tool_calls=[
        ToolCall("fixed-effect", "write_file", {"path": "effect.txt", "content": "once"}),
    ])]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a,)),
                    policy_path=policy_path, fault_injector=crash)
    try:
        first.run("Fixed effect recovery")
        raise AssertionError("expected fixed effect crash")
    except InjectedCrash:
        pass
    task_id = first.store.list_tasks()[0]["task_id"]
    signal_id = RecoveryPlanRevisionController(first, lambda *_: None).record_observation(
        task_id, "a", "continue after effect", ("fixed:file-effect",),
    )
    def policy(signal, revision, item):
        patch = PlanPatch("add after file effect", "c1c_controller",
                          (AddPlanItem(_draft(b)),), (signal.signal_id,))
        return ReplanDecision("ADD_ITEM", "file effect recovered",
                              revision.revision_id, patch, VerifiedSubtaskDAGConfig((a, b)))
    outcome = RecoveryPlanRevisionController(first, policy).process(task_id, signal_id)
    resumed = Runtime(root, ScriptedModel([
        ModelResponse(text="a done\nSUBTASK_COMPLETE"),
        ModelResponse(text="b done\nSUBTASK_COMPLETE"),
    ]), verified_subtask_dag=VerifiedSubtaskDAGConfig((a, b)), policy_path=policy_path)
    status = resumed.resume(task_id).status
    trace = TraceReporter(resumed.store).summary(task_id)
    reexecution, reverification = _completed_replay_counts(resumed, task_id)
    call = resumed.store.get_tool_call(task_id, "fixed-effect")
    passed = (status == "completed" and outcome.outcome == "accepted"
              and (root / "effect.txt").read_text(encoding="utf-8") == "once"
              and call["effect_attempts"] == 1 and trace["effect_attempts"] == 1
              and trace["confirmed_duplicate_side_effects"] == 0
              and reexecution == 0 and reverification == 0
              and resumed.store.scan_invariants(task_id) == [])
    return {
        "id": str(spec["id"]), "task_id": task_id, "status": status,
        "passed": bool(passed), "error": None, "interrupted": True,
        "fault_triggered": True, "recovery_case": True, "evaluation_error": None,
        "decision": "ADD_ITEM", "signal_id": signal_id,
        "signal_type": "replan_observation", "reason": outcome.reason,
        "base_revision_id": outcome.base_revision_id,
        "result_revision_id": outcome.result_revision_id,
        "decision_outcome": outcome.outcome, "rejection_reason": None,
        "expected_rejection": False,
        "revision_count": len(resumed.store.list_plan_revisions(task_id)),
        "completed_reexecution_count": reexecution,
        "completed_reverification_count": reverification, "trace": trace,
    }


def run_case(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Execute one frozen choice on a disposable Runtime database."""
    kind = str(spec["decision"])
    scenario = str(spec.get("scenario", "normal"))
    if scenario in {"verifier_failure", "execution_failure"}:
        return _real_failure_signal(root, spec)
    if scenario == "committed_effect_recovery":
        return _committed_effect_recovery(root, spec)
    if scenario == "completed_preservation":
        return _completed_preservation(root, spec)
    for name in ("a", "b", "a1", "a2"):
        (root / f"{name}.txt").write_bytes(b"artifact")
    a, b, a1, a2 = _node("a"), _node("b"), _node("a1"), _node("a2")
    initial_nodes = (a, b) if kind in {"CHANGE_DEPENDENCY", "TOMBSTONE_PENDING"} else (a,)
    initial_dag = VerifiedSubtaskDAGConfig(initial_nodes)
    runtime = Runtime(root, ScriptedModel([]), verified_subtask_dag=initial_dag)
    task_id = f"task_{spec['id']}"
    runtime.store.bootstrap_task(task_id, str(root.resolve()), "Fixed C1c task", "scripted",
                                 [{"role": "user", "content": "Fixed C1c task"}], {"turn": 0})
    runtime.store.create_plan(task_id, [{
        "subtask_id": node.subtask_id, "description": node.description,
        "blocked_by": list(node.blocked_by),
        "verifier_bundle_hash": node.verifier_bundle_hash,
        "max_turns": node.max_turns,
    } for node in initial_nodes], dag_hash=initial_dag.dag_hash)
    initial_revision = runtime.get_current_plan_revision(task_id).revision_id
    item = runtime.store.get_plan_item(task_id, "a")
    if kind in {"RETRY", "SPLIT"}:
        runtime.store.start_plan_item(item["plan_item_id"])
        runtime.store.fail_plan_item(item["plan_item_id"], "fixed failure")
        runtime.store.mark_plan_item_retryable(item["plan_item_id"])
    elif scenario == "invalid_patch":
        runtime.store.start_plan_item(item["plan_item_id"])
    signal_id = RecoveryPlanRevisionController(runtime, lambda *_: None).record_observation(
        task_id, "a", "fixed signal", (f"fixed:{spec['id']}",),
    )
    operation = None
    proposed = None
    if kind == "SPLIT":
        operation, proposed = SplitPlanItem("a", (_draft(a1), _draft(a2))), (a1, a2)
    elif kind == "ADD_ITEM":
        operation, proposed = AddPlanItem(_draft(b)), (a, b)
    elif kind == "CHANGE_DEPENDENCY":
        operation, proposed = UpdatePlanItemDependencies("b", ("a",)), (
            a, replace(b, blocked_by=("a",)),
        )
    elif kind == "TOMBSTONE_PENDING":
        operation, proposed = TombstonePlanItem("b"), (a,)
    if scenario == "invalid_patch":
        operation, proposed = TombstonePlanItem("a"), (b,)
    proposed_dag = (VerifiedSubtaskDAGConfig(proposed)
                    if proposed is not None and scenario != "missing_verifier" else None)

    def policy(signal, revision, item):
        patch = PlanPatch(
            reason=f"fixed {kind}", trigger="c1c_controller",
            operations=(operation,), evidence_refs=(signal.signal_id,),
        ) if operation is not None else None
        return ReplanDecision(kind, f"fixed {kind}", revision.revision_id,
                              patch, proposed_dag)

    fault_point = {
        "crash_before_decision": "replan_before_commit",
        "crash_after_decision": "replan_after_commit",
    }.get(scenario)
    if fault_point:
        def fault(actual, **_):
            if actual == fault_point:
                raise InjectedCrash(actual)
        runtime.fault_injector = fault
    interrupted = False
    controller = RecoveryPlanRevisionController(runtime, policy)
    try:
        outcome = controller.process(task_id, signal_id)
    except InjectedCrash:
        interrupted = True
        runtime = Runtime(root, ScriptedModel([]), verified_subtask_dag=initial_dag)
        outcome = RecoveryPlanRevisionController(runtime, policy).process(task_id, signal_id)
    if scenario == "duplicate":
        assert RecoveryPlanRevisionController(runtime, policy).process(task_id, signal_id) == outcome
    expected_rejected = scenario in {"invalid_patch", "missing_verifier"}
    if outcome.result_revision_id is not None:
        active = proposed_dag
    else:
        active = initial_dag
    final_status = runtime.store.get_task(task_id)["status"]
    if not expected_rejected and kind != "FAIL":
        activation_point = {
            "crash_before_activation": "plan_revision_activation_before_commit",
            "crash_after_activation": "plan_revision_activation_after_commit",
        }.get(scenario)
        if activation_point:
            def activation_fault(actual, **_):
                if actual == activation_point:
                    raise InjectedCrash(actual)
            crashing = Runtime(root, ScriptedModel([]), verified_subtask_dag=active,
                               fault_injector=activation_fault)
            try:
                crashing.resume(task_id)
                raise AssertionError("expected fixed activation fault")
            except InjectedCrash:
                interrupted = True
        model = ScriptedModel([
            ModelResponse(text=f"{node.subtask_id} done\nSUBTASK_COMPLETE")
            for node in active.nodes
        ])
        resumed = Runtime(root, model, verified_subtask_dag=active)
        final_status = resumed.resume(task_id).status
        runtime = resumed
    decisions = runtime.store.list_replan_decisions(task_id)
    trace = TraceReporter(runtime.store).summary(task_id)
    reexecution, reverification = _completed_replay_counts(runtime, task_id)
    expected_revisions = 2 if kind in {"SPLIT", "ADD_ITEM", "CHANGE_DEPENDENCY",
                                            "TOMBSTONE_PENDING"} and not expected_rejected else 1
    passed = (
        outcome.outcome == ("rejected" if expected_rejected else "accepted")
        and len(decisions) == 1
        and len(runtime.store.list_plan_revisions(task_id)) == expected_revisions
        and trace["replan_unnecessary_revision_count"] == 0
        and trace["replan_missing_revision_count"] == 0
        and final_status == ("created" if expected_rejected else "failed" if kind == "FAIL" else "completed")
        and (not fault_point and scenario not in {
            "crash_before_activation", "crash_after_activation"
        } or interrupted)
        and reexecution == 0 and reverification == 0
        and runtime.store.scan_invariants(task_id) == []
    )
    return {
        "id": str(spec["id"]), "task_id": task_id, "status": final_status,
        "passed": bool(passed), "error": outcome.rejection_reason,
        "interrupted": interrupted, "fault_triggered": interrupted,
        "recovery_case": interrupted, "evaluation_error": None,
        "decision": kind, "signal_id": signal_id, "reason": outcome.reason,
        "base_revision_id": outcome.base_revision_id,
        "result_revision_id": outcome.result_revision_id,
        "decision_outcome": outcome.outcome, "rejection_reason": outcome.rejection_reason,
        "expected_rejection": expected_rejected,
        "revision_count": len(runtime.store.list_plan_revisions(task_id)),
        "completed_reexecution_count": reexecution,
        "completed_reverification_count": reverification,
        "trace": trace,
    }
