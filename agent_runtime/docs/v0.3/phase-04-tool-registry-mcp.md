# Phase 4: Tool Registry + Real MCP

## Status

Planned for v0.3. This phase replaces the current static tool dispatch with a
registry and adds real MCP transport.

## Objective

Introduce a durable/typed tool registry and connect MCP servers through an
adapter that participates in discovery, timeout, authentication, and
permission evaluation.

## Reused teaching modules

| Module | What is reused | What changes |
|---|---|---|
| `s19_mcp_plugin` | dynamic tool assembly and MCP naming | mock transport becomes real protocol |
| current `tools.py` | built-in tool adapters | static map becomes registry entries |

## Scope

### In scope

- Add a `ToolRegistry` abstraction.
- Migrate existing built-in tools to registry adapters.
- Add `tool_registrations` and `mcp_connections` durable metadata.
- Implement MCP discovery and tool listing.
- Implement timeout, authentication, and connection lifecycle.
- Feed MCP tool schemas into model tool schemas.
- Feed MCP permission declarations into `PermissionEngine`.
- Record MCP tool calls through existing tool-call and operation paths.

### Out of scope

- Arbitrary plugin ABI or marketplace.
- Automatic trust decisions for unknown MCP servers.
- Multi-tenant MCP authorization servers.
- Rewriting existing tool safety semantics.

## Registry contract

Every registered tool must provide:

```text
name
description
schema
effect_kind
permission_requirements
timeout
adapter
```

The runtime resolves tool names through the registry and never through an
ad-hoc string map.

## Schema direction

```text
tool_registrations
  registration_id
  tool_name
  adapter_kind
  schema_json
  effect_kind
  permission_json
  enabled
  version
  created_at
  updated_at

mcp_connections
  connection_id
  server_name
  transport
  endpoint
  auth_profile
  status
  last_connected_at
  last_error
  created_at
  updated_at
```

Secrets must not be stored in `mcp_connections`; use an explicit secret
reference or environment-backed profile.

## MCP integration

```text
connect/discover -> list tools -> normalize schema
  -> register permission declarations
  -> expose mcp__<server>__<tool>
```

Tool invocation:

```text
model tool_use
  -> resolve through ToolRegistry
  -> PermissionEngine check
  -> prepare operation
  -> MCP call with timeout
  -> commit or reconcile effect
```

## Permission requirement

An MCP tool is not available until its permission declaration is registered.
Unknown or malformed permission declarations fail closed.

## Acceptance criteria

- All built-in tools still execute through the registry.
- MCP discovery populates registry entries without raw string dispatch.
- MCP timeout does not leak a running external effect without reconciliation.
- Authentication failure is recorded and does not expose a tool.
- Permission denial prevents invocation and is audited.
- Existing tool, permission, and effect-ledger tests remain green.

## Verification

```powershell
python -m pytest agent_runtime/tests -q
python -m agent_runtime db-check --repo <sandbox>
python -m agent_runtime eval --suite hardening.yaml
```

## Handoff

### Complete before Phase 5

- Tool registry is the single dispatch path.
- MCP transport and permission integration exist.
- Built-in and MCP tools share effect/audit semantics.

### Phase 5 entry state

Phase 5 can bind registered tools to worktree/lane-aware repos without changing
the registry contract.

### Known deferred boundaries

- Plugin marketplace is deferred.
- Automatic MCP trust negotiation is deferred.
- Cross-process MCP servers using shared mutable files are not yet sandboxed.

