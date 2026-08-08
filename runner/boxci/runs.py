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


def list_runs(limit: int = 50) -> list[RunResult]:
    with _LOCK:
        runs = sorted(RUNS.values(), key=_run_recency, reverse=True)
    return runs[:limit]


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
