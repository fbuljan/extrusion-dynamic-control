"""End-to-end run on this machine: a replay of print 2 -> the real app -> a fake LEAM gateway, all on the local broker.

    .venv/bin/python -m tools.sim.e2e_run smoke [--minutes 3]      # no camera model: measures, never acts
    .venv/bin/python -m tools.sim.e2e_run advice|dry|auto|all      # needs app/models/camera_model.json + printer_model.json

advice: Auto off.  dry: Auto on, sends disabled.  auto: Auto on, CONTROL_SEND_ENABLED=1 (to the fake gateway only).
Safety, in this order: the broker host must be loopback (checked before anything else, never configurable); every run
starts its own private mosquitto on a free loopback port (nobody else's traffic can reach it, and it reaches nobody); the
app gets an environment built from scratch (never .env, never ours); the replay never carries the recorded writer
status, so the fake gateway is the only status source. The replay is open-loop: readbacks never follow our commands,
so in `auto` the controller is expected to stop with an alarm after its first unconfirmed command, not to converge.
Outputs (logs, tables, no images) go under --out. IR frames reach the websocket client but only width/decision messages
are ever written.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt

from tools.sim.fake_gateway import DEFAULT_BASE_TOPIC, FakeGateway, require_loopback

REPO = Path(__file__).resolve().parents[2]
CAPTURE_NAME = "golden2-20261020-1815"
CAPTURE = REPO / "data" / "captures" / CAPTURE_NAME
FRAMES = REPO / "data" / "model" / "frames.parquet"
MODELS = REPO / "app" / "models"
BROKER_HOST = "127.0.0.1"
MOSQUITTO_PATHS = ["/opt/homebrew/opt/mosquitto/sbin/mosquitto", "/usr/local/opt/mosquitto/sbin/mosquitto",
                   "/usr/sbin/mosquitto"]
CONTROLLER_ID = "sim-controller"
REQUEST_TOPIC, RESULT_TOPIC = f"{DEFAULT_BASE_TOPIC}/request", f"{DEFAULT_BASE_TOPIC}/result"
TELEMETRY_TOPIC = "demex/demo/mini/telemetry"
IR_TOPIC = "demex/demo/mini/preview/ir/trailing"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".npy", ".npz", ".pgm", ".ppm"}
DATA_FILES = {"prototype.db", "prototype.db-wal", "prototype.db-shm", "control.json"}
KEEP_WS = {"width", "decision"}  # everything else on the socket can carry frames
ALLOWED_OPERATIONS = {"flow_percent", "speed_override_percent", "nozzle_temperature_c"}
SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules"}

SCENARIOS = {
    "smoke": {"auto": True, "send": True, "models": "geometry-only"},
    "advice": {"auto": False, "send": False, "models": "app"},
    "dry": {"auto": True, "send": False, "models": "app"},
    "auto": {"auto": True, "send": True, "models": "app"},
}
PRE_BASELINE_S = 270  # replayed baseline before the flow 60 band
HOLD_BEFORE_S = 60  # the target is taken this long before flow 60 starts
POST_BASELINE_S = 180
EFFECT_DELAY_S = 90  # a band's first layer (~53 s) plus the pressure lag and the estimator window
MATCH_TOL_MS = 250
STRICT_PX = 0.02
WARMUP_S = 20  # the app's motion buffer starts empty
MIN_DECISIONS_PER_S = 0.8  # the controller decides once a second; fewer in a judged window means a log with holes
MIN_ON_TARGET_BASELINE = 5  # on-target decisions the first baseline needs before it counts as a deadband check


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def assert_loopback(host=BROKER_HOST, port=None, broker_port=None):
    """Refuses any broker host but this machine, and, given ports, any connection to other than the run's own broker.

    Called first with the defaults, then (in run_scenario) with the host and port each process is actually handed.
    """
    if host != BROKER_HOST:
        raise SystemExit(f"refusing broker host {host!r}: the end-to-end run only talks to {BROKER_HOST}")
    require_loopback(host)
    if broker_port is not None and port != broker_port:
        raise SystemExit(f"refusing to connect to port {port!r}: the run's own broker is on {broker_port}")


def now_ms():
    return int(time.time() * 1000)


# the recording ------------------------------------------------------------------------------------------------------------
def band_starts(capture=CAPTURE):
    """Band -> recorded local-clock ms of its start, from the last band runner in bands.jsonl. Print 2: band 8 baseline,
    9 flow 60, 10 flow 140, 11 baseline in effect (the fan command changed nothing)."""
    starts = {}
    for line in (capture / "bands.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("event") == "band_start" and row.get("band", 0) >= 2:
            starts[row["band"]] = row["t_ms"]
    return starts


@dataclass
class Window:
    start_ms: int
    end_ms: int
    hold_ms: int
    flow60: tuple
    flow140: tuple
    baselines: list
    note: str = ""

    @property
    def minutes(self):
        return (self.end_ms - self.start_ms) / 60000


def make_window(minutes=None, smoke=False, bands=None):
    """Recorded-time windows (ms): what to replay, when to take the target, and where each band's effect is judged."""
    b = bands or band_starts()
    f60, f140, base = b[9], b[10], b[11]
    if smoke:
        start = b[8] + 4 * 60_000
        end = start + int((minutes or 3) * 60_000)
        return Window(start, end, start + 30_000, (0, 0), (0, 0), [], "baseline only")
    if minutes:  # a short run: from just before flow 60, so the first correction can show
        start = f60 - 90_000
        return Window(start, start + int(minutes * 60_000), f60 - 30_000,
                      (f60 + EFFECT_DELAY_S * 1000, min(f140 - 10_000, start + int(minutes * 60_000))), (0, 0), [],
                      "short")
    start = f60 - PRE_BASELINE_S * 1000
    end = base + POST_BASELINE_S * 1000
    return Window(start, end, f60 - HOLD_BEFORE_S * 1000,
                  (f60 + EFFECT_DELAY_S * 1000, f140 - 10_000), (f140 + EFFECT_DELAY_S * 1000, base - 10_000),
                  [(f60 - HOLD_BEFORE_S * 1000 + 15_000, f60 + 5_000), (base + EFFECT_DELAY_S * 1000, end)])


def first_telemetry_recv_ms(start_ms, capture=CAPTURE):
    """Recorded receive time of the first telemetry row at or after start_ms (what the replay plays first)."""
    rx = re.compile(rb'"recv_ms": (\d+)')
    with open(capture / "telemetry.jsonl", "rb") as f:
        for line in f:
            ms = int(rx.search(line).group(1))
            if ms >= start_ms:
                return ms
    raise SystemExit(f"no telemetry at or after {local_clock(start_ms)} in {capture}")


def local_clock(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


# environment and processes --------------------------------------------------------------------------------------------------
def app_environment(data_dir, tmp_dir, models_dir, send_enabled, port):
    """The app's whole environment, built here: nothing inherited, .env never read."""
    return {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "MQTT_HOST": BROKER_HOST, "MQTT_PORT": str(port), "MQTT_USER": "", "MQTT_PASSWORD": "",
            "MQTT_WRITER_USER": "sim-writer", "MQTT_WRITER_PASSWORD": "sim-writer",
            "MQTT_CONTROLLER_ID": CONTROLLER_ID, "DATA_DIR": str(data_dir), "TMPDIR": str(tmp_dir),
            "MODELS_DIR": str(models_dir), "CONTROL_SEND_ENABLED": "1" if send_enabled else "0",
            "APP_VERSION": "e2e"}


def free_port():
    with socket.socket() as s:
        s.bind((BROKER_HOST, 0))
        return s.getsockname()[1]


def find_mosquitto():
    return shutil.which("mosquitto") or next((p for p in MOSQUITTO_PATHS if os.path.exists(p)), None)


def start_private_broker(procs, out):
    """A mosquitto of our own on a free loopback port; returns the port. SystemExit when there is no binary."""
    exe = find_mosquitto()
    if not exe:
        raise SystemExit("no mosquitto binary found: the end-to-end run needs one for its private broker")
    port = free_port()
    procs.start([exe, "-p", str(port)], REPO, {"PATH": "/usr/bin:/bin"}, out / "broker.log")
    for _ in range(100):
        try:
            socket.create_connection((BROKER_HOST, port), timeout=0.2).close()
            return port
        except OSError:
            time.sleep(0.1)
    raise SystemExit("the private mosquitto did not come up")


def http(port, path, body=None, timeout=10):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://{BROKER_HOST}:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Procs:
    """Child processes in their own sessions, always killed on the way out."""

    def __init__(self):
        self.running = []

    def start(self, argv, cwd, env, log_path):
        out = open(log_path, "wb")
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        self.running.append((p, out))
        return p

    def stop(self, p, grace=20):
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(grace)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(5)

    def close(self, grace=30):
        for p, out in reversed(self.running):  # the broker was started first, so it stops last
            self.stop(p, grace)
            out.close()


class WsRecorder(threading.Thread):
    """Reads the app's websocket; writes only width and decision messages (scalars) to JSONL."""

    def __init__(self, port, path):
        super().__init__(daemon=True)
        self.url, self.path, self.stop_flag = f"ws://{BROKER_HOST}:{port}/ws", path, threading.Event()
        self.seen, self.error = Counter(), None

    def run(self):
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect
        try:
            with connect(self.url, max_size=None, open_timeout=15) as ws, open(self.path, "w") as f:
                while not self.stop_flag.is_set():
                    try:
                        raw = ws.recv(timeout=0.5)
                    except TimeoutError:
                        continue
                    except ConnectionClosed:
                        if not self.stop_flag.is_set():
                            self.error = "the app closed the websocket before the run ended"
                        break
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    self.seen[kind] += 1
                    if kind in KEEP_WS:
                        f.write(json.dumps({"wall_ms": now_ms(), **msg}) + "\n")
                        f.flush()
        except Exception as e:  # noqa: BLE001 - reported in the summary
            self.error = f"{type(e).__name__}: {e}"


class Tap:
    """Records every request and result on the writer topics of the local broker, and when telemetry first arrives."""

    def __init__(self, path, port):
        self.f = open(path, "w")
        self.lock = threading.Lock()
        self.port = port
        self.first_telemetry_ms = None
        self.counts = Counter()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"e2e-tap-{uuid.uuid4().hex[:8]}",
                                  protocol=mqtt.MQTTv311)
        self.client.on_connect = lambda c, u, f, rc, p=None: c.subscribe(
            [(REQUEST_TOPIC, 0), (RESULT_TOPIC, 0), (TELEMETRY_TOPIC, 0), (IR_TOPIC, 0)])
        self.client.on_message = self._on_message

    def _on_message(self, client, userdata, msg):
        if msg.topic in (TELEMETRY_TOPIC, IR_TOPIC):
            self.counts[msg.topic] += 1
            if msg.topic == TELEMETRY_TOPIC:
                self.first_telemetry_ms = self.first_telemetry_ms or now_ms()
            return
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            payload = msg.payload[:200].decode("utf-8", "replace")
        with self.lock:
            self.f.write(json.dumps({"wall_ms": now_ms(), "topic": msg.topic.rsplit("/", 1)[-1], "payload": payload}) + "\n")
            self.f.flush()

    def start(self):
        self.client.connect(BROKER_HOST, self.port)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()
        self.f.close()


# one run -------------------------------------------------------------------------------------------------------------------------
@dataclass
class Run:
    name: str
    dir: Path
    window: Window
    send: bool
    auto: bool
    port: int = 0
    replay_wall_ms: int = 0
    posts: list = field(default_factory=list)  # (wall_ms, what, status, body)
    api: dict = field(default_factory=dict)  # last GET /api/control
    seen: dict = field(default_factory=dict)
    gateway_entries: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    images_before: set = field(default_factory=set)
    broker_port: int = 0
    code_before: dict = field(default_factory=dict)
    code_after: dict = field(default_factory=dict)
    pipeline_api: dict = field(default_factory=dict)  # last GET /api/pipeline
    tap_counts: dict = field(default_factory=dict)
    models_at_start: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    shift_ms: float = 0.0  # live clock minus recorded clock: first telemetry's arrival minus its recorded receive time

    @property
    def data(self):
        return self.dir / "data"

    @property
    def tmp(self):
        return self.dir / "tmp"

    @property
    def ws_path(self):
        return self.dir / "ws.jsonl"

    @property
    def requests_path(self):
        return self.dir / "requests.jsonl"

    @property
    def db_path(self):
        return self.data / "prototype.db"

    def live(self, rec_ms):
        """A recorded laptop-clock time on the replay's (live) clock."""
        return rec_ms + self.shift_ms


def image_files(root=REPO):
    found = set()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(base) == root / "data" and d == "captures")]
        found |= {str(Path(base) / f) for f in files if Path(f).suffix.lower() in IMAGE_SUFFIXES}
    return found


def prepare_models(name, out):
    """MODELS_DIR for the scenario: app/models, or (smoke) a folder with the geometry only, so no camera model exists."""
    if SCENARIOS[name]["models"] == "app":
        return MODELS
    only = out / "models"
    only.mkdir()
    shutil.copy(MODELS / "geometry.json", only / "geometry.json")
    return only


def models_ready(models_dir=MODELS):
    """True when the fitted camera and printer models exist and load (the model scenarios need them)."""
    code = ("import sys; sys.path.insert(0, 'app'); from server.pipeline import load_models; m = load_models(sys.argv[1]); "
            "sys.exit(0 if all(m[k] is not None for k in ('geometry', 'camera_model', 'printer_model')) else 1)")
    done = subprocess.run([sys.executable, "-c", code, str(models_dir)], cwd=REPO, env={"PATH": "/usr/bin:/bin"},
                          capture_output=True)
    return done.returncode == 0


def run_scenario(name, minutes=None, out_root=None, rgb=False, settle_s=8):
    """Runs one scenario end to end and returns the Run (files under run.dir). Raises on infrastructure trouble."""
    assert_loopback()
    spec = SCENARIOS[name]
    if not CAPTURE.joinpath("ir.jsonl").exists():
        raise SystemExit(f"{CAPTURE} is missing: the run replays print 2")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(out_root or default_out()) / f"{name}-{stamp}"
    for sub in ("data", "tmp"):
        (out / sub).mkdir(parents=True)
    run = Run(name, out, make_window(minutes, smoke=name == "smoke"), spec["send"], spec["auto"])
    run.images_before, run.code_before = image_files(), code_state()
    procs, gateway, tap, ws = Procs(), None, None, None
    try:
        run.broker_port = port = start_private_broker(procs, out)
        env = app_environment(run.data, run.tmp, prepare_models(name, out), spec["send"], port)
        assert_loopback(env["MQTT_HOST"], int(env["MQTT_PORT"]), port)
        run.port = free_port()
        gateway = FakeGateway(BROKER_HOST, port, CONTROLLER_ID, log_path=str(out / "gateway.jsonl")).start()
        tap = Tap(run.requests_path, port)
        tap.start()
        app = procs.start([sys.executable, "-m", "uvicorn", "server.main:app", "--host", BROKER_HOST, "--port",
                           str(run.port), "--log-level", "warning"], REPO / "app", env, out / "app.log")
        wait_for_app(run, app)
        run.models_at_start = http(run.port, "/api/pipeline")[1].get("models")
        ws = WsRecorder(run.port, run.ws_path)
        ws.start()
        w = run.window
        argv = [sys.executable, "-m", "tools.mqtt.replay", str(CAPTURE), "--host", BROKER_HOST, "--port", str(port),
                "--speed", "1", "--start", local_clock(w.start_ms), "--end", local_clock(w.end_ms)]
        if not rgb:
            argv.append("--no-rgb")
        assert_loopback(argv[argv.index("--host") + 1], int(argv[argv.index("--port") + 1]), port)
        log(f"{name}: replaying {local_clock(w.start_ms)}-{local_clock(w.end_ms)} ({w.minutes:.1f} min) at 1x on a "
            f"private broker (port {port}); target at {local_clock(w.hold_ms)}; auto={spec['auto']} "
            f"send={spec['send']}; output {out}")
        run.replay_wall_ms = now_ms()
        replay = procs.start(argv, REPO, {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}, out / "replay.log")
        first_rec = first_telemetry_recv_ms(w.start_ms)
        while tap.first_telemetry_ms is None and replay.poll() is None and now_ms() - run.replay_wall_ms < 30_000:
            time.sleep(0.05)
        run.shift_ms = (tap.first_telemetry_ms or run.replay_wall_ms) - first_rec
        drive(run, replay, spec)
        time.sleep(settle_s)
        run.api = http(run.port, "/api/control")[1]
        run.pipeline_api = http(run.port, "/api/pipeline")[1]
        run.seen = dict(ws.seen)
        run.tap_counts = dict(tap.counts)
        if ws.error:
            run.errors.append(f"websocket: {ws.error}")
    finally:
        if ws:
            ws.stop_flag.set()
            ws.join(5)
        run.code_after = code_state()
        if gateway:
            run.gateway_entries = list(gateway.entries)
            gateway.stop()
        if tap:
            tap.stop()
        procs.close()  # the app first writes its WAL back (SIGTERM -> lifespan shutdown); the broker goes with it
    write_json(out / "run.json", {"name": name, "window": run.window.__dict__, "send": run.send, "auto": run.auto,
                                  "broker_port": run.broker_port, "posts": run.posts, "api": run.api,
                                  "pipeline": run.pipeline_api, "seen": run.seen, "errors": run.errors,
                                  "gateway_entries": len(run.gateway_entries), "code": run.code_before,
                                  "code_after": run.code_after, "tap_counts": run.tap_counts, "shift_ms": run.shift_ms,
                                  "models_at_start": run.models_at_start,
                                  "notes": run.notes})
    return run


WATCHED = ("app/server/width.py", "app/server/geometry.py", "app/models/geometry.json")
INFO = ("app/server/pipeline.py", "app/server/controller.py", "app/server/estimator.py", "app/server/camera_model.py",
        "app/server/printer_model.py", "app/models/camera_model.json", "app/models/printer_model.json",
        "data/model/frames.parquet")


def code_state():
    """Modification times of what the per-frame widths depend on, and of the rest of the stack, for the report."""
    return {f: (REPO / f).stat().st_mtime_ns if (REPO / f).exists() else None for f in (*WATCHED, *INFO)}


def staleness(run):
    """Why the width comparison may not mean what it says: the extractor changed under the run, or the offline widths
    were built by an older one."""
    notes = []
    for f in WATCHED if run.code_after else ():
        if run.code_before.get(f) != run.code_after.get(f):
            notes.append(f"INCONCLUSIVE: {f} changed while the run was going; rerun")
        parquet, mtime = run.code_after.get("data/model/frames.parquet"), run.code_after.get(f)
        if parquet and mtime and parquet < mtime:
            notes.append(f"INCONCLUSIVE: frames.parquet is older than {f}; rebuild it (python -m tools.model.dataset build)")
    return notes


def drift_notes(run):
    """Files of the rest of the stack edited while the run went: the app ran the version it loaded at its start."""
    if not run.code_after:
        return []
    return [f"{f} was edited during the run; the app ran the version loaded at its start"
            for f in INFO if run.code_before.get(f) != run.code_after.get(f)]


def load_run(path):
    """A finished run from its folder, for analysing again with newer checks. Parts the folder cannot give back (the
    repo's image files before the run, older run files) are left out and their checks skipped."""
    path = Path(path)
    saved = json.loads((path / "run.json").read_text())
    window = Window(**saved["window"])
    window.flow60, window.flow140 = tuple(window.flow60), tuple(window.flow140)
    window.baselines = [tuple(b) for b in window.baselines]
    run = Run(saved["name"], path, window, saved["send"], saved["auto"])
    run.images_before = None
    run.broker_port = saved.get("broker_port", 0)
    run.posts, run.api, run.pipeline_api = saved["posts"], saved["api"], saved.get("pipeline", {})
    run.seen, run.errors, run.notes = saved["seen"], saved["errors"], []
    run.code_before, run.code_after = saved.get("code", {}), saved.get("code_after", {})
    run.tap_counts = saved.get("tap_counts", {})
    run.models_at_start = saved.get("models_at_start", {})
    gateway_log = path / "gateway.jsonl"
    run.gateway_entries = read_jsonl(gateway_log) if gateway_log.exists() else []
    run.shift_ms = saved.get("shift_ms") or (run.posts[0][0] - window.hold_ms if run.posts else 0.0)
    return run


def default_out():
    return REPO / "data" / "e2e"  # data/ is gitignored


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=1, default=str))


def wait_for_app(run, proc, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"the app exited early (code {proc.returncode}); see {run.dir / 'app.log'}")
        try:
            if http(run.port, "/api/version", timeout=1)[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise SystemExit(f"the app did not come up within {timeout} s; see {run.dir / 'app.log'}")


def post(run, what, body):
    status, reply = http(run.port, "/api/control", body)
    run.posts.append((now_ms(), what, status, reply.get("error") or {k: reply.get(k) for k in ("target_mm", "auto")}))
    return status, reply


def usable_target(reply, margin=0.05):
    """A held reading at or just under the camera's ceiling is a target the controller itself refuses."""
    mm, ceiling = reply.get("target_mm"), reply.get("ceiling_mm")
    return mm is not None and (ceiling is None or mm <= ceiling - margin)


def drive(run, replay, spec):
    """Takes the target, then switches Auto on, at their recorded times while the replay runs.

    "Hold current" is retried every few seconds until the reading it takes sits clear of the ceiling, as an operator
    would; a reading that high is noted, since the controller then refuses it as a target.
    """
    w, smoke = run.window, run.name == "smoke"
    target_at, target_ok, auto_ok = run.live(w.hold_ms), False, False
    while replay.poll() is None:
        t = now_ms()
        if not target_ok and t >= target_at:
            body, what = ({"target_mm": 1.5}, "target 1.5 mm") if smoke else ({"hold_current": True}, "hold current")
            status, reply = post(run, what, body)
            target_ok = status == 200 and (smoke or usable_target(reply))
            if status == 200 and not target_ok:
                run.notes.append(f"hold current took {reply.get('target_mm')} mm, at the camera's ceiling "
                                 f"({reply.get('ceiling_mm')} mm): tried again")
            if not target_ok and t > target_at + 120_000:
                run.errors.append("hold_current kept failing or kept taking readings at the ceiling: no usable target")
                break
            if not target_ok:
                time.sleep(4)
        elif target_ok and spec["auto"] and not auto_ok:
            auto_ok = post(run, "auto on", {"auto": True})[0] == 200
        time.sleep(0.5)
    try:
        replay.wait(w.minutes * 60 + 60)
    except subprocess.TimeoutExpired:
        run.errors.append("the replay did not finish")
    if replay.returncode:
        run.errors.append(f"the replay exited with {replay.returncode}")


# analysis ----------------------------------------------------------------------------------------------------------------------
def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def store_rows(db_path):
    import pandas as pd
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {t: pd.read_sql(f'SELECT * FROM "{t}" ORDER BY rowid', db)
                for t in ("width_samples", "estimates", "decisions", "commands")}
    finally:
        db.close()


def match_frames(live, offline):
    """Pairs each stored live row with the offline row of the same camera whose time differs by the run's one shift.

    The shift (replay clock minus recorded clock plus the app's clock offset) is found from the first live rows; a
    live row matches only within MATCH_TOL_MS of it, and an offline row is used at most once.
    """
    a, b = live.t_ms.to_numpy(float), offline.t_ms.to_numpy(float)
    ca, cb = live.camera.to_numpy(), offline.camera.to_numpy()
    if len(a) == 0 or len(b) == 0:
        return live.iloc[0:0], offline.iloc[0:0], None
    best, shift = -1, None
    for ia in range(min(8, len(a))):
        for jb in range(min(80, len(b))):
            if ca[ia] != cb[jb]:
                continue
            d = a[ia] - b[jb]
            j = np.clip(np.searchsorted(b, a - d), 1, len(b) - 1)
            near = np.where(np.abs(b[j - 1] - (a - d)) < np.abs(b[j] - (a - d)), j - 1, j)
            hits = int(((np.abs(b[near] - (a - d)) < 100) & (cb[near] == ca)).sum())
            if hits > best:
                best, shift = hits, d
    if shift is None:
        return live.iloc[0:0], offline.iloc[0:0], None
    j = np.clip(np.searchsorted(b, a - shift), 1, len(b) - 1)
    near = np.where(np.abs(b[j - 1] - (a - shift)) < np.abs(b[j] - (a - shift)), j - 1, j)
    gap = np.abs(b[near] - (a - shift))
    ok = (gap <= MATCH_TOL_MS) & (cb[near] == ca)
    used, keep = {}, []
    for i in np.argsort(gap):
        if ok[i] and near[i] not in used:
            used[near[i]] = i
            keep.append(i)
    keep = np.array(sorted(keep), dtype=int)
    return live.iloc[keep].reset_index(drop=True), offline.iloc[near[keep]].reset_index(drop=True), shift


def compare_widths(run):
    """Live per-frame widths (from the app's SQLite log) against the dataset's offline widths for the same frames."""
    import pandas as pd
    w = run.window
    live = store_rows(run.db_path)["width_samples"].sort_values("t_ms").reset_index(drop=True)
    if not FRAMES.exists():
        return {"problems": [f"{FRAMES} is missing: nothing to compare the widths with"], "frames": len(live)}
    off = pd.read_parquet(FRAMES, columns=["capture", "camera", "t_ms", "recv_ms", "width_px", "reason", "group"])
    off = off[(off.capture == CAPTURE_NAME) & (off.recv_ms >= w.start_ms) & (off.recv_ms < w.end_ms)]
    off = off.sort_values("t_ms").reset_index(drop=True)
    mine, theirs, shift = match_frames(live, off)
    stats = {"live_rows": len(live), "offline_rows": len(off), "matched": len(mine)}
    problems = []
    if len(mine) == 0:
        return {**stats, "problems": ["no stored width matched an offline frame"]}
    age_s = (mine.t_ms - mine.t_ms.iloc[0]) / 1000
    strict = age_s >= WARMUP_S
    same = (mine.reason == theirs.reason).to_numpy()
    both_ok = ((mine.reason == "ok") & (theirs.reason == "ok")).to_numpy()
    delta = (mine.width_px - theirs.width_px).abs().to_numpy()
    group_same = (mine.group == theirs.group).to_numpy()
    stats |= {
        "unmatched_live": len(live) - len(mine), "unmatched_offline": len(off) - len(mine),
        "reason_mismatch": int((~same & strict).sum()), "reason_mismatch_warmup": int((~same & ~strict).sum()),
        "group_mismatch": int((~group_same & strict).sum()), "ok_pairs": int((both_ok & strict).sum()),
        "max_abs_dpx": float(np.nanmax(delta[both_ok & strict])) if (both_ok & strict).any() else None,
        "median_abs_dpx": float(np.nanmedian(delta[both_ok & strict])) if (both_ok & strict).any() else None,
        "over_tolerance": int((~(delta <= STRICT_PX) & both_ok & strict).sum()),  # NaN counts as a failure
        "nan_pairs": int((np.isnan(delta) & both_ok & strict).sum()),
        "equal_ok_pairs": int(((delta <= STRICT_PX) & both_ok & strict).sum()), "tolerance_px": STRICT_PX,
        "same_reason_frames": int((same & strict).sum()), "frames_after_warmup": int(strict.sum()),
        "warmup_s": WARMUP_S, "frame_time_shift_ms": shift,
    }
    stats["verdict"] = (f"widths equal within {STRICT_PX} px on {stats['equal_ok_pairs']} of {stats['ok_pairs']} ok pairs; "
                        f"{stats['same_reason_frames']} of {stats['frames_after_warmup']} frames after the "
                        f"{WARMUP_S} s warmup got the same reason as offline ({len(mine)} of {len(live)} stored rows matched)")
    if len(mine) < 0.9 * len(live):
        problems.append(f"only {len(mine)} of {len(live)} stored rows matched an offline frame")
    if stats["reason_mismatch"]:
        bad = Counter(zip(mine.reason[~same & strict], theirs.reason[~same & strict]))
        problems.append(f"{stats['reason_mismatch']} frames got another reason than offline: {dict(bad.most_common(5))}")
    if stats["over_tolerance"]:
        problems.append(f"{stats['over_tolerance']} ok frames differ by more than {STRICT_PX} px or have no comparable "
                        f"width ({stats['nan_pairs']} NaN; max {stats['max_abs_dpx']})")
    if stats["ok_pairs"] < 20:
        problems.append(f"only {stats['ok_pairs']} ok frames to compare")
    return {**stats, "problems": problems}


def check_safety(run):
    """Files, images, SQLite content and (no-model) requests. Returns problems."""
    problems = []
    names = {p.name for p in run.data.iterdir()}
    if not names <= DATA_FILES:
        problems.append(f"files in DATA_DIR besides the database and control.json: {sorted(names - DATA_FILES)}")
    if any(run.tmp.iterdir()):
        problems.append(f"files in the app's TMPDIR: {sorted(p.name for p in run.tmp.iterdir())}")
    new_images = image_files() - run.images_before if run.images_before is not None else set()
    if new_images:
        problems.append(f"new image-like files in the repo: {sorted(new_images)}")
    for base, _, files in os.walk(run.dir):
        problems += [f"image-like file in the output: {Path(base) / f}" for f in files
                     if Path(f).suffix.lower() in IMAGE_SUFFIXES]
    db = sqlite3.connect(f"file:{run.db_path}?mode=ro", uri=True)
    try:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for _, col, sql_type, *_ in db.execute(f'PRAGMA table_info("{table}")'):
                kinds = {k for (k,) in db.execute(f'SELECT DISTINCT typeof("{col}") FROM "{table}"')}
                if kinds - {"null", "integer", "real", "text"}:
                    problems.append(f"{table}.{col} holds {sorted(kinds)}")
                if "text" in kinds:
                    longest, jpeg = db.execute(f'SELECT max(length("{col}")), sum("{col}" LIKE \'/9j/%\') FROM "{table}"').fetchone()
                    if (longest or 0) > 1000 or jpeg:
                        problems.append(f"{table}.{col} holds long text or a base64 JPEG")
    finally:
        db.close()
    if run.ws_path.exists() and "base64" in run.ws_path.read_text():
        problems.append("the websocket log holds image data")
    return problems


def check_traffic(run):
    """Everything the tap heard on the printer topics came from our own replay."""
    log_text = (run.dir / "replay.log").read_text() if (run.dir / "replay.log").exists() else ""
    m = re.search(r"published \d+ messages (\{.*?\});", log_text)
    if not m:
        return ["the replay did not report what it published"]
    published = ast.literal_eval(m.group(1))
    problems = []
    for topic in (TELEMETRY_TOPIC, IR_TOPIC):
        heard, sent = run.tap_counts.get(topic, 0), published.get(topic, 0)
        if heard != sent:
            problems.append(f"{topic}: the replay published {sent} but the broker delivered {heard}")
    return problems


def check_pipeline_stats(run, expect_sent):
    """/api/pipeline's counters agree with what reached the broker."""
    stats = run.pipeline_api.get("stats") or {}
    problems = []
    if stats.get("sent") != expect_sent:
        problems.append(f"the app counts {stats.get('sent')} sends, the broker saw {expect_sent}")
    if not expect_sent and (stats.get("send_failed") or stats.get("send_uncertain")):
        problems.append(f"send_failed/send_uncertain without any send: {stats}")
    if run.pipeline_api.get("clock_alarm"):
        problems.append(f"clock alarm: {run.pipeline_api['clock_alarm']}")
    return problems


def requests_seen(run):
    return [r for r in read_jsonl(run.requests_path) if r["topic"] == "request"]


def check_no_requests(run):
    problems = []
    if requests_seen(run):
        problems.append(f"{len(requests_seen(run))} requests reached the writer request topic")
    if run.gateway_entries:
        problems.append(f"the fake gateway saw {len(run.gateway_entries)} requests")
    return problems


def check_no_model(run):
    problems = check_no_requests(run)
    api = run.api
    if api.get("estimate") is not None:
        problems.append("an estimate exists without a camera model")
    reason = (api.get("last_decision") or {}).get("reason", "")
    if not reason.startswith("no model"):
        problems.append(f"the last decision is {reason!r}, not a 'no model' hold")
    if (api.get("models") or {}).get("camera_model", {}).get("present"):
        problems.append("a camera model is present: this is not the no-model run")
    if not api.get("send_enabled") or not api.get("auto"):
        problems.append(f"the worst case was not set up: send_enabled={api.get('send_enabled')} auto={api.get('auto')}")
    decisions = [m for m in read_jsonl(run.ws_path) if m["type"] == "decision"]
    if not decisions or any(not d["hold"] or d["action"] for d in decisions):
        problems.append("a decision without a hold or with an action")
    w = run.window
    in_window = decisions_in(run, w.start_ms, w.end_ms, only_active=False)
    problems += [p for p in [sparse("replayed window", len(in_window), (w.end_ms - w.start_ms) / 1000)] if p]
    return problems


def decisions_in(run, rec_start, rec_end, only_active=True):
    lo, hi = run.live(rec_start), run.live(rec_end)
    out = [m for m in read_jsonl(run.ws_path) if m["type"] == "decision" and lo <= m["t_ms"] < hi]
    return [m for m in out if m["action"] and not m["hold"]] if only_active else out


def sparse(label, n, seconds):
    """A problem text when a judged window holds too few decisions to say anything (a cut or empty log passes nothing)."""
    if n < MIN_DECISIONS_PER_S * seconds:
        return (f"{label}: {n} decisions in {seconds:.0f} s, fewer than {MIN_DECISIONS_PER_S}/s: "
                f"the decision log is incomplete")


WALLS_ALARM = re.compile(r"alarm: below [\d.]+ mm \(walls may not touch\); ")


def own(reason):
    """The reason without the app's walls alarm in front (D45): checks of the controller's verdict read what follows."""
    m = WALLS_ALARM.match(reason)
    return reason[m.end():] if m else reason


def check_advice(run, mode, dry):
    """Right sign wherever the controller acted, nothing advised in baseline 0, and a log dense enough to say so.

    Flow 60 reads narrow: it must hold at least one actionable decision, every one must widen the bead, and no decision
    may call the bead on target. Flow 140 sits above the camera model's knee, where the IR is blind to over-width: no
    advice is the best answer, and any advice must still narrow the bead. Only baseline 0 (before any disturbance, the
    camera able to see) shows the deadband holding; baseline 1 mostly holds at the camera's ceiling or on an alarm, so it
    only reports. The report says how many decisions each sign verdict rests on.
    """
    w, problems, report = run.window, [], {}
    every = [m for m in read_jsonl(run.ws_path) if m["type"] == "decision"]
    auto_on = next((p[0] for p in run.posts if p[1] == "auto on" and p[2] == 200), None)
    if auto_on is not None:  # decisions before the switch are advice by definition
        every = [m for m in every if m["wall_ms"] > auto_on + 1500]
    refused = [m for m in every if own(m["reason"]).startswith("alarm: target")]
    if refused:
        problems.append(f"{len(refused)} decisions refuse the target as above the camera's ceiling "
                        f"({refused[0]['reason']!r}): the scenario did not exercise the controller")
    expected = {(mode, dry)}
    if {(m["mode"], m.get("dry", False)) for m in every} != expected:
        problems.append(f"decision modes {sorted({(m['mode'], m.get('dry')) for m in every})}, expected {sorted(expected)}")
    evidence = []
    for label, (lo, hi), sign, must_advise, plan in (("flow 60", w.flow60, +1, True, {"flow_percent": 60}),
                                                     ("flow 140", w.flow140, -1, False, {"flow_percent": 140})):
        if hi <= lo:
            continue
        window = decisions_in(run, lo, hi, only_active=False)
        problems += [p for p in [sparse(label, len(window), (hi - lo) / 1000)] if p]
        acted = [m for m in window if m["action"] and not m["hold"]]
        wrong = [m for m in acted if m["error_mm"] is None or m["error_mm"] * sign <= 0 or not lever_direction_ok(m, sign, plan)]
        holds = Counter(own(m["reason"]).split(" (")[0].split(";")[0][:48] for m in window if m not in acted)
        report[label] = {"seconds": round((hi - lo) / 1000), "decisions": len(window), "actionable": len(acted),
                         "right_sign": len(acted) - len(wrong), "wrong_sign": len(wrong),
                         "holds": dict(holds.most_common(6))}
        evidence.append(f"{label}: {len(acted) - len(wrong)} of {len(acted)} actionable decisions right-sign")
        if wrong:
            problems.append(f"{label}: {len(wrong)} of {len(acted)} advised actions point the wrong way, "
                            f"e.g. {wrong[0]['action']} (error {wrong[0]['error_mm']})")
        if must_advise:
            on_target = sum(own(m["reason"]).startswith("on target") for m in window)
            if on_target:
                problems.append(f"{label}: {on_target} of {len(window)} decisions call the bead on target")
            if not acted:
                problems.append(f"{label}: no actionable decision in {len(window)}: the sign of the advice was not "
                                f"tested; holds {dict(holds.most_common(3))}")
    report["sign_evidence"] = "; ".join(evidence)
    report["pinned_at_end"] = sorted(run.api.get("pinned") or {})
    for i, (lo, hi) in enumerate(w.baselines):
        window = decisions_in(run, lo, hi, only_active=False)
        acted = [m for m in window if m["action"] and not m["hold"]]
        on_target = sum(own(m["reason"]).startswith("on target") for m in window)
        holds = Counter(own(m["reason"]).split(" (")[0].split(";")[0][:48] for m in window if m not in acted)
        report.setdefault("baseline", []).append({
            "seconds": round((hi - lo) / 1000), "decisions": len(window), "actionable": len(acted),
            "on_target": on_target, "holds": dict(holds.most_common(4)),
            "deadband_check": i == 0 and not acted and on_target >= MIN_ON_TARGET_BASELINE})
        label = f"baseline {i} {local_clock(lo)}-{local_clock(hi)}"
        problems += [p for p in [sparse(label, len(window), (hi - lo) / 1000)] if p]
        if acted:
            problems.append(f"{label}: {len(acted)} advised actions, e.g. {acted[0]['action']} (error {acted[0]['error_mm']})")
        elif i == 0 and on_target < MIN_ON_TARGET_BASELINE:
            problems.append(f"{label}: only {on_target} on-target decisions of {len(window)}: nothing shows the deadband holds")
    return problems, report


BASELINE_SETTINGS = {"flow_percent": 100, "speed_override_percent": 100, "nozzle_temperature_c": 220}


def lever_direction_ok(decision, sign, current=None):
    """sign +1: the bead is too narrow, advice must widen it (more flow / slower / hotter than `current`, the setting
    the replayed band actually runs: the band plan, not what the controller claims)."""
    current = {**BASELINE_SETTINGS, **(current or {})}
    op, value = decision["action"]["operation"], decision["action"]["value"]
    if op == "flow_percent":
        return (value > current[op]) if sign > 0 else (value < current[op])
    if op == "speed_override_percent":
        return (value < current[op]) if sign > 0 else (value > current[op])
    if op == "nozzle_temperature_c":
        return (value > current[op]) if sign > 0 else (value < current[op])
    return False


def check_auto(run):
    """Exactly one valid command, then a held alarm that never clears and no second send.

    The replay is open loop (readbacks never follow the command), so the right outcome is one command, an alarm within
    30 s, and from the first alarm on nothing but held alarms. No command at all fails: the run would prove nothing.
    """
    problems, reqs = check_gateway(run), requests_seen(run)
    every = [m for m in read_jsonl(run.ws_path) if m["type"] == "decision"]
    if len(reqs) != 1:
        would = [m for m in every if own(m["reason"]).startswith("gate closed (would set")]
        why = f"; Auto wanted to act ({len(would)} decisions, e.g. {would[0]['reason']!r}) but the gate stayed closed" if would else ""
        problems.append(f"{len(reqs)} requests, expected exactly one (open loop: one unconfirmed command, then an alarm){why}")
    if not reqs:
        return problems
    first = reqs[0]["wall_ms"]
    after = [m for m in every if m["wall_ms"] > first]
    alarms = [m for m in after if own(m["reason"]).startswith("alarm:")]
    if not alarms:
        problems.append("no 'alarm:' decision after the first (unconfirmed) command")
    else:
        if alarms[0]["wall_ms"] - first > 30_000:
            problems.append(f"the alarm came {(alarms[0]['wall_ms'] - first) / 1000:.0f} s after the first command")
        later = [m for m in after if m["wall_ms"] >= alarms[0]["wall_ms"]]
        released = [m for m in later if not (m["hold"] and own(m["reason"]).startswith("alarm:"))]
        if released:
            problems.append(f"{len(released)} of {len(later)} decisions after the first alarm are not held alarms, "
                            f"e.g. {released[0]['reason']!r}")
    ids = [r["payload"]["request_id"] for r in reqs]
    if len(set(ids)) != len(ids):
        problems.append("a request id was published twice")
    return problems


def check_gateway(run):
    problems = []
    reqs = [r["payload"] for r in requests_seen(run)]
    walls = [r["wall_ms"] for r in requests_seen(run)]
    for a, b in zip(walls, walls[1:]):
        if b - a < 2000:
            problems.append(f"requests {b - a} ms apart")
    bounds = {"flow_percent": (70, 130), "speed_override_percent": (60, 100), "nozzle_temperature_c": (190, 250)}
    for r in reqs:
        c = r.get("correction") or {}
        if c.get("operation") not in ALLOWED_OPERATIONS:
            problems.append(f"operation {c.get('operation')!r} is not a controller lever")
        elif not bounds[c["operation"]][0] <= c.get("value", -1) <= bounds[c["operation"]][1]:
            problems.append(f"{c['operation']}={c.get('value')} outside {bounds[c['operation']]}")
        if r.get("controller_id") != CONTROLLER_ID or r.get("schema_version") != "0.2.0":
            problems.append("malformed request header")
    bad = [e for e in run.gateway_entries if e["state"] != "accepted"]
    if bad:
        problems.append(f"the fake gateway refused {len(bad)} requests: {[e['reason'] for e in bad][:3]}")
    if len(run.gateway_entries) != len(reqs):
        problems.append(f"the broker carried {len(reqs)} requests but the fake gateway logged {len(run.gateway_entries)}")
    return problems


def estimate_report(run):
    """Median real-width estimate (mm) per replayed band, from the websocket 'width' messages."""
    w, rows = run.window, {}
    widths = [m for m in read_jsonl(run.ws_path) if m["type"] == "width" and m.get("mm") is not None]
    for label, (lo, hi) in (("flow 60", w.flow60), ("flow 140", w.flow140), *[(f"baseline {i}", b) for i, b in enumerate(w.baselines)]):
        if hi > lo:
            v = [m["mm"] for m in widths if run.live(lo) <= m["wall_ms"] < run.live(hi)]
            rows[label] = {"n": len(v), "median_mm": round(float(np.median(v)), 3) if v else None}
    return rows


def summarize(run, spec, summary_name="summary.json"):
    """All checks for the run; returns (problems, report). The report is also written to the run folder as `summary_name`."""
    report = {"scenario": run.name, "dir": str(run.dir), "broker_port": run.broker_port,
              "models_at_start": run.models_at_start,
              "websocket_messages": run.seen, "posts": run.posts, "errors": run.errors}
    problems = list(run.errors) + check_safety(run) + (check_traffic(run) if run.tap_counts else []) + staleness(run)
    problems += check_pipeline_stats(run, len(requests_seen(run)))
    report["pipeline"] = {k: run.pipeline_api.get(k) for k in ("clock_offset_ms", "gate_open", "stats", "errors", "models")}
    cmp = compare_widths(run)
    report["widths_vs_offline"] = {k: v for k, v in cmp.items() if k != "problems"}
    problems += cmp["problems"]
    if run.name == "smoke":
        problems += check_no_model(run)
    else:
        report["estimates"] = estimate_report(run)
        mode = "auto" if spec["auto"] else "advise"
        found, adv = check_advice(run, mode, spec["auto"] and not spec["send"])
        report["advice"] = adv
        problems += found
        if spec["send"]:
            problems += check_auto(run)
            report["requests"] = [r["payload"]["correction"] for r in requests_seen(run)]
        else:
            problems += check_no_requests(run)
    run.notes += [n for n in drift_notes(run) if n not in run.notes]
    report["notes"] = run.notes
    report["problems"] = problems
    write_json(run.dir / summary_name, report)
    return problems, report


def main(argv=None):
    assert_loopback()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # so the children are stopped when a run is cancelled
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenario", choices=[*SCENARIOS, "all", "analyze"],
                    help="analyze DIR: check a finished run's folder again with the current checks")
    ap.add_argument("folder", nargs="?", help="for analyze: the run folder")
    ap.add_argument("--minutes", type=float, help="replay only this many minutes (a short run)")
    ap.add_argument("--out", help="output folder (default: data/e2e/ in the repo, which is gitignored)")
    ap.add_argument("--rgb", action="store_true", help="replay the RGB previews too")
    args = ap.parse_args(argv)
    if args.scenario == "analyze":
        run = load_run(args.folder)
        problems, report = summarize(run, SCENARIOS[run.name], "analyze_summary.json")
        print(json.dumps({k: v for k, v in report.items() if k != "posts"}, indent=1, default=str))
        log(f"{run.name}: {'FAIL' if problems else 'PASS'}" + "".join(f"\n  - {p}" for p in problems))
        return 1 if problems else 0
    names = ["advice", "dry", "auto"] if args.scenario == "all" else [args.scenario]
    if any(n != "smoke" for n in names) and not models_ready():
        raise SystemExit("the model scenarios need app/models/camera_model.json and printer_model.json (fit_camera / fit_printer write them)")
    failed = False
    for name in names:
        run = run_scenario(name, args.minutes, args.out, args.rgb)
        problems, report = summarize(run, SCENARIOS[name])
        print(json.dumps({k: v for k, v in report.items() if k != "posts"}, indent=1, default=str))
        failed |= bool(problems)
        log(f"{name}: {'FAIL' if problems else 'PASS'}" + "".join(f"\n  - {p}" for p in problems))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
