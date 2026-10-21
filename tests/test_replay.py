import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest

from conftest import CAPTURES, REPO
from tools.mqtt import replay

GOLDEN = CAPTURES / "golden-20261020-1736"
MOSQUITTO_PATHS = ["/opt/homebrew/opt/mosquitto/sbin/mosquitto", "/usr/local/opt/mosquitto/sbin/mosquitto",
                   "/usr/sbin/mosquitto"]
NOON = int(time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1)) * 1000)
TELEMETRY = "demex/demo/mini/telemetry"
RGB, IR = "demex/demo/mini/preview/rgb/trailing", "demex/demo/mini/preview/ir/trailing"
STATUS = "demex/hackathon/mini/extrusion/status"
FAKE_JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"

needs_golden = pytest.mark.skipif(not (GOLDEN / "ir.jsonl").exists(), reason="no golden capture in data/captures")


@pytest.fixture(scope="module")
def broker():
    """A private mosquitto on a free loopback port, so the tests never touch the shared :1884."""
    exe = shutil.which("mosquitto") or next((p for p in MOSQUITTO_PATHS if os.path.exists(p)), None)
    if not exe:
        pytest.skip("no mosquitto binary")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen([exe, "-p", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.skip("private mosquitto did not start")
        yield port
    finally:
        proc.terminate()
        proc.wait(5)


class FakeClient:
    """Stands in for the paho client in main(); raises Ctrl-C on the publish after stop_after messages."""

    def __init__(self, stop_after=None):
        self.published, self.stop_after, self.target = [], stop_after, None

    def connect(self, host, port):
        self.target = (host, port)

    def loop_start(self):
        pass

    loop_stop = disconnect = loop_start

    def publish(self, topic, payload, qos=0):
        if self.stop_after is not None and len(self.published) >= self.stop_after:
            raise KeyboardInterrupt
        self.published.append((topic, json.loads(payload)))
        return SimpleNamespace(rc=0, wait_for_publish=lambda timeout=None: None)


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(replay, "make_client", lambda prefix: client)
    return client


def jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_capture(root, ir_files=True):
    """Tiny capture at local noon with interleaved receive times: telemetry every 100 ms, rgb/ir/writer between."""
    (root / "rgb").mkdir(parents=True)
    (root / "ir").mkdir()
    base = NOON
    tele = [{"recv_ms": base + i * 100, "msg": {
        "session_id": "synthetic", "published_utc_ms": base + i * 100 - 120, "expires_utc_ms": base + i * 100 + 880,
        "sources": {"motion": {"source_utc_ns": (base + i * 100 - 150) * 1_000_000, "payload": {"is_moving": True}}}}}
        for i in range(10)]
    jsonl(root / "telemetry.jsonl", tele)

    def preview(kind, seq, recv, with_file=True):
        row = {"session_id": "synthetic", "sequence": seq, "published_utc_ms": recv - 300, "expires_utc_ms": recv + 70,
               "camera_kind": kind, "role": "trailing", "camera_index": 0, "source_utc_ns": (recv - 900) * 1_000_000,
               "source_age_ms": 600.5, "recv_ms": recv, "topic": f"demex/demo/mini/preview/{kind}/trailing"}
        if with_file:
            name = f"{kind}/trailing_{seq}_cam0.jpg"
            row["file"] = name
            if kind != "ir" or ir_files:
                (root / name).write_bytes(FAKE_JPEG + bytes([seq]))
        return row

    jsonl(root / "previews.jsonl", [preview("rgb", 1, base + 250), preview("rgb", 2, base + 750),
                                    preview("ir", 3, base + 400, with_file=False)])
    jsonl(root / "ir.jsonl", [preview("ir", 4, base + 250), preview("ir", 5, base + 600)])
    jsonl(root / "writer.jsonl", [{"recv_ms": base + 350, "topic": STATUS, "retain": False,
                                   "msg": {"gateway_session_id": "g", "published_utc_ms": base + 200,
                                           "expires_utc_ms": base + 1700}}])
    return base


def kinds(events):
    return [(e.rec_ms, e.kind) for e in events]


# --- restamp ---------------------------------------------------------------------------------------------------

def test_restamp_shifts_wall_clock_fields_only():
    t = 1_792_510_594_331
    msg = {
        "published_utc_ms": t, "expires_utc_ms": t + 1000, "source_age_ms": 600.5, "sequence": 7, "ok": True,
        "sources": {"motion": {"source_utc_ns": t * 1_000_000 + 123_456, "valid": True,
                               "payload": {"producer_monotonic_ns": 11_557_410_629_800, "age_ns": 130_443_500,
                                           "timestamp_ns": t * 1_000_000, "last_frame_age_ms": 218,
                                           "source_utc_ns": None, "expires_utc_ms": 0}},
                    "list": [{"acquired_at_utc_ns": t * 1_000_000}, 5]},
    }
    before = json.dumps(msg)
    out = replay.restamp(msg, -1_234_567)
    assert json.dumps(msg) == before
    assert out["published_utc_ms"] == t - 1_234_567
    assert out["expires_utc_ms"] - out["published_utc_ms"] == 1000
    assert out["sources"]["motion"]["source_utc_ns"] == (t - 1_234_567) * 1_000_000 + 123_456
    inner = out["sources"]["motion"]["payload"]
    assert inner["timestamp_ns"] == (t - 1_234_567) * 1_000_000
    assert out["sources"]["list"][0]["acquired_at_utc_ns"] == (t - 1_234_567) * 1_000_000
    assert (out["source_age_ms"], out["sequence"], out["ok"]) == (600.5, 7, True)
    assert (inner["producer_monotonic_ns"], inner["age_ns"], inner["last_frame_age_ms"]) == (
        11_557_410_629_800, 130_443_500, 218)
    assert inner["source_utc_ns"] is None and inner["expires_utc_ms"] == 0


def leaves(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, path + (i,))
    else:
        yield path, obj


def assert_all_wall_clock_fields_shifted(msg, rec_ms, offset):
    """Every epoch-scale number within a day of the receive time must have been shifted, and nothing else changed."""
    out = dict(leaves(replay.restamp(msg, offset)))
    for path, value in leaves(msg):
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        near = is_number and any(abs(value / unit - rec_ms) < 86_400_000 for unit in (1, 1_000_000))
        if near:
            unit = 1 if value < 1e15 else 1_000_000
            assert out[path] == value + offset * unit, path
        else:
            assert out[path] == value, path


@needs_golden
def test_restamp_real_messages_shift_every_wall_clock_field():
    rows = []
    for name in ("telemetry.jsonl", "writer.jsonl", "ir.jsonl", "previews.jsonl"):
        with open(GOLDEN / name) as f:
            rows += [json.loads(line) for line in [l for _, l in zip(range(300), f)][::60]]
    assert len(rows) >= 16
    for row in rows:
        msg = row["msg"] if "msg" in row else {k: v for k, v in row.items() if k not in ("recv_ms", "topic", "file")}
        assert_all_wall_clock_fields_shifted(msg, row["recv_ms"], 8_123_456)


@needs_golden
def test_restamp_real_telemetry_preserves_deltas():
    with open(GOLDEN / "telemetry.jsonl") as f:
        msg = json.loads(f.readlines(50_000)[3])["msg"]
    out = replay.restamp(msg, 42_000)
    get = lambda m: (m["expires_utc_ms"] - m["published_utc_ms"],
                     m["sources"]["motion"]["source_utc_ns"] - m["sources"]["duet_readback"]["source_utc_ns"],
                     m["sources"]["motion"]["payload"]["producer_wall_utc_ns"] - m["sources"]["motion"]["source_utc_ns"])
    assert get(out) == get(msg)
    assert out["published_utc_ms"] == msg["published_utc_ms"] + 42_000


# --- loading and merging ---------------------------------------------------------------------------------------

def test_merge_is_by_receive_time_and_skips_rows_without_image(tmp_path):
    base = make_capture(tmp_path)
    events = list(replay.load_events(str(tmp_path), writer=True))
    got = kinds(events)
    assert [ms for ms, _ in got] == sorted(ms for ms, _ in got)
    assert Counter(k for _, k in got) == {"telemetry": 10, "rgb": 2, "ir": 2, "writer": 1}
    assert got[:5] == [(base, "telemetry"), (base + 100, "telemetry"), (base + 200, "telemetry"),
                       (base + 250, "rgb"), (base + 250, "ir")]
    assert (base + 350, "writer") in got and (base + 600, "ir") in got


def test_flags_select_sources(tmp_path):
    make_capture(tmp_path)
    count = lambda **kw: Counter(e.kind for e in replay.load_events(str(tmp_path), **kw))
    assert count() == {"telemetry": 10, "rgb": 2, "ir": 2}
    assert count(ir=False) == {"telemetry": 10, "rgb": 2}
    assert count(rgb=False) == {"telemetry": 10, "ir": 2}
    assert count(ir=False, rgb=False) == {"telemetry": 10}
    assert count(writer=True)["writer"] == 1


def test_window_is_start_inclusive_end_exclusive(tmp_path):
    base = make_capture(tmp_path)
    events = list(replay.load_events(str(tmp_path), base + 250, base + 600))
    assert min(e.rec_ms for e in events) == base + 250
    assert max(e.rec_ms for e in events) < base + 600
    assert Counter(e.kind for e in events) == {"telemetry": 3, "rgb": 1, "ir": 1}


def test_image_payload_is_the_recorded_bytes_without_recorder_fields(tmp_path):
    base = make_capture(tmp_path)
    ir = next(e for e in replay.load_events(str(tmp_path)) if e.kind == "ir")
    msg = ir.load()
    assert ir.topic == IR
    assert base64.b64decode(msg.pop("jpeg_base64")) == FAKE_JPEG + bytes([4])
    assert not {"file", "recv_ms", "topic"} & msg.keys()
    assert msg["sequence"] == 4 and msg["camera_kind"] == "ir" and msg["source_age_ms"] == 600.5


def test_missing_image_file_is_skipped_and_counted(tmp_path):
    make_capture(tmp_path, ir_files=False)
    published, skips = [], Counter()
    counts, _ = replay.play(replay.load_events(str(tmp_path), skips=skips), lambda t, p: published.append(t),
                            skips=skips, clock=lambda: 0, sleep=lambda s: None)
    assert skips == {"image file missing": 2, "no image recorded": 1}
    assert counts[IR] == 0 and counts[RGB] == 2 and IR not in published


def test_capture_start_is_the_earliest_message(tmp_path):
    base = make_capture(tmp_path)
    assert replay.capture_start_ms(str(tmp_path)) == base


def test_capture_end_is_the_latest_message_even_with_huge_rows(tmp_path):
    base = make_capture(tmp_path)
    assert replay.capture_end_ms(str(tmp_path)) == base + 900
    big = {"recv_ms": base + 5_000, "msg": {"blob": "x" * 300_000}}
    with open(tmp_path / "telemetry.jsonl", "a") as f:
        f.write(json.dumps(big) + "\n")
    assert replay.capture_end_ms(str(tmp_path)) == base + 5_000


def test_empty_directory_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        replay.capture_start_ms(str(tmp_path))
    with pytest.raises(SystemExit):
        replay.capture_end_ms(str(tmp_path))


# --- time handling ---------------------------------------------------------------------------------------------

def test_play_paces_by_speed_and_restamps_by_replay_time(tmp_path):
    base = make_capture(tmp_path)
    now = {"t": 5_000.0}  # seconds, an arbitrary replay clock
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now["t"] += s

    sent = []
    counts, _ = replay.play(replay.load_events(str(tmp_path)), lambda t, p: sent.append((now["t"], t, json.loads(p))),
                            speed=2.0, clock=lambda: now["t"], sleep=sleep)
    assert sum(sleeps) == pytest.approx(0.9 / 2)
    tele = [s for s in sent if s[1] == TELEMETRY]
    assert len(tele) == 10
    for sent_at, _, msg in tele:
        published_at_recording = msg["published_utc_ms"]
        age_ms = sent_at * 1000 - published_at_recording
        assert age_ms == pytest.approx(120, abs=1.5)  # recorded: published 120 ms before receipt
        assert msg["expires_utc_ms"] - msg["published_utc_ms"] == 1000
        assert msg["sources"]["motion"]["source_utc_ns"] // 1_000_000 == msg["published_utc_ms"] - 30
    rgb = next(m for _, t, m in sent if t == RGB)
    assert rgb["expires_utc_ms"] - rgb["published_utc_ms"] == 370 and rgb["source_age_ms"] == 600.5
    assert counts == {TELEMETRY: 10, RGB: 2, IR: 2}


def test_clock_ms_is_local_time_on_the_recording_day():
    ref = int(time.mktime((2026, 10, 20, 17, 36, 34, 0, 0, -1)) * 1000) + 500
    assert replay.clock_ms("17:40:05", ref) == int(time.mktime((2026, 10, 20, 17, 40, 5, 0, 0, -1)) * 1000)
    assert replay.clock_ms("08:00", ref) == int(time.mktime((2026, 10, 20, 8, 0, 0, 0, 0, -1)) * 1000)
    with pytest.raises(SystemExit):
        replay.clock_ms("25:00:00", ref)
    with pytest.raises(SystemExit):
        replay.clock_ms("noon", ref)


def test_only_an_explicit_host_leaves_this_machine():
    assert replay.resolve_host(None) == "127.0.0.1"
    assert replay.resolve_host("localhost") == "localhost"
    with pytest.raises(SystemExit, match="refusing"):
        replay.resolve_host("broker.example")


# --- against the local broker ----------------------------------------------------------------------------------

class Listener:
    def __init__(self, port, topics):
        self.messages = []
        self.lock = threading.Lock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"test-replay-{uuid.uuid4().hex[:8]}",
                                  protocol=mqtt.MQTTv311)
        self.client.on_message = self.on_message
        self.client.on_connect = lambda c, u, f, rc, p: [c.subscribe(t) for t in topics]
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        time.sleep(0.3)

    def on_message(self, client, userdata, msg):
        with self.lock:
            self.messages.append((msg.topic, msg.payload, time.time() * 1000))

    def settle(self, session, quiet_s=0.6, limit_s=15):
        """Wait until nothing more arrives for this session; other publishers share the broker."""
        end = time.time() + limit_s
        seen = -1
        while time.time() < end:
            time.sleep(quiet_s)
            if len(self.mine(session)) == seen:
                break
            seen = len(self.mine(session))

    def mine(self, session):
        with self.lock:
            snapshot = list(self.messages)
        rows = []
        for topic, payload, at in snapshot:
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict) and session in (msg.get("session_id"), msg.get("gateway_session_id")):
                rows.append((topic, msg, at))
        return rows

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def run_cli(port, *args):
    return subprocess.run([sys.executable, str(REPO / "tools" / "mqtt" / "replay.py"), "--port", str(port),
                           *map(str, args)], capture_output=True, text=True, timeout=120, cwd=REPO)


def test_cli_replays_a_synthetic_capture_with_fresh_stamps(tmp_path, broker):
    make_capture(tmp_path)
    listener = Listener(broker, [TELEMETRY, RGB, IR, STATUS])
    try:
        done = run_cli(broker, tmp_path, "--speed", "5")
        listener.settle("synthetic")
        rows = listener.mine("synthetic")
        assert done.returncode == 0, done.stderr
        assert Counter(t for t, _, _ in rows) == {TELEMETRY: 10, RGB: 2, IR: 2}
        for topic, msg, received_at in rows:
            assert abs(received_at - msg["published_utc_ms"]) < 3_000  # looks live, not 2023
        ir = next(m for t, m, _ in rows if t == IR and m["sequence"] == 5)
        assert base64.b64decode(ir["jpeg_base64"]) == FAKE_JPEG + bytes([5])
        assert Counter(t for t, m, _ in listener.mine("g")) == {}

        listener.messages.clear()
        run_cli(broker, tmp_path, "--speed", "5", "--no-ir", "--no-rgb", "--writer-status")
        listener.settle("synthetic")
        assert Counter(t for t, _, _ in listener.mine("synthetic")) == {TELEMETRY: 10}
        assert Counter(t for t, _, _ in listener.mine("g")) == {STATUS: 1}
    finally:
        listener.close()


@needs_golden
def test_cli_replays_a_window_of_print_1_at_20x(broker):
    first = replay.capture_start_ms(str(GOLDEN))
    start = time.localtime((first + 120_000) / 1000)
    start_ms = int(time.mktime(start) * 1000)
    end_ms = start_ms + 30_000

    def expected(name):
        n = 0
        with open(GOLDEN / name) as f:
            for line in f:
                row = json.loads(line)
                if row["recv_ms"] >= end_ms:
                    break
                n += row["recv_ms"] >= start_ms and (name == "telemetry.jsonl" or bool(row.get("file")))
        return n

    want = {TELEMETRY: expected("telemetry.jsonl"), RGB: expected("previews.jsonl"), IR: expected("ir.jsonl")}
    assert all(want.values()), want
    session = json.loads(next(open(GOLDEN / "ir.jsonl")))["session_id"]
    listener = Listener(broker, [TELEMETRY, RGB, IR])
    try:
        t0 = time.time()
        done = run_cli(broker, GOLDEN, "--speed", "20", "--start", time.strftime("%H:%M:%S", start),
                       "--end", time.strftime("%H:%M:%S", time.localtime(end_ms / 1000)))
        elapsed = time.time() - t0
        listener.settle(session)
        rows = listener.mine(session)
        assert done.returncode == 0, done.stderr
        assert Counter(t for t, _, _ in rows) == want
        assert elapsed < 30
        ir = [m for t, m, _ in rows if t == IR]
        assert all(base64.b64decode(m["jpeg_base64"])[:2] == b"\xff\xd8" for m in ir)
        assert all(abs(at - m["published_utc_ms"]) < 5_000 for _, m, at in rows)
    finally:
        listener.close()


# --- offsets, skips and window handling (no broker: main() runs against a fake client) ---------------------------

def replay_offsets(events, speed, send):
    """Replay events on a fake clock; return the stamp offset (ms) each published message got, in order."""
    now = {"t": 5_000.0}

    def sleep(s):
        now["t"] += s

    originals = {id(e): e.load() for e in events}
    order, offsets = iter(events), []

    def publish(topic, payload):
        original, sent = originals[id(next(order))], json.loads(payload)
        offsets.append(sent["published_utc_ms"] - original["published_utc_ms"])
        send(original, sent)

    replay.play(iter(events), publish, speed=speed, clock=lambda: now["t"], sleep=sleep)
    return offsets


def test_speed_1_applies_one_offset_to_every_topic(tmp_path):
    make_capture(tmp_path)
    events = list(replay.load_events(str(tmp_path), writer=True))
    assert {e.kind for e in events} == {"telemetry", "rgb", "ir", "writer"}
    offsets = replay_offsets(events, 1.0, lambda original, sent: None)
    assert len(offsets) == len(events) and len(set(offsets)) == 1
    assert len(set(replay_offsets(events, 2.0, lambda original, sent: None))) > 1  # the documented drift


def test_re_stamped_ir_rows_keep_published_minus_source_equal_to_source_age(tmp_path):
    make_capture(tmp_path)
    events = [e for e in replay.load_events(str(tmp_path)) if e.kind == "ir"]
    checked = []

    def check(original, sent):
        age = sent["published_utc_ms"] - sent["source_utc_ns"] / 1e6
        assert age == pytest.approx(sent["source_age_ms"], abs=1.0)
        checked.append(age)

    replay_offsets(events, 1.0, check)
    assert len(checked) == 2


@needs_golden
def test_golden_window_at_1x_has_one_offset_and_consistent_ir_ages():
    first = replay.capture_start_ms(str(GOLDEN))
    events = list(replay.load_events(str(GOLDEN), first + 120_000, first + 150_000))
    ages = []

    def check(original, sent):
        if "source_age_ms" in sent and sent.get("camera_kind") == "ir":
            before = original["published_utc_ms"] - original["source_utc_ns"] / 1e6
            after = sent["published_utc_ms"] - sent["source_utc_ns"] / 1e6
            assert after == pytest.approx(before, abs=1e-3)
            assert abs(after - sent["source_age_ms"]) < 10  # as recorded: clocks agree to a few ms
            ages.append(after)

    offsets = replay_offsets(events, 1.0, check)
    assert Counter(e.kind for e in events).keys() >= {"telemetry", "rgb", "ir"}
    assert len(set(offsets)) == 1 and len(ages) > 10


def test_main_refuses_a_non_loopback_host_before_connecting(tmp_path, monkeypatch):
    make_capture(tmp_path)

    def connected(prefix):
        raise AssertionError("tried to connect")

    monkeypatch.setattr(replay, "make_client", connected)
    with pytest.raises(SystemExit, match="refusing"):
        replay.main([str(tmp_path), "--host", "broker.example"])


@pytest.mark.parametrize("speed", ["nan", "0", "-2"])
def test_speed_must_be_positive(tmp_path, fake_client, speed):
    make_capture(tmp_path)
    with pytest.raises(SystemExit, match="positive"):
        replay.main([str(tmp_path), f"--speed={speed}"])
    assert fake_client.target is None


def test_loop_over_an_empty_window_exits_with_an_error_instead_of_spinning(tmp_path, fake_client, monkeypatch):
    make_capture(tmp_path)
    calls = []
    real_play = replay.play

    def counting_play(*args, **kwargs):
        calls.append(1)
        assert len(calls) < 3, "still looping over an empty window"
        return real_play(*args, **kwargs)

    monkeypatch.setattr(replay, "play", counting_play)
    with pytest.raises(SystemExit, match="nothing to replay"):
        replay.main([str(tmp_path), "--loop", "--start", "13:00:00", "--end", "13:10:00"])
    assert len(calls) == 1 and fake_client.published == []


def test_loop_repeats_and_ctrl_c_summary_counts_every_pass(tmp_path, fake_client, capsys):
    make_capture(tmp_path)
    fake_client.stop_after = 14 * 2 + 3  # two full passes (10 telemetry + 2 rgb + 2 ir) and a bit of the third
    total = replay.main([str(tmp_path), "--loop", "--speed", "1000"])
    assert sum(total.values()) == 31 and total[TELEMETRY] == 10 * 2 + 3 and len(fake_client.published) == 31
    assert "published 31 messages" in capsys.readouterr().out


def test_ctrl_c_in_the_first_pass_still_reports_what_was_published(tmp_path, fake_client, capsys):
    make_capture(tmp_path)
    fake_client.stop_after = 5
    total = replay.main([str(tmp_path), "--speed", "1000"])
    assert sum(total.values()) == 5
    assert "published 5 messages" in capsys.readouterr().out


def test_skipped_rows_are_listed_with_their_reason(tmp_path, fake_client, capsys):
    make_capture(tmp_path, ir_files=False)
    replay.main([str(tmp_path), "--speed", "1000"])
    assert "skipped: 2 image file missing, 1 no image recorded" in capsys.readouterr().out
    replay.main([str(tmp_path), "--speed", "1000", "--no-ir"])
    assert "skipped: none" in capsys.readouterr().out


def test_stderr_notes_that_alignment_is_exact_only_at_1x(tmp_path, fake_client, capsys):
    make_capture(tmp_path)
    replay.main([str(tmp_path), "--speed", "1000"])
    assert "exact only at 1x" in capsys.readouterr().err
    replay.main([str(tmp_path)])
    assert capsys.readouterr().err == ""


def test_end_before_start_is_an_error_unless_the_capture_crosses_midnight(tmp_path, fake_client):
    make_capture(tmp_path)
    with pytest.raises(SystemExit, match="does not cross midnight"):
        replay.main([str(tmp_path), "--start", "12:30:00", "--end", "12:10:00"])
    assert fake_client.target is None

    night = tmp_path / "night"
    night.mkdir()
    before = int(time.mktime((2026, 10, 20, 23, 59, 59, 0, 0, -1)) * 1000) + 500
    after = int(time.mktime((2026, 10, 21, 0, 0, 0, 0, 0, -1)) * 1000) + 500
    jsonl(night / "telemetry.jsonl", [{"recv_ms": before, "msg": {"session_id": "n"}},
                                      {"recv_ms": after, "msg": {"session_id": "n"}}])
    total = replay.main([str(night), "--start", "23:59:59", "--end", "00:00:01", "--speed", "1000"])
    assert sum(total.values()) == 2
