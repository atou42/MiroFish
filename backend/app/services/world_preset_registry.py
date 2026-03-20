"""
World runtime preset registry.

Presets are static repo assets that bundle actor/resolver selectors, runtime
overrides, and evaluation metadata so world simulations can be switched between
known-good operating modes without editing config files by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def _normalize_key(value: Optional[str]) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _ensure_mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ensure_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class WorldPreset:
    preset_id: str
    label: str
    description: str
    strategy_class: str = "custom"
    tags: List[str] = field(default_factory=list)
    recommendation: str = ""
    actor_selector: Optional[str] = None
    resolver_selector: Optional[str] = None
    preserve_registry_agent_selectors: bool = False
    by_entity_type: Dict[str, str] = field(default_factory=dict)
    by_agent_name: Dict[str, str] = field(default_factory=dict)
    time_overrides: Dict[str, Any] = field(default_factory=dict)
    runtime_overrides: Dict[str, Any] = field(default_factory=dict)
    evaluation: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "WorldPreset":
        return cls(
            preset_id=str(payload.get("id") or "").strip(),
            label=str(payload.get("label") or "").strip(),
            description=str(payload.get("description") or "").strip(),
            strategy_class=str(payload.get("strategy_class") or "custom").strip() or "custom",
            tags=[str(item).strip() for item in _ensure_list(payload.get("tags")) if str(item).strip()],
            recommendation=str(payload.get("recommendation") or "").strip(),
            actor_selector=str(payload.get("actor_selector") or "").strip() or None,
            resolver_selector=str(payload.get("resolver_selector") or "").strip() or None,
            preserve_registry_agent_selectors=bool(payload.get("preserve_registry_agent_selectors", False)),
            by_entity_type={
                _normalize_key(key): str(value).strip()
                for key, value in _ensure_mapping(payload.get("by_entity_type")).items()
                if str(value).strip()
            },
            by_agent_name={
                _normalize_key(key): str(value).strip()
                for key, value in _ensure_mapping(payload.get("by_agent_name")).items()
                if str(value).strip()
            },
            time_overrides=dict(_ensure_mapping(payload.get("time_overrides"))),
            runtime_overrides=dict(_ensure_mapping(payload.get("runtime_overrides"))),
            evaluation=dict(_ensure_mapping(payload.get("evaluation"))),
            notes=[str(item).strip() for item in _ensure_list(payload.get("notes")) if str(item).strip()],
        )

    def selector_for_agent(
        self,
        *,
        entity_type: Optional[str],
        agent_name: Optional[str],
        fallback_selector: Optional[str] = None,
    ) -> str:
        name_key = _normalize_key(agent_name)
        if name_key and name_key in self.by_agent_name:
            return self.by_agent_name[name_key]

        entity_type_key = _normalize_key(entity_type)
        if entity_type_key and entity_type_key in self.by_entity_type:
            return self.by_entity_type[entity_type_key]

        if self.actor_selector and not self.preserve_registry_agent_selectors:
            return self.actor_selector

        return str(fallback_selector or self.actor_selector or "").strip()

    def to_api_dict(self, *, is_default: bool = False) -> Dict[str, Any]:
        return {
            "id": self.preset_id,
            "label": self.label,
            "description": self.description,
            "strategy_class": self.strategy_class,
            "tags": self.tags,
            "recommendation": self.recommendation,
            "actor_selector": self.actor_selector,
            "resolver_selector": self.resolver_selector,
            "preserve_registry_agent_selectors": self.preserve_registry_agent_selectors,
            "time_overrides": self.time_overrides,
            "runtime_overrides": self.runtime_overrides,
            "evaluation": self.evaluation,
            "notes": self.notes,
            "is_default": is_default,
        }

    def to_config_dict(self, *, is_default: bool = False) -> Dict[str, Any]:
        return self.to_api_dict(is_default=is_default)


class WorldPresetRegistry:
    _cache: Optional[Dict[str, Any]] = None
    _presets_by_id: Optional[Dict[str, WorldPreset]] = None

    @classmethod
    def _registry_path(cls) -> Path:
        return Path(__file__).resolve().parents[2] / "evals" / "world_runtime_presets.json"

    @classmethod
    def _load_registry(cls) -> Dict[str, Any]:
        if cls._cache is not None:
            return cls._cache

        path = cls._registry_path()
        with path.open("r", encoding="utf-8") as f:
            cls._cache = json.load(f)
        return cls._cache

    @classmethod
    def _load_presets(cls) -> Dict[str, WorldPreset]:
        if cls._presets_by_id is not None:
            return cls._presets_by_id

        registry = cls._load_registry()
        presets: Dict[str, WorldPreset] = {}
        for item in _ensure_list(registry.get("presets")):
            if not isinstance(item, dict):
                continue
            preset = WorldPreset.from_dict(item)
            if not preset.preset_id:
                continue
            presets[preset.preset_id] = preset
        cls._presets_by_id = presets
        return presets

    @classmethod
    def default_preset_id(cls) -> Optional[str]:
        registry = cls._load_registry()
        return str(registry.get("default_preset") or "").strip() or None

    @classmethod
    def list_presets(cls) -> List[WorldPreset]:
        presets = cls._load_presets()
        return list(presets.values())

    @classmethod
    def get_preset(cls, preset_id: Optional[str] = None) -> WorldPreset:
        presets = cls._load_presets()
        resolved_id = str(preset_id or cls.default_preset_id() or "").strip()
        if not resolved_id:
            raise ValueError("No world preset is configured")
        preset = presets.get(resolved_id)
        if preset is None:
            available = ", ".join(sorted(presets.keys()))
            raise ValueError(f"Unknown world preset '{resolved_id}'. Available: {available}")
        return preset

    @classmethod
    def build_api_payload(cls) -> Dict[str, Any]:
        default_id = cls.default_preset_id()
        presets = [
            preset.to_api_dict(is_default=(preset.preset_id == default_id))
            for preset in cls.list_presets()
        ]
        return {
            "schema_version": cls._load_registry().get("schema_version", 1),
            "default_preset": default_id,
            "presets": presets,
        }
