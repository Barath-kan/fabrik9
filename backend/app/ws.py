"""WebSocket endpoint: state stream out, control messages in."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


def get_manager():
    from .main import manager
    return manager


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    mgr = get_manager()
    mgr.clients.add(ws)
    try:
        # immediate world + state so a new client renders instantly
        await ws.send_json(mgr.world_message())
        mgr._client_versions[ws] = mgr.sim.structure_version
        await ws.send_json(mgr.state_message())

        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            if action == "pause":
                mgr.paused = True
            elif action == "resume":
                mgr.paused = False
            elif action == "speed":
                v = int(msg.get("value", 1))
                mgr.speed = max(1, min(8, v))
            elif action == "inject_fault":
                mgr.sim.inject_fault()
            elif action == "chaos":
                mgr.sim.chaos_on = bool(msg.get("value"))
                mgr.sim.add_log(
                    "Chaos mode enabled — random faults every 30s"
                    if mgr.sim.chaos_on else "Chaos mode disabled",
                    "ev-warn" if mgr.sim.chaos_on else "")
    except WebSocketDisconnect:
        pass
    finally:
        mgr.clients.discard(ws)
        mgr._client_versions.pop(ws, None)
