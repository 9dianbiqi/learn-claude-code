# Trusted container read/recovery bridge — next push candidate

Date: 2026-10-18. Base: published `74ef118c1b3ca44c45fe1115db8b091950f0182a`.
Initial status, retained as history: **in progress; NOT push-ready until real
Linux/container verification and incremental review complete**. Current disposition
is recorded in the dated gate-closure section below. The previously published
branch remains unchanged.

## Bounded milestone

Expose three read-only tools for the already isolated, controller-owned container:
`container_read_file`, `container_find_files`, `container_search_symbols`.
They must not read a same-named host file or fall back to native tools. Structured
PATH_NOT_FOUND must come from the trusted in-container program, not arbitrary
Shell stderr or JSON found in a repository file. A missing guessed path should
permit file discovery, literal symbol search and a subsequent implementation read.

This milestone tests the tool/control protocol with ScriptedModel. It does not
test real-model strategy improvement, generate an Agent patch or claim SWE-bench
success. No paid target-model calls or official Harness runs are authorized here.

## Implementation

`DockerReadBridge` uses the existing DockerWorkspaceBackend source checks and
Runtime fenced heartbeat. It sends a fixed isolated Python program as Docker exec
argv (UID 65534), never a host Shell command. Socket frames, output, read sizes,
search nodes/files/bytes, call count and execution time are bounded. Read-only
calls remain replay-safe tool records and do not create write operations.

Inside Linux, descriptor-relative opens use O_NOFOLLOW on every component and
O_NONBLOCK before rejecting non-regular files. Discovery skips symlinks, hardlinks,
Git/runtime metadata and the existing sensitive filename families. Reads return
the hash of exactly the bytes shown; changed file metadata during a read fails.
Search is literal, not user-supplied regex. Large/unsupported inputs fail explicitly.

The registry reuses its existing builtin storage kind with frozen
`controller_binding` metadata. It does not pretend Docker is an MCP server or
expand the database schema. Persisted metadata cannot recreate executable code:
restore leaves this adapter unbound until trusted `tool_setup` supplies it.

Runtime requires an explicit controller-only tool scope. A forged native read
call fails before host execution. The bootstrap profile binds tool names, schema,
source/image/session/engine/workspace/base identity, program/policy hash and scope.
Changed identity, absent live adapter or widened scope cannot resume a task.
Ordinary Runtime profiles without controller tools remain unchanged.

Configure these trusted tools before PermissionEngine loads the policy:

```python
bridge = DockerReadBridge(backend, fresh_read_trace_directory)
runtime = Runtime(controller_repo, model,
    tool_setup=bridge.install,
    tool_scope=bridge.names,
    recovery_advisor=RecoveryAdvisor(),
    policy_path=policy_allowing_only_these_three_tools)
backend.heartbeat = runtime.heartbeat_verification
```

The bridge's call cap is process-local. Durable task/model budgets still belong
to Runtime; this does not claim a cross-restart global tool quota. A controller
repository with these registrations cannot silently reopen as a host-tool task.

## Required evidence before the next push node

1. New Windows unit and affected registry/contract regressions.
2. Linux execution of the actual descriptor program, including symlink and
   protected-discovery cases; Windows skips are not substitute evidence.
3. `container_read_probe` on the existing local immutable image: missing path,
   file discovery, symbol search, exact implementation read; host sentinel remains
   invisible; credential/Git filtering; JSON-as-data; symlink refusal; loss of lease
   rejects before exec. This is a deliberately scripted diagnostic task.
4. Frozen traces, tool args/results, source identity, failure/pass logs and explicit
   target-model/Harness counters. No reuse of old successful container runs.
5. Incremental standards/spec review with no unresolved code blocker and the
   appropriate final compatibility checks after code changes.

## Current execution/environment notes

The first implementation attempted a new registry kind and hit the existing SQL
CHECK constraint. That development failure is retained; the design now reuses
builtin registrations without a schema change.

C: became full during testing. A subsequent pytest run aborted with ENOSPC and
is not a successful test result. All five modified source/test files were copied
byte-for-byte to a new D: worktree, on `codex/container-read-recovery`, without
deleting the C: copy or any accepted evidence. Tests now use D: temporary paths
and disable bytecode/cache writes. Affected Windows regression run recorded
**56 passed, 6 Linux-only skips**; the six skips still require Linux execution.

WSL currently fails to start with CreateInstance/E_FAIL while C: has no free
space. Archiving the newly migrated C: worktree also failed with ENOSPC. Automatic
approval review rejected the attempted cleanup of that worktree's generated
bytecode cache (`blocked by policy`); cleanup was not executed or bypassed.
Real-container acceptance remains blocked until the host environment recovers.

## Incremental review and latest Windows evidence

Standards review found zero hard violations and one nonblocking maintenance
suggestion (duplicate bounded Docker framing code). Initial Spec review returned
REJECT pending repair: disappearance of a listed child was mislabeled as a missing
request root. Two deterministic OS-protocol fault tests reproduced it.

The program now confirms the initial query root independently. Once scanning
starts, a vanished child returns REPOSITORY_CHANGED; it cannot become a false
PATH_NOT_FOUND for the still-existing root. The repaired code received a limited
ACCEPT WITH NOTES after read-only re-review, explicitly not a container acceptance.

Latest new Windows unit result: **23 passed, 6 skipped**. The six Linux descriptor
tests remain unexecuted. The full Windows suite that started before this program
correction finished **588 passed, 8 skipped**; that result is preserved as a
pre-correction compatibility run, not relabeled a final-tree combined pass.
The correction changed only the trusted program/error classification; its two
new fault tests were included in the later new-unit result.

Remaining push gates: actual Linux descriptor cases, the new real Docker fixture,
and evidence/review
closure for that exact candidate. No commit or push has been performed.

Post-correction affected compatibility confirmation subsequently passed:
**58 passed, 6 Linux-only skips** across the new bridge tests, existing completion
contract tests and tool-registry tests. This closes the available Windows check,
not the missing Linux/container gates. WSL also reported
CreateInstance/HCS_E_CONNECTION_TIMEOUT; the environment remains unavailable.

This document records a candidate and an explicit unmet gate, not a new acceptance
or a reason to downgrade the previously published version.

## Gate closure — 2026-10-18

Current status: **READY FOR NEXT PUSH NODE — ACCEPT WITH NOTES**. The previous
pending/environment record above is history, not the current disposition.
No completed Windows test, image build or old experiment was repeated to close
this gate. Only the two previously unexecuted groups ran after WSL recovered.

* The six Linux descriptor-program cases passed: regular read, missing file,
  symlink denial, protected discovery, literal symbol search and JSON-as-data.
  Result: **6 passed, 23 deselected**, 2.29 seconds.
* The new real Docker fixture passed. It records **5 ScriptedModel responses,
  4 Runtime tool calls and 6 real container exec reads**; these counts describe
  different layers and are not added together. A seventh direct bridge request
  was rejected for LeaseLost before any exec ID was created.
* The observed path was missing guessed file → file discovery → symbol search →
  exact implementation read in the bound source. The same-named host sentinel was
  not read; credentials/Git/runtime metadata were filtered; symlink access was
  denied; JSON file content did not forge a trusted error.
* Owned source cleanup succeeded and the post-run ownership inventory was empty.
  Engine and immutable image were the previously available local resources; no
  install, pull or rebuild was performed during this continuation.
* All seven frozen input files matched their source index before this dated
  documentation update. The six Python inputs remain unchanged after execution.

Standards final review: **0 hard violations, 1 nonblocking maintenance suggestion**
(shared framing implementation may be factored later). Spec/security final review:
**ACCEPT WITH NOTES; no unresolved code blocker for this bounded next-push node**.
Both reviews were read-only. Original REJECT, red tests, limited acceptance and
pending history remain preserved in their earlier archives.

Evidence: `.scratch/container-read-recovery-resume-20261018-01/` contains new Linux
JUnit/log, the Docker fixture's per-request nonce/source/exec records, Runtime
Trace, database and summary. The final local bundle is
`container-read-recovery-ready-evidence-20261018.zip`, with a SHA-256 sidecar.
It includes the earlier blocked evidence archive unchanged. Raw artifacts are
external to Git; the documentation is not a claim they have been uploaded.

Acceptance is limited to deterministic routing and trusted read/recovery semantics
in this controlled Linux/WSL Docker setup. Native Windows Docker transport, other
images/daemons, arbitrary concurrent writers and real-model competence are not
certified. `full06` remains the pre-last-program-correction full Windows contrast;
post-correction evidence is the targeted Windows checks plus these Linux/container
runs, not a newly executed complete final CI suite. Remote CI awaits an authorized
commit/push.

Target-model API calls **0**, official Harness runs **0**, Agent patch **none**.
The completed task is the fixture's read-only location goal, not a SWE-bench repair.
This continuation makes no commit or push and does not alter published `74ef118`.
