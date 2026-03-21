#!/usr/bin/env python3
"""
Summarize world-mode simulation runs into JSON and Markdown diagnostics.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Tuple

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)
sys.path.insert(0, _project_root)

from app.config import Config
from app.services.world_reading_surface import generate_reading_surface


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _latest_trace_run_dir(world_dir: str) -> str:
    root = os.path.join(world_dir, "debug", "llm_traces")
    if not os.path.isdir(root):
        return ""
    dirs = [
        os.path.join(root, item)
        for item in os.listdir(root)
        if os.path.isdir(os.path.join(root, item))
    ]
    return sorted(dirs)[-1] if dirs else ""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _collect_diagnostics(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    event_type_counts = Counter()
    action_type_counts = Counter()
    intent_source_counts = Counter()
    resolution_event_source_counts = Counter()
    resolution_status_counts = Counter()
    resolver_reason_counts = Counter()
    resolver_cluster_reason_counts = Counter()
    accepted_intent_ids = set()
    deferred_intent_ids = set()
    rejected_intent_ids = set()
    latest_runtime_config: Dict[str, Any] = {}
    latest_world_models: Dict[str, Any] = {}
    tick_end_rows: List[Dict[str, Any]] = []
    stage_notes: List[Dict[str, Any]] = []

    for event in events:
        event_type = str(event.get("event_type", "") or "")
        action_type = str(event.get("action_type", "") or "")
        if event_type:
            event_type_counts[event_type] += 1
        if action_type:
            action_type_counts[action_type] += 1

        if event_type == "simulation_start":
            latest_runtime_config = dict(event.get("runtime_config") or {})
            latest_world_models = dict(event.get("world_models") or {})

        if event_type == "tick_end":
            tick_end_rows.append(event)

        if event_type == "resolver_zero_accept_diagnostic":
            reason_code = str(event.get("reason_code", "") or "unknown")
            resolver_reason_counts[reason_code] += 1
            stage_notes.append(
                {
                    "tick": event.get("tick"),
                    "event_type": event_type,
                    "reason_code": reason_code,
                    "summary": event.get("summary", ""),
                }
            )
        elif event_type == "resolver_cluster_zero_accept":
            reason_code = str(event.get("reason_code", "") or "unknown")
            resolver_cluster_reason_counts[reason_code] += 1

        if event_type == "intent_created":
            source = str(event.get("action_args", {}).get("source", "") or "unknown")
            intent_source_counts[source] += 1

        if event_type == "intent_resolved":
            status = str(event.get("status", "") or "unknown")
            resolution_status_counts[status] += 1
            intent_id = str(event.get("action_args", {}).get("intent_id", "") or "")
            if status == "accepted":
                if intent_id:
                    accepted_intent_ids.add(intent_id)
                event_source = str(
                    event.get("action_args", {}).get("event", {}).get("source", "") or "unknown"
                )
                resolution_event_source_counts[event_source] += 1
            elif status == "deferred" and intent_id:
                deferred_intent_ids.add(intent_id)
            elif status == "rejected" and intent_id:
                rejected_intent_ids.add(intent_id)

    last_tick_end = tick_end_rows[-1] if tick_end_rows else {}
    world_state = dict(last_tick_end.get("world_state") or {})

    return {
        "event_type_counts": dict(event_type_counts),
        "action_type_counts": dict(action_type_counts),
        "intent_source_counts": dict(intent_source_counts),
        "resolution_event_source_counts": dict(resolution_event_source_counts),
        "resolution_status_counts": dict(resolution_status_counts),
        "resolver_reason_counts": dict(resolver_reason_counts),
        "resolver_cluster_reason_counts": dict(resolver_cluster_reason_counts),
        "accepted_intent_count": len(accepted_intent_ids),
        "deferred_intent_count": len(deferred_intent_ids),
        "rejected_intent_count": len(rejected_intent_ids),
        "latest_runtime_config": latest_runtime_config,
        "latest_world_models": latest_world_models,
        "last_tick_end": last_tick_end,
        "world_state": world_state,
        "stage_notes": stage_notes[-12:],
    }


def _build_markdown(summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# World Run Diagnostics: {summary['label']}")
    lines.append("")
    lines.append(f"- Simulation: `{summary['simulation_id']}`")
    lines.append(f"- Generated At: `{summary['generated_at']}`")
    lines.append(f"- Checkpoint Status: `{summary['checkpoint'].get('status', 'unknown')}`")
    lines.append(f"- Last Completed Tick: `{summary['checkpoint'].get('last_completed_tick', 0)}`")
    lines.append(f"- Total Actions Log Rows: `{summary['actions_rows']}`")
    lines.append(f"- Latest Trace Run: `{summary['latest_trace_run_dir'] or 'none'}`")
    lines.append("")

    runtime = summary["diagnostics"].get("latest_runtime_config", {})
    models = summary["diagnostics"].get("latest_world_models", {})
    if runtime or models:
        lines.append("## Runtime")
        lines.append("")
        if models:
            lines.append(f"- Actor Profile: `{models.get('agent_profile', '')}`")
            lines.append(f"- Resolver Profile: `{models.get('resolver_profile', '')}`")
        if runtime:
            lines.append(f"- Intent Agents Per Tick: `{runtime.get('intent_agents_per_tick')}`")
            lines.append(f"- Resolver Cluster Concurrency: `{runtime.get('resolver_cluster_concurrency')}`")
            lines.append(f"- Provider Request Timeout: `{runtime.get('provider_request_timeout')}`")
        lines.append("")

    diag = summary["diagnostics"]
    lines.append("## Key Stats")
    lines.append("")
    lines.append(f"- Accepted Intents: `{diag.get('accepted_intent_count', 0)}`")
    lines.append(f"- Deferred Intents: `{diag.get('deferred_intent_count', 0)}`")
    lines.append(f"- Rejected Intents: `{diag.get('rejected_intent_count', 0)}`")
    lines.append(f"- Resolver Salvaged: `{diag.get('event_type_counts', {}).get('resolver_salvaged', 0)}`")
    lines.append(f"- Resolver Zero-Accept Diagnostics: `{diag.get('event_type_counts', {}).get('resolver_zero_accept_diagnostic', 0)}`")
    lines.append("")

    if diag.get("intent_source_counts"):
        lines.append("## Intent Sources")
        lines.append("")
        for key, value in sorted(diag["intent_source_counts"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: {value}")
        lines.append("")

    if diag.get("resolution_event_source_counts"):
        lines.append("## Accepted Event Sources")
        lines.append("")
        for key, value in sorted(diag["resolution_event_source_counts"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: {value}")
        lines.append("")

    if diag.get("resolver_reason_counts"):
        lines.append("## Resolver Zero-Accept Reasons")
        lines.append("")
        for key, value in sorted(diag["resolver_reason_counts"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{key}`: {value}")
        lines.append("")

    world_state = diag.get("world_state", {})
    if world_state:
        lines.append("## World State")
        lines.append("")
        lines.append(f"- Tension: `{world_state.get('tension')}`")
        lines.append(f"- Stability: `{world_state.get('stability')}`")
        lines.append(f"- Momentum: `{world_state.get('momentum')}`")
        if world_state.get("last_tick_summary"):
            lines.append(f"- Last Tick Summary: {world_state.get('last_tick_summary')}")
        lines.append("")

    if diag.get("stage_notes"):
        lines.append("## Recent Notes")
        lines.append("")
        for item in diag["stage_notes"]:
            lines.append(
                f"- Tick {item.get('tick')}: `{item.get('reason_code')}` {item.get('summary', '')}"
            )
        lines.append("")

    reading_surface = summary.get("reading_surface") or {}
    if reading_surface:
        lines.append("## Reading Surfaces")
        lines.append("")
        chronicle = reading_surface.get("chronicle") or {}
        actor_board = reading_surface.get("actor_board") or {}
        risk_digest = reading_surface.get("risk_digest") or {}
        lines.append(f"- Chronicle: `{chronicle.get('markdown_path', '')}`")
        lines.append(f"- Actor Board: `{actor_board.get('markdown_path', '')}`")
        lines.append(f"- Risk Digest: `{risk_digest.get('markdown_path', '')}`")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def validate_summary(summary: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not os.path.exists(str(summary.get("json_path") or "")):
        errors.append("diagnostics json_path missing")
    if not os.path.exists(str(summary.get("markdown_path") or "")):
        errors.append("diagnostics markdown_path missing")
    if _safe_int(summary.get("actions_rows"), 0) <= 0:
        errors.append("actions_rows must be > 0")

    checkpoint = summary.get("checkpoint") or {}
    checkpoint_status = str(checkpoint.get("status") or "").strip().lower()
    if checkpoint_status not in {"running", "completed", "restored", "failed", "interrupted"}:
        errors.append(f"unexpected checkpoint.status: {checkpoint.get('status')}")
    if _safe_int(checkpoint.get("last_completed_tick"), 0) <= 0:
        errors.append("checkpoint.last_completed_tick must be > 0")

    diagnostics = summary.get("diagnostics") or {}
    if not isinstance(diagnostics.get("last_tick_end"), dict) or not diagnostics.get("last_tick_end"):
        errors.append("diagnostics.last_tick_end missing")
    if not isinstance(diagnostics.get("world_state"), dict) or not diagnostics.get("world_state"):
        errors.append("diagnostics.world_state missing")

    reading_surface = summary.get("reading_surface") or {}
    for key in ("chronicle", "actor_board", "risk_digest"):
        section = reading_surface.get(key) or {}
        markdown_path = str(section.get("markdown_path") or "")
        json_path = str(section.get("json_path") or "")
        if not markdown_path or not os.path.exists(markdown_path):
            errors.append(f"reading_surface.{key}.markdown_path missing")
        if not json_path or not os.path.exists(json_path):
            errors.append(f"reading_surface.{key}.json_path missing")
    return errors


def summarize(simulation_id: str, label: str) -> Dict[str, Any]:
    sim_dir = os.path.join(Config.UPLOAD_FOLDER, "simulations", simulation_id)
    world_dir = os.path.join(sim_dir, "world")
    actions_path = os.path.join(world_dir, "actions.jsonl")
    checkpoint_path = os.path.join(world_dir, "checkpoint.json")
    diagnostics_dir = os.path.join(world_dir, "diagnostics")
    os.makedirs(diagnostics_dir, exist_ok=True)

    actions = _load_jsonl(actions_path)
    checkpoint = _load_json(checkpoint_path)
    diagnostics = _collect_diagnostics(actions)
    generated_at = datetime.now().isoformat()
    latest_trace_run_dir = _latest_trace_run_dir(world_dir)
    summary = {
        "simulation_id": simulation_id,
        "label": label,
        "generated_at": generated_at,
        "actions_rows": len(actions),
        "checkpoint": checkpoint,
        "latest_trace_run_dir": latest_trace_run_dir,
        "diagnostics": diagnostics,
    }

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    base_name = f"{stamp}-{label}"
    json_path = os.path.join(diagnostics_dir, f"{base_name}.json")
    md_path = os.path.join(diagnostics_dir, f"{base_name}.md")
    summary["reading_surface"] = generate_reading_surface(
        simulation_id=simulation_id,
        label=label,
        diagnostics_dir=diagnostics_dir,
        base_name=base_name,
        actions=actions,
        checkpoint=checkpoint,
    )
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_build_markdown(summary))
    summary["json_path"] = os.path.abspath(json_path)
    summary["markdown_path"] = os.path.abspath(md_path)
    summary["validation_errors"] = validate_summary(summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize world simulation diagnostics")
    parser.add_argument("--simulation-id", required=True)
    parser.add_argument("--label", required=True, help="stage1, final, or another run label")
    args = parser.parse_args()

    summary = summarize(simulation_id=args.simulation_id, label=args.label)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
