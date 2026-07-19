"""Pinned-fingerprint regression test.

Phase 3 proved the pathfinding optimization behavior-identical by comparing a
SHA256 of the full simulation snapshot before and after (see PERF_REPORT.md).
This test pins that guarantee: any change to pathfinding.py or core.py that
alters simulation behavior — search order, tie-breaking, costs, RNG
consumption — produces a different snapshot and fails here, instead of
eroding determinism silently.

The workload runs 8 agents (the 3 world-spawned ones plus 5 extra placed on a
deterministic spiral around HQ) for 5000 ticks with chaos on. The extra
congestion matters: with the default 3 agents, a mutation to the A* teammate
penalty (+6 -> +5) never flips a path choice and goes undetected, whereas
this workload's fingerprint diverges. Runs in ~0.1s.
"""

import hashlib
import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.sim.core import Simulation, Agent
from app import config as C

PINNED_SEED = 1337
PINNED_TICKS = 5000
PINNED_EXTRA_AGENTS = 5
PINNED_FINGERPRINT = "3dda234ec8f28f0f"


def state_fingerprint(sim):
    """SHA256 over the sorted-key JSON snapshot — the same construction
    scripts/bench_load.py prints, so a bench run can be checked against
    this pin by eye."""
    blob = json.dumps(sim.to_dict(), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_pinned_workload():
    sim = Simulation(PINNED_SEED)
    sim.chaos_on = True
    hqx, hqy = C.HQ
    next_id, remaining, r = len(sim.agents), PINNED_EXTRA_AGENTS, 2
    while remaining > 0 and r < max(C.COLS, C.ROWS):
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
                remaining -= 1
                if remaining == 0:
                    break
            if remaining == 0:
                break
        r += 1
    assert remaining == 0
    return sim


def test_pinned_state_fingerprint():
    sim = build_pinned_workload()
    sim.run(PINNED_TICKS)
    fp = state_fingerprint(sim)
    assert fp == PINNED_FINGERPRINT, (
        f"Simulation behavior changed: fingerprint {fp} != pinned "
        f"{PINNED_FINGERPRINT} (seed {PINNED_SEED}, {PINNED_TICKS} ticks, "
        f"{PINNED_EXTRA_AGENTS} extra agents). If this change is intentional, "
        "update PINNED_FINGERPRINT and note the behavior change in the commit "
        "message; if not, a supposedly behavior-neutral edit (e.g. to "
        "pathfinding.py) altered the search."
    )
