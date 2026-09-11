---
status: accepted
---

# Preserve Plan Dependencies with Immutable Revisions

A Plan keeps a stable identity and advances by atomically appending immutable
PlanRevision snapshots while PlanItems remain the current execution
projection. Dependency edges are satisfied rather than deleted, and structural
changes use CAS PlanPatches; this preserves recovery history and rejects stale
writers without importing or extending the teaching TaskManager.
