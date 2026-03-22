"""
Build a reader-facing story payload from world-mode runtime artifacts.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import Config
from .report_agent import ReportManager


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _load_json_array(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _latest_matching_path(root: Path, suffix: str) -> Optional[Path]:
    if not root.is_dir():
        return None
    matches = sorted(path for path in root.iterdir() if path.is_file() and path.name.endswith(suffix))
    return matches[-1] if matches else None


def _latest_diagnostics_summary_path(root: Path) -> Optional[Path]:
    if not root.is_dir():
        return None
    matches = sorted(
        path
        for path in root.iterdir()
        if path.is_file()
        and path.suffix == ".json"
        and not path.name.endswith("-chronicle.json")
        and not path.name.endswith("-actor-board.json")
        and not path.name.endswith("-risk-digest.json")
    )
    return matches[-1] if matches else None


def _chunked(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _unique_texts(values: Iterable[str], limit: int) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned or cleaned in seen:
            continue
        result.append(cleaned)
        seen.add(cleaned)
        if len(result) >= limit:
            break
    return result


class WorldStoryService:
    @classmethod
    def build_story_payload(cls, simulation_id: str) -> Dict[str, Any]:
        sim_dir = Path(Config.UPLOAD_FOLDER).resolve() / "simulations" / simulation_id
        if not sim_dir.exists():
            raise ValueError(f"simulation not found: {simulation_id}")
        world_dir = sim_dir / "world"
        diagnostics_dir = world_dir / "diagnostics"

        config = _load_json(sim_dir / "simulation_config.json")
        fork_meta = _load_json(sim_dir / "fork_meta.json")
        checkpoint = _load_json(world_dir / "checkpoint.json")
        diagnostics_path = _latest_diagnostics_summary_path(diagnostics_dir)
        diagnostics = _load_json(diagnostics_path) if diagnostics_path else {}
        chronicle_path = _latest_matching_path(diagnostics_dir, "-chronicle.json")
        actor_board_path = _latest_matching_path(diagnostics_dir, "-actor-board.json")
        risk_digest_path = _latest_matching_path(diagnostics_dir, "-risk-digest.json")
        chronicle = _load_json_array(chronicle_path) if chronicle_path else []
        actor_board_payload = _load_json(actor_board_path) if actor_board_path else {}
        risk_digest = _load_json(risk_digest_path) if risk_digest_path else {}

        report = ReportManager.get_report_by_simulation(simulation_id)
        actor_rows = [row for row in actor_board_payload.get("actors") or [] if isinstance(row, dict)]
        risk_items = [item for item in risk_digest.get("items") or [] if isinstance(item, dict)]
        report_title = _clean_text(getattr(report.outline, "title", "")) if report and report.outline else ""
        report_summary = _clean_text(getattr(report.outline, "summary", "")) if report and report.outline else ""
        simulation_title = (
            _clean_text(config.get("world_pack", {}).get("title"))
            or report_title
            or _clean_text(config.get("simulation_requirement"))[:80]
            or simulation_id
        )

        hero = cls._build_hero(simulation_id, config, diagnostics, chronicle, actor_rows, risk_items, report)
        episodes = cls._build_episodes(chronicle, actor_rows, risk_items)
        factions = cls._build_factions(actor_rows)
        risks = cls._build_risks(risk_items)
        process = cls._build_process()

        return {
            "simulation_id": simulation_id,
            "hero": hero,
            "episodes": episodes,
            "factions": factions,
            "risks": risks,
            "process": process,
            "meta": {
                "simulation_title": simulation_title,
                "ticks": _safe_int(checkpoint.get("last_completed_tick"), len(chronicle)),
                "status": _clean_text(checkpoint.get("status")) or "unknown",
                "report_id": report.report_id if report else None,
                "report_title": report_title,
                "report_summary": report_summary,
                "fork_origin": (
                    fork_meta.get("fork_origin")
                    if isinstance(fork_meta.get("fork_origin"), dict)
                    else config.get("fork_origin")
                ),
                "source_paths": {
                    "diagnostics_json": str(diagnostics_path) if diagnostics_path else "",
                    "chronicle_json": str(chronicle_path) if chronicle_path else "",
                    "actor_board_json": str(actor_board_path) if actor_board_path else "",
                    "risk_digest_json": str(risk_digest_path) if risk_digest_path else "",
                },
            },
        }

    @classmethod
    def _build_hero(
        cls,
        simulation_id: str,
        config: Dict[str, Any],
        diagnostics: Dict[str, Any],
        chronicle: List[Dict[str, Any]],
        actor_rows: List[Dict[str, Any]],
        risk_items: List[Dict[str, Any]],
        report,
    ) -> Dict[str, Any]:
        latest_entry = chronicle[-1] if chronicle else {}
        world_state = (latest_entry.get("world_state") or {}) if isinstance(latest_entry, dict) else {}
        report_title = _clean_text(getattr(report.outline, "title", "")) if report and report.outline else ""
        report_summary = _clean_text(getattr(report.outline, "summary", "")) if report and report.outline else ""
        last_tick_summary = _clean_text(
            latest_entry.get("summary")
            or diagnostics.get("diagnostics", {}).get("world_state", {}).get("last_tick_summary")
        )
        top_risks = risk_items[:3]
        top_factions = actor_rows[:4]
        world_title = _clean_text(config.get("world_pack", {}).get("title")) or report_title or simulation_id

        headline = _clean_text(latest_entry.get("scene_title")) or "World in Motion"
        subtitle = last_tick_summary or report_summary or _clean_text(config.get("simulation_requirement"))
        if top_risks:
            risk_headline = _clean_text(top_risks[0].get("title"))
            if risk_headline:
                headline = f"{headline} / {risk_headline}"

        return {
            "eyebrow": f"{world_title} world chronicle",
            "headline": headline,
            "subtitle": subtitle,
            "cta": {
                "primary_label": "Read the full chronicle",
                "secondary_label": "Open the report",
                "report_id": report.report_id if report else None,
            },
            "metrics": [
                {
                    "label": "Tension",
                    "value": round(_safe_float(world_state.get("tension"), 0.0), 2),
                    "tone": "hot",
                },
                {
                    "label": "Stability",
                    "value": round(_safe_float(world_state.get("stability"), 0.0), 2),
                    "tone": "cool",
                },
                {
                    "label": "Momentum",
                    "value": round(_safe_float(world_state.get("momentum"), 0.0), 2),
                    "tone": "surge",
                },
            ],
            "flashpoints": [
                {
                    "title": _clean_text(item.get("title")),
                    "summary": _clean_text(item.get("summary")),
                    "severity": _safe_float(item.get("severity"), 0.0),
                }
                for item in top_risks
            ],
            "top_factions": [
                {
                    "name": _clean_text(item.get("agent_name")),
                    "activity_score": _safe_int(item.get("activity_score")),
                    "latest_move": _clean_text(item.get("latest_move")),
                }
                for item in top_factions
            ],
        }

    @classmethod
    def _build_episodes(
        cls,
        chronicle: List[Dict[str, Any]],
        actor_rows: List[Dict[str, Any]],
        risk_items: List[Dict[str, Any]],
        episode_size: int = 8,
    ) -> List[Dict[str, Any]]:
        episodes: List[Dict[str, Any]] = []
        actor_index = {row.get("agent_name"): row for row in actor_rows}
        chunks = _chunked(chronicle, episode_size)
        for index, chunk in enumerate(chunks, start=1):
            if not chunk:
                continue
            first = chunk[0]
            last = chunk[-1]
            scene_counter = Counter(_clean_text(item.get("scene_title")) for item in chunk if _clean_text(item.get("scene_title")))
            dominant_scene = scene_counter.most_common(1)[0][0] if scene_counter else f"Episode {index:02d}"
            accepted_titles = _unique_texts(
                (title for item in chunk for title in (item.get("accepted_titles") or [])),
                3,
            )
            completed_titles = _unique_texts(
                (title for item in chunk for title in (item.get("completed_titles") or [])),
                3,
            )
            started_titles = _unique_texts(
                (title for item in chunk for title in (item.get("started_titles") or [])),
                2,
            )
            key_actors = _unique_texts(
                (actor for item in chunk for actor in (item.get("key_actors") or [])),
                4,
            )
            tension_delta = round(
                _safe_float((last.get("world_state") or {}).get("tension"), 0.0)
                - _safe_float((first.get("world_state") or {}).get("tension"), 0.0),
                2,
            )
            stability_delta = round(
                _safe_float((last.get("world_state") or {}).get("stability"), 0.0)
                - _safe_float((first.get("world_state") or {}).get("stability"), 0.0),
                2,
            )
            featured_rows = [actor_index.get(name) for name in key_actors if actor_index.get(name)]
            cliffhanger = started_titles[0] if started_titles else (risk_items[0].get("title") if risk_items else "")
            episodes.append(
                {
                    "id": f"episode-{index:02d}",
                    "index": index,
                    "title": f"Episode {index:02d} · {dominant_scene}",
                    "tick_start": _safe_int(first.get("tick")),
                    "tick_end": _safe_int(last.get("tick")),
                    "logline": _clean_text(last.get("summary") or first.get("summary")),
                    "turning_points": accepted_titles or completed_titles,
                    "payoffs": completed_titles,
                    "cliffhanger": _clean_text(cliffhanger),
                    "key_actors": [
                        {
                            "name": _clean_text(row.get("agent_name")),
                            "latest_move": _clean_text(row.get("latest_move")),
                            "activity_score": _safe_int(row.get("activity_score")),
                        }
                        for row in featured_rows[:3]
                    ],
                    "world_shift": {
                        "tension_delta": tension_delta,
                        "stability_delta": stability_delta,
                        "ending_tension": round(_safe_float((last.get("world_state") or {}).get("tension"), 0.0), 2),
                        "ending_stability": round(_safe_float((last.get("world_state") or {}).get("stability"), 0.0), 2),
                    },
                }
            )
        return episodes

    @classmethod
    def _build_factions(cls, actor_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        primary = []
        for row in actor_rows[:12]:
            primary.append(
                {
                    "name": _clean_text(row.get("agent_name")),
                    "type": _clean_text(row.get("entity_type")),
                    "activity_score": _safe_int(row.get("activity_score")),
                    "selections": _safe_int(row.get("selections")),
                    "event_count": _safe_int(row.get("event_count")),
                    "accepted_events": _safe_int(row.get("accepted_events")),
                    "completed_events": _safe_int(row.get("completed_events")),
                    "latest_move": _clean_text(row.get("latest_move")),
                    "active_events": row.get("active_events") or [],
                    "queued_event_titles": row.get("queued_event_titles") or [],
                }
            )
        return {"primary": primary}

    @classmethod
    def _build_risks(cls, risk_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "headline": "Next Episode Pressure",
            "items": [
                {
                    "category": _clean_text(item.get("category")),
                    "title": _clean_text(item.get("title")),
                    "summary": _clean_text(item.get("summary")),
                    "severity": round(_safe_float(item.get("severity"), 0.0), 2),
                    "owner": _clean_text(item.get("owner")),
                    "status": _clean_text(item.get("status")),
                }
                for item in risk_items[:8]
            ],
        }

    @classmethod
    def _build_process(cls) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Intent ignition",
                "body": "Actors do not move one by one in a fixed script. Each tick begins with multiple concurrent intents competing to define the next state of the world.",
            },
            {
                "title": "Resolver pressure",
                "body": "Those intents are filtered into accepted, queued, deferred, or rejected moves, which is where the world chooses what actually becomes history.",
            },
            {
                "title": "Event materialization",
                "body": "Accepted moves become events with owners, dependencies, durations, and state impacts, so the world accumulates pressure instead of resetting every round.",
            },
            {
                "title": "World rewrite",
                "body": "Each committed tick updates tension, stability, momentum, and unresolved threads, which is why the next episode starts from a genuinely altered world.",
            },
        ]
