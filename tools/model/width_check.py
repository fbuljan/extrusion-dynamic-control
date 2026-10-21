"""Run the app's width extractor over recorded captures (IR frames + telemetry) and compare it with the research.

    .venv/bin/python -m tools.model.width_check [CAPTURE ...] [--research-bands bands_ir.csv]
                                                [--research-frames frames_w.pkl] [--geometry FILE]
                                                [--fit-nozzle [--force]]

Per capture: data/model/widths_<capture>.parquet (one row of scalars per IR frame, never images), coverage, runtime,
and per (camera, heading) group the width, confidence and how often the chosen ridge sits ~1.5 mm off the predicted
axis (print 2's second wall). For the first golden print also per-band medians and band contrasts on settled layers,
next to the research estimator's (bands_ir.csv) and frame by frame (its per-frame table). --fit-nozzle refits each
camera's nozzle pixel (where the bead leaves the nozzle) into app/models/geometry.json: cameras with too few frames keep
theirs, a move over 2 px needs --force, the old file is backed up. Aim only: the camera model stays valid, but each
capture has to be measured with the geometry of its own aim period (the golden prints: --geometry set to the backup from
before the refit), or its widths are not written (--no-write runs are always allowed). The width files record the
geometry version they were measured with, and dataset build checks it.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "app")]
from server.geometry import GEOMETRY_FILE, MODELS_DIR, Geometry  # noqa: E402
from server.pipeline import geometry_version, width_config_hash  # noqa: E402
from server.width import MotionBuffer, WidthExtractor, frame_time_ms, motion_from_telemetry  # noqa: E402

CAPTURES = REPO / "data" / "captures"
OUT = REPO / "data" / "model"
DEFAULT_CAPTURES = ["golden-20261020-1736", "golden2-20261020-1815", "print-20261020-1531"]
ARRIVAL_LAG_MS = 800  # IR previews arrive ~0.85 s after acquisition, so live motion is known that far past the frame
LOCK_ON_MM = (1.0, 2.0)  # chosen ridge this far off the predicted axis: likely print 2's other wall (1.5 mm)


def jsonl(path):
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:  # a recording may end mid-line
                pass


def settings_row(msg, recv_ms):
    s = msg.get("sources") or {}
    mo, du = s.get("motion") or {}, (s.get("duet_readback") or {}).get("payload") or {}
    nz = (du.get("temperatures") or {}).get("nozzle") or {}
    return {"recv_ms": recv_ms, "mot_ms": (mo.get("source_utc_ns") or 0) / 1e6,
            "flow": du.get("extrusion_percent") if du.get("extrusion_valid") else None,
            "nozzle_c": nz.get("current_c"), "override": (mo.get("payload") or {}).get("speed_override_percent")}


def run_capture(cap, extractor, limit=None, skip=0):
    """Feed telemetry and IR frames in LEAM-clock order, as the app would see them; returns (frames, settings)."""
    d = CAPTURES / cap
    frames = sorted((m for m in jsonl(d / "ir.jsonl") if frame_time_ms(m) and (d / m["file"]).exists()),
                    key=frame_time_ms)
    frames = frames[skip:skip + limit] if limit else frames[skip:]
    buf, rows, settings, k = MotionBuffer(), [], [], 0

    def flush(until_ms):
        nonlocal k
        while k < len(frames) and frame_time_ms(frames[k]) + ARRIVAL_LAG_MS <= until_ms:
            meta = frames[k]
            jpeg = (d / meta["file"]).read_bytes()
            t0 = time.perf_counter()
            motion = buf.at(frame_time_ms(meta))
            smp = extractor.process(jpeg, meta, motion)
            ms = 1000 * (time.perf_counter() - t0)
            m = motion or {}
            rows.append({"file": meta["file"], "sequence": meta.get("sequence"),
                         "published_utc_ms": meta.get("published_utc_ms"), **smp.__dict__, "runtime_ms": ms,
                         **{c: m.get(c) for c in ("x", "y", "z", "speed_pct", "speed_mm_s", "dist_since_turn_mm")},
                         **{c: v for c, v in extractor.last.items() if c != "dist_since_turn_mm"}})
            k += 1

    for r in jsonl(d / "telemetry.jsonl"):
        msg = r.get("msg") or {}
        settings.append(settings_row(msg, r.get("recv_ms")))
        got = motion_from_telemetry(msg)
        if got:
            buf.add(*got)
            flush(got[0])
        if k >= len(frames):
            break
    flush(float("inf"))
    W = pd.DataFrame(rows)
    for col in ("at_search_limit", "offset_px", "edge_l", "edge_r"):  # absent when no frame reached the search
        if col not in W:
            W[col] = float("nan")
    W["at_search_limit"] = W.at_search_limit.astype(float)  # NaN where the frame never got that far
    W["lateral_px"] = W.offset_px + 0.5 * (W.edge_l + W.edge_r)  # ridge centre from its edges: sub-bin
    W["lateral_mm"] = W.lateral_px * W.mm_per_px
    return W, pd.DataFrame(settings)


def report(cap, W):
    ok = W.reason == "ok"
    print(f"\n== {cap}: {len(W)} IR frames, ok {ok.sum()} ({100 * ok.mean():.1f} %)")
    print("   reasons:", ", ".join(f"{k} {v}" for k, v in W.reason.value_counts().items()))
    rt = W.runtime_ms
    print(f"   runtime per frame (motion lookup + extraction): median {rt.median():.2f} ms, "
          f"p95 {rt.quantile(0.95):.2f} ms, max {rt.max():.1f} ms; ok frames median {rt[ok].median():.2f} ms")
    print("   group        frames  ok   ok%   median px  IQR    mm/px  conf  lock-on%  neighbour<4px%  lateral px  limit%")
    for grp, g in W.groupby("group"):
        o = g[g.reason == "ok"]
        if len(o) < 5:
            continue
        q1, q3 = o.width_px.quantile([0.25, 0.75])
        lock = o.lateral_mm.abs().between(*LOCK_ON_MM).mean()
        nb = (o.neighbour_px.abs() < 4).mean()
        print(f"   {grp:11s} {len(g):6d} {len(o):5d} {100 * len(o) / len(g):5.1f}   {o.width_px.median():6.3f}  "
              f"{q3 - q1:5.3f}  {o.mm_per_px.median():5.3f}  {o.confidence.median():.2f}  {100 * lock:6.1f}  "
              f"{100 * nb:8.1f}  {o.lateral_px.median():+10.2f}  {100 * o.at_search_limit.mean():6.1f}")


# golden print 1: band plan, sides and settled-layer rules as in the research analysis --------------------------------
G1 = "golden-20261020-1736"
G1_PLAN = {1: (100, 100, 220), 2: (80, 100, 220), 3: (120, 100, 220), 4: (100, 100, 220), 5: (100, 60, 220),
           6: (100, 100, 200), 7: (100, 100, 240), 8: (100, 100, 220), 9: (100, 100, 220)}
G1_SIDES = {"leg_a": (0.0, 2), "hypotenuse": (135.0, 0), "leg_b": (-90.0, 1)}  # travel heading, trailing camera
RESEARCH_GROUP = {"leg_a": "leg_x/cam2", "hypotenuse": "hyp/cam0", "leg_b": "leg_y/cam1"}
CONTRASTS = [("flow120 - flow80", 3, 2), ("speed60 - baseline4", 5, 4), ("nozzle200 - baseline4", 6, 4),
             ("nozzle240 - baseline8", 7, 8), ("nozzle240 - nozzle200", 7, 6), ("baseline8 - baseline4", 8, 4),
             ("baseline4 - baseline1", 4, 1), ("after120 - baseline8", 9, 8)]
NOZZLE_TOL = 2.0


def g1_select(W, S):
    """Layer, band, side and settled flag per frame (flow/speed/nozzle at plan, not a band's first layer, nozzle steady)."""
    ev = list(jsonl(CAPTURES / G1 / "bands.jsonl"))
    i0 = max(i for i, e in enumerate(ev) if e.get("event") == "bands_start")
    L = pd.DataFrame([e for e in ev[i0:] if e.get("event") == "layer"]).reset_index(drop=True)
    W = W.copy()
    t_local = W.recv_ms - (W.published_utc_ms - W.t_ms)  # the layer log is on the laptop clock
    k = np.searchsorted(L.t_ms.values, t_local.values) - 1
    n = np.where(k >= 0, L.n.values[np.clip(k, 0, None)], 0)
    after = (k == len(L) - 1) & (W.z.fillna(0) > L.z.iloc[-1] + 0.25)
    W["layer"] = np.where(after, np.round(W.z.fillna(0) / 0.5), n).astype(int)
    W["band"] = np.where(W.layer > 0, np.minimum(9, (W.layer - 1) // 15 + 1), 0)
    settled = {}
    for i, r in L.iterrows():
        tgt = G1_PLAN[(int(r.n) - 1) // 15 + 1][2]
        nxt = L.nozzle[i + 1] if i + 1 < len(L) else r.nozzle
        settled[int(r.n)] = bool(((int(r.n) - 1) % 15 != 0 or r.n == 1) and abs(r.nozzle - tgt) <= NOZZLE_TOL
                                 and abs(nxt - tgt) <= NOZZLE_TOL)
    S = S[S.mot_ms > 0].sort_values("mot_ms")
    j = np.clip(np.searchsorted(S.mot_ms.values, W.t_ms.values) - 1, 0, len(S) - 1)
    for c in ("flow", "nozzle_c", "override"):
        W[c] = S[c].values[j].astype(float)
    plan = np.array([G1_PLAN.get(b, (np.nan,) * 3) for b in W.band])
    frame_ok = (W.flow == plan[:, 0]) & (W.override == plan[:, 1]) & ((W.nozzle_c - plan[:, 2]).abs() <= NOZZLE_TOL + 0.5)
    layer_ok = np.array([settled.get(int(n_), n_ > L.n.max()) for n_ in W.layer])
    W["settled"] = frame_ok & layer_ok & (W.layer >= 2)
    W["side"] = None
    for side, (hd, cam) in G1_SIDES.items():
        on = (((W.heading_deg - hd + 180) % 360 - 180).abs() < 15) & (W.camera == cam)
        W.loc[on, "side"] = side
    return W


def band_medians(df, col):
    return df.groupby(["side", "band"])[col].agg(["count", "median"])


def contrasts(med):
    return {name: med.get(a, np.nan) - med.get(b, np.nan) for name, a, b in CONTRASTS}


def g1_compare(W, S, bands_csv=None, frames_pkl=None, max_layer=None):
    W = g1_select(W, S)
    use = W.settled & W.side.notna() & (W.reason == "ok")
    if max_layer:
        use &= W.layer <= max_layer
    ours = band_medians(W[use], "width_px")
    R = pd.read_csv(bands_csv) if bands_csv else None
    F = None
    if frames_pkl:
        F = pd.read_pickle(frames_pkl)
        F = F[F.sbin == "18-28"][["file", "grad", "group"]].rename(columns={"grad": "research_px", "group": "research_group"})
        W = W.merge(F, on="file", how="left")
    print(f"\n== {G1}: settled layers" + (f" <= L{max_layer}" if max_layer else "")
          + ": per-band median width px (ours; research bands_ir.csv)")
    for side in ("leg_b", "leg_a", "hypotenuse"):
        cam = G1_SIDES[side][1]
        o = ours.loc[side] if side in ours.index.get_level_values(0) else pd.DataFrame(columns=["count", "median"])
        r = R[R.side == side].set_index("band") if R is not None else None
        print(f"   {side} / cam{cam}:  band   n  ours   | research n  median  diff")
        for b in sorted(set(o.index) | (set(r.index) if r is not None else set())):
            line = f"     {b}  " + (f"{int(o['count'][b]):3d}  {o['median'][b]:.3f}" if b in o.index else "  --   --  ")
            if r is not None and b in r.index:
                diff = o["median"][b] - r.median_px[b] if b in o.index else np.nan
                line += f"   | {r.n[b]:3d}  {r.median_px[b]:.3f}  {diff:+.3f}"
            print(line)
        if r is not None and len(o):
            common = sorted(set(o.index) & set(r.index))
            a, b_ = o["median"][common].values, r.median_px[common].values
            rank_a, rank_b = np.argsort(np.argsort(a)), np.argsort(np.argsort(b_))
            print(f"     max |diff| {np.max(np.abs(a - b_)):.3f} px; band ranks equal in {np.sum(rank_a == rank_b)}"
                  f"/{len(common)}; Spearman {pd.Series(a).corr(pd.Series(b_), method='spearman'):.2f}")
            co, cr = contrasts(o["median"]), contrasts(r.median_px)
            print("     contrasts (ours, research, diff): " + "; ".join(
                f"{k} {co[k]:+.3f} {cr[k]:+.3f} ({co[k] - cr[k]:+.3f})" for k in co))
    if F is not None:
        print("\n   frame by frame against the research estimator (its 18-28 px bin, same frames):")
        for side in ("leg_b", "leg_a", "hypotenuse"):
            g = W[W.research_group == RESEARCH_GROUP[side]]
            both = g[g.research_px.notna() & (g.reason == "ok")]
            dlt = both.width_px - both.research_px
            print(f"     {side}: research frames {len(g)}, ours ok on {len(both)} ({100 * len(both) / max(len(g), 1):.0f} %);"
                  f" median diff {dlt.median():+.3f}, median |diff| {dlt.abs().median():.3f}, "
                  f"p90 |diff| {dlt.abs().quantile(0.9):.3f} px; our reasons on the rest: "
                  + ", ".join(f"{k} {v}" for k, v in g[g.reason != "ok"].reason.value_counts().items()))
            sel = both[both.settled & ((both.layer <= max_layer) if max_layer else True)]
            m_o, m_r = sel.groupby("band").width_px.median(), sel.groupby("band").research_px.median()
            co, cr = contrasts(m_o), contrasts(m_r)
            print("       same frames, settled: band medians max |diff| "
                  f"{(m_o - m_r).abs().max():.3f} px; contrasts diff: "
                  + "; ".join(f"{k} {co[k] - cr[k]:+.3f}" for k in co))
    return W


# nozzle pixel fit -------------------------------------------------------------------------------------------------------
MAX_NOZZLE_SHIFT_PX = 2.0  # a bigger move is more likely a bad recording than a bumped camera


def solve_nozzle(W, geo, ex, min_frames=100):  # rarer directions are mostly camera-switch transients
    """Per camera, shift the bead origin in the plane so the predicted axis lands on the observed ridge.

    Each ok frame gives the ridge's lateral offset (px) from the predicted axis: offset = n . J^-1 delta, with J the
    plane/pixel Jacobian at the window and n the image normal. Directions far apart pin both components of delta; for
    near-parallel directions only the across part is identifiable and the truncated solution leaves the rest at 0.
    geo/ex carry the prior (no stored nozzle pixels). Returns {camera: (nozzle_px or None, usable frames)}."""
    W = W[(W.reason == "ok") & (W.offset_px.abs() < ex.cfg["lateral_search_px"] - 0.3)]
    fits = {}
    for cam in sorted(geo.cams):
        g = W[W.camera == cam]
        rows, rhs, notes = [], [], []
        for pdeg, gg in g.groupby((g.plane_deg / 5).round() * 5):
            if len(gg) < min_frames or gg.alpha_deg.abs().median() >= ex.cfg["angle_search_deg"] - 1:
                continue  # an angle pinned at the search limit: the bead there does not run along the heading
            lat = gg.lateral_px[(gg.lateral_px - gg.lateral_px.median()).abs() < 1.5]  # main ridge, no lock-ons
            ax = ex.predict_axis(cam, float(pdeg))
            nrm = np.array([-np.sin(ax["phi"]), np.cos(ax["phi"])])
            wgt = np.sqrt(min(len(lat), 200))
            rows.append(nrm @ np.linalg.inv(geo.jacobian(cam, ax["x"], ax["y"])) * wgt)
            rhs.append(float(lat.median()) * wgt)
            notes.append(f"{pdeg:+.0f} deg: n {len(gg)}, lateral {lat.median():+.2f} px")
        if not rows:
            fits[cam] = (None, len(g))
            continue
        A = np.array(rows)
        U, sv, Vt = np.linalg.svd(A, full_matrices=False)
        keep = sv >= 0.3 * sv[0]
        delta = Vt[keep].T @ ((U[:, keep].T @ np.array(rhs)) / sv[keep])
        px = geo.plane_to_pixel(cam, [geo.pixel_to_plane(cam, [geo.nozzle_px(cam)])[0] + delta])[0]
        fits[cam] = ((round(float(px[0]), 2), round(float(px[1]), 2)), len(g))
        print(f"cam{cam} vs the model prior: {'; '.join(notes)}; shift {np.round(delta, 2)} mm "
              f"({keep.sum()} of {len(sv)} components)")
    return fits


def apply_nozzle_fit(model, fits, path, force=False, now=None, captures=()):
    """Write fitted nozzle pixels into the geometry at path; returns the new model, or None when nothing was written.

    Cameras without a fit keep theirs; the old file is kept as a backup. The version gains "+nozzle-<minute>" for
    traceability only: the nozzle pixel just aims the window, so the camera model stays valid (the app's provenance
    check ignores the suffix). Captures filmed before it are at the old aim (see write_refusals)."""
    if path.exists() and without_aim(model) != without_aim(json.loads(path.read_text())):
        print(f"not written: the base geometry differs from {path} in more than the nozzle aim; installing it is not "
              "a nozzle refit (it needs a camera model refit)")
        return None
    geo, new, moved = Geometry(model), json.loads(json.dumps(model)), []
    for cam in sorted(geo.cams):
        before = tuple(round(float(v), 2) for v in geo.nozzle_px(cam))
        px, n = fits.get(cam, (None, 0))
        if px is None or not np.isfinite(px).all():
            print(f"cam{cam}: kept {before}, {n} frames{'' if px is None else f', fit {tuple(px)} not finite'}")
            continue
        shift = float(np.hypot(px[0] - before[0], px[1] - before[1]))
        new["cameras"][str(cam)]["nozzle_px"] = [px[0], px[1]]
        print(f"cam{cam}: {before} -> {tuple(px)}, shift {shift:.2f} px, {n} frames")
        if shift > MAX_NOZZLE_SHIFT_PX:
            moved.append(f"cam{cam}")
    if new["cameras"] == model["cameras"]:
        print("nothing refitted;", path, "unchanged")
        return None
    if moved and not force:
        print(f"not written: {', '.join(moved)} moved more than {MAX_NOZZLE_SHIFT_PX:g} px; check the recording, "
              "or rerun with --force to accept")
        return None
    now = now or datetime.now()
    new["version"] = f"{geometry_version(model)}+nozzle-{now:%Y%m%dT%H%M}"
    new["nozzle_fit_captures"] = {**fit_history(model), new["version"]: list(captures)}  # filmed before, at its aim
    if path.exists():
        backup = path.with_name(f"{path.name}.{now:%Y%m%dT%H%M%S}.bak")
        with open(backup, "xb") as f:  # never overwrite an earlier backup
            f.write(path.read_bytes())
        print("backup", backup)
    path.write_text(json.dumps(new, indent=1))
    print("wrote", path, "version", new["version"])
    print("aim only: the camera model stays valid; widths of captures filmed before this refit are written only with "
          "--geometry set to the backup from before it")
    return new


def fit_nozzle(caps, base=None, force=False, wide_px=7.0):
    path = MODELS_DIR / GEOMETRY_FILE
    model = json.loads(Path(base or path).read_text())
    prior = json.loads(json.dumps(model))
    for cm in prior["cameras"].values():
        cm.pop("nozzle_px", None)
    geo = Geometry(prior)
    ex = WidthExtractor(geo, {"lateral_search_px": wide_px, "angle_search_deg": 8.0})  # angle only to spot curved beads
    W = pd.concat([run_capture(cap, ex)[0].assign(capture=cap) for cap in caps])
    return apply_nozzle_fit(model, solve_nozzle(W, geo, ex), path, force, captures=caps) is not None


def capture_start(d):
    """Laptop time of a capture's first IR frame (metadata only), or None."""
    if not (Path(d) / "ir.jsonl").exists():
        return None
    for m in jsonl(Path(d) / "ir.jsonl"):
        if m.get("recv_ms"):
            return datetime.fromtimestamp(m["recv_ms"] / 1000)
    return None


def without_aim(model):
    m = json.loads(json.dumps(model))
    for k in ("version", "nozzle_fit_captures"):
        m.pop(k, None)
    for cm in m.get("cameras", {}).values():
        cm.pop("nozzle_px", None)
    return m


def fit_history(model):
    """{geometry version: captures its nozzle refit was fitted on}; a plain list belongs to the model's own version."""
    got = model.get("nozzle_fit_captures") or {}
    return dict(got) if isinstance(got, dict) else {geometry_version(model): list(got)}


def refit_time(stamp):
    return datetime.strptime(stamp, "%Y%m%dT%H%M")


def aim_refusals(cap, start, version, installed, used=None):
    """Why capture `cap` (first frame at `start`) is not at the aim of geometry `version` ([] when it is).

    Each nozzle refit starts an aim period at its timestamp, plus the captures it was fitted on (filmed just before it).
    `version` must be the installed geometry or one of its pre-refit backups (fit_camera stamps the installed version
    on whatever widths the dataset holds), and the capture must lie in that geometry's period, before the next refit."""
    iv = geometry_version(installed)
    chain, ichain = version.split("+nozzle-"), iv.split("+nozzle-")
    if ichain[:len(chain)] != chain:
        return [f"{cap}: geometry {version} is neither the installed {iv} nor one of its pre-refit backups"]
    fits = {**fit_history(used or {}), **fit_history(installed)}
    when = f"{start:%Y-%m-%d %H:%M}" if start else "at an unknown time"
    out = []
    if len(chain) > 1 and cap not in fits.get(version, ()) and (start is None or start < refit_time(chain[-1])):
        out.append(f"{cap} was filmed {when}, before the nozzle refit of {refit_time(chain[-1]):%Y-%m-%d %H:%M} "
                   f"({version})")
    if len(ichain) > len(chain):
        later = ["+nozzle-".join(ichain[:k]) for k in range(len(chain) + 1, len(ichain) + 1)]
        nxt = refit_time(ichain[len(chain)])
        if start is None or start >= nxt or any(cap in fits.get(v, ()) for v in later):
            out.append(f"{cap} was filmed {when}, at the aim of the nozzle refit of {nxt:%Y-%m-%d %H:%M} or later, "
                       f"not of {version}")
    return out


def write_refusals(caps, model, installed=None):
    """Why widths of these captures must not be written when measured with geometry `model` ([] when they may)."""
    return [r for cap in caps for r in aim_refusals(cap, capture_start(CAPTURES / cap), geometry_version(model),
                                                    installed or model, model)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="*", default=DEFAULT_CAPTURES)
    ap.add_argument("--research-bands", help="research bands_ir.csv for the first golden print")
    ap.add_argument("--research-frames", help="research per-frame widths (pickle with file, sbin, grad, group)")
    ap.add_argument("--max-layer", type=int, help="compare golden print 1 only up to this layer")
    ap.add_argument("--limit", type=int, help="first N frames per capture (quick runs)")
    ap.add_argument("--config", type=json.loads, default={}, help='extractor overrides, e.g. \'{"angle_search_deg": 0}\'')
    ap.add_argument("--no-write", action="store_true", help="skip the parquet files")
    ap.add_argument("--fit-nozzle", action="store_true")
    ap.add_argument("--base", help="geometry model to start the nozzle fit from (default: the installed one)")
    ap.add_argument("--force", action="store_true", help="write a nozzle refit even if a camera moves > 2 px")
    ap.add_argument("--geometry", help="measure with this geometry file instead of the installed one, e.g. the backup "
                                       "from before a nozzle refit for captures filmed at the old aim")
    a = ap.parse_args()
    if a.fit_nozzle:
        if a.geometry:
            ap.error("--fit-nozzle starts from --base, not --geometry")
        sys.exit(0 if fit_nozzle(a.captures, a.base, a.force) else 1)
    installed = MODELS_DIR / GEOMETRY_FILE
    geo = Geometry.load(a.geometry or installed)
    if not a.no_write:
        current = json.loads(installed.read_text()) if installed.exists() else None
        refused = write_refusals(a.captures, geo.model, current)
        if refused:
            print("not written:\n  " + "\n  ".join(refused) + "\nmeasure each capture with the geometry of its aim "
                  "(--geometry <a geometry.json.*.bak>), or check only with --no-write")
            sys.exit(1)
    ex = WidthExtractor(geo, a.config)
    OUT.mkdir(parents=True, exist_ok=True)
    for cap in a.captures:
        t0 = time.time()
        W, S = run_capture(cap, ex, a.limit)
        W["geometry_version"] = geometry_version(geo.model)
        W["width_config_hash"] = width_config_hash(ex.cfg)
        keep = [c for c in W.columns if W[c].dtype != object
                or c in ("file", "reason", "group", "source_frame_id", "geometry_version", "width_config_hash")]
        if not a.no_write:
            W[keep].to_parquet(OUT / f"widths_{cap}.parquet", index=False)
        report(cap, W)
        print(f"   wall time {time.time() - t0:.1f} s")
        if cap == G1:
            g1_compare(W, S, a.research_bands, a.research_frames, a.max_layer)


if __name__ == "__main__":
    main()
