"""Durable, deterministic decisions over existing C1a/C1b plan mechanisms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

from .models import (
    AddPlanItem, PlanPatch, PlanRevision, PlanRevisionItem, SplitPlanItem, TombstonePlanItem,
    UpdatePlanItemDependencies, VerifiedSubtaskDAGConfig,
)
from .plan_revisions import PlanPatchError, apply_patch as derive_revision
from .store import InvariantViolation, LeaseLost, StaleState, _loads, _now

if TYPE_CHECKING:
    import sqlite3
    from .runtime import Runtime


DecisionKind = Literal[
    "KEEP", "RETRY", "SPLIT", "ADD_ITEM", "CHANGE_DEPENDENCY",
    "TOMBSTONE_PENDING", "FAIL",
]
_STRUCTURAL = {
    "SPLIT": SplitPlanItem,
    "ADD_ITEM": AddPlanItem,
    "CHANGE_DEPENDENCY": UpdatePlanItemDependencies,
    "TOMBSTONE_PENDING": TombstonePlanItem,
}
_SIGNAL_EVENTS = {
    "plan_item_failed", "task_failed", "verified_subtask_checkpoint_stale",
    "replan_observation",
}


@dataclass(frozen=True)
class RecoverySignal:
    signal_id: str
    signal_type: str
    task_id: str
    plan_item_id: int | None
    subtask_id: str | None
    status: str
    summary: str
    created_at: float
    observed_revision_id: str | None = None
    observed_item_version: int | None = None
    observed_task_version: int | None = None


@dataclass(frozen=True)
class ReplanDecision:
    kind: DecisionKind
    reason: str
    expected_revision_id: str
    patch: PlanPatch | None = None
    proposed_dag: VerifiedSubtaskDAGConfig | None = None


@dataclass(frozen=True)
class ReplanOutcome:
    task_id: str
    signal_id: str
    decision_type: str
    outcome: Literal["accepted", "rejected"]
    base_revision_id: str
    result_revision_id: str | None
    reason: str
    rejection_reason: str | None = None


DecisionPolicy = Callable[[RecoverySignal, PlanRevision, dict], ReplanDecision]


class RecoveryPlanRevisionController:
    """Map one durable signal to at most one decision and PlanRevision."""

    def __init__(self, runtime: Runtime, policy: DecisionPolicy):
        self.runtime = runtime
        self.store = runtime.store
        self.policy = policy

    def pending_signal_ids(self, task_id: str) -> list[str]:
        """Newest durable failure signals not yet settled in the decision ledger."""
        if self.store.get_task(task_id)["status"] in {"completed", "aborted"}:
            return []
        settled = {row["signal_id"] for row in self.store.list_replan_decisions(task_id)}
        candidates: list[tuple[float, int, str]] = []
        for run in self.store.list_verifier_runs(task_id):
            if (not run["authoritative"] and run["status"] in {"fail", "uncertain"}
                    and run.get("observed_plan_item_version") is not None
                    and run["verifier_run_id"] not in settled):
                candidates.append((float(run["created_at"]), 2,
                                   str(run["verifier_run_id"])))
        for event in self.store.list_events(task_id):
            signal_id = f"event:{event['event_id']}"
            if signal_id in settled or event["type"] not in _SIGNAL_EVENTS:
                continue
            payload = event["payload"]
            if not isinstance(payload.get("observed_revision_id"), str):
                continue  # legacy events have no replay-safe revision binding
            if event["type"] == "task_failed":
                if not isinstance(payload.get("observed_task_version"), int):
                    continue
            elif not isinstance(payload.get("observed_item_version"), int):
                continue
            if event["type"] == "replan_observation" and not payload.get("evidence_refs"):
                continue
            priority = 3 if event["type"] == "plan_item_failed" else 1
            candidates.append((float(event["created_at"]), priority, signal_id))
        return [signal_id for _, _, signal_id in sorted(candidates, reverse=True)]

    def record_observation(
        self, task_id: str, subtask_id: str, reason: str,
        evidence_refs: tuple[str, ...],
    ) -> str:
        """Record a task-owned observation with its revision, item state and evidence."""
        if (not isinstance(reason, str) or not isinstance(evidence_refs, (tuple, list))):
            raise ValueError("observation requires a reason and evidence references")
        refs = tuple(evidence_refs)
        if (not reason.strip() or not refs
                or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
                or len(refs) != len(set(refs))):
            raise ValueError("observation requires a reason and non-empty unique evidence references")
        self.runtime._acquire(task_id)
        try:
            with self.store.transaction() as conn:
                plan = conn.execute(
                    "SELECT * FROM plans WHERE task_id = ? ORDER BY plan_id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if plan is None or plan["current_revision_id"] is None:
                    raise InvariantViolation("observation requires a current PlanRevision")
                item = conn.execute(
                    "SELECT * FROM plan_items WHERE plan_id = ? AND subtask_id = ? AND tombstoned = 0",
                    (plan["plan_id"], subtask_id),
                ).fetchone()
                if item is None:
                    raise ValueError("observation PlanItem is missing or inactive")
                event_id = self.store._emit_conn(conn, task_id, "replan_observation", {
                    "plan_item_id": int(item["plan_item_id"]), "subtask_id": subtask_id,
                    "reason": reason, "evidence_refs": sorted(refs),
                    "observed_revision_id": str(plan["current_revision_id"]),
                    "observed_item_version": int(item["version"]),
                })
            return f"event:{event_id}"
        finally:
            if self.runtime._lease_token is not None:
                self.runtime._release()

    @staticmethod
    def _decode(row) -> ReplanOutcome:
        return ReplanOutcome(
            task_id=str(row["task_id"]), signal_id=str(row["signal_id"]),
            decision_type=str(row["decision_type"]), outcome=str(row["outcome"]),
            base_revision_id=str(row["base_revision_id"]),
            result_revision_id=(str(row["result_revision_id"])
                                if row["result_revision_id"] is not None else None),
            reason=str(row["reason"]), rejection_reason=row["rejection_reason"],
        )

    @staticmethod
    def _signal(conn: sqlite3.Connection, task_id: str, signal_id: str) -> RecoverySignal:
        if signal_id.startswith("event:"):
            try:
                event_id = int(signal_id[6:])
            except ValueError as exc:
                raise ValueError("signal_id must contain a numeric event ID") from exc
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ? AND task_id = ?",
                (event_id, task_id),
            ).fetchone()
            if event is None or event["type"] not in _SIGNAL_EVENTS:
                raise ValueError("signal is missing, foreign, or not a recovery signal")
            payload = _loads(event["payload_json"], {})
            observed_revision_id = payload.get("observed_revision_id")
            observed_item_version = payload.get("observed_item_version")
            observed_task_version = payload.get("observed_task_version")
            if event["type"] == "replan_observation":
                refs = payload.get("evidence_refs")
                if (not isinstance(refs, list) or not refs
                        or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
                        or len(refs) != len(set(refs))):
                    raise ValueError("observation lacks durable evidence")
            if event["type"] == "task_failed" and (
                not isinstance(observed_revision_id, str)
                or isinstance(observed_task_version, bool)
                or not isinstance(observed_task_version, int)
            ):
                raise ValueError("task failure lacks a durable revision/task-state binding")
            if event["type"] != "task_failed" and (
                not isinstance(observed_revision_id, str)
                or isinstance(observed_item_version, bool)
                or not isinstance(observed_item_version, int)
            ):
                raise ValueError("recovery event lacks a durable revision/item-state binding")
            item_id = payload.get("plan_item_id")
            if event["type"] == "replan_observation" and (
                isinstance(item_id, bool) or not isinstance(item_id, int)
                or not isinstance(payload.get("subtask_id"), str)
                or not payload["subtask_id"].strip()
            ):
                raise ValueError("observation lacks a PlanItem identity")
            return RecoverySignal(
                signal_id, str(event["type"]), task_id,
                int(item_id) if item_id is not None else None,
                str(payload["subtask_id"]) if payload.get("subtask_id") else None,
                str(event["type"]), str(payload.get("reason") or payload.get("error") or ""),
                float(event["created_at"]), observed_revision_id, observed_item_version,
                observed_task_version if event["type"] == "task_failed" else None,
            )
        row = conn.execute(
            "SELECT * FROM verifier_runs WHERE verifier_run_id = ? AND task_id = ?",
            (signal_id, task_id),
        ).fetchone()
        if row is None or row["status"] not in {"fail", "uncertain"} or int(row["authoritative"]):
            raise ValueError("signal is missing, foreign, or not a failed verifier run")
        if row["observed_plan_item_version"] is None:
            raise ValueError("verifier signal lacks a durable PlanItem attempt binding")
        return RecoverySignal(
            signal_id, "verifier_run", task_id, int(row["plan_item_id"]),
            str(row["subtask_id"]), str(row["status"]), str(row["summary"]),
            float(row["created_at"]), None, int(row["observed_plan_item_version"]),
        )

    @staticmethod
    def _check_proposed_dag(
        revision: PlanRevision, statuses: dict[str, str],
        decision: ReplanDecision,
    ) -> tuple[PlanRevisionItem, ...]:
        if not isinstance(decision.patch, PlanPatch) or not isinstance(
            decision.proposed_dag, VerifiedSubtaskDAGConfig
        ):
            raise PlanPatchError("structural decision requires a typed patch and complete DAG configuration")
        if len(decision.patch.operations) != 1 or not isinstance(
            decision.patch.operations[0], _STRUCTURAL[decision.kind]
        ):
            raise PlanPatchError("decision type does not match its typed PlanPatch operation")
        if decision.patch.trigger != "c1c_controller":
            raise PlanPatchError("controller PlanPatch must identify its trigger")
        derived = derive_revision(revision.items, statuses, decision.patch)
        active = [item for item in derived if not item.tombstoned]
        nodes = decision.proposed_dag.nodes
        if len(active) != len(nodes) or any(
            item.subtask_id != node.subtask_id
            or item.blocked_by != node.blocked_by
            or item.verifier_bundle_hash != node.verifier_bundle_hash
            or item.max_turns != node.max_turns
            or not callable(node.verifier)
            for item, node in zip(active, nodes)
        ):
            raise PlanPatchError("proposed DAG lacks exact current verifier capabilities")
        if any(item.verifier_bundle_hash is None for item in active):
            raise PlanPatchError("verified DAG requires verifier bundle hashes for active items")
        return derived

    @staticmethod
    def _check_failed_activation(
        conn: sqlite3.Connection, task_id: str, plan, task,
        derived: tuple[PlanRevisionItem, ...], statuses: dict[str, str],
    ) -> None:
        """Reject a proposal that C1b could not activate after task failure."""
        if task["status"] != "failed" and plan["status"] != "failed":
            return
        checkpoint = conn.execute(
            "SELECT phase FROM checkpoints WHERE checkpoint_id = ? AND task_id = ?",
            (task["checkpoint_id"], task_id),
        ).fetchone()
        if (task["status"] != "failed" or plan["status"] != "failed"
                or checkpoint is None or checkpoint["phase"] != "failed"):
            raise PlanPatchError("failed task/Plan/checkpoint are not eligible for C1b activation")
        active = [item for item in derived if not item.tombstoned]
        if any(statuses.get(item.subtask_id) == "failed" for item in active):
            raise PlanPatchError("proposed revision leaves an active failed PlanItem")
        consumed = {
            str(row["subtask_id"]): int(row["consumed_turns"])
            for row in conn.execute(
                "SELECT subtask_id, consumed_turns FROM plan_items WHERE plan_id = ?",
                (plan["plan_id"],),
            )
        }
        completed = {item.subtask_id for item in active
                     if statuses.get(item.subtask_id) == "completed"}
        runnable = any(
            statuses.get(item.subtask_id, "pending") in {
                "pending", "retryable", "in_progress", "verifying"
            }
            and consumed.get(item.subtask_id, 0) < item.max_turns
            and set(item.blocked_by) <= completed
            for item in active
        )
        if not runnable:
            raise PlanPatchError("proposed failed-task revision has no runnable frontier")

    def process(self, task_id: str, signal_id: str, *, lease_acquired: bool = False) -> ReplanOutcome:
        if not isinstance(signal_id, str) or not signal_id.strip():
            raise ValueError("signal_id must be non-empty")
        if lease_acquired:
            if self.runtime._lease_token is None:
                raise LeaseLost(f"No lease is bound for {task_id}")
        else:
            self.runtime._acquire(task_id)
        try:
            with self.store.transaction() as conn:
                previous = conn.execute(
                    "SELECT * FROM replan_decisions WHERE task_id = ? AND signal_id = ? "
                    "ORDER BY decision_id DESC LIMIT 1", (task_id, signal_id),
                ).fetchone()
                if previous is not None:
                    return self._decode(previous)
                signal = self._signal(conn, task_id, signal_id)
                plan = conn.execute(
                    "SELECT * FROM plans WHERE task_id = ? ORDER BY plan_id DESC LIMIT 1", (task_id,),
                ).fetchone()
                if plan is None or plan["current_revision_id"] is None:
                    raise InvariantViolation("replanning requires a current PlanRevision")
                revision_row = conn.execute(
                    "SELECT * FROM plan_revisions WHERE revision_id = ? AND plan_id = ?",
                    (plan["current_revision_id"], plan["plan_id"]),
                ).fetchone()
                revision = self.store._decode_plan_revision(revision_row)
                if revision is None or signal.created_at < revision.created_at:
                    raise StaleState("recovery signal predates the current PlanRevision")
                if (signal.observed_revision_id is not None
                        and signal.observed_revision_id != revision.revision_id):
                    raise StaleState("observation was recorded against an older PlanRevision")
                task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                if (signal.observed_task_version is not None
                        and int(task["version"]) != signal.observed_task_version):
                    raise StaleState("task failure state has changed")
                item = None
                if signal.plan_item_id is not None:
                    item = conn.execute(
                        "SELECT * FROM plan_items WHERE plan_item_id = ? AND plan_id = ?",
                        (signal.plan_item_id, plan["plan_id"]),
                    ).fetchone()
                    if item is None or item["tombstoned"]:
                        raise StaleState("recovery signal refers to an inactive PlanItem")
                    if signal.subtask_id is not None and item["subtask_id"] != signal.subtask_id:
                        raise StaleState("recovery signal PlanItem identity has changed")
                    if item["status"] == "completed" and signal.signal_type != "replan_observation":
                        raise StaleState("recovery signal refers to already completed work")
                    if signal.signal_type == "plan_item_failed":
                        matches_failure = (
                            item["status"] == "failed"
                            and int(item["version"]) == signal.observed_item_version
                        ) or (
                            item["status"] == "retryable"
                            and int(item["version"]) == signal.observed_item_version + 1
                        )
                        if not matches_failure:
                            raise StaleState("failure event PlanItem attempt has changed")
                    if signal.signal_type == "verifier_run" and item["status"] not in {
                        "retryable", "failed"
                    }:
                        raise StaleState("verifier signal no longer matches the PlanItem state")
                    if signal.signal_type == "verifier_run":
                        newest = conn.execute(
                            "SELECT verifier_run_id FROM verifier_runs WHERE task_id = ? "
                            "AND plan_item_id = ? AND authoritative = 0 "
                            "AND status IN ('fail', 'uncertain') "
                            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                            (task_id, signal.plan_item_id),
                        ).fetchone()
                        if newest is None or newest["verifier_run_id"] != signal_id:
                            raise StaleState("verifier signal was superseded by a later attempt")
                    if (signal.signal_type != "plan_item_failed"
                            and signal.observed_item_version is not None
                            and int(item["version"]) != signal.observed_item_version):
                        raise StaleState("recovery event PlanItem state has changed")
                decision = self.policy(signal, revision, dict(item) if item else dict(task))
                if not isinstance(decision, ReplanDecision) or not decision.reason.strip():
                    raise ValueError("policy must return a reasoned ReplanDecision")
                if decision.expected_revision_id != revision.revision_id:
                    raise StaleState("decision expected a different PlanRevision")
                if decision.kind not in {*_STRUCTURAL, "KEEP", "RETRY", "FAIL"}:
                    raise ValueError("unknown replanning decision")
                result_revision_id = None
                statuses = {str(row["subtask_id"]): str(row["status"]) for row in conn.execute(
                    "SELECT subtask_id, status FROM plan_items WHERE plan_id = ?", (plan["plan_id"],)
                )}
                try:
                    if decision.kind in _STRUCTURAL:
                        if plan["status"] not in {"active", "failed"} or task["status"] in {"completed", "aborted"}:
                            raise PlanPatchError("task or Plan is ineligible for a structural decision")
                        derived = self._check_proposed_dag(revision, statuses, decision)
                        self._check_failed_activation(conn, task_id, plan, task, derived, statuses)
                        if signal_id not in decision.patch.evidence_refs:
                            raise PlanPatchError("PlanPatch must reference the durable recovery signal")
                    elif decision.patch is not None or decision.proposed_dag is not None:
                        raise PlanPatchError("non-structural decision cannot carry a PlanPatch or revised DAG")
                    elif decision.kind == "RETRY":
                        if (item is None or task["status"] in {"completed", "failed", "aborted"}
                                or item["status"] != "retryable"
                                or int(item["consumed_turns"]) >= int(item["max_turns"])):
                            raise PlanPatchError("RETRY is ineligible or has exhausted its existing budget")
                    elif decision.kind == "KEEP" and task["status"] in {"completed", "failed", "aborted"}:
                        raise PlanPatchError("KEEP cannot reopen a terminal task")
                    elif decision.kind == "FAIL" and task["status"] in {"completed", "aborted"}:
                        raise PlanPatchError("FAIL cannot change a completed or aborted task")
                except (PlanPatchError, ValueError) as exc:
                    reason = str(exc)[:256]
                    conn.execute(
                        "INSERT INTO replan_decisions(task_id, signal_id, signal_type, plan_item_id, "
                        "base_revision_id, decision_type, reason, outcome, rejection_reason, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?)",
                        (task_id, signal_id, signal.signal_type, signal.plan_item_id,
                         revision.revision_id, decision.kind, decision.reason, reason, _now()),
                    )
                    self.store._emit_conn(conn, task_id, "replan_decision_recorded", {
                        "signal_id": signal_id, "signal_type": signal.signal_type,
                        "decision": decision.kind, "reason": decision.reason,
                        "base_revision_id": revision.revision_id,
                        "result_revision_id": None, "outcome": "rejected", "rejection_reason": reason,
                    })
                    return ReplanOutcome(task_id, signal_id, decision.kind, "rejected",
                                         revision.revision_id, None, decision.reason, reason)
                if decision.kind in _STRUCTURAL:
                    patched = self.store._apply_plan_patch_conn(
                        conn, task_id, revision.revision_id, decision.patch,
                        fault_injector=self.runtime._fault,
                    )
                    result_revision_id = patched.revision_id
                elif decision.kind == "FAIL" and task["status"] != "failed":
                    self.store._fail_task_conn(conn, task_id, decision.reason)
                now = _now()
                conn.execute(
                    "INSERT INTO replan_decisions(task_id, signal_id, signal_type, plan_item_id, "
                    "base_revision_id, result_revision_id, decision_type, reason, outcome, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)",
                    (task_id, signal_id, signal.signal_type, signal.plan_item_id,
                     revision.revision_id, result_revision_id, decision.kind, decision.reason, now),
                )
                self.store._emit_conn(conn, task_id, "replan_decision_recorded", {
                    "signal_id": signal_id, "signal_type": signal.signal_type,
                    "decision": decision.kind, "reason": decision.reason,
                    "base_revision_id": revision.revision_id,
                    "result_revision_id": result_revision_id, "outcome": "accepted",
                })
                self.runtime._fault("replan_before_commit", task_id=task_id,
                                    signal_id=signal_id, revision_id=result_revision_id)
                result = ReplanOutcome(task_id, signal_id, decision.kind, "accepted",
                                       revision.revision_id, result_revision_id, decision.reason)
            self.runtime._fault("replan_after_commit", task_id=task_id,
                                signal_id=signal_id, revision_id=result_revision_id)
            return result
        finally:
            if not lease_acquired and self.runtime._lease_token is not None:
                self.runtime._release()
