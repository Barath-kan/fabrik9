"""Server-layer tests: WebSocket hub, persistence transactions, REST API.

These exercise the async stack that the pure-sim suite never touches — the
SimulationManager broadcast/persist loop, the FastAPI endpoints, and the
SQLAlchemy session behaviour under failure.

Two of the five required edge cases live here because they are inherently
async/DB-bound:
  3. WebSocket client disconnect mid-auction  -> dead sockets are pruned
  4. SQLAlchemy transaction failure/rollback  -> a failed batch leaves no rows
"""

import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient

from app import persistence, models
from app.db import SessionLocal
from app.runtime import SimulationManager, TELEMETRY_EVERY_TICKS, _task_label
from app.sim.core import Agent


class FakeWS:
    """Stand-in for a Starlette WebSocket. Optionally fails on send to simulate
    a client that dropped the connection between ticks."""

    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_json(self, message):
        if self.fail:
            raise RuntimeError("client disconnected")
        self.sent.append(message)


# --------------------------------------------------------------------------
# Edge case 3 — WebSocket client disconnect mid-auction
# --------------------------------------------------------------------------

async def test_broadcast_prunes_socket_that_dropped_mid_auction():
    """If a client disconnects while the authoritative loop is broadcasting the
    post-auction state, the broadcast must drop that socket and keep serving
    the survivors — never crash the tick loop."""
    mgr = SimulationManager(seed=1337)
    mgr.sim.run(200)                      # advance so there is real state to send
    live = FakeWS()
    dropped = FakeWS(fail=True)
    mgr.clients = {live, dropped}
    mgr._client_versions = {}

    await mgr._broadcast()

    assert dropped not in mgr.clients, "dead socket should be pruned"
    assert live in mgr.clients, "healthy socket must survive"
    assert dropped not in mgr._client_versions
    assert live.sent, "healthy client should have received world+state"


def test_ws_endpoint_connect_receives_snapshot_and_survives_disconnect():
    """End-to-end: a client connects, gets an immediate world+state, drives a
    control action, then disconnects cleanly — the endpoint's finally-block
    removes it from the client set without error."""
    from app.main import app, manager

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            world = ws.receive_json()
            state = ws.receive_json()
            assert world["type"] == "world"
            assert state["type"] == "state"
            ws.send_json({"action": "pause"})
        # Leaving the `with` closes the socket -> server raises
        # WebSocketDisconnect -> finally discards the client. No assertion on
        # timing here (the loop runs in a portal thread); the control-branch
        # coverage is asserted deterministically in the next test.


def test_ws_control_actions_mutate_manager_state():
    """Each control message routes to the right manager mutation."""
    import time
    from app.main import app, manager

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()   # world
            ws.receive_json()   # state
            ws.send_json({"action": "pause"})
            ws.send_json({"action": "speed", "value": 5})
            ws.send_json({"action": "chaos", "value": True})
            ws.send_json({"action": "inject_fault"})

            # The loop processes messages in order on the portal thread; poll
            # briefly for the settled state rather than assuming instant apply.
            deadline = time.time() + 3.0
            while time.time() < deadline and mgr_speed(manager) != 5:
                time.sleep(0.02)

            assert manager.speed == 5
            assert manager.paused is True
            assert manager.sim.chaos_on is True


def mgr_speed(manager):
    return manager.speed


# --------------------------------------------------------------------------
# Edge case 4 — SQLAlchemy transaction failure / rollback mid-write
# --------------------------------------------------------------------------

async def test_failed_event_batch_rolls_back_atomically():
    """A multi-row event write that fails partway (NULL message violates
    NOT NULL) must leave *no* rows behind — the batch is one transaction — and
    the session must be usable again after rollback."""
    async with SessionLocal() as session:
        run_id = await persistence.create_run(session, seed=7)

        bad_batch = [
            {"t": 1, "msg": "first ok", "cls": ""},
            {"t": 2, "msg": "second ok", "cls": ""},
            {"t": 3, "msg": None, "cls": ""},        # violates NOT NULL
        ]
        with pytest.raises(Exception):
            await persistence.record_events(session, run_id, bad_batch, seq_base=0)
        await session.rollback()

        remaining = (await session.execute(
            select(models.EventLog).where(models.EventLog.run_id == run_id)
        )).scalars().all()
        assert remaining == [], "failed batch must not leave partial rows"

        # The session recovers: a clean write after rollback commits normally.
        await persistence.record_events(
            session, run_id, [{"t": 9, "msg": "after recovery", "cls": ""}], seq_base=10)
        ok = (await session.execute(
            select(models.EventLog).where(models.EventLog.run_id == run_id)
        )).scalars().all()
        assert len(ok) == 1
        assert ok[0].message == "after recovery"


async def test_rolled_back_run_leaves_no_trace():
    """Rolling back before commit discards an uncommitted run row entirely."""
    async with SessionLocal() as session:
        run = models.SimulationRun(seed=123, label="doomed")
        session.add(run)
        await session.flush()          # row exists in the transaction
        pending_id = run.id
        await session.rollback()

    async with SessionLocal() as session2:
        found = await session2.get(models.SimulationRun, pending_id)
        assert found is None, "rolled-back run must not persist"


# --------------------------------------------------------------------------
# Persistence happy-path (raises coverage of persistence + models + db)
# --------------------------------------------------------------------------

async def test_snapshot_save_load_and_telemetry_roundtrip():
    mgr = SimulationManager(seed=1337)
    async with SessionLocal() as session:
        mgr.run_id = await persistence.create_run(session, mgr.sim.seed)

    mgr.sim.run(500)

    async with SessionLocal() as session:
        meta = await persistence.save_snapshot(session, mgr.run_id, mgr.sim, "cp1")
    assert meta["tick"] == mgr.sim.tick

    async with SessionLocal() as session:
        listed = await persistence.list_snapshots(session)
        assert any(s["id"] == meta["id"] for s in listed)
        snap = await persistence.get_snapshot(session, meta["id"])
        assert snap is not None
        assert snap.state["seed"] == 1337

    async with SessionLocal() as session:
        await persistence.record_sample(session, mgr.run_id, mgr.sim)
        rows = await persistence.list_telemetry(session, mgr.run_id)
    assert len(rows) == 1
    assert rows[0]["tick"] == mgr.sim.tick


async def test_maybe_persist_writes_a_sample_after_the_interval():
    mgr = SimulationManager(seed=1337)
    async with SessionLocal() as session:
        mgr.run_id = await persistence.create_run(session, mgr.sim.seed)

    mgr.sim.run(TELEMETRY_EVERY_TICKS + 10)   # cross the telemetry threshold
    await mgr._maybe_persist()

    async with SessionLocal() as session:
        rows = await persistence.list_telemetry(session, mgr.run_id)
    assert len(rows) >= 1


# --------------------------------------------------------------------------
# SimulationManager unit behaviour (runtime.py)
# --------------------------------------------------------------------------

async def test_reset_starts_a_fresh_run():
    mgr = SimulationManager(seed=1337)
    await mgr.reset(4242)
    assert mgr.sim.seed == 4242
    assert mgr.paused is False
    assert mgr.run_id is not None


async def test_load_snapshot_state_restores_paused():
    mgr = SimulationManager(seed=1337)
    mgr.sim.run(300)
    state = mgr.sim.to_dict()

    await mgr.load_snapshot_state(state, run_id="run-xyz")

    assert mgr.paused is True, "a loaded sim must resume explicitly, not auto-run"
    assert mgr.run_id == "run-xyz"
    assert mgr.sim.tick == 300


def test_world_and_state_messages_have_expected_shape():
    from app.config import ROWS, COLS
    mgr = SimulationManager(seed=1337)
    mgr.sim.run(100)
    world = mgr.world_message()
    state = mgr.state_message()
    assert world["type"] == "world"
    assert len(world["grid"]) == ROWS
    assert len(world["grid"][0]) == COLS
    assert state["type"] == "state"
    assert state["tick"] == mgr.sim.tick
    assert "agents" in state and len(state["agents"]) == len(mgr.sim.agents)


# --------------------------------------------------------------------------
# REST API integration (api.py end to end through the real app)
# --------------------------------------------------------------------------

def test_rest_api_full_flow():
    from app.main import app

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200 and health.json()["ok"] is True

        run = client.post("/api/runs", json={"seed": 4242})
        assert run.status_code == 200
        body = run.json()
        assert body["seed"] == 4242
        run_id = body["run_id"]

        assert client.get("/api/state").json()["type"] == "state"

        saved = client.post("/api/runs/current/snapshots", json={"label": "cp"})
        assert saved.status_code == 200
        snap_id = saved.json()["id"]

        listing = client.get("/api/snapshots").json()
        assert any(s["id"] == snap_id for s in listing)

        loaded = client.post(f"/api/snapshots/{snap_id}/load")
        assert loaded.status_code == 200 and loaded.json()["paused"] is True

        assert client.post("/api/snapshots/does-not-exist/load").status_code == 404

        telem = client.get(f"/api/runs/{run_id}/telemetry")
        assert telem.status_code == 200 and isinstance(telem.json(), list)


async def test_load_missing_snapshot_raises_404():
    """Direct (non-threaded) exercise of the 'snapshot not found' guard."""
    from app import api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await api.load_snapshot("no-such-snapshot-id")
    assert exc.value.status_code == 404


def test_rest_new_run_defaults_to_random_seed():
    from app.main import app

    with TestClient(app) as client:
        body = client.post("/api/runs", json={}).json()
        assert 1 <= body["seed"] <= 999999


# --------------------------------------------------------------------------
# runtime internals: the tick loop, broadcast/persist guards, task labels
# --------------------------------------------------------------------------

async def test_loop_advances_sim_and_broadcasts_then_cancels_cleanly():
    """Drive the real 30 Hz loop for a fraction of a second: it must advance
    the sim and push state to connected clients, and cancel without error."""
    mgr = SimulationManager(seed=1337)
    async with SessionLocal() as session:
        mgr.run_id = await persistence.create_run(session, mgr.sim.seed)
    ws = FakeWS()
    mgr.clients = {ws}

    task = asyncio.create_task(mgr._loop())
    await asyncio.sleep(0.3)                 # ~9 ticks at 30 Hz
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mgr.sim.tick > 0, "loop should have stepped the simulation"
    assert ws.sent, "loop should have broadcast state to the client"


async def test_broadcast_is_a_noop_with_no_clients():
    mgr = SimulationManager(seed=1337)
    mgr.clients = set()
    await mgr._broadcast()                    # must return without error


async def test_maybe_persist_skips_before_the_interval():
    """Fresh sim (tick 0) is below the telemetry interval, so nothing is
    written yet."""
    mgr = SimulationManager(seed=1337)
    async with SessionLocal() as session:
        mgr.run_id = await persistence.create_run(session, mgr.sim.seed)
    await mgr._maybe_persist()

    async with SessionLocal() as session:
        rows = await persistence.list_telemetry(session, mgr.run_id)
    assert rows == []


def test_task_label_covers_every_task_kind():
    a = Agent(0, 0, 0)

    a.task = None
    assert _task_label(a) == "selecting goal…"

    a.task = {"type": "MINE", "tile": (3, 4)}
    assert _task_label(a) == "mine (3,4)"

    a.cargo = 5
    a.task = {"type": "DELIVER"}
    assert _task_label(a) == "haul 5 ore"

    a.build_queue = [1, 2, 3]
    a.build_idx = 0
    a.task = {"type": "BUILD"}
    assert _task_label(a) == "build 1/3"

    a.task = {"type": "REPAIR", "fault": {"x": 1, "y": 2}}
    assert _task_label(a) == "repair (1,2)"

    a.task = {"type": "SUPERVISE"}
    assert _task_label(a) == "supervising"

    a.task = {"type": "SOMETHING_ELSE"}
    assert _task_label(a) == "…"
