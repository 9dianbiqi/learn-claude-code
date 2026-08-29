# Changelog

## v0.3.0.dev4 - Phase 3 Durable Subagents, Plan Approval & Team Mailbox

- Stabilized and froze the reproducible v0.3 Exec-Full baseline at package
  version `0.3.0.dev4` and schema version `v9`; execution checkpoints and
  full-history resume remain authoritative, while the verified-subtask layer
  is deferred to a later ticket.
- Fixed the public plan-approval resume path so its fenced compare-and-swap
  update binds the run identifier, version, and fencing token correctly;
  genuinely stale writers remain rejected.
- Added durable `subagent_runs`, `mailboxes`, `mailbox_messages`, and
  `plan_approvals` tables via a checksum-verified v9 schema migration. Fresh
  databases build directly to v9 and existing v4-v8 stores upgrade in place.
- Added a durable child-agent execution model: a subagent is a full Runtime
  task with its own checkpoint lineage, tool calls, operations, and effect
  ledger entries, while `subagent_runs` keeps orchestration metadata with
  fencing and CAS-protected state transitions.
- Added `Runtime.spawn_subagent` with Codex-style `prompt`, `context_window`
  (default 32000 tokens), `model` (inherited from the parent task), and
  `tool_scope` parameters; context budget overflow fails closed and spawning
  is capped at a depth of three task layers.
- Added plan approval persistence: `request_plan_approval` writes an immutable
  markdown plan file under `.agent_runtime/plans/`, records `plan_hash`, and
  stops the child in `needs_review`; `approve_subagent_plan` resumes or cancels
  the child with CAS-protected FSM transitions.
- Added an internal `.agent_runtime.spawn_subagent` orchestration tool that
  bypasses `PermissionEngine` and returns `run_id`, `child_task_id`, `status`,
  `result_summary`, and aggregated `token_usage`.
- Added team mailbox durability: idempotent send, atomic read, and
  `delivered -> read -> archived` transitions that survive restart.
- Added `subagent spawn|run|resume|list|approvals|approve|reject|show-plan` and
  `subagent mailbox send|read` CLI commands.
- Bumped the package version.

## v0.3.0.dev3 - Phase 4 Tool Registry & MCP

- Added a durable `ToolRegistry` as the single tool dispatch path; built-in
  tools are registry entries and tool schemas flow into model context.
- Added `tool_registrations` and `mcp_connections` metadata tables via a v8
  schema migration with connection status, last-connected time, and error
  history.
- Added a real stdio MCP client that discovers tools, normalizes
  `mcp__<server>__<tool>` names, infers read-only/idempotent/write hints from
  tool annotations, and persists callable adapters.
- MCP authentication uses an environment-variable reference
  (`auth_token_env`); tokens are never written to the database.
- `PermissionEngine` evaluates registered MCP tools and fails closed for
  unregistered or unknown MCP tools.
- Added `mcp add`, `mcp list`, and `mcp refresh` CLI lifecycle commands with
  GitHub setup documentation.
- Bumped the package version.

## v0.3.0.dev2 - Phase 2 Background Jobs & Cron

- Added durable `agent_jobs`, `job_runs`, and `cron_schedules` tables via a
  checksum-verified v7 schema migration that cleanly upgrades v4/v5/v6 stores.
- Added claim/heartbeat/complete/fail/cancel transitions with CAS and fencing
  tokens, one-active-run enforcement, and restart recovery that fences stale
  owners and returns borrowable jobs to `retryable`.
- Added a periodic cron scheduler that emits durable `pending` jobs at a
  schedule boundary instead of executing payloads inline.
- Extended the invariant scanner to validate job/run/cron consistency and
  exported job lifecycle metrics plus redacted job/run records to the trace.
- Added `jobs list` and `cron list` CLI inspection commands.
- Bumped the package version.

## v0.3.0.dev1 - Phase 1 Durable Memory & Context Projector

- Added durable `memories`, `memory_links`, `summaries`, `plans`, and
  `plan_items` tables with a checksum-verified v6 schema migration.
- Added a read-only `ContextProjector` that builds compact model-view messages
  (plan, memories, summaries, review evidence) before each model call while
  preserving full conversation history in `checkpoints.messages_json`.
- Linked projected model calls to their source checkpoint and exported
  projection/plan metrics to the trace summary and JSONL export.
- Added plan-item lifecycle transitions (pending → in_progress → verifying →
  completed, with failed → retryable) and evidence-gated completion.
- Bumped the package version; DB `db-check` now targets the current schema
  version instead of a hardcoded value.

## v0.2.1 - Release metadata

- Set the durable Agent Runtime package version to `0.2.1`.
- Added version-consistency coverage for package metadata, CLI `--version`,
  doctor runtime metadata, and the top Changelog entry.
- This release is metadata, documentation, and test-only; it does not change
  Runtime execution semantics or expand the supported safety boundaries.

## v0.2.0.dev1 - Phase 1 Effect Ledger

- Added explicit, checksum-verified SQLite SchemaManager migrations.
- Added direct fresh v5 schema creation and audited v4-to-v5 backfill.
- Added SQLite backup, integrity, lease preflight, dry-run, rollback, and
  migration fault-injection boundaries.
- Added EffectSemantics, operations, operation_outbox, CAS/fencing transitions,
  reconciliation evidence, and operation trace metrics.
- Integrated file and Shell effects with prepared/dispatched/committed/unknown
  recovery semantics.
- Follow-up hardening moved the dispatched boundary to immediately before tool
  execution, preserving prepared operations for pre-dispatch failures.
- Added db-migrate, schema-aware db-check, operation inspection, and Phase 1
  migration/ledger regression tests, including subprocess and transaction
  rollback fault matrices plus trace/event redaction coverage.
- Phase 1 does not provide OS sandboxing or generic exactly-once Shell
  execution.

## v0.1.1 — operator safety and usability

- Added hard default protection for `.env*`, `.git/**`, private-key formats,
  `.netrc`, and credential/secret file families across file, glob, and Shell
  access paths.
- Added `list`, `show`, `pending`, `events`, `doctor`, and `db-check` commands.
- Scoped CLI arguments to their actual subcommands and made task/tool IDs and
  reconciliation actions explicit required arguments.
- Added SQLite integrity checks and pending-call queries to `EventStore`.
- Added a deterministic three-scenario demo with SQLite and JSONL evidence.
- Added architecture/demo documentation and a Windows/Linux acceptance CI
  workflow with Runtime tests, MVP Eval, Hardening Eval, and demo smoke.

## v0.1.0 — frozen durable Runtime baseline

- Added SQLite WAL checkpoints, append-only events, durable model/tool state,
  file observations, leases, fencing, and effect reservations.
- Added allow/ask/deny permissions, repository containment, read-before-edit,
  SHA-256 conflict detection, atomic replacement, and explicit reconciliation.
- Added trace metrics, deterministic fault injection, MVP Eval, Hardening Eval,
  and controlled real-model read/write/Shell acceptance evidence.
