# Phase 6: Retention, GC, Observability, Sandbox

## Status

Planned for v0.3. This is the operational hardening and release-gate phase.
OS sandboxing is explicitly independent from memory, background, subagent, MCP,
and worktree work.

## Objective

Add retention and garbage collection for durable runtime records, expose
runtime metrics and long-task liveness, and begin an independently gated OS
sandbox path.

## Reused modules

- Existing `SchemaManager` and migration tests.
- Existing `TraceReporter`.
- Existing `PermissionEngine` and `ToolExecutor`.
- `s18_worktree_isolation` concepts only for sandbox path validation.

## Scope

### In scope

- Archive old `checkpoints`, `events`, `model_calls`, `tool_calls`, and
  operation audit records.
- Implement retention policies and GC jobs.
- Add long-task heartbeat and runtime metrics.
- Add schema migration for archival/retention metadata.
- Define OS sandbox adapter boundaries.

### Out of scope

- Implementing a complete cross-platform sandbox in the same phase as memory.
- Distributed telemetry.
- Auto-healing of corrupted databases.
- Universal side-effect sandbox guarantees.

## Retention policy

Retention is explicit and operator-controlled:

```text
retention_policy
  record_kind
  terminal_task_age
  active_task_age
  archive_mode
  delete_mode
```

GC must:

- never delete unresolved effects or active leases;
- never delete a checkpoint required to resume a non-terminal task;
- archive before delete where required;
- write an audit event with before/after counts and digests.

## Observability

Add minimal durable/runtime metrics:

```text
active_tasks
active_leases
pending_operations
blocked_operations
pending_jobs
stale_owners
checkpoint_count
event_count
projection_input_tokens
last_heartbeat_at
```

Long-task heartbeat must use the existing lease/fencing path where applicable.

## Sandbox boundary

Sandbox is a separate adapter behind an interface:

```text
prepare(repo_root, lane_id)
run(tool_call)
inspect(result)
teardown()
```

The first milestone is not a full sandbox; it is a fail-closed hook point and
validation suite. Docker, Bubblewrap, Windows restricted tokens, or equivalent
mechanisms are independent follow-up work.

## Acceptance criteria

- Archive and GC do not delete unresolved effects or active task state.
- GC audit events are durable and redacted.
- Long-task heartbeats and metrics are exported.
- Schema migrations pass rollback/fault tests.
- Sandbox hook fails closed when no sandbox adapter is configured.
- Full v0.3 regression suite passes.

## Verification

```powershell
python -m pytest agent_runtime/tests -q
python -m agent_runtime db-check --repo <sandbox>
python -m agent_runtime doctor --repo <sandbox>
python -m agent_runtime eval --suite mvp.yaml
python -m agent_runtime eval --suite hardening.yaml
```

## Handoff

### Release gate

Phase 6 is complete when all six phase docs have passing targeted suites, full
Runtime tests are green, migration/rollback evidence is collected, and the OS
sandbox adapter remains explicitly disabled unless a configured provider is
present.

### Known deferred boundaries

- Full cross-platform sandbox implementation is a follow-up.
- Distributed observability is deferred.
- Automatic retention tuning is deferred.
- Physical/storage corruption recovery is outside the SQLite transaction
  guarantee.
