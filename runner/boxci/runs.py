"""Run storage and async pipeline execution."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from boxci.job import publish_job_finished, publish_job_started
from boxci.runner import RunResult, run_pipeline

RUNS: dict[str, RunResult] = {}
_LOCK = threading.Lock()


def _boxci_root_from_env(extra_env: dict[str, str]) -> Path:
    raw = extra_env.get("BOXCI_ROOT") or os.environ.get("BOXCI_ROOT")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2]


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


def list_artifact_disk_runs(
    *,
    boxci_root: Path,
    repo: str | None = None,
    exclude_ids: set[str] | None = None,
) -> list[RunResult]:
    """Synthesize passed runs from on-disk artifact directories (survives process restart)."""
    from boxci.artifacts import list_run_artifacts_fast

    arts_root = boxci_root / "artifacts"
    if not arts_root.is_dir():
        return []

    exclude = exclude_ids or set()
    found: list[RunResult] = []

    for slug_dir in arts_root.iterdir():
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        for run_dir in slug_dir.iterdir():
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            if run_dir.name in exclude:
                continue
            files = [
                p
                for p in run_dir.iterdir()
                if p.is_file() and not p.name.startswith(".")
            ]
            if not files:
                continue

            env = {
                "BOXCI_REPO_SLUG": slug_dir.name,
                "BOXCI_RUN_ID": run_dir.name,
                "BOXCI_TRIGGER": "artifacts",
            }
            stub = RunResult(
                id=run_dir.name,
                pipeline="(on-disk artifacts)",
                status="passed",
                started_at=run_dir.stat().st_mtime,
                finished_at=run_dir.stat().st_mtime,
                env=env,
            )
            if repo and not run_matches_repo(stub, repo):
                continue
            stub.artifacts = list_run_artifacts_fast(boxci_root=boxci_root, env=env)
            if stub.artifacts:
                found.append(stub)

    found.sort(key=_run_recency, reverse=True)
    return found


def list_known_repos(*, boxci_root: Path | None = None) -> list[dict]:
    """Repos discovered from runs plus workspaces/ and artifacts/ on disk."""
    by_slug: dict[str, dict] = {}

    def _placeholder_name(name: str, slug: str) -> bool:
        if not name:
            return True
        if name == slug:
            return True
        if name.endswith("…") and slug.startswith(name[:-1]):
            return True
        return False

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
        if name and (_placeholder_name(str(cur.get("name") or ""), slug) or not cur["name"]):
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
        import os

        from boxci.repo import resolve_repo_name

        for dirname in ("workspaces", "artifacts"):
            root = boxci_root / dirname
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if path.is_dir() and not path.name.startswith("."):
                    derived = ""
                    if dirname == "workspaces":
                        derived = resolve_repo_name(path, path.name)
                    upsert(path.name, name=derived)

        # Per-slug workspace overrides (e.g. BOXCI_WORKSPACE_<slug>=/home/boxd/sleek).
        for key, val in os.environ.items():
            if not key.startswith("BOXCI_WORKSPACE_") or not val.strip():
                continue
            slug = key[len("BOXCI_WORKSPACE_") :]
            # Env keys may use the raw slug; accept as-is.
            path = Path(val.strip())
            if path.is_dir():
                upsert(slug, name=resolve_repo_name(path, slug))

        # Also re-derive for any known slug whose name is still a placeholder.
        for slug, cur in list(by_slug.items()):
            if not _placeholder_name(str(cur.get("name") or ""), slug):
                continue
            candidates = [
                boxci_root / "workspaces" / slug,
            ]
            override = os.environ.get(f"BOXCI_WORKSPACE_{slug}") or os.environ.get(
                f"BOXCI_WORKSPACE_{slug.upper()}"
            )
            if override:
                candidates.insert(0, Path(override))
            for path in candidates:
                if path.is_dir():
                    derived = resolve_repo_name(path, slug)
                    if derived:
                        upsert(slug, name=derived)
                        break

    repos = sorted(
        by_slug.values(),
        key=lambda r: (-float(r.get("last_at") or 0), (r.get("name") or r["slug"]).lower()),
    )
    for r in repos:
        if not r.get("name"):
            r["name"] = r["slug"][:12] + "…" if len(r["slug"]) > 16 else r["slug"]
    return repos


def serialize_run(run: RunResult, *, boxci_root: Path | None = None) -> dict:
    if boxci_root is not None:
        from boxci.artifacts import hydrate_run_artifacts

        hydrate_run_artifacts(run, boxci_root=boxci_root)

    def _public_env(env: dict[str, str] | None) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in (env or {}).items():
            ku = k.upper()
            if any(
                s in ku
                for s in (
                    "SECRET",
                    "TOKEN",
                    "PASSWORD",
                    "PASSPHRASE",
                    "PRIVATE_KEY",
                    "_KEY_ID",
                )
            ) or ku.endswith("_KEY") or ku.endswith("_KEY_ID"):
                continue
            out[k] = v
        return out

    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_s": (run.finished_at or time.time()) - run.started_at,
        "env": _public_env(run.env),
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
    extra_env = dict(extra_env)
    run_id = extra_env.get("BOXCI_RUN_ID") or str(uuid.uuid4())[:8]
    extra_env["BOXCI_RUN_ID"] = run_id
    boxci_root = _boxci_root_from_env(extra_env)

    def _finish(result: RunResult, job_uuid: str | None) -> None:
        try:
            publish_job_finished(
                boxci_root=boxci_root,
                env=extra_env,
                run=result,
                job_uuid=job_uuid,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the CI run
            print(f"[radicle-job] finish error: {exc}", flush=True)
        if on_complete:
            on_complete(result)

    if not async_run:
        job_uuid = None
        try:
            job_uuid = publish_job_started(
                boxci_root=boxci_root,
                env=extra_env,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[radicle-job] start error: {exc}", flush=True)
        run = run_pipeline(pipeline, run_id=run_id, extra_env=extra_env, cwd=cwd)
        store_run(run)
        _finish(run, job_uuid)
        return run

    pending = RunResult(
        id=run_id,
        pipeline=str(pipeline),
        status="running",
        started_at=time.time(),
        env={k: extra_env[k] for k in sorted(extra_env) if k.startswith(("BOXCI_", "GIT_", "RADICLE_"))},
    )
    store_run(pending)

    job_uuid: str | None = None
    try:
        job_uuid = publish_job_started(
            boxci_root=boxci_root,
            env=extra_env,
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[radicle-job] start error: {exc}", flush=True)

    queued_at = pending.started_at
    started_uuid = job_uuid

    def worker() -> None:
        result = run_pipeline(pipeline, run_id=run_id, extra_env=extra_env, cwd=cwd)
        result.started_at = queued_at
        store_run(result)
        _finish(result, started_uuid)

    threading.Thread(target=worker, daemon=True).start()
    return pending
