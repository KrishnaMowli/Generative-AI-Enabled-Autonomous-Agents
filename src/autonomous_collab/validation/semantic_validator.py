"""Semantic plan validation: allowlists, dependency DAG, feasibility, mission coverage.

A syntactically valid plan is not necessarily a valid plan. Every candidate - from a real
LLM, the mock, or a manual fixture - must pass this validator before allocation. The
validator never repairs a plan; it only returns stable-coded violations.
"""
from __future__ import annotations

import math
import re
from typing import Optional, Sequence

from ..config import Settings
from ..environment import GridWorld
from ..models import AgentSpec, RequiredOutcome, RetryPolicy, TaskSpec, V, Violation
from ..task_graph import find_cycle, graph_depth, transitive_dependencies

_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_RETRY = {p.value for p in RetryPolicy}
_INSPECTION_TYPES = ("visual_inspection", "environmental_reading")


class PlanValidator:
    def __init__(
        self,
        world: GridWorld,
        agents: Sequence[AgentSpec],
        settings: Settings,
        required_outcomes: Optional[Sequence[RequiredOutcome]] = None,
    ) -> None:
        self.world = world
        self.agents = list(agents)
        self.settings = settings
        self.required_outcomes = list(required_outcomes or [])

    # ------------------------------------------------------------------ main
    def validate_tasks(
        self,
        tasks: Sequence[TaskSpec],
        *,
        existing: Sequence[TaskSpec] = (),
        check_coverage: bool = True,
    ) -> list[Violation]:
        """Validate ``tasks`` (optionally added to an ``existing`` already-accepted set)."""
        lim = self.settings.planner_limits
        out: list[Violation] = []
        if not tasks:
            return [Violation(V.EMPTY_PLAN, "plan contains no tasks")]
        if len(tasks) + len(existing) > lim.max_tasks:
            out.append(Violation(V.TASK_LIMIT_EXCEEDED, f"{len(tasks) + len(existing)} tasks exceed limit {lim.max_tasks}"))
            return out

        existing_ids = {t.task_id for t in existing}
        seen: set[str] = set()
        for t in tasks:
            tid = t.task_id
            if not _ID_RE.match(tid):
                out.append(Violation(V.TASK_ID_INVALID, f"task_id {tid!r} must match {_ID_RE.pattern}", tid))
            if tid in seen or tid in existing_ids:
                out.append(Violation(V.DUPLICATE_TASK_ID, f"duplicate task_id {tid!r}", tid))
            seen.add(tid)
            out.extend(self._check_fields(t))

        all_tasks = list(existing) + list(tasks)
        graph_violations = self._check_graph(tasks, all_tasks)
        out.extend(graph_violations)
        structural = {V.UNKNOWN_DEPENDENCY, V.DEPENDENCY_CYCLE, V.SELF_DEPENDENCY, V.DUPLICATE_TASK_ID}
        if check_coverage and not any(v.code in structural for v in out):
            out.extend(self._check_report_semantics(all_tasks))
        out.extend(self._check_feasibility(tasks))
        if check_coverage and self.required_outcomes:
            out.extend(self._check_coverage(tasks))
        return out

    # ----------------------------------------------------------- field rules
    def _check_fields(self, t: TaskSpec) -> list[Violation]:
        s, lim = self.settings, self.settings.planner_limits
        out: list[Violation] = []
        spec = s.task_catalog.get(t.task_type)
        if spec is None:
            out.append(Violation(V.UNKNOWN_TASK_TYPE, f"task_type {t.task_type!r} is not in the allowlist {sorted(s.task_catalog)}", t.task_id))
        if t.target_zone not in self.world.zones:
            out.append(Violation(V.UNKNOWN_ZONE, f"target_zone {t.target_zone!r} is not in the world catalogue {sorted(self.world.zones)}", t.task_id))
        for cap in t.required_capabilities:
            if cap not in s.capabilities:
                out.append(Violation(V.UNKNOWN_CAPABILITY, f"capability {cap!r} is not in {s.capabilities}", t.task_id))
        if spec is not None and spec.capability not in t.required_capabilities:
            out.append(Violation(V.CAPABILITY_TYPE_MISMATCH, f"{t.task_type} requires capability {spec.capability!r}", t.task_id))
        if not (lim.min_priority <= t.priority <= lim.max_priority):
            out.append(Violation(V.PRIORITY_RANGE, f"priority {t.priority} outside [{lim.min_priority}, {lim.max_priority}]", t.task_id))
        if not math.isfinite(t.estimated_duration_s) or not (0 < t.estimated_duration_s <= lim.max_duration_s):
            out.append(Violation(V.DURATION_RANGE, f"estimated_duration_s {t.estimated_duration_s} outside (0, {lim.max_duration_s}]", t.task_id))
        if t.retry_policy not in _RETRY:
            out.append(Violation(V.RETRY_POLICY_INVALID, f"retry_policy {t.retry_policy!r} not in {sorted(_RETRY)}", t.task_id))
        recognised = {c.acceptance_criterion for c in s.task_catalog.values()}
        if not t.acceptance_criteria:
            out.append(Violation(V.ACCEPTANCE_UNRECOGNIZED, "acceptance_criteria must not be empty", t.task_id))
        for crit in t.acceptance_criteria:
            if crit not in recognised:
                out.append(Violation(V.ACCEPTANCE_UNRECOGNIZED, f"acceptance criterion {crit!r} is not a recognised postcondition", t.task_id))
        if spec is not None and spec.acceptance_criterion not in t.acceptance_criteria:
            out.append(Violation(V.ACCEPTANCE_UNRECOGNIZED, f"{t.task_type} must include criterion {spec.acceptance_criterion!r}", t.task_id))
        return out

    # ----------------------------------------------------------- graph rules
    def _check_graph(self, new_tasks: Sequence[TaskSpec], all_tasks: Sequence[TaskSpec]) -> list[Violation]:
        out: list[Violation] = []
        ids = {t.task_id for t in all_tasks}
        for t in new_tasks:
            for dep in t.depends_on:
                if dep == t.task_id:
                    out.append(Violation(V.SELF_DEPENDENCY, "task depends on itself", t.task_id))
                elif dep not in ids:
                    out.append(Violation(V.UNKNOWN_DEPENDENCY, f"depends on unknown task {dep!r}", t.task_id))
        deps: dict[str, list[str]] = {}
        for t in all_tasks:
            deps.setdefault(t.task_id, [])
            deps[t.task_id] = list(dict.fromkeys(deps[t.task_id] + list(t.depends_on)))
        cycle = find_cycle({k: [d for d in v if d != k] for k, v in deps.items()})
        if cycle:
            out.append(Violation(V.DEPENDENCY_CYCLE, "dependency cycle: " + " -> ".join(cycle)))
        elif not any(v.code in (V.UNKNOWN_DEPENDENCY, V.SELF_DEPENDENCY) for v in out):
            depth = graph_depth(deps)
            if depth > self.settings.planner_limits.max_graph_depth:
                out.append(Violation(V.GRAPH_DEPTH_EXCEEDED, f"dependency depth {depth} exceeds {self.settings.planner_limits.max_graph_depth}"))
        return out

    def _check_report_semantics(self, all_tasks: Sequence[TaskSpec]) -> list[Violation]:
        out: list[Violation] = []
        deps = {t.task_id: list(t.depends_on) for t in all_tasks}
        report_ids = {t.task_id for t in all_tasks if t.task_type == "compile_report"}
        data_ids = {t.task_id for t in all_tasks if t.task_type in _INSPECTION_TYPES}
        for rid in sorted(report_ids):
            missing = sorted(data_ids - transitive_dependencies(deps, rid))
            if missing:
                out.append(Violation(V.REPORT_DEPENDENCY_MISSING, f"report must run after all inspections/readings; missing prerequisites {missing}", rid))
        for t in all_tasks:
            if t.task_id in report_ids:
                continue
            if transitive_dependencies(deps, t.task_id) & report_ids:
                out.append(Violation(V.REPORT_NOT_TERMINAL, "a non-report task must not depend on a report", t.task_id))
        return out

    # ----------------------------------------------------------- feasibility
    def _check_feasibility(self, tasks: Sequence[TaskSpec]) -> list[Violation]:
        out: list[Violation] = []
        for t in tasks:
            if t.target_zone not in self.world.zones:
                continue  # already reported as UNKNOWN_ZONE
            needed = set(t.required_capabilities)
            if any(c not in self.settings.capabilities for c in needed):
                continue  # already reported as UNKNOWN_CAPABILITY
            capable = [a for a in self.agents if needed <= set(a.capabilities)]
            if not capable:
                out.append(Violation(V.NO_CAPABLE_AGENT, f"no agent has capabilities {sorted(needed)}", t.task_id))
                continue
            goal = self.world.zone_cell(t.target_zone)
            if not any(self.world.shortest_path(tuple(a.start), goal) is not None for a in capable):
                out.append(Violation(V.UNREACHABLE_ZONE, f"zone {t.target_zone} is unreachable for every capable agent on the current map", t.task_id))
        return out

    # ------------------------------------------------------------- coverage
    @staticmethod
    def _matches(t: TaskSpec, o: RequiredOutcome) -> bool:
        return t.task_type == o.task_type and (o.zone is None or t.target_zone == o.zone)

    def _check_coverage(self, tasks: Sequence[TaskSpec]) -> list[Violation]:
        out: list[Violation] = []
        for outcome in self.required_outcomes:
            if not any(self._matches(t, outcome) for t in tasks):
                zone = outcome.zone if outcome.zone else "any zone"
                out.append(Violation(V.COVERAGE_MISSING, f"required outcome not covered: {outcome.task_type} at {zone}"))
        for t in tasks:
            if not any(self._matches(t, o) for o in self.required_outcomes):
                out.append(Violation(V.UNREQUESTED_TASK, f"task {t.task_id} ({t.task_type} @ {t.target_zone}) is not part of the requested mission", t.task_id))
        return out
