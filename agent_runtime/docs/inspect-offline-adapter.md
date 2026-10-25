# Inspect AI offline adapter — milestone contract

Date: 2026-10-22. Base: cfd4017d4d769c82bbbfe5605151ab6e742d8e91.
This milestone is read-only interoperability, not a live solver migration.

## Entry decision and scope

The sealed evaluation-v1 interface and local regression results are sufficient to
start optional Inspect integration. The prior push's CI was queued at entry and
subsequently completed successfully (run 39798958324, commit cfd4017). This new
adapter has not been pushed or hosted-tested. No frozen Phase 2 source,
Runtime completion/Verifier authority, model provider or official harness changes.

Inspect is optional and pinned separately in `agent_runtime/requirements-inspect.txt`.
That optional install file includes the existing base requirements so a clean
environment also has the dependencies imported by the Runtime package initializer.
The adapter must run against the actual installed package, not mocked substitute
classes. Core tests remain usable without that dependency.

## Acceptance requirements

1. Read completed evaluation-v1 bundles without rerunning the evaluator, Runtime,
   tools, model or official harness. Require an externally supplied seal digest,
   verify every sealed file and exact inventory, reject traversal, links/reparse
   parents, duplicate entries and oversized inventories. A self-consistent hash
   manifest without a trusted external pin does not establish provenance.
2. Verify plan/report/result consistency, case identity/order, separate kind and
   mode constraints, tri-state outcome types, planned denominators and aggregates.
   Imported processing errors remain visible as unknown, not silently dropped.
3. Map nonempty Runtime, Agent and environment groups to separate real Inspect
   Tasks and Samples, a no-generation Solver, deterministic Scorer and .eval logs.
   Never include gold patches, raw model prompts/responses, credentials or raw
   archived input bytes in Inspect Samples. Project only evaluated result fields.
4. Preserve null semantics and all samples in the denominator. Report explicitly
   named success lower bound, failure, known and unknown fractions. A lower bound
   is not a strict success rate. Preserve strict rate (possibly null) and source
   aggregates in the sidecar. Do not use unscored/NaN to drop unknown cases.
5. Run only a locally constructed mock model whose generation method fails if
   invoked. Count actual attempted generation, enforce zero; do not consult .env
   for provider selection. No model-graded scorer or sandbox is supplied. Source
   usage remains historical metadata, separate from zero new requests/tokens.
6. Use a fresh output directory, freeze projected inputs and provenance before
   Inspect evaluation, retain errors/logs, read back actual .eval samples and
   metrics and verify they match every expected source case. Seal final outputs.
   Inspect execution/log success is not Agent task success. Do not overwrite a
   prior run, or present imported historical results as new experiments.
7. Add regression/fault tests for tampering, malformed/inconsistent bundles,
   unknowns, mixed denominators, projection, accidental generation, output reuse,
   and real Inspect end-to-end log round trips. Exercise real prior historical
   evidence offline, preserve tests and independent Standards/Spec review.

The dedicated `Inspect Offline Adapter` workflow installs the optional dependency
and runs the same focused suite on Linux and Windows, retaining synthetic logs.
The existing core Runtime CI and dependency requirements are unchanged.

The CLI is `python -m agent_runtime.inspect_adapter BUNDLE NEW_OUTPUT
--seal-sha256 DIGEST`. Install the optional requirements first. A full import
with valid logs exits 0 even when archived Agent tasks failed; it is interoperability
success. Invalid bundle input exits 2 before output creation; adapter/log failure
exits 1 and keeps diagnostic evidence. No new API/harness execution is authorized.

## Interpretation and trust

For a source group of N samples, successes S, failures F and unknown U:
`success_lower_bound=S/N`, `failure_fraction=F/N`, `unknown_fraction=U/N`,
`known_fraction=(S+F)/N`. Strict success rate is S/N only when U=0, otherwise
null. An absent group produces no Inspect task and retains the source zero-count,
null-rate aggregate. Gold controls never enter the Agent denominator.

The external seal pin must come from an accepted evaluator receipt, not simply
be recomputed after untrusted modification. This is trusted local evaluation
interchange, not protection against a malicious administrator, forged source
archives, or concurrent filesystem tampering. Hash inventories are not signatures.
The adapter verifies evidence consistency; it does not independently establish
repair correctness or regrade official tests. Original sealed bundles remain the
authority for complete traces and patch evidence, linked by digest rather than
copied into the model-facing Inspect transcript.

Inspect reference documentation checked 2026-10-22:

- [Tasks](https://inspect.aisi.org.uk/tasks.html): Task/Dataset/Solver/Scorer composition.
- [Scorer API](https://inspect.aisi.org.uk/reference/inspect_ai.scorer.html): dictionary-valued scores/metrics;
  unscored samples can be excluded, so this adapter uses explicit unknown counters.
- [Log files](https://inspect.aisi.org.uk/eval-logs.html): .eval output and read_eval_log API.

The next live Runtime-as-Solver stage requires separately frozen container/tool
bridge, model/budget limits, trace linkage and official scoring receipt. This
milestone neither bypasses those gates nor authorizes new paid calls.

The exact Inspect package is pinned; transitive dependencies are not a complete
lockfile. Local validation records resolved package versions. Negative tests
intentionally attempt local mock generation to prove the guard fails closed;
normal bundle imports must have zero generation attempts and all paths have zero
provider requests. No mock success is evidence of Agent competence.
