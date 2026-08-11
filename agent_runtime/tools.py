from __future__ import annotations

import glob as glob_module
import hashlib
import os
import subprocess
import tempfile
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file inside the repository.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "glob",
        "description": "Find files inside the repository using a glob pattern.",
        "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
    },
    {
        "name": "write_file",
        "description": "Write complete UTF-8 text to a repository file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace an exact text occurrence once in a repository file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "bash",
        "description": "Run a shell command in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]

MAX_READ_BYTES = 1 * 1024 * 1024
MAX_SHELL_STDOUT_BYTES = 50_000
MAX_SHELL_STDERR_BYTES = 50_000


@dataclass(frozen=True)
class ShellResult:
    output: str
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    status: str


class FileConflict(RuntimeError):
    """Raised when a file changes between precondition and final write check."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ToolExecutor:
    INTERNAL_NAMESPACE = ".agent_runtime"

    def __init__(self, repo_root: str | Path):
        self.repo_root = Path(repo_root).resolve()
        self.shell_timeout = 120.0
        self._expected_before: dict[str, dict[str, Any]] = {}

    def _is_internal_path(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.repo_root)
        except ValueError:
            return False
        return bool(relative.parts) and relative.parts[0].casefold() == self.INTERNAL_NAMESPACE.casefold()

    @classmethod
    def pattern_touches_internal(cls, pattern: str) -> bool:
        normalized = (pattern or "").replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            return False
        first = normalized.split("/", 1)[0]
        if first.casefold() == cls.INTERNAL_NAMESPACE.casefold():
            return True
        if not any(marker in first for marker in ("*", "?", "[")):
            return False
        if "/" not in normalized:
            return fnmatch.fnmatch(cls.INTERNAL_NAMESPACE, first)
        return True

    def safe_path(self, raw_path: str) -> Path:
        raw = str(raw_path or "")
        normalized = raw.replace("\\", "/")
        if not normalized or normalized in {".", "./"}:
            raise ValueError(f"A regular repository file path is required: {raw_path}")
        if normalized.startswith("//") or re.match(r"^[A-Za-z]:[^/]", normalized):
            raise ValueError(f"Unsupported absolute/drive-relative path: {raw_path}")
        if any(part == ".." for part in normalized.split("/")):
            raise ValueError(f"Path traversal is not allowed: {raw_path}")
        if any(":" in part for index, part in enumerate(normalized.split("/")) if not (index == 0 and re.match(r"^[A-Za-z]:$", part))):
            raise ValueError(f"Alternate data streams are not allowed: {raw_path}")
        candidate_unresolved = self.repo_root / raw
        for parent in [candidate_unresolved, *candidate_unresolved.parents]:
            if not parent.exists() or parent == self.repo_root.parent:
                continue
            try:
                stat_result = os.lstat(parent)
            except OSError:
                continue
            if parent.is_symlink() or bool(getattr(stat_result, "st_file_attributes", 0) & 0x0400):
                raise ValueError(f"Symlink/reparse point is not allowed: {raw_path}")
            if parent == self.repo_root:
                break
        candidate = candidate_unresolved.resolve()
        if not candidate.is_relative_to(self.repo_root):
            raise ValueError(f"Path escapes repository: {raw_path}")
        if self._is_internal_path(candidate):
            raise ValueError(f"Path is reserved for runtime internals: {raw_path}")
        return candidate

    def set_expected_before(self, raw_path: str, state: dict[str, Any]) -> None:
        self._expected_before[str(raw_path)] = dict(state)

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        handler = getattr(self, f"run_{name}", None)
        if handler is None:
            return f"Error: unknown tool {name}"
        return handler(**args)

    def file_state(self, raw_path: str) -> dict[str, Any]:
        file_path = self.safe_path(raw_path)
        if not file_path.exists():
            return {"path": str(file_path), "exists": False, "sha256": None, "identity": None}
        if not file_path.is_file():
            raise ValueError(f"Not a regular file: {raw_path}")
        stat_result = file_path.stat()
        return {
            "path": str(file_path),
            "exists": True,
            "sha256": sha256_file(file_path),
            "identity": {"st_dev": int(stat_result.st_dev), "st_ino": int(stat_result.st_ino)},
        }

    def prepare_file_write(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return before/after hashes without changing the file."""
        before = self.file_state(args["path"])
        if name == "write_file":
            content = args["content"]
            expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        elif name == "edit_file":
            if not before["exists"]:
                raise FileNotFoundError(args["path"])
            with self.safe_path(args["path"]).open("r", encoding="utf-8", newline="") as handle:
                text = handle.read()
            old_text = args["old_text"]
            if old_text not in text:
                raise ValueError(f"Text not found in {args['path']}")
            new_text = text.replace(old_text, args["new_text"], 1)
            expected = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
        else:
            raise ValueError(f"Not a file write tool: {name}")
        return before, {"path": before["path"], "exists": True, "sha256": expected, "identity": None}

    def _atomic_text_replace(self, file_path: Path, content: str) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{file_path.name}.", suffix=".agent-tmp", dir=file_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, file_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _check_expected_before(self, path: str) -> None:
        expected = self._expected_before.pop(str(path), None)
        if expected is not None and self.file_state(path) != expected:
            raise FileConflict(f"File changed after precondition check: {path}")

    def run_read_file(self, path: str, limit: int | None = None) -> str:
        file_path = self.safe_path(path)
        raw = file_path.read_bytes()
        if len(raw) > MAX_READ_BYTES:
            raise ValueError(f"File size exceeds {MAX_READ_BYTES} byte read limit: {path}")
        text = raw.decode("utf-8", errors="replace")
        if limit is not None:
            lines = text.splitlines(keepends=True)
            if limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "".join(lines)
        return text

    def run_glob(self, pattern: str) -> str:
        normalized = (pattern or "").replace("\\", "/")
        if normalized.startswith("/") or normalized.startswith("//") or re.match(r"^[A-Za-z]:", normalized):
            raise ValueError(f"Glob must be repository-relative: {pattern}")
        if any(part == ".." for part in normalized.split("/")) or any(":" in part for part in normalized.split("/")):
            raise ValueError(f"Glob contains an unsafe path component: {pattern}")
        matches = []
        for item in glob_module.glob(pattern, root_dir=self.repo_root, recursive=True):
            try:
                candidate = self.safe_path(item)
            except ValueError:
                continue
            if candidate.is_relative_to(self.repo_root) and not self._is_internal_path(candidate):
                matches.append(item.replace(os.sep, "/"))
        return "\n".join(sorted(matches)) if matches else "(no matches)"

    def run_write_file(self, path: str, content: str) -> str:
        file_path = self.safe_path(path)
        self._check_expected_before(path)
        self._atomic_text_replace(file_path, content)
        return f"Wrote {len(content.encode('utf-8'))} bytes to {path}"

    def run_edit_file(self, path: str, old_text: str, new_text: str) -> str:
        file_path = self.safe_path(path)
        self._check_expected_before(path)
        with file_path.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        if old_text not in text:
            return f"Error: text not found in {path}"
        self._atomic_text_replace(file_path, text.replace(old_text, new_text, 1))
        return f"Edited {path}"

    def run_bash(self, command: str) -> ShellResult:
        lowered = (command or "").casefold()
        if self.INTERNAL_NAMESPACE.casefold() in lowered or re.search(r"\.agent[_*?\[]|agent[_-]?runtime", lowered):
            raise ValueError("Runtime internal namespace is reserved")
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.shell_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _cap_output(exc.stdout)
            stderr = _cap_output(exc.stderr)
            output = ((stdout or "") + (stderr or "")).strip() or f"Error: Timeout ({self.shell_timeout:g}s)"
            return ShellResult(output, stdout, stderr, None, True, "timed_out")
        stdout = _cap_output(completed.stdout)
        stderr = _cap_output(completed.stderr)
        output = ((stdout or "") + (stderr or "")).strip() or "(no output)"
        status = "succeeded" if completed.returncode == 0 else "nonzero"
        return ShellResult(output, stdout, stderr, completed.returncode, False, status)


def _cap_output(value: Any, limit: int = MAX_SHELL_STDOUT_BYTES) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    if len(text.encode("utf-8")) <= limit:
        return text
    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore") + "... [truncated]"
