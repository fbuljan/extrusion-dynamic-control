import importlib
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server import model_files
from server.control_state import ControlState, default_levers

UNTUNED = {"band_below_mm": None, "band_above_mm": None, "levers": default_levers(), "settings": {}}

BROKER_ENV = ["MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_WRITER_USER", "MQTT_WRITER_PASSWORD",
              "MQTT_CONTROLLER_ID"]
APP_ENV = ["CONTROL_SEND_ENABLED"]


def _server_modules():
    return {n: m for n, m in sys.modules.items() if n == "server" or n.startswith("server.")}


@pytest.fixture
def control_json():
    return None


@pytest.fixture
def model_jsons():
    return {}


@pytest.fixture
def api(tmp_path, monkeypatch, control_json, model_jsons):
    """A fresh server.main with DATA_DIR and MODELS_DIR in tmp and no broker; other tests' modules are restored after."""
    data, models = tmp_path / "data", tmp_path / "models"
    models.mkdir()
    for name, body in model_jsons.items():
        (models / name).write_text(json.dumps(body))
    if control_json is not None:
        data.mkdir()
        (data / "control.json").write_text(control_json)
    for name in BROKER_ENV + APP_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("APP_VERSION", "test-1")
    saved = _server_modules()
    for name in saved:
        del sys.modules[name]
    try:
        main = importlib.import_module("server.main")
        with TestClient(main.app) as client:
            yield SimpleNamespace(client=client, main=main, data=data, models=models)
    finally:
        for name in _server_modules():
            del sys.modules[name]
        sys.modules.update(saved)


def test_starts_without_broker(api):
    assert api.main.bridge.status["reader"] == "no broker configured"


def test_control_defaults(api):
    r = api.client.get("/api/control")
    assert r.status_code == 200
    body = r.json()
    server_ms = body.pop("server_ms")
    assert isinstance(server_ms, int) and abs(server_ms - time.time() * 1000) < 5000
    assert body == {
        "target_mm": None, "auto": False, **UNTUNED, "estimate": None, "last_decision": None, "pinned": {}, "limits": {},
        "ceiling_mm": None, "relative": False,
        "models": {name: {"present": False, "version": None, "error": None}
                   for name in ("geometry", "camera_model", "printer_model")},
        "send_enabled": False, "load_error": None, "persist_error": None,
    }


def test_post_persists_across_a_fresh_control_state(api):
    r = api.client.post("/api/control", json={"target_mm": 0.8, "auto": True})
    assert r.status_code == 200
    assert (r.json()["target_mm"], r.json()["auto"]) == (0.8, True)
    assert api.client.get("/api/control").json()["target_mm"] == 0.8
    fresh = ControlState(api.data / "control.json")
    assert (fresh.target_mm, fresh.auto, fresh.load_error) == (0.8, False, None)
    assert json.loads((api.data / "control.json").read_text()) == {"target_mm": 0.8}
    assert not (api.data / "control.json.tmp").exists()


def test_auto_never_survives_a_restart(tmp_path):
    path = tmp_path / "control.json"
    state = ControlState(path)
    assert state.update({"target_mm": 0.7, "auto": True}) == {"target_mm": 0.7, "auto": True, **UNTUNED}
    assert state.auto is True
    fresh = ControlState(path)
    assert (fresh.target_mm, fresh.auto, fresh.load_error) == (0.7, False, None)


def test_a_stored_auto_value_is_ignored(tmp_path):
    path = tmp_path / "control.json"
    for stored in ('{"target_mm": 0.7, "auto": true}', '{"target_mm": 0.7, "auto": "x"}'):
        path.write_text(stored)
        state = ControlState(path)
        assert (state.target_mm, state.auto, state.load_error) == (0.7, False, None)


def test_partial_updates_keep_the_other_field(api):
    api.client.post("/api/control", json={"target_mm": 0.8})
    api.client.post("/api/control", json={"auto": True})
    assert api.client.get("/api/control").json()["target_mm"] == 0.8
    api.client.post("/api/control", json={"target_mm": None})
    body = api.client.get("/api/control").json()
    assert (body["target_mm"], body["auto"]) == (None, True)
    assert api.client.post("/api/control", json={}).status_code == 200


@pytest.mark.parametrize("payload", [
    {"target_mm": 0.29}, {"target_mm": 5.01}, {"target_mm": -1}, {"target_mm": "0.8"}, {"target_mm": True},
    {"target_mm": [0.8]}, {"auto": "yes"}, {"auto": 1}, {"auto": None}, {"width": 1}, {"target_mm": 0.8, "extra": 1},
])
def test_invalid_input_is_400_and_changes_nothing(api, payload):
    api.client.post("/api/control", json={"target_mm": 0.6, "auto": False})
    r = api.client.post("/api/control", json=payload)
    assert r.status_code == 400
    assert isinstance(r.json()["error"], str) and r.json()["error"]
    body = api.client.get("/api/control").json()
    assert (body["target_mm"], body["auto"]) == (0.6, False)
    assert ControlState(api.data / "control.json").public() == {"target_mm": 0.6, "auto": False, **UNTUNED}


def test_the_band_persists_with_the_target_and_tuning_lives_in_memory(api):
    r = api.client.post("/api/control", json={"target_mm": 1.5, "band_below_mm": 0.08, "band_above_mm": 0.0,
                                                "levers": {"flow_percent": {"max_step": 3},
                                                           "nozzle_temperature_c": {"enabled": False}}})
    assert r.status_code == 200
    body = r.json()
    assert (body["band_below_mm"], body["band_above_mm"]) == (0.08, 0.0)
    assert body["levers"]["flow_percent"] == {"enabled": True, "max_step": 3}
    assert body["levers"]["nozzle_temperature_c"] == {"enabled": False, "max_step": None}
    api.client.post("/api/control", json={"levers": {"flow_percent": {"enabled": False}}})
    assert api.client.get("/api/control").json()["levers"]["flow_percent"] == {"enabled": False, "max_step": 3}, \
        "merged per lever"
    assert json.loads((api.data / "control.json").read_text()) == {"target_mm": 1.5, "band_below_mm": 0.08,
                                                                   "band_above_mm": 0.0}
    fresh = ControlState(api.data / "control.json")
    assert fresh.public() == {"target_mm": 1.5, "auto": False, "band_below_mm": 0.08, "band_above_mm": 0.0,
                              "levers": default_levers(), "settings": {}}, "tuning starts from the configured levers"
    api.client.post("/api/control", json={"band_below_mm": None})
    assert json.loads((api.data / "control.json").read_text()) == {"target_mm": 1.5, "band_above_mm": 0.0}


@pytest.mark.parametrize("payload", [
    {"band_below_mm": -0.01}, {"band_above_mm": 1.01}, {"band_below_mm": "0.1"}, {"band_above_mm": True},
    {"band_below_mm": [0.1]},
    {"levers": {"fan_speed_percent": {"enabled": False}}}, {"levers": {"flow_percent": {"max_step": 11}}},
    {"levers": {"nozzle_temperature_c": {"max_step": 6}}}, {"levers": {"flow_percent": {"max_step": 0}}},
    {"levers": {"flow_percent": {"max_step": 2.5}}}, {"levers": {"flow_percent": {"enabled": "no"}}},
    {"levers": {"flow_percent": {"bounds": [0, 200]}}}, {"levers": [1]},
    {"levers": {op: {"enabled": False} for op in ("flow_percent", "speed_override_percent", "nozzle_temperature_c")}},
    {"settings": {"min_width_mm": 0.5}}, {"settings": {"min_width_mm": 2.5}}, {"settings": {"min_width_mm": "1.4"}},
    {"settings": {"persist_estimates": 2.5}}, {"settings": {"pin_s": 30}}, {"settings": {"kappa": 0}},
    {"settings": {"kappa": True}}, {"settings": {"deadband_mm": 0.1}}, {"settings": [1]},
    {"settings": {"min_width_mm": 1.4, "floor_step_factor": 3}},
])
def test_invalid_band_or_tuning_is_400_and_changes_nothing(api, payload):
    api.client.post("/api/control", json={"band_below_mm": 0.1})
    r = api.client.post("/api/control", json=payload)
    assert r.status_code == 400 and r.json()["error"]
    body = api.client.get("/api/control").json()
    assert (body["band_below_mm"], body["band_above_mm"], body["levers"], body["settings"]) == \
        (0.1, None, default_levers(), {})


def test_operator_settings_merge_persist_and_reset(api):
    r = api.client.post("/api/control", json={"settings": {"min_width_mm": 1.45, "persist_estimates": 3}})
    assert r.status_code == 200 and r.json()["settings"] == {"min_width_mm": 1.45, "persist_estimates": 3}
    api.client.post("/api/control", json={"settings": {"kappa": 0.7, "min_width_mm": None}})
    assert api.client.get("/api/control").json()["settings"] == {"persist_estimates": 3, "kappa": 0.7}, \
        "merged per key; None goes back to the deployment's value"
    assert json.loads((api.data / "control.json").read_text())["settings"] == {"persist_estimates": 3, "kappa": 0.7}
    assert ControlState(api.data / "control.json").public()["settings"] == {"persist_estimates": 3, "kappa": 0.7}
    assert isinstance(ControlState(api.data / "control.json").public()["settings"]["persist_estimates"], int)


def test_the_trace_serves_rows_and_the_operators_changes(api):
    api.client.post("/api/control", json={"auto": True, "band_above_mm": 0.1})
    api.client.post("/api/control", json={"levers": {"speed_override_percent": {"max_step": 4}}})
    api.client.post("/api/control", json={"auto": True})  # no change, no event
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not api.client.get("/api/trace").json()["rows"]:
        time.sleep(0.1)
    body = api.client.get("/api/trace").json()
    assert body["running"] is True and body["rows"][-1]["reason"].startswith("no model")
    assert [e["kind"] for e in body["events"]] == ["auto", "band", "levers"]
    assert body["events"][1]["band_above_mm"] == 0.1
    assert api.client.get("/api/trace", params={"since_ms": body["server_ms"] + 60_000}).json()["rows"] == []


def test_invalid_field_blocks_the_valid_one(api):
    r = api.client.post("/api/control", json={"target_mm": 0.8, "auto": "yes"})
    assert r.status_code == 400
    assert api.client.get("/api/control").json()["target_mm"] is None


@pytest.mark.parametrize("raw", ['{"target_mm": NaN}', '{"target_mm": Infinity}'])
def test_non_finite_target_is_400(api, raw):
    r = api.client.post("/api/control", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_range_edges_are_accepted(api):
    for value in (0.3, 5.0):
        assert api.client.post("/api/control", json={"target_mm": value}).status_code == 200


@pytest.mark.parametrize("raw", [b"[0.8]", b"not json", b'"x"', b"null", b"", b"\xff\xfe", b"[" * 100_000, b"0.8"])
def test_bad_bodies_are_400_with_a_json_error(api, raw):
    r = api.client.post("/api/control", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]
    assert api.client.get("/api/control").json()["target_mm"] is None


def test_control_state_defaults_on_damaged_file(tmp_path):
    path = tmp_path / "control.json"
    for text in ("{not json", "[1]", '{"target_mm": 99}', '{"target_mm": "0.8"}', "[" * 200_000, ""):
        path.write_text(text)
        state = ControlState(path)
        assert state.public() == {"target_mm": None, "auto": False, **UNTUNED}
        assert state.load_error


def test_unreadable_control_path_still_starts(tmp_path):
    path = tmp_path / "control.json"
    path.mkdir()
    state = ControlState(path)
    assert state.public() == {"target_mm": None, "auto": False, **UNTUNED} and state.load_error


@pytest.mark.parametrize("control_json", ["{broken"])
def test_load_error_is_exposed_and_a_new_target_repairs_the_file(api):
    assert api.client.get("/api/control").json()["load_error"]
    assert api.client.post("/api/control", json={"target_mm": 0.8}).status_code == 200
    assert json.loads((api.data / "control.json").read_text()) == {"target_mm": 0.8}


def block_persistence(api):
    api.data.mkdir(parents=True, exist_ok=True)
    (api.data / "blocker").write_text("a file where a directory is needed")
    api.main.control.path = api.data / "blocker" / "control.json"


def test_auto_can_always_be_switched_off_when_the_disk_fails(api):
    block_persistence(api)
    r = api.client.post("/api/control", json={"target_mm": 0.8, "auto": True})
    assert r.status_code == 200 and r.json()["auto"] is True and r.json()["target_mm"] == 0.8
    assert r.json()["persist_error"]
    r = api.client.post("/api/control", json={"auto": False})
    assert r.status_code == 200 and r.json()["auto"] is False and r.json()["persist_error"]
    body = api.client.get("/api/control").json()
    assert (body["auto"], body["target_mm"], bool(body["persist_error"])) == (False, 0.8, True)


def test_auto_switch_alone_never_touches_the_disk(api):
    block_persistence(api)
    r = api.client.post("/api/control", json={"auto": True})
    assert r.status_code == 200 and r.json()["auto"] is True and r.json()["persist_error"] is None
    assert api.client.post("/api/control", json={"auto": False}).json()["auto"] is False
    assert not (api.data / "control.json").exists()


def test_persist_error_clears_after_a_successful_write(api):
    block_persistence(api)
    assert api.client.post("/api/control", json={"target_mm": 0.8}).json()["persist_error"]
    api.main.control.path = api.data / "control.json"
    r = api.client.post("/api/control", json={"target_mm": 0.9})
    assert r.json()["persist_error"] is None
    assert json.loads((api.data / "control.json").read_text()) == {"target_mm": 0.9}


def test_only_a_changed_target_is_written(api):
    writes = []
    original = api.main.control._write
    api.main.control._write = lambda: (writes.append(1), original())[1]
    api.client.post("/api/control", json={"auto": True})
    api.client.post("/api/control", json={"target_mm": None})
    assert writes == []
    api.client.post("/api/control", json={"target_mm": 0.8})
    api.client.post("/api/control", json={"target_mm": 0.8, "auto": False})
    assert writes == [1]


def test_an_oserror_while_applying_is_json_not_a_bare_500(api):
    def boom(new, **kw):
        raise OSError(28, "No space left on device")
    api.main.control.update = boom
    r = api.client.post("/api/control", json={"auto": False})
    assert r.status_code == 500 and "No space left" in r.json()["error"]


def test_models_status(api):
    (api.models / "geometry.json").write_text(json.dumps({"version": "g1", "cameras": {}}))
    (api.models / "camera_model.json").write_text("{broken")
    models = api.client.get("/api/control").json()["models"]
    assert models["geometry"] == {"present": True, "version": "g1", "error": None}
    assert models["camera_model"]["present"] is False and models["camera_model"]["error"]
    assert models["printer_model"] == {"present": False, "version": None, "error": None}


def camera_file(knee_mm):
    group = {"k_px_per_mm": 2.2, "offset_px": -0.3, "knee_mm": knee_mm, "sigma_mm": 0.09, "ramp_mm": 0.05,
             "k_rel_sd": 0.1, "valid_px": [2.1, None if knee_mm is None else 3.5]}
    return {"version": "camera-test", "profile": "print2", "ref": {"nozzle_c": 220.0, "speed_mm_s": 20.0},
            "g_T_px_per_c": 0.008, "s_px_per_mm_s": 0.0, "above_knee_sigma_mm": 0.25,
            "profiles": {"print2": {"groups": {"cam3:45": group}}}}


@pytest.mark.parametrize("model_jsons,ceiling,status", [({"camera_model.json": camera_file(1.5)}, 1.5, 400),
                                                        ({"camera_model.json": camera_file(None)}, None, 200)])
def test_a_camera_model_without_a_knee_has_no_ceiling_and_takes_wider_targets(api, ceiling, status):
    body = api.client.get("/api/control").json()
    assert body["models"]["camera_model"]["present"] and body["ceiling_mm"] == ceiling
    assert api.client.post("/api/control", json={"target_mm": 1.7}).status_code == status


def test_model_files_status_cases(tmp_path):
    (tmp_path / "geometry.json").write_text("[1, 2]")
    (tmp_path / "camera_model.json").write_text(json.dumps({"k": 1}))
    (tmp_path / "printer_model.json").write_text(json.dumps({"version": 3}))
    status = model_files.status(tmp_path)
    assert status["geometry"] == {"present": False, "version": None, "error": "not a JSON object"}
    assert status["camera_model"] == {"present": True, "version": None, "error": None}
    assert status["printer_model"] == {"present": True, "version": "3", "error": None}
    assert model_files.status(tmp_path / "missing")["geometry"]["present"] is False


def test_model_files_status_never_raises_on_a_directory(tmp_path):
    (tmp_path / "geometry.json").mkdir()
    (tmp_path / "camera_model.json").write_bytes(b"\xff\xfe\x00")
    status = model_files.status(tmp_path)
    assert status["geometry"]["present"] is False
    assert status["camera_model"]["present"] is False and status["camera_model"]["error"]


@dataclass
class FakeDecision:
    t_ms: int
    mode: str
    action: dict
    error_mm: float


def test_pipeline_fields_are_served_when_present(api):
    pipeline = SimpleNamespace(
        estimate={"mm": 0.82, "sigma_mm": math.nan, "n": 12},
        last_decision=lambda: FakeDecision(1, "advise", {"operation": "flow_percent", "value": 105}, math.inf),
        pinned={"speed_override_percent": 123456},
        limits={"flow_percent": [60, 140]},
    )
    api.main.app.state.pipeline = pipeline
    body = api.client.get("/api/control").json()
    assert body["estimate"] == {"mm": 0.82, "sigma_mm": None, "n": 12}
    assert body["last_decision"] == {"t_ms": 1, "mode": "advise",
                                     "action": {"operation": "flow_percent", "value": 105}, "error_mm": None}
    assert body["pinned"] == {"speed_override_percent": 123456}
    assert body["limits"] == {"flow_percent": [60, 140]}
    assert api.client.post("/api/control", json={"auto": True}).json()["estimate"]["mm"] == 0.82


def estimate(mm=0.82, age_ms=400, n=12, confidence=0.9):
    return SimpleNamespace(estimate={"mm": mm, "sigma_mm": 0.03, "n": n, "age_ms": age_ms, "confidence": confidence})


def test_hold_current_adopts_the_live_estimate(api):
    api.main.app.state.pipeline = estimate(0.8234)
    r = api.client.post("/api/control", json={"hold_current": True})
    assert r.status_code == 200 and r.json()["target_mm"] == 0.823
    assert ControlState(api.data / "control.json").target_mm == 0.823
    assert api.client.post("/api/control", json={"hold_current": True, "auto": True}).json()["auto"] is True


def test_hold_current_accepts_exactly_the_minimum_confidence(api):
    api.main.app.state.pipeline = estimate(confidence=api.main.HOLD_MIN_CONFIDENCE, n=1)
    assert api.client.post("/api/control", json={"hold_current": True}).status_code == 200


def test_low_confidence_hold_is_refused_with_a_plain_message(api):
    api.main.app.state.pipeline = estimate(mm=0.35, confidence=0.02, n=1)
    r = api.client.post("/api/control", json={"hold_current": True})
    assert r.status_code == 400 and "uncertain" in r.json()["error"]
    assert api.client.get("/api/control").json()["target_mm"] is None


def test_hold_current_false_is_a_no_op(api):
    api.client.post("/api/control", json={"target_mm": 0.6})
    r = api.client.post("/api/control", json={"hold_current": False})
    assert r.status_code == 200 and r.json()["target_mm"] == 0.6


@pytest.mark.parametrize("pipeline", [
    None,
    SimpleNamespace(estimate=None),
    estimate(age_ms=60_000),
    estimate(age_ms=None),
    estimate(mm=math.nan),
    estimate(mm=0.2),
    estimate(mm=7.0),
    estimate(confidence=0.49),
    estimate(confidence=None),
    estimate(confidence=math.nan),
    estimate(n=0),
    estimate(n=None),
    SimpleNamespace(estimate=[0.8]),
    SimpleNamespace(estimate="0.8"),
])
def test_hold_current_without_a_usable_estimate_is_400(api, pipeline):
    api.client.post("/api/control", json={"target_mm": 0.6})
    if pipeline is not None:
        api.main.app.state.pipeline = pipeline
    r = api.client.post("/api/control", json={"hold_current": True, "auto": True})
    assert r.status_code == 400 and r.json()["error"]
    body = api.client.get("/api/control").json()
    assert (body["target_mm"], body["auto"]) == (0.6, False)


@pytest.mark.parametrize("payload", [{"hold_current": "yes"}, {"hold_current": 1}, {"hold_current": True, "target_mm": 0.8}])
def test_hold_current_bad_combinations_are_400(api, payload):
    api.main.app.state.pipeline = estimate()
    assert api.client.post("/api/control", json=payload).status_code == 400
    assert api.client.get("/api/control").json()["target_mm"] is None


def test_explicit_target_still_validated_alongside_the_pipeline(api):
    api.main.app.state.pipeline = estimate()
    assert api.client.post("/api/control", json={"target_mm": 9}).status_code == 400


def test_send_enabled_follows_the_env_and_is_read_only(api, monkeypatch):
    assert api.client.get("/api/control").json()["send_enabled"] is False
    for value, expected in (("1", True), ("0", False), ("true", False), ("", False)):
        monkeypatch.setenv("CONTROL_SEND_ENABLED", value)
        assert api.client.get("/api/control").json()["send_enabled"] is expected
    monkeypatch.setenv("CONTROL_SEND_ENABLED", "1")
    assert api.client.post("/api/control", json={"send_enabled": False}).status_code == 400
    assert api.client.get("/api/control").json()["send_enabled"] is True


def test_numpy_values_in_pipeline_fields(api):
    api.main.app.state.pipeline = SimpleNamespace(
        estimate={"mm": np.float32(0.5), "n": np.int64(5), "age_ms": np.int32(10), "confidence": np.float32(0.75),
                  "ok": np.bool_(True), "xs": np.array([1.0, np.nan]), "bad": np.float32("nan"),
                  "per_group": {np.str_("cam2"): np.float64(0.8)}},
        last_decision=FakeDecision(np.int64(1), "advise", {"value": np.float32(105)}, np.float64(0.25)),
        pinned={"flow": np.int64(7)}, limits={"flow": np.array([60, 140])},
    )
    r = api.client.get("/api/control")
    assert r.status_code == 200
    body = r.json()
    assert body["estimate"] == {"mm": 0.5, "n": 5, "age_ms": 10, "confidence": 0.75, "ok": True, "xs": [1.0, None],
                                "bad": None, "per_group": {"cam2": 0.8}}
    assert body["last_decision"]["t_ms"] == 1 and body["last_decision"]["action"] == {"value": 105.0}
    assert body["pinned"] == {"flow": 7} and body["limits"] == {"flow": [60, 140]}
    r = api.client.post("/api/control", json={"auto": True, "hold_current": True})
    assert r.status_code == 200 and r.json()["auto"] is True and r.json()["target_mm"] == 0.5


def test_failing_pipeline_accessors_never_block_control(api, caplog):
    def bad():
        raise RuntimeError("deque mutated during iteration")
    api.main.app.state.pipeline = SimpleNamespace(estimate=bad, last_decision=bad, pinned=bad, limits=bad)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            body = api.client.get("/api/control").json()
            assert (body["estimate"], body["last_decision"], body["pinned"], body["limits"]) == (None, None, {}, {})
        r = api.client.post("/api/control", json={"auto": True})
        assert r.status_code == 200 and r.json()["auto"] is True and api.main.control.auto is True
        assert api.client.post("/api/control", json={"hold_current": True}).status_code == 400
    assert len([rec for rec in caplog.records if "pipeline." in rec.getMessage()]) == 4


def test_send_enabled_helper_is_the_single_predicate(api, monkeypatch):
    from server.control_state import send_enabled
    for value, expected in (("1", True), ("0", False), ("true", False), (" 1", False), ("", False)):
        monkeypatch.setenv("CONTROL_SEND_ENABLED", value)
        assert send_enabled() is expected
        assert api.client.get("/api/control").json()["send_enabled"] is expected
    monkeypatch.delenv("CONTROL_SEND_ENABLED")
    assert send_enabled() is False


def test_existing_endpoints_still_work(api):
    assert api.client.get("/api/version").json() == {"version": "test-1"}
    settings = api.client.get("/api/settings").json()
    assert settings["host"] == "" and settings["reader_password"] is False
    posted = api.client.post("/api/settings", json={"controller_id": "ctl-1", "reader_password": "secret"}).json()
    assert posted["controller_id"] == "ctl-1" and posted["reader_password"] is True
    assert api.client.get("/api/topics").json() == {}
    assert api.client.get("/api/request-template", params={"operation": "nope"}).json()["error"]
    assert api.client.post("/api/request", json={"operation": "flow_percent", "value": 100}).json()["ok"] is False
    assert api.client.get("/").status_code == 200
    with api.client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_only_ir_previews_reach_the_dashboard(api):
    bridge, sent = api.main.bridge, []
    bridge.listeners.append(sent.append)

    def preview(kind, side="trailing"):
        payload = {"camera_kind": kind, "camera_index": 1, "role": side, "jpeg_base64": "AAAA", "sequence": 1}
        bridge._on_message(None, None, SimpleNamespace(
            topic=f"demex/demo/mini/preview/{kind}/{side}", payload=json.dumps(payload).encode(), retain=False))

    preview("rgb")
    assert bridge.snapshot()["frames"] == {} and [m for m in sent if m["type"] == "frame"] == []
    preview("ir")
    assert list(bridge.snapshot()["frames"]) == ["ir"]
    assert [m["camera_kind"] for m in sent if m["type"] == "frame"] == ["ir"]
    assert set(api.client.get("/api/topics").json()) == {"demex/demo/mini/preview/rgb/trailing",
                                                          "demex/demo/mini/preview/ir/trailing"}
