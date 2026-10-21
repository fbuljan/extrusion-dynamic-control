import json
import math
import queue
import socket
import time
import uuid

import numpy as np
import paho.mqtt.client as mqtt
import pytest

from tools.sim import fake_gateway
from tools.sim import plant as plant_module
from tools.sim.plant import (T0_MS, Gateway, GatewayParams, Plant, PlantParams, Sensor, SensorParams, SimHarness,
                             sim_ms)


def settled(params=None, **settings):
    plant = Plant(params, settings)
    for _ in range(600):
        plant.step(0.5)
    return plant.true_width_mm


def noop(*_):
    return None


# plant ---------------------------------------------------------------------------------------------------------------
def test_flow_above_melt_capacity_barely_changes_width():
    assert settled(flow_percent=160) - settled(flow_percent=140) < 0.005
    assert settled(flow_percent=160) <= 1.6 + 1e-9
    assert settled(flow_percent=100) - settled(flow_percent=80) > 0.2


def test_hotter_nozzle_raises_capacity_when_melt_limited():
    widths = [settled(flow_percent=160, nozzle_temperature_c=t) for t in (190, 205, 220, 235, 250)]
    assert all(b > a for a, b in zip(widths, widths[1:]))
    assert widths[-1] - widths[2] > 0.5


def test_slower_travel_raises_width_only_when_melt_limited():
    assert settled(flow_percent=160, speed_override_percent=60) - settled(flow_percent=160) > 0.5
    assert abs(settled(flow_percent=60, speed_override_percent=60) - settled(flow_percent=60)) < 0.005
    assert settled(flow_percent=160, speed_override_percent=150) < settled(flow_percent=160)


def test_pressure_advance_has_no_steady_state_effect():
    assert settled(pressure_advance_seconds=0.0) == settled(pressure_advance_seconds=0.8)


def test_flow_lag_reaches_63_percent_at_tau():
    plant = Plant(initial={"nozzle_temperature_c": 250})  # hot: not melt-limited, so width is linear in flow
    w0 = plant.true_width_mm
    plant.apply("flow_percent", 80)
    for _ in range(300):
        plant.step(0.01)
    assert abs(plant.flow_eff - (100 - 20 * (1 - np.exp(-1)))) < 0.05
    assert abs((w0 - plant.true_width_mm) / (w0 * 0.2) - (1 - np.exp(-1))) < 0.01


def test_heater_lag_and_slew_limit():
    plant = Plant()
    plant.apply("nozzle_temperature_c", 240)
    for _ in range(250):
        plant.step(0.1)
    assert abs((plant.nozzle_c - 220) / 20 - (1 - np.exp(-1))) < 0.01
    plant = Plant(initial={"nozzle_temperature_c": 190})
    plant.apply("nozzle_temperature_c", 250)
    rates = []
    for _ in range(2000):
        before = plant.nozzle_c
        plant.step(0.1)
        rates.append((plant.nozzle_c - before) / 0.1)
    assert max(rates) <= plant.p.heater_slew_c_per_s + 1e-9
    assert rates[0] == pytest.approx(plant.p.heater_slew_c_per_s)
    assert plant.nozzle_c == pytest.approx(250, abs=0.1)


def test_apply_rejects_out_of_range_and_unknown_and_rounds_speed():
    plant = Plant()
    for bad in [("flow_percent", 201), ("nozzle_temperature_c", 189), ("speed_override_percent", 9),
                ("pressure_advance_seconds", 1.5), ("flow_percent", True), ("flow_percent", "100"), ("bed_c", 60)]:
        with pytest.raises(ValueError):
            plant.apply(*bad)
    plant.apply("speed_override_percent", 87.6)
    assert plant.setpoints["speed_override_percent"] == 88


def test_disturbances_and_external_change():
    hot = {"nozzle_temperature_c": 250}
    clogged = Plant(initial=hot)
    clogged.set_disturbance(feed_factor=0.8)
    assert clogged.true_width_mm == pytest.approx(0.8 * Plant(initial=hot).true_width_mm, rel=1e-6)
    weak = Plant(initial={"flow_percent": 160})
    before = weak.true_width_mm
    weak.set_disturbance(melt_factor=0.7)
    assert weak.true_width_mm < 0.75 * before
    with pytest.raises(ValueError):
        weak.set_disturbance(feed_factor=0)
    plant = Plant()
    plant.external_change("speed_override_percent", 70)
    assert plant.readbacks()["setpoints"]["speed_override_percent"] == 70
    assert plant.external_log == [(0.0, "speed_override_percent", 70.0)]


def test_melt_drift_moves_melt_limited_width_only():
    drifting = Plant(PlantParams(melt_drift_per_min=-0.1), {"flow_percent": 160})
    start = drifting.true_width_mm
    for _ in range(600):
        drifting.step(0.1)
    assert drifting.true_width_mm < 0.95 * start
    free = Plant(PlantParams(melt_drift_per_min=-0.1), {"flow_percent": 60, "nozzle_temperature_c": 250})
    start = free.true_width_mm
    for _ in range(600):
        free.step(0.1)
    assert free.true_width_mm == pytest.approx(start, rel=1e-3)


# sensor --------------------------------------------------------------------------------------------------------------
def test_sensor_latency_and_acquisition_time():
    plant, sensor = Plant(), Sensor(SensorParams(dropout=0.0), seed=1)
    sensor.observe(5.0, plant)
    plant.apply("flow_percent", 60)
    for _ in range(100):
        plant.step(0.1)
    assert sensor.ready(5.84) == []
    (sample,) = sensor.ready(5.85)
    assert sample["t_ms"] == T0_MS + 5000 and sample["recv_ms"] == T0_MS + 5850
    assert sensor.acquired[0]["true_mm"] == pytest.approx(Plant().true_width_mm)
    assert sensor.ready(100.0) == []


def test_sensor_statistics_and_camera_relation():
    p = SensorParams(dropout=0.35, noise_px=0.13)
    drop, errors = [], []
    for nozzle, speed in [(220, 100), (240, 100), (200, 60), (230, 130)]:
        plant = Plant(initial={"nozzle_temperature_c": nozzle, "speed_override_percent": speed})
        for _ in range(2000):
            plant.step(0.5)
        sensor = Sensor(p, seed=nozzle)
        for k in range(3000):
            sensor.observe(float(k), plant)
        expected = p.k_px_per_mm * plant.true_width_mm + p.g_px_per_c * (nozzle - 220) \
            + p.s_px_per_pct * (speed - 100) + p.offset_px
        ok = [s for s in sensor.acquired if s["reason"] == "ok"]
        drop.append(1 - len(ok) / len(sensor.acquired))
        errors.append(np.array([s["width_px"] - expected for s in ok]))
        assert all(s["width_px"] is None and s["confidence"] == 0 for s in sensor.acquired if s["reason"] == "dropout")
    assert abs(np.mean(drop) - 0.35) < 0.02
    err = np.concatenate(errors)
    assert abs(err.mean()) < 0.01
    assert abs(err.std() - 0.13) < 0.01


def test_sensor_runs_at_its_rate_and_per_camera_offsets():
    plant = Plant()
    sensor = Sensor(SensorParams(dropout=0.0, noise_px=0.0, cameras=(1, 3), camera_offsets_px={3: 2.6}, rate_hz=2.0))
    for i in range(101):
        sensor.observe(i * 0.1, plant)
    assert len(sensor.acquired) == 2 * 21
    by_camera = {c: {s["width_px"] for s in sensor.acquired if s["camera"] == c} for c in (1, 3)}
    assert min(by_camera[3]) - max(by_camera[1]) == pytest.approx(1.0)


# gateway -------------------------------------------------------------------------------------------------------------
def test_gateway_rate_limit_is_shared_and_rejections_do_not_consume_it():
    gateway = Gateway(seed=3)
    assert gateway.submit(10.0, "flow_percent", 90)["state"] == "accepted"
    late = gateway.submit(10.5, "speed_override_percent", 90)
    assert (late["state"], late["reason"]) == ("rejected", "rate_limited")
    assert gateway.submit(11.0, "flow_percent", 95)["state"] == "accepted"
    assert gateway.submit(11.4, "flow_percent", 250)["reason"] == "bounds"
    assert gateway.submit(12.0, "flow_percent", 100)["state"] == "accepted"
    assert gateway.submit(20.0, "nozzle_temperature_c", 230, sender="operator")["state"] == "accepted"
    assert gateway.submit(20.4, "flow_percent", 100)["reason"] == "rate_limited"
    assert [gateway.submit(30.0 + i, "flow_percent", 100)["sequence"] for i in range(3)] == [5, 6, 7]


def test_gateway_gate_closed_window():
    gateway = Gateway(closed_windows=[(5.0, 8.0)])
    assert gateway.submit(4.9, "flow_percent", 90)["state"] == "accepted"
    closed = gateway.submit(6.0, "flow_percent", 90)
    assert (closed["state"], closed["reason"]) == ("rejected", "command_gate_closed")
    assert gateway.submit(8.0, "flow_percent", 90)["state"] == "accepted"
    gateway.close_during(20.0, 21.0)
    assert not gateway.is_open(20.5) and gateway.is_open(21.0)


def test_gateway_result_states_and_ordering():
    gateway = Gateway(params=GatewayParams(), seed=5)
    due = []
    for i in range(20):
        t = 10.0 * i
        assert gateway.submit(t, "flow_percent", 90 + i)["state"] == "accepted"
        assert gateway.poll(t + 0.49) == []
        (result,) = gateway.poll(t + 2.0)
        assert result["state"] == "setting_confirmed" and result["observation"]["observed_value"] == 90 + i
        due.append(result["t_s"] - t)
    assert 0.5 <= min(due) and max(due) <= 2.0 and max(due) - min(due) > 0.5
    gateway = Gateway(params=GatewayParams(min_interval_s=0.1, confirm_min_s=0.5, confirm_max_s=3.0), seed=1)
    for i in range(40):
        gateway.submit(i * 0.2, "flow_percent", 90 + (i % 5))
    confirmed = gateway.poll(100.0)
    assert [r["sequence"] for r in confirmed] == sorted(r["sequence"] for r in confirmed)
    assert all(a["t_s"] <= b["t_s"] for a, b in zip(confirmed, confirmed[1:]))


# harness -------------------------------------------------------------------------------------------------------------
def make_bench(seed=0, **gateway_kw):
    return SimHarness(Plant(), Sensor(seed=seed), Gateway(seed=seed, **gateway_kw))


def test_ten_minute_noop_run_is_fast_and_shaped():
    bench = make_bench()
    calls = []
    t0 = time.perf_counter()
    series = bench.run(lambda now, samples, rb, gate: calls.append((now, samples)), 600)
    assert time.perf_counter() - t0 < 2.0
    assert len(series["t_s"]) == 6000 and len(calls) == 599
    assert len(series["meas_t_s"]) == 600 and 0.55 < series["meas_ok"].mean() < 0.75
    assert np.allclose(series["true_mm"], series["true_mm"][0]) and not len(series["action_t_s"])
    assert all(call[0] % 1000 == 0 and call[0] >= T0_MS for call in calls)


def test_step_fn_sees_only_delivered_samples_and_readbacks():
    seen = []

    def step(now_ms, samples, readbacks, gate_open):
        seen.append((now_ms, samples, readbacks, gate_open))

    make_bench().run(step, 30)
    delivered = [s for _, samples, _, _ in seen for s in samples]
    assert all(now - s["t_ms"] >= 850 for now, samples, _, _ in seen for s in samples)
    assert len({s["t_ms"] for s in delivered}) == len(delivered) == 29
    rb = seen[0][2]
    assert rb["setpoints"]["flow_percent"] == 100 and rb["measured"]["nozzle_c"] == pytest.approx(220)
    assert all("true_mm" not in s and {"width_px", "mm_per_px", "confidence", "reason", "camera", "recv_ms", "t_dep_ms",
                                       "group", "source_frame_id"} <= set(s) for s in delivered)
    assert len({s["source_frame_id"] for s in delivered}) == len(delivered)
    assert all(s["recv_ms"] == s["t_ms"] + 850 and s["t_dep_ms"] == s["t_ms"] and s["group"] == s["camera"]
               for s in delivered)


def test_action_is_confirmed_after_gateway_delay_then_applied_with_lag():
    bench = make_bench(seed=2)
    sent = []

    def step(now_ms, samples, readbacks, gate_open):
        if not sent:
            sent.append(now_ms)
            return {"operation": "flow_percent", "value": 80}

    series = bench.run(step, 40)
    t_act = series["action_t_s"][0]
    assert list(series["action_state"]) == ["accepted"] and series["action_sequence"][0] == 1
    applied = series["t_s"][series["flow_percent"] == 80][0]
    assert 0.5 - 0.1 <= applied - t_act <= 2.0 + 0.1
    assert series["result_t_s"][0] == pytest.approx(applied, abs=0.11)
    i = np.searchsorted(series["t_s"], applied + 3.0)
    frac = (100 - series["flow_eff"][i]) / 20
    assert 0.55 < frac < 0.7
    assert series["true_mm"][-1] < series["true_mm"][0] - 0.25


def test_rejected_requests_never_reach_the_plant():
    bench = make_bench(closed_windows=[(10.0, 20.0)])
    script = {5: ("flow_percent", 250), 12: ("flow_percent", 70), 25: ("nozzle_temperature_c", 300)}

    def step(now_ms, samples, readbacks, gate_open):
        op = script.get(round((now_ms - T0_MS) / 1000))
        return {"operation": op[0], "value": op[1]} if op else None

    series = bench.run(step, 40)
    assert list(series["action_state"]) == ["rejected"] * 3
    assert list(series["action_reason"]) == ["bounds", "command_gate_closed", "bounds"]
    assert not len(series["result_t_s"])
    assert (series["flow_percent"] == 100).all() and (series["nozzle_target_c"] == 220).all()
    assert not series["gate_open"][(series["t_s"] >= 10.0) & (series["t_s"] < 20.0)].any()


def test_scheduled_disturbance_external_change_and_other_sender():
    bench = make_bench()
    bench.schedule(10.0, lambda plant, gw: plant.set_disturbance(feed_factor=0.8))
    bench.schedule(20.0, lambda plant, gw: plant.external_change("speed_override_percent", 70))
    bench.schedule(30.0, lambda plant, gw: gw.submit(30.0, "nozzle_temperature_c", 240, sender="operator"))
    series = bench.run(noop, 90)
    i = np.searchsorted(series["t_s"], 9.9)
    assert series["true_mm"][i + 1] < 0.85 * series["true_mm"][i - 1]
    assert list(series["external_operation"]) == ["speed_override_percent"] and series["external_t_s"][0] == pytest.approx(20.0, abs=0.01)
    assert series["speed_override_percent"][-1] == 70 and series["nozzle_target_c"][-1] == 240
    assert list(series["result_sender"]) == ["operator"]


def test_determinism_with_a_seed():
    def run(seed):
        bench = make_bench(seed=seed)
        bench.schedule(50.0, lambda plant, gw: plant.set_disturbance(melt_factor=0.9))

        def step(now_ms, samples, readbacks, gate_open):
            return {"operation": "flow_percent", "value": 90 + (now_ms // 1000) % 15}

        return bench.run(step, 120)

    a, b, c = run(7), run(7), run(8)
    assert a.keys() == b.keys()
    assert all(np.array_equal(a[k], b[k]) or (a[k].dtype.kind == "f" and np.allclose(a[k], b[k], equal_nan=True, rtol=0, atol=0))
               for k in a)
    assert not np.allclose(a["meas_px"], c["meas_px"], equal_nan=True)
    assert not np.array_equal(a["result_t_s"], c["result_t_s"])


# fake MQTT gateway ---------------------------------------------------------------------------------------------------
def test_fake_gateway_bounds_match_the_app():
    from server.bridge import OPERATIONS
    assert fake_gateway.BOUNDS == OPERATIONS


def valid_request(**over):
    now = fake_gateway.now_ms()
    request = {"schema_version": "0.2.0", "gateway_session_id": "s", "controller_id": "c", "request_id": str(uuid.uuid4()),
               "sequence": 5, "issued_utc_ms": now, "expires_utc_ms": now + 1500, "extruder_index": 0,
               "correction": {"operation": "flow_percent", "value": 90}}
    request.update(over)
    return request


def verdict(request, highest=4):
    return fake_gateway.validate(request, "c", "s", highest, fake_gateway.now_ms())


def test_validate_flags_each_contract_violation():
    assert verdict(valid_request()) == []
    missing = valid_request()
    del missing["request_id"]
    now = fake_gateway.now_ms()
    cases = [
        ("keys", {**valid_request(), "note": "x"}), ("keys", missing),
        ("schema_version", valid_request(schema_version="0.1.0")),
        ("controller_id", valid_request(controller_id="other")),
        ("session", valid_request(gateway_session_id="old")),
        ("sequence", valid_request(sequence=4)),
        ("expired", valid_request(expires_utc_ms=now - 5)),
        ("extruder_index", valid_request(extruder_index=1)),
        ("correction shape", valid_request(correction={"operation": "flow_percent", "value": 90, "x": 1})),
        ("correction shape", valid_request(correction={"operation": "bed_c", "value": 60})),
        ("bounds", valid_request(correction={"operation": "nozzle_temperature_c", "value": 260})),
        ("bounds", valid_request(correction={"operation": "flow_percent", "value": True})),
        ("bounds", valid_request(correction={"operation": "flow_percent", "value": "90"})),
    ]
    for expected, request in cases:
        assert any(p.startswith(expected) for p in verdict(request)), (expected, request)
    assert verdict([1]) == ["malformed"]


def test_fake_gateway_refuses_non_local_hosts_at_construction_and_start():
    for host in ("broker.example.org", "10.1.2.3"):
        with pytest.raises(ValueError):
            fake_gateway.FakeGateway(host=host)
    for host in ("127.0.0.1", "localhost", "::1"):
        fake_gateway.require_loopback(host)
    gateway = fake_gateway.FakeGateway()
    gateway.host = "10.1.2.3"
    with pytest.raises(ValueError):
        gateway.start()


def broker_up(port=1884):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


@pytest.fixture
def mqtt_bench(tmp_path):
    """Factory: a fake gateway on unique topics of the local broker plus a listening client."""
    if not broker_up():
        pytest.skip("local broker :1884 not reachable")
    made = []

    def make(**kw):
        tag = uuid.uuid4().hex[:8]
        base = f"test/sim-{tag}/extrusion"
        log = tmp_path / f"requests-{tag}.jsonl"
        settings = dict(controller_id="c", session_id="s", base_topic=base, control_topic=f"test/sim-{tag}/control",
                        log_path=str(log), confirm_delay_s=0.2, status_period_s=0.2, min_interval_s=0.0)
        gateway = fake_gateway.FakeGateway(**{**settings, **kw}).start()
        inbox = queue.Queue()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-test-{tag}", protocol=mqtt.MQTTv311)
        client.on_message = lambda c, u, m: inbox.put((m.topic.rsplit("/", 1)[-1], json.loads(m.payload)))
        client.connect("127.0.0.1", 1884)
        client.subscribe([(f"{base}/result", 0), (f"{base}/status", 0)])
        client.loop_start()
        made.append((gateway, client))
        return gateway, client, base, inbox, log

    yield make
    for gateway, client in made:
        client.disconnect()
        client.loop_stop()
        gateway.stop()


@pytest.fixture
def live_gateway(mqtt_bench):
    return mqtt_bench()


def next_message(inbox, kind, timeout=3.0, match=lambda p: True):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            got_kind, payload = inbox.get(timeout=0.1)
        except queue.Empty:
            continue
        if got_kind == kind and match(payload):
            return payload
    raise AssertionError(f"no {kind} message within {timeout}s")


def test_fake_gateway_on_local_broker_accepts_valid_and_rejects_malformed(live_gateway):
    gateway, client, base, inbox, log = live_gateway
    status = next_message(inbox, "status")
    assert status["schema_version"] == "0.1.0" and status["normal_authorized"] is True
    assert 0 < status["expires_utc_ms"] - status["published_utc_ms"] <= 2000
    good = valid_request(gateway_session_id=status["gateway_session_id"], controller_id=status["controller_id"],
                         sequence=status["highest_admitted_sequence"] + 1,
                         expires_utc_ms=min(status["published_utc_ms"] + 1500, status["expires_utc_ms"]))
    client.publish(f"{base}/request", json.dumps(good))
    mine = lambda p: p["request_id"] == good["request_id"]
    assert next_message(inbox, "result", match=mine)["state"] == "accepted"
    done = next_message(inbox, "result", match=mine)
    assert done["state"] == "setting_confirmed" and done["observation"]["observed_value"] == 90
    assert done["sequence"] == good["sequence"] and done["controller_id"] == "c"
    later = next_message(inbox, "status", match=lambda p: p["highest_admitted_sequence"] == good["sequence"])
    assert later["gateway_session_id"] == "s"

    bad = valid_request(gateway_session_id="s", sequence=good["sequence"] + 1,
                        correction={"operation": "flow_percent", "value": 500})
    client.publish(f"{base}/request", json.dumps(bad))
    rejected = next_message(inbox, "result", match=lambda p: p["request_id"] == bad["request_id"])
    assert rejected["state"] == "rejected" and "bounds" in rejected["reason"]
    client.publish(f"{base}/request", "not json")
    assert next_message(inbox, "result", match=lambda p: p["request_id"] is None)["reason"] == "malformed"

    time.sleep(0.2)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["state"], bool(e["reason"])) for e in lines] == [("accepted", False), ("rejected", True), ("rejected", True)]
    assert lines[0]["request"]["request_id"] == good["request_id"] and lines[2]["request"] == "not json"


def test_fake_gateway_gate_control_and_rate_limit(live_gateway):
    gateway, client, base, inbox, log = live_gateway
    gateway.min_interval_s = 5.0
    next_message(inbox, "status")
    first = valid_request(gateway_session_id="s", sequence=1)
    second = valid_request(gateway_session_id="s", sequence=2)
    for request in (first, second):
        client.publish(f"{base}/request", json.dumps(request))
    assert next_message(inbox, "result", match=lambda p: p["request_id"] == second["request_id"])["reason"] == "rate_limited"
    client.publish(gateway.control_topic, json.dumps({"authorized": False}))
    next_message(inbox, "status", match=lambda p: p["normal_authorized"] is False)
    gateway.min_interval_s = 0.0
    third = valid_request(gateway_session_id="s", sequence=3)
    client.publish(f"{base}/request", json.dumps(third))
    assert next_message(inbox, "result", match=lambda p: p["request_id"] == third["request_id"])["reason"] == "gate_closed"


def test_blur_floor_flattens_narrow_beads():
    plant = Plant(PlantParams(nominal_width_mm=0.6, melt_width_mm=5.0))
    floored = Sensor(SensorParams(noise_px=0.0, dropout=0.0, offset_px=0.0, floor_px=1.8), seed=1)
    plain = Sensor(SensorParams(noise_px=0.0, dropout=0.0, offset_px=0.0), seed=1)
    a, b = floored._capture(0.0, plant, 1)["width_px"], plain._capture(0.0, plant, 1)["width_px"]
    assert b == pytest.approx(0.6, abs=1e-6) and a == pytest.approx((1.8 ** 2 + 0.6 ** 2) ** 0.5, abs=1e-6)


def test_readbacks_refresh_slowly_and_go_missing():
    seen = []
    harness = SimHarness(Plant(), Sensor(SensorParams(dropout=0.0)), Gateway(), readback_period_s=1.4,
                         readback_none_prob=0.3, seed=3)
    harness.run(lambda now, s, rb, g: seen.append(rb["measured"]["nozzle_c"]) or None, 60)
    assert any(v is None for v in seen) and any(v is not None for v in seen)


def test_forced_failures_are_never_applied_and_reach_the_inbox():
    states, actions = [], iter([{"operation": "flow_percent", "value": 110}, None, None,
                               {"operation": "flow_percent", "value": 120}] + [None] * 20)
    harness = SimHarness(Plant(), Sensor(), Gateway(forced={1: "dispatch_unknown", 2: "rejected"}))

    def step(now, samples, readbacks, gate):
        states.extend((m["kind"], m["state"]) for m in harness.inbox)
        return next(actions)

    out = harness.run(step, 25)
    assert ("verdict", "accepted") in states and ("result", "dispatch_unknown") in states
    assert ("verdict", "rejected") in states
    assert out["flow_percent"].max() == 100  # neither request ever reached the plant


# -- fix round: realism options, the preset, and the mutants the first review found ------------------------------------
PRINT2_FLOW_MM = {60: 1.15, 80: 1.35, 100: 1.5, 120: 1.65, 140: 1.70}  # calliper, two-wall print


def test_flow_exponent_is_linear_by_default_and_a_square_root_matches_print_2():
    hot = {"nozzle_temperature_c": 250}
    assert settled(flow_percent=80, **hot) == pytest.approx(0.8 * settled(**hot), rel=1e-6)
    sqrt = PlantParams(flow_exponent=0.5)
    assert settled(sqrt, flow_percent=80, **hot) == pytest.approx(math.sqrt(0.8) * settled(sqrt, **hot), rel=1e-6)
    wide_melt = PlantParams(flow_exponent=0.5, melt_width_mm=2.1)  # print 2 ran slower, so the hotend melted more per mm
    assert all(abs(settled(wide_melt, flow_percent=f) - mm) < 0.08 for f, mm in PRINT2_FLOW_MM.items())
    assert PlantParams(**plant_module.REALISTIC_PLANT) == wide_melt


def test_sensor_shows_the_bead_laid_down_earlier_and_stamps_both_clocks():
    plant = Plant(initial={"nozzle_temperature_c": 250})
    sensor = Sensor(SensorParams(dropout=0.0, transport_s=1.0, clock_skew_s=0.3), seed=1)
    widths = {}
    plant.apply("flow_percent", 40)
    for i in range(80):
        widths[i] = plant.true_width_mm
        sensor.observe(i * 0.1, plant)
        plant.step(0.1)
    checked = 0
    for sample, t in zip(sensor.acquired, sensor.acquired_t_s):
        assert sample["t_ms"] == sim_ms(t) - 300 and sample["recv_ms"] == sim_ms(t + 0.85)
        assert sample["t_dep_ms"] == sim_ms(t - 1.0) - 300
        if t >= 1.0:
            assert sample["true_mm"] == pytest.approx(widths[round(t * 10) - 10], abs=1e-9)
            checked += 1
    assert checked == 7 and widths[10] - widths[70] > 0.2


def test_transport_range_is_drawn_per_sample_and_deposit_time_follows():
    sensor = Sensor(SensorParams(dropout=0.0, transport_s=(0.5, 1.5)), seed=2)
    plant = Plant()
    for k in range(400):
        sensor.observe(float(k), plant)
    lags = np.array([s["t_ms"] - s["t_dep_ms"] for s in sensor.acquired]) / 1000
    assert 0.5 <= lags.min() < 0.6 and 1.4 < lags.max() <= 1.5 and abs(lags.mean() - 1.0) < 0.05


def test_group_schedule_lets_one_group_report_at_a_time():
    plant = Plant()
    sensor = Sensor(SensorParams(dropout=0.0, noise_px=0.0, cameras=(9,), group_period_s=10.0,
                                 group_schedule=(("a", 0.0, 1), ("b", 0.2, 3), ("c", -0.2))), seed=1)
    for i in range(300):
        sensor.observe(i * 0.1, plant)
    samples = sensor.acquired
    assert [s["group"] for s in samples] == ["a"] * 10 + ["b"] * 10 + ["c"] * 10
    assert [s["camera"] for s in samples[::10]] == [1, 3, 9]
    px = {s["group"]: s["width_px"] for s in samples}
    assert px["b"] - px["a"] == pytest.approx(0.2) and px["c"] - px["a"] == pytest.approx(-0.2)
    assert len(sensor.ready(100.0)) == 30


def test_dropout_bursts_keep_the_rate_and_lengthen_runs():
    plant = Plant()

    def flags(burst):
        sensor = Sensor(SensorParams(dropout=0.35, dropout_burst_len=burst), seed=4)
        for k in range(20000):
            sensor.observe(float(k), plant)
        return np.array([s["reason"] == "dropout" for s in sensor.acquired])

    for burst, mean_run in ((1.0, 1 / 0.65), (4.0, 4.0)):
        dropped = flags(burst)
        starts = dropped & ~np.r_[False, dropped[:-1]]
        assert abs(dropped.mean() - 0.35) < 0.02
        assert abs(dropped.sum() / starts.sum() - mean_run) < 0.3
    with pytest.raises(ValueError):
        Sensor(SensorParams(dropout=0.8, dropout_burst_len=1.5))


def test_confidence_falls_as_the_bead_nears_the_blur_floor():
    def confidence(width, **kw):
        plant = Plant(PlantParams(nominal_width_mm=width, melt_width_mm=9))
        p = SensorParams(dropout=0.0, noise_px=0.0, floor_px=1.8, **kw)
        return Sensor(p, seed=1)._capture(0.0, plant, 1)["confidence"]

    scaled = [confidence(w, confidence_scale_px=1.0) for w in (0.4, 1.0, 1.5, 3.0)]
    assert scaled == sorted(set(scaled)) and scaled[0] == pytest.approx(0.09) and scaled[-1] == pytest.approx(0.9)
    assert confidence(0.4) == confidence(3.0) == 0.9


def test_gateway_forced_lost_and_dispatch_unknown_applied():
    forced = {1: "lost", 2: "dispatch_unknown_applied", 3: "dispatch_unknown", 4: "queued_unconfirmed"}
    for lost_applied in (False, True):
        gateway = Gateway(params=GatewayParams(lost_applied=lost_applied), forced=forced, seed=1)
        for n in range(4):
            assert gateway.submit(10.0 * n, "flow_percent", 90 + n)["state"] == "accepted"
        results, writes = gateway.poll(1000.0), gateway.poll_writes(1000.0)
        assert [(r["sequence"], r["state"]) for r in results] == [
            (2, "dispatch_unknown"), (3, "dispatch_unknown"), (4, "queued_unconfirmed")]
        assert [w["sequence"] for w in writes] == ([1, 2] if lost_applied else [2])
        assert gateway.poll(2000.0) == [] and gateway.poll_writes(2000.0) == []


def test_gateway_rejects_unknown_forced_states_and_bad_params():
    for forced in ({1: "dispatch_unkown"}, {0: "lost"}, {"1": "lost"}, {1: None}):
        with pytest.raises(ValueError):
            Gateway(forced=forced)
    gateway = Gateway(forced={1: "lost"})
    gateway.forced[2] = "typo"
    gateway.submit(0.0, "flow_percent", 90)
    with pytest.raises(ValueError):
        gateway.submit(5.0, "flow_percent", 90)
    for params in (GatewayParams(apply_at="later"), GatewayParams(fail_prob=1.5),
                   GatewayParams(fail_prob=0.5, fail_mix={"nope": 1}), GatewayParams(fail_prob=0.5, fail_mix={})):
        with pytest.raises(ValueError):
            Gateway(params=params)


def test_gateway_random_faults_follow_fail_prob_and_mix_for_the_controller_only():
    params = GatewayParams(fail_prob=0.4, fail_mix={"lost": 1.0, "dispatch_unknown": 3.0})
    gateway = Gateway(params=params, seed=2)
    for i in range(1000):
        gateway.submit(i * 2.0, "flow_percent", 100)
    states = [r["state"] for r in gateway.poll(1e9)]
    lost, unknown = 1000 - len(states), states.count("dispatch_unknown")
    assert set(states) == {"setting_confirmed", "dispatch_unknown"}
    assert 0.35 < (lost + unknown) / 1000 < 0.45 and 2.0 < unknown / lost < 4.5
    always = Gateway(params=GatewayParams(fail_prob=1.0), seed=1)
    always.submit(0.0, "flow_percent", 100, sender="operator")
    assert [r["state"] for r in always.poll(100.0)] == ["setting_confirmed"]
    always.submit(10.0, "flow_percent", 100)
    assert [r["state"] for r in always.poll(100.0)] != ["setting_confirmed"]
    again = Gateway(params=params, seed=2)
    for i in range(1000):
        again.submit(i * 2.0, "flow_percent", 100)
    assert [r["state"] for r in again.poll(1e9)] == states


def test_gateway_apply_at_submit_writes_precede_results_and_stay_in_order():
    gateway = Gateway(params=GatewayParams(apply_at="submit"), seed=3)
    for i in range(30):
        gateway.submit(10.0 * i, "flow_percent", 90 + i % 10)
    writes, results = gateway.poll_writes(1e9), gateway.poll(1e9)
    for write, result in zip(writes, results):
        submitted = 10.0 * (write["sequence"] - 1)
        assert 0.1 <= write["t_s"] - submitted <= 0.4 and 0.5 <= result["t_s"] - submitted <= 2.0
        assert write["t_s"] < result["t_s"]
    busy = Gateway(params=GatewayParams(apply_at="submit", min_interval_s=0.05), seed=3)
    for i in range(50):
        busy.submit(i * 0.1, "flow_percent", 90 + i % 10)
    writes = busy.poll_writes(1e9)
    assert [w["sequence"] for w in writes] == list(range(1, 51)) and [w["value"] for w in writes] == [
        90 + i % 10 for i in range(50)]


def test_gateway_flush_writes_hands_over_everything_still_on_its_way():
    gateway = Gateway(params=GatewayParams(apply_at="submit"), seed=1)
    for i in range(3):
        gateway.submit(float(i), "flow_percent", 90 + i)
    assert [w["value"] for w in gateway.flush_writes()] == [90, 91, 92]
    assert gateway.flush_writes() == [] and gateway.poll_writes(1e9) == []
    assert len(gateway.poll(1e9)) == 3


def test_gateway_cancel_on_gate_close_drops_unapplied_requests_only():
    def gateway(cancel):
        params = GatewayParams(apply_at="submit", cancel_on_gate_close=cancel, confirm_min_s=1.0, confirm_max_s=1.0)
        return Gateway([(5.0, 20.0)], params, seed=1)

    kept = gateway(False)
    kept.submit(4.9, "flow_percent", 90)
    assert [w["value"] for w in kept.poll_writes(10.0)] == [90]
    assert [r["state"] for r in kept.poll(10.0)] == ["setting_confirmed"]
    dropped = gateway(True)
    dropped.submit(4.9, "flow_percent", 90)
    assert dropped.poll_writes(5.0) == []
    (result,) = dropped.poll(5.0)
    assert (result["state"], result["reason"], result["sequence"]) == ("rejected", "command_gate_closed", 1)
    applied = gateway(True)
    applied.submit(4.2, "flow_percent", 90)
    assert len(applied.poll_writes(4.7)) == 1  # already on the printer: only the result is still due
    assert applied.poll(5.0) == [] and [r["state"] for r in applied.poll(5.3)] == ["setting_confirmed"]


def test_gateway_observation_echoes_the_stored_rounded_value():
    gateway = Gateway(seed=1)
    gateway.submit(0.0, "speed_override_percent", 87.6)
    gateway.submit(2.0, "fan_speed_percent", 12.34)
    gateway.submit(4.0, "flow_percent", 97.5)
    values = [r["observation"]["observed_value"] for r in gateway.poll(100.0)]
    assert values == [88, 12.3, 97.5]


def test_apply_at_submit_reaches_the_plant_before_the_result():
    bench = SimHarness(Plant(), Sensor(), Gateway(params=GatewayParams(apply_at="submit"), seed=2))
    sent = []

    def step(now_ms, samples, readbacks, gate_open):
        if not sent:
            sent.append(now_ms)
            return {"operation": "flow_percent", "value": 80}

    series = bench.run(step, 40)
    t_act = series["action_t_s"][0]
    applied = series["t_s"][series["flow_percent"] == 80][0]
    assert 0.1 - 1e-9 <= applied - t_act < 0.5
    assert series["result_t_s"][0] - t_act >= 0.5 - 1e-9 and series["result_t_s"][0] > applied


def operator_after_our_submit(apply_at):
    gateway = Gateway(params=GatewayParams(apply_at=apply_at), seed=2)
    bench = SimHarness(Plant(), Sensor(), gateway)
    bench.schedule(5.05, lambda plant, gw: plant.external_change("flow_percent", 120))

    def step(now_ms, samples, readbacks, gate_open):
        if round((now_ms - T0_MS) / 1000) == 5:
            return {"operation": "flow_percent", "value": 80}

    return bench.run(step, 20)


def test_an_operator_change_after_our_submit_wins_when_writes_apply_at_submit():
    series = operator_after_our_submit("submit")
    assert list(series["action_state"]) == ["accepted"]
    assert (series["flow_percent"][series["t_s"] >= 5.2] == 120).all() and series["flow_percent"][-1] == 120
    legacy = operator_after_our_submit("confirm")  # the late confirmation overwrites the operator: kept as it was
    assert legacy["flow_percent"][-1] == 80


def test_operator_through_the_gateway_lands_after_our_earlier_write():
    gateway = Gateway(params=GatewayParams(apply_at="submit"), seed=2)
    bench = SimHarness(Plant(), Sensor(), gateway)
    bench.schedule(6.0, lambda plant, gw: gw.submit(6.0, "flow_percent", 120, sender="operator"))
    series = bench.run(lambda now, s, rb, g: {"operation": "flow_percent", "value": 80} if now == T0_MS + 5000 else None, 20)
    assert series["flow_percent"][-1] == 120 and list(series["result_sender"]) == ["controller", "operator"]


def test_controller_then_operator_within_a_second_is_rate_limited():
    bench = make_bench(seed=1)
    verdicts = []
    bench.schedule(5.5, lambda plant, gw: verdicts.append(gw.submit(5.5, "nozzle_temperature_c", 240, sender="operator")))
    series = bench.run(lambda now, s, rb, g: {"operation": "flow_percent", "value": 90} if now == T0_MS + 5000 else None, 20)
    assert (verdicts[0]["state"], verdicts[0]["reason"]) == ("rejected", "rate_limited")
    assert (series["nozzle_target_c"] == 220).all() and series["flow_percent"][-1] == 90


def test_nozzle_readback_lags_its_target():
    seen = []

    def step(now_ms, samples, readbacks, gate_open):
        seen.append(readbacks)
        return {"operation": "nozzle_temperature_c", "value": 250} if len(seen) == 1 else None

    SimHarness(Plant(), Sensor(), Gateway(seed=1)).run(step, 12)
    late = seen[-1]
    assert late["setpoints"]["nozzle_temperature_c"] == 250 and 222 < late["measured"]["nozzle_c"] < 240


def test_readbacks_are_held_for_their_period_and_refresh_without_drift():
    def distinct(period):
        plant = Plant(initial={"nozzle_temperature_c": 190})
        plant.apply("nozzle_temperature_c", 250)
        values = []
        SimHarness(plant, Sensor(), Gateway(), readback_period_s=period).run(
            lambda now, s, rb, g: values.append(rb["measured"]["nozzle_c"]), 61)
        return len(set(values))

    assert distinct(0.0) == 60 and 41 <= distinct(1.4) <= 44
    bench = SimHarness(Plant(), Sensor(), Gateway(), readback_period_s=1.4)
    refreshes, refresh = [], bench._refresh_readbacks
    bench._refresh_readbacks = lambda: (refreshes.append(1), refresh())[1]
    bench.run(noop, 600)
    assert 428 <= len(refreshes) <= 430
    coarse = SimHarness(Plant(), Sensor(), Gateway(), readback_period_s=1.4)  # ticks coarser than the period's grid
    refreshes.clear()
    refresh = coarse._refresh_readbacks
    coarse._refresh_readbacks = lambda: (refreshes.append(1), refresh())[1]
    coarse.run(noop, 600, dt=0.5)
    assert 428 <= len(refreshes) <= 430


def test_readbacks_are_returned_as_copies():
    for kw in ({"readback_period_s": 1.4}, {"readback_none_prob": 0.0001, "readback_period_s": 0.0}):
        seen = []

        def step(now_ms, samples, readbacks, gate_open):
            seen.append((readbacks["setpoints"]["flow_percent"], readbacks["measured"]["nozzle_c"]))
            readbacks["setpoints"]["flow_percent"], readbacks["measured"]["nozzle_c"] = 999, -1

        SimHarness(Plant(), Sensor(), Gateway(), seed=1, **kw).run(step, 10)
        assert seen == [(100.0, pytest.approx(220))] * 9


def test_gate_open_in_step_fn_is_false_inside_a_closed_window():
    gates = []
    make_bench(closed_windows=[(10.0, 20.0)]).run(
        lambda now, s, rb, g: gates.append((round((now - T0_MS) / 1000), g)), 30)
    assert [g for t, g in gates if 10 <= t < 20] == [False] * 10
    assert all(g for t, g in gates if t < 10 or t >= 20) and len(gates) == 29


def test_chunked_runs_keep_the_one_second_control_cadence():
    def script(calls):
        def step(now_ms, samples, readbacks, gate_open):
            calls.append(now_ms)
            return {"operation": "flow_percent", "value": 80 + len(calls) % 20}
        return step

    calls, whole_calls = [], []
    chunked = make_bench(seed=1)
    first, second = chunked.run(script(calls), 30), chunked.run(script(calls), 30)
    whole = make_bench(seed=1).run(script(whole_calls), 60)
    assert np.diff(calls).tolist() == [1000] * 58 and calls == whole_calls
    for key in ("true_mm", "flow_percent", "meas_px", "action_t_s", "result_t_s"):
        joined = np.concatenate([first[key], second[key]])
        assert np.allclose(joined, whole[key], equal_nan=True, rtol=0, atol=1e-12), key


def test_dt_must_divide_one_second_into_whole_ticks():
    for dt in (0.3, 0.4, 0.7, 1.5):
        with pytest.raises(ValueError):
            make_bench().run(noop, 5, dt=dt)
    for dt in (0.05, 0.25, 0.5, 1.0):
        calls = []
        make_bench().run(lambda now, *a: calls.append(now), 5, dt=dt)
        assert len(calls) == 4


def test_submit_jitter_delays_the_submit_not_the_decision():
    bench = SimHarness(Plant(), Sensor(), Gateway(seed=1), submit_jitter_s=0.6, seed=2)
    calls = []

    def step(now_ms, samples, readbacks, gate_open):
        calls.append((now_ms - T0_MS) / 1000)
        return {"operation": "flow_percent", "value": 90 + len(calls) % 5}

    series = bench.run(step, 30)
    lag = series["action_t_s"] - np.array(calls)[:len(series["action_t_s"])]
    assert len(lag) >= len(calls) - 1 and lag.min() >= -1e-9 and lag.max() < 0.71 and lag.max() > 0.25
    with pytest.raises(ValueError):
        SimHarness(Plant(), Sensor(), Gateway(), submit_jitter_s=1.0)


def test_realistic_preset_turns_every_option_on():
    bench = SimHarness.realistic(seed=3)
    assert bench.plant.p == PlantParams(flow_exponent=0.5, melt_width_mm=2.1)
    sp = bench.sensor.p
    assert (sp.g_px_per_c, sp.floor_px, sp.confidence_scale_px, sp.dropout_burst_len) == (0.02, 1.8, 0.5, 3.0)
    assert (sp.transport_s, sp.clock_skew_s, sp.group_period_s) == ((0.5, 1.5), 0.3, 10.0)
    assert [(g[0], g[1]) for g in sp.group_schedule] == [("cam1:-90", 0.0), ("cam3:90", 0.15), ("cam2:180", -0.15)]
    gp = bench.gateway.p
    assert (gp.apply_at, gp.fail_prob, gp.lost_applied, gp.cancel_on_gate_close) == ("submit", 0.05, True, False)
    assert (bench.readback_period_s, bench.readback_none_prob, bench.submit_jitter_s) == (1.4, 0.05, 0.2)
    assert bench.plant.before_external is not None


def test_realistic_preset_overrides_are_routed_and_checked():
    bench = SimHarness.realistic(seed=1, g_px_per_c=0.008, fail_prob=0.0, flow_exponent=1.0, submit_jitter_s=0.0,
                                 closed_windows=[(5.0, 6.0)], forced={1: "lost"}, initial={"flow_percent": 110})
    assert bench.sensor.p.g_px_per_c == 0.008 and bench.gateway.p.fail_prob == 0.0 and bench.plant.p.flow_exponent == 1.0
    assert bench.submit_jitter_s == 0.0 and not bench.gateway.is_open(5.5) and bench.gateway.forced == {1: "lost"}
    assert bench.plant.setpoints["flow_percent"] == 110
    with pytest.raises(TypeError):
        SimHarness.realistic(bogus=1)
    given = SimHarness.realistic(plant_params=PlantParams(), sensor_params=SensorParams(), gateway=Gateway())
    assert given.plant.p.flow_exponent == 1.0 and given.sensor.p.floor_px == 0.0 and given.gateway.p.apply_at == "confirm"
    with pytest.raises(ValueError):
        SimHarness.realistic(gateway=Gateway(), fail_prob=0.1)


def test_realistic_preset_seeds_each_component_separately():
    bench = SimHarness.realistic(seed=5)
    draws = [bench.sensor.rng.random(), bench.gateway.rng.random(), bench._rb_rng.random(), bench.sensor._xrng.random(),
             bench.gateway._act_rng.random(), bench.gateway._fault_rng.random(), bench._jitter_rng.random()]
    assert len(set(draws)) == len(draws)
    assert Sensor(seed=5).rng.random() == Gateway(seed=5).rng.random()  # plain int seeds keep the legacy shared stream


def run_preset(seed, duration=600):
    bench = SimHarness.realistic(seed=seed)
    seen = []

    def step(now_ms, samples, readbacks, gate_open):
        seen.append((now_ms, samples, readbacks))
        return {"operation": "flow_percent", "value": 80 + (now_ms // 1000) % 40}

    return bench.run(step, duration), seen


def test_realistic_preset_run_is_hostile_deterministic_and_fast():
    t0 = time.perf_counter()
    series, seen = run_preset(7)
    assert time.perf_counter() - t0 < 2.0
    delivered = [s for _, samples, _ in seen for s in samples]
    assert delivered and all("true_mm" not in s for s in delivered)
    assert all(s["recv_ms"] - s["t_ms"] == 850 + 300 and 500 <= s["t_ms"] - s["t_dep_ms"] <= 1500 for s in delivered)
    groups = series["meas_group"]
    assert list(groups[:10]) == ["cam1:-90"] * 10 and list(groups[10:20]) == ["cam3:90"] * 10
    assert list(groups[20:30]) == ["cam2:180"] * 10 and len(groups) == 600
    assert 0.25 < 1 - series["meas_ok"].mean() < 0.45
    readbacks = [rb for _, _, rb in seen]
    assert any(rb["measured"]["nozzle_c"] is None for rb in readbacks)
    states = set(series["result_state"])
    accepted = int((series["action_state"] == "accepted").sum())
    assert accepted > 300 and ({"dispatch_unknown", "queued_unconfirmed"} & states or accepted > len(series["result_t_s"]))
    again, _ = run_preset(7)
    assert all(np.array_equal(series[k], again[k], equal_nan=series[k].dtype.kind == "f") for k in series)
    other, _ = run_preset(8)
    assert not np.array_equal(series["meas_px"], other["meas_px"], equal_nan=True)


# the fake MQTT gateway, tightened -------------------------------------------------------------------------------------
def test_fake_gateway_topics_and_bounds_match_the_app():
    from server import bridge
    assert fake_gateway.DEFAULT_BASE_TOPIC + "/request" == bridge.REQUEST_TOPIC
    assert [fake_gateway.DEFAULT_BASE_TOPIC + "/result", fake_gateway.DEFAULT_BASE_TOPIC + "/status"] == bridge.WRITER_TOPICS
    assert plant_module.BOUNDS == {k: (float(low), float(high)) for k, (low, high) in bridge.OPERATIONS.items()}


def test_validate_requires_an_integer_issue_time_and_a_short_validity_window():
    now = fake_gateway.now_ms()
    problems = lambda **over: fake_gateway.validate(valid_request(**over), "c", "s", 4, now)
    assert problems(issued_utc_ms=now, expires_utc_ms=now + 1500) == []
    assert problems(issued_utc_ms=now - 100, expires_utc_ms=now + 1400) == []
    for issued in ("garbage", None, float(now), True, [now]):
        assert any(p.startswith("issued_utc_ms") for p in problems(issued_utc_ms=issued, expires_utc_ms=now + 1000))
    for issued, expires in ((now, now + 3_600_000), (now - 10, now + 1500), (now, now), (now + 2000, now + 1000)):
        assert any(p.startswith("validity window") for p in problems(issued_utc_ms=issued, expires_utc_ms=expires)), (
            issued - now, expires - now)
    assert any(p.startswith("expired") for p in problems(issued_utc_ms=now - 500, expires_utc_ms=float(now + 900)))


def test_fake_gateway_defaults_to_the_one_per_second_limit():
    assert fake_gateway.FakeGateway().min_interval_s == 1.0
    assert fake_gateway.build_parser().parse_args([]).min_interval_s == 1.0


def test_fake_gateway_rate_limits_by_default_on_the_broker(mqtt_bench):
    gateway, client, base, inbox, log = mqtt_bench(min_interval_s=1.0)
    next_message(inbox, "status")
    first, second = (valid_request(gateway_session_id="s", sequence=n) for n in (1, 2))
    for request in (first, second):
        client.publish(f"{base}/request", json.dumps(request))
    assert next_message(inbox, "result", match=lambda p: p["request_id"] == second["request_id"])["reason"] == "rate_limited"


def test_fake_gateway_timers_are_daemons_and_cancelled_on_stop(mqtt_bench):
    gateway, client, base, inbox, log = mqtt_bench(confirm_delay_s=0.8)
    next_message(inbox, "status")
    request = valid_request(gateway_session_id="s", sequence=1)
    mine = lambda p: p["request_id"] == request["request_id"]
    client.publish(f"{base}/request", json.dumps(request))
    assert next_message(inbox, "result", match=mine)["state"] == "accepted"
    timers = list(gateway._timers)
    assert timers and all(t.daemon for t in timers)
    gateway.stop()
    assert not any(t.is_alive() for t in timers) and gateway._timers == []
    with pytest.raises(AssertionError):
        next_message(inbox, "result", timeout=1.2, match=mine)


@pytest.mark.parametrize("cancel, final, reason", [(False, "setting_confirmed", None), (True, "rejected", "gate_closed")])
def test_fake_gateway_request_in_flight_when_the_gate_closes(mqtt_bench, cancel, final, reason):
    gateway, client, base, inbox, log = mqtt_bench(cancel_on_gate_close=cancel, confirm_delay_s=0.6)
    next_message(inbox, "status")
    request = valid_request(gateway_session_id="s", sequence=1)
    mine = lambda p: p["request_id"] == request["request_id"]
    client.publish(f"{base}/request", json.dumps(request))
    assert next_message(inbox, "result", match=mine)["state"] == "accepted"
    client.publish(gateway.control_topic, json.dumps({"authorized": False}))
    done = next_message(inbox, "result", match=mine)
    assert done["state"] == final and done.get("reason") == reason


def test_fake_gateway_confirms_the_stored_rounded_value(mqtt_bench):
    gateway, client, base, inbox, log = mqtt_bench()
    next_message(inbox, "status")
    request = valid_request(gateway_session_id="s", sequence=1, correction={"operation": "speed_override_percent", "value": 87.6})
    client.publish(f"{base}/request", json.dumps(request))
    done = next_message(inbox, "result", match=lambda p: p["request_id"] == request["request_id"] and p["state"] != "accepted")
    assert done["observation"]["observed_value"] == 88
