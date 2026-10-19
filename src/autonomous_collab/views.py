"""Immutable snapshots passed between coordinator, allocator and safety monitor."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Cell


@dataclass(frozen=True)
class AgentView:
    """What the allocator / monitor may know about an agent at decision time.

    ``position``/``battery``/``committed_ticks`` are *virtual* start values: for an agent that
    is mid-task they describe the state after finishing that task.
    """

    agent_id: str
    capabilities: frozenset[str]
    position: Cell
    battery: float
    reserve: float
    energy_per_move: float
    available: bool = True  # False for FAILED / UNAVAILABLE agents
    idle: bool = True       # True when the agent can take a new task right now
    committed_ticks: float = 0.0
