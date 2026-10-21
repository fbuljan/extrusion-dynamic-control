import json
import math
import re

import numpy as np
import pandas as pd
import pytest

from conftest import CAPTURES
from tools.model import dataset as D

GOLDEN1, GOLDEN2, AFTERNOON = "golden-20261020-1736", "golden2-20261020-1815", "print-20261020-1531"
have_data = all((CAPTURES / c / "telemetry.jsonl").exists() and (CAPTURES / c / "ir.jsonl").exists()
                for c in (GOLDEN1, GOLDEN2, AFTERNOON))
needs_data = pytest.mark.skipif(not have_data, reason="no recordings in data/captures")


# synthetic telemetry ------------------------------------------------------------------------------------------------
V, A, B = np.array([-100.0, 0.0]), np.array([0.0, 100.0]), np.array([0.0, -100.0])
INCENTER, INRADIUS = np.array([-41.42, 0.0]), 41.42


def loop_samples(offset_mm, speed=20.0, dt=0.1):
    """Clockwise right triangle V -> A -> B -> V (legs at 45, -90, 135 deg), grown outward by offset_mm."""
    k = (INRADIUS + offset_mm) / INRADIUS
    pts = [INCENTER + (p - INCENTER) * k for p in (V, A, B)]
    out = []
    for p, q in zip(pts, pts[1:] + pts[:1]):
        u = (q - p) / np.linalg.norm(q - p)
        for i in range(int(np.linalg.norm(q - p) / (speed * dt))):
            out.append((*(p + u * speed * dt * i), *(u * speed)))
    return out


def synthetic_print(n_layers=4, prime=True, t0=1_000_000.0, spacing=1.5):
    """Motion series of a two-wall print (inner loop, then outer) with an optional priming pass at Z 0.5."""
    rows, t = [], t0

    def add(x, y, z, vx, vy, ext):
        nonlocal t
        rows.append((t, x, y, z, vx, vy, math.hypot(vx, vy), float(ext), 100.0))
        t += 100.0

    if prime:
        for _ in range(35):
            add(-100, 0, 0.5, 20, 0, True)
    starts = []
    for layer in range(1, n_layers + 1):
        for _ in range(4):
            add(-100, 0, 0.7 + 0.5 * (layer - 1) - 0.2, 0, 0, False)
        starts.append(t)
        for off in (0.0, spacing):
            for x, y, vx, vy in loop_samples(off):
                add(x, y, 0.7 + 0.5 * (layer - 1), vx, vy, True)
    cols = ["t", "x", "y", "z", "vx", "vy", "vmag", "ext", "spd"]
    series = {"mot": dict(zip(cols, np.array(rows).T))}
    t_all = series["mot"]["t"]
    grid = np.arange(t0, t_all[-1], 1400.0)
    series["noz"] = {"t": grid, "cur": np.full(len(grid), 220.0), "tgt": np.full(len(grid), 220.0)}
    series["flow"] = {"t": grid, "val": np.full(len(grid), 100.0)}
    return D.Telemetry(series, 250.0), starts


# layers, bands, passes, spacing ------------------------------------------------------------------------------------
def test_layer_counter_follows_z_steps_and_drops_the_priming_pass():
    tel, starts = synthetic_print(n_layers=4)
    layers = D.count_layers(tel, 0, 1e12)
    assert layers.n.tolist() == [1, 2, 3, 4]
    assert np.allclose(layers.z, [0.7, 1.2, 1.7, 2.2])
    assert np.allclose(layers.start_ms, starts, atol=250)  # the step is known from when it began to hold
    tel2, _ = synthetic_print(n_layers=3, prime=False)
    assert D.count_layers(tel2, 0, 1e12).n.tolist() == [1, 2, 3]


def test_layer_counter_ignores_jitter_and_z_hops_without_extrusion():
    lc = D.LayerCounter()
    events = [lc.update(t, z, e) for t, z, e in [(0, 0.5, True), (1, 0.5, True), (2.5, 0.5, True), (3, 0.9, False),
                                                    (4, 1.0, True), (5, 1.0, True), (6.5, 1.0, True), (7, 1.02, True)]]
    assert events == [False, False, True, False, False, False, True, False]
    assert lc.n == 2 and lc.z == 1.0


def test_layer_at_gives_zero_before_the_first_layer_and_the_period_of_the_previous_one():
    layers = pd.DataFrame({"n": [1, 2, 3], "start_ms": [1000.0, 3000.0, 7000.0], "z": [0.5, 1.0, 1.5]})
    n, t_in, prev = D.layer_at(layers, [500.0, 1000.0, 3500.0, 9000.0])
    assert n.tolist() == [0, 1, 2, 3]
    assert np.isnan(t_in[0]) and t_in[1] == 0 and t_in[2] == 0.5 and t_in[3] == 2.0
    assert np.isnan(prev[0]) and np.isnan(prev[1]) and prev[2] == 2.0 and prev[3] == 4.0


def test_band_edges_match_the_band_plans_of_both_prints():
    for pid, per_band in (("print1", [15] * 8), ("print2", [15] + [10] * 11)):
        first = D.PRINTS[pid].band_first_layers
        assert np.diff(first).tolist() == per_band
        n = np.arange(0, first[-1] + 5)
        band = D.band_of_layer(n, first)
        assert (band[n < first[0]] == 0).all() and (band[n >= first[-1]] == 0).all()
        assert [int((band == b).sum()) for b in range(1, len(per_band) + 1)] == per_band
    assert D.band_of_layer([3, 4], ()).tolist() == [0, 0]


def test_pass_labels_alternate_inner_then_outer_and_wall_spacing_is_measured():
    tel, _ = synthetic_print(n_layers=4)
    layers = D.count_layers(tel, 0, 1e12)
    end = tel.s["mot"]["t"][-1]
    bounds = D.loop_boundaries(tel, layers, end)
    assert sorted(bounds) == [1, 2, 3, 4] and all(np.isfinite(v) for v in bounds.values())
    for n, start in zip(layers.n, layers.start_ms):
        nxt = layers.start_ms.iloc[n] if n < len(layers) else end
        assert start < bounds[n] < nxt
        assert abs((bounds[n] - start) - (nxt - start) / 2) < 0.2 * (nxt - start)  # two equal loops
    sp = D.wall_spacings(tel, layers, bounds, end)
    assert len(sp) == 12 and sp.spacing_mm.between(1.45, 1.55).all()
    assert set(sp.leg_deg) == {45.0, -90.0, 135.0}


def test_layers_without_two_loops_get_no_pass():
    tel, _ = synthetic_print(n_layers=1, prime=False)
    m = tel.s["mot"]
    keep = m["t"] < m["t"][0] + 0.5 * (m["t"][-1] - m["t"][0])  # only the inner loop is there
    half = D.Telemetry({"mot": {c: v[keep] for c, v in m.items()}}, 0.0)
    layers = pd.DataFrame({"n": [1], "start_ms": [m["t"][0]], "z": [0.7]})
    assert math.isnan(D.loop_boundaries(half, layers, m["t"][-1])[1])


# joins --------------------------------------------------------------------------------------------------------------
def test_no_sample_is_joined_across_a_telemetry_gap():
    tel, _ = synthetic_print(n_layers=1, prime=False)
    m = tel.s["mot"]
    keep = (m["t"] < 1_005_000) | (m["t"] > 1_008_000)  # 3 s hole in the motion samples
    tel = D.Telemetry({"mot": {c: v[keep] for c, v in m.items()}, "noz": tel.s["noz"], "flow": tel.s["flow"]}, 250.0)
    q = np.array([1_004_950.0, 1_005_400.0, 1_006_500.0, 1_007_650.0, 1_008_050.0, np.nan])
    st = D.state_at(tel, q)
    assert st.mot_gap_ms.max() <= D.MAX_JOIN_MS
    assert st.x.notna().tolist() == [True, True, False, True, True, False]
    # readbacks: last sample at or before the query, never older than 3 s
    gone = D.Telemetry({"mot": tel.s["mot"], "noz": {"t": np.array([1_000_000.0]), "cur": np.array([219.0]),
                                                      "tgt": np.array([220.0])}}, 0.0)
    st = D.state_at(gone, np.array([999_000.0, 1_001_000.0, 1_002_999.0, 1_003_500.0]))
    assert st.nozzle_c.notna().tolist() == [False, True, True, False]
    assert st.readback_age_ms.dropna().max() <= D.MAX_READBACK_AGE_MS


def test_state_uses_the_last_readback_before_not_after():
    tel = D.Telemetry({"flow": {"t": np.array([0.0, 1400.0]), "val": np.array([80.0, 120.0])},
                       "mot": {"t": np.array([0.0]), "x": np.array([0.0]), "y": np.array([0.0]), "z": np.array([1.0]),
                               "vx": np.array([0.0]), "vy": np.array([0.0]), "vmag": np.array([0.0]),
                               "ext": np.array([0.0]), "spd": np.array([100.0])}}, 0.0)
    assert D.state_at(tel, np.array([1399.0, 1400.0])).flow_pct.tolist() == [80.0, 120.0]


def test_speed_in_mm_per_s_and_feed():
    tel = D.Telemetry({"mot": {"t": np.array([0.0]), "x": np.array([0.0]), "y": np.array([0.0]), "z": np.array([1.0]),
                               "vx": np.array([6.0]), "vy": np.array([8.0]), "vmag": np.array([12.0]),
                               "ext": np.array([1.0]), "spd": np.array([60.0])}}, 0.0)
    st = D.state_at(tel, np.array([0.0]))
    assert st.speed_mm_s[0] == 12.0 and st.feed_mm_s[0] == pytest.approx(20.0)
    assert st.motion_heading_deg[0] == pytest.approx(math.degrees(math.atan2(8, 6)))


def test_heading_group_matches_the_width_extractor_binning():
    assert D.heading_group(2, -90.2) == "cam2:-90"
    assert D.heading_group(0, 134.0) == "cam0:135"
    assert D.heading_group(3, 180.0) == "cam3:180"
    assert D.heading_group(1, -179.0) == "cam1:180"
    assert D.heading_group(1, None) == "cam1:none"
    try:
        from server.width import heading_group as app_group
    except ImportError:
        return
    for h in (-170, -91, -44, 0, 22.4, 22.6, 91, 135, 179):
        assert D.heading_group(1, h) == app_group(1, h)


# plans, labels, calliper, slicer ------------------------------------------------------------------------------------
def runner_events():
    t = lambda s: 1_000_000 + s * 1000
    return [
        {"t_ms": t(0), "event": "bands_start", "plan": ["baseline", "flow 80", "speed 60", "nozzle -20"]},
        {"t_ms": t(1), "event": "band_start", "band": 1, "settings": "as printed"},
        {"t_ms": t(10), "event": "band_start", "band": 2, "settings": {"flow": 55, "speed": 100.0, "nozzle_target": 220.0}},  # aborted runner
        {"t_ms": t(50), "event": "bands_start", "plan": ["baseline", "flow 80", "speed 60", "nozzle -20"]},
        {"t_ms": t(51), "event": "baseline", "flow": 100.0, "speed": 100.0, "nozzle_target": 220.0, "fan": 0.0},
        {"t_ms": t(52), "event": "band_start", "band": 1, "settings": {"flow": 100.0, "speed": 100.0, "nozzle_target": 220.0, "fan": 0.0}},
        {"t_ms": t(60), "event": "band_start", "band": 2, "settings": {"flow": 80, "speed": 100.0, "nozzle_target": 220.0, "fan": 0.0}},
        {"t_ms": t(70), "event": "band_start", "band": 3, "settings": {"flow": 100.0, "speed": 60, "nozzle_target": 220.0, "fan": 0.0}},
        {"t_ms": t(80), "event": "band_start", "band": 4, "settings": {"flow": 100.0, "speed": 100.0, "nozzle_target": 200.0, "fan": 0.0}},
        {"t_ms": t(5000), "event": "band_start", "band": 4, "settings": {"flow": 1, "speed": 1, "nozzle_target": 1}},
    ]


def test_band_plans_take_the_last_runner_and_fill_as_printed_bands_with_the_baseline():
    plan = D.band_plans(runner_events(), 1_000_000, 1_100_000)
    assert [plan[b]["kind"] for b in (1, 2, 3, 4)] == ["baseline", "flow", "speed", "nozzle"]
    assert plan[2]["plan_flow"] == 80 and plan[3]["plan_speed"] == 60 and plan[4]["plan_nozzle"] == 200
    assert plan[1]["plan_fan"] == 0.0 and plan[1]["plan"] == "baseline"
    only_first = D.band_plans(runner_events()[:2], 1_000_000, 1_100_000)  # 'as printed' with no baseline read yet
    assert only_first[1]["plan"] == "baseline" and np.isnan(only_first[1]["plan_flow"])


def test_sweep_windows_pair_start_and_end():
    ev = [{"event": "window_start", "label": "flow_70", "t_ms": 10, "flow": 70, "nozzle_target": 230},
          {"event": "window_end", "label": "flow_70", "t_ms": 70},
          {"event": "window_start", "label": "nozzle_210", "t_ms": 80, "flow": 100, "nozzle_target": 210},
          {"event": "window_end", "label": "nozzle_210", "t_ms": 170}]
    w = D.sweep_windows(ev)
    assert [(x["band"], x["plan"], x["t0"], x["t1"]) for x in w] == [(1, "flow_70", 10, 70), (2, "nozzle_210", 80, 170)]
    assert w[1]["plan_nozzle"] == 210


def test_calliper_table_keeps_filled_rows_and_reads_two_wall_gaps(tmp_path):
    (tmp_path / "measurements").mkdir()
    (tmp_path / "measurements" / "measurements.csv").write_text(
        "band,plan,reading_1_mm,reading_2_mm,gap_between_walls\n1,baseline,,,\n2,flow 80,2.9,2.8,no\n3,flow 120,3.2,3.1,yes\n")
    cal = D.read_calliper(tmp_path)
    assert cal.band.tolist() == [2, 3]
    assert cal.cal_mm.tolist() == pytest.approx([2.85, 3.15]) and cal.cal_gap.tolist() == [False, True]
    assert cal.cal_n.tolist() == [2, 2]
    assert D.read_calliper(tmp_path / "nowhere").empty


def test_slicer_settings_read_cura_style_values_and_report_nothing_for_an_empty_folder(tmp_path):
    (tmp_path / "print-file").mkdir()
    assert D.slicer_settings(tmp_path) == {}
    (tmp_path / "print-file" / "part.gcode").write_text(
        ";Layer height: 0.5\n;SETTING_3 ...\nline_width = 1.5\nwall_line_width_0 = 1.4\ninfill_line_width: 1.2\n")
    assert D.slicer_settings(tmp_path) == {"layer_height": 0.5, "line_width": 1.5, "wall_line_width_0": 1.4,
                                           "infill_line_width": 1.2}


# band table and gate on synthetic widths ---------------------------------------------------------------------------
FLOW_EFFECT = {1: 0.0, 2: -0.25, 3: 0.2, 4: 0.0, 5: 0.0, 6: -0.1, 7: 0.1, 8: 0.0, 9: -1.0, 10: 0.4, 11: 0.0, 12: 0.0}


def synthetic_frames(effect=FLOW_EFFECT, groups=("cam1:-90", "cam2:-90", "cam3:45"), seed=0, noise=0.05, per_layer=6,
                     first_layers=D.PRINTS["print2"].band_first_layers):
    rng = np.random.default_rng(seed)
    rows = []
    for band in range(1, len(first_layers)):
        for layer in range(first_layers[band - 1], first_layers[band]):
            drift = rng.normal(0, 0.02)
            for group in groups:
                for i in range(per_layer):
                    rows.append({
                        "print_id": "print2", "capture": "synthetic", "band": band, "layer": layer, "group": group, "pass": ("inner", "outer")[i % 2],
                        "camera": int(group[3]), "width_px": 3.0 + effect[band] + drift + rng.normal(0, noise),
                        "reason": "ok", "settled": layer != first_layers[band - 1], "plan_ok": True, "mm_per_px": 0.6,
                        "contrast": 20.0, "confidence": 0.9, "plan": f"b{band}", "kind": "x", "defect": band in (3, 7, 9),
                        "fan_on": band == 1, "plan_flow": 100.0, "plan_speed": 100.0, "plan_nozzle": 220.0, "plan_fan": 0.0,
                        "layer_h_mm": 0.5, "line_w_mm": 1.5, "z": 0.5 * layer, **dict.fromkeys(
                            ["flow_pct", "speed_pct", "speed_mm_s", "feed_mm_s", "nozzle_c", "nozzle_target_c", "bed_c",
                             "fan_pct", "substrate_c", "layer_time_s"], 1.0)})
    return pd.DataFrame(rows)


@pytest.fixture
def fast_boot(monkeypatch):
    monkeypatch.setattr(D, "N_BOOT", 300)


def test_gate_goes_when_flow_moves_the_width_in_two_groups(fast_boot):
    g = D.gate(synthetic_frames())
    assert g["verdict"] == "GO" and g["n_groups_passing"] == 3
    cell = next(c for c in g["cells"] if c["group"] == "cam3:45" and c["pass"] == "all")
    assert cell["contrast_px"] == pytest.approx(0.65, abs=0.05) and cell["z"] > 3 and cell["passes"]
    c = next(c for c in g["cells"] if c["group"] == "cam3:45" and c["pass"] == "all")
    assert c["vs_baseline"]["60"]["contrast_px"] < -0.8 and c["vs_baseline"]["140"]["contrast_px"] == pytest.approx(0.4, abs=0.1)
    assert sorted(c["baselines"]["medians_px"]) == [4, 8, 11, 12] and c["baselines"]["spread_px"] < 0.1  # band 1 ran with the fan on
    assert c["band_sd_px"] < 0.05 and cell["se_px"] == max(cell["se_layers_px"], cell["se_bands_px"])
    assert g["flow_ladder_px"]["cam3:45"]["flow 60"][0] < g["flow_ladder_px"]["cam3:45"]["flow 140"][0]


def test_gate_stops_when_ir_does_not_follow_flow(fast_boot):
    flat = {**FLOW_EFFECT, 10: 0.0, 2: 0.0}
    g = D.gate(synthetic_frames(flat))
    assert g["verdict"] == "NO-GO" and g["n_groups_passing"] == 0


def test_gate_needs_two_groups_and_enough_layers(fast_boot):
    g = D.gate(synthetic_frames(groups=("cam3:45",)))
    assert g["verdict"] == "NO-GO" and g["groups_passing"] == ["cam3:45"]
    F = synthetic_frames()
    F = F[~((F.band == 10) & (F.layer > 104))]  # layers 102-104 settled: three blocks left in every group
    assert D.gate(F)["verdict"] == "GO"
    F = F[~((F.band == 10) & (F.layer > 103))]
    g = D.gate(F)
    assert g["verdict"] == "NO-GO" and all(c["status"] == "insufficient" for c in g["cells"] if c["pass"] == "all")


def test_gate_without_usable_print_2_frames_is_no_go_and_summarises_cleanly():
    F = synthetic_frames().iloc[0:0]
    g = D.gate(F)
    assert g["verdict"] == "NO-GO" and g["cells"] == []
    assert "NO-GO" in D.summary_text(synthetic_frames().assign(reason="no_width"), {}, g)


def test_gate_ignores_frames_that_are_not_settled_or_not_at_plan(fast_boot):
    F = synthetic_frames()
    F.loc[(F.band == 10) & (F.group == "cam3:45"), "plan_ok"] = False
    g = D.gate(F)
    assert g["groups_passing"] == ["cam1:-90", "cam2:-90"]
    assert D.usable(F).plan_ok.all() and D.usable(F).settled.all()


def test_gate_does_not_take_a_drift_with_height_for_a_flow_effect(fast_boot):
    drift = {b: 0.06 * b for b in range(1, 13)}  # flow does nothing, but every band is wider than the one before it
    F = synthetic_frames(drift)
    g = D.gate(F)
    assert g["verdict"] == "NO-GO" and g["n_groups_passing"] == 0
    cells = [c for c in g["cells"] if c["pass"] == "all"]
    assert len(cells) == 3
    for c in cells:
        assert c["z_layers_only"] > 3 > c["z"]  # within-band scatter alone would have called it a flow effect
        assert c["se_bands_px"] > 5 * c["se_layers_px"] and all(abs(x["z"]) < 3 for x in c["aa"] if x["status"] == "ok")
    assert any(abs(x["z_layers_only"]) >= 3 for c in cells for x in c["aa"])
    text = D.summary_text(F, {}, g)
    assert "A/A check" in text and "b4-b8" in text and "with the layer bootstrap alone" in text


def test_gate_with_steady_baseline_bands_keeps_its_verdict_and_reports_the_a_a_pairs(fast_boot):
    F = synthetic_frames()
    g = D.gate(F)
    for c in (c for c in g["cells"] if c["pass"] == "all"):
        assert len(c["aa"]) == 6 and all(x["status"] == "ok" and abs(x["z"]) < 3 for x in c["aa"])
    assert "A/A check" in D.summary_text(F, {}, g)


def test_gate_cell_without_enough_baseline_bands_is_insufficient_not_a_pass(fast_boot):
    F = synthetic_frames()
    F = F[~F.band.isin([8, 11, 12])]  # one baseline band left: no way to tell how much bands printed alike differ
    g = D.gate(F)
    assert g["verdict"] == "NO-GO"
    for c in (c for c in g["cells"] if c["pass"] == "all"):
        assert c["status"] == "insufficient" and "baseline bands" in c["why"] and c["band_sd_px"] is None


def test_gate_se_comes_from_layers_not_from_frames():
    rng = np.random.default_rng(3)

    def side(band, shift, n_layers=6, per_layer=10):
        rows = [{"band": band, "layer": 10 * band + j, "width_px": 3 + shift + off + rng.normal(0, 0.01)}
                for j, off in enumerate(rng.normal(0, 0.2, n_layers)) for _ in range(per_layer)]
        return pd.DataFrame(rows)

    a, b = side(10, 0.5), side(2, 0.0)
    blocks = D.layer_blocks(a)
    assert len(blocks) == 6 and all(len(x) == 10 for x in blocks)
    c = D.cell_contrast(a, b, 0.0)
    assert c["status"] == "ok" and c["layers_a"] == 6
    assert c["se_layers_px"] > 0.08  # 0.2 * sqrt(2/6) = 0.115 from the layers; frame-level resampling would give ~0.04
    assert c["se_bands_px"] == 0.0 and c["se_px"] == c["se_layers_px"]


def test_bootstrap_se_shrinks_with_more_layers_and_matches_the_layer_scatter():
    rng = np.random.default_rng(1)
    def blocks(n_layers, sd_layer=0.1, shift=0.0):
        return [rng.normal(3 + shift + rng.normal(0, sd_layer), 0.05, 8) for _ in range(n_layers)]
    p10, se10, _ = D.boot_contrast(blocks(10), blocks(10, shift=-0.5))
    p40, se40, _ = D.boot_contrast(blocks(40), blocks(40, shift=-0.5))
    assert p10 == pytest.approx(0.5, abs=0.2) and p40 == pytest.approx(0.5, abs=0.1)
    assert se40 < se10 and se10 == pytest.approx(0.1 * math.sqrt(2 / 10) * 1.25, rel=0.6)  # sd_layer * sqrt(2/n), median ~1.25x


def test_band_table_aggregates_per_group_and_pass_and_joins_calliper():
    F = synthetic_frames()
    cal = pd.DataFrame({"print_id": "print2", "band": [3, 10], "cal_mm_1": [3.2, 3.2], "cal_mm_2": [3.1, 3.2], "cal_mm": [3.15, 3.2],
                        "cal_n": [2, 2], "cal_gap": [True, False]})
    F["walls"] = 2
    plans = {"print2": {"spacing": pd.DataFrame({"layer": range(1, 140), "spacing_mm": 1.5})}}
    B = D.band_table(F, plans, cal)
    assert set(B["pass"]) == {"all", "inner", "outer"} and set(B.group) == {"cam1:-90", "cam2:-90", "cam3:45"}
    row = B[(B.band == 10) & (B.group == "cam3:45") & (B["pass"] == "all")].iloc[0]
    assert row.cal_mm == 3.2 and row.cal_gap == False and row.bead_mm_cal == pytest.approx(3.2 - 1.5)
    assert row.n_layers == 9 and row.width_px_median == pytest.approx(3.4, abs=0.1)  # the band's first layer is not settled
    r3 = B[(B.band == 3) & (B["pass"] == "all")].iloc[0]
    assert r3.defect and r3.cal_gap
    assert B[B.band == 1].fan_on_fraction.eq(1).all() and B[B.band == 2].cal_mm.isna().all()
    b1 = B[(B.band == 1) & (B["pass"] == "all")]  # the fan-on rows are counted but not usable
    assert (b1.n_use == 0).all() and (b1.n_ok_fan_on == b1.n_ok).all() and (B[B.band == 2].n_ok_fan_on == 0).all()
    assert (B[(B["pass"] == "inner")].n_frames.values * 2 == B[(B["pass"] == "all")].n_frames.values).all()


def test_usable_needs_ok_width_settled_at_plan_and_the_fan_off():
    F = synthetic_frames()
    F = F[(F.band == 2) & F.settled].iloc[:8].copy()
    F["fan_on"] = False
    F.loc[F.index[0], "fan_on"] = True
    F.loc[F.index[1], "reason"] = "no_width"
    F.loc[F.index[2], "width_px"] = np.nan
    F.loc[F.index[3], "settled"] = False
    F.loc[F.index[4], "plan_ok"] = False
    assert list(D.usable(F).index) == list(F.index[5:])


def test_band_table_fails_loudly_when_a_band_has_several_calliper_rows():
    F = synthetic_frames()
    cal = pd.DataFrame({"print_id": "print2", "band": [3, 3], "cal_mm_1": [3.2, 3.1], "cal_mm_2": [3.1, 3.0], "cal_mm": [3.15, 3.05],
                        "cal_n": [2, 2], "cal_gap": [True, True]})
    F["walls"] = 2
    with pytest.raises(ValueError, match="several calliper rows"):
        D.band_table(F, {"print2": {"spacing": pd.DataFrame({"layer": range(1, 140), "spacing_mm": 1.5})}}, cal)


def finish_input(**over):
    base = dict(band=2, _first_layer=False, since_window_s=100.0, nozzle_c=220.0, nozzle_target_c=220.0, flow_pct=100.0,
                plan_flow=100.0, plan_nozzle=220.0, speed_pct=100.0, plan_speed=100.0, fan_pct=0.0, z=3.0, layer=7)
    rows = [{**base, **{k: v for k, v in o.items()}} for o in over["rows"]]
    return pd.DataFrame(rows)


def test_finish_print_marks_settled_and_plan_ok_per_frame():
    p = D.PRINTS["print2"]
    F = D.finish_print(finish_input(rows=[
        {},                                           # 0 as planned
        {"flow_pct": 130.0},                          # 1 flow readback off the band's plan
        {"speed_pct": 60.0},                          # 2 speed off
        {"nozzle_target_c": 200.0},                   # 3 nozzle target off (and the nozzle 20 C from it)
        {"plan_speed": np.nan, "speed_pct": 60.0},    # 4 band without a speed plan: not judged on speed
        {"_first_layer": True},                       # 5 first layer of the band
        {"nozzle_c": 224.0},                          # 6 nozzle 4 C off its target
        {"band": 0},                                  # 7 outside the band plan
        {"flow_pct": 100.4},                          # 8 inside the 0.5 tolerance
    ]), "print2", p)
    assert F.plan_ok.tolist() == [True, False, False, False, True, True, True, False, True]
    assert F.settled.tolist() == [True, True, True, False, True, False, False, False, True]
    assert "_first_layer" not in F and F.layer_h_mm.eq(0.5).all()


def test_finish_print_fan_comes_from_the_readback_or_the_height_when_there_is_none():
    F = D.finish_print(finish_input(rows=[{"fan_pct": 40.0, "z": 9.0}, {"fan_pct": 0.0, "z": 3.0},
                                          {"fan_pct": np.nan, "z": 3.0}, {"fan_pct": np.nan, "z": 9.0}]), "print2", D.PRINTS["print2"])
    assert F.fan_on.tolist() == [True, False, True, False]


# the glue on a tiny synthetic recording ----------------------------------------------------------------------------
T0_ISO, T0_MS = "2026-10-20T12:00:00+02:00", D.local_ms("2026-10-20T12:00:00+02:00")


def write_capture(root, widths_dir, final_schema=True):
    """One synthetic print inside a recording that also holds idle time before it; telemetry in the recorder's format."""
    tel, starts = synthetic_print(n_layers=3, t0=T0_MS - 250 + 20_000)
    m = tel.s["mot"]
    (root / "ir").mkdir(parents=True, exist_ok=True)
    lines = []
    for i, t in enumerate(m["t"]):
        msg = {"sources": {"motion": {"valid": True, "source_utc_ns": t * 1e6, "payload": {
            "position_mm": {"x": m["x"][i], "y": m["y"][i], "z": m["z"][i]},
            "velocity_mm_s": {"x": m["vx"][i], "y": m["vy"][i]}, "velocity_magnitude": m["vmag"][i],
            "extrusion_active": bool(m["ext"][i]), "speed_override_percent": 100.0}}}}
        if i % 14 == 0:
            flow = 80.0 if t > starts[1] else 100.0
            temp = {"valid": True, "source_utc_ns": t * 1e6, "current_c": 220.0, "active_target_c": 220.0}
            msg["sources"]["duet_readback"] = {"valid": True, "source_utc_ns": t * 1e6, "payload": {
                "extrusion_valid": True, "extrusion_percent": flow, "extrusion_source_utc_ns": t * 1e6,
                "temperatures": {"nozzle": temp, "bed": temp}, "fans": [{"actualValue": 0.0}]}}
        lines.append({"recv_ms": t + 250, "msg": msg})
    (root / "telemetry.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    frames = []
    for k, t in enumerate(np.arange(T0_MS - 250 + 5_000, m["t"][-1], 1000.0)):
        frames.append({"file": f"ir/f{k}.jpg", "camera_index": k % 3, "source_utc_ns": t * 1e6, "recv_ms": t + 1100,
                       "sequence": k})
    (root / "ir.jsonl").write_text("".join(json.dumps(r) + "\n" for r in frames))
    w = pd.DataFrame({"file": [f["file"] for f in frames], "camera": [f["camera_index"] for f in frames],
                      "heading_deg": -90.0, "width_px": 3.0, "reason": "ok", "mm_per_px": 0.6, "contrast": 20.0,
                      "confidence": 0.9, "group": [f"cam{f['camera_index']}:-90" for f in frames], "extra_diag": 1.5,
                      "t_dep_ms": [f["source_utc_ns"] / 1e6 - 800 for f in frames]})
    if not final_schema:
        w = w.drop(columns=["group", "t_dep_ms"])
    widths_dir.mkdir(exist_ok=True)
    w.to_parquet(widths_dir / "widths_synth.parquet")
    return starts


@pytest.fixture
def synth(tmp_path, monkeypatch):
    root, widths = tmp_path / "captures" / "synth", tmp_path / "widths"
    starts = write_capture(root, widths)
    print_def = D.Print("synth", ("synth",), T0_ISO.replace("12:00:00", "12:00:20"), T0_ISO.replace("12:00:00", "12:03:00"), 2,
                        (1, 2, 4), (2,))
    monkeypatch.setattr(D, "CAPTURES", tmp_path / "captures")
    monkeypatch.setattr(D, "PRINTS", {"synth": print_def})
    return tmp_path, widths, starts


def test_build_frames_splits_the_print_joins_state_at_deposit_time_and_tolerates_extra_columns(synth):
    tmp, widths, starts = synth
    F, info, plans = D.build_frames(["synth"], widths, verbose=False)
    assert set(F.print_id) == {"synth", "outside"}
    assert (F[F.t_ms < T0_MS + 20_000 - 250].print_id == "outside").all()
    assert (F[F.print_id == "outside"].band == 0).all() and not F[F.print_id == "outside"].settled.any()
    p = F[F.print_id == "synth"]
    assert p.dep_joined.all() and ((p.t_ms - p.t_join_ms) == 800).all()
    assert D.WIDTH_COLS[0] in F and "extra_diag" not in F
    assert F.mot_gap_ms.max() <= D.MAX_JOIN_MS and F.readback_age_ms.max() <= D.MAX_READBACK_AGE_MS
    # layers 1-3, band edges (1, 2, 4): band 1 = layer 1, band 2 = layers 2-3; the flow readback drops to 80 in layer 2
    assert p.groupby("layer").band.first().to_dict() == {0: 0, 1: 1, 2: 2, 3: 2}
    assert p[p.layer == 2].flow_pct.min() == 80.0 and p[p.layer == 1].flow_pct.max() == 100.0
    assert (p[p.band == 2].defect).all() and not p[p.band == 1].defect.any()
    assert set(p[p.layer > 0]["pass"]) <= {"inner", "outer"}
    b2 = p[p.band == 2]
    assert not b2[b2.layer == 2].settled.any() and b2[b2.layer == 3].settled.any()
    assert info["synth"]["wall_spacing_mm"]["median"] == pytest.approx(1.5, abs=0.05)
    assert info["synth"]["layer_h_mm"] == 0.5 and info["synth"]["n_layers"] == 3


def test_a_deposit_time_older_than_5_s_or_later_than_the_frame_is_not_used(synth):
    tmp, widths, _ = synth
    w = pd.read_parquet(widths / "widths_synth.parquet")
    t_frame = {f["file"]: f["source_utc_ns"] / 1e6 for f in map(json.loads, open(tmp / "captures" / "synth" / "ir.jsonl"))}
    lag = np.select([w.index % 4 == 0, w.index % 4 == 1, w.index % 4 == 2], [5_000, 5_001, -300], 800)
    w["t_dep_ms"] = w.file.map(t_frame) - lag
    w.loc[w.index % 4 == 3, "t_dep_ms"] = np.nan
    w.to_parquet(widths / "widths_synth.parquet")
    F, _, _ = D.build_frames(["synth"], widths, verbose=False)
    F = F.merge(w[["file"]].assign(lag=lag, i=w.index % 4), on="file")
    assert F[F.i == 0].dep_joined.all() and (F[F.i == 0].t_ms - F[F.i == 0].t_join_ms).eq(5_000).all()  # exactly 5 s still counts
    for i in (1, 2, 3):  # 5.001 s back, after the frame, none at all: the frame's own time
        f = F[F.i == i]
        assert len(f) and not f.dep_joined.any() and (f.t_join_ms == f.t_ms).all()


def test_provenance_columns_are_carried_into_the_frames_and_missing_ones_stay_empty(synth, monkeypatch):
    tmp, widths, _ = synth
    monkeypatch.setattr(D, "check_aim", lambda cap, w: None)
    w = pd.read_parquet(widths / "widths_synth.parquet").assign(geometry_version="0.2", width_config_hash="44f5f80c15d5")
    w.to_parquet(widths / "widths_synth.parquet")
    F, _, _ = D.build_frames(["synth"], widths, verbose=False)
    assert F.geometry_version.eq("0.2").all() and F.width_config_hash.eq("44f5f80c15d5").all()
    w.drop(columns=["geometry_version", "width_config_hash"]).to_parquet(widths / "widths_synth.parquet")
    F, _, _ = D.build_frames(["synth"], widths, verbose=False)
    assert "width_config_hash" not in F or F.width_config_hash.isna().all()


def test_several_extractor_configs_in_one_build_are_called_out(synth, monkeypatch, capsys):
    tmp, widths, _ = synth
    monkeypatch.setattr(D, "check_aim", lambda cap, w: None)
    w = pd.read_parquet(widths / "widths_synth.parquet")
    w["width_config_hash"] = np.where(w.index % 2, "aaaaaaaaaaaa", "bbbbbbbbbbbb")
    w.to_parquet(widths / "widths_synth.parquet")
    D.build_frames(["synth"], widths, verbose=False)
    assert "several width_config_hash values" in capsys.readouterr().err


def test_fit_camera_refuses_widths_made_with_another_extractor_config(tmp_path, monkeypatch):
    from tools.model import fit_camera as fc

    class Reached(Exception):
        pass

    def stop(frames):
        raise Reached

    monkeypatch.setattr(fc, "provenance", lambda geometry_path=None, width_config=None: {"geometry_version": "0.2",
                                                                                         "width_config_hash": "installedhash"})
    monkeypatch.setattr(fc, "usable_frames", stop)

    def run(**cols):
        pd.DataFrame({"geometry_version": ["0.2"] * 3, **{k: [v] * 3 for k, v in cols.items()}}).to_parquet(tmp_path / "frames.parquet")
        fc.main(["--data", str(tmp_path), "--out", str(tmp_path / "out"), "--dry-run"])

    with pytest.raises(SystemExit, match="extractor config.*otherhash.*installedhash"):
        run(width_config_hash="otherhash")
    with pytest.raises(Reached):  # same hash, and files from before the hash existed, go on to the fit
        run(width_config_hash="installedhash")
    with pytest.raises(Reached):
        run()


def test_build_without_final_widths_keeps_state_and_labels(synth):
    tmp, widths, _ = synth
    write_capture(tmp / "captures" / "synth", widths, final_schema=False)
    F, _, _ = D.build_frames(["synth"], widths, verbose=False)
    assert (F.reason == "no_width").all() and F.width_px.isna().all()
    assert F.group.str.startswith("cam").all() and F[F.print_id == "synth"].layer.max() == 3


def test_cli_writes_frames_bands_gate_and_summary(synth, capsys, fast_boot):
    tmp, widths, _ = synth
    D.main(["build", "synth", "--widths", str(widths), "--out", str(tmp / "out")])
    out = tmp / "out"
    assert {p.name for p in out.iterdir()} >= {"frames.parquet", "bands.parquet", "prints.json", "gate.json", "dataset_summary.txt"}
    assert (pd.read_parquet(out / "frames.parquet").print_id == "synth").any()
    assert json.loads((out / "gate.json").read_text())["verdict"] in {"GO", "NO-GO"}
    assert "VERDICT" in capsys.readouterr().out


# the three real recordings -----------------------------------------------------------------------------------------
final_widths = all((D.OUT / f"widths_{c}.parquet").exists() for c in (GOLDEN1, GOLDEN2, AFTERNOON)) and have_data \
    and all({"group", "t_dep_ms"} <= set(pd.read_parquet(D.OUT / f"widths_{c}.parquet").columns) for c in (GOLDEN1, GOLDEN2, AFTERNOON))


@pytest.fixture(scope="module")
def real():
    return D.build_frames([GOLDEN1, GOLDEN2, AFTERNOON], use_widths=bool(final_widths), verbose=False)


def readme_bands(capture):
    text = (CAPTURES / capture / "README.md").read_text()
    return {int(m[1]): (float(m[2]), float(m[3])) for m in re.finditer(r"^\|\s*(\d+)\s*\|[^|]*\|\s*([\d.]+)\s*[–-]\s*([\d.]+)\s*\|", text, re.M)}


@needs_data
def test_print1_layers_match_the_runner_band_table(real):
    _, _, plans = real
    layers = plans["print1"]["layers"]
    table = [json.loads(l) for l in open(CAPTURES / GOLDEN1 / "bands.jsonl") if '"band_table"' in l][-1]["bands"]
    assert len(table) == 8
    for b in table:
        lo, hi = b["layers"]
        first, last = D.band_layers(D.PRINTS["print1"], b["band"])
        assert (first, last) == (lo, hi) and hi - lo + 1 == 15
        assert layers.z[first - 1] == pytest.approx(b["z_first"], abs=0.02) and layers.z[last - 1] == pytest.approx(b["z_last"], abs=0.02)


@needs_data
def test_print2_layers_match_the_readme_and_runner_band_tables(real):
    _, _, plans = real
    layers = plans["print2"]["layers"]
    readme = readme_bands(GOLDEN2)
    assert sorted(readme) == list(range(1, 13))
    for band, (lo, hi) in readme.items():
        first, last = D.band_layers(D.PRINTS["print2"], band)
        assert last - first + 1 == (15 if band == 1 else 10)
        assert layers.z[last - 1] == pytest.approx(hi, abs=0.05)  # README heights are the band's top layer
        assert layers.z[first - 1] == pytest.approx(lo if band == 1 else lo + 0.5, abs=0.05)
    table = [json.loads(l) for l in open(CAPTURES / GOLDEN2 / "bands.jsonl") if '"band_table"' in l][-1]["bands"]
    for b in table:  # the resumed runner (bands 2-12), its own layer count restarted at 1
        first, last = D.band_layers(D.PRINTS["print2"], b["band"])
        assert last - first + 1 == b["layers"][1] - b["layers"][0] + 1
        assert layers.z[first - 1] == pytest.approx(b["z_first"], abs=0.02) and layers.z[last - 1] == pytest.approx(b["z_last"], abs=0.02)
    assert np.allclose(np.diff(layers.z), 0.5, atol=0.05) and layers.z[0] == pytest.approx(0.7, abs=0.05)


@needs_data
def test_print_split_follows_the_print_end_markers_not_the_folder(real):
    F, _, _ = real
    marker = lambda cap, name: json.loads((CAPTURES / cap / name).read_text())
    iso = lambda s: D.local_ms(s)
    off = {c: F[F.capture == c] for c in (GOLDEN1, GOLDEN2, AFTERNOON)}
    aborted = marker(GOLDEN1, "PRINT_END")
    g1 = off[GOLDEN1]
    lap = g1.recv_ms - 1100  # frames reach the recorder ~1 s after acquisition; enough for a window test
    in_aborted = (lap > iso(aborted["print_start"])) & (lap < iso(aborted["print_end_estimate_last_extrusion"]) - 3000)
    assert in_aborted.sum() > 20 and (g1[in_aborted].print_id == "outside").all()
    p1 = marker(GOLDEN1, "PRINT_END_2")
    assert D.PRINTS["print1"].start == p1["print_start"][:-6] + p1["print_start"][-6:]
    assert D.PRINTS["print1"].end == p1["print_end_estimate_last_extrusion"]
    assert D.PRINTS["print2"].end == marker(GOLDEN2, "PRINT_END")["print_end_estimate_last_extrusion"]
    assert D.PRINTS["afternoon"].end == marker(AFTERNOON, "PRINT_END")["print_end_estimate_last_extrusion"]
    assert set(g1.print_id) == {"outside", "print1", "print2"}  # the golden folder also holds the first ~70 s of print 2
    head = g1[g1.print_id == "print2"]
    assert 30 < len(head) < 100 and head.layer.max() <= 1 and (head.band == 0).all()
    assert (F[F.print_id == "print1"].capture == GOLDEN1).all()
    assert set(F[F.print_id == "print2"].capture) == {GOLDEN1, GOLDEN2}
    assert len(F) == sum(len(x) for x in off.values())
    assert F[F.print_id == "print1"].band.max() == 8 and F[F.print_id == "print2"].band.max() == 12


@needs_data
def test_no_frame_is_joined_to_motion_more_than_half_a_second_away(real):
    F, _, _ = real
    assert F.mot_gap_ms.max() <= 500
    assert F.readback_age_ms.max() <= 3000
    assert F.groupby("print_id").x.apply(lambda s: s.isna().mean()).max() < 0.01
    assert (F[F.x.isna()].mot_gap_ms.isna()).all()


@needs_data
def test_speeds_and_context_columns(real):
    F, info, _ = real
    p1, p2 = (F[(F.print_id == p) & (F.band > 0) & (F.speed_pct == 100) & (F.layer > 20)] for p in ("print1", "print2"))
    assert p1.speed_mm_s.median() == pytest.approx(26.5, abs=0.3) and p2.speed_mm_s.median() == pytest.approx(20.0, abs=0.3)
    assert p2.feed_mm_s.median() == pytest.approx(20.0, abs=0.3)
    s60 = F[(F.print_id == "print2") & (F.band == 5) & F.settled]
    assert s60.speed_pct.median() == 60 and s60.speed_mm_s.median() == pytest.approx(12.0, abs=0.3)
    assert (F[F.print_id.isin(["print1", "print2"])].layer_h_mm == 0.5).all()
    assert info["print2"]["wall_spacing_mm"]["median"] == pytest.approx(1.5, abs=0.02)
    assert F.pa_s.isna().all()


@needs_data
def test_defect_and_fan_flags(real):
    F, _, _ = real
    p2 = F[F.print_id == "print2"]
    assert set(p2[p2.defect].band) == {3, 7, 9} and not F[F.print_id != "print2"].defect.any()
    band1 = p2[p2.band == 1]
    assert 0.3 < band1.fan_on.mean() < 0.7  # Z 3.2-6.2 of 3.2-10.2
    assert not p2[p2.band > 1].fan_on.any() and F[F.print_id == "print1"].fan_on.mean() < 0.01  # one 1.4 s readback blip
    assert (p2[p2.fan_pct.notna() & (p2.z < 6.1) & (p2.layer >= 4)].fan_pct > 0).all()
    assert (p2[p2.fan_pct.notna() & (p2.z > 6.3)].fan_pct == 0).all()
    assert "baseline in effect" in p2[p2.band == 11].plan.iloc[0] and p2[p2.band == 11].kind.eq("baseline").all()


@needs_data
def test_pass_labels_alternate_inner_then_outer_within_every_layer_of_print_2(real):
    F, _, plans = real
    p2 = F[(F.print_id == "print2") & (F.layer >= 3)].sort_values("t_join_ms")  # labels are at the deposit time
    assert (p2["pass"] != "").all() and set(p2.walls) == {2.0}
    for layer, g in p2.groupby("layer"):
        seq = g["pass"].tolist()
        assert seq == sorted(seq, key=lambda s: s != "inner"), f"layer {layer}: inner frames must precede outer frames"
    assert 0.45 < (p2["pass"] == "inner").mean() < 0.55
    assert (F[F.print_id != "print2"]["pass"] == "").all()
    bounds = plans["print2"]["bounds"]
    assert len(bounds) == 133 and all(np.isfinite(list(bounds.values())))
    sp = plans["print2"]["spacing"]  # the second loop is the outward one on every leg (layer 1 holds the purge)
    v = sp[sp.layer >= 2].spacing_mm.dropna()
    assert len(v) >= 390 and v.between(1.48, 1.52).all()


@needs_data
def test_labels_per_band_and_plan_settings(real):
    F, _, _ = real
    for pid, n_bands in (("print1", 8), ("print2", 12)):
        f = F[(F.print_id == pid) & (F.band > 0)]
        assert sorted(f.band.unique()) == list(range(1, n_bands + 1))
    p2 = F[(F.print_id == "print2") & (F.band > 0)].groupby("band")
    assert p2.plan_flow.first().to_dict() == {1: 100, 2: 80, 3: 120, 4: 100, 5: 100, 6: 100, 7: 100, 8: 100, 9: 60, 10: 140, 11: 100, 12: 100}
    assert p2.plan_nozzle.first().to_dict()[6] == 200 and p2.plan_nozzle.first().to_dict()[7] == 240
    assert p2.plan_speed.first().to_dict()[5] == 60
    settled = F[F.settled]
    assert (settled.nozzle_c - settled.nozzle_target_c).abs().max() <= D.NOZZLE_TOL_C
    first_layers = {(p, D.band_layers(D.PRINTS[p], b)[0]) for p in ("print1", "print2") for b in range(1, 9)}
    assert not F[F.settled & F.apply(lambda r: (r.print_id, r.layer) in first_layers, axis=1)].shape[0]
    aft = F[(F.print_id == "afternoon") & (F.band > 0)]
    assert aft.groupby("band").plan.first().tolist() == ["flow_100", "flow_70", "flow_130", "flow_85", "flow_115", "flow_100",
                                                          "nozzle_230", "nozzle_210", "nozzle_250", "nozzle_230"]
    assert aft.walls.isna().all() and (aft.since_window_s[aft.settled] >= D.SETTLE_AFTER_WINDOW_S).all()


@needs_data
def test_calliper_joins_land_on_the_right_bands(real):
    F, info, plans = real
    B = D.band_table(F, plans)
    cal = pd.read_csv(CAPTURES / GOLDEN2 / "measurements" / "measurements.csv")
    for r in cal.itertuples():
        rows = B[(B.print_id == "print2") & (B.band == r.band) & (B["pass"] == "all")]
        assert len(rows) and rows.cal_mm.eq((r.reading_1_mm + r.reading_2_mm) / 2).all()
        assert rows.cal_gap.eq(r.gap_between_walls == "yes").all() and rows.defect.eq(r.band in (3, 7, 9)).all()
        text = r.plan  # "flow 80", "nozzle 240", "speed 60", "baseline ..."
        num = re.findall(r"\d+", text)
        mine = rows.iloc[0]
        if text.startswith("flow"):
            assert mine.plan_flow == float(num[0])
        elif text.startswith("nozzle"):
            assert mine.plan_nozzle == float(num[0])
        elif text.startswith("speed"):
            assert mine.plan_speed == float(num[0])
        else:
            assert mine.kind == "baseline"
    assert B[(B.print_id == "print2") & B.wall_spacing_mm.notna()].wall_spacing_mm.between(1.45, 1.55).all()
    b3 = B[(B.print_id == "print2") & (B.band == 3)].iloc[0]
    assert b3.bead_mm_cal == pytest.approx(3.15 - b3.wall_spacing_mm)
    one = pd.read_csv(CAPTURES / GOLDEN1 / "measurements" / "measurements.csv").dropna(subset=["reading_1_mm"])
    for r in one.itertuples():
        row = B[(B.print_id == "print1") & (B.band == r.band)].iloc[0]
        assert row.cal_mm == r.reading_1_mm and row.cal_kind == "single_wall_side_unspecified" and np.isnan(row.wall_spacing_mm)
    assert B[B.print_id == "afternoon"].cal_mm.isna().all()


@pytest.mark.skipif(not final_widths, reason="no final width parquets in data/model")
def test_full_build_with_widths_gate_and_band_aggregates(real):
    F, info, plans = real
    ok = F[(F.reason == "ok") & F.width_px.notna()]
    assert len(ok) > 1000 and F.group.ne("").all()
    assert (F[F.print_id == "print2"].groupby("group").size() > 0).all()
    g = D.gate(F)
    assert g["verdict"] in {"GO", "NO-GO"} and g["cells"]
    B = D.band_table(F, plans)
    assert B[B["pass"] == "all"].n_ok.sum() <= len(ok) and (B.n_use <= B.n_ok).all()
