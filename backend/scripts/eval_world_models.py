#!/usr/bin/env python3
"""
Evaluate world-simulation model selectors with isolated runs, repeat support, and
tick-level diagnostics.

The harness clones a base world config into per-run directories, swaps actor and
resolver selectors, runs `run_world_simulation.py`, then scores the resulting
timeline on speed, progression, resilience, and output cleanliness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
RUN_WORLD_SCRIPT = Path(__file__).resolve().with_name("run_world_simulation.py")
DEFAULT_OUTPUT_ROOT = BACKEND_DIR / "uploads" / "evals"
DEFAULT_SUITE_CONFIG = BACKEND_DIR / "evals" / "world_model_eval_suite.json"
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

PLACEHOLDER_TEXTS = {
    "",
    "none",
    "null",
    "unknown",
    "not specified",
    "not provided",
    "n/a",
    "na",
    "未指定",
    "未知",
    "无",
    "暂无",
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
    "核心行动",
    "行动名称",
    "行动提案",
    "本 tick 行动方案",
    "本 tick 建议行动",
    "本 tick 具体行动",
}

META_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(?:[｜|／/]\s*)?(?:第\s*\d+\s*轮|tick\s*\d+)",
        r"^(?:本\s*(?:tick|轮|回合)\s*)?(?:行动|意图)(?:建议|提案|方案|目标|方向|倾向)?",
        r"^(?:核心动作|核心决策|核心判断|核心行动|建议判定|建议|摘要|总结|风格判断|实际行动意图|具体行动倾向|角色意图|本轮意图|本轮行动|本轮主行动|意图生成|行动名称|行动提案|行动代号|本\s*tick\s*具体行动|本\s*tick\s*建议行动|本\s*tick\s*战略主轴)",
        r"^(?:目标|方式|理由|背景|契合当前局势|说明)\s*[：:]",
    )
]

LOW_SIGNAL_TITLE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(?:[（(][^）)]{1,16}[）)]\s*)?(?:在\s*)?本\s*(?:tick|轮|回合).{0,12}(?:应对建议|行动建议|行动方案|方案|计划|意图|行动|总结)$",
        r"^(?:[（(][^）)]{1,16}[）)]\s*)?(?:应对建议|行动建议|行动方案|行动提案|具体行动|核心行动|核心判断|总结|摘要)$",
        r"^(?:summary|plan|objective|intent|action)$",
    )
]

CHINESE_TEXT_RE = re.compile(r"[\u4e00-\u9fff]")

TEXT_RECOVERY_SOURCES = {
    "llm_text_recovered",
    "llm_low_signal_fallback",
}

LOW_SIGNAL_SOURCES = {
    "llm_low_signal_fallback",
}

SALVAGE_SOURCES = {
    "heuristic_salvage",
    "resolver_salvage",
    "resolver_salvaged",
}

EVENT_ROW_PRIORITY = {
    "event_started": 4,
    "event_queued": 3,
    "event_completed": 2,
    "intent_resolved": 1,
}

AGG_SCORE_KEYS = ("speed", "progression", "resilience", "cleanliness", "overall")
AGG_TIMING_KEYS = (
    "simulation_to_tick_start_s",
    "tick_start_to_first_intent_s",
    "tick_start_to_resolver_clustered_s",
    "tick_start_to_first_event_s",
    "tick_start_to_tick_end_s",
    "simulation_total_s",
)
AGG_EVENT_KEYS = (
    "intents_created",
    "intents_accepted",
    "intents_rejected",
    "intents_deferred",
    "events_started",
    "events_queued",
    "events_completed",
    "resolver_clusters",
    "resolver_salvages",
    "provider_waits",
    "ticks_blocked",
    "ticks_finished",
)
AGG_CONCURRENCY_KEYS = (
    "avg_active_events",
    "peak_active_events",
    "avg_queued_events",
    "peak_queued_events",
    "active_slot_utilization",
    "queue_slot_utilization",
    "dead_tick_rate",
    "queue_promotion_rate",
)
AGG_QUALITY_KEYS = (
    "dirty_intent_title_rate",
    "dirty_event_title_rate",
    "placeholder_location_rate",
    "unique_event_title_ratio",
    "low_signal_source_rate",
    "recovered_intent_rate",
    "state_movement",
    "pressure_track_movement",
    "progress_signal",
    "impact_potential",
)
AGG_DIAGNOSTIC_KEYS = (
    "intents_per_minute",
    "accepted_event_ratio",
    "provider_wait_total_s",
    "provider_wait_max_s",
    "salvage_tick_rate",
    "salvage_event_source_rate",
    "realized_impact",
)


@dataclass
class EvalCase:
    case_id: str
    label: str
    actor_selector: str
    resolver_selector: str
    max_rounds: int
    repeat_count: int
    runtime_overrides: Dict[str, Any]
    notes: str = ""
    rewrite_agent_selectors: bool = True


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def clone_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "case"


def deep_update(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalize_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return "" if text.lower() in PLACEHOLDER_TEXTS else text


def format_number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        rounded = round(value, digits)
        if digits <= 0:
            return str(int(rounded))
        return f"{rounded:.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def clean_candidate(text: Any) -> str:
    cleaned = normalize_text(text)
    if not cleaned:
        return ""
    cleaned = re.sub(r"[*_`]+", "", cleaned)
    cleaned = cleaned.lstrip("｜|／/")
    cleaned = re.sub(r"^[#>*\-\s]+", "", cleaned).strip()
    cleaned = re.sub(r"^\d+\s*[.)、：:]\s*", "", cleaned)
    cleaned = re.sub(r"^[（(][^）)]{1,16}[）)]\s*", "", cleaned)
    cleaned = cleaned.strip(" -*_`\"'“”[]()【】（）")
    for _ in range(3):
        updated = cleaned
        for pattern in META_PATTERNS:
            updated = pattern.sub("", updated).strip(" -*_`\"'“”[]()【】（）")
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned.strip()


def title_variants(text: Any) -> List[str]:
    raw = normalize_text(text)
    if not raw:
        return []

    variants = [raw]
    cleaned_full = clean_candidate(raw)
    if cleaned_full and cleaned_full != raw:
        variants.append(cleaned_full)
    for delimiter in ("：", ":", "｜", "|", " - ", " — "):
        if delimiter in raw:
            tail_raw = normalize_text(raw.split(delimiter)[-1])
            if tail_raw:
                variants.append(tail_raw)
                tail_clean = clean_candidate(tail_raw)
                if tail_clean and tail_clean != tail_raw:
                    variants.append(tail_clean)

    seen = set()
    ordered: List[str] = []
    for variant in variants:
        normalized = variant.lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def is_low_signal_title(text: Any) -> bool:
    raw = normalize_text(text)
    variants = title_variants(text)
    if not raw or not variants:
        return True
    if any(pattern.match(raw) for pattern in META_PATTERNS):
        return True

    for cleaned in variants:
        if cleaned in GENERIC_OBJECTIVE_MARKERS:
            return True
        if len(cleaned) > 140:
            return True
        if len(cleaned) <= 6:
            if CHINESE_TEXT_RE.search(cleaned):
                if len(cleaned) <= 2:
                    return True
            else:
                return True
        if cleaned.startswith(("这样可以", "若卷入", "不直接暴露", "当前核心威胁")):
            return True
        if any(pattern.match(cleaned) for pattern in LOW_SIGNAL_TITLE_PATTERNS):
            return True

    return False


def compute_initial_world_state(config: Dict[str, Any]) -> Dict[str, float]:
    pressure_map: Dict[str, float] = {}
    for track in config.get("pressure_tracks", []):
        try:
            pressure_map[str(track.get("name", "pressure"))] = max(
                0.05,
                min(float(track.get("starting_level", 0.35)), 0.95),
            )
        except (TypeError, ValueError):
            pressure_map[str(track.get("name", "pressure"))] = 0.35

    conflict = pressure_map.get("conflict", 0.35)
    scarcity = pressure_map.get("scarcity", 0.30)
    legitimacy = pressure_map.get("legitimacy", 0.45)
    momentum = max(0.05, min(0.35 + (conflict * 0.10), 0.95))
    tension = max(0.05, min((conflict * 0.60) + (scarcity * 0.25) + (momentum * 0.15), 0.95))
    stability = max(0.05, min((legitimacy * 0.50) + ((1 - conflict) * 0.35) + ((1 - scarcity) * 0.15), 0.95))
    return {
        "tension": round(tension, 3),
        "stability": round(stability, 3),
        "momentum": round(momentum, 3),
    }


def compute_initial_pressure_tracks(config: Dict[str, Any]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for track in config.get("pressure_tracks", []):
        key = normalize_text(track.get("name"))
        if not key:
            continue
        result[key] = round(max(0.05, min(safe_float(track.get("starting_level", 0.35), 0.35), 0.95)), 3)
    return result


def resolve_suite_cases(
    path: Path,
    filters: Optional[set[str]] = None,
    repeat_override: Optional[int] = None,
) -> tuple[Dict[str, Any], List[EvalCase]]:
    suite = load_json(path)
    shared_runtime = suite.get("shared_runtime_overrides", {})
    shared_max_rounds = int(suite.get("shared_max_rounds", 1))
    shared_repeat_count = int(suite.get("shared_repeat_count", 1))
    rewrite_shared = bool(suite.get("shared_rewrite_agent_selectors", True))
    cases: List[EvalCase] = []

    for item in suite.get("cases", []):
        case_id = slugify(str(item.get("case_id") or item.get("label") or f"case-{len(cases) + 1}"))
        if filters and case_id not in filters:
            continue
        cases.append(
            EvalCase(
                case_id=case_id,
                label=str(item.get("label") or case_id),
                actor_selector=str(item.get("actor_selector") or "").strip(),
                resolver_selector=str(item.get("resolver_selector") or "").strip(),
                max_rounds=int(item.get("max_rounds", shared_max_rounds)),
                repeat_count=max(1, repeat_override or int(item.get("repeat_count", shared_repeat_count))),
                runtime_overrides=deep_update(clone_json(shared_runtime), clone_json(item.get("runtime_overrides", {}))),
                notes=str(item.get("notes") or "").strip(),
                rewrite_agent_selectors=bool(item.get("rewrite_agent_selectors", rewrite_shared)),
            )
        )

    return suite, cases


def build_case_config(base_config: Dict[str, Any], case: EvalCase, repeat_index: int) -> Dict[str, Any]:
    payload = clone_json(base_config)
    payload["simulation_id"] = (
        f"{base_config.get('simulation_id', 'sim')}_{case.case_id}_r{repeat_index:02d}_{uuid.uuid4().hex[:6]}"
    )
    runtime = payload.setdefault("runtime_config", {})
    runtime["default_actor_llm_selector"] = case.actor_selector
    runtime["resolver_llm_selector"] = case.resolver_selector
    deep_update(runtime, case.runtime_overrides)
    if case.rewrite_agent_selectors:
        for agent in payload.get("agent_configs", []):
            agent["llm_selector"] = case.actor_selector
    return payload


def run_case(
    python_bin: str,
    config_path: Path,
    max_rounds: int,
    log_path: Path,
) -> Dict[str, Any]:
    env = os.environ.copy()
    for key in DEFAULT_PROXY_VARS:
        env.pop(key, None)

    command = [
        python_bin,
        str(RUN_WORLD_SCRIPT),
        "--config",
        str(config_path),
        "--max-rounds",
        str(max_rounds),
    ]
    started_at = time.time()
    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    return {
        "exit_code": process.returncode,
        "wall_clock_seconds": round(time.time() - started_at, 2),
        "command": command,
    }


def first_ts(rows: Iterable[Dict[str, Any]], event_type: str) -> Optional[datetime]:
    for row in rows:
        if row.get("event_type") == event_type:
            return parse_ts(row.get("timestamp"))
    return None


def unique_ids(rows: Iterable[Dict[str, Any]], event_type: str) -> List[str]:
    seen: List[str] = []
    for row in rows:
        if row.get("event_type") != event_type:
            continue
        event_id = normalize_text(row.get("event_id"))
        if event_id and event_id not in seen:
            seen.append(event_id)
    return seen


def numeric_stats(values: Sequence[Optional[float]]) -> Dict[str, Optional[float]]:
    valid = [float(value) for value in values if value is not None]
    if not valid:
        return {
            "count": 0,
            "mean": None,
            "p50": None,
            "min": None,
            "max": None,
            "std": None,
        }
    return {
        "count": len(valid),
        "mean": round(statistics.mean(valid), 3),
        "p50": round(statistics.median(valid), 3),
        "min": round(min(valid), 3),
        "max": round(max(valid), 3),
        "std": round(statistics.pstdev(valid), 3) if len(valid) > 1 else 0.0,
    }


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


def delta_seconds(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    if not start or not end:
        return None
    return round((end - start).total_seconds(), 2)


def extract_action_args(row: Dict[str, Any]) -> Dict[str, Any]:
    action_args = row.get("action_args")
    return action_args if isinstance(action_args, dict) else {}


def extract_event_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    action_args = extract_action_args(row)
    embedded_event = action_args.get("event")
    if isinstance(embedded_event, dict):
        return embedded_event
    return {
        "event_id": row.get("event_id"),
        "title": row.get("title"),
        "summary": row.get("summary") or row.get("result"),
        "priority": row.get("priority"),
        "duration_ticks": row.get("duration_ticks"),
        "remaining_ticks": row.get("remaining_ticks"),
        "resolves_at_tick": row.get("resolves_at_tick"),
        "location": row.get("location"),
        "participants": row.get("participants"),
        "status": row.get("status"),
        "state_impacts": row.get("state_impacts") or action_args.get("state_impacts") or {},
        "source": action_args.get("source") or row.get("source"),
    }


def extract_source(row: Dict[str, Any]) -> str:
    action_args = extract_action_args(row)
    payload = extract_event_payload(row)
    source = payload.get("source") or action_args.get("source") or row.get("source")
    return normalize_text(source).lower()


def extract_state_impacts(row: Dict[str, Any]) -> Dict[str, float]:
    payload = extract_event_payload(row)
    impacts = payload.get("state_impacts")
    if not isinstance(impacts, dict):
        return {}
    result: Dict[str, float] = {}
    for key, value in impacts.items():
        result[str(key)] = round(safe_float(value), 3)
    return result


def build_unique_event_records(actions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    priorities: Dict[str, int] = {}

    for row in actions:
        event_type = str(row.get("event_type") or "").lower()
        if event_type not in EVENT_ROW_PRIORITY:
            continue

        payload = extract_event_payload(row)
        event_id = normalize_text(payload.get("event_id") or row.get("event_id"))
        if not event_id:
            continue

        priority = EVENT_ROW_PRIORITY[event_type]
        if priority < priorities.get(event_id, -1):
            continue

        records[event_id] = {
            "event_id": event_id,
            "event_type": event_type,
            "title": normalize_text(payload.get("title") or row.get("title")),
            "summary": normalize_text(payload.get("summary") or row.get("summary") or row.get("result")),
            "location": normalize_text(payload.get("location") or row.get("location")),
            "source": extract_source(row),
            "state_impacts": extract_state_impacts(row),
        }
        priorities[event_id] = priority

    return records


def total_impact_magnitude(records: Dict[str, Dict[str, Any]], event_ids: Sequence[str]) -> float:
    total = 0.0
    for event_id in event_ids:
        impacts = records.get(event_id, {}).get("state_impacts") or {}
        total += sum(abs(safe_float(value)) for value in impacts.values())
    return round(total, 3)


def pressure_track_movement(initial: Dict[str, float], final: Dict[str, Any]) -> float:
    keys = set(initial) | {str(key) for key in final.keys()}
    total = 0.0
    for key in keys:
        total += abs(safe_float(final.get(key), safe_float(initial.get(key), 0.0)) - safe_float(initial.get(key), 0.0))
    return round(total, 3)


def resolve_final_world_state(
    world_state_payload: Dict[str, Any],
    snapshots: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if isinstance(world_state_payload.get("world_state"), dict):
        return world_state_payload["world_state"]
    if isinstance(world_state_payload, dict) and world_state_payload.get("tension") is not None:
        return world_state_payload
    for snapshot in reversed(snapshots):
        if isinstance(snapshot.get("world_state"), dict):
            return snapshot["world_state"]
    for row in reversed(actions):
        if isinstance(row.get("world_state"), dict):
            return row["world_state"]
    return {}


def build_tick_timeline(
    actions: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ticks: Dict[int, Dict[str, Any]] = {}

    def get_tick_entry(tick: int) -> Dict[str, Any]:
        return ticks.setdefault(
            tick,
            {
                "tick": tick,
                "intent_created": 0,
                "intent_resolved": 0,
                "intent_deferred": 0,
                "events_started": 0,
                "events_queued": 0,
                "events_completed": 0,
                "provider_waiting": 0,
                "ticks_blocked": 0,
                "wait_seconds": 0.0,
                "active_events_count": 0,
                "queued_events_count": 0,
                "completed_events_count": 0,
                "summary": "",
                "world_state": None,
                "phase": "idle",
            },
        )

    for row in actions:
        tick = safe_int(row.get("tick") or row.get("round"), 0)
        if tick <= 0:
            continue
        entry = get_tick_entry(tick)
        event_type = str(row.get("event_type") or "").lower()
        if event_type == "intent_created":
            entry["intent_created"] += 1
        elif event_type == "intent_resolved":
            entry["intent_resolved"] += 1
        elif event_type == "intent_deferred":
            entry["intent_deferred"] += 1
        elif event_type == "event_started":
            entry["events_started"] += 1
        elif event_type == "event_queued":
            entry["events_queued"] += 1
        elif event_type == "event_completed":
            entry["events_completed"] += 1
        elif event_type == "provider_waiting":
            entry["provider_waiting"] += 1
            entry["wait_seconds"] += safe_float(row.get("wait_seconds"), 0.0)
        elif event_type == "tick_blocked":
            entry["ticks_blocked"] += 1

        if event_type in {"tick_start", "tick_end"}:
            entry["phase"] = str(row.get("phase") or entry["phase"])
        if event_type == "tick_end":
            entry["summary"] = normalize_text(row.get("summary"))
            entry["active_events_count"] = safe_int(row.get("active_events_count"), entry["active_events_count"])
            entry["queued_events_count"] = safe_int(row.get("queued_events_count"), entry["queued_events_count"])
            entry["completed_events_count"] = safe_int(row.get("completed_events_count"), entry["completed_events_count"])
            if isinstance(row.get("world_state"), dict):
                entry["world_state"] = row["world_state"]

    for snapshot in snapshots:
        tick = safe_int(snapshot.get("tick") or snapshot.get("round"), 0)
        if tick <= 0:
            continue
        entry = get_tick_entry(tick)
        metrics = snapshot.get("metrics") or {}
        entry["summary"] = normalize_text(snapshot.get("summary")) or entry["summary"]
        entry["active_events_count"] = safe_int(metrics.get("active_events_count"), entry["active_events_count"])
        entry["queued_events_count"] = safe_int(metrics.get("queued_events_count"), entry["queued_events_count"])
        entry["completed_events_count"] = safe_int(metrics.get("completed_events_count"), entry["completed_events_count"])
        if isinstance(snapshot.get("world_state"), dict):
            entry["world_state"] = snapshot["world_state"]
        if snapshot.get("phase"):
            entry["phase"] = str(snapshot["phase"])

    return [ticks[tick] for tick in sorted(ticks)]


def overall_weights(score_profile: str) -> Dict[str, float]:
    if score_profile == "progression_mini":
        return {
            "speed": 0.20,
            "progression": 0.45,
            "resilience": 0.20,
            "cleanliness": 0.15,
        }
    return {
        "speed": 0.35,
        "progression": 0.25,
        "resilience": 0.20,
        "cleanliness": 0.20,
    }


def compute_case_metrics(
    case: EvalCase,
    case_config: Dict[str, Any],
    run_info: Dict[str, Any],
    actions: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
    world_state_payload: Dict[str, Any],
    run_id: str,
    run_dir: Path,
    score_profile: str,
) -> Dict[str, Any]:
    sim_start = first_ts(actions, "simulation_start")
    tick_start = first_ts(actions, "tick_start")
    resolver_clustered = first_ts(actions, "resolver_clustered")
    first_intent = first_ts(actions, "intent_created")
    first_event_started = first_ts(actions, "event_started") or first_ts(actions, "event_queued")
    tick_end = first_ts(actions, "tick_end")
    simulation_end = first_ts(actions, "simulation_end")
    last_ts = parse_ts(actions[-1].get("timestamp")) if actions else None

    intent_rows = [row for row in actions if row.get("event_type") == "intent_created"]
    resolution_rows = [row for row in actions if row.get("event_type") == "intent_resolved"]
    started_rows = [row for row in actions if row.get("event_type") == "event_started"]
    queued_rows = [row for row in actions if row.get("event_type") == "event_queued"]
    completed_rows = [row for row in actions if row.get("event_type") == "event_completed"]
    provider_wait_rows = [row for row in actions if row.get("event_type") == "provider_waiting"]
    blocked_rows = [row for row in actions if row.get("event_type") == "tick_blocked"]
    salvage_rows = [row for row in actions if row.get("event_type") in {"resolver_salvaged", "resolver_salvage"}]
    cluster_rows = [row for row in actions if row.get("event_type") == "resolver_clustered"]
    deferred_rows = [row for row in actions if row.get("event_type") == "intent_deferred"]
    tick_end_rows = [row for row in actions if row.get("event_type") == "tick_end"]

    started_ids = unique_ids(actions, "event_started")
    queued_ids = unique_ids(actions, "event_queued")
    completed_ids = unique_ids(actions, "event_completed")
    promoted_queued_ids = sorted(set(queued_ids) & set(started_ids))
    event_records = build_unique_event_records(actions)
    timeline = build_tick_timeline(actions, snapshots)

    dirty_intent_rows = [
        row
        for row in intent_rows
        if is_low_signal_title(row.get("title")) or extract_source(row) in LOW_SIGNAL_SOURCES
    ]
    dirty_event_rows = [
        row
        for row in [*started_rows, *queued_rows]
        if is_low_signal_title(row.get("title"))
    ]
    placeholder_locations = [
        row
        for row in intent_rows
        if normalize_text(row.get("location", "")).lower() in {"", "none", "unknown", "not specified"}
    ]
    event_titles = [normalize_text(row.get("title")) for row in [*started_rows, *queued_rows]]
    unique_event_title_ratio = (
        len({title for title in event_titles if title}) / len(event_titles)
        if event_titles
        else 0.0
    )

    low_signal_source_count = sum(1 for row in intent_rows if extract_source(row) in LOW_SIGNAL_SOURCES)
    recovered_intent_count = sum(1 for row in intent_rows if extract_source(row) in TEXT_RECOVERY_SOURCES)
    provider_wait_total_s = round(sum(safe_float(row.get("wait_seconds"), 0.0) for row in provider_wait_rows), 2)
    provider_wait_max_s = round(
        max((safe_float(row.get("wait_seconds"), 0.0) for row in provider_wait_rows), default=0.0),
        2,
    )
    salvage_event_source_count = sum(
        1 for event_id in [*started_ids, *queued_ids] if event_records.get(event_id, {}).get("source") in SALVAGE_SOURCES
    )

    avg_active_events = statistics.mean([entry["active_events_count"] for entry in timeline]) if timeline else 0.0
    avg_queued_events = statistics.mean([entry["queued_events_count"] for entry in timeline]) if timeline else 0.0
    peak_active_events = max((entry["active_events_count"] for entry in timeline), default=0)
    peak_queued_events = max((entry["queued_events_count"] for entry in timeline), default=0)
    dead_ticks = sum(
        1
        for entry in timeline
        if entry["intent_created"] == 0
        and entry["events_started"] == 0
        and entry["events_queued"] == 0
        and entry["events_completed"] == 0
        and entry["provider_waiting"] == 0
    )

    runtime_config = case_config.get("runtime_config", {})
    max_active_events = max(1, safe_int(runtime_config.get("max_active_events"), 1))
    max_queued_events = max(1, safe_int(runtime_config.get("max_queued_events"), 1))

    final_state = resolve_final_world_state(world_state_payload, snapshots, actions)
    initial_state = compute_initial_world_state(case_config)
    initial_pressure_tracks = compute_initial_pressure_tracks(case_config)
    final_pressure_tracks = final_state.get("pressure_tracks") or {}

    state_movement = (
        abs(safe_float(final_state.get("tension"), initial_state["tension"]) - initial_state["tension"])
        + abs(safe_float(final_state.get("stability"), initial_state["stability"]) - initial_state["stability"])
        + abs(safe_float(final_state.get("momentum"), initial_state["momentum"]) - initial_state["momentum"])
    )
    pressure_movement = pressure_track_movement(initial_pressure_tracks, final_pressure_tracks)
    impact_potential = total_impact_magnitude(event_records, [*started_ids, *queued_ids])
    realized_impact = total_impact_magnitude(event_records, completed_ids)
    progress_signal = max(round(state_movement + pressure_movement, 3), round(impact_potential * 0.25, 3))

    intents_created = len(intent_rows)
    ticks_finished = len(tick_end_rows) or len(timeline)
    events_started = len(started_ids)
    events_queued = len(queued_ids)
    events_completed = len(completed_ids)
    accepted_event_ratio = round((events_started + events_queued) / max(intents_created, 1), 3)
    queue_promotion_rate = (
        round(len(promoted_queued_ids) / len(queued_ids), 3)
        if queued_ids
        else None
    )
    dead_tick_rate = round(dead_ticks / max(ticks_finished, 1), 3)
    active_slot_utilization = round(avg_active_events / max_active_events, 3)
    queue_slot_utilization = round(avg_queued_events / max_queued_events, 3)
    salvage_tick_rate = round(len(salvage_rows) / max(ticks_finished, 1), 3)
    salvage_event_source_rate = round(salvage_event_source_count / max(events_started + events_queued, 1), 3)

    tick_duration = delta_seconds(tick_start, tick_end)
    intents_per_minute = (
        round(intents_created / (tick_duration / 60), 3)
        if tick_duration and intents_created
        else None
    )

    dirty_intent_titles = [normalize_text(row.get("title")) for row in dirty_intent_rows if normalize_text(row.get("title"))]
    dirty_event_titles = [normalize_text(row.get("title")) for row in dirty_event_rows if normalize_text(row.get("title"))]

    accepted_resolution_rows = [row for row in resolution_rows if row.get("status") in {"accepted", "queued"}]
    rejected_resolution_rows = [row for row in resolution_rows if row.get("status") == "rejected"]

    metrics: Dict[str, Any] = {
        "run_id": run_id,
        "output_dir": str(run_dir),
        "case_id": case.case_id,
        "label": case.label,
        "actor_selector": case.actor_selector,
        "resolver_selector": case.resolver_selector,
        "max_rounds": case.max_rounds,
        "score_profile": score_profile,
        "run_exit_code": run_info["exit_code"],
        "run_wall_clock_seconds": run_info["wall_clock_seconds"],
        "events": {
            "intents_created": intents_created,
            "intents_accepted": len(accepted_resolution_rows),
            "intents_rejected": len(rejected_resolution_rows),
            "intents_deferred": len(deferred_rows),
            "events_started": events_started,
            "events_queued": events_queued,
            "events_completed": events_completed,
            "resolver_clusters": len(cluster_rows),
            "resolver_salvages": len(salvage_rows),
            "provider_waits": len(provider_wait_rows),
            "ticks_blocked": len(blocked_rows),
            "ticks_finished": ticks_finished,
        },
        "timing": {
            "simulation_to_tick_start_s": delta_seconds(sim_start, tick_start),
            "tick_start_to_first_intent_s": delta_seconds(tick_start, first_intent),
            "tick_start_to_resolver_clustered_s": delta_seconds(tick_start, resolver_clustered),
            "tick_start_to_first_event_s": delta_seconds(tick_start, first_event_started),
            "tick_start_to_tick_end_s": tick_duration,
            "simulation_total_s": delta_seconds(sim_start, simulation_end or last_ts),
        },
        "concurrency": {
            "avg_active_events": round(avg_active_events, 3),
            "peak_active_events": peak_active_events,
            "avg_queued_events": round(avg_queued_events, 3),
            "peak_queued_events": peak_queued_events,
            "active_slot_utilization": active_slot_utilization,
            "queue_slot_utilization": queue_slot_utilization,
            "dead_tick_rate": dead_tick_rate,
            "queue_promotion_rate": queue_promotion_rate,
        },
        "quality": {
            "dirty_intent_title_count": len(dirty_intent_rows),
            "dirty_event_title_count": len(dirty_event_rows),
            "dirty_intent_title_rate": round(len(dirty_intent_rows) / len(intent_rows), 3) if intent_rows else 0.0,
            "dirty_event_title_rate": round(len(dirty_event_rows) / max(events_started + events_queued, 1), 3),
            "placeholder_location_rate": round(len(placeholder_locations) / len(intent_rows), 3) if intent_rows else 0.0,
            "unique_event_title_ratio": round(unique_event_title_ratio, 3),
            "low_signal_source_rate": round(low_signal_source_count / len(intent_rows), 3) if intent_rows else 0.0,
            "recovered_intent_rate": round(recovered_intent_count / len(intent_rows), 3) if intent_rows else 0.0,
            "state_movement": round(state_movement, 3),
            "pressure_track_movement": round(pressure_movement, 3),
            "impact_potential": impact_potential,
            "progress_signal": round(progress_signal, 3),
        },
        "diagnostics": {
            "intents_per_minute": intents_per_minute,
            "accepted_event_ratio": accepted_event_ratio,
            "provider_wait_total_s": provider_wait_total_s,
            "provider_wait_max_s": provider_wait_max_s,
            "salvage_tick_rate": salvage_tick_rate,
            "salvage_event_source_rate": salvage_event_source_rate,
            "realized_impact": realized_impact,
        },
        "samples": {
            "intent_titles": [normalize_text(row.get("title")) for row in intent_rows[:5]],
            "event_titles": event_titles[:5],
            "dirty_intent_titles": dirty_intent_titles[:5],
            "dirty_event_titles": dirty_event_titles[:5],
        },
        "final_world_state": {
            "tension": final_state.get("tension"),
            "stability": final_state.get("stability"),
            "momentum": final_state.get("momentum"),
            "pressure_tracks": final_state.get("pressure_tracks", {}),
            "last_tick_summary": final_state.get("last_tick_summary"),
        },
    }

    dirty_intent_rate = metrics["quality"]["dirty_intent_title_rate"]
    dirty_event_rate = metrics["quality"]["dirty_event_title_rate"]
    placeholder_rate = metrics["quality"]["placeholder_location_rate"]
    low_signal_source_rate = metrics["quality"]["low_signal_source_rate"]
    recovered_intent_rate = metrics["quality"]["recovered_intent_rate"]

    speed_score = (
        score_lower_better(metrics["timing"]["tick_start_to_first_intent_s"], excellent=12, acceptable=120) * 0.25
        + score_lower_better(metrics["timing"]["tick_start_to_first_event_s"], excellent=20, acceptable=180) * 0.30
        + score_lower_better(metrics["timing"]["tick_start_to_tick_end_s"], excellent=45, acceptable=240) * 0.25
        + score_higher_better(intents_per_minute, acceptable=2.0, excellent=7.0) * 0.20
    )

    progression_score = (
        score_higher_better(events_started / max(ticks_finished, 1), acceptable=0.5, excellent=2.5) * 0.22
        + score_higher_better(events_completed / max(ticks_finished, 1), acceptable=0.0, excellent=1.0) * 0.12
        + score_higher_better(accepted_event_ratio, acceptable=0.15, excellent=0.65) * 0.18
        + score_higher_better(active_slot_utilization, acceptable=0.15, excellent=0.75) * 0.18
        + score_lower_better(dead_tick_rate, excellent=0.0, acceptable=0.60) * 0.10
        + score_higher_better(progress_signal, acceptable=0.05, excellent=0.30) * 0.20
    )

    exit_code_score = 100.0 if run_info["exit_code"] == 0 else 0.0
    resilience_score = (
        score_lower_better(metrics["events"]["provider_waits"], excellent=0, acceptable=4) * 0.15
        + score_lower_better(provider_wait_total_s, excellent=0, acceptable=60) * 0.15
        + score_lower_better(metrics["events"]["ticks_blocked"], excellent=0, acceptable=2) * 0.20
        + score_lower_better(salvage_tick_rate, excellent=0, acceptable=1.0) * 0.25
        + score_lower_better(salvage_event_source_rate, excellent=0, acceptable=0.90) * 0.10
        + exit_code_score * 0.15
    )

    cleanliness_score = (
        score_lower_better(dirty_intent_rate, excellent=0.05, acceptable=0.50) * 0.25
        + score_lower_better(dirty_event_rate, excellent=0.00, acceptable=0.45) * 0.25
        + score_lower_better(placeholder_rate, excellent=0.05, acceptable=0.50) * 0.10
        + score_lower_better(low_signal_source_rate, excellent=0.00, acceptable=0.40) * 0.15
        + score_higher_better(metrics["quality"]["unique_event_title_ratio"], acceptable=0.45, excellent=0.95) * 0.10
        + score_lower_better(recovered_intent_rate, excellent=0.15, acceptable=0.90) * 0.15
    )

    weights = overall_weights(score_profile)
    overall = (
        progression_score * weights["progression"]
        + speed_score * weights["speed"]
        + resilience_score * weights["resilience"]
        + cleanliness_score * weights["cleanliness"]
    )
    metrics["scores"] = {
        "speed": round(speed_score, 1),
        "progression": round(progression_score, 1),
        "resilience": round(resilience_score, 1),
        "cleanliness": round(cleanliness_score, 1),
        "overall": round(overall, 1),
    }
    return metrics


def build_failed_run_metrics(
    case: EvalCase,
    run_id: str,
    run_dir: Path,
    score_profile: str,
    run_info: Optional[Dict[str, Any]],
    error: str,
    tb: str,
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "output_dir": str(run_dir),
        "case_id": case.case_id,
        "label": case.label,
        "actor_selector": case.actor_selector,
        "resolver_selector": case.resolver_selector,
        "max_rounds": case.max_rounds,
        "score_profile": score_profile,
        "run_exit_code": (run_info or {}).get("exit_code", 1),
        "run_wall_clock_seconds": (run_info or {}).get("wall_clock_seconds"),
        "events": {},
        "timing": {},
        "concurrency": {},
        "quality": {},
        "diagnostics": {},
        "samples": {
            "intent_titles": [],
            "event_titles": [],
            "dirty_intent_titles": [],
            "dirty_event_titles": [],
        },
        "final_world_state": {},
        "scores": {
            "speed": 0.0,
            "progression": 0.0,
            "resilience": 0.0,
            "cleanliness": 0.0,
            "overall": 0.0,
        },
        "error": error,
        "traceback": tb,
    }


def aggregate_section(
    runs: Sequence[Dict[str, Any]],
    section_name: str,
    keys: Sequence[str],
    digits: int = 3,
) -> Dict[str, Optional[float]]:
    aggregated: Dict[str, Optional[float]] = {}
    for key in keys:
        stats = numeric_stats([run.get(section_name, {}).get(key) for run in runs])
        mean = stats["mean"]
        aggregated[key] = round(mean, digits) if mean is not None else None
    return aggregated


def aggregate_case_results(
    case: EvalCase,
    score_profile: str,
    run_metrics: List[Dict[str, Any]],
) -> Dict[str, Any]:
    successful_runs = [run for run in run_metrics if not run.get("error") and run.get("run_exit_code") == 0]
    basis = successful_runs or run_metrics
    best_run = max(basis, key=lambda item: item.get("scores", {}).get("overall", 0.0), default=None)

    score_spread = {
        key: numeric_stats([run.get("scores", {}).get(key) for run in run_metrics])
        for key in AGG_SCORE_KEYS
    }

    summary = {
        "case_id": case.case_id,
        "label": case.label,
        "actor_selector": case.actor_selector,
        "resolver_selector": case.resolver_selector,
        "max_rounds": case.max_rounds,
        "repeat_count": case.repeat_count,
        "completed_repeats": len(run_metrics),
        "failed_repeats": len([run for run in run_metrics if run.get("error") or run.get("run_exit_code") != 0]),
        "score_profile": score_profile,
        "notes": case.notes,
        "scores": aggregate_section(run_metrics, "scores", AGG_SCORE_KEYS, digits=1),
        "score_spread": score_spread,
        "timing": aggregate_section(run_metrics, "timing", AGG_TIMING_KEYS),
        "events": aggregate_section(run_metrics, "events", AGG_EVENT_KEYS),
        "concurrency": aggregate_section(run_metrics, "concurrency", AGG_CONCURRENCY_KEYS),
        "quality": aggregate_section(run_metrics, "quality", AGG_QUALITY_KEYS),
        "diagnostics": aggregate_section(run_metrics, "diagnostics", AGG_DIAGNOSTIC_KEYS),
        "samples": (best_run or {}).get("samples", {}),
        "final_world_state": (best_run or {}).get("final_world_state", {}),
        "runs": [
            {
                "run_id": run.get("run_id"),
                "output_dir": run.get("output_dir"),
                "run_exit_code": run.get("run_exit_code"),
                "run_wall_clock_seconds": run.get("run_wall_clock_seconds"),
                "scores": run.get("scores", {}),
                "timing": run.get("timing", {}),
                "events": run.get("events", {}),
                "concurrency": run.get("concurrency", {}),
                "quality": run.get("quality", {}),
                "diagnostics": run.get("diagnostics", {}),
                "error": run.get("error"),
            }
            for run in run_metrics
        ],
    }
    return summary


def build_leaderboard(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(results, key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    leaderboard: List[Dict[str, Any]] = []
    for index, item in enumerate(ordered, start=1):
        leaderboard.append(
            {
                "rank": index,
                "case_id": item["case_id"],
                "label": item["label"],
                "actor_selector": item["actor_selector"],
                "resolver_selector": item["resolver_selector"],
                "repeat_count": item["repeat_count"],
                "failed_repeats": item["failed_repeats"],
                "scores": item.get("scores", {}),
                "score_spread": {
                    key: value.get("std")
                    for key, value in (item.get("score_spread") or {}).items()
                },
                "timing": {
                    "tick_start_to_first_event_s": item.get("timing", {}).get("tick_start_to_first_event_s"),
                    "tick_start_to_tick_end_s": item.get("timing", {}).get("tick_start_to_tick_end_s"),
                },
                "diagnostics": {
                    "accepted_event_ratio": item.get("diagnostics", {}).get("accepted_event_ratio"),
                    "salvage_tick_rate": item.get("diagnostics", {}).get("salvage_tick_rate"),
                },
                "concurrency": {
                    "dead_tick_rate": item.get("concurrency", {}).get("dead_tick_rate"),
                    "active_slot_utilization": item.get("concurrency", {}).get("active_slot_utilization"),
                },
            }
        )
    return leaderboard


def render_run_markdown(metrics: Dict[str, Any]) -> str:
    if metrics.get("error"):
        return (
            f"# {metrics['label']} / {metrics['run_id']}\n\n"
            f"- `actor_selector`: `{metrics['actor_selector']}`\n"
            f"- `resolver_selector`: `{metrics['resolver_selector']}`\n"
            f"- error: `{metrics['error']}`\n"
        )

    timing = metrics["timing"]
    events = metrics["events"]
    concurrency = metrics["concurrency"]
    quality = metrics["quality"]
    diagnostics = metrics["diagnostics"]
    scores = metrics["scores"]
    lines = [
        f"# {metrics['label']} / {metrics['run_id']}",
        "",
        f"- `actor_selector`: `{metrics['actor_selector']}`",
        f"- `resolver_selector`: `{metrics['resolver_selector']}`",
        f"- `overall`: `{scores['overall']}`",
        f"- `output_dir`: `{metrics['output_dir']}`",
        "",
        "## Scores",
        f"- Speed: `{scores['speed']}`",
        f"- Progression: `{scores['progression']}`",
        f"- Resilience: `{scores['resilience']}`",
        f"- Cleanliness: `{scores['cleanliness']}`",
        "",
        "## Timing",
        f"- `tick_start -> first_intent`: `{format_number(timing.get('tick_start_to_first_intent_s'))}` s",
        f"- `tick_start -> first_event`: `{format_number(timing.get('tick_start_to_first_event_s'))}` s",
        f"- `tick_start -> tick_end`: `{format_number(timing.get('tick_start_to_tick_end_s'))}` s",
        f"- `simulation_total`: `{format_number(timing.get('simulation_total_s'))}` s",
        "",
        "## Progression",
        f"- intents_created: `{format_number(events.get('intents_created'), 0)}`",
        f"- events_started: `{format_number(events.get('events_started'), 0)}`",
        f"- events_completed: `{format_number(events.get('events_completed'), 0)}`",
        f"- accepted_event_ratio: `{format_number(diagnostics.get('accepted_event_ratio'))}`",
        f"- progress_signal: `{format_number(quality.get('progress_signal'))}`",
        "",
        "## Concurrency",
        f"- avg_active_events: `{format_number(concurrency.get('avg_active_events'))}`",
        f"- active_slot_utilization: `{format_number(concurrency.get('active_slot_utilization'))}`",
        f"- dead_tick_rate: `{format_number(concurrency.get('dead_tick_rate'))}`",
        f"- queue_promotion_rate: `{format_number(concurrency.get('queue_promotion_rate'))}`",
        "",
        "## Cleanliness",
        f"- dirty_intent_title_rate: `{format_number(quality.get('dirty_intent_title_rate'))}`",
        f"- dirty_event_title_rate: `{format_number(quality.get('dirty_event_title_rate'))}`",
        f"- low_signal_source_rate: `{format_number(quality.get('low_signal_source_rate'))}`",
        f"- recovered_intent_rate: `{format_number(quality.get('recovered_intent_rate'))}`",
        "",
        "## Resilience",
        f"- provider_wait_total_s: `{format_number(diagnostics.get('provider_wait_total_s'))}`",
        f"- salvage_tick_rate: `{format_number(diagnostics.get('salvage_tick_rate'))}`",
        f"- salvage_event_source_rate: `{format_number(diagnostics.get('salvage_event_source_rate'))}`",
        "",
        "## Samples",
        *[f"- intent: `{title}`" for title in metrics["samples"]["intent_titles"]],
        *[f"- event: `{title}`" for title in metrics["samples"]["event_titles"]],
    ]
    return "\n".join(lines) + "\n"


def render_case_markdown(summary: Dict[str, Any]) -> str:
    timing = summary["timing"]
    events = summary["events"]
    concurrency = summary["concurrency"]
    quality = summary["quality"]
    diagnostics = summary["diagnostics"]
    scores = summary["scores"]
    spread = summary["score_spread"]
    lines = [
        f"# {summary['label']}",
        "",
        f"- `actor_selector`: `{summary['actor_selector']}`",
        f"- `resolver_selector`: `{summary['resolver_selector']}`",
        f"- repeats: `{summary['completed_repeats']}/{summary['repeat_count']}`",
        f"- failed_repeats: `{summary['failed_repeats']}`",
        f"- overall_mean: `{format_number(scores.get('overall'), 1)}`",
        f"- overall_std: `{format_number((spread.get('overall') or {}).get('std'))}`",
        "",
        "## Score Means",
        f"- Speed: `{format_number(scores.get('speed'), 1)}`",
        f"- Progression: `{format_number(scores.get('progression'), 1)}`",
        f"- Resilience: `{format_number(scores.get('resilience'), 1)}`",
        f"- Cleanliness: `{format_number(scores.get('cleanliness'), 1)}`",
        "",
        "## Timing Means",
        f"- `tick_start -> first_intent`: `{format_number(timing.get('tick_start_to_first_intent_s'))}` s",
        f"- `tick_start -> first_event`: `{format_number(timing.get('tick_start_to_first_event_s'))}` s",
        f"- `tick_start -> tick_end`: `{format_number(timing.get('tick_start_to_tick_end_s'))}` s",
        f"- `simulation_total`: `{format_number(timing.get('simulation_total_s'))}` s",
        "",
        "## World Advancement",
        f"- intents_created: `{format_number(events.get('intents_created'))}`",
        f"- events_started: `{format_number(events.get('events_started'))}`",
        f"- events_completed: `{format_number(events.get('events_completed'))}`",
        f"- accepted_event_ratio: `{format_number(diagnostics.get('accepted_event_ratio'))}`",
        f"- progress_signal: `{format_number(quality.get('progress_signal'))}`",
        "",
        "## Concurrency",
        f"- avg_active_events: `{format_number(concurrency.get('avg_active_events'))}`",
        f"- avg_queued_events: `{format_number(concurrency.get('avg_queued_events'))}`",
        f"- active_slot_utilization: `{format_number(concurrency.get('active_slot_utilization'))}`",
        f"- dead_tick_rate: `{format_number(concurrency.get('dead_tick_rate'))}`",
        f"- queue_promotion_rate: `{format_number(concurrency.get('queue_promotion_rate'))}`",
        "",
        "## Cleanliness",
        f"- dirty_intent_title_rate: `{format_number(quality.get('dirty_intent_title_rate'))}`",
        f"- dirty_event_title_rate: `{format_number(quality.get('dirty_event_title_rate'))}`",
        f"- low_signal_source_rate: `{format_number(quality.get('low_signal_source_rate'))}`",
        f"- recovered_intent_rate: `{format_number(quality.get('recovered_intent_rate'))}`",
        "",
        "## Resilience",
        f"- provider_wait_total_s: `{format_number(diagnostics.get('provider_wait_total_s'))}`",
        f"- salvage_tick_rate: `{format_number(diagnostics.get('salvage_tick_rate'))}`",
        f"- salvage_event_source_rate: `{format_number(diagnostics.get('salvage_event_source_rate'))}`",
        "",
        "## Samples",
        *[f"- intent: `{title}`" for title in summary.get("samples", {}).get("intent_titles", [])],
        *[f"- event: `{title}`" for title in summary.get("samples", {}).get("event_titles", [])],
    ]
    return "\n".join(lines) + "\n"


def render_suite_markdown(
    suite: Dict[str, Any],
    results: List[Dict[str, Any]],
    output_dir: Path,
) -> str:
    ordered = sorted(results, key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    lines = [
        "# World Model Eval",
        "",
        suite.get("description", "World-simulation model capability benchmark."),
        "",
        f"- Output dir: `{output_dir}`",
        f"- Suite name: `{suite.get('name', 'world-model-eval')}`",
        f"- Score profile: `{suite.get('score_profile', 'latency_smoke')}`",
        f"- Cases: `{len(results)}`",
        "",
        "## Ranking",
    ]
    for index, item in enumerate(ordered, start=1):
        spread = item.get("score_spread", {}).get("overall", {})
        lines.append(
            f"{index}. `{item['label']}` overall=`{format_number(item['scores'].get('overall'), 1)}` "
            f"std=`{format_number(spread.get('std'))}` "
            f"first_event=`{format_number(item['timing'].get('tick_start_to_first_event_s'))}`s "
            f"tick_end=`{format_number(item['timing'].get('tick_start_to_tick_end_s'))}`s "
            f"started=`{format_number(item['events'].get('events_started'))}` "
            f"dead_tick_rate=`{format_number(item['concurrency'].get('dead_tick_rate'))}` "
            f"salvage_tick_rate=`{format_number(item['diagnostics'].get('salvage_tick_rate'))}`"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "- `overall` is a triage score. Read the raw timing, dead-tick, salvage, and cleanliness metrics before drawing conclusions.",
            "- `progress_signal` combines realized world-state movement with accepted-event impact potential, so 1-tick smoke runs are still comparable.",
            "- `dirty_*_title_rate` and `low_signal_source_rate` are meant to catch generic intent headings and low-signal fallback artifacts leaking into the timeline.",
            "- Use the latency smoke suite to compare fast stall risk, and the progression mini suite to compare sustained world advancement.",
        ]
    )
    return "\n".join(lines) + "\n"


def list_selectors() -> None:
    registry_path = REPO_ROOT / "llm_registry.json"
    payload = load_json(registry_path)
    profiles = payload.get("profiles", {})
    print(json.dumps(profiles, ensure_ascii=False, indent=2))


def list_cases(suite_config_path: Path) -> None:
    suite, cases = resolve_suite_cases(suite_config_path)
    print(
        json.dumps(
            {
                "suite": suite.get("name"),
                "score_profile": suite.get("score_profile", "latency_smoke"),
                "cases": [asdict(case) for case in cases],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate world-simulation model selectors.")
    parser.add_argument("--base-config", required=False, default="", help="Path to a world simulation_config.json")
    parser.add_argument("--suite-config", default=str(DEFAULT_SUITE_CONFIG), help="Path to the eval suite JSON file")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Directory for eval outputs")
    parser.add_argument("--python-bin", default=sys.executable, help="Python interpreter used to run world simulation")
    parser.add_argument("--cases", default="", help="Comma-separated case ids to run")
    parser.add_argument("--repeat-count", type=int, default=0, help="Override repeat count for every resolved case")
    parser.add_argument("--list-selectors", action="store_true", help="Print known selector profiles and exit")
    parser.add_argument("--list-cases", action="store_true", help="Print resolved suite cases and exit")
    args = parser.parse_args()

    suite_config_path = Path(args.suite_config).resolve()

    if args.list_selectors:
        list_selectors()
        return
    if args.list_cases:
        list_cases(suite_config_path)
        return
    if not args.base_config:
        raise SystemExit("--base-config is required unless --list-selectors or --list-cases is used")

    base_config_path = Path(args.base_config).resolve()
    output_root = ensure_dir(Path(args.output_root).resolve())
    filters = {slugify(item) for item in args.cases.split(",") if item.strip()} or None
    repeat_override = args.repeat_count if args.repeat_count > 0 else None

    suite, cases = resolve_suite_cases(
        suite_config_path,
        filters=filters,
        repeat_override=repeat_override,
    )
    if not cases:
        raise SystemExit("No eval cases resolved from suite config")

    base_config = load_json(base_config_path)
    score_profile = str(suite.get("score_profile") or "latency_smoke").strip() or "latency_smoke"
    suite_dir = ensure_dir(output_root / f"world-model-eval-{now_stamp()}")

    eval_plan = {
        "suite": suite,
        "base_config": str(base_config_path),
        "generated_at": datetime.now().isoformat(),
        "score_profile": score_profile,
        "repeat_override": repeat_override,
        "cases": [asdict(case) for case in cases],
    }
    write_json(suite_dir / "eval_plan.json", eval_plan)
    write_json(suite_dir / "suite_manifest.json", eval_plan)

    results: List[Dict[str, Any]] = []

    for case in cases:
        case_dir = ensure_dir(suite_dir / case.case_id)
        write_json(case_dir / "case_manifest.json", asdict(case))
        run_metrics: List[Dict[str, Any]] = []

        for repeat_index in range(1, case.repeat_count + 1):
            run_id = f"run-{repeat_index:02d}"
            run_dir = ensure_dir(case_dir / run_id)
            config_path = run_dir / "simulation_config.json"
            log_path = run_dir / "run.log"
            case_config = build_case_config(base_config, case, repeat_index)
            write_json(config_path, case_config)

            run_info: Optional[Dict[str, Any]] = None
            try:
                run_info = run_case(
                    python_bin=args.python_bin,
                    config_path=config_path,
                    max_rounds=case.max_rounds,
                    log_path=log_path,
                )
                world_dir = run_dir / "world"
                actions = load_jsonl(world_dir / "actions.jsonl")
                snapshots = load_jsonl(world_dir / "state_snapshots.jsonl")
                world_state_payload = (
                    load_json(world_dir / "world_state.json")
                    if (world_dir / "world_state.json").exists()
                    else {}
                )
                metrics = compute_case_metrics(
                    case=case,
                    case_config=case_config,
                    run_info=run_info,
                    actions=actions,
                    snapshots=snapshots,
                    world_state_payload=world_state_payload,
                    run_id=run_id,
                    run_dir=run_dir,
                    score_profile=score_profile,
                )
            except Exception as exc:
                metrics = build_failed_run_metrics(
                    case=case,
                    run_id=run_id,
                    run_dir=run_dir,
                    score_profile=score_profile,
                    run_info=run_info,
                    error=str(exc),
                    tb=traceback.format_exc(limit=12),
                )

            write_json(run_dir / "metrics.json", metrics)
            (run_dir / "summary.md").write_text(render_run_markdown(metrics), encoding="utf-8")
            run_metrics.append(metrics)

        case_summary = aggregate_case_results(case, score_profile, run_metrics)
        write_json(case_dir / "runs.json", {"runs": run_metrics})
        write_json(case_dir / "metrics.json", case_summary)
        (case_dir / "summary.md").write_text(render_case_markdown(case_summary), encoding="utf-8")
        results.append(case_summary)

    leaderboard = build_leaderboard(results)
    summary = {
        "generated_at": datetime.now().isoformat(),
        "base_config": str(base_config_path),
        "suite_config": str(suite_config_path),
        "score_profile": score_profile,
        "results": results,
    }
    write_json(suite_dir / "leaderboard.json", {"leaderboard": leaderboard})
    write_json(suite_dir / "summary.json", summary)
    report = render_suite_markdown(suite, results, suite_dir)
    (suite_dir / "summary.md").write_text(report, encoding="utf-8")
    (suite_dir / "report.md").write_text(report, encoding="utf-8")
    print(str(suite_dir))


if __name__ == "__main__":
    main()
