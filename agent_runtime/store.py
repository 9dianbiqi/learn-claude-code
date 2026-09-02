from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .effects import EffectSemantics, OperationSpec, ReconcileEvidence, sha256_json
from .migrations import SCHEMA_VERSION, SchemaManager, _legacy_projection_violations
from .models import is_valid_sha256


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


_SENSITIVE_REASON = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|cookie)\s*[=:]\s*"
    r"(?:(?:bearer|basic)\s+)?[^\s,;]+"
)


def _safe_reason(value: str) -> str:
    return _SENSITIVE_REASON.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        str(value)[:256],
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cron_interval_seconds(expression: str) -> int:
    """Return a periodic boundary for supported 5-field cron expressions.

    Timezone, calendar, day-of-month, and day-of-week expansion is out of
    scope for Phase 2; only the coarsest recurring unit is honored so a
    schedule can be triggered at a stable, durable boundary.
    """
    fields = str(expression).split()
    if len(fields) != 5:
        return 60

    def step(field: str) -> int | None:
        if field == "*":
            return 1
        if field.startswith("*/"):
            try:
                return max(1, int(field[2:]))
            except ValueError:
                return None
        return None

    minute_step = step(fields[0])
    if minute_step is not None:
        return 60 * minute_step
    hour_step = step(fields[1])
    if hour_step is not None:
        return 3600 * hour_step
    return 60


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
        # Normal Runtime startup is intentionally not a migration command.
        # Fresh databases are created directly at the latest schema; an old
        # database raises SchemaUpgradeRequired and must go through db-migrate.
        SchemaManager(self.path).ensure_latest()

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
        safe_reason = _safe_reason(reason)
        metadata["reason"] = safe_reason
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT status, version FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            call = conn.execute(
                "SELECT status, version FROM tool_calls WHERE task_id = ? AND tool_use_id = ?",
                (task_id, tool_use_id),
            ).fetchone()
            operation = conn.execute(
                "SELECT operation_id, state FROM operations WHERE task_id = ? AND tool_use_id = ?",
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
                    (safe_reason, now, task_id, tool_use_id),
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
                if operation is not None and operation["state"] == "prepared":
                    # No external effect can have happened before dispatch.
                    # Keep the operation prepared, release the claimed outbox,
                    # and cancel only the local reservation so review can
                    # safely retry the same durable intent later.
                    pre_dispatch_evidence = {
                        "outcome": "not_happened",
                        "phase": "pre_dispatch",
                        "reason": safe_reason,
                    }
                    updated_operation = conn.execute(
                        "UPDATE operations SET probe_evidence_json = ?, updated_at = ?, version = version + 1 "
                        "WHERE operation_id = ? AND state = 'prepared'",
                        (_json(pre_dispatch_evidence), now, operation["operation_id"]),
                    )
                    if updated_operation.rowcount != 1:
                        raise StaleState(f"Operation changed during pre-dispatch review: {operation['operation_id']}")
                    conn.execute(
                        "UPDATE operation_outbox SET state = 'pending', claimed_by = NULL, claimed_until = NULL, "
                        "last_error = ?, updated_at = ? WHERE operation_id = ?",
                        (safe_reason, now, operation["operation_id"]),
                    )
                    conn.execute(
                        "UPDATE effect_reservations SET state = 'cancelled', finished_at = ?, details_json = ? "
                        "WHERE operation_id = ? AND state = 'running'",
                        (now, _json(pre_dispatch_evidence), operation["operation_id"]),
                    )
                    current_operation = self._operation_row_conn(conn, str(operation["operation_id"]))
                    self._append_operation_event_conn(
                        conn,
                        task_id,
                        "operation_pre_dispatch_blocked",
                        current_operation,
                        "prepared",
                        safe_reason,
                    )
                else:
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
                (phase, checkpoint_id, safe_reason, now, task_id),
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

    def fail_plan_item_and_task(
        self,
        task_id: str,
        plan_item_id: int,
        messages: list[dict],
        cursor: dict[str, Any],
        error: str,
    ) -> int:
        """Atomically fail a DAG item and its task after a durable budget boundary."""
        now = _now()
        messages_json = _checked_json(messages, MAX_CHECKPOINT_BYTES, "checkpoint messages")
        cursor_json = _checked_json(cursor, MAX_CHECKPOINT_BYTES, "checkpoint cursor")
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT status, version, checkpoint_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] in {"completed", "failed", "aborted"}:
                return int(task["checkpoint_id"])
            item = self._plan_call(conn, plan_item_id)
            if str(item["task_id"]) != task_id:
                raise InvariantViolation(f"Plan item {plan_item_id} does not belong to task {task_id}")
            if item["status"] == "completed":
                raise StaleState(f"Plan item completed before budget failure: {plan_item_id}")
            conn.execute(
                "UPDATE tool_calls SET status = 'failed', error = ?, finished_at = ?, version = version + 1 "
                "WHERE task_id = ? AND status = 'running'",
                (error, now, task_id),
            )
            item_updated = conn.execute(
                "UPDATE plan_items SET status = 'failed', evidence_hash = NULL, "
                "version = version + 1, updated_at = ? "
                "WHERE plan_item_id = ? AND version = ? AND status != 'completed'",
                (now, plan_item_id, int(item["version"])),
            )
            if item_updated.rowcount != 1:
                raise StaleState(f"Plan item changed during budget failure: {plan_item_id}")
            conn.execute(
                "UPDATE plans SET status = 'failed', updated_at = ? WHERE plan_id = ?",
                (now, item["plan_id"]),
            )
            checkpoint = conn.execute(
                "INSERT INTO checkpoints(task_id, phase, messages_json, cursor_json, created_at) "
                "VALUES (?, 'failed', ?, ?, ?)",
                (task_id, messages_json, cursor_json, now),
            )
            checkpoint_id = int(checkpoint.lastrowid)
            task_updated = conn.execute(
                "UPDATE tasks SET status = 'failed', checkpoint_id = ?, last_error = ?, "
                "updated_at = ?, version = version + 1 WHERE task_id = ? AND version = ?",
                (checkpoint_id, error, now, task_id, int(task["version"])),
            )
            if task_updated.rowcount != 1:
                raise StaleState(f"Task changed during budget failure: {task_id}")
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'plan_item_failed', ?, ?)",
                (
                    task_id,
                    _checked_json(
                        {
                            "plan_item_id": plan_item_id,
                            "subtask_id": item["subtask_id"],
                            "from": item["status"],
                            "reason": error,
                            "budget_exhausted": True,
                        },
                        MAX_EVENT_PAYLOAD_BYTES,
                        "event payload",
                    ),
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'task_failed', ?, ?)",
                (task_id, _checked_json({"error": error}, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'checkpoint_saved', ?, ?)",
                (
                    task_id,
                    _checked_json({"checkpoint_id": checkpoint_id, "phase": "failed"}, MAX_EVENT_PAYLOAD_BYTES, "event payload"),
                    now,
                ),
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
            request = _loads(latest["request_json"], {})
            if (
                messages
                and messages[-1].get("role") == "assistant"
                and request.get("messages") != messages
            ):
                return None
            messages.append({"role": "assistant", "content": content})
            cursor = _loads(current["cursor_json"], {})
            cursor["turn"] = int(latest["turn"])
            active_subtask_id = request.get("active_subtask_id")
            if active_subtask_id:
                cursor["active_subtask_id"] = str(active_subtask_id)
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
                "SELECT reservation_id, operation_id, state FROM effect_reservations "
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
            operation = conn.execute(
                "SELECT * FROM operations WHERE task_id = ? AND tool_use_id = ?",
                (task_id, tool_use_id),
            ).fetchone()
            if operation is not None and operation["state"] in {"prepared", "dispatched", "unknown"}:
                operation_updated = conn.execute(
                    "UPDATE operations SET state = 'cancelled', completed_at = ?, updated_at = ?, version = version + 1 "
                    "WHERE operation_id = ? AND state = ? AND version = ?",
                    (now, now, operation["operation_id"], operation["state"], int(operation["version"])),
                )
                if operation_updated.rowcount != 1:
                    raise StaleState(f"Operation changed before abort: {operation['operation_id']}")
                conn.execute(
                    "UPDATE operation_outbox SET state = 'cancelled', claimed_by = NULL, claimed_until = NULL, "
                    "last_error = ?, updated_at = ? WHERE operation_id = ?",
                    (_safe_reason(reason), now, operation["operation_id"]),
                )
                conn.execute(
                    "UPDATE effect_reservations SET state = 'cancelled', finished_at = ?, details_json = ? "
                    "WHERE operation_id = ? AND state IN ('running', 'unknown')",
                    (now, _json({"reason": "manual_abort", "operation_id": operation["operation_id"]}), operation["operation_id"]),
                )
                current_operation = conn.execute(
                    "SELECT * FROM operations WHERE operation_id = ?", (operation["operation_id"],)
                ).fetchone()
                if current_operation is not None:
                    self._append_operation_event_conn(
                        conn,
                        task_id,
                        "operation_reconciled",
                        current_operation,
                        "cancelled",
                        "operator aborted operation",
                    )
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

    def create_model_call(self, task_id: str, turn: int, request: dict[str, Any],
                          source_checkpoint_id: int | None = None,
                          projection: dict[str, Any] | None = None) -> int:
        request_json = _checked_json(request, MAX_CHECKPOINT_BYTES, "model request")
        projection_json = (
            _checked_json(projection, MAX_CHECKPOINT_BYTES, "model projection")
            if projection is not None else None
        )
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO model_calls(task_id, turn, status, request_json, "
                "source_checkpoint_id, projection_json, started_at) "
                "VALUES (?, ?, 'started', ?, ?, ?, ?)",
                (task_id, turn, request_json, source_checkpoint_id, projection_json, _now()),
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
        item["projection"] = _loads(item.pop("projection_json"), None)
        return item

    def list_model_calls(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM model_calls WHERE task_id = ? ORDER BY model_call_id", (task_id,))
        result = []
        for row in rows:
            item = dict(row)
            item["request"] = _loads(item.pop("request_json"), {})
            item["response"] = _loads(item.pop("response_json"), None)
            item["projection"] = _loads(item.pop("projection_json"), None)
            result.append(item)
        return result

    def add_memory(self, task_id: str, kind: str, content: str,
                   source_checkpoint_id: int | None = None,
                   evidence_hash: str | None = None,
                   expires_at: float | None = None) -> int:
        now = _now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO memories(task_id, kind, content, source_checkpoint_id, "
                "evidence_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, kind, content, source_checkpoint_id, evidence_hash, now, expires_at),
            )
            return int(cursor.lastrowid)

    def list_memories(self, task_id: str | None = None, kind: str | None = None,
                      include_expired: bool = False) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(_now())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(
            f"SELECT * FROM memories{where} ORDER BY created_at, memory_id",
            tuple(params),
        )
        return [dict(row) for row in rows]

    def link_memories(self, source_memory_id: int, target_memory_id: int, relation: str) -> None:
        if source_memory_id == target_memory_id:
            raise ValueError("memory cannot link to itself")
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_links(source_memory_id, target_memory_id, "
                "relation, created_at) VALUES (?, ?, ?, ?)",
                (source_memory_id, target_memory_id, relation, _now()),
            )

    def list_memory_links(self, memory_id: int | None = None) -> list[dict[str, Any]]:
        if memory_id is None:
            rows = self._fetchall("SELECT * FROM memory_links ORDER BY source_memory_id, target_memory_id, relation")
        else:
            rows = self._fetchall(
                "SELECT * FROM memory_links WHERE source_memory_id = ? OR target_memory_id = ? "
                "ORDER BY source_memory_id, target_memory_id, relation",
                (memory_id, memory_id),
            )
        return [dict(row) for row in rows]

    def add_summary(self, task_id: str, scope: str, content: str,
                    source_turn_range: tuple[int, int] | None = None,
                    evidence_hash: str | None = None,
                    model_call_id: int | None = None) -> int:
        now = _now()
        turn_json = _checked_json(
            {"start": source_turn_range[0], "end": source_turn_range[1]}
            if source_turn_range is not None else None,
            MAX_CHECKPOINT_BYTES,
            "summary turn range",
        )
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO summaries(task_id, scope, content, source_turn_range, "
                "evidence_hash, model_call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, scope, content, turn_json, evidence_hash, model_call_id, now),
            )
            return int(cursor.lastrowid)

    def list_summaries(self, task_id: str | None = None,
                       scope: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(
            f"SELECT * FROM summaries{where} ORDER BY created_at, summary_id",
            tuple(params),
        )
        result = []
        for row in rows:
            item = dict(row)
            turn_range = _loads(item.pop("source_turn_range"), None)
            item["source_turn_range"] = (
                (turn_range.get("start"), turn_range.get("end"))
                if isinstance(turn_range, dict) else None
            )
            result.append(item)
        return result

    def create_plan(self, task_id: str, subtasks: list[dict[str, Any]],
                    dag_hash: str | None = None, status: str = "active") -> int:
        now = _now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO plans(task_id, status, dag_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, status, dag_hash, now, now),
            )
            plan_id = int(cursor.lastrowid)
            for subtask in subtasks:
                blocked_by = _checked_json(
                    subtask.get("blocked_by", []),
                    MAX_CHECKPOINT_BYTES,
                    "plan blocked_by",
                )
                max_turns = subtask.get("max_turns", 1)
                if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
                    raise ValueError("plan item max_turns must be a positive integer")
                conn.execute(
                    "INSERT INTO plan_items(plan_id, subtask_id, description, status, "
                    "blocked_by_json, verifier_bundle_hash, max_turns, consumed_turns, "
                    "version, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'pending', ?, ?, ?, 0, 0, ?, ?)",
                    (
                        plan_id,
                        subtask["subtask_id"],
                        subtask.get("description", ""),
                        blocked_by,
                        subtask.get("verifier_bundle_hash"),
                        max_turns,
                        now,
                        now,
                    ),
                )
            return plan_id

    def get_active_plan(self, task_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM plans WHERE task_id = ? AND status = 'active' ORDER BY plan_id DESC LIMIT 1",
            (task_id,),
        )
        return self._decode_plan(row)

    def _decode_plan(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        plan = dict(row)
        items = self._fetchall(
            "SELECT * FROM plan_items WHERE plan_id = ? ORDER BY plan_item_id",
            (plan["plan_id"],),
        )
        plan["items"] = [self._decode_plan_item(item) for item in items]
        return plan

    @staticmethod
    def _decode_plan_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["blocked_by"] = _loads(item.pop("blocked_by_json"), [])
        return item

    def get_latest_plan(self, task_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM plans WHERE task_id = ? ORDER BY plan_id DESC LIMIT 1",
            (task_id,),
        )
        return self._decode_plan(row)

    def list_plans(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id is None:
            rows = self._fetchall("SELECT * FROM plans ORDER BY created_at, plan_id")
        else:
            rows = self._fetchall("SELECT * FROM plans WHERE task_id = ? ORDER BY created_at, plan_id", (task_id,))
        return [dict(row) for row in rows]

    def list_plan_items(self, plan_id: int) -> list[dict[str, Any]]:
        rows = self._fetchall(
            "SELECT * FROM plan_items WHERE plan_id = ? ORDER BY plan_item_id",
            (plan_id,),
        )
        return [self._decode_plan_item(row) for row in rows]

    def _plan_call(self, conn: sqlite3.Connection, plan_item_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT i.*, p.task_id, p.plan_id FROM plan_items i "
            "JOIN plans p ON p.plan_id = i.plan_id WHERE i.plan_item_id = ?",
            (plan_item_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Plan item not found: {plan_item_id}")
        return row

    def _set_plan_item_state(self, conn: sqlite3.Connection, plan_item_id: int,
                             row: sqlite3.Row, new_status: str,
                             completion_summary: str | None = None,
                             evidence_hash: str | None = None,
                             event_type: str | None = None,
                             event_payload: dict[str, Any] | None = None,
                             expected_version: int | None = None) -> None:
        where = "plan_item_id = ?"
        params: list[Any] = [
            new_status,
            completion_summary,
            evidence_hash,
            _now(),
            plan_item_id,
        ]
        if expected_version is not None:
            where += " AND version = ?"
            params.append(int(expected_version))
        updated = conn.execute(
            "UPDATE plan_items SET status = ?, completion_summary = ?, evidence_hash = ?, "
            f"version = version + 1, updated_at = ? WHERE {where}",
            params,
        )
        if expected_version is not None and updated.rowcount != 1:
            raise StaleState(f"Plan item state changed before {new_status}: {plan_item_id}")
        conn.execute(
            "UPDATE plans SET updated_at = ? WHERE plan_id = ?",
            (_now(), row["plan_id"]),
        )
        if event_type is not None:
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    row["task_id"],
                    event_type,
                    _checked_json(
                        {"plan_item_id": plan_item_id, **(event_payload or {})},
                        MAX_EVENT_PAYLOAD_BYTES,
                        "event payload",
                    ),
                    _now(),
                ),
            )

    def start_plan_item(self, plan_item_id: int, *, check_dependencies: bool = True) -> None:
        with self.transaction() as conn:
            row = self._plan_call(conn, plan_item_id)
            if row["status"] not in {"pending", "retryable"}:
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot start from {row['status']}"
                )
            if check_dependencies:
                blockers = _loads(row["blocked_by_json"], [])
                for blocker in blockers:
                    blocked = conn.execute(
                        "SELECT status FROM plan_items WHERE plan_id = ? AND subtask_id = ?",
                        (row["plan_id"], blocker),
                    ).fetchone()
                    if blocked is None or blocked["status"] != "completed":
                        raise InvariantViolation(
                            f"plan item {plan_item_id} blocked by uncompleted {blocker}"
                        )
            self._set_plan_item_state(
                conn, plan_item_id, row, "in_progress",
                event_type="plan_item_started", event_payload={"from": row["status"]},
                expected_version=int(row["version"]),
            )

    def plan_item_dependencies_complete(self, plan_item_id: int) -> bool:
        row = self._fetchone(
            "SELECT plan_id, blocked_by_json FROM plan_items WHERE plan_item_id = ?",
            (plan_item_id,),
        )
        if row is None:
            raise KeyError(f"Plan item not found: {plan_item_id}")
        blockers = _loads(row["blocked_by_json"], [])
        for blocker in blockers:
            dependency = self._fetchone(
                "SELECT status FROM plan_items WHERE plan_id = ? AND subtask_id = ?",
                (row["plan_id"], blocker),
            )
            if dependency is None or dependency["status"] != "completed":
                return False
        return True

    def reserve_plan_item_turn(self, plan_item_id: int) -> int | None:
        """Durably reserve one model-call attempt without ever resetting its budget."""
        with self.transaction() as conn:
            row = self._plan_call(conn, plan_item_id)
            if row["status"] != "in_progress":
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot reserve a turn from {row['status']}"
                )
            max_turns = int(row["max_turns"])
            consumed_turns = int(row["consumed_turns"])
            if consumed_turns >= max_turns:
                return None
            next_consumed = consumed_turns + 1
            now = _now()
            updated = conn.execute(
                "UPDATE plan_items SET consumed_turns = ?, version = version + 1, updated_at = ? "
                "WHERE plan_item_id = ? AND status = 'in_progress' AND consumed_turns = ? AND version = ?",
                (next_consumed, now, plan_item_id, consumed_turns, int(row["version"])),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Plan item changed during turn reservation: {plan_item_id}")
            conn.execute(
                "UPDATE plans SET updated_at = ? WHERE plan_id = ?",
                (now, row["plan_id"]),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    row["task_id"],
                    "plan_item_turn_reserved",
                    _checked_json(
                        {
                            "plan_item_id": plan_item_id,
                            "subtask_id": row["subtask_id"],
                            "consumed_turns": next_consumed,
                            "max_turns": max_turns,
                        },
                        MAX_EVENT_PAYLOAD_BYTES,
                        "event payload",
                    ),
                    now,
                ),
            )
            return next_consumed

    def return_plan_item_to_progress(self, plan_item_id: int) -> None:
        """Release a verifier-pending item when its dependencies are no longer ready."""
        with self.transaction() as conn:
            row = self._plan_call(conn, plan_item_id)
            if row["status"] != "verifying":
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot return to progress from {row['status']}"
                )
            self._set_plan_item_state(
                conn,
                plan_item_id,
                row,
                "in_progress",
                completion_summary=row["completion_summary"],
                evidence_hash=None,
                event_type="plan_item_dependency_blocked",
                event_payload={"from": "verifying"},
                expected_version=int(row["version"]),
            )

    def submit_plan_item_for_verification(self, plan_item_id: int,
                                          completion_summary: str | None = None,
                                          evidence_hash: str | None = None) -> None:
        with self.transaction() as conn:
            row = self._plan_call(conn, plan_item_id)
            if row["status"] != "in_progress":
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot verify from {row['status']}"
                )
            self._set_plan_item_state(
                conn, plan_item_id, row, "verifying",
                completion_summary=completion_summary,
                evidence_hash=evidence_hash,
                event_type="plan_item_verifying", event_payload={"from": "in_progress"},
                expected_version=int(row["version"]),
            )

    def verify_plan_item(self, plan_item_id: int, evidence_hash: str | None = None) -> None:
        if not evidence_hash:
            raise InvariantViolation(
                f"plan item {plan_item_id} cannot complete without evidence"
            )
        with self.transaction() as conn:
            row = self._plan_call(conn, plan_item_id)
            if row["verifier_bundle_hash"] is not None:
                raise InvariantViolation(
                    f"verified plan item {plan_item_id} requires a passing verifier bundle"
                )
            if row["status"] != "verifying":
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot complete from {row['status']}"
                )
            self._set_plan_item_state(
                conn, plan_item_id, row, "completed",
                completion_summary=row["completion_summary"],
                evidence_hash=evidence_hash,
                event_type="plan_item_verified",
                event_payload={"from": "verifying"},
            )

    def fail_plan_item(self, plan_item_id: int, reason: str | None = None) -> None:
        with self.transaction() as conn:
            row = self._plan_call(conn, plan_item_id)
            if row["status"] not in {"in_progress", "verifying"}:
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot fail from {row['status']}"
                )
            self._set_plan_item_state(
                conn, plan_item_id, row, "failed",
                event_type="plan_item_failed",
                event_payload={"from": row["status"], "reason": reason},
                expected_version=int(row["version"]),
            )

    def mark_plan_item_retryable(self, plan_item_id: int) -> None:
        with self.transaction() as conn:
            row = self._plan_call(conn, plan_item_id)
            if row["status"] != "failed":
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot be retryable from {row['status']}"
                )
            self._set_plan_item_state(
                conn, plan_item_id, row, "retryable",
                event_type="plan_item_retryable",
                event_payload={"from": "failed"},
                expected_version=int(row["version"]),
            )

    def get_plan_item(self, task_id: str, subtask_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT i.*, p.task_id FROM plan_items i "
            "JOIN plans p ON p.plan_id = i.plan_id "
            "WHERE p.task_id = ? AND p.status IN ('active', 'completed', 'failed') AND i.subtask_id = ? "
            "ORDER BY i.plan_item_id DESC LIMIT 1",
            (task_id, subtask_id),
        )
        if row is None:
            return None
        return self._decode_plan_item(row)

    def has_verified_subtask(self, task_id: str) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM plan_items i JOIN plans p ON p.plan_id = i.plan_id "
            "WHERE p.task_id = ? AND i.verifier_bundle_hash IS NOT NULL LIMIT 1",
            (task_id,),
        )
        return row is not None

    def has_frozen_dag(self, task_id: str) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM plans WHERE task_id = ? AND dag_hash IS NOT NULL LIMIT 1",
            (task_id,),
        )
        return row is not None

    @staticmethod
    def _decode_verifier_run(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["evidence_manifest"] = _loads(item.pop("evidence_manifest_json"), [])
        item["authoritative"] = bool(item["authoritative"])
        return item

    @staticmethod
    def _decode_semantic_checkpoint(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["evidence_manifest"] = _loads(item.pop("evidence_manifest_json"), [])
        item["verified_subtask_checkpoint_id"] = item.pop("semantic_checkpoint_id")
        return item

    def list_verifier_runs(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall(
            "SELECT * FROM verifier_runs WHERE task_id = ? "
            "ORDER BY created_at, verifier_run_id",
            (task_id,),
        )
        return [self._decode_verifier_run(row) for row in rows]

    def get_verifier_run(self, verifier_run_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM verifier_runs WHERE verifier_run_id = ?",
            (verifier_run_id,),
        )
        return self._decode_verifier_run(row) if row is not None else None

    def list_verified_subtask_checkpoints(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall(
            "SELECT * FROM semantic_checkpoints WHERE task_id = ? "
            "ORDER BY semantic_checkpoint_id",
            (task_id,),
        )
        return [self._decode_semantic_checkpoint(row) for row in rows]

    def get_verified_subtask_checkpoint(
        self, verified_subtask_checkpoint_id: int
    ) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM semantic_checkpoints WHERE semantic_checkpoint_id = ?",
            (verified_subtask_checkpoint_id,),
        )
        return self._decode_semantic_checkpoint(row) if row is not None else None

    def get_completed_result(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "completed":
            raise InvariantViolation(f"Task {task_id} is not completed")
        final_text = ""
        for event in reversed(self.list_events(task_id)):
            if event["type"] == "task_completed":
                final_text = str(event["payload"].get("final_text") or "")
                break
        return {
            "task_id": task_id,
            "status": "completed",
            "final_text": final_text,
            "checkpoint_id": task["checkpoint_id"],
        }

    def record_non_authoritative_verifier_run(
        self,
        task_id: str,
        plan_item_id: int,
        subtask_id: str,
        status: str,
        summary: str,
        completion_summary: str,
        evidence_manifest: list[dict[str, str]],
        verifier_id: str,
        verifier_version: str,
        verification_rule: str,
        verifier_bundle_hash: str,
        verifier_implementation_hash: str,
        execution_checkpoint_id: int,
    ) -> str:
        if status not in {"fail", "uncertain"}:
            raise ValueError("non-authoritative verifier runs must be fail or uncertain")
        verifier_run_id = f"verifier_{uuid.uuid4().hex}"
        evidence_json = _checked_json(evidence_manifest, MAX_CHECKPOINT_BYTES, "evidence manifest")
        evidence_hash = sha256_json(evidence_manifest)
        now = _now()
        with self.transaction() as conn:
            item = self._plan_call(conn, plan_item_id)
            if str(item["task_id"]) != task_id or str(item["subtask_id"]) != subtask_id:
                raise InvariantViolation(f"Plan item {plan_item_id} does not belong to task/subtask")
            if item["status"] != "verifying":
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot record verification from {item['status']}"
                )
            if item["verifier_bundle_hash"] != verifier_bundle_hash:
                raise InvariantViolation(f"Verifier bundle hash mismatch for plan item {plan_item_id}")
            checkpoint = conn.execute(
                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                (execution_checkpoint_id, task_id),
            ).fetchone()
            if checkpoint is None:
                raise InvariantViolation(f"Execution checkpoint is not owned by task {task_id}")
            conn.execute(
                "INSERT INTO verifier_runs("
                "verifier_run_id, task_id, plan_item_id, subtask_id, status, summary, "
                "completion_summary, verifier_id, verifier_version, verification_rule, "
                "verifier_bundle_hash, verifier_implementation_hash, evidence_manifest_json, "
                "evidence_hash, authoritative, "
                "execution_checkpoint_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    verifier_run_id,
                    task_id,
                    plan_item_id,
                    subtask_id,
                    status,
                    summary,
                    completion_summary,
                    verifier_id,
                    verifier_version,
                    verification_rule,
                    verifier_bundle_hash,
                    verifier_implementation_hash,
                    evidence_json,
                    evidence_hash,
                    execution_checkpoint_id,
                    now,
                ),
            )
            updated = conn.execute(
                "UPDATE plan_items SET status = 'retryable', completion_summary = ?, "
                "evidence_hash = NULL, version = version + 1, updated_at = ? "
                "WHERE plan_item_id = ? AND status = 'verifying' AND version = ?",
                (completion_summary, now, plan_item_id, int(item["version"])),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Plan item changed during verifier recording: {plan_item_id}")
            conn.execute(
                "UPDATE plans SET updated_at = ? WHERE plan_id = ?",
                (now, item["plan_id"]),
            )
            payload = {
                "plan_item_id": plan_item_id,
                "subtask_id": subtask_id,
                "verifier_run_id": verifier_run_id,
                "status": status,
                "authoritative": False,
                "verifier_bundle_hash": verifier_bundle_hash,
                "evidence_hash": evidence_hash,
                "summary": summary,
            }
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "verifier_run_recorded", _checked_json(payload, MAX_EVENT_PAYLOAD_BYTES, "event payload"), now),
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "plan_item_retryable",
                    _checked_json({"plan_item_id": plan_item_id, "from": "verifying", "verifier_run_id": verifier_run_id}, MAX_EVENT_PAYLOAD_BYTES, "event payload"),
                    now,
                ),
            )
        return verifier_run_id

    def commit_verified_subtask(
        self,
        task_id: str,
        plan_item_id: int,
        subtask_id: str,
        completion_summary: str,
        verifier_summary: str,
        evidence_manifest: list[dict[str, str]],
        verifier_id: str,
        verifier_version: str,
        verification_rule: str,
        verifier_bundle_hash: str,
        verifier_implementation_hash: str,
        execution_checkpoint_id: int,
        fault_injector: Callable[..., None] | None = None,
        complete_task: bool = True,
    ) -> dict[str, Any]:
        invalid_paths = [
            entry["path"]
            for entry in evidence_manifest
            if not is_valid_sha256(entry.get("sha256", ""))
        ]
        if invalid_paths:
            raise InvariantViolation(
                "verified-subtask evidence requires lowercase 64-hex SHA-256 values: "
                + ", ".join(invalid_paths)
            )
        evidence_json = _checked_json(evidence_manifest, MAX_CHECKPOINT_BYTES, "evidence manifest")
        evidence_hash = sha256_json(evidence_manifest)
        now = _now()
        verifier_run_id = f"verifier_{uuid.uuid4().hex}"
        with self.transaction() as conn:
            item = self._plan_call(conn, plan_item_id)
            if str(item["task_id"]) != task_id or str(item["subtask_id"]) != subtask_id:
                raise InvariantViolation(f"Plan item {plan_item_id} does not belong to task/subtask")
            if item["verifier_bundle_hash"] != verifier_bundle_hash:
                raise InvariantViolation(f"Verifier bundle hash mismatch for plan item {plan_item_id}")
            task = conn.execute(
                "SELECT status, version FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] in {"completed", "failed", "aborted"}:
                raise StaleState(f"Task is terminal during verified completion: {task_id}")
            if item["status"] != "verifying":
                raise InvariantViolation(
                    f"plan item {plan_item_id} cannot complete from {item['status']}"
                )
            checkpoint = conn.execute(
                "SELECT phase FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                (execution_checkpoint_id, task_id),
            ).fetchone()
            if checkpoint is None:
                raise InvariantViolation(f"Execution checkpoint is not owned by task {task_id}")

            conn.execute(
                "INSERT INTO verifier_runs("
                "verifier_run_id, task_id, plan_item_id, subtask_id, status, summary, "
                "completion_summary, verifier_id, verifier_version, verification_rule, "
                "verifier_bundle_hash, verifier_implementation_hash, evidence_manifest_json, "
                "evidence_hash, authoritative, execution_checkpoint_id, created_at) "
                "VALUES (?, ?, ?, ?, 'pass', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    verifier_run_id,
                    task_id,
                    plan_item_id,
                    subtask_id,
                    verifier_summary,
                    completion_summary,
                    verifier_id,
                    verifier_version,
                    verification_rule,
                    verifier_bundle_hash,
                    verifier_implementation_hash,
                    evidence_json,
                    evidence_hash,
                    execution_checkpoint_id,
                    now,
                ),
            )
            semantic = conn.execute(
                "INSERT INTO semantic_checkpoints("
                "task_id, plan_item_id, subtask_id, verifier_run_id, execution_checkpoint_id, "
                "completion_summary, verifier_id, verifier_version, verification_rule, "
                "verifier_bundle_hash, verifier_implementation_hash, evidence_manifest_json, "
                "evidence_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    plan_item_id,
                    subtask_id,
                    verifier_run_id,
                    execution_checkpoint_id,
                    completion_summary,
                    verifier_id,
                    verifier_version,
                    verification_rule,
                    verifier_bundle_hash,
                    verifier_implementation_hash,
                    evidence_json,
                    evidence_hash,
                    now,
                ),
            )
            verified_subtask_checkpoint_id = int(semantic.lastrowid)
            updated_item = conn.execute(
                "UPDATE plan_items SET status = 'completed', completion_summary = ?, "
                "evidence_hash = ?, version = version + 1, updated_at = ? "
                "WHERE plan_item_id = ? AND status = 'verifying' AND version = ?",
                (completion_summary, evidence_hash, now, plan_item_id, int(item["version"])),
            )
            if updated_item.rowcount != 1:
                raise StaleState(f"Plan item changed during verified completion: {plan_item_id}")
            remaining = conn.execute(
                "SELECT COUNT(*) FROM plan_items WHERE plan_id = ? AND status != 'completed'",
                (item["plan_id"],),
            ).fetchone()[0]
            if complete_task:
                if remaining != 0:
                    raise InvariantViolation(
                        f"Task completion requested with {remaining} plan item(s) remaining"
                    )
                conn.execute(
                    "UPDATE plans SET status = 'completed', updated_at = ? WHERE plan_id = ?",
                    (now, item["plan_id"]),
                )
                updated_checkpoint = conn.execute(
                    "UPDATE checkpoints SET phase = 'completed' "
                    "WHERE checkpoint_id = ? AND task_id = ?",
                    (execution_checkpoint_id, task_id),
                )
                if updated_checkpoint.rowcount != 1:
                    raise StaleState(
                        f"Execution checkpoint changed during verified completion: {execution_checkpoint_id}"
                    )
                updated_task = conn.execute(
                    "UPDATE tasks SET status = 'completed', checkpoint_id = ?, last_error = NULL, "
                    "updated_at = ?, version = version + 1 WHERE task_id = ? AND version = ?",
                    (execution_checkpoint_id, now, task_id, int(task["version"])),
                )
                if updated_task.rowcount != 1:
                    raise StaleState(f"Task changed during verified completion: {task_id}")
            else:
                if remaining == 0:
                    raise InvariantViolation(
                        "Intermediate verified completion cannot finish the plan"
                    )
                conn.execute(
                    "UPDATE plans SET status = 'active', updated_at = ? WHERE plan_id = ?",
                    (now, item["plan_id"]),
                )

            run_payload = {
                "plan_item_id": plan_item_id,
                "subtask_id": subtask_id,
                "verifier_run_id": verifier_run_id,
                "verified_subtask_checkpoint_id": verified_subtask_checkpoint_id,
                "status": "pass",
                "authoritative": True,
                "verifier_bundle_hash": verifier_bundle_hash,
                "evidence_hash": evidence_hash,
            }
            event_rows = (
                (
                    "verifier_run_recorded",
                    run_payload,
                ),
                (
                    "plan_item_verified",
                    {
                        "plan_item_id": plan_item_id,
                        "from": "verifying",
                        "verifier_run_id": verifier_run_id,
                    },
                ),
                (
                    "verified_subtask_checkpoint_created",
                    {
                        "verified_subtask_checkpoint_id": verified_subtask_checkpoint_id,
                        "verifier_run_id": verifier_run_id,
                        "execution_checkpoint_id": execution_checkpoint_id,
                    },
                ),
            )
            if complete_task:
                event_rows += (
                    ("task_completed", {"final_text": completion_summary}),
                    ("verified_subtask_committed", run_payload),
                    (
                        "checkpoint_saved",
                        {
                            "checkpoint_id": execution_checkpoint_id,
                            "phase": "completed",
                        },
                    ),
                )
            else:
                event_rows += (("verified_subtask_committed", run_payload),)
            for event_type, payload in event_rows:
                conn.execute(
                    "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    (
                        task_id,
                        event_type,
                        _checked_json(payload, MAX_EVENT_PAYLOAD_BYTES, "event payload"),
                        now,
                    ),
                )
            if fault_injector:
                fault_injector(
                    "verified_subtask_f2_mid",
                    task_id=task_id,
                    plan_item_id=plan_item_id,
                    verifier_run_id=verifier_run_id,
                    verified_subtask_checkpoint_id=verified_subtask_checkpoint_id,
                )
        result = {
            "verifier_run_id": verifier_run_id,
            "verified_subtask_checkpoint_id": verified_subtask_checkpoint_id,
            "execution_checkpoint_id": execution_checkpoint_id,
        }
        if fault_injector:
            fault_injector(
                "verified_subtask_f3",
                task_id=task_id,
                plan_item_id=plan_item_id,
                verifier_run_id=verifier_run_id,
                verified_subtask_checkpoint_id=verified_subtask_checkpoint_id,
            )
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
            "effect_confirmed", "effect_confirmation", "operation_id",
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
            "LEFT JOIN operations o ON o.operation_id = r.operation_id "
            "WHERE t.repo_root = ? AND r.state IN ('running', 'unknown') "
            "AND (o.operation_id IS NULL OR o.state IN ('dispatched', 'unknown'))"
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
                "SELECT task_id, tool_use_id, operation_id, owner_id, fencing_token, state FROM effect_reservations "
                "WHERE reservation_id = ?",
                (int(reservation_id),),
            ).fetchone()
            if reservation is None or reservation["task_id"] != task_id or reservation["tool_use_id"] != tool_use_id:
                raise StaleState(f"Effect reservation is missing: {reservation_id}")
            if reservation["state"] == "completed" and reservation["operation_id"] is not None:
                # v0.2's operation commit is authoritative. This no-op
                # compatibility projection lets v0.1.1 instrumentation call
                # complete_effect without reopening a committed reservation.
                return
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

    @staticmethod
    def _decode_operation(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for column, target, default in (
            ("request_json", "request", {}),
            ("result_json", "result", None),
            ("probe_evidence_json", "probe_evidence", None),
        ):
            if column in item:
                item[target] = _loads(item.pop(column), default)
        return item

    @staticmethod
    def _operation_event_payload(operation: sqlite3.Row | dict[str, Any], state: str,
                                 reason: str | None = None) -> dict[str, Any]:
        item = dict(operation)
        payload: dict[str, Any] = {
            "operation_id": item.get("operation_id"),
            "tool_use_id": item.get("tool_use_id"),
            "semantics": item.get("semantics"),
            "adapter": item.get("adapter"),
            "attempt": int(item.get("attempt_count") or 0),
            "state": state,
        }
        if reason:
            # Events are an audit index, not a result channel. Keep only a
            # bounded reason and never copy request/output/evidence fields.
            payload["reason"] = _safe_reason(reason)
        return payload

    def _append_operation_event_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        event_type: str,
        operation: sqlite3.Row | dict[str, Any],
        state: str,
        reason: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (
                task_id,
                event_type,
                _checked_json(
                    self._operation_event_payload(operation, state, reason),
                    MAX_EVENT_PAYLOAD_BYTES,
                    "event payload",
                ),
                _now(),
            ),
        )

    def _operation_lease_conn(self, conn: sqlite3.Connection, task_id: str) -> tuple[str, int]:
        if self._lease_context is None:
            raise LeaseLost("Operation transition requires a bound lease")
        self._assert_lease_conn(conn)
        repo_root, owner_id, fencing_token = self._lease_context
        task = conn.execute("SELECT repo_root FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None or str(task["repo_root"]) != repo_root:
            raise LeaseLost("Operation repository does not match the bound lease")
        return owner_id, int(fencing_token)

    def _operation_row_conn(self, conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Operation not found: {operation_id}")
        return row

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        return self._decode_operation(
            self._fetchone("SELECT * FROM operations WHERE operation_id = ?", (operation_id,))
        )

    def get_operation_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        return self._decode_operation(
            self._fetchone("SELECT * FROM operations WHERE dedupe_key = ?", (dedupe_key,))
        )

    def get_operation_for_tool_call(self, task_id: str, tool_use_id: str) -> dict[str, Any] | None:
        return self._decode_operation(
            self._fetchone(
                """
                SELECT o.* FROM operations o
                WHERE o.task_id = ? AND o.tool_use_id = ?
                """,
                (task_id, tool_use_id),
            )
        )

    def list_operations(self, task_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT o.*, b.state AS outbox_state, b.available_at, b.claimed_by, b.claimed_until, b.delivery_attempts, b.last_error FROM operations o LEFT JOIN operation_outbox b ON b.operation_id = o.operation_id"
        params: tuple[Any, ...] = ()
        if task_id is not None:
            query += " WHERE o.task_id = ?"
            params = (task_id,)
        query += " ORDER BY o.created_at, o.operation_id"
        return [self._decode_operation(row) for row in self._fetchall(query, params)]  # type: ignore[list-item]

    def list_pending_operations(self, task_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT o.*, b.state AS outbox_state, b.available_at, b.claimed_by, "
            "b.claimed_until, b.delivery_attempts, b.last_error "
            "FROM operations o JOIN operation_outbox b ON b.operation_id = o.operation_id "
            "WHERE (o.state IN ('prepared', 'dispatched', 'unknown') "
            "OR b.state IN ('pending', 'claimed', 'blocked'))"
        )
        params: list[Any] = []
        if task_id is not None:
            query += " AND o.task_id = ?"
            params.append(task_id)
        query += " ORDER BY o.updated_at, o.operation_id"
        return [self._decode_operation(row) for row in self._fetchall(query, tuple(params))]  # type: ignore[list-item]

    @staticmethod
    def _operation_effect(spec: OperationSpec) -> str:
        if spec.adapter == "file":
            return "file_write"
        if spec.semantics == EffectSemantics.IDEMPOTENT:
            return "idempotent"
        return "unknown_write"

    def prepare_operation(
        self,
        spec: OperationSpec | None = None,
        *,
        owner_pid: int | None = None,
        deadline_at: float | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Atomically prepare an effect, its outbox row, reservation, and audit event."""
        if spec is None:
            try:
                raw_semantics = fields.pop("semantics")
                spec = OperationSpec(
                    task_id=str(fields.pop("task_id")),
                    tool_use_id=str(fields.pop("tool_use_id")),
                    adapter=str(fields.pop("adapter")),
                    semantics=raw_semantics if isinstance(raw_semantics, EffectSemantics)
                    else EffectSemantics(str(raw_semantics)),
                    effect_scope=str(fields.pop("effect_scope")),
                    dedupe_key=str(fields.pop("dedupe_key")),
                    idempotency_key=fields.pop("idempotency_key", None),
                    args_hash=str(fields.pop("args_hash")),
                    request=dict(fields.pop("request")),
                )
            except KeyError as exc:
                raise TypeError(f"missing OperationSpec field: {exc.args[0]}") from exc
        if fields:
            raise TypeError(f"unknown prepare_operation fields: {sorted(fields)}")
        if not isinstance(spec.semantics, EffectSemantics):
            spec = OperationSpec(
                spec.task_id,
                spec.tool_use_id,
                spec.adapter,
                EffectSemantics(str(spec.semantics)),
                spec.effect_scope,
                spec.dedupe_key,
                spec.idempotency_key,
                spec.args_hash,
                spec.request,
            )
        if spec.semantics == EffectSemantics.REPLAY_SAFE:
            raise ValueError("read-only effects do not create operations")
        if not spec.dedupe_key or not spec.args_hash or not spec.adapter:
            raise ValueError("operation dedupe_key, args_hash, and adapter are required")
        request_json = _checked_json(spec.request, MAX_CHECKPOINT_BYTES, "operation request")
        now = _now()
        operation_id = "op_" + hashlib.sha256(
            f"{spec.task_id}\0{spec.tool_use_id}".encode("utf-8")
        ).hexdigest()[:48]
        if spec.idempotency_key is not None:
            conflict = self._fetchone(
                "SELECT * FROM operations WHERE effect_scope = ? AND idempotency_key = ?",
                (spec.effect_scope, spec.idempotency_key),
            )
            if conflict is not None and str(conflict["dedupe_key"]) != spec.dedupe_key:
                with self.transaction() as audit_conn:
                    self._operation_lease_conn(audit_conn, spec.task_id)
                    requested = {
                        "operation_id": operation_id,
                        "tool_use_id": spec.tool_use_id,
                        "semantics": spec.semantics.value,
                        "adapter": spec.adapter,
                        "attempt_count": 0,
                    }
                    self._append_operation_event_conn(
                        audit_conn,
                        spec.task_id,
                        "operation_idempotency_conflict",
                        requested,
                        "prepared",
                        f"idempotency key is already bound to operation {conflict['operation_id']}",
                    )
                raise StaleState(
                    f"Idempotency key already exists in effect scope: {spec.effect_scope}"
                )
        with self.transaction() as conn:
            owner_id, fencing_token = self._operation_lease_conn(conn, spec.task_id)
            existing = conn.execute(
                "SELECT * FROM operations WHERE dedupe_key = ?", (spec.dedupe_key,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["task_id"]) != spec.task_id
                    or str(existing["tool_use_id"]) != spec.tool_use_id
                    or str(existing["args_hash"]) != spec.args_hash
                    or str(existing["adapter"]) != spec.adapter
                    or str(existing["semantics"]) != spec.semantics.value
                ):
                    raise StaleState(f"Operation dedupe key conflicts: {spec.dedupe_key}")
                if existing["state"] == "committed":
                    self._append_operation_event_conn(
                        conn,
                        spec.task_id,
                        "operation_deduplicated",
                        existing,
                        "committed",
                        "committed operation already has a durable result",
                    )
                return self._decode_operation(existing)  # type: ignore[return-value]

            call = conn.execute(
                "SELECT * FROM tool_calls WHERE task_id = ? AND tool_use_id = ?",
                (spec.task_id, spec.tool_use_id),
            ).fetchone()
            if call is None:
                raise InvariantViolation(f"Missing tool call for operation: {spec.tool_use_id}")
            if str(call["args_hash"]) != spec.args_hash:
                raise StaleState(f"Tool call args hash changed: {spec.tool_use_id}")
            if call["operation_id"] is not None and str(call["operation_id"]) != operation_id:
                raise StaleState(f"Tool call is bound to another operation: {spec.tool_use_id}")
            task = conn.execute(
                "SELECT repo_root, status FROM tasks WHERE task_id = ?", (spec.task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"Task not found: {spec.task_id}")
            if task["status"] in {"completed", "failed", "aborted"}:
                raise StaleState(f"Task is terminal: {spec.task_id}")

            blocking = conn.execute(
                """
                SELECT r.* FROM effect_reservations r
                JOIN tasks t ON t.task_id = r.task_id
                LEFT JOIN operations o ON o.operation_id = r.operation_id
                WHERE t.repo_root = ? AND r.state IN ('running', 'unknown')
                  AND (o.operation_id IS NULL OR o.state IN ('dispatched', 'unknown'))
                ORDER BY r.reservation_id
                """,
                (str(task["repo_root"]),),
            ).fetchall()
            if blocking:
                raise EffectBlocked(str(task["repo_root"]), [
                    self._decode_reservation(row) for row in blocking  # type: ignore[list-item]
                ])

            try:
                conn.execute(
                    """
                    INSERT INTO operations(
                        operation_id, task_id, tool_use_id, adapter, semantics,
                        effect_scope, dedupe_key, idempotency_key, args_hash,
                        state, request_json, attempt_count, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, 0, 0, ?, ?)
                    """,
                    (
                        operation_id,
                        spec.task_id,
                        spec.tool_use_id,
                        spec.adapter,
                        spec.semantics.value,
                        spec.effect_scope,
                        spec.dedupe_key,
                        spec.idempotency_key,
                        spec.args_hash,
                        request_json,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if spec.idempotency_key is not None or "idempotency" in str(exc).lower():
                    raise StaleState(
                        f"Idempotency key already exists in effect scope: {spec.effect_scope}"
                    ) from exc
                raise StaleState(f"Operation already exists for tool call: {spec.tool_use_id}") from exc
            conn.execute(
                """
                INSERT INTO operation_outbox(
                    operation_id, state, available_at, claimed_by, claimed_until,
                    delivery_attempts, last_error, updated_at
                ) VALUES (?, 'pending', ?, NULL, NULL, 0, NULL, ?)
                """,
                (operation_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO effect_reservations(
                    task_id, tool_use_id, operation_id, owner_id, owner_pid,
                    fencing_token, effect, state, started_at, deadline_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    spec.task_id,
                    spec.tool_use_id,
                    operation_id,
                    owner_id,
                    int(owner_pid or os.getpid()),
                    fencing_token,
                    self._operation_effect(spec),
                    now,
                    deadline_at,
                ),
            )
            updated = conn.execute(
                """
                UPDATE tool_calls SET operation_id = ?, version = version + 1
                WHERE task_id = ? AND tool_use_id = ? AND operation_id IS NULL
                """,
                (operation_id, spec.task_id, spec.tool_use_id),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Tool call changed while preparing operation: {spec.tool_use_id}")
            operation = self._operation_row_conn(conn, operation_id)
            self._append_operation_event_conn(
                conn,
                spec.task_id,
                "operation_prepared",
                operation,
                "prepared",
                "effect intent and outbox prepared",
            )
        return self.get_operation(operation_id)  # type: ignore[return-value]

    def claim_operation(
        self,
        operation_id: str,
        *,
        claim_ttl: float = 30.0,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.transaction() as conn:
            operation = self._operation_row_conn(conn, operation_id)
            owner_id, fencing_token = self._operation_lease_conn(conn, str(operation["task_id"]))
            outbox = conn.execute(
                "SELECT * FROM operation_outbox WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if outbox is None:
                raise InvariantViolation(f"Missing operation outbox: {operation_id}")
            if operation["state"] != "prepared":
                if operation["state"] == "committed":
                    return self._decode_operation(operation)  # type: ignore[return-value]
                raise StaleState(f"Operation is not prepared: {operation_id}")
            if expected_version is not None and int(operation["version"]) != int(expected_version):
                raise StaleState(f"Operation version changed before claim: {operation_id}")
            if outbox["state"] not in {"pending", "claimed"}:
                raise StaleState(f"Operation outbox is not claimable: {operation_id}")
            if (
                outbox["state"] == "claimed"
                and outbox["claimed_by"] != owner_id
                and outbox["claimed_until"] is not None
                and float(outbox["claimed_until"]) > now
            ):
                raise StaleState(f"Operation outbox is claimed by another owner: {operation_id}")
            version = int(operation["version"])
            updated = conn.execute(
                "UPDATE operations SET updated_at = ?, version = version + 1 "
                "WHERE operation_id = ? AND state = 'prepared' AND version = ?",
                (now, operation_id, version),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Operation changed during claim: {operation_id}")
            outbox_updated = conn.execute(
                """
                UPDATE operation_outbox SET state = 'claimed', claimed_by = ?,
                    claimed_until = ?, delivery_attempts = delivery_attempts + 1,
                    updated_at = ?
                WHERE operation_id = ? AND state IN ('pending', 'claimed')
                """,
                (owner_id, now + claim_ttl, now, operation_id),
            )
            if outbox_updated.rowcount != 1:
                raise StaleState(f"Operation outbox changed during claim: {operation_id}")
            reservation = conn.execute(
                """
                SELECT reservation_id FROM effect_reservations
                WHERE operation_id = ? AND owner_id = ? AND fencing_token = ? AND state = 'running'
                ORDER BY reservation_id DESC LIMIT 1
                """,
                (operation_id, owner_id, fencing_token),
            ).fetchone()
            if reservation is None:
                conn.execute(
                    """
                    INSERT INTO effect_reservations(
                        task_id, tool_use_id, operation_id, owner_id, owner_pid,
                        fencing_token, effect, state, started_at, deadline_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        operation["task_id"],
                        operation["tool_use_id"],
                        operation_id,
                        owner_id,
                        os.getpid(),
                        fencing_token,
                        "file_write" if operation["adapter"] == "file" else "unknown_write",
                        now,
                        now + claim_ttl,
                    ),
                )
            current = self._operation_row_conn(conn, operation_id)
            self._append_operation_event_conn(
                conn,
                str(operation["task_id"]),
                "operation_claimed",
                current,
                "prepared",
                "outbox claimed by the current lease",
            )
        return self.get_operation(operation_id)  # type: ignore[return-value]

    def mark_operation_dispatched(
        self,
        operation_id: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.transaction() as conn:
            operation = self._operation_row_conn(conn, operation_id)
            owner_id, fencing_token = self._operation_lease_conn(conn, str(operation["task_id"]))
            if operation["state"] != "prepared":
                if operation["state"] == "dispatched":
                    return self._decode_operation(operation)  # type: ignore[return-value]
                raise StaleState(f"Operation is not dispatchable: {operation_id}")
            version = int(operation["version"])
            if expected_version is not None and version != int(expected_version):
                raise StaleState(f"Operation version changed before dispatch: {operation_id}")
            outbox = conn.execute(
                "SELECT * FROM operation_outbox WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if (
                outbox is None
                or outbox["state"] != "claimed"
                or outbox["claimed_by"] != owner_id
                or outbox["claimed_until"] is None
                or float(outbox["claimed_until"]) <= now
            ):
                raise StaleState(f"Operation outbox is not owned for dispatch: {operation_id}")
            updated = conn.execute(
                """
                UPDATE operations SET state = 'dispatched', attempt_count = attempt_count + 1,
                    dispatched_at = ?, updated_at = ?, version = version + 1
                WHERE operation_id = ? AND state = 'prepared' AND version = ?
                """,
                (now, now, operation_id, version),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Operation changed during dispatch: {operation_id}")
            reservation = conn.execute(
                """
                SELECT reservation_id FROM effect_reservations
                WHERE operation_id = ? AND owner_id = ? AND fencing_token = ? AND state = 'running'
                ORDER BY reservation_id DESC LIMIT 1
                """,
                (operation_id, owner_id, fencing_token),
            ).fetchone()
            if reservation is None:
                raise StaleState(f"Operation reservation is not owned for dispatch: {operation_id}")
            call_updated = conn.execute(
                """
                UPDATE tool_calls SET status = 'running', started_at = ?,
                    execution_status = 'running', execution_attempts = execution_attempts + 1,
                    effect_attempts = effect_attempts + 1, version = version + 1
                WHERE task_id = ? AND tool_use_id = ? AND status = 'planned'
                """,
                (now, operation["task_id"], operation["tool_use_id"]),
            )
            if call_updated.rowcount != 1:
                raise StaleState(f"Tool call is not planned for dispatch: {operation['tool_use_id']}")
            conn.execute(
                "UPDATE operation_outbox SET updated_at = ? WHERE operation_id = ?",
                (now, operation_id),
            )
            current = self._operation_row_conn(conn, operation_id)
            self._append_operation_event_conn(
                conn,
                str(operation["task_id"]),
                "operation_dispatched",
                current,
                "dispatched",
                "external effect boundary entered",
            )
        return self.get_operation(operation_id)  # type: ignore[return-value]

    @staticmethod
    def _evidence_payload(evidence: ReconcileEvidence | dict[str, Any] | None) -> dict[str, Any] | None:
        if evidence is None:
            return None
        if isinstance(evidence, ReconcileEvidence):
            return {
                "outcome": evidence.outcome,
                "reason": evidence.reason,
                "evidence": evidence.evidence,
            }
        return dict(evidence)

    def _update_operation_tool_fields(
        self,
        conn: sqlite3.Connection,
        operation: sqlite3.Row,
        tool_fields: dict[str, Any],
        *,
        statuses: tuple[str, ...] = ("running", "needs_review", "planned"),
    ) -> None:
        allowed = {
            "status", "output", "error", "finished_at", "returncode", "stdout", "stderr",
            "timed_out", "execution_status", "effect_confirmed", "effect_confirmation",
        }
        unknown = set(tool_fields) - allowed
        if unknown:
            raise ValueError(f"Unknown operation tool fields: {sorted(unknown)}")
        assignments = []
        values: list[Any] = []
        for key, value in tool_fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        if not assignments:
            return
        assignments.append("version = version + 1")
        placeholders = ",".join("?" for _ in statuses)
        values.extend([operation["task_id"], operation["tool_use_id"], *statuses])
        updated = conn.execute(
            f"UPDATE tool_calls SET {', '.join(assignments)} "
            f"WHERE task_id = ? AND tool_use_id = ? AND status IN ({placeholders})",
            values,
        )
        if updated.rowcount != 1:
            current = conn.execute(
                "SELECT status FROM tool_calls WHERE task_id = ? AND tool_use_id = ?",
                (operation["task_id"], operation["tool_use_id"]),
            ).fetchone()
            if current is None or current["status"] not in {"succeeded", "failed", "aborted"}:
                raise StaleState(f"Tool call changed during operation transition: {operation['tool_use_id']}")

    def _upsert_operation_observation(
        self,
        conn: sqlite3.Connection,
        operation: sqlite3.Row,
        observation: dict[str, Any] | None,
        now: float,
    ) -> None:
        if observation is None:
            return
        conn.execute(
            """
            INSERT INTO file_observations(
                task_id, path, exists_now, sha256, identity_json, observed_at, source_tool_use_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, path) DO UPDATE SET
                exists_now = excluded.exists_now,
                sha256 = excluded.sha256,
                identity_json = excluded.identity_json,
                observed_at = excluded.observed_at,
                source_tool_use_id = excluded.source_tool_use_id
            """,
            (
                operation["task_id"],
                observation["path"],
                int(observation.get("exists_now", True)),
                observation.get("sha256"),
                _json(observation.get("identity")) if observation.get("identity") is not None else None,
                now,
                operation["tool_use_id"],
            ),
        )

    def commit_operation(
        self,
        operation_id: str,
        result: Any = None,
        evidence: ReconcileEvidence | dict[str, Any] | None = None,
        *,
        tool_fields: dict[str, Any] | None = None,
        observation: dict[str, Any] | None = None,
        expected_version: int | None = None,
        allow_unknown: bool = False,
        reason: str = "effect result committed",
        emit_tool_event: bool = True,
    ) -> dict[str, Any]:
        now = _now()
        evidence_payload = self._evidence_payload(evidence)
        result_json = _checked_json(result, MAX_CHECKPOINT_BYTES, "operation result")
        result_digest = sha256_json(result)
        with self.transaction() as conn:
            operation = self._operation_row_conn(conn, operation_id)
            self._operation_lease_conn(conn, str(operation["task_id"]))
            if operation["state"] not in {"dispatched", "unknown"}:
                if operation["state"] == "committed":
                    return self._decode_operation(operation)  # type: ignore[return-value]
                raise StaleState(f"Operation cannot commit from {operation['state']}: {operation_id}")
            if operation["state"] == "unknown" and not allow_unknown:
                raise StaleState(f"Unknown operation requires explicit reconciliation: {operation_id}")
            if expected_version is not None and int(operation["version"]) != int(expected_version):
                raise StaleState(f"Operation version changed before commit: {operation_id}")
            previous_state = str(operation["state"])
            version = int(operation["version"])
            updated = conn.execute(
                """
                UPDATE operations SET state = 'committed', result_json = ?, result_digest = ?,
                    probe_evidence_json = ?, completed_at = ?, updated_at = ?, version = version + 1
                WHERE operation_id = ? AND state = ? AND version = ?
                """,
                (
                    result_json,
                    result_digest,
                    _json(evidence_payload) if evidence_payload is not None else None,
                    now,
                    now,
                    operation_id,
                    previous_state,
                    version,
                ),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Operation changed during commit: {operation_id}")
            conn.execute(
                """
                UPDATE operation_outbox SET state = 'delivered', claimed_by = NULL,
                    claimed_until = NULL, last_error = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (now, operation_id),
            )
            conn.execute(
                "UPDATE effect_reservations SET state = 'completed', finished_at = ?, details_json = ? "
                "WHERE operation_id = ? AND state IN ('running', 'unknown')",
                (now, _json(evidence_payload or {"reason": reason}), operation_id),
            )
            self._upsert_operation_observation(conn, operation, observation, now)
            fields = dict(tool_fields or {})
            fields.setdefault("status", "succeeded")
            fields.setdefault("finished_at", now)
            if "output" not in fields and isinstance(result, str):
                fields["output"] = result
            if fields.get("status") != "succeeded":
                raise ValueError("Committed operation must persist a succeeded tool call")
            self._update_operation_tool_fields(conn, operation, fields)
            current = self._operation_row_conn(conn, operation_id)
            self._append_operation_event_conn(
                conn,
                str(operation["task_id"]),
                "operation_committed",
                current,
                "committed",
                reason,
            )
            if emit_tool_event:
                conn.execute(
                    "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'tool_succeeded', ?, ?)",
                    (
                        operation["task_id"],
                        _checked_json(
                            {
                                "tool_use_id": operation["tool_use_id"],
                                "operation_id": operation_id,
                                "output_chars": len(str(fields.get("output", ""))),
                            },
                            MAX_EVENT_PAYLOAD_BYTES,
                            "event payload",
                        ),
                        now,
                    ),
                )
        return self.get_operation(operation_id)  # type: ignore[return-value]

    def fail_operation(
        self,
        operation_id: str,
        reason: str,
        *,
        tool_fields: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.transaction() as conn:
            operation = self._operation_row_conn(conn, operation_id)
            self._operation_lease_conn(conn, str(operation["task_id"]))
            if operation["state"] != "dispatched":
                raise StaleState(f"Operation cannot fail from {operation['state']}: {operation_id}")
            version = int(operation["version"])
            if expected_version is not None and version != int(expected_version):
                raise StaleState(f"Operation version changed before failure: {operation_id}")
            updated = conn.execute(
                "UPDATE operations SET state = 'failed', updated_at = ?, completed_at = ?, version = version + 1 "
                "WHERE operation_id = ? AND state = ? AND version = ?",
                (now, now, operation_id, operation["state"], version),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Operation changed during failure: {operation_id}")
            conn.execute(
                "UPDATE operation_outbox SET state = 'delivered', claimed_by = NULL, claimed_until = NULL, "
                "last_error = ?, updated_at = ? WHERE operation_id = ?",
                (_safe_reason(reason), now, operation_id),
            )
            conn.execute(
                "UPDATE effect_reservations SET state = 'cancelled', finished_at = ?, details_json = ? "
                "WHERE operation_id = ? AND state = 'running'",
                (now, _json({"reason": _safe_reason(reason)}), operation_id),
            )
            fields = dict(tool_fields or {})
            fields.setdefault("status", "failed")
            fields.setdefault("error", _safe_reason(reason))
            fields.setdefault("finished_at", now)
            self._update_operation_tool_fields(conn, operation, fields)
            current = self._operation_row_conn(conn, operation_id)
            self._append_operation_event_conn(
                conn,
                str(operation["task_id"]),
                "operation_failed",
                current,
                "failed",
                reason,
            )
            conn.execute(
                "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'tool_failed', ?, ?)",
                (
                    operation["task_id"],
                    _checked_json(
                        {
                            "tool_use_id": operation["tool_use_id"],
                            "operation_id": operation_id,
                            "reason": _safe_reason(reason),
                        },
                        MAX_EVENT_PAYLOAD_BYTES,
                        "event payload",
                    ),
                    now,
                ),
            )
        return self.get_operation(operation_id)  # type: ignore[return-value]

    def mark_operation_unknown(
        self,
        operation_id: str,
        reason: str,
        *,
        evidence: ReconcileEvidence | dict[str, Any] | None = None,
        tool_fields: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        now = _now()
        evidence_payload = self._evidence_payload(evidence)
        with self.transaction() as conn:
            operation = self._operation_row_conn(conn, operation_id)
            self._operation_lease_conn(conn, str(operation["task_id"]))
            if operation["state"] == "unknown":
                return self._decode_operation(operation)  # type: ignore[return-value]
            if operation["state"] != "dispatched":
                raise StaleState(f"Operation cannot become unknown from {operation['state']}: {operation_id}")
            version = int(operation["version"])
            if expected_version is not None and version != int(expected_version):
                raise StaleState(f"Operation version changed before unknown transition: {operation_id}")
            updated = conn.execute(
                """
                UPDATE operations SET state = 'unknown', probe_evidence_json = ?,
                    updated_at = ?, version = version + 1
                WHERE operation_id = ? AND state = 'dispatched' AND version = ?
                """,
                (
                    _json(evidence_payload or {"reason": _safe_reason(reason)}),
                    now,
                    operation_id,
                    version,
                ),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Operation changed during unknown transition: {operation_id}")
            conn.execute(
                "UPDATE operation_outbox SET state = 'blocked', claimed_by = NULL, claimed_until = NULL, "
                "last_error = ?, updated_at = ? WHERE operation_id = ?",
                (_safe_reason(reason), now, operation_id),
            )
            conn.execute(
                "UPDATE effect_reservations SET state = 'unknown', finished_at = ?, details_json = ? "
                "WHERE operation_id = ? AND state = 'running'",
                (now, _json(evidence_payload or {"reason": _safe_reason(reason)}), operation_id),
            )
            projection = dict(tool_fields or {})
            projection.setdefault("status", "needs_review")
            projection.setdefault("error", _safe_reason(reason))
            projection.setdefault("finished_at", now)
            projection.setdefault("execution_status", "unknown")
            self._update_operation_tool_fields(conn, operation, projection)
            current = self._operation_row_conn(conn, operation_id)
            self._append_operation_event_conn(
                conn,
                str(operation["task_id"]),
                "operation_unknown",
                current,
                "unknown",
                reason,
            )
        return self.get_operation(operation_id)  # type: ignore[return-value]

    def resolve_operation(
        self,
        operation_id: str,
        action: str,
        evidence: ReconcileEvidence | dict[str, Any] | None = None,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if action not in {"complete", "retry", "abort"}:
            raise ValueError("operation resolution action must be complete, retry, or abort")
        evidence_payload = self._evidence_payload(evidence)
        now = _now()
        with self.transaction() as conn:
            operation = self._operation_row_conn(conn, operation_id)
            owner_id, fencing_token = self._operation_lease_conn(conn, str(operation["task_id"]))
            task_row = conn.execute(
                "SELECT checkpoint_id, status, version FROM tasks WHERE task_id = ?",
                (operation["task_id"],),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"Task not found: {operation['task_id']}")
            operation_state = str(operation["state"])
            if action == "complete" and operation_state != "unknown":
                raise StaleState(f"Only unknown operations can be completed during resolution: {operation_id}")
            if action in {"retry", "abort"} and operation_state not in {"unknown", "prepared"}:
                raise StaleState(f"Operation cannot be {action} from {operation_state}: {operation_id}")
            version = int(operation["version"])
            if expected_version is not None and version != int(expected_version):
                raise StaleState(f"Operation version changed before resolution: {operation_id}")
            if action == "retry":
                outcome = str((evidence_payload or {}).get("outcome", ""))
                if outcome not in {"not_happened", "safe_to_retry", "before_hash"}:
                    raise ValueError("retry requires evidence that the external effect did not happen")
                conn.execute(
                    "UPDATE effect_reservations SET state = 'cancelled', finished_at = ?, details_json = ? "
                    "WHERE operation_id = ? AND state IN ('running', 'unknown')",
                    (now, _json(evidence_payload or {"reason": "manual_retry"}), operation_id),
                )
                conn.execute(
                    """
                    INSERT INTO effect_reservations(
                        task_id, tool_use_id, operation_id, owner_id, owner_pid,
                        fencing_token, effect, state, started_at, deadline_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        operation["task_id"],
                        operation["tool_use_id"],
                        operation_id,
                        owner_id,
                        os.getpid(),
                        fencing_token,
                        "file_write" if operation["adapter"] == "file" else "unknown_write",
                        now,
                        now + 300.0,
                    ),
                )
                updated = conn.execute(
                    """
                    UPDATE operations SET state = 'prepared', probe_evidence_json = ?,
                        updated_at = ?, version = version + 1
                    WHERE operation_id = ? AND state = ? AND version = ?
                    """,
                    (_json(evidence_payload or {}), now, operation_id, operation_state, version),
                )
                if updated.rowcount != 1:
                    raise StaleState(f"Operation changed during retry: {operation_id}")
                conn.execute(
                    "UPDATE operation_outbox SET state = 'pending', available_at = ?, claimed_by = NULL, "
                    "claimed_until = NULL, last_error = NULL, updated_at = ? WHERE operation_id = ?",
                    (now, now, operation_id),
                )
                conn.execute(
                    "UPDATE tool_calls SET status = 'planned', error = NULL, execution_status = NULL, "
                    "finished_at = NULL, version = version + 1 WHERE task_id = ? AND tool_use_id = ? "
                    "AND status IN ('running', 'needs_review')",
                    (operation["task_id"], operation["tool_use_id"]),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'running', last_error = NULL, updated_at = ?, version = version + 1 "
                    "WHERE task_id = ? AND status = 'needs_review'",
                    (now, operation["task_id"]),
                )
                conn.execute(
                    "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'review_resolved', ?, ?)",
                    (
                        operation["task_id"],
                        _json({
                            "tool_use_id": operation["tool_use_id"],
                            "operation_id": operation_id,
                            "action": action,
                            "checkpoint_id": task_row["checkpoint_id"],
                        }),
                        now,
                    ),
                )
                current = self._operation_row_conn(conn, operation_id)
                self._append_operation_event_conn(
                    conn,
                    str(operation["task_id"]),
                    "operation_reconciled",
                    current,
                    "prepared",
                    "operator authorized a safe retry",
                )
            else:
                target_state = "committed" if action == "complete" else "cancelled"
                manual_output = (
                    "[manually marked complete after reconciliation]"
                    if action == "complete"
                    else None
                )
                manual_result_json = (
                    _checked_json(manual_output, MAX_CHECKPOINT_BYTES, "operation result")
                    if manual_output is not None
                    else None
                )
                manual_result_digest = sha256_json(manual_output) if manual_output is not None else None
                updated = conn.execute(
                    "UPDATE operations SET state = ?, result_json = ?, result_digest = ?, "
                    "probe_evidence_json = ?, completed_at = ?, "
                    "updated_at = ?, version = version + 1 WHERE operation_id = ? AND state = ? AND version = ?",
                    (
                        target_state,
                        manual_result_json,
                        manual_result_digest,
                        _json(evidence_payload or {}),
                        now,
                        now,
                        operation_id,
                        operation_state,
                        version,
                    ),
                )
                if updated.rowcount != 1:
                    raise StaleState(f"Operation changed during resolution: {operation_id}")
                outbox_state = "delivered" if action == "complete" else "cancelled"
                conn.execute(
                    "UPDATE operation_outbox SET state = ?, claimed_by = NULL, claimed_until = NULL, "
                    "last_error = NULL, updated_at = ? WHERE operation_id = ?",
                    (outbox_state, now, operation_id),
                )
                reservation_state = "completed" if action == "complete" else "cancelled"
                conn.execute(
                    "UPDATE effect_reservations SET state = ?, finished_at = ?, details_json = ? "
                    "WHERE operation_id = ? AND state IN ('running', 'unknown')",
                    (reservation_state, now, _json(evidence_payload or {}), operation_id),
                )
                if action == "complete":
                    self._update_operation_tool_fields(
                        conn,
                        operation,
                        {
                            "status": "succeeded",
                            "output": manual_output,
                            "finished_at": now,
                            "effect_confirmed": 1,
                        },
                        statuses=("running", "needs_review", "planned"),
                    )
                else:
                    self._update_operation_tool_fields(
                        conn,
                        operation,
                        {
                            "status": "aborted",
                            "output": "Aborted during review.",
                            "finished_at": now,
                            "error": "Operator aborted the operation.",
                        },
                        statuses=("running", "needs_review", "planned"),
                    )
                conn.execute(
                    "UPDATE tasks SET status = 'running', last_error = NULL, updated_at = ?, version = version + 1 "
                    "WHERE task_id = ? AND status = 'needs_review'",
                    (now, operation["task_id"]),
                )
                conn.execute(
                    "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, 'review_resolved', ?, ?)",
                    (
                        operation["task_id"],
                        _json({
                            "tool_use_id": operation["tool_use_id"],
                            "operation_id": operation_id,
                            "action": action,
                            "checkpoint_id": task_row["checkpoint_id"],
                        }),
                        now,
                    ),
                )
                current = self._operation_row_conn(conn, operation_id)
                self._append_operation_event_conn(
                    conn,
                    str(operation["task_id"]),
                    "operation_reconciled",
                    current,
                    target_state,
                    "operator completed reconciliation" if action == "complete" else "operator aborted operation",
                )
        return self.get_operation(operation_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Background jobs + cron (Phase 2)
    # ------------------------------------------------------------------

    def _emit_conn(self, conn: sqlite3.Connection, task_id: str, event_type: str,
                   payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO events(task_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (
                task_id,
                event_type,
                _checked_json(payload, MAX_EVENT_PAYLOAD_BYTES, "event payload"),
                _now(),
            ),
        )

    def _job_lease_conn(self, conn: sqlite3.Connection, job_id: str) -> tuple[str, int]:
        if self._lease_context is None:
            raise LeaseLost("Job transition requires a bound lease")
        self._assert_lease_conn(conn)
        repo_root, owner_id, fencing_token = self._lease_context
        job = conn.execute("SELECT repo_root FROM agent_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(f"Job not found: {job_id}")
        if str(job["repo_root"]) != repo_root:
            raise LeaseLost("Job repository does not match the bound lease")
        return owner_id, int(fencing_token)

    def enqueue_job(self, job_id: str, task_id: str, repo_root: str, kind: str,
                    payload: dict[str, Any], *, lane_id: str | None = None,
                    max_attempts: int = 1, available_at: float | None = None) -> dict[str, Any]:
        now = _now()
        payload_json = _checked_json(payload, MAX_EVENT_PAYLOAD_BYTES, "job payload")
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO agent_jobs(
                    job_id, task_id, repo_root, lane_id, kind, payload_json, status,
                    attempts, max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """,
                (
                    job_id, task_id, repo_root, lane_id, kind, payload_json,
                    max(1, int(max_attempts)), available_at if available_at is not None else now,
                    now, now,
                ),
            )
            self._emit_conn(conn, task_id, "job_enqueued", {
                "job_id": job_id, "kind": kind, "status": "pending",
            })
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,))
        if row is None:
            return None
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json"), {})
        return item

    def list_jobs(self, task_id: str | None = None,
                  status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetchall(
            f"SELECT * FROM agent_jobs{where} ORDER BY created_at, job_id", tuple(params)
        )
        return [dict(row) for row in rows]

    def claim_job(self, job_id: str, *, ttl: float = 30.0) -> dict[str, Any]:
        now = _now()
        with self.transaction() as conn:
            job = conn.execute("SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"Job not found: {job_id}")
            owner_id, _ = self._job_lease_conn(conn, job_id)
            if job["status"] not in {"pending", "retryable"}:
                raise StaleState(f"Job is not claimable: {job_id} ({job['status']})")
            if job["available_at"] is not None and float(job["available_at"]) > now:
                raise StaleState(f"Job is not yet available: {job_id}")
            attempts = int(job["attempts"])
            if attempts >= int(job["max_attempts"]):
                raise StaleState(f"Job has exhausted its attempts: {job_id}")
            new_fencing = attempts + 1
            updated = conn.execute(
                "UPDATE agent_jobs SET status = 'claimed', attempts = ?, updated_at = ? "
                "WHERE job_id = ? AND status IN ('pending', 'retryable') AND attempts = ?",
                (new_fencing, now, job_id, attempts),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Job changed during claim: {job_id}")
            run_id = "run_" + _sha256_text(f"{job_id}\0{new_fencing}\0{now}")[:40]
            conn.execute(
                """
                INSERT INTO job_runs(
                    run_id, job_id, owner_id, fencing_token, status,
                    heartbeat_at, lease_expires_at, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (run_id, job_id, owner_id, new_fencing, now, now + ttl, now),
            )
            self._emit_conn(conn, str(job["task_id"]), "job_claimed", {
                "job_id": job_id, "run_id": run_id, "status": "claimed",
            })
            return {"job_id": job_id, "run_id": run_id, "owner_id": owner_id,
                    "fencing_token": new_fencing, "status": "running"}

    def heartbeat_job(self, run_id: str, *, ttl: float = 30.0) -> bool:
        now = _now()
        with self.transaction(guard=False) as conn:
            run = conn.execute("SELECT * FROM job_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Job run not found: {run_id}")
            owner_id, fencing = self._job_lease_conn(conn, str(run["job_id"]))
            if run["owner_id"] != owner_id or int(run["fencing_token"]) != fencing:
                raise LeaseLost(f"Stale job owner for run: {run_id}")
            if run["status"] != "running":
                raise StaleState(f"Job run is not running: {run_id}")
            conn.execute(
                "UPDATE agent_jobs SET status = 'running', updated_at = ? "
                "WHERE job_id = ? AND status = 'claimed'",
                (now, run["job_id"]),
            )
            updated = conn.execute(
                "UPDATE job_runs SET heartbeat_at = ?, lease_expires_at = ? "
                "WHERE run_id = ? AND status = 'running' AND owner_id = ? AND fencing_token = ?",
                (now, now + ttl, run_id, owner_id, int(fencing)),
            )
            return updated.rowcount == 1

    def complete_job(self, run_id: str, *, result_digest: str | None = None) -> None:
        now = _now()
        with self.transaction(guard=False) as conn:
            run = conn.execute("SELECT * FROM job_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Job run not found: {run_id}")
            job = conn.execute("SELECT * FROM agent_jobs WHERE job_id = ?", (run["job_id"],)).fetchone()
            if job is None:
                raise KeyError(f"Job not found: {run['job_id']}")
            owner_id, fencing = self._job_lease_conn(conn, str(run["job_id"]))
            if run["owner_id"] != owner_id or int(run["fencing_token"]) != fencing:
                raise LeaseLost(f"Stale job owner for run: {run_id}")
            updated = conn.execute(
                "UPDATE job_runs SET status = 'completed', completed_at = ?, error = NULL, "
                "result_digest = ? WHERE run_id = ? AND status = 'running' AND owner_id = ? "
                "AND fencing_token = ?",
                (now, result_digest, run_id, owner_id, int(fencing)),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Job run changed before completion: {run_id}")
            conn.execute(
                "UPDATE agent_jobs SET status = 'completed', updated_at = ? WHERE job_id = ?",
                (now, run["job_id"]),
            )
            self._emit_conn(conn, str(job["task_id"]), "job_completed", {
                "job_id": str(run["job_id"]), "run_id": run_id, "status": "completed",
            })

    def fail_job(self, run_id: str, *, error: str | None = None) -> None:
        now = _now()
        with self.transaction(guard=False) as conn:
            run = conn.execute("SELECT * FROM job_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Job run not found: {run_id}")
            job = conn.execute("SELECT * FROM agent_jobs WHERE job_id = ?", (run["job_id"],)).fetchone()
            if job is None:
                raise KeyError(f"Job not found: {run['job_id']}")
            owner_id, fencing = self._job_lease_conn(conn, str(run["job_id"]))
            if run["owner_id"] != owner_id or int(run["fencing_token"]) != fencing:
                raise LeaseLost(f"Stale job owner for run: {run_id}")
            updated = conn.execute(
                "UPDATE job_runs SET status = 'failed', completed_at = ?, error = ? "
                "WHERE run_id = ? AND status = 'running' AND owner_id = ? AND fencing_token = ?",
                (now, _safe_reason(error) if error else None, run_id, owner_id, int(fencing)),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Job run changed before failure: {run_id}")
            attempts = int(job["attempts"])
            new_status = "retryable" if attempts < int(job["max_attempts"]) else "failed"
            conn.execute(
                "UPDATE agent_jobs SET status = ?, available_at = ?, updated_at = ? WHERE job_id = ?",
                (new_status, now, now, run["job_id"]),
            )
            self._emit_conn(conn, str(job["task_id"]), "job_failed", {
                "job_id": str(run["job_id"]), "run_id": run_id, "status": new_status,
            })

    def recover_jobs(self, *, now: float | None = None) -> list[dict[str, Any]]:
        now = _now() if now is None else now
        recovered: list[dict[str, Any]] = []
        with self.transaction(guard=False) as conn:
            stale = conn.execute(
                """
                SELECT r.*, j.task_id, j.attempts, j.max_attempts
                FROM job_runs r JOIN agent_jobs j ON j.job_id = r.job_id
                WHERE r.status = 'running' AND r.lease_expires_at IS NOT NULL
                    AND r.lease_expires_at <= ?
                """,
                (now,),
            ).fetchall()
            for run in stale:
                attempts = int(run["attempts"])
                new_status = "retryable" if attempts < int(run["max_attempts"]) else "failed"
                conn.execute(
                    "UPDATE job_runs SET status = 'failed', completed_at = ?, error = ? "
                    "WHERE run_id = ? AND status = 'running'",
                    (now, "owner_stale_lease_expired", run["run_id"]),
                )
                conn.execute(
                    "UPDATE agent_jobs SET status = ?, available_at = ?, updated_at = ? "
                    "WHERE job_id = ?",
                    (new_status, now, now, run["job_id"]),
                )
                self._emit_conn(conn, str(run["task_id"]), "job_recovered", {
                    "job_id": str(run["job_id"]), "run_id": str(run["run_id"]),
                    "new_status": new_status,
                })
                recovered.append({
                    "job_id": str(run["job_id"]), "run_id": str(run["run_id"]),
                    "new_status": new_status,
                })
        return recovered

    def list_job_runs(self, job_id: str | None = None) -> list[dict[str, Any]]:
        if job_id is not None:
            rows = self._fetchall(
                "SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_at, run_id", (job_id,)
            )
        else:
            rows = self._fetchall("SELECT * FROM job_runs ORDER BY started_at, run_id")
        return [dict(row) for row in rows]

    def cancel_job(self, job_id: str, *, reason: str | None = None) -> None:
        now = _now()
        with self.transaction(guard=False) as conn:
            job = conn.execute("SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"Job not found: {job_id}")
            if job["status"] in {"completed", "failed", "cancelled"}:
                raise StaleState(f"Job is already terminal: {job_id}")
            if job["status"] in {"claimed", "running"}:
                owner_id, fencing = self._job_lease_conn(conn, job_id)
                updated = conn.execute(
                    "UPDATE job_runs SET status = 'cancelled', completed_at = ? "
                    "WHERE job_id = ? AND status = 'running' AND owner_id = ? AND fencing_token = ?",
                    (now, job_id, owner_id, int(fencing)),
                )
                if updated.rowcount == 0:
                    raise LeaseLost(f"Stale owner for job: {job_id}")
            conn.execute(
                "UPDATE agent_jobs SET status = 'cancelled', updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            self._emit_conn(conn, str(job["task_id"]), "job_cancelled", {
                "job_id": job_id, "reason": _safe_reason(reason) if reason else None,
            })

    def create_cron_schedule(self, schedule_id: str, task_id: str, expression: str,
                             job_kind: str, payload: dict[str, Any], *,
                             enabled: bool = True,
                             next_trigger_at: float | None = None) -> dict[str, Any]:
        now = _now()
        payload_json = _checked_json(payload, MAX_EVENT_PAYLOAD_BYTES, "cron payload")
        with self.transaction(guard=False) as conn:
            conn.execute(
                """
                INSERT INTO cron_schedules(
                    schedule_id, task_id, expression, job_kind, payload_json, enabled,
                    last_triggered_at, next_trigger_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    schedule_id, task_id, expression, job_kind, payload_json,
                    1 if enabled else 0,
                    next_trigger_at if next_trigger_at is not None else now + _cron_interval_seconds(expression),
                ),
            )
        row = self._fetchone("SELECT * FROM cron_schedules WHERE schedule_id = ?", (schedule_id,))
        item = dict(row)  # type: ignore[arg-type]
        item["payload"] = _loads(item.pop("payload_json"), {})
        return item

    def list_cron_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        if enabled_only:
            rows = self._fetchall("SELECT * FROM cron_schedules WHERE enabled = 1")
        else:
            rows = self._fetchall("SELECT * FROM cron_schedules")
        return [dict(row) for row in rows]

    def delete_cron_schedule(self, schedule_id: str) -> None:
        with self.transaction(guard=False) as conn:
            conn.execute("DELETE FROM cron_schedules WHERE schedule_id = ?", (schedule_id,))

    def trigger_due_cron(self, *, now: float | None = None) -> list[str]:
        now = _now() if now is None else now
        triggered: list[str] = []
        with self.transaction(guard=False) as conn:
            schedules = conn.execute(
                "SELECT c.*, t.repo_root FROM cron_schedules c JOIN tasks t ON t.task_id = c.task_id "
                "WHERE c.enabled = 1 AND c.next_trigger_at IS NOT NULL AND c.next_trigger_at <= ?",
                (now,),
            ).fetchall()
            for schedule in schedules:
                job_id = f"job_{schedule['schedule_id']}_{int(now * 1000)}"
                conn.execute(
                    """
                    INSERT INTO agent_jobs(
                        job_id, task_id, repo_root, lane_id, kind, payload_json, status,
                        attempts, max_attempts, available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, 'pending', 0, 1, ?, ?, ?)
                    """,
                    (
                        job_id, schedule["task_id"], schedule["repo_root"], schedule["job_kind"],
                        schedule["payload_json"], now, now, now,
                    ),
                )
                next_trigger = now + _cron_interval_seconds(str(schedule["expression"]))
                conn.execute(
                    "UPDATE cron_schedules SET last_triggered_at = ?, next_trigger_at = ? "
                    "WHERE schedule_id = ?",
                    (now, next_trigger, schedule["schedule_id"]),
                )
                self._emit_conn(conn, str(schedule["task_id"]), "cron_triggered", {
                    "schedule_id": str(schedule["schedule_id"]), "job_id": job_id,
                    "job_kind": str(schedule["job_kind"]),
                })
                triggered.append(job_id)
        return triggered

    def upsert_tool_registration(
        self,
        *,
        registration_id: str,
        tool_name: str,
        adapter_kind: str,
        schema: dict[str, Any],
        effect_kind: str,
        connection_id: str | None = None,
        server_name: str | None = None,
        source_tool_name: str | None = None,
        description: str = "",
        permission_requirements: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        enabled: bool = True,
        version: int = 1,
    ) -> dict[str, Any]:
        if adapter_kind not in {"builtin", "mcp"}:
            raise ValueError(f"Invalid adapter kind: {adapter_kind!r}")
        if effect_kind not in {"read_only", "file_write", "idempotent", "unknown_write", "opaque"}:
            raise ValueError(f"Invalid effect kind: {effect_kind!r}")
        if timeout_seconds is not None and float(timeout_seconds) <= 0:
            raise ValueError("Tool timeout must be positive")
        now = _now()
        schema_json = _checked_json(schema, MAX_EVENT_PAYLOAD_BYTES, "tool schema")
        permission_json = _checked_json(
            permission_requirements or {},
            MAX_EVENT_PAYLOAD_BYTES,
            "tool permission requirements",
        )
        with self.transaction(guard=False) as conn:
            conn.execute(
                """
                INSERT INTO tool_registrations(
                    registration_id, tool_name, adapter_kind, connection_id, server_name,
                    source_tool_name, description, schema_json,
                    effect_kind, permission_json, timeout_seconds, enabled, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_name) DO UPDATE SET
                    registration_id = excluded.registration_id,
                    adapter_kind = excluded.adapter_kind,
                    connection_id = excluded.connection_id,
                    server_name = excluded.server_name,
                    source_tool_name = excluded.source_tool_name,
                    description = excluded.description,
                    schema_json = excluded.schema_json,
                    effect_kind = excluded.effect_kind,
                    permission_json = excluded.permission_json,
                    timeout_seconds = excluded.timeout_seconds,
                    enabled = excluded.enabled,
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (
                    registration_id,
                    tool_name,
                    adapter_kind,
                    connection_id,
                    server_name,
                    source_tool_name,
                    description,
                    schema_json,
                    effect_kind,
                    permission_json,
                    timeout_seconds,
                    1 if enabled else 0,
                    version,
                    now,
                    now,
                ),
            )
        return self.get_tool_registration(tool_name)  # type: ignore[return-value]

    def get_tool_registration(self, tool_name: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM tool_registrations WHERE tool_name = ?",
            (tool_name,),
        )
        if row is None:
            return None
        item = dict(row)
        item["schema"] = _loads(item.pop("schema_json"), {})
        item["permission_requirements"] = _loads(item.pop("permission_json"), {})
        item["enabled"] = bool(item["enabled"])
        return item

    def list_tool_registrations(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM tool_registrations"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY tool_name"
        rows = self._fetchall(query)
        result = []
        for row in rows:
            item = dict(row)
            item["schema"] = _loads(item.pop("schema_json"), {})
            item["permission_requirements"] = _loads(item.pop("permission_json"), {})
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def delete_tool_registration(self, tool_name: str) -> None:
        with self.transaction(guard=False) as conn:
            conn.execute("DELETE FROM tool_registrations WHERE tool_name = ?", (tool_name,))

    def upsert_mcp_connection(
        self,
        *,
        connection_id: str,
        server_name: str,
        endpoint: str,
        transport: str = "stdio",
        args: list[str] | None = None,
        auth_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if transport != "stdio":
            raise ValueError(f"Unsupported MCP transport: {transport!r}")
        now = _now()
        args_json = _checked_json(args or [], MAX_EVENT_PAYLOAD_BYTES, "MCP args")
        auth_profile_json = _checked_json(auth_profile or {}, MAX_EVENT_PAYLOAD_BYTES, "MCP auth profile")
        with self.transaction(guard=False) as conn:
            conn.execute(
                """
                INSERT INTO mcp_connections(
                    connection_id, server_name, transport, endpoint, args_json,
                    auth_profile_json, status, last_connected_at, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'configured', NULL, NULL, ?, ?)
                ON CONFLICT(server_name) DO UPDATE SET
                    connection_id = excluded.connection_id,
                    transport = excluded.transport,
                    endpoint = excluded.endpoint,
                    args_json = excluded.args_json,
                    auth_profile_json = excluded.auth_profile_json,
                    updated_at = excluded.updated_at
                """,
                (
                    connection_id,
                    server_name,
                    transport,
                    endpoint,
                    args_json,
                    auth_profile_json,
                    now,
                    now,
                ),
            )
        return self.get_mcp_connection(connection_id)  # type: ignore[return-value]

    def get_mcp_connection(self, connection_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM mcp_connections WHERE connection_id = ?",
            (connection_id,),
        )
        if row is None:
            return None
        item = dict(row)
        item["args"] = _loads(item.pop("args_json"), [])
        item["auth_profile"] = _loads(item.pop("auth_profile_json"), {})
        return item

    def list_mcp_connections(self) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM mcp_connections ORDER BY server_name")
        result = []
        for row in rows:
            item = dict(row)
            item["args"] = _loads(item.pop("args_json"), [])
            item["auth_profile"] = _loads(item.pop("auth_profile_json"), {})
            result.append(item)
        return result

    def update_mcp_connection_status(
        self,
        connection_id: str,
        status: str,
        *,
        last_connected_at: float | None = None,
        last_error: str | None = None,
    ) -> None:
        if status not in {"configured", "connected", "error", "disabled"}:
            raise ValueError(f"Invalid MCP connection status: {status!r}")
        now = _now()
        with self.transaction(guard=False) as conn:
            conn.execute(
                """
                UPDATE mcp_connections
                SET status = ?, last_connected_at = COALESCE(?, last_connected_at),
                    last_error = ?, updated_at = ?
                WHERE connection_id = ?
                """,
                (status, last_connected_at, last_error, now, connection_id),
            )

    # ------------------------------------------------------------------
    # Durable subagents, team mailboxes, and plan approvals (Phase 3)
    # ------------------------------------------------------------------

    _SUBAGENT_RUN_STATUSES = {
        "pending", "running", "completed", "failed", "needs_review", "cancelled",
    }
    _PLAN_APPROVAL_STATUSES = {"requested", "approved", "rejected", "superseded"}
    _MAILBOX_MESSAGE_STATUSES = {"delivered", "read", "archived"}

    @staticmethod
    def _subagent_run_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["messages"] = _loads(item.pop("messages_json"), [])
        return item

    def create_subagent_run(self, run_id: str, parent_task_id: str | None, child_task_id: str,
                            repo_root: str, role: str, owner_id: str,
                            messages: list[dict]) -> dict[str, Any]:
        now = _now()
        messages_json = _checked_json(messages, MAX_CHECKPOINT_BYTES, "subagent messages")
        with self.transaction(guard=False) as conn:
            conn.execute(
                """
                INSERT INTO subagent_runs(
                    subagent_run_id, parent_task_id, child_task_id, repo_root,
                    lane_id, role, status, owner_id, version, messages_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'default', ?, 'pending', ?, 1, ?, ?, ?)
                """,
                (
                    run_id, parent_task_id, child_task_id, repo_root, role,
                    owner_id, messages_json, now, now,
                ),
            )
            self._emit_conn(conn, parent_task_id or child_task_id, "subagent_run_created", {
                "subagent_run_id": run_id,
                "child_task_id": child_task_id,
                "role": role,
                "repo_root": repo_root,
            })
        run = self.get_subagent_run(run_id)
        if run is None:
            raise RuntimeError(f"Subagent run was not persisted: {run_id}")
        return run

    def get_subagent_run(self, run_id: str) -> dict[str, Any] | None:
        return self._subagent_run_dict(
            self._fetchone(
                "SELECT * FROM subagent_runs WHERE subagent_run_id = ?",
                (run_id,),
            )
        )

    def list_subagent_runs(self, parent_task_id: str | None = None,
                           status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM subagent_runs"
        clauses: list[str] = []
        params: list[Any] = []
        if parent_task_id is not None:
            clauses.append("parent_task_id = ?")
            params.append(parent_task_id)
        if status is not None:
            if status not in self._SUBAGENT_RUN_STATUSES:
                raise ValueError(f"Invalid subagent run status: {status!r}")
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, subagent_run_id"
        rows = self._fetchall(query, tuple(params))
        return [item for item in (self._subagent_run_dict(row) for row in rows) if item is not None]

    def claim_subagent_run(self, run_id: str, owner_id: str, fencing_token: str) -> dict[str, Any]:
        now = _now()
        with self.transaction(guard=False) as conn:
            row = conn.execute(
                "SELECT * FROM subagent_runs WHERE subagent_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Subagent run not found: {run_id}")
            if row["status"] not in {"pending", "running"}:
                raise StaleState(
                    f"Subagent run is not claimable: {run_id} status={row['status']!r}"
                )
            if row["fencing_token"] is not None and (
                str(row["fencing_token"]) != str(fencing_token)
                or str(row["owner_id"]) != str(owner_id)
            ):
                lease = conn.execute(
                    "SELECT owner_id, task_id, expires_at FROM leases WHERE repo_root = ?",
                    (row["repo_root"],),
                ).fetchone()
                if (
                    lease is None
                    or str(lease["owner_id"]) != str(owner_id)
                    or str(lease["task_id"]) != str(row["child_task_id"])
                    or float(lease["expires_at"]) <= now
                ):
                    raise StaleState(f"Subagent run is fenced by another owner: {run_id}")
            updated = conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'running', owner_id = ?, fencing_token = ?,
                    updated_at = ?, version = version + 1
                WHERE subagent_run_id = ?
                """,
                (owner_id, str(fencing_token), now, run_id),
            )
            if updated.rowcount != 1:
                raise StaleState(f"Subagent run changed during claim: {run_id}")
            self._emit_conn(conn, str(row["parent_task_id"] or row["child_task_id"]), "subagent_run_claimed", {
                "subagent_run_id": run_id,
                "owner_id": owner_id,
                "fencing_token": str(fencing_token),
            })
        return self.get_subagent_run(run_id)  # type: ignore[return-value]

    def update_subagent_run(self, run_id: str, status: str | None = None,
                            messages: list[dict] | None = None,
                            result_summary: str | None = None,
                            error: str | None = None,
                            expected_version: int | None = None,
                            fencing_token: str | None = None) -> dict[str, Any]:
        if status is not None and status not in self._SUBAGENT_RUN_STATUSES:
            raise ValueError(f"Invalid subagent run status: {status!r}")
        assignments: list[str] = []
        values: list[Any] = []
        now = _now()
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if messages is not None:
            assignments.append("messages_json = ?")
            values.append(_checked_json(messages, MAX_CHECKPOINT_BYTES, "subagent messages"))
        if result_summary is not None:
            assignments.append("result_summary = ?")
            values.append(result_summary)
        if error is not None:
            assignments.append("error = ?")
            values.append(_safe_reason(error))
        assignments.append("updated_at = ?")
        values.append(now)
        assignments.append("version = version + 1")
        where = "subagent_run_id = ?"
        params: list[Any] = []
        params.extend(values)
        params.append(run_id)
        if expected_version is not None:
            where += " AND version = ?"
            params.append(int(expected_version))
        if fencing_token is not None:
            where += " AND fencing_token = ?"
            params.append(str(fencing_token))
        with self.transaction(guard=False) as conn:
            cursor = conn.execute(
                f"UPDATE subagent_runs SET {', '.join(assignments)} WHERE {where}",
                params,
            )
            if cursor.rowcount != 1:
                raise StaleState(f"Subagent run state changed before update: {run_id}")
        return self.get_subagent_run(run_id)  # type: ignore[return-value]

    def complete_subagent_run(self, run_id: str, result_summary: str,
                              messages: list[dict], expected_version: int,
                              fencing_token: str) -> dict[str, Any]:
        now = _now()
        with self.transaction(guard=False) as conn:
            row = conn.execute(
                "SELECT status, parent_task_id, child_task_id FROM subagent_runs WHERE subagent_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Subagent run not found: {run_id}")
            if row["status"] != "running":
                raise StaleState(
                    f"Subagent run is not running: {run_id} status={row['status']!r}"
                )
            cursor = conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'completed', result_summary = ?, messages_json = ?,
                    error = NULL, updated_at = ?, version = version + 1
                WHERE subagent_run_id = ? AND version = ? AND fencing_token = ?
                """,
                (
                    result_summary,
                    _checked_json(messages, MAX_CHECKPOINT_BYTES, "subagent messages"),
                    now, run_id, int(expected_version), str(fencing_token),
                ),
            )
            if cursor.rowcount != 1:
                raise StaleState(f"Subagent run changed during completion: {run_id}")
            self._emit_conn(conn, str(row["parent_task_id"] or row["child_task_id"]), "subagent_run_completed", {
                "subagent_run_id": run_id,
                "result_chars": len(result_summary),
            })
        return self.get_subagent_run(run_id)  # type: ignore[return-value]

    def fail_subagent_run(self, run_id: str, error: str, expected_version: int,
                          fencing_token: str) -> dict[str, Any]:
        now = _now()
        with self.transaction(guard=False) as conn:
            row = conn.execute(
                "SELECT status, parent_task_id, child_task_id FROM subagent_runs WHERE subagent_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Subagent run not found: {run_id}")
            if row["status"] not in {"pending", "running"}:
                raise StaleState(
                    f"Subagent run cannot fail: {run_id} status={row['status']!r}"
                )
            cursor = conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'failed', error = ?, updated_at = ?, version = version + 1
                WHERE subagent_run_id = ? AND version = ? AND fencing_token = ?
                """,
                (_safe_reason(error), now, run_id, int(expected_version), str(fencing_token)),
            )
            if cursor.rowcount != 1:
                raise StaleState(f"Subagent run changed before failure: {run_id}")
            self._emit_conn(conn, str(row["parent_task_id"] or row["child_task_id"]), "subagent_run_failed", {
                "subagent_run_id": run_id,
                "error": _safe_reason(error),
            })
        return self.get_subagent_run(run_id)  # type: ignore[return-value]

    def cancel_subagent_run(self, run_id: str, reason: str,
                            expected_version: int, fencing_token: str) -> dict[str, Any]:
        now = _now()
        with self.transaction(guard=False) as conn:
            row = conn.execute(
                "SELECT status, parent_task_id, child_task_id FROM subagent_runs WHERE subagent_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Subagent run not found: {run_id}")
            if row["status"] not in {"pending", "running", "needs_review"}:
                raise StaleState(
                    f"Subagent run cannot be cancelled: {run_id} status={row['status']!r}"
                )
            cursor = conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'cancelled', error = ?, updated_at = ?, version = version + 1
                WHERE subagent_run_id = ? AND version = ? AND fencing_token = ?
                """,
                (_safe_reason(reason), now, run_id, int(expected_version), str(fencing_token)),
            )
            if cursor.rowcount != 1:
                raise StaleState(f"Subagent run changed before cancellation: {run_id}")
            self._emit_conn(conn, str(row["parent_task_id"] or row["child_task_id"]), "subagent_run_cancelled", {
                "subagent_run_id": run_id,
                "reason": _safe_reason(reason),
            })
        return self.get_subagent_run(run_id)  # type: ignore[return-value]

    def mark_subagent_needs_review(self, run_id: str, reason: str,
                                   expected_version: int, fencing_token: str) -> dict[str, Any]:
        now = _now()
        with self.transaction(guard=False) as conn:
            row = conn.execute(
                "SELECT status, parent_task_id, child_task_id FROM subagent_runs WHERE subagent_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Subagent run not found: {run_id}")
            if row["status"] != "running":
                raise StaleState(
                    f"Subagent run cannot enter review: {run_id} status={row['status']!r}"
                )
            cursor = conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'needs_review', error = ?, updated_at = ?,
                    version = version + 1
                WHERE subagent_run_id = ? AND version = ? AND fencing_token = ?
                """,
                (_safe_reason(reason), now, run_id, int(expected_version), str(fencing_token)),
            )
            if cursor.rowcount != 1:
                raise StaleState(f"Subagent run changed before review: {run_id}")
            self._emit_conn(conn, str(row["parent_task_id"] or row["child_task_id"]), "subagent_run_needs_review", {
                "subagent_run_id": run_id,
                "reason": _safe_reason(reason),
            })
        return self.get_subagent_run(run_id)  # type: ignore[return-value]

    def ensure_mailbox(self, owner_task_id: str, owner_role: str,
                       mailbox_id: str | None = None) -> dict[str, Any]:
        mailbox_id = mailbox_id or f"mailbox-{owner_task_id}"
        now = _now()
        with self.transaction(guard=False) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO mailboxes(
                    mailbox_id, owner_task_id, owner_role, version, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (mailbox_id, owner_task_id, owner_role, now, now),
            )
        return self.get_mailbox(owner_task_id)  # type: ignore[return-value]

    def get_mailbox(self, owner_task_id: str) -> dict[str, Any] | None:
        return self._row(
            self._fetchone(
                "SELECT * FROM mailboxes WHERE owner_task_id = ?",
                (owner_task_id,),
            )
        )

    def get_mailbox_by_id(self, mailbox_id: str) -> dict[str, Any] | None:
        return self._row(
            self._fetchone(
                "SELECT * FROM mailboxes WHERE mailbox_id = ?",
                (mailbox_id,),
            )
        )

    def send_mailbox_message(self, message_id: str, mailbox_id: str,
                             sender_task_id: str, recipient_task_id: str,
                             payload: dict[str, Any]) -> dict[str, Any]:
        payload_json = _checked_json(payload, MAX_EVENT_PAYLOAD_BYTES, "mailbox payload")
        now = _now()
        with self.transaction(guard=False) as conn:
            mailbox = conn.execute(
                "SELECT mailbox_id FROM mailboxes WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchone()
            if mailbox is None:
                raise KeyError(f"Mailbox not found: {mailbox_id}")
            existing = conn.execute(
                "SELECT payload_json FROM mailbox_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise StaleState(f"Mailbox message id reused with different payload: {message_id}")
                return self.get_mailbox_message(message_id)  # type: ignore[return-value]
            conn.execute(
                """
                INSERT INTO mailbox_messages(
                    message_id, mailbox_id, sender_task_id, recipient_task_id,
                    payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'delivered', ?)
                """,
                (message_id, mailbox_id, sender_task_id, recipient_task_id, payload_json, now),
            )
            conn.execute(
                "UPDATE mailboxes SET updated_at = ?, version = version + 1 WHERE mailbox_id = ?",
                (now, mailbox_id),
            )
            self._emit_conn(conn, sender_task_id, "mailbox_message_sent", {
                "message_id": message_id,
                "mailbox_id": mailbox_id,
                "recipient_task_id": recipient_task_id,
            })
        return self.get_mailbox_message(message_id)  # type: ignore[return-value]

    @staticmethod
    def _mailbox_message_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json"), {})
        return item

    def get_mailbox_message(self, message_id: str) -> dict[str, Any] | None:
        return self._mailbox_message_dict(
            self._fetchone(
                "SELECT * FROM mailbox_messages WHERE message_id = ?",
                (message_id,),
            )
        )

    def list_mailbox_messages(self, mailbox_id: str,
                              status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM mailbox_messages WHERE mailbox_id = ?"
        params: list[Any] = [mailbox_id]
        if status is not None:
            if status not in self._MAILBOX_MESSAGE_STATUSES:
                raise ValueError(f"Invalid mailbox message status: {status!r}")
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at, message_id"
        rows = self._fetchall(query, tuple(params))
        return [
            item for item in (self._mailbox_message_dict(row) for row in rows) if item is not None
        ]

    def read_mailbox_messages(self, mailbox_id: str, recipient_task_id: str,
                              limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("Mailbox read limit must be positive")
        now = _now()
        with self.transaction(guard=False) as conn:
            mailbox = conn.execute(
                "SELECT mailbox_id FROM mailboxes WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchone()
            if mailbox is None:
                raise KeyError(f"Mailbox not found: {mailbox_id}")
            rows = conn.execute(
                """
                SELECT * FROM mailbox_messages
                WHERE mailbox_id = ? AND recipient_task_id = ? AND status = 'delivered'
                ORDER BY created_at, message_id LIMIT ?
                """,
                (mailbox_id, recipient_task_id, int(limit)),
            ).fetchall()
            message_ids = [str(row["message_id"]) for row in rows]
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                conn.execute(
                    f"""
                    UPDATE mailbox_messages
                    SET status = 'read', read_at = ?
                    WHERE message_id IN ({placeholders}) AND status = 'delivered'
                    """,
                    [now, *message_ids],
                )
                conn.execute(
                    "UPDATE mailboxes SET updated_at = ?, version = version + 1 WHERE mailbox_id = ?",
                    (now, mailbox_id),
                )
                rows = conn.execute(
                    f"""
                    SELECT * FROM mailbox_messages
                    WHERE message_id IN ({placeholders})
                    ORDER BY created_at, message_id
                    """,
                    tuple(message_ids),
                ).fetchall()
            return [
                item for item in (
                    self._mailbox_message_dict(row) for row in rows
                ) if item is not None
            ]

    def archive_mailbox_message(self, message_id: str, expected_version: int) -> dict[str, Any]:
        now = _now()
        with self.transaction(guard=False) as conn:
            message = conn.execute(
                "SELECT mailbox_id, status FROM mailbox_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if message is None:
                raise KeyError(f"Mailbox message not found: {message_id}")
            if message["status"] != "read":
                raise StaleState(
                    f"Mailbox message is not readable: {message_id} status={message['status']!r}"
                )
            mailbox = conn.execute(
                "SELECT version FROM mailboxes WHERE mailbox_id = ?",
                (message["mailbox_id"],),
            ).fetchone()
            if mailbox is None or int(mailbox["version"]) != int(expected_version):
                raise StaleState(f"Mailbox version changed before archive: {message_id}")
            cursor = conn.execute(
                """
                UPDATE mailbox_messages
                SET status = 'archived'
                WHERE message_id = ? AND status = 'read'
                """,
                (message_id,),
            )
            if cursor.rowcount != 1:
                raise StaleState(f"Mailbox message changed during archive: {message_id}")
            conn.execute(
                "UPDATE mailboxes SET updated_at = ?, version = version + 1 WHERE mailbox_id = ?",
                (now, message["mailbox_id"]),
            )
        return self.get_mailbox_message(message_id)  # type: ignore[return-value]

    def create_plan_approval(self, approval_id: str, subagent_run_id: str,
                             plan_hash: str, requested_by: str) -> dict[str, Any]:
        now = _now()
        with self.transaction(guard=False) as conn:
            run = conn.execute(
                "SELECT parent_task_id, child_task_id FROM subagent_runs WHERE subagent_run_id = ?",
                (subagent_run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"Subagent run not found: {subagent_run_id}")
            conn.execute(
                """
                INSERT INTO plan_approvals(
                    approval_id, subagent_run_id, plan_hash, status, requested_by,
                    version, created_at, updated_at
                ) VALUES (?, ?, ?, 'requested', ?, 1, ?, ?)
                """,
                (approval_id, subagent_run_id, plan_hash, requested_by, now, now),
            )
            self._emit_conn(conn, str(run["parent_task_id"] or run["child_task_id"]), "plan_approval_requested", {
                "approval_id": approval_id,
                "subagent_run_id": subagent_run_id,
                "plan_hash": plan_hash,
                "requested_by": requested_by,
            })
        return self.get_plan_approval(approval_id)  # type: ignore[return-value]

    @staticmethod
    def _plan_approval_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_plan_approval(self, approval_id: str) -> dict[str, Any] | None:
        return self._plan_approval_dict(
            self._fetchone(
                "SELECT * FROM plan_approvals WHERE approval_id = ?",
                (approval_id,),
            )
        )

    def list_plan_approvals(self, subagent_run_id: str | None = None,
                            status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM plan_approvals"
        clauses: list[str] = []
        params: list[Any] = []
        if subagent_run_id is not None:
            clauses.append("subagent_run_id = ?")
            params.append(subagent_run_id)
        if status is not None:
            if status not in self._PLAN_APPROVAL_STATUSES:
                raise ValueError(f"Invalid plan approval status: {status!r}")
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, approval_id"
        rows = self._fetchall(query, tuple(params))
        return [
            item for item in (self._plan_approval_dict(row) for row in rows) if item is not None
        ]

    def transition_plan_approval(self, approval_id: str, new_status: str,
                                 decided_by: str | None = None,
                                 reason: str | None = None,
                                 expected_version: int | None = None) -> dict[str, Any]:
        if new_status not in {"approved", "rejected", "superseded"}:
            raise ValueError(f"Invalid plan approval transition: {new_status!r}")
        now = _now()
        with self.transaction(guard=False) as conn:
            row = conn.execute(
                "SELECT * FROM plan_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Plan approval not found: {approval_id}")
            run = conn.execute(
                "SELECT parent_task_id, child_task_id FROM subagent_runs WHERE subagent_run_id = ?",
                (row["subagent_run_id"],),
            ).fetchone()
            if run is None:
                raise KeyError(f"Subagent run not found: {row['subagent_run_id']}")
            if row["status"] != "requested":
                raise StaleState(
                    f"Plan approval is not actionable: {approval_id} status={row['status']!r}"
                )
            if expected_version is not None and int(row["version"]) != int(expected_version):
                raise StaleState(f"Plan approval state changed before transition: {approval_id}")
            cursor = conn.execute(
                """
                UPDATE plan_approvals
                SET status = ?, decided_by = ?, reason = ?, updated_at = ?,
                    version = version + 1
                WHERE approval_id = ? AND status = 'requested'
                """,
                (new_status, decided_by, reason, now, approval_id),
            )
            if cursor.rowcount != 1:
                raise StaleState(f"Plan approval changed during transition: {approval_id}")
            if new_status in {"approved", "rejected"}:
                conn.execute(
                    """
                    UPDATE plan_approvals
                    SET status = 'superseded', updated_at = ?, version = version + 1
                    WHERE subagent_run_id = ? AND approval_id != ? AND status = 'requested'
                    """,
                    (now, row["subagent_run_id"], approval_id),
                )
            self._emit_conn(conn, str(run["parent_task_id"] or run["child_task_id"]), "plan_approval_decided", {
                "approval_id": approval_id,
                "status": new_status,
                "decided_by": decided_by,
            })
        return self.get_plan_approval(approval_id)  # type: ignore[return-value]

    def scan_invariants(self, task_id: str | None = None) -> list[str]:
        """Return durable state inconsistencies without attempting silent repair."""
        violations: list[str] = []
        conn = self._connect()
        try:
            current = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"]
            if current is None or int(current) != SCHEMA_VERSION:
                violations.append(f"schema version mismatch: {current!r} != {SCHEMA_VERSION}")
            violations.extend(
                _legacy_projection_violations(
                    conn,
                    task_id,
                    allow_prepared_operations=True,
                )
            )
            leases = conn.execute("SELECT * FROM leases").fetchall()
            for lease in leases:
                if not lease["owner_id"] or int(lease["fencing_token"]) < 1:
                    violations.append(f"lease {lease['repo_root']}: invalid owner or fencing token")
            operations = conn.execute(
                "SELECT o.*, b.state AS outbox_state FROM operations o "
                "LEFT JOIN operation_outbox b ON b.operation_id = o.operation_id"
            ).fetchall()
            valid_operation_states = {
                "prepared", "dispatched", "committed", "failed", "unknown", "cancelled"
            }
            valid_outbox_states = {"pending", "claimed", "delivered", "blocked", "cancelled"}
            for operation in operations:
                if operation["state"] not in valid_operation_states:
                    violations.append(f"operation {operation['operation_id']}: invalid state")
                if operation["semantics"] not in {
                    "replay_safe", "idempotent", "reconcilable", "opaque"
                }:
                    violations.append(f"operation {operation['operation_id']}: invalid semantics")
                if operation["version"] < 0 or operation["attempt_count"] < 0:
                    violations.append(f"operation {operation['operation_id']}: invalid version or attempt count")
                if operation["outbox_state"] not in valid_outbox_states:
                    violations.append(f"operation {operation['operation_id']}: missing or invalid outbox")
                expected_outbox = {
                    "committed": "delivered",
                    "unknown": "blocked",
                    "cancelled": "cancelled",
                }.get(operation["state"])
                if expected_outbox and operation["outbox_state"] != expected_outbox:
                    violations.append(
                        f"operation {operation['operation_id']}: {operation['state']} "
                        f"has outbox {operation['outbox_state']}"
                    )
                call = conn.execute(
                    "SELECT operation_id, effect, status FROM tool_calls "
                    "WHERE task_id = ? AND tool_use_id = ?",
                    (operation["task_id"], operation["tool_use_id"]),
                ).fetchone()
                if call is None:
                    violations.append(f"operation {operation['operation_id']}: missing tool call")
                else:
                    if call["operation_id"] != operation["operation_id"]:
                        violations.append(f"operation {operation['operation_id']}: tool call projection mismatch")
                    if call["effect"] == "read_only":
                        violations.append(f"operation {operation['operation_id']}: read-only call has operation")
            self._scan_v6_invariants(conn, task_id, violations)
            self._scan_verified_subtask_invariants(conn, task_id, violations)
            self._scan_job_invariants(conn, task_id, violations)
            self._scan_subagent_invariants(conn, task_id, violations)
        finally:
            conn.close()
        return violations

    def _scan_verified_subtask_invariants(
        self,
        conn: sqlite3.Connection,
        task_id: str | None,
        violations: list[str],
    ) -> None:
        if task_id is None:
            items = conn.execute(
                "SELECT i.*, p.task_id FROM plan_items i "
                "JOIN plans p ON p.plan_id = i.plan_id"
            ).fetchall()
            runs = conn.execute("SELECT * FROM verifier_runs").fetchall()
            semantic_rows = conn.execute("SELECT * FROM semantic_checkpoints").fetchall()
        else:
            items = conn.execute(
                "SELECT i.*, p.task_id FROM plan_items i "
                "JOIN plans p ON p.plan_id = i.plan_id WHERE p.task_id = ?",
                (task_id,),
            ).fetchall()
            runs = conn.execute(
                "SELECT * FROM verifier_runs WHERE task_id = ?", (task_id,)
            ).fetchall()
            semantic_rows = conn.execute(
                "SELECT * FROM semantic_checkpoints WHERE task_id = ?", (task_id,)
            ).fetchall()

        item_by_id = {int(item["plan_item_id"]): item for item in items}
        runs_by_item: dict[int, list[sqlite3.Row]] = {}
        runs_by_id: dict[str, sqlite3.Row] = {}
        for run in runs:
            run_id = str(run["verifier_run_id"])
            runs_by_id[run_id] = run
            item_id = int(run["plan_item_id"])
            runs_by_item.setdefault(item_id, []).append(run)
            if run["status"] not in {"pass", "fail", "uncertain"}:
                violations.append(f"verifier_run {run_id}: invalid status {run['status']!r}")
            if int(run["authoritative"]) not in {0, 1}:
                violations.append(f"verifier_run {run_id}: invalid authoritative flag")
            if int(run["authoritative"]) and run["status"] != "pass":
                violations.append(f"verifier_run {run_id}: non-pass run is authoritative")
            if not int(run["authoritative"]) and run["status"] == "pass":
                violations.append(f"verifier_run {run_id}: pass run is non-authoritative")
            if item_id not in item_by_id:
                violations.append(f"verifier_run {run_id}: missing plan item {item_id}")
                continue
            item = item_by_id[item_id]
            if str(item["task_id"]) != str(run["task_id"]):
                violations.append(f"verifier_run {run_id}: task projection mismatch")
            if str(item["subtask_id"]) != str(run["subtask_id"]):
                violations.append(f"verifier_run {run_id}: subtask projection mismatch")
            if item["verifier_bundle_hash"] != run["verifier_bundle_hash"]:
                violations.append(f"verifier_run {run_id}: verifier bundle hash mismatch")
            try:
                manifest = _loads(run["evidence_manifest_json"], [])
                if sha256_json(manifest) != str(run["evidence_hash"]):
                    violations.append(f"verifier_run {run_id}: evidence hash mismatch")
                if int(run["authoritative"]) and any(
                    not is_valid_sha256(entry.get("sha256", ""))
                    for entry in manifest
                    if isinstance(entry, dict)
                ):
                    violations.append(f"verifier_run {run_id}: invalid authoritative SHA-256")
            except (TypeError, ValueError):
                violations.append(f"verifier_run {run_id}: invalid evidence manifest")
            checkpoint = conn.execute(
                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                (run["execution_checkpoint_id"], run["task_id"]),
            ).fetchone()
            if checkpoint is None:
                violations.append(f"verifier_run {run_id}: missing execution checkpoint")

        semantic_by_run: dict[str, list[sqlite3.Row]] = {}
        for semantic in semantic_rows:
            semantic_id = int(semantic["semantic_checkpoint_id"])
            run_id = str(semantic["verifier_run_id"])
            semantic_by_run.setdefault(run_id, []).append(semantic)
            run = runs_by_id.get(run_id)
            if run is None:
                violations.append(
                    f"verified_subtask_checkpoint {semantic_id}: missing verifier run {run_id}"
                )
                continue
            if not int(run["authoritative"]) or run["status"] != "pass":
                violations.append(
                    f"verified_subtask_checkpoint {semantic_id}: verifier run is not authoritative pass"
                )
            if int(semantic["plan_item_id"]) != int(run["plan_item_id"]):
                violations.append(f"verified_subtask_checkpoint {semantic_id}: plan item mismatch")
            if str(semantic["task_id"]) != str(run["task_id"]):
                violations.append(f"verified_subtask_checkpoint {semantic_id}: task mismatch")
            for field in (
                "subtask_id",
                "execution_checkpoint_id",
                "completion_summary",
                "verifier_id",
                "verifier_version",
                "verification_rule",
                "verifier_bundle_hash",
                "verifier_implementation_hash",
                "evidence_hash",
            ):
                if semantic[field] != run[field]:
                    violations.append(
                        f"verified_subtask_checkpoint {semantic_id}: {field} mismatch"
                    )
            if semantic["evidence_manifest_json"] != run["evidence_manifest_json"]:
                violations.append(
                    f"verified_subtask_checkpoint {semantic_id}: evidence manifest mismatch"
                )
            checkpoint = conn.execute(
                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                (semantic["execution_checkpoint_id"], semantic["task_id"]),
            ).fetchone()
            if checkpoint is None:
                violations.append(
                    f"verified_subtask_checkpoint {semantic_id}: missing execution checkpoint"
                )

        for item_id, item in item_by_id.items():
            bundle_hash = item["verifier_bundle_hash"]
            if not bundle_hash:
                continue
            item_runs = runs_by_item.get(item_id, [])
            authoritative = [
                run for run in item_runs
                if int(run["authoritative"]) and run["status"] == "pass"
            ]
            linked = [
                semantic for semantic in semantic_rows
                if int(semantic["plan_item_id"]) == item_id
            ]
            if item["status"] == "completed":
                if not authoritative:
                    violations.append(
                        f"plan_item {item_id}: completed without an authoritative verifier run"
                    )
                if not linked:
                    violations.append(
                        f"plan_item {item_id}: completed without a verified-subtask checkpoint"
                    )

        for run_id, linked in semantic_by_run.items():
            if len(linked) > 1:
                violations.append(
                    f"verifier_run {run_id}: linked to {len(linked)} verified-subtask checkpoints"
                )

    def _scan_subagent_invariants(self, conn: sqlite3.Connection,
                                  task_id: str | None, violations: list[str]) -> None:
        if task_id is not None:
            runs = conn.execute(
                "SELECT * FROM subagent_runs WHERE parent_task_id = ? OR child_task_id = ?",
                (task_id, task_id),
            ).fetchall()
        else:
            runs = conn.execute("SELECT * FROM subagent_runs").fetchall()
        for run in runs:
            run_id = str(run["subagent_run_id"])
            if run["status"] not in self._SUBAGENT_RUN_STATUSES:
                violations.append(f"subagent_run {run_id}: invalid status {run['status']!r}")
            if int(run["version"]) < 1:
                violations.append(f"subagent_run {run_id}: invalid version {run['version']}")
            if run["status"] == "running" and run["fencing_token"] is None:
                violations.append(f"subagent_run {run_id}: running without fencing token")
            if run["status"] in {"completed", "failed"} and run["fencing_token"] is None:
                violations.append(f"subagent_run {run_id}: {run['status']} without fencing token")
            child = conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?",
                (run["child_task_id"],),
            ).fetchone()
            if child is None:
                violations.append(f"subagent_run {run_id}: missing child task {run['child_task_id']!r}")
            requested = conn.execute(
                "SELECT COUNT(*) AS count FROM plan_approvals "
                "WHERE subagent_run_id = ? AND status = 'requested'",
                (run_id,),
            ).fetchone()["count"]
            if requested and run["status"] != "needs_review":
                violations.append(
                    f"subagent_run {run_id}: requested plan approval while status={run['status']!r}"
                )

        if task_id is not None:
            messages = conn.execute(
                "SELECT m.* FROM mailbox_messages m JOIN mailboxes b "
                "ON b.mailbox_id = m.mailbox_id WHERE b.owner_task_id = ?",
                (task_id,),
            ).fetchall()
        else:
            messages = conn.execute("SELECT * FROM mailbox_messages").fetchall()
        for message in messages:
            message_id = str(message["message_id"])
            if message["status"] not in self._MAILBOX_MESSAGE_STATUSES:
                violations.append(
                    f"mailbox_message {message_id}: invalid status {message['status']!r}"
                )
            if message["status"] == "delivered" and message["read_at"] is not None:
                violations.append(f"mailbox_message {message_id}: delivered with read_at")
            if message["status"] in {"read", "archived"} and message["read_at"] is None:
                violations.append(f"mailbox_message {message_id}: {message['status']} without read_at")

        if task_id is not None:
            approvals = conn.execute(
                "SELECT a.* FROM plan_approvals a JOIN subagent_runs r "
                "ON r.subagent_run_id = a.subagent_run_id "
                "WHERE r.parent_task_id = ? OR r.child_task_id = ?",
                (task_id, task_id),
            ).fetchall()
        else:
            approvals = conn.execute("SELECT * FROM plan_approvals").fetchall()
        for approval in approvals:
            approval_id = str(approval["approval_id"])
            if approval["status"] not in self._PLAN_APPROVAL_STATUSES:
                violations.append(
                    f"plan_approval {approval_id}: invalid status {approval['status']!r}"
                )
            if int(approval["version"]) < 1:
                violations.append(f"plan_approval {approval_id}: invalid version")
            if approval["status"] != "requested" and approval["decided_by"] is None:
                violations.append(
                    f"plan_approval {approval_id}: {approval['status']} without decided_by"
                )

    def _scan_job_invariants(self, conn: sqlite3.Connection, task_id: str | None,
                             violations: list[str]) -> None:
        valid_job_statuses = {
            "pending", "claimed", "running", "completed", "failed", "retryable", "cancelled"
        }
        valid_run_statuses = {"running", "completed", "failed", "cancelled"}
        if task_id is not None:
            jobs = conn.execute(
                "SELECT * FROM agent_jobs WHERE task_id = ?", (task_id,)
            ).fetchall()
        else:
            jobs = conn.execute("SELECT * FROM agent_jobs").fetchall()
        for job in jobs:
            job_id = str(job["job_id"])
            if job["status"] not in valid_job_statuses:
                violations.append(f"job {job_id}: invalid status {job['status']!r}")
            attempts = int(job["attempts"])
            max_attempts = int(job["max_attempts"])
            if attempts < 0 or max_attempts < 1 or attempts > max_attempts:
                violations.append(f"job {job_id}: attempts {attempts} out of range 0..{max_attempts}")
            runs = conn.execute(
                "SELECT * FROM job_runs WHERE job_id = ?", (job_id,)
            ).fetchall()
            active = [run for run in runs if run["status"] == "running"]
            if len(active) > 1:
                violations.append(f"job {job_id}: multiple active runs")
            for run in runs:
                if run["status"] not in valid_run_statuses:
                    violations.append(f"job_run {run['run_id']}: invalid status {run['status']!r}")
                if int(run["fencing_token"]) < 1:
                    violations.append(f"job_run {run['run_id']}: invalid fencing token")
                if run["status"] == "running" and run["lease_expires_at"] is not None \
                        and float(run["lease_expires_at"]) <= _now():
                    violations.append(f"job_run {run['run_id']}: active run has an expired lease")
            if job["status"] in {"claimed", "running"} and not active:
                violations.append(f"job {job_id}: {job['status']} without an active run")
            elif job["status"] in {"pending", "retryable", "completed", "failed", "cancelled"} and active:
                violations.append(f"job {job_id}: {job['status']} still has an active run")

        if task_id is not None:
            schedules = conn.execute(
                "SELECT * FROM cron_schedules WHERE task_id = ?", (task_id,)
            ).fetchall()
        else:
            schedules = conn.execute("SELECT * FROM cron_schedules").fetchall()
        for schedule in schedules:
            if schedule["enabled"] not in (0, 1):
                violations.append(f"cron {schedule['schedule_id']}: invalid enabled flag")
            last_at = schedule["last_triggered_at"]
            next_at = schedule["next_trigger_at"]
            if last_at is not None and next_at is not None and float(next_at) < float(last_at):
                violations.append(
                    f"cron {schedule['schedule_id']}: next_trigger before last_triggered"
                )

    def _scan_v6_invariants(self, conn: sqlite3.Connection, task_id: str | None,
                            violations: list[str]) -> None:
        valid_statuses = {
            "pending", "in_progress", "verifying", "completed", "failed", "retryable"
        }
        if task_id is not None:
            items = conn.execute(
                "SELECT i.*, p.task_id, p.dag_hash AS plan_dag_hash "
                "FROM plan_items i JOIN plans p ON p.plan_id = i.plan_id "
                "WHERE p.task_id = ?",
                (task_id,),
            ).fetchall()
        else:
            items = conn.execute(
                "SELECT i.*, p.task_id, p.dag_hash AS plan_dag_hash "
                "FROM plan_items i JOIN plans p ON p.plan_id = i.plan_id"
            ).fetchall()
        deps_by_item: dict[int, list[Any]] = {}
        for item in items:
            deps_by_item[int(item["plan_item_id"])] = _loads(item["blocked_by_json"], [])
            try:
                max_turns = int(item["max_turns"])
                consumed_turns = int(item["consumed_turns"])
                if max_turns < 1 or consumed_turns < 0 or consumed_turns > max_turns:
                    violations.append(
                        f"plan_item {item['plan_item_id']}: turn count "
                        f"{consumed_turns} is outside 0..{max_turns}"
                    )
            except (KeyError, TypeError, ValueError):
                violations.append(f"plan_item {item['plan_item_id']}: invalid turn budget")
            if item["status"] not in valid_statuses:
                violations.append(
                    f"plan_item {item['plan_item_id']}: invalid status {item['status']!r}"
                )
            elif item["status"] == "completed":
                evidence = item["evidence_hash"]
                if not evidence or not str(evidence).strip():
                    violations.append(
                        f"plan_item {item['plan_item_id']}: completed without evidence_hash"
                    )
        for item in items:
            blockers = deps_by_item.get(int(item["plan_item_id"]), [])
            for blocker in blockers:
                dep = conn.execute(
                    "SELECT status FROM plan_items WHERE plan_id = ? AND subtask_id = ?",
                    (item["plan_id"], blocker),
                ).fetchone()
                if dep is None:
                    violations.append(
                        f"plan_item {item['plan_item_id']}: blocked_by references missing subtask {blocker!r}"
                    )
                elif (
                    item["status"] in {"in_progress", "verifying", "completed", "retryable"}
                    and dep["status"] != "completed"
                    and not (
                        item["plan_dag_hash"] is not None
                        and item["status"] in {"in_progress", "verifying", "retryable"}
                    )
                ):
                    violations.append(
                        f"plan_item {item['plan_item_id']}: blocked_by {blocker!r} is not completed"
                    )

        if task_id is not None:
            calls = conn.execute(
                "SELECT model_call_id, source_checkpoint_id FROM model_calls WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            memories = conn.execute(
                "SELECT memory_id, task_id, source_checkpoint_id FROM memories WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            summaries = conn.execute(
                "SELECT summary_id, task_id, model_call_id FROM summaries WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        else:
            calls = conn.execute(
                "SELECT model_call_id, source_checkpoint_id FROM model_calls"
            ).fetchall()
            memories = conn.execute(
                "SELECT memory_id, task_id, source_checkpoint_id FROM memories"
            ).fetchall()
            summaries = conn.execute(
                "SELECT summary_id, task_id, model_call_id FROM summaries"
            ).fetchall()
        for call in calls:
            source = call["source_checkpoint_id"]
            if source is not None and conn.execute(
                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ?", (source,)
            ).fetchone() is None:
                violations.append(
                    f"model_call {call['model_call_id']}: source_checkpoint_id {source} is missing"
                )
        valid_tasks = {
            str(row["task_id"])
            for row in conn.execute("SELECT task_id FROM tasks")
        }
        for memory in memories:
            if memory["task_id"] not in valid_tasks:
                violations.append(
                    f"memory {memory['memory_id']}: references missing task {memory['task_id']!r}"
                )
            source = memory["source_checkpoint_id"]
            if source is not None and conn.execute(
                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ?", (source,)
            ).fetchone() is None:
                violations.append(
                    f"memory {memory['memory_id']}: references missing checkpoint {source}"
                )
        for summary in summaries:
            if summary["task_id"] not in valid_tasks:
                violations.append(
                    f"summary {summary['summary_id']}: references missing task {summary['task_id']!r}"
                )
            model_call_id = summary["model_call_id"]
            if model_call_id is not None and conn.execute(
                "SELECT 1 FROM model_calls WHERE model_call_id = ?", (model_call_id,)
            ).fetchone() is None:
                violations.append(
                    f"summary {summary['summary_id']}: references missing model_call {model_call_id}"
                )

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
                    "SELECT r.reservation_id, r.operation_id, r.owner_pid, r.owner_id, r.fencing_token "
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
                    if reservation["operation_id"] is not None:
                        operation_updated = conn.execute(
                            "UPDATE operations SET state = 'unknown', updated_at = ?, version = version + 1 "
                            "WHERE operation_id = ? AND state = 'dispatched'",
                            (now, reservation["operation_id"]),
                        )
                        if operation_updated.rowcount == 1:
                            conn.execute(
                                "UPDATE operation_outbox SET state = 'blocked', claimed_by = NULL, "
                                "claimed_until = NULL, last_error = ?, updated_at = ? WHERE operation_id = ?",
                                ("owner_crashed", now, reservation["operation_id"]),
                            )
                        else:
                            conn.execute(
                                "UPDATE operation_outbox SET state = 'pending', claimed_by = NULL, "
                                "claimed_until = NULL, last_error = ?, updated_at = ? "
                                "WHERE operation_id = ? AND operation_id IN "
                                "(SELECT operation_id FROM operations WHERE state = 'prepared')",
                                ("owner_crashed_before_dispatch", now, reservation["operation_id"]),
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
            operation_rows = conn.execute(
                "SELECT DISTINCT operation_id FROM effect_reservations "
                "WHERE owner_id = ? AND fencing_token = ? AND operation_id IS NOT NULL",
                (owner_id, int(fencing_token)),
            ).fetchall()
            for operation in operation_rows:
                operation_updated = conn.execute(
                    "UPDATE operations SET state = 'unknown', updated_at = ?, version = version + 1 "
                    "WHERE operation_id = ? AND state = 'dispatched'",
                    (now, operation["operation_id"]),
                )
                if operation_updated.rowcount == 1:
                    conn.execute(
                        "UPDATE operation_outbox SET state = 'blocked', claimed_by = NULL, "
                        "claimed_until = NULL, last_error = ?, updated_at = ? WHERE operation_id = ?",
                        ("lease_released", now, operation["operation_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE operation_outbox SET state = 'pending', claimed_by = NULL, "
                        "claimed_until = NULL, last_error = ?, updated_at = ? "
                        "WHERE operation_id = ? AND operation_id IN "
                        "(SELECT operation_id FROM operations WHERE state = 'prepared')",
                        ("lease_released_before_dispatch", now, operation["operation_id"]),
                    )
            # Retain the fencing epoch so a later owner cannot reuse an old token.
            conn.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? "
                "WHERE repo_root = ? AND owner_id = ? AND fencing_token = ?",
                (now, now, repo_root, owner_id, int(fencing_token)),
            )
