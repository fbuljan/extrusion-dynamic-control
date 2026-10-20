# Edge deployment (Siemens Industrial Edge)

In plain words: our app is a normal Docker container. To run it on the Siemens edge device we package it as a `.app`
file, upload that to Industrial Edge Management (IEM, a Siemens-hosted web service), and install it on the device from
there. This whole path works: a hello-world `.app` built on a Mac was imported and installed on our device on day 1.
Two things are still missing: a login for the device's web UI (until then the app's page answers 401), and proof that
a container on the device can reach the MQTT broker. Keep a plain-Docker fallback ready.

Tags: **verified** = done on our machines; **docs** = Siemens documentation (v26.09); **untested**.

## The path

```
Dockerfile → image (linux/amd64) → .app (tiap, IECTL or App Publisher) → IEM (import) → device (install job)
```

## What works now (verified)

```sh
brew install go && go install github.com/thediveo/tiap/cmd/tiap@v0.14.5   # third-party packager, no Siemens login
tools/edge/build-app.sh app 0.1.0      # → app/dist/hello-edge_0.1.0.app (the dashboard; app id of the old hello-world)
```

- `build-app.sh` builds the image with `docker buildx --platform linux/amd64 --provenance=false` and packages it with
  `tiap`. Docker Desktop on Apple Silicon emulates amd64 fine.
- The `.app` contains `detail.json`, `digests.json`, the compose file (version interpolated), the image TAR, the
  reverse-proxy config and an icon.
- **IEM import works (verified):** IEM 2.3.2 (V2 UI) → Applications → "+ Add application" → pick the `.app`; the
  import task finished in ~45 s (`hello-edge_0.0.2.app`). No Siemens download or IE Hub login was needed.
- **Install works (verified):** the install job on our device completed in under a minute. Only warning: "Application
  is not signed" (expected for self-built apps).
- **Device (verified):** SIMATIC IPC BX-39A, x86-64, Edge OS 3.3.0, no GPU allocation, in the team's device group, on
  the same subnet as the MQTT broker. Self-signed certificate.
- **Not yet:** `/hello-edge/` on the device went from 503 to **401** once the app started: the reverse proxy puts apps
  behind the device UI login, which we don't have. Container → broker reachability is untested.

## App folder layout (copy `tools/edge/hello-edge/`)

```
<app>/
  Dockerfile
  ie-app/
    detail.json                    # appId (32 hex: uuidgen | tr -d - | tr A-Z a-z), title, redirect settings
    <service>/docker-compose.yaml  # image <service>:${APP_VERSION}
    <service>/appicon.png          # ~150×150 PNG
    <service>/nginx/nginx.json     # reverse-proxy entry: service name, container port, rewrite target
```

## Compose rules (docs; break one and import or install fails)

- `version: '2.4'` (2.1–2.4 only). Allowed service keys: `image, command, environment, labels, logging, mem_limit,
  mem_reservation, networks, network_mode, ports, restart, user, volumes, cpuset, container_name` (+ `tmpfs`). Treat
  `healthcheck`, `devices`, `build`, `env_file` as unsupported.
- **`mem_limit` on every service** (install validation fails without it) and a realistic `mem_reservation`.
- Semantic version tags, never `latest`; version string ≤ 32 characters.
- `restart` must not be `no` (use `unless-stopped`).
- No host ports below 1024 and none of the reserved ones (22, 80, 443, 4443, 8081, 8443, 9443, …). Prefer **no
  `ports:`**: expose the web UI through the device's reverse proxy at `https://<device>/<service>/`. Container port
  ≥ 8000. The UI must use **relative URLs** (served under a sub-path).
- Volumes: `./publish/:/publish/` and `./cfg-data/:/cfg-data/` are optional per-app persistent folders; secrets go in
  `cfg-data`, never in the image or compose file. `tmpfs` is allowed. Files on mounted volumes cannot be executed.
- Image must be **linux/amd64** (the architecture is fixed at the first app version). Avoid Docker's 172.17.0.0/16.
- Apps get at most 85 % CPU per core. Bound logs with the `json-file` driver (`max-size`, `max-file`).

Skeleton for our app (no Databus, UI through the reverse proxy):

```yaml
version: '2.4'
services:
  width-monitor:
    image: width-monitor:${APP_VERSION}
    mem_limit: 512m
    mem_reservation: 256m
    restart: unless-stopped
    environment: {OMP_NUM_THREADS: "1", OPENBLAS_NUM_THREADS: "1"}
    volumes: ['./publish/:/publish/', './cfg-data/:/cfg-data/']
    tmpfs: [/scratch]
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}
```

Image: `python:3.12-slim` + numpy + opencv-python-headless + paho-mqtt ≈ 127 MB compressed / 346 MB unpacked
(verified). Skip scipy, pandas and torch. No GPU on our device; check CPU flags and RAM (`lscpu`, `free -m`,
`grep -c avx /proc/cpuinfo`) before adding heavier wheels.

## Upload and install

| Route | Needs | Notes |
|---|---|---|
| IEM web UI: Applications → "+ Add application" → pick the `.app` → install on the device | an IEM login with import rights (the team has one; ask the team lead) | **What we use (verified).** Uploads via the UI are visible to all IEM users |
| IECTL (official CLI) | IE Hub login to download it; Linux/Windows amd64 only | `tools/edge/iectl.sh` runs it in a linux/amd64 container on a Mac (`IECTL_BIN=~/ie-tools/iectl`; files must be under the current directory) |
| App Publisher GUI | same IE Hub download (a macOS build exists) | Choose "Skip" for the Docker engine and pick the image TAR; architecture x86-64 |

`ie-app-publisher-linux` (the old CLI) is deprecated: don't use it.

### IECTL cookbook (IEM V2; docs, untested here)

```sh
printf '%s' "$IEM_PASS" | iectl config add iem --name leam --url <IEM base URL> --user "<login>" --password-stdin
iectl iem-v2 device list                                    # works only on IEM V2; "not found" = V1
iectl iem-v2 device-apps import --file_to_import ./out/<file>.app --appSource self-developed   # tutorial shows --file
iectl iem-v2 job import-job-wait --id <jobId> --timeout 300
iectl iem-v2 device-apps list --filter "name contains 'width-monitor'"     # → applicationId; details → versionId
iectl iem-v2 job batch-create --appid <appId> --operation installApplication \
   --versionId <versionId> --infoMap '{"devices":["<deviceId>"]}'
iectl iem-v2 job device-job-wait --id <installJobId> --timeout 300
```

- Updates: new version → export → import → `--operation updateApplication`. Also `startApplication`,
  `stopApplication`, `restartApplication`, `uninstallApplication`.
- `self-developed` apps are visible only to the uploader until published (`iectl iem-v2 device-apps publish`).
- Config changes are detected by name/description/path, not content: rename the config when its content changes.

## Networking and config on the device

- Data comes straight from the organisers' MQTT broker, not from the device's Databus. The device sits on the broker's
  subnet and reaches the cloud IEM, but whether an **app container** can open a connection to the broker is untested:
  test it first (a tiny probe app, Connectivity Check → External endpoints, or Siemens' `connectivity-tester` app).
- If the device uses a proxy, add the broker to "No Proxy". NTP must work (the MQTT expiry checks depend on the clock).
- Broker host, user and password go in `/cfg-data/mqtt-config.json` (fall back to environment variables for local
  runs), never in the image.
- IR images: never written to `/publish`, logs or caches. Don't log payloads: device logs are downloadable.
- Browser access to the UI needs a route from the laptops to the device on port 443.

## Debugging (docs)

- App logs: IEM → Devices → device → installed applications → context menu → download logs (or `iectl iem-v2 device
  list-logs` / `download-logs`). Needs device-owner rights.
- Device UI → Management: start, stop, restart, statistics per app.
- App Developer Mode (Device UI → Settings → System, 2–10 h, device owner only): live logs, interactive terminal; port
  forwarding needs a recent device kit.
- Common failures: arm64 image on an x86 device; missing `mem_limit`; `latest` tag; restart `no`; image name in the TAR ≠
  compose; absolute URLs in the UI; import button disabled or HTTP 403 (role or import flag missing); app invisible to
  teammates (uploaded privately); clock skew.

## Still needed from Siemens

1. A device UI login, so we can open apps behind the device's reverse proxy (currently 401).
2. Confirmation that an app container can reach the MQTT broker, and which NTP source the device uses.
3. How laptops reach the device's reverse proxy (address, port 443, self-signed certificate); are WebSocket or MJPEG
   streams allowed through it?
4. Optional now: the IECTL Linux binary or an IE Hub account (scripted installs); does "Application is not signed"
   matter for anything?

Plan B if rung 4 slips: run the same container with plain Docker on a machine on the printer's network and say what
remains for Industrial Edge.
