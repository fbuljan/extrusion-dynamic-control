# Fitted model parameters

`geometry.json`, `camera_model.json` and `printer_model.json` are produced by `tools/model/` from our own recordings and
are gitignored (calibration files stay local; the repo is public). The Docker build copies this folder into the image.
Without them the app runs, shows "no model" and gives no advice.
