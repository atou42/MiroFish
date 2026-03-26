"""
World mode report generation and chat.
"""

import json
import os
import signal
import threading
import time
from collections import Counter
from contextlib import contextmanager
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


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


class WorldReportLLMTimeoutError(TimeoutError):
    """Raised when a world report LLM call exceeds the hard timeout."""


@contextmanager
def _hard_timeout(seconds: float, label: str):
    timeout_seconds = float(seconds or 0)
    supports_alarm = (
        timeout_seconds > 0
        and threading.current_thread() is threading.main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and hasattr(signal, "getitimer")
        and hasattr(signal, "ITIMER_REAL")
    )
    if not supports_alarm:
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def _handler(signum, frame):
        raise WorldReportLLMTimeoutError(f"{label} timed out after {timeout_seconds:.2f}s")

    signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer and (previous_timer[0] > 0 or previous_timer[1] > 0):
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


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

    def _llm_timeout_seconds(self) -> float:
        raw = _clean_text(os.environ.get("WORLD_REPORT_LLM_TIMEOUT_SECONDS"))
        if raw:
            try:
                return max(float(raw), 0.0)
            except ValueError:
                logger.warning(f"Invalid WORLD_REPORT_LLM_TIMEOUT_SECONDS value: {raw}")
        return WORLD_REPORT_LLM_TIMEOUT_SECONDS

    def _call_llm_with_hard_timeout(self, fn: Callable[[], Any], *, label: str) -> Any:
        timeout_seconds = self._llm_timeout_seconds()
        if timeout_seconds <= 0:
            return fn()

        result: Dict[str, Any] = {}
        error: Dict[str, BaseException] = {}
        finished = threading.Event()

        def _run() -> None:
            try:
                result["value"] = fn()
            except BaseException as exc:  # pragma: no cover - surfaced to caller below
                error["value"] = exc
            finally:
                finished.set()

        worker = threading.Thread(
            target=_run,
            name=f"world-report-llm:{label}",
            daemon=True,
        )
        worker.start()
        finished.wait(timeout_seconds)
        if not finished.is_set():
            raise WorldReportLLMTimeoutError(f"{label} timed out after {timeout_seconds:.2f}s")
        if "value" in error:
            raise error["value"]
        return result.get("value")

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
        snapshots_path = os.path.join(sim_dir, "world", "state_snapshots.jsonl")

        config = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

        world_state = {}
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                world_state = json.load(f)
        if isinstance(world_state, dict) and isinstance(world_state.get("world_state"), dict):
            world_state = world_state.get("world_state") or {}

        actions = []
        lifecycle_events = []
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
                    if payload.get("action_type"):
                        actions.append(payload)
                    if payload.get("event_type"):
                        lifecycle_events.append(payload)

        snapshots = []
        if os.path.exists(snapshots_path):
            with open(snapshots_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        snapshots.append(payload)

        action_types = Counter(action.get("action_type", "UNKNOWN") for action in actions)
        top_actors = Counter(action.get("agent_name", "Unknown") for action in actions)
        tick_summaries = [
            {
                "tick": payload.get("tick") or payload.get("round"),
                "summary": _clean_text(payload.get("summary")),
                "active_events_count": payload.get("active_events_count"),
                "queued_events_count": payload.get("queued_events_count"),
                "completed_events_count": payload.get("completed_events_count"),
                "scene_title": payload.get("scene_title"),
            }
            for payload in lifecycle_events
            if str(payload.get("event_type") or "").strip().lower() == "tick_end"
        ]
        final_event = next(
            (
                payload
                for payload in reversed(lifecycle_events)
                if str(payload.get("event_type") or "").strip().lower() == "simulation_end"
            ),
            {},
        )
        recent_completed_events = [
            {
                "tick": payload.get("tick") or payload.get("round"),
                "actor": payload.get("agent_name"),
                "title": payload.get("title"),
                "summary": payload.get("summary") or payload.get("result"),
            }
            for payload in actions
            if str(payload.get("action_type") or "").strip().upper() == "EVENT_COMPLETED"
        ]
        recent_started_events = [
            {
                "tick": payload.get("tick") or payload.get("round"),
                "actor": payload.get("agent_name"),
                "title": payload.get("title"),
                "summary": payload.get("summary") or payload.get("result"),
            }
            for payload in actions
            if str(payload.get("action_type") or "").strip().upper() in {"EVENT_STARTED", "EVENT_QUEUED"}
        ]
        return {
            "config": config,
            "world_state": world_state,
            "actions": actions,
            "lifecycle_events": lifecycle_events,
            "snapshots": snapshots,
            "total_actions": len(actions),
            "action_types": dict(action_types.most_common(8)),
            "top_actors": dict(top_actors.most_common(6)),
            "tick_summaries": tick_summaries,
            "final_event": final_event if isinstance(final_event, dict) else {},
            "recent_completed_events": recent_completed_events,
            "recent_started_events": recent_started_events,
        }

    def _sample_items(self, items: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
        if len(items) <= count:
            return list(items)
        if count <= 0:
            return []
        indexes = sorted({int(round(i * (len(items) - 1) / max(count - 1, 1))) for i in range(count)})
        return [items[index] for index in indexes]

    def _section_lens(self, section_title: str) -> str:
        lowered = _clean_text(section_title)
        if any(token in lowered for token in ("起点", "驱动", "矛盾")):
            return "foundation"
        if any(token in lowered for token in ("轨迹", "事件链", "推进")):
            return "trajectory"
        if any(token in lowered for token in ("角色", "势力", "变化")):
            return "actors"
        return "risks"

    def _build_section_payload(self, section_title: str, context: Dict[str, Any]) -> Dict[str, Any]:
        lens = self._section_lens(section_title)
        config = context.get("config", {}) or {}
        world_state = context.get("world_state", {}) or {}
        tick_summaries = context.get("tick_summaries", []) or []
        snapshots = context.get("snapshots", []) or []
        final_event = context.get("final_event", {}) or {}
        recent_completed_events = context.get("recent_completed_events", []) or []
        recent_started_events = context.get("recent_started_events", []) or []

        early_ticks = tick_summaries[:3]
        middle_ticks = self._sample_items(tick_summaries[1:-1], 3) if len(tick_summaries) > 4 else tick_summaries[1:-1]
        late_ticks = tick_summaries[-4:]

        payload: Dict[str, Any] = {
            "lens": lens,
            "simulation_requirement": self.simulation_requirement,
            "starting_condition": (
                world_state.get("starting_condition")
                or config.get("initial_world_state", {}).get("starting_condition")
            ),
            "focus_threads": world_state.get("focus_threads") or [],
            "final_world_state": {
                "tension": world_state.get("tension") or (final_event.get("world_state") or {}).get("tension"),
                "stability": world_state.get("stability") or (final_event.get("world_state") or {}).get("stability"),
                "momentum": world_state.get("momentum") or (final_event.get("world_state") or {}).get("momentum"),
                "pressure_tracks": world_state.get("pressure_tracks") or (final_event.get("world_state") or {}).get("pressure_tracks") or {},
                "last_tick_summary": world_state.get("last_tick_summary") or (final_event.get("world_state") or {}).get("last_tick_summary"),
            },
            "top_actors": context.get("top_actors", {}),
        }

        if lens == "foundation":
            payload["tick_samples"] = early_ticks
            payload["initial_pressure_tracks"] = config.get("pressure_tracks", [])[:6]
            payload["opening_completed_events"] = recent_completed_events[:5]
        elif lens == "trajectory":
            payload["tick_windows"] = {
                "early": early_ticks,
                "middle": middle_ticks,
                "late": late_ticks,
            }
            payload["trajectory_events"] = self._sample_items(recent_completed_events, 8)
            payload["snapshot_samples"] = [
                {
                    "tick": item.get("tick") or item.get("round"),
                    "summary": item.get("summary"),
                    "metrics": item.get("metrics", {}),
                }
                for item in self._sample_items(snapshots, 6)
            ]
        elif lens == "actors":
            payload["latest_completed_events"] = recent_completed_events[-8:]
            payload["latest_started_events"] = recent_started_events[-8:]
            payload["late_tick_samples"] = late_ticks
        else:
            payload["late_tick_samples"] = late_ticks
            payload["unresolved_events"] = (final_event.get("unresolved_events") or [])[:8]
            payload["active_events_count"] = final_event.get("active_events_count")
            payload["queued_events_count"] = final_event.get("queued_events_count")
            payload["completed_events_count"] = final_event.get("completed_events_count")
            payload["latest_completed_events"] = recent_completed_events[-5:]

        return payload

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
            timeout_seconds = self._llm_timeout_seconds()
            response = self._call_llm_with_hard_timeout(
                lambda: self.llm.chat_json(
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
                    timeout=timeout_seconds,
                ),
                label="world report outline llm call",
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
            if self.report_logger:
                self.report_logger.log_error(
                    f"outline llm failed, using fallback: {exc}",
                    "planning",
                )
            logger.warning(f"World outline generation fallback: {exc}")

        return default_outline

    def _build_section_content(self, section_title: str, context: Dict[str, Any]) -> str:
        payload = self._build_section_payload(section_title, context)

        if not self.llm:
            return self._fallback_section_content(section_title, context, payload)

        try:
            timeout_seconds = self._llm_timeout_seconds()
            response = self._call_llm_with_hard_timeout(
                lambda: self.llm.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你在撰写世界观自动推进报告的单章内容。"
                                "输出中文 markdown，聚焦具体事件、角色变化和下一阶段含义。"
                                "不要重复其它章节，不要只复述 Tick 1。"
                                "必须基于 section_payload 的视角写出这一章。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "section_title": section_title,
                                    "section_payload": payload,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    temperature=0.4,
                    max_tokens=1200,
                    timeout=timeout_seconds,
                ),
                label=f"world report section llm call [{section_title}]",
            )
            if response.strip():
                return response.strip()
        except Exception as exc:
            if self.report_logger:
                self.report_logger.log_error(
                    f"section llm failed, using fallback: {exc}",
                    "generating",
                    section_title=section_title,
                )
            logger.warning(f"World section generation fallback: {exc}")

        return self._fallback_section_content(section_title, context, payload)

    def _fallback_section_content(
        self,
        section_title: str,
        context: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> str:
        lines = [f"本章节围绕“{section_title}”整理 world 模式的推进结果。", ""]
        lens = payload.get("lens")
        if payload.get("starting_condition"):
            lines.append(f"起点条件：{payload['starting_condition']}")
            lines.append("")
        if payload.get("focus_threads"):
            lines.append("**主线焦点**")
            for item in payload["focus_threads"][:6]:
                lines.append(f"- {item}")
            lines.append("")

        if lens == "foundation":
            lines.append("**开局样本**")
            for item in payload.get("tick_samples", [])[:4]:
                lines.append(
                    f"- Tick {item.get('tick')}: {item.get('summary')}"
                )
            if payload.get("opening_completed_events"):
                lines.append("")
                lines.append("**最早落地的动作**")
                for item in payload["opening_completed_events"][:5]:
                    lines.append(
                        f"- Tick {item.get('tick')}: {item.get('actor')} / {item.get('title')} / {item.get('summary')}"
                    )
        elif lens == "trajectory":
            lines.append("**阶段推进**")
            for label, items in payload.get("tick_windows", {}).items():
                if not items:
                    continue
                lines.append(f"- {label}: " + " | ".join(
                    f"Tick {item.get('tick')} {item.get('summary')}" for item in items[:3]
                ))
            if payload.get("trajectory_events"):
                lines.append("")
                lines.append("**关键事件链**")
                for item in payload["trajectory_events"][:8]:
                    lines.append(
                        f"- Tick {item.get('tick')}: {item.get('actor')} / {item.get('title')} / {item.get('summary')}"
                    )
        elif lens == "actors":
            if context.get("top_actors"):
                lines.append("**高活跃实体**")
                for name, count in context["top_actors"].items():
                    lines.append(f"- {name}: {count} 次关键行动")
                lines.append("")
            lines.append("**近期势力动作**")
            for item in payload.get("latest_completed_events", [])[:8]:
                lines.append(
                    f"- Tick {item.get('tick')}: {item.get('actor')} / {item.get('title')} / {item.get('summary')}"
                )
            if payload.get("late_tick_samples"):
                lines.append("")
                lines.append("**晚期局势信号**")
                for item in payload["late_tick_samples"][:4]:
                    lines.append(f"- Tick {item.get('tick')}: {item.get('summary')}")
        else:
            final_world_state = payload.get("final_world_state", {})
            lines.append("**最终态势**")
            lines.append(
                f"- tension={final_world_state.get('tension')}, "
                f"stability={final_world_state.get('stability')}, "
                f"momentum={final_world_state.get('momentum')}"
            )
            if final_world_state.get("last_tick_summary"):
                lines.append(f"- 最后一轮摘要: {final_world_state.get('last_tick_summary')}")
            if payload.get("unresolved_events"):
                lines.append("")
                lines.append("**未收束风险**")
                for item in payload["unresolved_events"][:6]:
                    lines.append(
                        f"- {item.get('title')}: {item.get('summary')}"
                    )
            if payload.get("latest_completed_events"):
                lines.append("")
                lines.append("**临近终局的已落地动作**")
                for item in payload["latest_completed_events"][:5]:
                    lines.append(
                        f"- Tick {item.get('tick')}: {item.get('actor')} / {item.get('title')} / {item.get('summary')}"
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
