from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.migrations import (
    _BASE_SCHEMA,
    _apply_v10_verified_subtask,
    _apply_v11_frozen_dag,
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
)
from agent_runtime.store import EventStore
from agent_runtime.trace import TraceReporter
from agent_runtime.models import (
    ModelResponse,
    VerifierContext,
    VerifierResult,
    VerifiedSubtaskConfig,
    VerifiedSubtaskDAGConfig,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _v11_database(tmp_path: Path) -> Path:
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        for statement in _BASE_SCHEMA:
            connection.execute(statement)
        _apply_v6_durable_context(connection)
        _apply_v7_background_jobs(connection)
        _apply_v8_tool_registry(connection)
        _apply_v9_subagents_mailbox(connection)
        _apply_v10_verified_subtask(connection)
        _apply_v11_frozen_dag(connection)
        now = time.time()
        for version, name, checksum in (
            (5, "v5_effect_ledger", V5_CHECKSUM),
            (6, "v6_durable_context", V6_CHECKSUM),
            (7, "v7_background_jobs", V7_CHECKSUM),
            (8, "v8_tool_registry_mcp", V8_CHECKSUM),
            (9, "v9_subagents_mailbox", V9_CHECKSUM),
            (10, "v10_verified_subtask", V10_CHECKSUM),
            (11, "v11_frozen_dag", V11_CHECKSUM),
        ):
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at, duration_ms, "
                "backup_filename, backup_sha256) VALUES (?, ?, ?, ?, 0.0, NULL, NULL)",
                (version, name, checksum, now),
            )
    return database


def test_v11_to_v12_migration_backfills_lifecycle_and_is_idempotent(tmp_path: Path) -> None:
    database = _v11_database(tmp_path)

    report = SchemaManager(database).migrate()

    assert report.ok is True
    assert report.from_version == 11
    assert report.to_version == 12
    assert report.applied == ("v12_stale_evidence",)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "semantic_checkpoint_state_events" in tables
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 12"
        ).fetchone()[0]

    repeated = SchemaManager(database).migrate()
    assert repeated.ok is True
    assert repeated.applied == ()


def test_v11_to_v12_migration_rolls_back_before_commit(tmp_path: Path) -> None:
    database = _v11_database(tmp_path)

    def fault(point: str, **_: object) -> None:
        if point == "before_migration_commit":
            raise RuntimeError(point)

    try:
        SchemaManager(database, fault_injector=fault).migrate()
    except RuntimeError as exc:
        assert str(exc) == "before_migration_commit"
    else:
        raise AssertionError("migration should have failed at the injected boundary")

    assert SchemaManager(database).inspect().current_version == 11
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'semantic_checkpoint_state_events'"
        ).fetchone()[0] == 0


def test_fresh_database_is_v12_with_lifecycle_schema(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "runtime.db")

    assert SchemaManager(store.path).inspect().current_version == 12
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 12"
        ).fetchone()[0] == "v12_stale_evidence"
        assert connection.execute(
            "SELECT COUNT(*) FROM semantic_checkpoint_state_events"
        ).fetchone()[0] == 0


def _node(
    subtask_id: str,
    verifier,
    *,
    blocked_by: tuple[str, ...] = (),
    max_turns: int = 4,
) -> VerifiedSubtaskConfig:
    return VerifiedSubtaskConfig(
        subtask_id=subtask_id,
        description=f"Create {subtask_id}",
        completion_criteria=f"{subtask_id} exists",
        evidence_paths=(f"{subtask_id}.txt",),
        verifier_id=f"{subtask_id}-verifier",
        verifier_version="1",
        verification_rule=f"{subtask_id} exists",
        verifier=verifier,
        verifier_implementation_hash="b" * 64,
        blocked_by=blocked_by,
        max_turns=max_turns,
    )


def test_dag_resume_refreshes_only_changed_completed_node(tmp_path: Path) -> None:
    for name in ("first", "second", "independent"):
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")

    def verify(context: VerifierContext) -> VerifierResult:
        path = Path(context.repo_root) / f"{context.subtask_id}.txt"
        return VerifierResult(
            "pass",
            "ok",
            [{"path": f"{context.subtask_id}.txt", "sha256": _sha256(path)}],
        )

    dag = VerifiedSubtaskDAGConfig(
        nodes=(
            _node("first", verify),
            _node("second", verify, blocked_by=("first",)),
            _node("independent", verify, blocked_by=("first",)),
        )
    )
    first = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text=f"{name}\nSUBTASK_COMPLETE")
                for name in ("first", "second", "independent")
            ]
        ),
        verified_subtask_dag=dag,
    )
    completed = first.run("Complete the DAG")
    assert completed.status == "completed"

    (tmp_path / "first.txt").write_text("changed", encoding="utf-8")
    recovered_model = ScriptedModel([])
    recovered = Runtime(tmp_path, recovered_model, verified_subtask_dag=dag)

    result = recovered.resume(completed.task_id)

    assert result.status == "completed"
    assert recovered_model.call_count == 0
    assert [run["subtask_id"] for run in recovered.store.list_verifier_runs(completed.task_id)] == [
        "first",
        "second",
        "independent",
        "first",
    ]
    assert [
        checkpoint["lifecycle_state"]
        for checkpoint in recovered.store.list_verified_subtask_checkpoints(completed.task_id)
    ] == ["superseded", "valid", "valid", "valid"]


def test_dag_breaking_mutation_invalidates_transitive_dependents_only(tmp_path: Path) -> None:
    for name in ("first", "second", "third", "independent"):
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")
    outcomes = iter(["pass", "pass", "pass", "pass", "fail", "pass", "pass", "pass"])

    def verify(context: VerifierContext) -> VerifierResult:
        status = next(outcomes)
        if status == "fail":
            return VerifierResult("fail", "evidence is no longer valid", [])
        path = Path(context.repo_root) / f"{context.subtask_id}.txt"
        return VerifierResult(
            "pass",
            "ok",
            [{"path": f"{context.subtask_id}.txt", "sha256": _sha256(path)}],
        )

    dag = VerifiedSubtaskDAGConfig(
        nodes=(
            _node("first", verify),
            _node("second", verify, blocked_by=("first",)),
            _node("third", verify, blocked_by=("second",)),
            _node("independent", verify),
        )
    )
    first = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text=f"{name}\nSUBTASK_COMPLETE")
                for name in ("first", "second", "third", "independent")
            ]
        ),
        verified_subtask_dag=dag,
    )
    completed = first.run("Complete the DAG")
    assert completed.status == "completed"

    (tmp_path / "first.txt").write_text("broken", encoding="utf-8")
    recovered = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="first repaired\nSUBTASK_COMPLETE"),
                ModelResponse(text="second repaired\nSUBTASK_COMPLETE"),
                ModelResponse(text="third repaired\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask_dag=dag,
    )

    result = recovered.resume(completed.task_id)

    assert result.status == "completed"
    assert recovered.model.call_count == 3
    items = recovered.store.get_latest_plan(completed.task_id)["items"]
    assert [(item["subtask_id"], item["status"], item["consumed_turns"]) for item in items] == [
        ("first", "completed", 2),
        ("second", "completed", 2),
        ("third", "completed", 2),
        ("independent", "completed", 1),
    ]
    state_by_subtask = {
        checkpoint["subtask_id"]: checkpoint["lifecycle_state"]
        for checkpoint in recovered.store.list_verified_subtask_checkpoints(completed.task_id)
        if checkpoint["lifecycle_state"] == "valid"
    }
    assert state_by_subtask == {
        "independent": "valid",
        "first": "valid",
        "second": "valid",
        "third": "valid",
    }
    assert sum(
        event["type"] == "verified_subtask_evidence_refresh_fail"
        for event in recovered.store.list_events(completed.task_id)
    ) == 1


def test_revalidation_pass_with_concurrent_evidence_mutation_is_uncertain(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("version one", encoding="utf-8")
    calls = 0

    def verify(_: VerifierContext) -> VerifierResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            digest_before_mutation = _sha256(artifact)
            artifact.write_text("changed while verifying", encoding="utf-8")
            return VerifierResult(
                "pass", "pass before mutation", [{"path": "artifact.txt", "sha256": digest_before_mutation}]
            )
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": _sha256(artifact)}])

    config = VerifiedSubtaskConfig(
        subtask_id="artifact",
        description="Create artifact",
        completion_criteria="artifact exists",
        evidence_paths=("artifact.txt",),
        verifier_id="artifact-verifier",
        verifier_version="1",
        verification_rule="artifact exists",
        verifier=verify,
        verifier_implementation_hash="b" * 64,
    )
    runtime = Runtime(
        tmp_path,
        ScriptedModel(
            [
                ModelResponse(text="initial\nSUBTASK_COMPLETE"),
                ModelResponse(text="retry\nSUBTASK_COMPLETE"),
            ]
        ),
        verified_subtask=config,
    )
    completed = runtime.run("Create artifact")
    assert completed.status == "completed"

    artifact.write_text("changed before resume", encoding="utf-8")
    result = runtime.resume(completed.task_id)

    assert result.status == "completed"
    assert [run["status"] for run in runtime.store.list_verifier_runs(completed.task_id)] == [
        "pass",
        "uncertain",
        "pass",
    ]
    assert any(
        event["type"] == "verified_subtask_evidence_refresh_uncertain"
        for event in runtime.store.list_events(completed.task_id)
    )


def test_deleted_evidence_is_detected_and_fails_closed(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("stable", encoding="utf-8")

    def verify(_: VerifierContext) -> VerifierResult:
        if not artifact.exists():
            return VerifierResult("fail", "artifact was deleted", [])
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": _sha256(artifact)}])

    dag = VerifiedSubtaskDAGConfig(
        nodes=(_node("artifact", verify, max_turns=1),)
    )
    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask_dag=dag,
    )
    completed = runtime.run("Create artifact")
    assert completed.status == "completed"
    artifact.unlink()

    recovered = Runtime(tmp_path, ScriptedModel([]), verified_subtask_dag=dag)
    result = recovered.resume(completed.task_id)

    assert result.status == "failed"
    assert [run["status"] for run in recovered.store.list_verifier_runs(completed.task_id)] == [
        "pass",
        "fail",
    ]
    checkpoint = recovered.store.list_verified_subtask_checkpoints(completed.task_id)[0]
    assert checkpoint["lifecycle_state"] == "stale"
    assert recovered.store.get_plan_item(completed.task_id, "artifact")["consumed_turns"] == 1


def test_resume_refreshes_completed_evidence_after_benign_change(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("version one", encoding="utf-8")
    verifier_calls: list[int] = []

    def verify(context: VerifierContext) -> VerifierResult:
        verifier_calls.append(1)
        evidence = Path(context.repo_root) / "artifact.txt"
        return VerifierResult(
            "pass",
            "artifact is valid",
            [{"path": "artifact.txt", "sha256": _sha256(evidence)}],
        )

    config = VerifiedSubtaskConfig(
        subtask_id="artifact",
        description="Create artifact",
        completion_criteria="artifact exists",
        evidence_paths=("artifact.txt",),
        verifier_id="artifact-verifier",
        verifier_version="1",
        verification_rule="artifact exists",
        verifier=verify,
        verifier_implementation_hash="b" * 64,
    )
    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
    )
    completed = first.run("Create artifact")
    assert completed.status == "completed"
    assert verifier_calls == [1]

    artifact.write_text("version two", encoding="utf-8")
    recovered_model = ScriptedModel([])
    recovered = Runtime(tmp_path, recovered_model, verified_subtask=config)

    refreshed = recovered.resume(completed.task_id)

    assert refreshed.status == "completed"
    assert recovered_model.call_count == 0
    assert verifier_calls == [1, 1]
    assert len(recovered.store.list_verifier_runs(completed.task_id)) == 2
    checkpoints = recovered.store.list_verified_subtask_checkpoints(completed.task_id)
    assert len(checkpoints) == 2
    assert [checkpoint["lifecycle_state"] for checkpoint in checkpoints] == [
        "superseded",
        "valid",
    ]
    assert checkpoints[0]["replacement_checkpoint_id"] == checkpoints[1][
        "verified_subtask_checkpoint_id"
    ]
    runs_before = len(recovered.store.list_verifier_runs(completed.task_id))
    checkpoints_before = len(recovered.store.list_verified_subtask_checkpoints(completed.task_id))
    repeated = recovered.resume(completed.task_id)
    assert repeated.status == "completed"
    assert len(recovered.store.list_verifier_runs(completed.task_id)) == runs_before
    assert len(recovered.store.list_verified_subtask_checkpoints(completed.task_id)) == checkpoints_before
    trace = TraceReporter(recovered.store).summary(completed.task_id)
    assert trace["verified_subtask_valid_checkpoint_count"] == 1
    assert trace["verified_subtask_superseded_checkpoint_count"] == 1
    assert trace["verified_subtask_refresh_count"] == 1


def test_resume_with_unchanged_evidence_does_not_verify_or_write(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("stable", encoding="utf-8")
    verifier_calls: list[int] = []

    def verify(_: VerifierContext) -> VerifierResult:
        verifier_calls.append(1)
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": _sha256(artifact)}])

    config = VerifiedSubtaskConfig(
        subtask_id="artifact",
        description="Create artifact",
        completion_criteria="artifact exists",
        evidence_paths=("artifact.txt",),
        verifier_id="artifact-verifier",
        verifier_version="1",
        verification_rule="artifact exists",
        verifier=verify,
        verifier_implementation_hash="b" * 64,
    )
    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
    )
    completed = runtime.run("Create artifact")
    event_count = len(runtime.store.list_events(completed.task_id))
    run_count = len(runtime.store.list_verifier_runs(completed.task_id))
    checkpoint_count = len(runtime.store.list_verified_subtask_checkpoints(completed.task_id))

    result = Runtime(tmp_path, ScriptedModel([]), verified_subtask=config).resume(completed.task_id)

    assert result.status == "completed"
    assert verifier_calls == [1]
    assert len(runtime.store.list_events(completed.task_id)) == event_count
    assert len(runtime.store.list_verifier_runs(completed.task_id)) == run_count
    assert len(runtime.store.list_verified_subtask_checkpoints(completed.task_id)) == checkpoint_count


def test_resume_returns_to_execution_after_breaking_evidence_change(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("version one", encoding="utf-8")
    verifier_results = iter(["pass", "fail", "pass"])

    def verify(_: VerifierContext) -> VerifierResult:
        status = next(verifier_results)
        if status == "fail":
            return VerifierResult("fail", "artifact is broken", [])
        return VerifierResult(
            "pass",
            "ok",
            [{"path": "artifact.txt", "sha256": _sha256(artifact)}],
        )

    config = VerifiedSubtaskConfig(
        subtask_id="artifact",
        description="Create artifact",
        completion_criteria="artifact exists",
        evidence_paths=("artifact.txt",),
        verifier_id="artifact-verifier",
        verifier_version="1",
        verification_rule="artifact exists",
        verifier=verify,
        verifier_implementation_hash="b" * 64,
    )
    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="initial\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
    )
    completed = first.run("Create artifact")
    assert completed.status == "completed"

    artifact.write_text("broken", encoding="utf-8")
    recovered = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="repaired\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
    )

    result = recovered.resume(completed.task_id)

    assert result.status == "completed"
    assert recovered.model.call_count == 1
    assert [run["status"] for run in recovered.store.list_verifier_runs(completed.task_id)] == [
        "pass",
        "fail",
        "pass",
    ]
    checkpoints = recovered.store.list_verified_subtask_checkpoints(completed.task_id)
    assert [checkpoint["lifecycle_state"] for checkpoint in checkpoints] == ["stale", "valid"]
    assert any(
        event["type"] == "verified_subtask_evidence_refresh_fail"
        for event in recovered.store.list_events(completed.task_id)
    )
