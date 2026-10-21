# Extrusion width monitoring — Siemens × LEAM hackathon (Munich, 20–22 Oct 2026)

Team instructions for humans and AI agents. Personal notes go in `CLAUDE.local.md` (gitignored).

## Mission

A miniaturised delta 3D printer (Duet board, LEAM's DEMEX process-control module) has RGB and IR cameras
around the nozzle. We build an edge app that **measures the extruded bead width in mm, with a confidence value,
in real time**, compares it to target and flags deviations. It runs as a Docker app packaged as a
Siemens Industrial Edge `.app`, and talks MQTT.

**End state: everything runs on the edge device** (ingest, measurement, alerts, dashboard, logging, control).
Laptops are for development against recordings; anything that only works on a laptop isn't done.

**Team milestones I–IX: `docs/milestones.md`** (current one marked there).

LEAM's levels — work in this order:
1. **Measure** — width vs an independent reference. A reliable measurement prototype is a complete result.
2. **Correct** — gradual flow feedback, no oscillation.
3. **Adapt** — learn from recurring corners.

## Key facts (details in `docs/`)

- RGB frames are 640×480 JPEG at **~0.06 mm/px** (varies across the frame — use the per-camera calibration
  homography, not a constant). 0.1 mm ≈ 1.5–2 px, so edges need subpixel accuracy.
- Since 20 Oct ~13:30 the broker runs **trailing-only**: RGB and IR previews only on the `trailing` topics, ≤ 1 frame/s
  each, and the physical camera behind it follows the travel direction — read `camera_index` every frame. (Before
  that, camera 3's `ahead`/`trailing` labels looked swapped; recheck against images in the new mode.)
- Flow readback is live in telemetry (`duet_readback.payload.extrusion_percent`, honour `extrusion_valid`).
- Writable through the writer account (request schema 0.2.0, LEAM command guide 20 Oct): **flow % (0–200)**,
  **pressure advance (0–1 s)**, **nozzle target (190–250 °C)**, **speed override (10–200 %)**, **part-cooling fans
  (0–100 %)**. One accepted request per second shared by all operations and all senders; only while LEAM's operator has
  native DEMEX in RUN; speed needs Motion PI and fans need Cooling PI disabled there. Bed and hotend fan are read-only.
- Subscribe to `demex/demo/mini/#` or the exact topics in `tools/mqtt/common.py` (`demex/#` is refused).
- Apps on the edge device reach the broker directly; a published container port is reachable from the event network
  without the device login (verified with `tools/edge/mqtt-probe`, port 18080).

## Hard rules

- **IR images are never persisted by the app** — no files, logs, caches, datasets, screenshots; it processes them in memory
  only. The one exception is the team's local calibration recordings (D10/D13): `data/` on a laptop, never in git.
  RGB frames and scalar telemetry may be recorded. (Organiser rule.)
- **Nothing is sent to the printer** without the team lead's explicit go-ahead. Default mode is detect and advise.
- **This repo is public.** No credentials, broker addresses, organiser documents or calibration files in git.
  Config comes from `.env` (gitignored; template in `.env.example`). Recordings live in `data/` (gitignored).
- Don't change the machine's networking, start prints, or publish on the organisers' topics.

## Layout

| Path | What |
|---|---|
| `app/` | **The dashboard app** (runs on the edge device): FastAPI + MQTT bridge (`server/`), pages in `static/` — `index.html` (IR primary, RGB inset, metrics, flow/PA controls) and `monitor.html` (IR, all topics, live graphs). IR shown raw, unprocessed. Device port 18081 |
| `tools/mqtt/` | `probe.py` (who's talking, how fast), `record.py` (save telemetry + RGB), `replay.py` (play a recording onto a local broker) |
| `app/server/` width loop | `width.py` (IR px) → `camera_model.py` (real mm) → `estimator.py` → `controller.py` (+ `printer_model.py`, `control_config.py`) → `pipeline.py`; how it works and how to run it: `docs/prototype.md` |
| `tools/model/` | `width_check.py` (widths per frame), `dataset.py` (frames + bands + go/no-go gate), `fit_camera.py`, `fit_printer.py` → `app/models/*.json` (gitignored) |
| `tools/sim/` | `plant.py` (simulated printer + sensor + gateway for controller tests), `fake_gateway.py` (loopback-only stand-in for LEAM's gateway) |
| `tests/` | `.venv/bin/python -m pytest tests -q`; tests needing recordings skip without `data/` |
| `docs/` | Team knowledge, index in `docs/README.md`: `SOURCE_OF_TRUTH.md`, `mqtt-and-telemetry.md`, `cameras-and-calibration.md`, `control.md`, `edge-deployment.md`, `decisions.md`, `experiments.md`, `prototype.md` |
| `.claude/skills/` | Shared agent skills: `experiment`, `fan-out` |
| `data/` | Local recordings and calibration files — gitignored |

## How we work — hackathon mode

- **Thin slice first:** frames in → width out → alert → running as `.app` on the device. Polish later.
  Package a hello-world `.app` early; deployment is where late surprises come from.
- **Experiment freely, write it down:** the `experiment` skill — hypothesis and timebox first, verdict after,
  logged in `docs/experiments.md`. A written-down failure is progress.
- **Prove on real data** and name the highest rung reached: 1 recorded frames offline · 2 live broker, read-only ·
  3 container locally · 4 `.app` on the edge device.
- **Iterate locally, deploy at milestones:** one device deploy takes ~4–5 min, a local reload seconds (D8).
- **Decisions** that affect others go in `docs/decisions.md`, one line of why.
- Small commits, Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`), pull before you push.
