"""Analytic stand-ins for the fitted camera and printer models, with the sim bench's physics (tools/sim/plant.py).

For tests only, and to build deliberately wrong models; production code never imports this.
"""
import math

REF_C = 220.0
REF_LAYER_MM = 0.5


def softmin(a, b, soft):
    m = min(a, b)
    return m if soft <= 0 else m - soft * math.log1p(math.exp(-abs(a - b) / soft))


class StubCameraModel:
    """IR px = hypot(floor, k * min(mm, knee)) + g * (T - 220) + s * (v - v_ref) + offset[group]; `groups` limits which
    groups have a fit; valid px start `bead_px_range[0]` above the floor; no knee unless `knee_mm` is given, and like
    the runtime model a reading is cut at knee + 2 * ramp_mm."""

    version = "stub"

    def __init__(self, k=1.0, g=0.008, s=-0.02, ref_speed_mm_s=20.0, offset_px=1.6, offsets_px=None, groups=None,
                 sigma_mm=0.03, bead_px_range=(0.0, math.inf), floor_px=0.0, knee_mm=None, ramp_mm=0.0):
        self.k, self.g, self.s, self.ref_speed = k, g, s, ref_speed_mm_s
        self.knee, self.ramp = (math.inf if knee_mm is None else knee_mm), ramp_mm
        self.offset, self.offsets = offset_px, dict(offsets_px or {})
        self.groups = None if groups is None else set(groups)
        self.sigma_mm, self.bead_px_range, self.floor = sigma_mm, bead_px_range, floor_px

    def _terms(self, group, nozzle_c, speed_mm_s):
        offset = self.offsets.get(group, self.offset)
        return self.g * (nozzle_c - REF_C) + self.s * (speed_mm_s - self.ref_speed) + offset

    def real_mm(self, width_px, group, nozzle_c, speed_mm_s):
        if self.groups is not None and group not in self.groups:
            return None
        bead = width_px - self._terms(group, nozzle_c, speed_mm_s)
        if self.floor > 0:
            bead = math.copysign(math.sqrt(abs(bead * bead - self.floor ** 2)), bead - self.floor)
        return min(bead / self.k, self.knee + 2 * self.ramp), self.sigma_mm

    def ir_px(self, real_mm, group, nozzle_c, speed_mm_s):
        mm = min(real_mm, self.knee)
        bead = math.hypot(self.floor, self.k * mm) if self.floor > 0 else self.k * mm
        return bead + self._terms(group, nozzle_c, speed_mm_s)

    def valid_px_range(self, group):
        lo, hi = self.bead_px_range
        offset = self.offsets.get(group, self.offset)
        return lo + self.floor + offset, hi + self.floor + offset

    def knee_mm(self, group):
        return None if math.isinf(self.knee) or (self.groups is not None and group not in self.groups) else self.knee

    def ceiling_mm(self):
        return None if math.isinf(self.knee) else self.knee


class StubPrinterModel:
    """Steady width = softmin(asked, meltable) + bias; `gains` scale each lever's input around 100 % / 220 C, so a
    gain of 1.4 makes the model claim 40 % more effect than the physics it was built from."""

    version = "stub"

    def __init__(self, nominal_width_mm=1.5, melt_width_mm=1.6, melt_alpha_per_c=0.02, softness_mm=0.08,
                 layer_height_mm=0.5, flow_tau_s=3.0, heater_tau_s=25.0, gains=None, bias_mm=0.0, flow_exponent=1.0):
        self.nominal, self.melt, self.alpha, self.soft = nominal_width_mm, melt_width_mm, melt_alpha_per_c, softness_mm
        self.layer, self.flow_tau, self.heater_tau = layer_height_mm, flow_tau_s, heater_tau_s
        self.gains = {"flow": 1.0, "speed": 1.0, "nozzle": 1.0, **(gains or {})}
        self.bias, self.exponent = bias_mm, flow_exponent

    def steady_width(self, settings, context=None):
        g = self.gains
        flow = 100 + g["flow"] * (settings["flow"] - 100)
        speed = max(1.0, 100 + g["speed"] * (settings["speed"] - 100))
        temp = REF_C + g["nozzle"] * (settings["nozzle_c"] - REF_C)
        layer = (context or {}).get("layer_height_mm") or self.layer
        asked = self.nominal * (max(flow, 0.0) / 100) ** self.exponent
        meltable = self.melt * math.exp(self.alpha * (temp - REF_C)) * 100 / speed * REF_LAYER_MM / layer
        return softmin(asked, meltable, self.soft) + self.bias

    def simulate(self, state, settings_seq, dt_s):
        flow, temp = state["flow"], state["nozzle_c"]
        a_flow, a_heat = 1 - math.exp(-dt_s / self.flow_tau), 1 - math.exp(-dt_s / self.heater_tau)
        out = []
        for s in settings_seq:
            flow += (s["flow"] - flow) * a_flow
            temp += (s["nozzle_c"] - temp) * a_heat
            out.append(self.steady_width({**s, "flow": flow, "nozzle_c": temp}, state.get("context")))
        return out

    def invert(self, lever, target_mm, settings, context, bounds):
        lo, hi = bounds
        grid = [lo + (hi - lo) * i / 400 for i in range(401)]
        best = min(grid, key=lambda v: abs(self.steady_width({**settings, lever: v}, context) - target_mm))
        return best if abs(self.steady_width({**settings, lever: best}, context) - target_mm) < 0.01 else None
