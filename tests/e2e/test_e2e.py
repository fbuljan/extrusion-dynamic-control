"""End-to-end proof on this machine: replay of print 2 -> the real app -> fake gateway, on a private local broker.

The live runs take minutes of real time (the replay runs at 1x), so they are opt-in:

    E2E_RUN=1 .venv/bin/python -m pytest tests/e2e/test_e2e.py -m slow -s     # smoke 3 min; advice, dry, auto ~25 min each
    E2E_OUT=<folder> keeps the logs (default: pytest's temp folder)

They skip without the print-2 recording, the dataset's offline widths, a mosquitto binary or (model scenarios) the fitted models.
The helpers' own tests at the bottom always run.
"""
import json
import os
import sqlite3

import numpy as np
import pandas as pd
import pytest

from tools.sim import e2e_run as e2e

HAVE_DATA = (e2e.CAPTURE / "ir.jsonl").exists() and e2e.FRAMES.exists() and (e2e.MODELS / "geometry.json").exists()
live = pytest.mark.skipif(not HAVE_DATA, reason="needs the print-2 recording, data/model/frames.parquet and geometry.json")
opt_in = pytest.mark.skipif(os.environ.get("E2E_RUN") != "1", reason="live replay runs take minutes: set E2E_RUN=1")
broker = pytest.mark.skipif(e2e.find_mosquitto() is None, reason="no mosquitto binary for the private broker")


@pytest.fixture(scope="module")
def out_root(tmp_path_factory):
    return os.environ.get("E2E_OUT") or str(tmp_path_factory.mktemp("e2e"))


def prove(name, out_root, minutes=None):
    run = e2e.run_scenario(name, minutes, out_root)
    problems, report = e2e.summarize(run, e2e.SCENARIOS[name])
    print("E2E", name, json.dumps({k: v for k, v in report.items() if k not in ("posts", "websocket_messages")},
                                   default=str))
    return problems, report


# the live runs ----------------------------------------------------------------------------------------------------------
@pytest.mark.slow
@opt_in
@live
@broker
def test_smoke_without_a_camera_model_measures_exactly_like_offline_and_never_acts(out_root):
    """Auto on and sends enabled, but no camera model: per-frame widths equal the offline ones, zero requests, only the database."""
    problems, report = prove("smoke", out_root, minutes=3)
    assert problems == []
    assert report["widths_vs_offline"]["matched"] > 150 and report["widths_vs_offline"]["max_abs_dpx"] <= e2e.STRICT_PX


needs_models = pytest.mark.skipif(not (e2e.MODELS / "camera_model.json").exists()
                                  or not (e2e.MODELS / "printer_model.json").exists() or not e2e.models_ready(),
                                  reason="needs app/models/camera_model.json and printer_model.json that load")


@pytest.mark.slow
@opt_in
@live
@broker
@needs_models
@pytest.mark.parametrize("name", ["advice", "dry", "auto"])
def test_print_2_replay_scenario(name, out_root):
    """advice: right sign where the camera can see; dry and auto: nothing / valid requests; auto: hold and alarm."""
    problems, report = prove(name, out_root)
    assert problems == []
    if name == "auto":
        assert len(report["requests"]) == 1


# helpers (always run) ------------------------------------------------------------------------------------------------------
def make_run(tmp_path, window=None, **kw):
    for sub in ("data", "tmp"):
        (tmp_path / sub).mkdir(exist_ok=True)
    window = window or e2e.Window(0, 1_000_000, 100_000, (200_000, 300_000), (400_000, 500_000),
                                  [(120_000, 180_000), (600_000, 700_000)])
    return e2e.Run("test", tmp_path, window, kw.get("send", False), kw.get("auto", False))


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def decision(t, action=None, error=None, reason="on target (|error| 0.01 <= 0.05 mm)", hold=True, mode="advise",
             dry=False):
    return {"wall_ms": t, "type": "decision", "t_ms": t, "mode": mode, "dry": dry, "action": action, "reason": reason,
            "error_mm": error, "predicted_mm": None, "candidates": [], "hold": hold}


def advice(value, op="flow_percent"):
    return {"operation": op, "value": value}


def test_loopback_is_asserted_and_nothing_else_is_accepted():
    e2e.assert_loopback("127.0.0.1")
    for host in ("10.0.0.5", "localhost", "example.org", "::1", ""):
        with pytest.raises(SystemExit):
            e2e.assert_loopback(host)


def test_a_process_handed_another_host_or_port_than_the_private_broker_is_refused():
    e2e.assert_loopback("127.0.0.1", 52000, 52000)
    for host, port in (("10.0.0.5", 52000), ("127.0.0.1", 1884), ("127.0.0.1", None)):
        with pytest.raises(SystemExit):
            e2e.assert_loopback(host, port, 52000)


def test_topics_match_the_apps_and_the_gateways():
    from server import bridge
    assert e2e.REQUEST_TOPIC == bridge.REQUEST_TOPIC
    assert e2e.RESULT_TOPIC in bridge.WRITER_TOPICS
    from tools.mqtt import common
    assert e2e.TELEMETRY_TOPIC == common.TELEMETRY


def test_procs_hands_the_child_exactly_the_environment_it_was_given(monkeypatch, tmp_path):
    seen = {}

    class FakeProc:
        def __init__(self, argv, **kw):
            seen.update(kw)

    monkeypatch.setattr(e2e.subprocess, "Popen", FakeProc)
    monkeypatch.setenv("MQTT_PASSWORD", "from-the-parent")
    env = {"PATH": "/usr/bin:/bin", "ONLY": "this"}
    e2e.Procs().start(["x"], tmp_path, env, tmp_path / "log")
    assert seen["env"] == env and seen["env"] is not None and seen["start_new_session"] is True


@live
def test_run_scenario_gives_the_app_the_built_environment_and_the_private_port(monkeypatch, tmp_path):
    monkeypatch.setenv("MQTT_PASSWORD", "from-the-parent")
    started = []

    class Gone:
        returncode = 1
        pid = 0

        def poll(self):
            return 1

    class Dummy:
        entries = []

        def __init__(self, *a, **k):
            pass

        def start(self):
            return self

        def stop(self):
            pass

    monkeypatch.setattr(e2e.Procs, "start", lambda self, argv, cwd, env, log_path: started.append((argv, env)) or Gone())
    monkeypatch.setattr(e2e, "start_private_broker", lambda procs, out: 54321)
    monkeypatch.setattr(e2e, "FakeGateway", Dummy)
    monkeypatch.setattr(e2e, "Tap", Dummy)
    with pytest.raises(SystemExit, match="exited early"):
        e2e.run_scenario("smoke", 1, tmp_path)
    (argv, env), = started
    assert "uvicorn" in argv and env["MQTT_HOST"] == "127.0.0.1" and env["MQTT_PORT"] == "54321"
    assert set(env) == set(e2e.app_environment(tmp_path, tmp_path, tmp_path, True, 1))
    assert "from-the-parent" not in json.dumps(env) and env["MQTT_PASSWORD"] == ""


def test_the_app_environment_is_built_from_scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("MQTT_HOST", "broker.example")
    monkeypatch.setenv("MQTT_WRITER_PASSWORD", "secret")
    monkeypatch.setenv("CONTROL_SEND_ENABLED", "1")
    env = e2e.app_environment(tmp_path / "d", tmp_path / "t", tmp_path / "m", False, 12345)
    assert env["MQTT_HOST"] == "127.0.0.1" and env["MQTT_PORT"] == "12345" and env["CONTROL_SEND_ENABLED"] == "0"
    assert "secret" not in json.dumps(env) and "example" not in json.dumps(env)
    assert set(env) <= {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "APP_VERSION"} | {
        k for k in env if k.startswith(("MQTT_", "DATA_DIR", "TMPDIR", "MODELS_DIR", "CONTROL_"))}
    assert e2e.app_environment(tmp_path, tmp_path, tmp_path, True, 1)["CONTROL_SEND_ENABLED"] == "1"


def test_match_frames_finds_the_one_shift_and_tolerates_dropped_and_extra_frames():
    rng = np.random.default_rng(0)
    t = np.cumsum(rng.uniform(700, 1300, 300)) + 1.79e12
    cam = rng.integers(0, 4, 300)
    off = pd.DataFrame({"t_ms": t, "camera": cam, "width_px": rng.uniform(2, 4, 300)})
    keep = np.setdiff1d(np.arange(300), [0, 1, 50, 120, 121, 299])
    live_t = t[keep] + 12_345_678 + rng.integers(-20, 21, len(keep))
    live = pd.DataFrame({"t_ms": live_t, "camera": cam[keep], "width_px": off.width_px.to_numpy()[keep]})
    extra = pd.DataFrame({"t_ms": [t[10] + 12_345_678 + 450, t[200] + 12_345_678 + 480], "camera": [cam[10], cam[200]],
                          "width_px": [9.0, 9.0]})
    live = pd.concat([live, extra]).sort_values("t_ms").reset_index(drop=True)
    mine, theirs, shift = e2e.match_frames(live, off)
    assert len(mine) == len(keep) and abs(shift - 12_345_678) <= 20
    assert (mine.camera == theirs.camera).all() and ((mine.t_ms - theirs.t_ms - shift).abs() <= 45).all()
    assert (mine.width_px == theirs.width_px).all(), "the extra live frames are left unmatched"


def test_match_frames_never_pairs_different_cameras():
    off = pd.DataFrame({"t_ms": [1000.0, 2000.0, 3000.0], "camera": [0, 1, 2], "width_px": [1.0, 2.0, 3.0]})
    live = pd.DataFrame({"t_ms": [1000.0 + 5e6, 2000.0 + 5e6, 3000.0 + 5e6], "camera": [3, 3, 3], "width_px": [0.0] * 3})
    mine, _, _ = e2e.match_frames(live, off)
    assert len(mine) == 0


@pytest.mark.parametrize("op, value, sign, current, ok", [
    ("flow_percent", 110, +1, None, True), ("flow_percent", 90, +1, None, False), ("flow_percent", 90, -1, None, True),
    ("flow_percent", 70, +1, {"flow_percent": 60}, True), ("flow_percent", 130, -1, {"flow_percent": 140}, True),
    ("flow_percent", 110, -1, {"flow_percent": 100}, False),
    ("speed_override_percent", 90, +1, None, True), ("speed_override_percent", 90, -1, None, False),
    ("nozzle_temperature_c", 225, +1, None, True), ("nozzle_temperature_c", 225, -1, None, False),
    ("pressure_advance", 1, 1, None, False)])
def test_lever_direction_is_judged_against_the_bands_own_setting(op, value, sign, current, ok):
    assert e2e.lever_direction_ok({"action": advice(value, op)}, sign, current) is ok


def every_second(lo, hi, **kw):
    return [decision(t, **kw) for t in range(lo, hi, 1000)]


def complete_log(flow60=None, flow140=None, baseline0=None, baseline1=None):
    """One decision a second through every judged window of make_run's window, silent unless a part is given."""
    ceiling = dict(reason="at the camera's ceiling (>= 1.50 mm): can't confirm the width")
    return (every_second(120_000, 180_000, **(baseline0 or {})) + every_second(200_000, 300_000, **(flow60 or {}))
            + every_second(400_000, 500_000, **(flow140 or ceiling)) + every_second(600_000, 700_000, **(baseline1 or ceiling)))


WIDEN = dict(action=advice(70), error=0.2, reason="flow 60 % -> 70", hold=False)


def test_advice_check_accepts_right_sign_in_flow_60_and_silence_at_the_plateau(tmp_path):
    run = make_run(tmp_path)
    rows = complete_log(flow60=WIDEN)
    write(run.ws_path, rows)
    problems, report = e2e.check_advice(run, "advise", False)
    assert problems == []
    assert report["flow 60"]["actionable"] == 100 == report["flow 60"]["right_sign"] and report["flow 140"]["actionable"] == 0
    assert "flow 60: 100 of 100 actionable decisions right-sign" in report["sign_evidence"]
    assert [b["deadband_check"] for b in report["baseline"]] == [True, False], \
        "only baseline 0 can show the deadband; baseline 1 is not claimed"


def test_advice_check_flags_wrong_sign_on_target_calls_and_baseline_advice(tmp_path):
    run = make_run(tmp_path)
    write(run.ws_path, complete_log(flow60=dict(WIDEN, action=advice(55)), flow140=dict(action=advice(150), error=-0.1,
                                                                                     reason="x", hold=False),
                                    baseline0=dict(action=advice(110), error=0.1, reason="x", hold=False)))
    problems, _ = e2e.check_advice(run, "advise", False)
    assert any(p.startswith("flow 60") and "wrong way" in p for p in problems)
    assert any(p.startswith("flow 140") and "wrong way" in p for p in problems)
    assert any(p.startswith("baseline 0") and "advised actions" in p for p in problems)
    write(run.ws_path, complete_log(flow60={}))  # silent, and says "on target"
    assert any("call the bead on target" in p for p in e2e.check_advice(run, "advise", False)[0])
    write(run.ws_path, complete_log(flow60=dict(reason="low confidence (0.40)")))
    assert any(p.startswith("flow 60: no actionable decision") for p in e2e.check_advice(run, "advise", False)[0]), \
        "holding the whole band proves nothing about the sign"
    write(run.ws_path, complete_log(flow60=dict(WIDEN, mode="auto")))
    assert any("decision modes" in p for p in e2e.check_advice(run, "advise", False)[0])


def test_a_cut_or_empty_decision_log_fails_instead_of_passing_with_nothing_to_judge(tmp_path):
    run = make_run(tmp_path)
    write(run.ws_path, [])
    problems, _ = e2e.check_advice(run, "advise", False)
    assert sum("decision log is incomplete" in p for p in problems) == 4, "flow 60, flow 140 and both baselines"
    write(run.ws_path, complete_log(flow60=WIDEN)[:150])  # the log stops after 150 s
    assert any("incomplete" in p for p in e2e.check_advice(run, "advise", False)[0])
    write(run.ws_path, complete_log(flow60=WIDEN))
    assert e2e.check_advice(run, "advise", False)[0] == []
    thin = [r for i, r in enumerate(complete_log(flow60=WIDEN)) if i % 2 == 0]  # half a decision a second
    write(run.ws_path, thin)
    assert any("fewer than 0.8/s" in p for p in e2e.check_advice(run, "advise", False)[0])


def test_baseline_0_must_show_the_deadband_while_baseline_1_is_only_reported(tmp_path):
    run = make_run(tmp_path)
    write(run.ws_path, complete_log(flow60=WIDEN, baseline0=dict(reason="no width estimate")))
    assert any("nothing shows the deadband holds" in p for p in e2e.check_advice(run, "advise", False)[0])
    write(run.ws_path, complete_log(flow60=WIDEN, baseline1=dict(reason="no width estimate")))
    assert e2e.check_advice(run, "advise", False)[0] == []


def test_a_target_the_controller_refuses_as_above_the_ceiling_is_a_problem_not_a_pass(tmp_path):
    run = make_run(tmp_path)
    write(run.ws_path, every_second(200_000, 300_000, reason="alarm: target 1.53 mm is above what the camera can see (1.50 mm)"))
    assert any("did not exercise the controller" in p for p in e2e.check_advice(run, "advise", False)[0])


@pytest.mark.parametrize("target, ceiling, ok", [(1.43, 1.50, True), (1.45, 1.50, True), (1.46, 1.50, False),
                                                 (1.50, 1.50, False), (None, 1.50, False), (1.4, None, True)])
def test_hold_current_is_retried_until_it_takes_a_reading_clear_of_the_ceiling(target, ceiling, ok):
    assert e2e.usable_target({"target_mm": target, "ceiling_mm": ceiling}) is ok


def request_row(wall, rid, op="flow_percent", value=110):
    return {"wall_ms": wall, "topic": "request", "payload": {
        "schema_version": "0.2.0", "controller_id": e2e.CONTROLLER_ID, "request_id": rid,
        "correction": {"operation": op, "value": value}}}


ALARM = "alarm: flow 110 % contradicted by the readback; waiting"


def auto_log(after=None):
    after = after if after is not None else [decision(100_000 + 1000 * i, reason=ALARM, mode="auto") for i in range(5, 40)]
    return [decision(99_000, advice(110), 0.2, "flow 100 % -> 110", False, "auto")] + after


def test_auto_check_wants_one_command_then_a_held_alarm(tmp_path):
    run = make_run(tmp_path, send=True, auto=True)
    write(run.ws_path, auto_log())
    write(run.requests_path, [request_row(100_000, "a")])
    run.gateway_entries = [{"state": "accepted", "reason": ""}]
    assert e2e.check_auto(run) == []
    write(run.requests_path, [request_row(100_000, "a"), request_row(103_000, "b")])
    run.gateway_entries = [{"state": "accepted", "reason": ""}] * 2
    assert any("2 requests, expected exactly one" in p for p in e2e.check_auto(run))
    write(run.requests_path, [request_row(100_000, "a"), request_row(101_000, "b")])
    assert any("ms apart" in p for p in e2e.check_auto(run))
    run.gateway_entries = [{"state": "accepted", "reason": ""}]
    write(run.requests_path, [request_row(100_000, "a", "pressure_advance_seconds", 0.2)])
    assert any("not a controller lever" in p for p in e2e.check_auto(run))
    write(run.requests_path, [request_row(100_000, "a", "flow_percent", 140)])
    assert any("outside" in p for p in e2e.check_auto(run))
    write(run.ws_path, auto_log([decision(100_000 + 1000 * i, reason="waiting for flow 110 %", mode="auto") for i in range(5, 40)]))
    write(run.requests_path, [request_row(100_000, "a")])
    assert any("no 'alarm:'" in p for p in e2e.check_auto(run))


def test_every_decision_after_the_first_alarm_must_be_a_held_alarm(tmp_path):
    run = make_run(tmp_path, send=True, auto=True)
    write(run.requests_path, [request_row(100_000, "a")])
    run.gateway_entries = [{"state": "accepted", "reason": ""}]
    alarms = [decision(100_000 + 1000 * i, reason=ALARM, mode="auto") for i in range(5, 40)]
    alarms[20] = decision(120_000, reason="on target (|error| 0.01 <= 0.05 mm)", mode="auto")
    write(run.ws_path, auto_log(alarms))
    assert any("not held alarms" in p for p in e2e.check_auto(run))
    alarms[20] = decision(120_000, advice(120), 0.2, "flow 110 % -> 120", False, "auto")
    write(run.ws_path, auto_log(alarms))
    assert any("not held alarms" in p for p in e2e.check_auto(run))


def test_auto_without_a_command_fails_and_so_does_a_gateway_that_logged_another_count(tmp_path):
    run = make_run(tmp_path, send=True, auto=True)
    write(run.requests_path, [])
    write(run.ws_path, [decision(1000, reason="low confidence (0.4)", mode="auto")])
    assert any("0 requests, expected exactly one" in p for p in e2e.check_auto(run))
    write(run.ws_path, [decision(1000, reason="gate closed (would set flow 100 % -> 110)", mode="auto")])
    assert any("gate stayed closed" in p for p in e2e.check_auto(run))
    write(run.ws_path, auto_log())
    write(run.requests_path, [request_row(100_000, "a")])
    run.gateway_entries = []
    assert any("fake gateway logged 0" in p for p in e2e.check_auto(run))


def test_the_no_model_smoke_check_wants_a_dense_decision_log(tmp_path):
    run = make_run(tmp_path, window=e2e.Window(0, 100_000, 30_000, (0, 0), (0, 0), []))
    run.api = {"estimate": None, "last_decision": {"reason": "no model"}, "models": {}, "send_enabled": True, "auto": True}
    write(run.requests_path, [])
    write(run.ws_path, every_second(0, 100_000, reason="no model", mode="auto"))
    assert e2e.check_no_model(run) == []
    write(run.ws_path, every_second(0, 20_000, reason="no model", mode="auto"))
    assert any("decision log is incomplete" in p for p in e2e.check_no_model(run))


def test_the_websocket_recorder_says_so_when_the_app_closes_the_socket_early(tmp_path):
    import threading
    from websockets.sync.server import serve

    def handler(ws):
        ws.send(json.dumps({"type": "width", "mm": 1.5}))

    with serve(handler, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        rec = e2e.WsRecorder(server.socket.getsockname()[1], tmp_path / "ws.jsonl")
        rec.start()
        rec.join(10)
        server.shutdown()
    assert rec.error and "closed the websocket" in rec.error and rec.seen["width"] == 1


def test_analyze_never_overwrites_the_runs_own_summary(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    (tmp_path / "summary.json").write_text('{"original": true}')
    write(run.requests_path, [])
    for name in ("check_safety", "check_traffic", "staleness", "check_no_requests"):
        monkeypatch.setattr(e2e, name, lambda *a, **k: [])
    monkeypatch.setattr(e2e, "check_pipeline_stats", lambda *a, **k: [])
    monkeypatch.setattr(e2e, "compare_widths", lambda r: {"problems": []})
    monkeypatch.setattr(e2e, "check_advice", lambda *a: ([], {}))
    monkeypatch.setattr(e2e, "estimate_report", lambda r: {})
    e2e.summarize(run, e2e.SCENARIOS["advice"], "analyze_summary.json")
    assert json.loads((tmp_path / "summary.json").read_text()) == {"original": True}
    assert (tmp_path / "analyze_summary.json").exists()


def test_a_nan_width_on_a_pair_both_called_ok_is_a_failure_not_a_skip(tmp_path, monkeypatch):
    def frame(widths, reasons):
        return pd.DataFrame({"t_ms": np.arange(60) * 1000.0 + 1.79e12, "camera": 0, "width_px": widths,
                             "reason": reasons, "group": "g", "capture": e2e.CAPTURE_NAME, "recv_ms": 5})
    base = np.full(60, 3.0)
    for bad, expect_problem in ((np.nan, True), (3.0, False)):
        live_w = base.copy()
        live_w[40] = bad
        run = make_run(tmp_path, window=e2e.Window(0, 10, 0, (0, 0), (0, 0), []))
        monkeypatch.setattr(e2e, "store_rows", lambda path, w=live_w: {"width_samples": frame(w, "ok")})
        monkeypatch.setattr(e2e, "FRAMES", tmp_path / "frames.parquet")
        frame(base, "ok").to_parquet(e2e.FRAMES)
        out = e2e.compare_widths(run)
        assert bool(out["problems"]) is expect_problem, out
        if expect_problem:
            assert out["nan_pairs"] == 1
        else:
            assert out["equal_ok_pairs"] == out["ok_pairs"] and "ok pairs" in out["verdict"]


def test_safety_check_flags_stray_files_images_and_non_scalar_rows(tmp_path):
    run = make_run(tmp_path)
    from server.store import Store
    Store(run.db_path).close()
    run.images_before = e2e.image_files()
    write(run.ws_path, [{"type": "width", "mm": 1.5}])
    assert e2e.check_safety(run) == []
    (run.data / "settings.json").write_text("{}")
    (run.tmp / "frame.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_path / "overlay.png").write_bytes(b"\x89PNG")
    problems = e2e.check_safety(run)
    assert any("settings.json" in p for p in problems) and any("TMPDIR" in p for p in problems)
    assert any("overlay.png" in p for p in problems)
    (run.data / "settings.json").unlink()
    (run.tmp / "frame.jpg").unlink()
    (tmp_path / "overlay.png").unlink()
    db = sqlite3.connect(run.db_path)
    db.execute("INSERT INTO decisions (t_ms, reason) VALUES (1, ?)", ("/9j/" + "A" * 100,))
    db.commit()
    db.close()
    assert any("base64 JPEG" in p for p in e2e.check_safety(run))
    write(run.ws_path, [{"type": "width", "jpeg_base64": "abc"}])
    assert any("image data" in p for p in e2e.check_safety(run))


def test_staleness_says_inconclusive_when_the_extractor_changed_under_the_run(tmp_path):
    run = make_run(tmp_path)
    same = {f: 5 for f in (*e2e.WATCHED, *e2e.INFO)}
    run.code_before, run.code_after = dict(same), dict(same)
    assert e2e.staleness(run) == []
    run.code_after = {**same, "app/server/width.py": 6}
    assert any("width.py changed" in n for n in e2e.staleness(run))
    run.code_before = run.code_after = {**same, "data/model/frames.parquet": 1}
    assert any("older than" in n for n in e2e.staleness(run))


def test_traffic_check_counts_what_the_replay_published(tmp_path):
    run = make_run(tmp_path)
    (tmp_path / "replay.log").write_text("replaying x\npublished 12 messages {'%s': 10, '%s': 2}; skipped: none; 0 failed\n"
                                         % (e2e.TELEMETRY_TOPIC, e2e.IR_TOPIC))
    run.tap_counts = {e2e.TELEMETRY_TOPIC: 10, e2e.IR_TOPIC: 2}
    assert e2e.check_traffic(run) == []
    run.tap_counts = {e2e.TELEMETRY_TOPIC: 10, e2e.IR_TOPIC: 5}
    assert any("delivered 5" in p for p in e2e.check_traffic(run))
