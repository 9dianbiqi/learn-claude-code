# Long-Horizon Agent Runtime

This context names the planning layers used by the durable runtime while
keeping the teaching harness concepts distinct.

## Planning language

**Todo**:
An ephemeral checklist entry used by one agent session to organize immediate
execution steps.
_Avoid_: Task, PlanItem

**Runtime Task**:
One durable agent execution created for a user goal and recoverable across
interruptions.
_Avoid_: Todo, PlanItem

**Plan**:
The stable identity that owns the evolving decomposition of a Runtime Task and
points to its current PlanRevision.
_Avoid_: PlanVersion, task list

**PlanItem**:
A durable unit of work inside a Plan whose readiness is determined by its
status and dependency edges.
_Avoid_: Todo, child Task

**PlanRevision**:
An immutable snapshot of a Plan's PlanItems and dependency edges at one point
in its history.
_Avoid_: PlanVersion, mutable plan

**PlanPatch**:
A typed request to derive a new PlanRevision from an expected current
revision.
_Avoid_: plan rewrite, dynamic plan

**Dependency edge**:
A durable relation stating that one PlanItem requires another; completion
satisfies the edge but does not erase it.
_Avoid_: mutable blocker list

**Ready PlanItem**:
An active PlanItem whose dependency edges all point to completed PlanItems.
_Avoid_: unblocked task

**Tombstoned PlanItem**:
A PlanItem retained in revision history but excluded from the current Plan.
_Avoid_: deleted PlanItem
