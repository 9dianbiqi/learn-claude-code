# Inspect offline adapter — local validation, 2026-10-22

> Publication follow-up (2026-10-23): this report preserves its original validation-time status. See [project closeout](project-closeout-20261023.md) for published commit/hosted CI status and the subsequent uncommitted integration. Historical “not committed/pushed” wording below is not the current publication state.

Status: local offline interoperability ACCEPT WITH NOTES. No new commit/push.
Branch: `codex/inspect-offline-adapter`, base
`cfd4017d4d769c82bbbfe5605151ab6e742d8e91`.

## Can the project enter Inspect AI integration?

Yes, for the offline interoperability stage defined by
[the milestone contract](inspect-offline-adapter.md). The predecessor release's
[CI run 39798958324](https://github.com/9dianbiqi/learn-claude-code/actions/runs/39798958324)
completed successfully on Linux and Windows at the expected cfd4017 commit.
This is an entry condition, not hosted acceptance of this new adapter.

The actual pinned `inspect-ai==0.3.283` package is used. Optional requirements
include the existing base requirements for clean environments; the core Runtime
dependency file and existing CI remain unchanged. A dedicated optional workflow
has been added, but has not run on GitHub because these changes are unpushed.

## Delivered and checked

- Externally pinned seal verification checks complete file inventory, every
  file digest, bounded portable paths and plan/report/result consistency. It
  rejects links/reparse points and enumeration errors, and preserves planned
  unknown/error cases rather than dropping them.
- Fresh output must be outside the input bundle. The adapter projects evaluated
  fields only, excluding gold patches, raw archived inputs and model prompts.
- Real Inspect Tasks, no-generation Solver, deterministic dictionary Scorer and
  `.eval` logs cover separate Runtime, Agent and environment denominators.
- Log read-back checks every sample, score, group, metric and empty model usage.
  The guarded mock fails on attempted generation. No new provider or harness is
  available through this adapter, and no model-graded scorer is configured.
- Source tri-state outcomes and strict rates remain in the sidecar. Inspect
  metrics are explicitly success lower bound, failure/unknown/known fractions;
  unknown samples do not disappear through an unscored/NaN mechanism.

| Validation | Result |
| --- | --- |
| Windows final focused suite, actual Inspect | 33 passed, 1 skipped |
| Linux clean environment final focused suite, actual Inspect | 34 passed |
| Windows core environment without optional Inspect | 25 passed, 2 skipped |
| Final real historical Agent + gold import | 2 Inspect group logs, 2 samples, read-back verified |
| Final existing Runtime example import | 1 Inspect group log, 3 samples, read-back verified |
| Normal import generation attempts / new provider calls / official runs | 0 / 0 / 0 |

The Windows platform skip is unavailable symlink privilege. The core-only check
additionally skips the optional integration module. Four deprecation warnings
come from the pinned Inspect retry implementation during deliberate negative
mock-generation tests; warnings are retained, not suppressed. Those tests attempt
local mock generation by design and assert rejection; they never contact a
provider. All normal archival imports show zero generation attempts.

Historical results retain their original meaning: the readonly Agent has three
archived requests (1,363 input / 128 output tokens), empty Patch and task failure,
with official resolved unknown. The gold run02 has official resolved=true only
in the environment denominator. The three earlier Runtime checks remain scripted
regression evidence. Importing them into Inspect did not execute them again.

## Failures retained and repairs

1. First Windows attempt: 1 failed / 26 passed / 1 skipped. The generation guard
   callback was passed through a nested model_args object that the installed
   get_model API did not consume. Passing custom_outputs directly and disabling
   memoization fixed it. The direct guard and unexpected-generation integration
   tests now exercise actual failure through the installed package.
2. First Spec review rejected output-inside-source and silently ignored directory
   enumeration errors. Both are now rejected before unsafe continuation, with
   source immutability and injected PermissionError regressions.
3. A clean Linux environment initially failed collection with missing mcp because
   optional installation did not include Runtime package prerequisites. Including
   the unchanged base requirements in the optional install file fixed it. The
   failing collection log and JUnit remain separate from the final passing run.

The first D/WSL dependency install completed slowly. Final Linux checks used an
independent native tmpfs environment with --no-compile. That environment and raw
temporary test fixtures are ephemeral and are not claimed to be archived; final
logs, JUnit and resolved package versions are preserved on D. Actual historical
and Runtime Inspect import logs are also on D. No C-drive cleanup was performed.

## Reviews and boundaries

Standards: 0 hard violations, 1 nonblocking duplicated protocol-mapping
maintenance note. Spec code repair review: ACCEPT WITH NOTES, no identified
remaining code blockers; final evidence review is stored with the evidence bundle.
Original rejection and repair history are retained.

Evidence directory:
`D:/opencv/learn-claude-code/.scratch/inspect-adapter-evidence-20261022`.
It contains source snapshots/diff, package versions, all test phases, predecessor
CI records, original-source digest references and the final Inspect logs/seals.

Inspect log success here means successful evidence interchange. It is not a new
Agent repair or SWE-bench result. Source seals require an external trusted pin;
hashes do not detect a wholly fabricated consistent archive or a malicious local
administrator. Transitive dependencies are recorded, not fully locked. No frozen
Phase 2 code, accepted read-budget candidate, or existing source evidence changed.

The next stage can wrap Runtime as an Inspect Solver only after its model,
container/tool bridge and budget contracts are frozen. Runtime/Verifier and
official Harness must retain their existing authorities. New paid model calls or
official evaluation still require separate authorization; this milestone does
not grant it.
