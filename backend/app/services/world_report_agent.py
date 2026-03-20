"""
World mode report generation and chat.
"""

import json
import os
import time
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..config import Config
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from ..models.simulation_mode import SimulationMode
from .report_agent import (
    Report,
    ReportConsoleLogger,
    ReportLogger,
    ReportManager,
    ReportOutline,
    ReportSection,
    ReportStatus,
)

logger = get_logger("mirofish.world_report_agent")
WORLD_REPORT_LLM_TIMEOUT_SECONDS = 90.0


class WorldReportAgent:
    def __init__(
        self,
        graph_id: str,
        simulation_id: str,
        simulation_requirement: str,
        llm_client: Optional[LLMClient] = None,
        enable_llm: bool = True,
    ):
        self.graph_id = graph_id
        self.simulation_id = simulation_id
        self.simulation_requirement = simulation_requirement
        self.llm = llm_client if enable_llm else None
        if enable_llm and self.llm is None:
            try:
                self.llm = LLMClient.from_namespace("WORLD_REPORT")
            except Exception:
                self.llm = None
        self.report_logger: Optional[ReportLogger] = None
        self.console_logger: Optional[ReportConsoleLogger] = None

    def generate_report(
        self,
        progress_callback: Optional[Callable[[str, int, str], None]] = None,
        report_id: Optional[str] = None,
    ) -> Report:
        start_time = time.time()
        report_id = report_id or f"report_world_{int(time.time())}"
        self.report_logger = ReportLogger(report_id)
        self.console_logger = ReportConsoleLogger(report_id)

        try:
            self.report_logger.log_start(
                simulation_id=self.simulation_id,
                graph_id=self.graph_id,
                simulation_requirement=self.simulation_requirement,
            )
            ReportManager.update_progress(report_id, "planning", 0, "正在分析 world 模拟结果...")
            if progress_callback:
                progress_callback("planning", 0, "正在分析 world 模拟结果...")

            context = self._load_world_context()
            self.report_logger.log_planning_start()
            self.report_logger.log_planning_context(context)

            outline = self._build_outline(context)
            ReportManager.save_outline(report_id, outline)
            self.report_logger.log_planning_complete(outline.to_dict())
            ReportManager.update_progress(report_id, "generating", 20, "报告大纲已生成")
            if progress_callback:
                progress_callback("planning", 20, "报告大纲已生成")

            sections_output: List[ReportSection] = []
            total_sections = len(outline.sections)
            for index, section in enumerate(outline.sections, start=1):
                base_progress = 20 + int(((index - 1) / max(total_sections, 1)) * 70)
                ReportManager.update_progress(
                    report_id,
                    "generating",
                    base_progress,
                    f"正在生成章节: {section.title}",
                    current_section=section.title,
                    completed_sections=[item.title for item in sections_output],
                )
                if progress_callback:
                    progress_callback("generating", base_progress, f"正在生成章节: {section.title}")

                self.report_logger.log_section_start(section.title, index)
                content = self._build_section_content(section.title, context)
                self.report_logger.log_section_content(section.title, index, content, tool_calls_count=0)
                section.content = content
                sections_output.append(section)
                ReportManager.save_section(report_id, index, section)
                self.report_logger.log_section_full_complete(section.title, index, content)

            markdown_content = outline.to_markdown()
            report = Report(
                report_id=report_id,
                simulation_id=self.simulation_id,
                graph_id=self.graph_id,
                simulation_requirement=self.simulation_requirement,
                status=ReportStatus.COMPLETED,
                simulation_mode=SimulationMode.WORLD.value,
                outline=outline,
                markdown_content=markdown_content,
                created_at=datetime.now().isoformat(),
                completed_at=datetime.now().isoformat(),
            )

            ReportManager.update_progress(
                report_id,
                "completed",
                100,
                "报告生成完成",
                completed_sections=[section.title for section in sections_output],
            )
            self.report_logger.log_report_complete(total_sections, time.time() - start_time)
            return report
        except Exception as exc:
            logger.error(f"World report generation failed: {exc}")
            if self.report_logger:
                self.report_logger.log_error(str(exc), "world_report")
            ReportManager.update_progress(report_id, "failed", 100, str(exc))
            return Report(
                report_id=report_id,
                simulation_id=self.simulation_id,
                graph_id=self.graph_id,
                simulation_requirement=self.simulation_requirement,
                status=ReportStatus.FAILED,
                simulation_mode=SimulationMode.WORLD.value,
                created_at=datetime.now().isoformat(),
                completed_at=datetime.now().isoformat(),
                error=str(exc),
            )
        finally:
            if self.console_logger:
                self.console_logger.close()

    def chat(self, message: str, chat_history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        context = self._load_world_context()
        report = ReportManager.get_report_by_simulation(self.simulation_id)
        report_text = report.markdown_content if report else ""
        history = chat_history or []
        answer = self._answer_question(message, history, context, report_text)
        return {
            "response": answer,
            "simulation_mode": SimulationMode.WORLD.value,
            "used_sources": ["world_actions", "world_state", "report_markdown" if report_text else "world_config"],
        }

    def _load_world_context(self) -> Dict[str, Any]:
        sim_dir = os.path.join(Config.UPLOAD_FOLDER, "simulations", self.simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        state_path = os.path.join(sim_dir, "world", "world_state.json")
        actions_path = os.path.join(sim_dir, "world", "actions.jsonl")

        config = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

        world_state = {}
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                world_state = json.load(f)

        actions = []
        if os.path.exists(actions_path):
            with open(actions_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not payload.get("action_type"):
                        continue
                    actions.append(payload)

        action_types = Counter(action.get("action_type", "UNKNOWN") for action in actions)
        top_actors = Counter(action.get("agent_name", "Unknown") for action in actions)
        return {
            "config": config,
            "world_state": world_state,
            "actions": actions,
            "total_actions": len(actions),
            "action_types": dict(action_types.most_common(8)),
            "top_actors": dict(top_actors.most_common(6)),
        }

    def _build_outline(self, context: Dict[str, Any]) -> ReportOutline:
        default_outline = ReportOutline(
            title="世界观自动推进报告",
            summary="从初始世界设定出发，归纳主要驱动力、剧情推进轨迹、关键转折和后续风险。",
            sections=[
                ReportSection(title="世界起点与驱动矛盾"),
                ReportSection(title="推进轨迹与事件链"),
                ReportSection(title="关键角色与势力变化"),
                ReportSection(title="后续风险与可操作建议"),
            ],
        )

        if not self.llm:
            return default_outline

        try:
            response = self.llm.chat_json(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你在为世界观推进模拟撰写报告大纲。"
                            "返回 JSON，字段包含 title,summary,sections。"
                            "sections 是对象数组，每项包含 title。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "simulation_requirement": self.simulation_requirement,
                                "world_state": context.get("world_state", {}),
                                "action_types": context.get("action_types", {}),
                                "top_actors": context.get("top_actors", {}),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.4,
                max_tokens=700,
                timeout=WORLD_REPORT_LLM_TIMEOUT_SECONDS,
            )
            sections = [
                ReportSection(title=item.get("title", f"章节 {index + 1}"))
                for index, item in enumerate(response.get("sections", []))
            ]
            if sections:
                return ReportOutline(
                    title=response.get("title", default_outline.title),
                    summary=response.get("summary", default_outline.summary),
                    sections=sections[:6],
                )
        except Exception as exc:
            logger.warning(f"World outline generation fallback: {exc}")

        return default_outline

    def _build_section_content(self, section_title: str, context: Dict[str, Any]) -> str:
        actions = context.get("actions", [])
        snippets = [
            {
                "round": action.get("round"),
                "actor": action.get("agent_name"),
                "type": action.get("action_type"),
                "content": (
                    action.get("result")
                    or action.get("summary")
                    or action.get("title")
                    or action.get("action_args", {}).get("summary")
                    or action.get("action_args", {}).get("objective")
                    or ""
                ),
            }
            for action in actions[:20]
        ]

        if not self.llm:
            return self._fallback_section_content(section_title, context, snippets)

        try:
            response = self.llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你在撰写世界观自动推进报告的单章内容。"
                            "输出中文 markdown，聚焦具体事件、角色变化和下一阶段含义。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "section_title": section_title,
                                "simulation_requirement": self.simulation_requirement,
                                "world_state": context.get("world_state", {}),
                                "action_types": context.get("action_types", {}),
                                "top_actors": context.get("top_actors", {}),
                                "event_snippets": snippets,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.4,
                max_tokens=1200,
                timeout=WORLD_REPORT_LLM_TIMEOUT_SECONDS,
            )
            if response.strip():
                return response.strip()
        except Exception as exc:
            logger.warning(f"World section generation fallback: {exc}")

        return self._fallback_section_content(section_title, context, snippets)

    def _fallback_section_content(
        self,
        section_title: str,
        context: Dict[str, Any],
        snippets: List[Dict[str, Any]],
    ) -> str:
        lines = [f"本章节围绕“{section_title}”整理 world 模式的推进结果。", ""]
        if context.get("world_state", {}).get("core_tensions"):
            lines.append("**核心矛盾**")
            for item in context["world_state"]["core_tensions"][:4]:
                lines.append(f"- {item}")
            lines.append("")
        if context.get("top_actors"):
            lines.append("**高活跃实体**")
            for name, count in context["top_actors"].items():
                lines.append(f"- {name}: {count} 次关键行动")
            lines.append("")
        if snippets:
            lines.append("**代表性事件**")
            for snippet in snippets[:5]:
                lines.append(
                    f"- Tick {snippet['round']}: {snippet['actor']} / {snippet['type']} / {snippet['content']}"
                )
        return "\n".join(lines).strip()

    def _answer_question(
        self,
        message: str,
        chat_history: List[Dict[str, str]],
        context: Dict[str, Any],
        report_text: str,
    ) -> str:
        if not self.llm:
            top_actor = next(iter(context.get("top_actors", {}) or {}), "当前没有明确主导者")
            return (
                f"基于 world 模式当前结果，最活跃的推动者是 {top_actor}。"
                f" 当前共记录 {context.get('total_actions', 0)} 条推进事件。"
            )

        history = chat_history[-6:]
        try:
            return self.llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 MiroFish 的 world report agent。"
                            "基于世界推进日志回答问题，优先引用具体角色、事件和 tension 变化。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "question": message,
                                "chat_history": history,
                                "world_state": context.get("world_state", {}),
                                "top_actors": context.get("top_actors", {}),
                                "action_types": context.get("action_types", {}),
                                "report_excerpt": report_text[:4000],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.4,
                max_tokens=900,
                timeout=WORLD_REPORT_LLM_TIMEOUT_SECONDS,
            ).strip()
        except Exception as exc:
            logger.warning(f"World chat fallback: {exc}")
            snippets = [
                {
                    "round": action.get("round"),
                    "actor": action.get("agent_name"),
                    "type": action.get("action_type"),
                    "content": (
                        action.get("result")
                        or action.get("summary")
                        or action.get("title")
                        or action.get("action_args", {}).get("summary")
                        or action.get("action_args", {}).get("objective")
                        or ""
                    ),
                }
                for action in context.get("actions", [])[:3]
            ]
            return self._fallback_section_content("问答上下文", context, snippets)
