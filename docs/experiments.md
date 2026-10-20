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
