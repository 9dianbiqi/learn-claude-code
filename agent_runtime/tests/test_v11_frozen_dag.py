from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.migrations import (
    _BASE_SCHEMA,
    _apply_v10_verified_subtask,
    _apply_v6_durable_context,
    _apply_v7_background_jobs,
    _apply_v8_tool_registry,
    _apply_v9_subagents_mailbox,
    SchemaManager,
    V5_CHECKSUM,
    V6_CHECKSUM,
    V7_CHECKSUM,
    V8_CHECKSUM,
    V9_CHECKSUM,
    V10_CHECKSUM,
    V11_CHECKSUM,
    V12_CHECKSUM,
)
from agent_runtime.models import (
    ModelResponse,
    ToolCall,
    VerifierContext,
    VerifierResult,
    VerifiedSubtaskConfig,
    VerifiedSubtaskDAGConfig,
)
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import InvariantViolation


VALID_SHA256 = "a" * 64
VALID_VERIFIER_IMPLEMENTATION_HASH = "b" * 64


def _v10_database(tmp_path: Path) -> Path:
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        for statement in _BASE_SCHEMA:
            connection.execute(statement)
        _apply_v6_durable_context(connection)
        _apply_v7_background_jobs(connection)
        _apply_v8_tool_registry(connection)
        _apply_v9_subagents_mailbox(connection)
        _apply_v10_verified_subtask(connection)
        now = time.time()
        for version, name, checksum in (
            (5, "v5_effect_ledger", V5_CHECKSUM),
            (6, "v6_durable_context", V6_CHECKSUM),
            (7, "v7_background_jobs", V7_CHECKSUM),
            (8, "v8_tool_registry_mcp", V8_CHECKSUM),
            (9, "v9_subagents_mailbox", V9_CHECKSUM),
            (10, "v10_verified_subtask", V10_CHECKSUM),
        ):
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at, duration_ms, "
                "backup_filename, backup_sha256) VALUES (?, ?, ?, ?, 0.0, NULL, NULL)",
                (version, name, checksum, now),
            )
    return database


def _node(
    subtask_id: str,
    *,
    blocked_by: tuple[str, ...] = (),
    max_turns: int = 3,
) -> VerifiedSubtaskConfig:
    return VerifiedSubtaskConfig(
        subtask_id=subtask_id,
        description=f"Complete {subtask_id}",
        completion_criteria=f"{subtask_id} is complete",
        evidence_paths=(f"{subtask_id}.txt",),
        verifier_id=f"verifier-{subtask_id}",
        verifier_version="1.0",
        verification_rule=f"rule-{subtask_id}",
        verifier=lambda _: VerifierResult(
            "pass",
            f"{subtask_id} passed",
            [{"path": f"{subtask_id}.txt", "sha256": VALID_SHA256}],
        ),
        verifier_implementation_hash=VALID_VERIFIER_IMPLEMENTATION_HASH,
        blocked_by=blocked_by,
        max_turns=max_turns,
    )


def test_public_run_executes_frozen_dag_in_dependency_order_and_stable_ready_order(
    tmp_path: Path,
) -> None:
    verified: list[str] = []

    def verify(context: VerifierContext) -> VerifierResult:
        verified.append(context.subtask_id)
        return VerifierResult(
            "pass",
            f"{context.subtask_id} passed",
            [{"path": f"{context.subtask_id}.txt", "sha256": VALID_SHA256}],
        )

    nodes = (
        _node("prepare", max_turns=2),
        _node("wire", blocked_by=("prepare",), max_turns=2),
        _node("docs", blocked_by=("prepare",), max_turns=2),
        _node("finish", blocked_by=("wire", "docs"), max_turns=2),
    )
    nodes = tuple(
        VerifiedSubtaskConfig(
            subtask_id=node.subtask_id,
            description=node.description,
            completion_criteria=node.completion_criteria,
            evidence_paths=node.evidence_paths,
            verifier_id=node.verifier_id,
            verifier_version=node.verifier_version,
            verification_rule=node.verification_rule,
            verifier=verify,
            verifier_implementation_hash=node.verifier_implementation_hash,
            blocked_by=node.blocked_by,
            max_turns=node.max_turns,
        )
        for node in nodes
    )
    dag = VerifiedSubtaskDAGConfig(nodes=nodes)
    model = ScriptedModel(
        [
            ModelResponse(text=f"{subtask_id}\nSUBTASK_COMPLETE")
            for subtask_id in ("prepare", "wire", "docs", "finish")
        ]
    )

    runtime = Runtime(tmp_path, model, verified_subtask_dag=dag)
    result = runtime.run("Build the project")

    assert result.status == "completed"
    assert verified == ["prepare", "wire", "docs", "finish"]
    assert model.call_count == 4
    task_id = result.task_id
    plan = runtime.store.list_plans(task_id)[0]
    assert plan["status"] == "completed"
    assert plan["dag_hash"] == dag.dag_hash
    items = runtime.store.list_plan_items(plan["plan_id"])
    assert [item["subtask_id"] for item in items] == [
        "prepare",
        "wire",
        "docs",
        "finish",
    ]
    assert all(item["status"] == "completed" for item in items)
    assert all(item["consumed_turns"] == 1 for item in items)
    assert len(runtime.store.list_verifier_runs(task_id)) == 4
    assert len(runtime.store.list_verified_subtask_checkpoints(task_id)) == 4


def test_v10_to_v11_migration_is_audited_and_idempotent(tmp_path: Path) -> None:
    database = _v10_database(tmp_path)

    report = SchemaManager(database).migrate()

    assert report.ok is True
    assert report.from_version == 10
    assert report.to_version == 12
    assert report.applied == ("v11_frozen_dag", "v12_stale_evidence")
    assert SchemaManager(database).inspect().current_version == 12
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(plan_items)")}
        assert {"max_turns", "consumed_turns"} <= columns
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 11"
        ).fetchone()[0] == V11_CHECKSUM
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 12"
        ).fetchone()[0] == V12_CHECKSUM

    repeated = SchemaManager(database).migrate()
    assert repeated.ok is True
    assert repeated.applied == ()


def test_v10_to_v11_migration_rolls_back_before_commit(tmp_path: Path) -> None:
    database = _v10_database(tmp_path)

    def fault(point: str, **_: object) -> None:
        if point == "before_migration_commit":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match="before_migration_commit"):
        SchemaManager(database, fault_injector=fault).migrate()

    assert SchemaManager(database).inspect().current_version == 10
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(plan_items)")}
        assert "max_turns" not in columns
        assert "consumed_turns" not in columns


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ((), "must not be empty"),
        ((_node("a"), _node("a")), "duplicate"),
        ((_node("a", blocked_by=("missing",)),), "missing"),
        ((_node("a", blocked_by=("a",)),), "itself"),
        (
            (_node("a", blocked_by=("b",)), _node("b", blocked_by=("a",))),
            "cycles",
        ),
    ],
)
def test_frozen_dag_rejects_invalid_graphs(
    nodes: tuple[VerifiedSubtaskConfig, ...], message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        VerifiedSubtaskDAGConfig(nodes=nodes)


def test_dag_hash_covers_order_dependencies_bundles_and_budgets() -> None:
    first = VerifiedSubtaskDAGConfig(
        nodes=(_node("a", max_turns=2), _node("b", blocked_by=("a",), max_turns=2))
    )
    reordered = VerifiedSubtaskDAGConfig(
        nodes=(_node("b", blocked_by=("a",), max_turns=2), _node("a", max_turns=2))
    )
    changed_dependency = VerifiedSubtaskDAGConfig(
        nodes=(_node("a", max_turns=2), _node("b", blocked_by=(), max_turns=2))
    )
    changed_budget = VerifiedSubtaskDAGConfig(
        nodes=(_node("a", max_turns=3), _node("b", blocked_by=("a",), max_turns=2))
    )
    changed_bundle = VerifiedSubtaskDAGConfig(
        nodes=(
            replace(_node("a", max_turns=2), description="changed description"),
            _node("b", blocked_by=("a",), max_turns=2),
        )
    )

    assert len(first.dag_hash) == 64
    assert first.dag_hash != reordered.dag_hash
    assert first.dag_hash != changed_dependency.dag_hash
    assert first.dag_hash != changed_budget.dag_hash
    assert first.dag_hash != changed_bundle.dag_hash


def test_f3_recovery_selects_next_ready_node_without_duplicate_bundle(tmp_path: Path) -> None:
    nodes = VerifiedSubtaskDAGConfig(
        nodes=(
            _node("first", max_turns=2),
            _node("second", blocked_by=("first",), max_turns=2),
        )
    )
    crashed = False

    def crash(point: str, **_: object) -> None:
        nonlocal crashed
        if point == "verified_subtask_f3" and not crashed:
            crashed = True
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="first summary\nSUBTASK_COMPLETE"),
                ModelResponse(text="second summary\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask_dag=nodes,
        fault_injector=crash,
    )

    with pytest.raises(InjectedCrash, match="verified_subtask_f3"):
        first.run("Build both artifacts")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert first.store.get_task(task_id)["status"] == "running"
    assert first.store.get_active_plan(task_id)["status"] == "active"
    items = first.store.get_active_plan(task_id)["items"]
    assert [(item["subtask_id"], item["status"]) for item in items] == [
        ("first", "completed"),
        ("second", "pending"),
    ]
    checkpoint = first.store.get_checkpoint(first.store.get_task(task_id)["checkpoint_id"])
    assert checkpoint["phase"] == "model_responded"
    events = first.store.list_events(task_id)
    assert any(event["type"] == "verified_subtask_committed" for event in events)
    assert not any(event["type"] == "task_completed" for event in events)
    assert len(first.store.list_verifier_runs(task_id)) == 1
    assert len(first.store.list_verified_subtask_checkpoints(task_id)) == 1

    recovered_model = ScriptedModel(
        [ModelResponse(text="second summary\nSUBTASK_COMPLETE")]
    )
    recovered = Runtime(tmp_path, recovered_model, verified_subtask_dag=nodes)
    result = recovered.resume(task_id)
    repeated = recovered.resume(task_id)

    assert result.status == "completed"
    assert repeated.status == "completed"
    assert recovered_model.call_count == 1
    assert len(recovered.store.list_verifier_runs(task_id)) == 2
    assert len(recovered.store.list_verified_subtask_checkpoints(task_id)) == 2
    assert [item["status"] for item in recovered.store.get_latest_plan(task_id)["items"]] == [
        "completed",
        "completed",
    ]


def test_recovery_preserves_next_node_response_after_checkpoint_boundary(
    tmp_path: Path,
) -> None:
    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("first", max_turns=1), _node("second", blocked_by=("first",), max_turns=1))
    )
    model_response_boundaries = 0

    def fault(point: str, **_: object) -> None:
        nonlocal model_response_boundaries
        if point == "after_model_response":
            model_response_boundaries += 1
            if model_response_boundaries == 2:
                raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="first\nSUBTASK_COMPLETE"),
                ModelResponse(text="second\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask_dag=dag,
        fault_injector=fault,
    )
    with pytest.raises(InjectedCrash, match="after_model_response"):
        first.run("Complete both")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert first.store.get_plan_item(task_id, "second")["consumed_turns"] == 1
    assert first.store.list_verifier_runs(task_id)[-1]["subtask_id"] == "first"

    recovered_model = ScriptedModel([])
    recovered = Runtime(tmp_path, recovered_model, verified_subtask_dag=dag)
    result = recovered.resume(task_id)

    assert result.status == "completed"
    assert recovered_model.call_count == 0
    assert [run["subtask_id"] for run in recovered.store.list_verifier_runs(task_id)] == [
        "first",
        "second",
    ]


def test_dag_resume_requires_the_same_frozen_configuration(tmp_path: Path) -> None:
    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("first", max_turns=2), _node("second", blocked_by=("first",), max_turns=2))
    )
    runtime = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="first\nSUBTASK_COMPLETE"),
                ModelResponse(text="second\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask_dag=dag,
    )
    completed = runtime.run("Complete both")

    changed_budget = VerifiedSubtaskDAGConfig(
        nodes=(_node("first", max_turns=3), _node("second", blocked_by=("first",), max_turns=2))
    )
    changed_order = VerifiedSubtaskDAGConfig(
        nodes=(_node("second", blocked_by=("first",), max_turns=2), _node("first", max_turns=2))
    )

    for changed in (changed_budget, changed_order):
        with pytest.raises(RuntimeError, match="DAG hash mismatch"):
            Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=changed).resume(completed.task_id)

    with pytest.raises(RuntimeError, match="frozen verified-subtask DAG configuration"):
        Runtime(tmp_path, ScriptedModel([])).resume(completed.task_id)


def test_single_and_dag_configuration_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        Runtime(
            tmp_path,
            ScriptedModel([]),
            verified_subtask=_node("single"),
            verified_subtask_dag=VerifiedSubtaskDAGConfig(nodes=(_node("dag"),)),
        )


def test_resume_prefers_in_progress_node_over_another_ready_pending_node(
    tmp_path: Path,
) -> None:
    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("first", max_turns=2), _node("second", max_turns=2))
    )

    def crash(point: str, **_: object) -> None:
        if point == "before_model_call":
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="first\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash, match="before_model_call"):
        first.run("Complete either ready item")

    task_id = first.store.list_tasks()[0]["task_id"]
    items = first.store.get_latest_plan(task_id)["items"]
    assert [(item["subtask_id"], item["status"]) for item in items] == [
        ("first", "in_progress"),
        ("second", "pending"),
    ]

    recovered_model = ScriptedModel(
        [
            ModelResponse(text="first\nSUBTASK_COMPLETE"),
            ModelResponse(text="second\nSUBTASK_COMPLETE"),
        ]
    )
    recovered = Runtime(tmp_path, recovered_model, verified_subtask_dag=dag)
    result = recovered.resume(task_id)

    assert result.status == "completed"
    assert recovered_model.call_count == 2
    assert [run["subtask_id"] for run in recovered.store.list_verifier_runs(task_id)] == [
        "first",
        "second",
    ]


def test_resume_retryable_blocked_node_returns_fixed_feedback_without_verifying(
    tmp_path: Path,
) -> None:
    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("first", max_turns=1), _node("second", blocked_by=("first",), max_turns=1))
    )
    crashed = False

    def crash(point: str, **_: object) -> None:
        nonlocal crashed
        if point == "verified_subtask_f3" and not crashed:
            crashed = True
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="first\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash, match="verified_subtask_f3"):
        first.run("Complete both")
    task_id = first.store.list_tasks()[0]["task_id"]

    with sqlite3.connect(first.store.path) as connection:
        connection.execute(
            "UPDATE plan_items SET status = 'retryable' WHERE subtask_id = 'second'"
        )
        connection.execute(
            "UPDATE plan_items SET status = 'pending' WHERE subtask_id = 'first'"
        )
        checkpoint = first.store.list_verified_subtask_checkpoints(task_id)[0]
        connection.execute(
            "INSERT INTO semantic_checkpoint_state_events("
            "task_id, semantic_checkpoint_id, plan_item_id, state, reason, "
            "observed_manifest_json, observation_complete, created_at) "
            "SELECT task_id, semantic_checkpoint_id, plan_item_id, 'stale', "
            "'test_dependency_block', observed_manifest_json, observation_complete, "
            "strftime('%s','now') FROM semantic_checkpoint_state_events "
            "WHERE semantic_checkpoint_id = ? ORDER BY state_event_id DESC LIMIT 1",
            (checkpoint["verified_subtask_checkpoint_id"],),
        )

    recovered = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="second\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
    )
    result = recovered.resume(task_id)

    assert result.status == "failed"
    assert [run["subtask_id"] for run in recovered.store.list_verifier_runs(task_id)] == [
        "first",
    ]
    assert recovered.store.get_plan_item(task_id, "second")["status"] == "failed"


@pytest.mark.parametrize("fault_point", ["verified_subtask_f2_pre", "verified_subtask_f2_mid"])
def test_dag_f2_recovery_reverifies_without_partial_authority(
    tmp_path: Path, fault_point: str
) -> None:
    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("first", max_turns=2), _node("second", blocked_by=("first",), max_turns=2))
    )

    def fault(point: str, **_: object) -> None:
        if point == fault_point:
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="first\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
        fault_injector=fault,
    )
    with pytest.raises(InjectedCrash, match=fault_point):
        first.run("Complete both")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert first.store.get_task(task_id)["status"] == "running"
    assert first.store.get_plan_item(task_id, "first")["status"] == "verifying"
    assert first.store.list_verifier_runs(task_id) == []
    assert first.store.list_verified_subtask_checkpoints(task_id) == []

    recovered = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="second\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask_dag=dag,
    )
    result = recovered.resume(task_id)

    assert result.status == "completed"
    runs = recovered.store.list_verifier_runs(task_id)
    assert [(run["subtask_id"], run["status"], run["authoritative"]) for run in runs] == [
        ("first", "pass", True),
        ("second", "pass", True),
    ]


def test_dag_fail_and_uncertain_runs_are_audited_and_retry_current_node(tmp_path: Path) -> None:
    outcomes = iter(
        [
            VerifierResult("fail", "first check failed", []),
            VerifierResult("uncertain", "second check was unavailable", []),
            VerifierResult("pass", "first check passed", [{"path": "first.txt", "sha256": VALID_SHA256}]),
            VerifierResult("pass", "second check passed", [{"path": "second.txt", "sha256": VALID_SHA256}]),
        ]
    )

    def verify(_: VerifierContext) -> VerifierResult:
        return next(outcomes)

    first = replace(_node("first", max_turns=3), verifier=verify)
    second = replace(_node("second", blocked_by=("first",), max_turns=2), verifier=verify)
    dag = VerifiedSubtaskDAGConfig(nodes=(first, second))
    runtime = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="attempt 1\nSUBTASK_COMPLETE"),
                ModelResponse(text="attempt 2\nSUBTASK_COMPLETE"),
                ModelResponse(text="attempt 3\nSUBTASK_COMPLETE"),
                ModelResponse(text="second\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask_dag=dag,
    )

    result = runtime.run("Complete both")

    assert result.status == "completed"
    runs = runtime.store.list_verifier_runs(result.task_id)
    assert [(run["subtask_id"], run["status"], run["authoritative"]) for run in runs] == [
        ("first", "fail", False),
        ("first", "uncertain", False),
        ("first", "pass", True),
        ("second", "pass", True),
    ]
    items = runtime.store.get_latest_plan(result.task_id)["items"]
    assert [(item["subtask_id"], item["consumed_turns"]) for item in items] == [
        ("first", 3),
        ("second", 1),
    ]


def test_dag_budget_exhaustion_fails_item_and_task_without_verifier(tmp_path: Path) -> None:
    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", max_turns=2),))
    model = ScriptedModel(
        [ModelResponse(text="not finished"), ModelResponse(text="still not finished")]
    )
    runtime = Runtime(tmp_path, model, verified_subtask_dag=dag)

    result = runtime.run("Finish the only item")

    assert result.status == "failed"
    assert model.call_count == 2
    assert runtime.store.list_verifier_runs(result.task_id) == []
    plan = runtime.store.get_latest_plan(result.task_id)
    assert plan["status"] == "failed"
    item = plan["items"][0]
    assert item["status"] == "failed"
    assert item["max_turns"] == 2
    assert item["consumed_turns"] == 2
    assert runtime.store.get_task(result.task_id)["status"] == "failed"
    failed_events = [
        event for event in runtime.store.list_events(result.task_id)
        if event["type"] == "plan_item_failed"
    ]
    assert failed_events[-1]["payload"]["budget_exhausted"] is True


def test_dag_turn_budget_is_not_reset_after_restart(tmp_path: Path) -> None:
    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", max_turns=1),))

    def crash(point: str, **_: object) -> None:
        if point == "before_model_call":
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="would have completed\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash, match="before_model_call"):
        first.run("Recover the only item")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert first.store.get_latest_plan(task_id)["items"][0]["consumed_turns"] == 1

    recovered_model = ScriptedModel(
        [ModelResponse(text="completion after restart\nSUBTASK_COMPLETE")]
    )
    recovered = Runtime(tmp_path, recovered_model, verified_subtask_dag=dag)
    result = recovered.resume(task_id)

    assert result.status == "failed"
    assert recovered_model.call_count == 0
    assert recovered.store.list_verifier_runs(task_id) == []
    assert recovered.store.get_plan_item(task_id, "only")["status"] == "failed"
    assert recovered.store.get_task(task_id)["status"] == "failed"


def test_dag_failed_verification_at_budget_boundary_is_recorded_then_fails_task(
    tmp_path: Path,
) -> None:
    node = replace(
        _node("only", max_turns=1),
        verifier=lambda _: VerifierResult("fail", "not ready", []),
    )
    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="attempt\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=VerifiedSubtaskDAGConfig(nodes=(node,)),
    )

    result = runtime.run("Try once")

    assert result.status == "failed"
    assert [run["status"] for run in runtime.store.list_verifier_runs(result.task_id)] == ["fail"]
    assert runtime.store.list_verifier_runs(result.task_id)[0]["authoritative"] is False
    assert runtime.store.get_plan_item(result.task_id, "only")["status"] == "failed"


def test_dag_completion_marker_must_be_an_exclusive_line(tmp_path: Path) -> None:
    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", max_turns=2),))
    runtime = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="inline SUBTASK_COMPLETE mention"),
                ModelResponse(text="completed summary\nSUBTASK_COMPLETE\n"),
            ]
        ),
        verified_subtask_dag=dag,
    )

    result = runtime.run("Complete only")

    assert result.status == "completed"
    assert result.final_text == "completed summary\n"
    assert runtime.model.call_count == 2
    assert len(runtime.store.list_verifier_runs(result.task_id)) == 1


def test_dag_marker_with_tool_calls_fails_current_item_without_running_tools(
    tmp_path: Path,
) -> None:
    dag = VerifiedSubtaskDAGConfig(nodes=(_node("only", max_turns=1),))
    runtime = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(
                    text="done\nSUBTASK_COMPLETE",
                    tool_calls=[ToolCall("read", "read_file", {"path": "only.txt"})],
                )
            ]
        ),
        verified_subtask_dag=dag,
    )

    result = runtime.run("Complete only")

    assert result.status == "failed"
    assert runtime.store.list_tool_calls(result.task_id) == []
    assert runtime.store.list_verifier_runs(result.task_id) == []
    assert runtime.store.get_plan_item(result.task_id, "only")["status"] == "failed"


def test_blocked_completion_marker_gets_fixed_feedback_without_verifier_run(
    tmp_path: Path,
) -> None:
    dag = VerifiedSubtaskDAGConfig(
        nodes=(
            _node("dependency", max_turns=1),
            _node("current", blocked_by=("dependency",), max_turns=1),
        )
    )
    model_call_boundaries = 0
    runtime_holder: dict[str, Runtime] = {}

    def fault(point: str, **_: object) -> None:
        nonlocal model_call_boundaries
        if point != "before_model_call":
            return
        model_call_boundaries += 1
        if model_call_boundaries == 2:
            with sqlite3.connect(runtime_holder["runtime"].store.path) as connection:
                connection.execute(
                    "UPDATE plan_items SET status = 'pending' WHERE subtask_id = 'dependency'"
                )

    runtime = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="dependency\nSUBTASK_COMPLETE"),
                ModelResponse(text="current\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask_dag=dag,
        fault_injector=fault,
    )
    runtime_holder["runtime"] = runtime

    result = runtime.run("Complete the dependency and current item")

    assert result.status == "failed"
    runs = runtime.store.list_verifier_runs(result.task_id)
    assert [(run["subtask_id"], run["status"]) for run in runs] == [("dependency", "pass")]
    assert runtime.store.get_plan_item(result.task_id, "current")["status"] == "failed"
    checkpoint = runtime.store.get_checkpoint(runtime.store.get_task(result.task_id)["checkpoint_id"])
    assert any(
        "blocked by incomplete dependencies" in str(entry["content"])
        for entry in checkpoint["messages"]
        if entry.get("role") == "user"
        and isinstance(entry.get("content"), str)
    )


def test_blocked_marker_can_continue_after_dependency_becomes_complete(tmp_path: Path) -> None:
    dag = VerifiedSubtaskDAGConfig(
        nodes=(
            _node("dependency", max_turns=1),
            _node("current", blocked_by=("dependency",), max_turns=2),
        )
    )
    model_call_boundaries = 0
    runtime_holder: dict[str, Runtime] = {}

    def fault(point: str, **_: object) -> None:
        nonlocal model_call_boundaries
        if point != "before_model_call":
            return
        model_call_boundaries += 1
        if model_call_boundaries == 2:
            statement = "UPDATE plan_items SET status = 'pending' WHERE subtask_id = 'dependency'"
        elif model_call_boundaries == 3:
            statement = "UPDATE plan_items SET status = 'completed' WHERE subtask_id = 'dependency'"
        else:
            return
        with sqlite3.connect(runtime_holder["runtime"].store.path) as connection:
            connection.execute(statement)

    runtime = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="dependency\nSUBTASK_COMPLETE"),
                ModelResponse(text="blocked marker\nSUBTASK_COMPLETE"),
                ModelResponse(text="current\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask_dag=dag,
        fault_injector=fault,
    )
    runtime_holder["runtime"] = runtime

    with pytest.raises(InvariantViolation, match="pending has a current valid checkpoint"):
        runtime.run("Complete both")
