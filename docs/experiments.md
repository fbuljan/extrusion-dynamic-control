# Experiments

Every experiment, including the failed ones. Newest at the bottom. Format: the `experiment` skill
(`.claude/skills/experiment/SKILL.md`) — hypothesis, pass criterion and timebox before running; result and
verdict (adopt / drop / park) after.

---

## live-mqtt-probe | 2026-10-20 | timebox 15 min
Hypothesis: we can read all organiser topics with the reader account from a laptop on site.
Pass if: CONNACK success and messages on all five topics.
Where: `tools/mqtt/probe.py`
Result: works only from the printer's subnet (unreachable from the general venue Wi-Fi). Login OK. Wildcard
subscription refused; all five exact topics granted. Rates over 20 s: telemetry ~14/s (~5.6 KB); RGB previews
~1.5/s ahead and ~0.6/s trailing (~55 KB, 640×480); IR ~2/s each (~2.5 KB). Ran live (rung 2).
Verdict: adopt. Because: the data path works; topic list lives in `tools/mqtt/common.py`.

## live-recording | 2026-10-20 | timebox 10 min
Hypothesis: we can record enough real frames to develop offline.
Pass if: several hundred RGB frames + matching telemetry, replayable.
Where: `tools/mqtt/record.py`, `tools/mqtt/replay.py`
Result: 5 min during a Benchy print → 4217 telemetry messages, 926 RGB ahead + 866 trailing frames (~3/s each;
rates were higher than in the first probe), 112 MB. Replay onto a local mosquitto verified. First look: the
trailing camera views the print at an oblique angle; material is glossy/translucent; many frames are motion-blurred.
Verdict: adopt. Because: offline dev loop exists. Recording kept locally (not in git); ask Filip for a copy.

## edge-app-packaging | 2026-10-20 | timebox 30 min
Hypothesis: we can build an Industrial Edge `.app` on a Mac without the Siemens tools (they need an IE Hub login).
Pass if: a `.app` with an amd64 image, compose and reverse-proxy config is produced.
Where: `tools/edge/build-app.sh`, `tools/edge/hello-edge/`
Result: works with the third-party packager `tiap` v0.14.5 — 45 MB `.app` containing detail.json, digests.json,
compose (version interpolated), image TAR, nginx.json, icon. NOT yet imported into IEM or installed on the device
(no IEM login yet). The official IECTL has no macOS build; `tools/edge/iectl.sh` runs it in a linux/amd64 container
once someone downloads it from the IE Hub.
Verdict: adopt (packaging). Next: import into IEM + install on the device — needs IEM credentials and the
`appimporter` role from Siemens.

## iem-import | 2026-10-20 | timebox 20 min
Hypothesis: the management portal accepts a `.app` built with tiap (not the official Siemens tools).
Pass if: the import task finishes and the app appears in Device Applications.
Where: portal → Applications → "+ Add application" → `tools/edge/hello-edge/dist/hello-edge_0.0.2.app`
Result: import task **Finished** in ~45 s; app listed. Portal is IEM 2.3.2 (V2 UI). Our team's device is a SIMATIC IPC
BX-39A (x86-64, Edge OS 3.3.0, no GPU allocation) in the "Leam" device group, on the same subnet as the MQTT broker.
Verdict: adopt — tiap output is accepted; no IE Hub login needed for packaging. Next: install on our device and open
`/hello-edge/` through the device's reverse proxy.

## device-install | 2026-10-20 | timebox 20 min
Hypothesis: the imported hello-world app installs on our device and is served through the device's reverse proxy.
Pass if: install job completes and `/hello-edge/` on the device answers.
Result: install job **Completed** in under a minute; only warning "Application is not signed" (expected). The route
`/hello-edge/` went from 503 to 401 once the app started — the device's reverse proxy puts apps behind the device UI
login, which we don't have yet. The device uses a self-signed certificate. Rung 4 reached for install; the page itself
not yet viewed.
Verdict: adopt — the full path (build → import → install) works. Next: device UI login from Siemens, then confirm the
page and test that a container on the device can reach the MQTT broker.

## device-broker-reach | 2026-10-20 | timebox 45 min
Hypothesis: a container on our edge device can reach the organisers' MQTT broker.
Pass if: the probe app on the device opens TCP to the broker and gets an MQTT answer.
Where: `tools/edge/mqtt-probe/` (versions 0.0.2–0.0.3), status JSON on device port 18080.
Result: **works.** On the device: TCP ok, CONNACK "Not authorized" (the probe deliberately carries no credentials, so
nothing secret goes into the shared portal; a broker answer proves the path). Bonus: a published container port
(`ports: 18080:8000`) is reachable from the event network without the device UI login. Hurdles on the way: the first
build baked credentials into the `.app` (rejected — never upload credentials); device log streaming is disabled, so
logs aren't visible in the portal; the portal's upload dialog froze once although the import had finished.
Verdict: adopt. Next: give the real app its credentials at install time (IEM app configuration), not in the `.app`.

## dashboard-on-device | 2026-10-20 | timebox 20 min
Hypothesis: the dashboard app (0.1.0) installs as an update over the hello-world app and serves on the device.
Pass if: device port 18081 serves both pages.
Result: **works.** Import ~2 min, update ~1.5 min; `/` and `/monitor.html` served, settings API answers with broker
host and users preset and no passwords. Live data not yet seen: the broker's reader password changed and we don't
have the new one. Rung 4 for deploy; live data pending credentials.
Verdict: adopt. One full deploy cycle costs ~4–5 min, so iterate locally against replays and deploy milestones only.

## deploy-routine | 2026-10-20 | timebox 15 min
Hypothesis: a written deploy routine (build → no-secrets check → import → update → poll the version) gets a fix onto
the device in ~5 min, and the app's saved settings survive an update.
Result: 0.1.1 (version label, readable login-refusal reason, no broken images before the first frame) built, imported
and running in ~4 min. Settings stored on the device survived the update. The update job was started by someone
else on the shared IEM login two minutes after the import; check the running version before clicking Update.
Verdict: adopt. The app now reports its version at `/api/version`, so "what's deployed" is one request.

## dashboard-audit | 2026-10-20 | timebox 30 min
Hypothesis: what the dashboard shows matches the live MQTT data exactly and honours the guide's freshness/validity rules.
How: three read-only audit agents compared raw broker messages with what the device app sent to browsers (60–75 s
captures; IR handled in memory only).
Result: every displayed number matched the raw data (250/250 updates), images were never swapped or dropped. Defects
found and fixed in 0.1.2: values stayed on screen ~220 s after an outage (now blanked within 1 s, gateway shows "no
data"); previous-layer temperature and cooling ignored their source's `valid`; camera "fresh" ignored each frame's own
lease and true age (IR arrives ~0.85 s old; label now shows the age); a slow browser could get a backlog of old frames;
graphs drew lines across gaps; browsers kept old scripts after a deploy (now `Cache-Control: no-cache`). Added nozzle and
bed temperature (new in telemetry, refreshed every ~1.4 s). Not changed: an image from the previous trailing camera
stays visible (labelled with its camera) after a camera switch — the guide allows labelled held frames.
Verdict: adopt. 0.1.2 live on the device 14:37, verified on live data.

## writer-contract | 2026-10-20 | timebox 45 min
Hypothesis: building requests exactly like LEAM's command guide (schema 0.2.0, reference helper) makes them admissible.
Result: our first guessed request (flow 150, sent by the team lead at 14:15) was rejected `invalid_request_shape_or_size`.
Rebuilt `make_request` from the guide; against a fake gateway on the local broker that checks the exact key set, ranges,
session, sequence and expiry, all five operations were accepted, and the app refused out-of-range values, a closed gate,
stale status and >1/s. 0.1.3 on the device builds a valid live request from LEAM's status (not sent). Rung 3 for sending;
no live command sent with the new format yet.
Verdict: adopt. Next: one no-op live request (flow 100 while at 100) with the team lead's OK to confirm the round trip.
Update 15:03: first live round trip — flow 140 sent by the team lead from the device app (0.1.3) → `accepted` →
`setting_confirmed`, observed 140; telemetry `extrusion_percent` 140.0 within seconds. Rung 4 for sending.
0.1.4–0.1.5 add refused attempts to the command log, a "now" readback per control, and sliders that follow the live value.

## width-rgb-v0 | 2026-10-20 | timebox 90 min
Hypothesis: across-line brightness profiles on calibrated RGB previews give bead width in mm.
Method: per frame, the camera's homography by camera_index; travel direction from telemetry decides whether the view
shows the fresh bead (behind the nozzle) or the previous pass (ahead); 4 windows, subpixel steepest-slope edges in
blue+green, width perpendicular to the line; heuristic confidence and reject reasons. Code in the workspace scratch area.
Result: runs fast (~6 s per 1800 frames) but does **not** measure the bead yet. During the 20 Oct afternoon print flow
was stepped 100 → 200 → 50 % and the measured width barely moved (median 0.90 / 0.82 / 0.86 mm), so the number tracks
blur and the bright line top, not bead width. The camera labelled `trailing` looks ahead of the nozzle for +x+y travel:
only 1 of 494 frames showed the fresh bead. Edge-to-edge vs FWHM differ ~0.1 mm; calibration is 0.16–0.26 mm RMS.
Verdict: park. Any method must follow the recorded flow steps before we trust it. Next: gap = spacing − width or a
blurred-bar fit; IR as a fresh-bead signal; ask LEAM for a truly trailing camera, focus, white light, nozzle size, and a
caliper sample.

## ir-steps-1 | 2026-10-20 | timebox 15 min
Hypothesis: controlled single-variable steps give clean labelled data for IR width methods.
Run 16:08–16:20 through the device app (one sender at a time, readback-verified, abort on interference): flow
100/70/130/85/115/100 % at nozzle 230 °C (60 s each), then nozzle 230/210/250/230 °C at flow 100 % (90 s each);
labels in the capture's `experiments.jsonl`. Every setpoint confirmed by telemetry within 1–2 s; settings restored.
Confounder: speed override changed underneath us (100 → 75 % at 16:10:59 by another sender, → 60 % at 16:12:24 with no
gateway command), so flow windows from 16:11 run at lower speed; the runner only guarded flow and nozzle.
Verdict: adopt the runner (guard all five levers next time); data in analysis.

## ir-width-methods | 2026-10-20 | timebox 30 min
Hypothesis: an edge-based estimator on the IR preview gives a bead width that follows the ir-steps-1 flow steps.
Method: profiles across the bead 18–28 px behind the nozzle; steepest-slope edges, Canny on 4× upsampled frames, FWHM,
thresholds and a blurred-bar fit, on brightness (luma) vs a colour-to-heat inversion. Code in the workspace scratch area.
Result: best is steepest-slope edges on luma: 2.49 px ≈ 1.5 mm at ~0.60 mm/px, per-frame noise 0.13 px SD (~0.06 mm
robust). It follows flow weakly (+0.056 px per +10 %, t = 7.6), but nozzle temperature (+0.084 px per +10 °C) and speed
(−0.145 px per +10 % override) move it more: flow 100 % at speed 60 vs 100 differs by 0.82 px, the whole 70–130 % flow
sweep by 0.34 px. The bead is 2–2.8 px wide, barely above the ~1.8 px blur; its centre saturates the display palette;
colour is 4:2:0-subsampled, so luma beats colour. Cameras differ by 0.3–0.4 px: never pool raw px across cameras.
~65 % of extruding frames usable.
Verdict: adopt as the candidate estimator for milestone I, not trusted as mm. Next: golden-sample proof (calliper beads
printed at speed 100 vs 60 % and 210 vs 250 °C); ask LEAM for radiometric IR or a fixed range / grey palette; clean flow
sweep 60–140 % with speed pinned.

## ir-geometry | 2026-10-20 | timebox 45 min
Hypothesis: the IR cameras' angle can be modelled from the print itself, giving px → mm across the bead per camera.
Method: rulers from the print: infill pass spacing (3.00 mm from position telemetry, ~13,000 ridge peaks per camera)
across the bead, and U-turn tips that move by the known head travel along it. Joint pinhole fit with radial distortion
for cam1/cam3 (97 % of IR frames). Code in the workspace scratch area.
Result: the nozzle sits on a fixed pixel per camera (±1 px, no drift over 36 min); the fresh bead leaves along a fixed
image line (~134°). One shared lens (f ≈ 108 px, k1 ≈ −0.29); cameras ~56 mm above the layer, tilted ~21°. Pass
positions reproduced to ~0.1 mm. Across an infill bead, mm/px falls along the bead: cam1 0.63 / 0.535 / 0.50 and cam3
0.68 / 0.56 / 0.52 at 20 / 60 / 100 px from the nozzle; ±2 % from 30 to 110 px, ±4 % at 10–30 px; invalid closer than
~10 px and in the image corners. A flat 0.6 mm/px is off by −11 to +19 % for infill and up to −44 % for beads crossing
the view (walls). With the model, the ir-width-methods bead reads ~1.65 mm (cam3) and ~1.76 mm (cam1) at 23 px.
Verdict: adopt for cam1/cam3 infill. Across-view scale and cam0/cam2 are unverified. Next: one infill layer rotated 90°,
or a dot-and-jog calibration move; LEAM's IR calibration files.

## ground-truth-bands-1 | 2026-10-20 | done (closed 21 Oct)
Hypothesis: a walls-only triangle printed in 8 bands of 15 layers (baseline, flow 80 / 120 %, baseline, speed 60 %,
nozzle 210 / 250 °C, baseline), measured by calliper per band, tells whether the IR width follows real width and whether
speed and temperature change real width or only the glow. Settings switched automatically at layer changes counted from
telemetry Z (checked on the afternoon recording: 21 of 21 layers, no false counts). Run 17:41–18:06: all 8 bands applied on
schedule, each change confirmed by readback in 0.5–2.1 s; speed 60 % held (LEAM's speed controller did not interfere);
nozzle reached ±20 °C within ~4 layers. Band heights: 7.5 mm each from the bed (band k = 7.5·(k−1) to 7.5·k mm). The
nozzle baseline was 220 °C, so the temperature bands ran at 200 / 240 °C (relative ±20, not the planned 210 / 250).
IR (gradient edges, 18–28 px behind the nozzle, per camera/side): the part has ONE wall line; legs are
seen by cam1/cam2 with the bead near-vertical in the image, the hypotenuse by cam0. Flow 80 → 120 % moved IR width by
−0.11 to +0.01 px (noise ±0.05–0.1 px per band median) although a real ~1 px change was expected; nozzle −20 °C −0.24 to
−0.80 px, +20 °C +0.40 to +0.57 px; speed 60 % +0.07 to +0.22 px. Legs return to baseline (bands 4/8 within 0.08 px);
hypotenuse baseline drifted −22 % from band 4 to 8, unexplained. So far the IR edge width tracks heat, not flow.
Calliper (one value per band, side not stated, 0.1 mm): 1.7 / 1.4 (flow 80) / 1.4 (flow 120) / 1.4 /
1.6 (speed 60) / 1.1 (200 °C) / 1.6 (240 °C) / 1.6 mm. The real wall did not change with flow 80→120 either; temperature
moved it most. IR band medians agree in direction: leg_b/cam1 r = +0.80 (0.48 without the 200 °C band), mm ≈ 1.02·px −
1.67, residual 0.11 mm; leg_a/cam2 r = +0.60; hypotenuse/cam0 r = +0.33. Hypothesis: the hotend is melt-limited
(~20 mm³/s commanded at 100 %), so flow % barely changes width while temperature and speed do. Baseline calliper spread
1.4–1.7 mm limits what can be resolved. Verdict: IR follows real width on the legs (park the "heat only" reading);
flow is not an effective width lever in this regime. Next: 3 readings per band and side; run 2's flow 60/140 bands.
Final state (21 Oct): this is "print 1" in the model fits. Its calliper readings (one per band) feed the printer model
(melt-limited regime: flow 80/120 % change nothing; nominal width 1.59 mm at 26.5 mm/s) and the camera model's speed term.
It does not fit the camera model that the runtime uses: one wall, its own knee (~1.1 mm), and the hypotenuse view (cam0)
drifted -0.59 px within the print, so that view is disabled for it. The app runs on print 2's profile. No further runs
planned. Verdict stands: adopt as fitted data, not as the demo profile.

## ground-truth-bands-2 | 2026-10-20 | done (closed 21 Oct)
Same idea on a larger triangle (same Cura file scaled; 0.2 mm layers; first real layer Z 0.7, solid bottom): 5 bottom
layers skipped, then 12 bands × 25 layers (5 mm): the 8 bands of run 1, then flow 60 %, flow 140 %, fan opposite of
baseline, baseline. Flow 90/110 dropped in favour of 60/140 after run 1 showed no IR response at 80/120. Fan off as
baseline to replicate run 1. Bands keep going if another sender changes a setting (logged, not fought).
Run notes: two walls (inner then outer, 1.5 mm apart), 0.5 mm layers, ~53 s per layer; 15 layers for band 1, then
10 per band (5 mm). The fan command (band 11) came back `setting_confirmed` 100 % from LEAM's gateway, yet the Duet
reported 0 % and the fans did not spin: the fan is not a usable lever or disturbance right now, and a gateway
confirmation alone does not prove a physical change (verify against Duet readback).
Calliper (two readings per band, two-wall thickness, 0.05 mm tool, hard to measure): baselines 1/4/8/11/12 = 3.0, 3.1,
3.0, 3.0, 3.0 mm; flow 60 / 80 / 120 / 140 = 2.65 / 2.85 / 3.15 / 3.2; speed 60 = 3.1; nozzle 200 = 2.8, 240 = 2.9.
Bands 3 (flow 120), 7 (240 °C) and 9 (flow 60) show a hole between the walls, i.e. beads narrower than intended (the
calliper overstates them): defect examples, kept out of the golden fit. Unlike run 1, flow moves width here (slower
travel, ~20 vs 26.5 mm/s, so less melt-limited). IR analysis of run 2 not run yet.
Final state (21 Oct): IR analysis done, see ir-follows-flow-gate (GO on cam0:135 and cam3:45; cam1:-90 borderline) and camera-model-hinge. Golden mm = calliper
two-wall thickness minus the wall pitch, 1.500 mm measured from the motion telemetry (IQR 1.498-1.502 over 391 layer x leg),
mean of the two readings. Defect bands 3/7/9 stay out of the camera fit (predicted afterwards as a check) and count only
as upper bounds in the printer model; band 1 counts from Z >= 6.7 mm (fan on below); band 11 counts as baseline (the fan
never changed). 70.7 % of this print's IR frames give a width (4939 of 6988). The camera model, the printer model, the
replay behind every end-to-end test and, if the demo print is the same file in the same orientation (to confirm), the
demo profile all rest on this print.
Verdict: adopt as the golden fit and the standard replay. Because: it is the only print where flow moves real width and
the bead is wider than the IR can resolve at one end of the range.


## ir-follows-flow-gate | 2026-10-20 | timebox: one overnight unit
Hypothesis: the app's IR bead width follows the real width change that flow causes on print 2, so a camera model can turn px into mm. A go/no-go before anything is built on it.
Pass if: flow 140 % minus flow 80 % is positive and ≥ 3 standard errors in ≥ 2 camera-heading groups, pass-pooled. SE = the larger of the bootstrap over layers and the band-to-band term (SD of the median over the four same-settings baseline bands × √2, as each side of the contrast is one band), because within-band scatter cannot see drift between bands (the layers-only rule called 18 of 60 same-settings band pairs different, all cells).
Where: `tools/model/dataset.py` (`build`, `gate`), widths from `tools/model/width_check.py`.
Result: **GO, 3 of 4 groups** (honest SE): cam0:135 +0.47 px (z 6.3), cam3:45 +0.43 (z 4.9), cam1:-90 +0.27 (z 3.0); cam2:-90 +0.26 px does not reach 3 (z 1.3, its baseline bands differ by 0.14 px SD). The first version of the gate (layers-only SE) said 4 of 4 and was too confident. Height drift: a line through the baseline band medians against height explains about half of cam1:-90's contrast (+0.15 of +0.27 px; corrected +0.13 px) and all of cam2:-90's, while cam0:135 and cam3:45 drift the other way (corrected +0.58 / +0.57 px). So the GO rests on cam0:135 and cam3:45; cam1:-90 is borderline (four baseline bands, indicative only; for cam1:-90 the apparent drift is mostly its changing inner/outer pass mix, see camera-gain-per-view). The response is one-sided. Against the baseline bands, flow 60 / 80 % narrows the IR bead a lot (cam0:135 −0.98 / −0.54 px, z −16 / −9), flow 120 % widens it (+0.41 px, z 6.8), flow 140 % reads back at the baseline (−0.07 px) although the calliper keeps widening (two-wall thickness 3.15 → 3.2 mm). Per pass: the inner pass passes in cam0, cam2 and cam3 (z 7.5, 4.0, 8.1; cam1's has too few baseline frames to judge); the outer pass passes only in cam0 (z 6.9), cam3's reaches 2.7 and cam2's +0.03 px (z 0.6) does not respond. The A/A pairs (baseline band vs baseline band) are printed in `dataset_summary.txt`; their z is bounded by construction, since the SD is measured on those same four bands (the SD from four bands is itself rough).
Verdict: adopt (go). Because: IR follows flow below ~1.5 mm, the range the controller needs for under-extrusion; above it the camera is blind (next entry). Rerun after any extractor change: `python -m tools.model.dataset build`.

## camera-model-hinge | 2026-10-20 | timebox: one overnight unit
Hypothesis: IR width in px is linear in real width (calliper) plus a glow term (nozzle °C) and a speed term (mm/s), per camera-heading group, with one gain shared by all groups.
Method: `tools/model/fit_camera.py` on pass-pooled band medians of settled frames of both band prints; gain as a multiple of the geometric px/mm; offsets per group and print; a hinge (linear below a knee, flat above, knee by profile likelihood); leave-one-band-out (LOBO) error; a pre-written rule decides absolute or relative (D31).
Result: **knee 1.50 mm = the wall pitch of print 2**: a bead wider than the pitch merges with the neighbour wall and stops reading wider, so the camera is blind above it. Gain 1.70× geometric (profile 95 % 1.30–2.40; flow-only and nozzle-only bands predict each other: 1.44 vs 1.97); glow 0.011 px/°C. LOBO error below the knee 0.070 mm vs 0.205 mm for a constant per-print model. Flow 60 %, held out, predicted 1.11 vs calliper 1.15 mm. Baseline reads 1.50–1.51 mm. Print 1 does not fit (one wall, own knee ~1.1 mm, drifting cam0 baseline), so the runtime profile is print 2. Enabled: cam0:135, cam1:-90, cam3:45; cam2:-90 off (D33). Runtime sigma 0.07–0.10 mm per group, including half the inner/outer pass difference.
Limits: the mm level is print 2's baseline calliper (±0.1 mm); offsets do not carry across prints (cam1 moved 0.34 mm); the knee may not always be the wall pitch (print 1's single wall has one at ~1.1 mm). The first safety check (bootstrap of the gain over bands) fired from a resampling bug and was replaced by the rule above.
Verdict: adopt, absolute (D31), with the ceiling rule (D29). Because: it reads the narrowing the controller must correct to ~0.07 mm; the blind range is known and handled instead of hidden.

## printer-model-physics | 2026-10-20 | timebox: one overnight unit
Hypothesis: a small physics model fitted straight to the calliper by autodiff predicts held-out bands better than a constant width and tells the controller which lever works.
Method: `tools/model/fit_printer.py` (PyTorch; numpy runtime in `app/server/printer_model.py`, parity-tested). Width = soft minimum of the asked width (nominal × flow^β) and the melt limit (Q220 · e^(α·(T−220)) / (feed × layer height)); pressure lag and heater lag. Per-print offset, print 2's absolute level at low weight, defect bands as upper bounds. No learned residual, no previous-layer IR input.
Result: Q220 19.9 mm³/s (only a lower bound, ≥ ~18.5), α 0.018 /°C, β 0.46, pressure lag 0.84 s (data say ≲ 2 s), heater dead time 3.9 s + lag 4.7 s (14 nozzle steps). Leave-one-band-out RMSE:

| held out | model | constant per print |
|---|---|---|
| pooled (17 bands) | 0.137 mm | 0.169 mm |
| print 1 (8) | 0.174 mm | 0.204 mm |
| print 2 (9) | 0.091 mm (0.082 level-free) | 0.131 mm |

At print 2's conditions (asked 1.55 mm, melt limit 1.99 mm: not melt-limited) flow is the lever: ±0.07 mm per ±10 %. Speed and heating do nothing; cooling narrows only below ~210 °C. This agrees with the calliper (speed 60 % +0.1 mm, nozzle 200 °C −0.2 mm, flow 60 → 140 % 1.15 → 1.70 mm). The regime flips (speed and nozzle start to matter) if Q220 is ~22 % lower or the feed exceeds ~25.6 mm/s. It also flips after a nozzle drop to ~200 °C: the bead narrows (1.55 → 1.39 mm), more flow does nothing, and speed 90 % / 80 % restores 1.54 / 1.55 mm (model only, not measured on the printer).
Limits: β < 1 rests on print 2's flow-80 and flow-140 differences; β = 1 does as well or better on print 1 and pooled level-free, but not on print 2 (level-free 0.082 for β 0.46 vs 0.110 for β 1). Under β < 1 the flow knee sits at ~172 % flow against ~130 % by volume, so expect wasted flow steps near the top of the range. Volume-consistent form is future work.
Verdict: adopt for choosing and sizing levers, not for absolute width. Because: it beats the constant model in every held-out split and the controller's own checks catch a lever that stops working.

## controller-sim-bench | 2026-10-20 | timebox: one overnight unit
Hypothesis: the controller (increments with κ 0.5, deposit-time gating, holds) keeps a target without oscillating, never breaks a limit and does no harm when nothing is wrong, on a bench harder than its own model.
Method: `tools/sim/plant.py` realistic preset: settings land at confirmation 0.5–2 s late, lost results, 5 % gateway faults, three views switching every 10 s with ±0.15 mm offsets, width ∝ flow^0.5, blur floor, burst dropouts, clock skew. Criteria: (a) do no harm: no disturbance → median ≤ 1 command per 10 min, runs end within 0.08 mm; (b) clog recovery within 180 s; (c) limits, 2 s spacing and no-resend never violated. About 400 sim runs by the builder, ~260 realistic runs plus a 300 × 900 fuzz by the reviewer.
Result: (c) 0 violations. (a) median 1 command / 10 min, 19 of 20 runs end within 0.08 mm; (b) 20 of 20 within 180 s. Both (a) and (b) pass only with **learned relative view offsets** (zero mean, 60 s average): without them the controller chased per-view offsets (3.5 sends / 10 min, drift up to 0.16 mm). The cost is that a change common to all views is still seen, so a clog is not absorbed. Window 25 s, error must persist 10 ticks, trimmed mean; anchoring the model at the estimated operating point was neutral and stays off. On print 2's recorded frames with the real models: an estimate on 95 % of ticks, confidence ≥ 0.5 on 98.7 % of steady ticks, settling ~13 s median; only 63 % of the time has a usable view (D33). Replay is open-loop, so on a replay with Auto on the correct behaviour is one command, then a latched alarm, no resend (observed: flow 100 → 90).
Verdict: adopt. Rung 2 (replay + fake gateway) and 3 (container); the real closed loop is rung 4, not yet run.

## width-extractor-parity | 2026-10-20 | timebox: one overnight unit
Hypothesis: the in-app extractor (`app/server/width.py`, IR decoded in memory) reproduces the research estimator that was checked against the calliper.
Method: `tools/model/width_check.py` runs it over the recorded captures; compared per frame and per band with the research widths on print 1's legs.
Result: parity at the research-equivalent settings (window 16–26 px from the glow, ±1 px lateral search, no angle search): per-frame median difference 0.026 / 0.030 px on the two legs. The first defaults (window 18–28 px with an angle search) did not reproduce it. Review fixes that did not change widths on normal frames: confidence no longer depends on bead angle (cam0:135 window confidence < 0.5 in 2.7 % of windows, was 21.9 %); the "search at limit" flag judged on the final position (cam1, print 1: 83 % → 3.4 %); wide or ambiguous ridges rejected instead of read narrow; false "turning" rejects on diagonals fixed (+13 % usable frames on cam0/cam3, print 2). Nozzle pixel refit on all prints (cam1 lock-on on the neighbour wall 38 → 13 %). The hypotenuse view (cam0) agrees less well with the research numbers but is still the strongest gate group. Frame and motion clocks agree within ~0.1 s.
Verdict: adopt. Pre-demo step: refit each camera's nozzle pixel on the first minutes of the demo print (`width_check --fit-nozzle`); cameras shift between prints (cam1 ~1.7 px). Parked: interpolated profiles and finer bins near 45° (they change widths, so they need a camera refit).

## r13-widening-check | 2026-10-21 | timebox: one overnight unit
Hypothesis: a guard that only watches the estimate's level near the camera's ceiling (D29) stops blind widening when the IR baseline drifts; if not, each widening step has to show its effect before the next.
Method: realistic bench (`tools/sim/plant.py`) with the IR baseline drifted by -0.05 / -0.12 / -0.20 mm and a typed target of 1.50 mm (the knee), 20 seeds per row, before and after the response check (D39); then the demo scenario without drift to count false latches. Reviewer runs of the whole control path (X1, then R2) in between.
Result: **before** (level guard only): drift ≥ 0.12 mm hid the plateau under the guard's band; the controller kept widening to flow 130 % and the bead reached ~1.70 mm while the reading stayed below target, never latching. **After** (steps judged one at a time): widening latched in 20 of 20 runs per row. Blind steps before the latch: at most 3 at -0.05 mm (bead up to 1.615 mm), at most 1 at -0.12 and -0.20 mm (1.569 mm). Costs: each widening step takes 30–180 s to judge, clog recovery is 2–3× slower, and the demo scenario false-latches 1–2 times in 20 (safe, the operator re-arms). Bugs found on the way, each now with a regression test that fails on the old code: a stale pre-change estimate re-entered the settled check 15–30 s early (false latches); a widening step landing late was never judged; Auto off and on cleared a pending judgement; a hand-applied change in advice mode did not reset it. A stuck judgement (< 20 estimates per 180 s) holds widening until a narrowing step, a foreign change or a restart.
Verdict: adopt (D39). Because: it bounds the damage of an invisible plateau to a few steps at any drift, at the price of slower recovery. Open: bench numbers only; the printer's own response is untested (rung 4).

## demo-target-choice | 2026-10-21 | timebox: one overnight unit
Hypothesis: where the demo target sits relative to the camera ceiling decides how often the loop widens blind or stops on a false alarm; *Hold current width* at ~1.50 parks the loop on the knee and is the worst choice.
Method: two benches (the realistic X1 demo scenario and the hinge bench), 20 seeds each, operator nozzle drop mid-run; targets: Hold current (1.50), typed 1.45, typed 1.40. Measured: false latches (no real fault), blind widening steps, back in band, time to get there. Run again after the stale-estimate fix of `r13-widening-check`.
Result: X1 demo after the fix:

| target | false latches | back in band | median time |
|---|---|---|---|
| Hold at 1.50 | 2 / 20 | 19 / 20 | 126 s |
| typed 1.45 | 2 / 20 | 19 / 20 | 178 s |
| typed 1.40 | 1 / 20 | 20 / 20 | 80 s |

Hinge bench at 1.40: 0 / 20 false latches. Before the fix Hold at 1.50 let noise alone cause blind widening in 6 of 20 runs (up to 1.596 mm); 1.40 had none. A lower target only protects against drifts smaller than ceiling minus target (0.10 mm at 1.40); larger ones are left to the widening check.
Verdict: adopt as the recommendation: type 1.40 mm, drop the nozzle to 200 °C (195 °C for a bigger dip) and let the controller answer with speed; do not use Hold at the knee. Pending a check on the printer: the bench is a model, and the nozzle-drop response comes from the fitted printer model.

## camera-gain-per-view | 2026-10-21 | timebox: one evidence round
Hypothesis: a separate gain per camera view (and a height term) predicts held-out bands better than the shared gain in the installed camera model; the dataset review saw cam1:-90 respond less to flow than cam0:135 and cam3:45.
Where: a candidate fit on top of `tools/model/fit_camera.py`, not installed; leave-one-band-out against the calliper, plus an estimator replay of print 2.
Result: no. Leave-one-band-out error on print 2 below the knee: shared gain 0.070 mm, gain per view 0.076 mm (worse), gain per view + height 0.061 mm, shared gain + height 0.049 mm (6 held-out rows, less than the calliper's own scatter). cam1:-90's gain is shaky (bootstrap 95 % 0.70–2.04; 1.49 outer pass, 0.96 inner pass); its low response in the gate came mostly from the inner/outer pass mix, and at runtime it responds about 1:1. Replay with the installed model: baseline − flow 80 reads 0.175 mm (calliper 0.150), baseline − nozzle 200 reads 0.175 mm (calliper 0.199). A typed 1.40 mm target settles at about 1.40 mm real (1.38–1.42 by camera view).
Verdict: drop (keep the shared gain). Revisit a height term once the runtime knows Z and the pass, and with more than two prints.

## live-square-bands | 2026-10-21 | timebox: the square print
Hypothesis: on a live print the deployed app's width follows setting changes in the direction and roughly the size the printer model predicts, in every camera view.
Method: 45°-rotated square, two walls, 0.5 mm layers, app 0.4.x on the device (advice only). A script set flow / speed / nozzle every 5 layers through the writer and logged readbacks; the app's estimates came from its WebSocket, per-frame IR widths from `width_check` offline. Verdicts on the app's median over each band's settled layers against the band before (or the reference baseline).
Result: device and offline agree within 0.02 mm. IR px shift per view (cam0:135 / cam1:-135 / cam2:-45 / cam3:45) and the app's mm:

| band | IR px shift | app mm shift | printer model | verdict |
|---|---|---|---|---|
| flow 80 % | −0.74 / −0.64 / −0.42 / −0.70 | −0.28 | −0.15 | narrower, PASS |
| flow 120 % (full speed) | +0.06 / +0.02 / +0.02 / −0.06 | 0.00 | +0.13 | flat: melt-limited |
| baseline again | −0.26 / −0.27 / −0.13 / −0.24 | −0.10 | 0 | FAIL (drift) |
| nozzle 200 °C | −0.27 / −0.43 / −0.59 / −0.55 | −0.07 | −0.16 | narrower, PASS |
| nozzle 200 °C + speed 80 % | −0.01 / −0.12 / −0.31 / −0.02 | +0.08 (+0.15 vs nozzle-only) | 0 | recovers, PASS |
| flow 130 % + speed 80 % | +0.42 to +0.87 in all 4 | at the 1.50 ceiling | wider | wider, PASS |
| flow 115 % + speed 85 % | +0.42 / +0.43 / +0.29 / +0.42 | +0.16 | +0.10 | wider, PASS |
| flow 140 % + speed 70 % | +0.91 / +0.83 / +0.87 / +0.82 | +0.23 | +0.26 | wider, PASS |
| baseline after them | +0.00 / −0.01 / +0.02 / +0.03 | +0.01 | 0 | PASS |

An estimate was on screen ~48 % of the time (morning, ~1 IR frame/s): 69 % on sides with a calibrated view, 35 % around corners, 22 % on sides with none. The baseline reading drifts down with height (1.59 mm at Z ~6, 1.42 at Z ~45).
Calliper (one reading per band, two-wall thickness minus the 1.50 mm pitch; two first readings were mis-aimed and re-measured): on the 11 bands from 1.40 to 1.70 mm the deployed model (v2, D43) reads +0.04 mm high on average, RMS 0.12 mm (normal bands +0.08). Beads well above target are under-read: flow 130 % + speed 80 % is 2.17 mm real vs 1.76 app, flow 140 % + speed 70 % 2.40 vs 1.81 (only 5 % of frames rejected as too wide; the IR grows less than the bead up there). Flow 80 % read 1.45 real vs 1.33 app.
Verdict: adopt as live proof (rung 4) of Level 1 for narrowing and widening. The 1.50 mm knee of `camera-model-hinge` was the hot end's melt limit, not the camera: when the melt demand fits (flow % × speed % ≲ 100 %), the IR widens with flow past 1.50 mm. Open: the height drift (calliper shows none beyond its scatter), and only two of the square's four directions are calibrated.

## auto-live-trials | 2026-10-21 | timebox: two runs on the square
Hypothesis: with Auto on (sends enabled), the controller holds the bead ≥ 1.30 mm (walls touch) and within 1.40–1.50 mm through an operator nozzle drop 220 → 200 °C.
Method: target 1.40 then 1.47 / 1.45 mm; 5 min settle, 8 min at 200 °C, 4–5 min back at 220 °C; judged on the app's own estimates (share ≥ 1.30, share in band, commands sent).
Result: before the drop 100 % ≥ 1.30 in both. Trial 1: speed 100 → 90 % 2 min after the drop, one step; 80 % ≥ 1.30 (below on and off for ~6 min), 28 % in band. Trial 2: speed 95 % 2 min after the drop, then nothing; 43 % ≥ 1.30, 7 % in band. After the nozzle went back: 90–98 % ≥ 1.30, 67–80 % in band. Causes: half the ticks had no estimate (uncalibrated sides, corners) and the off-target counter reset on each gap; low confidence near the 1.50 mm knee; the knee's widening check (D39) waits 30–180 s per step.
Verdict: drop the knee and count persistence in time (0.5.0); live trial 3 to judge it.

## live-cube-auto-test | 2026-10-21 | timebox: one 35 min run
Hypothesis: on a fully infilled part, app 0.5.2 (Auto from layer 3, operator hold 60 s) keeps the bead ≥ 1.30 mm and recovers to 1.40–1.50 mm after an operator nozzle drop (220 → 200 °C) and a flow drop (100 → 85 %, a stand-in for a clog).
Method: 45°-rotated cube (~155 mm side), 100 % infill, all infill lines along one diagonal, ~12 min per layer. A script acted as operator: 3 min measuring only, Auto on at layer 3 (target 1.50 mm), 3 min steady, nozzle 200 °C for 8 min, back to 220 °C for 4 min, flow 85 % for 8 min, 2 min watch. Judged on the app's own estimate.
Result: a width was on screen 67–77 % of the time with Auto on (one view, cam3:45), but single readings scatter ~3× more than on walls (estimate ± 0.10–0.18 mm vs ~0.03). Nozzle fault: below 1.30 mm 98 s of 8 min (worst ~1.17); Auto chose speed (90 → 80 → 70 %) but only after 266 s, because the deadband (2 × ±) was 0.2–0.35 mm; after the second step the width was back in 1.40–1.50 within ~20 s and held. Flow fault: Auto's only move was speed 82 → 90 % (answering the wide reading left from the previous phase); when the width then read ~1.05 mm it held on low confidence (0.47–0.50 < 0.5) and never raised flow. Report, graphs and CSVs: local `pitch/live-test-cube/` (workspace).
Verdict: park the controller tuning as is; next: deadband and confidence rules that account for many noisy readings, a look at why infill readings scatter (neighbouring hot line 1.5 mm away), and faults separated by a settling period.

## floor-rules | 2026-10-21 | timebox: one fix round before the next cube
Hypothesis: letting Auto act below 1.30 mm inside the noise band, on lower confidence and with bigger steps answers real faults on noisy infill much sooner without unsafe commands.
Method: replay of the cube test's recorded estimates and readbacks through the deployed and the new controller (open loop, first action only; the recorded 25 s estimate is counted as fresh 15 s after a change), and the realistic bench at 2 Hz with correlated frames at walls (1×) and infill (3×) frame noise: nozzle drop, clog 85 % and steady prints (10–30 seeds). After the review: steps 1× vs 2×, confidence 0.35 vs 0.45, and a steady-drift measure (mean bias and time off target on 30 steady prints).
Result: replay: nozzle fault first action +58 s (deployed: none in the replay; live +265 s); clog dip +40 s (deployed: never, held on confidence 0.48–0.50); the same with 1× and 2× steps and confidence 0.45. Bench at 3× noise, final defaults (1×, 0.45), 20 seeds: clog fixed within 180 s 16/20 (2×: 19/20; deployed 6/20), nozzle drop first action 68 s (75), below 1.30 mm 5 s (11), back within 0.05 mm 131 s (142); unneeded moves on steady prints 94 per 200 min (deployed 44). Steady drift, 30 × 10 min at 3×: mean bias +0.074 mm (2×: +0.111; deployed +0.019), off target by > 0.10 mm 22 % of the time (2×: 52 %; deployed 2 %), flow reached 130 % in the worst run (also with 1×). At 1× noise nothing gets worse (17 vs 15 moves per 100 min). No safety violations; the step checks allow a bigger step only on the deciding tick, widening a bead narrower than wanted. A 20 s hold without the 1.35 mm release made the steady cost 58.
Verdict: adopt (D48) with 1× steps by default (2× ratchets the bead wide on noisy prints; `CONTROL_FLOOR_STEP_FACTOR` for a test); count unneeded moves and the drift on the next cube's steady stretch.
