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
