"""Bead width in IR pixels from one trailing IR preview and the motion state at the frame's acquisition time.

Clocks: everything is on LEAM's clock. A frame's time is the IR preview's source_utc_ns (acquisition, ~0.85 s before
publish); motion is the telemetry motion source's own source_utc_ns. Around corners the image matches the motion state
~100 ms after source_utc_ns best (20 Oct prints); not corrected, it is inside the turn margin. IR frames are decoded in
memory only.
Estimator: the fresh bead's axis is predicted from the nozzle pixel and the travel heading through the geometry model,
refined by a small sideways search (an angle search is optional, off by default), then pixels at s 16-26 px behind the
nozzle glow are pooled by their distance across the axis (0.25 px bins), smoothed (sigma 0.5 px), and the width is the
gap between the steepest rising and falling edge of the ridge nearest the predicted axis. These defaults reproduce the
research estimator that was checked against the calliper.
"""
import io
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

import numpy as np
from PIL import Image

DEFAULTS = {
    "s_window_px": (16.0, 26.0),  # along the axis from the glow's foot on it = the research's 18-28 px on the legs
    "angle_search_deg": 0.0,  # +-8 fits frame noise: ~15 % more scatter on the golden prints
    "angle_step_deg": 1.0,
    "lateral_search_px": 1.0,  # print 2's second wall runs 1.5 mm (~2.5 px) beside the fresh bead
    "lateral_step_px": 0.25,
    "ridge_sigma_px": 1.0,
    "flank_px": (3.5, 6.0),
    "profile_half_px": 8.0,
    "bin_px": 0.25,
    "smooth_px": 0.5,
    "edge_search_px": 4.0,
    "base_px": (4.0, 7.0),
    "min_amp": 15.0,  # luma; below this the ridge is JPEG noise
    "min_ridge_luma": 4.0,  # a local maximum this little above the background is JPEG noise
    "min_edge_ratio": 0.15,  # an edge's slope (luma/px) must carry this share of the ridge height; real edges 0.28+
    "merge_px": 2.5,  # maxima closer than this, with a shallow dip between, are one bead (hot core past the luma peak)
    "ambiguous_px": 3.5,  # another resolved ridge this close: no telling which bead is the fresh one
    "max_width_px": 4.5,  # real beads read 2-3.5 px; wider ones saturate the palette and the edges stop meaning width
    "max_extent_px": 6.0,  # above half the ridge's height across this much: a saturated or merged blob, not one bead
    "min_speed_mm_s": 2.0,
    "turn_margin_mm": 5.0,  # the corner itself (rounding, seam) stays out of the measured stretch
    "max_view_angle_deg": 75.0,  # beads running further across or away from the camera are hidden or too foreshortened
}


@dataclass
class WidthSample:
    t_ms: int  # frame acquisition time, LEAM clock; 0 when the frame has none
    camera: int
    heading_deg: float | None
    axis_deg: float  # image direction of the bead leaving the nozzle (deg, y down); NaN if not predicted
    width_px: float | None
    mm_per_px: float | None
    contrast: float  # ridge peak above background, luma
    confidence: float
    reason: str
    recv_ms: int | None = None  # local receive time, for staleness
    source_frame_id: str | None = None
    group: str = ""  # camera and travel heading to 45 deg, e.g. "cam2:-90": widths are comparable within a group
    t_dep_ms: int | None = None  # when the measured bead stretch was laid down, LEAM clock


def _finite(v):
    return isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool) and math.isfinite(v)


def frame_time_ms(meta):
    """Acquisition time (ms, LEAM clock) from the IR preview's source_utc_ns, or None."""
    ns = meta.get("source_utc_ns")
    return ns / 1e6 if _finite(ns) and ns > 0 else None


def motion_from_telemetry(msg):
    """(t_ms, state) of the telemetry message's motion source, or None when it is missing or invalid."""
    src = (msg.get("sources") or {}).get("motion") or {}
    ts = src.get("source_utc_ns")
    if src.get("valid") is not True or not isinstance(ts, (int, float)):
        return None
    p = src.get("payload") or {}
    pos, vel = p.get("position_mm") or {}, p.get("velocity_mm_s") or {}
    return ts / 1e6, {"vx": vel.get("x"), "vy": vel.get("y"), "x": pos.get("x"), "y": pos.get("y"), "z": pos.get("z"),
                      "extruding": bool(p.get("extrusion_active")), "speed_pct": p.get("speed_override_percent")}


def heading_group(cam, heading_deg):
    if not _finite(heading_deg):
        return f"cam{cam}:none"
    h = int(round(heading_deg / 45.0)) * 45 % 360
    return f"cam{cam}:{h - 360 if h > 180 else h}"


def _num(v):
    return float(v) if _finite(v) else math.nan


def _angdiff(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


class MotionBuffer:
    """Recent motion states. at(t) interpolates them and adds a smoothed heading, the straight extruded path length
    behind the nozzle (dist_since_turn_mm) and that path's history (path_back: distance back -> time)."""

    def __init__(self, span_ms=10_000, heading_window_ms=200, max_gap_ms=1000, min_speed_mm_s=2.0, turn_deg=20.0,
                 turn_persist_ms=150, max_path_mm=150.0):
        self.span_ms, self.heading_window_ms, self.max_gap_ms = span_ms, heading_window_ms, max_gap_ms
        self.min_speed, self.turn_deg, self.turn_persist_ms = min_speed_mm_s, turn_deg, turn_persist_ms
        self.max_path_mm = max_path_mm
        self._t, self._rows, self._flags = [], [], []
        self._last_pos = None

    def add(self, t_ms, state):
        if not _finite(t_ms) or self._t and t_ms <= self._t[-1]:
            return  # every telemetry message repeats the latest motion sample
        row = [_num(state.get(k)) for k in ("vx", "vy", "x", "y", "z")]
        pos, prev, self._last_pos = row[2:], self._last_pos, row[2:]
        if pos == prev and math.hypot(row[0], row[1]) > 0:
            row[2:] = [math.nan] * 3  # position refreshes at half the motion rate; a repeat while moving is stale
        self._t.append(float(t_ms))
        self._rows.append(row)
        self._flags.append((state.get("extruding") is True, state.get("speed_pct")))
        if self._t[-1] - self._t[0] > 1.5 * self.span_ms:
            k = bisect_left(self._t, self._t[-1] - self.span_ms)
            del self._t[:k], self._rows[:k], self._flags[:k]

    def _headings(self, T, V, times):
        # vector mean over +-heading_window: x and y velocity sometimes update one sample apart
        m = (np.abs(times[:, None] - T[None, :]) <= self.heading_window_ms).astype(float)
        n = np.maximum(m.sum(1), 1)
        vx, vy = (m @ V[:, 0]) / n, (m @ V[:, 1]) / n
        h = np.degrees(np.arctan2(vy, vx))
        return np.where((np.hypot(vx, vy) >= self.min_speed) & (m.sum(1) > 0), h, np.nan)

    def at(self, t_ms):
        if not _finite(t_ms) or not self._t or not self._t[0] <= t_ms <= self._t[-1]:
            return None
        i = bisect_right(self._t, t_ms) - 1
        j = min(i + 1, len(self._t) - 1)
        if self._t[j] - self._t[i] > self.max_gap_ms:
            return None
        T = np.asarray(self._t)
        R = np.asarray(self._rows)
        V = np.nan_to_num(R[:, :2])
        w = 0.0 if j == i else (t_ms - T[i]) / (T[j] - T[i])
        vx, vy = (1 - w) * V[i] + w * V[j]
        pos = []
        for c in (2, 3, 4):
            ok = ~np.isnan(R[:, c])
            pos.append(float(np.interp(t_ms, T[ok], R[ok, c])) if ok.any() else None)
        H = self._headings(T, V, np.r_[t_ms, T[:i + 1]])
        heading = None if np.isnan(H[0]) else float(H[0])
        speed = float(math.hypot(vx, vy))
        dist, path_d, path_t = None, [], []
        if heading is not None:
            # walk back to the last turn, extrusion stop or telemetry gap. A heading off by more than turn_deg is a
            # turn only once it persists (>= 2 samples over >= turn_persist_ms): on diagonals LEAM reports the x and
            # y velocity one sample apart, which reads as a brief 45 deg swerve.
            sp = np.hypot(V[:, 0], V[:, 1])
            dist, tau, v_tau, prev = 0.0, t_ms, speed, t_ms
            path_d, path_t, pending = [0.0], [float(t_ms)], []
            for k in range(i, -1, -1):
                if prev - T[k] > self.max_gap_ms or not self._flags[k][0]:
                    break
                prev = T[k]
                hk = H[k + 1]
                if np.isnan(hk) or _angdiff(hk, heading) > self.turn_deg:
                    pending.append(k)
                    if len(pending) >= 2 and T[pending[0]] - T[k] >= self.turn_persist_ms:
                        break
                    continue
                for kk in pending + [k]:
                    if T[kk] < tau:
                        dist += 0.5 * (sp[kk] + v_tau) * (tau - T[kk]) / 1000.0
                        tau, v_tau = T[kk], sp[kk]
                        path_d.append(dist)
                        path_t.append(float(T[kk]))
                pending = []
                if dist > self.max_path_mm:
                    break
        extruding, speed_pct = self._flags[i]
        return {"t_ms": t_ms, "vx": float(vx), "vy": float(vy), "x": pos[0], "y": pos[1], "z": pos[2],
                "extruding": extruding, "speed_pct": speed_pct, "speed_mm_s": speed, "heading_deg": heading,
                "dist_since_turn_mm": dist, "path_back": {"d_mm": path_d, "t_ms": path_t}}


def _luma(jpeg):
    """Luma of an encoded frame, or None when the bytes are not a decodable image."""
    try:
        im = Image.open(io.BytesIO(jpeg))
        if im.mode == "L":
            return np.asarray(im, dtype=float)
        im.draft("YCbCr", im.size)  # decode straight to YCbCr: luma without a round trip through RGB
        if im.mode != "YCbCr":
            im = im.convert("YCbCr")
        return np.asarray(im)[..., 0].astype(float)
    except (OSError, ValueError, TypeError, SyntaxError):  # truncated, unknown or unconvertible image data
        return None


def _smooth(p, sigma_bins):
    r = int(4 * sigma_bins + 0.5)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma_bins) ** 2)
    return np.convolve(np.pad(p, r, mode="symmetric"), k / k.sum(), mode="valid")


def _vertex(g, i):
    """Sub-bin offset of the extremum of g at index i (parabola through i-1, i, i+1), in bins."""
    if 0 < i < len(g) - 1:
        a, b, c = g[i - 1], g[i], g[i + 1]
        den = a - 2 * b + c
        if den < 0:
            return float(np.clip(0.5 * (a - c) / den, -1, 1))
    return 0.0


def _ridges(q, u, base, min_height, merge_px):
    """Ridges of q as lists of local-maximum indices. Maxima within merge_px whose dip stays above half the lower peak
    (over the base) are one bead: past green the palette's luma falls, so a hot bead core reads as a dip."""
    i = np.flatnonzero((q[1:-1] > q[:-2]) & (q[1:-1] >= q[2:])) + 1
    i = [k for k in i if q[k] - base >= min_height]
    ridges = []
    for k in i:
        if ridges:
            a = ridges[-1][-1]
            if u[k] - u[a] <= merge_px and q[a:k + 1].min() - base >= 0.5 * (min(q[a], q[k]) - base):
                ridges[-1].append(k)
                continue
        ridges.append([k])
    return ridges


class WidthExtractor:
    def __init__(self, geometry, config=None):
        self.geometry = geometry
        self.cfg = {**DEFAULTS, **(config or {})}
        self._axes = {}
        self.last = {}  # scalar diagnostics of the latest frame, for offline tools

    # axis prediction ---------------------------------------------------------
    def predict_axis(self, cam, plane_deg):
        """Window centre, image direction, plane distances and pixel table of the bead stretch to measure, or None."""
        key = (cam, round(plane_deg * 2) / 2)
        if key in self._axes:
            return self._axes[key]
        c, g = self.cfg, self.geometry
        th = math.radians(key[1])
        p0 = g.pixel_to_plane(cam, [g.nozzle_px(cam)])[0]
        t = np.arange(-15.0, 60.0, 0.25)  # mm along the bead from the nozzle; the upstream part only locates the glow
        px = g.plane_to_pixel(cam, p0 + t[:, None] * np.array([math.cos(th), math.sin(th)]))
        arc = np.r_[0, np.cumsum(np.hypot(*np.diff(px, axis=0).T))]
        hot = np.asarray(g.hotspot_px(cam))
        k = min(max(int(np.argmin(np.hypot(*(px - hot).T))), 1), len(px) - 2)
        tan = px[k + 1] - px[k - 1]
        s_foot = arc[k] + float((hot - px[k]) @ tan / np.linalg.norm(tan))
        s0, s1 = c["s_window_px"]
        sc, half_s = s_foot + 0.5 * (s0 + s1), 0.5 * (s1 - s0)
        self._axes[key] = None
        if not arc[0] < sc < arc[-1] - half_s:
            return None
        xc, yc = np.interp(sc, arc, px[:, 0]), np.interp(sc, arc, px[:, 1])
        dx = np.interp(sc + 1, arc, px[:, 0]) - np.interp(sc - 1, arc, px[:, 0])
        dy = np.interp(sc + 1, arc, px[:, 1]) - np.interp(sc - 1, arc, px[:, 1])
        phi = math.atan2(dy, dx)
        tx, ty = math.cos(phi), math.sin(phi)
        half_u = c["profile_half_px"] + c["lateral_search_px"] + 1.0
        W, H = g.image_size
        corners = [(xc + a * tx - b * ty, yc + a * ty + b * tx) for a in (-half_s, half_s) for b in (-half_u, half_u)]
        if any(not (0 <= x <= W - 1 and 0 <= y <= H - 1) for x, y in corners):
            return None
        reach = math.hypot(half_s + 2.0, half_u)
        yy, xx = np.mgrid[max(int(yc - reach), 0):min(int(yc + reach) + 1, H - 1) + 1,
                          max(int(xc - reach), 0):min(int(xc + reach) + 1, W - 1) + 1]
        ds = (xx - xc) * tx + (yy - yc) * ty
        du = -(xx - xc) * ty + (yy - yc) * tx
        m = (np.abs(ds) <= half_s + 2.0) & (np.abs(du) <= half_u)
        axis = {"x": float(xc), "y": float(yc), "phi": phi, "yy": yy[m], "xx": xx[m], "ds": ds[m], "du": du[m],
                "mid_mm": float(np.interp(sc, arc, t)),  # plane distance from the nozzle, window centre
                "far_mm": float(np.interp(s_foot + s1, arc, t))}
        if len(self._axes) > 4000:
            self._axes.clear()
        self._axes[key] = axis
        return axis

    # measurement -------------------------------------------------------------
    def _refine(self, v, ds, du):
        """Angle and lateral offset (rad, px) of the ridge that maximise its contrast within the window."""
        c = self.cfg
        A = np.radians(np.arange(-c["angle_search_deg"], c["angle_search_deg"] + 1e-9, c["angle_step_deg"]))
        O = np.arange(-c["lateral_search_px"], c["lateral_search_px"] + 1e-9, c["lateral_step_px"])
        ca, sa = np.cos(A)[:, None], np.sin(A)[:, None]
        inwin = (np.abs(ds * ca + du * sa) < 0.5 * (c["s_window_px"][1] - c["s_window_px"][0]))[:, None, :]
        U = np.abs((-ds * sa + du * ca)[:, None, :] - O[None, :, None])
        wr = np.exp(-0.5 * (U / c["ridge_sigma_px"]) ** 2) * inwin
        wf = ((U >= c["flank_px"][0]) & (U <= c["flank_px"][1]) & inwin).astype(float)
        score = (wr @ v) / np.maximum(wr.sum(-1), 1e-9) - (wf @ v) / np.maximum(wf.sum(-1), 1e-9)
        ia, io_ = np.unravel_index(int(np.argmax(score)), score.shape)
        return float(A[ia]), float(O[io_])

    def _profile(self, v, ds, du, alpha, off):
        c = self.cfg
        half_s, half_u, du_bin = 0.5 * (c["s_window_px"][1] - c["s_window_px"][0]), c["profile_half_px"], c["bin_px"]
        s = ds * math.cos(alpha) + du * math.sin(alpha)
        u = -ds * math.sin(alpha) + du * math.cos(alpha) - off
        m = (np.abs(s) < half_s) & (np.abs(u) < half_u)
        edges = np.arange(-half_u, half_u + du_bin / 2, du_bin)
        centres = 0.5 * (edges[:-1] + edges[1:])
        k = np.clip(np.digitize(u[m], edges) - 1, 0, len(centres) - 1)
        num = np.bincount(k, v[m], minlength=len(centres))
        cnt = np.bincount(k, minlength=len(centres))
        ok = cnt > 0
        if ok.sum() < 5:
            return centres, None, cnt
        return centres, np.interp(centres, centres[ok], num[ok] / cnt[ok]), cnt

    def _edges(self, u, p, cnt, u_pred):
        """Gradient-max edges of the ridge nearest u_pred; returns (reason, width_px, amp, diagnostics)."""
        c = self.cfg
        du = u[1] - u[0]
        q = _smooth(p, c["smooth_px"] / du)
        e = c["edge_search_px"]
        b0, b1 = c["base_px"]

        def base_at(m):
            return float(np.median(np.r_[q[(u < m - b0) & (u > m - b1)], q[(u > m + b0) & (u < m + b1)]]))

        cen = np.flatnonzero(np.abs(u) < 2.0)
        all_r = _ridges(q, u, base_at(u[cen[np.argmax(q[cen])]]), c["min_ridge_luma"], c["merge_px"])
        near = [r for r in all_r if np.abs(u[r]).min() < 2.0]
        if not near:
            return "low_contrast", None, 0.0, {}
        # the ridge nearest the predicted axis, not the strongest: the other wall of print 2 can be brighter
        ridge = min(near, key=lambda r: max(u[r[0]] - u_pred, u_pred - u[r[-1]], 0.0))
        mu = u[min(ridge, key=lambda k: abs(u[k] - u_pred))]
        base = base_at(mu)
        amp = float(q[ridge].max() - base)
        others = [r for r in all_r if r is not ridge]
        gaps = [min(abs(u[k] - mu) for k in r) for r in others]  # from our maximum: a hot core's shoulder is not it
        nearest = int(np.argmin(gaps)) if others else None
        nb = min(others[nearest], key=lambda k: abs(u[k] - mu)) if others else None
        diag = {"mu": float(mu), "neighbour_px": float(u[nb] - mu) if others else math.nan}
        if amp < c["min_amp"]:
            return "low_contrast", None, amp, diag
        above = q >= base + 0.5 * amp
        a, b = ridge[0], ridge[-1]
        while a > 0 and above[a - 1]:
            a -= 1
        while b < len(q) - 1 and above[b + 1]:
            b += 1
        diag["extent_px"] = float(u[b] - u[a]) if 0 < a and b < len(q) - 1 else math.inf
        if diag["extent_px"] >= c["max_extent_px"]:
            return "too_wide", None, amp, diag  # above half its peak over more than one bead can cover
        if others and gaps[nearest] < c["ambiguous_px"]:
            return "ambiguous", None, amp, diag
        g = np.gradient(q, du)
        # edges lie outside the bead's maxima; a neighbouring ridge within reach bounds the search at the dip between
        u0, u1 = u[ridge[0]], u[ridge[-1]]
        left = [r[-1] for r in others if r[-1] < ridge[0] and u[r[-1]] > u0 - e]
        right = [r[0] for r in others if r[0] > ridge[-1] and u[r[0]] < u1 + e]
        lo = u[left[-1] + int(np.argmin(q[left[-1]:ridge[0] + 1]))] if left else u0 - e
        hi = u[ridge[-1] + int(np.argmin(q[ridge[-1]:right[0] + 1]))] if right else u1 + e
        L = np.flatnonzero((u > lo) & (u < u0))
        R = np.flatnonzero((u > u1) & (u < hi))
        if not len(L) or not len(R):
            return "no_edges", None, amp, diag
        il, ir = L[np.argmax(g[L])], R[np.argmin(g[R])]
        gl, gr = float(g[il]), float(-g[ir])
        if min(gl, gr) < c["min_edge_ratio"] * amp or il == L[0] or ir == R[-1]:
            return "no_edges", None, amp, diag  # too faint, or pinned to the search limit: not an edge
        el = u[il] + _vertex(g, il) * du
        er = u[ir] + _vertex(-g, ir) * du
        centre = 0.5 * (el + er)
        n_near = int(cnt[np.abs(u - centre) <= e].sum())
        diag.update(edge_l=float(el), edge_r=float(er), grad_l=gl, grad_r=gr, n_near=n_near)
        if er - el >= c["max_width_px"]:
            return "too_wide", None, amp, diag
        return "ok", float(er - el), amp, diag

    def _confidence(self, amp, d):
        c = self.cfg
        contrast = min(1.0, max(0.0, (amp - c["min_amp"]) / (3 * c["min_amp"])))
        symmetry = min(d["grad_l"], d["grad_r"]) / max(d["grad_l"], d["grad_r"])
        # pixels counted, not profile bins filled: near 45 deg the pixel grid fills only every other 0.25 px bin
        expected = (c["s_window_px"][1] - c["s_window_px"][0]) * 2 * c["edge_search_px"]
        support = min(1.0, d["n_near"] / (0.9 * expected))
        return float(contrast * symmetry * support)

    def process(self, jpeg, frame_meta, motion):
        c = self.cfg
        cam = frame_meta.get("camera_index")
        cam = int(cam) if _finite(cam) and cam == int(cam) else -1
        t = frame_time_ms(frame_meta)
        fid, recv = frame_meta.get("source_frame_id"), frame_meta.get("recv_ms")
        sample = WidthSample(t_ms=int(round(t)) if t is not None else 0, camera=cam, heading_deg=None,
                             axis_deg=math.nan, width_px=None, mm_per_px=None, contrast=0.0, confidence=0.0,
                             reason="no_geometry", recv_ms=int(recv) if _finite(recv) else None,
                             source_frame_id=None if fid is None else str(fid), group=heading_group(cam, None))
        self.last = {}
        if cam not in self.geometry.cams:
            return sample
        sample.reason = "no_timestamp"
        if t is None:
            return sample
        sample.reason = "no_motion"
        if motion is None:
            return sample
        heading = motion.get("heading_deg")
        if not _finite(heading):
            vx, vy = _num(motion.get("vx")), _num(motion.get("vy"))
            heading = math.degrees(math.atan2(vy, vx)) if math.hypot(vx, vy) >= c["min_speed_mm_s"] else None
        if heading is None:
            return sample
        sample.heading_deg = float(heading)
        sample.group = heading_group(cam, heading)
        if motion.get("extruding") is not True:
            sample.reason = "not_extruding"
            return sample
        plane_deg = (self.geometry.world_heading_to_plane_angle(cam, heading + 180.0) + 180.0) % 360.0 - 180.0
        axis = self.predict_axis(cam, plane_deg) if abs(plane_deg) <= c["max_view_angle_deg"] else None
        if axis is None:
            sample.reason = "axis_out_of_frame"
            return sample
        sample.axis_deg = math.degrees(axis["phi"]) % 360.0
        sample.t_dep_ms = self._deposit_time(t, axis["mid_mm"], motion)
        dist = motion.get("dist_since_turn_mm")
        need = axis["far_mm"] + c["turn_margin_mm"]
        self.last = {"plane_deg": plane_deg, "dist_since_turn_mm": dist, "need_mm": need}
        if not _finite(dist) or dist < need:
            sample.reason = "turning"
            return sample
        img = _luma(jpeg)
        if img is None or img.shape != (self.geometry.image_size[1], self.geometry.image_size[0]):
            sample.reason = "bad_image"
            return sample
        v = img[axis["yy"], axis["xx"]]
        alpha, off = self._refine(v, axis["ds"], axis["du"])
        u, p, cnt = self._profile(v, axis["ds"], axis["du"], alpha, off)
        if p is None:
            sample.reason = "axis_out_of_frame"
            return sample
        reason, width, amp, d = self._edges(u, p, cnt, -off)
        sample.axis_deg = math.degrees(axis["phi"] + alpha) % 360.0
        sample.contrast = amp
        self.last.update(alpha_deg=math.degrees(alpha), offset_px=off, **d)
        if reason != "ok":
            sample.reason = reason
            return sample
        phi = axis["phi"] + alpha
        lateral = off + 0.5 * (d["edge_l"] + d["edge_r"])  # the bead's centre across the predicted axis
        at_limit = abs(lateral) >= c["lateral_search_px"]
        self.last.update(lateral_px=lateral, at_search_limit=at_limit)
        x, y = axis["x"] - lateral * math.sin(phi), axis["y"] + lateral * math.cos(phi)
        sample.width_px = width
        sample.mm_per_px = self.geometry.mm_per_px_across(cam, x, y, math.degrees(phi))
        # a bead at or past the search limit may be off the predicted axis, or the neighbour
        sample.confidence = self._confidence(amp, d) * (0.5 if at_limit else 1.0)
        sample.reason = "ok"
        return sample

    def _deposit_time(self, t, back_mm, motion):
        path = motion.get("path_back") or {}
        d, ts = path.get("d_mm") or [], path.get("t_ms") or []
        if len(d) > 1 and len(d) == len(ts) and all(map(_finite, d)) and all(map(_finite, ts)) and d[-1] >= back_mm:
            return int(round(float(np.interp(back_mm, d, ts))))
        speed = motion.get("speed_mm_s")
        if _finite(speed) and speed >= self.cfg["min_speed_mm_s"]:
            return int(round(t - 1000.0 * back_mm / speed))
        return None
