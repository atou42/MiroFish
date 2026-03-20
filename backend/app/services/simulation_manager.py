"""
OASIS模拟管理器
管理Twitter和Reddit双平台并行模拟
使用预设脚本 + LLM智能生成配置参数
"""

import os
import json
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..config import Config
from ..utils.logger import get_logger
from ..models.simulation_mode import SimulationMode
from ..config import Config
from .zep_entity_reader import ZepEntityReader
from .oasis_profile_generator import OasisProfileGenerator
from .simulation_config_generator import SimulationConfigGenerator
from .world_profile_generator import WorldProfileGenerator
from .world_config_generator import WorldConfigGenerator
from .world_preset_registry import WorldPresetRegistry

logger = get_logger('mirofish.simulation')


class SimulationStatus(str, Enum):
    """模拟状态"""
    CREATED = "created"
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"      # 模拟被手动停止
    COMPLETED = "completed"  # 模拟自然完成
    FAILED = "failed"


class PlatformType(str, Enum):
    """平台类型"""
    TWITTER = "twitter"
    REDDIT = "reddit"


@dataclass
class SimulationState:
    """模拟状态"""
    simulation_id: str
    project_id: str
    graph_id: str
    simulation_mode: str = SimulationMode.SOCIAL.value
    
    # 平台启用状态
    enable_twitter: bool = True
    enable_reddit: bool = True
    
    # 状态
    status: SimulationStatus = SimulationStatus.CREATED
    
    # 准备阶段数据
    entities_count: int = 0
    profiles_count: int = 0
    entity_types: List[str] = field(default_factory=list)
    
    # 配置生成信息
    config_generated: bool = False
    config_reasoning: str = ""
    runtime_metadata: Dict[str, Any] = field(default_factory=dict)
    
    # 运行时数据
    current_round: int = 0
    twitter_status: str = "not_started"
    reddit_status: str = "not_started"
    
    # 时间戳
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # 错误信息
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """完整状态字典（内部使用）"""
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "simulation_mode": self.simulation_mode,
            "enable_twitter": self.enable_twitter,
            "enable_reddit": self.enable_reddit,
            "status": self.status.value,
            "entities_count": self.entities_count,
            "profiles_count": self.profiles_count,
            "entity_types": self.entity_types,
            "config_generated": self.config_generated,
            "config_reasoning": self.config_reasoning,
            "runtime_metadata": self.runtime_metadata,
            "current_round": self.current_round,
            "twitter_status": self.twitter_status,
            "reddit_status": self.reddit_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }
    
    def to_simple_dict(self) -> Dict[str, Any]:
        """简化状态字典（API返回使用）"""
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "simulation_mode": self.simulation_mode,
            "status": self.status.value,
            "entities_count": self.entities_count,
            "profiles_count": self.profiles_count,
            "entity_types": self.entity_types,
            "config_generated": self.config_generated,
            "runtime_metadata": self.runtime_metadata,
            "error": self.error,
        }


class SimulationManager:
    """
    模拟管理器
    
    核心功能：
    1. 从Zep图谱读取实体并过滤
    2. 生成OASIS Agent Profile
    3. 使用LLM智能生成模拟配置参数
    4. 准备预设脚本所需的所有文件
    """
    
    # 模拟数据存储目录
    SIMULATION_DATA_DIR = os.path.join(
        os.path.dirname(__file__), 
        '../../uploads/simulations'
    )
    
    def __init__(self):
        # 确保目录存在
        os.makedirs(self.SIMULATION_DATA_DIR, exist_ok=True)
        
        # 内存中的模拟状态缓存
        self._simulations: Dict[str, SimulationState] = {}
    
    def _get_simulation_dir(self, simulation_id: str) -> str:
        """获取模拟数据目录"""
        sim_dir = os.path.join(self.SIMULATION_DATA_DIR, simulation_id)
        os.makedirs(sim_dir, exist_ok=True)
        return sim_dir
    
    def _save_simulation_state(self, state: SimulationState):
        """保存模拟状态到文件"""
        sim_dir = self._get_simulation_dir(state.simulation_id)
        state_file = os.path.join(sim_dir, "state.json")
        
        state.updated_at = datetime.now().isoformat()
        
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        
        self._simulations[state.simulation_id] = state
    
    def _load_simulation_state(self, simulation_id: str) -> Optional[SimulationState]:
        """从文件加载模拟状态"""
        if simulation_id in self._simulations:
            return self._simulations[simulation_id]
        
        sim_dir = self._get_simulation_dir(simulation_id)
        state_file = os.path.join(sim_dir, "state.json")
        
        if not os.path.exists(state_file):
            return None
        
        with open(state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        state = SimulationState(
            simulation_id=simulation_id,
            project_id=data.get("project_id", ""),
            graph_id=data.get("graph_id", ""),
            simulation_mode=SimulationMode.normalize(data.get("simulation_mode")).value,
            enable_twitter=data.get("enable_twitter", True),
            enable_reddit=data.get("enable_reddit", True),
            status=SimulationStatus(data.get("status", "created")),
            entities_count=data.get("entities_count", 0),
            profiles_count=data.get("profiles_count", 0),
            entity_types=data.get("entity_types", []),
            config_generated=data.get("config_generated", False),
            config_reasoning=data.get("config_reasoning", ""),
            runtime_metadata=data.get("runtime_metadata", {}),
            current_round=data.get("current_round", 0),
            twitter_status=data.get("twitter_status", "not_started"),
            reddit_status=data.get("reddit_status", "not_started"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            error=data.get("error"),
        )
        
        self._simulations[simulation_id] = state
        return state
    
    def create_simulation(
        self,
        project_id: str,
        graph_id: str,
        enable_twitter: bool = True,
        enable_reddit: bool = True,
        simulation_mode: str = SimulationMode.SOCIAL.value,
    ) -> SimulationState:
        """
        创建新的模拟
        
        Args:
            project_id: 项目ID
            graph_id: Zep图谱ID
            enable_twitter: 是否启用Twitter模拟
            enable_reddit: 是否启用Reddit模拟
            
        Returns:
            SimulationState
        """
        import uuid
        simulation_id = f"sim_{uuid.uuid4().hex[:12]}"
        
        state = SimulationState(
            simulation_id=simulation_id,
            project_id=project_id,
            graph_id=graph_id,
            simulation_mode=SimulationMode.normalize(simulation_mode).value,
            enable_twitter=enable_twitter if SimulationMode.normalize(simulation_mode) == SimulationMode.SOCIAL else False,
            enable_reddit=enable_reddit if SimulationMode.normalize(simulation_mode) == SimulationMode.SOCIAL else False,
            status=SimulationStatus.CREATED,
        )
        
        self._save_simulation_state(state)
        logger.info(f"创建模拟: {simulation_id}, project={project_id}, graph={graph_id}")
        
        return state
    
    def prepare_simulation(
        self,
        simulation_id: str,
        simulation_requirement: str,
        document_text: str,
        defined_entity_types: Optional[List[str]] = None,
        use_llm_for_profiles: bool = True,
        progress_callback: Optional[callable] = None,
        parallel_profile_count: int = 3,
        world_preset_id: Optional[str] = None,
    ) -> SimulationState:
        """
        准备模拟环境（全程自动化）
        
        步骤：
        1. 从Zep图谱读取并过滤实体
        2. 为每个实体生成OASIS Agent Profile（可选LLM增强，支持并行）
        3. 使用LLM智能生成模拟配置参数（时间、活跃度、发言频率等）
        4. 保存配置文件和Profile文件
        5. 复制预设脚本到模拟目录
        
        Args:
            simulation_id: 模拟ID
            simulation_requirement: 模拟需求描述（用于LLM生成配置）
            document_text: 原始文档内容（用于LLM理解背景）
            defined_entity_types: 预定义的实体类型（可选）
            use_llm_for_profiles: 是否使用LLM生成详细人设
            progress_callback: 进度回调函数 (stage, progress, message)
            parallel_profile_count: 并行生成人设的数量，默认3
            
        Returns:
            SimulationState
        """
        state = self._load_simulation_state(simulation_id)
        if not state:
            raise ValueError(f"模拟不存在: {simulation_id}")
        
        try:
            state.status = SimulationStatus.PREPARING
            self._save_simulation_state(state)
            
            sim_dir = self._get_simulation_dir(simulation_id)
            
            # ========== 阶段1: 读取并过滤实体 ==========
            if progress_callback:
                progress_callback("reading", 0, "正在连接Zep图谱...")
            
            reader = ZepEntityReader()
            
            if progress_callback:
                progress_callback("reading", 30, "正在读取节点数据...")
            
            filtered = reader.filter_defined_entities(
                graph_id=state.graph_id,
                defined_entity_types=defined_entity_types,
                enrich_with_edges=True
            )
            
            state.entities_count = filtered.filtered_count
            state.entity_types = list(filtered.entity_types)
            
            if progress_callback:
                progress_callback(
                    "reading", 100, 
                    f"完成，共 {filtered.filtered_count} 个实体",
                    current=filtered.filtered_count,
                    total=filtered.filtered_count
                )
            
            if filtered.filtered_count == 0:
                state.status = SimulationStatus.FAILED
                state.error = "没有找到符合条件的实体，请检查图谱是否正确构建"
                self._save_simulation_state(state)
                return state
            if SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
                self._prepare_world_runtime(
                    state=state,
                    sim_dir=sim_dir,
                    filtered=filtered,
                    simulation_id=simulation_id,
                    simulation_requirement=simulation_requirement,
                    document_text=document_text,
                    use_llm_for_profiles=use_llm_for_profiles,
                    progress_callback=progress_callback,
                    parallel_profile_count=parallel_profile_count,
                    world_preset_id=world_preset_id,
                )
            else:
                self._prepare_social_runtime(
                    state=state,
                    sim_dir=sim_dir,
                    filtered=filtered,
                    simulation_id=simulation_id,
                    simulation_requirement=simulation_requirement,
                    document_text=document_text,
                    use_llm_for_profiles=use_llm_for_profiles,
                    progress_callback=progress_callback,
                    parallel_profile_count=parallel_profile_count,
                )
            
            # 注意：运行脚本保留在 backend/scripts/ 目录，不再复制到模拟目录
            # 启动模拟时，simulation_runner 会从 scripts/ 目录运行脚本
            
            # 更新状态
            state.status = SimulationStatus.READY
            self._save_simulation_state(state)
            
            logger.info(f"模拟准备完成: {simulation_id}, "
                       f"entities={state.entities_count}, profiles={state.profiles_count}")
            
            return state
            
        except Exception as e:
            logger.error(f"模拟准备失败: {simulation_id}, error={str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            state.status = SimulationStatus.FAILED
            state.error = str(e)
            self._save_simulation_state(state)
            raise

    def _prepare_social_runtime(
        self,
        state: SimulationState,
        sim_dir: str,
        filtered: Any,
        simulation_id: str,
        simulation_requirement: str,
        document_text: str,
        use_llm_for_profiles: bool,
        progress_callback: Optional[callable],
        parallel_profile_count: int,
    ) -> None:
        total_entities = len(filtered.entities)

        if progress_callback:
            progress_callback(
                "generating_profiles",
                0,
                "开始生成...",
                current=0,
                total=total_entities,
            )

        generator = OasisProfileGenerator(graph_id=state.graph_id)

        def profile_progress(current, total, msg):
            if progress_callback:
                progress_callback(
                    "generating_profiles",
                    int(current / max(total, 1) * 100),
                    msg,
                    current=current,
                    total=total,
                    item_name=msg,
                )

        realtime_output_path = None
        realtime_platform = "reddit"
        if state.enable_reddit:
            realtime_output_path = os.path.join(sim_dir, "reddit_profiles.json")
            realtime_platform = "reddit"
        elif state.enable_twitter:
            realtime_output_path = os.path.join(sim_dir, "twitter_profiles.csv")
            realtime_platform = "twitter"

        profiles = generator.generate_profiles_from_entities(
            entities=filtered.entities,
            use_llm=use_llm_for_profiles,
            progress_callback=profile_progress,
            graph_id=state.graph_id,
            parallel_count=parallel_profile_count,
            realtime_output_path=realtime_output_path,
            output_platform=realtime_platform,
        )

        state.profiles_count = len(profiles)

        if progress_callback:
            progress_callback(
                "generating_profiles",
                95,
                "保存Profile文件...",
                current=total_entities,
                total=total_entities,
            )

        if state.enable_reddit:
            generator.save_profiles(
                profiles=profiles,
                file_path=os.path.join(sim_dir, "reddit_profiles.json"),
                platform="reddit",
            )

        if state.enable_twitter:
            generator.save_profiles(
                profiles=profiles,
                file_path=os.path.join(sim_dir, "twitter_profiles.csv"),
                platform="twitter",
            )

        if progress_callback:
            progress_callback(
                "generating_profiles",
                100,
                f"完成，共 {len(profiles)} 个Profile",
                current=len(profiles),
                total=len(profiles),
            )

        if progress_callback:
            progress_callback(
                "generating_config",
                0,
                "正在分析模拟需求...",
                current=0,
                total=3,
            )

        config_generator = SimulationConfigGenerator()

        if progress_callback:
            progress_callback(
                "generating_config",
                30,
                "正在调用LLM生成配置...",
                current=1,
                total=3,
            )

        sim_params = config_generator.generate_config(
            simulation_id=simulation_id,
            project_id=state.project_id,
            graph_id=state.graph_id,
            simulation_requirement=simulation_requirement,
            document_text=document_text,
            entities=filtered.entities,
            enable_twitter=state.enable_twitter,
            enable_reddit=state.enable_reddit,
        )

        if progress_callback:
            progress_callback(
                "generating_config",
                70,
                "正在保存配置文件...",
                current=2,
                total=3,
            )

        config_path = os.path.join(sim_dir, "simulation_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(sim_params.to_json())

        state.config_generated = True
        state.config_reasoning = sim_params.generation_reasoning
        state.runtime_metadata = {
            "profile_format": "dual_platform",
            "profiles_file": "reddit_profiles.json" if state.enable_reddit else "twitter_profiles.csv",
        }

        if progress_callback:
            progress_callback(
                "generating_config",
                100,
                "配置生成完成",
                current=3,
                total=3,
            )

    def _prepare_world_runtime(
        self,
        state: SimulationState,
        sim_dir: str,
        filtered: Any,
        simulation_id: str,
        simulation_requirement: str,
        document_text: str,
        use_llm_for_profiles: bool,
        progress_callback: Optional[callable],
        parallel_profile_count: int,
        world_preset_id: Optional[str],
    ) -> None:
        world_preset = WorldPresetRegistry.get_preset(world_preset_id)
        total_entities = len(filtered.entities)
        if progress_callback:
            progress_callback(
                "generating_profiles",
                0,
                "正在构建世界实体卡...",
                current=0,
                total=total_entities,
            )

        profile_generator = WorldProfileGenerator()

        def profile_progress(current, total, msg):
            if progress_callback:
                progress_callback(
                    "generating_profiles",
                    int(current / max(total, 1) * 100),
                    f"整理世界实体: {msg}",
                    current=current,
                    total=total,
                    item_name=msg,
                )

        world_profiles_path = os.path.join(sim_dir, "world_profiles.json")
        profiles = profile_generator.generate_profiles_from_entities(
            entities=filtered.entities,
            use_llm=use_llm_for_profiles,
            progress_callback=profile_progress,
            realtime_output_path=world_profiles_path,
            parallel_count=parallel_profile_count,
            world_preset=world_preset,
        )

        state.profiles_count = len(profiles)
        profile_generator.save_profiles(profiles, world_profiles_path)

        if progress_callback:
            progress_callback(
                "generating_profiles",
                100,
                f"完成，共 {len(profiles)} 个世界实体卡",
                current=len(profiles),
                total=len(profiles),
            )

        if progress_callback:
            progress_callback(
                "generating_config",
                0,
                "正在梳理世界推进节奏...",
                current=0,
                total=3,
            )

        config_generator = WorldConfigGenerator()
        world_config = config_generator.generate_config(
            simulation_id=simulation_id,
            project_id=state.project_id,
            graph_id=state.graph_id,
            simulation_requirement=simulation_requirement,
            document_text=document_text,
            entities=filtered.entities,
            profiles=profiles,
            world_preset=world_preset,
        )

        if progress_callback:
            progress_callback(
                "generating_config",
                70,
                "正在保存 world 配置...",
                current=2,
                total=3,
            )

        config_path = os.path.join(sim_dir, "simulation_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(world_config.to_json())

        state.config_generated = True
        state.config_reasoning = world_config.generation_reasoning
        state.enable_twitter = False
        state.enable_reddit = False
        actor_llm_config = Config.get_llm_config(selector=world_preset.actor_selector or "WORLD_AGENT")
        resolver_llm_config = Config.get_llm_config(selector=world_preset.resolver_selector or "WORLD_RESOLVER")

        state.runtime_metadata = {
            "profile_format": "world",
            "profiles_file": "world_profiles.json",
            "world_preset_id": world_preset.preset_id,
            "world_preset_label": world_preset.label,
            "world_preset_strategy_class": world_preset.strategy_class,
            "world_preset_actor_selector": world_preset.actor_selector,
            "world_preset_resolver_selector": world_preset.resolver_selector,
            "world_preset_tags": world_preset.tags,
            "actor_count": len(world_config.agent_configs),
            "pressure_track_count": len(world_config.pressure_tracks),
            "tick_unit": world_config.time_config.get("scene_unit", "tick"),
            "runtime_config": world_config.runtime_config,
            "world_agent_model": actor_llm_config.get("model_name"),
            "world_agent_provider": actor_llm_config.get("provider_id"),
            "world_agent_profile": actor_llm_config.get("profile_id"),
            "world_resolver_model": resolver_llm_config.get("model_name"),
            "world_resolver_provider": resolver_llm_config.get("provider_id"),
            "world_resolver_profile": resolver_llm_config.get("profile_id"),
        }

        if progress_callback:
            progress_callback(
                "generating_config",
                100,
                "world 配置生成完成",
                current=3,
                total=3,
            )

    def get_simulation(self, simulation_id: str) -> Optional[SimulationState]:
        """获取模拟状态"""
        return self._load_simulation_state(simulation_id)
    
    def list_simulations(self, project_id: Optional[str] = None) -> List[SimulationState]:
        """列出所有模拟"""
        simulations = []
        
        if os.path.exists(self.SIMULATION_DATA_DIR):
            for sim_id in os.listdir(self.SIMULATION_DATA_DIR):
                # 跳过隐藏文件（如 .DS_Store）和非目录文件
                sim_path = os.path.join(self.SIMULATION_DATA_DIR, sim_id)
                if sim_id.startswith('.') or not os.path.isdir(sim_path):
                    continue
                
                state = self._load_simulation_state(sim_id)
                if state:
                    if project_id is None or state.project_id == project_id:
                        simulations.append(state)
        
        return simulations
    
    def get_profiles(self, simulation_id: str, platform: str = "reddit") -> List[Dict[str, Any]]:
        """获取模拟的Agent Profile"""
        state = self._load_simulation_state(simulation_id)
        if not state:
            raise ValueError(f"模拟不存在: {simulation_id}")
        
        sim_dir = self._get_simulation_dir(simulation_id)
        if SimulationMode.normalize(state.simulation_mode) == SimulationMode.WORLD:
            profile_path = os.path.join(sim_dir, "world_profiles.json")
        else:
            if platform == "twitter":
                profile_path = os.path.join(sim_dir, "twitter_profiles.csv")
            else:
                profile_path = os.path.join(sim_dir, "reddit_profiles.json")
        
        if not os.path.exists(profile_path):
            return []

        if profile_path.endswith(".csv"):
            import csv

            with open(profile_path, 'r', encoding='utf-8') as f:
                return list(csv.DictReader(f))

        with open(profile_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def get_simulation_config(self, simulation_id: str) -> Optional[Dict[str, Any]]:
        """获取模拟配置"""
        sim_dir = self._get_simulation_dir(simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        
        if not os.path.exists(config_path):
            return None
        
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def get_run_instructions(self, simulation_id: str) -> Dict[str, str]:
        """获取运行说明"""
        sim_dir = self._get_simulation_dir(simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")
        scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../scripts'))
        state = self._load_simulation_state(simulation_id)
        mode = SimulationMode.normalize(state.simulation_mode if state else None)

        if mode == SimulationMode.WORLD:
            return {
                "simulation_dir": sim_dir,
                "scripts_dir": scripts_dir,
                "config_file": config_path,
                "commands": {
                    "world": f"python {scripts_dir}/run_world_simulation.py --config {config_path}",
                },
                "instructions": (
                    f"1. 激活conda环境: conda activate MiroFish\n"
                    f"2. 运行 world 模拟:\n"
                    f"   - python {scripts_dir}/run_world_simulation.py --config {config_path}"
                ),
            }
        
        return {
            "simulation_dir": sim_dir,
            "scripts_dir": scripts_dir,
            "config_file": config_path,
            "commands": {
                "twitter": f"python {scripts_dir}/run_twitter_simulation.py --config {config_path}",
                "reddit": f"python {scripts_dir}/run_reddit_simulation.py --config {config_path}",
                "parallel": f"python {scripts_dir}/run_parallel_simulation.py --config {config_path}",
            },
            "instructions": (
                f"1. 激活conda环境: conda activate MiroFish\n"
                f"2. 运行模拟 (脚本位于 {scripts_dir}):\n"
                f"   - 单独运行Twitter: python {scripts_dir}/run_twitter_simulation.py --config {config_path}\n"
                f"   - 单独运行Reddit: python {scripts_dir}/run_reddit_simulation.py --config {config_path}\n"
                f"   - 并行运行双平台: python {scripts_dir}/run_parallel_simulation.py --config {config_path}"
            )
        }
