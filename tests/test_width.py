import builtins
import io
import json
import math
import os
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from PIL import Image

from conftest import CAPTURES
from server.geometry import MODELS_DIR, Geometry
from server.width import MotionBuffer, WidthExtractor, WidthSample, frame_time_ms, heading_group, motion_from_telemetry

T0 = 1_790_000_000_000
META = {"camera_index": 1, "source_utc_ns": T0 * 1_000_000, "source_frame_id": "42", "recv_ms": T0 + 900}
STRAIGHT = {"vx": -10.0, "vy": -10.0, "heading_deg": -135.0, "extruding": True, "dist_since_turn_mm": 100.0,
            "speed_mm_s": 14.1, "path_back": {"d_mm": [0.0, 50.0], "t_ms": [T0, T0 - 3546.0]}}


def synthetic_geometry(k1=-0.2, roll_deg=40.0):
    """A made-up camera like LEAM's (56 mm up, 62 mm out, looking back at the nozzle). Rolled 40 deg, the bead runs at
    50 deg in the image, crossing the pixel grid diagonally. Not a calibration: the real one stays out of the repo."""
    C, Q = np.array([62.0, 0.0, 56.0]), np.array([30.0, 0.0, 0.0])
    z = (Q - C) / np.linalg.norm(Q - C)
    y = np.array([1.0, 0, 0]) - z[0] * z
    y /= np.linalg.norm(y)
    x = np.cross(y, z)
    r = math.radians(roll_deg)
    x, y = math.cos(r) * x + math.sin(r) * y, -math.sin(r) * x + math.cos(r) * y
    cam = {"type": "pinhole_radial", "C_mm": C.tolist(), "R_world_to_cam": np.vstack([x, y, z]).tolist(),
           "f_px": 108.0, "principal_point_px": [79.5, 59.5], "k1": k1, "view_azimuth_deg": 45.0}
    g = Geometry({"image_size": [160, 120], "cameras": {"1": cam}})
    hx, hy = g.plane_to_pixel(1, [[-4.0, 0.0]])[0]
    cam["nozzle_hotspot_px"] = [float(hx) + 3, float(hy) - 2]
    return Geometry({"image_size": [160, 120], "cameras": {"1": cam}})


GEO = synthetic_geometry()


def _phi(z):
    return 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))


# A display palette like LEAM's: luma peaks at green and falls toward red, so a hot bead core reads as a dip.
PALETTE_T = [0.0, 0.2, 0.4, 0.54, 0.62, 0.8, 1.0]
PALETTE_RGB = np.array([[30, 30, 130], [100, 40, 150], [120, 170, 120], [80, 240, 90], [200, 190, 40],
                        [240, 120, 20], [220, 40, 30]], float)


def render(width, sigma=0.4, rot_deg=0.0, shift=0.4, quality=95, palette=False, plane_deg=0.0, ex=None, amp=170.0,
           profile=None):
    """JPEG of a bar of `width` px, blurred by a Gaussian of `sigma` px, along the predicted bead axis (turned by
    rot_deg and moved across by shift px). `profile(u)` in 0..1 replaces the bar."""
    ax = (ex or WidthExtractor(GEO)).predict_axis(1, plane_deg)
    phi = ax["phi"] + math.radians(rot_deg)
    yy, xx = np.mgrid[0:120, 0:160].astype(float)
    cx, cy = ax["x"] - shift * math.sin(phi), ax["y"] + shift * math.cos(phi)
    s = (xx - cx) * math.cos(phi) + (yy - cy) * math.sin(phi)
    u = -(xx - cx) * math.sin(phi) + (yy - cy) * math.cos(phi)
    bar = profile(u) if profile else _phi((u + width / 2) / sigma) - _phi((u - width / 2) / sigma)
    prof = bar * ((s > -40) & (s < 60))
    if palette:
        t = 0.05 + 0.7 * prof
        rgb = np.stack([np.interp(t, PALETTE_T, PALETTE_RGB[:, i]) for i in range(3)], -1)
    else:
        rgb = np.repeat((30 + amp * prof)[..., None], 3, -1)
    buf = io.BytesIO()
    Image.fromarray(np.clip(np.round(rgb), 0, 255).astype(np.uint8), "RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def measure(jpeg, motion=STRAIGHT, meta=META, ex=None):
    return (ex or WidthExtractor(GEO)).process(jpeg, meta, motion)


def two_beads(sep, gain, width=2.5, sigma=0.5):
    """Our bead on the predicted axis and a second one sep px across it, gain times as bright."""
    a = np.asarray(Image.open(io.BytesIO(render(width, sigma=sigma, shift=0.0))).convert("L"), float)
    b = np.asarray(Image.open(io.BytesIO(render(width, sigma=sigma, shift=sep))).convert("L"), float)
    buf = io.BytesIO()
    Image.fromarray(np.clip(np.maximum(a, 30 + gain * (b - 30)), 0, 255).astype(np.uint8)).convert("RGB").save(
        buf, "JPEG", quality=95)
    return buf.getvalue()


# geometry --------------------------------------------------------------------------------------------------------------
def test_geometry_round_trip_and_scale():
    xy = np.array([[60.0, 40.0], [100.0, 70.0], [80.0, 59.5]])
    assert np.allclose(GEO.plane_to_pixel(1, GEO.pixel_to_plane(1, xy)), xy, atol=1e-6)
    s = GEO.mm_per_px_across(1, 80, 60, 30)
    assert 0.3 < s < 1.0 and s == pytest.approx(GEO.mm_per_px_across(1, 80, 60, 210))
    assert GEO.world_heading_to_plane_angle(1, -135) == pytest.approx(180)
    assert np.allclose(GEO.nozzle_px(1), GEO.plane_to_pixel(1, [[0, 0]])[0])  # no fitted nozzle pixel: plane origin


def test_missing_geometry_file_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="geometry model not found"):
        Geometry.load(tmp_path / "geometry.json")


# width on synthetic frames ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("width", [2.0, 2.5, 3.0, 3.5, 4.0])
def test_sharp_bar_width_within_a_tenth_of_a_pixel(width):
    # the estimator reads narrow sharp bars ~0.05-0.1 px wide (edge interplay after smoothing): per frame 0.15, mean 0.1
    ex, ex_angle = WidthExtractor(GEO), WidthExtractor(GEO, {"angle_search_deg": 8.0})
    cases = [(ex, sh, 0.0) for sh in (-0.6, 0.3, 0.7)] + [(ex_angle, 0.3, 5.0)]  # the angle search is optional
    got = []
    for e, shift, rot in cases:
        s = measure(render(width, sigma=0.3, shift=shift, rot_deg=rot, ex=e), ex=e)
        assert s.reason == "ok" and 0.5 < s.confidence <= 1 and s.contrast > 100
        got.append(s.width_px)
    assert max(abs(g - width) for g in got) < 0.15 and abs(np.mean(got) - width) < 0.1


def test_mm_per_px_is_taken_at_the_bead_centre():
    ex = WidthExtractor(GEO)
    ax = ex.predict_axis(1, 45.0)  # off the camera's symmetry plane, where the scale changes ~1 % per px across
    for shift in (-0.8, 0.8):
        s = measure(render(3.0, sigma=0.3, shift=shift, plane_deg=45.0, ex=ex), ex=ex,
                    motion={**STRAIGHT, "heading_deg": -90.0})
        c = ex.last["lateral_px"]
        assert c == pytest.approx(shift, abs=0.1)
        x, y = ax["x"] - c * math.sin(ax["phi"]), ax["y"] + c * math.cos(ax["phi"])
        assert s.mm_per_px == pytest.approx(GEO.mm_per_px_across(1, x, y, math.degrees(ax["phi"])), rel=0.002)
        across = GEO.mm_per_px_across(1, x, y, math.degrees(ax["phi"]) + 90)
        assert abs(s.mm_per_px / across - 1) > 0.02  # across the bead, not along it


def test_bead_at_the_search_limit_halves_confidence():
    ex = WidthExtractor(GEO)
    inside = ex.process(render(3.0, shift=0.3), META, STRAIGHT)
    assert inside.reason == "ok" and ex.last["at_search_limit"] is False
    near = ex.process(render(3.0, shift=0.9), META, STRAIGHT)  # the search stops at 1 px; the bead is inside
    assert near.reason == "ok" and ex.last["at_search_limit"] is False
    edge = ex.process(render(3.0, shift=1.6), META, STRAIGHT)  # the bead sits beyond the +-1 px search
    assert edge.reason == "ok" and ex.last["at_search_limit"] is True and ex.last["lateral_px"] > 1.3
    assert edge.confidence == pytest.approx(0.5 * inside.confidence, rel=0.15)


@pytest.mark.parametrize("roll", [12.0, -44.0])  # the bead at 78 and at 134 deg in the image
def test_confidence_does_not_depend_on_the_axis_angle(roll):
    ex = WidthExtractor(synthetic_geometry(roll_deg=roll))
    ref = WidthExtractor(GEO)
    assert math.degrees(ex.predict_axis(1, 0.0)["phi"]) % 180 == pytest.approx(90 - roll, abs=2)
    a = [measure(render(3.0, sigma=0.5, shift=sh, ex=ex), ex=ex).confidence for sh in (-0.3, 0.2)]
    b = [measure(render(3.0, sigma=0.5, shift=sh, ex=ref), ex=ref).confidence for sh in (-0.3, 0.2)]
    assert np.mean(a) == pytest.approx(np.mean(b), abs=0.05) and min(a) > 0.85


def bias_curve(widths, **kw):
    ex = WidthExtractor(GEO)
    return [float(np.mean([measure(render(w, shift=sh, ex=ex, **kw), ex=ex).width_px for sh in (-0.5, 0.0, 0.5)]))
            for w in widths]


@pytest.mark.parametrize("palette", [False, True])
def test_blur_floor_jpeg_and_palette_keep_width_monotonic(palette):
    # sigma 0.75 px: a vanishing line then reads ~1.8-2 px, the blur floor seen on the real frames; quality 66, 4:2:0
    widths = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5] + ([] if palette else [4.0])  # saturated 4 px reads > 4.5: too_wide
    got = bias_curve(widths, sigma=0.75, quality=66, palette=palette)
    print("\nbias curve (palette)" if palette else "\nbias curve (grey)", ", ".join(
        f"{w:.1f}->{g:.2f} ({g - w:+.2f})" for w, g in zip(widths, got)))
    assert all(b > a for a, b in zip(got, got[1:]))
    assert 1.5 < got[0] < 2.4  # the floor


@pytest.mark.parametrize("palette", [False, True])
def test_wide_or_saturated_beads_are_never_read_small(palette):
    ex = WidthExtractor(GEO)
    for w in (5.0, 5.5, 6.0, 6.5, 7.0):
        for shift in (-0.5, 0.0, 0.5):
            s = measure(render(w, sigma=0.75, quality=66, palette=palette, shift=shift, ex=ex), ex=ex)
            assert s.reason != "ok", (w, shift, s.width_px)
    assert measure(render(9.0, sigma=0.75, quality=66), ex=ex).reason == "too_wide"


def test_neighbour_distance_is_taken_from_the_chosen_maximum():
    # a hot core at 0 with a merged shoulder at 1.7 px, and the next pass 4.5 px from the core (2.8 px from the shoulder)
    u = np.arange(-8, 8, 0.25) + 0.125
    g = lambda c, w, h: h * np.exp(-0.5 * ((u - c) / w) ** 2)
    p = 30 + g(0.0, 0.45, 160) + g(1.7, 0.45, 150) + g(4.5, 0.6, 150)
    ex = WidthExtractor(GEO)
    reason, width, amp, d = ex._edges(u, p, np.full(len(u), 3), 0.0)
    assert reason == "ok" and d["mu"] == pytest.approx(0.0, abs=0.2) and d["neighbour_px"] == pytest.approx(4.5, abs=0.3)
    assert d["edge_l"] < -0.3 and d["edge_r"] > 1.9  # the edges enclose core and shoulder
    p_close = 30 + g(0.0, 0.45, 160) + g(1.7, 0.45, 150) + g(3.3, 0.6, 150)  # the next pass 3.3 px from the core
    assert ex._edges(u, p_close, np.full(len(u), 3), 0.0)[0] in ("ambiguous", "too_wide")


def test_narrow_core_on_a_broad_hot_base_is_too_wide():
    base = lambda u: np.clip(0.55 * (np.abs(u) < 5) + 0.45 * (np.abs(u) < 0.75), 0, 1)  # base above half the peak
    ex = WidthExtractor(GEO)
    s = measure(render(0, profile=base, sigma=0.3, shift=0.0, ex=ex), ex=ex)
    assert s.reason == "too_wide" and ex.last["extent_px"] >= ex.cfg["max_extent_px"]


def test_confidence_terms():
    ex = WidthExtractor(GEO)
    full = {"grad_l": 60.0, "grad_r": 60.0, "n_near": 80}
    assert ex._confidence(100.0, full) == pytest.approx(1.0)
    assert ex._confidence(100.0, {**full, "grad_r": 30.0}) == pytest.approx(0.5)  # one soft edge
    assert ex._confidence(100.0, {**full, "n_near": 36}) == pytest.approx(0.5)  # half the pixels expected
    assert ex._confidence(30.0, full) == pytest.approx(1 / 3)  # faint ridge


def test_saturated_core_is_one_ridge_not_two():
    ex = WidthExtractor(GEO)
    s = measure(render(3.0, sigma=0.75, quality=66, palette=True), ex=ex)
    assert s.reason == "ok" and s.width_px > 2.8
    assert math.isnan(ex.last["neighbour_px"])


@pytest.mark.parametrize("sep", [-4.2, 4.2])
def test_picks_the_ridge_nearest_the_predicted_axis(sep):
    ex = WidthExtractor(GEO)
    ax = ex.predict_axis(1, 0.0)
    s = ex.process(two_beads(sep, 1.3), META, STRAIGHT)  # a brighter second wall 4.2 px to one side
    assert s.reason == "ok" and s.width_px == pytest.approx(2.5, abs=0.25)
    assert abs(ex.last["lateral_px"]) < 0.3 and abs(ex.last["neighbour_px"] - sep) < 0.6
    assert ax is ex.predict_axis(1, 0.0)  # cached


@pytest.mark.parametrize("sep", [-3.2, 3.2])
def test_a_close_brighter_neighbour_is_ambiguous_not_measured(sep):
    ex = WidthExtractor(GEO)
    s = ex.process(two_beads(sep, 1.3), META, STRAIGHT)
    assert s.reason == "ambiguous" and s.width_px is None
    assert ex.process(two_beads(sep * 0.75, 1.3), META, STRAIGHT).reason == "too_wide"  # 2.4 px: one merged blob


# reject reasons -------------------------------------------------------------------------------------------------------
def test_reject_reasons():
    jpeg = render(3.0)
    assert measure(jpeg, meta={"camera_index": 1, "published_utc_ms": T0}).reason == "no_timestamp"
    assert measure(jpeg, motion=None).reason == "no_motion"
    assert measure(jpeg, motion={**STRAIGHT, "heading_deg": None, "vx": 0.0, "vy": 0.0}).reason == "no_motion"
    assert measure(jpeg, motion={**STRAIGHT, "extruding": False}).reason == "not_extruding"
    assert measure(jpeg, motion={**STRAIGHT, "dist_since_turn_mm": 12.0}).reason == "turning"
    assert measure(jpeg, motion={**STRAIGHT, "dist_since_turn_mm": None}).reason == "turning"
    assert measure(jpeg, motion={**STRAIGHT, "heading_deg": 45.0}).reason == "axis_out_of_frame"  # runs away
    flat = io.BytesIO()
    Image.new("RGB", (160, 120), (40, 40, 40)).save(flat, "JPEG")
    assert measure(flat.getvalue()).reason == "low_contrast"
    assert measure(render(3.0, amp=12.0)).reason == "low_contrast"  # below min_amp
    assert measure(render(3.0, amp=40.0)).reason == "ok"
    assert measure(render(3.0, shift=3.6)).reason == "low_contrast"  # the only ridge is beyond search + near window
    ramp = lambda u: np.clip((u + 10) / 10, 0, 1) * (u < 1.25)  # one side rises over 10 px: no edge there
    assert measure(render(0, profile=ramp, sigma=0.3, shift=0.4)).reason == "no_edges"


def test_turning_boundary():
    ex = WidthExtractor(GEO)
    need = ex.predict_axis(1, 0.0)["far_mm"] + ex.cfg["turn_margin_mm"]
    jpeg = render(3.0)
    assert measure(jpeg, motion={**STRAIGHT, "dist_since_turn_mm": need - 1}, ex=ex).reason == "turning"
    assert measure(jpeg, motion={**STRAIGHT, "dist_since_turn_mm": need + 1}, ex=ex).reason == "ok"
    assert ex.last["need_mm"] == pytest.approx(need)


def test_bad_inputs_never_raise():
    good = render(3.0)
    big = io.BytesIO()
    Image.open(io.BytesIO(good)).resize((320, 240)).save(big, "JPEG")
    for jpeg in (good[: len(good) // 2], b"not a jpeg", b"", big.getvalue()):
        assert measure(jpeg).reason == "bad_image"
    for ns in (float("nan"), 0, -5, "1790000000000000000", None, float("inf")):
        assert measure(good, meta={**META, "source_utc_ns": ns}).reason == "no_timestamp"
    for cam in (7, None, "1", 1.5, float("nan")):
        assert measure(good, meta={**META, "camera_index": cam}).reason == "no_geometry"
    s = measure(good, meta={**META, "recv_ms": float("nan")})
    assert s.reason == "ok" and s.recv_ms is None
    for h in (float("nan"), float("inf")):  # no usable heading: fall back to the velocity
        s = measure(good, motion={**STRAIGHT, "heading_deg": h})
        assert s.reason == "ok" and s.heading_deg == pytest.approx(-135)
    assert measure(good, motion={**STRAIGHT, "heading_deg": float("nan"), "vx": float("nan")}).reason == "no_motion"
    assert measure(good, motion={**STRAIGHT, "extruding": "false"}).reason == "not_extruding"
    assert measure(good, motion={**STRAIGHT, "dist_since_turn_mm": float("nan")}).reason == "turning"
    s = measure(good, motion={**STRAIGHT, "path_back": {"d_mm": [0.0, float("nan")], "t_ms": [T0, T0 - 1]},
                              "speed_mm_s": float("nan")})
    assert s.reason == "ok" and s.t_dep_ms is None


def test_sample_fields():
    s = measure(render(3.0))
    assert isinstance(s, WidthSample) and s.reason == "ok"
    assert s.t_ms == T0 and s.recv_ms == T0 + 900 and s.source_frame_id == "42" and s.group == "cam1:-135"
    assert s.heading_deg == -135.0 and 0 <= s.axis_deg < 360
    mid = WidthExtractor(GEO).predict_axis(1, 0.0)["mid_mm"]
    assert s.t_dep_ms == pytest.approx(T0 - 3546.0 * mid / 50.0, abs=1)
    no_path = measure(render(3.0), motion={**STRAIGHT, "path_back": None})
    assert no_path.t_dep_ms == pytest.approx(T0 - 1000 * mid / 14.1, abs=1)
    assert frame_time_ms({"source_utc_ns": 5_000_000}) == 5.0 and frame_time_ms({"published_utc_ms": 5}) is None
    assert heading_group(2, -90.2) == "cam2:-90" and heading_group(1, -179.0) == "cam1:180"


def test_process_writes_nothing(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.chdir(tmp_path)
    writes = []
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file, mode="r", *a, **k):
        if any(c in mode for c in "wax+"):
            writes.append(file)
        return real_open(file, mode, *a, **k)

    def guarded_os_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writes.append(path)
        return real_os_open(path, flags, *a, **k)

    jpegs = [render(w) for w in (2.0, 3.0)] + [render(3.0, palette=True, quality=66)]
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(Image.Image, "save", lambda *a, **k: writes.append("Image.save"))
    ex = WidthExtractor(GEO)
    for j in jpegs:
        ex.process(j, META, STRAIGHT)
    assert writes == [] and list(tmp_path.iterdir()) == [data] and list(data.iterdir()) == []


# motion buffer --------------------------------------------------------------------------------------------------------
def straight_then_turn(buf, t_turn=None, speed=20.0, until=6000, dt=100):
    """+X at `speed`, turning to +Y at t_turn (ms after T0); samples every dt ms."""
    x = y = 0.0
    for t in range(0, until + 1, dt):
        vx, vy = (speed, 0.0) if t_turn is None or t < t_turn else (0.0, speed)
        buf.add(T0 + t, {"vx": vx, "vy": vy, "x": x, "y": y, "z": 1.0, "extruding": True, "speed_pct": 100})
        x, y = x + vx * dt / 1000, y + vy * dt / 1000


def test_motion_interpolation_and_range():
    b = MotionBuffer()
    b.add(T0, {"vx": 10.0, "vy": 0.0, "x": 0.0, "y": 0.0, "z": 1.0, "extruding": True, "speed_pct": 100})
    b.add(T0 + 100, {"vx": 20.0, "vy": 0.0, "x": 1.0, "y": 0.0, "z": 1.0, "extruding": False, "speed_pct": 90})
    m = b.at(T0 + 25)
    assert m["vx"] == pytest.approx(12.5) and m["x"] == pytest.approx(0.25) and m["extruding"] is True
    assert m["speed_pct"] == 100 and m["speed_mm_s"] == pytest.approx(12.5)
    assert b.at(T0 - 1) is None and b.at(T0 + 101) is None and b.at(None) is None
    b.add(T0 + 50, {"vx": 99.0, "vy": 0.0})  # out of order: ignored
    assert b.at(T0 + 50)["vx"] == pytest.approx(15)
    b.add(float("nan"), {"vx": 99.0})
    assert b.at(T0 + 100)["vx"] == pytest.approx(20)
    b.add(T0 + 2000, {"vx": 20.0, "vy": 0.0, "x": 39.0, "y": 0.0, "z": 1.0, "extruding": True})
    assert b.at(T0 + 1000) is None  # a gap longer than max_gap_ms


def test_stale_position_is_not_a_stop():
    b = MotionBuffer()
    for i, x in enumerate([0.0, 0.0, 4.0, 4.0, 8.0]):  # position refreshes every second motion sample
        b.add(T0 + 100 * i, {"vx": 20.0, "vy": 0.0, "x": x, "y": 0.0, "z": 1.0, "extruding": True})
    assert b.at(T0 + 100)["x"] == pytest.approx(2.0)


def test_distance_since_turn():
    b = MotionBuffer()
    straight_then_turn(b)
    assert b.at(T0 + 5000)["dist_since_turn_mm"] >= 99.0  # 5 s at 20 mm/s, at least
    b = MotionBuffer()
    straight_then_turn(b, t_turn=4000)
    m = b.at(T0 + 5000)
    assert m["heading_deg"] == pytest.approx(90)
    assert 12.0 <= m["dist_since_turn_mm"] <= 20.0  # 1 s after the corner; the smoothed heading may cost a sample
    assert m["path_back"]["d_mm"][0] == 0 and m["path_back"]["t_ms"][0] == T0 + 5000
    assert np.all(np.diff(m["path_back"]["d_mm"]) > 0) and np.all(np.diff(m["path_back"]["t_ms"]) < 0)
    b = MotionBuffer()
    straight_then_turn(b)
    b.add(T0 + 6100, {"vx": 20.0, "vy": 0.0, "extruding": False})
    b.add(T0 + 6200, {"vx": 20.0, "vy": 0.0, "extruding": True})
    b.add(T0 + 6300, {"vx": 20.0, "vy": 0.0, "extruding": True})
    assert b.at(T0 + 6250)["dist_since_turn_mm"] < 3.0  # no bead was laid while not extruding


def test_gradual_drift_counts_as_a_turn():
    b = MotionBuffer()
    for i in range(60):  # an arc: 1 deg more per 100 ms sample at 20 mm/s
        h = math.radians(i)
        b.add(T0 + 100 * i, {"vx": 20 * math.cos(h), "vy": 20 * math.sin(h), "extruding": True})
    m = b.at(T0 + 5000)
    assert 30.0 <= m["dist_since_turn_mm"] <= 46.0  # ~20 samples (2 mm each) until 20 deg off, not the whole arc


def test_walk_back_stops_at_a_telemetry_gap():
    b = MotionBuffer()
    for t in list(range(0, 2001, 100)) + list(range(3500, 6001, 100)):  # 1.5 s without motion samples
        b.add(T0 + t, {"vx": 20.0, "vy": 0.0, "extruding": True})
    assert b.at(T0 + 2700) is None
    assert 49.0 <= b.at(T0 + 6000)["dist_since_turn_mm"] <= 51.0


def test_recorded_diagonal_report_pattern_is_not_a_turn():
    # golden print 1, hypotenuse at 135 deg and 26.5 mm/s, motion samples as recorded around a frame at 0 ms
    d = (-18.75, 18.75)
    seq = ([(t, d) for t in range(-4000, -1100, 100)] + [(-1027, d), (-985, (0.0, 26.5)), (-838, (-26.5, 0.0)),
           (-745, (-26.5, 0.0)), (-637, d), (-586, d), (-431, d), (-278, d), (-127, d), (-74, d), (55, d), (171, d),
           (300, d)])
    b = MotionBuffer()
    for t, (vx, vy) in seq:
        b.add(T0 + t, {"vx": vx, "vy": vy, "extruding": True})
    m = b.at(T0)
    assert m["heading_deg"] == pytest.approx(135, abs=1) and m["dist_since_turn_mm"] > 90


def test_split_axis_update_is_not_a_turn():
    b = MotionBuffer()
    v = 20.0 / math.sqrt(2)
    for i in range(40):
        vx, vy = {20: (0.0, 20.0), 21: (20.0, 0.0)}.get(i, (v, v))  # x and y velocity updated one sample apart
        b.add(T0 + 100 * i, {"vx": vx, "vy": vy, "extruding": True})
    m = b.at(T0 + 3500)
    assert m["heading_deg"] == pytest.approx(45, abs=1) and m["dist_since_turn_mm"] > 60
    assert b.at(T0 + 2000)["heading_deg"] == pytest.approx(45, abs=10)  # on the artefact sample itself


def test_motion_from_telemetry():
    msg = {"sources": {"motion": {"source_utc_ns": T0 * 1_000_000, "valid": True, "payload": {
        "position_mm": {"x": 1, "y": 2, "z": 3}, "velocity_mm_s": {"x": 4, "y": 5}, "extrusion_active": True,
        "speed_override_percent": 60}}}}
    t, st = motion_from_telemetry(msg)
    assert t == T0 and st == {"vx": 4, "vy": 5, "x": 1, "y": 2, "z": 3, "extruding": True, "speed_pct": 60}
    msg["sources"]["motion"]["valid"] = False
    assert motion_from_telemetry(msg) is None


# nozzle refit ---------------------------------------------------------------------------------------------------------
NOW = datetime(2026, 10, 21, 9, 5, 7)


def nozzle_model(version="0.2"):
    cam = dict(GEO.model["cameras"]["1"])  # stored pixels away from the prior, which a kept camera must keep
    model = {"image_size": [160, 120], "cameras": {"1": dict(cam, nozzle_px=[70.0, 40.0]),
                                                   "2": dict(cam, nozzle_px=[66.0, 44.0])}}
    return dict(model, version=version) if version else model


def refit(tmp_path, fits, model=None, **kw):
    from tools.model.width_check import apply_nozzle_fit

    model = model or nozzle_model()
    path = tmp_path / "geometry.json"
    path.write_text(json.dumps(model, indent=1))
    return model, path, apply_nozzle_fit(model, fits, path, now=NOW, **kw)


def test_nozzle_solve_keeps_cameras_without_enough_frames():
    from tools.model.width_check import solve_nozzle

    ex = WidthExtractor(GEO, {"lateral_search_px": 7.0, "angle_search_deg": 8.0})
    delta, rows = np.array([0.15, -0.1]), []
    for pdeg in [d for d in range(-180, 180, 30) if ex.predict_axis(1, d)][:4]:
        ax = ex.predict_axis(1, pdeg)
        nrm = np.array([-np.sin(ax["phi"]), np.cos(ax["phi"])])
        lat = nrm @ np.linalg.inv(GEO.jacobian(1, ax["x"], ax["y"])) @ delta
        rows += [(1, pdeg, lat)] * 120
    rows += [(2, 0.0, 0.0)] * 50
    W = pd.DataFrame(rows, columns=["camera", "plane_deg", "lateral_px"]).assign(
        reason="ok", alpha_deg=0.0, offset_px=lambda d: d.lateral_px)
    two = Geometry(nozzle_model(None) | {"cameras": {"1": GEO.model["cameras"]["1"], "2": GEO.model["cameras"]["1"]}})
    fits = solve_nozzle(W, two, ex)
    want = GEO.plane_to_pixel(1, [GEO.pixel_to_plane(1, [GEO.nozzle_px(1)])[0] + delta])[0]
    assert fits[1][0] == pytest.approx(want, abs=0.02) and fits[1][1] == 480
    assert fits[2] == (None, 50)


def test_nozzle_refit_keeps_unfitted_cameras_versions_and_backs_up(tmp_path, capsys):
    from server.pipeline import geometry_version

    old, path, new = refit(tmp_path, {1: ((71.0, 40.5), 300), 2: (None, 40)})
    out = capsys.readouterr().out
    assert "cam1: (70.0, 40.0) -> (71.0, 40.5), shift 1.12 px, 300 frames" in out
    assert "cam2: kept (66.0, 44.0), 40 frames" in out
    written = json.loads(path.read_text())
    assert written == new and written["cameras"]["1"]["nozzle_px"] == [71.0, 40.5]
    assert written["cameras"]["2"] == old["cameras"]["2"]
    assert written["version"] == "0.2+nozzle-20261021T0905"
    backup = tmp_path / "geometry.json.20261021T090507.bak"
    assert json.loads(backup.read_text()) == old
    assert provenance(geometry_version(old), written["version"]) == ("ok", False), "aim only: the camera fit stays"
    from tools.model.width_check import apply_nozzle_fit
    with pytest.raises(FileExistsError):
        apply_nozzle_fit(written, {1: ((71.2, 40.5), 300)}, path, now=NOW)
    assert json.loads(backup.read_text()) == old and json.loads(path.read_text()) == written


def provenance(recorded, running, cfg_changed=False):
    """The app's camera-model check: fitted on geometry `recorded`, running geometry `running`."""
    from server.pipeline import Pipeline, width_config_hash

    cfg = {"lateral_search_px": 1.0}
    camera = SimpleNamespace(geometry_version=recorded, width_config_hash=width_config_hash(cfg))
    app = SimpleNamespace(camera_model=camera, extractor=SimpleNamespace(cfg={"lateral_search_px": 2.0} if cfg_changed
                                                                         else cfg))
    return Pipeline._provenance(app, SimpleNamespace(model={"version": running}))


@pytest.mark.parametrize("recorded, running, cfg_changed, holds", [
    ("0.2", "0.2+nozzle-20261021T0905", False, False),
    ("0.2+nozzle-20261021T0905", "0.2+nozzle-20261021T0905+nozzle-20261022T0800", False, False),
    ("0.2+nozzle-20261021T0905", "0.2", False, False),
    ("0.2", "0.3+nozzle-20261021T0905", False, True),
    ("0.2+nozzle-20261021T0905", "0.3", False, True),
    ("0.2", "0.2+nozzle-20261021T0905", True, True),
])
def test_a_nozzle_refit_keeps_the_camera_model_other_changes_hold(recorded, running, cfg_changed, holds):
    status, hold = provenance(recorded, running, cfg_changed)
    assert hold == holds and (status == "ok") != holds
    if holds and not cfg_changed:
        assert recorded in status and running in status, "the full versions stay visible"


def test_nozzle_refit_without_a_version_extends_the_digest(tmp_path):
    from server.pipeline import geometry_version

    old, _, new = refit(tmp_path, {1: ((70.5, 40.0), 300)}, model=nozzle_model(None))
    assert new["version"] == geometry_version(old) + "+nozzle-20261021T0905"


@pytest.mark.parametrize("fits", [{1: (None, 99), 2: (None, 0)}, {1: ((72.0, 41.5), 300)}])
def test_nozzle_refit_that_is_empty_or_moves_over_2px_writes_nothing(tmp_path, capsys, fits):
    old, path, new = refit(tmp_path, fits)
    assert new is None and json.loads(path.read_text()) == old and list(tmp_path.iterdir()) == [path]
    assert ("not written: cam1 moved more than 2 px" if fits[1][0] else "nothing refitted") in capsys.readouterr().out


def test_nozzle_refit_over_2px_is_written_with_force(tmp_path):
    _, path, new = refit(tmp_path, {1: ((72.0, 41.5), 300)}, force=True)
    assert new["cameras"]["1"]["nozzle_px"] == [72.0, 41.5] and len(list(tmp_path.iterdir())) == 2


def fake_capture(root, name, when):
    (root / name).mkdir(parents=True)
    (root / name / "ir.jsonl").write_text(json.dumps({"recv_ms": when.timestamp() * 1000, "file": "ir/a.jpg"}) + "\n")


@pytest.fixture
def refitted(tmp_path, monkeypatch):
    """Installed geometry refitted at NOW on capture "demo"; captures filmed before, during and after it."""
    import tools.model.width_check as wc

    caps = tmp_path / "captures"
    fake_capture(caps, "golden-20261020-1736", datetime(2026, 10, 20, 17, 36))
    fake_capture(caps, "print-20261020-1531", datetime(2026, 10, 20, 15, 31))
    fake_capture(caps, "demo", datetime(2026, 10, 21, 8, 30))
    fake_capture(caps, "after", datetime(2026, 10, 21, 9, 30))
    old, path, new = refit(tmp_path, {1: ((71.0, 40.5), 300)}, captures=["demo"])
    monkeypatch.setattr(wc, "CAPTURES", caps)
    monkeypatch.setattr(wc, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(wc, "OUT", tmp_path / "out")
    return wc, old, new, next(tmp_path.glob("geometry.json.*.bak"))


def test_widths_filmed_outside_a_geometrys_aim_are_refused(refitted):
    wc, old, new, _ = refitted
    assert new["nozzle_fit_captures"] == {new["version"]: ["demo"]}
    assert wc.write_refusals(["demo", "after"], new) == [], "fitted on, or filmed after the refit"
    refused = wc.write_refusals(["golden-20261020-1736", "missing"], new)
    assert len(refused) == 2 and "golden-20261020-1736 was filmed 2026-10-20 17:36" in refused[0]
    assert wc.write_refusals(["golden-20261020-1736"], old, installed=new) == [], "the backup's aim"
    assert len(wc.write_refusals(["demo", "after", "missing"], old, installed=new)) == 3, "at the new aim, or unknown"
    assert wc.write_refusals(["golden-20261020-1736"], dict(old, version="0.1"), installed=new), "not its backup"


A, B = "0.2+nozzle-20261021T0900", "0.2+nozzle-20261021T0900+nozzle-20261021T1200"
TWO_REFITS = {"version": B, "nozzle_fit_captures": {A: ["d1"], B: ["d2"]}}


@pytest.mark.parametrize("cap, start, version, ok", [
    ("golden", (20, 18, 0), "0.2", True), ("golden", (20, 18, 0), A, False), ("golden", (20, 18, 0), "0.1", False),
    ("golden", None, "0.2", False),
    ("d1", (21, 8, 40), "0.2", False), ("d1", (21, 8, 40), A, True), ("mid", (21, 10, 0), A, True),
    ("d2", (21, 11, 40), A, False), ("d2", (21, 11, 40), B, True), ("mid", (21, 10, 0), B, False),
    ("late", (21, 13, 0), A, False), ("late", (21, 13, 0), B, True), ("x", None, B, False),
])
def test_each_capture_belongs_to_the_aim_period_of_one_geometry(cap, start, version, ok):
    from tools.model.width_check import aim_refusals

    when = datetime(2026, 10, *start) if start else None
    assert (aim_refusals(cap, when, version, TWO_REFITS) == []) == ok


def test_a_non_finite_or_non_aim_refit_is_not_written(tmp_path, capsys):
    old, path, new = refit(tmp_path, {1: ((float("nan"), 40.0), 300)})
    assert new is None and "not finite" in capsys.readouterr().out and json.loads(path.read_text()) == old
    other = nozzle_model()
    other["cameras"]["1"] = dict(other["cameras"]["1"], f_px=113.4)
    from tools.model.width_check import apply_nozzle_fit
    assert apply_nozzle_fit(other, {1: ((70.5, 40.0), 300)}, path, now=NOW) is None
    assert "more than the nozzle aim" in capsys.readouterr().out and json.loads(path.read_text()) == old
    aim_only = dict(nozzle_model(), version="0.2", nozzle_fit_captures=["x"])
    aim_only["cameras"]["2"] = dict(aim_only["cameras"]["2"], nozzle_px=[60.0, 30.0])
    assert apply_nozzle_fit(aim_only, {1: ((70.5, 40.0), 300)}, path, now=NOW) is not None


def test_nozzle_refits_keep_the_history_of_fitted_captures(tmp_path):
    from tools.model.width_check import apply_nozzle_fit

    _, path, first = refit(tmp_path, {1: ((71.0, 40.5), 300)}, captures=["d1"])
    second = apply_nozzle_fit(first, {1: ((71.5, 40.5), 300)}, path, now=datetime(2026, 10, 21, 12, 0), captures=["d2"])
    assert second["nozzle_fit_captures"] == {first["version"]: ["d1"], second["version"]: ["d2"]}


def test_dataset_and_camera_fit_refuse_widths_at_another_aim(refitted, tmp_path, monkeypatch):
    from tools.model import dataset as D
    from tools.model import fit_camera as fc

    wc, old, new, _ = refitted
    monkeypatch.setattr(D, "CAPTURES", wc.CAPTURES)
    widths = tmp_path / "out"
    widths.mkdir()
    for cap, version in (("after", new["version"]), ("golden-20261020-1736", "0.2")):
        pd.DataFrame({"file": ["ir/a.jpg"], "geometry_version": version}).to_parquet(widths / f"widths_{cap}.parquet")
        assert D.read_widths(cap, widths, require_final=False).geometry_version.iloc[0] == version
    pd.DataFrame({"file": ["ir/a.jpg"], "geometry_version": "0.2"}).to_parquet(widths / "widths_after.parquet")
    with pytest.raises(SystemExit, match="widths_after.parquet: after was filmed"):
        D.read_widths("after", widths, require_final=False)
    monkeypatch.setattr(fc, "MODELS_DIR", tmp_path)
    pd.DataFrame({"geometry_version": ["0.1", None]}).to_parquet(widths / "frames.parquet")
    with pytest.raises(SystemExit, match="not the installed 0.2"):
        fc.main(["--data", str(widths), "--dry-run"])


def test_width_check_measures_each_capture_with_the_geometry_of_its_aim(refitted, monkeypatch):
    wc, old, new, backup = refitted
    used = []

    def run(cap, ex, limit=None, skip=0):
        used.append((cap, ex.geometry.model["version"]))
        return pd.DataFrame({"file": ["ir/a.jpg"], "reason": ["ok"], "width_px": [3.0]}), pd.DataFrame()

    monkeypatch.setattr(wc, "run_capture", run)
    monkeypatch.setattr(wc, "report", lambda cap, W: None)
    for argv in ([], ["golden-20261020-1736", "after"], ["after", "--geometry", str(backup)]):
        monkeypatch.setattr("sys.argv", ["width_check", *argv])
        with pytest.raises(SystemExit) as e:
            wc.main()
        assert e.value.code == 1 and used == [] and not wc.OUT.exists()
    monkeypatch.setattr("sys.argv", ["width_check", "print-20261020-1531", "--no-write"])
    wc.main()
    assert used.pop() == ("print-20261020-1531", new["version"]) and not any(wc.OUT.iterdir())
    monkeypatch.setattr("sys.argv", ["width_check", "print-20261020-1531", "--geometry", str(backup)])
    wc.main()
    assert used.pop() == ("print-20261020-1531", "0.2")
    assert pd.read_parquet(wc.OUT / "widths_print-20261020-1531.parquet").geometry_version.tolist() == ["0.2"]


# recorded frames ------------------------------------------------------------------------------------------------------
G1 = CAPTURES / "golden-20261020-1736"


@pytest.mark.skipif(not (G1 / "ir.jsonl").exists() or not (MODELS_DIR / "geometry.json").exists(),
                    reason="needs the first golden print recording and the local geometry model")
def test_recorded_golden_print_regression():
    from tools.model.width_check import run_capture

    W, _ = run_capture(G1.name, WidthExtractor(Geometry.load()), skip=1300, limit=300)
    ok = W[W.reason == "ok"]
    assert len(ok) > 0.3 * len(W)
    med = ok.groupby("group").width_px.median()
    for grp, ref in {"cam1:-90": 3.11, "cam2:0": 3.20, "cam0:135": 2.34}.items():  # this stretch, 21 Oct build
        assert med[grp] == pytest.approx(ref, abs=0.1)
    assert ok.runtime_ms.median() < 20
