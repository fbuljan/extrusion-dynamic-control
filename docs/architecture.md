# Architecture: width control on the edge device

Agreed by the team on 20 Oct 2026 (decisions D14–D20). One app on the edge device estimates the real bead width from
the IR camera and keeps it at an operator-set target by adjusting the printer's settings.

What was actually built overnight, and where it differs from this plan: the last section and `prototype.md`.

```
IR frame ─► width extraction ─► IR px ─► camera model (reversed) ─► real width estimate (mm)
                                   (nozzle temp, speed)                     │
operator target (mm) ───────────────────────────────────────────────► error ┘
                                                                            │
current settings + context ─► printer model (DDSP-style) ─► best change ─► printer (MQTT, ≤ 1 command/s)
```

## Parts

1. **Width extraction** (built, `scratch` prototype): bead width in IR pixels at a fixed spot behind the nozzle, per
   camera; median over the last 10–20 s. Only views shown to be reliable are used (currently the two legs of the test
   triangle; the hypotenuse view drifted).
2. **Camera model** (sensor): what the IR shows for a real bead.
   `IR px = k · real mm + g · (nozzle °C − 220) + s · (speed % − 100) + offset`, per camera. Fitted once from the golden
   samples (calliper readings of the band prints). At runtime it runs backwards: IR px → real mm. It keeps the
   controller from "fixing" glow: hotter nozzles and slower speeds make the bead look wider in IR.
3. **Printer model** (process, DDSP-style): what real width the settings produce. A physics block
   (width = delivered plastic ÷ (speed × layer height); delivered plastic = min(what flow % asks for, what the hotend can
   melt at this temperature); delays: flow seconds, temperature ~40 s) plus a small learned part for what the physics
   misses. Inputs: flow %, speed %, nozzle °C, pressure advance (later), and context: previous-layer IR temperature,
   layer time, bed temperature, layer height. Written differentiably so all unknowns fit at once from recordings and the
   model can be inverted. Trained offline on the laptop; small enough for the device CPU.
4. **Live adaptation**: on the device only an offset adapts (slowly), absorbing drift such as a new spool or room
   temperature. No training service on the device.
5. **Controller**: updates every second; compares the real-width estimate with the target; asks the printer model for
   the cheapest change that closes the gap. Soft costs: flow cheapest, then speed, nozzle temperature last (slow, and it
   changes what IR sees) but still used when nothing else works (melt limit). Accounts for changes still taking effect,
   so it doesn't pile up commands. Limits: nozzle 190–250 °C, step limits per command, one command per second.
   Holds when data is stale, the gate is closed or confidence is low.
   - **Pinning:** a setting changed from outside is accepted as given; the controller restores width with the others.
   - **Auto switch** on the dashboard: OFF shows advice only, ON acts.

## Data

| Set | Purpose |
|---|---|
| Golden samples: band prints with calliper readings (runs 1 and 2) | Fit the camera model (absolute mm) and the printer model |
| Golden dataset, 60 min at fixed settings (milestone II) | Normal noise and drift; baseline for live adaptation |
| Outliers, 10 × 5 min with mixed 1–2-setting changes (milestone III) | Held-out test of the models on unseen combinations |
| Afternoon controlled sweep (20 Oct) | Extra response data (no calliper) |

After the golden samples nothing is measured by hand; everything comes from IR.

## Build order

1. Fit the camera model from golden samples (laptop).
2. Fit the printer model (laptop); check it on held-out runs.
3. Controller in the app in advice mode; deploy.
4. Auto mode with limits, pinning and the Auto switch.
5. Live offset adaptation.

Fallback, ready after step 1: a simple proportional controller on the estimated width.

## Not now

Pressure advance and corners (LEAM Level 3); training on the device; absolute accuracy beyond what the golden
samples support (baseline calliper spread so far ±0.15 mm).

## As built (21 Oct)

Plain version: the plan above runs end to end on a laptop and as a local container, on recordings of print 2 and a fake
gateway. Every part exists; several are simpler or more careful than planned, and one limit of the sensor reshaped the
controller. How it works, how to run and refit it, safety and limits: `prototype.md`. Decisions D21–D41, evidence in
`experiments.md` (`ir-follows-flow-gate` onward). Proof rung 3; not on the device yet, never sent to the printer.

What changed from the plan:

| Plan | As built |
|---|---|
| Width extraction from a `scratch` prototype, 10–20 s median | In the app (`width.py`), IR decoded in memory with Pillow; window 16–26 px behind the glow, ±1 px lateral search; turning rejected by distance, not time (D32). Averaging moved into the estimator |
| Camera model `px = k·mm + g·ΔT + s·Δspeed + offset` per camera | Per camera × heading group, with a hinge: linear below a knee of 1.50 mm (print 2's wall pitch), flat above. Gain 1.70× geometric. Pass-pooled; the long-side view is off (D23, D31, D33) |
| The IR width follows real width | Only below the knee. Above it a bead merges with the neighbour wall and reads the same: the camera is blind and the controller knows (D29). Every widening step must also show its effect before the next, at any level; a missing response latches widening (D39). Recovery steps take 30–180 s to judge |
| Printer model: physics plus a learned part, with previous-layer IR temperature, layer time, bed | Physics only, fitted to the calliper directly with a per-print offset; context is feed speed, layer height, line width. Learned part and extra inputs dropped: confounded with band order, 20 band-level points cannot validate them |
| Controller asks the model for the target width | Asks for 0.5 × the gap, from the current settings (D22); judged on beads deposited after the effect delay (D24); one lever in flight, ≥ 2 s apart (D27); holds when widening is wanted at the camera's ceiling or when a lever stops working |
| Target: a number | A target is stamped with the camera model it was set under; under another model the controller holds until it is set again (D40) |
| Pinning, Auto switch | As planned, plus: Auto starts OFF at every start (D21), a second key `CONTROL_SEND_ENABLED` (D28), no automatic retry (D26), "Hold current width" (D34) |
| Live offset adaptation (build step 5) | Not built (a real disturbance and a camera drift look the same without a reference). The estimator only learns zero-mean offsets between views |
| Limits: nozzle 190–250 °C, step limits | Flow 70–130 %, speed 60–100 %, nozzle 190–250 °C (D30); config, not code |
| Golden dataset (60 min) and outlier runs as training data | Not used so far: the fits rest on the two band prints (calliper); the afternoon sweep was tried for flow timing and left it undetermined |

Build order status: 1–4 done on the laptop (camera model, printer model, controller in advice mode, Auto with limits,
pinning and the switch); 5 not built. Open, the lead's call: deploying this version to the device (it only advises until
the second key is switched on) and the first supervised Auto test on the printer (rung 4).
