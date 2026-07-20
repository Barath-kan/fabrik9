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
            reply = _apply_control(mgr, msg)
            if reply is not None:
                await ws.send_json(reply)
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
    """Apply one control message. Returns an optional reply dict to send back
    to *this* client only (used for targeted-fault rejections); None means
    nothing to reply."""
    action = msg.get("action")
    reply = None
    if action == "pause":
        mgr.paused = True
    elif action == "resume":
        mgr.paused = False
    elif action == "speed":
        v = int(msg.get("value", 1))
        mgr.speed = max(1, min(8, v))
    elif action == "inject_fault":
        # x/y present -> a user-clicked target that must be validated and
        # reachability-checked; absent -> the original random injection.
        x, y = msg.get("x"), msg.get("y")
        if x is not None and y is not None:
            try:
                result = mgr.sim.inject_fault(x, y)
            except (TypeError, ValueError):
                result = {"ok": False, "reason": "invalid coordinates"}
            if not result.get("ok"):
                reply = {"type": "fault_rejected", "reason": result["reason"]}
        else:
            mgr.sim.inject_fault()
    elif action == "chaos":
        mgr.sim.chaos_on = bool(msg.get("value"))
        mgr.sim.add_log(
            "Chaos mode enabled — random faults every 30s"
            if mgr.sim.chaos_on else "Chaos mode disabled",
            "ev-warn" if mgr.sim.chaos_on else "")
    if action:
        log.debug("control action", extra={"action": action})
    return reply
