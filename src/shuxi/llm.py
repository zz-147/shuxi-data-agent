"""LLM Provider 抽象：演示用 Mock，生产用通义千问（DashScope OpenAI 兼容模式）."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .config import Settings


class ProviderError(RuntimeError):
    """LLM 调用失败（网络、鉴权、限流等）."""


@dataclass
class LLMResponse:
    content: str
    model: str = ""
    usage: dict = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(self, messages: list[dict], *, temperature: float = 0.2) -> LLMResponse:
        ...


class MockProvider:
    """按剧本顺序返回响应，用于离线演示与单测；剧本耗尽时返回合法的 final_answer."""

    name = "mock"

    def __init__(self, script: list[str] | None = None):
        self.script: list[str] = list(script or [])
        self.calls: list[list[dict]] = []

    def push(self, content: str) -> None:
        self.script.append(content)

    def complete(self, messages: list[dict], *, temperature: float = 0.2) -> LLMResponse:
        self.calls.append(messages)
        if self.script:
            return LLMResponse(content=self.script.pop(0), model=self.name)
        return LLMResponse(
            content=json.dumps(
                {
                    "thought": "mock 剧本已结束，直接给出结论",
                    "final_answer": "演示剧本已执行完毕。这里本应是对工具结果的总结；"
                    "真实模式会由通义千问基于工具观察生成结论。",
                },
                ensure_ascii=False,
            ),
            model=self.name,
        )

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> "MockProvider":
        with open(path, "r", encoding="utf-8") as handle:
            script = json.load(handle)
        if not isinstance(script, list) or not all(isinstance(item, str) for item in script):
            raise ValueError(f"Mock 剧本必须是字符串数组: {path}")
        return cls(script)


class OpenAICompatibleProvider:
    """OpenAI 兼容接口（DashScope compatible-mode 走的就是这套协议）."""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
    ):
        if not api_key:
            raise ProviderError(
                "缺少 API Key：请设置 DASHSCOPE_API_KEY，或使用 --mock 演示模式。"
            )
        try:
            from openai import OpenAI  # 延迟导入，让 --mock 模式零网络零依赖
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise ProviderError("未安装 openai SDK：pip install -r requirements.txt") from exc
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def complete(self, messages: list[dict], *, temperature: float = 0.2) -> LLMResponse:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装为 ProviderError
            raise ProviderError(f"LLM 调用失败: {type(exc).__name__}: {exc}") from exc

        choices = getattr(response, "choices", None) or []
        content = ""
        if choices:
            content = choices[0].message.content or ""
        usage = {}
        if getattr(response, "usage", None) is not None:
            try:
                usage = response.usage.model_dump()
            except AttributeError:
                usage = dict(response.usage)  # 兼容旧版 SDK 返回对象
        return LLMResponse(content=content, model=getattr(response, "model", self.model), usage=usage)


class DashScopeProvider(OpenAICompatibleProvider):
    """通义千问（DashScope）."""

    name = "dashscope"


def build_provider(settings: Settings, script: list[str] | None = None) -> LLMProvider:
    """按配置构造 provider。mock 模式不要求 API Key。"""
    provider = settings.provider.lower()
    if provider == "mock":
        return MockProvider(script)
    if provider in {"dashscope", "qwen", "tongyi"}:
        return DashScopeProvider(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            timeout=settings.request_timeout,
        )
    raise ProviderError(f"未知 provider: {settings.provider}（可选 mock / dashscope）")
