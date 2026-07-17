"""Edge-case tests for the simulation core, beyond the seed-based suite.

Covers behaviours the 16-seed regression never exercises directly:
  1. Empty task queue          -> agent degrades to SUPERVISE / IDLE
  2. Agent-agent collision     -> same-tile occupancy is always resolved
  5. Duplicate simultaneous bids -> reservations/build-lock enforce exclusivity

(WebSocket disconnect mid-auction and SQLAlchemy rollback mid-write live in
test_server.py, since they need the async server + DB layers.)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.sim.core import Simulation, Agent
from app import config as C
from app.config import T, COLS, ROWS


def blank_sim():
    """A world with an empty grid and no agents — a clean slate to script."""
    sim = Simulation(1)
    for y in range(ROWS):
        for x in range(COLS):
            sim.grid[y][x] = {"type": T.EMPTY, "dir": 0, "amount": 0}
    sim.agents = []
    return sim


# --------------------------------------------------------------------------
# Edge case 1 — Empty task queue
# --------------------------------------------------------------------------

def test_empty_queue_degrades_to_supervise():
    """With no ore, no faults, no cargo and nothing to build, the only bid an
    agent can make is SUPERVISE — the auction must never return an empty menu."""
    sim = blank_sim()
    ag = Agent(0, C.HQ[0], C.HQ[1])
    sim.agents = [ag]

    cands = sim.candidate_tasks(ag)

    assert len(cands) == 1, f"expected only SUPERVISE, got {[t['type'] for t in cands]}"
    assert cands[0]["type"] == "SUPERVISE"


def test_empty_queue_forces_idle_when_nothing_assignable():
    """If every candidate fails to assign (a genuinely empty actionable queue),
    the agent parks in IDLE with a back-off timer rather than spinning."""
    sim = Simulation(1337)
    ag = sim.agents[0]
    ag.state = "PLAN"

    sim.candidate_tasks = lambda a: []  # nothing biddable this tick
    sim.run_auction(ag)

    assert ag.state == "IDLE"
    assert ag.timer == 30


def test_idle_agent_returns_to_plan_after_backoff():
    """An IDLE agent counts its timer down and re-enters the auction."""
    sim = Simulation(1337)
    ag = sim.agents[0]
    ag.state = "IDLE"
    ag.timer = 1
    sim.agent_tick(ag)          # timer -> 0
    assert ag.state == "PLAN"


# --------------------------------------------------------------------------
# Edge case 2 — Agent-agent collision on the same tile
# --------------------------------------------------------------------------

def test_mover_waits_instead_of_stacking_on_blocker():
    """When the next path tile is occupied, the mover must wait in place — it
    may never move onto a tile another agent already holds."""
    sim = blank_sim()
    mover = Agent(0, 5, 5)
    blocker = Agent(1, 6, 5)          # sitting on the mover's next tile
    sim.agents = [mover, blocker]

    mover.state = "MOVE"
    mover.dest = (9, 5, False)
    mover.path = [(6, 5), (7, 5), (8, 5), (9, 5)]
    blocker.state = "IDLE"
    blocker.timer = 999

    sim.agent_tick(mover)

    assert mover.wait_count == 1
    assert (mover.x, mover.y) == (5, 5), "mover must not stack onto the blocker"


def test_idle_blocker_is_nudged_aside():
    """A stationary (IDLE) blocker gets side-stepped once the mover has waited,
    freeing the contested tile."""
    sim = blank_sim()
    mover = Agent(0, 5, 5)
    blocker = Agent(1, 6, 5)
    sim.agents = [mover, blocker]

    mover.state = "MOVE"
    mover.dest = (9, 5, False)
    mover.path = [(6, 5), (7, 5), (8, 5), (9, 5)]
    blocker.state = "IDLE"
    blocker.timer = 999

    sim.agent_tick(mover)             # wait_count 1
    sim.agent_tick(mover)             # wait_count 2 -> nudge the idle blocker

    assert (blocker.x, blocker.y) != (6, 5), "idle blocker should have moved aside"


def test_no_two_agents_ever_share_a_tile_over_a_full_run():
    """The strongest collision guarantee: across a long deterministic run with
    real congestion, no two agents ever occupy the same tile on any tick."""
    sim = Simulation(1337)
    for _ in range(3000):
        sim.step()
        positions = [(a.x, a.y) for a in sim.agents]
        assert len(set(positions)) == len(positions), \
            f"tile collision at tick {sim.tick}: {positions}"


def test_congestion_abort_refunds_an_inflight_build():
    """The end state of unresolved congestion is an aborted task. When the
    aborted task is a BUILD, its reserved gears are refunded and the patch is
    un-committed — the economy is never leaked by a collision."""
    sim = Simulation(1337)
    sim.run(1500)
    ag = sim.agents[0]

    patch = sim.patches[0]
    plan = {"cost": 8, "patch": patch}
    patch["automated"] = True
    sim.stock["gears"] = 10
    ag.task = {"type": "BUILD", "plan": plan}
    ag.plan = plan
    sim.build_lock = ag.id

    sim.abort_task(ag, reason="congestion")

    assert sim.stock["gears"] == 18, "in-flight build gears must be refunded"
    assert patch["automated"] is False, "patch must be un-committed on abort"
    assert sim.build_lock == -1, "build lock must be released"
    assert ag.task is None and ag.state == "PLAN"


# --------------------------------------------------------------------------
# Edge case 5 — Duplicate task bids arriving simultaneously
# --------------------------------------------------------------------------

def test_reserved_mine_tile_excluded_from_second_bidder():
    """Once one agent reserves a mine tile, the contract-net must not offer the
    same tile to a second agent — no double-claiming."""
    sim = blank_sim()
    sim.grid[5][5] = {"type": T.ORE, "dir": 0, "amount": 100}
    sim.patches = [{"tiles": [(5, 5)], "automated": False, "no_auto": False}]
    first = Agent(0, 4, 5)
    second = Agent(1, 4, 6)
    sim.agents = [first, second]

    mine_bid = next(t for t in sim.candidate_tasks(first) if t["type"] == "MINE")
    assert sim.try_assign(first, mine_bid) is True
    assert sim.reserved.get("mine:5,5") == first.id

    second_bids = sim.candidate_tasks(second)
    assert all(
        not (t["type"] == "MINE" and t.get("tile") == (5, 5))
        for t in second_bids
    ), "reserved tile leaked into the second agent's candidate list"


def test_reserved_repair_excluded_from_second_bidder():
    """A fault already reserved by one agent is invisible to the next."""
    sim = blank_sim()
    sim.faults = [{"x": 5, "y": 5, "dir": 0, "created": 0}]
    a = Agent(0, 4, 5)
    b = Agent(1, 4, 6)
    sim.agents = [a, b]
    sim.reserved["repair:5,5"] = a.id

    assert all(t["type"] != "REPAIR" for t in sim.candidate_tasks(b))


def test_build_lock_prevents_a_second_concurrent_build():
    """The single build lock means only one agent can hold a build contract at
    a time; a second agent gets no BUILD candidate while it's held."""
    sim = Simulation(1337)
    sim.run(2000)
    holder, other = sim.agents[0], sim.agents[1]
    sim.build_lock = holder.id

    assert all(t["type"] != "BUILD" for t in sim.candidate_tasks(other))


def test_reservation_released_frees_the_task_again():
    """Releasing an agent's reservations makes its claimed tile biddable again —
    the inverse of exclusivity, proving reservations aren't leaked forever."""
    sim = blank_sim()
    sim.grid[5][5] = {"type": T.ORE, "dir": 0, "amount": 100}
    sim.patches = [{"tiles": [(5, 5)], "automated": False, "no_auto": False}]
    a = Agent(0, 4, 5)
    b = Agent(1, 4, 6)
    sim.agents = [a, b]

    bid = next(t for t in sim.candidate_tasks(a) if t["type"] == "MINE")
    sim.try_assign(a, bid)
    assert "mine:5,5" in sim.reserved

    sim.release_reservations(a)
    assert "mine:5,5" not in sim.reserved
    assert any(
        t["type"] == "MINE" and t.get("tile") == (5, 5)
        for t in sim.candidate_tasks(b)
    ), "released tile should be biddable again"


# --------------------------------------------------------------------------
# Supporting: resource-depletion boundary (exercises the mining FSM edge)
# --------------------------------------------------------------------------

def test_mining_stops_and_replans_when_the_tile_runs_dry():
    """Mining the final ore unit clears the tile, and the agent re-plans rather
    than mining an exhausted tile."""
    sim = blank_sim()
    sim.grid[5][5] = {"type": T.ORE, "dir": 0, "amount": 1}
    ag = Agent(0, 5, 5)
    sim.agents = [ag]
    ag.state = "MINE"
    ag.task = {"type": "MINE", "tile": (5, 5)}
    ag.timer = C.MINE_TICKS - 1

    sim.agent_tick(ag)                       # extracts the last unit
    assert ag.cargo == 1
    assert sim.grid[5][5]["type"] == T.EMPTY, "depleted ore tile becomes empty"

    sim.agent_tick(ag)                       # tile dry -> back to planning
    assert ag.state == "PLAN"


def test_snapshot_rolls_back_an_inflight_build():
    """A snapshot taken while an agent is mid-BUILD must refund that agent's
    reserved gears and un-commit the patch — the 'uncommitted transaction'
    rule. (The existing economy test passes trivially when nobody is building;
    this forces the in-flight path.)"""
    sim = Simulation(1337)
    sim.run(2000)
    ag = sim.agents[0]
    patch = sim.patches[0]
    plan = {"cost": 8, "patch": patch}

    patch["automated"] = True                # committed while building
    ag.task = {"type": "BUILD", "plan": plan}
    ag.plan = plan
    committed_gears = sim.stock["gears"]

    snap = sim.to_dict()

    assert snap["stock"]["gears"] == committed_gears + 8, "in-flight cost refunded"
    idx = sim.patches.index(patch)
    assert snap["patches"][idx]["automated"] is False, "patch un-committed in snapshot"
