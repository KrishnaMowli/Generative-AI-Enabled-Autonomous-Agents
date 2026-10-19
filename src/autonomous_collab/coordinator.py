"""Mission coordinator: owns authoritative task state, assignments, scheduling and replanning.

The simulator owns physical truth (positions, battery, task outcomes); the coordinator owns the
plan. Every dispatch passes the SafetyMonitor; completed (SUCCEEDED) work is immutable.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Optional

from pydantic import ValidationError

from .allocation import AllocationContext, AllocationResult, Allocator
from .allocation.cost_model import CostModel, explain_unassigned
from .config import Settings
from .environment import GridWorld
from .logging_utils import EventLog
from .models import AgentStatus, RetryPolicy, TaskSpec, TaskState, V, Violation
from .simulator import SimEvent, Simulator
from .task_state import TaskRuntime, TaskTracker
from .validation.safety_monitor import SafetyMonitor
from .validation.semantic_validator import PlanValidator
from .views import AgentView

_REPLAN_EVENTS = {"AGENT_FAILED", "LOW_BATTERY", "CELL_BLOCKED", "TASK_ADDED", "TASK_FAILED",
                  "ROUTE_UNREACHABLE", "DEADLOCK", "ALLOCATION_INVALID"}
_EXCLUDING_CODES = {V.BATTERY_RESERVE, V.NO_PATH, V.ROUTE_BLOCKED, V.CAPABILITY_MISMATCH, V.AGENT_NOT_AVAILABLE}


class Coordinator:
    def __init__(
        self,
        *,
        world: GridWorld,
        sim: Simulator,
        tracker: TaskTracker,
        allocator: Allocator,
        monitor: SafetyMonitor,
        validator: PlanValidator,
        settings: Settings,
        log: EventLog,
        replan_enabled: bool,
    ) -> None:
        self.world, self.sim, self.tracker = world, sim, tracker
        self.allocator, self.monitor, self.validator = allocator, monitor, validator
        self.settings, self.log, self.replan_enabled = settings, log, replan_enabled
        self.cost_model = CostModel(world, settings)
        self.plan_version = 1
        self.queues: dict[str, list[str]] = {aid: [] for aid in sim.agents}
        self.pending_events: list[dict[str, Any]] = []
        self.last_replan_tick = -10**9
        self._retry_denied: set[str] = set()

    # ------------------------------------------------------------------ views
    def agent_views(self) -> list[AgentView]:
        views: list[AgentView] = []
        cm = self.cost_model
        for aid, a in self.sim.agents.items():
            available = a.status not in (AgentStatus.FAILED, AgentStatus.UNAVAILABLE)
            position, battery, committed = a.position, a.battery, 0.0
            busy = available and a.active_spec is not None
            if busy and a.goal is not None:
                remaining = 0 if a.status == AgentStatus.EXECUTING else (self.world.path_length(a.position, a.goal) or 0)
                work = (a.work_total - a.progress) if a.status == AgentStatus.EXECUTING else cm.work_ticks(a.active_spec)
                energy = remaining * a.energy_per_move + (0.0 if a.work_started else cm.task_energy(a.active_spec))
                position, battery, committed = a.goal, a.battery - energy, float(remaining + work)
            has_queue = any(self.tracker.get(t).state == TaskState.ASSIGNED for t in self.queues[aid])
            views.append(AgentView(
                aid, frozenset(a.spec.capabilities), position, battery, a.spec.battery_reserve, a.energy_per_move,
                available=available, idle=available and a.status == AgentStatus.IDLE and not has_queue and not busy,
                committed_ticks=committed,
            ))
        return views

    # -------------------------------------------------------------- allocation
    @staticmethod
    def _reason(rt: TaskRuntime, code: str) -> str:
        """Keep the underlying cause when every agent was excluded after a rejected dispatch."""
        if code == V.EXCLUDED_AFTER_REJECTION and rt.exclusion_causes:
            return f"{code}({','.join(sorted(rt.exclusion_causes))})"
        return code

    def _context(self, specs: list[TaskSpec]) -> AllocationContext:
        return AllocationContext(
            tasks=specs, agents=self.agent_views(), cost_model=self.cost_model,
            excluded={t.task_id: set(t.excluded_agents) for t in self.tracker.all()},
            workload_limit_ticks=self.settings.allocation.workload_limit_ticks,
        )

    def _apply(self, res: AllocationResult, tick: int, trigger: str, n_tasks: int, *, block_unassigned: bool) -> None:
        for tid in sorted(res.assignments):
            aid = res.assignments[tid]
            rt = self.tracker.transition(tid, TaskState.ASSIGNED, tick, agent_id=aid, reason=f"allocation:{self.allocator.name}")
            rt.assigned_version = self.plan_version
            self.queues[aid].append(tid)
        if block_unassigned:
            for tid in sorted(res.unassigned):
                if self.tracker.get(tid).state == TaskState.PENDING:
                    self.tracker.transition(tid, TaskState.BLOCKED, tick, reason=self._reason(self.tracker.get(tid), res.unassigned[tid]))
        self.log.append(
            "allocation", tick, plan_version=self.plan_version, allocator=self.allocator.name, trigger=trigger,
            n_tasks=n_tasks, assignments=dict(sorted(res.assignments.items())), unassigned=dict(sorted(res.unassigned.items())),
            objective=res.objective, status=res.status,
        )

    def initial_allocation(self, tick: int = 0) -> None:
        if self.allocator.mode == "batch":
            self.allocate_batch(tick, "initial")
        else:
            self.allocate_online(tick, "initial")

    def allocate_batch(self, tick: int, trigger: str) -> None:
        specs = [t.spec for t in self.tracker.by_state(TaskState.PENDING)]
        if not specs:
            return
        res = self.allocator.allocate(self._context(specs))
        self._apply(res, tick, trigger, len(specs), block_unassigned=True)

    def allocate_online(self, tick: int, trigger: str = "cycle") -> None:
        views = self.agent_views()
        if not any(v.idle and v.available for v in views):
            return
        ready = [t.spec for t in self.tracker.by_state(TaskState.PENDING) if self.tracker.deps_succeeded(t.task_id)]
        if ready:
            res = self.allocator.allocate(self._context(ready))
            if res.assignments:
                self._apply(res, tick, trigger, len(ready), block_unassigned=False)
        self.scan_infeasible(tick)

    def scan_infeasible(self, tick: int) -> None:
        """Online mode: block PENDING tasks that no (even busy) agent could ever take."""
        views = self.agent_views()
        for rt in self.tracker.by_state(TaskState.PENDING):
            excl = rt.excluded_agents
            if not any(self.cost_model.pair(v, rt.spec, excl).feasible for v in views):
                self.tracker.transition(rt.task_id, TaskState.BLOCKED, tick, reason=self._reason(rt, explain_unassigned(rt.spec, views, self.cost_model, excl)))

    # ----------------------------------------------------------------- dispatch
    def dispatch(self, tick: int) -> None:
        if self.replan_enabled and self.pending_events:
            return  # freeze dispatch on possibly stale state until the replan runs
        views = {v.agent_id: v for v in self.agent_views()}
        for aid, a in self.sim.agents.items():
            if a.status != AgentStatus.IDLE:
                continue
            ready = [self.tracker.get(t) for t in self.queues[aid]
                     if self.tracker.get(t).state == TaskState.ASSIGNED and self.tracker.deps_succeeded(t)]
            if not ready:
                continue
            def key(rt: TaskRuntime) -> tuple[int, int, str]:
                n = self.world.path_length(a.position, self.world.zone_cell(rt.spec.target_zone))
                return (-rt.spec.priority, n if n is not None else 10**6, rt.task_id)
            rt = min(ready, key=key)
            self.tracker.transition(rt.task_id, TaskState.READY, tick)
            goal = self.world.zone_cell(rt.spec.target_zone)
            path = self.world.shortest_path(a.position, goal)
            deps = {d: self.tracker.get(d).state for d in rt.spec.depends_on}
            physical = replace(views[aid], idle=views[aid].available and a.status == AgentStatus.IDLE)
            violations = self.monitor.check_dispatch(
                rt.spec, physical, deps, assigned_version=rt.assigned_version, plan_version=self.plan_version, path=path
            )
            if violations:
                self._reject_dispatch(rt, aid, violations, tick)
                continue
            self.tracker.transition(rt.task_id, TaskState.EXECUTING, tick, reason="dispatch_approved")
            self.queues[aid].remove(rt.task_id)
            self.handle_sim_events(self.sim.start_task(aid, rt.spec, path or [a.position], tick), tick)

    def _reject_dispatch(self, rt: TaskRuntime, aid: str, violations: list, tick: int) -> None:
        codes = [v.code for v in violations]
        self.log.append("dispatch_rejected", tick, task_id=rt.task_id, agent_id=aid, codes=codes, messages=[v.message for v in violations])
        self.queues[aid].remove(rt.task_id)
        if any(c in _EXCLUDING_CODES for c in codes):
            rt.excluded_agents.add(aid)
            rt.exclusion_causes.update(c for c in codes if c in _EXCLUDING_CODES)
        if self.replan_enabled:
            self.tracker.transition(rt.task_id, TaskState.PENDING, tick, clear_agent=True, reason=f"dispatch_rejected:{codes[0]}")
            self._enqueue({"type": "ALLOCATION_INVALID", "tick": tick, "task_id": rt.task_id, "agent_id": aid})
        else:
            self.tracker.transition(rt.task_id, TaskState.BLOCKED, tick, reason=codes[0])

    # --------------------------------------------------------------- sim events
    def handle_sim_events(self, events: list[SimEvent], tick: int) -> None:
        for ev in events:
            rt = self.tracker.get(ev.task_id) if ev.task_id and ev.task_id in self.tracker.ids() else None
            if ev.type == "WORK_STARTED" and rt:
                rt.work_started = True
            elif ev.type == "TASK_COMPLETED" and rt:
                rt.result = ev.payload.get("result")
                rt.attempts = self.sim.attempts.get(rt.task_id, rt.attempts)
                self.tracker.transition(rt.task_id, TaskState.SUCCEEDED, tick, reason="postcondition_ok")
            elif ev.type in ("TASK_FAILED", "AGENT_FAILED", "LOW_BATTERY") and rt and rt.state == TaskState.EXECUTING:
                rt.attempts = self.sim.attempts.get(rt.task_id, rt.attempts)
                rt.work_started = bool(ev.payload.get("work_started", rt.work_started))
                reason = ev.payload.get("code") or ev.type
                self.tracker.transition(rt.task_id, TaskState.FAILED, tick, reason=str(reason))
            elif ev.type in ("ROUTE_UNREACHABLE", "DEADLOCK") and rt and rt.state == TaskState.EXECUTING:
                rt.attempts = self.sim.attempts.get(rt.task_id, rt.attempts)
                self.tracker.transition(rt.task_id, TaskState.BLOCKED, tick, reason="UNREACHABLE" if ev.type == "ROUTE_UNREACHABLE" else "DEADLOCK")
            if ev.type in _REPLAN_EVENTS:
                self._enqueue({"type": ev.type, "tick": tick, "task_id": ev.task_id, "agent_id": ev.agent_id, **{k: v for k, v in ev.payload.items() if k == "cell"}})

    def enqueue_task_added(self, task: dict[str, Any], tick: int) -> None:
        """A mission change is required work whether or not replanning is enabled: validate and register the
        task now; replanning only decides whether batch allocators (re)allocate it."""
        self.log.append("event", tick, event_type="TASK_ADDED", task_id=task.get("task_id"), agent_id=None)
        if self._add_task(task, tick):
            self._enqueue({"type": "TASK_ADDED", "tick": tick, "task_id": task.get("task_id")})

    def _enqueue(self, event: dict[str, Any]) -> None:
        if self.replan_enabled:
            self.pending_events.append(event)
        else:
            self.log.append("event_ignored", event["tick"], event_type=event["type"], reason="replanning disabled")

    # ----------------------------------------------------------------- replanning
    def process_events(self, tick: int) -> bool:
        if not self.pending_events or tick - self.last_replan_tick < self.settings.simulation.min_replan_interval_ticks:
            return False
        self.replan(tick)
        return True

    def replan(self, tick: int) -> None:
        started = time.perf_counter()
        events, self.pending_events = self.pending_events, []
        self.plan_version += 1
        for rt in self.tracker.by_state(TaskState.ASSIGNED, TaskState.READY):  # release unstarted work
            self.tracker.transition(rt.task_id, TaskState.PENDING, tick, clear_agent=True, reason="replan_release")
        for q in self.queues.values():
            q.clear()
        for rt in self.tracker.by_state(TaskState.FAILED):
            self._retry(rt, tick)
        for rt in self.tracker.by_state(TaskState.BLOCKED):
            if rt.task_id in self._retry_denied:
                continue
            if rt.attempts >= self.settings.simulation.max_task_attempts:
                self._retry_denied.add(rt.task_id)
                rt.reason = "RETRY_EXHAUSTED"
                continue
            self.tracker.transition(rt.task_id, TaskState.PENDING, tick, reason="replan_reevaluate")
        if self.allocator.mode == "batch":
            self.allocate_batch(tick, "replan")
        else:
            self.scan_infeasible(tick)
        self.last_replan_tick = tick
        unfinished = len([t for t in self.tracker.all() if t.state != TaskState.SUCCEEDED])
        self.log.append(
            "replan", tick, plan_version=self.plan_version, triggers=sorted({e["type"] for e in events}),
            first_event_tick=min(e["tick"] for e in events), unfinished=unfinished,
            compute_ms=round((time.perf_counter() - started) * 1000, 3),
        )

    def _retry(self, rt: TaskRuntime, tick: int) -> None:
        if rt.task_id in self._retry_denied:
            return
        policy = rt.spec.retry_policy
        deny = None
        if rt.attempts >= self.settings.simulation.max_task_attempts:
            deny = "RETRY_EXHAUSTED"
        elif rt.work_started and policy == RetryPolicy.NO_AUTOMATIC_RETRY.value:
            deny = "NO_AUTOMATIC_RETRY"
        if deny:
            self._retry_denied.add(rt.task_id)
            rt.reason = deny
            self.log.append("retry_denied", tick, task_id=rt.task_id, reason=deny)
            return
        if rt.work_started and policy == RetryPolicy.VERIFY_BEFORE_RETRY.value:
            found = rt.task_id in self.sim.registry
            self.log.append("verify_before_retry", tick, task_id=rt.task_id, completion_recorded=found)
            if found:  # impossible by construction (results are only stored on success)
                raise AssertionError(f"invariant violated: result recorded for FAILED task {rt.task_id}")
        rt.work_started = False
        self.tracker.transition(rt.task_id, TaskState.PENDING, tick, clear_agent=True, reason=f"retry:{policy}")

    def _add_task(self, raw: dict[str, Any], tick: int) -> bool:
        try:
            spec = TaskSpec.model_validate(raw)
            violations = self.validator.validate_tasks([spec], existing=self.tracker.specs(), check_coverage=False)
        except ValidationError as exc:
            violations = [Violation(V.SCHEMA_TYPE_ERROR, str(exc.errors()[0]["msg"]))]
            spec = None
        if violations or spec is None:
            self.log.append("plan_rejected", tick, source="task_added", codes=[v.code for v in violations], messages=[v.message for v in violations])
            return False
        self.tracker.add(spec)
        self.log.append("task_added", tick, task_id=spec.task_id, plan_version=self.plan_version)
        return True
