"""Pipeline: bridge hooks -> width samples -> estimates -> decisions -> the send gate, driven without MQTT."""
import base64
import builtins
import heapq
import importlib
import json
import logging
import math
import random
import re
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest
from conftest import CAPTURES, REPO
from fastapi.testclient import TestClient
from PIL import Image

from server import bridge as bridge_module
from server import control_config
from server.bridge import REQUEST_TOPIC, Bridge
from server.control_state import ControlState
from server.controller import Decision
from server.model_stubs import StubCameraModel, StubPrinterModel
from server.pipeline import Pipeline, geometry_version, load_models, width_config_hash
from server.store import Store

TELEMETRY = "demex/demo/mini/telemetry"
IR = "demex/demo/mini/preview/ir/trailing"
STATUS = "demex/hackathon/mini/extrusion/status"
RESULT = "demex/hackathon/mini/extrusion/result"
ALLOWED = set(control_config.LEVERS)
T0 = 1_800_000_000_000
OFFSET_MS = 150  # the simulated LEAM clock runs this far behind ours (incl. delivery)
GROUP = "cam2:180"
CONTROLLER_ID = "ctl-test"
DB_FILES = {"prototype.db", "prototype.db-wal", "prototype.db-shm"}
GOLDEN2 = CAPTURES / "golden2-20261020-1815"
GEOMETRY = REPO / "app" / "models" / "geometry.json"
BROKER_ENV = ["MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_WRITER_USER", "MQTT_WRITER_PASSWORD",
              "MQTT_CONTROLLER_ID"]


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return int(self.t)


# messages as LEAM publishes them -------------------------------------------------------------------------------------
def telemetry(now, flow=100.0, speed=100.0, target_c=220.0, actual_c=220.0, z=7.2, extruding=True, gateway="RUN"):
    ns = (now - OFFSET_MS - 20) * 1_000_000
    x = -20.0 * ((now - T0) / 1000 % 50)
    return {"published_utc_ms": now - OFFSET_MS, "sources": {
        "motion": {"valid": True, "source_utc_ns": ns, "payload": {
            "position_mm": {"x": x, "y": 0.0, "z": z}, "velocity_mm_s": {"x": -20.0, "y": 0.0, "z": 0.0},
            "velocity_magnitude": 20.0, "extrusion_active": extruding, "speed_override_percent": speed}},
        "duet_readback": {"valid": True, "source_utc_ns": ns, "payload": {
            "extrusion_percent": flow, "extrusion_valid": True, "extrusion_source_utc_ns": ns,
            "temperatures": {"nozzle": {"valid": True, "current_c": actual_c, "active_target_c": target_c,
                                        "state": "active", "source_utc_ns": ns}}}},
        "gateway": {"valid": True, "source_utc_ns": ns, "payload": {"state": gateway, "permits_outputs": gateway == "RUN",
                                               "command_gate_closed": gateway != "RUN"}}}}


def writer_status(now, authorized=True):
    return {"schema_version": "0.1.0", "controller_id": CONTROLLER_ID, "gateway_session_id": "gw-1",
            "normal_authorized": authorized, "published_utc_ms": now - OFFSET_MS,
            "expires_utc_ms": now - OFFSET_MS + 1500, "highest_admitted_sequence": 0}


def ir_frame(now, seq, cam=2, jpeg=b"\xff\xd8 not a real frame \xff\xd9"):
    return {"camera_index": cam, "camera_kind": "ir", "role": "trailing", "sequence": seq,
            "source_frame_id": str(seq), "source_utc_ns": (now - 850 - OFFSET_MS) * 1_000_000,
            "jpeg_base64": base64.b64encode(jpeg).decode()}


@dataclass
class Sample:  # the WidthSample fields, so these tests don't depend on the width extractor's module
    t_ms: int
    camera: int
    heading_deg: float | None
    axis_deg: float
    width_px: float | None
    mm_per_px: float | None
    contrast: float
    confidence: float
    reason: str
    recv_ms: int | None = None
    source_frame_id: str | None = None
    group: str = ""
    t_dep_ms: int | None = None


class FakeExtractor:
    """Reads the simulated printer instead of the image: px for the real width deposited 0.5 s before the frame."""

    def __init__(self, width_mm, camera, noise_px=0.02, seed=0):
        self.width_mm, self.camera, self.noise, self.rng = width_mm, camera, noise_px, random.Random(seed)
        self.calls = []

    def process(self, jpeg, frame_meta, motion):
        assert isinstance(jpeg, bytes)
        self.calls.append((dict(frame_meta), motion))
        t = frame_meta["source_utc_ns"] / 1e6
        dep = t - 500
        px = self.camera.ir_px(self.width_mm(dep + OFFSET_MS), GROUP, 220.0, 20.0) + self.rng.gauss(0, self.noise)
        return Sample(int(t), frame_meta["camera_index"], 180.0, 0.0, px, 0.4, 40.0, 0.9, "ok",
                      frame_meta["recv_ms"], frame_meta["source_frame_id"], GROUP, int(dep))


class FakeBridge:
    def __init__(self):
        self.hooks, self.sent_ids, self.sent, self.refused = [], deque(maxlen=200), [], []
        self.make_error, self.gateway, self.probe = None, None, None

    def make_request(self, operation, value):
        if self.make_error:
            raise ValueError(self.make_error)
        return {"request_id": str(uuid.uuid4()), "correction": {"operation": operation, "value": value}}

    def send_request(self, request):
        self.sent.append({"request": request, **(self.probe() if self.probe else {})})
        self.sent_ids.append(request["request_id"])
        if self.gateway:
            self.gateway(request)
        return {"ok": True, "request": request}

    def log_refused(self, what, error):
        self.refused.append((what, error))
        return {"ok": False, "error": error}

    def deliver(self, topic, payload, recv_ms):
        for hook in list(self.hooks):
            hook(topic, payload, recv_ms)


class FakeWriter:
    """paho client stand-in for the real Bridge: records publishes, never touches a network."""

    def __init__(self, on_publish):
        self.published, self.on_publish = [], on_publish

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))
        self.on_publish(json.loads(payload))
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)


def real_bridge(monkeypatch, clock, on_publish):
    """The app's Bridge on the simulated clock, with a fake writer client; hooks run through its real _on_message."""
    for name in BROKER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bridge_module, "now_ms", clock)
    monkeypatch.setattr(bridge_module, "time", SimpleNamespace(time=lambda: clock() / 1000,
                                                               monotonic=lambda: clock() / 1000))
    b = Bridge()
    b.settings["controller_id"] = CONTROLLER_ID
    b.writer = FakeWriter(on_publish)
    b.status["writer"] = "connected"
    b.deliver = lambda topic, payload, recv_ms: b._on_message(None, None, SimpleNamespace(
        topic=topic, payload=json.dumps(payload).encode(), retain=False))
    return b


# a printer, its gateway and the trailing IR camera on one simulated clock -------------------------------------------
class Sim:
    def __init__(self, tmp_path, monkeypatch, target=1.35, auto=False, send=True, bridge="fake", results=None,
                 camera="stub", printer="stub", config=None, extractor="fake", control=None, store=None, gate=None,
                 geometry=None, extractor_cfg=None):
        self.clock = Clock()
        if send:
            monkeypatch.setenv("CONTROL_SEND_ENABLED", "1")
        else:
            monkeypatch.delenv("CONTROL_SEND_ENABLED", raising=False)
        self.flow, self.speed, self.nozzle = 100.0, 100.0, 220.0
        self.flow_history = [(-math.inf, 100.0)]
        self.results = results or {}
        self.gate = gate or (lambda now: (True, "RUN"))
        self.telemetry_on = lambda now: True
        self.queue, self.n_frames, self.decisions, self.requests = [], 0, [], []
        self.camera = StubCameraModel(g=0.0, s=0.0) if camera == "stub" else camera
        self.bridge = FakeBridge() if bridge == "fake" else real_bridge(monkeypatch, self.clock, self.on_request)
        if bridge == "fake":
            self.bridge.gateway = self.on_request
            self.bridge.probe = lambda: {"t": self.clock(), "auto": self.control.auto,
                                         "gate": self.pipeline.gate_open(), "flow": self.flow}
        data = tmp_path / "data"
        self.control = control or ControlState(data / "control.json")
        self.control.update({"auto": auto})
        self.store = store if store is not None else Store(data / "prototype.db", clock_ms=self.clock)
        self.emitted = []
        self.extractor = FakeExtractor(self.width_mm, self.camera) if extractor == "fake" else extractor
        if extractor_cfg is not None:
            self.extractor.cfg = extractor_cfg
        self.pipeline = Pipeline(self.bridge, self.control, self.store, camera_model=self.camera,
                                 printer_model=StubPrinterModel() if printer == "stub" else printer,
                                 controller_config=config, extractor=self.extractor, clock_ms=self.clock,
                                 emit=self.emitted.append, geometry=geometry)
        self.bridge.hooks.append(self.pipeline.on_message)
        if target is not None:  # as the app sets it: stamped with the camera model it was set under
            self.control.update({"target_mm": target}, stamp=self.pipeline.target_stamp("typed"))

    def close(self):
        self.pipeline.stop()
        if hasattr(self.store, "close"):
            self.store.close()

    def width_mm(self, t_local):
        flow = [f for t, f in self.flow_history if t <= t_local][-1]
        return 1.5 * flow / 100

    def on_request(self, request):
        """LEAM's gateway: accepted, then the final state; a confirmed flow lands on the printer 0.6 s after."""
        n, now = len(self.requests) + 1, self.clock()
        self.requests.append((now, request))
        state, rid = self.results.get(n, "setting_confirmed"), request["request_id"]
        self.later(now + 200, RESULT, {"request_id": rid, "state": "accepted"})
        self.later(now + 700, RESULT, {"request_id": rid, "state": state})
        if state == "setting_confirmed":
            c = request["correction"]
            self.later(now + 600, None, (c["operation"], c["value"]))

    def later(self, due, topic, payload):
        heapq.heappush(self.queue, (due, id(payload), topic, payload))

    def apply(self, op, value):
        if op == "flow_percent":
            self.flow = value
            self.flow_history.append((self.clock(), value))
        elif op == "speed_override_percent":
            self.speed = value
        else:
            self.nozzle = value

    def run(self, seconds, auto=None, every_second=None):
        end = self.clock.t + seconds * 1000
        while self.clock.t < end:
            self.clock.t += 100
            now = self.clock()
            while self.queue and self.queue[0][0] <= now:
                _, _, topic, payload = heapq.heappop(self.queue)
                if topic is None:
                    self.apply(*payload)
                else:
                    self.bridge.deliver(topic, payload, now)
            authorized, gateway = self.gate(now)
            if self.telemetry_on(now):
                self.bridge.deliver(TELEMETRY, telemetry(now, self.flow, self.speed, self.nozzle, self.nozzle,
                                                         gateway=gateway), now)
            if (now - T0) % 1000 == 0:
                self.bridge.deliver(STATUS, writer_status(now, authorized), now)
                self.n_frames += 1
                self.bridge.deliver(IR, ir_frame(now, self.n_frames), now)
                self.pipeline.process_pending()
                if auto is not None and auto(now) != self.control.auto:
                    self.control.update({"auto": auto(now)})
                if every_second:
                    every_second(now)
                self.decisions.append(self.pipeline.tick())
        return self

    def sent(self):
        if isinstance(self.bridge, FakeBridge):
            return self.bridge.sent
        return [{"request": r, "t": t} for t, r in self.requests]

    def check_sends(self):
        """Every send: an allowed lever, within the configured bounds and step, >= 2 s after the previous one."""
        cfg = self.pipeline.controller.cfg
        last = None
        for s in self.sent():
            c = s["request"]["correction"]
            assert set(c) == {"operation", "value"} and c["operation"] in ALLOWED
            lever = cfg["levers"][c["operation"]]
            assert lever["bounds"][0] <= c["value"] <= lever["bounds"][1]
            if "flow" in s and c["operation"] == "flow_percent":
                assert abs(c["value"] - s["flow"]) <= lever["max_step"]
            if "auto" in s:
                assert s["auto"] is True and s["gate"] is True
            if last is not None:
                assert s["t"] - last >= cfg["min_spacing_ms"]
            last = s["t"]


@pytest.fixture
def sim(tmp_path, monkeypatch):
    made = []

    def make(**kw):
        made.append(Sim(tmp_path, monkeypatch, **kw))
        return made[-1]
    yield make
    for s in made:
        s.close()


def sends_between(sim, start, end):
    return [s for s in sim.sent() if start <= s["t"] < end]


# advice, dry runs, Auto ---------------------------------------------------------------------------------------------
def test_auto_is_off_at_startup_even_if_a_file_says_otherwise(sim, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "control.json").write_text(
        '{"target_mm": 1.35, "auto": true, "target_stamp": {"camera_model": "stub", "relative": false, "by": "typed"}}')
    s = sim(control=ControlState(tmp_path / "data" / "control.json"), target=None, auto=False).run(60)
    assert s.control.auto is False
    assert {(d["mode"], d["dry"]) for d in s.decisions} == {("advise", False)}
    assert any(d["action"] for d in s.decisions), "advice is still given"
    assert s.sent() == []


def test_advice_mode_never_sends(sim):
    s = sim(auto=False, send=True).run(90)
    assert {(d["mode"], d["dry"]) for d in s.decisions} == {("advise", False)}
    advice = [d for d in s.decisions if d["action"]]
    assert advice and all(d["action"]["operation"] in ALLOWED for d in advice)
    assert s.sent() == [] and s.bridge.refused == []


@pytest.mark.parametrize("value", [None, "0", "true", "yes", " 1"])
def test_auto_without_the_deployment_switch_is_a_dry_run(sim, monkeypatch, value):
    s = sim(auto=True, send=False)
    if value is not None:
        monkeypatch.setenv("CONTROL_SEND_ENABLED", value)
    s.run(90)
    assert {(d["mode"], d["dry"]) for d in s.decisions} == {("auto", True)}
    assert any(d["action"] and not d["hold"] for d in s.decisions)
    assert s.sent() == [] and s.bridge.refused == [] and s.pipeline.stats["send_refused"] == 0
    assert not any("not sent" in d["reason"] for d in s.decisions)
    assert {(r["mode"], r["dry"]) for r in s.store.recent_decisions(1000)} == {("auto", 1)}


@pytest.mark.parametrize("bridge", ["fake", "real"])
def test_auto_sends_valid_spaced_requests_and_converges(sim, bridge):
    s = sim(auto=False, bridge=bridge)
    s.run(20)
    s.run(100, auto=lambda now: True)
    sent = s.sent()
    assert len(sent) >= 2
    s.check_sends()
    assert s.flow < 100 and abs(s.width_mm(s.clock()) - 1.35) <= 0.08
    assert {(d["mode"], d["dry"]) for d in s.decisions[20:]} == {("auto", False)}
    commands = s.store.recent_commands(1000)
    assert {r["state"] for r in commands} >= {"sent", "accepted", "setting_confirmed"}
    assert {r["request_id"] for r in commands} == {x["request"]["request_id"] for x in sent}
    if bridge == "real":
        assert {topic for topic, _ in s.bridge.writer.published} == {REQUEST_TOPIC}
        req = s.bridge.writer.published[0][1]
        assert req["controller_id"] == CONTROLLER_ID and req["schema_version"] == "0.2.0"
        assert all(r["ours"] for r in s.bridge.writer_log if r["kind"] == "result")


def test_auto_off_stops_sending_at_once(sim):
    s = sim(auto=False)
    s.run(20)
    off_at = s.clock() + 40_000
    s.run(100, auto=lambda now: now < off_at)
    assert sends_between(s, T0, off_at) and not sends_between(s, off_at, math.inf)


def test_nothing_is_sent_while_the_gate_is_closed(sim):
    closed = {}

    def gate(now):
        t = (now - T0) / 1000
        if 45 <= t < 75:
            return False, "RUN"  # operator took DEMEX out of RUN for us
        if 75 <= t < 95:
            return True, "SAFE"  # gateway not in RUN
        return True, "RUN"
    s = sim(auto=False, gate=gate)
    s.run(20)
    s.run(130, auto=lambda now: True, every_second=lambda now: closed.setdefault(now, s.pipeline.gate_open(now)))
    assert not sends_between(s, T0 + 45_000, T0 + 95_000)
    assert sends_between(s, T0, T0 + 45_000) or sends_between(s, T0 + 95_000, math.inf)
    assert not closed[T0 + 50_000] and not closed[T0 + 80_000] and closed[T0 + 100_000]
    s.check_sends()


def test_stale_writer_status_closes_the_gate(sim):
    s = sim()
    s.run(5)
    assert s.pipeline.gate_open()
    s.clock.t += 5001
    assert not s.pipeline.gate_open()


def test_failed_requests_are_not_resent_and_two_failures_stop_auto(sim):
    s = sim(auto=False, results={1: "rejected", 2: "dispatch_unknown"})
    s.run(20)
    s.run(120, auto=lambda now: True)
    sent = s.sent()
    assert len(sent) == 2
    assert sent[1]["t"] - (sent[0]["t"] + 700) >= s.pipeline.controller.cfg["explain_wait_ms"]
    assert any(d["reason"].endswith("% rejected; waiting for the readback") for d in s.decisions)
    assert s.decisions[-1]["reason"].startswith("alarm: 2 commands in a row failed")
    assert "(last: flow 95 % dispatch unknown)" in s.decisions[-1]["reason"]
    assert s.flow == 100.0


def test_a_foreign_result_holds_auto(sim):
    s = sim(auto=False)
    s.run(20)
    t_foreign = s.clock() + 5000
    s.later(t_foreign, RESULT, {"request_id": "someone-else", "state": "setting_confirmed",
                                "observation": {"observed_value": 104.0}})
    s.run(60, auto=lambda now: True)
    hold_s = s.pipeline.controller.cfg["foreign_hold_s"]
    assert not sends_between(s, t_foreign, t_foreign + hold_s * 1000)
    assert any("another sender" in d["reason"] for d in s.decisions)
    assert s.pipeline.stats["foreign_results"] == 1


@pytest.mark.parametrize("state", ["accepted", "setting_confirmed", "rejected"])
def test_a_foreign_result_holds_for_30_s_from_its_arrival(sim, state):
    s = sim(auto=False)
    s.run(30)
    t_foreign = s.clock() + 500
    s.later(t_foreign, RESULT, {"request_id": "someone-else", "state": state})
    s.run(40)
    hold_s = s.pipeline.controller.cfg["foreign_hold_s"]
    held = [d["t_ms"] for d in s.decisions if "another sender" in d["reason"]]
    assert held == [t_foreign + 500 + i * 1000 for i in range(hold_s)]
    assert all(d["action"] is None for d in s.decisions if d["t_ms"] in held)


def test_results_reach_the_controller_before_the_step_that_follows(sim):
    s = sim(auto=False)
    s.run(30)
    assert s.decisions[-1]["action"], "warm: advice is being given"
    s.clock.t += 1000
    s.bridge.deliver(RESULT, {"request_id": "someone-else", "state": "setting_confirmed"}, s.clock())
    d = s.pipeline.tick()
    assert d["action"] is None and "another sender" in d["reason"]


def test_a_manual_send_while_auto_is_on_pins_that_lever(sim):
    s = sim(auto=True)
    request = {"request_id": "manual-1", "correction": {"operation": "flow_percent", "value": 110}}
    s.pipeline.note_manual_send(request, {"ok": False, "error": "rate limit"})
    s.pipeline.tick()
    assert s.pipeline.pinned() == {}
    s.bridge.sent_ids.append("manual-1")
    s.pipeline.note_manual_send(request, {"ok": True, "request": request})
    s.bridge.deliver(RESULT, {"request_id": "manual-1", "state": "setting_confirmed"}, s.clock())
    s.pipeline.tick()
    assert s.pipeline.pinned()["flow_percent"]["value"] == 110
    assert s.pipeline.stats["foreign_results"] == 0


def test_a_manual_send_in_advice_mode_does_not_pin(sim):
    s = sim(auto=False)
    request = {"request_id": "manual-2", "correction": {"operation": "flow_percent", "value": 110}}
    s.pipeline.note_manual_send(request, {"ok": True, "request": request})
    s.pipeline.tick()
    assert s.pipeline.pinned() == {}


def test_a_refused_request_is_logged_and_noted_on_the_decision(sim):
    s = sim(auto=False)
    s.bridge.make_error = "gate closed: the operator must put native DEMEX in RUN"
    s.run(20)
    s.run(60, auto=lambda now: True)
    assert s.bridge.sent == [] and s.bridge.refused
    assert all(what["operation"] in ALLOWED for what, _ in s.bridge.refused)
    refused = [d for d in s.decisions if d["sent"] is False]
    assert refused and all(d["reason"] == "not sent: gate closed: the operator must put native DEMEX in RUN"
                           and d["hold"] and d["action"] for d in refused)
    assert not any(d["action"] and not d["hold"] for d in s.decisions if d["mode"] == "auto"), \
        "nothing reaches the dashboard as taken"


def dispatch_unknown_gateway(s):
    def on_request(request):
        now = s.clock()
        s.requests.append((now, request))
        s.later(now + 200, RESULT, {"request_id": request["request_id"], "state": "accepted"})
        s.later(now + 700, RESULT, {"request_id": request["request_id"], "state": "dispatch_unknown"})
    return on_request


@pytest.mark.parametrize("raise_after_publish", [False, True])
def test_a_send_that_raises_after_publishing_is_never_published_again(sim, raise_after_publish):
    s = sim(auto=False)
    s.bridge.gateway = dispatch_unknown_gateway(s)
    real_send = s.bridge.send_request

    def send(request):
        result = real_send(request)
        if raise_after_publish:
            raise RuntimeError("Event loop is closed")
        return result
    s.bridge.send_request = send
    s.run(20)
    s.run(180, auto=lambda now: True)
    sent = s.bridge.sent
    assert len(sent) == 2 and len({x["request"]["request_id"] for x in sent}) == 2
    assert s.decisions[-1]["reason"].startswith("alarm: 2 commands in a row failed")
    states = {(r["request_id"], r["state"]) for r in s.store.recent_commands(100)}
    assert all((x["request"]["request_id"], "dispatch_unknown") in states for x in sent)
    assert s.pipeline.stats["send_uncertain"] == (2 if raise_after_publish else 0)


def test_a_send_that_raises_before_publishing_is_not_counted(sim):
    s = sim(auto=True)
    s.run(3)
    s.pipeline.controller = RogueController(s.pipeline.controller, {"operation": "flow_percent", "value": 95})

    def broken(request):
        raise OSError("writer gone")
    s.bridge.send_request = broken
    d = s.pipeline.tick()
    assert d["reason"].endswith("not sent: OSError: writer gone") and s.pipeline.controller.sent == []
    assert s.pipeline.stats["send_failed"] == 1 and s.pipeline.errors["send"] == 1


def test_sent_marks_published_actions_and_unsent_ones_reach_the_controller(sim, monkeypatch):
    unsent = []
    for send, make_error in ((True, None), (False, None), (True, "no fresh writer status, try again in a moment")):
        s = sim(auto=False, send=send)
        s.bridge.make_error = make_error
        monkeypatch.setattr(s.pipeline.controller, "note_unsent", lambda op, value, now: unsent.append(
            (send, make_error, op, value, now)), raising=False)
        s.run(20)
        s.run(60, auto=lambda now: True)
        acted = [d for d in s.decisions if d["action"]]
        assert acted
        if send and not make_error:
            assert {d["sent"] for d in acted} <= {True, None} and sum(d["sent"] is True for d in acted) == len(s.sent())
            assert not [u for u in unsent if u[:2] == (True, None)]
        elif not send:
            assert all(d["sent"] is None and d["dry"] and not d["hold"] for d in acted)
            assert len([u for u in unsent if not u[0]]) == len(acted)
        else:
            assert all(d["sent"] is False and d["hold"] for d in acted)
            assert [(u[2], u[3], u[4]) for u in unsent if u[1]] == \
                [(d["action"]["operation"], d["action"]["value"], d["t_ms"]) for d in acted]


def test_a_manual_send_within_2_s_of_an_auto_send_is_refused(sim):
    s = sim(auto=False, bridge="real")
    s.run(20)
    tried = {}

    def manual(now):
        if s.requests and now - s.requests[0][0] in (1000, 2000) and now not in tried:
            request = s.bridge.make_request("speed_override_percent", 95)
            tried[now] = s.bridge.send_request(request)
            s.pipeline.note_manual_send(request, tried[now])
    s.run(60, auto=lambda now: True, every_second=manual)
    first = s.requests[0][0]
    assert tried[first + 1000] == {"ok": False, "error": "rate limit: wait 1.0s"}
    assert tried[first + 2000]["ok"] is True
    assert [t - first for t, _ in s.requests[:2]] == [0, 2000]


# the last check before a send, against a controller that misbehaves ------------------------------------------------
class RogueController:
    def __init__(self, real, action, hold=False, floor=False, error_mm=None):
        self.real, self.action, self.hold, self.floor, self.error_mm = real, action, hold, floor, error_mm
        self.levers, self.cfg = real.levers, real.cfg
        self.sent = []

    def estimate_since(self):
        return None

    def step(self, now_ms, estimate, target_mm, setpoints, measured, context, gate_open, auto, band=None):
        return Decision(now_ms, "auto" if auto else "advise", self.action, "rogue", self.error_mm, None, [], self.hold,
                        self.floor)

    def walls_alarm(self, estimate):
        return self.real.walls_alarm(estimate)

    def tune(self, levers):
        self.real.tune(levers)

    def configure(self, settings):
        self.real.configure(settings)

    def sensitivity(self, *args):
        return self.real.sensitivity(*args)

    def band_mm(self, band):
        return self.real.band_mm(band)

    def note_sent(self, *args):
        self.sent.append(args)

    def pinned(self, now_ms):
        return {}


REFUSALS = {"ok": None, "auto_off_mid_step": "Auto is off", "sends_disabled": "sending is disabled", "hold": None,
            "fan": "not an allowed action", "pressure_advance": "not an allowed action", "out_of_bounds": "outside",
            "out_of_bounds_small_step": "outside 70-130", "nan": "not an allowed action", "gate_closed": "gate closed",
            "stopping": "app stopping", "step_down_past_max": "more than 10 from the readback",
            "step_up_past_max": "more than 10 from the readback", "no_flow_readback": "no fresh readback"}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_the_pipeline_refuses_what_the_controller_must_never_ask(sim, monkeypatch, case):
    s = sim(auto=True)
    s.control.update({"settings": {"flow_percent_min": 70, "flow_percent_max": 130}})  # the operator's bounds (D54)
    if case == "out_of_bounds_small_step":
        s.flow = 125.0
    s.run(3)
    action = {"fan": {"operation": "fan_speed_percent", "value": 50},
              "pressure_advance": {"operation": "pressure_advance_seconds", "value": 0.05},
              "out_of_bounds": {"operation": "flow_percent", "value": 140},
              "nan": {"operation": "flow_percent", "value": math.nan},
              "out_of_bounds_small_step": {"operation": "flow_percent", "value": 135},
              "step_down_past_max": {"operation": "flow_percent", "value": 70},
              "step_up_past_max": {"operation": "flow_percent", "value": 130}}.get(
        case, {"operation": "flow_percent", "value": 95})
    s.pipeline.controller = RogueController(s.pipeline.controller, action, hold=case == "hold")
    if case == "auto_off_mid_step":  # the step read Auto on (snapshot); it is off by the final check (public)
        monkeypatch.setattr(s.control, "public", lambda: {"target_mm": 1.35, "auto": False})
    if case == "sends_disabled":
        monkeypatch.setattr("server.pipeline.send_enabled", iter([True, False]).__next__)
    if case == "gate_closed":
        s.pipeline._status = (writer_status(s.clock(), authorized=False), s.clock())
    if case == "stopping":
        s.pipeline._stop.set()
    if case == "no_flow_readback":
        msg = telemetry(s.clock())
        msg["sources"]["duet_readback"]["payload"]["extrusion_valid"] = False
        s.bridge.deliver(TELEMETRY, msg, s.clock())
    d = s.pipeline.tick()
    assert (len(s.bridge.sent) == 1) is (case == "ok")
    assert (len(s.pipeline.controller.sent) == 1) is (case == "ok")
    if REFUSALS[case]:
        assert d["reason"].startswith("not sent: ") and REFUSALS[case] in d["reason"]
        assert d["hold"] is True and d["sent"] is False and d["action"] == action, "intended, not taken"
    else:
        assert "not sent" not in d["reason"] and d["sent"] is (True if case == "ok" else None)


WALLS = "alarm: below 1.30 mm (walls may not touch); "


@pytest.mark.parametrize("override", ["not sent", "clock", "target stamp"])
def test_the_walls_alarm_leads_the_pipelines_own_overrides(sim, monkeypatch, tmp_path, override):
    if override == "clock":
        monkeypatch.setattr(sys.modules[__name__], "OFFSET_MS", 5000)
    s = sim(auto=True, **({"camera": stub_camera(), "target": None, "control": legacy_file(tmp_path)}
                          if override == "target stamp" else {}))
    s.flow, s.flow_history = 80.0, [(-math.inf, 80.0)]  # a 1.20 mm bead
    s.run(30)
    if override == "not sent":
        s.pipeline.controller = RogueController(s.pipeline.controller, {"operation": "flow_percent", "value": 90})
        s.pipeline._status = (writer_status(s.clock(), authorized=False), s.clock())
        d = s.pipeline.tick()
        assert d["reason"].startswith(WALLS + "not sent: gate closed") and d["sent"] is False and d["hold"]
    else:
        d = s.decisions[-1]
        rest = "alarm: device clock differs from LEAM's" if override == "clock" else TARGET_ALARM
        assert d["reason"].startswith(WALLS + rest) and d["action"] is None and d["hold"]
    s.flow, s.flow_history = 100.0, [(-math.inf, 100.0)]  # 1.50 mm: no walls alarm, the overrides as before
    s.run(30)
    assert not s.decisions[-1]["reason"].startswith(WALLS)


@pytest.mark.parametrize("op, value, floor, error_mm, sent", [
    ("flow_percent", 120, True, 0.2, True), ("speed_override_percent", 80, True, 0.2, True),
    ("flow_percent", 121, True, 0.2, False), ("flow_percent", 80, True, 0.2, False),
    ("speed_override_percent", 100, True, 0.2, False), ("flow_percent", 120, True, -0.1, False),
    ("flow_percent", 120, False, 0.2, False)],
    ids=["floor 2x widening", "floor 2x widening by speed", "floor 2x + 1", "floor 2x narrowing", "floor 2x speed up",
         "floor 2x, bead wider than wanted", "2x without the floor rules"])
def test_the_floor_rules_bigger_step_goes_out_only_widening_a_narrow_bead(sim, op, value, floor, error_mm, sent):
    s = sim(auto=True, config={"floor_step_factor": 2.0})
    if (op, value) == ("speed_override_percent", 100):
        s.speed = 80.0  # so that +20 is inside the bounds: a narrowing step
    s.run(3)
    s.pipeline.controller = RogueController(s.pipeline.controller, {"operation": op, "value": value}, floor=floor,
                                            error_mm=error_mm)
    d = s.pipeline.tick()
    assert (len(s.bridge.sent) == 1) is sent
    if not sent:
        assert "from the readback" in d["reason"] and d["sent"] is False


def test_the_pipeline_keeps_its_own_spacing(sim):
    s = sim(auto=True)
    s.run(3)
    s.pipeline.controller = RogueController(s.pipeline.controller, {"operation": "flow_percent", "value": 95})
    s.pipeline.tick()
    s.clock.t += 1999
    s.pipeline._status = (writer_status(s.clock()), s.clock())
    assert "spacing" in s.pipeline.tick()["reason"]
    s.clock.t += 1
    s.pipeline.tick()
    assert [x["t"] for x in s.bridge.sent] == [T0 + 3000, T0 + 5000]


@pytest.mark.parametrize("broken", [None, "normal_authorized", "status_age", "telemetry_age", "gateway_state",
                                    "permits_outputs", "command_gate_closed", "gateway_source_age",
                                    "gateway_source_invalid"])
def test_the_gate_needs_every_condition(sim, broken):
    s = sim()
    s.run(3)
    now = s.clock() + 100
    s.clock.t = now
    msg = telemetry(now)
    gw = msg["sources"]["gateway"]
    if broken == "gateway_state":
        gw["payload"]["state"] = "SAFE"
    if broken == "permits_outputs":
        gw["payload"]["permits_outputs"] = False
    if broken == "command_gate_closed":
        gw["payload"]["command_gate_closed"] = True
    if broken == "gateway_source_age":
        gw["source_utc_ns"] = (msg["published_utc_ms"] - 3001) * 1_000_000
    if broken == "gateway_source_invalid":
        gw["valid"] = False
    s.bridge.deliver(TELEMETRY, msg, now - (5001 if broken == "telemetry_age" else 0))
    s.bridge.deliver(STATUS, writer_status(now, authorized=broken != "normal_authorized"),
                     now - (5001 if broken == "status_age" else 0))
    assert s.pipeline.gate_open(now) is (broken is None)


def test_when_telemetry_stops_the_controller_holds_and_nothing_is_sent(sim):
    s = sim(auto=False)
    s.run(20)
    s.run(30, auto=lambda now: True)
    stop_at = s.clock()
    s.telemetry_on = lambda now: now < stop_at
    s.run(30, auto=lambda now: True)
    expired = stop_at + 2000  # telemetry expires 1 s after publishing; 1 s for delivery
    late = [d for d in s.decisions if d["t_ms"] > expired]
    assert late and all(d["hold"] and d["action"] is None for d in late)
    assert sends_between(s, T0, stop_at) and not sends_between(s, expired + 1, math.inf)


def test_no_advice_from_expired_telemetry(sim):
    s = sim(auto=False)
    s.run(30)
    assert s.decisions[-1]["action"], "warm: advice is being given"
    stop_at = s.clock()
    s.telemetry_on = lambda now: now < stop_at
    s.run(10)
    late = [d for d in s.decisions if d["t_ms"] > stop_at + 2000]
    assert late and all(d["action"] is None and d["reason"].startswith(("not extruding", "no readback")) for d in late)


@pytest.mark.parametrize("age_ms, fresh", [(2999, True), (3001, False)])
def test_readbacks_are_fresh_by_leam_clock(sim, age_ms, fresh):
    s = sim()
    s.clock.t += 100
    msg = telemetry(s.clock())
    old_ns = (msg["published_utc_ms"] - age_ms) * 1_000_000
    msg["sources"]["duet_readback"]["payload"]["extrusion_source_utc_ns"] = old_ns
    msg["sources"]["duet_readback"]["payload"]["temperatures"]["nozzle"]["source_utc_ns"] = old_ns
    s.bridge.deliver(TELEMETRY, msg, s.clock())
    setpoints, measured, _ = s.pipeline._inputs(s.clock())
    assert (setpoints["flow_percent"] is not None) is fresh
    assert (setpoints["nozzle_temperature_c"] is not None) is fresh and (measured["nozzle_c"] is not None) is fresh
    assert setpoints["speed_override_percent"] == 100.0


@pytest.mark.parametrize("skew_ms", [150, 1500, 2990, 3010, -5000])
def test_a_skewed_device_clock_still_measures_and_alarms_beyond_2_s(sim, monkeypatch, skew_ms):
    monkeypatch.setattr(sys.modules[__name__], "OFFSET_MS", skew_ms - 20)
    s = sim(auto=False)
    s.run(60)
    assert s.pipeline.offset_ms == pytest.approx(skew_ms, abs=1)
    assert s.pipeline.estimate() is not None and s.pipeline.estimator.skipped == {}
    alarm = abs(skew_ms) > 2000
    last = s.decisions[-20:]
    assert all(d["reason"].startswith("alarm: device clock differs from LEAM's") for d in last) is alarm
    assert any(d["action"] for d in last) is not alarm
    assert (s.pipeline.status()["clock_alarm"] is not None) is alarm


def test_estimator_gets_the_nozzle_reading_nearest_the_frame_and_speed_in_mm_s(sim, monkeypatch):
    s = sim()
    calls = []
    monkeypatch.setattr(s.pipeline.estimator, "add", lambda sample, nozzle_c, speed: calls.append((nozzle_c, speed)))
    for i in range(40):  # nozzle reads 200 C for 2 s, then 240 C; the frame is acquired ~1 s before it arrives
        s.clock.t += 100
        s.bridge.deliver(TELEMETRY, telemetry(s.clock(), actual_c=200.0 if i < 20 else 240.0), s.clock())
        if i == 25:
            s.bridge.deliver(IR, ir_frame(s.clock(), 1), s.clock())
    s.pipeline.process_pending()
    assert calls == [(200.0, pytest.approx(20.0))]
    s.clock.t += 10_000
    s.bridge.deliver(IR, ir_frame(s.clock(), 2), s.clock())
    s.pipeline.process_pending()
    assert calls[-1][0] is None, "no nozzle reading within 3 s of the frame"


# models ---------------------------------------------------------------------------------------------------------------
def test_no_geometry_means_no_extraction_and_no_action(sim):
    s = sim(auto=True, extractor=None, camera=None, printer=None)
    s.run(30)
    assert s.pipeline.extraction == "no geometry" and s.pipeline._frame is None
    assert s.pipeline.stats["frames_done"] == 0
    assert {d["reason"] for d in s.decisions} == {"no model: geometry, camera model, printer model"}
    assert all(d["hold"] and d["action"] is None for d in s.decisions)
    assert s.sent() == [] and s.pipeline.limits() == {} and s.pipeline.estimate() is None


def test_no_camera_model_logs_samples_but_never_estimates_or_acts(sim):
    s = sim(auto=True, camera=None)
    s.extractor.camera = StubCameraModel(g=0.0, s=0.0)
    s.run(30)
    assert len(s.store.recent_width_samples(1000)) == 30
    assert s.pipeline.estimate() is None
    widths = [m for m in s.emitted if m["type"] == "width"]
    assert widths and all(m["mm"] is None and m["reason"] == "no model: camera model" for m in widths)
    assert {d["reason"] for d in s.decisions} == {"no model: camera model"} and s.sent() == []


def test_no_printer_model_holds_unless_fallback_mode(sim):
    s = sim(auto=True, printer=None).run(60)
    assert {d["reason"] for d in s.decisions} == {"no model: printer model"} and s.sent() == []
    s = sim(auto=False, printer=None, config={"mode": "fallback"})
    s.run(20)
    s.run(100, auto=lambda now: True)
    assert s.sent() and {x["request"]["correction"]["operation"] for x in s.sent()} == {"flow_percent"}
    assert set(s.pipeline.limits()["levers"]) == {"flow_percent"}
    s.check_sends()


def test_width_config_hash_survives_json_and_key_order():
    from server.width import DEFAULTS
    h = width_config_hash(DEFAULTS)
    assert len(h) == 12 and int(h, 16) >= 0
    assert width_config_hash(json.loads(json.dumps(DEFAULTS))) == h, "a config read back from JSON hashes the same"
    assert width_config_hash(dict(reversed(list(DEFAULTS.items())))) == h
    assert width_config_hash({**DEFAULTS, "lateral_search_px": 2.0}) != h


def test_geometry_version_is_the_files_version_or_a_digest():
    assert geometry_version({"version": "0.2", "cameras": {}}) == "0.2"
    digest = geometry_version({"cameras": {"0": {"f_px": 100}}, "image_size": [160, 120]})
    assert len(digest) == 12 and digest == geometry_version({"image_size": [160, 120], "cameras": {"0": {"f_px": 100}}})


GEOMETRY_02 = SimpleNamespace(model={"version": "0.2"})
WIDTH_CFG = {"s_window_px": (18.0, 28.0), "lateral_search_px": 1.0}


def fitted_camera(**fields):
    camera = StubCameraModel(g=0.0, s=0.0)
    camera.data = fields  # what CameraModel keeps from camera_model.json
    return camera


@pytest.mark.parametrize("fields, provenance", [
    ({"geometry_version": "0.2", "width_config_hash": width_config_hash(WIDTH_CFG)}, "ok"),
    ({}, "unknown provenance"),
    ({"geometry_version": "0.2"}, "unknown provenance"),
    ({"geometry_version": "0.1", "width_config_hash": width_config_hash(WIDTH_CFG)}, "differs"),
    ({"geometry_version": "0.2", "width_config_hash": width_config_hash({**WIDTH_CFG, "lateral_search_px": 2.0})},
     "differs"),
    ({"geometry_version": "0.1"}, "differs"),
])
def test_a_camera_fit_from_another_setup_holds_everything(sim, fields, provenance):
    s = sim(auto=True, camera=fitted_camera(**fields), geometry=GEOMETRY_02, extractor_cfg=dict(WIDTH_CFG))
    s.run(40)
    assert s.pipeline.status()["camera_provenance"].startswith(provenance)
    if provenance == "differs":
        assert {d["reason"] for d in s.decisions} == {
            "alarm: camera model fitted on a different geometry/width setup; refit"}
        assert all(d["hold"] and d["action"] is None for d in s.decisions)
        assert s.sent() == [] and s.pipeline.limits() == {}
        s.control.update({"auto": False})
        assert s.pipeline.tick()["reason"].startswith("alarm: camera model fitted"), "no advice either"
    else:
        assert s.sent(), "unknown provenance only warns"


TARGET_ALARM = "alarm: target set under another camera model; set it again"


def stub_camera(version="cam-A", relative=False):
    camera = StubCameraModel(g=0.0, s=0.0)
    camera.version, camera.relative = version, relative
    return camera


def legacy_file(tmp_path, stamp=None):
    (tmp_path / "data").mkdir(exist_ok=True)
    data = {"target_mm": 1.35, **({"target_stamp": stamp} if stamp else {})}
    (tmp_path / "data" / "control.json").write_text(json.dumps(data))
    return ControlState(tmp_path / "data" / "control.json")


@pytest.mark.parametrize("case", ["legacy file", "model swapped", "relative, typed", "relative, hold",
                                  "unreadable stamp", "same model"])
def test_a_target_acts_only_under_the_camera_model_it_was_set_under(sim, tmp_path, case):
    stamps = {"legacy file": None,
              "model swapped": {"camera_model": "cam-OLD", "relative": False, "by": "typed"},
              "relative, typed": {"camera_model": "cam-A", "relative": True, "by": "typed"},
              "relative, hold": {"camera_model": "cam-A", "relative": True, "by": "hold"},
              "unreadable stamp": {"camera_model": "cam-A"},
              "same model": {"camera_model": "cam-A", "relative": False, "by": "typed"}}
    camera = stub_camera(relative=case.startswith("relative"))
    s = sim(auto=False, camera=camera, target=None, control=legacy_file(tmp_path, stamps[case]))
    s.run(20)
    s.run(60, auto=lambda now: True)
    acts = case in ("relative, hold", "same model")
    assert any(d["action"] for d in s.decisions) is acts and bool(s.sent()) is acts
    assert all(d["reason"] == TARGET_ALARM and d["hold"] and d["action"] is None for d in s.decisions) is not acts
    assert s.control.target_mm == 1.35, "the target is kept and shown, just not acted on"


def test_setting_the_target_again_clears_the_alarm(app, sim, tmp_path):
    s = sim(auto=False, camera=stub_camera(), target=None, control=legacy_file(tmp_path))
    s.run(20)
    assert s.decisions[-1]["reason"] == TARGET_ALARM
    app.main.app.state.pipeline, app.main.control = s.pipeline, s.control
    assert app.client.get("/api/control").json()["last_decision"]["reason"] == TARGET_ALARM
    assert app.client.post("/api/control", json={"target_mm": 1.35}).status_code == 200
    assert s.control.target_stamp == {"camera_model": "cam-A", "relative": False, "by": "typed"}
    assert json.loads((tmp_path / "data" / "control.json").read_text())["target_stamp"] == s.control.target_stamp
    s.run(10)
    assert TARGET_ALARM not in {d["reason"] for d in s.decisions[-10:]}
    assert ControlState(tmp_path / "data" / "control.json").target_stamp == s.control.target_stamp, "survives restart"


def test_under_a_relative_model_hold_current_stamps_a_target_that_acts(app, sim, tmp_path):
    s = sim(auto=False, camera=stub_camera(relative=True), target=None)
    s.run(20)
    app.main.app.state.pipeline, app.main.control = s.pipeline, s.control
    assert app.client.post("/api/control", json={"target_mm": 1.3}).status_code == 400
    assert app.client.post("/api/control", json={"hold_current": False, "target_mm": 1.3}).status_code == 400
    assert app.client.post("/api/control", json={"hold_current": True}).status_code == 200
    assert s.control.target_stamp == {"camera_model": "cam-A", "relative": True, "by": "hold"}
    s.run(5)
    assert TARGET_ALARM not in {d["reason"] for d in s.decisions[-5:]}


def test_clearing_the_target_drops_its_stamp_and_auto_alone_keeps_it(tmp_path):
    state = ControlState(tmp_path / "control.json")
    stamp = {"camera_model": "cam-A", "relative": False, "by": "typed"}
    state.update({"target_mm": 1.3}, stamp=stamp)
    state.update({"auto": True}, stamp={"camera_model": "other", "relative": False, "by": "typed"})
    assert {k: state.snapshot()[k] for k in ("target_mm", "auto", "target_stamp")} == \
        {"target_mm": 1.3, "auto": True, "target_stamp": stamp}
    state.update({"target_mm": None}, stamp=stamp)
    assert state.target_stamp is None and json.loads((tmp_path / "control.json").read_text()) == {"target_mm": None}
    with pytest.raises(ValueError):
        state.update({"target_mm": 1.3, "target_stamp": stamp})


@pytest.mark.parametrize("levers, value, refusal", [
    ({"flow_percent": {"enabled": False}}, 95, "flow_percent is switched off for Auto"),
    ({"flow_percent": {"max_step": 3}}, 95, "flow_percent 95 is more than 3 from the readback 100"),
    ({"flow_percent": {"max_step": 3}}, 97, None),
    ({"flow_percent": {"max_step": 3}}, "floor 106", None),
    ({"flow_percent": {"max_step": 3}}, "floor 107", "flow_percent 107 is more than 6 from the readback 100"),
    ({"flow_percent": {"max_step": 3}}, "floor 94", "flow_percent 94 is more than 3 from the readback 100"),
])
def test_the_last_check_takes_the_operators_tuning_from_the_control_state(sim, levers, value, refusal):
    floor = isinstance(value, str)  # a floor-rules widening step may be floor_step_factor x the tuned step, no more
    value = int(value.split()[1]) if floor else value
    s = sim(auto=True, config={"floor_step_factor": 2.0})
    s.run(3)
    s.control.update({"levers": levers})
    s.pipeline.controller = RogueController(s.pipeline.controller, {"operation": "flow_percent", "value": value},
                                            floor=floor, error_mm=0.2 if floor else None)
    d = s.pipeline.tick()
    assert (len(s.bridge.sent) == 1) is (refusal is None)
    if refusal:
        assert d["reason"] == f"not sent: {refusal}" and d["sent"] is False


def test_every_step_leaves_a_trace_row_and_manual_sends_are_events(sim):
    s = sim(auto=True, target=1.35)
    s.control.update({"band_below_mm": 0.1, "band_above_mm": None})
    s.run(5)
    rows = s.pipeline.trace()["rows"]
    assert len(rows) == 5
    last = rows[-1]
    assert (last["target_mm"], last["band_below_mm"], last["band_above_mm"], last["auto"]) == (1.35, 0.1, 0.05, True)
    assert (last["flow"], last["speed"], last["nozzle_target"]) == (100, 100, 220) and last["reason"]
    judged = s.pipeline._judged  # the noise band is the controller's, from its own (longer, post-change) estimate
    assert isinstance(last["mm"], float) and last["noise_mm"] == (pytest.approx(2 * judged["sigma_mm"]) if judged else None)
    assert [r["t_ms"] for r in s.pipeline.trace(rows[-2]["t_ms"])["rows"]] == [last["t_ms"]]
    s.pipeline.note_manual_send({"correction": {"operation": "flow_percent", "value": 90}}, {"ok": True})
    s.pipeline.note_manual_send({"correction": {"operation": "flow_percent", "value": 80}}, {"ok": False})
    assert [(e["kind"], e["operation"], e["value"]) for e in s.pipeline.trace()["events"]] == \
        [("manual", "flow_percent", 90)]
    json.dumps(s.pipeline.trace(), allow_nan=False)


def test_every_frame_is_a_sample_and_every_step_carries_the_models_view(sim):
    s = sim(auto=True, target=1.35)
    s.run(6)
    trace = s.pipeline.trace()
    assert len(trace["samples"]) == 6 and all(isinstance(x["mm"], float) and x["reason"] == "ok" for x in trace["samples"])
    assert [m for m in s.emitted if m["type"] == "sample"][-1]["t_ms"] == trace["samples"][-1]["t_ms"]
    model = trace["rows"][-1]["model"]
    assert set(model["levers"]) == {"flow_percent", "speed_override_percent", "nozzle_temperature_c"}
    flow, speed = model["levers"]["flow_percent"], model["levers"]["speed_override_percent"]
    assert flow["up"] > 0 > flow["down"] and speed["up"] < 0 <= speed["down"], "faster narrows (LEAM bounds, D54)"
    assert [m["t_ms"] for m in s.emitted if m["type"] == "trace"] == [r["t_ms"] for r in trace["rows"]]
    json.dumps(trace, allow_nan=False)


def test_the_operators_settings_reach_the_controller_at_the_next_step(sim):
    s = sim(auto=True, target=1.35)
    s.run(2)
    assert s.pipeline.trace()["rows"][-1]["min_width_mm"] == 1.30
    s.control.update({"settings": {"min_width_mm": 1.45, "persist_s": 3}})
    s.run(1)
    assert s.pipeline.trace()["rows"][-1]["min_width_mm"] == 1.45
    assert s.pipeline.controller.cfg["persist_s"] == 3.0 and s.pipeline.limits()["defaults"]["min_width_mm"] == 1.30
    s.control.update({"settings": {"min_width_mm": None}})
    s.run(1)
    assert s.pipeline.trace()["rows"][-1]["min_width_mm"] == 1.30 and s.pipeline.controller.cfg["persist_s"] == 3.0


def test_the_trace_keeps_fifteen_minutes(sim):
    s = sim(auto=False)
    assert s.pipeline._trace.maxlen == 900


def test_ceiling_and_relative_come_from_the_camera_model(sim):
    camera = StubCameraModel(g=0.0, s=0.0)
    s = sim(camera=camera)
    assert (s.pipeline.ceiling_mm(), s.pipeline.relative()) == (None, False)
    camera.ceiling_mm = lambda: 1.5
    camera.relative = True
    assert (s.pipeline.ceiling_mm(), s.pipeline.relative()) == (1.5, True)
    status = s.pipeline.status()
    assert (status["ceiling_mm"], status["relative"]) == (1.5, True)
    camera.ceiling_mm = lambda: math.nan
    assert s.pipeline.ceiling_mm() is None


def test_load_models_reports_missing_and_broken_files_as_none(tmp_path):
    (tmp_path / "camera_model.json").write_text("{}")
    (tmp_path / "printer_model.json").write_text("{broken")
    assert load_models(tmp_path) == {"geometry": None, "camera_model": None, "printer_model": None}
    if GEOMETRY.exists():
        (tmp_path / "geometry.json").write_text(GEOMETRY.read_text())
        assert load_models(tmp_path)["geometry"] is not None


# clocks, frames, failures ---------------------------------------------------------------------------------------------
def test_deposit_times_reach_the_estimator_on_the_local_clock(sim):
    s = sim()
    for i, delay in enumerate((300, OFFSET_MS + 20, 200)):
        s.clock.t += 100
        msg = telemetry(s.clock())
        msg["sources"]["motion"]["source_utc_ns"] = (s.clock() - delay) * 1_000_000
        s.bridge.deliver(TELEMETRY, msg, s.clock())
    s.bridge.deliver(IR, ir_frame(s.clock(), 1), s.clock())
    sample = s.pipeline.process_pending()
    assert s.pipeline.clock_offset_ms() == pytest.approx(OFFSET_MS + 20, abs=0.01)
    leam_t = s.clock() - 850 - OFFSET_MS
    assert sample.t_ms == leam_t + OFFSET_MS + 20 and sample.t_dep_ms == leam_t - 500 + OFFSET_MS + 20
    row = s.store.recent_width_samples(1)[0]
    assert (row["t_ms"], row["t_dep_ms"], row["group"]) == (sample.t_ms, sample.t_dep_ms, GROUP)
    s.clock.t += 31_000
    s.bridge.deliver(IR, ir_frame(s.clock(), 2), s.clock())
    sample = s.pipeline.process_pending()
    assert s.pipeline.clock_offset_ms() is None
    assert sample.t_ms == s.clock() and sample.t_dep_ms is None


def test_only_the_newest_frame_is_measured_and_no_image_is_kept(sim):
    s = sim()
    s.run(3)
    for seq in (101, 102, 103):
        s.bridge.deliver(IR, ir_frame(s.clock(), seq), s.clock())
    sample = s.pipeline.process_pending()
    assert sample.source_frame_id == "103" and s.pipeline.process_pending() is None
    assert s.pipeline.stats["frames_dropped"] == 2
    assert holds_image(s.pipeline) == []


def holds_image(pipeline):
    """Paths to any bytes or base64-sized text the pipeline, its extractor or its estimator still holds."""
    found, seen = [], set()

    def walk(value, path, depth):
        if id(value) in seen or depth > 5:
            return
        seen.add(id(value))
        if isinstance(value, (bytes, bytearray, memoryview)) or (isinstance(value, str) and len(value) > 300):
            found.append(path)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}[{k!r}]", depth + 1)
        elif isinstance(value, (list, tuple, deque, set)):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]", depth + 1)
        elif hasattr(value, "__dict__") and depth < 2:
            walk(vars(value), path, depth + 1)
    for name in ("pipeline", "extractor", "estimator", "controller", "motion"):
        obj = pipeline if name == "pipeline" else getattr(pipeline, name)
        walk({k: v for k, v in vars(obj).items() if k not in ("bridge", "control", "store", "emit")}, name, 0)
    return found


def test_a_failing_store_never_stops_the_worker_or_the_loop(sim, caplog):
    class BrokenStore:
        def __getattr__(self, name):
            def fail(row):
                raise OSError(28, "No space left on device")
            return fail

        def close(self):
            pass
    s = sim(auto=False, store=BrokenStore())
    s.run(20)
    with caplog.at_level(logging.ERROR, logger="server.pipeline"):
        s.run(40, auto=lambda now: True)
    assert len(s.decisions) == 60 and s.pipeline.stats["frames_done"] == 60
    assert s.sent(), "a broken log never blocks control"
    assert s.pipeline.errors["store"] > 50
    assert len([r for r in caplog.records if "store" in r.getMessage()]) <= 1


def test_a_failing_extractor_or_step_is_contained(sim):
    s = sim(auto=True)

    def boom(*args):
        raise RuntimeError("extractor bug")
    s.extractor.process = boom
    s.run(5)
    assert s.pipeline.errors["frame"] == 5 and len(s.decisions) == 5
    s.pipeline.controller.step = boom
    d = s.pipeline.tick()
    assert d["hold"] and d["action"] is None and d["reason"] == "alarm: control step failed (RuntimeError)"
    assert s.sent() == []


def test_decisions_are_emitted_every_step_and_stored_on_change_or_every_10_s(sim):
    s = sim(auto=False, target=None)
    s.run(35)
    emitted = [m for m in s.emitted if m["type"] == "decision"]
    assert len(emitted) == 35 and len({m["t_ms"] for m in emitted}) == 35
    stored = [r["t_ms"] for r in reversed(s.store.recent_decisions(100))]
    assert stored == [T0 + 1000, T0 + 11_000, T0 + 21_000, T0 + 31_000]
    assert {r["reason"] for r in s.store.recent_decisions(100)} == {"no target"}


def test_widths_are_emitted_at_most_twice_a_second(sim):
    s = sim()
    for i in range(10):
        s.clock.t += 100
        s.bridge.deliver(TELEMETRY, telemetry(s.clock()), s.clock())
        s.bridge.deliver(IR, ir_frame(s.clock(), i), s.clock())
        s.pipeline.process_pending()
    times = [m for m in s.emitted if m["type"] == "width"]
    assert len(times) == 2


def test_display_estimate_is_ungated_and_aged_at_each_call(sim):
    s = sim(auto=False)
    s.run(20)
    s.control.update({"auto": True})
    s.pipeline.tick()  # Auto just armed: the controller only counts beads deposited from now on
    since = s.pipeline.controller.estimate_since()
    assert since == s.clock() and s.pipeline.estimator.estimate(s.clock(), since_ms=since) is None
    first = s.pipeline.estimate()
    assert first["n"] >= 10 and first["since_ms"] is None
    s.clock.t += 1500
    assert s.pipeline.estimate()["age_ms"] == first["age_ms"] + 1500
    assert s.pipeline.last_decision()["age_ms"] == 1500


def test_feed_is_the_travel_speed_at_100_percent_override(sim):
    s = sim()
    assert s.pipeline._inputs(s.clock())[2] == {"layer": None}
    for speed in (100.0, 60.0, 60.0):
        s.clock.t += 100
        msg = telemetry(s.clock(), speed=speed)
        msg["sources"]["motion"]["payload"]["velocity_magnitude"] = 0.2 * speed
        s.bridge.deliver(TELEMETRY, msg, s.clock())
    context = s.pipeline._inputs(s.clock())[2]
    assert context["feed_mm_s"] == pytest.approx(20.0) and context["layer_h_mm"] == 0.5
    assert context["line_w_mm"] == 1.5


def test_layer_comes_from_z_not_from_a_counter(sim):
    s = sim()
    for z, layer in ((0.695, 1), (0.7, 1), (1.21, 2), (3.2, 6), (7.19, 14)):
        s.clock.t += 100
        s.bridge.deliver(TELEMETRY, telemetry(s.clock(), z=z), s.clock())
        assert s.pipeline._inputs(s.clock())[2]["layer"] == layer
    s.bridge.deliver(TELEMETRY, telemetry(s.clock(), z=9.0, extruding=False), s.clock())
    assert s.pipeline._inputs(s.clock())[2]["layer"] == 14, "travel moves and z hops don't count"


# the bridge hook --------------------------------------------------------------------------------------------------------
def test_a_raising_hook_never_stops_message_handling(monkeypatch, caplog):
    for name in BROKER_ENV:
        monkeypatch.delenv(name, raising=False)
    b, seen = Bridge(), []

    def bad(topic, payload, recv_ms):
        raise KeyError("boom")
    b.hooks += [bad, lambda *args: seen.append(args)]
    with caplog.at_level(logging.ERROR, logger="server.bridge"):
        for i in range(3):
            msg = SimpleNamespace(topic=TELEMETRY, payload=json.dumps(telemetry(T0 + i)).encode(), retain=False)
            b._on_message(None, None, msg)
        b._on_message(None, None, SimpleNamespace(topic=STATUS, payload=b"not json", retain=False))
    assert [a[0] for a in seen] == [TELEMETRY] * 3 + [STATUS]
    assert isinstance(seen[0][1], dict) and seen[3][1] == "not json" and isinstance(seen[0][2], int)
    assert b.topics[TELEMETRY]["count"] == 3 and len(b.history) >= 1
    logged = [r for r in caplog.records if "hook" in r.getMessage()]
    assert len(logged) == 1 and logged[0].exc_info


@pytest.mark.parametrize("payload", [b'{"sources": {"melt": 5}}', b'{"sources": {"melt": null}}',
                                     b'{"sources": null}', b"[" * 100_000])
def test_a_malformed_message_never_reaches_paho(monkeypatch, caplog, payload):
    for name in BROKER_ENV:
        monkeypatch.delenv(name, raising=False)
    b, seen = Bridge(), []
    b.hooks.append(lambda topic, p, recv_ms: seen.append(p))
    good = SimpleNamespace(topic=TELEMETRY, payload=json.dumps(telemetry(T0)).encode(), retain=False)
    with caplog.at_level(logging.ERROR, logger="server.bridge"):
        for _ in range(3):
            b._on_message(None, None, SimpleNamespace(topic=TELEMETRY, payload=payload, retain=False))
        b._on_message(None, None, good)
    assert isinstance(seen[-1], dict) and seen[-1]["sources"]["motion"]["valid"] is True
    assert len(seen) == (1 if payload.startswith(b"[") else 4), "the pipeline judges what the bridge couldn't book"
    assert len(b.history) == 1 and b.history[0]["nozzle"]["current"] == 220.0
    assert len([r for r in caplog.records if r.exc_info]) == 1


def test_a_broken_dashboard_listener_never_breaks_a_send(monkeypatch, caplog):
    clock = Clock()
    published = []
    b = real_bridge(monkeypatch, clock, published.append)
    reached = []

    def dead(message):
        raise RuntimeError("Event loop is closed")
    b.listeners += [dead, reached.append]
    with caplog.at_level(logging.ERROR, logger="server.bridge"):
        result = b.send_request({"request_id": "r1", "sequence": 1, "correction": {}})
        clock.t += 2000
        b.send_request({"request_id": "r2", "sequence": 2, "correction": {}})
    assert result["ok"] is True and [r["request_id"] for r in published] == ["r1", "r2"]
    assert [m["payload"]["request_id"] for m in reached] == ["r1", "r2"]
    assert len([r for r in caplog.records if "listener" in r.getMessage()]) == 1


def test_sends_from_two_threads_share_one_rate_limit_and_sequence(monkeypatch):
    clock = Clock()
    published = []
    b = real_bridge(monkeypatch, clock, published.append)

    def slow_time():  # widens the window between the rate-limit check and the publish
        time.sleep(0.01)
        return clock() / 1000
    monkeypatch.setattr(bridge_module, "time", SimpleNamespace(time=slow_time, monotonic=lambda: clock() / 1000))
    results = []
    threads = [threading.Thread(target=lambda n=n: results.append(b.send_request(
        {"request_id": f"r{n}", "sequence": n, "correction": {}}))) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(published) == 1 and sorted(r["ok"] for r in results) == [False, True]
    clock.t += 2000
    assert b.send_request({"request_id": "r3", "sequence": 1, "correction": {}})["error"].startswith("sequence 1")
    assert len(published) == 1


def test_retained_messages_reach_no_hook(monkeypatch):
    for name in BROKER_ENV:
        monkeypatch.delenv(name, raising=False)
    b, seen = Bridge(), []
    b.hooks.append(lambda *args: seen.append(args))
    b._on_message(None, None, SimpleNamespace(topic=STATUS, payload=b"{}", retain=True))
    assert seen == []


# threads ----------------------------------------------------------------------------------------------------------------
class ThreadSpy:
    """Records which threads call the wrapped object's methods."""

    def __init__(self, inner):
        self.inner, self.threads = inner, set()

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kw):
            self.threads.add(threading.current_thread().name)
            return attr(*args, **kw)
        return call


def test_threads_measure_frames_and_tick_until_stopped(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTROL_SEND_ENABLED", raising=False)
    bridge, emitted = FakeBridge(), []
    store = Store(tmp_path / "prototype.db")
    control = ControlState(tmp_path / "control.json")
    control.update({"auto": True})
    p = Pipeline(bridge, control, store, camera_model=StubCameraModel(),
                 printer_model=StubPrinterModel(), extractor=FakeExtractor(lambda t: 1.5, StubCameraModel()),
                 settings={"control_period_s": 0.05}, emit=emitted.append)
    p.controller = spy = ThreadSpy(p.controller)
    p.start()
    try:
        assert bridge.hooks == [p.on_message]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (p.stats["frames_done"] < 3 or p.stats["foreign_results"] < 3
                                               or sum(m["type"] == "decision" for m in emitted) < 3):
            now = int(time.time() * 1000)
            bridge.deliver(TELEMETRY, telemetry(now), now)
            bridge.deliver(IR, ir_frame(now, int(now)), now)
            bridge.deliver(RESULT, {"request_id": f"other-{now}", "state": "accepted"}, now)
            request = {"request_id": f"m-{now}", "correction": {"operation": "flow_percent", "value": 101}}
            p.note_manual_send(request, {"ok": True})
            p.pinned()
            time.sleep(0.02)
        assert p.stats["frames_done"] >= 3 and sum(m["type"] == "decision" for m in emitted) >= 3
        assert p.stats["foreign_results"] >= 3 and "flow_percent" in p.pinned()
        assert spy.threads == {"pipeline-control"}
    finally:
        p.stop()
        store.close()
    assert bridge.hooks == [] and not any(t.is_alive() for t in p._threads)
    done = p.stats["frames_done"]
    bridge.deliver(IR, ir_frame(T0, 1), T0)
    assert p._frame is None and p.stats["frames_done"] == done


# the app ----------------------------------------------------------------------------------------------------------------
def _server_modules():
    return {n: m for n, m in sys.modules.items() if n == "server" or n.startswith("server.")}


@pytest.fixture
def app(tmp_path, monkeypatch):
    yield from fresh_app(tmp_path, monkeypatch)


def fresh_app(tmp_path, monkeypatch):
    """A fresh server.main with DATA_DIR, MODELS_DIR and TMPDIR in tmp and no broker."""
    data, models, tmp = tmp_path / "data", tmp_path / "models", tmp_path / "tmp"
    models.mkdir()
    tmp.mkdir()
    for name in BROKER_ENV + ["CONTROL_SEND_ENABLED"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setattr(tempfile, "tempdir", None)
    saved = _server_modules()
    for name in saved:
        del sys.modules[name]
    try:
        main = importlib.import_module("server.main")
        with TestClient(main.app) as client:
            yield SimpleNamespace(client=client, main=main, data=data, tmp=tmp)
    finally:
        for name in _server_modules():
            del sys.modules[name]
        sys.modules.update(saved)


def test_app_runs_the_pipeline_and_serves_its_state(app):
    main, pipeline = app.main, app.main.app.state.pipeline
    assert type(pipeline).__name__ == "Pipeline" and pipeline.on_message in main.bridge.hooks
    assert main.control.auto is False and main.HOLD_MIN_CONFIDENCE == control_config.DEFAULTS["min_confidence"]
    body = app.client.get("/api/control").json()
    assert abs(body["server_ms"] - time.time() * 1000) < 5000
    assert (body["estimate"], body["limits"], body["pinned"]) == (None, {}, {})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and app.client.get("/api/control").json()["last_decision"] is None:
        time.sleep(0.1)
    d = app.client.get("/api/control").json()["last_decision"]
    assert d["reason"] == "no model: geometry, camera model, printer model" and d["hold"] is True
    assert 0 <= d["age_ms"] < 2000
    main.bridge._on_message(None, None, SimpleNamespace(
        topic=TELEMETRY, payload=json.dumps(telemetry(int(time.time() * 1000))).encode(), retain=False))
    assert pipeline._telemetry is not None


def test_app_stops_the_pipeline_before_closing_the_store_and_writes_no_other_file(app):
    pipeline, order = app.main.app.state.pipeline, []
    stop, close = pipeline.stop, pipeline.store.close
    pipeline.stop = lambda: (order.append("pipeline stopped"), stop())
    pipeline.store.close = lambda: (order.append("store closed"), close())
    app.client.__exit__(None, None, None)
    assert order == ["pipeline stopped", "store closed"]
    assert not any(t.is_alive() for t in pipeline._threads) and pipeline.on_message not in app.main.bridge.hooks
    assert {p.name for p in app.data.iterdir()} <= DB_FILES and list(app.tmp.iterdir()) == []


def test_pipeline_health_is_served_read_only_and_scalar(app):
    body = app.client.get("/api/pipeline").json()
    assert body["running"] is True and body["extraction"] == "no geometry"
    assert body["missing"] == ["geometry", "camera model", "printer model"]
    assert body["models"] == {"geometry": None, "camera_model": None, "printer_model": None}
    assert body["clock_offset_ms"] is None and body["clock_alarm"] is None and body["gate_open"] is False
    assert {"sent", "send_refused", "send_failed", "send_uncertain"} <= set(body["stats"])
    assert body["settings"] == {"first_layer_mm": 0.7, "layer_mm": 0.5, "line_width_mm": 1.5, "display_window_s": 15.0}

    def scalars(v):
        return all(scalars(x) for x in v.values()) if isinstance(v, dict) else \
            all(scalars(x) for x in v) if isinstance(v, list) else isinstance(v, (str, int, float, bool, type(None)))
    assert scalars(body)
    assert app.client.post("/api/pipeline", json={}).status_code == 405
    app.main.app.state.pipeline = None
    assert app.client.get("/api/pipeline").json()["running"] is False


def test_slicing_comes_from_the_environment(monkeypatch, caplog):
    from server.pipeline import env_settings
    assert env_settings({}) == {}
    assert env_settings({"LAYER_MM": "0.3", "FIRST_LAYER_MM": "0.4", "LINE_WIDTH_MM": "0.8"}) == \
        {"layer_mm": 0.3, "first_layer_mm": 0.4, "line_width_mm": 0.8}
    with caplog.at_level(logging.WARNING, logger="server.pipeline"):
        assert env_settings({"LAYER_MM": "-1", "LINE_WIDTH_MM": "wide", "FIRST_LAYER_MM": "nan"}) == {}
    assert len(caplog.records) == 3


def test_the_app_uses_the_slicing_from_its_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYER_MM", "0.3")
    monkeypatch.setenv("FIRST_LAYER_MM", "0.4")
    for fixture in fresh_app(tmp_path, monkeypatch):
        assert fixture.main.app.state.pipeline.cfg["layer_mm"] == 0.3
        assert fixture.client.get("/api/pipeline").json()["settings"]["first_layer_mm"] == 0.4


def test_app_logs_print_at_info_whatever_uvicorn_runs_at(app, capsys):
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("server.pipeline").info("LEAM clock offset +140 ms")
    assert "LEAM clock offset +140 ms" in capsys.readouterr().err


def test_typed_targets_above_the_camera_ceiling_are_refused(app, sim):
    camera = StubCameraModel(g=0.0, s=0.0)
    camera.ceiling_mm = lambda: 1.5
    s = sim(auto=False, camera=camera, target=None)
    s.run(20)
    app.main.app.state.pipeline = s.pipeline
    body = app.client.get("/api/control").json()
    assert (body["ceiling_mm"], body["relative"]) == (1.5, False)
    r = app.client.post("/api/control", json={"target_mm": 1.6})
    assert r.status_code == 400
    assert r.json()["error"] == "the camera can't see beads wider than 1.50 mm; set a target of at most 1.50"
    assert app.client.get("/api/control").json()["target_mm"] is None
    assert app.client.post("/api/control", json={"target_mm": 1.5}).json()["target_mm"] == 1.5
    assert app.client.post("/api/control", json={"target_mm": None}).status_code == 200
    camera.ceiling_mm = lambda: 1.45
    estimate = s.pipeline.estimate()["mm"]
    assert estimate > 1.45, "the baseline reads above this ceiling"
    r = app.client.post("/api/control", json={"hold_current": True})
    assert r.status_code == 200 and r.json()["target_mm"] == 1.45, "holding the baseline is allowed, at the ceiling"
    r = app.client.post("/api/control", json={"target_mm": 1.46})
    assert r.json()["error"].endswith("at most 1.45")
    assert app.client.post("/api/control", json={"target_mm": 1.45}).status_code == 200
    assert app.client.get("/api/pipeline").json()["ceiling_mm"] == 1.45
    camera.ceiling_mm = lambda: 1.497
    assert app.client.post("/api/control", json={"target_mm": 1.5}).json()["error"].endswith("at most 1.49")
    assert app.client.post("/api/control", json={"target_mm": 1.49}).status_code == 200, "the offered value is accepted"


@pytest.mark.parametrize("estimate_mm, ceiling, target", [(1.53, 1.5, 1.5), (1.493, 1.497, 1.49), (1.4234, 1.5, 1.423),
                                                         (1.53, None, 1.53)])
def test_hold_current_never_sets_a_target_above_the_camera_ceiling(app, estimate_mm, ceiling, target):
    app.main.app.state.pipeline = SimpleNamespace(
        estimate={"mm": estimate_mm, "sigma_mm": 0.03, "n": 12, "age_ms": 300, "confidence": 0.9},
        ceiling_mm=ceiling, relative=False)
    r = app.client.post("/api/control", json={"hold_current": True})
    assert r.status_code == 200 and r.json()["target_mm"] == target


def test_a_relative_camera_model_allows_only_hold_current(app, sim):
    camera = StubCameraModel(g=0.0, s=0.0)
    camera.relative = True
    s = sim(auto=False, camera=camera, target=None)
    s.run(20)
    app.main.app.state.pipeline = s.pipeline
    assert app.client.get("/api/control").json()["relative"] is True
    r = app.client.post("/api/control", json={"target_mm": 1.2})
    assert r.status_code == 400 and "Hold current width" in r.json()["error"]
    assert app.client.post("/api/control", json={"target_mm": None}).status_code == 200
    assert app.client.post("/api/control", json={"hold_current": True}).status_code == 200
    assert app.client.get("/api/pipeline").json()["relative"] is True


def test_control_view_ages_the_live_pipeline_at_request_time(app, sim):
    s = sim(auto=False)
    s.run(20)
    app.main.app.state.pipeline = s.pipeline
    first = app.client.get("/api/control").json()
    s.clock.t += 2500
    second = app.client.get("/api/control").json()
    assert second["estimate"]["age_ms"] == first["estimate"]["age_ms"] + 2500
    assert second["last_decision"]["age_ms"] == first["last_decision"]["age_ms"] + 2500
    assert second["server_ms"] >= first["server_ms"]


def test_manual_sends_from_the_dashboard_reach_the_pipeline(app, sim, monkeypatch):
    s = sim(auto=True)
    app.main.app.state.pipeline = s.pipeline
    request = {"request_id": "m-1", "correction": {"operation": "speed_override_percent", "value": 90}}
    monkeypatch.setattr(app.main.bridge, "make_request", lambda op, value: request)
    monkeypatch.setattr(app.main.bridge, "send_request", lambda r: {"ok": True, "request": r})
    assert app.client.post("/api/request", json={"operation": "speed_override_percent", "value": 90}).json()["ok"]
    s.pipeline.tick()
    assert s.pipeline.pinned()["speed_override_percent"]["value"] == 90
    monkeypatch.setattr(s.pipeline, "note_manual_send", lambda *a: 1 / 0)
    assert app.client.post("/api/request", json={"raw": json.dumps(request)}).json()["ok"] is True


# a recorded minute of print 2 through the real extractor -------------------------------------------------------------
RECORDED = (1792513740000, 1792514040000)  # 18:29-18:34 local: band 1 (baseline), DEMEX in RUN since 18:27:59


def recorded_events(start, end):
    rx, events = re.compile(rb'"recv_ms": (\d+)'), []
    with open(GOLDEN2 / "telemetry.jsonl", "rb") as f:
        for line in f:
            ms = int(rx.search(line).group(1))
            if ms >= end:
                break
            if ms >= start:
                events.append((ms, TELEMETRY, json.loads(line)["msg"]))
    with open(GOLDEN2 / "ir.jsonl") as f:
        for line in f:
            row = json.loads(line)
            if start <= row["recv_ms"] < end:
                meta = {k: v for k, v in row.items() if k not in ("recv_ms", "topic", "file")}
                jpeg = (GOLDEN2 / row["file"]).read_bytes()
                events.append((row["recv_ms"], row["topic"], {**meta, "jpeg_base64": base64.b64encode(jpeg).decode()}))
    with open(GOLDEN2 / "writer.jsonl") as f:
        for line in f:
            row = json.loads(line)
            if start <= row["recv_ms"] < end:
                events.append((row["recv_ms"], row["topic"], row["msg"]))
    return sorted(events, key=lambda e: e[0])


@pytest.mark.skipif(not (GOLDEN2 / "ir.jsonl").exists() or not GEOMETRY.exists(),
                    reason="needs the print-2 recording and the local geometry model")
def test_recorded_print_gives_estimates_and_advice_and_writes_no_image(tmp_path, monkeypatch):
    from server.geometry import Geometry
    events = recorded_events(*RECORDED)
    monkeypatch.setenv("CONTROL_SEND_ENABLED", "1")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    (tmp_path / "tmp").mkdir()
    clock, bridge, emitted = Clock(RECORDED[0]), FakeBridge(), []
    control = ControlState(tmp_path / "data" / "control.json")
    store = Store(tmp_path / "data" / "prototype.db", clock_ms=clock)
    p = Pipeline(bridge, control, store, geometry=Geometry.load(GEOMETRY), camera_model=StubCameraModel(),
                 printer_model=StubPrinterModel(), clock_ms=clock, emit=emitted.append)
    bridge.hooks.append(p.on_message)
    control.update({"target_mm": 1.5}, stamp=p.target_stamp("typed"))
    writes, real_open = [], builtins.open

    def spy_open(file, mode="r", *args, **kw):
        if any(c in mode for c in "wax+"):
            writes.append(str(file))
        return real_open(file, mode, *args, **kw)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Image.Image, "save", lambda *a, **k: writes.append("Image.save"))
    gate, next_tick = [], RECORDED[0] + 1000
    try:
        for ms, topic, payload in events:
            while ms >= next_tick:
                clock.t = next_tick
                p.tick()
                gate.append(p.gate_open())
                next_tick += 1000
            clock.t = ms
            bridge.deliver(topic, payload, ms)
            if "/preview/ir/" in topic:
                p.process_pending()
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
        store.close()
    frames = sum(1 for e in events if "/preview/ir/" in e[1])
    assert p.stats["frames_done"] == frames > 250 and p.stats["sample_ok"] > 0.5 * frames
    assert 50 <= p.offset_ms <= 500
    assert 0.6 * len(gate) < sum(gate) < len(gate), "DEMEX left RUN briefly in this window; the gate follows it"
    widths = [m["mm"] for m in emitted if m["type"] == "width" and m["mm"] is not None]
    assert len(widths) > 0.8 * frames and all(0.5 < w < 3.0 for w in widths)
    decisions = [m for m in emitted if m["type"] == "decision"]
    assert len(decisions) == (RECORDED[1] - RECORDED[0]) // 1000 - 1
    assert {(d["mode"], d["dry"]) for d in decisions} == {("advise", False)}
    assert any(d["action"] for d in decisions) and bridge.sent == []
    assert writes == [] and holds_image(p) == []
    assert {f.name for f in (tmp_path / "data").iterdir()} <= DB_FILES | {"control.json"}
    assert list((tmp_path / "tmp").iterdir()) == []


def test_env_controller_reads_the_operator_pin_and_ignores_bad_values():
    from server.pipeline import env_controller
    assert env_controller({}) is None
    for bad in ("inf", "nan", "-inf"):
        assert env_controller({"CONTROL_PIN_S": bad, "CONTROL_FLOOR_STEP_FACTOR": bad}) is None
    assert env_controller({"CONTROL_FLOOR_STEP_FACTOR": "2"}) == {"floor_step_factor": 2.0}
    assert env_controller({"CONTROL_FLOOR_STEP_FACTOR": "2.5"}) is None  # outside 1-2: keep the default
    assert env_controller({"CONTROL_PIN_S": "60"}) == {"pin_s": 60.0}
    assert env_controller({"CONTROL_PIN_S": "10"}) is None  # below the floor: keep the default
    assert env_controller({"CONTROL_PIN_S": "soon"}) is None
    assert env_controller({"CONTROL_PIN_S": "60", "CONTROL_MIN_LAYER": "3"}) == {"pin_s": 60.0, "min_layer": 3}
    assert env_controller({"CONTROL_MIN_LAYER": "0"}) is None
    fast = {"CONTROL_PERSIST_S": "5", "CONTROL_PERSIST_ESTIMATES": "3", "CONTROL_KAPPA": "0.7"}
    assert env_controller(fast) == {"persist_s": 5.0, "persist_estimates": 3, "kappa": 0.7}
    for bad in ({"CONTROL_PERSIST_ESTIMATES": "0"}, {"CONTROL_KAPPA": "1.5"}, {"CONTROL_KAPPA": "0"},
                {"CONTROL_PERSIST_S": "-1"}, {"CONTROL_PERSIST_ESTIMATES": "2.5"}):
        assert env_controller(bad) is None, bad


def test_env_settings_reads_the_display_window():
    from server.pipeline import env_settings
    assert env_settings({"DISPLAY_WINDOW_S": "10"}) == {"display_window_s": 10.0}
    assert env_settings({"DISPLAY_WINDOW_S": "0"}) == {} and env_settings({"DISPLAY_WINDOW_S": "x"}) == {}
