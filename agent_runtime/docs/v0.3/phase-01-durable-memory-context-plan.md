# Phase 1: Durable Memory, Context Projector, Plan Store

## Status

Implemented in `v0.3.0.dev1` and reviewed by an independent review agent (no
P0/P1 findings; all P2 findings fixed and covered by tests). This is the first
semantic-state phase and must not change the durable execution-checkpoint
contract.

## Objective

Add durable long-horizon state for memories, summaries, plans, and plan items
without replacing full-history execution checkpoints. Before each model call,
the runtime may project a compact, task-relevant context; after the model
returns, tool results and effects continue to be persisted against the full
checkpoint.

## Reused teaching modules

| Module | What is reused | What changes |
|---|---|---|
| `s08_context_compact` | compaction pipeline ideas | becomes a read-only context projector |
| `s09_memory` | durable memory concept | moves from Markdown/JSON to SQLite rows |
| `s12_task_system` | subtask DAG and blockedBy semantics | moves from file-backed JSON to SQLite |

## Scope

### In scope

- Add `memories`, `memory_links`, `summaries`, `plans`, and `plan_items` tables.
- Add plan status and dependency transitions.
- Add a context projector that produces model-view messages.
- Link projected model calls to the underlying checkpoint and turn cursor.
- Preserve full messages in `checkpoints.messages_json`.
- Add trace fields for projection token estimates and plan transitions.

### Out of scope

- Training, fine-tuning, or neural memory.
- PostgreSQL or `pgvector`.
- Dynamic `MERGE`, `REMOVE`, or `REORDER` of plan items in this phase.
- Background execution and subagents.
- OS sandboxing.

## Schema direction

```text
memories
  memory_id
  task_id
  kind
  content
  source_checkpoint_id
  evidence_hash
  created_at
  expires_at

memory_links
  source_memory_id
  target_memory_id
  relation

summaries
  summary_id
  task_id
  scope
  content
  source_turn_range
  evidence_hash
  model_call_id
  created_at

plans
  plan_id
  task_id
  status
  dag_hash
  created_at
  updated_at

plan_items
  plan_item_id
  plan_id
  subtask_id
  status
  blocked_by_json
  completion_summary
  evidence_hash
  version
  created_at
  updated_at
```

## Runtime integration

The projector belongs immediately before `model.complete(...)`:

```text
load task + full checkpoint
  -> load current plan
  -> select active/unblocked plan item
  -> build memory/summary projection
  -> model.complete(projected_messages, tools)
  -> persist response against full checkpoint
  -> append tool results against full messages
```

Projection must be deterministic enough for tests and must include:

- global instructions and current task identity;
- active subtask description and necessary dependency summaries;
- relevant memories or summaries selected by the projection policy;
- recent failed evidence or review reasons;
- minimum full-history fallback when projection metadata is missing.

## Checkpoint invariant

`checkpoints.messages_json` remains the authoritative conversation history.
Projection is never written back into that field. A recovery run must be able
to reconstruct the same model input projection or fall back to full history.

## Plan state machine

```text
plan_items.status:
  pending -> in_progress -> verifying -> completed
                         \-> failed -> retryable
```

Only a verifier or an explicit review action may transition an item to
`completed`. Model text alone is not sufficient completion evidence.

## Acceptance criteria

- Fresh databases migrate cleanly from the current schema.
- Full pytest and Phase 1 targeted tests pass.
- A projection-only run does not mutate `checkpoints.messages_json`.
- Tool/effect deduplication remains intact after projection.
- A plan item does not become `completed` without a verifier/evidence event.
- Recovery after model response and after tool result uses full checkpoint
  state and produces no duplicate effect.
- Trace JSONL includes projection and plan metrics.

## Verification

```powershell
python -m pytest agent_runtime/tests -q
python -m agent_runtime db-check --repo <sandbox>
python -m agent_runtime eval --suite mvp.yaml
```

## Handoff

### Complete before Phase 2

- Durable memory/summary/plan schema exists.
- Context projector is wired before model calls.
- Full checkpoint invariant is covered by tests.
- Projection metrics are exported to trace.

### Phase 2 entry state

Phase 2 can assume a task may have long-lived plan/memory state. Background
jobs should be able to reference the active task, plan, and repo root, but must
not depend on subagents or MCP.

### Known deferred boundaries

- Semantic similarity search is deferred.
- Memory eviction policy is minimal and explicit.
- Plan graph mutation is limited to retry/complete transitions in this phase.
