import random
from collections import deque

import pytest

from autonomous_collab.environment import GridWorld, manhattan
from helpers import make_world


def bfs(world, start, goal):
    if start == goal:
        return 0
    seen, q = {start}, deque([(start, 0)])
    while q:
        cur, d = q.popleft()
        for n in world.neighbors(cur):
            if n == goal:
                return d + 1
            if n not in seen:
                seen.add(n)
                q.append((n, d + 1))
    return None


def random_world(rng, rows=9, cols=9, density=0.25):
    obstacles = {(r, c) for r in range(rows) for c in range(cols) if rng.random() < density}
    return GridWorld(rows, cols, obstacles, {})


def test_astar_matches_bfs_on_random_grids():
    rng = random.Random(7)
    checked = unreachable = 0
    for _ in range(60):
        w = random_world(rng)
        free = w.free_cells()
        if len(free) < 2:
            continue
        a, b = rng.sample(free, 2)
        path = w.shortest_path(a, b)
        expected = bfs(w, a, b)
        if expected is None:
            assert path is None
            unreachable += 1
        else:
            assert path is not None and len(path) - 1 == expected
            assert path[0] == a and path[-1] == b and w.validate_path(path) == []
        checked += 1
    assert checked > 40 and unreachable > 0  # the test exercises unreachable goals too


def test_path_cases():
    w = make_world(["....", ".##.", "...."], {"HQ": (0, 0)})
    assert w.shortest_path((0, 0), (0, 0)) == [(0, 0)]
    assert w.shortest_path((0, 0), (1, 1)) is None  # obstacle goal
    assert w.shortest_path((0, 0), (9, 9)) is None  # out of bounds goal
    assert w.path_length((0, 0), (2, 3)) == 5
    assert w.shortest_path((0, 0), (2, 3)) == w.shortest_path((0, 0), (2, 3))  # deterministic


def test_dynamic_block_changes_version_cache_and_route():
    w = make_world(["...", "...", "..."], {"HQ": (0, 0)})
    v0 = w.version
    assert w.path_length((0, 0), (0, 2)) == 2
    assert w.block_cell((0, 1), "test") and w.version == v0 + 1
    assert w.path_length((0, 0), (0, 2)) == 4  # cache invalidated: detour
    assert not w.block_cell((0, 1)) and not w.block_cell((5, 5)) and w.version == v0 + 1
    assert w.unblock_cell((0, 1)) and w.path_length((0, 0), (0, 2)) == 2


def test_blocked_goal_and_extra_blocked_and_start_on_blocked_cell():
    w = make_world(["...", "...", "..."], {"HQ": (0, 0)})
    w.block_cell((0, 2))
    assert w.shortest_path((0, 0), (0, 2)) is None
    assert w.shortest_path((0, 2), (0, 0)) is not None  # may leave a cell that became blocked
    assert w.shortest_path((0, 0), (2, 2), extra_blocked={(2, 2)}) is None
    p = w.shortest_path((0, 0), (2, 2), extra_blocked={(1, 1)})
    assert p is not None and (1, 1) not in p and len(p) - 1 == 4
    assert w.shortest_path((0, 0), (2, 2), extra_blocked={(1, 1), (1, 0)}) is None  # (0,2) is blocked too


def test_validate_path_reports_every_defect():
    w = make_world(["...", ".#.", "..."], {"HQ": (0, 0)})
    probs = w.validate_path([(0, 0), (1, 1), (1, 2), (3, 3)])
    assert any("not passable" in p for p in probs) and any("non-adjacent" in p for p in probs)
    assert w.validate_path([]) == ["empty path"]
    assert w.validate_path([(0, 0), (0, 1)], extra_blocked={(0, 1)})


def test_copy_is_independent():
    w = make_world(["...", "..."], {"HQ": (0, 0)})
    c = w.copy()
    c.block_cell((0, 1))
    assert w.is_passable((0, 1)) and not c.is_passable((0, 1))
    assert manhattan((0, 0), (2, 3)) == 5
