"""Deterministic SafetyMonitor: the gate between *proposed* actions and simulator state.

Scope (read this): these checks hold for the discrete grid simulator only. They are not a
proof of collision avoidance for physical robots, which would need geometry, localisation
uncertainty, dynamics, braking distance and a separate low-level safety controller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from ..config import Settings
from ..environment import GridWorld, manhattan
from ..models import Cell, TaskSpec, TaskState, V, Violation
from ..views import AgentView

ALLOWED_ACTION_KINDS = frozenset({"move", "task"})


@dataclass(frozen=True)
class MoveProposal:
    agent_id: str
    from_cell: Cell
    to_cell: Cell
    priority: int          # higher moves first when two proposals conflict
    battery: float
    energy: float
    world_version: int


class SafetyMonitor:
    def __init__(self, world: GridWorld, settings: Settings) -> None:
        self.world = world
        self.settings = settings

    # ------------------------------------------------------------ allowlist
    def check_action(self, kind: str, name: str) -> list[Violation]:
        """Action allowlist: only typed ``move`` actions and catalogued task handlers."""
        if kind not in ALLOWED_ACTION_KINDS:
            return [Violation(V.ACTION_NOT_ALLOWED, f"action kind {kind!r} is not allowed")]
        if kind == "task" and name not in self.settings.task_catalog:
            return [Violation(V.ACTION_NOT_ALLOWED, f"task handler {name!r} is not in the catalogue")]
        return []

    # -------------------------------------------------------------- dispatch
    def check_dispatch(
        self,
        spec: TaskSpec,
        agent: AgentView,
        dep_states: Mapping[str, TaskState],
        *,
        assigned_version: int,
        plan_version: int,
        path: Optional[Sequence[Cell]],
    ) -> list[Violation]:
        """Approve (empty list) or reject starting ``spec`` on ``agent`` right now."""
        out = self.check_action("task", spec.task_type)
        if out:
            return out
        if not agent.available or not agent.idle:
            out.append(Violation(V.AGENT_NOT_AVAILABLE, f"{agent.agent_id} cannot start a task now", spec.task_id))
        missing = sorted(set(spec.required_capabilities) - set(agent.capabilities))
        if missing:
            out.append(Violation(V.CAPABILITY_MISMATCH, f"{agent.agent_id} lacks {missing}", spec.task_id))
        unmet = sorted(d for d in spec.depends_on if dep_states.get(d) != TaskState.SUCCEEDED)
        if unmet:
            out.append(Violation(V.DEPENDENCY_NOT_SATISFIED, f"prerequisites not SUCCEEDED: {unmet}", spec.task_id))
        if assigned_version != plan_version:
            out.append(Violation(V.STALE_STATE, f"assignment is from plan v{assigned_version}, current is v{plan_version}", spec.task_id))
        if path is None:
            out.append(Violation(V.NO_PATH, f"no path from {agent.position} to zone {spec.target_zone}", spec.task_id))
        else:
            problems = self.world.validate_path(list(path))
            if problems:
                out.append(Violation(V.ROUTE_BLOCKED, "; ".join(problems), spec.task_id))
            else:
                cost = (len(path) - 1) * agent.energy_per_move + self.settings.task_catalog[spec.task_type].energy
                if agent.battery - cost < agent.reserve:
                    out.append(
                        Violation(
                            V.BATTERY_RESERVE,
                            f"{agent.agent_id}: battery {agent.battery:.1f} - need {cost:.1f} would end below reserve {agent.reserve:.1f}",
                            spec.task_id,
                        )
                    )
        return out

    # ----------------------------------------------------------------- moves
    def resolve_moves(
        self,
        proposals: Sequence[MoveProposal],
        positions: Mapping[str, Cell],
    ) -> tuple[list[MoveProposal], list[tuple[MoveProposal, Violation]]]:
        """Centrally resolve simultaneous move proposals in deterministic priority order.

        Semantics (conservative): a destination must be unoccupied at the *start* of the tick
        (no following into a just-vacated cell), at most one accepted move per destination,
        and opposite moves on the same edge are rejected. Positions only change after all
        accepted moves are decided.
        """
        occupant: dict[Cell, str] = {cell: aid for aid, cell in positions.items()}
        ordered = sorted(proposals, key=lambda p: (-p.priority, p.agent_id))
        edges = {(p.from_cell, p.to_cell) for p in ordered}
        reserved: dict[Cell, str] = {}
        accepted: list[MoveProposal] = []
        rejected: list[tuple[MoveProposal, Violation]] = []
        for p in ordered:
            violation = self._check_move(p, positions, occupant, edges, reserved)
            if violation is None:
                reserved[p.to_cell] = p.agent_id
                accepted.append(p)
            else:
                rejected.append((p, violation))
        return accepted, rejected

    def _check_move(
        self,
        p: MoveProposal,
        positions: Mapping[str, Cell],
        occupant: Mapping[Cell, str],
        edges: set[tuple[Cell, Cell]],
        reserved: Mapping[Cell, str],
    ) -> Optional[Violation]:
        if positions.get(p.agent_id) != p.from_cell or manhattan(p.from_cell, p.to_cell) != 1:
            return Violation(V.ILLEGAL_MOVE, f"{p.agent_id}: {p.from_cell}->{p.to_cell} is not a one-cell move from its position")
        if p.world_version != self.world.version:
            return Violation(V.STALE_STATE, f"{p.agent_id}: move computed on world v{p.world_version}, current v{self.world.version}")
        if not self.world.is_passable(p.to_cell):
            return Violation(V.ROUTE_BLOCKED, f"{p.agent_id}: cell {p.to_cell} is not passable")
        if p.battery - p.energy < 0:
            return Violation(V.BATTERY_EXHAUSTED, f"{p.agent_id}: battery {p.battery:.2f} cannot pay {p.energy:.2f}")
        if (p.to_cell, p.from_cell) in edges:
            return Violation(V.EDGE_SWAP, f"{p.agent_id}: opposite move on edge {p.from_cell}<->{p.to_cell}", data={"blocker": occupant.get(p.to_cell)})
        blocker = occupant.get(p.to_cell)
        if blocker is not None and blocker != p.agent_id:
            return Violation(V.NODE_OCCUPIED, f"{p.agent_id}: {p.to_cell} is occupied by {blocker}", data={"blocker": blocker})
        if p.to_cell in reserved:
            return Violation(V.NODE_RESERVED, f"{p.agent_id}: {p.to_cell} already reserved by {reserved[p.to_cell]}", data={"blocker": reserved[p.to_cell]})
        return None
