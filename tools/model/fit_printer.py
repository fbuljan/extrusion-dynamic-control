"""Fit the DDSP-style printer model (physics only) and write app/models/printer_model.json.

    .venv/bin/python -m tools.model.fit_printer [--beta fit|1] [--out PATH] [--no-write] [--boot N]

Physics block, the same formula as app/server/printer_model.py (parity-tested):
  asked width   w_ask  = w_nom[print] * line_w / LINE_W_REF * f ** beta      f = flow % / 100
  melt width    w_melt = Q220 * exp(alpha * (T - 220)) / (v * h)              v = travel mm/s, h = layer mm, T actual
  bead width    w      = softmin(w_ask, w_melt; softness)
M220 scales travel and extrusion together, so speed enters only through the melt limit. beta = 1 is plain volume
balance; beta < 1 says an under-extruded bead, no longer pressed by the nozzle, loses width slower than volume.
Dynamics: flow reaches the bead through a first-order pressure lag tau_p; the nozzle follows its target after a dead
time with a first-order heater lag tau_h.

Data (the dataset builder's data/model/): calliper per band from bands.parquet, band settings from the settled frames of frames.parquet.
Print 1 bead = calliper; print 2 bead = two-wall thickness - wall pitch, its absolute level weighted LEVEL_WEIGHT and its
band-to-band differences fully. Defect bands (hole between the walls: the calliper overstates them) count only as upper
bounds. tau_h from every nozzle target step in the telemetry. tau_p from flow steps in IR px, shape only (free level and
size per step and camera group): the afternoon sweep's, or print 2's band steps when the sweep leaves tau_p undetermined
(its only clean steps move the infill's IR by about the frame noise). Huber losses, LBFGS from fixed starts, alpha >= 0.
Validation: leave-one-band-out against a constant-per-print model and against the other beta choice (with and without
degenerate folds), each lever's effect against the bands, and a band bootstrap (within print) for intervals.

Limits (also written to the JSON's fit.notes with tonight's numbers):
- beta < 1 rests on print 2's flow-80 and flow-140 band differences; on print 1, and pooled level-free, beta = 1 does as
  well or better. Under beta < 1, Q220 is a capacity in width space, not a pure melt rate, and the flow knee at print 2 (~172 %)
  sits above the volume-balance knee (~130 %): expect wasted flow steps near the top of the range. Volume-consistent
  alternative for later: w = w_nom * softmin(f, Q220 / (w_nom * h * v)) ** beta.
- Q220 has a lower bound (~19 mm3/s) but no upper bound in these data; ~20 % less, or a feed above ~25.6 mm/s, makes
  print 2 melt-limited at baseline and the controller would use speed or nozzle instead of flow.
- tau_p <~ 2 s is all the data say: flow readbacks place each step only to +-0.7 s.
"""
import argparse
import itertools
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from tools.model.dataset import CAPTURES, OUT, Telemetry, jsonl

REPO = Path(__file__).resolve().parents[2]
MODEL_OUT = REPO / "app" / "models" / "printer_model.json"
REF_C = 220.0
LINE_W_REF = 1.5
FIT_PRINTS = ("print1", "print2")
DEFAULT_PRINT = "print2"  # Q3: the demo runs print 2's file and orientation
LEVEL_WEIGHT = 0.1  # print 2's absolute level rests on the wall pitch; its differences carry the information
HUBER_MM = 0.1  # calliper resolution
HUBER_C = 0.5
HUBER_PX = 0.2  # ~1.5 x the per-frame IR noise
AFTERNOON = "print-20261020-1531"
HEATER_CAPTURES = ("golden-20261020-1736", "golden2-20261020-1815", AFTERNOON)
HEATER_WINDOW_S = 60.0
TAU_P_GRID = np.geomspace(0.05, 60.0, 61)
torch.set_default_dtype(torch.float64)


# physics block -------------------------------------------------------------------------------------------------------
def softmin(a, b, s):
    return torch.minimum(a, b) - s * torch.log1p(torch.exp(-torch.abs(a - b) / s))


def width(p, f, v, temp, h, w_nom):
    """Steady bead width (mm); p holds tensors q220, alpha, softness, beta; inputs broadcast."""
    w_ask = w_nom * torch.clamp(f, min=0.0) ** p["beta"]
    w_melt = p["q220"] * torch.exp(p["alpha"] * (temp - REF_C)) / (v * h)
    return softmin(w_ask, w_melt, p["softness"])


def huber(x, delta):
    a = torch.abs(x)
    return torch.where(a <= delta, 0.5 * x ** 2, delta * (a - 0.5 * delta))


def minimize(params, loss_fn, iters=200):
    opt = torch.optim.LBFGS(params, lr=1, max_iter=iters, tolerance_grad=1e-12, tolerance_change=1e-14,
                            line_search_fn="strong_wolfe")

    def closure():
        opt.zero_grad()
        loss = loss_fn()
        loss.backward()
        return loss

    opt.step(closure)
    with torch.no_grad():
        return float(loss_fn())


# band points ---------------------------------------------------------------------------------------------------------
def band_points(data_dir=OUT):
    """One row per calliper band of prints 1 and 2: settings at the settled frames, bead mm, defect flag."""
    B = pd.read_parquet(Path(data_dir) / "bands.parquet")
    F = pd.read_parquet(Path(data_dir) / "frames.parquet")
    S = F[F.print_id.isin(FIT_PRINTS) & (F.band > 0) & F.settled & F.plan_ok & (F.extruding == 1)]
    st = S.groupby(["print_id", "band"]).agg(flow=("flow_pct", "median"), speed=("speed_pct", "median"),
                                             v=("speed_mm_s", "median"), nozzle_c=("nozzle_c", "median"),
                                             h=("layer_h_mm", "median"), line_w=("line_w_mm", "median"),
                                             n_frames=("t_ms", "size"))
    cal = B[B.print_id.isin(FIT_PRINTS)].groupby(["print_id", "band"]).agg(
        plan=("plan", "first"), kind=("kind", "first"), defect=("defect", "max"), cal_gap=("cal_gap", "max"),
        cal_mm=("cal_mm", "first"), cal_n=("cal_n", "first"), wall_spacing=("wall_spacing_mm", "first"),
        bead_cal=("bead_mm_cal", "first"))
    P = cal.join(st, how="inner").reset_index().rename(columns={"print_id": "print"})
    P = P[P.cal_mm.notna() & P.bead_cal.notna()].copy()
    P["bead"] = P.bead_cal
    P["upper"] = P.defect.astype(bool) | P.cal_gap.astype(bool)
    P["feed"] = P.v / (P.speed / 100.0)
    return P.sort_values(["print", "band"]).reset_index(drop=True)


def _tensors(P):
    t = {c: torch.tensor(P[c].to_numpy(float)) for c in ("flow", "v", "nozzle_c", "h", "bead")}
    t["line_w"] = torch.tensor(P.line_w.fillna(LINE_W_REF).to_numpy(float))
    t["upper"] = torch.tensor(P.upper.to_numpy(bool))
    t["pi"] = torch.tensor([FIT_PRINTS.index(p) for p in P["print"]])
    return t


# steady-state fit ----------------------------------------------------------------------------------------------------
STARTS = list(itertools.product((15.0, 20.0, 26.0), (0.008, 0.02), (0.03, 0.12), (0.5, 1.0)))


def _unpack(raw, beta_fixed):
    p = {"q220": torch.exp(raw["log_q220"]), "alpha": torch.exp(raw["log_alpha"]),  # hotter melt flows easier
         "softness": 0.005 + torch.exp(raw["log_soft"]),
         "beta": torch.exp(raw["log_beta"]) if beta_fixed is None else torch.tensor(float(beta_fixed))}
    return p, torch.exp(raw["log_w_nom"])


def steady_loss(raw, t, beta_fixed):
    p, w_nom = _unpack(raw, beta_fixed)
    pred = width(p, t["flow"] / 100, t["v"], t["nozzle_c"], t["h"], w_nom[t["pi"]] * t["line_w"] / LINE_W_REF)
    r = pred - t["bead"]
    p2, ok = t["pi"] == 1, ~t["upper"]
    level = r[p2 & ok].mean() if bool((p2 & ok).any()) else torch.tensor(0.0)
    r = torch.where(p2, r - level, r)
    loss = huber(r[ok], HUBER_MM).sum() + huber(torch.relu(r[~ok]), HUBER_MM).sum()
    return loss + LEVEL_WEIGHT * int((p2 & ok).sum()) * huber(level, HUBER_MM)


def fit_steady(P, beta_fixed=None):
    """Best of the fixed multi-start LBFGS fits; returns plain floats."""
    torch.manual_seed(0)
    t = _tensors(P)
    best = None
    for q, a, s, b in STARTS if beta_fixed is None else [st for st in STARTS if st[3] == 1.0]:
        raw = {"log_q220": torch.tensor(math.log(q)), "log_alpha": torch.tensor(math.log(a)),
               "log_soft": torch.tensor(math.log(s)),
               "log_beta": torch.tensor(math.log(b)), "log_w_nom": torch.tensor([math.log(1.6), math.log(1.5)])}
        for v in raw.values():
            v.requires_grad_(True)
        names = [k for k in raw if not (k == "log_beta" and beta_fixed is not None)]
        loss = minimize([raw[k] for k in names], lambda: steady_loss(raw, t, beta_fixed))
        if math.isfinite(loss) and (best is None or loss < best[0] - 1e-12):
            best = (loss, {k: v.detach().clone() for k, v in raw.items()})
    loss, raw = best
    p, w_nom = _unpack(raw, beta_fixed)
    out = {k: float(v) for k, v in p.items()}
    out["w_nom"] = dict(zip(FIT_PRINTS, map(float, w_nom)))
    out["loss"] = loss
    return out


def predict(fit, P):
    p = {k: torch.tensor(fit[k]) for k in ("q220", "alpha", "softness", "beta")}
    w_nom = torch.tensor([fit["w_nom"][x] for x in P["print"]]) * torch.tensor(
        P.line_w.fillna(LINE_W_REF).to_numpy(float)) / LINE_W_REF
    with torch.no_grad():
        return width(p, torch.tensor(P.flow.to_numpy(float)) / 100, torch.tensor(P.v.to_numpy(float)),
                     torch.tensor(P.nozzle_c.to_numpy(float)), torch.tensor(P.h.to_numpy(float)), w_nom).numpy()


def p2_level(fit, P):
    """Mean residual of print 2's non-defect bands: the part of its absolute level the fit left to the offset."""
    m = (P["print"] == "print2") & ~P.upper
    return float(np.mean(predict(fit, P[m]) - P.bead[m])) if m.any() else 0.0


# validation ----------------------------------------------------------------------------------------------------------
W_NOM_RANGE = (0.5, 3.0)  # a fold whose w_nom leaves this had no band left to set it (all melt-limited)


def lobo(P, beta_fixed, folds=None):
    """Leave one band out: refit without it, predict it; the constant model predicts its print's other bands' mean.
    folds: the band rows to hold out (default all)."""
    rows, fits = [], []
    for i in range(len(P)) if folds is None else folds:
        train, test = P.drop(index=i), P.loc[[i]]
        fit = fit_steady(train, beta_fixed)
        fits.append(fit)
        pr = float(predict(fit, test)[0])
        level = p2_level(fit, train) if test["print"].iloc[0] == "print2" else 0.0
        same = train[(train["print"] == test["print"].iloc[0]) & ~train.upper]
        rows.append({"print": test["print"].iloc[0], "band": int(test.band.iloc[0]), "plan": test.plan.iloc[0],
                     "upper": bool(test.upper.iloc[0]), "bead": float(test.bead.iloc[0]), "lobo": pr,
                     "lobo_level": pr - level, "const": float(same.bead.mean()),
                     "degenerate": not all(W_NOM_RANGE[0] <= w <= W_NOM_RANGE[1] for w in fit["w_nom"].values())})
    return pd.DataFrame(rows), fits


PARAM_KEYS = ["q220", "alpha", "softness", "beta", "w_nom_print1", "w_nom_print2"]


def _param(fit, k):
    return fit["w_nom"][k[6:]] if k.startswith("w_nom_") else fit[k]


def bootstrap(P, beta_fixed, n_boot, seed=0):
    """Refits on bands resampled with replacement within each print."""
    rng = np.random.default_rng(seed)
    fits = []
    for _ in range(n_boot):
        idx = np.concatenate([rng.choice(d.index, len(d)) for _, d in P.groupby("print")])
        fits.append(fit_steady(P.loc[idx].reset_index(drop=True), beta_fixed))
    return fits


def percentiles(values):
    return [float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))]


def lobo_metrics(L):
    """RMSE over non-defect bands, pooled and per print, with every fold and without the degenerate ones; level-free
    takes off the print-2 level the training fit left (what the incremental controller sees)."""
    rmse = lambda e: float(np.sqrt(np.mean(e ** 2)))  # noqa: E731
    out = {"degenerate_folds": [f"{r['print']} b{r.band}" for _, r in L[L.degenerate].iterrows()]}
    for folds, D in (("all_folds", L), ("without_degenerate", L[~L.degenerate])):
        ok = D[~D.upper]
        out[folds] = {name: {"n": len(d), "rmse_model": rmse(d.lobo - d.bead),
                             "rmse_model_level_free": rmse(d.lobo_level - d.bead), "rmse_const": rmse(d.const - d.bead)}
                      for name, d in [("pooled", ok), *ok.groupby("print")]}
    bounds = L[L.upper]
    out["upper_bounds_respected"] = f"{int((bounds.lobo <= bounds.bead + HUBER_MM).sum())}/{len(bounds)}"
    return out


def lever_effects(fit, P, lobo_fits=None):
    """Each lever band's effect vs the mean of its print's baseline bands: measured, fitted and held-out prediction."""
    rows = []
    for pr, d in P.groupby("print"):
        base = d[(d.kind == "baseline") & ~d.upper]
        ref = base.iloc[[0]].assign(flow=100.0, speed=100.0, v=float(base.feed.median()), nozzle_c=REF_C)
        for i, r in d[d.kind != "baseline"].iterrows():
            band = d.loc[[i]]
            fits = [("fit", fit)] + ([("lobo", lobo_fits[i])] if lobo_fits else [])
            row = {"print": pr, "band": int(r.band), "plan": r.plan, "upper": bool(r.upper),
                   "measured": float(r.bead - base.bead.mean()), "baseline_sd": float(base.bead.std(ddof=1))}
            for name, f in fits:
                row[name] = float(predict(f, band)[0] - predict(f, ref)[0])
            rows.append(row)
    return pd.DataFrame(rows)


# heater lag ----------------------------------------------------------------------------------------------------------
def heater_steps(captures=HEATER_CAPTURES):
    """Every nozzle target step between printing temperatures: times (s) since the step, nozzle C, start and target C."""
    steps = []
    for cap in captures:
        path = CAPTURES / cap / "telemetry.jsonl"
        if not path.exists():
            continue
        n = Telemetry.load(path).s["noz"]
        t, cur, tgt = n["t"], n["cur"], n["tgt"]
        idx = np.where(np.diff(tgt) != 0)[0]
        for k, i in enumerate(idx):
            t_old, t_new = tgt[i], tgt[i + 1]
            if not (190 <= t_old <= 260 and 190 <= t_new <= 260 and abs(t_new - t_old) >= 5):
                continue
            t0 = (t[i] + t[i + 1]) / 2  # the target changed somewhere in the readback gap
            t_end = min(t0 + HEATER_WINDOW_S * 1000, t[idx[k + 1]] if k + 1 < len(idx) else np.inf)
            before = cur[(t > t0 - 5000) & (t < t0)]
            sel = (t > t0) & (t < t_end) & np.isfinite(cur)
            if len(before) and sel.sum() >= 5:
                steps.append({"capture": cap, "t": (t[sel] - t0) / 1000, "cur": cur[sel],
                              "start": float(np.median(before)), "target": float(t_new)})
    return steps


def fit_heater(steps):
    """Shared first-order lag and dead time over the given steps."""
    tt = torch.tensor(np.concatenate([s["t"] for s in steps]))
    y = torch.tensor(np.concatenate([s["cur"] for s in steps]))
    a = torch.tensor(np.concatenate([np.full(len(s["t"]), s["start"]) for s in steps]))
    b = torch.tensor(np.concatenate([np.full(len(s["t"]), s["target"]) for s in steps]))
    best = None
    for tau0, d0 in itertools.product((5.0, 15.0), (0.5, 2.0)):
        log_tau, log_d = torch.tensor(math.log(tau0), requires_grad=True), torch.tensor(math.log(d0), requires_grad=True)

        def loss():
            tau, d = torch.exp(log_tau), torch.exp(log_d)
            pred = b + (a - b) * torch.exp(-torch.relu(tt - d) / tau)
            return huber(pred - y, HUBER_C).sum()

        val = minimize([log_tau, log_d], loss)
        if best is None or val < best[0]:
            best = (val, float(torch.exp(log_tau.detach())), float(torch.exp(log_d.detach())))
    return {"tau_h_s": best[1], "dead_h_s": best[2], "n_steps": len(steps), "n_samples": int(len(tt))}


# pressure lag --------------------------------------------------------------------------------------------------------
def readback_changes(tel, name, col, t0, t1):
    """(time, before, after) of each change of a readback series; the change happened inside the readback gap."""
    d = tel.s[name]
    m = (d["t"] > t0) & (d["t"] < t1) & np.isfinite(d[col])
    t, val = d["t"][m], d[col][m]
    return [((t[i] + t[i + 1]) / 2, float(val[i]), float(val[i + 1])) for i in np.where(np.diff(val) != 0)[0]]


def step_series(capture, print_id, t0=None, t1=None, data_dir=OUT, max_window_s=60.0):
    """IR frames around each flow step of a capture, cut at any other setting change; steps whose travel speed differs
    on the two sides are dropped (the IR glow follows speed at once, which would hide the lag)."""
    tel = Telemetry.load(CAPTURES / capture / "telemetry.jsonl")
    F = pd.read_parquet(Path(data_dir) / "frames.parquet")
    A = F[(F.capture == capture) & (F.print_id == print_id) & (F.reason == "ok") & F.dep_joined.astype(bool)
          & F.speed_mm_s.notna()].copy()
    t0 = A.t_dep_ms.min() if t0 is None else t0
    t1 = A.t_dep_ms.max() if t1 is None else t1
    flow = readback_changes(tel, "flow", "val", t0, t1)
    cuts = sorted([s[0] for s in flow] + [s[0] for s in readback_changes(tel, "noz", "tgt", t0, t1)]
                  + [(tel.s["mot"]["t"][i] + tel.s["mot"]["t"][i + 1]) / 2 for i in np.where(
                      np.diff(tel.s["mot"]["spd"]) != 0)[0] if t0 < tel.s["mot"]["t"][i] < t1] + [t0, t1])
    A["key"] = A.group + A["pass"].fillna("").map(lambda x: f":{x}" if x else "")
    rows, used = [], []
    for k, (ts, a, b) in enumerate(flow):
        lo = max([c for c in cuts if c < ts - 1] + [ts - max_window_s * 1000])
        hi = min([c for c in cuts if c > ts + 1] + [ts + max_window_s * 1000])
        W = A[(A.t_dep_ms > lo) & (A.t_dep_ms < hi)]
        pre, post = W[W.t_dep_ms < ts], W[W.t_dep_ms >= ts]
        if not len(pre) or not len(post) or abs(pre.speed_mm_s.median() - post.speed_mm_s.median()) > 1.0:
            continue
        for key, g in W.groupby("key"):
            if (g.t_dep_ms < ts).sum() >= 2 and (g.t_dep_ms >= ts).sum() >= 2:
                rows.append(g.assign(step=k, step_t=ts, cell=f"{k}:{key}"))
        used.append((ts, a, b))
    S = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return S, used


def afternoon_window(data_dir=OUT):
    """The sweep's flow part on LEAM's clock: experiment start to the first nozzle window."""
    ev = list(jsonl(CAPTURES / AFTERNOON / "experiments.jsonl"))
    tel = Telemetry.load(CAPTURES / AFTERNOON / "telemetry.jsonl")
    start = next(e["t_ms"] for e in ev if e["event"] == "experiment_start") - tel.offset_ms
    end = next(e["t_ms"] for e in ev if e["event"] == "window_start" and e["label"].startswith("nozzle")) - tel.offset_ms
    return start, end


def fit_pressure(S):
    """Per step and camera group: px = c + b * (1 - exp(-(t - t_step) / tau_p)) after the step; tau_p shared.
    Shape only (free level and size per cell); profiled over a grid for its 95 % interval."""
    cells = sorted(S.cell.unique())
    ci = torch.tensor([cells.index(c) for c in S.cell])
    dt = torch.tensor(((S.t_dep_ms - S.step_t) / 1000).to_numpy(float))
    y = torch.tensor(S.width_px.to_numpy(float))

    def fit_at(tau):
        u = 1 - torch.exp(-torch.relu(dt) / tau)
        c, b = torch.zeros(len(cells), requires_grad=True), torch.zeros(len(cells), requires_grad=True)
        loss = minimize([c, b], lambda: huber(c[ci] + b[ci] * u - y, HUBER_PX).sum())
        with torch.no_grad():
            return loss, (c[ci] + b[ci] * u - y).numpy()

    grid = list(TAU_P_GRID)
    prof = {tau: fit_at(tau) for tau in grid}
    k = int(np.argmin([prof[t][0] for t in grid]))
    for tau in np.geomspace(grid[max(k - 1, 0)], grid[min(k + 1, len(grid) - 1)], 21):
        prof[tau] = fit_at(tau)
    taus = sorted(prof)
    losses = np.array([prof[t][0] for t in taus])
    k = int(np.argmin(losses))
    r = prof[taus[k]][1]
    sigma = 1.4826 * float(np.median(np.abs(r - np.median(r))))
    # Near the optimum the Huber loss is ~0.5 * sum r^2, so 2 * dL / sigma^2 is ~chi-square(1).
    inside = [t for t, l in zip(taus, losses) if 2 * (l - losses[k]) / sigma ** 2 <= 3.84]
    return {"tau_p_s": float(taus[k]), "ci95_s": [float(min(inside)), float(max(inside))], "sigma_px": sigma,
            "n_frames": int(len(S)), "n_steps": int(S.step.nunique()), "n_cells": len(cells),
            "at_grid_edge": bool(min(inside) <= grid[0] or max(inside) >= grid[-1])}


# export --------------------------------------------------------------------------------------------------------------
def print_context(P):
    out = {}
    for pr, d in P.groupby("print"):
        base = d[d.kind == "baseline"]
        out[pr] = {"feed_mm_s": float(base.feed.median()), "layer_h_mm": float(base.h.median()),
                   "line_w_mm": float(base.line_w.fillna(LINE_W_REF).median())}
    return out


def lever_gains(fit, P):
    """Predicted width change from each print's baseline for the controller's typical steps."""
    ctx = print_context(P)
    rows = []
    for pr in FIT_PRINTS:
        base = {"print": pr, "flow": 100.0, "speed": 100.0, "nozzle_c": REF_C, "h": ctx[pr]["layer_h_mm"],
                "line_w": ctx[pr]["line_w_mm"]}
        cases = [("baseline", {})] + [(f"flow {d:+d}", {"flow": 100.0 + d}) for d in (-20, -10, 10, 20)] + \
                [(f"speed {d:+d}", {"speed": 100.0 + d}) for d in (-40, -20, -10)] + \
                [(f"nozzle {d:+d}", {"nozzle_c": REF_C + d}) for d in (-20, -10, -5, 5, 10, 20)]
        D = pd.DataFrame([{**base, **c} for _, c in cases])
        D["v"] = ctx[pr]["feed_mm_s"] * D.speed / 100
        w = predict(fit, D)
        rows += [{"print": pr, "case": name, "width": float(x), "change": float(x - w[0])} for (name, _), x in zip(cases, w)]
    return pd.DataFrame(rows)


def choose_tau_p(pressure):
    """The brief's source is the afternoon sweep; when its interval spans the whole grid it says nothing about tau_p."""
    return "afternoon" if not pressure["afternoon"]["at_grid_edge"] else "print2"


def notes(fit, P, metrics, pressure, boot):
    """What the numbers do and don't support, in words (printed and kept in the JSON)."""
    ctx = print_context(P)[DEFAULT_PRINT]
    w_nom, h, feed = fit["w_nom"][DEFAULT_PRINT], ctx["layer_h_mm"], ctx["feed_mm_s"]
    w_melt = fit["q220"] / (feed * h)
    knee_width = (w_melt / w_nom) ** (1 / fit["beta"])
    knee_volume = fit["q220"] / (w_nom * h * feed)
    q_low = min(m for m in [boot.get("q220", [math.inf])[0], *metrics["lobo_q220"]])
    shipped, other = metrics["shipped"], metrics["other"]

    def lf(m, folds, name):
        return m[folds][name]["rmse_model_level_free"]

    beta_note = "beta fixed at 1 (plain volume balance)" if fit["beta"] == 1.0 else (
        f"beta {fit['beta']:.2f} rests on print 2's flow-80 and flow-140 band differences; on print 1, and pooled "
        f"level-free, beta = 1 predicts held-out bands as well or better (without degenerate folds, level-free RMSE beta fit vs 1: "
        f"pooled {lf(shipped, 'without_degenerate', 'pooled'):.3f} vs {lf(other, 'without_degenerate', 'pooled'):.3f}, "
        f"print 1 {lf(shipped, 'without_degenerate', 'print1'):.3f} vs {lf(other, 'without_degenerate', 'print1'):.3f}, "
        f"print 2 {lf(shipped, 'without_degenerate', 'print2'):.3f} vs {lf(other, 'without_degenerate', 'print2'):.3f} mm;"
        f" degenerate folds of beta = 1: {', '.join(other['degenerate_folds']) or 'none'})")
    return {
        "beta": beta_note,
        "q220": f"q220 >= ~{math.floor(q_low * 2) / 2:g} mm3/s, upper bound unidentified (no band where print 2 is melt-limited by "
                f"flow). Under beta < 1 it is a capacity in width space: the asked width w_nom*f**beta is not the asked "
                f"volume, so q220 is not a pure melt rate",
        "flow_knee": f"{DEFAULT_PRINT} at baseline: asked {w_nom:.2f} mm, melt limit {w_melt:.2f} mm. The flow knee is set "
                     f"by beta at {100 * knee_width:.0f} % flow; by volume (f*w_nom*h*v = q220) it would be "
                     f"{100 * knee_volume:.0f} %, so expect wasted flow steps near the top of the range (the controller's "
                     f"ineffective-lever check covers them). Volume-consistent alternative for later: "
                     f"w = w_nom * softmin(f, q220/(w_nom*h*v))**beta",
        "regime_flip": f"{DEFAULT_PRINT} turns melt-limited at baseline if q220 is "
                       f"{100 * (1 - w_nom * h * feed / fit['q220']):.0f} % lower or the feed exceeds "
                       f"{fit['q220'] / (h * w_nom):.1f} mm/s; the controller would then move speed or nozzle instead of flow",
        "tau_p": f"tau_p <~ 2 s, not separable from step timing (flow readbacks every ~1.4 s place each step to +-0.7 s; "
                 f"the fit rests on a few frames within 2 s of each step). Value kept for simulate(): "
                 f"{pressure['chosen']['tau_p_s']:.2f} s from {pressure['source']}; the afternoon sweep's interval spans "
                 f"{pressure['afternoon']['ci95_s'][0]:.2f}-{pressure['afternoon']['ci95_s'][1]:.0f} s",
    }


def model_json(fit, heater, pressure, P, fits, metrics, beta_mode, boot, note):
    ctx = print_context(P)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    return {
        "version": f"physics-1 {stamp}",
        "formula": "w = softmin(w_nom*line_w/line_w_ref*(flow/100)**beta, q220*exp(alpha*(T-ref_c))/(v*h); softness),"
                   " v = feed_mm_s*speed/100",
        "ref_c": REF_C, "line_w_ref_mm": LINE_W_REF, "default_print": DEFAULT_PRINT,
        "params": {"q220_mm3_s": fit["q220"], "alpha_per_c": fit["alpha"], "softness_mm": fit["softness"],
                   "beta": fit["beta"], "tau_p_s": pressure["chosen"]["tau_p_s"], "tau_h_s": heater["all"]["tau_h_s"],
                   "dead_h_s": heater["all"]["dead_h_s"]},
        "prints": {pr: {"w_nom_mm": fit["w_nom"][pr], **ctx[pr]} for pr in FIT_PRINTS},
        "uncertainty": {"lobo_range": {k: [min(_param(f, k) for f in fits), max(_param(f, k) for f in fits)]
                                       for k in PARAM_KEYS},
                        "bootstrap_95": {**boot, "q220": [boot["q220"][0], None]} if boot else {},
                        "tau_p_ci95_s": pressure["chosen"]["ci95_s"]},
        "fit": {"beta": beta_mode, "bands": int(len(P)), "upper_bound_bands": int(P.upper.sum()),
                "level_weight_print2": LEVEL_WEIGHT, "print2_level_offset_mm": p2_level(fit, P),
                "lobo": {"shipped": metrics["shipped"], "beta_alternative": metrics["other"]},
                "heater": heater, "tau_p_source": pressure["source"],
                "pressure": {k: pressure[k] for k in ("afternoon", "print2")}, "notes": note},
    }


# report --------------------------------------------------------------------------------------------------------------
def run(beta_mode="fit", out=MODEL_OUT, write=True, data_dir=OUT, verbose=True, n_boot=200):
    say = print if verbose else (lambda *a, **k: None)
    beta_fixed = None if beta_mode == "fit" else float(beta_mode)
    other_fixed = 1.0 if beta_fixed is None else None
    P = band_points(data_dir)
    fit = fit_steady(P, beta_fixed)
    P["pred"] = predict(fit, P)
    L, fits = lobo(P, beta_fixed)
    L_other, _ = lobo(P, other_fixed)
    boot_fits = bootstrap(P, beta_fixed, n_boot) if n_boot else []
    boot = {k: percentiles([_param(f, k) for f in boot_fits]) for k in PARAM_KEYS} if boot_fits else {}
    gains = lever_gains(fit, P)
    if boot_fits:
        g = np.array([lever_gains(f, P).change.to_numpy() for f in boot_fits])
        gains["lo"], gains["hi"] = np.percentile(g, 2.5, axis=0), np.percentile(g, 97.5, axis=0)
    metrics = {"shipped": lobo_metrics(L), "other": lobo_metrics(L_other), "lobo_q220": [f["q220"] for f in fits]}
    eff = lever_effects(fit, P, fits)
    hs = heater_steps()
    heater = {"all": fit_heater(hs), "heating": fit_heater([s for s in hs if s["target"] > s["start"]]),
              "cooling": fit_heater([s for s in hs if s["target"] < s["start"]])}
    pressure = {"afternoon": fit_pressure(step_series(AFTERNOON, "afternoon", *afternoon_window(data_dir),
                                                      data_dir=data_dir)[0]),
                "print2": fit_pressure(step_series("golden2-20261020-1815", "print2", data_dir=data_dir)[0])}
    pressure["source"] = choose_tau_p(pressure)
    pressure["chosen"] = pressure[pressure["source"]]
    note = notes(fit, P, metrics, pressure, boot)

    say(f"steady fit (beta {beta_mode}), loss {fit['loss']:.4f}, print-2 level offset {p2_level(fit, P):+.3f} mm")
    for k in PARAM_KEYS:
        lo, hi = min(_param(f, k) for f in fits), max(_param(f, k) for f in fits)
        b = f"  bootstrap 95% {boot[k][0]:.4f}..{'unidentified' if k == 'q220' else f'{boot[k][1]:.4f}'}" if boot else ""
        say(f"  {k:14s} {_param(fit, k):9.4f}  leave-one-band-out range {lo:.4f}..{hi:.4f}{b}")
    for name, h in heater.items():
        say(f"heater ({name}): tau_h {h['tau_h_s']:.2f} s, dead time {h['dead_h_s']:.2f} s, {h['n_steps']} steps")
    for name in ("afternoon", "print2"):
        pr = pressure[name]
        say(f"pressure lag from {name} flow steps: tau_p {pr['tau_p_s']:.2f} s, profile 95% {pr['ci95_s'][0]:.2f}-"
            f"{pr['ci95_s'][1]:.2f} s{' (spans the grid: undetermined)' if pr['at_grid_edge'] else ''}; "
            f"{pr['n_frames']} frames, {pr['n_steps']} steps, {pr['n_cells']} step x group cells, "
            f"sigma {pr['sigma_px']:.3f} px")
    say(f"  -> tau_p from {pressure['source']}")
    say("\nbands: bead (mm), fit, held-out fit, constant model")
    for (_, r), (_, l) in zip(P.iterrows(), L.iterrows()):
        say(f"  {r['print']} b{r.band:<2d} {r.plan[:26]:26s} f{r.flow:4.0f} v{r.v:5.1f} T{r.nozzle_c:5.1f}  "
            f"bead {r.bead:.3f}{' (upper)' if r.upper else '        '}  fit {r.pred:.3f}  lobo {l.lobo:.3f}  "
            f"const {l.const:.3f}")
    other_name = "beta 1" if beta_fixed is None else "beta fit"
    say("\nleave-one-band-out RMSE over non-defect bands, mm: absolute / level-free (constant model)")
    for folds in ("all_folds", "without_degenerate"):
        degen = {n: metrics[k]["degenerate_folds"] for n, k in ((f"beta {beta_mode}", "shipped"), (other_name, "other"))}
        say(f"  {folds.replace('_', ' ')} (degenerate: {degen})")
        for name in ("pooled", "print1", "print2"):
            a, b = metrics["shipped"][folds][name], metrics["other"][folds][name]
            say(f"    {name:7s} n {a['n']:2d}/{b['n']:2d}  beta {beta_mode}: {a['rmse_model']:.3f} / "
                f"{a['rmse_model_level_free']:.3f}   {other_name}: {b['rmse_model']:.3f} / {b['rmse_model_level_free']:.3f}"
                f"   (const {a['rmse_const']:.3f})")
    say(f"  defect bands held out at or below their calliper upper bound (+{HUBER_MM}): "
        f"{metrics['shipped']['upper_bounds_respected']}")
    say("\nlever effects vs the print's baseline mean, mm")
    for _, r in eff.iterrows():
        say(f"  {r['print']} b{r.band:<2d} {r.plan[:12]:12s} measured {r.measured:+.3f}"
            f"{' (upper)' if r.upper else '        '}  fit {r.fit:+.3f}  held-out {r.lobo:+.3f}   "
            f"baseline SD {r.baseline_sd:.3f}")
    say("\npredicted change from each print's baseline, mm (bootstrap 95 %)")
    for pr, d in gains.groupby("print"):
        say(f"  {pr}: baseline {d.width.iloc[0]:.3f}")
        for _, r in d.iloc[1:].iterrows():
            say(f"    {r.case:10s} {r.change:+.3f}" + (f"  ({r.lo:+.3f}..{r.hi:+.3f})" if "lo" in d else ""))
    say("\nnotes")
    for k, v in note.items():
        say(f"  {k}: {v}")
    model = model_json(fit, heater, pressure, P, fits, metrics, beta_mode, boot, note)
    model["fit"]["lever_changes_mm"] = {f"{r['print']} {r.case}": [r.change] + ([r.lo, r.hi] if "lo" in gains else [])
                                        for _, r in gains.iloc[1:].iterrows() if r.case != "baseline"}
    if write:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(model, indent=1) + "\n")
        say(f"\nwrote {out}")
    return {"model": model, "points": P, "lobo": L, "effects": eff, "metrics": metrics}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--beta", default="fit", help="'fit' or a fixed exponent (1 = plain volume balance)")
    ap.add_argument("--out", default=str(MODEL_OUT))
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--boot", type=int, default=200, help="bootstrap refits for the parameter intervals (0: none)")
    a = ap.parse_args(argv)
    if not (OUT / "bands.parquet").exists():
        sys.exit(f"no dataset in {OUT}; build it with tools.model.dataset first")
    run(a.beta, a.out, not a.no_write, n_boot=a.boot)


if __name__ == "__main__":
    main()
