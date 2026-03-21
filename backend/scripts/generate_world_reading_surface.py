#!/usr/bin/env python3
"""
Generate chronicle / actor board / risk digest artifacts for a world simulation.
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

from app.services.world_reading_surface import generate_reading_surface  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate world reading surface artifacts")
    parser.add_argument("--simulation-id", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--diagnostics-dir", default="")
    parser.add_argument("--base-name", default="")
    args = parser.parse_args()

    result = generate_reading_surface(
        simulation_id=args.simulation_id,
        label=args.label,
        diagnostics_dir=args.diagnostics_dir,
        base_name=args.base_name,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
