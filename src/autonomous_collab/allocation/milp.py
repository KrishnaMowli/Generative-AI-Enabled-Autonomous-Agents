"""Batch optimiser: binary assignment MILP (scipy.optimize.milp / HiGHS).

Decision variables
    x_ij in {0,1}  task j -> agent i, created ONLY for feasible pairs (hard constraints are
                   enforced by omitting the variable, not by a cost)
    u_j  in {0,1}  task j left unassigned
    Z    >= 0      makespan proxy (max over agents of committed + assigned ticks, / T_ref)

    minimise   sum_ij (c_ij + eps_i) x_ij + U * sum_j u_j + w_m * Z
    s.t.       sum_i x_ij + u_j = 1                              for each task j
               committed_i/T + sum_j (t_ij/T) x_ij <= Z          for each agent i
               sum_j E_ij x_ij <= battery_i - reserve_i          for each agent i  (energy budget)
               sum_j t_ij x_ij <= limit - committed_i            if a workload limit is set

U exceeds any achievable cost total, so the solver first maximises the number of assigned
tasks, then minimises cost + w_m * Z (lexicographic). ``eps_i`` is a tiny agent-index
perturbation giving deterministic tie-breaking.

Assumptions under which the solution is optimal: the static assignment approximation - each
c_ij is evaluated from the agent's position at decision time, so sequence-dependent travel
between several tasks of one agent is NOT modelled (the guide's warning); the energy budget
uses the same per-pair estimates. Dependencies are not modelled here (the scheduler enforces
them). Hence "optimal for the encoded model", not for full multi-robot routing.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from ..errors import AllocationError
from ..models import V
from .base import AllocationContext, AllocationResult, Allocator
from .cost_model import explain_unassigned

EPS_TIE = 1e-7


class MilpAllocator(Allocator):
    name = "milp"
    mode = "batch"

    def allocate(self, ctx: AllocationContext) -> AllocationResult:
        cm = ctx.cost_model
        agents = sorted(ctx.agents, key=lambda a: a.agent_id)
        tasks = sorted(ctx.tasks, key=lambda t: t.task_id)
        result = AllocationResult()
        if not tasks:
            result.objective = 0.0
            return result

        pairs: list[tuple[int, int, object]] = []
        for j, t in enumerate(tasks):
            for i, a in enumerate(agents):
                pc = cm.pair(a, t, ctx.excluded.get(t.task_id, set()))
                if pc.feasible:
                    pairs.append((i, j, pc))
        placeable = sorted({j for _, j, _ in pairs})
        for j, t in enumerate(tasks):
            if j not in placeable:
                result.unassigned[t.task_id] = explain_unassigned(t, agents, cm, ctx.excluded.get(t.task_id, set()))
        if not pairs:
            result.objective = 0.0
            result.status = "no_feasible_pairs"
            return result

        n_x, n_u = len(pairs), len(placeable)
        iz = n_x + n_u
        n_var = iz + 1
        w = cm.weights
        t_ref = cm.t_ref
        u_pos = {j: n_x + k for k, j in enumerate(placeable)}

        max_cost = {j: max(pc.cost for _, jj, pc in pairs if jj == j) for j in placeable}
        max_ticks = {j: max(pc.total_ticks for _, jj, pc in pairs if jj == j) for j in placeable}
        z_max = (max((a.committed_ticks for a in agents), default=0.0) + sum(max_ticks.values())) / t_ref
        big_u = 1.0 + sum(max_cost.values()) + w.makespan * z_max

        c = np.zeros(n_var)
        for k, (i, j, pc) in enumerate(pairs):
            c[k] = pc.cost + EPS_TIE * (i + 1)
        for j in placeable:
            c[u_pos[j]] = big_u
        c[iz] = w.makespan

        rows, lbs, ubs = [], [], []
        for j in placeable:  # each task assigned once or explicitly unassigned
            row = np.zeros(n_var)
            for k, (i, jj, _) in enumerate(pairs):
                if jj == j:
                    row[k] = 1.0
            row[u_pos[j]] = 1.0
            rows.append(row); lbs.append(1.0); ubs.append(1.0)
        for i, a in enumerate(agents):
            mine = [(k, pc) for k, (ii, _, pc) in enumerate(pairs) if ii == i]
            if not mine:
                continue
            row = np.zeros(n_var)  # makespan proxy
            for k, pc in mine:
                row[k] = pc.total_ticks / t_ref
            row[iz] = -1.0
            rows.append(row); lbs.append(-np.inf); ubs.append(-a.committed_ticks / t_ref)
            row = np.zeros(n_var)  # energy budget
            for k, pc in mine:
                row[k] = pc.energy
            rows.append(row); lbs.append(-np.inf); ubs.append(max(0.0, a.battery - a.reserve))
            if ctx.workload_limit_ticks is not None:
                row = np.zeros(n_var)
                for k, pc in mine:
                    row[k] = pc.total_ticks
                rows.append(row); lbs.append(-np.inf); ubs.append(max(0.0, ctx.workload_limit_ticks - a.committed_ticks))

        integrality = np.ones(n_var)
        integrality[iz] = 0
        upper = np.ones(n_var)
        upper[iz] = np.inf
        res = milp(
            c,
            constraints=LinearConstraint(np.vstack(rows), np.array(lbs), np.array(ubs)),
            integrality=integrality,
            bounds=Bounds(np.zeros(n_var), upper),
            options={"disp": False},
        )
        if res.status != 0 or res.x is None:
            raise AllocationError(f"MILP did not reach optimality (status={res.status}: {res.message})")

        x = res.x
        static_sum = 0.0
        for k, (i, j, pc) in enumerate(pairs):
            if x[k] > 0.5:
                tid = tasks[j].task_id
                if tid in result.assignments:
                    raise AllocationError(f"task {tid} assigned twice by MILP")
                result.assignments[tid] = agents[i].agent_id
                static_sum += pc.cost
        reason = V.WORKLOAD_LIMIT if ctx.workload_limit_ticks is not None else V.BATTERY_LIMIT
        for j in placeable:
            tid = tasks[j].task_id
            if tid not in result.assignments:
                result.unassigned[tid] = reason
        result.objective = float(static_sum + w.makespan * x[iz])
        result.details = {"static_sum": float(static_sum), "makespan_proxy_Z": float(x[iz]), "solver_status": int(res.status)}
        return result
