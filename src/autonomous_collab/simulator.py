"""Deterministic time-stepped grid simulator: owns true positions, battery and task outcomes.

The simulator applies only actions approved by the SafetyMonitor and reports what actually
happened as ``SimEvent`` objects. It never trusts the coordinator's or the LLM's claim that a
task succeeded: success requires the handler's postcondition.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from . import executor
from .config import Settings
from .environment import GridWorld
from .errors import PreconditionError
from .logging_utils import EventLog
from .models import AgentSpec, AgentStatus, Cell, TaskSpec, V
from .rng import stable_rng
from .validation.safety_monitor import MoveProposal, SafetyMonitor


# Yield moves outrank ordinary task moves so a boxed-in idle agent can always claim the cell
# a retreating visitor just vacated (otherwise both re-propose it forever: livelock).
YIELD_PRIORITY = 100


@dataclass
class SimEvent:
    type: str            # TASK_COMPLETED TASK_FAILED WORK_STARTED AGENT_FAILED LOW_BATTERY ROUTE_UNREACHABLE DEADLOCK CELL_BLOCKED
    tick: int
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRuntime:
    spec: AgentSpec
    position: Cell
    battery: float
    energy_per_move: float
    status: AgentStatus = AgentStatus.IDLE
    active_task: Optional[str] = None
    active_spec: Optional[TaskSpec] = None
    goal: Optional[Cell] = None
    path: list[Cell] = field(default_factory=list)
    path_idx: int = 0
    path_version: int = -1
    progress: int = 0
    work_total: int = 0
    work_started: bool = False
    hold_ticks: int = 0
    yield_requested: bool = False
    retreat_requested: bool = False
    retreat_avoid: set = field(default_factory=set)
    holdoff: int = 0

    @property
    def agent_id(self) -> str:
        return self.spec.agent_id


def detect_collisions(before: Mapping[str, Cell], after: Mapping[str, Cell]) -> list[dict[str, Any]]:
    """Independent post-hoc audit of one tick: same-cell occupancy and edge swaps.

    Deliberately does not use the SafetyMonitor, so it can catch monitor bugs.
    """
    out: list[dict[str, Any]] = []
    by_cell: dict[Cell, list[str]] = {}
    for aid, cell in after.items():
        by_cell.setdefault(cell, []).append(aid)
    for cell, ids in sorted(by_cell.items()):
        if len(ids) > 1:
            out.append({"kind": "same_cell", "cell": list(cell), "agents": sorted(ids)})
    ids = sorted(after)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in before and b in before and before[a] != after[a] and before[a] == after[b] and before[b] == after[a]:
                out.append({"kind": "edge_swap", "agents": [a, b]})
    return out


class Simulator:
    def __init__(
        self,
        world: GridWorld,
        agents: list[AgentSpec],
        settings: Settings,
        seed: int,
        log: EventLog,
        *,
        monitor: Optional[SafetyMonitor] = None,
        duration_jitter: float = 0.0,
    ) -> None:
        self.world = world
        self.settings = settings
        self.seed = seed
        self.log = log
        self.monitor = monitor or SafetyMonitor(world, settings)
        self.duration_jitter = duration_jitter
        self.registry: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, int] = {}
        default_epm = settings.simulation.energy_per_move
        self.agents: dict[str, AgentRuntime] = {
            a.agent_id: AgentRuntime(a, tuple(a.start), a.battery, a.energy_per_move or default_epm)
            for a in sorted(agents, key=lambda s: s.agent_id)
        }
        starts = [r.position for r in self.agents.values()]  # initial starts must be distinct
        if len(set(starts)) != len(starts):
            raise ValueError("agents must start on distinct cells")

    # ---------------------------------------------------------------- helpers
    def _occupies(self, a: AgentRuntime) -> bool:
        """A disabled agent occupies its cell only if ``failed_agents_block_cell`` is set."""
        if a.status in (AgentStatus.FAILED, AgentStatus.UNAVAILABLE):
            return self.settings.simulation.failed_agents_block_cell
        return True

    def positions(self) -> dict[str, Cell]:
        return {aid: a.position for aid, a in self.agents.items() if self._occupies(a)}

    def occupied_cells(self) -> set[Cell]:
        return set(self.positions().values())

    def _emit(self, events: list[SimEvent], ev: SimEvent) -> None:
        events.append(ev)
        self.log.append("event", ev.tick, event_type=ev.type, agent_id=ev.agent_id, task_id=ev.task_id, **ev.payload)

    def _static_occupied(self, exclude: str) -> set[Cell]:
        return {a.position for aid, a in self.agents.items() if aid != exclude and a.status != AgentStatus.MOVING and self._occupies(a)}

    # ------------------------------------------------------------ control API
    def start_task(self, agent_id: str, spec: TaskSpec, path: list[Cell], tick: int) -> list[SimEvent]:
        a = self.agents[agent_id]
        if a.status != AgentStatus.IDLE:
            raise ValueError(f"{agent_id} is {a.status.value}, cannot start {spec.task_id}")
        self.attempts[spec.task_id] = self.attempts.get(spec.task_id, 0) + 1
        a.active_task, a.active_spec = spec.task_id, spec
        a.goal = self.world.zone_cell(spec.target_zone)
        a.path, a.path_idx, a.path_version = list(path), 0, self.world.version
        a.progress, a.work_total, a.work_started, a.hold_ticks = 0, 0, False, 0
        a.status = AgentStatus.MOVING
        events: list[SimEvent] = []
        if a.position == a.goal:
            self._begin_work(a, tick, events)
        return events

    def fail_agent(self, agent_id: str, tick: int, reason: str = "injected_failure") -> list[SimEvent]:
        return self._disable(self.agents[agent_id], tick, AgentStatus.FAILED, "AGENT_FAILED", reason)

    def block_cell(self, cell: Cell, tick: int, reason: str = "injected") -> list[SimEvent]:
        events: list[SimEvent] = []
        if self.world.block_cell(cell, reason):
            self._emit(events, SimEvent("CELL_BLOCKED", tick, payload={"cell": list(cell), "world_version": self.world.version}))
        return events

    def _disable(self, a: AgentRuntime, tick: int, status: AgentStatus, ev_type: str, reason: str) -> list[SimEvent]:
        events: list[SimEvent] = []
        if a.status in (AgentStatus.FAILED, AgentStatus.UNAVAILABLE):
            return events
        payload = {"reason": reason, "work_started": a.work_started, "cell": list(a.position)}
        task_id = a.active_task
        a.status = status
        a.active_task, a.active_spec, a.goal, a.path = None, None, None, []
        a.work_started = False
        if self.settings.simulation.failed_agents_block_cell:
            self.world.block_cell(a.position, f"{status.value.lower()}:{a.agent_id}")
        self._emit(events, SimEvent(ev_type, tick, a.agent_id, task_id, payload))
        return events

    def progress_of(self, agent_id: str) -> tuple[AgentStatus, Optional[str], float]:
        a = self.agents[agent_id]
        frac = (a.progress / a.work_total) if a.status == AgentStatus.EXECUTING and a.work_total else 0.0
        return a.status, a.active_task, frac

    # ------------------------------------------------------------------- step
    def step(self, tick: int) -> list[SimEvent]:
        events: list[SimEvent] = []
        for a in self.agents.values():  # (1) work progress
            if a.status == AgentStatus.EXECUTING:
                a.progress += 1
                if a.progress >= a.work_total:
                    self._complete(a, tick, events)
        self._movement_phase(tick, events)  # (2) movement + arrivals
        return events

    # -------------------------------------------------------------- execution
    def _begin_work(self, a: AgentRuntime, tick: int, events: list[SimEvent]) -> None:
        spec = a.active_spec
        assert spec is not None
        try:
            executor.check_precondition(spec, self.registry)
        except PreconditionError as exc:
            self._fail_active(a, tick, events, "PRECONDITION_FAILED", str(exc))
            return
        energy = self.settings.task_catalog[spec.task_type].energy
        if a.battery - energy < 0:
            events.extend(self._disable(a, tick, AgentStatus.UNAVAILABLE, "LOW_BATTERY", "cannot pay task energy"))
            return
        a.battery -= energy
        base = max(1, math.ceil(spec.estimated_duration_s / self.settings.simulation.tick_seconds))
        j = self.duration_jitter
        factor = 1.0 if j == 0 else float(stable_rng(self.seed, "duration", spec.task_id).uniform(1 - j, 1 + j))
        a.work_total = max(1, round(base * factor))
        a.progress, a.work_started, a.status = 0, True, AgentStatus.EXECUTING
        self.log.append("work_start", tick, agent_id=a.agent_id, task_id=spec.task_id, energy=energy, work_ticks=a.work_total)
        events.append(SimEvent("WORK_STARTED", tick, a.agent_id, spec.task_id))

    def _fail_active(self, a: AgentRuntime, tick: int, events: list[SimEvent], code: str, detail: str) -> None:
        spec_id, started = a.active_task, a.work_started
        a.status = AgentStatus.IDLE
        a.active_task, a.active_spec, a.goal, a.path, a.work_started = None, None, None, [], False
        self._emit(events, SimEvent("TASK_FAILED", tick, a.agent_id, spec_id, {"code": code, "detail": detail, "work_started": started}))

    def _complete(self, a: AgentRuntime, tick: int, events: list[SimEvent]) -> None:
        spec = a.active_spec
        assert spec is not None
        attempt = self.attempts.get(spec.task_id, 1)
        p_fail = self.settings.simulation.task_failure_probability
        if p_fail > 0 and stable_rng(self.seed, "taskfail", spec.task_id, attempt).random() < p_fail:
            self._fail_active(a, tick, events, "EXEC_FAILURE", "simulated execution failure")
            return
        rng = stable_rng(self.seed, "result", spec.task_id)
        result = executor.run_handler(spec, a.agent_id, rng, self.registry)
        if not executor.check_postcondition(spec, result, self.registry):
            self._fail_active(a, tick, events, "POSTCONDITION_FAILED", "handler result did not satisfy acceptance criteria")
            return
        self.registry[spec.task_id] = result
        self.log.append("task_result", tick, task_id=spec.task_id, agent_id=a.agent_id, result=result)
        a.status = AgentStatus.IDLE
        a.active_task, a.active_spec, a.goal, a.path, a.work_started = None, None, None, [], False
        self._emit(events, SimEvent("TASK_COMPLETED", tick, a.agent_id, spec.task_id, {"result": result}))

    # --------------------------------------------------------------- movement
    def _ensure_path(self, a: AgentRuntime, tick: int, events: list[SimEvent]) -> bool:
        """Recompute the route if the map changed. Returns False if the goal became unreachable."""
        if a.path_version == self.world.version and a.path:
            return True
        assert a.goal is not None
        path = self.world.shortest_path(a.position, a.goal)
        if path is None:
            spec_id = a.active_task
            a.status = AgentStatus.IDLE
            a.active_task, a.active_spec, a.goal, a.path, a.work_started = None, None, None, [], False
            self._emit(events, SimEvent("ROUTE_UNREACHABLE", tick, a.agent_id, spec_id, {"cell": list(a.position)}))
            return False
        a.path, a.path_idx, a.path_version = path, 0, self.world.version
        return True

    def _yield_cell(self, a: AgentRuntime, taken: set[Cell]) -> Optional[Cell]:
        for cell in self.world.neighbors(a.position):
            if cell not in taken:
                return cell
        return None

    def _movement_phase(self, tick: int, events: list[SimEvent]) -> None:
        positions = self.positions()
        taken = self.occupied_cells()
        proposals: list[MoveProposal] = []
        for a in self.agents.values():
            if a.status == AgentStatus.MOVING:
                if a.holdoff > 0:  # just stepped aside: let the other agent clear the cell first
                    a.holdoff -= 1
                    continue
                if not self._ensure_path(a, tick, events):
                    continue
                nxt = a.path[a.path_idx + 1]
                prio = a.active_spec.priority if a.active_spec else 1
                if a.retreat_requested:
                    side = next((c for c in self.world.neighbors(a.position) if c not in taken and c != nxt and c not in a.retreat_avoid), None)
                    if side is not None:  # step aside so an idle blocker can leave the dead end
                        proposals.append(MoveProposal(a.agent_id, a.position, side, prio, a.battery, a.energy_per_move, self.world.version))
                        continue
                    a.retreat_requested = False
                proposals.append(MoveProposal(a.agent_id, a.position, nxt, prio, a.battery, a.energy_per_move, a.path_version))
            elif a.status == AgentStatus.IDLE and a.yield_requested:
                cell = self._yield_cell(a, taken)
                if cell is not None:
                    proposals.append(MoveProposal(a.agent_id, a.position, cell, YIELD_PRIORITY, a.battery, a.energy_per_move, self.world.version))
        accepted, rejected = self.monitor.resolve_moves(proposals, positions)

        before = dict(positions)
        for p in accepted:
            a = self.agents[p.agent_id]
            a.position = p.to_cell
            a.battery -= p.energy
            a.hold_ticks = 0
            if a.status == AgentStatus.MOVING and a.retreat_requested:
                a.retreat_requested = False
                a.holdoff = 2
                a.path_version = -1  # recompute the route from the new cell
            elif a.status == AgentStatus.MOVING and a.path[a.path_idx + 1] == p.to_cell:
                a.path_idx += 1
            else:
                a.yield_requested = False
            self.log.append("move", tick, agent_id=a.agent_id, from_cell=list(p.from_cell), to_cell=list(p.to_cell), energy=p.energy)
        after = self.positions()
        for col in detect_collisions(before, after):
            self.log.append("collision", tick, **col)

        for p, violation in rejected:
            self._handle_rejection(self.agents[p.agent_id], p, violation, tick, events)

        for a in self.agents.values():
            if a.status != AgentStatus.MOVING:
                continue
            if a.position == a.goal:
                self._begin_work(a, tick, events)
            elif a.battery < a.spec.battery_reserve:
                events.extend(self._disable(a, tick, AgentStatus.UNAVAILABLE, "LOW_BATTERY", "battery below reserve"))

    def _handle_rejection(self, a: AgentRuntime, p: MoveProposal, violation, tick: int, events: list[SimEvent]) -> None:
        self.log.append("move_rejected", tick, agent_id=a.agent_id, code=violation.code, blocker=violation.data.get("blocker"))
        if a.status != AgentStatus.MOVING:
            return  # a refused yield: stay put
        code = violation.code
        if code in (V.STALE_STATE, V.ROUTE_BLOCKED):
            a.path_version = -1  # recompute next tick
            return
        if code == V.BATTERY_EXHAUSTED:
            events.extend(self._disable(a, tick, AgentStatus.UNAVAILABLE, "LOW_BATTERY", "battery exhausted"))
            return
        a.hold_ticks += 1
        blocker_id = violation.data.get("blocker")
        blocker = self.agents.get(blocker_id) if blocker_id else None
        if blocker is not None and blocker.status == AgentStatus.IDLE:
            blocker.yield_requested = True
            occupied = self.occupied_cells()
            if self._yield_cell(blocker, occupied) is None and a.hold_ticks >= 2:
                a.retreat_requested = True  # blocker is boxed in by us: make room
                a.retreat_avoid = set()
        elif blocker is not None and blocker.status == AgentStatus.MOVING and a.hold_ticks >= 1:
            ahead = blocker.path[blocker.path_idx + 1] if blocker.path_idx + 1 < len(blocker.path) else None
            if ahead == a.position:  # head-on: exactly one of the two steps aside
                occupied = self.occupied_cells()
                a_can = self._yield_cell(a, occupied) is not None
                b_can = self._yield_cell(blocker, occupied) is not None
                pa = a.active_spec.priority if a.active_spec else 1
                pb = blocker.active_spec.priority if blocker.active_spec else 1
                a_lower = (-pa, a.agent_id) > (-pb, blocker.agent_id)
                if a_can and (not b_can or a_lower):
                    a.retreat_requested = True
                    a.retreat_avoid = set(blocker.path[blocker.path_idx + 1: blocker.path_idx + 4])
        if a.hold_ticks >= self.settings.simulation.max_hold_ticks:
            task_id = a.active_task
            a.status = AgentStatus.IDLE
            a.active_task, a.active_spec, a.goal, a.path, a.hold_ticks = None, None, None, [], 0
            self._emit(events, SimEvent("DEADLOCK", tick, a.agent_id, task_id, {"cell": list(a.position)}))
            return
        assert a.goal is not None
        alt = self.world.shortest_path(a.position, a.goal, extra_blocked=self._static_occupied(a.agent_id) - {a.goal})
        if alt is not None and len(alt) > 1 and alt[1] != a.path[a.path_idx + 1]:
            a.path, a.path_idx, a.path_version = alt, 0, self.world.version
