# Edge packaging

Getting a Docker app onto the Siemens Industrial Edge device: image (linux/amd64) → `.app` → Industrial Edge
Management (IEM) → device.

## Build a `.app` (works now, no Siemens login)

```sh
brew install go && go install github.com/thediveo/tiap/cmd/tiap@v0.14.5   # third-party packager, Apache-2.0
tools/edge/build-app.sh app 0.1.0   # → app/dist/hello-edge_0.1.0.app (the dashboard app)
```

An app folder holds a `Dockerfile` and `ie-app/`: `detail.json` (app id, title, reverse-proxy settings) and
`<service>/docker-compose.yaml` (compose 2.4, image `<service>:${APP_VERSION}`, `mem_limit` required, no `ports:`
when the web UI goes through the device's reverse proxy at `/<service>/`), `nginx/nginx.json`, `appicon.png`.
Copy `app/` or `tools/edge/mqtt-probe/` as a starting point for a new app; generate a fresh app id with
`uuidgen | tr -d - | tr A-Z a-z`.

## Upload and install

- **IEM web UI:** Device Applications → "+ Add application" → pick the `.app`, then install it on the device.
  The import button needs the `appimporter` role on our IEM user — ask Siemens.
- **IECTL** (official CLI; Linux/Windows amd64 only, download from the IE Hub, login required): `tools/edge/iectl.sh`
  runs it in a linux/amd64 container on a Mac. Typical V2 flow: `device-apps import` → `job import-job-wait` →
  `job batch-create --operation installApplication`.
- **App Publisher GUI** (macOS build exists, same IE Hub download): import the compose file, pick the image TAR.

Status (2026-10-20): the full path works — a tiap-built `.app` imported into our IEM (2.3.2) and installed on our
device (SIMATIC IPC BX-39A, x86-64, no GPU). Only warning: "Application is not signed". The app's page sits behind
the device UI login (`/<service>/` answers 401 without it) and the device uses a self-signed certificate.
