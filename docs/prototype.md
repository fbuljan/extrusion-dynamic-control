# Prototype: measure the bead, hold the width

Built overnight 20→21 Oct against the design in `architecture.md`. Decisions D21–D41 (`decisions.md`), the evidence in
the experiments from `ir-follows-flow-gate` on (`experiments.md`). Highest proof rung for this code: **3** (container on a
laptop, against a replay of print 2 and a fake gateway). The `.app` is built but not on the device, and nothing has ever
been sent to the printer by this code.

"Print 1" and "print 2" are the two ground-truth band prints (`ground-truth-bands-1` and `-2` in `experiments.md`);
the models are fitted on them and print 2 is the profile the app runs on.

## In plain words

About twice a second (LEAM's IR rate since 21 Oct) the app looks at the IR camera behind the nozzle, measures how wide
the fresh bead looks in pixels, and turns that into millimetres with a model fitted to our calliper readings. It compares the result with the target width
the operator set. If the bead is off by more than the noise for long enough, it picks the cheapest setting to change
(flow first, then speed, then nozzle temperature), sizes the step with a small physics model of the printer, and shows it
as advice. Only when Auto is switched on **and** sending is enabled on the device does it send that step to the
printer, one small step at a time, and it waits to see the effect before the next one. It never retries a failed
command. One limit matters for every demo: on print 2 the camera cannot see beads wider than 1.50 mm (they merge with
the neighbouring wall), so the app can detect and correct a bead that gets too narrow, not one that gets too wide.

## Pipeline

```
IR frame (trailing topic, newest only, decoded in memory)
  │  width.py + geometry.py        bead axis predicted from the nozzle pixel and the travel heading;
  │                                 pixels 16–26 px behind the glow pooled across the axis; edge-to-edge gap
  ▼
width in px  + confidence + group (camera, heading) + deposit time        ◄── motion state at the frame's acquisition time
  │  camera_model.py                px → real mm per group (nozzle °C, speed); knows where the camera goes blind
  ▼
real mm per sample
  │  estimator.py                   rolling robust estimate per view, views combined; relative view offsets learned
  ▼
estimate: mm, sigma, model sigma, confidence, age, at_ceiling             ◄── operator target (mm)
  │  controller.py + printer_model.py     decides once a second (IR ~2 frames/s): hold, or one setting change
  ▼                                        reads settings from the telemetry readbacks, never from what it last sent
decision (advice)  ──►  dashboard (WebSocket), SQLite log (scalars only)
  │  pipeline.py → bridge.py        only if Auto ∧ CONTROL_SEND_ENABLED ∧ every send check (below)
  ▼
request to LEAM's gateway (schema 0.2.0, ≥ 2 s apart)
```

| Stage | Code | Notes |
|---|---|---|
| Ingest | `app/server/bridge.py` (hook), `pipeline.py` | One worker measures the newest IR frame and drops older waiting ones. IR bytes live only in that slot and the worker's locals |
| Width | `width.py`, `geometry.py` | Rejects with a reason: no timestamp, turning (distance since the corner, not time), ambiguous or too-wide ridge. Readings near the blur floor get low confidence. Never raises |
| Camera model | `camera_model.py` | Hinge model per (camera, heading) group; groups without a fit give nothing (D23, D33) |
| Estimate | `estimator.py` | Trimmed mean per view over every frame, inverse-variance across views; `sigma_mm` is random noise with at most one frame per second counting (`sigma_bucket_s`: at 2 frames/s neighbours correlate ~0.5), widened by the views' disagreement until their offsets are learned (`harmonize_s`; after that by at most `view_spread_cap`, default 1 = not at all); the calibration error is `model_sigma_mm` |
| Control | `controller.py`, `control_config.py` | Pure logic, no I/O. Limits, costs and thresholds in config (D30): `deadband_mm` 0.05, `persist_s` 10 / `persist_estimates` 5 / `persist_gap_s` 30, `min_width_mm` 1.30 (the walls alarm, 0.8–2.0), floor rules `floor_min_confidence` 0.45 / `floor_step_factor` 1 / `floor_hold_s` 20 / `floor_release_mm` 0.05. `mode: fallback` is a flow-only proportional controller that needs no printer model |
| Printer model | `printer_model.py` | numpy forward and inversion of the fitted physics model |
| State | `control_state.py`, `store.py` | Target persisted in `DATA_DIR/control.json`; Auto memory-only. `DATA_DIR/prototype.db` holds scalars only |
| Dashboard | `app/static/` | Width / target / Δ with **Hold current width**, Auto switch, "sends" LED, decision line with a *Why*, pinned levers, models LED, width-vs-target chart with the camera ceiling. At the ceiling the width and Δ are shown as lower bounds ("≥") |

**Clocks.** Frame and motion times are LEAM's clock (`source_utc_ns`); the estimator, controller and store use the
app host's own. The two meet through one offset (minimum of receive time minus source time over 30 s). Nothing compares the two
clocks directly; an offset beyond 2 s holds everything with an alarm.

**When the controller holds** (it says why on the dashboard): no target, a model or view missing, estimate stale
(> 5 s) or confidence < 0.5, fewer than two views in the window, not extruding, first 6 layers, Auto just switched
on, gate closed, a lever still settling, the error inside the deadband (0.05 mm, or 2 sigma) or not yet persistent,
widening wanted while the estimate is at the camera's ceiling, no lever able to move the width (alarm). Two of these give
way under the floor rules (below): confidence 0.45–0.5 and the deadband. Costs: flow 1, speed 3, nozzle 10. Effect
delays: flow and speed 3 s, nozzle 30 s.

**Persistent** means off target on one side for 10 s, spanned by at least 5 estimates. Ticks without a usable estimate
(none, stale or weak, one view only, no readback, not extruding) count as time but not as estimates, until the last
off-target estimate is 30 s old (`persist_gap_s`, longer than a layer change or a blind side): then the run starts over.
It also starts over on an estimate on target or on the other side, and on any other hold: a settings change (ours or
someone else's), spacing, a command in flight, another sender's 30 s hold, the first layers, widening latched, a
printer-model failure, Auto switched on, and the at-ceiling holds (only with a camera model that has a ceiling).

**Walls alarm** (D45): a usable estimate (fresh, confidence ≥ `floor_min_confidence` 0.45, real mm) below
`min_width_mm` (1.30 mm) puts "alarm: below 1.30 mm (walls may not
touch)" in front of every reason the controller decides and of the pipeline's not sent, clock and target-stamp
overrides; an alarm behind it stays one. It is an alarm, not a hold. There is none without a usable estimate (no model,
refit, control step failed), with a relative camera model, or at a ceiling.

**Floor rules** (D48): below the floor the cautious rules give way. While a usable estimate (confidence ≥
`floor_min_confidence` 0.45) is under `min_width_mm` and narrower than the target, Auto ignores the noise band and
accepts that lower confidence; the persistence rule (10 s, 5 estimates), bounds and spacing still apply. The rules stay
on for `floor_hold_s` (20 s) after the last estimate below the floor while the estimate stays under
`min_width_mm + floor_release_mm` (1.35 mm), so a reading bouncing around 1.30 doesn't restart the confirmation; a
settings change or a new target ends them. Decisions made this way say "floor rules: " and carry `floor: true`. Steps
stay at each lever's `max_step` unless `floor_step_factor` (default 1, at most 2) is raised: 2× answered a clog on noisy
infill more often (19/20 vs 16/20 on the bench) but ratcheted a steady noisy print wide (mean +0.11 mm, flow to 130 %).
A bigger step goes out only when widening a bead narrower than wanted; the pipeline checks that again before sending.
The price at 1×, on 3× frame noise: a steady print drifts +0.07 mm wide on average (D48). `/api/control` `limits`
reports `pin_s`, `min_layer`, `min_width_mm` and `floor_step_factor` (deploy overrides in the table below).

## Safety design

| What could go wrong | What stops it |
|---|---|
| A restart resumes sending | Auto is memory-only and starts OFF (D21) |
| A command goes out by accident | Two keys: the Auto switch and the deployment setting `CONTROL_SEND_ENABLED=1`, default off (D28). The compose file does not set it, so the device app only advises until someone adds it. With Auto on and the second key off the controller runs *dry*: decisions are recorded and shown (`dry: true`), nothing is published |
| The controller touches the wrong thing | Allowlist: flow, speed override, nozzle temperature. Pressure advance and fans are never sent by the controller |
| A bad value goes out | Re-checked right before the request: Auto still on, sending enabled, allowed lever the operator left on, inside bounds, step ≤ the lever's maximum (or the operator's smaller one, read from the control state) from the fresh readback (× `floor_step_factor` only for a floor-rules decision that widens a bead narrower than wanted, D48), ≥ 2 s since our last command, gate open |
| LEAM is not ready | Gate: fresh writer status with `normal_authorized`, fresh telemetry with the gateway in RUN, outputs permitted, command gate open, gateway source fresh |
| A failed command is repeated | No automatic retry (D26). Rejected, dispatch_unknown or unconfirmed: the lever is held until a readback explains it; two in a row hold everything with a latched alarm. A send that raised after publishing counts as sent |
| Commands stack up | One lever in flight, ≥ 2 s apart, next judged only on beads deposited after the effect delay (D24, D27) |
| The controller fights a person | A change from outside (two readbacks that match none of our values, a foreign gateway result, a manual send) pins that lever for 5 min; any foreign result holds Auto for 30 s (D18, D27) |
| The camera cannot see the error | Ceiling at 1.50 mm: no widening at the ceiling, typed targets above it refused, display "≥ 1.50" (D29). A relative camera model allows only *Hold current width* |
| The camera goes blind below the ceiling (IR drift) | Every widening step starts from a settled width and must show its effect before the next one, at any level, whenever the camera has a ceiling (D39). Each view is compared with itself; the rise is judged on beads laid 30–180 s after the step and must reach half the predicted change. Too little: widening latches with "alarm: widening had no visible effect … check the bead; lower the target, or switch Auto off and on to re-arm" (in advice mode: "switch Auto on and off again"). Narrowing still works while latched |
| The judgement itself gets stuck | Fewer than 20 estimates in 180 s ("waiting for enough width estimates to judge …"): widening stays held until a narrowing step, a foreign change or a restart. Switching Auto off and on does not clear it |
| A saved target means something else after a model change | The target is stamped with the camera model it was set under (id, relative flag, typed or Hold). Another model, a stamp from an older version or an unreadable one: hold with "alarm: target set under another camera model; set it again" (D40) |
| The estimate is wrong | Fitted on a different geometry or extractor setting than the one running: hold all with a "refit" alarm. NaN or missing inputs hold instead of acting |
| Limits | Flow 70–130 % (±10 per command), speed 60–100 % (±10), nozzle 190–250 °C (±5), pinned levers excluded (D30); below 1.30 mm a widening step may be `floor_step_factor` × that (1 by default, at most 2; D48). The Test bench can only make steps smaller or switch levers off, never widen a limit (D50) |
| An operator zone tighter than the noise | Auto still holds within 2 × sigma, and the Test bench says when the noise is wider than the zone (D50) |
| IR gets stored | It is not: memory only; the log holds numbers. The end-to-end runner checks that no image files appear |
| Local tests reach the real printer | Fake gateway and replay refuse non-loopback hosts; end-to-end runs use a private broker and a clean environment (D37) |

Latched alarms say how to clear them. With Auto on: switch Auto off and on. In advice mode the text says to switch Auto on
and off again, and Auto on sends commands if sending is enabled, so check the second key first.

## Run it locally

Needs: a Python env (`.venv`) with `app/requirements.txt` and `tools/requirements.txt` (the tools, tests and CPU
PyTorch for the printer fit), `mosquitto`, the fitted files in `app/models/` and a print-2 recording with IR under
`data/captures/`. The last two are local-only (D35, D13).

```sh
mkdir -p data/dev
mosquitto -p 1884 &                                              # local broker, loopback only
.venv/bin/python -m tools.sim.fake_gateway --port 1884 --controller-id demo --log data/dev/requests.jsonl &

cd app
DATA_DIR=../data/dev MODELS_DIR=models LOG_LEVEL=INFO \
MQTT_HOST=127.0.0.1 MQTT_PORT=1884 MQTT_WRITER_USER=local MQTT_WRITER_PASSWORD=local MQTT_CONTROLLER_ID=demo \
../.venv/bin/uvicorn server.main:app --port 8765                 # http://127.0.0.1:8765
# add CONTROL_SEND_ENABLED=1 to let Auto publish (to the fake gateway); leave it out to see advice and dry runs

# in another shell, from the repo root: replay print 2 at 1x (alignment across topics is exact only at 1x)
.venv/bin/python tools/mqtt/replay.py data/captures/<print-2 capture> --port 1884 --speed 1 --no-rgb
```

Set `DATA_DIR` as above to keep runs out of the app folder: the default `app/.data` is gitignored, but it holds
`settings.json` with any passwords typed into the dashboard.

| Variable | Default | What |
|---|---|---|
| `MQTT_HOST`, `MQTT_PORT` | none, 1883 | Broker. Locally `127.0.0.1` and `1884` |
| `MQTT_USER`, `MQTT_PASSWORD` | none | Reader account. Empty on the local broker |
| `MQTT_WRITER_USER`, `MQTT_WRITER_PASSWORD` | none | Writer account. Anything on the local broker; sending needs both |
| `MQTT_CONTROLLER_ID` | none | Our id in requests. Must match the fake gateway's `--controller-id` locally |
| `CONTROL_SEND_ENABLED` | off | `1` is the second key (D28). Anything else, or unset, is off |
| `CONTROL_PIN_S` | 300 | Seconds Auto leaves an operator's change alone (at least 60). A bad or non-finite value keeps the default |
| `CONTROL_MIN_LAYER` | 6 | First layer Auto may act on (at least 1) |
| `CONTROL_FLOOR_STEP_FACTOR` | 1.0 | Step multiple for a floor-rules widening step below 1.30 mm, 1–2 (D48) |
| `MODELS_DIR` | `app/models` | Folder with `geometry.json`, `camera_model.json`, `printer_model.json`. A missing file is a "no model" state, not a crash |
| `DATA_DIR` | `app/.data` | `control.json` (target), `prototype.db`, `settings.json`. The image uses `/cfg-data` |
| `FIRST_LAYER_MM`, `LAYER_MM`, `LINE_WIDTH_MM` | 0.7, 0.5, 1.5 | Slicing of the part being printed: layer counting (first 6 layers hold) and the printer model's context. A bad value is ignored with a warning |
| `LOG_LEVEL` | `INFO` | The app's own log lines (the image runs uvicorn at `warning`) |
| `APP_VERSION` | `dev` | Shown at `/api/version` |

What to expect on a replay: it is open-loop, so readbacks never follow our commands. With Auto on and sending enabled
the controller sends one command to the fake gateway, then stops with a latched alarm ("confirmed but not applied").
That is correct; do not "fix" it. The closed loop is proved in-process on the simulated plant (`tools/sim/plant.py`,
`tests/test_controller.py`).

One-command alternatives:

```sh
.venv/bin/python -m tools.sim.e2e_run smoke              # no camera model: measures, never acts
.venv/bin/python -m tools.sim.e2e_run advice|dry|auto|all # needs the camera and printer models
.venv/bin/python -m pytest                                # about 800 tests
tools/edge/build-app.sh app <version>                     # image + .app, nothing uploaded (docs/edge-deployment.md)
```

`e2e_run` starts its own private broker, the app, the fake gateway and the replay, and checks that the broker is
loopback before anything else. It writes logs and a report to `data/e2e/` (gitignored) unless `--out` is given.
The same live scenarios are opt-in under pytest and take 3 min (smoke) to about 26 min (advice, dry, auto) at replay
speed 1x: `E2E_RUN=1 .venv/bin/python -m pytest tests/e2e -m slow`. A plain `pytest` skips them.

## Refit the models

Recordings with IR frames stay under `data/captures/` and outputs under `data/model/` (numbers only). Order matters:

```sh
.venv/bin/python -m tools.model.width_check                 # widths per capture → data/model/widths_*.parquet; coverage and lock-on report. Refuses captures filmed before a nozzle refit unless --geometry <backup>
.venv/bin/python -m tools.model.dataset build               # frames, bands, prints + the go/no-go gate (GO needed)
.venv/bin/python -m tools.model.fit_camera                  # → app/models/camera_model.json; both variants → data/model/
.venv/bin/python -m tools.model.fit_printer                 # → app/models/printer_model.json
```

- **When:** after any change to the extractor or the geometry (other than the nozzle aim, below), new calliper data, or a
  new print profile. The camera file records the geometry version and an extractor-settings hash and the app holds on a
  mismatch; the width data carries both too and `fit_camera` refuses data that does not match (D41).
- **`fit_camera`** writes the absolute model if the identifiability rule passes (D31), else the relative one;
  `--absolute` forces it, `--dry-run` only reports. It prints the report tables.
- **`fit_printer`** takes `--beta fit|1` and `--boot N`. Read the notes it writes into the JSON (`fit.notes`) before
  trusting β or the flow knee.
- **Pre-demo nozzle aim (aim only, D38).** Cameras shift between prints (cam1 ~1.7 px), so before the demo each camera's
  measuring window is re-aimed at the nozzle. Nothing else is refitted: no dataset rebuild, no camera-model refit.
  1. Record 20–30 min of the demo print with a recorder that keeps raw IR locally (the D13 exception, never in git).
     `tools/mqtt/record.py` drops IR by design, and with 5 min most cameras get too few frames and simply keep their
     value.
  2. `.venv/bin/python -m tools.model.width_check --fit-nozzle <capture>`. Cameras with too few frames keep their nozzle
     pixel. If a camera moves more than 2 px it exits 1 and writes nothing (likely a bad recording): inspect, then rerun
     with `--force`. The old file is kept as `app/models/geometry.json.<timestamp>.bak`; the version gains
     `+nozzle-<minute>`, which the app's provenance check ignores.
  3. `.venv/bin/python -m tools.model.width_check <capture> --no-write`: each camera's lateral offset should be about 0 px
     and its "search at limit" share low.
  Old recordings are then refused at the new aim (their widths would be measured at the wrong window). To rebuild from
  them, pass the backup: `width_check --geometry app/models/geometry.json.<timestamp>.bak`. Why aim only: re-measuring the
  golden prints at a new aim distorted cam0 by up to 16 %.

## Endpoints

| Endpoint | What |
|---|---|
| `GET /api/control` | `target_mm`, `auto`, `send_enabled`, `estimate`, `last_decision`, `pinned`, `models`, `limits`, `ceiling_mm`, `relative`, `server_ms`, `persist_error`, `load_error` |
| `POST /api/control` | `{"target_mm": number or null, "auto": bool, "hold_current": true, "band_below_mm": 0–1 or null, "band_above_mm": 0–1 or null, "levers": {op: {"enabled": bool, "max_step": whole number from 1 to the configured step, or null}}}`, any subset; levers merge per lever; returns the same view. 400 with `{"error"}` for a target outside 0.3–5.0 mm or above the camera ceiling, `target_mm` together with `hold_current`, or `hold_current` without a fresh estimate of confidence ≥ 0.5. `send_enabled` is read-only: it comes from the environment |
| `GET /api/trace?since_ms=` | The Test bench's history: one scalar row per control step for 15 min (`mm`, `sigma_mm`, `noise_mm`, `judged_mm`, `confidence`, `target_mm`, `band_below_mm`/`band_above_mm` as applied, `auto`, `dry`, `gate_open`, `flow`, `speed`, `nozzle_target`, `nozzle_c`, `layer`, `action`, `sent`, `hold`, `floor`, `reason`) and the operator's `events` (`manual` sends, `auto`, `target`, `band`, `levers`), newer than `since_ms` |
| `GET /api/pipeline` | Health of the width loop: `running`, `extraction`, `missing` models, `clock_offset_ms`, `clock_alarm`, `gate_open`, `camera_provenance`, counters (`frames_done`, `frames_dropped`, `sent`, `send_refused`, `send_failed`, `send_uncertain`, `foreign_results`), errors per stage, model versions |
| WebSocket `/ws` | New message types `width` (the estimate, ≤ 2 Hz) and `decision` (every control step, 1 Hz) next to the existing ones |

An estimate carries `mm`, `sigma_mm`, `model_sigma_mm`, `n`, `age_ms`, `confidence`, `per_group`, `ceiling_mm`,
`at_ceiling`. A decision carries `mode` (`advise` or `auto`), `dry`, `sent`, `action` (`operation`, `value`), `reason`
(starts with `alarm:` when a person should look), `error_mm`, `predicted_mm`, `candidates`, `hold`.

## Test bench page

`test.html` (tab *Test bench*) is for running a test by hand. Top: the width now (coloured by zone), the zone as Min /
Target / Max (presets ±0.03/0.05/0.10, *Hold now*) with a meter of zone, target, 1.30 line and width, the Auto switch with
send and gate lights, and a small raw IR view. Middle: width over the last 5/10/15 min with the noise ribbon, target,
zone, 1.30 line, Auto-on spans and markers for Auto's commands, the operator's sends and outside changes; flow, speed and
nozzle below on the same time axis. Right: the *Auto loop* (measure → compare → confirm → act → settle, with the current
step in plain words and the controller's own reason), the levers (readback, Auto on/off, Auto's step, nudges and Set) and
an event list. Everything comes from `/api/trace` and `/api/control`, so every open page shows the same history;
nothing image-like is stored.

## Demo setup (recommended)

Bench results, not yet checked on the printer.

- **Target: type 1.40 mm.** Do not use *Hold current width* at ~1.50: it parks the loop on the camera's knee, where noise
  alone makes the controller widen blind (6 of 20 bench runs, up to 1.60 mm). 1.40 had the fewest false stops and no blind
  step. A lower target only protects against a drift smaller than ceiling minus target (0.10 mm); beyond that the widening
  check (D39) takes over. On the way to 1.40 the controller narrows with flow.
- **Disturbance: the operator drops the nozzle to 200 °C** (195 °C for a bigger dip; do not raise the target instead).
  At print 2's settings this melt-limits the hotend and narrows the bead (fitted model: 1.55 → 1.39 mm). More flow then does
  nothing; slowing down restores it (model: speed 90 % → 1.54 mm, 80 % → 1.55 mm). The nozzle change is pinned as the
  operator's and the controller answers with speed. A flow change by the operator is pinned too and leaves no usable
  lever: the controller holds and says so.
- **Order:** re-aim the cameras (above), then set the target, so it is stamped with the final camera model (D40).

## Known limitations

- **IR is blind above 1.50 mm** (print 2's wall pitch): under-extrusion is detected and corrected, over-extrusion is not.
- **The long-side view is off** (cam2, −90°; its outer pass does not follow flow). A usable width exists ~63 % of the
  time on print 2; the readout blanks and the controller holds for ~20 s per layer. Fix options: label inner/outer pass
  at runtime, or rotate the demo print.
- **Fitted on print 2's file and orientation.** Another orientation needs new camera offsets; the mm level rests on
  print 2's baseline calliper (±0.1 mm), the baseline spread is ±0.15 mm.
- **The widening check costs time and can stop for nothing.** On the bench a drift of -0.05 mm with a typed 1.50 can still
  allow up to 3 blind widening steps (bead up to ~1.62 mm); -0.12 and -0.20 mm allow 1 (~1.57 mm). Each widening step takes
  30–180 s to judge, clog recovery is 2–3× slower than without the check, and 1–2 of 20 demo runs stop with a false latch
  (safe: the operator re-arms).
- **At print 2's settings, with the hotend not melt-limited, flow is the only lever that moves width** (±0.07 mm per 10 %).
  If an operator changes flow it gets pinned and no usable lever remains. After a nozzle drop to ~200 °C the hotend is
  melt-limited, flow does nothing and slowing down restores the width (see Demo setup).
- **Printer model:** β < 1 and the pressure lag rest on thin evidence; Q220 has no upper bound (`experiments.md`).
- **Manual sends are not behind the second key.** `/api/request` and its raw variant are the dashboard's older testing
  controls: they bypass the controller's limits and need no second key; only the controller path needs
  `CONTROL_SEND_ENABLED=1`.
- **Auto off or app stopped:** the printer keeps its last settings; nothing is sent to restore a baseline.
- **Not built:** live offset adaptation (D14; a real disturbance and a camera drift look the same without a reference),
  pressure advance and corners (Level 3), a defect alarm for the "hole between walls" case, automated UI tests.
- **Not yet proved on the printer** (rung 4), and no `.app` of this version is on the device.
- Intervals use the wall clock, not a monotonic one.
