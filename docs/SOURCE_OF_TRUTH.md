# SOURCE OF TRUTH — extrusion width monitoring (Siemens × LEAM hackathon)

> **Version:** v2 (2026-10-20, day 1 afternoon). Publish-safe team edition: no addresses, accounts, passwords, URLs or
> organiser documents. Connection details come from `.env` (ask the team).
> **Purpose:** the single reference for the team and every AI agent. When sources disagree, the order is
> **measured on live data > organiser documents > team briefing > assumptions**. Details live in the topic docs listed
> in §12; this file points to them.

---

## 0. How to use this document (for AI agents)

1. Read §1, §3 and §11 first.
2. **Priority:** Level 1 (Measure) before Correct and Adapt, unless the user explicitly asks otherwise.
3. Tags:
   - **[CONFIRMED]**: stated by LEAM or the organisers, or by the team lead.
   - **[MEASURED]**: measured by us on live data on 2026-10-20: a 20 s probe (12:12 CEST) and a 5 min recording
     `rec1` (12:14:40–12:19:40 CEST, Benchy mid-print).
   - **[ASSUMPTION]**: our interpretation, to be checked.
   - **[BACKGROUND]**: general knowledge (Siemens docs, literature).
   - **[OPEN]**: unknown or contradictory (§11).
4. Never put credentials, addresses or URLs in this repo. Never invent them.
5. Units: mm, s, °C. **Camera numbering:** MQTT `camera_index` k = 0..3; physical camera and calibration map = k+1.

---

## 1. TL;DR

- **Domain:** LEAM's DEMEX process-control module for large-format extrusion 3D printing. The hackathon rig is a
  **miniaturised delta FDM printer with a Duet controller**, with 4 RGB + 4 IR cameras on the print head.
- **Task:** measure **bead width in mm with a confidence value**, report the **error against an independent
  reference**, and flag deviations from target (too narrow matters most). A dependable measurement alone already counts
  as a full result for LEAM.
- **Data:** one MQTT broker on the printer's network: telemetry JSON (~14/s) and JPEG previews inside JSON (RGB 640×480,
  IR 160×120). Read-only account; **wildcard subscriptions are refused**.
- **RGB scale ≈ 0.06 mm/px** (0.052–0.083, varies 1.4–1.5× across the frame; the briefing's 0.006 was a decimal slip).
  Use the per-camera homography; it **over-reads 4–11 % near the nozzle**, so correct the scale with the known 1.5 mm
  infill spacing. 0.1 mm ≈ 1.2–1.9 px: subpixel edges needed.
- **Frames (rec1):** only RGB camera index 3 streams (~96 % of frames; index 1 never). Defocused (σ ≈ 0.23 mm). The fresh
  line is measurable in ~20 % of frames (~1.2/s). **First widths ≈ 0.9–1.0 mm, ±0.12–0.16 mm per frame.** 0.1 mm not
  demonstrated.
- **Cameras:** telemetry names an `ahead` and a `trailing` camera; derived azimuth of index k ≈ (315 + 90k)°.
  **[OPEN]** for camera 3, `trailing` frames show the area ahead of the nozzle and `ahead` frames show the fresh bead:
  pick frames by **travel direction + image evidence**.
- **Levers:** only **`flow_percent` (0–200 %)** and **`pressure_advance_seconds` (0–1 s)**, absolute, **≤ 1 correction/s
  across both**, via a separate writer account (not issued yet). **Nozzle temperature and motor RPM are not writable.**
  The output gate was `SAFE` all day: no commands possible yet.
- **IR:** 160×120, ~0.6 mm/px. **Never persist IR images** (organiser rule). Fresh-material gate at most.
- **Platform:** Docker → `.app` (`tools/edge/build-app.sh`, tiap) → IEM 2.3.2 (V2) → our device, a SIMATIC IPC BX-39A.
  **A hello-world `.app` imported and installed (~13:00).** Open: a device UI login (the app's page answers 401) and
  whether a container on the device reaches the broker.

---

## 2. Problem description

### 2.1 Context [CONFIRMED]
- Siemens × LEAM hackathon, Munich, 20–22 Oct 2026. LEAM Technologies (Munich, TUM spin-off) makes DEMEX: a retrofit
  ring around the nozzle of large-format extruders with LED heating, active cooling and 360° infrared sensing, running on
  Siemens Industrial Edge.
- The rig here is a small delta printer; the real target machines are large-format (robot or gantry cells, beads of
  several mm). Keep every machine-specific value in configuration.

### 2.2 The problem (paraphrased)
- Build an AI-powered edge application that estimates bead width (or wall thickness) during printing from thermal
  data, camera images and/or machine data, compares it with the intended target, shows trends, flags deviations,
  estimates its own confidence and helps operators tune extrusion settings.
- Why: bead geometry depends non-linearly on extrusion rate, speed, material and temperature; operators tune by test
  prints and experience; real prints can run for two weeks while room temperature drifts; multi-wall parts get gaps.
- The challenge owner's emphasis: know the width at all times and flag when it leaves the allowed band, especially when
  it is **too narrow**. Regulation is a bonus.

---

## 3. Objectives and priorities

### 3.1 LEAM's three levels [CONFIRMED, paraphrased]

| Level | Name | Build | Evidence | Status (day 1) |
|---|---|---|---|---|
| 1 | **Measure** | width in mm with a confidence value | error against an independent reference measurement | Bead found and measured offline on rec1; no live app, no reference yet |
| 2 | **Correct** | gradual feedback on extrusion flow | smaller width error, **no oscillation** | Designed (`control.md`); blocked: gate SAFE, no writer account |
| 3 | **Adapt** (stretch) | learn the extrusion dynamics at recurring corners | improvement on later, comparable corners | Designed (`control.md`) |

Data collection is part of every level: log widths, telemetry and RGB frames (allowed) for validation, step tests and
learning. **IR images never.**

### 3.2 Why measurement first
Alarms need an explainable number, control needs a measured variable, models need labels. For alarms robustness
matters as much as the last decimal; for control and labels, bias matters.

### 3.3 Success criteria
- [ ] Live width (mm) + confidence per frame, smoothed over ~1 s.
- [ ] Reference error: bias and spread vs caliper/microscope on printed samples. Target ≤ 0.1 mm (LEAM said 0.1 mm
  should be enough; tolerance vs precision is [OPEN]).
- [ ] Flags `TOO_NARROW` / `TOO_WIDE` / `NO_MEASUREMENT` with configurable limits and debouncing.
- [ ] Runs as `.app` on the edge device (rung 4), with a plain-Docker fallback.
- [ ] (L2) Flow correction without oscillation, advisory mode first. (L3) Smaller error on later comparable corners.

---

## 4. Physical setup and sensors

### 4.1 Machine

| Item | Value | Tag |
|---|---|---|
| Kinematics / controller | miniaturised delta; Duet (RepRapFirmware), version unknown | CONFIRMED / MEASURED |
| Output gate | `SAFE`, outputs not permitted, all of rec1 | MEASURED |
| Print in rec1 | Benchy G-code at **50 % speed override**; one layer (Z ≈ 18) of **±45° infill at 1.500 mm pitch** | MEASURED |
| Speeds | 7.5 mm/s printing (15 programmed × 50 %), 18.8 on curves, 75 travel (no extrusion) | MEASURED |
| Line width | **~0.9–1.0 mm** (about twice a 0.4 mm nozzle's line) | MEASURED |
| Nozzle, layer height, target width, tolerance, material | unknown; translucent clear/grey lines, black walls; IR melt ~141 °C median | OPEN |

### 4.2 Cameras

| | RGB | IR |
|---|---|---|
| Count / mount | 4, on the head, moving with it | 4, same |
| Image | 640×480 JPEG (q91) | 160×120 colour-mapped JPEG (colour scale per frame, no per-pixel °C) |
| Scale | 0.052–0.083 mm/px; field ~40×30 mm | ~0.6 mm/px |
| Streamed (rec1) | **index 3 in ~96 % of frames**, 0 and 2 briefly, **1 never** | all four, ~2 Hz per topic |
| Our use | width | fresh-material gate at most; **never persist** |

Two cameras per modality are published at a time (`ahead`, `trailing`). Details, including the camera-3 conflict and
the frame-selection rule: `cameras-and-calibration.md` and `mqtt-and-telemetry.md`.

### 4.3 Camera selection and the camera-3 conflict
- **Certain:** `camera_index` k ↔ calibration `rgb(k+1)` (camera uuid in the frame id = uuid in the calibration file).
- **Selection rule [MEASURED, 99.9 % fit]:** azimuth of index k = (315 + 90k) mod 360° CCW from machine +X; `ahead` =
  nearest the travel direction, `trailing` = opposite. This is the organisers' labelling; mount angles unconfirmed.
- **Labels agree with telemetry [MEASURED]**, but **camera 3's view is swapped [MEASURED]**: its `trailing` frames show
  the area ahead of the nozzle, its `ahead` frames the fresh bead. Indices 0 and 2 looked consistent (~4 s of data).
  **[OPEN]**. Rule: travel direction + image evidence (camera 3: head moving toward −x−y).

### 4.4 What the frames show, calibration, accuracy [MEASURED unless tagged]
- Nozzle = orange-rimmed disc in the top-left corner (camera 3 rim exit ~(108, 53) px); the fresh line runs at ~44°
  toward the bottom-right. Defocus σ ≈ 0.23 mm; red LED cast; glints add 0.3–0.4 mm; neighbouring lines 1.5 mm away.
- Calibration: per camera a raw-pixel → plane-mm homography (no lens model), mm/px maps and a coverage map. 44–53 % of
  each frame is extrapolated; the nozzle region is not validated. Organiser residual 0.157–0.260 mm RMS on 5 mm edges.
  Files go in `data/calibration/` (gitignored).
- Through the homography the 1.5 mm pitch reads +4..11 % too wide next to the nozzle → divide by 1.04–1.11 until
  reference coupons give a per-camera factor.
- Estimator accuracy on synthetic beads is < 0.01 mm RMS: **not the bottleneck**. Focus, one streaming camera and
  calibration bias are. Expect ~0.15–0.2 mm per frame now. Full budget: `cameras-and-calibration.md`.

### 4.5 IR
Too coarse for width (~2 px across a 1 mm line). Process in memory only; never write IR images anywhere. LEAM's
telemetry already carries IR-derived melt and previous-layer temperatures (valid 35 % / 65 %) and an IR ridge line; none
is a width.

---

## 5. Controllable and measurable parameters

### 5.1 Levers [CONFIRMED]
`flow_percent` 0–200 % and `pressure_advance_seconds` 0–1 s, absolute, ≤ 1 correction/s across both, separate writer
account, command topic `demex/hackathon/mini/extrusion/request` (payload schema unknown). Flow is the width lever
(estimated actuation delay 0.4–1.2 s plus camera transport). A pressure-advance change probably pauses motion
[ASSUMPTION from firmware source]: use rarely. Nozzle temperature, motor RPM and speed are **not** writable. Room
temperature: not available. Design and safety: `control.md`.

### 5.2 Telemetry [MEASURED]
Position (mm, ~4.6 Hz content), commanded speed and override, extrusion state, travel direction, camera pair,
IR-derived temperatures (only when the inner `valid` is true), output gate. **Not present:** nozzle temperature, RPM,
pressure, layer number, room temperature, flow/PA readback, any width. Field paths and what not to use:
`mqtt-and-telemetry.md`.

---

## 6. Platform and deployment

- **Our device [MEASURED]:** SIMATIC IPC BX-39A, x86-64, Edge OS 3.3.0, no GPU allocation, on the same subnet as the
  broker. IEM is a Siemens-hosted cloud instance, version 2.3.2 with the V2 UI.
- **Works [MEASURED]:** `tools/edge/build-app.sh` → amd64 image → `.app` via the third-party packager `tiap` → IEM
  import (~45 s) → install job on our device (< 1 min; warning "Application is not signed", expected). No Siemens
  download needed. `tools/edge/iectl.sh` is ready if we ever get Siemens' IECTL.
- **Open:** the app's page behind the device's reverse proxy answers 401 until we have a device UI login; container →
  broker reachability and NTP untested.
- Constraints: compose 2.4, `mem_limit`, semver tags, restart policy, reverse proxy instead of ports, amd64, no GPU.
  Playbook: `edge-deployment.md`.

```
[cameras + Duet/gateway] → MQTT broker (printer network, read-only) → [edge device: our .app]
  telemetry + RGB → frame gate → width (H + scale fix) + confidence → flags → log → UI (reverse proxy)
  → (L2/L3) controller → writer account → flow_percent / PA (≤ 1/s) → gateway  [only in RUN, with team lead OK]
```

---

## 7. Communication: MQTT

- MQTT 3.1.1, plain TCP, no TLS, clean session, QoS 0, unique client ID. Host, port, user, password: **`.env`** (ask the
  team). You must be on the printer's network segment; the general venue Wi-Fi can't reach the broker.
- Five read topics: `demex/demo/mini/telemetry` and `demex/demo/mini/preview/{rgb,ir}/{ahead,trailing}`. Exact names
  only (`tools/mqtt/common.py`). The app subscribes to telemetry + RGB.
- Rates changed between sessions (RGB ~0.6–1.5/s in the probe, ~3/s per topic averaged in rec1, ~6/s while camera 3
  holds a role, 0 while camera 1 does). Re-measure with `tools/mqtt/probe.py`.
- Rules: drop retained, expired, older-session and older-revision messages; `sequence` is shared across all topics and
  has gaps (order only); align frames to telemetry by source time; RGB flows only while the organisers' RGB viewer is
  open. Full schema and timing: `mqtt-and-telemetry.md`.
- Our output: the reader account cannot publish and we don't use organiser topics, so results go to the app UI and a
  local log (proposed fields: width, uncertainty, confidence, status, camera, deposit position, travel angle, speed,
  override, scale factor, reference error).

---

## 8. Plan

1. **Level 1 (P0):** the 12-step pipeline in `cameras-and-calibration.md` (ingest → align → gate → anchor → profiles →
   three edge estimators → homography + scale fix → prior → confidence → 1 s median → flags → validate on coupons).
   Prototype code exists outside the repo (ask the team); port it into the app.
2. **Logging:** per sample the width profile, confidence, camera, deposit position and arc length, speed, override,
   program, Z bucket, valid IR scalars, commands and readbacks; RGB frames and telemetry via `tools/mqtt/record.py`.
3. **Level 2 (P1):** gated incremental integral controller on flow only, advisory first (`control.md`). First on-site
   step: flow 100 → 115 % → 100 % on a straight, with the organisers' OK.
4. **Level 3 (P2):** position-keyed flow offsets per recurring corner + one pressure-advance value changed between
   layers (`control.md`).
5. **Delivery:** thin slice first: frames in → width out → flag → UI → `.app` on the device. Rungs: 1 recorded frames ·
   2 live broker read-only · 3 container locally · 4 `.app` on the device (reached for hello-world install). Offline
   loop: `tools/mqtt/replay.py`.

---

## 9. Decisions
Canonical log: `decisions.md` (D1 measure first · D2 measurement is the foundation · D3 MQTT + Industrial Edge `.app` ·
D4 levers are flow % and pressure advance only · D5 nothing secret in git · D6 hackathon mode).
**Proposed, awaiting the team lead:** frame selection by direction + image evidence; homography + pitch scale fix until
coupons exist; app subscribes to telemetry + RGB only; advisory by default, PA rarely; output a width profile along the
path.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Defocus limits accuracy | Ask the organisers to refocus; robust estimators; average over ~1 s |
| Only camera 3 streams RGB; one diagonal is blind | Ask for index 1 or both cameras of a pair; aggregate over time |
| Camera-3 label/view swap | Direction + image evidence |
| Homography bias 4–11 % near the nozzle | Pitch self-calibration, then reference coupons |
| Glare, LED, neighbouring lines | Saturation gate, blue channel, width prior, nozzle anchor, method agreement |
| RGB stops when the organisers' viewer closes | Freshness monitor; ask |
| Command gate SAFE; PA pauses the print | Advisory mode; flow only |
| No device UI login; container → broker untested | Ask Siemens; probe container on the device; plain-Docker Plan B |
| IR stored by accident | App doesn't subscribe to IR; no payload logging |
| Telemetry glitches, transient camera flips | Pair-stability gate; direction from position |
| Device CPU budget unknown, clock skew | numpy + OpenCV only; check `lscpu`; NTP |

---

## 11. Open questions (the team lead keeps the prioritised list for the organisers)

Answered since v1: camera transport (MQTT JSON + base64 JPEG), telemetry contents, levers, camera index mapping and
selection rule, RGB scale, the rig vs the real machine.

Still open:
- **Cameras:** what `ahead`/`trailing` mean for RGB (camera-3 swap); why index 1 never streams RGB; can both cameras of a
  pair stream; can focus, exposure and the red LED be changed.
- **Process:** nozzle diameter, target line width and tolerance, layer height, material; is 0.1 mm a tolerance or a
  precision goal; bead width or wall thickness.
- **Reference:** printed samples with known widths and a caliper/microscope on site; how "reference error" is judged;
  calibration board height vs nozzle tip; IR calibration status.
- **Commands:** writer account, payload schema, readback topic, SAFE → RUN, permission to actuate, does a PA change
  pause the print, firmware version, nozzle → camera-view distance.
- **Telemetry:** unit of `extrusion_speed`; meaning of `controls.healthy=false`; layer number; room temperature.
- **Edge:** device UI login, container → broker reachability, NTP, laptop access to the device's reverse proxy.

---

## 12. Where the details are

| Doc | What it holds |
|---|---|
| `README.md` | Index of `docs/` |
| `mqtt-and-telemetry.md` | Topics, schemas, measured rates, camera index/azimuth/role, timing, handling checklist |
| `cameras-and-calibration.md` | What the frames show, frame selection, calibration usage, scale fix, accuracy budget, pipeline, proven vs not |
| `control.md` | Levers, latency, Level 2 controller, Level 3 corner learner, step test, safety |
| `edge-deployment.md` | Build a `.app`, compose rules, upload/install, IECTL, networking, debugging, what's still missing |
| `decisions.md`, `experiments.md` | Team decision log and experiment log |
| `../CLAUDE.md` | Mission, hard rules, layout, ways of working |

Organiser documents, calibration files, recordings and the raw research notes stay outside the repo: ask the team.

---

## 13. Glossary
- **flow_percent:** extrusion multiplier. **Pressure advance (PA):** extra extruder motion proportional to extruder
  acceleration; compensates pressure lag at corners, starts and stops.
- **ahead / trailing:** the two cameras published per modality. **selection_revision:** bumps on every pair change.
- **H:** 3×3 raw-pixel → plane-mm homography. **Coverage class:** 0 extrapolated, 1 first pose, 2 both poses.
- **FWHM:** full width at half maximum of the across-bead profile. **SAFE / RUN:** output gate state.
- **IED / IEM / IECTL / tiap:** edge device / Edge Management / Siemens CLI / third-party `.app` packager.
- **Dead time:** delay between a command or event at the nozzle and its observation in the camera.

---

## 14. Changelog
- **v1 (2026-10-20 morning):** first draft from the team's verbal briefing (not in the repo).
- **v2 (2026-10-20 afternoon):** merged organiser material, live MQTT measurements and analyses. RGB scale ~0.06 mm/px
  with homography and near-nozzle correction; levers = flow and pressure advance; LEAM's three levels; telemetry and
  camera selection from live data; camera-3 conflict; first widths and accuracy budget; hello-world `.app` installed on
  our device.
