# docs/

Team knowledge for humans and agents. Start with `SOURCE_OF_TRUTH.md`. Connection details are never here: use `.env`.

| File | One line |
|---|---|
| `SOURCE_OF_TRUTH.md` | The reference: task, levels, setup, facts with tags, risks, open questions; points to the docs below |
| `mqtt-and-telemetry.md` | The five topics, preview and telemetry schemas, measured rates, camera index/azimuth/role, timing, gotchas |
| `cameras-and-calibration.md` | What the cameras see, which frames to use, homography + scale fix, accuracy budget, measurement pipeline |
| `control.md` | Flow and pressure-advance levers, latency, Level 2 controller, Level 3 corner learner, step test, safety |
| `edge-deployment.md` | Building the `.app`, compose rules, uploading and installing via IEM (verified), IECTL, what is still missing |
| `decisions.md` | Decision log: one row per decision, one line of why |
| `experiments.md` | Experiment log: hypothesis, result, verdict |
