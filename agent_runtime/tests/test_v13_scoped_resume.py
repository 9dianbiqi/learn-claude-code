from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import (
    ModelResponse,
    VerifierContext,
    VerifierResult,
    VerifiedSubtaskConfig,
    VerifiedSubtaskDAGConfig,
)
from agent_runtime.runtime import InjectedCrash
from agent_runtime.trace import TraceReporter


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _node(name: str, verifier, *, blocked_by: tuple[str, ...] = ()) -> VerifiedSubtaskConfig:
    return VerifiedSubtaskConfig(
        subtask_id=name,
        description=f"Complete {name}",
        completion_criteria=f"{name} is complete",
        evidence_paths=(f"{name}.txt",),
        verifier_id=f"verifier-{name}",
        verifier_version="1",
        verification_rule=f"{name} exists",
        verifier=verifier,
        verifier_implementation_hash="b" * 64,
        blocked_by=blocked_by,
        max_turns=3,
    )


def test_scoped_resume_uses_public_resume_and_excludes_old_history(tmp_path: Path) -> None:
    for name in ("dependency", "current"):
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        path = Path(context.repo_root) / f"{context.subtask_id}.txt"
        return VerifierResult(
            "pass",
            f"{context.subtask_id} passed",
            [{"path": f"{context.subtask_id}.txt", "sha256": _sha256(path)}],
        )

    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("dependency", verify), _node("current", verify, blocked_by=("dependency",)))
    )
    before_model_calls = 0

    def crash(point: str, **_: object) -> None:
        nonlocal before_model_calls
        if point == "before_model_call":
            before_model_calls += 1
            if before_model_calls == 2:
                raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="dependency done\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash):
        first.run("global constraint: preserve the API")

    task_id = first.store.list_tasks()[0]["task_id"]
    checkpoint = first.store.get_checkpoint(first.store.get_task(task_id)["checkpoint_id"])
    first.store.save_checkpoint(
        task_id,
        checkpoint["phase"],
        checkpoint["messages"] + [{"role": "user", "content": "unrelated-history"}],
        checkpoint["cursor"],
    )
    resumed_model = ScriptedModel([ModelResponse(text="current done\nSUBTASK_COMPLETE")])
    resumed = Runtime(
        tmp_path,
        resumed_model,
        verified_subtask_dag=dag,
        scoped_context_budget=10_000,
    )

    result = resumed.resume(task_id)

    assert result.status == "completed"
    scoped_messages = resumed_model.calls[0]
    rendered = "\n".join(str(message.get("content", "")) for message in scoped_messages)
    assert "Verified-Scoped recovery context" in rendered
    assert "current" in rendered
    assert "dependency done" in rendered
    assert "unrelated-history" not in rendered
    call = resumed.store.list_model_calls(task_id)[-1]
    assert call["projection"]["basis"] == "verified_scoped"
    assert call["projection"]["resume_unit_id"] == "current"
    assert call["projection"]["dependency_count"] == 1
    assert call["projection"]["relevant_path_count"] == 1
    # The full history remains the authoritative execution checkpoint and
    # model-call request even though the model saw the scoped projection.
    assert "unrelated-history" in str(call["request"]["messages"])
    trace = TraceReporter(resumed.store).summary(task_id)
    assert trace["verified_scoped_resume_count"] == 1
    assert trace["verified_scoped_resume_unit_ids"] == ["current"]


def test_scoped_mode_is_inactive_during_uninterrupted_run(tmp_path: Path) -> None:
    artifact = tmp_path / "only.txt"
    artifact.write_text("only", encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        return VerifierResult(
            "pass",
            "only passed",
            [{"path": "only.txt", "sha256": _sha256(artifact)}],
        )

    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", verify),))
    model = ScriptedModel([ModelResponse(text="only done\nSUBTASK_COMPLETE")])
    runtime = Runtime(
        tmp_path,
        model,
        verified_subtask_dag=dag,
        scoped_context_budget=10_000,
    )

    result = runtime.run("Complete normally")

    assert result.status == "completed"
    assert model.call_count == 1
    projection = runtime.store.list_model_calls(result.task_id)[0]["projection"]
    assert projection["basis"] != "verified_scoped"
    assert projection.get("scoped_resume") is not True


def test_valid_completed_resume_returns_persisted_result_without_scoped_context(tmp_path: Path) -> None:
    artifact = tmp_path / "only.txt"
    artifact.write_text("only", encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        return VerifierResult(
            "pass",
            "only passed",
            [{"path": "only.txt", "sha256": _sha256(artifact)}],
        )

    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", verify),))
    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="only done\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
    )
    completed = first.run("Complete only")
    calls_before = len(first.store.list_model_calls(completed.task_id))

    resumed_model = ScriptedModel([])
    resumed = Runtime(
        tmp_path,
        resumed_model,
        verified_subtask_dag=dag,
        scoped_context_budget=1,
    )
    result = resumed.resume(completed.task_id)

    assert result.status == "completed"
    assert result.final_text == "only done\n"
    assert resumed_model.call_count == 0
    assert len(resumed.store.list_model_calls(completed.task_id)) == calls_before


def test_scoped_context_overflow_fails_closed_without_model_call(tmp_path: Path) -> None:
    artifact = tmp_path / "only.txt"
    artifact.write_text("only", encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        return VerifierResult(
            "pass",
            "only passed",
            [{"path": "only.txt", "sha256": _sha256(artifact)}],
        )

    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", verify),))

    def crash(point: str, **_: object) -> None:
        if point == "before_model_call":
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="never called")]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash):
        first.run("A sufficiently explicit global instruction")
    task_id = first.store.list_tasks()[0]["task_id"]

    resumed_model = ScriptedModel([ModelResponse(text="must not be called")])
    resumed = Runtime(
        tmp_path,
        resumed_model,
        verified_subtask_dag=dag,
        scoped_context_budget=1,
    )
    result = resumed.resume(task_id)

    assert result.status == "failed"
    assert result.error is not None
    assert "scoped context budget overflow" in result.error
    assert resumed_model.call_count == 0
    assert resumed.store.list_model_calls(task_id)[-1]["status"] == "abandoned"
    overflow_events = [
        event for event in resumed.store.list_events(task_id)
        if event["type"] == "verified_scoped_context_overflow"
    ]
    assert len(overflow_events) == 1
    assert overflow_events[0]["payload"]["outcome"] == "overflow"
    assert resumed.store.get_task(task_id)["status"] == "failed"
    trace = TraceReporter(resumed.store).summary(task_id)
    assert trace["scoped_context_overflow_count"] == 1
    assert trace["scoped_context_overflow_outcomes"] == ["overflow"]
    assert trace["scoped_token_estimates"] == [
        overflow_events[0]["payload"]["token_estimate"]
    ]


def test_scoped_context_budget_accepts_exact_rendered_estimate(tmp_path: Path) -> None:
    artifact = tmp_path / "only.txt"
    artifact.write_text("only", encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        return VerifierResult(
            "pass",
            "only passed",
            [{"path": "only.txt", "sha256": _sha256(artifact)}],
        )

    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", verify),))

    def crash_initial(point: str, **_: object) -> None:
        if point == "before_model_call":
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="never called")]),
        verified_subtask_dag=dag,
        fault_injector=crash_initial,
    )
    with pytest.raises(InjectedCrash):
        first.run("Complete only")
    task_id = first.store.list_tasks()[0]["task_id"]

    def crash_again(point: str, **_: object) -> None:
        if point == "before_model_call":
            raise InjectedCrash(point)

    probe = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="never called")]),
        verified_subtask_dag=dag,
        scoped_context_budget=10_000,
        fault_injector=crash_again,
    )
    with pytest.raises(InjectedCrash):
        probe.resume(task_id)
    probe_call = probe.store.list_model_calls(task_id)[-1]
    estimate = probe_call["projection"]["token_estimate"]
    assert estimate > 0

    exact_model = ScriptedModel([ModelResponse(text="only done\nSUBTASK_COMPLETE")])
    exact = Runtime(
        tmp_path,
        exact_model,
        verified_subtask_dag=dag,
        scoped_context_budget=estimate,
    )
    result = exact.resume(task_id)

    assert result.status == "completed"
    assert exact_model.call_count == 1
    exact_projection = exact.store.list_model_calls(task_id)[-1]["projection"]
    assert exact_projection["token_estimate"] == estimate
    assert exact_projection["overflow"] is False


def test_scoped_tail_grows_without_reintroducing_pre_resume_history(tmp_path: Path) -> None:
    artifact = tmp_path / "only.txt"
    artifact.write_text("only", encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        return VerifierResult(
            "pass",
            "only passed",
            [{"path": "only.txt", "sha256": _sha256(artifact)}],
        )

    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", verify),))

    def crash(point: str, **_: object) -> None:
        if point == "before_model_call":
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="never called")]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash):
        first.run("Complete only")
    task_id = first.store.list_tasks()[0]["task_id"]
    checkpoint = first.store.get_checkpoint(first.store.get_task(task_id)["checkpoint_id"])
    first.store.save_checkpoint(
        task_id,
        checkpoint["phase"],
        checkpoint["messages"] + [{"role": "user", "content": "old irrelevant history"}],
        checkpoint["cursor"],
    )

    resumed_model = ScriptedModel([
        ModelResponse(text="first resumed attempt"),
        ModelResponse(text="finished\nSUBTASK_COMPLETE"),
    ])
    resumed = Runtime(
        tmp_path,
        resumed_model,
        verified_subtask_dag=dag,
        scoped_context_budget=10_000,
    )
    result = resumed.resume(task_id)

    assert result.status == "completed"
    assert resumed_model.call_count == 2
    first_view = "\n".join(str(message.get("content", "")) for message in resumed_model.calls[0])
    second_view = "\n".join(str(message.get("content", "")) for message in resumed_model.calls[1])
    assert "old irrelevant history" not in first_view
    assert "old irrelevant history" not in second_view
    assert "first resumed attempt" in second_view
    assert "The subtask is not complete" in second_view
    projections = resumed.store.list_model_calls(task_id)[-2:]
    assert all(call["projection"]["basis"] == "verified_scoped" for call in projections)


def test_scoped_bundle_contains_transitive_dependencies_and_relevant_state(tmp_path: Path) -> None:
    for name in ("first", "second", "current"):
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        path = Path(context.repo_root) / f"{context.subtask_id}.txt"
        return VerifierResult(
            "pass",
            f"{context.subtask_id} summary",
            [{"path": f"{context.subtask_id}.txt", "sha256": _sha256(path)}],
        )

    dag = VerifiedSubtaskDAGConfig(
        nodes=(
            _node("first", verify),
            _node("second", verify, blocked_by=("first",)),
            _node("current", verify, blocked_by=("second",)),
        )
    )
    calls = 0

    def crash(point: str, **_: object) -> None:
        nonlocal calls
        if point == "before_model_call":
            calls += 1
            if calls == 3:
                raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(text="first summary\nSUBTASK_COMPLETE"),
            ModelResponse(text="second summary\nSUBTASK_COMPLETE"),
        ]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash):
        first.run("Global rule: retain compatibility")
    task_id = first.store.list_tasks()[0]["task_id"]
    first.store.append_event(
        task_id,
        "verifier_run_recorded",
        {
            "subtask_id": "current",
            "status": "fail",
            "summary": "latest relevant failure",
        },
    )

    resumed_model = ScriptedModel([ModelResponse(text="current\nSUBTASK_COMPLETE")])
    resumed = Runtime(
        tmp_path,
        resumed_model,
        verified_subtask_dag=dag,
        scoped_context_budget=10_000,
    )
    result = resumed.resume(task_id)

    assert result.status == "completed"
    context_text = resumed_model.calls[0][0]["content"]
    bundle = json.loads(context_text.split("\n", 1)[1])
    assert bundle["global_constraints"] == "Global rule: retain compatibility"
    assert bundle["resume_unit"]["subtask_id"] == "current"
    assert bundle["resume_unit"]["description"] == "Complete current"
    assert bundle["resume_unit"]["completion_criteria"] == "current is complete"
    assert bundle["resume_unit"]["remaining_turn_budget"] == 2
    assert [
        snapshot["subtask_id"]
        for snapshot in bundle["required_dependency_snapshots"]
    ] == ["first", "second"]
    assert [
        snapshot["completion_summary"]
        for snapshot in bundle["required_dependency_snapshots"]
    ] == ["first summary\n", "second summary\n"]
    assert bundle["latest_recovery_reason"]["reason"] == "latest relevant failure"
    assert bundle["relevant_path_state"] == [{
        "missing": False,
        "path": "current.txt",
        "sha256": _sha256(tmp_path / "current.txt"),
        "size": len("current"),
        "status": "present",
    }]


def test_evidence_recovery_precedes_scoped_bundle_and_reopens_stale_node(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency.txt"
    current = tmp_path / "current.txt"
    dependency.write_text("dependency", encoding="utf-8")
    current.write_text("current", encoding="utf-8")
    verifier_calls: list[str] = []

    def verify(context: VerifierContext) -> VerifierResult:
        verifier_calls.append(context.subtask_id)
        path = Path(context.repo_root) / f"{context.subtask_id}.txt"
        if context.subtask_id == "dependency" and len(verifier_calls) == 2:
            return VerifierResult("fail", "dependency evidence is stale", [])
        return VerifierResult(
            "pass",
            f"{context.subtask_id} passed",
            [{"path": f"{context.subtask_id}.txt", "sha256": _sha256(path)}],
        )

    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("dependency", verify), _node("current", verify, blocked_by=("dependency",)))
    )
    before_model_calls = 0

    def crash(point: str, **_: object) -> None:
        nonlocal before_model_calls
        if point == "before_model_call":
            before_model_calls += 1
            if before_model_calls == 2:
                raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="dependency\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash):
        first.run("Repair the dependency before current")
    task_id = first.store.list_tasks()[0]["task_id"]
    dependency.write_text("dependency changed", encoding="utf-8")

    resumed_model = ScriptedModel([
        ModelResponse(text="dependency repaired\nSUBTASK_COMPLETE"),
        ModelResponse(text="current\nSUBTASK_COMPLETE"),
    ])
    resumed = Runtime(
        tmp_path,
        resumed_model,
        verified_subtask_dag=dag,
        scoped_context_budget=10_000,
    )
    result = resumed.resume(task_id)

    assert result.status == "completed"
    assert verifier_calls == ["dependency", "dependency", "dependency", "current"]
    first_scoped_context = resumed_model.calls[0][0]["content"]
    first_bundle = json.loads(first_scoped_context.split("\n", 1)[1])
    assert first_bundle["resume_unit"]["subtask_id"] == "dependency"
    assert first_bundle["latest_recovery_reason"]["status"] == "fail"
    assert "stale" in first_bundle["latest_recovery_reason"]["reason"]
    assert any(
        event["type"] == "verified_subtask_evidence_refresh_fail"
        for event in resumed.store.list_events(task_id)
    )


def test_missing_required_dependency_snapshot_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("dependency", "current"):
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        path = Path(context.repo_root) / f"{context.subtask_id}.txt"
        return VerifierResult(
            "pass",
            f"{context.subtask_id} passed",
            [{"path": f"{context.subtask_id}.txt", "sha256": _sha256(path)}],
        )

    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("dependency", verify), _node("current", verify, blocked_by=("dependency",)))
    )

    before_model_calls = 0

    def crash(point: str, **_: object) -> None:
        nonlocal before_model_calls
        if point == "before_model_call":
            before_model_calls += 1
            if before_model_calls == 2:
                raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="dependency\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash):
        first.run("Complete both")
    task_id = first.store.list_tasks()[0]["task_id"]
    resumed = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="must not be called")]),
        verified_subtask_dag=dag,
        scoped_context_budget=10_000,
    )
    original_lookup = resumed.store.get_current_verified_subtask_checkpoint
    dependency_lookups = 0

    def missing_lookup(task: str, plan_item_id: int):
        nonlocal dependency_lookups
        checkpoint = original_lookup(task, plan_item_id)
        if checkpoint is not None and checkpoint["subtask_id"] == "dependency":
            dependency_lookups += 1
            if dependency_lookups >= 2:
                return None
        return checkpoint

    monkeypatch.setattr(resumed.store, "get_current_verified_subtask_checkpoint", missing_lookup)
    result = resumed.resume(task_id)

    assert result.status == "failed"
    assert result.error is not None
    assert "missing current valid dependency snapshot" in result.error
    assert resumed.model.call_count == 0
    assert any(
        event["type"] == "verified_scoped_context_failed"
        for event in resumed.store.list_events(task_id)
    )
