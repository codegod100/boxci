#!/usr/bin/env bash
# boxci builtin: merge a Radicle patch into main.
#
# Required env:
#   BOXCI_REPO_ROOT / RADICLE_* — checkout + identity (via bootstrap.sh)
#   RADICLE_PATCH_ID — the patch OID to merge (full or abbreviated hex)
#
# Optional:
#   GIT_BRANCH — target branch (default: main)
#   RADICLE_RID / BOXCI_REPO_ID — repository id (for --repo flag)
#   PATCH_MERGE_MESSAGE — optional merge commit annotation
#   RADICLE_AGENT_DRY_RUN=1 — stop before merge
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${BOXCI_REPO_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
cd "$REPO_ROOT"

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

PATCH_ID="${RADICLE_PATCH_ID:-${PATCH_ID:-}}"
BASE_BRANCH="${GIT_BRANCH:-${BOXCI_BASE_BRANCH:-main}}"
DRY_RUN="${RADICLE_AGENT_DRY_RUN:-0}"

if [[ -z "$PATCH_ID" ]]; then
  bk_die "RADICLE_PATCH_ID required"
fi

echo "=== boxci Radicle patch merge ==="
echo "radicle checkout: $REPO_ROOT"
echo "base branch:      $BASE_BRANCH"
echo "patch id:         $PATCH_ID"

# Identity + rad remote setup (same as github-patch / issue-agent builtins).
export RADICLE_REQUIRE_IDENTITY="${RADICLE_REQUIRE_IDENTITY:-1}"
bash "$SCRIPT_DIR/bootstrap.sh"
bk_export_rad_path
command -v rad >/dev/null 2>&1 \
  || bk_die "rad not on PATH after bootstrap (RAD_HOME=${RAD_HOME:-unset} PATH=$PATH)"
echo "[patch-merge] rad=$(command -v rad)"

RID="${RADICLE_RID:-${BOXCI_REPO_ID:-}}"
if [[ -n "$RID" && "$RID" != rad:* ]]; then
  RID="rad:${RID#rad://}"
fi

# Ensure we are on the target branch and up to date.
git fetch origin "$BASE_BRANCH" 2>/dev/null || git fetch origin "refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}" || true
git checkout -B "$BASE_BRANCH" "origin/${BASE_BRANCH}" 2>/dev/null \
  || git checkout -B "$BASE_BRANCH" "$BASE_BRANCH" 2>/dev/null \
  || git checkout "$BASE_BRANCH"
git reset --hard "origin/${BASE_BRANCH}" 2>/dev/null || git reset --hard "HEAD"

# Ensure the rad remote is set up (rad clone / rad remote add).
if ! git remote get-url rad >/dev/null 2>&1; then
  if [[ -n "$RID" ]]; then
    echo "[patch-merge] adding rad remote: $RID"
    git remote add rad "$RID"
  else
    bk_die "remote 'rad' missing and no RID to add it"
  fi
fi

# Fetch latest refs from rad (patch + branch state).
echo "=== fetching rad refs ==="
git fetch rad 2>/dev/null || true

# Show the patch before merging for logging.
echo "=== patch details ==="
if [[ -n "$RID" ]]; then
  rad patch show "$PATCH_ID" --repo "$RID" 2>&1 || rad patch show "$PATCH_ID" 2>&1 || true
else
  rad patch show "$PATCH_ID" 2>&1 || true
fi

if [[ "$DRY_RUN" == "1" || "$DRY_RUN" == "true" || "$DRY_RUN" == "yes" ]]; then
  echo "=== dry-run: skipping rad patch merge ==="
  echo "merged=0"
  echo "merged_sha="
  exit 0
fi

# Merge the patch into the target branch.
echo "=== merging patch ${PATCH_ID:0:7} into $BASE_BRANCH ==="
MERGE_ARGS=(rad patch merge "$PATCH_ID")
if [[ -n "$RID" ]]; then
  MERGE_ARGS+=(--repo "$RID")
fi
if [[ -n "${PATCH_MERGE_MESSAGE:-}" ]]; then
  MERGE_ARGS+=(--message "$PATCH_MERGE_MESSAGE")
fi
# --no-announce may not be supported by all rad versions; try without on failure.
merge_out="$(mktemp)"
set +e
"${MERGE_ARGS[@]}" 2>&1 | tee "$merge_out"
merge_rc=${PIPESTATUS[0]}
set -e

if [[ "$merge_rc" -ne 0 ]]; then
  # Retry without --no-announce (older rad versions may not support it).
  echo "[patch-merge] retrying without --no-announce..."
  MERGE_ARGS=(rad patch merge "$PATCH_ID")
  if [[ -n "$RID" ]]; then
    MERGE_ARGS+=(--repo "$RID")
  fi
  if [[ -n "${PATCH_MERGE_MESSAGE:-}" ]]; then
    MERGE_ARGS+=(--message "$PATCH_MERGE_MESSAGE")
  fi
  set +e
  "${MERGE_ARGS[@]}" 2>&1 | tee "$merge_out"
  merge_rc=${PIPESTATUS[0]}
  set -e
fi

if [[ "$merge_rc" -ne 0 ]]; then
  cat "$merge_out" >&2
  rm -f "$merge_out"
  bk_die "rad patch merge failed (exit $merge_rc)"
fi

rm -f "$merge_out"

# Capture the merged commit SHA (HEAD after merge).
MERGED_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
echo "merged=1"
echo "merged_sha=${MERGED_SHA}"
echo "patch_id=${PATCH_ID}"
echo "=== done ==="
