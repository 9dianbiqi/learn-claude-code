# Phase 3: Durable Subagent / Team Mailbox

## Status

Implemented. Decisions are locked with the project owner:

- Execution model: **Option A** - a child agent is a full Runtime task with its
  own `task_id`, checkpoint lineage, tool calls, and effect ledger entries.
  `subagent_runs` is orchestration metadata only, never a second source of
  truth for child progress.
- Plan approval: **full FSM** (`requested -> approved/rejected/superseded`)
  with CAS-protected transitions and durable `plan_hash` / `decided_by` /
  `reason`.
- Real GitHub MCP smoke is deferred until after Gate 2. This phase verifies
  the same registry/lease/effect paths with the local mock server.
- Phase 5 schema (`lanes` / `worktrees` / default-lane backfill) may be drafted
  in parallel, but the Phase 5 runtime merge waits until Phase 3 lands.
- Parent-invoked spawn is synchronous by default (`wait=True`); `wait=False`
  persists a pending run for explicit `run`/`resume` and does not add
  background or concurrent orchestration.
- Operator-initiated CLI spawns use `parent_task_id = NULL`; the schema allows
  a nullable parent so CLI spawns do not need a synthetic root task.
- Plan approval requires a human-readable markdown file under
  `.agent_runtime/plans/{approval_id}.md`; the database stores only the hash
  and audit fields, and `subagent show-plan <approval_id>` prints the file.
- Child context is fail-closed against `context_window` (default 32000
  estimated tokens) and spawn depth is capped at three task layers.

Target version: `0.3.0.dev4`. Schema target: `v9`.

## Objective

Make child agents and team communication durable instead of process-local. A
parent must be able to restart and resume a pending child run, mailbox mail
must survive restart, and a stale child owner must not mutate another owner's
state.

## Reused teaching modules

| Module | What is reused | What changes |
|---|---|---|
| `s06_subagent` | isolated child `messages[]` and summary-return pattern | child state becomes durable task state |
| `s15_agent_teams` | teammate mailbox semantics | mailbox becomes SQLite rows with CAS |
| `s16_team_protocols` | plan approval FSM | FSM becomes durable, fenced, and CAS-protected |

## Locked execution model

1. Spawning a subagent creates a normal task through
   `store.bootstrap_task(child_task_id, ...)` and a `subagent_runs` row that
   links `parent_task_id -> child_task_id`.
2. Parent and child never share the same top-level `task_id`.
3. All child progress lives in the child task's checkpoints, model calls, tool
   calls, and operations. `subagent_runs.messages_json` stores the
   orchestration-facing message set (prompt + returned summary), and
   `result_summary` stores the final parent-facing result.
4. `Runtime.run_subagent(run_id)` claims the run with fencing, then executes
   through the normal `resume(child_task_id)` path so an interrupted child is
   recovered from its latest checkpoint.
5. Phase 3 executes child runs serially against the existing repo lease: the
   caller must not hold the parent lease while `run_subagent` runs. The parent
   loop releases its lease around a synchronous spawn and re-acquires it after
   the child returns. Phase 5 replaces this with lane-aware leases so parent
   and child can run concurrently in different lanes.

## Schema v9

```sql
CREATE TABLE IF NOT EXISTS subagent_runs (
    subagent_run_id TEXT PRIMARY KEY,
    parent_task_id TEXT,
    child_task_id TEXT NOT NULL UNIQUE,
    repo_root TEXT NOT NULL,
    lane_id TEXT NOT NULL DEFAULT 'default',
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    fencing_token TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    messages_json TEXT NOT NULL,
    result_summary TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'needs_review', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_parent ON subagent_runs(parent_task_id, status);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_child ON subagent_runs(child_task_id);

CREATE TABLE IF NOT EXISTS mailboxes (
    mailbox_id TEXT PRIMARY KEY,
    owner_task_id TEXT NOT NULL UNIQUE,
    owner_role TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mailbox_messages (
    message_id TEXT PRIMARY KEY,
    mailbox_id TEXT NOT NULL,
    sender_task_id TEXT NOT NULL,
    recipient_task_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'delivered',
    created_at REAL NOT NULL,
    read_at REAL,
    FOREIGN KEY (mailbox_id) REFERENCES mailboxes(mailbox_id)
);
CREATE INDEX IF NOT EXISTS idx_mailbox_messages_pending
    ON mailbox_messages(mailbox_id, status, created_at);

CREATE TABLE IF NOT EXISTS plan_approvals (
    approval_id TEXT PRIMARY KEY,
    subagent_run_id TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    decided_by TEXT,
    reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (status IN ('requested', 'approved', 'rejected', 'superseded')),
    FOREIGN KEY (subagent_run_id) REFERENCES subagent_runs(subagent_run_id)
);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_run
    ON plan_approvals(subagent_run_id, status);
```

`lane_id` defaults to `'default'` so Phase 5 can migrate existing rows without
changing Phase 3 semantics. No existing table is altered by v9.

## State machines

`subagent_runs.status`:

```text
pending -> running -> completed
                 \-> needs_review -> running | cancelled
pending/running -> failed
pending -> cancelled
```

`mailbox_messages.status`:

```text
delivered -> read -> archived
```

`plan_approvals.status`:

```text
requested -> approved
          -> rejected
          -> superseded
```

## Store API additions

All transitions that change ownership or state must use CAS on `version` and,
where applicable, the `fencing_token`. `StaleState` is raised when the expected
version does not match.

### Subagent runs

- `create_subagent_run(run_id, parent_task_id, child_task_id, repo_root, role, owner_id, messages)`.
- `get_subagent_run(run_id)`.
- `list_subagent_runs(parent_task_id=None, status=None)`.
- `claim_subagent_run(run_id, owner_id, fencing_token)` - requires
  `status IN ('pending', 'running')` and an unmatched or matching token; bumps
  `version`, sets `status='running'`.
- `update_subagent_run(run_id, status=None, messages=None, result_summary=None,
  error=None, expected_version=None, fencing_token=None)`.
- `complete_subagent_run(run_id, result_summary, messages, expected_version, fencing_token)`.
- `fail_subagent_run(run_id, error, expected_version, fencing_token)`.
- `cancel_subagent_run(run_id, reason, expected_version, fencing_token)`.
- `mark_subagent_needs_review(run_id, reason, expected_version, fencing_token)`.

### Mailboxes

- `ensure_mailbox(owner_task_id, owner_role)` - idempotent upsert, returns the
  mailbox with its `version`.
- `get_mailbox(owner_task_id)`.
- `send_mailbox_message(message_id, mailbox_id, sender_task_id, recipient_task_id, payload)`.
- `read_mailbox_messages(mailbox_id, recipient_task_id, limit=50)` - a single
  transaction marks `delivered -> read` for the recipient and returns the
  messages; a second read returns nothing new.
- `archive_mailbox_message(message_id, expected_version)` - only from `read`.
- `list_mailbox_messages(mailbox_id, status=None)`.

### Plan approvals

- `create_plan_approval(approval_id, subagent_run_id, plan_hash, requested_by)`.
- `get_plan_approval(approval_id)`.
- `list_plan_approvals(subagent_run_id=None, status=None)`.
- `transition_plan_approval(approval_id, new_status, decided_by=None, reason=None,
  expected_version=None)` - CAS; approving or rejecting a plan supersedes all
  other `requested` approvals for the same subagent run.

## Runtime integration

### `Runtime.spawn_subagent(prompt, role, wait=True, tool_scope=None,
context_window=32000, model=None) -> dict`

1. Create `child_task_id` and bootstrap the child task with the prompt.
2. Create `subagent_run_id` and the `subagent_runs` row with `status='pending'`.
3. When `wait=True`: save the parent checkpoint, release the parent lease, run
   the child with `run_subagent`, then re-acquire the parent lease. The parent
   loop treats spawn as an internal orchestration tool and never routes it
   through the effect ledger.
4. `model` inherits the parent task's model by default; `tool_scope` inherits
   the parent's visible tool scope when not supplied.
5. The child's estimated context is checked against `context_window` before it
   starts and again before any resume; an overflow raises and never starts or
   resumes the child.
6. Return `{run_id, child_task_id, status, result_summary, error,
   token_usage}`.

### `Runtime.run_subagent(run_id) -> RunResult`

1. `claim_subagent_run(run_id, self.owner_id, new_token)`.
2. Resume `child_task_id` through the normal `resume` path (child acquires its
   own repo lease).
3. On completion, persist `result_summary` and orchestration messages via
   `complete_subagent_run`; on failure, persist the error via
   `fail_subagent_run`. If the child stops for plan review, persist
   `needs_review` and return that status.

### `Runtime.resume_subagent(run_id) -> RunResult`

Used after a parent or operator restart. It validates the stored run, then
delegates to `run_subagent` for `pending`/`running`/`needs_review` runs and
returns the persisted summary for terminal runs.

### `Runtime.approve_subagent_plan(approval_id, approve, reason) -> RunResult`

CAS-transition the approval, then resume the associated child run when
approved, or cancel it when rejected.

## Internal tools

Two internal tools are registered under the `.agent_runtime` namespace and are
handled by the runtime, not by `ToolExecutor`:

- `.agent_runtime.spawn_subagent`: creates a child run and, by default, waits
  for completion before returning the summary to the model.
- `.agent_runtime.request_plan_approval`: persists a `plan_approvals` row
  (`requested`), marks the subagent run `needs_review`, and stops the child
  loop. The child resumes with the approval decision appended to its messages.

Both tools are runtime-special-cased and intentionally bypass
`PermissionEngine`: they orchestrate durable state and never touch the
filesystem through a tool adapter. They still go through the normal durable
`tool_calls` row, argument hashing, and deduplication paths so a restarted
parent cannot double-spawn or double-request approval.

## Permission and effect boundary

- Child agents inherit the parent policy path. A new `Runtime(tool_scope=...)`
  parameter restricts which registered tools a child can see; tools outside the
  scope fail closed because they are absent from the child registry.
- Internal `.agent_runtime` tools are visible regardless of `tool_scope`; they
  are orchestration hooks, not repository tools, and bypass `PermissionEngine`.
- Child permission decisions are recorded against the child `task_id` through
  the normal `tool_calls.permission_*` columns.
- Child non-read-only calls create `operations` and `effect_reservations`
  entries with `task_id = child_task_id` in the same effect ledger.
- A child cannot bind or release the parent lease. In Phase 3 it acquires the
  repo lease under its own `task_id`; Phase 5 routes it through a lane lease.
- Mailbox payloads are data only. No tool execution is triggered by reading a
  message, so a denied permission cannot be bypassed through the mailbox.

## Mailbox protocol

- Delivery inserts a `delivered` message. A message is never deleted without an
  explicit `delivered -> read -> archived` transition.
- Read uses a single transaction and marks messages `read` atomically with the
  mailbox `version` guard. Restart preserves unread mail.
- `message_id` must be unique; resending the same id is an idempotent no-op if
  the existing row matches the payload hash.

## Plan approval FSM

- `request_plan_approval` stores `plan_hash` and `requested_by`, and marks the
  run `needs_review`. It also writes an immutable markdown file at
  `.agent_runtime/plans/{approval_id}.md` containing frontmatter and the plan
  body so an approver can read the exact plan text.
- `approve` transitions to `approved` with `decided_by` and `reason`, then the
  run returns to `running` and the child resumes.
- `reject` transitions to `rejected` with the same audit fields, then the run
  is cancelled.
- `superseded` is applied to older `requested` approvals when a newer decision
  lands; it is never applied manually.

## CLI surface

```text
agent-runtime subagent spawn --repo <sandbox> --prompt <text> --role <role>
    [--tool-scope <name> ...] [--context-window <tokens>] [--model <name>]
agent-runtime subagent run <run_id> --repo <sandbox>
agent-runtime subagent resume <run_id> --repo <sandbox>
agent-runtime subagent list --repo <sandbox> [--status <status>]
agent-runtime subagent approvals <run_id> --repo <sandbox>
agent-runtime subagent approve <approval_id> --repo <sandbox> [--reason <text>]
agent-runtime subagent reject <approval_id> --repo <sandbox> [--reason <text>]
agent-runtime subagent show-plan <approval_id> --repo <sandbox>
agent-runtime subagent mailbox send --repo <sandbox> --mailbox <id>
    --sender <task> --recipient <task> --payload <json>
agent-runtime subagent mailbox read --repo <sandbox> --mailbox <id> --recipient <task>
```

## Acceptance criteria

- A parent can spawn a child run; both the child task and `subagent_runs` row
  persist.
- `subagent approvals` reports `plan_file` for every approval, and
  `subagent show-plan` prints the immutable markdown file.
- A child whose estimated context exceeds `context_window` fails closed before
  it starts or resumes.
- Spawning deeper than three task layers is rejected.
- Operator-initiated CLI spawns persist with `parent_task_id = NULL`.
- A parent or operator can restart and resume a pending/running child from the
  child's checkpoint; a duplicate claim is rejected by fencing/CAS.
- Child messages and `result_summary` survive restart.
- Mailbox send/read survives restart; read is atomic and cannot double-deliver;
  archive only follows read.
- Plan approval transitions are CAS-protected and record `plan_hash`,
  `decided_by`, and `reason`; stale transitions raise `StaleState`.
- Child non-read-only calls appear in `operations` and `effect_reservations`
  under the child `task_id`.
- A child cannot execute a tool excluded by `tool_scope`.
- Parent and child never share a `task_id`.
- Existing fencing, effect, and Phase 4 MCP tests still pass.

## Verification

```powershell
python -m pytest agent_runtime/tests -q
python -m agent_runtime db-check --repo <sandbox>
python -m agent_runtime db-migrate --repo <sandbox> --dry-run
python -m agent_runtime trace-export <child-task-id> --out trace.jsonl
```

New test module: `agent_runtime/tests/test_v09_phase3_subagents.py`.

## Migration and rollback

- v9 only adds new tables and indexes; it never alters existing rows.
- Fresh databases build directly to v9.
- Existing v8 databases migrate v5 -> v9 through the standard `SchemaManager`
  path with backup evidence.
- Rollback is documented in `db-migrate --dry-run` output and the migration
  table; no data is rewritten by v9.

## Handoff

### Complete before Phase 5

- `subagent_runs`, `mailboxes`, `mailbox_messages`, and `plan_approvals` exist
  at v9.
- Child execution uses the full Runtime task path and survives restart.
- Plan approval FSM and mailbox CAS are tested.
- Permission and effect boundaries are tested.

### Phase 5 entry state

- `subagent_runs.lane_id` already exists and defaults to `'default'`.
- Phase 5 replaces the repo lease with `(repo_root, lane_id)` and can then run
  parent and child concurrently in different lanes without changing subagent
  state.
- MCP tools are already registered through `ToolRegistry`; child `tool_scope`
  can restrict which MCP tools are exposed.

### Known deferred boundaries

- Full multi-agent consensus is deferred.
- Dynamic teammate discovery is deferred.
- Cross-repository team protocols are deferred.
- Inline subagent concurrency inside one repo is deferred to Phase 5 lanes.
- Real GitHub MCP smoke is deferred to after Gate 2.
