"""
OASIS模拟运行器
在后台运行模拟并记录每个Agent的动作，支持实时状态监控
"""

import os
import sys
import json
import time
import math
import asyncio
import threading
import subprocess
import signal
import atexit
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from queue import Queue

from ..config import Config
from ..utils.logger import get_logger
from ..utils.world_run_lock import (
    cleanup_world_run_lease,
    inspect_world_run_lease,
    world_run_paths_for_simulation_dir,
)
from ..models.simulation_mode import SimulationMode
from .zep_graph_memory_updater import ZepGraphMemoryManager
from .simulation_ipc import SimulationIPCClient, CommandType, IPCResponse

logger = get_logger('mirofish.simulation_runner')

# 标记是否已注册清理函数
_cleanup_registered = False

# 平台检测
IS_WINDOWS = sys.platform == 'win32'


class RunnerStatus(str, Enum):
    """运行器状态"""
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentAction:
    """Agent动作记录"""
    round_num: int
    timestamp: str
    platform: str  # twitter / reddit
    agent_id: int
    agent_name: str
    action_type: str  # CREATE_POST, LIKE_POST, etc.
    action_args: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    success: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_num": self.round_num,
            "timestamp": self.timestamp,
            "platform": self.platform,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "action_type": self.action_type,
            "action_args": self.action_args,
            "result": self.result,
            "success": self.success,
        }


@dataclass
class RoundSummary:
    """每轮摘要"""
    round_num: int
    start_time: str
    end_time: Optional[str] = None
    simulated_hour: int = 0
    twitter_actions: int = 0
    reddit_actions: int = 0
    active_agents: List[int] = field(default_factory=list)
    actions: List[AgentAction] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_num": self.round_num,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "simulated_hour": self.simulated_hour,
            "twitter_actions": self.twitter_actions,
            "reddit_actions": self.reddit_actions,
            "active_agents": self.active_agents,
            "actions_count": len(self.actions),
            "actions": [a.to_dict() for a in self.actions],
        }


@dataclass
class SimulationRunState:
    """模拟运行状态（实时）"""
    simulation_id: str
    simulation_mode: str = SimulationMode.SOCIAL.value
    runner_status: RunnerStatus = RunnerStatus.IDLE
    
    # 进度信息
    current_round: int = 0
    total_rounds: int = 0
    simulated_hours: int = 0
    total_simulation_hours: int = 0
    
    # 各平台独立轮次和模拟时间（用于双平台并行显示）
    twitter_current_round: int = 0
    reddit_current_round: int = 0
    twitter_simulated_hours: int = 0
    reddit_simulated_hours: int = 0
    
    # 平台状态
    twitter_running: bool = False
    reddit_running: bool = False
    world_running: bool = False
    twitter_actions_count: int = 0
    reddit_actions_count: int = 0
    world_actions_count: int = 0
    
    # 平台完成状态（通过检测 actions.jsonl 中的 simulation_end 事件）
    twitter_completed: bool = False
    reddit_completed: bool = False
    world_completed: bool = False
    
    # 每轮摘要
    rounds: List[RoundSummary] = field(default_factory=list)
    
    # 最近动作（用于前端实时展示）
    recent_actions: List[AgentAction] = field(default_factory=list)
    max_recent_actions: int = 50
    world_recent_events: List[Dict[str, Any]] = field(default_factory=list)
    max_world_recent_events: int = 120
    world_active_events: List[Dict[str, Any]] = field(default_factory=list)
    world_queued_events: List[Dict[str, Any]] = field(default_factory=list)
    world_phase_counts: Dict[str, int] = field(default_factory=dict)
    world_completed_events_count: int = 0
    world_current_phase: str = "idle"
    latest_snapshot: Optional[Dict[str, Any]] = None
    
    # 时间戳
    started_at: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    
    # 错误信息
    error: Optional[str] = None
    terminal_status: str = ""
    stop_reason: str = ""
    
    # 进程ID（用于停止）
    process_pid: Optional[int] = None
    
    def add_action(self, action: AgentAction):
        """添加动作到最近动作列表"""
        self.recent_actions.insert(0, action)
        if len(self.recent_actions) > self.max_recent_actions:
            self.recent_actions = self.recent_actions[:self.max_recent_actions]
        
        if action.platform == "twitter":
            self.twitter_actions_count += 1
        elif action.platform == "reddit":
            self.reddit_actions_count += 1
        else:
            self.world_actions_count += 1
        
        self.updated_at = datetime.now().isoformat()

    def add_world_event(self, event: Dict[str, Any]):
        """添加 world 生命周期记录并维护并发状态摘要。"""
        raw_type = event.get("event_type") or event.get("action_type")
        event_type = str(raw_type or "").strip().lower()
        if not event_type:
            return

        self.world_phase_counts[event_type] = self.world_phase_counts.get(event_type, 0) + 1
        normalized = self._normalize_world_event(event, event_type)

        if event_type == "tick_blocked":
            self.world_current_phase = event_type
        elif event_type in {"tick_start", "tick_end", "provider_waiting", "provider_recovered"}:
            self.world_current_phase = normalized.get("phase") or event_type
        elif event_type == "simulation_end":
            self.world_current_phase = "completed"
        elif event_type == "simulation_failed":
            self.world_current_phase = "failed"

        if event_type in {
            "intent_created",
            "intent_resolved",
            "intent_deferred",
            "event_started",
            "event_queued",
            "event_completed",
            "provider_waiting",
            "provider_recovered",
            "tick_blocked",
        }:
            self.world_recent_events.insert(0, normalized)
            if len(self.world_recent_events) > self.max_world_recent_events:
                self.world_recent_events = self.world_recent_events[:self.max_world_recent_events]

        event_id = normalized.get("event_id")
        if event_id:
            if event_type == "intent_resolved" and normalized.get("resolution_status") == "queued":
                queued_event = normalized.get("event") or normalized
                self._upsert_world_event(self.world_queued_events, queued_event)
            elif event_type == "event_queued":
                self._upsert_world_event(self.world_queued_events, normalized)
            elif event_type == "event_started":
                self._remove_world_event(self.world_queued_events, event_id)
                self._upsert_world_event(self.world_active_events, normalized)
            elif event_type == "event_completed":
                self._remove_world_event(self.world_active_events, event_id)
                self._remove_world_event(self.world_queued_events, event_id)
                self.world_completed_events_count += 1

        self.updated_at = datetime.now().isoformat()

    def _normalize_world_event(self, event: Dict[str, Any], event_type: str) -> Dict[str, Any]:
        action_args = event.get("action_args") or {}
        embedded_event = action_args.get("event") if isinstance(action_args.get("event"), dict) else {}
        payload = embedded_event if embedded_event else action_args
        return {
            "event_type": event_type,
            "timestamp": event.get("timestamp"),
            "round": event.get("round") or event.get("tick") or payload.get("tick"),
            "tick": event.get("round") or event.get("tick") or payload.get("tick"),
            "phase": event.get("phase"),
            "agent_id": event.get("agent_id"),
            "agent_name": event.get("agent_name"),
            "action_type": event.get("action_type"),
            "intent_id": action_args.get("intent_id"),
            "objective": action_args.get("objective"),
            "resolution_status": action_args.get("resolution_status"),
            "reason": action_args.get("reason") or event.get("reason"),
            "event_id": payload.get("event_id"),
            "title": payload.get("title"),
            "summary": payload.get("summary") or action_args.get("summary") or event.get("summary") or event.get("result"),
            "priority": payload.get("priority"),
            "duration_ticks": payload.get("duration_ticks"),
            "resolves_at_tick": payload.get("resolves_at_tick"),
            "participants": payload.get("participants") or action_args.get("participants") or [],
            "location": payload.get("location") or action_args.get("location"),
            "resource": payload.get("resource") or action_args.get("resource"),
            "target": payload.get("target") or action_args.get("target"),
            "status": payload.get("status") or action_args.get("status"),
            "state_impacts": payload.get("state_impacts") or action_args.get("state_impacts") or {},
            "provider_role": event.get("provider_role"),
            "wait_seconds": event.get("wait_seconds"),
            "context": event.get("context"),
            "event": embedded_event or None,
        }

    def update_world_snapshot(self, snapshot: Dict[str, Any]):
        self.latest_snapshot = snapshot
        if "active_events" in snapshot:
            self.world_active_events = list(snapshot.get("active_events") or [])
        if "queued_events" in snapshot:
            self.world_queued_events = list(snapshot.get("queued_events") or [])
        self.world_current_phase = snapshot.get("phase", self.world_current_phase)
        counts = snapshot.get("counts") or snapshot.get("metrics") or {}
        self.world_completed_events_count = counts.get(
            "completed_events_count",
            self.world_completed_events_count,
        )
        lifecycle_counters = counts.get("lifecycle_counters") or {}
        if lifecycle_counters:
            for key, value in lifecycle_counters.items():
                self.world_phase_counts[str(key).strip().lower()] = int(value)
        self.current_round = max(
            self.current_round,
            snapshot.get("round", 0) or snapshot.get("tick", 0),
        )
        self.simulated_hours = snapshot.get("simulated_hours", self.simulated_hours)
        self.updated_at = datetime.now().isoformat()

    def _upsert_world_event(self, container: List[Dict[str, Any]], event: Dict[str, Any]):
        event_id = event.get("event_id")
        if not event_id:
            return

        simplified = {
            "event_id": event_id,
            "tick": event.get("tick") or event.get("round"),
            "title": event.get("title"),
            "summary": event.get("summary"),
            "action_type": event.get("action_type"),
            "priority": event.get("priority"),
            "duration_ticks": event.get("duration_ticks"),
            "remaining_ticks": event.get("remaining_ticks"),
            "resolves_at_tick": event.get("resolves_at_tick"),
            "participants": event.get("participants") or [],
            "location": event.get("location"),
            "resource": event.get("resource"),
            "target": event.get("target"),
            "status": event.get("status"),
        }

        for idx, item in enumerate(container):
            if item.get("event_id") == event_id:
                container[idx] = simplified
                return

        container.insert(0, simplified)

    def _remove_world_event(self, container: List[Dict[str, Any]], event_id: str):
        container[:] = [item for item in container if item.get("event_id") != event_id]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "simulation_mode": self.simulation_mode,
            "runner_status": self.runner_status.value,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "simulated_hours": self.simulated_hours,
            "total_simulation_hours": self.total_simulation_hours,
            "progress_percent": round(self.current_round / max(self.total_rounds, 1) * 100, 1),
            # 各平台独立轮次和时间
            "twitter_current_round": self.twitter_current_round,
            "reddit_current_round": self.reddit_current_round,
            "twitter_simulated_hours": self.twitter_simulated_hours,
            "reddit_simulated_hours": self.reddit_simulated_hours,
            "twitter_running": self.twitter_running,
            "reddit_running": self.reddit_running,
            "world_running": self.world_running,
            "twitter_completed": self.twitter_completed,
            "reddit_completed": self.reddit_completed,
            "world_completed": self.world_completed,
            "twitter_actions_count": self.twitter_actions_count,
            "reddit_actions_count": self.reddit_actions_count,
            "world_actions_count": self.world_actions_count,
            "total_actions_count": self.twitter_actions_count + self.reddit_actions_count + self.world_actions_count,
            "world_active_events_count": len(self.world_active_events),
            "world_queued_events_count": len(self.world_queued_events),
            "world_completed_events_count": self.world_completed_events_count,
            "world_current_phase": self.world_current_phase,
            "world_phase_counts": self.world_phase_counts,
            "world_intents_created_count": self.world_phase_counts.get("intent_created", 0),
            "world_resolved_intents_count": self.world_phase_counts.get("intent_resolved", 0),
            "world_events_started_count": self.world_phase_counts.get("event_started", 0),
            "world_events_completed_count": self.world_phase_counts.get("event_completed", self.world_completed_events_count),
            "world_provider_waits_count": self.world_phase_counts.get("provider_waiting", 0),
            "world_ticks_blocked_count": self.world_phase_counts.get("tick_blocked", 0),
            "world_intents_deferred_count": self.world_phase_counts.get("intent_deferred", 0),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "terminal_status": self.terminal_status,
            "stop_reason": self.stop_reason,
            "process_pid": self.process_pid,
        }
    
    def to_detail_dict(self) -> Dict[str, Any]:
        """包含最近动作的详细信息"""
        result = self.to_dict()
        result["recent_actions"] = [a.to_dict() for a in self.recent_actions]
        result["world_recent_events"] = self.world_recent_events
        result["world_active_events"] = self.world_active_events
        result["world_queued_events"] = self.world_queued_events
        result["latest_snapshot"] = self.latest_snapshot
        result["rounds_count"] = len(self.rounds)
        return result


class SimulationRunner:
    """
    模拟运行器
    
    负责：
    1. 在后台进程中运行OASIS模拟
    2. 解析运行日志，记录每个Agent的动作
    3. 提供实时状态查询接口
    4. 支持暂停/停止/恢复操作
    """
    
    # 运行状态存储目录
    RUN_STATE_DIR = os.path.join(
        os.path.dirname(__file__),
        '../../uploads/simulations'
    )
    
    # 脚本目录
    SCRIPTS_DIR = os.path.join(
        os.path.dirname(__file__),
        '../../scripts'
    )
    
    # 内存中的运行状态
    _run_states: Dict[str, SimulationRunState] = {}
    _processes: Dict[str, subprocess.Popen] = {}
    _action_queues: Dict[str, Queue] = {}
    _monitor_threads: Dict[str, threading.Thread] = {}
    _stdout_files: Dict[str, Any] = {}  # 存储 stdout 文件句柄
    _stderr_files: Dict[str, Any] = {}  # 存储 stderr 文件句柄
    
    # 图谱记忆更新配置
    _graph_memory_enabled: Dict[str, bool] = {}  # simulation_id -> enabled
    
    @classmethod
    def get_run_state(cls, simulation_id: str) -> Optional[SimulationRunState]:
        """获取运行状态"""
        if simulation_id in cls._run_states:
            state = cls._run_states[simulation_id]
            if (
                SimulationMode.normalize(state.simulation_mode).value == SimulationMode.WORLD.value
                and not cls._has_live_process(simulation_id)
            ):
                refreshed = cls.refresh_world_run_state_from_artifacts(simulation_id, persist=True)
                if refreshed:
                    return refreshed
            return cls._reconcile_world_run_state(simulation_id, state, persist=True)
        
        # 尝试从文件加载
        state = cls._load_run_state(simulation_id)
        if state:
            cls._run_states[simulation_id] = state
            if (
                SimulationMode.normalize(state.simulation_mode).value == SimulationMode.WORLD.value
                and not cls._has_live_process(simulation_id)
            ):
                refreshed = cls.refresh_world_run_state_from_artifacts(simulation_id, persist=True)
                if refreshed:
                    return refreshed
            return cls._reconcile_world_run_state(simulation_id, state, persist=True)

        refreshed = cls.refresh_world_run_state_from_artifacts(simulation_id, persist=True)
        if refreshed:
            return refreshed
        return None
    
    @classmethod
    def _load_run_state(cls, simulation_id: str) -> Optional[SimulationRunState]:
        """从文件加载运行状态"""
        state_file = os.path.join(cls.RUN_STATE_DIR, simulation_id, "run_state.json")
        if not os.path.exists(state_file):
            return None
        
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            state = SimulationRunState(
                simulation_id=simulation_id,
                simulation_mode=SimulationMode.normalize(data.get("simulation_mode")).value,
                runner_status=RunnerStatus(data.get("runner_status", "idle")),
                current_round=data.get("current_round", 0),
                total_rounds=data.get("total_rounds", 0),
                simulated_hours=data.get("simulated_hours", 0),
                total_simulation_hours=data.get("total_simulation_hours", 0),
                # 各平台独立轮次和时间
                twitter_current_round=data.get("twitter_current_round", 0),
                reddit_current_round=data.get("reddit_current_round", 0),
                twitter_simulated_hours=data.get("twitter_simulated_hours", 0),
                reddit_simulated_hours=data.get("reddit_simulated_hours", 0),
                twitter_running=data.get("twitter_running", False),
                reddit_running=data.get("reddit_running", False),
                world_running=data.get("world_running", False),
                twitter_completed=data.get("twitter_completed", False),
                reddit_completed=data.get("reddit_completed", False),
                world_completed=data.get("world_completed", False),
                twitter_actions_count=data.get("twitter_actions_count", 0),
                reddit_actions_count=data.get("reddit_actions_count", 0),
                world_actions_count=data.get("world_actions_count", 0),
                started_at=data.get("started_at"),
                updated_at=data.get("updated_at", datetime.now().isoformat()),
                completed_at=data.get("completed_at"),
                error=data.get("error"),
                terminal_status=data.get("terminal_status", ""),
                stop_reason=data.get("stop_reason", ""),
                process_pid=data.get("process_pid"),
                world_recent_events=data.get("world_recent_events", []),
                world_active_events=data.get("world_active_events", []),
                world_queued_events=data.get("world_queued_events", []),
                world_phase_counts=data.get("world_phase_counts", {}),
                world_completed_events_count=data.get("world_completed_events_count", 0),
                world_current_phase=data.get("world_current_phase", "idle"),
                latest_snapshot=data.get("latest_snapshot"),
            )
            
            # 加载最近动作
            actions_data = data.get("recent_actions", [])
            for a in actions_data:
                state.recent_actions.append(AgentAction(
                    round_num=a.get("round_num", 0),
                    timestamp=a.get("timestamp", ""),
                    platform=a.get("platform", ""),
                    agent_id=a.get("agent_id", 0),
                    agent_name=a.get("agent_name", ""),
                    action_type=a.get("action_type", ""),
                    action_args=a.get("action_args", {}),
                    result=a.get("result"),
                    success=a.get("success", True),
                ))
            
            return state
        except Exception as e:
            logger.error(f"加载运行状态失败: {str(e)}")
            return None
    
    @classmethod
    def _save_run_state(cls, state: SimulationRunState):
        """保存运行状态到文件"""
        sim_dir = os.path.join(cls.RUN_STATE_DIR, state.simulation_id)
        os.makedirs(sim_dir, exist_ok=True)
        state_file = os.path.join(sim_dir, "run_state.json")
        
        data = state.to_detail_dict()
        
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        cls._run_states[state.simulation_id] = state

    @classmethod
    def _resolve_total_rounds(
        cls,
        config: Dict[str, Any],
        max_rounds: Optional[int] = None,
        allow_world_extension: bool = False,
    ) -> tuple[int, int, int]:
        simulation_mode = SimulationMode.normalize(config.get("simulation_mode")).value
        time_config = config.get("time_config", {})

        if simulation_mode == SimulationMode.WORLD.value:
            total_rounds = int(time_config.get("total_ticks", time_config.get("total_rounds", 12)))
            total_hours = int(time_config.get("total_simulation_hours", total_rounds))
            minutes_per_round = int(time_config.get("minutes_per_round", 60))
        else:
            total_hours = int(time_config.get("total_simulation_hours", 72))
            minutes_per_round = int(time_config.get("minutes_per_round", 30))
            total_rounds = int(total_hours * 60 / minutes_per_round)

        if max_rounds is not None and max_rounds > 0:
            if simulation_mode == SimulationMode.WORLD.value and allow_world_extension:
                total_rounds = max_rounds
                total_hours = max(total_hours, math.ceil(total_rounds * minutes_per_round / 60))
            else:
                total_rounds = min(total_rounds, max_rounds)

        return total_rounds, total_hours, minutes_per_round

    @classmethod
    def _world_checkpoint_path(cls, simulation_id: str) -> str:
        return os.path.join(cls.RUN_STATE_DIR, simulation_id, "world", "checkpoint.json")

    @classmethod
    def get_world_checkpoint(cls, simulation_id: str) -> Optional[Dict[str, Any]]:
        checkpoint_path = cls._world_checkpoint_path(simulation_id)
        if not os.path.exists(checkpoint_path):
            return None

        try:
            with open(checkpoint_path, 'r', encoding='utf-8') as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning(f"读取 world checkpoint 失败: {simulation_id}, error={e}")
            return None

        return payload if isinstance(payload, dict) else None

    @classmethod
    def get_world_checkpoint_meta(cls, simulation_id: str) -> Optional[Dict[str, Any]]:
        payload = cls.get_world_checkpoint(simulation_id)
        if not payload:
            return None

        last_snapshot = payload.get("last_snapshot") or {}
        active_events = payload.get("active_events") or []
        queued_events = payload.get("queued_events") or []
        completed_events = payload.get("completed_events") or []

        try:
            last_completed_tick = int(payload.get("last_completed_tick", 0) or 0)
        except (TypeError, ValueError):
            last_completed_tick = 0

        try:
            run_total_rounds = int(payload.get("run_total_rounds", 0) or 0)
        except (TypeError, ValueError):
            run_total_rounds = 0

        try:
            minutes_per_round = int(payload.get("minutes_per_round", 60) or 60)
        except (TypeError, ValueError):
            minutes_per_round = 60

        simulated_hours = last_snapshot.get("simulated_hours")
        if simulated_hours is None:
            simulated_hours = round(last_completed_tick * minutes_per_round / 60, 2)

        return {
            "checkpoint_available": True,
            "checkpoint_path": cls._world_checkpoint_path(simulation_id),
            "config_path": payload.get("config_path") or "",
            "saved_at": payload.get("saved_at"),
            "status": payload.get("status") or "running",
            "terminal_status": payload.get("terminal_status") or "",
            "stop_reason": payload.get("stop_reason") or "",
            "last_completed_tick": last_completed_tick,
            "run_total_rounds": run_total_rounds,
            "minutes_per_round": minutes_per_round,
            "simulated_hours": simulated_hours,
            "last_snapshot": last_snapshot if isinstance(last_snapshot, dict) else {},
            "active_events": active_events if isinstance(active_events, list) else [],
            "queued_events": queued_events if isinstance(queued_events, list) else [],
            "completed_events_count": len(completed_events) if isinstance(completed_events, list) else 0,
        }

    @classmethod
    def _load_simulation_config(
        cls,
        simulation_id: str,
    ) -> tuple[str, Dict[str, Any]]:
        config_path = os.path.join(cls.RUN_STATE_DIR, simulation_id, "simulation_config.json")
        if not os.path.exists(config_path):
            return config_path, {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning(f"读取模拟配置失败: {simulation_id}, error={e}")
            return config_path, {}
        return config_path, payload if isinstance(payload, dict) else {}

    @classmethod
    def _read_world_start_event(cls, sim_dir: str) -> Dict[str, Any]:
        actions_log = os.path.join(sim_dir, "world", "actions.jsonl")
        if not os.path.exists(actions_log):
            return {}

        try:
            with open(actions_log, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(payload.get("event_type") or "").strip().lower() == "simulation_start":
                        return payload if isinstance(payload, dict) else {}
        except Exception as e:
            logger.warning(f"读取 world start event 失败: {sim_dir}, error={e}")
        return {}

    @classmethod
    def _build_world_run_state_from_artifacts(
        cls,
        simulation_id: str,
        *,
        existing_state: Optional[SimulationRunState] = None,
        fallback_runner_status: Optional[RunnerStatus] = None,
        process_pid: Optional[int] = None,
    ) -> Optional[SimulationRunState]:
        config_path, config = cls._load_simulation_config(simulation_id)
        if config and SimulationMode.normalize(config.get("simulation_mode")).value != SimulationMode.WORLD.value:
            return None

        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        world_dir = os.path.join(sim_dir, "world")
        actions_log = os.path.join(world_dir, "actions.jsonl")
        snapshots_log = os.path.join(world_dir, "state_snapshots.jsonl")
        checkpoint_meta = cls.get_world_checkpoint_meta(simulation_id) or {}
        start_event = cls._read_world_start_event(sim_dir)

        existing_total_rounds = int(existing_state.total_rounds or 0) if existing_state else 0
        checkpoint_total_rounds = int(checkpoint_meta.get("run_total_rounds", 0) or 0)
        started_total_rounds = int(start_event.get("total_rounds", 0) or 0)
        desired_total_rounds = max(existing_total_rounds, checkpoint_total_rounds, started_total_rounds, 0)

        if config:
            total_rounds, total_hours, _ = cls._resolve_total_rounds(
                config,
                max_rounds=(desired_total_rounds or None),
                allow_world_extension=True,
            )
        else:
            total_rounds = desired_total_rounds
            minutes_per_round = int(checkpoint_meta.get("minutes_per_round", 60) or 60)
            total_hours = math.ceil(total_rounds * minutes_per_round / 60) if total_rounds else 0

        started_at = (
            (existing_state.started_at if existing_state else "")
            or str(start_event.get("timestamp") or "").strip()
            or None
        )
        state = SimulationRunState(
            simulation_id=simulation_id,
            simulation_mode=SimulationMode.WORLD.value,
            runner_status=RunnerStatus.IDLE,
            total_rounds=total_rounds,
            total_simulation_hours=total_hours,
            started_at=started_at,
        )

        if existing_state and existing_state.completed_at:
            state.completed_at = existing_state.completed_at

        if os.path.exists(actions_log):
            cls._read_action_log(actions_log, 0, state, "world")
        if os.path.exists(snapshots_log):
            cls._read_world_snapshot_log(snapshots_log, 0, state)

        if checkpoint_meta:
            cls._apply_world_checkpoint_meta_to_state(state, checkpoint_meta)

        checkpoint_status = str(checkpoint_meta.get("status") or "").strip().lower()
        terminal_event = cls._read_world_terminal_event(sim_dir)
        terminal_hint = str(
            checkpoint_meta.get("saved_at")
            or terminal_event.get("timestamp")
            or (existing_state.completed_at if existing_state else "")
            or ""
        ).strip()

        if fallback_runner_status is not None:
            state.runner_status = fallback_runner_status
            state.world_running = fallback_runner_status in {RunnerStatus.STARTING, RunnerStatus.RUNNING}
            state.world_completed = fallback_runner_status == RunnerStatus.COMPLETED
            state.process_pid = process_pid
            if state.world_running and state.world_current_phase in {
                "idle",
                "completed",
                "failed",
                "interrupted",
                "stopped",
            }:
                restored_phase = "running"
                if isinstance(state.latest_snapshot, dict):
                    restored_phase = state.latest_snapshot.get("phase") or restored_phase
                state.world_current_phase = restored_phase
        elif checkpoint_status in {"completed", "failed", "interrupted"} or terminal_event:
            terminal = cls._classify_world_process_exit(
                simulation_id,
                None,
                sim_dir,
                state,
                respect_stop_state=False,
            )
            cls._apply_world_terminal_to_state(
                state,
                terminal,
                completed_at_hint=terminal_hint,
            )
        elif checkpoint_status == "running":
            lease_config_path = str(checkpoint_meta.get("config_path") or config_path or "").strip()
            if lease_config_path:
                lease_status = cls._world_lease_status(simulation_id, lease_config_path)
                if lease_status.get("alive"):
                    state.runner_status = RunnerStatus.RUNNING
                    state.world_running = True
                    state.world_completed = False
                    state.process_pid = int(lease_status.get("pid") or 0) or None
                    if state.world_current_phase in {
                        "idle",
                        "completed",
                        "failed",
                        "interrupted",
                        "stopped",
                    }:
                        restored_phase = "running"
                        if isinstance(state.latest_snapshot, dict):
                            restored_phase = state.latest_snapshot.get("phase") or restored_phase
                        state.world_current_phase = restored_phase
                else:
                    terminal = cls._classify_world_process_exit(
                        simulation_id,
                        None,
                        sim_dir,
                        state,
                        respect_stop_state=False,
                    )
                    cls._apply_world_terminal_to_state(
                        state,
                        terminal,
                        completed_at_hint=terminal_hint,
                    )
        elif checkpoint_meta:
            state.runner_status = RunnerStatus.IDLE
            state.world_running = False
            state.world_completed = False
            state.process_pid = None
        elif existing_state:
            state.runner_status = existing_state.runner_status
            state.world_running = existing_state.world_running
            state.world_completed = existing_state.world_completed
            state.process_pid = existing_state.process_pid
            state.terminal_status = existing_state.terminal_status
            state.stop_reason = existing_state.stop_reason
            state.error = existing_state.error
            state.world_current_phase = existing_state.world_current_phase

        state.updated_at = datetime.now().isoformat()
        return state

    @classmethod
    def refresh_world_run_state_from_artifacts(
        cls,
        simulation_id: str,
        *,
        persist: bool = True,
        fallback_runner_status: Optional[RunnerStatus] = None,
        process_pid: Optional[int] = None,
    ) -> Optional[SimulationRunState]:
        existing_state = cls._run_states.get(simulation_id)
        if not existing_state:
            existing_state = cls._load_run_state(simulation_id)

        state = cls._build_world_run_state_from_artifacts(
            simulation_id,
            existing_state=existing_state,
            fallback_runner_status=fallback_runner_status,
            process_pid=process_pid,
        )
        if not state:
            return None

        if persist:
            cls._save_run_state(state)
        else:
            cls._run_states[simulation_id] = state
        return cls._reconcile_world_run_state(simulation_id, state, persist=persist)

    @classmethod
    def bootstrap_world_operator_run_state(
        cls,
        simulation_id: str,
        config_path: str,
        *,
        max_rounds: Optional[int] = None,
        resume_from_checkpoint: bool = False,
        process_pid: Optional[int] = None,
    ) -> SimulationRunState:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        total_rounds, total_hours, _ = cls._resolve_total_rounds(
            config,
            max_rounds=max_rounds,
            allow_world_extension=resume_from_checkpoint,
        )
        state = SimulationRunState(
            simulation_id=simulation_id,
            simulation_mode=SimulationMode.WORLD.value,
            runner_status=RunnerStatus.RUNNING,
            total_rounds=total_rounds,
            total_simulation_hours=total_hours,
            started_at=datetime.now().isoformat(),
            process_pid=process_pid,
        )
        state.world_running = True
        state.world_current_phase = "resuming" if resume_from_checkpoint else "starting"

        if resume_from_checkpoint:
            checkpoint_meta = cls.get_world_checkpoint_meta(simulation_id)
            if checkpoint_meta:
                state.current_round = int(checkpoint_meta.get("last_completed_tick", 0) or 0)
                state.simulated_hours = checkpoint_meta.get("simulated_hours", 0) or 0
                state.latest_snapshot = checkpoint_meta.get("last_snapshot") or None
                state.world_active_events = checkpoint_meta.get("active_events") or []
                state.world_queued_events = checkpoint_meta.get("queued_events") or []
                state.world_completed_events_count = int(checkpoint_meta.get("completed_events_count", 0) or 0)

        cls._save_run_state(state)
        return state

    @classmethod
    def _apply_world_checkpoint_meta_to_state(
        cls,
        state: SimulationRunState,
        checkpoint_meta: Dict[str, Any],
    ) -> bool:
        changed = False

        run_total_rounds = int(checkpoint_meta.get("run_total_rounds", 0) or 0)
        if run_total_rounds and state.total_rounds != run_total_rounds:
            state.total_rounds = run_total_rounds
            changed = True

        minutes_per_round = int(checkpoint_meta.get("minutes_per_round", 60) or 60)
        total_hours = math.ceil(run_total_rounds * minutes_per_round / 60) if run_total_rounds else 0
        if total_hours and state.total_simulation_hours != total_hours:
            state.total_simulation_hours = total_hours
            changed = True

        last_completed_tick = int(checkpoint_meta.get("last_completed_tick", 0) or 0)
        if last_completed_tick > state.current_round:
            state.current_round = last_completed_tick
            changed = True

        simulated_hours = checkpoint_meta.get("simulated_hours")
        if simulated_hours is not None:
            simulated_hours_value = float(simulated_hours)
            if simulated_hours_value > float(state.simulated_hours or 0):
                state.simulated_hours = simulated_hours_value
                changed = True

        last_snapshot = checkpoint_meta.get("last_snapshot") or {}
        if isinstance(last_snapshot, dict) and last_snapshot and state.latest_snapshot != last_snapshot:
            state.latest_snapshot = last_snapshot
            changed = True

        active_events = checkpoint_meta.get("active_events") or []
        if state.world_active_events != active_events:
            state.world_active_events = list(active_events)
            changed = True

        queued_events = checkpoint_meta.get("queued_events") or []
        if state.world_queued_events != queued_events:
            state.world_queued_events = list(queued_events)
            changed = True

        completed_events_count = int(checkpoint_meta.get("completed_events_count", 0) or 0)
        if state.world_completed_events_count != completed_events_count:
            state.world_completed_events_count = completed_events_count
            changed = True

        return changed

    @classmethod
    def _apply_world_terminal_to_state(
        cls,
        state: SimulationRunState,
        terminal: Dict[str, Any],
        completed_at_hint: str = "",
    ) -> bool:
        changed = False

        runner_status = terminal.get("runner_status")
        if runner_status and state.runner_status != runner_status:
            state.runner_status = runner_status
            changed = True

        world_current_phase = str(terminal.get("world_current_phase") or "").strip()
        if world_current_phase and state.world_current_phase != world_current_phase:
            state.world_current_phase = world_current_phase
            changed = True

        terminal_status = str(terminal.get("terminal_status") or "").strip()
        if state.terminal_status != terminal_status:
            state.terminal_status = terminal_status
            changed = True

        stop_reason = str(terminal.get("stop_reason") or "").strip()
        if state.stop_reason != stop_reason:
            state.stop_reason = stop_reason
            changed = True

        error = terminal.get("error")
        if state.error != error:
            state.error = error
            changed = True

        if state.world_running:
            state.world_running = False
            changed = True

        completed = runner_status == RunnerStatus.COMPLETED
        if state.world_completed != completed:
            state.world_completed = completed
            changed = True

        desired_completed_at = completed_at_hint or datetime.now().isoformat()
        if runner_status in {RunnerStatus.COMPLETED, RunnerStatus.FAILED, RunnerStatus.STOPPED}:
            if state.process_pid is not None:
                state.process_pid = None
                changed = True
            if state.completed_at != desired_completed_at:
                state.completed_at = desired_completed_at
                changed = True

        return changed

    @classmethod
    def _reconcile_world_run_state(
        cls,
        simulation_id: str,
        state: Optional[SimulationRunState],
        persist: bool = False,
    ) -> Optional[SimulationRunState]:
        if not state:
            return None
        if SimulationMode.normalize(state.simulation_mode).value != SimulationMode.WORLD.value:
            return state

        checkpoint_meta = cls.get_world_checkpoint_meta(simulation_id)
        if not checkpoint_meta:
            return state

        changed = cls._apply_world_checkpoint_meta_to_state(state, checkpoint_meta)
        checkpoint_status = str(checkpoint_meta.get("status") or "").strip().lower()

        if checkpoint_status in {"completed", "failed", "interrupted"}:
            terminal = cls._classify_world_process_exit(
                simulation_id,
                None,
                os.path.join(cls.RUN_STATE_DIR, simulation_id),
                state,
                respect_stop_state=False,
            )
            changed = (
                cls._apply_world_terminal_to_state(
                    state,
                    terminal,
                    completed_at_hint=str(checkpoint_meta.get("saved_at") or ""),
                )
                or changed
            )
        elif checkpoint_status == "running":
            config_path = str(checkpoint_meta.get("config_path") or "").strip()
            if config_path:
                lease_status = cls._world_lease_status(simulation_id, config_path)
                if lease_status.get("alive"):
                    if state.runner_status != RunnerStatus.RUNNING:
                        state.runner_status = RunnerStatus.RUNNING
                        changed = True
                    if not state.world_running:
                        state.world_running = True
                        changed = True
                    if state.world_completed:
                        state.world_completed = False
                        changed = True
                    if state.world_current_phase in {"idle", "completed", "failed", "interrupted", "stopped"}:
                        state.world_current_phase = "running"
                        changed = True

        if changed:
            state.updated_at = datetime.now().isoformat()
            if persist:
                cls._save_run_state(state)
            else:
                cls._run_states[simulation_id] = state

        return state

    @classmethod
    def _world_lease_status(
        cls,
        simulation_id: str,
        config_path: str,
    ) -> Dict[str, Any]:
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        paths = world_run_paths_for_simulation_dir(sim_dir)
        status = inspect_world_run_lease(paths, expected_config_path=config_path)
        if status.get("pid") and not status.get("alive"):
            cleanup_world_run_lease(paths)
            status = inspect_world_run_lease(paths, expected_config_path=config_path)
        return status

    @classmethod
    def _ensure_world_not_running(
        cls,
        simulation_id: str,
        config_path: str,
    ) -> None:
        lease_status = cls._world_lease_status(simulation_id, config_path)
        if lease_status.get("matches_expected_run"):
            raise ValueError(
                f"world 模拟已在运行中: {simulation_id}, pid={lease_status.get('pid')}"
            )

        existing = cls.get_run_state(simulation_id)
        if existing and existing.runner_status in [RunnerStatus.RUNNING, RunnerStatus.STARTING]:
            if cls._has_live_process(simulation_id):
                raise ValueError(f"模拟已在运行中: {simulation_id}")
            existing.runner_status = RunnerStatus.STOPPED
            existing.world_running = False
            existing.updated_at = datetime.now().isoformat()
            cls._save_run_state(existing)

    @classmethod
    def _has_live_process(cls, simulation_id: str) -> bool:
        process = cls._processes.get(simulation_id)
        return bool(process and process.poll() is None)

    @classmethod
    def has_live_process(cls, simulation_id: str) -> bool:
        return cls._has_live_process(simulation_id)
    
    @classmethod
    def start_simulation(
        cls,
        simulation_id: str,
        platform: str = "parallel",  # twitter / reddit / parallel
        max_rounds: int = None,  # 最大模拟轮数（可选，用于截断过长的模拟）
        enable_graph_memory_update: bool = False,  # 是否将活动更新到Zep图谱
        graph_id: str = None,  # Zep图谱ID（启用图谱更新时必需）
        resume_from_checkpoint: bool = False,
        checkpoint_meta: Optional[Dict[str, Any]] = None,
    ) -> SimulationRunState:
        """
        启动模拟
        
        Args:
            simulation_id: 模拟ID
            platform: 运行平台 (twitter/reddit/parallel)
            max_rounds: 最大模拟轮数（可选，用于截断过长的模拟）
            enable_graph_memory_update: 是否将Agent活动动态更新到Zep图谱
            graph_id: Zep图谱ID（启用图谱更新时必需）
            resume_from_checkpoint: world 模式下是否从 checkpoint 续跑
            checkpoint_meta: 已加载的 checkpoint 元信息（可选）
            
        Returns:
            SimulationRunState
        """
        # 加载模拟配置
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        
        if not os.path.exists(config_path):
            raise ValueError(f"模拟配置不存在，请先调用 /prepare 接口")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        simulation_mode = SimulationMode.normalize(config.get("simulation_mode")).value
        if simulation_mode == SimulationMode.WORLD.value:
            cls._ensure_world_not_running(simulation_id, config_path)
        else:
            existing = cls.get_run_state(simulation_id)
            if existing and existing.runner_status in [RunnerStatus.RUNNING, RunnerStatus.STARTING]:
                raise ValueError(f"模拟已在运行中: {simulation_id}")
        
        # 初始化运行状态
        base_total_rounds, total_hours, minutes_per_round = cls._resolve_total_rounds(config)
        total_rounds, _, _ = cls._resolve_total_rounds(
            config,
            max_rounds=max_rounds,
            allow_world_extension=(
                resume_from_checkpoint and simulation_mode == SimulationMode.WORLD.value
            ),
        )
        if max_rounds is not None and max_rounds > 0 and total_rounds < base_total_rounds:
            logger.info(f"轮数已截断: {base_total_rounds} -> {total_rounds} (max_rounds={max_rounds})")
        
        state = SimulationRunState(
            simulation_id=simulation_id,
            simulation_mode=simulation_mode,
            runner_status=RunnerStatus.STARTING,
            total_rounds=total_rounds,
            total_simulation_hours=total_hours,
            started_at=datetime.now().isoformat(),
        )

        if resume_from_checkpoint and simulation_mode == SimulationMode.WORLD.value:
            checkpoint_meta = checkpoint_meta or cls.get_world_checkpoint_meta(simulation_id)
            if checkpoint_meta:
                state.current_round = int(checkpoint_meta.get("last_completed_tick", 0) or 0)
                state.simulated_hours = checkpoint_meta.get("simulated_hours", 0) or 0
                state.latest_snapshot = checkpoint_meta.get("last_snapshot") or None
                state.world_active_events = checkpoint_meta.get("active_events") or []
                state.world_queued_events = checkpoint_meta.get("queued_events") or []
                state.world_completed_events_count = int(checkpoint_meta.get("completed_events_count", 0) or 0)
                state.world_current_phase = "resuming"
        
        cls._save_run_state(state)
        
        # 如果启用图谱记忆更新，创建更新器
        if enable_graph_memory_update and simulation_mode != SimulationMode.WORLD.value:
            if not graph_id:
                raise ValueError("启用图谱记忆更新时必须提供 graph_id")
            
            try:
                ZepGraphMemoryManager.create_updater(simulation_id, graph_id)
                cls._graph_memory_enabled[simulation_id] = True
                logger.info(f"已启用图谱记忆更新: simulation_id={simulation_id}, graph_id={graph_id}")
            except Exception as e:
                logger.error(f"创建图谱记忆更新器失败: {e}")
                cls._graph_memory_enabled[simulation_id] = False
        else:
            cls._graph_memory_enabled[simulation_id] = False
        
        # 确定运行哪个脚本（脚本位于 backend/scripts/ 目录）
        if simulation_mode == SimulationMode.WORLD.value:
            script_name = "run_world_simulation.py"
            state.world_running = True
        elif platform == "twitter":
            script_name = "run_twitter_simulation.py"
            state.twitter_running = True
        elif platform == "reddit":
            script_name = "run_reddit_simulation.py"
            state.reddit_running = True
        else:
            script_name = "run_parallel_simulation.py"
            state.twitter_running = True
            state.reddit_running = True
        
        script_path = os.path.join(cls.SCRIPTS_DIR, script_name)
        
        if not os.path.exists(script_path):
            raise ValueError(f"脚本不存在: {script_path}")
        
        # 创建动作队列
        action_queue = Queue()
        cls._action_queues[simulation_id] = action_queue
        
        # 启动模拟进程
        try:
            # 构建运行命令，使用完整路径
            # 新的日志结构：
            #   twitter/actions.jsonl - Twitter 动作日志
            #   reddit/actions.jsonl  - Reddit 动作日志
            #   simulation.log        - 主进程日志
            
            cmd = [
                sys.executable,  # Python解释器
                script_path,
                "--config", config_path,  # 使用完整配置文件路径
            ]
            
            # 如果指定了最大轮数，添加到命令行参数
            if max_rounds is not None and max_rounds > 0:
                cmd.extend(["--max-rounds", str(max_rounds)])
            if resume_from_checkpoint:
                cmd.append("--resume-from-checkpoint")
            
            # 创建主日志文件，避免 stdout/stderr 管道缓冲区满导致进程阻塞
            main_log_path = os.path.join(sim_dir, "simulation.log")
            log_mode = 'a' if resume_from_checkpoint else 'w'
            main_log_file = open(main_log_path, log_mode, encoding='utf-8')
            
            # 设置子进程环境变量，确保 Windows 上使用 UTF-8 编码
            # 这可以修复第三方库（如 OASIS）读取文件时未指定编码的问题
            env = os.environ.copy()
            env['PYTHONUTF8'] = '1'  # Python 3.7+ 支持，让所有 open() 默认使用 UTF-8
            env['PYTHONIOENCODING'] = 'utf-8'  # 确保 stdout/stderr 使用 UTF-8
            
            # 设置工作目录为模拟目录（数据库等文件会生成在此）
            # 使用 start_new_session=True 创建新的进程组，确保可以通过 os.killpg 终止所有子进程
            process = subprocess.Popen(
                cmd,
                cwd=sim_dir,
                stdout=main_log_file,
                stderr=subprocess.STDOUT,  # stderr 也写入同一个文件
                text=True,
                encoding='utf-8',  # 显式指定编码
                bufsize=1,
                env=env,  # 传递带有 UTF-8 设置的环境变量
                start_new_session=True,  # 创建新进程组，确保服务器关闭时能终止所有相关进程
            )
            
            # 保存文件句柄以便后续关闭
            cls._stdout_files[simulation_id] = main_log_file
            cls._stderr_files[simulation_id] = None  # 不再需要单独的 stderr
            
            state.process_pid = process.pid
            state.runner_status = RunnerStatus.RUNNING
            cls._processes[simulation_id] = process
            cls._save_run_state(state)
            
            # 启动监控线程
            monitor_thread = threading.Thread(
                target=cls._monitor_simulation,
                args=(simulation_id,),
                daemon=True
            )
            monitor_thread.start()
            cls._monitor_threads[simulation_id] = monitor_thread
            
            logger.info(f"模拟启动成功: {simulation_id}, pid={process.pid}, platform={platform}")
            
        except Exception as e:
            state.runner_status = RunnerStatus.FAILED
            state.error = str(e)
            cls._save_run_state(state)
            raise
        
        return state
    
    @classmethod
    def _monitor_simulation(cls, simulation_id: str):
        """监控模拟进程，解析动作日志"""
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        
        # 新的日志结构：分平台的动作日志
        twitter_actions_log = os.path.join(sim_dir, "twitter", "actions.jsonl")
        reddit_actions_log = os.path.join(sim_dir, "reddit", "actions.jsonl")
        
        process = cls._processes.get(simulation_id)
        state = cls.get_run_state(simulation_id)
        
        if not process or not state:
            return

        if SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
            cls._monitor_world_simulation(simulation_id, process, state, sim_dir)
            return
        
        twitter_position = 0
        reddit_position = 0
        
        try:
            while process.poll() is None:  # 进程仍在运行
                # 读取 Twitter 动作日志
                if os.path.exists(twitter_actions_log):
                    twitter_position = cls._read_action_log(
                        twitter_actions_log, twitter_position, state, "twitter"
                    )
                
                # 读取 Reddit 动作日志
                if os.path.exists(reddit_actions_log):
                    reddit_position = cls._read_action_log(
                        reddit_actions_log, reddit_position, state, "reddit"
                    )
                
                # 更新状态
                cls._save_run_state(state)
                time.sleep(2)
            
            # 进程结束后，最后读取一次日志
            if os.path.exists(twitter_actions_log):
                cls._read_action_log(twitter_actions_log, twitter_position, state, "twitter")
            if os.path.exists(reddit_actions_log):
                cls._read_action_log(reddit_actions_log, reddit_position, state, "reddit")
            
            # 进程结束
            exit_code = process.returncode
            
            if state.runner_status in {RunnerStatus.STOPPING, RunnerStatus.STOPPED}:
                state.runner_status = RunnerStatus.STOPPED
                state.completed_at = state.completed_at or datetime.now().isoformat()
                logger.info(f"模拟已停止: {simulation_id}")
            elif exit_code == 0:
                state.runner_status = RunnerStatus.COMPLETED
                state.completed_at = datetime.now().isoformat()
                logger.info(f"模拟完成: {simulation_id}")
            else:
                state.runner_status = RunnerStatus.FAILED
                # 从主日志文件读取错误信息
                main_log_path = os.path.join(sim_dir, "simulation.log")
                error_info = ""
                try:
                    if os.path.exists(main_log_path):
                        with open(main_log_path, 'r', encoding='utf-8') as f:
                            error_info = f.read()[-2000:]  # 取最后2000字符
                except Exception:
                    pass
                state.error = f"进程退出码: {exit_code}, 错误: {error_info}"
                logger.error(f"模拟失败: {simulation_id}, error={state.error}")
            
            state.twitter_running = False
            state.reddit_running = False
            cls._save_run_state(state)
            
        except Exception as e:
            logger.error(f"监控线程异常: {simulation_id}, error={str(e)}")
            state.runner_status = RunnerStatus.FAILED
            state.error = str(e)
            cls._save_run_state(state)
        
        finally:
            # 停止图谱记忆更新器
            if cls._graph_memory_enabled.get(simulation_id, False):
                try:
                    ZepGraphMemoryManager.stop_updater(simulation_id)
                    logger.info(f"已停止图谱记忆更新: simulation_id={simulation_id}")
                except Exception as e:
                    logger.error(f"停止图谱记忆更新器失败: {e}")
                cls._graph_memory_enabled.pop(simulation_id, None)
            
            # 清理进程资源
            cls._processes.pop(simulation_id, None)
            cls._action_queues.pop(simulation_id, None)
            
            # 关闭日志文件句柄
            if simulation_id in cls._stdout_files:
                try:
                    cls._stdout_files[simulation_id].close()
                except Exception:
                    pass
                cls._stdout_files.pop(simulation_id, None)
            if simulation_id in cls._stderr_files and cls._stderr_files[simulation_id]:
                try:
                    cls._stderr_files[simulation_id].close()
                except Exception:
                    pass
                cls._stderr_files.pop(simulation_id, None)

    @classmethod
    def _monitor_world_simulation(
        cls,
        simulation_id: str,
        process: subprocess.Popen,
        state: SimulationRunState,
        sim_dir: str,
    ) -> None:
        actions_log = os.path.join(sim_dir, "world", "actions.jsonl")
        snapshots_log = os.path.join(sim_dir, "world", "state_snapshots.jsonl")
        position = 0
        snapshot_position = 0

        try:
            while process.poll() is None:
                if os.path.exists(actions_log):
                    position = cls._read_action_log(actions_log, position, state, "world")
                if os.path.exists(snapshots_log):
                    snapshot_position = cls._read_world_snapshot_log(
                        snapshots_log,
                        snapshot_position,
                        state,
                    )
                cls._save_run_state(state)
                time.sleep(1)

            if os.path.exists(actions_log):
                cls._read_action_log(actions_log, position, state, "world")
            if os.path.exists(snapshots_log):
                cls._read_world_snapshot_log(snapshots_log, snapshot_position, state)

            terminal = cls._classify_world_process_exit(simulation_id, process.returncode, sim_dir, state)
            state.runner_status = terminal["runner_status"]
            state.completed_at = state.completed_at or datetime.now().isoformat()
            state.terminal_status = terminal.get("terminal_status", state.terminal_status)
            state.stop_reason = terminal.get("stop_reason", state.stop_reason)
            state.world_current_phase = terminal.get("world_current_phase", state.world_current_phase)
            state.error = terminal.get("error") or state.error
            state.world_running = False
            state.process_pid = None
            cls._save_run_state(state)
        except Exception as e:
            logger.error(f"world 监控线程异常: {simulation_id}, error={e}")
            state.runner_status = RunnerStatus.FAILED
            state.world_running = False
            state.error = str(e)
            cls._save_run_state(state)
        finally:
            cls._processes.pop(simulation_id, None)
            cls._action_queues.pop(simulation_id, None)
            if simulation_id in cls._stdout_files:
                try:
                    cls._stdout_files[simulation_id].close()
                except Exception:
                    pass
                cls._stdout_files.pop(simulation_id, None)
            if simulation_id in cls._stderr_files and cls._stderr_files[simulation_id]:
                try:
                    cls._stderr_files[simulation_id].close()
                except Exception:
                    pass
                cls._stderr_files.pop(simulation_id, None)

    @classmethod
    def _read_world_terminal_event(cls, sim_dir: str) -> Dict[str, Any]:
        actions_log = os.path.join(sim_dir, "world", "actions.jsonl")
        terminal_types = {"simulation_end", "simulation_failed", "simulation_interrupted"}
        last_terminal: Dict[str, Any] = {}
        if not os.path.exists(actions_log):
            return last_terminal
        try:
            with open(actions_log, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(payload.get("event_type") or "").strip().lower() in terminal_types:
                        last_terminal = payload
        except Exception as e:
            logger.warning(f"读取 world terminal event 失败: {sim_dir}, error={e}")
        return last_terminal

    @classmethod
    def _classify_world_process_exit(
        cls,
        simulation_id: str,
        returncode: Optional[int],
        sim_dir: str,
        state: SimulationRunState,
        respect_stop_state: bool = True,
    ) -> Dict[str, Any]:
        if respect_stop_state and state.runner_status in {RunnerStatus.STOPPING, RunnerStatus.STOPPED}:
            return {
                "runner_status": RunnerStatus.STOPPED,
                "world_current_phase": "stopped",
                "terminal_status": state.terminal_status or "stopped",
                "stop_reason": state.stop_reason or "operator_stop_requested",
                "error": state.error,
            }

        terminal_event = cls._read_world_terminal_event(sim_dir)
        event_type = str(terminal_event.get("event_type") or "").strip().lower()
        checkpoint_meta = cls.get_world_checkpoint_meta(simulation_id) or {}
        checkpoint_status = str(checkpoint_meta.get("status") or "").strip().lower()
        checkpoint_terminal = str(checkpoint_meta.get("terminal_status") or checkpoint_status or "").strip().lower()
        stop_reason = str(
            terminal_event.get("stop_reason")
            or checkpoint_meta.get("stop_reason")
            or state.stop_reason
            or ""
        ).strip()

        if event_type == "simulation_end" or checkpoint_status == "completed":
            return {
                "runner_status": RunnerStatus.COMPLETED,
                "world_current_phase": "completed",
                "terminal_status": checkpoint_terminal or "completed",
                "stop_reason": stop_reason,
                "error": None,
            }

        if event_type in {"simulation_failed", "simulation_interrupted"} or checkpoint_status in {"failed", "interrupted"}:
            terminal_status = checkpoint_terminal or event_type.replace("simulation_", "") or "failed"
            error = (
                terminal_event.get("error")
                or terminal_event.get("summary")
                or state.error
                or f"world process terminated with terminal_status={terminal_status}"
            )
            return {
                "runner_status": RunnerStatus.FAILED,
                "world_current_phase": terminal_status,
                "terminal_status": terminal_status,
                "stop_reason": stop_reason,
                "error": error,
            }

        error = (
            f"world process exited without terminal marker: returncode={returncode}, "
            f"checkpoint.status={checkpoint_status or 'unknown'}, "
            f"last_completed_tick={checkpoint_meta.get('last_completed_tick', 0)}"
        )
        return {
            "runner_status": RunnerStatus.FAILED,
            "world_current_phase": "interrupted",
            "terminal_status": "interrupted",
            "stop_reason": stop_reason or "missing_terminal_marker",
            "error": error,
        }

    @classmethod
    def _read_world_snapshot_log(
        cls,
        log_path: str,
        position: int,
        state: SimulationRunState,
    ) -> int:
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(position)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        snapshot = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    state.update_world_snapshot(snapshot)
                return f.tell()
        except Exception as e:
            logger.warning(f"读取 world snapshot 日志失败: {log_path}, error={e}")
            return position
    
    @classmethod
    def _read_action_log(
        cls, 
        log_path: str, 
        position: int, 
        state: SimulationRunState,
        platform: str
    ) -> int:
        """
        读取动作日志文件
        
        Args:
            log_path: 日志文件路径
            position: 上次读取位置
            state: 运行状态对象
            platform: 平台名称 (twitter/reddit)
            
        Returns:
            新的读取位置
        """
        # 检查是否启用了图谱记忆更新
        graph_memory_enabled = cls._graph_memory_enabled.get(state.simulation_id, False)
        graph_updater = None
        if graph_memory_enabled:
            graph_updater = ZepGraphMemoryManager.get_updater(state.simulation_id)
        
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                f.seek(position)
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            action_data = json.loads(line)
                            
                            # 处理事件类型的条目
                            if "event_type" in action_data:
                                cls._handle_event_log_entry(state, platform, action_data)
                                continue
                            
                            action = AgentAction(
                                round_num=action_data.get("round", 0),
                                timestamp=action_data.get("timestamp", datetime.now().isoformat()),
                                platform=platform,
                                agent_id=action_data.get("agent_id", 0),
                                agent_name=action_data.get("agent_name", ""),
                                action_type=action_data.get("action_type", ""),
                                action_args=action_data.get("action_args", {}),
                                result=action_data.get("result"),
                                success=action_data.get("success", True),
                            )
                            state.add_action(action)

                            if platform == "world":
                                cls._handle_world_action_entry(state, action_data)
                            
                            # 更新轮次
                            if action.round_num and action.round_num > state.current_round:
                                state.current_round = action.round_num
                            
                            # 如果启用了图谱记忆更新，将活动发送到Zep
                            if graph_updater:
                                graph_updater.add_activity_from_dict(action_data, platform)
                            
                        except json.JSONDecodeError:
                            pass
                return f.tell()
        except Exception as e:
            logger.warning(f"读取动作日志失败: {log_path}, error={e}")
            return position

    @classmethod
    def _handle_event_log_entry(
        cls,
        state: SimulationRunState,
        platform: str,
        action_data: Dict[str, Any],
    ) -> None:
        event_type = action_data.get("event_type")

        if event_type == "simulation_end":
            if platform == "twitter":
                state.twitter_completed = True
                state.twitter_running = False
                logger.info(
                    f"Twitter 模拟已完成: {state.simulation_id}, total_rounds={action_data.get('total_rounds')}, total_actions={action_data.get('total_actions')}"
                )
            elif platform == "reddit":
                state.reddit_completed = True
                state.reddit_running = False
                logger.info(
                    f"Reddit 模拟已完成: {state.simulation_id}, total_rounds={action_data.get('total_rounds')}, total_actions={action_data.get('total_actions')}"
                )
            elif platform == "world":
                state.world_completed = True
                state.world_running = False
                state.terminal_status = str(action_data.get("terminal_status") or "completed")
                state.stop_reason = str(action_data.get("stop_reason") or "")
                logger.info(
                    f"World 模拟已完成: {state.simulation_id}, total_rounds={action_data.get('total_rounds')}, total_actions={action_data.get('total_actions')}"
                )

            if cls._check_all_platforms_completed(state):
                state.runner_status = RunnerStatus.COMPLETED
                state.completed_at = datetime.now().isoformat()
                logger.info(f"所有平台模拟已完成: {state.simulation_id}")
            return
        if event_type == "simulation_failed":
            if platform == "world":
                state.world_running = False
                state.runner_status = RunnerStatus.FAILED
                state.world_current_phase = "failed"
                state.terminal_status = "failed"
                state.stop_reason = str(action_data.get("stop_reason") or "runtime_exception")
                state.error = action_data.get("error") or action_data.get("summary") or "world simulation failed"
            return
        if event_type == "simulation_interrupted":
            if platform == "world":
                state.world_running = False
                state.runner_status = RunnerStatus.FAILED
                state.world_current_phase = "interrupted"
                state.terminal_status = "interrupted"
                state.stop_reason = str(action_data.get("stop_reason") or "interrupted")
                state.error = action_data.get("error") or action_data.get("summary") or "world simulation interrupted"
            return

        round_num = action_data.get("round") or action_data.get("tick", 0)
        simulated_hours = action_data.get("simulated_hours", 0)

        if event_type in {"round_end", "tick_end"}:
            if platform == "twitter":
                if round_num > state.twitter_current_round:
                    state.twitter_current_round = round_num
                state.twitter_simulated_hours = simulated_hours
            elif platform == "reddit":
                if round_num > state.reddit_current_round:
                    state.reddit_current_round = round_num
                state.reddit_simulated_hours = simulated_hours
            elif platform == "world":
                state.world_running = True
                if round_num > state.current_round:
                    state.current_round = round_num
                state.simulated_hours = simulated_hours

            if round_num > state.current_round:
                state.current_round = round_num
            if platform != "world":
                state.simulated_hours = max(state.twitter_simulated_hours, state.reddit_simulated_hours)

        if platform == "world":
            if event_type in {"tick_start", "tick_end", "round_start", "round_end"} and round_num > state.current_round:
                state.current_round = round_num
            state.world_running = event_type not in {"simulation_end", "simulation_failed", "simulation_interrupted"}
            state.add_world_event(action_data)

    @classmethod
    def _handle_world_action_entry(
        cls,
        state: SimulationRunState,
        action_data: Dict[str, Any],
    ) -> None:
        action_type = str(action_data.get("action_type") or "").strip()
        if action_type:
            state.add_world_event(action_data)
    
    @classmethod
    def _check_all_platforms_completed(cls, state: SimulationRunState) -> bool:
        """
        检查所有启用的平台是否都已完成模拟
        
        通过检查对应的 actions.jsonl 文件是否存在来判断平台是否被启用
        
        Returns:
            True 如果所有启用的平台都已完成
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, state.simulation_id)
        twitter_log = os.path.join(sim_dir, "twitter", "actions.jsonl")
        reddit_log = os.path.join(sim_dir, "reddit", "actions.jsonl")
        world_log = os.path.join(sim_dir, "world", "actions.jsonl")

        if SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
            return os.path.exists(world_log) and state.world_completed
        
        # 检查哪些平台被启用（通过文件是否存在判断）
        twitter_enabled = os.path.exists(twitter_log)
        reddit_enabled = os.path.exists(reddit_log)
        
        # 如果平台被启用但未完成，则返回 False
        if twitter_enabled and not state.twitter_completed:
            return False
        if reddit_enabled and not state.reddit_completed:
            return False
        
        # 至少有一个平台被启用且已完成
        return twitter_enabled or reddit_enabled
    
    @classmethod
    def _terminate_process(cls, process: subprocess.Popen, simulation_id: str, timeout: int = 10):
        """
        跨平台终止进程及其子进程
        
        Args:
            process: 要终止的进程
            simulation_id: 模拟ID（用于日志）
            timeout: 等待进程退出的超时时间（秒）
        """
        if IS_WINDOWS:
            # Windows: 使用 taskkill 命令终止进程树
            # /F = 强制终止, /T = 终止进程树（包括子进程）
            logger.info(f"终止进程树 (Windows): simulation={simulation_id}, pid={process.pid}")
            try:
                # 先尝试优雅终止
                subprocess.run(
                    ['taskkill', '/PID', str(process.pid), '/T'],
                    capture_output=True,
                    timeout=5
                )
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # 强制终止
                    logger.warning(f"进程未响应，强制终止: {simulation_id}")
                    subprocess.run(
                        ['taskkill', '/F', '/PID', str(process.pid), '/T'],
                        capture_output=True,
                        timeout=5
                    )
                    process.wait(timeout=5)
            except Exception as e:
                logger.warning(f"taskkill 失败，尝试 terminate: {e}")
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        else:
            # Unix: 使用进程组终止
            # 由于使用了 start_new_session=True，进程组 ID 等于主进程 PID
            pgid = os.getpgid(process.pid)
            logger.info(f"终止进程组 (Unix): simulation={simulation_id}, pgid={pgid}")
            
            # 先发送 SIGTERM 给整个进程组
            os.killpg(pgid, signal.SIGTERM)
            
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # 如果超时后还没结束，强制发送 SIGKILL
                logger.warning(f"进程组未响应 SIGTERM，强制终止: {simulation_id}")
                os.killpg(pgid, signal.SIGKILL)
                process.wait(timeout=5)

    @classmethod
    def _signal_process_group(
        cls,
        process: subprocess.Popen,
        simulation_id: str,
        sig: int,
    ) -> None:
        """向模拟进程组发送控制信号。"""
        if process.poll() is not None:
            raise ValueError(f"模拟进程已退出: {simulation_id}")

        if IS_WINDOWS:
            raise NotImplementedError("当前仅支持在 Unix 系统上暂停/恢复模拟进程")

        pgid = os.getpgid(process.pid)
        logger.info(f"发送进程组信号: simulation={simulation_id}, pgid={pgid}, signal={sig}")
        os.killpg(pgid, sig)

    @classmethod
    def pause_simulation(cls, simulation_id: str) -> SimulationRunState:
        """暂停模拟进程，但保留当前运行上下文。"""
        state = cls.get_run_state(simulation_id)
        if not state:
            raise ValueError(f"模拟不存在: {simulation_id}")

        if state.runner_status != RunnerStatus.RUNNING:
            raise ValueError(f"模拟当前不可暂停: {simulation_id}, status={state.runner_status}")

        process = cls._processes.get(simulation_id)
        if not process:
            raise ValueError(f"模拟进程不可用，无法暂停: {simulation_id}")

        try:
            cls._signal_process_group(process, simulation_id, signal.SIGSTOP)
        except ProcessLookupError as e:
            raise ValueError(f"模拟进程不存在，无法暂停: {simulation_id}") from e

        state.runner_status = RunnerStatus.PAUSED
        if SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
            state.world_current_phase = "paused"
        state.updated_at = datetime.now().isoformat()
        cls._save_run_state(state)

        logger.info(f"模拟已暂停: {simulation_id}")
        return state

    @classmethod
    def resume_simulation(cls, simulation_id: str) -> SimulationRunState:
        """恢复已暂停的模拟进程。"""
        state = cls.get_run_state(simulation_id)
        if not state:
            raise ValueError(f"模拟不存在: {simulation_id}")

        if state.runner_status != RunnerStatus.PAUSED:
            raise ValueError(f"模拟当前不可恢复: {simulation_id}, status={state.runner_status}")

        process = cls._processes.get(simulation_id)
        if not process:
            raise ValueError(f"模拟进程不可用，无法恢复: {simulation_id}")

        try:
            cls._signal_process_group(process, simulation_id, signal.SIGCONT)
        except ProcessLookupError as e:
            raise ValueError(f"模拟进程不存在，无法恢复: {simulation_id}") from e

        state.runner_status = RunnerStatus.RUNNING
        if SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
            restored_phase = "running"
            if isinstance(state.latest_snapshot, dict):
                restored_phase = state.latest_snapshot.get("phase") or restored_phase
            state.world_current_phase = restored_phase
        state.updated_at = datetime.now().isoformat()
        cls._save_run_state(state)

        logger.info(f"模拟已恢复: {simulation_id}")
        return state

    @classmethod
    def resume_world_from_checkpoint(
        cls,
        simulation_id: str,
        max_rounds: Optional[int] = None,
    ) -> SimulationRunState:
        checkpoint_meta = cls.get_world_checkpoint_meta(simulation_id)
        if not checkpoint_meta:
            raise ValueError(f"world checkpoint 不存在，无法续跑: {simulation_id}")

        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        if not os.path.exists(config_path):
            raise ValueError(f"模拟配置不存在，请先调用 /prepare 接口")

        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        simulation_mode = SimulationMode.normalize(config.get("simulation_mode")).value
        if simulation_mode != SimulationMode.WORLD.value:
            raise ValueError("只有 world 模式支持 checkpoint 续跑")
        cls._ensure_world_not_running(simulation_id, config_path)

        resume_round_cap = max_rounds
        if resume_round_cap is None:
            checkpoint_round_cap = checkpoint_meta.get("run_total_rounds")
            if checkpoint_round_cap:
                resume_round_cap = int(checkpoint_round_cap)

        total_rounds, _, _ = cls._resolve_total_rounds(
            config,
            max_rounds=resume_round_cap,
            allow_world_extension=True,
        )
        if checkpoint_meta["last_completed_tick"] >= total_rounds:
            raise ValueError(
                f"checkpoint 已经跑到末尾: tick={checkpoint_meta['last_completed_tick']}, total_rounds={total_rounds}"
            )

        return cls.start_simulation(
            simulation_id=simulation_id,
            platform="world",
            max_rounds=resume_round_cap,
            enable_graph_memory_update=False,
            graph_id=None,
            resume_from_checkpoint=True,
            checkpoint_meta=checkpoint_meta,
        )

    @classmethod
    def stop_simulation(cls, simulation_id: str) -> SimulationRunState:
        """停止模拟"""
        state = cls.get_run_state(simulation_id)
        if not state:
            raise ValueError(f"模拟不存在: {simulation_id}")
        
        if state.runner_status not in [RunnerStatus.RUNNING, RunnerStatus.PAUSED]:
            raise ValueError(f"模拟未在运行: {simulation_id}, status={state.runner_status}")
        
        state.runner_status = RunnerStatus.STOPPING
        cls._save_run_state(state)
        
        # 终止进程
        process = cls._processes.get(simulation_id)
        if process and process.poll() is None:
            try:
                if SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
                    stop_request_path = world_run_paths_for_simulation_dir(
                        os.path.join(cls.RUN_STATE_DIR, simulation_id)
                    ).stop_request_path
                    os.makedirs(os.path.dirname(stop_request_path), exist_ok=True)
                    with open(stop_request_path, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "reason": "operator_stop_requested",
                                "requested_at": datetime.now().isoformat(),
                            },
                            f,
                            ensure_ascii=False,
                            indent=2,
                        )
                    deadline = time.time() + 5
                    while process.poll() is None and time.time() < deadline:
                        time.sleep(0.2)
                if process.poll() is None:
                    cls._terminate_process(process, simulation_id)
            except ProcessLookupError:
                # 进程已经不存在
                pass
            except Exception as e:
                logger.error(f"终止进程组失败: {simulation_id}, error={e}")
                # 回退到直接终止进程
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
        
        state.runner_status = RunnerStatus.STOPPED
        state.twitter_running = False
        state.reddit_running = False
        state.world_running = False
        if SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
            state.world_current_phase = "stopped"
            state.terminal_status = "stopped"
            state.stop_reason = "operator_stop_requested"
        state.completed_at = datetime.now().isoformat()
        cls._save_run_state(state)
        
        # 停止图谱记忆更新器
        if cls._graph_memory_enabled.get(simulation_id, False):
            try:
                ZepGraphMemoryManager.stop_updater(simulation_id)
                logger.info(f"已停止图谱记忆更新: simulation_id={simulation_id}")
            except Exception as e:
                logger.error(f"停止图谱记忆更新器失败: {e}")
            cls._graph_memory_enabled.pop(simulation_id, None)
        
        logger.info(f"模拟已停止: {simulation_id}")
        return state
    
    @classmethod
    def _read_actions_from_file(
        cls,
        file_path: str,
        default_platform: Optional[str] = None,
        platform_filter: Optional[str] = None,
        agent_id: Optional[int] = None,
        round_num: Optional[int] = None
    ) -> List[AgentAction]:
        """
        从单个动作文件中读取动作
        
        Args:
            file_path: 动作日志文件路径
            default_platform: 默认平台（当动作记录中没有 platform 字段时使用）
            platform_filter: 过滤平台
            agent_id: 过滤 Agent ID
            round_num: 过滤轮次
        """
        if not os.path.exists(file_path):
            return []
        
        actions = []
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                try:
                    data = json.loads(line)
                    
                    # 跳过非动作记录（如 simulation_start, round_start, round_end 等事件）
                    if "event_type" in data:
                        continue
                    
                    # 跳过没有 agent_id 的记录（非 Agent 动作）
                    if "agent_id" not in data:
                        continue
                    
                    # 获取平台：优先使用记录中的 platform，否则使用默认平台
                    record_platform = data.get("platform") or default_platform or ""
                    
                    # 过滤
                    if platform_filter and record_platform != platform_filter:
                        continue
                    if agent_id is not None and data.get("agent_id") != agent_id:
                        continue
                    if round_num is not None and data.get("round") != round_num:
                        continue
                    
                    actions.append(AgentAction(
                        round_num=data.get("round", 0),
                        timestamp=data.get("timestamp", ""),
                        platform=record_platform,
                        agent_id=data.get("agent_id", 0),
                        agent_name=data.get("agent_name", ""),
                        action_type=data.get("action_type", ""),
                        action_args=data.get("action_args", {}),
                        result=data.get("result"),
                        success=data.get("success", True),
                    ))
                    
                except json.JSONDecodeError:
                    continue
        
        return actions
    
    @classmethod
    def get_all_actions(
        cls,
        simulation_id: str,
        platform: Optional[str] = None,
        agent_id: Optional[int] = None,
        round_num: Optional[int] = None
    ) -> List[AgentAction]:
        """
        获取所有平台的完整动作历史（无分页限制）
        
        Args:
            simulation_id: 模拟ID
            platform: 过滤平台（twitter/reddit/world）
            agent_id: 过滤Agent
            round_num: 过滤轮次
            
        Returns:
            完整的动作列表（按时间戳排序，新的在前）
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        actions = []
        
        # 读取 Twitter 动作文件（根据文件路径自动设置 platform 为 twitter）
        twitter_actions_log = os.path.join(sim_dir, "twitter", "actions.jsonl")
        if not platform or platform == "twitter":
            actions.extend(cls._read_actions_from_file(
                twitter_actions_log,
                default_platform="twitter",  # 自动填充 platform 字段
                platform_filter=platform,
                agent_id=agent_id, 
                round_num=round_num
            ))
        
        # 读取 Reddit 动作文件（根据文件路径自动设置 platform 为 reddit）
        reddit_actions_log = os.path.join(sim_dir, "reddit", "actions.jsonl")
        if not platform or platform == "reddit":
            actions.extend(cls._read_actions_from_file(
                reddit_actions_log,
                default_platform="reddit",  # 自动填充 platform 字段
                platform_filter=platform,
                agent_id=agent_id,
                round_num=round_num
            ))

        world_actions_log = os.path.join(sim_dir, "world", "actions.jsonl")
        if not platform or platform == "world":
            actions.extend(
                cls._read_actions_from_file(
                    world_actions_log,
                    default_platform="world",
                    platform_filter=platform,
                    agent_id=agent_id,
                    round_num=round_num,
                )
            )
        
        # 如果分平台文件不存在，尝试读取旧的单一文件格式
        if not actions:
            actions_log = os.path.join(sim_dir, "actions.jsonl")
            actions = cls._read_actions_from_file(
                actions_log,
                default_platform=None,  # 旧格式文件中应该有 platform 字段
                platform_filter=platform,
                agent_id=agent_id,
                round_num=round_num
            )
        
        # 按时间戳排序（新的在前）
        actions.sort(key=lambda x: x.timestamp, reverse=True)
        
        return actions

    @classmethod
    def get_world_events(
        cls,
        simulation_id: str,
        event_types: Optional[List[str]] = None,
        tick: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        world_actions_log = os.path.join(sim_dir, "world", "actions.jsonl")
        if not os.path.exists(world_actions_log):
            return []

        normalized_filter = {event_type.lower() for event_type in (event_types or [])}
        events: List[Dict[str, Any]] = []
        with open(world_actions_log, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                normalized = cls._normalize_world_feed_entry(payload)
                if not normalized.get("event_type"):
                    continue
                event_tick = normalized.get("tick") or normalized.get("round")

                if normalized_filter and normalized["event_type"] not in normalized_filter:
                    continue
                if tick is not None and event_tick != tick:
                    continue
                events.append(normalized)

        events.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        if limit is not None:
            return events[:limit]
        return events

    @classmethod
    def _normalize_world_feed_entry(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        if "event_type" in payload:
            normalized = dict(payload)
            normalized["event_type"] = str(payload.get("event_type") or "").strip().lower()
            normalized["tick"] = payload.get("tick") or payload.get("round")
            normalized["round"] = payload.get("round") or payload.get("tick")
            return normalized

        action_type = str(payload.get("action_type") or "").strip().lower()
        if not action_type:
            return {}

        action_args = payload.get("action_args") or {}
        embedded_event = action_args.get("event") if isinstance(action_args.get("event"), dict) else {}
        event_payload = embedded_event if embedded_event else action_args
        return {
            "event_type": action_type,
            "timestamp": payload.get("timestamp"),
            "tick": payload.get("round") or event_payload.get("tick"),
            "round": payload.get("round") or event_payload.get("tick"),
            "phase": payload.get("phase"),
            "agent_id": payload.get("agent_id"),
            "agent_name": payload.get("agent_name"),
            "action_type": payload.get("action_type"),
            "intent_id": action_args.get("intent_id"),
            "objective": action_args.get("objective"),
            "resolution_status": action_args.get("resolution_status"),
            "reason": action_args.get("reason"),
            "event_id": event_payload.get("event_id"),
            "title": event_payload.get("title"),
            "summary": event_payload.get("summary") or action_args.get("summary") or payload.get("result"),
            "priority": event_payload.get("priority"),
            "duration_ticks": event_payload.get("duration_ticks"),
            "resolves_at_tick": event_payload.get("resolves_at_tick"),
            "participants": event_payload.get("participants") or action_args.get("participants") or [],
            "location": event_payload.get("location") or action_args.get("location"),
            "resource": event_payload.get("resource") or action_args.get("resource"),
            "target": event_payload.get("target") or action_args.get("target"),
            "status": event_payload.get("status") or action_args.get("status"),
            "state_impacts": event_payload.get("state_impacts") or action_args.get("state_impacts") or {},
            "event": embedded_event or None,
        }

    @classmethod
    def get_world_snapshots(
        cls,
        simulation_id: str,
        tick: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        snapshots_log = os.path.join(sim_dir, "world", "state_snapshots.jsonl")
        if not os.path.exists(snapshots_log):
            return []

        snapshots: List[Dict[str, Any]] = []
        with open(snapshots_log, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snapshot = json.loads(line)
                except json.JSONDecodeError:
                    continue
                snapshot_tick = snapshot.get("tick") or snapshot.get("round")
                if tick is not None and snapshot_tick != tick:
                    continue
                snapshots.append(snapshot)

        snapshots.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        if limit is not None:
            return snapshots[:limit]
        return snapshots
    
    @classmethod
    def get_actions(
        cls,
        simulation_id: str,
        limit: int = 100,
        offset: int = 0,
        platform: Optional[str] = None,
        agent_id: Optional[int] = None,
        round_num: Optional[int] = None
    ) -> List[AgentAction]:
        """
        获取动作历史（带分页）
        
        Args:
            simulation_id: 模拟ID
            limit: 返回数量限制
            offset: 偏移量
            platform: 过滤平台
            agent_id: 过滤Agent
            round_num: 过滤轮次
            
        Returns:
            动作列表
        """
        actions = cls.get_all_actions(
            simulation_id=simulation_id,
            platform=platform,
            agent_id=agent_id,
            round_num=round_num
        )
        
        # 分页
        return actions[offset:offset + limit]
    
    @classmethod
    def get_timeline(
        cls,
        simulation_id: str,
        start_round: int = 0,
        end_round: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        获取模拟时间线（按轮次汇总）
        
        Args:
            simulation_id: 模拟ID
            start_round: 起始轮次
            end_round: 结束轮次
            
        Returns:
            每轮的汇总信息
        """
        state = cls.get_run_state(simulation_id)
        if state and SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
            return cls._get_world_timeline(
                simulation_id=simulation_id,
                start_round=start_round,
                end_round=end_round,
            )

        actions = cls.get_actions(simulation_id, limit=10000)
        
        # 按轮次分组
        rounds: Dict[int, Dict[str, Any]] = {}
        
        for action in actions:
            round_num = action.round_num
            
            if round_num < start_round:
                continue
            if end_round is not None and round_num > end_round:
                continue
            
            if round_num not in rounds:
                rounds[round_num] = {
                    "round_num": round_num,
                    "twitter_actions": 0,
                    "reddit_actions": 0,
                    "world_actions": 0,
                    "active_agents": set(),
                    "action_types": {},
                    "first_action_time": action.timestamp,
                    "last_action_time": action.timestamp,
                }
            
            r = rounds[round_num]
            
            if action.platform == "twitter":
                r["twitter_actions"] += 1
            elif action.platform == "reddit":
                r["reddit_actions"] += 1
            else:
                r["world_actions"] += 1
            
            r["active_agents"].add(action.agent_id)
            r["action_types"][action.action_type] = r["action_types"].get(action.action_type, 0) + 1
            r["last_action_time"] = action.timestamp
        
        # 转换为列表
        result = []
        for round_num in sorted(rounds.keys()):
            r = rounds[round_num]
            result.append({
                "round_num": round_num,
                "twitter_actions": r["twitter_actions"],
                "reddit_actions": r["reddit_actions"],
                "world_actions": r["world_actions"],
                "total_actions": r["twitter_actions"] + r["reddit_actions"] + r["world_actions"],
                "active_agents_count": len(r["active_agents"]),
                "active_agents": list(r["active_agents"]),
                "action_types": r["action_types"],
                "first_action_time": r["first_action_time"],
                "last_action_time": r["last_action_time"],
            })
        
        return result

    @classmethod
    def _get_world_timeline(
        cls,
        simulation_id: str,
        start_round: int = 0,
        end_round: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        lifecycle_types = {
            "intent_created",
            "intent_resolved",
            "intent_deferred",
            "event_started",
            "event_queued",
            "event_completed",
            "provider_waiting",
            "tick_blocked",
            "tick_start",
            "tick_end",
        }
        events = cls.get_world_events(simulation_id, event_types=list(lifecycle_types))
        snapshots = {
            (snapshot.get("tick") or snapshot.get("round")): snapshot
            for snapshot in cls.get_world_snapshots(simulation_id)
        }

        ticks: Dict[int, Dict[str, Any]] = {}
        for event in events:
            tick = event.get("tick") or event.get("round", 0)
            if tick < start_round:
                continue
            if end_round is not None and tick > end_round:
                continue

            entry = ticks.setdefault(
                tick,
                {
                    "tick": tick,
                    "round_num": tick,
                    "intent_created": 0,
                    "intent_resolved": 0,
                    "intent_deferred": 0,
                    "events_started": 0,
                    "events_queued": 0,
                    "events_completed": 0,
                    "provider_waiting": 0,
                    "ticks_blocked": 0,
                    "active_events_count": 0,
                    "queued_events_count": 0,
                    "current_phase": "idle",
                    "summary": "",
                    "world_state": None,
                    "last_event_time": event.get("timestamp"),
                },
            )

            event_type = event.get("event_type")
            if event_type == "intent_created":
                entry["intent_created"] += 1
            elif event_type == "intent_resolved":
                entry["intent_resolved"] += 1
            elif event_type == "intent_deferred":
                entry["intent_deferred"] += 1
            elif event_type == "event_started":
                entry["events_started"] += 1
            elif event_type == "event_queued":
                entry["events_queued"] += 1
            elif event_type == "event_completed":
                entry["events_completed"] += 1
            elif event_type == "provider_waiting":
                entry["provider_waiting"] += 1
            elif event_type == "tick_blocked":
                entry["ticks_blocked"] += 1
            elif event_type in {"tick_start", "tick_end"}:
                entry["current_phase"] = event.get("phase", entry["current_phase"])
            if event_type == "tick_end":
                entry["summary"] = event.get("summary", "")
                if event.get("world_state") is not None:
                    entry["world_state"] = event.get("world_state")
                if event.get("active_events_count") is not None:
                    entry["active_events_count"] = event.get("active_events_count", entry["active_events_count"])
                if event.get("queued_events_count") is not None:
                    entry["queued_events_count"] = event.get("queued_events_count", entry["queued_events_count"])

            entry["last_event_time"] = event.get("timestamp")

        for tick, snapshot in snapshots.items():
            if tick not in ticks:
                if tick < start_round:
                    continue
                if end_round is not None and tick > end_round:
                    continue
                ticks[tick] = {
                    "tick": tick,
                    "round_num": tick,
                    "intent_created": 0,
                    "intent_resolved": 0,
                    "intent_deferred": 0,
                    "events_started": 0,
                    "events_queued": 0,
                    "events_completed": 0,
                    "provider_waiting": 0,
                    "ticks_blocked": 0,
                    "active_events_count": 0,
                    "queued_events_count": 0,
                    "current_phase": "idle",
                    "summary": "",
                    "world_state": None,
                    "last_event_time": snapshot.get("timestamp"),
                }
            ticks[tick]["summary"] = snapshot.get("summary", ticks[tick]["summary"])
            ticks[tick]["active_events_count"] = len(snapshot.get("active_events") or [])
            ticks[tick]["queued_events_count"] = len(snapshot.get("queued_events") or [])
            ticks[tick]["current_phase"] = snapshot.get("phase", ticks[tick]["current_phase"])
            ticks[tick]["world_state"] = snapshot.get("world_state")

        result = [ticks[tick] for tick in sorted(ticks.keys())]
        return result
    
    @classmethod
    def get_agent_stats(cls, simulation_id: str) -> List[Dict[str, Any]]:
        """
        获取每个Agent的统计信息
        
        Returns:
            Agent统计列表
        """
        actions = cls.get_actions(simulation_id, limit=10000)
        
        agent_stats: Dict[int, Dict[str, Any]] = {}
        
        for action in actions:
            agent_id = action.agent_id
            
            if agent_id not in agent_stats:
                agent_stats[agent_id] = {
                    "agent_id": agent_id,
                    "agent_name": action.agent_name,
                    "total_actions": 0,
                    "twitter_actions": 0,
                    "reddit_actions": 0,
                    "world_actions": 0,
                    "action_types": {},
                    "first_action_time": action.timestamp,
                    "last_action_time": action.timestamp,
                }
            
            stats = agent_stats[agent_id]
            stats["total_actions"] += 1
            
            if action.platform == "twitter":
                stats["twitter_actions"] += 1
            elif action.platform == "reddit":
                stats["reddit_actions"] += 1
            else:
                stats["world_actions"] += 1
            
            stats["action_types"][action.action_type] = stats["action_types"].get(action.action_type, 0) + 1
            stats["last_action_time"] = action.timestamp
        
        # 按总动作数排序
        result = sorted(agent_stats.values(), key=lambda x: x["total_actions"], reverse=True)
        
        return result
    
    @classmethod
    def cleanup_simulation_logs(cls, simulation_id: str) -> Dict[str, Any]:
        """
        清理模拟的运行日志（用于强制重新开始模拟）
        
        会删除以下文件：
        - run_state.json
        - twitter/actions.jsonl
        - reddit/actions.jsonl
        - world/actions.jsonl
        - world/state_snapshots.jsonl
        - simulation.log
        - stdout.log / stderr.log
        - twitter_simulation.db（模拟数据库）
        - reddit_simulation.db（模拟数据库）
        - env_status.json（环境状态）
        
        注意：不会删除配置文件（simulation_config.json）和 profile 文件
        
        Args:
            simulation_id: 模拟ID
            
        Returns:
            清理结果信息
        """
        import shutil
        
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        
        if not os.path.exists(sim_dir):
            return {"success": True, "message": "模拟目录不存在，无需清理"}
        
        cleaned_files = []
        errors = []
        
        # 要删除的文件列表（包括数据库文件）
        files_to_delete = [
            "run_state.json",
            "simulation.log",
            "stdout.log",
            "stderr.log",
            "twitter_simulation.db",  # Twitter 平台数据库
            "reddit_simulation.db",   # Reddit 平台数据库
            "env_status.json",        # 环境状态文件
        ]
        
        # 要删除的目录列表（包含动作日志）
        dirs_to_clean = ["twitter", "reddit", "world"]
        
        # 删除文件
        for filename in files_to_delete:
            file_path = os.path.join(sim_dir, filename)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    cleaned_files.append(filename)
                except Exception as e:
                    errors.append(f"删除 {filename} 失败: {str(e)}")
        
        # 清理平台目录中的动作日志
        for dir_name in dirs_to_clean:
            dir_path = os.path.join(sim_dir, dir_name)
            if os.path.exists(dir_path):
                actions_file = os.path.join(dir_path, "actions.jsonl")
                if os.path.exists(actions_file):
                    try:
                        os.remove(actions_file)
                        cleaned_files.append(f"{dir_name}/actions.jsonl")
                    except Exception as e:
                        errors.append(f"删除 {dir_name}/actions.jsonl 失败: {str(e)}")
                if dir_name == "world":
                    snapshots_file = os.path.join(dir_path, "state_snapshots.jsonl")
                    if os.path.exists(snapshots_file):
                        try:
                            os.remove(snapshots_file)
                            cleaned_files.append(f"{dir_name}/state_snapshots.jsonl")
                        except Exception as e:
                            errors.append(f"删除 {dir_name}/state_snapshots.jsonl 失败: {str(e)}")
                    world_state_file = os.path.join(dir_path, "world_state.json")
                    if os.path.exists(world_state_file):
                        try:
                            os.remove(world_state_file)
                            cleaned_files.append(f"{dir_name}/world_state.json")
                        except Exception as e:
                            errors.append(f"删除 {dir_name}/world_state.json 失败: {str(e)}")
                    checkpoint_file = os.path.join(dir_path, "checkpoint.json")
                    if os.path.exists(checkpoint_file):
                        try:
                            os.remove(checkpoint_file)
                            cleaned_files.append(f"{dir_name}/checkpoint.json")
                        except Exception as e:
                            errors.append(f"删除 {dir_name}/checkpoint.json 失败: {str(e)}")
        
        # 清理内存中的运行状态
        if simulation_id in cls._run_states:
            del cls._run_states[simulation_id]
        
        logger.info(f"清理模拟日志完成: {simulation_id}, 删除文件: {cleaned_files}")
        
        return {
            "success": len(errors) == 0,
            "cleaned_files": cleaned_files,
            "errors": errors if errors else None
        }
    
    # 防止重复清理的标志
    _cleanup_done = False
    
    @classmethod
    def cleanup_all_simulations(cls):
        """
        清理所有运行中的模拟进程
        
        在服务器关闭时调用，确保所有子进程被终止
        """
        # 防止重复清理
        if cls._cleanup_done:
            return
        cls._cleanup_done = True
        
        # 检查是否有内容需要清理（避免空进程的进程打印无用日志）
        has_processes = bool(cls._processes)
        has_updaters = bool(cls._graph_memory_enabled)
        
        if not has_processes and not has_updaters:
            return  # 没有需要清理的内容，静默返回
        
        logger.info("正在清理所有模拟进程...")
        
        # 首先停止所有图谱记忆更新器（stop_all 内部会打印日志）
        try:
            ZepGraphMemoryManager.stop_all()
        except Exception as e:
            logger.error(f"停止图谱记忆更新器失败: {e}")
        cls._graph_memory_enabled.clear()
        
        # 复制字典以避免在迭代时修改
        processes = list(cls._processes.items())
        
        for simulation_id, process in processes:
            try:
                if process.poll() is None:  # 进程仍在运行
                    logger.info(f"终止模拟进程: {simulation_id}, pid={process.pid}")
                    
                    try:
                        # 使用跨平台的进程终止方法
                        cls._terminate_process(process, simulation_id, timeout=5)
                    except (ProcessLookupError, OSError):
                        # 进程可能已经不存在，尝试直接终止
                        try:
                            process.terminate()
                            process.wait(timeout=3)
                        except Exception:
                            process.kill()
                    
                    # 更新 run_state.json
                    state = cls.get_run_state(simulation_id)
                    if state:
                        state.runner_status = RunnerStatus.STOPPED
                        state.twitter_running = False
                        state.reddit_running = False
                        state.world_running = False
                        state.completed_at = datetime.now().isoformat()
                        state.error = "服务器关闭，模拟被终止"
                        cls._save_run_state(state)
                    
                    # 同时更新 state.json，将状态设为 stopped
                    try:
                        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
                        state_file = os.path.join(sim_dir, "state.json")
                        logger.info(f"尝试更新 state.json: {state_file}")
                        if os.path.exists(state_file):
                            with open(state_file, 'r', encoding='utf-8') as f:
                                state_data = json.load(f)
                            state_data['status'] = 'stopped'
                            state_data['updated_at'] = datetime.now().isoformat()
                            with open(state_file, 'w', encoding='utf-8') as f:
                                json.dump(state_data, f, indent=2, ensure_ascii=False)
                            logger.info(f"已更新 state.json 状态为 stopped: {simulation_id}")
                        else:
                            logger.warning(f"state.json 不存在: {state_file}")
                    except Exception as state_err:
                        logger.warning(f"更新 state.json 失败: {simulation_id}, error={state_err}")
                        
            except Exception as e:
                logger.error(f"清理进程失败: {simulation_id}, error={e}")
        
        # 清理文件句柄
        for simulation_id, file_handle in list(cls._stdout_files.items()):
            try:
                if file_handle:
                    file_handle.close()
            except Exception:
                pass
        cls._stdout_files.clear()
        
        for simulation_id, file_handle in list(cls._stderr_files.items()):
            try:
                if file_handle:
                    file_handle.close()
            except Exception:
                pass
        cls._stderr_files.clear()
        
        # 清理内存中的状态
        cls._processes.clear()
        cls._action_queues.clear()
        
        logger.info("模拟进程清理完成")
    
    @classmethod
    def register_cleanup(cls):
        """
        注册清理函数
        
        在 Flask 应用启动时调用，确保服务器关闭时清理所有模拟进程
        """
        global _cleanup_registered
        
        if _cleanup_registered:
            return
        
        # Flask debug 模式下，只在 reloader 子进程中注册清理（实际运行应用的进程）
        # WERKZEUG_RUN_MAIN=true 表示是 reloader 子进程
        # 如果不是 debug 模式，则没有这个环境变量，也需要注册
        is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
        is_debug_mode = os.environ.get('FLASK_DEBUG') == '1' or os.environ.get('WERKZEUG_RUN_MAIN') is not None
        
        # 在 debug 模式下，只在 reloader 子进程中注册；非 debug 模式下始终注册
        if is_debug_mode and not is_reloader_process:
            _cleanup_registered = True  # 标记已注册，防止子进程再次尝试
            return
        
        # 保存原有的信号处理器
        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)
        # SIGHUP 只在 Unix 系统存在（macOS/Linux），Windows 没有
        original_sighup = None
        has_sighup = hasattr(signal, 'SIGHUP')
        if has_sighup:
            original_sighup = signal.getsignal(signal.SIGHUP)
        
        def cleanup_handler(signum=None, frame=None):
            """信号处理器：先清理模拟进程，再调用原处理器"""
            # 只有在有进程需要清理时才打印日志
            if cls._processes or cls._graph_memory_enabled:
                logger.info(f"收到信号 {signum}，开始清理...")
            cls.cleanup_all_simulations()
            
            # 调用原有的信号处理器，让 Flask 正常退出
            if signum == signal.SIGINT and callable(original_sigint):
                original_sigint(signum, frame)
            elif signum == signal.SIGTERM and callable(original_sigterm):
                original_sigterm(signum, frame)
            elif has_sighup and signum == signal.SIGHUP:
                # SIGHUP: 终端关闭时发送
                if callable(original_sighup):
                    original_sighup(signum, frame)
                else:
                    # 默认行为：正常退出
                    sys.exit(0)
            else:
                # 如果原处理器不可调用（如 SIG_DFL），则使用默认行为
                raise KeyboardInterrupt
        
        # 注册 atexit 处理器（作为备用）
        atexit.register(cls.cleanup_all_simulations)
        
        # 注册信号处理器（仅在主线程中）
        try:
            # SIGTERM: kill 命令默认信号
            signal.signal(signal.SIGTERM, cleanup_handler)
            # SIGINT: Ctrl+C
            signal.signal(signal.SIGINT, cleanup_handler)
            # SIGHUP: 终端关闭（仅 Unix 系统）
            if has_sighup:
                signal.signal(signal.SIGHUP, cleanup_handler)
        except ValueError:
            # 不在主线程中，只能使用 atexit
            logger.warning("无法注册信号处理器（不在主线程），仅使用 atexit")
        
        _cleanup_registered = True
    
    @classmethod
    def get_running_simulations(cls) -> List[str]:
        """
        获取所有正在运行的模拟ID列表
        """
        running = []
        for sim_id, process in cls._processes.items():
            if process.poll() is None:
                running.append(sim_id)
        return running

    @classmethod
    def _get_simulation_mode(cls, simulation_id: str) -> SimulationMode:
        config_path = os.path.join(cls.RUN_STATE_DIR, simulation_id, "simulation_config.json")
        if not os.path.exists(config_path):
            return SimulationMode.SOCIAL
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return SimulationMode.normalize(config.get("simulation_mode"))
        except Exception:
            return SimulationMode.SOCIAL
    
    # ============== Interview 功能 ==============
    
    @classmethod
    def check_env_alive(cls, simulation_id: str) -> bool:
        """
        检查模拟环境是否存活（可以接收Interview命令）

        Args:
            simulation_id: 模拟ID

        Returns:
            True 表示环境存活，False 表示环境已关闭
        """
        if cls._get_simulation_mode(simulation_id) == SimulationMode.WORLD:
            return False
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            return False

        ipc_client = SimulationIPCClient(sim_dir)
        return ipc_client.check_env_alive()

    @classmethod
    def get_env_status_detail(cls, simulation_id: str) -> Dict[str, Any]:
        """
        获取模拟环境的详细状态信息

        Args:
            simulation_id: 模拟ID

        Returns:
            状态详情字典，包含 status, twitter_available, reddit_available, timestamp
        """
        if cls._get_simulation_mode(simulation_id) == SimulationMode.WORLD:
            return {
                "status": "unsupported",
                "twitter_available": False,
                "reddit_available": False,
                "world_available": False,
                "message": "world 模式暂不支持实时采访环境",
                "timestamp": None,
            }
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        status_file = os.path.join(sim_dir, "env_status.json")
        
        default_status = {
            "status": "stopped",
            "twitter_available": False,
            "reddit_available": False,
            "world_available": False,
            "timestamp": None
        }
        
        if not os.path.exists(status_file):
            return default_status
        
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                status = json.load(f)
            return {
                "status": status.get("status", "stopped"),
                "twitter_available": status.get("twitter_available", False),
                "reddit_available": status.get("reddit_available", False),
                "world_available": status.get("world_available", False),
                "timestamp": status.get("timestamp")
            }
        except (json.JSONDecodeError, OSError):
            return default_status

    @classmethod
    def interview_agent(
        cls,
        simulation_id: str,
        agent_id: int,
        prompt: str,
        platform: str = None,
        timeout: float = 60.0
    ) -> Dict[str, Any]:
        """
        采访单个Agent

        Args:
            simulation_id: 模拟ID
            agent_id: Agent ID
            prompt: 采访问题
            platform: 指定平台（可选）
                - "twitter": 只采访Twitter平台
                - "reddit": 只采访Reddit平台
                - None: 双平台模拟时同时采访两个平台，返回整合结果
            timeout: 超时时间（秒）

        Returns:
            采访结果字典

        Raises:
            ValueError: 模拟不存在或环境未运行
            TimeoutError: 等待响应超时
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"模拟不存在: {simulation_id}")

        if cls._get_simulation_mode(simulation_id) == SimulationMode.WORLD:
            raise ValueError("world 模式暂不支持实时角色采访")

        ipc_client = SimulationIPCClient(sim_dir)

        if not ipc_client.check_env_alive():
            raise ValueError(f"模拟环境未运行或已关闭，无法执行Interview: {simulation_id}")

        logger.info(f"发送Interview命令: simulation_id={simulation_id}, agent_id={agent_id}, platform={platform}")

        response = ipc_client.send_interview(
            agent_id=agent_id,
            prompt=prompt,
            platform=platform,
            timeout=timeout
        )

        if response.status.value == "completed":
            return {
                "success": True,
                "agent_id": agent_id,
                "prompt": prompt,
                "result": response.result,
                "timestamp": response.timestamp
            }
        else:
            return {
                "success": False,
                "agent_id": agent_id,
                "prompt": prompt,
                "error": response.error,
                "timestamp": response.timestamp
            }
    
    @classmethod
    def interview_agents_batch(
        cls,
        simulation_id: str,
        interviews: List[Dict[str, Any]],
        platform: str = None,
        timeout: float = 120.0
    ) -> Dict[str, Any]:
        """
        批量采访多个Agent

        Args:
            simulation_id: 模拟ID
            interviews: 采访列表，每个元素包含 {"agent_id": int, "prompt": str, "platform": str(可选)}
            platform: 默认平台（可选，会被每个采访项的platform覆盖）
                - "twitter": 默认只采访Twitter平台
                - "reddit": 默认只采访Reddit平台
                - None: 双平台模拟时每个Agent同时采访两个平台
            timeout: 超时时间（秒）

        Returns:
            批量采访结果字典

        Raises:
            ValueError: 模拟不存在或环境未运行
            TimeoutError: 等待响应超时
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"模拟不存在: {simulation_id}")

        if cls._get_simulation_mode(simulation_id) == SimulationMode.WORLD:
            raise ValueError("world 模式暂不支持实时角色采访")

        ipc_client = SimulationIPCClient(sim_dir)

        if not ipc_client.check_env_alive():
            raise ValueError(f"模拟环境未运行或已关闭，无法执行Interview: {simulation_id}")

        logger.info(f"发送批量Interview命令: simulation_id={simulation_id}, count={len(interviews)}, platform={platform}")

        response = ipc_client.send_batch_interview(
            interviews=interviews,
            platform=platform,
            timeout=timeout
        )

        if response.status.value == "completed":
            return {
                "success": True,
                "interviews_count": len(interviews),
                "result": response.result,
                "timestamp": response.timestamp
            }
        else:
            return {
                "success": False,
                "interviews_count": len(interviews),
                "error": response.error,
                "timestamp": response.timestamp
            }
    
    @classmethod
    def interview_all_agents(
        cls,
        simulation_id: str,
        prompt: str,
        platform: str = None,
        timeout: float = 180.0
    ) -> Dict[str, Any]:
        """
        采访所有Agent（全局采访）

        使用相同的问题采访模拟中的所有Agent

        Args:
            simulation_id: 模拟ID
            prompt: 采访问题（所有Agent使用相同问题）
            platform: 指定平台（可选）
                - "twitter": 只采访Twitter平台
                - "reddit": 只采访Reddit平台
                - None: 双平台模拟时每个Agent同时采访两个平台
            timeout: 超时时间（秒）

        Returns:
            全局采访结果字典
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"模拟不存在: {simulation_id}")

        if cls._get_simulation_mode(simulation_id) == SimulationMode.WORLD:
            raise ValueError("world 模式暂不支持实时角色采访")

        # 从配置文件获取所有Agent信息
        config_path = os.path.join(sim_dir, "simulation_config.json")
        if not os.path.exists(config_path):
            raise ValueError(f"模拟配置不存在: {simulation_id}")

        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        agent_configs = config.get("agent_configs", [])
        if not agent_configs:
            raise ValueError(f"模拟配置中没有Agent: {simulation_id}")

        # 构建批量采访列表
        interviews = []
        for agent_config in agent_configs:
            agent_id = agent_config.get("agent_id")
            if agent_id is not None:
                interviews.append({
                    "agent_id": agent_id,
                    "prompt": prompt
                })

        logger.info(f"发送全局Interview命令: simulation_id={simulation_id}, agent_count={len(interviews)}, platform={platform}")

        return cls.interview_agents_batch(
            simulation_id=simulation_id,
            interviews=interviews,
            platform=platform,
            timeout=timeout
        )
    
    @classmethod
    def close_simulation_env(
        cls,
        simulation_id: str,
        timeout: float = 30.0
    ) -> Dict[str, Any]:
        """
        关闭模拟环境（而不是停止模拟进程）
        
        向模拟发送关闭环境命令，使其优雅退出等待命令模式
        
        Args:
            simulation_id: 模拟ID
            timeout: 超时时间（秒）
            
        Returns:
            操作结果字典
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        if not os.path.exists(sim_dir):
            raise ValueError(f"模拟不存在: {simulation_id}")

        if cls._get_simulation_mode(simulation_id) == SimulationMode.WORLD:
            return {
                "success": True,
                "message": "world 模式没有可关闭的实时采访环境",
            }
        
        ipc_client = SimulationIPCClient(sim_dir)
        
        if not ipc_client.check_env_alive():
            return {
                "success": True,
                "message": "环境已经关闭"
            }
        
        logger.info(f"发送关闭环境命令: simulation_id={simulation_id}")
        
        try:
            response = ipc_client.send_close_env(timeout=timeout)
            
            return {
                "success": response.status.value == "completed",
                "message": "环境关闭命令已发送",
                "result": response.result,
                "timestamp": response.timestamp
            }
        except TimeoutError:
            # 超时可能是因为环境正在关闭
            return {
                "success": True,
                "message": "环境关闭命令已发送（等待响应超时，环境可能正在关闭）"
            }
    
    @classmethod
    def _get_interview_history_from_db(
        cls,
        db_path: str,
        platform_name: str,
        agent_id: Optional[int] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """从单个数据库获取Interview历史"""
        import sqlite3
        
        if not os.path.exists(db_path):
            return []
        
        results = []
        
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            if agent_id is not None:
                cursor.execute("""
                    SELECT user_id, info, created_at
                    FROM trace
                    WHERE action = 'interview' AND user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (agent_id, limit))
            else:
                cursor.execute("""
                    SELECT user_id, info, created_at
                    FROM trace
                    WHERE action = 'interview'
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,))
            
            for user_id, info_json, created_at in cursor.fetchall():
                try:
                    info = json.loads(info_json) if info_json else {}
                except json.JSONDecodeError:
                    info = {"raw": info_json}
                
                results.append({
                    "agent_id": user_id,
                    "response": info.get("response", info),
                    "prompt": info.get("prompt", ""),
                    "timestamp": created_at,
                    "platform": platform_name
                })
            
            conn.close()
            
        except Exception as e:
            logger.error(f"读取Interview历史失败 ({platform_name}): {e}")
        
        return results

    @classmethod
    def get_interview_history(
        cls,
        simulation_id: str,
        platform: str = None,
        agent_id: Optional[int] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取Interview历史记录（从数据库读取）
        
        Args:
            simulation_id: 模拟ID
            platform: 平台类型（reddit/twitter/None）
                - "reddit": 只获取Reddit平台的历史
                - "twitter": 只获取Twitter平台的历史
                - None: 获取两个平台的所有历史
            agent_id: 指定Agent ID（可选，只获取该Agent的历史）
            limit: 每个平台返回数量限制
            
        Returns:
            Interview历史记录列表
        """
        sim_dir = os.path.join(cls.RUN_STATE_DIR, simulation_id)
        
        results = []
        
        # 确定要查询的平台
        if platform in ("reddit", "twitter"):
            platforms = [platform]
        else:
            # 不指定platform时，查询两个平台
            platforms = ["twitter", "reddit"]
        
        for p in platforms:
            db_path = os.path.join(sim_dir, f"{p}_simulation.db")
            platform_results = cls._get_interview_history_from_db(
                db_path=db_path,
                platform_name=p,
                agent_id=agent_id,
                limit=limit
            )
            results.extend(platform_results)
        
        # 按时间降序排序
        results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        # 如果查询了多个平台，限制总数
        if len(platforms) > 1 and len(results) > limit:
            results = results[:limit]
        
        return results
