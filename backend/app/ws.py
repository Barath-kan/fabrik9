"""WebSocket endpoint: state stream out, control messages in."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .logging_setup import get_logger

router = APIRouter()
log = get_logger("ws")


def get_manager():
    from .main import manager
    return manager


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    mgr = get_manager()
    mgr.clients.add(ws)
    log.info("client connected", extra={"clients": len(mgr.clients)})
    reason = "disconnect"
    try:
        # immediate world + state so a new client renders instantly
        await ws.send_json(mgr.world_message())
        mgr._client_versions[ws] = mgr.sim.structure_version
        await ws.send_json(mgr.state_message())

        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                # A malformed / non-JSON frame must not tear down the socket;
                # log it and keep serving this client.
                log.warning("ignored malformed client frame")
                continue
            _apply_control(mgr, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        # Never let an unexpected error escape without a trace — cleanup below
        # still runs, so the manager's client set stays consistent.
        reason = "error"
        log.exception("ws handler crashed")
    finally:
        mgr.clients.discard(ws)
        mgr._client_versions.pop(ws, None)
        # Client state lives only in these two collections; the simulation and
        # every agent task are server-owned, so nothing is orphaned on drop.
        log.info("client disconnected",
                 extra={"reason": reason, "clients": len(mgr.clients)})


def _apply_control(mgr, msg):
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
    if action:
        log.debug("control action", extra={"action": action})
