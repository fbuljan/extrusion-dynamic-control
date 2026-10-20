#!/usr/bin/env bash
# Build an app's image for the edge device (linux/amd64) and package it as an Industrial Edge .app with tiap.
# Usage: tools/edge/build-app.sh <app-dir> <version>     e.g. tools/edge/build-app.sh tools/edge/hello-edge 0.0.1
# <app-dir> holds a Dockerfile and ie-app/ (detail.json + <service>/docker-compose.yaml using image <service>:${APP_VERSION}).
set -euo pipefail

app_dir=${1:?usage: build-app.sh <app-dir> <version>}
export APP_VERSION=${2:?usage: build-app.sh <app-dir> <version>}
name=$(basename "$app_dir")
tiap=$(command -v tiap || echo "$HOME/go/bin/tiap")
[ -x "$tiap" ] || { echo "tiap missing: brew install go && go install github.com/thediveo/tiap/cmd/tiap@v0.14.5" >&2; exit 1; }

docker buildx build --platform linux/amd64 --provenance=false -t "$name:$APP_VERSION" --load "$app_dir"
mkdir -p "$app_dir/dist"
"$tiap" -i -o "$app_dir/dist/${name}_${APP_VERSION}.app" --platform linux/amd64 --app-version "$APP_VERSION" "$app_dir/ie-app"
echo "built $app_dir/dist/${name}_${APP_VERSION}.app"
