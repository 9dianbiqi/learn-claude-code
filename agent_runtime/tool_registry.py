from __future__ import annotations

import asyncio
import time
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .mcp_client import MCPClient
from .store import EventStore
from .tools import TOOL_SCHEMAS, ToolExecutor


BUILTIN_EFFECT_KINDS = {
    "read_file": "read_only",
    "glob": "read_only",
    "write_file": "file_write",
    "edit_file": "file_write",
    "bash": "unknown_write",
}


def normalize_mcp_name(server_name: str, tool_name: str) -> str:
    def _normalize(value: str) -> str:
        return re.sub(r"_+", "_", re.sub(r"[^0-9A-Za-z]+", "_", value)).strip("_").casefold()

    return f"mcp__{_normalize(server_name)}__{_normalize(tool_name)}"


@dataclass(frozen=True)
class ToolRegistration:
    registration_id: str
    tool_name: str
    adapter_kind: str
    connection_id: str | None
    server_name: str | None
    source_tool_name: str | None
    description: str
    schema: dict[str, Any]
    effect_kind: str
    permission_requirements: dict[str, Any]
    timeout_seconds: float | None
    enabled: bool
    version: int
    created_at: float
    updated_at: float
    adapter: Callable[[dict[str, Any]], Any] | None = None


class ToolRegistry:
    def __init__(
        self,
        repo_root: str | Path,
        executor: ToolExecutor | None = None,
        store: EventStore | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.executor = executor or ToolExecutor(self.repo_root)
        self.store = store
        self._entries: dict[str, ToolRegistration] = {}

    def install_builtins(self) -> None:
        now = time.time()
        for schema in TOOL_SCHEMAS:
            name = str(schema["name"])
            entry = ToolRegistration(
                registration_id=f"builtin_{name}",
                tool_name=name,
                adapter_kind="builtin",
                connection_id=None,
                server_name=None,
                source_tool_name=None,
                description=str(schema["description"]),
                schema=dict(schema["input_schema"]),
                effect_kind=BUILTIN_EFFECT_KINDS[name],
                permission_requirements={},
                timeout_seconds=self.executor.shell_timeout if name == "bash" else None,
                enabled=True,
                version=1,
                created_at=now,
                updated_at=now,
                adapter=lambda args, _name=name, _executor=self.executor: getattr(
                    _executor, "execute"
                )(_name, args),
            )
            self._entries[name] = entry

    def register_mcp_tool(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        schema: dict[str, Any],
        *,
        effect_kind: str,
        connection_id: str | None = None,
        permission_requirements: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        version: int = 1,
        adapter: Callable[[dict[str, Any]], Any] | None = None,
    ) -> ToolRegistration:
        if effect_kind not in {"read_only", "file_write", "idempotent", "unknown_write", "opaque"}:
            raise ValueError(f"Invalid MCP effect kind: {effect_kind!r}")
        now = time.time()
        qualified = normalize_mcp_name(server_name, tool_name)
        entry = ToolRegistration(
            registration_id=f"mcp_{qualified}",
            tool_name=qualified,
            adapter_kind="mcp",
            connection_id=connection_id,
            server_name=server_name,
            source_tool_name=tool_name,
            description=description,
            schema=dict(schema),
            effect_kind=effect_kind,
            permission_requirements=dict(permission_requirements or {}),
            timeout_seconds=timeout_seconds,
            enabled=True,
            version=version,
            created_at=now,
            updated_at=now,
            adapter=adapter,
        )
        self._entries[qualified] = entry
        return entry

    def discover_mcp(self, connection_id: str) -> list[ToolRegistration]:
        """Discover tools from a stored stdio MCP connection and make them callable."""
        if self.store is None:
            raise RuntimeError("ToolRegistry requires a store to discover MCP connections")
        connection = self.store.get_mcp_connection(connection_id)
        if connection is None:
            raise KeyError(f"MCP connection not found: {connection_id}")
        client = MCPClient(connection, cwd=self.repo_root)
        try:
            tools = asyncio.run(client.list_tools())
        except Exception as exc:
            self.store.update_mcp_connection_status(
                connection_id,
                "error",
                last_error=f"{type(exc).__name__}: {exc}",
            )
            raise

        registered: list[ToolRegistration] = []
        for tool in tools:
            hints = tool.annotations
            if hints.get("read_only_hint"):
                effect_kind = "read_only"
            elif hints.get("idempotent_hint"):
                effect_kind = "idempotent"
            else:
                effect_kind = "unknown_write"
            entry = self.register_mcp_tool(
                client.server_name,
                tool.name,
                tool.description,
                tool.input_schema,
                effect_kind=effect_kind,
                connection_id=connection_id,
                timeout_seconds=30.0,
                adapter=lambda args, _name=tool.name, _client=client: asyncio.run(
                    _client.call_tool(_name, args, timeout=30.0)
                ),
            )
            registered.append(entry)

        self.sync_to_store()
        self.store.update_mcp_connection_status(
            connection_id,
            "connected",
            last_connected_at=time.time(),
        )
        return registered

    def sync_to_store(self) -> None:
        if self.store is None:
            return
        for entry in self._entries.values():
            self.store.upsert_tool_registration(
                registration_id=entry.registration_id,
                tool_name=entry.tool_name,
                adapter_kind=entry.adapter_kind,
                connection_id=entry.connection_id,
                server_name=entry.server_name,
                source_tool_name=entry.source_tool_name,
                description=entry.description,
                schema=entry.schema,
                effect_kind=entry.effect_kind,
                permission_requirements=entry.permission_requirements,
                timeout_seconds=entry.timeout_seconds,
                enabled=entry.enabled,
                version=entry.version,
            )

    def load_from_store(self) -> None:
        if self.store is None:
            return
        for item in self.store.list_tool_registrations(enabled_only=True):
            adapter = None
            if item["adapter_kind"] == "builtin":
                adapter = lambda args, _name=item["tool_name"], _executor=self.executor: getattr(
                    _executor, "execute"
                )(_name, args)
            elif item["adapter_kind"] == "mcp" and item.get("connection_id") and item.get("source_tool_name"):
                connection = self.store.get_mcp_connection(str(item["connection_id"]))
                if connection is not None:
                    client = MCPClient(connection, cwd=self.repo_root)
                    source_tool_name = str(item["source_tool_name"])
                    timeout_seconds = item.get("timeout_seconds") or 30.0
                    adapter = lambda args, _name=source_tool_name, _client=client, _timeout=timeout_seconds: asyncio.run(
                        _client.call_tool(_name, args, timeout=float(_timeout))
                    )
            entry = ToolRegistration(
                registration_id=str(item["registration_id"]),
                tool_name=str(item["tool_name"]),
                adapter_kind=str(item["adapter_kind"]),
                connection_id=item.get("connection_id"),
                server_name=item.get("server_name"),
                source_tool_name=item.get("source_tool_name"),
                description=str(item.get("description") or ""),
                schema=dict(item.get("schema") or {}),
                effect_kind=str(item["effect_kind"]),
                permission_requirements=dict(item.get("permission_requirements") or {}),
                timeout_seconds=item.get("timeout_seconds"),
                enabled=bool(item["enabled"]),
                version=int(item.get("version", 1)),
                created_at=float(item.get("created_at", 0.0)),
                updated_at=float(item.get("updated_at", 0.0)),
                adapter=adapter,
            )
            self._entries[entry.tool_name] = entry

    def get(self, tool_name: str) -> ToolRegistration | None:
        return self._entries.get(tool_name)

    def list_tools(self) -> list[ToolRegistration]:
        return list(self._entries.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": entry.tool_name,
                "description": entry.description,
                "input_schema": entry.schema,
            }
            for entry in self._entries.values()
            if entry.enabled
        ]
