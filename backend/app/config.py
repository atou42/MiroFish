"""
配置管理
统一从项目根目录的 .env 文件加载配置
"""

import os
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))

# 加载项目根目录的 .env 文件
# 路径: MiroFish/.env (相对于 backend/app/config.py)
project_root_env = os.path.join(PROJECT_ROOT, '.env')

if os.path.exists(project_root_env):
    load_dotenv(project_root_env, override=True)
else:
    # 如果根目录没有 .env，尝试加载环境变量（用于生产环境）
    load_dotenv(override=True)


class Config:
    """Flask配置类"""

    SUPPORTED_LLM_SPEED_MODES = {"fast", "balanced", "deep"}
    SUPPORTED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
    SUPPORTED_VERBOSITY_LEVELS = {"low", "medium", "high"}
    SUPPORTED_SERVICE_TIERS = {"auto", "default", "flex", "scale", "priority"}
    
    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mirofish-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'
    
    # JSON配置 - 禁用ASCII转义，让中文直接显示（而不是 \uXXXX 格式）
    JSON_AS_ASCII = False
    
    # LLM配置（统一使用OpenAI格式）
    LLM_REGISTRY_PATH = os.environ.get('LLM_REGISTRY_PATH', os.path.join(PROJECT_ROOT, 'llm_registry.json'))
    LLM_REGISTRY_SOURCE = os.environ.get('LLM_REGISTRY_SOURCE', 'auto')
    OPENCLAW_CONFIG_PATH = os.environ.get('OPENCLAW_CONFIG_PATH', os.path.expanduser('~/.openclaw/openclaw.json'))
    LLM_API_KEY = os.environ.get('LLM_API_KEY')
    LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
    LLM_MODEL_NAME = os.environ.get('LLM_MODEL_NAME', 'gpt-4o-mini')
    LLM_SPEED_MODE = os.environ.get('LLM_SPEED_MODE', '')
    LLM_REASONING_EFFORT = os.environ.get('LLM_REASONING_EFFORT', '')
    LLM_VERBOSITY = os.environ.get('LLM_VERBOSITY', '')
    LLM_SERVICE_TIER = os.environ.get('LLM_SERVICE_TIER', '')
    WORLD_AGENT_LLM_API_KEY = os.environ.get('WORLD_AGENT_LLM_API_KEY') or LLM_API_KEY
    WORLD_AGENT_LLM_BASE_URL = os.environ.get('WORLD_AGENT_LLM_BASE_URL') or LLM_BASE_URL
    WORLD_AGENT_LLM_MODEL_NAME = os.environ.get('WORLD_AGENT_LLM_MODEL_NAME') or LLM_MODEL_NAME
    WORLD_AGENT_LLM_SPEED_MODE = os.environ.get('WORLD_AGENT_LLM_SPEED_MODE') or LLM_SPEED_MODE
    WORLD_AGENT_LLM_REASONING_EFFORT = os.environ.get('WORLD_AGENT_LLM_REASONING_EFFORT') or LLM_REASONING_EFFORT
    WORLD_AGENT_LLM_VERBOSITY = os.environ.get('WORLD_AGENT_LLM_VERBOSITY') or LLM_VERBOSITY
    WORLD_AGENT_LLM_SERVICE_TIER = os.environ.get('WORLD_AGENT_LLM_SERVICE_TIER') or LLM_SERVICE_TIER
    WORLD_RESOLVER_LLM_API_KEY = os.environ.get('WORLD_RESOLVER_LLM_API_KEY') or LLM_API_KEY
    WORLD_RESOLVER_LLM_BASE_URL = os.environ.get('WORLD_RESOLVER_LLM_BASE_URL') or LLM_BASE_URL
    WORLD_RESOLVER_LLM_MODEL_NAME = os.environ.get('WORLD_RESOLVER_LLM_MODEL_NAME') or LLM_MODEL_NAME
    WORLD_RESOLVER_LLM_SPEED_MODE = os.environ.get('WORLD_RESOLVER_LLM_SPEED_MODE') or LLM_SPEED_MODE
    WORLD_RESOLVER_LLM_REASONING_EFFORT = os.environ.get('WORLD_RESOLVER_LLM_REASONING_EFFORT') or LLM_REASONING_EFFORT
    WORLD_RESOLVER_LLM_VERBOSITY = os.environ.get('WORLD_RESOLVER_LLM_VERBOSITY') or LLM_VERBOSITY
    WORLD_RESOLVER_LLM_SERVICE_TIER = os.environ.get('WORLD_RESOLVER_LLM_SERVICE_TIER') or LLM_SERVICE_TIER
    WORLD_INTENT_CONCURRENCY = int(os.environ.get('WORLD_INTENT_CONCURRENCY', '6'))
    WORLD_STRICT_MODEL_IDENTITY = os.environ.get('WORLD_STRICT_MODEL_IDENTITY', 'true').lower() == 'true'
    WORLD_ALLOW_SEMANTIC_FALLBACK = os.environ.get('WORLD_ALLOW_SEMANTIC_FALLBACK', 'false').lower() == 'true'
    WORLD_PROVIDER_PREFLIGHT_CHECK = os.environ.get('WORLD_PROVIDER_PREFLIGHT_CHECK', 'true').lower() == 'true'
    WORLD_PROVIDER_MAX_RETRIES = int(os.environ.get('WORLD_PROVIDER_MAX_RETRIES', '3'))
    WORLD_PROVIDER_BACKOFF_SECONDS = float(os.environ.get('WORLD_PROVIDER_BACKOFF_SECONDS', '5'))
    WORLD_PROVIDER_HEALTHCHECK_TIMEOUT = float(os.environ.get('WORLD_PROVIDER_HEALTHCHECK_TIMEOUT', '15'))
    WORLD_PROVIDER_REQUEST_TIMEOUT = float(
        os.environ.get(
            'WORLD_PROVIDER_REQUEST_TIMEOUT',
            os.environ.get('WORLD_PROVIDER_HEALTHCHECK_TIMEOUT', '15'),
        )
    )
    WORLD_RUN_ON_PROVIDER_DEGRADED = os.environ.get('WORLD_RUN_ON_PROVIDER_DEGRADED', 'wait')
    WORLD_RESOLVER_ON_FAILURE = os.environ.get('WORLD_RESOLVER_ON_FAILURE', 'pause')
    WORLD_ACTOR_ON_FAILURE = os.environ.get('WORLD_ACTOR_ON_FAILURE', 'defer')
    
    # Zep配置
    ZEP_API_KEY = os.environ.get('ZEP_API_KEY')
    
    # 文件上传配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}
    
    # 文本处理配置
    DEFAULT_CHUNK_SIZE = 500  # 默认切块大小
    DEFAULT_CHUNK_OVERLAP = 50  # 默认重叠大小
    
    # OASIS模拟配置
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
    OASIS_SIMULATION_DATA_DIR = os.path.join(os.path.dirname(__file__), '../uploads/simulations')
    
    # OASIS平台可用动作配置
    OASIS_TWITTER_ACTIONS = [
        'CREATE_POST', 'LIKE_POST', 'REPOST', 'FOLLOW', 'DO_NOTHING', 'QUOTE_POST'
    ]
    OASIS_REDDIT_ACTIONS = [
        'LIKE_POST', 'DISLIKE_POST', 'CREATE_POST', 'CREATE_COMMENT',
        'LIKE_COMMENT', 'DISLIKE_COMMENT', 'SEARCH_POSTS', 'SEARCH_USER',
        'TREND', 'REFRESH', 'DO_NOTHING', 'FOLLOW', 'MUTE'
    ]
    
    # Report Agent配置
    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get('REPORT_AGENT_MAX_TOOL_CALLS', '5'))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(os.environ.get('REPORT_AGENT_MAX_REFLECTION_ROUNDS', '2'))
    REPORT_AGENT_TEMPERATURE = float(os.environ.get('REPORT_AGENT_TEMPERATURE', '0.5'))

    @dataclass(frozen=True)
    class LLMSettings:
        api_key: Optional[str]
        base_url: str
        model_name: str
        speed_mode: Optional[str] = None
        reasoning_effort: Optional[str] = None
        verbosity: Optional[str] = None
        service_tier: Optional[str] = None
        provider_id: Optional[str] = None
        profile_id: Optional[str] = None
        route: Optional[str] = None
        selector: Optional[str] = None
        source: str = "default"

    _llm_registry_cache: Optional[Dict[str, Any]] = None
    _llm_registry_mtime: Optional[float] = None
    _openclaw_cache: Optional[Dict[str, Any]] = None
    _openclaw_mtime: Optional[float] = None

    OPENCLAW_COMPATIBLE_APIS = {
        "openai",
        "openai-chat",
        "openai-chat-completions",
        "openai-completions",
        "openai-responses",
    }

    @staticmethod
    def _parse_bool(value: Optional[str], default: bool) -> bool:
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _normalize_choice(value: Optional[str], allowed: set[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if not normalized:
            return None
        return normalized if normalized in allowed else None

    @staticmethod
    def _normalize_key(value: Optional[str]) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _current_registry_source(cls) -> str:
        source = cls._normalize_key(os.environ.get("LLM_REGISTRY_SOURCE", cls.LLM_REGISTRY_SOURCE))
        return source if source in {"auto", "local", "openclaw"} else "auto"

    @staticmethod
    def _resolve_env_template_value(raw_value: Any) -> Optional[str]:
        if raw_value in (None, ""):
            return None
        value = str(raw_value).strip()
        if not value:
            return None
        env_match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if env_match:
            return os.environ.get(env_match.group(1))
        return value

    @classmethod
    def _namespace_to_route(cls, namespace: Optional[str]) -> Optional[str]:
        normalized = cls._normalize_key(namespace)
        if normalized.endswith("_llm"):
            normalized = normalized[:-4]
        return normalized or None

    @classmethod
    def _load_llm_registry(cls) -> Dict[str, Any]:
        path = cls.LLM_REGISTRY_PATH
        if not path or not os.path.exists(path):
            cls._llm_registry_cache = None
            cls._llm_registry_mtime = None
            return {}

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {}

        if cls._llm_registry_cache is not None and cls._llm_registry_mtime == mtime:
            return cls._llm_registry_cache

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

        if not isinstance(data, dict):
            return {}

        cls._llm_registry_cache = data
        cls._llm_registry_mtime = mtime
        return data

    @classmethod
    def _load_openclaw_config(cls) -> Dict[str, Any]:
        path = os.environ.get("OPENCLAW_CONFIG_PATH", cls.OPENCLAW_CONFIG_PATH)
        if not path or not os.path.exists(path):
            cls._openclaw_cache = None
            cls._openclaw_mtime = None
            return {}

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {}

        if cls._openclaw_cache is not None and cls._openclaw_mtime == mtime:
            return cls._openclaw_cache

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

        if not isinstance(data, dict):
            return {}

        cls._openclaw_cache = data
        cls._openclaw_mtime = mtime
        return data

    @classmethod
    def _normalized_mapping(cls, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        normalized: Dict[str, Any] = {}
        for key, value in payload.items():
            normalized_key = cls._normalize_key(key)
            if normalized_key:
                normalized[normalized_key] = value
        return normalized

    @classmethod
    def _normalized_lookup(cls, payload: Any) -> Dict[str, Tuple[str, Any]]:
        if not isinstance(payload, dict):
            return {}
        normalized: Dict[str, Tuple[str, Any]] = {}
        for key, value in payload.items():
            normalized_key = cls._normalize_key(key)
            if normalized_key:
                normalized[normalized_key] = (str(key), value)
        return normalized

    @classmethod
    def _resolve_secret_value(cls, payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        raw_value = payload.get("api_key")
        resolved_value = cls._resolve_env_template_value(raw_value)
        if resolved_value is not None:
            return resolved_value
        env_name = payload.get("api_key_env")
        if env_name:
            return os.environ.get(str(env_name).strip())
        return None

    @classmethod
    def _resolve_literal_or_env(
        cls,
        payload: Dict[str, Any],
        value_field: str,
        env_field: str,
    ) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        raw_value = payload.get(value_field)
        resolved_value = cls._resolve_env_template_value(raw_value)
        if resolved_value is not None:
            return resolved_value
        env_name = payload.get(env_field)
        if env_name:
            return os.environ.get(str(env_name).strip())
        return None

    @classmethod
    def _extract_profile_id(cls, route_entry: Any) -> Optional[str]:
        if isinstance(route_entry, str):
            return cls._normalize_key(route_entry)
        if isinstance(route_entry, dict):
            return cls._normalize_key(route_entry.get("profile"))
        return None

    @classmethod
    def _extract_openclaw_selector(cls, payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(payload, dict):
            return None

        explicit_selector = payload.get("openclaw_selector")
        if explicit_selector:
            selector = str(explicit_selector).strip()
            if selector:
                return selector if selector.startswith("openclaw:") else f"openclaw:{selector}"

        model_ref = payload.get("openclaw_model") or payload.get("openclaw_model_id")
        if model_ref:
            return f"openclaw:{str(model_ref).strip()}"

        agent_ref = payload.get("openclaw_agent")
        if agent_ref:
            return f"openclaw:agent:{str(agent_ref).strip()}"

        alias_ref = payload.get("openclaw_alias")
        if alias_ref:
            return f"openclaw:alias:{str(alias_ref).strip()}"

        return None

    @classmethod
    def _extract_openclaw_model_reference(cls, model_entry: Any) -> Optional[str]:
        if isinstance(model_entry, str):
            model_ref = str(model_entry).strip()
            return model_ref or None
        if isinstance(model_entry, dict):
            for key in ("primary", "model", "id"):
                candidate = model_entry.get(key)
                if candidate:
                    model_ref = str(candidate).strip()
                    if model_ref:
                        return model_ref
        return None

    @classmethod
    def _resolve_openclaw_agent_model(cls, openclaw: Dict[str, Any], agent_id: str) -> Optional[str]:
        normalized_agent_id = cls._normalize_key(agent_id)
        agents = openclaw.get("agents", {}).get("list", [])
        if not isinstance(agents, list):
            return None

        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if cls._normalize_key(agent.get("id")) != normalized_agent_id:
                continue
            return cls._extract_openclaw_model_reference(agent.get("model"))
        return None

    @classmethod
    def _resolve_openclaw_alias_model(cls, openclaw: Dict[str, Any], alias: str) -> Optional[str]:
        models = openclaw.get("agents", {}).get("defaults", {}).get("models", {})
        if not isinstance(models, dict):
            return None

        normalized_alias = cls._normalize_key(alias)
        for model_ref, model_config in models.items():
            if not isinstance(model_config, dict):
                continue
            if cls._normalize_key(model_config.get("alias")) == normalized_alias:
                model_ref = str(model_ref).strip()
                return model_ref or None
        return None

    @classmethod
    def _resolve_openclaw_model_target(
        cls,
        openclaw: Dict[str, Any],
        selector: Optional[str],
        namespace: Optional[str],
        allow_implicit_selector: bool = False,
    ) -> Tuple[Optional[str], Optional[str]]:
        defaults = openclaw.get("agents", {}).get("defaults", {})
        default_model_ref = cls._extract_openclaw_model_reference(defaults.get("model"))
        route_name = cls._namespace_to_route(namespace)

        raw_selector = str(selector or "").strip()
        direct_selector = raw_selector
        if raw_selector.startswith("openclaw:"):
            direct_selector = raw_selector.split(":", 1)[1].strip()
            allow_implicit_selector = True

        if direct_selector and allow_implicit_selector:
            normalized_direct = cls._normalize_key(direct_selector)
            if normalized_direct in {"", "default"}:
                return default_model_ref, "default"
            if normalized_direct.startswith("agent:"):
                agent_id = direct_selector.split(":", 1)[1].strip()
                return cls._resolve_openclaw_agent_model(openclaw, agent_id), cls._normalize_key(agent_id) or route_name
            if normalized_direct.startswith("alias:"):
                alias = direct_selector.split(":", 1)[1].strip()
                return cls._resolve_openclaw_alias_model(openclaw, alias), cls._normalize_key(alias) or route_name
            if direct_selector.startswith("@"):
                agent_id = direct_selector[1:].strip()
                return cls._resolve_openclaw_agent_model(openclaw, agent_id), cls._normalize_key(agent_id) or route_name
            if "/" in direct_selector:
                return direct_selector, route_name

            agent_model_ref = cls._resolve_openclaw_agent_model(openclaw, direct_selector)
            if agent_model_ref:
                return agent_model_ref, cls._normalize_key(direct_selector)

            alias_model_ref = cls._resolve_openclaw_alias_model(openclaw, direct_selector)
            if alias_model_ref:
                return alias_model_ref, cls._normalize_key(direct_selector)

        if route_name:
            agent_model_ref = cls._resolve_openclaw_agent_model(openclaw, route_name)
            if agent_model_ref:
                return agent_model_ref, route_name

        return default_model_ref, route_name or "default"

    @classmethod
    def _resolve_openclaw_config(
        cls,
        namespace: Optional[str] = None,
        selector: Optional[str] = None,
        allow_implicit_selector: bool = False,
    ) -> Optional[Dict[str, Any]]:
        openclaw = cls._load_openclaw_config()
        if not openclaw:
            return None

        model_ref, resolved_route = cls._resolve_openclaw_model_target(
            openclaw,
            selector=selector,
            namespace=namespace,
            allow_implicit_selector=allow_implicit_selector,
        )
        if not model_ref:
            return None

        provider_id, separator, model_name = str(model_ref).partition("/")
        if not separator or not model_name:
            return None

        providers_lookup = cls._normalized_lookup(openclaw.get("models", {}).get("providers"))
        provider_entry = providers_lookup.get(cls._normalize_key(provider_id))
        if not provider_entry:
            return None

        provider_key, provider = provider_entry
        provider_api = cls._normalize_key(provider.get("api"))
        if provider_api and provider_api not in cls.OPENCLAW_COMPATIBLE_APIS:
            raise ValueError(
                f"OpenClaw provider '{provider_key}' 使用 '{provider.get('api')}' 协议，"
                "当前 MiroFish 仅支持复用 OpenAI 兼容 provider"
            )

        return {
            "api_key": cls._resolve_env_template_value(provider.get("apiKey")),
            "base_url": cls._resolve_env_template_value(provider.get("baseUrl")) or cls.LLM_BASE_URL,
            "model_name": model_name,
            "speed_mode": None,
            "reasoning_effort": None,
            "verbosity": None,
            "service_tier": None,
            "provider_id": provider_key,
            "profile_id": None,
            "route": resolved_route,
            "selector": selector or f"openclaw:{model_ref}",
            "source": "openclaw",
        }

    @classmethod
    def _resolve_registry_config(
        cls,
        namespace: Optional[str] = None,
        selector: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        registry = cls._load_llm_registry()
        if not registry:
            return None

        providers = cls._normalized_mapping(registry.get("providers"))
        profiles = cls._normalized_mapping(registry.get("profiles"))
        routes = cls._normalized_mapping(registry.get("routes"))

        normalized_selector = cls._normalize_key(selector)
        route_name = None
        profile_id = None

        if normalized_selector:
            if normalized_selector in profiles:
                profile_id = normalized_selector
            elif normalized_selector in routes:
                route_name = normalized_selector
                profile_id = cls._extract_profile_id(routes.get(route_name))

        if not profile_id:
            route_name = cls._namespace_to_route(namespace)
            if route_name and route_name in routes:
                profile_id = cls._extract_profile_id(routes.get(route_name))

        if not profile_id and not namespace and not normalized_selector and "default" in routes:
            route_name = "default"
            profile_id = cls._extract_profile_id(routes.get(route_name))

        if not profile_id:
            return None

        profile = profiles.get(profile_id)
        if not isinstance(profile, dict):
            return None

        openclaw_selector = cls._extract_openclaw_selector(profile)
        provider_id = cls._normalize_key(profile.get("provider"))
        provider = providers.get(provider_id, {}) if provider_id else {}
        openclaw_config = None
        if openclaw_selector:
            openclaw_config = cls._resolve_openclaw_config(
                namespace=namespace,
                selector=openclaw_selector,
                allow_implicit_selector=True,
            )

        api_key = (
            cls._resolve_secret_value(profile)
            or cls._resolve_secret_value(provider)
            or (openclaw_config or {}).get("api_key")
        )
        base_url = (
            cls._resolve_literal_or_env(profile, "base_url", "base_url_env")
            or cls._resolve_literal_or_env(provider, "base_url", "base_url_env")
            or (openclaw_config or {}).get("base_url")
            or cls.LLM_BASE_URL
        )
        model_name = (
            cls._resolve_literal_or_env(profile, "model_name", "model_name_env")
            or cls._resolve_literal_or_env(profile, "model", "model_env")
            or cls._resolve_literal_or_env(provider, "model_name", "model_name_env")
            or (openclaw_config or {}).get("model_name")
            or cls.LLM_MODEL_NAME
        )

        return {
            "api_key": api_key,
            "base_url": base_url,
            "model_name": model_name,
            "speed_mode": cls._normalize_choice(
                profile.get("speed_mode") or provider.get("speed_mode"),
                cls.SUPPORTED_LLM_SPEED_MODES,
            ),
            "reasoning_effort": cls._normalize_choice(
                profile.get("reasoning_effort") or provider.get("reasoning_effort"),
                cls.SUPPORTED_REASONING_EFFORTS,
            ),
            "verbosity": cls._normalize_choice(
                profile.get("verbosity") or provider.get("verbosity"),
                cls.SUPPORTED_VERBOSITY_LEVELS,
            ),
            "service_tier": cls._normalize_choice(
                profile.get("service_tier") or provider.get("service_tier"),
                cls.SUPPORTED_SERVICE_TIERS,
            ),
            "provider_id": (openclaw_config or {}).get("provider_id") or provider_id or None,
            "profile_id": profile_id,
            "route": route_name,
            "selector": normalized_selector or route_name or profile_id,
            "source": "registry+openclaw" if openclaw_config else "registry",
        }

    @classmethod
    def get_agent_llm_selector(
        cls,
        simulation_mode: str,
        entity_type: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[str]:
        registry = cls._load_llm_registry()
        if not registry:
            return None

        selectors = cls._normalized_mapping(registry.get("agent_selectors"))
        mode_config = selectors.get(cls._normalize_key(simulation_mode))
        if not isinstance(mode_config, dict):
            return None

        by_name = cls._normalized_mapping(mode_config.get("by_agent_name"))
        by_entity_type = cls._normalized_mapping(mode_config.get("by_entity_type"))

        normalized_agent_name = cls._normalize_key(agent_name)
        if normalized_agent_name and normalized_agent_name in by_name:
            return str(by_name[normalized_agent_name]).strip() or None

        normalized_entity_type = cls._normalize_key(entity_type)
        if normalized_entity_type and normalized_entity_type in by_entity_type:
            return str(by_entity_type[normalized_entity_type]).strip() or None

        default_selector = mode_config.get("default")
        if default_selector:
            return str(default_selector).strip() or None
        return None

    @classmethod
    def get_llm_settings(
        cls,
        prefix: Optional[str] = None,
        fallback: bool = True,
        selector: Optional[str] = None,
    ) -> "Config.LLMSettings":
        """解析指定命名空间的模型配置，支持 WORLD_AGENT / WORLD_RESOLVER。"""
        normalized = (prefix or "").strip().upper().rstrip("_")
        if normalized.endswith("_LLM"):
            normalized = normalized[:-4]

        config = cls.get_llm_config(namespace=normalized or None, selector=selector)
        api_key = config.get("api_key")
        if not fallback and normalized and not str(config.get("source") or "").startswith("registry"):
            env_prefix = normalized.upper()
            api_key = os.environ.get(f"{env_prefix}_LLM_API_KEY")
            config["base_url"] = os.environ.get(f"{env_prefix}_LLM_BASE_URL")
            config["model_name"] = os.environ.get(f"{env_prefix}_LLM_MODEL_NAME")
            config["speed_mode"] = os.environ.get(f"{env_prefix}_LLM_SPEED_MODE")
            config["reasoning_effort"] = os.environ.get(f"{env_prefix}_LLM_REASONING_EFFORT")
            config["verbosity"] = os.environ.get(f"{env_prefix}_LLM_VERBOSITY")
            config["service_tier"] = os.environ.get(f"{env_prefix}_LLM_SERVICE_TIER")

        return cls.LLMSettings(
            api_key=api_key,
            base_url=config.get("base_url") or cls.LLM_BASE_URL,
            model_name=config.get("model_name") or cls.LLM_MODEL_NAME,
            speed_mode=config.get("speed_mode"),
            reasoning_effort=config.get("reasoning_effort"),
            verbosity=config.get("verbosity"),
            service_tier=config.get("service_tier"),
            provider_id=config.get("provider_id"),
            profile_id=config.get("profile_id"),
            route=config.get("route"),
            selector=config.get("selector"),
            source=config.get("source") or (normalized.lower() if normalized else "default"),
        )

    @classmethod
    def get_llm_config(cls, namespace: str = None, selector: Optional[str] = None):
        """
        读取指定命名空间的 LLM 配置。

        `namespace=None` 时返回通用 `LLM_*`。
        `namespace="WORLD_AGENT"` 时优先读取 `WORLD_AGENT_LLM_*`，缺省回退到通用配置。
        """
        explicit_openclaw_selector = str(selector or "").strip().startswith("openclaw:")
        if explicit_openclaw_selector:
            openclaw_config = cls._resolve_openclaw_config(
                namespace=namespace,
                selector=selector,
                allow_implicit_selector=True,
            )
            if openclaw_config:
                return openclaw_config

        registry_config = cls._resolve_registry_config(namespace=namespace, selector=selector)
        if registry_config:
            return registry_config

        if cls._current_registry_source() in {"auto", "openclaw"}:
            openclaw_config = cls._resolve_openclaw_config(
                namespace=namespace,
                selector=selector,
                allow_implicit_selector=cls._current_registry_source() == "openclaw",
            )
            if openclaw_config:
                return openclaw_config

        if selector and not namespace:
            namespace = str(selector).strip().upper()

        if not namespace:
            return {
                "api_key": cls.LLM_API_KEY,
                "base_url": cls.LLM_BASE_URL,
                "model_name": cls.LLM_MODEL_NAME,
                "speed_mode": cls._normalize_choice(cls.LLM_SPEED_MODE, cls.SUPPORTED_LLM_SPEED_MODES),
                "reasoning_effort": cls._normalize_choice(
                    cls.LLM_REASONING_EFFORT,
                    cls.SUPPORTED_REASONING_EFFORTS,
                ),
                "verbosity": cls._normalize_choice(cls.LLM_VERBOSITY, cls.SUPPORTED_VERBOSITY_LEVELS),
                "service_tier": cls._normalize_choice(cls.LLM_SERVICE_TIER, cls.SUPPORTED_SERVICE_TIERS),
                "provider_id": None,
                "profile_id": None,
                "route": cls._namespace_to_route(namespace) if namespace else "default",
                "selector": cls._namespace_to_route(namespace) if namespace else "default",
                "source": "env",
            }

        prefix = namespace.upper().strip('_')
        api_key = os.environ.get(f'{prefix}_LLM_API_KEY') or cls.LLM_API_KEY
        base_url = os.environ.get(f'{prefix}_LLM_BASE_URL') or cls.LLM_BASE_URL
        model_name = os.environ.get(f'{prefix}_LLM_MODEL_NAME') or cls.LLM_MODEL_NAME
        speed_mode = os.environ.get(f'{prefix}_LLM_SPEED_MODE') or cls.LLM_SPEED_MODE
        reasoning_effort = os.environ.get(f'{prefix}_LLM_REASONING_EFFORT') or cls.LLM_REASONING_EFFORT
        verbosity = os.environ.get(f'{prefix}_LLM_VERBOSITY') or cls.LLM_VERBOSITY
        service_tier = os.environ.get(f'{prefix}_LLM_SERVICE_TIER') or cls.LLM_SERVICE_TIER
        return {
            "api_key": api_key,
            "base_url": base_url,
            "model_name": model_name,
            "speed_mode": cls._normalize_choice(speed_mode, cls.SUPPORTED_LLM_SPEED_MODES),
            "reasoning_effort": cls._normalize_choice(reasoning_effort, cls.SUPPORTED_REASONING_EFFORTS),
            "verbosity": cls._normalize_choice(verbosity, cls.SUPPORTED_VERBOSITY_LEVELS),
            "service_tier": cls._normalize_choice(service_tier, cls.SUPPORTED_SERVICE_TIERS),
            "provider_id": None,
            "profile_id": None,
            "route": cls._namespace_to_route(namespace),
            "selector": cls._namespace_to_route(namespace),
            "source": "env",
        }

    @classmethod
    def get_world_agent_llm_config(cls):
        return cls.get_llm_config("WORLD_AGENT")

    @classmethod
    def get_world_resolver_llm_config(cls):
        return cls.get_llm_config("WORLD_RESOLVER")

    @classmethod
    def get_world_runtime_policy(cls) -> dict:
        return {
            "strict_model_identity": cls._parse_bool(
                os.environ.get("WORLD_STRICT_MODEL_IDENTITY"),
                cls.WORLD_STRICT_MODEL_IDENTITY,
            ),
            "allow_semantic_fallback": cls._parse_bool(
                os.environ.get("WORLD_ALLOW_SEMANTIC_FALLBACK"),
                cls.WORLD_ALLOW_SEMANTIC_FALLBACK,
            ),
            "provider_preflight_check": cls._parse_bool(
                os.environ.get("WORLD_PROVIDER_PREFLIGHT_CHECK"),
                cls.WORLD_PROVIDER_PREFLIGHT_CHECK,
            ),
            "provider_max_retries": int(
                os.environ.get("WORLD_PROVIDER_MAX_RETRIES", cls.WORLD_PROVIDER_MAX_RETRIES)
            ),
            "provider_backoff_seconds": float(
                os.environ.get("WORLD_PROVIDER_BACKOFF_SECONDS", cls.WORLD_PROVIDER_BACKOFF_SECONDS)
            ),
            "provider_healthcheck_timeout": float(
                os.environ.get("WORLD_PROVIDER_HEALTHCHECK_TIMEOUT", cls.WORLD_PROVIDER_HEALTHCHECK_TIMEOUT)
            ),
            "provider_request_timeout": float(
                os.environ.get("WORLD_PROVIDER_REQUEST_TIMEOUT", cls.WORLD_PROVIDER_REQUEST_TIMEOUT)
            ),
            "run_on_provider_degraded": os.environ.get(
                "WORLD_RUN_ON_PROVIDER_DEGRADED",
                cls.WORLD_RUN_ON_PROVIDER_DEGRADED,
            ).strip().lower(),
            "resolver_on_failure": os.environ.get(
                "WORLD_RESOLVER_ON_FAILURE",
                cls.WORLD_RESOLVER_ON_FAILURE,
            ).strip().lower(),
            "actor_on_failure": os.environ.get(
                "WORLD_ACTOR_ON_FAILURE",
                cls.WORLD_ACTOR_ON_FAILURE,
            ).strip().lower(),
            "stop_mode": os.environ.get(
                "WORLD_STOP_MODE",
                "hard_cap",
            ).strip().lower(),
            "max_drain_rounds": int(
                os.environ.get("WORLD_MAX_DRAIN_ROUNDS", 0)
            ),
        }
    
    @classmethod
    def validate(cls):
        """验证必要配置"""
        errors = []
        default_config = cls.get_llm_config()
        world_agent_config = cls.get_llm_config("WORLD_AGENT")
        world_resolver_config = cls.get_llm_config("WORLD_RESOLVER")
        if not any([
            default_config.get("api_key"),
            world_agent_config.get("api_key"),
            world_resolver_config.get("api_key"),
        ]):
            errors.append("未找到可用 LLM 配置（OpenClaw / llm_registry.json / .env）")
        if not cls.ZEP_API_KEY:
            errors.append("ZEP_API_KEY 未配置")
        return errors
