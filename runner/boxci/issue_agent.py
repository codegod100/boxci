"""Built-in Radicle issue → Think sandbox → patch (webhook/manual only)."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path
from boxci.repo import (
    checkout_repo,
    issue_cob_exists,
    repo_slug,
    resolve_repo_name,
)
from boxci.runner import RunResult, StepResult
from boxci.runs import store_run
from boxci.think_sandbox import (
    format_think_output,
    run_think_job,
    think_enabled,
    think_job_ok,
)

_SCRIPTS = Path(__file__).resolve().parent / "scripts"
_REPO_AGENT = "scripts/buildkite/run-issue-agent.sh"


def scripts_dir() -> Path:
    return _SCRIPTS


def resolve_radicle_rid(repo_id: str | None, slug: str) -> str:
    if repo_id:
        rid = repo_id.strip()
        if rid.startswith("rad:"):
            return rid
        if rid.startswith("rad://"):
            return f"rad:{rid[6:]}"
    return f"rad:{slug}"


def find_issue_agent_script(workspace: Path) -> Path:
    # Builtin boxci scripts (comments, auth fixes) — repo buildkite copy is legacy fallback.
    bundled = _SCRIPTS / "run-issue-agent.sh"
    if bundled.is_file():
        return bundled
    repo_script = workspace / _REPO_AGENT
    if repo_script.is_file():
        return repo_script
    return bundled


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
        "BOXCI_AGENT_BACKEND": "think",
        "THINK_URL": os.environ.get("THINK_URL", "https://think.latha.org"),
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


def _issue_prompt(issue_id: str, repo: str, branch: str) -> str:
    return f"""A new Radicle issue was opened. The repository is already cloned at /workspace in this Think sandbox.

Issue ID: {issue_id}
Repo: {repo}
Base branch: {branch}

Use sandbox_exec, sandbox_read, and sandbox_write to inspect and edit files under /workspace.

Requirements:
1. Read the issue (rad issue show {issue_id} if rad is available, or infer from git/cobs).
2. Implement a fix in /workspace.
3. Open a Radicle patch on the rad remote. The patch MUST have a title AND a description body.
   Prefer: git push rad HEAD:refs/patches with repeated -o patch.message=...
4. Do not close the issue. Only open the patch.

If the issue is not actionable, explain why and do not open a patch.
"""


def _run_think_issue(
    extra_env: dict[str, str],
    *,
    cwd: Path,
    run_id: str | None = None,
    label: str = "issue agent",
    started_at: float | None = None,
) -> RunResult:
    run_id = run_id or str(uuid.uuid4())[:8]
    env = dict(os.environ)
    env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    env["BOXCI_RUN_ID"] = run_id
    run = RunResult(
        id=run_id,
        pipeline="builtin:think-sandbox-agent",
        status="running",
        started_at=started_at or time.time(),
        env={k: env[k] for k in sorted(env) if k.startswith(("BOXCI_", "GIT_", "RADICLE_", "BUILDKITE_", "THINK_"))},
    )
    step = StepResult(key="issue-agent", label=label, status="running")
    run.steps.append(step)
    t0 = time.time()
    issue_id = extra_env.get("RADICLE_ISSUE_ID") or extra_env.get("GIT_SHA") or ""
    try:
        result = run_think_job(
            {
                "action": "agent",
                "run_id": run_id,
                "repo_url": extra_env["BOXCI_REPO_URL"],
                "repo": extra_env.get("RADICLE_RID") or extra_env.get("BOXCI_REPO_ID"),
                "branch": extra_env.get("GIT_BRANCH") or "main",
                "sha": extra_env.get("GIT_SHA"),
                "prompt": _issue_prompt(
                    issue_id,
                    extra_env.get("RADICLE_RID") or extra_env.get("BOXCI_REPO_ID") or "",
                    extra_env.get("GIT_BRANCH") or "main",
                ),
                "dry_run": extra_env.get("RADICLE_AGENT_DRY_RUN") in ("1", "true", "yes"),
            },
            timeout_s=int(os.environ.get("BOXCI_THINK_TIMEOUT", "1800")),
        )
        step.output = format_think_output(result)
        ok = think_job_ok(result)
        step.exit_code = 0 if ok else 1
        step.status = "passed" if ok else "failed"
    except Exception as exc:  # noqa: BLE001
        step.output = str(exc)
        step.exit_code = 1
        step.status = "failed"
    step.duration_s = time.time() - t0
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
    """Run issue agent in a Think sandbox (fallback: local cursor script)."""
    slug = repo_slug(repo_id or "", repo_url)
    workspace = _repo_workspace(boxci_root, slug)
    sha = issue_id
    if not think_enabled():
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
    label = f"Issue {issue_id[:7]} → think sandbox" if think_enabled() else f"Issue {issue_id[:7]} → agent"

    if think_enabled():
        def runner(extra, *, cwd, run_id=None, label=label, started_at=None):
            return _run_think_issue(
                extra, cwd=cwd, run_id=run_id, label=label, started_at=started_at
            )
        pipeline_script = Path("builtin:think-sandbox-agent")
    else:
        def runner(extra, *, cwd, run_id=None, label=label, started_at=None):
            return _run_script(
                script, extra, cwd=cwd, run_id=run_id, label=label, started_at=started_at
            )
        pipeline_script = script

    if async_run:
        run = _run_issue_async(runner, env, cwd=workspace, label=label)
        store_run(run)
    else:
        run = runner(env, cwd=workspace, label=label)

    return pipeline_script, env, run


def _run_issue_async(
    runner,
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
        pipeline="builtin:think-sandbox-agent" if think_enabled() else "builtin:run-issue-agent.sh",
        status="running",
        started_at=queued_at,
        env={k: env[k] for k in sorted(env) if k.startswith(("BOXCI_", "GIT_", "RADICLE_", "BUILDKITE_", "THINK_"))},
    )
    store_run(pending)

    def worker() -> None:
        result = runner(env, cwd=cwd, run_id=run_id, label=label, started_at=queued_at)
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
