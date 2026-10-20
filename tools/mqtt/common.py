"""Shared MQTT settings for the tools. Values come from the environment or the repo-root .env (never committed)."""
import os
import uuid

import paho.mqtt.client as mqtt

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

TELEMETRY = "demex/demo/mini/telemetry"
PREVIEWS = [f"demex/demo/mini/preview/{kind}/{side}" for kind in ("rgb", "ir") for side in ("ahead", "trailing")]
# The broker refuses wildcard subscriptions; topics must be named exactly.
ALL_TOPICS = [TELEMETRY] + PREVIEWS


def load_env(path=os.path.join(REPO_ROOT, ".env")):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def settings():
    load_env()
    missing = [k for k in ("MQTT_HOST", "MQTT_USER", "MQTT_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)}: copy .env.example to .env and fill it in")
    return {
        "host": os.environ["MQTT_HOST"],
        "port": int(os.environ.get("MQTT_PORT", "1883")),
        "user": os.environ["MQTT_USER"],
        "password": os.environ["MQTT_PASSWORD"],
    }


def make_client(prefix, user=None, password=None):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
                         clean_session=True, protocol=mqtt.MQTTv311, userdata={})
    if user:
        client.username_pw_set(user, password)
    return client
