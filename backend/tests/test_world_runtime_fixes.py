import json
from pathlib import Path

from app.config import Config
from app.services.simulation_runner import RunnerStatus, SimulationRunState, SimulationRunner
from app.services.world_report_agent import WorldReportAgent
from scripts.generate_world_report import validate_report_artifacts
from scripts import run_world_simulation
from scripts.run_world_simulation import ActorIntent, WorldSimulationRuntime
from scripts import world_run


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_runtime_config(tmp_path: Path) -> Path:
    simulation_dir = tmp_path / "sim_runtime_fix"
    simulation_dir.mkdir()
    config_path = simulation_dir / "simulation_config.json"
    config = {
        "simulation_id": "sim_runtime_fix",
        "simulation_mode": "world",
        "time_config": {
            "total_ticks": 6,
            "minutes_per_round": 60,
        },
        "agent_configs": [
            {
                "agent_id": 1,
                "entity_name": "Actor One",
                "entity_type": "faction",
                "home_location": "Harbor",
            }
        ],
        "plot_threads": [{"title": "Runtime Front"}],
        "pressure_tracks": [
            {"name": "conflict", "starting_level": 0.4},
            {"name": "scarcity", "starting_level": 0.3},
            {"name": "legitimacy", "starting_level": 0.5},
        ],
        "initial_world_state": {"starting_condition": "runtime regression"},
        "runtime_config": {},
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def _build_runtime(tmp_path: Path, monkeypatch) -> WorldSimulationRuntime:
    monkeypatch.setattr(WorldSimulationRuntime, "_build_llm", lambda self, selector=None: None)
    monkeypatch.setattr(WorldSimulationRuntime, "_get_actor_llm", lambda self, agent=None: None)
    return WorldSimulationRuntime(config_path=str(_write_runtime_config(tmp_path)), max_rounds=1)


def test_supervised_world_run_auto_resumes_until_target(monkeypatch):
    calls = []
    statuses = iter(
        [
            {
                "checkpoint": {
                    "status": "running",
                    "last_completed_tick": 4,
                    "run_total_rounds": 8,
                }
            },
            {
                "checkpoint": {
                    "status": "completed",
                    "last_completed_tick": 8,
                    "run_total_rounds": 8,
                }
            },
        ]
    )

    monkeypatch.setattr(world_run, "_resolve_target_rounds", lambda *args, **kwargs: 8)

    def fake_run_once(config_path, *, max_rounds=None, resume_from_checkpoint=False):
        calls.append(resume_from_checkpoint)
        return {
            "command": ["python", "scripts/run_world_simulation.py"],
            "started_at": "t1",
            "completed_at": "t2",
            "returncode": 0,
        }

    monkeypatch.setattr(world_run, "_run_world_process_once", fake_run_once)
    monkeypatch.setattr(world_run, "_status", lambda config_path: next(statuses))

    result = world_run._run_world_process(
        "/tmp/sim_runtime_fix.json",
        max_rounds=8,
        resume_from_checkpoint=False,
        max_resume_attempts=2,
    )

    assert calls == [False, True]
    assert result["auto_resumed"] is True
    assert result["checkpoint"]["last_completed_tick"] == 8


def test_resume_from_completed_checkpoint_preserves_extended_target(tmp_path, monkeypatch):
    config_path = _write_runtime_config(tmp_path)
    world_dir = config_path.parent / "world"
    world_dir.mkdir(parents=True, exist_ok=True)
    (world_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "saved_at": "2026-03-22T00:07:17.313225",
                "status": "completed",
                "terminal_status": "completed",
                "stop_reason": "target_rounds_reached",
                "simulation_id": "sim_runtime_fix",
                "config_path": str(config_path),
                "last_completed_tick": 4,
                "run_total_rounds": 4,
                "target_rounds": 4,
                "minutes_per_round": 60,
                "active_events": [],
                "queued_events": [],
                "completed_events": [],
                "world_state": {
                    "tension": 0.8,
                    "stability": 0.2,
                    "momentum": 0.9,
                    "pressure_tracks": {
                        "conflict": 0.9,
                        "scarcity": 0.7,
                        "legitimacy": 0.2,
                    },
                    "last_tick_summary": "Tick 4 完成。",
                },
                "last_snapshot": {
                    "tick": 4,
                    "summary": "Tick 4 完成。",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(WorldSimulationRuntime, "_build_llm", lambda self, selector=None: None)
    monkeypatch.setattr(WorldSimulationRuntime, "_get_actor_llm", lambda self, agent=None: None)

    runtime = WorldSimulationRuntime(
        config_path=str(config_path),
        max_rounds=8,
        resume_from_checkpoint=True,
    )

    assert runtime.last_completed_tick == 4
    assert runtime.target_rounds == 8
    assert runtime.total_rounds == 8
    assert runtime.stop_reason == ""
    assert runtime.terminal_status == ""


def test_write_checkpoint_uses_atomic_replace(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)
    replace_calls = []
    real_replace = run_world_simulation.os.replace

    def record_replace(src, dst):
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(run_world_simulation.os, "replace", record_replace)

    runtime._write_checkpoint(status="completed", stop_reason="target_rounds_reached")

    payload = json.loads(Path(runtime.checkpoint_path).read_text(encoding="utf-8"))
    assert replace_calls
    assert replace_calls[0][1] == runtime.checkpoint_path
    assert payload["status"] == "completed"
    assert payload["stop_reason"] == "target_rounds_reached"


def test_invalid_json_intent_prefers_structured_recovery(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)

    intent = runtime._intent_from_invalid_json_output(
        tick=1,
        scene_title="Runtime Front",
        agent={
            "agent_id": 1,
            "entity_name": "Actor One",
            "entity_type": "faction",
            "home_location": "Harbor",
        },
        raw_text='analysis {"objective":"Secure Harbor","summary":"Send patrols to lock down the harbor entrance.","location":"Harbor","priority":4}',
        repaired_text="",
    )

    assert intent is not None
    assert intent.source == "llm_invalid_json_recovered"
    assert "Secure Harbor" in intent.objective


def test_invalid_json_intent_partial_recovery_preserves_structured_fields(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)
    runtime.active_events["event_238_0620"] = None

    intent = runtime._intent_from_invalid_json_output(
        tick=1,
        scene_title="Runtime Front",
        agent={
            "agent_id": 1,
            "entity_name": "Actor One",
            "entity_type": "faction",
            "home_location": "Harbor",
        },
        raw_text=(
            '{'
            '"objective":"Intercept Certified Ships",'
            '"summary":"Dispatch the Seventh Fleet to stop three certified merchant ships and force a log inspection.",'
            '"location":"Alpha-9 edge",'
            '"target":"Certified merchant convoy",'
            '"desired_duration":2,'
            '"priority":5,'
            '"urgency":4,'
            '"risk_level":4,'
            '"dependencies":["event_238_0620"],'
            '"participants":["Navy HQ","Seventh Fleet","Merchant Convoy"],'
            '"tags":["lawfare","interception"],'
            '"state_impacts":{"conflict":0.15,"legitimacy":0.1,"stability":-0.1},'
            '"rationale":"If the new decree is not enforced immediately'
        ),
        repaired_text="",
    )

    assert intent is not None
    assert intent.source == "llm_invalid_json_partial_recovered"
    assert intent.desired_duration == 2
    assert intent.priority == 5
    assert intent.urgency == 4
    assert intent.risk_level == 4
    assert intent.dependencies == ["event_238_0620"]
    assert "Seventh Fleet" in intent.participants
    assert "lawfare" in intent.tags
    assert intent.state_impacts["conflict"] == 0.15
    assert intent.state_impacts["stability"] == -0.1


def test_summary_only_bootstrap_uses_accept_count_hint(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)
    intents = [
        ActorIntent(
            intent_id="intent_a",
            tick=1,
            agent_id=1,
            agent_name="Actor One",
            objective="Secure Harbor",
            summary="Lock down the harbor",
            priority=5,
            urgency=4,
            risk_level=4,
        ),
        ActorIntent(
            intent_id="intent_b",
            tick=1,
            agent_id=2,
            agent_name="Actor Two",
            objective="Cut Supply",
            summary="Disrupt the enemy supply line",
            priority=4,
            urgency=4,
            risk_level=3,
        ),
        ActorIntent(
            intent_id="intent_c",
            tick=1,
            agent_id=3,
            agent_name="Actor Three",
            objective="Spread Rumors",
            summary="Drive panic across nearby islands",
            priority=2,
            urgency=2,
            risk_level=2,
        ),
    ]

    result = runtime._parse_resolver_response(
        tick=1,
        intents=intents,
        response={
            "summary": "第 1 轮结束：生成 3 个角色意图，其中 2 个被整理为事件，当前世界紧张度上升。",
        },
        source="llm_invalid_json_recovered",
    )

    assert len(result["accepted_events"]) == 2
    assert result["diagnostics"]["reason_code"] == "resolver_summary_bootstrapped"


def test_resolved_event_preserves_explicit_long_title(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)
    long_title = (
        "完成最终撤离并切断海军通讯链路，整体转入地下潜伏"
        "（增补：在二级深海安全屋近域启用非发射式被动信号遮蔽与签名压低，"
        "并在多个安全屋之间执行错峰热源管理、吸波隔离层轮换、低功率监听板熄火、"
        "可追溯介质再清洗，以及分层撤离路线的最终脱敏校验）"
    )
    assert len(long_title) > 96

    primary_intent = ActorIntent(
        intent_id="intent_a",
        tick=1,
        agent_id=1,
        agent_name="Actor One",
        objective="Secure Harbor",
        summary="Lock down the harbor",
        desired_duration=2,
        priority=4,
        urgency=4,
        risk_level=3,
        source="llm",
    )

    event, drop_reason = runtime._build_event_from_resolved_item(
        tick=1,
        item={
            "owner_intent_id": "intent_a",
            "title": long_title,
            "summary": (
                "该意图与既有撤离收口线和静默校验线高度连续，不新开事件，"
                "直接并入 queued 事件 event_1。"
            ),
            "duration_ticks": 2,
            "priority": 5,
        },
        intent_map={primary_intent.intent_id: primary_intent},
        source="llm",
    )

    assert drop_reason == ""
    assert event is not None
    assert event.title == long_title[:180]
    assert event.title != event.summary
    assert event.summary.startswith("该意图与既有撤离收口线")


def test_world_report_fallback_uses_late_run_context(tmp_path, monkeypatch):
    upload_root = tmp_path / "uploads"
    sim_dir = upload_root / "simulations" / "sim_report_fix" / "world"
    sim_dir.mkdir(parents=True)

    monkeypatch.setattr(Config, "UPLOAD_FOLDER", str(upload_root))

    config = {
        "simulation_id": "sim_report_fix",
        "simulation_mode": "world",
        "initial_world_state": {"starting_condition": "A world after a major broadcast shock."},
    }
    (upload_root / "simulations" / "sim_report_fix" / "simulation_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (sim_dir / "world_state.json").write_text(
        json.dumps(
            {
                "tension": 0.91,
                "stability": 0.42,
                "momentum": 0.88,
                "focus_threads": ["海军高压执行与内部裂缝", "四皇与十字公会争夺真空地带"],
                "last_tick_summary": "Tick 8 结束，海军与四皇在 Alpha-9 进入贴身对峙。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        sim_dir / "actions.jsonl",
        [
            {
                "action_type": "EVENT_COMPLETED",
                "round": 1,
                "agent_name": "世界政府",
                "title": "封锁广播残余节点",
                "summary": "世界政府抢先封口。",
            },
            {
                "event_type": "tick_end",
                "tick": 1,
                "round": 1,
                "summary": "Tick 1 开局以封锁和试探为主。",
            },
            {
                "action_type": "EVENT_COMPLETED",
                "round": 8,
                "agent_name": "四皇",
                "title": "贴身护航商船",
                "summary": "红发阵营强推庇护航线。",
            },
            {
                "event_type": "tick_end",
                "tick": 8,
                "round": 8,
                "summary": "Tick 8 时海军与四皇的法理战转为近距对峙。",
                "active_events_count": 1,
                "queued_events_count": 2,
            },
            {
                "event_type": "simulation_end",
                "total_rounds": 8,
                "active_events_count": 1,
                "queued_events_count": 2,
                "completed_events_count": 9,
                "unresolved_events": [
                    {
                        "title": "CP-0 潜入交易所窃取名单",
                        "summary": "若成功将建立秘密打击链。",
                    }
                ],
            },
        ],
    )
    _write_jsonl(
        sim_dir / "state_snapshots.jsonl",
        [
            {"tick": 1, "summary": "Tick 1 开局以封锁和试探为主。", "metrics": {"completed_events_count": 1}},
            {"tick": 8, "summary": "Tick 8 时海军与四皇的法理战转为近距对峙。", "metrics": {"completed_events_count": 9}},
        ],
    )

    agent = WorldReportAgent(
        graph_id="graph_x",
        simulation_id="sim_report_fix",
        simulation_requirement="推进这个世界",
        enable_llm=False,
    )
    context = agent._load_world_context()
    trajectory = agent._build_section_content("推进轨迹与事件链", context)
    risks = agent._build_section_content("后续风险与可操作建议", context)

    assert "Tick 8" in trajectory
    assert "未收束风险" in risks
    assert trajectory != risks


def test_validate_report_artifacts_flags_duplicate_sections(tmp_path):
    report_dir = tmp_path / "report_dup"
    report_dir.mkdir()
    (report_dir / "meta.json").write_text(
        json.dumps({"status": "completed"}, ensure_ascii=False),
        encoding="utf-8",
    )
    duplicate_content = "## A\n\nSame body"
    (report_dir / "outline.json").write_text(
        json.dumps(
            {
                "sections": [
                    {"title": "A", "content": duplicate_content},
                    {"title": "B", "content": duplicate_content},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (report_dir / "progress.json").write_text(
        json.dumps({"status": "completed"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (report_dir / "full_report.md").write_text("## A\n\nSame body\n\n## B\n\nSame body\n", encoding="utf-8")
    (report_dir / "section_01.md").write_text("## A\n\nSame body\n", encoding="utf-8")
    (report_dir / "section_02.md").write_text("## B\n\nSame body\n", encoding="utf-8")

    errors = validate_report_artifacts("report_dup", str(report_dir))

    assert "report sections appear duplicated" in errors


def test_world_exit_without_terminal_marker_is_failed(tmp_path, monkeypatch):
    sim_dir = tmp_path / "sim_missing_terminal"
    world_dir = sim_dir / "world"
    world_dir.mkdir(parents=True)
    _write_jsonl(
        world_dir / "actions.jsonl",
        [
            {"event_type": "tick_end", "tick": 4, "round": 4, "summary": "Tick 4 完成。"},
        ],
    )

    monkeypatch.setattr(
        SimulationRunner,
        "get_world_checkpoint_meta",
        classmethod(
            lambda cls, simulation_id: {
                "status": "running",
                "last_completed_tick": 4,
            }
        ),
    )

    state = SimulationRunState(
        simulation_id="sim_missing_terminal",
        simulation_mode="world",
        runner_status=RunnerStatus.RUNNING,
    )
    result = SimulationRunner._classify_world_process_exit(
        "sim_missing_terminal",
        0,
        str(sim_dir),
        state,
    )

    assert result["runner_status"] == RunnerStatus.FAILED
    assert result["terminal_status"] == "interrupted"


def test_get_run_state_reconciles_stale_world_run_state_with_completed_checkpoint(tmp_path, monkeypatch):
    simulation_id = "sim_stale_world_state"
    sim_dir = tmp_path / simulation_id
    world_dir = sim_dir / "world"
    world_dir.mkdir(parents=True)

    run_state_path = sim_dir / "run_state.json"
    run_state_path.write_text(
        json.dumps(
            {
                "simulation_id": simulation_id,
                "simulation_mode": "world",
                "runner_status": "failed",
                "current_round": 1,
                "total_rounds": 4,
                "simulated_hours": 0,
                "total_simulation_hours": 4,
                "world_running": False,
                "world_completed": False,
                "world_completed_events_count": 0,
                "world_current_phase": "intent_generation",
                "world_phase_counts": {},
                "updated_at": "2026-03-19T09:01:28.479082",
                "error": "进程退出码: -15",
                "terminal_status": "",
                "stop_reason": "",
                "process_pid": 24687,
                "recent_actions": [],
                "world_recent_events": [],
                "world_active_events": [],
                "world_queued_events": [],
                "latest_snapshot": None,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (world_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "saved_at": "2026-03-21T13:29:52.548612",
                "status": "completed",
                "terminal_status": "completed",
                "stop_reason": "target_rounds_reached",
                "simulation_id": simulation_id,
                "config_path": str(sim_dir / "simulation_config.json"),
                "last_completed_tick": 12,
                "run_total_rounds": 12,
                "minutes_per_round": 60,
                "active_events": [],
                "queued_events": [],
                "completed_events": [{"event_id": "event_001"}],
                "last_snapshot": {
                    "tick": 12,
                    "simulated_hours": 12.0,
                    "summary": "Tick 12 完成。",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationRunner, "_run_states", {})

    state = SimulationRunner.get_run_state(simulation_id)

    assert state is not None
    assert state.runner_status == RunnerStatus.COMPLETED
    assert state.current_round == 12
    assert state.total_rounds == 12
    assert state.simulated_hours == 12.0
    assert state.world_completed is True
    assert state.world_running is False
    assert state.world_completed_events_count == 1
    assert state.terminal_status == "completed"
    assert state.stop_reason == "target_rounds_reached"
    assert state.error is None
    assert state.process_pid is None

    persisted = json.loads(run_state_path.read_text(encoding="utf-8"))
    assert persisted["runner_status"] == "completed"
    assert persisted["current_round"] == 12
    assert persisted["world_completed"] is True
    assert persisted["terminal_status"] == "completed"
    assert persisted["stop_reason"] == "target_rounds_reached"
    assert persisted["error"] is None
    assert persisted["process_pid"] is None


def test_get_run_state_synthesizes_world_state_when_run_state_missing(tmp_path, monkeypatch):
    simulation_id = "sim_operator_state_bridge"
    sim_dir = tmp_path / simulation_id
    world_dir = sim_dir / "world"
    world_dir.mkdir(parents=True)

    (sim_dir / "simulation_config.json").write_text(
        json.dumps(
            {
                "simulation_id": simulation_id,
                "simulation_mode": "world",
                "time_config": {
                    "total_ticks": 6,
                    "minutes_per_round": 60,
                },
                "agent_configs": [
                    {
                        "agent_id": 1,
                        "entity_name": "Actor One",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        world_dir / "actions.jsonl",
        [
            {
                "event_type": "simulation_start",
                "timestamp": "2026-03-21T17:10:06",
                "simulation_mode": "world",
                "total_rounds": 6,
            },
            {
                "event_type": "tick_end",
                "timestamp": "2026-03-21T17:11:06",
                "round": 1,
                "tick": 1,
                "phase": "tick_complete",
                "simulated_hours": 1.0,
                "summary": "Tick 1 完成。",
            },
            {
                "event_type": "tick_end",
                "timestamp": "2026-03-21T17:12:06",
                "round": 2,
                "tick": 2,
                "phase": "tick_complete",
                "simulated_hours": 2.0,
                "summary": "Tick 2 完成。",
            },
            {
                "event_type": "simulation_end",
                "timestamp": "2026-03-21T17:12:10",
                "terminal_status": "completed",
                "stop_reason": "target_rounds_reached",
                "total_rounds": 2,
                "target_rounds": 2,
                "total_actions": 8,
            },
        ],
    )
    _write_jsonl(
        world_dir / "state_snapshots.jsonl",
        [
            {
                "tick": 2,
                "phase": "tick_complete",
                "simulated_hours": 2.0,
                "summary": "Tick 2 完成。",
                "active_events": [],
                "queued_events": [],
                "counts": {
                    "completed_events_count": 1,
                    "lifecycle_counters": {
                        "tick_end": 2,
                    },
                },
            },
        ],
    )
    (world_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "saved_at": "2026-03-21T17:12:12",
                "status": "completed",
                "terminal_status": "completed",
                "stop_reason": "target_rounds_reached",
                "simulation_id": simulation_id,
                "config_path": str(sim_dir / "simulation_config.json"),
                "last_completed_tick": 2,
                "run_total_rounds": 2,
                "minutes_per_round": 60,
                "active_events": [],
                "queued_events": [],
                "completed_events": [{"event_id": "event_001"}],
                "last_snapshot": {
                    "tick": 2,
                    "phase": "tick_complete",
                    "simulated_hours": 2.0,
                    "summary": "Tick 2 完成。",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationRunner, "_run_states", {})

    state = SimulationRunner.get_run_state(simulation_id)

    assert state is not None
    assert state.runner_status == RunnerStatus.COMPLETED
    assert state.current_round == 2
    assert state.total_rounds == 2
    assert state.world_completed is True
    assert state.world_running is False
    assert state.terminal_status == "completed"
    assert state.stop_reason == "target_rounds_reached"
    assert state.started_at == "2026-03-21T17:10:06"
    assert state.world_phase_counts["tick_end"] == 2
    assert state.world_completed_events_count == 1

    persisted = json.loads((sim_dir / "run_state.json").read_text(encoding="utf-8"))
    assert persisted["runner_status"] == "completed"
    assert persisted["current_round"] == 2
    assert persisted["started_at"] == "2026-03-21T17:10:06"
    assert persisted["terminal_status"] == "completed"


def test_world_run_process_once_bridges_operator_run_state(tmp_path, monkeypatch):
    simulation_id = "sim_operator_bridge_calls"
    sim_dir = tmp_path / simulation_id
    sim_dir.mkdir(parents=True)
    config_path = sim_dir / "simulation_config.json"
    config_path.write_text(
        json.dumps(
            {
                "simulation_id": simulation_id,
                "simulation_mode": "world",
                "time_config": {
                    "total_ticks": 4,
                    "minutes_per_round": 60,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    bootstrap_calls = []
    refresh_calls = []

    class FakeProcess:
        def __init__(self):
            self.pid = 43210
            self._polls = 0

        def poll(self):
            self._polls += 1
            return None if self._polls == 1 else 0

    monkeypatch.setattr(world_run, "_backend_dir", str(tmp_path))
    monkeypatch.setattr(world_run, "_python_bin", lambda: "/fake/python")
    monkeypatch.setattr(world_run.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        world_run.subprocess,
        "Popen",
        lambda cmd, cwd=None: FakeProcess(),
    )
    monkeypatch.setattr(
        world_run.SimulationRunner,
        "bootstrap_world_operator_run_state",
        classmethod(
            lambda cls, sim_id, cfg_path, **kwargs: bootstrap_calls.append(
                {
                    "simulation_id": sim_id,
                    "config_path": cfg_path,
                    **kwargs,
                }
            )
        ),
    )
    monkeypatch.setattr(
        world_run.SimulationRunner,
        "refresh_world_run_state_from_artifacts",
        classmethod(
            lambda cls, sim_id, **kwargs: refresh_calls.append(
                {
                    "simulation_id": sim_id,
                    **kwargs,
                }
            )
        ),
    )

    result = world_run._run_world_process_once(
        str(config_path),
        max_rounds=4,
        resume_from_checkpoint=False,
    )

    assert result["returncode"] == 0
    assert bootstrap_calls == [
        {
            "simulation_id": simulation_id,
            "config_path": str(config_path),
            "max_rounds": 4,
            "resume_from_checkpoint": False,
            "process_pid": 43210,
        }
    ]
    assert refresh_calls == [
        {
            "simulation_id": simulation_id,
            "persist": True,
            "fallback_runner_status": RunnerStatus.RUNNING,
            "process_pid": 43210,
        },
        {
            "simulation_id": simulation_id,
            "persist": True,
        },
    ]


def test_world_simulation_end_uses_terminal_status_field():
    state = SimulationRunState(
        simulation_id="sim_terminal_status",
        simulation_mode="world",
        runner_status=RunnerStatus.RUNNING,
        world_running=True,
    )

    SimulationRunner._handle_event_log_entry(
        state,
        "world",
        {
            "event_type": "simulation_end",
            "terminal_status": "completed",
            "stop_reason": "target_rounds_reached",
            "total_rounds": 8,
            "total_actions": 42,
        },
    )

    assert state.terminal_status == "completed"
    assert state.stop_reason == "target_rounds_reached"
