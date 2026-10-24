"""工具层基础设施：ToolSpec / ToolContext / 注册表 / 统一执行入口。

设计要点：
- 工具是「纯函数 + 显式上下文」，不依赖全局单例，便于单测与嵌入式复用；
- 注册表顺序固定（inspect_schema → query_data → compute_metrics →
  detect_anomaly_drivers → generate_chart → export_report），保证 prompt / CLI / UI 展示稳定；
- ``execute_tool`` 负责兜底异常，任何 handler 抛错都转成 ``{"ok": False, "error": ...}``，
  保证 agent 循环不会因单个工具异常而崩溃。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

TOOL_ORDER = (
    "inspect_schema",
    "query_data",
    "compute_metrics",
    "detect_anomaly_drivers",
    "generate_chart",
    "export_report",
)


@dataclass(frozen=True)
class ToolSpec:
    """一个可被 LLM 调用的工具定义."""

    name: str
    description: str
    parameters: dict
    risk: str
    handler: Callable[[dict, "ToolContext"], dict]


@dataclass
class ToolContext:
    """调用工具时显式传入的运行时上下文（不使用全局状态）."""

    store: Any
    settings: Any
    trace: Any
    artifacts_dir: Path


_REGISTRY: Dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    """注册工具（同名覆盖，便于测试替换）."""
    if not isinstance(spec, ToolSpec):
        raise TypeError(f"register 需要 ToolSpec，实际为 {type(spec).__name__}")
    if not callable(spec.handler):
        raise TypeError(f"工具 {spec.name} 的 handler 必须可调用")
    _REGISTRY[spec.name] = spec


def get_tools() -> Dict[str, ToolSpec]:
    """按固定顺序返回 ``{name: ToolSpec}``；未在 TOOL_ORDER 中的工具追加在后面."""
    ordered: Dict[str, ToolSpec] = {}
    for name in TOOL_ORDER:
        if name in _REGISTRY:
            ordered[name] = _REGISTRY[name]
    for name, spec in _REGISTRY.items():
        if name not in ordered:
            ordered[name] = spec
    return ordered


def tool_schemas() -> List[dict]:
    """OpenAI function-calling 风格的工具描述列表."""
    return [
        {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
        for spec in get_tools().values()
    ]


def execute_tool(name: str, args: dict, ctx: ToolContext) -> dict:
    """按名字执行已注册工具，统一兜底异常并校验返回结构."""
    spec = get_tools().get(name)
    if spec is None:
        return {"ok": False, "error": f"未知工具：{name}"}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "工具参数必须是 JSON 对象（dict）"}
    try:
        result = spec.handler(args, ctx)
    except Exception as exc:  # noqa: BLE001 - 工具异常统一变成可回灌的 observation
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "error": f"工具 {name} 返回了非 dict 结果：{type(result).__name__}"}
    if "ok" not in result:
        return {"ok": False, "error": f"工具 {name} 的返回值缺少 ok 字段", "result": result}
    return result


# --------------------------------------------------------------------------- #
# 工具共用的小工具函数（artifact 路径 / trace_id）
# --------------------------------------------------------------------------- #
_SAFE_TRACE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_trace_id(ctx: ToolContext) -> str:
    """取用于文件名的 trace_id：过滤非法字符，缺失时用 notrace."""
    trace = getattr(ctx, "trace", None)
    raw = str(getattr(trace, "trace_id", "") or "notrace")
    cleaned = _SAFE_TRACE_RE.sub("_", raw).strip("._")[:64]
    return cleaned or "notrace"


def ensure_artifacts_dir(ctx: ToolContext) -> Path:
    """取工具产物目录并确保存在（默认 artifacts/）."""
    raw = getattr(ctx, "artifacts_dir", None)
    directory = Path(raw) if raw else Path("artifacts")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


__all__ = [
    "TOOL_ORDER",
    "ToolSpec",
    "ToolContext",
    "register",
    "get_tools",
    "tool_schemas",
    "execute_tool",
    "safe_trace_id",
    "ensure_artifacts_dir",
]
