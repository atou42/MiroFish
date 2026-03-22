import json
from pathlib import Path

from app.services.simulation_runner import SimulationRunner
from scripts.run_world_simulation import fork_world_simulation_from_logs


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _row_tick(payload: dict):
    raw = payload.get("tick")
    if raw is None:
        raw = payload.get("round")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def test_world_fork_creates_parallel_simulation(tmp_path: Path, monkeypatch):
    source_sim_dir = tmp_path / "sim_source"
    source_world_dir = source_sim_dir / "world"
    source_world_dir.mkdir(parents=True)

    _write_json(
        source_sim_dir / "simulation_config.json",
        {
            "simulation_id": "sim_source",
            "simulation_mode": "world",
            "time_config": {"total_ticks": 6, "minutes_per_round": 60},
            "agent_configs": [{"agent_id": 1, "entity_name": "Actor", "entity_type": "faction"}],
            "plot_threads": [{"title": "Fork Front"}],
            "pressure_tracks": [
                {"name": "conflict", "starting_level": 0.4},
                {"name": "scarcity", "starting_level": 0.3},
                {"name": "legitimacy", "starting_level": 0.5},
            ],
            "initial_world_state": {"starting_condition": "fork"},
            "runtime_config": {},
        },
    )
    _write_json(source_sim_dir / "world_profiles.json", [{"agent_id": 1, "entity_name": "Actor"}])
    _write_json(
        source_sim_dir / "state.json",
        {
            "simulation_id": "sim_source",
            "project_id": "proj_test",
            "graph_id": "graph_test",
            "simulation_mode": "world",
            "enable_twitter": False,
            "enable_reddit": False,
            "status": "running",
            "entities_count": 1,
            "profiles_count": 1,
            "entity_types": ["Faction"],
            "config_generated": True,
            "runtime_metadata": {"profiles_file": "world_profiles.json"},
        },
    )

    actions = [
        {"event_type": "simulation_start", "timestamp": "t0", "simulation_mode": "world", "total_rounds": 6},
        {"event_type": "tick_start", "timestamp": "t1", "simulation_mode": "world", "tick": None, "round": 1},
        {
            "event_type": "stimulus_injected",
            "timestamp": "t2",
            "simulation_mode": "world",
            "tick": 2,
            "round": 2,
            "stimulus_id": "shock_001",
        },
        {"event_type": "tick_start", "timestamp": "t3", "simulation_mode": "world", "tick": None, "round": 3},
        {"event_type": "event_started", "timestamp": "t3", "simulation_mode": "world", "tick": 3, "round": 3, "action_args": {"event_id": "event_3"}},
    ]
    _write_jsonl(source_world_dir / "actions.jsonl", actions)

    snapshots = [
        {"tick": 1, "round": 1, "phase": "tick_complete", "summary": "tick1", "world_state": {"tension": 0.5}},
        {"tick": 2, "round": 2, "phase": "tick_complete", "summary": "tick2", "world_state": {"tension": 0.6}},
        {"tick": 3, "round": 3, "phase": "tick_complete", "summary": "tick3", "world_state": {"tension": 0.7}},
    ]
    _write_jsonl(source_world_dir / "state_snapshots.jsonl", snapshots)

    _write_json(source_world_dir / "checkpoint.json", {"run_total_rounds": 10, "target_rounds": 10, "minutes_per_round": 60})

    result = fork_world_simulation_from_logs(
        str(source_sim_dir / "simulation_config.json"),
        2,
        new_simulation_id="sim_forked",
        destination_base_dir=str(tmp_path),
    )

    assert result["source_simulation_id"] == "sim_source"
    assert result["source_tick"] == 2
    assert result["new_simulation_id"] == "sim_forked"

    fork_dir = tmp_path / "sim_forked"
    assert fork_dir.exists()

    fork_config = json.loads((fork_dir / "simulation_config.json").read_text(encoding="utf-8"))
    assert fork_config["simulation_id"] == "sim_forked"
    assert fork_config.get("fork_origin", {}).get("source_simulation_id") == "sim_source"
    assert fork_config.get("fork_origin", {}).get("source_tick") == 2

    fork_state = json.loads((fork_dir / "state.json").read_text(encoding="utf-8"))
    assert fork_state["simulation_id"] == "sim_forked"
    assert fork_state["status"] == "ready"
    assert fork_state["current_round"] == 2
    assert fork_state.get("runtime_metadata", {}).get("fork_origin", {}).get("source_simulation_id") == "sim_source"

    fork_actions_path = fork_dir / "world" / "actions.jsonl"
    fork_actions = [
        json.loads(line)
        for line in fork_actions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert not any((_row_tick(row) or 0) > 2 for row in fork_actions if _row_tick(row) is not None)
    assert not any(row.get("event_type") == "event_started" and (_row_tick(row) or 0) > 2 for row in fork_actions)

    fork_snapshots_path = fork_dir / "world" / "state_snapshots.jsonl"
    fork_snapshots = [
        json.loads(line)
        for line in fork_snapshots_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {row.get("tick") for row in fork_snapshots} == {1, 2}

    fork_checkpoint = json.loads((fork_dir / "world" / "checkpoint.json").read_text(encoding="utf-8"))
    assert fork_checkpoint["status"] == "restored"
    assert fork_checkpoint["last_completed_tick"] == 2
    assert fork_checkpoint["run_total_rounds"] == 10
    assert fork_checkpoint["target_rounds"] == 10
    assert "shock_001" in fork_checkpoint.get("applied_stimuli_ids", [])

    world_state = json.loads((fork_dir / "world" / "world_state.json").read_text(encoding="utf-8"))
    assert world_state.get("tick") == 2

    assert (fork_dir / "fork_meta.json").exists()

    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    run_state = SimulationRunner.refresh_world_run_state_from_artifacts("sim_forked", persist=False)
    assert run_state is not None
    assert run_state.runner_status.value == "idle"
    assert run_state.world_running is False
    assert run_state.current_round == 2
    assert run_state.total_rounds == 10

    # Source untouched.
    source_config = json.loads((source_sim_dir / "simulation_config.json").read_text(encoding="utf-8"))
    assert source_config["simulation_id"] == "sim_source"
