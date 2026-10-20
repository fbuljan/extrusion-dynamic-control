"""Long-running read-only MQTT probe for the edge device.

Logs broker reachability, CONNACK/SUBACK and per-topic message rates every 10 s (visible in the device's app logs),
and serves the latest status as JSON on :8000. Never publishes. Payloads are counted, never decoded or stored.
Without MQTT_USER it connects anonymously: a refused login still proves the network path, and it doesn't retry.
"""
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from common import ALL_TOPICS, make_client

INTERVAL_S = 10
cfg = {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", "1883")),
       "user": os.environ.get("MQTT_USER"), "password": os.environ.get("MQTT_PASSWORD")}
lock = threading.Lock()
counts = {t: 0 for t in ALL_TOPICS}
status = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "broker": f"{cfg['host']}:{cfg['port']}",
          "tcp": None, "connack": None, "suback": {}, "rates_per_s": {}, "totals": {}, "last_error": None}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def tcp_check():
    try:
        with socket.create_connection((cfg["host"], cfg["port"]), timeout=5):
            return "ok"
    except OSError as e:
        return f"fail: {e}"


def on_connect(client, userdata, flags, reason_code, properties):
    status["connack"] = str(reason_code)
    log(f"CONNACK {reason_code}")
    if reason_code.is_failure:
        log("broker answered, so the network path works; login refused, not retrying")
        client.disconnect()
        client.loop_stop()
    else:
        for t in ALL_TOPICS:
            userdata[client.subscribe(t, qos=0)[1]] = t


def on_subscribe(client, userdata, mid, reason_codes, properties):
    topic = userdata.get(mid, mid)
    status["suback"][topic] = str(reason_codes[0])
    log(f"SUBACK {topic}: {reason_codes[0]}")


def on_disconnect(client, userdata, flags, reason_code, properties):
    status["last_error"] = f"disconnected: {reason_code}"
    log(f"disconnected: {reason_code}")


def on_message(client, userdata, msg):
    with lock:
        counts[msg.topic] = counts.get(msg.topic, 0) + 1


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(status, indent=1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


threading.Thread(target=HTTPServer(("0.0.0.0", 8000), StatusHandler).serve_forever, daemon=True).start()
log(f"mqtt-probe starting, broker {status['broker']}, host {socket.gethostname()}")

while (result := tcp_check()) != "ok":
    status["tcp"] = result
    log(f"TCP {status['broker']}: {result}; retrying in {INTERVAL_S}s")
    time.sleep(INTERVAL_S)
status["tcp"] = "ok"
log(f"TCP {status['broker']}: ok")

client = make_client("edge-probe", cfg["user"], cfg["password"])
client.on_connect, client.on_subscribe = on_connect, on_subscribe
client.on_disconnect, client.on_message = on_disconnect, on_message
client.connect_async(cfg["host"], cfg["port"], keepalive=30)
client.loop_start()

totals = {t: 0 for t in ALL_TOPICS}
while True:
    time.sleep(INTERVAL_S)
    with lock:
        window = dict(counts)
        for t in counts:
            counts[t] = 0
    for t, n in window.items():
        totals[t] = totals.get(t, 0) + n
    status["rates_per_s"] = {t.split("mini/")[-1]: round(n / INTERVAL_S, 2) for t, n in window.items()}
    status["totals"] = {t.split("mini/")[-1]: n for t, n in totals.items()}
    log("rates/s " + json.dumps(status["rates_per_s"]))
