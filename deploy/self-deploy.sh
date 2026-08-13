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
OUT_LINK="${TMPDIR:-/tmp}/boxci-docker-image"
ARCHIVE="${TMPDIR:-/tmp}/boxci-image.tar"
HOME="${HOME:-/var/lib/boxci}"
export HOME
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-$HOME/.npm}"
export NIX_CONFIG="${NIX_CONFIG:+${NIX_CONFIG}
}sandbox = false"
mkdir -p "$HOME" "$NPM_CONFIG_CACHE" "$(dirname "$OUT_LINK")"

have_tools() {
  command -v skopeo >/dev/null \
    && command -v node >/dev/null \
    && command -v jq >/dev/null \
    && command -v gzip >/dev/null \
    && command -v npx >/dev/null
}

# Deploy tools stay out of the runtime image so Cloudflare can unpack it.
if [[ "${BOXCI_SELF_DEPLOY_TOOLS:-}" != 1 ]] && ! have_tools; then
  export BOXCI_SELF_DEPLOY_TOOLS=1
  exec nix shell --inputs-from "$ROOT" \
    nixpkgs#skopeo nixpkgs#nodejs_22 nixpkgs#jq nixpkgs#gzip nixpkgs#gnused \
    -c bash "$0" "$@"
fi

cd "$ROOT"

echo "--- :nix: nix build .#dockerImage ($SHA)"
if ! nix build .#dockerImage -L --out-link "$OUT_LINK"; then
  echo "--- retrying with writable store /var/lib/boxci/nix"
  mkdir -p /var/lib/boxci/nix
  nix build --store /var/lib/boxci/nix .#dockerImage -L --out-link "$OUT_LINK"
fi

TARBALL="$(readlink -f "$OUT_LINK")"
if [[ -d "$TARBALL" ]]; then
  TARBALL="$(find "$TARBALL" -type f \( -name '*.tar' -o -name '*.tar.gz' \) | head -1)"
fi
ls -lh "$OUT_LINK" "$TARBALL"
[[ -e "$TARBALL" ]] || {
  echo "nix build produced no image at $TARBALL" >&2
  exit 1
}

if gzip -t "$TARBALL" 2>/dev/null; then
  gzip -dc "$TARBALL" >"$ARCHIVE"
else
  cp -f "$TARBALL" "$ARCHIVE"
fi

echo "--- :npm: wrangler in $WORKER_DIR"
cd "$WORKER_DIR"
npm install --silent

echo "--- :lock: registry credentials"
CREDS_JSON="$(npx wrangler@latest containers registries credentials --push --json --expiration-minutes 30)"
USER="$(echo "$CREDS_JSON" | jq -r '.username')"
PASS="$(echo "$CREDS_JSON" | jq -r '.password')"
HOST="$(echo "$CREDS_JSON" | jq -r '.registry_host // "registry.cloudflare.com"')"
[[ -n "$USER" && -n "$PASS" && "$USER" != "null" && "$PASS" != "null" ]] || {
  echo "failed to parse wrangler registry credentials" >&2
  exit 1
}

REF="${HOST}/${ACCOUNT}/${IMAGE_NAME}:${SHA}"
echo "--- :skopeo: copy docker-archive → docker://${REF}"
skopeo copy --insecure-policy \
  --dest-creds "${USER}:${PASS}" \
  "docker-archive:${ARCHIVE}" \
  "docker://${REF}"

echo "--- :cloudflare: wrangler deploy"
sed -i "s|image = .*|image = \"${REF}\"|" wrangler.toml
npx wrangler@latest deploy
echo "deployed ${REF}"
