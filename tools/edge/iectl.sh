#!/usr/bin/env bash
# Run Siemens IECTL (Linux amd64 only) inside a container, so it works on macOS/arm64.
# Needs the iectl binary from the IE Hub ("Download Software" → "Developer tools" → "Industrial Edge Control Linux").
# Usage: IECTL_BIN=~/ie-tools/iectl tools/edge/iectl.sh <iectl args>   (files must be under the current directory)
set -euo pipefail

bin=${IECTL_BIN:-$HOME/ie-tools/iectl}
[ -f "$bin" ] || { echo "iectl binary not found at $bin (set IECTL_BIN)" >&2; exit 1; }
docker image inspect ie-tools:amd64 >/dev/null 2>&1 || docker build --platform linux/amd64 -t ie-tools:amd64 - <<'EOF'
FROM debian:12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
EOF

mkdir -p "$HOME/.iectl-container"
exec docker run --rm -i --platform linux/amd64 \
  -v "$PWD":/work -w /work \
  -v "$HOME/.iectl-container":/root/.iectl \
  -v "$bin":/usr/local/bin/iectl:ro \
  -e EDGE_SKIP_TLS -e IEM_TOKEN -e IE_WORKSPACE \
  ie-tools:amd64 iectl "$@"
