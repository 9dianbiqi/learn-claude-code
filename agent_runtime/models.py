from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import CodeType
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


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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


def _normalize_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase 64-hex SHA-256 digest")
    return value


def normalize_evidence_manifest(
    manifest: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if not isinstance(manifest, list):
        raise ValueError("evidence manifest must be a list")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in manifest:
        if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
            raise ValueError("evidence manifest entries require path and sha256")
        path = normalize_evidence_path(entry["path"])
        if path in seen:
            raise ValueError(f"duplicate evidence path: {path}")
        seen.add(path)
        normalized.append({
            "path": path,
            "sha256": _normalize_sha256(entry["sha256"], "evidence sha256"),
        })
    return sorted(normalized, key=lambda entry: entry["path"])


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, CodeType):
        return {"code": _stable_code(value)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, set):
        return sorted((_stable_value(item) for item in value), key=repr)
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
        "repr": repr(value),
    }


def _stable_code(code: CodeType) -> dict[str, Any]:
    return {
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
        "code": code.co_code.hex(),
        "consts": [_stable_value(value) for value in code.co_consts],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
    }


def _verifier_implementation_hash(verifier: Callable[..., Any]) -> str:
    target = verifier if getattr(verifier, "__code__", None) is not None else getattr(verifier, "__call__", verifier)
    code = getattr(target, "__code__", None)
    payload: dict[str, Any] = {
        "module": getattr(target, "__module__", type(verifier).__module__),
        "qualname": getattr(target, "__qualname__", type(verifier).__qualname__),
    }
    if code is not None:
        payload["code"] = _stable_code(code)
        payload["defaults"] = _stable_value(getattr(target, "__defaults__", None))
        payload["kwdefaults"] = _stable_value(getattr(target, "__kwdefaults__", None))
        closure = getattr(target, "__closure__", None)
        payload["closure"] = (
            [_stable_value(cell.cell_contents) for cell in closure]
            if closure is not None else None
        )
    else:
        try:
            payload["source"] = inspect.getsource(target)
        except (OSError, TypeError):
            payload["source"] = None
        payload["callable_type"] = f"{type(verifier).__module__}.{type(verifier).__qualname__}"
        payload["state"] = _stable_value(getattr(verifier, "__dict__", None))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        object.__setattr__(self, "evidence_manifest", normalize_evidence_manifest(self.evidence_manifest))


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
    verifier_implementation_hash: str | None = None

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
        if implementation_hash is None:
            implementation_hash = _verifier_implementation_hash(self.verifier)
        else:
            implementation_hash = _normalize_sha256(
                implementation_hash,
                "verifier_implementation_hash",
            )
        object.__setattr__(self, "evidence_paths", normalized)
        object.__setattr__(self, "verifier_implementation_hash", implementation_hash)

    @property
    def verifier_bundle_hash(self) -> str:
        payload = {
            "subtask_id": self.subtask_id,
            "description": self.description,
            "completion_criteria": self.completion_criteria,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "verification_rule": self.verification_rule,
            "evidence_paths": list(self.evidence_paths),
            "verifier_implementation_hash": self.verifier_implementation_hash,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
