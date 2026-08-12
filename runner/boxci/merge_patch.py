"""Built-in: merge a Radicle patch into main via ``rad patch merge``."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from boxci.repo import checkout_repo, repo_slug, resolve_repo_name, resolve_repo_url
from boxci.runner import RunResult, StepResult
from boxci.runs import store_run

_SCRIPTS = Path(__file__).resolve().parent / "scripts"
_SCRIPT_NAME = "merge-patch.sh"


def scripts_dir() -> Path:
    return _SCRIPTS


def resolve_radicle_rid(repo_id: str | None, slug: str) -> str:
    if repo_id:
        rid = repo_id.strip()
        if rid.startswith("rad:"):
            return rid
        if rid.startswith("rad://"):
            return f"rad:{rid[6:]}"
        return f"rad:{rid}"
    return f"rad:{slug}"


def build_merge_patch_env(
    *,
    workspace: Path,
    repo_url: str,
    slug: str,
    branch: str,
    patch_id: str,
    repo_id: str | None = None,
    merge_message: str | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    rid = resolve_radicle_rid(repo_id, slug)
    extra: dict[str, str] = {
        "BOXCI_TRIGGER": "patch-merge",
        "BOXCI_REPO_ROOT": str(workspace),
        "BOXCI_REPO_URL": repo_url,
        "BOXCI_REPO_SLUG": slug,
        "BOXCI_REPO_ID": repo_id or rid,
        "GIT_SHA": "",
        "GIT_BRANCH": branch,
        "GIT_TERMINAL_PROMPT": "0",
        "RADICLE_TRIGGER": "patch-merge",
        "RADICLE_RID": rid,
        "RADICLE_GARDEN_GIT": repo_url,
        "RADICLE_PATCH_ID": patch_id,
        "BOXCI_SCRIPTS": str(_SCRIPTS),
    }
    if merge_message:
        extra["PATCH_MERGE_MESSAGE"] = merge_message
    if dry_run:
        extra["RADICLE_AGENT_DRY_RUN"] = "1"
    repo_name = resolve_repo_name(workspace, slug)
    if repo_name:
        extra["BOXCI_REPO_NAME"] = repo_name
    return extra


def _public_run_env(env: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in sorted(env):
        if not k.startswith(("BOXCI_", "GIT_", "RADICLE_", "PATCH_")):
            continue
        if k in ("GITHUB_TOKEN", "RADICLE_SECRET_KEY", "RAD_PASSPHRASE"):
            continue
        ku = k.upper()
        if ku.endswith("_KEY") or "SECRET" in ku or "TOKEN" in ku or "PASSWORD" in ku:
            continue
        out[k] = env[k]
    return out


def _run_script(
    script: Path,
    extra_env: dict[str, str],
    *,
    cwd: Path,
    run_id: str | None = None,
    label: str = "patch merge",
    started_at: float | None = None,
) -> RunResult:
    run_id = run_id or str(uuid.uuid4())[:8]
    env = dict(os.environ)
    env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    env["BOXCI_RUN_ID"] = run_id
    env.setdefault("PATH", os.environ.get("PATH", ""))

    run = RunResult(
        id=run_id,
        pipeline=f"builtin:{script.name}",
        status="running",
        started_at=started_at or time.time(),
        env=_public_run_env(env),
    )
    step = StepResult(key="patch-merge", label=label, status="running")
    run.steps.append(step)

    t0 = time.time()
    proc = subprocess.run(
        ["bash", str(script)],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    step.duration_s = time.time() - t0
    step.exit_code = proc.returncode
    step.output = (proc.stdout or "") + (proc.stderr or "")
    step.status = "passed" if proc.returncode == 0 else "failed"
    run.status = step.status
    run.finished_at = time.time()
    return run


def _run_script_async(
    script: Path,
    env: dict[str, str],
    *,
    cwd: Path,
    label: str,
) -> RunResult:
    import threading

    run_id = str(uuid.uuid4())[:8]
    queued_at = time.time()
    pending = RunResult(
        id=run_id,
        pipeline=f"builtin:{script.name}",
        status="running",
        started_at=queued_at,
        env=_public_run_env(env),
    )
    store_run(pending)

    def worker() -> None:
        result = _run_script(
            script, env, cwd=cwd, run_id=run_id, label=label, started_at=queued_at
        )
        store_run(result)

    threading.Thread(target=worker, daemon=True).start()
    return pending


def _repo_workspace(boxci_root: Path, slug: str) -> Path:
    override = os.environ.get(f"BOXCI_WORKSPACE_{slug.upper()}") or os.environ.get(
        f"BOXCI_WORKSPACE_{slug}"
    )
    if override:
        return Path(override)
    return boxci_root / "workspaces" / slug


def _git_head(workspace: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def run_patch_merge(
    *,
    boxci_root: Path,
    repo_url: str | None = None,
    repo_id: str | None = None,
    patch_id: str,
    branch: str = "main",
    merge_message: str | None = None,
    dry_run: bool = False,
    async_run: bool = False,
) -> tuple[Path, dict[str, str], RunResult]:
    """Checkout Radicle repo and merge a patch into main via ``rad patch merge``."""
    patch_id = (patch_id or "").strip()
    if not patch_id:
        raise ValueError("patch_id required")

    if not repo_url:
        if not repo_id:
            raise ValueError("repo_url or repo / repo_id required")
        repo_url = resolve_repo_url(repo_id) or ""
    if not repo_url:
        raise ValueError("could not resolve Radicle repo clone URL")

    slug = repo_slug(repo_id or "", repo_url)
    workspace = _repo_workspace(boxci_root, slug)
    checkout_repo(repo_url, workspace, branch=branch, sha=None)

    script = _SCRIPTS / _SCRIPT_NAME
    if not script.is_file():
        raise FileNotFoundError(f"missing builtin script: {script}")

    env = build_merge_patch_env(
        workspace=workspace,
        repo_url=repo_url,
        slug=slug,
        branch=branch,
        patch_id=patch_id,
        repo_id=repo_id,
        merge_message=merge_message,
        dry_run=dry_run,
    )

    label = f"Merge patch {patch_id[:7]}"
    if async_run:
        run = _run_script_async(script, env, cwd=workspace, label=label)
        store_run(run)
    else:
        run = _run_script(script, env, cwd=workspace, label=label)

    return script, env, run
