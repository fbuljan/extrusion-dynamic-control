"""Read-only MQTT probe: connect, subscribe, summarise traffic per topic.

Usage: python tools/mqtt/probe.py [--seconds N] [--topic T ...]
Never publishes. IR payloads are summarised in memory and never written (organiser rule).
"""
import argparse
import json
import time
from collections import defaultdict

from common import ALL_TOPICS, make_client, settings

p = argparse.ArgumentParser()
p.add_argument("--seconds", type=float, default=20)
p.add_argument("--topic", action="append", help="override the topic list (repeatable)")
args = p.parse_args()
topics = args.topic or ALL_TOPICS
cfg = settings()

stats = defaultdict(lambda: {"n": 0, "bytes": 0, "first": None, "last": None, "retained": 0, "keys": None})
acks = {}


def summarise(obj, depth=0):
    if not isinstance(obj, dict) or depth > 1:
        return type(obj).__name__
    return {k: ("<base64 %d chars>" % len(v) if k.endswith("base64") and isinstance(v, str) else summarise(v, depth + 1))
            for k, v in obj.items()}


def on_connect(client, userdata, flags, reason_code, properties):
    acks["connack"] = str(reason_code)
    if reason_code.is_failure:
        return
    for t in topics:
        userdata[client.subscribe(t, qos=0)[1]] = t


def on_subscribe(client, userdata, mid, reason_codes, properties):
    acks.setdefault("suback", {})[userdata.get(mid, mid)] = str(reason_codes[0])


def on_message(client, userdata, msg):
    now = time.time()
    s = stats[msg.topic]
    s["n"] += 1
    s["bytes"] += len(msg.payload)
    s["first"] = s["first"] or now
    s["last"] = now
    s["retained"] += int(msg.retain)
    if s["keys"] is None:
        try:
            s["keys"] = summarise(json.loads(msg.payload))
        except ValueError:
            s["keys"] = "non-JSON, first bytes: %r" % msg.payload[:40]


client = make_client("probe", cfg["user"], cfg["password"])
client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
print(f"connecting to {cfg['host']}:{cfg['port']} as {cfg['user']} ...")
try:
    client.connect(cfg["host"], cfg["port"], keepalive=30)
except OSError as e:
    raise SystemExit(f"TCP connect failed: {e} (are you on the printer's network?)")
client.loop_start()
time.sleep(args.seconds)
client.loop_stop()
client.disconnect()

print("CONNACK:", acks.get("connack", "none received"))
print("SUBACK:", json.dumps(acks.get("suback"), indent=1))
for topic, s in sorted(stats.items()):
    span = max(s["last"] - s["first"], 1e-9)
    rate = (s["n"] - 1) / span if s["n"] > 1 else 0
    print(f"\n{topic}: {s['n']} msgs, ~{rate:.2f}/s, avg {s['bytes'] // s['n']} B, retained {s['retained']}")
    print("  shape:", json.dumps(s["keys"])[:1500])
if not stats:
    print("no messages received")
