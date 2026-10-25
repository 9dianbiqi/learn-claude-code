# B/C paired recovery pilot

This opt-in experiment compares Verified-Full (B) with Verified-Scoped (C)
after a real process termination at `verified_subtask_f3`. It is a synthetic,
fixed-plan **pilot**, not SWE-bench, not a long-horizon benchmark, and not an
estimate of population success rates or non-inferiority.

## Protocol v4 (identity-preserving snapshots)

- Two small Python tasks: duration parsing plus settings integration, and
  inventory parsing plus aggregation. Each has two frozen dependent subtasks.
- A real model executes each common prefix. The first verified completion
  invokes `os._exit(73)`, without Python cleanup. The supervisor verifies the
  exit code and fault record. A prefix that never reaches F3 is reported.
- After the child exits, SQLite checkpoints its WAL. The supervisor waits for
  natural lease expiration; it does not edit runtime state or bypass leases.
- B and C restore the byte-identical database and files at the **same absolute
  workspace path**, sequentially. Archives preserve each continuation. Orders
  are B/C for duration and C/B for inventory. Source hashes, snapshot hashes,
  model requests and responses, traces and results are retained.
- V4 preserves source-file identity with hard links on the same filesystem;
  runtime databases are ordinary independent copies. This is valid only for
  this pilot's atomic-replace file tools, not shell or arbitrary in-place
  writers. Before each run, persisted file observations must match actual
  content hashes and file identities. After each run, source snapshot hashes
  must remain unchanged. Archives should be treated as read-only because
  unchanged files may still share hard links with the original snapshot.
- A `FullHistory` experiment adapter explicitly disables the runtime's default
  context projection before the crash and for B. C uses the production scoped
  resume implementation with a 16,000 estimated-token bundle budget. The
  Runtime includes the F3 empty-batch fix and a persisted `input_ready`
  continuation prompt after non-final verified markers, equally for B and C.
- Both arms expose the same four file tools. No shell, subagent or MCP tools
  are sent to the model. Runtime metadata paths remain protected. Permission
  prompts for these file tools are automatically accepted by the experiment.
- Verifier code and reference implementations are outside the agent workspace.
  The final evaluator runs again in a fresh Python process and checks both
  contracts. It uses the same assertions as the subtask checkers; it is an
  independent invocation, **not** additional unseen coverage. Python `-I` is
  import isolation, not a security sandbox for arbitrary untrusted code.
- A trajectory has at most 32 model calls including the shared prefix. Stop
  before the next call once recorded input (including cache reads/creation)
  plus output tokens reach 150,000;
  one response may cross this threshold. Each phase has a 600-second timeout,
  a 90-second request timeout, SDK retries disabled, and max output 8,000
  tokens per call. Model version is the configured provider alias, temperature
  uses its default, and no claim of deterministic model sampling is made.
- Actual provider input/output usage is measured. Raw provider usage is also
  retained for cache accounting. No monetary saving is inferred without an
  applicable price schedule. Scripted checks never report invented tokens.

The common prefix is charged to each arm's hypothetical total trajectory but
was paid for only once in this paired experiment. Post-restart tokens include
the remaining coding work and must not be called pure recovery overhead.
Time from the crash through lease expiry is shared setup time; reported resume
seconds begin in the restarted runtime. Provider startup is in process timing.

## Running

From this worktree, with the repository dependencies installed:

```powershell
python -m pytest agent_runtime/tests/test_bc_pilot.py -q
python -m agent_runtime.experiments.bc_pilot --mode scripted --output <new-output-directory>
python -m agent_runtime.experiments.bc_pilot --mode live --env-file <existing-env-file> --output <new-output-directory>
python -m agent_runtime.experiments.bc_pilot --mode live --env-file <existing-env-file> --resume-from <prior-run-directory> --output <new-output-directory>
```

Live mode makes billable requests to the configured provider. Output directories
must be new; previous results are not overwritten. The manifest records model
and endpoint, never credentials. Full traces may contain task source code.

## Interpretation and limits

Primary observations are final contract pass, runtime completion, invariant
violations, provider token counts, and the first actual request's context.
Unchanged completed files provide a narrow check, not proof of zero duplicate
side effects. A prefix failure is not a B/C recovery failure. Do not omit failed
arms, reuse successful results as retries, or merge protocol versions.

One shared prefix and one continuation per arm per task cannot establish a
statistically significant difference. These tiny tasks may make scoped
metadata larger than full history; that is a valid result. Subsequent work
should freeze more complex tasks, repeat continuations, add an uninterrupted
control, and only then integrate a SWE-bench adapter.

## Development record (2026-10-04)

Protocol v1 reached neither task's F3 boundary: the live model combined a
completion marker with a tool call, which the runtime correctly rejects.
Protocol v2 clarifies that a completion marker must be a text-only response.
Both v1 attempts remain in the first run directory. They are protocol debugging
data and are not pooled with v2 recovery comparisons.

Protocol v2 reached F3 on both tasks but all four continuations were rejected
by the live API because Runtime appended an empty user tool-result message.
On 2026-10-05, v3 continued the preserved pre-error snapshots with the fixed
Runtime, without regenerating either prefix. `--resume-from` requires identical
task/verifier hashes, mode, model alias and endpoint; it restores each snapshot
at its original absolute workspace path. Original failed attempts remain intact.
Model aliases do not guarantee an immutable provider version.

V3 also counts cache input tokens and corrects the invariant-count field name.
Do not directly compare v2's uncached-input field with v3's total-input field.
See [the completed pilot report](reports/bc-pilot-20261005.md).

## Five-pair replication

`bc_repeat` freezes a 20-cell schedule before execution: two existing snapshots,
five continuations per arm per snapshot. Odd repetitions use B/C on duration
and C/B on inventory; even repetitions reverse both orders. Each worker uses
the same v3 prompts, runtime and budgets. No prefix calls or automatic retries
are made. Original pilot results are not pooled with these new runs.

```powershell
python -m agent_runtime.experiments.bc_repeat --env-file <existing-env-file> --resume-from <original-F3-run-directory> --output <new-output-directory>
```

The frozen `protocol.json` defines observable event categories. An exact
completion marker followed by a colon is a format error, not a missing marker.
Text without a marker is recorded as a candidate missing-completion event;
the classifier does not infer intent. Repeated reads and reads of completed
files are observations, not proof of wasted work or duplicate side effects.
All responses and traces are retained for explaining individual trajectories.
Only two distinct tasks are represented, regardless of the 20 continuations.

The first 20-cell repetition batch exposed a restoration confound: ordinary
copying changed file identity, so one B continuation that wrote without a new
read was correctly stopped by the runtime's conflict guard. The whole batch
is retained and labeled confounded, not just its failed cell excluded. A
second batch uses v4 restoration with exactly the same runtime, tasks,
prompts, classifier, schedule and budgets. No runtime safety check was relaxed.
