#!/usr/bin/env bash
# Detect whether BUILDKITE_COMMIT is a newly opened Radicle issue.
# On success, prints exportable shell assignments to stdout:
#   RADICLE_ISSUE_ID, RADICLE_ISSUE_TITLE, RADICLE_ISSUE_BODY, RADICLE_ISSUE_BRANCH
#
# Exit 0  — issue detected (assignments printed)
# Exit 1  — error
# Exit 2  — not an issue event (skip agent)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

COMMIT="${BUILDKITE_COMMIT:-}"
if [[ -z "$COMMIT" ]]; then
  echo "BUILDKITE_COMMIT is not set" >&2
  exit 1
fi

# rad is optional when RADICLE_ISSUE_ID is provided (Garden HTTPS checkouts).
if [[ -z "${RADICLE_ISSUE_ID:-}" ]]; then
  bk_require_cmd rad
  bk_require_rad_repo
fi

# Issue opens create a COB root commit that does not contain .buildkite/pipeline.yml,
# so the Radicle Buildkite adapter skips them. Trigger builds on main instead and
# pass the issue id explicitly (see scripts/buildkite/trigger-issue-build.sh / poll).
if [[ -n "${RADICLE_ISSUE_ID:-}" ]]; then
  ISSUE_ID="$RADICLE_ISSUE_ID"
  if ! bk_issue_cob_exists "$ISSUE_ID"; then
    echo "warn: could not verify issue COB ${ISSUE_ID:0:7} via rad/git — continuing with RADICLE_ISSUE_ID" >&2
  fi
elif bk_commit_is_new_issue "$COMMIT"; then
  ISSUE_ID="$COMMIT"
else
  echo "commit $COMMIT is not a new issue COB root (set RADICLE_ISSUE_ID to trigger from main)" >&2
  exit 2
fi

TITLE=""
BODY=""
if command -v python3 >/dev/null 2>&1; then
  if mapfile -t _details < <(bk_issue_details "$ISSUE_ID" 2>/dev/null); then
    TITLE="${_details[0]:-}"
    BODY="${_details[1]:-}"
  fi
fi

if [[ -z "$TITLE" ]]; then
  TITLE="Radicle issue $(bk_short_id "$ISSUE_ID")"
fi

SHORT="$(bk_short_id "$ISSUE_ID")"
BRANCH="issue/${SHORT}"

printf 'RADICLE_ISSUE_ID=%q\n' "$ISSUE_ID"
printf 'RADICLE_ISSUE_TITLE=%q\n' "$TITLE"
printf 'RADICLE_ISSUE_BODY=%q\n' "$BODY"
printf 'RADICLE_ISSUE_BRANCH=%q\n' "$BRANCH"
