"""MQTT side of the dashboard: reads every printer topic, tracks per-topic stats, sends extrusion requests."""
import json
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
MIN_REQUEST_INTERVAL_S = 1.0  # organiser limit: one accepted correction per second across flow and PA
HISTORY_POINTS = 600
SETTING_KEYS = ["host", "port", "reader_user", "reader_password", "writer_user", "writer_password"]
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / ".data"))


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


def telemetry_metrics(msg, recv_ms):
    s = msg.get("sources", {})
    motion = dig(s, "motion", "payload") or {}
    duet = dig(s, "duet_readback", "payload") or {}
    melt = s.get("melt", {})
    substrate = dig(s, "process", "payload", "measurement", "substrate") or {}
    gateway = dig(s, "gateway", "payload") or {}
    flow_valid = bool(dig(s, "duet_readback", "valid")) and duet.get("extrusion_valid") is True
    melt_valid = bool(melt.get("valid")) and dig(melt, "payload", "valid") is True
    fan = lambda i: (v * 100 if isinstance(v := dig(duet, "fans", i, "actualValue"), (int, float)) else None)
    return {
        "type": "telemetry", "t": recv_ms,
        "melt": dig(melt, "payload", "temperature_c") if melt_valid else None,
        "melt_reason": dig(melt, "payload", "reason"),
        "substrate": substrate.get("temperature_c") if substrate.get("valid") else None,
        "flow_pct": duet.get("extrusion_percent") if flow_valid else None,
        "speed_override": motion.get("speed_override_percent"),
        "extrusion_speed": motion.get("extrusion_speed"),
        "vel": motion.get("velocity_magnitude"),
        "extruding": motion.get("extrusion_active"),
        "moving": motion.get("is_moving"),
        "pos": motion.get("position_mm"),
        "program": motion.get("active_program_name"),
        "cooling": dig(s, "process", "payload", "pid_state", "cooling", "current_output"),
        "fan0": fan(0), "fan1": fan(1),
        "gateway": gateway.get("state"),
        "permits_outputs": gateway.get("permits_outputs"),
        "cameras": msg.get("cameras"),
    }


class Bridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.listeners = []  # callables fed from the MQTT thread
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
        self.last_request_s = 0.0

    # settings -------------------------------------------------------------
    def _load_settings(self):
        settings = {
            "host": os.environ.get("MQTT_HOST", ""), "port": int(os.environ.get("MQTT_PORT", "1883")),
            "reader_user": os.environ.get("MQTT_USER", ""), "reader_password": os.environ.get("MQTT_PASSWORD", ""),
            "writer_user": os.environ.get("MQTT_WRITER_USER", ""),
            "writer_password": os.environ.get("MQTT_WRITER_PASSWORD", ""),
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
        if msg.retain:
            return
        t = now_ms()
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            payload = msg.payload[:200].decode(errors="replace")
        with self.lock:
            st = self.topics.setdefault(msg.topic, {"count": 0, "recent": deque(maxlen=200), "last_ms": 0, "last": None})
            st["count"] += 1
            st["recent"].append(t)
            st["last_ms"] = t
            st["bytes"] = len(msg.payload)
            st["last"] = payload

        if msg.topic.endswith("/telemetry") and isinstance(payload, dict):
            metrics = telemetry_metrics(payload, t)
            if t - self.last_history_ms >= 500:
                self.history.append(metrics)
                self.last_history_ms = t
            if t - self.last_telemetry_emit_ms >= 200:
                self.last_telemetry_emit_ms = t
                self._emit(metrics)
        elif "/preview/" in msg.topic and isinstance(payload, dict) and payload.get("jpeg_base64"):
            frame = {"type": "frame", "recv_ms": t, **{k: payload.get(k) for k in (
                "camera_kind", "camera_index", "role", "jpeg_base64", "published_utc_ms", "expires_utc_ms",
                "source_age_ms", "sequence")}}
            self.frames[payload.get("camera_kind")] = frame
            self._emit(frame)
        elif msg.topic.endswith("/extrusion/status"):
            self.writer_status = payload
            self._emit({"type": "writer_status", "t": t, "payload": payload})
        elif msg.topic.endswith("/extrusion/result"):
            entry = {"t": t, "kind": "result", "payload": payload}
            self.writer_log.appendleft(entry)
            self._emit({"type": "writer_log", **entry})

    def topics_summary(self):
        t = now_ms()
        with self.lock:
            return {topic: {"count": st["count"], "bytes": st.get("bytes"), "age_ms": t - st["last_ms"],
                            "rate": round(sum(1 for x in st["recent"] if t - x <= 10_000) / 10, 2),
                            "last": summarize(st["last"])}
                    for topic, st in sorted(self.topics.items())}

    # outgoing -------------------------------------------------------------
    def send_request(self, request):
        if not self.writer or self.status.get("writer") != "connected":
            return {"ok": False, "error": f"writer not connected ({self.status.get('writer')})"}
        wait = MIN_REQUEST_INTERVAL_S - (time.time() - self.last_request_s)
        if wait > 0:
            return {"ok": False, "error": f"rate limit: wait {wait:.1f}s"}
        self.last_request_s = time.time()
        info = self.writer.publish(REQUEST_TOPIC, json.dumps(request), qos=0)
        entry = {"t": now_ms(), "kind": "request", "payload": request, "published": info.rc == mqtt.MQTT_ERR_SUCCESS}
        self.writer_log.appendleft(entry)
        self._emit({"type": "writer_log", **entry})
        return {"ok": entry["published"], "request": request}

    # fan-out to websockets --------------------------------------------------
    def _emit(self, message):
        for listener in list(self.listeners):
            listener(message)

    def snapshot(self):
        t = now_ms()
        frames = {kind: {**f, "age_ms": t - f["recv_ms"]} for kind, f in self.frames.items()}
        return {"type": "snapshot", "status": self.status, "settings": self.public_settings(),
                "history": list(self.history), "frames": frames, "writer_log": list(self.writer_log),
                "writer_status": self.writer_status, "request_topic": REQUEST_TOPIC}
