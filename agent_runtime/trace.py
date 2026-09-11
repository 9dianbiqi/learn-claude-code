from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .store import EventStore


class TraceReporter:
    def __init__(self, store: EventStore):
        self.store = store

    def summary(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        events = self.store.list_events(task_id)
        models = self.store.list_model_calls(task_id)
        tools = self.store.list_tool_calls(task_id)
        operations = self.store.list_operations(task_id)
        permission_counts = {"allow": 0, "ask": 0, "deny": 0}
        for event in events:
            if event["type"] == "permission_decision":
                permission = event["payload"].get("effect")
                if permission in permission_counts:
                    permission_counts[permission] += 1
        end_time = max((event["created_at"] for event in events), default=task["updated_at"])
        duration = max(0.0, float(end_time) - float(task["created_at"]))
        effect_groups: dict[str, list[dict[str, Any]]] = {}
        for call in tools:
            if call.get("effect_key"):
                effect_groups.setdefault(str(call["effect_key"]), []).append(call)
        duplicate_effect_attempts = sum(max(0, sum(int(call.get("effect_attempts") or 0) for call in group) - 1)
                                          for group in effect_groups.values())
        confirmed_duplicate_side_effects = sum(
            1 for group in effect_groups.values()
            if sum(int(call.get("effect_confirmed") or 0) for call in group) > 1
        )
        permission_bypass_count = sum(
            1 for call in tools if call.get("status") == "succeeded" and call.get("permission") == "deny"
        )
        review_calls = [call for call in tools if call.get("status") == "needs_review"]
        correct_reviews = sum(1 for call in review_calls if call.get("effect") in {"unknown_write", "file_write"})
        invariant_violations = self.store.scan_invariants(task_id)
        operation_deduplications = sum(
            event["type"] == "operation_deduplicated" for event in events
        )
        projection_metrics = self._projection_metrics(models)
        scoped_overflow_events = [
            event for event in events
            if event["type"] == "verified_scoped_context_overflow"
        ]
        scoped_overflow_token_estimates = [
            int(event["payload"]["token_estimate"])
            for event in scoped_overflow_events
            if isinstance(event.get("payload", {}).get("token_estimate"), (int, float))
        ]
        scoped_overflow_budgets = [
            int(event["payload"]["scoped_context_budget"])
            for event in scoped_overflow_events
            if isinstance(event.get("payload", {}).get("scoped_context_budget"), (int, float))
        ]
        scoped_overflow_units = [
            str(event["payload"]["resume_unit_id"])
            for event in scoped_overflow_events
            if event.get("payload", {}).get("resume_unit_id") is not None
        ]
        scoped_overflow_dependencies = [
            int(event["payload"]["dependency_count"])
            for event in scoped_overflow_events
            if isinstance(event.get("payload", {}).get("dependency_count"), (int, float))
        ]
        scoped_overflow_paths = [
            int(event["payload"]["relevant_path_count"])
            for event in scoped_overflow_events
            if isinstance(event.get("payload", {}).get("relevant_path_count"), (int, float))
        ]
        scoped_token_estimates = (
            projection_metrics["scoped_token_estimates"] + scoped_overflow_token_estimates
        )
        scoped_budgets = projection_metrics["scoped_budgets"] + scoped_overflow_budgets
        scoped_resume_unit_ids = (
            projection_metrics["scoped_resume_unit_ids"] + scoped_overflow_units
        )
        scoped_dependency_counts = (
            projection_metrics["scoped_dependency_counts"] + scoped_overflow_dependencies
        )
        scoped_relevant_path_counts = (
            projection_metrics["scoped_relevant_path_counts"] + scoped_overflow_paths
        )
        projection_basis = list(projection_metrics["basis"])
        if scoped_overflow_events and "verified_scoped" not in projection_basis:
            projection_basis.append("verified_scoped")
        plan_metrics = self._plan_metrics(task_id)
        verified_metrics = self._verified_metrics(task_id)
        jobs = self.store.list_jobs(task_id)
        job_status_counts: dict[str, int] = {}
        job_run_count = 0
        for job in jobs:
            job_status_counts[str(job["status"])] = job_status_counts.get(str(job["status"]), 0) + 1
            job_run_count += len(self.store.list_job_runs(str(job["job_id"])))
        return {
            "task_id": task_id,
            "status": task["status"],
            "event_count": len(events),
            "model_calls": len(models),
            "tool_calls": len(tools),
            "succeeded_tool_calls": sum(call["status"] == "succeeded" for call in tools),
            "duplicate_tool_calls": sum(event["type"] == "tool_deduplicated" for event in events),
            "review_count": sum(event["type"] == "tool_needs_review" for event in events),
            "total_input_tokens": sum(call.get("input_tokens") or 0 for call in models),
            "total_output_tokens": sum(call.get("output_tokens") or 0 for call in models),
            "duration_seconds": duration,
            "permission_counts": permission_counts,
            "tool_execution_attempts": sum(int(call.get("execution_attempts") or 0) for call in tools),
            "effect_attempts": sum(int(call.get("effect_attempts") or 0) for call in tools),
            "duplicate_effect_attempts": duplicate_effect_attempts,
            "confirmed_duplicate_side_effects": confirmed_duplicate_side_effects,
            "permission_bypass_count": permission_bypass_count,
            "invariant_violation_count": len(invariant_violations),
            "needs_review_correctness": correct_reviews / len(review_calls) if review_calls else 1.0,
            "stale_lease_execution_attempts": sum(
                event["type"] == "stale_lease_execution_attempt" for event in events
            ),
            "operation_count": len(operations),
            "operation_attempts": sum(int(operation.get("attempt_count") or 0) for operation in operations),
            "committed_operations": sum(operation.get("state") == "committed" for operation in operations),
            "unknown_operations": sum(operation.get("state") == "unknown" for operation in operations),
            "reconciled_operations": sum(
                event["type"] == "operation_reconciled" for event in events
            ),
            "operation_deduplications": operation_deduplications,
            "idempotency_conflicts": sum(
                event["type"] == "operation_idempotency_conflict" for event in events
            ),
            "blocked_outbox_count": sum(
                operation.get("outbox_state") == "blocked" for operation in operations
            ),
            "projection_basis": projection_basis,
            "projection_used_count": projection_metrics["used_count"],
            "projection_token_estimates": projection_metrics["token_estimates"],
            "verified_scoped_resume_count": projection_metrics["scoped_resume_count"],
            "verified_scoped_token_estimates": scoped_token_estimates,
            "verified_scoped_budgets": scoped_budgets,
            "verified_scoped_resume_unit_ids": scoped_resume_unit_ids,
            "verified_scoped_dependency_counts": scoped_dependency_counts,
            "verified_scoped_relevant_path_counts": scoped_relevant_path_counts,
            "verified_scoped_overflow_count": (
                projection_metrics["scoped_overflow_count"]
                + len(scoped_overflow_events)
            ),
            "verified_scoped_context_failure_count": sum(
                event["type"] == "verified_scoped_context_failed" for event in events
            ),
            # Keep concise scoped names alongside the verified-prefixed
            # fields for trace consumers that group projection modes.
            "scoped_resume_count": projection_metrics["scoped_resume_count"],
            "scoped_token_estimates": scoped_token_estimates,
            "scoped_context_budgets": scoped_budgets,
            "scoped_resume_unit_ids": scoped_resume_unit_ids,
            "scoped_dependency_counts": scoped_dependency_counts,
            "scoped_relevant_path_counts": scoped_relevant_path_counts,
            "scoped_context_overflow_count": (
                projection_metrics["scoped_overflow_count"]
                + len(scoped_overflow_events)
            ),
            "scoped_context_overflow_outcomes": [
                str(event["payload"].get("outcome") or "overflow")
                for event in scoped_overflow_events
            ],
            "memory_count": projection_metrics["memory_count"],
            "summary_count": projection_metrics["summary_count"],
            "plan_item_count": sum(
                len(item.get("items", [])) for item in plan_metrics["plans"]
            ),
            "plan_events": plan_metrics["events"],
            "plan_revision_count": len(plan_metrics["revisions"]),
            "current_plan_revision_id": plan_metrics["current_revision_id"],
            "current_plan_revision_dag_hash": plan_metrics["current_revision_dag_hash"],
            "verifier_run_count": verified_metrics["verifier_run_count"],
            "authoritative_verifier_run_count": verified_metrics["authoritative_verifier_run_count"],
            "non_authoritative_verifier_run_count": verified_metrics["non_authoritative_verifier_run_count"],
            "verified_subtask_checkpoint_count": verified_metrics["verified_subtask_checkpoint_count"],
            "verified_subtask_bundle_count": verified_metrics["verified_subtask_bundle_count"],
            "verified_subtask_valid_checkpoint_count": verified_metrics["valid_checkpoint_count"],
            "verified_subtask_stale_checkpoint_count": verified_metrics["stale_checkpoint_count"],
            "verified_subtask_superseded_checkpoint_count": verified_metrics["superseded_checkpoint_count"],
            "verified_subtask_refresh_count": verified_metrics["refresh_count"],
            "verified_subtask_f4_fail_count": verified_metrics["f4_fail_count"],
            "verified_subtask_f4_uncertain_count": verified_metrics["f4_uncertain_count"],
            "background_job_count": len(jobs),
            "background_job_runs": job_run_count,
            "background_job_statuses": job_status_counts,
            "recovered_job_count": sum(event["type"] == "job_recovered" for event in events),
            "cron_trigger_count": sum(event["type"] == "cron_triggered" for event in events),
            "cron_schedule_count": len(self.store.list_cron_schedules()),
        }

    @staticmethod
    def _projection_metrics(models: list[dict[str, Any]]) -> dict[str, Any]:
        used_count = 0
        bases: set[str] = set()
        token_estimates: list[int] = []
        memory_count = 0
        summary_count = 0
        scoped_resume_count = 0
        scoped_token_estimates: list[int] = []
        scoped_budgets: list[int] = []
        scoped_resume_unit_ids: list[str] = []
        scoped_dependency_counts: list[int] = []
        scoped_relevant_path_counts: list[int] = []
        scoped_overflow_count = 0
        for call in models:
            projection = call.get("projection") or {}
            basis = projection.get("basis")
            if projection.get("projection_used"):
                used_count += 1
            if basis:
                bases.add(basis)
            estimate = projection.get("token_estimate")
            if isinstance(estimate, (int, float)):
                token_estimates.append(int(estimate))
            memory_count = max(memory_count, int(projection.get("memory_count") or 0))
            summary_count = max(summary_count, int(projection.get("summary_count") or 0))
            if projection.get("basis") == "verified_scoped" or projection.get("scoped_resume"):
                scoped_resume_count += 1
                if isinstance(estimate, (int, float)):
                    scoped_token_estimates.append(int(estimate))
                budget = projection.get("scoped_context_budget")
                if isinstance(budget, (int, float)):
                    scoped_budgets.append(int(budget))
                resume_unit_id = projection.get("resume_unit_id")
                if resume_unit_id is not None:
                    scoped_resume_unit_ids.append(str(resume_unit_id))
                scoped_dependency_counts.append(int(projection.get("dependency_count") or 0))
                scoped_relevant_path_counts.append(int(projection.get("relevant_path_count") or 0))
                if projection.get("overflow") or projection.get("overflow_outcome") == "overflow":
                    scoped_overflow_count += 1
        return {
            "basis": sorted(bases),
            "used_count": used_count,
            "token_estimates": token_estimates,
            "memory_count": memory_count,
            "summary_count": summary_count,
            "scoped_resume_count": scoped_resume_count,
            "scoped_token_estimates": scoped_token_estimates,
            "scoped_budgets": scoped_budgets,
            "scoped_resume_unit_ids": scoped_resume_unit_ids,
            "scoped_dependency_counts": scoped_dependency_counts,
            "scoped_relevant_path_counts": scoped_relevant_path_counts,
            "scoped_overflow_count": scoped_overflow_count,
        }

    def _plan_metrics(self, task_id: str) -> dict[str, Any]:
        plans = self.store.list_plans(task_id)
        result = []
        for plan in plans:
            item = dict(plan)
            item["items"] = self.store.list_plan_items(int(plan["plan_id"]))
            result.append(item)
        events = [
            event["type"]
            for event in self.store.list_events(task_id)
            if event["type"].startswith("plan_item_")
            or event["type"].startswith("plan_revision_")
        ]
        revisions = self.store.list_plan_revisions(task_id)
        current = revisions[-1] if revisions else None
        return {
            "plans": result,
            "events": events,
            "revisions": revisions,
            "current_revision_id": current.revision_id if current else None,
            "current_revision_dag_hash": current.dag_hash if current else None,
        }

    def _verified_metrics(self, task_id: str) -> dict[str, int]:
        runs = self.store.list_verifier_runs(task_id)
        checkpoints = self.store.list_verified_subtask_checkpoints(task_id)
        current_counts = {
            state: sum(
                checkpoint.get("lifecycle_state") == state
                for checkpoint in checkpoints
            )
            for state in ("valid", "stale", "superseded")
        }
        events = self.store.list_events(task_id)
        bundle_hashes = {
            str(run["verifier_bundle_hash"])
            for run in runs
            if run["authoritative"]
        }
        return {
            "verifier_run_count": len(runs),
            "authoritative_verifier_run_count": sum(run["authoritative"] for run in runs),
            "non_authoritative_verifier_run_count": sum(not run["authoritative"] for run in runs),
            "verified_subtask_checkpoint_count": len(checkpoints),
            "verified_subtask_bundle_count": len(bundle_hashes),
            "valid_checkpoint_count": current_counts["valid"],
            "stale_checkpoint_count": current_counts["stale"],
            "superseded_checkpoint_count": current_counts["superseded"],
            "refresh_count": sum(
                event["type"] == "verified_subtask_evidence_refreshed"
                for event in events
            ),
            "f4_fail_count": sum(
                event["type"] == "verified_subtask_evidence_refresh_fail"
                for event in events
            ),
            "f4_uncertain_count": sum(
                event["type"] == "verified_subtask_evidence_refresh_uncertain"
                for event in events
            ),
        }

    def export_jsonl(self, task_id: str, output_path: str | Path) -> int:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        events = self.store.list_events(task_id)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(_redact({"record_type": "task", **self.store.get_task(task_id)}), ensure_ascii=False, sort_keys=True) + "\n")
            for event in events:
                handle.write(json.dumps(_redact(event), ensure_ascii=False, sort_keys=True) + "\n")
            for record_type, records in (
                ("model_call", self.store.list_model_calls(task_id)),
                ("tool_call", self.store.list_tool_calls(task_id)),
            ):
                for record in records:
                    handle.write(json.dumps(_redact({"record_type": record_type, **record}), ensure_ascii=False, sort_keys=True) + "\n")
            for operation in self.store.list_operations(task_id):
                safe_operation = {
                    key: operation.get(key)
                    for key in (
                        "operation_id",
                        "task_id",
                        "tool_use_id",
                        "adapter",
                        "semantics",
                        "effect_scope",
                        "args_hash",
                        "state",
                        "result_digest",
                        "attempt_count",
                        "version",
                        "created_at",
                        "updated_at",
                        "dispatched_at",
                        "completed_at",
                        "outbox_state",
                        "delivery_attempts",
                    )
                }
                handle.write(
                    json.dumps(
                        _redact({"record_type": "operation", **safe_operation}),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            for checkpoint in self.store.list_checkpoints(task_id):
                handle.write(json.dumps(_redact({"record_type": "checkpoint", **checkpoint}), ensure_ascii=False, sort_keys=True) + "\n")
            for plan in self.store.list_plans(task_id):
                plan_copy = dict(plan)
                items = self.store.list_plan_items(int(plan["plan_id"]))
                plan_copy["items"] = items
                handle.write(
                    json.dumps(
                        _redact({"record_type": "plan", **plan_copy}),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            for revision in self.store.list_plan_revisions(task_id):
                handle.write(
                    json.dumps(
                        _redact({"record_type": "plan_revision", **revision.as_dict()}),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            for verifier_run in self.store.list_verifier_runs(task_id):
                handle.write(
                    json.dumps(
                        _redact({"record_type": "verifier_run", **verifier_run}),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            for checkpoint in self.store.list_verified_subtask_checkpoints(task_id):
                handle.write(
                    json.dumps(
                        _redact({"record_type": "verified_subtask_checkpoint", **checkpoint}),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            for state_event in self.store.list_checkpoint_state_events(task_id):
                handle.write(
                    json.dumps(
                        _redact({"record_type": "verified_subtask_checkpoint_state_event", **state_event}),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            for job in self.store.list_jobs(task_id):
                safe_job = {
                    key: job.get(key)
                    for key in (
                        "job_id", "task_id", "lane_id", "kind", "status", "attempts",
                        "max_attempts", "created_at", "updated_at", "available_at",
                    )
                }
                handle.write(
                    json.dumps(
                        _redact({"record_type": "job", **safe_job}),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                for run in self.store.list_job_runs(str(job["job_id"])):
                    safe_run = {
                        key: run.get(key)
                        for key in (
                            "run_id", "job_id", "status", "heartbeat_at", "lease_expires_at",
                            "started_at", "completed_at", "result_digest",
                        )
                    }
                    handle.write(
                        json.dumps(
                            _redact({"record_type": "job_run", **safe_run}),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
        return len(events)


_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|authorization|cookie)", re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|cookie)\s*[=:]\s*"
    r"(?:(?:bearer|basic)\s+)?[^\s,;]+"
)


def _redact(value: Any, key: str | None = None) -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return value
