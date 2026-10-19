"""End-to-end mission pipeline:

mission text -> sanitise -> planner (LLM/mock/manual) -> schema + semantic validation ->
cost service -> allocator -> dependency scheduler -> SafetyMonitor -> simulator -> logs -> metrics

Fail-closed: if planning or validation fails nothing is executed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from pydantic import ValidationError

from .allocation import make_allocator
from .allocation.cost_model import CostModel, static_objective
from .config import Settings, settings_for_scenario
from .coordinator import Coordinator
from .environment import GridWorld, manhattan
from .logging_utils import EventLog
from .metrics import compute_metrics
from .models import AgentSpec, AgentStatus, InjectedEvent, ScenarioSpec, TaskState
from .planner.interface import Planner, PlanningRequest
from .planner.mission_text import derive_required_outcomes, sanitize_mission
from .rng import stable_rng
from .simulator import Simulator
from .task_graph import transitive_dependencies
from .task_state import TaskTracker
from .validation.safety_monitor import SafetyMonitor
from .validation.semantic_validator import PlanValidator
from .views import AgentView

_PROGRESS_TYPES = {"task_transition", "move", "work_start", "task_result", "event", "allocation", "replan"}


@dataclass
class MissionResult:
    status: str
    success: bool
    scenario: str
    seed: int
    metrics: dict[str, Any]
    tasks: list[dict[str, Any]]
    records: list[dict[str, Any]]
    questions: list[str] = field(default_factory=list)
    rejection_codes: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [f"Mission {self.scenario!r} (seed {self.seed}): {self.status}  success={self.success}"]
        for t in self.tasks:
            note = f"  [{t['reason']}]" if t["reason"] and t["state"] != "SUCCEEDED" else ""
            lines.append(f"  {t['task_id']:<28} {t['state']:<10} agent={t['agent_id'] or '-':<8}{note}")
        for q in self.questions:
            lines.append(f"  clarification needed: {q}")
        if self.rejection_codes:
            lines.append(f"  rejection codes: {sorted(set(self.rejection_codes))}")
        m = self.metrics
        lines.append(
            f"  tasks {m['n_succeeded']}/{m['n_mandatory']}  makespan_s={m['makespan_s']}  travel_cells={m['travel_cells']}  "
            f"energy={m['energy_total']}  replans={m['n_replans']}  collisions={m['collisions']}"
        )
        return lines


# ---------------------------------------------------------------------------
def resolve_agents(scenario: ScenarioSpec, world: GridWorld, seed: int) -> list[AgentSpec]:
    """Apply the scenario's seeded randomisation (start jitter, battery jitter) to the agents."""
    rnd = scenario.randomization
    zone_cells = set(world.zones.values())
    occupied: set[tuple[int, int]] = set()
    out: list[AgentSpec] = []
    for spec in sorted(scenario.agents, key=lambda a: a.agent_id):
        start = tuple(spec.start)
        if rnd.start_jitter > 0:
            rng = stable_rng(seed, "start", spec.agent_id)
            cands = sorted(c for c in world.free_cells() if manhattan(c, start) <= rnd.start_jitter and c not in zone_cells and c not in occupied)
            if cands:
                start = cands[int(rng.integers(len(cands)))]
        occupied.add(start)
        battery = spec.battery
        if rnd.battery_jitter > 0:
            factor = 1 + float(stable_rng(seed, "battery", spec.agent_id).uniform(-rnd.battery_jitter, rnd.battery_jitter))
            battery = float(np.clip(spec.battery * factor, 0.0, 100.0))
        out.append(spec.model_copy(update={"start": start, "battery": round(battery, 4)}))
    return out


class FaultSchedule:
    """Deterministic injection of scenario events (timed, seeded-window or conditional)."""

    def __init__(self, events: list[InjectedEvent], seed: int) -> None:
        self.timed: dict[int, list[InjectedEvent]] = {}
        self.conditional: list[InjectedEvent] = []
        for idx, ev in enumerate(events):
            if ev.when_agent_executing:
                self.conditional.append(ev)
            elif ev.tick_window is not None:
                lo, hi = ev.tick_window
                tick = int(stable_rng(seed, "event", idx).integers(lo, hi + 1))
                self.timed.setdefault(tick, []).append(ev)
            elif ev.tick is not None:
                self.timed.setdefault(ev.tick, []).append(ev)
            else:
                raise ValueError(f"event {idx} ({ev.type}) needs tick, tick_window or when_agent_executing")
        self._fired: set[int] = set()

    def has_future(self, tick: int) -> bool:
        return any(t > tick for t in self.timed)

    def fire(self, tick: int, sim: Simulator, coord: Coordinator) -> None:
        due = list(self.timed.get(tick, []))
        for ev in self.conditional:
            if id(ev) in self._fired:
                continue
            status, task_id, frac = sim.progress_of(ev.when_agent_executing)  # type: ignore[arg-type]
            if status == AgentStatus.EXECUTING and (ev.when_task_id in (None, task_id)) and frac >= ev.when_progress:
                self._fired.add(id(ev))
                due.append(ev)
        for ev in due:
            if ev.type == "AGENT_FAILED":
                coord.handle_sim_events(sim.fail_agent(ev.agent_id, tick), tick)  # type: ignore[arg-type]
            elif ev.type == "CELL_BLOCKED":
                coord.handle_sim_events(sim.block_cell(tuple(ev.cell), tick), tick)  # type: ignore[arg-type]
            elif ev.type == "TASK_ADDED":
                coord.enqueue_task_added(dict(ev.task or {}), tick)


# ---------------------------------------------------------------------------
def _final_tasks(tracker: TaskTracker, sim: Simulator, timed_out: bool) -> list[dict[str, Any]]:
    deps = tracker.deps_map()
    rows = []
    for rt in tracker.all():
        reason = rt.reason if rt.state != TaskState.SUCCEEDED else ""
        if rt.state in (TaskState.PENDING, TaskState.ASSIGNED, TaskState.READY, TaskState.EXECUTING):
            dead = [d for d in sorted(transitive_dependencies(deps, rt.task_id))
                    if tracker.get(d).state in (TaskState.FAILED, TaskState.BLOCKED)]
            agent = sim.agents.get(rt.agent_id) if rt.agent_id else None
            if dead:
                reason = f"BLOCKED_BY_DEPENDENCY:{dead[0]}"
            elif agent is not None and agent.status in (AgentStatus.FAILED, AgentStatus.UNAVAILABLE):
                reason = f"ASSIGNED_TO_UNAVAILABLE_AGENT:{rt.agent_id}"
            elif timed_out:
                reason = "NOT_COMPLETED_BEFORE_TIMEOUT"
            else:
                reason = "NOT_DISPATCHED"
        rows.append({"task_id": rt.task_id, "state": rt.state.value, "agent_id": rt.agent_id, "reason": reason,
                     "mandatory": rt.spec.mandatory, "attempts": rt.attempts})
    return rows


def run_mission(
    scenario: ScenarioSpec,
    settings: Settings,
    *,
    planner: Planner,
    allocator_name: str,
    replan: bool,
    seed: int,
    mission_text: Optional[str] = None,
    variant: str = "",
    provider_label: str = "",
    log_path: Optional[Path] = None,
    monitor: Optional[SafetyMonitor] = None,
) -> MissionResult:
    settings = settings_for_scenario(scenario, settings)
    sim_cfg = settings.simulation
    log = EventLog()
    world = GridWorld.from_spec(scenario.world)
    initial_world = world.copy()
    agents = resolve_agents(scenario, world, seed)
    allocator = make_allocator(allocator_name)
    text_in = mission_text if mission_text is not None else scenario.mission_text
    log.append("mission_start", 0, scenario=scenario.name, seed=seed, variant=variant, allocator=allocator_name,
               replan=replan, provider=provider_label, tick_seconds=sim_cfg.tick_seconds, settings_digest=settings.digest(),
               agents=[{"agent_id": a.agent_id, "start": list(a.start), "battery": a.battery, "capabilities": a.capabilities} for a in agents])

    def finish(status: str, tick: int, tasks: list[dict[str, Any]], questions=(), codes=()) -> MissionResult:
        log.append("mission_end", tick, status=status, tasks_final=tasks)
        if log_path is not None:
            log.write_jsonl(Path(log_path))
        metrics = compute_metrics(log.records)
        return MissionResult(status, status == "COMPLETE", scenario.name, seed, metrics, tasks, log.records, list(questions), list(codes))

    # -- 1. mission hygiene (weak first line of defence) ------------------------------
    clean, problems = sanitize_mission(text_in, settings.planner_limits.max_mission_chars)
    fatal = [p for p in problems if not (p.code == "MISSION_INJECTION_SUSPECTED" and settings.planner_limits.injection_policy == "warn")]
    if problems:
        log.append("mission_flagged", 0, codes=[p.code for p in problems], messages=[p.message for p in problems])
    if fatal:
        return finish("MISSION_REJECTED", 0, [], codes=[p.code for p in fatal])

    # -- 2. plan: propose -> validate (planner calls the validator on every attempt) ----
    outcomes = scenario.required_outcomes or derive_required_outcomes(clean, sorted(world.zones), scenario.world.report_zone) or []
    validator = PlanValidator(world, agents, settings, outcomes)
    catalog = settings.task_catalog
    request = PlanningRequest(
        mission_id=f"{scenario.name}-{seed}", mission_text=clean, zones=sorted(world.zones), report_zone=scenario.world.report_zone,
        allowed_task_types={k: v.capability for k, v in catalog.items()}, capability_catalogue=list(settings.capabilities),
        required_outcomes=[o.model_dump() for o in outcomes], task_limit=settings.planner_limits.max_tasks,
    )
    outcome = planner.plan(request, validator)
    for att in outcome.attempts:
        log.append("plan_attempt", 0, index=att.index, outcome=att.outcome, violation_codes=att.violation_codes, latency_s=att.latency_s,
                   input_tokens=att.input_tokens, output_tokens=att.output_tokens, provider=att.provider, model=att.model,
                   prompt_version=outcome.prompt_version, raw_output=att.raw_text)
    codes = [v.code for v in outcome.violations]
    if outcome.status != "plan":
        status = {"needs_clarification": "NEEDS_CLARIFICATION", "provider_error": "PLANNER_UNAVAILABLE"}.get(outcome.status, "PLAN_REJECTED")
        log.append("plan_failed", 0, status=status, codes=codes, messages=[v.message for v in outcome.violations])
        return finish(status, 0, [], questions=outcome.questions, codes=codes)
    log.append("plan_accepted", 0, n_tasks=len(outcome.tasks), task_ids=[t.task_id for t in outcome.tasks])

    # -- 3. execution -----------------------------------------------------------------
    sim_monitor = monitor or SafetyMonitor(world, settings)
    sim = Simulator(world, agents, settings, seed, log, monitor=sim_monitor, duration_jitter=scenario.randomization.duration_jitter)
    tracker = TaskTracker(outcome.tasks, log)
    coord = Coordinator(world=world, sim=sim, tracker=tracker, allocator=allocator, monitor=SafetyMonitor(world, settings),
                        validator=validator, settings=settings, log=log, replan_enabled=replan)
    schedule = FaultSchedule(scenario.events, seed)
    initial_views = {a.agent_id: AgentView(a.agent_id, frozenset(a.capabilities), tuple(a.start), a.battery, a.battery_reserve,
                                           a.energy_per_move or sim_cfg.energy_per_move) for a in agents}
    coord.initial_allocation(0)

    stall, last_sig, status, tick = 0, None, "TIMEOUT", 0
    for tick in range(sim_cfg.max_ticks):
        schedule.fire(tick, sim, coord)
        coord.process_events(tick)
        if allocator.mode == "online":
            coord.allocate_online(tick)
        coord.dispatch(tick)
        coord.handle_sim_events(sim.step(tick), tick)

        mandatory = [t for t in tracker.all() if t.spec.mandatory]
        if mandatory and all(t.state == TaskState.SUCCEEDED for t in mandatory):
            status = "COMPLETE"
            break
        active = tracker.by_state(TaskState.PENDING, TaskState.ASSIGNED, TaskState.READY, TaskState.EXECUTING)
        if not active and not coord.pending_events and not schedule.has_future(tick):
            status = "INCOMPLETE"
            break
        sig = (sum(1 for r in log.records if r["type"] in _PROGRESS_TYPES), sum(a.progress for a in sim.agents.values()))
        stall = stall + 1 if sig == last_sig else 0
        last_sig = sig
        if stall >= sim_cfg.stall_limit_ticks and not schedule.has_future(tick) and not coord.pending_events:
            status = "INCOMPLETE"
            break

    tasks = _final_tasks(tracker, sim, timed_out=(status == "TIMEOUT"))
    done = {t.task_id: t.agent_id for t in tracker.all() if t.state == TaskState.SUCCEEDED and t.agent_id}
    specs = {t.task_id: t.spec for t in tracker.all()}
    log.append("objective_realized", tick, value=round(static_objective(CostModel(initial_world, settings), initial_views, specs, done), 6))
    return finish(status, tick, tasks)
