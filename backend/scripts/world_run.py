#!/usr/bin/env python3
"""
World-mode operator CLI: compile-pack, run, resume, restore, finalize, staged,
pipeline, status.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)
sys.path.insert(0, _project_root)

from app.config import Config
from app.services.world_pack_compiler import WorldPackCompiler
from app.services.simulation_runner import RunnerStatus, SimulationRunner
from app.utils.world_run_lock import inspect_world_run_lease, world_run_paths_for_config
from scripts.run_world_pipeline import _parse_stage_rounds, run_world_pipeline
from scripts.run_world_simulation import fork_world_simulation_from_logs, restore_world_checkpoint_from_logs
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


def _configured_total_rounds(config_path: str) -> int:
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    time_config = config.get("time_config", {})
    configured_rounds = int(
        time_config.get(
            "total_ticks",
            time_config.get("total_rounds", 0),
        )
        or 0
    )
    if configured_rounds > 0:
        return configured_rounds
    total_hours = int(time_config.get("total_simulation_hours", 12) or 12)
    minutes_per_round = int(time_config.get("minutes_per_round", 60) or 60)
    return max(1, int(total_hours * 60 / max(minutes_per_round, 1)))


def _resolve_target_rounds(
    config_path: str,
    *,
    max_rounds: Optional[int],
    resume_from_checkpoint: bool,
) -> int:
    configured_rounds = _configured_total_rounds(config_path)
    if max_rounds is not None and max_rounds > 0:
        return int(max_rounds)
    if resume_from_checkpoint:
        checkpoint_status = _status(config_path).get("checkpoint") or {}
        checkpoint_rounds = int(checkpoint_status.get("run_total_rounds") or 0)
        if checkpoint_rounds > 0:
            return checkpoint_rounds
    return configured_rounds


def _run_world_process_once(
    config_path: str,
    *,
    max_rounds: Optional[int] = None,
    resume_from_checkpoint: bool = False,
) -> Dict[str, Any]:
    simulation_id = _simulation_id_from_config(config_path)
    target_rounds = _resolve_target_rounds(
        config_path,
        max_rounds=max_rounds,
        resume_from_checkpoint=resume_from_checkpoint,
    )
    cmd = [_python_bin(), "scripts/run_world_simulation.py", "--config", config_path]
    if max_rounds is not None and max_rounds > 0:
        cmd.extend(["--max-rounds", str(max_rounds)])
    if resume_from_checkpoint:
        cmd.append("--resume-from-checkpoint")
    started_at = datetime.now().isoformat()
    process = subprocess.Popen(cmd, cwd=_backend_dir)
    SimulationRunner.bootstrap_world_operator_run_state(
        simulation_id,
        config_path,
        max_rounds=target_rounds,
        resume_from_checkpoint=resume_from_checkpoint,
        process_pid=process.pid,
    )

    while True:
        returncode = process.poll()
        if returncode is not None:
            break
        SimulationRunner.refresh_world_run_state_from_artifacts(
            simulation_id,
            persist=True,
            fallback_runner_status=RunnerStatus.RUNNING,
            process_pid=process.pid,
        )
        time.sleep(1.0)

    SimulationRunner.refresh_world_run_state_from_artifacts(
        simulation_id,
        persist=True,
    )
    result = {
        "command": cmd,
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(),
        "returncode": returncode,
    }
    if returncode != 0:
        raise RuntimeError(f"world run failed with returncode={returncode}")
    return result


def _checkpoint_reached_target(checkpoint: Dict[str, Any], target_rounds: int) -> bool:
    status = str(checkpoint.get("status") or "").strip().lower()
    last_completed_tick = int(checkpoint.get("last_completed_tick") or 0)
    return status == "completed" and last_completed_tick >= target_rounds


def _run_world_process(
    config_path: str,
    *,
    max_rounds: Optional[int] = None,
    resume_from_checkpoint: bool = False,
    max_resume_attempts: int = 3,
) -> Dict[str, Any]:
    target_rounds = _resolve_target_rounds(
        config_path,
        max_rounds=max_rounds,
        resume_from_checkpoint=resume_from_checkpoint,
    )
    attempts = []
    started_at = datetime.now().isoformat()
    next_resume = resume_from_checkpoint
    last_error = ""

    for attempt_index in range(max(max_resume_attempts, 0) + 1):
        try:
            run_result = _run_world_process_once(
                config_path,
                max_rounds=max_rounds,
                resume_from_checkpoint=next_resume,
            )
        except Exception as exc:
            run_result = {
                "command": [],
                "started_at": datetime.now().isoformat(),
                "completed_at": datetime.now().isoformat(),
                "returncode": -1,
                "error": str(exc),
            }
            last_error = str(exc)

        status_payload = _status(config_path)
        checkpoint = status_payload.get("checkpoint") or {}
        run_result["checkpoint_after"] = checkpoint
        run_result["attempt_index"] = attempt_index
        run_result["resume_from_checkpoint"] = next_resume
        attempts.append(run_result)

        if _checkpoint_reached_target(checkpoint, target_rounds):
            return {
                "command": attempts[-1].get("command", []),
                "started_at": started_at,
                "completed_at": datetime.now().isoformat(),
                "returncode": 0,
                "target_rounds": target_rounds,
                "auto_resumed": len(attempts) > 1,
                "attempts": attempts,
                "checkpoint": checkpoint,
            }

        last_completed_tick = int(checkpoint.get("last_completed_tick") or 0)
        checkpoint_status = str(checkpoint.get("status") or "").strip().lower()
        terminal = checkpoint_status in {"failed", "interrupted"}
        if terminal:
            last_error = last_error or f"checkpoint.status={checkpoint_status}"
        if attempt_index >= max(max_resume_attempts, 0):
            break
        if last_completed_tick >= target_rounds:
            break
        next_resume = True
        time.sleep(0.2)

    checkpoint = attempts[-1].get("checkpoint_after", {}) if attempts else {}
    raise RuntimeError(
        "world supervised run did not reach target rounds: "
        f"target_rounds={target_rounds}, checkpoint_status={checkpoint.get('status')}, "
        f"last_completed_tick={checkpoint.get('last_completed_tick')}, error={last_error or 'unknown'}"
    )


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
            "terminal_status": checkpoint.get("terminal_status"),
            "stop_reason": checkpoint.get("stop_reason"),
            "saved_at": checkpoint.get("saved_at"),
            "last_completed_tick": checkpoint.get("last_completed_tick"),
            "run_total_rounds": checkpoint.get("run_total_rounds"),
            "target_rounds": checkpoint.get("target_rounds"),
            "stop_mode": checkpoint.get("stop_mode"),
            "max_drain_rounds": checkpoint.get("max_drain_rounds"),
            "drain_rounds_used": checkpoint.get("drain_rounds_used"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="World run operator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser("compile-pack")
    compile_parser.add_argument("--source-dir", required=True)
    compile_parser.add_argument("--simulation-requirement", default="")
    compile_parser.add_argument("--simulation-id", default="")
    compile_parser.add_argument("--project-id", default="")
    compile_parser.add_argument("--graph-id", default="")
    compile_parser.add_argument("--world-preset", default="")
    compile_parser.add_argument("--pack-id", default="")
    compile_parser.add_argument("--pack-title", default="")
    compile_parser.add_argument("--no-llm-profiles", action="store_true")
    compile_parser.add_argument("--use-llm-config", action="store_true")
    compile_parser.add_argument("--profile-parallel-count", type=int, default=3)

    for name in ("run", "resume", "restore", "status"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--config", default="")
        sub.add_argument("--simulation-id", default="")
        if name in {"run", "resume"}:
            sub.add_argument("--max-rounds", type=int, default=None)
            sub.add_argument("--max-resume-attempts", type=int, default=3)
            sub.add_argument("--finalize-label", default="")
            sub.add_argument("--report-timeout-seconds", type=float, default=DEFAULT_REPORT_TIMEOUT_SECONDS)
        if name == "restore":
            sub.add_argument("--tick", type=int, required=True)
            sub.add_argument("--output", default="")

    fork_parser = subparsers.add_parser("fork")
    fork_parser.add_argument("--config", default="")
    fork_parser.add_argument("--simulation-id", default="")
    fork_parser.add_argument("--tick", type=int, required=True)
    fork_parser.add_argument("--new-simulation-id", default="")

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

    pipeline_parser = subparsers.add_parser("pipeline")
    pipeline_parser.add_argument("--simulation-id", required=True)
    pipeline_parser.add_argument(
        "--stage-rounds",
        action="append",
        required=True,
        help="Comma-separated stage targets, for example 8,16,32 or repeat the flag.",
    )
    pipeline_parser.add_argument("--label-prefix", default="stage")
    pipeline_parser.add_argument("--report-timeout-seconds", type=float, default=DEFAULT_REPORT_TIMEOUT_SECONDS)
    pipeline_parser.add_argument("--skip-reports", action="store_true")

    args = parser.parse_args()

    if args.command == "compile-pack":
        compiler = WorldPackCompiler()
        result = compiler.compile(
            source_dir=args.source_dir,
            simulation_requirement=args.simulation_requirement,
            simulation_id=args.simulation_id,
            project_id=args.project_id,
            graph_id=args.graph_id,
            world_preset_id=args.world_preset or None,
            pack_id=args.pack_id,
            pack_title=args.pack_title,
            use_llm_for_profiles=not args.no_llm_profiles,
            use_llm_for_config=args.use_llm_config,
            profile_parallel_count=args.profile_parallel_count,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command in {"run", "resume"}:
        config_path = _resolve_config_path(args.config, args.simulation_id)
        simulation_id = _simulation_id_from_config(config_path)
        result: Dict[str, Any] = {
            "run": _run_world_process(
                config_path,
                max_rounds=args.max_rounds,
                resume_from_checkpoint=(args.command == "resume"),
                max_resume_attempts=args.max_resume_attempts,
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

    if args.command == "fork":
        config_path = _resolve_config_path(args.config, args.simulation_id)
        result = fork_world_simulation_from_logs(
            config_path=config_path,
            tick=args.tick,
            new_simulation_id=args.new_simulation_id,
        )
        SimulationRunner.refresh_world_run_state_from_artifacts(
            result["new_simulation_id"],
            persist=True,
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

    if args.command == "pipeline":
        result = run_world_pipeline(
            simulation_id=args.simulation_id,
            stage_rounds=_parse_stage_rounds(args.stage_rounds),
            label_prefix=args.label_prefix,
            report_timeout_seconds=args.report_timeout_seconds,
            skip_reports=args.skip_reports,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()
