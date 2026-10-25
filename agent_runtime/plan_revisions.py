from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Mapping

from .models import (
    AddPlanItem,
    PlanItemDraft,
    PlanPatch,
    PlanRevisionItem,
    SplitPlanItem,
    TombstonePlanItem,
    UpdatePlanItemDependencies,
)


class PlanPatchError(ValueError):
    """Raised when a PlanPatch cannot safely derive a new revision."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def revision_snapshot(items: tuple[PlanRevisionItem, ...]) -> list[dict[str, object]]:
    return [item.item_dict() for item in items]


def decode_snapshot(value: str) -> tuple[PlanRevisionItem, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise PlanPatchError("plan revision snapshot must be a list")
    items: list[PlanRevisionItem] = []
    required_fields = {
        "subtask_id",
        "description",
        "blocked_by",
        "verifier_bundle_hash",
        "max_turns",
        "tombstoned",
    }
    for entry in raw:
        if not isinstance(entry, dict):
            raise PlanPatchError("plan revision item must be an object")
        if set(entry) != required_fields:
            raise PlanPatchError("plan revision item has missing or unknown fields")
        subtask_id = entry["subtask_id"]
        description = entry["description"]
        blocked_by = entry["blocked_by"]
        verifier_bundle_hash = entry["verifier_bundle_hash"]
        max_turns = entry["max_turns"]
        tombstoned = entry["tombstoned"]
        if not isinstance(subtask_id, str) or not subtask_id.strip():
            raise PlanPatchError("plan revision subtask_id must be a non-empty string")
        if not isinstance(description, str):
            raise PlanPatchError("plan revision description must be a string")
        if not isinstance(blocked_by, list) or any(
            not isinstance(dependency, str) or not dependency.strip()
            for dependency in blocked_by
        ):
            raise PlanPatchError("plan revision blocked_by must be a string list")
        if blocked_by != sorted(blocked_by) or len(blocked_by) != len(set(blocked_by)):
            raise PlanPatchError("plan revision blocked_by must be sorted and unique")
        if verifier_bundle_hash is not None and (
            not isinstance(verifier_bundle_hash, str)
            or not verifier_bundle_hash.strip()
        ):
            raise PlanPatchError(
                "plan revision verifier_bundle_hash must be a non-empty string"
            )
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
            raise PlanPatchError("plan revision max_turns must be a positive integer")
        if not isinstance(tombstoned, bool):
            raise PlanPatchError("plan revision tombstoned must be a boolean")
        items.append(
            PlanRevisionItem(
                subtask_id=subtask_id,
                description=description,
                blocked_by=tuple(blocked_by),
                verifier_bundle_hash=verifier_bundle_hash,
                max_turns=max_turns,
                tombstoned=tombstoned,
            )
        )
    result = tuple(items)
    validate_snapshot(result)
    return result


def draft_item(item: PlanItemDraft) -> PlanRevisionItem:
    return PlanRevisionItem(
        subtask_id=item.subtask_id,
        description=item.description,
        blocked_by=item.blocked_by,
        verifier_bundle_hash=item.verifier_bundle_hash,
        max_turns=item.max_turns,
    )


def dag_hash(items: tuple[PlanRevisionItem, ...]) -> str:
    payload = {
        "nodes": [
            {
                "subtask_id": item.subtask_id,
                "blocked_by": list(item.blocked_by),
                "verifier_bundle_hash": item.verifier_bundle_hash,
                "max_turns": item.max_turns,
            }
            for item in items
            if not item.tombstoned
        ]
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def patch_hash(patch: PlanPatch) -> str:
    operations: list[dict[str, object]] = []
    for operation in patch.operations:
        if isinstance(operation, AddPlanItem):
            operations.append({"type": "add", "item": draft_item(operation.item).item_dict()})
        elif isinstance(operation, SplitPlanItem):
            operations.append({
                "type": "split",
                "subtask_id": operation.subtask_id,
                "replacements": [draft_item(item).item_dict() for item in operation.replacements],
            })
        elif isinstance(operation, UpdatePlanItemDependencies):
            operations.append({
                "type": "update_dependencies",
                "subtask_id": operation.subtask_id,
                "blocked_by": list(operation.blocked_by),
            })
        elif isinstance(operation, TombstonePlanItem):
            operations.append({"type": "tombstone", "subtask_id": operation.subtask_id})
    payload = {
        "reason": patch.reason,
        "trigger": patch.trigger,
        "evidence_refs": list(patch.evidence_refs),
        "operations": operations,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def revision_id(
    *,
    task_id: str,
    plan_id: int,
    parent_revision_id: str | None,
    revision_number: int,
    dag_digest: str,
    patch_digest: str,
) -> str:
    payload = {
        "task_id": task_id,
        "plan_id": plan_id,
        "parent_revision_id": parent_revision_id,
        "revision_number": revision_number,
        "dag_hash": dag_digest,
        "patch_hash": patch_digest,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"planrev_{digest}"


def initial_patch_hash(*, reason: str, trigger: str) -> str:
    payload = {"kind": "initial", "reason": reason, "trigger": trigger}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validate_snapshot(items: tuple[PlanRevisionItem, ...]) -> None:
    ids = [item.subtask_id for item in items]
    if len(ids) != len(set(ids)):
        raise PlanPatchError("plan revision contains duplicate subtask IDs")
    active = {item.subtask_id: item for item in items if not item.tombstoned}
    for item in active.values():
        if item.subtask_id in item.blocked_by:
            raise PlanPatchError(f"PlanItem {item.subtask_id} cannot depend on itself")
        missing = [dependency for dependency in item.blocked_by if dependency not in active]
        if missing:
            raise PlanPatchError(
                f"PlanItem {item.subtask_id} has missing or tombstoned dependencies: "
                + ", ".join(missing)
            )

    states = {subtask_id: 0 for subtask_id in active}

    def visit(subtask_id: str) -> None:
        if states[subtask_id] == 1:
            raise PlanPatchError("PlanPatch would create a dependency cycle")
        if states[subtask_id] == 2:
            return
        states[subtask_id] = 1
        for dependency in active[subtask_id].blocked_by:
            visit(dependency)
        states[subtask_id] = 2

    for subtask_id in active:
        visit(subtask_id)


def apply_patch(
    items: tuple[PlanRevisionItem, ...],
    statuses: Mapping[str, str],
    patch: PlanPatch,
) -> tuple[PlanRevisionItem, ...]:
    current = list(items)

    def index_for(subtask_id: str) -> int:
        for index, item in enumerate(current):
            if item.subtask_id == subtask_id:
                return index
        raise PlanPatchError(f"PlanItem does not exist: {subtask_id}")

    for operation in patch.operations:
        if isinstance(operation, AddPlanItem):
            if any(item.subtask_id == operation.item.subtask_id for item in current):
                raise PlanPatchError(f"PlanItem already exists: {operation.item.subtask_id}")
            current.append(draft_item(operation.item))
            continue

        if isinstance(operation, UpdatePlanItemDependencies):
            index = index_for(operation.subtask_id)
            item = current[index]
            if item.tombstoned or statuses.get(item.subtask_id) == "completed":
                raise PlanPatchError("completed or tombstoned PlanItems cannot be rewritten")
            current[index] = replace(item, blocked_by=operation.blocked_by)
            continue

        if isinstance(operation, TombstonePlanItem):
            index = index_for(operation.subtask_id)
            item = current[index]
            if item.tombstoned or statuses.get(item.subtask_id) != "pending":
                raise PlanPatchError("only an active pending PlanItem can be tombstoned")
            current[index] = replace(item, tombstoned=True)
            continue

        if isinstance(operation, SplitPlanItem):
            index = index_for(operation.subtask_id)
            item = current[index]
            if item.tombstoned or statuses.get(item.subtask_id) not in {"failed", "retryable"}:
                raise PlanPatchError("only an active failed or retryable PlanItem can be split")
            replacement_ids = [replacement.subtask_id for replacement in operation.replacements]
            if len(replacement_ids) != len(set(replacement_ids)) or any(
                any(existing.subtask_id == replacement_id for existing in current)
                for replacement_id in replacement_ids
            ):
                raise PlanPatchError("split replacement IDs must be new and unique")
            old_dependencies = set(item.blocked_by)
            if any(
                not old_dependencies.issubset(set(replacement.blocked_by))
                for replacement in operation.replacements
            ):
                raise PlanPatchError("split replacements must preserve original dependencies")
            current[index] = replace(item, tombstoned=True)
            current.extend(draft_item(replacement) for replacement in operation.replacements)
            for dependent_index, dependent in enumerate(current):
                if dependent.tombstoned or item.subtask_id not in dependent.blocked_by:
                    continue
                if statuses.get(dependent.subtask_id) == "completed":
                    raise PlanPatchError("split cannot rewrite a completed dependent PlanItem")
                dependencies = set(dependent.blocked_by)
                dependencies.remove(item.subtask_id)
                dependencies.update(replacement_ids)
                current[dependent_index] = replace(
                    dependent,
                    blocked_by=tuple(sorted(dependencies)),
                )

    result = tuple(current)
    before_by_id = {item.subtask_id: item for item in items}
    after_by_id = {item.subtask_id: item for item in result}
    for subtask_id, status in statuses.items():
        if status == "completed" and after_by_id.get(subtask_id) != before_by_id.get(subtask_id):
            raise PlanPatchError("completed PlanItems cannot be rewritten")
    validate_snapshot(result)
    if result == items:
        raise PlanPatchError("PlanPatch must change the PlanRevision")
    return result
