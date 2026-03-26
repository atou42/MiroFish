import json
from pathlib import Path

from scripts.run_world_simulation import WorldEvent, WorldSimulationRuntime


def _write_minimal_world_config(tmp_path: Path) -> Path:
    simulation_dir = tmp_path / "sim_world_stimuli"
    simulation_dir.mkdir()
    config_path = simulation_dir / "simulation_config.json"
    config = {
        "simulation_id": "sim_world_stimuli",
        "simulation_mode": "world",
        "simulation_requirement": "stimuli test",
        "time_config": {
            "total_ticks": 4,
            "minutes_per_round": 60,
        },
        "agent_configs": [
            {
                "agent_id": 1,
                "entity_name": "Actor One",
                "entity_type": "faction",
            }
        ],
        "plot_threads": [{"title": "Stimuli Front"}],
        "pressure_tracks": [
            {"name": "conflict", "starting_level": 0.4},
            {"name": "scarcity", "starting_level": 0.3},
            {"name": "legitimacy", "starting_level": 0.5},
        ],
        "initial_world_state": {"starting_condition": "stimuli"},
        "runtime_config": {
            "max_active_events": 1,
            "max_queued_events": 1,
        },
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def _build_runtime(tmp_path: Path, monkeypatch) -> WorldSimulationRuntime:
    monkeypatch.setattr(WorldSimulationRuntime, "_build_llm", lambda self, selector=None: None)
    monkeypatch.setattr(WorldSimulationRuntime, "_get_actor_llm", lambda self, agent=None: None)
    return WorldSimulationRuntime(config_path=str(_write_minimal_world_config(tmp_path)), max_rounds=1)


def test_stimulus_force_active_preempts_low_priority_event(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)

    low_event = WorldEvent(
        event_id="event_0_low",
        tick=0,
        title="Low Priority Patrol",
        summary="keep calm",
        primary_agent_id=1,
        primary_agent_name="Actor One",
        participants=["Actor One"],
        participant_ids=[1],
        source_intent_ids=["intent_low"],
        priority=1,
        duration_ticks=3,
        resolves_at_tick=3,
        status="active",
        location="Test Sea",
        dependencies=[],
        state_impacts={},
        source="test",
        rationale="",
    )
    runtime.active_events[low_event.event_id] = low_event

    stimuli = [
        {
            "stimulus_id": "shock_001",
            "tick": 1,
            "title": "Major Shock Event",
            "summary": "forces everyone to react",
            "participants": ["Actor One", "World Government"],
            "priority": 5,
            "duration_ticks": 1,
            "state_impacts": {"conflict": 0.2, "legitimacy": -0.2},
            "inject_mode": "force_active",
        }
    ]
    runtime.stimuli_path = str(Path(runtime.world_dir) / "stimuli.json")
    Path(runtime.stimuli_path).write_text(json.dumps(stimuli, ensure_ascii=False, indent=2), encoding="utf-8")

    runtime._inject_stimuli(1, "Stimuli Front")

    assert "shock_001" in runtime.applied_stimuli_ids
    assert len(runtime.active_events) == 1
    assert any(event.title == "Major Shock Event" for event in runtime.active_events.values())
    assert "event_0_low" in runtime.queued_events


def test_stimulus_persists_in_checkpoint(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)
    runtime.applied_stimuli_ids.add("shock_002")
    runtime._write_checkpoint(status="running")
    payload = json.loads(Path(runtime.checkpoint_path).read_text(encoding="utf-8"))
    assert "shock_002" in payload.get("applied_stimuli_ids", [])


def test_stimulus_event_uses_risk_tags_and_participant_ids(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)

    event = runtime._event_from_stimulus(
        1,
        {
            "stimulus_id": "shock_003",
            "tick": 1,
            "title": "Pier Ambush",
            "summary": "A lethal boarding clash erupts at the pier.",
            "participants": ["Actor One", "Unknown Raider"],
            "participant_ids": [1],
            "priority": 5,
            "risk_level": 5,
            "tags": ["battle", "ambush"],
            "duration_ticks": 1,
            "state_impacts": {"conflict": 0.2},
        },
    )

    assert event.risk_level == 5
    assert event.tags == ["battle", "ambush"]
    assert event.participant_ids == [1]
