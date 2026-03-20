from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


def _abs_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class WorldRunPaths:
    simulation_dir: str
    world_dir: str
    lock_path: str
    pid_path: str
    lease_path: str


def world_run_paths_for_simulation_dir(simulation_dir: str) -> WorldRunPaths:
    simulation_dir = _abs_path(simulation_dir)
    world_dir = os.path.join(simulation_dir, "world")
    return WorldRunPaths(
        simulation_dir=simulation_dir,
        world_dir=world_dir,
        lock_path=os.path.join(world_dir, ".run.lock"),
        pid_path=os.path.join(world_dir, "run.pid"),
        lease_path=os.path.join(world_dir, "run.lease.json"),
    )


def world_run_paths_for_config(config_path: str) -> WorldRunPaths:
    return world_run_paths_for_simulation_dir(os.path.dirname(_abs_path(config_path)))


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_process_command(pid: int) -> str:
    if pid <= 0 or sys.platform == "win32":
        return ""
    try:
        completed = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip().splitlines()[0].strip() if (completed.stdout or "").strip() else ""


def load_world_run_lease(paths: WorldRunPaths) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if os.path.exists(paths.lease_path):
        try:
            with open(paths.lease_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                payload.update(loaded)
        except Exception:
            payload = {}
    if "pid" not in payload and os.path.exists(paths.pid_path):
        try:
            with open(paths.pid_path, "r", encoding="utf-8") as f:
                payload["pid"] = int((f.read() or "").strip())
        except Exception:
            pass
    return payload


def cleanup_world_run_lease(paths: WorldRunPaths) -> None:
    for path in (paths.pid_path, paths.lease_path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def inspect_world_run_lease(
    paths: WorldRunPaths,
    expected_config_path: str = "",
) -> Dict[str, Any]:
    lease = load_world_run_lease(paths)
    pid = _safe_int(lease.get("pid"), 0)
    alive = pid_is_alive(pid)
    command = read_process_command(pid) if alive else ""
    expected_config = _abs_path(expected_config_path) if expected_config_path else ""
    matches_script = True if not command else "run_world_simulation.py" in command
    matches_config = True if not expected_config else expected_config in command
    return {
        "lease": lease,
        "pid": pid,
        "alive": alive,
        "command": command,
        "matches_script": matches_script,
        "matches_config": matches_config,
        "matches_expected_run": alive and matches_script and matches_config,
    }


class WorldRunLease:
    def __init__(self, config_path: str, simulation_id: str = ""):
        self.config_path = _abs_path(config_path)
        self.simulation_id = simulation_id or os.path.basename(os.path.dirname(self.config_path))
        self.paths = world_run_paths_for_config(self.config_path)
        self._lock_file: Optional[Any] = None
        self._acquired = False

    def acquire(self) -> "WorldRunLease":
        os.makedirs(self.paths.world_dir, exist_ok=True)
        self._lock_file = open(self.paths.lock_path, "a+", encoding="utf-8")

        if fcntl is not None:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(self._conflict_message()) from exc

        lease_status = inspect_world_run_lease(self.paths, expected_config_path=self.config_path)
        if lease_status["matches_expected_run"] and lease_status["pid"] != os.getpid():
            self.release(clear_files=False)
            raise RuntimeError(self._conflict_message(status=lease_status))

        cleanup_world_run_lease(self.paths)
        self._write_lease()
        self._acquired = True
        return self

    def release(self, clear_files: bool = True) -> None:
        if clear_files:
            lease = load_world_run_lease(self.paths)
            lease_pid = _safe_int(lease.get("pid"), 0)
            if not lease_pid or lease_pid == os.getpid():
                cleanup_world_run_lease(self.paths)

        if self._lock_file is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._lock_file.close()
            except OSError:
                pass
            self._lock_file = None
        self._acquired = False

    def _write_lease(self) -> None:
        payload = {
            "pid": os.getpid(),
            "simulation_id": self.simulation_id,
            "config_path": self.config_path,
            "world_dir": self.paths.world_dir,
            "started_at": datetime.now().isoformat(),
            "host": socket.gethostname(),
            "argv": list(sys.argv),
            "command": " ".join(sys.argv),
        }
        with open(self.paths.pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        with open(self.paths.lease_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _conflict_message(self, status: Optional[Dict[str, Any]] = None) -> str:
        status = status or inspect_world_run_lease(self.paths, expected_config_path=self.config_path)
        pid = status.get("pid") or "unknown"
        command = status.get("command") or "(command unavailable)"
        return (
            f"world simulation already running for {self.simulation_id}: "
            f"pid={pid}, config={self.config_path}, command={command}"
        )

    def __enter__(self) -> "WorldRunLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
