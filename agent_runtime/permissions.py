from __future__ import annotations

import fnmatch
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .security import command_mentions_sensitive_path, glob_targets_sensitive, is_sensitive_relative

try:
    import yaml
except ImportError:  # pragma: no cover - requirements include PyYAML
    yaml = None


@dataclass(frozen=True)
class PermissionDecision:
    effect: str
    rule_id: str
    reason: str
    risk: str


class PermissionEngine:
    """Fail-closed allow/ask/deny policy evaluation."""

    _file_tools = {"read_file", "glob", "write_file", "edit_file"}
    _known_tools = _file_tools | {"bash"}
    _internal_namespace = ".agent_runtime"

    _hard_deny = (
        r"\brm\s+-rf\b",
        r"\bsudo\b",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bmkfs(?:\.|\s|$)",
        r"\bdd\s+if=",
        r">\s*/dev/",
        r"remove-item\s+.*(?:-recurse|-force)",
        r"\bformat(?:\.com)?\b",
        r"\bdiskpart\b",
        r"git\s+reset\s+--hard",
        r"git\s+clean\s+-[a-z-]*f",
    )
    _shell_hard_deny = (
        r"(?:^|\s)(?:cmd(?:\.exe)?\s+/c|powershell(?:\.exe)?\s+-(?:enc|encodedcommand)\b)",
        r"(?:^|\s)(?:python|python3|node|nodejs|bash|sh|pwsh)(?:\.exe)?\s+-(?:c|e|p|command)\b",
        r"(?:^|\s)(?:powershell|pwsh)(?:\.exe)?\s+-command\b",
        r"(?:^|\s)(?:python|python3|node|nodejs)(?:\.exe)?\s+(?!-m\b|-c\b|-e\b|-p\b)[^\s]+",
        r"(?:^|\s)(?:powershell|pwsh)(?:\.exe)?\s+-file\b",
        r"\b(?:invoke-expression|iex|start-process)\b",
        r"(?:^|\s)(?:curl|wget)\b[^\r\n]*\|",
    )
    _read_only_commands = (
        r"git\s+(?:status|diff|log)(?:\s+.*)?",
        r"rg\s+.+",
        r"(?:get-childitem|dir|ls)(?:\s+.*)?",
        r"(?:cat|type|more)\s+.+",
        r"python\s+-m\s+pytest\s+--collect-only(?:\s+.*)?",
    )

    def __init__(self, repo_root: str | Path, policy_path: str | Path | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.rules = self._load_rules(policy_path)

    @staticmethod
    def normalize_command(command: str) -> str:
        normalized = unicodedata.normalize("NFKC", command or "")
        return re.sub(r"\s+", " ", normalized.strip()).lower()

    def canonical_path(self, raw_path: str) -> Path:
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
        unresolved = self.repo_root / raw
        for parent in [unresolved, *unresolved.parents]:
            if not parent.exists() or parent == self.repo_root.parent:
                continue
            try:
                stat_result = parent.lstat()
            except OSError:
                continue
            if parent.is_symlink() or bool(getattr(stat_result, "st_file_attributes", 0) & 0x0400):
                raise ValueError(f"Symlink/reparse point is not allowed: {raw_path}")
            if parent == self.repo_root:
                break
        path = unresolved.resolve()
        if not path.is_relative_to(self.repo_root):
            raise ValueError(f"Path escapes repository: {raw_path}")
        return path

    def relative_path(self, raw_path: str) -> str:
        return self.canonical_path(raw_path).relative_to(self.repo_root).as_posix()

    def classify_effect(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name in {"read_file", "glob"}:
            return "read_only"
        if tool_name in {"write_file", "edit_file"}:
            return "file_write"
        if tool_name == "bash":
            command = self.normalize_command(args.get("command", ""))
            if any(re.fullmatch(pattern, command) for pattern in self._read_only_commands):
                return "read_only"
            return "unknown_write"
        return "unknown_write"

    def evaluate(self, tool_name: str, args: dict[str, Any]) -> PermissionDecision:
        if tool_name in {"read_file", "write_file", "edit_file"}:
            try:
                relative = self.relative_path(args.get("path", ""))
            except (TypeError, ValueError) as exc:
                return PermissionDecision("deny", "invariant.path-within-repo", str(exc), "file_write")
            if self._is_internal_relative(relative):
                return PermissionDecision(
                    "deny",
                    "invariant.runtime-internal-path",
                    "Runtime internal namespace is reserved",
                    self.classify_effect(tool_name, args),
                )
            if is_sensitive_relative(relative):
                return PermissionDecision(
                    "deny",
                    "invariant.sensitive-path",
                    "Sensitive credential material is protected by default",
                    self.classify_effect(tool_name, args),
                )
        else:
            relative = ""

        if tool_name == "glob" and glob_targets_sensitive(args.get("pattern", "")):
            return PermissionDecision(
                "deny",
                "invariant.sensitive-path",
                "Sensitive credential material is protected by default",
                "read_only",
            )
        if tool_name == "glob" and self._pattern_touches_internal(args.get("pattern", "")):
            return PermissionDecision(
                "deny",
                "invariant.runtime-internal-path",
                "Runtime internal namespace is reserved",
                "read_only",
            )

        if tool_name == "bash":
            command = self.normalize_command(args.get("command", ""))
            if self._internal_namespace in command or re.search(r"\.agent[_*?\[]|agent[_-]?runtime", command):
                return PermissionDecision(
                    "deny",
                    "invariant.runtime-internal-path",
                    "Runtime internal namespace is reserved",
                    "unknown_write",
                )
            if command_mentions_sensitive_path(command):
                return PermissionDecision(
                    "deny",
                    "invariant.sensitive-path",
                    "Shell access to sensitive credential material is denied",
                    "unknown_write",
                )
            for pattern in self._hard_deny:
                if re.search(pattern, command, flags=re.IGNORECASE):
                    return PermissionDecision("deny", "invariant.dangerous-shell", f"Dangerous shell pattern: {pattern}", "unknown_write")
            for pattern in self._shell_hard_deny:
                if re.search(pattern, command, flags=re.IGNORECASE):
                    return PermissionDecision("deny", "invariant.shell-wrapper", "Indirect shell execution is denied", "unknown_write")
            if self._contains_external_path(command):
                return PermissionDecision("deny", "invariant.shell-external-path", "Shell path is outside the repository boundary", "unknown_write")
            if re.search(r"\$\(|\$\{|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%|`", command):
                return PermissionDecision("deny", "invariant.shell-expansion", "Shell expansion is denied in the MVP", "unknown_write")
            composition = self._contains_shell_composition(command)
        else:
            composition = False

        matches: list[dict[str, Any]] = []
        for rule in self.rules:
            if self._matches(rule, tool_name, args, relative):
                matches.append(rule)
        if matches:
            rank = {"deny": 3, "ask": 2, "allow": 1}
            rule = max(matches, key=lambda item: rank.get(str(item.get("effect", "deny")), 3))
            effect = str(rule.get("effect", "deny"))
            if composition and effect == "allow":
                return PermissionDecision(
                    "ask",
                    "invariant.shell-composition",
                    "Composed shell commands require explicit approval",
                    self.classify_effect(tool_name, args),
                )
            return PermissionDecision(effect, str(rule.get("id", "unnamed")), str(rule.get("reason", effect)), self.classify_effect(tool_name, args))

        if self._has_scoped_allow_or_ask(tool_name):
            return PermissionDecision(
                "deny",
                "policy.no-matching-scope",
                "No allow/ask rule matched this tool scope",
                self.classify_effect(tool_name, args),
            )

        defaults = {
            "read_file": ("allow", "default.read", "Read-only file access"),
            "glob": ("allow", "default.glob", "Read-only file listing"),
            "write_file": ("ask", "default.file-write", "Writing a repository file"),
            "edit_file": ("ask", "default.file-edit", "Editing a repository file"),
            "bash": ("ask", "default.shell", "Shell command is not in the read-only allowlist"),
        }
        effect, rule_id, reason = defaults.get(tool_name, ("deny", "default.unknown-tool", "Unknown tool"))
        if composition:
            return PermissionDecision("ask", "invariant.shell-composition", "Composed shell commands require explicit approval", self.classify_effect(tool_name, args))
        return PermissionDecision(effect, rule_id, reason, self.classify_effect(tool_name, args))

    def _matches(self, rule: dict[str, Any], tool_name: str, args: dict[str, Any], relative: str) -> bool:
        tools = rule.get("tools", [])
        if tools and tool_name not in tools and "*" not in tools:
            return False
        path_patterns = rule.get("paths")
        if path_patterns:
            if tool_name in {"read_file", "write_file", "edit_file"}:
                if not any(fnmatch.fnmatch(relative, str(pattern)) for pattern in path_patterns):
                    return False
            elif tool_name == "glob":
                if not self._glob_matches_paths(args.get("pattern", ""), path_patterns):
                    return False
            else:
                return False
        command_pattern = rule.get("command_regex")
        if command_pattern:
            if tool_name != "bash":
                return False
            if re.fullmatch(str(command_pattern), self.normalize_command(args.get("command", ""))) is None:
                return False
        return True

    def _has_scoped_allow_or_ask(self, tool_name: str) -> bool:
        for rule in self.rules:
            if rule.get("effect") not in {"allow", "ask"}:
                continue
            if not (tool_name in rule.get("tools", []) or "*" in rule.get("tools", [])):
                continue
            if rule.get("paths") or rule.get("command_regex"):
                return True
        return False

    @staticmethod
    def _contains_shell_composition(command: str) -> bool:
        return bool(re.search(r"\|\||&&|[|><;&]|`|\$\(|\$\{|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%", command))

    @staticmethod
    def _contains_external_path(command: str) -> bool:
        if re.search(r"(?:^|[\s/\\=\"'`()])\.\.(?=$|[\s/\\=\"'`()])", command):
            return True
        if re.search(r"(?:^|[\s=\"'])(?:[A-Za-z]:[\\/]|\\\\|/[^\s]+)", command):
            return True
        return False

    @classmethod
    def _is_internal_relative(cls, relative: str) -> bool:
        return bool(relative.split("/", 1)[0].casefold() == cls._internal_namespace.casefold())

    @classmethod
    def _pattern_touches_internal(cls, pattern: str) -> bool:
        normalized = (pattern or "").replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            return False
        first = normalized.split("/", 1)[0]
        if first.casefold() == cls._internal_namespace.casefold():
            return True
        if not any(marker in first for marker in ("*", "?", "[")):
            return False
        if "/" not in normalized:
            return fnmatch.fnmatch(cls._internal_namespace, first)
        return True

    @staticmethod
    def _glob_matches_paths(pattern: str, path_patterns: list[Any]) -> bool:
        normalized = (pattern or "").replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if (
            normalized.startswith("/")
            or normalized.startswith("//")
            or re.match(r"^[A-Za-z]:", normalized)
            or any(part == ".." for part in normalized.split("/"))
            or any(":" in part for part in normalized.split("/"))
        ):
            return False
        pattern_prefix = re.split(r"[*?\[]", normalized, maxsplit=1)[0].rstrip("/")
        for selector in path_patterns:
            selector_text = str(selector).replace("\\", "/")
            selector_prefix = re.split(r"[*?\[]", selector_text, maxsplit=1)[0].rstrip("/")
            if pattern_prefix == selector_prefix or pattern_prefix.startswith(selector_prefix + "/"):
                return True
        return False

    def _load_rules(self, policy_path: str | Path | None) -> list[dict[str, Any]]:
        if policy_path is None:
            return []
        path = Path(policy_path)
        if not path.exists():
            return []
        if yaml is None:
            raise RuntimeError("PyYAML is required to load a permission policy")
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rules = document.get("rules", [])
        if not isinstance(rules, list):
            raise ValueError("Permission policy 'rules' must be a list")
        normalized = []
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("Permission policy rules must be mappings")
            item = dict(rule)
            if item.get("effect") not in {"allow", "ask", "deny"}:
                raise ValueError(f"Invalid permission effect: {item.get('effect')!r}")
            tools = item.get("tools")
            if not isinstance(tools, list) or not tools:
                raise ValueError("Permission policy rule must declare non-empty tools")
            if any(not isinstance(tool, str) or tool not in self._known_tools | {"*"} for tool in tools):
                raise ValueError(f"Invalid permission tools: {tools!r}")
            paths = item.get("paths")
            if paths is not None:
                if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path for path in paths):
                    raise ValueError("Permission policy paths must be a non-empty list of strings")
                if "*" in tools or not set(tools).issubset(self._file_tools):
                    raise ValueError("Permission policy paths require file tools")
            command_pattern = item.get("command_regex")
            if command_pattern is not None:
                if not isinstance(command_pattern, str) or not command_pattern:
                    raise ValueError("Permission policy command_regex must be a non-empty string")
                if tools != ["bash"]:
                    raise ValueError("Permission policy command_regex requires tools: ['bash']")
                try:
                    re.compile(command_pattern)
                except re.error as exc:
                    raise ValueError(f"Invalid permission command_regex: {exc}") from exc
            if paths is not None and command_pattern is not None:
                raise ValueError("Permission policy rule cannot combine paths and command_regex")
            normalized.append(item)
        return normalized
