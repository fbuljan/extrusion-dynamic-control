"""Dataset builder and go/no-go gate for the width model: IR frames joined to printer state, labels and calliper.

    .venv/bin/python -m tools.model.dataset build [CAPTURE ...] [--no-widths]
    .venv/bin/python -m tools.model.dataset gate

build  -> data/model/frames.parquet (one row of scalars per IR frame: width_check's columns + printer state at the
          acquisition or deposit time + print/layer/band/pass labels), bands.parquet (per print, band, group, pass),
          prints.json (per print context), then the gate and the printed summary when widths are present.
gate   -> data/model/gate.json from frames.parquet: print 2, band 10 (flow 140) minus band 2 (flow 80) per camera-heading
          group and pass, block-bootstrapped by layer. Never reads an image; widths come from width_check's per-frame scalars.

Clocks: frame, motion and readback times are LEAM's (source_utc_ns). The recorder's receive times and the runner logs are
on the laptop clock; they only place print and window boundaries, via the capture's median receive-minus-source offset.

Rows: print_id is print1, print2, afternoon (sweep windows) or outside; band 0 is outside the band plan (plan pre / post /
none); pass is inner / outer on print 2 and blank elsewhere. State (flow, nozzle, speed, Z, layer, band, pass) is joined at
t_join_ms: width_check's deposit time when it is set and within 5 s before the frame, else the frame time. Motion is the nearest
sample within 0.5 s, readbacks (1.4 s refresh) the last sample within 3 s; nothing is joined beyond that. settled = not a
band's first layer and nozzle within 2 C of its target; plan_ok = flow, speed and nozzle-target readbacks at the band's
plan; usable = reason ok, settled and plan_ok.
"""
import argparse
import itertools
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
CAPTURES = REPO / "data" / "captures"
OUT = REPO / "data" / "model"

MAX_JOIN_MS = 500  # a frame is joined to the nearest motion sample no further than this
MAX_READBACK_AGE_MS = 3000  # readbacks refresh every ~1.4 s; the bridge drops anything older than 3 s
MAX_DEP_LAG_MS = 5000  # a deposit time further back than this is not a bead of this frame
NOZZLE_TOL_C = 2.0
PLAN_TOL = 0.5
SETTLE_AFTER_WINDOW_S = 5.0  # afternoon sweep: skip this long after a window opens
# Layers (ported from the team's band-labelling research script): a layer is a Z step of one layer height that holds while extruding
LAYER_MIN_MM, LAYER_MAX_MM, SETTLE_S, PRINT_Z_MAX = 0.1, 2.0, 2.0, 300
PRIME_STEP_MM = 0.3  # a first "layer" that the next one clears by less than this is a priming pass
LEG_DIRS = np.array([45.0, -90.0, 135.0])  # print 2's three leg directions as printed (clockwise triangle)
LEG_TOL_DEG, MIN_RUN, MIN_SPEED_MM_S, LEG_TRIM_MM = 25.0, 4, 5.0, 10.0
GROUP_DEG = 45.0
GATE_GROUP_MIN_FRAMES, GATE_GROUP_MIN_LAYERS, GATE_SE, GATE_GROUPS, N_BOOT = 15, 3, 3.0, 2, 1000
GATE_MIN_BASELINE_BANDS = 3  # baseline bands needed to estimate how much a band's median drifts from band to band

WIDTH_COLS = ["heading_deg", "axis_deg", "width_px", "mm_per_px", "contrast", "confidence", "reason", "source_frame_id", "group",
              "t_dep_ms", "dist_since_turn_mm"]
PROVENANCE_COLS = ["geometry_version", "width_config_hash"]  # what the width extractor ran with, per width row
BASELINE_BANDS = (1, 4, 8, 11, 12)
REF_BANDS = (4, 8, 11, 12)  # reference for contrasts: band 1 ran with the fan on below Z 6.2
FLOW_BANDS = {60: 9, 80: 2, 120: 3, 140: 10}  # print 2 band of each non-baseline flow
LAYER_H_MM, LINE_W_MM = 0.5, 1.5  # Z steps; wall pitch (no slicer file was recorded)


@dataclass(frozen=True)
class Print:
    id: str
    captures: tuple  # recordings that hold this print (print 2 starts in the first one)
    start: str  # Europe/Berlin local time from the PRINT_END markers / READMEs
    end: str
    walls: int | None
    band_first_layers: tuple = ()  # first layer of each band; the last entry closes the final band
    defect_bands: tuple = ()
    labels: tuple = ()  # band label overrides: ((band, text), ...)


PRINTS = {
    "print1": Print("print1", ("golden-20261020-1736",), "2026-10-20T17:41:34.028+02:00",
                    "2026-10-20T18:08:21.919+02:00", 1, tuple(range(1, 122, 15))),
    "print2": Print("print2", ("golden-20261020-1736", "golden2-20261020-1815"), "2026-10-20T18:14:23+02:00",
                    "2026-10-20T20:20:42.482+02:00", 2, (6, *range(21, 132, 10)), (3, 7, 9),
                    ((11, "baseline in effect (fan 100 commanded, no change)"),)),
    "afternoon": Print("afternoon", ("print-20261020-1531",), "2026-10-20T15:34:19.573+02:00",
                       "2026-10-20T17:27:10.645+02:00", None),
}
FAN_ON_BELOW_Z = {"print2": 6.2}  # README: band 1's fan-off command was refused until DEMEX went to RUN


def jsonl(path):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:  # a recording may end mid-line
                pass


def local_ms(iso):
    return datetime.fromisoformat(iso).timestamp() * 1000


def wrap_deg(a):
    return (np.asarray(a, float) + 180.0) % 360.0 - 180.0


def heading_group(cam, heading_deg):
    """Camera x travel heading to 45 deg, as in server.width (the key of the per-group camera model)."""
    if heading_deg is None or not np.isfinite(heading_deg):
        return f"cam{cam}:none"
    h = int(round(heading_deg / GROUP_DEG)) * int(GROUP_DEG) % 360
    return f"cam{cam}:{h - 360 if h > 180 else h}"


# telemetry ---------------------------------------------------------------------------------------------------------
SERIES = {  # name -> columns; every series is sorted by its own LEAM time t (ms), deduplicated
    "mot": ["t", "x", "y", "z", "vx", "vy", "vmag", "ext", "spd"],
    "noz": ["t", "cur", "tgt"], "bed": ["t", "cur"], "flow": ["t", "val"], "fan": ["t", "act"],
    "proc": ["t", "sub", "melt"],
}


class Telemetry:
    """Scalar telemetry series on LEAM's clock with the laptop-clock offset; lookups never reach further than a gap."""

    def __init__(self, series, offset_ms=0.0):
        self.s = {k: {c: np.asarray(series.get(k, {}).get(c, []), float) for c in cols} for k, cols in SERIES.items()}
        self.offset_ms = float(offset_ms)
        for d in self.s.values():
            order = np.argsort(d["t"], kind="stable")
            keep = np.r_[True, np.diff(d["t"][order]) > 0] if len(order) else np.zeros(0, bool)
            for c in d:
                d[c] = d[c][order][keep]

    @staticmethod
    def load(path):
        rows = {k: {c: [] for c in cols} for k, cols in SERIES.items()}
        last = dict.fromkeys(SERIES)
        recv_minus_src = []

        def add(name, t, *vals):
            if t is not None and t != last[name]:
                last[name] = t
                for c, v in zip(SERIES[name], (t / 1e6, *vals)):
                    rows[name][c].append(np.nan if v is None else v)

        for r in jsonl(path):
            s = (r.get("msg") or {}).get("sources") or {}
            mo = s.get("motion") or {}
            if mo.get("valid") is True:
                p = mo.get("payload") or {}
                pos, vel = p.get("position_mm") or {}, p.get("velocity_mm_s") or {}
                add("mot", mo.get("source_utc_ns"), pos.get("x"), pos.get("y"), pos.get("z"), vel.get("x"), vel.get("y"),
                    p.get("velocity_magnitude"), bool(p.get("extrusion_active")), p.get("speed_override_percent"))
                if isinstance(mo.get("source_utc_ns"), (int, float)):
                    recv_minus_src.append(r["recv_ms"] - mo["source_utc_ns"] / 1e6)
            du = s.get("duet_readback") or {}
            dp = du.get("payload") or {}
            if du.get("valid") is True:
                temps = dp.get("temperatures") or {}
                for name, key in (("noz", "nozzle"), ("bed", "bed")):
                    h = temps.get(key) or {}
                    if h.get("valid") is True:
                        add(name, h.get("source_utc_ns"), h.get("current_c"), *([h.get("active_target_c")] * (name == "noz")))
                if dp.get("extrusion_valid") is True:
                    add("flow", dp.get("extrusion_source_utc_ns"), dp.get("extrusion_percent"))
                fans = dp.get("fans") or []
                if fans and isinstance(fans[0].get("actualValue"), (int, float)):
                    add("fan", du.get("source_utc_ns"), 100.0 * fans[0]["actualValue"])
            pr, me = s.get("process") or {}, s.get("melt") or {}
            sub = (((pr.get("payload") or {}).get("measurement") or {}).get("substrate")) or {}
            melt = (me.get("payload") or {}) if me.get("valid") is True and (me.get("payload") or {}).get("valid") is True else {}
            sub_t = sub.get("temperature_c") if pr.get("valid") is True and sub.get("valid") is True else None
            if pr.get("valid") is True:
                add("proc", pr.get("source_utc_ns"), sub_t, melt.get("temperature_c"))
        offset = float(np.median(recv_minus_src)) if recv_minus_src else 0.0
        return Telemetry(rows, offset)

    @staticmethod
    def concat(tels):
        merged = {k: {c: np.concatenate([t.s[k][c] for t in tels]) for c in cols} for k, cols in SERIES.items()}
        return Telemetry(merged, float(np.median([t.offset_ms for t in tels])))

    def pick(self, name, tq, mode, max_gap_ms):
        """Index of the sample for each query time, -1 where none lies within the gap.
        mode 'nearest': closest on either side; 'before': last sample at or before the query."""
        t = self.s[name]["t"]
        tq = np.asarray(tq, float)
        if not len(t):
            return np.full(len(tq), -1)
        i = np.clip(np.searchsorted(t, tq, side="right") - 1, 0, len(t) - 1)
        if mode == "nearest":
            j = np.clip(i + 1, 0, len(t) - 1)
            i = np.where(np.abs(t[j] - tq) < np.abs(t[i] - tq), j, i)
        gap = np.where(np.isfinite(tq), tq - t[i], np.inf)
        ok = (np.abs(gap) <= max_gap_ms) if mode == "nearest" else ((gap >= 0) & (gap <= max_gap_ms))
        return np.where(ok, i, -1)

    def get(self, name, col, tq, mode, max_gap_ms):
        """(value, time gap) of the sample picked for each query time; NaN where none lies within the gap."""
        tq = np.asarray(tq, float)
        d = self.s[name]
        if not len(d["t"]):
            return np.full(len(tq), np.nan), np.full(len(tq), np.nan)
        i = self.pick(name, tq, mode, max_gap_ms)
        j, hit = np.clip(i, 0, len(d["t"]) - 1), i >= 0
        return np.where(hit, d[col][j], np.nan), np.where(hit, np.abs(tq - d["t"][j]), np.nan)

    def window(self, t0, t1):
        sel = {k: {c: d[c][(d["t"] >= t0) & (d["t"] <= t1)] for c in d} for k, d in self.s.items()}
        return Telemetry(sel, self.offset_ms)


def state_at(tel, tq):
    """Printer state at LEAM times tq: motion nearest within MAX_JOIN_MS, readbacks the last sample within 3 s."""
    tq = np.asarray(tq, float)
    out, gaps = {}, {}
    for col, key in (("x", "x"), ("y", "y"), ("z", "z"), ("vmag", "speed_mm_s"), ("spd", "speed_pct"), ("ext", "extruding")):
        out[key], gaps[key] = tel.get("mot", col, tq, "nearest", MAX_JOIN_MS)
    vx, _ = tel.get("mot", "vx", tq, "nearest", MAX_JOIN_MS)
    vy, _ = tel.get("mot", "vy", tq, "nearest", MAX_JOIN_MS)
    out["motion_heading_deg"] = np.where(np.hypot(vx, vy) >= 2.0, np.degrees(np.arctan2(vy, vx)), np.nan)
    for name, col, key in (("flow", "val", "flow_pct"), ("noz", "cur", "nozzle_c"), ("noz", "tgt", "nozzle_target_c"),
                           ("bed", "cur", "bed_c"), ("fan", "act", "fan_pct")):
        out[key], gaps[key] = tel.get(name, col, tq, "before", MAX_READBACK_AGE_MS)
    for col, key in (("sub", "substrate_c"), ("melt", "melt_c")):
        out[key], gaps[key] = tel.get("proc", col, tq, "nearest", MAX_JOIN_MS)
    df = pd.DataFrame(out)
    df["mot_gap_ms"] = gaps["x"]
    df["readback_age_ms"] = pd.DataFrame({k: gaps[k] for k in ("flow_pct", "nozzle_c", "bed_c")}).max(axis=1)
    df["pa_s"] = np.nan  # not in telemetry; no pressure-advance request was ever sent in these recordings
    df["feed_mm_s"] = df.speed_mm_s / (df.speed_pct / 100.0).where(df.speed_pct > 0)
    return df


# layers, passes, wall spacing -------------------------------------------------------------------------------------
class LayerCounter:
    """Layers, ported from the team's band-labelling research script; start is when the committed layer's Z step began holding."""

    def __init__(self):
        self.z = self.cand = self.start = None
        self.n = 0

    def update(self, t, z, extruding):
        if not extruding or z is None or not np.isfinite(z) or z > PRINT_Z_MAX:
            self.cand = None
            return False
        if self.z is not None and not LAYER_MIN_MM <= z - self.z <= LAYER_MAX_MM:
            self.cand = None
            return False
        if self.cand is None or abs(self.cand[1] - z) > 0.05:
            self.cand = (t, z)
            return False
        if t - self.cand[0] < SETTLE_S:
            return False
        self.start = self.cand[0]
        self.z, self.n, self.cand = z, self.n + 1, None
        return True


def count_layers(tel, t0, t1):
    """Layers of the print inside [t0, t1] (LEAM ms): DataFrame n, start_ms, z."""
    m = tel.s["mot"]
    sel = (m["t"] >= t0) & (m["t"] <= t1)
    lc, rows = LayerCounter(), []
    for t, z, e in zip(m["t"][sel], m["z"][sel], m["ext"][sel]):
        if lc.update(t / 1000.0, z, bool(e)):
            rows.append((lc.n, lc.start * 1000.0, lc.z))
    while len(rows) > 1 and rows[1][2] - rows[0][2] < PRIME_STEP_MM:  # print 2's priming pass at Z 0.5 is not layer 1
        rows = [(n - 1, s, z) for n, s, z in rows[1:]]
    return pd.DataFrame(rows, columns=["n", "start_ms", "z"])


def layer_at(layers, tq):
    """Layer number (0 before the first) and seconds since that layer started / the previous layer's duration."""
    tq = np.asarray(tq, float)
    starts = layers.start_ms.values
    i = np.searchsorted(starts, tq, side="right") - 1
    n = np.where(i >= 0, i + 1, 0)
    t_in = np.where(i >= 0, (tq - starts[np.clip(i, 0, None)]) / 1000.0, np.nan) if len(starts) else np.full(len(tq), np.nan)
    prev = np.where(i >= 1, (starts[np.clip(i, 0, None)] - starts[np.clip(i - 1, 0, None)]) / 1000.0, np.nan) \
        if len(starts) else np.full(len(tq), np.nan)
    return n, t_in, prev


def band_of_layer(n, first_layers):
    """Band 1.. for layer numbers inside the band plan, 0 before its first layer or after its last."""
    n = np.asarray(n)
    if not first_layers:
        return np.zeros(n.shape, int)
    b = np.searchsorted(np.asarray(first_layers), n, side="right")
    return np.where((b >= 1) & (b <= len(first_layers) - 1), b, 0)


def leg_runs(tel, t0, t1):
    """Extruding samples of [t0, t1] with their leg class (index into LEG_DIRS, -1 between legs) and position."""
    m = tel.s["mot"]
    sel = (m["t"] >= t0) & (m["t"] <= t1) & (m["ext"] > 0) & (m["z"] < PRINT_Z_MAX)
    t, x, y, vx, vy = (m[c][sel] for c in ("t", "x", "y", "vx", "vy"))
    sp = np.hypot(vx, vy)
    d = np.abs(wrap_deg(np.degrees(np.arctan2(vy, vx))[:, None] - LEG_DIRS[None, :]))
    cls = np.where((d.min(1) <= LEG_TOL_DEG) & (sp >= MIN_SPEED_MM_S), d.argmin(1), -1)
    fresh = np.r_[True, (np.diff(x) != 0) | (np.diff(y) != 0)]  # position refreshes at half the motion rate
    return t, x, y, cls, fresh


def runs_of(cls):
    """(class, first index, last index) of runs of one leg class lasting at least MIN_RUN samples, neighbours merged."""
    out, i = [], 0
    while i < len(cls):
        j = i
        while j + 1 < len(cls) and cls[j + 1] == cls[i]:
            j += 1
        if cls[i] >= 0 and j - i + 1 >= MIN_RUN:
            if out and out[-1][0] == cls[i]:
                out[-1] = (cls[i], out[-1][1], j)
            else:
                out.append((int(cls[i]), i, j))
        i = j + 1
    return out


def loop_boundaries(tel, layers, t_end):
    """Per layer the time the second loop starts (LEAM ms), NaN when the layer's legs do not show two loops.
    The inner loop is printed first: legs 45, -90, 135, then 45 again starts the outer loop."""
    out = {}
    for k, (n, t0) in enumerate(zip(layers.n, layers.start_ms)):
        t1 = layers.start_ms.iloc[k + 1] if k + 1 < len(layers) else t_end
        t, _, _, cls, _ = leg_runs(tel, t0, t1)
        runs = runs_of(cls)
        out[int(n)] = math.nan
        for i in range(1, len(runs)):
            a, b = runs[i - 1], runs[i]
            if a[0] == 2 and b[0] == 0 and {r[0] for r in runs[:i]} >= {0, 1, 2}:
                out[int(n)] = 0.5 * (t[a[2]] + t[b[1]])
                break
    return out


def wall_spacings(tel, layers, bounds, t_end):
    """Centre-to-centre spacing of the two walls: per layer and leg, outer-loop minus inner-loop median offset across
    the leg (left of travel = outward for the clockwise print), legs trimmed LEG_TRIM_MM at both ends."""
    rows = []
    for k, (n, t0) in enumerate(zip(layers.n, layers.start_ms)):
        tb = bounds.get(int(n), math.nan)
        if not np.isfinite(tb):
            continue
        t1 = layers.start_ms.iloc[k + 1] if k + 1 < len(layers) else t_end
        t, x, y, cls, fresh = leg_runs(tel, t0, t1)
        for c, phi in enumerate(np.radians(LEG_DIRS)):
            ok = (cls == c) & fresh
            along, across = x[ok] * math.cos(phi) + y[ok] * math.sin(phi), -x[ok] * math.sin(phi) + y[ok] * math.cos(phi)
            late = t[ok] >= tb
            med = []
            for flag in (False, True):
                a, u_ = along[late == flag], across[late == flag]
                keep = (a >= a.min() + LEG_TRIM_MM) & (a <= a.max() - LEG_TRIM_MM) if len(a) else np.zeros(0, bool)
                med.append(np.median(u_[keep]) if keep.sum() >= 3 else math.nan)
            rows.append((int(n), c, float(LEG_DIRS[c]), med[1] - med[0]))
    return pd.DataFrame(rows, columns=["layer", "leg", "leg_deg", "spacing_mm"])


# plans and labels --------------------------------------------------------------------------------------------------
def band_plans(events, t0, t1):
    """Per band the runner's settings and label from the runner log (laptop ms in [t0, t1]); later runners win.
    A band that ran 'as printed' gets the baseline the runner read."""
    plan, labels, base = {}, [], {}
    for e in events:
        if not t0 <= e.get("t_ms", 0) <= t1:
            continue
        ev = e.get("event")
        if ev == "bands_start":
            labels = [p if isinstance(p, str) else "?" for p in e.get("plan") or []]
        elif ev == "baseline":
            base = {k: e[k] for k in ("flow", "speed", "nozzle_target", "fan") if k in e}
        elif ev == "band_start":
            b, s = e["band"], e.get("settings")
            plan[b] = {"plan": labels[b - 1] if b <= len(labels) else "?", "settings": s if isinstance(s, dict) else None}
    rows = {}
    for b, p in plan.items():
        s = p["settings"] if p["settings"] is not None else base
        rows[b] = {"plan": p["plan"], "plan_flow": s.get("flow", np.nan), "plan_speed": s.get("speed", np.nan),
                   "plan_nozzle": s.get("nozzle_target", np.nan), "plan_fan": s.get("fan", np.nan)}
    ref = {"plan_flow": base.get("flow"), "plan_speed": base.get("speed"), "plan_nozzle": base.get("nozzle_target")}
    for r in rows.values():
        r["kind"] = plan_kind(r, ref)
    return rows


def plan_kind(r, base):
    """Which lever a band moves relative to the baseline: flow, speed, nozzle or baseline."""
    for k, name in (("plan_flow", "flow"), ("plan_speed", "speed"), ("plan_nozzle", "nozzle")):
        if base.get(k) is not None and np.isfinite(r.get(k, np.nan)) and abs(r[k] - base[k]) > PLAN_TOL:
            return name
    return "baseline"


def sweep_windows(events):
    """Afternoon sweep: labelled windows (laptop ms) from the experiment runner's log."""
    rows, cur = [], None
    for e in events:
        if e.get("event") == "window_start":
            cur = {"band": len(rows) + 1, "plan": e["label"], "t0": e["t_ms"], "plan_flow": e.get("flow", np.nan),
                   "plan_nozzle": e.get("nozzle_target", np.nan)}
        elif e.get("event") == "window_end" and cur:
            rows.append({**cur, "t1": e["t_ms"]})
            cur = None
    return rows


def read_calliper(capture_dir):
    """Calliper readings per band (rows with a value only): readings 1-2, their mean, how many there are, gap-between-walls flag."""
    path = Path(capture_dir) / "measurements" / "measurements.csv"
    if not path.exists():
        return pd.DataFrame()
    d = pd.read_csv(path)
    for c in ("reading_1_mm", "reading_2_mm", "reading_3_mm"):
        if c in d:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[d.reading_1_mm.notna()].copy()
    cols = [c for c in ("reading_1_mm", "reading_2_mm", "reading_3_mm") if c in d]
    d["cal_mm"] = d[cols].mean(axis=1)
    d["cal_n"] = d[cols].notna().sum(axis=1)
    d["cal_gap"] = d["gap_between_walls"].eq("yes") if "gap_between_walls" in d else False
    return d.rename(columns={"reading_1_mm": "cal_mm_1", "reading_2_mm": "cal_mm_2"})[
        ["band", "cal_mm_1", "cal_mm_2", "cal_mm", "cal_n", "cal_gap"]].reset_index(drop=True)


def slicer_settings(capture_dir):
    """Layer height and line widths from a Cura gcode/ini in the capture's print-file/ folder; {} when none."""
    found = {}
    for p in sorted((Path(capture_dir) / "print-file").glob("*")):
        if p.suffix.lower() not in {".gcode", ".ini", ".txt", ".cfg"}:
            continue
        text = p.read_text(errors="ignore")[:400_000]
        for key, rx in (("layer_height", r"(?:layer_height|;Layer height:)\s*[=:]?\s*([0-9.]+)"),
                        ("line_width", r"(?<![a-z_])line_width\s*[=:]\s*([0-9.]+)"),
                        ("wall_line_width_0", r"wall_line_width_0\s*[=:]\s*([0-9.]+)"),
                        ("wall_line_width_x", r"wall_line_width_x\s*[=:]\s*([0-9.]+)"),
                        ("infill_line_width", r"infill_line_width\s*[=:]\s*([0-9.]+)")):
            m = re.search(rx, text)
            if m:
                found[key] = float(m.group(1))
    return found


# building ------------------------------------------------------------------------------------------------------------
def read_widths(cap, widths_dir, require_final=True):
    """width_check's per-frame scalars for a capture, or None (missing, or the interim schema without group / t_dep_ms)."""
    path = Path(widths_dir) / f"widths_{cap}.parquet"
    if not path.exists():
        return None
    w = pd.read_parquet(path)
    if require_final and not {"group", "t_dep_ms"} <= set(w.columns):
        print(f"  {path.name}: interim schema (no group / t_dep_ms), widths ignored", file=sys.stderr)
        return None
    check_aim(cap, w)
    w = w[["file"] + [c for c in WIDTH_COLS + PROVENANCE_COLS if c in w.columns]].copy()
    w["capture"] = cap
    return w


def check_aim(cap, w):
    """Refuse widths measured with a geometry not aimed for this capture (nozzle refits re-aim the extractor's window)."""
    from tools.model import width_check as wc

    path = wc.MODELS_DIR / wc.GEOMETRY_FILE
    if "geometry_version" not in w.columns or not path.exists():
        print(f"  widths_{cap}.parquet: geometry version or installed geometry unknown; aim not checked",
              file=sys.stderr)
        return
    installed, start = json.loads(path.read_text()), wc.capture_start(CAPTURES / cap)
    problems = [r for v in sorted(set(w.geometry_version.dropna())) for r in wc.aim_refusals(cap, start, v, installed)]
    if problems:
        raise SystemExit(f"widths_{cap}.parquet: {'; '.join(problems)}; re-measure it with the geometry of its aim")


def ir_rows(cap):
    rows = [{"capture": cap, "file": m.get("file"), "camera": m.get("camera_index"),
             "t_ms": m["source_utc_ns"] / 1e6 if isinstance(m.get("source_utc_ns"), (int, float)) else np.nan,
             "recv_ms": m.get("recv_ms"), "sequence": m.get("sequence")}
            for m in jsonl(CAPTURES / cap / "ir.jsonl") if m.get("file")]
    return pd.DataFrame(rows)


def frame_rows(captures, widths_dir, use_widths, verbose=False):
    """One row per IR frame of the recordings with width_check's columns where they exist."""
    rows = pd.concat([ir_rows(c) for c in captures], ignore_index=True)
    got = [w for w in (read_widths(c, widths_dir) for c in captures) if w is not None] if use_widths else []
    if got:
        rows = rows.merge(pd.concat(got, ignore_index=True), on=["capture", "file"], how="left")
        if verbose:
            print(f"  widths joined for {sorted({w.capture.iloc[0] for w in got})}")
        for c in PROVENANCE_COLS:
            if c in rows and rows[c].nunique() > 1:
                print(f"  warning: widths were made with several {c} values: {sorted(rows[c].dropna().unique())}",
                      file=sys.stderr)
    for c in WIDTH_COLS:
        if c not in rows:
            rows[c] = "" if c in ("reason", "source_frame_id", "group") else np.nan
    rows["reason"] = rows.reason.where(rows.reason.notna() & (rows.reason != ""), "no_width")
    rows["group"] = rows.group.fillna("")
    return rows


def print_windows(tels, prints):
    """LEAM-clock [t0, t1] of each print, from the laptop-clock marker times and the recordings' clock offsets."""
    return {pid: (local_ms(p.start) - np.median([tels[c].offset_ms for c in p.captures]),
                  local_ms(p.end) - np.median([tels[c].offset_ms for c in p.captures])) for pid, p in prints.items()}


def label_frames(F, pid, p, offset_ms, layers, plan, bounds, windows):
    """Layer, band, plan, pass and context columns for the frames of one print, all at the join time."""
    t = F.t_join_ms.values
    n, t_in, prev = layer_at(layers, t)
    F["layer"], F["layer_time_s"], F["layer_period_s"] = n, t_in, prev
    F["since_window_s"] = np.nan
    first_layer = np.zeros(len(F), bool)
    if pid == "afternoon":
        band, lap = np.zeros(len(F), int), t + offset_ms
        for w in windows:
            inside = (lap >= w["t0"]) & (lap < w["t1"])
            band[inside] = w["band"]
            F.loc[inside, "since_window_s"] = (lap[inside] - w["t0"]) / 1000.0
    else:
        band = band_of_layer(n, p.band_first_layers)
        first = {b: p.band_first_layers[b - 1] for b in range(1, len(p.band_first_layers))}
        first_layer = np.array([first.get(b) == l for b, l in zip(band, n)])
    F["band"] = band
    for col, default in (("plan", ""), ("kind", ""), ("plan_flow", np.nan), ("plan_speed", np.nan),
                         ("plan_nozzle", np.nan), ("plan_fan", np.nan)):
        F[col] = [plan.get(b, {}).get(col, default) if b else default for b in band]
    first_banded = p.band_first_layers[0] if p.band_first_layers else math.inf
    F.loc[band == 0, "plan"] = "none" if pid == "afternoon" else np.where(n[band == 0] < first_banded, "pre", "post")
    F.loc[band == 0, "kind"] = F.plan[band == 0]
    F["defect"] = np.isin(band, p.defect_bands)
    tb = np.array([bounds.get(int(l), math.nan) for l in n])
    F["pass"] = np.where(np.isfinite(tb), np.where(t < tb, "inner", "outer"), "") if p.walls == 2 else ""
    F["walls"] = p.walls if p.walls else np.nan
    F["_first_layer"] = first_layer
    return F


def finish_print(F, pid, p):
    noz_ok = (F.nozzle_c - F.nozzle_target_c).abs() <= NOZZLE_TOL_C
    window_ok = (F.since_window_s >= SETTLE_AFTER_WINDOW_S) if pid == "afternoon" else ~F._first_layer
    F["settled"] = ((F.band > 0) & window_ok & noz_ok).fillna(False).astype(bool)
    close = lambda a, b: (a - b).abs() <= PLAN_TOL
    ok = close(F.flow_pct, F.plan_flow) & close(F.nozzle_target_c, F.plan_nozzle) & (close(F.speed_pct, F.plan_speed) | F.plan_speed.isna())
    F["plan_ok"] = (ok & (F.band > 0)).fillna(False).astype(bool)
    z_fan = FAN_ON_BELOW_Z.get(pid)
    F["fan_on"] = np.where(F.fan_pct.notna(), F.fan_pct > 0, (F.z < z_fan) if z_fan else False).astype(bool)
    F["layer_h_mm"] = LAYER_H_MM
    F["line_w_mm"] = LINE_W_MM if p.walls else np.nan
    return F.drop(columns=["_first_layer"])


def finish_outside(F):
    for c, v in (("layer", 0), ("band", 0), ("plan", ""), ("kind", ""), ("pass", ""), ("defect", False), ("settled", False),
                 ("plan_ok", False), ("fan_on", False)):
        F[c] = v
    for c in ("layer_time_s", "layer_period_s", "since_window_s", "walls", "plan_flow", "plan_speed", "plan_nozzle", "plan_fan",
              "layer_h_mm", "line_w_mm"):
        F[c] = np.nan
    return F


def print_frames(F, pid, p, tel, win):
    """Join one print's frames to its telemetry and label them. Returns (frames, print info, labels for the band table)."""
    layers = count_layers(tel, *win)
    offset = tel.offset_ms
    windows, plan = [], {}
    if pid == "afternoon":
        windows = sweep_windows(jsonl(CAPTURES / p.captures[0] / "experiments.jsonl"))
        plan = {w["band"]: {"plan": w["plan"], "plan_flow": w["plan_flow"], "plan_nozzle": w["plan_nozzle"],
                            "kind": "flow" if w["plan"].startswith("flow") else "nozzle"} for w in windows}
    else:
        events = [e for c in p.captures if (CAPTURES / c / "bands.jsonl").exists() for e in jsonl(CAPTURES / c / "bands.jsonl")]
        plan = band_plans(events, win[0] + offset - 60_000, win[1] + offset + 60_000)
        for b, text in p.labels:  # band 11: the fan command was confirmed by the gateway but nothing changed
            plan[b].update(plan=text, kind="baseline", plan_fan=0.0)
    bounds = loop_boundaries(tel, layers, win[1]) if p.walls == 2 else {}
    st = state_at(tel, F.t_join_ms.values)
    for k in st:
        F[k] = st[k].values
    F = finish_print(label_frames(F, pid, p, offset, layers, plan, bounds, windows), pid, p)
    spacing = wall_spacings(tel, layers, bounds, win[1]) if p.walls == 2 else pd.DataFrame()
    return F, print_info(p, layers, spacing), {"plan": plan, "layers": layers, "bounds": bounds, "spacing": spacing}


def build_frames(captures, widths_dir=OUT, use_widths=True, verbose=True):
    """Frames table, per-print context and per-print labels. Prints whose recordings are not all given are skipped."""
    tels = {c: Telemetry.load(CAPTURES / c / "telemetry.jsonl") for c in captures}
    prints = {pid: p for pid, p in PRINTS.items() if all(c in tels for c in p.captures)}
    wins = print_windows(tels, prints)
    rows = frame_rows(captures, widths_dir, use_widths, verbose)
    rows["print_id"] = "outside"
    for pid, (t0, t1) in wins.items():
        rows.loc[(rows.t_ms >= t0) & (rows.t_ms <= t1) & rows.capture.isin(prints[pid].captures), "print_id"] = pid
    t_dep = rows.t_dep_ms.astype(float)
    dep_ok = t_dep.notna() & ((rows.t_ms - t_dep) > 0) & ((rows.t_ms - t_dep) <= MAX_DEP_LAG_MS)
    rows["t_join_ms"] = np.where(dep_ok, t_dep, rows.t_ms)
    rows["dep_joined"] = dep_ok
    parts, info, plans = [], {}, {}
    for c in captures:
        f = rows[(rows.print_id == "outside") & (rows.capture == c)].copy()
        st = state_at(tels[c], f.t_join_ms.values)
        for k in st:
            f[k] = st[k].values
        parts.append(finish_outside(f))
    for pid, p in prints.items():
        tel = Telemetry.concat([tels[c] for c in p.captures]).window(wins[pid][0] - 60_000, wins[pid][1] + 60_000)
        F, info[pid], plans[pid] = print_frames(rows[rows.print_id == pid].copy(), pid, p, tel, wins[pid])
        parts.append(F)
    frames = pd.concat(parts, ignore_index=True).sort_values("t_ms", kind="stable").reset_index(drop=True)
    frames["heading_deg"] = frames.heading_deg.fillna(frames.motion_heading_deg)
    blank = frames.group == ""
    frames.loc[blank, "group"] = [heading_group(c, h) for c, h in zip(frames.camera[blank], frames.heading_deg[blank])]
    return frames.drop(columns="motion_heading_deg"), info, plans


def print_info(p, layers, spacing):
    zs = layers.z.values
    wall = p.walls == 2
    info = {"captures": list(p.captures), "walls": p.walls, "start": p.start, "end": p.end, "n_layers": int(len(layers)),
            "layer_h_mm": float(np.round(np.median(np.diff(zs)), 3)) if len(zs) > 2 else None,
            "z_first_layer": float(zs[0]) if len(zs) else None, "line_w_mm": LINE_W_MM if p.walls else None,
            "line_w_source": "wall pitch measured from motion telemetry (Cura spaces walls by line width)" if wall else
            ("same Cura file as print 2, scaled; not measured" if p.walls else "unknown"),
            "slicer_settings_found": {c: slicer_settings(CAPTURES / c) for c in p.captures},
            "band_first_layers": list(p.band_first_layers)}
    if len(spacing):
        s = spacing[spacing.layer >= 3]
        v = s.spacing_mm.dropna()
        info["wall_spacing_mm"] = {
            "median": float(v.median()), "q1": float(v.quantile(0.25)), "q3": float(v.quantile(0.75)), "n": int(len(v)),
            "per_leg_median": {str(int(k)): float(x) for k, x in s.groupby("leg_deg").spacing_mm.median().items()},
            "method": "outer-loop minus inner-loop median offset across each leg (position telemetry, extruding, legs "
                      f"trimmed {LEG_TRIM_MM:g} mm at both ends), median over layers >= 3 and the 3 legs"}
    return info


# band aggregates -----------------------------------------------------------------------------------------------------
def usable(F):
    """Frames the statistics use. The part fan is excluded: it runs only in print 2's band 1 (below Z 6.2), which would
    otherwise mix two cooling states in one band (n_ok_fan_on in the band table counts what this drops)."""
    return F[(F.reason == "ok") & F.width_px.notna() & F.settled & F.plan_ok & ~F.fan_on]


def calliper_table():
    """Calliper readings of both golden prints keyed (print_id, band)."""
    parts = []
    for pid, p in PRINTS.items():
        d = read_calliper(CAPTURES / p.captures[-1]) if p.walls else pd.DataFrame()
        if len(d):
            parts.append(d.assign(print_id=pid))
    cols = ["print_id", "band", "cal_mm_1", "cal_mm_2", "cal_mm", "cal_n", "cal_gap"]
    return pd.concat(parts, ignore_index=True)[cols] if parts else pd.DataFrame(columns=cols)


def band_layers(p, band):
    f = p.band_first_layers
    return f[band - 1], f[band] - 1


def band_table(F, plans, cal=None):
    """Per (print, band, group, pass) width statistics, settings and context, with calliper and wall spacing."""
    rows = []
    for (pid, band, group), g in F[F.band > 0].groupby(["print_id", "band", "group"]):
        for pass_ in ["all", *(["inner", "outer"] if PRINTS[pid].walls == 2 else [])]:
            h = g if pass_ == "all" else g[g["pass"] == pass_]
            if h.empty:
                continue
            ok = h[(h.reason == "ok") & h.width_px.notna()]
            use = usable(h)
            q1, q3 = use.width_px.quantile([0.25, 0.75]) if len(use) else (np.nan, np.nan)
            rows.append({
                "print_id": pid, "captures": "+".join(sorted(h.capture.unique())), "band": int(band), "plan": h.plan.iloc[0],
                "kind": h.kind.iloc[0], "group": group,
                "camera": int(h.camera.iloc[0]), "pass": pass_, "walls": PRINTS[pid].walls, "defect": bool(h.defect.iloc[0]),
                "n_frames": len(h), "n_ok": len(ok), "n_ok_fan_on": int(ok.fan_on.sum()), "n_use": len(use),
                "n_layers": use.layer.nunique(),
                "width_px_median": use.width_px.median(), "width_px_q1": q1, "width_px_q3": q3, "width_px_iqr": q3 - q1,
                "width_px_median_all_ok": ok.width_px.median(), "mm_per_px_median": use.mm_per_px.median(),
                "contrast_median": use.contrast.median(), "confidence_median": use.confidence.median(),
                **{f"{c}_median": h[c].median() for c in ("flow_pct", "speed_pct", "speed_mm_s", "feed_mm_s", "nozzle_c",
                                                          "nozzle_target_c", "bed_c", "fan_pct", "substrate_c", "z",
                                                          "layer_time_s")},
                "plan_flow": h.plan_flow.iloc[0], "plan_speed": h.plan_speed.iloc[0], "plan_nozzle": h.plan_nozzle.iloc[0],
                "plan_fan": h.plan_fan.iloc[0], "fan_on_fraction": h.fan_on.mean(),
                "layer_min": int(h.layer.min()), "layer_max": int(h.layer.max()),
                "layer_h_mm": h.layer_h_mm.iloc[0], "line_w_mm": h.line_w_mm.iloc[0],
            })
    B = pd.DataFrame(rows)
    if B.empty:
        return B
    cal = calliper_table() if cal is None else cal
    if cal.duplicated(["print_id", "band"]).any():
        raise ValueError("several calliper rows for one (print, band), e.g. print 1's per-side readings once filled in: "
                         "average them or join by side before the band table")
    B = B.merge(cal, on=["print_id", "band"], how="left", validate="m:1")
    B["cal_gap"] = B.cal_gap.astype("boolean").fillna(False).astype(bool)
    B["cal_kind"] = np.where(B.cal_mm.isna(), "", np.where(B.walls == 2, "two_wall_thickness", "single_wall_side_unspecified"))
    B["wall_spacing_mm"] = np.nan
    for pid, pl in plans.items():
        if len(pl["spacing"]):
            for b in B[B.print_id == pid].band.unique():
                lo, hi = band_layers(PRINTS[pid], b)
                B.loc[(B.print_id == pid) & (B.band == b), "wall_spacing_mm"] = \
                    pl["spacing"][(pl["spacing"].layer >= lo) & (pl["spacing"].layer <= hi)].spacing_mm.median()
    B["bead_mm_cal"] = np.where(B.walls == 2, B.cal_mm - B.wall_spacing_mm, B.cal_mm)
    return B


# gate ----------------------------------------------------------------------------------------------------------------
def boot_contrast(a, b, n_boot=None, seed=0):
    """median(a) - median(b) of frame widths with a block bootstrap by layer; a, b: lists of per-layer width arrays."""
    n_boot = n_boot or N_BOOT
    rng = np.random.default_rng(seed)
    point = float(np.median(np.concatenate(a)) - np.median(np.concatenate(b)))
    d = np.empty(n_boot)
    for i in range(n_boot):
        ra = np.concatenate([a[j] for j in rng.integers(0, len(a), len(a))])
        rb = np.concatenate([b[j] for j in rng.integers(0, len(b), len(b))])
        d[i] = np.median(ra) - np.median(rb)
    return point, float(d.std(ddof=1)), [float(x) for x in np.percentile(d, [2.5, 97.5])]


def layer_blocks(U):
    return [g.width_px.values for _, g in U.groupby(["band", "layer"])]


def band_sd(base):
    """SD of the median width over the baseline bands (4/8/11/12 share every setting): how far two bands printed alike
    differ, from drift with height, heat and time as well as sampling, which a within-band bootstrap cannot see.
    None with fewer than GATE_MIN_BASELINE_BANDS bands that have enough frames and layers."""
    meds = [g.width_px.median() for _, g in base.groupby("band")
            if len(g) >= GATE_GROUP_MIN_FRAMES and g.layer.nunique() >= GATE_GROUP_MIN_LAYERS]
    return float(np.std(meds, ddof=1)) if len(meds) >= GATE_MIN_BASELINE_BANDS else None


def cell_contrast(Ua, Ub, sd=0.0):
    """Median width of Ua minus Ub in px. Each side is a few whole bands, so besides the layer-block bootstrap the contrast
    is judged against how far bands printed alike differ: SE = max(bootstrap, sd sqrt(1/bands in Ua + 1/bands in Ub)),
    sd = band_sd of the baseline bands (the SD of a band median already holds its sampling noise, so the two are not
    added). sd None (no baseline bands to measure it from) or too little data: 'insufficient'."""
    la, lb = layer_blocks(Ua), layer_blocks(Ub)
    row = {"n_a": len(Ua), "n_b": len(Ub), "layers_a": len(la), "layers_b": len(lb)}
    why = [w for w, bad in (("frames", min(len(Ua), len(Ub)) < GATE_GROUP_MIN_FRAMES),
                            ("layers", min(len(la), len(lb)) < GATE_GROUP_MIN_LAYERS),
                            ("baseline bands to measure band-to-band spread", sd is None)) if bad]
    if why:
        return {**row, "status": "insufficient", "why": " and ".join(why)}
    point, se_layers, ci = boot_contrast(la, lb)
    se_bands = sd * math.sqrt(1 / Ua.band.nunique() + 1 / Ub.band.nunique())
    se = max(se_layers, se_bands)
    return {**row, "status": "ok", "median_a_px": float(Ua.width_px.median()), "median_b_px": float(Ub.width_px.median()),
            "contrast_px": point, "se_layers_px": se_layers, "se_bands_px": se_bands, "se_px": se,
            "z": point / se if se > 0 else math.inf, "z_layers_only": point / se_layers if se_layers > 0 else math.inf,
            "ci95_px": [point + (x - point) * se / se_layers for x in ci] if se_layers > 0 else ci,
            "passes": bool(point > 0 and se > 0 and point >= GATE_SE * se)}


def baseline_agreement(base):
    """Do the baseline bands (flow/speed/nozzle at baseline) give the same width? Medians, spread, SE of a band vs the rest."""
    med = {int(b): float(g.width_px.median()) for b, g in base.groupby("band")}
    if len(med) < 2:
        return None
    ses = [boot_contrast(layer_blocks(g), layer_blocks(base[base.band != b]))[1] for b, g in base.groupby("band")
           if len(layer_blocks(g)) >= GATE_GROUP_MIN_LAYERS and len(layer_blocks(base[base.band != b])) >= GATE_GROUP_MIN_LAYERS]
    rest = {b: v for b, v in med.items() if b != 1}  # band 1 ran with the fan on below Z 6.2
    return {"medians_px": med, "n": {int(b): int((base.band == b).sum()) for b in med},
            "spread_px": float(max(med.values()) - min(med.values())),
            "spread_excl_band1_px": float(max(rest.values()) - min(rest.values())) if len(rest) > 1 else None,
            "deviation_from_median_px": {b: float(v - np.median(list(med.values()))) for b, v in med.items()},
            "median_se_px": float(np.median(ses)) if ses else None}


def gate(F):
    """Go/no-go: does the IR width follow real width? Print 2, flow 140 (band 10) minus flow 80 (band 2)."""
    P = usable(F[F.print_id == "print2"])
    out = {"question": "print 2, band 10 (flow 140) minus band 2 (flow 80), IR width in px, per camera-heading group and pass",
           "rule": f"GO when the contrast is positive and at least {GATE_SE:g} SE in at least {GATE_GROUPS} groups "
                   "(pass-pooled cell per group); SE = the larger of the layer-block bootstrap and sd sqrt(1/k_hi + 1/k_lo), "
                   "sd = SD of the medians of the baseline bands 4/8/11/12 (same settings), so band-to-band drift counts",
           "frames": "reason ok, settled (not a band's first layer, nozzle within 2 C of target), plan_ok (readbacks at plan), "
                     "part fan off",
           "cells": []}
    if P.empty:
        return {**out, "flow_ladder_px": {}, "verdict": "NO-GO", "why": "no usable print 2 frames"}
    for group in sorted(P.group.unique()):
        for pass_ in ("all", "inner", "outer"):
            G = P[(P.group == group) & ((P["pass"] == pass_) | (pass_ == "all"))]
            ref = G[G.band.isin(REF_BANDS)]
            sd = band_sd(ref)
            out["cells"].append({
                "group": group, "pass": pass_, "band_sd_px": sd, **cell_contrast(G[G.band == 10], G[G.band == 2], sd),
                "vs_baseline": {str(fl): cell_contrast(G[G.band == b], ref, sd) for fl, b in FLOW_BANDS.items()},
                "flow60_vs_band8": cell_contrast(G[G.band == 9], G[G.band == 8], sd),
                "aa": [{"bands": [a, b], **cell_contrast(ref[ref.band == b], ref[ref.band == a], sd)}
                       for a, b in itertools.combinations(REF_BANDS, 2)],
                "baselines": baseline_agreement(G[G.band.isin(BASELINE_BANDS)])})
    out["flow_ladder_px"] = {grp: {f"flow {fl}": [float(G.width_px.median()), int(len(G))]
                                   for fl, G in ((60, g[g.band == 9]), (80, g[g.band == 2]), (100, g[g.band.isin(REF_BANDS)]),
                                                 (120, g[g.band == 3]), (140, g[g.band == 10])) if len(G)}
                             for grp, g in P.groupby("group")}
    pooled = [c for c in out["cells"] if c["pass"] == "all" and c["status"] == "ok"]
    passing = [c["group"] for c in pooled if c["passes"]]
    out.update({"groups_passing": passing, "n_groups_passing": len(passing),
                "groups_passing_in_a_pass": sorted({c["group"] for c in out["cells"] if c["pass"] != "all"
                                                    and c["status"] == "ok" and c["passes"]}),
                "groups_evaluated": [c["group"] for c in pooled],
                "verdict": "GO" if len(passing) >= GATE_GROUPS else "NO-GO"})
    return out


# summary -------------------------------------------------------------------------------------------------------------
def cell_line(name, c, verdict=True):
    if c["status"] != "ok":
        return f"{name}  insufficient: {c['why']} (frames {c['n_b']}/{c['n_a']}, layers {c['layers_b']}/{c['layers_a']})"
    return (f"{name} {c['n_b']:4d} {c['n_a']:5d}  {c['median_b_px']:6.3f}  {c['median_a_px']:6.3f}  {c['contrast_px']:+7.3f}  "
            f"{c['se_layers_px']:6.3f} {c['se_bands_px']:6.3f} {c['se_px']:6.3f}  {c['z']:5.1f}"
            + (f"  {'yes' if c['passes'] else 'no'}" if verdict else ""))


def aa_lines(g):
    """Same-settings band pairs. The gate's sd comes from these very bands, so its z is bounded by construction (a
    consistency check, not independent evidence); the bracketed layers-only z shows what the old rule would have said."""
    L = ["\nA/A check: baseline band pairs (4/8/11/12 share every setting), later minus earlier, pass-pooled; "
         "z with the gate's SE, in brackets with the layer bootstrap alone"]
    tot = {"honest": 0, "layers": 0, "n": 0}
    for c in g["cells"]:
        if c["pass"] != "all":
            continue
        pairs = [x for x in c["aa"] if x["status"] == "ok"]
        if not pairs:
            continue
        L.append(f"  {c['group']:10s} baseline band SD {c['band_sd_px']:.3f} px: " + "; ".join(
            f"b{x['bands'][0]}-b{x['bands'][1]} {x['contrast_px']:+.3f} z {x['z']:+.1f} ({x['z_layers_only']:+.1f})" for x in pairs))
    for c in g["cells"]:
        for x in c["aa"]:
            if x["status"] == "ok":
                tot["n"] += 1
                tot["honest"] += abs(x["z"]) >= GATE_SE
                tot["layers"] += abs(x["z_layers_only"]) >= GATE_SE
    L.append(f"  all cells: {tot['n']} pairs, |z| >= {GATE_SE:g} in {tot['honest']} with the gate's SE, in {tot['layers']} "
             "with the layer bootstrap alone")
    return L


def summary_text(F, info, g):
    L = ["frames per print (ok = reason ok with a width; usable = ok, settled, readbacks at plan, part fan off)"]
    for pid, f in F.groupby("print_id"):
        ok = ((f.reason == "ok") & f.width_px.notna()).sum()
        L.append(f"  {pid:10s} frames {len(f):5d}  ok {ok:5d} ({100 * ok / max(len(f), 1):.1f} %)  usable {len(usable(f)):5d}")
    U = usable(F)
    if len(U):
        L += ["\nusable frames per band x group", U.groupby(["print_id", "band", "group"]).size().unstack("group", fill_value=0).to_string()]
    for pid, i in info.items():
        if "wall_spacing_mm" in i:
            s = i["wall_spacing_mm"]
            L.append(f"\nwall spacing {pid}: {s['median']:.3f} mm (IQR {s['q1']:.3f}-{s['q3']:.3f}, n {s['n']} layer x leg); "
                     f"per leg {s['per_leg_median']}")
        L.append(f"{pid}: {i['n_layers']} layers of {i['layer_h_mm']} mm, line width {i['line_w_mm']} mm ({i['line_w_source']})")
    if g:
        head = "group      pass    n lo  n hi  med lo  med hi  contrast  SE lay SE band  SE use      z"
        L += [f"\ngate: {g['rule']}", "flow 140 (hi) minus flow 80 (lo), px", head + "    pass"]
        L += [cell_line(f"{c['group']:10s} {c['pass']:6s}", c) for c in g["cells"] if c["n_a"] + c["n_b"] > 0]
        L += ["\nflow X (hi column) minus the baseline bands 4/8/11/12 (lo column), px, pass-pooled", head]
        for c in g["cells"]:
            if c["pass"] == "all":
                L += [cell_line(f"{c['group']:10s} f{fl:<4s}", cc, False) for fl, cc in c["vs_baseline"].items()
                      if cc["n_a"] + cc["n_b"] > 2]
        L.append("\nmedian px by flow % (pass-pooled; 100 = baseline bands 4/8/11/12; flow 120 is a defect band)")
        for grp, lad in g["flow_ladder_px"].items():
            L.append(f"  {grp:10s} " + "  ".join(f"{k.split()[1]}: {v[0]:.3f} (n {v[1]})" for k, v in lad.items()))
        L.append("\nbaseline bands 1/4/8/11/12, median px (pass-pooled; band 1 counts its fan-off frames only)")
        for c in g["cells"]:
            if c["pass"] == "all" and c.get("baselines"):
                b = c["baselines"]
                L.append(f"  {c['group']:10s} " + "  ".join(f"b{k} {v:.3f}" for k, v in b["medians_px"].items())
                         + f"  spread {b['spread_px']:.3f}, SE {b['median_se_px'] or float('nan'):.3f}")
        L += aa_lines(g)
        L.append(f"\nVERDICT: {g['verdict']} ({g.get('n_groups_passing', 0)} groups >= {GATE_SE:g} SE: {g.get('groups_passing')})")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("captures", nargs="*", default=sorted({c for p in PRINTS.values() for c in p.captures}))
    b.add_argument("--no-widths", action="store_true", help="printer state and labels only")
    b.add_argument("--widths", default=str(OUT), help="folder with width_check's widths_<capture>.parquet")
    b.add_argument("--out", default=str(OUT))
    sub.add_parser("gate").add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "build":
        F, info, plans = build_frames(a.captures, Path(a.widths), use_widths=not a.no_widths)
        B = band_table(F, plans)
        F.to_parquet(out / "frames.parquet")
        B.to_parquet(out / "bands.parquet")
        (out / "prints.json").write_text(json.dumps(info, indent=1, default=float))
        have_widths = bool(((F.reason == "ok") & F.width_px.notna()).any())
        g = gate(F) if have_widths else None
        (out / "gate.json").unlink(missing_ok=True)  # never leave a gate from an earlier build next to different frames
        if g:
            (out / "gate.json").write_text(json.dumps(g, indent=1, default=float))
        text = summary_text(F, info, g)
        (out / "dataset_summary.txt").write_text(text + "\n")
        print(text)
        if not have_widths:
            print("\nno final width columns found (run width_check first): frames and bands carry state and labels only")
    else:
        F = pd.read_parquet(out / "frames.parquet")
        g = gate(F)
        (out / "gate.json").write_text(json.dumps(g, indent=1, default=float))
        print(summary_text(F, {}, g))


if __name__ == "__main__":
    main()
