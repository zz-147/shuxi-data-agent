"""工具 5：generate_chart —— 指标时间序列 → SVG 图表.

实际绘图交给 ``shuxi.charts.render_chart``（py-ui 模块，matplotlib Agg 后端）；
本工具只负责：参数校验 → 复用 compute_metrics 的指标/过滤逻辑 → 调渲染函数 → 返回产物路径。
"""

from __future__ import annotations

from pathlib import Path

from .base import ToolContext, ToolSpec, ensure_artifacts_dir, register, safe_trace_id
from .metrics_tool import METRICS, metric_series, parse_metric_request

CHART_TYPES = ("line", "bar", "pie")
DEFAULT_GRAIN = "month"

PARAMETERS = {
    "type": "object",
    "properties": {
        "chart_type": {
            "type": "string",
            "enum": list(CHART_TYPES),
            "description": "图表类型：line 折线 / bar 柱状 / pie 饼图，默认 line",
        },
        "metric": {
            "type": "string",
            "enum": list(METRICS),
            "description": "指标名（同 compute_metrics）",
        },
        "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD（含）"},
        "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD（含）"},
        "grain": {
            "type": "string",
            "enum": ["day", "week", "month"],
            "description": "时间粒度，默认 month",
        },
        "filters": {
            "type": "object",
            "description": "维度筛选：region / channel / category / member_level",
            "properties": {
                "region": {"type": "string"},
                "channel": {"type": "string"},
                "category": {"type": "string"},
                "member_level": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "title": {"type": "string", "description": "图表标题，省略时根据指标自动生成"},
    },
    "required": ["metric", "start_date", "end_date"],
    "additionalProperties": False,
}


def _load_render_chart():
    """延迟导入图表渲染函数：py-ui 的 charts.py 未就绪时也能 import 本模块.

    测试可用 ``monkeypatch.setattr(chart_tool, "_load_render_chart", ...)`` 替换。
    """
    from ..charts import render_chart

    return render_chart


def generate_chart(args: dict, ctx: ToolContext) -> dict:
    """按指标时间序列生成 SVG 图表并写入 artifacts 目录."""
    try:
        chart_type = str(args.get("chart_type") or "line").strip().lower()
        if chart_type not in CHART_TYPES:
            raise ValueError(f"不支持的 chart_type：{chart_type!r}，可选 {list(CHART_TYPES)}")

        request = parse_metric_request(
            metric=args.get("metric"),
            start_date=args.get("start_date"),
            end_date=args.get("end_date"),
            grain=args.get("grain") or DEFAULT_GRAIN,
            filters=args.get("filters"),
        )
        series = metric_series(ctx.store, request)
        if not series:
            return {"ok": False, "error": "没有可用于绘图的数据（当前区间/筛选项无数据）"}

        labels = [str(item["period"]) for item in series]
        values = [float(item["value"]) for item in series]

        raw_title = args.get("title")
        if raw_title is not None and not isinstance(raw_title, str):
            raise ValueError("title 必须是字符串")
        title = raw_title.strip() if raw_title and raw_title.strip() else (
            f"{request['metric']} ({request['grain']})"
        )

        output_path = ensure_artifacts_dir(ctx) / f"chart_{safe_trace_id(ctx)}.svg"
        render_chart = _load_render_chart()
        written = render_chart(
            chart_type=chart_type,
            labels=labels,
            values=values,
            title=title,
            output_path=output_path,
            ylabel=request["metric"],
        )
        path = Path(written) if written is not None else output_path
        summary = (
            f"已生成 {chart_type} 图：指标 {request['metric']}，按 {request['grain']} 粒度 "
            f"共 {len(labels)} 个数据点，文件 {path.name}"
        )
        return {
            "ok": True,
            "path": str(path),
            "chart_type": chart_type,
            "points": len(labels),
            "metric": request["metric"],
            "grain": request["grain"],
            "labels": labels,
            "values": values,
            "summary": summary,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


register(
    ToolSpec(
        name="generate_chart",
        description=(
            "根据指标时间序列生成 SVG 图表（line/bar/pie），写入 artifacts/chart_<trace_id>.svg。"
            "参数与 compute_metrics 一致，另加 chart_type 和 title；图表标签使用日期/英文指标名，"
            "避免依赖中文字体。"
        ),
        parameters=PARAMETERS,
        risk="low",
        handler=generate_chart,
    )
)

__all__ = ["generate_chart", "PARAMETERS", "CHART_TYPES"]
