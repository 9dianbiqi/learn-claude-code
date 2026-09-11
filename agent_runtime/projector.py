from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .store import EventStore


RECENT_MESSAGE_LIMIT = 8


def estimate_tokens(text: str) -> int:
    """Deterministic, cheap token estimate (~4 characters per token)."""
    return max(1, (len(text) + 3) // 4)


@dataclass
class ContextProjection:
    messages: list[dict[str, Any]]
    source_checkpoint_id: int | None
    metrics: dict[str, Any] = field(default_factory=dict)

    def persisted(self) -> dict[str, Any]:
        data = {
            "source_checkpoint_id": self.source_checkpoint_id,
            **self.metrics,
        }
        if self.metrics.get("projection_used"):
            data["messages"] = self.messages
        return data


class ContextProjector:
    """Build a compact, read-only model view from durable state."""

    def __init__(self, store: EventStore):
        self.store = store

    def project(self, task_id: str, messages: list[dict[str, Any]],
                source_checkpoint_id: int | None,
                recent_message_limit: int = RECENT_MESSAGE_LIMIT) -> ContextProjection:
        task = self.store.get_task(task_id)
        plan = self.store.get_active_plan(task_id)
        memories = self.store.list_memories(task_id)
        summaries = self.store.list_summaries(task_id)
        review_reasons = self._recent_review_reasons(task_id)

        active_subtask = self._select_active_subtask(plan) if plan else None
        evidence = bool(plan or memories or summaries or review_reasons)
        if not evidence:
            return ContextProjection(
                messages,
                source_checkpoint_id,
                {
                    "basis": "full_history",
                    "projection_used": False,
                    "token_estimate": estimate_tokens(_render_messages(messages)),
                    "active_subtask_id": None,
                    "memory_count": len(memories),
                    "summary_count": len(summaries),
                    "plan_item_count": len(plan["items"]) if plan else 0,
                },
            )

        projected = self._build(
            messages,
            task,
            plan,
            active_subtask,
            memories,
            summaries,
            review_reasons,
            recent_message_limit,
        )
        return ContextProjection(
            projected,
            source_checkpoint_id,
            {
                "basis": "projected",
                "projection_used": True,
                "token_estimate": estimate_tokens(_render_messages(projected)),
                "active_subtask_id": active_subtask["subtask_id"] if active_subtask else None,
                "memory_count": len(memories),
                "summary_count": len(summaries),
                "plan_item_count": len(plan["items"]) if plan else 0,
                "message_count": len(projected),
            },
        )

    @staticmethod
    def _select_active_subtask(plan: dict[str, Any]) -> dict[str, Any] | None:
        item_by_subtask = {
            item["subtask_id"]: item
            for item in plan["items"]
            if not item.get("tombstoned", False)
        }
        frozen_dag = plan.get("dag_hash") is not None

        def unblocked(item: dict[str, Any]) -> bool:
            return all(
                blocker in item_by_subtask and item_by_subtask[blocker]["status"] == "completed"
                for blocker in item.get("blocked_by", [])
            )

        preference = (
            ("verifying", "in_progress", "retryable", "pending")
            if frozen_dag
            else ("in_progress", "verifying", "retryable", "pending")
        )
        for preferred in preference:
            for item in plan["items"]:
                if item.get("tombstoned", False):
                    continue
                if item["status"] != preferred:
                    continue
                if (not frozen_dag or preferred == "pending") and not unblocked(item):
                    continue
                return item
        return None

    def _recent_review_reasons(self, task_id: str) -> list[str]:
        reasons: list[str] = []
        for event in self.store.list_events(task_id):
            if event["type"] in {"model_failed", "tool_needs_review"}:
                reason = event["payload"].get("reason") or event["payload"].get("error")
                if reason:
                    reasons.append(str(reason))
        return reasons[-3:]

    def _build(
        self,
        messages: list[dict[str, Any]],
        task: dict[str, Any],
        plan: dict[str, Any],
        active_subtask: dict[str, Any] | None,
        memories: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        review_reasons: list[str],
        recent_message_limit: int,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        blocks.append({
            "role": "system",
            "content": (
                "You are operating within a durable, replay-safe agent runtime.\n"
                f"Task identity: {task['task_id']}\n"
                f"Initial instruction: {str(task['prompt'])[:400]}"
            ),
        })

        if active_subtask is not None:
            plan_block = f"Current plan item: {active_subtask['subtask_id']}"
            if active_subtask.get("description"):
                plan_block += f" ({active_subtask['description']})"
            item_by_subtask = {item["subtask_id"]: item for item in plan["items"]}
            deps = [
                item_by_subtask[blocker]["description"] or blocker
                for blocker in active_subtask.get("blocked_by", [])
                if blocker in item_by_subtask and item_by_subtask[blocker]["status"] == "completed"
            ]
            if deps:
                plan_block += "\nDependencies (completed): " + "; ".join(deps)
            blocks.append({"role": "system", "content": plan_block})

        if memories:
            body = "Durable memories:\n" + "\n".join(
                f"[{memory['kind']}] {memory['content']}" for memory in memories
            )
            blocks.append({"role": "system", "content": body})
        if summaries:
            body = "Context summaries:\n" + "\n".join(
                f"[{summary['scope']}] {summary['content']}" for summary in summaries
            )
            blocks.append({"role": "system", "content": body})
        if review_reasons:
            body = "Recent failures / review reasons:\n" + "\n".join(
                f"- {reason}" for reason in review_reasons
            )
            blocks.append({"role": "system", "content": body})

        tail = messages[-recent_message_limit:] if recent_message_limit > 0 else messages
        return blocks + tail


def _render_messages(messages: list[dict[str, Any]]) -> str:
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
