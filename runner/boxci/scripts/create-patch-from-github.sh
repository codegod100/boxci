#!/usr/bin/env bash
# boxci builtin: cherry-pick a GitHub commit onto a Radicle repo and open a patch.
#
# Required env:
#   BOXCI_REPO_ROOT / RADICLE_* — checkout + identity (via bootstrap.sh)
#   GITHUB_COMMIT — full or abbreviated commit SHA from GitHub
#   GITHUB_REPO_URL — https://github.com/org/repo.git (or git@…)
#
# Optional:
#   GIT_BRANCH — Radicle base branch (default: main)
#   PATCH_TITLE / PATCH_DESCRIPTION — patch messages
#   RADICLE_AGENT_DRY_RUN=1 — stop before push
#   GITHUB_TOKEN — for private GitHub fetches (Authorization: Bearer)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${BOXCI_REPO_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
cd "$REPO_ROOT"

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

GITHUB_COMMIT="${GITHUB_COMMIT:-${GIT_SHA:-}}"
GITHUB_REPO_URL="${GITHUB_REPO_URL:-}"
BASE_BRANCH="${GIT_BRANCH:-${BOXCI_BASE_BRANCH:-main}}"
DRY_RUN="${RADICLE_AGENT_DRY_RUN:-0}"

if [[ -z "$GITHUB_COMMIT" ]]; then
  bk_die "GITHUB_COMMIT (or GIT_SHA) required"
fi
if [[ -z "$GITHUB_REPO_URL" ]]; then
  bk_die "GITHUB_REPO_URL required (e.g. https://github.com/org/repo.git)"
fi

# Normalize GitHub URL to https .git form when possible.
case "$GITHUB_REPO_URL" in
  https://github.com/*|http://github.com/*|git@github.com:*)
    ;;
  *)
    echo "[github-patch] warn: GITHUB_REPO_URL does not look like github.com: $GITHUB_REPO_URL" >&2
    ;;
esac
if [[ "$GITHUB_REPO_URL" == git@github.com:* ]]; then
  path="${GITHUB_REPO_URL#git@github.com:}"
  path="${path%.git}"
  GITHUB_REPO_URL="https://github.com/${path}.git"
elif [[ "$GITHUB_REPO_URL" != *.git ]]; then
  GITHUB_REPO_URL="${GITHUB_REPO_URL%/}.git"
fi

SHORT_SHA="$(printf '%s' "$GITHUB_COMMIT" | cut -c1-12)"
PATCH_BRANCH="github/${SHORT_SHA}"

echo "=== boxci GitHub commit → Radicle patch ==="
echo "radicle checkout: $REPO_ROOT"
echo "base branch:      $BASE_BRANCH"
echo "github repo:      $GITHUB_REPO_URL"
echo "github commit:    $GITHUB_COMMIT"
echo "patch branch:     $PATCH_BRANCH"

# Identity + rad remote (same path as issue agent; skip cursor install noise where possible).
export RADICLE_REQUIRE_IDENTITY="${RADICLE_REQUIRE_IDENTITY:-1}"
# bootstrap.sh always installs cursor-agent; that is fine and idempotent.
bash "$SCRIPT_DIR/bootstrap.sh"

git fetch origin "$BASE_BRANCH" 2>/dev/null || git fetch origin "refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}" || true
git checkout -B "$BASE_BRANCH" "origin/${BASE_BRANCH}" 2>/dev/null \
  || git checkout -B "$BASE_BRANCH" "$BASE_BRANCH" 2>/dev/null \
  || git checkout "$BASE_BRANCH"
git reset --hard "origin/${BASE_BRANCH}" 2>/dev/null || git reset --hard "HEAD"

# Fetch the GitHub commit into FETCH_HEAD (depth-friendly).
FETCH_URL="$GITHUB_REPO_URL"
if [[ -n "${GITHUB_TOKEN:-}" && "$FETCH_URL" == https://github.com/* ]]; then
  FETCH_URL="https://x-access-token:${GITHUB_TOKEN}@${FETCH_URL#https://}"
fi

echo "=== fetching GitHub commit ==="
# depth=2 so cherry-pick can see the parent and compute the diff
git fetch --depth=2 "$FETCH_URL" "$GITHUB_COMMIT"
FETCHED="$(git rev-parse FETCH_HEAD)"
echo "fetched: $FETCHED"

# Prefer the GitHub subject as default title.
SUBJECT="$(git log -1 --format=%s "$FETCHED" 2>/dev/null || true)"
BODY="$(git log -1 --format=%b "$FETCHED" 2>/dev/null || true)"
PATCH_TITLE="${PATCH_TITLE:-${SUBJECT:-github ${SHORT_SHA}}}"
if [[ -z "${PATCH_DESCRIPTION:-}" ]]; then
  PATCH_DESCRIPTION="$(printf 'Imported from GitHub commit %s\n\nSource: %s\n%s' \
    "$FETCHED" "$GITHUB_REPO_URL" "$BODY")"
fi

git checkout -B "$PATCH_BRANCH"

echo "=== cherry-pick $FETCHED ==="
set +e
git cherry-pick --allow-empty -x "$FETCHED"
pick_rc=$?
set -e
if [[ "$pick_rc" -ne 0 ]]; then
  # Merge commits need -m 1
  if git rev-parse -q --verify "${FETCHED}^2" >/dev/null 2>&1; then
    echo "[github-patch] merge commit detected; retrying cherry-pick -m 1"
    git cherry-pick --abort 2>/dev/null || true
    git cherry-pick --allow-empty -x -m 1 "$FETCHED"
  else
    git cherry-pick --abort 2>/dev/null || true
    bk_die "cherry-pick failed for $FETCHED (conflicts or missing parents)"
  fi
fi

echo "patch.title=$PATCH_TITLE"
echo "patch.description<<EOF"
printf '%s\n' "$PATCH_DESCRIPTION"
echo "EOF"

if [[ "$DRY_RUN" == "1" || "$DRY_RUN" == "true" || "$DRY_RUN" == "yes" ]]; then
  echo "=== dry-run: skipping git push rad HEAD:refs/patches ==="
  echo "patch_id="
  exit 0
fi

echo "=== opening Radicle patch ==="
push_out="$(mktemp)"
set +e
git push rad HEAD:refs/patches \
  -o "patch.message=${PATCH_TITLE}" \
  -o "patch.message=${PATCH_DESCRIPTION}" 2>&1 | tee "$push_out"
push_rc=${PIPESTATUS[0]}
set -e
if [[ "$push_rc" -ne 0 ]]; then
  cat "$push_out" >&2
  rm -f "$push_out"
  bk_die "git push rad HEAD:refs/patches failed"
fi

PATCH_ID="$(
  grep -Eo '\b[0-9a-f]{40}\b' "$push_out" | head -1 || true
)"
if [[ -z "$PATCH_ID" ]]; then
  # Fallback: newest authored open patch tip on this branch message
  PATCH_ID="$(rad patch list --authored 2>/dev/null | awk 'NR==1 {print $1}' || true)"
fi
rm -f "$push_out"

if [[ -n "$PATCH_ID" ]]; then
  echo "=== ensuring patch description (rad patch edit ${PATCH_ID:0:7}) ==="
  rad patch edit "$PATCH_ID" -m "$PATCH_TITLE" -m "$PATCH_DESCRIPTION" --no-announce 2>&1 || true
fi

echo "patch_id=${PATCH_ID}"
echo "github_commit=${FETCHED}"
echo "=== done ==="
