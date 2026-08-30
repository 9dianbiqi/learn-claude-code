from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field
from functools import partial
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


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _source_for(value: Any) -> str | None:
    try:
        return inspect.getsource(value)
    except (OSError, TypeError):
        return None


def _stable_object_state(value: Any, seen: set[int]) -> dict[str, Any] | None:
    state: dict[str, Any] = {}
    object_dict = getattr(value, "__dict__", None)
    if isinstance(object_dict, dict):
        state["dict"] = _stable_value(object_dict, seen)

    slot_values: dict[str, Any] = {}
    for owner in getattr(type(value), "__mro__", (type(value),)):
        slots = owner.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for slot in slots:
            if slot in {"__dict__", "__weakref__"} or slot in slot_values:
                continue
            try:
                slot_values[slot] = _stable_value(getattr(value, slot), seen)
            except AttributeError:
                continue
    if slot_values:
        state["slots"] = {
            slot: slot_values[slot] for slot in sorted(slot_values)
        }
    return state or None


def _stable_value(
    value: Any,
    seen: set[int] | None = None,
    globals_dict: dict[str, Any] | None = None,
) -> Any:
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    identity = id(value)
    if identity in seen:
        return {"cycle": f"{type(value).__module__}.{type(value).__qualname__}"}
    seen.add(identity)
    try:
        if isinstance(value, partial):
            return {"partial": _stable_callable_payload(value, seen)}
        if isinstance(value, CodeType):
            return {"code": _stable_code(value, seen, globals_dict)}
        if isinstance(value, list):
            return {"list": [_stable_value(item, seen, globals_dict) for item in value]}
        if isinstance(value, tuple):
            return {"tuple": [_stable_value(item, seen, globals_dict) for item in value]}
        if isinstance(value, dict):
            entries = [
                [
                    _stable_value(key, seen, globals_dict),
                    _stable_value(item, seen, globals_dict),
                ]
                for key, item in value.items()
            ]
            return {"dict": sorted(entries, key=lambda entry: _stable_json(entry[0]))}
        if isinstance(value, set):
            values = [_stable_value(item, seen, globals_dict) for item in value]
            return {"set": sorted(values, key=_stable_json)}
        if isinstance(value, type):
            return {
                "kind": "type",
                "module": value.__module__,
                "qualname": value.__qualname__,
                "source": _source_for(value),
            }
        if inspect.isfunction(value) or inspect.ismethod(value):
            return {"callable": _stable_callable_payload(value, seen)}

        qualified_type = f"{type(value).__module__}.{type(value).__qualname__}"
        callable_target = getattr(value, "__call__", None)
        return {
            "kind": "object",
            "type": qualified_type,
            "source": _source_for(type(value)),
            "callable": (
                _stable_callable_payload(value, seen)
                if callable_target is not None else None
            ),
            "state": _stable_object_state(value, seen),
        }
    finally:
        seen.remove(identity)


def _stable_code(
    code: CodeType,
    seen: set[int] | None = None,
    globals_dict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "flags": code.co_flags,
        "code": code.co_code.hex(),
        "consts": [_stable_value(value, seen, globals_dict) for value in code.co_consts],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
    }
    if globals_dict is not None:
        payload["globals"] = _stable_global_bindings(globals_dict, code, seen)
    return payload


def _stable_global_bindings(
    globals_dict: dict[str, Any] | None,
    code: CodeType,
    seen: set[int],
) -> dict[str, Any]:
    if not isinstance(globals_dict, dict):
        return {}
    return {
        name: _stable_value(globals_dict[name], seen)
        for name in sorted(code.co_names)
        if name != "__builtins__" and name in globals_dict
    }


def _stable_callable_payload(
    verifier: Callable[..., Any],
    seen: set[int] | None = None,
) -> dict[str, Any]:
    if seen is None:
        seen = set()
    if isinstance(verifier, partial):
        return {
            "kind": "partial",
            "function": _stable_callable_payload(verifier.func, seen),
            "args": _stable_value(verifier.args, seen),
            "keywords": _stable_value(verifier.keywords, seen),
        }
    target = verifier if getattr(verifier, "__code__", None) is not None else getattr(verifier, "__call__", verifier)
    code = getattr(target, "__code__", None)
    payload: dict[str, Any] = {
        "module": getattr(target, "__module__", type(verifier).__module__),
        "qualname": getattr(target, "__qualname__", type(verifier).__qualname__),
    }
    if code is not None:
        globals_dict = getattr(target, "__globals__", None)
        payload["code"] = _stable_code(
            code,
            seen,
            globals_dict if isinstance(globals_dict, dict) else None,
        )
        payload["defaults"] = _stable_value(getattr(target, "__defaults__", None), seen)
        payload["kwdefaults"] = _stable_value(getattr(target, "__kwdefaults__", None), seen)
        closure = getattr(target, "__closure__", None)
        payload["closure"] = (
            [_stable_value(cell.cell_contents, seen) for cell in closure]
            if closure is not None else None
        )
        payload["globals"] = _stable_global_bindings(globals_dict, code, seen)
        bound_self = getattr(target, "__self__", None)
        if bound_self is not None:
            payload["bound_self"] = {
                "type": f"{type(bound_self).__module__}.{type(bound_self).__qualname__}",
                "source": _source_for(type(bound_self)),
                "state": _stable_object_state(bound_self, seen),
            }
    else:
        payload["source"] = _source_for(target)
        payload["callable_type"] = f"{type(verifier).__module__}.{type(verifier).__qualname__}"
        payload["state"] = _stable_object_state(verifier, seen)
    return payload


def _verifier_implementation_hash(verifier: Callable[..., Any]) -> str:
    return _hash_payload(_stable_callable_payload(verifier))


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
        computed_implementation_hash = _verifier_implementation_hash(self.verifier)
        implementation_hash = self.verifier_implementation_hash
        if implementation_hash is None:
            implementation_hash = computed_implementation_hash
        else:
            implementation_hash = _normalize_sha256(
                implementation_hash,
                "verifier_implementation_hash",
            )
            if implementation_hash != computed_implementation_hash:
                raise ValueError("verifier_implementation_hash does not match verifier")
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
        return _hash_payload(payload)
