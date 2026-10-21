"""Closed loop without MQTT: the real Pipeline and Bridge, fed from the simulator's realistic bench instead of the broker.

Every simulated second the bench's telemetry, writer status, gateway results and IR samples are delivered through
Bridge._on_message exactly as the broker would, then Pipeline.tick() runs; a request the pipeline publishes goes back
to the bench's gateway. The stub models are legitimate here because the sim is the plant (tools/sim/plant.py). Use
`pytest -s tests/e2e/test_closed_loop.py` for one CLOSED_LOOP summary line per run.
"""
import json
import math
import re
from collections import Counter
from types import SimpleNamespace

import numpy as np
import paho.mqtt.client as mqtt
import pytest

from server import bridge as bridge_module
from server import control_config
from server.bridge import Bridge
from server.control_state import ControlState
from server.model_stubs import StubCameraModel, StubPrinterModel
from server.pipeline import Pipeline
from server.store import Store
from server.width import WidthSample
from tools.sim.fake_gateway import validate
from tools.sim.plant import T0_MS, SimHarness

TELEMETRY = "demex/demo/mini/telemetry"
IR = "demex/demo/mini/preview/ir/trailing"
STATUS = "demex/hackathon/mini/extrusion/status"
RESULT = "demex/hackathon/mini/extrusion/result"
CONTROLLER_ID = "sim-controller"
SESSION_ID = "sess-sim"
BROKER_ENV = ["MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_WRITER_USER", "MQTT_WRITER_PASSWORD",
              "MQTT_CONTROLLER_ID"]
NOMINAL_MM_S = 20.0  # print 2's feed at 100 % override; the sensor's speed term is built on it
DELIVERY_MS = 100  # LEAM publishes this long before the app receives
Z_MM = 7.2  # layer 14: past the controller's first-layers hold
DUMMY_JPEG = "eA=="
COLUMN = {"flow_percent": "flow_percent", "speed_override_percent": "speed_override_percent",
          "nozzle_temperature_c": "nozzle_target_c"}
DEADBAND_MM = 0.05  # the controller's own band; its 2-sigma term widens it to ~0.1 mm on this bench's noise
WITHIN_MM = 0.10  # recovery is asserted "within 0.10 mm" of the target (true width); within 0.05 mm is only reported
RECOVERED_WITHIN_S = 120
# Limits written down here, not read from the controller's config, so a loosened config cannot pass its own test.
ABSOLUTE_BOUNDS = {"flow_percent": (70, 130), "speed_override_percent": (60, 100), "nozzle_temperature_c": (190, 250)}
ABSOLUTE_STEP = {"flow_percent": 10, "speed_override_percent": 10, "nozzle_temperature_c": 5}
ABSOLUTE_SPACING_S = 2.0
WIDENS = {"flow_percent": 1, "speed_override_percent": -1, "nozzle_temperature_c": 1}  # the way that widens the bead
DISTURBED_AT_S = 60
SEEDS = range(5)
DB_FILES = {"prototype.db", "prototype.db-wal", "prototype.db-shm", "control.json"}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return int(self.t)


class FakeWriter:
    """paho writer stand-in: collects what the real Bridge publishes."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)


class SimExtractor:
    """Hands the pipeline the bench's sample for each frame instead of reading an image."""

    def __init__(self):
        self.samples = {}

    def process(self, jpeg, frame_meta, motion):
        s = self.samples.pop(frame_meta["source_frame_id"])
        return WidthSample(
            t_ms=s["t_ms"], camera=s["camera"], heading_deg=None, axis_deg=0.0, width_px=s["width_px"],
            mm_per_px=s["mm_per_px"], contrast=s["contrast"], confidence=s["confidence"], reason=s["reason"],
            recv_ms=frame_meta["recv_ms"], source_frame_id=frame_meta["source_frame_id"], group=s["group"],
            t_dep_ms=s["t_dep_ms"])


class Rig:
    """SimHarness step function: one call per simulated second."""

    def __init__(self, tmp_path, monkeypatch, harness, printer, camera, target, auto=True, send=True, config=None,
                 open_loop=False, gate_signal="both", stamped=True):
        for name in BROKER_ENV:
            monkeypatch.delenv(name, raising=False)
        if send:
            monkeypatch.setenv("CONTROL_SEND_ENABLED", "1")
        else:
            monkeypatch.delenv("CONTROL_SEND_ENABLED", raising=False)
        self.h, self.open_loop, self._frozen, self.gate_signal = harness, open_loop, None, gate_signal
        self.skew_ms = round(harness.sensor.p.clock_skew_s * 1000)
        self.clock = Clock(T0_MS)
        monkeypatch.setattr(bridge_module, "now_ms", self.clock)
        monkeypatch.setattr(bridge_module, "time", SimpleNamespace(time=lambda: self.clock() / 1000,
                                                                   monotonic=lambda: self.clock() / 1000))
        self.bridge = Bridge()
        self.bridge.settings["controller_id"] = CONTROLLER_ID
        self.bridge.writer = FakeWriter()
        self.bridge.status["writer"] = "connected"
        data = tmp_path / "data"
        self.control = ControlState(data / "control.json")
        self.store = Store(data / "prototype.db", clock_ms=self.clock)
        self.extractor = SimExtractor()
        self.emitted = []
        self.pipeline = Pipeline(self.bridge, self.control, self.store, camera_model=camera, printer_model=printer,
                                 controller_config=config, extractor=self.extractor, clock_ms=self.clock,
                                 emit=self.emitted.append)
        self.bridge.hooks.append(self.pipeline.on_message)
        self.control.update({"target_mm": target, "auto": auto},
                            stamp=self.pipeline.target_stamp("typed") if stamped else None)
        self.ticks = []  # (now_ms, decision, gate_open, auto)
        self.pins = []  # (now_ms, pinned levers)
        self.foreign_at = None  # first tick that knew about a foreign gateway result
        self.requests = []  # {"t_ms", "request", "gate", "auto"} in the order published
        self.final = {}  # request_id -> last state the gateway reported
        self._seq_rid, self._pending, self._frames = {}, [], 0

    def close(self):
        self.pipeline.stop()
        self.store.close()

    # delivery -------------------------------------------------------------------------------------------------------
    def deliver(self, topic, payload):
        self.bridge._on_message(None, None, SimpleNamespace(topic=topic, payload=json.dumps(payload).encode(),
                                                            retain=False))

    def telemetry(self, now_ms, readbacks, age_ms, gate_open=True):
        if self.open_loop:  # a replay: the readbacks never follow what we send
            self._frozen = self._frozen or dict(self.h.plant.setpoints)
            readbacks = {"setpoints": self._frozen, "measured": readbacks["measured"]}
        leam = now_ms - self.skew_ms - age_ms
        ns = leam * 1_000_000
        sp, measured = readbacks["setpoints"], readbacks["measured"]
        vel = NOMINAL_MM_S * self.h.plant.setpoints["speed_override_percent"] / 100
        flow = sp["flow_percent"]
        return {"published_utc_ms": leam + 20, "sources": {
            "motion": {"valid": True, "source_utc_ns": ns, "payload": {
                "position_mm": {"x": -vel * (leam / 1000 % 50), "y": 0.0, "z": Z_MM},
                "velocity_mm_s": {"x": -vel, "y": 0.0, "z": 0.0}, "velocity_magnitude": vel,
                "extrusion_active": True, "speed_override_percent": sp["speed_override_percent"]}},
            "duet_readback": {"valid": True, "source_utc_ns": ns, "payload": {
                "extrusion_percent": flow, "extrusion_valid": flow is not None, "extrusion_source_utc_ns": ns,
                "temperatures": {"nozzle": {"valid": True, "current_c": measured["nozzle_c"],
                                            "active_target_c": sp["nozzle_temperature_c"], "state": "active",
                                            "source_utc_ns": ns}}}},
            "gateway": {"valid": True, "source_utc_ns": ns, "payload": {
                "state": "RUN" if gate_open else "SAFE", "permits_outputs": gate_open,
                "command_gate_closed": not gate_open}}}}

    def status(self, now_ms, authorized):
        return {"schema_version": "0.1.0", "controller_id": CONTROLLER_ID, "gateway_session_id": SESSION_ID,
                "normal_authorized": authorized, "published_utc_ms": now_ms, "expires_utc_ms": now_ms + 1500,
                "highest_admitted_sequence": self.h.gateway.sequence}

    def _gateway_mail(self):
        for m in self.h.inbox:
            if m["kind"] == "verdict":
                i = next((i for i, (_, op, v) in enumerate(self._pending)
                          if op == m["operation"] and v == m["value"]), None)
                if i is None:
                    continue
                rid = self._pending.pop(i)[0]
                if m["state"] == "rejected":
                    self.final[rid] = "rejected"
                    self.deliver(RESULT, {"request_id": rid, "state": "rejected", "reason": m.get("reason")})
                else:
                    self._seq_rid[m["sequence"]] = rid
                    self.deliver(RESULT, {"request_id": rid, "state": "accepted"})
            elif m["sender"] == "controller":
                rid = self._seq_rid.get(m["sequence"])
                if rid is not None:
                    self.final[rid] = m["state"]
                    self.deliver(RESULT, {"request_id": rid, "state": m["state"], "observation": m.get("observation")})
            else:
                self.deliver(RESULT, {"request_id": f"{m['sender']}-{m['sequence']}", "state": m["state"],
                                      "observation": m.get("observation")})

    # the step -------------------------------------------------------------------------------------------------------
    def __call__(self, now_ms, samples, readbacks, gate_open):
        self.clock.t = now_ms
        self._gateway_mail()
        in_status, in_telemetry = (gate_open or self.gate_signal == "telemetry", gate_open or self.gate_signal == "status")
        for age in (600, DELIVERY_MS):
            self.deliver(TELEMETRY, self.telemetry(now_ms, readbacks, age, in_telemetry))
        self.deliver(STATUS, self.status(now_ms, in_status))
        for s in samples:
            self._frames += 1
            self.extractor.samples[s["source_frame_id"]] = s
            self.deliver(IR, {"camera_index": s["camera"], "camera_kind": "ir", "role": "trailing",
                              "sequence": self._frames, "source_frame_id": s["source_frame_id"],
                              "source_utc_ns": s["t_ms"] * 1_000_000, "jpeg_base64": DUMMY_JPEG})
            self.pipeline.process_pending()
        before = len(self.bridge.writer.published)
        decision = self.pipeline.tick()
        auto = self.control.public()["auto"]
        self.ticks.append((now_ms, decision, gate_open, auto))
        self.pins.append((now_ms, self.pipeline.pinned()))
        if self.foreign_at is None and self.pipeline.stats["foreign_results"]:
            self.foreign_at = now_ms
        sent = self.bridge.writer.published[before:]
        assert len(sent) <= 1, "the pipeline published more than one request in one tick"
        if not sent:
            return None
        request = sent[0][1]
        correction = request["correction"]
        self.requests.append({"t_ms": now_ms, "request": request, "gate": gate_open, "auto": auto})
        self._pending.append((request["request_id"], correction["operation"], correction["value"]))
        return dict(correction)


def build(tmp_path, monkeypatch, seed=0, events=(), printer="matching", duration_s=420, auto=True, send=True,
          config=None, open_loop=False, gate_signal="both", stamped=True, **overrides):
    harness = SimHarness.realistic(seed=seed, **overrides)
    p, sp = harness.plant.p, harness.sensor.p
    camera = StubCameraModel(g=sp.g_px_per_c, floor_px=sp.floor_px, bead_px_range=(0.3, math.inf), sigma_mm=0.03)
    if printer == "matching":
        printer = StubPrinterModel(melt_width_mm=p.melt_width_mm, flow_exponent=p.flow_exponent)
    for t, fn in events:
        harness.schedule(t, fn)
    target = harness.plant.true_width_mm
    rig = Rig(tmp_path, monkeypatch, harness, printer, camera, target, auto, send, config, open_loop, gate_signal, stamped)
    try:
        out = harness.run(rig, duration_s)
    finally:
        rig.close()
    out["target"] = target
    return out, rig


@pytest.fixture
def run(tmp_path, monkeypatch):
    return lambda **kw: build(tmp_path, monkeypatch, **kw)


# disturbances -----------------------------------------------------------------------------------------------------------
def flow_efficiency(fraction=0.8):
    """Only this share of the commanded flow reaches the bead (a partial clog): the asked width follows flow ** exponent."""
    return lambda plant, gateway: plant.set_disturbance(feed_factor=fraction ** plant.p.flow_exponent)


def melt_limit(factor):
    return lambda plant, gateway: plant.set_disturbance(melt_factor=factor)


def operator(op, value):
    """Another sender changes a lever through the gateway: the app sees a foreign result, then the readback. When our
    request took the gateway's one-per-second slot, the operator sends again a second later."""
    def submit(plant, gateway):
        if gateway.submit(plant.t, op, value, sender="operator").get("reason") == "rate_limited":
            gateway.submit(plant.t + gateway.p.min_interval_s, op, value, sender="operator")
    return submit


# measures -----------------------------------------------------------------------------------------------------------------
WALLS_ALARM = re.compile(r"alarm: below [\d.]+ mm \(walls may not touch\); ")


def own(reason):
    """The reason without the walls alarm in front: a check of the controller's own verdict reads what is behind it."""
    m = WALLS_ALARM.match(reason)
    return reason[m.end():] if m else reason


def error(out):
    return out["true_mm"] - out["target"]


def within(out, t0, band=WITHIN_MM, hold_s=30):
    """Seconds after t0 until the true error enters the band and stays in it for hold_s (None if never)."""
    start = None
    for t, e in zip(out["t_s"], error(out)):
        if t < t0:
            continue
        if abs(e) <= band:
            start = t if start is None else start
            if t - start >= hold_s:
                return start - t0
        else:
            start = None
    return None


def reversals(out, t0, band=DEADBAND_MM):
    """True-error sign changes after t0, counted only from beyond +band to beyond -band or back."""
    sign = count = 0
    for e in error(out)[out["t_s"] >= t0]:
        s = 1 if e > band else -1 if e < -band else 0
        count += bool(s and sign and s != sign)
        sign = s or sign
    return count


def reversals_per_5min(out, t0, band=DEADBAND_MM):
    return reversals(out, t0, band) / max(1.0, (out["t_s"][-1] - t0) / 300)


def safety_problems(out, rig):
    """Every published request: allowed lever, in bounds, one step, whole number, spaced, settled, admissible."""
    cfg, problems, previous, seen = rig.pipeline.controller.cfg, [], None, Counter()
    # the one sanctioned exception to the step limits (D48): a floor-rules decision widening a bead narrower than
    # wanted, sent on the tick it was decided
    floor_at = {t for t, d, *_ in getattr(rig, "ticks", ()) if d.get("floor") and (d.get("error_mm") or 0) > 0}
    # a pair's second half (D53) follows its first once the readback shows it; the next command waits for both
    second_half = {t for t, d, *_ in getattr(rig, "ticks", ()) if "second half of" in (d.get("reason") or "")}
    sequences = [r["request"]["sequence"] for r in rig.requests]
    if sequences != sorted(set(sequences)):
        problems.append(f"sequences not strictly increasing: {sequences}")
    for r in rig.requests:
        req, c = r["request"], r["request"]["correction"]
        op, value, rid = c["operation"], c["value"], req["request_id"]
        t_s, spec = (r["t_ms"] - T0_MS) / 1000, cfg["levers"].get(op)
        seen[rid] += 1
        if op not in control_config.LEVERS or spec is None or set(c) != {"operation", "value"}:
            problems.append(f"{t_s}: operation {op}")
            continue
        now = out[COLUMN[op]][min(np.searchsorted(out["t_s"], t_s), len(out["t_s"]) - 1)]
        if not spec["bounds"][0] <= value <= spec["bounds"][1]:
            problems.append(f"{t_s}: {op}={value} outside {spec['bounds']}")
        if not ABSOLUTE_BOUNDS[op][0] <= value <= ABSOLUTE_BOUNDS[op][1]:
            problems.append(f"{t_s}: {op}={value} outside the absolute limits {ABSOLUTE_BOUNDS[op]}")
        floor = r["t_ms"] in floor_at and (value - now) * WIDENS[op] > 0
        if abs(value - now) > min(spec["max_step"], ABSOLUTE_STEP[op]) * (cfg["floor_step_factor"] if floor else 1) + 1e-9:
            problems.append(f"{t_s}: {op} step {now}->{value}")
        if value != round(value):
            problems.append(f"{t_s}: {op}={value} not rounded")
        if not r["gate"] or not r["auto"]:
            problems.append(f"{t_s}: sent with gate={r['gate']} auto={r['auto']}")
        bad = validate(req, CONTROLLER_ID, SESSION_ID, 0, r["t_ms"])
        if bad:
            problems.append(f"{t_s}: the gateway would refuse it: {bad}")
        state, until = rig.final.get(rid), t_s
        if state == "setting_confirmed":
            until += spec["effect_delay_s"] + cfg["settle_samples"] - 1
        elif state in ("rejected", "dispatch_unknown", "queued_unconfirmed"):
            until += cfg["explain_wait_ms"] / 1000
        if previous:
            p_t, p_op, p_value, p_state, p_until = previous
            gap = t_s - p_t
            spacing = max(cfg["min_spacing_ms"] / 1000, ABSOLUTE_SPACING_S)
            if gap < spacing:
                problems.append(f"{t_s}: {op} only {gap:.1f} s after {p_op} (spacing {spacing:.0f} s)")
            if r["t_ms"] in second_half:
                if out[COLUMN[p_op]][min(np.searchsorted(out["t_s"], t_s), len(out["t_s"]) - 1)] != p_value:
                    problems.append(f"{t_s}: {op} as the second half before {p_op} {p_value} showed")
                until = max(until, p_until)
            elif t_s < p_until:
                problems.append(f"{t_s}: {op} only {gap:.1f} s after {p_op} ({p_state}; needs {p_until - p_t:.0f} s)")
        previous = (t_s, op, value, state, until)
    problems += [f"request {rid} published {n} times" for rid, n in seen.items() if n > 1]
    return problems


def summary(name, seed, out, rig, t0=None):
    t_back = None if t0 is None else within(out, t0)
    t_tight = None if t0 is None else within(out, t0, DEADBAND_MM)
    ops = Counter(r["request"]["correction"]["operation"] for r in rig.requests)
    line = {"scenario": name, "seed": seed, "requests": len(rig.requests), "ops": dict(ops),
            f"within_{WITHIN_MM}_mm_s": None if t_back is None else round(t_back),
            f"within_{DEADBAND_MM}_mm_s": None if t_tight is None else round(t_tight),
            "reversals_per_5min": None if t_back is None else round(reversals_per_5min(out, t0 + t_back), 2),
            "final_error_mm": round(float(error(out)[-1]), 3)}
    print("CLOSED_LOOP", json.dumps(line))
    return line, t_back


def decisions_between(rig, t0_s, t1_s):
    return [d for t, d, *_ in rig.ticks if t0_s <= (t - T0_MS) / 1000 < t1_s]


def sent_between(rig, t0_s, t1_s):
    return [r for r in rig.requests if t0_s <= (r["t_ms"] - T0_MS) / 1000 < t1_s]




def only_scalar_files(tmp_path):
    return {p.name for p in (tmp_path / "data").iterdir()} <= DB_FILES


def recovers(t_back, extra_s=0):
    return t_back is not None and t_back <= RECOVERED_WITHIN_S + extra_s


# scenarios --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rate_hz", [1.0, 2.0])  # LEAM's IR rate before and after 21 Oct ~13:22
@pytest.mark.parametrize("seed", SEEDS)
def test_flow_efficiency_drop_is_recovered_without_oscillation(run, tmp_path, seed, rate_hz):
    out, rig = run(seed=seed, events=[(DISTURBED_AT_S, flow_efficiency(0.8))], rate_hz=rate_hz)
    line, t_back = summary("flow efficiency -20 %", seed, out, rig, DISTURBED_AT_S)
    assert abs(error(out)[out["t_s"] < DISTURBED_AT_S]).max() < 0.02
    assert len(sent_between(rig, 0, DISTURBED_AT_S)) <= 1, "at most one premature move (the views' offsets are unlearned)"
    assert recovers(t_back)
    assert line["reversals_per_5min"] <= 2
    assert {r["request"]["correction"]["operation"] for r in rig.requests} == {"flow_percent"}
    assert out["flow_percent"].max() <= 130 and out["flow_percent"][-1] > 100
    assert safety_problems(out, rig) == []
    assert only_scalar_files(tmp_path)


@pytest.mark.parametrize("seed", SEEDS)
def test_melt_limit_drop_is_recovered_with_speed(run, seed):
    """The printer model is the one fitted at the lowered capacity; a model that doesn't know is the next test."""
    printer = StubPrinterModel(melt_width_mm=2.1 * 0.6, flow_exponent=0.5)
    out, rig = run(seed=seed, printer=printer, events=[(DISTURBED_AT_S, melt_limit(0.6))])
    line, t_back = summary("melt limit -40 %", seed, out, rig, DISTURBED_AT_S)
    assert error(out)[out["t_s"] > DISTURBED_AT_S + 5].min() < -0.2, "the disturbance really narrows the bead"
    assert len(sent_between(rig, 0, DISTURBED_AT_S)) <= 1
    assert recovers(t_back)
    assert line["reversals_per_5min"] <= 2
    assert out["speed_override_percent"].min() < 95, "capacity comes back with slower travel"
    later = out["nozzle_target_c"][out["t_s"] > DISTURBED_AT_S + 15]
    assert later.min() == later.max(), "the nozzle is the last resort"
    assert safety_problems(out, rig) == []


@pytest.mark.parametrize("seed", range(3))
def test_melt_limit_drop_with_an_unaware_model_stays_safe_and_alarms(run, seed):
    out, rig = run(seed=seed, duration_s=600, events=[(DISTURBED_AT_S, melt_limit(0.6))])
    summary("melt limit -40 %, unaware model", seed, out, rig, DISTURBED_AT_S)
    assert out["flow_percent"].max() <= 130 and out["speed_override_percent"].min() >= 60
    assert len(rig.requests) <= 8, "it does not keep pushing a lever that cannot help"
    assert rig.ticks[-1][1]["hold"] and own(rig.ticks[-1][1]["reason"]).startswith("alarm:")
    assert reversals(out, DISTURBED_AT_S) == 0
    assert safety_problems(out, rig) == []


@pytest.mark.parametrize("seed", SEEDS)
def test_foreign_sender_holds_auto_and_pins_its_lever(run, seed):
    out, rig = run(seed=seed, events=[(DISTURBED_AT_S, flow_efficiency(0.8)),
                                      (100, operator("speed_override_percent", 90))])
    line, t_back = summary("foreign sender", seed, out, rig, DISTURBED_AT_S)
    assert rig.foreign_at is not None, "the foreign gateway result reached the controller"
    seen_s = (rig.foreign_at - T0_MS) / 1000
    assert not sent_between(rig, seen_s, seen_s + 30), "all of Auto is held for 30 s after a foreign result"
    assert all(d["hold"] for d in decisions_between(rig, seen_s, seen_s + 30))
    assert any("someone else" in d["reason"] or "another sender" in d["reason"]
               for d in decisions_between(rig, seen_s, seen_s + 40))
    pinned = dict(rig.pins)[T0_MS + 300_000]
    assert pinned["speed_override_percent"]["value"] == 90
    assert out["speed_override_percent"][-1] == 90 and "operator" in set(out["result_sender"])
    assert {r["request"]["correction"]["operation"] for r in rig.requests} == {"flow_percent"}
    assert recovers(t_back, 60), "the 30 s hold, and the estimate refilling after the operator's change"
    assert safety_problems(out, rig) == []


@pytest.mark.parametrize("signal", ["both", "status", "telemetry"], ids=lambda s: f"gate shown in {s}")
@pytest.mark.parametrize("seed", range(3))
def test_gate_closure_blocks_sending_and_recovery_follows(run, seed, signal):
    closed = (70, 140)  # covers the first correction of every seed
    out, rig = run(seed=seed, events=[(DISTURBED_AT_S, flow_efficiency(0.8))], closed_windows=[closed],
                   gate_signal=signal)
    line, t_back = summary("gate closed 70-140 s", seed, out, rig, DISTURBED_AT_S)
    assert not sent_between(rig, *closed)
    assert "command_gate_closed" not in set(out["action_reason"]), "the app shut the gate before the gateway had to"
    assert any(d["reason"].startswith("gate closed") for d in decisions_between(rig, *closed)), \
        "the held correction is reported as waiting for the gate"
    assert all(d["hold"] for d in decisions_between(rig, *closed))
    assert sent_between(rig, closed[1], 1e9), "and goes out once the gate opens"
    assert recovers(t_back, closed[1] - closed[0])
    assert line["reversals_per_5min"] <= 2
    assert safety_problems(out, rig) == []


@pytest.mark.parametrize("seed", range(3))
def test_one_rejected_request_is_not_resent_but_a_fresh_decision_follows(run, seed):
    out, rig = run(seed=seed, events=[(DISTURBED_AT_S, flow_efficiency(0.8))], forced={1: "rejected"})
    line, t_back = summary("first request rejected", seed, out, rig, DISTURBED_AT_S)
    first = rig.requests[0]["request"]["correction"]
    assert rig.final[rig.requests[0]["request"]["request_id"]] == "rejected"
    assert len({r["request"]["request_id"] for r in rig.requests}) == len(rig.requests)
    assert all(r["request"]["correction"] != first for r in rig.requests[1:]), "a failed value is never proposed again"
    assert recovers(t_back, 30)
    assert safety_problems(out, rig) == []


@pytest.mark.parametrize("seed", range(3))
def test_two_failed_requests_in_a_row_latch_an_alarm_and_nothing_more_is_sent(run, seed):
    out, rig = run(seed=seed, events=[(DISTURBED_AT_S, flow_efficiency(0.8))],
                   forced={1: "dispatch_unknown", 2: "rejected"})
    summary("two failures", seed, out, rig, DISTURBED_AT_S)
    assert len(rig.requests) == 2
    last = rig.ticks[-1][1]
    assert last["hold"] and own(last["reason"]).startswith("alarm:")
    assert sent_between(rig, (rig.requests[1]["t_ms"] - T0_MS) / 1000 + 1, 1e9) == []
    assert safety_problems(out, rig) == []


@pytest.mark.parametrize("auto, send", [(False, True), (True, False)], ids=["advice only", "Auto on, sends disabled"])
@pytest.mark.parametrize("seed", range(3))
def test_advice_and_dry_runs_publish_nothing(run, seed, auto, send):
    out, rig = run(seed=seed, auto=auto, send=send, events=[(DISTURBED_AT_S, flow_efficiency(0.8))])
    summary("advice" if not auto else "dry", seed, out, rig)
    assert rig.requests == [] and rig.bridge.writer.published == [] and not rig.bridge.writer_log
    modes = {(d["mode"], d["dry"]) for _, d, *_ in rig.ticks}
    assert modes == ({("advise", False)} if not auto else {("auto", True)})
    advice = [d for _, d, *_ in rig.ticks
              if d["action"] and not d["hold"] and d["t_ms"] >= T0_MS + (DISTURBED_AT_S + 30) * 1000]
    assert advice and all(d["action"]["operation"] == "flow_percent" for d in advice)
    assert all(d["action"]["value"] > 100 for d in advice), "a narrow bead asks for more flow"
    assert all(d["error_mm"] > 0 for d in advice)


@pytest.mark.parametrize("seed", range(3))
def test_open_loop_one_unconfirmed_command_then_a_latched_alarm(run, seed):
    """A replay: the readbacks never follow our command, so the controller must stop after one and say why."""
    out, rig = run(seed=seed, open_loop=True, events=[(DISTURBED_AT_S, flow_efficiency(0.8))])
    summary("open loop", seed, out, rig, DISTURBED_AT_S)
    assert len(rig.requests) == 1, "exactly one command, never a second"
    sent_s = (rig.requests[0]["t_ms"] - T0_MS) / 1000
    after = decisions_between(rig, sent_s + 1, 1e9)
    alarms = [d for d in after if own(d["reason"]).startswith("alarm:")]
    assert alarms and alarms[0]["hold"] and (alarms[0]["t_ms"] - T0_MS) / 1000 - sent_s <= 30
    assert all(d["hold"] for d in after) and own(after[-1]["reason"]).startswith("alarm:")
    assert safety_problems(out, rig) == []


def test_a_target_without_the_apps_stamp_is_never_acted_on(run):
    """A control.json from another camera model (or written by hand) must not steer the printer."""
    out, rig = run(seed=0, stamped=False, events=[(DISTURBED_AT_S, flow_efficiency(0.8))])
    assert rig.requests == []
    held = [d for _, d, *_ in rig.ticks if d["error_mm"] is None or d["hold"]]
    assert len(held) == len(rig.ticks) and own(rig.ticks[-1][1]["reason"]).startswith("alarm:")


def test_the_safety_check_holds_the_written_down_limits_even_against_a_loosened_config():
    loose = control_config.load({"levers": {"flow_percent": {"bounds": (0, 200), "max_step": 100}}})
    t_s = np.arange(0, 100.0)
    out = {"t_s": t_s, "flow_percent": np.full(100, 100.0), "speed_override_percent": np.full(100, 100.0),
           "nozzle_target_c": np.full(100, 220.0)}

    def rig(*values):
        rows = [{"t_ms": T0_MS + 10_000 * (i + 1), "gate": True, "auto": True, "request": {
            "schema_version": "0.2.0", "gateway_session_id": SESSION_ID, "controller_id": CONTROLLER_ID,
            "request_id": f"r{i}", "sequence": i + 1, "issued_utc_ms": T0_MS + 10_000 * (i + 1) - 400,
            "expires_utc_ms": T0_MS + 10_000 * (i + 1) + 1000, "extruder_index": 0,
            "correction": {"operation": "flow_percent", "value": v}}} for i, v in enumerate(values)]
        return SimpleNamespace(pipeline=SimpleNamespace(controller=SimpleNamespace(cfg=loose)), requests=rows, final={})

    assert safety_problems(out, rig(110)) == []
    assert any("absolute limits" in p for p in safety_problems(out, rig(190)))
    assert any("step" in p for p in safety_problems(out, rig(125)))
