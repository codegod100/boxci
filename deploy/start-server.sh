#!/usr/bin/env bash
# Start boxci server on a boxd VM (run inside VM after clone + nix build).
set -euo pipefail

ROOT="${BOXCI_ROOT:-/home/boxd/boxci}"
PORT="${BOXCI_PORT:-8080}"

cd "$ROOT"

if [[ ! -x result/bin/boxci-server ]]; then
  echo "Building boxci..."
  nix build .#boxci -L
fi

export BOXCI_ROOT="$ROOT"
export BOXCI_PORT="$PORT"
export PATH="$ROOT/result/bin:$PATH"

# Stop prior instance
pkill -f 'boxci-server' 2>/dev/null || true
sleep 1

nohup setsid "$ROOT/result/bin/boxci-server" > /tmp/boxci-server.log 2>&1 &
for i in $(seq 1 20); do
  curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null && break
  sleep 1
done

curl -sf "http://127.0.0.1:${PORT}/health" | jq .
echo "boxci listening on port ${PORT}"
