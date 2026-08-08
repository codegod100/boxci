"""Webhook handlers for external CI triggers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from boxci.repo import (
    checkout_repo,
    find_pipeline_file,
    issue_cob_exists,
    load_repo_pipeline,
    parse_garden_payload,
    pipeline_supports_trigger,
    repo_slug,
    resolve_repo_url,
)
from boxci.runs import execute_pipeline
from boxci.runner import RunResult


def _default_branch() -> str:
    return os.environ.get("BOXCI_DEFAULT_BRANCH", "main")


def _default_repo_url() -> str:
    return os.environ.get("BOXCI_DEFAULT_REPO_URL", "").strip()


def _default_repo_id() -> str:
    return os.environ.get("BOXCI_DEFAULT_REPO_ID", "").strip()


def _workspaces_root(boxci_root: Path) -> Path:
    return boxci_root / "workspaces"


def _repo_workspace(boxci_root: Path, slug: str) -> Path:
    override = os.environ.get(f"BOXCI_WORKSPACE_{slug.upper()}") or os.environ.get(
        f"BOXCI_WORKSPACE_{slug}"
    )
    if override:
        return Path(override)
    return _workspaces_root(boxci_root) / slug


def _build_extra_env(
    *,
    trigger: str,
    workspace: Path,
    repo_url: str,
    slug: str,
    branch: str,
    sha: str | None,
    issue_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """Env for repo pipeline runs — mirrors Buildkite RADICLE_* for sleek scripts."""
    tip = sha or ""
    extra_env: dict[str, str] = {
        "BOXCI_TRIGGER": trigger,
        "BOXCI_REPO_ROOT": str(workspace),
        "BOXCI_REPO_URL": repo_url,
        "BOXCI_REPO_SLUG": slug,
        "GIT_SHA": tip,
        "GIT_BRANCH": branch,
        "GIT_TERMINAL_PROMPT": "0",
        # sleek scripts/buildkite/* compat
        "BUILDKITE_COMMIT": tip,
        "BUILDKITE_BRANCH": branch,
    }

    if trigger in ("issue", "poll"):
        extra_env["RADICLE_TRIGGER"] = trigger
    else:
        extra_env.setdefault("RADICLE_TRIGGER", "")

    if issue_id:
        extra_env["RADICLE_ISSUE_ID"] = issue_id

    if dry_run:
        extra_env["RADICLE_AGENT_DRY_RUN"] = "1"

    return extra_env


def trigger_from_repo(
    *,
    boxci_root: Path,
    repo_url: str,
    trigger: str,
    sha: str | None = None,
    branch: str | None = None,
    repo_id: str | None = None,
    issue_id: str | None = None,
    dry_run: bool = False,
    async_run: bool = False,
) -> tuple[Path, dict[str, str], RunResult]:
    """Checkout repo, locate .boxci pipeline, and run it."""
    branch = branch or _default_branch()
    slug = repo_slug(repo_id or "", repo_url)
    workspace = _repo_workspace(boxci_root, slug)

    checkout_repo(repo_url, workspace, branch=branch, sha=sha)

    pipeline_path = find_pipeline_file(workspace)
    pipeline = load_repo_pipeline(pipeline_path)
    if not pipeline_supports_trigger(pipeline, trigger):
        raise ValueError(
            f"Pipeline {pipeline_path} does not define trigger {trigger!r} in on:"
        )

    extra_env = _build_extra_env(
        trigger=trigger,
        workspace=workspace,
        repo_url=repo_url,
        slug=slug,
        branch=branch,
        sha=sha or _git_head(workspace),
        issue_id=issue_id,
        dry_run=dry_run,
    )

    run = execute_pipeline(
        pipeline_path,
        extra_env,
        cwd=workspace,
        async_run=async_run,
    )
    return pipeline_path, extra_env, run


def _git_head(workspace: Path) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _resolve_repo(body: dict[str, Any]) -> tuple[str, str, str]:
    parsed = parse_garden_payload(body)
    repo_id = parsed["repo"] or _default_repo_id()
    repo_url = parsed["repo_url"] or _default_repo_url()
    if not repo_url and repo_id:
        repo_url = resolve_repo_url(repo_id) or ""
    branch = parsed["branch"] or _default_branch()
    return repo_id, repo_url, branch


def handle_garden_webhook(body: dict[str, Any], *, boxci_root: Path) -> dict[str, Any]:
    """Parse a Garden merge webhook and run the repo's .boxci pipeline."""
    parsed = parse_garden_payload(body)
    commit = parsed["commit"]
    branch = parsed["branch"] or _default_branch()
    repo_id = parsed["repo"] or _default_repo_id()
    repo_url = parsed["repo_url"] or _default_repo_url()

    if not commit:
        return {"ignored": True, "reason": "no commit in payload"}

    if branch and branch != _default_branch():
        return {
            "ignored": True,
            "reason": f"branch {branch!r} is not {_default_branch()!r}",
        }

    if not repo_url and repo_id:
        repo_url = resolve_repo_url(repo_id) or ""
    if not repo_url:
        return {"ignored": True, "reason": "could not resolve repo clone URL"}

    # Issue COB roots have no .boxci at that commit — route to issue handler.
    if issue_cob_exists(commit, repo_url=repo_url):
        return handle_garden_issue_webhook(body, boxci_root=boxci_root)

    pipeline_path, env, run = trigger_from_repo(
        boxci_root=boxci_root,
        repo_url=repo_url,
        trigger="merge",
        sha=commit,
        branch=branch,
        repo_id=repo_id,
        async_run=_should_run_async("merge"),
    )
    return {
        "ignored": False,
        "pipeline": str(pipeline_path),
        "env": env,
        "run": run,
    }


def handle_garden_issue_webhook(body: dict[str, Any], *, boxci_root: Path) -> dict[str, Any]:
    """Garden issue open → checkout main + run issue agent (.boxci on: issue)."""
    parsed = parse_garden_payload(body)
    branch = parsed["branch"] or _default_branch()
    repo_id = parsed["repo"] or _default_repo_id()
    repo_url = parsed["repo_url"] or _default_repo_url()
    commit = parsed["commit"]
    issue_id = str(body.get("issue_id") or body.get("issue") or commit or "").strip()

    if not issue_id:
        return {"ignored": True, "reason": "no issue id / commit in payload"}

    if not repo_url and repo_id:
        repo_url = resolve_repo_url(repo_id) or ""
    if not repo_url:
        return {"ignored": True, "reason": "could not resolve repo clone URL"}

    if not issue_cob_exists(issue_id, repo_url=repo_url):
        return {
            "ignored": True,
            "reason": f"commit {issue_id[:7]} is not a xyz.radicle.issue COB",
        }

    dry_run = _dry_run_requested(body)
    pipeline_path, env, run = trigger_from_repo(
        boxci_root=boxci_root,
        repo_url=repo_url,
        trigger="issue",
        sha=None,
        branch=branch,
        repo_id=repo_id,
        issue_id=issue_id,
        dry_run=dry_run,
        async_run=_should_run_async("issue"),
    )
    return {
        "ignored": False,
        "pipeline": str(pipeline_path),
        "env": env,
        "run": run,
        "issue_id": issue_id,
    }


def handle_poll(body: dict[str, Any], *, boxci_root: Path) -> dict[str, Any]:
    """Scheduled / manual poll → run .boxci on: poll (lists issues, dispatches agents)."""
    repo_id, repo_url, branch = _resolve_repo(body)
    if not repo_url:
        return {"ignored": True, "reason": "repo_url required (or set BOXCI_DEFAULT_REPO_URL)"}

    dry_run = _dry_run_requested(body)
    pipeline_path, env, run = trigger_from_repo(
        boxci_root=boxci_root,
        repo_url=repo_url,
        trigger="poll",
        sha=None,
        branch=branch,
        repo_id=repo_id or None,
        dry_run=dry_run,
        async_run=_should_run_async("poll"),
    )
    return {
        "ignored": False,
        "pipeline": str(pipeline_path),
        "env": env,
        "run": run,
    }


def _dry_run_requested(body: dict[str, Any]) -> bool:
    if os.environ.get("RADICLE_AGENT_DRY_RUN", "").strip() in ("1", "true", "yes"):
        return True
    val = body.get("dry_run")
    return val in (True, 1, "1", "true", "yes")


def _should_run_async(trigger: str) -> bool:
    if os.environ.get("BOXCI_SYNC_RUNS", "").strip() in ("1", "true", "yes"):
        return False
    return trigger in ("issue", "poll", "merge")
