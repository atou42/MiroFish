#!/usr/bin/env python3
"""
Run an end-to-end world-model strategy evaluation.

Stages:
1. Preflight probes candidate selectors for health + JSON compliance
2. Actor smoke sweep with a fixed resolver
3. Resolver smoke sweep with the winning actor
4. Progression finalist combo sweep
5. Repeated finals for stability
6. Final strategy report
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
EVAL_WORLD_SCRIPT = Path(__file__).resolve().with_name("eval_world_models.py")
DEFAULT_CONFIG_PATH = BACKEND_DIR / "evals" / "world_strategy_eval_config.json"
DEFAULT_OUTPUT_ROOT = BACKEND_DIR / "uploads" / "evals"
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

from app.utils.llm_client import InvalidJSONResponseError, LLMClient


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    label: str
    selector: str
    roles: tuple[str, ...]
    family: str
    allow_health_only: bool = False
    notes: str = ""


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slugify(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-") or "case"


def strip_proxy_env() -> None:
    for key in DEFAULT_PROXY_VARS:
        os.environ.pop(key, None)


def pick_results_by_metric(
    results: Sequence[Dict[str, Any]],
    metric: str,
    reverse: bool = True,
) -> List[Dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: item.get("scores", {}).get(metric, 0.0) or 0.0,
        reverse=reverse,
    )


def select_diverse_results(
    results: Sequence[Dict[str, Any]],
    overall_count: int,
    metric_winners: Sequence[str],
    max_total: Optional[int] = None,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for item in pick_results_by_metric(results, "overall"):
        key = item.get("case_id")
        if key in seen:
            continue
        selected.append(item)
        seen.add(key)
        if len(selected) >= overall_count:
            break

    for metric in metric_winners:
        ordered = pick_results_by_metric(results, metric)
        for item in ordered:
            key = item.get("case_id")
            if key in seen:
                continue
            selected.append(item)
            seen.add(key)
            break

    if max_total is not None:
        return selected[:max_total]
    return selected


def candidate_from_payload(item: Dict[str, Any]) -> Candidate:
    return Candidate(
        candidate_id=str(item["candidate_id"]),
        label=str(item["label"]),
        selector=str(item["selector"]),
        roles=tuple(str(role) for role in item.get("roles", [])),
        family=str(item.get("family") or "unknown"),
        allow_health_only=bool(item.get("allow_health_only", False)),
        notes=str(item.get("notes") or ""),
    )


def probe_candidate(candidate: Candidate, preflight: Dict[str, Any]) -> Dict[str, Any]:
    strip_proxy_env()
    row: Dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "label": candidate.label,
        "selector": candidate.selector,
        "family": candidate.family,
        "roles": list(candidate.roles),
        "allow_health_only": candidate.allow_health_only,
        "notes": candidate.notes,
    }

    started = time.time()
    try:
        client = LLMClient.from_selector(candidate.selector)
        row["provider_id"] = client.provider_id
        row["model"] = client.model

        health_started = time.time()
        client.health_check(timeout=float(preflight.get("health_timeout_s", 20)))
        row["health_ok"] = True
        row["health_s"] = round(time.time() - health_started, 2)

        json_started = time.time()
        payload = client.chat_json(
            [
                {"role": "system", "content": str(preflight["json_probe_system"])},
                {"role": "user", "content": str(preflight["json_probe_user"])},
            ],
            temperature=0.0,
            max_tokens=120,
            timeout=float(preflight.get("json_timeout_s", 30)),
        )
        row["json_ok"] = isinstance(payload, dict) and payload.get("ok") in (True, "true", "True", 1)
        row["json_s"] = round(time.time() - json_started, 2)
        row["json_payload"] = payload
    except InvalidJSONResponseError as exc:
        row["health_ok"] = row.get("health_ok", True)
        row["json_ok"] = False
        row["error"] = f"InvalidJSONResponseError: {exc}"
        row["raw_response"] = exc.raw_response[:400]
        row["repaired_response"] = exc.repaired_response[:400]
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"

    row["total_s"] = round(time.time() - started, 2)
    row["usable"] = bool(row.get("health_ok")) and (bool(row.get("json_ok")) or candidate.allow_health_only)
    if row["usable"] and not row.get("json_ok"):
        row["probe_status"] = "health_only"
    elif row["usable"]:
        row["probe_status"] = "json_ok"
    else:
        row["probe_status"] = "rejected"
    return row


def build_suite_payload(
    name: str,
    description: str,
    score_profile: str,
    shared_max_rounds: int,
    shared_repeat_count: int,
    shared_runtime_overrides: Dict[str, Any],
    cases: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "score_profile": score_profile,
        "shared_max_rounds": shared_max_rounds,
        "shared_repeat_count": shared_repeat_count,
        "shared_rewrite_agent_selectors": True,
        "shared_runtime_overrides": shared_runtime_overrides,
        "cases": cases,
    }


def write_stage_suite(path: Path, payload: Dict[str, Any]) -> None:
    write_json(path, payload)


def run_eval_suite(
    python_bin: str,
    base_config: Path,
    suite_config: Path,
    output_root: Path,
    log_path: Path,
) -> Path:
    command = [
        python_bin,
        str(EVAL_WORLD_SCRIPT),
        "--base-config",
        str(base_config),
        "--suite-config",
        str(suite_config),
        "--output-root",
        str(output_root),
        "--python-bin",
        python_bin,
    ]
    env = os.environ.copy()
    for key in DEFAULT_PROXY_VARS:
        env.pop(key, None)

    result = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    log_path.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"Stage eval failed: {result.stdout[-1200:]}")

    output_dir = Path(result.stdout.strip().splitlines()[-1]).resolve()
    if not output_dir.exists():
        raise RuntimeError(f"Eval output directory missing: {output_dir}")
    return output_dir


def successful_stage_results(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = summary.get("results") or []
    return [item for item in results if item.get("failed_repeats", 1) < item.get("repeat_count", 1)]


def case_payload(
    case_id: str,
    label: str,
    actor_selector: str,
    resolver_selector: str,
    notes: str = "",
) -> Dict[str, Any]:
    return {
        "case_id": case_id,
        "label": label,
        "actor_selector": actor_selector,
        "resolver_selector": resolver_selector,
        "notes": notes,
    }


def build_actor_smoke_cases(candidates: Sequence[Candidate], baseline_resolver: str) -> List[Dict[str, Any]]:
    cases = []
    for candidate in candidates:
        cases.append(
            case_payload(
                case_id=f"actor-{candidate.candidate_id}",
                label=f"Actor Sweep | {candidate.label}",
                actor_selector=candidate.selector,
                resolver_selector=baseline_resolver,
                notes=candidate.notes,
            )
        )
    return cases


def build_resolver_smoke_cases(candidates: Sequence[Candidate], baseline_actor: str) -> List[Dict[str, Any]]:
    cases = []
    for candidate in candidates:
        cases.append(
            case_payload(
                case_id=f"resolver-{candidate.candidate_id}",
                label=f"Resolver Sweep | {candidate.label}",
                actor_selector=baseline_actor,
                resolver_selector=candidate.selector,
                notes=candidate.notes,
            )
        )
    return cases


def build_progression_cases(
    actor_results: Sequence[Dict[str, Any]],
    resolver_results: Sequence[Dict[str, Any]],
    combo_limit: int,
) -> List[Dict[str, Any]]:
    combos: List[Dict[str, Any]] = []
    for actor in actor_results:
        for resolver in resolver_results:
            actor_selector = actor["actor_selector"]
            resolver_selector = resolver["resolver_selector"]
            combos.append(
                {
                    "rank_hint": (actor.get("scores", {}).get("overall", 0.0) or 0.0)
                    + (resolver.get("scores", {}).get("overall", 0.0) or 0.0),
                    "case": case_payload(
                        case_id=f"combo-{slugify(actor['case_id'])}-{slugify(resolver['case_id'])}",
                        label=f"Progression | {actor['label']} + {resolver['label']}",
                        actor_selector=actor_selector,
                        resolver_selector=resolver_selector,
                        notes=(
                            f"actor_source={actor['case_id']}; resolver_source={resolver['case_id']}"
                        ),
                    ),
                }
            )

    ordered = sorted(combos, key=lambda item: item["rank_hint"], reverse=True)
    return [item["case"] for item in ordered[:combo_limit]]


def best_by_metric(results: Sequence[Dict[str, Any]], metric: str) -> Optional[Dict[str, Any]]:
    ordered = pick_results_by_metric(results, metric)
    return ordered[0] if ordered else None


def find_case_by_id(results: Sequence[Dict[str, Any]], case_id: str) -> Optional[Dict[str, Any]]:
    for item in results:
        if item.get("case_id") == case_id:
            return item
    return None


def selector_family(item: Dict[str, Any]) -> str:
    actor_selector = str(item.get("actor_selector") or "").lower()
    resolver_selector = str(item.get("resolver_selector") or "").lower()
    joined = f"{actor_selector} {resolver_selector}"
    if "gpt" in joined:
        return "gpt"
    if "claude" in joined:
        return "claude"
    if "kimi" in joined:
        return "kimi"
    if "minimax" in joined:
        return "minimax"
    if "qwen" in joined:
        return "qwen"
    return "other"


def pick_best_non_gpt(results: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    filtered = [item for item in results if selector_family(item) not in {"gpt", "claude"}]
    return best_by_metric(filtered, "overall") if filtered else None


def render_preflight_markdown(rows: Sequence[Dict[str, Any]]) -> str:
    lines = [
        "# Preflight",
        "",
        "| Candidate | Selector | Status | Health | JSON | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| "
            f"{row['label']} | `{row['selector']}` | `{row.get('probe_status')}` | "
            f"`{row.get('health_s', 'n/a')}` | `{row.get('json_s', 'n/a')}` | "
            f"{(row.get('error') or row.get('notes') or '').replace('|', '/')} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_stage_ranking(title: str, results: Sequence[Dict[str, Any]]) -> str:
    lines = [f"## {title}", ""]
    ordered = sorted(results, key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    for index, item in enumerate(ordered, start=1):
        scores = item.get("scores", {})
        timing = item.get("timing", {})
        diagnostics = item.get("diagnostics", {})
        lines.append(
            f"{index}. `{item['label']}` overall=`{scores.get('overall')}` "
            f"speed=`{scores.get('speed')}` progression=`{scores.get('progression')}` "
            f"resilience=`{scores.get('resilience')}` cleanliness=`{scores.get('cleanliness')}` "
            f"first_event=`{timing.get('tick_start_to_first_event_s')}`s "
            f"salvage_tick_rate=`{diagnostics.get('salvage_tick_rate')}`"
        )
    lines.append("")
    return "\n".join(lines)


def render_strategy_report(payload: Dict[str, Any]) -> str:
    recommendations = payload["recommendations"]
    lines = [
        "# World Strategy Eval",
        "",
        payload["description"],
        "",
        f"- Master output: `{payload['output_dir']}`",
        "",
        render_preflight_markdown(payload["preflight_results"]),
        render_stage_ranking("Actor Smoke", payload["actor_smoke"]["summary"]["results"]),
        render_stage_ranking("Resolver Smoke", payload["resolver_smoke"]["summary"]["results"]),
        render_stage_ranking("Progression Finals", payload["progression"]["summary"]["results"]),
        render_stage_ranking("Repeated Finals", payload["final_repeats"]["summary"]["results"]),
        "## Recommendations",
        "",
    ]

    for key, label in (
        ("default", "Default"),
        ("speed_first", "Speed First"),
        ("stability_first", "Stability First"),
        ("non_gpt", "Non-GPT / Lower-Cost Family"),
    ):
        item = recommendations.get(key)
        if not item:
            continue
        lines.append(
            f"- {label}: `{item['label']}` "
            f"actor=`{item['actor_selector']}` resolver=`{item['resolver_selector']}` "
            f"overall=`{item['scores']['overall']}` speed=`{item['scores']['speed']}` "
            f"progression=`{item['scores']['progression']}` resilience=`{item['scores']['resilience']}` "
            f"cleanliness=`{item['scores']['cleanliness']}`"
        )

    lines.extend(
        [
            "",
            "## Excluded",
            "",
        ]
    )
    for row in payload["preflight_results"]:
        if row.get("usable"):
            continue
        lines.append(f"- `{row['label']}` excluded: {row.get('error') or row.get('probe_status')}")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full world model strategy eval.")
    parser.add_argument("--base-config", required=True, help="Path to base world simulation_config.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Strategy eval config JSON")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Output root directory")
    parser.add_argument("--python-bin", default=sys.executable, help="Python interpreter for stage evals")
    args = parser.parse_args()

    base_config = Path(args.base_config).resolve()
    strategy_config = load_json(Path(args.config).resolve())
    output_root = ensure_dir(Path(args.output_root).resolve())
    master_dir = ensure_dir(output_root / f"world-strategy-eval-{now_stamp()}")
    write_json(master_dir / "strategy_config_snapshot.json", strategy_config)

    candidates = [candidate_from_payload(item) for item in strategy_config.get("candidates", [])]
    preflight_config = strategy_config.get("preflight", {})
    preflight_results = [probe_candidate(candidate, preflight_config) for candidate in candidates]
    write_json(master_dir / "preflight.json", {"results": preflight_results})

    actor_candidates = [
        candidate
        for candidate in candidates
        if "actor" in candidate.roles
        and next((row for row in preflight_results if row["candidate_id"] == candidate.candidate_id and row.get("usable")), None)
    ]
    resolver_candidates = [
        candidate
        for candidate in candidates
        if "resolver" in candidate.roles
        and next((row for row in preflight_results if row["candidate_id"] == candidate.candidate_id and row.get("usable")), None)
    ]

    if not actor_candidates:
        raise RuntimeError("No actor candidates survived preflight")
    if not resolver_candidates:
        raise RuntimeError("No resolver candidates survived preflight")

    actor_stage = strategy_config["actor_smoke"]
    actor_suite = build_suite_payload(
        name="world-actor-smoke",
        description=str(actor_stage["description"]),
        score_profile=str(actor_stage["score_profile"]),
        shared_max_rounds=int(actor_stage["shared_max_rounds"]),
        shared_repeat_count=int(actor_stage["shared_repeat_count"]),
        shared_runtime_overrides=dict(actor_stage["shared_runtime_overrides"]),
        cases=build_actor_smoke_cases(actor_candidates, str(actor_stage["baseline_resolver_selector"])),
    )
    actor_suite_path = master_dir / "actor_smoke_suite.json"
    write_stage_suite(actor_suite_path, actor_suite)
    actor_output_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=actor_suite_path,
        output_root=master_dir / "actor_smoke",
        log_path=master_dir / "actor_smoke.log",
    )
    actor_summary = load_json(actor_output_dir / "summary.json")
    actor_results = successful_stage_results(actor_summary)
    if not actor_results:
        raise RuntimeError("Actor smoke produced no successful results")

    actor_winner = best_by_metric(actor_results, "overall")
    assert actor_winner is not None

    resolver_stage = strategy_config["resolver_smoke"]
    resolver_suite = build_suite_payload(
        name="world-resolver-smoke",
        description=str(resolver_stage["description"]),
        score_profile=str(resolver_stage["score_profile"]),
        shared_max_rounds=int(resolver_stage["shared_max_rounds"]),
        shared_repeat_count=int(resolver_stage["shared_repeat_count"]),
        shared_runtime_overrides=dict(resolver_stage["shared_runtime_overrides"]),
        cases=build_resolver_smoke_cases(resolver_candidates, str(actor_winner["actor_selector"])),
    )
    resolver_suite_path = master_dir / "resolver_smoke_suite.json"
    write_stage_suite(resolver_suite_path, resolver_suite)
    resolver_output_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=resolver_suite_path,
        output_root=master_dir / "resolver_smoke",
        log_path=master_dir / "resolver_smoke.log",
    )
    resolver_summary = load_json(resolver_output_dir / "summary.json")
    resolver_results = successful_stage_results(resolver_summary)
    if not resolver_results:
        raise RuntimeError("Resolver smoke produced no successful results")

    progression_stage = strategy_config["progression"]
    selected_actor_results = select_diverse_results(
        actor_results,
        overall_count=int(progression_stage["top_actor_overall_count"]),
        metric_winners=progression_stage.get("top_actor_metric_winners", []),
    )
    selected_resolver_results = select_diverse_results(
        resolver_results,
        overall_count=int(progression_stage["top_resolver_overall_count"]),
        metric_winners=progression_stage.get("top_resolver_metric_winners", []),
    )
    progression_cases = build_progression_cases(
        selected_actor_results,
        selected_resolver_results,
        combo_limit=int(progression_stage["combo_limit"]),
    )
    progression_suite = build_suite_payload(
        name="world-progression-finals",
        description=str(progression_stage["description"]),
        score_profile=str(progression_stage["score_profile"]),
        shared_max_rounds=int(progression_stage["shared_max_rounds"]),
        shared_repeat_count=int(progression_stage["shared_repeat_count"]),
        shared_runtime_overrides=dict(progression_stage["shared_runtime_overrides"]),
        cases=progression_cases,
    )
    progression_suite_path = master_dir / "progression_suite.json"
    write_stage_suite(progression_suite_path, progression_suite)
    progression_output_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=progression_suite_path,
        output_root=master_dir / "progression",
        log_path=master_dir / "progression.log",
    )
    progression_summary = load_json(progression_output_dir / "summary.json")
    progression_results = successful_stage_results(progression_summary)
    if not progression_results:
        raise RuntimeError("Progression stage produced no successful results")

    final_stage = strategy_config["final_repeats"]
    repeated_targets = pick_results_by_metric(progression_results, "overall")[: int(final_stage["top_combo_count"])]
    final_suite_cases = [
        case_payload(
            case_id=item["case_id"],
            label=item["label"],
            actor_selector=item["actor_selector"],
            resolver_selector=item["resolver_selector"],
            notes=f"source_progression_case={item['case_id']}",
        )
        for item in repeated_targets
    ]
    final_suite = build_suite_payload(
        name="world-final-repeats",
        description=str(final_stage["description"]),
        score_profile=str(progression_stage["score_profile"]),
        shared_max_rounds=int(progression_stage["shared_max_rounds"]),
        shared_repeat_count=int(final_stage["repeat_count"]),
        shared_runtime_overrides=dict(progression_stage["shared_runtime_overrides"]),
        cases=final_suite_cases,
    )
    final_suite_path = master_dir / "final_repeats_suite.json"
    write_stage_suite(final_suite_path, final_suite)
    final_output_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=final_suite_path,
        output_root=master_dir / "final_repeats",
        log_path=master_dir / "final_repeats.log",
    )
    final_summary = load_json(final_output_dir / "summary.json")
    final_results = successful_stage_results(final_summary)
    if not final_results:
        raise RuntimeError("Final repeats stage produced no successful results")

    default_reco = best_by_metric(final_results, "overall")
    speed_reco = best_by_metric(final_results, "speed")
    stability_reco = best_by_metric(final_results, "resilience")
    non_gpt_reco = pick_best_non_gpt(final_results) or pick_best_non_gpt(progression_results)

    summary_payload = {
        "generated_at": datetime.now().isoformat(),
        "description": strategy_config.get("description", ""),
        "output_dir": str(master_dir),
        "preflight_results": preflight_results,
        "actor_smoke": {
            "suite_path": str(actor_suite_path),
            "output_dir": str(actor_output_dir),
            "summary": actor_summary,
        },
        "resolver_smoke": {
            "suite_path": str(resolver_suite_path),
            "output_dir": str(resolver_output_dir),
            "summary": resolver_summary,
        },
        "progression": {
            "suite_path": str(progression_suite_path),
            "output_dir": str(progression_output_dir),
            "summary": progression_summary,
        },
        "final_repeats": {
            "suite_path": str(final_suite_path),
            "output_dir": str(final_output_dir),
            "summary": final_summary,
        },
        "recommendations": {
            "default": default_reco,
            "speed_first": speed_reco,
            "stability_first": stability_reco,
            "non_gpt": non_gpt_reco,
        },
    }
    write_json(master_dir / "strategy_summary.json", summary_payload)
    report = render_strategy_report(summary_payload)
    (master_dir / "strategy_report.md").write_text(report, encoding="utf-8")
    print(str(master_dir))


if __name__ == "__main__":
    main()
