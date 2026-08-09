"""Publish boxci runs as Radicle Job COBs via rad-job / publish-radicle-job.sh."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent / "scripts"
_PUBLISH = _SCRIPTS / "publish-radicle-job.sh"

# BOXCI_RUN_ID → job-run UUID (in-process; also mirrored under state/)
_LOCK = threading.Lock()
_JOB_RUNS: dict[str, str] = {}


def _enabled(env: dict[str, str] | None = None) -> bool:
    src = env if env is not None else os.environ
    flag = (src.get("BOXCI_RADICLE_JOB") or os.environ.get("BOXCI_RADICLE_JOB") or "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    # Only merge CI publishes Job COBs (issue agent is a different workflow).
    trigger = (src.get("BOXCI_TRIGGER") or "").strip()
    if trigger and trigger != "merge":
        return False
    sha = (src.get("GIT_SHA") or src.get("BUILDKITE_COMMIT") or "").strip()
    if not sha:
        return False
    rid = (src.get("RADICLE_RID") or src.get("BOXCI_REPO_ID") or "").strip()
    if not rid and not (src.get("BOXCI_REPO_SLUG") or "").strip():
        return False
    return True


def _state_path(boxci_root: Path, run_id: str) -> Path:
    return boxci_root / "state" / "radicle-jobs" / f"{run_id}.json"


def _save_job_uuid(boxci_root: Path, run_id: str, job_uuid: str, env: dict[str, str]) -> None:
    with _LOCK:
        _JOB_RUNS[run_id] = job_uuid
    path = _state_path(boxci_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "boxci_run_id": run_id,
        "job_run_uuid": job_uuid,
        "oid": env.get("GIT_SHA") or env.get("BUILDKITE_COMMIT") or "",
        "rid": env.get("RADICLE_RID") or env.get("BOXCI_REPO_ID") or "",
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _load_job_uuid(boxci_root: Path, run_id: str) -> str | None:
    with _LOCK:
        cached = _JOB_RUNS.get(run_id)
    if cached:
        return cached
    path = _state_path(boxci_root, run_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    uuid = str(data.get("job_run_uuid") or "").strip()
    if uuid:
        with _LOCK:
            _JOB_RUNS[run_id] = uuid
    return uuid or None


def _run_publish(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    proc_env = dict(os.environ)
    proc_env.update({k: str(v) for k, v in env.items() if v is not None})
    # Ensure script helpers + rad-job are reachable when installed under RAD_HOME.
    rad_home = proc_env.get("RAD_HOME") or str(Path.home() / ".radicle")
    proc_env["RAD_HOME"] = rad_home
    path = proc_env.get("PATH", "")
    proc_env["PATH"] = f"{rad_home}/bin:{_SCRIPTS}:{path}"
    return subprocess.run(
        ["bash", str(_PUBLISH), *args],
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=int(proc_env.get("BOXCI_RADICLE_JOB_TIMEOUT", "180")),
    )


def job_log_url(env: dict[str, str]) -> str:
    base = (env.get("BOXCI_PUBLIC_URL") or os.environ.get("BOXCI_PUBLIC_URL") or "https://boxci.boxd.sh").rstrip(
        "/"
    )
    slug = (env.get("BOXCI_REPO_SLUG") or "").strip()
    run_id = (env.get("BOXCI_RUN_ID") or "").strip()
    if slug and run_id:
        return f"{base}/repos/{slug}/runs/{run_id}"
    if run_id:
        return f"{base}/runs/{run_id}"
    return f"{base}/"


def publish_job_started(
    *,
    boxci_root: Path,
    env: dict[str, str],
    run_id: str,
) -> str | None:
    """Create/reuse Job COB and record a Started run. Returns job-run UUID or None."""
    if not _enabled(env):
        return None
    if not _PUBLISH.is_file():
        return None

    publish_env = dict(env)
    publish_env.setdefault("BOXCI_RUN_ID", run_id)
    publish_env.setdefault("BOXCI_ROOT", str(boxci_root))

    try:
        proc = _run_publish(["start"], publish_env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[radicle-job] start failed: {exc}", flush=True)
        return None

    if proc.stderr:
        print(proc.stderr.rstrip(), flush=True)
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout.rstrip(), flush=True)
        print(f"[radicle-job] start exited {proc.returncode}", flush=True)
        return None

    uuid = (proc.stdout or "").strip().splitlines()
    job_uuid = uuid[-1].strip() if uuid else ""
    if not job_uuid:
        print("[radicle-job] start produced no UUID (skipped or soft-fail)", flush=True)
        return None

    _save_job_uuid(boxci_root, run_id, job_uuid, publish_env)
    print(f"[radicle-job] started run {job_uuid} log={job_log_url(publish_env)}", flush=True)
    return job_uuid


def publish_job_finished(
    *,
    boxci_root: Path,
    env: dict[str, str],
    run: Any,
    job_uuid: str | None = None,
) -> None:
    """Mark the Job COB run succeeded/failed. Never raises."""
    if not _enabled(env):
        return
    if not _PUBLISH.is_file():
        return

    uuid = job_uuid or _load_job_uuid(boxci_root, run.id)
    if not uuid:
        return

    outcome = "succeeded" if run.status == "passed" else "failed"
    publish_env = dict(env)
    publish_env.setdefault("BOXCI_RUN_ID", run.id)
    publish_env.setdefault("BOXCI_ROOT", str(boxci_root))
    run_env = getattr(run, "env", None) or {}
    # Prefer SHA from the finished run env.
    for key in ("GIT_SHA", "BUILDKITE_COMMIT", "RADICLE_RID", "BOXCI_REPO_ID", "BOXCI_REPO_SLUG"):
        if run_env.get(key) and not publish_env.get(key):
            publish_env[key] = run_env[key]

    try:
        proc = _run_publish(["finish", outcome, uuid], publish_env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[radicle-job] finish failed: {exc}", flush=True)
        return

    if proc.stderr:
        print(proc.stderr.rstrip(), flush=True)
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout.rstrip(), flush=True)
        print(f"[radicle-job] finish exited {proc.returncode}", flush=True)
        return

    print(f"[radicle-job] finished run {uuid} as {outcome}", flush=True)
