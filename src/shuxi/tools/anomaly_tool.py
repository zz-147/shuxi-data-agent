"""工具 4：detect_anomaly_drivers —— 定位指标环比变化的主要贡献维度.

**零依赖实现**：一条 ``GROUP BY 维度, 月份`` 的 sqlite3 聚合即可拿到本期/基期数据，
再在纯 Python 里算 change_pct / contribution_pct，不依赖 pandas。

用来回答「销售额为什么涨/跌」：把本期相对上月的总变化拆到
region / channel / category 等维度上，按变化绝对值取 Top-N。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Tuple

from .base import ToolContext, ToolSpec, register
from .metrics_tool import (
    BASE_FROM,
    METRIC_LABELS,
    _change_pct,
    _days_in_month,
    _format_value,
)

ANOMALY_METRICS = ("sales_total", "order_count")
DIMENSIONS = ("region", "channel", "category")
DIMENSION_COLUMNS = {
    "region": "st.region",
    "channel": "s.channel",
    "category": "p.category",
}
DEFAULT_TOP_N = 5
MAX_TOP_N = 20

PARAMETERS = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "enum": list(ANOMALY_METRICS),
            "description": "要归因的指标：sales_total 销售额 / order_count 订单量，默认 sales_total",
        },
        "period": {
            "type": "string",
            "description": '分析月份 YYYY-MM（如 "2025-09"），基期为上一个自然月',
        },
        "dimension": {
            "type": "string",
            "enum": list(DIMENSIONS),
            "description": "拆解维度：region 区域 / channel 渠道 / category 品类，默认 region",
        },
        "top_n": {
            "type": "integer",
            "description": "返回变化最大的前 N 个维度值，默认 5，最大 20",
            "minimum": 1,
            "maximum": MAX_TOP_N,
        },
    },
    "required": ["period"],
    "additionalProperties": False,
}


def _parse_period(period: Any) -> Tuple[date, date]:
    if not isinstance(period, str) or not period.strip():
        raise ValueError('period 必须是 YYYY-MM 字符串（如 "2025-09"）')
    parts = period.strip().split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f'period 格式错误：{period!r}，应为 "YYYY-MM"')
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"period 月份非法：{period!r}")
    return date(year, month, 1), date(year, month, _days_in_month(year, month))


def _previous_month(month_start: date) -> Tuple[date, date]:
    if month_start.month == 1:
        prev_start = date(month_start.year - 1, 12, 1)
    else:
        prev_start = date(month_start.year, month_start.month - 1, 1)
    return prev_start, date(
        prev_start.year, prev_start.month, _days_in_month(prev_start.year, prev_start.month)
    )


def _month_label(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _format_change(metric: str, value: float) -> str:
    """带正负号的变化量文案（销售额用元，订单量用单）."""
    if metric == "order_count":
        return f"{int(round(float(value))):+d} 单"
    return f"{float(value):+,.2f} 元"


def _parse_top_n(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_TOP_N
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"top_n 必须是整数，实际为 {value!r}") from error
    return max(1, min(parsed, MAX_TOP_N))


def _fetch_dimension_values(
    store: Any,
    start: date,
    end: date,
    dimension: str,
    metric: str,
) -> Dict[Tuple[str, str], float]:
    """按 (维度值, 月份) 聚合，返回 ``{(dim_value, "YYYY-MM"): metric_value}``."""
    dimension_column = DIMENSION_COLUMNS[dimension]
    value_expression = (
        "COALESCE(SUM(s.amount), 0)" if metric == "sales_total" else "COUNT(DISTINCT s.order_id)"
    )
    sql = (
        f"SELECT {dimension_column} AS dim_value, substr(s.order_date, 1, 7) AS period, "
        f"{value_expression} AS metric_value"
        + BASE_FROM
        + " WHERE s.order_date >= ? AND s.order_date <= ?"
        + " GROUP BY dim_value, period"
    )
    result = store.execute_readonly(sql, [start.isoformat(), end.isoformat()], limit=500)
    values: Dict[Tuple[str, str], float] = {}
    for dim_value, period, metric_value in result.rows:
        key = (str(dim_value), str(period))
        values[key] = round(float(metric_value or 0.0), 2)
    return values


def detect_anomaly_drivers(args: dict, ctx: ToolContext) -> dict:
    """拆解某个指标在指定月份相对上月的维度贡献."""
    try:
        metric = args.get("metric") or "sales_total"
        if metric not in ANOMALY_METRICS:
            raise ValueError(
                f"不支持的 metric：{metric!r}，detect_anomaly_drivers 目前支持 {list(ANOMALY_METRICS)}"
            )
        period_start, period_end = _parse_period(args.get("period"))
        dimension = args.get("dimension") or "region"
        if dimension not in DIMENSIONS:
            raise ValueError(f"不支持的 dimension：{dimension!r}，可选 {list(DIMENSIONS)}")
        top_n = _parse_top_n(args.get("top_n"))

        baseline_start, baseline_end = _previous_month(period_start)
        current_label = _month_label(period_start)
        baseline_label = _month_label(baseline_start)
        aggregates = _fetch_dimension_values(
            ctx.store, baseline_start, period_end, dimension, metric
        )
        if not aggregates:
            return {
                "ok": False,
                "error": f"{baseline_label}~{current_label} 区间没有销售数据",
            }

        current_values: Dict[str, float] = {}
        baseline_values: Dict[str, float] = {}
        for (dim_value, period), metric_value in aggregates.items():
            if period == current_label:
                current_values[dim_value] = metric_value
            elif period == baseline_label:
                baseline_values[dim_value] = metric_value

        dimension_values = sorted(set(current_values) | set(baseline_values))
        total_current = sum(current_values.values())
        total_baseline = sum(baseline_values.values())
        total_change = total_current - total_baseline

        rows: List[dict] = []
        for dim_value in dimension_values:
            current = round(float(current_values.get(dim_value, 0.0)), 2)
            previous = round(float(baseline_values.get(dim_value, 0.0)), 2)
            change = round(current - previous, 2)
            rows.append(
                {
                    "value": dim_value,
                    "current": _round_metric(metric, current),
                    "previous": _round_metric(metric, previous),
                    "change": _round_metric(metric, change),
                    "change_pct": _change_pct(current, previous),
                    "contribution_pct": (
                        round(change / abs(total_change) * 100, 2) if total_change else 0.0
                    ),
                }
            )
        rows.sort(key=lambda item: abs(item["change"]), reverse=True)
        drivers = rows[:top_n]

        label = METRIC_LABELS.get(metric, metric)
        total_change_pct = _change_pct(total_current, total_baseline)
        summary = (
            f"{current_label} {label}为 {_format_value(metric, total_current)}，"
            f"环比 {baseline_label} 变化 {_format_change(metric, total_change)}"
        )
        if total_change_pct is not None:
            summary += f"（{total_change_pct:+.2f}%）"
        gainers = [row for row in rows if row["change"] > 0]
        losers = [row for row in rows if row["change"] < 0]
        if gainers:
            summary += (
                f"；主要拉动：{gainers[0]['value']} "
                f"{_format_change(metric, gainers[0]['change'])}"
                f"（贡献 {gainers[0]['contribution_pct']:+.2f}%）"
            )
        if losers:
            summary += (
                f"；主要拖累：{losers[0]['value']} "
                f"{_format_change(metric, losers[0]['change'])}"
                f"（贡献 {losers[0]['contribution_pct']:+.2f}%）"
            )

        return {
            "ok": True,
            "period": current_label,
            "baseline_period": baseline_label,
            "metric": metric,
            "dimension": dimension,
            "current_total": _round_metric(metric, total_current),
            "previous_total": _round_metric(metric, total_baseline),
            "change_pct": total_change_pct,
            "drivers": drivers,
            "summary": summary,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _round_metric(metric: str, value: float):
    if metric == "order_count":
        return int(round(float(value)))
    return round(float(value), 2)


register(
    ToolSpec(
        name="detect_anomaly_drivers",
        description=(
            "指标异常归因：给定月份 YYYY-MM 和维度（region/channel/category），"
            "对比上一个自然月，返回各维度值的 current/previous/change_pct/contribution_pct，"
            "按变化绝对值排序取 Top-N，用于解释销售额为什么涨/跌。"
        ),
        parameters=PARAMETERS,
        risk="low",
        handler=detect_anomaly_drivers,
    )
)

__all__ = ["detect_anomaly_drivers", "PARAMETERS", "DIMENSIONS", "ANOMALY_METRICS"]
