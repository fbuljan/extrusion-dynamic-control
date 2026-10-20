# extrusion-dynamic-control

Real-time extrusion bead-width measurement for LEAM's DEMEX printer, running as a Siemens Industrial Edge app.
Built at the Siemens × LEAM hackathon, Munich, October 2026.

Status: early — tooling and research. See `CLAUDE.md` for the mission, rules and layout.

## Quickstart

```sh
cp .env.example .env            # fill in the broker details (ask the team)
uv venv && uv pip install -r tools/requirements.txt   # or: python3 -m venv .venv && .venv/bin/pip install -r tools/requirements.txt

# on the printer's network:
.venv/bin/python tools/mqtt/probe.py                      # connect and summarise every topic for 20 s
.venv/bin/python tools/mqtt/record.py --seconds 300       # save telemetry + RGB frames to data/captures/<timestamp>

# anywhere — replay a recording onto a local broker (brew install mosquitto):
mosquitto -p 1884 &
.venv/bin/python tools/mqtt/replay.py data/captures/<name> --port 1884 --loop
```

Quick looks with the mosquitto CLI:

```sh
mosquitto_sub -h $MQTT_HOST -V mqttv311 -u $MQTT_USER -P $MQTT_PASSWORD -t demex/demo/mini/telemetry -C 1 | jq .cameras
```
