#!/usr/bin/env python3
"""
Run a single case from a world-model eval suite and emit its result payload.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts.run_world_model_eval_campaign import (  # noqa: E402
    ensure_dir,
    load_json,
    run_single_case_stage_eval,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a single world-model eval case.")
    parser.add_argument("--base-config", required=True, help="Path to a world simulation_config.json")
    parser.add_argument("--suite-config", required=True, help="Path to the suite.json")
    parser.add_argument("--stage-dir", required=True, help="Stage dir used as the case output root")
    parser.add_argument("--case-id", required=True, help="Case id to run from the suite")
    parser.add_argument("--registry-path", required=True, help="Path to temp_llm_registry.json")
    parser.add_argument("--python-bin", default=sys.executable, help="Python interpreter used for stage runs")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_config_path = Path(args.base_config).resolve()
    suite_path = Path(args.suite_config).resolve()
    stage_dir = ensure_dir(Path(args.stage_dir).resolve())
    registry_path = Path(args.registry_path).resolve()

    suite = load_json(suite_path)
    case = next((item for item in suite.get("cases", []) if item.get("case_id") == args.case_id), None)
    if not case:
        raise ValueError(f"Case not found in suite: {args.case_id}")

    result = run_single_case_stage_eval(
        stage_dir=stage_dir,
        suite=suite,
        case=case,
        base_config_path=base_config_path,
        python_bin=args.python_bin,
        registry_path=registry_path,
    )
    result_path = stage_dir / "raw" / args.case_id / "case_result.json"
    write_json(result_path, result)
    print(str(result_path), flush=True)


if __name__ == "__main__":
    main()
