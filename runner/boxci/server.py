"""HTTP API + dashboard for triggering pipeline runs."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, Response

from boxci.runner import load_pipeline, run_pipeline, RunResult, StepResult

app = Flask(__name__)

ROOT = Path(os.environ.get("BOXCI_ROOT", Path(__file__).resolve().parents[2]))
PIPELINES = ROOT / "pipelines"
RUNS: dict[str, RunResult] = {}
_LOCK = threading.Lock()


def _serialize_run(run: RunResult) -> dict:
    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_s": (run.finished_at or time.time()) - run.started_at,
        "env": run.env,
        "steps": [
            {
                "key": s.key,
                "label": s.label,
                "status": s.status,
                "exit_code": s.exit_code,
                "duration_s": s.duration_s,
                "output_tail": "\n".join((s.output or "").strip().splitlines()[-30:]),
            }
            for s in run.steps
        ],
    }


def _execute_async(pipeline: Path, extra_env: dict[str, str]) -> RunResult:
    run = run_pipeline(pipeline, extra_env=extra_env, cwd=ROOT)
    with _LOCK:
        RUNS[run.id] = run
    return run


@app.get("/")
def index() -> Response:
    html = """<!DOCTYPE html>
<html><head><meta charset=utf-8><title>boxci</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}
code{background:#f4f4f4;padding:.2em .4em;border-radius:4px}
pre{background:#111;color:#eee;padding:1rem;overflow:auto;border-radius:8px}
.ok{color:#0a0}.fail{color:#c00}
</style></head><body>
<h1>boxci</h1>
<p>Minimal Nix-flake CI engine — sleek-inspired pipeline YAML.</p>
<h2>API</h2>
<ul>
<li><code>GET /api/pipelines</code> — list pipelines</li>
<li><code>POST /api/runs</code> — <code>{"pipeline":"example.yml","env":{}}</code></li>
<li><code>GET /api/runs</code> — list runs</li>
<li><code>GET /api/runs/&lt;id&gt;</code> — run detail</li>
</ul>
<h2>CLI</h2>
<pre>boxci run pipelines/example.yml
curl -X POST https://ci.boxd.sh/api/runs -H 'Content-Type: application/json' \\
  -d '{"pipeline":"example.yml"}'</pre>
</body></html>"""
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
def list_runs():
    with _LOCK:
        runs = sorted(RUNS.values(), key=lambda r: r.started_at, reverse=True)
    return jsonify({"runs": [_serialize_run(r) for r in runs[:50]]})


@app.get("/api/runs/<run_id>")
def get_run(run_id: str):
    with _LOCK:
        run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize_run(run))


@app.post("/api/runs")
def create_run():
    body = request.get_json(silent=True) or {}
    name = body.get("pipeline", "example.yml")
    path = PIPELINES / name
    if not path.exists():
        return jsonify({"error": f"pipeline not found: {name}"}), 404

    extra_env = {str(k): str(v) for k, v in (body.get("env") or {}).items()}

    # Run synchronously for simplicity (boxd VM has enough resources)
    run = _execute_async(path, extra_env)
    return jsonify(_serialize_run(run)), 201


def main() -> None:
    host = os.environ.get("BOXCI_HOST", "0.0.0.0")
    port = int(os.environ.get("BOXCI_PORT", "8080"))
    print(f"boxci server on http://{host}:{port} (root={ROOT})")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
