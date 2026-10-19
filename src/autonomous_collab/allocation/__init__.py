"""Task allocators: greedy and nearest baselines, Hungarian (online) and MILP (batch)."""
from __future__ import annotations

from ..errors import ConfigError
from .base import AllocationContext, AllocationResult, Allocator
from .greedy import GreedyAllocator
from .hungarian import HungarianAllocator
from .milp import MilpAllocator
from .nearest import NearestFeasibleAllocator

ALLOCATORS: dict[str, type[Allocator]] = {
    "greedy": GreedyAllocator,
    "milp": MilpAllocator,
    "nearest": NearestFeasibleAllocator,
    "hungarian": HungarianAllocator,
}


def make_allocator(name: str) -> Allocator:
    try:
        return ALLOCATORS[name]()
    except KeyError as exc:
        raise ConfigError(f"unknown allocator {name!r}; choose from {sorted(ALLOCATORS)}") from exc


__all__ = ["ALLOCATORS", "AllocationContext", "AllocationResult", "Allocator", "make_allocator"]
