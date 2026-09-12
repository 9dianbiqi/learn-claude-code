from __future__ import annotations

import sqlite3
import time
import json
from pathlib import Path

from agent_runtime import (
    AddPlanItem,
    PlanItemDraft,
    PlanPatch,
    PlanPatchError,
    Runtime,
    SplitPlanItem,
    TombstonePlanItem,
    UpdatePlanItemDependencies,
)
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.migrations import (
    SchemaManager,
    V5_CHECKSUM,
    V6_CHECKSUM,
    V7_CHECKSUM,
    V8_CHECKSUM,
    V9_CHECKSUM,
    V10_CHECKSUM,
    V11_CHECKSUM,
    V12_CHECKSUM,
    V13_CHECKSUM,
    _BASE_SCHEMA,
    _apply_v6_durable_context,
    _apply_v7_background_jobs,
    _apply_v8_tool_registry,
    _apply_v9_subagents_mailbox,
    _apply_v10_verified_subtask,
    _apply_v11_frozen_dag,
    _apply_v12_stale_evidence,
)
from agent_runtime.store import InvariantViolation, StaleState
from agent_runtime.runtime import InjectedCrash
from agent_runtime.trace import TraceReporter

import pytest


def _v12_database_with_plan(tmp_path: Path) -> Path:
    database = tmp_path / "runtime.db"
    now = time.time()
    with sqlite3.connect(database) as connection:
        for statement in _BASE_SCHEMA:
            connection.execute(statement)
        _apply_v6_durable_context(connection)
        _apply_v7_background_jobs(connection)
        _apply_v8_tool_registry(connection)
        _apply_v9_subagents_mailbox(connection)
        _apply_v10_verified_subtask(connection)
        _apply_v11_frozen_dag(connection)
        _apply_v12_stale_evidence(connection)
        for version, name, checksum in (
            (5, "v5_effect_ledger", V5_CHECKSUM),
            (6, "v6_durable_context", V6_CHECKSUM),
            (7, "v7_background_jobs", V7_CHECKSUM),
            (8, "v8_tool_registry_mcp", V8_CHECKSUM),
            (9, "v9_subagents_mailbox", V9_CHECKSUM),
            (10, "v10_verified_subtask", V10_CHECKSUM),
            (11, "v11_frozen_dag", V11_CHECKSUM),
            (12, "v12_stale_evidence", V12_CHECKSUM),
        ):
            connection.execute(
                "INSERT INTO schema_migrations("
                "version, name, checksum, applied_at, duration_ms, backup_filename, backup_sha256"
                ") VALUES (?, ?, ?, ?, 0.0, NULL, NULL)",
                (version, name, checksum, now),
            )
        connection.execute(
            "INSERT INTO tasks(task_id, repo_root, prompt, model, status, created_at, updated_at) "
            "VALUES ('migrated-task', ?, 'prompt', 'model', 'created', ?, ?)",
            (str(tmp_path.resolve()), now, now),
        )
        plan_id = connection.execute(
            "INSERT INTO plans(task_id, status, dag_hash, created_at, updated_at) "
            "VALUES ('migrated-task', 'active', NULL, ?, ?)",
            (now, now),
        ).lastrowid
        connection.execute(
            "INSERT INTO plan_items("
            "plan_id, subtask_id, description, status, blocked_by_json, "
            "max_turns, consumed_turns, version, created_at, updated_at"
            ") VALUES (?, 'first', 'First', 'completed', '[]', 2, 1, 1, ?, ?)",
            (plan_id, now, now),
        )
        connection.execute(
            "INSERT INTO plan_items("
            "plan_id, subtask_id, description, status, blocked_by_json, "
            "max_turns, consumed_turns, version, created_at, updated_at"
            ") VALUES (?, 'second', 'Second', 'pending', '[\"first\"]', 2, 0, 0, ?, ?)",
            (plan_id, now, now),
        )
    return database


def _runtime_with_plan(tmp_path):
    runtime = Runtime(tmp_path, ScriptedModel([]))
    task_id = "task-plan-revisions"
    runtime.store.bootstrap_task(
        task_id,
        str(tmp_path.resolve()),
        "Build the product",
        "scripted",
        [{"role": "user", "content": "Build the product"}],
        {"turn": 0},
    )
    runtime.store.create_plan(
        task_id,
        [
            {"subtask_id": "prepare", "description": "Prepare inputs"},
            {
                "subtask_id": "ship",
                "description": "Ship result",
                "blocked_by": ["prepare"],
            },
        ],
    )
    return runtime, task_id


def test_public_plan_patch_appends_revision_and_preserves_dependency_edges(tmp_path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    initial = runtime.get_current_plan_revision(task_id)

    updated = runtime.apply_plan_patch(
        task_id,
        initial.revision_id,
        PlanPatch(
            reason="Document the prepared result",
            trigger="operator",
            evidence_refs=("issue:#15",),
            operations=(
                AddPlanItem(
                    PlanItemDraft(
                        subtask_id="document",
                        description="Document the result",
                        blocked_by=("prepare",),
                    )
                ),
            ),
        ),
    )

    assert initial.parent_revision_id is None
    assert initial.revision_number == 0
    assert initial.item("ship").blocked_by == ("prepare",)
    assert updated.parent_revision_id == initial.revision_id
    assert updated.revision_number == 1
    assert updated.item("ship").blocked_by == ("prepare",)
    assert updated.item("document").blocked_by == ("prepare",)
    assert runtime.get_current_plan_revision(task_id) == updated


def test_stale_plan_patch_writer_is_rejected_without_partial_revision(tmp_path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    initial = runtime.get_current_plan_revision(task_id)
    accepted = runtime.apply_plan_patch(
        task_id,
        initial.revision_id,
        PlanPatch(
            reason="Add documentation",
            trigger="operator",
            operations=(AddPlanItem(PlanItemDraft("docs", "Write docs")),),
        ),
    )

    with pytest.raises(StaleState, match="Plan revision changed"):
        runtime.apply_plan_patch(
            task_id,
            initial.revision_id,
            PlanPatch(
                reason="Stale addition",
                trigger="operator",
                operations=(AddPlanItem(PlanItemDraft("tests", "Write tests")),),
            ),
        )

    assert runtime.get_current_plan_revision(task_id) == accepted
    assert len(runtime.store.list_plan_revisions(task_id)) == 2


def test_dependency_patch_preserves_old_edge_and_tombstones_instead_of_deleting(tmp_path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    initial = runtime.get_current_plan_revision(task_id)

    updated = runtime.apply_plan_patch(
        task_id,
        initial.revision_id,
        PlanPatch(
            reason="Preparation is no longer required",
            trigger="operator",
            evidence_refs=("decision:prepare-not-needed",),
            operations=(
                UpdatePlanItemDependencies("ship", ()),
                TombstonePlanItem("prepare"),
            ),
        ),
    )

    assert initial.item("ship").blocked_by == ("prepare",)
    assert initial.item("prepare").tombstoned is False
    assert updated.item("ship").blocked_by == ()
    assert updated.item("prepare").tombstoned is True
    assert updated.evidence_refs == ("decision:prepare-not-needed",)
    projected = {
        item["subtask_id"]: item
        for item in runtime.store.list_plan_items(updated.plan_id)
    }
    assert projected["prepare"]["tombstoned"] is True
    assert projected["ship"]["blocked_by"] == []
    with pytest.raises(InvariantViolation, match="tombstoned"):
        runtime.store.start_plan_item(projected["prepare"]["plan_item_id"])


def test_tombstone_with_live_dependent_fails_closed(tmp_path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    initial = runtime.get_current_plan_revision(task_id)

    with pytest.raises(PlanPatchError, match="missing or tombstoned dependencies"):
        runtime.apply_plan_patch(
            task_id,
            initial.revision_id,
            PlanPatch(
                reason="Unsafe removal",
                trigger="operator",
                operations=(TombstonePlanItem("prepare"),),
            ),
        )

    assert runtime.get_current_plan_revision(task_id) == initial


def test_split_failed_item_rewires_dependents_and_keeps_original_tombstone(tmp_path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    plan = runtime.store.get_latest_plan(task_id)
    assert plan is not None
    prepare = next(item for item in plan["items"] if item["subtask_id"] == "prepare")
    runtime.store.start_plan_item(prepare["plan_item_id"])
    runtime.store.fail_plan_item(prepare["plan_item_id"], "needs decomposition")
    initial = runtime.get_current_plan_revision(task_id)

    updated = runtime.apply_plan_patch(
        task_id,
        initial.revision_id,
        PlanPatch(
            reason="Split the failed preparation step",
            trigger="verifier_failure",
            evidence_refs=("verifier:prepare-failed",),
            operations=(
                SplitPlanItem(
                    "prepare",
                    (
                        PlanItemDraft("prepare-inputs", "Prepare inputs"),
                        PlanItemDraft("validate-inputs", "Validate inputs"),
                    ),
                ),
            ),
        ),
    )

    assert updated.item("prepare").tombstoned is True
    assert updated.item("prepare-inputs").tombstoned is False
    assert updated.item("validate-inputs").tombstoned is False
    assert updated.item("ship").blocked_by == ("prepare-inputs", "validate-inputs")
    projected = {
        item["subtask_id"]: item
        for item in runtime.store.list_plan_items(updated.plan_id)
    }
    assert projected["prepare"]["status"] == "failed"
    assert projected["prepare"]["tombstoned"] is True
    assert projected["prepare-inputs"]["status"] == "pending"
    assert projected["validate-inputs"]["status"] == "pending"
    assert projected["ship"]["blocked_by"] == ["prepare-inputs", "validate-inputs"]


def test_completed_item_and_invalid_dag_rewrites_fail_closed(tmp_path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    plan = runtime.store.get_latest_plan(task_id)
    assert plan is not None
    prepare = next(item for item in plan["items"] if item["subtask_id"] == "prepare")
    runtime.store.start_plan_item(prepare["plan_item_id"])
    runtime.store.submit_plan_item_for_verification(prepare["plan_item_id"], "done")
    runtime.store.verify_plan_item(prepare["plan_item_id"], "evidence")
    initial = runtime.get_current_plan_revision(task_id)

    with pytest.raises(PlanPatchError, match="completed or tombstoned"):
        runtime.apply_plan_patch(
            task_id,
            initial.revision_id,
            PlanPatch(
                reason="Rewrite completed work",
                trigger="operator",
                operations=(UpdatePlanItemDependencies("prepare", ("ship",)),),
            ),
        )

    with pytest.raises(PlanPatchError, match="dependency cycle"):
        runtime.apply_plan_patch(
            task_id,
            initial.revision_id,
            PlanPatch(
                reason="Create a cycle",
                trigger="operator",
                operations=(
                    AddPlanItem(PlanItemDraft("loop", "Loop", blocked_by=("ship",))),
                    UpdatePlanItemDependencies("ship", ("prepare", "loop")),
                ),
            ),
        )

    with pytest.raises(PlanPatchError, match="missing or tombstoned"):
        runtime.apply_plan_patch(
            task_id,
            initial.revision_id,
            PlanPatch(
                reason="Reference missing work",
                trigger="operator",
                operations=(UpdatePlanItemDependencies("ship", ("missing",)),),
            ),
        )

    assert runtime.get_current_plan_revision(task_id) == initial


def test_v12_to_v13_migration_backfills_initial_revision_without_mutating_plan_items(
    tmp_path: Path,
) -> None:
    database = _v12_database_with_plan(tmp_path)

    report = SchemaManager(database).migrate()

    assert report.ok is True
    assert report.from_version == 12
    assert report.to_version == 13
    assert report.applied == ("v13_plan_revisions",)
    assert SchemaManager(database).inspect().current_version == 13
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        plan = connection.execute("SELECT * FROM plans").fetchone()
        assert plan["current_revision_id"] is not None
        revision = connection.execute("SELECT * FROM plan_revisions").fetchone()
        assert revision["revision_id"] == plan["current_revision_id"]
        assert revision["parent_revision_id"] is None
        assert revision["revision_number"] == 0
        assert revision["reason"] == "migration_backfill"
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 13"
        ).fetchone()[0] == V13_CHECKSUM
        items = connection.execute(
            "SELECT subtask_id, status, blocked_by_json, tombstoned "
            "FROM plan_items ORDER BY plan_item_id"
        ).fetchall()
        assert [tuple(item) for item in items] == [
            ("first", "completed", "[]", 0),
            ("second", "pending", '["first"]', 0),
        ]

    repeated = SchemaManager(database).migrate()
    assert repeated.ok is True
    assert repeated.applied == ()


@pytest.mark.parametrize(
    ("fault_point", "new_revision_visible"),
    [
        ("plan_patch_before_commit", False),
        ("plan_patch_after_commit", True),
    ],
)
def test_plan_patch_crash_exposes_only_complete_old_or_new_revision(
    tmp_path: Path,
    fault_point: str,
    new_revision_visible: bool,
) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    initial = runtime.get_current_plan_revision(task_id)

    def fault(point: str, **_: object) -> None:
        if point == fault_point:
            raise InjectedCrash(point)

    crashing = Runtime(
        tmp_path,
        ScriptedModel([]),
        store=runtime.store,
        fault_injector=fault,
    )
    patch = PlanPatch(
        reason="Crash-safe addition",
        trigger="operator",
        operations=(AddPlanItem(PlanItemDraft("docs", "Write docs")),),
    )

    with pytest.raises(InjectedCrash, match=fault_point):
        crashing.apply_plan_patch(task_id, initial.revision_id, patch)

    reopened = Runtime(tmp_path, ScriptedModel([]), store=runtime.store)
    current = reopened.get_current_plan_revision(task_id)
    revisions = reopened.store.list_plan_revisions(task_id)
    projected_ids = {
        item["subtask_id"]
        for item in reopened.store.list_plan_items(initial.plan_id)
    }
    if new_revision_visible:
        assert current.parent_revision_id == initial.revision_id
        assert current.item("docs").description == "Write docs"
        assert len(revisions) == 2
        assert "docs" in projected_ids
    else:
        assert current == initial
        assert len(revisions) == 1
        assert "docs" not in projected_ids


def test_v13_migration_rolls_back_before_commit(tmp_path: Path) -> None:
    database = _v12_database_with_plan(tmp_path)

    def fault(point: str, **_: object) -> None:
        if point == "before_migration_commit":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match="before_migration_commit"):
        SchemaManager(database, fault_injector=fault).migrate()

    assert SchemaManager(database).inspect().current_version == 12
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        plan_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(plans)")
        }
        item_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(plan_items)")
        }
        assert "plan_revisions" not in tables
        assert "current_revision_id" not in plan_columns
        assert "tombstoned" not in item_columns


def test_plan_revision_rows_are_append_only(tmp_path: Path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    initial = runtime.get_current_plan_revision(task_id)

    with sqlite3.connect(runtime.store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE plan_revisions SET reason = 'rewritten' WHERE revision_id = ?",
                (initial.revision_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM plan_revisions WHERE revision_id = ?",
                (initial.revision_id,),
            )


def test_invariant_scan_validates_revision_chain_audit_and_current_projection(tmp_path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    initial = runtime.get_current_plan_revision(task_id)
    runtime.apply_plan_patch(
        task_id,
        initial.revision_id,
        PlanPatch(
            reason="Add documentation",
            trigger="operator",
            operations=(AddPlanItem(PlanItemDraft("docs", "Write docs")),),
        ),
    )

    assert runtime.store.scan_invariants(task_id) == []

    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute(
            "UPDATE plan_items SET blocked_by_json = '[\"docs\"]' "
            "WHERE subtask_id = 'ship'"
        )

    assert any(
        "PlanItem projection differs from current revision" in violation
        for violation in runtime.store.scan_invariants(task_id)
    )


def test_trace_exposes_current_plan_revision_and_append_only_history(tmp_path: Path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    initial = runtime.get_current_plan_revision(task_id)
    updated = runtime.apply_plan_patch(
        task_id,
        initial.revision_id,
        PlanPatch(
            reason="Add documentation",
            trigger="operator",
            operations=(AddPlanItem(PlanItemDraft("docs", "Write docs")),),
        ),
    )

    reporter = TraceReporter(runtime.store)
    summary = reporter.summary(task_id)
    assert summary["plan_revision_count"] == 2
    assert summary["current_plan_revision_id"] == updated.revision_id
    assert summary["current_plan_revision_dag_hash"] == updated.dag_hash

    output = tmp_path / "trace.jsonl"
    reporter.export_jsonl(task_id, output)
    revision_records = [
        json.loads(line)
        for line in output.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("record_type") == "plan_revision"
    ]
    assert [record["revision_id"] for record in revision_records] == [
        initial.revision_id,
        updated.revision_id,
    ]
    assert revision_records[0]["items"][1]["blocked_by"] == ["prepare"]


def test_tombstoned_history_does_not_block_final_verified_completion(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path, ScriptedModel([]))
    task_id = "task-tombstone-completion"
    checkpoint_id = runtime.store.bootstrap_task(
        task_id,
        str(tmp_path.resolve()),
        "Complete active work",
        "scripted",
        [{"role": "user", "content": "Complete active work"}],
        {"turn": 0},
    )
    bundle_hash = "a" * 64
    plan_id = runtime.store.create_plan(
        task_id,
        [
            {
                "subtask_id": "active",
                "description": "Active work",
                "verifier_bundle_hash": bundle_hash,
            },
            {"subtask_id": "obsolete", "description": "Obsolete work"},
        ],
    )
    initial = runtime.get_current_plan_revision(task_id)
    runtime.apply_plan_patch(
        task_id,
        initial.revision_id,
        PlanPatch(
            reason="Remove obsolete work",
            trigger="operator",
            operations=(TombstonePlanItem("obsolete"),),
        ),
    )
    active = next(
        item for item in runtime.store.list_plan_items(plan_id)
        if item["subtask_id"] == "active"
    )
    runtime.store.start_plan_item(active["plan_item_id"])
    runtime.store.submit_plan_item_for_verification(active["plan_item_id"], "done")

    runtime.store.commit_verified_subtask(
        task_id=task_id,
        plan_item_id=active["plan_item_id"],
        subtask_id="active",
        completion_summary="done",
        verifier_summary="passed",
        evidence_manifest=[{"path": "active.txt", "sha256": "b" * 64}],
        verifier_id="active-verifier",
        verifier_version="1",
        verification_rule="active exists",
        verifier_bundle_hash=bundle_hash,
        verifier_implementation_hash="c" * 64,
        execution_checkpoint_id=checkpoint_id,
        complete_task=True,
    )

    assert runtime.store.get_task(task_id)["status"] == "completed"
    assert runtime.store.get_latest_plan(task_id)["status"] == "completed"


def test_plan_revision_decoder_rejects_coerced_snapshot_types(tmp_path: Path) -> None:
    runtime, task_id = _runtime_with_plan(tmp_path)
    revision = runtime.get_current_plan_revision(task_id)
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute("DROP TRIGGER immutable_plan_revisions")
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM plan_revisions WHERE revision_id = ?",
                (revision.revision_id,),
            ).fetchone()[0]
        )
        snapshot[0]["tombstoned"] = "false"
        connection.execute(
            "UPDATE plan_revisions SET snapshot_json = ? WHERE revision_id = ?",
            (json.dumps(snapshot), revision.revision_id),
        )

    with pytest.raises(PlanPatchError, match="tombstoned"):
        runtime.get_current_plan_revision(task_id)
