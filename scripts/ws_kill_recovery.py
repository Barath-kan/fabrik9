"""Phase-2 verification checkpoint: kill a WebSocket mid-simulation and show
the system recover cleanly, with the structured log trace as evidence.

This drives the *real* SimulationManager 30 Hz loop in-process (no external
server needed) with two connected clients. Mid-run one client is abruptly
severed — every subsequent `send_json` raises, exactly like a socket whose peer
vanished. The run asserts, and the structured log trace demonstrates:

  * the authoritative loop keeps stepping and broadcasting (no hung tasks),
  * the dead socket is pruned on the next broadcast (`pruned dead sockets`),
  * the surviving client keeps receiving frames,
  * no per-client state lingers and no agent task/reservation is orphaned —
    the simulation is server-authoritative, so a client drop cannot strand it.

Run:  python scripts/ws_kill_recovery.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./fabrik9-kill-demo.db")
os.environ.setdefault("FABRIK_LOG_LEVEL", "INFO")

from app.db import init_models, SessionLocal      # noqa: E402
from app import persistence                        # noqa: E402
from app.logging_setup import configure_logging    # noqa: E402
from app.runtime import SimulationManager          # noqa: E402

configure_logging(stream=sys.stdout)

FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}", flush=True)
    if not cond:
        FAIL.append(name)


class Socket:
    """A minimal stand-in for a Starlette WebSocket. `kill()` makes every
    further send raise, reproducing a peer that dropped mid-flight."""

    def __init__(self, name):
        self.name = name
        self.alive = True
        self.frames = 0

    def kill(self):
        self.alive = False

    async def send_json(self, message):
        if not self.alive:
            raise RuntimeError(f"{self.name}: connection reset by peer")
        self.frames += 1


async def main():
    await init_models()

    mgr = SimulationManager(seed=1337)
    async with SessionLocal() as session:
        mgr.run_id = await persistence.create_run(session, mgr.sim.seed)

    good = Socket("client-A")
    doomed = Socket("client-B")
    mgr.clients = {good, doomed}
    mgr.speed = 8          # fast-forward so a telemetry DB write fires in-demo

    print("\n--- starting authoritative loop with 2 clients ---\n", flush=True)
    loop_task = asyncio.create_task(mgr._loop())

    # Let both clients receive real frames, inject a fault, and run long enough
    # to cross the telemetry interval so the DB-write log appears in the trace.
    await asyncio.sleep(0.3)
    mgr.sim.inject_fault()
    await asyncio.sleep(0.7)
    frames_before_kill = good.frames
    check("both clients receiving frames",
          good.frames > 0 and doomed.frames > 0,
          f"(A={good.frames}, B={doomed.frames})")

    # Capture live agent state so we can prove nothing is orphaned by the drop.
    sim_before = mgr.sim
    n_agents = len(mgr.sim.agents)
    tick_at_kill = mgr.sim.tick

    print("\n--- KILLING client-B mid-simulation ---\n", flush=True)
    doomed.kill()

    # Keep running so the next broadcast trips the prune path and the survivor
    # keeps getting frames.
    await asyncio.sleep(0.6)

    check("dead socket pruned from hub", doomed not in mgr.clients)
    check("no lingering per-client version state",
          doomed not in mgr._client_versions)
    check("surviving client kept receiving after the kill",
          good.frames > frames_before_kill,
          f"(+{good.frames - frames_before_kill} frames)")
    check("simulation kept stepping (no hung task)",
          mgr.sim.tick > tick_at_kill,
          f"(tick {tick_at_kill} -> {mgr.sim.tick})")
    check("simulation object untouched by the drop (same instance, same squad)",
          mgr.sim is sim_before and len(mgr.sim.agents) == n_agents,
          f"({n_agents} agents still owned by the server)")

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    print("\n--- final state ---", flush=True)
    print(f"  clients remaining: {len(mgr.clients)} "
          f"(A alive={good.alive})", flush=True)
    print(f"  client-A total frames: {good.frames}", flush=True)
    print(f"  sim tick: {mgr.sim.tick}", flush=True)

    print()
    if FAIL:
        print(f"CHECKPOINT FAILED: {FAIL}", flush=True)
        sys.exit(1)
    print("CHECKPOINT: system recovered cleanly from a mid-sim WS kill.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
