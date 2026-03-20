#!/usr/bin/env python3
"""
World-mode operator CLI: run, resume, restore, finalize, staged, status.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, Optional

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)
sys.path.insert(0, _project_root)

from app.config import Config
from app.utils.world_run_lock import inspect_world_run_lease, world_run_paths_for_config
from scripts.run_world_simulation import restore_world_checkpoint_from_logs
from scripts.run_world_staged_experiment import (
    DEFAULT_REPORT_TIMEOUT_SECONDS,
    generate_world_report_with_fallback,
    run_staged_experiment,
    validate_diagnostics_or_raise,
)
from scripts.world_run_diagnostics import summarize as summarize_world_run


def _resolve_config_path(config_path: str = "", simulation_id: str = "") -> str:
    if config_path:
        resolved = os.path.abspath(config_path)
    elif simulation_id:
        resolved = os.path.join(
            Config.UPLOAD_FOLDER,
            "simulations",
            simulation_id,
            "simulation_config.json",
        )
    else:
        raise ValueError("either --config or --simulation-id is required")
    if not os.path.exists(resolved):
        raise ValueError(f"simulation config not found: {resolved}")
    return resolved


def _simulation_id_from_config(config_path: str) -> str:
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    simulation_id = str(config.get("simulation_id") or "").strip()
    if simulation_id:
        return simulation_id
    return os.path.basename(os.path.dirname(config_path))


def _python_bin() -> str:
    candidate = os.path.join(_backend_dir, ".venv", "bin", "python")
    return candidate if os.path.exists(candidate) else sys.executable


def _run_world_process(
    config_path: str,
    *,
    max_rounds: Optional[int] = None,
    resume_from_checkpoint: bool = False,
) -> Dict[str, Any]:
    cmd = [_python_bin(), "scripts/run_world_simulation.py", "--config", config_path]
    if max_rounds is not None and max_rounds > 0:
        cmd.extend(["--max-rounds", str(max_rounds)])
    if resume_from_checkpoint:
        cmd.append("--resume-from-checkpoint")
    started_at = datetime.now().isoformat()
    completed = subprocess.run(cmd, cwd=_backend_dir, check=False)
    result = {
        "command": cmd,
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(),
        "returncode": completed.returncode,
    }
    if completed.returncode != 0:
        raise RuntimeError(f"world run failed with returncode={completed.returncode}")
    return result


def _finalize(
    simulation_id: str,
    *,
    label: str,
    report_id: str = "",
    report_timeout_seconds: float = DEFAULT_REPORT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    diagnostics = validate_diagnostics_or_raise(summarize_world_run(simulation_id, label))
    report = generate_world_report_with_fallback(
        python_bin=_python_bin(),
        backend_dir=_backend_dir,
        simulation_id=simulation_id,
        label=label,
        report_id=report_id or f"report_world_{simulation_id}_{label}",
        timeout_seconds=report_timeout_seconds,
    )
    return {
        "simulation_id": simulation_id,
        "label": label,
        "diagnostics": diagnostics,
        "report": report,
    }


def _status(config_path: str) -> Dict[str, Any]:
    simulation_id = _simulation_id_from_config(config_path)
    paths = world_run_paths_for_config(config_path)
    checkpoint_path = os.path.join(paths.world_dir, "checkpoint.json")
    checkpoint: Dict[str, Any] = {}
    if os.path.exists(checkpoint_path):
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            checkpoint = loaded
    lease = inspect_world_run_lease(paths, expected_config_path=config_path)
    return {
        "simulation_id": simulation_id,
        "config_path": config_path,
        "world_dir": paths.world_dir,
        "lease": lease,
        "checkpoint": {
            "status": checkpoint.get("status"),
            "saved_at": checkpoint.get("saved_at"),
            "last_completed_tick": checkpoint.get("last_completed_tick"),
            "run_total_rounds": checkpoint.get("run_total_rounds"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="World run operator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("run", "resume", "restore", "status"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--config", default="")
        sub.add_argument("--simulation-id", default="")
        if name in {"run", "resume"}:
            sub.add_argument("--max-rounds", type=int, default=None)
            sub.add_argument("--finalize-label", default="")
            sub.add_argument("--report-timeout-seconds", type=float, default=DEFAULT_REPORT_TIMEOUT_SECONDS)
        if name == "restore":
            sub.add_argument("--tick", type=int, required=True)
            sub.add_argument("--output", default="")

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--simulation-id", required=True)
    finalize_parser.add_argument("--label", required=True)
    finalize_parser.add_argument("--report-id", default="")
    finalize_parser.add_argument("--report-timeout-seconds", type=float, default=DEFAULT_REPORT_TIMEOUT_SECONDS)

    staged_parser = subparsers.add_parser("staged")
    staged_parser.add_argument("--simulation-id", required=True)
    staged_parser.add_argument("--stage1-rounds", type=int, default=8)
    staged_parser.add_argument("--final-rounds", type=int, default=16)
    staged_parser.add_argument("--report-timeout-seconds", type=float, default=DEFAULT_REPORT_TIMEOUT_SECONDS)

    args = parser.parse_args()

    if args.command in {"run", "resume"}:
        config_path = _resolve_config_path(args.config, args.simulation_id)
        simulation_id = _simulation_id_from_config(config_path)
        result: Dict[str, Any] = {
            "run": _run_world_process(
                config_path,
                max_rounds=args.max_rounds,
                resume_from_checkpoint=(args.command == "resume"),
            ),
            "simulation_id": simulation_id,
        }
        if args.finalize_label:
            result["finalize"] = _finalize(
                simulation_id,
                label=args.finalize_label,
                report_timeout_seconds=args.report_timeout_seconds,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "restore":
        config_path = _resolve_config_path(args.config, args.simulation_id)
        result = restore_world_checkpoint_from_logs(
            config_path=config_path,
            tick=args.tick,
            output_path=args.output,
            in_place=not bool(args.output),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "status":
        config_path = _resolve_config_path(args.config, args.simulation_id)
        print(json.dumps(_status(config_path), ensure_ascii=False, indent=2))
        return

    if args.command == "finalize":
        result = _finalize(
            args.simulation_id,
            label=args.label,
            report_id=args.report_id,
            report_timeout_seconds=args.report_timeout_seconds,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "staged":
        result = run_staged_experiment(
            simulation_id=args.simulation_id,
            stage1_rounds=args.stage1_rounds,
            final_rounds=args.final_rounds,
            report_timeout_seconds=args.report_timeout_seconds,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()
