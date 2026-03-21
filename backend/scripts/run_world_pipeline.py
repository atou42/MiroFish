#!/usr/bin/env python3
"""
Run a multi-stage world pipeline with resume-aware checkpoints, diagnostics,
reading surfaces, and reports.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)
sys.path.insert(0, _project_root)

from app.config import Config
from scripts.run_world_staged_experiment import (  # noqa: E402
    DEFAULT_REPORT_TIMEOUT_SECONDS,
    _run_command,
    generate_world_report_with_fallback,
    validate_diagnostics_or_raise,
)
from scripts.world_run_diagnostics import summarize as summarize_world_run  # noqa: E402


def _python_bin() -> str:
    candidate = os.path.join(_backend_dir, ".venv", "bin", "python")
    return candidate if os.path.exists(candidate) else sys.executable


def _checkpoint_meta(simulation_id: str) -> Dict[str, Any]:
    path = os.path.join(
        Config.UPLOAD_FOLDER,
        "simulations",
        simulation_id,
        "world",
        "checkpoint.json",
    )
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _parse_stage_rounds(raw_values: List[str]) -> List[int]:
    rounds: List[int] = []
    for raw in raw_values:
        for chunk in str(raw or "").split(","):
            value = chunk.strip()
            if not value:
                continue
            rounds.append(int(value))
    if not rounds:
        raise ValueError("at least one stage round must be provided")
    normalized = []
    last_value = 0
    for value in rounds:
        if value <= 0:
            raise ValueError(f"invalid stage round: {value}")
        if value <= last_value:
            raise ValueError("stage rounds must be strictly increasing")
        normalized.append(value)
        last_value = value
    return normalized


def _stage_label(prefix: str, index: int, rounds: int) -> str:
    return f"{prefix}{index:02d}_r{rounds}"


def _write_manifest(manifest_path: str, manifest: Dict[str, Any]) -> None:
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def run_world_pipeline(
    *,
    simulation_id: str,
    stage_rounds: List[int],
    label_prefix: str = "stage",
    report_timeout_seconds: float = DEFAULT_REPORT_TIMEOUT_SECONDS,
    skip_reports: bool = False,
) -> Dict[str, Any]:
    sim_dir = os.path.join(Config.UPLOAD_FOLDER, "simulations", simulation_id)
    config_path = os.path.join(sim_dir, "simulation_config.json")
    if not os.path.exists(config_path):
        raise ValueError(f"simulation_config.json not found: {config_path}")

    pipeline_dir = os.path.join(
        sim_dir,
        "world",
        "pipeline_runs",
        datetime.now().strftime("%Y%m%dT%H%M%S"),
    )
    os.makedirs(pipeline_dir, exist_ok=True)
    manifest_path = os.path.join(pipeline_dir, "manifest.json")

    python_bin = _python_bin()
    manifest: Dict[str, Any] = {
        "simulation_id": simulation_id,
        "config_path": config_path,
        "created_at": datetime.now().isoformat(),
        "stage_rounds": stage_rounds,
        "label_prefix": label_prefix,
        "skip_reports": skip_reports,
        "stages": [],
        "manifest_path": manifest_path,
    }
    _write_manifest(manifest_path, manifest)

    for index, target_rounds in enumerate(stage_rounds, start=1):
        label = _stage_label(label_prefix, index, target_rounds)
        checkpoint_before = _checkpoint_meta(simulation_id)
        last_tick_before = int(checkpoint_before.get("last_completed_tick") or 0)
        checkpoint_status_before = str(checkpoint_before.get("status") or "").strip().lower()
        command_name = "resume" if last_tick_before > 0 else "run"
        skip_run = last_tick_before >= target_rounds and checkpoint_status_before in {"completed", "running", "restored"}

        if skip_run:
            run_result = {
                "command": [],
                "returncode": 0,
                "skipped_due_to_checkpoint": True,
                "checkpoint_before": checkpoint_before,
                "target_rounds": target_rounds,
                "command_name": command_name,
            }
        else:
            cmd = [
                python_bin,
                "scripts/world_run.py",
                command_name,
                "--simulation-id",
                simulation_id,
                "--max-rounds",
                str(target_rounds),
                "--max-resume-attempts",
                "4",
            ]
            run_result = _run_command(cmd, _backend_dir)
            run_result["checkpoint_before"] = checkpoint_before
            run_result["target_rounds"] = target_rounds
            run_result["command_name"] = command_name

        diagnostics = validate_diagnostics_or_raise(summarize_world_run(simulation_id, label))
        stage_result: Dict[str, Any] = {
            "stage_index": index,
            "label": label,
            "target_rounds": target_rounds,
            "run": run_result,
            "checkpoint_after": _checkpoint_meta(simulation_id),
            "diagnostics": diagnostics,
        }
        if not skip_reports:
            stage_result["report"] = generate_world_report_with_fallback(
                python_bin=python_bin,
                backend_dir=_backend_dir,
                simulation_id=simulation_id,
                label=label,
                report_id=f"report_world_{simulation_id}_{label}",
                timeout_seconds=report_timeout_seconds,
            )
        manifest["stages"].append(stage_result)
        _write_manifest(manifest_path, manifest)

    manifest["completed_at"] = datetime.now().isoformat()
    _write_manifest(manifest_path, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a staged world pipeline")
    parser.add_argument("--simulation-id", required=True)
    parser.add_argument(
        "--stage-rounds",
        action="append",
        required=True,
        help="Comma-separated stage targets, for example 8,16,32 or repeat the flag.",
    )
    parser.add_argument("--label-prefix", default="stage")
    parser.add_argument("--report-timeout-seconds", type=float, default=DEFAULT_REPORT_TIMEOUT_SECONDS)
    parser.add_argument("--skip-reports", action="store_true")
    args = parser.parse_args()

    manifest = run_world_pipeline(
        simulation_id=args.simulation_id,
        stage_rounds=_parse_stage_rounds(args.stage_rounds),
        label_prefix=args.label_prefix,
        report_timeout_seconds=args.report_timeout_seconds,
        skip_reports=args.skip_reports,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
