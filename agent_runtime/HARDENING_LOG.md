# Agent Runtime MVP Hardening Log

Phase 1 baseline: `06ac34ed4d03951c3b9db884335962a6fbff3aff`

Status values: `OPEN`, `FIXED`, `VERIFIED`, `DEFERRED`, `REOPENED`.

`VERIFIED` is reserved for a later independent review. Phase 1 may mark an item
`FIXED` only after the implementation and targeted/full tests pass.

| Issue | Severity | Status | Fix Stage | Verification |
|---|---|---|---|---|
| P0-1 Bootstrap / initial checkpoint atomicity | P0 | VERIFIED | Phase 1 | Phase 3 independent rollback checks pass |
| P0-2 Abort terminal state and replay prevention | P0 | VERIFIED | Phase 1 | Phase 3 independent terminal/replay checks pass |
| P0-3 Lease fencing and stale-owner rejection | P0 | VERIFIED | Phase 6 | Phase 7 independent cross-process, recovery, fencing, and Windows liveness acceptance passes |
| P0-4 Permission rule fail-closed scoping | P0 | VERIFIED | Phase 5 | Independent deny/composition matrix passes |
| P0-5 Runtime internal namespace isolation | P0 | VERIFIED | Phase 1 | Phase 3 independent namespace checks pass |

## Phase 1 follow-up review

- Review finding: the v0.2 Runtime marked an operation `dispatched` before its
  final file, lease, and Shell preflight checks.
- Fix: `mark_operation_dispatched()` now runs immediately before
  `tools.execute()`. A pre-dispatch failure keeps the operation `prepared`,
  returns the outbox to `pending`, cancels only the local reservation, and
  supports an explicit safe retry.
- Tests: subprocess exit after migration DDL, prepare/claim/dispatch/commit
  transaction rollback, illegal state transition no-op, resolve projection
  matrix, pre-dispatch file conflict retry, and token/API-key/Authorization
  redaction.
- Verification status: `FIXED`; independent review remains required before
  changing this follow-up item to `VERIFIED`.

### PR #2 migration review follow-up

- The v4-to-v5 migration now validates legacy reservation/tool projections
  before changing v4 rows or applying v5 DDL. Invalid completed/running
  reservation histories fail closed and remain readable v4 databases.
- P2 deferred: the current contract requires sensitive fields to be redacted
  in operation events and exported Trace/JSONL. It does not define a blanket
  prohibition on every generic event payload being stored in plaintext in the
  local Runtime DB. Generic event-payload-at-rest redaction remains a v0.2
  hardening item and is not part of this merge-blocker fix.

### PR #2 final blocker closure

The preceding independent review recorded `REJECT FOR MERGE` for two remaining
blockers. That historical result is retained; this section records the
follow-up reproduction and fix.

#### Legacy projection validation

- Pre-fix reproduction at the PR head: six schema-valid v4 fixtures migrated
  successfully and then produced immediate v5 invariant violations. The
  fixtures were a completed task without a checkpoint, a failed task without a
  checkpoint, an aborted task without a checkpoint, a `needs_review` task with
  a succeeded tool, a `waiting_approval` task with a succeeded tool, and a
  completed task with a planned tool.
- Root cause: `_validate_legacy_rows()` checked only a subset of
  reservation/tool relationships. It did not validate the task, checkpoint,
  event, and review projection that `scan_invariants()` already enforced.
- Fix: the shared `_legacy_projection_violations()` validator now covers task
  status, task/checkpoint ownership and phase, terminal/review events,
  checkpoint-saved pointers, executable terminal/review tools, orphan events,
  tool statuses, and reservation/tool/task projections. Migration invokes it
  before stale-running mutation, DDL, backfill, or schema-version update;
  `scan_invariants()` uses the same validator with only the v5 prepared
  operation exception.
- After-fix evidence: all six fixtures reject twice; schema remains v4, the
  complete SQLite snapshot is unchanged, no v5 tables/columns appear, and
  `PRAGMA integrity_check` is `ok`. A legal completed+succeeded fixture
  migrates to v5 and scans with zero violations. A mixed legal v4 projection
  parity fixture also migrates and scans with zero violations.
- Tests: migration targeted `33 passed`; migration fault/rollback subset
  `6 passed`; the six adversarial cases are included in the targeted count.

#### Windows fencing test determinism

- Pre-fix CI reproduction: Windows Runtime run `31678898092` failed the two
  fencing tests at `started.wait(2)` and `effect_started.wait(2)` while the
  other Runtime tests passed (`2 failed, 173 passed`). Local repeated runs
  showed the Runtime behavior was correct; the test depended on scheduler and
  wall-clock lease timing.
- Root cause: the tests used `sleep()` both to wait for the effect boundary and
  to make the lease expire. Under Windows CI load the owner thread was not
  guaranteed to reach the event before the fixed two-second wait.
- Fix: tests now synchronize at Runtime fault hooks/events, inject lease expiry
  directly in the disposable test database, release the blocked model/effect
  through events, and assert the owner thread terminates. No Runtime fencing,
  takeover, stale-write, or effect barrier semantics were changed.
- After-fix evidence: the two-test fencing subset passed `20/20` repetitions
  (`40 passed, 0 failed`); Runtime tests passed `180 passed, 2 skipped`.

CI evidence for commit `d1b937a09d4d8b19b69c8d211f3fa23fdb26dfce`: Agent
Runtime push run `31686341539` and pull-request run `31686344680` both passed,
including Windows jobs `94403176801` and `94403188227`; the Test workflow
(`31686344638`) and CI workflow (`31686344584`) also passed. The PR required
check rollup was `7/7 pass`. P2 generic event-payload-at-rest redaction
remains `DEFERRED` under the existing contract documented above.

## P0-1 — Bootstrap / initial checkpoint atomicity

- First identified: adversarial review against the Phase 1 baseline.
- Trigger: a crash between task creation, `task_created`, and the initial checkpoint transaction.
- Risk: a task can exist with `checkpoint_id = NULL`, after which normal `resume()` cannot recover it.
- Root cause: bootstrap records are written by separate store transactions.
- Planned minimal fix: one domain-level SQLite transaction for task, initial checkpoint, task pointer, and bootstrap event.
- Tests: crash at each bootstrap boundary, restart recovery, no duplicate task or bootstrap event.

### Phase 1 result

- Pre-fix reproduction: the three bootstrap boundary cases did not raise/rollback; the baseline left a task recoverable only through a partially committed state.
- Fix: `EventStore.bootstrap_task()` commits task, initial checkpoint, pointer, `task_created`, and initial `checkpoint_saved` in one transaction, with fault hooks inside the transaction.
- Files: `agent_runtime/store.py`, `agent_runtime/runtime.py`.
- Tests: `agent_runtime/tests/test_phase1_p0.py` bootstrap parameterization (3 cases) passed.
- Verification status: `VERIFIED` in Phase 3; bootstrap crash matrix independently re-run.

## P0-2 — Abort terminal state and replay prevention

- First identified: adversarial review against the Phase 1 baseline.
- Trigger: an effect completes, recovery reaches `needs_review`, the operator selects `abort`, and a later `resume()` is issued.
- Risk: a failed/aborted call can re-enter execution and repeat Shell/file side effects.
- Root cause: no explicit aborted task terminal state, no terminal resume guard, and failed tool calls fall through to execution.
- Planned minimal fix: explicit `aborted` state, atomic abort terminal checkpoint/event, terminal resume rejection, and fail-closed tool status handling.
- Tests: Shell/file abort flows, completed/aborted resume rejection, effect counter invariants.

### Phase 1 result

- Pre-fix reproduction: Shell effect followed by `abort` and ordinary `resume()` executed the Shell effect twice; completed tasks could also be resumed.
- Fix: explicit `aborted` terminal state, atomic abort checkpoint/event/tool update, terminal resume rejection, and fail-closed handling for failed/aborted tool calls.
- Files: `agent_runtime/store.py`, `agent_runtime/runtime.py`.
- Tests: Shell abort, file abort, completed resume, and terminal tool-call cases passed.
- Verification status: `VERIFIED` in Phase 3; terminal replay independently re-run.

## P0-3 — Lease fencing and stale-owner rejection

- First identified: adversarial review against the Phase 1 baseline.
- Trigger: an owner blocks longer than the lease TTL and a second Runtime takes over.
- Risk: the stale owner can still mutate state or execute tools after takeover.
- Root cause: owner/TTL leases have no fencing generation and heartbeat failure is not surfaced to the execution path.
- Planned minimal fix: monotonic fencing epoch checked on state/event/checkpoint writes and immediately before/after tool effects; bounded model timeout and fail-closed heartbeat handling.
- Tests: slow model takeover, stale DB write/tool execution, stale heartbeat/release, TTL boundary, invalid token.

### Phase 1 result

- Pre-fix reproduction: two Runtime instances both completed after the first owner exceeded a short TTL.
- Fix: lease fencing token migration, bound lease context on every guarded transaction, stale heartbeat/release rejection, and lease checks before/after tool effects. Provider model timeout is configured and Runtime rejects a timeout not smaller than the lease TTL.
- Files: `agent_runtime/store.py`, `agent_runtime/runtime.py`, `agent_runtime/providers.py`.
- Tests: slow-owner takeover, stale tool effect, stale heartbeat/release, invalid token, and timeout margin cases passed.
- Verification status: `REOPENED` in Phase 3; stale-owner audit mutation and in-flight effect races remain.

## P0-4 — Permission rule fail-closed scoping

- First identified: adversarial review against the Phase 1 baseline.
- Trigger: a rule contains a path selector or command selector without an explicit compatible tool selector.
- Risk: a path-only rule can accidentally allow arbitrary Shell, while glob path restrictions can be ignored.
- Root cause: selector applicability is implicit and non-matching selector types are treated as unconstrained.
- Planned minimal fix: strict rule schema validation and explicit wildcard semantics; ambiguous combinations fail during policy load.
- Tests: path-only/command-only cross-tool cases, glob path enforcement, empty selector rejection, explicit wildcard acceptance.

### Phase 1 result

- Pre-fix reproduction: a path-only allow rule matched arbitrary Bash; a glob path rule did not constrain unmatched patterns; empty selectors loaded successfully.
- Fix: rule schema validation, explicit tool selectors, compatible path/command selector domains, conservative glob scope matching, and deny-on-no-matching scoped allow/ask rules.
- Files: `agent_runtime/permissions.py`, `agent_runtime/tests/test_phase1_p0.py`.
- Tests: path-only, command-only, glob restriction, explicit wildcard, and empty selector cases passed.
- Verification status: `REOPENED` in Phase 3; explicit Shell deny precedence is bypassable.

## P0-5 — Runtime internal namespace isolation

- First identified: adversarial review against the Phase 1 baseline.
- Trigger: an Agent accesses `.agent_runtime/**` through file, glob, or Shell tools.
- Risk: runtime DB/checkpoint/trace contents can leak or be destroyed by the Agent.
- Root cause: internal state is stored under the repository but is not reserved by the path or Shell policy.
- Planned minimal fix: reserve `.agent_runtime/**` and fail closed for all tool types, including Shell path operations.
- Tests: read/glob/write/edit and Shell delete/overwrite/rename/move/copy attempts; cross-task isolation.

### Phase 1 result

- Pre-fix reproduction: `read_file` and `glob` defaulted to `allow` for `.agent_runtime/runtime.db`.
- Fix: reserved namespace checks in permission evaluation and ToolExecutor path/glob/Shell defenses.
- Files: `agent_runtime/permissions.py`, `agent_runtime/tools.py`, `agent_runtime/runtime.py`.
- Tests: internal DB read, file write, glob, direct Shell access, and Runtime read attempts passed.
- Verification status: `VERIFIED` in Phase 3; internal namespace checks independently re-run.

## Phase 1 validation snapshot

- Targeted P0 tests: `18 passed, 0 failed`.
- `python -m pytest agent_runtime/tests -q`: `38 passed, 0 failed`.
- `python -m pytest -q`: `49 passed, 0 failed`.
- `python -m compileall -q agent_runtime`: passed.
- MVP Eval: `5/5`, completion rate `1.0`, recovery cases `1/1`, recovery success rate `1.0`.

## Phase 2 baseline and scope

The Phase 2 adversarial test file was added before implementation changes:
`agent_runtime/tests/test_phase2_hardening.py`.

- Baseline command: `python -m pytest agent_runtime/tests/test_phase2_hardening.py -q`
- Baseline result: `21 failed, 3 passed, 1 skipped`.
- The failures reproduce the Phase 2 gaps across terminal recovery, CAS, Windows path
  containment, Shell boundaries/results, effect-attempt accounting, store invariants,
  trace metrics, Eval fault/root safety, and size limits.

| Issue | Severity | Status | Fix Stage | Verification |
|---|---|---|---|---|
| P1-1 Terminal completion atomicity and model-response recovery | P1 | VERIFIED | Phase 2 | Phase 3 completion-boundary recovery checks pass |
| P1-2 Approval/resolve lease + CAS | P1 | VERIFIED | Phase 2 | Phase 3 approval race check pass |
| P1-3 File TOCTOU and Windows containment | P1 | VERIFIED | Phase 2 | Existing and Phase 3 containment checks pass |
| P1-4 Shell Controlled Live boundary | P1 | VERIFIED | Phase 5 | Independent embedded-parent traversal matrix passes |
| P1-5 Shell result semantics | P1 | VERIFIED | Phase 2 | Phase 3 nonzero/timeout check pass |
| P1-6 Exactly-once-ish attempt semantics | P1 | VERIFIED | Phase 2 | Existing Eval and Phase 3 lease/effect checks reviewed |
| P1-7 Store invariant checker and schema fail-closed | P1 | VERIFIED | Phase 5 | Independent fault/mismatch/fail-closed checks pass |
| P1-8 Trace/audit metrics and redaction | P1 | VERIFIED | Phase 2 | Existing full suite and code review pass |
| P1-9 Hardening Eval false-positive and root safety | P1 | VERIFIED | Phase 5 | Independent marker/identity/reparse checks pass; real symlink conditional |
| P1-10 Timeout and size limits | P1 | VERIFIED | Phase 2 | Existing full suite and Eval pass |

## Phase 2 implementation result

### P1-1 Terminal completion atomicity and model-response recovery

- `EventStore.complete_task()` now commits the terminal checkpoint, task status,
  completion event, and checkpoint event in one guarded transaction with fault
  hooks at each boundary.
- A successful durable model response is materialized into a recoverable
  `model_responded` checkpoint when its checkpoint was missing. Resume uses that
  response and does not invoke the model again.
- A persisted model response and a completion-boundary crash therefore roll back
  or recover without a partially terminal task.

### P1-2 Approval/resolve lease + CAS

- Approval, denial, retry, manual completion, and abort acquire the repository
  lease, re-read current task/tool state, and use task/tool versions as CAS
  preconditions.
- `StaleState` rejects stale updates before any partial state is committed.
  Approval resume evaluates the current permission context again.

### P1-3 File TOCTOU and Windows containment

- File observations include SHA-256 plus an OS identity tuple; the executor
  rechecks both immediately before a write and verifies the post-write hash.
- Symlink/reparse components, UNC, drive-relative, ADS, traversal, and absolute
  outside paths fail closed. Edit/read paths use `newline=""`, preserving CRLF
  and BOM bytes.
- A full kernel-level compare-and-swap/open-handle write primitive is not part of
  this MVP; the remaining narrow race is documented for v0.2, while a detected
  conflict becomes `needs_review` and never overwrites the observed external
  state.

### P1-4 Shell Controlled Live boundary

- Shell policy never auto-allows composed commands; pipe, redirect, `&&`, `||`,
  `;`, `&`, command/variable substitution, external paths, wrappers, encoded
  PowerShell, and indirect scripts are ask/deny (explicitly dangerous wrappers
  are denied). Runtime internals remain reserved.
- Execution still uses `shell=True` behind this policy boundary. A complete OS
  sandbox is deliberately deferred and is not claimed by this MVP.

### P1-5 Shell result semantics

- Bash returns and persists return code, bounded stdout/stderr, timeout flag, and
  execution status. Only return code 0 without timeout is successful.
- Nonzero and timeout outcomes become `needs_review`; unknown side effects are
  never auto-retried.

### P1-6 Exactly-once-ish attempt semantics

- Tool rows persist execution/effect attempt counters, effect keys, and confirmed
  effect metadata. File effects are confirmed by post-state hash; Shell effects
  retain an explicit unknown-side-effect marker.
- Trace/Eval report duplicate effect attempts separately from dedup events and do
  not infer side-effect uniqueness from `tool_deduplicated`.
- A general cross-process effect journal/idempotency key protocol is deferred to
  v0.2; unknown effects fail closed to review.

### P1-7 Store invariants and schema fail-closed

- Schema version, local SQLite path, task/checkpoint pointers, terminal phases,
  executable-call restrictions, terminal events, checkpoint event pointers, and
  lease fencing fields are checked by `scan_invariants()`.
- Unsupported schema versions and invariant mismatches raise and stop execution;
  the runtime does not silently repair corrupted state.

### P1-8 Trace/audit metrics

- Trace export relates task, events/state transitions, model calls, tool args and
  result metadata, permissions, checkpoints, and attempt counters. Sensitive keys
  and token-like values are redacted in exported traces.
- Portable multi-file bundles, retention, and archival are deferred to v0.2;
  the current JSONL export remains a bounded local audit artifact.

### P1-9 Hardening Eval false-positive and root safety

- Eval records actual fault trigger/interruption state; a configured but
  non-triggered fault fails the case and is not counted as recovery.
- Recovery success is calculated only over actual interrupted cases. Run roots
  require a dedicated marker, reject the repository/`.agent_runtime`, and are
  checked before cleanup; fixture and check paths are containment-validated.
- `evals/hardening.yaml` adds deterministic crash, completion, file, Shell, and
  traversal cases; adversarial unit tests cover lease, approval, schema, and
  invariant categories.

### P1-10 Timeout and size limits

- Model timeout remains strictly below the lease TTL; Shell timeout is bounded to
  the same lease margin. Read files, Shell streams, model responses, model
  requests, and checkpoint messages have explicit byte limits.
- Context compaction, retention/archival, and WAL maintenance are deferred to
  v0.2; oversized data fails closed instead of growing without a bound.

## Phase 2 final validation snapshot

- Starting/ending HEAD: `06ac34ed4d03951c3b9db884335962a6fbff3aff` (no commit, push, or merge).
- Phase 1 P0 targeted: `python -m pytest agent_runtime/tests/test_phase1_p0.py -q` → `18 passed`.
- Phase 2 targeted: `python -m pytest agent_runtime/tests/test_phase2_hardening.py -q` → `38 passed, 1 skipped`.
- Runtime tests: `python -m pytest agent_runtime/tests -q` → `76 passed, 1 skipped`.
- Full non-Live tests: `python -m pytest -q` → `87 passed, 1 skipped`.
- Compile check: `python -m compileall -q agent_runtime` → passed.
- MVP Eval (`evals/mvp.yaml`): `5/5`, completion `1.0`, actual interruptions `1`, recovery `1/1`, recovery success `1.0`.
- Hardening Eval (`evals/hardening.yaml`): `9/9`, completion `7/9` (two expected `needs_review`), actual interruptions `7`, recovery `7/7`, recovery success `1.0`.
- Final Eval metrics: permission bypass `0`, invariant violations `0`, needs-review correctness `1.0`, stale-lease execution attempts `0`; duplicate effect attempts and confirmed duplicate side effects are reported independently (`0` in these suites).
- Independent invariant scan: completed task with `0` violations; `assert_invariants()` passed.
- Log consistency: `15 FIXED`, `0 OPEN`, `0 VERIFIED`, `0 DEFERRED`, `0 REOPENED` (Phase 2 remains `FIXED`, not `VERIFIED`).
- Existing unrelated dirty worktree entries, including the teaching track, were preserved unchanged; only `.gitignore`, `agent_runtime/`, and `evals/` were in Phase 2 scope.

## Phase 3 independent controlled-live review (2026-08-10)

- Baseline and ending HEAD: `06ac34ed4d03951c3b9db884335962a6fbff3aff`.
- No source/test/teaching-code changes were made. No real model call and no Controlled Live execution were performed, per the Phase 3 read-only constraint.
- Re-run evidence: `agent_runtime/tests` `76 passed, 1 skipped`; full non-Live `87 passed, 1 skipped`; `compileall` passed; MVP Eval `5/5`; Hardening Eval `9/9` with `7/7` recovery success.
- Independent adversarial matrix: `25` assertions, `20` passed, `4` failed findings, `1` skipped because this Windows host denies symlink creation (`WinError 1314`).
- Current status counts: `10 VERIFIED`, `5 REOPENED`, `0 FIXED`, `0 DEFERRED`, `0 OPEN`.
- Reopened blockers: P0-3 stale-owner event/effect fencing; P0-4 explicit Shell deny precedence; P1-4 embedded Shell parent traversal; P1-7 non-terminal review/checkpoint mismatch detection; P1-9 Eval symlink/reparse run-root containment.
- Decision: `REJECT FOR CONTROLLED LIVE`.

## Phase 4 reopened-live-blocker fixes (2026-08-11)

Phase 3's original `REOPENED` evidence is retained above. This section records
only the bounded fixes and post-fix validation; these items are `FIXED`, not
`VERIFIED`.

### P0-3A — stale owner store writes

- Reproduction: after owner A's token expired, `_assert_lease_for_effect()` raised
  `LeaseLost` but `append_event_unfenced()` still inserted a task event.
- Root cause: the unfenced audit helper was an active execution escape hatch.
- Fix: removed `append_event_unfenced()` and changed stale diagnostics to Python
  logging only. Guarded transactions remain the only task/tool/checkpoint/event
  projection writes. Lease release now validates the owner/token and retains the
  fencing epoch; it may only mark that owner's reservation journal unknown.
- Files: `agent_runtime/runtime.py`, `agent_runtime/store.py`.
- Tests: stale owner event/write matrix in `test_phase2_hardening.py` and
  `test_phase4_hardening.py` (heartbeat, release, task/tool/event/checkpoint
  writes; zero stale projection writes).
- Before: one stale task event write succeeded. After: zero; B's lease row and
  fencing token remain unchanged.

### P0-3B — in-flight effect versus takeover

- Reproduction: A entered Shell, TTL expired, B took over, and A still produced
  a marker before noticing `LeaseLost`.
- Root cause: lease checks bracketed the effect but did not reserve the effect
  while it was in flight.
- Fix: schema version 4 adds a fenced `effect_reservations` journal. A valid
  owner must reserve before an effect; a takeover is blocked while a live owner
  process has a running reservation. Dead owners are reconciled to
  `unknown_effect` before takeover. `complete_effect()` atomically rechecks
  fencing, closes the reservation, and persists the confirmed tool outcome;
  crashed/ambiguous Shell effects are never retried automatically.
- Files: `agent_runtime/store.py`, `agent_runtime/runtime.py`.
- Tests: in-flight takeover race, dead-owner reservation recovery, Shell crash
  recovery, file hash reconciliation, and duplicate-effect metrics.
- Before: concurrent takeover was possible. After: takeover while the
  reservation is running returns no lease; the marker is produced at most once
  in the injected race and recovery enters review for unknown Shell effects.

### P0-4 — explicit deny precedence

- Reproduction: an explicit deny for `echo x | cat` was returned as `ask` by the
  composition safety branch.
- Root cause: composition downgrade ran before policy rule ranking.
- Fix: matching rules are ranked first (`deny > ask > allow`); composition can
  downgrade only allow/default outcomes to ask. Operator approval cannot change
  an explicit deny.
- Files: `agent_runtime/permissions.py`.
- Tests: deny plus pipe, redirect, `&&`, `||`, `;`, substitution, PowerShell
  composition, and repo escape; Runtime execution-attempt count is zero.
- Before: deny became ask. After: all cases deny and execute zero tool effects.

### P1-4 — Shell parent-segment escape

- Reproduction: explicit allow for `type .\\..\\outside.txt` read outside the
  repository; embedded `foo/../outside` segments were missed.
- Root cause: the detector only recognized `..` at token starts.
- Fix: Shell external-path detection now matches `..` as a path segment at any
  separator/quote boundary, including mixed separators and redirect targets.
- Files: `agent_runtime/permissions.py`, `agent_runtime/tests/test_phase4_hardening.py`.
- Tests: ten read/write/redirect/quoted variants covering `/`, `\\`, mixed
  separators, and multiple parent segments.
- Before: explicit allow and outside read were possible. After: permission is
  deny, execution attempts remain zero, and the outside marker is unchanged.

### P1-7 — atomic review transition and invariants

- Reproduction: a crash after `update_task(needs_review)` left task status and
  checkpoint/event projections inconsistent while the invariant scan returned
  no violation.
- Root cause: task, tool, checkpoint, and review events were separate writes;
  invariants did not relate review projections.
- Fix: `transition_review()` commits tool status, task status, checkpoint pointer,
  review metadata, task/tool events, and checkpoint event in one guarded
  transaction. Fault hooks cover checkpoint/status/event boundaries. Invariants
  now require matching review checkpoints/events/tool states and allow a review
  checkpoint to be running only with a recorded CAS resolution event.
- Files: `agent_runtime/store.py`, `agent_runtime/runtime.py`.
- Tests: all three review crash boundaries plus manual mismatch matrices;
  rollback leaves no half review projection and restart performs no effect.
- Before: task `needs_review` + `model_responded` checkpoint + no event passed.
  After: the transition either fully commits or rolls back, and mismatches fail
  closed.

### P1-9 — Eval root symlink/junction/reparse safety

- Reproduction: Eval resolved the run root before checking whether the user
  supplied a symlink/reparse path; the host could not create a symlink because
  of Windows `WinError 1314`.
- Root cause: cleanup safety relied on `Path.resolve()` and marker presence only.
- Fix: lexical root/parent `lstat` and Windows reparse-attribute checks happen
  before resolve; cleanup rechecks marker content, root identity, containment,
  and reparse status. Synthetic branch coverage is included for restricted
  Windows hosts.
- Files: `agent_runtime/eval_runner.py`, `agent_runtime/tests/test_phase4_hardening.py`.
- Tests: synthetic reparse-root rejection passes; real symlink integration is
  conditional and skipped on this host when privilege is unavailable.
- Before: no root reparse guard. After: unsafe roots are rejected before
  creation/cleanup and safe roots retain the dedicated marker/identity checks.

### Phase 4 validation snapshot

- Starting/ending HEAD: `06ac34ed4d03951c3b9db884335962a6fbff3aff`; no commit,
  push, or merge; no real model and no Controlled Live execution.
- Targeted Phase 4: `28 passed, 1 skipped` (Windows symlink privilege branch).
- Runtime suite: `python -m pytest agent_runtime/tests -q --tb=short` →
  `104 passed, 2 skipped`.
- Full non-Live (excluding ignored worktree/virtualenv copies):
  `python -m pytest -q --ignore=.worktrees --ignore=.venv --ignore=venv` →
  `115 passed, 2 skipped`.
- Compile: `python -m compileall -q agent_runtime` passed.
- MVP Eval: `5/5`, completion `1.0`, interruption `1`, recovery `1/1`, recovery
  success `1.0`; duplicate effects `0`, bypasses `0`, invariant violations `0`.
- Hardening Eval: `9/9`, completion `7/9`, interruptions `7`, recovery `7/7`,
  recovery success `1.0`; duplicate effects `0`, bypasses `0`, invariant
  violations `0`, needs-review correctness `1.0`.
- Independent reopened matrix: all four prior failures plus review/reparse
  boundaries covered; Phase 4 targeted set `28 passed, 1 skipped`.
- Effect/fencing metrics: stale task-store writes `0`; stale effect starts `0`;
  concurrent in-flight takeover violations `0`; duplicate confirmed effects `0`.
- Current status counts: `10 VERIFIED`, `5 FIXED`, `0 REOPENED`, `0 DEFERRED`,
  `0 OPEN`.
- Remaining live blockers: none reopened in this Phase 4 scope. The next step is
  an independent review of these `FIXED` items; this log does not grant Live
  acceptance.

## Phase 5 independent reopened-issue acceptance review (2026-08-11)

This review was read-only against the same HEAD. No Runtime/Eval implementation,
existing test, or teaching code was changed; only this status/evidence section
was appended after validation. No real model or Controlled Live execution was
performed.

### Reopened-issue results

- **P0-3 — `REOPENED`**. The independent stale-owner matrix passed: heartbeat,
  release, task/tool/event/checkpoint writes, and stale completion were all
  fenced, with `stale store writes = 0` and the newer lease unchanged. The
  active-reservation branch also blocked takeover when the owner was synthetic-
  live, and crash recovery did not retry an unknown Shell effect.
- **P0-3 reproduction (repository-level unknown-effect takeover)**: a dead
  owner reservation was converted to `unknown`; after the lease was released,
  a fresh Runtime started a new task in the same repository and executed
  `echo new > new-task-marker.txt` before the original effect was resolved.
  The marker was created and one new effect attempt was recorded. This violates
  the Phase 5 requirement that an active/unknown effect prevent a new executable
  owner/effect, so `concurrent takeover violations = 1` and `stale effect starts
  = 1` for this adversarial case.
- **P0-3 additional Windows finding**: the liveness implementation calls
  `os.kill(pid, 0)`. An independent subprocess check using the current Windows
  PID produced a delayed `KeyboardInterrupt`; the combined Runtime suite and
  Phase 4 suite consequently stop part-way through when the in-flight test
  probes the current process. This is an unsafe process-liveness primitive, not
  a test-only failure.
- **P0-4 — `VERIFIED`**. Nine independent commands covering pipe, redirect,
  `&&`, `||`, `;`, command substitution, encoded/command PowerShell, and repo
  escape remained `deny` under an allow+deny policy. Approval callbacks were not
  invoked and execution attempts were `0` for every case.
- **P1-4 — `VERIFIED`**. Ten independent embedded-parent variants (mixed
  separators, quotes, whitespace, multiple segments, read and redirect targets)
  failed closed; the outside marker was unchanged and execution attempts were
  `0`.
- **P1-7 — `VERIFIED`**. Three review-transaction fault boundaries rolled back
  completely. Five hand-built mismatch states were detected, and Runtime
  refused to continue from a mismatch before invoking a tool.
- **P1-9 — `VERIFIED`**. Independent checks covered lexical root/parent
  reparse rejection, missing and invalid markers, root identity drift, and an
  actual Windows junction. The real symlink integration remains a conditional
  skip because this host lacks the privilege (`WinError 1314`).

### Phase 5 validation snapshot

- Starting/ending HEAD: `06ac34ed4d03951c3b9db884335962a6fbff3aff`.
- Independent adversarial harness: `6 passed / 7 total`; only the P0-3
  repository-level unknown-effect takeover assertion failed.
- Key regression smoke set: `12 passed`.
- Runtime files individually: Phase 1 `18 passed`; Phase 2 `38 passed, 1
  skipped`; Stage 1–4 `20 passed`. The required combined
  `python -m pytest agent_runtime/tests -q --tb=short` was interrupted by the
  Windows liveness `KeyboardInterrupt` (`84 passed, 2 skipped` at the last
  verbose reproduction); the Phase 4 file likewise stopped after `14 passed`,
  while excluding the in-flight probe yielded `27 passed, 1 skipped`.
- Full non-Live command (`--ignore=.worktrees --ignore=.venv --ignore=venv`)
  was also interrupted by the same signal (`81 passed, 2 skipped` at the
  reproduction).
- `python -m compileall -q agent_runtime`: passed.
- MVP Eval: `5/5`, completion `1.0`, recovery `1/1`, duplicate confirmed effects
  `0`, permission bypasses `0`, invariant violations `0`.
- Hardening Eval: `9/9`, completion `7/9`, recovery `7/7`, duplicate confirmed
  effects `0`, permission bypasses `0`, invariant violations `0`, needs-review
  correctness `1.0`.
- Independent clean invariant scan: `0` violations.
- Key metrics: stale store writes `0`; stale effect starts `1` in the reopened
  attack; concurrent takeover violations `1`; automatic retry of unknown
  effects `0`; duplicate confirmed effects `0`; permission bypasses `0`; clean
  invariant violations `0` (corrupt-state cases were all detected).
- Current status counts: `14 VERIFIED`, `0 FIXED`, `1 REOPENED`, `0 DEFERRED`,
  `0 OPEN`.
- Decision: `REJECT FOR CONTROLLED LIVE`.

### Minimum next-fix scope for P0-3

Before any Controlled Live attempt, repository lease acquisition and new-task
execution must be gated on unresolved `unknown` reservations for the repository;
recovery must enter reconciliation/`needs_review` and require an explicit
resolution before any new effect. Replace the Windows `os.kill(pid, 0)` probe
with a non-signalling process-liveness check and add a cross-process regression
that holds an unknown reservation while a second task attempts an effect.

## Phase 6 P0-3 remediation (2026-08-11)

This phase implemented only the reopened P0-3 scope. No real model call or
Controlled Live execution was performed, and no teaching-track code was
changed.

### P0-3 repository effect gate and recovery

- `EventStore.list_blocking_reservations()` now exposes every `running` or
  `unknown` reservation for a canonical repository. `reserve_effect()` repeats
  that check inside its guarded `BEGIN IMMEDIATE` transaction, so a second
  process cannot race the preflight check and create a new side effect.
- Runtime side-effect execution performs a fail-closed preflight and handles
  the transactional `EffectBlocked` result as `needs_review`; no reservation
  or external effect is created for the blocked call.
- Resume checks repository blockers before model execution. An unresolved
  Shell/unknown-write reservation is recovered in reconciliation-only mode and
  enters `needs_review` without a model call. A file reservation is allowed to
  verify its durable expected-after SHA-256; a matching post-state atomically
  completes the unknown reservation and records `tool_recovered_succeeded`.
- `complete`, `retry`, and `abort` now close the associated unknown reservation
  (`completed` or `cancelled`) in the same CAS-protected transaction as the
  review decision. Other tasks remain blocked until every repository blocker is
  terminal.

### P0-3 Windows liveness

- Windows process checks use `OpenProcess` + `GetExitCodeProcess` and never call
  `os.kill(pid, 0)`; explicit invalid-PID results are dead, while unknown
  Win32 query errors fail closed as alive. POSIX retains the existing
  `os.kill(pid, 0)` behavior.
- The regression runs the liveness probe in a child process, waits beyond the
  former delayed-signal window, and requires a clean exit.

### Phase 6 validation snapshot

- Targeted Phase 6 matrix:
  `python -m pytest agent_runtime/tests/test_phase6_p0_3.py -q --tb=short`
  -> `9 passed`.
- Runtime suite:
  `python -m pytest agent_runtime/tests -q --tb=short`
  -> `113 passed, 2 skipped`.
- Full non-Live suite:
  `python -m pytest -q --tb=short --disable-warnings --ignore=.worktrees --ignore=.venv --ignore=venv`
  -> `124 passed, 2 skipped`.
- Compile check: `python -m compileall -q agent_runtime` -> passed.
- MVP Eval (`evals/mvp.yaml`): `5/5`, completion `1.0`, recovery `1/1`,
  recovery success `1.0`, duplicate confirmed effects `0`, permission bypasses
  `0`, invariant violations `0`.
- Hardening Eval (`evals/hardening.yaml`): `9/9`, completion `7/9` (expected
  review outcomes), recovery `7/7`, recovery success `1.0`, duplicate confirmed
  effects `0`, permission bypasses `0`, invariant violations `0`, needs-review
  correctness `1.0`.
- The matrix includes active-owner takeover rejection, dead-owner
  `unknown` conversion, cross-task and cross-process effect blocking,
  reconciliation-only recovery, explicit complete/retry/abort resolution,
  stale-owner mutation rejection, and the Windows non-signalling probe.
- Starting/ending HEAD:
  `06ac34ed4d03951c3b9db884335962a6fbff3aff` (no commit, push, or merge).
- Current status counts: `14 VERIFIED`, `1 FIXED`, `0 REOPENED`, `0 DEFERRED`,
  `0 OPEN`.
- Decision: `READY FOR NEXT INDEPENDENT ACCEPTANCE`. This remediation does not
  itself grant Controlled Live approval; the next independent review must
  re-run the cross-process takeover and Windows liveness checks against the
  final workspace.

## Phase 7 independent acceptance of Phase 6 P0-3 (2026-08-11)

This was a read-only acceptance review against the final workspace. No Runtime
or test implementation was changed, no teaching-track code was changed, and
no real model or Controlled Live execution was performed.

### Independent P0-3 matrix

An independently authored harness exercised eight cases using separate SQLite
connections and a real child process for the cross-process and liveness paths:

- active owner blocks lease takeover;
- expired/dead owner converts a running reservation to `unknown`;
- a second process cannot create a new effect or marker while the repository is
  blocked;
- original-task resume does not call the model and enters `needs_review`;
- `complete`, `retry`, and `abort` terminate the reservation correctly;
- a stale owner cannot mutate task state after takeover;
- Windows liveness probing does not inject a delayed `KeyboardInterrupt`.

Independent result: `8/8 passed`.

### Phase 7 validation snapshot

- Starting/ending HEAD:
  `06ac34ed4d03951c3b9db884335962a6fbff3aff` (no commit, push, or merge).
- Runtime suite:
  `python -m pytest agent_runtime/tests -q --tb=short` ->
  `113 passed, 2 skipped`.
- Full non-Live suite:
  `python -m pytest -q --tb=short --disable-warnings --ignore=.worktrees --ignore=.venv --ignore=venv`
  -> `124 passed, 2 skipped`.
- Compile check: `python -m compileall -q agent_runtime` -> passed.
- MVP Eval: `5/5`, completion `1.0`, recovery `1/1`, invariant violations `0`,
  duplicate confirmed effects `0`, permission bypasses `0`.
- Hardening Eval: `9/9`, completion `7/9` with expected review outcomes,
  recovery `7/7`, invariant violations `0`, duplicate confirmed effects `0`,
  permission bypasses `0`, needs-review correctness `1.0`.
- The two skipped tests are the existing Windows symlink-privilege branches;
  no P0-3 assertion was skipped.
- Final status counts: `15 VERIFIED`, `0 FIXED`, `0 REOPENED`, `0 DEFERRED`,
  `0 OPEN`.
- Decision: `P0-3 VERIFIED; READY FOR A SEPARATE CONTROLLED LIVE GATE`.
  This acceptance did not execute or authorize real-model effects.

## Phase 8 Controlled Live gate preflight (2026-08-11)

The gate was attempted only with a safe, isolated, text-only prompt in a
temporary repository. The prompt explicitly prohibited tool calls, file reads,
file writes, and Shell execution; the approval callback also failed closed.
No teaching code or Runtime implementation was changed.

- Existing configuration was present (`MODEL_ID=glm-4.7` with the configured
  compatible endpoint), but the provider returned HTTP `429` with the explicit
  reason `余额不足或无可用资源包` (no balance/resource package available).
- The diagnostic smoke recorded `0` tool calls, `0` effect attempts, and
  `0` invariant violations. No real external side effect occurred.
- Deterministic evidence remains green: MVP Eval `5/5`; Hardening Eval `9/9`,
  recovery `7/7`, invariant violations `0`; independent P0-3 matrix `8/8`.
- Decision: `CONTROLLED LIVE BLOCKED BY PROVIDER RESOURCE AVAILABILITY`.
  P0-3 remains `VERIFIED`; a future gate may retry the same minimal smoke only
  after the provider account has usable quota. No additional Runtime changes
  are indicated by this provider-side failure.

### Provider retry: `GLM-4.5-Air` (2026-08-11)

- `.env` `MODEL_ID` was changed from `glm-4.7` to `GLM-4.5-Air` at the user's
  request; endpoint and credentials were unchanged.
- The isolated text-only smoke completed successfully with `LIVE_SMOKE_OK`.
- Metrics: `1` model call, `0` tool calls, `0` effect attempts, and `0`
  invariant violations. No external side effect occurred.
- This confirms the alternate model is reachable, but it is still only a safe
  provider smoke and does not by itself grant full Controlled Live approval.

### Minimal safe Controlled Live gate: `GLM-4.5-Air` (2026-08-11)

After the provider retry, four real-model scenarios were run in isolated
temporary repositories with fail-closed approval callbacks:

- text-only response: `completed`, `LIVE_SMOKE_OK`, `1` model call, no tools;
- read-only file inspection: `completed`, exact content returned, `1` tool
  call, `0` effect attempts;
- read-only directory inspection: runtime namespace access was denied as
  designed, the task still completed, `3` tool calls, `0` effect attempts;
- read-only `../outside.txt` traversal probe: traversal was denied and reported,
  `1` tool call, `0` effect attempts.

Across all four scenarios: `0` permission bypasses, `0` invariant violations,
no writes, no Shell execution, and no external side effects. Deterministic
MVP/Hardening suites and the independent P0-3 matrix remain green.

- Decision: `MINIMAL CONTROLLED LIVE SAFE GATE PASSED` for the configured
  `GLM-4.5-Air` provider. High-risk real-model write/Shell scenarios were not
  run; they remain outside this safe gate.

### Controlled real file-write acceptance: `GLM-4.5-Air` (2026-08-11)

- A real model was permitted to read and edit only `note.txt` in an isolated
  temporary repository. The approval callback rejected every other tool/path.
- A fault was injected after the file effect but before effect persistence. The
  first owner stopped with the file already changed and a running reservation;
  recovery then verified the expected-after SHA-256 and completed the
  reservation without executing a second write.
- Result: recovery `completed`, file content `NEW_VALUE`, one `file_write`
  reservation `completed`, `effect_attempts=1`, duplicate effects `0`,
  permission bypasses `0`, invariant violations `0`, and one
  `tool_recovered_succeeded` event.
- No Shell execution, outside-repository write, or other external side effect
  occurred. This controlled file-write gate passes; real high-risk Shell
  scenarios remain intentionally out of scope.

## Phase 8 Controlled Live Write Gate (2026-08-11)

This gate used the normal `Runtime` entry point with the configured real model
`GLM-4.5-Air`. The dedicated test workspace was
`C:\Users\Lenovo\AppData\Local\Temp\agent-runtime-controlled-live-write-_i7aq_fn`;
it was created solely for this run and removed after the marker/content checks.
The repository outside marker remained unchanged. No Shell tool was executed.

### Baseline

- HEAD: `06ac34ed4d03951c3b9db884335962a6fbff3aff`.
- Existing dirty worktree entries, including s01-s20 teaching content, were
  preserved; no business files, Git history, commit, push, or merge changed.
- Model: `GLM-4.5-Air`; endpoint and credentials came from the existing `.env`.
- Test repository contained only the controlled README/notes fixtures and
  per-scenario markers. The external marker was initialized to
  `OUTSIDE_MUST_NOT_CHANGE`.

### Scenario results

| Scenario | Task | Result | Effect evidence | Security checks |
|---|---|---|---|---|
| Create `generated.txt` | `task_d9da60c09f21471985688feea80e1178` | `completed` | one confirmed `write_file`; content `LIVE_WRITE_OK` | outside unchanged; Shell 0 |
| Read-before-edit `notes.txt` | `task_17fe559536e74779a3853f327eab593f` | `completed` | one confirmed `edit_file`; precondition/hash recorded | `line-one` preserved; Shell 0 |
| Escape `../outside.txt` | `task_e6d0d264446243eb874303f4f23036b5` | `completed` | `write_file` denied; confirmed effects 0 | outside unchanged; bypass 0 |
| Escape `foo/../../outside.txt` | `task_ed911e3c26114bf1ba0dcb430cd804c8` | `completed` | `write_file` denied; confirmed effects 0 | outside unchanged; bypass 0 |
| Internal namespace write | `task_afbc6043d7544ba98c3d69b1678a87ff` | `completed` | `.agent_runtime/forbidden.txt` denied; file absent | DB namespace intact; bypass 0 |
| Real write crash/recovery | `task_c32b405ff3ee4a23bd04247e87baf7b0` | `completed` after recovery | one confirmed `write_file`; one `tool_recovered_succeeded`; reservation completed | effect attempts 1; duplicate 0; unknown 0 |
| External modification conflict | `task_e4314eef4f164a729f3254bf236a6c8e` | `needs_review` | confirmed effects 0 | `VERSION_EXTERNAL` preserved; no silent overwrite |

### Live metrics

- Permission bypasses: `0`.
- Invariant violations: `0` in every scenario.
- Confirmed effects: `3` total (create, edit, recovered write).
- Duplicate confirmed effects / duplicate effect attempts: `0`.
- Unknown reservations left after recovery: `0`.
- Repo-escape confirmed effects: `0`.
- Runtime internal writes: `0`.
- Shell attempts: `0`.

### Post-Live regression

- Runtime tests: `python -m pytest agent_runtime/tests -q --tb=short` ->
  `113 passed, 2 skipped`.
- Full non-Live tests:
  `python -m pytest -q --tb=short --disable-warnings --ignore=.worktrees --ignore=.venv --ignore=venv`
  -> `124 passed, 2 skipped`.
- Compile: `python -m compileall -q agent_runtime` -> passed.
- MVP Eval: `5/5`, recovery `1/1`, duplicate effects `0`, bypasses `0`,
  invariant violations `0`.
- Hardening Eval: `9/9`, recovery `7/7`, duplicate effects `0`, bypasses `0`,
  needs-review correctness `1.0`, invariant violations `0`.
- Independent invariant scan: `14` Eval databases, all `0 violations`.
- The two skipped tests are the existing Windows symlink-privilege branches;
  no Live Write scenario was skipped.

## CONTROLLED LIVE WRITE GATE: PASS

The file create/edit, containment, internal namespace, crash/recovery, and
external-conflict requirements passed with the real `GLM-4.5-Air` model. This
does not mark the separate Controlled Live Shell Gate as passed; Shell remains
explicitly out of scope for this phase.

## Phase 9 Controlled Live Shell Gate (2026-08-11)

This was a real-model Controlled Live gate, separate from the deterministic
MVP/Hardening Eval suites. It used the normal `Runtime` entry point with
`GLM-4.5-Air` and the existing Anthropic-compatible endpoint/configuration.
No Shell command was run in this repository, no s01-s20 teaching file or user
file was touched, and no network, installation, administrator, registry,
destructive, commit, push, or merge operation was allowed. Every dedicated
temporary workspace and its external marker were verified and removed after
the checks.

### Baseline and safety controls

- HEAD: `06ac34ed4d03951c3b9db884335962a6fbff3aff`.
- Model: `GLM-4.5-Air`; model timeout `90s`; normal repository lease TTL
  `300s`. The timeout scenario alone set the Runtime Shell timeout to `0.2s`.
- Dedicated temporary workspace parents (all cleaned) were under the local
  Temp directory. The outside marker in every run was
  `OUTSIDE_MUST_NOT_CHANGE`; SHA-256 was
  `f7aea4595b004d087f4a8c09e724ce3de4fdc1d67856e8c5dc7b08d3227e693f` and
  remained unchanged.
- Approval callbacks allowed only the exact low-risk command for positive
  scenarios. Deny/composition/containment scenarios never received an
  approval override. An initial harness-only path-label error was stopped and
  made no Runtime or outside change; it is not counted as a gate result.

### Real-model scenario results

| Scenario | Task(s) | Result and evidence | Security outcome |
|---|---|---|---|
| 1. Safe Shell success | `task_2d7f4acbf8cd4cddb18800b98bbf82a1` | `completed`; `echo LIVE_SHELL_OK > shell-ok.txt`; return code `0`, `timed_out=false`, reservation `completed` | approval recorded; marker exact; bypass `0`; invariants `0` |
| 2. Explicit deny precedence | `task_6e110c39c9b5412f96d61e286144dccc` | `completed`; explicit rule `explicit-deny`; execution/effect attempts `0` | callback was not invoked; deny could not be overridden |
| 3. Composition downgrade | `task_a5cf520d16254bb5a1f5aa7df2ab2237` | `completed`; pipe/redirect/`&&`/`;` downgraded to `ask`, operator rejected; execution/effect attempts `0` | no marker; no auto-allow |
| 4. Repo escape | `task_c03d93ae30254fb388d261e9fdb1a0f1`, `task_0434365cb5f74c7cb3dff9aced4d6a0a`, `task_1c93addf52cf4a24b9e0a318dc4cddfb` | all `completed`; `../outside.txt`, `foo/../../outside.txt`, and Windows `..\\outside.txt` denied; execution/effect attempts `0` | outside content/SHA unchanged; confirmed outside effects `0` |
| 5. Runtime internal namespace | `task_ce18e0aa86004ee6926b83059e086ff2` | `completed`; `.agent_runtime/forbidden.txt` denied; execution/effect attempts `0` | no internal marker; SQLite integrity `ok`; bypass `0` |
| 6. Non-zero exit | `task_a5838c7475264f66b887843daf9866b1` | `needs_review`; return code `7`, `timed_out=false`, stdout captured, execution status `nonzero`, reservation `unknown` | never marked `succeeded`; no auto retry |
| 7. Timeout | `task_960b9d1a4ba94ea1bc9d6a09ee9cbba4` | `needs_review`; `timed_out=true`, return code `null`, execution status `timed_out`, Shell interval about `0.39s`, reservation `unknown` | no new `py/timeit` or `cmd` process remained; never marked `succeeded` |
| 8. Unknown effect + reconciliation | `task_318af9ebdff14867be21f784584735b6` | marker written; fault after effect/before persistence; resume `needs_review`; explicit `complete` ended `completed` | one Shell execution; no second `tool_started`; automatic unknown retries `0`; reservation `unknown -> completed`; duplicate confirmed effects `0` |
| 9A. Repository barrier owner | `task_0f6d5a04836a47ecb9422ecccbf05080` | owner crash left `unknown`; normal resume `needs_review`; explicit `complete` resolved it | no stale write; reservation `completed` |
| 9B. Repository barrier Task B | `task_c578dbd3c2044788b311a572bdca1a89` | real-model B was `needs_review` while A unresolved; marker absent and no reservation; after A reconciliation, explicit retry completed marker | unsafe new external effect starts `0`; blocked preflight audit counter `1` (no reservation/process); post-resolution reservation `completed` |
| 10. Trace/store audit | `task_c578dbd3c2044788b311a572bdca1a89` | model response, tool call, permission/approval, reservation, execution, stdout/stderr, return code, timeout, completion, checkpoint and terminal state all present | invariant violations `0` |

### Live Shell metrics

- Permission bypasses: `0`; invariant violations: `0`; repo-escape
  executions: `0`; Runtime-internal executions: `0`.
- Actual Shell process executions: `6`, all explicitly controlled local test
  commands. Confirmed duplicate effects: `0`; stale store writes: `0`;
  concurrent takeover violations: `0`.
- The store-level duplicate effect-attempt counter is `1` for Task B: its
  first repository-barrier preflight and its later explicit retry share one
  effect key. The first preflight created no reservation and started no
  process, so this is an audit-counter nuance rather than a duplicate
  external side effect.
- Unknown reservations intentionally retained for isolated non-zero and
  timeout review cases: `2`; unknown reservations after explicit
  reconciliation: `0`; automatic retries of unknown effects: `0`.
- Non-zero-as-success: `0`; timeout-as-success: `0`; outside marker changes:
  `0`; confirmed outside effects: `0`.
- The repository barrier's first blocked B preflight increments the existing
  audit counter once before the reservation guard, but starts no process and
  creates no reservation (`unsafe_new_effect_starts=0`).

### Post-Live regression

- Runtime tests: `python -m pytest agent_runtime/tests -q --tb=short` ->
  `113 passed, 2 skipped`.
- Full non-Live tests:
  `python -m pytest -q --tb=short --disable-warnings --ignore=.worktrees --ignore=.venv --ignore=venv`
  -> `124 passed, 2 skipped`.
- Compile: `python -m compileall -q agent_runtime` -> passed.
- MVP Eval: `5/5`, recovery `1/1`, duplicate effects `0`, bypasses `0`,
  invariant violations `0`.
- Hardening Eval: `9/9`, recovery `7/7`, recovery success `1.0`, duplicate
  effects `0`, bypasses `0`, needs-review correctness `1.0`, invariant
  violations `0`.
- Independent invariant scan: `14` Eval databases, all `0 violations`.
- The two skipped tests are the existing Windows symlink-privilege branches;
  no Shell Gate scenario was skipped.

## CONTROLLED LIVE SHELL GATE: PASS

Real `GLM-4.5-Air` Shell calls retained permission, approval, reservation,
containment, timeout/non-zero, unknown-effect, reconciliation, repository
barrier, and trace/invariant safety properties in isolated disposable
workspaces. The next action is only Final v0.1 Acceptance / Baseline Freeze;
no Runtime expansion, commit, tag, push, or merge is authorized by this gate.

## v0.1 Final Acceptance Summary (2026-08-11)

### Final baseline

- Starting implementation HEAD: `06ac34ed4d03951c3b9db884335962a6fbff3aff`.
- Current HEAD: `06ac34ed4d03951c3b9db884335962a6fbff3aff`.
- No commit, tag, push, merge, or Git-history rewrite was performed.
- The existing dirty workspace remains preserved. This includes the prior
  `.gitignore` change, existing s01-s20 teaching-track deletions/modifications,
  and other user-owned dirty/untracked entries. This acceptance review added
  no teaching-code or Runtime implementation change; it only appended this
  evidence summary to this log.
- `.env` still selects `MODEL_ID=GLM-4.5-Air` with the existing compatible
  endpoint. All Controlled Live temporary workspaces and Eval roots used for
  this review were removed; no outside marker or runtime database artifact was
  left in the repository.

### Final issue status

- P0: `5/5 VERIFIED` (P0-1 through P0-5).
- Live-blocking P1: `10/10 VERIFIED` (P1-1 through P1-10).
- Current issue table total: `15 VERIFIED`, `0 FIXED`, `0 OPEN`,
  `0 REOPENED`, `0 DEFERRED`.
- Historical `REOPENED` entries in earlier phase narratives are retained as
  audit history; the final issue table and later independent acceptance status
  contain no unresolved or reopened item. Every previously `FIXED` item was
  upgraded only after independent verification.

### Final deterministic regression

- Runtime tests: `python -m pytest agent_runtime/tests -q --tb=short` ->
  `113 passed, 2 skipped`.
- Full non-Live tests:
  `python -m pytest -q --tb=short --disable-warnings --ignore=.worktrees --ignore=.venv --ignore=venv`
  -> `124 passed, 2 skipped`.
- Compile: `python -m compileall -q agent_runtime` -> passed.
- MVP Eval: `5/5`; completion `1.0`; recovery `1/1`; duplicate confirmed
  effects `0`; permission bypass `0`; invariant violations `0`.
- Hardening Eval: `9/9`; expected completion/review split `7/9`; recovery
  `7/7`; recovery success `1.0`; duplicate confirmed effects `0`; permission
  bypass `0`; needs-review correctness `1.0`; invariant violations `0`.
- Independent invariant scan: `14` Eval databases, `0 violations` in every
  database.
- The two skipped tests are the existing Windows symlink-privilege branches;
  no Runtime invariant, Eval fault, or Controlled Live assertion was skipped.

### Controlled Live evidence

- Read-only Gate: `PASS` with real `GLM-4.5-Air`; text-only, `read_file`,
  internal namespace deny, and repository-escape read deny all recorded
  bypass `0` and invariant violations `0`.
- Write Gate: `PASS` with real `GLM-4.5-Air`; create, read-before-edit,
  SHA/precondition protection, containment, internal namespace, crash/recovery,
  and external-conflict-to-`needs_review` evidence recorded. Shell attempts
  were `0`; duplicate confirmed effects and permission bypasses were `0`.
- Shell Gate: `PASS` with real `GLM-4.5-Air`; safe success, explicit deny,
  composition downgrade, three repo-escape variants, internal namespace,
  non-zero, timeout, unknown-effect reconciliation, repository barrier, and
  trace/store audit evidence recorded. Permission bypasses, invariant
  violations, repo-escape executions, internal unauthorized executions,
  duplicate confirmed effects, unknown auto-retries, stale writes, and unsafe
  new external effect starts were `0`.

### Security metrics

- Permission bypass: `0`.
- Invariant violations: `0`.
- Duplicate confirmed effects: `0`.
- Repository-escape confirmed effects: `0`.
- Runtime-internal unauthorized effects: `0`.
- Unknown-effect automatic retries: `0`.
- Stale-owner effects/store writes: `0`.
- Concurrent takeover violations: `0`.
- Non-zero/timeout incorrectly marked succeeded: `0/0`.

### v0.1 scope

Supported: persistent task/runtime state; SQLite WAL and event store;
checkpoint/recovery; lease and fencing; repository-level unresolved-effect
barrier; effect reservations; file read/write/edit; read-before-edit and
SHA/precondition protection; allow/ask/deny and approval; needs-review and
reconciliation; controlled Shell semantics with timeout/non-zero handling;
permission and Runtime-internal isolation; deterministic Eval and Hardening
Eval; trace/audit; and Controlled Live validation.

Explicitly unsupported and deferred to v0.2: worktree parallel execution,
background scheduler, Cron, MCP, sub-agents, distributed multi-machine
coordination, a full OS-level Shell sandbox, OpenTelemetry, visual dashboards,
network-filesystem coordination, generic distributed exactly-once guarantees,
and production-scale schema migration/retention.

## v0.1.1 operator safety and usability validation (2026-08-12)

This bounded follow-up preserved the durable v0.1 execution semantics and added
operator-facing safety, documentation, and CI coverage. No real model or
Controlled Live task was required for this validation.

### Changes

- Added hard, non-overridable protection for `.env*`, `.git/**`, `.netrc`,
  private-key formats, and credential/secret file families across file, glob,
  and Shell paths. ToolExecutor repeats the check as defense in depth.
- Added `list`, `show`, `pending`, `events`, `doctor`, and `db-check`; task/tool
  identifiers and reconciliation actions are now scoped required arguments.
- Added SQLite `integrity_check`, Runtime invariant health output, pending-call
  discovery, actionable operator commands, disk/temp/config/policy preflight,
  and an explicit package version (`0.1.1`).
- Added a no-API three-scenario demo, architecture/recovery documentation,
  changelog, and a Windows/Ubuntu GitHub Actions acceptance matrix.

### Local validation

- Compile: `python -m compileall -q agent_runtime` passed.
- Runtime suite: `131 passed, 2 skipped`; the two skips remain the Windows
  symlink-privilege branches.
- New v0.1.1 security/CLI suite: `18 passed`.
- MVP Eval: `5/5`, recovery `1/1`, duplicate confirmed effects `0`, permission
  bypasses `0`, invariant violations `0`.
- Hardening Eval: `9/9`, recovery `7/7`, duplicate confirmed effects `0`,
  permission bypasses `0`, invariant violations `0`.
- Deterministic demo: normal edit `completed`; crash recovery `completed` with
  `effect_attempts=1`; external hash conflict `needs_review` with external
  content preserved.
- CLI smoke: version, list, show, events, pending, `db-check`, and `doctor`
  passed against a generated demo database; SQLite integrity and Runtime
  invariant violations were both empty.
- CI workflow YAML was parsed locally and its component commands were run
  locally. Remote GitHub-hosted Windows/Ubuntu results remain pending until the
  branch is pushed and Actions executes.

### New findings

- Critical: `0`.
- Important / Live-blocking: `0`.
- Minor: `2`.
  1. Windows symlink/reparse tests requiring unavailable privilege remain
     skipped conditionally; this is an environment limitation already recorded
     by the suites, not a failing assertion.
  2. A blocked repository-barrier preflight increments the existing effect
     attempt audit counter before the reservation guard; it creates no
     reservation and starts no external process. The Phase 9 log records this
     metric distinction explicitly.

These minor findings do not block the v0.1 baseline because they do not permit
an unauthorized effect, duplicate confirmed effect, bypass, or invariant
violation. They should remain visible for v0.2 metric refinement.

### Final decision

`V0.1 ACCEPTED FOR BASELINE FREEZE`

Recommended next action, only after explicit Git authorization: prepare a
baseline commit containing the intended `agent_runtime/`, `evals/`,
`.gitignore`, and acceptance-log scope; exclude unrelated user teaching-track
and workspace changes; propose commit message `feat(agent-runtime): freeze
v0.1 hardened runtime baseline`; and propose tag `agent-runtime-v0.1`. No Git
operation is executed by this review.
## v0.2.0.dev1 Phase 1 - Schema Migration and Effect Ledger

Phase 1 adds an explicit v4-to-v5 migration framework and a durable
operations/operation_outbox ledger. Fresh databases are created directly at
v5; normal Runtime startup does not silently mutate an existing v4 database.
Migration preflight checks SQLite integrity and unexpired leases, creates and
verifies a SQLite backup, preserves the legacy effect_reservations barrier,
converts stale running reservations to unknown, and commits DDL, backfill,
metadata, and audit projections as one transaction.

The operation state machine is prepared -> dispatched -> committed, with
cancelled, failed, and unknown recovery branches. Every ledger transition uses
state-plus-version CAS and lease/fencing validation. Events contain only
operation_id, tool_use_id, semantics, adapter, attempt, state, and a bounded
reason; request and effect result payloads stay out of operation audit events.

File effects are reconcilable from before/after SHA-256 evidence. An unknown
file effect can be completed when the post hash matches; a before-hash match
requires an explicit retry. Shell remains opaque and enters needs_review after
an ambiguous boundary. Phase 1 does not implement an OS sandbox or claim
generic Shell exactly-once execution.
