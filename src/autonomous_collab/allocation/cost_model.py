"""Pairwise agent-task cost and hard feasibility (see docs/ALLOCATION.md).

Cost for agent i and task j (all terms dimensionless after normalisation):

    c_ij = w_d * d_ij / D_ref + w_t * (T_travel_ij + T_work_j) / T_ref + w_e * E_ij / E_ref
         + w_p * (p_j / P_max) * T_travel_ij / T_ref + w_l * L_i / T_ref

d_ij: cells from the agent's (virtual) position to the task zone; T in ticks; E = travel +
task energy; p_j priority; L_i ticks of work already committed to agent i.

Feasibility is a *hard* predicate computed separately from the cost: an infeasible pair has
``feasible=False`` and ``cost=inf`` and is never offered to a solver as a candidate variable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from ..config import Settings
from ..environment import GridWorld
from ..models import TaskSpec, V
from ..views import AgentView


@dataclass(frozen=True)
class PairCost:
    feasible: bool
    reasons: tuple[str, ...]
    cost: float
    parts: Mapping[str, float] = field(default_factory=dict)
    travel_cells: int = 0
    travel_ticks: int = 0
    work_ticks: int = 0
    energy: float = 0.0

    @property
    def total_ticks(self) -> int:
        return self.travel_ticks + self.work_ticks


class CostModel:
    def __init__(self, world: GridWorld, settings: Settings) -> None:
        self.world = world
        self.settings = settings
        self.weights = settings.allocation.weights
        sim = settings.simulation
        catalog = settings.task_catalog
        max_work_ticks = max(math.ceil(c.default_duration_s / sim.tick_seconds) for c in catalog.values())
        max_task_energy = max(c.energy for c in catalog.values())
        norms = settings.allocation.norms
        dist_ref = world.rows + world.cols
        self.d_ref = float(dist_ref if norms.get("distance", "auto") == "auto" else norms["distance"])
        self.t_ref = float(self.d_ref + max_work_ticks if norms.get("time", "auto") == "auto" else norms["time"])
        self.e_ref = float(
            self.d_ref * sim.energy_per_move + max_task_energy if norms.get("energy", "auto") == "auto" else norms["energy"]
        )
        self.p_max = settings.planner_limits.max_priority

    # ------------------------------------------------------------- basics
    def work_ticks(self, spec: TaskSpec) -> int:
        return max(1, math.ceil(spec.estimated_duration_s / self.settings.simulation.tick_seconds))

    def task_energy(self, spec: TaskSpec) -> float:
        return self.settings.task_catalog[spec.task_type].energy

    # --------------------------------------------------------------- pairs
    def pair(
        self,
        agent: AgentView,
        spec: TaskSpec,
        excluded: Iterable[str] = (),
        *,
        ignore_constraints: bool = False,
    ) -> PairCost:
        """Cost and feasibility of ``agent`` doing ``spec`` from its (virtual) position.

        ``ignore_constraints=True`` evaluates the cost formula for any *reachable* pair, ignoring
        capability / availability / battery / exclusion; used only to score a finished run with
        one common objective definition.
        """
        reasons: list[str] = []
        if not ignore_constraints:
            if not agent.available:
                reasons.append(V.AGENT_NOT_AVAILABLE)
            if not set(spec.required_capabilities) <= set(agent.capabilities):
                reasons.append(V.CAPABILITY_MISMATCH)
            if agent.agent_id in set(excluded):
                reasons.append(V.EXCLUDED_AFTER_REJECTION)
        goal = self.world.zone_cell(spec.target_zone)
        length = self.world.path_length(agent.position, goal)
        if length is None:
            reasons.append(V.NO_PATH)
            return PairCost(False, tuple(reasons), math.inf)
        travel_ticks = length
        work = self.work_ticks(spec)
        energy = length * agent.energy_per_move + self.task_energy(spec)
        if not ignore_constraints and agent.battery - energy < agent.reserve:
            reasons.append(V.BATTERY_RESERVE)
        w = self.weights
        parts = {
            "distance": w.distance * length / self.d_ref,
            "time": w.time * (travel_ticks + work) / self.t_ref,
            "energy": w.energy * energy / self.e_ref,
            "priority": w.priority * (spec.priority / self.p_max) * travel_ticks / self.t_ref,
            "load": w.load * agent.committed_ticks / self.t_ref,
        }
        return PairCost(
            feasible=not reasons,
            reasons=tuple(reasons),
            cost=float(sum(parts.values())),
            parts=parts,
            travel_cells=length,
            travel_ticks=travel_ticks,
            work_ticks=work,
            energy=energy,
        )


def explain_unassigned(
    spec: TaskSpec,
    agents: Iterable[AgentView],
    cost_model: CostModel,
    excluded: Iterable[str] = (),
) -> str:
    """Most fundamental reason code for why no agent can take ``spec`` (precedence order)."""
    agents = list(agents)
    excluded = set(excluded)
    capable = [a for a in agents if set(spec.required_capabilities) <= set(a.capabilities)]
    if not capable:
        return V.NO_CAPABLE_AGENT
    available = [a for a in capable if a.available]
    if not available:
        return V.NO_AVAILABLE_AGENT
    allowed = [a for a in available if a.agent_id not in excluded]
    if not allowed:
        return V.EXCLUDED_AFTER_REJECTION
    pairs = [cost_model.pair(a, spec) for a in allowed]
    reachable = [p for p in pairs if V.NO_PATH not in p.reasons]
    if not reachable:
        return V.NO_REACHABLE_PATH
    if all(V.BATTERY_RESERVE in p.reasons for p in reachable):
        return V.BATTERY_LIMIT
    return V.WORKLOAD_LIMIT


def static_objective(
    cost_model: CostModel,
    agents: Mapping[str, AgentView],
    specs: Mapping[str, TaskSpec],
    assignment: Mapping[str, str],
) -> float:
    """Sum of c_ij over (task -> agent) using ``agents`` as the reference (initial) snapshot.

    The same formula is applied to every allocator's outcome so that objective values are
    comparable (weights and cost definition identical). Unreachable pairs contribute nothing.
    """
    total = 0.0
    for tid, aid in assignment.items():
        if aid not in agents or tid not in specs:
            continue
        pc = cost_model.pair(agents[aid], specs[tid], ignore_constraints=True)
        if math.isfinite(pc.cost):
            total += pc.cost
    return total
