#!/usr/bin/env python3
"""
Run a staged world-model evaluation matrix.

Stages:
1. Probe selector health and JSON compatibility.
2. Actor smoke with a fixed baseline resolver.
3. Resolver smoke with the best actor from stage 2.
4. Combo smoke on top actor/resolver finalists.
5. Progression mini on top combos.
6. Final repeat validation on the top progression finalists.
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


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
EVAL_WORLD_SCRIPT = SCRIPT_DIR / "eval_world_models.py"
DEFAULT_CANDIDATES = BACKEND_DIR / "evals" / "world_model_matrix_candidates.json"
SMOKE_TEMPLATE = BACKEND_DIR / "evals" / "world_model_eval_suite.json"
PROGRESSION_TEMPLATE = BACKEND_DIR / "evals" / "world_model_eval_progression_suite.json"
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

from app.utils.llm_client import LLMClient


@dataclass
class Candidate:
    id: str
    selector: str
    label: str
    family: str
    provider: str
    priority: int
    roles: List[str]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def clear_proxy_env(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    payload = dict(env or os.environ)
    for key in DEFAULT_PROXY_VARS:
        payload.pop(key, None)
    return payload


def drop_proxy_env_inplace() -> None:
    for key in DEFAULT_PROXY_VARS:
        os.environ.pop(key, None)


def load_candidates(path: Path) -> tuple[Dict[str, Any], List[Candidate]]:
    raw = load_json(path)
    candidates = [
        Candidate(
            id=str(item["id"]),
            selector=str(item["selector"]),
            label=str(item["label"]),
            family=str(item["family"]),
            provider=str(item["provider"]),
            priority=int(item.get("priority", 0)),
            roles=[str(role) for role in item.get("roles", [])],
        )
        for item in raw.get("candidates", [])
    ]
    return raw, candidates


def build_probe_messages() -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Return only a compact JSON object with keys "
                "title, summary, location, priority, duration_ticks."
            ),
        },
        {
            "role": "user",
            "content": (
                "Scene: world government tries to suppress the broadcast fallout. "
                "Actor: World Government. Return one concrete action."
            ),
        },
    ]


def probe_candidates(
    candidates: Sequence[Candidate],
    probe_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    drop_proxy_env_inplace()
    messages = build_probe_messages()
    health_timeout = float(probe_config.get("health_timeout_seconds", 12))
    json_timeout = float(probe_config.get("json_timeout_seconds", 25))
    max_tokens = int(probe_config.get("max_tokens", 256))
    results: List[Dict[str, Any]] = []

    for candidate in candidates:
        item = {
            "id": candidate.id,
            "selector": candidate.selector,
            "label": candidate.label,
            "family": candidate.family,
            "provider": candidate.provider,
            "roles": candidate.roles,
            "priority": candidate.priority,
        }
        try:
            client = LLMClient.from_selector(candidate.selector)
            start_health = time.time()
            client.health_check(timeout=health_timeout)
            item["health_s"] = round(time.time() - start_health, 2)

            start_json = time.time()
            payload = client.chat_json(
                messages=messages,
                temperature=0.0,
                max_tokens=max_tokens,
                timeout=json_timeout,
            )
            item["json_s"] = round(time.time() - start_json, 2)
            item["ok"] = True
            item["json_keys"] = sorted(payload.keys())
            item["sample_title"] = str(payload.get("title") or payload.get("objective") or "")[:120]
        except Exception as exc:
            item["ok"] = False
            item["error"] = str(exc)[:500]
        results.append(item)

    return results


def candidate_lookup(candidates: Sequence[Candidate]) -> Dict[str, Candidate]:
    return {candidate.id: candidate for candidate in candidates}


def sort_probe_results(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def sort_key(item: Dict[str, Any]) -> tuple:
        return (
            0 if item.get("ok") else 1,
            -int(item.get("priority", 0)),
            float(item.get("health_s") or 9999),
            float(item.get("json_s") or 9999),
        )

    return sorted(rows, key=sort_key)


def limit_candidates(
    probe_rows: Sequence[Dict[str, Any]],
    role: str,
    max_count: int,
) -> List[Dict[str, Any]]:
    eligible = [
        row
        for row in sort_probe_results(probe_rows)
        if row.get("ok") and role in (row.get("roles") or [])
    ]
    return eligible[:max_count] if max_count > 0 else eligible


def make_case(
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


def build_stage_suite(
    template_path: Path,
    output_path: Path,
    *,
    name: str,
    description: str,
    score_profile: Optional[str],
    cases: Sequence[Dict[str, Any]],
    shared_repeat_count: Optional[int] = None,
) -> Path:
    template = load_json(template_path)
    template["name"] = name
    template["description"] = description
    if score_profile:
        template["score_profile"] = score_profile
    if shared_repeat_count is not None:
        template["shared_repeat_count"] = int(shared_repeat_count)
    template["cases"] = list(cases)
    write_json(output_path, template)
    return output_path


def run_eval_suite(
    *,
    python_bin: str,
    base_config: Path,
    suite_config: Path,
    output_root: Path,
) -> Path:
    env = clear_proxy_env()
    result = subprocess.run(
        [
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
        ],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"eval_world_models.py failed for {suite_config.name}: {result.stderr or result.stdout}"
        )
    stage_dir = Path((result.stdout or "").strip().splitlines()[-1]).resolve()
    if not stage_dir.exists():
        raise RuntimeError(f"Missing stage output dir for {suite_config.name}: {stage_dir}")
    return stage_dir


def load_stage_summary(stage_dir: Path) -> Dict[str, Any]:
    return load_json(stage_dir / "summary.json")


def results_by_case(summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {item["case_id"]: item for item in summary.get("results", [])}


def stage_case_summary(stage_name: str, stage_dir: Path, summary: Dict[str, Any]) -> Dict[str, Any]:
    ordered_results = sorted(
        summary.get("results", []),
        key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0,
        reverse=True,
    )
    return {
        "stage": stage_name,
        "output_dir": str(stage_dir),
        "results": ordered_results,
    }


def select_top_case_ids(
    summary: Dict[str, Any],
    top_n: int,
) -> List[str]:
    ordered = sorted(
        summary.get("results", []),
        key=lambda item: item.get("scores", {}).get("overall", 0.0) or 0.0,
        reverse=True,
    )
    return [item["case_id"] for item in ordered[:top_n]]


def load_case_runs(stage_dir: Path, case_id: str) -> List[Dict[str, Any]]:
    path = stage_dir / case_id / "runs.json"
    if not path.exists():
        return []
    return load_json(path).get("runs", [])


def numeric_stats(values: Iterable[Optional[float]]) -> Dict[str, Optional[float]]:
    valid = [float(value) for value in values if value is not None]
    if not valid:
        return {"count": 0, "mean": None, "min": None, "max": None}
    return {
        "count": len(valid),
        "mean": round(sum(valid) / len(valid), 3),
        "min": round(min(valid), 3),
        "max": round(max(valid), 3),
    }


def combine_finalist_runs(
    progression_stage_dir: Path,
    final_stage_dir: Path,
    finalist_case_ids: Sequence[str],
) -> List[Dict[str, Any]]:
    combined: List[Dict[str, Any]] = []
    for case_id in finalist_case_ids:
        progression_runs = load_case_runs(progression_stage_dir, case_id)
        final_runs = load_case_runs(final_stage_dir, case_id)
        all_runs = progression_runs + final_runs
        if not all_runs:
            continue

        label = (progression_runs or final_runs)[0].get("label", case_id)
        actor_selector = (progression_runs or final_runs)[0].get("actor_selector")
        resolver_selector = (progression_runs or final_runs)[0].get("resolver_selector")
        item = {
            "case_id": case_id,
            "label": label,
            "actor_selector": actor_selector,
            "resolver_selector": resolver_selector,
            "run_count": len(all_runs),
            "scores": {
                key: numeric_stats(run.get("scores", {}).get(key) for run in all_runs)
                for key in ("speed", "progression", "resilience", "cleanliness", "overall")
            },
            "timing": {
                key: numeric_stats(run.get("timing", {}).get(key) for run in all_runs)
                for key in ("tick_start_to_first_event_s", "tick_start_to_tick_end_s", "simulation_total_s")
            },
            "diagnostics": {
                key: numeric_stats(run.get("diagnostics", {}).get(key) for run in all_runs)
                for key in ("accepted_event_ratio", "salvage_tick_rate", "provider_wait_total_s")
            },
            "quality": {
                key: numeric_stats(run.get("quality", {}).get(key) for run in all_runs)
                for key in ("progress_signal", "dirty_intent_title_rate", "low_signal_source_rate")
            },
            "runs": all_runs,
        }
        combined.append(item)
    combined.sort(key=lambda item: item["scores"]["overall"]["mean"] or 0.0, reverse=True)
    return combined


def pick_recommendation(
    combined_finalists: Sequence[Dict[str, Any]],
    stage_results: Dict[str, Any],
) -> Dict[str, Any]:
    if not combined_finalists:
        return {}

    default_combo = combined_finalists[0]
    speed_first = max(
        combined_finalists,
        key=lambda item: (
            item["scores"]["speed"]["mean"] or 0.0,
            item["scores"]["overall"]["mean"] or 0.0,
        ),
    )
    progression_first = max(
        combined_finalists,
        key=lambda item: (
            item["scores"]["progression"]["mean"] or 0.0,
            item["scores"]["overall"]["mean"] or 0.0,
        ),
    )
    stability_first = max(
        combined_finalists,
        key=lambda item: (
            item["scores"]["resilience"]["mean"] or 0.0,
            -(item["diagnostics"]["salvage_tick_rate"]["mean"] or 0.0),
            item["scores"]["overall"]["mean"] or 0.0,
        ),
    )

    return {
        "default_combo": default_combo,
        "speed_first": speed_first,
        "progression_first": progression_first,
        "stability_first": stability_first,
        "best_actor_smoke": stage_results["actor_smoke"]["results"][0] if stage_results["actor_smoke"]["results"] else None,
        "best_resolver_smoke": stage_results["resolver_smoke"]["results"][0] if stage_results["resolver_smoke"]["results"] else None,
    }


def render_matrix_report(summary: Dict[str, Any]) -> str:
    lines = [
        "# World Model Matrix Eval",
        "",
        summary["description"],
        "",
        f"- Matrix dir: `{summary['matrix_dir']}`",
        f"- Base config: `{summary['base_config']}`",
        "",
        "## Probe",
    ]
    for row in summary["probe"]["results"]:
        if row.get("ok"):
            lines.append(
                f"- `{row['label']}` ok health=`{row.get('health_s')}`s json=`{row.get('json_s')}`s selector=`{row['selector']}`"
            )
        else:
            lines.append(
                f"- `{row['label']}` failed selector=`{row['selector']}` error=`{row.get('error')}`"
            )

    lines.extend(["", "## Stages"])
    for stage_name in ("actor_smoke", "resolver_smoke", "combo_smoke", "progression", "final_repeats"):
        stage = summary["stages"].get(stage_name)
        if not stage:
            continue
        lines.append(f"- `{stage_name}` output=`{stage['output_dir']}`")

    recommendations = summary.get("recommendations") or {}
    if recommendations:
        lines.extend(["", "## Recommendations"])
        for key in ("default_combo", "speed_first", "progression_first", "stability_first"):
            item = recommendations.get(key)
            if not item:
                continue
            lines.append(
                f"- `{key}`: `{item['label']}` actor=`{item['actor_selector']}` resolver=`{item['resolver_selector']}` "
                f"overall_mean=`{item['scores']['overall']['mean']}` speed_mean=`{item['scores']['speed']['mean']}` "
                f"progression_mean=`{item['scores']['progression']['mean']}` resilience_mean=`{item['scores']['resilience']['mean']}` "
                f"first_event_mean=`{item['timing']['tick_start_to_first_event_s']['mean']}` "
                f"salvage_tick_rate_mean=`{item['diagnostics']['salvage_tick_rate']['mean']}`"
            )

    finalists = summary.get("finalists") or []
    if finalists:
        lines.extend(["", "## Finalists"])
        for item in finalists:
            lines.append(
                f"- `{item['label']}` runs=`{item['run_count']}` overall_mean=`{item['scores']['overall']['mean']}` "
                f"speed_mean=`{item['scores']['speed']['mean']}` progression_mean=`{item['scores']['progression']['mean']}` "
                f"resilience_mean=`{item['scores']['resilience']['mean']}` first_event_mean=`{item['timing']['tick_start_to_first_event_s']['mean']}` "
                f"accepted_event_ratio_mean=`{item['diagnostics']['accepted_event_ratio']['mean']}` "
                f"salvage_tick_rate_mean=`{item['diagnostics']['salvage_tick_rate']['mean']}`"
            )

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run staged world model eval matrix.")
    parser.add_argument("--base-config", required=True, help="Path to world simulation_config.json")
    parser.add_argument("--candidates-config", default=str(DEFAULT_CANDIDATES), help="Candidate pool JSON")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Root directory for eval outputs")
    parser.add_argument("--python-bin", default=sys.executable, help="Python interpreter for eval runs")
    args = parser.parse_args()

    base_config = Path(args.base_config).resolve()
    candidates_config = Path(args.candidates_config).resolve()
    output_root = ensure_dir(Path(args.output_root).resolve())

    config, candidates = load_candidates(candidates_config)
    matrix_dir = ensure_dir(output_root / f"world-model-matrix-{now_stamp()}")
    ensure_dir(matrix_dir / "stages")

    probe_results = probe_candidates(candidates, config.get("probe", {}))
    write_json(
        matrix_dir / "probe_results.json",
        {
            "generated_at": datetime.now().isoformat(),
            "results": probe_results,
        },
    )

    limits = config.get("limits", {})
    actor_candidates = limit_candidates(
        probe_results,
        role="actor",
        max_count=int(limits.get("actor_smoke_max", 0)),
    )
    resolver_candidates = limit_candidates(
        probe_results,
        role="resolver",
        max_count=int(limits.get("resolver_smoke_max", 0)),
    )
    if not actor_candidates:
        raise SystemExit("No actor candidates survived probe stage")
    if not resolver_candidates:
        raise SystemExit("No resolver candidates survived probe stage")

    actor_cases = [
        make_case(
            case_id=row["id"],
            label=row["label"],
            actor_selector=row["selector"],
            resolver_selector=str(config["base_resolver_selector"]),
            notes="Actor smoke with fixed baseline resolver.",
        )
        for row in actor_candidates
    ]
    actor_suite_path = build_stage_suite(
        template_path=SMOKE_TEMPLATE,
        output_path=matrix_dir / "actor_smoke_suite.json",
        name="world-model-actor-smoke",
        description="Actor selector smoke with fixed baseline resolver.",
        score_profile="latency_smoke",
        cases=actor_cases,
        shared_repeat_count=1,
    )
    actor_stage_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=actor_suite_path,
        output_root=matrix_dir / "stages" / "actor_smoke",
    )
    actor_summary = load_stage_summary(actor_stage_dir)
    best_actor_case = select_top_case_ids(actor_summary, 1)[0]
    best_actor_selector = results_by_case(actor_summary)[best_actor_case]["actor_selector"]

    resolver_cases = [
        make_case(
            case_id=row["id"],
            label=row["label"],
            actor_selector=best_actor_selector,
            resolver_selector=row["selector"],
            notes="Resolver smoke with best actor from actor stage.",
        )
        for row in resolver_candidates
    ]
    resolver_suite_path = build_stage_suite(
        template_path=SMOKE_TEMPLATE,
        output_path=matrix_dir / "resolver_smoke_suite.json",
        name="world-model-resolver-smoke",
        description="Resolver selector smoke with the best actor from actor stage.",
        score_profile="latency_smoke",
        cases=resolver_cases,
        shared_repeat_count=1,
    )
    resolver_stage_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=resolver_suite_path,
        output_root=matrix_dir / "stages" / "resolver_smoke",
    )
    resolver_summary = load_stage_summary(resolver_stage_dir)

    top_actor_case_ids = select_top_case_ids(actor_summary, int(limits.get("combo_actor_top", 3)))
    top_resolver_case_ids = select_top_case_ids(resolver_summary, int(limits.get("combo_resolver_top", 2)))
    actor_results = results_by_case(actor_summary)
    resolver_results = results_by_case(resolver_summary)
    combo_cases: List[Dict[str, Any]] = []
    for actor_case_id in top_actor_case_ids:
        for resolver_case_id in top_resolver_case_ids:
            actor_item = actor_results[actor_case_id]
            resolver_item = resolver_results[resolver_case_id]
            combo_cases.append(
                make_case(
                    case_id=f"{actor_case_id}__{resolver_case_id}",
                    label=f"{actor_item['label']} + {resolver_item['label']}",
                    actor_selector=actor_item["actor_selector"],
                    resolver_selector=resolver_item["resolver_selector"],
                    notes="Combo smoke on top actor and resolver finalists.",
                )
            )

    combo_suite_path = build_stage_suite(
        template_path=SMOKE_TEMPLATE,
        output_path=matrix_dir / "combo_smoke_suite.json",
        name="world-model-combo-smoke",
        description="Smoke test for top actor/resolver combinations.",
        score_profile="latency_smoke",
        cases=combo_cases,
        shared_repeat_count=1,
    )
    combo_stage_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=combo_suite_path,
        output_root=matrix_dir / "stages" / "combo_smoke",
    )
    combo_summary = load_stage_summary(combo_stage_dir)
    top_combo_case_ids = select_top_case_ids(combo_summary, int(limits.get("progression_top", 3)))
    combo_results = results_by_case(combo_summary)

    progression_cases = [
        make_case(
            case_id=case_id,
            label=combo_results[case_id]["label"],
            actor_selector=combo_results[case_id]["actor_selector"],
            resolver_selector=combo_results[case_id]["resolver_selector"],
            notes="Progression mini for top smoke combinations.",
        )
        for case_id in top_combo_case_ids
    ]
    progression_suite_path = build_stage_suite(
        template_path=PROGRESSION_TEMPLATE,
        output_path=matrix_dir / "progression_suite.json",
        name="world-model-progression-finalists",
        description="Three-tick progression test for top smoke combinations.",
        score_profile="progression_mini",
        cases=progression_cases,
        shared_repeat_count=1,
    )
    progression_stage_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=progression_suite_path,
        output_root=matrix_dir / "stages" / "progression",
    )
    progression_summary = load_stage_summary(progression_stage_dir)
    finalist_case_ids = select_top_case_ids(progression_summary, int(limits.get("finalists_top", 2)))
    progression_results = results_by_case(progression_summary)

    final_repeat_cases = [
        make_case(
            case_id=case_id,
            label=progression_results[case_id]["label"],
            actor_selector=progression_results[case_id]["actor_selector"],
            resolver_selector=progression_results[case_id]["resolver_selector"],
            notes="Repeat validation for top progression finalists.",
        )
        for case_id in finalist_case_ids
    ]
    final_repeat_suite_path = build_stage_suite(
        template_path=PROGRESSION_TEMPLATE,
        output_path=matrix_dir / "final_repeats_suite.json",
        name="world-model-final-repeats",
        description="Repeat validation for top progression finalists.",
        score_profile="progression_mini",
        cases=final_repeat_cases,
        shared_repeat_count=int(limits.get("final_repeat_count", 2)),
    )
    final_repeat_stage_dir = run_eval_suite(
        python_bin=args.python_bin,
        base_config=base_config,
        suite_config=final_repeat_suite_path,
        output_root=matrix_dir / "stages" / "final_repeats",
    )
    final_repeat_summary = load_stage_summary(final_repeat_stage_dir)

    stages = {
        "actor_smoke": stage_case_summary("actor_smoke", actor_stage_dir, actor_summary),
        "resolver_smoke": stage_case_summary("resolver_smoke", resolver_stage_dir, resolver_summary),
        "combo_smoke": stage_case_summary("combo_smoke", combo_stage_dir, combo_summary),
        "progression": stage_case_summary("progression", progression_stage_dir, progression_summary),
        "final_repeats": stage_case_summary("final_repeats", final_repeat_stage_dir, final_repeat_summary),
    }
    finalists = combine_finalist_runs(
        progression_stage_dir=progression_stage_dir,
        final_stage_dir=final_repeat_stage_dir,
        finalist_case_ids=finalist_case_ids,
    )
    recommendations = pick_recommendation(finalists, stages)

    summary = {
        "generated_at": datetime.now().isoformat(),
        "description": config.get("description", "World model matrix eval"),
        "base_config": str(base_config),
        "matrix_dir": str(matrix_dir),
        "probe": {
            "results": sort_probe_results(probe_results),
            "actor_candidates": actor_candidates,
            "resolver_candidates": resolver_candidates,
        },
        "stages": stages,
        "finalists": finalists,
        "recommendations": recommendations,
    }
    write_json(matrix_dir / "matrix_summary.json", summary)
    report = render_matrix_report(summary)
    (matrix_dir / "matrix_report.md").write_text(report, encoding="utf-8")
    print(str(matrix_dir))


if __name__ == "__main__":
    main()
