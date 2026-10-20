# Control: Correct (Level 2) and Adapt (Level 3)

In plain words: we can change only two printer settings, the flow multiplier and pressure advance, at most once per
second between them. Flow is the real width lever. Pressure advance only matters at corners and probably pauses the
print every time it changes, so we touch it rarely. Between sending a command and seeing its effect in the camera
there are several seconds of delay, so a normal fast controller would oscillate; the design below waits for the effect
before acting again. **Nothing is sent to the printer without the team lead's explicit OK**; the default is advisory
mode (show the recommendation, don't send it). The command path is not live yet.

Tags: **organiser** = from the organisers' slides or mail; **measured** = live data; **estimate** / **sim** = our
reasoning or toy simulations (numbers show behaviour, not real performance).

## Levers (organiser)

| Setting | Range | What it does |
|---|---|---|
| `flow_percent` | 0–200 %, absolute value | Extrusion multiplier; scales material per mm of travel. Probably sent to the Duet as `M221` (estimate) |
| `pressure_advance_seconds` | 0–1 s, absolute value | Extra extruder motion proportional to extruder acceleration; fixes bulges/gaps at corners, starts and stops. Probably `M572` (estimate) |
| Rate limit | **max 1 correction per second across both settings** | Feedback: a request result plus a readback of the setting in force; the physical effect is ours to measure |

- Sent with a **separate writer account** (not issued yet) to `demex/hackathon/mini/extrusion/request`. Payload schema,
  readback topic and what happens above the rate limit are unknown.
- **Not controllable:** nozzle temperature, extruder motor RPM, print speed. The organisers said acceleration or speed
  might be opened later.
- **Status (measured, all of 2026-10-20 so far):** gateway `state = "SAFE"`, `permits_outputs = false`,
  `command_gate_closed = true`. The organisers' slides said the command route was implemented but target activation and
  rehearsal were pending.
- LEAM's process service has its own speed controller (`pid_state.speed_substrate`, output 50–200 %), currently
  disabled. If it runs, speed changes under us: read `speed_override_percent` every cycle.

## Latency and dead time (estimates until the step test)

| Part | Estimate | Basis |
|---|---|---|
| Actuation: gateway hop + Duet move queue | 0.4–1.2 s | firmware queue depth on a delta at ~10 mm/s; maybe ~1–1.6 s at the 7.5 mm/s we saw |
| Material response τ | 0.1–3 s | literature range; unknown for this printer |
| Transport: nozzle → measured spot | 0.2–2 s | measurement windows sit ~2–15 mm behind the nozzle; 7.5 mm/s |
| Camera: age at publish + network | ~0.1 + ~0.12 s | measured |
| Our processing | 0.05–0.3 s | estimate |
| **Total dead time** | **~1.5–4.5 s, plus τ** | |

Handle transport in **arc length**, not time: tag each width sample with the time the nozzle was at that spot (from
telemetry position history). That removes the speed dependence of the transport delay.

**Pressure advance (estimate from the firmware source):** a PA change waits for the printer to stand still before it
applies, so each change is probably a visible pause in the bead. Never use PA as a 1 Hz lever; change it only between
layers or during travel, after confirming with the organisers.

## Level 2: gated incremental integral controller on flow

Why this shape: with dead time longer than the response, a proportional or derivative term reacts to measurements that
do not yet contain the last correction. In simulation an ungated PI chattered (47–97 command reversals per 4 min); the
gated controller stayed bounded (sim). With gating, a correction gain κ = 0.5 converges without overshoot as long as our
gain estimate is within 2× of the true gain (noise-free analysis).

```python
# runs on every accepted width sample (see cameras-and-calibration.md); flow only, PA untouched
class FlowLoop:
    base, F, t_cmd = 100.0, 100.0, -1e9          # % flow; F = last commanded value
    ghat, kappa, dead, step, band = 0.8, 0.5, 0.02, 5.0, 15.0
    La, tau, min_gap, nmed = 1.0, 1.5, 2.0, 2    # replace La/tau/ghat with step-test results

    def on_sample(s, p, now):                    # p: width, conf, target, t_dep (deposit time), ctx (telemetry)
        if not gate_ok(p): return None           # see Safety; also re-gate after speed or camera changes
        if p.t_dep < s.t_cmd + s.La + 2 * s.tau: return None   # effect of the last command not visible yet
        s.buf.append(((p.target - p.width) / p.target, p.conf))  # >0 = too narrow
        if len(s.buf) < s.nmed: return None
        e = s.kalman.update(median(e for e, _ in s.buf), conf=mean(c for _, c in s.buf)); s.buf.clear()
        if abs(e) < s.dead or now - s.t_cmd < s.min_gap: return None
        du = clip(100 * s.kappa * e / s.ghat, -s.step, s.step)          # percentage points
        F_new = clip(s.F + du, s.base - s.band, s.base + s.band)
        if abs(F_new - s.F) < 0.25: return None
        s.F, s.t_cmd = F_new, now
        return {"flow_percent": round(F_new, 1)}  # absolute value: a lost QoS 0 message is repaired by the next one
```

| Parameter | Start | Notes |
|---|---|---|
| Error | relative: (target − width) / target | independent of the target width |
| Gain estimate ĝ | 0.8 | width changes ~0.5–1.0 % per 1 % flow; replace with the measured value |
| κ | 0.5 (0.3 if noisy) | |
| Deadband | 2 % of width | stops noise-driven chatter |
| Step cap | ±5 % flow per update (3 % on the first rehearsal) | |
| Bounds | baseline ±15 % | the organisers' 0–200 % is far wider than sensible |
| Spacing | ≥ 2 s between commands | leaves headroom under the shared 1/s limit |
| Accept a sample | confidence ≥ 0.6, extruding, straight segment, speed stable ±10 % for 3 s, same camera pair, inside calibrated area, abs(error) < 0.4 | |
| No data | hold; after 15 s decay towards baseline by 10 %/min | |

Expected response: ~3 cycles of 5–8 s = 15–25 s (≈ 110–190 mm of path at 7.5 mm/s). Corners are Level 3's job.
A writer thread sends at most one request per spacing interval, coalesces to the newest value, checks the readback after
~1.5 s, resends once, and raises an alarm if it still differs after 3 s.

## Level 3: learn recurring corners

Two levers, two learned objects. Flow can change at most once per second, so a flow table has ~5–10 mm cells; a corner
disturbance is 5–15 mm long, so we can remove its net excess or deficit, not its exact shape. PA changes pause the
print, so PA is one scalar per layer or job.

```python
class CornerLearner:                              # table persisted per program name (scalars only, never IR)
    gamma, lam, dead, cap = 0.4, 0.97, 0.025, 5.0

    def end_of_layer(s, obs):                     # obs: cell -> [relative errors]; cell = arc-length bin >= 5 mm
        med = median(median(v) for v in obs.values())          # global bias belongs to Level 2
        for c, v in obs.items():
            r = weighted_median(v) - med
            upd = 0.0 if abs(r) < s.dead else clip(-s.gamma * 100 * r / ghat, -s.cap, s.cap)
            s.tab[c] = clip(s.lam * s.tab.get(c, 0.0) + upd, -15, 15)
        for c in set(s.tab) - set(obs): s.tab[c] *= s.lam       # unseen cells decay
        s.maybe_update_PA(obs)                    # bulge index: wide entry / thin exit = PA too low; every >= 3 layers

    def schedule(s, next_cells):                  # issue each command La before the nozzle reaches the cell
        return [(t_reach(c) - La, clip(level2.F + s.tab.get(c, 0.0), lo, hi)) for c in next_cells]
```

- Key: `(program, cell)`, with cells counted in arc length from the layer start (layer starts from Z steps). Fallback:
  `(program, x/4 mm, y/4 mm, heading octant)`.
- γ 0.3–0.5: higher γ learns faster but injects more measurement noise into the table. Toy simulation: corner RMS error
  ~3× smaller after 6–10 layers (sim).
- Feed-forward timing matters: a 0.6 s timing error halved the benefit in simulation. Measure La first.
- Fallback if timing cannot be made reliable: report the corner bulge map and a recommended PA value.
- Needs repeated geometry: ask the organisers for the test-print G-code (corner positions, layer count).

## First on-site test: flow step (10 min, needs the organisers' OK and the writer account)

1. Print a straight of ≥ 100 mm at nominal flow. Record (`tools/mqtt/record.py`).
2. At a logged time, command flow 100 → 115 %. Hold 20 s. Command 115 → 100 %.
3. Log command time, readback time, and the width profile against deposit time.
4. Read off: gain g (width ratio ÷ flow ratio), dead time La (first visible change), τ (63 % time), up/down asymmetry.
5. Repeat at a second speed override if the organisers use one. Also ask for the Duet firmware version and the nozzle →
   camera-view distance.

## Safety

- **Nothing is sent without the team lead's explicit go-ahead.** Default mode is advisory: show "recommend flow 104 %,
  PA unchanged" with reason and confidence.
- Hold (send nothing) when: `gateway.state != "RUN"`, `permits_outputs` false, `command_gate_closed` true, telemetry
  expired, camera pair changing, low confidence, readback ≠ request for > 3 s, or a large error persists > 5 s (more
  likely a measurement problem: alarm instead of pushing flow).
- Hard limits: baseline ±15 % flow, ≤ 5 % per step, ≥ 2 s spacing, PA frozen in Level 2.
- A STOP control sends the baseline and switches to advisory. On clean shutdown send the baseline; ask whether the
  writer account may set an MQTT last-will.
- First automatic run only on a sacrificial single-layer straight. Rehearse STOP. Log every request, readback and gate
  decision (RGB and scalars only).
- Do not change machine networking or start prints; the organisers operate the printer.
