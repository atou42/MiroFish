import json
from pathlib import Path

import pytest

from app.utils.world_run_lock import WorldRunLease, world_run_paths_for_config


def _write_config(tmp_path: Path) -> Path:
    simulation_dir = tmp_path / "sim_lock"
    simulation_dir.mkdir()
    config_path = simulation_dir / "simulation_config.json"
    config = {
        "simulation_id": "sim_lock",
        "simulation_mode": "world",
        "time_config": {"total_ticks": 4, "minutes_per_round": 60},
        "agent_configs": [],
        "plot_threads": [],
        "pressure_tracks": [],
        "initial_world_state": {},
        "runtime_config": {},
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def test_world_run_lease_blocks_duplicate_start(tmp_path):
    config_path = _write_config(tmp_path)

    lease = WorldRunLease(str(config_path)).acquire()
    try:
        with pytest.raises(RuntimeError):
            WorldRunLease(str(config_path)).acquire()
    finally:
        lease.release()


def test_world_run_lease_recovers_stale_pid(tmp_path):
    config_path = _write_config(tmp_path)
    paths = world_run_paths_for_config(str(config_path))
    paths_dir = Path(paths.world_dir)
    paths_dir.mkdir(parents=True, exist_ok=True)
    (paths_dir / "run.pid").write_text("999999", encoding="utf-8")
    (paths_dir / "run.lease.json").write_text(
        json.dumps(
            {
                "pid": 999999,
                "simulation_id": "sim_lock",
                "config_path": str(config_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lease = WorldRunLease(str(config_path)).acquire()
    try:
        assert (paths_dir / "run.pid").read_text(encoding="utf-8").strip().isdigit()
        lease_payload = json.loads((paths_dir / "run.lease.json").read_text(encoding="utf-8"))
        assert int(lease_payload["pid"]) > 0
        assert lease_payload["config_path"] == str(config_path)
    finally:
        lease.release()
