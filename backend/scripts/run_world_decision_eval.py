#!/usr/bin/env python3
"""
Run a decision-grade world eval with targeted shortlists and parallel per-case execution.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
EVAL_SCRIPT = SCRIPT_DIR / "eval_world_models.py"
DEFAULT_CONFIG = BACKEND_DIR / "evals" / "world_decision_eval_config.json"
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


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    label: str
    selector: str


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slugify(value: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in value.lower())
    return slug.strip("-") or "case"


def build_suite(
    *,
    name: str,
    description: str,
    score_profile: str,
    max_rounds: int,
    repeat_count: int,
    runtime_overrides: Dict[str, Any],
    cases: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "score_profile": score_profile,
        "shared_max_rounds": max_rounds,
        "shared_repeat_count": repeat_count,
        "shared_rewrite_agent_selectors": True,
        "shared_runtime_overrides": runtime_overrides,
        "cases": list(cases),
    }


def case_payload(
    *,
    case_id: str,
    label: str,
    actor_selector: str,
    resolver_selector: str,
    notes: str,
) -> Dict[str, Any]:
    return {
        "case_id": case_id,
        "label": label,
        "actor_selector": actor_selector,
        "resolver_selector": resolver_selector,
        "notes": notes,
    }


def candidate_list(items: Sequence[Dict[str, Any]]) -> List[Candidate]:
    return [
        Candidate(
            candidate_id=str(item["candidate_id"]),
            label=str(item["label"]),
            selector=str(item["selector"]),
        )
        for item in items
    ]


def run_case(
    *,
    python_bin: str,
    base_config: Path,
    suite_path: Path,
    stage_dir: Path,
    case_id: str,
) -> Dict[str, Any]:
    case_slug = slugify(case_id)
    case_root = ensure_dir(stage_dir / "raw" / case_slug)
    log_path = ensure_dir(stage_dir / "logs") / f"{case_slug}.log"
    env = os.environ.copy()
    for key in DEFAULT_PROXY_VARS:
        env.pop(key, None)

    command = [
        python_bin,
        str(EVAL_SCRIPT),
        "--base-config",
        str(base_config),
        "--suite-config",
        str(suite_path),
        "--output-root",
        str(case_root),
        "--python-bin",
        python_bin,
        "--cases",
        case_id,
    ]
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    combined_output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    log_path.write_text(combined_output, encoding="utf-8")

    if completed.returncode != 0:
        return {
            "case_id": case_id,
            "error": combined_output.strip()[-2000:] or f"exit={completed.returncode}",
        }

    output_line = (completed.stdout or "").strip().splitlines()
    if not output_line:
        return {
            "case_id": case_id,
            "error": "missing output directory from eval_world_models.py",
        }
    output_dir = Path(output_line[-1]).resolve()
    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        return {
            "case_id": case_id,
            "error": f"missing summary.json: {summary_path}",
        }

    summary = load_json(summary_path)
    results = summary.get("results") or []
    if not results:
        return {
            "case_id": case_id,
            "error": f"empty results: {summary_path}",
        }

    item = results[0]
    item["stage_output_dir"] = str(output_dir)
    item["stage_log_path"] = str(log_path)
    return item


def run_stage(
    *,
    stage_name: str,
    suite: Dict[str, Any],
    stage_dir: Path,
    base_config: Path,
    python_bin: str,
    max_parallel: int,
) -> Dict[str, Any]:
    ensure_dir(stage_dir)
    suite_path = stage_dir / "suite.json"
    write_json(suite_path, suite)

    cases = suite.get("cases") or []
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    progress_path = stage_dir / "stage_summary.json"

    print(
        f"[{datetime.now().isoformat(timespec='seconds')}] stage={stage_name} start cases={len(cases)}",
        flush=True,
    )

    def persist_progress() -> None:
        ordered = sorted(results, key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
        payload = {
            "stage": stage_name,
            "suite_path": str(suite_path),
            "results": ordered,
            "failures": sorted(failures, key=lambda item: item.get("case_id", "")),
        }
        write_json(progress_path, payload)

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        future_map = {
            executor.submit(
                run_case,
                python_bin=python_bin,
                base_config=base_config,
                suite_path=suite_path,
                stage_dir=stage_dir,
                case_id=str(item["case_id"]),
            ): str(item["case_id"])
            for item in cases
        }
        for future in as_completed(future_map):
            payload = future.result()
            if payload.get("error"):
                failures.append(payload)
                print(
                    f"[{datetime.now().isoformat(timespec='seconds')}] stage={stage_name} case={payload.get('case_id')} status=failed",
                    flush=True,
                )
            else:
                results.append(payload)
                print(
                    f"[{datetime.now().isoformat(timespec='seconds')}] stage={stage_name} case={payload.get('case_id')} "
                    f"overall={payload.get('scores', {}).get('overall')}",
                    flush=True,
                )
            persist_progress()

    summary = load_json(progress_path)
    print(
        f"[{datetime.now().isoformat(timespec='seconds')}] stage={stage_name} completed ok={len(results)} failed={len(failures)}",
        flush=True,
    )
    return summary


def pick_results_by_metric(results: Sequence[Dict[str, Any]], metric: str) -> List[Dict[str, Any]]:
    return sorted(results, key=lambda item: item.get("scores", {}).get(metric, 0.0) or 0.0, reverse=True)


def select_diverse_results(
    results: Sequence[Dict[str, Any]],
    overall_count: int,
    metric_winners: Sequence[str],
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for item in pick_results_by_metric(results, "overall"):
        key = str(item.get("case_id"))
        if key in seen:
            continue
        selected.append(item)
        seen.add(key)
        if len(selected) >= overall_count:
            break

    for metric in metric_winners:
        for item in pick_results_by_metric(results, metric):
            key = str(item.get("case_id"))
            if key in seen:
                continue
            selected.append(item)
            seen.add(key)
            break

    return selected


def build_actor_cases(candidates: Sequence[Candidate], resolver_selector: str) -> List[Dict[str, Any]]:
    return [
        case_payload(
            case_id=f"actor-{candidate.candidate_id}",
            label=f"Actor Smoke / {candidate.label}",
            actor_selector=candidate.selector,
            resolver_selector=resolver_selector,
            notes="Actor smoke with fixed balanced GPT resolver baseline.",
        )
        for candidate in candidates
    ]


def build_resolver_cases(candidates: Sequence[Candidate], actor_selector: str) -> List[Dict[str, Any]]:
    return [
        case_payload(
            case_id=f"resolver-{candidate.candidate_id}",
            label=f"Resolver Smoke / {candidate.label}",
            actor_selector=actor_selector,
            resolver_selector=candidate.selector,
            notes="Resolver smoke with winning actor baseline.",
        )
        for candidate in candidates
    ]


def build_combo_cases(
    actor_results: Sequence[Dict[str, Any]],
    resolver_results: Sequence[Dict[str, Any]],
    combo_limit: int,
) -> List[Dict[str, Any]]:
    combos: List[Dict[str, Any]] = []
    for actor in actor_results:
        for resolver in resolver_results:
            combos.append(
                {
                    "rank_hint": (actor.get("scores", {}).get("overall", 0.0) or 0.0)
                    + (resolver.get("scores", {}).get("overall", 0.0) or 0.0),
                    "case": case_payload(
                        case_id=f"combo-{slugify(actor['case_id'])}-{slugify(resolver['case_id'])}",
                        label=f"Progression / {actor['label']} + {resolver['label']}",
                        actor_selector=actor["actor_selector"],
                        resolver_selector=resolver["resolver_selector"],
                        notes=f"actor_source={actor['case_id']} resolver_source={resolver['case_id']}",
                    ),
                }
            )
    ordered = sorted(combos, key=lambda item: item["rank_hint"], reverse=True)
    return [item["case"] for item in ordered[:combo_limit]]


def selector_family(item: Dict[str, Any]) -> str:
    joined = f"{item.get('actor_selector', '')} {item.get('resolver_selector', '')}".lower()
    if "gpt" in joined:
        return "gpt"
    if "claude" in joined:
        return "claude"
    if "qwen" in joined:
        return "qwen"
    if "kimi" in joined:
        return "kimi"
    if "minimax" in joined:
        return "minimax"
    if "gemini" in joined:
        return "gemini"
    return "other"


def pick_best_non_premium(results: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    filtered = [
        item
        for item in results
        if selector_family(item) not in {"gpt", "claude"}
    ]
    if not filtered:
        return None
    return pick_results_by_metric(filtered, "overall")[0]


def recommendation_block(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not item:
        return {}
    return {
        "case_id": item.get("case_id"),
        "label": item.get("label"),
        "actor_selector": item.get("actor_selector"),
        "resolver_selector": item.get("resolver_selector"),
        "scores": item.get("scores", {}),
        "timing": item.get("timing", {}),
        "events": item.get("events", {}),
        "concurrency": item.get("concurrency", {}),
        "quality": item.get("quality", {}),
        "diagnostics": item.get("diagnostics", {}),
        "score_spread": item.get("score_spread", {}),
        "stage_output_dir": item.get("stage_output_dir"),
        "stage_log_path": item.get("stage_log_path"),
    }


def render_stage(title: str, stage: Dict[str, Any]) -> List[str]:
    lines = [f"## {title}", ""]
    for index, item in enumerate(stage.get("results", []), start=1):
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
    if stage.get("failures"):
        lines.append("")
        lines.append("Failures:")
        for item in stage["failures"]:
            lines.append(f"- `{item['case_id']}`: {item['error']}")
    lines.append("")
    return lines


def build_report(summary: Dict[str, Any]) -> str:
    recommendations = summary["recommendations"]
    lines = [
        "# World Decision Eval",
        "",
        summary["description"],
        "",
        f"- Output dir: `{summary['output_dir']}`",
        f"- Base config: `{summary['base_config']}`",
        "",
    ]
    lines.extend(render_stage("Actor Smoke", summary["stages"]["actor_smoke"]))
    lines.extend(render_stage("Resolver Smoke", summary["stages"]["resolver_smoke"]))
    lines.extend(render_stage("Progression", summary["stages"]["progression"]))
    lines.extend(render_stage("Final Repeats", summary["stages"]["final_repeats"]))
    lines.extend(
        [
            "## Recommendations",
            "",
            f"- Default: `{recommendations['default'].get('label', 'n/a')}`",
            f"- Fastest viable: `{recommendations['fastest_viable'].get('label', 'n/a')}`",
            f"- Most stable: `{recommendations['most_stable'].get('label', 'n/a')}`",
            f"- Lower-cost family: `{recommendations['lower_cost_family'].get('label', 'n/a')}`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a targeted parallel world decision eval.")
    parser.add_argument("--base-config", required=True, help="Path to world simulation_config.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Decision eval config JSON")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Output root directory")
    parser.add_argument("--python-bin", default=sys.executable, help="Python interpreter for eval runs")
    args = parser.parse_args()

    base_config = Path(args.base_config).resolve()
    config = load_json(Path(args.config).resolve())
    output_root = ensure_dir(Path(args.output_root).resolve())
    run_dir = ensure_dir(output_root / f"world-decision-eval-{now_stamp()}")
    write_json(run_dir / "config_snapshot.json", config)

    max_parallel = max(1, int(config.get("execution", {}).get("max_parallel", 3)))

    actor_cfg = config["actor_smoke"]
    actor_suite = build_suite(
        name="world-decision-actor-smoke",
        description=str(actor_cfg["description"]),
        score_profile=str(actor_cfg["score_profile"]),
        max_rounds=int(actor_cfg["shared_max_rounds"]),
        repeat_count=int(actor_cfg["shared_repeat_count"]),
        runtime_overrides=dict(actor_cfg["runtime_overrides"]),
        cases=build_actor_cases(
            candidate_list(actor_cfg["candidates"]),
            resolver_selector=str(actor_cfg["baseline_resolver_selector"]),
        ),
    )
    actor_stage = run_stage(
        stage_name="actor_smoke",
        suite=actor_suite,
        stage_dir=run_dir / "actor_smoke",
        base_config=base_config,
        python_bin=args.python_bin,
        max_parallel=max_parallel,
    )
    if not actor_stage["results"]:
        raise RuntimeError("Actor smoke produced no successful results")

    actor_winner = actor_stage["results"][0]

    resolver_cfg = config["resolver_smoke"]
    resolver_suite = build_suite(
        name="world-decision-resolver-smoke",
        description=str(resolver_cfg["description"]),
        score_profile=str(resolver_cfg["score_profile"]),
        max_rounds=int(resolver_cfg["shared_max_rounds"]),
        repeat_count=int(resolver_cfg["shared_repeat_count"]),
        runtime_overrides=dict(resolver_cfg["runtime_overrides"]),
        cases=build_resolver_cases(
            candidate_list(resolver_cfg["candidates"]),
            actor_selector=str(actor_winner["actor_selector"]),
        ),
    )
    resolver_stage = run_stage(
        stage_name="resolver_smoke",
        suite=resolver_suite,
        stage_dir=run_dir / "resolver_smoke",
        base_config=base_config,
        python_bin=args.python_bin,
        max_parallel=max_parallel,
    )
    if not resolver_stage["results"]:
        raise RuntimeError("Resolver smoke produced no successful results")

    progression_cfg = config["progression"]
    selected_actors = select_diverse_results(
        actor_stage["results"],
        overall_count=int(progression_cfg["top_actor_overall_count"]),
        metric_winners=progression_cfg.get("top_actor_metric_winners", []),
    )
    selected_resolvers = select_diverse_results(
        resolver_stage["results"],
        overall_count=int(progression_cfg["top_resolver_overall_count"]),
        metric_winners=progression_cfg.get("top_resolver_metric_winners", []),
    )
    progression_suite = build_suite(
        name="world-decision-progression",
        description=str(progression_cfg["description"]),
        score_profile=str(progression_cfg["score_profile"]),
        max_rounds=int(progression_cfg["shared_max_rounds"]),
        repeat_count=int(progression_cfg["shared_repeat_count"]),
        runtime_overrides=dict(progression_cfg["runtime_overrides"]),
        cases=build_combo_cases(
            selected_actors,
            selected_resolvers,
            combo_limit=int(progression_cfg["combo_limit"]),
        ),
    )
    progression_stage = run_stage(
        stage_name="progression",
        suite=progression_suite,
        stage_dir=run_dir / "progression",
        base_config=base_config,
        python_bin=args.python_bin,
        max_parallel=max_parallel,
    )
    if not progression_stage["results"]:
        raise RuntimeError("Progression stage produced no successful results")

    final_cfg = config["final_repeats"]
    top_progression = progression_stage["results"][: int(final_cfg["top_combo_count"])]
    final_suite = build_suite(
        name="world-decision-final-repeats",
        description=str(final_cfg["description"]),
        score_profile=str(progression_cfg["score_profile"]),
        max_rounds=int(progression_cfg["shared_max_rounds"]),
        repeat_count=int(final_cfg["shared_repeat_count"]),
        runtime_overrides=dict(progression_cfg["runtime_overrides"]),
        cases=[
            case_payload(
                case_id=item["case_id"],
                label=item["label"],
                actor_selector=item["actor_selector"],
                resolver_selector=item["resolver_selector"],
                notes=f"source_progression_case={item['case_id']}",
            )
            for item in top_progression
        ],
    )
    final_stage = run_stage(
        stage_name="final_repeats",
        suite=final_suite,
        stage_dir=run_dir / "final_repeats",
        base_config=base_config,
        python_bin=args.python_bin,
        max_parallel=max_parallel,
    )
    if not final_stage["results"]:
        raise RuntimeError("Final repeat stage produced no successful results")

    default = final_stage["results"][0]
    fastest_viable = pick_results_by_metric(final_stage["results"], "speed")[0]
    most_stable = sorted(
        final_stage["results"],
        key=lambda item: (
            -(item.get("scores", {}).get("resilience", 0.0) or 0.0),
            item.get("diagnostics", {}).get("salvage_tick_rate") or 0.0,
            -(item.get("scores", {}).get("overall", 0.0) or 0.0),
        ),
    )[0]
    lower_cost_family = pick_best_non_premium(final_stage["results"]) or pick_best_non_premium(progression_stage["results"])

    summary = {
        "generated_at": datetime.now().isoformat(),
        "description": config.get("description", ""),
        "base_config": str(base_config),
        "output_dir": str(run_dir),
        "stages": {
            "actor_smoke": actor_stage,
            "resolver_smoke": resolver_stage,
            "progression": progression_stage,
            "final_repeats": final_stage,
        },
        "recommendations": {
            "default": recommendation_block(default),
            "fastest_viable": recommendation_block(fastest_viable),
            "most_stable": recommendation_block(most_stable),
            "lower_cost_family": recommendation_block(lower_cost_family),
        },
    }
    write_json(run_dir / "decision_summary.json", summary)
    (run_dir / "decision_report.md").write_text(build_report(summary), encoding="utf-8")
    print(str(run_dir))


if __name__ == "__main__":
    main()
