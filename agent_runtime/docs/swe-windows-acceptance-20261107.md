# Windows SWE transport and single-instance acceptance

The native Ubuntu Docker Engine and Docker Desktop are distinct stores. The
original `sympy__sympy-19346` image was recovered, not rebuilt. Its original OCI
manifest identity is `c84f3478fc6e8268fbac170fd8fe5c0dc209dc1276aca66409645c250de5eb47`;
the Desktop classic store exposes config identity
`fc591c838948239b853df816d07332870597d72f1a54e6b86e8c21294308be42`.
Recovery verified the original manifest, configuration and all 14 layers.

## Implementation

- Docker SDK named-pipe shutdown closes both directions. Upload skips subsequent
  reads on that closed handle but still requires a completed exec with exit zero.
- Error 109 is normalized to EOF only for Docker SDK named-pipe socket classes.
  Frame completeness, bounded output, process exits and response validation remain
  the callers' responsibility. Other errors propagate.
- SWE verification has a bounded 180-second scope, included in verifier identity.
  Git is not launched after scope exhaustion; each Git command remains bounded.
- Snapshot diffing copies only files with changed bytes or executable modes into
  Git's comparison trees. The complete base remains available for patch checking,
  reconstruction and isolated probes.

## Evidence and scope

The real scripted candidate completed on the pinned Desktop Engine on 2026-11-06:
both frozen dict/set probes passed, a nonempty prediction was generated, and owned
source/validation containers were removed. The official v3.0.17 Harness at commit
`3f01bd622c0a22c00406139f69a234ef08225f22` subsequently reported this instance
resolved: FAIL_TO_PASS 1/1 and PASS_TO_PASS 40/40. Raw tests had 42 passes and two
exceptions (`test_Add`, `test_Mul`) outside those required sets. This is not a claim
of an exception-free test suite or autonomous model repair.

The grading controller used existing Linux Python 3.11 for preflight/report
validation and existing Python 3.12 for the exact official CLI. It did not exercise
`grade(execute=True)` as a single-environment integration. No provider calls,
dependency installation or image rebuilding occurred. Earlier failed attempts
remain preserved and are not counted as successful evaluations.

Local evidence directories relative to the workspace root:

- `.scratch/swe-recovery-20261105/`: original image recovery and identity bridge.
- `.scratch/swe19346-candidate-20261106-05-report/`: successful scripted candidate.
- `.scratch/swe19346-official-20261106-01/`: official run, raw logs and hashes.
- `.scratch/swe-closeout-review-20261106/`: review and new boundary checks.

Post-review checks cover truncated named-pipe EOF, deletion, binary additions,
file/directory replacement and executable-only changes. Windows: 5 passed, one
POSIX-only skip; Linux: 6 passed. Earlier targeted batches overlap and must not be
summed. Production implementation hashes match the successful candidate snapshot;
the post-review additions are tests, CI configuration and this document.

The incremental workflow now includes affected transport/capture tests on both
platforms. This is configuration coverage, not evidence of a new hosted CI run.
Publication and final-commit hosted CI remain separate steps.
