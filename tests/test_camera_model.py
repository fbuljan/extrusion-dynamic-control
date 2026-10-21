import json
import math

import numpy as np
import pandas as pd
import pytest

from server.camera_model import CameraModel
from server.estimator import WidthEstimator
from server.geometry import Geometry
from server.model_files import MODELS_DIR
from server.pipeline import geometry_version, load_models, width_config_hash
from server.width import DEFAULTS as WIDTH_DEFAULTS, WidthExtractor
from tools.model import fit_camera as fc

REAL = MODELS_DIR / "camera_model.json"
DATA = fc.DATA
HAVE_DATA = (DATA / "frames.parquet").exists() and (DATA / "bands.parquet").exists()
needs_data = pytest.mark.skipif(not HAVE_DATA, reason="no data/model parquets (local recordings)")


def group(k=2.7, offset=-0.8, knee=1.5, lo=2.13, hi=3.35, sigma=0.04, ramp=0.04, k_rel_sd=0.1, k_nom=1.45):
    return {"k_px_per_mm": k, "k_nom_px_per_mm": k_nom, "offset_px": offset, "knee_mm": knee, "valid_px": [lo, hi],
            "sigma_mm": sigma, "ramp_mm": ramp, "k_rel_sd": k_rel_sd}


def model_data(**over):
    return {"version": "test", "relative": False, "profile": "print2", "ref": {"nozzle_c": 220.0, "speed_mm_s": 20.0},
            "g_T_px_per_c": 0.0074, "s_px_per_mm_s": -0.01, "above_knee_sigma_mm": 0.25,
            "geometry_version": "0.2", "width_config_hash": "abc123def456", "choice": {"written": "absolute"},
            "profiles": {"print2": {"groups": {"cam3:45": group(), "cam1:-90": group(k=2.0, offset=0.0, lo=2.41,
                                                                                     knee=1.48)}},
                         "print1": {"groups": {"cam2:0": group(k=2.05, offset=0.66, knee=None, hi=None)}}},
            **over}


CAM = CameraModel(model_data())


# runtime ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mm", [1.0, 1.2, 1.35, 1.47])
@pytest.mark.parametrize("nozzle_c,speed", [(220, 20), (200, 20), (240, 12), (231.5, 26.5)])
def test_inverse_of_forward_is_identity_below_the_knee(mm, nozzle_c, speed):
    for g in ("cam3:45", "cam1:-90"):
        got, sigma = CAM.real_mm(CAM.ir_px(mm, g, nozzle_c, speed), g, nozzle_c, speed)
        assert got == pytest.approx(mm, abs=1e-9) and 0 < sigma < 0.25


def test_hotter_nozzle_and_slower_travel_read_wider_and_are_corrected():
    assert CAM.ir_px(1.3, "cam3:45", 240, 20) > CAM.ir_px(1.3, "cam3:45", 220, 20)
    assert CAM.ir_px(1.3, "cam3:45", 220, 12) > CAM.ir_px(1.3, "cam3:45", 220, 20)
    px = CAM.ir_px(1.3, "cam3:45", 220, 20)
    assert CAM.real_mm(px, "cam3:45", 240, 20)[0] < 1.3 < CAM.real_mm(px, "cam3:45", 200, 20)[0]
    assert CAM.real_mm(px, "cam3:45", 220, 12)[0] < 1.3


def test_above_the_knee_reads_flat_and_clips_two_ramps_up_with_a_large_sigma():
    knee_px = CAM.ir_px(1.5, "cam3:45", 220, 20)
    assert CAM.ir_px(1.7, "cam3:45", 220, 20) == pytest.approx(knee_px)
    mm, sigma = CAM.real_mm(knee_px + 0.5, "cam3:45", 220, 20)
    assert mm == pytest.approx(1.5 + 2 * 0.04) and sigma >= 0.25
    at_mm, at = CAM.real_mm(knee_px, "cam3:45", 220, 20)
    below = CAM.real_mm(CAM.ir_px(1.3, "cam3:45", 220, 20), "cam3:45", 220, 20)[1]
    assert at_mm == pytest.approx(1.5) and below < 0.1
    assert at == pytest.approx(math.hypot(0.04, 0.125), rel=1e-6)  # half-way up the ramp at the knee


def test_frames_scattered_around_the_knee_average_to_the_knee():
    knee_px = CAM.ir_px(1.5, "cam3:45", 220, 20)
    noise = np.random.default_rng(0).normal(0, 0.04 * 2.7, 4000)  # one ramp (= frame SD) in px
    mms = [CAM.real_mm(knee_px + e, "cam3:45", 220, 20)[0] for e in noise]
    assert np.mean(mms) == pytest.approx(1.5, abs=0.005)
    assert max(mms) == pytest.approx(1.58)


def test_sigma_rises_smoothly_through_the_knee():
    pxs = np.linspace(CAM.ir_px(1.3, "cam3:45", 220, 20), CAM.ir_px(1.5, "cam3:45", 220, 20) + 0.4, 200)
    sig = [CAM.real_mm(px, "cam3:45", 220, 20)[1] for px in pxs]
    ramp_start = int(pxs.searchsorted(CAM.ir_px(1.46, "cam3:45", 220, 20)))
    assert all(b >= a - 1e-12 for a, b in zip(sig[ramp_start:-1], sig[ramp_start + 1:]))
    assert max(abs(b - a) for a, b in zip(sig, sig[1:])) < 0.02


def test_k_uncertainty_grows_away_from_the_knee():
    s14 = CAM.real_mm(CAM.ir_px(1.4, "cam3:45", 220, 20), "cam3:45", 220, 20)[1]
    s10 = CAM.real_mm(CAM.ir_px(1.0, "cam3:45", 220, 20), "cam3:45", 220, 20)[1]
    assert s10 == pytest.approx(math.hypot(0.04, 0.5 * 0.1)) and s14 < s10


def test_knee_and_ceiling():
    assert CAM.knee_mm("cam3:45") == 1.5 and CAM.knee_mm("cam1:-90") == 1.48
    assert CAM.knee_mm("cam2:-90") is None and CAM.ceiling_mm() == 1.48
    print1 = CameraModel(model_data(), profile="print1")  # its only group has no knee
    assert print1.knee_mm("cam2:0") is None and print1.ceiling_mm() is None


def test_unfitted_group_and_bad_inputs_give_none():
    assert CAM.real_mm(3.0, "cam2:-90", 220, 20) is None and CAM.ir_px(1.5, "cam2:-90", 220, 20) is None
    assert CAM.real_mm(3.0, "cam2:0", 220, 20) is None  # print 1's group, not in the active profile
    for bad in (None, math.nan, math.inf, True, "3.0"):
        assert CAM.real_mm(bad, "cam3:45", 220, 20) is None
        assert CAM.real_mm(3.0, "cam3:45", bad, 20) is None
        assert CAM.real_mm(3.0, "cam3:45", 220, bad) is None
    lo, hi = CAM.valid_px_range("cam2:-90")
    assert not lo <= 3.0 <= hi


def test_numpy_scalars_are_accepted():
    plain = CAM.real_mm(2.9, "cam3:45", 220.0, 20.0)
    assert CAM.real_mm(np.float32(2.9), "cam3:45", np.int64(220), np.float64(20)) == pytest.approx(plain, rel=1e-6)
    assert CAM.real_mm(np.float64(np.nan), "cam3:45", 220, 20) is None


def test_valid_range_brackets_the_baseline():
    lo, hi = CAM.valid_px_range("cam3:45")
    assert lo < CAM.ir_px(1.35, "cam3:45", 220, 20) < CAM.ir_px(1.5, "cam3:45", 220, 20) < hi


def test_other_profile_and_no_knee():
    cam = CameraModel(model_data(), profile="print1")
    assert cam.real_mm(3.0, "cam3:45", 220, 20) is None
    mm, sigma = cam.real_mm(cam.ir_px(1.8, "cam2:0", 220, 20), "cam2:0", 220, 20)
    assert mm == pytest.approx(1.8) and sigma == pytest.approx(0.04)
    assert cam.valid_px_range("cam2:0") == (2.13, math.inf)


def test_relative_file_is_ir_width_in_geometric_mm():
    rel = model_data(relative=True, g_T_px_per_c=0.0, s_px_per_mm_s=0.0, profiles={"print2": {"groups": {
        "cam3:45": group(k=1.45, offset=0.0, knee=3.2 / 1.45, hi=3.35, sigma=0.13, k_rel_sd=0.0, ramp=0.0)}}})
    cam = CameraModel(rel)
    assert cam.relative
    assert cam.real_mm(2.9, "cam3:45", 240, 12)[0] == pytest.approx(2.9 / 1.45)  # no glow or speed correction
    assert cam.real_mm(3.3, "cam3:45", 220, 20)[0] == pytest.approx(3.2 / 1.45)  # the measured plateau still caps


def test_provenance_is_exposed():
    assert CAM.geometry_version == "0.2" and CAM.width_config_hash == "abc123def456"
    assert CAM.choice == {"written": "absolute"}


def test_fit_records_the_apps_provenance(tmp_path):
    geo = {"version": "9.9", "image_size": [160, 120], "cameras": {}}
    (tmp_path / "geometry.json").write_text(json.dumps(geo))
    prov = fc.provenance(tmp_path / "geometry.json")
    assert prov["geometry_version"] == geometry_version(geo) == "9.9"
    assert prov["width_config_hash"] == width_config_hash({**WIDTH_DEFAULTS})
    assert fc.provenance(tmp_path / "geometry.json", {"lateral_search_px": 2.0})["width_config_hash"] != \
        prov["width_config_hash"]


def test_load_raises_for_a_missing_file_and_rejects_broken_ones(tmp_path):
    with pytest.raises(FileNotFoundError):
        CameraModel.load(tmp_path / "camera_model.json")
    (tmp_path / "camera_model.json").write_text("{}")
    with pytest.raises(KeyError):
        CameraModel.load(tmp_path / "camera_model.json")
    bad = model_data()
    bad["profiles"]["print2"]["groups"]["cam3:45"]["k_px_per_mm"] = 0.0
    (tmp_path / "camera_model.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        CameraModel.load(tmp_path / "camera_model.json")
    assert load_models(tmp_path)["camera_model"] is None
    (tmp_path / "camera_model.json").write_text(json.dumps(model_data()))
    assert CameraModel.load(tmp_path / "camera_model.json").version == "test"
    assert load_models(tmp_path)["camera_model"].version == "test"


def test_drop_in_for_the_estimator():
    est = WidthEstimator(CAM)
    rng = np.random.default_rng(0)
    for i, mm in enumerate([1.35] * 40 + [1.5] * 80):
        g = "cam3:45" if i % 2 else "cam1:-90"
        px = CAM.ir_px(mm, g, 220, 20) + rng.normal(0, 0.05)
        est.add({"t_ms": i * 500, "recv_ms": i * 500, "width_px": px, "reason": "ok", "group": g, "confidence": 0.9},
                220, 20)
        if i == 39:
            narrow = est.estimate(i * 500)
    wide = est.estimate(119 * 500)
    assert narrow["mm"] == pytest.approx(1.35, abs=0.03) and narrow["in_range"] == 1.0
    assert not narrow["at_ceiling"] and narrow["ceiling_mm"] == 1.48
    assert wide["mm"] == pytest.approx(1.49, abs=0.03) and wide["at_ceiling"]
    assert wide["model_sigma_mm"] > 0.1 > narrow["model_sigma_mm"]


# fit tool, synthetic --------------------------------------------------------------------------------------------------
def synthetic_rows(kappa=1.8, knee=1.5, gT=0.007, s=-0.01, noise=0.02, seed=0, p="print2"):
    rng = np.random.default_rng(seed)
    bands = [(1.5, 220, 20), (1.35, 220, 20), (1.6, 220, 20), (1.6, 220, 12), (1.3, 200, 20), (1.5, 220, 20),
             (1.7, 220, 20), (1.5, 220, 20), (1.2, 220, 20), (1.4, 240, 20)]
    rows = []
    for g, k_nom, off in (("a", 1.45, -0.8), ("b", 1.1, 0.0), ("c", 1.48, -1.0)):
        for b, (mm, T, v) in enumerate(bands, 1):
            px = kappa * k_nom * min(mm, knee) + gT * (T - 220) + s * (v - 20) + off + rng.normal(0, noise)
            rows.append({"p": p, "g": g, "b": b, "px": px, "se": noise, "T": T, "v": v, "k_nom": k_nom, "mm": mm,
                         "cal_sd": 0.01, "kind": "flow" if T == 220 and v == 20 else "nozzle" if v == 20 else "speed",
                         "plan": f"band {b}", "defect": False})
    return pd.DataFrame(rows)


def test_fit_recovers_a_known_hinge():
    R = synthetic_rows()
    o = fc.fit(R, {"print2": fc.KNEE_GRID["print2"]})
    named, offsets = fc.params(o)
    assert o["knees"]["print2"] == pytest.approx(1.5, abs=0.03) and o["identified"]
    assert named["kappa"][0] == pytest.approx(1.8, rel=0.1) and named["g_T"][0] == pytest.approx(0.007, abs=0.003)
    assert named["s:print2"][0] == pytest.approx(-0.01, abs=0.005)
    assert offsets[("b", "print2")][0] == pytest.approx(0.0, abs=0.15)
    mm, _ = fc.invert(o, R)
    assert np.allclose(mm[R.mm < 1.45], R.mm[R.mm < 1.45], atol=0.05)


def test_speed_term_is_per_print():
    R = pd.concat([synthetic_rows(s=-0.03, p="print1"), synthetic_rows(s=0.0, seed=1)], ignore_index=True)
    named = fc.fit(R)["named"]
    assert named["s:print1"][0] == pytest.approx(-0.03, abs=0.006)
    assert named["s:print2"][0] == pytest.approx(0.0, abs=0.006)


def test_kappa_needs_two_bands_under_a_knee_and_the_rank_test_ignores_k_nom_wobble():
    R = synthetic_rows()
    plateau = R[R.mm >= 1.5].reset_index(drop=True)
    plateau["k_nom"] = plateau.k_nom * (1 + 0.02 * np.sin(np.arange(len(plateau))))  # wobble alone must not count
    o = fc.fit_fixed(plateau, {"print2": 1.5})
    assert not o["rank_ok"] and not o["identified"] and o["n_below"] == 0
    one = R[(R.mm >= 1.5) | (R.mm == 1.35)].reset_index(drop=True)
    o = fc.fit_fixed(one, {"print2": 1.5})
    assert o["rank_ok"] and o["n_below"] == 1 and not o["identified"]


def test_calliper_sd_enters_the_weights_below_the_knee_only():
    R = synthetic_rows().assign(cal_sd=0.1)
    o = fc.fit_fixed(R, {"print2": 1.5})
    var_below = o["var"][(R.mm < 1.5).values]
    var_above = o["var"][(R.mm >= 1.5).values]
    k = o["named"]["kappa"][0]
    assert var_below.min() > var_above.max()
    assert var_below.min() - var_above.max() == pytest.approx((k * 1.1 * 0.1) ** 2, rel=0.05)


@pytest.mark.parametrize("ci,levers,force,written", [
    ((1.3, 2.4), {"flow": (1.4, True), "nozzle": (1.9, True)}, False, "absolute"),
    ((0.4, 2.4), {"flow": (1.4, True), "nozzle": (1.9, True)}, False, "relative"),
    ((1.3, 2.4), {"flow": (1.4, True)}, False, "relative"),
    ((1.3, 2.4), {"flow": (0.8, True), "nozzle": (1.9, True)}, False, "relative"),
    ((1.3, 2.4), {"flow": (1.4, False), "nozzle": (1.9, True)}, False, "relative"),
    ((1.3, 2.4), {"flow": (-0.2, True), "nozzle": (1.9, True)}, False, "relative"),
    ((0.4, 2.4), {"flow": (1.4, True)}, True, "absolute"),
])
def test_identifiability_rule(ci, levers, force, written):
    lv = {k: {"bands": [], "kappa_without": v, "identified": ok} for k, (v, ok) in levers.items()}
    write_absolute, choice = fc.decide(ci, lv, force)
    assert choice["written"] == written and write_absolute == (written == "absolute")
    assert choice["forced"] == (force and not choice["passed"])


def test_leave_one_band_out_never_trains_on_its_test_band():
    R = synthetic_rows()
    seen = []

    def spy(train):
        seen.append(set(zip(train.p, train.b)))
        return fc.fit(train, {"print2": fc.KNEE_GRID["print2"][::5]}, rounds=1)

    loo = fc.leave_one_band_out(R, R, fit_fn=spy)
    assert len(seen) == R.b.nunique()
    for trained, (key, _) in zip(seen, R.groupby(["p", "b"])):
        assert key not in trained and len(trained) == R.b.nunique() - 1
    assert set(zip(loo.p, loo.b)) == set(zip(R.p, R.b))


# fit tool, real data -------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real():
    F = fc.usable_frames(pd.read_parquet(DATA / "frames.parquet"))
    C = fc.calliper(pd.read_parquet(DATA / "bands.parquet"))
    Rall = fc.band_rows(F, C)
    return F, C, Rall, fc.fit_rows(Rall)


@needs_data
def test_golden_values(real):
    C = real[1].set_index(["p", "b"])
    assert C.loc[("print2", 2), "mm"] == pytest.approx(1.350, abs=0.002)
    assert C.loc[("print2", 6), "mm"] == pytest.approx(1.301, abs=0.002)
    assert C.loc[("print2", 10), "mm"] == pytest.approx(1.700, abs=0.002)
    assert C.loc[("print1", 6), "mm"] == pytest.approx(1.1)
    assert sorted(C[C.defect].reset_index().query("p == 'print2'").b) == [3, 7, 9]
    assert C.loc[("print2", 2), "cal_sd"] == pytest.approx(0.05, abs=0.001)


@needs_data
def test_fit_rows_exclusions(real):
    F, _, Rall, R = real
    assert not R.defect.any() and len(R) < len(Rall)
    keys = set(zip(R.g, R.p))
    assert ("cam2:-90", "print2") not in keys and ("cam0:135", "print1") not in keys
    assert {("cam3:45", "print2"), ("cam1:-90", "print2"), ("cam0:135", "print2")} <= keys
    assert not ((F.print_id == "print2") & (F.band == 1) & (F.z < 6.7)).any()
    assert ((F.print_id == "print2") & (F.band == 1)).sum() > 100


@needs_data
def test_print2_baselines_fall_inside_the_computed_valid_range(real):
    F, C, Rall, R = real
    best = fc.fit(R)
    loo = pd.DataFrame({"p": [], "g": [], "b": [], "defect": [], "mm": [], "pred": []}).astype({"defect": bool})
    boot = pd.DataFrame({"kappa": [best["named"]["kappa"][0]] * 3})
    absolute, relative = fc.build_models(best, boot, F, R, loo, fc.calliper_scatter(C))
    assert absolute["s_px_per_mm_s"] == best["named"]["s:print2"][0] and relative["s_px_per_mm_s"] == 0.0
    for g, e in absolute["profiles"]["print2"]["groups"].items():  # a window sees one pass: half the gap is error
        assert e["sigma_mm"] >= abs(e["pass"]["inner_minus_outer_px"]) / 2 / e["k_px_per_mm"] - 1e-12
    for m in (absolute, relative):
        cam = CameraModel(m)
        assert "cam3:45" in cam.groups and "cam2:-90" not in cam.groups  # print 2's offsets, cam2 off
        base = Rall[(Rall.p == "print2") & Rall.b.isin(fc.BASELINE_BANDS["print2"])]
        for g in cam.groups:
            lo, hi = cam.valid_px_range(g)
            for px in base[base.g == g].px:
                assert lo <= px <= hi, (m["relative"], g, px, lo, hi)
            assert lo <= Rall[(Rall.p == "print2") & (Rall.g == g) & (Rall.b == 6)].px.iloc[0]  # nozzle 200 in range


@needs_data
def test_real_fit_signs_knee_and_rule(real):
    _, _, _, R = real
    best = fc.fit(R)
    named = best["named"]
    assert named["kappa"][0] > 1 and named["g_T"][0] > 0 and best["knees"]["print2"] == pytest.approx(1.5, abs=0.05)
    assert abs(named["s:print2"][0]) < 2 * named["s:print2"][1] + 0.005  # print 2: speed leaves the IR width alone


@pytest.mark.skipif(not REAL.exists(), reason="no fitted camera_model.json (local calibration)")
def test_real_file_loads_and_is_sane():
    data = json.loads(REAL.read_text())
    cam = CameraModel.load(REAL)
    assert cam.profile == "print2" and "cam2:-90" not in cam.groups and cam.groups
    assert data["choice"]["written"] == ("relative" if cam.relative else "absolute")
    assert cam.geometry_version and cam.width_config_hash
    geo = MODELS_DIR / "geometry.json"
    if geo.exists():
        assert cam.width_config_hash == width_config_hash(WidthExtractor(Geometry.load(geo)).cfg)
    for g in cam.groups:
        lo, hi = cam.valid_px_range(g)
        mm, sigma = cam.real_mm((lo + hi) / 2, g, 220, 20)
        assert lo < hi and 0 < sigma < 0.5 and 0.5 < mm < 3.0
        if not cam.relative:
            assert data["kappa"] > 0 and data["g_T_px_per_c"] > 0 and cam.knee_mm(g) == pytest.approx(1.5, abs=0.05)
            assert cam.real_mm(cam.ir_px(1.3, g, 220, 20), g, 220, 20)[0] == pytest.approx(1.3)
    if not cam.relative:
        assert cam.ceiling_mm() == pytest.approx(1.5, abs=0.05)
