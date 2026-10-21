import json
import math
from bisect import bisect_right

import numpy as np
import pytest

from server import control_config
from server.controller import ALL, LEVEL_S, Controller, lead_with
from server.estimator import WidthEstimator
from server.model_stubs import StubCameraModel, StubPrinterModel
from tools.sim.plant import T0_MS, Gateway, Plant, PlantParams, Sensor, SensorParams, SimHarness

NOMINAL_MM_S = 20.0  # the sim has no absolute speed; this maps its speed % to the camera model's mm/s
OPS = ("flow_percent", "speed_override_percent", "nozzle_temperature_c")
WIDENS = {"flow_percent": 1, "speed_override_percent": -1, "nozzle_temperature_c": 1}  # the way that widens the bead


# sim loop: what the pipeline does every second, against the bench ---------------------------------------------------
class Loop:
    def __init__(self, harness, controller, estimator, target_mm, auto=True, layer=10, band=None):
        self.h, self.ctl, self.est, self.band = harness, controller, estimator, band
        self.target, self.auto, self.layer = target_mm, auto, layer
        self.decisions, self.sent, self.history = [], [], []
        self._ids, self._next_id, self._awaiting = {}, 0, None

    def __call__(self, now_ms, samples, readbacks, gate_open):
        plant = self.h.plant
        self.history.append((now_ms, plant.nozzle_c, plant.setpoints["speed_override_percent"]))
        for m in self.h.inbox:
            if m["kind"] == "verdict":
                rid, self._awaiting = self._awaiting, None
                if m["state"] == "rejected":
                    self.ctl.note_result(rid, "rejected", now_ms)
                else:
                    self._ids[m["sequence"]] = rid
            elif m["sender"] != "controller":
                self.ctl.note_external(m["operation"], m["value"], now_ms)
            else:
                self.ctl.note_result(self._ids.get(m["sequence"]), m["state"], now_ms)
        skew_ms = round(self.h.sensor.p.clock_skew_s * 1000)
        for s in samples:
            s = {k: v for k, v in s.items() if k != "true_mm"}
            s["t_dep_ms"] += skew_ms  # deposit time onto the local clock, as the pipeline does
            nozzle, speed = self._at(s["t_dep_ms"])
            self.est.add(s, nozzle, NOMINAL_MM_S * speed / 100)
        estimate = self.est.estimate(now_ms, since_ms=self.ctl.estimate_since())
        auto = self.auto(now_ms) if callable(self.auto) else self.auto
        target = self.target(now_ms, estimate) if callable(self.target) else self.target
        d = self.ctl.step(now_ms, estimate, target, readbacks["setpoints"], {**readbacks["measured"],
                          "extruding": True}, {"layer": self.layer}, gate_open, auto, band=self.band)
        self.decisions.append((d, estimate, gate_open, auto))
        if not (auto and d.action):
            return None
        self._next_id += 1
        self.ctl.note_sent(d.action["operation"], d.action["value"], self._next_id, now_ms)
        self._awaiting = self._next_id
        self.sent.append((now_ms, d.action["operation"], d.action["value"], self._next_id, gate_open))
        return d.action

    def _at(self, t_ms):
        i = bisect_right(self.history, (t_ms, math.inf, math.inf)) - 1
        return self.history[max(i, 0)][1:]


def sim(plant_params=None, sensor_params=None, initial=None, printer="default", camera=None, config=None, target=None,
        seed=0, duration_s=300, events=(), auto=True, gate_closed=(), forced=None, readbacks=True, estimator=None,
        setup=None):
    """The gentle legacy bench: one camera, no transport, no clock skew, results always answered."""
    plant = Plant(plant_params or PlantParams(), initial)
    sensor = Sensor(sensor_params or SensorParams(), seed=seed)
    gateway = Gateway(gate_closed, seed=seed, forced=forced)
    kw = {"readback_period_s": 1.4, "readback_none_prob": 0.05} if readbacks else {}
    harness = SimHarness(plant, sensor, gateway, seed=seed, **kw)
    printer = StubPrinterModel() if printer == "default" else printer
    return _run(harness, printer, StubCameraModel() if camera is None else camera, config, target, auto, duration_s,
                events, setup, estimator)


REALISTIC_CAMERA_SIGMA = 0.03  # what the camera model claims per sample; it does not know the per-view offsets


def realistic(seed=0, printer="matching", camera="matching", config=None, target=None, duration_s=600, events=(),
              auto=True, setup=None, estimator=None, knee_mm=None, drift_px=0.0, tune=None, band=None, **overrides):
    """SimHarness.realistic (print-2 physics, blur floor, glow, three views with +-0.15 mm offsets every 10 s,
    transport, clock skew, lost and failed results); the models match its physics but not the view offsets.
    knee_mm: the IR can't see a bead get wider than this; drift_px: the IR baseline has drifted by this much."""
    harness = SimHarness.realistic(seed=seed, **overrides)
    p, sp = harness.plant.p, harness.sensor.p
    if knee_mm is not None or drift_px:
        _blind_above(harness.sensor, knee_mm, drift_px)
    if printer == "matching":
        printer = StubPrinterModel(melt_width_mm=p.melt_width_mm, flow_exponent=p.flow_exponent)
    if camera == "matching":
        camera = StubCameraModel(k=sp.k_px_per_mm, g=sp.g_px_per_c, floor_px=sp.floor_px, knee_mm=knee_mm,
                                 ramp_mm=0.0 if knee_mm is None else HINGE_RAMP_MM, bead_px_range=(0.3, math.inf),
                                 sigma_mm=REALISTIC_CAMERA_SIGMA)
    return _run(harness, printer, camera, config, target, auto, duration_s, events, setup, estimator, tune, band)


def _blind_above(sensor, knee_mm, drift_px):
    """Wraps the bench's sensor: a bead wider than the knee images like one at the knee, and every reading drifts."""
    capture, p = sensor._capture, sensor.p

    def bead(mm):
        return math.hypot(p.floor_px, p.k_px_per_mm * mm)

    def blind(t_s, plant, camera, group=None, group_offset=0.0):
        s = capture(t_s, plant, camera, group, group_offset)
        if s["width_px"] is not None:
            over = 0.0 if knee_mm is None else bead(s["true_mm"]) - bead(min(s["true_mm"], knee_mm))
            s["width_px"] += drift_px - over
        return s

    sensor._capture = blind


# Print 2 seen by the real camera: blind above 1.50 mm, ~2.5 px/mm; views share the drift instead of own offsets.
HINGE_RAMP_MM = 0.05  # the fitted views' ramps are 0.04-0.10 mm
HINGE = {"knee_mm": 1.5, "k_px_per_mm": 2.5,
         "group_schedule": (("cam1:-90", 0.0, 1), ("cam3:45", 0.0, 3), ("cam0:135", 0.0, 0))}


def _run(harness, printer, camera, config, target, auto, duration_s, events=(), setup=None, estimator=None,
         tune=None, band=None):
    for t, fn in events:
        harness.schedule(t, fn)
    if setup:
        setup(harness)
    target = harness.plant.true_width_mm if target is None else target
    loop = Loop(harness, Controller(printer, config), WidthEstimator(camera, estimator), target, auto, band=band)
    loop.ctl.tune(tune)
    out = harness.run(loop, duration_s)
    out["loop"], out["target"] = loop, target.held.get("mm") if callable(target) else target
    return out


def hold_current(at_s=30):
    """The dashboard's Hold button: from then on the target is the estimate shown at that moment."""
    held = {}

    def target(now_ms, estimate):
        if "mm" not in held and now_ms >= T0_MS + at_s * 1000 and estimate and estimate["confidence"] >= 0.5:
            held["mm"] = min(estimate["mm"], estimate.get("ceiling_mm") or math.inf)  # at the ceiling: the ceiling
        return held.get("mm")

    target.held = held
    return target


# scenarios ------------------------------------------------------------------------------------------------------------
HEADROOM = 2.0  # melt capacity (mm at 220 C, 100 %) far above the bead: flow alone can answer a clog


def clog(factor=0.85):
    return lambda plant, gateway: plant.set_disturbance(feed_factor=factor)


def operator(op, value, via_gateway=False):
    """Someone else changes a lever: on the printer directly, or through the gateway (we then see a foreign result)."""
    if via_gateway:
        return lambda plant, gateway: gateway.submit(plant.t, op, value, sender="operator")
    return lambda plant, gateway: plant.external_change(op, value)


def clog_scenario(seed, melt=HEADROOM, printer="matching", events=(), **kw):
    printer = StubPrinterModel(melt_width_mm=melt) if printer == "matching" else printer
    return sim(PlantParams(melt_width_mm=melt), printer=printer, seed=seed, events=[(60, clog()), *events], **kw)


def melt_limited_scenario(seed, **kw):
    return sim(PlantParams(melt_width_mm=1.3), printer=StubPrinterModel(melt_width_mm=1.3), target=1.45, seed=seed,
               **kw)


def glow_scenario(seed, camera=None, **kw):
    return sim(seed=seed, camera=camera, events=[(60, operator("nozzle_temperature_c", 240))], **kw)


def blind_sides(seen_s=20.0, blind_s=10.0):
    """The square of 21 Oct: the trailing camera sees only some of its sides, so the samples stop for blind_s out of
    every seen_s + blind_s (with the bench's own dropouts about half of the ticks have no usable width estimate)."""
    def setup(harness):
        capture, period = harness.sensor._capture, seen_s + blind_s

        def capture_(t_s, plant, camera, group=None, group_offset=0.0):
            s = capture(t_s, plant, camera, group, group_offset)
            if t_s % period >= seen_s:
                s.update(width_px=None, contrast=0.0, confidence=0.0, reason="dropout")
            return s
        harness.sensor._capture = capture_
    return setup


def nozzle_drop(seed, hinge=False, **kw):
    """Trial 1 of 21 Oct on the realistic bench: target 1.45 mm, the operator drops the nozzle 220 -> 200 C at 60 s (the
    melt then carries ~1.27 mm: the walls stop touching below 1.30) and the camera sees half the square. hinge: the
    camera model with a 1.50 mm ceiling; otherwise linear, without one."""
    plant = {"nominal_width_mm": 1.45, "melt_width_mm": 1.9}
    printer = StubPrinterModel(nominal_width_mm=1.45, melt_width_mm=1.9, flow_exponent=0.5)
    camera = HINGE if hinge else {k: v for k, v in HINGE.items() if k != "knee_mm"}
    return realistic(seed, printer=printer, target=1.45, duration_s=kw.pop("duration_s", 420),
                     events=[(60, operator("nozzle_temperature_c", 200))], setup=kw.pop("setup", blind_sides()),
                     **plant, **camera, **kw)


def switching_groups(offsets_px, every_s=10.0, until_s=600.0):
    """The trailing camera follows the travel direction: a different group (with its own unknown offset) every leg."""
    def setup(harness):
        sensor, cams = harness.sensor, sorted(offsets_px)
        sensor.p.camera_offsets_px = dict(offsets_px)
        for i in range(int(until_s / every_s)):
            harness.schedule(i * every_s, lambda p, g, cam=cams[i % len(cams)]: setattr(sensor.p, "cameras", (cam,)))
    return setup


def settle_time(out, t0, band=0.08):
    """Seconds after t0 from which the true width stays within band of target to the end (None if never)."""
    t, err = out["t_s"], out["true_mm"] - out["target"]
    outside = np.nonzero((t >= t0) & (np.abs(err) > band))[0]
    if len(outside) == 0:
        return 0.0
    last = outside[-1]
    return None if last + 1 >= len(t) else t[last + 1] - t0


def reversals(out, t0, band=0.02):
    """Sign changes of the true error after t0, counted only when it crosses from beyond +band to beyond -band."""
    err = (out["true_mm"] - out["target"])[out["t_s"] >= t0]
    sign, count = 0, 0
    for e in err:
        s = 1 if e > band else -1 if e < -band else 0
        if s and sign and s != sign:
            count += 1
        sign = s or sign
    return count


def sent_after(out, t0):
    return [s for s in out["loop"].sent if (s[0] - T0_MS) / 1000 >= t0]


def lever_moves(out, op):
    values = out[{"flow_percent": "flow_percent", "speed_override_percent": "speed_override_percent",
                  "nozzle_temperature_c": "nozzle_target_c"}[op]]
    return values.max() - values.min()


def converged_at(out, t0, band=0.08, hold_s=60.0):
    """First time after t0 from which the true error stays within band for hold_s (None if never)."""
    t, inside = out["t_s"], np.abs(out["true_mm"] - out["target"]) <= band
    start = None
    for ti, ok in zip(t, inside):
        if ti < t0:
            continue
        start = (start if start is not None else ti) if ok else None
        if start is not None and ti - start >= hold_s:
            return start
    return None


def reversals_per_5min(out, t0, band=0.05):
    span = out["t_s"][-1] - t0
    return reversals(out, t0, band) / max(1.0, span / 300)


def safety_violations(out, config=None):
    """Every sent command against bounds, step limit, rounding, spacing, settling, failures, gate and Auto."""
    cfg, problems, loop = control_config.load(config), [], out["loop"]
    by_time = {d.t_ms: (gate, auto) for d, _, gate, auto in loop.decisions}
    # the floor rules' bigger step (D48): only on the tick it was decided, widening a bead narrower than wanted
    floor_at = {d.t_ms for d, *_ in loop.decisions if d.floor and d.error_mm is not None and d.error_mm > 0}
    columns = dict(zip(OPS, ("flow_percent", "speed_override_percent", "nozzle_target_c")))
    confirmed = {int(q) for q, st in zip(out["result_sequence"], out["result_state"]) if st == "setting_confirmed"}
    seq_of = dict(zip(np.round(out["action_t_s"] * 1000).astype(int), out["action_sequence"]))
    # a pair's second half (D53) follows its confirmed first half; the next command waits for both to settle
    second_half = {d.t_ms for d, *_ in loop.decisions if "second half of" in (d.reason or "")}
    previous = None
    for t_ms, op, value, rid, _ in loop.sent:
        t_s = (t_ms - T0_MS) / 1000
        spec = cfg["levers"].get(op)
        if spec is None:
            problems.append(f"{t_s}: operation {op}")
            continue
        gate, auto = by_time[t_ms]
        current = out[columns[op]][np.searchsorted(out["t_s"], t_s)]
        lo, hi = spec["bounds"]
        if not lo <= value <= hi:
            problems.append(f"{t_s}: {op}={value} outside {lo}-{hi}")
        widening = t_ms in floor_at and (value - current) * WIDENS[op] > 0
        if abs(value - current) > spec["max_step"] * (cfg["floor_step_factor"] if widening else 1) + 1e-9:
            problems.append(f"{t_s}: {op} step {current}->{value}")
        if value != round(value):
            problems.append(f"{t_s}: {op}={value} not rounded")
        if not gate or not auto:
            problems.append(f"{t_s}: sent with gate={gate} auto={auto}")
        ok = seq_of.get(round(t_s * 1000)) in confirmed
        # settled = confirmed + effect delay + settle_samples samples at <= 1 Hz; failed = explained by a readback
        until = t_s + (spec["effect_delay_s"] + cfg["settle_samples"] - 1 if ok else cfg["explain_wait_ms"] / 1000)
        if previous:
            p_t, p_op, p_value, p_ok, p_until = previous
            gap = t_s - p_t
            if gap < cfg["min_spacing_ms"] / 1000:
                problems.append(f"{t_s}: spacing {gap:.1f} s")
            if t_ms in second_half:  # its first half must show in the readback (the truth, D53), not only in a result
                if out[columns[p_op]][np.searchsorted(out["t_s"], t_s)] != p_value:
                    problems.append(f"{t_s}: {op} as the second half before {p_op} {p_value} showed")
                until = max(until, p_until)
            elif t_s < p_until:
                problems.append(f"{t_s}: {op} only {gap:.1f} s after {p_op} (needs {p_until - p_t:g} s)")
        previous = (t_s, op, value, ok, until)
    return problems


# unit: holds, limits, bookkeeping ------------------------------------------------------------------------------------
SETPOINTS = {"flow_percent": 100.0, "speed_override_percent": 100.0, "nozzle_temperature_c": 220.0}
MELTY = 1.3  # melt capacity (mm) below the bead: speed and nozzle work, flow doesn't
NOW = {"persist_s": 0, "persist_estimates": 1}  # act on the first estimate off target
BELOW = "alarm: below 1.30 mm (walls may not touch); "


def seen_at(mm, reason):
    """The reason as decided on an estimate of mm: below the minimum width the walls alarm leads it."""
    return BELOW + reason if mm < 1.30 else reason


class Rig:
    """Direct step() calls with synthetic estimates and the production config unless told otherwise; the estimate
    honours estimate_since() like the pipeline does."""

    def __init__(self, printer="default", config=None, target=1.48, auto=True):
        printer = StubPrinterModel() if printer == "default" else printer
        self.c = Controller(printer, config)
        self.t, self.target, self.auto, self.gate = T0_MS, target, auto, True
        self.sp, self.measured, self.context = dict(SETPOINTS), {"nozzle_c": 220.0, "extruding": True}, {"layer": 10}
        self.ceiling = None  # the camera model's ceiling the estimate reports
        self.relative = False  # the estimate is in a relative camera model's units

    def step(self, mm=None, dt_ms=1000, sigma=0.02, n=10, age_ms=500, confidence=0.9, since_ms="pipeline",
             per_group=None, groups_active=1):
        self.t += dt_ms
        since = self.c.estimate_since() if since_ms == "pipeline" else since_ms
        est = None if mm is None else {"mm": mm, "sigma_mm": sigma, "n": n, "age_ms": age_ms, "confidence": confidence,
                                       "per_group": per_group or {"1": {"mm": mm, "sigma_mm": sigma, "n": n}},
                                       "groups_active": groups_active, "since_ms": since, "ceiling_mm": self.ceiling,
                                       "at_ceiling": self.ceiling is not None and mm >= self.ceiling - 0.05,
                                       "relative": self.relative}
        return self.c.step(self.t, est, self.target, dict(self.sp), dict(self.measured), dict(self.context),
                           self.gate, self.auto)

    def act(self, mm, **kw):
        """Step through the persistence window: the first proposal, or the last hold."""
        for _ in range(max(math.ceil(self.c.cfg["persist_s"]) + 1, self.c.cfg["persist_estimates"]) + 2):
            d = self.step(mm, **kw)
            if d.action:
                return d
        return d

    def armed(self, mm):
        self.step(mm)  # the first Auto tick only arms (waits for an estimate newer than the switch)
        return self

    def send(self, d, rid=1):
        self.c.note_sent(d.action["operation"], d.action["value"], rid, self.t)

    def settle(self, d, mm, rid=1):
        """Send d, confirm it by readback, feed estimates until it settles; then the next proposal or hold."""
        self.send(d, rid)
        self.sp[d.action["operation"]] = d.action["value"]
        self.c.note_result(rid, "setting_confirmed", self.t + 500)
        for _ in range(60):
            out = self.step(mm, n=6)
            if not out.reason.startswith("waiting"):
                return out if out.action or out.reason.startswith("alarm") else self.act(mm)
        raise AssertionError(out.reason)


def test_holds_before_anything_is_proposed():
    r = Rig()
    r.target = None
    assert r.step(1.3).reason == "no target"
    assert Rig(printer=None).armed(1.3).step(1.3).reason == "no printer model"
    r = Rig()
    r.sp["nozzle_temperature_c"] = None
    assert r.step(1.3).reason == "no readback for nozzle"
    for key, value, reason in (("extruding", False, "not extruding"), ("extruding", None, "not extruding"),
                               ("layer", 3, "first layers (3 of 6)"), ("layer", None, "first layers (? of 6)")):
        r = Rig().armed(1.3)
        (r.measured if key == "extruding" else r.context)[key] = value
        assert r.step(1.3).reason == reason
    r = Rig().armed(1.3)
    assert r.step(None).reason == "no width estimate"
    assert r.step(1.3, age_ms=6000).reason == "width estimate stale"
    assert r.step(1.3, confidence=0.3).reason == "low confidence (0.30)"
    for d in (r.step(1.3, confidence=0.3), r.step(None)):
        assert d.hold and d.action is None and not d.reason.startswith("alarm:")


class NaNModel(StubPrinterModel):
    def steady_width(self, settings, context=None):
        return math.nan


class BrokenModel(StubPrinterModel):
    def steady_width(self, settings, context=None):
        raise ValueError("bad coefficients")


@pytest.mark.parametrize("change, reason", [
    (dict(mm=math.nan), "non-finite width estimate mm"), (dict(mm=math.inf), "non-finite width estimate mm"),
    (dict(sigma=math.nan), "non-finite width estimate sigma_mm"),
    (dict(confidence=math.nan), "non-finite width estimate confidence"),
    (dict(target=math.nan), "non-finite target"), (dict(flow=math.nan), "non-finite readback for flow"),
    (dict(nozzle_c=math.inf), "non-finite measured nozzle_c")])
def test_non_finite_inputs_hold_and_say_which(change, reason):
    r = Rig(config=NOW).armed(1.30)
    r.target = change.get("target", r.target)
    if "flow" in change:
        r.sp["flow_percent"] = change["flow"]
    if "nozzle_c" in change:
        r.measured["nozzle_c"] = change["nozzle_c"]
    d = r.step(change.get("mm", 1.30), sigma=change.get("sigma", 0.02), confidence=change.get("confidence", 0.9))
    assert d.action is None and d.hold and d.reason == reason
    json.dumps(d.to_dict())


@pytest.mark.parametrize("model, reason", [(NaNModel(), "alarm: printer model returned a non-finite width (nan)"),
                                           (BrokenModel(), "alarm: printer model failed (ValueError: bad coefficients)")])
@pytest.mark.parametrize("anchor", [False, True])
def test_a_failing_printer_model_holds_with_an_alarm(model, reason, anchor):
    r = Rig(model, config={**NOW, "anchor_operating_point": anchor}).armed(1.30)
    for target in (1.48, 1.10):
        r.target = target
        d = r.step(1.30)
        assert d.action is None and d.reason == reason


def test_fallback_mode_with_non_finite_inputs_never_raises():
    r = Rig(None, config={"mode": "fallback", **NOW}).armed(1.30)
    assert r.step(math.nan).reason == "non-finite width estimate mm"
    r.sp["flow_percent"] = math.inf
    assert r.step(1.30).reason == "non-finite readback for flow"
    r.sp["flow_percent"] = 100.0
    assert r.step(1.30).action is not None


def test_readback_older_than_max_age_holds():
    r = Rig().armed(1.48)
    r.sp["speed_override_percent"] = None
    r.step(1.48, dt_ms=3000)
    assert r.step(1.3, dt_ms=3000).reason == "no readback for speed"


def confirming(error, s, n):
    return f"off target {error:+.3f} mm, confirming ({s}/10 s, {n}/5 estimates)"


def test_deadband_two_sigma_and_persistence_in_estimate_time():
    r = Rig().armed(1.48)
    assert (r.c.cfg["persist_s"], r.c.cfg["persist_estimates"]) == (10, 5)
    assert r.step(1.44).reason.startswith("on target")  # inside 0.05 mm
    assert r.step(1.40, sigma=0.05).reason.startswith("on target")  # inside 2 sigma
    reasons = [r.step(1.30).reason for _ in range(10)]
    assert reasons == [confirming(0.18, i, i + 1) for i in range(10)]  # 5 estimates are not yet 10 s
    assert r.step(1.30).action is not None


def test_the_operators_band_is_where_auto_holds_and_it_steers_to_the_target_outside():
    r = Rig(config=NOW, target=1.50).armed(1.50)
    band = (0.10, 0.02)  # zone 1.40-1.52, not centred on the target
    r.c.cfg["sigma_factor"] = 0.0  # this test is about the operator's band, not the noise guard
    for mm in (1.41, 1.50, 1.52):
        d = r.c.step(r.t + 1000, *r_inputs(r, mm), band=band)
        r.t += 1000
        assert d.action is None and d.reason == "on target (inside 1.40-1.52 mm)", mm
    d = r.c.step(r.t + 1000, *r_inputs(r, 1.53), band=band)
    r.t += 1000
    assert d.action is not None and d.error_mm == pytest.approx(-0.03), "just above max: narrow towards the target"
    assert WIDENS[d.action["operation"]] * (d.action["value"] - SETPOINTS[d.action["operation"]]) < 0


def test_the_noise_guard_holds_inside_a_band_tighter_than_the_noise():
    r = Rig(config=NOW, target=1.50).armed(1.50)
    d = r.c.step(r.t + 1000, *r_inputs(r, 1.44, sigma=0.04), band=(0.01, 0.01))
    assert d.action is None and d.reason == "on target (|error| 0.060 <= noise 0.080 mm)"
    d = r.c.step(r.t + 2000, *r_inputs(r, 1.40, sigma=0.04), band=(0.01, 0.01))
    assert d.action is not None, "outside both the band and the noise"


def test_without_a_band_auto_keeps_the_configured_deadband_each_side():
    c = Controller(StubPrinterModel())
    assert c.band_mm(None) == c.band_mm((None, None)) == c.band_mm((-1, math.nan)) == (0.05, 0.05)
    assert c.band_mm((0.0, 0.2)) == (0.0, 0.2)


def test_a_switched_off_lever_is_never_proposed_and_steps_only_get_smaller():
    for off in OPS:
        r = Rig(config=NOW, target=1.50).armed(1.50)
        r.c.tune({off: {"enabled": False}})
        d = r.act(1.30)
        assert (d.action or {}).get("operation") != off and all(c["operation"] != off for c in d.candidates), off
    r = Rig(config=NOW, target=1.50).armed(1.50)
    r.c.tune({"speed_override_percent": {"enabled": False}, "nozzle_temperature_c": {"enabled": False}})
    assert r.act(1.30).action["operation"] == "flow_percent"
    r = Rig(config=NOW, target=1.50).armed(1.50)
    r.c.tune({op: {"max_step": 2} for op in OPS})
    d = r.act(1.10)
    assert abs(d.action["value"] - SETPOINTS[d.action["operation"]]) <= 2
    r.c.tune({"flow_percent": {"max_step": 50}, "speed_override_percent": {"max_step": 0.5}})
    assert (r.c.max_step("flow_percent"), r.c.max_step("speed_override_percent")) == (10, 1), "never larger, at least 1"
    r.c.tune({"nozzle_temperature_c": {"enabled": False, "max_step": 3}})
    lever = r.c.limits()["levers"]["nozzle_temperature_c"]
    assert (lever["max_step"], lever["base_max_step"], lever["enabled"]) == (3, 5, False)
    r.c.tune(None)
    assert r.c.lever_enabled("nozzle_temperature_c") and r.c.max_step("nozzle_temperature_c") == 5


def test_a_smaller_step_does_not_make_a_melt_limited_lever_look_useful():
    # Flow does next to nothing at the melt limit: the default holds, and so must a tuned 1 % or 3 % step.
    for step in (10, 3, 1):
        r = Rig(StubPrinterModel(melt_width_mm=1.2), config=NOW, target=1.22).armed(1.198)
        r.c.tune({"speed_override_percent": {"enabled": False}, "nozzle_temperature_c": {"enabled": False},
                  "flow_percent": {"max_step": step}})
        d = r.act(1.198)
        assert d.action is None and "alarm: no usable lever can move the width (" in d.reason, (step, d.reason)


def test_the_floor_rules_act_inside_a_wide_band():
    r = Rig(config=NOW, target=1.50).armed(1.25)
    d = r.c.step(r.t + 1000, *r_inputs(r, 1.25), band=(0.3, 0.3))
    assert d.action is not None and d.floor and d.reason.startswith("alarm: below 1.30 mm")


def test_all_levers_switched_off_is_an_alarm_not_a_move():
    r = Rig(config=NOW, target=1.50).armed(1.50)
    r.c.tune({op: {"enabled": False} for op in OPS})
    d = r.act(1.30)
    assert d.action is None and d.reason.startswith("alarm: no usable lever")


def r_inputs(r, mm, sigma=0.02):
    """step() arguments after the time, for direct calls with a band."""
    est = {"mm": mm, "sigma_mm": sigma, "n": 10, "age_ms": 500, "confidence": 0.9,
           "per_group": {"1": {"mm": mm, "sigma_mm": sigma, "n": 10}}, "groups_active": 1,
           "since_ms": r.c.estimate_since(), "ceiling_mm": None, "at_ceiling": False, "relative": False}
    return est, r.target, dict(r.sp), dict(r.measured), dict(r.context), r.gate, r.auto


def test_ticks_without_a_usable_estimate_pause_the_persistence_but_never_reset_it():
    # The square of 21 Oct: half the ticks had no usable estimate, so 10 estimates in a row took up to a minute.
    r = Rig().armed(1.48)
    assert [r.step(1.30).reason for _ in range(2)] == [confirming(0.18, 0, 1), confirming(0.18, 1, 2)]
    gaps = [r.step(None), r.step(1.30, confidence=0.2), r.step(1.30, age_ms=6000),
            r.step(1.30, per_group={"1": {"mm": 1.30, "sigma_mm": 0.02, "n": 10}}, groups_active=2)]
    r.measured["extruding"] = False
    gaps.append(r.step(1.30))
    r.measured["extruding"] = True
    r.sp["speed_override_percent"] = None
    gaps.append(r.step(1.30, dt_ms=6000))
    r.sp["speed_override_percent"] = 100.0
    assert [d.reason for d in gaps] == ["no width estimate", "low confidence (0.20)", "width estimate stale",
                                        "waiting for a second view", "not extruding", "no readback for speed"]
    assert r.step(1.30).reason == confirming(0.18, 13, 3)  # past 10 s, but on 3 estimates only
    assert r.step(1.30).reason == confirming(0.18, 14, 4)
    assert r.step(1.30).action is not None


@pytest.mark.parametrize("pause", ["no estimate", "not extruding"])
def test_a_paused_run_off_target_expires_after_persist_gap_s(pause):
    # 4 estimates off target, a pause, then one more: a layer change's blank continues the run, a long stop does not.
    for gap_s, acts in ((20, True), (600, False)):
        r = Rig().armed(1.48)
        for _ in range(4):
            r.step(1.30)
        r.measured["extruding"] = pause != "not extruding"
        for _ in range(gap_s):
            r.step(None if pause == "no estimate" else 1.30)
        r.measured["extruding"] = True
        d = r.step(1.30)
        assert (d.action is not None) is acts, d.reason
        if not acts:
            assert d.reason == confirming(0.18, 0, 1)
    r = Rig().armed(1.48)
    for _ in range(4):
        r.step(1.30)
    assert r.step(1.30, dt_ms=-30_000).reason == confirming(0.18, 0, 1)  # a clock stepped back starts over


def test_an_estimate_on_target_or_off_on_the_other_side_starts_the_persistence_over():
    r = Rig().armed(1.48)
    for _ in range(8):
        r.step(1.30)
    r.step(None)
    assert r.step(1.48).reason.startswith("on target")
    assert r.step(1.30).reason == confirming(0.18, 0, 1)
    r = Rig().armed(1.48)
    for _ in range(8):
        r.step(1.30)
    r.step(None)  # a gap doesn't carry the run over to the other side
    assert [r.step(1.60).reason for _ in range(10)] == [confirming(-0.12, i, i + 1) for i in range(10)]
    d = r.step(1.60)
    assert d.action and d.candidates[0]["predicted_change_mm"] < 0
    r = Rig().armed(1.48)
    for _ in range(8):
        r.step(1.30)
    r.sp["flow_percent"] = 95.0  # someone else's change: what was seen before it says nothing about after it
    assert r.step(1.30).reason.startswith("alarm: readback shows someone else set flow 95 %")
    assert r.step(1.30, dt_ms=30_000).reason == confirming(0.18, 0, 1)


def test_switching_auto_on_confirms_the_deviation_afresh():
    r = Rig(auto=False)
    for _ in range(8):
        r.step(1.30)
    r.auto = True
    assert r.step(1.30).reason == "waiting for a fresh estimate"
    assert r.step(1.30).reason == confirming(0.18, 0, 1)


def test_below_the_minimum_width_the_walls_alarm_leads_and_the_controller_still_acts():
    r = Rig(target=1.45).armed(1.25)
    assert r.step(1.25).reason == BELOW + confirming(0.20, 0, 1)
    d = r.act(1.25)
    assert d.action["operation"] == "flow_percent" and not d.hold  # an alarm on the bead, not a hold
    assert d.reason == f"{BELOW}floor rules: flow 100 % -> {d.action['value']}" and d.floor
    json.dumps(d.to_dict())
    advice = Rig(target=1.45, auto=False).act(1.25)
    assert advice.action == d.action and advice.reason == d.reason and not advice.hold
    r = Rig(target=1.45).armed(1.25)
    r.sp["flow_percent"] = 95.0
    assert r.step(1.25).reason == f"{BELOW}alarm: readback shows someone else set flow 95 %; holding 30 s"
    r = Rig(target=None)
    assert r.step(1.25).reason == f"{BELOW}no target"  # a print without a target still has walls
    r = Rig(target=1.45).armed(1.25)
    assert [r.step(1.25, confidence=0.3).reason, r.step(1.25, age_ms=6000).reason, r.step(None).reason,
            r.step(1.30).reason] == ["low confidence (0.30)", "width estimate stale", "no width estimate",
                                     confirming(0.15, 0, 1)]


def test_the_minimum_width_is_config_within_bounds():
    assert control_config.load()["min_width_mm"] == 1.30
    r = Rig(config={"min_width_mm": 1.20}, target=1.45).armed(1.25)
    assert r.step(1.25).reason == confirming(0.20, 0, 1)
    r = Rig(config={"min_width_mm": 1.40}, target=1.45).armed(1.35)
    assert r.step(1.35).reason.startswith("alarm: below 1.40 mm (walls may not touch); off target")
    for bad in (0.7, 2.1):
        with pytest.raises(ValueError):
            Controller(StubPrinterModel(), {"min_width_mm": bad})


def test_no_walls_alarm_in_a_relative_models_units_or_at_a_ceiling():
    r = Rig(config={"min_width_mm": 1.60}, target=1.45).armed(1.25)
    est = {"mm": 1.25, "sigma_mm": 0.02, "n": 10, "age_ms": 500, "confidence": 0.9}
    assert r.c.walls_alarm(est) == "alarm: below 1.60 mm (walls may not touch)"
    assert r.c.walls_alarm({**est, "relative": True}) is None  # geometric mm of the IR, not the bead
    assert r.c.walls_alarm({**est, "mm": 1.50, "ceiling_mm": 1.50, "at_ceiling": True}) is None  # may be wider


def test_an_alarm_of_the_reasons_own_stays_one_behind_the_walls_alarm():
    walls = "alarm: below 1.30 mm (walls may not touch)"
    assert lead_with(walls, "alarm: no usable lever can move the width") == \
        f"{walls}; alarm: no usable lever can move the width"
    assert lead_with(walls, lead_with(walls, "not sent: gate closed")) == f"{walls}; not sent: gate closed"
    assert lead_with(None, "spacing") == "spacing"


def test_auto_switched_on_waits_for_an_estimate_newer_than_the_switch():
    r = Rig(config=NOW)
    d = r.step(1.30)
    assert d.reason == "waiting for a fresh estimate" and r.c.estimate_since() == r.t
    assert r.step(1.30, since_ms=None).reason == "waiting for a fresh estimate"
    assert r.step(1.30).action is not None


def test_advice_and_auto_decide_the_same_and_only_the_mode_differs():
    a, b = Rig().armed(1.30), Rig(auto=False)
    b.step(1.30)
    da, db = a.act(1.30), b.act(1.30)
    assert (da.mode, db.mode) == ("auto", "advise") and da.action == db.action and not da.hold and not db.hold


def test_gate_closed_holds_in_auto_and_still_advises():
    r = Rig().armed(1.30)
    r.gate = False
    d = r.act(1.30)
    assert d.hold and d.action is None and d.reason.startswith("gate closed (would set flow 100 % -> ")
    assert d.candidates and d.predicted_mm is not None
    r = Rig(auto=False)
    r.gate = False
    d = r.act(1.30)
    assert not d.hold and d.action and d.reason.endswith("(gate closed)")


def test_flow_first_with_increments_and_candidates_contract():
    r = Rig().armed(1.30)
    d = r.act(1.30)
    assert d.action["operation"] == "flow_percent" and d.action["value"] > 100
    assert d.error_mm == pytest.approx(0.18) and d.t_ms == r.t
    assert {c["operation"] for c in d.candidates} == set(OPS)
    for c in d.candidates:
        assert isinstance(c["value"], (int, float)) and isinstance(c["score"], float)
        assert isinstance(c["predicted_change_mm"], float) and all(not isinstance(v, (dict, list)) for v in c.values())
    best = min(d.candidates, key=lambda c: c["score"])
    assert best["operation"] == "flow_percent" and best["predicted_change_mm"] == pytest.approx(0.09, abs=0.02)
    assert d.predicted_mm == pytest.approx(1.30 + best["predicted_change_mm"])
    json.dumps(d.to_dict())


def test_model_bias_cancels_in_increments():
    a, b = Rig().armed(1.30), Rig(StubPrinterModel(bias_mm=0.3)).armed(1.30)
    assert a.act(1.30).action == b.act(1.30).action


def test_melt_limited_flow_loses_on_residual_not_by_rule():
    r = Rig(StubPrinterModel(melt_width_mm=MELTY), target=1.45).armed(1.29)
    d = r.act(1.29)
    by_op = {c["operation"]: c for c in d.candidates}
    assert d.action["operation"] == "speed_override_percent" and d.action["value"] < 100
    assert by_op["flow_percent"]["residual"] > 0.7 > by_op["speed_override_percent"]["residual"]
    r = Rig(StubPrinterModel(melt_width_mm=MELTY), target=1.45).armed(1.29)
    r.c.note_external("speed_override_percent", 100.0, r.t - 60_000)  # pinned, foreign hold already over
    assert r.act(1.29).action["operation"] == "nozzle_temperature_c"


def test_below_the_floor_auto_acts_inside_the_noise_band_and_on_lower_confidence():
    # 0.25 mm short with sigma 0.15 (band 0.30) and confidence 0.47: the cube's infill, its clog dip. Above 1.30 mm: hold.
    above = Rig(target=1.60).armed(1.35)
    assert above.act(1.35, sigma=0.15).reason.startswith("on target")
    assert above.act(1.35, sigma=0.02, confidence=0.47).reason == "low confidence (0.47)"
    below = Rig(target=1.50).armed(1.25)
    d = below.act(1.25, sigma=0.15, confidence=0.47)
    assert d.action and d.floor and d.error_mm > 0 and d.reason.startswith(BELOW + "floor rules: ")
    # still not on noise: under floor_min_confidence, or narrower than the target only above the floor
    assert Rig(target=1.50).armed(1.25).act(1.25, confidence=0.4).reason == "low confidence (0.40)"
    assert not Rig(target=1.20).armed(1.25).act(1.25, sigma=0.15).action


def test_below_the_floor_steps_double_only_when_configured_and_only_there():
    melty = StubPrinterModel(melt_width_mm=MELTY)
    one = Rig(melty, target=1.50).armed(1.10).act(1.10)  # default factor 1: 2x ratcheted the bead wide on noise
    assert one.floor and abs(one.action["value"] - SETPOINTS[one.action["operation"]]) <= 10
    big = Rig(melty, config={"floor_step_factor": 2.0}, target=1.50).armed(1.10).act(1.10)
    assert big.floor and 10 < abs(big.action["value"] - SETPOINTS[big.action["operation"]]) <= 20
    small = Rig(melty, config={"floor_step_factor": 2.0}, target=1.50).armed(1.36).act(1.36)
    assert not small.floor and abs(small.action["value"] - SETPOINTS[small.action["operation"]]) <= 10
    assert Rig(melty, target=1.50).armed(1.10).act(1.10, confidence=0.4).action is None
    assert control_config.limits(control_config.load({"floor_step_factor": 2.0}))["floor_step_factor"] == 2.0


def test_no_floor_rules_for_a_relative_model_or_at_a_ceiling():
    r = Rig(target=1.50)
    est = {"mm": 1.20, "sigma_mm": 0.15, "n": 10, "age_ms": 500, "confidence": 0.47}
    assert r.c.walls_alarm(est) and not r.c.walls_alarm({**est, "relative": True})
    assert not r.c.walls_alarm({**est, "at_ceiling": True}) and not r.c.walls_alarm({**est, "confidence": 0.4})
    # in a control step: 0.25 mm short inside a 0.30 mm band acts only by the floor rules
    assert Rig(target=1.50).armed(1.25).act(1.25, sigma=0.15).floor
    relative = Rig(target=1.50)
    relative.relative = True
    d = relative.armed(1.25).act(1.25, sigma=0.15)
    assert d.action is None and d.reason.startswith("on target") and not relative.c._floor
    ceiling = Rig(target=1.25)
    ceiling.ceiling = 1.28  # reads 1.24 at its ceiling: the bead may be wider
    ceiling.target = 1.28
    d = ceiling.armed(1.24).act(1.24, sigma=0.15)
    assert d.action is None and not d.reason.startswith("alarm: below") and not ceiling.c._floor


def test_the_floor_rules_hold_20_s_after_the_last_dip_while_under_135_mm():
    r = Rig(config=NOW, target=1.45).armed(1.45)
    r.step(1.28)  # a dip below 1.30 mm
    flags = []
    for _ in range(20):
        r.step(1.33)
        flags.append(r.c._floor)
    assert flags == [True] * 19 + [False]  # floor_hold_s after the last estimate below the floor
    r.step(1.28)
    r.step(1.36)
    assert not r.c._floor  # at or above min_width_mm + floor_release_mm: off at once
    r.step(1.33)
    assert r.c._floor  # the hold from the dip still runs
    r.target = 1.46  # a new target: what was decided for the old one goes
    r.step(1.33)
    assert not r.c._floor


def test_the_floor_hold_ends_with_a_settings_change_and_survives_auto_rearmed():
    r = Rig(config=NOW, target=1.45).armed(1.45)
    d = r.step(1.28)
    assert d.action and d.floor
    r.send(d)
    r.sp[d.action["operation"]] = float(d.action["value"])
    r.c.note_result(1, "setting_confirmed", r.t)
    r.step(1.28)  # the readback shows it: beads laid before it say nothing about after it
    assert r.c._floor_until == -math.inf
    r = Rig(config=NOW, target=1.45).armed(1.45)
    r.step(1.28)
    until = r.c._floor_until
    r.auto = False
    r.step(1.33)
    r.auto = True
    r.step(1.33)
    assert r.c._floor_until == until and r.c._floor


def test_rounding_bounds_and_step_limits_hold_for_any_state():
    rng = np.random.default_rng(0)
    cfg = control_config.load()
    for i in range(400):
        mode = "fallback" if i % 4 == 0 else "model"
        r = Rig(None if mode == "fallback" else StubPrinterModel(melt_width_mm=rng.uniform(1.2, 2.2)),
                config={"mode": mode, **NOW}, target=rng.uniform(0.8, 2.4))
        r.sp = {"flow_percent": float(rng.uniform(50, 150)), "speed_override_percent": float(rng.uniform(40, 120)),
                "nozzle_temperature_c": float(rng.uniform(190, 250))}
        r.measured["nozzle_c"] = r.sp["nozzle_temperature_c"] + rng.normal(0, 3)
        mm, target = rng.uniform(0.8, 2.4), r.target
        r.step(mm)
        d = r.step(mm)
        if d.action is None:
            continue
        op, value = d.action["operation"], d.action["value"]
        lo, hi = cfg["levers"][op]["bounds"]
        assert op in control_config.LEVERS and value == round(value) and lo <= value <= hi
        floor = mm < min(cfg["min_width_mm"], target)  # below the walls floor and narrower than wanted
        step = cfg["levers"][op]["max_step"] * (cfg["floor_step_factor"] if floor else 1)
        assert abs(value - r.sp[op]) <= step + 1e-9
        assert mode == "model" or op == "flow_percent"


@pytest.mark.parametrize("bad", [
    {"min_spacing_ms": 1000}, {"max_failures": 0}, {"max_failures": 4}, {"foreign_hold_s": 5}, {"pin_s": 30},
    {"min_confidence": 0.2}, {"settle_samples": 2}, {"levers": {"flow_percent": {"effect_delay_s": -1}}},
    {"levers": {"flow_percent": {"max_stepp": 50}}}, {"levers": {"flow_percent": {"tolerance": 6}}},
    {"levers": {"pressure_advance_seconds": {"bounds": (0, 1), "max_step": 0.1, "cost": 1, "effect_delay_s": 0,
                                            "tolerance": 0.01}}},
    {"levers": {"nozzle_temperature_c": {"bounds": (180, 250)}}}, {"no_such_setting": 1},
    {"mode": "fallback", "levers": {"flow_percent": None}}, {"group_rule": "agree"},
    {"floor_min_confidence": 0.25}, {"floor_min_confidence": 0.55}, {"floor_min_confidence": math.nan},
    {"floor_step_factor": 0.9}, {"floor_step_factor": 2.5}, {"floor_step_factor": math.inf},
    {"floor_hold_s": -1}, {"floor_hold_s": 61}, {"floor_release_mm": -0.01}, {"floor_release_mm": 0.25}])
def test_config_refuses_unsafe_or_unknown_settings(bad):
    with pytest.raises(ValueError):
        Controller(StubPrinterModel(), bad)


def test_config_merges_per_lever_and_limits_are_json_safe():
    limits = Controller(StubPrinterModel()).limits()
    assert limits["levers"]["flow_percent"] == {"min": 70, "max": 130, "max_step": 10, "base_max_step": 10,
                                                "enabled": True}
    assert set(limits["levers"]) == set(OPS)
    assert set(Controller(None, {"mode": "fallback"}).limits()["levers"]) == {"flow_percent"}
    narrower = Controller(StubPrinterModel(), {"levers": {"flow_percent": {"bounds": (90, 110)},
                                                          "nozzle_temperature_c": None}}).limits()["levers"]
    assert narrower == {"flow_percent": {"min": 90, "max": 110, "max_step": 10, "base_max_step": 10, "enabled": True},
                        "speed_override_percent": {"min": 60, "max": 100, "max_step": 10, "base_max_step": 10,
                                                   "enabled": True}}
    json.dumps(limits)


def test_one_command_in_flight_until_its_effect_is_measured():
    r = Rig().armed(1.30)
    d = r.act(1.30)
    r.send(d)
    assert r.step(1.30).reason == f"waiting for flow {d.action['value']} % to be confirmed"
    r.sp["flow_percent"] = d.action["value"]
    r.c.note_result(1, "setting_confirmed", r.t)
    cutoff = r.t + 3000  # result time + flow effect delay
    assert r.step(1.30).reason.endswith("to take effect (0/6 samples)") and r.c.estimate_since() == cutoff
    assert r.step(1.30, since_ms=cutoff, n=8).reason.startswith("waiting")  # now is still before the cutoff
    assert r.step(1.30, since_ms=cutoff - 1, n=8).reason.endswith("(0/6 samples)")
    assert r.step(1.30, since_ms=cutoff, n=5).reason.endswith("(5/6 samples)")
    assert not r.step(1.38, since_ms=cutoff, n=6).reason.startswith("waiting")


def test_settling_waits_for_a_second_view_when_the_print_shows_several():
    r = Rig().armed(1.30)
    d = r.act(1.30)
    r.send(d)
    r.sp["flow_percent"] = d.action["value"]
    r.c.note_result(1, "setting_confirmed", r.t)
    r.step(1.30, dt_ms=4000)
    one = {"1": {"mm": 1.38, "sigma_mm": 0.03, "n": 8}}
    assert r.step(1.38, n=8, per_group=one, groups_active=3).reason.endswith("(8/6 samples, waiting for a second view)")
    two = {**one, "2": {"mm": 1.38, "sigma_mm": 0.03, "n": 3}}
    assert not r.step(1.38, n=11, per_group=two, groups_active=3).reason.startswith("waiting")
    assert r.step(1.30, per_group=one, groups_active=3).reason == "waiting for a second view"
    assert Rig(config={"group_rule": "none", **NOW}).armed(1.3).step(
        1.3, per_group=one, groups_active=3).action is not None


def test_spacing_when_a_proposed_command_was_not_sent():
    r = Rig().armed(1.30)
    assert r.act(1.30).action is not None
    assert r.step(1.30).reason == "spacing"
    assert r.act(1.30).action is not None


def test_external_readback_change_pins_the_lever_and_holds_everything():
    r = Rig(StubPrinterModel(melt_width_mm=MELTY), target=1.45).armed(1.30)
    r.sp["flow_percent"] = 95.0
    d = r.step(1.30)
    assert d.reason.startswith("alarm: readback shows someone else set flow 95 %; holding 30 s")
    assert r.c.pinned(r.t) == {"flow_percent": {"value": 95.0, "until_ms": r.t + 300_000}}
    for _ in range(29):
        assert r.step(1.30).action is None
    d = r.act(1.30)
    assert d.action["operation"] == "speed_override_percent"
    assert "flow_percent" not in {c["operation"] for c in d.candidates}
    assert r.c.pinned(r.t + 300_000) == {}
    r = Rig().armed(1.30)
    r.sp["flow_percent"] = 95.0
    r.step(1.30)
    r.step(1.30, dt_ms=30_000)
    assert r.act(1.30).reason.startswith("alarm: no usable lever can move the width (")  # only flow works here


def test_own_command_is_never_taken_for_an_external_change():
    r = Rig().armed(1.30)
    d = r.act(1.30)
    r.send(d)
    r.step(1.30)
    r.step(1.30)  # readback still lags at 100
    r.sp["flow_percent"] = d.action["value"]
    assert r.step(1.30).reason.startswith("waiting for flow")
    assert r.c.pinned(r.t) == {}


def _two_flow_steps():
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.70).armed(1.30)
    d = r.act(1.30)
    first = d.action["value"]
    d = r.settle(d, 1.39, rid=1)
    assert d.action["operation"] == "flow_percent", d.reason
    second = d.action["value"]
    r.settle(d, 1.48, rid=2)
    return r, first, second


def test_operator_setting_an_older_value_of_ours_is_external_and_never_overridden():
    r, first, second = _two_flow_steps()
    assert r.sp["flow_percent"] == second
    r.sp["flow_percent"] = float(first)  # the operator steps back to our first value, on the printer
    d = r.step(1.48)
    assert d.reason.startswith(f"alarm: readback shows someone else set flow {first} %")
    assert r.c.pinned(r.t)["flow_percent"]["value"] == first
    for _ in range(295):  # the pin lasts 5 min
        d = r.step(1.30)
        assert not (d.action and d.action["operation"] == "flow_percent")


def test_a_second_operator_change_back_to_our_value_is_seen():
    r, first, second = _two_flow_steps()
    r.t += 61_000  # our commands' "may still land" memory is over
    r.sp["flow_percent"] = 105.0
    r.step(1.48)
    r.t += 120_000
    r.sp["flow_percent"] = float(second)
    d = r.step(1.48)
    assert r.c.pinned(r.t)["flow_percent"] == {"value": second, "until_ms": r.t + 300_000}
    assert d.reason.startswith("alarm: readback shows someone else set flow")


def test_advice_applied_by_hand_is_our_change_then_auto_carries_on():
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.48, auto=False)
    d = r.act(1.30)
    assert d.mode == "advise" and d.action["operation"] == "flow_percent"
    r.sp["flow_percent"] = float(d.action["value"])  # the operator applies the advice from the dashboard
    d2 = r.step(1.30)
    assert r.c.pinned(r.t) == {} and not d2.reason.startswith("alarm")
    assert d2.reason.startswith(f"waiting for flow {d.action['value']} % to take effect")
    r.auto = True
    r.step(1.40, n=8)  # arms
    for _ in range(20):
        d3 = r.step(1.40, n=8)
    assert not d3.reason.startswith("alarm") and r.c.pinned(r.t) == {}
    assert r.act(1.30).action["operation"] == "flow_percent"


def test_note_external_pins_its_operation_and_an_unnamed_one_only_holds():
    r = Rig(config=NOW).armed(1.30)
    r.c.note_external("fan_speed_percent", 40.0, r.t)
    assert r.step(1.30).reason == "alarm: another sender set fan_speed_percent 40; holding 29 s"
    assert r.c.pinned(r.t)["fan_speed_percent"]["value"] == 40.0
    assert r.step(1.30, dt_ms=29_000).action is not None
    for lever, value in ((None, None), ("unknown", 5.0), ("flow_percent", math.nan), (None, math.nan)):
        r = Rig(config=NOW).armed(1.30)
        r.c.note_external(lever, value, r.t)
        assert r.step(1.30).reason == "alarm: another sender's command was accepted; holding 29 s"
        assert r.c.pinned(r.t) == {}
        assert r.step(1.30, dt_ms=29_000).action is not None


def test_rejected_command_is_not_resent_until_a_fresh_readback_explains_it():
    r = Rig(config=NOW).armed(1.30)
    d = r.step(1.30)
    r.send(d, rid=7)
    r.c.note_result(7, "rejected", r.t)
    assert r.step(1.30).reason == f"flow {d.action['value']} % rejected; waiting for the readback"
    assert r.step(1.30).action is None
    again = r.step(1.30).action  # 3 s on, the readback (still 100) explains it: a new decision, never the same value
    assert again is not None and again != d.action


def test_failure_is_explained_only_by_a_readback_seen_after_it():
    r = Rig(config=NOW).armed(1.30)
    d = r.step(1.30)
    r.sp["flow_percent"] = None  # readbacks stop before the send
    r.send(d, rid=7)
    r.c.note_result(7, "dispatch_unknown", r.t)
    reasons = [r.step(1.30) for _ in range(10)]
    assert all(d.action is None for d in reasons)  # the readback from before the send explains nothing
    assert reasons[2].reason.endswith("dispatch unknown; waiting for the readback")
    r.sp["flow_percent"] = 100.0
    again = r.step(1.30).action  # a readback taken after the failure: not applied, decide afresh
    assert again is not None and again != d.action


def test_rejected_value_is_forgotten_so_an_operator_setting_it_is_external():
    r = Rig(config=NOW).armed(1.30)
    d = r.step(1.30)
    r.send(d, rid=7)
    r.c.note_result(7, "rejected", r.t)
    for _ in range(4):
        r.step(1.30)
    r.sp["flow_percent"] = float(d.action["value"])  # the operator sets the very value we were refused
    assert r.step(1.30).reason.startswith("alarm: readback shows someone else set flow")
    assert "flow_percent" in r.c.pinned(r.t)


def test_dispatch_unknown_that_lands_later_is_ours_and_gated_from_the_landing():
    r = Rig(config=NOW).armed(1.30)
    d = r.step(1.30)
    r.send(d, rid=7)
    r.c.note_result(7, "dispatch_unknown", r.t + 500)
    r.step(1.30)
    r.step(1.30)
    assert r.c._cmd["state"] == "failed"
    r.sp["flow_percent"] = d.action["value"]  # it did land after all
    assert r.step(1.30).reason.endswith("to take effect (0/6 samples)")
    assert r.c.pinned(r.t) == {} and r.c.estimate_since() == r.t + 3000


def test_a_value_of_ours_landing_after_its_failure_was_explained_is_still_ours():
    r = Rig().armed(1.30)
    d = r.act(1.30)
    r.send(d, rid=7)
    r.c.note_result(7, "dispatch_unknown", r.t)
    for _ in range(4):
        r.step(1.30)  # a fresh readback (still 100) explains the failure and clears it
    assert r.c._cmd is None
    r.sp["flow_percent"] = float(d.action["value"])  # it lands after all, within a minute
    assert r.step(1.30).reason.startswith(f"waiting for flow {d.action['value']} % to take effect")
    assert r.c.pinned(r.t) == {}


def test_an_unsent_auto_proposal_applied_by_hand_is_our_change_not_an_external_one():
    for unsent in (True, False):
        r = Rig(config=NOW).armed(1.30)
        d = r.step(1.30)  # sends disabled, or the pipeline refused it
        if unsent:
            r.c.note_unsent(d.action["operation"], d.action["value"], r.t)
        r.sp["flow_percent"] = float(d.action["value"])  # someone applies it by hand
        reason = r.step(1.30).reason
        if unsent:
            assert reason.startswith(f"waiting for flow {d.action['value']} % to take effect") and r.c.pinned(r.t) == {}
        else:
            assert reason.startswith("alarm: readback shows someone else set flow") and r.c.pinned(r.t)


def test_two_failures_in_a_row_latch_an_alarm_until_auto_is_rearmed():
    for states in (("dispatch_unknown", "queued_unconfirmed"), ("unanswered", "unanswered"), ("rejected", "rejected")):
        r = Rig(config=NOW).armed(1.30)
        for rid, state in enumerate(states, 1):
            for _ in range(10):
                d = r.step(1.30)
                if d.action:
                    break
            r.send(d, rid)
            if state != "unanswered":
                r.c.note_result(rid, state, r.t)
        for _ in range(4):
            d = r.step(1.30)
        assert d.reason.startswith("alarm: 2 commands in a row failed (last: flow ")
        assert d.reason.endswith(f"{states[1].replace('_', ' ')}); switch Auto off and on to re-arm")
        for _ in range(10):
            assert r.step(1.30).reason.startswith("alarm: 2 commands")
        r.auto = False
        assert r.step(1.30).mode == "advise"
        r.auto = True
        r.step(1.30)
        assert r.step(1.30).action is not None


def test_confirmed_but_not_applied_latches_an_alarm_until_auto_is_rearmed():
    r = Rig(config=NOW).armed(1.30)
    d = r.step(1.30)
    r.send(d, rid=1)
    r.c.note_result(1, "setting_confirmed", r.t)
    alarm = (f"alarm: flow {d.action['value']} % confirmed by the gateway but not applied by the printer; "
             "switch Auto off and on to re-arm")
    for _ in range(5):
        r.step(1.30)  # the readback never follows
    for _ in range(120):
        assert r.step(1.30).reason == alarm
    r.auto = False
    assert r.step(1.30).action != d.action  # nor is it advised again
    r.auto = True
    r.step(1.30)
    assert r.step(1.30).action == d.action  # re-armed by the operator: a deliberate new decision


def test_fallback_takes_a_smaller_step_instead_of_retrying_a_failed_value():
    r = Rig(None, config={"mode": "fallback", **NOW}).armed(1.30)
    d = r.step(1.30)
    r.send(d, rid=1)
    r.c.note_result(1, "rejected", r.t)
    for _ in range(3):
        again = r.step(1.30)
    assert again.action == {"operation": "flow_percent", "value": d.action["value"] - 1}


def test_a_late_landing_after_not_applied_is_ours_but_the_alarm_stays():
    r = Rig(config=NOW).armed(1.30)
    d = r.step(1.30)
    r.send(d, rid=7)
    r.c.note_result(7, "setting_confirmed", r.t + 500)
    for _ in range(7):
        r.step(1.30)
    r.sp["flow_percent"] = d.action["value"]
    assert r.step(1.30).reason.endswith("not applied by the printer; switch Auto off and on to re-arm")
    assert r.c.pinned(r.t) == {} and r.c.estimate_since() == r.t + 3000


@pytest.mark.parametrize("printer", ["default", "melty"])
@pytest.mark.parametrize("state", ["setting_confirmed", "rejected", "dispatch_unknown", "queued_unconfirmed",
                                   "unanswered"])
def test_open_loop_where_readbacks_never_follow_never_retries(printer, state):
    r = Rig(StubPrinterModel(melt_width_mm=MELTY) if printer == "melty" else "default").armed(1.30)
    sent = []
    for _ in range(600):
        d = r.step(1.30)
        if d.action:
            sent.append((d.action["operation"], d.action["value"]))
            r.send(d, rid=len(sent))
            if state != "unanswered":
                r.c.note_result(len(sent), state, r.t + 300)
    assert len(sent) == (1 if state == "setting_confirmed" else 2) and len(set(sent)) == len(sent)
    assert d.reason.startswith("alarm: ") and d.reason == r.step(1.30).reason


@pytest.mark.parametrize("state", ["rejected", "dispatch_unknown", "queued_unconfirmed", "unanswered"])
def test_a_failed_value_is_never_proposed_again_until_rearmed_or_a_new_target(state):
    r = Rig(config=NOW).armed(1.30)
    d = r.step(1.30)
    r.send(d, rid=1)
    if state != "unanswered":
        r.c.note_result(1, state, r.t)
    proposals = [r.step(1.30).action for _ in range(30)]
    assert d.action not in proposals and any(proposals)  # other values once the readback explains the failure
    r.target = 1.49
    r.step(1.30)
    r.target = 1.48
    d2 = r.step(1.30, dt_ms=3000)
    assert d2.action == d.action
    r.send(d2, rid=2)  # fails the same way: a second failure in a row
    if state != "unanswered":
        r.c.note_result(2, state, r.t)
    for _ in range(5):
        alarm = r.step(1.30).reason
    assert alarm.startswith("alarm: 2 commands in a row failed")
    r.auto = False
    r.step(1.30)
    r.auto = True
    r.step(1.30)
    assert r.step(1.30).action == d.action


def test_nozzle_change_is_gated_for_its_own_30_s_effect_delay():
    r = Rig(StubPrinterModel(melt_width_mm=MELTY), config={"pair_moves": False}, target=1.45).armed(1.29)
    r.c.note_external("speed_override_percent", 100.0, r.t - 60_000)
    d = r.act(1.29)
    assert d.action["operation"] == "nozzle_temperature_c"
    r.send(d)
    r.sp["nozzle_temperature_c"] = float(d.action["value"])
    r.c.note_result(1, "setting_confirmed", r.t)
    cutoff = r.t + 30_000
    r.step(1.29)
    assert r.c.estimate_since() == cutoff
    for _ in range(25):
        assert r.step(1.35, since_ms=cutoff, n=8).reason.startswith("waiting for nozzle")
    r.step(1.35, dt_ms=6000, since_ms=cutoff, n=8)
    assert not r.step(1.35, since_ms=cutoff, n=8).reason.startswith("waiting")


def test_unreachable_target_takes_one_step_then_alarms():
    for printer, config in ((StubPrinterModel(melt_width_mm=2.0), None), (None, {"mode": "fallback"})):
        r = Rig(printer, config=config, target=2.6).armed(1.5)
        d = r.act(1.5)
        assert d.action and d.reason.startswith("target out of reach (needs +1.10 mm")
        d = r.settle(d, 1.55)
        assert d.action is None and d.reason.startswith("alarm: target out of reach")
        assert r.step(1.55).action is None


def test_a_step_taken_while_the_estimate_still_lagged_does_not_make_the_lever_look_ineffective():
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.60).armed(1.40)
    d = r.act(1.40, sigma=0.005)  # the bead already fell to ~1.30; the rolling estimate still shows 1.40
    d = r.settle(d, 1.33, rid=1)
    assert d.action, d.reason
    d = r.settle(d, 1.43, rid=2)  # +0.10 from 1.33, as the second step predicted
    assert r.c._disabled == {} and "less than" not in d.reason


def test_after_a_step_the_next_needs_its_own_run_off_target_even_across_data_gaps():
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.60).armed(1.40)
    d = r.act(1.40)
    r.send(d)
    r.sp["flow_percent"] = float(d.action["value"])
    r.c.note_result(1, "setting_confirmed", r.t)
    for _ in range(10):
        r.step(None)  # no estimate while the change takes effect
    reasons = [r.step(1.45, n=8).reason for _ in range(5)]
    assert reasons == [confirming(0.15, i, i + 1) for i in range(5)]


def test_lever_with_less_than_30_percent_of_the_predicted_effect_is_dropped():
    r = Rig(StubPrinterModel(melt_width_mm=2.0), target=1.5).armed(1.30)
    assert r.ceiling is None  # a camera model without a ceiling: widening steps are judged like any other
    d = r.act(1.30)
    for i in range(2):
        assert d.action["operation"] == "flow_percent"
        d = r.settle(d, 1.30, rid=i + 1)  # the width did not move at all
    assert d.reason.startswith("alarm: flow moved the width less than 30% of the predicted change")
    assert d.reason.endswith("; switch Auto off and on to re-arm")
    assert all(c["operation"] != "flow_percent" for c in d.candidates)


def test_effective_lever_is_kept():
    r = Rig(StubPrinterModel(melt_width_mm=2.0), target=1.5).armed(1.20)
    d, mm = r.act(1.20), 1.20
    for i in range(3):
        mm += d.candidates[0]["predicted_change_mm"] * 0.8
        d = r.settle(d, mm, rid=i + 1)
        if d.action is None:
            break
    assert "less than" not in d.reason


@pytest.mark.parametrize("gap", ["on target", "two minutes"])
def test_separate_episodes_never_add_up_to_an_ineffective_lever(gap):
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.48).armed(1.30)
    d = r.act(1.30)
    d = r.settle(d, 1.30 + d.candidates[0]["predicted_change_mm"], rid=1)  # worked as predicted
    if gap == "on target":
        for _ in range(30):
            r.step(1.47)
    else:
        r.step(1.40, dt_ms=121_000)
    d = r.act(1.10)  # a new, unrelated disturbance
    assert d.action["operation"] == "flow_percent", d.reason
    d = r.settle(d, 1.10 + d.candidates[0]["predicted_change_mm"], rid=2)
    assert r.c._disabled == {} and "less than" not in d.reason


# the camera's ceiling: blind above its knee ---------------------------------------------------------------------
NO_EFFECT = ("alarm: widening had no visible effect (camera saturated or lever ineffective): check the bead; lower the "
             "target, or switch Auto ")
SATURATED = NO_EFFECT + "off and on to re-arm"
SATURATED_ADVISE = NO_EFFECT + "on and off again to re-arm (Auto on sends commands if sending is enabled)"


def _settled(r, mm, ticks=40, **kw):
    """Feed a settled width long enough to widen from."""
    for _ in range(ticks):
        r.step(mm, **kw)
    return r


def test_a_target_above_the_cameras_ceiling_is_refused_and_the_ceiling_itself_is_allowed():
    r = Rig(target=1.60).armed(1.30)
    r.ceiling = 1.50
    assert r.step(1.30).reason == "alarm: target 1.60 mm is above what the camera can see (1.50 mm)"
    r.target = 1.50
    assert _settled(r, 1.30).act(1.30).action["operation"] == "flow_percent"  # well below the ceiling: as usual


def test_on_target_at_the_ceiling_says_the_width_cannot_be_confirmed():
    r = Rig(target=1.48).armed(1.47)
    r.ceiling = 1.50
    assert r.step(1.47).reason == "at the camera's ceiling (≥ 1.47 mm): can't confirm the width"  # what it reads
    assert r.step(1.52).reason == "at the camera's ceiling (≥ 1.50 mm): can't confirm the width"
    assert r.step(1.44).reason.startswith("on target")


def test_at_the_ceiling_it_never_widens_but_narrows():
    r = Rig(StubPrinterModel(melt_width_mm=2.2), config={"deadband_mm": 0.02}, target=1.50).armed(1.46)
    r.ceiling = 1.50
    assert r.act(1.46, sigma=0.005).reason == "at the camera's ceiling: can't see wider than 1.50 mm"
    r.target = 1.40
    d = r.act(1.50, sigma=0.005)
    assert d.action["operation"] == "flow_percent" and d.candidates[0]["predicted_change_mm"] < 0


@pytest.mark.parametrize("auto", [True, False])
@pytest.mark.parametrize("mm, target", [(1.42, 1.50), (1.20, 1.40)], ids=["near", "far"])
def test_every_widening_step_must_show_its_effect_before_the_next(auto, mm, target):
    # A drifted IR puts the camera's blind plateau anywhere below its ceiling: the check doesn't depend on the level.
    r = _settled(Rig(StubPrinterModel(melt_width_mm=2.2), target=target, auto=auto), mm, sigma=0.005)
    r.ceiling = 1.50
    d = r.act(mm, sigma=0.005)
    assert d.action["operation"] == "flow_percent" and d.candidates[0]["predicted_change_mm"] > 0
    if auto:
        r.send(d)
        r.c.note_result(1, "setting_confirmed", r.t)
    r.sp["flow_percent"] = float(d.action["value"])  # sent, or the advice applied by hand
    out = [r.step(mm, sigma=0.005) for _ in range(90)]  # the reading doesn't follow
    assert not any(o.action for o in out)
    waiting = f"waiting for flow {d.action['value']:g} % to show its effect before widening again"
    assert seen_at(mm, waiting) in {o.reason for o in out}
    latched = SATURATED if auto else SATURATED_ADVISE  # with Auto off, switching it on may send: the text says so
    assert out[-1].reason == seen_at(mm, latched)
    r.target = mm - 0.2
    assert r.act(mm, sigma=0.005).candidates[0]["predicted_change_mm"] < 0  # narrowing stays allowed
    r.target = target
    assert r.act(mm, sigma=0.005).reason == seen_at(mm, latched)  # a new target doesn't clear it
    if auto:
        r.auto = False
        r.step(mm, sigma=0.005)
        r.auto = True
        r.step(mm, sigma=0.005)
        assert r.act(mm, sigma=0.005).action["operation"] == "flow_percent"


def test_a_widening_step_that_shows_its_effect_allows_the_next():
    r = _settled(Rig(StubPrinterModel(melt_width_mm=2.2), target=1.50), 1.30, sigma=0.005)
    r.ceiling = 1.50
    d = r.act(1.30, sigma=0.005)
    mm = 1.30 + d.candidates[0]["predicted_change_mm"] * 0.8
    r.send(d)
    r.c.note_result(1, "setting_confirmed", r.t)
    r.sp["flow_percent"] = float(d.action["value"])
    reasons = [r.step(mm, sigma=0.005).reason for _ in range(60)]
    assert SATURATED not in reasons and r.act(mm, sigma=0.005).action["operation"] == "flow_percent"


def test_widening_waits_for_a_settled_width_and_narrowing_does_not():
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.40)
    r.ceiling = 1.50
    r.measured["nozzle_c"] = 205.0  # the heater still on its way to 220 C
    waiting = seen_at(1.20, "waiting for the width to settle before widening")
    assert [r.step(1.20).reason for _ in range(60)][-1] == waiting
    r.measured["nozzle_c"] = 220.0
    assert [r.step(1.20).reason for _ in range(25)][-1] == waiting
    assert _settled(r, 1.20, ticks=6).act(1.20).action["operation"] == "flow_percent"
    falling = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.40)
    falling.ceiling = 1.50
    assert [falling.step(1.30 - 0.002 * i).reason for i in range(60)][-1] == waiting
    falling.target = 1.00
    assert falling.act(1.18).action["operation"] == "flow_percent"


def test_an_estimate_made_before_a_change_never_counts_as_the_settled_width():
    # The pipeline makes each tick's estimate before step() reads the change in the readback; after the change there
    # are no estimates until beads laid after it have been seen.
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.20)
    r.ceiling = 1.50
    _settled(r, 1.20)
    r.target = 1.40
    r.sp["speed_override_percent"] = 90.0  # someone else's change, in this tick's readback
    r.step(1.10)  # still the beads laid before it
    for _ in range(25):
        r.step(None)
    fresh = 1
    while not (d := r.step(1.20)).action:
        fresh += 1
    assert fresh >= LEVEL_S and d.action["operation"] == "flow_percent"
    assert r.c._proposal["before"][4][ALL][0] == pytest.approx(1.20)


def _blind_step(auto=True, mm=1.30):
    """A settled reading below a 1.50 mm ceiling and the first widening step proposed from it."""
    r = _settled(Rig(StubPrinterModel(melt_width_mm=2.2), target=1.50, auto=auto), mm, sigma=0.005)
    r.ceiling = 1.50
    d = r.act(mm, sigma=0.005)
    assert d.action and d.candidates[0]["predicted_change_mm"] > 0
    return r, d


def test_a_widening_command_that_lands_late_is_judged_like_one_that_landed():
    r, d = _blind_step()
    r.send(d)  # never answered, and the readback keeps the old value past the explain window: given up on
    for _ in range(8):
        r.step(1.30, sigma=0.005)
    assert r.c._cmd is None
    r.sp["flow_percent"] = float(d.action["value"])  # it lands after all
    out = [r.step(1.30, sigma=0.005) for _ in range(120)]  # the reading doesn't follow
    assert not any(o.action for o in out) and out[-1].reason == SATURATED


def test_switching_auto_off_and_on_keeps_a_pending_judgement():
    r, d = _blind_step()
    r.send(d)
    r.c.note_result(1, "setting_confirmed", r.t)
    r.sp["flow_percent"] = float(d.action["value"])
    for _ in range(25):
        r.step(1.30, sigma=0.005)
    assert r.c._widen is not None
    r.auto = False
    r.step(1.30, sigma=0.005)
    r.auto = True
    out = [r.step(1.30, sigma=0.005) for _ in range(60)]
    assert not any(o.action for o in out) and out[-1].reason == SATURATED


def test_a_change_applied_by_hand_cancels_a_pending_judgement():
    r, d = _blind_step(auto=False)
    r.sp["flow_percent"] = float(d.action["value"])  # the operator applies the advice, and the width follows it
    up = 1.30 + d.candidates[0]["predicted_change_mm"]
    for _ in range(12):
        r.step(up, sigma=0.005)
    assert r.c._widen is not None
    r.target = 1.20
    back = r.act(up, sigma=0.005)
    r.sp["flow_percent"] = float(back.action["value"])  # then applies the narrowing advice before it was judged
    down = up + back.candidates[0]["predicted_change_mm"]
    reasons = {r.step(down, sigma=0.005).reason for _ in range(200)}
    r.target = 1.50
    assert SATURATED_ADVISE not in reasons and r.act(down, sigma=0.005).action["operation"] == "flow_percent"


def _pending(**kw):
    r, d = _blind_step(**kw)
    r.send(d)
    r.c.note_result(1, "setting_confirmed", r.t)
    r.sp["flow_percent"] = float(d.action["value"])
    for _ in range(12):
        r.step(1.30, sigma=0.005)
    assert r.c._widen is not None
    return r, f"flow {d.action['value']:g} %"


def test_a_judgement_waits_for_the_heater_and_says_so():
    r, what = _pending()
    r.measured["nozzle_c"] = 210.0  # the heater has drifted off its target: the bead keeps changing
    out = [r.step(1.30, sigma=0.005) for _ in range(240)]
    assert out[-1].reason == f"waiting for the heater to settle before judging {what}" and r.c._widen is not None


def test_a_judgement_needs_enough_estimates_even_at_its_deadline():
    r, what = _pending()
    for _ in range(200):
        r.step(None)
    out = [r.step(1.30, sigma=0.005) for _ in range(11)]  # after so long a gap the run off target starts over
    assert out[-1].reason == f"waiting for enough width estimates to judge {what}"
    out = [r.step(1.30, sigma=0.005) for _ in range(20)]
    assert out[-1].reason == SATURATED


def test_a_view_seen_for_a_short_while_is_less_certain():
    r = Rig()
    for i in range(40):
        views = {"a": {"mm": 1.40, "sigma_mm": 0.02, "n": 10}}
        if i >= 35:
            views["b"] = {"mm": 1.40, "sigma_mm": 0.02, "n": 10}
        r.step(1.40, per_group=views, groups_active=2)
    level = r.c._level(r.t - 40_000)
    assert level["b"][1] > 1.1 * level["a"][1]


def test_without_a_ceiling_widening_goes_like_narrowing_even_while_the_heater_moves():
    # A linear camera model reports no ceiling: no at-ceiling holds, no settled width, heater or response to wait for.
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.70).armed(1.55)
    assert r.ceiling is None
    r.sp["nozzle_temperature_c"] = 200.0  # the operator drops the nozzle; the heater takes a while to follow
    r.measured["nozzle_c"] = 212.0
    changed = r.t + 1000
    assert r.step(1.55).reason.startswith("alarm: readback shows someone else set nozzle 200 °C; holding 30 s")
    r.t += 28_000
    d = r.act(1.55)
    assert d.action and d.candidates and d.predicted_mm > 1.55
    assert r.t - changed <= 30_000 + 11_000  # the foreign hold, then only the persistence
    mm = 1.55 + (d.predicted_mm - 1.55) * 0.8
    reasons = []
    r.send(d)
    r.sp[d.action["operation"]] = float(d.action["value"])
    r.c.note_result(1, "setting_confirmed", r.t)
    for _ in range(20):
        d2 = r.step(mm, n=8)
        if d2.action:
            break
        reasons.append(d2.reason)
    assert d2.action and d2.predicted_mm > mm and r.c._widen is None
    assert not any("ceiling" in why or "settle" in why or "show its effect" in why or "heater" in why
                   for why in reasons)


def test_an_estimate_flagged_at_the_ceiling_without_a_ceiling_is_not_at_one():
    r = Rig(StubPrinterModel(melt_width_mm=2.2), config=NOW, target=1.70).armed(1.55)
    est = {"mm": 1.55, "sigma_mm": 0.02, "n": 10, "age_ms": 500, "confidence": 0.9, "per_group": {},
           "groups_active": 1, "since_ms": r.c.estimate_since(), "ceiling_mm": None, "at_ceiling": True}
    d = r.c.step(r.t + 1000, est, 1.70, dict(r.sp), dict(r.measured), dict(r.context), True, True)
    assert d.action and d.predicted_mm > 1.55


def test_steps_down_from_above_the_knee_show_nothing_at_first_and_keep_the_lever():
    r = Rig(StubPrinterModel(melt_width_mm=2.2), target=1.30).armed(1.50)
    r.ceiling = 1.50
    d = r.act(1.50, sigma=0.005)
    for i in range(3):
        assert d.action and d.candidates[0]["predicted_change_mm"] < 0
        d = r.settle(d, 1.50, rid=i + 1)  # still above the knee: the reading can't move yet
    assert r.c._disabled == {}


def test_fallback_mode_needs_no_model_and_moves_flow_proportionally():
    r = Rig(None, config={"mode": "fallback"}, target=1.5).armed(1.25)
    d = r.act(1.25)
    assert d.action == {"operation": "flow_percent", "value": 110}  # 100 * (1 + 0.5 * 0.25 / 1.25)
    assert d.candidates == [{"operation": "flow_percent", "value": 110, "from": 100.0,
                             "predicted_change_mm": pytest.approx(0.125), "full_step_change_mm": pytest.approx(0.125),
                             "residual": pytest.approx(0.0), "score": 1.0}]


def test_anchoring_evaluates_the_model_where_it_reproduces_the_measured_width():
    # Near the melt corner the model believes flow saturates; a clog moved the real point away from it.
    plain = Rig(StubPrinterModel(), target=1.48).armed(1.27)
    anchored = Rig(StubPrinterModel(), target=1.48, config={"anchor_operating_point": True}).armed(1.27)
    for _ in range(180):
        anchored.step(1.27, sigma=0.5)  # 3 min inside a wide band: the anchor learns measured / modelled width
    assert anchored.c._feed == pytest.approx(1.27 / 1.4804, abs=0.01)

    def gain(d):  # predicted mm per % of flow
        c = next(c for c in d.candidates if c["operation"] == "flow_percent")
        return c["predicted_change_mm"] / (c["value"] - c["from"])
    assert gain(anchored.act(1.27)) > 1.3 * gain(plain.act(1.27))


# closed loop on the gentle bench (tools/sim/plant.py, legacy options) -----------------------------------------------
SEEDS = range(20)


def test_partial_clog_recovers_with_flow():
    runs = [clog_scenario(seed) for seed in SEEDS]
    settle = [settle_time(out, 60) for out in runs]
    assert sum(t is not None and t <= 120 for t in settle) >= 18, settle
    for out in runs:
        assert (out["true_mm"] - out["target"])[out["t_s"] >= 60].max() <= 0.1
        assert len(sent_after(out, 60)) <= 6
        assert lever_moves(out, "nozzle_temperature_c") == 0
        assert safety_violations(out) == []


def test_partial_clog_near_the_melt_limit_with_and_without_anchoring():
    # Without anchoring the model believes flow is near the melt corner and also reaches for speed (see the report).
    plain = [clog_scenario(seed, melt=1.6) for seed in SEEDS]
    anchored = [clog_scenario(seed, melt=1.6, config={"anchor_operating_point": True}) for seed in SEEDS]
    assert all(safety_violations(out) == [] for out in plain + anchored)
    assert all(converged_at(out, 60) is not None for out in anchored)
    assert sum(converged_at(out, 60) is not None for out in plain) >= 18
    assert sum(settle_time(o, 60) is not None and settle_time(o, 60) <= 120 for o in anchored) >= 17
    assert all(s[1] == "flow_percent" for o in anchored for s in o["loop"].sent)
    assert any(s[1] == "speed_override_percent" for o in plain for s in o["loop"].sent)


def test_melt_limited_start_uses_speed_not_flow_to_the_bound():
    for seed in range(10):
        out = melt_limited_scenario(seed)
        assert settle_time(out, 0) <= 150
        assert out["speed_override_percent"].min() < 95
        assert out["flow_percent"].max() <= 110
        assert lever_moves(out, "nozzle_temperature_c") == 0  # last resort, not needed here
        assert safety_violations(out) == []


@pytest.mark.parametrize("seed", range(5))
def test_glow_from_an_external_nozzle_change_is_not_corrected_with_flow(seed):
    out = glow_scenario(seed)
    after = out["t_s"] >= 60
    assert np.abs(out["true_mm"] - out["target"])[after].max() <= 0.08
    assert out["flow_percent"][after].min() >= 94
    assert "nozzle_temperature_c" in out["loop"].ctl.pinned(T0_MS + 100_000)
    assert lever_moves(out, "nozzle_temperature_c") == 20  # only the operator's change
    blind = glow_scenario(seed, camera=StubCameraModel(g=0.0))  # same run with a camera model that ignores glow
    assert (blind["true_mm"] - blind["target"])[after].min() < -0.08  # outside the tolerance held above
    assert safety_violations(out) == safety_violations(blind) == []


@pytest.mark.parametrize("model", [dict(gains={"flow": 1.4, "speed": 1.4, "nozzle": 1.4}),
                                   dict(gains={"flow": 0.6, "speed": 0.6, "nozzle": 0.6}), dict(bias_mm=0.2)],
                         ids=["gains+40%", "gains-40%", "bias+0.2"])
@pytest.mark.parametrize("seed", range(5))
def test_wrong_printer_model_still_converges_without_oscillation(model, seed):
    out = clog_scenario(seed, printer=StubPrinterModel(melt_width_mm=HEADROOM, **model))
    start = converged_at(out, 60)
    assert start is not None and start <= 240
    assert reversals(out, start, 0.05) <= 2
    assert safety_violations(out) == []


@pytest.mark.parametrize("seed", range(5))
def test_camera_bias_moves_the_absolute_width_but_stays_stable(seed):
    out = clog_scenario(seed, camera=StubCameraModel(offset_px=1.4))  # reads 0.2 mm wide
    out["target"] = out["target"] - 0.2
    start = converged_at(out, 60)
    assert start is not None and reversals(out, start, 0.05) <= 2


def test_property_random_disturbances_never_break_limits():
    rng = np.random.default_rng(42)
    for seed in range(50):
        events, gate = [], []
        for _ in range(rng.integers(1, 4)):
            t = float(rng.uniform(20, 280))
            kind = rng.integers(0, 4)
            if kind == 0:
                events.append((t, clog(float(rng.uniform(0.75, 1.2)))))
            elif kind == 1:
                events.append((t, lambda p, g, f=float(rng.uniform(0.7, 1.3)): p.set_disturbance(melt_factor=f)))
            elif kind == 2:
                op = OPS[rng.integers(0, 3)]
                value = {"flow_percent": 90.0, "speed_override_percent": 80.0, "nozzle_temperature_c": 235.0}[op]
                events.append((t, operator(op, value, via_gateway=bool(rng.integers(0, 2)))))
            else:
                gate.append((t, t + float(rng.uniform(5, 40))))
        forced = {int(rng.integers(1, 6)): str(rng.choice(["rejected", "dispatch_unknown", "queued_unconfirmed"]))}
        out = sim(PlantParams(melt_width_mm=float(rng.uniform(1.3, 2.2))),
                  printer=StubPrinterModel(melt_width_mm=float(rng.uniform(1.3, 2.2))),
                  target=float(rng.uniform(1.2, 1.7)), seed=seed, events=events, gate_closed=gate, forced=forced)
        assert safety_violations(out) == [], seed


@pytest.mark.parametrize("seed", range(5))
def test_gate_closed_for_30_s_blocks_actions_and_recovery_follows(seed):
    out = clog_scenario(seed, gate_closed=[(65, 95)])
    sent = [(s[0] - T0_MS) / 1000 for s in out["loop"].sent]
    assert not any(65 <= t < 95 for t in sent)
    assert "command_gate_closed" not in set(out["action_reason"])
    assert any(t >= 95 for t in sent)
    assert converged_at(out, 60) is not None
    assert safety_violations(out) == []


def test_failed_requests_are_never_resent_and_two_in_a_row_alarm():
    out = clog_scenario(0, forced={1: "dispatch_unknown"})
    sent = out["loop"].sent
    assert out["result_state"][0] == "dispatch_unknown" and len(sent) >= 2
    assert (sent[1][0] - sent[0][0]) / 1000 >= 3 + 0.5  # explained by a readback first
    assert safety_violations(out) == []
    out = clog_scenario(0, forced={1: "rejected", 2: "dispatch_unknown"})
    assert len(out["loop"].sent) == 2
    last = out["loop"].decisions[-1]
    assert last[0].reason.startswith(seen_at(last[1]["mm"], "alarm: 2 commands in a row failed"))


def test_nothing_is_sent_with_auto_off():
    out = clog_scenario(0, auto=False)
    assert out["loop"].sent == [] and len(out["action_t_s"]) == 0
    assert any(d.action for d, *_ in out["loop"].decisions)  # it still advises
    off_at = T0_MS + 100_000
    out = clog_scenario(1, auto=lambda now_ms: now_ms < off_at)
    assert all(s[0] < off_at for s in out["loop"].sent)
    assert safety_violations(out) == []


def test_unreachable_target_is_not_chased_to_the_bounds():
    out = sim(PlantParams(melt_width_mm=HEADROOM), printer=StubPrinterModel(melt_width_mm=HEADROOM), target=2.6)
    assert len(out["loop"].sent) <= 1
    assert out["loop"].decisions[-1][0].reason.startswith("alarm: target out of reach")


def test_foreign_sender_holds_auto_and_pins_its_lever():
    out = clog_scenario(0, events=[(150, operator("speed_override_percent", 90, via_gateway=True))])
    sent = [(s[0] - T0_MS) / 1000 for s in out["loop"].sent]
    assert not any(150 <= t < 180 for t in sent)
    assert out["loop"].ctl.pinned(T0_MS + 200_000)["speed_override_percent"]["value"] == 90


def test_fallback_mode_recovers_a_clog_without_a_printer_model():
    out = clog_scenario(0, printer=None, config={"mode": "fallback"})
    assert {s[1] for s in out["loop"].sent} == {"flow_percent"}
    assert converged_at(out, 60) is not None
    assert safety_violations(out, {"mode": "fallback"}) == []


# Model/plant mismatches on the gentle bench; the clog is 10 %, the model knows melt capacity 2.0 and the sensor's
# defaults. "converges": back within 0.08 mm and no oscillation. Otherwise the stated, expected failure is asserted.
GRID = {
    "plant gain x0.5": dict(printer=dict(gains={"flow": 2.0, "speed": 2.0, "nozzle": 2.0}), expect="converges"),
    "plant gain x2": dict(printer=dict(gains={"flow": 0.5, "speed": 0.5, "nozzle": 0.5}), expect="converges"),
    "melt +25%": dict(melt=2.5, expect="converges"),
    "melt -25%": dict(melt=1.5, expect="converges"),
    "group offsets +-0.15 mm every 10 s": dict(setup=switching_groups({0: 1.75, 1: 1.45, 2: 1.6}), expect="converges"),
    "gate closures": dict(gate_closed=[(100, 130), (200, 215)], expect="converges"),
    "failed requests": dict(forced={2: "dispatch_unknown", 4: "rejected"}, expect="converges"),
    # a camera model with the wrong glow term reads the operator's +15 C as 0.12 mm of width: steady, within that
    "glow x0": dict(sensor=dict(g_px_per_c=0.0), events=[(200, operator("nozzle_temperature_c", 235))],
                    expect="biased", bound=0.12 + 0.05),
    "glow x2": dict(sensor=dict(g_px_per_c=0.016), events=[(200, operator("nozzle_temperature_c", 235))],
                    expect="biased", bound=0.12 + 0.05),
    # an unmodelled blur floor compresses narrow beads: never converges, the width is held 0.3-0.45 mm off, steadily
    "blur floor": dict(sensor=dict(floor_px=1.0), expect="off", bound=0.5),
    # flow pinned at 95 % by someone else and nothing else moves the width here: alarm for the 5 min pin, then flow
    "foreign sender": dict(events=[(150, operator("flow_percent", 95, via_gateway=True))], expect="pinned"),
}


@pytest.mark.parametrize("seed", range(2))
@pytest.mark.parametrize("name", list(GRID))
def test_mismatch_grid(name, seed):
    spec = GRID[name]
    out = sim(PlantParams(melt_width_mm=spec.get("melt", HEADROOM)), SensorParams(**spec.get("sensor", {})),
              printer=StubPrinterModel(melt_width_mm=HEADROOM, **spec.get("printer", {})),
              target=Plant(PlantParams(melt_width_mm=HEADROOM)).true_width_mm, seed=seed, duration_s=600,
              events=[(60, clog(0.9)), *spec.get("events", [])], gate_closed=spec.get("gate_closed", ()),
              forced=spec.get("forced"), setup=spec.get("setup"))
    assert safety_violations(out) == []
    err = np.abs(out["true_mm"] - out["target"])[out["t_s"] > 540]
    start = converged_at(out, 60)
    if spec["expect"] == "converges":
        assert start is not None and reversals_per_5min(out, start) <= 2
    elif spec["expect"] in ("biased", "off"):
        assert reversals_per_5min(out, 60) <= 2 and err.max() < spec["bound"]
        assert spec["expect"] == "biased" or (start is None and err.min() > 0.25)
    else:
        pinned = [d.reason for d, *_ in out["loop"].decisions if T0_MS + 181_000 <= d.t_ms < T0_MS + 450_000]
        assert any(r.startswith("alarm: no usable lever can move the width (") for r in pinned)
        assert not any(s[1] == "flow_percent" and T0_MS + 150_000 <= s[0] < T0_MS + 450_000 for s in out["loop"].sent)
        assert converged_at(out, 450) is not None


# the camera's ceiling on the realistic bench, seen through a camera blind above its 1.50 mm knee ---------------
@pytest.mark.parametrize("drift_px", [-0.08, -0.13, -0.18])
def test_a_drifted_ir_never_drives_the_bead_far_past_the_cameras_knee(drift_px):
    # -0.18 px reads all views 0.08 mm low; before the ceiling rule any of these pushed flow to 130 % and the bead to 1.71 mm.
    # At -0.08 px the plateau reads within the knee margin, so it mostly holds at the ceiling instead of latching.
    for seed in range(10):
        out = realistic(seed, target=1.50, drift_px=drift_px, **HINGE)
        reasons = {d.reason for d, *_ in out["loop"].decisions}
        held = any(r.startswith("at the camera's ceiling") for r in reasons)
        assert SATURATED in reasons or (drift_px == -0.08 and held)
        assert len(out["loop"].sent) <= 2 and out["flow_percent"].max() <= 110 and out["true_mm"].max() <= 1.57
        assert safety_violations(out) == []


def test_targets_above_the_ceiling_hold_and_an_over_wide_bead_is_never_widened():
    for seed in range(5):
        above = realistic(seed, target=1.60, **HINGE)
        assert above["loop"].sent == []
        assert any(d.reason == "alarm: target 1.60 mm is above what the camera can see (1.50 mm)"
                   for d, *_ in above["loop"].decisions)
        wide = realistic(seed, target=1.50, initial={"flow_percent": 120}, **HINGE)  # real 1.64, reads at the plateau
        assert wide["flow_percent"].max() <= 120 and wide["true_mm"].max() <= 1.65


def test_a_target_below_the_knee_is_reached_from_above_it():
    for seed in range(5):
        out = realistic(seed, target=1.40, **HINGE)
        assert abs(out["true_mm"][-1] - 1.40) <= 0.05 and out["loop"].ctl._disabled == {}


def test_demo_hold_current_then_a_nozzle_drop_is_restored_to_the_ceilings_margin():
    # the simulator's physics narrows the bead through the melt limit, so the model reaches for speed here, not flow
    outs = [realistic(seed, target=hold_current(), events=[(60, operator("nozzle_temperature_c", 200))], **HINGE)
            for seed in range(20)]
    assert all(o["target"] <= 1.50 and o["true_mm"].max() <= 1.50 + 1e-9 for o in outs)
    assert sum(o["true_mm"][-1] >= 1.45 for o in outs) >= 14
    assert all(safety_violations(o) == [] for o in outs)


# closed loop on SimHarness.realistic (print-2 physics, blur floor, three views, transport, skew, failed results) --------
# LEAM's IR rate before and after 21 Oct ~13:22; on the square frames 0.5 s apart correlated 0.5
@pytest.mark.parametrize("sensor", [{"rate_hz": 1.0}, {"rate_hz": 2.0}, {"rate_hz": 2.0, "noise_tau_s": 0.72}],
                         ids=["1 Hz", "2 Hz", "2 Hz correlated"])
def test_realistic_nozzle_drop_with_half_the_estimates_missing_is_corrected_without_a_ceiling(sensor):
    # Trial 1 of 21 Oct (camera model with a ceiling) took ~2 min to act and one step only; without a ceiling the same
    # disturbance, with about half of the ticks lacking an estimate, is caught as the bead narrows.
    for seed in range(10):
        out = nozzle_drop(seed, **sensor)
        t, w = out["t_s"], out["true_mm"]
        sent = [(s[0] - T0_MS) / 1000 - 60 for s in out["loop"].sent if s[0] >= T0_MS + 60_000]
        ticks = [d for d, *_ in out["loop"].decisions]
        gaps = sum(d.reason.startswith(("no width estimate", "low confidence")) for d in ticks) / len(ticks)
        back = converged_at(out, 60, band=0.05, hold_s=30.0)
        assert 0.3 <= gaps <= 0.65, gaps  # fewer weak estimates at 2 Hz
        assert sent and sent[0] <= 80 and len(sent) >= 3, sent  # ~1.27 mm back to 1.45 takes three steps
        assert np.sum((t >= 60) & (w < 1.30)) * 0.1 <= 20
        assert back is not None and back - 60 <= 180, back
        assert lever_moves(out, "nozzle_temperature_c") == 20  # only the operator's change
        assert safety_violations(out) == []


@pytest.mark.parametrize("rate_hz", [1.0, 2.0])
def test_realistic_do_no_harm(rate_hz):
    runs = [realistic(seed, duration_s=600, rate_hz=rate_hz) for seed in range(20)]
    commands = [len(out["loop"].sent) for out in runs]
    assert np.median(commands) <= 1, commands
    assert sum(abs(out["true_mm"][-1] - out["target"]) <= 0.08 for out in runs) >= 18
    assert all(safety_violations(out) == [] for out in runs)


def test_realistic_clog_recovers():
    runs = [realistic(seed, duration_s=420, events=[(60, clog(0.85))]) for seed in range(20)]
    settle = [settle_time(out, 60, 0.1) for out in runs]
    assert sum(t is not None and t <= 180 for t in settle) >= 18, settle
    assert all(safety_violations(out) == [] for out in runs)


def test_realistic_clog_recovers_with_flow_only_in_small_steps():
    # Test bench tuning: flow only, at most 3 % a command. Slower than the default (median ~100 s) but it gets there.
    tune = {"speed_override_percent": {"enabled": False}, "nozzle_temperature_c": {"enabled": False},
            "flow_percent": {"max_step": 3}}
    runs = [realistic(seed, duration_s=480, events=[(60, clog(0.85))], tune=tune) for seed in range(10)]
    settle = [settle_time(out, 60, 0.1) for out in runs]
    assert sum(t is not None and t <= 300 for t in settle) >= 8, settle
    for out in runs:
        moves = [(d.action, next(c["from"] for c in d.candidates if c["operation"] == d.action["operation"]))
                 for d, *_ in out["loop"].decisions if d.action and not d.hold]
        assert moves and all(a["operation"] == "flow_percent" and abs(a["value"] - frm) <= 3 for a, frm in moves)
        assert safety_violations(out) == []


def test_realistic_random_tuning_and_bands_never_break_the_tuned_limits():
    rng = np.random.default_rng(11)
    for seed in range(12):
        tune = {op: {"enabled": bool(rng.random() > 0.3),
                     "max_step": int(rng.integers(1, control_config.DEFAULTS["levers"][op]["max_step"] + 1))} for op in OPS}
        tune["flow_percent"]["enabled"] = tune["flow_percent"]["enabled"] or not any(t["enabled"] for t in tune.values())
        band = tuple(float(rng.choice([0.0, 0.02, 0.05, 0.1, 0.3])) for _ in range(2))
        factor = float(rng.choice([1.0, 2.0]))
        events = [(float(rng.uniform(30, 200)), clog(float(rng.uniform(0.75, 1.2)))),
                  (float(rng.uniform(200, 380)), lambda p, g, f=float(rng.uniform(0.7, 1.3)): p.set_disturbance(melt_factor=f))]
        out = realistic(seed, duration_s=420, events=events, tune=tune, band=band, target=float(rng.uniform(1.2, 1.7)),
                        melt_width_mm=float(rng.uniform(1.2, 2.8)), config={"floor_step_factor": factor})
        for d, *_ in out["loop"].decisions:
            if not d.action or d.hold:
                continue
            op, value = d.action["operation"], d.action["value"]
            frm = next(c["from"] for c in d.candidates if c["operation"] == op)
            widening = d.floor and d.error_mm > 0 and (value - frm) * WIDENS[op] > 0
            assert tune[op]["enabled"], (seed, op)
            assert abs(value - frm) <= tune[op]["max_step"] * (factor if widening else 1) + 1e-9, (seed, op, frm, value)
        assert safety_violations(out, {"floor_step_factor": factor}) == []


def test_realistic_foreign_sender_holds_and_pins():
    for seed in range(5):
        out = realistic(seed, duration_s=300, events=[(60, clog(0.85)),
                                                      (150, operator("speed_override_percent", 90, via_gateway=True))])
        assert not any(150 <= (s[0] - T0_MS) / 1000 < 180 for s in out["loop"].sent)
        assert out["loop"].ctl.pinned(T0_MS + 200_000)["speed_override_percent"]["value"] == 90
        assert safety_violations(out) == []


def test_realistic_operator_revert_of_our_last_flow_step_is_respected():
    reverted = 0
    for seed in range(20):
        state = {}

        def revert(plant, gateway, state=state):
            loop = state.get("loop")
            flows = [s for s in loop.sent if s[1] == "flow_percent"] if loop else []
            if not state.get("t") and len(flows) >= 2 and plant.setpoints["flow_percent"] == flows[-1][2] and \
                    plant.t * 1000 + T0_MS - flows[-1][0] > 5000:
                plant.external_change("flow_percent", float(flows[-2][2]))
                state["t"], state["value"] = plant.t, flows[-2][2]

        def setup(harness, state=state):
            original = harness.run

            def run(step_fn, duration_s, dt=0.1):
                state["loop"] = step_fn
                return original(step_fn, duration_s, dt)
            harness.run = run
            for t in range(70, 400):
                harness.schedule(t, revert)

        out = realistic(seed, duration_s=500, events=[(60, clog(0.80))], setup=setup)
        if not state.get("t"):
            continue
        reverted += 1
        t_ms = T0_MS + state["t"] * 1000
        assert out["loop"].ctl.pinned(t_ms + 5000)["flow_percent"]["value"] == state["value"]
        assert not any(s[1] == "flow_percent" and t_ms <= s[0] < t_ms + 300_000 for s in out["loop"].sent)
        assert safety_violations(out) == []
    assert reverted >= 5


def test_realistic_property_random_events_never_break_limits():
    rng = np.random.default_rng(7)
    for seed in range(30):
        events, gate = [], []
        for _ in range(rng.integers(1, 5)):
            t, kind = float(rng.uniform(20, 380)), rng.integers(0, 4)
            if kind == 0:
                events.append((t, clog(float(rng.uniform(0.75, 1.2)))))
            elif kind == 1:
                events.append((t, lambda p, g, f=float(rng.uniform(0.7, 1.3)): p.set_disturbance(melt_factor=f)))
            elif kind == 2:
                op = OPS[rng.integers(0, 3)]
                value = {"flow_percent": float(rng.integers(80, 121)), "speed_override_percent":
                         float(rng.integers(70, 101)), "nozzle_temperature_c": float(rng.integers(200, 241))}[op]
                events.append((t, operator(op, value, via_gateway=bool(rng.integers(0, 2)))))
            else:
                gate.append((t, t + float(rng.uniform(5, 40))))
        out = realistic(seed, duration_s=420, events=events, closed_windows=gate, fail_prob=0.1,
                        cancel_on_gate_close=True, melt_width_mm=float(rng.uniform(1.6, 2.8)),
                        target=float(rng.uniform(1.3, 1.7)))
        assert safety_violations(out) == [], seed


def test_operator_settings_apply_over_the_deployment_and_report_its_defaults():
    c = Controller(StubPrinterModel(), {"persist_s": 5.0, "min_width_mm": 1.30})
    c.configure({"min_width_mm": 1.45, "kappa": 0.7, "pin_s": None})
    assert (c.cfg["min_width_mm"], c.cfg["kappa"], c.cfg["persist_s"]) == (1.45, 0.7, 5.0)
    limits = c.limits()
    assert limits["min_width_mm"] == 1.45 and limits["defaults"]["min_width_mm"] == 1.30
    assert limits["defaults"]["persist_s"] == 5.0
    c.configure({"floor_step_factor": 5.0})
    assert c.cfg["min_width_mm"] == 1.45 and c.cfg["floor_step_factor"] != 5.0, "an invalid set changes nothing"
    c.configure({})
    assert (c.cfg["min_width_mm"], c.cfg["kappa"]) == (1.30, control_config.DEFAULTS["kappa"])


KNEE = dict(melt_width_mm=1.5, softness_mm=0.01)  # asked = meltable at 100 % / 220 C: each caps the other


def test_a_pair_moves_two_levers_when_each_alone_is_capped_by_the_other():
    # Live 21 Oct 19:12: speed at its bound, flow +10 % and nozzle +5 C each did ~0.01 mm, together ~0.08 (D53).
    r = Rig(StubPrinterModel(**KNEE), config=NOW, target=1.7).armed(1.5)
    r.c.tune({"speed_override_percent": {"enabled": False}})
    d = r.act(1.5)
    assert d.action["operation"] == "nozzle_temperature_c" and d.then["operation"] == "flow_percent", d.reason
    assert d.predicted_mm - 1.5 > 4 * max(c["predicted_change_mm"] for c in d.candidates), "more than either alone"
    assert d.reason == f"nozzle 220 °C -> {d.action['value']:g} + flow 100 % -> {d.then['value']:g}"
    r.send(d)
    assert r.step(1.5).reason.startswith("waiting for nozzle"), "the second half waits for the first's readback"
    r.sp["nozzle_temperature_c"] = float(d.action["value"])
    r.c.note_result(1, "setting_confirmed", r.t)
    confirmed = r.t
    second = r.step(None)  # decided already: no fresh estimate needed
    assert second.action == d.then and "second half of" in second.reason and not second.hold
    assert second.candidates[0]["from"] == 100.0
    r.c.note_sent("flow_percent", second.action["value"], 2, r.t)
    r.sp["flow_percent"] = float(second.action["value"])
    r.c.note_result(2, "setting_confirmed", r.t)
    assert r.step(1.5).reason.startswith("waiting for flow")
    assert r.c.estimate_since() == confirmed + 30_000, "the pair settles on the slower lever's effect"


def test_a_pair_is_dropped_when_its_first_half_is_not_sent_or_someone_else_moves_a_lever():
    r = Rig(StubPrinterModel(**KNEE), config=NOW, target=1.7).armed(1.5)
    r.c.tune({"speed_override_percent": {"enabled": False}})
    d = r.act(1.5)
    r.c.note_unsent(d.action["operation"], d.action["value"], r.t)  # dry run or refused by the send guard
    assert r.c._next is None
    r = Rig(StubPrinterModel(**KNEE), config=NOW, target=1.7).armed(1.5)
    r.c.tune({"speed_override_percent": {"enabled": False}})
    d = r.act(1.5)
    r.send(d)
    r.c.note_external("flow_percent", 105.0, r.t)
    assert r.c._next is None


def test_advice_shows_a_pair_and_keeps_nothing_to_send():
    r = Rig(StubPrinterModel(**KNEE), config=NOW, target=1.7, auto=False).armed(1.5)
    r.c.tune({"speed_override_percent": {"enabled": False}})
    d = r.act(1.5)
    assert d.mode == "advise" and d.then and " + flow 100 % -> " in d.reason and r.c._next is None


def test_a_lever_near_its_bound_is_spared_for_one_in_the_middle():
    # Speed at 66 % in 60-100 (D53): walking it to 60 % leaves Auto nothing to widen with next time.
    plain = Rig(StubPrinterModel(melt_width_mm=0.9), config={**NOW, "headroom_weight": 0.0}, target=1.5)
    spared = Rig(StubPrinterModel(melt_width_mm=0.9), config=NOW, target=1.5)  # melt-limited at 66 %: flow can't help
    for r in (plain, spared):
        r.sp["speed_override_percent"] = 66.0
        r.armed(1.36)
    assert plain.act(1.36).action["operation"] == "speed_override_percent"
    d = spared.act(1.36)
    assert d.action["value"] >= 62 if d.action["operation"] == "speed_override_percent" else True
    assert {d.action["operation"], (d.then or {}).get("operation")} != {"speed_override_percent", None}, d.reason
