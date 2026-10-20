"""Extrusion monitor web app: live IR/RGB view, all printer topics, graphs, and LEAM correction requests."""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .bridge import OPERATIONS, Bridge, now_ms

STATIC = Path(__file__).resolve().parent.parent / "static"
bridge = Bridge()
VERSION = os.environ.get("APP_VERSION", "dev")
FRAME_MAX_AGE_MS = 2000  # guide: a held display frame may be shown for up to 2 s; older ones are never sent


def as_number(value):
    value = float(value)
    return int(value) if value.is_integer() else value


@asynccontextmanager
async def lifespan(app):
    bridge.connect()
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def no_stale_pages(request, call_next):
    # Browsers otherwise keep running the previous version's scripts after a deploy.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/version")
def get_version():
    return {"version": VERSION}


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
    operation = body.get("operation")
    if operation not in OPERATIONS:
        return {"ok": False, "error": f"unknown operation {operation!r}"}
    try:
        request = bridge.make_request(operation, as_number(body.get("value")))
    except (TypeError, ValueError) as e:
        return bridge.log_refused({"operation": operation, "value": body.get("value")}, str(e))
    return bridge.send_request(request)


@app.get("/api/request-template")
def request_template(operation: str = "flow_percent", value: float = 100.0):
    if operation not in OPERATIONS:
        return {"error": f"unknown operation {operation!r}"}
    try:
        return bridge.make_request(operation, as_number(value))
    except ValueError as e:
        return {"error": str(e)}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=200)

    def listener(message):
        def put():
            if queue.full():
                queue.get_nowait()  # a slow client loses the oldest message, never the newest
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
            except asyncio.TimeoutError:
                continue
            if message["type"] == "frame":
                age_ms = now_ms() - message["recv_ms"]
                if age_ms > FRAME_MAX_AGE_MS:
                    continue
                message = {**message, "age_ms": age_ms}
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bridge.listeners.remove(listener)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
