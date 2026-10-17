from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import yaml

from agent_runtime.cli import main
from agent_runtime.mcp_client import MCPClient
from agent_runtime.permissions import PermissionEngine
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import Runtime
from agent_runtime.store import EventStore
from agent_runtime.tool_registry import ToolRegistry, normalize_mcp_name


def test_builtin_tools_are_registered_and_schemas_are_exposed(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    registry.install_builtins()

    assert [tool["name"] for tool in registry.schemas()] == [
        "read_file",
        "glob",
        "write_file",
        "edit_file",
        "bash",
    ]
    entry = registry.get("read_file")
    assert entry is not None
    assert entry.adapter_kind == "builtin"
    assert entry.effect_kind == "read_only"
    assert entry.description
    assert entry.schema["type"] == "object"
    assert entry.adapter is not None
    assert registry.get("mcp__nope__not_a_tool") is None


def test_mcp_tool_registration_uses_normalized_qualified_names(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    registry.register_mcp_tool(
        "GitHub MCP",
        "Get-Repo-Contents!",
        "Read files from a GitHub repository.",
        {"type": "object", "properties": {"owner": {"type": "string"}}},
        effect_kind="read_only",
    )

    tool_name = "mcp__github_mcp__get_repo_contents"
    assert tool_name in {tool["name"] for tool in registry.schemas()}
    entry = registry.get(tool_name)
    assert entry is not None
    assert entry.adapter_kind == "mcp"
    assert entry.effect_kind == "read_only"
    assert entry.description == "Read files from a GitHub repository."
    assert entry.schema["properties"]["owner"]["type"] == "string"
    assert normalize_mcp_name("GitHub MCP", "Get-Repo-Contents!") == tool_name


def test_tool_registrations_and_mcp_connections_persist_in_store(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    store.upsert_tool_registration(
        registration_id="reg-github",
        tool_name="mcp__github__get_repo_contents",
        adapter_kind="mcp",
        connection_id="conn-github",
        server_name="github",
        source_tool_name="get_repo_contents",
        description="Read a GitHub repository file.",
        schema={"type": "object", "properties": {"owner": {"type": "string"}}},
        effect_kind="read_only",
        permission_requirements={"effect_kind": "read_only"},
        timeout_seconds=30.0,
    )
    stored = store.list_tool_registrations()
    assert len(stored) == 1
    assert stored[0]["tool_name"] == "mcp__github__get_repo_contents"
    assert stored[0]["schema"]["properties"]["owner"]["type"] == "string"
    assert stored[0]["effect_kind"] == "read_only"
    assert stored[0]["timeout_seconds"] == 30.0
    assert stored[0]["connection_id"] == "conn-github"
    assert stored[0]["server_name"] == "github"
    assert stored[0]["source_tool_name"] == "get_repo_contents"

    store.upsert_mcp_connection(
        connection_id="conn-github",
        server_name="github",
        transport="stdio",
        endpoint="npx -y @modelcontextprotocol/server-github",
        args=[],
        auth_profile={"auth_token_env": "GITHUB_PERSONAL_ACCESS_TOKEN"},
    )
    connections = store.list_mcp_connections()
    assert len(connections) == 1
    assert connections[0]["server_name"] == "github"
    assert connections[0]["auth_profile"]["auth_token_env"] == "GITHUB_PERSONAL_ACCESS_TOKEN"

    store.update_mcp_connection_status("conn-github", "connected", last_connected_at=123.0)
    assert store.get_mcp_connection("conn-github")["status"] == "connected"
    assert store.get_mcp_connection("conn-github")["last_connected_at"] == 123.0


def test_registry_reloads_persisted_tools_from_store(tmp_path: Path):
    db = tmp_path / "runtime.db"
    first = ToolRegistry(tmp_path, store=EventStore(db))
    first.install_builtins()
    first.register_mcp_tool(
        "github",
        "get_repo_contents",
        "Read a GitHub repository file.",
        {"type": "object", "properties": {"owner": {"type": "string"}}},
        effect_kind="read_only",
    )
    first.sync_to_store()

    reloaded = ToolRegistry(tmp_path, store=EventStore(db))
    reloaded.load_from_store()
    assert reloaded.get("read_file") is not None
    assert reloaded.get("read_file").adapter is not None  # type: ignore[union-attr]
    mcp_entry = reloaded.get("mcp__github__get_repo_contents")
    assert mcp_entry is not None
    assert mcp_entry.adapter_kind == "mcp"
    assert mcp_entry.effect_kind == "read_only"
    assert mcp_entry.schema["properties"]["owner"]["type"] == "string"


def test_permission_engine_evaluates_registered_mcp_tools(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    registry.register_mcp_tool(
        "github",
        "get_repo_contents",
        "Read a GitHub repository file.",
        {"type": "object", "properties": {"owner": {"type": "string"}}},
        effect_kind="read_only",
    )
    registry.register_mcp_tool(
        "github",
        "create_repo",
        "Create a GitHub repository.",
        {"type": "object", "properties": {"name": {"type": "string"}}},
        effect_kind="unknown_write",
    )
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {
                        "id": "github-read",
                        "effect": "allow",
                        "tools": ["mcp__github__get_repo_contents"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    engine = PermissionEngine(tmp_path, policy_path=policy, tool_registry=registry)
    allowed = engine.evaluate("mcp__github__get_repo_contents", {"owner": "openai"})
    assert allowed.effect == "allow"
    assert engine.classify_effect("mcp__github__get_repo_contents", {}) == "read_only"

    denied = engine.evaluate("mcp__github__create_repo", {"name": "x"})
    assert denied.effect == "deny"
    assert denied.rule_id == "default.unknown-tool"
    assert engine.classify_effect("mcp__github__create_repo", {}) == "unknown_write"

    unregistered = engine.evaluate("mcp__unknown__nope", {})
    assert unregistered.effect == "deny"
    assert unregistered.rule_id == "default.unknown-tool"


def test_mcp_client_discovers_and_calls_stdio_server(tmp_path: Path):
    fixture = Path(__file__).resolve().parent / "fixtures" / "mock_mcp_server.py"
    client = MCPClient(
        connection={
            "connection_id": "conn-mock",
            "server_name": "mock-github",
            "transport": "stdio",
            "endpoint": sys.executable,
            "args": [str(fixture)],
            "auth_profile": {},
        },
        cwd=tmp_path,
    )

    tools = asyncio.run(client.list_tools())
    assert {tool.name for tool in tools} == {"get_repo_contents", "create_repo"}
    contents_tool = next(tool for tool in tools if tool.name == "get_repo_contents")
    assert contents_tool.input_schema["required"] == ["owner", "repo", "path"]

    output = asyncio.run(
        client.call_tool(
            "get_repo_contents",
            {"owner": "openai", "repo": "codex", "path": "README.md"},
            timeout=10.0,
        )
    )
    assert "contents of openai/codex:README.md" in output


def test_discovery_registers_mcp_tools_and_updates_connection_status(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")
    fixture = Path(__file__).resolve().parent / "fixtures" / "mock_mcp_server.py"
    store.upsert_mcp_connection(
        connection_id="conn-mock",
        server_name="mock-github",
        transport="stdio",
        endpoint=sys.executable,
        args=[str(fixture)],
        auth_profile={},
    )
    registry = ToolRegistry(tmp_path, store=store)

    registry.discover_mcp("conn-mock")

    read_entry = registry.get("mcp__mock_github__get_repo_contents")
    assert read_entry is not None
    assert read_entry.effect_kind == "read_only"
    assert registry.get("mcp__mock_github__create_repo").effect_kind == "unknown_write"  # type: ignore[union-attr]
    connection = store.get_mcp_connection("conn-mock")
    assert connection["status"] == "connected"
    assert connection["last_connected_at"] is not None
    persisted = store.get_tool_registration("mcp__mock_github__get_repo_contents")
    assert persisted is not None
    assert persisted["schema"]["required"] == ["owner", "repo", "path"]
    assert persisted["connection_id"] == "conn-mock"
    assert persisted["server_name"] == "mock-github"
    assert persisted["source_tool_name"] == "get_repo_contents"


def test_runtime_executes_discovered_mcp_tools(tmp_path: Path):
    db = tmp_path / "runtime.db"
    store = EventStore(db)
    fixture = Path(__file__).resolve().parent / "fixtures" / "mock_mcp_server.py"
    store.upsert_mcp_connection(
        connection_id="conn-mock",
        server_name="mock-github",
        transport="stdio",
        endpoint=sys.executable,
        args=[str(fixture)],
        auth_profile={},
    )
    registry = ToolRegistry(tmp_path, store=store)
    registry.discover_mcp("conn-mock")

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {
                        "id": "mcp-read",
                        "effect": "allow",
                        "tools": ["mcp__mock_github__get_repo_contents"],
                    },
                    {
                        "id": "mcp-write",
                        "effect": "allow",
                        "tools": ["mcp__mock_github__create_repo"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "read-github",
                        "mcp__mock_github__get_repo_contents",
                        {"owner": "openai", "repo": "codex", "path": "README.md"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall("create-github", "mcp__mock_github__create_repo", {"name": "demo"})
                ]
            ),
            ModelResponse(text="done"),
        ]
    )
    runtime = Runtime(
        tmp_path,
        model,
        store=EventStore(db),
        approval_callback=lambda *_: True,
        policy_path=policy,
    )

    result = runtime.run("use GitHub")

    assert result.status == "completed"
    assert result.final_text == "done"
    task_id = runtime.store.list_tasks()[0]["task_id"]
    read_call = runtime.store.get_tool_call(task_id, "read-github")
    assert read_call["status"] == "succeeded"
    assert "contents of openai/codex:README.md" in read_call["output"]
    write_call = runtime.store.get_tool_call(task_id, "create-github")
    assert write_call["status"] == "succeeded"
    assert "created demo" in write_call["output"]
    operation = runtime.store.get_operation_for_tool_call(task_id, "create-github")
    assert operation is not None
    assert operation["state"] == "committed"


def test_cli_mcp_add_and_list_round_trip(tmp_path: Path, capsys):
    fixture = Path(__file__).resolve().parent / "fixtures" / "mock_mcp_server.py"

    assert main(
        [
            "mcp", "add",
            "--repo", str(tmp_path),
            "--server", "mock-github",
            "--endpoint", sys.executable,
            "--arg", str(fixture),
            "--auth-token-env", "GITHUB_PERSONAL_ACCESS_TOKEN",
        ]
    ) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["connection_id"] == "conn-mock-github"
    assert added["server_name"] == "mock-github"
    assert added["endpoint"] == sys.executable
    assert added["args"] == [str(fixture)]
    assert added["auth_profile"]["auth_token_env"] == "GITHUB_PERSONAL_ACCESS_TOKEN"
    assert added["status"] == "configured"

    assert main(["mcp", "list", "--repo", str(tmp_path)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["connection_id"] == "conn-mock-github"
    assert listed[0]["tools"] == []


def test_cli_mcp_refresh_discovers_and_persists_tools(tmp_path: Path, capsys):
    fixture = Path(__file__).resolve().parent / "fixtures" / "mock_mcp_server.py"
    assert main(
        [
            "mcp", "add",
            "--repo", str(tmp_path),
            "--server", "mock-github",
            "--endpoint", sys.executable,
            "--arg", str(fixture),
        ]
    ) == 0
    capsys.readouterr()

    assert main(["mcp", "refresh", "--repo", str(tmp_path)]) == 0
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 1
    connection = results[0]
    assert connection["status"] == "connected"
    assert connection["server_name"] == "mock-github"
    tools = {tool["tool_name"]: tool for tool in connection["tools"]}
    assert tools["mcp__mock_github__get_repo_contents"]["effect_kind"] == "read_only"
    assert tools["mcp__mock_github__create_repo"]["effect_kind"] == "unknown_write"

    assert main(["mcp", "list", "--repo", str(tmp_path)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert {tool["tool_name"] for tool in listed[0]["tools"]} == {
        "mcp__mock_github__get_repo_contents",
        "mcp__mock_github__create_repo",
    }


def test_cli_mcp_refresh_reports_failed_connection(tmp_path: Path, capsys):
    assert main(
        [
            "mcp", "add",
            "--repo", str(tmp_path),
            "--server", "broken-server",
            "--endpoint", "agent-runtime-does-not-exist",
        ]
    ) == 0
    capsys.readouterr()

    assert main(["mcp", "refresh", "--repo", str(tmp_path)]) == 2
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert results[0]["error"]
