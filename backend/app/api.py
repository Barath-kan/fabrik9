"""REST endpoints: runs, snapshots (save/load), telemetry."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .db import SessionLocal
from . import persistence

router = APIRouter(prefix="/api")


def get_manager():
    from .main import manager
    return manager


class NewRun(BaseModel):
    seed: int | None = None


class SaveBody(BaseModel):
    label: str = ""


@router.get("/health")
async def health():
    mgr = get_manager()
    return {"ok": True, "tick": mgr.sim.tick, "run_id": mgr.run_id}


@router.get("/state")
async def state():
    return get_manager().state_message()


@router.post("/runs")
async def new_run(body: NewRun):
    import random
    mgr = get_manager()
    seed = body.seed if body.seed is not None else random.randint(1, 999999)
    await mgr.reset(seed)
    return {"run_id": mgr.run_id, "seed": seed}


@router.post("/runs/current/snapshots")
async def save_snapshot(body: SaveBody):
    mgr = get_manager()
    async with SessionLocal() as session:
        return await persistence.save_snapshot(session, mgr.run_id,
                                               mgr.sim, body.label)


@router.get("/snapshots")
async def snapshots():
    async with SessionLocal() as session:
        return await persistence.list_snapshots(session)


@router.post("/snapshots/{snapshot_id}/load")
async def load_snapshot(snapshot_id: str):
    mgr = get_manager()
    async with SessionLocal() as session:
        snap = await persistence.get_snapshot(session, snapshot_id)
        if snap is None:
            raise HTTPException(404, "snapshot not found")
    await mgr.load_snapshot_state(snap.state, snap.run_id)
    return {"loaded": snapshot_id, "tick": mgr.sim.tick,
            "paused": True, "note": "resume via WS control or the UI"}


@router.get("/runs/{run_id}/telemetry")
async def telemetry(run_id: str):
    async with SessionLocal() as session:
        return await persistence.list_telemetry(session, run_id)
