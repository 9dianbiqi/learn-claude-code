"""Local stdio MCP server used by Phase 4 integration tests."""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations


server = MCPServer("mock-github")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_repo_contents(owner: str, repo: str, path: str) -> str:
    """Read a file from a mock GitHub repository."""
    return f"contents of {owner}/{repo}:{path}"


@server.tool()
def create_repo(name: str) -> str:
    """Create a mock GitHub repository."""
    return f"created {name}"


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
