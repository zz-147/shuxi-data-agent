"""工具层入口：注册表 + OpenAI function-calling 风格 schema + 统一执行入口.

固定顺序（prompt / CLI / UI 展示稳定）：
``inspect_schema → query_data → compute_metrics → detect_anomaly_drivers →
generate_chart → export_report``
"""

from __future__ import annotations

from .base import (
    TOOL_ORDER,
    ToolContext,
    ToolSpec,
    execute_tool,
    get_tools,
    register,
    tool_schemas,
)

# 逐个 import 触发模块级 register()；顺序与 TOOL_ORDER 一致
from . import schema_tool  # noqa: F401,E402  (工具 1)
from . import query_tool  # noqa: F401,E402  (工具 2)
from . import metrics_tool  # noqa: F401,E402  (工具 3)
from . import anomaly_tool  # noqa: F401,E402  (工具 4)
from . import chart_tool  # noqa: F401,E402  (工具 5)
from . import report_tool  # noqa: F401,E402  (工具 6)

__all__ = [
    "TOOL_ORDER",
    "ToolContext",
    "ToolSpec",
    "register",
    "get_tools",
    "tool_schemas",
    "execute_tool",
    "schema_tool",
    "query_tool",
    "metrics_tool",
    "anomaly_tool",
    "chart_tool",
    "report_tool",
]
