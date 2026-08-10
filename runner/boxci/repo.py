"""Repo checkout + .boxci pipeline discovery."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

BOXCI_FILENAMES = (
    ".boxci/pipeline.yml",
    ".boxci/pipeline.yaml",
    ".boxci.yml",
    ".boxci.yaml",
)

GARDEN_HOST = "nandi.radicle.garden"


def repo_slug(repo: str, url: str | None = None) -> str:
    """Stable directory name for a repo."""
    for candidate in (repo, url or ""):
        if not candidate:
            continue
        naked = _naked_rid(candidate)
        if naked:
            return naked
        m = re.search(r"/([^/]+?)(?:\.git)?/?$", candidate)
        if m:
            return re.sub(r"[^\w.-]+", "_", m.group(1))
    return "repo"


def _naked_rid(value: str) -> str | None:
    value = value.strip()
    if value.startswith("rad:"):
        return value[4:]
    if value.startswith("rad://"):
        return value[6:]
    if re.fullmatch(r"[A-Za-z0-9]{43,}", value):
        return value
    m = re.search(r"/([A-Za-z0-9]{43,})(?:\.git)?/?$", value)
    return m.group(1) if m else None


def resolve_repo_url(repo: str, *, garden_host: str = GARDEN_HOST) -> str | None:
    """Map Garden/Radicle repo identifiers to an HTTPS clone URL."""
    repo = (repo or "").strip()
    if not repo:
        return None
    if repo.startswith(("http://", "https://", "git@")):
        return repo if repo.endswith(".git") or repo.endswith("/") else f"{repo}.git"
    naked = _naked_rid(repo)
    if naked:
        return f"https://{garden_host}/{naked}.git"
    return None


def _normalize_branch(branch: str) -> str:
    """Garden broker refs may be ``main`` or ``<nid>/refs/heads/main``."""
    branch = branch.strip()
    if branch.startswith("refs/heads/"):
        branch = branch.removeprefix("refs/heads/")
    if "/refs/heads/" in branch:
        branch = branch.split("/refs/heads/", 1)[1]
    return branch


def _normalize_garden_event_header(header_event: str) -> str:
    """Normalize ``x-radicle-event-type`` (push, patch_created, …)."""
    return header_event.strip().lower().replace("-", "_")


def _garden_event_kind(body: dict[str, Any], *, header_event: str = "") -> str:
    """Classify Garden webhook payloads (push, patch, branch delete, broker)."""
    header = _normalize_garden_event_header(header_event)
    if header in ("patch_created", "patch_updated", "patch"):
        return "patch"
    if header in ("branch_deleted",):
        return "branch_deleted"
    if header in ("push", "branch_updated"):
        return "push"

    action = str(body.get("action") or "").strip().lower()
    if body.get("patch") is not None:
        return "patch"
    if body.get("deleted") is True or action == "deleted":
        return "branch_deleted"
    if body.get("after") is not None or body.get("commits") is not None:
        return "push"
    if body.get("commit") or body.get("Commit") or body.get("head") or body.get("sha"):
        return "push"
    return "unknown"


def _garden_commit(body: dict[str, Any]) -> str:
    commit = str(
        body.get("commit")
        or body.get("Commit")
        or body.get("head")
        or body.get("sha")
        or body.get("after")
        or ""
    ).strip()
    if commit:
        return commit

    commits = body.get("commits")
    if isinstance(commits, list) and commits:
        last = commits[-1]
        if isinstance(last, dict):
            return str(last.get("id") or last.get("oid") or "").strip()
        if isinstance(last, str):
            return last.strip()
    return ""


def _garden_repo_fields(body: dict[str, Any]) -> tuple[str, str]:
    repo = ""
    repo_url = ""
    repository = body.get("repository")
    if isinstance(repository, dict):
        repo = str(
            repository.get("id")
            or repository.get("rid")
            or repository.get("full_name")
            or repository.get("name")
            or ""
        ).strip()
        repo_url = str(
            repository.get("clone_url")
            or repository.get("http_url")
            or repository.get("url")
            or ""
        ).strip()
        if not repo_url and repo:
            repo_url = resolve_repo_url(repo) or ""

    if not repo:
        repo = str(
            body.get("repo")
            or body.get("repo_id")
            or body.get("rid")
            or ""
        ).strip()
        if isinstance(body.get("repository"), str):
            repo = str(body.get("repository")).strip()

    if not repo_url:
        repo_url = str(body.get("repo_url") or body.get("clone_url") or "").strip()
    if not repo_url and repo:
        repo_url = resolve_repo_url(repo) or ""
    return repo, repo_url


def parse_garden_payload(
    body: dict[str, Any],
    *,
    header_event: str = "",
) -> dict[str, str]:
    """Extract commit, branch, repo, and event kind from a Garden/broker webhook."""
    event_kind = _garden_event_kind(body, header_event=header_event)
    repo, repo_url = _garden_repo_fields(body)

    if event_kind == "patch":
        return {
            "commit": "",
            "branch": "",
            "repo": repo,
            "repo_url": repo_url,
            "event_kind": event_kind,
        }

    if event_kind == "branch_deleted":
        branch = _normalize_branch(
            str(body.get("branch") or body.get("ref") or "").strip()
        )
        return {
            "commit": "",
            "branch": branch,
            "repo": repo,
            "repo_url": repo_url,
            "event_kind": event_kind,
        }

    commit = _garden_commit(body)
    branch = _normalize_branch(
        str(
            body.get("branch")
            or body.get("ref")
            or body.get("refs/heads")
            or body.get("default_branch")
            or ""
        ).strip()
    )
    if not branch:
        repository = body.get("repository")
        if isinstance(repository, dict):
            branch = _normalize_branch(
                str(repository.get("default_branch") or "").strip()
            )

    return {
        "commit": commit,
        "branch": branch,
        "repo": repo,
        "repo_url": repo_url,
        "event_kind": event_kind,
    }


def find_pipeline_file(repo_root: Path) -> Path:
    for rel in BOXCI_FILENAMES:
        path = repo_root / rel
        if path.is_file():
            return path
    names = ", ".join(BOXCI_FILENAMES)
    raise FileNotFoundError(f"No .boxci pipeline in {repo_root} (tried {names})")


def load_repo_pipeline(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "steps" not in data:
        raise ValueError(f"Invalid .boxci pipeline (missing steps): {path}")
    return data


def _toml_table_string(text: str, table: str, key: str = "name") -> str:
    """Best-effort extract ``key = "…"`` from a named TOML table (no full parser)."""
    in_table = False
    exact = f"[{table}]"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_table = line == exact
            continue
        if not in_table:
            continue
        m = re.match(rf'^{re.escape(key)}\s*=\s*"([^"]+)"\s*$', line)
        if m:
            return m.group(1).strip()
        m = re.match(rf"^{re.escape(key)}\s*=\s*'([^']+)'\s*$", line)
        if m:
            return m.group(1).strip()
    return ""


def _name_from_pyproject(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    name = _toml_table_string(text, "project", "name")
    if name:
        return name
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_table = line == "[tool.poetry]"
            continue
        if not in_table:
            continue
        m = re.match(r'^name\s*=\s*"([^"]+)"\s*$', line)
        if m:
            return m.group(1).strip()
    return ""


def _name_from_flake(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Prefer buildPythonApplication / mkDerivation pname.
    m = re.search(r'\bpname\s*=\s*"([^"]+)"', text)
    if m:
        return m.group(1).strip()
    m = re.search(r'description\s*=\s*"([^"—\-]+)', text)
    if m:
        # "boxci — minimal…" → boxci
        return m.group(1).strip().split()[0].strip()
    return ""


def _name_from_readme(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^#\s+(.+)$", line)
        if m:
            title = m.group(1).strip()
            # First token if title is "boxci — …" or similar
            token = re.split(r"[\s—–|:]+", title, maxsplit=1)[0].strip()
            if token and not _naked_rid(token) and len(token) < 64:
                return token
        break
    return ""


def _name_from_manifests(workspace: Path) -> str:
    """Derive a display name from common project manifests in the checkout."""
    cargo = workspace / "Cargo.toml"
    if cargo.is_file():
        try:
            text = cargo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        name = _toml_table_string(text, "package", "name")
        if name:
            return name

    gleam = workspace / "gleam.toml"
    if gleam.is_file():
        try:
            text = gleam.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for raw in text.splitlines():
            m = re.match(r'^name\s*=\s*"([^"]+)"\s*$', raw.strip())
            if m:
                return m.group(1).strip()

    for rel in (
        "pyproject.toml",
        "runner/pyproject.toml",
        "python/pyproject.toml",
        "backend/pyproject.toml",
    ):
        path = workspace / rel
        if path.is_file():
            name = _name_from_pyproject(path)
            if name:
                return name

    package_json = workspace / "package.json"
    if package_json.is_file():
        try:
            import json

            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            name = data.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip().rsplit("/", 1)[-1]

    flake = workspace / "flake.nix"
    if flake.is_file():
        name = _name_from_flake(flake)
        if name:
            return name

    for readme_name in ("README.md", "README.MD", "Readme.md"):
        readme = workspace / readme_name
        if readme.is_file():
            name = _name_from_readme(readme)
            if name:
                return name
            break

    return ""


def resolve_repo_name(
    workspace: Path,
    slug: str,
    *,
    pipeline_path: Path | None = None,
) -> str:
    """Human-readable repo name for dashboard display."""
    path = pipeline_path
    if path is None:
        try:
            path = find_pipeline_file(workspace)
        except FileNotFoundError:
            path = None

    if path is not None:
        try:
            pipeline = load_repo_pipeline(path)
            name = pipeline.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except (OSError, ValueError):
            pass

    if workspace.is_dir():
        derived = _name_from_manifests(workspace)
        if derived:
            return derived

    base = workspace.name
    if base and base != slug and not _naked_rid(base):
        return base

    return ""


def pipeline_supports_trigger(pipeline: dict[str, Any], trigger: str) -> bool:
    on = pipeline.get("on")
    if on is None:
        return True
    if isinstance(on, str):
        on = [on]
    return trigger in on


_ISSUE_ID_RE = re.compile(r"^[0-9a-f]{40}$")


def issue_cob_exists(issue_id: str, *, repo_url: str, repo_root: Path | None = None) -> bool:
    """True when issue_id is a xyz.radicle.issue COB on the remote or checkout."""
    if not _ISSUE_ID_RE.fullmatch(issue_id):
        return False

    if repo_root is not None:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-t", issue_id],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            show = subprocess.run(
                ["git", "-C", str(repo_root), "show", f"{issue_id}:manifest"],
                capture_output=True,
                text=True,
            )
            if show.returncode == 0 and "xyz.radicle.issue" in show.stdout:
                return True

    proc = subprocess.run(
        ["git", "ls-remote", repo_url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    needle = f"/refs/cobs/xyz.radicle.issue/{issue_id}"
    return any(needle in line for line in proc.stdout.splitlines())


def _recover_git_workspace(dest: Path) -> None:
    """Abort interrupted ops and hard-reset so a dirty shared checkout can be reused.

    Failed ``from-github`` cherry-picks (esp. merge commits) can leave
    ``CHERRY_PICK_HEAD`` and a dirty index; a plain ``git checkout main`` then
    fails and surfaces as HTTP 500 on the next request.
    """
    git = ["git", "-C", str(dest)]
    for args in (
        ["cherry-pick", "--abort"],
        ["rebase", "--abort"],
        ["merge", "--abort"],
        ["am", "--abort"],
    ):
        subprocess.run(git + args, capture_output=True, text=True)
    subprocess.run(git + ["reset", "--hard"], check=False, capture_output=True, text=True)
    subprocess.run(git + ["clean", "-fd"], check=False, capture_output=True, text=True)


def checkout_repo(
    url: str,
    dest: Path,
    *,
    branch: str = "main",
    sha: str | None = None,
) -> Path:
    """Clone or update a repo checkout and return the repo root."""
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not (dest / ".git").is_dir():
        subprocess.run(
            ["git", "clone", "--branch", branch, "--depth", "50", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        _recover_git_workspace(dest)
        subprocess.run(
            ["git", "-C", str(dest), "fetch", "origin", branch, "--depth", "50"],
            check=True,
            capture_output=True,
            text=True,
        )

    if sha:
        subprocess.run(
            ["git", "-C", str(dest), "checkout", "-f", sha],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            ["git", "-C", str(dest), "checkout", "-f", branch],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(dest), "reset", "--hard", f"origin/{branch}"],
            check=True,
            capture_output=True,
            text=True,
        )

    return dest
