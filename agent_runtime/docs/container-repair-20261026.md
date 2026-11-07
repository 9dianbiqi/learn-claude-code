# Container repair capability — offline implementation

Follow-up: [bound SWE instance execution](inspect-swe-runner-20261027.md) connects
this capability to the bounded model runner and pinned dataset prediction export.
Backend provisioning and official scoring remain separate.

This successor adds `container_edit_file` to the existing bound-container read,
discovery and symbol-search tools. The model never receives native host tools or
a shell through this capability. The existing Docker transport still checks the
bound source, immutable image/session identity and Runtime heartbeat. It now
selects a class-owned program; normal reads keep their original program.

An edit requires a successful earlier read receipt from the same bridge, a
matching SHA-256, an existing bounded UTF-8 single-link regular file, and exactly
one occurrence of old_text. It writes a temporary sibling, fsyncs the contents,
rechecks the precondition and replaces the target. Symlinks/reparse points,
hardlinks, protected paths, traversal, oversized requests and stale content are
rejected. Receipt validation also checks nonce, path and before/after hashes.
Linux execution has a five-second alarm in addition to the bounded transport.

The workspace must have a single trusted writer. Path/hash checks do not provide
an OS-level fence against arbitrary concurrent container processes. A successful
reply is not a guarantee of crash durability on every filesystem. Existing-file
edits only are supported; creating/deleting files and arbitrary model-selected
commands are deliberately not exposed in this milestone.

Registry metadata explicitly declares controller_effect=unknown_write. The store
retains its prohibition on undeclared write effects masquerading as read adapters.
Runtime applies its existing external-effect reservation and ambiguous-outcome
review behavior, rather than using host file-write reconciliation. Read budgets
count only read-only controller tools. Controller binding/profile checks still
include both read and edit tools. The edit adapter's own attempt bound is local
to the bound object, not a newly introduced durable cross-process quota.

`repair_binding(backend, output, max_turns=...)` builds a trusted programmatic
`ContainerCaseBinding` using the existing backend's candidate-patch Verifier.
The caller supplies an already provisioned DockerWorkspaceBackend and owns its
cleanup. Use `execution_kind: container_repair` in an Inspect controlled case,
and pass the binding to `inspect_runtime_solver.run(..., bindings=...)`.
The verifier continues to freeze/capture/reconstruct/validate the candidate;
its success is not official SWE-bench resolved. No gold patch is a model input.

Offline verification uses a real subprocess for the edit program, a separate
fixture directory for simulated container contents, and fake Docker transport.
The integrated test uses real Inspect and Runtime, checks a host sentinel never
enters the model request, and verifies the write's unknown_write ledger effect.
A separate injected interruption after the effect but before persistence proves
resume returns needs_review and never invokes the edit a second time.

Evidence under repository-root `.scratch/`:

- `container-edit-tests-20261026.xml`: 11 passed, one initial integration failure
  from the store correctly rejecting an undeclared controller write.
- `container-edit-integration-20261026.xml`: only that failed integration rerun
  after adding the explicit effect declaration; passed.
- `container-edit-recovery-20261026.xml`: two new recovery/declaration checks passed.
- `container-edit-final-20261026.xml`: targeted final checks and binding factory.
- `container-edit-budget-20261026`: new read-budget membership check; edits do not
  consume the durable read-only quota. There are 16 distinct new checks overall,
  run incrementally rather than as a renewed historical suite.

This is offline capability acceptance, not a real Docker run. Previous accepted
experiments were not rerun. Still pending: new real-container edit/probe evidence,
general SWE repository provisioning/lifecycle, connecting live model evaluation
to the repair binding, and official scoring execution with authorized budgets.
The prior round's dataset preparation/export and live local-file model runner
remain separate. No new provider calls, official evaluations, remote publication
or main merge occurred in this round.
