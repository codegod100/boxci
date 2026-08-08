"""Built-in Radicle issue poll + cursor-agent → patch (no repo .boxci steps required)."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from boxci.repo import (
    checkout_repo,
    issue_cob_exists,
    list_issue_ids,
    remote_branch_exists,
    repo_slug,
    resolve_repo_name,
    resolve_repo_url,
)
from boxci.runner import RunResult, StepResult
from boxci.runs import execute_pipeline, store_run

_SCRIPTS = Path(__file__).resolve().parent / "scripts"
_REPO_AGENT = "scripts/buildkite/run-issue-agent.sh"


def scripts_dir() -> Path:
    return _SCRIPTS


def issue_branch(issue_id: str) -> str:
    return f"issue/{issue_id[:7]}"


def resolve_radicle_rid(repo_id: str | None, slug: str) -> str:
    if repo_id:
        rid = repo_id.strip()
        if rid.startswith("rad:"):
            return rid
        if rid.startswith("rad://"):
            return f"rad:{rid[6:]}"
    return f"rad:{slug}"


def find_issue_agent_script(workspace: Path) -> Path:
    repo_script = workspace / _REPO_AGENT
    if repo_script.is_file():
        return repo_script
    return _SCRIPTS / "run-issue-agent.sh"


def build_issue_env(
    *,
    trigger: str,
    workspace: Path,
    repo_url: str,
    slug: str,
    branch: str,
    sha: str,
    repo_id: str | None = None,
    issue_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    rid = resolve_radicle_rid(repo_id, slug)
    extra: dict[str, str] = {
        "BOXCI_TRIGGER": trigger,
        "BOXCI_REPO_ROOT": str(workspace),
        "BOXCI_REPO_URL": repo_url,
        "BOXCI_REPO_SLUG": slug,
        "BOXCI_REPO_ID": repo_id or rid,
        "GIT_SHA": sha,
        "GIT_BRANCH": branch,
        "GIT_TERMINAL_PROMPT": "0",
        "BUILDKITE_COMMIT": sha,
        "BUILDKITE_BRANCH": branch,
        "RADICLE_TRIGGER": trigger,
        "RADICLE_RID": rid,
        "RADICLE_GARDEN_GIT": repo_url,
        "BOXCI_SCRIPTS": str(_SCRIPTS),
    }
    if issue_id:
        extra["RADICLE_ISSUE_ID"] = issue_id
        extra["BUILDKITE_COMMIT"] = issue_id
    if dry_run:
        extra["RADICLE_AGENT_DRY_RUN"] = "1"
    repo_name = resolve_repo_name(workspace, slug)
    if repo_name:
        extra["BOXCI_REPO_NAME"] = repo_name
    return extra


def _run_script(
    script: Path,
    extra_env: dict[str, str],
    *,
    cwd: Path,
    run_id: str | None = None,
    label: str = "issue agent",
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
        started_at=time.time(),
        env={k: env[k] for k in sorted(env) if k.startswith(("BOXCI_", "GIT_", "RADICLE_", "BUILDKITE_"))},
    )
    step = StepResult(key="issue-agent", label=label, status="running")
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


def run_issue_agent(
    *,
    boxci_root: Path,
    repo_url: str,
    branch: str = "main",
    repo_id: str | None = None,
    issue_id: str,
    dry_run: bool = False,
    async_run: bool = False,
) -> tuple[Path, dict[str, str], RunResult]:
    """Checkout repo and run issue agent (repo script or boxci builtin)."""
    slug = repo_slug(repo_id or "", repo_url)
    workspace = _repo_workspace(boxci_root, slug)

    checkout_repo(repo_url, workspace, branch=branch, sha=None)
    sha = _git_head(workspace)

    if not issue_cob_exists(issue_id, repo_url=repo_url, repo_root=workspace):
        raise ValueError(f"commit {issue_id[:7]} is not a xyz.radicle.issue COB")

    script = find_issue_agent_script(workspace)
    env = build_issue_env(
        trigger="issue",
        workspace=workspace,
        repo_url=repo_url,
        slug=slug,
        branch=branch,
        sha=sha,
        repo_id=repo_id,
        issue_id=issue_id,
        dry_run=dry_run,
    )

    if async_run:
        run = _run_script_async(script, env, cwd=workspace, label=f"Issue {issue_id[:7]} → agent")
        store_run(run)
    else:
        run = _run_script(script, env, cwd=workspace, label=f"Issue {issue_id[:7]} → agent")

    return script, env, run


def run_poll(
    *,
    boxci_root: Path,
    repo_url: str,
    branch: str = "main",
    repo_id: str | None = None,
    dry_run: bool = False,
    async_run: bool = False,
) -> dict[str, Any]:
    """List issue COBs, skip branches that exist, dispatch issue agent runs."""
    slug = repo_slug(repo_id or "", repo_url)
    workspace = _repo_workspace(boxci_root, slug)
    checkout_repo(repo_url, workspace, branch=branch, sha=None)

    issue_ids = list_issue_ids(repo_url)
    pending: list[str] = []
    skipped: list[str] = []

    for iid in issue_ids:
        br = issue_branch(iid)
        if remote_branch_exists(repo_url, br):
            skipped.append(iid)
            continue
        pending.append(iid)

    result: dict[str, Any] = {
        "total_issues": len(issue_ids),
        "pending": len(pending),
        "skipped": [f"{i[:7]} (branch exists)" for i in skipped],
        "dispatched": [],
    }

    if not pending:
        env = build_issue_env(
            trigger="poll",
            workspace=workspace,
            repo_url=repo_url,
            slug=slug,
            branch=branch,
            sha=_git_head(workspace),
            repo_id=repo_id,
            dry_run=dry_run,
        )
        run = RunResult(
            id=str(uuid.uuid4())[:8],
            pipeline="builtin:poll",
            status="passed",
            started_at=time.time(),
            finished_at=time.time(),
            env=env,
            steps=[
                StepResult(
                    key="poll",
                    label="Poll issues",
                    status="passed",
                    exit_code=0,
                    output=f"poll: {len(issue_ids)} issue(s), nothing to dispatch\n",
                )
            ],
        )
        result["run"] = run
        result["env"] = env
        result["script"] = str(_SCRIPTS / "poll")
        return result

    runs: list[RunResult] = []
    for iid in pending:
        if dry_run:
            msg = f"poll: would dispatch {iid[:7]} ({iid})"
            run = RunResult(
                id=str(uuid.uuid4())[:8],
                pipeline="builtin:poll-dry-run",
                status="passed",
                started_at=time.time(),
                finished_at=time.time(),
                steps=[
                    StepResult(
                        key=f"issue-{iid[:7]}",
                        label=f"Dry-run {iid[:7]}",
                        status="passed",
                        exit_code=0,
                        output=msg + "\n",
                    )
                ],
            )
            runs.append(run)
            result["dispatched"].append({"issue_id": iid, "dry_run": True, "run_id": run.id})
            continue

        _script, _env, run = run_issue_agent(
            boxci_root=boxci_root,
            repo_url=repo_url,
            branch=branch,
            repo_id=repo_id,
            issue_id=iid,
            dry_run=False,
            async_run=async_run,
        )
        store_run(run)
        runs.append(run)
        result["dispatched"].append({"issue_id": iid, "run_id": run.id, "status": run.status})

    poll_env = build_issue_env(
        trigger="poll",
        workspace=workspace,
        repo_url=repo_url,
        slug=slug,
        branch=branch,
        sha=_git_head(workspace),
        repo_id=repo_id,
    )
    summary = (
        f"poll: {len(issue_ids)} issue COB(s), {len(pending)} to dispatch, "
        f"{len(skipped)} skipped (branch exists)\n"
    )
    for item in result["dispatched"]:
        summary += f"  dispatch {item['issue_id'][:7]} → run {item['run_id']}\n"

    poll_run = RunResult(
        id=str(uuid.uuid4())[:8],
        pipeline="builtin:poll",
        status="passed" if all(r.status != "failed" for r in runs) else "failed",
        started_at=time.time(),
        finished_at=time.time(),
        env=poll_env,
        steps=[
            StepResult(
                key="poll",
                label="Poll issues → dispatch",
                status="passed",
                exit_code=0,
                output=summary,
            )
        ],
    )
    result["run"] = poll_run
    result["env"] = poll_env
    result["script"] = "builtin:poll"
    result["runs"] = runs
    return result


def _run_script_async(
    script: Path,
    env: dict[str, str],
    *,
    cwd: Path,
    label: str,
) -> RunResult:
    import threading

    run_id = str(uuid.uuid4())[:8]
    pending = RunResult(
        id=run_id,
        pipeline=f"builtin:{script.name}",
        status="running",
        started_at=time.time(),
        env={k: env[k] for k in sorted(env) if k.startswith(("BOXCI_", "GIT_", "RADICLE_", "BUILDKITE_"))},
    )
    store_run(pending)

    def worker() -> None:
        result = _run_script(script, env, cwd=cwd, run_id=run_id, label=label)
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
