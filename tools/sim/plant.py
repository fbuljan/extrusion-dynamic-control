"""Simulated printer, IR sensor and gateway: a deterministic test bench for the width controller.

Width physics: a bead asks for width nominal * (flow / 100) ** flow_exponent (extrusion is tied to travel, so speed
does not enter) but the hotend can melt only so much per second, which as a width is
melt_width * exp(alpha * (T - 220)) * 100 / speed. The bead is a smooth minimum of the two, which is why flow barely
moves width when melt-limited while heat and slower travel do.
Defaults come from print 1 (one wall, ~26.5 mm/s: nominal 1.5, melt 1.6 mm, flow dead above ~125 %); print 2 (two
walls, ~20 mm/s, melt ~2.1 mm) follows flow_exponent 0.5: calliper 1.15/1.35/1.5/1.65/1.70 mm at flow 60-140 %.
Every option defaults to the gentle legacy behaviour; `SimHarness.realistic(...)` turns the hostile ones on (the preset).

Time is in seconds from the start of the run; samples and callbacks carry ms offset by T0_MS (epoch-like, never 0).
Seeds are ints (legacy stream) or SeedSequences; the preset spawns one per component so noise streams never correlate.
"""
from __future__ import annotations

import dataclasses
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

T0_MS = 1_700_000_000_000
BOUNDS = {  # LEAM command guide ranges (same as app/server/bridge.py OPERATIONS)
    "flow_percent": (0.0, 200.0),
    "pressure_advance_seconds": (0.0, 1.0),
    "nozzle_temperature_c": (190.0, 250.0),
    "speed_override_percent": (10.0, 200.0),
    "fan_speed_percent": (0.0, 100.0),
}
REF_LAYER_MM = 0.5  # layer height at which melt_width_mm is specified
REF_C = 220.0
EPS = 1e-9
# Gateway outcomes the controller must survive. *_applied: the Duet took the value although the result says otherwise.
FAULT_STATES = ("rejected", "dispatch_unknown", "dispatch_unknown_applied", "queued_unconfirmed", "lost")
FORCED_STATES = FAULT_STATES + ("setting_confirmed",)
RESULT_STATE = {"dispatch_unknown_applied": "dispatch_unknown"}
APPLIED_STATES = ("setting_confirmed", "dispatch_unknown_applied")


def sim_ms(t_s: float) -> int:
    return T0_MS + round(t_s * 1000)


def stored_value(operation: str, value):
    """What the printer keeps: LEAM rounds speed to whole % and fans to 0.1 %."""
    if operation == "speed_override_percent":
        return round(value)
    if operation == "fan_speed_percent":
        return round(value, 1)
    return value


def _streams(seed, n: int):
    """The main stream (default_rng(seed), the legacy one) plus n independent extra streams for newer options."""
    ss = seed if isinstance(seed, np.random.SeedSequence) else np.random.SeedSequence(seed)
    extra = [np.random.default_rng(np.random.SeedSequence(ss.entropy, spawn_key=ss.spawn_key + (i + 1,)))
             for i in range(n)]
    return np.random.default_rng(seed), extra


def _softmin(a: float, b: float, soft: float) -> float:
    m = min(a, b)
    return m if soft <= 0 else m - soft * math.log1p(math.exp(-abs(a - b) / soft))


def _check_operation(operation, value):
    if operation not in BOUNDS:
        raise ValueError(f"unknown operation {operation!r}")
    low, high = BOUNDS[operation]
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"{operation} must be a number within {low}-{high}, got {value!r}")


@dataclass
class PlantParams:
    nominal_width_mm: float = 1.5    # at flow 100 %
    melt_width_mm: float = 1.6       # melt capacity as a width at 220 C, 100 % speed, reference layer height
    melt_alpha_per_c: float = 0.02
    softness_mm: float = 0.08        # smooth-min width between asked-for and meltable width
    flow_tau_s: float = 3.0          # pressure lag
    heater_tau_s: float = 25.0
    heater_slew_c_per_s: float = 2.0
    layer_height_mm: float = 0.5
    melt_drift_per_min: float = 0.0  # melt capacity grows (+) or shrinks (-) linearly with time, fraction per minute
    flow_exponent: float = 1.0       # asked width ~ flow ** exponent: 1.0 linear, 0.5 as measured on print 2


class Plant:
    def __init__(self, params: PlantParams | None = None, initial: dict | None = None):
        self.p = params or PlantParams()
        self.t = 0.0
        self.setpoints = {"flow_percent": 100.0, "speed_override_percent": 100.0, "nozzle_temperature_c": 220.0,
                          "pressure_advance_seconds": 0.0, "fan_speed_percent": 0.0}
        for operation, value in (initial or {}).items():
            _check_operation(operation, value)
            self.setpoints[operation] = float(value)
        self.flow_eff = self.setpoints["flow_percent"]
        self.nozzle_c = self.setpoints["nozzle_temperature_c"]
        self.melt_factor = 1.0
        self.feed_factor = 1.0
        self.external_log: list[tuple[float, str, float]] = []
        self.before_external = None  # harness hook: our earlier gateway writes land before an operator's change
        self._decay: dict[float, tuple[float, float]] = {}

    def apply(self, operation: str, value: float) -> None:
        _check_operation(operation, value)
        self.setpoints[operation] = float(stored_value(operation, value))

    def external_change(self, operation: str, value: float) -> None:
        """Someone else (the operator) sets a lever; the controller only sees it in the readbacks."""
        if self.before_external:
            self.before_external()
        self.apply(operation, value)
        self.external_log.append((self.t, operation, self.setpoints[operation]))

    def set_disturbance(self, melt_factor: float | None = None, feed_factor: float | None = None) -> None:
        for name, value in (("melt_factor", melt_factor), ("feed_factor", feed_factor)):
            if value is not None:
                if not value > 0:
                    raise ValueError(f"{name} must be > 0")
                setattr(self, name, float(value))

    def step(self, dt: float) -> None:
        if dt not in self._decay:
            self._decay[dt] = (1 - math.exp(-dt / self.p.flow_tau_s), 1 - math.exp(-dt / self.p.heater_tau_s))
        flow_gain, heat_gain = self._decay[dt]
        self.flow_eff += (self.setpoints["flow_percent"] - self.flow_eff) * flow_gain
        slew = self.p.heater_slew_c_per_s * dt
        self.nozzle_c += max(-slew, min(slew, (self.setpoints["nozzle_temperature_c"] - self.nozzle_c) * heat_gain))
        self.t += dt

    @property
    def melt_width_now_mm(self) -> float:
        p = self.p
        drift = max(0.2, 1 + p.melt_drift_per_min * self.t / 60)
        return (p.melt_width_mm * self.melt_factor * drift * math.exp(p.melt_alpha_per_c * (self.nozzle_c - REF_C))
                * 100 / self.setpoints["speed_override_percent"] * REF_LAYER_MM / p.layer_height_mm)

    @property
    def true_width_mm(self) -> float:
        ratio = max(self.flow_eff, 0.0) / 100
        if self.p.flow_exponent != 1.0:
            ratio **= self.p.flow_exponent
        return _softmin(self.p.nominal_width_mm * ratio * self.feed_factor, self.melt_width_now_mm, self.p.softness_mm)

    def readbacks(self) -> dict:
        """What the controller may read: commanded levers as the gateway confirmed them, actual nozzle temperature."""
        return {"setpoints": dict(self.setpoints), "measured": {"nozzle_c": self.nozzle_c}}


@dataclass
class SensorParams:
    rate_hz: float = 1.0
    latency_s: float = 0.85
    dropout: float = 0.35
    noise_px: float = 0.13
    k_px_per_mm: float = 1.0
    g_px_per_c: float = 0.008
    s_px_per_pct: float = -0.004
    offset_px: float = 1.6
    mm_per_px: float = 1.0
    confidence: float = 0.9
    phase_s: float = 0.0
    cameras: tuple = (1,)
    camera_offsets_px: dict = field(default_factory=dict)  # per-camera offset overriding offset_px
    floor_px: float = 0.0  # optical blur floor: a narrow bead never images narrower than this
    transport_s: float | tuple = 0.0  # nozzle -> imaged spot: a frame shows the bead laid down this long before it
    clock_skew_s: float = 0.0  # LEAM's clock runs this far behind the local one (t_ms and t_dep_ms are on LEAM's)
    group_schedule: tuple = ()  # (group, offset_px[, camera]) switching every group_period_s; one group reports at a time
    group_period_s: float = 10.0
    dropout_burst_len: float = 1.0  # mean run of consecutive dropouts (1 = independent); the overall rate stays `dropout`
    confidence_scale_px: float = 0.0  # >0: confidence falls as the bead's blur excess over the floor shrinks below this


class Sensor:
    """IR width samples shaped like WidthSample; t_ms is the acquisition time, delivery is later.

    `acquired` keeps every sample with `true_mm` (the width of the bead the frame shows, for scoring); `ready()` hands
    out copies without it, plus recv_ms (local arrival), t_dep_ms, group and source_frame_id.
    """

    def __init__(self, params: SensorParams | None = None, seed=0):
        self.p = params or SensorParams()
        p = self.p
        if p.dropout_burst_len > 1 and not 0 <= p.dropout / (1 - p.dropout + EPS) / p.dropout_burst_len <= 1:
            raise ValueError("dropout_burst_len is too short for this dropout rate")
        self.rng, (self._xrng,) = _streams(seed, 1)
        self.acquired: list[dict] = []
        self.acquired_t_s: list[float] = []
        self._pending: deque = deque()
        self._next: float | None = None
        self._t0: float | None = None
        self._history: list[tuple] = []
        self._dropped: dict = {}
        self._frames = 0

    def observe(self, t_s: float, plant: Plant) -> None:
        if self._next is None:
            self._t0 = t_s
            self._next = t_s + self.p.phase_s
        low, high = self._transport_range()
        if high > 0:
            self._history.append((t_s, plant.true_width_mm, plant.nozzle_c, plant.setpoints["speed_override_percent"]))
            while len(self._history) > 2 and self._history[1][0] < t_s - high - 1.0:
                self._history.pop(0)
        while t_s + EPS >= self._next:
            for camera, group, group_offset in self._reporters(t_s):
                sample = self._capture(t_s, plant, camera, group, group_offset)
                self.acquired.append(sample)
                self.acquired_t_s.append(t_s)
                self._pending.append((t_s + self.p.latency_s, sample))
            self._next += 1.0 / self.p.rate_hz

    def ready(self, t_s: float) -> list[dict]:
        out = []
        while self._pending and self._pending[0][0] <= t_s + EPS:
            sample = dict(self._pending.popleft()[1])
            del sample["true_mm"]
            out.append(sample)
        return out

    def _transport_range(self) -> tuple[float, float]:
        tr = self.p.transport_s
        return (float(tr[0]), float(tr[1])) if isinstance(tr, (tuple, list)) else (float(tr), float(tr))

    def _reporters(self, t_s: float):
        p = self.p
        if not p.group_schedule:
            return [(camera, camera, 0.0) for camera in p.cameras]
        entry = p.group_schedule[int((t_s - self._t0 + EPS) // p.group_period_s) % len(p.group_schedule)]
        group, offset = entry[0], entry[1]
        return [(entry[2] if len(entry) > 2 else p.cameras[0], group, offset)]

    def _state_at(self, t_s: float, plant: Plant, transport: float) -> tuple[float, float, float]:
        if transport <= 0 or not self._history:
            return plant.true_width_mm, plant.nozzle_c, plant.setpoints["speed_override_percent"]
        t_dep = t_s - transport
        later = self._history[-1]
        for earlier in reversed(self._history[:-1]):
            if earlier[0] <= t_dep:
                span = later[0] - earlier[0]
                w = min(1.0, max(0.0, (t_dep - earlier[0]) / span)) if span > 0 else 1.0
                return tuple(a + (b - a) * w for a, b in zip(earlier[1:], later[1:]))
            later = earlier
        return self._history[0][1:]

    def _is_dropped(self, key) -> bool:
        p = self.p
        u = self.rng.random()
        if p.dropout_burst_len <= 1:
            return u < p.dropout
        stay = 1 - 1 / p.dropout_burst_len
        enter = p.dropout / (1 - p.dropout) / p.dropout_burst_len
        self._dropped[key] = u < (stay if self._dropped.get(key) else enter)
        return self._dropped[key]

    def _capture(self, t_s: float, plant: Plant, camera: int, group=None, group_offset: float = 0.0) -> dict:
        p = self.p
        group = camera if group is None else group
        dropped = self._is_dropped(group)
        noise = self.rng.normal(0.0, p.noise_px)
        low, high = self._transport_range()
        transport = self._xrng.uniform(low, high) if high > low else low
        true_mm, nozzle_c, speed = self._state_at(t_s, plant, transport)
        offset = p.camera_offsets_px.get(camera, p.offset_px) + group_offset
        bead = p.k_px_per_mm * true_mm
        if p.floor_px > 0:
            bead = math.hypot(p.floor_px, bead)
        blur_excess = bead - p.floor_px
        px = bead + p.g_px_per_c * (nozzle_c - REF_C) + p.s_px_per_pct * (speed - 100.0) + offset + noise
        confidence = p.confidence
        if p.confidence_scale_px > 0:
            confidence *= min(1.0, max(0.1, blur_excess / p.confidence_scale_px))
        skew_ms = round(p.clock_skew_s * 1000)
        self._frames += 1
        return {"t_ms": sim_ms(t_s) - skew_ms, "camera": camera, "heading_deg": 0.0, "axis_deg": 0.0,
                "width_px": None if dropped else px, "mm_per_px": p.mm_per_px, "contrast": 0.0 if dropped else 1.0,
                "confidence": 0.0 if dropped else confidence, "reason": "dropout" if dropped else "ok",
                "recv_ms": sim_ms(t_s + p.latency_s), "source_frame_id": f"sim-{self._frames}", "group": group,
                "t_dep_ms": sim_ms(t_s - transport) - skew_ms, "true_mm": true_mm}


@dataclass
class GatewayParams:
    min_interval_s: float = 1.0  # one accepted request per second, shared by all senders
    confirm_min_s: float = 0.5
    confirm_max_s: float = 2.0
    apply_at: str = "confirm"  # "submit": the write reaches the plant after the actuation delay, before the result
    actuation_min_s: float = 0.1
    actuation_max_s: float = 0.4
    lost_applied: bool = False  # a "lost" request (accepted, no result ever) still reaches the plant
    fail_prob: float = 0.0  # chance an accepted controller request ends in a state drawn from fail_mix
    fail_mix: dict = field(default_factory=lambda: {"dispatch_unknown": 1.0, "dispatch_unknown_applied": 1.0,
                                                    "queued_unconfirmed": 1.0, "lost": 1.0})
    cancel_on_gate_close: bool = False  # a request still in flight when the gate closes is dropped and rejected


class Gateway:
    """submit() answers at once (accepted / rejected); poll() returns the results that fell due and poll_writes() the
    writes that reached the plant (the same instant as the result unless apply_at="submit")."""

    def __init__(self, closed_windows=(), params: GatewayParams | None = None, seed=0, forced=None):
        self.p = params or GatewayParams()
        p = self.p
        if p.apply_at not in ("confirm", "submit"):
            raise ValueError(f"apply_at must be 'confirm' or 'submit', got {p.apply_at!r}")
        mix_ok = bool(p.fail_mix) and set(p.fail_mix) <= set(FAULT_STATES) and sum(p.fail_mix.values()) > 0
        if not 0 <= p.fail_prob <= 1 or p.fail_prob > 0 and not mix_ok:
            raise ValueError(f"fail_prob must be in 0-1 and fail_mix weights over {FAULT_STATES}")
        # n-th controller submit (1-based) -> one of FORCED_STATES
        self.forced = dict(forced or {})
        for n, state in self.forced.items():
            if type(n) is not int or n < 1 or state not in FORCED_STATES:
                raise ValueError(f"forced must map submit numbers (1-based) to one of {FORCED_STATES}, got {n!r}: {state!r}")
        self._submits = 0
        self.rng, (self._act_rng, self._fault_rng) = _streams(seed, 2)
        self.closed_windows = [tuple(w) for w in closed_windows]
        self.sequence = 0
        self._last_accept: float | None = None
        self._last_due = 0.0
        self._last_write = 0.0
        self._inflight: list[dict] = []

    def close_during(self, t0_s: float, t1_s: float) -> None:
        self.closed_windows.append((t0_s, t1_s))

    def is_open(self, t_s: float) -> bool:
        return not any(a - EPS <= t_s < b - EPS for a, b in self.closed_windows)

    def submit(self, t_s: float, operation, value, sender: str = "controller") -> dict:
        result = {"t_s": t_s, "sender": sender, "operation": operation, "value": value}
        reason = None
        if operation not in BOUNDS:
            reason = "correction shape"
        elif type(value) not in (int, float) or not BOUNDS[operation][0] <= value <= BOUNDS[operation][1]:
            reason = "bounds"
        elif not self.is_open(t_s):
            reason = "command_gate_closed"
        elif self._last_accept is not None and t_s - self._last_accept < self.p.min_interval_s - EPS:
            reason = "rate_limited"
        forced = None
        if sender == "controller":
            self._submits += 1
            forced = self.forced.get(self._submits)
            if forced is None and not reason and self.p.fail_prob > 0 and self._fault_rng.random() < self.p.fail_prob:
                states = sorted(self.p.fail_mix)
                weights = np.array([self.p.fail_mix[s] for s in states], dtype=float)
                forced = states[self._fault_rng.choice(len(states), p=weights / weights.sum())]
        if forced not in (None, *FORCED_STATES):
            raise ValueError(f"forced state {forced!r} is not one of {FORCED_STATES}")
        if forced == "rejected":
            reason = reason or "forced"
        if reason:
            return {**result, "sequence": None, "state": "rejected", "reason": reason}
        self.sequence += 1
        self._last_accept = t_s
        confirm_due = max(t_s + self.rng.uniform(self.p.confirm_min_s, self.p.confirm_max_s), self._last_due)
        self._last_due = confirm_due
        state = forced or "setting_confirmed"
        base = {**result, "sequence": self.sequence}
        stored = stored_value(operation, value)
        confirmed = state == "setting_confirmed"
        outcome = None if state == "lost" else {
            **base, "state": RESULT_STATE.get(state, state),
            "observation": {"observed_value": stored, "setting_confirmed": True} if confirmed else None}
        write, write_due = None, confirm_due
        if state in APPLIED_STATES or state == "lost" and self.p.lost_applied:
            write = {"t_s": t_s, "sender": sender, "operation": operation, "value": value, "sequence": self.sequence}
            if self.p.apply_at == "submit":
                write_due = max(t_s + self._act_rng.uniform(self.p.actuation_min_s, self.p.actuation_max_s),
                                self._last_write)
                self._last_write = write_due
        self._inflight.append({"seq": self.sequence, "base": base, "result": outcome, "result_due": confirm_due,
                               "write": write, "write_due": write_due, "applied": False})
        return {**base, "state": "accepted"}

    def poll(self, t_s: float) -> list[dict]:
        self._cancel_if_gate_closed(t_s)
        due = [(r["result_due"], r["seq"], r) for r in self._inflight
               if r["result"] is not None and r["result_due"] <= t_s + EPS]
        out = []
        for when, _, rec in sorted(due, key=lambda d: d[:2]):
            out.append({**rec["result"], "t_s": when})
            rec["result"] = None
        self._prune()
        return out

    def poll_writes(self, t_s: float) -> list[dict]:
        self._cancel_if_gate_closed(t_s)
        due = [(r["write_due"], r["seq"], r) for r in self._inflight
               if r["write"] is not None and r["write_due"] <= t_s + EPS]
        return self._release(sorted(due, key=lambda d: d[:2]))

    def flush_writes(self) -> list[dict]:
        """Every write still on its way, now, in order: the Duet already holds them when someone else changes a lever."""
        due = [(r["write_due"], r["seq"], r) for r in self._inflight if r["write"] is not None]
        return self._release(sorted(due, key=lambda d: d[:2]))

    def _release(self, due) -> list[dict]:
        out = []
        for when, _, rec in due:
            out.append({**rec["write"], "t_s": when})
            rec["write"], rec["applied"] = None, True
        self._prune()
        return out

    def _prune(self) -> None:
        self._inflight = [r for r in self._inflight if r["result"] is not None or r["write"] is not None]

    def _cancel_if_gate_closed(self, t_s: float) -> None:
        if not self.p.cancel_on_gate_close or self.is_open(t_s):
            return
        for rec in self._inflight:
            if rec["applied"]:
                continue
            rec["write"] = None
            if rec["result"] is not None:
                rec["result"] = {**rec["base"], "state": "rejected", "reason": "command_gate_closed", "observation": None}
                rec["result_due"] = min(rec["result_due"], t_s)


class SimHarness:
    def __init__(self, plant: Plant, sensor: Sensor, gateway: Gateway, readback_period_s: float = 0.0,
                 readback_none_prob: float = 0.0, seed=0, submit_jitter_s: float = 0.0):
        if not 0 <= submit_jitter_s < 1.0:
            raise ValueError("submit_jitter_s must be in [0, 1)")
        self.plant, self.sensor, self.gateway = plant, sensor, gateway
        self._events: list[tuple[float, int, object]] = []
        # Real readbacks refresh only every ~1.4 s and are sometimes invalid (None); 0 = always fresh and valid.
        self.readback_period_s, self.readback_none_prob = readback_period_s, readback_none_prob
        self.submit_jitter_s = submit_jitter_s
        self._rb_rng, (self._jitter_rng,) = _streams(seed, 1)
        self._rb_cache, self._rb_next = None, None
        self._next_control: float | None = None
        self._submits: list[tuple[float, int, dict]] = []
        # Gateway answers since the last control step (verdicts on submit, later results); step_fn reads harness.inbox.
        self.inbox: list[dict] = []
        self._mail: list[dict] = []
        if gateway.p.apply_at == "submit":
            plant.before_external = self._flush_gateway_writes

    @classmethod
    def realistic(cls, plant_params: PlantParams | None = None, sensor_params: SensorParams | None = None,
                  gateway: Gateway | None = None, seed: int = 0, **overrides) -> "SimHarness":
        """The hostile preset (every option on). Params or a gateway you pass are used as given; `overrides` are field
        names of PlantParams / SensorParams / GatewayParams, SimHarness kwargs (readback_period_s, readback_none_prob,
        submit_jitter_s), `initial` (Plant) or `closed_windows` / `forced` (Gateway), applied on top of the preset."""
        buckets = {"plant": {}, "sensor": {}, "gateway": {}, "harness": {}, "initial": None, "closed": (), "forced": None}
        fields = {"plant": {f.name for f in dataclasses.fields(PlantParams)},
                  "sensor": {f.name for f in dataclasses.fields(SensorParams)},
                  "gateway": {f.name for f in dataclasses.fields(GatewayParams)},
                  "harness": {"readback_period_s", "readback_none_prob", "submit_jitter_s"}}
        for key, value in overrides.items():
            if key in ("initial", "closed_windows", "forced"):
                buckets[{"initial": "initial", "closed_windows": "closed", "forced": "forced"}[key]] = value
                continue
            for name, names in fields.items():
                if key in names:
                    buckets[name][key] = value
                    break
            else:
                raise TypeError(f"unknown override {key!r}")
        s_sensor, s_gateway, s_harness = np.random.SeedSequence(seed).spawn(3)
        plant_params = dataclasses.replace(plant_params or PlantParams(**REALISTIC_PLANT), **buckets["plant"])
        sensor_params = dataclasses.replace(sensor_params or SensorParams(**REALISTIC_SENSOR), **buckets["sensor"])
        if gateway is not None and (buckets["gateway"] or buckets["closed"] or buckets["forced"]):
            raise ValueError("gateway overrides need the preset to build the gateway; pass them or a gateway, not both")
        if gateway is None:
            params = GatewayParams(**{**REALISTIC_GATEWAY, **buckets["gateway"]})
            gateway = Gateway(buckets["closed"], params, s_gateway, buckets["forced"])
        harness = {**REALISTIC_HARNESS, **buckets["harness"]}
        return cls(Plant(plant_params, buckets["initial"]), Sensor(sensor_params, s_sensor), gateway,
                   seed=s_harness, **harness)

    def _refresh_readbacks(self) -> None:
        rb = self.plant.readbacks()
        for group in rb.values():
            for key in group:
                if self._rb_rng.random() < self.readback_none_prob:
                    group[key] = None
        self._rb_cache = rb

    def _readbacks(self) -> dict:
        if self.readback_period_s <= 0 and self.readback_none_prob <= 0:
            return self.plant.readbacks()
        if self.readback_period_s <= 0:
            self._refresh_readbacks()
        return {k: dict(v) for k, v in self._rb_cache.items()}

    def _tick_readbacks(self, t: float) -> None:
        if self.readback_period_s <= 0:
            return
        if self._rb_next is None:
            self._rb_next = t
        if t + EPS >= self._rb_next:
            self._refresh_readbacks()
            while self._rb_next <= t + EPS:
                self._rb_next += self.readback_period_s

    def _flush_gateway_writes(self) -> None:
        for write in self.gateway.flush_writes():
            self.plant.apply(write["operation"], write["value"])

    def schedule(self, t_s: float, fn) -> None:
        """fn(plant, gateway) runs at the first tick at or after t_s: disturbances, external changes, gate windows."""
        self._events.append((t_s, len(self._events), fn))
        self._events.sort(key=lambda e: e[:2])

    def run(self, step_fn, duration_s: float, dt: float = 0.1) -> dict:
        """Calls step_fn(now_ms, samples, readbacks, gate_open) once per second; it returns None or an action dict.
        Runs can be chunked: the one-second control cadence carries over from one run() to the next."""
        per_control = round(1.0 / dt)
        if per_control < 1 or abs(per_control * dt - 1.0) > 1e-9:
            raise ValueError(f"dt must divide 1 s into whole ticks, got {dt}")
        plant, sensor, gateway = self.plant, self.sensor, self.gateway
        t_start = plant.t
        if self._next_control is None:
            self._next_control = t_start + 1.0
        ticks = round(duration_s / dt)
        first_sample = len(sensor.acquired)
        rows: list[tuple] = []
        actions: list[tuple] = []
        results: list[dict] = []
        external_from = len(plant.external_log)

        def submit(t, action):
            verdict = gateway.submit(t, action["operation"], action["value"])
            self._mail.append({"kind": "verdict", **verdict})
            actions.append((t, action["operation"], action["value"], verdict["state"], verdict.get("reason", ""),
                            verdict["sequence"] or 0))

        for i in range(ticks):
            t = t_start + i * dt
            while self._events and self._events[0][0] <= t + EPS:
                self._events.pop(0)[2](plant, gateway)
            while self._submits and self._submits[0][0] <= t + EPS:
                submit(t, self._submits.pop(0)[2])
            for write in gateway.poll_writes(t):
                plant.apply(write["operation"], write["value"])
            for result in gateway.poll(t):
                results.append(result)
                self._mail.append({"kind": "result", **result})
            sensor.observe(t, plant)
            self._tick_readbacks(t)
            sp = plant.setpoints
            rows.append((t, plant.true_width_mm, plant.flow_eff, plant.nozzle_c, sp["flow_percent"],
                         sp["speed_override_percent"], sp["nozzle_temperature_c"], sp["pressure_advance_seconds"],
                         gateway.is_open(t)))
            if t + EPS >= self._next_control:
                self._next_control += 1.0
                self.inbox, self._mail = self._mail, []
                action = step_fn(sim_ms(t), sensor.ready(t), self._readbacks(), gateway.is_open(t))
                if action is not None and self.submit_jitter_s > 0:
                    self._submits.append((t + self._jitter_rng.uniform(0.0, self.submit_jitter_s), len(self._submits),
                                          action))
                    self._submits.sort(key=lambda s: s[:2])
                elif action is not None:
                    submit(t, action)
            plant.step(dt)
        return self._series(rows, actions, results, sensor.acquired[first_sample:],
                            sensor.acquired_t_s[first_sample:], plant.external_log[external_from:])

    @staticmethod
    def _series(rows, actions, results, samples, sample_t_s, external) -> dict:
        names = ("t_s", "true_mm", "flow_eff", "nozzle_c", "flow_percent", "speed_override_percent",
                 "nozzle_target_c", "pressure_advance_seconds", "gate_open")
        out = {name: np.array(col) for name, col in zip(names, zip(*rows))} if rows else {n: np.array([]) for n in names}
        out.update(
            meas_t_s=np.array(sample_t_s, dtype=float),
            meas_t_dep_s=np.array([(s["t_dep_ms"] - s["t_ms"]) / 1000 for s in samples], dtype=float) + np.array(
                sample_t_s, dtype=float),
            meas_camera=np.array([s["camera"] for s in samples], dtype=int),
            meas_group=np.array([str(s["group"]) for s in samples], dtype=str),
            meas_px=np.array([np.nan if s["width_px"] is None else s["width_px"] for s in samples], dtype=float),
            meas_true_mm=np.array([s["true_mm"] for s in samples], dtype=float),
            meas_ok=np.array([s["reason"] == "ok" for s in samples], dtype=bool),
            action_t_s=np.array([a[0] for a in actions], dtype=float),
            action_operation=np.array([a[1] for a in actions], dtype=str),
            action_value=np.array([a[2] for a in actions], dtype=float),
            action_state=np.array([a[3] for a in actions], dtype=str),
            action_reason=np.array([a[4] for a in actions], dtype=str),
            action_sequence=np.array([a[5] for a in actions], dtype=int),
            result_t_s=np.array([r["t_s"] for r in results], dtype=float),
            result_operation=np.array([r["operation"] for r in results], dtype=str),
            result_value=np.array([r["value"] for r in results], dtype=float),
            result_sender=np.array([r["sender"] for r in results], dtype=str),
            result_state=np.array([r["state"] for r in results], dtype=str),
            result_sequence=np.array([r["sequence"] for r in results], dtype=int),
            external_t_s=np.array([e[0] for e in external], dtype=float),
            external_operation=np.array([e[1] for e in external], dtype=str),
            external_value=np.array([e[2] for e in external], dtype=float),
        )
        return out


# The `SimHarness.realistic` preset. Glow 0.02 px/C and floor 1.8 px are what the golden prints show; skew, transport,
# groups, bursts, faults and jitter are plausible magnitudes chosen to be unkind, not measurements.
REALISTIC_PLANT = {"flow_exponent": 0.5, "melt_width_mm": 2.1}  # print 2: the demo profile (Q3)
REALISTIC_SENSOR = {
    "g_px_per_c": 0.02, "floor_px": 1.8, "confidence_scale_px": 0.5, "dropout_burst_len": 3.0,
    "transport_s": (0.5, 1.5), "clock_skew_s": 0.3, "group_period_s": 10.0,
    "group_schedule": (("cam1:-90", 0.0, 1), ("cam3:90", 0.15, 3), ("cam2:180", -0.15, 2)),
}
REALISTIC_GATEWAY = {"apply_at": "submit", "fail_prob": 0.05, "lost_applied": True}
REALISTIC_HARNESS = {"readback_period_s": 1.4, "readback_none_prob": 0.05, "submit_jitter_s": 0.2}
