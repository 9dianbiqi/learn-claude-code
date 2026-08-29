# Agent Runtime v0.3 Execution Docs

This directory contains one execution document per v0.3 phase. Each document is
self-contained and ends with the handoff content required to start the next
phase without re-reading the full design discussion.

## Phase sequence

| Phase | Document | Primary handoff target |
|---|---|---|
| 1 | [Durable Memory / Context Projector / Plan Store](phase-01-durable-memory-context-plan.md) | Phase 2 |
| 2 | [Background Job + Cron](phase-02-background-job-cron.md) | Phase 3 |
| 3 | [Durable Subagent / Team Mailbox](phase-03-durable-subagent-team-mailbox.md) | Phase 4 |
| 4 | [Tool Registry + Real MCP](phase-04-tool-registry-mcp.md) | Phase 5 |
| 5 | [Lane-aware Lease + Worktree](phase-05-lane-aware-lease-worktree.md) | Phase 6 |
| 6 | [Retention / GC / Observability / Sandbox](phase-06-retention-gc-observability-sandbox.md) | Release gate |

## Shared invariants

- The core `Runtime` loop remains: model response -> tool call -> result
  feedback -> next turn.
- `checkpoints` always store the complete durable history. Model projection is
  a read-only view and never replaces checkpoint state.
- New state uses the existing SQLite `SchemaManager` migration path.
- Tool effects continue through `operations`, `operation_outbox`, and
  `effect_reservations`.
- Permissions remain enforced by `PermissionEngine`.
- Background jobs, subagents, MCP tools, and worktrees must not bypass the
  effect ledger or lease/fencing rules.

## Frozen v0.3 Exec-Full baseline

Ticket #6 freezes the reproducible execution baseline at package version
`0.3.0.dev4` and schema version `v9`. It includes the durable Phase 1 through
Phase 4 runtime behavior: full-history execution checkpoints and recovery,
effect reconciliation, migrations, background jobs, durable subagents and
mailboxes, plan approval, the ToolRegistry, and stdio MCP.

The baseline remains execution-only: `checkpoints.messages_json` is the
authoritative history, and no first-class verified-subtask ledger or semantic
checkpointing mechanism is included. Those dissertation-specific mechanisms
are deferred to later work. The Ticket #6 review compares changes against the
immutable pre-freeze commit `a16f90e`; the resulting implementation commit is
the handoff point for subsequent phases.

## Phase delivery checklist

Every phase must produce:

1. Durable schema changes with migration and rollback evidence.
2. Runtime integration with explicit fault hooks.
3. Invariant and recovery tests.
4. Trace or audit fields where new durable state is exposed.
5. A handoff section confirming what is complete and what remains deferred.

