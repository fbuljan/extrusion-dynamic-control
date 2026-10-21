"""Numpy runtime of the DDSP-style printer model; parameters from app/models/printer_model.json (tools/model/fit_printer).

Steady bead width, parity-tested against the PyTorch fit:
  w = softmin(w_nom * line_w / line_w_ref * (flow / 100) ** beta,  q220 * exp(alpha * (T - ref_c)) / (v * h); softness)
  v = feed_mm_s * speed / 100 (M220 scales travel and extrusion together, so speed acts only through the melt limit).
settings: flow %, speed % (override), nozzle_c (actual C), pa (ignored: never read back, assumed constant).
context: feed_mm_s (travel speed at 100 % override), layer_h_mm, line_w_mm, print (selects w_nom); a missing or
non-positive value falls back to the selected print's (default print: the file's default_print).
Under beta < 1, q220 is a capacity in width space, not a pure melt rate; limits of the fit are in the file's fit.notes.
"""
import json
import math
import numbers
from collections import deque
from pathlib import Path

import numpy as np

LEVERS = {"flow": "flow", "flow_percent": "flow", "speed": "speed", "speed_override_percent": "speed",
          "nozzle_c": "nozzle_c", "nozzle_temperature_c": "nozzle_c"}
MIN_SPEED_MM_S = 0.5
INVERT_GRID = 400
VERSION_PREFIX = "physics-1"
POSITIVE = ("q220_mm3_s", "beta", "softness_mm")
NON_NEGATIVE = ("alpha_per_c",)
LAGS = ("tau_p_s", "tau_h_s", "dead_h_s")  # optional; missing means no lag
PRINT_KEYS = ("w_nom_mm", "feed_mm_s", "layer_h_mm", "line_w_mm")


def softmin(a, b, s):
    return np.minimum(a, b) - s * np.log1p(np.exp(-np.abs(a - b) / s))


def width(p, flow_frac, v_mm_s, nozzle_c, layer_h_mm, w_nom_mm):
    """Steady width in mm, numpy broadcasting; p = the file's params plus ref_c."""
    w_ask = w_nom_mm * np.maximum(flow_frac, 0.0) ** p["beta"]
    w_melt = p["q220_mm3_s"] * np.exp(p["alpha_per_c"] * (nozzle_c - p["ref_c"])) / (v_mm_s * layer_h_mm)
    return softmin(w_ask, w_melt, p["softness_mm"])


def _number(x):
    return float(x) if isinstance(x, numbers.Real) and not isinstance(x, bool) and math.isfinite(x) else None


def _positive(x):
    n = _number(x)
    return n if n is not None and n > 0 else None


def _check(data):
    """ValueError unless the file is a physics-1 model with every parameter the forward needs, finite and in range."""
    if not isinstance(data, dict) or not str(data.get("version", "")).startswith(VERSION_PREFIX):
        raise ValueError(f"not a {VERSION_PREFIX} printer model")
    params, prints = data.get("params"), data.get("prints")
    if not isinstance(params, dict) or not isinstance(prints, dict) or not prints:
        raise ValueError("params or prints missing")
    for key in POSITIVE + NON_NEGATIVE + LAGS:
        if key in LAGS and key not in params:
            continue
        n = _number(params.get(key))
        if n is None or n < 0 or (n == 0 and key in POSITIVE):
            raise ValueError(f"params.{key} = {params.get(key)!r}")
    for name, pr in prints.items():
        bad = [k for k in PRINT_KEYS if not isinstance(pr, dict) or _positive(pr.get(k)) is None]
        if bad:
            raise ValueError(f"prints.{name}: {', '.join(bad)} missing or not positive")
    for key in ("ref_c", "line_w_ref_mm"):
        if key in data and _positive(data[key]) is None:
            raise ValueError(f"{key} = {data[key]!r}")


class PrinterModel:
    def __init__(self, data: dict):
        _check(data)
        self.data = data
        self.version = str(data.get("version"))
        self.p = {"tau_p_s": 0.0, "tau_h_s": 0.0, "dead_h_s": 0.0, **data["params"],
                  "ref_c": float(data.get("ref_c", 220.0))}
        self.prints = data["prints"]
        self.default_print = data.get("default_print") if data.get("default_print") in self.prints else \
            next(iter(self.prints))
        self.line_w_ref = float(data.get("line_w_ref_mm", 1.5))

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def context(self, context=None):
        """(feed_mm_s, layer_h_mm, w_nom_mm) for a context dict, defaults from its print."""
        c = context or {}
        pr = self.prints.get(c.get("print"), self.prints[self.default_print])
        feed = _positive(c.get("feed_mm_s")) or pr["feed_mm_s"]
        layer = _positive(c.get("layer_h_mm")) or _positive(c.get("layer_height_mm")) or pr["layer_h_mm"]
        line_w = _positive(c.get("line_w_mm")) or pr["line_w_mm"]
        return feed, layer, pr["w_nom_mm"] * line_w / self.line_w_ref

    def _widths(self, flow, speed, nozzle_c, context):
        feed, layer, w_nom = self.context(context)
        v = np.maximum(feed * np.asarray(speed, float) / 100.0, MIN_SPEED_MM_S)
        return width(self.p, np.asarray(flow, float) / 100.0, v, np.asarray(nozzle_c, float), layer, w_nom)

    def steady_width(self, settings, context=None):
        return float(self._widths(settings["flow"], settings["speed"], settings["nozzle_c"], context))

    def simulate(self, state, settings_seq, dt_s):
        """Width after each dt_s step while settings_seq applies (its nozzle_c is the target): flow through the pressure
        lag, the nozzle through the heater dead time and lag, speed at once. state: flow, nozzle_c (actual), context."""
        if not dt_s > 0:
            raise ValueError(f"dt_s must be > 0, got {dt_s!r}")
        p = self.p
        flow, temp = float(state["flow"]), float(state["nozzle_c"])
        a_p = 1 - math.exp(-dt_s / p["tau_p_s"]) if p["tau_p_s"] > 0 else 1.0
        a_h = 1 - math.exp(-dt_s / p["tau_h_s"]) if p["tau_h_s"] > 0 else 1.0
        targets = deque([temp] * max(0, round(p["dead_h_s"] / dt_s)))
        out = []
        for s in settings_seq:
            targets.append(s["nozzle_c"])
            flow += (s["flow"] - flow) * a_p
            temp += (targets.popleft() - temp) * a_h
            out.append(self.steady_width({**s, "flow": flow, "nozzle_c": temp}, state.get("context")))
        return out

    def invert(self, lever, target_mm, settings, context, bounds):
        """Value of one lever within bounds whose steady width is target_mm, nearest the current value; None if no value
        within bounds reaches it. lever: flow / speed / nozzle_c (or the controller's operation names)."""
        if lever not in LEVERS:
            raise ValueError(f"unknown lever {lever!r}")
        key = LEVERS[lever]
        lo, hi = float(bounds[0]), float(bounds[1])
        if not lo <= hi:
            return None

        def gap(x):
            return self._widths(*(x if k == key else settings[k] for k in ("flow", "speed", "nozzle_c")),
                                context) - target_mm

        xs = np.linspace(lo, hi, INVERT_GRID + 1)
        g = gap(xs)
        roots = list(xs[g == 0])
        for i in np.where(g[:-1] * g[1:] < 0)[0]:
            a, b = xs[i], xs[i + 1]
            for _ in range(60):
                m = (a + b) / 2
                if (gap(m) > 0) == (g[i] > 0):
                    a = m
                else:
                    b = m
            roots.append((a + b) / 2)
        if not roots:
            return None
        current = settings.get(key)
        return float(min(roots, key=lambda r: abs(r - current)) if current is not None else roots[0])
