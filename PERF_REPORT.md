# Phase 3 — Performance Report

Profiled the A* pathfinding and the utility-auction loop under load, found one
real bottleneck (an O(agents) scan per A* neighbor expansion), fixed it, and
proved the fix behavior-identical via a deterministic state fingerprint.
**Result: 2.9× end-to-end speedup at the 50-agent load point (15.1 → 5.2
ms/tick), with zero change in simulation output.**

- Harness: `scripts/bench_load.py` (deterministic; extra agents spawn on a
  fixed spiral around HQ, chaos mode on).
- Environment: Python 3.12.10, Windows 11, single thread.
- Workload: 50 agents, 96×64 grid (≈5× default area), 3000 ticks, seed 1337.
- Repro: `python scripts/bench_load.py --profile`

## Measured baseline (before)

```
config: 50 agents, 96x64 grid, 3000 ticks, seed 1337, chaos on
wall time        : 45.29 s
avg per tick     : 15.10 ms
ms/tick by block : 23.0 14.2 13.0 6.8 13.3 20.3
ticks/sec        : 66  (realtime needs 30)
astar runs       : 4,814
astar nodes      : 6,445,868  (1339/run)
heap ops         : 13,018,159
avg path len     : 14.8
fingerprint      : 3d8cebe0e92ebbef
```

cProfile, sorted by internal time (top of table):

```
   ncalls  tottime  percall  cumtime  filename:lineno(function)
 25236036   32.584    0.000   32.584  core.py:150(agent_at)          <-- bottleneck
     4814   31.931    0.007   81.944  pathfinding.py:12(astar)
 25189754    3.423    0.000    3.423  {method 'get' of 'dict' objects}
 25447816    3.357    0.000    3.357  core.py:160(base_cost)
 25783759    3.091    0.000    3.091  core.py:144(in_bounds)
  6572291    1.839    0.000    2.681  pathfinding.py:29(h)
  6445868    1.725    0.000    1.725  {built-in method _heapq.heappop}
  6206304    1.625    0.000    2.404  core.py:194(<lambda>)          <-- route goal_test
     2695    0.268    0.000   82.161  core.py:189(route)
     3978    0.033    0.000    0.172  core.py:235(candidate_tasks)   <-- auction: negligible
     3978    0.019    0.000   74.311  core.py:292(run_auction)
```

Findings:

1. **A\* is 99.5% of runtime** (`route` cumtime 82.2s of 82.6s). Everything
   else — world tick, belt sim, FSMs — is noise.
2. **The single bottleneck is `Simulation.agent_at`**: an O(N_agents) linear
   scan over the agent list, called once per neighbor expansion inside the A\*
   loop (`+6` congestion penalty for teammate-occupied cells). 25.2M calls ×
   O(50) scan = 32.6s of 82.6s profiled time. Measured complexity of one
   search was effectively **O(nodes × 4 × N_agents)** instead of the textbook
   O(nodes log nodes) — the agent scan, not the heap, dominated.
3. **The auction loop is not a bottleneck.** `run_auction` + `candidate_tasks`
   excluding the routing they trigger cost ~0.2s total (≈0.25%) across 3,978
   auctions. Its own complexity (O(faults + patch_tiles + assemblers) per
   auction) is trivial at this scale. No changes made there.

## Fix

`backend/app/sim/pathfinding.py`: agent positions cannot change during a
single search, so snapshot them into a set once per `astar()` call and test
membership in the neighbor loop, instead of re-scanning the agent list per
expansion. Search order, tie-breaking, and costs are untouched.

```python
occupied = (None if cost_fn is not None
            else {(a.x, a.y) for a in sim.agents if a is not self_agent})
...
if occupied is not None and (nx, ny) in occupied:
    c += 6
```

## Measured after

```
config: 50 agents, 96x64 grid, 3000 ticks, seed 1337, chaos on
wall time        : 15.56 s          (was 45.29 s  ->  2.9x)
avg per tick     : 5.19 ms          (was 15.10 ms)
ms/tick by block : 7.8 4.9 4.6 2.3 4.6 7.0
ticks/sec        : 193  (realtime needs 30 -> 6.4x headroom)
astar runs       : 4,814            (identical)
astar nodes      : 6,445,868        (identical)
heap ops         : 13,018,159       (identical)
fingerprint      : 3d8cebe0e92ebbef (identical)
```

cProfile after (top of table):

```
   ncalls  tottime  percall  cumtime  filename:lineno(function)
     4814   26.534    0.006   42.212  pathfinding.py:12(astar)
 25189754    3.117    0.000    3.117  {method 'get' of 'dict' objects}
 25783759    2.733    0.000    2.733  core.py:144(in_bounds)
 25447816    2.703    0.000    2.703  core.py:160(base_cost)
  6572291    1.755    0.000    2.548  pathfinding.py:34(h)
  6445868    1.572    0.000    1.572  {built-in method _heapq.heappop}
    56883    0.069    0.000    0.069  core.py:150(agent_at)   <-- was 32.6s
```

Correctness evidence:

- State fingerprint (SHA256 of the full sorted-key snapshot, including grid,
  agents, stock, stats, and event log) is **bit-for-bit identical** before and
  after: `3d8cebe0e92ebbef`. Node/heap counters match exactly, so the search
  visited the same nodes in the same order.
- Full test suite: **44 passed** (includes the determinism assertions).

## Scaling data points

| Config                        | ms/tick | ticks/s | A\* nodes/run |
|-------------------------------|--------:|--------:|--------------:|
| 3 agents, 42×28 (default)     |    0.01 | 138,700 |            27 |
| 50 agents, 96×64 (after fix)  |    5.19 |     193 |         1,339 |
| 80 agents, 128×84 (after fix) |   92.06 |      11 |         5,078 |

Known scaling cliff (documented, deliberately not "fixed"): past ~50 agents on
5 ore patches, contention makes many searches *fail* — e.g. every cell adjacent
to a delivery assembler is occupied — and a failed goal-test A\* exhausts the
entire reachable grid before returning `None` (nodes/run 1,339 → 5,078). Any
mitigation (node budget, reachability pre-check, shared flow fields) changes
simulation semantics and therefore the deterministic replay contract, so it is
out of scope for a performance pass. At the phase target (50+ agents, large
grid) the sim runs at 6.4× realtime.

Not optimized, and why: the remaining A\* cost is evenly spread Python
call overhead (`in_bounds`/`base_cost`/`h`, ~25M calls each, ~2–3s apiece) and
the per-node stats instrumentation that the API metrics rely on. Inlining
those would buy maybe another 1.5× at real readability cost, with no current
need — profiling shows no single remaining hotspot, so per the phase rules the
working code stays as is.
