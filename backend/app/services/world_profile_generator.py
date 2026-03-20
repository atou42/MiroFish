"""
World mode profile generator.

Builds simulation-ready character / faction profiles from graph entities without
assuming social-media account semantics.
"""

import concurrent.futures
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from .world_preset_registry import WorldPreset
from .zep_entity_reader import EntityNode

logger = get_logger("mirofish.world_profile_generator")


DEFAULT_TRAIT_SETS = [
    ["pragmatic", "observant", "defensive"],
    ["ambitious", "adaptive", "risk_tolerant"],
    ["disciplined", "calculating", "patient"],
    ["idealistic", "curious", "restless"],
    ["charismatic", "protective", "opportunistic"],
]


@dataclass
class WorldAgentProfile:
    agent_id: int
    entity_uuid: str
    entity_name: str
    entity_type: str
    core_identity: str
    public_role: str
    driving_goals: List[str] = field(default_factory=list)
    resources: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    temperament: List[str] = field(default_factory=list)
    connected_entities: List[str] = field(default_factory=list)
    story_hooks: List[str] = field(default_factory=list)
    home_location: str = ""
    summary: str = ""
    llm_selector: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class WorldProfileGenerator:
    """Derive world-simulation profiles from filtered graph entities."""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self._llm = llm_client
        self._llm_checked = False

    def generate_profiles_from_entities(
        self,
        entities: List[EntityNode],
        use_llm: bool = True,
        progress_callback: Optional[callable] = None,
        realtime_output_path: Optional[str] = None,
        parallel_count: int = 3,
        world_preset: Optional[WorldPreset] = None,
    ) -> List[WorldAgentProfile]:
        total = max(len(entities), 1)
        if not entities:
            return []

        parallel_count = max(1, min(parallel_count, len(entities)))
        profiles: List[Optional[WorldAgentProfile]] = [None] * len(entities)

        def build_profile(index: int, entity: EntityNode) -> WorldAgentProfile:
            profile = self._build_base_profile(index, entity, world_preset=world_preset)
            if use_llm:
                return self._maybe_enrich_with_llm(profile, entity)
            return profile

        if parallel_count == 1:
            for index, entity in enumerate(entities):
                profiles[index] = build_profile(index, entity)
                completed_profiles = [profile for profile in profiles if profile is not None]
                if realtime_output_path:
                    self.save_profiles(completed_profiles, realtime_output_path)
                if progress_callback and profiles[index]:
                    progress_callback(index + 1, total, f"{profiles[index].entity_name} / {profiles[index].entity_type}")
            return [profile for profile in profiles if profile is not None]

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_count) as executor:
            future_to_index = {
                executor.submit(build_profile, index, entity): index
                for index, entity in enumerate(entities)
            }
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                entity = entities[index]
                try:
                    profiles[index] = future.result()
                except Exception as exc:
                    logger.warning(f"World profile generation failed for {entity.name}, fallback to base profile: {exc}")
                    profiles[index] = self._build_base_profile(index, entity, world_preset=world_preset)

                completed += 1
                completed_profiles = [profile for profile in profiles if profile is not None]
                completed_profiles.sort(key=lambda profile: profile.agent_id)

                if realtime_output_path:
                    self.save_profiles(completed_profiles, realtime_output_path)

                if progress_callback and profiles[index]:
                    progress_callback(
                        completed,
                        total,
                        f"{profiles[index].entity_name} / {profiles[index].entity_type}",
                    )

        return [profile for profile in profiles if profile is not None]

    def save_profiles(self, profiles: List[WorldAgentProfile], file_path: str) -> None:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump([profile.to_dict() for profile in profiles], f, ensure_ascii=False, indent=2)

    def _build_base_profile(
        self,
        agent_id: int,
        entity: EntityNode,
        world_preset: Optional[WorldPreset] = None,
    ) -> WorldAgentProfile:
        entity_type = entity.get_entity_type() or "Actor"
        related_names = [
            node.get("name", "")
            for node in (entity.related_nodes or [])
            if node.get("name")
        ][:8]
        hooks = [
            edge.get("fact") or edge.get("name") or ""
            for edge in (entity.related_edges or [])
            if edge.get("fact") or edge.get("name")
        ][:5]

        attributes = entity.attributes or {}
        attr_fragments = [
            f"{key}: {value}"
            for key, value in attributes.items()
            if value not in (None, "", [], {})
        ]
        summary = (entity.summary or "").strip()
        identity_parts = [summary] if summary else []
        identity_parts.extend(attr_fragments[:3])
        core_identity = "；".join(identity_parts[:3]) if identity_parts else f"{entity.name} is a key {entity_type} in the world."

        location = self._guess_location(entity)
        goals = self._infer_goals(entity_type, summary, related_names, hooks)
        resources = self._infer_resources(attributes, hooks)
        constraints = self._infer_constraints(summary, hooks)
        temperament = DEFAULT_TRAIT_SETS[agent_id % len(DEFAULT_TRAIT_SETS)]

        fallback_selector = Config.get_agent_llm_selector(
            simulation_mode="world",
            entity_type=entity_type,
            agent_name=entity.name,
        ) or ""
        preset_selector = (
            world_preset.selector_for_agent(
                entity_type=entity_type,
                agent_name=entity.name,
                fallback_selector=fallback_selector,
            )
            if world_preset
            else fallback_selector
        )

        return WorldAgentProfile(
            agent_id=agent_id,
            entity_uuid=entity.uuid,
            entity_name=entity.name,
            entity_type=entity_type,
            core_identity=core_identity,
            public_role=self._infer_public_role(entity_type, summary),
            driving_goals=goals,
            resources=resources,
            constraints=constraints,
            temperament=temperament,
            connected_entities=related_names,
            story_hooks=hooks,
            home_location=location,
            summary=summary or core_identity,
            llm_selector=preset_selector,
        )

    def _get_llm(self) -> Optional[LLMClient]:
        if self._llm_checked:
            return self._llm

        self._llm_checked = True
        if self._llm is not None:
            return self._llm

        try:
            self._llm = LLMClient.from_namespace("WORLD_PROFILE")
        except Exception as exc:
            logger.info(f"WorldProfileGenerator LLM unavailable, using heuristic profiles: {exc}")
            self._llm = None
        return self._llm

    def _maybe_enrich_with_llm(
        self,
        profile: WorldAgentProfile,
        entity: EntityNode,
    ) -> WorldAgentProfile:
        llm = self._get_llm()
        if llm is None:
            return profile

        try:
            response = llm.chat_json(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是世界模拟的角色编排器。请将输入实体整理为推进剧情所需的人设，"
                            "输出 JSON，字段包括 public_role, driving_goals, resources, constraints, "
                            "temperament, home_location, story_hooks, summary。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "entity_name": entity.name,
                                "entity_type": entity.get_entity_type() or "Actor",
                                "summary": entity.summary,
                                "attributes": entity.attributes,
                                "related_edges": entity.related_edges[:8],
                                "related_nodes": entity.related_nodes[:8],
                                "base_profile": profile.to_dict(),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.4,
                max_tokens=1200,
            )

            merged = profile.to_dict()
            for key in (
                "public_role",
                "driving_goals",
                "resources",
                "constraints",
                "temperament",
                "home_location",
                "story_hooks",
                "summary",
            ):
                value = response.get(key)
                if value:
                    merged[key] = value

            return WorldAgentProfile(**merged)
        except Exception as exc:
            logger.warning(f"World profile LLM enrich failed for {entity.name}: {exc}")
            return profile

    def _guess_location(self, entity: EntityNode) -> str:
        for node in entity.related_nodes or []:
            labels = node.get("labels") or []
            if any(label.lower() in {"place", "location", "city", "region", "territory", "realm"} for label in labels):
                return node.get("name", "")

        for key, value in (entity.attributes or {}).items():
            if "location" in key.lower() or "place" in key.lower():
                return str(value)

        return ""

    def _infer_public_role(self, entity_type: str, summary: str) -> str:
        type_lower = entity_type.lower()
        if any(key in type_lower for key in ("faction", "organization", "guild", "order")):
            return "Power bloc shaping regional decisions"
        if any(key in type_lower for key in ("place", "city", "region", "territory")):
            return "Strategic location where change accumulates"
        if any(key in type_lower for key in ("artifact", "resource", "secret")):
            return "Catalyst object that shifts leverage"
        if any(key in type_lower for key in ("rule", "institution", "law")):
            return "Structural force constraining behavior"
        if summary:
            return summary[:80]
        return "Autonomous actor inside the world"

    def _infer_goals(
        self,
        entity_type: str,
        summary: str,
        related_names: List[str],
        hooks: List[str],
    ) -> List[str]:
        type_lower = entity_type.lower()
        goals: List[str] = []

        if any(key in type_lower for key in ("character", "person", "actor", "creature")):
            goals.extend(["protect current position", "respond to nearby threats"])
        elif any(key in type_lower for key in ("faction", "organization", "guild", "house")):
            goals.extend(["expand influence", "keep internal cohesion"])
        elif any(key in type_lower for key in ("place", "city", "territory", "realm")):
            goals.extend(["maintain stability", "absorb or repel outside pressure"])
        elif any(key in type_lower for key in ("artifact", "resource", "secret")):
            goals.extend(["change hands only with consequence", "reshape who holds leverage"])
        elif any(key in type_lower for key in ("rule", "institution", "law")):
            goals.extend(["preserve legitimacy", "limit destabilizing actions"])

        if related_names:
            goals.append(f"manage ties with {related_names[0]}")
        if hooks:
            goals.append(hooks[0][:120])
        if summary and len(goals) < 3:
            goals.append(summary[:120])

        return goals[:4] or ["survive the next change in world state"]

    def _infer_resources(self, attributes: Dict[str, Any], hooks: List[str]) -> List[str]:
        resources = []
        for key, value in attributes.items():
            if value in (None, "", [], {}):
                continue
            if len(resources) >= 4:
                break
            resources.append(f"{key}: {value}")

        if len(resources) < 2:
            for hook in hooks[: 2 - len(resources)]:
                resources.append(hook[:120])

        return resources or ["limited but meaningful local knowledge"]

    def _infer_constraints(self, summary: str, hooks: List[str]) -> List[str]:
        constraints = []
        if summary:
            constraints.append(summary[:120])
        for hook in hooks:
            if len(constraints) >= 3:
                break
            if hook[:120] not in constraints:
                constraints.append(hook[:120])

        return constraints or ["subject to broader world pressure"]
