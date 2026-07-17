"""Test suite for the Python simulation core.

Mirrors the JavaScript suite (same seeds, same criteria) plus a
snapshot round-trip test for the new persistence layer.
Run: python -m pytest tests/ -q
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.sim.core import Simulation
from app.sim import pathfinding
from app.config import T, COLS, ROWS


def blank_sim():
    sim = Simulation(1)
    for y in range(ROWS):
        for x in range(COLS):
            sim.grid[y][x] = {"type": T.EMPTY, "dir": 0, "amount": 0}
    sim.agents = []
    return sim


def test_astar_shortest_path_on_open_ground():
    sim = blank_sim()
    p = pathfinding.astar(sim, 2, 2, lambda x, y: (x, y) == (10, 2), (10, 2))
    assert p is not None
    assert len(p) == 8
    assert p[-1] == (10, 2)


def test_astar_walls_and_gaps():
    sim = blank_sim()
    for y in range(ROWS):
        sim.grid[y][6]["type"] = T.ROCK
    assert pathfinding.astar(sim, 2, 2, lambda x, y: (x, y) == (10, 2), (10, 2)) is None
    sim.grid[14][6]["type"] = T.EMPTY
    p = pathfinding.astar(sim, 2, 2, lambda x, y: (x, y) == (10, 2), (10, 2))
    assert p is not None
    assert (6, 14) in p


def test_full_cargo_forces_deliver():
    sim = Simulation(1337)
    ag = sim.agents[0]
    ag.cargo = 10
    cands = sorted(sim.candidate_tasks(ag), key=lambda t: -t["score"])
    assert cands[0]["type"] == "DELIVER"
    assert cands[0]["score"] == 999


def test_bootstrap_across_seeds():
    for seed in (1337, 7, 90210):
        sim = Simulation(seed)
        sim.run(12000)
        assert sim.automated_count() == 5, f"seed {seed}"
        assert len(sim.miners) >= 3, f"seed {seed}"
        assert sim.total_crafted() > 200, f"seed {seed}"


def test_fault_recovery():
    sim = Simulation(1337)
    sim.run(12000)
    injected = 0
    for _ in range(5):
        if sim.inject_fault():
            injected += 1
        sim.run(1200)
    sim.run(3000)
    assert injected == 5
    assert len(sim.faults) == 0
    assert sim.stats["faults_fixed"] == 5
    assert sim.mttr_seconds() < 10


def test_determinism():
    a = Simulation(1337); a.run(15000)
    b = Simulation(1337); b.run(15000)
    assert a.total_crafted() == b.total_crafted()
    assert a.stats["nodes"] == b.stats["nodes"]
    assert a.stock["ore_delivered"] == b.stock["ore_delivered"]
    assert [(g.x, g.y, g.state) for g in a.agents] == \
           [(g.x, g.y, g.state) for g in b.agents]


def test_different_seeds_differ():
    a, b = Simulation(1), Simulation(2)
    flat = lambda s: [t["type"] for row in s.grid for t in row]
    assert flat(a) != flat(b)


def test_snapshot_roundtrip():
    """Save mid-run, restore, and verify the restored sim keeps working:
    same automation level preserved, faults still repairable, economy sane."""
    sim = Simulation(1337)
    sim.run(9000)
    automated_before = sim.automated_count()
    crafted_before = sim.total_crafted()

    snap = sim.to_dict()
    import json
    snap = json.loads(json.dumps(snap))   # force through JSON like the DB will

    restored = Simulation.from_dict(snap)
    assert restored.tick == sim.tick
    assert restored.automated_count() >= automated_before - 1  # in-flight build rolled back
    assert restored.total_crafted() == crafted_before

    restored.run(6000)
    assert restored.automated_count() == 5
    restored.inject_fault()
    restored.run(2000)
    assert len(restored.faults) == 0, "restored sim must still repair faults"


def test_snapshot_conserves_economy():
    """The build-rollback rule must refund gears, never create or destroy them."""
    sim = Simulation(7)
    sim.run(4000)
    total_before = sim.stock["gears"] + sum(
        ag.plan["cost"] for ag in sim.agents
        if ag.task and ag.task["type"] == "BUILD" and ag.plan)
    snap = sim.to_dict()
    assert snap["stock"]["gears"] == total_before
