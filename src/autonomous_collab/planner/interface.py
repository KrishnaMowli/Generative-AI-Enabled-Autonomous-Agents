"""Provider-independent planner contracts. The rest of the system depends only on these."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from ..models import TaskSpec, Violation


@dataclass
class PlanningRequest:
    mission_id: str
    mission_text: str
    zones: list[str]
    report_zone: str
    allowed_task_types: dict[str, str]     # task_type -> required capability
    capability_catalogue: list[str]
    required_outcomes: list[dict[str, Any]]
    task_limit: int


@dataclass
class AttemptRecord:
    index: int
    outcome: str                            # accepted | rejected | clarification | planner_error | provider_error
    violation_codes: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    raw_text: str = ""
    provider: str = ""
    model: str = ""


@dataclass
class PlanningOutcome:
    status: str                             # plan | needs_clarification | rejected | provider_error | planner_error
    tasks: list[TaskSpec] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    prompt_version: str = ""

    @property
    def calls(self) -> int:
        return len(self.attempts)


class PlanValidatorLike(Protocol):
    def validate_tasks(self, tasks: list[TaskSpec], **kwargs: Any) -> list[Violation]: ...


class Planner(Protocol):
    def plan(self, request: PlanningRequest, validator: PlanValidatorLike) -> PlanningOutcome: ...
