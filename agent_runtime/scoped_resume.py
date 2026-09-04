"""Internal verified-scoped resume context construction.

The public runtime surface remains ``Runtime.run``/``Runtime.resume``.  This
module owns the deterministic projection used only after an interrupted
verified-DAG resume.  It deliberately has a small interface: callers provide
the selected resume unit and the messages created after the resume boundary;
the implementation validates dependencies, snapshots evidence, and either
returns a complete model view or fails closed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import VerifiedSubtaskConfig, is_valid_sha256, normalize_evidence_path
from .projector import estimate_tokens
from .store import EventStore


class ScopedContextError(RuntimeError):
    """Raised when a required scoped-resume section cannot be constructed."""


@dataclass(frozen=True)
class ScopedContextProjection:
    """A complete, deterministic scoped model view and its audit metrics."""

    messages: list[dict[str, Any]]
    metrics: dict[str, Any]
    source_checkpoint_id: int | None = None

    def persisted(self) -> dict[str, Any]:
        return {
            "source_checkpoint_id": self.source_checkpoint_id,
            "messages": copy.deepcopy(self.messages),
            **copy.deepcopy(self.metrics),
        }

    def with_post_resume_messages(
        self,
        post_resume_messages: Sequence[Mapping[str, Any]],
        source_checkpoint_id: int | None = None,
    ) -> "ScopedContextProjection":
        """Reuse the immutable scoped base while extending only the resume tail."""

        if not self.messages:
            raise ScopedContextError("scoped context base is empty")
        view = [copy.deepcopy(self.messages[0])]
        view.extend(copy.deepcopy(dict(message)) for message in post_resume_messages)
        rendered = estimate_tokens(_render_messages(view))
        metrics = {
            **copy.deepcopy(self.metrics),
            "token_estimate": int(rendered),
            "overflow": bool(
                rendered > int(self.metrics.get("scoped_context_budget") or 0)
            ),
            "overflow_outcome": (
                "overflow"
                if rendered > int(self.metrics.get("scoped_context_budget") or 0)
                else None
            ),
        }
        return ScopedContextProjection(
            view,
            metrics,
            self.source_checkpoint_id if source_checkpoint_id is None else source_checkpoint_id,
        )


def _render_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    """Render exactly the message text counted by the repository convention."""

    parts: list[str] = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            texts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("text")
            ]
            parts.append(" ".join(texts))
        elif content:
            parts.append(str(content))
    return "\n".join(parts)


class VerifiedScopedResume:
    """Build a verified-scoped context behind one small internal interface."""

    def __init__(
        self,
        store: EventStore,
        repo_root: str | Path,
        configs: Mapping[str, VerifiedSubtaskConfig],
        ordered_subtask_ids: Sequence[str],
    ) -> None:
        self.store = store
        self.repo_root = Path(repo_root).resolve()
        self.configs = dict(configs)
        self.ordered_subtask_ids = tuple(str(item) for item in ordered_subtask_ids)

    def build(
        self,
        task_id: str,
        resume_unit: Mapping[str, Any],
        post_resume_messages: Sequence[Mapping[str, Any]],
        budget: int,
        source_checkpoint_id: int | None = None,
    ) -> ScopedContextProjection:
        """Build one complete view; never omit a required section to fit budget."""

        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ScopedContextError("scoped context budget must be a positive integer")

        task = self.store.get_task(task_id)
        plan = self.store.get_active_plan(task_id) or self.store.get_latest_plan(task_id)
        if plan is None:
            raise ScopedContextError(f"scoped context requires a frozen plan for task {task_id}")
        plan_items = {
            str(item["subtask_id"]): item for item in plan.get("items", [])
        }
        resume_id = str(resume_unit.get("subtask_id") or "")
        if not resume_id or resume_id not in plan_items:
            raise ScopedContextError("scoped context resume unit is missing from the frozen plan")
        config = self.configs.get(resume_id)
        if config is None:
            raise ScopedContextError(
                f"scoped context has no frozen configuration for resume unit {resume_id}"
            )

        required_ids = self._transitive_dependencies(config.subtask_id)
        dependency_snapshots: list[dict[str, Any]] = []
        for subtask_id in self.ordered_subtask_ids:
            if subtask_id not in required_ids:
                continue
            item = plan_items.get(subtask_id)
            if item is None:
                raise ScopedContextError(
                    f"scoped context missing required dependency plan item {subtask_id}"
                )
            if item.get("status") != "completed":
                raise ScopedContextError(
                    f"scoped context required dependency {subtask_id} is not completed"
                )
            checkpoint = self.store.get_current_verified_subtask_checkpoint(
                task_id, int(item["plan_item_id"])
            )
            if checkpoint is None:
                raise ScopedContextError(
                    f"scoped context missing current valid dependency snapshot {subtask_id}"
                )
            if checkpoint.get("lifecycle_state") != "valid":
                raise ScopedContextError(
                    f"scoped context dependency {subtask_id} is not current valid evidence"
                )
            dependency_snapshots.append(self._dependency_snapshot(subtask_id, checkpoint))

        latest_recovery = self._latest_recovery_reason(task_id, resume_id, required_ids)
        path_states = [
            self._path_state(normalize_evidence_path(path))
            for path in config.evidence_paths
        ]
        path_states.sort(key=lambda entry: str(entry["path"]))

        item = plan_items[resume_id]
        consumed_turns = self._int_field(item, "consumed_turns", 0)
        max_turns = self._int_field(item, "max_turns", 0)
        if max_turns < 1 or consumed_turns < 0 or consumed_turns > max_turns:
            raise ScopedContextError(
                f"scoped context has invalid turn budget for resume unit {resume_id}"
            )

        bundle = {
            "global_constraints": str(task.get("prompt") or ""),
            "resume_unit": {
                "subtask_id": resume_id,
                "description": config.description,
                "completion_criteria": config.completion_criteria,
                "remaining_turn_budget": max_turns - consumed_turns,
            },
            "required_dependency_snapshots": dependency_snapshots,
            "latest_recovery_reason": latest_recovery,
            "relevant_path_state": path_states,
        }
        # Canonical JSON is the rendered durable representation.  It gives the
        # model explicit section names while making order and token accounting
        # independent of dict insertion order.
        rendered_bundle = json.dumps(
            bundle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        base_messages: list[dict[str, Any]] = [{
            "role": "system",
            "content": "Verified-Scoped recovery context:\n" + rendered_bundle,
        }]
        # Only messages created after the resume boundary may be reintroduced.
        view = base_messages + [copy.deepcopy(dict(message)) for message in post_resume_messages]
        token_estimate = estimate_tokens(_render_messages(view))
        metrics = {
            "basis": "verified_scoped",
            "projection_used": True,
            "scoped_resume": True,
            "scoped_context_budget": int(budget),
            "token_estimate": int(token_estimate),
            "resume_unit_id": resume_id,
            "dependency_count": len(dependency_snapshots),
            "relevant_path_count": len(path_states),
            "overflow": bool(token_estimate > int(budget)),
            "overflow_outcome": "overflow" if token_estimate > int(budget) else None,
        }
        return ScopedContextProjection(view, metrics, source_checkpoint_id)

    def _transitive_dependencies(self, subtask_id: str) -> set[str]:
        required: set[str] = set()
        visiting: set[str] = set()

        def visit(current: str) -> None:
            if current in required:
                return
            if current in visiting:
                raise ScopedContextError(
                    f"scoped context dependency cycle includes {current}"
                )
            config = self.configs.get(current)
            if config is None:
                raise ScopedContextError(
                    f"scoped context missing frozen dependency configuration {current}"
                )
            visiting.add(current)
            for dependency in config.blocked_by:
                visit(str(dependency))
            visiting.remove(current)
            if current != subtask_id:
                required.add(current)

        config = self.configs.get(subtask_id)
        if config is None:
            raise ScopedContextError(
                f"scoped context missing frozen configuration for resume unit {subtask_id}"
            )
        for dependency in config.blocked_by:
            visit(str(dependency))
        return required

    @staticmethod
    def _int_field(item: Mapping[str, Any], name: str, default: int) -> int:
        try:
            return int(item.get(name, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _dependency_snapshot(
        subtask_id: str,
        checkpoint: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw_manifest = checkpoint.get("evidence_manifest")
        if not isinstance(raw_manifest, list):
            raise ScopedContextError(
                f"scoped context dependency {subtask_id} has no evidence manifest"
            )
        manifest: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in raw_manifest:
            if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
                raise ScopedContextError(
                    f"scoped context dependency {subtask_id} has a malformed evidence manifest"
                )
            try:
                path = normalize_evidence_path(str(entry["path"]))
            except (TypeError, ValueError) as exc:
                raise ScopedContextError(
                    f"scoped context dependency {subtask_id} has an invalid evidence path"
                ) from exc
            if path in seen:
                raise ScopedContextError(
                    f"scoped context dependency {subtask_id} has duplicate evidence path {path}"
                )
            digest = str(entry["sha256"])
            if not is_valid_sha256(digest):
                raise ScopedContextError(
                    f"scoped context dependency {subtask_id} has an invalid evidence hash"
                )
            seen.add(path)
            manifest.append({"path": path, "sha256": digest})
        manifest.sort(key=lambda entry: entry["path"])
        return {
            "subtask_id": subtask_id,
            "lifecycle_state": "valid",
            "completion_summary": str(checkpoint.get("completion_summary") or ""),
            "evidence_manifest": manifest,
            "verified_subtask_checkpoint_id": int(
                checkpoint["verified_subtask_checkpoint_id"]
            ),
        }

    def _latest_recovery_reason(
        self,
        task_id: str,
        resume_id: str,
        dependency_ids: set[str],
    ) -> dict[str, Any]:
        relevant = {resume_id, *dependency_ids}
        selected: dict[str, Any] | None = None
        for event in self.store.list_events(task_id):
            event_type = str(event.get("type") or "")
            payload = event.get("payload") or {}
            if event_type not in {
                "model_failed",
                "tool_needs_review",
                "verifier_run_recorded",
                "verified_subtask_checkpoint_stale",
                "verified_subtask_evidence_refresh_fail",
                "verified_subtask_evidence_refresh_uncertain",
                "plan_item_dependency_invalidated",
            }:
                continue
            subtask_id = payload.get("subtask_id") or payload.get("active_subtask_id")
            if subtask_id is not None and str(subtask_id) not in relevant:
                continue
            status = payload.get("status")
            if event_type == "verifier_run_recorded" and status not in {"fail", "uncertain"}:
                continue
            summary = payload.get("summary")
            reason = summary or payload.get("reason") or payload.get("error")
            if not reason:
                continue
            selected = {
                "event_type": event_type,
                "status": str(status or self._status_for_event(event_type)),
                "reason": str(reason),
                "subtask_id": str(subtask_id) if subtask_id is not None else None,
            }
            if summary and payload.get("reason") and str(summary) != str(payload["reason"]):
                selected["detail"] = str(payload["reason"])
        if selected is not None:
            return selected
        return {
            "event_type": None,
            "status": "none",
            "reason": "No prior fail, uncertain, or stale recovery reason recorded.",
            "subtask_id": resume_id,
        }

    @staticmethod
    def _status_for_event(event_type: str) -> str:
        if event_type.endswith("uncertain"):
            return "uncertain"
        if event_type.endswith("fail") or event_type.endswith("stale"):
            return "fail"
        return "review"

    def _path_state(self, relative_path: str) -> dict[str, Any]:
        candidate = self.repo_root.joinpath(*relative_path.split("/"))
        state: dict[str, Any] = {
            "path": relative_path,
            "sha256": None,
            "size": None,
            "missing": True,
            "status": "missing",
        }
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.repo_root)
        except (OSError, RuntimeError, ValueError):
            state["status"] = "path_escape"
            state["missing"] = True
            return state
        try:
            if not resolved.exists():
                return state
            stat_result = resolved.stat()
            state["size"] = int(stat_result.st_size)
            if not stat.S_ISREG(stat_result.st_mode):
                state["status"] = "not_regular"
                state["size"] = None
                return state
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            state["sha256"] = digest
            state["missing"] = False
            state["status"] = "present"
            return state
        except (OSError, PermissionError):
            state["status"] = "unreadable"
            state["sha256"] = None
            state["size"] = None
            return state


__all__ = ["ScopedContextError", "ScopedContextProjection", "VerifiedScopedResume"]
