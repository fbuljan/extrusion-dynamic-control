"""Fake LEAM writer gateway for a LOCAL broker: publishes the status, validates 0.2.0 requests, publishes results.

It does not publish telemetry (a replay does). Refuses any non-loopback host so it can never speak on the event
broker, and rate-limits to 1 request/s by default.
Runtime control: publish {"authorized": bool, "paused": bool, "session_id": str} to the control topic.

    python -m tools.sim.fake_gateway --controller-id demo --log requests.jsonl
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import threading
import time
import uuid

import paho.mqtt.client as mqtt

from tools.sim.plant import stored_value

DEFAULT_BASE_TOPIC = "demex/hackathon/mini/extrusion"
DEFAULT_CONTROL_TOPIC = "fake/gateway/control"
MAX_VALIDITY_MS = 1500  # the guide's helper: expires = min(published + 1500, status expiry)
KEYS = {"schema_version", "gateway_session_id", "controller_id", "request_id", "sequence", "issued_utc_ms",
        "expires_utc_ms", "extruder_index", "correction"}
BOUNDS = {"flow_percent": (0, 200), "pressure_advance_seconds": (0, 1), "nozzle_temperature_c": (190, 250),
          "speed_override_percent": (10, 200), "fan_speed_percent": (0, 100)}


def now_ms() -> int:
    return int(time.time() * 1000)


def require_loopback(host: str) -> None:
    """Raise ValueError unless the host (every address it resolves to) is loopback: never the event broker."""
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in socket.getaddrinfo(host, None)}
        except (socket.gaierror, UnicodeError, ValueError) as error:
            raise ValueError(f"cannot verify that {host!r} is a loopback address: {error}") from error
    if not all(address.is_loopback for address in addresses):
        raise ValueError(f"fake gateway only runs against a local broker, not {host!r}")


def validate(request, controller_id: str, session_id: str, highest_sequence: int, at_ms: int) -> list[str]:
    """Reasons the request would be refused; empty means admissible."""
    if not isinstance(request, dict):
        return ["malformed"]
    problems = []
    if set(request) != KEYS:
        problems.append(f"keys {sorted(set(request) ^ KEYS)}")
    if request.get("schema_version") != "0.2.0":
        problems.append("schema_version")
    if request.get("controller_id") != controller_id:
        problems.append("controller_id")
    if request.get("gateway_session_id") != session_id:
        problems.append("session")
    sequence = request.get("sequence")
    if type(sequence) is not int or sequence <= highest_sequence:
        problems.append(f"sequence {sequence} <= {highest_sequence}")
    issued, expires = request.get("issued_utc_ms"), request.get("expires_utc_ms")
    if type(issued) is not int:
        problems.append("issued_utc_ms")
    if type(expires) is not int or expires < at_ms:
        problems.append("expired")
    elif type(issued) is int and not 0 < expires - issued <= MAX_VALIDITY_MS:
        problems.append("validity window")
    if type(request.get("extruder_index")) is not int or request["extruder_index"] != 0:
        problems.append("extruder_index")
    correction = request.get("correction")
    if not isinstance(correction, dict) or set(correction) != {"operation", "value"} \
            or correction.get("operation") not in BOUNDS:
        problems.append("correction shape")
    else:
        low, high = BOUNDS[correction["operation"]]
        value = correction["value"]
        if type(value) not in (int, float) or not low <= value <= high:
            problems.append("bounds")
    return problems


class FakeGateway:
    def __init__(self, host="127.0.0.1", port=1884, controller_id="sim-controller", session_id="sess-test",
                 authorized=True, base_topic=DEFAULT_BASE_TOPIC, control_topic=DEFAULT_CONTROL_TOPIC, log_path=None,
                 min_interval_s=1.0, confirm_delay_s=0.5, status_period_s=0.5, highest_sequence=0,
                 cancel_on_gate_close=False):
        require_loopback(host)
        self.host, self.port = host, port
        self.controller_id, self.session_id, self.authorized, self.paused = controller_id, session_id, authorized, False
        self.request_topic, self.result_topic, self.status_topic = (f"{base_topic}/{n}" for n in ("request", "result", "status"))
        self.control_topic = control_topic
        self.log_path, self.min_interval_s, self.confirm_delay_s = log_path, min_interval_s, confirm_delay_s
        self.status_period_s, self.cancel_on_gate_close = status_period_s, cancel_on_gate_close
        self._timers: list[threading.Timer] = []
        self.highest_sequence = highest_sequence
        self.entries: list[dict] = []
        self._lock = threading.Lock()
        self._last_accept = None
        self._stop = threading.Event()
        self._thread = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"fake-gateway-{uuid.uuid4().hex[:8]}",
                                  protocol=mqtt.MQTTv311)
        self.client.on_message = self._on_message
        self._subscribed = threading.Event()
        self.client.on_connect = lambda c, u, f, rc, p=None: c.subscribe(
            [(self.request_topic, 0), (self.control_topic, 0)])
        self.client.on_subscribe = lambda c, u, mid, rcs, p=None: self._subscribed.set()

    def start(self) -> "FakeGateway":
        require_loopback(self.host)
        self.client.connect(self.host, self.port)
        self.client.loop_start()
        if not self._subscribed.wait(5):
            self.client.loop_stop()
            raise TimeoutError(f"no subscription on {self.host}:{self.port}")
        self._thread = threading.Thread(target=self._publish_status, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()
            timer.join(timeout=1)
        if self._thread:
            self._thread.join(timeout=2)
        self.client.disconnect()
        self.client.loop_stop()

    def _publish_status(self) -> None:
        while not self._stop.is_set():
            if not self.paused:
                t = now_ms()
                with self._lock:
                    status = {"schema_version": "0.1.0", "controller_id": self.controller_id,
                              "gateway_session_id": self.session_id, "highest_admitted_sequence": self.highest_sequence,
                              "normal_authorized": self.authorized, "published_utc_ms": t, "expires_utc_ms": t + 1500,
                              "maximum_update_hz": 1}
                self.client.publish(self.status_topic, json.dumps(status))
            self._stop.wait(self.status_period_s)

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic == self.control_topic:
            self._control(msg.payload)
        elif msg.topic == self.request_topic:
            self._handle(msg.payload)

    def _control(self, payload) -> None:
        try:
            update = json.loads(payload)
        except ValueError:
            return
        if not isinstance(update, dict):
            return
        with self._lock:
            self.authorized = bool(update.get("authorized", self.authorized))
            self.paused = bool(update.get("paused", self.paused))
            self.session_id = str(update.get("session_id", self.session_id))

    def _handle(self, payload) -> None:
        try:
            request = json.loads(payload)
        except ValueError:
            request = None
        with self._lock:
            problems = validate(request, self.controller_id, self.session_id, self.highest_sequence, now_ms())
            if not problems and not self.authorized:
                problems.append("gate_closed")
            mono = time.monotonic()
            if not problems and self.min_interval_s > 0 and self._last_accept is not None \
                    and mono - self._last_accept < self.min_interval_s:
                problems.append("rate_limited")
            if not problems:
                self.highest_sequence = request["sequence"]
                self._last_accept = mono
            session = self.session_id
        base = {"schema_version": "0.1.0", "gateway_session_id": session, "controller_id": self.controller_id,
                "request_id": request.get("request_id") if isinstance(request, dict) else None,
                "sequence": request.get("sequence") if isinstance(request, dict) else None}
        self._record(request, payload, "rejected" if problems else "accepted", "; ".join(problems))
        if problems:
            self.client.publish(self.result_topic, json.dumps({**base, "state": "rejected", "reason": "; ".join(problems)}))
            return
        correction = request["correction"]
        confirmed = {**base, "state": "setting_confirmed", "observation": {
            "observed_value": stored_value(correction["operation"], correction["value"]), "setting_confirmed": True}}
        if self.confirm_delay_s > 0:
            self.client.publish(self.result_topic, json.dumps({**base, "state": "accepted"}))
            timer = threading.Timer(self.confirm_delay_s, self._confirm, (base, confirmed))
            timer.daemon = True
            with self._lock:
                self._timers = [t for t in self._timers if t.is_alive()] + [timer]
            timer.start()
        else:
            self.client.publish(self.result_topic, json.dumps(confirmed))

    def _confirm(self, base, confirmed) -> None:
        if self.cancel_on_gate_close and not self.authorized:
            confirmed = {**base, "state": "rejected", "reason": "gate_closed"}
        self.client.publish(self.result_topic, json.dumps(confirmed))

    def _record(self, request, payload, state, reason) -> None:
        entry = {"recv_ms": now_ms(), "request": request if request is not None else payload.decode("utf-8", "replace"),
                 "state": state, "reason": reason}
        with self._lock:
            self.entries.append(entry)
            if self.log_path:
                with open(self.log_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
        print("REQUEST", json.dumps(entry["request"]), "->", entry["state"], entry["reason"], flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1884)
    ap.add_argument("--controller-id", default=os.environ.get("MQTT_CONTROLLER_ID", "sim-controller"))
    ap.add_argument("--session-id", default="sess-test")
    ap.add_argument("--gate-closed", action="store_true", help="start with normal_authorized false")
    ap.add_argument("--log", help="append every request and its verdict to this JSONL file")
    ap.add_argument("--base-topic", default=DEFAULT_BASE_TOPIC)
    ap.add_argument("--control-topic", default=DEFAULT_CONTROL_TOPIC)
    ap.add_argument("--min-interval-s", type=float, default=1.0,
                    help="reject requests closer than this (LEAM: 1.0; 0 = off)")
    ap.add_argument("--cancel-on-gate-close", action="store_true",
                    help="reject requests still in flight when the gate closes")
    ap.add_argument("--confirm-delay-s", type=float, default=0.5,
                    help="accepted -> setting_confirmed delay; 0 skips accepted")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    gateway = FakeGateway(args.host, args.port, args.controller_id, args.session_id, not args.gate_closed,
                          args.base_topic, args.control_topic, args.log, args.min_interval_s, args.confirm_delay_s,
                          cancel_on_gate_close=args.cancel_on_gate_close).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        gateway.stop()


if __name__ == "__main__":
    main()
