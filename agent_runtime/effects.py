from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class EffectSemantics(str, Enum):
    REPLAY_SAFE = "replay_safe"
    IDEMPOTENT = "idempotent"
    RECONCILABLE = "reconcilable"
    OPAQUE = "opaque"


@dataclass(frozen=True)
class OperationSpec:
    task_id: str
    tool_use_id: str
    adapter: str
    semantics: EffectSemantics
    effect_scope: str
    dedupe_key: str
    idempotency_key: str | None
    args_hash: str
    request: dict[str, Any]


@dataclass(frozen=True)
class ReconcileEvidence:
    outcome: str
    reason: str
    evidence: dict[str, Any]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_dedupe_key(task_id: str, tool_use_id: str) -> str:
    return "dedupe_" + hashlib.sha256(f"{task_id}\0{tool_use_id}".encode("utf-8")).hexdigest()


def stable_operation_id(task_id: str, tool_use_id: str) -> str:
    return "op_" + hashlib.sha256(f"{task_id}\0{tool_use_id}".encode("utf-8")).hexdigest()[:48]


def semantics_for_effect(effect: str) -> EffectSemantics:
    if effect == "read_only":
        return EffectSemantics.REPLAY_SAFE
    if effect == "file_write":
        return EffectSemantics.RECONCILABLE
    if effect == "idempotent":
        return EffectSemantics.IDEMPOTENT
    return EffectSemantics.OPAQUE


__all__ = [
    "EffectSemantics",
    "OperationSpec",
    "ReconcileEvidence",
    "canonical_json",
    "semantics_for_effect",
    "sha256_json",
    "stable_dedupe_key",
    "stable_operation_id",
]
