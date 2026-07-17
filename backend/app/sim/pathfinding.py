"""Instrumented A* over Python's heapq — a faithful port of pathfinding.js.

Ties in the open list are broken by insertion order (a monotonic counter),
which keeps the search fully deterministic for a given world state.
"""

import heapq

from .. import config as C


def astar(sim, sx, sy, goal_test, h_target, self_agent=None, cost_fn=None):
    """A* from (sx, sy) until goal_test(x, y) passes.

    - Manhattan heuristic toward h_target (admissible on a 4-grid).
    - Other agents' cells cost +6 so paths flow around teammates.
    - cost_fn overrides terrain costs entirely (the belt router passes an
      empty-ground-only function, guaranteeing contiguous conveyor plans).

    Returns a list of (x, y) steps (start exclusive) or None.
    """
    sim.stats["astar_runs"] += 1
    hx, hy = h_target
    open_heap = []
    counter = 0
    came = {}
    g_score = {(sx, sy): 0}

    def h(x, y):
        return abs(x - hx) + abs(y - hy)

    heapq.heappush(open_heap, (h(sx, sy), counter, sx, sy, 0))
    sim.stats["heap_ops"] += 1

    while open_heap:
        _f, _c, cx, cy, cg = heapq.heappop(open_heap)
        sim.stats["heap_ops"] += 1
        sim.stats["nodes"] += 1

        if goal_test(cx, cy):
            path = []
            node = (cx, cy)
            while node in came:
                path.append(node)
                node = came[node]
            path.reverse()
            sim.stats["path_sum"] += len(path)
            sim.stats["path_n"] += 1
            return path

        for dx, dy in C.DIRS:
            nx, ny = cx + dx, cy + dy
            if not sim.in_bounds(nx, ny):
                continue
            c = cost_fn(nx, ny) if cost_fn else sim.base_cost(nx, ny)
            if c == float("inf"):
                continue
            if cost_fn is None and sim.agent_at(nx, ny, self_agent) is not None:
                c += 6
            ng = cg + c
            if ng < g_score.get((nx, ny), float("inf")):
                g_score[(nx, ny)] = ng
                came[(nx, ny)] = (cx, cy)
                counter += 1
                heapq.heappush(open_heap, (ng + h(nx, ny), counter, nx, ny, ng))
                sim.stats["heap_ops"] += 1
    return None
