# How width estimation works

How the app turns the IR camera's picture into a bead width in millimetres, with an uncertainty and a confidence.
Describes the app as deployed on 21 Oct (0.4.1); changes coming with 0.5.0 are marked **0.5.0** and are still in
review. The controller that acts on the estimate is in `prototype.md` and `control.md`.

## In plain words

**What the camera sees.** The IR camera sees heat, not shape. Freshly laid plastic is much hotter than the layers
under it, so the newest bead shows up as a bright stripe trailing behind the nozzle. A wider bead makes a wider stripe.

**What we measure.** For each IR frame we work out where the fresh bead must be, from where the nozzle is in the image
and which way the printer is moving. A short stretch of bead about 15–20 mm behind the nozzle (past the nozzle's own
glare) is cut across, giving a brightness profile with one hump. The width is the distance between the two steepest
slopes of that hump: where the bead's edge is, the brightness changes fastest. This is done to a fraction of a pixel,
because one IR pixel is about 0.7–0.9 mm on the part.

**From pixels to millimetres.** Pixels are not millimetres, and the glow makes a bead look wider than it is. We printed
calibration parts with known settings, measured them with a calliper, and fitted a simple straight-line model per
camera and direction: so many pixels means so many millimetres. The model also corrects for things that change the
glow without changing the bead, mainly nozzle temperature (a cooler nozzle makes the same bead look narrower).

**From many frames to one number.** A single frame is noisy. The app keeps the last 25 seconds of frames, throws out
the odd ones, lets each camera keep its own small bias, and averages the rest into one number with a ± (how much the
number would wobble from noise alone). A separate confidence value (0 to 1) says how much to trust it.

**When it says nothing.** The camera can only measure a straight, hot, freshly laid bead that it can see. Right after a
corner, when the printer is not extruding, in a direction we have no calibration for, or when the picture is
ambiguous (two beads side by side), the frame is skipped and the reason is recorded. With too few good frames the
dashboard shows "no estimate" instead of a guess. That is on purpose: a missing number is better than a wrong one.

**How good it is.** Against the calliper, each camera's calibration is good to about ±0.07–0.10 mm. Live on the
square print it clearly follows real changes: more or less flow, a cooler nozzle and slower printing all moved the
reading in the direction physics predicts, though the sizes differ from our printer model by up to 2× either way (the
calliper will tell which one is off). The app on the device gives the same numbers as our offline tools
(within 0.02 mm). The weak spot is coverage: on the square print a number was on screen only about half the time.

## The pipeline at a glance

```
IR preview (trailing camera, newest frame only, decoded in memory, never stored)
  │  + motion state at the frame's acquisition time (heading, speed, distance since the last turn, extruding?)
  ▼
width.py      one frame → bead width in IR px, per-frame confidence, view ("cam1:-90"), deposit time
  │           or a reason it could not measure (turning, not_extruding, ambiguous, ...)
  ▼
camera_model.py   px → real mm for that view (nozzle °C, speed) + calibration sigma
  ▼
estimator.py  last 25 s of samples → one estimate: mm, ± sigma, calibration sigma, n, confidence
  ▼
dashboard (width ± sigma, confidence light) and controller (Auto)
```

## Step 1: one frame → width in pixels (`app/server/width.py`)

### Which frame, which moment

LEAM publishes one IR preview (160×120 JPEG) from the camera currently trailing the nozzle; which physical camera that
is follows the travel direction (`camera_index` on every frame). Since 21 Oct ~13:20 the IR comes at ~2 frames/s (it
was ~1 before LEAM removed the RGB feed). The app decodes each frame in memory, measures it and drops it.

Times are on LEAM's clock: a frame's time is its `source_utc_ns`, the acquisition time, about 0.85 s before it is
published. The motion state (heading, speed, path history) is looked up at that time, not at arrival. Each sample also
gets a **deposit time**: when the measured stretch of bead was laid down, interpolated back along the recorded path
(≈ 0.8–1 s before the frame at 20 mm/s). After a setting change the controller judges only beads laid after it.

### Where the bead must be

From the travel heading and each camera's geometry model (a homography from the image to the print plane, refined
on our prints), the app predicts the bead's axis in the image: a line from the nozzle pixel, opposite to the travel
direction. Then it slides that line sideways by up to ±1 px in 0.25 px steps and keeps the position that best matches a
ridge. That small search absorbs aim errors; it is kept small on purpose, because on a two-wall print the neighbouring
wall runs only ~2.5 px away.

Each sample is labelled with its **view**: camera plus heading rounded to 45°, e.g. `cam1:-90`. Calibration is per
view, because each camera sees each direction at a different angle.

### When it does not try

| Reason | Meaning |
|---|---|
| `no_motion` | no motion state, or slower than 2 mm/s, so no heading |
| `not_extruding` | moving without extruding (travel moves) |
| `axis_out_of_frame` | the bead would run more than 75° across or away from the camera, or off the image |
| `turning` | the straight stretch behind the nozzle is too short: the heading must stay within 20° for the measured stretch plus a 5 mm margin, ≈ 23–29 mm depending on the view |
| `low_contrast` | no ridge near the predicted axis, or less than 15 grey levels above the background |
| `too_wide` | the hot area is wider than one bead can be (> 6 px at half height, or > 4.5 px edge to edge): saturated or merged with another bead |
| `ambiguous` | another ridge within 3.5 px: no telling which one is the fresh bead |
| `no_edges` | an edge is too faint (slope under 15 % of the ridge height) or stuck at the search limit |
| `bad_image` | the JPEG did not decode to the expected size |

The turning rule also decides what curved parts can be measured: on a circle the heading turns 20° over an arc of
0.35 × radius, so a measurable circle needs a diameter of roughly 150 mm or more.

### The cross-section

Every pixel 16–26 px along the axis behind the nozzle glow is assigned its distance across the axis. Those distances
are pooled in 0.25 px bins (a sub-pixel histogram of brightness), averaged per bin and smoothed with a 0.5 px Gaussian.
Pooling ten pixels' worth of bead length is what makes sub-pixel edges possible on a 160×120 image.

```
 brightness
     ▲              ╭──╮
     │             ╱    ╲          left edge  = steepest rise
     │            ╱      ╲         right edge = steepest fall
     │  ─────────╯        ╰─────   (each refined to a fraction of a bin)
     └───────────┼────────┼──────▶ distance across the bead (px)
                 └─ width ┘
```

### Finding the edges

1. Find the ridges (local maxima) in the profile; maxima closer than 2.5 px with only a shallow dip between them are
   one bead (a hot core can show two peaks).
2. Take the ridge **nearest the predicted axis**, not the brightest one: on a two-wall print the other wall can be
   brighter.
3. The ridge's height is measured against the background a few px to each side.
4. Left edge: the steepest rising slope left of the ridge; right edge: the steepest falling slope right of it. If a
   neighbouring ridge is close, the search stops at the dip between them. Each edge position is refined with a parabola
   through the slope's peak, so it lands between bins.
5. Width = right edge − left edge, in IR pixels. Real beads read 2–3.5 px.

These defaults reproduce the research estimator that was checked against the calliper on the first prints.

### Per-frame confidence

`confidence = contrast × symmetry × support`, each 0..1:

- contrast: how far the ridge rises above the 15-grey-level minimum (full at 60),
- symmetry: weaker edge slope ÷ stronger one (a lopsided profile is suspicious),
- support: how many pixels actually fell near the bead, against what a clean frame gives.

## Step 2: pixels → millimetres (`app/server/camera_model.py`)

Per view `g`:

```
px = k_g · mm  +  g_T · (nozzle_°C − 220)  +  s · (speed_mm/s − 20)  +  offset_g
```

solved for mm. The fit lives in `app/models/camera_model.json` (local only, not in git), made by
`tools/model/fit_camera.py` from calibration prints whose bands were measured with a calliper.

- **Scale `k_g`.** Starts from the geometric scale (pixels per mm across the bead, from the camera geometry at that
  view) times one factor κ fitted across all views: κ ≈ 1.70, so the glow's edges move about 1.7× as much as pure
  geometry predicts. Current values: 1.9–2.5 px per mm, so one IR pixel ≈ 0.4–0.5 mm of real width.
- **Offset `offset_g`.** Where each view's zero sits: the glow and the camera's aim make each view read a little wide
  or narrow. The aim part can be refreshed from any straight print without a calliper; the scale cannot.
- **Temperature `g_T` ≈ +0.011 px/°C.** A 20 °C cooler nozzle makes the same bead look ~0.2 px (≈ 0.1 mm) narrower;
  the model adds that back, so a cooler nozzle is not mistaken for a narrower bead.
- **Speed `s`.** Fitted, negligible (−0.002 px per mm/s).
- **Uncertainty.** Each view carries its calibration sigma (0.07–0.10 mm today), reported as `model_sigma_mm`, apart from
  the random noise.

| View | Scale (px/mm) | Frame-to-frame noise (px) | ≈ noise (mm) | Calibration sigma (mm) |
|---|---|---|---|---|
| `cam0:135` | 2.52 | 0.28 | 0.11 | 0.09 |
| `cam1:-90` | 1.88 | 0.17 | 0.09 | 0.10 |
| `cam3:45` | 2.47 | 0.10 | 0.04 | 0.07 |

Views not in the file give a pixel width but **no millimetres**, so they do not feed the estimate. That covers other
camera/direction pairs and `cam2:-90`, which is off because its outer-wall pass did not follow flow changes (D33).

The file records the geometry version and a hash of the extractor settings its widths came from. Widths made with
other settings don't belong to the model, and the app refuses to mix them.

### The 1.50 mm "ceiling", and why it goes away (0.5.0)

The deployed model has a **knee** at 1.50 mm: above it the model assumed the camera stops seeing the bead get wider,
because the widest calibration band (flow 140 %) read no wider than the 1.50 mm band. Near and above the knee the app
shows "≥ 1.50 mm" and lowers its confidence.

On 21 Oct the square print pointed to the **extruder's melt limit** instead: at full speed the hot end cannot melt much
more than ~20 mm³/s, so more flow % may not make a wider bead. With flow 130 % and speed 80 % (melt demand ≈ 104 %) the
IR width rose by +0.42 to +0.87 px in all four views, so the camera can see beads wider than 1.50 mm.

**0.5.0** (candidate, in review) fits the camera model as a plain straight line without a knee, leaving out bands
where the asked-for flow exceeded what the hot end can melt. It has no ceiling, so wide beads read as numbers; its
calibration sigma is 0.09–0.11 mm, and on narrow beads it is about as accurate as the deployed model (within the
calliper's own scatter). The evidence is not one-sided: on the calibration print alone, two baseline-like bands that
the calliper read at 1.60 mm still favour a knee. The case for the straight line rests on the live square (three wide
bands that widened in every view, see below), and the calliper check of those bands is still open.

## Step 3: many frames → one estimate (`app/server/estimator.py`)

1. **Window.** Samples from the last 25 s. No estimate with fewer than 5 samples, or if the newest is older than 5 s
   ("stale").
2. **Per view: trimmed mean.** The single-frame noise is learned from the last 120 samples (median absolute deviation
   × 1.48). In each view, samples further than 2.5 noise-widths from the view's median are dropped and the rest
   averaged. That view's sigma is noise ÷ √n.
3. **View offsets.** Even calibrated, views disagree a little. Each view's offset relative to the others is learned
   slowly (60 s time constant) and removed. The offsets average to zero, so the overall level stays the calibration's
   and the learning cannot drift the absolute width.
4. **Combining views.** Inverse-variance weighted mean: quieter views count more. If the views disagree by more than
   their noise explains, the sigma is widened (by up to 1.2× once the offsets are learned).
5. **Two uncertainties.** `sigma_mm` is the random part, which shrinks as frames accumulate. `model_sigma_mm` is the
   calibration error shared by every frame of a view, which no amount of averaging removes. They are reported
   separately because they mean different things: sigma says whether a change is real, model sigma how far the absolute
   number could be off.
6. **Confidence** = mean per-frame confidence × min(1, n ÷ 10) × a penalty when under half the samples are inside the
   model's calibrated range × 0.5 until the noise scale has been learned.

At ~2 frames/s the window holds up to ~50 samples; in practice turns and skipped frames leave fewer. A typical live
estimate is ±0.02–0.04 mm random, ±0.07–0.10 mm calibration. Being a 25 s window, a sudden real change shows up
gradually: about halfway after ~12 s and fully after 25 s. **0.5.0** re-tunes the sample thresholds that were set for
1 frame/s.

## What the operator sees

- **Width** `1.46 ± 0.03 mm`: estimate and random sigma. Hover for the calibration sigma.
- **Confidence light** with the value: green ≥ 0.7, amber ≥ 0.4, red below.
- `n 23`: samples behind the number.
- **"no estimate"** or **"estimate stale"** instead of a number when step 3 has too little to go on.
- **"≥ 1.50 mm"** only at the knee of the deployed model; gone in **0.5.0**.
- **0.5.0**: a red **"below 1.30 mm: walls may not touch"** alarm, the width below which neighbouring walls stop
  touching.

## How it did live (21 Oct, 45°-rotated square, two walls)

| Check | Result |
|---|---|
| Device vs offline tools on the same frames | same within 0.02 mm |
| Flow 100 → 80 % | −0.28 mm (printer model predicted −0.15; calliper pending) |
| Nozzle 220 → 200 °C | −0.07 mm (printer model −0.16) |
| Nozzle 200 °C + speed 80 % | +0.15 mm back against nozzle 200 °C alone (slower printing lets the cooler hot end keep up) |
| Flow 130 % + speed 80 % | IR widened +0.42 to +0.87 px in all 4 views: wider beads are visible |
| Flow 115 % + speed 85 %, then 140 % + 70 % (both within what the hot end can melt) | IR widened +0.29 to +0.43 px, then +0.82 to +0.91 px, in all 4 views; the baseline after them came back within 0.03 px |
| Time with a number on screen (morning, ~1 frame/s) | ~48 %: 69 % on calibrated sides, 35 % around corners, 22 % on sides with no calibration for that direction |

## Known limits

- **Coverage.** Each corner costs ~25 mm of path before measuring resumes, and only calibrated views produce mm.
  More views come from a calibration print that runs in more directions (or a circle); 2 frames/s helps by filling the
  window faster.
- **Absolute accuracy** rests on one calibration session. Changes are tracked well; the absolute level carries
  ±0.07–0.10 mm until more calliper data arrives.
- **Delay.** The measured bead was laid ~1 s before the frame, the frame is published ~0.85 s after it was taken, and
  the 25 s window smooths on top. Fine for holding a width, too slow for single corners.
- **Two-wall prints.** A neighbouring bead closer than 3.5 px makes frames `ambiguous`; beads merged with a neighbour
  read `too_wide`.

## Where to look

| What | Where |
|---|---|
| Per-frame extractor, reject reasons, defaults | `app/server/width.py` |
| Camera geometry (image ↔ print plane) | `app/server/geometry.py` |
| px ↔ mm model | `app/server/camera_model.py`, fitted by `tools/model/fit_camera.py` |
| Rolling estimate | `app/server/estimator.py` |
| Wiring, clocks, what is sent to the dashboard | `app/server/pipeline.py` |
| Widths per frame from a recording (offline) | `tools/model/width_check.py` |
| Evidence and verdicts | `experiments.md`, decisions D21–D41 in `decisions.md` |
