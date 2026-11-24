"""FastAPI server: REST API, WebSocket updates and the background simulation loop."""
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import building
from control import ControlError, ControlSystem
from devices import CLOCK, CommandError

log = logging.getLogger("smart-building")

ROOT = Path(__file__).resolve().parent.parent
systems: Dict[str, ControlSystem] = {}
sockets: Dict[str, set] = {}

# The simulation loop and the REST handlers (run in a thread pool) share the same state,
# so every access to a ControlSystem goes through this lock.
lock = threading.RLock()


async def sim_loop():
    """Tick every building once per second and push snapshots to WebSocket clients."""
    while True:
        try:
            with lock:
                dt = CLOCK.advance(1.0)
                for bid, cs in systems.items():
                    try:
                        cs.tick(dt)
                    except Exception:  # one faulty building must not stop the whole simulation
                        log.exception("tick failed for building '%s'", bid)
                snaps = {bid: systems[bid].snapshot() for bid, conns in sockets.items() if conns}
            for bid, snap in snaps.items():
                for ws in list(sockets[bid]):
                    try:
                        await ws.send_json(snap)
                    except Exception:
                        sockets[bid].discard(ws)
        except Exception:
            log.exception("simulation loop error")
        await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(app):
    rules = building.load_json("rules.json")
    for bid, b in building.load_buildings().items():
        systems[bid] = ControlSystem(b, rules)
        sockets[bid] = set()
        if b.mismatches():
            print(f"[warn] {bid}: totals differ from expected_totals {b.mismatches()}")
        if b.limit_warnings():
            print(f"[warn] {bid}: ATIOS SmartCore limits exceeded {b.limit_warnings()}")
    task = asyncio.create_task(sim_loop())
    yield
    task.cancel()


app = FastAPI(title="Smart Building Digital Twin", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------- request bodies ----------
class CommandBody(BaseModel):
    command: str
    args: Dict[str, Any] = {}


class InputBody(BaseModel):
    value: Any


class PressBody(BaseModel):
    event: str = "press"  # press, double_press, long_press, release


class ModeBody(BaseModel):
    mode: str  # home or away


class GeofenceBody(BaseModel):
    event: str = "arrive"  # arrive or leave


class RuleBody(BaseModel):
    enabled: bool


class SimBody(BaseModel):
    hour: Optional[float] = None   # pin the hour of day (0-24)
    real_time: bool = False        # true = use the computer clock again
    speed: Optional[float] = None  # timer speed multiplier


# ---------- helpers ----------
def get(bid):
    cs = systems.get(bid)
    if cs is None:
        raise HTTPException(404, f"unknown building '{bid}'")
    return cs


def run(fn, *a, **kw):
    """Run a control call under the lock and turn domain errors into HTTP 400."""
    try:
        with lock:
            return fn(*a, **kw)
    except (ControlError, CommandError) as e:
        raise HTTPException(400, str(e))
    except TypeError as e:  # bad or clashing keyword arguments sent by the client
        raise HTTPException(400, f"bad arguments: {e}")


# ---------- buildings ----------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/frontend/")


@app.get("/api/buildings")
def list_buildings():
    return [{"id": bid, "name": cs.b.name} for bid, cs in systems.items()]


@app.get("/api/buildings/{bid}")
def snapshot(bid: str):
    return run(get(bid).snapshot)


@app.get("/api/buildings/{bid}/validation")
def validation(bid: str):
    """Compare generated totals with expected_totals and show ATIOS SmartCore usage."""
    b = get(bid).b
    with lock:
        return {
            "totals": b.totals(),
            "expected": b.expected,
            "mismatches": {k: {"expected": e, "actual": a} for k, (e, a) in b.mismatches().items()},
            "atios": b.atios_load(),
            "limit_warnings": {k: {"used": u, "max": m} for k, (u, m) in b.limit_warnings().items()},
        }


# ---------- devices, pushbuttons, groups, scenes ----------
@app.post("/api/buildings/{bid}/devices/{did}/command")
def device_command(bid: str, did: str, body: CommandBody):
    return run(get(bid).command, did, body.command, "online", **body.args)


@app.post("/api/buildings/{bid}/devices/{did}/input")
def device_input(bid: str, did: str, body: InputBody):
    """Simulate a sensor value or a window contact (sensor changes are not user commands)."""
    return run(get(bid).set_input, did, body.value)


@app.post("/api/buildings/{bid}/pushbuttons/{did}/press")
def pushbutton(bid: str, did: str, body: PressBody):
    return run(get(bid).press, did, body.event)


@app.post("/api/buildings/{bid}/groups/{gid}/command")
def group_command(bid: str, gid: str, body: CommandBody):
    return run(get(bid).group_command, gid, body.command, "online", **body.args)


@app.post("/api/buildings/{bid}/scenes/{sid}")
def scene(bid: str, sid: str):
    run(get(bid).scene, sid, "online")
    return {"ok": True}


@app.post("/api/buildings/{bid}/rooms/{rid}/resume")
def resume(bid: str, rid: str):
    run(get(bid).resume_auto, rid)
    return {"ok": True}


# ---------- mode, geofence, rules, safety ----------
@app.post("/api/buildings/{bid}/mode")
def set_mode(bid: str, body: ModeBody):
    return run(get(bid).set_mode, body.mode)


@app.post("/api/buildings/{bid}/geofence")
def geofence(bid: str, body: GeofenceBody):
    return run(get(bid).geofence, body.event)


@app.get("/api/buildings/{bid}/rules")
def list_rules(bid: str):
    cs = get(bid)
    with lock:
        return [{"id": r["id"], "name": r["name"], "priority_source": r.get("priority_source"),
                 "opt_in": bool(r.get("requires_user_opt_in")), "enabled": cs.enabled[r["id"]]}
                for r in cs.rules["rules"]]


@app.put("/api/buildings/{bid}/rules/{rule_id}")
def set_rule(bid: str, rule_id: str, body: RuleBody):
    """Enable or disable a rule, for example the opt-in garage door rule R22."""
    return run(get(bid).set_rule_enabled, rule_id, body.enabled)


@app.post("/api/buildings/{bid}/safety/reset")
def safety_reset(bid: str):
    run(get(bid).reset_safety)
    return {"ok": True}


# ---------- simulation clock ----------
@app.get("/api/sim")
def sim_state():
    with lock:
        return {"hour": CLOCK.hour(), "speed": CLOCK.speed, "pinned": CLOCK.hour_override is not None}


@app.post("/api/sim")
def sim(body: SimBody):
    with lock:
        if body.real_time:
            CLOCK.hour_override = None
        if body.hour is not None:
            CLOCK.hour_override = body.hour % 24
        if body.speed is not None:
            CLOCK.speed = max(0.1, min(body.speed, 600.0))
        return {"hour": CLOCK.hour(), "speed": CLOCK.speed, "pinned": CLOCK.hour_override is not None}


# ---------- WebSocket ----------
@app.websocket("/ws/{bid}")
async def ws(websocket: WebSocket, bid: str):
    if bid not in systems:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    sockets[bid].add(websocket)
    try:
        with lock:
            first = systems[bid].snapshot()
        await websocket.send_json(first)
        while True:
            await websocket.receive_text()  # keep the socket open; updates are pushed by sim_loop
    except WebSocketDisconnect:
        pass
    finally:
        sockets[bid].discard(websocket)


# Static files go last so they never shadow the API routes
app.mount("/data", StaticFiles(directory=ROOT / "data"), name="data")
if (ROOT / "models").exists():
    app.mount("/models", StaticFiles(directory=ROOT / "models"), name="models")
if (ROOT / "frontend").exists():
    app.mount("/frontend", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")