"""工具 3：compute_metrics —— 核心指标聚合（时间粒度 / 维度筛选 / 环比同比）.

**零依赖实现**：指标计算全部走 ``sqlite3`` 聚合 + 纯 Python 滚动汇总，不依赖 pandas；
pandas 只作为 ``DemoStore.query_dataframe`` 的可选增强保留。

指标口径（README / docs 保持同一口径）：
- ``sales_total``     销售额 = SUM(sales.amount)
- ``order_count``     订单量 = COUNT(DISTINCT sales.order_id)
- ``avg_order_value`` 客单价 = 销售额 / 订单量
- ``conversion_rate`` 转化率 = 期间下单客户数 / 期间结束时已注册客户数 × 100%
  （演示数仓没有流量表，用「注册客户购买转化」作代理口径，属于演示近似）
- ``repeat_rate``     复购率 = 期间下单 ≥2 次的客户数 / 期间下单客户数 × 100%

所有 SQL 都是固定模板 + ``?`` 参数化，filters 只做白名单映射，不拼接用户原始字符串。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .base import ToolContext, ToolSpec, register

METRICS = ("sales_total", "order_count", "avg_order_value", "conversion_rate", "repeat_rate")
ADDITIVE_METRICS = frozenset({"sales_total", "order_count"})
RATE_METRICS = frozenset({"conversion_rate", "repeat_rate"})
METRIC_LABELS = {
    "sales_total": "销售额",
    "order_count": "订单量",
    "avg_order_value": "客单价",
    "conversion_rate": "转化率",
    "repeat_rate": "复购率",
}
GRAINS = ("day", "week", "month")
COMPARE_MODES = ("mom", "yoy")
COMPARE_LABELS = {"mom": "环比", "yoy": "同比"}
FILTER_COLUMNS = {
    "region": "st.region",
    "channel": "s.channel",
    "category": "p.category",
    "member_level": "c.member_level",
}
FILTER_KEYS = tuple(FILTER_COLUMNS)

BASE_FROM = (
    " FROM sales s"
    " JOIN stores st ON s.store_id = st.store_id"
    " JOIN products p ON s.product_id = p.product_id"
    " JOIN customers c ON s.customer_id = c.customer_id"
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "enum": list(METRICS),
            "description": "指标名：sales_total / order_count / avg_order_value / conversion_rate / repeat_rate",
        },
        "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD（含）"},
        "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD（含）"},
        "grain": {
            "type": "string",
            "enum": list(GRAINS),
            "description": "时间粒度；省略时返回区间标量值",
        },
        "filters": {
            "type": "object",
            "description": "维度筛选，可选键：region / channel / category / member_level",
            "properties": {
                "region": {"type": "string"},
                "channel": {"type": "string"},
                "category": {"type": "string"},
                "member_level": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "compare": {
            "type": "string",
            "enum": list(COMPARE_MODES),
            "description": "对比方式：mom 环比 / yoy 同比；省略则不对比",
        },
    },
    "required": ["metric", "start_date", "end_date"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# 日期工具（纯标准库）
# --------------------------------------------------------------------------- #
def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _parse_date(value: Any, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 字符串")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise ValueError(f"{field} 格式错误：{value!r}，应为 YYYY-MM-DD") from error


def _shift_months(day: date, delta: int) -> date:
    total = day.year * 12 + (day.month - 1) + delta
    year, month_index = divmod(total, 12)
    month = month_index + 1
    return date(year, month, min(day.day, _days_in_month(year, month)))


def _shift_years(day: date, delta: int) -> date:
    year = day.year + delta
    return date(year, day.month, min(day.day, _days_in_month(year, day.month)))


def _is_full_month(start: date, end: date) -> bool:
    return (
        start.day == 1
        and end.day == _days_in_month(end.year, end.month)
        and (start.year, start.month) == (end.year, end.month)
    )


def _is_full_year(start: date, end: date) -> bool:
    return (
        start.year == end.year
        and start.month == 1
        and start.day == 1
        and end.month == 12
        and end.day == 31
    )


def compare_range(start: date, end: date, compare: str) -> Tuple[date, date]:
    """计算环比/同比的基期区间（整月/整年优先，其余按端点平移）."""
    if compare == "mom":
        if _is_full_month(start, end):
            prev_start = _shift_months(start, -1)
            return prev_start, date(
                prev_start.year, prev_start.month, _days_in_month(prev_start.year, prev_start.month)
            )
        return _shift_months(start, -1), _shift_months(end, -1)
    if compare == "yoy":
        if _is_full_month(start, end):
            prev_start = _shift_years(start, -1)
            return prev_start, date(
                prev_start.year, prev_start.month, _days_in_month(prev_start.year, prev_start.month)
            )
        return _shift_years(start, -1), _shift_years(end, -1)
    raise ValueError(f"不支持的 compare：{compare!r}，可选 {list(COMPARE_MODES)}")


# --------------------------------------------------------------------------- #
# 参数校验 / 归一化
# --------------------------------------------------------------------------- #
def _validate_compare(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("compare 必须是字符串 mom / yoy")
    normalized = value.strip().lower()
    if normalized not in COMPARE_MODES:
        raise ValueError(f"不支持的 compare：{value!r}，可选 {list(COMPARE_MODES)}")
    return normalized


def _validate_filters(filters: Any) -> Dict[str, str]:
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ValueError("filters 必须是对象（dict）")
    cleaned: Dict[str, str] = {}
    for key, value in filters.items():
        if key not in FILTER_COLUMNS:
            raise ValueError(f"不支持的筛选字段 {key!r}，可选：{list(FILTER_KEYS)}")
        if value is None or value == "":
            continue
        if not isinstance(value, (str, int, float)):
            raise ValueError(f"筛选字段 {key} 的取值必须是字符串")
        cleaned[key] = str(value)
    return cleaned


def parse_metric_request(
    metric: Any = None,
    start_date: Any = None,
    end_date: Any = None,
    grain: Any = None,
    filters: Any = None,
) -> dict:
    """校验并归一化 compute_metrics / generate_chart 的公共参数（非法时抛 ValueError）."""
    if not isinstance(metric, str) or metric not in METRICS:
        raise ValueError(f"不支持的 metric：{metric!r}，可选 {list(METRICS)}")
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start > end:
        raise ValueError(f"start_date（{start}）不能晚于 end_date（{end}）")
    normalized_grain: Optional[str] = None
    if grain not in (None, ""):
        if not isinstance(grain, str) or grain.strip().lower() not in GRAINS:
            raise ValueError(f"不支持的 grain：{grain!r}，可选 {list(GRAINS)}")
        normalized_grain = grain.strip().lower()
    return {
        "metric": metric,
        "start": start,
        "end": end,
        "grain": normalized_grain,
        "filters": _validate_filters(filters),
    }


# --------------------------------------------------------------------------- #
# 周期标签
# --------------------------------------------------------------------------- #
def _period_label(day_iso: str, grain: str) -> str:
    day = date.fromisoformat(str(day_iso)[:10])
    if grain == "day":
        return day.isoformat()
    if grain == "month":
        return f"{day.year:04d}-{day.month:02d}"
    iso = day.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _period_end(label: str, grain: str) -> date:
    if grain == "day":
        return date.fromisoformat(label)
    if grain == "month":
        year, month = (int(part) for part in label.split("-"))
        return date(year, month, _days_in_month(year, month))
    year_text, week_text = label.split("-W")
    return date.fromisocalendar(int(year_text), int(week_text), 7)


def _all_period_labels(start: date, end: date, grain: str) -> List[str]:
    """枚举 [start, end] 覆盖的全部周期标签（无数据的周期补 0，图表更直观）."""
    labels: List[str] = []
    if grain == "day":
        cursor = start
        while cursor <= end:
            labels.append(cursor.isoformat())
            cursor += timedelta(days=1)
        return labels
    if grain == "month":
        cursor = date(start.year, start.month, 1)
        last = date(end.year, end.month, 1)
        while cursor <= last:
            labels.append(f"{cursor.year:04d}-{cursor.month:02d}")
            cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(
                cursor.year, cursor.month + 1, 1
            )
        return labels
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        iso = monday.isocalendar()
        labels.append(f"{iso[0]:04d}-W{iso[1]:02d}")
        monday += timedelta(days=7)
    return labels


# --------------------------------------------------------------------------- #
# 取数（sqlite3，零依赖）
# --------------------------------------------------------------------------- #
def _where_clause(start: date, end: date, filters: Dict[str, str]) -> Tuple[str, List[Any]]:
    clauses = ["s.order_date >= ?", "s.order_date <= ?"]
    params: List[Any] = [start.isoformat(), end.isoformat()]
    for key in FILTER_KEYS:
        if key in filters:
            clauses.append(f"{FILTER_COLUMNS[key]} = ?")
            params.append(filters[key])
    return " WHERE " + " AND ".join(clauses), params


def _fetch_daily_summary(store: Any, start: date, end: date, filters: Dict[str, str]) -> List[dict]:
    """按天聚合：order_date / 订单数 / 去重买家数 / 销售额（最多 366 行）."""
    where, params = _where_clause(start, end, filters)
    sql = (
        "SELECT s.order_date AS day, COUNT(DISTINCT s.order_id) AS orders, "
        "COUNT(DISTINCT s.customer_id) AS buyers, COALESCE(SUM(s.amount), 0) AS amount"
        + BASE_FROM
        + where
        + " GROUP BY s.order_date ORDER BY s.order_date"
    )
    result = store.execute_readonly(sql, params, limit=MAX_DAILY_ROWS)
    daily: List[dict] = []
    for row in result.rows:
        daily.append(
            {
                "day": str(row[0]),
                "orders": int(row[1] or 0),
                "buyers": int(row[2] or 0),
                "amount": float(row[3] or 0.0),
            }
        )
    return daily


def _fetch_customer_orders(store: Any, start: date, end: date, filters: Dict[str, str]) -> List[Tuple[str, str, int]]:
    """按 (日期, 客户) 聚合订单数，用于转化率/复购率的去重计算（≤4000 行）."""
    where, params = _where_clause(start, end, filters)
    sql = (
        "SELECT s.order_date AS day, s.customer_id AS customer_id, "
        "COUNT(DISTINCT s.order_id) AS orders"
        + BASE_FROM
        + where
        + " GROUP BY s.order_date, s.customer_id"
    )
    result = store.execute_readonly(sql, params, limit=MAX_CUSTOMER_DAY_ROWS)
    return [(str(row[0]), str(row[1]), int(row[2] or 0)) for row in result.rows]


def _fetch_registration_dates(store: Any, filters: Dict[str, str]) -> List[str]:
    """客户注册日期（升序），用于转化率分母的「截至某日已注册客户数」."""
    sql = "SELECT register_date FROM customers"
    params: List[Any] = []
    if "member_level" in filters:
        sql += " WHERE member_level = ?"
        params.append(filters["member_level"])
    sql += " ORDER BY register_date"
    result = store.execute_readonly(sql, params, limit=MAX_CUSTOMER_ROWS)
    return [str(row[0]) for row in result.rows]


def _registered_count(sorted_dates: List[str], as_of: date) -> int:
    return bisect_right(sorted_dates, as_of.isoformat())


MAX_DAILY_ROWS = 366
MAX_CUSTOMER_DAY_ROWS = 8000
MAX_CUSTOMER_ROWS = 5000


# --------------------------------------------------------------------------- #
# 聚合计算
# --------------------------------------------------------------------------- #
def _additive_scalar(daily: List[dict], metric: str) -> float:
    amount = round(sum(item["amount"] for item in daily), 2)
    orders = int(sum(item["orders"] for item in daily))
    if metric == "sales_total":
        return amount
    if metric == "order_count":
        return orders
    if metric == "avg_order_value":
        return round(amount / orders, 2) if orders else 0.0
    raise ValueError(f"不支持的 metric：{metric}")


def _bucket_value(bucket: Dict[str, float], metric: str) -> float:
    if metric == "sales_total":
        return round(bucket["amount"], 2)
    if metric == "order_count":
        return int(bucket["orders"])
    if metric == "avg_order_value":
        return round(bucket["amount"] / bucket["orders"], 2) if bucket["orders"] else 0.0
    raise ValueError(f"不支持的 metric：{metric}")


def _additive_series(daily: List[dict], metric: str, grain: str, start: date, end: date) -> List[dict]:
    labels = _all_period_labels(start, end, grain)
    buckets: Dict[str, Dict[str, float]] = {
        label: {"amount": 0.0, "orders": 0} for label in labels
    }
    for item in daily:
        label = _period_label(item["day"], grain)
        bucket = buckets.get(label)
        if bucket is None:
            continue
        bucket["amount"] += item["amount"]
        bucket["orders"] += item["orders"]
    return [{"period": label, "value": _bucket_value(buckets[label], metric)} for label in labels]


def _rate_series(
    customer_orders: List[Tuple[str, str, int]],
    metric: str,
    grain: str,
    start: date,
    end: date,
    registration_dates: List[str],
) -> List[dict]:
    labels = _all_period_labels(start, end, grain)
    per_period: Dict[str, Dict[str, int]] = {label: {} for label in labels}
    for day, customer, orders in customer_orders:
        label = _period_label(day, grain)
        bucket = per_period.get(label)
        if bucket is None:
            continue
        bucket[customer] = bucket.get(customer, 0) + orders
    series: List[dict] = []
    for label in labels:
        counts = per_period[label]
        buyers = len(counts)
        if metric == "repeat_rate":
            repeat = sum(1 for count in counts.values() if count >= 2)
            value = round(repeat / buyers * 100, 2) if buyers else 0.0
        else:
            denominator = _registered_count(registration_dates, _period_end(label, grain))
            value = round(buyers / denominator * 100, 2) if denominator else 0.0
        series.append({"period": label, "value": value})
    return series


def _rate_scalar(
    customer_orders: List[Tuple[str, str, int]],
    metric: str,
    registration_dates: List[str],
    end: date,
) -> float:
    counts: Dict[str, int] = {}
    for _, customer, orders in customer_orders:
        counts[customer] = counts.get(customer, 0) + orders
    buyers = len(counts)
    if metric == "repeat_rate":
        repeat = sum(1 for count in counts.values() if count >= 2)
        return round(repeat / buyers * 100, 2) if buyers else 0.0
    denominator = _registered_count(registration_dates, end)
    return round(buyers / denominator * 100, 2) if denominator else 0.0


def _change_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous in (None, 0):
        return None
    return round((float(current) - float(previous)) / abs(float(previous)) * 100, 2)


def _format_value(metric: str, value: Optional[float]) -> str:
    if value is None:
        return "—"
    if metric == "order_count":
        return f"{int(value)} 单"
    if metric in RATE_METRICS:
        return f"{float(value):.2f}%"
    return f"¥{float(value):,.2f}"


def compute_metric(store: Any, request: dict, compare: Optional[str] = None) -> dict:
    """按已校验的 request 计算指标（不含 ok 字段，供 handler 与图表工具复用）."""
    metric = request["metric"]
    grain = request["grain"]
    filters = request["filters"]
    start, end = request["start"], request["end"]

    registration_dates: List[str] = []
    if metric in RATE_METRICS:
        customer_orders = _fetch_customer_orders(store, start, end, filters)
        if metric == "conversion_rate":
            registration_dates = _fetch_registration_dates(store, filters)
        current_total = _rate_scalar(customer_orders, metric, registration_dates, end)
        current: Any = (
            _rate_series(customer_orders, metric, grain, start, end, registration_dates)
            if grain
            else current_total
        )
        has_current_data = bool(customer_orders)
    else:
        daily = _fetch_daily_summary(store, start, end, filters)
        current_total = _additive_scalar(daily, metric)
        current = _additive_series(daily, metric, grain, start, end) if grain else current_total
        has_current_data = bool(daily)

    previous: Any = None
    previous_total: Optional[float] = None
    change_pct: Optional[float] = None
    previous_period: Optional[dict] = None
    if compare:
        prev_start, prev_end = compare_range(start, end, compare)
        previous_period = {"start": prev_start.isoformat(), "end": prev_end.isoformat()}
        if metric in RATE_METRICS:
            previous_orders = _fetch_customer_orders(store, prev_start, prev_end, filters)
            if previous_orders:
                previous_total = _rate_scalar(previous_orders, metric, registration_dates, prev_end)
                previous = (
                    _rate_series(previous_orders, metric, grain, prev_start, prev_end, registration_dates)
                    if grain
                    else previous_total
                )
                change_pct = _change_pct(current_total, previous_total)
        else:
            previous_daily = _fetch_daily_summary(store, prev_start, prev_end, filters)
            if previous_daily:
                previous_total = _additive_scalar(previous_daily, metric)
                previous = (
                    _additive_series(previous_daily, metric, grain, prev_start, prev_end)
                    if grain
                    else previous_total
                )
                change_pct = _change_pct(current_total, previous_total)

    summary = _build_summary(
        metric=metric,
        start=start,
        end=end,
        grain=grain,
        current_total=current_total,
        current=current,
        previous_total=previous_total,
        change_pct=change_pct,
        compare=compare,
        previous_period=previous_period,
        has_current_data=has_current_data,
    )
    return {
        "metric": metric,
        "grain": grain,
        "current": current,
        "previous": previous,
        "change_pct": change_pct,
        "current_total": current_total,
        "previous_total": previous_total,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "compare": compare,
        "filters": filters,
        "previous_period": previous_period,
        "summary": summary,
    }


def _build_summary(
    *,
    metric: str,
    start: date,
    end: date,
    grain: Optional[str],
    current_total: float,
    current: Any,
    previous_total: Optional[float],
    change_pct: Optional[float],
    compare: Optional[str],
    previous_period: Optional[dict],
    has_current_data: bool,
) -> str:
    label = METRIC_LABELS[metric]
    current_text = _format_value(metric, current_total)
    if grain and isinstance(current, list):
        summary = (
            f"{start.isoformat()}~{end.isoformat()} 的{label}按 {grain} 粒度共 "
            f"{len(current)} 个周期，整体为 {current_text}"
        )
    else:
        summary = f"{start.isoformat()}~{end.isoformat()} 的{label}为 {current_text}"

    if compare and previous_total is not None and change_pct is not None and previous_period:
        direction = "增长" if change_pct >= 0 else "下降"
        summary += (
            f"；较 {previous_period['start']}~{previous_period['end']}"
            f"（{COMPARE_LABELS.get(compare, compare)}）{direction} {abs(change_pct):.2f}%"
            f"（{_format_value(metric, previous_total)} → {current_text}）"
        )
    elif compare:
        summary += f"；无可比基期数据（{COMPARE_LABELS.get(compare, compare)}，演示数据仅覆盖 2025 年）"
    if not has_current_data:
        summary += "（当前区间无数据）"
    return summary


# --------------------------------------------------------------------------- #
# 对外复用：图表工具的时间序列
# --------------------------------------------------------------------------- #
def metric_series(store: Any, request: dict) -> List[dict]:
    """按 grain 输出 ``[{"period","value"}]``（供 generate_chart 复用）."""
    if not request.get("grain"):
        raise ValueError("时间序列需要显式指定 grain（day / week / month）")
    metric = request["metric"]
    grain = request["grain"]
    start, end = request["start"], request["end"]
    filters = request["filters"]
    if metric in RATE_METRICS:
        customer_orders = _fetch_customer_orders(store, start, end, filters)
        if not customer_orders:
            return []
        registration_dates = (
            _fetch_registration_dates(store, filters) if metric == "conversion_rate" else []
        )
        return _rate_series(customer_orders, metric, grain, start, end, registration_dates)
    daily = _fetch_daily_summary(store, start, end, filters)
    if not daily:
        return []
    return _additive_series(daily, metric, grain, start, end)


# --------------------------------------------------------------------------- #
# handler
# --------------------------------------------------------------------------- #
def compute_metrics(args: dict, ctx: ToolContext) -> dict:
    """核心指标计算：时间序列/标量 + 环比/同比 + 维度筛选."""
    try:
        request = parse_metric_request(
            metric=args.get("metric"),
            start_date=args.get("start_date"),
            end_date=args.get("end_date"),
            grain=args.get("grain"),
            filters=args.get("filters"),
        )
        compare = _validate_compare(args.get("compare"))
        result = compute_metric(ctx.store, request, compare=compare)
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


register(
    ToolSpec(
        name="compute_metrics",
        description=(
            "计算核心业务指标：sales_total 销售额 / order_count 订单量 / avg_order_value 客单价 / "
            "conversion_rate 转化率 / repeat_rate 复购率。支持 start_date~end_date、"
            "grain 时间粒度（day/week/month）、filters 维度筛选（region/channel/category/member_level）"
            "以及 compare=mom 环比 / yoy 同比。"
        ),
        parameters=PARAMETERS,
        risk="low",
        handler=compute_metrics,
    )
)

__all__ = [
    "compute_metrics",
    "compute_metric",
    "metric_series",
    "parse_metric_request",
    "compare_range",
    "PARAMETERS",
    "METRICS",
    "GRAINS",
    "FILTER_KEYS",
    "BASE_FROM",
    "FILTER_COLUMNS",
    "METRIC_LABELS",
    "_format_value",
    "_change_pct",
    "_period_label",
    "_days_in_month",
]
