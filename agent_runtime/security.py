from __future__ import annotations

import fnmatch
import re


SENSITIVE_BASENAME_PATTERNS = (
    ".env",
    ".env.*",
    ".netrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "credentials",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "credentials.toml",
    "credentials.ini",
    "credentials.env",
    ".credentials",
    ".credentials.json",
    ".credentials.yaml",
    ".credentials.yml",
    ".credentials.toml",
    ".credentials.ini",
    ".credentials.env",
    "secrets",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.toml",
    "secrets.ini",
    "secrets.env",
    ".secrets",
    ".secrets.json",
    ".secrets.yaml",
    ".secrets.yml",
    ".secrets.toml",
    ".secrets.ini",
    ".secrets.env",
    "id_rsa",
    "id_ed25519",
)


def is_sensitive_relative(relative: str) -> bool:
    """Return True for repository paths that must never be exposed to tools."""
    normalized = str(relative or "").replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = [part.casefold() for part in normalized.split("/") if part]
    if ".git" in parts:
        return True
    return any(
        fnmatch.fnmatch(part, pattern.casefold())
        for part in parts
        for pattern in SENSITIVE_BASENAME_PATTERNS
    )


def glob_targets_sensitive(pattern: str) -> bool:
    """Detect a glob that explicitly names a protected credential family.

    Broad discovery patterns remain useful and are filtered by ToolExecutor;
    targeted secret globs fail at permission evaluation.
    """
    normalized = str(pattern or "").replace("\\", "/").casefold()
    markers = (
        ".env",
        ".git",
        ".netrc",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".keystore",
        "/credentials",
        "credentials.",
        "/secrets",
        "secrets.",
        "id_rsa",
        "id_ed25519",
    )
    return any(marker in normalized for marker in markers)


_SENSITIVE_COMMAND_PATH = re.compile(
    r"(?ix)"
    r"(?:^|[\s\"'=:/\\])"
    r"(?:"
    r"\.env(?:\.[a-z0-9_.-]+)?|"
    r"\.netrc|"
    r"\.git(?:[/\\][^\s\"'|;&<>]*)?|"
    r"[^\s\"'|;&<>]+\.(?:pem|key|p12|pfx|keystore)|"
    r"(?:credentials|secrets)(?:[/\\][^\s\"'|;&<>]*|\.(?:json|ya?ml|toml|ini|env))|"
    r"id_(?:rsa|ed25519)"
    r")"
    r"(?=$|[\s\"'|;&<>])"
)


def command_mentions_sensitive_path(command: str) -> bool:
    return _SENSITIVE_COMMAND_PATH.search(str(command or "")) is not None
