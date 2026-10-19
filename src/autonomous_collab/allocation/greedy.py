"""Greedy, route-aware sequential allocator (batch baseline from the project guide).

Tasks are visited in a deterministic topological order (prerequisites first; ties: higher
priority, then task id). Each task goes to the feasible agent with the lowest *incremental*
cost computed from that agent's virtual position/battery/load after its earlier picks.
Tie-break: lowest agent id. Optimal for nothing in particular - it is a baseline.
"""
from __future__ import annotations

from dataclasses import replace

from ..task_graph import topological_order
from .base import AllocationContext, AllocationResult, Allocator
from .cost_model import explain_unassigned


class GreedyAllocator(Allocator):
    name = "greedy"
    mode = "batch"

    def allocate(self, ctx: AllocationContext) -> AllocationResult:
        specs = {t.task_id: t for t in ctx.tasks}
        deps = {tid: [d for d in s.depends_on if d in specs] for tid, s in specs.items()}
        order = topological_order(deps, {tid: s.priority for tid, s in specs.items()})
        virtual = {a.agent_id: a for a in ctx.agents}
        result = AllocationResult(objective=0.0)
        for tid in order:
            spec = specs[tid]
            excluded = ctx.excluded.get(tid, set())
            best = None
            for aid in sorted(virtual):
                pc = ctx.cost_model.pair(virtual[aid], spec, excluded)
                if pc.feasible and (best is None or pc.cost < best[0] - 1e-12):
                    best = (pc.cost, aid, pc)
            if best is None:
                result.unassigned[tid] = explain_unassigned(spec, virtual.values(), ctx.cost_model, excluded)
                continue
            cost, aid, pc = best
            result.assignments[tid] = aid
            result.objective += cost
            a = virtual[aid]
            virtual[aid] = replace(
                a,
                position=ctx.cost_model.world.zone_cell(spec.target_zone),
                battery=a.battery - pc.energy,
                committed_ticks=a.committed_ticks + pc.total_ticks,
            )
        return result
