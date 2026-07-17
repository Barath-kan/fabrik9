"""Save/load and telemetry persistence."""

from sqlalchemy import select
from . import models


async def create_run(session, seed: int, label: str = "") -> str:
    run = models.SimulationRun(seed=seed, label=label)
    session.add(run)
    await session.commit()
    return run.id


async def save_snapshot(session, run_id: str, sim, label: str = "") -> dict:
    snap = models.Snapshot(run_id=run_id, tick=sim.tick,
                           label=label, state=sim.to_dict())
    session.add(snap)
    await session.commit()
    return {"id": snap.id, "run_id": run_id, "tick": snap.tick, "label": label}


async def list_snapshots(session, limit: int = 50):
    rows = (await session.execute(
        select(models.Snapshot.id, models.Snapshot.run_id,
               models.Snapshot.tick, models.Snapshot.label,
               models.Snapshot.created_at,
               models.SimulationRun.seed)
        .join(models.SimulationRun,
              models.Snapshot.run_id == models.SimulationRun.id)
        .order_by(models.Snapshot.created_at.desc())
        .limit(limit))).all()
    return [{"id": r.id, "run_id": r.run_id, "tick": r.tick,
             "label": r.label, "seed": r.seed,
             "created_at": r.created_at.isoformat()} for r in rows]


async def get_snapshot(session, snapshot_id: str):
    return await session.get(models.Snapshot, snapshot_id)


async def record_sample(session, run_id: str, sim):
    mttr = sim.mttr_seconds()
    session.add(models.TelemetrySample(
        run_id=run_id, tick=sim.tick,
        gears_stock=sim.stock["gears"],
        gears_crafted=sim.total_crafted(),
        ore_delivered=sim.stock["ore_delivered"],
        faults_active=len(sim.faults),
        faults_fixed=sim.stats["faults_fixed"],
        mttr_s=mttr,
        agents=[{"id": a.id, "x": a.x, "y": a.y, "state": a.state,
                 "cargo": a.cargo, "tasks_done": a.tasks_done}
                for a in sim.agents]))
    await session.commit()


async def record_events(session, run_id: str, logs, seq_base: int):
    for i, l in enumerate(logs):
        session.add(models.EventLog(run_id=run_id, tick=l["t"],
                                    seq=seq_base + i,
                                    level=l.get("cls", ""),
                                    message=l["msg"]))
    if logs:
        await session.commit()


async def list_telemetry(session, run_id: str, limit: int = 500):
    rows = (await session.execute(
        select(models.TelemetrySample)
        .where(models.TelemetrySample.run_id == run_id)
        .order_by(models.TelemetrySample.tick)
        .limit(limit))).scalars().all()
    return [{"tick": r.tick, "gears_stock": r.gears_stock,
             "gears_crafted": r.gears_crafted,
             "ore_delivered": r.ore_delivered,
             "faults_active": r.faults_active,
             "faults_fixed": r.faults_fixed,
             "mttr_s": r.mttr_s, "agents": r.agents} for r in rows]
