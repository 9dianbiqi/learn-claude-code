# Real Docker workspace backend — offline fixture round

Date: 2026-10-17. Status: **offline fixture integration passed; deployment review pending**.
Branch: `codex/completion-recovery-contract`, still based on accepted Phase 2
`68e4e51ca9cc4049f575d617762f89934e443baa`. No commits or pushes were made.

This round implements the concrete Docker freeze/export/validation seam left by
the previous completion-contract prototype. It does not rerun its 31 tests, the
legacy compatibility suite, API Smoke, old benchmark attempts or image builds.
Only the changed/new backend tests and new container fixtures execute here.

## What is implemented

`docker_workspace.py` supplies an explicit, bounded backend to WorkspaceVerifier.
The Runtime only gains a fenced `heartbeat_verification()` hook; the existing
Verifier, authority transaction, permissions and completion profile remain in use.

* Source ownership is checked by full container ID, immutable image ID and session
  label. Required conditions include UID 65534, private PID namespace, network
  none, readonly root, dropped capabilities, no-new-privileges, no host/volume
  mounts or credentials, and a controller-owned idle PID 1 (`sleep N`).
* A fixed Python program in the immutable image SIGSTOPs non-idle workspace
  processes and records their PID/start-time identities. It rejects pre-stopped
  processes instead of adopting another worker's freeze. PID 1 is separately
  validated as idle; same-namespace SIGSTOP cannot reliably stop namespace init.
* The trusted exporter runs with isolated Python imports and an 8-second alarm,
  exports ordinary files only, and declares the exported file count. The source
  then enters Docker cgroup pause. Its processes stay quiescent from capture
  through the existing SQLite authority commit. PID/start-time checks govern
  later SIGCONT; no unknown process is resumed.
* Archive traversal, links/special files, excessive member count, input/output
  sizes and unsupported data fail closed. Solver `.git` is ignored entirely.
* A controller-provided base tar is hash-pinned. Patch generation uses isolated
  Git configuration and no external diff/textconv; the patch must apply to the
  clean base. Validation input is reconstructed from that base plus patch,
  including new files, rather than copied from arbitrary solver Git state.
* Each frozen command probe executes in a separate non-root, network-none,
  readonly-root container, with memory/CPU/PID/tmpfs caps and no host mounts or
  credentials. Its actual exit, separate stdout/stderr, output oracle hash and
  elapsed time are retained. These are command probes, not a fabricated pytest
  case count or an official resolved result.
* A background heartbeat renews only the current fenced repository lease across
  export, validation and commit. Commands have frozen time limits, total export
  scope is bounded, and combined stdout/stderr is capped at 64 KiB. Owned validator
  containers and log readers are cleaned up on the examined failure paths.
* Lease loss rejects the stale verifier and deliberately leaves the source frozen
  for explicit recovery. The stale worker does not resume a replacement owner's
  workspace. Fixture-owner removal afterward is a separately recorded test cleanup.

## Real platform findings and retained failures

Execution uses the existing WSL Docker Engine **29.1.3** and retained local image
`sha256:d6cf11fb58af6b71dcda13f4c5d0c7bbceb036b09d968893b2570c1c53be72e2`.
No image was rebuilt or pulled. The image provides Python/Git; the actual test
repository is a new tiny fixture, **not SymPy or a SWE-bench repair task**.

Three failed attempts remain intact:

1. Attempt 01: Docker `put_archive` rejected writes because the root filesystem
   is readonly, even at a tmpfs destination. Replaced with a bounded, UID-65534 tar
   receiver over Docker exec stdin; readonly isolation was retained.
2. Attempt 02: Docker `get_archive` returned only the tmpfs mountpoint, omitting
   files that were visible inside the container. The incomplete candidate failed
   the trusted validation probe; it was not accepted as a successful snapshot.
   An independent diagnostic recorded identical omission while running and paused.
3. Attempt 03: the first quiescence program attempted to stop idle PID 1, leading
   to timeout. The source contract now requires an immutable idle init command,
   quiesces other actors, exports, then cgroup-pauses the whole source.

The diagnostic and failed logs are preserved. Source and validation containers
from those attempts were removed explicitly. No global prune/reset was used.

## New verification results

**12 backend unit tests passed.** They cover applicable patch reconstruction and
new files, ignored solver Git, traversal rejection, symlink/hardlink rejection,
pre-paused source refusal, PID namespace/init/credential rejection and upload
destination restrictions. After the export implementation changed, only these
affected backend tests were run again; old contract/baseline experiments were not.

The real-container fixtures use ScriptedModel and controller-authored changes.
Their reported Runtime completion is **fixture candidate verification**, never
model-generated repair success.

| New fixture scenario | Result |
|---|---|
| Successful candidate and later code drift | PASS; pause observed at F2-pre/F2-mid/F3; later resume rejects with EVIDENCE_STALE |
| Long probe with lease renewal | Probe sleeps 4 seconds with Runtime lease TTL=2 seconds; successful validation/commit; success-and-drift scope recorded 42 heartbeats |
| Command timeout | PASS; 0.6-second execution limit stops the 4-second probe, no authority checkpoint |
| Output bound | PASS; excessive output stops validation, no authority checkpoint |
| Lease takeover | PASS; stale owner cannot commit, replacement owner lease preserved, source retained paused |
| Active background writer | PASS; non-init writer stopped before export, pause held through commit, writer resumes afterward; later explicit drift again rejected |

The long-probe and first drift checks are one combined scenario, so this is
**5 distinct real-container scenarios**, not six independent trials. Attempt 04
passed the first four. Attempt 05 ran **only** the additional active-writer
scenario; successful scenarios were not repeated to fill the report.

Final query found no remaining containers bearing this round's source or
validation ownership labels. The fixture owner removed the intentionally frozen
source in the lease-takeover test after checking that stale code had not unpaused it.

## Evidence

Root: `D:/opencv/learn-claude-code/.scratch/docker-workspace-backend-evidence-20261017/`.
It contains new unit/JUnit results, attempt 01–05 records, the tmpfs diagnostic,
controller traces/databases, snapshot manifests and candidate diffs, frozen probe
configurations, validation stdout/stderr/exit/inspection, freeze/release records,
source snapshots and incremental/cumulative patches.

Actual passing runtime directories remain under `.scratch/docker-workspace-backend-20261017-04/`
and `...-05/`. Attempt 04's original driver/backend source snapshots were saved
before adding the extra driver case. The backend implementation is unchanged
between its passing scenarios and the final source snapshot.

## Remaining limits before live use

* The tested archive/worker protocol is Linux-specific and deliberately rejects
  links/special files. It has fixed transfer/frame/member limits; arbitrary large
  repositories or platform variants are not certified by this fixture.
* The source must have a trusted idle init. This is not an automatic replacement
  for every old solver container startup command. Docker endpoint ownership and
  source actor control are cooperative; this is not a distributed CAS against an
  arbitrary privileged Docker administrator or kernel adversary.
* A controller crash during quiescence/export/commit can leave stopped or paused
  source processes. Automatic frozen-container adoption is deliberately refused.
  Explicit recovery and partial thaw/daemon-failure paths need further fault
  injection before unattended deployment. This round did not repeat or extend
  the earlier process-exit suite to real Docker cgroups.
* The observed timeout/cleanup paths pass, but arbitrary Docker-daemon hangs,
  post-commit unpause failures and all socket failures are not exhaustively
  tested. Goal authority and cleanup outcome must remain distinguishable.
* Validation command probes are operator-frozen oracles. Their meaningful coverage
  depends on the frozen command specification; they do not become official SWE
  scores. Candidate code still executes only in the disposable validation sandbox.
* The current long probe validates lease renewal over seconds. A 600-second
  official harness job, async result delivery and task-type swebench_repair remain
  separately gated and were not run. Native Shell read failures also still need
  trusted typed-error integration before Recovery Advisor can consume them.

This round makes **0 real model API requests, 0 paid calls and 0 official harness
runs**. It provides no new SWE-bench resolved result or Agent competence evidence.
Accepted Phase 2 tracked files remain unchanged. No independent review is claimed.
