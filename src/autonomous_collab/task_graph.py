"""Pure dependency-graph utilities (no state, no I/O)."""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

Deps = Mapping[str, Sequence[str]]


def find_cycle(deps: Deps) -> Optional[list[str]]:
    """Return one dependency cycle as ``[a, b, ..., a]`` or ``None``.

    Iterative DFS (no recursion limit issues). Edges point task -> prerequisite; references to
    unknown tasks are ignored here (reported separately by the validator).
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {t: WHITE for t in deps}
    for root in sorted(deps):
        if colour[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = [root]
        colour[root] = GREY
        while stack:
            node, idx = stack[-1]
            children = [d for d in deps[node] if d in colour]
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                child = children[idx]
                if colour[child] == GREY:
                    return path[path.index(child):] + [child]
                if colour[child] == WHITE:
                    colour[child] = GREY
                    stack.append((child, 0))
                    path.append(child)
            else:
                colour[node] = BLACK
                stack.pop()
                path.pop()
    return None


def topological_order(deps: Deps, priority: Optional[Mapping[str, int]] = None) -> list[str]:
    """Kahn's algorithm; prerequisites first. Ties: higher priority first, then task id.

    Raises ``ValueError`` if the graph has a cycle. Unknown prerequisites are ignored.
    """
    priority = priority or {}
    known = set(deps)
    indeg = {t: len([d for d in set(deps[t]) if d in known]) for t in deps}
    dependents: dict[str, list[str]] = {t: [] for t in deps}
    for t in deps:
        for d in set(deps[t]):
            if d in known:
                dependents[d].append(t)
    ready = sorted([t for t, n in indeg.items() if n == 0], key=lambda t: (-priority.get(t, 0), t))
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in dependents[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda t: (-priority.get(t, 0), t))
    if len(order) != len(deps):
        raise ValueError("dependency graph contains a cycle")
    return order


def graph_depth(deps: Deps) -> int:
    """Length (in tasks) of the longest dependency chain. Assumes the graph is acyclic."""
    memo: dict[str, int] = {}

    def depth(task: str, trail: frozenset[str]) -> int:
        if task in memo:
            return memo[task]
        if task in trail:
            raise ValueError("dependency graph contains a cycle")
        best = 1
        for d in deps[task]:
            if d in deps:
                best = max(best, 1 + depth(d, trail | {task}))
        memo[task] = best
        return best

    return max((depth(t, frozenset()) for t in deps), default=0)


def transitive_dependencies(deps: Deps, task_id: str) -> set[str]:
    seen: set[str] = set()
    stack = [d for d in deps.get(task_id, []) if d in deps]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(d for d in deps[cur] if d in deps and d not in seen)
    return seen
