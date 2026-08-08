#!/usr/bin/env bash
set -euo pipefail

cd /home/boxd/boxci

if ! command -v nix >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf -L \
    https://install.determinate.systems/nix \
    | sh -s -- install linux --no-confirm --init none
fi

# shellcheck disable=SC1091
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh 2>/dev/null || true
export PATH="/nix/var/nix/profiles/default/bin:${PATH}"

if [[ ! -S /nix/var/nix/daemon-socket/socket ]]; then
  sudo /nix/var/nix/profiles/default/bin/nix daemon >/tmp/nix-daemon.log 2>&1 &
  for _ in $(seq 1 30); do
    [[ -S /nix/var/nix/daemon-socket/socket ]] && break
    sleep 1
  done
fi

nix --version
nix build .#boxci -L
echo BUILD_DONE
