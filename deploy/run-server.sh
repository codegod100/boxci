#!/usr/bin/env bash
# systemd wrapper — load boxd account env (OPENBAO_TOKEN, etc.) then start boxci-server.
set -euo pipefail

ROOT="${BOXCI_ROOT:-/home/boxd/boxci}"
if [[ -f /etc/profile.d/boxd-env.sh ]]; then
  # shellcheck disable=SC1091
  source /etc/profile.d/boxd-env.sh
fi

export BOXCI_ROOT="$ROOT"
export BOXCI_PORT="${BOXCI_PORT:-8080}"
export PATH="$ROOT/result/bin:/nix/var/nix/profiles/default/bin:${PATH}"

exec "$ROOT/result/bin/boxci-server"
