# Extrusion width monitoring — Siemens × LEAM hackathon (Munich, 20–22 Oct 2026)

Team instructions for humans and AI agents. Personal notes go in `CLAUDE.local.md` (gitignored).

## Mission

A miniaturised delta 3D printer (Duet board, LEAM's DEMEX process-control module) has RGB and IR cameras
around the nozzle. We build an edge app that **measures the extruded bead width in mm, with a confidence value,
in real time**, compares it to target and flags deviations. It runs as a Docker app packaged as a
Siemens Industrial Edge `.app`, and talks MQTT.

**End state: everything runs on the edge device** (ingest, measurement, alerts, dashboard, logging, control).
Laptops are for development against recordings; anything that only works on a laptop isn't done.

LEAM's levels — work in this order:
1. **Measure** — width vs an independent reference. A reliable measurement prototype is a complete result.
2. **Correct** — gradual flow feedback, no oscillation.
3. **Adapt** — learn from recurring corners.

## Key facts (details in `docs/`)

- RGB frames are 640×480 JPEG at **~0.06 mm/px** (varies across the frame — use the per-camera calibration
  homography, not a constant). 0.1 mm ≈ 1.5–2 px, so edges need subpixel accuracy.
- Telemetry names an `ahead` and a `trailing` camera, but RGB comes almost only from camera index 3, and for it the
  `ahead` topic shows the fresh bead. Pick frames by travel direction + image evidence, not by topic name.
- The only writable levers are **flow % (0–200)** and **pressure advance (0–1 s)**, max one correction per second,
  through a separate writer account. Nozzle temperature and motor speed are **not** controllable.
- The broker refuses wildcard subscriptions — name the five topics exactly (`tools/mqtt/common.py`).

## Hard rules

- **IR images are never persisted** — no files, logs, caches, datasets, screenshots. Process them in memory only.
  RGB frames and scalar telemetry may be recorded. (Organiser rule.)
- **Nothing is sent to the printer** without the team lead's explicit go-ahead. Default mode is detect and advise.
- **This repo is public.** No credentials, broker addresses, organiser documents or calibration files in git.
  Config comes from `.env` (gitignored; template in `.env.example`). Recordings live in `data/` (gitignored).
- Don't change the machine's networking, start prints, or publish on the organisers' topics.

## Layout

| Path | What |
|---|---|
| `tools/mqtt/` | `probe.py` (who's talking, how fast), `record.py` (save telemetry + RGB), `replay.py` (play a recording onto a local broker) |
| `docs/` | Team knowledge, index in `docs/README.md`: `SOURCE_OF_TRUTH.md`, `mqtt-and-telemetry.md`, `cameras-and-calibration.md`, `control.md`, `edge-deployment.md`, `decisions.md`, `experiments.md` |
| `.claude/skills/` | Shared agent skills: `experiment`, `fan-out` |
| `data/` | Local recordings and calibration files — gitignored |

## How we work — hackathon mode

- **Thin slice first:** frames in → width out → alert → running as `.app` on the device. Polish later.
  Package a hello-world `.app` early; deployment is where late surprises come from.
- **Experiment freely, write it down:** the `experiment` skill — hypothesis and timebox first, verdict after,
  logged in `docs/experiments.md`. A written-down failure is progress.
- **Prove on real data** and name the highest rung reached: 1 recorded frames offline · 2 live broker, read-only ·
  3 container locally · 4 `.app` on the edge device.
- **Decisions** that affect others go in `docs/decisions.md`, one line of why.
- Small commits, Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`), pull before you push.
