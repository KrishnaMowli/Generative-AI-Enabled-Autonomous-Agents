"""Runtime task state machine and tracker (owned by the coordinator)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import IllegalTransitionError
from .logging_utils import EventLog
from .models import TaskSpec, TaskState

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.ASSIGNED, TaskState.BLOCKED}),
    TaskState.ASSIGNED: frozenset({TaskState.READY, TaskState.PENDING}),
    TaskState.READY: frozenset({TaskState.EXECUTING, TaskState.PENDING, TaskState.BLOCKED}),
    TaskState.EXECUTING: frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.BLOCKED}),
    TaskState.SUCCEEDED: frozenset(),  # terminal and immutable
    TaskState.FAILED: frozenset({TaskState.PENDING}),  # only via the replanning manager
    TaskState.BLOCKED: frozenset({TaskState.PENDING}),  # only via the replanning manager
}


@dataclass
class TaskRuntime:
    spec: TaskSpec
    state: TaskState = TaskState.PENDING
    agent_id: Optional[str] = None
    attempts: int = 0
    work_started: bool = False
    result: Optional[dict[str, Any]] = None
    start_tick: Optional[int] = None
    end_tick: Optional[int] = None
    reason: str = ""
    assigned_version: int = 0
    excluded_agents: set[str] = field(default_factory=set)
    exclusion_causes: set[str] = field(default_factory=set)

    @property
    def task_id(self) -> str:
        return self.spec.task_id


class TaskTracker:
    def __init__(self, specs: list[TaskSpec], log: EventLog) -> None:
        self._tasks: dict[str, TaskRuntime] = {}
        self._log = log
        for spec in specs:
            self.add(spec)

    # ------------------------------------------------------------- accessors
    def add(self, spec: TaskSpec) -> TaskRuntime:
        if spec.task_id in self._tasks:
            raise ValueError(f"duplicate task id {spec.task_id!r}")
        rt = TaskRuntime(spec=spec)
        self._tasks[spec.task_id] = rt
        return rt

    def get(self, task_id: str) -> TaskRuntime:
        return self._tasks[task_id]

    def all(self) -> list[TaskRuntime]:
        return [self._tasks[k] for k in sorted(self._tasks)]

    def ids(self) -> list[str]:
        return sorted(self._tasks)

    def specs(self) -> list[TaskSpec]:
        return [t.spec for t in self.all()]

    def by_state(self, *states: TaskState) -> list[TaskRuntime]:
        wanted = set(states)
        return [t for t in self.all() if t.state in wanted]

    def deps_succeeded(self, task_id: str) -> bool:
        return all(self._tasks[d].state == TaskState.SUCCEEDED for d in self._tasks[task_id].spec.depends_on)

    def deps_map(self) -> dict[str, list[str]]:
        return {tid: list(t.spec.depends_on) for tid, t in self._tasks.items()}

    # ------------------------------------------------------------ transitions
    def transition(
        self,
        task_id: str,
        to: TaskState,
        tick: int,
        *,
        agent_id: Optional[str] = None,
        clear_agent: bool = False,
        reason: str = "",
    ) -> TaskRuntime:
        task = self._tasks[task_id]
        if to not in ALLOWED_TRANSITIONS[task.state]:
            raise IllegalTransitionError(
                f"task {task_id}: illegal transition {task.state.value} -> {to.value}"
            )
        previous = task.state
        task.state = to
        if agent_id is not None:
            task.agent_id = agent_id
        if clear_agent:
            task.agent_id = None
        task.reason = reason
        if to == TaskState.EXECUTING and task.start_tick is None:
            task.start_tick = tick
        if to in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.BLOCKED):
            task.end_tick = tick
        self._log.append(
            "task_transition",
            tick,
            task_id=task_id,
            from_state=previous.value,
            to_state=to.value,
            agent_id=task.agent_id,
            reason=reason,
        )
        return task
