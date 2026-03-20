"""
World mode simulation config generator.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from .world_preset_registry import WorldPreset, WorldPresetRegistry
from .world_profile_generator import WorldAgentProfile
from .zep_entity_reader import EntityNode

logger = get_logger("mirofish.world_config_generator")


@dataclass
class WorldSimulationConfig:
    simulation_mode: str
    simulation_id: str
    project_id: str
    graph_id: str
    simulation_requirement: str
    time_config: Dict[str, Any]
    runtime_config: Dict[str, Any]
    agent_configs: List[Dict[str, Any]]
    world_rules: List[str] = field(default_factory=list)
    plot_threads: List[Dict[str, Any]] = field(default_factory=list)
    pressure_tracks: List[Dict[str, Any]] = field(default_factory=list)
    initial_world_state: Dict[str, Any] = field(default_factory=dict)
    preset: Dict[str, Any] = field(default_factory=dict)
    generation_reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class WorldConfigGenerator:
    """Build deterministic world-simulation config, with optional LLM enrichment."""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self._llm = llm_client
        self._llm_checked = False

    def generate_config(
        self,
        simulation_id: str,
        project_id: str,
        graph_id: str,
        simulation_requirement: str,
        document_text: str,
        entities: List[EntityNode],
        profiles: List[WorldAgentProfile],
        world_preset: Optional[WorldPreset] = None,
    ) -> WorldSimulationConfig:
        total_rounds = min(max(8, len(profiles) // 3 + 6), 24)
        minutes_per_round = 60
        dominant_types = self._top_entity_types(entities)
        actor_count = max(len(profiles), 1)
        intent_agents_per_tick = min(
            actor_count,
            max(6, min(10, actor_count // 5 + 4)),
        )
        intent_concurrency = min(
            max(2, Config.WORLD_INTENT_CONCURRENCY),
            intent_agents_per_tick,
            4,
        )

        config = WorldSimulationConfig(
            simulation_mode="world",
            simulation_id=simulation_id,
            project_id=project_id,
            graph_id=graph_id,
            simulation_requirement=simulation_requirement,
            time_config={
                "total_simulation_hours": total_rounds,
                "minutes_per_round": minutes_per_round,
                "total_rounds": total_rounds,
                "total_ticks": total_rounds,
                "scene_unit": "tick",
                "pacing": "concurrent-world",
            },
            runtime_config={
                "intent_agents_per_tick": intent_agents_per_tick,
                "intent_concurrency": intent_concurrency,
                "resolver_cluster_size": min(max(3, intent_agents_per_tick // 3), 4),
                "resolver_cluster_concurrency": 2 if intent_agents_per_tick > 4 else 1,
                "actor_selection_core_ratio": 0.35,
                "actor_selection_hot_ratio": 0.35,
                "max_active_events": min(max(4, len(profiles) // 2), 10),
                "max_queued_events": min(max(6, len(profiles)), 18),
                "max_event_duration_ticks": 3,
                "snapshot_interval_ticks": 1,
                "default_actor_llm_selector": "WORLD_AGENT",
                "resolver_llm_selector": "WORLD_RESOLVER",
                "resolver_salvage_on_zero_accept": True,
            },
            agent_configs=[profile.to_dict() for profile in profiles],
            world_rules=self._infer_world_rules(document_text, simulation_requirement),
            plot_threads=self._infer_plot_threads(profiles),
            pressure_tracks=self._infer_pressure_tracks(simulation_requirement, dominant_types),
            initial_world_state={
                "entity_count": len(entities),
                "agent_count": len(profiles),
                "dominant_entity_types": dominant_types,
                "starting_condition": self._summarize_condition(simulation_requirement, document_text),
            },
            preset={},
            generation_reasoning=(
                "World mode uses graph entities as actors and world elements, converts them "
                "into plot threads and pressure tracks, then runs a concurrent tick-based "
                "simulation with intent generation, conflict resolution, and multi-tick events."
            ),
        )

        config = self._maybe_enrich_with_llm(config, document_text)
        return self._apply_world_preset(config, world_preset)

    def _get_llm(self) -> Optional[LLMClient]:
        if self._llm_checked:
            return self._llm

        self._llm_checked = True
        if self._llm is not None:
            return self._llm

        try:
            self._llm = LLMClient.from_namespace("WORLD_CONFIG")
        except Exception as exc:
            logger.info(f"WorldConfigGenerator LLM unavailable, using heuristic config: {exc}")
            self._llm = None
        return self._llm

    def _maybe_enrich_with_llm(
        self,
        config: WorldSimulationConfig,
        document_text: str,
    ) -> WorldSimulationConfig:
        llm = self._get_llm()
        if llm is None:
            return config

        try:
            response = llm.chat_json(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是世界推进模拟的编排器。基于输入的初始配置，输出 JSON，"
                            "只允许更新 world_rules, plot_threads, pressure_tracks, generation_reasoning, "
                            "time_config, runtime_config。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "simulation_requirement": config.simulation_requirement,
                                "document_excerpt": document_text[:6000],
                                "draft_config": config.to_dict(),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.4,
                max_tokens=1600,
            )

            merged = config.to_dict()
            for key in (
                "world_rules",
                "plot_threads",
                "pressure_tracks",
                "generation_reasoning",
                "time_config",
                "runtime_config",
            ):
                value = response.get(key)
                if value:
                    merged[key] = value

            return WorldSimulationConfig(**merged)
        except Exception as exc:
            logger.warning(f"World config LLM enrich failed: {exc}")
            return config

    def _apply_world_preset(
        self,
        config: WorldSimulationConfig,
        world_preset: Optional[WorldPreset],
    ) -> WorldSimulationConfig:
        if world_preset is None:
            return config

        merged = config.to_dict()
        runtime_config = dict(merged.get("runtime_config") or {})
        time_config = dict(merged.get("time_config") or {})

        runtime_config.update(world_preset.runtime_overrides)
        time_config.update(world_preset.time_overrides)

        if world_preset.actor_selector:
            runtime_config["default_actor_llm_selector"] = world_preset.actor_selector
        if world_preset.resolver_selector:
            runtime_config["resolver_llm_selector"] = world_preset.resolver_selector

        merged["runtime_config"] = runtime_config
        merged["time_config"] = time_config
        merged["preset"] = world_preset.to_config_dict(
            is_default=world_preset.preset_id == WorldPresetRegistry.default_preset_id()
        )
        merged["generation_reasoning"] = (
            f"{merged.get('generation_reasoning', '').strip()} "
            f"Preset '{world_preset.preset_id}' applied to lock model routing and runtime defaults."
        ).strip()
        return WorldSimulationConfig(**merged)

    def _infer_world_rules(self, document_text: str, simulation_requirement: str) -> List[str]:
        text = f"{simulation_requirement}\n{document_text[:4000]}"
        candidates: List[str] = []

        for line in text.splitlines():
            line = line.strip(" -\t")
            if not line:
                continue
            if any(token in line for token in ("规则", "禁忌", "法则", "必须", "不得", "不能", "only", "forbidden")):
                candidates.append(line[:140])
            if len(candidates) >= 4:
                break

        if candidates:
            return candidates

        return [
            "世界状态不会静止，每一轮至少有一个关系或资源分布发生变化。",
            "角色优先维护自身目标，但会受规则、地点和稀缺资源约束。",
            "势力冲突和联盟都可以跨轮持续累积，不会在单轮内自动归零。",
        ]

    def _infer_plot_threads(self, profiles: List[WorldAgentProfile]) -> List[Dict[str, Any]]:
        threads = []
        for profile in profiles[: min(6, len(profiles))]:
            threads.append(
                {
                    "title": f"{profile.entity_name} pushes for change",
                    "owner": profile.entity_name,
                    "focus": profile.driving_goals[:2],
                    "risk": profile.constraints[:2],
                }
            )
        return threads

    def _infer_pressure_tracks(
        self,
        simulation_requirement: str,
        dominant_types: List[str],
    ) -> List[Dict[str, Any]]:
        requirement = simulation_requirement.lower()
        tracks = [
            {
                "name": "conflict",
                "starting_level": 0.55 if any(word in requirement for word in ("war", "conflict", "争", "战", "政变")) else 0.35,
                "driver": "factional rivalry and incompatible goals",
            },
            {
                "name": "scarcity",
                "starting_level": 0.5 if any(word in requirement for word in ("resource", "能源", "粮", "water", "稀缺")) else 0.3,
                "driver": "competition over limited leverage",
            },
            {
                "name": "legitimacy",
                "starting_level": 0.45 if any("rule" in entity.lower() or "institution" in entity.lower() for entity in dominant_types) else 0.25,
                "driver": "trust in rules, rulers, and social order",
            },
        ]
        return tracks

    def _top_entity_types(self, entities: List[EntityNode]) -> List[str]:
        counts: Dict[str, int] = {}
        for entity in entities:
            entity_type = entity.get_entity_type() or "Actor"
            counts[entity_type] = counts.get(entity_type, 0) + 1

        ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        return [item[0] for item in ordered[:6]]

    def _summarize_condition(self, simulation_requirement: str, document_text: str) -> str:
        seed = simulation_requirement.strip() or document_text.strip()
        if not seed:
            return "A volatile world with unresolved tensions."
        return seed[:180]
