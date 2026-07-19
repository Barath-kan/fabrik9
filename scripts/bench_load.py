"""Load-profiling harness: scale the sim past its default config and measure.

Usage: python scripts/bench_load.py [--ticks N] [--agents N] [--cols N]
                                    [--rows N] [--seed N] [--profile]

Runs a plain timed pass (accurate wall-clock numbers) and, with --profile,
a cProfile pass over the same deterministic workload. Prints a timing table,
the sim's built-in instrumentation counters, and a state fingerprint
(SHA256 of the sorted-key JSON snapshot) so before/after optimization runs
can be proven behavior-identical.

Extra agents beyond the three the world generator spawns are placed
deterministically on empty cells spiraling out from HQ, so a given
(seed, agents, cols, rows, ticks) tuple always produces the same run.
"""

import argparse
import cProfile
import hashlib
import io
import json
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import config as C  # noqa: E402


def build_sim(seed, n_agents, cols, rows):
    C.COLS, C.ROWS = cols, rows
    from app.sim.core import Simulation, Agent
    sim = Simulation(seed)
    sim.chaos_on = True

    hqx, hqy = C.HQ
    next_id = len(sim.agents)
    r = 2
    while next_id < n_agents and r < max(cols, rows):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = hqx + dx, hqy + dy
                if not sim.in_bounds(x, y):
                    continue
                if sim.grid[y][x]["type"] != C.T.EMPTY:
                    continue
                if sim.agent_at(x, y) is not None:
                    continue
                sim.agents.append(Agent(next_id, x, y))
                next_id += 1
                if next_id >= n_agents:
                    break
            if next_id >= n_agents:
                break
        r += 1
    assert len(sim.agents) == n_agents, f"only placed {len(sim.agents)} agents"
    return sim


def fingerprint(sim):
    blob = json.dumps(sim.to_dict(), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def timed_run(seed, n_agents, cols, rows, ticks):
    sim = build_sim(seed, n_agents, cols, rows)
    t0 = time.perf_counter()
    per_block = []  # ms/tick per 500-tick block, to show load ramp
    for start in range(0, ticks, 500):
        n = min(500, ticks - start)
        b0 = time.perf_counter()
        sim.run(n)
        per_block.append((time.perf_counter() - b0) * 1000 / n)
    wall = time.perf_counter() - t0
    return sim, wall, per_block


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=3000)
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--cols", type=int, default=96)
    ap.add_argument("--rows", type=int, default=64)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()

    print(f"config: {args.agents} agents, {args.cols}x{args.rows} grid, "
          f"{args.ticks} ticks, seed {args.seed}, chaos on")

    sim, wall, per_block = timed_run(args.seed, args.agents, args.cols,
                                     args.rows, args.ticks)
    s = sim.stats
    print("\n--- timed run (no profiler) ---")
    print(f"wall time        : {wall:.2f} s")
    print(f"avg per tick     : {wall * 1000 / args.ticks:.2f} ms")
    print("ms/tick by block : " + " ".join(f"{b:.1f}" for b in per_block))
    print(f"ticks/sec        : {args.ticks / wall:,.0f}  "
          f"(realtime needs {C.TICKS_PER_SEC})")
    print(f"astar runs       : {s['astar_runs']:,}")
    print(f"astar nodes      : {s['nodes']:,}  "
          f"({s['nodes'] / max(1, s['astar_runs']):.0f}/run)")
    print(f"heap ops         : {s['heap_ops']:,}")
    print(f"avg path len     : {s['path_sum'] / max(1, s['path_n']):.1f}")
    print(f"tasks done       : {s['tasks_done']:,}   "
          f"faults fixed: {s['faults_fixed']}   gears: {sim.stock['gears']}")
    print(f"fingerprint      : {fingerprint(sim)}")

    if args.profile:
        print("\n--- cProfile (identical workload) ---")
        pr = cProfile.Profile()
        pr.enable()
        timed_run(args.seed, args.agents, args.cols, args.rows, args.ticks)
        pr.disable()
        buf = io.StringIO()
        pstats.Stats(pr, stream=buf).sort_stats("tottime").print_stats(18)
        print("\n".join(ln for ln in buf.getvalue().splitlines() if ln.strip()))


if __name__ == "__main__":
    main()
