#!/usr/bin/env bash
# Shared helpers for Radicle issue → cursor-agent → patch Buildkite steps (sleek).
set -euo pipefail

bk_die() { echo "radicle-issue-agent: $*" >&2; exit 1; }

bk_require_cmd() {
  command -v "$1" >/dev/null 2>&1 || bk_die "$1 not found on PATH"
}

# Cursor installer puts `agent` / `cursor-agent` under ~/.local/bin (and sometimes
# ~/.cursor/bin). Bootstrap runs as a subprocess, so every issue-agent entrypoint
# must re-export these dirs — otherwise delegate sees exit 127 after a successful
# install.
bk_export_cursor_path() {
  export PATH="${HOME}/.cursor/bin:${HOME}/.local/bin:/usr/local/bin:${PATH:-}"
}

# Resolve cursor-agent CLI name after PATH is set. Prints binary name or returns 1.
bk_cursor_agent_cmd() {
  if command -v cursor-agent >/dev/null 2>&1; then
    echo "cursor-agent"
    return 0
  fi
  if command -v agent >/dev/null 2>&1; then
    echo "agent"
    return 0
  fi
  return 1
}

bk_repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || bk_die "not inside a git repository"
}

bk_require_rad_repo() {
  local root
  root="$(bk_repo_root)"
  git -C "$root" remote get-url rad >/dev/null 2>&1 \
    || bk_die "remote 'rad' missing — clone via rad clone or rad remote add rad <rid>"
}

bk_rad_rid() {
  git remote get-url rad
}

# New issue COBs use the creation commit as the issue id (40-char hex).
bk_commit_is_new_issue() {
  local commit=$1 rid
  rid="$(bk_rad_rid)"
  rad cob list --repo "$rid" --type xyz.radicle.issue 2>/dev/null \
    | grep -qxF "$commit"
}

# True if issue_id is a xyz.radicle.issue COB (rad CLI and/or git remotes/storage).
# Garden issues often live only under refs/namespaces/<nid>/refs/cobs/... — rad issue
# show may miss them even after sync when the local node is not a delegate.
bk_issue_cob_exists() {
  local issue_id=$1 rid url root rad_home naked ref line
  if [[ ! "$issue_id" =~ ^[0-9a-f]{40}$ ]]; then
    return 1
  fi
  if command -v rad >/dev/null 2>&1 && rad issue show "$issue_id" --header >/dev/null 2>&1; then
    return 0
  fi
  root="$(bk_repo_root 2>/dev/null || true)"
  [[ -n "$root" ]] || return 1

  # Already fetched into this working tree.
  if git -C "$root" cat-file -t "$issue_id" >/dev/null 2>&1; then
    if git -C "$root" show "$issue_id:manifest" 2>/dev/null | grep -q 'xyz.radicle.issue'; then
      return 0
    fi
  fi

  # Local radicle storage (bootstrap hydrate).
  rad_home="${RAD_HOME:-${HOME}/.radicle}"
  rid="$(git -C "$root" remote get-url rad 2>/dev/null || true)"
  if [[ "$rid" =~ rad://([A-Za-z0-9]+) ]]; then
    naked="${BASH_REMATCH[1]}"
    if git -C "${rad_home}/storage/${naked}" cat-file -t "$issue_id" >/dev/null 2>&1; then
      return 0
    fi
  fi

  # Garden / HTTPS remotes advertise namespaced COB refs.
  while IFS= read -r url; do
    [[ -n "$url" ]] || continue
    while IFS= read -r line; do
      ref="${line##*$'\t'}"
      if [[ "$ref" == *"/refs/cobs/xyz.radicle.issue/${issue_id}" ]]; then
        return 0
      fi
    done < <(git ls-remote "$url" 2>/dev/null || true)
  done < <(
    for candidate in origin rad; do
      git -C "$root" remote get-url "$candidate" 2>/dev/null || true
    done
  )
  return 1
}

# Parse issue title + description. Prefer `rad issue show`; fall back to reading
# the issue COB commit (Garden HTTPS / $RAD_HOME/storage) when the CLI cannot
# see another node's namespaced COB.
bk_issue_details() {
  local issue_id=$1
  local root rad_home
  root="$(bk_repo_root 2>/dev/null || pwd)"
  rad_home="${RAD_HOME:-${HOME}/.radicle}"
  python3 - "$issue_id" "$root" "$rad_home" <<'PY'
import json
import os
import re
import subprocess
import sys

issue_id, repo_root, rad_home = sys.argv[1:4]


def run(cmd, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def parse_rad_header(text: str):
    title = ""
    body = ""
    title_match = re.search(r"Title\s+(.+?)\s+Issue", text, re.DOTALL)
    if title_match:
        title = " ".join(title_match.group(1).split())
    lines = [ln.strip() for ln in text.splitlines()]
    for ln in lines:
        if not ln or ln.startswith("╭") or ln.startswith("╰") or ln.startswith("│"):
            continue
        if re.match(r"^(Title|Issue|Author|Status)\b", ln):
            continue
        if re.match(r"^[●○]", ln):
            continue
        body = ln
        break
    if not body:
        for ln in reversed(lines):
            cleaned = ln.strip("│ ").strip()
            if cleaned and not re.match(r"^(Title|Issue|Author|Status)\b", cleaned):
                body = cleaned
                break
    return title, body


def git_show(git_dir: str | None, obj: str, path: str) -> str | None:
    cmd = ["git"]
    if git_dir:
        cmd += ["--git-dir", git_dir]
    cmd += ["show", f"{obj}:{path}"]
    proc = run(cmd, cwd=None if git_dir else repo_root)
    if proc.returncode != 0:
        return None
    return proc.stdout


def ensure_cob_object() -> tuple[str | None, str | None]:
    """Return (git_dir_or_None, issue_id) if the COB commit is readable."""
    # Working tree object database.
    if run(["git", "cat-file", "-t", issue_id], cwd=repo_root).returncode == 0:
        return None, issue_id

    # $RAD_HOME/storage/<rid>
    rid_url = run(["git", "remote", "get-url", "rad"], cwd=repo_root).stdout.strip()
    naked = ""
    m = re.match(r"rad://([A-Za-z0-9]+)", rid_url or "")
    if m:
        naked = m.group(1)
    if naked:
        storage = os.path.join(rad_home, "storage", naked)
        if os.path.isdir(storage) and run(
            ["git", "--git-dir", storage, "cat-file", "-t", issue_id]
        ).returncode == 0:
            return storage, issue_id

    # Discover namespaced COB on remotes and fetch into a disposable ref.
    remotes = []
    for name in ("origin", "rad"):
        url = run(["git", "remote", "get-url", name], cwd=repo_root).stdout.strip()
        if url:
            remotes.append(url)
    cob_ref = None
    remote_url = None
    for url in remotes:
        ls = run(["git", "ls-remote", url])
        for line in ls.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            ref = parts[1]
            if ref.endswith(f"/refs/cobs/xyz.radicle.issue/{issue_id}"):
                cob_ref = ref
                remote_url = url
                break
        if cob_ref:
            break
    if not cob_ref or not remote_url:
        return None, None

    local_ref = f"refs/radicle-issue-cache/{issue_id}"
    fetch = run(
        ["git", "fetch", "--no-tags", remote_url, f"+{cob_ref}:{local_ref}"],
        cwd=repo_root,
    )
    if fetch.returncode != 0:
        sys.stderr.write(fetch.stderr or fetch.stdout)
        return None, None
    return None, issue_id


def parse_cob_actions(git_dir: str | None, obj: str):
    title = ""
    body = ""
    # Action files are numeric blobs on the COB commit (0 = comment, 1 = edit, …).
    ls = run(
        ["git"]
        + (["--git-dir", git_dir] if git_dir else [])
        + ["ls-tree", "-r", "--name-only", obj],
        cwd=None if git_dir else repo_root,
    )
    if ls.returncode != 0:
        return title, body
    names = sorted(
        (n for n in ls.stdout.splitlines() if re.fullmatch(r"[0-9]+", n)),
        key=int,
    )
    for name in names:
        raw = git_show(git_dir, obj, name)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        typ = data.get("type")
        if typ == "edit" and data.get("title"):
            title = str(data["title"]).strip()
        elif typ == "comment" and body == "" and data.get("body") is not None:
            body = str(data["body"]).strip()
    return title, body


# 1) Preferred: rad CLI
proc = run(["rad", "issue", "show", issue_id, "--header"])
if proc.returncode == 0 and proc.stdout.strip():
    title, body = parse_rad_header(proc.stdout)
    if title or body:
        print(title)
        print(body)
        sys.exit(0)

# 2) Fallback: raw issue COB commit
git_dir, obj = ensure_cob_object()
if not obj:
    sys.stderr.write(proc.stderr or proc.stdout or "issue COB not found\n")
    sys.exit(1)

manifest = git_show(git_dir, obj, "manifest") or ""
if "xyz.radicle.issue" not in manifest:
    sys.stderr.write("object is not an xyz.radicle.issue COB\n")
    sys.exit(1)

title, body = parse_cob_actions(git_dir, obj)
if not title and not body:
    sys.stderr.write("issue COB has no title/body actions\n")
    sys.exit(1)

print(title)
print(body)
PY
}

bk_short_id() {
  local id=$1
  echo "${id:0:7}"
}

bk_annotate() {
  local style=$1
  local message=$2
  if command -v buildkite-agent >/dev/null 2>&1; then
    buildkite-agent annotate "$message" --style "$style" --context "radicle-issue-agent" || true
  fi
}
