"""Rule-based online baseline: each idle agent takes the nearest feasible ready task."""
from __future__ import annotations

from ..models import TaskSpec
from .base import AllocationContext, AllocationResult, Allocator


class NearestFeasibleAllocator(Allocator):
    name = "nearest"
    mode = "online"

    def allocate(self, ctx: AllocationContext) -> AllocationResult:
        result = AllocationResult(objective=0.0)
        remaining: dict[str, TaskSpec] = {t.task_id: t for t in ctx.tasks}
        for agent in sorted((a for a in ctx.agents if a.available and a.idle), key=lambda a: a.agent_id):
            best = None
            for tid in sorted(remaining):
                spec = remaining[tid]
                pc = ctx.cost_model.pair(agent, spec, ctx.excluded.get(tid, set()))
                if not pc.feasible:
                    continue
                key = (pc.travel_cells, -spec.priority, tid)
                if best is None or key < best[0]:
                    best = (key, tid, pc)
            if best is not None:
                _, tid, pc = best
                result.assignments[tid] = agent.agent_id
                result.objective += pc.cost
                del remaining[tid]
        return result
