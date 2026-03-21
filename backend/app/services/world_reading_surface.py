"""
Generate higher-level reading surfaces from world runtime artifacts.

Outputs are deterministic summaries over actions/snapshots/checkpoint so they
can be regenerated after every stage or resume without extra LLM cost.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import Config


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="ignore") as f:
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


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dedupe_actions(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        action_args = row.get("action_args") or {}
        key = (
            row.get("event_type"),
            row.get("action_type"),
            _safe_int(row.get("tick") or row.get("round"), 0),
            row.get("agent_id"),
            row.get("event_id") or action_args.get("event_id"),
            action_args.get("intent_id"),
            row.get("status"),
            row.get("title"),
            row.get("summary"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _dedupe_snapshots(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_tick: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        tick = _safe_int(row.get("tick") or row.get("round"), 0)
        if tick <= 0:
            continue
        by_tick[tick] = row
    return [by_tick[tick] for tick in sorted(by_tick)]


def _action_tick(row: Dict[str, Any]) -> int:
    return _safe_int(row.get("tick") or row.get("round"), 0)


def _action_agent_name(row: Dict[str, Any], agent_lookup: Dict[str, Dict[str, Any]]) -> str:
    name = _clean_text(row.get("agent_name"))
    if name:
        return name
    agent_id = row.get("agent_id")
    if agent_id is None:
        return "Unknown"
    return _clean_text((agent_lookup.get(str(agent_id)) or {}).get("entity_name")) or "Unknown"


def _join_titles(events: Iterable[Dict[str, Any]], *, limit: int = 4) -> List[str]:
    titles: List[str] = []
    seen = set()
    for event in events:
        title = _clean_text(event.get("title"))
        if not title or title in seen:
            continue
        titles.append(title)
        seen.add(title)
        if len(titles) >= limit:
            break
    return titles


def _event_risk_score(event: Dict[str, Any]) -> float:
    score = float(_safe_int(event.get("priority"), 0))
    score += 0.5 * len(event.get("dependencies") or [])
    score += 0.25 * _safe_int(event.get("duration_ticks"), 0)
    impacts = event.get("state_impacts") or {}
    if isinstance(impacts, dict):
        score += max(0.0, _safe_float(impacts.get("conflict"), 0.0)) * 4.0
        score += max(0.0, -_safe_float(impacts.get("stability"), 0.0)) * 4.0
        score += max(0.0, -_safe_float(impacts.get("legitimacy"), 0.0)) * 3.0
    return round(score, 2)


def _pressure_risk_items(world_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    tension = _safe_float(world_state.get("tension"), 0.0)
    stability = _safe_float(world_state.get("stability"), 0.0)
    momentum = _safe_float(world_state.get("momentum"), 0.0)
    pressure_tracks = world_state.get("pressure_tracks") or {}
    if tension >= 0.75:
        items.append(
            {
                "category": "world_pressure",
                "severity": round(tension * 10, 2),
                "title": "Global tension remains elevated",
                "summary": f"Tension is {tension:.2f}, indicating the world is still primed for spillover conflict.",
            }
        )
    if stability <= 0.35:
        items.append(
            {
                "category": "world_pressure",
                "severity": round((1.0 - stability) * 10, 2),
                "title": "Institutional stability remains fragile",
                "summary": f"Stability is only {stability:.2f}, so local shocks can still cascade quickly.",
            }
        )
    if momentum >= 0.85:
        items.append(
            {
                "category": "world_pressure",
                "severity": round(momentum * 8, 2),
                "title": "Momentum is still running hot",
                "summary": f"Momentum is {momentum:.2f}, meaning actors still have space to escalate before the world cools down.",
            }
        )
    if isinstance(pressure_tracks, dict):
        for name, value in sorted(pressure_tracks.items()):
            score = _safe_float(value, 0.0)
            if score >= 0.7:
                items.append(
                    {
                        "category": "pressure_track",
                        "severity": round(score * 10, 2),
                        "title": f"Pressure track `{name}` is elevated",
                        "summary": f"`{name}` sits at {score:.2f}, so downstream actions are likely to keep amplifying it.",
                    }
                )
    return items


def generate_reading_surface(
    simulation_id: str,
    label: str,
    *,
    diagnostics_dir: str = "",
    base_name: str = "",
    actions: Optional[List[Dict[str, Any]]] = None,
    snapshots: Optional[List[Dict[str, Any]]] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sim_dir = Path(Config.UPLOAD_FOLDER).resolve() / "simulations" / simulation_id
    world_dir = sim_dir / "world"
    diagnostics_root = Path(diagnostics_dir) if diagnostics_dir else world_dir / "diagnostics"
    diagnostics_root.mkdir(parents=True, exist_ok=True)
    resolved_base_name = base_name or f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{label}"

    config_payload = config or _load_json(sim_dir / "simulation_config.json")
    checkpoint_payload = checkpoint or _load_json(world_dir / "checkpoint.json")
    action_rows = _dedupe_actions(actions if actions is not None else _load_jsonl(world_dir / "actions.jsonl"))
    snapshot_rows = _dedupe_snapshots(snapshots if snapshots is not None else _load_jsonl(world_dir / "state_snapshots.jsonl"))

    agent_lookup = {
        str(item.get("agent_id")): item
        for item in (config_payload.get("agent_configs") or [])
        if isinstance(item, dict) and item.get("agent_id") is not None
    }

    chronicle_entries = _build_chronicle_entries(snapshot_rows, action_rows, agent_lookup)
    actor_board = _build_actor_board(checkpoint_payload, action_rows, config_payload, agent_lookup)
    risk_digest = _build_risk_digest(checkpoint_payload, action_rows)

    chronicle_json_path = diagnostics_root / f"{resolved_base_name}-chronicle.json"
    chronicle_md_path = diagnostics_root / f"{resolved_base_name}-chronicle.md"
    actor_board_json_path = diagnostics_root / f"{resolved_base_name}-actor-board.json"
    actor_board_md_path = diagnostics_root / f"{resolved_base_name}-actor-board.md"
    risk_digest_json_path = diagnostics_root / f"{resolved_base_name}-risk-digest.json"
    risk_digest_md_path = diagnostics_root / f"{resolved_base_name}-risk-digest.md"

    chronicle_json_path.write_text(json.dumps(chronicle_entries, ensure_ascii=False, indent=2), encoding="utf-8")
    chronicle_md_path.write_text(_chronicle_markdown(simulation_id, label, chronicle_entries), encoding="utf-8")
    actor_board_json_path.write_text(json.dumps(actor_board, ensure_ascii=False, indent=2), encoding="utf-8")
    actor_board_md_path.write_text(_actor_board_markdown(simulation_id, label, actor_board), encoding="utf-8")
    risk_digest_json_path.write_text(json.dumps(risk_digest, ensure_ascii=False, indent=2), encoding="utf-8")
    risk_digest_md_path.write_text(_risk_digest_markdown(simulation_id, label, risk_digest), encoding="utf-8")

    return {
        "simulation_id": simulation_id,
        "label": label,
        "generated_at": datetime.now().isoformat(),
        "chronicle": {
            "tick_count": len(chronicle_entries),
            "latest_tick": chronicle_entries[-1]["tick"] if chronicle_entries else 0,
            "json_path": str(chronicle_json_path),
            "markdown_path": str(chronicle_md_path),
        },
        "actor_board": {
            "row_count": len(actor_board.get("actors") or []),
            "json_path": str(actor_board_json_path),
            "markdown_path": str(actor_board_md_path),
        },
        "risk_digest": {
            "item_count": len(risk_digest.get("items") or []),
            "json_path": str(risk_digest_json_path),
            "markdown_path": str(risk_digest_md_path),
        },
    }


def _build_chronicle_entries(
    snapshots: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    agent_lookup: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    actions_by_tick: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in actions:
        tick = _action_tick(row)
        if tick > 0:
            actions_by_tick[tick].append(row)

    entries: List[Dict[str, Any]] = []
    for snapshot in snapshots:
        tick = _safe_int(snapshot.get("tick") or snapshot.get("round"), 0)
        if tick <= 0:
            continue
        tick_actions = actions_by_tick.get(tick, [])
        accepted_titles = _join_titles(
            row for row in tick_actions if _clean_text(row.get("status")).lower() == "accepted"
        )
        completed_titles = _join_titles(
            row for row in tick_actions if _clean_text(row.get("event_type")).lower() == "event_completed"
        )
        started_titles = _join_titles(
            row for row in tick_actions if _clean_text(row.get("event_type")).lower() in {"event_started", "event_queued"}
        )
        actor_counts = Counter(_action_agent_name(row, agent_lookup) for row in tick_actions if _action_agent_name(row, agent_lookup) != "Unknown")
        key_actors = [name for name, _ in actor_counts.most_common(4)]
        world_state = snapshot.get("world_state") or {}
        entries.append(
            {
                "tick": tick,
                "scene_title": _clean_text(snapshot.get("scene_title")) or f"Tick {tick}",
                "summary": _clean_text(snapshot.get("summary")),
                "key_actors": key_actors,
                "accepted_titles": accepted_titles,
                "completed_titles": completed_titles or _join_titles(snapshot.get("recent_completed_events") or []),
                "started_titles": started_titles,
                "metrics": snapshot.get("metrics") or {},
                "world_state": {
                    "tension": _safe_float(world_state.get("tension"), 0.0),
                    "stability": _safe_float(world_state.get("stability"), 0.0),
                    "momentum": _safe_float(world_state.get("momentum"), 0.0),
                    "pressure_tracks": world_state.get("pressure_tracks") or {},
                },
            }
        )
    return entries


def _build_actor_board(
    checkpoint: Dict[str, Any],
    actions: List[Dict[str, Any]],
    config: Dict[str, Any],
    agent_lookup: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    actor_selection_counts = checkpoint.get("actor_selection_counts") or {}
    actor_event_counts = checkpoint.get("actor_event_counts") or {}
    rows_by_name: Dict[str, Dict[str, Any]] = {}

    def ensure_row(agent_name: str, agent_id: Optional[Any] = None) -> Dict[str, Any]:
        normalized_name = _clean_text(agent_name) or "Unknown"
        if normalized_name in rows_by_name:
            return rows_by_name[normalized_name]
        lookup = agent_lookup.get(str(agent_id)) if agent_id is not None else None
        row = {
            "agent_name": normalized_name,
            "agent_id": lookup.get("agent_id") if isinstance(lookup, dict) else agent_id,
            "entity_type": _clean_text((lookup or {}).get("entity_type")) or "Unknown",
            "selections": 0,
            "event_count": 0,
            "intents_created": 0,
            "accepted_events": 0,
            "queued_events": 0,
            "completed_events": 0,
            "active_events": [],
            "queued_event_titles": [],
            "latest_move": "",
            "last_tick": 0,
        }
        rows_by_name[normalized_name] = row
        return row

    for agent in config.get("agent_configs") or []:
        if not isinstance(agent, dict):
            continue
        ensure_row(agent.get("entity_name"), agent.get("agent_id"))

    for key, value in _coerce_counter_mapping(actor_selection_counts).items():
        lookup_agent_id = key if str(key) in agent_lookup else None
        row = ensure_row(_counter_actor_name(key, agent_lookup), lookup_agent_id)
        row["selections"] = _safe_int(value)
    for key, value in _coerce_counter_mapping(actor_event_counts).items():
        lookup_agent_id = key if str(key) in agent_lookup else None
        row = ensure_row(_counter_actor_name(key, agent_lookup), lookup_agent_id)
        row["event_count"] = _safe_int(value)

    for row in actions:
        agent_name = _action_agent_name(row, agent_lookup)
        board_row = ensure_row(agent_name, row.get("agent_id"))
        board_row["last_tick"] = max(board_row["last_tick"], _action_tick(row))
        if _clean_text(row.get("event_type")).lower() == "intent_created":
            board_row["intents_created"] += 1
            board_row["latest_move"] = _clean_text(row.get("summary") or row.get("title")) or board_row["latest_move"]
        if _clean_text(row.get("status")).lower() == "accepted":
            board_row["accepted_events"] += 1
            board_row["latest_move"] = _clean_text(row.get("title") or row.get("summary")) or board_row["latest_move"]
        if _clean_text(row.get("event_type")).lower() == "event_queued":
            board_row["queued_events"] += 1
            title = _clean_text(row.get("title"))
            if title and title not in board_row["queued_event_titles"]:
                board_row["queued_event_titles"].append(title)
        if _clean_text(row.get("event_type")).lower() == "event_completed":
            board_row["completed_events"] += 1

    for event in checkpoint.get("active_events") or []:
        row = ensure_row(event.get("primary_agent_name"), event.get("primary_agent_id"))
        title = _clean_text(event.get("title"))
        if title and title not in row["active_events"]:
            row["active_events"].append(title)
    for event in checkpoint.get("queued_events") or []:
        row = ensure_row(event.get("primary_agent_name"), event.get("primary_agent_id"))
        title = _clean_text(event.get("title"))
        if title and title not in row["queued_event_titles"]:
            row["queued_event_titles"].append(title)

    rows = list(rows_by_name.values())
    for row in rows:
        row["activity_score"] = (
            row["event_count"]
            + row["accepted_events"]
            + row["completed_events"]
            + len(row["active_events"])
            + len(row["queued_event_titles"])
        )
        row["active_events"] = row["active_events"][:4]
        row["queued_event_titles"] = row["queued_event_titles"][:4]
    rows.sort(key=lambda item: (-item["activity_score"], -item["selections"], item["agent_name"]))
    return {
        "generated_at": datetime.now().isoformat(),
        "actors": rows,
    }


def _coerce_counter_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _counter_actor_name(key: Any, agent_lookup: Dict[str, Dict[str, Any]]) -> str:
    normalized = str(key)
    if normalized in agent_lookup:
        return _clean_text((agent_lookup.get(normalized) or {}).get("entity_name")) or normalized
    return _clean_text(key) or "Unknown"


def _build_risk_digest(checkpoint: Dict[str, Any], actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    unresolved_events = _find_unresolved_events(actions) or list(checkpoint.get("active_events") or []) + list(checkpoint.get("queued_events") or [])
    items: List[Dict[str, Any]] = []
    for event in unresolved_events:
        if not isinstance(event, dict):
            continue
        items.append(
            {
                "category": "unresolved_event",
                "severity": _event_risk_score(event),
                "title": _clean_text(event.get("title")) or "Unnamed unresolved event",
                "summary": _clean_text(event.get("summary")),
                "owner": _clean_text(event.get("primary_agent_name")) or _clean_text(event.get("agent_name")),
                "status": _clean_text(event.get("status")) or "unknown",
                "dependencies": event.get("dependencies") or [],
                "location": _clean_text(event.get("location")),
                "state_impacts": event.get("state_impacts") or {},
            }
        )

    world_state = checkpoint.get("world_state") or (checkpoint.get("last_snapshot") or {}).get("world_state") or {}
    items.extend(_pressure_risk_items(world_state))

    zero_accept_count = sum(
        1
        for row in actions
        if _clean_text(row.get("event_type")).lower() == "resolver_zero_accept_diagnostic"
    )
    if zero_accept_count > 0:
        items.append(
            {
                "category": "operational",
                "severity": round(2.5 + zero_accept_count * 0.4, 2),
                "title": "Resolver produced zero-accept diagnostics",
                "summary": f"The run recorded {zero_accept_count} zero-accept resolver diagnostics, so some world pressure may still be under-materialized.",
            }
        )

    category_priority = {
        "unresolved_event": 0,
        "operational": 1,
        "world_pressure": 2,
        "pressure_track": 3,
    }
    items.sort(
        key=lambda item: (
            category_priority.get(str(item.get("category") or ""), 99),
            -_safe_float(item.get("severity"), 0.0),
            item.get("title", ""),
        )
    )
    return {
        "generated_at": datetime.now().isoformat(),
        "world_state": {
            "tension": _safe_float(world_state.get("tension"), 0.0),
            "stability": _safe_float(world_state.get("stability"), 0.0),
            "momentum": _safe_float(world_state.get("momentum"), 0.0),
            "pressure_tracks": world_state.get("pressure_tracks") or {},
        },
        "items": items,
    }


def _find_unresolved_events(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for row in reversed(actions):
        if _clean_text(row.get("event_type")).lower() != "simulation_end":
            continue
        unresolved = row.get("unresolved_events")
        if isinstance(unresolved, list):
            return [item for item in unresolved if isinstance(item, dict)]
    return []


def _chronicle_markdown(simulation_id: str, label: str, entries: List[Dict[str, Any]]) -> str:
    lines = [
        f"# World Chronicle: {label}",
        "",
        f"- Simulation: `{simulation_id}`",
        f"- Ticks Covered: `{len(entries)}`",
        "",
    ]
    for entry in entries:
        world_state = entry.get("world_state") or {}
        lines.append(f"## Tick {entry['tick']}: {entry.get('scene_title') or 'Untitled'}")
        lines.append("")
        if entry.get("summary"):
            lines.append(entry["summary"])
            lines.append("")
        lines.append(
            f"- Tension / Stability / Momentum: `{world_state.get('tension')}` / `{world_state.get('stability')}` / `{world_state.get('momentum')}`"
        )
        if entry.get("key_actors"):
            lines.append(f"- Key Actors: {', '.join(entry['key_actors'])}")
        if entry.get("accepted_titles"):
            lines.append(f"- Accepted Moves: {', '.join(entry['accepted_titles'])}")
        if entry.get("completed_titles"):
            lines.append(f"- Completed Events: {', '.join(entry['completed_titles'])}")
        if entry.get("started_titles"):
            lines.append(f"- Opened Threads: {', '.join(entry['started_titles'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _actor_board_markdown(simulation_id: str, label: str, actor_board: Dict[str, Any]) -> str:
    actors = actor_board.get("actors") or []
    lines = [
        f"# Actor Board: {label}",
        "",
        f"- Simulation: `{simulation_id}`",
        f"- Actors Tracked: `{len(actors)}`",
        "",
        "| Actor | Type | Select | Events | Active / Queued | Latest Move |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for row in actors[:24]:
        open_threads = ", ".join(row.get("active_events") or row.get("queued_event_titles") or []) or "-"
        latest_move = _clean_text(row.get("latest_move")) or "-"
        lines.append(
            f"| {row['agent_name']} | {row['entity_type']} | {row['selections']} | {row['event_count']} | {open_threads} | {latest_move} |"
        )
    if len(actors) > 24:
        lines.extend(["", f"- Remaining actors not shown: `{len(actors) - 24}`"])
    return "\n".join(lines).rstrip() + "\n"


def _risk_digest_markdown(simulation_id: str, label: str, risk_digest: Dict[str, Any]) -> str:
    items = risk_digest.get("items") or []
    world_state = risk_digest.get("world_state") or {}
    lines = [
        f"# Unresolved Risk Digest: {label}",
        "",
        f"- Simulation: `{simulation_id}`",
        f"- Tension / Stability / Momentum: `{world_state.get('tension')}` / `{world_state.get('stability')}` / `{world_state.get('momentum')}`",
        f"- Risk Items: `{len(items)}`",
        "",
    ]
    for item in items[:20]:
        lines.append(
            f"- [{item.get('category')}] `{item.get('severity')}` {item.get('title')}: {item.get('summary')}"
        )
    if len(items) > 20:
        lines.append(f"- Additional items not shown: `{len(items) - 20}`")
    return "\n".join(lines).rstrip() + "\n"
