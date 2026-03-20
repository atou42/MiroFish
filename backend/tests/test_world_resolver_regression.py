import json
from pathlib import Path

from scripts.replay_world_resolver_trace import replay_trace
from scripts.run_world_simulation import WorldSimulationRuntime


def _write_minimal_world_config(tmp_path: Path) -> Path:
    simulation_dir = tmp_path / "sim_trace_regression"
    simulation_dir.mkdir()
    config_path = simulation_dir / "simulation_config.json"
    config = {
        "simulation_id": "sim_trace_regression",
        "simulation_mode": "world",
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
        "plot_threads": [{"title": "Regression Front"}],
        "pressure_tracks": [
            {"name": "conflict", "starting_level": 0.4},
            {"name": "scarcity", "starting_level": 0.3},
            {"name": "legitimacy", "starting_level": 0.5},
        ],
        "initial_world_state": {"starting_condition": "regression"},
        "runtime_config": {},
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def _build_runtime(tmp_path: Path, monkeypatch) -> WorldSimulationRuntime:
    monkeypatch.setattr(WorldSimulationRuntime, "_build_llm", lambda self, selector=None: None)
    monkeypatch.setattr(WorldSimulationRuntime, "_get_actor_llm", lambda self, agent=None: None)
    return WorldSimulationRuntime(config_path=str(_write_minimal_world_config(tmp_path)), max_rounds=1)


def test_tick101_trace_regression_accepts_queued_events(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)
    trace_path = (
        Path(__file__).resolve().parents[1]
        / "uploads/simulations/sim_8ac60f042d62/world/debug/llm_traces/20260320T155535/"
        / "00084-tick101-resolver_cluster-world_resolver-zero_accept.json"
    )
    assert trace_path.exists(), f"missing regression trace fixture: {trace_path}"

    result = replay_trace(runtime, str(trace_path))

    assert result["accepted_event_count"] == 3
    assert not result["deferred_map"]
    assert not result["rejected_map"]
    assert result["diagnostics"]["reason_code"] == "resolver_accepted_alt_schema"


def test_prompt_contracts_are_hardened(tmp_path, monkeypatch):
    runtime = _build_runtime(tmp_path, monkeypatch)

    actor_prompt = runtime._actor_intent_system_prompt()
    resolver_prompt = runtime._resolver_system_prompt()

    assert "不要把字段留空来逃避决策" in actor_prompt
    assert "objective 必须是具体动作" in actor_prompt
    assert "每个 intent_id 必须且只能出现一次" in resolver_prompt
    assert "queued" in resolver_prompt
    assert "禁止输出 active_events" in resolver_prompt
