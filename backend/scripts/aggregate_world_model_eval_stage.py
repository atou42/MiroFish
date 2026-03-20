#!/usr/bin/env python3
"""
Aggregate per-case world-model eval results into a stage summary/report.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.run_world_model_eval_campaign import (  # noqa: E402
    build_stage_report,
    load_json,
    summarize_ranking,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate single-case world-model eval outputs.")
    parser.add_argument("--stage-dir", required=True, help="Stage output dir")
    parser.add_argument("--suite-config", required=True, help="Path to suite.json")
    parser.add_argument("--base-config", required=True, help="Path to simulation_config.json")
    return parser.parse_args()


def load_case_result(stage_dir: Path, case_id: str) -> Dict[str, Any]:
    raw_result = stage_dir / "raw" / case_id / "case_result.json"
    if raw_result.exists():
        payload = load_json(raw_result)
        return payload["result"]

    candidate_dirs = [stage_dir / case_id, stage_dir / case_id.replace("_", "-")]
    for case_dir in candidate_dirs:
        direct_result = case_dir / "metrics.json"
        if direct_result.exists():
            return load_json(direct_result)

    raise FileNotFoundError(f"Missing result for case: {case_id}")


def main() -> None:
    args = parse_args()
    stage_dir = Path(args.stage_dir).resolve()
    suite_path = Path(args.suite_config).resolve()
    base_config_path = Path(args.base_config).resolve()
    suite = load_json(suite_path)
    cases = list(suite.get("cases", []))

    results: List[Dict[str, Any]] = [load_case_result(stage_dir, case["case_id"]) for case in cases]
    summary = {
        "generated_at": datetime.now().isoformat(),
        "base_config": str(base_config_path),
        "suite_config": str(suite_path),
        "score_profile": suite.get("score_profile", "latency_smoke"),
        "results": results,
        "case_outputs": {
            case["case_id"]: str((stage_dir / "raw" / case["case_id"]).resolve())
            if (stage_dir / "raw" / case["case_id"]).exists()
            else str((stage_dir / case["case_id"]).resolve())
            for case in cases
        },
    }
    write_json(stage_dir / "summary.json", summary)
    write_json(
        stage_dir / "leaderboard.json",
        {
            "results": summarize_ranking(summary, top_n=max(len(results), 1)),
        },
    )
    (stage_dir / "report.md").write_text(build_stage_report(stage_dir.name, suite, results), encoding="utf-8")
    print(str(stage_dir / "summary.json"), flush=True)


if __name__ == "__main__":
    main()
