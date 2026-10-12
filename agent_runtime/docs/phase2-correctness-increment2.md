# Phase 2 Correctness Increment 2: cross-process recovery

Date: 2026-10-08. Status: ACCEPT WITH NOTES for Increment 2 only; locally sealed 2026-10-12.
Baseline: `43c8127fb490bafd6c956fb03335f2588d6255c7`, tag
`phase2-increment1-accepted-20261008`. No Increment 1 contract was changed.

## Scope and implementation inspection

Read the Increment 1 seal, remaining-work inventory, and relevant Runtime,
EventStore and ToolExecutor paths before adding tests. HEAD matched the baseline
and the worktree was clean. Existing test entry is `python -m pytest`.

`EventStore.transaction` checks bound lease at entry and before commit.
`acquire_lease` refuses takeover while an expired owner's running reservation
belongs to a live OS PID; a dead owner's dispatched operation becomes unknown,
its outbox blocked, before a new epoch is granted. Heartbeat/release check owner
and token. Runtime resumes under the lease and checks it at effect boundaries.
Checkpoint/model-result writes are fenced. File recovery accepts an expected
post-state match without executing the write again; third-state content requires
review. File hash/identity checks plus replacement are not an arbitrary external
writer's atomic CAS. These existing contracts remain unchanged.

## Test method and raw evidence

Only `tests/test_phase2_multiprocess.py` was added; production changes: **none**.
Workers use Python multiprocessing **spawn**, separate PIDs and independent
Runtime/SQLite connections. IPC Events/Pipes control the start and hold barriers.
The parent never uses sleep to guess ordering. It injects `expires_at=0` only
after a worker acknowledges the selected barrier. This tests the expired state,
not accuracy of wall-clock TTL duration. OS process liveness is not mocked.

The seed is a deterministic bootstrap checkpoint (including a pending assistant
tool request where required), not a model-generated prefix. Workers call public
`Runtime.resume`; the foreign-task probe calls `Runtime.run`. ScriptedModel only;
no provider calls. Fault callbacks pause existing runtime boundaries; a wrapper
records dispatch and forwards to the actual built-in tool without replacing it.
Opaque shell really appends a marker. File writes use the real atomic replace.
The external drift variant uses another independent writer process.

Every case saves controller IPC order, worker PID/exit codes, fsynced worker and
dispatch logs, SQLite backup snapshots, full selected table dumps, and file
SHA-256 values. Dispatch logs count actual tool entry; marker content/file hashes
separately check effects. A dispatched operation counter alone is not proof that
the external effect occurred. Handled lease-refusal workers exit zero and report
the exception in JSON; hard-crash workers exit 73 via `os._exit`.

Evidence directory:
`D:/opencv/learn-claude-code/.scratch/phase2-increment2-20261008/`.
`run01/pytest.log`, `run01/exitcode.txt`, `run01/cases/`, `results.json`,
`test-source.py`, `increment2.patch`, `manifest.json` preserve the initial run.
The manifest binds the exact tested source and 101 evidence files. No failures
occurred in this first run and no tests were rerun.

Command (set PHASE2_MP_EVIDENCE to a new evidence directory):

```text
C:\Python311\python.exe -m pytest agent_runtime/tests/test_phase2_multiprocess.py -q --tb=short
```

Result: **5 passed in 40.59s**, pytest exit **0**. No older tests were executed.

## Safety invariants and evidence mapping

| Case | Controlled window and assertion | Result / evidence |
|---|---|---|
| Two workers resume one checkpoint | Shared start gate; winner held after durable tool preparation while loser attempts admission. Exactly one owner/operation, zero dispatch before release, one shell dispatch/effect afterward | PASS; `test_two_workers_resume_one_checkpoint`: both exit 0; loser reports lease refusal; marker once; operation committed, attempt/execution count 1 |
| Old worker returns after takeover | Old model response held before persistence; expire lease; new owner held at same boundary with higher token; release old worker. Exact equality of all ten table dumps before/after stale runtime return and explicit checkpoint/event/heartbeat/release probes | PASS; `test_old_worker_cannot_commit_after_takeover`: both exit 0; old reports LeaseLost; no old response committed, no tool dispatch; new completes |
| Live reservation then dead opaque owner | Real shell already appended, operation dispatched/reservation running. Expire while owner lives: contender refused without DB mutation. Hard-exit owner, resume; foreign task also attempts effect | PASS; `test_live_reservation_then_dead_opaque_owner`: writer 73, others 0; unknown/blocked state, zero recovery model calls, only one shell execution, no foreign marker |
| File effect before DB commit, matching content | Real file write held at effect-before-persist, process exits 73; independent worker resumes with expected post hash | PASS; `test_file_effect_crash_recovery[matching-post-state]`: recovery 0/completed; one total write, committed/delivered/completed operation/outbox/reservation |
| Same file crash, external drift | Separate writer changes file after crash and before recovery; current hash differs from expected | PASS; `test_file_effect_crash_recovery[external-conflict]`: writer 73, external/recovery 0; needs_review, no recovery model calls, unknown/blocked/unknown state; EXTERNAL bytes preserved; one runtime write |

Each case directory contains named intermediate `.json`/`.sqlite` snapshots and
`final` snapshots. `dispatch-counts.json` and `trace.json` map worker actions to
the database states. Crash snapshots assert dispatched operation, running
reservation with the real writer PID, and claimed outbox before termination.

## Remaining boundaries

- Five controlled schedules are bounded evidence, not exhaustive concurrency or
  power-loss verification. Real process death is tested; host/OS crash is not.
- TTL expiry is an explicit state injection. No claim about real-time timeout
  accuracy, process-tree cancellation, distributed leases or hung-owner recovery.
- The opaque effect is finished before the worker crash; surviving child process
  effects are not covered. No universal exactly-once guarantee is added.
- File conflict case is drift before recovery, not a writer racing the final
  check/replace instruction. Filesystem and SQLite remain separate transactions.
- Tests use the common non-DAG recovery path. They do not newly verify every
  verified-DAG/scoped context/approval branch, semantic obligation conservation,
  or the remaining Phase 2 requirement-to-evidence matrix.
- Independent review will be bounded to this increment. Passing these tests
  does not constitute final acceptance of all Phase 2.

## Independent acceptance

Fresh `gpt-6.1-sol`, medium reasoning, read-only reviewer
`/root/phase2_increment2_review` returned **ACCEPT WITH NOTES**. No blocker or
substantive issue found. The raw opinion is preserved without rewriting as
`review-increment2-accept.md` beside `acceptance.json` in the evidence directory.
The reviewer checked all 101 manifest hashes, 25 SQLite snapshots and matching
ten-table JSON dumps, process logs and the exact tested source. No tests or
model evaluations were rerun. Notes limit acceptance to the controlled admission
schedule, stale-model response path, already-finished opaque effect and
pre-recovery external drift. They do not extend any Increment 1 guarantee.

No production fixes were required. Increment 2 ends here; no merge, push or
whole-Phase-2 acceptance was performed.

## Local seal - 2026-10-12

Tag: `phase2-increment2-accepted-20261012`. Test source remains byte-identical
to its reviewed source manifest. Only document closure metadata changed. See
`phase2-final-acceptance.md` for the subsequent whole-phase bounded verdict.
