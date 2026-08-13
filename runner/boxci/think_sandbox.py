"""Dispatch CI workspace jobs to think.latha.org (Cloudflare Sandbox)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def think_enabled() -> bool:
    return bool(os.environ.get("THINK_URL", "").strip() and os.environ.get("BOXCI_THINK_SECRET", "").strip())


def think_base() -> str:
    return os.environ.get("THINK_URL", "https://think.latha.org").rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Boxci-Secret": os.environ.get("BOXCI_THINK_SECRET", ""),
        "User-Agent": "boxci-think-sandbox/1.0",
    }


def _request(method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    url = f"{think_base()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {"error": (raw or "")[:400], "http_status": getattr(resp, "status", 0)}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(err)
        except json.JSONDecodeError:
            parsed = {"error": err or f"HTTP {exc.code}"}
        parsed.setdefault("error", f"HTTP {exc.code}")
        parsed["http_status"] = exc.code
        return parsed


def run_think_job(job: dict[str, Any], *, timeout_s: int = 1800, poll_s: float = 5.0) -> dict[str, Any]:
    """POST /boxci/run and poll /boxci/status until the sandbox job finishes."""
    if not think_enabled():
        raise RuntimeError("THINK_URL and BOXCI_THINK_SECRET are required")
    job = dict(job)
    job.setdefault("run_id", os.environ.get("BOXCI_RUN_ID", "boxci"))
    posted = _request("POST", "/boxci/run", job, timeout=60)
    if posted.get("http_status") and posted.get("http_status") >= 400:
        raise RuntimeError(posted.get("error") or f"think /boxci/run failed: {posted}")
    run_id = str(posted.get("run_id") or job["run_id"])
    deadline = time.time() + timeout_s
    last: dict[str, Any] = posted
    while time.time() < deadline:
        last = _request("GET", f"/boxci/status?run_id={urllib.parse.quote(run_id)}", timeout=30)
        status = str(last.get("status") or "")
        if status in ("passed", "failed") or last.get("ok") is False or last.get("merged") is not None or last.get("answer"):
            if status == "running":
                time.sleep(poll_s)
                continue
            return last
        time.sleep(poll_s)
    raise TimeoutError(f"think sandbox job {run_id} timed out after {timeout_s}s: {last}")


def format_think_output(result: dict[str, Any]) -> str:
    parts = []
    if result.get("stdout"):
        parts.append(str(result["stdout"]))
    if result.get("stderr"):
        parts.append(str(result["stderr"]))
    if result.get("answer"):
        parts.append(str(result["answer"]))
    if result.get("error"):
        parts.append(f"error: {result['error']}")
    if result.get("merged_sha"):
        parts.append(f"merged_sha={result['merged_sha']}")
    if result.get("patch_id"):
        parts.append(f"patch_id={result['patch_id']}")
    return "\n".join(parts) if parts else json.dumps(result)


def think_job_ok(result: dict[str, Any]) -> bool:
    if result.get("ok") is True or result.get("status") == "passed":
        return True
    if result.get("ok") is False or result.get("status") == "failed":
        return False
    return not result.get("error")
