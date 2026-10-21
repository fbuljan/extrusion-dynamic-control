import copy
import inspect
import json
import math

import numpy as np
import pandas as pd
import pytest

from server import model_files
from server.controller import Controller
from server.pipeline import load_models
from server.model_stubs import StubPrinterModel
from server.printer_model import PrinterModel

REAL = model_files.MODELS_DIR / "printer_model.json"
DATA = model_files.MODELS_DIR.parent.parent / "data" / "model"
SYN = {
    "version": "physics-1 test", "ref_c": 220.0, "line_w_ref_mm": 1.5, "default_print": "print2",
    "params": {"q220_mm3_s": 20.0, "alpha_per_c": 0.018, "softness_mm": 0.03, "beta": 0.5, "tau_p_s": 0.8,
               "tau_h_s": 5.0, "dead_h_s": 4.0},
    "prints": {"print1": {"w_nom_mm": 1.6, "feed_mm_s": 26.5, "layer_h_mm": 0.5, "line_w_mm": 1.5},
               "print2": {"w_nom_mm": 1.55, "feed_mm_s": 20.0, "layer_h_mm": 0.5, "line_w_mm": 1.5}},
}
BASE = {"flow": 100.0, "speed": 100.0, "nozzle_c": 220.0, "pa": 0.0}
P1 = {"print": "print1"}


@pytest.fixture
def m():
    return PrinterModel(SYN)


def w(model, context=None, **settings):
    return model.steady_width({**BASE, **settings}, context)


def param_sets():
    sets = [SYN]
    if REAL.is_file():
        sets.append(json.loads(REAL.read_text()))
    return sets


# physics ------------------------------------------------------------------------------------------------------------
def test_width_follows_flow_below_the_melt_limit(m):
    widths = [w(m, flow=f, speed=30) for f in range(50, 151, 10)]  # 6 mm/s: the hotend keeps up with anything
    assert all(b > a for a, b in zip(widths, widths[1:]))
    assert widths[-1] / widths[5] == pytest.approx(1.5 ** 0.5, rel=1e-3)


def test_width_is_flat_above_the_melt_limit(m):
    melt = 20.0 * math.exp(0.018 * -20) / (26.5 * 0.5)  # print 1 at 200 C: ~1.05 mm
    hi = [w(m, P1, flow=f, nozzle_c=200) for f in (120, 140, 160)]
    assert all(x == pytest.approx(melt, abs=0.03) for x in hi)
    assert hi[-1] - hi[0] < 0.01 < w(m, P1, flow=40, nozzle_c=200) - w(m, P1, flow=20, nozzle_c=200)


def test_hotter_nozzle_raises_the_limit(m):
    limited = [w(m, P1, flow=140, nozzle_c=t) for t in (200, 220, 240)]
    assert limited[0] < limited[1] < limited[2]
    assert w(m, flow=80, nozzle_c=240) == pytest.approx(w(m, flow=80), abs=1e-3)  # print 2 at 80 %: not limited


def test_slower_travel_widens_only_when_melt_limited(m):
    assert w(m, P1, flow=120, speed=60) - w(m, P1, flow=120) > 0.1
    assert w(m, flow=80, speed=60) == pytest.approx(w(m, flow=80), abs=1e-3)


def test_context_defaults_aliases_and_line_width(m):
    assert w(m) == w(m, {"print": "print2"}) == w(m, {"print": "nope"}) == w(m, {"feed_mm_s": None, "layer_h_mm": 0})
    assert w(m, P1) != w(m)
    assert w(m, {"layer_height_mm": 0.25}, flow=140) == w(m, {"layer_h_mm": 0.25}, flow=140)
    assert w(m, {"line_w_mm": 0.75}, speed=30) == pytest.approx(w(m, speed=30) / 2)
    assert w(m, {"feed_mm_s": 40.0}) == w(m, speed=200)


# dynamics -----------------------------------------------------------------------------------------------------------
def test_simulate_applies_pressure_and_heater_lags(m):
    dt = 0.5
    out = m.simulate({"flow": 100.0, "nozzle_c": 220.0}, [{**BASE, "flow": 120.0}] * 10, dt)
    for k, x in enumerate(out, 1):
        assert x == pytest.approx(w(m, flow=120 - 20 * math.exp(-k * dt / 0.8)), abs=1e-9)
    ctx = {"context": P1}
    out = m.simulate({"flow": 140.0, "nozzle_c": 220.0, **ctx}, [{**BASE, "flow": 140.0, "nozzle_c": 200.0}] * 80, dt)
    delay = round(4.0 / dt)
    assert out[delay - 1] == pytest.approx(w(m, P1, flow=140))  # dead time: the heater has not reacted yet
    temp = 200 + 20 * math.exp(-(len(out) - delay) * dt / 5.0)
    assert out[-1] == pytest.approx(w(m, P1, flow=140, nozzle_c=temp), abs=1e-9)
    assert out[-1] == pytest.approx(w(m, P1, flow=140, nozzle_c=200), abs=2e-3)


def test_simulate_refuses_a_non_positive_step(m):
    for dt in (0.0, -1.0):
        with pytest.raises(ValueError):
            m.simulate({"flow": 100.0, "nozzle_c": 220.0}, [BASE], dt)


# inversion ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("lever,value,bounds,context", [
    ("flow", 117.0, (70, 130), None), ("flow_percent", 83.0, (70, 130), None),
    ("speed", 73.0, (60, 100), P1), ("speed_override_percent", 66.0, (60, 100), P1),
    ("nozzle_c", 207.0, (190, 250), P1), ("nozzle_temperature_c", 233.0, (190, 250), {**P1, "feed_mm_s": 35.0}),
])
def test_invert_round_trips(m, lever, value, bounds, context):
    key = {"flow_percent": "flow", "speed_override_percent": "speed", "nozzle_temperature_c": "nozzle_c"}.get(lever, lever)
    settings = {**BASE, "flow": 130.0} if key != "flow" else dict(BASE)
    target = m.steady_width({**settings, key: value}, context)
    got = m.invert(lever, target, settings, context, bounds)
    assert got == pytest.approx(value, abs=1e-3)
    assert m.steady_width({**settings, key: got}, context) == pytest.approx(target, abs=1e-6)


def test_invert_returns_none_out_of_reach(m):
    assert m.invert("flow", 5.0, BASE, None, (70, 130)) is None
    assert m.invert("flow", 0.2, BASE, None, (70, 130)) is None
    assert m.invert("speed", w(m) + 0.2, BASE, None, (60, 100)) is None  # print 2 is not melt-limited: speed is dead
    assert m.invert("flow", w(m), BASE, None, (130, 70)) is None
    with pytest.raises(ValueError):
        m.invert("pa", 1.5, BASE, None, (0, 1))


def test_invert_picks_the_root_nearest_the_current_value(m, monkeypatch):
    monkeypatch.setattr(m, "_widths", lambda flow, speed, nozzle_c, context: 1.0 + 1e-4 * (np.asarray(flow) - 100) ** 2)
    target = 1.0 + 1e-4 * 20 ** 2  # reached at flow 80 and 120
    assert m.invert("flow", target, {**BASE, "flow": 110.0}, None, (60, 140)) == pytest.approx(120, abs=1e-6)
    assert m.invert("flow", target, {**BASE, "flow": 85.0}, None, (60, 140)) == pytest.approx(80, abs=1e-6)


# interfaces ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["steady_width", "simulate", "invert"])
def test_signatures_match_the_stub(name):
    ours, stub = inspect.signature(getattr(PrinterModel, name)), inspect.signature(getattr(StubPrinterModel, name))
    assert [(p.name, p.default) for p in ours.parameters.values()] == \
           [(p.name, p.default) for p in stub.parameters.values()]


def test_controller_moves_flow_the_right_way(m):
    c = Controller(m, {"persist_s": 0, "persist_estimates": 1})
    sp = {"flow_percent": 100.0, "speed_override_percent": 100.0, "nozzle_temperature_c": 220.0}
    for mm, sign in ((1.35, 1), (1.75, -1)):
        est = {"mm": mm, "sigma_mm": 0.02, "n": 10, "age_ms": 500, "confidence": 0.9, "per_camera": {}, "since_ms": 0}
        d = c.step(10_000, est, 1.55, dict(sp), {"nozzle_c": 220.0, "extruding": True},
                   {"layer": 10, "feed_mm_s": 20.0, "layer_h_mm": 0.5, "line_w_mm": 1.5}, True, False)
        assert d.action["operation"] == "flow_percent" and (d.action["value"] - 100) * sign > 0, d.reason


def test_real_file_loads_and_is_plausible():
    if not REAL.is_file():
        pytest.skip("no fitted printer_model.json (build it with tools.model.fit_printer)")
    model = PrinterModel.load(REAL)
    assert model_files.status()["printer_model"]["present"] and model.version.startswith("physics-1")
    assert all(math.isfinite(v) for v in model.p.values())
    assert 1.2 < w(model) < 1.9


def test_real_file_reproduces_print2_band_effects():
    """Print 2's calliper bands (two-wall thickness - 1.5 mm pitch) against its baseline mean 1.52 mm."""
    if not REAL.is_file():
        pytest.skip("no fitted printer_model.json (build it with tools.model.fit_printer)")
    model = PrinterModel.load(REAL)
    ctx = {"print": "print2"}
    base = w(model, ctx)
    assert w(model, ctx, flow=80) - base == pytest.approx(-0.17, abs=0.08)
    assert w(model, ctx, flow=140) - base == pytest.approx(0.18, abs=0.12)
    assert abs(w(model, ctx, speed=60) - base) < 0.1
    assert w(model, ctx, nozzle_c=200) - base < -0.05
    pr, p = model.prints["print2"], model.p
    assert p["q220_mm3_s"] / (pr["feed_mm_s"] * pr["layer_h_mm"]) > pr["w_nom_mm"], "print 2 melt-limited at baseline"


@pytest.mark.parametrize("breakage", [
    lambda d: d.update(version="physics-2 x"), lambda d: d.pop("version"),
    lambda d: d["params"].pop("q220_mm3_s"), lambda d: d["params"].update(alpha_per_c=float("nan")),
    lambda d: d["params"].update(softness_mm=0.0), lambda d: d["params"].update(beta=-0.5),
    lambda d: d["params"].update(alpha_per_c=-0.01), lambda d: d["params"].update(tau_p_s=-1.0),
    lambda d: d["params"].update(q220_mm3_s="20"), lambda d: d.update(prints={}),
    lambda d: d["prints"]["print2"].pop("feed_mm_s"), lambda d: d["prints"]["print1"].update(layer_h_mm=0.0),
    lambda d: d["prints"]["print1"].update(w_nom_mm=float("inf")), lambda d: d.update(line_w_ref_mm=0),
])
def test_load_refuses_incomplete_or_broken_files(breakage, tmp_path):
    data = copy.deepcopy(SYN)
    breakage(data)
    with pytest.raises(ValueError):
        PrinterModel(data)
    (tmp_path / "printer_model.json").write_text(json.dumps(data))
    assert load_models(tmp_path)["printer_model"] is None  # the app shows "no model"


def test_lags_are_optional_on_load():
    data = copy.deepcopy(SYN)
    for k in ("tau_p_s", "tau_h_s", "dead_h_s"):
        data["params"].pop(k)
    model = PrinterModel(data)
    assert model.simulate({"flow": 100.0, "nozzle_c": 220.0}, [{**BASE, "flow": 120.0}], 0.5)[0] == w(model, flow=120)


# parity with the PyTorch fit ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("data", param_sets(), ids=lambda d: d["version"])
def test_parity_with_the_torch_fit(data):
    torch = pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    model = PrinterModel(data)
    rng = np.random.default_rng(0)
    n = 1000
    flow, speed, temp = rng.uniform(0, 200, n), rng.uniform(10, 200, n), rng.uniform(185, 260, n)
    feed, layer, line_w = rng.uniform(5, 40, n), rng.uniform(0.1, 0.8, n), rng.uniform(0.4, 2.0, n)
    prints = rng.choice(sorted(data["prints"]), n)
    ours = np.array([model.steady_width({"flow": flow[i], "speed": speed[i], "nozzle_c": temp[i], "pa": 0.0},
                                        {"feed_mm_s": feed[i], "layer_h_mm": layer[i], "line_w_mm": line_w[i],
                                         "print": prints[i]}) for i in range(n)])
    q = data["params"]
    p = {"q220": torch.tensor(q["q220_mm3_s"]), "alpha": torch.tensor(q["alpha_per_c"]),
         "softness": torch.tensor(q["softness_mm"]), "beta": torch.tensor(q["beta"])}
    w_nom = np.array([data["prints"][x]["w_nom_mm"] for x in prints]) * line_w / data["line_w_ref_mm"]
    t = lambda a: torch.tensor(np.asarray(a, float))  # noqa: E731
    theirs = fp.width(p, t(flow / 100), t(feed * speed / 100), t(temp), t(layer), t(w_nom)).numpy()
    assert np.max(np.abs(ours - theirs)) < 1e-4


# fitters ------------------------------------------------------------------------------------------------------------
TRUTH = {"q220": 20.0, "alpha": 0.018, "softness": 0.02, "beta": 0.5}
TRUTH_W_NOM = {"print1": 1.6, "print2": 1.55}


def synthetic_points(torch, fp):
    design = [(100, 100, 220), (80, 100, 220), (120, 100, 220), (100, 60, 220), (100, 100, 200), (100, 100, 240),
              (60, 100, 220), (140, 100, 220)]
    rows = [{"print": pr, "band": i, "plan": "x", "kind": "baseline" if f == 100 and s == 100 and t == 220 else "x",
             "flow": float(f), "speed": float(s), "v": feed * s / 100, "nozzle_c": float(t), "h": 0.5, "line_w": 1.5,
             "upper": False, "feed": feed}
            for pr, feed in (("print1", 26.5), ("print2", 20.0)) for i, (f, s, t) in enumerate(design, 1)]
    P = pd.DataFrame(rows)
    pt = {k: torch.tensor(v) for k, v in TRUTH.items()}
    P["bead"] = fp.width(pt, torch.tensor(P.flow.values / 100), torch.tensor(P.v.values),
                         torch.tensor(P.nozzle_c.values), torch.tensor(P.h.values),
                         torch.tensor(P["print"].map(TRUTH_W_NOM).values)).numpy()
    return P


def raw_params(torch, beta=0.5):
    return {"log_q220": torch.tensor(math.log(20.0)), "log_alpha": torch.tensor(math.log(0.018)),
            "log_soft": torch.tensor(math.log(0.02)), "log_beta": torch.tensor(math.log(beta)),
            "log_w_nom": torch.tensor([math.log(1.6), math.log(1.55)])}


def test_band_points_read_the_calliper_right():
    pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    if not (DATA / "bands.parquet").is_file():
        pytest.skip("no dataset (tools.model.dataset build)")
    P = fp.band_points(DATA).set_index(["print", "band"])
    assert P.loc[("print2", 2), "cal_mm"] == pytest.approx(2.85)
    assert P.loc[("print2", 2), "bead"] == pytest.approx(2.85 - 1.50, abs=0.005)  # thickness - wall pitch
    assert P.loc[("print1", 6), "bead"] == P.loc[("print1", 6), "cal_mm"] == pytest.approx(1.1)  # one wall
    assert set(P[P.upper].index) == {("print2", 3), ("print2", 7), ("print2", 9)}
    assert P.loc[("print1", 5), "v"] == pytest.approx(15.9, abs=0.2) and P.loc[("print2", 5), "v"] == pytest.approx(12.0)


def test_defect_bands_only_bound_from_above():
    torch = pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    P = synthetic_points(torch, fp).iloc[[0, 1]].reset_index(drop=True)  # print 1 baseline and flow 80
    raw = raw_params(torch)
    alone = float(fp.steady_loss(raw, fp._tensors(P.iloc[[0]]), None))

    def with_bound(shift):
        return float(fp.steady_loss(raw, fp._tensors(P.assign(upper=[False, True], bead=P.bead + [0.0, shift])), None))

    assert with_bound(+0.2) == pytest.approx(alone, abs=1e-12)  # prediction below the bound: free
    assert with_bound(-0.2) > alone + 0.01


def test_print2_level_is_left_to_the_offset(monkeypatch):
    torch = pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    P = synthetic_points(torch, fp)
    P.loc[(P["print"] == "print2") & (P.flow == 60), "upper"] = True
    raw = raw_params(torch, beta=0.6)  # off the truth, so residuals are not all zero
    shifted = P.assign(bead=P.bead + np.where(P["print"] == "print2", 0.3, 0.0))
    monkeypatch.setattr(fp, "LEVEL_WEIGHT", 0.0)
    assert float(fp.steady_loss(raw, fp._tensors(shifted), None)) == \
           pytest.approx(float(fp.steady_loss(raw, fp._tensors(P), None)), abs=1e-9)


def test_held_out_band_never_reaches_its_own_fit():
    torch = pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    P = synthetic_points(torch, fp)
    k = int(P.index[(P["print"] == "print2") & (P.flow == 80)][0])
    a, _ = fp.lobo(P, 1.0, folds=[k])
    b, _ = fp.lobo(P.assign(bead=P.bead + np.where(P.index == k, 0.4, 0.0)), 1.0, folds=[k])
    assert a.lobo[0] == b.lobo[0] and a.const[0] == b.const[0] and b.bead[0] == pytest.approx(a.bead[0] + 0.4)


def test_tau_p_comes_from_the_afternoon_unless_it_is_undetermined():
    pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    assert fp.choose_tau_p({"afternoon": {"at_grid_edge": False}, "print2": {"at_grid_edge": False}}) == "afternoon"
    assert fp.choose_tau_p({"afternoon": {"at_grid_edge": True}, "print2": {"at_grid_edge": False}}) == "print2"


def test_steady_fit_recovers_known_physics():
    torch = pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    fit = fp.fit_steady(synthetic_points(torch, fp))
    assert fit["beta"] == pytest.approx(0.5, abs=0.02)
    assert fit["q220"] == pytest.approx(20.0, rel=0.03)
    assert fit["alpha"] == pytest.approx(0.018, abs=0.001)
    assert fit["w_nom"]["print2"] == pytest.approx(1.55, abs=0.02)


def test_heater_fit_recovers_lag_and_dead_time():
    pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    t = np.arange(0.7, 60, 1.4)
    steps = [{"t": t, "start": a, "target": b, "cur": b + (a - b) * np.exp(-np.maximum(t - 4.0, 0) / 5.0)}
             for a, b in ((220, 200), (200, 240), (240, 220))]
    h = fp.fit_heater(steps)
    assert h["tau_h_s"] == pytest.approx(5.0, abs=0.1) and h["dead_h_s"] == pytest.approx(4.0, abs=0.1)


def test_pressure_fit_recovers_the_step_shape():
    pytest.importorskip("torch")
    from tools.model import fit_printer as fp

    rng = np.random.default_rng(1)
    rows = []
    for k, (size, level) in enumerate(((0.6, 2.4), (-0.4, 3.1), (0.9, 2.0))):
        dt = np.sort(rng.uniform(-20, 20, 40))
        px = level + size * (1 - np.exp(-np.maximum(dt, 0) / 0.8)) + rng.normal(0, 0.05, len(dt))
        rows.append(pd.DataFrame({"cell": f"{k}:g", "step": k, "step_t": 1e6 * k, "t_dep_ms": 1e6 * k + dt * 1000,
                                  "width_px": px}))
    r = fp.fit_pressure(pd.concat(rows, ignore_index=True))
    assert r["tau_p_s"] == pytest.approx(0.8, abs=0.3) and r["ci95_s"][0] <= 0.8 <= r["ci95_s"][1]
    assert not r["at_grid_edge"]
