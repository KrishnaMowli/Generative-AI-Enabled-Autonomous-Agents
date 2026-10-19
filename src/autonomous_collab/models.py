"""Validated data contracts.

Plan data (``TaskSpec``) is what the LLM may propose. Runtime fields (assigned agent,
state, results, observed position) live elsewhere and are never read from model output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Cell = tuple[int, int]


class TaskState(str, Enum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    READY = "READY"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class AgentStatus(str, Enum):
    IDLE = "IDLE"
    ASSIGNED = "ASSIGNED"  # reserved for dispatch bookkeeping; MOVING/EXECUTING are the active states
    MOVING = "MOVING"
    EXECUTING = "EXECUTING"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"  # e.g. battery below reserve / deadlocked and withdrawn


class RetryPolicy(str, Enum):
    SAFE_TO_RETRY = "safe_to_retry"
    VERIFY_BEFORE_RETRY = "verify_before_retry"
    NO_AUTOMATIC_RETRY = "no_automatic_retry"


class PlanStatus(str, Enum):
    PLAN = "plan"
    NEEDS_CLARIFICATION = "needs_clarification"
    ERROR = "error"


class V:
    """Stable violation / rejection codes."""

    # schema
    MALFORMED_JSON = "MALFORMED_JSON"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    SCHEMA_NOT_OBJECT = "SCHEMA_NOT_OBJECT"
    SCHEMA_MISSING_FIELD = "SCHEMA_MISSING_FIELD"
    SCHEMA_EXTRA_FIELD = "SCHEMA_EXTRA_FIELD"
    SCHEMA_TYPE_ERROR = "SCHEMA_TYPE_ERROR"
    SCHEMA_BAD_STATUS = "SCHEMA_BAD_STATUS"
    EMPTY_PLAN = "EMPTY_PLAN"
    # semantic
    TASK_LIMIT_EXCEEDED = "TASK_LIMIT_EXCEEDED"
    TASK_ID_INVALID = "TASK_ID_INVALID"
    DUPLICATE_TASK_ID = "DUPLICATE_TASK_ID"
    UNKNOWN_TASK_TYPE = "UNKNOWN_TASK_TYPE"
    UNKNOWN_ZONE = "UNKNOWN_ZONE"
    UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
    CAPABILITY_TYPE_MISMATCH = "CAPABILITY_TYPE_MISMATCH"
    PRIORITY_RANGE = "PRIORITY_RANGE"
    DURATION_RANGE = "DURATION_RANGE"
    RETRY_POLICY_INVALID = "RETRY_POLICY_INVALID"
    ACCEPTANCE_UNRECOGNIZED = "ACCEPTANCE_UNRECOGNIZED"
    UNKNOWN_DEPENDENCY = "UNKNOWN_DEPENDENCY"
    SELF_DEPENDENCY = "SELF_DEPENDENCY"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    GRAPH_DEPTH_EXCEEDED = "GRAPH_DEPTH_EXCEEDED"
    REPORT_DEPENDENCY_MISSING = "REPORT_DEPENDENCY_MISSING"
    REPORT_NOT_TERMINAL = "REPORT_NOT_TERMINAL"
    NO_CAPABLE_AGENT = "NO_CAPABLE_AGENT"
    UNREACHABLE_ZONE = "UNREACHABLE_ZONE"
    COVERAGE_MISSING = "COVERAGE_MISSING"
    UNREQUESTED_TASK = "UNREQUESTED_TASK"
    # mission / planner
    MISSION_INJECTION_SUSPECTED = "MISSION_INJECTION_SUSPECTED"
    MISSION_TOO_LONG = "MISSION_TOO_LONG"
    MISSION_EMPTY = "MISSION_EMPTY"
    PLANNER_CLARIFICATION = "PLANNER_CLARIFICATION"
    PLANNER_ERROR_STATUS = "PLANNER_ERROR_STATUS"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    # safety monitor
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    DEPENDENCY_NOT_SATISFIED = "DEPENDENCY_NOT_SATISFIED"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    AGENT_NOT_AVAILABLE = "AGENT_NOT_AVAILABLE"
    STALE_STATE = "STALE_STATE"
    NO_PATH = "NO_PATH"
    ROUTE_BLOCKED = "ROUTE_BLOCKED"
    ILLEGAL_MOVE = "ILLEGAL_MOVE"
    NODE_OCCUPIED = "NODE_OCCUPIED"
    NODE_RESERVED = "NODE_RESERVED"
    EDGE_SWAP = "EDGE_SWAP"
    BATTERY_RESERVE = "BATTERY_RESERVE"
    BATTERY_EXHAUSTED = "BATTERY_EXHAUSTED"
    # allocation (unassigned reasons)
    NO_AVAILABLE_AGENT = "NO_AVAILABLE_AGENT"
    NO_REACHABLE_PATH = "NO_REACHABLE_PATH"
    BATTERY_LIMIT = "BATTERY_LIMIT"
    EXCLUDED_AFTER_REJECTION = "EXCLUDED_AFTER_REJECTION"
    WORKLOAD_LIMIT = "WORKLOAD_LIMIT"


@dataclass(frozen=True)
class Violation:
    """A stable-coded rejection reason."""

    code: str
    message: str
    task_id: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def __str__(self) -> str:
        prefix = f"[{self.code}]"
        if self.task_id:
            prefix += f" task={self.task_id}"
        return f"{prefix} {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "task_id": self.task_id}


# ---------------------------------------------------------------------------
# Plan contracts (LLM-proposed)
# ---------------------------------------------------------------------------
class TaskSpec(BaseModel):
    """Candidate task proposed by the planner. Strict: no extra fields, no coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str
    task_type: str
    target_zone: str
    required_capabilities: list[str]
    depends_on: list[str] = Field(default_factory=list)
    priority: int
    estimated_duration_s: float
    acceptance_criteria: list[str]
    retry_policy: str = RetryPolicy.SAFE_TO_RETRY.value
    mandatory: bool = True


class CandidatePlan(BaseModel):
    """Planner response envelope. Only status=='plan' proceeds to semantic validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["plan", "needs_clarification", "error"]
    tasks: list[TaskSpec] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Scenario / configuration contracts
# ---------------------------------------------------------------------------
class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    capabilities: list[str]
    start: tuple[int, int]
    battery: float = Field(ge=0, le=100)
    battery_reserve: float = Field(default=10.0, ge=0, le=100)
    energy_per_move: Optional[float] = Field(default=None, gt=0)


class WorldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grid: list[str]  # '#': obstacle, anything else: free cell
    zones: dict[str, tuple[int, int]]
    report_zone: str = "HQ"


class RequiredOutcome(BaseModel):
    """One line of the deterministic mission checklist (``zone=None`` means any zone)."""

    model_config = ConfigDict(extra="forbid")

    task_type: str
    zone: Optional[str] = None


class InjectedEvent(BaseModel):
    """Deterministic fault / change injected by the scenario."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["AGENT_FAILED", "CELL_BLOCKED", "TASK_ADDED"]
    tick: Optional[int] = None
    tick_window: Optional[tuple[int, int]] = None  # sampled uniformly from the seeded stream
    agent_id: Optional[str] = None
    cell: Optional[tuple[int, int]] = None
    task: Optional[dict[str, Any]] = None
    # conditional trigger: fire when this agent is executing (optionally this task) past progress
    when_agent_executing: Optional[str] = None
    when_task_id: Optional[str] = None
    when_progress: float = Field(default=0.5, ge=0, le=1)


class Randomization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_jitter: int = Field(default=0, ge=0)  # manhattan radius for random agent starts
    duration_jitter: float = Field(default=0.0, ge=0, lt=1)  # +/- fraction on task durations
    battery_jitter: float = Field(default=0.0, ge=0, lt=1)  # +/- fraction on initial battery


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    mission_text: str
    required_outcomes: list[RequiredOutcome] = Field(default_factory=list)
    world: WorldSpec
    agents: list[AgentSpec]
    events: list[InjectedEvent] = Field(default_factory=list)
    manual_plan: list[dict[str, Any]] = Field(default_factory=list)
    planner_faults: list[str] = Field(default_factory=list)
    planner_faults_persistent: bool = False
    randomization: Randomization = Field(default_factory=Randomization)
    settings_overrides: dict[str, Any] = Field(default_factory=dict)
    expectation: dict[str, Any] = Field(default_factory=dict)


def json_schema_for_task() -> dict[str, Any]:
    return TaskSpec.model_json_schema()


def json_schema_for_plan() -> dict[str, Any]:
    return CandidatePlan.model_json_schema()
