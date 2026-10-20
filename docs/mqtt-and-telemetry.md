# MQTT and telemetry

Everything the app needs arrives on one MQTT broker on the printer's network: a telemetry snapshot about 14 times a
second, plus camera previews (JPEG inside JSON). Our reader account can only read; corrections would go through a
separate writer account (contract in `control.md`).

## Current mode — organiser guide update, 20 Oct ~13:30 (overrides the measurements below where they differ)

- **Trailing-only mode.** Only `preview/rgb/trailing` and `preview/ir/trailing` publish, **at most 1 frame/s each**
  (newest eligible frame only, no backlog). The `ahead` topics are inactive. The physical camera behind the trailing
  topic changes with travel direction — never assume a fixed camera; read `camera_index` (physical number = index + 1).
- **Wildcard:** `demex/demo/mini/#` is allowed (verified); only the broader `demex/#` is refused.
- **Flow readback (new):** `sources.duet_readback.payload.extrusion_multiplier` (1.0 = 100 %), `.extrusion_percent`,
  `.extrusion_valid`, `.extrusion_source_utc_ns`, ~1/s. Use only when `duet_readback.valid` and `extrusion_valid` are
  true; null/false means Unknown, never 0 or 100. Verified live: 100 %, valid.
- **Other paths from the guide:** melt temperature `sources.melt.payload.temperature_c`; previous-layer temperature
  `sources.process.payload.measurement.substrate.temperature_c`; cooling output
  `sources.process.payload.pid_state.cooling.current_output`; gateway `sources.gateway.payload.state` (was SAFE all
  morning, **RUN** at 13:30).
- **Writer account** (issued, works; updated from LEAM's command guide, 20 Oct): publish
  `demex/hackathon/mini/extrusion/request`, schema `0.2.0`, five absolute-value operations (`flow_percent`,
  `pressure_advance_seconds`, `nozzle_temperature_c`, `speed_override_percent`, `fan_speed_percent`; ranges in
  `control.md`), ≤ 1 accepted request/s shared by all operations and senders. Subscribe `.../extrusion/result`
  (per-request outcome: `accepted`, `setting_confirmed`, `queued_unconfirmed`, `dispatch_unknown`, `rejected`;
  `setting_confirmed` + `observation.observed_value` is the readback for that request) and `.../extrusion/status`
  (gateway session, command gate, `highest_admitted_sequence`), both still schema `0.1.0`. Other senders share the
  controller's sequence and rate limit, so the result topic is not ours alone: match results by `request_id`. Needs
  native DEMEX in RUN, which only LEAM's operator can set. No auto-retry of uncertain results. A reader connection is
  still needed for monitoring. Envelope, sequence/expiry and result handling: `control.md` (Request contract).
- Frame lease ≤ 400 ms: a held frame may be shown (labelled) for ≤ 2 s but never measured as fresh.
- Passwords are operator-supplied and **must not be embedded in shared code**.

Earlier measurements (before the mode change): a 20 s probe at 12:12 CEST and a 5 min recording (`rec1`,
12:14:40–12:19:40 CEST, Benchy mid-print). Rates changed between the two; re-measure with `tools/mqtt/probe.py`.

## Connecting

- MQTT **3.1.1 only** (v5 unsupported), plain TCP, no TLS, clean session, QoS 0, unique client ID per connection
  (`tools/mqtt/common.py` uses `<prefix>-<8 hex>`; a duplicate ID kicks the older client off).
- Host, port, user and password: **`.env`** (template `.env.example`; values from the team). Never commit them.
- You must be on the printer's network segment. The general venue Wi-Fi cannot reach the broker. Test the TCP port, not
  ping.
- Our reader account is **read-only** (monitoring topics only). Corrections use a separate writer account, which has
  only the request, result and status permissions (so keep a reader connection for telemetry). Its password is
  operator-supplied: `.env` only.
- `demex/#` is refused (SUBACK error); `demex/demo/mini/#` or the exact names in `tools/mqtt/common.py` work.
- An app **on the edge device** reaches the broker directly (verified 20 Oct with `tools/edge/mqtt-probe`).

## Topics

| Topic | Payload | Probe (20 s) | rec1 (5 min) |
|---|---|---|---|
| `demex/demo/mini/telemetry` | JSON state snapshot, 5.2–7.3 KB | ~13.8/s | 14.1/s, event-driven |
| `demex/demo/mini/preview/rgb/ahead` | JSON + base64 JPEG 640×480, ~55 KB | ~1.5/s | 3.1/s average; ~6.4/s while camera 3 holds the role, 0 while camera 1 does |
| `demex/demo/mini/preview/rgb/trailing` | same | ~0.6/s | 2.9/s average; ~5.9/s while camera 3 holds the role |
| `demex/demo/mini/preview/ir/ahead` | JSON + base64 JPEG 160×120, ~2.5 KB | ~2/s | 1.9/s (paced at 2 Hz) |
| `demex/demo/mini/preview/ir/trailing` | same | ~2/s | 1.9/s |

- Camera 3's own frame counter runs at ~7.4 fps; we receive ~6/s of it on whichever topic it is on. JPEG quality 91.
- RGB previews only flow while the organisers keep their RGB viewer open with cameras selected. If RGB stops, ask them.
- **The app subscribes to telemetry + the two RGB topics only.** IR images must never be persisted (organiser rule), and
  we do not need them for width.
- Command topics (writer account): we publish `demex/hackathon/mini/extrusion/request` and subscribe
  `demex/hackathon/mini/extrusion/result` and `.../status` by exact name. Note the `hackathon` vs `demo` namespace: they
  are outside the `demex/demo/mini/#` wildcard. Request schema `0.2.0`, results and status `0.1.0`; the guide asks for
  the status and result subscriptions to be in place before the first publish. See `control.md`.

## Preview message

| Key | Example | Meaning |
|---|---|---|
| `schema_version` | `"0.1.0"` | check it |
| `session_id` | uuid | new value = reset all state |
| `sequence` | int | **one global counter shared with telemetry and all previews**; order only |
| `published_utc_ms` | ms epoch | publish time |
| `expires_utc_ms` | RGB: published + 400; IR: source + ~1150 | drop the frame after this |
| `selection_revision` | int | camera-pair revision at publish; must equal the telemetry revision you pair it with |
| `kind`, `media_type` | `"preview"`, `"image/jpeg"` | |
| `camera_index` | 0–3 | zero-based; physical camera and calibration map = index + 1 |
| `camera_kind`, `role` | `"rgb"`/`"ir"`, `"ahead"`/`"trailing"` | role = topic suffix |
| `source_frame_id` | RGB `"<camera uuid>:<stream uuid>:<counter>"`, IR int | opaque; the RGB uuid identifies the camera |
| `source_utc_ns` | null for RGB | not usable for RGB |
| `source_age_ms` | RGB 30–250 (median ~100); IR 740–1120 | age of the frame at publish |
| `jpeg_base64` | string | decode in memory: `cv2.imdecode(np.frombuffer(b64decode(s), np.uint8), 1)` |

**RGB capture time = `published_utc_ms − source_age_ms`.**

## Telemetry message

Envelope: `schema_version` (0.1.0), `session_id`, `sequence`, `published_utc_ms`, `expires_utc_ms` (= published +
1000), `selection_revision`, `kind: "telemetry"`, `cameras: {ahead, trailing}` (indices 0–3), and `sources`, where each
of six sources is `{source_utc_ns, payload, valid}`. Telemetry always carries the latest value of every source; each
source has its own timestamp.

| Source | Update rate | Age at publish (median) | What it is |
|---|---|---|---|
| `motion` | 9.0 Hz (position content only ~4.6 Hz) | ~50 ms | Duet position, speed, extrusion state |
| `process` | 8.2 Hz | ~60 ms | LEAM process service: direction, camera choice, IR-derived measurements |
| `melt` | 5.3 Hz | ~100 ms (up to 1.9 s) | IR temperature of the fresh melt behind the nozzle |
| `gateway` | 1.0 Hz | ~0.5 s | SAFE/RUN gate for outputs |
| `controls` | 1.0 Hz | ~0.5 s | controller health |
| `duet_readback` | 0.9 Hz | ~0.5 s | fan values |

### Fields the app uses (paths under `sources.`)

| Need | Path | Unit / notes |
|---|---|---|
| Position | `motion.payload.position_mm.{x,y,z}` | mm. Refreshes in ~1.6 mm steps at 4.6 Hz; x and y sometimes update separately. Interpolate by `source_utc_ns` |
| Speed | `motion.payload.velocity_magnitude` | mm/s, **commanded** (program feed × override), not measured: 7.5 print, 18.8 curves, 75 travel in rec1 |
| Speed override | `motion.payload.speed_override_percent` | %; 50 throughout rec1 |
| Extruding / moving | `motion.payload.extrusion_active`, `is_moving` | bool; extrusion false only on 75 mm/s travel |
| Program | `motion.payload.active_program_name` | G-code file name |
| Travel direction | `process.payload.motion_direction.{x,y}` | unit vector. `measurement.{guidance,measurement,routing}_angle_deg` are identical copies (deg, CCW from +X). 5.5 % of messages are exactly axis-aligned glitches: take direction from position over ≥ 0.3–0.5 s |
| Camera pair | `cameras.ahead`, `cameras.trailing`, `selection_revision` | `process.payload.active_camera_index` always equals `cameras.ahead` |
| Melt temperature | `melt.payload.temperature_c` | °C, IR-derived; use only if `melt.payload.valid` (true 35 %) |
| Previous-layer temperature | `process.payload.measurement.substrate.temperature_c` | °C, IR-derived; use only if `...substrate.valid` (65 %) |
| Output gate | `gateway.payload.state`, `permits_outputs`, `command_gate_closed` | `SAFE` / false / true all morning, `RUN` seen at ~13:30. Never write unless RUN / true / false (and writer status `normal_authorized` true) |

**Do not use:** `motion.payload.extrusion_speed` (no unit, noisy), `process.payload.pid_state.*` (controllers off),
`filter_status.*` booleans (constant), `controls.payload.healthy` (false 100 % while status is "OK"), fan values
(constant), the **outer** `valid` flags (true even when the inner measurement is invalid), `velocity_mm_s.z` (noise).
Invalid `melt` payloads are short (no temperature): read every field with `.get()`.

**Heater temperatures (added 20 Oct afternoon):** `sources.duet_readback.payload.temperatures.{nozzle,bed}` with
`current_c`, `active_target_c`, `state` ("active"), `valid`, `heater_index` (nozzle 1, bed 0) and `source_utc_ns`. The
whole `duet_readback` block refreshes only every ~1.4 s, so values repeat across ~20 messages, and `valid` stays true
while the reading ages (up to ~1.5 s seen) — check `source_utc_ns`. Flow multiplier readback is the same block
(`extrusion_percent`, gated on `extrusion_valid`). Not seen yet: a non-"active" state, a fault, or a setpoint change.

**Not in telemetry at all:** extruder RPM or pressure, layer number, room temperature, pressure-advance readback (only
in the writer's per-request result), any bead width. For layers, bucket Z with ≥ 0.05 mm hysteresis (Z noise is ±0.01 mm).

## Cameras: index, azimuth, role

- **Index mapping (certain):** `camera_index` k ↔ physical camera k+1 ↔ calibration map `rgb(k+1)`. The camera uuid in
  the RGB `source_frame_id` equals the `source_uuid` in that camera's calibration file.
- **Selection rule (derived from rec1, 99.9 % fit):** each camera k has an azimuth of (315 + 90k) mod 360° CCW from
  machine +X: index 0 = 315°, 1 = 45°, 2 = 135°, 3 = 225°. `ahead` = the camera whose azimuth is nearest the travel
  direction; `trailing` = the opposite one ((ahead + 2) mod 4, 97 %; it lags by up to ~350 ms after a switch). This is
  how the organisers *label* cameras; the physical mount angles are not confirmed.
- **Switching:** the pair changes every ~2.3 s on the ±45° infill. At turnarounds, and on direction glitches, there are
  short flips to cameras 0 and 2 (median 150 ms). `selection_revision` bumps on every change (~44 per minute). Accept a
  frame only if the pair was stable ≥ 500 ms before and ≥ 300 ms after its capture time (so process with a ~300 ms
  delay).
- **RGB comes almost only from camera index 3** (96 % of frames; 0 and 2 in short bursts; **index 1 never**). IR comes
  from all four. While camera 1 holds a role, that RGB topic is silent for 7–9 s.
- **Labels agree with telemetry:** preview `camera_index`/`role` matched `cameras.<role>` in 100 % of frames at the same
  `selection_revision`.
- **Open conflict, camera 3:** frames on `rgb/trailing` from index 3 show the area **ahead** of the nozzle (no line on the
  nozzle path), and frames on `rgb/ahead` show the **fresh bead** leaving the nozzle. For indices 0 and 2 the labels
  matched the view, but on only ~4 s of data. Both findings can be true: consistent labels, but for camera 3 the label
  does not describe what the image shows. Open question to the organisers; until answered, **select frames by travel direction
  plus image evidence**: camera 3 shows the fresh bead when the head moves toward −x−y (travel ≈ 225°, telemetry labels
  it `ahead`), confirmed by a line on the nozzle path. Details: `cameras-and-calibration.md`.

## Timing

| Item | Value (rec1) |
|---|---|
| Receive − publish, telemetry | p50 91 ms, p95 139 ms, floor 57 ms (our clock ≤ 57 ms ahead; no drift) |
| Receive − publish, RGB | p50 ~120 ms; 5 of 866 trailing frames arrived already expired |
| RGB age at publish | median ~100 ms |
| Telemetry inter-arrival | p50 68 ms, max 240 ms |
| Direction change → camera switch | same message in 127 of 133 switches; ~70 ms after the motion update |

Join frames to telemetry by **source time**: take the latest `motion`/`process` state with `source_utc_ns/1e6 ≤
capture time + 50 ms` and interpolate position. Joining by message time puts the state up to ~240 ms (1.8 mm) off.

## Handling checklist

- Drop `retain=True` messages (none seen, but the organisers say to reject them).
- New `session_id` → reset everything. Older `selection_revision` → drop. Keep only the newest preview per topic.
- Drop anything past `expires_utc_ms`; clear cached values on expiry or disconnect. The edge device needs NTP.
- `sequence` gaps are normal: ~36 % of numbers never reach us (shared counter, QoS 0). Never alarm on gaps.
- Process IR, if ever subscribed, in memory only: no files, logs, caches, datasets, screenshots. Do not log payloads.
- Do not publish on the organisers' topics with any account unless the team lead says so.

## Tools

`tools/mqtt/probe.py` (rates and shapes), `tools/mqtt/record.py` (telemetry JSONL + RGB JPEGs, IR metadata only),
`tools/mqtt/replay.py` (replays a recording onto a local broker with original pacing and fresh expiry stamps). A 5 min
recording (`rec1`) exists locally; ask the team for a copy.
