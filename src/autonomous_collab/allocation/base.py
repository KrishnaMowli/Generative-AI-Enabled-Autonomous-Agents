"""Allocator interface and shared containers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ..models import TaskSpec
from ..views import AgentView
from .cost_model import CostModel


@dataclass
class AllocationContext:
    tasks: Sequence[TaskSpec]                      # tasks to place in this cycle
    agents: Sequence[AgentView]
    cost_model: CostModel
    excluded: Mapping[str, set[str]] = field(default_factory=dict)  # task_id -> agents ruled out
    workload_limit_ticks: Optional[float] = None


@dataclass
class AllocationResult:
    assignments: dict[str, str] = field(default_factory=dict)   # task_id -> agent_id
    unassigned: dict[str, str] = field(default_factory=dict)    # task_id -> reason code
    objective: Optional[float] = None                           # allocator-specific objective
    status: str = "ok"
    details: dict[str, Any] = field(default_factory=dict)


class Allocator(ABC):
    """``mode='batch'``: place every pending task up front (and again after a replan).
    ``mode='online'``: called each time agents are idle and tasks are ready (one-to-one cycle)."""

    name: str = "base"
    mode: str = "batch"

    @abstractmethod
    def allocate(self, ctx: AllocationContext) -> AllocationResult:
        ...
