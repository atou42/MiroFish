"""
LLM客户端封装
统一使用OpenAI格式调用
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import httpx
from openai import OpenAI

from ..config import Config


class InvalidJSONResponseError(ValueError):
    """Raised when a model returns non-JSON content for a JSON request."""

    def __init__(
        self,
        message: str,
        raw_response: str = "",
        repaired_response: str = "",
    ):
        super().__init__(message)
        self.raw_response = raw_response
        self.repaired_response = repaired_response


@dataclass
class LLMSettings:
    api_key: Optional[str]
    base_url: Optional[str]
    model_name: Optional[str]
    speed_mode: Optional[str] = None
    reasoning_effort: Optional[str] = None
    verbosity: Optional[str] = None
    service_tier: Optional[str] = None
    provider_id: Optional[str] = None
    profile_id: Optional[str] = None
    route: Optional[str] = None
    selector: Optional[str] = None
    namespace: str = "default"

    def to_client_kwargs(self) -> Dict[str, Optional[str]]:
        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model": self.model_name,
            "speed_mode": self.speed_mode,
            "reasoning_effort": self.reasoning_effort,
            "verbosity": self.verbosity,
            "service_tier": self.service_tier,
            "provider_id": self.provider_id,
            "profile_id": self.profile_id,
            "route": self.route,
            "selector": self.selector,
        }


def resolve_llm_settings(namespace: Optional[str] = None, selector: Optional[str] = None) -> LLMSettings:
    config = Config.get_llm_config(namespace=namespace, selector=selector)
    return LLMSettings(
        api_key=config.get("api_key"),
        base_url=config.get("base_url"),
        model_name=config.get("model_name"),
        speed_mode=config.get("speed_mode"),
        reasoning_effort=config.get("reasoning_effort"),
        verbosity=config.get("verbosity"),
        service_tier=config.get("service_tier"),
        provider_id=config.get("provider_id"),
        profile_id=config.get("profile_id"),
        route=config.get("route"),
        selector=config.get("selector"),
        namespace=namespace or "default",
    )


class LLMClient:
    """LLM客户端"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        speed_mode: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        verbosity: Optional[str] = None,
        service_tier: Optional[str] = None,
        provider_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        route: Optional[str] = None,
        selector: Optional[str] = None,
        trust_env_proxy: Optional[bool] = None,
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME
        self.speed_mode = self._normalize_choice(speed_mode, Config.SUPPORTED_LLM_SPEED_MODES)
        self.reasoning_effort = self._normalize_choice(reasoning_effort, Config.SUPPORTED_REASONING_EFFORTS)
        self.verbosity = self._normalize_choice(verbosity, Config.SUPPORTED_VERBOSITY_LEVELS)
        self.service_tier = self._normalize_choice(service_tier, Config.SUPPORTED_SERVICE_TIERS)
        self.provider_id = provider_id
        self.profile_id = profile_id
        self.route = route
        self.selector = selector or route or profile_id
        self.trust_env_proxy = self._resolve_trust_env_proxy(trust_env_proxy)

        if not self.api_key:
            identity = self.selector or self.profile_id or self.route or self.model or "default"
            raise ValueError(f"LLM API Key 未配置: {identity}")

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=httpx.Client(trust_env=self.trust_env_proxy),
        )

    @classmethod
    def from_env_prefix(cls, prefix: Optional[str] = None, fallback: bool = True) -> "LLMClient":
        """根据环境变量前缀创建客户端，如 WORLD_AGENT_LLM / WORLD_RESOLVER_LLM。"""
        settings = Config.get_llm_settings(prefix=prefix, fallback=fallback)
        return cls(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model_name,
            speed_mode=settings.speed_mode,
            reasoning_effort=settings.reasoning_effort,
            verbosity=settings.verbosity,
            service_tier=settings.service_tier,
            provider_id=settings.provider_id,
            profile_id=settings.profile_id,
            route=settings.route,
            selector=settings.selector,
        )

    @classmethod
    def from_namespace(cls, namespace: Optional[str] = None) -> "LLMClient":
        settings = resolve_llm_settings(namespace=namespace)
        return cls(**settings.to_client_kwargs())

    @classmethod
    def from_selector(cls, selector: str) -> "LLMClient":
        settings = resolve_llm_settings(selector=selector)
        return cls(**settings.to_client_kwargs())

    @staticmethod
    def _normalize_choice(value: Optional[str], allowed: set[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if not normalized:
            return None
        return normalized if normalized in allowed else None

    @staticmethod
    def _resolve_trust_env_proxy(value: Optional[bool]) -> bool:
        if value is not None:
            return bool(value)
        raw = str(
            os.environ.get("LLM_TRUST_ENV_PROXY")
            or os.environ.get("OPENAI_TRUST_ENV_PROXY")
            or ""
        ).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _is_gpt5_family(self) -> bool:
        return str(self.model or "").strip().lower().startswith("gpt-5")

    def _effective_reasoning_effort(self) -> Optional[str]:
        if self.reasoning_effort:
            return self.reasoning_effort
        if self.speed_mode == "fast":
            return "low"
        if self.speed_mode == "balanced":
            return "medium"
        if self.speed_mode == "deep":
            return "high"
        return None

    def _effective_verbosity(self) -> Optional[str]:
        if self.verbosity:
            return self.verbosity
        if self.speed_mode == "fast":
            return "low"
        if self.speed_mode == "balanced":
            return "medium"
        if self.speed_mode == "deep":
            return "high"
        return None

    def _build_chat_kwargs(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        response_format: Optional[Dict] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if not self._is_gpt5_family():
            kwargs["temperature"] = temperature

        if response_format:
            kwargs["response_format"] = response_format
        if timeout is not None:
            kwargs["timeout"] = timeout

        # GPT-5 family supports reasoning controls; keep them off for generic compatible providers
        # unless the selected model is clearly in the GPT-5 family.
        if self._is_gpt5_family():
            reasoning_effort = self._effective_reasoning_effort()
            verbosity = self._effective_verbosity()
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
            if verbosity:
                kwargs["verbosity"] = verbosity
            if self.service_tier:
                kwargs["service_tier"] = self.service_tier

        return kwargs
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """
        发送聊天请求
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            response_format: 响应格式（如JSON模式）
            
        Returns:
            模型响应文本
        """
        kwargs = self._build_chat_kwargs(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            timeout=timeout,
        )
        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        # 部分模型（如MiniMax M2.5）会在content中包含<think>思考内容，需要移除
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content

    @staticmethod
    def _clean_json_text(response: str) -> str:
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        return cleaned_response.strip()

    @classmethod
    def _extract_json_candidate(cls, response: str) -> Optional[str]:
        cleaned_response = cls._clean_json_text(response)
        if not cleaned_response:
            return None

        if cleaned_response.startswith("{") and cleaned_response.endswith("}"):
            return cleaned_response

        decoder = json.JSONDecoder()
        first_brace = cleaned_response.find("{")
        if first_brace == 0:
            try:
                payload, end = decoder.raw_decode(cleaned_response)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(payload, dict):
                    return cleaned_response[:end]
            # If the response already starts like a JSON object but is truncated,
            # do not scan for nested inner objects such as `state_impacts`.
            # Returning the full top-level candidate lets the caller repair it
            # instead of silently parsing the wrong subtree.
            return cleaned_response

        if first_brace > 0:
            candidate = cleaned_response[first_brace:]
            try:
                payload, end = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                return candidate
            if isinstance(payload, dict):
                return candidate[:end]

        for idx, char in enumerate(cleaned_response):
            if char != "{":
                continue
            try:
                payload, end = decoder.raw_decode(cleaned_response[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return cleaned_response[idx:idx + end]

        return None

    def _repair_json_response(
        self,
        raw_response: str,
        max_tokens: int,
        timeout: Optional[float] = None,
    ) -> str:
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "你是一个 JSON 修复器。"
                    "把用户提供的文本重写为一个合法的 JSON 对象。"
                    "只输出 JSON，不要标题、不要 markdown、不要解释。"
                ),
            },
            {
                "role": "user",
                "content": raw_response,
            },
        ]

        return self.chat(
            messages=repair_messages,
            temperature=0.0,
            max_tokens=max(128, min(max_tokens, 1024)),
            response_format={"type": "json_object"},
            timeout=timeout,
        )
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.chat_json_with_meta(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )["data"]

    def chat_json_with_meta(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        发送聊天请求并返回 JSON 以及解析元信息。
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            解析后的JSON对象
        """
        raw_response = self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        cleaned_response = self._clean_json_text(raw_response)
        candidate = self._extract_json_candidate(raw_response) or cleaned_response

        try:
            return {
                "data": json.loads(candidate),
                "raw_response": cleaned_response,
                "json_candidate": candidate,
                "repair_used": False,
                "repaired_response": "",
                "repaired_candidate": "",
            }
        except json.JSONDecodeError:
            repaired_response = self._repair_json_response(
                raw_response=raw_response,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            repaired_candidate = (
                self._extract_json_candidate(repaired_response)
                or self._clean_json_text(repaired_response)
            )
            try:
                return {
                    "data": json.loads(repaired_candidate),
                    "raw_response": cleaned_response,
                    "json_candidate": candidate,
                    "repair_used": True,
                    "repaired_response": self._clean_json_text(repaired_response),
                    "repaired_candidate": repaired_candidate,
                }
            except json.JSONDecodeError:
                raise InvalidJSONResponseError(
                    "LLM返回的JSON格式无效，且修复失败: "
                    f"raw={cleaned_response[:1200]} | "
                    f"repaired={repaired_candidate[:1200]}",
                    raw_response=cleaned_response,
                    repaired_response=repaired_candidate,
                )

    def health_check(self, timeout: float = 15.0) -> bool:
        """
        对当前 provider + model 做一次轻量健康检查。
        成功返回 True，失败抛异常，由调用方决定等待/暂停/失败策略。
        """
        self.chat(
            messages=[
                {"role": "system", "content": "Respond with OK."},
                {"role": "user", "content": "OK"},
            ],
            temperature=0.0,
            max_tokens=8,
            timeout=timeout,
        )
        return True
