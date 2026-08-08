"""HTTP API + dashboard for triggering pipeline runs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from flask import Flask, jsonify, request, Response

from boxci.runs import execute_pipeline, get_run, list_runs, serialize_run, store_run
from boxci.webhooks import (
    handle_garden_issue_webhook,
    handle_garden_webhook,
    handle_poll,
    trigger_from_repo,
)

app = Flask(__name__)

ROOT = Path(os.environ.get("BOXCI_ROOT", Path(__file__).resolve().parents[2]))
PIPELINES = ROOT / "pipelines"
_DASHBOARD = Path(__file__).with_name("dashboard.html")


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
    if "poll" in result:
        payload["poll"] = result["poll"]
    if result.get("builtin"):
        payload["builtin"] = True
    if accepted:
        payload["accepted"] = True
    return jsonify(payload), status


@app.get("/")
def index() -> Response:
    html = _DASHBOARD.read_text(encoding="utf-8")
    return Response(html, mimetype="text/html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "boxci"})


@app.get("/api/pipelines")
def list_pipelines():
    PIPELINES.mkdir(parents=True, exist_ok=True)
    files = sorted(p.name for p in PIPELINES.glob("*.yml"))
    return jsonify({"pipelines": files})


@app.get("/api/runs")
def list_runs_api():
    return jsonify({"runs": [serialize_run(r) for r in list_runs()]})


@app.get("/api/runs/<run_id>")
def get_run_api(run_id: str):
    run = get_run(run_id)
    if not run:
        return jsonify({"error": "not found"}), 404
    return jsonify(serialize_run(run))


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
    ) in ("merge", "issue", "poll")
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
    sha = str(body.get("sha") or body.get("commit") or body.get("GIT_SHA") or "").strip() or None
    branch = str(body.get("branch") or body.get("GIT_BRANCH") or "main").strip()
    repo_id = str(body.get("repo") or body.get("repo_id") or "").strip() or None
    issue_id = str(body.get("issue_id") or body.get("RADICLE_ISSUE_ID") or "").strip() or None
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
            async_run=trigger in ("issue", "poll", "merge"),
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 — surface git/checkout failures
        return jsonify({"error": str(exc)}), 500

    store_run(run)
    status = 202 if run.status == "running" else 201
    return jsonify({
        "pipeline": str(pipeline_path),
        "env": env,
        **serialize_run(run),
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


@app.post("/api/poll")
def poll_trigger():
    body, err = _webhook_json_body()
    if err:
        return err
    try:
        result = handle_poll(body, boxci_root=ROOT)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    return _webhook_response(result)


def main() -> None:
    host = os.environ.get("BOXCI_HOST", "0.0.0.0")
    port = int(os.environ.get("BOXCI_PORT", "8080"))
    print(f"boxci server on http://{host}:{port} (root={ROOT})")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
