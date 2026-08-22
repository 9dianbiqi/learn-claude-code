# Phase 5: Lane-aware Lease + Worktree

## Status

Planned for v0.3. This phase changes repository ownership from a single global
lease to a repo-plus-lane lease.

## Objective

Allow isolated execution lanes within one repository, primarily for worktree
or subagent execution, while keeping effect reservations, permissions, and
operations scoped to the correct lane.

## Reused teaching modules

| Module | What is reused | What changes |
|---|---|---|
| `s18_worktree_isolation` | worktree path binding | worktree becomes durable and lane-aware |
| current `store.py` lease | repo-level fencing | key becomes `repo_root + lane_id` |

## Scope

### In scope

- Add `lanes` and `worktrees` metadata.
- Change lease identity to `(repo_root, lane_id)`.
- Scope effect reservations to lane identity.
- Bind file/read/glob safety to the lane's resolved root.
- Migrate existing single-lane databases to a default lane.
- Add lane collision and cross-lane fence tests.

### Out of scope

- Distributed filesystems.
- Generic cross-repo lock manager.
- OS sandbox.
- Multi-repo worktree orchestration.

## Schema direction

```text
lanes
  lane_id
  repo_root
  lane_kind
  owner_task_id
  created_at
  updated_at

worktrees
  worktree_id
  lane_id
  repo_root
  path
  branch
  status
  created_at
  updated_at
```

Existing lease rows must be backfilled to the default lane during migration.
No existing single-lane behavior should silently change semantics.

## Lease boundary

```text
acquire_lease(repo_root, lane_id, task_id, owner_id, ttl)
heartbeat_lease(repo_root, lane_id, owner_id, fencing_token, ttl)
release_lease(repo_root, lane_id, owner_id, fencing_token)
```

Two tasks may run in different lanes of the same repo. Two tasks may not run in
the same lane simultaneously.

## Effect reservation boundary

Every effect reservation must record its `lane_id`. The unknown/owner-crashed
recovery path must resolve effects against the same lane. A lane lease release
must not release another lane's reservations.

## File safety boundary

`ToolExecutor` resolves paths against the active lane root. `read_file`, `glob`,
`write_file`, `edit_file`, and shell execution must all use the lane root.
Repository-level protected paths remain protected in every lane.

## Acceptance criteria

- Migration preserves existing databases as the default lane.
- Same repo, different lanes can run without fencing each other.
- Same lane cannot be acquired twice.
- Cross-lane effect reservation writes are rejected.
- Worktree paths are contained within the assigned lane.
- Existing fencing and effect tests pass.

## Verification

```powershell
python -m pytest agent_runtime/tests -q
python -m pytest agent_runtime/tests/test_phase6_p0_3.py -q
python -m agent_runtime db-migrate --repo <sandbox> --dry-run
```

## Handoff

### Complete before Phase 6

- Lane-aware leases and worktrees exist.
- Effect reservations and file tools are lane-scoped.
- Migration/backfill and cross-lane tests pass.

### Phase 6 entry state

Phase 6 can apply retention, GC, observability, and sandbox policies to
multiple lanes without redesigning the lease boundary.

### Known deferred boundaries

- Cross-repo lane coordination is deferred.
- Git worktree branch merge policies are deferred.
- OS-level path isolation remains a Phase 6 sandbox item.

