from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .effects import (
    OperationSpec,
    ReconcileEvidence,
    semantics_for_effect,
    stable_dedupe_key,
)
from .models import (
    ModelResponse,
    RunResult,
    ToolCall,
    VerifierContext,
    VerifierResult,
    VerifiedSubtaskConfig,
    VerifiedSubtaskDAGConfig,
    is_valid_sha256,
    normalize_evidence_path,
)
from .permissions import PermissionDecision, PermissionEngine
from .store import EffectBlocked, EventStore, InvariantViolation, LeaseLost, StaleState
from .tools import FileConflict, ShellResult, ToolExecutor
from .tool_registry import ToolRegistry
from .projector import ContextProjector, estimate_tokens
from .scoped_resume import (
    ScopedContextError,
    ScopedContextProjection,
    VerifiedScopedResume,
)
from .verified_evidence import VerifiedEvidenceRecovery, capture_evidence_manifest


DEFAULT_SUBAGENT_CONTEXT_WINDOW = 32000
MAX_SUBAGENT_DEPTH = 3


class InjectedCrash(RuntimeError):
    """Raised by tests at a named durable boundary."""


class WaitingForApproval(RuntimeError):
    def __init__(self, tool_use_id: str, reason: str):
        super().__init__(reason)
        self.tool_use_id = tool_use_id
        self.reason = reason


class NeedsReview(RuntimeError):
    def __init__(self, tool_use_id: str, reason: str):
        super().__init__(reason)
        self.tool_use_id = tool_use_id
        self.reason = reason


def canonical_args_hash(name: str, args: dict[str, Any]) -> str:
    payload = json.dumps({"name": name, "input": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


INTERNAL_TOOL_SCHEMAS = {
    ".agent_runtime.spawn_subagent": {
        "name": ".agent_runtime.spawn_subagent",
        "description": "Create and run a child agent in its own durable task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "role": {"type": "string"},
                "wait": {"type": "boolean"},
                "tool_scope": {"type": "array", "items": {"type": "string"}},
                "context_window": {"type": "integer"},
                "model": {"type": "string"},
            },
            "required": ["prompt"],
        },
    },
    ".agent_runtime.request_plan_approval": {
        "name": ".agent_runtime.request_plan_approval",
        "description": "Request human approval for a subagent plan before continuing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_text": {"type": "string"},
                "plan_hash": {"type": "string"},
            },
            "required": ["plan_text"],
        },
    },
}


class Runtime:
    _DAG_BLOCKED_FEEDBACK = (
        "The current subtask is blocked by incomplete dependencies. Continue working "
        "on the current subtask and emit SUBTASK_COMPLETE only after all dependencies "
        "are complete."
    )

    def __init__(self, repo_root: str | Path, model: Any, store: EventStore | None = None,
                 model_name: str | None = None, owner_id: str | None = None,
                 fault_injector: Callable[..., None] | None = None,
                 approval_callback: Callable[[str, dict[str, Any], str], bool] | None = None,
                 interactive: bool = False, policy_path: str | Path | None = None,
                 lease_ttl: float = 300.0,
                 tool_scope: set[str] | None = None,
                 verified_subtask: VerifiedSubtaskConfig | None = None,
                 verified_subtask_dag: VerifiedSubtaskDAGConfig | None = None,
                 scoped_context_budget: int | None = None):
        if verified_subtask is not None and verified_subtask_dag is not None:
            raise ValueError("verified_subtask and verified_subtask_dag are mutually exclusive")
        self.repo_root = Path(repo_root).resolve()
        self.model = model
        self.model_name = model_name or getattr(model, "name", "unknown")
        self.store = store or EventStore(self.repo_root / ".agent_runtime" / "runtime.db")
        self.owner_id = owner_id or f"runtime-{uuid.uuid4().hex}"
        self.fault_injector = fault_injector
        self.approval_callback = approval_callback
        self.interactive = interactive
        self.lease_ttl = lease_ttl
        self.tool_scope = self._normalize_tool_scope(tool_scope)
        self.verified_subtask = verified_subtask
        model_timeout = getattr(model, "timeout", None)
        if model_timeout is not None and float(model_timeout) >= lease_ttl:
            raise ValueError("Model timeout must be smaller than lease TTL")
        self._lease_token: int | None = None
        self._active_task_id: str | None = None
        self._active_tool_scope: set[str] | None = None
        self._active_subagent_run_id: str | None = None
        self._subagent_fencing: dict[str, str] = {}
        self._subagent_tool_scopes: dict[str, set[str] | None] = {}
        self.verified_subtask_dag = verified_subtask_dag
        if scoped_context_budget is not None:
            if (
                isinstance(scoped_context_budget, bool)
                or not isinstance(scoped_context_budget, int)
                or scoped_context_budget < 1
            ):
                raise ValueError("scoped_context_budget must be a positive integer")
        self.scoped_context_budget = scoped_context_budget
        self._dag_nodes = (
            {node.subtask_id: node for node in verified_subtask_dag.nodes}
            if verified_subtask_dag is not None else {}
        )
        self._evidence_recovery = VerifiedEvidenceRecovery(
            self.store,
            self.repo_root,
            self._dag_nodes if verified_subtask_dag is not None else (
                {verified_subtask.subtask_id: verified_subtask}
                if verified_subtask is not None else {}
            ),
        )
        self._scoped_resume = (
            VerifiedScopedResume(
                self.store,
                self.repo_root,
                self._dag_nodes,
                tuple(node.subtask_id for node in verified_subtask_dag.nodes),
            )
            if verified_subtask_dag is not None
            else None
        )
        self.tools = ToolExecutor(self.repo_root)
        if float(self.tools.shell_timeout) >= lease_ttl:
            self.tools.shell_timeout = max(0.01, float(lease_ttl) * 0.8)
        self.tool_registry = ToolRegistry(self.repo_root, executor=self.tools, store=self.store)
        self.tool_registry.install_builtins()
        self.tool_registry.load_from_store()
        self.permissions = PermissionEngine(
            self.repo_root,
            policy_path=policy_path,
            tool_registry=self.tool_registry,
        )
        self._pending_review: tuple[str, str, str] | None = None
        self.projector = ContextProjector(self.store)

    def _fault(self, point: str, **context: Any) -> None:
        if self.fault_injector:
            self.fault_injector(point, **context)

    @staticmethod
    def _has_completion_marker(text: str) -> bool:
        return any(line == "SUBTASK_COMPLETE" for line in str(text).splitlines())

    @staticmethod
    def _completion_summary(text: str) -> str:
        return "".join(
            line
            for line in str(text).splitlines(keepends=True)
            if line.rstrip("\r\n") != "SUBTASK_COMPLETE"
        )

    @staticmethod
    def _normalize_evidence_manifest(
        manifest: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in manifest:
            if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
                raise ValueError("evidence manifest entries require path and sha256")
            normalized_path = normalize_evidence_path(str(entry["path"]))
            if normalized_path in seen:
                raise ValueError(f"duplicate evidence path: {normalized_path}")
            seen.add(normalized_path)
            normalized.append({"path": normalized_path, "sha256": str(entry["sha256"])})
        return sorted(normalized, key=lambda entry: entry["path"])

    @staticmethod
    def _verifier_feedback(status: str, summary: str) -> str:
        return (
            f"Verifier result: {status}. {summary}\n"
            "The subtask is not complete. Address the verifier feedback and emit "
            "SUBTASK_COMPLETE on its own line when the completion criteria are satisfied."
        )

    def _uses_verified_subtasks(self) -> bool:
        return self.verified_subtask is not None or self.verified_subtask_dag is not None

    def _assert_dag_config(self, task_id: str) -> dict[str, Any]:
        if self.verified_subtask_dag is None:
            raise RuntimeError(
                f"Task {task_id} requires its frozen verified-subtask DAG configuration to resume"
            )
        plan = self.store.get_latest_plan(task_id)
        if plan is None or plan.get("dag_hash") is None:
            raise RuntimeError(f"Task {task_id} does not use a frozen verified-subtask DAG")
        if plan["dag_hash"] != self.verified_subtask_dag.dag_hash:
            raise RuntimeError(f"DAG hash mismatch for task {task_id}")
        items = plan.get("items", [])
        nodes = self.verified_subtask_dag.nodes
        if len(items) != len(nodes):
            raise RuntimeError(f"DAG configuration mismatch for task {task_id}")
        for item, node in zip(items, nodes):
            try:
                stored_max_turns = int(item["max_turns"])
            except (KeyError, TypeError, ValueError):
                stored_max_turns = None
            if (
                item.get("subtask_id") != node.subtask_id
                or tuple(item.get("blocked_by", [])) != node.blocked_by
                or item.get("verifier_bundle_hash") != node.verifier_bundle_hash
                or stored_max_turns != node.max_turns
            ):
                raise RuntimeError(f"DAG configuration mismatch for task {task_id}")
        return plan

    def _select_dag_item(
        self,
        task_id: str,
        *,
        start_selected_item: bool = True,
    ) -> dict[str, Any] | None:
        plan = self.store.get_active_plan(task_id)
        if plan is None:
            return None
        items = plan["items"]
        by_id = {item["subtask_id"]: item for item in items}
        for status in ("verifying", "in_progress", "retryable"):
            candidates = [item for item in items if item["status"] == status]
            if candidates:
                item = candidates[0]
                if (
                    status == "retryable"
                    and start_selected_item
                    and self.store.plan_item_dependencies_complete(
                        int(item["plan_item_id"])
                    )
                ):
                    self.store.start_plan_item(int(item["plan_item_id"]))
                    return self.store.get_plan_item(task_id, str(item["subtask_id"]))
                return item
        for item in items:
            if item["status"] != "pending":
                continue
            ready = all(
                dependency in by_id and by_id[dependency]["status"] == "completed"
                for dependency in item.get("blocked_by", [])
            )
            if not ready:
                continue
            if start_selected_item:
                self.store.start_plan_item(int(item["plan_item_id"]))
                return self.store.get_plan_item(task_id, str(item["subtask_id"]))
            return item
        return None

    def _dag_config_for_item(self, item: dict[str, Any]) -> VerifiedSubtaskConfig:
        try:
            return self._dag_nodes[str(item["subtask_id"])]
        except KeyError as exc:
            raise InvariantViolation(
                f"Plan item has no frozen DAG configuration: {item.get('subtask_id')!r}"
            ) from exc

    def _dag_budget_error(self, item: dict[str, Any]) -> str:
        return (
            f"Subtask {item['subtask_id']} exhausted its maximum turn budget "
            f"(max_turns={item['max_turns']}) without a valid completion marker"
        )

    def _fail_dag_budget(
        self,
        task_id: str,
        item: dict[str, Any],
        messages: list[dict],
        turn: int,
    ) -> RunResult:
        error = self._dag_budget_error(item)
        self.store.fail_plan_item_and_task(
            task_id,
            int(item["plan_item_id"]),
            messages,
            {"turn": turn, "active_subtask_id": item["subtask_id"]},
            error,
        )
        return RunResult(task_id, "failed", error=error)

    def _handle_blocked_dag_marker(
        self,
        task_id: str,
        item: dict[str, Any],
        messages: list[dict],
        turn: int,
    ) -> RunResult | None:
        if item["status"] == "verifying":
            self.store.return_plan_item_to_progress(int(item["plan_item_id"]))
            refreshed = self.store.get_plan_item(task_id, str(item["subtask_id"]))
            if refreshed is not None:
                item = refreshed
        self._append_verified_feedback(
            task_id,
            messages,
            turn,
            self._DAG_BLOCKED_FEEDBACK,
            active_subtask_id=str(item["subtask_id"]),
        )
        if int(item["consumed_turns"]) >= int(item["max_turns"]):
            return self._fail_dag_budget(task_id, item, messages, turn)
        return None

    def _append_verified_feedback(
        self,
        task_id: str,
        messages: list[dict],
        turn: int,
        feedback: str,
        active_subtask_id: str | None = None,
    ) -> None:
        messages.append({"role": "user", "content": feedback})
        cursor: dict[str, Any] = {"turn": turn + 1}
        if active_subtask_id is not None:
            cursor["active_subtask_id"] = active_subtask_id
        self.store.save_checkpoint(
            task_id,
            "tool_results_appended",
            messages,
            cursor,
        )

    def _handle_verified_marker(
        self,
        task_id: str,
        messages: list[dict],
        turn: int,
        execution_checkpoint_id: int,
        completion_summary: str,
        marker_subtask_id: str | None = None,
    ) -> RunResult | None:
        if self.verified_subtask_dag is not None:
            if marker_subtask_id is not None:
                marker_item = self.store.get_plan_item(task_id, marker_subtask_id)
                if marker_item is not None and marker_item["status"] == "retryable":
                    # Do not let _select_dag_item start the reopened node and
                    # then feed it the stale completion marker below.
                    return None
            item = self._select_dag_item(task_id)
            if item is None:
                return None
            if marker_subtask_id is not None and item["subtask_id"] != marker_subtask_id:
                return None
            if item["status"] == "completed":
                return None
            config = self._dag_config_for_item(item)
        else:
            config = self.verified_subtask
            if config is None:
                return None
            item = self._verified_plan_item(task_id)
        if self.verified_subtask_dag is not None and not self.store.plan_item_dependencies_complete(
            int(item["plan_item_id"])
        ):
            return self._handle_blocked_dag_marker(task_id, item, messages, turn)
        if item["status"] == "retryable":
            self.store.start_plan_item(int(item["plan_item_id"]))
            item = (
                self.store.get_plan_item(task_id, str(item["subtask_id"]))
                if self.verified_subtask_dag is not None
                else self._verified_plan_item(task_id)
            )
        if item["status"] == "in_progress":
            self.store.submit_plan_item_for_verification(
                int(item["plan_item_id"]),
                completion_summary=completion_summary,
            )
            item = (
                self.store.get_plan_item(task_id, str(item["subtask_id"]))
                if self.verified_subtask_dag is not None
                else self._verified_plan_item(task_id)
            )
        if self.verified_subtask_dag is not None and not self.store.plan_item_dependencies_complete(
            int(item["plan_item_id"])
        ):
            return self._handle_blocked_dag_marker(task_id, item, messages, turn)
        if item["status"] == "completed":
            completed = self.store.get_completed_result(task_id)
            return RunResult(task_id, "completed", completed["final_text"])
        if item["status"] != "verifying":
            raise InvariantViolation(
                f"verified subtask {config.subtask_id} cannot verify from {item['status']}"
            )

        context = VerifierContext(
            repo_root=str(self.repo_root),
            task_id=task_id,
            plan_item_id=int(item["plan_item_id"]),
            subtask_id=config.subtask_id,
            completion_summary=completion_summary,
            execution_checkpoint_id=execution_checkpoint_id,
        )
        try:
            verifier_result = config.verifier(context)
        except Exception as exc:
            verifier_result = VerifierResult(
                status="uncertain",
                summary=f"Verifier raised {type(exc).__name__}: {str(exc)[:256]}",
                evidence_manifest=[],
            )
        if not isinstance(verifier_result, VerifierResult):
            verifier_result = VerifierResult(
                status="uncertain",
                summary="Verifier returned a malformed result; expected VerifierResult.",
                evidence_manifest=[],
            )
        try:
            manifest = self._normalize_evidence_manifest(verifier_result.evidence_manifest)
        except Exception as exc:
            verifier_result = VerifierResult(
                status="uncertain",
                summary=f"Verifier returned an invalid evidence manifest: {str(exc)[:256]}",
                evidence_manifest=[],
            )
            manifest = []
        invalid_hashes = [
            entry["path"] for entry in manifest if not is_valid_sha256(entry["sha256"])
        ]
        if invalid_hashes and verifier_result.status == "pass":
            verifier_result = VerifierResult(
                status="uncertain",
                summary=(
                    "Verifier pass rejected because evidence contains invalid SHA-256 "
                    "values: " + ", ".join(invalid_hashes)
                ),
                evidence_manifest=manifest,
            )
        covered_paths = {entry["path"] for entry in manifest}
        missing_paths = sorted(set(config.evidence_paths) - covered_paths)
        if verifier_result.status == "pass" and missing_paths:
            verifier_result = VerifierResult(
                status="uncertain",
                summary=(
                    "Verifier pass rejected because evidence is incomplete: "
                    + ", ".join(missing_paths)
                ),
                evidence_manifest=manifest,
            )

        if verifier_result.status == "pass":
            observed_snapshot = capture_evidence_manifest(self.repo_root, manifest)
            if not observed_snapshot.complete:
                verifier_result = VerifierResult(
                    "uncertain",
                    "Verifier pass rejected because evidence could not be captured: "
                    + (observed_snapshot.reason or "unknown evidence capture failure"),
                    manifest,
                )
            elif observed_snapshot.manifest != manifest:
                verifier_result = VerifierResult(
                    "uncertain",
                    "Verifier pass rejected because evidence did not match the current files",
                    manifest,
                )
        if verifier_result.status == "pass":
            self._fault(
                "verified_subtask_f2_pre",
                task_id=task_id,
                plan_item_id=int(item["plan_item_id"]),
                execution_checkpoint_id=execution_checkpoint_id,
            )
            is_final = self.verified_subtask_dag is None or not any(
                other["status"] != "completed"
                for other in self.store.get_active_plan(task_id)["items"]
                if int(other["plan_item_id"]) != int(item["plan_item_id"])
            )
            self.store.commit_verified_subtask(
                task_id=task_id,
                plan_item_id=int(item["plan_item_id"]),
                subtask_id=config.subtask_id,
                completion_summary=completion_summary,
                verifier_summary=verifier_result.summary,
                evidence_manifest=manifest,
                verifier_id=config.verifier_id,
                verifier_version=config.verifier_version,
                verification_rule=config.verification_rule,
                verifier_bundle_hash=config.verifier_bundle_hash,
                verifier_implementation_hash=config.verifier_implementation_hash,
                execution_checkpoint_id=execution_checkpoint_id,
                complete_task=is_final,
                fault_injector=self._fault,
                observed_manifest=observed_snapshot.manifest,
                observation_complete=observed_snapshot.complete,
            )
            if is_final:
                recovery = self._evidence_recovery.recover(task_id)
                if recovery.invalidated:
                    return None
                return RunResult(task_id, "completed", completion_summary)
            return None

        self.store.record_non_authoritative_verifier_run(
            task_id=task_id,
            plan_item_id=int(item["plan_item_id"]),
            subtask_id=config.subtask_id,
            status=verifier_result.status,
            summary=verifier_result.summary,
            completion_summary=completion_summary,
            evidence_manifest=manifest,
            verifier_id=config.verifier_id,
            verifier_version=config.verifier_version,
            verification_rule=config.verification_rule,
            verifier_bundle_hash=config.verifier_bundle_hash,
            verifier_implementation_hash=config.verifier_implementation_hash,
            execution_checkpoint_id=execution_checkpoint_id,
        )
        self._append_verified_feedback(
            task_id,
            messages,
            turn,
            self._verifier_feedback(verifier_result.status, verifier_result.summary),
            active_subtask_id=(
                str(item["subtask_id"]) if self.verified_subtask_dag is not None else None
            ),
        )
        if self.verified_subtask_dag is not None and int(item["consumed_turns"]) >= int(
            item["max_turns"]
        ):
            return self._fail_dag_budget(task_id, item, messages, turn)
        self.store.start_plan_item(int(item["plan_item_id"]))
        return None

    def run(self, prompt: str) -> RunResult:
        task_id = f"task_{uuid.uuid4().hex}"
        messages = [{"role": "user", "content": prompt}]
        self.store.bootstrap_task(
            task_id,
            str(self.repo_root),
            prompt,
            self.model_name,
            messages,
            {"turn": 0},
            fault_injector=self._fault,
        )
        if self.verified_subtask is not None:
            plan_id = self.store.create_plan(
                task_id,
                [{
                    "subtask_id": self.verified_subtask.subtask_id,
                    "description": self.verified_subtask.description,
                    "verifier_bundle_hash": self.verified_subtask.verifier_bundle_hash,
                }],
            )
            self.store.start_plan_item(self.store.list_plan_items(plan_id)[0]["plan_item_id"])
        elif self.verified_subtask_dag is not None:
            self.store.create_plan(
                task_id,
                [
                    {
                        "subtask_id": node.subtask_id,
                        "description": node.description,
                        "blocked_by": list(node.blocked_by),
                        "verifier_bundle_hash": node.verifier_bundle_hash,
                        "max_turns": node.max_turns,
                    }
                    for node in self.verified_subtask_dag.nodes
                ],
                dag_hash=self.verified_subtask_dag.dag_hash,
            )
        return self._run_task(task_id, messages, turn=0)

    def resume(self, task_id: str) -> RunResult:
        latest_plan = self.store.get_latest_plan(task_id)
        if self.verified_subtask_dag is not None or (
            latest_plan is not None and latest_plan.get("dag_hash") is not None
        ):
            self._assert_dag_config(task_id)
            return self._resume_verified_task(task_id)
        elif self.verified_subtask is not None or self.store.has_verified_subtask(task_id):
            self._assert_verified_subtask_config(task_id)
            return self._resume_verified_task(task_id)
        return self._resume_task(task_id, lease_acquired=False)

    def _resume_verified_task(self, task_id: str) -> RunResult:
        """Run evidence recovery before terminal fast paths or DAG selection."""
        self.store.assert_invariants(task_id)
        task = self.store.get_task(task_id)
        if task["status"] in {"failed", "aborted"}:
            raise RuntimeError(f"Task {task_id} is terminal: {task['status']}")
        self._acquire(task_id)
        try:
            self.store.assert_invariants(task_id)
            self._evidence_recovery.recover(task_id)
            task = self.store.get_task(task_id)
            if task["status"] == "completed":
                self.store.assert_invariants(task_id)
                completed = self.store.get_completed_result(task_id)
                return RunResult(task_id, "completed", completed["final_text"])
            return self._resume_task(
                task_id,
                lease_acquired=True,
                scoped_resume=self.scoped_context_budget is not None,
            )
        finally:
            if self._lease_token is not None:
                self._release()

    def _assert_verified_subtask_config(self, task_id: str) -> dict[str, Any]:
        latest_plan = self.store.get_latest_plan(task_id)
        if latest_plan is not None and latest_plan.get("dag_hash") is not None:
            raise RuntimeError(f"Task {task_id} requires its frozen verified-subtask DAG configuration")
        if self.verified_subtask is None:
            raise RuntimeError(
                f"Task {task_id} requires its frozen verified-subtask configuration to resume"
            )
        item = self.store.get_plan_item(task_id, self.verified_subtask.subtask_id)
        if item is None:
            raise RuntimeError(f"Verified subtask is missing from task {task_id}")
        if item.get("verifier_bundle_hash") != self.verified_subtask.verifier_bundle_hash:
            raise RuntimeError(f"Verifier bundle hash mismatch for task {task_id}")
        return item

    def _verified_plan_item(self, task_id: str) -> dict[str, Any]:
        return self._assert_verified_subtask_config(task_id)

    def _resume_task(
        self,
        task_id: str,
        lease_acquired: bool,
        *,
        scoped_resume: bool = False,
    ) -> RunResult:
        acquired_here = False
        if not lease_acquired:
            self._acquire(task_id)
            acquired_here = True
        try:
            self.store.assert_invariants(task_id)
            task = self.store.get_task(task_id)
            if task["status"] in {"completed", "failed", "aborted"}:
                raise RuntimeError(f"Task {task_id} is terminal: {task['status']}")
            self.store.recover_model_checkpoint(task_id)
            task = self.store.get_task(task_id)
            checkpoint_id = task.get("checkpoint_id")
            if checkpoint_id is None:
                raise RuntimeError(f"Task {task_id} has no checkpoint")
            checkpoint = self.store.get_checkpoint(checkpoint_id)
            phase = checkpoint["phase"]
            if task["status"] in {"waiting_approval", "needs_review"} and phase == task["status"]:
                result = RunResult(task_id, task["status"], error=task.get("last_error"))
                if acquired_here and self._lease_token is not None:
                    self._release()
                return result
            reconciliation = self._reconcile_task_blockers(task, checkpoint)
            if reconciliation is not None:
                if acquired_here and self._lease_token is not None:
                    self._release()
                return reconciliation
            pending = self._pending_calls(checkpoint["messages"]) if phase in {
                "model_responded", "waiting_approval", "needs_review"
            } else None
            return self._run_task(
                task_id,
                checkpoint["messages"],
                int(checkpoint["cursor"].get("turn", 0)),
                pending_calls=pending,
                resume_recovery=True,
                lease_acquired=True,
                scoped_resume=scoped_resume and self.verified_subtask_dag is not None,
            )
        except Exception:
            if acquired_here and self._lease_token is not None:
                self._release()
            raise

    def _reconcile_task_blockers(self, task: dict[str, Any], checkpoint: dict[str, Any]) -> RunResult | None:
        """Recover a task with an unresolved effect without calling the model."""
        blockers = self.store.list_blocking_reservations(str(self.repo_root), task["task_id"])
        if not blockers:
            return None
        blocker = blockers[0]
        if blocker.get("effect") == "file_write":
            # File effects have a durable before/expected-after hash path. Let
            # _reconcile_running_call verify that post-state before deciding
            # whether review is required; do not mask a safe hash recovery.
            return None
        tool_use_id = str(blocker["tool_use_id"])
        call = self.store.get_tool_call(task["task_id"], tool_use_id)
        if call is None:
            raise InvariantViolation(f"Missing tool call for blocking reservation: {blocker['reservation_id']}")
        reason = (
            "Repository effect reservation requires reconciliation before resume: "
            f"reservation={blocker['reservation_id']} state={blocker['state']}"
        )
        self.store.transition_review(
            task["task_id"],
            tool_use_id,
            "needs_review",
            checkpoint["messages"],
            {**checkpoint["cursor"], "pending_tool_use_id": tool_use_id},
            reason,
            unknown_effect={
                "unknown_effect": True,
                "reservation_id": blocker["reservation_id"],
                "reservation_state": blocker["state"],
            },
        )
        return RunResult(task["task_id"], "needs_review", error=reason)

    def approve(self, tool_use_id: str) -> RunResult:
        call = self.store.find_tool_call(tool_use_id)
        task_id = call["task_id"]
        self._acquire(task_id)
        try:
            call = self.store.find_tool_call(tool_use_id)
            if call["status"] != "waiting_approval":
                raise RuntimeError(f"Tool call is not waiting for approval: {tool_use_id}")
            task = self.store.get_task(task_id)
            self.store.resolve_review(task_id, tool_use_id, "approve", int(call["version"]), int(task["version"]))
            return self._resume_task(task_id, lease_acquired=True)
        finally:
            if self._lease_token is not None:
                self._release()

    def deny(self, tool_use_id: str) -> RunResult:
        call = self.store.find_tool_call(tool_use_id)
        task_id = call["task_id"]
        self._acquire(task_id)
        try:
            call = self.store.find_tool_call(tool_use_id)
            if call["status"] != "waiting_approval":
                raise RuntimeError(f"Tool call is not waiting for approval: {tool_use_id}")
            task = self.store.get_task(task_id)
            self.store.resolve_review(task_id, tool_use_id, "deny", int(call["version"]), int(task["version"]))
            return self._resume_task(task_id, lease_acquired=True)
        finally:
            if self._lease_token is not None:
                self._release()

    def resolve_call(self, tool_use_id: str, action: str) -> RunResult:
        call = self.store.find_tool_call(tool_use_id)
        task_id = call["task_id"]
        self._acquire(task_id)
        try:
            call = self.store.find_tool_call(tool_use_id)
            task = self.store.get_task(task_id)
            if action == "abort":
                self._assert_review_call(task_id, tool_use_id)
                self.store.abort_task(
                    task_id,
                    tool_use_id,
                    "Aborted during review",
                    expected_tool_version=int(call["version"]),
                    expected_task_version=int(task["version"]),
                )
                return RunResult(task_id, "aborted", error="Aborted during review")
            if action not in {"retry", "complete"}:
                raise ValueError("action must be retry, complete, or abort")
            operation = self.store.get_operation_for_tool_call(task_id, tool_use_id)
            if operation is not None:
                evidence = ReconcileEvidence(
                    "safe_to_retry" if action == "retry" else "operator_confirmed",
                    "operator supplied the explicit resolve-call decision",
                    {"source": "operator", "action": action},
                )
                self.store.resolve_operation(
                    operation["operation_id"],
                    action,
                    evidence,
                    expected_version=int(operation["version"]),
                )
                return self._resume_task(task_id, lease_acquired=True)
            self.store.resolve_review(task_id, tool_use_id, action, int(call["version"]), int(task["version"]))
            return self._resume_task(task_id, lease_acquired=True)
        finally:
            if self._lease_token is not None:
                self._release()

    def _assert_review_call(self, task_id: str, tool_use_id: str) -> None:
        call = self.store.get_tool_call(task_id, tool_use_id)
        if call is None or call["status"] != "needs_review":
            raise RuntimeError(f"Tool call is not awaiting review: {tool_use_id}")

    def _acquire(self, task_id: str) -> None:
        token = self.store.acquire_lease(str(self.repo_root), task_id, self.owner_id, ttl=self.lease_ttl)
        if token is None:
            raise RuntimeError(f"Repository is leased by another active task: {self.repo_root}")
        self._lease_token = int(token)
        self.store.bind_lease(str(self.repo_root), self.owner_id, self._lease_token)

    def _release(self) -> None:
        token = self._lease_token
        try:
            self.store.release_lease(str(self.repo_root), self.owner_id, fencing_token=token)
        finally:
            self.store.clear_lease()
            self._lease_token = None

    def _run_task(self, task_id: str, messages: list[dict], turn: int,
                  pending_calls: list[ToolCall] | None = None,
                  resume_recovery: bool = False,
                  lease_acquired: bool = False,
                  scoped_resume: bool = False) -> RunResult:
        recovered_completion_text: str | None = None
        verified_marker_checkpoint_id: int | None = None
        verified_marker_subtask_id: str | None = None
        scoped_projection: ScopedContextProjection | None = None
        scoped_boundary_message_count: int | None = None
        scoped_pending_tool_use_message: dict[str, Any] | None = None
        scoped_tool_result_pair: list[dict[str, Any]] | None = None
        scoped_tool_result_tail_error: str | None = None
        self._pending_review = None
        previous_active_task = self._active_task_id
        self._active_task_id = task_id
        if not lease_acquired:
            self._acquire(task_id)
        elif self._lease_token is None:
            raise LeaseLost(f"No lease is bound for {task_id}")
        if resume_recovery:
            current_task = self.store.get_task(task_id)
            if current_task["status"] in {"completed", "failed", "aborted"}:
                self._release()
                raise RuntimeError(f"Task {task_id} is terminal: {current_task['status']}")
            current_checkpoint_id = current_task.get("checkpoint_id")
            if current_checkpoint_id is None:
                self._release()
                raise RuntimeError(f"Task {task_id} has no checkpoint")
            current_checkpoint = self.store.get_checkpoint(current_checkpoint_id)
            messages = current_checkpoint["messages"]
            turn = int(current_checkpoint["cursor"].get("turn", 0))
            phase = current_checkpoint["phase"]
            pending_calls = self._pending_calls(messages) if phase in {
                "model_responded", "waiting_approval", "needs_review"
            } else None
            if scoped_resume and self.verified_subtask_dag is not None:
                # The checkpoint recovered above is the resume boundary. Any
                # messages appended while replaying a pending response/tool
                # belong to the scoped tail; pre-interruption history never
                # enters the model view. Keep only the corresponding assistant
                # tool-use blocks so replayed tool results remain structurally
                # paired without restoring unrelated conversation history.
                scoped_boundary_message_count = len(messages)
                if pending_calls and messages and messages[-1].get("role") == "assistant":
                    pending_ids = {call.id for call in pending_calls}
                    tool_use_blocks = [
                        copy.deepcopy(block)
                        for block in messages[-1].get("content", [])
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and str(block.get("id")) in pending_ids
                        )
                    ]
                    if tool_use_blocks:
                        scoped_pending_tool_use_message = {
                            "role": "assistant",
                            "content": tool_use_blocks,
                        }
                elif phase == "tool_results_appended":
                    scoped_tool_result_pair = self._latest_tool_result_pair(messages)
                    if not scoped_tool_result_pair:
                        scoped_tool_result_tail_error = (
                            "latest tool_results_appended checkpoint tail is not a "
                            "valid adjacent assistant/user pair"
                        )
            if phase == "model_responded" and not pending_calls:
                last_text = self._last_text(messages)
                if self._uses_verified_subtasks() and self._has_completion_marker(last_text):
                    verified_marker_checkpoint_id = int(current_checkpoint["checkpoint_id"])
                    if self.verified_subtask_dag is not None:
                        verified_marker_subtask_id = current_checkpoint["cursor"].get(
                            "active_subtask_id"
                        )
                elif not self._uses_verified_subtasks():
                    recovered_completion_text = last_text
        try:
            if resume_recovery and self.store.abandon_open_model_calls(task_id):
                self.store.append_event(task_id, "model_call_abandoned", {"reason": "resume"})
            self.store.update_task(task_id, status="running", clear_error=True)
        except Exception:
            self._release()
            raise
        try:
            while True:
                self.store.assert_invariants(task_id)
                if self._lease_token is None or not self.store.heartbeat_lease(
                    str(self.repo_root), self.owner_id, ttl=self.lease_ttl, fencing_token=self._lease_token
                ):
                    raise LeaseLost(f"Lease heartbeat failed for {self.repo_root}")
                self.store.update_task(task_id, status="running", clear_error=True)

                if recovered_completion_text is not None:
                    self.store.complete_task(
                        task_id,
                        messages,
                        {"turn": turn},
                        recovered_completion_text,
                        fault_injector=self._fault,
                    )
                    return RunResult(task_id, "completed", recovered_completion_text)

                if verified_marker_checkpoint_id is not None:
                    marker_text = self._last_text(messages)
                    marker_result = self._handle_verified_marker(
                        task_id,
                        messages,
                        turn,
                        verified_marker_checkpoint_id,
                        self._completion_summary(marker_text),
                        marker_subtask_id=verified_marker_subtask_id,
                    )
                    verified_marker_checkpoint_id = None
                    verified_marker_subtask_id = None
                    if marker_result is not None:
                        return marker_result
                    turn += 1
                    continue

                if pending_calls is not None:
                    self._append_tool_results(task_id, turn, messages, pending_calls)
                    pending_calls = None
                    turn += 1
                    continue

                current_item: dict[str, Any] | None = None
                if self.verified_subtask is not None:
                    current_item = self._verified_plan_item(task_id)
                    if current_item["status"] == "retryable":
                        self.store.start_plan_item(int(current_item["plan_item_id"]))
                        current_item = self._verified_plan_item(task_id)
                elif self.verified_subtask_dag is not None:
                    current_item = self._select_dag_item(task_id)
                    if current_item is None:
                        # A prior F4b recovery intentionally stops after the
                        # first invalid completed node.  Before declaring the
                        # DAG exhausted, rescan the remaining completed
                        # nodes so an unrelated stale branch cannot be
                        # trusted as a terminal plan.
                        self._evidence_recovery.recover(task_id)
                        current_item = self._select_dag_item(task_id)
                    if current_item is None:
                        raise InvariantViolation(
                            f"Frozen DAG for task {task_id} has no executable subtask"
                        )
                    if current_item["status"] == "retryable" and not self.store.plan_item_dependencies_complete(
                        int(current_item["plan_item_id"])
                    ):
                        self.store.start_plan_item(
                            int(current_item["plan_item_id"]),
                            check_dependencies=False,
                        )
                        current_item = self.store.get_plan_item(
                            task_id, str(current_item["subtask_id"])
                        )
                        if current_item is None:
                            raise InvariantViolation(
                                f"Frozen DAG subtask disappeared: {task_id}"
                            )
                        blocked_result = self._handle_blocked_dag_marker(
                            task_id, current_item, messages, turn
                        )
                        if blocked_result is not None:
                            return blocked_result
                        turn += 1
                        continue
                    if current_item["status"] != "in_progress":
                        raise InvariantViolation(
                            f"Frozen DAG subtask {current_item['subtask_id']} is not in progress"
                        )
                    if int(current_item["consumed_turns"]) >= int(current_item["max_turns"]):
                        return self._fail_dag_budget(task_id, current_item, messages, turn)

                checkpoint_id = self.store.get_task(task_id)["checkpoint_id"]
                if (
                    scoped_resume
                    and self.verified_subtask_dag is not None
                    and self._scoped_resume is not None
                    and self.scoped_context_budget is not None
                    and current_item is not None
                ):
                    if scoped_boundary_message_count is None:
                        scoped_boundary_message_count = len(messages)
                    post_resume_messages = messages[scoped_boundary_message_count:]
                    if scoped_pending_tool_use_message is not None:
                        post_resume_messages = [
                            scoped_pending_tool_use_message,
                            *post_resume_messages,
                        ]
                    if scoped_tool_result_pair is not None:
                        post_resume_messages = [
                            *scoped_tool_result_pair,
                            *post_resume_messages,
                        ]
                    try:
                        if scoped_tool_result_tail_error is not None:
                            raise ScopedContextError(scoped_tool_result_tail_error)
                        if (
                            scoped_projection is None
                            or scoped_projection.metrics.get("resume_unit_id")
                            != current_item.get("subtask_id")
                        ):
                            scoped_projection = self._scoped_resume.build(
                                task_id,
                                current_item,
                                post_resume_messages,
                                self.scoped_context_budget,
                                source_checkpoint_id=checkpoint_id,
                            )
                        else:
                            scoped_projection = scoped_projection.with_post_resume_messages(
                                post_resume_messages,
                                source_checkpoint_id=checkpoint_id,
                            )
                    except ScopedContextError as exc:
                        error = f"verified-scoped context construction failed: {exc}"
                        self.store.append_event(
                            task_id,
                            "verified_scoped_context_failed",
                            {
                                "error": error,
                                "resume_unit_id": current_item.get("subtask_id"),
                                "reason": str(exc),
                            },
                        )
                        self.store.fail_plan_item_and_task(
                            task_id,
                            int(current_item["plan_item_id"]),
                            messages,
                            {
                                "turn": turn,
                                "active_subtask_id": current_item["subtask_id"],
                            },
                            error,
                        )
                        return RunResult(task_id, "failed", error=error)
                    if scoped_projection.metrics.get("overflow"):
                        estimated = int(scoped_projection.metrics["token_estimate"])
                        budget = int(self.scoped_context_budget)
                        error = (
                            "verified-scoped context budget overflow: "
                            f"estimated {estimated} tokens exceeds budget {budget} "
                            f"for resume unit {current_item['subtask_id']}"
                        )
                        self.store.append_event(
                            task_id,
                            "verified_scoped_context_overflow",
                            {
                                **scoped_projection.metrics,
                                "error": error,
                                "resume_unit_id": current_item["subtask_id"],
                                "outcome": "overflow",
                            },
                        )
                        self.store.fail_plan_item_and_task(
                            task_id,
                            int(current_item["plan_item_id"]),
                            messages,
                            {
                                "turn": turn,
                                "active_subtask_id": current_item["subtask_id"],
                            },
                            error,
                        )
                        return RunResult(task_id, "failed", error=error)
                    projection = scoped_projection
                else:
                    projection = self.projector.project(task_id, messages, checkpoint_id)
                projected_messages = projection.messages
                active_tool_scope = self._effective_tool_scope()
                tool_schemas = [
                    schema for schema in self.tool_registry.schemas()
                    if active_tool_scope is None or schema["name"] in active_tool_scope
                ]
                tool_schemas.extend(INTERNAL_TOOL_SCHEMAS.values())
                request = {"messages": messages, "tools": tool_schemas}
                if self.verified_subtask_dag is not None and current_item is not None:
                    request["active_subtask_id"] = current_item["subtask_id"]
                    reserved_turn = self.store.reserve_plan_item_turn(
                        int(current_item["plan_item_id"])
                    )
                    if reserved_turn is None:
                        return self._fail_dag_budget(task_id, current_item, messages, turn)
                    current_item = {**current_item, "consumed_turns": reserved_turn}
                model_call_id = self.store.create_model_call(
                    task_id,
                    turn,
                    request,
                    source_checkpoint_id=projection.source_checkpoint_id,
                    projection=projection.persisted(),
                )
                self._fault("before_model_call", task_id=task_id, turn=turn)
                try:
                    response: ModelResponse = self.model.complete(projected_messages, tool_schemas)
                except Exception as exc:
                    self.store.finish_model_call(model_call_id, error=f"{type(exc).__name__}: {exc}")
                    self.store.append_event(task_id, "model_failed", {"error": str(exc), "turn": turn})
                    self.store.fail_task(task_id, messages, {"turn": turn}, str(exc))
                    return RunResult(task_id, "failed", error=str(exc))
                self._fault("after_model_response_before_persist", task_id=task_id, turn=turn)
                try:
                    response_payload = response.as_dict()
                    if len(json.dumps(response_payload, ensure_ascii=False).encode("utf-8")) > 2 * 1024 * 1024:
                        raise ValueError("model response exceeds 2097152 bytes")
                except ValueError as exc:
                    self.store.finish_model_call(model_call_id, error=str(exc))
                    self.store.append_event(task_id, "model_failed", {"error": str(exc), "turn": turn})
                    self.store.fail_task(task_id, messages, {"turn": turn}, str(exc))
                    return RunResult(task_id, "failed", error=str(exc))
                self.store.finish_model_call(model_call_id, response=response.as_dict())
                self.store.append_event(task_id, "model_response", {
                    "model_call_id": model_call_id,
                    "turn": turn,
                    "stop_reason": response.stop_reason,
                    "usage": response.usage,
                })
                messages.append({"role": "assistant", "content": response.content})
                self._fault("after_model_response", task_id=task_id, turn=turn)
                checkpoint_cursor: dict[str, Any] = {"turn": turn}
                if self.verified_subtask_dag is not None and current_item is not None:
                    checkpoint_cursor["active_subtask_id"] = current_item["subtask_id"]
                execution_checkpoint_id = self.store.save_checkpoint(
                    task_id, "model_responded", messages, checkpoint_cursor
                )

                if (
                    self._uses_verified_subtasks()
                    and response.tool_calls
                    and self._has_completion_marker(response.text)
                ):
                    error = "Completion marker cannot be combined with tool calls"
                    if self.verified_subtask_dag is not None and current_item is not None:
                        self.store.fail_plan_item_and_task(
                            task_id,
                            int(current_item["plan_item_id"]),
                            messages,
                            {
                                "turn": turn,
                                "active_subtask_id": current_item["subtask_id"],
                            },
                            error,
                        )
                    else:
                        self.store.fail_task(task_id, messages, {"turn": turn}, error)
                    return RunResult(task_id, "failed", error=error)

                if not response.tool_calls:
                    text = response.text
                    if self._uses_verified_subtasks():
                        if self._has_completion_marker(text):
                            marker_result = self._handle_verified_marker(
                                task_id,
                                messages,
                                turn,
                                execution_checkpoint_id,
                                self._completion_summary(text),
                                marker_subtask_id=(
                                    str(current_item["subtask_id"])
                                    if self.verified_subtask_dag is not None and current_item is not None
                                    else None
                                ),
                            )
                            if marker_result is not None:
                                return marker_result
                            turn += 1
                            continue
                        if self.verified_subtask_dag is not None and current_item is not None and int(
                            current_item["consumed_turns"]
                        ) >= int(current_item["max_turns"]):
                            return self._fail_dag_budget(task_id, current_item, messages, turn)
                        self._append_verified_feedback(
                            task_id,
                            messages,
                            turn,
                            "The subtask is not complete. Emit SUBTASK_COMPLETE on its own line "
                            "after satisfying the completion criteria.",
                            active_subtask_id=(
                                str(current_item["subtask_id"])
                                if self.verified_subtask_dag is not None and current_item is not None
                                else None
                            ),
                        )
                        turn += 1
                        continue
                    self.store.complete_task(task_id, messages, {"turn": turn}, text, fault_injector=self._fault)
                    return RunResult(task_id, "completed", text)
                self._append_tool_results(task_id, turn, messages, response.tool_calls)
                turn += 1
        except WaitingForApproval as exc:
            pending = self._pending_review
            tool_use_id = pending[1] if pending and pending[0] == "waiting_approval" else exc.tool_use_id
            reason = pending[2] if pending and pending[0] == "waiting_approval" else exc.reason
            self.store.transition_review(
                task_id,
                tool_use_id,
                "waiting_approval",
                messages,
                {"turn": turn, "pending_tool_use_id": tool_use_id},
                reason,
                fault_injector=self._fault,
            )
            self._pending_review = None
            return RunResult(task_id, "waiting_approval", error=reason)
        except NeedsReview as exc:
            pending = self._pending_review
            tool_use_id = pending[1] if pending and pending[0] == "needs_review" else exc.tool_use_id
            reason = pending[2] if pending and pending[0] == "needs_review" else exc.reason
            reservation = self.store.get_effect_reservation(task_id, tool_use_id)
            unknown_effect = None
            if reservation is not None and reservation["state"] in {"running", "unknown"}:
                unknown_effect = {
                    "unknown_effect": True,
                    "reservation_id": reservation["reservation_id"],
                    "reservation_state": reservation["state"],
                }
            self.store.transition_review(
                task_id,
                tool_use_id,
                "needs_review",
                messages,
                {"turn": turn, "pending_tool_use_id": tool_use_id},
                reason,
                unknown_effect=unknown_effect,
                fault_injector=self._fault,
            )
            self._pending_review = None
            return RunResult(task_id, "needs_review", error=reason)
        except (InjectedCrash, LeaseLost, InvariantViolation):
            raise
        except Exception as exc:
            self.store.fail_task(task_id, messages, {"turn": turn}, str(exc))
            return RunResult(task_id, "failed", error=str(exc))
        finally:
            self._release()
            self._active_task_id = previous_active_task

    def _append_tool_results(self, task_id: str, turn: int, messages: list[dict], calls: list[ToolCall]) -> None:
        results: list[dict[str, Any]] = []
        for call in calls:
            output = self._execute_call(task_id, turn, call, messages)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": output})
        messages.append({"role": "user", "content": results})
        self.store.save_checkpoint(task_id, "tool_results_appended", messages, {"turn": turn + 1})

    def spawn_subagent(self, prompt: str, role: str = "assistant", wait: bool = True,
                       tool_scope: set[str] | None = None,
                       context_window: int | None = None,
                       model: str | None = None,
                       parent_task_id: str | None = None) -> dict[str, Any]:
        if parent_task_id is None:
            parent_task_id = self._active_task_id
        return self._spawn_subagent(
            parent_task_id,
            str(prompt),
            role=role,
            wait=wait,
            tool_scope=tool_scope,
            context_window=context_window,
            model=model,
        )

    def run_subagent(self, run_id: str, tool_scope: set[str] | None = None) -> RunResult:
        run = self.store.get_subagent_run(run_id)
        if run is None:
            raise KeyError(f"Subagent run not found: {run_id}")
        child_task_id = str(run["child_task_id"])
        if run["status"] in {"completed", "failed", "cancelled"}:
            return RunResult(
                child_task_id,
                run["status"],
                final_text=run.get("result_summary") or "",
                error=run.get("error"),
            )
        if run["status"] == "needs_review":
            return RunResult(child_task_id, "needs_review", error=run.get("error"))
        token = self._subagent_fencing.get(run_id)
        if token is None:
            token = f"run-{uuid.uuid4().hex}"
        lease_acquired_here = False
        try:
            self._acquire(child_task_id)
            lease_acquired_here = True
            claimed = self.store.claim_subagent_run(run_id, self.owner_id, token)
            self._subagent_fencing[run_id] = token
            child_task_id = str(claimed["child_task_id"])
            scope = self._normalize_tool_scope(tool_scope)
            if scope is None:
                scope = self._effective_tool_scope()
            if scope is not None:
                self._subagent_tool_scopes[child_task_id] = scope
            else:
                scope = self._subagent_tool_scopes.get(child_task_id)
                if scope is None:
                    scope = self._subagent_tool_scope_from_messages(claimed.get("messages") or [])
                    if scope is not None:
                        self._subagent_tool_scopes[child_task_id] = scope
            previous_task = self._active_task_id
            previous_scope = self._active_tool_scope
            previous_run = self._active_subagent_run_id
            self._active_subagent_run_id = run_id
            self._active_tool_scope = scope
            try:
                metadata = self._subagent_orchestration_metadata(list(claimed.get("messages") or []))
                context_window = int(metadata["context_window"] or DEFAULT_SUBAGENT_CONTEXT_WINDOW)
                self._check_subagent_context_budget(child_task_id, context_window)
                child_result = self._resume_task(child_task_id, lease_acquired=True)
            finally:
                self._active_task_id = previous_task
                self._active_tool_scope = previous_scope
                self._active_subagent_run_id = previous_run
        finally:
            if lease_acquired_here and self._lease_token is not None:
                self._release()
        if child_result.status in {"completed", "failed", "needs_review"}:
            self._settle_subagent_run(run_id, claimed, child_result, self._child_messages(child_task_id))
        return child_result

    def resume_subagent(self, run_id: str, tool_scope: set[str] | None = None) -> RunResult:
        run = self.store.get_subagent_run(run_id)
        if run is None:
            raise KeyError(f"Subagent run not found: {run_id}")
        child_task_id = str(run["child_task_id"])
        if run["status"] in {"completed", "failed", "cancelled"}:
            return RunResult(
                child_task_id,
                run["status"],
                final_text=run.get("result_summary") or "",
                error=run.get("error"),
            )
        if run["status"] == "needs_review":
            return RunResult(child_task_id, "needs_review", error=run.get("error"))
        return self.run_subagent(run_id, tool_scope=tool_scope)

    def approve_subagent_plan(self, approval_id: str, approve: bool = True,
                              reason: str | None = None) -> RunResult:
        approval = self.store.get_plan_approval(approval_id)
        if approval is None:
            raise KeyError(f"Plan approval not found: {approval_id}")
        run = self.store.get_subagent_run(approval["subagent_run_id"])
        if run is None:
            raise KeyError(f"Subagent run not found: {approval['subagent_run_id']}")
        child_task_id = str(run["child_task_id"])
        tool_use_id = self._find_plan_approval_call(child_task_id)
        if tool_use_id is None:
            raise RuntimeError(f"Child task has no pending plan approval call: {child_task_id}")
        call = self.store.get_tool_call(child_task_id, tool_use_id)
        task = self.store.get_task(child_task_id)
        if task["status"] != "needs_review" or call is None or call["status"] != "needs_review":
            raise StaleState(f"Child task is not awaiting plan approval: {child_task_id}")
        if not approve:
            decision_reason = reason or "Plan rejected"
            self.store.transition_plan_approval(
                approval_id,
                "rejected",
                decided_by=self.owner_id,
                reason=decision_reason,
                expected_version=int(approval["version"]),
            )
            self.store.cancel_subagent_run(
                run["subagent_run_id"],
                decision_reason,
                expected_version=int(run["version"]),
                fencing_token=str(run["fencing_token"]),
            )
            self.store.abort_task(
                child_task_id,
                tool_use_id,
                decision_reason,
                expected_tool_version=int(call["version"]),
                expected_task_version=int(task["version"]),
            )
            return RunResult(child_task_id, "cancelled", error=decision_reason)
        self.store.transition_plan_approval(
            approval_id,
            "approved",
            decided_by=self.owner_id,
            reason=reason,
            expected_version=int(approval["version"]),
        )
        self.store.resolve_review(
            child_task_id,
            tool_use_id,
            "complete",
            int(call["version"]),
            int(task["version"]),
        )
        output = json.dumps(
            {"approved": True, "approval_id": approval_id, "reason": reason},
            ensure_ascii=False,
            sort_keys=True,
        )
        self.store.update_tool_call(
            child_task_id,
            tool_use_id,
            status="succeeded",
            output=output,
            finished_at=time.time(),
        )
        checkpoint = self.store.get_checkpoint(task["checkpoint_id"])
        messages = list(checkpoint.get("messages") or [])
        turn = int(checkpoint.get("cursor", {}).get("turn", 0))
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": output}],
        })
        self.store.save_checkpoint(child_task_id, "tool_results_appended", messages, {"turn": turn + 1})
        updated_run = self.store.update_subagent_run(
            run["subagent_run_id"],
            status="running",
            expected_version=int(run["version"]),
            fencing_token=str(run["fencing_token"]),
        )
        child_result = self.resume(child_task_id)
        if child_result.status in {"completed", "failed", "needs_review"}:
            self._settle_subagent_run(
                run["subagent_run_id"],
                updated_run,
                child_result,
                self._child_messages(child_task_id),
            )
        return child_result

    def _spawn_subagent(self, parent_task_id: str, prompt: str, role: str = "assistant",
                        wait: bool = True, tool_scope: set[str] | None = None,
                        context_window: int | None = None,
                        model: str | None = None,
                        run_id: str | None = None,
                        child_task_id: str | None = None) -> dict[str, Any]:
        parent_depth = self._subagent_depth(parent_task_id)
        if parent_depth + 1 > MAX_SUBAGENT_DEPTH:
            raise RuntimeError(
                f"Subagent depth limit exceeded: parent depth {parent_depth}, "
                f"max depth {MAX_SUBAGENT_DEPTH}"
            )
        run_id = run_id or f"subagent_{uuid.uuid4().hex}"
        child_task_id = child_task_id or f"task_{uuid.uuid4().hex}"
        scope = self._normalize_tool_scope(tool_scope)
        if scope is None:
            scope = self._effective_tool_scope()
        child_model = self._resolve_child_model(parent_task_id, model)
        self._ensure_subagent_child(
            parent_task_id,
            run_id,
            child_task_id,
            prompt,
            role,
            scope,
            context_window=context_window,
            model=child_model,
        )
        if not wait:
            return {
                "run_id": run_id,
                "child_task_id": child_task_id,
                "status": "pending",
                "result_summary": None,
                "error": None,
                "token_usage": self._child_token_usage(child_task_id),
            }
        had_lease = self._lease_token is not None
        if had_lease:
            self._release()
        try:
            child_result = self.run_subagent(run_id)
        except Exception as exc:
            child_result = RunResult(child_task_id, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if had_lease:
                self._acquire(parent_task_id)
        return {
            "run_id": run_id,
            "child_task_id": child_task_id,
            "status": child_result.status,
            "result_summary": child_result.final_text,
            "error": child_result.error,
            "token_usage": self._child_token_usage(child_task_id),
        }

    def _ensure_subagent_child(self, parent_task_id: str, run_id: str, child_task_id: str,
                               prompt: str, role: str, tool_scope: set[str] | None,
                               context_window: int | None = None,
                               model: str | None = None) -> None:
        context_window = int(context_window or DEFAULT_SUBAGENT_CONTEXT_WINDOW)
        if context_window < 1:
            raise ValueError("context_window must be a positive integer")
        existing_run = self.store.get_subagent_run(run_id)
        if existing_run is not None:
            persisted = self._subagent_orchestration_metadata(list(existing_run.get("messages") or []))
            if persisted["context_window"] is not None:
                context_window = int(persisted["context_window"])
            if persisted["tool_scope"] is not None:
                tool_scope = persisted["tool_scope"]
            if persisted["model"]:
                model = persisted["model"]
        try:
            self.store.get_task(child_task_id)
        except KeyError:
            self.store.bootstrap_task(
                child_task_id,
                str(self.repo_root),
                prompt,
                model or self.model_name,
                [{"role": "user", "content": prompt}],
                {"turn": 0},
                fault_injector=self._fault,
            )
        if existing_run is None:
            self.store.create_subagent_run(
                run_id,
                parent_task_id,
                child_task_id,
                str(self.repo_root),
                role,
                self.owner_id,
                self._subagent_orchestration_messages(
                    prompt,
                    tool_scope,
                    context_window=context_window,
                    model=model or self.model_name,
                ),
            )
        self._subagent_tool_scopes[child_task_id] = tool_scope
        self._check_subagent_context_budget(child_task_id, context_window)

    def _settle_subagent_run(self, run_id: str, claimed: dict[str, Any],
                             child_result: RunResult, child_messages: list[dict]) -> None:
        version = int(claimed["version"])
        token = str(claimed["fencing_token"])
        if child_result.status == "completed":
            summary = self._last_text(child_messages) or child_result.final_text
            self.store.complete_subagent_run(run_id, summary, child_messages, version, token)
        elif child_result.status == "failed":
            self.store.fail_subagent_run(run_id, child_result.error or "Subagent failed", version, token)
        elif child_result.status == "needs_review":
            current = self.store.get_subagent_run(run_id)
            if current is not None and current["status"] != "needs_review":
                self.store.mark_subagent_needs_review(
                    run_id,
                    child_result.error or "Plan approval required",
                    version,
                    token,
                )

    def _child_messages(self, task_id: str) -> list[dict]:
        task = self.store.get_task(task_id)
        checkpoint_id = task.get("checkpoint_id")
        if checkpoint_id is None:
            return []
        checkpoint = self.store.get_checkpoint(checkpoint_id)
        return list(checkpoint.get("messages") or [])

    def _find_plan_approval_call(self, task_id: str) -> str | None:
        for message in reversed(self._child_messages(task_id)):
            if message.get("role") != "assistant":
                continue
            for block in reversed(message.get("content", [])):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == ".agent_runtime.request_plan_approval"
                ):
                    return str(block.get("id"))
        return None

    @staticmethod
    def _normalize_tool_scope(tool_scope: Any) -> set[str] | None:
        if tool_scope is None:
            return None
        if isinstance(tool_scope, str):
            return {tool_scope}
        return {str(item) for item in tool_scope}

    @staticmethod
    def _subagent_orchestration_messages(prompt: str, tool_scope: set[str] | None,
                                         context_window: int = DEFAULT_SUBAGENT_CONTEXT_WINDOW,
                                         model: str | None = None) -> list[dict]:
        messages: list[dict] = []
        metadata: dict[str, Any] = {"context_window": int(context_window)}
        if tool_scope is not None:
            metadata["tool_scope"] = sorted(tool_scope)
        if model:
            metadata["model"] = str(model)
        messages.append({
            "role": "system",
            "content": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        })
        messages.append({"role": "user", "content": prompt})
        return messages

    @staticmethod
    def _subagent_orchestration_metadata(messages: list[dict]) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "tool_scope": None,
            "context_window": DEFAULT_SUBAGENT_CONTEXT_WINDOW,
            "model": None,
        }
        for message in messages:
            if message.get("role") != "system" or not isinstance(message.get("content"), str):
                continue
            try:
                parsed = json.loads(message["content"])
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                if isinstance(parsed.get("tool_scope"), list):
                    metadata["tool_scope"] = {str(item) for item in parsed["tool_scope"]}
                if isinstance(parsed.get("context_window"), int):
                    metadata["context_window"] = int(parsed["context_window"])
                if isinstance(parsed.get("model"), str):
                    metadata["model"] = parsed["model"]
                return metadata
        return metadata

    def _subagent_tool_scope_from_messages(self, messages: list[dict]) -> set[str] | None:
        return self._subagent_orchestration_metadata(messages)["tool_scope"]

    def _effective_tool_scope(self) -> set[str] | None:
        if self._active_tool_scope is not None:
            return self._active_tool_scope
        return self.tool_scope

    def _resolve_child_model(self, parent_task_id: str | None, requested: str | None) -> str:
        if requested:
            return str(requested)
        if parent_task_id is not None:
            try:
                parent = self.store.get_task(parent_task_id)
                return str(parent["model"])
            except KeyError:
                pass
        return self.model_name

    def _subagent_depth(self, task_id: str | None) -> int:
        child_to_parent = {
            str(run["child_task_id"]): run["parent_task_id"]
            for run in self.store.list_subagent_runs()
        }
        depth = 0
        seen: set[str] = set()
        current = task_id
        while current is not None:
            if current in seen:
                raise InvariantViolation(f"Subagent parent cycle detected at task {current}")
            seen.add(current)
            parent = child_to_parent.get(current)
            if parent is None:
                return depth
            current = str(parent)
            depth += 1
        return depth

    @staticmethod
    def _estimate_messages_tokens(messages: list[dict]) -> int:
        rendered = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        return estimate_tokens(rendered)

    def _check_subagent_context_budget(self, child_task_id: str, context_window: int) -> None:
        task = self.store.get_task(child_task_id)
        checkpoint_id = task.get("checkpoint_id")
        if checkpoint_id is None:
            raise RuntimeError(f"Subagent child has no checkpoint: {child_task_id}")
        checkpoint = self.store.get_checkpoint(int(checkpoint_id))
        estimated = self._estimate_messages_tokens(list(checkpoint.get("messages") or []))
        if estimated > int(context_window):
            raise RuntimeError(
                f"Subagent context budget exceeded: estimated {estimated} tokens "
                f"> context_window {int(context_window)}"
            )

    def _child_token_usage(self, child_task_id: str) -> dict[str, int]:
        input_tokens = 0
        output_tokens = 0
        for call in self.store.list_model_calls(child_task_id):
            if call.get("status") != "succeeded":
                continue
            input_tokens += int(call.get("input_tokens") or 0)
            output_tokens += int(call.get("output_tokens") or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def _handle_internal_tool(self, task_id: str, turn: int, call: ToolCall,
                              messages: list[dict]) -> str:
        args_hash = canonical_args_hash(call.name, call.input)
        existing = self.store.get_tool_call(task_id, call.id)
        if existing is not None:
            if existing["args_hash"] != args_hash:
                raise RuntimeError(f"Tool call ID reused with different arguments: {call.id}")
            if existing["status"] == "succeeded":
                self.store.append_event(task_id, "tool_deduplicated", {"tool_use_id": call.id})
                return existing.get("output") or "{}"
            if existing["status"] in {"denied", "failed", "aborted"}:
                raise RuntimeError(f"Terminal internal tool call cannot execute again: {call.id}")
            if existing["status"] == "needs_review":
                raise NeedsReview(call.id, existing.get("error") or "Manual review required")
            if existing["status"] == "waiting_approval":
                raise WaitingForApproval(call.id, existing.get("permission_reason") or "Approval required")
        if existing is None:
            existing = self.store.create_tool_call(
                task_id, call.id, turn, call.name, call.input, args_hash, effect="read_only"
            )
        if existing["status"] == "planned":
            self.store.start_tool_call(task_id, call.id, "read_only", started_at=time.time())
        self.store.append_event(task_id, "internal_tool_started", {
            "tool_use_id": call.id,
            "name": call.name,
        })
        self._fault("after_tool_started", task_id=task_id, tool_use_id=call.id)
        if call.name == ".agent_runtime.spawn_subagent":
            output = self._run_internal_spawn(task_id, turn, call)
        elif call.name == ".agent_runtime.request_plan_approval":
            output = self._run_internal_plan_approval(task_id, call)
        else:
            raise RuntimeError(f"Unknown internal tool: {call.name}")
        self.store.update_tool_call(task_id, call.id, status="succeeded", output=output, finished_at=time.time())
        self.store.append_event(task_id, "internal_tool_succeeded", {
            "tool_use_id": call.id,
            "name": call.name,
            "output_chars": len(output),
        })
        self._fault("after_tool_persist", task_id=task_id, tool_use_id=call.id)
        return output

    def _run_internal_spawn(self, task_id: str, turn: int, call: ToolCall) -> str:
        prompt = call.input.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("spawn_subagent requires a non-empty prompt")
        role = str(call.input.get("role") or "assistant")
        wait = bool(call.input.get("wait", True))
        scope = self._normalize_tool_scope(call.input.get("tool_scope"))
        context_window = call.input.get("context_window")
        if context_window is not None and not isinstance(context_window, int):
            raise ValueError("spawn_subagent context_window must be an integer")
        model = call.input.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError("spawn_subagent model must be a string")
        existing = self.store.get_tool_call(task_id, call.id)
        pending: dict[str, Any] | None = None
        if existing is not None and existing.get("output"):
            try:
                parsed = json.loads(existing["output"])
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                pending = parsed
        if pending and pending.get("run_id") and pending.get("child_task_id"):
            result = self._spawn_subagent(
                task_id,
                prompt,
                role=role,
                wait=wait,
                tool_scope=scope,
                context_window=context_window,
                model=model,
                run_id=str(pending["run_id"]),
                child_task_id=str(pending["child_task_id"]),
            )
        else:
            run_id = f"subagent_{uuid.uuid4().hex}"
            child_task_id = f"task_{uuid.uuid4().hex}"
            self.store.update_tool_call(
                task_id,
                call.id,
                output=json.dumps(
                    {"run_id": run_id, "child_task_id": child_task_id, "status": "pending"},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            result = self._spawn_subagent(
                task_id,
                prompt,
                role=role,
                wait=wait,
                tool_scope=scope,
                context_window=context_window,
                model=model,
                run_id=run_id,
                child_task_id=child_task_id,
            )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    def _run_internal_plan_approval(self, task_id: str, call: ToolCall) -> str:
        run_id = self._active_subagent_run_id
        if run_id is None:
            raise RuntimeError("request_plan_approval requires an active subagent run")
        plan_text = call.input.get("plan_text")
        if not isinstance(plan_text, str) or not plan_text.strip():
            raise ValueError("request_plan_approval requires plan_text")
        reason = "Plan approval requested by subagent"
        plan_hash = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
        supplied_hash = call.input.get("plan_hash")
        if supplied_hash is not None and str(supplied_hash) != plan_hash:
            raise ValueError("request_plan_approval plan_hash does not match plan_text")
        requested = self.store.list_plan_approvals(run_id, status="requested")
        if requested:
            approval = requested[0]
            if str(approval["plan_hash"]) != plan_hash:
                raise StaleState("Plan approval already requested with a different plan")
            self._write_plan_file(approval, plan_text)
        else:
            approval_id = f"approval_{uuid.uuid4().hex}"
            approval_draft = {
                "approval_id": approval_id,
                "subagent_run_id": run_id,
                "plan_hash": plan_hash,
            }
            self._write_plan_file(approval_draft, plan_text)
            approval = self.store.create_plan_approval(
                approval_id,
                run_id,
                plan_hash,
                self.owner_id,
            )
        run = self.store.get_subagent_run(run_id)
        if run is None:
            raise RuntimeError(f"Subagent run not found: {run_id}")
        if run["status"] == "running":
            self.store.mark_subagent_needs_review(
                run_id,
                reason,
                int(run["version"]),
                str(run["fencing_token"]),
            )
        self._pending_review = ("needs_review", call.id, reason)
        raise NeedsReview(call.id, reason)

    def _plan_file_path(self, approval_id: str) -> Path:
        return self.repo_root / ".agent_runtime" / "plans" / f"{approval_id}.md"

    def _write_plan_file(self, approval: dict[str, Any], plan_text: str) -> str:
        path = self._plan_file_path(str(approval["approval_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        run = self.store.get_subagent_run(str(approval["subagent_run_id"]))
        content = "\n".join([
            "---",
            f"approval_id: {approval['approval_id']}",
            f"subagent_run_id: {approval['subagent_run_id']}",
            f"child_task_id: {run['child_task_id'] if run else 'unknown'}",
            f"plan_hash: {approval['plan_hash']}",
            f"requested_by: {self.owner_id}",
            "immutable: true",
            "---",
            "",
            str(plan_text).rstrip(),
            "",
        ])
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise StaleState(f"Plan approval file already exists with different content: {path}")
        else:
            path.write_text(content, encoding="utf-8")
        return str(path)

    def _execute_tool(self, name: str, args: dict[str, Any]) -> Any:
        entry = self.tool_registry.get(name)
        if entry is None or not entry.enabled or entry.adapter is None:
            raise RuntimeError(f"Tool has no executable adapter: {name}")
        return entry.adapter(args)

    def _execute_call(self, task_id: str, turn: int, call: ToolCall, messages: list[dict]) -> str:
        if call.name.startswith(".agent_runtime."):
            return self._handle_internal_tool(task_id, turn, call, messages)
        active_tool_scope = self._effective_tool_scope()
        if active_tool_scope is not None and call.name not in active_tool_scope:
            raise RuntimeError(f"Tool is outside the active tool scope: {call.name}")
        args_hash = canonical_args_hash(call.name, call.input)
        existing = self.store.get_tool_call(task_id, call.id)
        operation = None
        if existing is not None:
            if existing["args_hash"] != args_hash:
                raise RuntimeError(f"Tool call ID reused with different arguments: {call.id}")
            operation = self.store.get_operation_for_tool_call(task_id, call.id)
            if operation is not None and operation["state"] == "committed":
                output = self._operation_output(operation, existing)
                self.store.append_event(
                    task_id,
                    "operation_deduplicated",
                    {
                        "operation_id": operation["operation_id"],
                        "tool_use_id": call.id,
                        "semantics": operation["semantics"],
                        "adapter": operation["adapter"],
                        "attempt": operation["attempt_count"],
                        "state": "committed",
                        "reason": "committed operation already has a durable result",
                    },
                )
                return output
            if existing["status"] in {"succeeded", "denied"}:
                self.store.append_event(task_id, "tool_deduplicated", {"tool_use_id": call.id})
                return existing.get("output") or "Permission denied."
            if existing["status"] in {"failed", "aborted"}:
                raise RuntimeError(f"Terminal tool call cannot execute again: {call.id}")
            if existing["status"] == "waiting_approval":
                if existing.get("permission") == "allow":
                    self.store.update_tool_call(task_id, call.id, status="planned")
                else:
                    reason = existing.get("permission_reason") or "Approval required"
                    self._pending_review = ("waiting_approval", call.id, reason)
                    raise WaitingForApproval(call.id, reason)
            if existing["status"] == "needs_review":
                reason = existing.get("error") or "Manual review required"
                self._pending_review = ("needs_review", call.id, reason)
                raise NeedsReview(call.id, reason)
            if operation is not None and operation["state"] in {"dispatched", "unknown"}:
                recovered = self._reconcile_running_call(task_id, existing)
                if recovered is not None:
                    return recovered
            elif existing["status"] == "running":
                recovered = self._reconcile_running_call(task_id, existing)
                if recovered is not None:
                    return recovered
        else:
            effect = self.permissions.classify_effect(call.name, call.input)
            effect_key = args_hash if effect != "read_only" else None
            existing = self.store.create_tool_call(
                task_id, call.id, turn, call.name, call.input, args_hash, effect=effect, effect_key=effect_key
            )

        decision = self.permissions.evaluate(call.name, call.input)
        preapproved = existing is not None and existing.get("status") == "planned" and existing.get("permission") == "allow"
        persisted_permission = "allow" if preapproved and decision.effect == "ask" else decision.effect
        self.store.update_tool_call(
            task_id,
            call.id,
            permission=persisted_permission,
            permission_rule=decision.rule_id,
            permission_reason=decision.reason,
            effect=decision.risk,
        )
        self.store.append_event(task_id, "permission_decision", {
            "tool_use_id": call.id, "effect": decision.effect, "rule_id": decision.rule_id,
            "risk": decision.risk,
        })

        if decision.effect == "deny":
            output = f"Permission denied: {decision.reason}"
            self.store.update_tool_call(task_id, call.id, status="denied", permission="deny", output=output,
                                        finished_at=time.time())
            self.store.append_event(task_id, "tool_denied", {"tool_use_id": call.id, "reason": decision.reason})
            return output

        if decision.effect == "ask" and not preapproved:
            approved = self._ask(call, decision)
            if approved is None:
                self._pending_review = ("waiting_approval", call.id, decision.reason)
                raise WaitingForApproval(call.id, decision.reason)
            if not approved:
                output = "Permission denied by operator."
                self.store.update_tool_call(task_id, call.id, status="denied", permission="deny", output=output,
                                            finished_at=time.time())
                self.store.append_event(task_id, "tool_denied", {"tool_use_id": call.id, "source": "operator"})
                return output
            self.store.update_tool_call(task_id, call.id, permission="allow")

        before_state = None
        expected_after = None
        if call.name in {"write_file", "edit_file"}:
            before_state, expected_after = self._prepare_file_call(task_id, call)
            self.store.update_tool_call(task_id, call.id, before_state=before_state, expected_after=expected_after)

        if decision.risk == "read_only":
            self.store.start_tool_call(task_id, call.id, decision.risk, started_at=time.time())
            self.store.append_event(task_id, "tool_started", {"tool_use_id": call.id, "name": call.name})
            self._fault("after_tool_started", task_id=task_id, tool_use_id=call.id)
            self._assert_lease_for_effect(task_id, call.id, "before_tool_effect")
            try:
                raw_output = self._execute_tool(call.name, call.input)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self.store.update_tool_call(
                    task_id, call.id, status="failed", error=reason, execution_status="error", finished_at=time.time()
                )
                self.store.append_event(task_id, "tool_failed", {"tool_use_id": call.id, "reason": reason})
                raise
            output = raw_output.output if isinstance(raw_output, ShellResult) else str(raw_output)
            self._fault("after_tool_effect_before_persist", task_id=task_id, tool_use_id=call.id)
            if call.name == "read_file":
                state = self.tools.file_state(call.input["path"])
                if state["exists"]:
                    self.store.upsert_file_observation(
                        task_id,
                        state["path"],
                        True,
                        state["sha256"],
                        call.id,
                        state.get("identity"),
                    )
            fields: dict[str, Any] = {"status": "succeeded", "output": output, "finished_at": time.time()}
            if isinstance(raw_output, ShellResult):
                fields.update({
                    "returncode": raw_output.returncode,
                    "stdout": raw_output.stdout,
                    "stderr": raw_output.stderr,
                    "timed_out": int(raw_output.timed_out),
                    "execution_status": raw_output.status,
                })
            self.store.update_tool_call(task_id, call.id, **fields)
            self.store.append_event(task_id, "tool_succeeded", {
                "tool_use_id": call.id, "name": call.name, "output_chars": len(output),
            })
            self._fault("after_tool_persist", task_id=task_id, tool_use_id=call.id)
            return output

        semantics = semantics_for_effect(decision.risk)
        adapter = "file" if decision.risk == "file_write" else "legacy"
        spec = OperationSpec(
            task_id=task_id,
            tool_use_id=call.id,
            adapter=adapter,
            semantics=semantics,
            effect_scope=str(self.repo_root),
            dedupe_key=stable_dedupe_key(task_id, call.id),
            idempotency_key=None,
            args_hash=args_hash,
            request={"name": call.name, "input": call.input},
        )
        try:
            operation = self.store.prepare_operation(
                spec,
                deadline_at=time.time() + float(self.tools.shell_timeout if call.name == "bash" else self.lease_ttl),
            )
            if operation["state"] == "committed":
                output = self._operation_output(operation, existing)
                self.store.append_event(task_id, "operation_deduplicated", {
                    "operation_id": operation["operation_id"],
                    "tool_use_id": call.id,
                    "semantics": operation["semantics"],
                    "adapter": operation["adapter"],
                    "attempt": operation["attempt_count"],
                    "state": "committed",
                    "reason": "committed operation already has a durable result",
                })
                return output
            if operation["state"] in {"dispatched", "unknown"}:
                recovered = self._reconcile_running_call(task_id, existing)
                if recovered is not None:
                    return recovered
                reason = "Operation requires reconciliation before another dispatch"
                self._mark_needs_review(task_id, call.id, reason)
                raise NeedsReview(call.id, reason)
            operation = self.store.claim_operation(operation["operation_id"], claim_ttl=self.lease_ttl)
            # This compatibility fault point is deliberately before the
            # dispatched boundary. A crash here leaves a prepared operation
            # that the next owner may safely claim; once dispatched, recovery
            # must never infer that the external effect did not happen.
            self.store.append_event(task_id, "tool_started", {"tool_use_id": call.id, "name": call.name})
            self._fault("after_tool_started", task_id=task_id, tool_use_id=call.id)
        except EffectBlocked as exc:
            reason = str(exc)
            self._mark_needs_review(task_id, call.id, reason)
            raise NeedsReview(call.id, reason) from exc

        if call.name in {"write_file", "edit_file"} and before_state is not None:
            current = self.tools.file_state(call.input["path"])
            if not self._same_state(current, before_state):
                reason = "File changed after precondition check and before write"
                self._mark_needs_review(task_id, call.id, reason)
                raise NeedsReview(call.id, reason)
        self._assert_lease_for_effect(task_id, call.id, "before_tool_effect")
        if call.name == "bash" and float(self.tools.shell_timeout) >= self.lease_ttl:
            reason = "Shell timeout is not below the repository lease TTL"
            self._mark_needs_review(task_id, call.id, reason)
            raise NeedsReview(call.id, reason)
        if call.name in {"write_file", "edit_file"} and before_state is not None:
            self.tools.set_expected_before(call.input["path"], before_state)
        # This is the irreversible execution boundary. Every validation,
        # lease, and file precondition check above must finish before this
        # state transition; any failure before it remains safely prepared.
        operation = self.store.mark_operation_dispatched(operation["operation_id"])
        try:
            raw_output = self._execute_tool(call.name, call.input)
        except FileConflict as exc:
            reason = str(exc)
            self.store.mark_operation_unknown(operation["operation_id"], reason)
            self._mark_needs_review(task_id, call.id, reason)
            raise NeedsReview(call.id, reason)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.store.mark_operation_unknown(
                operation["operation_id"],
                reason,
                tool_fields={"error": reason, "execution_status": "error"},
            )
            self._mark_needs_review(task_id, call.id, reason)
            raise NeedsReview(call.id, reason)
        self._assert_lease_for_effect(task_id, call.id, "after_tool_effect")
        self._fault("after_tool_effect_before_persist", task_id=task_id, tool_use_id=call.id)

        output = raw_output.output if isinstance(raw_output, ShellResult) else str(raw_output)
        if isinstance(raw_output, ShellResult):
            shell_fields = {
                "returncode": raw_output.returncode,
                "stdout": raw_output.stdout,
                "stderr": raw_output.stderr,
                "timed_out": int(raw_output.timed_out),
                "execution_status": raw_output.status,
                "output": output,
            }
            if raw_output.timed_out or raw_output.returncode != 0:
                reason = "Shell timed out; side effects are unknown" if raw_output.timed_out else (
                    f"Shell exited with return code {raw_output.returncode}; side effects are unknown"
                )
                self.store.mark_operation_unknown(
                    operation["operation_id"],
                    reason,
                    evidence=ReconcileEvidence(
                        "unknown",
                        reason,
                        {"returncode": raw_output.returncode, "timed_out": raw_output.timed_out},
                    ),
                    tool_fields=shell_fields,
                )
                self._mark_needs_review(task_id, call.id, reason)
                raise NeedsReview(call.id, reason)
            shell_fields["effect_confirmation"] = "process_returncode:0;side_effect_unknown"
            self.store.commit_operation(
                operation["operation_id"],
                result=output,
                evidence={"outcome": "returned_zero", "side_effect_unknown": True},
                tool_fields={**shell_fields, "status": "succeeded", "finished_at": time.time()},
                reason="opaque shell returned zero",
            )
        elif call.name in {"write_file", "edit_file"}:
            current = self.tools.file_state(call.input["path"])
            if not self._same_state(current, expected_after):
                reason = "File state differs from expected post-write hash"
                self.store.mark_operation_unknown(
                    operation["operation_id"],
                    reason,
                    evidence=ReconcileEvidence("ambiguous", reason, {"current": current}),
                )
                self._mark_needs_review(task_id, call.id, reason)
                raise NeedsReview(call.id, reason)
            self.store.commit_operation(
                operation["operation_id"],
                result=output,
                evidence={"outcome": "post_hash_match", "path": current["path"], "sha256": current["sha256"]},
                tool_fields={
                    "status": "succeeded",
                    "output": output,
                    "finished_at": time.time(),
                    "effect_confirmed": 1,
                    "effect_confirmation": json.dumps(
                        {"path": current["path"], "sha256": current["sha256"]}, sort_keys=True
                    ),
                },
                observation={
                    "path": current["path"],
                    "exists_now": True,
                    "sha256": current["sha256"],
                    "identity": current.get("identity"),
                },
                reason="file post-write hash matched",
            )
            # Keep the v0.1.1 completion hook observable for integrations
            # that instrumented complete_effect. The ledger commit above is
            # authoritative; this compatibility call cannot replay a write
            # because its reservation is already completed.
            reservation = self.store.get_effect_reservation(task_id, call.id)
            if reservation is not None:
                self.store.complete_effect(
                    int(reservation["reservation_id"]),
                    task_id,
                    call.id,
                    {
                        "status": "succeeded",
                        "output": output,
                        "finished_at": time.time(),
                        "effect_confirmed": 1,
                    },
                    {"post_hash": True},
                    {
                        "path": current["path"],
                        "exists_now": True,
                        "sha256": current["sha256"],
                        "identity": current.get("identity"),
                    },
                )
        else:
            self.store.commit_operation(
                operation["operation_id"],
                result=output,
                evidence={"outcome": "returned"},
                tool_fields={"status": "succeeded", "output": output, "finished_at": time.time()},
            )
        self._fault("after_tool_persist", task_id=task_id, tool_use_id=call.id)
        return output

    def _prepare_file_call(self, task_id: str, call: ToolCall) -> tuple[dict[str, Any], dict[str, Any]]:
        before = self.tools.file_state(call.input["path"])
        observation = self.store.get_file_observation(task_id, before["path"])
        if before["exists"]:
            if observation is None:
                reason = f"read-before-edit required for {call.input['path']}"
                self._mark_needs_review(task_id, call.id, reason)
                raise NeedsReview(call.id, reason)
            if (
                not observation["exists_now"]
                or observation["sha256"] != before["sha256"]
                or (observation.get("identity") is not None and observation["identity"] != before.get("identity"))
            ):
                reason = f"File hash conflict before editing {call.input['path']}"
                self._mark_needs_review(task_id, call.id, reason)
                raise NeedsReview(call.id, reason)
        elif observation is not None and observation["exists_now"]:
            reason = f"File appeared after it was observed absent: {call.input['path']}"
            self._mark_needs_review(task_id, call.id, reason)
            raise NeedsReview(call.id, reason)
        try:
            _, expected = self.tools.prepare_file_write(call.name, call.input)
        except (FileNotFoundError, ValueError) as exc:
            reason = f"File precondition conflict for {call.input.get('path', '')}: {exc}"
            self._mark_needs_review(task_id, call.id, reason)
            raise NeedsReview(call.id, reason)
        return before, expected

    @staticmethod
    def _operation_output(operation: dict[str, Any], call: dict[str, Any] | None = None) -> str:
        result = operation.get("result")
        if isinstance(result, dict) and "output" in result:
            return str(result["output"])
        if isinstance(result, str):
            return result
        if call is not None and call.get("output") is not None:
            return str(call["output"])
        return "[deduplicated committed operation]"

    def _reconcile_running_call(self, task_id: str, call: dict[str, Any]) -> str | None:
        operation_id = call.get("operation_id")
        operation = self.store.get_operation(operation_id) if operation_id else None
        if operation is not None:
            if operation["state"] == "committed":
                self.store.append_event(
                    task_id,
                    "operation_deduplicated",
                    {
                        "operation_id": operation["operation_id"],
                        "tool_use_id": call["tool_use_id"],
                        "semantics": operation["semantics"],
                        "adapter": operation["adapter"],
                        "attempt": operation["attempt_count"],
                        "state": "committed",
                        "reason": "committed operation already has a durable result",
                    },
                )
                return self._operation_output(operation, call)
            if operation["state"] == "prepared":
                return None
            if operation["state"] == "dispatched":
                try:
                    self.store.mark_operation_unknown(
                        operation["operation_id"],
                        "Interrupted dispatched operation requires reconciliation",
                    )
                except StaleState:
                    pass
                operation = self.store.get_operation(operation["operation_id"]) or operation
            if operation["state"] in {"failed", "cancelled"}:
                raise RuntimeError(f"Operation is terminal and cannot execute again: {operation['operation_id']}")
            if operation["state"] == "unknown":
                if operation["adapter"] == "file" and call.get("before_state") and call.get("expected_after"):
                    current = self.tools.file_state(call["args"]["path"])
                    if self._same_state(current, call["expected_after"]):
                        output = "[recovered] file already matches expected post-write hash"
                        self.store.commit_operation(
                            operation["operation_id"],
                            result=output,
                            evidence=ReconcileEvidence(
                                "post_hash_match",
                                "file already matched the expected post-write hash",
                                {"path": current["path"], "sha256": current["sha256"]},
                            ),
                            tool_fields={
                                "status": "succeeded",
                                "output": output,
                                "finished_at": time.time(),
                                "effect_confirmed": 1,
                                "effect_confirmation": json.dumps(
                                    {"path": current["path"], "sha256": current["sha256"]}, sort_keys=True
                                ),
                            },
                            observation={
                                "path": current["path"],
                                "exists_now": True,
                                "sha256": current["sha256"],
                                "identity": current.get("identity"),
                            },
                            allow_unknown=True,
                            reason="reconciled file post-write hash",
                            emit_tool_event=False,
                        )
                        self.store.append_event(
                            task_id,
                            "tool_recovered_succeeded",
                            {"tool_use_id": call["tool_use_id"], "operation_id": operation["operation_id"]},
                        )
                        return output
                    if self._same_state(current, call["before_state"]):
                        reason = "File still matches the before hash; explicit retry confirmation is required"
                    else:
                        reason = "Interrupted file write has an ambiguous current hash"
                else:
                    reason = "Interrupted opaque effect has unknown external outcome"
                self._mark_needs_review(task_id, call["tool_use_id"], reason)
                raise NeedsReview(call["tool_use_id"], reason)

        effect = call.get("effect") or "unknown_write"
        reservation = self.store.get_effect_reservation(task_id, call["tool_use_id"])
        if reservation is not None:
            if reservation["state"] == "running":
                reason = "Effect reservation is still running; automatic retry is unsafe"
                self._mark_needs_review(task_id, call["tool_use_id"], reason)
                raise NeedsReview(call["tool_use_id"], reason)
            if effect != "file_write" and reservation["state"] in {"unknown", "completed"}:
                reason = "Interrupted effect has unknown external outcome"
                self._mark_needs_review(task_id, call["tool_use_id"], reason)
                raise NeedsReview(call["tool_use_id"], reason)
            if effect == "file_write" and reservation["state"] == "completed":
                expected = call.get("expected_after") or {}
                current = self.tools.file_state(call["args"]["path"])
                if not self._same_state(current, expected):
                    reason = "Completed file reservation no longer matches its expected post-write hash"
                    self._mark_needs_review(task_id, call["tool_use_id"], reason)
                    raise NeedsReview(call["tool_use_id"], reason)
        if effect == "file_write" and call.get("before_state") and call.get("expected_after"):
            current = self.tools.file_state(call["args"]["path"])
            if self._same_state(current, call["expected_after"]):
                output = "[recovered] file already matches expected post-write hash"
                if reservation is not None and reservation["state"] == "unknown":
                    self.store.complete_effect(
                        int(reservation["reservation_id"]),
                        task_id,
                        call["tool_use_id"],
                        {
                            "status": "succeeded",
                            "output": output,
                            "finished_at": time.time(),
                            "effect_confirmed": 1,
                            "effect_confirmation": json.dumps(
                                {"path": current["path"], "sha256": current["sha256"]}, sort_keys=True
                            ),
                        },
                        {"post_state_hash_reconciled": True},
                        {
                            "path": current["path"],
                            "exists_now": True,
                            "sha256": current["sha256"],
                            "identity": current.get("identity"),
                        },
                        event_payload={"name": call.get("name"), "output_chars": len(output)},
                        allow_unknown=True,
                        event_type="tool_recovered_succeeded",
                    )
                else:
                    self.store.upsert_file_observation(
                        task_id,
                        call["expected_after"]["path"],
                        True,
                        call["expected_after"]["sha256"],
                        call["tool_use_id"],
                        current.get("identity"),
                    )
                    self.store.update_tool_call(task_id, call["tool_use_id"], status="succeeded", output=output,
                                                finished_at=time.time(), effect_confirmed=1,
                                                effect_confirmation=json.dumps({"path": current["path"], "sha256": current["sha256"]}, sort_keys=True))
                    self.store.append_event(task_id, "tool_recovered_succeeded", {"tool_use_id": call["tool_use_id"]})
                return output
            if self._same_state(current, call["before_state"]):
                self.store.update_tool_call(task_id, call["tool_use_id"], status="planned")
                return None
            reason = "Interrupted file write has an ambiguous current hash"
            self._mark_needs_review(task_id, call["tool_use_id"], reason)
            raise NeedsReview(call["tool_use_id"], reason)
        if effect == "read_only":
            self.store.update_tool_call(task_id, call["tool_use_id"], status="planned")
            return None
        reason = "Interrupted write-capable tool cannot be retried automatically"
        self._mark_needs_review(task_id, call["tool_use_id"], reason)
        raise NeedsReview(call["tool_use_id"], reason)

    def _mark_needs_review(self, task_id: str, tool_use_id: str, reason: str) -> None:
        self._pending_review = ("needs_review", tool_use_id, reason)

    def _assert_lease_for_effect(self, task_id: str, tool_use_id: str, boundary: str) -> None:
        try:
            self.store.assert_lease()
        except LeaseLost:
            logging.getLogger(__name__).warning(
                "stale lease blocked effect: task=%s tool=%s boundary=%s owner=%s token=%s",
                task_id,
                tool_use_id,
                boundary,
                self.owner_id,
                self._lease_token,
            )
            raise

    def _ask(self, call: ToolCall, decision: PermissionDecision) -> bool | None:
        if self.approval_callback is not None:
            return bool(self.approval_callback(call.name, call.input, decision.reason))
        if not self.interactive or not sys.stdin.isatty():
            return None
        print(f"\n[permission] {decision.reason}: {call.name}({call.input})")
        return input("Allow? [y/N] ").strip().lower() in {"y", "yes"}

    @staticmethod
    def _same_state(left: dict[str, Any], right: dict[str, Any] | None) -> bool:
        if right is None:
            return False
        if left.get("exists") != right.get("exists") or left.get("sha256") != right.get("sha256"):
            return False
        left_identity = left.get("identity")
        right_identity = right.get("identity")
        return not (left_identity is not None and right_identity is not None and left_identity != right_identity)

    @staticmethod
    def _pending_calls(messages: list[dict]) -> list[ToolCall]:
        if not messages or messages[-1].get("role") != "assistant":
            return []
        calls = []
        for block in messages[-1].get("content", []):
            if block.get("type") == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], block.get("input", {})))
        return calls

    @staticmethod
    def _latest_tool_result_pair(messages: list[dict]) -> list[dict[str, Any]]:
        """Return only the latest adjacent assistant/user recovery pair."""
        if len(messages) < 2:
            return []
        assistant = messages[-2]
        user = messages[-1]
        if assistant.get("role") != "assistant" or user.get("role") != "user":
            return []

        user_content = user.get("content")
        if isinstance(user_content, str):
            if not user_content.strip():
                return []
            assistant_content = assistant.get("content")
            if isinstance(assistant_content, str):
                if not assistant_content.strip():
                    return []
                return [copy.deepcopy(assistant), copy.deepcopy(user)]
            if isinstance(assistant_content, list):
                text_blocks = [
                    copy.deepcopy(block)
                    for block in assistant_content
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and str(block.get("text") or "").strip()
                    )
                ]
                if text_blocks:
                    return [
                        {"role": "assistant", "content": text_blocks},
                        {"role": "user", "content": user_content},
                    ]
            return []

        if not isinstance(user_content, list):
            return []
        result_blocks = [
            copy.deepcopy(block)
            for block in user_content
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") is not None
            )
        ]
        if not result_blocks or not isinstance(assistant.get("content"), list):
            return []
        result_ids = {str(block["tool_use_id"]) for block in result_blocks}
        tool_use_blocks = [
            copy.deepcopy(block)
            for block in assistant["content"]
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") is not None
                and str(block["id"]) in result_ids
            )
        ]
        if {str(block["id"]) for block in tool_use_blocks} != result_ids:
            return []
        return [
            {"role": "assistant", "content": tool_use_blocks},
            {"role": "user", "content": result_blocks},
        ]

    @staticmethod
    def _last_text(messages: list[dict]) -> str:
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            content = message.get("content", [])
            if isinstance(content, str):
                return content
            for block in reversed(content):
                if block.get("type") == "text":
                    return block.get("text", "")
        return ""
