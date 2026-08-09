#!/usr/bin/env bash
# Publish boxci run status as a Radicle Job COB (xyz.radworks.job).
#
# Usage:
#   publish-radicle-job.sh start
#     → prints job-run UUID on stdout (and JSON diagnostics on stderr)
#   publish-radicle-job.sh finish <succeeded|failed> <job-run-uuid>
#
# Required env:
#   GIT_SHA / BUILDKITE_COMMIT — commit OID
#   RADICLE_RID / BOXCI_REPO_ID — repository id
#   RADICLE_SECRET_KEY (or keys already under $RAD_HOME)
#
# Optional:
#   BOXCI_PUBLIC_URL — base URL for log links (default https://boxci.boxd.sh)
#   BOXCI_RUN_ID / BOXCI_REPO_SLUG — used in log URL
#   BOXCI_RADICLE_JOB=0 — disable
#   RAD_JOB — path to rad-job binary (default: rad-job on PATH)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=radicle-ci-env.sh
source "$SCRIPT_DIR/radicle-ci-env.sh"

if [[ "${BOXCI_RADICLE_JOB:-1}" == "0" || "${BOXCI_RADICLE_JOB:-1}" == "false" ]]; then
  echo "[radicle-job] disabled (BOXCI_RADICLE_JOB=0)" >&2
  exit 0
fi

OID="${GIT_SHA:-${BUILDKITE_COMMIT:-}}"
if [[ -z "$OID" || "$OID" == "unknown" ]]; then
  echo "[radicle-job] skip: no GIT_SHA" >&2
  exit 0
fi

RID="${RADICLE_RID:-${BOXCI_REPO_ID:-}}"
if [[ -z "$RID" && -n "${BOXCI_REPO_SLUG:-}" ]]; then
  RID="rad:${BOXCI_REPO_SLUG}"
fi
if [[ -z "$RID" ]]; then
  echo "[radicle-job] skip: no RADICLE_RID" >&2
  exit 0
fi
if [[ "$RID" != rad:* ]]; then
  RID="rad:${RID#rad://}"
fi

RAD_JOB_BIN="${RAD_JOB:-rad-job}"
if ! command -v "$RAD_JOB_BIN" >/dev/null 2>&1; then
  echo "[radicle-job] skip: rad-job not on PATH (install radicle-job / set RAD_JOB)" >&2
  exit 0
fi

log_url() {
  local base slug run_id
  base="${BOXCI_PUBLIC_URL:-https://boxci.boxd.sh}"
  base="${base%/}"
  slug="${BOXCI_REPO_SLUG:-}"
  run_id="${BOXCI_RUN_ID:-}"
  if [[ -n "$slug" && -n "$run_id" ]]; then
    printf '%s/repos/%s/runs/%s' "$base" "$slug" "$run_id"
  elif [[ -n "$run_id" ]]; then
    printf '%s/runs/%s' "$base" "$run_id"
  else
    printf '%s/' "$base"
  fi
}

ensure_job_for_commit() {
  # Reuse an existing Job COB for this commit when present; otherwise create.
  if "$RAD_JOB_BIN" --repository "$RID" --no-sync show "$OID" >/dev/null 2>&1; then
    return 0
  fi
  echo "[radicle-job] creating job COB for ${OID:0:7}" >&2
  "$RAD_JOB_BIN" --repository "$RID" --no-sync new "$OID" >/dev/null
}

cmd_start() {
  if ! ensure_radicle_ci_env; then
    echo "[radicle-job] skip: could not prepare Radicle CI env" >&2
    exit 0
  fi

  ensure_job_for_commit

  local url uuid
  url="$(log_url)"
  echo "[radicle-job] starting run oid=${OID:0:7} log=${url}" >&2
  uuid="$("$RAD_JOB_BIN" --repository "$RID" run "$OID" "$url")"
  # Announce after mutations (run already syncs unless --no-sync; we sync here
  # for new+run when new used --no-sync).
  rad sync "$RID" -a >/dev/null 2>&1 || true
  printf '%s\n' "$uuid"
}

cmd_finish() {
  local outcome="${1:-}"
  local uuid="${2:-}"
  if [[ -z "$outcome" || -z "$uuid" ]]; then
    echo "usage: publish-radicle-job.sh finish <succeeded|failed> <uuid>" >&2
    exit 2
  fi
  if ! ensure_radicle_ci_env; then
    echo "[radicle-job] skip finish: could not prepare Radicle CI env" >&2
    exit 0
  fi

  case "$outcome" in
    succeeded|success|passed|ok)
      echo "[radicle-job] marking run ${uuid} succeeded" >&2
      "$RAD_JOB_BIN" --repository "$RID" succeeded "$OID" "$uuid"
      ;;
    failed|failure|error)
      echo "[radicle-job] marking run ${uuid} failed" >&2
      "$RAD_JOB_BIN" --repository "$RID" failed "$OID" "$uuid"
      ;;
    *)
      echo "[radicle-job] unknown outcome: $outcome" >&2
      exit 2
      ;;
  esac
}

main() {
  local action="${1:-}"
  case "$action" in
    start)
      cmd_start
      ;;
    finish)
      shift
      cmd_finish "$@"
      ;;
    -h|--help|help|"")
      sed -n '2,20p' "$0" | sed 's/^# \?//'
      ;;
    *)
      echo "unknown action: $action" >&2
      exit 2
      ;;
  esac
}

main "$@"
