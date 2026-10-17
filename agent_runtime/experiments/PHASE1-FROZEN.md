# Phase 1 Recovery Context Evaluation — frozen

Freeze date: 2026-10-07. Runtime baseline: `ebb308e50a01ffc4edc945f47fad890c65bc610c`.

- C remains the recovery-context research baseline; this does not change the
  production Runtime's default configuration.
- C-time did not pass the predeclared 32-cell screening expansion gate and is
  retained as a negative/paused candidate. Do not append 80/120 runs to chase a gain.
- C-receipt is retained as an inactive candidate.
- The final screening was independently accepted with notes by a fresh
  GPT-6.1 Sol medium reviewer. Both conditions passed 16/16 tasks; C-time used
  178,039 vs 157,604 input tokens and 91 vs 83 model calls. Input improved on
  1/8 tasks and reobservation intensity on 2/8. Verification skips were zero.

Evidence archive (local, no remote upload):

`D:/opencv/learn-claude-code/.scratch/phase1-frozen-20261007/phase1-evidence.zip`

SHA-256: `5b2cee7dd5d1f7f5a0bc528865fe5d3ef624a962ed826adabe34e22fc63a984f`

The sibling `manifest.json` records sizes and SHA-256 for all 2,032 members.
Every archive member was read back and verified. It includes experimental
scripts, prompts, private oracles, source snapshots, original requests/results,
failed/confounded batches, raw independent reviews, and test evidence. Original
directories remain intact; their status distinctions are not flattened.

The archive preserves evidence bytes, not original NTFS inode identities.
Unzip-and-resume is not an approved reproduction method; future restoration
must respect the recorded file observations and experiment workspace contract.

Next work is Phase 2 Runtime Correctness & Crash Consistency. It starts from
the frozen Runtime and uses deterministic fault/interleaving cases. It does not
reopen Phase 1 efficiency tuning or expand the model evaluation cohort.
