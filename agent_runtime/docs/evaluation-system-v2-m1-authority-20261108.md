# M1 second-review fixes: identity, verdict authority and execution scope

Date: 2026-11-08 (Asia/Shanghai). Fix base:
`323fec035aea8a281bab5c306fcdb2962535a6b6`.
Branch: `codex/evaluation-system-v2-m1`. This remains M1, not M2.

## Findings and corrections

| Finding | Correction | Checks |
| --- | --- | --- |
| P2: reported verifier A was silently replaced with validated verifier B | Oracle now distinguishes explicit identity from placeholder identity and records separate identity components. Authority rank cannot override a different explicit verifier ID or bundle hash. | Both input orders, IDs resembling placeholder names, and bundle conflicts despite missing outcome fields |
| P2: all verifier invocations failed but fixture goal still passed | A positive container fixture goal with supplied matching invocations must be supported by the latest persisted invocation: pass and authoritative=true. Explicit failure, uncertain or non-authoritative evidence contradicting success blocks projection. Missing outcome proof stays reported. | Fail, uncertain, non-authoritative pass, fail-then-pass, pass-then-fail/uncertain, missing fields |
| P1: official failures moved out of their official cohort | A bound, explicitly controller-declared real official attempt retains grading=official_harness even if the grading verdict is unavailable. Verdict and oracle authority remain blocked/unknown and unavailable. | One success plus two FAILED or STARTED subjects remains one N=3 cohort; strict rate null |

The last row is tested with synthetic evidence only. Its expected
success_lower_bound is 1/3, not a new SWE-bench success rate. No real grading,
model or container execution was performed.

## Identity and authority are separate

The V2 Oracle entity gains additive metadata:

- `identity_status`: explicit or placeholder;
- `identity_components`: recorded string fields, such as verifier_id and
  bundle_sha256.

These are not inferred from a name prefix. A verifier literally named
`v1-reported-container-verifier` is explicit when recorded in a binding. A
placeholder cannot carry explicit components or claim source_validated.
Known bundle identity is retained even when its invocation outcome is missing.

The same verifier may gain its missing bundle hash, even while evidence
authority stays reported. A matching literal identity may gain stronger proof.
An explicit different verifier, different known bundle or incompatible oracle
kind blocks aggregation. Merely assigning a greater authority rank is never a
reason to replace a conflicting identity. Distinct executions/repeats remain
independent; unknown aliases matching multiple executions remain ambiguous.

Historical reports are not deserialized and upgraded in place. Their exact bytes,
schemas and producer metadata remain preserved. V1 adapters provide the new
metadata when generating a fresh V2 report. V1 execution/output defaults and
Runtime DB schemas are unchanged.

## Outcome proof is objective-specific

The additional success gate applies to positive model fixture goals, not to
every regression pass. A Runtime regression whose frozen expectation is a
failed/uncertain verification may still pass runtime_regression. New tests cover
both fail and uncertain negative controls.

Invocation order follows the persisted verifier export; there is no best-attempt
selection or reconstruction of missing timestamps. Fail followed by authoritative
pass may establish success. A later fail/uncertain record cannot be ignored in
favor of an earlier success. Missing binding, invocation, bundle hash, outcome
or authority remains PARTIAL/reported as appropriate; known contradictions are
rejected rather than silently downgraded into a trusted pass.

## Official classification is not official resolution

Retaining the official cohort requires a sealed, pin-consistent prediction and
invocation, an original run ID, an attempted state (STARTED/FAILED/REPORTED), and
explicit controller declarations of official_harness and real_container. Matching
hashes still do not authenticate the author or establish physical execution.

A preflight does not gain execution attempts or official authority. A bare
official_resolved field, even alongside a scope declaration, does not establish
an official verdict. A simulated/undeclared physical execution is not promoted.
The existing full grading-report validation remains necessary for resolved.

The official cohort now retains failed/unresolved evidence without changing its
execution classification into an authority label. A missing verdict affects
blocked/unknown and strict completeness, not the already bound planned cohort.

## Validation

New file `test_evaluation_m1_authority.py`: 27 checks. The first seven tests were
run before implementation and all seven failed on the reviewed symptoms; their
original JUnit is retained as red.xml. The expanded final three-module batch is
**105 passed, no skips** (original 50 + first-fix 28 + new 27).

Nine related original offline modules: **165 passed, 2 skipped**, with 43
upstream Inspect deprecation warnings. Skips are Windows symlink-privilege
branches. These are selected regressions, not full Runtime/all Evaluation
acceptance. Exact commands and test hashes are in
`evidence/evaluation-v2-m1-authority-20261108/checks.json`.

All test temporary paths are on D:. No historical evidence or failed test
directories were cleaned. No packages were installed. The existing optional
Inspect environment was reused for related offline regressions.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:TEMP='D:\CodexTemp\evaluation-m1-fix-review-20261108-native'
$env:TMP=$env:TEMP
python -m pytest agent_runtime/tests/test_evaluation_contract.py `
  agent_runtime/tests/test_evaluation_m1_fixes.py `
  agent_runtime/tests/test_evaluation_m1_authority.py -q --tb=short -p no:cacheprovider `
  --basetemp=<fresh-D-directory> --junitxml=<new-D-junit.xml>

python -m agent_runtime.evaluation_v2 `
  agent_runtime/docs/evaluation-v2-m1-authority-input-20261108.json <fresh-output>

python agent_runtime/docs/evidence/evaluation-v2-m1-authority-20261108/verify.py
```

## New sealed report and preserved history

The new example is `../examples/evaluation-v2-m1-authority-20261108/report.md`
and report.json. It uses the same 13 existing-evidence entries as the first
fix report: eight supplied sealed bundles, 146 member checks across those
bundles, covering 129 distinct source files (one bundle is nested in another).
No synthetic official test results are inserted into this historical report.

The new report has 11 objective-specific observations and 53 unique persisted
attempt records. Importing the entire batch twice yields identical aggregates.
The historical batch has no contradiction between its supplied positive
container goals and authoritative verifier invocations.

Fresh projection of each earlier input preserves its previously recorded
aggregates (10/52 for original M1, 11/53 for the first fix). Earlier reports,
inputs, JUnit evidence and seals are byte-compared with Git at the fix base.
New producer fingerprints and oracle metadata are written only to the new
output, which has an independent seal.

Previous source-authentication, missing comparison configuration, unsupported
schema and missing latency/cost limits remain. The two original P3 maintenance
suggestions are still deferred. No PR was merged, published or changed to ready;
the original workspace's user edits remain untouched. This is a corrected M1
submission for review; it does not assert independent acceptance or model
competence beyond the recorded evidence.
