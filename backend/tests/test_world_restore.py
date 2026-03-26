import json
from pathlib import Path

from scripts.run_world_simulation import ActorIntent, WorldEvent, restore_world_checkpoint_from_logs


def _write_config(simulation_dir: Path) -> Path:
    config_path = simulation_dir / "simulation_config.json"
    config = {
        "simulation_id": "sim_restore",
        "simulation_mode": "world",
        "time_config": {
            "total_ticks": 6,
            "minutes_per_round": 60,
        },
        "agent_configs": [
            {"agent_id": 1, "entity_name": "Actor One", "entity_type": "faction"},
            {"agent_id": 2, "entity_name": "Actor Two", "entity_type": "faction"},
        ],
        "plot_threads": [{"title": "Restore Front"}],
        "pressure_tracks": [
            {"name": "conflict", "starting_level": 0.4},
            {"name": "scarcity", "starting_level": 0.3},
            {"name": "legitimacy", "starting_level": 0.5},
        ],
        "initial_world_state": {"starting_condition": "restore"},
        "runtime_config": {},
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def _write_jsonl(path: Path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_restore_world_checkpoint_rebuilds_counters(tmp_path):
    simulation_dir = tmp_path / "sim_restore"
    world_dir = simulation_dir / "world"
    world_dir.mkdir(parents=True)
    config_path = _write_config(simulation_dir)

    intent1 = ActorIntent(
        intent_id="intent_001_0001",
        tick=1,
        agent_id=1,
        agent_name="Actor One",
        objective="Secure Harbor",
        summary="Actor One secures the harbor",
    )
    event1 = WorldEvent(
        event_id="event_001_0001",
        tick=1,
        title="Secure Harbor",
        summary="Harbor secured",
        primary_agent_id=1,
        primary_agent_name="Actor One",
        participants=["Actor One"],
        participant_ids=[1],
        source_intent_ids=[intent1.intent_id],
        priority=4,
        duration_ticks=1,
        resolves_at_tick=1,
        status="completed",
    )

    intent2 = ActorIntent(
        intent_id="intent_002_0002",
        tick=2,
        agent_id=2,
        agent_name="Actor Two",
        objective="Build Relay",
        summary="Actor Two starts a relay network",
    )
    event2 = WorldEvent(
        event_id="event_002_0002",
        tick=2,
        title="Build Relay",
        summary="Relay queued",
        primary_agent_id=2,
        primary_agent_name="Actor Two",
        participants=["Actor Two"],
        participant_ids=[2],
        source_intent_ids=[intent2.intent_id],
        priority=3,
        duration_ticks=2,
        resolves_at_tick=3,
        status="queued",
    )

    actions = [
        {"event_type": "simulation_start", "total_rounds": 6},
        intent1.to_log(),
        event1.start_log(active_count=1, queued_count=0),
        event1.complete_log(tick=1, active_count=0, queued_count=0),
        intent2.to_log(),
        event2.queue_log(queued_count=1),
    ]
    _write_jsonl(world_dir / "actions.jsonl", actions)

    snapshots = [
        {
            "tick": 1,
            "round": 1,
            "phase": "tick_complete",
            "summary": "tick1",
            "simulated_hours": 1.0,
            "world_state": {
                "tension": 0.61,
                "stability": 0.48,
                "momentum": 0.52,
                "pressure_tracks": {"conflict": 0.4},
                "focus_threads": ["Restore Front"],
                "last_tick_summary": "tick1",
            },
            "metrics": {
                "intents_created": 1,
                "accepted_events": 1,
                "deferred_intents": 0,
                "rejected_intents": 0,
                "active_events_count": 0,
                "queued_events_count": 0,
                "completed_events_count": 1,
            },
            "active_events": [],
            "queued_events": [],
            "recent_completed_events": [event1.to_state_dict()],
        },
        {
            "tick": 2,
            "round": 2,
            "phase": "tick_complete",
            "summary": "tick2",
            "simulated_hours": 2.0,
            "world_state": {
                "tension": 0.64,
                "stability": 0.45,
                "momentum": 0.58,
                "pressure_tracks": {"conflict": 0.42},
                "focus_threads": ["Restore Front"],
                "last_tick_summary": "tick2",
            },
            "metrics": {
                "intents_created": 1,
                "accepted_events": 1,
                "deferred_intents": 0,
                "rejected_intents": 0,
                "active_events_count": 0,
                "queued_events_count": 1,
                "completed_events_count": 1,
            },
            "active_events": [],
            "queued_events": [event2.to_state_dict()],
            "recent_completed_events": [event1.to_state_dict()],
        },
    ]
    _write_jsonl(world_dir / "state_snapshots.jsonl", snapshots)

    result = restore_world_checkpoint_from_logs(str(config_path), tick=2)
    checkpoint = json.loads((world_dir / "checkpoint.json").read_text(encoding="utf-8"))

    assert result["written"] is True
    assert checkpoint["status"] == "restored"
    assert checkpoint["last_completed_tick"] == 2
    assert checkpoint["intent_counter"] == 2
    assert checkpoint["event_counter"] == 2
    assert checkpoint["lifecycle_records"] == 5
    assert checkpoint["actor_selection_counts"] == {"1": 1, "2": 1} or checkpoint["actor_selection_counts"] == {1: 1, 2: 1}
    assert checkpoint["actor_event_counts"] == {"1": 1, "2": 1} or checkpoint["actor_event_counts"] == {1: 1, 2: 1}
    assert len(checkpoint["queued_events"]) == 1
    assert checkpoint["queued_events"][0]["event_id"] == "event_002_0002"
    assert len(checkpoint["completed_events"]) == 1
    assert checkpoint["completed_events"][0]["event_id"] == "event_001_0001"


def test_restore_world_checkpoint_preserves_actor_conditions_from_snapshot(tmp_path):
    simulation_dir = tmp_path / "sim_restore_conditions"
    world_dir = simulation_dir / "world"
    world_dir.mkdir(parents=True)
    config_path = _write_config(simulation_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for agent in config["agent_configs"]:
        agent["entity_type"] = "Character"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    intent = ActorIntent(
        intent_id="intent_001_0001",
        tick=1,
        agent_id=1,
        agent_name="Actor One",
        objective="Hold the harbor",
        summary="Actor One commits to a lethal harbor defense",
    )
    event = WorldEvent(
        event_id="event_001_0001",
        tick=1,
        title="Harbor Defense",
        summary="The harbor defense leaves Actor One dead.",
        primary_agent_id=1,
        primary_agent_name="Actor One",
        participants=["Actor One"],
        participant_ids=[1],
        source_intent_ids=[intent.intent_id],
        priority=5,
        risk_level=5,
        duration_ticks=1,
        resolves_at_tick=1,
        status="completed",
        tags=["battle"],
        condition_updates=[
            {
                "tick": 1,
                "event_id": "event_001_0001",
                "event_title": "Harbor Defense",
                "agent_id": 1,
                "agent_name": "Actor One",
                "from_status": "critical",
                "to_status": "dead",
                "summary": "Actor One 在「Harbor Defense」中死亡。",
            }
        ],
    )

    _write_jsonl(
        world_dir / "actions.jsonl",
        [
            {"event_type": "simulation_start", "total_rounds": 6},
            intent.to_log(),
            event.start_log(active_count=1, queued_count=0),
            event.complete_log(tick=1, active_count=0, queued_count=0),
        ],
    )
    _write_jsonl(
        world_dir / "state_snapshots.jsonl",
        [
            {
                "tick": 1,
                "round": 1,
                "phase": "tick_complete",
                "summary": "tick1",
                "simulated_hours": 1.0,
                "world_state": {
                    "tension": 0.74,
                    "stability": 0.22,
                    "momentum": 0.63,
                    "pressure_tracks": {"conflict": 0.7},
                    "focus_threads": ["Restore Front"],
                    "last_tick_summary": "tick1",
                    "actor_conditions": {
                        "1": {
                            "agent_id": 1,
                            "entity_name": "Actor One",
                            "entity_type": "Character",
                            "status": "dead",
                            "injury_score": 5,
                            "alive": False,
                            "availability": "removed",
                            "last_updated_tick": 1,
                            "last_event_id": "event_001_0001",
                            "last_event_title": "Harbor Defense",
                            "latest_note": "Actor One 在「Harbor Defense」中死亡。",
                        }
                    },
                    "actor_condition_summary": {
                        "healthy": 0,
                        "shaken": 0,
                        "wounded": 0,
                        "critical": 0,
                        "incapacitated": 0,
                        "dead": 1,
                        "active": 0,
                    },
                    "recent_condition_updates": event.condition_updates,
                },
                "metrics": {
                    "intents_created": 1,
                    "accepted_events": 1,
                    "deferred_intents": 0,
                    "rejected_intents": 0,
                    "active_events_count": 0,
                    "queued_events_count": 0,
                    "completed_events_count": 1,
                    "condition_updates_count": 1,
                },
                "active_events": [],
                "queued_events": [],
                "recent_completed_events": [event.to_state_dict()],
            }
        ],
    )

    result = restore_world_checkpoint_from_logs(str(config_path), tick=1)
    checkpoint = json.loads((world_dir / "checkpoint.json").read_text(encoding="utf-8"))

    assert result["written"] is True
    assert checkpoint["world_state"]["actor_conditions"]["1"]["status"] == "dead"
    assert checkpoint["world_state"]["actor_condition_summary"]["dead"] == 1
    assert checkpoint["last_snapshot"]["world_state"]["actor_conditions"]["1"]["status"] == "dead"


def test_restore_world_checkpoint_rebuilds_actor_memory_from_updates_log(tmp_path):
    simulation_dir = tmp_path / "sim_restore_memory"
    world_dir = simulation_dir / "world"
    memory_dir = world_dir / "memory"
    memory_dir.mkdir(parents=True)
    config_path = _write_config(simulation_dir)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    for agent in config["agent_configs"]:
        agent["entity_type"] = "Character"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_jsonl(world_dir / "actions.jsonl", [{"event_type": "simulation_start", "total_rounds": 6}])
    _write_jsonl(
        world_dir / "state_snapshots.jsonl",
        [
            {
                "tick": 1,
                "round": 1,
                "phase": "tick_complete",
                "summary": "tick1",
                "simulated_hours": 1.0,
                "world_state": {
                    "tension": 0.71,
                    "stability": 0.28,
                    "momentum": 0.64,
                    "pressure_tracks": {"conflict": 0.73},
                    "focus_threads": ["Restore Front"],
                    "last_tick_summary": "tick1",
                },
                "metrics": {
                    "intents_created": 1,
                    "accepted_events": 1,
                    "deferred_intents": 0,
                    "rejected_intents": 0,
                    "active_events_count": 0,
                    "queued_events_count": 0,
                    "completed_events_count": 0,
                },
                "active_events": [],
                "queued_events": [],
                "recent_completed_events": [],
            }
        ],
    )
    _write_jsonl(
        memory_dir / "actor_memory_updates.jsonl",
        [
            {
                "tick": 1,
                "timestamp": "2026-03-26T01:00:00",
                "revision": 1,
                "actor_id": 1,
                "actor_name": "Actor One",
                "event_id": "event_001_0001",
                "event_title": "Harbor Clash",
                "summary": "Actor One remembers Harbor Clash and wants payback.",
                "reason": "condition_fallout",
                "actor_memory": {
                    "agent_id": 1,
                    "entity_name": "Actor One",
                    "entity_type": "Character",
                    "public_role": "",
                    "home_location": "",
                    "standing_drives": ["Hold the harbor"],
                    "temperament": [],
                    "episodic_memories": [
                        {
                            "memory_id": "mem:event_001_0001:1",
                            "tick": 1,
                            "event_id": "event_001_0001",
                            "event_title": "Harbor Clash",
                            "summary": "Actor One remembers Harbor Clash and wants payback.",
                            "location": "Harbor",
                            "tags": ["battle"],
                            "counterpart_names": ["Actor Two"],
                            "significance": 5,
                            "valence": "harm",
                        }
                    ],
                    "open_loops": [
                        {
                            "loop_id": "loop:event_001_0001:1",
                            "summary": "Finish what Harbor Clash started.",
                            "status": "active",
                            "urgency": 5,
                            "created_tick": 1,
                            "last_updated_tick": 1,
                            "event_id": "event_001_0001",
                            "event_title": "Harbor Clash",
                            "location": "Harbor",
                            "tags": ["battle"],
                            "counterpart_names": ["Actor Two"],
                        }
                    ],
                    "relationship_tensions": [
                        {
                            "counterpart_id": 2,
                            "counterpart_name": "Actor Two",
                            "trust": 0,
                            "grievance": 3,
                            "last_updated_tick": 1,
                            "last_event_title": "Harbor Clash",
                            "summary": "Actor One now treats Actor Two as a hostile rival.",
                        }
                    ],
                    "last_updated_tick": 1,
                },
            },
            {
                "tick": 2,
                "timestamp": "2026-03-26T02:00:00",
                "revision": 2,
                "actor_id": 1,
                "actor_name": "Actor One",
                "event_id": "event_002_0001",
                "event_title": "Late Betrayal",
                "summary": "This later row should be ignored at tick 1.",
                "reason": "condition_fallout",
                "actor_memory": {
                    "agent_id": 1,
                    "entity_name": "Actor One",
                    "entity_type": "Character",
                    "public_role": "",
                    "home_location": "",
                    "standing_drives": ["Hold the harbor"],
                    "temperament": [],
                    "episodic_memories": [
                        {
                            "memory_id": "mem:event_002_0001:1",
                            "tick": 2,
                            "event_id": "event_002_0001",
                            "event_title": "Late Betrayal",
                            "summary": "This later row should be ignored at tick 1.",
                            "location": "Harbor",
                            "tags": ["betrayal"],
                            "counterpart_names": ["Actor Two"],
                            "significance": 4,
                            "valence": "harm",
                        }
                    ],
                    "open_loops": [],
                    "relationship_tensions": [],
                    "last_updated_tick": 2,
                },
            },
        ],
    )

    result = restore_world_checkpoint_from_logs(str(config_path), tick=1)
    checkpoint = json.loads((world_dir / "checkpoint.json").read_text(encoding="utf-8"))
    restored_memory_state = json.loads((memory_dir / "actor_memory_state.json").read_text(encoding="utf-8"))

    assert result["written"] is True
    assert checkpoint["actor_memory_state"]["actors"]["1"]["episodic_memories"][0]["event_title"] == "Harbor Clash"
    assert checkpoint["actor_memory_state"]["revision"] == 1
    assert restored_memory_state["actors"]["1"]["episodic_memories"][0]["event_title"] == "Harbor Clash"


def test_restore_world_checkpoint_preserves_dynamic_agents_from_snapshot(tmp_path):
    simulation_dir = tmp_path / "sim_restore_dynamic"
    world_dir = simulation_dir / "world"
    world_dir.mkdir(parents=True)
    config_path = _write_config(simulation_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["agent_configs"] = [
        {"agent_id": 1, "entity_name": "Faction One", "entity_type": "Faction"},
        {"agent_id": 2, "entity_name": "Faction Two", "entity_type": "Faction"},
    ]
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_jsonl(
        world_dir / "actions.jsonl",
        [
            {"event_type": "simulation_start", "total_rounds": 6},
        ],
    )
    _write_jsonl(
        world_dir / "state_snapshots.jsonl",
        [
            {
                "tick": 1,
                "round": 1,
                "phase": "tick_complete",
                "summary": "tick1",
                "simulated_hours": 1.0,
                "world_state": {
                    "tension": 0.58,
                    "stability": 0.44,
                    "momentum": 0.51,
                    "pressure_tracks": {"conflict": 0.4},
                    "focus_threads": ["Restore Front"],
                    "last_tick_summary": "tick1",
                    "runtime_cast": {
                        "schema_version": 1,
                        "next_agent_id": 4,
                        "dynamic_agents": [
                            {
                                "agent_id": 3,
                                "entity_name": "Harbor Clerk",
                                "entity_type": "Character",
                                "public_role": "Harbor Clerk is a newly promoted archive courier.",
                                "driving_goals": ["protect Faction One leverage", "survive the current escalation"],
                                "resources": ["faction: Faction One", "runtime_source: observed_participant"],
                                "constraints": ["newly promoted under pressure"],
                                "connected_entities": ["Faction One"],
                                "story_hooks": ["Archive Lockdown"],
                                "home_location": "Harbor Archive",
                                "summary": "Harbor Clerk is pushed to the front line after repeated archive crises.",
                                "runtime_origin": "observed_participant",
                                "runtime_affiliation": "Faction One",
                                "runtime_seat_label": "archive courier",
                                "runtime_created_tick": 1,
                            }
                        ],
                        "promotion_history": [
                            {
                                "tick": 1,
                                "agent_id": 3,
                                "entity_name": "Harbor Clerk",
                            }
                        ],
                    },
                    "actor_conditions": {
                        "3": {
                            "agent_id": 3,
                            "entity_name": "Harbor Clerk",
                            "entity_type": "Character",
                            "status": "healthy",
                            "injury_score": 0,
                            "alive": True,
                            "availability": "active",
                            "last_updated_tick": 1,
                            "last_event_id": "",
                            "last_event_title": "",
                            "latest_note": "",
                        }
                    },
                    "actor_condition_summary": {
                        "healthy": 1,
                        "shaken": 0,
                        "wounded": 0,
                        "critical": 0,
                        "incapacitated": 0,
                        "dead": 0,
                        "active": 1,
                    },
                    "recent_condition_updates": [],
                },
                "metrics": {
                    "intents_created": 0,
                    "accepted_events": 0,
                    "deferred_intents": 0,
                    "rejected_intents": 0,
                    "active_events_count": 0,
                    "queued_events_count": 0,
                    "completed_events_count": 0,
                },
                "active_events": [],
                "queued_events": [],
                "recent_completed_events": [],
            }
        ],
    )

    result = restore_world_checkpoint_from_logs(str(config_path), tick=1)
    checkpoint = json.loads((world_dir / "checkpoint.json").read_text(encoding="utf-8"))

    assert result["written"] is True
    assert checkpoint["world_state"]["runtime_cast"]["dynamic_agents"][0]["entity_name"] == "Harbor Clerk"
    assert checkpoint["world_state"]["actor_conditions"]["3"]["status"] == "healthy"
    assert checkpoint["last_snapshot"]["world_state"]["runtime_cast"]["dynamic_agents"][0]["entity_name"] == "Harbor Clerk"
