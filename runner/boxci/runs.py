"""Run storage and async pipeline execution."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from boxci.runner import RunResult, run_pipeline

RUNS: dict[str, RunResult] = {}
_LOCK = threading.Lock()


def store_run(run: RunResult) -> RunResult:
    with _LOCK:
        RUNS[run.id] = run
    return run


def get_run(run_id: str) -> RunResult | None:
    with _LOCK:
        return RUNS.get(run_id)


def _run_recency(run: RunResult) -> float:
    """Newest-first ordering: running runs by start time, else by finish time."""
    if run.status == "running":
        return run.started_at
    return run.finished_at or run.started_at


def run_repo_slug(env: dict[str, str] | None) -> str:
    """Stable slug used to group a run under a repo."""
    if not env:
        return ""
    slug = (env.get("BOXCI_REPO_SLUG") or "").strip()
    if slug:
        return slug
    for key in ("RADICLE_RID", "BOXCI_REPO_ID"):
        val = (env.get(key) or "").strip()
        if not val:
            continue
        if val.startswith("rad:"):
            return val[4:]
        if val.startswith("rad://"):
            return val[6:]
        return val
    return ""


def run_matches_repo(run: RunResult, repo: str) -> bool:
    """True if run belongs to repo (slug, RID, or display name)."""
    needle = (repo or "").strip()
    if not needle:
        return True
    if needle.startswith("rad:"):
        needle = needle[4:]
    elif needle.startswith("rad://"):
        needle = needle[6:]

    env = run.env or {}
    slug = run_repo_slug(env)
    if slug and slug == needle:
        return True

    name = (env.get("BOXCI_REPO_NAME") or "").strip()
    if name and name == needle:
        return True

    for key in ("RADICLE_RID", "BOXCI_REPO_ID"):
        val = (env.get(key) or "").strip()
        if not val:
            continue
        naked = val[4:] if val.startswith("rad:") else val[6:] if val.startswith("rad://") else val
        if naked == needle or val == needle:
            return True
    return False


def list_runs(limit: int = 50, *, repo: str | None = None) -> list[RunResult]:
    with _LOCK:
        runs = sorted(RUNS.values(), key=_run_recency, reverse=True)
    if repo:
        runs = [r for r in runs if run_matches_repo(r, repo)]
    return runs[:limit]


def list_known_repos(*, boxci_root: Path | None = None) -> list[dict]:
    """Repos discovered from runs plus workspaces/ and artifacts/ on disk."""
    by_slug: dict[str, dict] = {}

    def upsert(
        slug: str,
        *,
        name: str = "",
        rid: str = "",
        url: str = "",
        last_status: str = "",
        last_at: float = 0.0,
        run_count: int = 0,
    ) -> None:
        if not slug:
            return
        cur = by_slug.get(slug)
        if cur is None:
            by_slug[slug] = {
                "slug": slug,
                "name": name,
                "rid": rid or (f"rad:{slug}" if len(slug) >= 43 else ""),
                "url": url,
                "last_status": last_status,
                "last_at": last_at,
                "run_count": run_count,
            }
            return
        if name and not cur["name"]:
            cur["name"] = name
        if rid and not cur["rid"]:
            cur["rid"] = rid
        if url and not cur["url"]:
            cur["url"] = url
        if last_at and last_at >= float(cur.get("last_at") or 0):
            cur["last_at"] = last_at
            if last_status:
                cur["last_status"] = last_status
        if run_count:
            cur["run_count"] = int(cur.get("run_count") or 0) + run_count

    with _LOCK:
        runs = list(RUNS.values())

    for run in runs:
        env = run.env or {}
        slug = run_repo_slug(env)
        if not slug:
            continue
        rid = (env.get("RADICLE_RID") or env.get("BOXCI_REPO_ID") or "").strip()
        if rid and not rid.startswith("rad:"):
            rid = f"rad:{rid}"
        upsert(
            slug,
            name=(env.get("BOXCI_REPO_NAME") or "").strip(),
            rid=rid,
            url=(env.get("BOXCI_REPO_URL") or "").strip(),
            last_status=run.status,
            last_at=_run_recency(run),
            run_count=1,
        )

    if boxci_root is not None:
        for dirname in ("workspaces", "artifacts"):
            root = boxci_root / dirname
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if path.is_dir() and not path.name.startswith("."):
                    upsert(path.name)

    repos = sorted(
        by_slug.values(),
        key=lambda r: (-float(r.get("last_at") or 0), (r.get("name") or r["slug"]).lower()),
    )
    for r in repos:
        if not r.get("name"):
            r["name"] = r["slug"][:12] + "…" if len(r["slug"]) > 16 else r["slug"]
    return repos


def serialize_run(run: RunResult) -> dict:
    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_s": (run.finished_at or time.time()) - run.started_at,
        "env": run.env,
        "artifacts": [
            {
                "name": a.name,
                "url": a.url,
                "size": a.size,
                "b2_key": a.b2_key,
            }
            for a in run.artifacts
        ],
        "steps": [
            {
                "key": s.key,
                "label": s.label,
                "status": s.status,
                "exit_code": s.exit_code,
                "duration_s": s.duration_s,
                "output_tail": "\n".join((s.output or "").strip().splitlines()[-30:]),
            }
            for s in run.steps
        ],
    }


def execute_pipeline(
    pipeline: Path,
    extra_env: dict[str, str],
    *,
    cwd: Path | None = None,
    async_run: bool = False,
    on_complete: Callable[[RunResult], None] | None = None,
) -> RunResult:
    if not async_run:
        run = run_pipeline(pipeline, extra_env=extra_env, cwd=cwd)
        return store_run(run)

    import uuid

    run_id = extra_env.get("BOXCI_RUN_ID") or str(uuid.uuid4())[:8]
    extra_env = dict(extra_env)
    extra_env["BOXCI_RUN_ID"] = run_id

    pending = RunResult(
        id=run_id,
        pipeline=str(pipeline),
        status="running",
        started_at=time.time(),
        env={k: extra_env[k] for k in sorted(extra_env) if k.startswith(("BOXCI_", "GIT_", "RADICLE_"))},
    )
    store_run(pending)

    queued_at = pending.started_at

    def worker() -> None:
        result = run_pipeline(pipeline, run_id=run_id, extra_env=extra_env, cwd=cwd)
        result.started_at = queued_at
        store_run(result)
        if on_complete:
            on_complete(result)

    threading.Thread(target=worker, daemon=True).start()
    return pending
