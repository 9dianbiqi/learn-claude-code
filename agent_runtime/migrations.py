from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 12
MIGRATION_NAME = "v12_stale_evidence"
V5_MIGRATION_NAME = "v5_effect_ledger"
V6_MIGRATION_NAME = "v6_durable_context"
V7_MIGRATION_NAME = "v7_background_jobs"
V8_MIGRATION_NAME = "v8_tool_registry_mcp"
V9_MIGRATION_NAME = "v9_subagents_mailbox"
V10_MIGRATION_NAME = "v10_verified_subtask"
V11_MIGRATION_NAME = "v11_frozen_dag"
V12_MIGRATION_NAME = "v12_stale_evidence"
MAX_EVENT_PAYLOAD_BYTES = 1 * 1024 * 1024

_TASK_STATUSES = frozenset({
    "created", "running", "waiting_approval", "needs_review", "completed", "failed", "aborted",
})
_CHECKPOINT_PHASES = frozenset({
    "input_ready", "model_responded", "tool_results_appended", "waiting_approval",
    "needs_review", "completed", "failed", "aborted",
})
_TOOL_STATUSES = frozenset({
    "planned", "running", "waiting_approval", "needs_review", "succeeded", "failed", "denied", "aborted",
})
_RESERVATION_STATES = frozenset({"running", "completed", "unknown", "cancelled"})


class SchemaError(RuntimeError):
    """Base class for fail-closed schema errors."""


class SchemaUpgradeRequired(SchemaError):
    """Raised when a normal Runtime command encounters an old database."""

    def __init__(self, database: str | Path, current_version: int, target_version: int = SCHEMA_VERSION):
        self.database = str(database)
        self.current_version = int(current_version)
        self.target_version = int(target_version)
        database_path = Path(database)
        repo_root = database_path.parent.parent if database_path.parent.name == ".agent_runtime" else database_path.parent
        super().__init__(
            f"Runtime database schema v{self.current_version} requires an explicit migration to "
            f"v{self.target_version}; run agent-runtime db-migrate --repo {repo_root}"
        )


class UnsupportedSchemaVersion(SchemaError):
    """Raised when a database is newer than this Runtime."""


class MigrationChecksumMismatch(SchemaError):
    """Raised when durable migration metadata differs from code."""


class MigrationBlocked(SchemaError):
    """Raised when migration cannot safely acquire the database boundary."""


class MigrationValidationError(SchemaError):
    """Raised when legacy data cannot be mapped without guessing."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class SchemaStatus:
    current_version: int
    target_version: int
    pending_migrations: tuple[Migration, ...]
    database_exists: bool
    has_business_tables: bool
    integrity_check: tuple[str, ...] = ()
    active_leases: int = 0

    @property
    def pending_names(self) -> list[str]:
        return [migration.name for migration in self.pending_migrations]

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "target_version": self.target_version,
            "pending_migrations": self.pending_names,
            "database_exists": self.database_exists,
            "has_business_tables": self.has_business_tables,
            "integrity_check": list(self.integrity_check),
            "active_leases": self.active_leases,
        }


@dataclass(frozen=True)
class BackupResult:
    filename: str
    path: str
    sha256: str
    integrity_check: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MigrationReport:
    ok: bool
    from_version: int
    to_version: int
    applied: tuple[str, ...]
    backup: BackupResult | None
    integrity_check: str
    dry_run: bool = False
    preflight: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["applied"] = list(self.applied)
        result["preflight"] = list(self.preflight)
        result["backup"] = self.backup.as_dict() if self.backup else None
        return result


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise MigrationValidationError("legacy JSON payload is invalid") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _stable_args_hash(name: str, args: dict[str, Any]) -> str:
    return _sha256_text(_json({"name": name, "input": args}))


def _operation_id(task_id: str, tool_use_id: str) -> str:
    return "op_" + _sha256_text(f"{task_id}\0{tool_use_id}")[:48]


def _dedupe_key(task_id: str, tool_use_id: str) -> str:
    return "dedupe_" + _sha256_text(f"{task_id}\0{tool_use_id}")


def _execute_all(conn: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    """Execute DDL one statement at a time so no implicit script commit is possible."""
    for statement in statements:
        conn.execute(statement)


_BASE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at REAL NOT NULL,
        duration_ms REAL,
        backup_filename TEXT,
        backup_sha256 TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        repo_root TEXT NOT NULL,
        prompt TEXT NOT NULL,
        model TEXT NOT NULL,
        status TEXT NOT NULL,
        checkpoint_id INTEGER,
        last_error TEXT,
        version INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        phase TEXT NOT NULL,
        messages_json TEXT NOT NULL,
        cursor_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_calls (
        model_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        turn INTEGER NOT NULL,
        status TEXT NOT NULL,
        request_json TEXT NOT NULL,
        response_json TEXT,
        stop_reason TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        started_at REAL NOT NULL,
        finished_at REAL,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_calls (
        tool_call_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        tool_use_id TEXT NOT NULL,
        operation_id TEXT,
        turn INTEGER NOT NULL,
        name TEXT NOT NULL,
        args_json TEXT NOT NULL,
        args_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        permission TEXT,
        permission_rule TEXT,
        permission_reason TEXT,
        effect TEXT,
        before_state_json TEXT,
        expected_after_json TEXT,
        output TEXT,
        error TEXT,
        returncode INTEGER,
        stdout TEXT,
        stderr TEXT,
        timed_out INTEGER NOT NULL DEFAULT 0,
        execution_status TEXT,
        execution_attempts INTEGER NOT NULL DEFAULT 0,
        effect_attempts INTEGER NOT NULL DEFAULT 0,
        effect_confirmed INTEGER NOT NULL DEFAULT 0,
        effect_confirmation TEXT,
        effect_key TEXT,
        version INTEGER NOT NULL DEFAULT 0,
        started_at REAL,
        finished_at REAL,
        UNIQUE(task_id, tool_use_id),
        FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_observations (
        observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        path TEXT NOT NULL,
        exists_now INTEGER NOT NULL,
        sha256 TEXT,
        identity_json TEXT,
        observed_at REAL NOT NULL,
        source_tool_use_id TEXT,
        UNIQUE(task_id, path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leases (
        repo_root TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        owner_id TEXT NOT NULL,
        heartbeat_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        fencing_token INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        tool_use_id TEXT NOT NULL,
        adapter TEXT NOT NULL,
        semantics TEXT NOT NULL,
        effect_scope TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE,
        idempotency_key TEXT,
        args_hash TEXT NOT NULL,
        state TEXT NOT NULL,
        request_json TEXT NOT NULL,
        result_json TEXT,
        result_digest TEXT,
        probe_evidence_json TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        dispatched_at REAL,
        completed_at REAL,
        FOREIGN KEY(task_id, tool_use_id)
            REFERENCES tool_calls(task_id, tool_use_id),
        UNIQUE(task_id, tool_use_id),
        CHECK (
            semantics IN (
                'replay_safe',
                'idempotent',
                'reconcilable',
                'opaque'
            )
        ),
        CHECK (
            state IN (
                'prepared',
                'dispatched',
                'committed',
                'failed',
                'unknown',
                'cancelled'
            )
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operation_outbox (
        operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
        state TEXT NOT NULL,
        available_at REAL NOT NULL,
        claimed_by TEXT,
        claimed_until REAL,
        delivery_attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_at REAL NOT NULL,
        CHECK (
            state IN (
                'pending',
                'claimed',
                'delivered',
                'blocked',
                'cancelled'
            )
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS effect_reservations (
        reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        tool_use_id TEXT NOT NULL,
        operation_id TEXT,
        owner_id TEXT NOT NULL,
        owner_pid INTEGER NOT NULL,
        fencing_token INTEGER NOT NULL,
        effect TEXT NOT NULL,
        state TEXT NOT NULL,
        started_at REAL NOT NULL,
        deadline_at REAL,
        finished_at REAL,
        details_json TEXT,
        CHECK (state IN ('running', 'completed', 'unknown', 'cancelled')),
        FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_operations_idempotency ON operations(effect_scope, idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_operations_task ON operations(task_id, created_at, operation_id)",
    "CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_operation_outbox_state ON operation_outbox(state, available_at)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(task_id, tool_call_row_id)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_operation ON tool_calls(operation_id)",
    "CREATE INDEX IF NOT EXISTS idx_effect_reservations_task_tool ON effect_reservations(task_id, tool_use_id, reservation_id)",
    "CREATE INDEX IF NOT EXISTS idx_effect_reservations_operation ON effect_reservations(operation_id)",
    "CREATE INDEX IF NOT EXISTS idx_effect_reservations_state ON effect_reservations(state, task_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_effect_reservation_running ON effect_reservations(task_id, tool_use_id) WHERE state = 'running'",
    "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, checkpoint_id)",
    "CREATE INDEX IF NOT EXISTS idx_model_calls_task ON model_calls(task_id, model_call_id)",
)


_V5_MIGRATION_SOURCE = "\n".join(statement.strip() for statement in _BASE_SCHEMA)
V5_CHECKSUM = _sha256_text(_V5_MIGRATION_SOURCE)


_V6_ADDITIONS = (
    """
    CREATE TABLE IF NOT EXISTS memories (
        memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        source_checkpoint_id INTEGER REFERENCES checkpoints(checkpoint_id),
        evidence_hash TEXT,
        created_at REAL NOT NULL,
        expires_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_links (
        source_memory_id INTEGER NOT NULL REFERENCES memories(memory_id),
        target_memory_id INTEGER NOT NULL REFERENCES memories(memory_id),
        relation TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY(source_memory_id, target_memory_id, relation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS summaries (
        summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        scope TEXT NOT NULL,
        content TEXT NOT NULL,
        source_turn_range TEXT,
        evidence_hash TEXT,
        model_call_id INTEGER REFERENCES model_calls(model_call_id),
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        status TEXT NOT NULL DEFAULT 'active',
        dag_hash TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (status IN ('active', 'superseded', 'completed', 'failed'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_items (
        plan_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL REFERENCES plans(plan_id),
        subtask_id TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        blocked_by_json TEXT NOT NULL DEFAULT '[]',
        completion_summary TEXT,
        evidence_hash TEXT,
        version INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (plan_id, subtask_id),
        CHECK (status IN (
            'pending', 'in_progress', 'verifying', 'completed', 'failed', 'retryable'
        ))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_task ON memories(task_id, kind, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memories_expiry ON memories(task_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_summaries_task ON summaries(task_id, scope, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_plans_task ON plans(task_id, status, plan_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_plans_active_task ON plans(task_id) WHERE status = 'active'",
    "CREATE INDEX IF NOT EXISTS idx_plan_items_plan ON plan_items(plan_id, status, plan_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_plan_items_task_dep ON plan_items(plan_id, subtask_id)",
)


_V6_MIGRATION_SOURCE = "\n".join(
    [statement.strip() for statement in _BASE_SCHEMA]
    + [statement.strip() for statement in _V6_ADDITIONS]
)
V6_CHECKSUM = _sha256_text(_V6_MIGRATION_SOURCE)


_V7_ADDITIONS = (
    """
    CREATE TABLE IF NOT EXISTS agent_jobs (
        job_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        repo_root TEXT NOT NULL,
        lane_id TEXT,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        available_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (status IN (
            'pending', 'claimed', 'running', 'completed', 'failed', 'retryable', 'cancelled'
        ))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_runs (
        run_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES agent_jobs(job_id),
        owner_id TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        status TEXT NOT NULL,
        heartbeat_at REAL,
        lease_expires_at REAL,
        started_at REAL,
        completed_at REAL,
        error TEXT,
        result_digest TEXT,
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cron_schedules (
        schedule_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        expression TEXT NOT NULL,
        job_kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_triggered_at REAL,
        next_trigger_at REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON agent_jobs(status, available_at, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_task ON agent_jobs(task_id, created_at, job_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_runs_active ON job_runs(job_id) WHERE status = 'running'",
    "CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id, run_id)",
    "CREATE INDEX IF NOT EXISTS idx_cron_enabled ON cron_schedules(enabled, next_trigger_at)",
)


_V7_MIGRATION_SOURCE = "\n".join(
    [statement.strip() for statement in _BASE_SCHEMA]
    + [statement.strip() for statement in _V6_ADDITIONS]
    + [statement.strip() for statement in _V7_ADDITIONS]
)
V7_CHECKSUM = _sha256_text(_V7_MIGRATION_SOURCE)


_V8_ADDITIONS = (
    """
    CREATE TABLE IF NOT EXISTS tool_registrations (
        registration_id TEXT PRIMARY KEY,
        tool_name TEXT NOT NULL UNIQUE,
        adapter_kind TEXT NOT NULL,
        connection_id TEXT,
        server_name TEXT,
        source_tool_name TEXT,
        description TEXT NOT NULL DEFAULT '',
        schema_json TEXT NOT NULL,
        effect_kind TEXT NOT NULL,
        permission_json TEXT NOT NULL,
        timeout_seconds REAL,
        enabled INTEGER NOT NULL DEFAULT 1,
        version INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (adapter_kind IN ('builtin', 'mcp')),
        CHECK (effect_kind IN ('read_only', 'file_write', 'idempotent', 'unknown_write', 'opaque')),
        CHECK (timeout_seconds IS NULL OR timeout_seconds > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_connections (
        connection_id TEXT PRIMARY KEY,
        server_name TEXT NOT NULL UNIQUE,
        transport TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        args_json TEXT NOT NULL DEFAULT '[]',
        auth_profile_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'configured',
        last_connected_at REAL,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (transport IN ('stdio')),
        CHECK (status IN ('configured', 'connected', 'error', 'disabled'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_registrations_adapter ON tool_registrations(adapter_kind, enabled)",
    "CREATE INDEX IF NOT EXISTS idx_mcp_connections_status ON mcp_connections(status, server_name)",
)


_V8_MIGRATION_SOURCE = "\n".join(
    [statement.strip() for statement in _BASE_SCHEMA]
    + [statement.strip() for statement in _V6_ADDITIONS]
    + [statement.strip() for statement in _V7_ADDITIONS]
    + [statement.strip() for statement in _V8_ADDITIONS]
)
V8_CHECKSUM = _sha256_text(_V8_MIGRATION_SOURCE)


_V9_ADDITIONS = (
    """
    CREATE TABLE IF NOT EXISTS subagent_runs (
        subagent_run_id TEXT PRIMARY KEY,
        parent_task_id TEXT,
        child_task_id TEXT NOT NULL UNIQUE,
        repo_root TEXT NOT NULL,
        lane_id TEXT NOT NULL DEFAULT 'default',
        role TEXT NOT NULL,
        status TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        fencing_token TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        messages_json TEXT NOT NULL,
        result_summary TEXT,
        error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'needs_review', 'cancelled'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_subagent_runs_parent ON subagent_runs(parent_task_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_subagent_runs_child ON subagent_runs(child_task_id)",
    """
    CREATE TABLE IF NOT EXISTS mailboxes (
        mailbox_id TEXT PRIMARY KEY,
        owner_task_id TEXT NOT NULL UNIQUE,
        owner_role TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mailbox_messages (
        message_id TEXT PRIMARY KEY,
        mailbox_id TEXT NOT NULL,
        sender_task_id TEXT NOT NULL,
        recipient_task_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'delivered',
        created_at REAL NOT NULL,
        read_at REAL,
        FOREIGN KEY (mailbox_id) REFERENCES mailboxes(mailbox_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mailbox_messages_pending ON mailbox_messages(mailbox_id, status, created_at)",
    """
    CREATE TABLE IF NOT EXISTS plan_approvals (
        approval_id TEXT PRIMARY KEY,
        subagent_run_id TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        decided_by TEXT,
        reason TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (status IN ('requested', 'approved', 'rejected', 'superseded')),
        FOREIGN KEY (subagent_run_id) REFERENCES subagent_runs(subagent_run_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plan_approvals_run ON plan_approvals(subagent_run_id, status)",
)


_V9_MIGRATION_SOURCE = "\n".join(
    [statement.strip() for statement in _BASE_SCHEMA]
    + [statement.strip() for statement in _V6_ADDITIONS]
    + [statement.strip() for statement in _V7_ADDITIONS]
    + [statement.strip() for statement in _V8_ADDITIONS]
    + [statement.strip() for statement in _V9_ADDITIONS]
)
V9_CHECKSUM = _sha256_text(_V9_MIGRATION_SOURCE)


_V10_ADDITIONS = (
    "ALTER TABLE plan_items ADD COLUMN verifier_bundle_hash TEXT",
    """
    CREATE TABLE IF NOT EXISTS verifier_runs (
        verifier_run_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        plan_item_id INTEGER NOT NULL REFERENCES plan_items(plan_item_id),
        subtask_id TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL,
        completion_summary TEXT NOT NULL,
        verifier_id TEXT NOT NULL,
        verifier_version TEXT NOT NULL,
        verification_rule TEXT NOT NULL,
        verifier_bundle_hash TEXT NOT NULL,
        verifier_implementation_hash TEXT NOT NULL,
        evidence_manifest_json TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        authoritative INTEGER NOT NULL DEFAULT 0,
        execution_checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(checkpoint_id),
        created_at REAL NOT NULL,
        CHECK (status IN ('pass', 'fail', 'uncertain')),
        CHECK (authoritative IN (0, 1)),
        CHECK (authoritative = 0 OR status = 'pass')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS semantic_checkpoints (
        semantic_checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        plan_item_id INTEGER NOT NULL REFERENCES plan_items(plan_item_id),
        subtask_id TEXT NOT NULL,
        verifier_run_id TEXT NOT NULL UNIQUE REFERENCES verifier_runs(verifier_run_id),
        execution_checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(checkpoint_id),
        completion_summary TEXT NOT NULL,
        verifier_id TEXT NOT NULL,
        verifier_version TEXT NOT NULL,
        verification_rule TEXT NOT NULL,
        verifier_bundle_hash TEXT NOT NULL,
        verifier_implementation_hash TEXT NOT NULL,
        evidence_manifest_json TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_verifier_runs_task ON verifier_runs(task_id, created_at, verifier_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_verifier_runs_plan_item ON verifier_runs(plan_item_id, created_at, verifier_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_semantic_checkpoints_task ON semantic_checkpoints(task_id, semantic_checkpoint_id)",
    "CREATE INDEX IF NOT EXISTS idx_semantic_checkpoints_plan_item ON semantic_checkpoints(plan_item_id, semantic_checkpoint_id)",
)

_V10_MIGRATION_SOURCE = "\n".join(
    [_V9_MIGRATION_SOURCE.strip()]
    + [statement.strip() for statement in _V10_ADDITIONS]
)
V10_CHECKSUM = _sha256_text(_V10_MIGRATION_SOURCE)


_V11_ADDITIONS = (
    "ALTER TABLE plan_items ADD COLUMN max_turns INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE plan_items ADD COLUMN consumed_turns INTEGER NOT NULL DEFAULT 0",
)

_V11_MIGRATION_SOURCE = "\n".join(
    [_V10_MIGRATION_SOURCE.strip()]
    + [statement.strip() for statement in _V11_ADDITIONS]
)
V11_CHECKSUM = _sha256_text(_V11_MIGRATION_SOURCE)


_V12_ADDITIONS = (
    """
    CREATE TABLE IF NOT EXISTS semantic_checkpoint_state_events (
        state_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        semantic_checkpoint_id INTEGER NOT NULL
            REFERENCES semantic_checkpoints(semantic_checkpoint_id),
        plan_item_id INTEGER NOT NULL REFERENCES plan_items(plan_item_id),
        state TEXT NOT NULL,
        reason TEXT NOT NULL,
        replacement_checkpoint_id INTEGER
            REFERENCES semantic_checkpoints(semantic_checkpoint_id),
        observed_manifest_json TEXT NOT NULL DEFAULT '[]',
        observation_complete INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        CHECK (state IN ('valid', 'stale', 'superseded')),
        CHECK (observation_complete IN (0, 1))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_checkpoint_state_task ON "
    "semantic_checkpoint_state_events(task_id, semantic_checkpoint_id, state_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_checkpoint_state_item ON "
    "semantic_checkpoint_state_events(plan_item_id, state_event_id)",
    """
    CREATE TRIGGER IF NOT EXISTS immutable_verifier_runs
    BEFORE UPDATE ON verifier_runs
    BEGIN
        SELECT RAISE(ABORT, 'verifier_runs are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS immutable_verifier_runs_delete
    BEFORE DELETE ON verifier_runs
    BEGIN
        SELECT RAISE(ABORT, 'verifier_runs are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS immutable_semantic_checkpoints
    BEFORE UPDATE ON semantic_checkpoints
    BEGIN
        SELECT RAISE(ABORT, 'semantic_checkpoints are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS immutable_semantic_checkpoints_delete
    BEFORE DELETE ON semantic_checkpoints
    BEGIN
        SELECT RAISE(ABORT, 'semantic_checkpoints are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS immutable_checkpoint_state_events
    BEFORE UPDATE ON semantic_checkpoint_state_events
    BEGIN
        SELECT RAISE(ABORT, 'semantic_checkpoint_state_events are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS immutable_checkpoint_state_events_delete
    BEFORE DELETE ON semantic_checkpoint_state_events
    BEGIN
        SELECT RAISE(ABORT, 'semantic_checkpoint_state_events are append-only');
    END
    """,
)

_V12_MIGRATION_SOURCE = "\n".join(
    [_V11_MIGRATION_SOURCE.strip()]
    + [statement.strip() for statement in _V12_ADDITIONS]
)
V12_CHECKSUM = _sha256_text(_V12_MIGRATION_SOURCE)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _business_tables(table_names: set[str]) -> set[str]:
    return table_names & {
        "tasks",
        "checkpoints",
        "model_calls",
        "tool_calls",
        "file_observations",
        "events",
        "leases",
        "effect_reservations",
        "operations",
        "operation_outbox",
        "memories",
        "memory_links",
        "summaries",
        "plans",
        "plan_items",
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


def _integrity(conn: sqlite3.Connection) -> list[str]:
    result = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    return [] if result == ["ok"] else result


def _reservation_state(reservations: list[sqlite3.Row]) -> str:
    states = {str(row["state"]) for row in reservations}
    if "completed" in states:
        return "committed"
    if "unknown" in states or "running" in states:
        return "unknown"
    if states and states <= {"cancelled"}:
        return "cancelled"
    return "unknown"


def _outbox_state(operation_state: str) -> str:
    return {
        "committed": "delivered",
        "unknown": "blocked",
        "cancelled": "cancelled",
    }.get(operation_state, "pending")


def _adapter_and_semantics(tool_name: str, effect: str | None) -> tuple[str, str]:
    effect_name = str(effect or "")
    if effect_name == "file_write":
        return "file", "reconcilable"
    if effect_name == "idempotent":
        return "legacy", "idempotent"
    return "legacy", "opaque"


def _legacy_projection_violations(
    conn: sqlite3.Connection,
    task_id: str | None = None,
    *,
    allow_prepared_operations: bool = False,
) -> list[str]:
    """Validate the v0.1 task/checkpoint/event/tool/reservation projection.

    This is deliberately shared by migration pre-validation and the runtime
    invariant scanner.  The only v5-specific exception is a prepared
    operation whose reservation is already running while its tool call is
    still planned; v4 has no operation table and therefore cannot use that
    exception.
    """
    violations: list[str] = []
    task_clause = " WHERE task_id = ?" if task_id is not None else ""
    task_params = (task_id,) if task_id is not None else ()
    tasks = conn.execute(f"SELECT * FROM tasks{task_clause}", task_params).fetchall()
    task_by_id = {str(row["task_id"]): row for row in tasks}

    def payload(event: sqlite3.Row, owner_task_id: str | None = None) -> dict[str, Any]:
        value = _loads(event["payload_json"], {})
        if not isinstance(value, dict):
            event_task_id = owner_task_id
            if event_task_id is None and "task_id" in event.keys():
                event_task_id = str(event["task_id"])
            violations.append(f"task {event_task_id or 'unknown'}: event payload is not an object")
            return {}
        return value

    for task in tasks:
        task_key = str(task["task_id"])
        prefix = f"task {task_key}"
        status = str(task["status"])
        if status not in _TASK_STATUSES:
            violations.append(f"{prefix}: invalid task status {status}")
        checkpoint = conn.execute(
            "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (task["checkpoint_id"],)
        ).fetchone() if task["checkpoint_id"] is not None else None
        if checkpoint is None:
            violations.append(f"{prefix}: missing checkpoint pointer")
        elif checkpoint["task_id"] != task_key:
            violations.append(f"{prefix}: checkpoint belongs to another task")
        elif status == "completed" and checkpoint["phase"] != "completed":
            violations.append(f"{prefix}: completed task has phase {checkpoint['phase']}")
        elif status == "aborted" and checkpoint["phase"] != "aborted":
            violations.append(f"{prefix}: aborted task has phase {checkpoint['phase']}")
        elif status == "failed" and checkpoint["phase"] != "failed":
            violations.append(f"{prefix}: failed task has phase {checkpoint['phase']}")
        if checkpoint is not None and checkpoint["phase"] not in _CHECKPOINT_PHASES:
            violations.append(f"{prefix}: invalid checkpoint phase {checkpoint['phase']}")

        calls = conn.execute(
            "SELECT status FROM tool_calls WHERE task_id = ?", (task_key,)
        ).fetchall()
        invalid_statuses = {str(row["status"]) for row in calls} - _TOOL_STATUSES
        if invalid_statuses:
            violations.append(f"{prefix}: invalid tool status {sorted(invalid_statuses)}")
        if status == "aborted" and any(row["status"] in {"planned", "running"} for row in calls):
            violations.append(f"{prefix}: aborted task has executable tool call")
        if status == "completed" and any(row["status"] in {"planned", "running"} for row in calls):
            violations.append(f"{prefix}: completed task has executable tool call")
        call_statuses = {str(row["status"]) for row in calls}
        events = conn.execute(
            "SELECT type, payload_json FROM events WHERE task_id = ? ORDER BY event_id", (task_key,)
        ).fetchall()
        event_types = {str(row["type"]) for row in events}
        if status == "completed" and "task_completed" not in event_types:
            violations.append(f"{prefix}: completed task missing task_completed event")
        if status == "aborted" and "task_aborted" not in event_types:
            violations.append(f"{prefix}: aborted task missing task_aborted event")

        if status in {"needs_review", "waiting_approval"}:
            expected_phase = status
            if checkpoint is None or checkpoint["phase"] != expected_phase:
                violations.append(f"{prefix}: {status} task has incompatible checkpoint")
            checkpoint_saved = False
            if checkpoint is not None:
                for event in conn.execute(
                    "SELECT payload_json FROM events WHERE task_id = ? AND type = 'checkpoint_saved'",
                    (task_key,),
                ):
                    event_payload = payload(event, task_key)
                    if (
                        event_payload.get("checkpoint_id") == checkpoint["checkpoint_id"]
                        and event_payload.get("phase") == expected_phase
                    ):
                        checkpoint_saved = True
                        break
            if not checkpoint_saved:
                violations.append(f"{prefix}: review checkpoint missing checkpoint_saved event")
            expected_event = "task_needs_review" if expected_phase == "needs_review" else "task_waiting_approval"
            matching_review_event = False
            for event in conn.execute(
                "SELECT payload_json FROM events WHERE task_id = ? AND type = ? ORDER BY event_id DESC",
                (task_key, expected_event),
            ):
                event_payload = payload(event, task_key)
                if checkpoint is not None and event_payload.get("checkpoint_id") == checkpoint["checkpoint_id"]:
                    matching_review_event = True
                    break
            if not matching_review_event:
                violations.append(f"{prefix}: missing {expected_event} transition event")
            if expected_phase == "needs_review":
                if not any(status_value == "needs_review" for status_value in call_statuses):
                    violations.append(f"{prefix}: needs_review task has no needs_review tool call")
                if any(status_value in {"planned", "running", "waiting_approval"} for status_value in call_statuses):
                    violations.append(f"{prefix}: needs_review task has executable tool call")
            elif "waiting_approval" not in call_statuses:
                violations.append(f"{prefix}: waiting_approval task has no waiting tool call")
            if any(status_value in {"planned", "running"} for status_value in call_statuses):
                violations.append(f"{prefix}: review task has executable tool call")
        elif checkpoint is not None and checkpoint["phase"] in {"needs_review", "waiting_approval"}:
            checkpoint_saved = False
            for event in conn.execute(
                "SELECT payload_json FROM events WHERE task_id = ? AND type = 'checkpoint_saved'",
                (task_key,),
            ):
                event_payload = payload(event, task_key)
                if (
                    event_payload.get("checkpoint_id") == checkpoint["checkpoint_id"]
                    and event_payload.get("phase") == checkpoint["phase"]
                ):
                    checkpoint_saved = True
                    break
            if not checkpoint_saved:
                violations.append(f"{prefix}: review checkpoint missing checkpoint_saved event")
            resolved = False
            for event in conn.execute(
                "SELECT payload_json FROM events WHERE task_id = ? "
                "AND type IN ('permission_approved', 'permission_denied', 'review_resolved')",
                (task_key,),
            ):
                event_payload = payload(event, task_key)
                if event_payload.get("checkpoint_id") == checkpoint["checkpoint_id"]:
                    resolved = True
                    break
            if not resolved:
                violations.append(f"{prefix}: running task has unresolved review checkpoint")

        for event in conn.execute(
            "SELECT payload_json FROM events WHERE task_id = ? AND type = 'checkpoint_saved'",
            (task_key,),
        ):
            event_payload = payload(event, task_key)
            checkpoint_id = event_payload.get("checkpoint_id")
            if checkpoint_id is None or conn.execute(
                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                (checkpoint_id, task_key),
            ).fetchone() is None:
                violations.append(f"{prefix}: checkpoint_saved points to missing checkpoint")

    event_clause = " WHERE task_id = ?" if task_id is not None else ""
    event_params = (task_id,) if task_id is not None else ()
    for event in conn.execute(f"SELECT task_id, payload_json FROM events{event_clause}", event_params):
        event_task_id = str(event["task_id"])
        if event_task_id not in task_by_id:
            violations.append(f"event for missing task: {event_task_id}")
        payload(event, event_task_id)

    reservation_clause = " WHERE task_id = ?" if task_id is not None else ""
    reservation_params = (task_id,) if task_id is not None else ()
    reservations = conn.execute(
        f"SELECT * FROM effect_reservations{reservation_clause}", reservation_params
    ).fetchall()
    for reservation in reservations:
        reservation_id = reservation["reservation_id"]
        reservation_task_id = str(reservation["task_id"])
        state = str(reservation["state"])
        if state not in _RESERVATION_STATES:
            violations.append(f"reservation {reservation_id}: invalid state")
        if not reservation["owner_id"] or int(reservation["fencing_token"] or 0) < 1:
            violations.append(f"reservation {reservation_id}: invalid owner or fencing token")
        task = task_by_id.get(reservation_task_id)
        if task is None:
            violations.append(f"reservation {reservation_id}: missing task")
        else:
            task_status = str(task["status"])
            if state in {"running", "unknown"} and task_status in {"completed", "failed", "aborted"}:
                violations.append(f"reservation {reservation_id}: unresolved on terminal task")
            elif state == "unknown" and task_status not in {"created", "running", "needs_review"}:
                violations.append(f"reservation {reservation_id}: unknown on incompatible task")
        tool = conn.execute(
            "SELECT status FROM tool_calls WHERE task_id = ? AND tool_use_id = ?",
            (reservation_task_id, reservation["tool_use_id"]),
        ).fetchone()
        if tool is None:
            violations.append(f"reservation {reservation_id}: missing tool call")
        elif state == "completed" and tool["status"] != "succeeded":
            violations.append(f"reservation {reservation_id}: completed without succeeded tool")
        elif state == "running" and tool["status"] != "running":
            prepared_projection = False
            if allow_prepared_operations and reservation["operation_id"] is not None:
                operation = conn.execute(
                    "SELECT state FROM operations WHERE operation_id = ?",
                    (reservation["operation_id"],),
                ).fetchone()
                prepared_projection = (
                    operation is not None
                    and operation["state"] == "prepared"
                    and tool["status"] == "planned"
                )
            if not prepared_projection:
                violations.append(f"reservation {reservation_id}: running without running tool")
    return violations


def _validate_legacy_rows(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[sqlite3.Row, list[sqlite3.Row]]]:
    tables = _table_names(conn)
    required = {"tasks", "checkpoints", "events", "tool_calls", "effect_reservations"}
    if not required <= tables:
        missing = ", ".join(sorted(required - tables))
        raise MigrationValidationError(f"v4 database is missing required tables: {missing}")
    projection_violations = _legacy_projection_violations(conn)
    if projection_violations:
        raise MigrationValidationError(projection_violations[0])
    task_rows = conn.execute("SELECT task_id, status FROM tasks").fetchall()
    task_statuses = {str(row["task_id"]): str(row["status"]) for row in task_rows}
    valid_tool_statuses = {
        "planned", "running", "waiting_approval", "needs_review",
        "succeeded", "failed", "denied", "aborted",
    }
    calls = conn.execute("SELECT * FROM tool_calls ORDER BY tool_call_row_id").fetchall()
    by_key: dict[tuple[str, str], sqlite3.Row] = {}
    for call in calls:
        key = (str(call["task_id"]), str(call["tool_use_id"]))
        if key[0] not in task_statuses:
            raise MigrationValidationError(f"legacy tool call references missing task: {key[0]}")
        if key in by_key:
            raise MigrationValidationError(f"duplicate legacy tool call: {key[0]}/{key[1]}")
        if str(call["status"]) not in valid_tool_statuses:
            raise MigrationValidationError(
                f"legacy tool call has invalid status: {key[0]}/{key[1]}"
            )
        args = _loads(call["args_json"], {})
        if not isinstance(args, dict):
            raise MigrationValidationError(f"legacy args are not an object: {key[0]}/{key[1]}")
        expected_hash = _stable_args_hash(str(call["name"]), args)
        if str(call["args_hash"]) != expected_hash:
            raise MigrationValidationError(f"legacy args hash mismatch: {key[0]}/{key[1]}")
        by_key[key] = call

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for reservation in conn.execute("SELECT * FROM effect_reservations ORDER BY reservation_id").fetchall():
        key = (str(reservation["task_id"]), str(reservation["tool_use_id"]))
        if key not in by_key:
            raise MigrationValidationError(
                f"legacy reservation has no matching tool call: {key[0]}/{key[1]}"
            )
        if str(reservation["state"]) not in {"running", "completed", "unknown", "cancelled"}:
            raise MigrationValidationError(
                f"legacy reservation has invalid state: {key[0]}/{key[1]}"
            )
        if not reservation["owner_id"] or int(reservation["owner_pid"] or 0) <= 0:
            raise MigrationValidationError(
                f"legacy reservation has invalid owner: {key[0]}/{key[1]}"
            )
        if int(reservation["fencing_token"] or 0) < 1:
            raise MigrationValidationError(
                f"legacy reservation has invalid fencing token: {key[0]}/{key[1]}"
            )
        if str(reservation["state"]) in {"running", "unknown"} and task_statuses[key[0]] in {
            "completed", "failed", "aborted"
        }:
            raise MigrationValidationError(
                f"legacy unresolved reservation belongs to terminal task: {key[0]}/{key[1]}"
            )
        if str(reservation["state"]) == "unknown" and task_statuses[key[0]] not in {
            "created", "running", "needs_review"
        }:
            raise MigrationValidationError(
                f"legacy unknown reservation belongs to incompatible task: {key[0]}/{key[1]}"
            )
        effect_name = str(by_key[key]["effect"] or "")
        tool_name = str(by_key[key]["name"])
        if effect_name == "read_only" or tool_name in {"read_file", "glob"}:
            raise MigrationValidationError(
                f"legacy read-only tool has an effect reservation: {key[0]}/{key[1]}"
            )
        if tool_name in {"write_file", "edit_file"} and effect_name != "file_write":
            raise MigrationValidationError(
                f"legacy file tool has incompatible effect classification: {key[0]}/{key[1]}"
            )
        groups.setdefault(key, []).append(reservation)
    grouped = {key: (by_key[key], rows) for key, rows in groups.items()}
    for key, (call, reservations) in grouped.items():
        reservation_states = {str(row["state"]) for row in reservations}
        tool_status = str(call["status"])
        if "completed" in reservation_states and tool_status != "succeeded":
            raise MigrationValidationError(
                f"legacy completed reservation has non-succeeded tool: {key[0]}/{key[1]}"
            )
        if "running" in reservation_states and tool_status != "running":
            raise MigrationValidationError(
                f"legacy running reservation has non-running tool: {key[0]}/{key[1]}"
            )
    return grouped


def _backfill_v5(conn: sqlite3.Connection) -> None:
    grouped = _validate_legacy_rows(conn)
    task_roots = {
        str(row["task_id"]): str(row["repo_root"])
        for row in conn.execute("SELECT task_id, repo_root FROM tasks").fetchall()
    }
    now = _now()
    for (task_id, tool_use_id), (call, reservations) in grouped.items():
        if task_id not in task_roots:
            raise MigrationValidationError(f"legacy tool call references missing task: {task_id}")
        args = _loads(call["args_json"], {})
        adapter, semantics = _adapter_and_semantics(str(call["name"]), call["effect"])
        state = _reservation_state(reservations)
        operation_id = _operation_id(task_id, tool_use_id)
        dedupe_key = _dedupe_key(task_id, tool_use_id)
        started = [float(row["started_at"]) for row in reservations if row["started_at"] is not None]
        finished = [float(row["finished_at"]) for row in reservations if row["finished_at"] is not None]
        created_at = min(started or [now])
        updated_at = max(finished or started or [created_at])
        dispatched_at = min(started or [created_at])
        completed_at = max(finished or [updated_at]) if state in {"committed", "cancelled"} else None
        result_json = None
        result_digest = None
        if state == "committed" and call["output"] is not None:
            result_json = _json({"output": call["output"]})
            result_digest = _sha256_text(result_json)
        probe = {
            "source": "legacy-v4",
            "reservation_ids": [int(row["reservation_id"]) for row in reservations],
            "reservation_states": [str(row["state"]) for row in reservations],
        }
        request_json = _json({"name": str(call["name"]), "input": args})
        existing = conn.execute(
            "SELECT operation_id, task_id, tool_use_id, args_hash FROM operations WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO operations(
                    operation_id, task_id, tool_use_id, adapter, semantics, effect_scope,
                    dedupe_key, idempotency_key, args_hash, state, request_json, result_json,
                    result_digest, probe_evidence_json, attempt_count, version, created_at,
                    updated_at, dispatched_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    task_id,
                    tool_use_id,
                    adapter,
                    semantics,
                    task_roots[task_id],
                    dedupe_key,
                    str(call["args_hash"]),
                    state,
                    request_json,
                    result_json,
                    result_digest,
                    _json(probe),
                    len(reservations),
                    created_at,
                    updated_at,
                    dispatched_at,
                    completed_at,
                ),
            )
        else:
            if (
                str(existing["task_id"]) != task_id
                or str(existing["tool_use_id"]) != tool_use_id
                or str(existing["args_hash"]) != str(call["args_hash"])
            ):
                raise MigrationValidationError(f"legacy operation collision: {dedupe_key}")
            operation_id = str(existing["operation_id"])

        conn.execute(
            """
            INSERT INTO operation_outbox(
                operation_id, state, available_at, claimed_by, claimed_until,
                delivery_attempts, last_error, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, ?, NULL, ?)
            ON CONFLICT(operation_id) DO UPDATE SET
                state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (operation_id, _outbox_state(state), created_at, len(reservations), updated_at),
        )
        conn.execute(
            "UPDATE tool_calls SET operation_id = ? WHERE task_id = ? AND tool_use_id = ?",
            (operation_id, task_id, tool_use_id),
        )
        conn.execute(
            "UPDATE effect_reservations SET operation_id = ? WHERE task_id = ? AND tool_use_id = ?",
            (operation_id, task_id, tool_use_id),
        )


def _apply_v5_effect_ledger(conn: sqlite3.Connection, *, backfill: bool = True) -> None:
    for column, definition in (
        ("name", "TEXT"),
        ("checksum", "TEXT"),
        ("duration_ms", "REAL"),
        ("backup_filename", "TEXT"),
        ("backup_sha256", "TEXT"),
    ):
        _ensure_column(conn, "schema_migrations", column, definition)
    _ensure_column(conn, "tool_calls", "operation_id", "TEXT")
    _ensure_column(conn, "effect_reservations", "operation_id", "TEXT")
    _execute_all(
        conn,
        (
            """
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                tool_use_id TEXT NOT NULL,
                adapter TEXT NOT NULL,
                semantics TEXT NOT NULL,
                effect_scope TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                idempotency_key TEXT,
                args_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                result_digest TEXT,
                probe_evidence_json TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                dispatched_at REAL,
                completed_at REAL,
                FOREIGN KEY(task_id, tool_use_id)
                    REFERENCES tool_calls(task_id, tool_use_id),
                UNIQUE(task_id, tool_use_id),
                CHECK (semantics IN ('replay_safe', 'idempotent', 'reconcilable', 'opaque')),
                CHECK (state IN ('prepared', 'dispatched', 'committed', 'failed', 'unknown', 'cancelled'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS operation_outbox (
                operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                state TEXT NOT NULL,
                available_at REAL NOT NULL,
                claimed_by TEXT,
                claimed_until REAL,
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at REAL NOT NULL,
                CHECK (state IN ('pending', 'claimed', 'delivered', 'blocked', 'cancelled'))
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_operations_idempotency ON operations(effect_scope, idempotency_key) WHERE idempotency_key IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_operations_task ON operations(task_id, created_at, operation_id)",
            "CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_operation_outbox_state ON operation_outbox(state, available_at)",
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_operation ON tool_calls(operation_id)",
            "CREATE INDEX IF NOT EXISTS idx_effect_reservations_operation ON effect_reservations(operation_id)",
        ),
    )
    if backfill:
        _backfill_v5(conn)


def _apply_v6_durable_context(conn: sqlite3.Connection) -> None:
    for column, definition in (
        ("source_checkpoint_id", "INTEGER"),
        ("projection_json", "TEXT"),
    ):
        _ensure_column(conn, "model_calls", column, definition)
    _execute_all(conn, _V6_ADDITIONS)


def _apply_v7_background_jobs(conn: sqlite3.Connection) -> None:
    _execute_all(conn, _V7_ADDITIONS)


def _apply_v8_tool_registry(conn: sqlite3.Connection) -> None:
    _execute_all(conn, _V8_ADDITIONS)


def _apply_v9_subagents_mailbox(conn: sqlite3.Connection) -> None:
    _execute_all(conn, _V9_ADDITIONS)


def _apply_v10_verified_subtask(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "plan_items", "verifier_bundle_hash", "TEXT")
    _execute_all(conn, _V10_ADDITIONS[1:])


def _apply_v11_frozen_dag(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "plan_items", "max_turns", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "plan_items", "consumed_turns", "INTEGER NOT NULL DEFAULT 0")


def _apply_v12_stale_evidence(conn: sqlite3.Connection) -> None:
    _execute_all(conn, _V12_ADDITIONS)
    rows = conn.execute(
        "SELECT semantic_checkpoint_id, task_id, plan_item_id, evidence_manifest_json "
        "FROM semantic_checkpoints ORDER BY semantic_checkpoint_id"
    ).fetchall()
    now = _now()
    pending_by_item: dict[tuple[str, int], list[tuple[int, str, int, str]]] = {}
    for row in rows:
        semantic_checkpoint_id = int(row[0])
        task_id = str(row[1])
        plan_item_id = int(row[2])
        evidence_manifest_json = str(row[3])
        exists = conn.execute(
            "SELECT 1 FROM semantic_checkpoint_state_events "
            "WHERE semantic_checkpoint_id = ? LIMIT 1",
            (semantic_checkpoint_id,),
        ).fetchone()
        if exists is not None:
            continue
        pending_by_item.setdefault((task_id, plan_item_id), []).append(
            (semantic_checkpoint_id, task_id, plan_item_id, evidence_manifest_json)
        )

    for group in pending_by_item.values():
        newest_checkpoint_id = group[-1][0]
        for semantic_checkpoint_id, task_id, plan_item_id, evidence_manifest_json in group:
            try:
                manifest = _loads(evidence_manifest_json, [])
                if not isinstance(manifest, list):
                    raise ValueError("evidence manifest is not a list")
                manifest = sorted(
                    manifest,
                    key=lambda entry: str(entry.get("path", ""))
                    if isinstance(entry, dict) else "",
                )
                observed_manifest_json = _json(manifest)
            except (TypeError, ValueError):
                observed_manifest_json = "[]"
            conn.execute(
                "INSERT INTO semantic_checkpoint_state_events("
                "task_id, semantic_checkpoint_id, plan_item_id, state, reason, "
                "observed_manifest_json, observation_complete, created_at) "
                "VALUES (?, ?, ?, 'valid', 'migration_backfill', ?, 1, ?)",
                (
                    task_id,
                    semantic_checkpoint_id,
                    plan_item_id,
                    observed_manifest_json,
                    now,
                ),
            )
        for semantic_checkpoint_id, *_ in group[:-1]:
            conn.execute(
                "INSERT INTO semantic_checkpoint_state_events("
                "task_id, semantic_checkpoint_id, plan_item_id, state, reason, "
                "replacement_checkpoint_id, observed_manifest_json, observation_complete, created_at) "
                "SELECT task_id, semantic_checkpoint_id, plan_item_id, 'superseded', "
                "'migration_backfill', ?, observed_manifest_json, observation_complete, ? "
                "FROM semantic_checkpoint_state_events "
                "WHERE semantic_checkpoint_id = ? ORDER BY state_event_id DESC LIMIT 1",
                (newest_checkpoint_id, now, semantic_checkpoint_id),
            )


def _create_latest_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    _execute_all(conn, _BASE_SCHEMA)
    _apply_v6_durable_context(conn)
    _apply_v7_background_jobs(conn)
    _apply_v8_tool_registry(conn)
    _apply_v9_subagents_mailbox(conn)
    _apply_v10_verified_subtask(conn)
    _apply_v11_frozen_dag(conn)
    _apply_v12_stale_evidence(conn)
    row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,)).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO schema_migrations(
                version, name, checksum, applied_at, duration_ms, backup_filename, backup_sha256
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
            """,
            (SCHEMA_VERSION, MIGRATION_NAME, V12_CHECKSUM, _now(), 0.0),
        )


V5_MIGRATION = Migration(5, V5_MIGRATION_NAME, V5_CHECKSUM, _apply_v5_effect_ledger)
V6_MIGRATION = Migration(6, V6_MIGRATION_NAME, V6_CHECKSUM, _apply_v6_durable_context)
V7_MIGRATION = Migration(7, V7_MIGRATION_NAME, V7_CHECKSUM, _apply_v7_background_jobs)
V8_MIGRATION = Migration(8, V8_MIGRATION_NAME, V8_CHECKSUM, _apply_v8_tool_registry)
V9_MIGRATION = Migration(9, V9_MIGRATION_NAME, V9_CHECKSUM, _apply_v9_subagents_mailbox)
V10_MIGRATION = Migration(10, V10_MIGRATION_NAME, V10_CHECKSUM, _apply_v10_verified_subtask)
V11_MIGRATION = Migration(11, V11_MIGRATION_NAME, V11_CHECKSUM, _apply_v11_frozen_dag)
V12_MIGRATION = Migration(12, V12_MIGRATION_NAME, V12_CHECKSUM, _apply_v12_stale_evidence)


class SchemaManager:
    """Auditable, explicit SQLite schema migration manager."""

    def __init__(
        self,
        database: str | Path,
        *,
        repo_root: str | Path | None = None,
        fault_injector: Callable[..., None] | None = None,
    ):
        raw = Path(database)
        database_suffixes = {".db", ".db3", ".sqlite", ".sqlite3"}
        if raw.exists():
            self.database = raw if raw.is_file() else raw / ".agent_runtime" / "runtime.db"
        elif raw.suffix.lower() in database_suffixes or raw.name == "runtime.db":
            self.database = raw
        else:
            self.database = raw / ".agent_runtime" / "runtime.db"
        self.database = self.database.resolve()
        self.repo_root = Path(repo_root).resolve() if repo_root is not None else self.database.parent.parent
        self.fault_injector = fault_injector

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return (
            V5_MIGRATION,
            V6_MIGRATION,
            V7_MIGRATION,
            V8_MIGRATION,
            V9_MIGRATION,
            V10_MIGRATION,
            V11_MIGRATION,
            V12_MIGRATION,
        )

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.database.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            conn = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _read_metadata(self, conn: sqlite3.Connection) -> tuple[int, set[str], bool]:
        tables = _table_names(conn)
        if "schema_migrations" not in tables:
            if _business_tables(tables):
                raise MigrationValidationError(
                    "database contains business tables but has no schema_migrations table"
                )
            return 0, tables, False
        rows = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        if not rows:
            if _business_tables(tables):
                raise MigrationValidationError(
                    "database contains business tables but has no registered schema migration"
                )
            return 0, tables, False
        current = max(int(row["version"]) for row in rows)
        if current > SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"Runtime database schema v{current} is newer than supported v{SCHEMA_VERSION}"
            )
        if current < 4:
            raise MigrationValidationError(
                f"database schema v{current} has no supported migration path to v{SCHEMA_VERSION}"
            )
        known_migrations = {migration.version: migration for migration in self.migrations}
        for row in rows:
            version = int(row["version"])
            migration = known_migrations.get(version)
            if migration is None:
                if version == 4:
                    keys = set(row.keys())
                    if "name" not in keys or "checksum" not in keys:
                        continue
                    if row["name"] is None and row["checksum"] is None:
                        continue
                    if str(row["name"]) == "legacy-v4" and str(row["checksum"]) == "legacy":
                        continue
                raise MigrationValidationError(f"unknown registered schema migration: v{version}")
            if str(row["name"]) != migration.name or str(row["checksum"]) != migration.checksum:
                raise MigrationChecksumMismatch(
                    f"migration checksum mismatch for v{version}: "
                    f"{row['name']!r}/{row['checksum']!r}"
                )
        if current == SCHEMA_VERSION:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()}
            if not {"name", "checksum"} <= columns:
                raise MigrationValidationError("schema_migrations is missing migration audit columns")
            latest = next(row for row in rows if int(row["version"]) == SCHEMA_VERSION)
            if str(latest["name"]) != MIGRATION_NAME or str(latest["checksum"]) != V12_CHECKSUM:
                raise MigrationChecksumMismatch(
                    f"migration checksum mismatch for v{SCHEMA_VERSION}: "
                    f"{latest['name']!r}/{latest['checksum']!r}"
                )
            required = {
                "tasks", "checkpoints", "model_calls", "tool_calls", "file_observations",
                "events", "leases", "operations", "operation_outbox", "effect_reservations",
                "memories", "memory_links", "summaries", "plans", "plan_items",
                "agent_jobs", "job_runs", "cron_schedules",
                "tool_registrations", "mcp_connections",
                "subagent_runs", "mailboxes", "mailbox_messages", "plan_approvals",
                "verifier_runs", "semantic_checkpoints",
                "semantic_checkpoint_state_events",
            }
            missing = sorted(required - tables)
            if missing:
                raise MigrationValidationError(
                    f"v{SCHEMA_VERSION} database is missing required tables: {', '.join(missing)}"
                )
            required_columns = {
                "schema_migrations": {"version", "name", "checksum", "applied_at", "duration_ms", "backup_filename", "backup_sha256"},
                "tool_calls": {"operation_id"},
                "effect_reservations": {"operation_id"},
                "model_calls": {"source_checkpoint_id", "projection_json"},
                "operations": {
                    "operation_id", "task_id", "tool_use_id", "adapter", "semantics", "effect_scope",
                    "dedupe_key", "idempotency_key", "args_hash", "state", "request_json", "result_json",
                    "result_digest", "probe_evidence_json", "attempt_count", "version", "created_at",
                    "updated_at", "dispatched_at", "completed_at",
                },
                "operation_outbox": {
                    "operation_id", "state", "available_at", "claimed_by", "claimed_until",
                    "delivery_attempts", "last_error", "updated_at",
                },
                "memories": {
                    "memory_id", "task_id", "kind", "content", "source_checkpoint_id",
                    "evidence_hash", "created_at", "expires_at",
                },
                "memory_links": {"source_memory_id", "target_memory_id", "relation", "created_at"},
                "summaries": {
                    "summary_id", "task_id", "scope", "content", "source_turn_range",
                    "evidence_hash", "model_call_id", "created_at",
                },
                "plans": {
                    "plan_id", "task_id", "status", "dag_hash", "created_at", "updated_at",
                },
                "plan_items": {
                    "plan_item_id", "plan_id", "subtask_id", "description", "status",
                    "blocked_by_json", "completion_summary", "evidence_hash", "version",
                    "created_at", "updated_at", "verifier_bundle_hash", "max_turns",
                    "consumed_turns",
                },
                "agent_jobs": {
                    "job_id", "task_id", "repo_root", "lane_id", "kind", "payload_json",
                    "status", "attempts", "max_attempts", "available_at", "created_at", "updated_at",
                },
                "job_runs": {
                    "run_id", "job_id", "owner_id", "fencing_token", "status", "heartbeat_at",
                    "lease_expires_at", "started_at", "completed_at", "error", "result_digest",
                },
                "cron_schedules": {
                    "schedule_id", "task_id", "expression", "job_kind", "payload_json",
                    "enabled", "last_triggered_at", "next_trigger_at",
                },
                "tool_registrations": {
                    "registration_id", "tool_name", "adapter_kind", "description", "schema_json",
                    "effect_kind", "permission_json", "timeout_seconds", "enabled", "version",
                    "created_at", "updated_at",
                },
                "mcp_connections": {
                    "connection_id", "server_name", "transport", "endpoint", "args_json",
                    "auth_profile_json", "status", "last_connected_at", "last_error",
                    "created_at", "updated_at",
                },
                "subagent_runs": {
                    "subagent_run_id", "parent_task_id", "child_task_id", "repo_root",
                    "lane_id", "role", "status", "owner_id", "fencing_token", "version",
                    "messages_json", "result_summary", "error", "created_at", "updated_at",
                },
                "mailboxes": {
                    "mailbox_id", "owner_task_id", "owner_role", "version",
                    "created_at", "updated_at",
                },
                "mailbox_messages": {
                    "message_id", "mailbox_id", "sender_task_id", "recipient_task_id",
                    "payload_json", "status", "created_at", "read_at",
                },
                "plan_approvals": {
                    "approval_id", "subagent_run_id", "plan_hash", "status",
                    "requested_by", "decided_by", "reason", "version",
                    "created_at", "updated_at",
                },
                "verifier_runs": {
                    "verifier_run_id", "task_id", "plan_item_id", "subtask_id", "status",
                "summary", "completion_summary", "verifier_id", "verifier_version",
                    "verification_rule", "verifier_bundle_hash", "verifier_implementation_hash",
                    "evidence_manifest_json",
                    "evidence_hash", "authoritative", "execution_checkpoint_id", "created_at",
                },
                "semantic_checkpoints": {
                    "semantic_checkpoint_id", "task_id", "plan_item_id", "subtask_id",
                    "verifier_run_id", "execution_checkpoint_id", "completion_summary",
                    "verifier_id", "verifier_version", "verification_rule",
                    "verifier_bundle_hash", "verifier_implementation_hash", "evidence_manifest_json",
                    "evidence_hash",
                    "created_at",
                },
                "semantic_checkpoint_state_events": {
                    "state_event_id", "task_id", "semantic_checkpoint_id", "plan_item_id",
                    "state", "reason", "replacement_checkpoint_id", "observed_manifest_json",
                    "observation_complete", "created_at",
                },
            }
            for table, columns in required_columns.items():
                actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                missing_columns = sorted(columns - actual)
                if missing_columns:
                    raise MigrationValidationError(
                        f"v{SCHEMA_VERSION} table {table} is missing columns: {', '.join(missing_columns)}"
                    )
        elif current == V5_MIGRATION.version:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()}
            if not {"name", "checksum"} <= columns:
                raise MigrationValidationError("v5 schema_migrations is missing migration audit columns")
            latest = next((row for row in rows if int(row["version"]) == current), None)
            if latest is None or str(latest["name"]) != V5_MIGRATION_NAME or str(latest["checksum"]) != V5_CHECKSUM:
                raise MigrationChecksumMismatch(
                    f"migration checksum mismatch for v5: "
                    f"{latest['name'] if latest else None!r}/"
                    f"{latest['checksum'] if latest else None!r}"
                )
        return current, tables, bool(_business_tables(tables))

    def inspect(self) -> SchemaStatus:
        if not self.database.exists():
            return SchemaStatus(0, SCHEMA_VERSION, self.migrations, False, False)
        try:
            conn = self._connect(read_only=True)
        except sqlite3.DatabaseError as exc:
            raise SchemaError(f"cannot open Runtime database: {self.database}") from exc
        try:
            try:
                current, tables, has_business = self._read_metadata(conn)
                integrity = tuple(_integrity(conn))
            except (sqlite3.DatabaseError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise SchemaError(f"cannot inspect Runtime database: {self.database}") from exc
            active_leases = 0
            if "leases" in tables:
                active_leases = int(
                    conn.execute("SELECT COUNT(*) FROM leases WHERE expires_at > ?", (_now(),)).fetchone()[0]
                )
            pending = tuple(migration for migration in self.migrations if migration.version > current)
            return SchemaStatus(
                current,
                SCHEMA_VERSION,
                pending,
                True,
                has_business,
                integrity,
                active_leases,
            )
        finally:
            conn.close()

    def plan(self) -> list[Migration]:
        return list(self.inspect().pending_migrations)

    def _expected_backup_filename(self, from_version: int) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return f"{self.database.stem}.v{from_version}.backup.{stamp}.{os.getpid()}.db"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _backup_connection(self, source: sqlite3.Connection, filename: str) -> BackupResult:
        destination = self.database.parent / filename
        if destination.exists():
            suffix = 1
            while True:
                candidate = self.database.parent / f"{destination.stem}.{suffix}{destination.suffix}"
                if not candidate.exists():
                    destination = candidate
                    break
                suffix += 1
        target = sqlite3.connect(destination, timeout=30)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
        check_conn = sqlite3.connect(destination, timeout=30)
        try:
            check = _integrity(check_conn)
        finally:
            check_conn.close()
        if check:
            raise SchemaError(f"migration backup integrity check failed: {check}")
        return BackupResult(destination.name, str(destination), self._sha256_file(destination), "ok")

    def backup(self) -> BackupResult:
        if not self.database.exists():
            raise SchemaError(f"Runtime database does not exist: {self.database}")
        status = self.inspect()
        conn = self._connect()
        try:
            return self._backup_connection(conn, self._expected_backup_filename(status.current_version))
        finally:
            conn.close()

    def _preflight(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        integrity = _integrity(conn)
        checks.append({
            "name": "integrity_check",
            "status": "pass" if not integrity else "fail",
            "details": "ok" if not integrity else integrity,
        })
        tables = _table_names(conn)
        active = conn.execute(
            "SELECT repo_root, owner_id, task_id, expires_at FROM leases WHERE expires_at > ?",
            (_now(),),
        ).fetchall() if "leases" in tables else []
        checks.append({
            "name": "active_lease",
            "status": "pass" if not active else "fail",
            "details": [] if not active else [
                {"repo_root": row["repo_root"], "task_id": row["task_id"], "owner_id": row["owner_id"]}
                for row in active
            ],
        })
        return checks

    def _inject(self, point: str, **context: Any) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point, **context)

    def ensure_latest(self) -> None:
        """Create a fresh database directly at the latest schema; never upgrade an old database."""
        if self.database.exists():
            try:
                status = self.inspect()
            except sqlite3.DatabaseError as exc:
                raise SchemaError(f"cannot inspect Runtime database: {self.database}") from exc
            if status.current_version == SCHEMA_VERSION and not status.integrity_check:
                return
            if status.current_version == SCHEMA_VERSION:
                raise SchemaError(
                    f"Runtime database integrity check failed: {list(status.integrity_check)}"
                )
            if status.current_version == 0 and not status.has_business_tables:
                pass
            else:
                raise SchemaUpgradeRequired(self.database, status.current_version)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("BEGIN EXCLUSIVE")
            current, _, has_business = self._read_metadata(conn)
            if current == SCHEMA_VERSION:
                conn.commit()
                return
            if current != 0 or has_business:
                raise SchemaUpgradeRequired(self.database, current)
            _create_latest_schema(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_fresh(self, dry_run: bool, preflight: tuple[dict[str, Any], ...]) -> MigrationReport:
        if dry_run:
            return MigrationReport(
                True,
                0,
                SCHEMA_VERSION,
                (MIGRATION_NAME,),
                None,
                "not-run",
                True,
                preflight,
            )
        self.ensure_latest()
        return MigrationReport(True, 0, SCHEMA_VERSION, (MIGRATION_NAME,), None, "ok", False, preflight)

    def migrate(self, dry_run: bool = False) -> MigrationReport:
        status = self.inspect()
        preflight = (
            {
                "name": "database",
                "status": "pass",
                "details": status.as_dict(),
            },
            {
                "name": "integrity_check",
                "status": "pass" if not status.integrity_check else "fail",
                "details": "ok" if not status.integrity_check else list(status.integrity_check),
            },
            {
                "name": "active_lease",
                "status": "pass" if status.active_leases == 0 else "fail",
                "details": status.active_leases,
            },
        )
        if status.current_version == SCHEMA_VERSION and not status.pending_migrations:
            return MigrationReport(
                not status.integrity_check,
                SCHEMA_VERSION,
                SCHEMA_VERSION,
                (),
                None,
                "ok" if not status.integrity_check else "fail",
                dry_run,
                preflight,
            )
        if status.current_version == 0 and not status.has_business_tables:
            return self._migrate_fresh(dry_run, preflight)
        pending = status.pending_migrations
        expected_versions = list(range(status.current_version + 1, SCHEMA_VERSION + 1))
        if [m.version for m in pending] != expected_versions:
            raise SchemaUpgradeRequired(self.database, status.current_version)
        pending_names = tuple(m.name for m in pending)
        if dry_run:
            checks = list(preflight)
            checks.append({
                "name": "preflight",
                "status": "pass" if not status.integrity_check and status.active_leases == 0 else "fail",
                "details": {
                    "integrity_check": list(status.integrity_check),
                    "active_leases": status.active_leases,
                    "backup_filename": self._expected_backup_filename(status.current_version),
                },
            })
            return MigrationReport(
                not status.integrity_check and status.active_leases == 0,
                status.current_version,
                SCHEMA_VERSION,
                pending_names,
                None,
                "not-run",
                True,
                tuple(checks),
            )
        if status.integrity_check:
            raise MigrationBlocked(f"Migration integrity preflight failed: {list(status.integrity_check)}")
        if status.active_leases:
            raise MigrationBlocked(f"Migration is blocked by {status.active_leases} active lease(s)")

        backup: BackupResult | None = None
        committed = False
        started = time.perf_counter()
        # SQLite's backup API cannot run from a connection that already owns
        # an EXCLUSIVE/IMMEDIATE write transaction on Windows. Take the
        # immutable pre-migration backup first, then acquire the migration
        # lock and repeat every safety check before changing the database.
        backup_conn = self._connect()
        try:
            backup = self._backup_connection(backup_conn, self._expected_backup_filename(status.current_version))
        finally:
            backup_conn.close()
        self._inject("after_migration_backup", backup=backup.as_dict())

        conn = self._connect()
        try:
            conn.execute("BEGIN EXCLUSIVE")
            current, _, has_business = self._read_metadata(conn)
            if current == SCHEMA_VERSION:
                conn.commit()
                committed = True
                return MigrationReport(True, SCHEMA_VERSION, SCHEMA_VERSION, (), None, "ok", False, preflight)
            pending_versions = [m.version for m in self.migrations if m.version > current]
            if (
                current != status.current_version
                or pending_versions != expected_versions
                or not has_business
            ):
                raise SchemaUpgradeRequired(self.database, current)
            checks = self._preflight(conn)
            if any(item["status"] == "fail" for item in checks):
                raise MigrationBlocked(f"Migration preflight failed: {checks}")

            if status.current_version == 4:
                # Validate the complete legacy projection before changing any
                # v4 rows or applying v5 DDL. The backfill repeats this shared
                # check after the stale-running conversion.
                _validate_legacy_rows(conn)
                now = _now()
                conn.execute(
                    "UPDATE effect_reservations SET state = 'unknown', finished_at = ?, details_json = ? "
                    "WHERE state = 'running'",
                    (now, _json({"reason": "migration_stale_running"})),
                )
                _apply_v5_effect_ledger(conn, backfill=False)
                self._inject("after_migration_ddl", from_version=4, to_version=5)
                _backfill_v5(conn)
                self._inject("after_migration_backfill", from_version=4, to_version=5)
                conn.execute(
                    "UPDATE schema_migrations SET name = ?, checksum = ? WHERE version = 4",
                    ("legacy-v4", "legacy"),
                )
            if 6 in expected_versions:
                _apply_v6_durable_context(conn)
            if 7 in expected_versions:
                _apply_v7_background_jobs(conn)
            if 8 in expected_versions:
                _apply_v8_tool_registry(conn)
            if 9 in expected_versions:
                _apply_v9_subagents_mailbox(conn)
            if 10 in expected_versions:
                _apply_v10_verified_subtask(conn)
            if 11 in expected_versions:
                _apply_v11_frozen_dag(conn)
            if 12 in expected_versions:
                _apply_v12_stale_evidence(conn)

            duration_ms = (time.perf_counter() - started) * 1000.0
            for migration in pending:
                conn.execute(
                    """
                    INSERT INTO schema_migrations(
                        version, name, checksum, applied_at, duration_ms,
                        backup_filename, backup_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        _now(),
                        duration_ms,
                        backup.filename if backup else None,
                        backup.sha256 if backup else None,
                    ),
                )
            self._inject(
                "before_migration_commit",
                from_version=status.current_version,
                to_version=SCHEMA_VERSION,
            )
            conn.commit()
            committed = True
            self._inject(
                "after_migration_commit",
                from_version=status.current_version,
                to_version=SCHEMA_VERSION,
            )
        except Exception:
            if not committed:
                conn.rollback()
            raise
        finally:
            conn.close()

        final_integrity = "ok"
        check_conn = self._connect(read_only=True)
        try:
            errors = _integrity(check_conn)
            if errors:
                final_integrity = "; ".join(errors)
        finally:
            check_conn.close()
        if final_integrity != "ok":
            raise SchemaError(f"post-migration integrity check failed: {final_integrity}")
        return MigrationReport(
            True,
            status.current_version,
            SCHEMA_VERSION,
            pending_names,
            backup,
            final_integrity,
            False,
            preflight,
        )


__all__ = [
    "BackupResult",
    "MIGRATION_NAME",
    "Migration",
    "MigrationChecksumMismatch",
    "MigrationBlocked",
    "MigrationReport",
    "MigrationValidationError",
    "SchemaError",
    "SchemaManager",
    "SchemaStatus",
    "SchemaUpgradeRequired",
    "SCHEMA_VERSION",
    "UnsupportedSchemaVersion",
    "V5_CHECKSUM",
    "V6_CHECKSUM",
    "V7_CHECKSUM",
    "V8_CHECKSUM",
    "V9_CHECKSUM",
    "V10_CHECKSUM",
    "V11_CHECKSUM",
    "V12_CHECKSUM",
]
