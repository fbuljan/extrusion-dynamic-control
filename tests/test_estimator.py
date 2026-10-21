import json
import math
import threading

import numpy as np
import pytest

from server.camera_model import CameraModel
from server.estimator import WidthEstimator
from server.model_stubs import StubCameraModel

T0 = 1_700_000_000_000
CAM = StubCameraModel()


def sample(t_s, mm, group=1, nozzle_c=220.0, speed_mm_s=20.0, reason="ok", camera_model=CAM, **extra):
    t_ms = T0 + round(t_s * 1000)
    px = camera_model.ir_px(mm, group, nozzle_c, speed_mm_s) if mm is not None else None
    return {"t_ms": t_ms, "recv_ms": t_ms + 850, "camera": group, "group": group, "heading_deg": 0.0, "axis_deg": 0.0,
            "width_px": px, "mm_per_px": 1.0, "contrast": 1.0, "confidence": 0.9, "reason": reason, **extra}


def fed(values, start_s=0.0, step_s=1.0, est=None, **kw):
    est = est or WidthEstimator(CAM)
    for i, mm in enumerate(values):
        est.add(sample(start_s + i * step_s, mm, **kw), kw.get("nozzle_c", 220.0), kw.get("speed_mm_s", 20.0))
    return est


def now(t_s):
    return T0 + round(t_s * 1000) + 900


def noisy(n, sd=0.05, seed=0, mm=1.5):
    rng = np.random.default_rng(seed)
    return [mm + rng.normal(0, sd) for _ in range(n)]


def test_none_until_min_samples_then_a_robust_location():
    est = fed([1.50, 1.52, 1.48, 1.51])
    assert est.estimate(now(3)) is None and est.reason == "4/5 samples"
    fed([3.0], start_s=4, est=est)  # one wild sample: the median ignores it
    e = est.estimate(now(4))
    assert e["n"] == 5 and e["mm"] == pytest.approx(1.51, abs=1e-9)
    assert set(e) >= {"mm", "sigma_mm", "n", "age_ms", "per_group", "groups_active", "confidence", "since_ms"}
    json.dumps(e)
    est = fed(noisy(40) + [3.0, 0.2])  # once the noise scale is known, outliers are trimmed before averaging
    assert est.estimate(now(41))["mm"] == pytest.approx(1.5, abs=0.03)


def test_rejected_unsupported_unconverted_and_non_finite_samples_are_counted_not_used():
    cam = StubCameraModel(groups={1})
    est = WidthEstimator(cam)
    est.add(sample(0, 1.5, reason="dropout"), 220, 20)
    est.add({**sample(1, 1.5), "width_px": None}, 220, 20)
    est.add(sample(2, 1.5, group=7, camera_model=cam), 220, 20)
    est.add(sample(3, 1.5), None, 20)
    est.add(sample(4, 1.5), 220, math.nan)
    est.add({**sample(5, 1.5), "width_px": math.inf}, 220, 20)
    est.add({**sample(6, 1.5), "confidence": math.nan}, 220, 20)
    assert est.skipped == {"rejected": 2, "group unsupported": 1, "no settings": 2, "non-finite": 2}
    assert WidthEstimator(None).add(sample(0, 1.5), 220, 20) is None


class NaNCamera(StubCameraModel):
    def real_mm(self, width_px, group, nozzle_c, speed_mm_s):
        return (math.nan, 0.03) if width_px > 9 else super().real_mm(width_px, group, nozzle_c, speed_mm_s)


def test_a_non_finite_conversion_never_poisons_the_estimate_or_the_noise_scale():
    est = WidthEstimator(NaNCamera())
    values = noisy(80)
    for i, mm in enumerate(values):
        s = sample(i, mm)
        if i % 10 == 5:
            s["width_px"] = 10.0
        est.add(s, 220, 20)
        e = est.estimate(now(i))
        assert e is None or all(math.isfinite(e[k]) for k in ("mm", "sigma_mm", "confidence"))
    assert est.skipped["non-finite"] == 8 and math.isfinite(est.noise_mm())


def test_view_field_wins_over_camera():
    cam = StubCameraModel(offsets_px={"leg-a": 2.0})
    est = WidthEstimator(cam)
    for i in range(6):
        est.add({**sample(i, 1.5, group="leg-a", camera_model=cam), "camera": 3}, 220, 20)
    assert est.estimate(now(5))["per_group"].keys() == {"leg-a"}
    assert est.estimate(now(5))["mm"] == pytest.approx(1.5)


def test_uses_nozzle_and_speed_at_the_frame_time():
    hot = fed([1.5] * 6, nozzle_c=245.0, speed_mm_s=12.0)  # px include glow and slow-speed terms
    assert hot.estimate(now(5))["mm"] == pytest.approx(1.5)
    wrong = WidthEstimator(CAM)
    for i in range(6):
        wrong.add(sample(i, 1.5, nozzle_c=245.0), 220.0, 20.0)  # converted with the wrong temperature
    assert wrong.estimate(now(5))["mm"] == pytest.approx(1.5 + 0.008 * 25)


def test_stale_and_window():
    est = fed([1.5] * 8)
    assert est.estimate(now(7))["age_ms"] == 50  # received 850 ms after acquisition, asked 900 ms after
    assert est.estimate(now(7) + 5000) is None and est.reason == "stale"
    est = fed([1.0] * 10 + [2.0] * 30)
    assert est.estimate(now(39))["mm"] == pytest.approx(2.0)  # the 1.0 mm samples left the 25 s window


def test_since_gates_on_deposit_time_with_receive_time_as_the_fallback():
    est = fed([1.4] * 10)
    fed([1.6] * 6, start_s=10, est=est)
    assert est.estimate(now(15))["mm"] == pytest.approx(1.4)  # 10 old vs 6 new
    e = est.estimate(now(15), since_ms=T0 + 10_850)  # no t_dep_ms: received at T0 + 10.85 s and later
    assert e["mm"] == pytest.approx(1.6) and e["n"] == 6 and e["since_ms"] == T0 + 10_850
    assert est.estimate(now(15), since_ms=T0 + 12_000) is None
    est2 = WidthEstimator(CAM)
    for i in range(6):  # deposited 5 s before acquisition: all of them predate the cutoff
        est2.add(sample(10 + i, 1.6, t_dep_ms=T0 + (5 + i) * 1000), 220, 20)
    assert est2.estimate(now(15), since_ms=T0 + 10_000) is None
    assert est2.estimate(now(15), since_ms=T0 + 5_000)["n"] == 6


@pytest.mark.parametrize("skew_s", [-3600, -5, 5])
def test_the_device_clock_is_never_compared_with_ours(skew_s):
    est = WidthEstimator(CAM)
    cutoff = T0 + 10_000
    first = None
    for i in range(40):
        local = T0 + i * 1000
        s = {**sample(0, 1.5), "t_ms": local - 900 + skew_s * 1000, "recv_ms": local, "t_dep_ms": None}
        est.add(s, 220, 20)
        e = est.estimate(local, since_ms=cutoff)
        if e and e["n"] >= 6 and first is None:
            first = i
        assert est.estimate(local) is None or est.estimate(local)["age_ms"] == 0
    assert first == 15  # six samples received from 10 s on, whatever the device clock says


def test_noise_scale_is_learned_fast_and_sigma_is_calibrated():
    rng = np.random.default_rng(1)
    est, misses, sigmas, noise, learned = WidthEstimator(CAM), 0, [], [], None
    for i in range(600):
        est.add(sample(i, 1.5 + rng.normal(0, 0.13), group=i // 10 % 3), 220, 20)  # three views, 10 s each
        if learned is None and est.noise_mm() is not None:
            learned = i
        e = est.estimate(now(i))
        if i > 60 and e:
            misses += abs(e["mm"] - 1.5) > 2 * e["sigma_mm"]
            sigmas.append(e["sigma_mm"])
            noise.append(est.noise_mm())
    assert learned <= 30
    assert np.mean(noise) == pytest.approx(0.13, rel=0.2)
    assert np.mean(sigmas) == pytest.approx(0.13 / math.sqrt(25), rel=0.3)  # 25 samples in the window
    assert misses / len(sigmas) < 0.1


def test_before_the_noise_scale_is_learned_sigma_is_the_medians_standard_error():
    values = [1.45, 1.55] * 3 + [1.5, 1.5]
    est = fed(values)
    assert est.noise_mm() is None
    spread = 1.4826 * 0.05  # MAD of the window
    expected = 1.2533 * spread / math.sqrt(len(values))
    assert est.estimate(now(len(values) - 1))["sigma_mm"] == pytest.approx(expected, rel=1e-3)


def test_camera_model_sigma_is_reported_apart_and_never_widens_sigma():
    tight, loose = WidthEstimator(StubCameraModel(sigma_mm=0.0)), WidthEstimator(StubCameraModel(sigma_mm=0.1))
    for est in (tight, loose):
        fed(noisy(40), est=est)
    t, lo = tight.estimate(now(39)), loose.estimate(now(39))
    assert lo["sigma_mm"] == pytest.approx(t["sigma_mm"]) and lo["mm"] == pytest.approx(t["mm"])
    assert t["model_sigma_mm"] == 0.0 and lo["model_sigma_mm"] == pytest.approx(0.1)
    assert lo["per_group"]["1"]["model_sigma_mm"] == pytest.approx(0.1)


def test_estimate_reports_the_cameras_ceiling_and_when_it_is_at_it():
    est = fed(noisy(30, sd=0.01, mm=1.47), est=WidthEstimator(StubCameraModel(knee_mm=1.5)))
    e = est.estimate(now(29))
    assert e["ceiling_mm"] == 1.5 and e["at_ceiling"]
    est = fed(noisy(30, sd=0.01, mm=1.44), est=WidthEstimator(StubCameraModel(knee_mm=1.5)))
    assert not est.estimate(now(29))["at_ceiling"]
    e = fed(noisy(30, sd=0.01, mm=1.47)).estimate(now(29))  # a model without a knee
    assert e["ceiling_mm"] is None and not e["at_ceiling"]


def runtime_camera_model():
    """The runtime CameraModel on made-up parameters (fitted files stay local): cam2:-90 is not in the profile."""
    group = {"k_px_per_mm": 2.0, "offset_px": 0.0, "knee_mm": 1.5, "valid_px": [2.2, 3.4], "sigma_mm": 0.07,
             "k_rel_sd": 0.0, "ramp_mm": 0.05}
    return CameraModel({"version": "test", "profile": "p", "ref": {"nozzle_c": 220.0, "speed_mm_s": 20.0},
                        "g_T_px_per_c": 0.0, "s_px_per_mm_s": 0.0, "above_knee_sigma_mm": 0.25,
                        "profiles": {"p": {"groups": {g: dict(group) for g in ("cam0:135", "cam1:-90", "cam3:45")}}}})


def test_runtime_camera_model_groups_systematic_sigma_and_readings_above_the_knee():
    cam, rng = runtime_camera_model(), np.random.default_rng(3)
    groups = ("cam2:-90", "cam0:135", "cam1:-90", "cam3:45")
    est = WidthEstimator(cam)
    for i in range(80):
        g = groups[i // 5 % 4]
        est.add({**sample(i, None, group=g), "camera": g, "width_px": 2.0 * (1.4 + rng.normal(0, 0.02))}, 220, 20)
    assert est.skipped == {"group unsupported": 20}
    e = est.estimate(now(79))
    assert set(e["per_group"]) <= {"cam0:135", "cam1:-90", "cam3:45"} and e["groups_active"] == 3
    assert e["mm"] == pytest.approx(1.4, abs=0.02) and e["confidence"] > 0.8
    assert e["model_sigma_mm"] == pytest.approx(0.07, rel=0.05) and e["sigma_mm"] < 0.03
    assert e["ceiling_mm"] == 1.5 and not e["at_ceiling"]
    for i in range(80, 110):  # wider than the knee: the IR reads past the valid range
        g = groups[1 + i // 5 % 3]
        est.add({**sample(i, None, group=g), "camera": g, "width_px": 3.6 + rng.normal(0, 0.04)}, 220, 20)
    e = est.estimate(now(109))
    assert e["in_range"] < 0.1 and e["confidence"] < 0.5  # the controller holds on low confidence
    assert e["model_sigma_mm"] > 0.2 and e["sigma_mm"] < 0.05 and e["at_ceiling"]


def test_confidence_low_until_noise_learned_and_with_few_samples():
    est = fed([1.5, 1.51, 1.49, 1.5, 1.52])
    assert est.estimate(now(4))["confidence"] == pytest.approx(0.9 * 5 / 10 * 0.5)
    est = fed([1.5 + 0.01 * (i % 3) for i in range(40)])
    assert est.noise_mm() is not None
    assert est.estimate(now(39))["confidence"] == pytest.approx(0.9)


def test_view_offsets_are_learned_relative_and_removed():
    rng = np.random.default_rng(2)
    est = WidthEstimator(StubCameraModel(offsets_px={1: 1.6, 2: 1.6, 3: 1.6}))
    true_offsets = {1: 0.15, 2: -0.15, 3: 0.0}  # what the views really add, unknown to the camera model
    errors = []
    for i in range(600):
        group = 1 + i // 10 % 3
        mm = 1.5 + true_offsets[group] + rng.normal(0, 0.05)
        est.add(sample(i, mm, group=group), 220, 20)
        e = est.estimate(now(i))
        if e and i > 300:
            errors.append(e["mm"] - 1.5)
    learned = est.estimate(now(599))["view_offsets_mm"]
    for g, o in true_offsets.items():
        assert learned[str(g)] == pytest.approx(o, abs=0.03)
    assert sum(learned.values()) == pytest.approx(0.0, abs=1e-9)  # relative only: the absolute level stays
    assert np.abs(errors).max() < 0.06 and abs(np.mean(errors)) < 0.02
    plain = WidthEstimator(CAM, {"harmonize_s": 0})
    for i in range(80):
        group = 1 + i // 10 % 3
        plain.add(sample(i, 1.5 + true_offsets[group] + rng.normal(0, 0.05), group=group), 220, 20)
    e = plain.estimate(now(79))
    assert e["view_offsets_mm"] == {} and e["sigma_mm"] > 0.05  # disagreeing views widen sigma instead


@pytest.mark.parametrize("cap", [1.0, 1.5])
def test_views_that_disagree_widen_sigma_until_their_offsets_are_learned(cap):
    # At 2 frames/s the random part alone is small enough for unlearned view offsets to look like a deviation.
    rng = np.random.default_rng(3)
    est = WidthEstimator(StubCameraModel(offsets_px={1: 1.6, 2: 1.6, 3: 1.6}), {"view_spread_cap": cap})
    true_offsets = {1: 0.15, 2: -0.15, 3: 0.0}
    early, late = [], []
    for i in range(600):
        t = i / 2
        group = 1 + int(t // 10) % 3
        est.add(sample(t, 1.5 + true_offsets[group] + rng.normal(0, 0.05), group=group), 220, 20)
        e = est.estimate(now(t))
        if e and len(e["per_group"]) > 1:
            random = 1 / math.sqrt(sum(p["sigma_mm"] ** -2 for p in e["per_group"].values()))
            (early if t < 50 else late if t > 200 else []).append(e["sigma_mm"] / random)
    assert min(early) > 2  # offsets still being learned: the views' disagreement counts in full
    assert min(late) >= 1 - 1e-9 and max(late) <= cap + 1e-9  # learned: at most the cap


def test_frames_less_than_a_second_apart_count_once_in_sigma_but_all_in_the_mean():
    # On the square at 2 frames/s, frames 0.5 s apart correlated 0.5: two of them say little more than one.
    values = noisy(40, seed=4)
    bucketed = fed(values, step_s=0.5).estimate(now(19.5))
    every = fed(values, step_s=0.5, est=WidthEstimator(CAM, {"sigma_bucket_s": 0})).estimate(now(19.5))
    assert bucketed["mm"] == pytest.approx(every["mm"]) and bucketed["n"] == every["n"] == 40
    assert bucketed["sigma_mm"] == pytest.approx(every["sigma_mm"] * math.sqrt(40 / 21))  # 21 distinct seconds
    at_1_hz = fed(values[:20]).estimate(now(19))
    assert at_1_hz["sigma_mm"] == pytest.approx(
        fed(values[:20], est=WidthEstimator(CAM, {"sigma_bucket_s": 0})).estimate(now(19))["sigma_mm"])


def test_groups_active_counts_views_seen_in_the_last_minute():
    est = WidthEstimator(CAM)
    for i in range(90):
        est.add(sample(i, 1.5, group="a" if i < 20 else "b" if i < 40 else "c"), 220, 20)
    assert est.estimate(now(89))["groups_active"] == 2  # "a" was last seen more than a minute ago


def test_estimate_counts_as_in_range_when_half_its_samples_are():
    cam = StubCameraModel(floor_px=1.8, bead_px_range=(0.3, math.inf), offset_px=0.0)
    est = WidthEstimator(cam)
    for i in range(30):
        est.add(sample(i, 0.6 if i % 2 else 1.5, camera_model=cam), 220, 20)
    e = est.estimate(now(29))
    assert e["in_range"] == pytest.approx(0.5, abs=0.04) and e["confidence"] > 0.8
    est = WidthEstimator(cam)
    for i in range(30):
        est.add(sample(i, 0.6 if i % 4 else 1.5, camera_model=cam), 220, 20)  # mostly at the blur floor
    assert est.estimate(now(29))["confidence"] < 0.5


def test_add_and_estimate_from_different_threads():
    est = WidthEstimator(CAM, {"window_s": 1e9})
    errors, stop = [], threading.Event()

    def worker():
        i = 0
        while not stop.is_set():
            i += 1
            est.add({**sample(0, 1.5 + (i % 7) * 0.01, group=i % 3), "recv_ms": T0 + i}, 220, 20)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        for _ in range(300):
            try:
                est.estimate(T0 + 10**9)
            except Exception as e:  # noqa: BLE001 - any exception here is the failure being tested for
                errors.append(repr(e))
    finally:
        stop.set()
        thread.join()
    assert errors == []


def test_accepts_width_sample_objects():
    width = pytest.importorskip("server.width")
    est = WidthEstimator(CAM)
    fields = width.WidthSample.__dataclass_fields__
    for i in range(6):
        s = sample(i, 1.5)
        est.add(width.WidthSample(**{k: s.get(k) for k in fields}), 220, 20)
    e = est.estimate(T0 + 5000 + 850)
    assert e["mm"] == pytest.approx(1.5)
