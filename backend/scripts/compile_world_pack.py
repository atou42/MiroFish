#!/usr/bin/env python3
"""
Compile raw world materials into a runnable world-mode simulation bundle.
"""

import argparse
import json
import os
import sys

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)
sys.path.insert(0, _project_root)

from app.services.world_pack_compiler import WorldPackCompiler  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile a local world pack")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--simulation-requirement", default="")
    parser.add_argument("--simulation-id", default="")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--graph-id", default="")
    parser.add_argument("--world-preset", default="")
    parser.add_argument("--pack-id", default="")
    parser.add_argument("--pack-title", default="")
    parser.add_argument("--no-llm-profiles", action="store_true")
    parser.add_argument("--use-llm-config", action="store_true")
    parser.add_argument("--profile-parallel-count", type=int, default=3)
    args = parser.parse_args()

    compiler = WorldPackCompiler()
    result = compiler.compile(
        source_dir=args.source_dir,
        simulation_requirement=args.simulation_requirement,
        simulation_id=args.simulation_id,
        project_id=args.project_id,
        graph_id=args.graph_id,
        world_preset_id=args.world_preset or None,
        pack_id=args.pack_id,
        pack_title=args.pack_title,
        use_llm_for_profiles=not args.no_llm_profiles,
        use_llm_for_config=args.use_llm_config,
        profile_parallel_count=args.profile_parallel_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
