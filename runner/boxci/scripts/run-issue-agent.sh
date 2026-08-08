#!/usr/bin/env bash
# boxci builtin: Garden issue → cursor-agent → Radicle patch.
#
# Required env (boxd env / VM secrets):
#   CURSOR_API_KEY       — Cursor CLI / service account key
#   RADICLE_SECRET_KEY   — dedicated CI identity OpenSSH private key (PEM)
#
# Optional:
#   RADICLE_PUBLIC_KEY / RAD_PASSPHRASE
#   RADICLE_AGENT_MODEL / RADICLE_AGENT_TIMEOUT / RADICLE_AGENT_DRY_RUN
#   RADICLE_RID / BOXCI_REPO_ID / BOXCI_REPO_URL — repo identity
#   BOXCI_REPO_ROOT — checkout path
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${BOXCI_REPO_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
cd "$REPO_ROOT"

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
bk_export_cursor_path

TIMEOUT="${RADICLE_AGENT_TIMEOUT:-3600}"
MODEL="${RADICLE_AGENT_MODEL:-}"
DRY_RUN="${RADICLE_AGENT_DRY_RUN:-0}"

echo "=== boxci Radicle issue agent ==="
echo "commit: ${BUILDKITE_COMMIT:-unknown}"
echo "branch: ${BUILDKITE_BRANCH:-unknown}"
echo "repo:   $REPO_ROOT"

detect_err="$(mktemp)"
set +e
detect_out="$(bash "$SCRIPT_DIR/detect-issue.sh" 2>"$detect_err")"
detect_rc=$?
set -e
if [[ -s "$detect_err" ]]; then
  cat "$detect_err" >&2
fi
rm -f "$detect_err"

if [[ "$detect_rc" -eq 2 ]]; then
  echo "$detect_out"
  echo "Skipped: not a new Radicle issue event."
  exit 0
fi
if [[ "$detect_rc" -ne 0 ]]; then
  echo "$detect_out" >&2
  bk_die "issue detection failed (exit $detect_rc)"
fi

eval "$(printf '%s\n' "$detect_out" | grep -E '^RADICLE_ISSUE_')"

echo "issue:  $RADICLE_ISSUE_ID"
echo "title:  $RADICLE_ISSUE_TITLE"
echo "branch: $RADICLE_ISSUE_BRANCH"

if git show-ref --verify --quiet "refs/heads/$RADICLE_ISSUE_BRANCH"; then
  msg="Branch \`$RADICLE_ISSUE_BRANCH\` already exists — skipping duplicate agent run."
  echo "$msg"
  exit 0
fi

bash "$SCRIPT_DIR/bootstrap.sh"
bk_export_cursor_path
if ! bk_cursor_agent_cmd >/dev/null; then
  bk_die "cursor-agent / agent not found on PATH after bootstrap (PATH=$PATH)"
fi
echo "cursor-agent: $(command -v "$(bk_cursor_agent_cmd)")"

RADICLE_RID="${RADICLE_RID:-${BOXCI_REPO_ID:-}}"
if [[ -z "$RADICLE_RID" && -n "${BOXCI_REPO_SLUG:-}" ]]; then
  RADICLE_RID="rad:${BOXCI_REPO_SLUG}"
fi
GARDEN_HOST="${RADICLE_GARDEN_HOST:-nandi.radicle.garden}"
RID_NAKED="${RADICLE_RID#rad:}"
RID_NAKED="${RID_NAKED#rad://}"
ISSUE_SHORT="$(bk_short_id "$RADICLE_ISSUE_ID")"
ISSUE_LINK="https://${GARDEN_HOST}/${RID_NAKED}/issues/${RADICLE_ISSUE_ID}"
PATCH_TITLE="Fix: ${RADICLE_ISSUE_TITLE}"
ISSUE_BODY_TEXT="${RADICLE_ISSUE_BODY:-*(no description)*}"
PATCH_DESCRIPTION=$(cat <<EOF
## Summary
Fixes Radicle issue \`${ISSUE_SHORT}\`.

## Issue
- ID: \`${RADICLE_ISSUE_ID}\`
- Title: ${RADICLE_ISSUE_TITLE}
- Link: ${ISSUE_LINK}
- Repo: ${RADICLE_RID}

## Issue description
${ISSUE_BODY_TEXT}

## Context
Opened by boxci issue→agent on branch \`${RADICLE_ISSUE_BRANCH}\`.
EOF
)

VERIFY_HINT=""
if [[ -f "$REPO_ROOT/AGENTS.md" ]]; then
  VERIFY_HINT="Run relevant verification from AGENTS.md when practical."
else
  VERIFY_HINT="Run relevant tests for the changed code."
fi

PROMPT=$(cat <<EOF
A new Radicle issue was opened in this repository. Implement a fix and open a Radicle patch.

Issue ID: ${RADICLE_ISSUE_ID}
Title: ${RADICLE_ISSUE_TITLE}
Description:
${RADICLE_ISSUE_BODY}

Requirements:
1. Read the codebase and implement a fix for this issue.
2. ${VERIFY_HINT}
3. Open a Radicle patch on the \`rad\` remote. The patch MUST have both a title AND a full
   description body (not title-only). Prefer \`git push rad HEAD:refs/patches\` with
   **repeated** \`-o patch.message=...\` (first option = title; each later option = body
   paragraph, joined with a blank line).

   Required title:
   ${PATCH_TITLE}

   Required description:
   -----
${PATCH_DESCRIPTION}
   -----

   Example:
   git checkout -b "${RADICLE_ISSUE_BRANCH}"
   # … commit the fix …
   git push rad HEAD:refs/patches \\
     -o patch.message="${PATCH_TITLE}" \\
     -o patch.message="<Required description from above>"

   - branch: "${RADICLE_ISSUE_BRANCH}"
   - title: "${PATCH_TITLE}"
   - description: the Required description block above
4. Do not close the issue. Only open the patch.

If the issue is not actionable (needs clarification, is a duplicate, etc.), explain why and do not open a patch.
EOF
)

if [[ "$DRY_RUN" == "1" ]]; then
  echo "--- DRY RUN: agent prompt ---"
  echo "$PROMPT"
  exit 0
fi

DELEGATE="$SCRIPT_DIR/delegate.sh"
if [[ ! -x "$DELEGATE" ]]; then
  chmod +x "$DELEGATE"
fi

echo "=== Running cursor-agent (timeout=${TIMEOUT}s) ==="
set +e
agent_args=(--workspace "$REPO_ROOT" --timeout "$TIMEOUT" --force)
if [[ -n "$MODEL" ]]; then
  agent_args+=(--model "$MODEL")
fi
agent_out="$("$DELEGATE" "$PROMPT" "${agent_args[@]}" 2>&1)"
agent_rc=$?
set -e

echo "$agent_out"

if [[ "$agent_rc" -ne 0 ]]; then
  echo "cursor-agent failed (exit $agent_rc)" >&2
  exit "$agent_rc"
fi

PATCH_ID=""
PATCH_ID=$(echo "$agent_out" | grep -oE 'patches/[0-9a-f]{7,40}|Patch[[:space:]]+[0-9a-f]{7,40}|"patch_id"[[:space:]]*:[[:space:]]*"[0-9a-f]+"' | grep -oE '[0-9a-f]{7,40}' | head -1 || true)

if [[ -n "$PATCH_ID" ]] && command -v rad >/dev/null 2>&1; then
  patch_show=$(rad patch show "$PATCH_ID" 2>/dev/null || true)
  if echo "$patch_show" | grep -qF "$ISSUE_LINK"; then
    echo "Patch ${PATCH_ID:0:7} already has structured description — skipping edit"
  else
    echo "=== Ensuring patch description (rad patch edit ${PATCH_ID:0:7}) ==="
    rad patch edit "$PATCH_ID" -m "$PATCH_TITLE" -m "$PATCH_DESCRIPTION" --no-announce 2>&1 || true
  fi
fi

echo "=== Done ==="
