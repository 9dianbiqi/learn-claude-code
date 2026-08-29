from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_runtime.migrations import (
    MigrationBlocked,
    MigrationChecksumMismatch,
    MigrationValidationError,
    SchemaManager,
    SchemaUpgradeRequired,
    UnsupportedSchemaVersion,
    V5_CHECKSUM,
    V6_CHECKSUM,
    V7_CHECKSUM,
    V8_CHECKSUM,
    V9_CHECKSUM,
    _BASE_SCHEMA,
    _apply_v5_effect_ledger,
    _apply_v6_durable_context,
    _apply_v7_background_jobs,
)
from agent_runtime.store import EventStore


FIXTURE = Path(__file__).parent / "fixtures" / "schema_v4.sql"


def _args_hash(name: str, args: dict) -> str:
    payload = json.dumps({"name": name, "input": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _v4_database(tmp_path: Path, states: tuple[str, ...] = ("completed", "unknown", "cancelled")) -> Path:
    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    connection.executescript(FIXTURE.read_text(encoding="utf-8"))
    repo = str(tmp_path.resolve())
    for index, state in enumerate(states):
        task_id = f"task-{index}"
        tool_use_id = f"tool-{index}"
        now = float(index + 1)
        task_status = {
            "completed": "completed",
            "failed": "failed",
            "unknown": "needs_review",
            "cancelled": "aborted",
            "running": "running",
            "waiting": "waiting_approval",
        }.get(state, "running")
        tool_status = {
            "completed": "succeeded",
            "failed": "failed",
            "unknown": "needs_review",
            "cancelled": "aborted",
            "running": "running",
            "waiting": "waiting_approval",
        }.get(state, "running")
        checkpoint_phase = {
            "completed": "completed",
            "failed": "failed",
            "unknown": "needs_review",
            "cancelled": "aborted",
            "running": "input_ready",
            "waiting": "waiting_approval",
        }.get(state, "input_ready")
        connection.execute(
            "INSERT INTO tasks(task_id, repo_root, prompt, model, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (task_id, repo, "prompt", "fake", now, now),
        )
        args = {"path": f"file-{index}.txt", "content": str(index)}
        connection.execute(
            "INSERT INTO tool_calls(task_id, tool_use_id, turn, name, args_json, args_hash, status, effect, "
            "output, effect_attempts, effect_confirmed) VALUES (?, ?, 0, 'write_file', ?, ?, ?, 'file_write', ?, 1, ?)",
            (
                task_id,
                tool_use_id,
                json.dumps(args, ensure_ascii=False, sort_keys=True),
                _args_hash("write_file", args),
                tool_status,
                "done" if state == "completed" else None,
                1 if state == "completed" else 0,
            ),
        )
        checkpoint = connection.execute(
            "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) "
            "VALUES (?, ?, '[]', '{}', ?)",
            (task_id, checkpoint_phase, now),
        )
        checkpoint_id = int(checkpoint.lastrowid)
        connection.execute(
            "UPDATE tasks SET status = ?, checkpoint_id = ?, version = 1 WHERE task_id = ?",
            (task_status, checkpoint_id, task_id),
        )
        connection.execute(
            "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_created', ?, ?)",
            (task_id, json.dumps({"prompt": "prompt"}, sort_keys=True), now),
        )
        if state == "completed":
            connection.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_completed', ?, ?)",
                (task_id, json.dumps({"checkpoint_id": checkpoint_id}, sort_keys=True), now),
            )
        elif state == "cancelled":
            connection.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_aborted', ?, ?)",
                (task_id, json.dumps({"checkpoint_id": checkpoint_id}, sort_keys=True), now),
            )
        elif state == "failed":
            connection.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_failed', ?, ?)",
                (task_id, json.dumps({"checkpoint_id": checkpoint_id}, sort_keys=True), now),
            )
        elif state == "waiting":
            connection.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_waiting_approval', ?, ?)",
                (
                    task_id,
                    json.dumps({"checkpoint_id": checkpoint_id, "tool_use_id": tool_use_id}, sort_keys=True),
                    now,
                ),
            )
        elif state == "unknown":
            connection.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_needs_review', ?, ?)",
                (
                    task_id,
                    json.dumps({"checkpoint_id": checkpoint_id, "tool_use_id": tool_use_id}, sort_keys=True),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'tool_needs_review', ?, ?)",
                (task_id, json.dumps({"tool_use_id": tool_use_id}, sort_keys=True), now),
            )
        connection.execute(
            "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'checkpoint_saved', ?, ?)",
            (
                task_id,
                json.dumps({"checkpoint_id": checkpoint_id, "phase": checkpoint_phase}, sort_keys=True),
                now,
            ),
        )
        if state != "waiting":
            reservation_state = "cancelled" if state == "failed" else state
            connection.execute(
                "INSERT INTO effect_reservations(task_id, tool_use_id, owner_id, owner_pid, fencing_token, effect, "
                "state, started_at, finished_at, details_json) VALUES (?, ?, 'legacy', 1, 1, 'file_write', ?, ?, ?, ?)",
                (
                    task_id,
                    tool_use_id,
                    reservation_state,
                    now,
                    None if state == "running" else now + 0.1,
                    json.dumps({"legacy": True}),
                ),
            )
    connection.commit()
    connection.close()
    return database


def _v5_database(tmp_path: Path, states: tuple[str, ...] = ("completed",)) -> Path:
    database = _v4_database(tmp_path, states)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    _apply_v5_effect_ledger(connection)
    connection.execute(
        "INSERT INTO schema_migrations(version, name, checksum, applied_at, duration_ms, "
        "backup_filename, backup_sha256) VALUES (5, 'v5_effect_ledger', ?, ?, 0.0, NULL, NULL)",
        (V5_CHECKSUM, time.time()),
    )
    connection.commit()
    connection.close()
    return database


def _database_snapshot(database: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        schema = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        rows: list[tuple[object, ...]] = [schema]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows.append((table, tuple(connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall())))
        return tuple(rows)


def _v7_database(tmp_path: Path) -> Path:
    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    _execute = lambda statement: connection.execute(statement)
    for statement in _BASE_SCHEMA:
        _execute(statement)
    _apply_v6_durable_context(connection)
    _apply_v7_background_jobs(connection)
    connection.execute(
        "INSERT INTO schema_migrations(version, name, checksum, applied_at, duration_ms, "
        "backup_filename, backup_sha256) VALUES (5, 'v5_effect_ledger', ?, ?, 0.0, NULL, NULL)",
        (V5_CHECKSUM, time.time()),
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, name, checksum, applied_at, duration_ms, "
        "backup_filename, backup_sha256) VALUES (6, 'v6_durable_context', ?, ?, 0.0, NULL, NULL)",
        (V6_CHECKSUM, time.time()),
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, name, checksum, applied_at, duration_ms, "
        "backup_filename, backup_sha256) VALUES (7, 'v7_background_jobs', ?, ?, 0.0, NULL, NULL)",
        (V7_CHECKSUM, time.time()),
    )
    connection.commit()
    connection.close()
    return database


def test_fresh_store_creates_v9_directly(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    versions = store._fetchall("SELECT version FROM schema_migrations ORDER BY version")
    assert [int(row["version"]) for row in versions] == [9]
    assert store._fetchone(
        "SELECT name, checksum FROM schema_migrations WHERE version = 9"
    )["checksum"] == V9_CHECKSUM
    tables = {
        str(row[0])
        for row in store._fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('tool_registrations', 'mcp_connections', "
            "'subagent_runs', 'mailboxes', 'mailbox_messages', 'plan_approvals')"
        )
    }
    assert tables == {
        "tool_registrations",
        "mcp_connections",
        "subagent_runs",
        "mailboxes",
        "mailbox_messages",
        "plan_approvals",
    }


def test_v7_database_migrates_to_v9(tmp_path: Path):
    database = _v7_database(tmp_path)
    assert SchemaManager(database).inspect().current_version == 7
    assert "tool_registrations" not in {
        str(row[0])
        for row in sqlite3.connect(database).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    report = SchemaManager(database).migrate()
    assert report.ok is True
    assert report.from_version == 7
    assert report.to_version == 9
    assert report.applied == ("v8_tool_registry_mcp", "v9_subagents_mailbox")
    assert report.backup is not None

    store = EventStore(database)
    assert store.integrity_check() == []
    assert store._fetchone(
        "SELECT name, checksum FROM schema_migrations WHERE version = 9"
    )["checksum"] == V9_CHECKSUM
    tables = {
        str(row[0])
        for row in store._fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('tool_registrations', 'mcp_connections', "
            "'subagent_runs', 'mailboxes', 'mailbox_messages', 'plan_approvals')"
        )
    }
    assert tables == {
        "tool_registrations",
        "mcp_connections",
        "subagent_runs",
        "mailboxes",
        "mailbox_messages",
        "plan_approvals",
    }


def test_v4_fixture_migrates_without_losing_rows_or_reservations(tmp_path: Path):
    database = _v4_database(tmp_path)
    with pytest.raises(SchemaUpgradeRequired):
        EventStore(database)

    report = SchemaManager(database).migrate()
    assert report.ok is True
    assert report.from_version == 4
    assert report.to_version == 9
    assert report.applied == (
        "v5_effect_ledger",
        "v6_durable_context",
        "v7_background_jobs",
        "v8_tool_registry_mcp",
        "v9_subagents_mailbox",
    )
    assert report.backup is not None
    assert Path(report.backup.path).exists()
    assert report.backup.sha256 == hashlib.sha256(Path(report.backup.path).read_bytes()).hexdigest()
    assert report.backup.integrity_check == "ok"

    store = EventStore(database)
    assert len(store.list_tasks()) == 3
    operations = store.list_operations()
    assert len(operations) == 3
    assert {item["state"] for item in operations} == {"committed", "unknown", "cancelled"}
    assert all(item["attempt_count"] == 1 for item in operations)
    assert all(item["operation_id"] for item in store.list_tool_calls("task-0"))
    assert all(item["operation_id"] for item in store.list_effect_reservations())
    assert store.integrity_check() == []

    metadata = store._fetchall(
        "SELECT version, name, checksum, backup_filename, backup_sha256 "
        "FROM schema_migrations ORDER BY version"
    )
    assert [(int(row["version"]), row["name"], row["checksum"]) for row in metadata] == [
        (4, "legacy-v4", "legacy"),
        (5, "v5_effect_ledger", V5_CHECKSUM),
        (6, "v6_durable_context", V6_CHECKSUM),
        (7, "v7_background_jobs", V7_CHECKSUM),
        (8, "v8_tool_registry_mcp", V8_CHECKSUM),
        (9, "v9_subagents_mailbox", V9_CHECKSUM),
    ]
    assert metadata[-1]["backup_filename"] == report.backup.filename
    assert metadata[-1]["backup_sha256"] == report.backup.sha256


def test_stale_running_reservation_is_migrated_to_unknown_and_blocked(tmp_path: Path):
    database = _v4_database(tmp_path, ("running",))
    report = SchemaManager(database).migrate()
    assert report.ok
    store = EventStore(database)
    operation = store.list_operations()[0]
    reservation = store.list_effect_reservations()[0]
    assert operation["state"] == "unknown"
    assert operation["outbox_state"] == "blocked"
    assert reservation["state"] == "unknown"


@pytest.mark.parametrize("tool_status", ["needs_review", "failed", "planned", "running", "pending"])
def test_invalid_completed_reservation_is_rejected_before_v5_changes(tmp_path: Path, tool_status: str):
    database = _v4_database(tmp_path, ("completed",))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tool_calls SET status = ?", (tool_status,))
        connection.commit()
    before = _database_snapshot(database)

    with pytest.raises(MigrationValidationError):
        SchemaManager(database).migrate()

    assert SchemaManager(database).inspect().current_version == 4
    assert _database_snapshot(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'operations'"
        ).fetchone() is None
        assert "operation_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(tool_calls)").fetchall()
        }


@pytest.mark.parametrize("reservation_state,tool_status", [("running", "needs_review"), ("running", "planned")])
def test_invalid_running_reservation_tool_projection_is_rejected(tmp_path: Path, reservation_state: str, tool_status: str):
    database = _v4_database(tmp_path, (reservation_state,))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tool_calls SET status = ?", (tool_status,))
        connection.commit()

    with pytest.raises(MigrationValidationError):
        SchemaManager(database).migrate()
    assert SchemaManager(database).inspect().current_version == 4


def test_unknown_reservation_on_incompatible_task_is_rejected(tmp_path: Path):
    database = _v4_database(tmp_path, ("unknown",))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tasks SET status = 'waiting_approval'")
        connection.commit()

    with pytest.raises(MigrationValidationError):
        SchemaManager(database).migrate()
    assert SchemaManager(database).inspect().current_version == 4


def test_completed_reservation_with_succeeded_tool_migrates(tmp_path: Path):
    database = _v4_database(tmp_path, ("completed",))
    report = SchemaManager(database).migrate()

    assert report.ok
    assert SchemaManager(database).inspect().current_version == 9
    store = EventStore(database)
    assert store.get_tool_call("task-0", "tool-0")["status"] == "succeeded"
    assert store.get_effect_reservation("task-0", "tool-0")["state"] == "completed"
    assert store.list_operations()[0]["state"] == "committed"
    assert store.scan_invariants() == []


def test_invalid_legacy_rows_stay_v4_after_reopen_and_retry(tmp_path: Path):
    database = _v4_database(tmp_path, ("completed",))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE tool_calls SET status = 'needs_review'")
        connection.commit()
    before = _database_snapshot(database)

    for _ in range(2):
        with pytest.raises(MigrationValidationError):
            SchemaManager(database).migrate()
        assert SchemaManager(database).inspect().current_version == 4
        assert _database_snapshot(database) == before
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'operations'"
            ).fetchone() is None


@pytest.mark.parametrize(
    "case",
    [
        "completed_task_missing_checkpoint",
        "failed_task_missing_checkpoint",
        "aborted_task_missing_checkpoint",
        "needs_review_task_with_succeeded_tool",
        "waiting_approval_task_with_succeeded_tool",
        "completed_task_with_planned_tool",
    ],
)
def test_all_legacy_task_projection_cases_fail_closed(tmp_path: Path, case: str):
    states = {
        "completed_task_missing_checkpoint": ("completed",),
        "failed_task_missing_checkpoint": ("failed",),
        "aborted_task_missing_checkpoint": ("cancelled",),
        "needs_review_task_with_succeeded_tool": ("unknown",),
        "waiting_approval_task_with_succeeded_tool": ("waiting",),
        "completed_task_with_planned_tool": ("completed",),
    }
    database = _v4_database(tmp_path, states[case])
    with sqlite3.connect(database) as connection:
        if case.endswith("missing_checkpoint"):
            connection.execute("UPDATE tasks SET checkpoint_id = NULL")
        elif case == "needs_review_task_with_succeeded_tool":
            connection.execute("UPDATE tool_calls SET status = 'succeeded', output = 'done'")
        elif case == "waiting_approval_task_with_succeeded_tool":
            connection.execute("UPDATE tool_calls SET status = 'succeeded', output = 'done'")
        elif case == "completed_task_with_planned_tool":
            connection.execute("UPDATE tool_calls SET status = 'planned', output = NULL")
        connection.commit()
    before = _database_snapshot(database)

    for _ in range(2):
        with pytest.raises(MigrationValidationError):
            SchemaManager(database).migrate()
        assert SchemaManager(database).inspect().current_version == 4
        assert _database_snapshot(database) == before
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'operations'"
            ).fetchone() is None


def test_legacy_projection_parity_has_no_post_migration_invariant_gap(tmp_path: Path):
    database = _v4_database(tmp_path, ("completed", "failed", "cancelled", "unknown", "waiting", "running"))
    report = SchemaManager(database).migrate()
    assert report.ok
    store = EventStore(database)
    assert store.scan_invariants() == []
    assert store.integrity_check() == []


def test_active_lease_blocks_migration(tmp_path: Path):
    database = _v4_database(tmp_path, ("running",))
    repo = str(tmp_path.resolve())
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO leases(repo_root, task_id, owner_id, heartbeat_at, expires_at, fencing_token) "
        "VALUES (?, 'task-0', 'owner', 1, 9999999999, 1)",
        (repo,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(MigrationBlocked):
        SchemaManager(database).migrate()
    assert SchemaManager(database).inspect().current_version == 4


def test_dry_run_has_zero_writes_and_no_backup(tmp_path: Path):
    database = _v4_database(tmp_path, ("completed",))
    before_mtime = database.stat().st_mtime_ns
    report = SchemaManager(database).migrate(dry_run=True)
    assert report.dry_run is True
    assert report.from_version == 4
    assert report.to_version == 9
    assert report.backup is None
    assert database.stat().st_mtime_ns == before_mtime
    assert not list(tmp_path.glob("*.backup.*.db"))
    assert SchemaManager(database).inspect().current_version == 4


@pytest.mark.parametrize("point", ["after_migration_ddl", "after_migration_backfill", "before_migration_commit"])
def test_migration_fault_rolls_back_to_complete_v4(tmp_path: Path, point: str):
    database = _v4_database(tmp_path, ("completed", "unknown"))

    def inject(actual: str, **_: object) -> None:
        if actual == point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=point):
        SchemaManager(database, fault_injector=inject).migrate()
    assert SchemaManager(database).inspect().current_version == 4
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'operations'"
    ).fetchone() is None
    assert "operation_id" not in {
        row[1] for row in connection.execute("PRAGMA table_info(tool_calls)").fetchall()
    }
    connection.close()


def test_checksum_mismatch_fails_closed(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 9"
        )
        connection.commit()
    with pytest.raises(MigrationChecksumMismatch):
        SchemaManager(store.path).inspect()


def test_business_tables_without_migration_history_fail_closed(tmp_path: Path):
    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(MigrationValidationError):
        SchemaManager(database).inspect()


def test_repeated_migrate_is_idempotent_and_does_not_add_a_second_audit_row(tmp_path: Path):
    database = _v4_database(tmp_path, ("completed",))
    first = SchemaManager(database).migrate()
    second = SchemaManager(database).migrate()
    assert first.applied == (
        "v5_effect_ledger",
        "v6_durable_context",
        "v7_background_jobs",
        "v8_tool_registry_mcp",
        "v9_subagents_mailbox",
    )
    assert second.from_version == 9
    assert second.to_version == 9
    assert second.applied == ()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 6


def test_v5_database_migrates_to_v9(tmp_path: Path):
    database = _v5_database(tmp_path)
    assert SchemaManager(database).inspect().current_version == 5

    report = SchemaManager(database).migrate()
    assert report.ok is True
    assert report.from_version == 5
    assert report.to_version == 9
    assert report.applied == (
        "v6_durable_context",
        "v7_background_jobs",
        "v8_tool_registry_mcp",
        "v9_subagents_mailbox",
    )

    store = EventStore(database)
    assert store.integrity_check() == []
    assert store._fetchone(
        "SELECT name, checksum FROM schema_migrations WHERE version = 9"
    )["checksum"] == V9_CHECKSUM


def test_future_schema_version_fails_closed(tmp_path: Path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "checksum TEXT NOT NULL, applied_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (10, 'future', 'future', 0)"
        )
    with pytest.raises(UnsupportedSchemaVersion):
        SchemaManager(database).inspect()


def test_concurrent_migrate_has_one_writer_and_one_final_v9_reader(tmp_path: Path):
    database = _v4_database(tmp_path, ("completed", "unknown"))
    barrier = threading.Barrier(2)
    reports: list[tuple[int, int, tuple[str, ...]]] = []
    errors: list[BaseException] = []

    def migrate() -> None:
        try:
            barrier.wait()
            report = SchemaManager(database).migrate()
            reports.append((report.from_version, report.to_version, report.applied))
        except BaseException as exc:  # pragma: no cover - assertion reports the unexpected error
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sorted(reports) == [
        (
            4,
            9,
            (
                "v5_effect_ledger",
                "v6_durable_context",
                "v7_background_jobs",
                "v8_tool_registry_mcp",
                "v9_subagents_mailbox",
            ),
        ),
        (9, 9, ()),
    ]
    assert SchemaManager(database).inspect().current_version == 9


@pytest.mark.parametrize("point", ["after_migration_backup", "after_migration_commit"])
def test_migration_boundary_faults_leave_a_valid_pre_or_post_state(tmp_path: Path, point: str):
    database = _v4_database(tmp_path, ("completed",))

    def inject(actual: str, **_: object) -> None:
        if actual == point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=point):
        SchemaManager(database, fault_injector=inject).migrate()
    version = SchemaManager(database).inspect().current_version
    assert version == (4 if point == "after_migration_backup" else 9)


def test_migration_subprocess_exit_after_ddl_reopens_as_complete_v4(tmp_path: Path):
    database = _v4_database(tmp_path, ("completed", "unknown"))
    source_root = Path(__file__).resolve().parents[2]
    script = """
import os
import sys

from agent_runtime.migrations import SchemaManager


def crash(point, **_):
    if point == "after_migration_ddl":
        os._exit(23)


SchemaManager(sys.argv[1], fault_injector=crash).migrate()
"""
    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(source_root) + (
        os.pathsep + current_pythonpath if current_pythonpath else ""
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        cwd=source_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 23, completed.stderr

    # SQLite must recover the interrupted transaction to one complete schema,
    # not expose half-created v5 tables or columns.
    status = SchemaManager(database).inspect()
    assert status.current_version in {4, 5}
    assert not status.integrity_check
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if status.current_version == 4:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'operations'"
            ).fetchone() is None
            assert "operation_id" not in {
                row[1] for row in connection.execute("PRAGMA table_info(tool_calls)").fetchall()
            }
        else:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'operations'"
            ).fetchone() is not None
            assert connection.execute(
                "SELECT COUNT(*) FROM operations"
            ).fetchone()[0] == 2
