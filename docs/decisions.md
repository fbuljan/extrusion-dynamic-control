# Decisions

Newest at the bottom. One row per decision, one line of why. A reversal is a new row naming what it supersedes.

| # | Date | Decision | Why |
|---|---|---|---|
| D1 | 2026-10-20 | Level 1 (measure width + flag deviations) comes before correction or learning | The challenge owner cares about this most; a reliable measurement is a complete result |
| D2 | 2026-10-20 | An accurate width measurement is the foundation of every approach | Needed for alerts, data labels and any feedback control |
| D3 | 2026-10-20 | MQTT is the data channel; deploy as Docker → Siemens Industrial Edge `.app` | Required by the setup |
| D4 | 2026-10-20 | Control levers are flow % and pressure advance only (supersedes "nozzle temperature + motor RPM") | The organisers' interface exposes only these two, max 1 correction/s |
| D5 | 2026-10-20 | No credentials, organiser files or calibration data in git; config via `.env` | The repo is public |
| D6 | 2026-10-20 | Hackathon mode: thin slice first, timeboxed experiments logged in `docs/experiments.md`, no review ceremony | Speed and learning over rigour |
