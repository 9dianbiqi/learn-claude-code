# Phase 2: Background Job + Cron

## Status

Implemented in `v0.3.0.dev2`. This phase replaces teaching-thread/in-memory queues with a
SQLite-backed job system that can survive process restart.

## Objective

Add durable background jobs and cron schedules. Jobs must be claimable,
heartbeated, fenced, retried, and recovered using the same lease discipline as
the main runtime.

## Reused teaching modules

| Module | What is reused | What changes |
|---|---|---|
| `s13_background_tasks` | slow-operation background semantics | in-memory thread queue becomes SQLite-backed |
| `s14_cron_scheduler` | schedule representation and trigger semantics | durable schedules and job runs |

## Scope

### In scope

- Add `agent_jobs`, `job_runs`, and `cron_schedules`.
- Add claim/heartbeat/release/complete/fail transitions.
- Recover `running` or `unfinished` jobs on restart.
- Fence stale job owners.
- Trigger cron jobs into durable job records.
- Emit audit events for job lifecycle.

### Out of scope

- Distributed workers or external queue systems.
- Cron timezone DSL expansion.
- Generic external side-effect exactly-once guarantees.
- Subagent job execution.

## Schema direction

```text
agent_jobs
  job_id
  task_id
  repo_root
  lane_id
  kind
  payload_json
  status
  attempts
  max_attempts
  available_at
  created_at
  updated_at

job_runs
  run_id
  job_id
  owner_id
  fencing_token
  status
  heartbeat_at
  lease_expires_at
  started_at
  completed_at
  error
  result_digest

cron_schedules
  schedule_id
  task_id
  expression
  job_kind
  payload_json
  enabled
  last_triggered_at
  next_trigger_at
```

## Job state machine

```text
agent_jobs.status:
  pending -> claimed -> running -> completed
                        \-> failed -> retryable
                        \-> cancelled
```

`job_runs.status` records the active execution. A job cannot have more than one
active claimed run. Lease expiry must move the run to failed/retryable and
release the job for another worker.

## Runtime integration

- Background tool adapters enqueue `agent_jobs`, then return a placeholder
  `tool_result`.
- A worker acquires a job only after repo and lane lease checks succeed.
- Job completion appends an audit event and, where applicable, a
  `task_notification`.
- Cron scheduler creates a job row and does not execute the payload directly.

## Fencing requirement

Every job transition uses expected version or fencing token:

```text
claim -> expected status/version
heartbeat -> expected owner + fencing token
complete/fail -> expected owner + fencing token + status
```

A stale owner must not be able to complete or fail a job after its lease is
lost.

## Acceptance criteria

- A running job is recovered after subprocess or runtime restart.
- A stale job owner is fenced and the job becomes retryable.
- A failed job does not spawn a duplicate active run.
- Cron schedule creates a durable job at the intended boundary.
- Background job events are redacted and exported to trace.
- Existing Runtime and effect-ledger tests remain green.

## Verification

```powershell
python -m pytest agent_runtime/tests -q
python -m pytest agent_runtime/tests/test_phase2_hardening.py -q
python -m agent_runtime db-check --repo <sandbox>
```

## Handoff

### Complete before Phase 3

- SQLite-backed jobs and cron schedules exist.
- Claim/heartbeat/fencing/recovery are implemented.
- Restart and stale-owner tests pass.

### Phase 3 entry state

Phase 3 subagents may use job records for asynchronous work, but the initial
subagent implementation can run synchronously and reuse the durable run
lifecycle directly.

### Known deferred boundaries

- No external distributed queue.
- No generic exactly-once for arbitrary shell payloads.
- Cron timezone and calendar extensions are deferred.
