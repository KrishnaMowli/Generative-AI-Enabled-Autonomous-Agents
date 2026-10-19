"""Deterministic grid world with obstacles, dynamic blocked cells and A* navigation."""
from __future__ import annotations

import heapq
from typing import Iterable, Optional

from .errors import ConfigError
from .models import Cell, WorldSpec

# Fixed neighbour order (up, right, down, left) keeps path search deterministic.
_DELTAS: tuple[Cell, ...] = ((-1, 0), (0, 1), (1, 0), (0, -1))


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


class GridWorld:
    """4-connected unit-cost grid. ``version`` increments whenever passability changes."""

    def __init__(self, rows: int, cols: int, obstacles: Iterable[Cell], zones: dict[str, Cell]) -> None:
        self.rows = rows
        self.cols = cols
        self.obstacles: frozenset[Cell] = frozenset(obstacles)
        self.zones: dict[str, Cell] = dict(zones)
        self.blocked: dict[Cell, str] = {}
        self.version = 1
        self._path_cache: dict[tuple[Cell, Cell], Optional[tuple[Cell, ...]]] = {}

    # ------------------------------------------------------------------ build
    @classmethod
    def from_spec(cls, spec: WorldSpec) -> "GridWorld":
        if not spec.grid:
            raise ConfigError("world.grid must have at least one row")
        width = len(spec.grid[0])
        if width == 0 or any(len(row) != width for row in spec.grid):
            raise ConfigError("world.grid rows must be non-empty and of equal length")
        obstacles = {(r, c) for r, row in enumerate(spec.grid) for c, ch in enumerate(row) if ch == "#"}
        world = cls(len(spec.grid), width, obstacles, {k: tuple(v) for k, v in spec.zones.items()})
        problems = []
        seen: dict[Cell, str] = {}
        for name, cell in world.zones.items():
            if not world.in_bounds(cell):
                problems.append(f"zone {name} at {cell} is outside the grid")
            elif cell in world.obstacles:
                problems.append(f"zone {name} at {cell} is on an obstacle")
            elif cell in seen:
                problems.append(f"zones {seen[cell]} and {name} share cell {cell}")
            seen[cell] = name
        if spec.report_zone not in world.zones:
            problems.append(f"report_zone {spec.report_zone!r} is not a defined zone")
        if problems:
            raise ConfigError("invalid world: " + "; ".join(problems))
        return world

    def copy(self) -> "GridWorld":
        clone = GridWorld(self.rows, self.cols, self.obstacles, self.zones)
        clone.blocked = dict(self.blocked)
        clone.version = self.version
        return clone

    # --------------------------------------------------------------- queries
    def in_bounds(self, cell: Cell) -> bool:
        return 0 <= cell[0] < self.rows and 0 <= cell[1] < self.cols

    def is_passable(self, cell: Cell) -> bool:
        return self.in_bounds(cell) and cell not in self.obstacles and cell not in self.blocked

    def neighbors(self, cell: Cell) -> list[Cell]:
        out = []
        for dr, dc in _DELTAS:
            nxt = (cell[0] + dr, cell[1] + dc)
            if self.is_passable(nxt):
                out.append(nxt)
        return out

    def zone_cell(self, zone: str) -> Cell:
        try:
            return self.zones[zone]
        except KeyError as exc:
            raise KeyError(f"unknown zone {zone!r}") from exc

    def free_cells(self) -> list[Cell]:
        return [(r, c) for r in range(self.rows) for c in range(self.cols) if self.is_passable((r, c))]

    # ------------------------------------------------------------- mutations
    def block_cell(self, cell: Cell, reason: str = "blocked") -> bool:
        """Block a cell dynamically. Returns False if nothing changed."""
        if not self.in_bounds(cell) or cell in self.obstacles or cell in self.blocked:
            return False
        self.blocked[cell] = reason
        self._bump()
        return True

    def unblock_cell(self, cell: Cell) -> bool:
        if cell not in self.blocked:
            return False
        del self.blocked[cell]
        self._bump()
        return True

    def _bump(self) -> None:
        self.version += 1
        self._path_cache.clear()

    # ------------------------------------------------------------ navigation
    def shortest_path(
        self, start: Cell, goal: Cell, extra_blocked: frozenset[Cell] | set[Cell] = frozenset()
    ) -> Optional[list[Cell]]:
        """A* with Manhattan heuristic (admissible and consistent on a unit-cost 4-grid).

        The start cell is always allowed (an agent may stand on a cell that became blocked);
        the goal must be passable and not in ``extra_blocked``. Returns ``None`` if unreachable.
        """
        if start == goal:
            return [start] if self.in_bounds(start) else None
        if not self.is_passable(goal) or goal in extra_blocked:
            return None
        use_cache = not extra_blocked
        key = (start, goal)
        if use_cache and key in self._path_cache:
            cached = self._path_cache[key]
            return list(cached) if cached is not None else None

        counter = 0
        open_heap: list[tuple[int, int, int, Cell]] = [(manhattan(start, goal), manhattan(start, goal), counter, start)]
        g_score = {start: 0}
        came_from: dict[Cell, Cell] = {}
        closed: set[Cell] = set()
        result: Optional[list[Cell]] = None
        while open_heap:
            _, _, _, current = heapq.heappop(open_heap)
            if current in closed:
                continue
            if current == goal:
                path = [current]
                while path[-1] in came_from:
                    path.append(came_from[path[-1]])
                path.reverse()
                result = path
                break
            closed.add(current)
            for nxt in self.neighbors(current):
                if nxt in extra_blocked or nxt in closed:
                    continue
                tentative = g_score[current] + 1
                if tentative < g_score.get(nxt, 1 << 30):
                    g_score[nxt] = tentative
                    came_from[nxt] = current
                    counter += 1
                    h = manhattan(nxt, goal)
                    heapq.heappush(open_heap, (tentative + h, h, counter, nxt))
        if use_cache:
            self._path_cache[key] = tuple(result) if result is not None else None
        return result

    def path_length(self, start: Cell, goal: Cell) -> Optional[int]:
        path = self.shortest_path(start, goal)
        return None if path is None else len(path) - 1

    def validate_path(
        self, path: list[Cell], extra_blocked: frozenset[Cell] | set[Cell] = frozenset()
    ) -> list[str]:
        """Return a list of problems (empty = path is legal on the current map)."""
        problems: list[str] = []
        if not path:
            return ["empty path"]
        if not self.in_bounds(path[0]):
            problems.append(f"start {path[0]} out of bounds")
        for prev, cur in zip(path, path[1:]):
            if manhattan(prev, cur) != 1:
                problems.append(f"non-adjacent step {prev}->{cur}")
            if not self.is_passable(cur):
                problems.append(f"cell {cur} is not passable")
            if cur in extra_blocked:
                problems.append(f"cell {cur} is reserved/blocked")
        return problems
