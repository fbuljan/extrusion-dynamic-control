"""Camera model fit: what the IR shows (bead width in px) for a real bead (mm, calliper), per camera-heading group.

    .venv/bin/python -m tools.model.fit_camera [--boot N] [--dry-run] [--absolute] [--linear | --exclude-over-asked]
                                               [--out FILE] [--variants-dir DIR]

Model, on pass-pooled band medians of usable frames (reason ok, settled, readbacks at plan) of the golden prints:
    px = k_g * min(mm, knee_p) + g_T * (nozzle_c - 220) + s_p * (speed_mm_s - 20) + o[g, p],   k_g = kappa * k_nom[g]
k_nom = 1 / the extractor's mm_per_px at the measured stretch: kappa is the gain over pure geometry, shared by all groups and
prints, as is g_T; the speed term s_p is per print (print 1 ran melt-limited, print 2 not) and the runtime uses its
profile's. Offsets per (group, print): a print's absolute level never sets a slope. The hinge (linear below the knee,
flat above) is print 2's one-sided response: beads wider than the 1.5 mm wall pitch merge with the neighbour wall and
stop reading wider. Print 1 (one wall) gets its own knee, fitted, linear allowed.
Golden mm: print 1 = calliper per band (one reading); print 2 = two-wall thickness - measured wall pitch, mean of two
readings, defect bands 3/7/9 out of the fit (predicted afterwards as a check), band 1 only from Z >= 6.7 (fan on below).
Weights: 1 / (SE_px^2 + (k * calliper SD)^2 + tau^2); SE_px is a layer-block bootstrap of the band median, tau the
band-level model scatter, set so the weighted residuals have unit variance. Knees by profile likelihood on a grid.
kappa is identified only by bands >= BELOW_MM under a knee (at least MIN_BELOW_BANDS of them).
Absolute or relative: the absolute model is written when the profile-likelihood 95 % interval of kappa
starts at >= 0.5 and >= 2 levers (flow, nozzle, ...) have bands below the knee whose leave-one-lever-out kappas agree
within 2x; else the relative one (k = k_nom, no glow or speed terms, no offsets, the measured baseline level as the
plateau, sigma widened by what that leaves uncorrected). --absolute overrides; `choice` in the file records which.
Runtime sigma: leave-one-band-out error below the knee, plus half the inner/outer pass difference (the runtime can't
tell passes apart), plus kappa's spread away from the knee; above the knee it ramps up to ABOVE_KNEE_SIGMA_MM.
Writes app/models/camera_model.json (gitignored: calibration stays local) and both variants to
data/model/camera_model_{absolute,relative}.json; prints the report tables. The band bootstrap is a diagnostic only.
--exclude-over-asked: bands whose flow above 100 % asks for more melt (flow x line width x layer height x travel speed)
than the printer model's capacity at the band's nozzle temperature leave the fit, like defect bands: the extra flow
can't come out as a steady wider bead, so their calliper is not the bead the IR saw (print 2 flow 140). A cold band
over capacity stays: its bead narrows to the melt limit and both instruments see that.
--linear (implies --exclude-over-asked): the runtime profile without a knee, so the file has no ceiling and a flat
sigma; print 1 keeps its own fitted knee (one wall, not a runtime profile; it only shares kappa and the glow term). A
speed term set by a single band only fits that band's calliper error, so the runtime then uses 0.
"""
import argparse
import functools
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "app")]
from server.geometry import GEOMETRY_FILE, MODELS_DIR, Geometry  # noqa: E402
from server.model_files import MODEL_FILES  # noqa: E402
from server.pipeline import aim_free, geometry_version, width_config_hash  # noqa: E402
from server.width import WidthExtractor  # noqa: E402

DATA = REPO / "data" / "model"
OUT = REPO / "app" / "models" / "camera_model.json"

PRINTS = ("print1", "print2")
PROFILE = "print2"  # runtime offsets: the demo repeats print 2's file and orientation (parked Q3)
REF_C, REF_SPEED = 220.0, 20.0
BAND1_MIN_Z = {"print2": 6.7}  # print 2 band 1 ran with the part fan on below Z 6.2
MIN_FRAMES, MIN_BANDS = 5, 3
EXCLUDE = {  # (group, print): why it never enters the fit nor the runtime
    ("cam0:135", "print1"): "baseline drifted -0.59 px within print 1 (golden RESULTS)",
    ("cam2:-90", "print2"): "the outer pass doesn't follow flow (gate: within noise; likely the neighbour wall), "
                            "and the runtime can't tell passes apart",
}
CAM0_GROUP, CAM0_MAX_SPREAD_PX = "cam0:135", 0.15  # cam0's hypotenuse view only if print 2's baselines agree
BASELINE_BANDS = {"print1": (4, 8), "print2": (1, 4, 8, 11, 12)}
CAL_SD_PRINT1 = 0.14  # one reading per band, 0.1 mm steps; baseline bands 4/8 read 1.4/1.6 at the same settings
CAL_SD_FLOOR = 0.053  # print 2: pooled SD of one reading over the non-defect bands' reading pairs
SE_FLOOR_PX = 0.01
KNEE_GRID = {"print2": np.round(np.arange(1.30, 1.8001, 0.01), 3),
             "print1": np.concatenate([[np.inf], np.round(np.arange(1.00, 1.8001, 0.02), 3)])}
BELOW_MM, MIN_BELOW_BANDS = 0.05, 2  # about one calliper SD: a band closer to the knee can't tell slope from plateau
TEMP_SPREAD_C, SPEED_SPREAD_MM_S = 2.0, 1.0  # g_T / s_p are fitted only when the rows vary that much
KAPPA_GRID = np.round(np.arange(0.0, 4.0001, 0.05), 3)
MIN_KAPPA, LEVER_AGREE = 0.5, 2.0
BIAS_FLOOR_PX = 1.95  # the extractor's bias curve with the IR palette: a vanishing line reads 1.95 px after blur and JPEG
LO_MARGIN_PX = 0.15  # just above the floor the reading follows width at half its normal slope
HI_SD = 1.5  # valid range ends this many frame SDs above the knee or the highest baseline band, whichever is higher
ABOVE_KNEE_SIGMA_MM = 0.25  # flow 140 (calliper +0.2 mm) reads like baseline: anything up to ~+0.25 mm is unseen
GLOW_RANGE_C, SPEED_RANGE_MM_S = 20.0, 8.0  # tested excursions: nozzle +-20 C, speed 60 % of 20 mm/s


# data ----------------------------------------------------------------------------------------------------------------
def usable_frames(frames):
    F = frames[(frames.reason == "ok") & frames.width_px.notna() & frames.settled & frames.plan_ok
               & frames.print_id.isin(PRINTS)].copy()
    for pid, zmin in BAND1_MIN_Z.items():
        F = F[~((F.print_id == pid) & (F.band == 1) & (F.z < zmin))]
    return F


def block_se(d, n_boot=300, seed=0):
    """SE of the band median, frames resampled in whole layers (frames of one layer are not independent)."""
    blocks = [g.width_px.values for _, g in d.groupby("layer")]
    if len(blocks) < 2:
        return np.nan
    rng = np.random.default_rng(seed)
    meds = [np.median(np.concatenate([blocks[j] for j in rng.integers(0, len(blocks), len(blocks))]))
            for _ in range(n_boot)]
    return float(np.std(meds, ddof=1))


def calliper(bands):
    C = bands[bands["pass"] == "all"].drop_duplicates(["print_id", "band"])
    C = C[C.print_id.isin(PRINTS)][["print_id", "band", "plan", "kind", "defect", "cal_mm_1", "cal_mm_2",
                                    "wall_spacing_mm", "bead_mm_cal"]]
    two = C.cal_mm_2.notna()
    one_sd = np.maximum(CAL_SD_FLOOR, (C.cal_mm_1 - C.cal_mm_2).abs() / math.sqrt(2))
    C["cal_sd"] = np.where(two, one_sd / math.sqrt(2), CAL_SD_PRINT1)
    return C.rename(columns={"print_id": "p", "band": "b", "bead_mm_cal": "mm"}).reset_index(drop=True)


def band_rows(F, C):
    rows = []
    for (p, g, b), d in F.groupby(["print_id", "group", "band"]):
        if len(d) < MIN_FRAMES:
            continue
        rows.append({"p": p, "g": g, "b": int(b), "n": len(d), "layers": d.layer.nunique(),
                     "px": float(d.width_px.median()), "se": max(SE_FLOOR_PX, np.nan_to_num(block_se(d), nan=0.1)),
                     "T": float(d.nozzle_c.median()), "v": float(d.speed_mm_s.median()), "z": float(d.z.median()),
                     "k_nom": float(1 / d.mm_per_px.median())})
    R = pd.DataFrame(rows).merge(C, on=["p", "b"], how="left")
    return R[R.mm.notna()].reset_index(drop=True)


def melt_load(F, printer):
    """Per (print, band): melt demand of the asked flow (mm^3/s) against the printer model's capacity at the band's
    nozzle temperature; over_asked when flow above 100 % pushes the demand past it."""
    p = printer["params"]
    b = F.groupby(["print_id", "band"]).agg(flow=("flow_pct", "median"), v=("speed_mm_s", "median"),
                                           T=("nozzle_c", "median"), h=("layer_h_mm", "median"),
                                           lw=("line_w_mm", "median")).reset_index()
    b["demand_mm3_s"] = b.flow / 100 * b.lw * b.h * b.v
    b["capacity_mm3_s"] = p["q220_mm3_s"] * np.exp(p["alpha_per_c"] * (b["T"] - float(printer.get("ref_c", 220.0))))
    b["melt_ratio"] = b.demand_mm3_s / b.capacity_mm3_s
    b["over_asked"] = (b.flow > 100) & (b.melt_ratio > 1)
    return b.rename(columns={"print_id": "p", "band": "b"})[
        ["p", "b", "flow", "demand_mm3_s", "capacity_mm3_s", "melt_ratio", "over_asked"]]


def fit_rows(R, over_asked=False):
    """Rows that enter the fit: not defect, not excluded, (with over_asked) not over-asked, and their (group, print)
    has >= MIN_BANDS such bands."""
    keep = ~R.defect & ~R.apply(lambda r: (r.g, r.p) in EXCLUDE, axis=1)
    if over_asked:
        keep &= ~R.over_asked.fillna(False).astype(bool)
    n = R[keep].groupby(["g", "p"]).b.transform("nunique")
    return R[keep][n >= MIN_BANDS].reset_index(drop=True)


# fit -----------------------------------------------------------------------------------------------------------------
def knee_grids(linear=False):
    """Knee grid per print; linear: the runtime profile without a knee."""
    return {**KNEE_GRID, PROFILE: np.array([np.inf])} if linear else KNEE_GRID


def coarse(grids):
    return {p: g[::2] for p, g in grids.items()}


def _h(R, knees):
    knee = R.p.map(knees).astype(float).values
    return np.minimum(R.mm.values, knee), (R.mm.values < knee).astype(float)


def below_knee(R, knees):
    """Distinct (print, band) whose calliper width sits >= BELOW_MM under its print's knee: the ones that carry kappa."""
    return R[R.mm.values < R.p.map(knees).astype(float).values - BELOW_MM][["p", "b"]].drop_duplicates()


def _design(R, knees, keys, fixed_kappa):
    h, slope = _h(R, knees)
    named_cols = {}
    if np.ptp(R["T"].values) > TEMP_SPREAD_C:
        named_cols["g_T"] = R["T"].values - REF_C
    for p in PRINTS:
        on = (R.p == p).values
        if on.any() and np.ptp(R.v.values[on]) > SPEED_SPREAD_MM_S:
            named_cols[f"s:{p}"] = (R.v.values - REF_SPEED) * on
    for g, p in keys:
        named_cols[("off", g, p)] = ((R.g == g) & (R.p == p)).values.astype(float)
    names, rest = list(named_cols), list(named_cols.values())
    if fixed_kappa:
        X = np.column_stack(rest)
        return X, X, names, h, slope
    # the rank test uses h alone: k_nom's 0.1-3 % band-to-band wobble must not pass for a slope
    return (np.column_stack([R.k_nom.values * h, *rest]), np.column_stack([h, *rest]), ["kappa", *names], h, slope)


def fit_fixed(R, knees, keys=None, n_iter=60, kappa=None):
    """Weighted least squares at fixed knees (and kappa, if given); tau (band-level scatter, px) re-estimated until
    the weights settle. Parameters a print's rows can't inform (no T or speed spread) stay 0 and are not listed."""
    keys = keys or sorted(set(zip(R.g, R.p)))
    X, Xr, names, h, slope = _design(R, knees, keys, kappa is not None)
    y = R.px.values - (0.0 if kappa is None else kappa * R.k_nom.values * h)
    dof = max(1, len(y) - X.shape[1] - (kappa is not None))  # same dof with kappa fixed or free: comparable nll
    k_now, tau = (1.5 if kappa is None else kappa), 0.05
    for _ in range(n_iter):
        var0 = R.se.values ** 2 + (k_now * R.k_nom.values * R.cal_sd.values * slope) ** 2
        sw = 1 / np.sqrt(var0 + tau ** 2)
        beta, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
        r = y - X @ beta
        k_new, tau_new = (beta[0] if kappa is None else kappa), _solve_tau(r, var0, dof)
        done = abs(k_new - k_now) < 1e-7 and abs(tau_new - tau) < 1e-7
        k_now, tau = k_new, tau_new
        if done:
            break
    var = var0 + tau ** 2
    cov = np.linalg.pinv((X / var[:, None]).T @ X)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    est = dict(zip(map(str, names), zip(beta, se)))
    named = {"kappa": est.get("kappa", (kappa, 0.0)), "g_T": est.get("g_T", (0.0, math.nan)),
             **{f"s:{p}": est.get(f"s:{p}", (0.0, math.nan)) for p in PRINTS}}
    offsets = {(n[1], n[2]): (b, e) for n, b, e in zip(names, beta, se) if isinstance(n, tuple)}
    n_below = len(below_knee(R, knees))
    rank_ok = kappa is None and np.linalg.matrix_rank(Xr) == Xr.shape[1]
    return {"named": named, "offsets": offsets, "tau": tau, "resid": r, "var": var, "keys": keys,
            "knees": dict(knees), "rank_ok": bool(rank_ok),
            "identified": bool(rank_ok and n_below >= MIN_BELOW_BANDS), "n_below": n_below, "n": len(y),
            "nll": 0.5 * float(np.sum(r ** 2 / var) + np.sum(np.log(var)))}


def _solve_tau(r, var0, dof):
    f = lambda t: float(np.sum(r ** 2 / (var0 + t ** 2))) - dof
    if f(0.0) <= 0:
        return 0.0
    lo, hi = 0.0, 1.0
    while f(hi) > 0:
        hi *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) > 0 else (lo, mid)
    return (lo + hi) / 2


def fit(R, grids=None, rounds=3, kappa=None):
    """Knees per print by coordinate-wise profile likelihood over their grids; returns the best fit_fixed."""
    grids = grids or KNEE_GRID
    prints = [p for p in PRINTS if p in set(R.p)]
    knees = {p: (1.5 if p == "print2" else np.inf) for p in prints}
    knees = {p: m if m in grids[p] else float(grids[p][0]) for p, m in knees.items()}
    best = fit_fixed(R, knees, kappa=kappa)
    for _ in range(rounds):
        before = dict(knees)
        for p in prints:
            for m in grids[p]:
                o = fit_fixed(R, {**knees, p: m}, best["keys"], kappa=kappa)
                if o["nll"] < best["nll"] - 1e-9:
                    best, knees = o, {**knees, p: m}
        if knees == before:
            break
    return best


def params(o):
    return o["named"], o["offsets"]


def knee_profile(R, best, p, grids=None):
    """nll over print p's knee grid with the other knees at the best fit: the profile-likelihood interval."""
    return [(float(m), fit_fixed(R, {**best["knees"], p: m}, best["keys"])["nll"]) for m in (grids or KNEE_GRID)[p]]


def kappa_profile(R, grid=KAPPA_GRID, grids=None):
    """nll with kappa fixed on a grid (knees, g_T, s, offsets re-fitted): ([(kappa, nll)], 95 % interval)."""
    prof = [(float(k), fit(R, coarse(grids or KNEE_GRID), rounds=2, kappa=k)["nll"]) for k in grid]
    nll0 = min(n for _, n in prof)
    inside = [k for k, n in prof if n <= nll0 + 1.92]
    return prof, (min(inside), max(inside))


def invert(o, rows):
    """mm from px per row with the fit's parameters; (clipped at the knee, unclipped). NaN for groups not in the fit."""
    named, offsets = params(o)
    kappa, gT = named["kappa"][0], named["g_T"][0]
    lin = np.array([(r.px - gT * (r["T"] - REF_C) - named[f"s:{r.p}"][0] * (r.v - REF_SPEED)
                     - offsets[(r.g, r.p)][0]) / (kappa * r.k_nom) if (r.g, r.p) in offsets else np.nan
                    for _, r in rows.iterrows()])
    knee = rows.p.map(o["knees"]).astype(float).values
    return np.minimum(lin, knee), lin


def lever_check(R, best, fit_fn=None):
    """Per lever (band kind) with bands under the knee: kappa refitted without that lever's under-knee bands, i.e.
    from the other levers alone (so one band under the knee is enough to count as identified here)."""
    fit_fn = fit_fn or fit
    below = below_knee(R, best["knees"]).merge(R[["p", "b", "kind"]].drop_duplicates(), on=["p", "b"])
    out = {}
    for lever, d in below.groupby("kind"):
        drop = set(zip(d.p, d.b))
        o = fit_fn(R[[(p, b) not in drop for p, b in zip(R.p, R.b)]].reset_index(drop=True))
        out[lever] = {"bands": sorted(f"{p} b{b}" for p, b in drop), "kappa_without": float(o["named"]["kappa"][0]),
                      "identified": o["rank_ok"] and o["n_below"] >= 1}
    return out


def decide(kappa_ci, levers, force=False):
    """Identifiability rule -> (write absolute?, choice record)."""
    ks = [v["kappa_without"] for v in levers.values()]
    agree = (len(levers) >= 2 and all(v["identified"] for v in levers.values()) and min(ks) > 0
             and max(ks) / min(ks) <= LEVER_AGREE)
    passed = kappa_ci[0] >= MIN_KAPPA and agree
    return passed or force, {
        "rule": f"absolute if the profile-likelihood 95 % interval of kappa starts at >= {MIN_KAPPA} and >= 2 levers "
                f"have bands >= {BELOW_MM} mm under the knee whose leave-one-lever-out kappas agree within "
                f"{LEVER_AGREE:g}x",
        "kappa_profile95": [float(kappa_ci[0]), float(kappa_ci[1])], "levers": levers, "levers_agree": bool(agree),
        "passed": bool(passed), "forced": bool(force and not passed),
        "written": "absolute" if passed or force else "relative"}


def bootstrap(R, n_boot, seed=1, grids=None):
    """Diagnostic: band ids resampled with replacement within each print; refit (coarser knee grid). Replicates that
    keep < MIN_BELOW_BANDS bands under a knee can't say anything about kappa: counted, not kept."""
    rng = np.random.default_rng(seed)
    grid = coarse(grids or KNEE_GRID)
    by_print = {p: sorted(R[R.p == p].b.unique()) for p in PRINTS if p in set(R.p)}
    out, unidentified = [], 0
    for _ in range(n_boot):
        parts = [R[(R.p == p) & (R.b == b)] for p, bands in by_print.items()
                 for b in rng.choice(bands, len(bands), replace=True)]
        o = fit(pd.concat(parts, ignore_index=True), grid, rounds=2)
        if not o["identified"]:
            unidentified += 1
            continue
        out.append({"kappa": o["named"]["kappa"][0], **{f"knee_{p}": o["knees"][p] for p in o["knees"]}})
    return pd.DataFrame(out), unidentified


# validation ----------------------------------------------------------------------------------------------------------
def leave_one_band_out(R, Rall, fit_fn=None):
    """Each (print, band) left out in turn, refit, invert its rows; the constant model predicts the group's mean
    calliper width of the remaining bands."""
    fit_fn = fit_fn or fit
    out = []
    for (p, b), _ in R.groupby(["p", "b"]):
        train = R[~((R.p == p) & (R.b == b))].reset_index(drop=True)
        o = fit_fn(train)
        test = Rall[(Rall.p == p) & (Rall.b == b) & Rall.apply(lambda r: (r.g, r.p) in set(o["keys"]), axis=1)]
        if test.empty:
            continue
        mm, _ = invert(o, test)
        const = [train[(train.g == r.g) & (train.p == r.p)].mm.mean() for _, r in test.iterrows()]
        out.append(test[["p", "g", "b", "plan", "defect", "mm", "cal_sd"]].assign(
            pred=mm, const=const, kappa=o["named"]["kappa"][0]))
    return pd.concat(out, ignore_index=True)


def leave_one_print_out(R, best):
    """kappa and g_T from one print, knees held at the joint fit; the held-out print's offsets from its baseline bands
    only (calliper anchor), its speed term 0 (speed is per print); then its other bands are predicted."""
    out = []
    for held in PRINTS:
        train = R[R.p != held].reset_index(drop=True)
        o = fit_fixed(train, {p: best["knees"][p] for p in set(train.p)})
        kappa, gT = o["named"]["kappa"][0], o["named"]["g_T"][0]
        test = R[R.p == held].copy()
        knee = best["knees"][held]
        h = np.minimum(test.mm.values, knee)
        base = test.b.isin(BASELINE_BANDS[held]).values
        rest = test.px.values - kappa * test.k_nom.values * h - gT * (test["T"].values - REF_C)
        test["off"] = test.g.map(pd.Series(rest[base]).groupby(test.g.values[base]).mean())
        lin = (test.px - gT * (test["T"] - REF_C) - test.off) / (kappa * test.k_nom)
        test["pred"] = np.minimum(lin, knee)
        test["const"] = test.g.map(test[base].groupby("g").mm.mean())
        out.append(test[~base].assign(train_kappa=kappa, train_identified=o["identified"]))
    return pd.concat(out, ignore_index=True)


def rmse(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else math.nan


def calliper_scatter(C):
    pairs = C[C.cal_mm_2.notna() & ~C.defect]
    d = (pairs.cal_mm_1 - pairs.cal_mm_2).values
    one = float(np.sqrt(np.mean(d ** 2) / 2))
    return {"print2_one_reading_mm": one, "print2_mean_of_two_mm": one / math.sqrt(2),
            "print2_baseline_band_sd_mm": float(C[(C.p == "print2") & C.b.isin(BASELINE_BANDS["print2"])].mm.std()),
            "print1_one_reading_mm": CAL_SD_PRINT1}


# runtime parameters --------------------------------------------------------------------------------------------------
def frame_sd(F, g, p, by=("band",)):
    """Robust per-frame SD (px) about the medians of `by` over print p's baseline bands for group g."""
    d = F[(F.group == g) & (F.print_id == p) & F.band.isin(BASELINE_BANDS[p])]
    dev = d.width_px - d.groupby(list(by)).width_px.transform("median")
    return float(1.4826 * dev.abs().median()) if len(d) else math.nan


def pass_difference(F, g, p):
    """Inner minus outer pass (px): median over bands with enough frames in both, vs the within-pass frame SD. The
    runtime can't tell passes apart, so a window seeing one pass is off by about half of it."""
    d = F[(F.group == g) & (F.print_id == p) & F["pass"].isin(["inner", "outer"])]
    if d.empty:
        return None
    n = d.groupby(["band", "pass"]).size().unstack()
    if not {"inner", "outer"} <= set(n.columns):
        return None
    m = d.groupby(["band", "pass"]).width_px.median().unstack()
    diff = (m.inner - m.outer)[(n.inner >= MIN_FRAMES) & (n.outer >= MIN_FRAMES)]
    if diff.empty:
        return None
    sd = frame_sd(F, g, p, by=("band", "pass"))
    med = float(diff.median())
    return {"inner_minus_outer_px": med, "bands": int(len(diff)), "within_pass_sd_px": sd,
            "exceeds_2sd": bool(abs(med) > 2 * sd)}


def cam0_baseline_spread(R):
    b = R[(R.g == CAM0_GROUP) & (R.p == PROFILE) & R.b.isin(BASELINE_BANDS[PROFILE])]
    return float(b.px.max() - b.px.min()) if len(b) else math.nan


def provenance(geometry_path=None, width_config=None):
    """What the app checks the model against: geometry.json's version and the extractor config's hash (pipeline.py's helpers)."""
    data = json.loads((Path(geometry_path) if geometry_path else MODELS_DIR / GEOMETRY_FILE).read_text())
    return {"geometry_version": geometry_version(data),
            "width_config_hash": width_config_hash(WidthExtractor(Geometry(data), width_config).cfg)}


def build_models(best, boot, F, R, loo, scatter, speed_bands_min=1):
    """(absolute, relative) model dicts with the same groups; main() picks one by the identifiability rule. The runtime
    speed term is 0 when fewer than speed_bands_min of the profile's bands ran off the reference speed."""
    named, offsets = params(best)
    kappa, gT = named["kappa"][0], named["g_T"][0]
    s = named[f"s:{PROFILE}"][0]
    prof = R[R.p == PROFILE]
    speed_bands = int(prof[(prof.v - REF_SPEED).abs() > SPEED_SPREAD_MM_S].b.nunique())
    s_note = None
    if speed_bands < speed_bands_min:
        s_note = (f"runtime speed term 0: {speed_bands} {PROFILE} band(s) off the reference speed can't tell a speed "
                  f"effect from their calliper error (fitted {s:+.4f} px per mm/s)")
        s = 0.0
    # robust spread: replicates that keep only a band or two under the knee scatter wide and would swamp a plain SD
    k_rel_sd = (1.4826 * float((boot.kappa - boot.kappa.median()).abs().median()) / kappa if len(boot) > 1
                else named["kappa"][1] / kappa)
    spread = cam0_baseline_spread(R)
    disabled = {f"{g}@{p}": why for (g, p), why in EXCLUDE.items()}
    absolute, relative = {}, {}
    for p in PRINTS:
        knee = best["knees"].get(p, np.inf)
        below = loo[(loo.p == p) & ~loo.defect & (loo.mm < knee - 0.02)]
        loo_mm = rmse(below.pred - below.mm) if len(below) >= 3 else 0.0
        ga, gr = {}, {}
        for (g, pp), (off, off_se) in offsets.items():
            if pp != p:
                continue
            if p == PROFILE and g == CAM0_GROUP and not spread <= CAM0_MAX_SPREAD_PX:
                disabled[f"{g}@{p}"] = f"baseline bands spread {spread:.3f} px > {CAM0_MAX_SPREAD_PX} px"
                continue
            rows = R[(R.g == g) & (R.p == p)]
            k_nom, sd = float(rows.k_nom.median()), frame_sd(F, g, p)
            res_px = math.hypot(best["tau"], float(rows.se.median()))
            pd_ = pass_difference(F, g, p)
            half_pass = abs(pd_["inner_minus_outer_px"]) / 2 if pd_ else 0.0
            common = {"k_nom_px_per_mm": k_nom, "floor_px": BIAS_FLOOR_PX, "frame_sd_px": sd,
                      "bands": int(rows.b.nunique()), "pass": pd_}
            k = kappa * k_nom
            knee_px = float(k * knee + off) if math.isfinite(knee) else None
            base_px = rows[rows.b.isin(BASELINE_BANDS[p])].px
            top = max(knee_px, float(base_px.max())) + HI_SD * sd if knee_px is not None else None  # every baseline in
            ga[g] = {**common, "k_px_per_mm": k, "offset_px": float(off), "offset_se_px": float(off_se),
                     "knee_mm": knee if math.isfinite(knee) else None, "knee_px": knee_px,
                     "valid_px": [BIAS_FLOOR_PX + LO_MARGIN_PX, top], "ramp_mm": sd / k, "k_rel_sd": k_rel_sd,
                     "sigma_mm": math.hypot(max(res_px / k, loo_mm), half_pass / k)}
            # relative: the measured baseline level is the plateau; glow and speed go uncorrected, so they join sigma
            plateau = float(base_px.median())
            gr[g] = {**common, "k_px_per_mm": k_nom, "offset_px": 0.0, "offset_se_px": 0.0,
                     "knee_mm": plateau / k_nom if math.isfinite(knee) else None,
                     "knee_px": plateau if math.isfinite(knee) else None,
                     "valid_px": [BIAS_FLOOR_PX + LO_MARGIN_PX, top], "ramp_mm": sd / k_nom, "k_rel_sd": 0.0,
                     "sigma_mm": math.sqrt(res_px ** 2 + (gT * GLOW_RANGE_C) ** 2 + (s * SPEED_RANGE_MM_S) ** 2
                                           + half_pass ** 2) / k_nom}
        if ga:
            absolute[p] = {"knee_mm": knee if math.isfinite(knee) else None, "groups": ga}
            relative[p] = {"knee_mm": None, "groups": gr}
    k_lo, k_hi = (np.percentile(boot.kappa, [2.5, 97.5]) if len(boot) else (math.nan, math.nan))
    base = {
        "version": datetime.now().strftime("camera-%Y%m%d-%H%M"),
        "profile": PROFILE,
        "ref": {"nozzle_c": REF_C, "speed_mm_s": REF_SPEED},
        "above_knee_sigma_mm": ABOVE_KNEE_SIGMA_MM,
        "disabled": disabled,
        "fitted": {"kappa": kappa, "kappa_se": named["kappa"][1], "kappa_boot95_bands": [float(k_lo), float(k_hi)],
                   "g_T_px_per_c": gT, "g_T_se": named["g_T"][1],
                   "s_px_per_mm_s": {p: named[f"s:{p}"][0] for p in PRINTS},
                   "s_se": {p: named[f"s:{p}"][1] for p in PRINTS},
                   "knees_mm": {p: (m if math.isfinite(m) else None) for p, m in best["knees"].items()},
                   "tau_px": best["tau"], "bands": int(R.groupby(["p", "b"]).ngroups), "rows": int(len(R)),
                   "boot_replicates": int(len(boot)), "calliper": scatter, "cam0_baseline_spread_px": spread,
                   **({"s_runtime_note": s_note} if s_note else {})},
    }
    a = {**base, "relative": False, "kappa": kappa, "g_T_px_per_c": gT, "s_px_per_mm_s": s, "profiles": absolute,
         "model": "px = k*min(mm, knee_mm) + g_T*(nozzle_c - 220) + s*(speed_mm_s - 20) + offset_px, k = kappa*k_nom",
         "sigma_note": "sigma_mm: band-level model error (leave-one-band-out) and half the inner/outer pass difference; "
                       "plus kappa's spread away from the knee at runtime. The calliper's absolute level (print 2, "
                       "about +-0.1 mm) is not in it"}
    r = {**base, "relative": True, "kappa": 1.0, "g_T_px_per_c": 0.0, "s_px_per_mm_s": 0.0, "profiles": relative,
         "model": "relative: mm = min(px, plateau_px) / k_nom: IR width in geometric mm, not real width",
         "sigma_note": "sigma_mm in geometric mm: band-level scatter plus the uncorrected glow (20 C) and speed "
                       "(8 mm/s) effects and half the pass difference; good for holding the current width only"}
    return a, r


# report --------------------------------------------------------------------------------------------------------------
def report(R, Rall, F, best, boot, unident, loo, lopo, knee_prof, kappa_ci, choice, models):
    named, offsets = params(best)
    L = []
    P = L.append
    P("golden bands (bead mm: print 1 calliper; print 2 thickness - wall pitch; * = defect, not fitted)")
    C = Rall.drop_duplicates(["p", "b"]).sort_values(["p", "b"])
    for _, r in C.iterrows():
        P(f"  {r.p} b{r.b:<2} {r.plan[:22]:22} {'*' if r.defect else ' '} mm {r.mm:.3f} +- {r.cal_sd:.3f}"
          f"  readings {r.cal_mm_1}/{'' if pd.isna(r.cal_mm_2) else r.cal_mm_2}")
    P(f"\nfit: {best['n']} band rows, {len(best['keys'])} (group, print) offsets, {best['n_below']} bands under a "
      f"knee; tau {best['tau']:.3f} px")
    for name, unit in (("kappa", ""), ("g_T", " px/C"), *((f"s:{p}", " px per mm/s") for p in PRINTS)):
        P(f"  {name:9} {named[name][0]:+.4f} +- {named[name][1]:.4f}{unit}")
    P(f"  kappa profile likelihood 95 % [{kappa_ci[0]:.2f}, {kappa_ci[1]:.2f}]")
    for p, m in best["knees"].items():
        prof = knee_prof.get(p, [])
        nll0 = min(x[1] for x in prof)
        inside = [x[0] for x in prof if x[1] <= nll0 + 1.92 and math.isfinite(x[0])]
        lin = [x[1] - nll0 for x in prof if not math.isfinite(x[0])]
        if not inside:
            P(f"  knee {p}: none (linear)")
            continue
        P(f"  knee {p}: {m:.2f} mm  profile95 [{min(inside):.2f}, {max(inside):.2f}]"
          + (f"  linear (no knee) +{lin[0]:.2f} nll" if lin else ""))
    if len(boot):
        q = boot.quantile([0.025, 0.5, 0.975])
        P(f"  band bootstrap (diagnostic): {len(boot)} kept, {unident} with < {MIN_BELOW_BANDS} bands under a knee "
          f"dropped; kappa {q.kappa.iloc[1]:.2f} [{q.kappa.iloc[0]:.2f}, {q.kappa.iloc[2]:.2f}], "
          f"P(kappa < {MIN_KAPPA}) {(boot.kappa < MIN_KAPPA).mean():.3f}")
    P("  levers under the knee, kappa refitted without each: " + "; ".join(
        f"{k} ({', '.join(v['bands'])}) {v['kappa_without']:.2f}{'' if v['identified'] else ' unidentified'}"
        for k, v in choice["levers"].items()))
    P("\nk vs nominal (k = kappa * k_nom), offsets, knee px at 220 C / 20 mm/s")
    for (g, p), (o, se) in offsets.items():
        rows = R[(R.g == g) & (R.p == p)]
        k_nom = rows.k_nom.median()
        knee = best["knees"][p]
        kp = f"{named['kappa'][0] * k_nom * knee + o:.3f}" if math.isfinite(knee) else "  -  "
        P(f"  {g:9} {p}  k_nom {k_nom:.3f}  k {named['kappa'][0] * k_nom:.3f} px/mm  offset {o:+.3f} +- {se:.3f}"
          f"  knee_px {kp}  bands {rows.b.nunique()}")
    P("\nresiduals px (fit rows)")
    P(R.assign(res=best["resid"]).pivot_table(index=["p", "b"], columns="g", values="res").round(3).to_string())
    P("\nleave-one-band-out, mm (model / constant = group's mean calliper of the other bands)")
    nd = loo[~loo.defect]
    for p in PRINTS:
        q2 = nd[nd.p == p]
        knee = best["knees"][p]
        lo, hi = q2[q2.mm < knee - 0.02], q2[q2.mm >= knee - 0.02]
        P(f"  {p}: all {rmse(q2.pred - q2.mm):.3f} / {rmse(q2.const - q2.mm):.3f}   below knee {rmse(lo.pred - lo.mm):.3f} /"
          f" {rmse(lo.const - lo.mm):.3f} (n {len(lo)})   at/above knee {rmse(hi.pred - hi.mm):.3f} / "
          f"{rmse(hi.const - hi.mm):.3f} (n {len(hi)})")
        for g, qg in q2.groupby("g"):
            P(f"    {g:9} {rmse(qg.pred - qg.mm):.3f} / {rmse(qg.const - qg.mm):.3f}  (n {len(qg)})")
    ks = loo.groupby(["p", "b"]).kappa.first()
    jack = math.sqrt((len(ks) - 1) / len(ks) * float(((ks - ks.mean()) ** 2).sum()))
    P(f"  jackknife over bands: kappa SE {jack:.3f} -> {named['kappa'][0] - 1.96 * jack:.2f} .. "
      f"{named['kappa'][0] + 1.96 * jack:.2f}")
    P("  kappa when a band is left out: " + ", ".join(f"{p[-1]}b{b} {k:.2f}" for (p, b), k in ks.items()))
    P("  per band error mm (pred - calliper):")
    P(nd.pivot_table(index=["p", "b"], columns="g", values="pred").sub(
        nd.pivot_table(index=["p", "b"], columns="g", values="mm")).round(3).to_string())
    P("\nleave-one-print-out, mm (kappa, g_T from the other print with knees held at the joint fit; offsets from this "
      "print's baseline bands; speed term 0)")
    for p, q3 in lopo.groupby("p"):
        P(f"  {p} (kappa from the other print {q3.train_kappa.iloc[0]:.2f}"
          f"{'' if q3.train_identified.iloc[0] else ', not identified'}): model {rmse(q3.pred - q3.mm):.3f}"
          f" / constant {rmse(q3.const - q3.mm):.3f}  (n {len(q3)})")
    scatter = models[0]["fitted"]["calliper"]
    P(f"\ncalliper scatter: print 2 one reading {scatter['print2_one_reading_mm']:.3f} mm, mean of two "
      f"{scatter['print2_mean_of_two_mm']:.3f} mm, baseline bands SD {scatter['print2_baseline_band_sd_mm']:.3f} mm;"
      f" print 1 one reading ~{scatter['print1_one_reading_mm']:.2f} mm")
    P("\ndefect bands (print 2), joint fit: predicted mm (unclipped) vs calliper")
    D = Rall[(Rall.p == "print2") & Rall.defect & Rall.apply(lambda r: (r.g, r.p) in offsets, axis=1)]
    if len(D):
        mm, lin = invert(best, D)
        for (_, r), a, b in zip(D.iterrows(), mm, lin):
            P(f"  b{r.b} {r.plan:10} {r.g:9} px {r.px:.3f}  pred {a:.3f} (lin {b:.3f})  calliper {r.mm:.3f}")
    O = Rall[Rall.over_asked.fillna(False).astype(bool) & ~Rall.defect
             & Rall.apply(lambda r: (r.g, r.p) in offsets, axis=1)] if "over_asked" in Rall else Rall[:0]
    if len(O) and not R.over_asked.fillna(False).astype(bool).any():
        P("\nover-asked bands (flow above 100 % past the melt capacity; out of the fit): predicted mm (unclipped) vs "
          "calliper")
        mm, lin = invert(best, O)
        for (_, r), a, b in zip(O.iterrows(), mm, lin):
            P(f"  {r.p} b{r.b} {r.plan:10} {r.g:9} melt demand {r.melt_ratio:.2f}x capacity  px {r.px:.3f}  pred "
              f"{a:.3f} (lin {b:.3f})  calliper {r.mm:.3f}")
    P("\ninner minus outer pass, print 2 (median over bands; within-pass frame SD)")
    for g in sorted(Rall[Rall.p == "print2"].g.unique()):
        d = pass_difference(F, g, "print2")
        if d:
            P(f"  {g:9} {d['inner_minus_outer_px']:+.3f} px over {d['bands']} bands, SD {d['within_pass_sd_px']:.3f}"
              f" px -> {'exceeds 2 SD' if d['exceeds_2sd'] else 'within 2 SD'}")
    P(f"\ncam0:135 baseline bands spread on {PROFILE}: {models[0]['fitted']['cam0_baseline_spread_px']:.3f} px "
      f"(limit {CAM0_MAX_SPREAD_PX})")
    for m in models:
        P(f"\nruntime profile {m['profile']}, {'RELATIVE (geometric mm)' if m['relative'] else 'absolute'}, speed term "
          f"{m['s_px_per_mm_s']:+.4f} px per mm/s" + (f" ({m['fitted']['s_runtime_note']})"
                                                      if m["fitted"].get("s_runtime_note") else ""))
        for g, e in m["profiles"].get(m["profile"], {}).get("groups", {}).items():
            knee = "-" if e["knee_px"] is None else f"{e['knee_mm']:.3f} mm / {e['knee_px']:.3f} px"
            P(f"  {g:9} k {e['k_px_per_mm']:.3f} px/mm  offset {e['offset_px']:+.3f}  knee {knee}  floor "
              f"{e['floor_px']:.3f}  valid {[None if v is None else round(v, 3) for v in e['valid_px']]}  frame SD "
              f"{e['frame_sd_px']:.3f} px  ramp {e['ramp_mm']:.3f} mm  sigma {e['sigma_mm']:.3f} mm")
    for k, why in models[0]["disabled"].items():
        P(f"  off {k}: {why}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--boot", type=int, default=300, help="band-bootstrap replicates (diagnostic and kappa spread)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--absolute", action="store_true", help="write the absolute model even if the identifiability rule fails")
    ap.add_argument("--width-config", type=json.loads, default={},
                    help="WidthExtractor overrides the widths were made with (default: none)")
    ap.add_argument("--linear", action="store_true",
                    help=f"no knee on the runtime profile ({PROFILE}): no ceiling; implies --exclude-over-asked")
    ap.add_argument("--exclude-over-asked", action="store_true",
                    help="leave out bands whose flow above 100 %% asks for more melt than the printer model's capacity")
    ap.add_argument("--printer-model", default=str(MODELS_DIR / MODEL_FILES["printer_model"]),
                    help="printer model whose melt capacity --exclude-over-asked uses (default: the installed one)")
    ap.add_argument("--variants-dir", help="where both variants are written (default: --data)")
    a = ap.parse_args(argv)
    over_asked = a.exclude_over_asked or a.linear
    data = Path(a.data)
    frames = pd.read_parquet(data / "frames.parquet")
    prov = provenance(width_config=a.width_config)
    measured = set(frames.geometry_version.dropna()) if "geometry_version" in frames else set()
    if any(aim_free(v) != aim_free(prov["geometry_version"]) for v in measured):
        sys.exit(f"widths were measured on geometry {sorted(measured)}, not the installed {prov['geometry_version']} "
                 "up to a nozzle refit; re-measure and rebuild the dataset")
    made_with = set(frames.width_config_hash.dropna()) if "width_config_hash" in frames else set()
    if made_with - {prov["width_config_hash"]}:
        sys.exit(f"widths were extracted with extractor config {sorted(made_with)}, not the installed "
                 f"{prov['width_config_hash']} (--width-config); re-measure and rebuild the dataset")
    F = usable_frames(frames)
    C = calliper(pd.read_parquet(data / "bands.parquet"))
    Rall = band_rows(F, C)
    melt, printer = None, None
    if over_asked:
        printer = json.loads(Path(a.printer_model).read_text())
        melt = melt_load(F, printer)
        Rall = Rall.merge(melt[["p", "b", "melt_ratio", "over_asked"]], on=["p", "b"], how="left")
    R = fit_rows(Rall, over_asked)
    grids = knee_grids(a.linear)
    fit_fn = functools.partial(fit, grids=grids)
    best = fit_fn(R)
    _, kappa_ci = kappa_profile(R, grids=grids)
    write_absolute, choice = decide(kappa_ci, lever_check(R, best, fit_fn), a.absolute)
    boot, unident = bootstrap(R, a.boot, grids=grids)
    loo = leave_one_band_out(R, Rall, fit_fn)
    lopo = leave_one_print_out(R, best)
    knee_prof = {p: knee_profile(R, best, p, grids) for p in best["knees"]}
    absolute, relative = build_models(best, boot, F, R, loo, calliper_scatter(C), speed_bands_min=2 if a.linear else 1)
    for m in (absolute, relative):
        m.update(prov, choice=choice, fit_mode="linear" if a.linear else "hinge")
        if a.linear and not m["relative"]:
            m["model"] = (f"px = k*mm + g_T*(nozzle_c - 220) + s*(speed_mm_s - 20) + offset_px, k = kappa*k_nom; "
                          f"{PROFILE} without a knee (knee_mm null: no ceiling)")
            m["sigma_note"] = ("sigma_mm: band-level model error (leave-one-band-out over the fitted bands) and half "
                               "the inner/outer pass difference, flat in mm. The calliper's absolute level (print 2, "
                               "about +-0.1 mm) is not in it")
        if melt is not None:
            out_ = melt[melt.over_asked]
            m["over_asked"] = {
                "rule": "out of the fit: flow above 100 % and melt demand (flow x line width x layer height x travel "
                        "speed) above the printer model's capacity q220*exp(alpha*(T-220)) at the band's nozzle C",
                "printer_model": printer.get("version"),
                "excluded": {f"{r.p} b{r.b}": round(float(r.melt_ratio), 3) for r in out_.itertuples()}}
    model = absolute if write_absolute else relative
    if melt is not None:
        print("melt demand vs the printer model's capacity per band (over_asked = out of the fit):")
        print(melt.round(3).to_string(index=False) + "\n")
    print(report(R, Rall, F, best, boot, unident, loo, lopo, knee_prof, kappa_ci, choice, (absolute, relative)))
    print(f"\nchoice: {choice['written']} (rule {'passed' if choice['passed'] else 'failed'}"
          f"{', forced by --absolute' if choice['forced'] else ''}); kappa profile95 {choice['kappa_profile95']}, "
          f"levers agree {choice['levers_agree']}; geometry {prov['geometry_version']}, width config "
          f"{prov['width_config_hash']}")
    if not a.dry_run:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(model, indent=1, default=float))
        variants = Path(a.variants_dir) if a.variants_dir else data
        variants.mkdir(parents=True, exist_ok=True)
        for name, m in (("absolute", absolute), ("relative", relative)):
            (variants / f"camera_model_{name}.json").write_text(json.dumps(m, indent=1, default=float))
        print(f"wrote {out} ({choice['written']}); both variants in {variants}")


if __name__ == "__main__":
    sys.exit(main())
