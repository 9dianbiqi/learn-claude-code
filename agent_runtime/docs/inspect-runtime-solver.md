# Inspect Runtime-as-Solver — controlled execution contract

Base: de858a1dc57a8b5b9ec628dcf12ff706e4fe9b88, 2026-10-22.
This successor executes the actual Runtime inside Inspect instead of replaying
archived outcomes. Only a locally constructed Inspect mock model is enabled.
It is a wiring/correctness milestone, not autonomous repair or SWE-bench grading.

## Required behavior

1. Commit/push the accepted offline adapter unchanged before developing this
   successor. Preserve its seals and frozen Phase 2 source. Observe its remote
   CI separately; do not call a queued job accepted.
2. A custom Inspect Solver must invoke real Runtime.run/resume, and the Runtime
   model adapter must call real Inspect Model.generate. Preserve text, tool ids,
   arguments, tool results/error flags, stop reasons and explicit usage. Unsupported
   content must fail rather than silently disappear. Model/tool schemas must
   retain their existing meanings; Inspect does not execute Runtime tools.
3. Run synchronous Runtime on an AnyIO worker thread; marshal generation back to
   the active Inspect event loop/context. Keep one model call per Runtime model
   attempt, no hidden generate loops or retries. Use an explicit fresh mock model
   per case, not .env defaults or provider fallback. No provider or official run.
4. Completion must reuse CompletionContract(verified_task) and the existing
   VerifiedSubtaskDAGConfig/Verifier ledger. Bind frozen file goals or a trusted
   controller verifier. Persist max_turns (1–16), finite stop checks, and recover
   exactly once after an explicitly injected durable-boundary exception. Never
   mark a stopped, budget-exhausted, declined or unverified task completed.
5. Restrict CLI fixture tools to read_file/write_file/edit_file inside fresh
   per-case workspaces. Keep native permission/read-before-write behavior. A
   trusted programmatic seam can install the existing DockerReadBridge with
   container-only scope; do not recreate that bridge or fall back to host files.
   Verify an actual bound-container read/discovery loop with a host sentinel and
   immutable image using a separate offline probe, not an official instance run.
6. Freeze/validate a bounded plan before creating output. No empty/duplicate
   cases, unsafe/alias paths, unbounded scripts, noninteger limits or fixture
   collisions with internal state. No overwrite or output inside source inputs.
   Never send verifier goals/checks as model context. Trusted local fixtures are
   not an operating-system sandbox for arbitrary code or third-party models.
7. Keep Runtime DB/checkpoints, unredacted synthetic model/tool records, raw Trace,
   durable bridge-call receipts, diffs and Inspect .eval logs linked by case/task/
   model-call identities and hashes. Distinguish generated-but-unpersisted Inspect
   responses from Runtime-confirmed usage after injected crashes; do not erase
   abandoned attempts or invent missing tokens. Mock usage is explicitly synthetic.
8. Read actual .eval logs back and match samples, model attempts, per-case outcomes
   and raw Runtime records. A regression oracle can pass for an expected failed
   Runtime; this is not Agent task success. Provider usage/official resolved are
   inapplicable/unknown; new provider calls/official runs are zero. Seal outputs
   and preserve diagnostic artifacts on failure, not only success.
9. Verify conversion/errors, normal tool feedback, recovery advice, early stop,
   write review, finite budget, denied/outside-scope requests and persistent
   recovery through the actual Inspect package. Complete dual-platform focused
   regression, the real Docker bridge probe and independent Standards/Spec review.
   Add this suite to the optional Inspect CI; no paid calls or official Harness.

## Plan and CLI

`python -m agent_runtime.inspect_runtime_solver PLAN.json NEW_OUTPUT_DIRECTORY`

The schema is `inspect-runtime-plan-v1`: run_id and 1–8 cases. Each case has id,
prompt, fixture files, goal_files, model response entries, max_turns and checks
(expected Runtime status and final files). An optional supported fault_point is
injected once; responses are consumed in order across recovery without resetting
budgets. A response may require an exact expect_feedback substring in the next
request. That assertion is a fixture check, never an extra model request.

Scripts are at most 16 entries and 64 KiB per case. Goals name 1–8 fixture files;
their exact contents are evaluator-owned, not added to Runtime's user prompt or
Verifier description. The model still receives the actual problem prompt and
tool feedback. Mock usage is explicitly fixed per response unless supplied by
the bounded fixture. Runtime's own durable turn limit is authoritative. Inspect
also has finite sample time/token bounds; no cancellation abandons a worker
thread to continue mutating a workspace invisibly.

Cancellation is deferred until the finite Runtime worker finishes, with a
separate 15-second cooperative deadline around each mock generation. Inspect's
90-second observation limit is not advertised as a hard process-kill deadline.
Registered Solver arguments exclude controller capabilities and hidden goals;
the frozen plan/hash is the replay recipe. Python callbacks remain installed on
MockLLM but are removed from its JSON model-argument metadata. Inspect log retry
alone is not a supported replay mechanism for these controller-owned fixtures.

The .eval recorder needs an anonymous temporary file that supports ZIP append and
truncate. The runner probes this before execution. Some WSL cross-drive temp
directories fail that operation; use a native TMPDIR (e.g. private tmpfs). Final
logs may still be stored on D. No silent filesystem/provider fallback is used.
Log read-back resolves Inspect's internal attachment references before semantic
comparison with Runtime requests and confirmed responses.

CLI exit 0 means the bridge/log processing succeeded; inspect the regression
aggregate for acceptance. Valid evidence of an unresolved Runtime is not an
adapter malfunction. Invalid plans exit 2 before tools run; bridge/log failures
exit 1 with diagnostic output. The Docker probe is a separate trusted controller
experiment with no automatic image pull and cleanup limited to its owned source.

## Remaining authority and risks

Inspect logs record real mock ModelEvents and the converted final conversation;
Runtime remains authority for tool execution, permission checks, task state,
Verifier commits and checkpoints. Bridge receipts add audit linkage, not a second
recovery ledger. Exception injection is not OS process-death testing. Native local
file fixtures prove integration only; only the separate container probe exercises
container routing. A successful fixture/file diff is not an official patch score.

The public CLI cannot select a live provider or attach arbitrary trusted tools.
A future live stage must separately freeze provider/model, external budget,
container tool capability and official scorer provenance. This milestone does
not authorize such calls and must not be described as that stage passing.

Inspect APIs: [custom Solvers](https://inspect.aisi.org.uk/solvers.html) and
[Model API](https://inspect.aisi.org.uk/reference/inspect_ai.model.html).
