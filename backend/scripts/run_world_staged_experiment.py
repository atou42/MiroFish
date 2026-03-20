#!/usr/bin/env python3
"""
Run a two-stage world experiment with diagnostics and reports.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)
sys.path.insert(0, _project_root)

from app.config import Config
from scripts.generate_world_report import (
    report_dir_for_report_id,
    reset_report_dir,
    validate_report_artifacts,
)
from scripts.world_run_diagnostics import summarize as summarize_world_run, validate_summary


DEFAULT_REPORT_TIMEOUT_SECONDS = 240.0


def _run_command(
    cmd: List[str],
    cwd: str,
    *,
    check: bool = True,
    capture_output: bool = False,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    started_at = datetime.now().isoformat()
    started_monotonic = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            check=False,
            capture_output=capture_output,
            text=capture_output,
            timeout=timeout_seconds,
        )
        result = {
            "command": cmd,
            "cwd": cwd,
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "") if capture_output else "",
            "stderr": (completed.stderr or "") if capture_output else "",
            "timed_out": False,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(),
            "duration_seconds": round(time.monotonic() - started_monotonic, 2),
        }
    except subprocess.TimeoutExpired as exc:
        result = {
            "command": cmd,
            "cwd": cwd,
            "returncode": None,
            "stdout": ((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr": ((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
            "timed_out": True,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(),
            "duration_seconds": round(time.monotonic() - started_monotonic, 2),
        }
    if check and (result["timed_out"] or result["returncode"] != 0):
        raise RuntimeError(
            f"command failed: timed_out={result['timed_out']} returncode={result['returncode']} cmd={cmd}"
        )
    return result


def validate_diagnostics_or_raise(summary: Dict[str, Any]) -> Dict[str, Any]:
    errors = validate_summary(summary)
    summary["validation_errors"] = errors
    if errors:
        raise RuntimeError(f"diagnostics validation failed: {'; '.join(errors)}")
    return summary


def _parse_json_stdout(execution: Dict[str, Any]) -> Dict[str, Any]:
    stdout = str(execution.get("stdout") or "").strip()
    if not stdout:
        return {}
    candidates = [stdout]
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].lstrip().startswith("{"):
            candidates.append("\n".join(lines[index:]).strip())
            break
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _report_command(
    python_bin: str,
    simulation_id: str,
    label: str,
    report_id: str,
    *,
    fallback_only: bool,
) -> List[str]:
    cmd = [
        python_bin,
        "scripts/generate_world_report.py",
        "--simulation-id",
        simulation_id,
        "--label",
        label,
        "--report-id",
        report_id,
    ]
    if fallback_only:
        cmd.append("--fallback-only")
    return cmd


def generate_world_report_with_fallback(
    *,
    python_bin: str,
    backend_dir: str,
    simulation_id: str,
    label: str,
    report_id: str = "",
    timeout_seconds: float = DEFAULT_REPORT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    stable_report_id = report_id or f"report_world_{simulation_id}_{label}"
    attempts: List[Dict[str, Any]] = []

    def attempt(*, fallback_only: bool) -> Dict[str, Any]:
        reset_report_dir(stable_report_id)
        execution = _run_command(
            _report_command(
                python_bin,
                simulation_id=simulation_id,
                label=label,
                report_id=stable_report_id,
                fallback_only=fallback_only,
            ),
            backend_dir,
            check=False,
            capture_output=True,
            timeout_seconds=timeout_seconds,
        )
        payload = _parse_json_stdout(execution)
        errors = []
        if execution["timed_out"]:
            errors.append("report generation timed out")
        if execution["returncode"] not in {0, None}:
            errors.append(f"report command returncode={execution['returncode']}")
        if not payload:
            errors.append("report command did not return json")
        validation_errors = validate_report_artifacts(
            stable_report_id,
            payload.get("report_dir") if payload else report_dir_for_report_id(stable_report_id),
        )
        errors.extend(validation_errors)
        attempt_result = {
            "mode": "fallback_only" if fallback_only else "live",
            "report_id": stable_report_id,
            "execution": execution,
            "result": payload,
            "validation_errors": errors,
            "ok": not errors,
        }
        attempts.append(attempt_result)
        return attempt_result

    live_attempt = attempt(fallback_only=False)
    if live_attempt["ok"]:
        final_result = dict(live_attempt["result"])
        final_result["attempts"] = attempts
        final_result["used_fallback"] = False
        return final_result

    fallback_attempt = attempt(fallback_only=True)
    final_result = dict(fallback_attempt["result"])
    final_result["attempts"] = attempts
    final_result["used_fallback"] = True
    if not fallback_attempt["ok"]:
        raise RuntimeError(
            "report fallback failed: "
            + "; ".join(fallback_attempt["validation_errors"] or ["unknown report failure"])
        )
    return final_result


def run_staged_experiment(
    simulation_id: str,
    stage1_rounds: int,
    final_rounds: int,
    report_timeout_seconds: float = DEFAULT_REPORT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    backend_dir = _backend_dir
    config_path = os.path.join(
        Config.UPLOAD_FOLDER,
        "simulations",
        simulation_id,
        "simulation_config.json",
    )
    if not os.path.exists(config_path):
        raise ValueError(f"simulation_config.json not found: {config_path}")

    python_bin = os.path.join(backend_dir, ".venv", "bin", "python")
    if not os.path.exists(python_bin):
        python_bin = sys.executable

    manifest_dir = os.path.join(
        Config.UPLOAD_FOLDER,
        "simulations",
        simulation_id,
        "world",
        "experiment_runs",
        datetime.now().strftime("%Y%m%dT%H%M%S"),
    )
    os.makedirs(manifest_dir, exist_ok=True)

    manifest: Dict[str, Any] = {
        "simulation_id": simulation_id,
        "stage1_rounds": stage1_rounds,
        "final_rounds": final_rounds,
        "created_at": datetime.now().isoformat(),
        "stages": {},
    }

    stage1_cmd = [
        python_bin,
        "scripts/run_world_simulation.py",
        "--config",
        config_path,
        "--max-rounds",
        str(stage1_rounds),
    ]
    manifest["stages"]["stage1_run"] = _run_command(stage1_cmd, backend_dir)
    manifest["stages"]["stage1_diagnostics"] = validate_diagnostics_or_raise(
        summarize_world_run(simulation_id, "stage1")
    )
    manifest["stages"]["stage1_report"] = generate_world_report_with_fallback(
        python_bin=python_bin,
        backend_dir=backend_dir,
        simulation_id=simulation_id,
        label="stage1",
        report_id=f"report_world_{simulation_id}_stage1",
        timeout_seconds=report_timeout_seconds,
    )

    stage2_cmd = [
        python_bin,
        "scripts/run_world_simulation.py",
        "--config",
        config_path,
        "--max-rounds",
        str(final_rounds),
        "--resume-from-checkpoint",
    ]
    manifest["stages"]["final_run"] = _run_command(stage2_cmd, backend_dir)
    manifest["stages"]["final_diagnostics"] = validate_diagnostics_or_raise(
        summarize_world_run(simulation_id, "final")
    )
    manifest["stages"]["final_report"] = generate_world_report_with_fallback(
        python_bin=python_bin,
        backend_dir=backend_dir,
        simulation_id=simulation_id,
        label="final",
        report_id=f"report_world_{simulation_id}_final",
        timeout_seconds=report_timeout_seconds,
    )
    manifest["completed_at"] = datetime.now().isoformat()

    manifest_path = os.path.join(manifest_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest["manifest_path"] = manifest_path
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run staged world experiment")
    parser.add_argument("--simulation-id", required=True)
    parser.add_argument("--stage1-rounds", type=int, default=8)
    parser.add_argument("--final-rounds", type=int, default=16)
    parser.add_argument("--report-timeout-seconds", type=float, default=DEFAULT_REPORT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    manifest = run_staged_experiment(
        simulation_id=args.simulation_id,
        stage1_rounds=args.stage1_rounds,
        final_rounds=args.final_rounds,
        report_timeout_seconds=args.report_timeout_seconds,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
