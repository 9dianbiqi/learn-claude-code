from __future__ import annotations

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
from .models import ModelResponse, RunResult, ToolCall
from .permissions import PermissionDecision, PermissionEngine
from .store import EffectBlocked, EventStore, InvariantViolation, LeaseLost, StaleState
from .tools import FileConflict, ShellResult, TOOL_SCHEMAS, ToolExecutor


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


class Runtime:
    def __init__(self, repo_root: str | Path, model: Any, store: EventStore | None = None,
                 model_name: str | None = None, owner_id: str | None = None,
                 fault_injector: Callable[..., None] | None = None,
                 approval_callback: Callable[[str, dict[str, Any], str], bool] | None = None,
                 interactive: bool = False, policy_path: str | Path | None = None,
                 lease_ttl: float = 300.0):
        self.repo_root = Path(repo_root).resolve()
        self.model = model
        self.model_name = model_name or getattr(model, "name", "unknown")
        self.store = store or EventStore(self.repo_root / ".agent_runtime" / "runtime.db")
        self.owner_id = owner_id or f"runtime-{uuid.uuid4().hex}"
        self.fault_injector = fault_injector
        self.approval_callback = approval_callback
        self.interactive = interactive
        self.lease_ttl = lease_ttl
        model_timeout = getattr(model, "timeout", None)
        if model_timeout is not None and float(model_timeout) >= lease_ttl:
            raise ValueError("Model timeout must be smaller than lease TTL")
        self._lease_token: int | None = None
        self.tools = ToolExecutor(self.repo_root)
        if float(self.tools.shell_timeout) >= lease_ttl:
            self.tools.shell_timeout = max(0.01, float(lease_ttl) * 0.8)
        self.permissions = PermissionEngine(self.repo_root, policy_path=policy_path)
        self._pending_review: tuple[str, str, str] | None = None

    def _fault(self, point: str, **context: Any) -> None:
        if self.fault_injector:
            self.fault_injector(point, **context)

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
        return self._run_task(task_id, messages, turn=0)

    def resume(self, task_id: str) -> RunResult:
        return self._resume_task(task_id, lease_acquired=False)

    def _resume_task(self, task_id: str, lease_acquired: bool) -> RunResult:
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
            } else []
            return self._run_task(
                task_id,
                checkpoint["messages"],
                int(checkpoint["cursor"].get("turn", 0)),
                pending_calls=pending,
                resume_recovery=True,
                lease_acquired=True,
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
                  lease_acquired: bool = False) -> RunResult:
        recovered_completion_text: str | None = None
        self._pending_review = None
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
            } else []
            if phase == "model_responded" and not pending_calls:
                recovered_completion_text = self._last_text(messages)
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

                if pending_calls is not None:
                    self._append_tool_results(task_id, turn, messages, pending_calls)
                    pending_calls = None
                    turn += 1
                    continue

                request = {"messages": messages, "tools": TOOL_SCHEMAS}
                model_call_id = self.store.create_model_call(task_id, turn, request)
                self._fault("before_model_call", task_id=task_id, turn=turn)
                try:
                    response: ModelResponse = self.model.complete(messages, TOOL_SCHEMAS)
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
                self.store.save_checkpoint(task_id, "model_responded", messages, {"turn": turn})

                if not response.tool_calls:
                    text = response.text
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

    def _append_tool_results(self, task_id: str, turn: int, messages: list[dict], calls: list[ToolCall]) -> None:
        results: list[dict[str, Any]] = []
        for call in calls:
            output = self._execute_call(task_id, turn, call)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": output})
        messages.append({"role": "user", "content": results})
        self.store.save_checkpoint(task_id, "tool_results_appended", messages, {"turn": turn + 1})

    def _execute_call(self, task_id: str, turn: int, call: ToolCall) -> str:
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
                raw_output = self.tools.execute(call.name, call.input)
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
            raw_output = self.tools.execute(call.name, call.input)
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
