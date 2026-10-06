# Historical committed-effect pilot v1 — pre-execution contract

Scope: 2 original F3 snapshots × C/C-time/C-receipt × 5 repetitions = 30
new continuations. No new prefix, no change to runtime ebb308e, task/verifier,
completion-marker parser, tools, permissions, model alias or existing budgets.
This is an experimental condition, not Epic #13's product C2 diagnosis/router.

## Exact intervention

`receipt_projection.py` freezes TIME_NOTE and RECEIPT_NOTE byte-for-byte.
Render tests check C messages are unchanged; C-time only appends TIME_NOTE to
the existing scoped system message, with all other messages unchanged.
C-receipt equals C-time until an eligible effect exists, then appends the
historical receipt payload and its fixed interpretation note. No new roles,
dynamic budget, decision summary, verifier execution or prohibition on reads.
No current_hash field or source-file probe is added by receipt projection.

All conditions execute the same read-only ledger query for instrumentation.
Only C-receipt exposes the receipt. This keeps observation overhead consistent
and distinguishes evidence available in the runtime from evidence delivered
to the model. Database transactions and projection latency are reported,
rather than treated as free. Provider input counts include cache reads/creation.

## Receipt fields and provenance

```yaml
operation_id: existing committed operation id
tool_call_id: linked successful tool call
path: relevant task-relative path
effect_type: file_write
committed_after_sha256: persisted post_hash_match evidence
bytes_written: UTF-8 write_file argument length, checked against the persisted hash
commit_check: post_hash_match
evidence_checkpoint_id: first execution checkpoint containing that tool result
observed_at: persisted tool finished_at, recorded after the write/hash check
freshness: historical_committed_effect
```

For edit_file, bytes_written is null (not recorded reliably); projection does
not read the file to fill it. evidence_checkpoint_id is **not** the transaction
that atomically committed the effect: the current runtime saves the tool-result
execution checkpoint afterward. The fixed model-visible note states this.
The projection log separately records view_checkpoint_id and boundary_checkpoint_id;
these do not refresh the historical evidence timestamp.

Eligibility requires committed operation, succeeded file-write tool, confirmed
effect, post_hash_match evidence, valid SHA-256, a related path, and a post-boundary
execution checkpoint containing the tool result. Pre-boundary receipts are
excluded. write_file content must hash to the persisted digest. A single SQLite
read transaction pins the read view. Pending/unknown/failed/cancelled effects
are omitted, never promoted to committed.

This confirms a historical effect only. External drift does not change that
past fact, and does not cause this projection to assert a current hash. Existing
file conflict, stale evidence, CAS, safe replay and verifier gates remain active.
We cannot guarantee a model's interpretation merely by testing a prompt, but
we can guarantee the rendered claim and that runtime authority is unchanged.

## Bounds

Expose at most the most recent eligible receipt, ordered by completed_at
descending then operation_id. Payload cap: 2,000 JSON characters. Deterministic
omission preserves complete records and reports omitted_count; no partial
receipt is sent. Rendered full context remains subject to the original 16,000
estimated-token limit and fails closed on overflow. No silent message truncation.

## Design and execution

The three conditions rotate across the five repetitions, offset by task;
across ten task/repetition blocks each condition occupies each position three
or four times. The complete schedule and source hashes are written before the
first call. Existing identity-preserving snapshot restoration and per-arm
integrity checks are reused. Each process has a 600-second cap; provider request
timeout 90 seconds, SDK retries disabled, max output 8,000 tokens. Model-call
and cumulative token budgets include the original prefix as before (32 calls,
150,000-token stopping threshold; one response may cross the threshold).

No interim-result-driven changes, exclusions, prompt tuning or retries. Failures,
missing usage and zero-denominator cells remain visible. Existing pilot and both
20-cell batches are background only; they are not pooled with the new C control.

## Outcomes

Primary descriptive outcomes: final verifier pass/completed status; cumulative
input and output; model/tool calls; post-write read requests; invariant failures;
parent runtime source-file read opens; projection latency; resume latency.

The proposed “avoidable verification ratio” is operationalized without assuming
avoidability:

- **Reobservation intensity** = read_file requests linked to an eligible
  committed write / eligible committed write operations with a subsequent model
  decision opportunity. May exceed 1 if a write is read multiple times.
- **Reobserved-operation fraction** = distinct eligible writes read again /
  eligible writes. This is bounded by 0–1.
- **Exposure-conditioned intensity** uses only delivered receipts and read
  decisions at which that receipt was visible. It is N/A for C and C-time.

Each read links to the latest eligible committed write on the same path known
at the preceding decision point. A later write ends the previous write's window.
These are request counts, not assumptions of successful/necessary/unnecessary I/O.
Report runtime-available and model-exposed denominator counts separately. Do not
infer reduced need for verification solely from a lower ratio.

Host I/O instrumentation counts parent-process read opens of workspace data
files during resume, excluding .agent_runtime, setup and final grading. Verifier
child-process/import I/O is not included; verifier invocation counts are recorded
to expose unequal schedules. Projection-added source reads and database read
transactions are separate. Do not claim total OS I/O or byte-level savings from
these counters. Offline tests prove receipt construction does not open source files.

## Offline gates and interpretation

Tests cover committed receipt/no drift, external drift/history unchanged,
prepared/dispatched/unknown/failed/cancelled effects, pre-boundary/unrelated
effects, complete-record truncation, overall overflow, exact condition deltas,
real runtime resume under all three conditions using a fake provider, and
intensity > 1 versus bounded operation fraction/exposure N/A. No model billable
calls are used in these tests. Original pytest output is saved.

A fresh GPT-6.1 Sol medium reviewer must review this contract, implementation,
and raw test output before live execution. A separate fresh reviewer evaluates
the resulting raw records afterward. Acceptance of this pilot does not establish
population significance or long-horizon generalization. C-receipt vs C-time
tests an increment on top of time labeling; it cannot isolate the hash itself
from extra text or salience, or retrospectively prove why earlier models reread.
