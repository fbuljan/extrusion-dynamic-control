# Cameras and calibration

In plain words: the cameras ride on the print head and look down at an angle at a ~40 × 30 mm patch beside the nozzle.
The nozzle sits in the top-left corner of every frame; the fresh line comes out of it and runs diagonally toward the
bottom-right, next to older lines 1.5 mm apart. Everything is out of focus. We can find and follow the fresh line, and a
first measurement gives ~0.9–1.0 mm, but 0.1 mm accuracy is **not shown yet**. The calibration files turn pixels into
mm, but they over-read by 4–11 % next to the nozzle, so we correct the scale using the known 1.5 mm line spacing.

Source data: the 5 min recording `rec1` (2026-10-20, 1792 RGB frames, Benchy at 50 % speed, one layer of ±45° infill at
1.500 mm pitch, 7.5 mm/s). Tags: **measured** = on real frames; **synthetic** = on simulated beads; **assumption**.

## What the RGB frames show (camera index 3 = calibration `rgb4`, measured)

| Item | Value |
|---|---|
| Image | raw 640×480 JPEG (quality 91); oblique view; far side (largest mm/px) is the top-left |
| Nozzle anchor | orange-rimmed dark disc in the top-left corner; rim circle centre (30, −25) px, radius 110 px, stable ±7 px over all frames (the camera is rigid with the head) |
| Fresh bead | leaves the rim at ~(108, 53) px and runs at ~44° toward the bottom-right; its centre lies 0.11–0.16 mm from the assumed path |
| Other cameras | index 2: rim centre (50, 16) px, r 103; index 0: same corner, partly visible. Only ~4 s of data each |
| Blur | defocus, not motion: 10–90 % edge width ~9 px everywhere; σ ≈ 3.4–3.7 px ≈ 0.23 mm. Motion smear runs along the bead, so it does not widen it |
| Light | red LED cast in 95 % of frames (blue channel gives slightly more contrast); white specular glints on the lines; index 2 has a large saturated blob |
| Scene | translucent clear/grey lines, black outer walls; lower-layer structure visible through the 1.5 mm gaps; the plate is ~18 mm lower and very blurred |
| Motion | scene moves ~13–14 px (≈ 1 mm of head travel) between consecutive frames |

- **Laplacian variance tracks image content, not focus** (plate-only frames 40–50, part-filled 200–340). Do not use it
  as a blur gate; use the fitted edge σ instead.
- IR (160×120) shows the nozzle at the top-**right**: IR and RGB are mirrored or rotated relative to each other. IR is
  too coarse for width (~2 px across a 1 mm line) and must never be saved. Use it only as a fresh-material gate, if at all.

## Which frames contain the fresh bead

- Index mapping is certain: `camera_index` k ↔ calibration `rgb(k+1)` (camera uuid in `source_frame_id` = `source_uuid`
  in the calibration JSON).
- **Camera 3 conflict (open):** frames on `rgb/trailing` from index 3 show the area *ahead* of the nozzle: averaged
  across-line profiles have no line on the nozzle path, and the scene flows toward the nozzle. Frames on `rgb/ahead`
  show a line exactly on the path (offset −0.1 mm) and the scene flows away from the nozzle: the fresh bead. For indices
  0 and 2 the labels matched the view (only ~4 s of data). The labels themselves agree with telemetry 100 %. Open
  question to the organisers: what do `ahead`/`trailing` mean for RGB?
- **Rule until then:** choose frames by **travel direction + image evidence**, not by topic. Keep a per-camera view
  table (which travel directions show the area behind the nozzle), learned from image flow. Camera 3: head moving toward
  −x−y (travel ≈ 225°). Confirm each frame: a line on the nozzle path with contrast above the no-bead null.
- **Usable fraction (measured):** 49 % of RGB frames look behind the nozzle; 78 % of those have printed part under the
  path; ~52 % of those show the bead clearly → **~20 % of all frames, ~1.2 frames/s**. Index 1 never streams RGB, so
  the other diagonal is blind.

## Calibration files

Not in git (public repo). Copy the organisers' four map folders into **`data/calibration/`** (gitignored), e.g.
`data/calibration/System_RGB4_Maps/rgb4/rgb4-mm-per-pixel.json`. Each camera folder holds:

| File | Content |
|---|---|
| `rgbN-mm-per-pixel.json` | metadata + **`raw_pixel_to_plane_mm_homography`** (3×3, raw pixel → calibration-plane mm) |
| `rgbN-mm-per-pixel-x.tiff`, `-y.tiff` | float32 480×640, mm per pixel along image columns / rows. Derived from the homography; adds nothing to it |
| `rgbN-mm-per-pixel-coverage.png` | per pixel 0 = extrapolated, 1 = inside the first calibration pose, 2 = inside both poses |

- Model: one plane homography per camera from a 5.0 mm checkerboard, **no lens-distortion model**. Plane axes are the
  board's, not printer XY: use it for distances, not headings.
- Valid only on **raw 640×480 frames** with the same mount, focus and plane: never resize, crop or rotate before mapping.
- **44–53 % of each frame is extrapolated** (bottom ~80–110 rows and the side margins). The nozzle region is not
  validated.
- The organisers' own check (second board pose, 5 mm edges): RMS **0.220 / 0.260 / 0.166 / 0.157 mm** for rgb1–rgb4
  (max 0.35–0.51 mm). That is above our 0.1 mm target, and the organisers say it is not a bead-width accuracy figure.
- **Scale:** 0.052–0.083 mm/px (median 0.062–0.066), **varies 1.42–1.51× across the frame** (largest top-left, 4.5–5.2 %
  per 100 px). Across the bead near the nozzle: ~0.067 mm/px. 0.1 mm ≈ 1.2–1.9 px. The briefing's 0.006 mm/px was a
  decimal slip.

## Pixels → mm

Map both edges through the homography, fit the bead axis in the plane, take the perpendicular gap. Tested to
< 0.002 mm against simulation.

```python
import json, numpy as np
H = np.array(json.load(open("data/calibration/System_RGB4_Maps/rgb4/rgb4-mm-per-pixel.json"))
             ["raw_pixel_to_plane_mm_homography"], float)

def px_to_mm(H, uv):                     # uv: (N,2) raw (u=column, v=row), subpixel OK
    uv = np.atleast_2d(np.asarray(uv, float))
    q = np.column_stack([uv, np.ones(len(uv))]) @ H.T
    return q[:, :2] / q[:, 2:3]

def width_mm(H, edge_a_px, edge_b_px):   # two edge point sets (N,2), raw pixels
    A, B = px_to_mm(H, edge_a_px), px_to_mm(H, edge_b_px)
    P = np.vstack([A, B]); d = np.linalg.svd(P - P.mean(0), full_matrices=False)[2][0]
    n = np.array([-d[1], d[0]])          # normal to the bead axis, in the plane
    return abs(B.mean(0) @ n - A.mean(0) @ n)
```

Don't: multiply by one mm/px constant (up to ±0.25 mm error on a 2 mm bead); read the TIFF scale at one endpoint (up to
+0.1 mm on a 5 mm bead); use the TIFFs for diagonal profiles (they lack the cross term); work on resized frames.

## Scale correction (measured)

Through the homography, four infill pitches next to the nozzle read 1.56–1.67 mm instead of the true 1.500 mm (from
telemetry and the G-code pattern): the homography **over-reads by +4 to +11 %**, worst right at the nozzle (25–125 px
away). Likely the bead plane is not exactly the calibration plane (~1.2 % scale per mm of height offset).

- Until reference samples exist: divide widths by (measured pitch / true pitch), currently **1.04–1.11** (we used 1.07).
- Later: one scale factor and one edge offset per camera, fitted against caliper or microscope widths on printed
  coupons. That fit is also our "reference error".
- Bead height or top-vs-side effects are < 0.3 %. Beads running *across* the viewing direction get an oblique side-wall
  bias up to ~0.07–0.14 mm (assumption, geometry); the ±45° infill for camera 3 runs roughly along the view, so it is
  small here.

## Accuracy budget

| Source | Size | Status |
|---|---|---|
| Subpixel edge localisation (3 estimators, median) | 0.007–0.009 mm RMS clean; ~0.02 mm at σ 3–8 px | synthetic |
| Pixel → mm through the homography | < 0.002 mm | synthetic |
| Homography bias near the nozzle | +4..11 % = 0.04–0.10 mm on a ~0.9 mm line; correctable | measured |
| Organiser calibration residual | 0.157–0.260 mm RMS on 5 mm edges | organiser figure |
| Defocus σ ≈ 0.23 mm | width and blur are degenerate in a joint fit; FWHM and edge-to-edge stay robust while the line is ≥ ~4σ wide | measured |
| Glint on the line | +0.3–0.4 mm | measured |
| Wrong edge pair (neighbour line, brighter layer) | mm-sized; ~25 % of synthetic cases without a width prior | synthetic |
| Real per-frame scatter | 0.12–0.16 mm std | measured |

Bottom line: the estimator is not the bottleneck. Focus, the single streaming camera and calibration bias are. Expect
~0.15–0.2 mm per frame; better only by averaging over ~1 s and after scale correction plus reference validation.

## Measurement pipeline (recommended, from the frame analysis and the vision toolkit)

1. Decode the raw 640×480 frame in memory. Drop expired, retained or older-revision messages (`mqtt-and-telemetry.md`).
2. Capture time = `published_utc_ms − source_age_ms`; telemetry state by source time; interpolate the nozzle position.
3. Gate: extruding at the modal print speed; camera pair stable ≥ 500 ms before / ≥ 300 ms after; view behind the nozzle
   (travel direction + view table); part under the path; no saturated pixels on the path.
4. Anchor at the per-camera nozzle exit (camera 3 ~(108, 53) px). Bead direction from travel, refined by a ±6–12°
   contrast search.
5. Rectified strip along the bead; 4–8 windows 25–225 px from the nozzle, inside coverage ≥ 1; ~20 block profiles; blue
   channel or luminance.
6. Edges per profile: gradient peak + parabola, erf (blurred bar) fit, first moment; take the median. FWHM as a
   cross-check.
7. Both edge sets through the homography → perpendicular width → divide by the pitch scale factor.
8. Width prior from config: ~0.9–1.0 mm now, the slicer value once known. **The prototype toolkit's default width range
   (1.2–8 mm) rejects the real line: set ~0.4–2.5 mm.**
9. Confidence from: contrast vs a no-bead null, fitted edge σ, inlier share, agreement between windows and between the
   three methods (≤ 0.08 mm), glare fraction, coverage class, distance from the nozzle. Accept at ≥ 0.5.
10. Robust median over ~1 s. Emit a width-vs-arc-length profile with deposit-time tags (control uses it).
11. Flags with hysteresis and debounce: `TOO_NARROW`, `TOO_WIDE`, `NO_MEASUREMENT`.
12. Validate on printed coupons (caliper or microscope); report bias and spread as the reference error.

Prototype code (frame analysis, the subpixel toolkit, synthetic tests) exists outside the repo; ask the team. It runs in
1–8 ms per frame on a laptop (expect 10–20× slower on a small edge CPU); numpy + OpenCV-headless only.

## Proven vs not

| Proven (measured on rec1) | Not proven |
|---|---|
| Bead located from a fixed nozzle anchor; tracked frame to frame | Width to 0.1 mm (per frame ~0.15–0.2 mm) |
| First widths: edge-to-edge 0.99 mm (IQR 0.88–1.29), FWHM 1.00 mm (IQR 0.91–1.15); ~0.88–0.96 mm after scale correction | Any reference (caliper/microscope) comparison |
| Homography over-read 4–11 % near the nozzle, from the known pitch | Behaviour across layers (rec1 is one layer) |
| Index ↔ calibration mapping; camera 3 view vs label | Cameras 0, 1, 2 in practice (little or no data) |
| Synthetic estimator accuracy ≤ 0.01 mm RMS | Live, on-device performance |

Open with the organisers: the nozzle size and nominal line width (lines look ~2× what a 0.4 mm nozzle prints: a bigger
nozzle, over-extrusion or translucent edges?), focus and exposure, the red LED, streaming RGB from index 1 or both
cameras of a pair, the calibration board height, and printed reference samples.
