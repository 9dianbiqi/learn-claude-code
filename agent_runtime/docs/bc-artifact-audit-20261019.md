# Offline B/C CI artifact audit — next commit contract

Base: published `a1d38152dd591e903e3c2b1f0cac0fe96445b9cc`.
Branch: `codex/bc-artifact-audit`. Initial status: **IN PROGRESS**.

The previous node retained hosted B/C artifacts. This node independently checks
their consistency and rejects incomplete or contradictory evidence. It does not
execute an oracle, resume a task, repair the unknown old Windows CI failure, or
create a second Runtime completion authority.

## Contract

- Exactly one attempt lane, two scripted manifests matching the caller-supplied
  full CI commit SHA, and the complete duration/inventory × B/C result matrix.
- Original F3 exit 73 and fault marker, immutable prefix-request records and the
  continued manifest's hash link to the first manifest.
- Shared snapshot database/content hashes and recorded historical identities,
  consistently paired across B/C and continued runs. Copied file inodes are never
  treated as the original workspace identity; no SQLite execution is performed.
- Agreement between result rows, per-worker process/result files, private-check
  result flags, task IDs, trace invariants and actual scripted request counts.
- Missing, unreadable, oversized or malformed data cannot produce PASS. Explicit
  failures or contradictions produce FAILED; unavailable evidence is INCOMPLETE.
  Neither status exits successfully. Reports never overwrite old reports or write
  inside the input artifact tree.
- Bounded regular-file reads, static symlink/escape rejection, no commands taken
  from artifacts, and no model/API/provider/Harness calls. The input is a quiescent
  trusted CI download, not protection against a concurrent administrator rewriting
  files or a fully fabricated internally consistent package. Expected commit
  comes from independently checked CI metadata; manifests are not signatures.
- An always-run CI audit step emits its report under the existing artifact upload
  path. Tests/cleanup failures stay failures; audit PASS is only evidence
  consistency, never Runtime completion, Agent competence or SWE-bench success.

## Required evidence

1. New bounded malformed/contradictory/symlink/CLI fault tests, including no-input
   mutation and no report overwrite.
2. Audit the newly uploaded real Linux and Windows artifacts in place, without
   rerunning successful paired experiments. Preserve hosted CI logs and commit
   identity, audit output, and before/after hashes.
3. Inject faults into a copy of real hosted evidence and show that invalid data
   cannot pass, while original evidence remains unchanged.
4. Independent Standards/Spec review and frozen patch/source/evidence index.

No commit or push is made for this second candidate before the user authorizes
the next publication. No prior successful Docker, image, full-suite or paired
experiment is repeated locally. The first requested commit/push is tracked
separately on `codex/bc-pilot-ci-evidence`.

## Executed evidence — 20261019

The first requested publication completed as `a1d3815`, CI run `39147148936`:
Linux **624 passed**; Windows **618 passed, 6 Linux-only skipped**, fencing 30/30;
both platforms uploaded the new B/C attempt directory with four passing pilot
rows and four passing continued rows. This current diagnostic release is remotely
accepted with notes. The old `e47621f` failure stays failed in its original record;
its cause was not reproduced or established by the new successful run.

This second candidate performed no paired-process or model reruns. Its initial
new audit tests passed **24/1 skipped on Windows** and **25 on Linux**. An offline
probe copied the real hosted Linux package, injected six artifact faults (missing
process, failed process, changed snapshot, false private-check flag, wrong expected
commit, truncated matrix), and rejected each. Original input bytes stayed unchanged.
These are artifact mutations, not executions of those fabricated process failures.

Initial independent Spec review found a false-PASS bug: changing only cumulative
Trace model_calls was not rejected. Three new red cases (999, false, null)
reproduced it. The repair strictly checks Trace model_calls against prefix plus
continuation JSONL counts, separately from the parent's post-restart count.
Final new tests: Windows **27 passed, 1 symlink skipped**, 14.96 seconds; Linux
**28 passed**, including symlink rejection, 37.24 seconds.

Final-source audit read both original hosted packages in place: each PASS checks
eight result rows, six prefix requests and 24 continuation requests. On a copied
real Windows package, trace.model_calls=999 returned PASS with the retained
pre-repair auditor and FAILED/TRACE_MODEL_COUNT_MISMATCH with the final auditor.
The before/after index confirms both original packages' 100 files each unchanged.

Raw external artifacts under `.scratch/` include `bc-audit-hosted-fault-20261019-01`
(six pre-count-guard faults), `bc-audit-final-hosted-20261019` (final dual-platform
audit and real-data red/green), the two auditable drivers, all test logs/JUnit and
`bc-audit-before-count-guard-20261019`. Their source hashes identify the revision
used; initial results are not relabeled final-source combined runs.

## Local closure

Current status: **LOCAL NEXT-COMMIT READY — ACCEPT WITH NOTES**.
Standards: zero hard violations or new important maintenance findings.
Spec: ACCEPT WITH NOTES; the original REJECT and three red tests remain archived,
and the false-PASS blocker is closed. Both reviews were read-only.

The new CI audit step has not run hosted; the previous successful CI verifies the
published diagnostic release, not this second uncommitted candidate. This node
only certifies the stated quiescent-artifact checks, not fully forged packages,
concurrent adversaries, Runtime completion, Agent repair ability or SWE-bench.
Actual model API/token and Harness use for this offline node are **0**.

The four-file patch, final source, reviews and evidence inventory are frozen in
`.scratch/bc-artifact-audit-ready-20261019.zip` with a SHA-256 sidecar. No second
commit/push, main merge or accepted Phase 2 baseline modification was made.
