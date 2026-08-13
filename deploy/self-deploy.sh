#!/usr/bin/env bash
# Build the boxci OCI image, push it to Cloudflare's registry (no Docker daemon),
# and wrangler-deploy the worker so the next instance boots the new image.
set -euo pipefail

: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN is required for self-deploy}"

ACCOUNT="${CLOUDFLARE_ACCOUNT_ID:-2612967e82750619224e7446c4c41b0b}"
ROOT="${BOXCI_REPO_ROOT:-$(git rev-parse --show-toplevel)}"
SHA="${GIT_SHA:-$(git -C "$ROOT" rev-parse HEAD)}"
IMAGE_NAME="boxci"
WORKER_DIR="$ROOT/deploy/worker"
STAGE="${BOXCI_ROOT:-/var/lib/boxci}/self-deploy"
OUT_LINK="$STAGE/docker-image"
ARCHIVE="$STAGE/boxci-image.tar"
HOME="${HOME:-/var/lib/boxci}"
export HOME
export PYTHONUNBUFFERED=1
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-$HOME/.npm}"
export NIX_CONFIG="${NIX_CONFIG:+${NIX_CONFIG}
}sandbox = false"
mkdir -p "$HOME" "$NPM_CONFIG_CACHE" "$STAGE"
# npm/npx shebangs are #!/usr/bin/env node; dockerTools only has /bin/env.
mkdir -p /usr/bin
if [[ ! -e /usr/bin/env ]]; then
  ln -sf "$(command -v env 2>/dev/null || echo /bin/env)" /usr/bin/env
fi

ART_DIR="${BOXCI_ROOT:-/var/lib/boxci}/artifacts/${BOXCI_REPO_SLUG:-boxci}/${BOXCI_RUN_ID:-self-deploy}"
mkdir -p "$ART_DIR"
LOG="$ART_DIR/self-deploy.log"

log() {
  printf '[self-deploy +%ds %s] %s\n' "$SECONDS" "$(date -u +%H:%M:%S)" "$*"
  # Before stdout is teed, keep a copy on disk so a nix-shell wrap still leaves a log.
  if [[ -n "${LOG:-}" && "${BOXCI_SELF_DEPLOY_LOGGED:-}" != 1 ]]; then
    printf '[self-deploy +%ds %s] %s\n' "$SECONDS" "$(date -u +%H:%M:%S)" "$*" >>"$LOG"
  fi
}

_phase_name=""
_phase_t=0
phase() {
  if [[ -n "$_phase_name" ]]; then
    log "finished ${_phase_name} in $((SECONDS - _phase_t))s"
  fi
  _phase_name=$1
  _phase_t=$SECONDS
  echo
  echo "--- $1"
  log "start $1"
}

dump_env() {
  log "ROOT=$ROOT"
  log "SHA=$SHA"
  log "HOME=$HOME STAGE=$STAGE"
  log "LOG=$LOG"
  log "host=$(uname -n 2>/dev/null || echo unknown) $(uname -m 2>/dev/null || true)"
  log "nproc=$(nproc 2>/dev/null || echo unknown)"
  if [[ -r /proc/meminfo ]]; then
    log "$(awk '/MemTotal|MemAvailable|SwapTotal|SwapFree/ { printf "%s=%s ", $1, $2 }' /proc/meminfo)"
  fi
  df -h / /tmp "${BOXCI_ROOT:-/var/lib/boxci}" 2>/dev/null || df -h || true
  log "nix=$(command -v nix >/dev/null && nix --version | head -1 || echo missing)"
  log "node=$(command -v node >/dev/null && node --version || echo missing)"
  log "skopeo=$(command -v skopeo >/dev/null && skopeo --version || echo missing)"
  log "npx=$(command -v npx >/dev/null && npx --version || echo missing)"
}

on_err() {
  local code=$? line=$1
  log "FAILED exit=${code} line=${line} phase=${_phase_name:-none}"
  df -h / /tmp "${BOXCI_ROOT:-/var/lib/boxci}" 2>/dev/null || true
  if [[ -r /proc/meminfo ]]; then
    grep -E 'Mem(Total|Free|Available)|Swap(Total|Free)' /proc/meminfo || true
  fi
  log "full log: $LOG"
  exit "$code"
}
trap 'on_err $LINENO' ERR

have_tools() {
  command -v skopeo >/dev/null \
    && command -v node >/dev/null \
    && command -v jq >/dev/null \
    && command -v gzip >/dev/null \
    && command -v npx >/dev/null
}

log "self-deploy begin sha=${SHA:0:12} run=${BOXCI_RUN_ID:-local}"
dump_env

# Deploy tools stay out of the runtime image so Cloudflare can unpack it.
if [[ "${BOXCI_SELF_DEPLOY_TOOLS:-}" != 1 ]] && ! have_tools; then
  export BOXCI_SELF_DEPLOY_TOOLS=1
  log "missing deploy tools — wrapping with nix shell (skopeo/nodejs/jq/gzip)"
  exec nix shell --inputs-from "$ROOT" \
    nixpkgs#skopeo nixpkgs#nodejs_22 nixpkgs#jq nixpkgs#gzip nixpkgs#gnused \
    -c bash "$0" "$@"
fi
log "deploy tools ready"

# Tee everything from here (nix build, skopeo, wrangler) into the artifact.
if [[ "${BOXCI_SELF_DEPLOY_LOGGED:-}" != 1 ]]; then
  log "teeing output to $LOG"
  export BOXCI_SELF_DEPLOY_LOGGED=1
  if command -v stdbuf >/dev/null 2>&1; then
    exec > >(stdbuf -oL tee -a "$LOG") 2>&1
  else
    exec > >(tee -a "$LOG") 2>&1
  fi
fi

cd "$ROOT"

copy_out_link() {
  # --store $dir makes --out-link point at /nix/store/… (logical). The bytes
  # live at $dir/nix/store/… on the host overlay.
  log "copy_out_link $OUT_LINK"
  ls -lh "$OUT_LINK" || return 1
  local src="$OUT_LINK"
  if [[ -L "$OUT_LINK" && ! -e "$OUT_LINK" ]]; then
    local target
    target="$(readlink "$OUT_LINK")"
    log "dangling gc root $OUT_LINK -> $target"
    if [[ "$target" == /nix/store/* && -e "/var/lib/boxci/nix$target" ]]; then
      src="/var/lib/boxci/nix$target"
      log "using physical store path $src"
      ls -lh "$src" || return 1
    else
      log "no physical store path for $target"
      return 1
    fi
  fi
  if gzip -t "$src" 2>/dev/null; then
    log "decompressing gzip image $src -> $ARCHIVE"
    gzip -dc "$src" >"$ARCHIVE"
  else
    log "copying uncompressed image $src -> $ARCHIVE"
    cp -L "$src" "$ARCHIVE"
  fi
  ls -lh "$ARCHIVE"
  [[ -s "$ARCHIVE" ]]
}

phase ":nix: nix build .#dockerImage ($SHA)"
# dockerTools images include /homeless-shelter; nix refuses unsandboxed builds if it exists.
# The container overlay also hides newly-realised /nix/store paths, so build into a private store.
rm -rf /homeless-shelter
mkdir -p /var/lib/boxci/nix
rm -f "$OUT_LINK" "$ARCHIVE"
log "nix build --store /var/lib/boxci/nix .#dockerImage -L --out-link $OUT_LINK"
nix build --store /var/lib/boxci/nix .#dockerImage -L --out-link "$OUT_LINK"
copy_out_link
[[ -s "$ARCHIVE" ]] || {
  log "nix build produced no image archive"
  exit 1
}
log "image archive $(du -h "$ARCHIVE" | awk '{print $1}')"

phase ":npm: wrangler in $WORKER_DIR"
cd "$WORKER_DIR"
log "npm install"
npm install

phase ":lock: registry credentials"
log "npx wrangler containers registries credentials --push"
CREDS_JSON="$(npx wrangler@latest containers registries credentials --push --json --expiration-minutes 30)"
USER="$(echo "$CREDS_JSON" | jq -r '.username')"
PASS="$(echo "$CREDS_JSON" | jq -r '.password')"
HOST="$(echo "$CREDS_JSON" | jq -r '.registry_host // "registry.cloudflare.com"')"
[[ -n "$USER" && -n "$PASS" && "$USER" != "null" && "$PASS" != "null" ]] || {
  log "failed to parse wrangler registry credentials (keys: $(echo "$CREDS_JSON" | jq -r 'keys | join(",")' 2>/dev/null || echo unparseable))"
  exit 1
}
log "registry host=$HOST user=$USER"

REF="${HOST}/${ACCOUNT}/${IMAGE_NAME}:${SHA}"
phase ":skopeo: copy docker-archive → docker://${REF}"
log "skopeo copy docker-archive:${ARCHIVE} docker://${REF}"
skopeo copy --insecure-policy \
  --dest-creds "${USER}:${PASS}" \
  "docker-archive:${ARCHIVE}" \
  "docker://${REF}"

phase ":cloudflare: wrangler deploy"
log "image = ${REF}"
sed -i "s|image = .*|image = \"${REF}\"|" wrangler.toml
grep -n '^image = ' wrangler.toml || grep -n 'image =' wrangler.toml || true
log "npx wrangler deploy"
npx wrangler@latest deploy

log "finished ${_phase_name} in $((SECONDS - _phase_t))s"
log "deployed ${REF}"
log "total ${SECONDS}s  log=$LOG"
echo "deployed ${REF}"
