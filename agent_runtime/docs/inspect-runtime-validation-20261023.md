# Controlled Inspect Runtime-as-Solver — validation

> Publication follow-up (2026-10-23): this report preserves its original validation-time status. See [project closeout](project-closeout-20261023.md) for published commit/hosted CI status and the subsequent uncommitted integration. Historical “not committed/pushed” wording below is not the current publication state.

Local candidate status: ACCEPT WITH NOTES, 2026-10-23.
Started 2026-10-22; the evidence directory retains that start date.
Branch `codex/inspect-runtime-solver`, base
`de858a1dc57a8b5b9ec628dcf12ff706e4fe9b88`. This successor is not committed/pushed.

## Published predecessor and current scope

The sealed offline adapter was verified against its nine-file source manifest,
committed as de858a1 and pushed to `origin/codex/inspect-offline-adapter` with
matching remote SHA and a clean worktree. Its existing
[Runtime workflow](https://github.com/9dianbiqi/learn-claude-code/actions/runs/39806252698)
passed. The new [Inspect workflow](https://github.com/9dianbiqi/learn-claude-code/actions/runs/39806253045)
failed on both platforms because pytest's nested basetemp parent `.inspect_ci`
did not exist. That remote failure remains recorded; it is not relabeled PASS.

This candidate fixes that workflow setup and adds the controlled execution stage
defined by [the contract](inspect-runtime-solver.md). It changes no frozen Phase 2
code, base dependency file, core Runtime state machine or Verifier authority.

## Executable results

The actual pinned Inspect0.3.283 package drives Runtime model requests through a
synchronous protocol bridge on an AnyIO worker. Runtime executes tools, owns
permissions/leases, and commits verification through CompletionContract and its
existing DAG/Verifier ledger. Inspect logs actual mock ModelEvents. The bridge
does not run a second tool dispatcher or completion gate.

| Check | Authoritative evidence | Result |
| --- | --- | --- |
| Focused protocol/execution initial suite | windows-01-junit.xml | 26 passed |
| Corrected cancellation check | windows-cancel-03-junit.xml | 1 passed; worker joined before return |
| New-file diff and raw-ledger forgery checks | windows-audit-04-junit.xml | 2 passed; same logs audited without regeneration |
| Final actual workflow commands in fresh Windows layout | windows-ci-artifacts/junit.xml | 65 passed, 1 skipped |
| Final same commands in fresh Linux layout | linux-ci-artifacts/junit.xml | 66 passed |
| Additional valid CLI read-only goal (new case) | cli-verified-readonly/report.json | PASS, 2 mock calls, no workspace diff |
| Owned real container attempt04 | container evidence and probe-result.json | PASS, 4 Inspect events / 4 Runtime model records / 3 container exec reads |
| Final container audit | container-final-readback.json | PASS, read-only; no second container execution |

The final suites comprise only four focused Inspect suites, including the
existing importer under the newly repaired fresh-CI directory condition. They
are not a rerun of the old full Runtime, SWE-bench or provider experiments. Both
platforms used the exact initialization and pytest commands read from the updated
workflow, against fresh copies of the source. Windows' one skip is unavailable
symlink privilege; Linux executed it. The 35 retained warnings are upstream
Inspect/tenacity deprecation warnings, not suppressed failures.

Eight execution cases cover successful edit/feedback, unsupported early stop,
explicit give-up, finite durable turn exhaustion, unread-write review, typed
read-error feedback, lost unpersisted response and persisted-write recovery.
The lost-response case has four real Inspect mock events and four ledger model
attempts, including one abandoned response. Inspect usage is known synthetic
4/4; the Runtime confirmed subtotal is3/3 and its full total remains unknown.
The persisted write has one execution/effect confirmation. Neither case proves
general exactly-once behavior or OS-process-death recovery.

The additional CLI case verifies the actual module entry point with a new
no-modification task: the frozen goal is already satisfied, the model reads the
file, and the existing Verifier permits completion with an empty diff. This
does not repeat the eight-case execution corpus or treat every empty patch as
failure regardless of task type.

Negative tests also reject malformed plans/limits, out-of-scope tool requests,
unexpected native shell dispatch, unsupported temporary storage and mismatched
container capabilities/budgets. JSON response forgery, request forgery and
duplicate model records are rejected by re-auditing the same produced evidence.
File diffs include newly created native files. Model-construction failure retains
the plan, diagnostic report and seal before any model/tool request.

## Real container scope

The probe uses the cached immutable image
`sha256:d6cf11fb58af6b71dcda13f4c5d0c7bbceb036b09d968893b2570c1c53be72e2`,
UID65534, network none, read-only root, no capabilities or host mounts, and only
the existing DockerReadBridge's container read/discovery scope. A guessed path
exists as a host sentinel but is missing inside the container. Structured missing
path feedback reaches Inspect, discovery finds the actual implementation, and
the exact bound-container bytes satisfy the trusted Runtime verifier.

All three exec records bind the same owned source id; the sentinel never enters
model context. The owned container is removed after verification. The instance
is `inspect-container-read-fixture`, not a SWE-bench instance. A synthetic base
Git commit is created only within the disposable fixture, not the release branch.

Attempt04 has its own execution-source hash manifest. It was not rerun after
passing. Final read-only checks apply the final auditor to its sealed logs and
raw Runtime/bridge records, recording the auditor hash separately; this is not
claimed as another identical-source execution.

## Failures, repairs and retained limits

- The published Inspect CI basetemp-parent failure is repaired by an explicit
  evidence-directory initialization step, verified under fresh layout on both OSes.
- Early container logging attempts failed before generation. The diagnostic
  exporter also encountered a Python callback that was not JSON serializable;
  callbacks are now excluded from model-argument metadata after installation.
  The direct logging failure was separately traced to anonymous ZIP truncate on
  WSL's cross-drive temporary filesystem. A zero-model minimal probe reproduces
  failure there and success on native tmpfs. Preflight now rejects unsupported
  storage; the passing probe uses native temp with final evidence on D.
- Stronger semantic audits initially encountered unresolved attachment:// values.
  Read-back now uses Inspect's attachment resolution, then checks actual content.
- A cancellation fault test hung. Only its verified owned pytest process was
  stopped, and the incomplete result is retained without a fabricated JUnit pass.
  Explicit finite shielding/join and per-generation timeout fixed the regression.
  Cancellation is deferred; this is not a hard 90-second termination guarantee.
- The user freed C space after a measured zero-free-space condition. No cleanup
  of user files or accepted evidence was performed by the agent.

The final CI-layout raw regular files, logs, JUnit, package versions, source
manifests and Windows fixtures are archived on D. Linux's 30 symlink entries
(pytest aliases and deliberate negative fixtures) are omitted explicitly and
listed; their targets' regular evidence is preserved. Native Linux scratch may
disappear after WSL exits and is not claimed retained. No old accepted experiment
was repeated to generate a new benchmark score.

Standards: 0 hard findings, one nonblocking message-normalization duplication
note. Spec's two initial audit blockers were repaired; final evidence review is
included in the bundle. The new workflow has not run hosted at this candidate
SHA because this next node is intentionally uncommitted.

Evidence directory:
`D:/opencv/learn-claude-code/.scratch/inspect-runtime-solver-evidence-20261022`.
Normal execution uses only a local mock model; new provider requests and official
Harness runs are0. Mock tokens are explicitly synthetic. Agent task success and
official resolved remain null, regardless of regression-oracle success.

This node is suitable for a local experimental commit after final evidence audit.
It does not grant live-provider, container-write/test, official scoring or hosted
release acceptance. A future stage must separately freeze those capabilities and
budgets; this candidate does not authorize new paid calls.
