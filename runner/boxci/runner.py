"""Pipeline runner — executes sleek-inspired YAML pipelines."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from boxci.artifacts import ArtifactInfo


@dataclass
class StepResult:
    key: str
    label: str
    status: str  # pending | running | passed | failed | skipped
    exit_code: int | None = None
    duration_s: float = 0.0
    output: str = ""


@dataclass
class RunResult:
    id: str
    pipeline: str
    status: str  # running | passed | failed
    started_at: float
    finished_at: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    steps: list[StepResult] = field(default_factory=list)
    artifacts: list[ArtifactInfo] = field(default_factory=list)


def _merge_env(base: dict[str, str], extra: dict[str, Any] | None) -> dict[str, str]:
    merged = dict(base)
    if extra:
        for k, v in extra.items():
            merged[str(k)] = "" if v is None else str(v)
    return merged


def _eval_if(expr: str | None, env: dict[str, str]) -> bool:
    if not expr:
        return True
    expr = expr.strip()

    # build.env("KEY") != "value"
    m = re.match(r'''build\.env\("([^"]+)"\)\s*(!=|==)\s*"([^"]*)"''', expr)
    if m:
        key, op, val = m.group(1), m.group(2), m.group(3)
        actual = env.get(key, "")
        return actual != val if op == "!=" else actual == val

    # build.env("KEY") != null  (treat missing as null)
    m = re.match(r'''build\.env\("([^"]+)"\)\s*(!=|==)\s*null''', expr)
    if m:
        key, op = m.group(1), m.group(2)
        present = key in env and env[key] not in ("", "null")
        return not present if op == "!=" else present

    # env("KEY") shorthand
    m = re.match(r'''env\("([^"]+)"\)\s*(!=|==)\s*"([^"]*)"''', expr)
    if m:
        key, op, val = m.group(1), m.group(2), m.group(3)
        actual = env.get(key, "")
        return actual != val if op == "!=" else actual == val

    # Fallback: truthy if non-empty string
    return bool(expr)


def _step_ready(step: dict[str, Any], completed: dict[str, StepResult], allow_fail: bool) -> bool:
    deps = step.get("depends_on")
    if not deps:
        return True
    if isinstance(deps, str):
        deps = [deps]
    for dep in deps:
        result = completed.get(dep)
        if result is None:
            return False
        if result.status == "failed" and not allow_fail:
            return False
        if result.status not in ("passed", "failed", "skipped"):
            return False
    return True


_MAX_LIVE_OUTPUT_CHARS = 100_000
_PROGRESS_EVERY_S = 1.0


def _run_command(
    command: str,
    env: dict[str, str],
    cwd: Path | None,
    *,
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run a step, streaming stdout+stderr so long deploys are visible live."""
    argv = ["bash", "-lc", command]
    stdbuf = shutil.which("stdbuf")
    if stdbuf:
        argv = [stdbuf, "-oL", "-eL", *argv]
    proc = subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    assert proc.stdout is not None
    parts: list[str] = []
    while True:
        data = proc.stdout.read(4096)
        if not data:
            break
        text = data.decode("utf-8", errors="replace").replace("\r", "\n")
        parts.append(text)
        if on_chunk:
            on_chunk(text)
    code = proc.wait()
    return code, "".join(parts)


def load_pipeline(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "steps" not in data:
        raise ValueError(f"Invalid pipeline (missing steps): {path}")
    return data


def run_pipeline(
    pipeline_path: Path,
    *,
    run_id: str | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
    on_progress: Callable[[RunResult], None] | None = None,
) -> RunResult:
    pipeline = load_pipeline(pipeline_path)
    base_env = os.environ.copy()
    base_env.update({str(k): str(v) for k, v in (pipeline.get("env") or {}).items()})
    base_env.update(extra_env or {})
    base_env.setdefault("BOXCI_PIPELINE", str(pipeline_path))
    base_env.setdefault("BOXCI_RUN_ID", run_id or str(uuid.uuid4())[:8])

    run = RunResult(
        id=base_env["BOXCI_RUN_ID"],
        pipeline=str(pipeline_path),
        status="running",
        started_at=time.time(),
        env={k: base_env[k] for k in sorted(base_env) if k.startswith(("BOXCI_", "CI_", "GIT_")) or k in (pipeline.get("env") or {})},
    )
    if on_progress:
        on_progress(run)

    steps: list[dict[str, Any]] = pipeline["steps"]
    completed: dict[str, StepResult] = {}
    pending = list(steps)

    while pending:
        progressed = False
        for step in list(pending):
            key = step.get("key") or step.get("label", "step")
            if not _eval_if(step.get("if"), base_env):
                sr = StepResult(key=key, label=step.get("label", key), status="skipped")
                completed[key] = sr
                run.steps.append(sr)
                pending.remove(step)
                progressed = True
                continue

            allow_fail = bool(step.get("allow_dependency_failure"))
            if not _step_ready(step, completed, allow_fail):
                continue

            label = step.get("label", key)
            sr = StepResult(key=key, label=label, status="running")
            run.steps.append(sr)
            completed[key] = sr
            if on_progress:
                on_progress(run)
            print(f"[boxci] run {run.id} step {key} running", flush=True)

            command = step.get("command")
            if not command:
                sr.status = "failed"
                sr.exit_code = 1
                sr.output = "step missing command"
                run.status = "failed"
                pending.remove(step)
                progressed = True
                continue

            step_env = _merge_env(base_env, step.get("env"))
            step_env.setdefault("PYTHONUNBUFFERED", "1")
            t0 = time.time()
            output_parts: list[str] = []
            last_progress = 0.0
            line_buf = ""

            def on_chunk(text: str, *, step=sr) -> None:
                nonlocal last_progress, line_buf
                output_parts.append(text)
                joined = "".join(output_parts)
                if len(joined) > _MAX_LIVE_OUTPUT_CHARS:
                    joined = joined[-_MAX_LIVE_OUTPUT_CHARS:]
                    output_parts[:] = [joined]
                step.output = joined
                step.duration_s = time.time() - t0
                line_buf += text
                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    print(f"[{run.id}/{step.key}] {line}", flush=True)
                now = time.time()
                if on_progress and now - last_progress >= _PROGRESS_EVERY_S:
                    last_progress = now
                    on_progress(run)

            code, output = _run_command(command, step_env, cwd, on_chunk=on_chunk)
            if line_buf:
                print(f"[{run.id}/{sr.key}] {line_buf}", flush=True)
            sr.duration_s = time.time() - t0
            sr.exit_code = code
            clipped = (output or sr.output or "")
            if len(clipped) > _MAX_LIVE_OUTPUT_CHARS:
                clipped = clipped[-_MAX_LIVE_OUTPUT_CHARS:]
            sr.output = clipped
            sr.status = "passed" if code == 0 else "failed"
            print(
                f"[boxci] run {run.id} step {key} {sr.status} exit={code} in {sr.duration_s:.1f}s",
                flush=True,
            )
            if on_progress:
                on_progress(run)

            if code != 0:
                run.status = "failed"
                run.finished_at = time.time()
                pending.remove(step)
                progressed = True
                break

            pending.remove(step)
            progressed = True

        if not progressed:
            # Deadlock — unmet dependencies
            for step in pending:
                key = step.get("key") or step.get("label", "step")
                sr = StepResult(key=key, label=step.get("label", key), status="skipped")
                sr.output = "unmet dependency"
                completed[key] = sr
                run.steps.append(sr)
            run.status = "failed"
            break

    if run.status == "running":
        run.status = "passed"
    run.finished_at = time.time()

    from boxci.artifacts import attach_artifact_urls, collect_run_artifacts

    boxci_root = Path(base_env.get("BOXCI_ROOT", Path(__file__).resolve().parents[2]))
    try:
        collected = collect_run_artifacts(boxci_root=boxci_root, env=base_env)
        run.artifacts = collected
        summary = attach_artifact_urls(run.steps, collected)
        if summary and run.steps:
            run.steps[-1].output = (run.steps[-1].output or "") + summary
    except Exception as exc:  # noqa: BLE001 — artifact publish must not fail the run
        note = f"\n=== Artifact publish skipped: {exc} ===\n"
        if run.steps:
            run.steps[-1].output = (run.steps[-1].output or "") + note

    if on_progress:
        on_progress(run)
    return run


def print_run_summary(run: RunResult) -> None:
    print(f"Run {run.id}: {run.status} ({run.pipeline})")
    for step in run.steps:
        icon = {"passed": "✓", "failed": "✗", "skipped": "-", "running": "…"}.get(step.status, "?")
        dur = f"{step.duration_s:.1f}s" if step.duration_s else ""
        print(f"  {icon} {step.label} [{step.key}] {dur}")
        if step.status == "failed" and step.output:
            tail = step.output.strip().splitlines()[-20:]
            for line in tail:
                print(f"      {line}")


def main_cli(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("Usage: boxci run <pipeline.yml> [--env KEY=VAL ...]")
        return 0 if not argv else 0

    if argv[0] != "run":
        print(f"Unknown command: {argv[0]}", file=sys.stderr)
        return 2

    path = Path(argv[1]) if len(argv) > 1 else None
    if not path or not path.exists():
        print("Pipeline file required", file=sys.stderr)
        return 2

    extra: dict[str, str] = {}
    i = 2
    while i < len(argv):
        if argv[i] == "--env" and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("=")
            extra[k] = v
            i += 2
        else:
            i += 1

    run = run_pipeline(path, extra_env=extra, cwd=path.parent.parent if path.parent.name in ("pipelines", ".boxci") else Path.cwd())
    print_run_summary(run)
    return 0 if run.status == "passed" else 1
