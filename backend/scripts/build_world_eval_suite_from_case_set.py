#!/usr/bin/env python3
"""
List and materialize durable world eval case sets.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = BACKEND_DIR / "evals" / "world_case_sets" / "manifest.json"


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clone_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def parse_case_ids(raw_value: str | None) -> List[str]:
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def load_manifest(manifest_path: Path) -> Dict[str, Any]:
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"Invalid manifest payload: {manifest_path}")
    return manifest


def find_case_set_entry(manifest: Dict[str, Any], case_set_id: str) -> Dict[str, Any]:
    for entry in manifest.get("case_sets", []):
        if str(entry.get("id") or "").strip() == case_set_id:
            return entry
    available = ", ".join(sorted(str(item.get("id") or "").strip() for item in manifest.get("case_sets", [])))
    raise ValueError(f"Unknown case set '{case_set_id}'. Available: {available}")


def resolve_case_set_path(manifest_path: Path, entry: Dict[str, Any]) -> Path:
    relative_path = str(entry.get("path") or "").strip()
    if not relative_path:
        raise ValueError(f"Manifest entry missing path: {entry}")
    return (manifest_path.parent / relative_path).resolve()


def materialize_case_set(
    payload: Dict[str, Any],
    *,
    case_ids: List[str],
    repeat_count: int | None,
    max_rounds: int | None,
    case_set_entry: Dict[str, Any],
    source_path: Path,
) -> Dict[str, Any]:
    suite = clone_json(payload)

    if case_ids:
        requested = set(case_ids)
        filtered_cases = [case for case in suite.get("cases", []) if str(case.get("case_id") or "").strip() in requested]
        found = {str(case.get("case_id") or "").strip() for case in filtered_cases}
        missing = [case_id for case_id in case_ids if case_id not in found]
        if missing:
            raise ValueError(f"Unknown case ids for set '{case_set_entry['id']}': {', '.join(missing)}")
        suite["cases"] = filtered_cases

    if repeat_count is not None:
        suite["shared_repeat_count"] = max(1, int(repeat_count))

    if max_rounds is not None:
        suite["shared_max_rounds"] = max(1, int(max_rounds))

    suite["generated_from_case_set"] = {
        "id": case_set_entry.get("id"),
        "label": case_set_entry.get("label"),
        "path": str(source_path),
    }
    return suite


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List or materialize durable world eval case sets.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to world_case_sets manifest.json")
    parser.add_argument("--list", action="store_true", help="List available case sets")
    parser.add_argument("--case-set-id", help="Selected case set id")
    parser.add_argument("--case-ids", help="Comma-separated case ids to keep from the selected set")
    parser.add_argument("--repeat-count", type=int, help="Override shared_repeat_count")
    parser.add_argument("--max-rounds", type=int, help="Override shared_max_rounds")
    parser.add_argument("--output", help="Path to write the materialized suite JSON")
    parser.add_argument("--stdout", action="store_true", help="Print the materialized suite JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)

    if args.list:
        for entry in manifest.get("case_sets", []):
            resolved_path = resolve_case_set_path(manifest_path, entry)
            print(
                f"{entry.get('id')}\t{entry.get('label')}\t{entry.get('score_profile')}\t{resolved_path}",
                flush=True,
            )
        return

    if not args.case_set_id:
        raise ValueError("--case-set-id is required unless --list is used")

    entry = find_case_set_entry(manifest, args.case_set_id)
    source_path = resolve_case_set_path(manifest_path, entry)
    payload = load_json(source_path)
    case_ids = parse_case_ids(args.case_ids)
    has_transform = bool(case_ids or args.repeat_count is not None or args.max_rounds is not None)

    if not args.output and not args.stdout and not has_transform:
        print(str(source_path), flush=True)
        return

    if not args.output and not args.stdout and has_transform:
        raise ValueError("Use --output or --stdout when applying filters or overrides")

    suite = materialize_case_set(
        payload,
        case_ids=case_ids,
        repeat_count=args.repeat_count,
        max_rounds=args.max_rounds,
        case_set_entry=entry,
        source_path=source_path,
    )

    if args.stdout:
        sys.stdout.write(json.dumps(suite, ensure_ascii=False, indent=2) + "\n")

    if args.output:
        output_path = Path(args.output).resolve()
        write_json(output_path, suite)
        print(str(output_path), flush=True)


if __name__ == "__main__":
    main()
