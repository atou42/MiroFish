import json
from pathlib import Path

from app.config import Config
from app.models.project import ProjectManager
from app.services.simulation_manager import SimulationManager
from app.services.world_config_generator import WorldConfigGenerator
from app.services.world_pack_compiler import WorldPackCompiler
from app.services.world_reading_surface import generate_reading_surface
from scripts import run_world_pipeline
from scripts.world_run_diagnostics import summarize, validate_summary


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _configure_tmp_storage(tmp_path: Path, monkeypatch) -> Path:
    uploads_dir = tmp_path / "uploads"
    simulations_dir = uploads_dir / "simulations"
    projects_dir = uploads_dir / "projects"
    uploads_dir.mkdir()
    simulations_dir.mkdir()
    projects_dir.mkdir()
    monkeypatch.setattr(Config, "UPLOAD_FOLDER", str(uploads_dir))
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(simulations_dir))
    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(simulations_dir))
    monkeypatch.setattr(WorldConfigGenerator, "_get_llm", lambda self: None)
    return uploads_dir


def test_world_pack_compiler_bootstraps_runnable_simulation(tmp_path, monkeypatch):
    uploads_dir = _configure_tmp_storage(tmp_path, monkeypatch)
    source_dir = tmp_path / "one_piece_pack"
    import_dir = source_dir / "mirofish_import"
    import_dir.mkdir(parents=True)

    (import_dir / "01_世界种子.md").write_text(
        "\n".join(
            [
                "# 海雾世界种子",
                "",
                "## 势力实体（Faction）",
                "### 蓝潮联盟",
                "- 类型：Faction",
                "- 摘要：掌控北海航线和贸易护航。",
                "",
                "### 灰港执政团",
                "- 类型：Faction",
                "- 摘要：依靠港口税和情报网络维持统治。",
                "",
                "## 地点实体（Place）",
                "### 灰港",
                "- 类型：Place",
                "- 描述：所有主线贸易和暗市交易的汇合点。",
            ]
        ),
        encoding="utf-8",
    )
    (import_dir / "02_核心角色卡.md").write_text(
        "\n".join(
            [
                "# 核心角色卡",
                "",
                "## 阵营：蓝潮联盟",
                "### 艾拉",
                "- 类型：Character",
                "- 阵营：蓝潮联盟",
                "- 摘要：年轻舰队指挥官，急于把灰港纳入联盟保护网。",
                "",
                "## 阵营：灰港执政团",
                "### 莫恩",
                "- 类型：Character",
                "- 阵营：灰港执政团",
                "- 摘要：港务总督，擅长拖延谈判并操控黑市代理人。",
            ]
        ),
        encoding="utf-8",
    )
    _write_json(
        source_dir / "sources.json",
        [{"title": "dummy", "used_for": "metadata should not become entities"}],
    )

    compiler = WorldPackCompiler()
    manifest = compiler.compile(
        source_dir=str(source_dir),
        simulation_id="sim_pack_test",
        pack_title="海雾世界",
        use_llm_for_profiles=False,
    )

    sim_dir = uploads_dir / "simulations" / "sim_pack_test"
    config = json.loads((sim_dir / "simulation_config.json").read_text(encoding="utf-8"))
    state = json.loads((sim_dir / "state.json").read_text(encoding="utf-8"))
    project_id = manifest["project_id"]
    project = json.loads((uploads_dir / "projects" / project_id / "project.json").read_text(encoding="utf-8"))
    compiled_actors = json.loads((sim_dir / "world_pack" / "compiled_actor_entities.json").read_text(encoding="utf-8"))
    compiled_entities = json.loads((sim_dir / "world_pack" / "compiled_entities.json").read_text(encoding="utf-8"))

    assert manifest["simulation_id"] == "sim_pack_test"
    assert config["simulation_mode"] == "world"
    assert config["world_pack"]["title"] == "海雾世界"
    assert state["status"] == "ready"
    assert state["config_generated"] is True
    assert project["graph_id"].startswith("world_pack_")
    assert (uploads_dir / "projects" / project_id / "extracted_text.txt").exists()
    assert {item["name"] for item in compiled_actors} >= {"艾拉", "莫恩", "蓝潮联盟", "灰港执政团"}
    assert "dummy" not in {item["name"] for item in compiled_entities}


def test_world_reading_surface_and_diagnostics_generate_artifacts(tmp_path, monkeypatch):
    uploads_dir = _configure_tmp_storage(tmp_path, monkeypatch)
    sim_dir = uploads_dir / "simulations" / "sim_reading"
    world_dir = sim_dir / "world"
    world_dir.mkdir(parents=True)

    _write_json(
        sim_dir / "simulation_config.json",
        {
            "simulation_id": "sim_reading",
            "simulation_mode": "world",
            "agent_configs": [
                {"agent_id": 1, "entity_name": "蓝潮联盟", "entity_type": "Faction"},
                {"agent_id": 2, "entity_name": "灰港执政团", "entity_type": "Faction"},
            ],
            "runtime_config": {},
        },
    )
    _write_json(
        world_dir / "checkpoint.json",
        {
            "status": "completed",
            "last_completed_tick": 2,
            "run_total_rounds": 2,
            "actor_selection_counts": {"1": 2, "2": 1},
            "actor_event_counts": {"1": 3, "2": 2},
            "world_state": {
                "tension": 0.84,
                "stability": 0.28,
                "momentum": 0.9,
                "pressure_tracks": {"conflict": 0.91, "scarcity": 0.52},
            },
            "active_events": [
                {
                    "event_id": "event_2_1",
                    "title": "蓝潮封锁外港",
                    "summary": "联盟开始对外港执行选择性通行。",
                    "primary_agent_id": 1,
                    "primary_agent_name": "蓝潮联盟",
                    "priority": 4,
                    "duration_ticks": 2,
                    "status": "active",
                    "location": "灰港外海",
                    "dependencies": [],
                    "state_impacts": {"conflict": 0.2, "stability": -0.1},
                }
            ],
            "queued_events": [],
        },
    )
    _write_jsonl(
        world_dir / "actions.jsonl",
        [
            {
                "event_type": "simulation_start",
                "runtime_config": {},
            },
            {
                "event_type": "intent_created",
                "tick": 1,
                "agent_id": 1,
                "agent_name": "蓝潮联盟",
                "title": "控制外港",
                "summary": "要求商船接受临检。",
                "action_args": {"intent_id": "intent_1"},
            },
            {
                "event_type": "intent_resolved",
                "tick": 1,
                "agent_id": 1,
                "agent_name": "蓝潮联盟",
                "title": "蓝潮封锁外港",
                "summary": "联盟开始对外港执行选择性通行。",
                "status": "accepted",
                "event_id": "event_1_1",
                "action_args": {"intent_id": "intent_1"},
            },
            {
                "event_type": "tick_end",
                "tick": 1,
                "scene_title": "港口升温",
                "summary": "第1轮结束，蓝潮联盟先手试探。",
                "world_state": {"tension": 0.7, "stability": 0.4, "momentum": 0.6},
            },
            {
                "event_type": "tick_end",
                "tick": 1,
                "scene_title": "港口升温",
                "summary": "第1轮结束，蓝潮联盟先手试探。",
                "world_state": {"tension": 0.7, "stability": 0.4, "momentum": 0.6},
            },
            {
                "event_type": "event_completed",
                "tick": 2,
                "agent_id": 2,
                "agent_name": "灰港执政团",
                "title": "灰港黑市反制",
                "summary": "执政团通过黑市中间人绕开封锁。",
            },
            {
                "event_type": "resolver_zero_accept_diagnostic",
                "tick": 2,
                "reason_code": "low_confidence",
                "summary": "resolver hesitated",
            },
            {
                "event_type": "simulation_end",
                "tick": 2,
                "unresolved_events": [
                    {
                        "event_id": "event_2_1",
                        "title": "蓝潮封锁外港",
                        "summary": "联盟开始对外港执行选择性通行。",
                        "primary_agent_name": "蓝潮联盟",
                        "priority": 4,
                        "duration_ticks": 2,
                        "status": "active",
                        "location": "灰港外海",
                        "dependencies": [],
                        "state_impacts": {"conflict": 0.2, "stability": -0.1},
                    }
                ],
            },
        ],
    )
    _write_jsonl(
        world_dir / "state_snapshots.jsonl",
        [
            {
                "tick": 1,
                "round": 1,
                "scene_title": "港口升温",
                "summary": "第1轮结束，蓝潮联盟先手试探。",
                "world_state": {"tension": 0.7, "stability": 0.4, "momentum": 0.6, "pressure_tracks": {"conflict": 0.7}},
                "recent_completed_events": [],
                "metrics": {},
            },
            {
                "tick": 1,
                "round": 1,
                "scene_title": "港口升温",
                "summary": "第1轮结束，蓝潮联盟先手试探。",
                "world_state": {"tension": 0.7, "stability": 0.4, "momentum": 0.6, "pressure_tracks": {"conflict": 0.7}},
                "recent_completed_events": [],
                "metrics": {},
            },
            {
                "tick": 2,
                "round": 2,
                "scene_title": "黑市回潮",
                "summary": "第2轮结束，执政团借黑市打开缺口。",
                "world_state": {"tension": 0.84, "stability": 0.28, "momentum": 0.9, "pressure_tracks": {"conflict": 0.91}},
                "recent_completed_events": [{"title": "灰港黑市反制"}],
                "metrics": {},
            },
        ],
    )

    reading_surface = generate_reading_surface("sim_reading", "stage01")
    chronicle = json.loads(Path(reading_surface["chronicle"]["json_path"]).read_text(encoding="utf-8"))
    actor_board = json.loads(Path(reading_surface["actor_board"]["json_path"]).read_text(encoding="utf-8"))
    risk_digest = json.loads(Path(reading_surface["risk_digest"]["json_path"]).read_text(encoding="utf-8"))

    assert reading_surface["chronicle"]["tick_count"] == 2
    assert chronicle[0]["tick"] == 1
    assert actor_board["actors"][0]["agent_name"] == "蓝潮联盟"
    assert actor_board["actors"][0]["selections"] == 2
    assert risk_digest["items"][0]["title"] == "蓝潮封锁外港"

    summary = summarize("sim_reading", "stage01")
    assert not validate_summary(summary)
    assert Path(summary["reading_surface"]["chronicle"]["markdown_path"]).exists()


def test_world_pipeline_runs_and_resumes_between_stages(tmp_path, monkeypatch):
    uploads_dir = _configure_tmp_storage(tmp_path, monkeypatch)
    sim_dir = uploads_dir / "simulations" / "sim_pipeline"
    world_dir = sim_dir / "world"
    world_dir.mkdir(parents=True)
    _write_json(
        sim_dir / "simulation_config.json",
        {
            "simulation_id": "sim_pipeline",
            "simulation_mode": "world",
            "time_config": {"total_ticks": 16, "minutes_per_round": 60},
            "agent_configs": [],
            "runtime_config": {},
        },
    )

    command_names = []

    def fake_run_command(cmd, cwd, **kwargs):
        command_names.append(cmd[2])
        target_rounds = int(cmd[cmd.index("--max-rounds") + 1])
        _write_json(
            world_dir / "checkpoint.json",
            {
                "status": "completed",
                "last_completed_tick": target_rounds,
                "run_total_rounds": target_rounds,
            },
        )
        return {"command": cmd, "cwd": cwd, "returncode": 0}

    def fake_summarize(simulation_id, label):
        base_dir = world_dir / "diagnostics"
        base_dir.mkdir(parents=True, exist_ok=True)
        json_path = base_dir / f"{label}.json"
        md_path = base_dir / f"{label}.md"
        chronicle_json = base_dir / f"{label}-chronicle.json"
        chronicle_md = base_dir / f"{label}-chronicle.md"
        actor_json = base_dir / f"{label}-actor-board.json"
        actor_md = base_dir / f"{label}-actor-board.md"
        risk_json = base_dir / f"{label}-risk-digest.json"
        risk_md = base_dir / f"{label}-risk-digest.md"
        for path in [json_path, md_path, chronicle_json, chronicle_md, actor_json, actor_md, risk_json, risk_md]:
            path.write_text("{}" if path.suffix == ".json" else "# ok\n", encoding="utf-8")
        return {
            "simulation_id": simulation_id,
            "label": label,
            "json_path": str(json_path),
            "markdown_path": str(md_path),
            "actions_rows": 1,
            "checkpoint": {"status": "completed", "last_completed_tick": 4},
            "diagnostics": {"last_tick_end": {"tick": 1}, "world_state": {"tension": 0.5}},
            "reading_surface": {
                "chronicle": {"json_path": str(chronicle_json), "markdown_path": str(chronicle_md)},
                "actor_board": {"json_path": str(actor_json), "markdown_path": str(actor_md)},
                "risk_digest": {"json_path": str(risk_json), "markdown_path": str(risk_md)},
            },
        }

    monkeypatch.setattr(run_world_pipeline, "_run_command", fake_run_command)
    monkeypatch.setattr(run_world_pipeline, "summarize_world_run", fake_summarize)
    monkeypatch.setattr(run_world_pipeline, "validate_diagnostics_or_raise", lambda summary: summary)
    monkeypatch.setattr(
        run_world_pipeline,
        "generate_world_report_with_fallback",
        lambda **kwargs: {"report_id": f"report-{kwargs['label']}", "report_status": "completed"},
    )

    manifest = run_world_pipeline.run_world_pipeline(
        simulation_id="sim_pipeline",
        stage_rounds=[4, 8],
    )

    assert command_names == ["run", "resume"]
    assert len(manifest["stages"]) == 2
    assert manifest["stages"][0]["label"] == "stage01_r4"
    assert manifest["stages"][1]["label"] == "stage02_r8"
