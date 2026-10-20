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
