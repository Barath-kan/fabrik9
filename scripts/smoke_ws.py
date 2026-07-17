"""Live integration test against a running server (uvicorn on :8000).

Verifies the full stack end to end:
  1. WS connects and receives world + state messages
  2. State ticks advance at ~30 fps
  3. Controls work (pause freezes the tick, speed multiplies it)
  4. Fault injection appears in the state stream
  5. REST save -> load roundtrip restores tick and pauses the sim
Run:  python scripts/smoke_ws.py
"""

import asyncio, json, sys
import requests
import websockets

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws"
FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAIL.append(name)


async def main():
    print("1) WebSocket connect + message types")
    async with websockets.connect(WS) as ws:
        first = json.loads(await ws.recv())
        second = json.loads(await ws.recv())
        check("first message is world", first["type"] == "world",
              f"(grid {len(first['grid'])}x{len(first['grid'][0])})")
        check("second message is state", second["type"] == "state")

        print("2) Tick rate (drain stream for ~1.5s wall time)")
        t0 = second["tick"]
        t_end = asyncio.get_event_loop().time() + 1.5
        last_tick = t0
        frames = 0
        while asyncio.get_event_loop().time() < t_end:
            m = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if m["type"] == "state":
                frames += 1
                last_tick = m["tick"]
        advanced = last_tick - t0
        check("ticks advance ~30/s", 35 <= advanced <= 60, f"(+{advanced} ticks)")
        check("broadcast ~30 fps", 35 <= frames <= 60, f"({frames} frames)")

        print("3) Pause control")
        await ws.send(json.dumps({"action": "pause"}))
        await asyncio.sleep(0.3)
        # drain, then sample two states
        a = b = None
        deadline = asyncio.get_event_loop().time() + 2
        while asyncio.get_event_loop().time() < deadline:
            m = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if m["type"] == "state" and m["paused"]:
                if a is None:
                    a = m["tick"]
                    await asyncio.sleep(0.3)
                else:
                    b = m["tick"]
                    break
        check("paused sim does not tick", a is not None and b == a, f"({a}->{b})")

        print("4) Speed control (drain stream for ~1s at 4x)")
        await ws.send(json.dumps({"action": "resume"}))
        await ws.send(json.dumps({"action": "speed", "value": 4}))
        s1 = None
        t_end = asyncio.get_event_loop().time() + 1.2
        s2 = None
        while asyncio.get_event_loop().time() < t_end:
            m = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if m["type"] == "state" and not m["paused"]:
                if s1 is None:
                    s1 = m["tick"]
                s2 = m["tick"]
        check("4x speed ticks ~120/s", s1 is not None and s2 - s1 >= 90,
              f"(+{(s2-s1) if s1 is not None else 0} in ~1.2s)")
        await ws.send(json.dumps({"action": "speed", "value": 1}))

        print("5) Fault injection (fast-forward to automation, verify via REST)")
        await ws.send(json.dumps({"action": "speed", "value": 8}))
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline:
            st = requests.get(f"{BASE}/api/state").json()
            if st["stats"]["miners"] >= 1:
                break
            await asyncio.sleep(0.5)
        await ws.send(json.dumps({"action": "speed", "value": 1}))
        check("automation built under fast-forward", st["stats"]["miners"] >= 1,
              f"(tick {st['tick']}, miners {st['stats']['miners']})")
        await ws.send(json.dumps({"action": "inject_fault"}))
        saw = False
        deadline = asyncio.get_event_loop().time() + 5
        base_fixed = st["stats"]["faults_fixed"]
        while asyncio.get_event_loop().time() < deadline:
            st2 = requests.get(f"{BASE}/api/state").json()
            if st2["faults"] or st2["stats"]["faults_fixed"] > base_fixed:
                saw = True
                break
            await asyncio.sleep(0.2)
        check("injected fault detected or repaired", saw)

    print("6) REST: save -> load roundtrip")
    r = requests.post(f"{BASE}/api/runs/current/snapshots",
                      json={"label": "smoke"})
    check("save returns 200", r.status_code == 200, r.text[:80])
    snap = r.json()
    saved_tick = snap["tick"]

    r = requests.get(f"{BASE}/api/snapshots")
    ids = [s["id"] for s in r.json()]
    check("snapshot listed", snap["id"] in ids, f"({len(ids)} snapshots)")

    r = requests.post(f"{BASE}/api/snapshots/{snap['id']}/load")
    check("load returns 200", r.status_code == 200)
    j = r.json()
    check("loaded tick matches saved", j["tick"] == saved_tick,
          f"(saved {saved_tick}, loaded {j['tick']})")
    check("sim paused after load", j["paused"] is True)

    r = requests.get(f"{BASE}/api/health")
    check("health after load", r.json()["tick"] == saved_tick)

    print("7) Telemetry persisted")
    run_id = requests.get(f"{BASE}/api/health").json()["run_id"]
    r = requests.get(f"{BASE}/api/runs/{run_id}/telemetry")
    check("telemetry endpoint responds", r.status_code == 200,
          f"({len(r.json())} samples for loaded run)")

    print()
    if FAIL:
        print(f"SMOKE TEST FAILED: {FAIL}")
        sys.exit(1)
    print("SMOKE TEST: ALL CHECKS PASS")


asyncio.run(main())
