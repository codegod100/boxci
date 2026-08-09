"""HTTP API + dashboard for triggering pipeline runs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from flask import Flask, jsonify, request, Response, send_file, send_from_directory

from boxci.artifacts import resolve_artifact_path
from boxci.github_patch import run_github_commit_patch
from boxci.runs import (
    execute_pipeline,
    find_run,
    list_artifact_disk_runs,
    list_known_repos,
    list_runs,
    run_repo_slug,
    serialize_run,
    store_run,
)
from boxci.repo import resolve_repo_url
from boxci.webhooks import (
    handle_garden_issue_webhook,
    handle_garden_webhook,
    trigger_from_repo,
)

app = Flask(__name__)

ROOT = Path(os.environ.get("BOXCI_ROOT", Path(__file__).resolve().parents[2]))
PIPELINES = ROOT / "pipelines"
_DASHBOARD = Path(__file__).with_name("dashboard.html")
_STATIC = Path(__file__).with_name("static")


def _check_webhook_secret(raw_body: bytes) -> tuple[bool, tuple[Response, int] | None]:
    secret = os.environ.get("BOXCI_WEBHOOK_SECRET", "").strip()
    if not secret:
        return True, None

    # Garden native delivery (rad webhooks / dashboard): HMAC-SHA256 of raw body.
    for header in ("X-Hub-Signature-256", "X-Radicle-Signature"):
        sig = request.headers.get(header, "").strip()
        if sig.startswith("sha256="):
            expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
            provided = sig.removeprefix("sha256=")
            if hmac.compare_digest(expected, provided):
                return True, None

    # Manual curl / adapter scripts that re-post JSON without re-signing.
    provided = request.headers.get("X-Boxci-Secret", "").strip()
    if provided == secret:
        return True, None

    return False, (jsonify({"error": "invalid webhook secret"}), 401)


def _webhook_json_body() -> tuple[dict, tuple[Response, int] | None]:
    raw_body = request.get_data()
    ok, err = _check_webhook_secret(raw_body)
    if not ok:
        return {}, err  # type: ignore[return-value]
    if not raw_body:
        return {}, None
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return {}, (jsonify({"error": "invalid JSON body"}), 400)
    if not isinstance(body, dict):
        return {}, (jsonify({"error": "JSON body must be an object"}), 400)
    return body, None


def _webhook_response(result: dict, *, accepted: bool = False) -> tuple[Response, int]:
    if result.get("ignored"):
        return jsonify(result), 200

    run = result["run"]
    store_run(run)
    status = 202 if run.status == "running" else 201
    payload = {
        "ignored": False,
        "pipeline": result["pipeline"],
        "env": result["env"],
        **serialize_run(run),
    }
    if "issue_id" in result:
        payload["issue_id"] = result["issue_id"]
    if result.get("builtin"):
        payload["builtin"] = True
    if accepted:
        payload["accepted"] = True
    return jsonify(payload), status


def _merged_runs(*, repo: str | None = None, limit: int = 50) -> list:
    memory = list_runs(repo=repo)
    disk = list_artifact_disk_runs(
        boxci_root=ROOT,
        repo=repo,
        exclude_ids={r.id for r in memory},
    )
    return sorted(
        [*memory, *disk],
        key=lambda r: r.finished_at or r.started_at,
        reverse=True,
    )[:limit]


def _dashboard_bootstrap(repo_key: str | None = None, run_id: str | None = None) -> dict:
    focus = (run_id or "").strip() or None
    repo = (repo_key or "").strip() or None

    focus_run = find_run(focus, boxci_root=ROOT) if focus else None
    if focus_run is not None and not repo:
        repo = run_repo_slug(focus_run.env) or None

    runs = _merged_runs(repo=repo)
    if focus_run is not None and all(r.id != focus_run.id for r in runs):
        runs = [focus_run, *runs][:50]

    return {
        "ok": True,
        "service": "boxci",
        "repo": repo,
        "focus_run": focus_run.id if focus_run is not None else focus,
        "repos": list_known_repos(boxci_root=ROOT),
        "runs": [serialize_run(r, boxci_root=ROOT) for r in runs],
    }


def _dashboard(repo_key: str | None = None, run_id: str | None = None) -> Response:
    html = _DASHBOARD.read_text(encoding="utf-8")
    try:
        boot = _dashboard_bootstrap(repo_key, run_id)
    except Exception as exc:  # noqa: BLE001 — still serve shell if data fails
        boot = {
            "ok": False,
            "error": str(exc),
            "repo": repo_key,
            "focus_run": run_id,
            "repos": [],
            "runs": [],
        }
    # Prevent </script> breakout when embedding JSON in HTML.
    payload = json.dumps(boot, separators=(",", ":")).replace("<", "\\u003c")
    if "__BOXCI_BOOTSTRAP__" not in html:
        html = html.replace(
            "</head>",
            f'<script type="application/json" id="boxci-bootstrap">{payload}</script>\n</head>',
            1,
        )
    else:
        html = html.replace("__BOXCI_BOOTSTRAP__", payload, 1)
    return Response(html, mimetype="text/html")


@app.get("/")
def index() -> Response:
    return _dashboard()


@app.get("/favicon.ico")
def favicon_ico():
    path = _STATIC / "favicon.ico"
    if not path.is_file():
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="image/x-icon")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    path = _STATIC / "apple-touch-icon.png"
    if not path.is_file():
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="image/png")


@app.get("/static/<path:filename>")
def static_file(filename: str):
    return send_from_directory(_STATIC, filename)


@app.get("/runs/<run_id>")
def run_page(run_id: str) -> Response:
    return _dashboard(run_id=run_id)


@app.get("/repos/<path:repo_key>/runs/<run_id>")
def repo_run_page(repo_key: str, run_id: str) -> Response:
    return _dashboard(repo_key, run_id)


@app.get("/repos/<path:repo_key>")
def repo_page(repo_key: str) -> Response:
    return _dashboard(repo_key)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "boxci"})


@app.get("/api/pipelines")
def list_pipelines():
    PIPELINES.mkdir(parents=True, exist_ok=True)
    files = sorted(p.name for p in PIPELINES.glob("*.yml"))
    return jsonify({"pipelines": files})


@app.get("/api/repos")
def list_repos_api():
    return jsonify({"repos": list_known_repos(boxci_root=ROOT)})


@app.get("/api/runs")
def list_runs_api():
    repo = str(request.args.get("repo") or "").strip() or None
    merged = _merged_runs(repo=repo)
    return jsonify({"runs": [serialize_run(r, boxci_root=ROOT) for r in merged]})


@app.get("/api/runs/<run_id>")
def get_run_api(run_id: str):
    run = find_run(run_id, boxci_root=ROOT)
    if not run:
        return jsonify({"error": "not found"}), 404
    return jsonify(serialize_run(run, boxci_root=ROOT))


@app.get("/artifacts/<slug>/<run_id>/<path:filename>")
def download_artifact(slug: str, run_id: str, filename: str):
    path = resolve_artifact_path(
        boxci_root=ROOT,
        slug=slug,
        run_id=run_id,
        filename=filename,
    )
    if path is None:
        return jsonify({"error": "not found"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.post("/api/runs")
def create_run():
    body = request.get_json(silent=True) or {}
    name = body.get("pipeline", "example.yml")
    path = PIPELINES / name
    if not path.exists():
        return jsonify({"error": f"pipeline not found: {name}"}), 404

    extra_env = {str(k): str(v) for k, v in (body.get("env") or {}).items()}
    async_run = body.get("async") in (True, 1, "1", "true", "yes") or extra_env.get(
        "BOXCI_TRIGGER"
    ) in ("merge", "issue")
    run = execute_pipeline(path, extra_env, cwd=ROOT, async_run=async_run)
    status = 202 if run.status == "running" else 201
    return jsonify(serialize_run(run)), status


@app.post("/api/runs/from-repo")
def create_run_from_repo():
    body = request.get_json(silent=True) or {}
    repo_url = str(body.get("repo_url") or body.get("url") or "").strip()
    if not repo_url:
        return jsonify({"error": "repo_url required"}), 400

    trigger = str(body.get("trigger") or body.get("on") or "merge").strip()
    sha = str(body.get("sha") or body.get("commit") or body.get("GIT_SHA") or body.get("github_commit") or "").strip() or None
    branch = str(body.get("branch") or body.get("GIT_BRANCH") or "main").strip()
    repo_id = str(body.get("repo") or body.get("repo_id") or "").strip() or None
    issue_id = str(body.get("issue_id") or body.get("RADICLE_ISSUE_ID") or "").strip() or None
    github_repo_url = str(
        body.get("github_repo_url") or body.get("github_url") or body.get("github") or ""
    ).strip() or None
    patch_title = str(body.get("title") or body.get("patch_title") or "").strip() or None
    patch_description = str(
        body.get("description") or body.get("patch_description") or body.get("body") or ""
    ).strip() or None
    dry_run = body.get("dry_run") in (True, 1, "1", "true", "yes")

    try:
        pipeline_path, env, run = trigger_from_repo(
            boxci_root=ROOT,
            repo_url=repo_url,
            trigger=trigger,
            sha=sha,
            branch=branch,
            repo_id=repo_id,
            issue_id=issue_id,
            dry_run=dry_run,
            async_run=trigger in ("issue", "merge", "github-commit", "github_commit", "github"),
            github_repo_url=github_repo_url,
            patch_title=patch_title,
            patch_description=patch_description,
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 — surface git/checkout failures
        return jsonify({"error": str(exc)}), 500

    store_run(run)
    status = 202 if run.status == "running" else 201
    payload = {
        "pipeline": str(pipeline_path),
        "env": env,
        **serialize_run(run),
    }
    if trigger in ("github-commit", "github_commit", "github", "issue"):
        payload["builtin"] = True
    return jsonify(payload), status


@app.post("/api/patches/from-github")
def create_patch_from_github():
    """Cherry-pick a GitHub commit onto a Radicle repo and open a patch.

    Preferred endpoint for Cursor cloud agents. Same builtin as
    ``POST /api/runs/from-repo`` with ``trigger: "github-commit"``.
    """
    body = request.get_json(silent=True) or {}

    # Optional shared secret (same as Garden webhooks) when BOXCI_WEBHOOK_SECRET is set.
    secret = os.environ.get("BOXCI_WEBHOOK_SECRET", "").strip()
    if secret:
        provided = request.headers.get("X-Boxci-Secret", "").strip()
        if provided != secret:
            return jsonify({"error": "invalid webhook secret"}), 401

    repo_id = str(body.get("repo") or body.get("repo_id") or body.get("rid") or "").strip() or None
    repo_url = str(body.get("repo_url") or body.get("url") or "").strip() or None
    if not repo_url and repo_id:
        repo_url = resolve_repo_url(repo_id)
    if not repo_url and not repo_id:
        return jsonify({"error": "repo_url or repo (rad:…) required"}), 400

    github_commit = str(
        body.get("github_commit")
        or body.get("sha")
        or body.get("commit")
        or body.get("GIT_SHA")
        or ""
    ).strip()
    github_repo_url = str(
        body.get("github_repo_url") or body.get("github_url") or body.get("github") or ""
    ).strip()
    if not github_commit:
        return jsonify({"error": "github_commit required"}), 400
    if not github_repo_url:
        return jsonify({"error": "github_repo_url required"}), 400

    branch = str(body.get("branch") or body.get("GIT_BRANCH") or "main").strip()
    title = str(body.get("title") or body.get("patch_title") or "").strip() or None
    description = str(
        body.get("description") or body.get("patch_description") or body.get("body") or ""
    ).strip() or None
    dry_run = body.get("dry_run") in (True, 1, "1", "true", "yes")
    sync = body.get("async") in (False, 0, "0", "false", "no") or body.get("sync") in (
        True,
        1,
        "1",
        "true",
        "yes",
    )

    try:
        script, env, run = run_github_commit_patch(
            boxci_root=ROOT,
            repo_url=repo_url,
            repo_id=repo_id,
            github_commit=github_commit,
            github_repo_url=github_repo_url,
            branch=branch,
            title=title,
            description=description,
            dry_run=dry_run,
            async_run=not sync,
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    store_run(run)
    status = 202 if run.status == "running" else 201
    return jsonify({
        "pipeline": str(script),
        "env": {k: v for k, v in env.items() if k != "GITHUB_TOKEN"},
        "builtin": True,
        "trigger": "github-commit",
        **serialize_run(run, boxci_root=ROOT),
    }), status


@app.post("/api/webhooks/garden")
def garden_webhook():
    body, err = _webhook_json_body()
    if err:
        return err

    header_event = request.headers.get("X-Radicle-Event-Type", "")
    try:
        result = handle_garden_webhook(
            body,
            boxci_root=ROOT,
            header_event=header_event,
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    return _webhook_response(result)


@app.post("/api/webhooks/garden/issue")
def garden_issue_webhook():
    body, err = _webhook_json_body()
    if err:
        return err

    header_event = request.headers.get("X-Radicle-Event-Type", "")
    try:
        result = handle_garden_issue_webhook(
            body,
            boxci_root=ROOT,
            header_event=header_event,
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    return _webhook_response(result, accepted=True)


def main() -> None:
    host = os.environ.get("BOXCI_HOST", "0.0.0.0")
    port = int(os.environ.get("BOXCI_PORT", "8080"))
    print(f"boxci server on http://{host}:{port} (root={ROOT})")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
