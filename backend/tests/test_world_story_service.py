import json
from pathlib import Path

from flask import Flask

from app.api import simulation_bp
from app.config import Config
from app.services.report_agent import ReportManager
from app.services.world_story_service import WorldStoryService


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _setup_simulation_root(tmp_path: Path, monkeypatch, simulation_id: str) -> Path:
    uploads_dir = tmp_path / "uploads"
    (uploads_dir / "simulations").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Config, "UPLOAD_FOLDER", str(uploads_dir))
    return uploads_dir / "simulations" / simulation_id


def _write_minimal_world_artifacts(sim_dir: Path, simulation_id: str) -> None:
    _write_json(
        sim_dir / "simulation_config.json",
        {
            "simulation_id": simulation_id,
            "simulation_mode": "world",
            "simulation_requirement": "Test requirement: a world that keeps moving.",
            "world_pack": {"title": "Test World Pack"},
            "runtime_config": {},
        },
    )

    world_dir = sim_dir / "world"
    world_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        world_dir / "checkpoint.json",
        {
            "status": "running",
            "last_completed_tick": 0,
            "run_total_rounds": 8,
        },
    )


def test_world_story_service_builds_payload_from_minimal_artifacts(tmp_path, monkeypatch):
    sim_dir = _setup_simulation_root(tmp_path, monkeypatch, simulation_id="sim_min_story")
    sim_dir.mkdir(parents=True, exist_ok=True)
    _write_minimal_world_artifacts(sim_dir, simulation_id="sim_min_story")

    monkeypatch.setattr(ReportManager, "get_report_by_simulation", lambda *_args, **_kwargs: None)

    payload = WorldStoryService.build_story_payload("sim_min_story")

    assert payload["simulation_id"] == "sim_min_story"
    assert isinstance(payload["hero"], dict)
    assert isinstance(payload["episodes"], list)
    assert isinstance(payload["factions"], dict)
    assert isinstance(payload["risks"], dict)
    assert isinstance(payload["process"], list)
    assert isinstance(payload["meta"], dict)

    assert payload["meta"]["simulation_title"] == "Test World Pack"
    assert isinstance(payload["meta"]["ticks"], int)
    assert isinstance(payload["meta"]["status"], str)

    metrics = payload["hero"]["metrics"]
    assert isinstance(metrics, list)
    assert len(metrics) == 3
    for metric in metrics:
        assert "label" in metric
        assert "value" in metric
        assert "tone" in metric


def test_world_story_service_builds_payload_with_diagnostics_artifacts(tmp_path, monkeypatch):
    simulation_id = "sim_story_full"
    sim_dir = _setup_simulation_root(tmp_path, monkeypatch, simulation_id=simulation_id)
    sim_dir.mkdir(parents=True, exist_ok=True)
    _write_minimal_world_artifacts(sim_dir, simulation_id=simulation_id)

    monkeypatch.setattr(ReportManager, "get_report_by_simulation", lambda *_args, **_kwargs: None)

    diagnostics_dir = sim_dir / "world" / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        diagnostics_dir / "2026-03-21-summary.json",
        {
            "diagnostics": {
                "world_state": {
                    "last_tick_summary": "Tick 2 ends with tension rising and stability collapsing.",
                }
            }
        },
    )
    _write_json(
        diagnostics_dir / "2026-03-21-chronicle.json",
        [
            {
                "tick": 1,
                "scene_title": "Harbor heats up",
                "summary": "A first move that forces the port to choose a side.",
                "world_state": {"tension": 0.62, "stability": 0.44, "momentum": 0.51},
                "accepted_titles": ["Blockade outer harbor"],
                "started_titles": ["Smugglers route probe"],
                "completed_titles": [],
                "key_actors": ["Blue Tide Alliance"],
            },
            {
                "tick": 2,
                "scene_title": "Market cracks",
                "summary": "The ruling council pushes back with a black market counter.",
                "world_state": {"tension": 0.74, "stability": 0.31, "momentum": 0.67},
                "accepted_titles": ["Black market counter"],
                "started_titles": [],
                "completed_titles": ["Blockade outer harbor"],
                "key_actors": ["Grayport Council"],
            },
        ],
    )
    _write_json(
        diagnostics_dir / "2026-03-21-actor-board.json",
        {
            "actors": [
                {
                    "agent_name": "Blue Tide Alliance",
                    "entity_type": "Faction",
                    "activity_score": 9,
                    "selections": 2,
                    "event_count": 3,
                    "accepted_events": 2,
                    "completed_events": 1,
                    "latest_move": "Press blockade to force compliance.",
                    "active_events": ["Blockade outer harbor"],
                    "queued_event_titles": ["Escort convoy"],
                },
                {
                    "agent_name": "Grayport Council",
                    "entity_type": "Faction",
                    "activity_score": 7,
                    "selections": 2,
                    "event_count": 2,
                    "accepted_events": 1,
                    "completed_events": 0,
                    "latest_move": "Activate black market intermediaries.",
                    "active_events": ["Black market counter"],
                    "queued_event_titles": [],
                },
            ]
        },
    )
    _write_json(
        diagnostics_dir / "2026-03-21-risk-digest.json",
        {
            "items": [
                {
                    "category": "conflict",
                    "title": "Portside skirmish",
                    "summary": "A minor clash could spiral into full blockade warfare.",
                    "severity": 0.86,
                    "owner": "Blue Tide Alliance",
                    "status": "active",
                },
                {
                    "category": "scarcity",
                    "title": "Food price shock",
                    "summary": "Supplies shrink as routes move underground.",
                    "severity": 0.62,
                    "owner": "Grayport Council",
                    "status": "warning",
                },
            ]
        },
    )

    payload = WorldStoryService.build_story_payload(simulation_id)

    assert payload["meta"]["ticks"] >= 0
    assert payload["hero"]["headline"]
    assert payload["hero"]["subtitle"]
    assert len(payload["episodes"]) >= 1
    assert len(payload["factions"]["primary"]) == 2
    assert len(payload["risks"]["items"]) == 2

    episode = payload["episodes"][0]
    assert episode["tick_start"] == 1
    assert episode["tick_end"] == 2
    assert isinstance(episode["turning_points"], list)
    assert isinstance(episode["world_shift"], dict)


def test_world_story_route_returns_success_shape(tmp_path, monkeypatch):
    simulation_id = "sim_story_api"
    sim_dir = _setup_simulation_root(tmp_path, monkeypatch, simulation_id=simulation_id)
    sim_dir.mkdir(parents=True, exist_ok=True)
    _write_minimal_world_artifacts(sim_dir, simulation_id=simulation_id)

    monkeypatch.setattr(ReportManager, "get_report_by_simulation", lambda *_args, **_kwargs: None)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(simulation_bp, url_prefix="/api/simulation")

    client = app.test_client()
    resp = client.get(f"/api/simulation/{simulation_id}/world-story")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["data"]["simulation_id"] == simulation_id
    assert "hero" in data["data"]
    assert "episodes" in data["data"]
    assert "meta" in data["data"]

