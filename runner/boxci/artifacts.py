"""Discover run artifacts, optionally upload to Backblaze B2, and expose download URLs."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_MANIFEST_NAME = ".boxci-artifacts.json"
_auth_lock = threading.Lock()
_auth_cache: tuple[float, "_B2Auth"] | None = None
_dl_token_cache: dict[str, tuple[float, str]] = {}
_publish_lock = threading.Lock()


@dataclass
class ArtifactInfo:
    name: str
    path: str
    b2_key: str
    url: str
    size: int


@dataclass
class _B2Auth:
    api_url: str
    download_url: str
    authorization_token: str
    account_id: str
    bucket_id: str
    bucket_name: str


def b2_configured() -> bool:
    return bool(
        os.environ.get("B2_APPLICATION_KEY_ID", "").strip()
        and os.environ.get("B2_APPLICATION_KEY", "").strip()
    )


def public_base_url() -> str:
    return os.environ.get("BOXCI_PUBLIC_URL", "").strip().rstrip("/")


def local_artifact_url(slug: str, run_id: str, name: str) -> str:
    path = f"/artifacts/{quote(slug, safe='')}/{quote(run_id, safe='')}/{quote(name, safe='')}"
    base = public_base_url()
    return f"{base}{path}" if base else path


def _b2_request(url: str, token: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": token}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _authorize() -> _B2Auth:
    key_id = os.environ["B2_APPLICATION_KEY_ID"].strip()
    app_key = os.environ["B2_APPLICATION_KEY"].strip()
    bucket_name = os.environ.get("B2_BUCKET_NAME", "boxci-artifacts").strip()

    creds = base64.b64encode(f"{key_id}:{app_key}".encode()).decode()
    req = urllib.request.Request(
        "https://api.backblazeb2.com/b2api/v2/b2_authorize_account",
        headers={"Authorization": f"Basic {creds}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        auth = json.loads(resp.read())

    api_url = auth["apiUrl"]
    token = auth["authorizationToken"]
    account_id = auth["accountId"]

    buckets = _b2_request(
        f"{api_url}/b2api/v2/b2_list_buckets",
        token,
        {"accountId": account_id, "bucketName": bucket_name},
    )
    found = buckets.get("buckets") or []
    if not found:
        raise RuntimeError(f"B2 bucket not found: {bucket_name}")

    bucket = found[0]
    return _B2Auth(
        api_url=api_url,
        download_url=auth["downloadUrl"],
        authorization_token=token,
        account_id=account_id,
        bucket_id=bucket["bucketId"],
        bucket_name=bucket["bucketName"],
    )


def _cached_authorize() -> _B2Auth:
    global _auth_cache
    now = time.time()
    with _auth_lock:
        if _auth_cache and now - _auth_cache[0] < 3600:
            return _auth_cache[1]
        auth = _authorize()
        _auth_cache = (now, auth)
        return auth


def _download_auth_token(auth: _B2Auth, file_name_prefix: str) -> str:
    """Return a cached B2 download authorization token for a key prefix."""
    now = time.time()
    with _auth_lock:
        hit = _dl_token_cache.get(file_name_prefix)
        # Reuse for up to 1h (tokens themselves can live up to 7d).
        if hit and now - hit[0] < 3600:
            return hit[1]

    valid_s = min(int(os.environ.get("B2_DOWNLOAD_VALID_SECONDS", "604800")), 604800)
    dl_auth = _b2_request(
        f"{auth.api_url}/b2api/v2/b2_get_download_authorization",
        auth.authorization_token,
        {
            "bucketId": auth.bucket_id,
            "fileNamePrefix": file_name_prefix,
            "validDurationInSeconds": valid_s,
        },
    )
    token = str(dl_auth["authorizationToken"])
    with _auth_lock:
        _dl_token_cache[file_name_prefix] = (now, token)
    return token


def _download_url(auth: _B2Auth, b2_key: str) -> str:
    prefix = os.environ.get("B2_PUBLIC_URL_PREFIX", "").strip().rstrip("/")
    if prefix:
        return f"{prefix}/{b2_key}"

    # Private bucket: authorize once for the shared key prefix (default "artifacts/")
    # instead of one B2 round-trip per file on every /api/runs poll.
    key_prefix = os.environ.get("B2_KEY_PREFIX", "artifacts").strip().strip("/")
    if key_prefix and not key_prefix.endswith("/"):
        key_prefix = key_prefix + "/"
    auth_prefix = key_prefix if key_prefix and b2_key.startswith(key_prefix) else b2_key
    token = _download_auth_token(auth, auth_prefix)

    return (
        f"{auth.download_url}/file/{auth.bucket_name}/{quote(b2_key, safe='/')}"
        f"?Authorization={quote(token, safe='')}"
    )


def _upload_file(auth: _B2Auth, local_path: Path, b2_key: str) -> None:
    upload = _b2_request(
        f"{auth.api_url}/b2api/v2/b2_get_upload_url",
        auth.authorization_token,
        {"bucketId": auth.bucket_id},
    )
    upload_url = upload["uploadUrl"]
    upload_token = upload["authorizationToken"]

    data = local_path.read_bytes()
    import hashlib

    sha1 = hashlib.sha1(data).hexdigest()
    headers = {
        "Authorization": upload_token,
        "X-Bz-File-Name": b2_key,
        "Content-Type": "b2/x-auto",
        "Content-Length": str(len(data)),
        "X-Bz-Content-Sha1": sha1,
    }
    req = urllib.request.Request(upload_url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        json.loads(resp.read())


def artifacts_dir_for_run(boxci_root: Path, env: dict[str, str]) -> Path | None:
    run_id = env.get("BOXCI_RUN_ID", "").strip()
    if not run_id:
        return None

    artifacts_root = boxci_root / "artifacts"
    slug = env.get("BOXCI_REPO_SLUG", "").strip()
    if slug:
        candidate = artifacts_root / slug / run_id
        return candidate if candidate.is_dir() else None

    # Fallback: first matching run-id directory under any repo slug.
    if not artifacts_root.is_dir():
        return None
    for repo_dir in artifacts_root.iterdir():
        if not repo_dir.is_dir():
            continue
        candidate = repo_dir / run_id
        if candidate.is_dir():
            return candidate
    return None


def _run_identity(art_dir: Path, env: dict[str, str]) -> tuple[str, str]:
    slug = (env.get("BOXCI_REPO_SLUG") or "").strip() or art_dir.parent.name
    run_id = (env.get("BOXCI_RUN_ID") or "").strip() or art_dir.name
    return slug, run_id


def _artifact_files(art_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in art_dir.iterdir()
        if p.is_file() and p.name != _MANIFEST_NAME and not p.name.startswith(".")
    )


def _manifest_path(art_dir: Path) -> Path:
    return art_dir / _MANIFEST_NAME


def _save_manifest(art_dir: Path, artifacts: list[ArtifactInfo]) -> None:
    rows = [
        {"name": a.name, "size": a.size, "b2_key": a.b2_key, "path": a.path}
        for a in artifacts
        if a.b2_key
    ]
    if not rows:
        return
    _manifest_path(art_dir).write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _infos_from_manifest(art_dir: Path, env: dict[str, str]) -> list[ArtifactInfo] | None:
    path = _manifest_path(art_dir)
    if not path.is_file():
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list) or not rows:
        return None

    files = {p.name: p for p in _artifact_files(art_dir)}
    if not files:
        return None

    # Manifest must cover every current file with a b2_key.
    by_name = {str(r.get("name") or ""): r for r in rows if isinstance(r, dict)}
    if set(files) != set(by_name) or any(not (by_name[n].get("b2_key") or "").strip() for n in files):
        return None

    auth = _cached_authorize()
    slug, run_id = _run_identity(art_dir, env)
    out: list[ArtifactInfo] = []
    for name, file_path in sorted(files.items()):
        row = by_name[name]
        b2_key = str(row["b2_key"]).strip()
        size = int(row.get("size") or file_path.stat().st_size)
        out.append(
            ArtifactInfo(
                name=name,
                path=str(file_path),
                b2_key=b2_key,
                url=_download_url(auth, b2_key),
                size=size,
            )
        )
    return out


def list_local_artifacts(
    *,
    boxci_root: Path,
    env: dict[str, str],
) -> list[ArtifactInfo]:
    """Return ArtifactInfo entries for files on disk, with local download URLs."""
    art_dir = artifacts_dir_for_run(boxci_root, env)
    if not art_dir:
        return []

    files = _artifact_files(art_dir)
    if not files:
        return []

    slug, run_id = _run_identity(art_dir, env)
    return [
        ArtifactInfo(
            name=path.name,
            path=str(path),
            b2_key="",
            url=local_artifact_url(slug, run_id, path.name),
            size=path.stat().st_size,
        )
        for path in files
    ]


def upload_run_artifacts(
    *,
    boxci_root: Path,
    env: dict[str, str],
) -> list[ArtifactInfo]:
    """Upload files from the run artifacts directory to B2. Raises on B2 errors."""
    if not b2_configured():
        return []

    art_dir = artifacts_dir_for_run(boxci_root, env)
    if not art_dir:
        return []

    files = _artifact_files(art_dir)
    if not files:
        return []

    slug, run_id = _run_identity(art_dir, env)
    prefix = os.environ.get("B2_KEY_PREFIX", "artifacts").strip().strip("/")

    auth = _cached_authorize()
    uploaded: list[ArtifactInfo] = []

    for path in files:
        b2_key = f"{prefix}/{slug}/{run_id}/{path.name}"
        _upload_file(auth, path, b2_key)
        uploaded.append(
            ArtifactInfo(
                name=path.name,
                path=str(path),
                b2_key=b2_key,
                url=_download_url(auth, b2_key),
                size=path.stat().st_size,
            )
        )

    _save_manifest(art_dir, uploaded)
    return uploaded


def list_run_artifacts_fast(
    *,
    boxci_root: Path,
    env: dict[str, str],
) -> list[ArtifactInfo]:
    """Fast path for API listing: never upload; B2 from manifest or local URLs."""
    art_dir = artifacts_dir_for_run(boxci_root, env)
    if art_dir is None:
        return []

    if b2_configured():
        try:
            from_manifest = _infos_from_manifest(art_dir, env)
            if from_manifest:
                return from_manifest
        except Exception:
            pass

    return list_local_artifacts(boxci_root=boxci_root, env=env)


def collect_run_artifacts(
    *,
    boxci_root: Path,
    env: dict[str, str],
) -> list[ArtifactInfo]:
    """Prefer B2 URLs when available; fall back to local download URLs.

    May upload to B2 when files exist but no manifest yet — use only after a run,
    not on GET /api/runs.
    """
    local = list_local_artifacts(boxci_root=boxci_root, env=env)
    if not local:
        return []

    if not b2_configured():
        return local

    art_dir = artifacts_dir_for_run(boxci_root, env)
    if art_dir is None:
        return local

    with _publish_lock:
        try:
            from_manifest = _infos_from_manifest(art_dir, env)
            if from_manifest:
                return from_manifest
            uploaded = upload_run_artifacts(boxci_root=boxci_root, env=env)
        except Exception:
            return local

    return uploaded or local


def hydrate_run_artifacts(run, *, boxci_root: Path) -> None:
    """Attach artifact links for API responses without blocking on uploads."""
    existing = list(getattr(run, "artifacts", None) or [])
    if existing:
        # Keep whatever URLs we already have (B2 or local). Refreshing private
        # download tokens on every list made /api/runs take multiple seconds.
        return

    env = dict(getattr(run, "env", None) or {})
    if not env.get("BOXCI_RUN_ID"):
        env["BOXCI_RUN_ID"] = getattr(run, "id", "") or ""

    filled = list_run_artifacts_fast(boxci_root=boxci_root, env=env)
    if filled:
        run.artifacts = filled


def resolve_artifact_path(
    *,
    boxci_root: Path,
    slug: str,
    run_id: str,
    filename: str,
) -> Path | None:
    """Resolve a safe path under artifacts/<slug>/<run_id>/<filename>."""
    if not slug or not run_id or not filename:
        return None
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        return None
    if any(part in (".", "..") for part in (slug, run_id)):
        return None
    if filename == _MANIFEST_NAME or filename.startswith("."):
        return None

    root = (boxci_root / "artifacts").resolve()
    candidate = (root / slug / run_id / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def attach_artifact_urls(run_steps: list, artifacts: list[ArtifactInfo]) -> str:
    """Append artifact_url= lines to step outputs; return upload summary log."""
    if not artifacts:
        return ""

    via_b2 = any(a.b2_key for a in artifacts)
    lines = ["", "=== B2 artifact upload ===" if via_b2 else "=== Artifact links ==="]
    for art in artifacts:
        lines.append(f"artifact={art.path}")
        lines.append(f"artifact_url={art.url}")
        lines.append(f"artifact_size={art.size}")

        for step in run_steps:
            if art.path in (step.output or "") or f"artifact={art.path}" in (step.output or ""):
                step.output = (step.output or "").rstrip() + f"\nartifact_url={art.url}\n"

    return "\n".join(lines) + "\n"
