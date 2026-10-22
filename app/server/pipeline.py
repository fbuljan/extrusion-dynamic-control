"""Live width loop inside the app: IR frame -> width sample -> real-width estimate -> controller -> (Auto) request.

Threads: the MQTT thread only files messages (bridge hook); one worker measures the newest IR frame (older waiting
frames are dropped); one loop steps the controller at 1 Hz and is the only thread that touches it (writer results and
manual sends reach it through an inbox). IR bytes live only in the one-frame slot and the worker's locals, and nothing
image-like is stored. Clocks: frame and motion times are LEAM's; the controller, the estimator's windows and the store
use ours. Sample times cross over with offset = min over 30 s of (telemetry receive time - motion source time); without
telemetry the deposit time is left out, so effect gating falls back to receive time.
A request is sent only when Auto is on, CONTROL_SEND_ENABLED=1, the decision is a non-hold action on a controller lever
the operator left on, within its step, and the gate is open, all checked again right before the request is built.
Every step also leaves a scalar row in a 15-minute trace (width, target and band, Auto, setpoints, decision) for the
Test bench page, next to the operator's events (manual sends, Auto on/off, target, band and lever changes).
"""
import base64
import dataclasses
import hashlib
import importlib
import json
import logging
import math
import os
import statistics
import threading
import time
from collections import Counter, deque
from pathlib import Path

from . import control_config, model_files
from .bridge import STALE_SOURCE_MS, dig, now_ms, telemetry_metrics
from .control_state import send_enabled
from .controller import Controller, Decision, lead_with
from .estimator import WidthEstimator
from .geometry import Geometry
from .width import MotionBuffer, WidthExtractor, frame_time_ms, motion_from_telemetry

log = logging.getLogger(__name__)

DEFAULTS = {
    "control_period_s": 1.0,
    "status_fresh_ms": 5000,  # an older writer status closes the gate
    "telemetry_fresh_ms": 2000,  # telemetry expires 1 s after publishing (hold when expired); +1 s for delivery
    "offset_window_ms": 30_000,
    "width_emit_ms": 500,  # WS width messages at most 2 Hz
    "decision_store_ms": 10_000,  # an unchanged decision is still stored this often
    "first_layer_mm": 0.7,  # slicing of the golden prints: 0.7 mm first layer, then 0.5 mm, walls 1.5 mm apart
    "layer_mm": 0.5,
    "line_width_mm": 1.5,
    "max_clock_offset_ms": 2000,  # beyond this our clock or LEAM's is wrong: hold and say so
    "trace_s": 900,
    # The dashboard's width averages over this many seconds; the controller keeps the estimator's 25 s, where a shorter
    # window widens the noise band faster than it cuts the lag (D51).
    "display_window_s": 15.0,
}
ENV_SETTINGS = {"FIRST_LAYER_MM": "first_layer_mm", "LAYER_MM": "layer_mm", "LINE_WIDTH_MM": "line_width_mm",
                "DISPLAY_WINDOW_S": "display_window_s"}
TARGET_ALARM = "alarm: target set under another camera model; set it again"
STATS = ("frames_done", "frames_dropped", "sent", "send_refused", "send_failed", "send_uncertain", "foreign_results")
TRACE_REASON_CHARS = 200


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def jsonable(value):
    """Numpy scalars as Python numbers and non-finite floats as None: the WS encoder would write NaN."""
    if not isinstance(value, (bool, int, float, str)) and callable(getattr(value, "item", None)) \
            and getattr(value, "shape", None) == ():
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def width_config_hash(cfg):
    """Ties a camera fit to the WidthExtractor settings it was fitted with (fit_camera writes the same into its JSON)."""
    return hashlib.sha1(json.dumps(cfg, sort_keys=True, default=list).encode()).hexdigest()[:12]


def geometry_version(model):
    """geometry.json's "version", or a digest of the whole file when it has none."""
    version = model.get("version")
    if version is not None:
        return str(version)
    return hashlib.sha1(json.dumps(model, sort_keys=True).encode()).hexdigest()[:12]


def aim_free(version):
    """A geometry version minus its "+nozzle-..." refits, which only re-aim the window and leave a camera fit valid."""
    return str(version).split("+nozzle-")[0]


def model_field(model, name):
    value = getattr(model, name, None)
    data = getattr(model, "data", None)
    return data.get(name) if value is None and isinstance(data, dict) else value


def env_settings(environ=None):
    """Slicing of the part being printed from FIRST_LAYER_MM, LAYER_MM and LINE_WIDTH_MM; a bad value keeps the default."""
    environ = os.environ if environ is None else environ
    out = {}
    for var, key in ENV_SETTINGS.items():
        raw = environ.get(var)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if is_number(value) and value > 0:
            out[key] = value
        else:
            log.warning("ignoring %s=%r: expected a positive number; using %s", var, raw, DEFAULTS[key])
    return out


WIDENS = {"flow_percent": 1, "speed_override_percent": -1, "nozzle_temperature_c": 1}  # direction that widens the bead


def _floor_widening(decision, current):
    """The floor rules' bigger step is for widening a bead narrower than wanted only."""
    action, error = decision.action or {}, decision.error_mm
    sign = WIDENS.get(action.get("operation"))
    return bool(getattr(decision, "floor", False)) and is_number(error) and error > 0 and sign is not None and \
        is_number(action.get("value")) and (action["value"] - current) * sign > 0


def env_controller(environ=None):
    """Controller overrides from CONTROL_PIN_S (seconds an operator's change is left alone), CONTROL_MIN_LAYER (first
    layer Auto may act on), CONTROL_FLOOR_STEP_FACTOR (step multiple under the floor rules, 1-2), CONTROL_PERSIST_S and
    CONTROL_PERSIST_ESTIMATES (how long a deviation must last), CONTROL_KAPPA (share of the gap per command) and
    CONTROL_MIN_WIDTH_MM (the walls limit) and the lever bounds CONTROL_{FLOW,SPEED,NOZZLE}_{MIN,MAX} (whole numbers
    inside LEAM's ranges); a bad value keeps that setting's default. The operator's settings on the Test bench go over
    these."""
    environ = os.environ if environ is None else environ
    overrides = {}
    for var, key, cast in (("CONTROL_PIN_S", "pin_s", float), ("CONTROL_MIN_LAYER", "min_layer", int),
                           ("CONTROL_FLOOR_STEP_FACTOR", "floor_step_factor", float),
                           ("CONTROL_PERSIST_S", "persist_s", float), ("CONTROL_PERSIST_ESTIMATES", "persist_estimates", int),
                           ("CONTROL_KAPPA", "kappa", float), ("CONTROL_MIN_WIDTH_MM", "min_width_mm", float)):
        raw = environ.get(var)
        if raw is None:
            continue
        try:
            value = cast(raw)
            if not math.isfinite(value) or key in ("min_layer", "persist_estimates") and value < 1:
                raise ValueError
            control_config.load({key: value})
        except ValueError:
            log.warning("ignoring %s=%r: keeping the default %s", var, raw, control_config.DEFAULTS[key])
            continue
        overrides[key] = value
    bounds = {}
    for short, op in (("FLOW", "flow_percent"), ("SPEED", "speed_override_percent"), ("NOZZLE", "nozzle_temperature_c")):
        for end in ("MIN", "MAX"):
            var, raw = f"CONTROL_{short}_{end}", environ.get(f"CONTROL_{short}_{end}")
            if raw is None:
                continue
            key = f"{op}_{end.lower()}"
            try:
                value = int(raw)
                control_config.load(control_config.with_operator(None, {**bounds, key: value}))
                lo, hi, _ = control_config.OPERATOR[key]
                if not lo <= value <= hi:
                    raise ValueError
            except ValueError:
                log.warning("ignoring %s=%r: keeping LEAM's range", var, raw)
                continue
            bounds[key] = value
    if bounds:
        overrides = control_config.with_operator(overrides, bounds)
    return overrides or None


def load_models(models_dir=None):
    """Fitted models by Pipeline keyword; a missing or unloadable one is None, so the app reports "no model"."""
    base = Path(models_dir) if models_dir is not None else model_files.MODELS_DIR
    status = model_files.status(base)
    loaders = {
        "geometry": Geometry.load,
        "camera_model": lambda path: importlib.import_module(".camera_model", __package__).CameraModel.load(path),
        "printer_model": lambda path: importlib.import_module(".printer_model", __package__).PrinterModel.load(path),
    }
    models = dict.fromkeys(loaders)
    for name, load in loaders.items():
        if status[name]["present"]:
            try:
                models[name] = load(base / model_files.MODEL_FILES[name])
            except Exception:  # noqa: BLE001 - a broken model file is a "no model" state, not a crash
                log.exception("could not load %s; running without it", name)
    return models


class Pipeline:
    def __init__(self, bridge, control, store, geometry=None, camera_model=None, printer_model=None,
                 controller_config=None, settings=None, extractor=None, clock_ms=now_ms, emit=None):
        self.bridge, self.control, self.store = bridge, control, store
        self.cfg = {**DEFAULTS, **(settings or {})}
        self.clock = clock_ms
        self.emit = emit or (lambda message: None)
        self.extractor = extractor if extractor is not None else WidthExtractor(geometry) if geometry else None
        self.controller = Controller(printer_model, controller_config)
        self.estimator = WidthEstimator(camera_model)
        self.display = WidthEstimator(camera_model, {"window_s": self.cfg["display_window_s"]})
        self._judged = None  # the controller's estimate at the last step
        self.motion = MotionBuffer()
        need = {"geometry": self.extractor is not None, "camera model": camera_model is not None}
        if self.controller.cfg["mode"] == "model":
            need["printer model"] = printer_model is not None
        self.missing = [name for name, ok in need.items() if not ok]
        self.extraction = "running" if self.extractor else "no geometry"
        self.camera_model = camera_model
        self.provenance, self.mismatch = self._provenance(geometry)
        self._can_act = not (self.missing or self.mismatch)
        self._pinned = {}
        self.offset_ms = None
        self.stats = Counter()
        self.errors = {}
        self._last_error = {}
        self._inbox = deque(maxlen=1000)  # ("result", payload, ms) | ("manual", op, value, ms) for the control loop
        self._est_lock = threading.Lock()
        self._motion_lock = threading.Lock()  # motion buffer, clock offsets, nozzle history
        self._frame_cv = threading.Condition()
        self._frame = None  # (jpeg_base64, meta) of the newest unprocessed IR frame
        self._offsets = deque()  # (recv_ms, recv_ms - motion source ms)
        self._nozzle = deque(maxlen=32)  # (LEAM ms, nozzle actual C)
        self._telemetry = None
        self._status = (None, None)  # (writer status payload, recv_ms)
        self._z_extruding = None
        self._feed = deque(maxlen=60)  # travel speed at 100 % override while extruding, ~4 s; corners dip it
        self._commands = {}  # request_id -> {"operation", "value"} for the controller's own sends
        self._last_sent_ms = None
        self._last_width_emit = -math.inf
        self._last_decision = None
        self._stored = (None, -math.inf)
        self._logged_offset = None
        self._trace_lock = threading.Lock()
        self._trace = deque(maxlen=int(self.cfg["trace_s"] / self.cfg["control_period_s"]))
        self._events = deque(maxlen=300)
        self._samples = deque(maxlen=int(self.cfg["trace_s"] * 3))  # every frame's outcome, ~2 a second
        self._stop = threading.Event()
        self._threads = []

    # lifecycle ------------------------------------------------------------------------------------------------------
    def start(self):
        self.bridge.hooks.append(self.on_message)
        self._threads = [threading.Thread(target=self._run_worker, name="pipeline-width", daemon=True),
                         threading.Thread(target=self._run_control, name="pipeline-control", daemon=True)]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout=5.0):
        """Stops sending and both threads; the caller closes the store afterwards."""
        self._stop.set()
        if self.on_message in self.bridge.hooks:
            self.bridge.hooks.remove(self.on_message)
        with self._frame_cv:
            self._frame = None
            self._frame_cv.notify_all()
        for thread in self._threads:
            thread.join(timeout)

    def _run_worker(self):
        while not self._stop.is_set():
            with self._frame_cv:
                self._frame_cv.wait_for(lambda: self._frame is not None or self._stop.is_set(), timeout=1.0)
            self.process_pending()

    def _run_control(self):
        period = self.cfg["control_period_s"]
        due = time.monotonic() + period
        while not self._stop.wait(max(0.0, due - time.monotonic())):
            due = max(due + period, time.monotonic())  # after a stall, tick once, not a burst
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the loop must outlive any single failure
                self._fail("control loop", e)

    # for main.control_view ------------------------------------------------------------------------------------------
    def estimate(self, now=None):
        """Display estimate: every recent sample over the display window, not the controller's post-change subset; age as
        of this call."""
        now = self.clock() if now is None else now
        with self._est_lock:
            return self.display.estimate(now)

    def last_decision(self):
        d = self._last_decision
        return None if d is None else {**d, "age_ms": self.clock() - d["t_ms"]}

    def pinned(self):
        """As of the last control step, without pins that have run out since."""
        now = self.clock()
        return {op: dict(p) for op, p in self._pinned.items() if p["until_ms"] > now}

    def limits(self):
        """What the controller may do (with the operator's tuning); nothing while a model it needs is missing."""
        return self.controller.limits() if self._can_act else {}

    def trace(self, since_ms=None):
        """Trace rows and operator events newer than since_ms (all kept without), oldest first."""
        with self._trace_lock:
            rows, events, samples = list(self._trace), list(self._events), list(self._samples)
        if since_ms is not None:
            rows = [r for r in rows if r["t_ms"] > since_ms]
            events = [e for e in events if e["t_ms"] > since_ms]
            samples = [x for x in samples if x["t_ms"] > since_ms]
        return {"rows": rows, "events": events, "samples": samples}

    def note_event(self, kind, **fields):
        """An operator change for the trace: "auto", "target", "band", "levers" or "manual" (a send by hand)."""
        with self._trace_lock:
            self._events.append(jsonable({"t_ms": self.clock(), "kind": kind, **fields}))

    def ceiling_mm(self):
        """Widest bead the camera can tell apart, or None."""
        fn = getattr(self.camera_model, "ceiling_mm", None)
        value = fn() if callable(fn) else None
        return float(value) if is_number(value) else None

    def relative(self):
        """The camera model holds IR widths, not real ones: only "hold current width" makes sense."""
        return bool(getattr(self.camera_model, "relative", False))

    def target_stamp(self, by):
        """What a target set now ("typed" or "hold") is bound to, or None without a camera model."""
        cam = self.camera_model
        if cam is None:
            return None
        version, profile = model_field(cam, "version"), getattr(cam, "profile", None)
        return {"camera_model": f"{version}:{profile}" if profile else str(version), "relative": self.relative(),
                "by": by}

    def target_problem(self, target, stamp):
        """Why this target must not be acted on under the loaded camera model, or None."""
        if target is None or self.camera_model is None:
            return None
        want = self.target_stamp("hold")
        if not (isinstance(stamp, dict) and stamp.get("camera_model") == want["camera_model"]
                and stamp.get("relative") is want["relative"] and (not want["relative"] or stamp.get("by") == "hold")):
            return TARGET_ALARM
        return None

    def _provenance(self, geometry):
        """("ok" | "unknown provenance" | what differs, hold?): a camera fit only holds for the geometry and width
        settings it was fitted with."""
        cam = self.camera_model
        if cam is None:
            return "no camera model", False
        ours = {"geometry_version": geometry_version(geometry.model) if isinstance(getattr(geometry, "model", None),
                                                                                  dict) else None,
                "width_config_hash": width_config_hash(self.extractor.cfg)
                if isinstance(getattr(self.extractor, "cfg", None), dict) else None}
        theirs = {name: model_field(cam, name) for name in ours}
        same = {"geometry_version": aim_free}
        differ = [f"{name} {theirs[name]} (running {ours[name]})" for name in ours
                  if None not in (theirs[name], ours[name])
                  and same.get(name, str)(theirs[name]) != same.get(name, str)(ours[name])]
        if differ:
            log.error("camera model fitted on a different setup: %s", "; ".join(differ))
            return "differs: " + "; ".join(differ), True
        if None in theirs.values() or None in ours.values():
            log.warning("camera model provenance unknown (camera %s, running %s)", theirs, ours)
            return "unknown provenance", False
        return "ok", False

    def status(self):
        offset = self.offset_ms
        return {"extraction": self.extraction, "missing": list(self.missing), "clock_offset_ms": offset,
                "clock_alarm": self._clock_alarm(offset), "gate_open": self.gate_open(),
                "ceiling_mm": self.ceiling_mm(), "relative": self.relative(), "camera_provenance": self.provenance,
                "settings": {k: self.cfg[k] for k in ENV_SETTINGS.values()},
                "stats": {**{k: 0 for k in STATS}, **self.stats}, "errors": dict(self.errors),
                "last_errors": dict(self._last_error)}

    def _clock_alarm(self, offset):
        if offset is None or abs(offset) <= self.cfg["max_clock_offset_ms"]:
            return None
        return f"alarm: device clock differs from LEAM's by {offset / 1000:+.1f} s"

    # incoming (MQTT thread) -----------------------------------------------------------------------------------------
    def on_message(self, topic, payload, recv_ms):
        if self._stop.is_set() or not isinstance(payload, dict):
            return
        try:
            if topic.endswith("/telemetry"):
                self._on_telemetry(payload, recv_ms)
            elif "/preview/ir/" in topic and topic.endswith("/trailing"):
                self._on_ir(payload, recv_ms)
            elif topic.endswith("/extrusion/status"):
                self._status = (payload, recv_ms)
            elif topic.endswith("/extrusion/result"):
                self._inbox.append(("result", payload, self.clock()))
        except Exception as e:  # noqa: BLE001 - one bad message must not stop the others
            self._fail(f"message {topic.rsplit('/', 1)[-1]}", e)

    def _on_telemetry(self, msg, recv_ms):
        m = telemetry_metrics(msg, recv_ms)
        sources, published = msg.get("sources") or {}, msg.get("published_utc_ms")

        def fresh(ns):  # within the producer's own clock, so our offset doesn't matter
            return is_number(published) and is_number(ns) and published - ns / 1e6 <= STALE_SOURCE_MS

        motion = motion_from_telemetry(msg)
        nozzle = m["nozzle"] or {}
        with self._motion_lock:
            if motion:
                self.motion.add(*motion)
                self._offsets.append((recv_ms, recv_ms - motion[0]))
                while self._offsets[0][0] < recv_ms - self.cfg["offset_window_ms"]:
                    self._offsets.popleft()
            heater_ns = dig(sources, "duet_readback", "payload", "temperatures", "nozzle", "source_utc_ns")
            if is_number(nozzle.get("current")) and is_number(heater_ns) and \
                    (not self._nozzle or heater_ns / 1e6 > self._nozzle[-1][0]):
                self._nozzle.append((heater_ns / 1e6, nozzle["current"]))
        extruding = m["extruding"] is True
        z = m["pos"].get("z") if isinstance(m["pos"], dict) else None
        if extruding and is_number(z):
            self._z_extruding = z
        if extruding and is_number(m["vel"]) and is_number(m["speed_override"]) and m["speed_override"] > 0:
            self._feed.append(m["vel"] * 100 / m["speed_override"])
        self._telemetry = {
            "recv_ms": recv_ms, "gateway": m["gateway"], "permits_outputs": m["permits_outputs"],
            "command_gate_closed": dig(sources, "gateway", "payload", "command_gate_closed"),
            "gateway_fresh": fresh(dig(sources, "gateway", "source_utc_ns")),
            "extruding": extruding, "nozzle_c": nozzle.get("current"),
            "feed_mm_s": statistics.median(self._feed) if self._feed else None,
            "setpoints": {
                "flow_percent": m["flow_pct"] if fresh(dig(sources, "duet_readback", "payload",
                                                           "extrusion_source_utc_ns")) else None,
                "speed_override_percent": m["speed_override"] if fresh(dig(sources, "motion", "source_utc_ns"))
                else None,
                "nozzle_temperature_c": nozzle.get("target"),
            },
        }

    def _on_ir(self, payload, recv_ms):
        if self.extractor is None:
            return
        b64, cam = payload.get("jpeg_base64"), payload.get("camera_index")
        if not isinstance(b64, str) or not isinstance(cam, int) or isinstance(cam, bool):
            self.stats["frames_unusable"] += 1
            return
        meta = {"camera_index": cam, "source_utc_ns": payload.get("source_utc_ns"), "sequence": payload.get("sequence"),
                "source_frame_id": payload.get("source_frame_id"), "recv_ms": recv_ms}
        with self._frame_cv:
            if self._frame is not None:
                self.stats["frames_dropped"] += 1
            self._frame = (b64, meta)
            self._frame_cv.notify()

    def _on_result(self, payload, now):
        """Control thread: by now a request of ours is in _commands, so a result can't overtake its registration."""
        rid, state = payload.get("request_id"), payload.get("state")
        cmd = self._commands.get(rid) if isinstance(rid, str) else None
        if cmd is not None:
            self.controller.note_result(rid, state, now)
            self._store("insert_commands", {"t_ms": now, **cmd, "request_id": rid,
                                            "state": state if isinstance(state, str) else None})
        elif not self._sent_by_this_app(rid):
            # Results don't name the operation; the readback shows which lever moved and pins it then.
            op = dig(payload, "correction", "operation") or "unknown"
            value = dig(payload, "observation", "observed_value")
            self.controller.note_external(op, value if is_number(value) else math.nan, now)
            self.stats["foreign_results"] += 1

    def _sent_by_this_app(self, rid, if_unsure=False):
        try:
            return rid in self.bridge.sent_ids
        except RuntimeError:  # appended to meanwhile; callers pick the answer that holds rather than sends
            return if_unsure

    def note_manual_send(self, request, result):
        """A request sent by hand from the dashboard: while Auto is on, the controller leaves that lever alone."""
        correction = request.get("correction") if isinstance(request, dict) else None
        if not (isinstance(result, dict) and result.get("ok") and isinstance(correction, dict)):
            return
        op, value = correction.get("operation"), correction.get("value")
        if isinstance(op, str) and is_number(value):
            if self.control.public()["auto"] is True:
                self._inbox.append(("manual", op, value, self.clock()))
            self.note_event("manual", operation=op, value=value)

    # width (worker thread) ------------------------------------------------------------------------------------------
    def process_pending(self):
        """Measures the waiting frame, if any; returns its sample on the local clock."""
        with self._frame_cv:
            item, self._frame = self._frame, None
        if item is None:
            return None
        try:
            return self._measure(*item)
        except Exception as e:  # noqa: BLE001 - a bad frame must not stop the worker
            self._fail("frame", e)
            return None

    def _measure(self, b64, meta):
        jpeg = base64.b64decode(b64, validate=True)
        t = frame_time_ms(meta)
        with self._motion_lock:
            motion = self.motion.at(t) if t is not None else None
        sample = self.extractor.process(jpeg, meta, motion)
        self.stats["frames_done"] += 1
        self.stats[f"sample_{sample.reason}"] += 1
        local = self._local_times(sample)
        self._store("insert_width_samples", {
            "t_ms": local.t_ms, "camera": sample.camera, "width_px": sample.width_px, "mm_per_px": sample.mm_per_px,
            "confidence": sample.confidence, "reason": sample.reason, "group": sample.group, "t_dep_ms": local.t_dep_ms})
        nozzle, speed = self._nozzle_at(t), (motion or {}).get("speed_mm_s")
        with self._est_lock:
            mm = self.estimator.add(local, nozzle, speed)
            self.display.add(local, nozzle, speed)
        # Every frame's outcome for the Test bench: the width it gave, or why it gave none. Scalars only.
        seen = jsonable({"t_ms": local.t_ms, "mm": mm, "reason": sample.reason if mm is not None or sample.reason != "ok"
                         else "not_converted", "group": sample.group})
        with self._trace_lock:
            self._samples.append(seen)
        self.emit({"type": "sample", **seen})
        self._emit_width()
        return local

    def clock_offset_ms(self):
        """Local minus LEAM clock (incl. the smallest delivery delay) over the last 30 s, or None without telemetry."""
        now = self.clock()
        with self._motion_lock:
            while self._offsets and self._offsets[0][0] < now - self.cfg["offset_window_ms"]:
                self._offsets.popleft()
            offset = min((d for _, d in self._offsets), default=None)
        self.offset_ms = offset
        if offset is not None and (self._logged_offset is None or abs(offset - self._logged_offset) >= 100):
            log.info("LEAM clock offset %+.0f ms (local = LEAM + offset)", offset)
            self._logged_offset = offset
        return offset

    def _local_times(self, sample):
        offset = self.clock_offset_ms()
        recv = sample.recv_ms if sample.recv_ms is not None else self.clock()
        if offset is None or not sample.t_ms:
            return dataclasses.replace(sample, t_ms=recv, t_dep_ms=None)
        dep = None if sample.t_dep_ms is None else int(round(sample.t_dep_ms + offset))
        return dataclasses.replace(sample, t_ms=int(round(sample.t_ms + offset)), t_dep_ms=dep)

    def _nozzle_at(self, t):
        with self._motion_lock:
            history = list(self._nozzle)
        if t is None or not history:
            return None
        ts, c = min(history, key=lambda e: abs(e[0] - t))
        return c if abs(ts - t) <= STALE_SOURCE_MS else None

    def _emit_width(self):
        now = self.clock()
        if now - self._last_width_emit < self.cfg["width_emit_ms"]:
            return
        self._last_width_emit = now
        with self._est_lock:
            est = self.display.estimate(now)
            why = self.display.reason
        if est is None:
            missing = "no model: camera model" if "camera model" in self.missing else why
            self.emit({"type": "width", "mm": None, "reason": missing})
            return
        self._store("insert_estimates", {"t_ms": now, **{k: est[k] for k in ("mm", "sigma_mm", "n", "confidence")}})
        self.emit({"type": "width", **jsonable(est)})

    # control (1 Hz) -------------------------------------------------------------------------------------------------
    def gate_open(self, now=None):
        """LEAM admits writes (docs/control.md, Safety): a fresh writer status with normal_authorized, and fresh
        telemetry whose valid, fresh gateway source is in RUN, permits outputs and has the command gate open."""
        now = self.clock() if now is None else now
        status, status_ms = self._status
        tel = self._telemetry
        return (isinstance(status, dict) and status.get("normal_authorized") is True
                and now - status_ms <= self.cfg["status_fresh_ms"]
                and tel is not None and now - tel["recv_ms"] <= self.cfg["telemetry_fresh_ms"]
                and tel["gateway"] == "RUN" and tel["permits_outputs"] is True
                and tel["command_gate_closed"] is not True and tel["gateway_fresh"])

    def tick(self):
        """One control step; the decision is emitted every step and stored on change and every 10 s."""
        now = self.clock()
        snapshot = getattr(self.control, "snapshot", None)
        view = snapshot() if callable(snapshot) else self.control.public()
        auto = view["auto"] is True
        band = (view.get("band_below_mm"), view.get("band_above_mm"))
        try:
            self._drain()
            self.controller.configure(view.get("settings"))
            self.controller.tune(view.get("levers"))
            out = self._step(now, view["target_mm"], auto, view.get("target_stamp"), band)
            self._pinned = self.controller.pinned(now)
        except Exception as e:  # noqa: BLE001 - nothing is sent on a failed step, and the operator sees why
            self._fail("control step", e)
            out = {**Decision(now, "auto" if auto else "advise", None,
                              f"alarm: control step failed ({type(e).__name__})", None, None, [], True).to_dict(),
                   "dry": auto and not send_enabled(), "sent": None}
        self._record(jsonable(out), now)
        try:
            self._trace_step(now, view, band, out)
        except Exception as e:  # noqa: BLE001 - the trace is for display; it must never stop the loop
            self._fail("trace", e)
        return out

    def _trace_step(self, now, view, band, out):
        with self._est_lock:
            est = self.display.estimate(now)
        setpoints, measured, context = self._inputs(now)
        below, above = self.controller.band_mm(band)
        sigma = est["sigma_mm"] if est else None
        judged_sigma = (self._judged or {}).get("sigma_mm")
        error, target = out.get("error_mm"), view["target_mm"]
        row = {"t_ms": now, "mm": est["mm"] if est else None, "sigma_mm": sigma,
               # what the controller judged: only beads laid since the last change, so it can differ from mm
               "judged_mm": target - error if is_number(error) and is_number(target) else None,
               "confidence": est["confidence"] if est else None, "n": est["n"] if est else None,
               "at_ceiling": bool(est and est.get("at_ceiling")),
               # the noise band Auto holds within: from the controller's own (longer) estimate
               "noise_mm": self.controller.cfg["sigma_factor"] * judged_sigma if is_number(judged_sigma) else None,
               "target_mm": target, "band_below_mm": below, "band_above_mm": above,
               "min_width_mm": self.controller.cfg.get("min_width_mm"),
               "auto": view["auto"] is True, "gate_open": self.gate_open(now),
               "flow": setpoints.get("flow_percent"), "speed": setpoints.get("speed_override_percent"),
               "nozzle_target": setpoints.get("nozzle_temperature_c"), "nozzle_c": measured.get("nozzle_c"),
               "extruding": measured.get("extruding"), "layer": context.get("layer"),
               "action": out.get("action"), "then": out.get("then"), "sent": out.get("sent"), "hold": out.get("hold"),
               "floor": out.get("floor"), "dry": out.get("dry"), "error_mm": out.get("error_mm"),
               "predicted_mm": out.get("predicted_mm"), "reason": (out.get("reason") or "")[:TRACE_REASON_CHARS],
               "candidates": [{k: c.get(k) for k in ("operation", "value", "from", "predicted_change_mm", "score")}
                              for c in out.get("candidates") or []],
               "model": self.controller.sensitivity(setpoints, measured, context) if self._can_act else None}
        row = jsonable(row)
        with self._trace_lock:
            self._trace.append(row)
        self.emit({"type": "trace", **row})

    def _drain(self):
        while self._inbox:
            kind, *args = self._inbox.popleft()
            if kind == "result":
                self._on_result(*args)
            else:
                op, value, at = args
                self.controller.note_external(op, value, at)

    def _step(self, now, target, auto, stamp=None, band=None):
        mode = "auto" if auto else "advise"
        problem = self.target_problem(target, stamp)
        walls = None  # the overrides below keep the bead's own alarm in front
        if self.missing:
            d = Decision(now, mode, None, f"no model: {', '.join(self.missing)}", None, None, [], True)
        elif self.mismatch:
            d = Decision(now, mode, None, "alarm: camera model fitted on a different geometry/width setup; refit",
                         None, None, [], True)
        else:
            with self._est_lock:
                est = self._judged = self.estimator.estimate(now, since_ms=self.controller.estimate_since())
            setpoints, measured, context = self._inputs(now)
            d = self.controller.step(now, est, None if problem else target, setpoints, measured, context,
                                     self.gate_open(now), auto, band)
            walls = self.controller.walls_alarm(est)
            if problem:
                d = Decision(now, mode, None, lead_with(walls, problem), None, None, [], True)
        alarm = self._clock_alarm(self.clock_offset_ms())
        if alarm:
            d = Decision(now, mode, None, lead_with(walls, alarm), d.error_mm, d.predicted_mm, d.candidates, True)
        # dry: what Auto would do, nothing published; sent: whether this decision's action went out (None: not tried)
        out = {**d.to_dict(), "dry": auto and not send_enabled(), "sent": None}
        if auto and d.action is not None and not d.hold:
            if out["dry"]:
                self._note_unsent(d.action, now)
            else:
                why = self._send(d, now)
                out["sent"] = why is None
                if why:  # the planned action stays in `action`; the dashboard must not show it as taken
                    out.update(hold=True, reason=lead_with(walls, f"not sent: {why}"))
                    self._note_unsent(d.action, now)
        return out

    def _note_unsent(self, action, now):
        fn = getattr(self.controller, "note_unsent", None)
        if callable(fn):
            fn(action.get("operation"), action.get("value"), now)

    def _inputs(self, now):
        tel = self._telemetry
        if tel is None or now - tel["recv_ms"] > self.cfg["telemetry_fresh_ms"]:
            return {}, {"nozzle_c": None, "extruding": False}, {"layer": None}
        z = self._z_extruding
        layer = None if z is None else math.floor((z - self.cfg["first_layer_mm"]) / self.cfg["layer_mm"] + 0.5) + 1
        context = {"layer": layer, "z_mm": z, "feed_mm_s": tel["feed_mm_s"], "layer_h_mm": self.cfg["layer_mm"],
                   "line_w_mm": self.cfg["line_width_mm"]}
        return dict(tel["setpoints"]), {"nozzle_c": tel["nozzle_c"], "extruding": tel["extruding"]}, context

    def _refusal(self, decision, now):
        """Why this action must not go out now, re-checked at the last moment; None when it may."""
        action = decision.action or {}
        op, value = action.get("operation"), action.get("value")
        lever = self.controller.levers.get(op) if op in control_config.LEVERS else None
        if self._stop.is_set():
            return "app stopping"
        if self.control.public()["auto"] is not True:
            return "Auto is off"
        if not send_enabled():
            return "sending is disabled"
        if decision.hold or lever is None or not is_number(value):
            return f"not an allowed action: {action}"
        tuned = (self.control.public().get("levers") or {}).get(op) or {}  # the operator's, not the controller's word
        if tuned.get("enabled") is False:
            return f"{op} is switched off for Auto"
        lo, hi = lever["bounds"]
        if not lo <= value <= hi:
            return f"{op} {value} outside {lo}-{hi}"
        current = self._inputs(now)[0].get(op)
        if not is_number(current):
            return f"no fresh readback for {op}"
        step = min(lever["max_step"], tuned["max_step"]) if is_number(tuned.get("max_step")) else lever["max_step"]
        step *= self.controller.cfg["floor_step_factor"] if _floor_widening(decision, current) else 1
        if abs(value - current) > step + 1e-9:
            return f"{op} {value} is more than {step:g} from the readback {current:g}"
        if self._last_sent_ms is not None and now - self._last_sent_ms < self.controller.cfg["min_spacing_ms"]:
            return "less than the minimum spacing since the last command"
        if not self.gate_open(self.clock()):
            return "gate closed"
        return None

    def _send(self, decision, now):
        why = self._refusal(decision, now)
        if why:
            self.stats["send_refused"] += 1
            return why
        op, value = decision.action["operation"], decision.action["value"]
        try:
            request = self.bridge.make_request(op, value)
        except ValueError as e:
            self.bridge.log_refused({"operation": op, "value": value}, str(e))
            self.stats["send_refused"] += 1
            return str(e)
        rid = request["request_id"]
        try:
            result = self.bridge.send_request(request)
        except Exception as e:  # noqa: BLE001 - whether it left is decided below
            self._fail("send", e)
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not (isinstance(result, dict) and result.get("ok")):
            if not self._sent_by_this_app(rid, if_unsure=True):
                self.stats["send_failed"] += 1
                return (result if isinstance(result, dict) else {}).get("error") or "not published"
            self.stats["send_uncertain"] += 1  # it reached the publish: counted as sent, so it never goes out twice
        self.controller.note_sent(op, value, rid, now)
        self._commands[rid] = {"operation": op, "value": value}
        if len(self._commands) > 200:
            self._commands.pop(next(iter(self._commands)))
        self._last_sent_ms = now
        self.stats["sent"] += 1
        self._store("insert_commands", {"t_ms": now, "operation": op, "value": value, "request_id": rid,
                                        "state": "sent"})
        return None

    def _record(self, out, now):
        self._last_decision = out
        key = json.dumps([out.get(k) for k in ("mode", "dry", "action", "reason", "hold")])
        if key != self._stored[0] or now - self._stored[1] >= self.cfg["decision_store_ms"]:
            self._stored = (key, now)
            self._store("insert_decisions", {
                "t_ms": now, "mode": out["mode"], "action_json": out["action"], "reason": (out["reason"] or "")[:1000],
                "error_mm": out["error_mm"], "predicted_mm": out["predicted_mm"], "hold": out["hold"],
                "dry": out.get("dry", False)})
        self.emit({"type": "decision", **out})

    # failures -------------------------------------------------------------------------------------------------------
    def _store(self, method, row):
        if self.store is None:
            return
        try:
            getattr(self.store, method)(row)
        except Exception as e:  # noqa: BLE001 - a logging failure must never stop the loop or the worker
            self._fail("store", e)

    def _fail(self, where, error):
        self.errors[where] = self.errors.get(where, 0) + 1
        text = f"{type(error).__name__}: {error}"
        if self._last_error.get(where) != text:
            self._last_error[where] = text
            log.error("pipeline %s failed: %s", where, text, exc_info=error)
