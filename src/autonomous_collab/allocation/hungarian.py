"""Online one-to-one allocator: priority-first task selection, cost-optimal matching.

Per cycle: take ready tasks sorted by (-priority, id) that at least one idle agent can do,
keep the first k = #idle agents, then solve the k x n assignment with the Hungarian method
(``scipy.optimize.linear_sum_assignment``) on the cost matrix c_ij.

Infeasible pairs get a finite penalty M only so the solver can run; M exceeds the sum of all
feasible costs, so the solver first maximises the number of feasible pairs and then
minimises cost, and any pair that still carries M is DISCARDED after solving. Hard
constraints are therefore never satisfied by paying a penalty. Optimal for the one-to-one
cost matrix of the current cycle only (no lookahead).
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment

from .base import AllocationContext, AllocationResult, Allocator


class HungarianAllocator(Allocator):
    name = "hungarian"
    mode = "online"

    def allocate(self, ctx: AllocationContext) -> AllocationResult:
        result = AllocationResult(objective=0.0)
        agents = sorted((a for a in ctx.agents if a.available and a.idle), key=lambda a: a.agent_id)
        if not agents or not ctx.tasks:
            return result
        ordered = sorted(ctx.tasks, key=lambda t: (-t.priority, t.task_id))
        pairs = {
            (t.task_id, a.agent_id): ctx.cost_model.pair(a, t, ctx.excluded.get(t.task_id, set()))
            for t in ordered
            for a in agents
        }
        candidates = [t for t in ordered if any(pairs[(t.task_id, a.agent_id)].feasible for a in agents)]
        chosen = candidates[: len(agents)]
        if not chosen:
            return result
        feasible_costs = [pairs[(t.task_id, a.agent_id)].cost for t in chosen for a in agents if pairs[(t.task_id, a.agent_id)].feasible]
        big_m = 1.0 + (len(chosen) + len(agents)) * (max(feasible_costs) + 1.0)
        matrix = np.full((len(chosen), len(agents)), big_m)
        for i, t in enumerate(chosen):
            for j, a in enumerate(agents):
                pc = pairs[(t.task_id, a.agent_id)]
                if pc.feasible:
                    matrix[i, j] = pc.cost
        rows, cols = linear_sum_assignment(matrix)
        for i, j in zip(rows, cols):
            pc = pairs[(chosen[i].task_id, agents[j].agent_id)]
            if not pc.feasible:  # discard penalty pairs: hard constraint stays hard
                continue
            result.assignments[chosen[i].task_id] = agents[j].agent_id
            result.objective += pc.cost
        return result
