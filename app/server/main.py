"""Extrusion monitor web app: live IR/RGB view, all printer topics, graphs, and flow/PA requests."""
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .bridge import Bridge

STATIC = Path(__file__).resolve().parent.parent / "static"
bridge = Bridge()
OPERATIONS = {"flow_percent": (0.0, 200.0), "pressure_advance_seconds": (0.0, 1.0)}


def build_request(operation, value):
    # Field names are our best guess until LEAM shares mini_event_extrusion.schema.json; the UI can send raw JSON.
    return {"schema_version": "0.1.0", "request_id": str(uuid.uuid4()), "operation": operation,
            operation: value, "value": value, "requested_utc_ms": int(time.time() * 1000)}


@asynccontextmanager
async def lifespan(app):
    bridge.connect()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/settings")
def get_settings():
    return bridge.public_settings()


@app.post("/api/settings")
def post_settings(new: dict = Body(...)):
    bridge.update_settings(new)
    return bridge.public_settings()


@app.get("/api/topics")
def get_topics():
    return bridge.topics_summary()


@app.post("/api/request")
def post_request(body: dict = Body(...)):
    if "raw" in body:
        try:
            request = json.loads(body["raw"]) if isinstance(body["raw"], str) else body["raw"]
        except ValueError as e:
            return {"ok": False, "error": f"invalid JSON: {e}"}
        return bridge.send_request(request)
    operation, value = body.get("operation"), body.get("value")
    if operation not in OPERATIONS:
        return {"ok": False, "error": f"unknown operation {operation!r}"}
    lo, hi = OPERATIONS[operation]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": "value must be a number"}
    if not lo <= value <= hi:
        return {"ok": False, "error": f"{operation} must be within {lo}–{hi}"}
    return bridge.send_request(build_request(operation, value))


@app.get("/api/request-template")
def request_template(operation: str = "flow_percent", value: float = 100.0):
    return build_request(operation, value)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=200)

    def listener(message):
        def put():
            if not queue.full():
                queue.put_nowait(message)
        loop.call_soon_threadsafe(put)

    bridge.listeners.append(listener)
    try:
        await websocket.send_json(bridge.snapshot())
        next_topics = time.monotonic()
        while True:
            if time.monotonic() >= next_topics:
                await websocket.send_json({"type": "topics", "topics": bridge.topics_summary()})
                next_topics = time.monotonic() + 1.0
            try:
                message = await asyncio.wait_for(queue.get(), timeout=max(0.05, next_topics - time.monotonic()))
                await websocket.send_json(message)
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bridge.listeners.remove(listener)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
