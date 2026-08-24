from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass(frozen=True)
class MCPToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]


class MCPCallError(RuntimeError):
    """Raised when an MCP server reports an error or times out."""


class MCPClient:
    def __init__(self, connection: dict[str, Any], cwd: str | Path | None = None):
        self.connection_id = str(connection["connection_id"])
        self.server_name = str(connection["server_name"])
        self.transport = str(connection.get("transport", "stdio"))
        if self.transport != "stdio":
            raise ValueError(f"Unsupported MCP transport: {self.transport!r}")
        self.endpoint = str(connection["endpoint"])
        self.args = list(connection.get("args") or [])
        self.auth_profile = dict(connection.get("auth_profile") or {})
        self.cwd = str(Path(cwd).resolve()) if cwd is not None else None

    def _parameters(self) -> StdioServerParameters:
        env = os.environ.copy()
        token_env = self.auth_profile.get("auth_token_env")
        if token_env and str(token_env).strip():
            env[str(token_env)] = os.environ.get(str(token_env), "")
        return StdioServerParameters(
            command=self.endpoint,
            args=self.args,
            env=env,
            cwd=self.cwd,
        )

    async def list_tools(self) -> list[MCPToolInfo]:
        params = self._parameters()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    MCPToolInfo(
                        name=str(tool.name),
                        description=str(tool.description or ""),
                        input_schema=dict(tool.input_schema),
                        annotations=_annotation_hints(tool.annotations),
                    )
                    for tool in result.tools
                ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 30.0,
    ) -> str:
        params = self._parameters()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                try:
                    result = await session.call_tool(
                        name,
                        arguments,
                        read_timeout_seconds=timeout,
                    )
                except Exception as exc:
                    raise MCPCallError(
                        f"MCP tool {name} failed: {type(exc).__name__}: {exc}"
                    ) from exc
                if getattr(result, "is_error", False):
                    raise MCPCallError(f"MCP tool {name} returned an error: {_mcp_output(result)}")
                return _mcp_output(result)


def _mcp_output(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(str(getattr(block, "text", "")))
        else:
            parts.append(str(block.model_dump()))
    structured = getattr(result, "structured_content", None)
    if not parts and structured is not None:
        parts.append(str(structured))
    return "\n".join(parts)


def _annotation_hints(annotations: Any) -> dict[str, Any]:
    if annotations is None:
        return {}
    hints = {}
    for field in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
        if getattr(annotations, field, None) is not None:
            hints[field] = bool(getattr(annotations, field))
    return hints
