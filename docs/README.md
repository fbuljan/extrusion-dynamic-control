# docs/

Team knowledge for humans and agents. Start with `SOURCE_OF_TRUTH.md`. Connection details are never here: use `.env`.

| File | One line |
|---|---|
| `architecture.md` | The agreed design: width extraction, camera model, DDSP-style printer model, controller, data, build order; ends with what was built and how it differs |
| `prototype.md` | The width controller as built: pipeline, safety design, running it locally, refitting the models, endpoints, known limits |
| `width-estimation.md` | How the bead width is measured: IR frame → px → mm → rolling estimate, plain and technical, with live results |
| `milestones.md` | The team plan, milestones I–IX in order, and where we are on the current one |
| `SOURCE_OF_TRUTH.md` | The reference: task, levels, setup, facts with tags, risks, open questions; points to the docs below |
| `mqtt-and-telemetry.md` | The five topics, preview and telemetry schemas, measured rates, camera index/azimuth/role, timing, gotchas |
| `cameras-and-calibration.md` | What the cameras see, which frames to use, homography + scale fix, accuracy budget, measurement pipeline |
| `control.md` | The five writable levers and request contract (schema 0.2.0), latency, Level 2 controller, Level 3 corner learner, step test, safety |
| `edge-deployment.md` | Building the `.app`, compose rules, uploading and installing via IEM (verified), IECTL, what is still missing |
| `decisions.md` | Decision log: one row per decision, one line of why (D1–D47) |
| `experiments.md` | Experiment log: hypothesis, result, verdict |
