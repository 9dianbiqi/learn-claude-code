# Opt-in durable controller read attempt cap — next commit contract

Base: published `ba5a242019ec411e5138fa5ba028b529dd66c546`.
Branch: `codex/container-read-budget`. Initial status: **IN PROGRESS**.

The existing DockerReadBridge cap is process-local by design. This opt-in node
adds a task-local limit for Runtime-dispatched controller read attempts, using
existing tool_calls.execution_attempts. It adds no database/table migration,
no second budget ledger, and no alternate completion authority.

## Contract

- `Runtime(controller_read_attempt_limit=N)` accepts integer1..64 only with bound
  controller-only tools and an existing frozen CompletionContract. The default
  None preserves existing profiles and behavior. This is not a host-tool quota.
  Persisted identity metadata alone is insufficient: exposed adapters must be
  callable at construction when this opt-in policy is enabled.
- Policy and controller tool set are frozen into the bootstrap Runtime profile;
  removing/expanding the budget on resume fails before model/adapter calls.
- The existing fenced BEGIN IMMEDIATE transaction atomically checks the task's
  sum of started attempts across the configured controller read names and starts
  the next attempt only below the cap. Distinct tasks get distinct budgets.
- Failed reads and interrupted starts consume attempts. A start reserved before
  a crash is never refunded, even if no Docker exec occurred. Unpersisted reads
  need another attempt on replay; persisted successful/error results are reused
  without another charge. This is a conservative cap, not exactly-once or a cap
  on trusted controller/verifier direct API calls outside Runtime dispatch.
- Exhaustion rejects before adapter/Docker dispatch, records a distinct
  CONTROLLER_READ_BUDGET_EXHAUSTED reason, then uses the existing contract failure
  transition so both task and final plan item fail with budget_exhausted=true.
  No forced retry or auxiliary model call is added.
- Existing write review, permission scope, model/turn budgets, leases/fencing,
  recovery and schema remain intact. No widening to multi-agent/shared quotas.

## Acceptance evidence

1. Synthetic replay/dedup/error/cross-tool/task-scope/profile/invalid-config tests.
2. Atomic competing starts and stale-owner rejection at the actual Store seam.
3. Fresh real Docker process death cases: unpersisted read cap1 rejects replay;
   cap2 permits exactly one replay; saved result at cap1 deduplicates; started but
   not dispatched at cap1 conservatively exhausts. New owner/fence required.
4. Real changed-limit restore rejected before model/read calls, host sentinel
   invisible, source-specific exec records, correct task/plan outcomes and cleanup.
5. New and affected tests, final-source compatibility checks appropriate to core
   changes, independent Standards/Spec review, frozen sources/traces/counters.

These four cases add new budget behavior, not repeats of old accepted read-only
fixtures. Use only existing pinned local image/SDK. No install/pull/build, real
model API or official SWE-bench Harness. No candidate commit/push yet; no main or
accepted Phase2 baseline changes. The prior requested publication is separate.

## Additional hosted failure evidence

The first requested publication `ba5a242` passed full tests and the new artifact
audit on both platforms, but Windows fencing iteration10 failed. Its stack names
owner-b/fence2 during finish_model_call, not the deliberately stale owner-a. The
fixture gave owner-b a wall-clock lease of0.5 seconds. A controlled0.8-second delay
at that exact boundary reproduces owner-b LeaseLost with the original test.

This candidate also corrects that fixture's clock: Store time is fixed only within
the test, while owner-a is still explicitly expired via the existing SQL helper.
Original30/0.5-second TTL values, fencing tokens, assertion that owner-b completes
and owner-a gets LeaseLost, and bounded thread cleanup remain. The same injected
delay now passes. Production clock, lease durations, retry counts and permission
semantics are unchanged. This explains the new fencing failure seam, not the older
unrelated B/C failure. No remote rerun erased the failed iteration.

The budget probe's four fresh process-death cases have passed, with owned sources
removed. The later clock-fixture-only change does not affect their production
inputs. Their original complete source index is retained, including the old test
version, rather than relabeled a final-tree execution.

## Recovery repair and final checks

Initial unit run: **17 passed**. Independent Spec review then found a failure
window after a tool's budget-denial row was committed but before the Task/Plan
failure transaction completed. Four red cases reproduced loss of the typed budget
reason: saved denial, after-item, after-checkpoint and before-commit rollback.
The recovery branch now validates the stored budget code/maximum/consumed against
the frozen policy, restores typed exhaustion and reuses the original contract
failure transaction without dispatch or recharging. The four cases passed, and
the combined unit run passed **21 tests**. Malformed-record validation is also
reviewed statically; no separate malformed-budget injection result is claimed.

Only two new real Docker cases ran after this repair: actual exit73 after the
denial row, and exit73 inside the failure transaction. Both recovered with the
budget reason, failed Task/Plan, unchanged count1, zero extra model/read calls,
new fencing and zero remaining containers. The original four successful cases
were not repeated. Their old source index and pre-repair source are preserved;
the new two have their own frozen index.

Full-suite results before the final live-binding configuration guard:

| Execution | Result | Scope |
|---|---|---|
| Windows before typed-denial repair | 660 passed / 9 skipped | preserved earlier compatibility result |
| Windows after typed-denial repair | 664 passed / 9 skipped | 435.94 seconds; explicit test clock fix included |
| Linux on DrvFs | 7 failed / 260 passed, interrupted | short1-second fixture leases expired before fault points |
| Same seven Linux failures on native temporary FS | 7 passed | 16.30 seconds; no deadline/TTL change |
| Linux complete on native tmpfs | 673 passed | 154.70 seconds; process recovery, not power-loss storage certification |

The DrvFs run was interrupted after the short-lease failures became clear and is
retained as an interrupted failure, not a pass. The first Linux invocation never
ran tests because DrvFs anonymous capture-file truncation failed. Linux capture
then used native memory temp files. C: exhaustion caused earlier WSL E_FAIL and a
download-temp failure; the user freed space, and output/download/test records use
D:. A narrow test-only Git wrapper maps only `git rev-parse HEAD` at this Windows
worktree to its Linux metadata paths, leaving other Git operations unchanged.

The native full run's temporary B/C raw tree was absent when archival was
attempted, so **its raw B/C tree is not included**. Its completed log/JUnit remain;
the new budget experiment's six original D: traces/databases/read records remain
complete. The interrupted DrvFs run's separate B/C data are not relabeled native.

Final configuration review found one further gap: stored controller metadata with
no executable rebind was accepted by the budget constructor. A new red test
reproduced it. The final guard checks callable adapters only for the exposed scope
and only when budget is enabled. Guard-after affected checks (22 budget tests,
existing bridge and completion-contract tests): Windows **69 passed / 6 skipped**,
26.63 seconds; Linux **75 passed**, 7.14 seconds. Full suites and six Docker cases
retain their before-guard/source-stage labels. This narrow pre-model guard does
not warrant repeating those passed execution cases.

## Local closure

**LOCAL NEXT-COMMIT READY — ACCEPT WITH NOTES**. Standards: zero hard violations,
one nonblocking duplicated experiment-cleanup suggestion. Spec: accepted with
notes; initial REJECT and red tests remain preserved, both identified gaps closed.

The seven-file candidate includes the opt-in policy, Runtime/Store integration,
probe/tests/report and the deterministic fencing fixture correction. No second
commit/push or main merge was made. First publication `ba5a242` remains remotely
CI FAILED at fencing iteration10; local correction does not retroactively accept
that run, and the older B/C failure cause remains unknown.

Evidence bundle: `.scratch/controller-read-budget-ready-20261019.zip` and SHA-256
sidecar. It separates all source stages and environment failures, records six
budget cases, the controlled fencing red/green and final targeted compatibility.
New budget experiment model API/tokens and official Harness runs are **0**. This
is not an autonomous patch or SWE-bench result, a direct-controller RPC quota,
an inter-task shared quota, or strict exactly-once execution.
