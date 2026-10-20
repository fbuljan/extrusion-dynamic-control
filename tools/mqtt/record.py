"""Read-only recorder: telemetry as JSONL, RGB previews as JPEG + metadata index.

IR previews: metadata only, the image is dropped unread (organiser rule: IR images are never persisted).
Usage: python tools/mqtt/record.py --seconds 300 [--out data/captures/NAME]
"""
import argparse
import base64
import json
import os
import time

from common import ALL_TOPICS, REPO_ROOT, make_client, settings

p = argparse.ArgumentParser()
p.add_argument("--seconds", type=float, default=300)
p.add_argument("--out", default=os.path.join(REPO_ROOT, "data", "captures", time.strftime("%Y%m%d-%H%M%S")))
args = p.parse_args()
cfg = settings()

os.makedirs(os.path.join(args.out, "rgb"), exist_ok=True)
telemetry_f = open(os.path.join(args.out, "telemetry.jsonl"), "a")
preview_f = open(os.path.join(args.out, "previews.jsonl"), "a")
counts = {}


def on_connect(client, userdata, flags, reason_code, properties):
    print("CONNACK", reason_code, flush=True)
    for t in ALL_TOPICS:
        client.subscribe(t, qos=0)


def on_message(client, userdata, msg):
    if msg.retain:
        return
    recv_ms = int(time.time() * 1000)
    counts[msg.topic] = counts.get(msg.topic, 0) + 1
    if msg.topic.endswith("/telemetry"):
        telemetry_f.write(json.dumps({"recv_ms": recv_ms, "msg": json.loads(msg.payload)}) + "\n")
        return
    m = json.loads(msg.payload)
    jpeg = m.pop("jpeg_base64", None)
    m["recv_ms"] = recv_ms
    m["topic"] = msg.topic
    if m.get("camera_kind") == "rgb" and jpeg:
        fname = f"{m['role']}_{m['sequence']}_cam{m['camera_index']}.jpg"
        with open(os.path.join(args.out, "rgb", fname), "wb") as f:
            f.write(base64.b64decode(jpeg))
        m["file"] = "rgb/" + fname
    preview_f.write(json.dumps(m) + "\n")


client = make_client("rec", cfg["user"], cfg["password"])
client.on_connect, client.on_message = on_connect, on_message
client.connect(cfg["host"], cfg["port"], keepalive=30)
print("recording to", os.path.abspath(args.out), flush=True)
client.loop_start()
time.sleep(args.seconds)
client.loop_stop()
client.disconnect()
telemetry_f.close()
preview_f.close()
print("done", json.dumps(counts, indent=1), flush=True)
with open(os.path.join(args.out, "DONE"), "w") as f:
    f.write(json.dumps(counts))
