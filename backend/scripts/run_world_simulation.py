#!/usr/bin/env python3
"""
Concurrent world-mode simulation runner.

The runtime advances a shared world by ticks. On each tick it:
1. Promotes queued events whose dependencies are satisfied
2. Generates actor intents concurrently
3. Resolves intents into accepted/deferred/rejected world events
4. Starts accepted events and completes due events
5. Persists lifecycle logs and world snapshots for the Flask polling layer
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from dotenv import load_dotenv

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)

_env_file = os.path.join(_project_root, ".env")
if os.path.exists(_env_file):
    load_dotenv(_env_file)

from app.config import Config
from app.utils.world_run_lock import WorldRunLease, world_run_paths_for_config
from app.utils.llm_client import InvalidJSONResponseError, LLMClient


def now_iso() -> str:
    return datetime.now().isoformat()


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def safe_int(value: Any, default: int, lower: Optional[int] = None, upper: Optional[int] = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if lower is not None:
        result = max(lower, result)
    if upper is not None:
        result = min(upper, result)
    return result


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ensure_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, "", [], {}):
        return []
    return [str(value).strip()]


def dedupe_keep_order(values: Sequence[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def clip_list(values: Any, item_limit: int, list_limit: int) -> List[str]:
    return [clip_text(item, item_limit) for item in ensure_list(values)[:list_limit]]


def json_list_to_tuple_tree(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(json_list_to_tuple_tree(item) for item in value)
    if isinstance(value, dict):
        return {key: json_list_to_tuple_tree(item) for key, item in value.items()}
    return value


def json_keyed_int_dict(payload: Any) -> Dict[int, int]:
    if not isinstance(payload, dict):
        return {}

    normalized: Dict[int, int] = {}
    for raw_key, raw_value in payload.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            continue
        if key < 0:
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 0
        normalized[key] = max(0, value)
    return normalized


def build_initial_world_state(
    plot_threads: Sequence[Dict[str, Any]],
    pressure_tracks: Sequence[Dict[str, Any]],
    initial_world_state: Dict[str, Any],
) -> Dict[str, Any]:
    pressure_map: Dict[str, float] = {}
    for track in pressure_tracks:
        pressure_map[str(track.get("name", "pressure"))] = clamp(
            safe_float(track.get("starting_level", 0.35), 0.35),
            0.05,
            0.95,
        )

    conflict = pressure_map.get("conflict", 0.35)
    scarcity = pressure_map.get("scarcity", 0.30)
    legitimacy = pressure_map.get("legitimacy", 0.45)
    momentum = clamp(0.35 + (conflict * 0.10), 0.05, 0.95)
    tension = clamp((conflict * 0.60) + (scarcity * 0.25) + (momentum * 0.15), 0.05, 0.95)
    stability = clamp((legitimacy * 0.50) + ((1 - conflict) * 0.35) + ((1 - scarcity) * 0.15), 0.05, 0.95)

    return {
        "tension": round(tension, 3),
        "stability": round(stability, 3),
        "momentum": round(momentum, 3),
        "pressure_tracks": pressure_map,
        "focus_threads": [thread.get("title", "") for thread in plot_threads[:6] if thread.get("title")],
        "starting_condition": initial_world_state.get("starting_condition", ""),
        "last_tick_summary": "",
        "active_event_ids": [],
        "queued_event_ids": [],
        "completed_event_ids": [],
    }


PLACEHOLDER_TEXTS = {
    "",
    "none",
    "null",
    "not specified",
    "n/a",
    "na",
    "unknown",
    "unspecified",
    "未说明",
    "未知",
    "无",
}

GENERIC_OBJECTIVE_MARKERS = {
    "",
    "advance current leverage",
    "maintain current leverage",
    "protect current position",
    "respond to nearby threats",
    "summary",
    "title",
    "name",
    "plan",
    "objective",
    "intent",
    "action",
    "方案",
    "标题",
    "名称",
    "摘要",
    "动机",
    "意图",
    "行动",
}

META_LINE_MARKERS = {
    "角色意图",
    "本轮意图",
    "本轮行动",
    "本回合行动",
    "本 tick 行动",
    "本 tick 行动提案",
    "本 tick 行动方案",
    "本 tick 具体行动",
    "核心判断",
    "核心行动",
    "核心意图",
    "具体行动倾向",
    "实际行动意图",
    "行动名称",
    "行动提案",
    "行动拆解",
    "意图拆解",
    "风格判断",
    "行动方向",
    "行动建议",
    "行动方案",
    "建议判定",
    "本轮角色意图",
}

TOKEN_STOPWORDS = {
    "current",
    "world",
    "scene",
    "tick",
    "round",
    "intent",
    "summary",
    "action",
    "objective",
    "角色意图",
    "本轮",
    "当前局势",
    "当前",
    "局势",
    "行动",
    "意图",
    "世界",
    "角色",
    "方案",
    "目标",
    "建议",
    "推进",
}


def normalize_optional_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text).strip()
    if normalized.lower() in PLACEHOLDER_TEXTS:
        return ""
    return normalized


def extract_signal_tokens(value: Any) -> List[str]:
    text = normalize_optional_text(value)
    if not text:
        return []

    english = [
        token.strip("_-").lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text)
    ]
    chinese = re.findall(r"[\u4e00-\u9fff]{2,8}", text)
    ordered: List[str] = []

    for token in [*english, *chinese]:
        normalized = token.strip()
        if (
            not normalized
            or normalized in TOKEN_STOPWORDS
            or normalized.isdigit()
            or re.fullmatch(r"(?:tick|round)\d*", normalized)
        ):
            continue
        ordered.append(normalized)

    return dedupe_keep_order(ordered)


def weighted_sample_without_replacement(
    rng: random.Random,
    items: Sequence[Any],
    weights: Sequence[float],
    count: int,
) -> List[Any]:
    pool = list(zip(items, weights))
    chosen: List[Any] = []

    while pool and len(chosen) < count:
        total = sum(max(weight, 0.0) for _, weight in pool)
        if total <= 0:
            chosen.extend(item for item, _ in pool[: count - len(chosen)])
            break

        roll = rng.random() * total
        upto = 0.0
        picked_index = 0
        for index, (_, weight) in enumerate(pool):
            upto += max(weight, 0.0)
            if upto >= roll:
                picked_index = index
                break

        item, _ = pool.pop(picked_index)
        chosen.append(item)

    return chosen


@dataclass
class ActorIntent:
    intent_id: str
    tick: int
    agent_id: int
    agent_name: str
    objective: str
    summary: str
    location: str = ""
    target: Optional[str] = None
    desired_duration: int = 1
    priority: int = 3
    urgency: int = 3
    risk_level: int = 3
    dependencies: List[str] = field(default_factory=list)
    participants: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    state_impacts: Dict[str, float] = field(default_factory=dict)
    rationale: str = ""
    source: str = "heuristic"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "tick": self.tick,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "objective": self.objective,
            "summary": self.summary,
            "location": self.location,
            "target": self.target,
            "desired_duration": self.desired_duration,
            "priority": self.priority,
            "urgency": self.urgency,
            "risk_level": self.risk_level,
            "dependencies": self.dependencies,
            "participants": self.participants,
            "tags": self.tags,
            "state_impacts": self.state_impacts,
            "rationale": self.rationale,
            "source": self.source,
        }

    def to_log(self) -> Dict[str, Any]:
        return {
            "event_type": "intent_created",
            "round": self.tick,
            "tick": self.tick,
            "timestamp": now_iso(),
            "platform": "world",
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "title": self.objective,
            "summary": self.summary,
            "location": self.location,
            "target": self.target,
            "participants": self.participants,
            "priority": self.priority,
            "urgency": self.urgency,
            "duration_ticks": self.desired_duration,
            "dependencies": self.dependencies,
            "state_impacts": self.state_impacts,
            "action_type": "INTENT_CREATED",
            "action_args": self.to_dict(),
            "result": self.summary,
            "success": True,
        }


@dataclass
class WorldEvent:
    event_id: str
    tick: int
    title: str
    summary: str
    primary_agent_id: int
    primary_agent_name: str
    participants: List[str]
    participant_ids: List[int]
    source_intent_ids: List[str]
    priority: int = 3
    duration_ticks: int = 1
    resolves_at_tick: int = 1
    status: str = "queued"
    location: str = ""
    dependencies: List[str] = field(default_factory=list)
    state_impacts: Dict[str, float] = field(default_factory=dict)
    source: str = "heuristic"
    rationale: str = ""

    def to_state_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tick": self.tick,
            "title": self.title,
            "summary": self.summary,
            "primary_agent_id": self.primary_agent_id,
            "primary_agent_name": self.primary_agent_name,
            "participants": self.participants,
            "participant_ids": self.participant_ids,
            "source_intent_ids": self.source_intent_ids,
            "priority": self.priority,
            "duration_ticks": self.duration_ticks,
            "resolves_at_tick": self.resolves_at_tick,
            "status": self.status,
            "location": self.location,
            "dependencies": self.dependencies,
            "state_impacts": self.state_impacts,
            "source": self.source,
            "rationale": self.rationale,
        }

    @classmethod
    def from_state_dict(cls, payload: Dict[str, Any]) -> "WorldEvent":
        return cls(
            event_id=str(payload.get("event_id") or "").strip(),
            tick=safe_int(payload.get("tick", 0), default=0, lower=0),
            title=normalize_optional_text(payload.get("title")),
            summary=normalize_optional_text(payload.get("summary")),
            primary_agent_id=safe_int(payload.get("primary_agent_id", 0), default=0, lower=0),
            primary_agent_name=normalize_optional_text(payload.get("primary_agent_name")),
            participants=ensure_list(payload.get("participants")),
            participant_ids=[
                int(value)
                for value in payload.get("participant_ids", [])
                if isinstance(value, int) and value >= 0
            ],
            source_intent_ids=ensure_list(payload.get("source_intent_ids")),
            priority=safe_int(payload.get("priority", 3), default=3, lower=1, upper=5),
            duration_ticks=safe_int(payload.get("duration_ticks", 1), default=1, lower=1, upper=12),
            resolves_at_tick=safe_int(payload.get("resolves_at_tick", 1), default=1, lower=1),
            status=str(payload.get("status") or "queued").strip() or "queued",
            location=normalize_optional_text(payload.get("location")),
            dependencies=ensure_list(payload.get("dependencies")),
            state_impacts={
                str(key): safe_float(value)
                for key, value in (payload.get("state_impacts") or {}).items()
            },
            source=normalize_optional_text(payload.get("source")) or "heuristic",
            rationale=normalize_optional_text(payload.get("rationale")),
        )

    def start_log(self, active_count: int, queued_count: int) -> Dict[str, Any]:
        return {
            "event_type": "event_started",
            "round": self.tick,
            "tick": self.tick,
            "timestamp": now_iso(),
            "platform": "world",
            "event_id": self.event_id,
            "agent_id": self.primary_agent_id,
            "agent_name": self.primary_agent_name,
            "title": self.title,
            "summary": self.summary,
            "participants": self.participants,
            "priority": self.priority,
            "duration_ticks": self.duration_ticks,
            "remaining_ticks": max(self.resolves_at_tick - self.tick + 1, 0),
            "resolves_at_tick": self.resolves_at_tick,
            "location": self.location,
            "dependencies": self.dependencies,
            "status": "active",
            "state_impacts": self.state_impacts,
            "action_type": "EVENT_STARTED",
            "action_args": {
                **self.to_state_dict(),
                "active_events_count": active_count,
                "queued_events_count": queued_count,
            },
            "result": self.summary,
            "success": True,
        }

    def complete_log(self, tick: int, active_count: int, queued_count: int) -> Dict[str, Any]:
        return {
            "event_type": "event_completed",
            "round": tick,
            "tick": tick,
            "timestamp": now_iso(),
            "platform": "world",
            "event_id": self.event_id,
            "agent_id": self.primary_agent_id,
            "agent_name": self.primary_agent_name,
            "title": self.title,
            "summary": self.summary,
            "participants": self.participants,
            "priority": self.priority,
            "duration_ticks": self.duration_ticks,
            "remaining_ticks": 0,
            "resolves_at_tick": self.resolves_at_tick,
            "location": self.location,
            "dependencies": self.dependencies,
            "status": "completed",
            "state_impacts": self.state_impacts,
            "action_type": "EVENT_COMPLETED",
            "action_args": {
                **self.to_state_dict(),
                "completed_at_tick": tick,
                "active_events_count": active_count,
                "queued_events_count": queued_count,
            },
            "result": self.summary,
            "success": True,
        }

    def queue_log(self, queued_count: int) -> Dict[str, Any]:
        return {
            "event_type": "event_queued",
            "round": self.tick,
            "tick": self.tick,
            "timestamp": now_iso(),
            "platform": "world",
            "event_id": self.event_id,
            "agent_id": self.primary_agent_id,
            "agent_name": self.primary_agent_name,
            "title": self.title,
            "summary": self.summary,
            "participants": self.participants,
            "priority": self.priority,
            "duration_ticks": self.duration_ticks,
            "remaining_ticks": self.duration_ticks,
            "resolves_at_tick": self.resolves_at_tick,
            "location": self.location,
            "dependencies": self.dependencies,
            "status": "queued",
            "state_impacts": self.state_impacts,
            "queued_events_count": queued_count,
            "action_type": "EVENT_QUEUED",
            "action_args": self.to_state_dict(),
            "result": self.summary,
            "success": True,
        }


class WorldSimulationRuntime:
    CHECKPOINT_SCHEMA_VERSION = 1

    def __init__(
        self,
        config_path: str,
        max_rounds: Optional[int] = None,
        resume_from_checkpoint: bool = False,
    ):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.config_path = config_path
        self.simulation_dir = os.path.dirname(config_path)
        self.world_dir = os.path.join(self.simulation_dir, "world")
        os.makedirs(self.world_dir, exist_ok=True)

        self.actions_log = os.path.join(self.world_dir, "actions.jsonl")
        self.snapshots_log = os.path.join(self.world_dir, "state_snapshots.jsonl")
        self.world_state_path = os.path.join(self.world_dir, "world_state.json")
        self.checkpoint_path = os.path.join(self.world_dir, "checkpoint.json")

        time_config = self.config.get("time_config", {})
        configured_rounds = safe_int(
            time_config.get("total_ticks", time_config.get("total_rounds", 0)),
            default=0,
            lower=0,
        )
        if configured_rounds <= 0:
            total_hours = safe_int(time_config.get("total_simulation_hours", 12), default=12, lower=1)
            minutes_per_round = safe_int(time_config.get("minutes_per_round", 60), default=60, lower=1)
            configured_rounds = max(1, int(total_hours * 60 / minutes_per_round))

        # Fresh runs use max_rounds as a cap, while resumed runs use it as the
        # new absolute target tick so long-lived worlds can be extended beyond
        # the original config horizon.
        if resume_from_checkpoint and max_rounds:
            self.total_rounds = max_rounds
        else:
            self.total_rounds = min(configured_rounds, max_rounds) if max_rounds else configured_rounds
        self.minutes_per_round = safe_int(time_config.get("minutes_per_round", 60), default=60, lower=1)
        self.agent_configs = self.config.get("agent_configs", [])
        self.plot_threads = self.config.get("plot_threads", [])
        self.pressure_tracks = self.config.get("pressure_tracks", [])
        self.world_rules = self.config.get("world_rules", [])
        self.initial_world_state = self.config.get("initial_world_state", {})
        self.runtime_config = self.config.get("runtime_config", {})
        self.debug_capture_llm_io = str(
            self.runtime_config.get("debug_capture_llm_io", "")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.llm_trace_root_dir = os.path.join(self.world_dir, "debug", "llm_traces")
        self.llm_trace_run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.llm_trace_dir = os.path.join(self.llm_trace_root_dir, self.llm_trace_run_id)
        self.llm_trace_index_path = os.path.join(self.llm_trace_dir, "index.jsonl")
        self._llm_trace_counter = 0
        if self.debug_capture_llm_io:
            os.makedirs(self.llm_trace_dir, exist_ok=True)

        self.intent_agents_per_tick = safe_int(
            self.runtime_config.get("intent_agents_per_tick", len(self.agent_configs) or 1),
            default=len(self.agent_configs) or 1,
            lower=1,
        )
        self.intent_concurrency = safe_int(
            self.runtime_config.get("intent_concurrency", Config.WORLD_INTENT_CONCURRENCY),
            default=Config.WORLD_INTENT_CONCURRENCY,
            lower=1,
        )
        if self.agent_configs:
            self.intent_concurrency = min(self.intent_concurrency, len(self.agent_configs))
        self.resolver_cluster_size = safe_int(
            self.runtime_config.get(
                "resolver_cluster_size",
                min(max(3, self.intent_agents_per_tick // 3), 6),
            ),
            default=min(max(3, self.intent_agents_per_tick // 3), 6),
            lower=1,
            upper=max(self.intent_agents_per_tick, 1),
        )
        self.resolver_cluster_concurrency = safe_int(
            self.runtime_config.get("resolver_cluster_concurrency", 2),
            default=2,
            lower=1,
            upper=max(self.intent_agents_per_tick, 1),
        )
        self.actor_selection_core_ratio = clamp(
            safe_float(self.runtime_config.get("actor_selection_core_ratio", 0.35), 0.35),
            0.1,
            0.8,
        )
        self.actor_selection_hot_ratio = clamp(
            safe_float(self.runtime_config.get("actor_selection_hot_ratio", 0.35), 0.35),
            0.0,
            0.8,
        )

        actor_count = max(len(self.agent_configs), 1)
        self.max_active_events = safe_int(
            self.runtime_config.get("max_active_events", min(max(4, actor_count // 2), 10)),
            default=min(max(4, actor_count // 2), 10),
            lower=1,
        )
        self.max_queued_events = safe_int(
            self.runtime_config.get("max_queued_events", min(max(6, actor_count), 18)),
            default=min(max(6, actor_count), 18),
            lower=1,
        )
        self.max_event_duration = safe_int(
            self.runtime_config.get("max_event_duration_ticks", 3),
            default=3,
            lower=1,
            upper=6,
        )
        self.runtime_policy = Config.get_world_runtime_policy()
        self.strict_model_identity = self._runtime_bool(
            "strict_model_identity",
            self.runtime_policy["strict_model_identity"],
        )
        self.allow_semantic_fallback = self._runtime_bool(
            "allow_semantic_fallback",
            self.runtime_policy["allow_semantic_fallback"],
        )
        self.provider_preflight_check = self._runtime_bool(
            "provider_preflight_check",
            self.runtime_policy["provider_preflight_check"],
        )
        self.provider_max_retries = safe_int(
            self.runtime_config.get("provider_max_retries", self.runtime_policy["provider_max_retries"]),
            default=self.runtime_policy["provider_max_retries"],
            lower=1,
            upper=10,
        )
        self.provider_backoff_seconds = max(
            safe_float(
                self.runtime_config.get("provider_backoff_seconds", self.runtime_policy["provider_backoff_seconds"]),
                self.runtime_policy["provider_backoff_seconds"],
            ),
            0.5,
        )
        self.provider_healthcheck_timeout = max(
            safe_float(
                self.runtime_config.get(
                    "provider_healthcheck_timeout",
                    self.runtime_policy["provider_healthcheck_timeout"],
                ),
                self.runtime_policy["provider_healthcheck_timeout"],
            ),
            3.0,
        )
        self.provider_request_timeout = max(
            safe_float(
                self.runtime_config.get(
                    "provider_request_timeout",
                    self.runtime_policy["provider_request_timeout"],
                ),
                self.runtime_policy["provider_request_timeout"],
            ),
            5.0,
        )
        self.run_on_provider_degraded = self._runtime_str(
            "run_on_provider_degraded",
            self.runtime_policy["run_on_provider_degraded"],
        )
        self.resolver_on_failure = self._runtime_str(
            "resolver_on_failure",
            self.runtime_policy["resolver_on_failure"],
        )
        self.actor_on_failure = self._runtime_str(
            "actor_on_failure",
            self.runtime_policy["actor_on_failure"],
        )
        self.resolver_salvage_on_zero_accept = self._runtime_bool(
            "resolver_salvage_on_zero_accept",
            True,
        )
        self.default_actor_llm_selector = str(
            self.runtime_config.get("default_actor_llm_selector") or "WORLD_AGENT"
        ).strip() or "WORLD_AGENT"
        self.resolver_llm_selector = str(
            self.runtime_config.get("resolver_llm_selector") or "WORLD_RESOLVER"
        ).strip() or "WORLD_RESOLVER"

        seed_source = f"{self.config.get('simulation_id', '')}:{self.config.get('simulation_requirement', '')}"
        self.rng = random.Random(seed_source)

        self.actor_llm_cache: Dict[str, Optional[LLMClient]] = {}
        self.agent_llm = self._get_actor_llm()
        self.resolver_llm = self._build_llm(selector=self.resolver_llm_selector)

        self.active_events: Dict[str, WorldEvent] = {}
        self.queued_events: Dict[str, WorldEvent] = {}
        self.completed_events: List[WorldEvent] = []
        self.world_state = self._build_initial_state()
        self.last_snapshot: Dict[str, Any] = {}
        self.actor_last_selected_tick: Dict[int, int] = {}
        self.actor_last_event_tick: Dict[int, int] = {}
        self.actor_selection_counts: Dict[int, int] = {}
        self.actor_event_counts: Dict[int, int] = {}
        self._counted_event_ids: set[str] = set()
        self.actor_static_scores = {
            safe_int(agent.get("agent_id", 0), default=0, lower=0): self._actor_static_score(agent)
            for agent in self.agent_configs
        }

        self._event_counter = 0
        self._intent_counter = 0
        self._lifecycle_records = 0
        self.last_completed_tick = 0
        self.resume_from_checkpoint = resume_from_checkpoint

        if self.resume_from_checkpoint:
            self._load_checkpoint()

    def _runtime_bool(self, key: str, default: bool) -> bool:
        value = self.runtime_config.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _runtime_str(self, key: str, default: str) -> str:
        value = self.runtime_config.get(key)
        if value is None:
            return default.strip().lower()
        return str(value).strip().lower()

    def _semantic_fallback_enabled(self) -> bool:
        return (not self.strict_model_identity) or self.allow_semantic_fallback

    def _checkpoint_payload(self, status: str = "running") -> Dict[str, Any]:
        return {
            "schema_version": self.CHECKPOINT_SCHEMA_VERSION,
            "saved_at": now_iso(),
            "status": status,
            "simulation_id": self.config.get("simulation_id"),
            "config_path": self.config_path,
            "last_completed_tick": self.last_completed_tick,
            "run_total_rounds": self.total_rounds,
            "minutes_per_round": self.minutes_per_round,
            "active_events": [event.to_state_dict() for event in self.active_events.values()],
            "queued_events": [event.to_state_dict() for event in self.queued_events.values()],
            "completed_events": [event.to_state_dict() for event in self.completed_events],
            "world_state": self.world_state,
            "last_snapshot": self.last_snapshot,
            "actor_last_selected_tick": self.actor_last_selected_tick,
            "actor_last_event_tick": self.actor_last_event_tick,
            "actor_selection_counts": self.actor_selection_counts,
            "actor_event_counts": self.actor_event_counts,
            "counted_event_ids": sorted(self._counted_event_ids),
            "event_counter": self._event_counter,
            "intent_counter": self._intent_counter,
            "lifecycle_records": self._lifecycle_records,
            "rng_state": self.rng.getstate(),
        }

    def _write_checkpoint(self, status: str = "running") -> None:
        with open(self.checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(self._checkpoint_payload(status=status), f, ensure_ascii=False, indent=2)

    def _load_checkpoint(self) -> None:
        if not os.path.exists(self.checkpoint_path):
            raise FileNotFoundError(f"checkpoint 不存在，无法续跑: {self.checkpoint_path}")

        with open(self.checkpoint_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        base_world_state = self._build_initial_state()
        checkpoint_world_state = payload.get("world_state") or {}
        if isinstance(checkpoint_world_state, dict):
            base_world_state.update(checkpoint_world_state)
        self.world_state = base_world_state

        self.active_events = {}
        for event_payload in payload.get("active_events", []):
            event = WorldEvent.from_state_dict(event_payload)
            if event.event_id:
                self.active_events[event.event_id] = event

        self.queued_events = {}
        for event_payload in payload.get("queued_events", []):
            event = WorldEvent.from_state_dict(event_payload)
            if event.event_id:
                self.queued_events[event.event_id] = event

        self.completed_events = []
        for event_payload in payload.get("completed_events", []):
            event = WorldEvent.from_state_dict(event_payload)
            if event.event_id:
                self.completed_events.append(event)

        self.last_snapshot = payload.get("last_snapshot") or {}
        self.actor_last_selected_tick = json_keyed_int_dict(payload.get("actor_last_selected_tick"))
        self.actor_last_event_tick = json_keyed_int_dict(payload.get("actor_last_event_tick"))
        self.actor_selection_counts = json_keyed_int_dict(payload.get("actor_selection_counts"))
        self.actor_event_counts = json_keyed_int_dict(payload.get("actor_event_counts"))
        self._counted_event_ids = set(ensure_list(payload.get("counted_event_ids")))
        self._event_counter = safe_int(payload.get("event_counter", 0), default=0, lower=0)
        self._intent_counter = safe_int(payload.get("intent_counter", 0), default=0, lower=0)
        self._lifecycle_records = safe_int(payload.get("lifecycle_records", 0), default=0, lower=0)
        self.last_completed_tick = safe_int(payload.get("last_completed_tick", 0), default=0, lower=0)

        rng_state = payload.get("rng_state")
        if rng_state is not None:
            self.rng.setstate(json_list_to_tuple_tree(rng_state))

    def _truncate_logs_to_checkpoint(self) -> None:
        self._truncate_actions_log()
        self._truncate_snapshots_log()
        if self.last_snapshot:
            with open(self.world_state_path, "w", encoding="utf-8") as f:
                json.dump(self.last_snapshot, f, ensure_ascii=False, indent=2)
        elif os.path.exists(self.world_state_path):
            os.remove(self.world_state_path)

    def _truncate_actions_log(self) -> None:
        if not os.path.exists(self.actions_log):
            return

        kept_lines: List[str] = []
        with open(self.actions_log, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                tick = payload.get("tick", payload.get("round"))
                try:
                    tick_value = int(tick)
                except (TypeError, ValueError):
                    tick_value = None
                if tick_value is None:
                    # Drop stale run-lifecycle markers from previous resume
                    # attempts so diagnostics reflect the current run history.
                    if payload.get("event_type") in {"simulation_resumed", "simulation_end"}:
                        continue
                    kept_lines.append(json.dumps(payload, ensure_ascii=False) + "\n")
                    continue
                if tick_value <= self.last_completed_tick:
                    kept_lines.append(json.dumps(payload, ensure_ascii=False) + "\n")

        with open(self.actions_log, "w", encoding="utf-8") as f:
            f.writelines(kept_lines)

    def _truncate_snapshots_log(self) -> None:
        if not os.path.exists(self.snapshots_log):
            return

        kept_lines: List[str] = []
        with open(self.snapshots_log, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                raw_tick = payload.get("tick", payload.get("round"))
                try:
                    tick = int(raw_tick)
                except (TypeError, ValueError):
                    tick = -1
                if 0 <= tick <= self.last_completed_tick:
                    kept_lines.append(json.dumps(payload, ensure_ascii=False) + "\n")

        with open(self.snapshots_log, "w", encoding="utf-8") as f:
            f.writelines(kept_lines)

    def _build_llm(
        self,
        namespace: Optional[str] = None,
        selector: Optional[str] = None,
    ) -> Optional[LLMClient]:
        try:
            if selector:
                return LLMClient.from_selector(selector)
            return LLMClient.from_namespace(namespace)
        except Exception:
            return None

    def _actor_provider_role(self, selector: Optional[str]) -> str:
        normalized = str(selector or self.default_actor_llm_selector).strip()
        if not normalized or normalized.upper() == "WORLD_AGENT":
            return "world_agent"
        return f"world_agent:{normalized}"

    def _get_actor_llm(self, agent: Optional[Dict[str, Any]] = None) -> Optional[LLMClient]:
        selector = str(
            (agent or {}).get("llm_selector") or self.default_actor_llm_selector
        ).strip() or self.default_actor_llm_selector
        if selector not in self.actor_llm_cache:
            self.actor_llm_cache[selector] = self._build_llm(selector=selector)
        return self.actor_llm_cache[selector]

    async def _preflight_world_providers(self) -> None:
        if not self.provider_preflight_check:
            return
        actor_selectors = {
            str(agent.get("llm_selector") or self.default_actor_llm_selector).strip() or self.default_actor_llm_selector
            for agent in self.agent_configs
        } or {self.default_actor_llm_selector}
        for selector in sorted(actor_selectors):
            await self._wait_for_provider_recovery(
                provider_role=self._actor_provider_role(selector),
                llm=self._get_actor_llm({"llm_selector": selector}),
                tick=0,
                phase="preflight",
                reason="startup preflight",
                context="agent_intent_generation",
            )
        await self._wait_for_provider_recovery(
            provider_role="world_resolver",
            llm=self.resolver_llm,
            tick=0,
            phase="preflight",
            reason="startup preflight",
            context="resolver",
        )

    async def _wait_for_provider_recovery(
        self,
        provider_role: str,
        llm: Optional[LLMClient],
        tick: int,
        phase: str,
        reason: str,
        context: str = "",
    ) -> None:
        if llm is None:
            raise RuntimeError(f"{provider_role} LLM 未配置，无法在 strict world 模式下运行")

        while True:
            try:
                await asyncio.to_thread(
                    llm.health_check,
                    self.provider_healthcheck_timeout,
                )
                if phase != "preflight" or reason != "startup preflight":
                    self._write_meta_event(
                        {
                            "event_type": "provider_recovered",
                            "timestamp": now_iso(),
                            "simulation_mode": "world",
                            "round": tick,
                            "tick": tick,
                            "phase": "provider_recovered",
                            "provider_role": provider_role,
                            "summary": f"{provider_role} provider recovered and world runtime resumes.",
                            "reason": reason,
                            "context": context,
                        }
                    )
                return
            except Exception as exc:
                if phase == "preflight" and self.run_on_provider_degraded == "fail":
                    raise RuntimeError(
                        f"{provider_role} provider preflight failed: {exc}"
                    ) from exc

                reason = str(exc)
                self._write_meta_event(
                    {
                        "event_type": "provider_waiting",
                        "timestamp": now_iso(),
                        "simulation_mode": "world",
                        "round": tick,
                        "tick": tick,
                        "phase": "waiting_provider",
                        "provider_role": provider_role,
                        "summary": (
                            f"{provider_role} provider unavailable, waiting "
                            f"{self.provider_backoff_seconds:.1f}s before retry."
                        ),
                        "reason": reason,
                        "context": context,
                        "wait_seconds": self.provider_backoff_seconds,
                    }
                )
                await asyncio.sleep(self.provider_backoff_seconds)

    async def _call_llm_json_with_retry(
        self,
        *,
        llm: Optional[LLMClient],
        provider_role: str,
        tick: int,
        phase: str,
        context: str,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        if llm is None:
            raise RuntimeError(f"{provider_role} LLM 未配置")

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.provider_max_retries + 1):
            try:
                return await asyncio.to_thread(
                    llm.chat_json_with_meta,
                    messages,
                    temperature,
                    max_tokens,
                    self.provider_request_timeout,
                )
            except InvalidJSONResponseError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt >= self.provider_max_retries:
                    break
                await asyncio.sleep(self.provider_backoff_seconds * attempt)

        raise RuntimeError(
            f"{provider_role} failed after {self.provider_max_retries} attempts: {last_exc}"
        ) from last_exc

    def _build_initial_state(self) -> Dict[str, Any]:
        return build_initial_world_state(
            plot_threads=self.plot_threads,
            pressure_tracks=self.pressure_tracks,
            initial_world_state=self.initial_world_state,
        )

    async def run(self) -> None:
        if self.resume_from_checkpoint:
            self._truncate_logs_to_checkpoint()
            self._write_meta_event(
                {
                    "event_type": "simulation_resumed",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "resume_from_tick": self.last_completed_tick,
                    "remaining_rounds": max(self.total_rounds - self.last_completed_tick, 0),
                    "total_rounds": self.total_rounds,
                    "phase": "resuming",
                    "world_state": self._world_state_brief(),
                }
            )
        else:
            self._reset_output_files()
            self._write_meta_event(
                {
                    "event_type": "simulation_start",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "total_rounds": self.total_rounds,
                    "agents_count": len(self.agent_configs),
                    "runtime_config": {
                        "intent_agents_per_tick": self.intent_agents_per_tick,
                        "intent_concurrency": self.intent_concurrency,
                        "resolver_cluster_size": self.resolver_cluster_size,
                        "resolver_cluster_concurrency": self.resolver_cluster_concurrency,
                        "actor_selection_core_ratio": self.actor_selection_core_ratio,
                        "actor_selection_hot_ratio": self.actor_selection_hot_ratio,
                        "max_active_events": self.max_active_events,
                        "max_queued_events": self.max_queued_events,
                        "max_event_duration_ticks": self.max_event_duration,
                        "strict_model_identity": self.strict_model_identity,
                        "allow_semantic_fallback": self.allow_semantic_fallback,
                        "provider_preflight_check": self.provider_preflight_check,
                        "provider_max_retries": self.provider_max_retries,
                        "provider_backoff_seconds": self.provider_backoff_seconds,
                        "provider_healthcheck_timeout": self.provider_healthcheck_timeout,
                        "provider_request_timeout": self.provider_request_timeout,
                        "run_on_provider_degraded": self.run_on_provider_degraded,
                        "resolver_on_failure": self.resolver_on_failure,
                        "actor_on_failure": self.actor_on_failure,
                        "resolver_salvage_on_zero_accept": self.resolver_salvage_on_zero_accept,
                        "default_actor_llm_selector": self.default_actor_llm_selector,
                        "resolver_llm_selector": self.resolver_llm_selector,
                    },
                    "world_models": {
                        "agent_model": Config.get_llm_config(selector=self.default_actor_llm_selector).get("model_name"),
                        "agent_provider": Config.get_llm_config(selector=self.default_actor_llm_selector).get("provider_id"),
                        "agent_profile": Config.get_llm_config(selector=self.default_actor_llm_selector).get("profile_id"),
                        "resolver_model": Config.get_llm_config(selector=self.resolver_llm_selector).get("model_name"),
                        "resolver_provider": Config.get_llm_config(selector=self.resolver_llm_selector).get("provider_id"),
                        "resolver_profile": Config.get_llm_config(selector=self.resolver_llm_selector).get("profile_id"),
                        "actor_selectors": sorted(
                            {
                                str(agent.get("llm_selector") or self.default_actor_llm_selector).strip()
                                or self.default_actor_llm_selector
                                for agent in self.agent_configs
                            }
                            or {self.default_actor_llm_selector}
                        ),
                    },
                }
            )

        if self.last_completed_tick >= self.total_rounds:
            self._write_meta_event(
                {
                    "event_type": "simulation_end",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "total_rounds": self.total_rounds,
                    "total_actions": self._lifecycle_records,
                    "world_state": self.world_state,
                    "active_events_count": len(self.active_events),
                    "queued_events_count": len(self.queued_events),
                    "completed_events_count": len(self.completed_events),
                }
            )
            self._write_checkpoint(status="completed")
            return

        await self._preflight_world_providers()

        for tick in range(self.last_completed_tick + 1, self.total_rounds + 1):
            scene_title = self._scene_title(tick)

            self._promote_queued_events(tick)
            self._write_meta_event(
                {
                    "event_type": "tick_start",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "round": tick,
                    "scene_title": scene_title,
                    "phase": "intent_generation",
                    "active_events_count": len(self.active_events),
                    "queued_events_count": len(self.queued_events),
                    "completed_events_count": len(self.completed_events),
                }
            )

            intents = await self._generate_tick_intents(tick, scene_title)
            resolution = await self._resolve_tick_intents(tick, scene_title, intents)

            for event in resolution["accepted_events"]:
                self._accept_event(event)

            completed_this_tick = self._complete_due_events(tick)
            snapshot = self._capture_snapshot(
                tick=tick,
                scene_title=scene_title,
                intents=intents,
                resolution=resolution,
                completed_this_tick=completed_this_tick,
            )
            self._write_snapshot(snapshot)

            self._write_meta_event(
                {
                    "event_type": "tick_end",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "round": tick,
                    "scene_title": scene_title,
                    "phase": "tick_complete",
                    "simulated_hours": round(tick * self.minutes_per_round / 60, 2),
                    "summary": snapshot["summary"],
                    "active_events_count": snapshot["metrics"]["active_events_count"],
                    "queued_events_count": snapshot["metrics"]["queued_events_count"],
                    "completed_events_count": snapshot["metrics"]["completed_events_count"],
                    "world_state": snapshot["world_state"],
                }
            )
            self.last_completed_tick = tick
            self._write_checkpoint(status="running")
            await asyncio.sleep(0.05)

        self._write_meta_event(
            {
                "event_type": "simulation_end",
                "timestamp": now_iso(),
                "simulation_mode": "world",
                "total_rounds": self.total_rounds,
                "total_actions": self._lifecycle_records,
                "world_state": self.world_state,
                "active_events_count": len(self.active_events),
                "queued_events_count": len(self.queued_events),
                "completed_events_count": len(self.completed_events),
                "unresolved_events": [
                    event.to_state_dict()
                    for event in list(self.active_events.values()) + list(self.queued_events.values())
                ][:20],
            }
        )
        self._write_checkpoint(status="completed")

    def _reset_output_files(self) -> None:
        for path in (self.actions_log, self.snapshots_log, self.world_state_path, self.checkpoint_path):
            if os.path.exists(path):
                os.remove(path)

    def _scene_title(self, tick: int) -> str:
        thread = self.plot_threads[(tick - 1) % len(self.plot_threads)] if self.plot_threads else None
        if thread:
            return thread.get("title", f"World Tick {tick}")
        return f"World Tick {tick}"

    def _agent_id(self, agent: Dict[str, Any]) -> int:
        return safe_int(agent.get("agent_id", 0), default=0, lower=0)

    def _agent_name(self, agent: Dict[str, Any]) -> str:
        agent_id = self._agent_id(agent)
        return str(agent.get("entity_name") or agent.get("username") or f"Actor {agent_id}")

    def _is_placeholder_text(self, value: Any, scene_title: str = "") -> bool:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return True
        if normalized in {
            "none",
            "null",
            "unknown",
            "not specified",
            "not provided",
            "n/a",
            "未指定",
            "未知",
            "无",
            "暂无",
            "none.",
        }:
            return True
        if scene_title and normalized == str(scene_title).strip().lower():
            return True
        return False

    def _actor_static_score(self, agent: Dict[str, Any]) -> int:
        entity_type = str(agent.get("entity_type") or "").strip().lower()
        score = 0
        score += min(len(ensure_list(agent.get("connected_entities"))), 6) * 3
        score += min(len(ensure_list(agent.get("story_hooks"))), 4) * 2
        score += min(len(ensure_list(agent.get("driving_goals"))), 4)
        score += min(len(ensure_list(agent.get("resources"))), 3)
        score += 4 if entity_type in {"faction", "organization"} else 2 if entity_type else 0
        if not self._is_placeholder_text(agent.get("home_location")):
            score += 2
        if clip_text(agent.get("summary", ""), 220):
            score += 1
        return score

    def _actor_selection_gap(self, agent_id: int, tick: int) -> int:
        last_tick = self.actor_last_selected_tick.get(agent_id)
        if last_tick is None:
            return tick + 2
        return max(tick - last_tick, 1)

    def _actor_heat_score(self, agent: Dict[str, Any], tick: int, scene_title: str) -> int:
        agent_id = self._agent_id(agent)
        agent_name = self._agent_name(agent)
        identity_terms = dedupe_keep_order(
            [agent_name, *ensure_list(agent.get("connected_entities"))[:4]]
        )
        score = 0
        scene_blob = f"{scene_title}\n" + "\n".join(self.world_state.get("focus_threads", [])[:6])
        for term in identity_terms:
            if term and term in scene_blob:
                score += 3

        recent_events = list(self.active_events.values()) + list(self.queued_events.values()) + self.completed_events[-6:]
        for event in recent_events:
            event_blob = f"{event.title}\n{event.summary}\n{event.location}".lower()
            participant_set = {participant.lower() for participant in event.participants}
            if agent_name.lower() in participant_set:
                score += 8 if event.status == "active" else 5
                continue
            for term in identity_terms[1:]:
                normalized = term.lower()
                if normalized and (normalized in participant_set or normalized in event_blob):
                    score += 3 if event.status == "active" else 2
                    break

        last_event_tick = self.actor_last_event_tick.get(agent_id)
        if last_event_tick is not None:
            event_gap = max(tick - last_event_tick, 0)
            if event_gap <= 1:
                score += 6
            elif event_gap <= 3:
                score += 3
        score += min(self.actor_event_counts.get(agent_id, 0), 3)
        score += min(self._actor_selection_gap(agent_id, tick), 4)
        return score

    def _actors_for_tick(self, tick: int) -> List[Dict[str, Any]]:
        if not self.agent_configs:
            return []
        scene_title = self._scene_title(tick)
        target_count = min(self.intent_agents_per_tick, len(self.agent_configs))
        if target_count >= len(self.agent_configs):
            selected = list(self.agent_configs)
        else:
            scored_agents: List[Dict[str, Any]] = []
            for agent in self.agent_configs:
                agent_id = self._agent_id(agent)
                scored_agents.append(
                    {
                        "agent": agent,
                        "agent_id": agent_id,
                        "static": self.actor_static_scores.get(agent_id, 0),
                        "heat": self._actor_heat_score(agent, tick, scene_title),
                        "gap": self._actor_selection_gap(agent_id, tick),
                        "selected_count": self.actor_selection_counts.get(agent_id, 0),
                    }
                )
            selected_ids = set()
            selected: List[Dict[str, Any]] = []

            def extend_bucket(candidates: List[Dict[str, Any]], limit: int) -> None:
                if len(selected) >= limit:
                    return
                for candidate in candidates:
                    if len(selected) >= limit:
                        return
                    agent_id = candidate["agent_id"]
                    if agent_id in selected_ids:
                        continue
                    selected.append(candidate["agent"])
                    selected_ids.add(agent_id)

            core_count = min(
                target_count,
                max(1, int(round(target_count * self.actor_selection_core_ratio))),
            )
            remaining_after_core = max(target_count - core_count, 0)
            hot_count = min(
                remaining_after_core,
                int(round(target_count * self.actor_selection_hot_ratio)),
            )

            core_pool = sorted(
                scored_agents,
                key=lambda item: (
                    item["static"] + item["heat"],
                    item["static"],
                    item["gap"],
                    -item["selected_count"],
                    self.rng.random(),
                ),
                reverse=True,
            )
            extend_bucket(core_pool, core_count)

            hot_pool = [
                agent
                for agent in sorted(
                    scored_agents,
                    key=lambda item: (
                        item["heat"],
                        item["gap"],
                        item["static"],
                        -item["selected_count"],
                        self.rng.random(),
                    ),
                    reverse=True,
                )
                if agent["heat"] > 0
            ]
            extend_bucket(hot_pool, core_count + hot_count)

            remaining_slots = max(target_count - len(selected), 0)
            fringe_pool = [item for item in scored_agents if item["agent_id"] not in selected_ids]
            if remaining_slots > 0 and fringe_pool:
                fringe_weights = [
                    max(
                        (item["gap"] * 1.2)
                        + (item["heat"] * 0.35)
                        + (item["static"] * 0.15)
                        - (item["selected_count"] * 0.08),
                        0.1,
                    )
                    for item in fringe_pool
                ]
                fringe_sample = weighted_sample_without_replacement(
                    self.rng,
                    fringe_pool,
                    fringe_weights,
                    remaining_slots,
                )
                extend_bucket(fringe_sample, target_count)

        for agent in selected:
            agent_id = self._agent_id(agent)
            self.actor_last_selected_tick[agent_id] = tick
            self.actor_selection_counts[agent_id] = self.actor_selection_counts.get(agent_id, 0) + 1
        return sorted(selected, key=lambda item: item.get("agent_id", 0))

    async def _generate_tick_intents(self, tick: int, scene_title: str) -> List[ActorIntent]:
        actors = self._actors_for_tick(tick)
        if not actors:
            return []

        semaphore = asyncio.Semaphore(max(self.intent_concurrency, 1))

        async def generate(agent: Dict[str, Any]) -> Optional[ActorIntent]:
            async with semaphore:
                return await self._generate_single_intent(tick, scene_title, agent)

        intents: List[ActorIntent] = []
        tasks = [asyncio.create_task(generate(agent)) for agent in actors]
        for task in asyncio.as_completed(tasks):
            intent = await task
            if not intent:
                continue
            intents.append(intent)
            self._write_action(intent.to_log())
        return intents

    def _actor_intent_system_prompt(self) -> str:
        return (
            "你是一个世界观自动推进系统中的角色意图生成器。"
            "你的任务不是分析局势，而是替该角色给出本 tick 会执行的一个具体下一步动作。"
            "必须输出一个 JSON 对象，字段只允许包含："
            "objective, summary, location, target, desired_duration, priority, urgency, "
            "risk_level, dependencies, participants, tags, state_impacts, rationale。"
            "硬约束：objective 必须是具体动作，summary 必须描述该动作如何推进局势；"
            "不要输出前言、标题、分析、解释、markdown、代码块、注释、替代 schema。"
            "不要把字段留空来逃避决策；如果信息不足，也要基于角色设定给出最可信的一步行动。"
            "objective 不得是“行动方案/核心判断/摘要/计划”之类元文本，"
            "也不得复述 JSON 字段名。"
            "desired_duration 取 1 到 3 的整数，priority/urgency/risk_level 取 1 到 5 的整数。"
            "state_impacts 只允许 conflict, scarcity, legitimacy, momentum, stability 这几个键，"
            "值在 -0.25 到 0.25 之间。"
            "只输出一个 JSON 对象。"
        )

    def _resolver_system_prompt(self) -> str:
        return (
            "你是世界模拟的并发事件裁决器。当前只处理同一 front 的一小簇角色意图，"
            "请把这些意图整理成可并行推进的世界事件。"
            "只允许输出一个 JSON 对象，字段严格为 accepted_events, deferred_intents, rejected_intents。"
            "accepted_events 每项必须包含 title, summary, owner_intent_id, supporting_intent_ids, "
            "priority, duration_ticks, location, dependencies, participants, state_impacts, rationale。"
            "deferred_intents 与 rejected_intents 每项必须包含 intent_id, reason。"
            "coverage 规则：输入中的每个 intent_id 必须且只能出现一次。"
            "如果某个意图被接受，它必须通过 owner_intent_id 或 supporting_intent_ids 出现在 accepted_events 中；"
            "如果不被接受，就必须进入 deferred_intents 或 rejected_intents。"
            "若事件因为容量或依赖暂时不能启动，仍要把它放进 accepted_events，并在 rationale 中说明 queued。"
            "禁止输出 active_events、resolved_events、queued_events、scene_summary、resolution_summary、"
            "projected_trends、selection_summary 或任何 summary-only 结果。"
            "禁止输出 schema 模板、占位符或示例值，例如 string、0、0.0、[]、event_id or intent_id。"
            "如果存在 priority>=4 且明确可执行的行动意图，除非它纯观察或完全无动作，否则至少接受一个事件。"
            "不要因为信息不完整而把整簇意图全部 reject。"
        )

    async def _generate_single_intent(
        self,
        tick: int,
        scene_title: str,
        agent: Dict[str, Any],
    ) -> Optional[ActorIntent]:
        agent_llm = self._get_actor_llm(agent)
        provider_role = self._actor_provider_role(agent.get("llm_selector"))
        if agent_llm is None:
            if self._semantic_fallback_enabled():
                return self._heuristic_intent(tick, scene_title, agent)
            raise RuntimeError("WORLD_AGENT LLM 未配置")

        active_brief = [self._event_brief(event) for event in list(self.active_events.values())[:6]]
        queued_brief = [self._event_brief(event) for event in list(self.queued_events.values())[:6]]
        completed_brief = [self._event_brief(event) for event in self.completed_events[-4:]]
        agent_name = agent.get("entity_name") or agent.get("username") or "Unknown Actor"
        prompt_payload = {
            "tick": tick,
            "scene_title": scene_title,
            "world_state": self._world_state_brief(),
            "world_rules": clip_list(self.world_rules, 90, 4),
            "plot_threads": self.plot_threads[:4],
            "active_events": active_brief,
            "queued_events": queued_brief,
            "recent_completed_events": completed_brief,
            "actor_profile": {
                "agent_id": agent.get("agent_id", 0),
                "entity_name": agent_name,
                "entity_type": agent.get("entity_type", "Actor"),
                "public_role": clip_text(agent.get("public_role", ""), 120),
                "driving_goals": clip_list(agent.get("driving_goals", []), 80, 3),
                "resources": clip_list(agent.get("resources", []), 80, 3),
                "constraints": clip_list(agent.get("constraints", []), 80, 3),
                "connected_entities": clip_list(agent.get("connected_entities", []), 40, 5),
                "story_hooks": clip_list(agent.get("story_hooks", []), 80, 3),
                "home_location": clip_text(agent.get("home_location", ""), 60),
                "summary": clip_text(agent.get("summary", ""), 180),
            },
            "output_contract": {
                "must_choose_one_concrete_action": True,
                "forbid_meta_text": True,
                "objective_non_empty": True,
                "summary_non_empty": True,
                "unknown_fields_should_be_best_effort_not_blank": True,
            },
        }

        try:
            messages = [
                {
                    "role": "system",
                    "content": self._actor_intent_system_prompt(),
                },
                {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
            ]

            response_meta: Optional[Dict[str, Any]] = None
            for attempt in range(1, self.provider_max_retries + 1):
                try:
                    response_meta = await asyncio.to_thread(
                        agent_llm.chat_json_with_meta,
                        messages,
                        0.65,
                        320,
                        self.provider_request_timeout,
                    )
                    break
                except Exception as exc:
                    if attempt >= self.provider_max_retries:
                        raise
                    await asyncio.sleep(self.provider_backoff_seconds * attempt)

            response = dict((response_meta or {}).get("data") or {})
            intent = self._intent_from_response(tick, agent, response, source="llm")
            if self._intent_is_low_signal(intent):
                fallback_intent = self._heuristic_intent(tick, scene_title, agent)
                fallback_intent.source = "llm_low_signal_fallback"
                fallback_intent.rationale = (
                    f"low-signal json fallback: {str((response_meta or {}).get('json_candidate') or '')[:180]}"
                )
                self._write_llm_trace(
                    stage="actor_intent",
                    tick=tick,
                    provider_role=provider_role,
                    llm=agent_llm,
                    status="low_signal_fallback",
                    reason_code="actor_low_signal_json",
                    context=scene_title,
                    messages=messages,
                    raw_response=str((response_meta or {}).get("raw_response") or ""),
                    json_candidate=str((response_meta or {}).get("json_candidate") or ""),
                    repaired_response=str((response_meta or {}).get("repaired_response") or ""),
                    repaired_candidate=str((response_meta or {}).get("repaired_candidate") or ""),
                    parsed_json=response,
                    outcome=fallback_intent.to_dict(),
                )
                return fallback_intent

            self._write_llm_trace(
                stage="actor_intent",
                tick=tick,
                provider_role=provider_role,
                llm=agent_llm,
                status="accepted",
                reason_code="actor_json",
                context=scene_title,
                messages=messages,
                raw_response=str((response_meta or {}).get("raw_response") or ""),
                json_candidate=str((response_meta or {}).get("json_candidate") or ""),
                repaired_response=str((response_meta or {}).get("repaired_response") or ""),
                repaired_candidate=str((response_meta or {}).get("repaired_candidate") or ""),
                parsed_json=response,
                outcome=intent.to_dict(),
            )
            return intent
        except Exception as exc:
            if isinstance(exc, InvalidJSONResponseError):
                raw_text = exc.raw_response or exc.repaired_response
                if raw_text:
                    recovered_intent = self._intent_from_text_output(
                        tick=tick,
                        scene_title=scene_title,
                        agent=agent,
                        raw_text=raw_text,
                    )
                    if self._intent_is_low_signal(recovered_intent):
                        recovered_intent = self._heuristic_intent(tick, scene_title, agent)
                        recovered_intent.source = "llm_low_signal_fallback"
                        recovered_intent.rationale = f"invalid-json low-signal fallback: {raw_text[:180]}"
                        reason_code = "actor_invalid_json_low_signal_fallback"
                        status = "low_signal_fallback"
                    else:
                        reason_code = "actor_invalid_json_text_recovered"
                        status = "text_recovered"

                    self._write_llm_trace(
                        stage="actor_intent",
                        tick=tick,
                        provider_role=provider_role,
                        llm=agent_llm,
                        status=status,
                        reason_code=reason_code,
                        context=scene_title,
                        messages=messages,
                        raw_response=str(exc.raw_response or ""),
                        repaired_response=str(exc.repaired_response or ""),
                        outcome=recovered_intent.to_dict(),
                        error=str(exc),
                    )
                    return recovered_intent
            if self._semantic_fallback_enabled():
                fallback_intent = self._heuristic_intent(tick, scene_title, agent)
                self._write_llm_trace(
                    stage="actor_intent",
                    tick=tick,
                    provider_role=provider_role,
                    llm=agent_llm,
                    status="heuristic_fallback",
                    reason_code="actor_exception_semantic_fallback",
                    context=scene_title,
                    messages=messages,
                    outcome=fallback_intent.to_dict(),
                    error=str(exc),
                )
                return fallback_intent
            if self.actor_on_failure == "defer":
                self._write_llm_trace(
                    stage="actor_intent",
                    tick=tick,
                    provider_role=provider_role,
                    llm=agent_llm,
                    status="deferred",
                    reason_code="actor_provider_error_deferred",
                    context=scene_title,
                    messages=messages,
                    error=str(exc),
                    extra={
                        "agent_id": agent.get("agent_id", 0),
                        "agent_name": agent_name,
                    },
                )
                self._write_meta_event(
                    {
                        "event_type": "intent_deferred",
                        "timestamp": now_iso(),
                        "simulation_mode": "world",
                        "round": tick,
                        "tick": tick,
                        "phase": "waiting_provider",
                        "provider_role": provider_role,
                        "agent_id": agent.get("agent_id", 0),
                        "agent_name": agent_name,
                        "summary": (
                            f"{agent_name} 在本 tick 的意图被延后，原因是 {provider_role} 暂时不可用。"
                        ),
                        "reason": str(exc),
                        "context": scene_title,
                    }
                )
                return None
            if self.actor_on_failure == "pause":
                await self._wait_for_provider_recovery(
                    provider_role=provider_role,
                    llm=agent_llm,
                    tick=tick,
                    phase="waiting_provider",
                    reason=str(exc),
                    context=agent_name,
                )
                return await self._generate_single_intent(tick, scene_title, agent)
            raise

    def _has_json_key_leakage(self, text: Any) -> bool:
        normalized = normalize_optional_text(text)
        if not normalized:
            return False
        return bool(
            re.search(
                r'(?i)(?:^|[\s{[,])(?:objective|summary|title|name|location|target|participants|supporting_intent_ids|owner_intent_id|rationale|事件名|行动名|行动目标|摘要|地点|对象)\s*["\'“”`]*\s*[:：]',
                normalized,
            )
        )

    def _extract_json_style_field(
        self,
        raw_text: Any,
        field_names: Sequence[str],
    ) -> str:
        text = str(raw_text or "")
        if not text.strip():
            return ""

        for field_name in field_names:
            patterns = [
                rf'["\'“”`]?{re.escape(field_name)}["\'“”`]?\s*[:：]\s*"((?:\\.|[^"\\]){{1,640}})"',
                rf'["\'“”`]?{re.escape(field_name)}["\'“”`]?\s*[:：]\s*“([^”]{{1,640}})”',
                rf'["\'“”`]?{re.escape(field_name)}["\'“”`]?\s*[:：]\s*([^\n\r{{}}]{{1,320}})',
            ]
            for pattern in patterns:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if not match:
                    continue
                candidate = (
                    match.group(1)
                    .replace('\\"', '"')
                    .replace("\\n", " ")
                    .replace("\\t", " ")
                    .replace("\\/", "/")
                    .strip()
                )
                candidate = re.sub(r'["\'“”`]\s*,?\s*$', "", candidate).strip(" ,，;；")
                if candidate:
                    return candidate
        return ""

    def _clean_intent_candidate(self, text: Any, agent_name: str = "") -> str:
        cleaned = normalize_optional_text(text)
        if not cleaned:
            return ""
        cleaned = (
            cleaned.replace('\\"', '"')
            .replace("\\n", " ")
            .replace("\\t", " ")
            .replace("\\/", "/")
        )
        cleaned = re.sub(r"[*_`]+", "", cleaned)
        cleaned = cleaned.lstrip("｜|／/")
        cleaned = re.sub(r"^[#>*\-\s]+", "", cleaned).strip()
        cleaned = re.sub(r"^\d+\s*[.)、：:]\s*", "", cleaned)
        cleaned = cleaned.strip(" -*_`\"'“”[]()【】{}")
        if agent_name:
            cleaned = re.sub(
                rf"^{re.escape(agent_name)}\s*(?:本\s*(?:tick|轮|回合))?\s*(?:角色)?(?:行动|意图)?\s*[：:]?\s*",
                "",
                cleaned,
                flags=re.IGNORECASE,
            )
        for _ in range(3):
            updated = re.sub(
                r'^(?:["\'“”`{[]\s*)*(?:objective|summary|title|name|location|target|participants|supporting_intent_ids|owner_intent_id|rationale|事件名|行动名|行动目标|摘要|地点|对象)\s*["\'“”`]*\s*[：:]\s*',
                "",
                cleaned,
                flags=re.IGNORECASE,
            )
            updated = re.sub(
                r"^(?:基于当前局势|结合当前局势|考虑当前局势|在当前局势下|以下是(?:基于当前局势)?(?:为)?|作为[^，,:：]{1,24})[\s，,:：-]*",
                "",
                updated,
                flags=re.IGNORECASE,
            )
            updated = re.sub(
                r"^(?:[｜|]\s*)?(?:第\s*\d+\s*轮|tick\s*\d+)[\s，,:：-]*",
                "",
                updated,
                flags=re.IGNORECASE,
            )
            updated = re.sub(
                r"^(?:本\s*(?:tick|轮|回合)\s*)?(?:行动|意图)(?:建议|提案|方案|目标|方向|倾向)?\s*[：:]?\s*",
                "",
                updated,
                flags=re.IGNORECASE,
            )
            updated = re.sub(
                r"^(?:核心动作|核心决策|核心判断|核心行动|建议判定|建议|摘要|总结|风格判断|实际行动意图|具体行动倾向|角色意图|本轮意图|本轮行动|本轮主行动|意图生成|行动名称|行动提案|行动代号|本\s*tick\s*具体行动|本\s*tick\s*建议行动|本\s*tick\s*战略主轴)\s*[：:]?\s*",
                "",
                updated,
                flags=re.IGNORECASE,
            )
            updated = re.sub(
                r"^(?:目标|方式|理由|背景|契合当前局势|说明)\s*[：:]?\s*",
                "",
                updated,
                flags=re.IGNORECASE,
            )
            updated = re.sub(r'^[,;，；:：\s]+', "", updated)
            updated = re.sub(r'["\'“”`]\s*,?\s*$', "", updated)
            updated = updated.strip(" -*_`\"'“”[]()【】{},，；;")
            if updated == cleaned:
                break
            cleaned = updated
        return cleaned.strip(" -*_`\"'“”【】{},，；;")

    def _intent_text_is_meta(self, text: Any, agent_name: str = "") -> bool:
        cleaned = self._clean_intent_candidate(text, agent_name=agent_name)
        if not cleaned:
            return True
        if self._has_json_key_leakage(cleaned):
            return True
        normalized = cleaned.lower()
        if normalized in GENERIC_OBJECTIVE_MARKERS or cleaned in META_LINE_MARKERS:
            return True
        if cleaned.endswith(("：", ":")):
            return True
        if len(cleaned) <= 10 and any(token in cleaned for token in ("判断", "决策", "动作", "动机", "背景", "风险", "步骤", "理由", "意图")):
            return True
        if re.search(r"(?:第\s*\d+\s*轮|tick\s*\d+).*(?:角色意图|意图生成|行动倾向)", cleaned, flags=re.IGNORECASE):
            return True
        if (
            len(cleaned) > 28
            and any(prefix in cleaned for prefix in ("基于当前局势", "结合当前局势", "以下是基于当前局势", "以下是为", "作为"))
            and any(token in cleaned for token in ("最合理", "角色意图", "行动倾向", "生成的角色意图"))
        ):
            return True
        if not extract_signal_tokens(cleaned) and len(cleaned) <= 18:
            return True
        return False

    def _best_actionable_line(self, lines: List[str], agent_name: str, scene_title: str) -> str:
        action_markers = (
            "推动",
            "强化",
            "争夺",
            "封锁",
            "渗透",
            "控制",
            "扩张",
            "保护",
            "试探",
            "宣称",
            "接触",
            "调动",
            "重组",
            "召集",
            "扶持",
            "压制",
            "布局",
            "抢占",
            "潜入",
            "夺取",
            "稳住",
            "联络",
            "制造",
            "阻断",
            "清剿",
        )
        scored: List[tuple[int, str]] = []
        for raw_line in lines:
            cleaned = self._clean_intent_candidate(raw_line, agent_name=agent_name)
            if not cleaned or self._intent_text_is_meta(cleaned, agent_name=agent_name):
                continue
            if self._is_placeholder_text(cleaned, scene_title=scene_title):
                continue

            score = 0
            if re.match(r"^\d+\s*[.)、]", raw_line.strip()):
                score += 2
            if 8 <= len(cleaned) <= 88:
                score += 3
            if any(marker in cleaned for marker in action_markers):
                score += 4
            if cleaned.startswith(("目标", "方式", "理由", "契合当前局势", "背景")):
                score -= 3
            if cleaned.startswith(("由于", "当前", "在")) and len(cleaned) > 32:
                score -= 2
            scored.append((score, cleaned))

        if not scored:
            return ""
        scored.sort(key=lambda item: (item[0], -abs(len(item[1]) - 28)), reverse=True)
        return scored[0][1] if scored[0][0] > 0 else ""

    def _normalize_text_intent_fields(
        self,
        objective: str,
        summary: str,
        lines: List[str],
        agent_name: str,
        scene_title: str,
    ) -> tuple[str, str]:
        cleaned_objective = self._clean_intent_candidate(objective, agent_name=agent_name)
        cleaned_summary = self._clean_intent_candidate(summary, agent_name=agent_name)
        best_action = self._best_actionable_line(lines, agent_name=agent_name, scene_title=scene_title)

        objective_is_meta = self._intent_text_is_meta(cleaned_objective, agent_name=agent_name)
        summary_is_meta = self._intent_text_is_meta(cleaned_summary, agent_name=agent_name)

        if objective_is_meta or len(cleaned_objective) > 96:
            if best_action:
                cleaned_objective = best_action
            elif cleaned_summary and not summary_is_meta:
                cleaned_objective = cleaned_summary
        if not cleaned_summary or summary_is_meta:
            cleaned_summary = best_action or cleaned_objective
        if len(cleaned_objective) > 96 and cleaned_summary and not self._intent_text_is_meta(cleaned_summary, agent_name=agent_name):
            cleaned_objective = cleaned_summary
        if not cleaned_objective:
            cleaned_objective = best_action or cleaned_summary or "advance current leverage"
        if not cleaned_summary:
            cleaned_summary = cleaned_objective
        if (
            best_action
            and cleaned_objective
            and extract_signal_tokens(cleaned_objective) == extract_signal_tokens(cleaned_summary)
            and self._intent_text_is_meta(cleaned_objective, agent_name=agent_name)
        ):
            cleaned_objective = best_action
        if best_action and (not extract_signal_tokens(cleaned_objective) or len(cleaned_objective) > 96):
            cleaned_objective = best_action

        return cleaned_objective[:220], cleaned_summary[:320]

    def _intent_is_low_signal(self, intent: ActorIntent) -> bool:
        if self._has_json_key_leakage(intent.objective) or self._has_json_key_leakage(intent.summary):
            return True
        objective = self._clean_intent_candidate(intent.objective, agent_name=intent.agent_name).lower()
        summary = self._clean_intent_candidate(intent.summary, agent_name=intent.agent_name).lower()
        generic_markers = {
            "",
            "advance current leverage",
            "maintain current leverage",
            "protect current position",
            "respond to nearby threats",
        }
        if self._intent_text_is_meta(intent.objective, agent_name=intent.agent_name) and (
            self._intent_text_is_meta(intent.summary, agent_name=intent.agent_name) or summary in generic_markers
        ):
            return True
        if objective in generic_markers and summary in generic_markers:
            return True
        if (
            objective in generic_markers
            and (not intent.target)
            and (not intent.tags)
            and (not intent.state_impacts)
            and intent.priority <= 3
            and summary == objective
        ):
            return True
        return False

    def _intent_from_response(
        self,
        tick: int,
        agent: Dict[str, Any],
        response: Dict[str, Any],
        source: str,
    ) -> ActorIntent:
        agent_id = safe_int(agent.get("agent_id", 0), default=0, lower=0)
        agent_name = str(agent.get("entity_name") or agent.get("username") or f"Actor {agent_id}")
        raw_objective = str(response.get("objective") or response.get("summary") or "advance current leverage").strip()
        raw_summary = str(response.get("summary") or raw_objective).strip()
        objective, summary = self._normalize_text_intent_fields(
            raw_objective,
            raw_summary,
            lines=[raw_objective, raw_summary, str(response.get("rationale") or "")],
            agent_name=agent_name,
            scene_title="",
        )
        dependencies = [
            item
            for item in ensure_list(response.get("dependencies"))
            if item in self.active_events or item in self.queued_events
        ]
        participants = dedupe_keep_order([agent_name, *ensure_list(response.get("participants"))])
        return ActorIntent(
            intent_id=self._next_intent_id(tick),
            tick=tick,
            agent_id=agent_id,
            agent_name=agent_name,
            objective=objective[:220],
            summary=summary[:320],
            location=normalize_optional_text(response.get("location") or agent.get("home_location") or ""),
            target=normalize_optional_text(response.get("target")) or None,
            desired_duration=safe_int(
                response.get("desired_duration", 1),
                default=1,
                lower=1,
                upper=self.max_event_duration,
            ),
            priority=safe_int(response.get("priority", 3), default=3, lower=1, upper=5),
            urgency=safe_int(response.get("urgency", 3), default=3, lower=1, upper=5),
            risk_level=safe_int(response.get("risk_level", 3), default=3, lower=1, upper=5),
            dependencies=dependencies,
            participants=participants[:6],
            tags=ensure_list(response.get("tags"))[:5],
            state_impacts=self._normalize_state_impacts(response.get("state_impacts")),
            rationale=str(response.get("rationale", "")).strip()[:220],
            source=source,
        )

    def _heuristic_intent(self, tick: int, scene_title: str, agent: Dict[str, Any]) -> ActorIntent:
        agent_id = safe_int(agent.get("agent_id", 0), default=0, lower=0)
        agent_name = str(agent.get("entity_name") or agent.get("username") or f"Actor {agent_id}")
        raw_goal_pool = (
            ensure_list(agent.get("driving_goals"))
            + ensure_list(agent.get("story_hooks"))
            + ensure_list(agent.get("resources"))
            + ensure_list(agent.get("constraints"))
        )
        goals = [
            self._clean_intent_candidate(item, agent_name=agent_name)
            for item in raw_goal_pool
            if self._clean_intent_candidate(item, agent_name=agent_name)
            and not self._intent_text_is_meta(item, agent_name=agent_name)
            and self._clean_intent_candidate(item, agent_name=agent_name).lower() not in GENERIC_OBJECTIVE_MARKERS
            and extract_signal_tokens(self._clean_intent_candidate(item, agent_name=agent_name))
            and not re.match(
                r"^(?:name|affiliation|faction|category|scope|world_role|entity_name|public_role)\s*:",
                self._clean_intent_candidate(item, agent_name=agent_name).lower(),
            )
            and not re.search(
                r"\b(?:is|are|was|were|located|lives|resides|key figure|current|known)\b",
                self._clean_intent_candidate(item, agent_name=agent_name).lower(),
            )
            and not (
                any(
                    token in self._clean_intent_candidate(item, agent_name=agent_name)
                    for token in ("是", "位于", "居住地", "关键人物", "成员", "当前", "知晓")
                )
                and not any(
                    action in self._clean_intent_candidate(item, agent_name=agent_name)
                    for action in ("推动", "争夺", "保护", "压制", "扩张", "控制", "封锁", "潜入", "联络", "重组")
                )
            )
        ]
        if not goals:
            goals = [
                "protect current leverage",
                "expand influence around the flashpoint",
                "probe nearby threats",
                "secure supply lines",
            ]
        resources = ensure_list(agent.get("resources"))
        links = [
            normalize_optional_text(link)
            for link in ensure_list(agent.get("connected_entities"))
            if not self._is_placeholder_text(link, scene_title=scene_title)
        ]
        objective = self._clean_intent_candidate(self.rng.choice(goals), agent_name=agent_name) or self.rng.choice(goals)
        if not extract_signal_tokens(objective):
            objective = self.rng.choice(
                [
                    "stabilize nearby allies",
                    "probe pressure points",
                    "expand influence in the flashpoint",
                    "lock down strategic routes",
                ]
            )
        target = self.rng.choice(links) if links and self.rng.random() > 0.35 else None
        location = normalize_optional_text(agent.get("home_location")) or scene_title
        duration = 1 + int(self.rng.random() < 0.45) + int(self.rng.random() < 0.20)
        priority = min(5, 2 + int(self.world_state["tension"] > 0.55) + int(bool(target)))
        urgency = min(5, 2 + int(self.world_state["momentum"] > 0.45) + int(self.world_state["stability"] < 0.45))
        risk_level = min(5, 2 + int(self.world_state["tension"] > 0.6))

        impacts = {
            "conflict": 0.06 if any(token in objective.lower() for token in ("expand", "control", "take", "夺", "争", "war")) else 0.0,
            "scarcity": 0.05 if any(token in objective.lower() for token in ("resource", "supply", "能源", "粮", "water")) else 0.0,
            "legitimacy": -0.04 if any(token in objective.lower() for token in ("secret", "coup", "betray", "阴谋")) else 0.0,
            "stability": 0.05 if any(token in objective.lower() for token in ("protect", "defend", "alliance", "守")) else -0.02,
            "momentum": 0.05 + (0.01 * priority),
        }
        if resources and impacts["scarcity"] == 0.0:
            impacts["scarcity"] = 0.03

        summary = (
            f"{agent_name} 决定围绕“{objective}”推动局势变化"
            f"{f'，并把焦点压向 {target}' if target else ''}"
            f"{f'，行动地点在 {location}' if location else ''}。"
        )
        tags = ["alliance" if impacts["stability"] > 0 else "conflict", "resource" if impacts["scarcity"] > 0 else "maneuver"]

        return ActorIntent(
            intent_id=self._next_intent_id(tick),
            tick=tick,
            agent_id=agent_id,
            agent_name=agent_name,
            objective=objective[:220],
            summary=summary[:320],
            location=location,
            target=target,
            desired_duration=min(duration, self.max_event_duration),
            priority=priority,
            urgency=urgency,
            risk_level=risk_level,
            dependencies=[],
            participants=dedupe_keep_order([agent_name, target] if target else [agent_name])[:6],
            tags=tags,
            state_impacts=self._normalize_state_impacts(impacts),
            rationale=f"heuristic intent for {scene_title}",
            source="heuristic",
        )

    def _intent_from_text_output(
        self,
        tick: int,
        scene_title: str,
        agent: Dict[str, Any],
        raw_text: str,
    ) -> ActorIntent:
        agent_id = safe_int(agent.get("agent_id", 0), default=0, lower=0)
        agent_name = str(agent.get("entity_name") or agent.get("username") or f"Actor {agent_id}")
        normalized = raw_text.strip()
        lines = [
            re.sub(r"[*_`]+", "", re.sub(r"^[#>*\-\s]+", "", line)).strip()
            for line in normalized.splitlines()
            if line.strip()
        ]

        def split_label_value(line: str) -> tuple[str, str]:
            for separator in ("：", ":"):
                if separator in line:
                    left, right = line.split(separator, 1)
                    return left.strip(), right.strip()
            return line.strip(), ""

        def find_prefixed(prefixes: List[str]) -> str:
            for line in lines:
                label, value = split_label_value(line)
                for prefix in prefixes:
                    normalized_prefix = prefix.lower()
                    if label.lower().startswith(normalized_prefix):
                        return value or label[len(prefix):].strip()
                    match = re.search(
                        rf"{re.escape(prefix)}\s*[：:]\s*(.+)",
                        line,
                        flags=re.IGNORECASE,
                    )
                    if match:
                        return match.group(1).strip()
            return ""

        def find_regex(patterns: List[str]) -> str:
            for pattern in patterns:
                match = re.search(pattern, normalized, flags=re.IGNORECASE | re.MULTILINE)
                if match:
                    return match.group(1).strip()
            return ""

        def looks_like_heading(text: str) -> bool:
            return self._intent_text_is_meta(text, agent_name=agent_name)

        def compact_objective(text: str) -> str:
            cleaned = self._clean_intent_candidate(text, agent_name=agent_name)
            if not cleaned:
                return ""
            parts = re.split(r"[。！？!?\n]", cleaned, maxsplit=1)
            cleaned = parts[0].strip() if parts else cleaned
            clause = re.split(r"[；;]", cleaned, maxsplit=1)[0].strip()
            if 8 <= len(clause) <= 56:
                return clause
            return cleaned[:56].strip()

        json_objective = self._extract_json_style_field(
            normalized,
            ["objective", "action", "title", "event_name", "事件名", "行动名", "行动目标"],
        )
        json_summary = self._extract_json_style_field(
            normalized,
            ["summary", "description", "result", "摘要", "执行内容", "结果"],
        )
        json_location = self._extract_json_style_field(
            normalized,
            ["location", "地点", "行动地点"],
        )
        json_target = self._extract_json_style_field(
            normalized,
            ["target", "目标对象", "对象"],
        )
        objective = (
            compact_objective(
                json_objective
                or find_prefixed(
                    [
                        "OBJECTIVE",
                        "事件名",
                        "行动名",
                        "行动目标",
                        "本回合行动",
                        "本轮行动",
                        "本 tick 行动",
                        "本 tick 行动方案",
                        "本轮主行动",
                        "行动方案",
                        "行动建议",
                        "行动方向",
                    ]
                )
                or find_regex(
                    [
                        r"(?:事件名|本\s*(?:tick|轮|回合)\s*行动(?:方案)?|本轮主行动|行动方案|行动建议)\s*[：:]\s*[`\"“”']?([^\n`\"“”']+)",
                        rf"{re.escape(agent_name)}.*?(?:本\s*(?:tick|轮|回合)\s*)?行动(?:方案)?\s*[：:]\s*([^\n]+)",
                    ]
                )
            )
            or compact_objective(next((line for line in lines if "行动" in line and not looks_like_heading(line)), ""))
            or compact_objective(next((line for line in lines if not looks_like_heading(line)), ""))
            or "advance current leverage"
        )
        summary = (
            self._clean_intent_candidate(
                json_summary
                or find_prefixed(["SUMMARY", "执行内容", "结果", "建议判定", "核心判断", "核心决策"]),
                agent_name=agent_name,
            )
            or self._clean_intent_candidate(
                next(
                    (
                        line
                        for line in lines
                        if self._clean_intent_candidate(line, agent_name=agent_name)
                        and self._clean_intent_candidate(line, agent_name=agent_name) != objective
                        and len(self._clean_intent_candidate(line, agent_name=agent_name)) > 12
                        and not looks_like_heading(line)
                    ),
                    "",
                ),
                agent_name=agent_name,
            )
            or objective
        )
        if objective.lower() in {"summary", "摘要", "动机", "风格", "标题", "名称", "方案"} or len(objective) <= 3:
            objective = (
                compact_objective(
                    find_regex(
                        [
                            r"(?:事件名|本\s*(?:tick|轮|回合)\s*行动(?:方案)?|本轮主行动|行动方案|行动建议)\s*[：:]\s*[`\"“”']?([^\n`\"“”']+)",
                            rf"{re.escape(agent_name)}.*?(?:本\s*(?:tick|轮|回合)\s*)?行动(?:方案)?\s*[：:]\s*([^\n]+)",
                        ]
                    )
                )
                or compact_objective(summary)
                or objective
            )
        objective, summary = self._normalize_text_intent_fields(
            objective=objective,
            summary=summary,
            lines=lines,
            agent_name=agent_name,
            scene_title=scene_title,
        )
        location = (
            json_location
            or find_prefixed(["LOCATION", "地点", "行动地点"])
            or normalize_optional_text(agent.get("home_location"))
            or scene_title
        )
        target = (
            json_target
            or find_prefixed(["TARGET", "目标对象", "对象"])
            or ""
        )
        target = normalize_optional_text(target) or (ensure_list(agent.get("connected_entities"))[:1] or [""])[0]
        combined = f"{objective}\n{summary}\n{normalized}".lower()

        impacts = {
            "conflict": 0.08 if any(token in combined for token in ("压制", "镇压", "清剿", "war", "strike", "夺", "争")) else 0.0,
            "scarcity": 0.05 if any(token in combined for token in ("港口", "补给", "资源", "supply", "route")) else 0.0,
            "legitimacy": -0.05 if any(token in combined for token in ("封口", "秘密", "谎言", "propaganda")) else 0.03,
            "stability": 0.06 if any(token in combined for token in ("维稳", "保护", "protect", "stabil")) else -0.02,
            "momentum": 0.06 if any(token in combined for token in ("布局", "扩张", "抢占", "window", "next step")) else 0.03,
        }
        priority = 4 if any(token in combined for token in ("世界政府", "海军", "革命军", "四皇", "broadcast", "贝加庞克")) else 3
        urgency = 4 if any(token in combined for token in ("立即", "立刻", "24小时", "窗口期", "now")) else 3
        risk_level = 4 if any(token in combined for token in ("风险", "danger", "暴动", "分裂", "war")) else 3
        participants = dedupe_keep_order([agent_name, *ensure_list(target)])[:6]
        tags = dedupe_keep_order(
            [
                "stability" if impacts["stability"] > 0 else "conflict",
                "legitimacy" if impacts["legitimacy"] < 0 else "maneuver",
            ]
        )

        return ActorIntent(
            intent_id=self._next_intent_id(tick),
            tick=tick,
            agent_id=agent_id,
            agent_name=agent_name,
            objective=objective[:220],
            summary=summary[:320],
            location=normalize_optional_text(location)[:120],
            target=normalize_optional_text(target)[:120] or None,
            desired_duration=1 + int(any(token in combined for token in ("后续", "持续", "下一步", "network"))),
            priority=priority,
            urgency=urgency,
            risk_level=risk_level,
            dependencies=[],
            participants=participants,
            tags=tags[:5],
            state_impacts=self._normalize_state_impacts(impacts),
            rationale=f"text-recovered intent from llm prose: {normalized[:180]}",
            source="llm_text_recovered",
        )

    def _intent_cluster_features(self, intent: ActorIntent) -> Dict[str, Any]:
        location = "" if self._is_placeholder_text(intent.location) else normalize_optional_text(intent.location)
        target = "" if self._is_placeholder_text(intent.target) else normalize_optional_text(intent.target)
        participants = [
            normalize_optional_text(participant)
            for participant in intent.participants
            if not self._is_placeholder_text(participant)
        ]
        tags = [
            normalize_optional_text(tag).lower()
            for tag in intent.tags
            if normalize_optional_text(tag)
        ]
        anchors = dedupe_keep_order([target, location, *participants[:3], *tags[:2]])
        tokens = [
            token.lower()
            for token in extract_signal_tokens(
                "\n".join(
                    [
                        intent.objective,
                        intent.summary,
                        target,
                        location,
                        " ".join(participants),
                        " ".join(tags),
                    ]
                )
            )
        ]
        return {
            "location": location.lower(),
            "target": target.lower(),
            "participants": {participant.lower() for participant in participants if participant},
            "tags": {tag.lower() for tag in tags if tag},
            "anchors": {anchor.lower() for anchor in anchors if anchor},
            "tokens": set(tokens),
        }

    def _intent_cluster_similarity(self, cluster: Dict[str, Any], features: Dict[str, Any]) -> float:
        score = 0.0
        if features["target"] and features["target"] in cluster["targets"]:
            score += 3.0
        if features["location"] and features["location"] in cluster["locations"]:
            score += 2.5
        score += min(len(cluster["participants"] & features["participants"]), 2) * 1.2
        score += min(len(cluster["tags"] & features["tags"]), 2) * 0.8
        score += min(len(cluster["anchors"] & features["anchors"]), 3) * 0.75
        score += min(len(cluster["tokens"] & features["tokens"]), 4) * 0.45
        return score

    def _resolver_cluster_label(self, scene_title: str, intents: List[ActorIntent]) -> str:
        label_parts: List[str] = []
        for intent in intents:
            for candidate in [
                intent.target,
                intent.location,
                *intent.tags[:1],
                *intent.participants[1:2],
            ]:
                cleaned = normalize_optional_text(candidate)
                if cleaned and not self._is_placeholder_text(cleaned, scene_title=scene_title):
                    label_parts.append(cleaned)
        label_parts = dedupe_keep_order(label_parts)
        if label_parts:
            return " / ".join(label_parts[:2])

        fallback_tokens = dedupe_keep_order(
            token
            for intent in intents
            for token in extract_signal_tokens(f"{intent.objective}\n{intent.summary}")
        )
        if fallback_tokens:
            return " / ".join(fallback_tokens[:2])
        return scene_title

    def _partition_intents_for_resolution(
        self,
        scene_title: str,
        intents: List[ActorIntent],
    ) -> List[Dict[str, Any]]:
        cluster_size = max(self.resolver_cluster_size, 1)
        if len(intents) <= cluster_size:
            return [{"label": scene_title, "intents": list(intents)}]

        target_cluster_count = max(1, (len(intents) + cluster_size - 1) // cluster_size)
        ordered_intents = sorted(
            intents,
            key=lambda intent: (intent.priority, intent.urgency, intent.risk_level),
            reverse=True,
        )
        raw_clusters: List[Dict[str, Any]] = []

        for intent in ordered_intents:
            features = self._intent_cluster_features(intent)
            best_index = -1
            best_score = -1.0
            for index, cluster in enumerate(raw_clusters):
                score = self._intent_cluster_similarity(cluster, features)
                if score > best_score:
                    best_score = score
                    best_index = index

            should_join = (
                best_index >= 0
                and (
                    best_score >= 1.25
                    or len(raw_clusters) >= target_cluster_count
                    or len(raw_clusters[best_index]["intents"]) < cluster_size
                )
            )
            if should_join:
                cluster = raw_clusters[best_index]
            else:
                cluster = {
                    "intents": [],
                    "locations": set(),
                    "targets": set(),
                    "participants": set(),
                    "tags": set(),
                    "anchors": set(),
                    "tokens": set(),
                }
                raw_clusters.append(cluster)

            cluster["intents"].append(intent)
            if features["location"]:
                cluster["locations"].add(features["location"])
            if features["target"]:
                cluster["targets"].add(features["target"])
            cluster["participants"].update(features["participants"])
            cluster["tags"].update(features["tags"])
            cluster["anchors"].update(features["anchors"])
            cluster["tokens"].update(features["tokens"])

        final_clusters: List[Dict[str, Any]] = []
        for cluster in raw_clusters:
            members = sorted(
                cluster["intents"],
                key=lambda intent: (intent.priority, intent.urgency, intent.risk_level),
                reverse=True,
            )
            while members:
                chunk = members[:cluster_size]
                members = members[cluster_size:]
                final_clusters.append(
                    {
                        "label": self._resolver_cluster_label(scene_title, chunk),
                        "intents": chunk,
                    }
                )

        return final_clusters or [{"label": scene_title, "intents": list(intents)}]

    async def _resolve_with_llm_cluster(
        self,
        tick: int,
        scene_title: str,
        cluster_label: str,
        cluster_index: int,
        cluster_count: int,
        intents: List[ActorIntent],
    ) -> Dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": self._resolver_system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "tick": tick,
                        "scene_title": scene_title,
                        "cluster_context": {
                            "label": cluster_label,
                            "cluster_index": cluster_index,
                            "cluster_count": cluster_count,
                            "intent_count": len(intents),
                        },
                        "world_state": self._world_state_brief(),
                        "active_events": [self._event_brief(event) for event in self.active_events.values()],
                        "queued_events": [self._event_brief(event) for event in self.queued_events.values()],
                        "max_active_events": self.max_active_events,
                        "max_queued_events": self.max_queued_events,
                        "output_contract": {
                            "schema": [
                                "accepted_events",
                                "deferred_intents",
                                "rejected_intents",
                            ],
                            "every_intent_must_be_accounted_for": True,
                            "accepted_event_requires_owner_intent_id": True,
                            "queued_results_still_belong_in_accepted_events": True,
                            "forbid_summary_only_output": True,
                        },
                        "intents": [intent.to_dict() for intent in intents],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            response_meta = await self._call_llm_json_with_retry(
                llm=self.resolver_llm,
                provider_role="world_resolver",
                tick=tick,
                phase="resolver",
                context=f"{scene_title} / {cluster_label}",
                messages=messages,
                temperature=0.30,
                max_tokens=min(1600, 920 + (180 * len(intents))),
            )
        except InvalidJSONResponseError as exc:
            recovery_response = {
                "_raw_response": str(exc.raw_response or ""),
                "_repaired_response": str(exc.repaired_response or ""),
            }
            recovered = self._parse_resolver_response(
                tick=tick,
                intents=intents,
                response=recovery_response,
                source="llm_invalid_json_recovered",
            )
            recovery_diagnostics = dict(recovered.get("diagnostics") or {})
            if (
                recovered.get("accepted_events")
                or recovered.get("deferred_map")
                or recovered.get("rejected_map")
            ):
                self._write_llm_trace(
                    stage="resolver_cluster",
                    tick=tick,
                    provider_role="world_resolver",
                    llm=self.resolver_llm,
                    status="accepted",
                    reason_code=recovery_diagnostics.get("reason_code", "resolver_invalid_json_recovered"),
                    context=f"{scene_title} / {cluster_label}",
                    messages=messages,
                    raw_response=str(exc.raw_response or ""),
                    repaired_response=str(exc.repaired_response or ""),
                    parsed_json=recovery_response,
                    outcome={
                        "accepted_event_count": len(recovered.get("accepted_events", [])),
                        "accepted_event_titles": [event.title for event in recovered.get("accepted_events", [])],
                        "deferred_count": len(recovered.get("deferred_map", {})),
                        "rejected_count": len(recovered.get("rejected_map", {})),
                        "diagnostics": recovery_diagnostics,
                    },
                    extra={
                        "cluster_label": cluster_label,
                        "cluster_index": cluster_index,
                        "cluster_count": cluster_count,
                        "intent_ids": [intent.intent_id for intent in intents],
                        "recovered_from_invalid_json": True,
                    },
                )
                return recovered
            self._write_llm_trace(
                stage="resolver_cluster",
                tick=tick,
                provider_role="world_resolver",
                llm=self.resolver_llm,
                status="invalid_json",
                reason_code="resolver_invalid_json",
                context=f"{scene_title} / {cluster_label}",
                messages=messages,
                raw_response=str(exc.raw_response or ""),
                repaired_response=str(exc.repaired_response or ""),
                error=str(exc),
                extra={
                    "cluster_label": cluster_label,
                    "cluster_index": cluster_index,
                    "cluster_count": cluster_count,
                    "intent_ids": [intent.intent_id for intent in intents],
                },
            )
            raise

        raw_response_text = str((response_meta or {}).get("raw_response") or "")
        repaired_response_text = str((response_meta or {}).get("repaired_response") or "")
        response = dict((response_meta or {}).get("data") or {})
        if raw_response_text:
            response["_raw_response"] = raw_response_text
        if repaired_response_text:
            response["_repaired_response"] = repaired_response_text
        parsed = self._parse_resolver_response(
            tick=tick,
            intents=intents,
            response=response,
            source="llm",
        )
        diagnostics = dict(parsed.get("diagnostics") or {})
        if not parsed.get("accepted_events"):
            self._write_meta_event(
                {
                    "event_type": "resolver_cluster_zero_accept",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "round": tick,
                    "tick": tick,
                    "phase": "resolver",
                    "summary": (
                        f"Resolver cluster {cluster_index}/{cluster_count} returned zero accepted events "
                        f"({diagnostics.get('reason_code', 'resolver_unknown')})."
                    ),
                    "context": f"{scene_title} / {cluster_label}",
                    "reason_code": diagnostics.get("reason_code", "resolver_unknown"),
                    "cluster_label": cluster_label,
                    "cluster_index": cluster_index,
                    "cluster_count": cluster_count,
                    "diagnostics": diagnostics,
                }
            )

        self._write_llm_trace(
            stage="resolver_cluster",
            tick=tick,
            provider_role="world_resolver",
            llm=self.resolver_llm,
            status="accepted" if parsed.get("accepted_events") else "zero_accept",
            reason_code=diagnostics.get("reason_code", "resolver_unknown"),
            context=f"{scene_title} / {cluster_label}",
            messages=messages,
            raw_response=str((response_meta or {}).get("raw_response") or ""),
            json_candidate=str((response_meta or {}).get("json_candidate") or ""),
            repaired_response=str((response_meta or {}).get("repaired_response") or ""),
            repaired_candidate=str((response_meta or {}).get("repaired_candidate") or ""),
            parsed_json=response,
            outcome={
                "accepted_event_count": len(parsed.get("accepted_events", [])),
                "accepted_event_titles": [event.title for event in parsed.get("accepted_events", [])],
                "deferred_count": len(parsed.get("deferred_map", {})),
                "rejected_count": len(parsed.get("rejected_map", {})),
                "diagnostics": diagnostics,
            },
            extra={
                "cluster_label": cluster_label,
                "cluster_index": cluster_index,
                "cluster_count": cluster_count,
                "intent_ids": [intent.intent_id for intent in intents],
            },
        )
        return parsed

    def _salvage_zero_accept_resolution(
        self,
        tick: int,
        scene_title: str,
        intents: List[ActorIntent],
        resolved: Dict[str, Any],
    ) -> Dict[str, Any]:
        if resolved.get("accepted_events") or not self.resolver_salvage_on_zero_accept:
            return resolved

        available_slots = max(self.max_active_events - len(self.active_events), 0) + max(
            self.max_queued_events - len(self.queued_events),
            0,
        )
        salvage_limit = max(1, min(3, available_slots or 1))
        salvaged = self._resolve_heuristically(
            tick,
            scene_title,
            intents,
            max_events=salvage_limit,
            source="heuristic_salvage",
        )
        if not salvaged["accepted_events"]:
            return resolved

        accepted_ids = {
            intent_id
            for event in salvaged["accepted_events"]
            for intent_id in event.source_intent_ids
        }
        deferred_map = {
            intent_id: reason
            for intent_id, reason in {
                **resolved.get("deferred_map", {}),
                **salvaged.get("deferred_map", {}),
            }.items()
            if intent_id not in accepted_ids
        }
        rejected_map = dict(resolved.get("rejected_map", {}))
        rejected_map.update(salvaged.get("rejected_map", {}))
        for intent_id in accepted_ids:
            deferred_map.pop(intent_id, None)
            rejected_map.pop(intent_id, None)

        self._write_meta_event(
            {
                "event_type": "resolver_salvaged",
                "timestamp": now_iso(),
                "simulation_mode": "world",
                "round": tick,
                "tick": tick,
                "phase": "resolver_salvage",
                "summary": (
                    f"Tick {tick} resolver returned zero accepted events; "
                    f"heuristic salvage promoted {len(salvaged['accepted_events'])} event(s)."
                ),
                "context": scene_title,
            }
        )

        return {
            "accepted_events": salvaged["accepted_events"],
            "deferred_map": deferred_map,
            "rejected_map": rejected_map,
        }

    async def _resolve_tick_intents(
        self,
        tick: int,
        scene_title: str,
        intents: List[ActorIntent],
    ) -> Dict[str, Any]:
        if not intents:
            return {
                "accepted_events": [],
                "accepted_count": 0,
                "deferred_count": 0,
                "rejected_count": 0,
            }

        if self.resolver_llm is None:
            if self._semantic_fallback_enabled():
                resolved = self._resolve_heuristically(tick, scene_title, intents)
            else:
                raise RuntimeError("WORLD_RESOLVER LLM 未配置")
        else:
            while True:
                try:
                    resolved = await self._resolve_with_llm(tick, scene_title, intents)
                    break
                except Exception as exc:
                    if isinstance(exc, InvalidJSONResponseError):
                        resolved = self._resolve_heuristically(tick, scene_title, intents)
                        break
                    if self._semantic_fallback_enabled():
                        resolved = self._resolve_heuristically(tick, scene_title, intents)
                        break

                    if self.resolver_on_failure == "pause":
                        self._write_meta_event(
                            {
                                "event_type": "tick_blocked",
                                "timestamp": now_iso(),
                                "simulation_mode": "world",
                                "round": tick,
                                "tick": tick,
                                "phase": "waiting_provider",
                                "provider_role": "world_resolver",
                                "summary": (
                                    f"Tick {tick} blocked because world_resolver is unavailable; "
                                    "waiting instead of switching models."
                                ),
                                "reason": str(exc),
                                "context": scene_title,
                            }
                        )
                        await self._wait_for_provider_recovery(
                            provider_role="world_resolver",
                            llm=self.resolver_llm,
                            tick=tick,
                            phase="waiting_provider",
                            reason=str(exc),
                            context=scene_title,
                        )
                        continue

                    raise

        resolved = self._salvage_zero_accept_resolution(tick, scene_title, intents, resolved)

        accepted_intent_ids = set()
        for event in resolved["accepted_events"]:
            accepted_intent_ids.update(event.source_intent_ids)

        deferred_map = resolved.get("deferred_map", {})
        rejected_map = resolved.get("rejected_map", {})

        for intent in intents:
            if intent.intent_id in accepted_intent_ids:
                event = next(
                    (item for item in resolved["accepted_events"] if intent.intent_id in item.source_intent_ids),
                    None,
                )
                status = "queued" if event and event.status == "queued" else "accepted"
                self._write_resolution_log(intent, status=status, event=event)
            elif intent.intent_id in deferred_map:
                self._write_resolution_log(intent, status="deferred", reason=deferred_map[intent.intent_id])
            else:
                reason = rejected_map.get(intent.intent_id, "resolver rejected the intent")
                self._write_resolution_log(intent, status="rejected", reason=reason)

        return {
            "accepted_events": resolved["accepted_events"],
            "accepted_count": len(resolved["accepted_events"]),
            "deferred_count": len(deferred_map),
            "rejected_count": max(len(intents) - len(accepted_intent_ids) - len(deferred_map), len(rejected_map)),
        }

    def _cluster_focus_label(self, cluster: List[ActorIntent], scene_title: str) -> str:
        return self._resolver_cluster_label(scene_title, cluster)

    def _cluster_intents_for_resolver(
        self,
        intents: List[ActorIntent],
        scene_title: str,
    ) -> List[List[ActorIntent]]:
        partitioned = self._partition_intents_for_resolution(scene_title, intents)
        return [cluster["intents"] for cluster in partitioned]

    def _build_event_from_resolved_item(
        self,
        tick: int,
        item: Dict[str, Any],
        intent_map: Dict[str, ActorIntent],
        source: str,
    ) -> tuple[Optional[WorldEvent], str]:
        if not isinstance(item, dict):
            return None, "invalid_event_item"

        owner_intent_id = str(item.get("owner_intent_id", "")).strip()
        if not owner_intent_id:
            return None, "missing_owner_intent_id"
        if owner_intent_id not in intent_map:
            return None, "unknown_owner_intent_id"

        source_ids = dedupe_keep_order(
            [owner_intent_id, *ensure_list(item.get("supporting_intent_ids"))]
        )
        source_intents = [intent_map[intent_id] for intent_id in source_ids if intent_id in intent_map]
        if not source_intents:
            return None, "no_valid_source_intents"

        dependencies = [
            dep
            for dep in ensure_list(item.get("dependencies"))
            if dep in self.active_events or dep in self.queued_events
        ]
        participants = dedupe_keep_order(
            ensure_list(item.get("participants"))
            + [intent.agent_name for intent in source_intents]
        )[:8]
        participant_ids = dedupe_keep_order([str(intent.agent_id) for intent in source_intents])
        duration = safe_int(
            item.get("duration_ticks", max(intent.desired_duration for intent in source_intents)),
            default=max(intent.desired_duration for intent in source_intents),
            lower=1,
            upper=self.max_event_duration,
        )
        primary_intent = intent_map[owner_intent_id]
        title, summary = self._normalize_text_intent_fields(
            str(item.get("title") or primary_intent.objective or "World event").strip(),
            str(item.get("summary") or primary_intent.summary).strip(),
            lines=[
                str(item.get("title") or ""),
                str(item.get("summary") or ""),
                primary_intent.objective,
                primary_intent.summary,
            ],
            agent_name=primary_intent.agent_name,
            scene_title="",
        )
        return WorldEvent(
            event_id=self._next_event_id(tick),
            tick=tick,
            title=title[:180],
            summary=summary[:360],
            primary_agent_id=primary_intent.agent_id,
            primary_agent_name=primary_intent.agent_name,
            participants=participants,
            participant_ids=[int(value) for value in participant_ids if str(value).isdigit()],
            source_intent_ids=[intent.intent_id for intent in source_intents],
            priority=safe_int(
                item.get("priority", max(intent.priority for intent in source_intents)),
                default=max(intent.priority for intent in source_intents),
                lower=1,
                upper=5,
            ),
            duration_ticks=duration,
            resolves_at_tick=tick + duration - 1,
            status=(
                "queued"
                if dependencies or normalize_optional_text(item.get("status")).lower() == "queued"
                else "active"
            ),
            location=normalize_optional_text(item.get("location") or primary_intent.location),
            dependencies=dependencies,
            state_impacts=self._merge_state_impacts(source_intents, item.get("state_impacts")),
            source=source,
            rationale=str(item.get("rationale", "")).strip()[:220],
        ), ""

    def _normalize_resolver_response_payload(
        self,
        response: Dict[str, Any],
        intents: Optional[List[ActorIntent]] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        normalized = dict(response or {}) if isinstance(response, dict) else {}
        nested_event_containers: List[tuple[str, Dict[str, Any]]] = []
        embedded_payloads: List[tuple[str, Dict[str, Any]]] = []

        def parse_embedded_payload(raw_value: Any) -> Dict[str, Any]:
            if isinstance(raw_value, dict):
                return dict(raw_value)
            raw_text = normalize_optional_text(raw_value)
            if not raw_text:
                return {}
            candidates = [raw_text]
            extracted_candidate = LLMClient._extract_json_candidate(raw_text)
            if extracted_candidate and extracted_candidate not in candidates:
                candidates.append(extracted_candidate)
            for candidate in candidates:
                try:
                    parsed = json.loads(candidate)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
            return {}

        for key, value in normalized.items():
            if not isinstance(value, dict):
                continue
            if any(
                subkey in value
                for subkey in (
                    "accepted_events",
                    "generated_events",
                    "created_events",
                    "new_active_events",
                    "resolved_events",
                    "active_events",
                    "queued_events",
                    "deferred_intents",
                    "rejected_intents",
                )
            ):
                nested_event_containers.append((key, value))
        for raw_key in ("_repaired_response", "_raw_response"):
            parsed_payload = parse_embedded_payload(normalized.get(raw_key))
            if not parsed_payload:
                continue
            embedded_key = f"{raw_key}_json"
            embedded_payloads.append((embedded_key, parsed_payload))
            if any(
                subkey in parsed_payload
                for subkey in (
                    "accepted_events",
                    "generated_events",
                    "created_events",
                    "new_active_events",
                    "resolved_events",
                    "active_events",
                    "queued_events",
                    "event_results",
                    "deferred_intents",
                    "rejected_intents",
                )
            ):
                nested_event_containers.append((embedded_key, parsed_payload))
            for subkey, subvalue in parsed_payload.items():
                if not isinstance(subvalue, dict):
                    continue
                if any(
                    nested_key in subvalue
                    for nested_key in (
                        "accepted_events",
                        "generated_events",
                        "created_events",
                        "new_active_events",
                        "resolved_events",
                        "active_events",
                        "queued_events",
                        "event_results",
                        "deferred_intents",
                        "rejected_intents",
                    )
                ):
                    nested_event_containers.append((f"{embedded_key}.{subkey}", subvalue))
        cluster_resolution = dict(next((value for key, value in nested_event_containers if key == "cluster_resolution"), {}))
        resolution_payload = normalized.get("resolution") or {}
        if not isinstance(resolution_payload, dict):
            resolution_payload = {}
        if not cluster_resolution:
            cluster_resolution = dict(
                next(
                    (
                        value
                        for key, value in nested_event_containers
                        if key.endswith(".cluster_resolution")
                    ),
                    {},
                )
            )
        if not resolution_payload:
            resolution_payload = dict(
                next(
                    (
                        value
                        for key, value in nested_event_containers
                        if key.endswith(".resolution")
                    ),
                    {},
                )
            )
        meta: Dict[str, Any] = {
            "alternative_schema_used": False,
            "converted_active_events": 0,
            "alternative_schema_keys": [],
            "summary_bootstrap_used": False,
            "summary_bootstrap_keys": [],
            "summary_bootstrap_events": 0,
            "summary_bootstrap_deferred": 0,
            "summary_bootstrap_rejected": 0,
            "placeholder_bootstrap_used": False,
            "placeholder_bootstrap_keys": [],
            "placeholder_bootstrap_events": 0,
        }

        existing_accepted = normalized.get("accepted_events")
        if isinstance(existing_accepted, list) and existing_accepted:
            return normalized, meta

        alternative_keys_used: List[str] = []
        placeholder_markers = {
            "string",
            "event_id or intent_id",
            "military|political|intel|economic|symbolic",
        }
        event_intent_links: Dict[str, List[str]] = {}
        for candidate in (
            normalized.get("intent_to_event"),
            cluster_resolution.get("intent_to_event"),
            resolution_payload.get("intent_to_event"),
        ):
            if not isinstance(candidate, list):
                continue
            for item in candidate:
                if not isinstance(item, dict):
                    continue
                event_id = normalize_optional_text(item.get("event_id"))
                intent_id = normalize_optional_text(item.get("intent_id"))
                if not event_id or not intent_id:
                    continue
                event_intent_links[event_id] = dedupe_keep_order(
                    [*event_intent_links.get(event_id, []), intent_id]
                )
        for _, embedded_payload in embedded_payloads:
            candidate = embedded_payload.get("intent_to_event")
            if not isinstance(candidate, list):
                continue
            for item in candidate:
                if not isinstance(item, dict):
                    continue
                event_id = normalize_optional_text(item.get("event_id"))
                intent_id = normalize_optional_text(item.get("intent_id"))
                if not event_id or not intent_id:
                    continue
                event_intent_links[event_id] = dedupe_keep_order(
                    [*event_intent_links.get(event_id, []), intent_id]
                )
        single_event_consumed_intents = dedupe_keep_order(
            ensure_list(normalized.get("consumed_intents"))
            + ensure_list(cluster_resolution.get("consumed_intents"))
        )
        single_generated_event_count = 0
        for candidate in (
            normalized.get("generated_events"),
            cluster_resolution.get("generated_events"),
        ):
            if isinstance(candidate, list) and candidate:
                single_generated_event_count = len(candidate)
                break

        def first_list_source(*candidates: tuple[str, Any]) -> List[Any]:
            for key, value in candidates:
                if isinstance(value, list) and value:
                    alternative_keys_used.append(key)
                    return value
            return []

        def is_placeholder_template_item(item: Dict[str, Any]) -> bool:
            placeholder_hits = 0
            for field in (
                "event_id",
                "title",
                "summary",
                "lead_agent",
                "target",
                "expected_resolution",
                "reason",
            ):
                marker = normalize_optional_text(item.get(field)).lower()
                if marker in placeholder_markers:
                    placeholder_hits += 1
            return placeholder_hits >= 1

        def resolve_linked_intents(item: Dict[str, Any]) -> tuple[str, List[str]]:
            linked_intents = dedupe_keep_order(
                ensure_list(item.get("linked_intents"))
                + ensure_list(item.get("linked_intent_ids"))
                + ensure_list(item.get("linked_intent_id"))
                + ensure_list(item.get("source_intent_ids"))
                + ensure_list(item.get("source_intent_id"))
                + ensure_list(item.get("source_intents"))
                + ensure_list(item.get("source_intent"))
                + ensure_list(item.get("intent_ids"))
                + ensure_list(item.get("intent_id"))
            )
            owner_intent_id = normalize_optional_text(
                item.get("owner_intent_id")
                or item.get("source_intent_id")
                or item.get("linked_intent_id")
                or item.get("source_intent")
                or item.get("intent_id")
            )
            if owner_intent_id:
                linked_intents = dedupe_keep_order([owner_intent_id, *linked_intents])
            event_id = normalize_optional_text(item.get("event_id"))
            if event_id and event_intent_links.get(event_id):
                linked_intents = dedupe_keep_order(
                    [*event_intent_links[event_id], *linked_intents]
                )
            if linked_intents:
                return owner_intent_id or linked_intents[0], linked_intents

            if single_generated_event_count == 1 and single_event_consumed_intents:
                return single_event_consumed_intents[0], list(single_event_consumed_intents)

            if not intents:
                return "", []

            candidate_text = "\n".join(
                filter(
                    None,
                    [
                        normalize_optional_text(item.get("title")),
                        normalize_optional_text(item.get("summary") or item.get("outcome_summary") or item.get("stakes")),
                        normalize_optional_text(item.get("location")),
                        normalize_optional_text(item.get("target")),
                        " ".join(ensure_list(item.get("participants"))[:8]),
                        " ".join(ensure_list(item.get("tags"))[:8]),
                    ],
                )
            )
            lead_agent = normalize_optional_text(
                item.get("lead_agent") or item.get("primary_agent") or item.get("agent_name")
            )
            scored_matches: List[tuple[int, str]] = []
            for intent in intents:
                score = self._intent_summary_match_score(intent, candidate_text)
                if lead_agent and lead_agent == intent.agent_name:
                    score += 6
                elif intent.agent_name and intent.agent_name in candidate_text:
                    score += 3
                if score > 0:
                    scored_matches.append((score, intent.intent_id))

            inferred_ids = [
                intent_id
                for score, intent_id in sorted(scored_matches, key=lambda item: item[0], reverse=True)
                if score >= 4
            ][:3]
            if not inferred_ids and len(intents) == 1:
                inferred_ids = [intents[0].intent_id]
            return (inferred_ids[0] if inferred_ids else ""), inferred_ids

        def best_event_list_source(*candidates: tuple[str, Any]) -> tuple[str, List[Any]]:
            ranked: List[tuple[int, int, str, List[Any]]] = []
            key_bonus = {
                "accepted_events": 70,
                "generated_events": 69,
                "create_events": 68,
                "created_events": 66,
                "new_active_events": 64,
                "queue_events": 63,
                "queued_events": 62,
                "recommended_queued_events": 61,
                "recommended_active_events": 58,
                "active_events": 54,
                "event_results": 46,
                "resolved_events": 40,
            }
            for index, (key, value) in enumerate(candidates):
                if not isinstance(value, list) or not value:
                    continue
                score = key_bonus.get(key.split(".")[-1], 32)
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    if is_placeholder_template_item(item):
                        score -= 4
                        continue
                    owner_intent_id, linked_intents = resolve_linked_intents(item)
                    if linked_intents:
                        score += 12 + min(len(linked_intents), 3)
                    elif owner_intent_id:
                        score += 10
                    else:
                        score += 1

                    status = normalize_optional_text(item.get("status")).lower()
                    if status in {"active", "queued"}:
                        score += 2
                    elif status in {"resolved", "completed"}:
                        score -= 2

                    if normalize_optional_text(item.get("summary") or item.get("outcome") or item.get("stakes")):
                        score += 1
                ranked.append((score, -index, key, value))

            if not ranked:
                return "", []

            ranked.sort(reverse=True)
            best_key = ranked[0][2]
            alternative_keys_used.append(best_key)
            return best_key, ranked[0][3]

        nested_event_candidates: List[tuple[str, Any]] = []
        nested_queue_candidates: List[tuple[str, Any]] = []
        nested_deferred_candidates: List[tuple[str, Any]] = []
        nested_rejected_candidates: List[tuple[str, Any]] = []
        for container_key, container_payload in nested_event_containers:
            nested_event_candidates.extend(
                [
                    (f"{container_key}.accepted_events", container_payload.get("accepted_events")),
                    (f"{container_key}.generated_events", container_payload.get("generated_events")),
                    (f"{container_key}.created_events", container_payload.get("created_events")),
                    (f"{container_key}.new_active_events", container_payload.get("new_active_events")),
                    (f"{container_key}.queue_events", container_payload.get("queue_events")),
                    (f"{container_key}.queued_events", container_payload.get("queued_events")),
                    (f"{container_key}.resolved_events", container_payload.get("resolved_events")),
                    (f"{container_key}.event_results", container_payload.get("event_results")),
                    (f"{container_key}.active_events", container_payload.get("active_events")),
                ]
            )
            nested_queue_candidates.append((f"{container_key}.queued_events", container_payload.get("queued_events")))
            nested_deferred_candidates.append((f"{container_key}.deferred_intents", container_payload.get("deferred_intents")))
            nested_rejected_candidates.append((f"{container_key}.rejected_intents", container_payload.get("rejected_intents")))

        selected_event_source_key, source_events = best_event_list_source(
            ("create_events", normalized.get("create_events")),
            ("generated_events", normalized.get("generated_events")),
            ("created_events", normalized.get("created_events")),
            ("new_active_events", normalized.get("new_active_events")),
            ("queue_events", normalized.get("queue_events")),
            ("queued_events", normalized.get("queued_events")),
            ("recommended_queued_events", normalized.get("recommended_queued_events")),
            ("recommended_active_events", normalized.get("recommended_active_events")),
            ("event_results", normalized.get("event_results")),
            ("resolved_events", normalized.get("resolved_events")),
            ("active_events", normalized.get("active_events")),
            *nested_event_candidates,
        )
        event_source_is_queue = selected_event_source_key.split(".")[-1] in {
            "queue_events",
            "queued_events",
            "recommended_queued_events",
        }
        if event_source_is_queue:
            queued_source_items = []
        else:
            queued_source_items = first_list_source(
                ("queue_events", normalized.get("queue_events")),
                ("queued_events", normalized.get("queued_events")),
                ("recommended_queued_events", normalized.get("recommended_queued_events")),
                *nested_queue_candidates,
            )
        deferred_source_items = first_list_source(
            ("update_existing_events", normalized.get("update_existing_events")),
            *nested_deferred_candidates,
        )
        rejected_source_items = first_list_source(
            ("discarded_intents", normalized.get("discarded_intents")),
            *nested_rejected_candidates,
        )
        def convert_event_items(
            items: Any,
            *,
            fallback_rationale: str = "",
            forced_status: str = "",
        ) -> List[Dict[str, Any]]:
            converted: List[Dict[str, Any]] = []
            if not isinstance(items, list) or not items:
                return converted

            for item in items:
                if not isinstance(item, dict):
                    continue
                if is_placeholder_template_item(item):
                    continue
                owner_intent_id, linked_intents = resolve_linked_intents(item)
                converted.append(
                    {
                        "title": item.get("title", ""),
                        "summary": item.get("summary")
                        or item.get("outcome")
                        or item.get("outcome_summary")
                        or item.get("stakes")
                        or "",
                        "owner_intent_id": owner_intent_id,
                        "supporting_intent_ids": linked_intents[1:],
                        "priority": item.get("priority"),
                        "duration_ticks": item.get(
                            "duration_ticks",
                            item.get(
                                "expected_duration",
                                item.get("duration", item.get("remaining_duration", item.get("projected_duration", 1))),
                            ),
                        ),
                        "location": item.get("location", ""),
                        "dependencies": ensure_list(item.get("dependencies")),
                        "participants": ensure_list(item.get("participants")),
                        "state_impacts": item.get("state_impacts")
                        or item.get("state_impacts_applied")
                        or item.get("applied_state_impacts")
                        or {},
                        "status": forced_status or normalize_optional_text(item.get("status")),
                        "rationale": (
                            item.get("stakes")
                            or "；".join(ensure_list(item.get("consequences"))[:3])
                            or item.get("next_tick_risk")
                            or fallback_rationale
                            or normalized.get("resolution_summary")
                            or normalized.get("scene_summary", "")
                            or normalized.get("tick_summary", "")
                            or normalized.get("summary", "")
                        ),
                    }
                )
            return converted

        converted_events: List[Dict[str, Any]] = convert_event_items(source_events)

        converted_events_with_links = [
            item
            for item in converted_events
            if normalize_optional_text(item.get("owner_intent_id"))
            or ensure_list(item.get("supporting_intent_ids"))
        ]
        converted_queued_events: List[Dict[str, Any]] = convert_event_items(
            queued_source_items,
            fallback_rationale="queued by resolver alternative schema",
            forced_status="queued",
        )
        converted_queued_events_with_links = [
            item
            for item in converted_queued_events
            if normalize_optional_text(item.get("owner_intent_id"))
            or ensure_list(item.get("supporting_intent_ids"))
        ]
        if converted_events_with_links or converted_queued_events_with_links:
            normalized["accepted_events"] = [
                *converted_events_with_links,
                *converted_queued_events_with_links,
            ]
            selection_summary = normalized.get("selection_summary") or {}
            deferred_ids = ensure_list(selection_summary.get("deferred_intents"))
            converted_deferred: List[Dict[str, Any]] = [
                {
                    "intent_id": intent_id,
                    "reason": str(
                        selection_summary.get("reason") or "deferred by resolver alternative schema"
                    ).strip(),
                }
                for intent_id in deferred_ids
            ]
            if deferred_source_items and not normalized.get("deferred_intents"):
                converted_deferred_source: List[Dict[str, str]] = []
                for item in deferred_source_items:
                    if not isinstance(item, dict):
                        continue
                    intent_ids = dedupe_keep_order(
                        ensure_list(item.get("intent_id"))
                        + ensure_list(item.get("intent_ids"))
                        + ensure_list(item.get("add_source_intents"))
                        + ensure_list(item.get("source_intents"))
                    )
                    reason = str(
                        item.get("reason")
                        or item.get("status")
                        or item.get("merge_reason")
                        or "deferred by resolver alternative schema"
                    ).strip()
                    for intent_id in intent_ids:
                        if intent_id:
                            converted_deferred_source.append({"intent_id": intent_id, "reason": reason})
                if converted_deferred_source:
                    normalized["deferred_intents"] = converted_deferred_source
            if rejected_source_items and not normalized.get("rejected_intents"):
                converted_rejected_source: List[Dict[str, str]] = []
                for item in rejected_source_items:
                    if not isinstance(item, dict):
                        continue
                    intent_ids = dedupe_keep_order(
                        ensure_list(item.get("intent_id"))
                        + ensure_list(item.get("intent_ids"))
                        + ensure_list(item.get("discarded_intents"))
                        + ensure_list(item.get("source_intents"))
                    )
                    reason = str(item.get("reason", "rejected by resolver alternative schema")).strip()
                    for intent_id in intent_ids:
                        if intent_id:
                            converted_rejected_source.append({"intent_id": intent_id, "reason": reason})
                if converted_rejected_source:
                    normalized["rejected_intents"] = converted_rejected_source
            if converted_deferred and not normalized.get("deferred_intents"):
                normalized["deferred_intents"] = converted_deferred

            meta["alternative_schema_used"] = True
            meta["converted_active_events"] = len(
                converted_events_with_links + converted_queued_events_with_links
            )
            meta["alternative_schema_keys"] = dedupe_keep_order(
                alternative_keys_used
                + [
                    key
                    for key in (
                        "dispatch_summary",
                        "scene_summary",
                        "resolution_summary",
                        "selection_summary",
                        "tick_summary",
                        "summary",
                        "projected_trends",
                        "cluster_resolution",
                        "event_generation_result",
                        "event_processing",
                    )
                    if key in normalized
                ]
            )
            return normalized, meta

        if intents:
            normalized, placeholder_meta = self._bootstrap_resolver_placeholder_payload(
                normalized,
                intents,
            )
            if placeholder_meta.get("placeholder_bootstrap_used"):
                meta.update(placeholder_meta)
                return normalized, meta
            normalized, summary_meta = self._bootstrap_resolver_summary_payload(
                normalized,
                intents,
            )
            if summary_meta.get("summary_bootstrap_used"):
                meta.update(summary_meta)
        return normalized, meta

    def _resolver_summary_text_candidates(self, response: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        cluster_resolution = response.get("cluster_resolution") or {}
        if not isinstance(cluster_resolution, dict):
            cluster_resolution = {}

        def add(value: Any) -> None:
            text = normalize_optional_text(value)
            if text and len(text) >= 18:
                candidates.append(text)

        add(response.get("resolution_summary"))
        add(response.get("scene_summary"))
        add(response.get("dispatch_summary"))
        add(response.get("tick_summary"))
        add(response.get("summary"))
        add(response.get("_repaired_response"))
        add(response.get("_raw_response"))
        add(cluster_resolution.get("summary"))
        selection_summary = response.get("selection_summary") or {}
        if isinstance(selection_summary, dict):
            add(selection_summary.get("reason"))
        world_state = response.get("world_state") or {}
        if isinstance(world_state, dict):
            add(world_state.get("last_tick_summary"))
        for note in ensure_list(response.get("consistency_notes"))[:3]:
            add(note)
        for note in ensure_list(response.get("interaction_notes"))[:3]:
            add(note)
        return dedupe_keep_order(candidates)

    def _bootstrap_resolver_placeholder_payload(
        self,
        response: Dict[str, Any],
        intents: List[ActorIntent],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        meta: Dict[str, Any] = {
            "placeholder_bootstrap_used": False,
            "placeholder_bootstrap_keys": [],
            "placeholder_bootstrap_events": 0,
        }
        normalized = dict(response or {})
        if not intents:
            return normalized, meta

        placeholder_markers = {
            "string",
            "event_id or intent_id",
            "military|political|intel|economic|symbolic",
        }
        seen_keys: List[str] = []
        placeholder_hits = 0

        def visit(value: Any, key_path: str = "") -> None:
            nonlocal placeholder_hits
            if placeholder_hits >= 4:
                return
            if isinstance(value, dict):
                for key, child in value.items():
                    child_path = f"{key_path}.{key}" if key_path else str(key)
                    visit(child, child_path)
                return
            if isinstance(value, list):
                for index, child in enumerate(value[:8]):
                    child_path = f"{key_path}[{index}]" if key_path else f"[{index}]"
                    visit(child, child_path)
                return
            if isinstance(value, str):
                marker = value.strip().lower()
                if marker in placeholder_markers:
                    placeholder_hits += 1
                    if key_path:
                        seen_keys.append(key_path)

        visit(normalized)
        if placeholder_hits < 2:
            return normalized, meta

        normalized["accepted_events"] = [
            {
                "title": intent.objective,
                "summary": intent.summary,
                "owner_intent_id": intent.intent_id,
                "supporting_intent_ids": [],
                "priority": intent.priority,
                "duration_ticks": intent.desired_duration,
                "location": intent.location,
                "dependencies": intent.dependencies,
                "participants": intent.participants,
                "state_impacts": intent.state_impacts,
                "rationale": "bootstrapped from resolver placeholder template output",
            }
            for intent in intents
        ]
        meta["placeholder_bootstrap_used"] = True
        meta["placeholder_bootstrap_keys"] = dedupe_keep_order(seen_keys)[:10]
        meta["placeholder_bootstrap_events"] = len(intents)
        return normalized, meta

    def _resolver_summary_sentences(self, texts: List[str]) -> List[str]:
        sentences: List[str] = []
        for text in texts:
            normalized = normalize_optional_text(text)
            if not normalized:
                continue
            for chunk in re.split(r"[。！？!?；;\n]+", normalized):
                sentence = normalize_optional_text(chunk)
                if sentence and len(sentence) >= 10:
                    sentences.append(sentence)
        return dedupe_keep_order(sentences)

    def _intent_summary_match_score(
        self,
        intent: ActorIntent,
        sentence: str,
    ) -> int:
        normalized_sentence = normalize_optional_text(sentence)
        if not normalized_sentence:
            return 0

        lowered_sentence = normalized_sentence.lower()
        score = 0
        if intent.agent_name and intent.agent_name in normalized_sentence:
            score += 5

        target = normalize_optional_text(intent.target)
        if target and target in normalized_sentence:
            score += 3

        location = normalize_optional_text(intent.location)
        if location and location in normalized_sentence:
            score += 2

        for participant in intent.participants[:6]:
            candidate = normalize_optional_text(participant)
            if not candidate or candidate == intent.agent_name or candidate == target:
                continue
            if candidate in normalized_sentence:
                score += 2

        signal_tokens = dedupe_keep_order(
            extract_signal_tokens(
                "\n".join(
                    [
                        intent.objective,
                        intent.summary,
                        target,
                        location,
                        " ".join(intent.participants),
                        " ".join(intent.tags),
                    ]
                )
            )
        )
        token_hits = 0
        for token in signal_tokens:
            if token in normalized_sentence or token.lower() in lowered_sentence:
                token_hits += 1
        score += min(token_hits, 4)
        return score

    def _bootstrap_resolver_summary_payload(
        self,
        response: Dict[str, Any],
        intents: List[ActorIntent],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        meta: Dict[str, Any] = {
            "summary_bootstrap_used": False,
            "summary_bootstrap_keys": [],
            "summary_bootstrap_events": 0,
            "summary_bootstrap_deferred": 0,
            "summary_bootstrap_rejected": 0,
        }
        normalized = dict(response or {})
        cluster_resolution = normalized.get("cluster_resolution") or {}
        if not isinstance(cluster_resolution, dict):
            cluster_resolution = {}
        summary_keys = [
            key
            for key in (
                "resolution_summary",
                "scene_summary",
                "tick_summary",
                "summary",
                "selection_summary",
                "world_state",
                "consistency_notes",
                "interaction_notes",
                "cluster_resolution",
                "_repaired_response",
                "_raw_response",
            )
            if key in normalized
        ]
        summary_texts = self._resolver_summary_text_candidates(normalized)
        summary_sentences = self._resolver_summary_sentences(summary_texts)
        if not summary_sentences:
            return normalized, meta

        defer_markers = (
            "暂未",
            "尚未",
            "未获",
            "等待",
            "排队",
            "queued",
            "queue",
            "待定",
            "暂缓",
            "延后",
            "推迟",
            "窗口尚未打开",
            "未打开",
        )
        reject_markers = (
            "驳回",
            "否决",
            "拒绝",
            "不予",
            "取消",
            "中止",
            "放弃",
            "失败",
        )
        accept_markers = (
            "状态： active",
            "状态: active",
            "status: active",
            "结算轮次",
            "被整理为事件",
            "转化为事件",
            "新增活跃事件",
            "新活跃事件",
            "被整理为新活跃事件",
            "转化为新活跃事件",
        )

        accepted_events: List[Dict[str, Any]] = []
        deferred_intents: List[Dict[str, Any]] = []
        rejected_intents: List[Dict[str, Any]] = []

        for intent in intents:
            scored_sentences = sorted(
                (
                    (self._intent_summary_match_score(intent, sentence), sentence)
                    for sentence in summary_sentences
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            matched_sentences = [
                sentence
                for score, sentence in scored_sentences
                if score >= 4
            ][:2]
            if not matched_sentences:
                continue

            summary_text = "；".join(matched_sentences)
            lowered_summary = summary_text.lower()
            has_accept_marker = any(
                marker in summary_text or marker in lowered_summary
                for marker in accept_markers
            )

            if (not has_accept_marker) and any(
                marker in summary_text or marker in lowered_summary for marker in defer_markers
            ):
                deferred_intents.append(
                    {
                        "intent_id": intent.intent_id,
                        "reason": clip_text(summary_text, 220),
                    }
                )
                continue

            if (not has_accept_marker) and any(
                marker in summary_text or marker in lowered_summary for marker in reject_markers
            ):
                rejected_intents.append(
                    {
                        "intent_id": intent.intent_id,
                        "reason": clip_text(summary_text, 220),
                    }
                )
                continue

            accepted_events.append(
                {
                    "title": intent.objective,
                    "summary": summary_text,
                    "owner_intent_id": intent.intent_id,
                    "supporting_intent_ids": [],
                    "priority": intent.priority,
                    "duration_ticks": intent.desired_duration,
                    "location": intent.location,
                    "dependencies": intent.dependencies,
                    "participants": intent.participants,
                    "state_impacts": intent.state_impacts,
                    "rationale": f"bootstrapped from resolver summary: {clip_text(summary_text, 180)}",
                }
            )

        if not accepted_events and not deferred_intents and not rejected_intents:
            return normalized, meta

        if accepted_events and not normalized.get("accepted_events"):
            normalized["accepted_events"] = accepted_events
        if deferred_intents and not normalized.get("deferred_intents"):
            normalized["deferred_intents"] = deferred_intents
        if rejected_intents and not normalized.get("rejected_intents"):
            normalized["rejected_intents"] = rejected_intents

        meta["summary_bootstrap_used"] = True
        meta["summary_bootstrap_keys"] = summary_keys
        meta["summary_bootstrap_events"] = len(accepted_events)
        meta["summary_bootstrap_deferred"] = len(deferred_intents)
        meta["summary_bootstrap_rejected"] = len(rejected_intents)
        return normalized, meta

    def _parse_resolver_response(
        self,
        tick: int,
        intents: List[ActorIntent],
        response: Dict[str, Any],
        source: str,
    ) -> Dict[str, Any]:
        response_payload, normalization_meta = self._normalize_resolver_response_payload(
            response,
            intents=intents,
        )
        intent_map = {intent.intent_id: intent for intent in intents}
        accepted_events: List[WorldEvent] = []
        deferred_map: Dict[str, str] = {}
        rejected_map: Dict[str, str] = {}
        raw_accepted_items = response_payload.get("accepted_events", []) or []
        raw_deferred_items = response_payload.get("deferred_intents", []) or []
        raw_rejected_items = response_payload.get("rejected_intents", []) or []
        diagnostics: Dict[str, Any] = {
            "intent_count": len(intents),
            "accepted_events_raw_count": len(raw_accepted_items),
            "accepted_events_valid_count": 0,
            "deferred_raw_count": len(raw_deferred_items),
            "rejected_raw_count": len(raw_rejected_items),
            "deferred_valid_count": 0,
            "rejected_valid_count": 0,
            "dropped_accepted_events": [],
            "reason_code": "",
            "normalization": normalization_meta,
        }

        for item in raw_deferred_items:
            intent_id = str(item.get("intent_id", "")).strip()
            if intent_id:
                deferred_map[intent_id] = str(item.get("reason", "deferred by resolver")).strip()
        diagnostics["deferred_valid_count"] = len(deferred_map)

        for item in raw_rejected_items:
            intent_id = str(item.get("intent_id", "")).strip()
            if intent_id:
                rejected_map[intent_id] = str(item.get("reason", "rejected by resolver")).strip()
        diagnostics["rejected_valid_count"] = len(rejected_map)

        for index, item in enumerate(raw_accepted_items):
            event, drop_reason = self._build_event_from_resolved_item(
                tick=tick,
                item=item,
                intent_map=intent_map,
                source=source,
            )
            if event:
                accepted_events.append(event)
            else:
                diagnostics["dropped_accepted_events"].append(
                    {
                        "index": index,
                        "reason_code": drop_reason,
                        "owner_intent_id": str(item.get("owner_intent_id", "")).strip()
                        if isinstance(item, dict)
                        else "",
                        "supporting_intent_ids": ensure_list(item.get("supporting_intent_ids"))
                        if isinstance(item, dict)
                        else [],
                        "title": clip_text(item.get("title", ""), 120)
                        if isinstance(item, dict)
                        else clip_text(item, 120),
                    }
                )
        diagnostics["accepted_events_valid_count"] = len(accepted_events)
        accepted_intent_ids = {
            intent_id
            for event in accepted_events
            for intent_id in event.source_intent_ids
        }
        unresolved_intent_ids = [
            intent.intent_id
            for intent in intents
            if intent.intent_id not in accepted_intent_ids
            and intent.intent_id not in deferred_map
            and intent.intent_id not in rejected_map
        ]
        diagnostics["accepted_intent_count"] = len(accepted_intent_ids)
        diagnostics["unresolved_intent_ids"] = unresolved_intent_ids

        if accepted_events:
            if normalization_meta.get("placeholder_bootstrap_used"):
                diagnostics["reason_code"] = "resolver_placeholder_bootstrapped"
            elif normalization_meta.get("summary_bootstrap_used"):
                diagnostics["reason_code"] = "resolver_summary_bootstrapped"
            elif normalization_meta.get("alternative_schema_used"):
                diagnostics["reason_code"] = "resolver_accepted_alt_schema"
            else:
                diagnostics["reason_code"] = (
                    "resolver_partial_schema_drop"
                    if diagnostics["accepted_events_valid_count"] < diagnostics["accepted_events_raw_count"]
                    else "resolver_accepted"
                )
        elif diagnostics["accepted_events_raw_count"] > 0:
            diagnostics["reason_code"] = "resolver_schema_drop"
        elif len(deferred_map) >= len(intents):
            diagnostics["reason_code"] = "resolver_deferred_all"
        elif len(rejected_map) >= len(intents):
            diagnostics["reason_code"] = "resolver_rejected_all"
        elif deferred_map or rejected_map:
            diagnostics["reason_code"] = "resolver_zero_accept_mixed"
        else:
            diagnostics["reason_code"] = "resolver_empty_accepted"

        return {
            "accepted_events": accepted_events,
            "deferred_map": deferred_map,
            "rejected_map": rejected_map,
            "diagnostics": diagnostics,
        }

    async def _resolve_single_cluster_with_llm(
        self,
        tick: int,
        scene_title: str,
        cluster: List[ActorIntent],
        cluster_index: int,
        cluster_count: int,
    ) -> Dict[str, Any]:
        cluster_focus = self._cluster_focus_label(cluster, scene_title)
        return await self._resolve_with_llm_cluster(
            tick=tick,
            scene_title=scene_title,
            cluster_label=cluster_focus,
            cluster_index=cluster_index,
            cluster_count=cluster_count,
            intents=cluster,
        )

    async def _resolve_with_llm(
        self,
        tick: int,
        scene_title: str,
        intents: List[ActorIntent],
    ) -> Dict[str, Any]:
        accepted_events: List[WorldEvent] = []
        deferred_map: Dict[str, str] = {}
        rejected_map: Dict[str, str] = {}
        cluster_diagnostics: List[Dict[str, Any]] = []
        clusters = self._cluster_intents_for_resolver(intents, scene_title)
        if len(clusters) > 1:
            self._write_meta_event(
                {
                    "event_type": "resolver_clustered",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "round": tick,
                    "tick": tick,
                    "phase": "resolver",
                    "summary": f"Resolver split tick {tick} into {len(clusters)} concurrent clusters.",
                    "context": scene_title,
                    "clusters": [
                        {
                            "label": self._cluster_focus_label(cluster, scene_title),
                            "intent_count": len(cluster),
                        }
                        for cluster in clusters
                    ],
                }
            )
        semaphore = asyncio.Semaphore(max(self.resolver_cluster_concurrency, 1))

        async def resolve_cluster(cluster_index: int, cluster: List[ActorIntent]) -> Dict[str, Any]:
            async with semaphore:
                try:
                    return await self._resolve_single_cluster_with_llm(
                        tick=tick,
                        scene_title=scene_title,
                        cluster=cluster,
                        cluster_index=cluster_index,
                        cluster_count=len(clusters),
                    )
                except InvalidJSONResponseError:
                    self._write_meta_event(
                        {
                            "event_type": "resolver_cluster_salvaged",
                            "timestamp": now_iso(),
                            "simulation_mode": "world",
                            "round": tick,
                            "tick": tick,
                            "phase": "resolver",
                            "summary": (
                                f"Resolver cluster {cluster_index}/{len(clusters)} returned invalid JSON; "
                                "using heuristic salvage for this cluster."
                            ),
                            "context": self._cluster_focus_label(cluster, scene_title),
                        }
                    )
                    return self._resolve_heuristically(
                        tick=tick,
                        scene_title=scene_title,
                        intents=cluster,
                        apply_capacity=False,
                        source="heuristic_cluster_salvage",
                        rejection_reason="not selected during cluster salvage",
                    )

        tasks = [
            asyncio.create_task(resolve_cluster(index + 1, cluster))
            for index, cluster in enumerate(clusters)
        ]
        for task in asyncio.as_completed(tasks):
            result = await task
            accepted_events.extend(result.get("accepted_events", []))
            deferred_map.update(result.get("deferred_map", {}))
            rejected_map.update(result.get("rejected_map", {}))
            if result.get("diagnostics"):
                cluster_diagnostics.append(result["diagnostics"])

        if not accepted_events:
            salvage_candidates = [
                intent
                for intent in intents
                if intent.intent_id not in deferred_map
            ]
            total_raw_accepted = sum(
                safe_int(item.get("accepted_events_raw_count", 0), default=0, lower=0)
                for item in cluster_diagnostics
            )
            if any(item.get("reason_code") == "resolver_schema_drop" for item in cluster_diagnostics):
                zero_accept_reason = "resolver_schema_drop"
            elif cluster_diagnostics and all(
                item.get("reason_code") == "resolver_deferred_all" for item in cluster_diagnostics
            ):
                zero_accept_reason = "resolver_deferred_all"
            elif cluster_diagnostics and all(
                item.get("reason_code") == "resolver_rejected_all" for item in cluster_diagnostics
            ):
                zero_accept_reason = "resolver_rejected_all"
            elif total_raw_accepted == 0 and cluster_diagnostics and all(
                item.get("reason_code") in {"resolver_empty_accepted", "resolver_zero_accept_mixed"}
                for item in cluster_diagnostics
            ):
                zero_accept_reason = "resolver_empty_accepted"
            else:
                zero_accept_reason = "resolver_zero_accept_mixed"

            self._write_meta_event(
                {
                    "event_type": "resolver_zero_accept_diagnostic",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "round": tick,
                    "tick": tick,
                    "phase": "resolver",
                    "summary": (
                        f"Resolver produced zero accepted events across {len(clusters)} clusters "
                        f"({zero_accept_reason})."
                    ),
                    "context": scene_title,
                    "reason_code": zero_accept_reason,
                    "cluster_count": len(clusters),
                    "intent_count": len(intents),
                    "candidate_count": len(salvage_candidates),
                    "accepted_events_raw_count": total_raw_accepted,
                    "deferred_count": len(deferred_map),
                    "rejected_count": len(rejected_map),
                    "cluster_diagnostics": cluster_diagnostics,
                }
            )
            if salvage_candidates and self.resolver_salvage_on_zero_accept:
                self._write_meta_event(
                    {
                        "event_type": "resolver_salvaged",
                        "timestamp": now_iso(),
                        "simulation_mode": "world",
                        "round": tick,
                        "tick": tick,
                        "phase": "resolver",
                        "summary": (
                            f"Resolver returned 0 accepted events across {len(clusters)} clusters; "
                            "applying heuristic salvage to keep the world moving."
                        ),
                        "context": scene_title,
                        "cluster_count": len(clusters),
                        "candidate_count": len(salvage_candidates),
                        "reason_code": zero_accept_reason,
                    }
                )
                salvaged = self._resolve_heuristically(
                    tick=tick,
                    scene_title=scene_title,
                    intents=salvage_candidates,
                    apply_capacity=False,
                    max_events=max(1, min(3, len(clusters))),
                    source="heuristic_salvage",
                    rejection_reason="not selected during zero-accept salvage",
                )
                accepted_events.extend(salvaged.get("accepted_events", []))
                deferred_map.update(salvaged.get("deferred_map", {}))
                rejected_map.update(salvaged.get("rejected_map", {}))
            elif self._semantic_fallback_enabled():
                return self._resolve_heuristically(tick, scene_title, intents)

        accepted_events = self._apply_capacity_limits(accepted_events, deferred_map)
        accepted_ids = {
            intent_id
            for event in accepted_events
            for intent_id in event.source_intent_ids
        }
        rejected_map = {
            intent_id: reason
            for intent_id, reason in rejected_map.items()
            if intent_id not in accepted_ids and intent_id not in deferred_map
        }
        for intent in intents:
            if (
                intent.intent_id not in accepted_ids
                and intent.intent_id not in deferred_map
                and intent.intent_id not in rejected_map
            ):
                rejected_map[intent.intent_id] = "resolver returned no actionable concurrent event"

        return {
            "accepted_events": accepted_events,
            "deferred_map": deferred_map,
            "rejected_map": rejected_map,
        }

    def _resolve_heuristically(
        self,
        tick: int,
        scene_title: str,
        intents: List[ActorIntent],
        apply_capacity: bool = True,
        max_events: Optional[int] = None,
        source: str = "heuristic",
        rejection_reason: str = "superseded by higher-priority concurrent events",
    ) -> Dict[str, Any]:
        deferred_map: Dict[str, str] = {}
        rejected_map: Dict[str, str] = {}
        accepted_events: List[WorldEvent] = []
        for cluster in self._cluster_intents_for_resolver(intents, scene_title):
            source_intents = cluster
            primary = source_intents[0]
            dependencies = dedupe_keep_order(
                [dep for intent in source_intents for dep in intent.dependencies if dep in self.active_events or dep in self.queued_events]
            )
            duration = min(
                self.max_event_duration,
                max(intent.desired_duration for intent in source_intents),
            )
            cluster_focus = self._cluster_focus_label(source_intents, scene_title)
            title, primary_summary = self._normalize_text_intent_fields(
                objective=primary.objective,
                summary=primary.summary,
                lines=[primary.objective, primary.summary, *[intent.summary for intent in source_intents[1:3]]],
                agent_name=primary.agent_name,
                scene_title=scene_title,
            )
            if cluster_focus and cluster_focus != scene_title and cluster_focus not in title:
                title = f"{cluster_focus}：{title}"[:180]
            summary_parts = dedupe_keep_order(
                [
                    self._clean_intent_candidate(intent.summary, agent_name=intent.agent_name)
                    for intent in source_intents
                ]
            )
            summary = "；".join(part for part in summary_parts if part)[:360] or primary_summary
            location = primary.location if not self._is_placeholder_text(primary.location, scene_title=scene_title) else cluster_focus
            event = WorldEvent(
                event_id=self._next_event_id(tick),
                tick=tick,
                title=title[:180],
                summary=summary[:360],
                primary_agent_id=primary.agent_id,
                primary_agent_name=primary.agent_name,
                participants=dedupe_keep_order(
                    [participant for intent in source_intents for participant in intent.participants]
                )[:8],
                participant_ids=dedupe_keep_order([str(intent.agent_id) for intent in source_intents]),
                source_intent_ids=[intent.intent_id for intent in source_intents],
                priority=max(intent.priority for intent in source_intents),
                duration_ticks=duration,
                resolves_at_tick=tick + duration - 1,
                status="queued" if dependencies else "active",
                location=str(location).strip()[:120],
                dependencies=dependencies,
                state_impacts=self._merge_state_impacts(source_intents),
                source=source,
                rationale=f"grouped by cluster focus: {cluster_focus}",
            )
            accepted_events.append(event)

        accepted_events = sorted(
            accepted_events,
            key=lambda event: (event.priority, len(event.source_intent_ids), event.duration_ticks),
            reverse=True,
        )
        if max_events is not None:
            accepted_events = accepted_events[: max(max_events, 1)]

        if apply_capacity:
            accepted_events = self._apply_capacity_limits(accepted_events, deferred_map)

        accepted_ids = set()
        for event in accepted_events:
            accepted_ids.update(event.source_intent_ids)

        for intent in intents:
            if intent.intent_id not in accepted_ids and intent.intent_id not in deferred_map:
                rejected_map[intent.intent_id] = rejection_reason

        return {
            "accepted_events": accepted_events,
            "deferred_map": deferred_map,
            "rejected_map": rejected_map,
        }

    def _apply_capacity_limits(
        self,
        events: List[WorldEvent],
        deferred_map: Dict[str, str],
    ) -> List[WorldEvent]:
        accepted: List[WorldEvent] = []
        active_slots = max(self.max_active_events - len(self.active_events), 0)
        queue_slots = max(self.max_queued_events - len(self.queued_events), 0)

        ordered_events = sorted(events, key=lambda item: (item.priority, -len(item.dependencies)), reverse=True)
        for event in ordered_events:
            if event.dependencies:
                if queue_slots <= 0:
                    for intent_id in event.source_intent_ids:
                        deferred_map[intent_id] = "queue capacity reached"
                    continue
                event.status = "queued"
                queue_slots -= 1
                accepted.append(event)
                continue

            if active_slots > 0:
                event.status = "active"
                active_slots -= 1
                accepted.append(event)
                continue

            if queue_slots > 0:
                event.status = "queued"
                queue_slots -= 1
                accepted.append(event)
                continue

            for intent_id in event.source_intent_ids:
                deferred_map[intent_id] = "active and queue capacities are both full"

        return accepted

    def _accept_event(self, event: WorldEvent) -> None:
        participant_ids = dedupe_keep_order(
            [str(event.primary_agent_id), *[str(value) for value in event.participant_ids]]
        )
        for value in participant_ids:
            if not value.isdigit():
                continue
            actor_id = int(value)
            self.actor_last_event_tick[actor_id] = event.tick
            if event.event_id not in self._counted_event_ids:
                self.actor_event_counts[actor_id] = self.actor_event_counts.get(actor_id, 0) + 1
        self._counted_event_ids.add(event.event_id)

        if event.status == "queued":
            self.queued_events[event.event_id] = event
            self._write_action(event.queue_log(len(self.queued_events)))
            return

        event.status = "active"
        self.active_events[event.event_id] = event
        self._write_action(event.start_log(len(self.active_events), len(self.queued_events)))

    def _promote_queued_events(self, tick: int) -> None:
        promotable = [
            event_id
            for event_id, event in self.queued_events.items()
            if all(dep not in self.active_events and dep not in self.queued_events for dep in event.dependencies)
        ]

        for event_id in promotable:
            if len(self.active_events) >= self.max_active_events:
                break
            event = self.queued_events.pop(event_id)
            event.status = "active"
            event.tick = tick
            event.resolves_at_tick = max(event.resolves_at_tick, tick + event.duration_ticks - 1)
            for value in dedupe_keep_order(
                [str(event.primary_agent_id), *[str(participant_id) for participant_id in event.participant_ids]]
            ):
                if value.isdigit():
                    self.actor_last_event_tick[int(value)] = tick
            self.active_events[event.event_id] = event
            self._write_action(event.start_log(len(self.active_events), len(self.queued_events)))

    def _complete_due_events(self, tick: int) -> List[WorldEvent]:
        due_event_ids = [
            event_id
            for event_id, event in self.active_events.items()
            if event.resolves_at_tick <= tick
        ]
        completed: List[WorldEvent] = []

        for event_id in due_event_ids:
            event = self.active_events.pop(event_id)
            event.status = "completed"
            self.completed_events.append(event)
            completed.append(event)
            self._apply_event_impacts(event)
            self._write_action(event.complete_log(tick, len(self.active_events), len(self.queued_events)))

        return completed

    def _apply_event_impacts(self, event: WorldEvent) -> None:
        pressure_tracks = self.world_state.setdefault("pressure_tracks", {})
        for key, delta in event.state_impacts.items():
            delta = clamp(safe_float(delta), -0.25, 0.25)
            if key in {"conflict", "scarcity", "legitimacy"}:
                pressure_tracks[key] = clamp(safe_float(pressure_tracks.get(key, 0.35)) + delta, 0.05, 0.95)
            elif key in {"tension", "stability", "momentum"}:
                self.world_state[key] = clamp(safe_float(self.world_state.get(key, 0.35)) + delta, 0.05, 0.95)

        conflict = safe_float(pressure_tracks.get("conflict", self.world_state.get("tension", 0.35)), 0.35)
        scarcity = safe_float(pressure_tracks.get("scarcity", 0.30), 0.30)
        legitimacy = safe_float(pressure_tracks.get("legitimacy", 0.45), 0.45)
        momentum = clamp(
            safe_float(self.world_state.get("momentum", 0.35), 0.35)
            + (0.015 * event.priority)
            + (0.010 * len(event.participants))
            - (0.010 * len(event.dependencies)),
            0.05,
            0.95,
        )
        tension = clamp((conflict * 0.60) + (scarcity * 0.25) + (momentum * 0.15), 0.05, 0.95)
        stability = clamp((legitimacy * 0.50) + ((1 - conflict) * 0.35) + ((1 - scarcity) * 0.15), 0.05, 0.95)

        self.world_state.update(
            {
                "momentum": round(momentum, 3),
                "tension": round(tension, 3),
                "stability": round(stability, 3),
            }
        )

    def _capture_snapshot(
        self,
        tick: int,
        scene_title: str,
        intents: List[ActorIntent],
        resolution: Dict[str, Any],
        completed_this_tick: List[WorldEvent],
    ) -> Dict[str, Any]:
        self.world_state["active_event_ids"] = list(self.active_events.keys())
        self.world_state["queued_event_ids"] = list(self.queued_events.keys())
        self.world_state["completed_event_ids"] = [event.event_id for event in self.completed_events[-20:]]

        summary = self._build_tick_summary(
            tick,
            scene_title,
            len(intents),
            resolution,
            completed_this_tick,
        )
        self.world_state["last_tick_summary"] = summary

        snapshot = {
            "round": tick,
            "tick": tick,
            "timestamp": now_iso(),
            "scene_title": scene_title,
            "phase": "tick_complete",
            "simulated_hours": round(tick * self.minutes_per_round / 60, 2),
            "summary": summary,
            "world_state": {
                "tension": self.world_state["tension"],
                "stability": self.world_state["stability"],
                "momentum": self.world_state["momentum"],
                "pressure_tracks": self.world_state.get("pressure_tracks", {}),
                "pressure_levels": self.world_state.get("pressure_tracks", {}),
                "focus_threads": self.world_state.get("focus_threads", []),
                "last_tick_summary": summary,
            },
            "metrics": {
                "intents_created": len(intents),
                "accepted_events": resolution["accepted_count"],
                "deferred_intents": resolution["deferred_count"],
                "rejected_intents": resolution["rejected_count"],
                "active_events_count": len(self.active_events),
                "queued_events_count": len(self.queued_events),
                "completed_events_count": len(self.completed_events),
            },
            "active_events": [event.to_state_dict() for event in self.active_events.values()],
            "queued_events": [event.to_state_dict() for event in self.queued_events.values()],
            "recent_completed_events": [event.to_state_dict() for event in self.completed_events[-12:]],
        }
        self.last_snapshot = snapshot
        return snapshot

    def _build_tick_summary(
        self,
        tick: int,
        scene_title: str,
        intent_count: int,
        resolution: Dict[str, Any],
        completed_this_tick: List[WorldEvent],
    ) -> str:
        completed_titles = "、".join(event.title for event in completed_this_tick[:3])
        active_titles = "、".join(event.title for event in list(self.active_events.values())[:3])
        total_intents = max(
            intent_count,
            resolution["accepted_count"] + resolution["deferred_count"] + resolution["rejected_count"],
        )

        summary = (
            f"第 {tick} 轮「{scene_title}」结束：生成 {total_intents} 个角色意图，"
            f"其中 {resolution['accepted_count']} 个被整理为事件，当前有 {len(self.active_events)} 个活跃事件、"
            f"{len(self.queued_events)} 个排队事件，世界紧张度 {self.world_state['tension']:.2f}，"
            f"稳定度 {self.world_state['stability']:.2f}。"
        )
        if completed_titles:
            summary += f" 本轮完成的关键事件包括：{completed_titles}。"
        if active_titles:
            summary += f" 仍在推进的主线有：{active_titles}。"
        return summary

    def _write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self._append_jsonl(self.snapshots_log, snapshot)
        with open(self.world_state_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    def _write_meta_event(self, payload: Dict[str, Any]) -> None:
        self._append_jsonl(self.actions_log, payload)

    def _write_action(self, payload: Dict[str, Any]) -> None:
        self._lifecycle_records += 1
        self._append_jsonl(self.actions_log, payload)

    def _append_jsonl(self, path: str, payload: Dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _trace_slug(self, value: Any) -> str:
        text = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value or "").strip())
        return text.strip("-")[:80] or "trace"

    def _write_llm_trace(
        self,
        *,
        stage: str,
        tick: int,
        provider_role: str,
        llm: Optional[LLMClient],
        status: str,
        context: str,
        messages: List[Dict[str, Any]],
        raw_response: str = "",
        json_candidate: str = "",
        repaired_response: str = "",
        repaired_candidate: str = "",
        parsed_json: Any = None,
        outcome: Any = None,
        reason_code: str = "",
        error: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.debug_capture_llm_io:
            return

        self._llm_trace_counter += 1
        trace_id = f"{self._llm_trace_counter:05d}"
        payload = {
            "trace_id": trace_id,
            "run_id": self.llm_trace_run_id,
            "simulation_id": self.config.get("simulation_id"),
            "timestamp": now_iso(),
            "stage": stage,
            "tick": tick,
            "provider_role": provider_role,
            "status": status,
            "reason_code": reason_code,
            "context": context,
            "llm": {
                "selector": getattr(llm, "selector", None) if llm else None,
                "provider_id": getattr(llm, "provider_id", None) if llm else None,
                "profile_id": getattr(llm, "profile_id", None) if llm else None,
                "model": getattr(llm, "model", None) if llm else None,
                "speed_mode": getattr(llm, "speed_mode", None) if llm else None,
                "reasoning_effort": getattr(llm, "reasoning_effort", None) if llm else None,
                "verbosity": getattr(llm, "verbosity", None) if llm else None,
                "service_tier": getattr(llm, "service_tier", None) if llm else None,
            },
            "request": {
                "messages": messages,
            },
            "response": {
                "raw_response": raw_response,
                "json_candidate": json_candidate,
                "repaired_response": repaired_response,
                "repaired_candidate": repaired_candidate,
                "parsed_json": parsed_json,
            },
            "outcome": outcome,
            "error": error,
            "extra": extra or {},
        }

        filename = (
            f"{trace_id}-tick{tick:03d}-{self._trace_slug(stage)}-"
            f"{self._trace_slug(provider_role)}-{self._trace_slug(status)}.json"
        )
        path = os.path.join(self.llm_trace_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        with open(self.llm_trace_index_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "trace_id": trace_id,
                        "timestamp": payload["timestamp"],
                        "tick": tick,
                        "stage": stage,
                        "provider_role": provider_role,
                        "status": status,
                        "reason_code": reason_code,
                        "path": path,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def _next_intent_id(self, tick: int) -> str:
        self._intent_counter += 1
        return f"intent_{tick:03d}_{self._intent_counter:04d}"

    def _next_event_id(self, tick: int) -> str:
        self._event_counter += 1
        return f"event_{tick:03d}_{self._event_counter:04d}"

    def _normalize_state_impacts(self, payload: Any) -> Dict[str, float]:
        allowed = {"conflict", "scarcity", "legitimacy", "momentum", "stability", "tension"}
        if not isinstance(payload, dict):
            return {}
        return {
            key: round(clamp(safe_float(value), -0.25, 0.25), 3)
            for key, value in payload.items()
            if key in allowed
        }

    def _merge_state_impacts(
        self,
        intents: List[ActorIntent],
        override_payload: Any = None,
    ) -> Dict[str, float]:
        if isinstance(override_payload, dict) and override_payload:
            return self._normalize_state_impacts(override_payload)

        merged: Dict[str, float] = {}
        for intent in intents:
            for key, value in intent.state_impacts.items():
                merged[key] = merged.get(key, 0.0) + safe_float(value)
        return {
            key: round(clamp(value, -0.25, 0.25), 3)
            for key, value in merged.items()
        }

    def _write_resolution_log(
        self,
        intent: ActorIntent,
        status: str,
        event: Optional[WorldEvent] = None,
        reason: str = "",
    ) -> None:
        payload = {
            "event_type": "intent_resolved",
            "round": intent.tick,
            "tick": intent.tick,
            "timestamp": now_iso(),
            "platform": "world",
            "agent_id": intent.agent_id,
            "agent_name": intent.agent_name,
            "event_id": event.event_id if event else None,
            "title": event.title if event else intent.objective,
            "summary": event.summary if event else intent.summary,
            "participants": event.participants if event else intent.participants,
            "priority": event.priority if event else intent.priority,
            "duration_ticks": event.duration_ticks if event else intent.desired_duration,
            "remaining_ticks": event.duration_ticks if event else intent.desired_duration,
            "resolves_at_tick": event.resolves_at_tick if event else intent.tick + intent.desired_duration - 1,
            "location": event.location if event else intent.location,
            "dependencies": event.dependencies if event else intent.dependencies,
            "status": status,
            "state_impacts": event.state_impacts if event else intent.state_impacts,
            "action_type": "INTENT_RESOLVED",
            "action_args": {
                **intent.to_dict(),
                "resolution_status": status,
                "reason": reason,
                "event": event.to_state_dict() if event else None,
            },
            "result": event.summary if event else reason or intent.summary,
            "success": status != "rejected",
        }
        self._write_action(payload)

    def _world_state_brief(self) -> Dict[str, Any]:
        return {
            "tension": self.world_state.get("tension"),
            "stability": self.world_state.get("stability"),
            "momentum": self.world_state.get("momentum"),
            "pressure_tracks": self.world_state.get("pressure_tracks", {}),
            "last_tick_summary": self.world_state.get("last_tick_summary", ""),
        }

    def _event_brief(self, event: WorldEvent) -> Dict[str, Any]:
        return {
            "event_id": event.event_id,
            "title": event.title,
            "summary": event.summary,
            "status": event.status,
            "priority": event.priority,
            "participants": event.participants[:4],
            "dependencies": event.dependencies,
            "resolves_at_tick": event.resolves_at_tick,
            "location": event.location,
        }


def _load_jsonl_rows(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _counter_suffix(identifier: Any) -> int:
    match = re.search(r"_(\d+)$", str(identifier or "").strip())
    return int(match.group(1)) if match else 0


def _normalize_event_state_list(items: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return normalized
    for item in items:
        if not isinstance(item, dict):
            continue
        event = WorldEvent.from_state_dict(item)
        if event.event_id:
            normalized.append(event.to_state_dict())
    return normalized


def _event_actor_ids(event_payload: Dict[str, Any]) -> List[int]:
    raw_ids = [
        event_payload.get("primary_agent_id"),
        *list(event_payload.get("participant_ids") or []),
    ]
    actor_ids: List[int] = []
    for raw_value in raw_ids:
        actor_id = safe_int(raw_value, default=0, lower=0)
        if actor_id > 0 and actor_id not in actor_ids:
            actor_ids.append(actor_id)
    return actor_ids


def restore_world_checkpoint_from_logs(
    config_path: str,
    tick: int,
    output_path: str = "",
    in_place: bool = True,
) -> Dict[str, Any]:
    config_path = os.path.abspath(config_path)
    target_tick = safe_int(tick, default=0, lower=1)
    if target_tick <= 0:
        raise ValueError("tick must be >= 1")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    paths = world_run_paths_for_config(config_path)
    snapshots_path = os.path.join(paths.world_dir, "state_snapshots.jsonl")
    actions_path = os.path.join(paths.world_dir, "actions.jsonl")
    checkpoint_path = os.path.join(paths.world_dir, "checkpoint.json")

    snapshots = _load_jsonl_rows(snapshots_path)
    target_snapshot = next(
        (
            snapshot
            for snapshot in reversed(snapshots)
            if safe_int(snapshot.get("tick", snapshot.get("round")), default=0, lower=0) == target_tick
        ),
        None,
    )
    if not target_snapshot:
        raise ValueError(f"snapshot for tick {target_tick} not found: {snapshots_path}")

    actions = _load_jsonl_rows(actions_path)
    existing_checkpoint: Dict[str, Any] = {}
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                loaded_checkpoint = json.load(f)
            if isinstance(loaded_checkpoint, dict):
                existing_checkpoint = loaded_checkpoint
        except Exception:
            existing_checkpoint = {}

    actor_last_selected_tick: Dict[int, int] = {}
    actor_last_event_tick: Dict[int, int] = {}
    actor_selection_counts: Dict[int, int] = {}
    actor_event_counts: Dict[int, int] = {}
    counted_event_ids: set[str] = set()
    completed_event_map: Dict[str, Dict[str, Any]] = {}
    completed_event_order: List[str] = []
    intent_counter = 0
    event_counter = 0
    lifecycle_records = 0
    run_total_rounds = safe_int(existing_checkpoint.get("run_total_rounds"), default=0, lower=0)

    for row in actions:
        event_type = str(row.get("event_type") or "").strip()
        row_tick_raw = row.get("tick", row.get("round"))
        row_tick = safe_int(row_tick_raw, default=-1)
        if row_tick > target_tick:
            continue

        if event_type == "simulation_start" and run_total_rounds <= 0:
            run_total_rounds = safe_int(row.get("total_rounds"), default=0, lower=0)

        if row.get("action_type"):
            lifecycle_records += 1

        action_args = row.get("action_args") if isinstance(row.get("action_args"), dict) else {}
        if event_type == "intent_created":
            agent_id = safe_int(action_args.get("agent_id", row.get("agent_id", 0)), default=0, lower=0)
            if agent_id > 0:
                actor_last_selected_tick[agent_id] = row_tick
                actor_selection_counts[agent_id] = actor_selection_counts.get(agent_id, 0) + 1
            intent_counter = max(intent_counter, _counter_suffix(action_args.get("intent_id")))
        elif event_type == "intent_resolved":
            intent_counter = max(intent_counter, _counter_suffix(action_args.get("intent_id")))
            embedded_event = action_args.get("event") if isinstance(action_args.get("event"), dict) else {}
            if embedded_event:
                event_counter = max(event_counter, _counter_suffix(embedded_event.get("event_id")))
        elif event_type in {"event_started", "event_queued", "event_completed"}:
            event_payload = dict(action_args) if action_args else {}
            event_id = normalize_optional_text(event_payload.get("event_id"))
            if not event_id:
                continue
            event_counter = max(event_counter, _counter_suffix(event_id))
            actor_ids = _event_actor_ids(event_payload)
            for actor_id in actor_ids:
                actor_last_event_tick[actor_id] = row_tick
            if event_id not in counted_event_ids and event_type in {"event_started", "event_queued"}:
                counted_event_ids.add(event_id)
                for actor_id in actor_ids:
                    actor_event_counts[actor_id] = actor_event_counts.get(actor_id, 0) + 1
            if event_type == "event_completed":
                normalized_payload = dict(event_payload)
                normalized_payload["status"] = "completed"
                event = WorldEvent.from_state_dict(normalized_payload)
                if event.event_id:
                    if event.event_id not in completed_event_map:
                        completed_event_order.append(event.event_id)
                    completed_event_map[event.event_id] = event.to_state_dict()

    normalized_active_events = _normalize_event_state_list(target_snapshot.get("active_events"))
    normalized_queued_events = _normalize_event_state_list(target_snapshot.get("queued_events"))
    normalized_completed_events = [
        completed_event_map[event_id]
        for event_id in completed_event_order
        if event_id in completed_event_map
    ]

    time_config = config.get("time_config", {})
    minutes_per_round = safe_int(time_config.get("minutes_per_round", 60), default=60, lower=1)
    if run_total_rounds <= 0:
        run_total_rounds = safe_int(
            time_config.get("total_ticks", time_config.get("total_rounds", 0)),
            default=0,
            lower=0,
        )

    base_world_state = build_initial_world_state(
        plot_threads=config.get("plot_threads", []),
        pressure_tracks=config.get("pressure_tracks", []),
        initial_world_state=config.get("initial_world_state", {}),
    )
    snapshot_world_state = target_snapshot.get("world_state") or {}
    if isinstance(snapshot_world_state, dict):
        base_world_state.update(snapshot_world_state)
    base_world_state["active_event_ids"] = [item["event_id"] for item in normalized_active_events]
    base_world_state["queued_event_ids"] = [item["event_id"] for item in normalized_queued_events]
    base_world_state["completed_event_ids"] = [
        item["event_id"] for item in normalized_completed_events[-20:]
    ]

    restored_snapshot = dict(target_snapshot)
    restored_snapshot["active_events"] = normalized_active_events
    restored_snapshot["queued_events"] = normalized_queued_events
    restored_snapshot["recent_completed_events"] = normalized_completed_events[-12:]
    restored_snapshot["world_state"] = dict(target_snapshot.get("world_state") or {})
    restored_snapshot["phase"] = restored_snapshot.get("phase") or "tick_complete"
    restored_snapshot["simulated_hours"] = round(target_tick * minutes_per_round / 60, 2)

    checkpoint_payload: Dict[str, Any] = {
        "schema_version": WorldSimulationRuntime.CHECKPOINT_SCHEMA_VERSION,
        "saved_at": now_iso(),
        "status": "restored",
        "simulation_id": config.get("simulation_id"),
        "config_path": config_path,
        "last_completed_tick": target_tick,
        "run_total_rounds": run_total_rounds,
        "minutes_per_round": minutes_per_round,
        "active_events": normalized_active_events,
        "queued_events": normalized_queued_events,
        "completed_events": normalized_completed_events,
        "world_state": base_world_state,
        "last_snapshot": restored_snapshot,
        "actor_last_selected_tick": actor_last_selected_tick,
        "actor_last_event_tick": actor_last_event_tick,
        "actor_selection_counts": actor_selection_counts,
        "actor_event_counts": actor_event_counts,
        "counted_event_ids": sorted(counted_event_ids),
        "event_counter": event_counter,
        "intent_counter": intent_counter,
        "lifecycle_records": lifecycle_records,
        "restore_meta": {
            "restored_from_logs": True,
            "restored_to_tick": target_tick,
            "actions_path": actions_path,
            "snapshots_path": snapshots_path,
        },
    }

    destination_path = os.path.abspath(output_path) if output_path else checkpoint_path
    if output_path or in_place:
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        with open(destination_path, "w", encoding="utf-8") as f:
            json.dump(checkpoint_payload, f, ensure_ascii=False, indent=2)

    return {
        "simulation_id": config.get("simulation_id"),
        "config_path": config_path,
        "target_tick": target_tick,
        "checkpoint_path": destination_path,
        "written": bool(output_path or in_place),
        "active_events_count": len(normalized_active_events),
        "queued_events_count": len(normalized_queued_events),
        "completed_events_count": len(normalized_completed_events),
        "intent_counter": intent_counter,
        "event_counter": event_counter,
        "lifecycle_records": lifecycle_records,
        "status": checkpoint_payload["status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run world-mode simulation")
    parser.add_argument("--config", required=True, help="Path to simulation_config.json")
    parser.add_argument("--max-rounds", type=int, default=None, help="Optional round cap")
    parser.add_argument(
        "--resume-from-checkpoint",
        action="store_true",
        help="Resume world simulation from the last committed checkpoint",
    )
    args = parser.parse_args()

    runtime: Optional[WorldSimulationRuntime] = None
    try:
        with WorldRunLease(config_path=args.config):
            runtime = WorldSimulationRuntime(
                config_path=args.config,
                max_rounds=args.max_rounds,
                resume_from_checkpoint=args.resume_from_checkpoint,
            )
            asyncio.run(runtime.run())
    except Exception as exc:
        if runtime is not None:
            runtime._write_meta_event(
                {
                    "event_type": "simulation_failed",
                    "timestamp": now_iso(),
                    "simulation_mode": "world",
                    "phase": "failed",
                    "summary": f"World simulation failed: {exc}",
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                }
            )
        raise


if __name__ == "__main__":
    main()
