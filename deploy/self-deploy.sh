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
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-$HOME/.npm}"
export NIX_CONFIG="${NIX_CONFIG:+${NIX_CONFIG}
}sandbox = false"
mkdir -p "$HOME" "$NPM_CONFIG_CACHE" "$STAGE"
# npm/npx shebangs are #!/usr/bin/env node; dockerTools only has /bin/env.
mkdir -p /usr/bin
if [[ ! -e /usr/bin/env ]]; then
  ln -sf "$(command -v env 2>/dev/null || echo /bin/env)" /usr/bin/env
fi

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

copy_out_link() {
  # --store $dir makes --out-link point at /nix/store/… (logical). The bytes
  # live at $dir/nix/store/… on the host overlay.
  ls -lh "$OUT_LINK" || return 1
  local src="$OUT_LINK"
  if [[ -L "$OUT_LINK" && ! -e "$OUT_LINK" ]]; then
    local target
    target="$(readlink "$OUT_LINK")"
    echo "dangling gc root $OUT_LINK -> $target"
    if [[ "$target" == /nix/store/* && -e "/var/lib/boxci/nix$target" ]]; then
      src="/var/lib/boxci/nix$target"
      echo "using physical store path $src"
    else
      return 1
    fi
  fi
  if gzip -t "$src" 2>/dev/null; then
    gzip -dc "$src" >"$ARCHIVE"
  else
    cp -L "$src" "$ARCHIVE"
  fi
  ls -lh "$ARCHIVE"
  [[ -s "$ARCHIVE" ]]
}

echo "--- :nix: nix build .#dockerImage ($SHA)"
# dockerTools images include /homeless-shelter; nix refuses unsandboxed builds if it exists.
# The container overlay also hides newly-realised /nix/store paths, so build into a private store.
rm -rf /homeless-shelter
mkdir -p /var/lib/boxci/nix
rm -f "$OUT_LINK" "$ARCHIVE"
nix build --store /var/lib/boxci/nix .#dockerImage -L --out-link "$OUT_LINK"
copy_out_link
[[ -s "$ARCHIVE" ]] || {
  echo "nix build produced no image archive" >&2
  exit 1
}

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
