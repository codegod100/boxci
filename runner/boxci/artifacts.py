"""Upload run artifacts to Backblaze B2 and expose download URLs."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


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


def _download_url(auth: _B2Auth, b2_key: str) -> str:
    prefix = os.environ.get("B2_PUBLIC_URL_PREFIX", "").strip().rstrip("/")
    if prefix:
        return f"{prefix}/{b2_key}"

    # Private bucket: short-lived download authorization (max 7 days).
    valid_s = min(int(os.environ.get("B2_DOWNLOAD_VALID_SECONDS", "604800")), 604800)
    dl_auth = _b2_request(
        f"{auth.api_url}/b2api/v2/b2_get_download_authorization",
        auth.authorization_token,
        {
            "bucketId": auth.bucket_id,
            "fileNamePrefix": b2_key,
            "validDurationInSeconds": valid_s,
        },
    )
    token = dl_auth["authorizationToken"]
    from urllib.parse import quote

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


def upload_run_artifacts(
    *,
    boxci_root: Path,
    env: dict[str, str],
) -> list[ArtifactInfo]:
    """Upload files from the run artifacts directory. Soft-skips if B2 is not configured."""
    if not b2_configured():
        return []

    art_dir = artifacts_dir_for_run(boxci_root, env)
    if not art_dir:
        return []

    files = sorted(p for p in art_dir.iterdir() if p.is_file())
    if not files:
        return []

    slug = env.get("BOXCI_REPO_SLUG") or art_dir.parent.name
    run_id = env.get("BOXCI_RUN_ID") or art_dir.name
    prefix = os.environ.get("B2_KEY_PREFIX", "artifacts").strip().strip("/")

    auth = _authorize()
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

    return uploaded


def attach_artifact_urls(run_steps: list, artifacts: list[ArtifactInfo]) -> str:
    """Append artifact_url= lines to step outputs; return upload summary log."""
    if not artifacts:
        return ""

    lines = ["", "=== B2 artifact upload ==="]
    for art in artifacts:
        lines.append(f"artifact={art.path}")
        lines.append(f"artifact_url={art.url}")
        lines.append(f"artifact_size={art.size}")

        for step in run_steps:
            if art.path in (step.output or "") or f"artifact={art.path}" in (step.output or ""):
                step.output = (step.output or "").rstrip() + f"\nartifact_url={art.url}\n"

    return "\n".join(lines) + "\n"
