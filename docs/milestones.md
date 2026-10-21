# Milestones

Team plan from the whiteboard, 20 Oct 2026 (day 1 afternoon). Worked in order. **Now: I.**

| # | Milestone | From the plan | Status |
|---|---|---|---|
| I | Width measurement & proof | proven against a golden sample; relative width is enough (D12) | **in progress** |
| II | Collect golden dataset | 60 min, fixed width | |
| III | Collect outliers | 10 × 5 min | |
| IV | Dataset preparation | | |
| V | DDSP training | | |
| VI | App development | | |
| VII | Test & validation | | |
| VIII | DDSP finetuning | | |
| IX | Verification | | |

Open: "golden sample" is read here as a physical print of known, calliper-measured width; "DDSP" is as written on the
board; it refers to Engel et al., "DDSP: Differentiable Digital Signal Processing" (ICLR 2020), used here as a
physics-based, differentiable printer model (see `architecture.md`).

## I: where we are

- Width comes from IR only (D11). Best estimator so far: steepest-slope edges on IR brightness, ~2.5 px,
  per-frame noise ~0.06 mm (`experiments.md`: ir-width-methods).
- It is repeatable but follows nozzle temperature and print speed more than flow, so it is not yet a width in mm.
- Camera angle: modelled from the print itself (ir-geometry). Cam1/cam3 px → mm within ±2–4 % along the infill bead,
  which puts the IR bead at ~1.65–1.76 mm. Wall beads (crossing the view) are not verified yet.
- Missing for "proof": a golden sample to compare against, and a clean flow sweep with speed held fixed.
