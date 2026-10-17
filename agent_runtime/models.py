from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)

    def as_content(self) -> dict[str, Any]:
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.stop_reason is None:
            self.stop_reason = "tool_use" if self.tool_calls else "end_turn"

    @property
    def content(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        blocks.extend(call.as_content() for call in self.tool_calls)
        return blocks

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class RunResult:
    task_id: str
    status: str
    final_text: str = ""
    error: str | None = None


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def normalize_evidence_path(path: str) -> str:
    value = str(path).replace("\\", "/")
    if not value or value.startswith("/") or value.startswith("//"):
        raise ValueError(f"evidence path must be repository-relative: {path!r}")
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise ValueError(f"evidence path must be repository-relative: {path!r}")
    parts = [part for part in PurePosixPath(value).parts if part not in {"", "."}]
    if not parts or ".." in parts:
        raise ValueError(f"evidence path must be repository-relative: {path!r}")
    return "/".join(parts)


def is_valid_sha256(value: str) -> bool:
    return bool(_SHA256_HEX.fullmatch(str(value)))


@dataclass(frozen=True)
class VerifierContext:
    repo_root: str
    task_id: str
    plan_item_id: int
    subtask_id: str
    completion_summary: str
    execution_checkpoint_id: int


@dataclass(frozen=True)
class VerifierResult:
    status: str
    summary: str
    evidence_manifest: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "uncertain"}:
            raise ValueError("verifier status must be pass, fail, or uncertain")
        if not isinstance(self.summary, str):
            raise TypeError("verifier summary must be a string")
        manifest: list[dict[str, str]] = []
        for entry in self.evidence_manifest:
            if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
                raise ValueError("evidence manifest entries require path and sha256")
            manifest.append({"path": str(entry["path"]), "sha256": str(entry["sha256"])})
        object.__setattr__(self, "evidence_manifest", manifest)


@dataclass(frozen=True)
class VerifiedSubtaskConfig:
    subtask_id: str
    description: str
    completion_criteria: str
    evidence_paths: tuple[str, ...]
    verifier_id: str
    verifier_version: str
    verification_rule: str
    verifier: Callable[[VerifierContext], VerifierResult]
    verifier_implementation_hash: str
    blocked_by: tuple[str, ...] = ()
    max_turns: int = 1

    def __post_init__(self) -> None:
        for field_name in (
            "subtask_id",
            "description",
            "completion_criteria",
            "verifier_id",
            "verifier_version",
            "verification_rule",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        normalized = tuple(sorted(normalize_evidence_path(path) for path in self.evidence_paths))
        if len(normalized) != len(set(normalized)):
            raise ValueError("evidence_paths must not contain duplicates")
        if not callable(self.verifier):
            raise TypeError("verifier must be callable")
        implementation_hash = self.verifier_implementation_hash
        if not isinstance(implementation_hash, str) or not implementation_hash.strip():
            raise ValueError("verifier_implementation_hash must be a non-empty string")
        if isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int) or self.max_turns < 1:
            raise ValueError("max_turns must be a positive integer")
        if isinstance(self.blocked_by, str):
            raise TypeError("blocked_by must be a collection of subtask IDs")
        try:
            dependencies = tuple(self.blocked_by)
        except TypeError as exc:
            raise TypeError("blocked_by must be a collection of subtask IDs") from exc
        if any(not isinstance(dependency, str) or not dependency.strip() for dependency in dependencies):
            raise ValueError("blocked_by must contain non-empty string subtask IDs")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("blocked_by must not contain duplicates")
        object.__setattr__(self, "evidence_paths", normalized)
        object.__setattr__(self, "verifier_implementation_hash", implementation_hash)
        object.__setattr__(self, "blocked_by", tuple(sorted(dependencies)))

    @property
    def verifier_bundle_hash(self) -> str:
        payload = {
            "subtask_id": self.subtask_id,
            "description": self.description,
            "completion_criteria": self.completion_criteria,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "verification_rule": self.verification_rule,
            "verifier_implementation_hash": self.verifier_implementation_hash,
            "evidence_paths": list(self.evidence_paths),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, init=False)
class VerifiedSubtaskDAGConfig:
    """A validated, immutable, deterministically ordered verified-subtask DAG."""

    nodes: tuple[VerifiedSubtaskConfig, ...]

    def __init__(
        self,
        nodes: tuple[VerifiedSubtaskConfig, ...] | list[VerifiedSubtaskConfig] | None = None,
    ) -> None:
        raw_nodes = nodes if nodes is not None else ()
        normalized: list[VerifiedSubtaskConfig] = []
        for node in raw_nodes:
            if isinstance(node, VerifiedSubtaskConfig):
                normalized.append(node)
            else:
                raise TypeError("DAG nodes must be VerifiedSubtaskConfig instances")
        if not normalized:
            raise ValueError("verified subtask DAG must not be empty")

        ids = [node.subtask_id for node in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("verified subtask DAG must not contain duplicate subtask IDs")
        node_by_id = {node.subtask_id: node for node in normalized}
        known_ids = set(ids)
        for node in normalized:
            if node.subtask_id in node.blocked_by:
                raise ValueError(f"subtask {node.subtask_id} cannot depend on itself")
            missing = [
                dependency for dependency in node.blocked_by if dependency not in known_ids
            ]
            if missing:
                raise ValueError(
                    f"subtask {node.subtask_id} depends on missing subtask(s): {', '.join(missing)}"
                )

        states: dict[str, int] = {subtask_id: 0 for subtask_id in ids}

        def visit(subtask_id: str) -> None:
            if states[subtask_id] == 1:
                raise ValueError("verified subtask DAG must not contain cycles")
            if states[subtask_id] == 2:
                return
            states[subtask_id] = 1
            node = node_by_id[subtask_id]
            for dependency in node.blocked_by:
                visit(dependency)
            states[subtask_id] = 2

        for subtask_id in ids:
            visit(subtask_id)
        object.__setattr__(self, "nodes", tuple(normalized))

    @property
    def dag_hash(self) -> str:
        payload = {
            "nodes": [
                {
                    "subtask_id": node.subtask_id,
                    "blocked_by": list(node.blocked_by),
                    "verifier_bundle_hash": node.verifier_bundle_hash,
                    "max_turns": node.max_turns,
                }
                for node in self.nodes
            ]
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_subtask_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{label} must be a collection of subtask IDs")
    normalized = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in normalized):
        raise ValueError(f"{label} must contain non-empty string subtask IDs")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class PlanItemDraft:
    subtask_id: str
    description: str = ""
    blocked_by: tuple[str, ...] = ()
    verifier_bundle_hash: str | None = None
    max_turns: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.subtask_id, str) or not self.subtask_id.strip():
            raise ValueError("subtask_id must be a non-empty string")
        if not isinstance(self.description, str):
            raise TypeError("description must be a string")
        if isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int) or self.max_turns < 1:
            raise ValueError("max_turns must be a positive integer")
        if self.verifier_bundle_hash is not None and not is_valid_sha256(
            self.verifier_bundle_hash
        ):
            raise ValueError("verifier_bundle_hash must be a lowercase SHA-256 value")
        object.__setattr__(
            self,
            "blocked_by",
            _normalize_subtask_ids(self.blocked_by, "blocked_by"),
        )


@dataclass(frozen=True)
class PlanRevisionItem:
    subtask_id: str
    description: str
    blocked_by: tuple[str, ...]
    verifier_bundle_hash: str | None
    max_turns: int
    tombstoned: bool = False

    def item_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "description": self.description,
            "blocked_by": list(self.blocked_by),
            "verifier_bundle_hash": self.verifier_bundle_hash,
            "max_turns": self.max_turns,
            "tombstoned": self.tombstoned,
        }


@dataclass(frozen=True)
class AddPlanItem:
    item: PlanItemDraft

    def __post_init__(self) -> None:
        if not isinstance(self.item, PlanItemDraft):
            raise TypeError("AddPlanItem.item must be a PlanItemDraft")


@dataclass(frozen=True)
class SplitPlanItem:
    subtask_id: str
    replacements: tuple[PlanItemDraft, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.subtask_id, str) or not self.subtask_id.strip():
            raise ValueError("subtask_id must be a non-empty string")
        replacements = tuple(self.replacements)
        if len(replacements) < 2 or any(
            not isinstance(item, PlanItemDraft) for item in replacements
        ):
            raise ValueError("SplitPlanItem requires at least two PlanItemDraft replacements")
        object.__setattr__(self, "replacements", replacements)


@dataclass(frozen=True)
class UpdatePlanItemDependencies:
    subtask_id: str
    blocked_by: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.subtask_id, str) or not self.subtask_id.strip():
            raise ValueError("subtask_id must be a non-empty string")
        object.__setattr__(
            self,
            "blocked_by",
            _normalize_subtask_ids(self.blocked_by, "blocked_by"),
        )


@dataclass(frozen=True)
class TombstonePlanItem:
    subtask_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.subtask_id, str) or not self.subtask_id.strip():
            raise ValueError("subtask_id must be a non-empty string")


@dataclass(frozen=True)
class PlanPatch:
    reason: str
    trigger: str
    operations: tuple[
        AddPlanItem | SplitPlanItem | UpdatePlanItemDependencies | TombstonePlanItem,
        ...,
    ]
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("PlanPatch.reason must be a non-empty string")
        if not isinstance(self.trigger, str) or not self.trigger.strip():
            raise ValueError("PlanPatch.trigger must be a non-empty string")
        operations = tuple(self.operations)
        allowed = (
            AddPlanItem,
            SplitPlanItem,
            UpdatePlanItemDependencies,
            TombstonePlanItem,
        )
        if not operations or any(not isinstance(operation, allowed) for operation in operations):
            raise ValueError("PlanPatch requires typed operations")
        evidence_refs = tuple(self.evidence_refs)
        if any(not isinstance(ref, str) or not ref.strip() for ref in evidence_refs):
            raise ValueError("evidence_refs must contain non-empty strings")
        if len(evidence_refs) != len(set(evidence_refs)):
            raise ValueError("evidence_refs must not contain duplicates")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "evidence_refs", tuple(sorted(evidence_refs)))


@dataclass(frozen=True)
class PlanRevision:
    revision_id: str
    plan_id: int
    task_id: str
    parent_revision_id: str | None
    revision_number: int
    dag_hash: str
    patch_hash: str
    reason: str
    trigger: str
    evidence_refs: tuple[str, ...]
    items: tuple[PlanRevisionItem, ...]
    created_at: float

    def item(self, subtask_id: str) -> PlanRevisionItem:
        for item in self.items:
            if item.subtask_id == subtask_id:
                return item
        raise KeyError(f"PlanItem not found in revision: {subtask_id}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "parent_revision_id": self.parent_revision_id,
            "revision_number": self.revision_number,
            "dag_hash": self.dag_hash,
            "patch_hash": self.patch_hash,
            "reason": self.reason,
            "trigger": self.trigger,
            "evidence_refs": list(self.evidence_refs),
            "items": [item.item_dict() for item in self.items],
            "created_at": self.created_at,
        }
