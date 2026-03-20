#!/usr/bin/env python3
"""
Replay captured world resolver traces through the current parser.

This is a targeted regression tool for debugging resolver schema drift.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)
sys.path.insert(0, _project_root)

from app.utils.llm_client import LLMClient
from scripts.run_world_simulation import ActorIntent, WorldSimulationRuntime, ensure_list


def _intent_from_payload(payload: Dict[str, Any]) -> ActorIntent:
    return ActorIntent(
        intent_id=str(payload.get("intent_id", "")).strip(),
        tick=int(payload.get("tick", 0) or 0),
        agent_id=int(payload.get("agent_id", 0) or 0),
        agent_name=str(payload.get("agent_name", "")).strip(),
        objective=str(payload.get("objective", "")).strip(),
        summary=str(payload.get("summary", "")).strip(),
        location=str(payload.get("location", "") or "").strip(),
        target=(str(payload.get("target", "")).strip() or None),
        desired_duration=int(payload.get("desired_duration", 1) or 1),
        priority=int(payload.get("priority", 3) or 3),
        urgency=int(payload.get("urgency", 3) or 3),
        risk_level=int(payload.get("risk_level", 3) or 3),
        dependencies=ensure_list(payload.get("dependencies")),
        participants=ensure_list(payload.get("participants")),
        tags=ensure_list(payload.get("tags")),
        state_impacts=dict(payload.get("state_impacts") or {}),
        rationale=str(payload.get("rationale", "") or "").strip(),
        source=str(payload.get("source", "llm") or "llm").strip(),
    )


def _load_trace_payload(trace_path: str) -> Dict[str, Any]:
    with open(trace_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_intents(trace_payload: Dict[str, Any]) -> List[ActorIntent]:
    messages = trace_payload.get("request", {}).get("messages", [])
    user_content = next(
        (
            message.get("content", "")
            for message in messages
            if message.get("role") == "user"
        ),
        "",
    )
    request_payload = json.loads(user_content or "{}")
    return [
        _intent_from_payload(item)
        for item in request_payload.get("intents", []) or []
        if isinstance(item, dict)
    ]


def replay_trace(runtime: WorldSimulationRuntime, trace_path: str) -> Dict[str, Any]:
    trace_payload = _load_trace_payload(trace_path)
    intents = _extract_intents(trace_payload)
    response_meta = trace_payload.get("response", {}) or {}
    response_payload = response_meta.get("parsed_json") or {}
    if not response_payload:
        for key in ("raw_response", "json_candidate", "repaired_response", "repaired_candidate"):
            candidate = LLMClient._extract_json_candidate(str(response_meta.get(key) or ""))
            if not candidate:
                continue
            try:
                response_payload = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
    if isinstance(response_payload, dict):
        raw_response = str(response_meta.get("raw_response") or "")
        repaired_response = str(response_meta.get("repaired_response") or "")
        if raw_response:
            response_payload["_raw_response"] = raw_response
        if repaired_response:
            response_payload["_repaired_response"] = repaired_response
    tick = int(trace_payload.get("tick", 0) or 0)

    parsed = runtime._parse_resolver_response(
        tick=tick,
        intents=intents,
        response=response_payload,
        source="llm",
    )

    return {
        "trace_path": trace_path,
        "tick": tick,
        "accepted_event_count": len(parsed.get("accepted_events", [])),
        "accepted_event_titles": [
            event.title for event in parsed.get("accepted_events", [])
        ],
        "accepted_event_sources": [
            event.source_intent_ids for event in parsed.get("accepted_events", [])
        ],
        "deferred_map": parsed.get("deferred_map", {}),
        "rejected_map": parsed.get("rejected_map", {}),
        "diagnostics": parsed.get("diagnostics", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay world resolver traces")
    parser.add_argument("--config", required=True, help="Path to simulation_config.json")
    parser.add_argument(
        "--trace",
        required=True,
        action="append",
        help="Path to a captured resolver trace JSON. Pass multiple times for multiple traces.",
    )
    args = parser.parse_args()

    runtime = WorldSimulationRuntime(config_path=args.config, max_rounds=1)
    results = [replay_trace(runtime, trace_path) for trace_path in args.trace]
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
