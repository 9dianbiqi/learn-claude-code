from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_runtime.migrations import (
    SchemaManager,
    V6_CHECKSUM,
    V7_CHECKSUM,
    V8_CHECKSUM,
    V9_CHECKSUM,
    V10_CHECKSUM,
    V11_CHECKSUM,
    _apply_v6_durable_context,
    _execute_all,
    _BASE_SCHEMA,
)
from agent_runtime.store import EventStore, LeaseLost
from agent_runtime.trace import TraceReporter


def _leased_store(tmp_path: Path, task_id: str = "task-jobs") -> tuple[EventStore, str, str, int]:
    store = EventStore(tmp_path / "runtime.db")
    repo = str(tmp_path.resolve())
    store.bootstrap_task(task_id, repo, "prompt", "fake", [], {"turn": 0})
    fencing = store.acquire_lease(repo, task_id, "owner-a", ttl=60)
    store.bind_lease(repo, "owner-a", fencing)
    return store, repo, task_id, int(fencing)


def test_running_job_is_recovered_after_restart(tmp_path: Path):
    store, repo, task_id, _ = _leased_store(tmp_path)
    store.enqueue_job("job-1", task_id, repo, "shell", {"cmd": "deploy"}, max_attempts=2)
    claimed = store.claim_job("job-1", ttl=60)
    assert store.heartbeat_job(claimed["run_id"], ttl=60) is True
    assert store.get_job("job-1")["status"] == "running"

    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE job_runs SET lease_expires_at = 1.0 WHERE run_id = ?", (claimed["run_id"],))
        conn.execute("UPDATE leases SET expires_at = 1.0")

    restarted = EventStore(tmp_path / "runtime.db")
    recovered = restarted.recover_jobs(now=10.0)
    assert recovered[0]["job_id"] == "job-1"
    assert recovered[0]["new_status"] == "retryable"
    assert restarted.get_job("job-1")["status"] == "retryable"
    run = [r for r in restarted.list_job_runs("job-1") if r["run_id"] == claimed["run_id"]][0]
    assert run["status"] == "failed"
    assert run["error"] == "owner_stale_lease_expired"
    assert restarted.scan_invariants() == []


def test_stale_owner_is_fenced_and_cannot_complete(tmp_path: Path):
    store, repo, task_id, first_fencing = _leased_store(tmp_path)
    store.enqueue_job("job-fence", task_id, repo, "shell", {"cmd": "migrate"}, max_attempts=2)
    claimed = store.claim_job("job-fence", ttl=60)

    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE job_runs SET lease_expires_at = 1.0 WHERE run_id = ?", (claimed["run_id"],))
        conn.execute("UPDATE leases SET expires_at = 1.0")

    new_store = EventStore(tmp_path / "runtime.db")
    new_fencing = new_store.acquire_lease(repo, task_id, "owner-b", ttl=60)
    new_store.bind_lease(repo, "owner-b", new_fencing)
    new_store.recover_jobs(now=10.0)
    assert new_store.get_job("job-fence")["status"] == "retryable"

    second = new_store.claim_job("job-fence", ttl=60)
    assert second["fencing_token"] == 2

    # The first owner can no longer complete the old run after fencing.
    store.bind_lease(repo, "owner-a", first_fencing)
    with pytest.raises(LeaseLost):
        store.complete_job(claimed["run_id"])


def test_failed_job_does_not_spawn_a_duplicate_active_run(tmp_path: Path):
    store, repo, task_id, _ = _leased_store(tmp_path)
    store.enqueue_job("job-retry", task_id, repo, "shell", {"cmd": "build"}, max_attempts=3)

    first = store.claim_job("job-retry", ttl=60)
    store.fail_job(first["run_id"], error="build error")
    assert store.get_job("job-retry")["status"] == "retryable"

    second = store.claim_job("job-retry", ttl=60)
    assert second["fencing_token"] == 2
    runs = store.list_job_runs("job-retry")
    active = [run for run in runs if run["status"] == "running"]
    assert len(active) == 1
    assert active[0]["run_id"] == second["run_id"]
    assert store.scan_invariants() == []


def test_cron_schedule_creates_a_durable_job_at_the_boundary(tmp_path: Path):
    store, repo, task_id, _ = _leased_store(tmp_path)
    store.create_cron_schedule(
        "cron-1", task_id, "*/1 * * * *", "snapshot",
        {"dir": repo}, next_trigger_at=2.0,
    )
    job_ids = store.trigger_due_cron(now=10.0)
    assert len(job_ids) == 1

    job = store.get_job(job_ids[0])
    assert job["job_id"] == job_ids[0]
    assert job["kind"] == "snapshot"
    assert job["status"] == "pending"
    assert job["payload"]["dir"] == repo

    schedule = store.list_cron_schedules()[0]
    assert schedule["last_triggered_at"] == 10.0
    assert schedule["next_trigger_at"] == 70.0
    assert store.scan_invariants() == []


def test_job_events_and_records_are_redacted_in_trace(tmp_path: Path):
    store, repo, task_id, _ = _leased_store(tmp_path)
    store.enqueue_job("job-secret", task_id, repo, "shell", {"api_key": "super-secret-value"})
    output = tmp_path / "trace.jsonl"
    TraceReporter(store).export_jsonl(task_id, output)
    text = output.read_text(encoding="utf-8")
    assert "super-secret-value" not in text
    assert '"record_type": "job"' in text

    summary = TraceReporter(store).summary(task_id)
    assert summary["background_job_count"] == 1


def test_v6_database_migrates_through_v7_to_v11(tmp_path: Path):
    db = tmp_path / "runtime.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    _execute_all(conn, _BASE_SCHEMA)
    _apply_v6_durable_context(conn)
    conn.execute(
        "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (6, ?, ?, 0)",
        ("v6_durable_context", V6_CHECKSUM),
    )
    conn.commit()
    conn.close()

    assert SchemaManager(db).inspect().current_version == 6
    report = SchemaManager(db).migrate()
    assert report.from_version == 6
    assert report.to_version == 11
    assert report.applied == (
        "v7_background_jobs",
        "v8_tool_registry_mcp",
        "v9_subagents_mailbox",
        "v10_verified_subtask",
        "v11_frozen_dag",
    )

    store = EventStore(db)
    assert store._fetchone(
        "SELECT checksum FROM schema_migrations WHERE version = 7"
    )["checksum"] == V7_CHECKSUM
    assert store._fetchone(
        "SELECT checksum FROM schema_migrations WHERE version = 8"
    )["checksum"] == V8_CHECKSUM
    assert store._fetchone(
        "SELECT checksum FROM schema_migrations WHERE version = 9"
    )["checksum"] == V9_CHECKSUM
    assert store._fetchone(
        "SELECT checksum FROM schema_migrations WHERE version = 10"
    )["checksum"] == V10_CHECKSUM
    assert store._fetchone(
        "SELECT checksum FROM schema_migrations WHERE version = 11"
    )["checksum"] == V11_CHECKSUM
    tables = {
        str(row[0])
        for row in store._fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('agent_jobs', 'job_runs', 'cron_schedules', "
            "'tool_registrations', 'mcp_connections', "
            "'subagent_runs', 'mailboxes', 'mailbox_messages', 'plan_approvals', "
            "'verifier_runs', 'semantic_checkpoints')"
        )
    }
    assert tables == {
        "agent_jobs",
        "job_runs",
        "cron_schedules",
        "tool_registrations",
        "mcp_connections",
        "subagent_runs",
        "mailboxes",
        "mailbox_messages",
        "plan_approvals",
        "verifier_runs",
        "semantic_checkpoints",
    }
    assert store.integrity_check() == []


def test_claim_requires_a_bound_lease_and_matching_repo(tmp_path: Path):
    store, repo, task_id, _ = _leased_store(tmp_path)
    store.enqueue_job("job-unbound", task_id, repo, "shell", {"cmd": "x"})

    unbound = EventStore(tmp_path / "runtime.db")
    with pytest.raises(LeaseLost):
        unbound.claim_job("job-unbound", ttl=60)

    other_repo = tmp_path / "other"
    unbound.bind_lease(str(other_repo.resolve()), "owner-z", 99)
    with pytest.raises(LeaseLost):
        unbound.claim_job("job-unbound", ttl=60)
