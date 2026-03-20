#!/usr/bin/env python3
"""
Run a staged world-model evaluation campaign:
1. Availability + structure probes
2. Actor smoke
3. Resolver smoke
4. Pair smoke
5. Progression mini
6. Stability repeats
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
EVAL_SCRIPT = BACKEND_DIR / "scripts" / "eval_world_models.py"
DEFAULT_OUTPUT_ROOT = BACKEND_DIR / "uploads" / "evals"
DEFAULT_CAMPAIGN_CONFIG = BACKEND_DIR / "evals" / "world_model_eval_campaign.json"
DEFAULT_PROXY_VARS = (
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "SOCKS_PROXY",
    "socks_proxy",
)

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Config
from app.utils.llm_client import LLMClient


PLACEHOLDER_TEXTS = {
    "",
    "none",
    "null",
    "unknown",
    "not specified",
    "未指定",
    "未知",
    "无",
    "n/a",
}

GENERIC_MARKERS = {
    "",
    "summary",
    "plan",
    "objective",
    "intent",
    "action",
    "行动建议",
    "行动方案",
    "本 tick 主行动",
    "本 tick 行动建议",
    "一、主行动",
}

META_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(?:[（(][^）)]{1,16}[）)]\s*)?(?:在\s*)?本\s*(?:tick|轮|回合).{0,12}(?:应对建议|行动建议|行动方案|方案|计划|意图|行动|总结)$",
        r"^(?:[（(][^）)]{1,16}[）)]\s*)?(?:应对建议|行动建议|行动方案|行动提案|具体行动|核心行动|核心判断|总结|摘要)$",
        r"^(?:summary|plan|objective|intent|action)$",
    )
]

WORLD_STATE_KEYS = {"conflict", "scarcity", "legitimacy", "momentum", "stability"}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return "" if text.lower() in PLACEHOLDER_TEXTS else text


def clean_title(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    text = re.sub(r"^[#>*\-\s]+", "", text).strip()
    text = re.sub(r"^[（(][^）)]{1,16}[）)]\s*", "", text)
    text = re.sub(r"^\d+\s*[.)、：:]\s*", "", text)
    text = text.strip(" -*_`\"'“”[]()【】（）")
    return text


def is_low_signal_title(value: Any) -> bool:
    text = clean_title(value)
    if not text:
        return True
    if text.lower() in GENERIC_MARKERS:
        return True
    if any(pattern.match(text) for pattern in META_PATTERNS):
        return True
    if len(text) <= 2:
        return True
    if len(text) > 140:
        return True
    if re.fullmatch(r"[A-Za-z_\-\s]{1,20}", text) and len(text) <= 20:
        return True
    return False


def score_lower_better(value: Optional[float], excellent: float, acceptable: float) -> float:
    if value is None:
        return 0.0
    if value <= excellent:
        return 100.0
    if value >= acceptable:
        return 0.0
    return max(0.0, min(100.0, (acceptable - value) / (acceptable - excellent) * 100.0))


def score_higher_better(value: Optional[float], acceptable: float, excellent: float) -> float:
    if value is None:
        return 0.0
    if value >= excellent:
        return 100.0
    if value <= acceptable:
        return 0.0
    return max(0.0, min(100.0, (value - acceptable) / (excellent - acceptable) * 100.0))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clear_config_caches(registry_path: Path) -> None:
    for key in DEFAULT_PROXY_VARS:
        os.environ.pop(key, None)
    os.environ["LLM_REGISTRY_PATH"] = str(registry_path)
    Config.LLM_REGISTRY_PATH = str(registry_path)
    Config._llm_registry_cache = None
    Config._llm_registry_mtime = None


def candidate_selector(candidate: Dict[str, Any]) -> str:
    return f"campaign_{candidate['id']}"


def clone_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def build_temp_registry(campaign_dir: Path, candidates: Sequence[Dict[str, Any]]) -> Path:
    base_registry = load_json(REPO_ROOT / "llm_registry.json")
    registry = clone_json(base_registry)
    profiles = registry.setdefault("profiles", {})
    for candidate in candidates:
        profile: Dict[str, Any] = {
            "openclaw_model": candidate["openclaw_model"],
        }
        for key in ("speed_mode", "reasoning_effort", "verbosity", "service_tier"):
            if candidate.get(key):
                profile[key] = candidate[key]
        profiles[candidate_selector(candidate)] = profile
    registry_path = campaign_dir / "temp_llm_registry.json"
    write_json(registry_path, registry)
    return registry_path


def world_state_brief(base_config: Dict[str, Any]) -> Dict[str, Any]:
    pressure_tracks = {}
    for track in base_config.get("pressure_tracks", []):
        name = normalize_text(track.get("name"))
        if not name:
            continue
        pressure_tracks[name] = round(safe_float(track.get("starting_level", 0.35), 0.35), 3)

    conflict = pressure_tracks.get("conflict", 0.35)
    scarcity = pressure_tracks.get("scarcity", 0.30)
    legitimacy = pressure_tracks.get("legitimacy", 0.45)
    momentum = round(max(0.05, min(0.35 + (conflict * 0.10), 0.95)), 3)
    tension = round(max(0.05, min((conflict * 0.60) + (scarcity * 0.25) + (momentum * 0.15), 0.95)), 3)
    stability = round(max(0.05, min((legitimacy * 0.50) + ((1 - conflict) * 0.35) + ((1 - scarcity) * 0.15), 0.95)), 3)
    return {
        "tension": tension,
        "stability": stability,
        "momentum": momentum,
        "pressure_tracks": pressure_tracks,
        "focus_threads": [item.get("title") for item in base_config.get("plot_threads", [])[:6] if item.get("title")],
        "last_tick_summary": "",
    }


def clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def clip_list(values: Any, item_limit: int, list_limit: int) -> List[str]:
    result: List[str] = []
    if not isinstance(values, list):
        values = [values] if values else []
    for item in values[:list_limit]:
        text = normalize_text(item)
        if text:
            result.append(clip_text(text, item_limit))
    return result


def find_actor_sample(base_config: Dict[str, Any], preferred_names: Sequence[str]) -> Dict[str, Any]:
    agents = base_config.get("agent_configs", [])
    normalized = {normalize_text(name): True for name in preferred_names}
    for agent in agents:
        if normalize_text(agent.get("entity_name")) in normalized:
            return agent
    return agents[0]


def build_actor_probe_payload(base_config: Dict[str, Any]) -> Dict[str, Any]:
    actor = find_actor_sample(base_config, ["世界政府", "四皇", "海军秘密部队"])
    return {
        "tick": 1,
        "scene_title": (base_config.get("plot_threads") or [{}])[0].get("title", "世界政府压制广播余波"),
        "world_state": world_state_brief(base_config),
        "world_rules": clip_list(base_config.get("world_rules", []), 90, 4),
        "plot_threads": (base_config.get("plot_threads") or [])[:4],
        "active_events": [],
        "queued_events": [],
        "recent_completed_events": [],
        "actor_profile": {
            "agent_id": actor.get("agent_id", 0),
            "entity_name": actor.get("entity_name", "Unknown Actor"),
            "entity_type": actor.get("entity_type", "Actor"),
            "public_role": clip_text(actor.get("public_role", ""), 120),
            "driving_goals": clip_list(actor.get("driving_goals", []), 80, 3),
            "resources": clip_list(actor.get("resources", []), 80, 3),
            "constraints": clip_list(actor.get("constraints", []), 80, 3),
            "connected_entities": clip_list(actor.get("connected_entities", []), 40, 5),
            "story_hooks": clip_list(actor.get("story_hooks", []), 80, 3),
            "home_location": clip_text(actor.get("home_location", ""), 60),
            "summary": clip_text(actor.get("summary", ""), 180),
        },
    }


def build_resolver_probe_payload(base_config: Dict[str, Any]) -> Dict[str, Any]:
    scene_title = (base_config.get("plot_threads") or [{}])[0].get("title", "世界政府压制广播余波")
    world_state = world_state_brief(base_config)
    intents = [
        {
            "intent_id": "intent_probe_001",
            "tick": 1,
            "agent_id": 23,
            "agent_name": "世界政府",
            "objective": "静默整肃 + 定点安抚",
            "summary": "通过统一解释令和节点高压稳住加盟国与海军执行面。",
            "location": "鱼人岛",
            "target": "秩序",
            "participants": ["世界政府", "秩序"],
            "priority": 4,
            "urgency": 4,
            "duration_ticks": 2,
            "dependencies": [],
            "state_impacts": {"conflict": 0.08, "scarcity": 0.05, "legitimacy": -0.05, "stability": 0.06, "momentum": 0.06},
        },
        {
            "intent_id": "intent_probe_002",
            "tick": 1,
            "agent_id": 34,
            "agent_name": "四皇",
            "objective": "抢占失控但未定型的节点",
            "summary": "趁世界政府压制广播余波时抢占外围节点与叙事空间。",
            "location": scene_title,
            "target": "路飞",
            "participants": ["四皇", "路飞"],
            "priority": 4,
            "urgency": 4,
            "duration_ticks": 2,
            "dependencies": [],
            "state_impacts": {"conflict": 0.08, "scarcity": 0.05, "legitimacy": -0.05, "stability": 0.06, "momentum": 0.06},
        },
        {
            "intent_id": "intent_probe_003",
            "tick": 1,
            "agent_id": 10,
            "agent_name": "海军秘密部队",
            "objective": "避免海军内部进一步撕裂",
            "summary": "优先保护 SWORD 的机动价值，并建立地方情报缓冲。",
            "location": scene_title,
            "target": "克比",
            "participants": ["海军秘密部队", "克比"],
            "priority": 4,
            "urgency": 4,
            "duration_ticks": 2,
            "dependencies": [],
            "state_impacts": {"conflict": 0.08, "scarcity": 0.05, "legitimacy": -0.05, "stability": 0.06, "momentum": 0.06},
        },
    ]
    return {
        "tick": 1,
        "scene_title": scene_title,
        "cluster_context": {
            "label": "秩序裂缝 / 广播余波",
            "cluster_index": 1,
            "cluster_count": 1,
            "intent_count": len(intents),
        },
        "world_state": world_state,
        "active_events": [],
        "queued_events": [],
        "max_active_events": 4,
        "max_queued_events": 6,
        "intents": intents,
    }


def actor_probe_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是一个世界观自动推进系统中的角色意图生成器。"
                "基于当前世界状态和角色设定，输出一个 JSON 对象，字段包含："
                "objective, summary, location, target, desired_duration, priority, urgency, "
                "risk_level, dependencies, participants, tags, state_impacts, rationale。"
                "state_impacts 只允许使用 conflict, scarcity, legitimacy, momentum, stability 这几个键，"
                "值在 -0.25 到 0.25 之间。不要输出额外字段。"
                "只输出一个 JSON 对象，不要 markdown，不要标题，不要解释。"
                "如果字段未知，请返回空字符串、空数组或空对象。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def resolver_probe_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是世界模拟的并发事件裁决器。当前只处理同一 front 的一小簇角色意图，"
                "请把这些意图整理成可并行推进的世界事件。"
                "输出 JSON，字段包含 accepted_events, deferred_intents, rejected_intents。"
                "accepted_events 每项字段包含 title, summary, owner_intent_id, supporting_intent_ids, "
                "priority, duration_ticks, location, dependencies, participants, state_impacts, rationale。"
                "deferred_intents / rejected_intents 每项字段包含 intent_id, reason。"
                "如果存在优先级>=4的明确行动意图，除非它纯观察或完全无动作，否则至少接受一个事件。"
                "不要因为信息不完整而把整簇意图全部 reject。"
                "只输出一个 JSON 对象，不要 markdown，不要标题，不要解释。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def probe_health(candidate: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    selector = candidate_selector(candidate)
    started = time.time()
    try:
        client = LLMClient.from_selector(selector)
        client.health_check(timeout=timeout)
        return {
            "selector": selector,
            "ok": True,
            "latency_s": round(time.time() - started, 2),
            "error": None,
        }
    except Exception as exc:
        return {
            "selector": selector,
            "ok": False,
            "latency_s": round(time.time() - started, 2),
            "error": str(exc),
        }


def probe_actor(candidate: Dict[str, Any], payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    selector = candidate_selector(candidate)
    started = time.time()
    try:
        client = LLMClient.from_selector(selector)
        raw = client.chat(
            messages=actor_probe_messages(payload),
            temperature=0.65,
            max_tokens=320,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        candidate_json = LLMClient._extract_json_candidate(raw) or LLMClient._clean_json_text(raw)
        data = json.loads(candidate_json)
        title = data.get("objective")
        location = normalize_text(data.get("location"))
        participants = data.get("participants") if isinstance(data.get("participants"), list) else []
        impacts = data.get("state_impacts") if isinstance(data.get("state_impacts"), dict) else {}
        state_impacts_valid = all(
            str(key) in WORLD_STATE_KEYS and -0.25 <= safe_float(value) <= 0.25
            for key, value in impacts.items()
        )
        score = (
            35.0
            + (15.0 if not is_low_signal_title(title) else 0.0)
            + (10.0 if location else 0.0)
            + (10.0 if state_impacts_valid else 0.0)
            + (10.0 if len(normalize_text(data.get("summary"))) >= 12 else 0.0)
            + (10.0 if participants else 0.0)
            + score_lower_better(round(time.time() - started, 2), excellent=8, acceptable=45) * 0.10
        )
        return {
            "selector": selector,
            "ok": True,
            "latency_s": round(time.time() - started, 2),
            "score": round(score, 1),
            "json_ok": True,
            "low_signal_title": is_low_signal_title(title),
            "placeholder_location": not bool(location),
            "state_impacts_valid": state_impacts_valid,
            "participants_count": len(participants),
            "summary_length": len(normalize_text(data.get("summary"))),
            "sample_title": normalize_text(title),
            "sample_summary": normalize_text(data.get("summary"))[:160],
            "error": None,
        }
    except Exception as exc:
        return {
            "selector": selector,
            "ok": False,
            "latency_s": round(time.time() - started, 2),
            "score": 0.0,
            "json_ok": False,
            "error": str(exc),
        }


def probe_resolver(candidate: Dict[str, Any], payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    selector = candidate_selector(candidate)
    started = time.time()
    known_ids = {item["intent_id"] for item in payload["intents"]}
    try:
        client = LLMClient.from_selector(selector)
        data = client.chat_json(
            messages=resolver_probe_messages(payload),
            temperature=0.30,
            max_tokens=1400,
            timeout=timeout,
        )
        accepted = data.get("accepted_events") if isinstance(data.get("accepted_events"), list) else []
        deferred = data.get("deferred_intents") if isinstance(data.get("deferred_intents"), list) else []
        rejected = data.get("rejected_intents") if isinstance(data.get("rejected_intents"), list) else []
        valid_refs = 0
        complete_events = 0
        for item in accepted:
            if not isinstance(item, dict):
                continue
            owner = normalize_text(item.get("owner_intent_id"))
            supporting = [normalize_text(x) for x in item.get("supporting_intent_ids", []) if normalize_text(x)]
            if owner in known_ids and all(x in known_ids for x in supporting):
                valid_refs += 1
            if (
                normalize_text(item.get("title"))
                and normalize_text(item.get("summary"))
                and normalize_text(item.get("location"))
                and isinstance(item.get("participants"), list)
            ):
                complete_events += 1
        accepted_count = len(accepted)
        total_outcome_count = accepted_count + len(deferred) + len(rejected)
        score = (
            30.0
            + (20.0 if accepted_count > 0 else 0.0)
            + score_higher_better(valid_refs / max(accepted_count, 1), acceptable=0.4, excellent=1.0) * 0.15
            + score_higher_better(complete_events / max(accepted_count, 1), acceptable=0.4, excellent=1.0) * 0.15
            + (10.0 if accepted_count > 0 and len(rejected) < len(payload["intents"]) else 0.0)
            + score_lower_better(round(time.time() - started, 2), excellent=8, acceptable=60) * 0.10
        )
        return {
            "selector": selector,
            "ok": True,
            "latency_s": round(time.time() - started, 2),
            "score": round(score, 1),
            "json_ok": True,
            "accepted_events_count": accepted_count,
            "deferred_count": len(deferred),
            "rejected_count": len(rejected),
            "valid_reference_rate": round(valid_refs / max(accepted_count, 1), 3),
            "complete_event_rate": round(complete_events / max(accepted_count, 1), 3),
            "outcome_coverage_rate": round(total_outcome_count / max(len(payload["intents"]), 1), 3),
            "sample_event_title": normalize_text((accepted[0] or {}).get("title")) if accepted else "",
            "error": None,
        }
    except Exception as exc:
        return {
            "selector": selector,
            "ok": False,
            "latency_s": round(time.time() - started, 2),
            "score": 0.0,
            "json_ok": False,
            "error": str(exc),
        }


def run_candidate_probe_bundle(
    candidate: Dict[str, Any],
    actor_payload: Dict[str, Any],
    resolver_payload: Dict[str, Any],
    probe_cfg: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    try:
        health = probe_health(candidate, timeout=float(probe_cfg["healthcheck_timeout_seconds"]))
        availability_item = {
            "id": candidate["id"],
            "label": candidate["label"],
            "group": candidate["group"],
            **health,
        }
        if not health["ok"]:
            return availability_item, None, None

        actor_item: Optional[Dict[str, Any]] = None
        resolver_item: Optional[Dict[str, Any]] = None
        if "actor" in candidate.get("roles", []):
            actor_result = probe_actor(candidate, actor_payload, timeout=float(probe_cfg["actor_timeout_seconds"]))
            actor_item = {
                "id": candidate["id"],
                "label": candidate["label"],
                "group": candidate["group"],
                **actor_result,
            }
        if "resolver" in candidate.get("roles", []):
            resolver_result = probe_resolver(candidate, resolver_payload, timeout=float(probe_cfg["resolver_timeout_seconds"]))
            resolver_item = {
                "id": candidate["id"],
                "label": candidate["label"],
                "group": candidate["group"],
                **resolver_result,
            }
        return availability_item, actor_item, resolver_item
    except Exception as exc:
        return (
            {
                "id": candidate["id"],
                "label": candidate["label"],
                "group": candidate["group"],
                "selector": candidate_selector(candidate),
                "ok": False,
                "latency_s": None,
                "error": f"probe bundle failed: {exc}",
            },
            None,
            None,
        )


def sort_probe_results(results: Sequence[Dict[str, Any]], candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    order_map = {candidate["id"]: index for index, candidate in enumerate(candidates)}
    return sorted(results, key=lambda item: order_map.get(item.get("id"), 999999))


def write_probe_snapshots(
    probe_dir: Path,
    candidates: Sequence[Dict[str, Any]],
    availability_results: Sequence[Dict[str, Any]],
    actor_probe_results: Sequence[Dict[str, Any]],
    resolver_probe_results: Sequence[Dict[str, Any]],
) -> None:
    write_json(probe_dir / "availability.json", {"results": sort_probe_results(availability_results, candidates)})
    write_json(probe_dir / "actor_probe.json", {"results": sort_probe_results(actor_probe_results, candidates)})
    write_json(probe_dir / "resolver_probe.json", {"results": sort_probe_results(resolver_probe_results, candidates)})


def select_group_winners(
    results: Sequence[Dict[str, Any]],
    candidates: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    candidate_map = {item["id"]: item for item in candidates}
    winners: Dict[str, Dict[str, Any]] = {}
    for result in sorted(results, key=lambda item: item.get("score", 0.0), reverse=True):
        candidate = candidate_map[result["id"]]
        group = candidate["group"]
        if group not in winners and result.get("ok"):
            winners[group] = result
    return list(winners.values())


def select_top_results(
    results: Sequence[Dict[str, Any]],
    top_n: int,
    forced_ids: Sequence[str],
) -> List[Dict[str, Any]]:
    ordered = [item for item in sorted(results, key=lambda x: x.get("score", 0.0), reverse=True) if item_ok(item)]

    selected: List[Dict[str, Any]] = []
    seen = set()
    for candidate_id in forced_ids:
        item = next((row for row in ordered if row["id"] == candidate_id), None)
        if item and item["id"] not in seen:
            selected.append(item)
            seen.add(item["id"])
    for item in ordered:
        if len(selected) >= top_n:
            break
        if item["id"] in seen:
            continue
        selected.append(item)
        seen.add(item["id"])
    return selected


def item_ok(item: Dict[str, Any]) -> bool:
    return bool(item.get("ok")) and not item.get("error")


def build_suite(
    name: str,
    description: str,
    score_profile: str,
    shared_max_rounds: int,
    shared_repeat_count: int,
    runtime_overrides: Dict[str, Any],
    cases: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "score_profile": score_profile,
        "shared_max_rounds": shared_max_rounds,
        "shared_repeat_count": shared_repeat_count,
        "shared_rewrite_agent_selectors": True,
        "shared_runtime_overrides": runtime_overrides,
        "cases": list(cases),
    }


def build_stage_report(stage_name: str, suite: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> str:
    ordered = sorted(results, key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    lines = [
        f"# {stage_name}",
        "",
        suite.get("description", ""),
        "",
        f"- score_profile: `{suite.get('score_profile', 'latency_smoke')}`",
        f"- cases: `{len(ordered)}`",
        "",
        "## Ranking",
    ]
    for index, item in enumerate(ordered, start=1):
        lines.append(
            f"{index}. `{item['label']}` overall=`{item.get('scores', {}).get('overall')}` "
            f"first_event=`{item.get('timing', {}).get('tick_start_to_first_event_s')}` "
            f"started=`{item.get('events', {}).get('events_started')}` "
            f"completed=`{item.get('events', {}).get('events_completed')}`"
        )
    return "\n".join(lines) + "\n"


def run_single_case_stage_eval(
    stage_dir: Path,
    suite: Dict[str, Any],
    case: Dict[str, Any],
    base_config_path: Path,
    python_bin: str,
    registry_path: Path,
) -> Dict[str, Any]:
    env = os.environ.copy()
    env["LLM_REGISTRY_PATH"] = str(registry_path)
    for key in DEFAULT_PROXY_VARS:
        env.pop(key, None)

    case_root = ensure_dir(stage_dir / "raw" / case["case_id"])
    case_suite = dict(suite)
    case_suite["cases"] = [case]
    case_suite_path = case_root / "suite.json"
    write_json(case_suite_path, case_suite)

    command = [
        python_bin,
        str(EVAL_SCRIPT),
        "--base-config",
        str(base_config_path),
        "--suite-config",
        str(case_suite_path),
        "--output-root",
        str(case_root),
        "--python-bin",
        python_bin,
        "--cases",
        case["case_id"],
    ]
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"Stage failed: {stage_dir.name} / {case['case_id']}"
        )
    suite_output_dir = Path(completed.stdout.strip().splitlines()[-1])
    summary = load_json(suite_output_dir / "summary.json")
    results = summary.get("results", [])
    if not results:
        raise RuntimeError(f"No results emitted for {stage_dir.name} / {case['case_id']}")
    return {
        "case_id": case["case_id"],
        "output_dir": str(suite_output_dir),
        "result": results[0],
        "summary": summary,
    }


def run_eval_stage(
    stage_dir: Path,
    suite: Dict[str, Any],
    base_config_path: Path,
    python_bin: str,
    registry_path: Path,
    case_workers: int = 1,
) -> Tuple[Path, Dict[str, Any]]:
    ensure_dir(stage_dir)
    suite_path = stage_dir / "suite.json"
    write_json(suite_path, suite)
    cases = list(suite.get("cases", []))
    if case_workers <= 1 or len(cases) <= 1:
        env = os.environ.copy()
        env["LLM_REGISTRY_PATH"] = str(registry_path)
        for key in DEFAULT_PROXY_VARS:
            env.pop(key, None)
        command = [
            python_bin,
            str(EVAL_SCRIPT),
            "--base-config",
            str(base_config_path),
            "--suite-config",
            str(suite_path),
            "--output-root",
            str(stage_dir),
            "--python-bin",
            python_bin,
        ]
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"Stage failed: {stage_dir.name}")
        suite_output_dir = Path(completed.stdout.strip().splitlines()[-1])
        summary = load_json(suite_output_dir / "summary.json")
        return suite_output_dir, summary

    results_by_case: Dict[str, Dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, case_workers)) as executor:
        future_map = {
            executor.submit(
                run_single_case_stage_eval,
                stage_dir,
                suite,
                case,
                base_config_path,
                python_bin,
                registry_path,
            ): case
            for case in cases
        }
        completed_count = 0
        for future in concurrent.futures.as_completed(future_map):
            case = future_map[future]
            case_result = future.result()
            results_by_case[case["case_id"]] = case_result
            completed_count += 1
            result = case_result["result"]
            print(
                f"[stage:{stage_dir.name}] "
                f"{completed_count}/{len(cases)} "
                f"{case['case_id']} "
                f"overall={result.get('scores', {}).get('overall')} "
                f"started={result.get('events', {}).get('events_started')} "
                f"completed={result.get('events', {}).get('events_completed')}",
                flush=True,
            )

    ordered_results = [
        results_by_case[case["case_id"]]["result"]
        for case in cases
        if case["case_id"] in results_by_case
    ]
    summary = {
        "generated_at": datetime.now().isoformat(),
        "base_config": str(base_config_path),
        "suite_config": str(suite_path),
        "score_profile": suite.get("score_profile", "latency_smoke"),
        "results": ordered_results,
        "case_outputs": {
            case_id: payload["output_dir"]
            for case_id, payload in results_by_case.items()
        },
    }
    write_json(stage_dir / "summary.json", summary)
    write_json(
        stage_dir / "leaderboard.json",
        {
            "results": summarize_ranking(summary, top_n=max(len(ordered_results), 1)),
        },
    )
    (stage_dir / "report.md").write_text(build_stage_report(stage_dir.name, suite, ordered_results), encoding="utf-8")
    return stage_dir, summary


def stage_case(
    case_id: str,
    label: str,
    actor_selector: str,
    resolver_selector: str,
    notes: str = "",
    max_rounds: Optional[int] = None,
    repeat_count: Optional[int] = None,
) -> Dict[str, Any]:
    payload = {
        "case_id": case_id,
        "label": label,
        "actor_selector": actor_selector,
        "resolver_selector": resolver_selector,
        "notes": notes,
    }
    if max_rounds is not None:
        payload["max_rounds"] = max_rounds
    if repeat_count is not None:
        payload["repeat_count"] = repeat_count
    return payload


def summarize_ranking(summary: Dict[str, Any], top_n: int = 5) -> List[Dict[str, Any]]:
    results = summary.get("results", [])
    ordered = sorted(results, key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    compact: List[Dict[str, Any]] = []
    for item in ordered[:top_n]:
        compact.append(
            {
                "case_id": item["case_id"],
                "label": item["label"],
                "actor_selector": item["actor_selector"],
                "resolver_selector": item["resolver_selector"],
                "overall": item.get("scores", {}).get("overall"),
                "speed": item.get("scores", {}).get("speed"),
                "progression": item.get("scores", {}).get("progression"),
                "resilience": item.get("scores", {}).get("resilience"),
                "cleanliness": item.get("scores", {}).get("cleanliness"),
                "first_event_s": item.get("timing", {}).get("tick_start_to_first_event_s"),
                "salvage_tick_rate": item.get("diagnostics", {}).get("salvage_tick_rate"),
            }
        )
    return compact


def choose_pair_finalists(summary: Dict[str, Any], top_n: int) -> List[Dict[str, Any]]:
    ordered = sorted(summary.get("results", []), key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    return ordered[:top_n]


def choose_probe_baseline(
    ranked_items: Sequence[Dict[str, Any]],
    fallback_candidate: Dict[str, Any],
) -> Dict[str, Any]:
    viable = [item for item in ranked_items if item_ok(item)]
    if viable:
        return sorted(
            viable,
            key=lambda item: (
                -(item.get("score") or 0.0),
                item.get("latency_s") if item.get("latency_s") is not None else 999999,
                item.get("id") or "",
            ),
        )[0]
    return {
        "id": fallback_candidate["id"],
        "label": fallback_candidate["label"],
        "selector": candidate_selector(fallback_candidate),
        "score": None,
        "latency_s": None,
        "ok": False,
        "fallback": True,
    }


def recommendation_block(stage_name: str, item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not item:
        return {}
    return {
        "stage": stage_name,
        "case_id": item.get("case_id"),
        "label": item.get("label"),
        "actor_selector": item.get("actor_selector"),
        "resolver_selector": item.get("resolver_selector"),
        "scores": item.get("scores", {}),
        "timing": item.get("timing", {}),
        "events": item.get("events", {}),
        "quality": item.get("quality", {}),
        "diagnostics": item.get("diagnostics", {}),
    }


def build_report(campaign: Dict[str, Any]) -> str:
    probe_actor = campaign["probe"]["actor_group_winners"]
    probe_resolver = campaign["probe"]["resolver_group_winners"]
    recommendations = campaign["recommendations"]
    baselines = campaign.get("baselines", {})
    lines = [
        "# World Model Eval Campaign",
        "",
        campaign["description"],
        "",
        f"- Campaign dir: `{campaign['campaign_dir']}`",
        f"- Base config: `{campaign['base_config']}`",
        "",
        "## Probe Winners",
    ]
    for item in probe_actor[:6]:
        lines.append(
            f"- actor `{item['label']}` score=`{item['score']}` latency=`{item['latency_s']}`s title=`{item.get('sample_title', '')}`"
        )
    lines.append("")
    for item in probe_resolver[:6]:
        lines.append(
            f"- resolver `{item['label']}` score=`{item['score']}` latency=`{item['latency_s']}`s accepted=`{item.get('accepted_events_count')}`"
        )
    lines.extend(
        [
            "",
            "## Recommendations",
            f"- Default strategy: `{recommendations['default_strategy'].get('label', 'n/a')}`",
            f"- Fastest viable: `{recommendations['fastest_viable'].get('label', 'n/a')}`",
            f"- Most stable progression: `{recommendations['most_stable_progression'].get('label', 'n/a')}`",
            "",
            "## Smoke Baselines",
            f"- Actor smoke resolver baseline: `{baselines.get('actor_smoke_resolver', {}).get('label', 'n/a')}`",
            f"- Resolver smoke actor baseline: `{baselines.get('resolver_smoke_actor', {}).get('label', 'n/a')}`",
            "",
            "## Stage Outputs",
            f"- Actor smoke: `{campaign['stages']['actor_smoke']['output_dir']}`",
            f"- Resolver smoke: `{campaign['stages']['resolver_smoke']['output_dir']}`",
            f"- Pair smoke: `{campaign['stages']['pair_smoke']['output_dir']}`",
            f"- Progression: `{campaign['stages']['progression']['output_dir']}`",
            f"- Stability: `{campaign['stages']['stability']['output_dir']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a complete world-model eval campaign.")
    parser.add_argument("--base-config", required=True, help="Path to a world simulation_config.json")
    parser.add_argument("--campaign-config", default=str(DEFAULT_CAMPAIGN_CONFIG), help="Campaign config JSON")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Root dir for campaign outputs")
    parser.add_argument("--python-bin", default=sys.executable, help="Python interpreter used for stage runs")
    parser.add_argument("--resume-probe-dir", help="Reuse an existing probe dir instead of rerunning the probe stage")
    args = parser.parse_args()

    base_config_path = Path(args.base_config).resolve()
    campaign_config_path = Path(args.campaign_config).resolve()
    output_root = ensure_dir(Path(args.output_root).resolve())
    campaign_dir = ensure_dir(output_root / f"world-model-campaign-{now_stamp()}")

    campaign_config = load_json(campaign_config_path)
    candidates = campaign_config["candidates"]
    registry_path = build_temp_registry(campaign_dir, candidates)
    clear_config_caches(registry_path)

    actor_payload = build_actor_probe_payload(load_json(base_config_path))
    resolver_payload = build_resolver_probe_payload(load_json(base_config_path))
    probe_cfg = campaign_config["probe"]
    probe_max_workers = max(1, int(probe_cfg.get("max_workers", min(4, max(len(candidates), 1)))))
    smoke_case_workers = max(1, int(campaign_config.get("smoke_case_workers", 3)))
    progression_case_workers = max(1, int(campaign_config.get("progression_case_workers", 2)))
    stability_case_workers = max(1, int(campaign_config.get("stability_case_workers", 2)))
    probe_dir = ensure_dir(campaign_dir / "probe")

    availability_results: List[Dict[str, Any]] = []
    actor_probe_results: List[Dict[str, Any]] = []
    resolver_probe_results: List[Dict[str, Any]] = []

    if args.resume_probe_dir:
        source_probe_dir = Path(args.resume_probe_dir).resolve()
        availability_results = load_json(source_probe_dir / "availability.json").get("results", [])
        actor_probe_results = load_json(source_probe_dir / "actor_probe.json").get("results", [])
        resolver_probe_results = load_json(source_probe_dir / "resolver_probe.json").get("results", [])
        availability_results = sort_probe_results(availability_results, candidates)
        actor_probe_results = sort_probe_results(actor_probe_results, candidates)
        resolver_probe_results = sort_probe_results(resolver_probe_results, candidates)
        write_probe_snapshots(probe_dir, candidates, availability_results, actor_probe_results, resolver_probe_results)
        print(f"[probe] reused existing probe from {source_probe_dir}", flush=True)
    else:
        print(f"[probe] starting {len(candidates)} candidates with max_workers={probe_max_workers}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=probe_max_workers) as executor:
            future_map = {
                executor.submit(
                    run_candidate_probe_bundle,
                    candidate,
                    actor_payload,
                    resolver_payload,
                    probe_cfg,
                ): candidate
                for candidate in candidates
            }
            completed_count = 0
            for future in concurrent.futures.as_completed(future_map):
                candidate = future_map[future]
                try:
                    availability_item, actor_item, resolver_item = future.result()
                except Exception as exc:
                    availability_item = {
                        "id": candidate["id"],
                        "label": candidate["label"],
                        "group": candidate["group"],
                        "selector": candidate_selector(candidate),
                        "ok": False,
                        "latency_s": None,
                        "error": f"probe future failed: {exc}",
                    }
                    actor_item = None
                    resolver_item = None
                availability_results.append(availability_item)
                if actor_item:
                    actor_probe_results.append(actor_item)
                if resolver_item:
                    resolver_probe_results.append(resolver_item)
                completed_count += 1
                write_probe_snapshots(
                    probe_dir,
                    candidates,
                    availability_results,
                    actor_probe_results,
                    resolver_probe_results,
                )
                print(
                    "[probe] "
                    f"{completed_count}/{len(candidates)} "
                    f"{candidate['id']} "
                    f"health={'ok' if availability_item.get('ok') else 'fail'} "
                    f"actor={'ok' if actor_item and actor_item.get('ok') else ('skip' if actor_item is None else 'fail')} "
                    f"resolver={'ok' if resolver_item and resolver_item.get('ok') else ('skip' if resolver_item is None else 'fail')}",
                    flush=True,
                )

        availability_results = sort_probe_results(availability_results, candidates)
        actor_probe_results = sort_probe_results(actor_probe_results, candidates)
        resolver_probe_results = sort_probe_results(resolver_probe_results, candidates)
        write_probe_snapshots(probe_dir, candidates, availability_results, actor_probe_results, resolver_probe_results)

    actor_group_winners = select_group_winners(actor_probe_results, candidates)
    resolver_group_winners = select_group_winners(resolver_probe_results, candidates)
    selection_cfg = campaign_config["selection"]
    selected_actor_probes = select_top_results(
        actor_group_winners,
        top_n=int(selection_cfg["actor_probe_top_n"]),
        forced_ids=selection_cfg.get("force_actor_ids", []),
    )
    selected_resolver_probes = select_top_results(
        resolver_group_winners,
        top_n=int(selection_cfg["resolver_probe_top_n"]),
        forced_ids=selection_cfg.get("force_resolver_ids", []),
    )

    probe_summary = {
        "actor_group_winners": actor_group_winners,
        "resolver_group_winners": resolver_group_winners,
        "selected_actor_probes": selected_actor_probes,
        "selected_resolver_probes": selected_resolver_probes,
    }
    write_json(probe_dir / "summary.json", probe_summary)
    print(
        "[probe] selected "
        f"actors={len(selected_actor_probes)} "
        f"resolvers={len(selected_resolver_probes)}",
        flush=True,
    )

    if not selected_actor_probes:
        raise RuntimeError("No actor candidates survived the probe stage")
    if not selected_resolver_probes:
        raise RuntimeError("No resolver candidates survived the probe stage")

    fallback_actor_candidate = next(item for item in candidates if item["id"] == "litellm_gpt54_fast")
    fallback_resolver_candidate = next(item for item in candidates if item["id"] == "litellm_gpt54_balanced")
    baseline_actor_probe = choose_probe_baseline(actor_group_winners, fallback_actor_candidate)
    baseline_resolver_probe = choose_probe_baseline(resolver_group_winners, fallback_resolver_candidate)
    baseline_actor_selector = baseline_actor_probe["selector"]
    baseline_resolver_selector = baseline_resolver_probe["selector"]
    print(
        "[probe] baselines "
        f"actor={baseline_actor_selector} "
        f"resolver={baseline_resolver_selector}",
        flush=True,
    )

    actor_smoke_cases = [
        stage_case(
            case_id=f"actor-{item['id']}",
            label=f"Actor Smoke / {item['label']}",
            actor_selector=item["selector"],
            resolver_selector=baseline_resolver_selector,
            notes="Actor candidate with the strongest resolver probe winner as baseline.",
        )
        for item in selected_actor_probes
    ]
    actor_smoke_suite = build_suite(
        name="campaign-actor-smoke",
        description="Actor-side world smoke with a fixed resolver baseline.",
        score_profile="latency_smoke",
        shared_max_rounds=1,
        shared_repeat_count=1,
        runtime_overrides=campaign_config["smoke_runtime_overrides"],
        cases=actor_smoke_cases,
    )
    actor_stage_dir = ensure_dir(campaign_dir / "actor_smoke")
    print(f"[stage] actor_smoke -> {actor_stage_dir}", flush=True)
    actor_output_dir, actor_summary = run_eval_stage(
        actor_stage_dir,
        actor_smoke_suite,
        base_config_path,
        args.python_bin,
        registry_path,
        case_workers=smoke_case_workers,
    )

    resolver_smoke_cases = [
        stage_case(
            case_id=f"resolver-{item['id']}",
            label=f"Resolver Smoke / {item['label']}",
            actor_selector=baseline_actor_selector,
            resolver_selector=item["selector"],
            notes="Resolver candidate with the strongest actor probe winner as baseline.",
        )
        for item in selected_resolver_probes
    ]
    resolver_smoke_suite = build_suite(
        name="campaign-resolver-smoke",
        description="Resolver-side world smoke with a fixed actor baseline.",
        score_profile="latency_smoke",
        shared_max_rounds=1,
        shared_repeat_count=1,
        runtime_overrides=campaign_config["smoke_runtime_overrides"],
        cases=resolver_smoke_cases,
    )
    resolver_stage_dir = ensure_dir(campaign_dir / "resolver_smoke")
    print(f"[stage] resolver_smoke -> {resolver_stage_dir}", flush=True)
    resolver_output_dir, resolver_summary = run_eval_stage(
        resolver_stage_dir,
        resolver_smoke_suite,
        base_config_path,
        args.python_bin,
        registry_path,
        case_workers=smoke_case_workers,
    )

    actor_ranked = sorted(actor_summary["results"], key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    resolver_ranked = sorted(resolver_summary["results"], key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    top_actors = actor_ranked[: int(selection_cfg["pair_top_actor_n"])]
    top_resolvers = resolver_ranked[: int(selection_cfg["pair_top_resolver_n"])]

    pair_smoke_cases: List[Dict[str, Any]] = []
    for actor_item in top_actors:
        for resolver_item in top_resolvers:
            pair_smoke_cases.append(
                stage_case(
                    case_id=f"pair-{actor_item['case_id']}-{resolver_item['case_id']}",
                    label=f"Pair Smoke / {actor_item['label']} + {resolver_item['label']}",
                    actor_selector=actor_item["actor_selector"],
                    resolver_selector=resolver_item["resolver_selector"],
                    notes="Top actor candidate crossed with top resolver candidate.",
                )
            )
    if not pair_smoke_cases:
        raise RuntimeError("No pair candidates advanced to pair smoke")
    pair_smoke_suite = build_suite(
        name="campaign-pair-smoke",
        description="Crossed smoke eval between top actor and resolver candidates.",
        score_profile="latency_smoke",
        shared_max_rounds=1,
        shared_repeat_count=1,
        runtime_overrides=campaign_config["smoke_runtime_overrides"],
        cases=pair_smoke_cases,
    )
    pair_stage_dir = ensure_dir(campaign_dir / "pair_smoke")
    print(f"[stage] pair_smoke -> {pair_stage_dir}", flush=True)
    pair_output_dir, pair_summary = run_eval_stage(
        pair_stage_dir,
        pair_smoke_suite,
        base_config_path,
        args.python_bin,
        registry_path,
        case_workers=smoke_case_workers,
    )

    progression_finalists = choose_pair_finalists(pair_summary, top_n=int(selection_cfg["progression_top_n"]))
    progression_cases = [
        stage_case(
            case_id=f"progression-{item['case_id']}",
            label=f"Progression / {item['label']}",
            actor_selector=item["actor_selector"],
            resolver_selector=item["resolver_selector"],
            notes="Top pair advanced to 3-tick progression stage.",
        )
        for item in progression_finalists
    ]
    if not progression_cases:
        raise RuntimeError("No pair candidates advanced to progression stage")
    progression_suite = build_suite(
        name="campaign-progression",
        description="Three-tick progression eval for top pair candidates.",
        score_profile="progression_mini",
        shared_max_rounds=3,
        shared_repeat_count=1,
        runtime_overrides=campaign_config["progression_runtime_overrides"],
        cases=progression_cases,
    )
    progression_stage_dir = ensure_dir(campaign_dir / "progression")
    print(f"[stage] progression -> {progression_stage_dir}", flush=True)
    progression_output_dir, progression_summary = run_eval_stage(
        progression_stage_dir,
        progression_suite,
        base_config_path,
        args.python_bin,
        registry_path,
        case_workers=progression_case_workers,
    )

    stability_finalists = choose_pair_finalists(progression_summary, top_n=int(selection_cfg["stability_top_n"]))
    stability_cases = [
        stage_case(
            case_id=f"stability-{item['case_id']}",
            label=f"Stability / {item['label']}",
            actor_selector=item["actor_selector"],
            resolver_selector=item["resolver_selector"],
            notes="Top progression pair advanced to repeat-based stability stage.",
        )
        for item in stability_finalists
    ]
    if not stability_cases:
        raise RuntimeError("No pair candidates advanced to stability stage")
    stability_suite = build_suite(
        name="campaign-stability",
        description="Repeat progression eval for the top pair candidates.",
        score_profile="progression_mini",
        shared_max_rounds=3,
        shared_repeat_count=int(selection_cfg["stability_repeat_count"]),
        runtime_overrides=campaign_config["progression_runtime_overrides"],
        cases=stability_cases,
    )
    stability_stage_dir = ensure_dir(campaign_dir / "stability")
    print(f"[stage] stability -> {stability_stage_dir}", flush=True)
    stability_output_dir, stability_summary = run_eval_stage(
        stability_stage_dir,
        stability_suite,
        base_config_path,
        args.python_bin,
        registry_path,
        case_workers=stability_case_workers,
    )

    stability_results = sorted(stability_summary["results"], key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    default_strategy = stability_results[0] if stability_results else {}
    pair_results = pair_summary.get("results", [])
    fastest_viable = min(
        [
            item
            for item in pair_results
            if (item.get("events", {}).get("events_started") or 0) >= 1
        ],
        key=lambda item: item.get("timing", {}).get("tick_start_to_first_event_s") or 999999,
        default={},
    )
    most_stable_progression = min(
        progression_summary.get("results", []),
        key=lambda item: (
            -(item.get("scores", {}).get("overall", 0.0) or 0.0),
            item.get("diagnostics", {}).get("salvage_tick_rate") or 0.0,
            -(item.get("events", {}).get("events_completed") or 0.0),
        ),
        default={},
    )

    campaign_summary = {
        "name": campaign_config["name"],
        "description": campaign_config["description"],
        "campaign_dir": str(campaign_dir),
        "base_config": str(base_config_path),
        "registry_path": str(registry_path),
        "probe": probe_summary,
        "baselines": {
            "actor_smoke_resolver": baseline_resolver_probe,
            "resolver_smoke_actor": baseline_actor_probe,
        },
        "stages": {
            "actor_smoke": {
                "output_dir": str(actor_output_dir),
                "top_results": summarize_ranking(actor_summary),
                "summary": actor_summary,
            },
            "resolver_smoke": {
                "output_dir": str(resolver_output_dir),
                "top_results": summarize_ranking(resolver_summary),
                "summary": resolver_summary,
            },
            "pair_smoke": {
                "output_dir": str(pair_output_dir),
                "top_results": summarize_ranking(pair_summary),
                "summary": pair_summary,
            },
            "progression": {
                "output_dir": str(progression_output_dir),
                "top_results": summarize_ranking(progression_summary),
                "summary": progression_summary,
            },
            "stability": {
                "output_dir": str(stability_output_dir),
                "top_results": summarize_ranking(stability_summary),
                "summary": stability_summary,
            },
        },
        "recommendations": {
            "default_strategy": recommendation_block("stability", default_strategy),
            "fastest_viable": recommendation_block("pair_smoke", fastest_viable),
            "most_stable_progression": recommendation_block("progression", most_stable_progression),
        },
    }
    write_json(campaign_dir / "campaign_summary.json", campaign_summary)
    report = build_report(campaign_summary)
    (campaign_dir / "campaign_report.md").write_text(report, encoding="utf-8")
    print(str(campaign_dir))


if __name__ == "__main__":
    main()
