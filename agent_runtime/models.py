from __future__ import annotations

import hashlib
import json
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


def _normalize_evidence_path(path: str) -> str:
    value = str(path).replace("\\", "/")
    if not value or value.startswith("/") or value.startswith("//"):
        raise ValueError(f"evidence path must be repository-relative: {path!r}")
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise ValueError(f"evidence path must be repository-relative: {path!r}")
    parts = [part for part in PurePosixPath(value).parts if part not in {"", "."}]
    if not parts or ".." in parts:
        raise ValueError(f"evidence path must be repository-relative: {path!r}")
    return "/".join(parts)


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
        normalized = tuple(sorted(_normalize_evidence_path(path) for path in self.evidence_paths))
        if len(normalized) != len(set(normalized)):
            raise ValueError("evidence_paths must not contain duplicates")
        if not callable(self.verifier):
            raise TypeError("verifier must be callable")
        object.__setattr__(self, "evidence_paths", normalized)

    @property
    def verifier_bundle_hash(self) -> str:
        payload = {
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "verification_rule": self.verification_rule,
            "evidence_paths": list(self.evidence_paths),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
