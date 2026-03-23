#!/usr/bin/env python3
"""
Supervise a long-running world simulation until a target round, then finalize.

The supervisor:
1. Polls checkpoint/lease/trace activity
2. Starts or restarts the world runtime if it is not running and target is not reached
3. Captures stall diagnostics when trace activity freezes
4. Runs serial finalization once the target round is completed
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.network_env import clear_proxy_env


DEFAULT_POLL_SECONDS = 20
DEFAULT_STALL_SECONDS = 360
DEFAULT_REPORT_TIMEOUT_SECONDS = 1800


def now_iso() -> str:
    return datetime.now().isoformat()


def safe_json_load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def process_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def latest_trace_timestamp(trace_root: Path) -> float:
    latest_mtime = 0.0
    if not trace_root.exists():
        return latest_mtime
    for path in trace_root.rglob("*"):
        try:
            if path.is_file():
                latest_mtime = max(latest_mtime, path.stat().st_mtime)
        except FileNotFoundError:
            continue
    return latest_mtime


def build_paths(simulation_id: str) -> Dict[str, Path]:
    sim_dir = BACKEND_DIR / "uploads" / "simulations" / simulation_id
    world_dir = sim_dir / "world"
    supervision_dir = world_dir / "supervision"
    supervision_dir.mkdir(parents=True, exist_ok=True)
    return {
        "sim_dir": sim_dir,
        "world_dir": world_dir,
        "config": sim_dir / "simulation_config.json",
        "checkpoint": world_dir / "checkpoint.json",
        "lease": world_dir / "run.lease.json",
        "trace_root": world_dir / "debug" / "llm_traces",
        "supervision_dir": supervision_dir,
    }


def capture_stall_sample(pid: int, output_path: Path) -> None:
    if not process_alive(pid):
        return
    sample_cmd = ["sample", str(pid), "1", "1", "-file", str(output_path)]
    try:
        subprocess.run(sample_cmd, check=False, capture_output=True, text=True)
    except Exception:
        return


def start_world_runtime(config_path: Path, target_rounds: int, env: Dict[str, str], log_path: Path) -> subprocess.Popen[str]:
    log_file = open(log_path, "a", encoding="utf-8")
    cmd = [
        str(BACKEND_DIR / ".venv" / "bin" / "python"),
        "scripts/run_world_simulation.py",
        "--config",
        str(config_path),
        "--max-rounds",
        str(target_rounds),
        "--resume-from-checkpoint",
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def run_finalize(simulation_id: str, label: str, report_timeout_seconds: float, env: Dict[str, str], log_path: Path) -> Dict[str, Any]:
    cmd = [
        str(BACKEND_DIR / ".venv" / "bin" / "python"),
        "scripts/world_run.py",
        "finalize",
        "--simulation-id",
        simulation_id,
        "--label",
        label,
        "--report-timeout-seconds",
        str(report_timeout_seconds),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    log_path.write_text(
        json.dumps(
            {
                "ran_at": now_iso(),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Supervise a long-running world simulation")
    parser.add_argument("--simulation-id", required=True)
    parser.add_argument("--target-rounds", type=int, required=True)
    parser.add_argument("--finalize-label", required=True)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--stall-seconds", type=float, default=DEFAULT_STALL_SECONDS)
    parser.add_argument("--report-timeout-seconds", type=float, default=DEFAULT_REPORT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    paths = build_paths(args.simulation_id)
    env = clear_proxy_env()

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    supervision_dir = paths["supervision_dir"]
    monitor_log = supervision_dir / f"{stamp}-target{args.target_rounds}.jsonl"
    runtime_log = supervision_dir / f"{stamp}-runtime.log"
    finalize_log = supervision_dir / f"{stamp}-finalize.json"
    stall_dir = supervision_dir / f"{stamp}-stall"
    stall_dir.mkdir(parents=True, exist_ok=True)

    append_jsonl(
        monitor_log,
        {
            "ts": now_iso(),
            "event": "supervisor_start",
            "simulation_id": args.simulation_id,
            "target_rounds": args.target_rounds,
            "finalize_label": args.finalize_label,
            "runtime_log": str(runtime_log),
        },
    )

    child: Optional[subprocess.Popen[str]] = None
    last_trace_mtime = latest_trace_timestamp(paths["trace_root"])
    last_progress_wall = time.time()
    stall_capture_index = 0
    finalized = False
    previous_completed_tick = 0

    while True:
        checkpoint = safe_json_load(paths["checkpoint"])
        lease = safe_json_load(paths["lease"])
        last_completed_tick = int(checkpoint.get("last_completed_tick") or 0)
        status = str(checkpoint.get("status") or "")
        lease_pid = int(lease.get("pid") or 0) if lease else 0
        alive_pid = lease_pid if process_alive(lease_pid) else 0
        trace_mtime = latest_trace_timestamp(paths["trace_root"])

        if trace_mtime > last_trace_mtime or last_completed_tick > previous_completed_tick:
            last_progress_wall = time.time()
            last_trace_mtime = trace_mtime
            previous_completed_tick = last_completed_tick

        append_jsonl(
            monitor_log,
            {
                "ts": now_iso(),
                "event": "poll",
                "checkpoint_status": status,
                "last_completed_tick": last_completed_tick,
                "target_rounds": checkpoint.get("target_rounds"),
                "lease_pid": lease_pid or None,
                "lease_alive": bool(alive_pid),
                "trace_mtime": trace_mtime,
            },
        )

        if status == "completed" and last_completed_tick >= args.target_rounds:
            if not finalized:
                append_jsonl(
                    monitor_log,
                    {
                        "ts": now_iso(),
                        "event": "finalize_start",
                        "label": args.finalize_label,
                    },
                )
                finalize_result = run_finalize(
                    simulation_id=args.simulation_id,
                    label=args.finalize_label,
                    report_timeout_seconds=args.report_timeout_seconds,
                    env=env,
                    log_path=finalize_log,
                )
                append_jsonl(
                    monitor_log,
                    {
                        "ts": now_iso(),
                        "event": "finalize_done",
                        "returncode": finalize_result["returncode"],
                    },
                )
                finalized = True
            break

        if not alive_pid:
            if child is not None and child.poll() is not None:
                child = None
            append_jsonl(
                monitor_log,
                {
                    "ts": now_iso(),
                    "event": "runtime_start",
                    "from_tick": last_completed_tick,
                },
            )
            child = start_world_runtime(paths["config"], args.target_rounds, env, runtime_log)
            time.sleep(2)
            continue

        if time.time() - last_progress_wall >= args.stall_seconds:
            stall_capture_index += 1
            sample_path = stall_dir / f"stall-{stall_capture_index:03d}-pid{alive_pid}.sample"
            capture_stall_sample(alive_pid, sample_path)
            append_jsonl(
                monitor_log,
                {
                    "ts": now_iso(),
                    "event": "stall_sample",
                    "pid": alive_pid,
                    "sample_path": str(sample_path),
                },
            )
            last_progress_wall = time.time()

        time.sleep(max(args.poll_seconds, 5.0))

    append_jsonl(
        monitor_log,
        {
            "ts": now_iso(),
            "event": "supervisor_exit",
            "finalized": finalized,
        },
    )


if __name__ == "__main__":
    main()
