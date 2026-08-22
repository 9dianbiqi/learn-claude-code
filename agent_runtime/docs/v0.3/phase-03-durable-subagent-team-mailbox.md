# Phase 3: Durable Subagent / Team Mailbox

## Status

Planned for v0.3. This phase makes child agents and team communication durable
instead of process-local.

## Objective

Add SQLite-backed subagent runs, mailboxes, and plan-approval state. A parent
agent must be able to resume child coordination after restart, and a stale
child owner must not mutate another owner's state.

## Reused teaching modules

| Module | What is reused | What changes |
|---|---|---|
| `s06_subagent` | isolated child `messages[]` and summary-return pattern | child state becomes durable |
| `s15_agent_teams` | teammate mailbox semantics | mailbox becomes SQLite rows |
| `s16_team_protocols` | plan approval FSM | FSM becomes durable and fenced |

## Scope

### In scope

- Add `subagent_runs`, `mailboxes`, `mailbox_messages`, and
  `plan_approvals`.
- Define child-run states and ownership.
- Persist child messages and result summary.
- Implement mailbox send/receive with CAS.
- Implement plan approval state transitions.
- Define permission inheritance and effect boundary.

### Out of scope

- Multi-agent model training.
- Arbitrary multi-party consensus.
- Distributed team discovery.
- OS-level tenant isolation.

## Schema direction

```text
subagent_runs
  subagent_run_id
  parent_task_id
  child_task_id
  repo_root
  lane_id
  role
  status
  owner_id
  fencing_token
  messages_json
  result_summary
  created_at
  updated_at

mailboxes
  mailbox_id
  owner_task_id
  owner_role
  version
  created_at
  updated_at

mailbox_messages
  message_id
  mailbox_id
  sender_task_id
  recipient_task_id
  payload_json
  status
  created_at
  read_at

plan_approvals
  approval_id
  subagent_run_id
  plan_hash
  status
  requested_by
  decided_by
  reason
  version
  created_at
  updated_at
```

## Subagent state machine

```text
subagent_runs.status:
  pending -> running -> completed
                    \-> failed
                    \-> needs_review
```

The child run uses its own task/checkpoint lineage. Parent and child must not
share the same top-level `task_id`.

## Permission and effect boundary

- Child agents inherit a scoped permission policy derived from the parent.
- Child permission decisions must be recorded against the child task.
- Child non-read-only calls must create operations in the same effect ledger.
- A child cannot release or acquire the parent repository lease unless it uses
  an explicit lane-aware lease path.
- Parent review of a child result must occur before its result is considered
  authoritative.

## Mailbox protocol

Mailbox delivery and read state use version CAS. A message is never removed
without an explicit archive/delivery transition. Restart must preserve unread
mail.

## Plan approval FSM

```text
plan_approvals.status:
  requested -> approved
            -> rejected
            -> superseded
```

Approval decisions must record `plan_hash`, `decided_by`, and `reason`.

## Acceptance criteria

- A parent process can restart and resume a pending child run.
- Child messages and mailboxes survive restart.
- Stale child owner writes are rejected by fencing.
- Child effects appear in operations and effect reservations.
- Parent plan approval transitions are CAS-protected.
- A denied permission cannot be bypassed by the mailbox or child summary.

## Verification

```powershell
python -m pytest agent_runtime/tests -q
python -m agent_runtime db-check --repo <sandbox>
python -m agent_runtime trace-export <task-id> --out trace.jsonl
```

## Handoff

### Complete before Phase 4

- Durable subagent runs and mailboxes exist.
- Plan approval FSM is durable and fenced.
- Permission and effect boundaries are tested.

### Phase 4 entry state

Phase 4 can register subagent-accessible tools through the same tool registry.
MCP tools must carry permission declarations before they can be exposed to a
child.

### Known deferred boundaries

- Full multi-agent consensus is deferred.
- Dynamic teammate discovery is deferred.
- Cross-repository team protocols are deferred.

