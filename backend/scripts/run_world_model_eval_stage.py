#!/usr/bin/env python3
"""
Run one world-model eval stage at a time.

This script is designed for long-running evals where the full campaign should be
broken into resumable checkpoints.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.run_world_model_eval_campaign import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT,
    candidate_selector,
    choose_pair_finalists,
    choose_probe_baseline,
    clear_config_caches,
    ensure_dir,
    load_json,
    now_stamp,
    recommendation_block,
    run_candidate_probe_bundle,
    run_eval_stage,
    select_group_winners,
    select_top_results,
    sort_probe_results,
    stage_case,
    summarize_ranking,
    write_json,
    write_probe_snapshots,
    build_actor_probe_payload,
    build_report,
    build_resolver_probe_payload,
    build_suite,
    build_temp_registry,
)


DEFAULT_CAMPAIGN_CONFIG = BACKEND_DIR / "evals" / "world_model_eval_campaign.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a single stage of the world-model eval campaign.")
    parser.add_argument(
        "--stage",
        required=True,
        choices=("probe", "actor_smoke", "resolver_smoke", "pair_smoke", "progression", "stability", "finalize"),
        help="Stage to execute.",
    )
    parser.add_argument("--base-config", required=True, help="Path to a world simulation_config.json")
    parser.add_argument("--campaign-config", default=str(DEFAULT_CAMPAIGN_CONFIG), help="Campaign config JSON")
    parser.add_argument("--campaign-dir", help="Existing campaign dir. Required for all stages except probe.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Root dir for new campaign outputs")
    parser.add_argument("--python-bin", default=sys.executable, help="Python interpreter used for stage runs")
    return parser.parse_args()


def resolve_campaign_dir(args: argparse.Namespace) -> Path:
    if args.campaign_dir:
        return ensure_dir(Path(args.campaign_dir).resolve())
    if args.stage != "probe":
        raise ValueError("--campaign-dir is required for this stage")
    output_root = ensure_dir(Path(args.output_root).resolve())
    return ensure_dir(output_root / f"world-model-campaign-stage-{now_stamp()}")


def stage_lock_path(campaign_dir: Path, stage: str) -> Path:
    return campaign_dir / f".stage-lock-{stage}.json"


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_stage_lock(campaign_dir: Path, stage: str) -> Path:
    lock_path = stage_lock_path(campaign_dir, stage)
    if lock_path.exists():
        existing_pid = 0
        try:
            payload = load_json(lock_path)
            existing_pid = int(payload.get("pid") or 0)
        except Exception:
            existing_pid = 0
        if process_alive(existing_pid):
            raise RuntimeError(f"Stage '{stage}' already running with pid={existing_pid}")
    write_json(lock_path, {"pid": os.getpid(), "stage": stage})
    return lock_path


def release_stage_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def ensure_registry(campaign_dir: Path, campaign_config: Dict[str, Any]) -> Path:
    registry_path = campaign_dir / "temp_llm_registry.json"
    if registry_path.exists():
        return registry_path
    return build_temp_registry(campaign_dir, campaign_config["candidates"])


def load_stage_summary(campaign_dir: Path, filename: str) -> Dict[str, Any]:
    path = campaign_dir / filename
    if not path.exists():
        if filename == "probe_summary.json":
            legacy_probe_path = campaign_dir / "probe" / "summary.json"
            if legacy_probe_path.exists():
                return load_json(legacy_probe_path)
        raise FileNotFoundError(f"Missing required stage summary: {path}")
    return load_json(path)


def run_probe_stage(
    campaign_dir: Path,
    base_config_path: Path,
    campaign_config: Dict[str, Any],
) -> Tuple[Path, Dict[str, Any]]:
    candidates = campaign_config["candidates"]
    probe_dir = ensure_dir(campaign_dir / "probe")
    registry_path = ensure_registry(campaign_dir, campaign_config)
    clear_config_caches(registry_path)

    base_config = load_json(base_config_path)
    actor_payload = build_actor_probe_payload(base_config)
    resolver_payload = build_resolver_probe_payload(base_config)
    probe_cfg = campaign_config["probe"]
    probe_max_workers = max(1, int(probe_cfg.get("max_workers", min(4, max(len(candidates), 1)))))

    availability_results: List[Dict[str, Any]] = []
    actor_probe_results: List[Dict[str, Any]] = []
    resolver_probe_results: List[Dict[str, Any]] = []

    print(f"[probe] starting {len(candidates)} candidates with max_workers={probe_max_workers}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=probe_max_workers) as executor:
        future_map = {
            executor.submit(run_candidate_probe_bundle, candidate, actor_payload, resolver_payload, probe_cfg): candidate
            for candidate in candidates
        }
        completed_count = 0
        for future in concurrent.futures.as_completed(future_map):
            candidate = future_map[future]
            availability_item, actor_item, resolver_item = future.result()
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
    write_json(campaign_dir / "probe_summary.json", probe_summary)
    print(str(probe_dir / "summary.json"), flush=True)
    return probe_dir, probe_summary


def run_actor_smoke_stage(
    campaign_dir: Path,
    base_config_path: Path,
    campaign_config: Dict[str, Any],
    python_bin: str,
) -> Tuple[Path, Dict[str, Any], Dict[str, Any]]:
    probe_summary = load_stage_summary(campaign_dir, "probe_summary.json")
    registry_path = ensure_registry(campaign_dir, campaign_config)
    clear_config_caches(registry_path)
    smoke_case_workers = max(1, int(campaign_config.get("smoke_case_workers", 1)))

    fallback_resolver_candidate = next(item for item in campaign_config["candidates"] if item["id"] == "litellm_gpt54_balanced")
    baseline_resolver_probe = choose_probe_baseline(probe_summary["resolver_group_winners"], fallback_resolver_candidate)

    cases = [
        stage_case(
            case_id=f"actor-{item['id']}",
            label=f"Actor Smoke / {item['label']}",
            actor_selector=item["selector"],
            resolver_selector=baseline_resolver_probe["selector"],
            notes=f"Actor candidate with fixed resolver baseline {baseline_resolver_probe['label']}.",
        )
        for item in probe_summary["selected_actor_probes"]
    ]
    suite = build_suite(
        name="campaign-actor-smoke-dynamic-baseline",
        description="Actor-side world smoke with the strongest probe resolver baseline.",
        score_profile="latency_smoke",
        shared_max_rounds=1,
        shared_repeat_count=1,
        runtime_overrides=campaign_config["smoke_runtime_overrides"],
        cases=cases,
    )
    stage_dir = ensure_dir(campaign_dir / "actor_smoke_dynamic")
    output_dir, summary = run_eval_stage(
        stage_dir,
        suite,
        base_config_path,
        python_bin,
        registry_path,
        case_workers=smoke_case_workers,
    )
    write_json(campaign_dir / "actor_smoke_dynamic_summary.json", summary)
    write_json(campaign_dir / "actor_smoke_dynamic_baseline.json", baseline_resolver_probe)
    print(str(output_dir), flush=True)
    return output_dir, summary, baseline_resolver_probe


def run_resolver_smoke_stage(
    campaign_dir: Path,
    base_config_path: Path,
    campaign_config: Dict[str, Any],
    python_bin: str,
) -> Tuple[Path, Dict[str, Any], Dict[str, Any]]:
    probe_summary = load_stage_summary(campaign_dir, "probe_summary.json")
    actor_smoke_summary_path = campaign_dir / "actor_smoke_dynamic_summary.json"
    registry_path = ensure_registry(campaign_dir, campaign_config)
    clear_config_caches(registry_path)
    smoke_case_workers = max(1, int(campaign_config.get("smoke_case_workers", 1)))

    fallback_actor_candidate = next(item for item in campaign_config["candidates"] if item["id"] == "litellm_gpt54_fast")
    if actor_smoke_summary_path.exists():
        actor_smoke_summary = load_json(actor_smoke_summary_path)
        ranked_actor_results = sorted(
            actor_smoke_summary.get("results", []),
            key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0,
            reverse=True,
        )
        top_actor_result = ranked_actor_results[0] if ranked_actor_results else {}
        baseline_actor_probe = {
            "id": top_actor_result.get("case_id", "").removeprefix("actor-") or fallback_actor_candidate["id"],
            "label": top_actor_result.get("label") or fallback_actor_candidate["label"],
            "selector": top_actor_result.get("actor_selector") or candidate_selector(fallback_actor_candidate),
            "score": (top_actor_result.get("scores") or {}).get("overall"),
            "latency_s": (top_actor_result.get("timing") or {}).get("tick_start_to_first_event_s"),
            "ok": bool(top_actor_result),
            "source_stage": "actor_smoke",
        }
    else:
        baseline_actor_probe = choose_probe_baseline(probe_summary["actor_group_winners"], fallback_actor_candidate)

    cases = [
        stage_case(
            case_id=f"resolver-{item['id']}",
            label=f"Resolver Smoke / {item['label']}",
            actor_selector=baseline_actor_probe["selector"],
            resolver_selector=item["selector"],
            notes=f"Resolver candidate with fixed actor baseline {baseline_actor_probe['label']}.",
        )
        for item in probe_summary["selected_resolver_probes"]
    ]
    suite = build_suite(
        name="campaign-resolver-smoke-dynamic-baseline",
        description="Resolver-side world smoke with the strongest probe actor baseline.",
        score_profile="latency_smoke",
        shared_max_rounds=1,
        shared_repeat_count=1,
        runtime_overrides=campaign_config["smoke_runtime_overrides"],
        cases=cases,
    )
    stage_dir = ensure_dir(campaign_dir / "resolver_smoke_dynamic")
    output_dir, summary = run_eval_stage(
        stage_dir,
        suite,
        base_config_path,
        python_bin,
        registry_path,
        case_workers=smoke_case_workers,
    )
    write_json(campaign_dir / "resolver_smoke_dynamic_summary.json", summary)
    write_json(campaign_dir / "resolver_smoke_dynamic_baseline.json", baseline_actor_probe)
    print(str(output_dir), flush=True)
    return output_dir, summary, baseline_actor_probe


def run_pair_smoke_stage(
    campaign_dir: Path,
    base_config_path: Path,
    campaign_config: Dict[str, Any],
    python_bin: str,
) -> Tuple[Path, Dict[str, Any]]:
    actor_summary = load_stage_summary(campaign_dir, "actor_smoke_dynamic_summary.json")
    resolver_summary = load_stage_summary(campaign_dir, "resolver_smoke_dynamic_summary.json")
    selection_cfg = campaign_config["selection"]
    registry_path = ensure_registry(campaign_dir, campaign_config)
    clear_config_caches(registry_path)
    smoke_case_workers = max(1, int(campaign_config.get("smoke_case_workers", 1)))

    actor_ranked = sorted(actor_summary["results"], key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    resolver_ranked = sorted(resolver_summary["results"], key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    top_actors = actor_ranked[: int(selection_cfg["pair_top_actor_n"])]
    top_resolvers = resolver_ranked[: int(selection_cfg["pair_top_resolver_n"])]

    cases: List[Dict[str, Any]] = []
    for actor_item in top_actors:
        for resolver_item in top_resolvers:
            cases.append(
                stage_case(
                    case_id=f"pair-{actor_item['case_id']}-{resolver_item['case_id']}",
                    label=f"Pair Smoke / {actor_item['label']} + {resolver_item['label']}",
                    actor_selector=actor_item["actor_selector"],
                    resolver_selector=resolver_item["resolver_selector"],
                    notes="Top actor candidate crossed with top resolver candidate.",
                )
            )

    suite = build_suite(
        name="campaign-pair-smoke",
        description="Crossed smoke eval between top actor and resolver candidates.",
        score_profile="latency_smoke",
        shared_max_rounds=1,
        shared_repeat_count=1,
        runtime_overrides=campaign_config["smoke_runtime_overrides"],
        cases=cases,
    )
    stage_dir = ensure_dir(campaign_dir / "pair_smoke")
    output_dir, summary = run_eval_stage(
        stage_dir,
        suite,
        base_config_path,
        python_bin,
        registry_path,
        case_workers=smoke_case_workers,
    )
    write_json(campaign_dir / "pair_smoke_summary.json", summary)
    print(str(output_dir), flush=True)
    return output_dir, summary


def run_progression_stage(
    campaign_dir: Path,
    base_config_path: Path,
    campaign_config: Dict[str, Any],
    python_bin: str,
) -> Tuple[Path, Dict[str, Any]]:
    pair_summary = load_stage_summary(campaign_dir, "pair_smoke_summary.json")
    selection_cfg = campaign_config["selection"]
    registry_path = ensure_registry(campaign_dir, campaign_config)
    clear_config_caches(registry_path)
    progression_case_workers = max(1, int(campaign_config.get("progression_case_workers", 1)))

    progression_finalists = choose_pair_finalists(pair_summary, top_n=int(selection_cfg["progression_top_n"]))
    cases = [
        stage_case(
            case_id=f"progression-{item['case_id']}",
            label=f"Progression / {item['label']}",
            actor_selector=item["actor_selector"],
            resolver_selector=item["resolver_selector"],
            notes="Top pair advanced to 3-tick progression stage.",
        )
        for item in progression_finalists
    ]
    suite = build_suite(
        name="campaign-progression",
        description="Three-tick progression eval for top pair candidates.",
        score_profile="progression_mini",
        shared_max_rounds=3,
        shared_repeat_count=1,
        runtime_overrides=campaign_config["progression_runtime_overrides"],
        cases=cases,
    )
    stage_dir = ensure_dir(campaign_dir / "progression")
    output_dir, summary = run_eval_stage(
        stage_dir,
        suite,
        base_config_path,
        python_bin,
        registry_path,
        case_workers=progression_case_workers,
    )
    write_json(campaign_dir / "progression_summary.json", summary)
    print(str(output_dir), flush=True)
    return output_dir, summary


def run_stability_stage(
    campaign_dir: Path,
    base_config_path: Path,
    campaign_config: Dict[str, Any],
    python_bin: str,
) -> Tuple[Path, Dict[str, Any]]:
    progression_summary = load_stage_summary(campaign_dir, "progression_summary.json")
    selection_cfg = campaign_config["selection"]
    registry_path = ensure_registry(campaign_dir, campaign_config)
    clear_config_caches(registry_path)
    stability_case_workers = max(1, int(campaign_config.get("stability_case_workers", 1)))

    finalists = choose_pair_finalists(progression_summary, top_n=int(selection_cfg["stability_top_n"]))
    cases = [
        stage_case(
            case_id=f"stability-{item['case_id']}",
            label=f"Stability / {item['label']}",
            actor_selector=item["actor_selector"],
            resolver_selector=item["resolver_selector"],
            notes="Top progression pair advanced to repeat-based stability stage.",
        )
        for item in finalists
    ]
    suite = build_suite(
        name="campaign-stability",
        description="Repeat progression eval for the top pair candidates.",
        score_profile="progression_mini",
        shared_max_rounds=3,
        shared_repeat_count=int(selection_cfg["stability_repeat_count"]),
        runtime_overrides=campaign_config["progression_runtime_overrides"],
        cases=cases,
    )
    stage_dir = ensure_dir(campaign_dir / "stability")
    output_dir, summary = run_eval_stage(
        stage_dir,
        suite,
        base_config_path,
        python_bin,
        registry_path,
        case_workers=stability_case_workers,
    )
    write_json(campaign_dir / "stability_summary.json", summary)
    print(str(output_dir), flush=True)
    return output_dir, summary


def finalize_campaign(campaign_dir: Path, base_config_path: Path, campaign_config: Dict[str, Any]) -> Path:
    probe_summary = load_stage_summary(campaign_dir, "probe_summary.json")
    actor_summary = load_stage_summary(campaign_dir, "actor_smoke_dynamic_summary.json")
    resolver_summary = load_stage_summary(campaign_dir, "resolver_smoke_dynamic_summary.json")
    pair_summary = load_stage_summary(campaign_dir, "pair_smoke_summary.json")
    progression_summary = load_stage_summary(campaign_dir, "progression_summary.json")
    stability_summary = load_stage_summary(campaign_dir, "stability_summary.json")
    actor_baseline = load_stage_summary(campaign_dir, "actor_smoke_dynamic_baseline.json")
    resolver_baseline = load_stage_summary(campaign_dir, "resolver_smoke_dynamic_baseline.json")

    def metric(item: Dict[str, Any], *path: str, default: float = 0.0) -> float:
        current: Any = item
        for key in path:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
        if current is None:
            return default
        try:
            return float(current)
        except Exception:
            return default

    def repeat_completed(item: Dict[str, Any]) -> bool:
        repeat_count = int(item.get("repeat_count") or 0)
        completed_repeats = int(item.get("completed_repeats") or 0)
        failed_repeats = int(item.get("failed_repeats") or 0)
        return repeat_count > 0 and completed_repeats >= repeat_count and failed_repeats == 0

    def stability_default_key(item: Dict[str, Any]) -> Tuple[float, float, float, float, float]:
        return (
            1.0 if repeat_completed(item) else 0.0,
            metric(item, "scores", "overall"),
            -metric(item, "diagnostics", "salvage_tick_rate", default=999.0),
            -metric(item, "diagnostics", "provider_wait_total_s", default=999999.0),
            metric(item, "events", "events_completed"),
        )

    def fastest_viable_key(item: Dict[str, Any]) -> Tuple[float, float, float, float]:
        return (
            metric(item, "timing", "simulation_total_s", default=999999.0),
            metric(item, "timing", "tick_start_to_first_event_s", default=999999.0),
            metric(item, "diagnostics", "provider_wait_total_s", default=999999.0),
            metric(item, "diagnostics", "salvage_tick_rate", default=999.0),
        )

    def stability_key(item: Dict[str, Any]) -> Tuple[float, float, float, float, float, float]:
        return (
            -metric(item, "scores", "resilience"),
            metric(item, "score_spread", "overall", "std", default=999999.0),
            metric(item, "diagnostics", "salvage_tick_rate", default=999.0),
            metric(item, "diagnostics", "provider_wait_total_s", default=999999.0),
            -metric(item, "events", "events_completed"),
            metric(item, "timing", "simulation_total_s", default=999999.0),
        )

    stability_results = sorted(stability_summary["results"], key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0, reverse=True)
    default_strategy = max(stability_results, key=stability_default_key, default={})

    stable_viable_pool = [
        item
        for item in stability_results
        if repeat_completed(item)
        and metric(item, "events", "events_completed") >= 1.0
        and metric(item, "diagnostics", "salvage_tick_rate", default=999.0) <= 0.5
    ]
    fastest_viable = min(stable_viable_pool, key=fastest_viable_key, default={})
    fastest_viable_stage = "stability" if fastest_viable else "pair_smoke"
    if not fastest_viable:
        pair_results = pair_summary.get("results", [])
        pair_viable_pool = [
            item
            for item in pair_results
            if metric(item, "events", "events_completed") >= 1.0
            and metric(item, "diagnostics", "salvage_tick_rate", default=999.0) <= 0.5
        ]
        fastest_viable = min(
            pair_viable_pool,
            key=lambda item: (
                metric(item, "timing", "tick_start_to_first_event_s", default=999999.0),
                metric(item, "timing", "simulation_total_s", default=999999.0),
            ),
            default={},
        )

    stable_progression_pool = [item for item in stability_results if repeat_completed(item)]
    most_stable_progression = min(stable_progression_pool, key=stability_key, default={})
    most_stable_progression_stage = "stability" if most_stable_progression else "progression"
    if not most_stable_progression:
        most_stable_progression = min(
            progression_summary.get("results", []),
            key=lambda item: (
                metric(item, "diagnostics", "salvage_tick_rate", default=999.0),
                metric(item, "diagnostics", "provider_wait_total_s", default=999999.0),
                -metric(item, "scores", "resilience"),
                -metric(item, "events", "events_completed"),
            ),
            default={},
        )

    stages = {
        "actor_smoke": {
            "output_dir": str(campaign_dir / "actor_smoke_dynamic"),
            "top_results": summarize_ranking(actor_summary),
            "summary": actor_summary,
        },
        "resolver_smoke": {
            "output_dir": str(campaign_dir / "resolver_smoke_dynamic"),
            "top_results": summarize_ranking(resolver_summary),
            "summary": resolver_summary,
        },
        "pair_smoke": {
            "output_dir": str(campaign_dir / "pair_smoke"),
            "top_results": summarize_ranking(pair_summary),
            "summary": pair_summary,
        },
        "progression": {
            "output_dir": str(campaign_dir / "progression"),
            "top_results": summarize_ranking(progression_summary),
            "summary": progression_summary,
        },
        "stability": {
            "output_dir": str(campaign_dir / "stability"),
            "top_results": summarize_ranking(stability_summary),
            "summary": stability_summary,
        },
    }

    campaign_summary = {
        "name": campaign_config["name"],
        "description": campaign_config["description"],
        "campaign_dir": str(campaign_dir),
        "base_config": str(base_config_path),
        "registry_path": str(campaign_dir / "temp_llm_registry.json"),
        "probe": probe_summary,
        "baselines": {
            "actor_smoke_resolver": actor_baseline,
            "resolver_smoke_actor": resolver_baseline,
        },
        "stages": stages,
        "recommendations": {
            "default_strategy": recommendation_block("stability", default_strategy),
            "fastest_viable": recommendation_block(fastest_viable_stage, fastest_viable),
            "most_stable_progression": recommendation_block(most_stable_progression_stage, most_stable_progression),
        },
    }
    summary_path = campaign_dir / "campaign_summary.json"
    write_json(summary_path, campaign_summary)
    (campaign_dir / "campaign_report.md").write_text(build_report(campaign_summary), encoding="utf-8")
    print(str(summary_path), flush=True)
    return summary_path


def main() -> None:
    args = parse_args()
    base_config_path = Path(args.base_config).resolve()
    campaign_config = load_json(Path(args.campaign_config).resolve())
    campaign_dir = resolve_campaign_dir(args)
    lock_path = acquire_stage_lock(campaign_dir, args.stage)
    try:
        if args.stage == "probe":
            run_probe_stage(campaign_dir, base_config_path, campaign_config)
            return
        if args.stage == "actor_smoke":
            run_actor_smoke_stage(campaign_dir, base_config_path, campaign_config, args.python_bin)
            return
        if args.stage == "resolver_smoke":
            run_resolver_smoke_stage(campaign_dir, base_config_path, campaign_config, args.python_bin)
            return
        if args.stage == "pair_smoke":
            run_pair_smoke_stage(campaign_dir, base_config_path, campaign_config, args.python_bin)
            return
        if args.stage == "progression":
            run_progression_stage(campaign_dir, base_config_path, campaign_config, args.python_bin)
            return
        if args.stage == "stability":
            run_stability_stage(campaign_dir, base_config_path, campaign_config, args.python_bin)
            return
        if args.stage == "finalize":
            finalize_campaign(campaign_dir, base_config_path, campaign_config)
            return

        raise ValueError(f"Unsupported stage: {args.stage}")
    finally:
        release_stage_lock(lock_path)


if __name__ == "__main__":
    main()
