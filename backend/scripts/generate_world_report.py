#!/usr/bin/env python3
"""
Generate and save a world-mode report for a simulation.
"""

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, List

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, ".."))
_project_root = os.path.abspath(os.path.join(_backend_dir, ".."))
sys.path.insert(0, _backend_dir)
sys.path.insert(0, _project_root)

from app.models.project import ProjectManager
from app.services.report_agent import ReportManager
from app.services.simulation_manager import SimulationManager
from app.services.world_report_agent import WorldReportAgent
from app.utils.network_env import drop_proxy_env_inplace


def report_dir_for_report_id(report_id: str) -> str:
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "uploads", "reports", report_id)
    )


def reset_report_dir(report_id: str) -> str:
    report_dir = report_dir_for_report_id(report_id)
    if os.path.isdir(report_dir):
        shutil.rmtree(report_dir)
    return report_dir


def validate_report_artifacts(report_id: str, report_dir: str = "") -> List[str]:
    errors: List[str] = []
    report_dir = os.path.abspath(report_dir or report_dir_for_report_id(report_id))
    meta_path = os.path.join(report_dir, "meta.json")
    markdown_path = os.path.join(report_dir, "full_report.md")
    outline_path = os.path.join(report_dir, "outline.json")
    progress_path = os.path.join(report_dir, "progress.json")

    meta: Dict[str, Any] = {}
    if not os.path.exists(meta_path):
        errors.append("report meta.json missing")
    else:
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as exc:
            errors.append(f"report meta.json unreadable: {exc}")

    if str(meta.get("status") or "").strip().lower() != "completed":
        errors.append(f"report meta.status not completed: {meta.get('status')}")

    outline: Dict[str, Any] = {}
    if not os.path.exists(outline_path):
        errors.append("report outline.json missing")
    else:
        try:
            with open(outline_path, "r", encoding="utf-8") as f:
                outline = json.load(f)
        except Exception as exc:
            errors.append(f"report outline.json unreadable: {exc}")

    sections = outline.get("sections") if isinstance(outline, dict) else []
    if not isinstance(sections, list) or not sections:
        errors.append("report outline sections missing")
    else:
        normalized_sections = []
        for item in sections:
            if not isinstance(item, dict):
                continue
            content = " ".join(str(item.get("content") or "").split()).strip().lower()
            if content:
                normalized_sections.append(content)
        if len(set(normalized_sections)) < len(normalized_sections):
            errors.append("report sections appear duplicated")

    if not os.path.exists(progress_path):
        errors.append("report progress.json missing")
    else:
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                progress = json.load(f)
            if str(progress.get("status") or "").strip().lower() != "completed":
                errors.append(f"report progress.status not completed: {progress.get('status')}")
        except Exception as exc:
            errors.append(f"report progress.json unreadable: {exc}")

    if not os.path.exists(markdown_path):
        errors.append("report full_report.md missing")
    else:
        try:
            with open(markdown_path, "r", encoding="utf-8") as f:
                markdown = f.read()
            if "## " not in markdown:
                errors.append("report full_report.md missing section headings")
            if len(markdown.strip()) < 80:
                errors.append("report full_report.md too short")
        except Exception as exc:
            errors.append(f"report full_report.md unreadable: {exc}")

    section_files = [
        name
        for name in os.listdir(report_dir)
        if name.startswith("section_") and name.endswith(".md")
    ] if os.path.isdir(report_dir) else []
    if isinstance(sections, list) and sections and len(section_files) != len(sections):
        errors.append(
            f"report section file count mismatch: files={len(section_files)} outline={len(sections)}"
        )
    return errors


def generate(
    simulation_id: str,
    label: str,
    report_id: str = "",
    fallback_only: bool = False,
    keep_proxy_env: bool = False,
) -> Dict[str, Any]:
    cleared_proxy_env: List[str] = []
    if not keep_proxy_env:
        cleared_proxy_env = drop_proxy_env_inplace()

    sim_manager = SimulationManager()
    state = sim_manager.get_simulation(simulation_id)
    if not state:
        raise ValueError(f"Simulation not found: {simulation_id}")

    project = ProjectManager.get_project(state.project_id)
    if not project:
        raise ValueError(f"Project not found: {state.project_id}")

    graph_id = state.graph_id or project.graph_id
    if not graph_id:
        raise ValueError(f"Missing graph_id for simulation: {simulation_id}")

    requirement = project.simulation_requirement or ""
    if not requirement:
        raise ValueError(f"Missing simulation requirement for project: {project.project_id}")

    report_id = report_id or f"report_world_{label}_{uuid.uuid4().hex[:10]}"
    report_dir = reset_report_dir(report_id)
    agent = WorldReportAgent(
        graph_id=graph_id,
        simulation_id=simulation_id,
        simulation_requirement=requirement,
        enable_llm=not fallback_only,
    )
    report = agent.generate_report(report_id=report_id)
    ReportManager.save_report(report)

    return {
        "simulation_id": simulation_id,
        "label": label,
        "report_id": report.report_id,
        "report_status": report.status.value,
        "report_dir": report_dir,
        "report_json_path": os.path.join(report_dir, "meta.json"),
        "report_markdown_path": os.path.join(report_dir, "full_report.md"),
        "validation_errors": validate_report_artifacts(report.report_id, report_dir),
        "cleared_proxy_env": cleared_proxy_env,
        "generated_at": datetime.now().isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate world-mode report")
    parser.add_argument("--simulation-id", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--report-id", default="")
    parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="Skip report LLM calls and generate the report via deterministic fallback sections.",
    )
    parser.add_argument(
        "--keep-proxy-env",
        action="store_true",
        help="Keep HTTP(S)/SOCKS proxy env vars instead of clearing them before report generation.",
    )
    args = parser.parse_args()

    result = generate(
        simulation_id=args.simulation_id,
        label=args.label,
        report_id=args.report_id,
        fallback_only=args.fallback_only,
        keep_proxy_env=args.keep_proxy_env,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
