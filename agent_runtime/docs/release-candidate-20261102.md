# SWE successor release candidate — 2026-11-02

Status: **LOCAL_REVIEWED_CANDIDATE; NOT PUBLISHED; REAL ACCEPTANCE PENDING**.

## Baseline and scope

The baseline is PR #19 head `4b872495a72a88f17d44acc532183bc0b46fac72`
in `9dianbiqi/learn-claude-code`. Its recursive Git tree was read through the
connected GitHub app. The seven changed pre-existing files were fetched at their
blob IDs and their Git object hashes were independently verified locally.
Local HEAD remains `c3e1192`; its working-copy status is not the release diff.

The candidate contains 50 files: 7 modified, 43 added, including this note.
It layers onto the PR head, not directly onto main. The older closeout workflow,
fencing fixture and published validation notes are unchanged and excluded.
Only LF/CRLF equivalence is ignored when classifying differences; archived
candidate bytes retain their SHA-256 identities.

Included: evaluation suite/report and dataset interchange, bounded Inspect model
entry, container repair binding, SWE instance runner/environment/grading/acceptance,
ordinary-user script, asset preparation/base export, synthetic test fixtures,
manual incremental workflow and accompanying docs/tests.

Excluded: local host diagnostics `swe-host-blocker-20261030.md` and
`swe-host-access-recheck-20261030.md`; all scratch archives, raw experiment evidence,
actual datasets/evaluator oracle bundles, exported bases, local response scripts,
credentials and unrelated teaching/research changes. Test sentinels in unit fixtures
are synthetic; they are not benchmark oracle data.

## Final difference review

- Container write registration remains an explicit `unknown_write` controller
  effect. Existing read tools and budgets keep their read-only scope. Ambiguous
  writes require review rather than automatic replay.
- Container identity/base matching and prior-bound-read checks remain on the
  repair path. Candidate verification and official grading retain separate outcomes.
- Missing real assets emit a draft; script preflight happens before container
  provisioning. CLI execution requires an explicit Docker endpoint/Engine ID.
- Grading uses a clean pinned Linux harness, preserves the full prediction
  denominator and records timeout/cleanup uncertainty. No official score is claimed.
- Optional Docker/Parquet requirements remain separate from core dependencies.
  The manual Windows/Linux workflow includes all 13 successor test modules;
  its hosted execution and final commit validation remain pending.
- Checked relative Python imports, candidate config hashes, Python syntax,
  workflow references and patch application against the exact baseline files.
  These are static assembly checks, not new runtime performance evidence.

## Fixes from this review

1. Base export previously caught only ordinary exceptions. KeyboardInterrupt or
   SystemExit could leave only the earlier journal and omit result.json. It now
   writes a BLOCKED receipt containing source/session/Engine identity and
   recovery-required state, then propagates cancellation without retry or cleanup.
2. Socket timeout configuration previously occurred outside the close-finally
   scope. It now occurs inside, so configuration errors also close the transport.

The exporter suite passed **14 checks**: 5 new interruption/transport regressions
and 9 affected existing checks. Docker transport is simulated. Initial collection
attempts using system Python and an old offline environment failed because mcp
was absent; the successful run used the existing project .venv, without installs.
Historical results retain their original source scope and are not added to this
count. No completed experiments, paid calls, real Docker or official runs were made.

Hard process termination or storage failure cannot guarantee a final receipt.
Use invocation.json and the latest event journal when result.json is absent.
An uncertain create is located by the recorded session label on the pinned Engine;
a known source ID must also be rechecked against its labels/image before recovery.
This release adds no automatic recovery/delete command.

## Packaging and remaining gates

The local release package contains an explicit file/hash manifest, candidate.zip,
candidate.patch and REVIEW.md under the release-candidate-20261102 review output.
Use the manifest allowlist, never an entire-worktree upload. The baseline files
and raw JUnit receipts stay outside candidate.zip. Previous historical documents
may name local evidence paths; those evidence files are deliberately excluded.

Remaining: real base.tar export and frozen repair responses; real candidate
execution and lifecycle confirmation; clean Linux official harness execution;
final-commit Windows/Linux CI and publication. General image building/dependency
installation and autonomous-model performance remain separate.
