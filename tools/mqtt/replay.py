"""Replay a recording from record.py onto a local broker, same topics, original timing.

Start a local broker first:  mosquitto -p 1884
Then:  python tools/mqtt/replay.py data/captures/NAME [--port 1884] [--speed 1.0] [--loop]
Republishes telemetry and RGB previews (IR was never recorded). Refuses non-local brokers.
"""
import argparse
import base64
import json
import os
import time

from common import make_client

p = argparse.ArgumentParser()
p.add_argument("recording")
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=1884)
p.add_argument("--speed", type=float, default=1.0)
p.add_argument("--loop", action="store_true")
args = p.parse_args()
if args.host not in ("127.0.0.1", "localhost"):
    raise SystemExit("replay only targets a local broker")


def load_events(rec):
    events = []
    with open(os.path.join(rec, "telemetry.jsonl")) as f:
        for line in f:
            e = json.loads(line)
            events.append((e["recv_ms"], "demex/demo/mini/telemetry", e["msg"]))
    with open(os.path.join(rec, "previews.jsonl")) as f:
        for line in f:
            m = json.loads(line)
            if "file" not in m:
                continue
            with open(os.path.join(rec, m.pop("file")), "rb") as img:
                m["jpeg_base64"] = base64.b64encode(img.read()).decode()
            events.append((m.pop("recv_ms"), m.pop("topic"), m))
    return sorted(events, key=lambda e: e[0])


events = load_events(args.recording)
client = make_client("replay")
client.connect(args.host, args.port)
client.loop_start()
print(f"replaying {len(events)} messages from {args.recording} at {args.speed}x")
while True:
    t0_rec, t0 = events[0][0], time.time()
    for rec_ms, topic, msg in events:
        delay = (rec_ms - t0_rec) / 1000 / args.speed - (time.time() - t0)
        if delay > 0:
            time.sleep(delay)
        # Consumers drop messages past expires_utc_ms, so shift the stamps to now.
        shift = int(time.time() * 1000) - rec_ms
        out = dict(msg)
        for k in ("published_utc_ms", "expires_utc_ms"):
            if isinstance(out.get(k), int):
                out[k] += shift
        client.publish(topic, json.dumps(out), qos=0)
    if not args.loop:
        break
client.loop_stop()
client.disconnect()
