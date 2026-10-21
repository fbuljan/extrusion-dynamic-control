# extrusion-dynamic-control

Real-time extrusion bead-width measurement for LEAM's DEMEX printer, running as a Siemens Industrial Edge app.
Built at the Siemens × LEAM hackathon, Munich, October 2026.

Status: dashboard app working on the edge device (live IR, all topics + graphs, printer corrections); width measurement
next. See `CLAUDE.md` for the mission, rules and layout.

## Dashboard app (`app/`)

```sh
cd app && uv venv && uv pip install -r requirements.txt
set -a && . ../.env && set +a                       # broker host/users; passwords can also be entered in the UI
.venv/bin/uvicorn server.main:app --port 8765       # open http://127.0.0.1:8765 (main) and /monitor.html
tools/edge/build-app.sh app 0.1.0                    # from the repo root → app/dist/hello-edge_0.1.0.app for the device
```

On the edge device it listens on port 18081. Passwords are entered once under Settings and kept on the device.
Corrections follow LEAM's request schema 0.2.0 (flow, pressure advance, nozzle target, speed override, part-cooling
fans; see `docs/control.md`) and need `MQTT_CONTROLLER_ID` in `.env`. Only one person should send at a time.

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
