"""Extrusion monitor web app: live IR view, all printer topics, graphs, and LEAM correction requests."""
import asyncio
import json
import logging
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import control_config, model_files
from .bridge import DATA_DIR, OPERATIONS, Bridge, now_ms
from .control_state import TARGET_MM_RANGE, ControlState, send_enabled
from .pipeline import Pipeline, env_controller, env_settings, load_models
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / "static"
bridge = Bridge()
control = ControlState(DATA_DIR / "control.json")
VERSION = os.environ.get("APP_VERSION", "dev")
log = logging.getLogger(__name__)
HOLD_MAX_ESTIMATE_AGE_MS = 5000  # "hold current" only adopts an estimate that is still live
HOLD_MIN_CONFIDENCE = control_config.DEFAULTS["min_confidence"]  # the controller acts on nothing less certain
NUMPY_ENCODERS = {np.generic: lambda v: v.item(), np.ndarray: lambda a: a.tolist()}
failed_pipeline_fields = set()
FRAME_MAX_AGE_MS = 2000  # guide: a held display frame may be shown for up to 2 s; older ones are never sent


class StderrNow(logging.StreamHandler):
    """Writes to the current sys.stderr, which tests swap per test."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


def configure_logging():
    """The app's own log lines print at LOG_LEVEL (default INFO) whatever level uvicorn runs at (the image: warning)."""
    logger = logging.getLogger("server")
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(level if isinstance(logging.getLevelName(level), int) else logging.INFO)
    if not any(getattr(h, "app_handler", False) for h in logger.handlers):
        handler = StderrNow()
        handler.app_handler = True
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


configure_logging()


def as_number(value):
    value = float(value)
    return int(value) if value.is_integer() else value


def open_store():
    try:
        return Store(DATA_DIR / "prototype.db")
    except Exception:  # noqa: BLE001 - monitoring and advice still work without the log
        log.exception("could not open the width log; running without it")
        return None


def start_pipeline(store):
    try:
        pipeline = Pipeline(bridge, control, store, **load_models(), controller_config=env_controller(),
                            settings=env_settings(), emit=bridge._emit)
    except Exception:  # noqa: BLE001 - the monitor and manual controls must come up even if the pipeline can't
        log.exception("width pipeline failed to start; running without it")
        return None
    pipeline.start()
    return pipeline


@asynccontextmanager
async def lifespan(app):
    store = open_store()
    pipeline = start_pipeline(store)
    if pipeline:
        app.state.pipeline = pipeline
    bridge.connect()
    try:
        yield
    finally:
        if pipeline:
            pipeline.stop()
        if store:
            store.close()


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
        return send_manual(request)
    operation = body.get("operation")
    if operation not in OPERATIONS:
        return {"ok": False, "error": f"unknown operation {operation!r}"}
    try:
        request = bridge.make_request(operation, as_number(body.get("value")))
    except (TypeError, ValueError) as e:
        return bridge.log_refused({"operation": operation, "value": body.get("value")}, str(e))
    return send_manual(request)


def send_manual(request):
    result = bridge.send_request(request)
    pipeline = getattr(app.state, "pipeline", None)
    try:
        if hasattr(pipeline, "note_manual_send"):
            pipeline.note_manual_send(request, result)
    except Exception:  # noqa: BLE001 - the operator must still get the send result
        log.warning("pipeline could not note a manual send", exc_info=True)
    return result


@app.get("/api/request-template")
def request_template(operation: str = "flow_percent", value: float = 100.0):
    if operation not in OPERATIONS:
        return {"error": f"unknown operation {operation!r}"}
    try:
        return bridge.make_request(operation, as_number(value))
    except ValueError as e:
        return {"error": str(e)}


def finite(value):
    """Non-finite floats become null: JSONResponse refuses NaN and browsers cannot parse it."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [finite(v) for v in value]
    return value


def pipeline_value(name, default):
    """A field of app.state.pipeline, as an attribute or a zero-argument method; absent or failing gives default."""
    try:
        value = getattr(getattr(app.state, "pipeline", None), name, None)
        if callable(value):
            value = value()
        return default if value is None else finite(jsonable_encoder(value, custom_encoder=NUMPY_ENCODERS))
    except Exception:  # noqa: BLE001 - a broken pipeline field must never take the control endpoint down
        if name not in failed_pipeline_fields:
            failed_pipeline_fields.add(name)
            log.warning("pipeline.%s failed; reporting it as empty", name, exc_info=True)
        return default


def control_view():
    return {**control.public(), "server_ms": now_ms(), "ceiling_mm": pipeline_value("ceiling_mm", None),
            "relative": pipeline_value("relative", False) is True, "estimate": pipeline_value("estimate", None),
            "last_decision": pipeline_value("last_decision", None), "pinned": pipeline_value("pinned", {}),
            "models": model_files.status(), "limits": pipeline_value("limits", {}),
            "send_enabled": send_enabled(), "load_error": control.load_error, "persist_error": control.persist_error}


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def current_estimate_mm():
    estimate = pipeline_value("estimate", None)
    if not isinstance(estimate, dict):
        raise ValueError("no fresh width estimate to hold; wait for a measurement or enter a target")
    mm, age_ms, n, confidence = (estimate.get(k) for k in ("mm", "age_ms", "n", "confidence"))
    if not all(is_number(v) for v in (mm, age_ms)) or not 0 <= age_ms <= HOLD_MAX_ESTIMATE_AGE_MS:
        raise ValueError("no fresh width estimate to hold; wait for a measurement or enter a target")
    if not is_number(n) or n < 1 or not is_number(confidence) or confidence < HOLD_MIN_CONFIDENCE:
        raise ValueError("the width estimate is too uncertain to hold; wait for a steadier measurement or enter a "
                         f"target (needs confidence of at least {HOLD_MIN_CONFIDENCE})")
    if not TARGET_MM_RANGE[0] <= mm <= TARGET_MM_RANGE[1]:
        raise ValueError(f"current estimate {mm:.2f} mm is outside the allowed target range "
                         f"{TARGET_MM_RANGE[0]}-{TARGET_MM_RANGE[1]} mm")
    ceiling = shown_ceiling_mm()  # near the knee an estimate can read a little above what the camera can see
    return round(mm, 3) if ceiling is None else min(round(mm, 3), ceiling)


def shown_ceiling_mm():
    """The camera's ceiling floored to 2 decimals, so any target we offer or set is one it can see; None without."""
    ceiling = pipeline_value("ceiling_mm", None)
    return math.floor(ceiling * 100 + 1e-9) / 100 if is_number(ceiling) else None


def check_typed_target(target):
    """The camera can't see past its ceiling, and a relative model knows no absolute width."""
    if pipeline_value("relative", False) is True:
        raise ValueError("this camera model only follows changes in width, not the width itself: "
                         "use Hold current width to keep the width you have now")
    ceiling, shown = pipeline_value("ceiling_mm", None), shown_ceiling_mm()
    if is_number(target) and is_number(ceiling) and target > ceiling:
        raise ValueError(f"the camera can't see beads wider than {shown:.2f} mm; set a target of at most {shown:.2f}")


def target_stamp(by):
    """The camera model a target is set under; None without a pipeline or camera model."""
    fn = getattr(getattr(app.state, "pipeline", None), "target_stamp", None)
    try:
        return fn(by) if callable(fn) else None
    except Exception:  # noqa: BLE001 - an unstamped target is never acted on, so failing here is safe
        log.warning("could not stamp the target", exc_info=True)
        return None


def apply_control(body):
    try:
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object with target_mm, auto and/or hold_current")
        body = dict(body)
        hold = body.pop("hold_current", False)
        if not isinstance(hold, bool):
            raise ValueError("hold_current must be true or false")
        if hold and "target_mm" in body:
            raise ValueError("send either target_mm or hold_current, not both")
        if hold:
            body["target_mm"] = current_estimate_mm()
        elif body.get("target_mm") is not None:
            check_typed_target(body["target_mm"])
        stamp = target_stamp("hold" if hold else "typed") if body.get("target_mm") is not None else None
        control.update(body, stamp=stamp)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"error": f"could not apply the change: {e}"}, status_code=500)
    return control_view()


@app.get("/api/control")
def get_control():
    return control_view()


@app.get("/api/pipeline")
def get_pipeline():
    """Read-only health of the width loop: clock offset, gate, counters, errors per stage, model versions."""
    status = pipeline_value("status", None)
    models = {name: s["version"] if s["present"] else None for name, s in model_files.status().items()}
    return {"server_ms": now_ms(), "running": isinstance(status, dict), **(status or {}), "models": models}


@app.post("/api/control")
async def post_control(request: Request):
    try:
        body = json.loads(await request.body())
    except Exception:  # noqa: BLE001 - invalid UTF-8, invalid JSON or absurd nesting are all just a bad body
        return JSONResponse({"error": "body must be valid JSON"}, status_code=400)
    return await run_in_threadpool(apply_control, body)


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
