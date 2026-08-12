from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 4
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVENT_PAYLOAD_BYTES = 1 * 1024 * 1024


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checked_json(value: Any, limit: int, label: str) -> str:
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return encoded


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class LeaseLost(RuntimeError):
    """Raised when a fenced Runtime no longer owns its repository lease."""


class StaleState(RuntimeError):
    """Raised when a compare-and-swap update observes a newer state."""


class InvariantViolation(RuntimeError):
    """Raised when durable state is inconsistent; callers must stop, not repair silently."""


class EffectBlocked(RuntimeError):
    """Raised when an unresolved repository effect blocks a new side effect."""

    def __init__(self, repo_root: str, reservations: list[dict[str, Any]]):
        self.repo_root = repo_root
        self.reservations = reservations
        summary = ", ".join(
            f"{item.get('reservation_id')}:{item.get('state')}:{item.get('task_id')}"
            for item in reservations
        )
        super().__init__(f"Repository has unresolved effect reservations: {summary or repo_root}")


class EventStore:
    """SQLite state store and append-only event log."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        if str(self.path).startswith(("\\\\", "//")):
            raise ValueError("SQLite runtime store must be on a local filesystem")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lease_context: tuple[str, str, int] | None = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def transaction(self, guard: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if guard:
                self._assert_lease_conn(conn)
            yield conn
            if guard:
                self._assert_lease_conn(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.transaction() as conn:
            migration_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if migration_table is not None:
                current = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"]
                if current is not None and int(current) > SCHEMA_VERSION:
                    raise RuntimeError(f"Unsupported runtime schema version: {current}")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                );
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
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    phase TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    cursor_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
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
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    tool_call_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    tool_use_id TEXT NOT NULL,
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
                    UNIQUE(task_id, tool_use_id)
                );
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
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    repo_root TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    owner_id TEXT NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    fencing_token INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS effect_reservations (
                    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    tool_use_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    effect TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    deadline_at REAL,
                    finished_at REAL,
                    details_json TEXT,
                    CHECK (state IN ('running', 'completed', 'unknown', 'cancelled'))
                );
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, checkpoint_id);
                CREATE INDEX IF NOT EXISTS idx_model_calls_task ON model_calls(task_id, model_call_id);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(task_id, tool_call_row_id);
                CREATE INDEX IF NOT EXISTS idx_effect_reservations_task_tool
                    ON effect_reservations(task_id, tool_use_id, reservation_id);
                CREATE INDEX IF NOT EXISTS idx_effect_reservations_state
                    ON effect_reservations(state, task_id);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_effect_reservation_running
                    ON effect_reservations(task_id, tool_use_id) WHERE state = 'running';
                """
            )
            def ensure_column(table: str, column: str, definition: str) -> None:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

            ensure_column("tasks", "version", "INTEGER NOT NULL DEFAULT 0")
            lease_columns = {row["name"] for row in conn.execute("PRAGMA table_info(leases)").fetchall()}
            if "fencing_token" not in lease_columns:
                conn.execute("ALTER TABLE leases ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 0")
            for column, definition in (
                ("returncode", "INTEGER"),
                ("stdout", "TEXT"),
                ("stderr", "TEXT"),
                ("timed_out", "INTEGER NOT NULL DEFAULT 0"),
                ("execution_status", "TEXT"),
                ("execution_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("effect_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("effect_confirmed", "INTEGER NOT NULL DEFAULT 0"),
                ("effect_confirmation", "TEXT"),
                ("effect_key", "TEXT"),
                ("version", "INTEGER NOT NULL DEFAULT 0"),
            ):
                ensure_column("tool_calls", column, definition)
            ensure_column("file_observations", "identity_json", "TEXT")
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now()),
            )

    def _assert_lease_conn(self, conn: sqlite3.Connection) -> None:
        if self._lease_context is None:
            return
        repo_root, owner_id, fencing_token = self._lease_context
        row = conn.execute(
            "SELECT owner_id, fencing_token, expires_at FROM leases WHERE repo_root = ?",
            (repo_root,),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != owner_id
            or int(row["fencing_token"]) != int(fencing_token)
            or float(row["expires_at"]) <= _now()
        ):
            raise LeaseLost(f"Lease lost for {repo_root} (owner={owner_id}, fencing_token={fencing_token})")

    def bind_lease(self, repo_root: str, owner_id: str, fencing_token: int) -> None:
        self._lease_context = (repo_root, owner_id, int(fencing_token))

    def clear_lease(self) -> None:
        self._lease_context = None

    def assert_lease(self) -> None:
        """Fail closed if the bound fencing token is no longer current."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            self._assert_lease_conn(conn)
        finally:
            conn.rollback()
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        conn = self._connect()
        try:
            return conn.execute(query, params).fetchone()
        finally:
            conn.close()

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def integrity_check(self) -> list[str]:
        """Return SQLite integrity errors; an empty list means the database is healthy."""
        conn = self._connect()
        try:
            results = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        finally:
            conn.close()
        return [] if results == ["ok"] else results

    def create_task(self, task_id: str, repo_root: str, prompt: str, model: str) -> None:
        if len(prompt.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            raise ValueError(f"task prompt exceeds {MAX_CHECKPOINT_BYTES} bytes")
        now = _now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id, repo_root, prompt, model, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'created', ?, ?)",
                (task_id, repo_root, prompt, model, now, now),
            )

    def bootstrap_task(
        self,
        task_id: str,
        repo_root: str,
        prompt: str,
        model: str,
        messages: list[dict],
        cursor: dict[str, Any],
        fault_injector: Callable[..., None] | None = None,
    ) -> int:
        """Atomically create a task and its first recoverable checkpoint."""
        if len(prompt.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            raise ValueError(f"task prompt exceeds {MAX_CHECKPOINT_BYTES} bytes")
        now = _now()
        messages_json = _checked_json(messages, MAX_CHECKPOINT_BYTES, "checkpoint messages")
        cursor_json = _checked_json(cursor, MAX_CHECKPOINT_BYTES, "checkpoint cursor")
        with self.transaction(guard=False) as conn:
            conn.execute(
                "INSERT INTO tasks(task_id, repo_root, prompt, model, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'created', ?, ?)",
                (task_id, repo_root, prompt, model, now, now),
            )
            if fault_injector:
                fault_injector("bootstrap_after_task_insert", task_id=task_id)

            checkpoint = conn.execute(
                "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) "
                "VALUES (?, 'input_ready', ?, ?, ?)",
                (task_id, messages_json, cursor_json, now),
            )
            checkpoint_id = int(checkpoint.lastrowid)
            if fault_injector:
                fault_injector("bootstrap_after_checkpoint_insert", task_id=task_id, checkpoint_id=checkpoint_id)

            conn.execute(
                "UPDATE tasks SET checkpoint_id = ?, updated_at = ?, version = version + 1 WHERE task_id = ?",
                (checkpoint_id, now, task_id),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_created', ?, ?)",
                (task_id, _checked_json({"prompt": prompt}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
            if fault_injector:
                fault_injector("bootstrap_after_task_event", task_id=task_id, checkpoint_id=checkpoint_id)
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'checkpoint_saved', ?, ?)",
                (task_id, _checked_json({"checkpoint_id": checkpoint_id, "phase": "input_ready"}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
        return checkpoint_id

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self._fetchone("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        return dict(row)

    def list_tasks(self) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM tasks ORDER BY created_at, task_id")
        return [dict(row) for row in rows]

    def update_task(self, task_id: str, status: str | None = None, checkpoint_id: int | None = None,
                    last_error: str | None = None, clear_error: bool = False,
                    expected_version: int | None = None) -> None:
        fields: list[str] = ["updated_at = ?", "version = version + 1"]
        values: list[Any] = [_now()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if checkpoint_id is not None:
            fields.append("checkpoint_id = ?")
            values.append(checkpoint_id)
        if clear_error:
            fields.append("last_error = NULL")
        elif last_error is not None:
            fields.append("last_error = ?")
            values.append(last_error)
        values.append(task_id)
        where = "task_id = ?"
        if expected_version is not None:
            where += " AND version = ?"
            values.append(int(expected_version))
        with self.transaction() as conn:
            cursor = conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE {where}", values)
            if expected_version is not None and cursor.rowcount != 1:
                raise StaleState(f"Task state changed before update: {task_id}")

    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> int:
        payload_json = _checked_json(payload or {}, MAX_EVENT_PAYLOAD_BYTES, "event payload")
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, event_type, payload_json, _now()),
            )
            return int(cursor.lastrowid)

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM events WHERE task_id = ? ORDER BY event_id", (task_id,))
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json"), {})
            result.append(item)
        return result

    def save_checkpoint(self, task_id: str, phase: str, messages: list[dict], cursor: dict[str, Any],
                        event_type: str = "checkpoint_saved") -> int:
        now = _now()
        messages_json = _checked_json(messages, MAX_CHECKPOINT_BYTES, "checkpoint messages")
        cursor_json = _checked_json(cursor, MAX_CHECKPOINT_BYTES, "checkpoint cursor")
        with self.transaction() as conn:
            checkpoint = conn.execute(
                "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, phase, messages_json, cursor_json, now),
            )
            checkpoint_id = int(checkpoint.lastrowid)
            conn.execute(
                "UPDATE tasks SET checkpoint_id = ?, updated_at = ?, version = version + 1 WHERE task_id = ?",
                (checkpoint_id, now, task_id),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, event_type, _checked_json({"checkpoint_id": checkpoint_id, "phase": phase}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
        return checkpoint_id

    def transition_review(
        self,
        task_id: str,
        tool_use_id: str,
        phase: str,
        messages: list[dict],
        cursor: dict[str, Any],
        reason: str,
        unknown_effect: dict[str, Any] | None = None,
        fault_injector: Callable[..., None] | None = None,
    ) -> int:
        """Atomically project a waiting/review transition and its checkpoint."""
        if phase not in {"waiting_approval", "needs_review"}:
            raise ValueError(f"Unsupported review phase: {phase}")
        now = _now()
        messages_json = _checked_json(messages, MAX_CHECKPOINT_BYTES, "checkpoint messages")
        cursor_json = _checked_json(cursor, MAX_CHECKPOINT_BYTES, "checkpoint cursor")
        metadata = dict(unknown_effect or {})
        metadata["reason"] = reason
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT status, version FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            call = conn.execute(
                "SELECT status, version FROM tool_calls WHERE task_id = ? AND tool_use_id = ?",
                (task_id, tool_use_id),
            ).fetchone()
            if task is None or call is None:
                raise KeyError(f"Review item not found: {tool_use_id}")
            if task["status"] in {"completed", "failed", "aborted"}:
                raise StaleState(f"Task is terminal during review transition: {task_id}")
            if call["status"] in {"succeeded", "denied", "failed", "aborted"}:
                raise StaleState(f"Tool call is terminal during review transition: {tool_use_id}")
            checkpoint = conn.execute(
                "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, phase, messages_json, cursor_json, now),
            )
            checkpoint_id = int(checkpoint.lastrowid)
            if fault_injector:
                fault_injector("review_after_checkpoint", task_id=task_id, checkpoint_id=checkpoint_id,
                               tool_use_id=tool_use_id, phase=phase)
            if phase == "needs_review":
                call_updated = conn.execute(
                    "UPDATE tool_calls SET status = 'needs_review', error = ?, finished_at = ?, "
                    "execution_status = CASE WHEN execution_status = 'running' THEN 'unknown' ELSE execution_status END, "
                    "version = version + 1 WHERE task_id = ? AND tool_use_id = ?",
                    (reason, now, task_id, tool_use_id),
                )
            else:
                call_updated = conn.execute(
                    "UPDATE tool_calls SET status = 'waiting_approval', permission = 'ask', "
                    "permission_reason = ?, version = version + 1 WHERE task_id = ? AND tool_use_id = ?",
                    (reason, task_id, tool_use_id),
                )
            if call_updated.rowcount != 1:
                raise StaleState(f"Tool call changed during review transition: {tool_use_id}")
            if phase == "needs_review":
                conn.execute(
                    "UPDATE effect_reservations SET state = 'unknown', finished_at = ?, details_json = ? "
                    "WHERE task_id = ? AND tool_use_id = ? AND state = 'running'",
                    (
                        now,
                        _json({"reason": "review_requires_reconciliation", "tool_use_id": tool_use_id}),
                        task_id,
                        tool_use_id,
                    ),
                )
            task_updated = conn.execute(
                "UPDATE tasks SET status = ?, checkpoint_id = ?, last_error = ?, updated_at = ?, "
                "version = version + 1 WHERE task_id = ?",
                (phase, checkpoint_id, reason, now, task_id),
            )
            if task_updated.rowcount != 1:
                raise StaleState(f"Task changed during review transition: {task_id}")
            if fault_injector:
                fault_injector("review_after_status", task_id=task_id, checkpoint_id=checkpoint_id,
                               tool_use_id=tool_use_id, phase=phase)
            event_type = "task_needs_review" if phase == "needs_review" else "task_waiting_approval"
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, event_type, _checked_json({
                    "tool_use_id": tool_use_id,
                    "checkpoint_id": checkpoint_id,
                    **metadata,
                }, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
            if phase == "needs_review":
                conn.execute(
                    "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'tool_needs_review', ?, ?)",
                    (task_id, _checked_json({
                        "tool_use_id": tool_use_id,
                        "checkpoint_id": checkpoint_id,
                        **metadata,
                    }, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
                )
            if fault_injector:
                fault_injector("review_after_event", task_id=task_id, checkpoint_id=checkpoint_id,
                               tool_use_id=tool_use_id, phase=phase)
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'checkpoint_saved', ?, ?)",
                (task_id, _checked_json({"checkpoint_id": checkpoint_id, "phase": phase}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
        return checkpoint_id

    def complete_task(
        self,
        task_id: str,
        messages: list[dict],
        cursor: dict[str, Any],
        final_text: str,
        fault_injector: Callable[..., None] | None = None,
        expected_task_version: int | None = None,
    ) -> int:
        """Atomically persist the terminal checkpoint, status, and completion event."""
        now = _now()
        messages_json = _checked_json(messages, MAX_CHECKPOINT_BYTES, "checkpoint messages")
        cursor_json = _checked_json(cursor, MAX_CHECKPOINT_BYTES, "checkpoint cursor")
        with self.transaction() as conn:
            task = conn.execute("SELECT status, version, checkpoint_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] in {"completed", "failed", "aborted"}:
                raise RuntimeError(f"Task {task_id} is terminal: {task['status']}")
            if expected_task_version is not None and int(task["version"]) != int(expected_task_version):
                raise StaleState(f"Task state changed before completion: {task_id}")
            checkpoint = conn.execute(
                "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) "
                "VALUES (?, 'completed', ?, ?, ?)",
                (task_id, messages_json, cursor_json, now),
            )
            checkpoint_id = int(checkpoint.lastrowid)
            if fault_injector:
                fault_injector("completion_after_checkpoint", task_id=task_id, checkpoint_id=checkpoint_id)
            where = "task_id = ?"
            params: list[Any] = [checkpoint_id, now, task_id]
            if expected_task_version is not None:
                where += " AND version = ?"
                params.append(int(expected_task_version))
            updated = conn.execute(
                f"UPDATE tasks SET status = 'completed', checkpoint_id = ?, updated_at = ?, "
                f"last_error = NULL, version = version + 1 WHERE {where}",
                params,
            )
            if updated.rowcount != 1:
                raise StaleState(f"Task state changed before completion: {task_id}")
            if fault_injector:
                fault_injector("completion_after_status", task_id=task_id, checkpoint_id=checkpoint_id)
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_completed', ?, ?)",
                (task_id, _checked_json({"final_text": final_text}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
            if fault_injector:
                fault_injector("completion_after_event", task_id=task_id, checkpoint_id=checkpoint_id)
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'checkpoint_saved', ?, ?)",
                (task_id, _checked_json({"checkpoint_id": checkpoint_id, "phase": "completed"}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
        return checkpoint_id

    def fail_task(self, task_id: str, messages: list[dict], cursor: dict[str, Any], error: str) -> int:
        """Atomically persist a failed terminal checkpoint and state transition."""
        now = _now()
        messages_json = _checked_json(messages, MAX_CHECKPOINT_BYTES, "checkpoint messages")
        cursor_json = _checked_json(cursor, MAX_CHECKPOINT_BYTES, "checkpoint cursor")
        with self.transaction() as conn:
            task = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] in {"completed", "failed", "aborted"}:
                return int(conn.execute("SELECT checkpoint_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()["checkpoint_id"])
            conn.execute(
                "UPDATE tool_calls SET status = 'failed', error = ?, finished_at = ?, version = version + 1 "
                "WHERE task_id = ? AND status = 'running'",
                (error, now, task_id),
            )
            checkpoint = conn.execute(
                "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) VALUES (?, 'failed', ?, ?, ?)",
                (task_id, messages_json, cursor_json, now),
            )
            checkpoint_id = int(checkpoint.lastrowid)
            conn.execute(
                "UPDATE tasks SET status = 'failed', checkpoint_id = ?, last_error = ?, updated_at = ?, version = version + 1 WHERE task_id = ?",
                (checkpoint_id, error, now, task_id),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_failed', ?, ?)",
                (task_id, _checked_json({"error": error}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'checkpoint_saved', ?, ?)",
                (task_id, _checked_json({"checkpoint_id": checkpoint_id, "phase": "failed"}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
        return checkpoint_id

    def recover_model_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        """Materialize a successful model response that was durable before its checkpoint."""
        with self.transaction() as conn:
            task = conn.execute("SELECT checkpoint_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None or task["checkpoint_id"] is None:
                return None
            current = conn.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                (task["checkpoint_id"], task_id),
            ).fetchone()
            if current is None:
                raise RuntimeError(f"Task {task_id} points to a missing checkpoint")
            latest = conn.execute(
                "SELECT * FROM model_calls WHERE task_id = ? AND status = 'succeeded' "
                "AND finished_at IS NOT NULL AND finished_at >= ? ORDER BY model_call_id DESC LIMIT 1",
                (task_id, current["created_at"]),
            ).fetchone()
            if latest is None or not latest["response_json"]:
                return None
            response = _loads(latest["response_json"], {})
            content = list(response.get("content") or [])
            messages = _loads(current["messages_json"], [])
            if messages and messages[-1].get("role") == "assistant":
                return None
            messages.append({"role": "assistant", "content": content})
            cursor = _loads(current["cursor_json"], {})
            cursor["turn"] = int(latest["turn"])
            messages_json = _checked_json(messages, MAX_CHECKPOINT_BYTES, "checkpoint messages")
            cursor_json = _checked_json(cursor, MAX_CHECKPOINT_BYTES, "checkpoint cursor")
            now = _now()
            checkpoint = conn.execute(
                "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) "
                "VALUES (?, 'model_responded', ?, ?, ?)",
                (task_id, messages_json, cursor_json, now),
            )
            checkpoint_id = int(checkpoint.lastrowid)
            conn.execute(
                "UPDATE tasks SET checkpoint_id = ?, updated_at = ?, version = version + 1 WHERE task_id = ?",
                (checkpoint_id, now, task_id),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'model_checkpoint_recovered', ?, ?)",
                (task_id, _json({"model_call_id": latest["model_call_id"], "checkpoint_id": checkpoint_id}), now),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'checkpoint_saved', ?, ?)",
                (task_id, _json({"checkpoint_id": checkpoint_id, "phase": "model_responded"}), now),
            )
            return {"checkpoint_id": checkpoint_id, "messages": messages, "cursor": cursor, "response": response}

    def resolve_review(
        self,
        task_id: str,
        tool_use_id: str,
        action: str,
        expected_tool_version: int,
        expected_task_version: int,
    ) -> None:
        """Apply an approval/review decision atomically with CAS protection."""
        if action not in {"approve", "deny", "retry", "complete"}:
            raise ValueError(f"Unsupported review action: {action}")
        now = _now()
        with self.transaction() as conn:
            task = conn.execute("SELECT status, version, checkpoint_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            call = conn.execute(
                "SELECT status, version FROM tool_calls WHERE task_id = ? AND tool_use_id = ?",
                (task_id, tool_use_id),
            ).fetchone()
            if task is None or call is None:
                raise KeyError(f"Review item not found: {tool_use_id}")
            reservation = conn.execute(
                "SELECT reservation_id, state FROM effect_reservations "
                "WHERE task_id = ? AND tool_use_id = ? ORDER BY reservation_id DESC LIMIT 1",
                (task_id, tool_use_id),
            ).fetchone()
            if int(task["version"]) != int(expected_task_version) or int(call["version"]) != int(expected_tool_version):
                raise StaleState(f"Review state changed before {action}: {tool_use_id}")
            expected_status = "waiting_approval" if action in {"approve", "deny"} else "needs_review"
            if call["status"] != expected_status or task["status"] != expected_status:
                raise StaleState(f"Review state is no longer actionable: {tool_use_id}")
            reservation_state = reservation["state"] if reservation is not None else None
            if action in {"retry", "complete"} and reservation_state == "running":
                raise StaleState(f"Effect reservation is still running: {tool_use_id}")
            if action == "retry" and reservation_state == "completed":
                raise StaleState(f"Confirmed effect cannot be retried: {tool_use_id}")
            if reservation is not None and action == "retry" and reservation_state == "unknown":
                conn.execute(
                    "UPDATE effect_reservations SET state = 'cancelled', finished_at = ?, details_json = ? "
                    "WHERE reservation_id = ? AND state = 'unknown'",
                    (
                        now,
                        _json({"reason": "manual_retry", "tool_use_id": tool_use_id}),
                        int(reservation["reservation_id"]),
                    ),
                )
                reservation_state = "cancelled"
            elif reservation is not None and action == "complete" and reservation_state == "unknown":
                conn.execute(
                    "UPDATE effect_reservations SET state = 'completed', finished_at = ?, details_json = ? "
                    "WHERE reservation_id = ? AND state = 'unknown'",
                    (
                        now,
                        _json({"reason": "manual_complete", "tool_use_id": tool_use_id}),
                        int(reservation["reservation_id"]),
                    ),
                )
                reservation_state = "completed"
            if action == "approve":
                call_updated = conn.execute(
                    "UPDATE tool_calls SET status = 'planned', permission = 'allow', version = version + 1 "
                    "WHERE task_id = ? AND tool_use_id = ? AND version = ?",
                    (task_id, tool_use_id, expected_tool_version),
                )
                if call_updated.rowcount != 1:
                    raise StaleState(f"Review state changed during {action}: {tool_use_id}")
                event_type = "permission_approved"
                payload = {"tool_use_id": tool_use_id, "checkpoint_id": task["checkpoint_id"]}
            elif action == "deny":
                call_updated = conn.execute(
                    "UPDATE tool_calls SET status = 'denied', permission = 'deny', output = ?, finished_at = ?, version = version + 1 "
                    "WHERE task_id = ? AND tool_use_id = ? AND version = ?",
                    ("Permission denied by operator.", now, task_id, tool_use_id, expected_tool_version),
                )
                if call_updated.rowcount != 1:
                    raise StaleState(f"Review state changed during {action}: {tool_use_id}")
                event_type = "permission_denied"
                payload = {"tool_use_id": tool_use_id, "source": "operator", "checkpoint_id": task["checkpoint_id"]}
            elif action == "retry":
                call_updated = conn.execute(
                    "UPDATE tool_calls SET status = 'planned', permission = NULL, error = NULL, version = version + 1 "
                    "WHERE task_id = ? AND tool_use_id = ? AND version = ?",
                    (task_id, tool_use_id, expected_tool_version),
                )
                if call_updated.rowcount != 1:
                    raise StaleState(f"Review state changed during {action}: {tool_use_id}")
                event_type = "review_resolved"
                payload = {
                    "tool_use_id": tool_use_id,
                    "action": action,
                    "checkpoint_id": task["checkpoint_id"],
                    "reservation_id": reservation["reservation_id"] if reservation is not None else None,
                    "reservation_state": reservation_state,
                }
            else:
                call_updated = conn.execute(
                    "UPDATE tool_calls SET status = 'succeeded', output = ?, error = NULL, finished_at = ?, version = version + 1 "
                    "WHERE task_id = ? AND tool_use_id = ? AND version = ?",
                    ("[manually marked complete after review]", now, task_id, tool_use_id, expected_tool_version),
                )
                if call_updated.rowcount != 1:
                    raise StaleState(f"Review state changed during {action}: {tool_use_id}")
                event_type = "review_resolved"
                payload = {
                    "tool_use_id": tool_use_id,
                    "action": action,
                    "checkpoint_id": task["checkpoint_id"],
                    "reservation_id": reservation["reservation_id"] if reservation is not None else None,
                    "reservation_state": reservation_state,
                }
            updated = conn.execute(
                "UPDATE tasks SET status = 'running', last_error = NULL, updated_at = ?, version = version + 1 "
                "WHERE task_id = ? AND version = ?",
                (now, task_id, expected_task_version),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Task state changed during {action}: {task_id}")
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, event_type, _json(payload), now),
            )

    def abort_task(self, task_id: str, tool_use_id: str, reason: str,
                   expected_tool_version: int | None = None,
                   expected_task_version: int | None = None) -> int:
        """Atomically mark a reviewed call and its task as terminally aborted."""
        now = _now()
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT checkpoint_id, version, status FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if expected_task_version is not None and int(task["version"]) != int(expected_task_version):
                raise StaleState(f"Task state changed before abort: {task_id}")
            if task["status"] != "needs_review":
                raise StaleState(f"Task is not awaiting review: {task_id}")
            if task["checkpoint_id"] is None:
                raise RuntimeError(f"Task {task_id} has no checkpoint to abort")
            checkpoint = conn.execute(
                "SELECT messages_json, cursor_json FROM checkpoints WHERE checkpoint_id = ?",
                (task["checkpoint_id"],),
            ).fetchone()
            if checkpoint is None:
                raise RuntimeError(f"Task {task_id} points to a missing checkpoint")
            reservation = conn.execute(
                "SELECT reservation_id, state FROM effect_reservations "
                "WHERE task_id = ? AND tool_use_id = ? ORDER BY reservation_id DESC LIMIT 1",
                (task_id, tool_use_id),
            ).fetchone()
            reservation_state = reservation["state"] if reservation is not None else None
            if reservation_state == "running":
                raise StaleState(f"Effect reservation is still running: {tool_use_id}")
            if reservation is not None and reservation_state == "unknown":
                conn.execute(
                    "UPDATE effect_reservations SET state = 'cancelled', finished_at = ?, details_json = ? "
                    "WHERE reservation_id = ? AND state = 'unknown'",
                    (
                        now,
                        _json({"reason": "manual_abort", "tool_use_id": tool_use_id}),
                        int(reservation["reservation_id"]),
                    ),
                )
                reservation_state = "cancelled"
            updated = conn.execute(
                "UPDATE tool_calls SET status = 'aborted', output = ?, error = ?, finished_at = ?, version = version + 1 "
                "WHERE task_id = ? AND tool_use_id = ? AND status = 'needs_review'"
                + (" AND version = ?" if expected_tool_version is not None else ""),
                ("Aborted during review.", reason, now, task_id, tool_use_id)
                + ((int(expected_tool_version),) if expected_tool_version is not None else ()),
            )
            if updated.rowcount != 1:
                raise KeyError(f"Tool call not found: {tool_use_id}")

            terminal_checkpoint = conn.execute(
                "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) "
                "VALUES (?, 'aborted', ?, ?, ?)",
                (task_id, checkpoint["messages_json"], checkpoint["cursor_json"], now),
            )
            checkpoint_id = int(terminal_checkpoint.lastrowid)
            task_updated = conn.execute(
                "UPDATE tasks SET status = 'aborted', checkpoint_id = ?, last_error = ?, updated_at = ?, version = version + 1 "
                "WHERE task_id = ? AND status NOT IN ('completed', 'failed', 'aborted')"
                + (" AND version = ?" if expected_task_version is not None else ""),
                (checkpoint_id, reason, now, task_id)
                + ((int(expected_task_version),) if expected_task_version is not None else ()),
            )
            if task_updated.rowcount != 1:
                raise StaleState(f"Task changed before abort: {task_id}")
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'review_resolved', ?, ?)",
                (task_id, _json({
                    "tool_use_id": tool_use_id,
                    "action": "abort",
                    "reservation_id": reservation["reservation_id"] if reservation is not None else None,
                    "reservation_state": reservation_state,
                }), now),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_aborted', ?, ?)",
                (task_id, _json({"tool_use_id": tool_use_id, "reason": reason}), now),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'checkpoint_saved', ?, ?)",
                (task_id, _json({"checkpoint_id": checkpoint_id, "phase": "aborted"}), now),
            )
        return checkpoint_id

    def get_checkpoint(self, checkpoint_id: int) -> dict[str, Any]:
        row = self._fetchone("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))
        if row is None:
            raise KeyError(f"Checkpoint not found: {checkpoint_id}")
        item = dict(row)
        item["messages"] = _loads(item.pop("messages_json"), [])
        item["cursor"] = _loads(item.pop("cursor_json"), {})
        return item

    def list_checkpoints(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_id", (task_id,))
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["messages"] = _loads(item.pop("messages_json"), [])
            item["cursor"] = _loads(item.pop("cursor_json"), {})
            result.append(item)
        return result

    def create_model_call(self, task_id: str, turn: int, request: dict[str, Any]) -> int:
        request_json = _checked_json(request, MAX_CHECKPOINT_BYTES, "model request")
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO model_calls(task_id, turn, status, request_json, started_at) VALUES (?, ?, 'started', ?, ?)",
                (task_id, turn, request_json, _now()),
            )
            return int(cursor.lastrowid)

    def finish_model_call(self, model_call_id: int, response: dict[str, Any] | None = None,
                          error: str | None = None) -> None:
        now = _now()
        usage = (response or {}).get("usage", {})
        status = "failed" if error else "succeeded"
        response_json = (
            _checked_json(response, MAX_MODEL_RESPONSE_BYTES, "model response")
            if response is not None else None
        )
        with self.transaction() as conn:
            conn.execute(
                "UPDATE model_calls SET status = ?, response_json = ?, stop_reason = ?, "
                "input_tokens = ?, output_tokens = ?, finished_at = ?, error = ? WHERE model_call_id = ?",
                (
                    status,
                    response_json,
                    (response or {}).get("stop_reason"),
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    now,
                    error,
                    model_call_id,
                ),
            )

    def abandon_open_model_calls(self, task_id: str) -> int:
        now = _now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE model_calls SET status = 'abandoned', finished_at = ?, error = ? "
                "WHERE task_id = ? AND status = 'started'",
                (now, "replayed after process restart", task_id),
            )
            return cursor.rowcount

    def get_model_call(self, model_call_id: int) -> dict[str, Any]:
        row = self._fetchone("SELECT * FROM model_calls WHERE model_call_id = ?", (model_call_id,))
        if row is None:
            raise KeyError(f"Model call not found: {model_call_id}")
        item = dict(row)
        item["request"] = _loads(item.pop("request_json"), {})
        item["response"] = _loads(item.pop("response_json"), None)
        return item

    def list_model_calls(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM model_calls WHERE task_id = ? ORDER BY model_call_id", (task_id,))
        result = []
        for row in rows:
            item = dict(row)
            item["request"] = _loads(item.pop("request_json"), {})
            item["response"] = _loads(item.pop("response_json"), None)
            result.append(item)
        return result

    def get_tool_call(self, task_id: str, tool_use_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM tool_calls WHERE task_id = ? AND tool_use_id = ?",
            (task_id, tool_use_id),
        )
        item = self._row(row)
        if item:
            item["args"] = _loads(item.pop("args_json"), {})
            item["before_state"] = _loads(item.pop("before_state_json"), None)
            item["expected_after"] = _loads(item.pop("expected_after_json"), None)
        return item

    def list_tool_calls(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM tool_calls WHERE task_id = ? ORDER BY tool_call_row_id", (task_id,))
        result = []
        for row in rows:
            item = dict(row)
            item["args"] = _loads(item.pop("args_json"), {})
            item["before_state"] = _loads(item.pop("before_state_json"), None)
            item["expected_after"] = _loads(item.pop("expected_after_json"), None)
            result.append(item)
        return result

    def list_pending_tool_calls(self, task_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT c.*, t.repo_root, t.status AS task_status FROM tool_calls c "
            "JOIN tasks t ON t.task_id = c.task_id "
            "WHERE c.status IN ('waiting_approval', 'needs_review')"
        )
        params: tuple[Any, ...] = ()
        if task_id is not None:
            query += " AND c.task_id = ?"
            params = (task_id,)
        query += " ORDER BY c.tool_call_row_id"
        result: list[dict[str, Any]] = []
        for row in self._fetchall(query, params):
            item = dict(row)
            item["args"] = _loads(item.pop("args_json"), {})
            item["before_state"] = _loads(item.pop("before_state_json"), None)
            item["expected_after"] = _loads(item.pop("expected_after_json"), None)
            result.append(item)
        return result

    def find_tool_call(self, tool_use_id: str) -> dict[str, Any]:
        rows = self._fetchall(
            "SELECT * FROM tool_calls WHERE tool_use_id = ? ORDER BY tool_call_row_id DESC",
            (tool_use_id,),
        )
        if len(rows) > 1:
            raise RuntimeError(f"Tool call ID is ambiguous across tasks: {tool_use_id}")
        row = rows[0] if rows else None
        item = self._row(row)
        if item is None:
            raise KeyError(f"Tool call not found: {tool_use_id}")
        item["args"] = _loads(item.pop("args_json"), {})
        item["before_state"] = _loads(item.pop("before_state_json"), None)
        item["expected_after"] = _loads(item.pop("expected_after_json"), None)
        return item

    def create_tool_call(self, task_id: str, tool_use_id: str, turn: int, name: str,
                         args: dict[str, Any], args_hash: str, effect: str = "unknown_write",
                         effect_key: str | None = None) -> dict[str, Any]:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tool_calls(task_id, tool_use_id, turn, name, args_json, args_hash, status, effect, effect_key) "
                "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
                (task_id, tool_use_id, turn, name, _json(args), args_hash, effect, effect_key),
            )
        return self.get_tool_call(task_id, tool_use_id)  # type: ignore[return-value]

    def update_tool_call(self, task_id: str, tool_use_id: str, **fields: Any) -> None:
        allowed = {
            "status", "permission", "permission_rule", "permission_reason", "effect",
            "before_state", "expected_after", "output", "error", "started_at", "finished_at",
            "returncode", "stdout", "stderr", "timed_out", "execution_status",
            "effect_confirmed", "effect_confirmation",
        }
        expected_version = fields.pop("expected_version", None)
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown tool call fields: {sorted(unknown)}")
        if not fields:
            return
        values: list[Any] = []
        assignments: list[str] = []
        for key, value in fields.items():
            column = {
                "before_state": "before_state_json",
                "expected_after": "expected_after_json",
            }.get(key, key)
            assignments.append(f"{column} = ?")
            values.append(_json(value) if key in {"before_state", "expected_after"} else value)
        values.extend([task_id, tool_use_id])
        where = "task_id = ? AND tool_use_id = ?"
        if expected_version is not None:
            where += " AND version = ?"
            values.append(int(expected_version))
        with self.transaction() as conn:
            assignments.append("version = version + 1")
            cursor = conn.execute(
                f"UPDATE tool_calls SET {', '.join(assignments)} WHERE {where}",
                values,
            )
            if expected_version is not None and cursor.rowcount != 1:
                raise StaleState(f"Tool call state changed before update: {tool_use_id}")

    def start_tool_call(self, task_id: str, tool_use_id: str, effect: str, started_at: float | None = None) -> None:
        now = started_at or _now()
        with self.transaction() as conn:
            effect_increment = 0 if effect == "read_only" else 1
            cursor = conn.execute(
                "UPDATE tool_calls SET status = 'running', started_at = ?, execution_status = 'running', "
                "execution_attempts = execution_attempts + 1, effect_attempts = effect_attempts + ?, "
                "version = version + 1 WHERE task_id = ? AND tool_use_id = ? AND status = 'planned'",
                (now, effect_increment, task_id, tool_use_id),
            )
            if cursor.rowcount != 1:
                raise StaleState(f"Tool call is no longer runnable: {tool_use_id}")

    def upsert_file_observation(self, task_id: str, path: str, exists_now: bool, sha256: str | None,
                                source_tool_use_id: str | None = None,
                                identity: dict[str, Any] | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO file_observations(task_id, path, exists_now, sha256, identity_json, observed_at, source_tool_use_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, path) DO UPDATE SET "
                "exists_now = excluded.exists_now, sha256 = excluded.sha256, identity_json = excluded.identity_json, "
                "observed_at = excluded.observed_at, source_tool_use_id = excluded.source_tool_use_id",
                (task_id, path, int(exists_now), sha256, _json(identity) if identity is not None else None, _now(), source_tool_use_id),
            )

    def get_file_observation(self, task_id: str, path: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM file_observations WHERE task_id = ? AND path = ?", (task_id, path)
        )
        item = self._row(row)
        if item is not None:
            item["identity"] = _loads(item.pop("identity_json"), None)
        return item

    @staticmethod
    def _decode_reservation(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["details"] = _loads(item.pop("details_json"), {})
        return item

    def reserve_effect(
        self,
        task_id: str,
        tool_use_id: str,
        owner_id: str,
        fencing_token: int,
        effect: str,
        started_at: float | None = None,
        deadline_at: float | None = None,
        owner_pid: int | None = None,
    ) -> int:
        """Reserve one potentially side-effecting call under the current lease."""
        if self._lease_context is None:
            raise LeaseLost("Effect reservation requires a bound lease")
        context_repo, context_owner, context_token = self._lease_context
        if context_owner != owner_id or int(context_token) != int(fencing_token):
            raise LeaseLost("Effect reservation owner does not match the bound lease")
        now = started_at or _now()
        with self.transaction() as conn:
            task = conn.execute("SELECT repo_root FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if str(task["repo_root"]) != context_repo:
                raise LeaseLost("Effect reservation repository does not match the bound lease")
            blocking_rows = conn.execute(
                "SELECT r.* FROM effect_reservations r "
                "JOIN tasks t ON t.task_id = r.task_id "
                "WHERE t.repo_root = ? AND r.state IN ('running', 'unknown') "
                "ORDER BY r.reservation_id",
                (context_repo,),
            ).fetchall()
            if blocking_rows:
                raise EffectBlocked(
                    context_repo,
                    [self._decode_reservation(row) for row in blocking_rows],  # type: ignore[list-item]
                )
            existing = conn.execute(
                "SELECT reservation_id FROM effect_reservations WHERE task_id = ? AND tool_use_id = ? "
                "AND state = 'running' ORDER BY reservation_id DESC LIMIT 1",
                (task_id, tool_use_id),
            ).fetchone()
            if existing is not None:
                raise StaleState(f"Effect reservation already running: {tool_use_id}")
            cursor = conn.execute(
                "INSERT INTO effect_reservations(task_id, tool_use_id, owner_id, owner_pid, fencing_token, "
                "effect, state, started_at, deadline_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                (task_id, tool_use_id, owner_id, int(owner_pid or os.getpid()), int(fencing_token), effect, now, deadline_at),
            )
            return int(cursor.lastrowid)

    def get_effect_reservation(self, task_id: str, tool_use_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM effect_reservations WHERE task_id = ? AND tool_use_id = ? "
            "ORDER BY reservation_id DESC LIMIT 1",
            (task_id, tool_use_id),
        )
        return self._decode_reservation(row)

    def list_effect_reservations(self, task_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM effect_reservations"
        params: tuple[Any, ...] = ()
        if task_id is not None:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY reservation_id"
        return [self._decode_reservation(row) for row in self._fetchall(query, params)]  # type: ignore[list-item]

    def list_blocking_reservations(
        self, repo_root: str, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List repository reservations that must be explicitly reconciled."""
        query = (
            "SELECT r.* FROM effect_reservations r "
            "JOIN tasks t ON t.task_id = r.task_id "
            "WHERE t.repo_root = ? AND r.state IN ('running', 'unknown')"
        )
        params: list[Any] = [repo_root]
        if task_id is not None:
            query += " AND r.task_id = ?"
            params.append(task_id)
        query += " ORDER BY r.reservation_id"
        return [self._decode_reservation(row) for row in self._fetchall(query, tuple(params))]  # type: ignore[list-item]

    def finish_effect_reservation(
        self,
        reservation_id: int,
        state: str,
        details: dict[str, Any] | None = None,
        allow_stale: bool = False,
    ) -> bool:
        """Close a reservation; stale cleanup only changes the reservation journal."""
        if state not in {"completed", "unknown", "cancelled"}:
            raise ValueError(f"Invalid effect reservation state: {state}")
        now = _now()
        details_json = _checked_json(details or {}, MAX_EVENT_PAYLOAD_BYTES, "reservation details")
        guard = not allow_stale
        with self.transaction(guard=guard) as conn:
            row = conn.execute("SELECT * FROM effect_reservations WHERE reservation_id = ?", (int(reservation_id),)).fetchone()
            if row is None or row["state"] != "running":
                return False
            if self._lease_context is None:
                raise LeaseLost("Effect reservation requires a bound lease")
            _, owner_id, fencing_token = self._lease_context
            if row["owner_id"] != owner_id or int(row["fencing_token"]) != int(fencing_token):
                raise LeaseLost("Effect reservation ownership changed")
            updated = conn.execute(
                "UPDATE effect_reservations SET state = ?, finished_at = ?, details_json = ? "
                "WHERE reservation_id = ? AND state = 'running'",
                (state, now, details_json, int(reservation_id)),
            )
            return updated.rowcount == 1

    def complete_effect(
        self,
        reservation_id: int,
        task_id: str,
        tool_use_id: str,
        tool_fields: dict[str, Any],
        details: dict[str, Any] | None = None,
        observation: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
        allow_unknown: bool = False,
        event_type: str = "tool_succeeded",
    ) -> None:
        """Atomically close a reservation and persist its confirmed tool outcome."""
        allowed = {
            "status", "output", "error", "finished_at", "returncode", "stdout", "stderr",
            "timed_out", "execution_status", "effect_confirmed", "effect_confirmation",
        }
        unknown = set(tool_fields) - allowed
        if unknown:
            raise ValueError(f"Unknown completed effect fields: {sorted(unknown)}")
        if tool_fields.get("status") != "succeeded":
            raise ValueError("Completed effect must persist a succeeded tool call")
        now = _now()
        details_json = _checked_json(details or {}, MAX_EVENT_PAYLOAD_BYTES, "reservation details")
        with self.transaction() as conn:
            reservation = conn.execute(
                "SELECT task_id, tool_use_id, owner_id, fencing_token, state FROM effect_reservations "
                "WHERE reservation_id = ?",
                (int(reservation_id),),
            ).fetchone()
            if reservation is None or reservation["task_id"] != task_id or reservation["tool_use_id"] != tool_use_id:
                raise StaleState(f"Effect reservation is missing: {reservation_id}")
            if reservation["state"] != "running" and not (allow_unknown and reservation["state"] == "unknown"):
                raise StaleState(f"Effect reservation is no longer running: {reservation_id}")
            if self._lease_context is None:
                raise LeaseLost("Completed effect requires a bound lease")
            _, owner_id, fencing_token = self._lease_context
            if reservation["state"] == "running" and (
                reservation["owner_id"] != owner_id or int(reservation["fencing_token"]) != int(fencing_token)
            ):
                raise LeaseLost("Effect reservation ownership changed")
            task_repo = conn.execute("SELECT repo_root FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task_repo is None or task_repo["repo_root"] != self._lease_context[0]:
                raise LeaseLost("Completed effect repository does not match the bound lease")
            updated_reservation = conn.execute(
                "UPDATE effect_reservations SET state = 'completed', finished_at = ?, details_json = ? "
                "WHERE reservation_id = ? AND state = ?",
                (now, details_json, int(reservation_id), reservation["state"]),
            )
            if updated_reservation.rowcount != 1:
                raise StaleState(f"Effect reservation changed during completion: {reservation_id}")
            if observation is not None:
                conn.execute(
                    "INSERT INTO file_observations(task_id, path, exists_now, sha256, identity_json, observed_at, source_tool_use_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, path) DO UPDATE SET "
                    "exists_now = excluded.exists_now, sha256 = excluded.sha256, identity_json = excluded.identity_json, "
                    "observed_at = excluded.observed_at, source_tool_use_id = excluded.source_tool_use_id",
                    (
                        task_id,
                        observation["path"],
                        int(observation.get("exists_now", True)),
                        observation.get("sha256"),
                        _json(observation.get("identity")) if observation.get("identity") is not None else None,
                        now,
                        tool_use_id,
                    ),
                )
            assignments: list[str] = []
            values: list[Any] = []
            for key, value in tool_fields.items():
                assignments.append(f"{key} = ?")
                values.append(value)
            assignments.append("version = version + 1")
            values.extend([task_id, tool_use_id])
            updated_tool = conn.execute(
                f"UPDATE tool_calls SET {', '.join(assignments)} WHERE task_id = ? AND tool_use_id = ? AND status = 'running'",
                values,
            )
            if updated_tool.rowcount != 1:
                raise StaleState(f"Tool call changed during effect completion: {tool_use_id}")
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    event_type,
                    _checked_json({
                        "tool_use_id": tool_use_id,
                        "reservation_id": int(reservation_id),
                        **(event_payload or {}),
                    }, MAX_EVENT_PAYLOAD_BYTES, "event payload"),
                    now,
                ),
            )

    def scan_invariants(self, task_id: str | None = None) -> list[str]:
        """Return durable state inconsistencies without attempting silent repair."""
        violations: list[str] = []
        conn = self._connect()
        try:
            current = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"]
            if current is None or int(current) != SCHEMA_VERSION:
                violations.append(f"schema version mismatch: {current!r} != {SCHEMA_VERSION}")
            task_filter = " WHERE task_id = ?" if task_id else ""
            params = (task_id,) if task_id else ()
            tasks = conn.execute(f"SELECT * FROM tasks{task_filter}", params).fetchall()
            for task in tasks:
                prefix = f"task {task['task_id']}"
                checkpoint = conn.execute(
                    "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (task["checkpoint_id"],)
                ).fetchone() if task["checkpoint_id"] is not None else None
                if checkpoint is None:
                    violations.append(f"{prefix}: missing checkpoint pointer")
                elif checkpoint["task_id"] != task["task_id"]:
                    violations.append(f"{prefix}: checkpoint belongs to another task")
                elif task["status"] == "completed" and checkpoint["phase"] != "completed":
                    violations.append(f"{prefix}: completed task has phase {checkpoint['phase']}")
                elif task["status"] == "aborted" and checkpoint["phase"] != "aborted":
                    violations.append(f"{prefix}: aborted task has phase {checkpoint['phase']}")
                elif task["status"] == "failed" and checkpoint["phase"] != "failed":
                    violations.append(f"{prefix}: failed task has phase {checkpoint['phase']}")
                if checkpoint is not None and checkpoint["phase"] not in {
                    "input_ready", "model_responded", "tool_results_appended", "waiting_approval",
                    "needs_review", "completed", "failed", "aborted",
                }:
                    violations.append(f"{prefix}: invalid checkpoint phase {checkpoint['phase']}")
                calls = conn.execute("SELECT status FROM tool_calls WHERE task_id = ?", (task["task_id"],)).fetchall()
                invalid_statuses = {row["status"] for row in calls} - {
                    "planned", "running", "waiting_approval", "needs_review", "succeeded", "failed", "denied", "aborted"
                }
                if invalid_statuses:
                    violations.append(f"{prefix}: invalid tool status {sorted(invalid_statuses)}")
                if task["status"] == "aborted" and any(row["status"] in {"planned", "running"} for row in calls):
                    violations.append(f"{prefix}: aborted task has executable tool call")
                if task["status"] == "completed" and any(row["status"] in {"planned", "running"} for row in calls):
                    violations.append(f"{prefix}: completed task has executable tool call")
                call_statuses = {row["status"] for row in calls}
                event_types = {
                    row["type"] for row in conn.execute("SELECT type FROM events WHERE task_id = ?", (task["task_id"],))
                }
                if task["status"] == "completed" and "task_completed" not in event_types:
                    violations.append(f"{prefix}: completed task missing task_completed event")
                if task["status"] == "aborted" and "task_aborted" not in event_types:
                    violations.append(f"{prefix}: aborted task missing task_aborted event")
                if task["status"] in {"needs_review", "waiting_approval"}:
                    expected_phase = task["status"]
                    if checkpoint is None or checkpoint["phase"] != expected_phase:
                        violations.append(f"{prefix}: {task['status']} task has incompatible checkpoint")
                    checkpoint_saved = False
                    if checkpoint is not None:
                        for event in conn.execute(
                            "SELECT payload_json FROM events WHERE task_id = ? AND type = 'checkpoint_saved'",
                            (task["task_id"],),
                        ):
                            payload = _loads(event["payload_json"], {})
                            if payload.get("checkpoint_id") == checkpoint["checkpoint_id"] and payload.get("phase") == expected_phase:
                                checkpoint_saved = True
                                break
                    if not checkpoint_saved:
                        violations.append(f"{prefix}: review checkpoint missing checkpoint_saved event")
                    expected_event = "task_needs_review" if expected_phase == "needs_review" else "task_waiting_approval"
                    matching_review_event = False
                    for event in conn.execute(
                        "SELECT payload_json FROM events WHERE task_id = ? AND type = ? ORDER BY event_id DESC",
                        (task["task_id"], expected_event),
                    ):
                        payload = _loads(event["payload_json"], {})
                        if checkpoint is not None and payload.get("checkpoint_id") == checkpoint["checkpoint_id"]:
                            matching_review_event = True
                            break
                    if not matching_review_event:
                        violations.append(f"{prefix}: missing {expected_event} transition event")
                    if expected_phase == "needs_review":
                        if not any(status == "needs_review" for status in call_statuses):
                            violations.append(f"{prefix}: needs_review task has no needs_review tool call")
                        if any(status in {"planned", "running", "waiting_approval"} for status in call_statuses):
                            violations.append(f"{prefix}: needs_review task has executable tool call")
                    elif "waiting_approval" not in call_statuses:
                        violations.append(f"{prefix}: waiting_approval task has no waiting tool call")
                    if any(status in {"planned", "running"} for status in call_statuses):
                        violations.append(f"{prefix}: review task has executable tool call")
                elif checkpoint is not None and checkpoint["phase"] in {"needs_review", "waiting_approval"}:
                    checkpoint_saved = False
                    for event in conn.execute(
                        "SELECT payload_json FROM events WHERE task_id = ? AND type = 'checkpoint_saved'",
                        (task["task_id"],),
                    ):
                        payload = _loads(event["payload_json"], {})
                        if payload.get("checkpoint_id") == checkpoint["checkpoint_id"] and payload.get("phase") == checkpoint["phase"]:
                            checkpoint_saved = True
                            break
                    if not checkpoint_saved:
                        violations.append(f"{prefix}: review checkpoint missing checkpoint_saved event")
                    resolved = False
                    for event in conn.execute(
                        "SELECT type, payload_json FROM events WHERE task_id = ? "
                        "AND type IN ('permission_approved', 'permission_denied', 'review_resolved')",
                        (task["task_id"],),
                    ):
                        payload = _loads(event["payload_json"], {})
                        if payload.get("checkpoint_id") == checkpoint["checkpoint_id"]:
                            resolved = True
                            break
                    if not resolved:
                        violations.append(f"{prefix}: running task has unresolved review checkpoint")
                for event in conn.execute(
                    "SELECT payload_json FROM events WHERE task_id = ? AND type = 'checkpoint_saved'",
                    (task["task_id"],),
                ):
                    payload = _loads(event["payload_json"], {})
                    checkpoint_id = payload.get("checkpoint_id")
                    if checkpoint_id is None or conn.execute(
                        "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                        (checkpoint_id, task["task_id"]),
                    ).fetchone() is None:
                        violations.append(f"{prefix}: checkpoint_saved points to missing checkpoint")
            leases = conn.execute("SELECT * FROM leases").fetchall()
            for lease in leases:
                if not lease["owner_id"] or int(lease["fencing_token"]) < 1:
                    violations.append(f"lease {lease['repo_root']}: invalid owner or fencing token")
            reservations = conn.execute("SELECT * FROM effect_reservations").fetchall()
            valid_reservation_states = {"running", "completed", "unknown", "cancelled"}
            for reservation in reservations:
                if reservation["state"] not in valid_reservation_states:
                    violations.append(f"reservation {reservation['reservation_id']}: invalid state")
                if int(reservation["fencing_token"]) < 1 or not reservation["owner_id"]:
                    violations.append(f"reservation {reservation['reservation_id']}: invalid owner or fencing token")
                task = conn.execute("SELECT status, repo_root FROM tasks WHERE task_id = ?", (reservation["task_id"],)).fetchone()
                if task is None:
                    violations.append(f"reservation {reservation['reservation_id']}: missing task")
                elif reservation["state"] in {"running", "unknown"} and task["status"] in {"completed", "failed", "aborted"}:
                    violations.append(f"reservation {reservation['reservation_id']}: unresolved on terminal task")
                elif reservation["state"] == "unknown" and task["status"] not in {"created", "running", "needs_review"}:
                    violations.append(f"reservation {reservation['reservation_id']}: unknown on incompatible task")
                tool = conn.execute(
                    "SELECT status FROM tool_calls WHERE task_id = ? AND tool_use_id = ?",
                    (reservation["task_id"], reservation["tool_use_id"]),
                ).fetchone()
                if tool is None:
                    violations.append(f"reservation {reservation['reservation_id']}: missing tool call")
                elif reservation["state"] == "completed" and tool["status"] != "succeeded":
                    violations.append(f"reservation {reservation['reservation_id']}: completed without succeeded tool")
                elif reservation["state"] == "running" and tool["status"] != "running":
                    violations.append(f"reservation {reservation['reservation_id']}: running without running tool")
        finally:
            conn.close()
        return violations

    def assert_invariants(self, task_id: str | None = None) -> None:
        violations = self.scan_invariants(task_id)
        if violations:
            raise InvariantViolation("invariant violation: " + "; ".join(violations))

    @staticmethod
    def _process_alive(pid: int) -> bool:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            # os.kill(pid, 0) is not a non-signalling probe on Windows: it can
            # inject a delayed CTRL+C into the current console process. Query
            # the process handle instead and retain access-denied as alive.
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            ERROR_INVALID_PARAMETER = 87
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if not handle:
                error = ctypes.get_last_error()
                if error == ERROR_ACCESS_DENIED:
                    return True
                if error == ERROR_INVALID_PARAMETER:
                    return False
                # A transient/unknown Win32 failure is fail-closed: do not
                # take over a lease while process liveness is uncertain.
                return True
            try:
                exit_code = ctypes.c_uint32()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return int(exit_code.value) == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def acquire_lease(self, repo_root: str, task_id: str, owner_id: str, ttl: float = 30.0) -> int | None:
        now = _now()
        with self.transaction(guard=False) as conn:
            row = conn.execute("SELECT * FROM leases WHERE repo_root = ?", (repo_root,)).fetchone()
            if row is not None and row["expires_at"] > now:
                if row["owner_id"] != owner_id or row["task_id"] != task_id:
                    return None
                fencing_token = int(row["fencing_token"])
            else:
                running = conn.execute(
                    "SELECT r.reservation_id, r.owner_pid, r.owner_id, r.fencing_token "
                    "FROM effect_reservations r JOIN tasks t ON t.task_id = r.task_id "
                    "WHERE t.repo_root = ? AND r.state = 'running' ORDER BY r.reservation_id",
                    (repo_root,),
                ).fetchall()
                for reservation in running:
                    if self._process_alive(int(reservation["owner_pid"])):
                        return None
                    conn.execute(
                        "UPDATE effect_reservations SET state = 'unknown', finished_at = ?, details_json = ? "
                        "WHERE reservation_id = ? AND state = 'running'",
                        (now, _json({"reason": "owner_crashed", "owner_id": reservation["owner_id"]}), reservation["reservation_id"]),
                    )
                fencing_token = (int(row["fencing_token"]) if row is not None else 0) + 1
            conn.execute(
                "INSERT INTO leases(repo_root, task_id, owner_id, heartbeat_at, expires_at, fencing_token) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo_root) DO UPDATE SET task_id = excluded.task_id, owner_id = excluded.owner_id, "
                "heartbeat_at = excluded.heartbeat_at, expires_at = excluded.expires_at, "
                "fencing_token = excluded.fencing_token",
                (repo_root, task_id, owner_id, now, now + ttl, fencing_token),
            )
            return fencing_token

    def heartbeat_lease(
        self,
        repo_root: str,
        owner_id: str,
        ttl: float = 30.0,
        fencing_token: int | None = None,
    ) -> bool:
        now = _now()
        if fencing_token is None:
            return False
        with self.transaction(guard=False) as conn:
            cursor = conn.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? "
                "WHERE repo_root = ? AND owner_id = ? AND fencing_token = ? AND expires_at > ?",
                (now, now + ttl, repo_root, owner_id, int(fencing_token), now),
            )
            return cursor.rowcount == 1

    def release_lease(self, repo_root: str, owner_id: str, fencing_token: int | None = None) -> None:
        if fencing_token is None:
            return
        now = _now()
        with self.transaction(guard=False) as conn:
            lease = conn.execute(
                "SELECT owner_id, fencing_token FROM leases WHERE repo_root = ?", (repo_root,)
            ).fetchone()
            if lease is None or lease["owner_id"] != owner_id or int(lease["fencing_token"]) != int(fencing_token):
                return
            conn.execute(
                "UPDATE effect_reservations SET state = 'unknown', finished_at = ?, details_json = ? "
                "WHERE owner_id = ? AND fencing_token = ? AND state = 'running'",
                (now, _json({"reason": "lease_released", "owner_id": owner_id}), owner_id, int(fencing_token)),
            )
            # Retain the fencing epoch so a later owner cannot reuse an old token.
            conn.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? "
                "WHERE repo_root = ? AND owner_id = ? AND fencing_token = ?",
                (now, now, repo_root, owner_id, int(fencing_token)),
            )
