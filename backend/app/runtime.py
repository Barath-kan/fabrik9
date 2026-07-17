"""SimulationManager — the server-authoritative heart of the backend.

Owns the single live Simulation, advances it on a fixed 30 Hz loop with an
accumulator (so wall-clock jitter never desyncs the tick count), broadcasts
state to every connected WebSocket at frame rate, and periodically persists
telemetry samples and event-log rows.

Design notes worth defending in an interview:
- The sim advances `speed` steps per frame, but broadcast stays at 30 fps —
  clients render frames, the server owns time.
- Static world data (the grid) is only re-sent when `structure_version`
  changes; the per-frame message carries only dynamic entities. This keeps
  steady-state bandwidth to a few KB/frame.
- Broadcast failures never crash the loop; dead sockets are pruned.
"""

import asyncio
import time

from .sim.core import Simulation
from . import config as C
from .db import SessionLocal
from . import persistence

TELEMETRY_EVERY_TICKS = 300      # one sample every 10 simulated seconds


class SimulationManager:

    def __init__(self, seed=1337):
        self.sim = Simulation(seed)
        self.run_id: str | None = None
        self.paused = False
        self.speed = 1
        self.clients: set = set()          # WebSocket connections
        self._client_versions: dict = {}   # ws -> last structure_version sent
        self._persisted_log_seq = 0
        self._last_telemetry_tick = 0
        self._task: asyncio.Task | None = None

    # ---------------- lifecycle ----------------

    async def start(self):
        async with SessionLocal() as session:
            self.run_id = await persistence.create_run(session, self.sim.seed)
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def reset(self, seed: int):
        self.sim = Simulation(seed)
        self.paused = False
        self._persisted_log_seq = 0
        self._last_telemetry_tick = 0
        self._client_versions.clear()
        async with SessionLocal() as session:
            self.run_id = await persistence.create_run(session, seed)

    async def load_snapshot_state(self, state: dict, run_id: str):
        self.sim = Simulation.from_dict(state)
        self.run_id = run_id
        self.paused = True                 # resume explicitly after a load
        self._persisted_log_seq = self.sim.log_seq
        self._last_telemetry_tick = self.sim.tick
        self._client_versions.clear()

    # ---------------- the loop ----------------

    async def _loop(self):
        tick_dt = 1.0 / C.TICKS_PER_SEC
        acc = 0.0
        last = time.monotonic()
        last_broadcast = 0.0
        while True:
            now = time.monotonic()
            acc += min(now - last, 0.1)    # clamp: never spiral after a stall
            last = now
            stepped = False
            while acc >= tick_dt:
                acc -= tick_dt
                if not self.paused:
                    for _ in range(self.speed):
                        self.sim.step()
                    stepped = True
            # Broadcast exactly once per tick batch (30 fps while running);
            # while paused, a 4 Hz heartbeat keeps clients' HUDs honest.
            if self.clients and (stepped or now - last_broadcast > 0.25):
                await self._broadcast()
                last_broadcast = now
            if stepped:
                await self._maybe_persist()
            await asyncio.sleep(tick_dt / 2)

    # ---------------- messages ----------------

    def world_message(self):
        s = self.sim
        return {
            "type": "world",
            "version": s.structure_version,
            "seed": s.seed,
            "cols": C.COLS, "rows": C.ROWS,
            "grid": [[[t["type"], t["dir"], t["amount"]] for t in row]
                     for row in s.grid],
        }

    def state_message(self):
        s = self.sim
        mttr = s.mttr_seconds()
        return {
            "type": "state",
            "tick": s.tick,
            "version": s.structure_version,
            "paused": self.paused,
            "speed": self.speed,
            "chaos": s.chaos_on,
            "agents": [{
                "id": a.id, "x": a.x, "y": a.y,
                "state": a.state, "cargo": a.cargo,
                "facing": list(a.facing),
                "path": [list(p) for p in a.path],
                "decision": a.last_decision,
                "task": _task_label(a),
            } for a in s.agents],
            "belt_items": [[b["x"], b["y"], round(b["prog"], 3)]
                           for b in s.belt_items],
            "assemblers": s.assemblers,
            "faults": s.faults,
            "stock": s.stock,
            "stats": {
                "tasks_done": s.stats["tasks_done"],
                "faults_fixed": s.stats["faults_fixed"],
                "astar_runs": s.stats["astar_runs"],
                "nodes": s.stats["nodes"],
                "heap_ops": s.stats["heap_ops"],
                "rate": len(s.gear_history),
                "mttr_s": round(mttr, 1) if mttr is not None else None,
                "uptime": round(s.uptime_pct(), 1),
                "miners": len(s.miners),
                "n_assemblers": len(s.assemblers),
            },
            "log": s.log[-12:],
            "log_seq": s.log_seq,
        }

    async def _broadcast(self):
        if not self.clients:
            return
        state = self.state_message()
        world = None
        dead = []
        for ws in list(self.clients):
            try:
                if self._client_versions.get(ws) != self.sim.structure_version:
                    if world is None:
                        world = self.world_message()
                    await ws.send_json(world)
                    self._client_versions[ws] = self.sim.structure_version
                await ws.send_json(state)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
            self._client_versions.pop(ws, None)

    # ---------------- persistence hooks ----------------

    async def _maybe_persist(self):
        s = self.sim
        if s.tick - self._last_telemetry_tick < TELEMETRY_EVERY_TICKS:
            return
        self._last_telemetry_tick = s.tick
        new_logs = [l for i, l in enumerate(s.log)
                    if s.log_seq - len(s.log) + i >= self._persisted_log_seq]
        seq_base = self._persisted_log_seq
        self._persisted_log_seq = s.log_seq
        async with SessionLocal() as session:
            await persistence.record_sample(session, self.run_id, s)
            await persistence.record_events(session, self.run_id, new_logs, seq_base)


def _task_label(a):
    t = a.task
    if not t:
        return "selecting goal…"
    k = t["type"]
    if k == "MINE":
        return f"mine ({t['tile'][0]},{t['tile'][1]})"
    if k == "DELIVER":
        return f"haul {a.cargo} ore"
    if k == "BUILD":
        return f"build {a.build_idx + 1}/{len(a.build_queue)}"
    if k == "REPAIR":
        return f"repair ({t['fault']['x']},{t['fault']['y']})"
    if k == "SUPERVISE":
        return "supervising"
    return "…"
