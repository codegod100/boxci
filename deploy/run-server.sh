#!/usr/bin/env bash
# systemd wrapper — load boxd account env (OPENBAO_TOKEN, etc.) then start boxci-server.
set -euo pipefail

ROOT="${BOXCI_ROOT:-/home/boxd/boxci}"
if [[ -f /etc/profile.d/boxd-env.sh ]]; then
  # shellcheck disable=SC1091
  source /etc/profile.d/boxd-env.sh
fi

# Optional local env (B2 keys, etc.). boxd-env.sh is only written at takeoff and
# may miss vars added later via `boxd env set`; keep those in $ROOT/.env.
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export BOXCI_ROOT="$ROOT"
export BOXCI_PORT="${BOXCI_PORT:-8080}"
export BOXCI_PUBLIC_URL="${BOXCI_PUBLIC_URL:-https://boxci.boxd.sh}"
# Radicle CLI (rad, git-remote-rad) lives under $HOME/.radicle/bin — required for
# patch pushes. Child scripts that run bootstrap in a subprocess must also call
# bk_export_rad_path; keep it on the service PATH as a backstop.
RAD_HOME="${RAD_HOME:-${HOME}/.radicle}"
export RAD_HOME
export PATH="${RAD_HOME}/bin:$ROOT/result/bin:/nix/var/nix/profiles/default/bin:${PATH}"
# Prefer synced runner/ over the nix-installed package (deploy/vm-build.sh may be cached).
export PYTHONPATH="$ROOT/runner${PYTHONPATH:+:$PYTHONPATH}"

exec "$ROOT/result/bin/boxci-server"
