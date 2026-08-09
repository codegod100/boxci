#!/usr/bin/env bash
# Shared Radicle CI identity + storage helpers (issue agent + job COB publish).
# shellcheck shell=bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

RAD_HOME="${RAD_HOME:-${HOME}/.radicle}"
export RAD_HOME

radicle_rid_naked() {
  local rid="${RADICLE_RID:-${BOXCI_REPO_ID:-}}"
  rid="${rid#rad:}"
  rid="${rid#rad://}"
  echo "$rid"
}

radicle_seeds_json_array() {
  local s first=1
  RADICLE_PUBLIC_SEEDS="${RADICLE_PUBLIC_SEEDS:-z6Mkmqogy2qEM2ummccUthFEaaHvyYmYBYh3dbe9W4ebScxo@rosa.radicle.network:8776 z6MkrLMMsiPWUcNPHcRajuMi9mDfYckSoJyPwwnknocNYPm7@iris.radicle.network:8776}"
  RADICLE_SEED="${RADICLE_SEED:-z6MknYm3iSpuY5hLCH93K5Ls5KG7cBK4fQwybqcHzxDsT2jU@nandi.radicle.garden:58019}"
  printf '['
  for s in $RADICLE_PUBLIC_SEEDS $RADICLE_SEED; do
    [[ -n "$s" ]] || continue
    if [[ $first -eq 1 ]]; then
      first=0
    else
      printf ', '
    fi
    printf '"%s"' "$s"
  done
  printf ']'
}

install_radicle_cli() {
  bk_export_rad_path
  if command -v rad >/dev/null 2>&1 && command -v git-remote-rad >/dev/null 2>&1; then
    return 0
  fi
  echo "[radicle-ci] installing Radicle CLI into ${RAD_HOME}..."
  curl -fsSL https://radicle.xyz/install | sh -s -- --no-modify-path --prefix="$RAD_HOME"
  bk_export_rad_path
  command -v rad >/dev/null 2>&1 || bk_die "rad missing after install"
}

write_radicle_config() {
  local seeds_json
  mkdir -p "$RAD_HOME"
  if [[ -f "$RAD_HOME/config.json" ]]; then
    return 0
  fi
  seeds_json="$(radicle_seeds_json_array)"
  cat >"$RAD_HOME/config.json" <<EOF
{
  "publicExplorer": "https://radicle.network/nodes/\$host/\$rid\$path",
  "preferredSeeds": ${seeds_json},
  "node": {
    "alias": "boxci",
    "listen": [],
    "peers": { "type": "dynamic" },
    "connect": ${seeds_json},
    "externalAddresses": [],
    "network": "main",
    "log": "INFO",
    "relay": "auto",
    "seedingPolicy": { "default": "block" }
  }
}
EOF
}

materialize_radicle_keys() {
  local key_path pub_path
  key_path="$RAD_HOME/keys/radicle"
  pub_path="$RAD_HOME/keys/radicle.pub"
  mkdir -p "$RAD_HOME/keys"

  if [[ -f "$key_path" && -f "$pub_path" ]]; then
    return 0
  fi

  if [[ -z "${RADICLE_SECRET_KEY:-}" ]]; then
    return 1
  fi

  if [[ "$RADICLE_SECRET_KEY" == -----BEGIN* ]]; then
    printf '%s\n' "$RADICLE_SECRET_KEY" >"$key_path"
  else
    if ! printf '%s' "$RADICLE_SECRET_KEY" | base64 -d >"$key_path" 2>/dev/null; then
      echo "[radicle-ci] RADICLE_SECRET_KEY is neither OpenSSH PEM nor base64" >&2
      return 1
    fi
  fi
  chmod 600 "$key_path"

  if [[ -n "${RADICLE_PUBLIC_KEY:-}" ]]; then
    printf '%s\n' "$RADICLE_PUBLIC_KEY" >"$pub_path"
  else
    if ! ssh-keygen -y -P "${RAD_PASSPHRASE:-}" -f "$key_path" >"$pub_path" 2>/dev/null; then
      echo "[radicle-ci] failed to derive public key — set RADICLE_PUBLIC_KEY or RAD_PASSPHRASE" >&2
      return 1
    fi
  fi
  chmod 644 "$pub_path"
  return 0
}

ensure_rad_passphrase_env() {
  if [[ -z "${RAD_PASSPHRASE+x}" ]]; then
    export RAD_PASSPHRASE=""
  else
    export RAD_PASSPHRASE
  fi
}

radicle_seed_nid() {
  local seed="${1:-}"
  echo "${seed%%@*}"
}

radicle_connect_seeds() {
  local seed connected=0
  RADICLE_PUBLIC_SEEDS="${RADICLE_PUBLIC_SEEDS:-z6Mkmqogy2qEM2ummccUthFEaaHvyYmYBYh3dbe9W4ebScxo@rosa.radicle.network:8776 z6MkrLMMsiPWUcNPHcRajuMi9mDfYckSoJyPwwnknocNYPm7@iris.radicle.network:8776}"
  RADICLE_SEED="${RADICLE_SEED:-z6MknYm3iSpuY5hLCH93K5Ls5KG7cBK4fQwybqcHzxDsT2jU@nandi.radicle.garden:58019}"
  for seed in $RADICLE_PUBLIC_SEEDS $RADICLE_SEED; do
    [[ -n "$seed" ]] || continue
    if rad node connect "$seed" >/dev/null 2>&1; then
      connected=1
    fi
  done
  [[ $connected -eq 1 ]]
}

start_rad_node() {
  echo "[radicle-ci] ensuring rad node is running..."
  rad node start >/dev/null 2>&1 || true
  radicle_connect_seeds || true
}

hydrate_storage_from_https() {
  local rid_naked="$1"
  local dest="$RAD_HOME/storage/$rid_naked"
  local src="${RADICLE_GARDEN_GIT:-${BOXCI_REPO_URL:-}}"
  local root tmp url

  root="${BOXCI_REPO_ROOT:-}"
  if [[ -n "$root" ]] && url="$(git -C "$root" remote get-url origin 2>/dev/null || true)"; then
    if [[ "$url" == *radicle.garden* || "$url" == https://* || "$url" == http://* ]]; then
      src="$url"
    fi
  fi
  if [[ -z "$src" ]]; then
    echo "[radicle-ci] no HTTPS clone URL for storage hydrate" >&2
    return 1
  fi

  echo "[radicle-ci] hydrating \$RAD_HOME/storage/${rid_naked} from HTTPS (${src})"
  mkdir -p "$RAD_HOME/storage"
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/rad-storage.XXXXXX")"
  if ! git clone --bare --mirror "$src" "$tmp/repo"; then
    rm -rf "$tmp"
    return 1
  fi
  if ! git -C "$tmp/repo" show-ref --verify --quiet refs/rad/id \
    && ! git -C "$tmp/repo" show-ref 2>/dev/null | grep -q 'refs/namespaces/.*/refs/rad/'; then
    echo "[radicle-ci] HTTPS mirror missing refs/rad/id" >&2
    rm -rf "$tmp"
    return 1
  fi
  rm -rf "$dest"
  mv "$tmp/repo" "$dest"
  rm -rf "$tmp"
  local rid="${RADICLE_RID:-rad:${rid_naked}}"
  rad seed "$rid" --scope followed --no-fetch >/dev/null 2>&1 || true
  return 0
}

hydrate_storage_from_p2p() {
  local rid_naked="$1"
  local timeout seed seed_nid
  local rid="${RADICLE_RID:-rad:${rid_naked}}"
  timeout="${RADICLE_SEED_TIMEOUT:-60s}"
  RADICLE_PUBLIC_SEEDS="${RADICLE_PUBLIC_SEEDS:-z6Mkmqogy2qEM2ummccUthFEaaHvyYmYBYh3dbe9W4ebScxo@rosa.radicle.network:8776 z6MkrLMMsiPWUcNPHcRajuMi9mDfYckSoJyPwwnknocNYPm7@iris.radicle.network:8776}"
  RADICLE_SEED="${RADICLE_SEED:-z6MknYm3iSpuY5hLCH93K5Ls5KG7cBK4fQwybqcHzxDsT2jU@nandi.radicle.garden:58019}"

  for seed in $RADICLE_PUBLIC_SEEDS $RADICLE_SEED; do
    [[ -n "$seed" ]] || continue
    seed_nid="$(radicle_seed_nid "$seed")"
    rad node connect "$seed" >/dev/null 2>&1 || true
    rad seed "$rid" --scope followed --from "$seed_nid" --timeout "$timeout" || true
    if [[ -d "$RAD_HOME/storage/$rid_naked" ]]; then
      return 0
    fi
  done
  rad seed "$rid" --scope followed --timeout "$timeout" || true
  [[ -d "$RAD_HOME/storage/$rid_naked" ]]
}

ensure_rid_in_storage() {
  local rid_naked
  rid_naked="$(radicle_rid_naked)"
  if [[ -z "$rid_naked" ]]; then
    echo "[radicle-ci] RADICLE_RID / BOXCI_REPO_ID unset" >&2
    return 1
  fi

  if [[ -d "$RAD_HOME/storage/$rid_naked" ]] \
    && git -C "$RAD_HOME/storage/$rid_naked" show-ref >/dev/null 2>&1; then
    return 0
  fi

  if hydrate_storage_from_https "$rid_naked"; then
    return 0
  fi
  if hydrate_storage_from_p2p "$rid_naked"; then
    return 0
  fi
  echo "[radicle-ci] failed to populate \$RAD_HOME/storage/${rid_naked}" >&2
  return 1
}

# Prepare RAD_HOME + storage for COB writes. Soft-fails (returns 1) when
# identity/storage cannot be set up — callers should not fail the CI run.
ensure_radicle_ci_env() {
  bk_export_rad_path

  if [[ -z "${RADICLE_SECRET_KEY:-}" && ! -f "$RAD_HOME/keys/radicle" ]]; then
    echo "[radicle-ci] skip: no RADICLE_SECRET_KEY / keys under \$RAD_HOME" >&2
    return 1
  fi

  install_radicle_cli
  write_radicle_config
  if ! materialize_radicle_keys; then
    return 1
  fi
  ensure_rad_passphrase_env
  start_rad_node
  ensure_rid_in_storage
}
