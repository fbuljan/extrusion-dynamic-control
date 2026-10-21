"""MQTT side of the dashboard: reads every printer topic, tracks per-topic stats, sends extrusion requests."""
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt

READ_TOPIC = "demex/demo/mini/#"
REQUEST_TOPIC = "demex/hackathon/mini/extrusion/request"
WRITER_TOPICS = ["demex/hackathon/mini/extrusion/result", "demex/hackathon/mini/extrusion/status"]
# LEAM accepts one request per second across all senders; ours (manual and Auto alike) keep 2 s of headroom.
MIN_REQUEST_INTERVAL_S = 2.0
HISTORY_POINTS = 600
STALE_SOURCE_MS = 3000
SETTING_KEYS = ["host", "port", "reader_user", "reader_password", "writer_user", "writer_password", "controller_id"]
# LEAM command guide (20 Oct, request schema 0.2.0): the five writable operations and their approved ranges.
OPERATIONS = {
    "flow_percent": (0, 200),
    "pressure_advance_seconds": (0, 1),
    "nozzle_temperature_c": (190, 250),
    "speed_override_percent": (10, 200),
    "fan_speed_percent": (0, 100),
}
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / ".data"))
log = logging.getLogger(__name__)


def now_ms():
    return int(time.time() * 1000)


def dig(d, *path):
    for key in path:
        if isinstance(d, list):
            d = d[key] if isinstance(key, int) and len(d) > key else None
        elif isinstance(d, dict):
            d = d.get(key)
        else:
            return None
    return d


def summarize(payload):
    """Payload preview for the topics view; images are replaced by their size."""
    if isinstance(payload, dict):
        return {k: (f"<{len(v)} chars base64>" if k.endswith("base64") and isinstance(v, str) else summarize(v))
                for k, v in payload.items()}
    if isinstance(payload, list):
        return [summarize(v) for v in payload[:20]]
    return payload


def source(s, name):
    """A telemetry source's payload, or {} when the source is marked invalid (guide: honour source validity)."""
    src = s.get(name) or {}
    return (src.get("payload") or {}) if src.get("valid") is True else {}


def heater(duet, name, published_ms):
    # Heater readings refresh only every ~1.4 s and stay valid=true while aging, so also drop old ones. Age is judged
    # against the message's own publish time (LEAM's clock), so a skewed clock on our side blanks nothing.
    h = dig(duet, "temperatures", name) or {}
    ts = h.get("source_utc_ns")
    if h.get("valid") is not True or not isinstance(ts, (int, float)) or not isinstance(published_ms, (int, float)) \
            or published_ms - ts / 1e6 > STALE_SOURCE_MS:
        return None
    return {"current": h.get("current_c"), "target": h.get("active_target_c"), "state": h.get("state")}


def telemetry_metrics(msg, recv_ms):
    s = msg.get("sources", {})
    motion, duet, process, gateway = (source(s, k) for k in ("motion", "duet_readback", "process", "gateway"))
    melt = s.get("melt", {})
    substrate = dig(process, "measurement", "substrate") or {}
    cooling = dig(process, "pid_state", "cooling") or {}
    melt_valid = melt.get("valid") is True and dig(melt, "payload", "valid") is True
    fan = lambda i: (v * 100 if isinstance(v := dig(duet, "fans", i, "actualValue"), (int, float)) else None)
    published = msg.get("published_utc_ms")
    return {
        "type": "telemetry", "t": recv_ms,
        "nozzle": heater(duet, "nozzle", published),
        "bed": heater(duet, "bed", published),
        "melt": dig(melt, "payload", "temperature_c") if melt_valid else None,
        "melt_reason": dig(melt, "payload", "reason"),
        "substrate": substrate.get("temperature_c") if substrate.get("valid") else None,
        "flow_pct": duet.get("extrusion_percent") if duet.get("extrusion_valid") is True else None,
        "speed_override": motion.get("speed_override_percent"),
        "extrusion_speed": motion.get("extrusion_speed"),
        "vel": motion.get("velocity_magnitude"),
        "extruding": motion.get("extrusion_active"),
        "moving": motion.get("is_moving"),
        "pos": motion.get("position_mm"),
        "program": motion.get("active_program_name"),
        "cooling": cooling.get("current_output"),
        "cooling_enabled": cooling.get("enabled"),
        "fan0": fan(0), "fan1": fan(1),
        "gateway": gateway.get("state"),
        "permits_outputs": gateway.get("permits_outputs"),
        "cameras": msg.get("cameras"),
    }


class Bridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.listeners = []  # callables fed from the MQTT thread
        self.hooks = []  # hook(topic, payload, recv_ms) for every parsed message, from the MQTT thread
        self.errors_seen = {}  # what failed -> last error text: something failing on every message logs once
        self.send_lock = threading.Lock()  # the control loop and manual sends share one rate limit and sequence
        self.settings = self._load_settings()
        self.reader = self.writer = None
        self.status = {"reader": "disconnected", "writer": "not configured"}
        self.topics = {}
        self.frames = {}
        self.history = deque(maxlen=HISTORY_POINTS)
        self.last_history_ms = 0
        self.last_telemetry_emit_ms = 0
        self.writer_log = deque(maxlen=50)
        self.writer_status = None
        self.status_rx = (None, 0.0)  # (latest writer status, time.monotonic() at receipt)
        self.last_request_s = 0.0
        self.last_sequence = 0
        self.sent_ids = deque(maxlen=200)

    # settings -------------------------------------------------------------
    def _load_settings(self):
        settings = {
            "host": os.environ.get("MQTT_HOST", ""), "port": int(os.environ.get("MQTT_PORT", "1883")),
            "reader_user": os.environ.get("MQTT_USER", ""), "reader_password": os.environ.get("MQTT_PASSWORD", ""),
            "writer_user": os.environ.get("MQTT_WRITER_USER", ""),
            "writer_password": os.environ.get("MQTT_WRITER_PASSWORD", ""),
            "controller_id": os.environ.get("MQTT_CONTROLLER_ID", ""),
        }
        path = DATA_DIR / "settings.json"
        if path.exists():
            settings.update({k: v for k, v in json.loads(path.read_text()).items() if k in SETTING_KEYS})
        return settings

    def public_settings(self):
        return {k: (bool(v) if k.endswith("password") else v) for k, v in self.settings.items()}

    def update_settings(self, new):
        for k in SETTING_KEYS:
            # An empty password field means "keep the stored one".
            if k in new and not (k.endswith("password") and new[k] in ("", None)):
                self.settings[k] = int(new[k]) if k == "port" else new[k]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "settings.json").write_text(json.dumps(self.settings))
        self.connect()

    # connections ----------------------------------------------------------
    def connect(self):
        for client in (self.reader, self.writer):
            if client:
                client.loop_stop()
                client.disconnect()
        self.reader = self.writer = None
        cfg = self.settings
        if not cfg["host"]:
            self._set_status("reader", "no broker configured")
            return
        self.reader = self._client("monitor-r", cfg["reader_user"], cfg["reader_password"], "reader", [READ_TOPIC])
        if cfg["writer_user"] and cfg["writer_password"]:
            self.writer = self._client("monitor-w", cfg["writer_user"], cfg["writer_password"], "writer", WRITER_TOPICS)
        else:
            self._set_status("writer", "not configured")

    def _client(self, prefix, user, password, role, topics):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
                             clean_session=True, protocol=mqtt.MQTTv311)
        if user:
            client.username_pw_set(user, password)

        def on_connect(c, userdata, flags, rc, props):
            self._set_status(role, "connected" if not rc.is_failure else f"refused: {rc}")
            if not rc.is_failure:
                for t in topics:
                    c.subscribe(t, qos=0)

        def on_disconnect(c, userdata, flags, rc, props):
            # A refused login is followed by a generic "Unspecified error" disconnect; keep the refusal visible.
            if not self.status[role].startswith("refused"):
                self._set_status(role, f"disconnected: {rc}")

        client.on_connect, client.on_disconnect = on_connect, on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=30)
        self._set_status(role, "connecting")
        client.connect_async(self.settings["host"], int(self.settings["port"]), keepalive=30)
        client.loop_start()
        return client

    def _set_status(self, role, state):
        self.status[role] = state
        self._emit({"type": "status", **self.status})

    # incoming -------------------------------------------------------------
    def _on_message(self, client, userdata, msg):
        # Nothing may escape: paho re-raises callback exceptions, and that stops its network thread for good.
        try:
            if msg.retain:
                return
            topic, t = msg.topic, now_ms()
            try:
                payload = json.loads(msg.payload)
            except ValueError:
                payload = msg.payload[:200].decode(errors="replace")
        except Exception as e:  # noqa: BLE001 - e.g. RecursionError on absurd nesting, a topic that isn't UTF-8
            self._log_once(("message",), e, "unreadable MQTT message")
            return
        try:
            self._handle(topic, payload, t, len(msg.payload))
        except Exception as e:  # noqa: BLE001 - a malformed message is skipped, the next one still counts
            self._log_once(("message", topic), e, f"handling a message on {topic} failed")
        self._run_hooks(topic, payload, t)

    def _handle(self, topic, payload, t, size):
        with self.lock:
            st = self.topics.setdefault(topic, {"count": 0, "recent": deque(maxlen=600), "last_ms": 0, "last": None})
            st["count"] += 1
            st["recent"].append(t)
            st["last_ms"] = t
            st["bytes"] = size
            st["last"] = payload

        if topic.endswith("/telemetry") and isinstance(payload, dict):
            metrics = telemetry_metrics(payload, t)
            if t - self.last_history_ms >= 500:
                self.history.append(metrics)
                self.last_history_ms = t
            if t - self.last_telemetry_emit_ms >= 200:
                self.last_telemetry_emit_ms = t
                self._emit(metrics)
        elif "/preview/" in topic and isinstance(payload, dict) and payload.get("jpeg_base64") \
                and payload.get("camera_kind") == "ir":  # the app shows IR only; any other preview is just counted
            frame = {"type": "frame", "recv_ms": t, **{k: payload.get(k) for k in (
                "camera_kind", "camera_index", "role", "jpeg_base64", "published_utc_ms", "expires_utc_ms",
                "source_age_ms", "sequence", "session_id", "selection_revision")}}
            self.frames["ir"] = frame
            self._emit(frame)
        elif topic.endswith("/extrusion/status"):
            self.writer_status = payload
            self.status_rx = (payload, time.monotonic())
            self._emit({"type": "writer_status", "t": t, "payload": payload})
        elif topic.endswith("/extrusion/result"):
            # Every sender shares this topic; mark which results answer our own requests.
            ours = isinstance(payload, dict) and payload.get("request_id") in self.sent_ids
            entry = {"t": t, "kind": "result", "payload": payload, "ours": ours}
            self.writer_log.appendleft(entry)
            self._emit({"type": "writer_log", **entry})

    def _run_hooks(self, topic, payload, recv_ms):
        for hook in list(self.hooks):
            try:
                hook(topic, payload, recv_ms)
            except Exception as e:  # noqa: BLE001 - one broken consumer must not starve the others
                self._log_once(("hook", hook), e, f"message hook {hook!r} failed on {topic}")

    def _log_once(self, key, error, what):
        text = f"{type(error).__name__}: {error}"
        if self.errors_seen.get(key) != text:
            if len(self.errors_seen) > 100:
                self.errors_seen.clear()
            self.errors_seen[key] = text
            log.error("%s", what, exc_info=error)

    def topics_summary(self):
        t = now_ms()
        with self.lock:
            return {topic: {"count": st["count"], "bytes": st.get("bytes"), "age_ms": t - st["last_ms"],
                            "rate": round(sum(1 for x in st["recent"] if t - x <= 10_000) / 10, 2),
                            "last": summarize(st["last"])}
                    for topic, st in sorted(self.topics.items())}

    # outgoing -------------------------------------------------------------
    def make_request(self, operation, value):
        """A 0.2.0 request built like the reference helper in LEAM's command guide; raises ValueError if not allowed."""
        status, received = self.status_rx
        if not status:
            raise ValueError("no writer status received yet")
        if status.get("schema_version") != "0.1.0":
            raise ValueError(f"unsupported status version {status.get('schema_version')!r}")
        controller = self.settings["controller_id"]
        if not controller or status.get("controller_id") != controller:
            raise ValueError("status does not name our configured controller ID (see Settings)")
        if status.get("normal_authorized") is not True:
            raise ValueError("gate closed: the operator must put native DEMEX in RUN")
        published, deadline = status.get("published_utc_ms"), status.get("expires_utc_ms")
        age_ms = (time.monotonic() - received) * 1000
        if not (isinstance(published, (int, float)) and isinstance(deadline, (int, float))
                and 0 < deadline - published <= 2000 and 0 <= age_ms < min(1500, deadline - published)):
            raise ValueError("no fresh writer status, try again in a moment")
        low, high = OPERATIONS[operation]
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f"{operation} must be within {low}–{high}")
        return {
            "schema_version": "0.2.0",
            "gateway_session_id": status["gateway_session_id"],
            "controller_id": controller,
            "request_id": str(uuid.uuid4()),
            "sequence": max(status.get("highest_admitted_sequence") or 0, self.last_sequence) + 1,
            "issued_utc_ms": published,
            "expires_utc_ms": min(published + 1500, deadline),
            "extruder_index": 0,
            "correction": {"operation": operation, "value": value},
        }

    def log_refused(self, what, error):
        """Record a command the app refused to send, so it shows in every open command log."""
        entry = {"t": now_ms(), "kind": "refused", "payload": {"request": what, "error": error}}
        self.writer_log.appendleft(entry)
        self._emit({"type": "writer_log", **entry})
        return {"ok": False, "error": error}

    def send_request(self, request):
        what = request.get("correction") if isinstance(request, dict) and request.get("correction") else "raw"
        sequence = request.get("sequence") if isinstance(request, dict) else None
        with self.send_lock:
            if not self.writer or self.status.get("writer") != "connected":
                return self.log_refused(what, f"writer not connected ({self.status.get('writer')})")
            wait = MIN_REQUEST_INTERVAL_S - (time.time() - self.last_request_s)
            if wait > 0:
                return self.log_refused(what, f"rate limit: wait {wait:.1f}s")
            if isinstance(sequence, int) and sequence <= self.last_sequence:
                return self.log_refused(what, f"sequence {sequence} already used; build a new request")
            self.last_request_s = time.time()
            info = self.writer.publish(REQUEST_TOPIC, json.dumps(request), qos=0)
            if isinstance(request, dict):
                if isinstance(sequence, int):
                    self.last_sequence = sequence
                self.sent_ids.append(request.get("request_id"))
        entry = {"t": now_ms(), "kind": "request", "payload": request, "published": info.rc == mqtt.MQTT_ERR_SUCCESS}
        self.writer_log.appendleft(entry)
        self._emit({"type": "writer_log", **entry})
        return {"ok": entry["published"], "request": request}

    # fan-out to websockets --------------------------------------------------
    def _emit(self, message):
        for listener in list(self.listeners):
            try:
                listener(message)
            except Exception as e:  # noqa: BLE001 - a dead dashboard socket must not break a send or the MQTT thread
                self._log_once(("listener", id(listener)), e, "a dashboard listener failed")

    def snapshot(self):
        t = now_ms()
        frames = {kind: {**f, "age_ms": t - f["recv_ms"]} for kind, f in self.frames.items()}
        return {"type": "snapshot", "server_ms": t, "status": self.status, "settings": self.public_settings(),
                "history": list(self.history), "frames": frames, "writer_log": list(self.writer_log),
                "writer_status": self.writer_status, "request_topic": REQUEST_TOPIC}
