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
