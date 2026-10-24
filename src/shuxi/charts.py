"""SVG 图表渲染：优先用 matplotlib（Agg 后端），缺少 matplotlib 时降级为纯标准库 SVG。

两条渲染路径
------------
1. **matplotlib 路径**（默认）：``chart_type ∈ {line, bar, pie}``，``Figure + Agg`` 输出 SVG，
   字体嵌入为路径，不依赖运行环境字体；
2. **标准库降级路径**：matplotlib 不可用（未安装/安装失败/无图形后端）时，用纯 ``str``
   拼接生成 SVG（坐标轴线、网格、刻度文字、标题、数据点/柱/扇形都是原生 SVG 元素），
   只依赖标准库 ``math`` / ``html``。这样 CI 或离线环境里也能产出合法 .svg，
   前端（Streamlit）、报告、单测都不受第三方依赖影响。

设计约定
--------
1. 服务端无显示器，matplotlib 路径必须用 Agg 后端；
2. 不依赖中文字体：图表标签（labels / ylabel / title）由调用方传英文或日期字符串，
   这样在没装中文字体的机器上也不会出现方块乱码；
3. 输入校验失败一律抛 ValueError，工具层把它转成 ``{"ok": False, "error": ...}``。
"""

from __future__ import annotations

import html
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Sequence

# matplotlib 默认把缓存写到 ~/.matplotlib；CI/容器/沙箱里该目录常不可写并触发告警，
# 提前指向系统临时目录（仅当调用方没显式设置 MPLCONFIGDIR 时）。
os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "shuxi-matplotlib"))

# ---------------------------------------------------------------- 后端探测

_MATPLOTLIB_IMPORT_ERROR = ""
try:  # pragma: no cover - 依赖 matplotlib 是否可用，两种环境都会覆盖
    import matplotlib

    matplotlib.use("Agg")  # 必须在导入 pyplot 之前指定后端
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    _HAS_MATPLOTLIB = True
except Exception as exc:  # noqa: BLE001 - 任何导入/后端错误都降级，不能让渲染整个不可用
    _HAS_MATPLOTLIB = False
    _MATPLOTLIB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

CHART_TYPES = ("line", "bar", "pie")
RENDER_BACKENDS = ("matplotlib", "stdlib-svg")

# 统一配色，两条路径共用，保证视觉一致
_PALETTE = ("#2f6fed", "#7aa2f7", "#f2b134", "#41b883", "#e35d6a", "#9b8cff", "#54c7ec")
_PRIMARY = _PALETTE[0]
_GRID_ALPHA = 0.30
_FIG_SIZE = (7.2, 4.0)
_MAX_BAR_ANNOTATIONS = 12

# 标准库 SVG 画布参数（单位 px）
_SVG_WIDTH = 720
_SVG_HEIGHT = 400
_MARGIN_LEFT = 72
_MARGIN_RIGHT = 32
_MARGIN_TOP = 58
_MARGIN_BOTTOM = 74


def matplotlib_available() -> bool:
    """当前进程是否有可用的 matplotlib 渲染后端。"""
    return _HAS_MATPLOTLIB


def active_backend() -> str:
    """当前实际会使用的渲染后端名，便于测试/排查环境。"""
    return RENDER_BACKENDS[0] if _HAS_MATPLOTLIB else RENDER_BACKENDS[1]


def _repo_root() -> Path:
    # src/shuxi/charts.py -> 仓库根目录
    return Path(__file__).resolve().parents[2]


def _default_output_path() -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return _repo_root() / "artifacts" / f"chart_{stamp}_{os.getpid()}.svg"


def _coerce_inputs(labels: Sequence, values: Sequence) -> tuple[list[str], list[float]]:
    """校验并转换输入；数据为空/长度不一致/非数值时抛 ValueError。"""
    if labels is None or values is None:
        raise ValueError("没有可用于绘图的数据")
    labels = list(labels)
    values = list(values)
    if not labels or not values:
        raise ValueError("没有可用于绘图的数据")
    if len(labels) != len(values):
        raise ValueError(
            f"labels 与 values 长度不一致: {len(labels)} != {len(values)}"
        )

    clean_labels: list[str] = []
    clean_values: list[float] = []
    for index, (label, value) in enumerate(zip(labels, values)):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {index + 1} 个数据点不是数值: {value!r}") from exc
        if not math.isfinite(number):
            raise ValueError(f"第 {index + 1} 个数据点是 NaN/Inf，无法绘图")
        clean_labels.append(str(label))
        clean_values.append(number)
    return clean_labels, clean_values


# ================================================================ matplotlib 路径


def _apply_axis_style(axes, labels: list[str], ylabel: str) -> None:
    axes.grid(True, axis="y", alpha=_GRID_ALPHA, linestyle="--", linewidth=0.8)
    axes.set_axisbelow(True)
    axes.spines["top"].set_visible(False)
    axes.spines["right"].set_visible(False)
    if ylabel:
        axes.set_ylabel(ylabel, fontsize=10)
    longest = max((len(label) for label in labels), default=0)
    if len(labels) > 8 or longest > 10:
        axes.tick_params(axis="x", labelrotation=35, labelsize=8)
    else:
        axes.tick_params(axis="x", labelsize=9)


def _draw_line_mpl(axes, labels: list[str], values: list[float], ylabel: str) -> None:
    axes.plot(
        labels,
        values,
        color=_PRIMARY,
        linewidth=2.0,
        marker="o",
        markersize=4.5,
        markerfacecolor="white",
        markeredgewidth=1.4,
    )
    _apply_axis_style(axes, labels, ylabel)


def _draw_bar_mpl(axes, labels: list[str], values: list[float], ylabel: str) -> None:
    bars = axes.bar(labels, values, color=_PRIMARY, edgecolor="white", linewidth=0.8)
    _apply_axis_style(axes, labels, ylabel)
    if len(values) <= _MAX_BAR_ANNOTATIONS:
        for bar, value in zip(bars, values):
            axes.annotate(
                _format_number(value),
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=7.5,
                color="#333333",
            )


def _draw_pie_mpl(fig, axes, labels: list[str], values: list[float]) -> None:
    wedges, _texts, autotexts = axes.pie(
        values,
        labels=labels,
        autopct=lambda pct: f"{pct:.1f}%" if pct >= 4 else "",
        startangle=90,
        colors=list(_PALETTE),
        wedgeprops={"edgecolor": "white", "linewidth": 1.0},
        textprops={"fontsize": 9},
    )
    for text in autotexts:
        text.set_fontsize(8)
        text.set_color("white")
    axes.axis("equal")
    axes.legend(
        wedges,
        [f"{label} ({_format_number(value)})" for label, value in zip(labels, values)],
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        fontsize=8,
        frameon=False,
    )
    fig.subplots_adjust(right=0.62)


def _render_with_matplotlib(
    kind: str,
    labels: list[str],
    values: list[float],
    title: str,
    target: Path,
    ylabel: str,
) -> None:
    # 饼图右侧要放图例，用经典边距 + subplots_adjust 更稳；折线/柱状用 constrained 自适应
    figure_layout = None if kind == "pie" else "constrained"
    fig = Figure(figsize=_FIG_SIZE, layout=figure_layout)
    FigureCanvasAgg(fig)
    axes = fig.add_subplot(111)
    axes.set_title(title, fontsize=12, pad=12)
    if kind == "line":
        _draw_line_mpl(axes, labels, values, ylabel)
    elif kind == "bar":
        _draw_bar_mpl(axes, labels, values, ylabel)
    else:
        _draw_pie_mpl(fig, axes, labels, values)
    fig.savefig(target, format="svg", bbox_inches="tight", facecolor="white")
    fig.clear()


# ================================================================ 标准库 SVG 路径


def _format_number(value: float) -> str:
    """人类可读的数字格式（千分位 / 保留必要小数）。"""
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _escape(text: object) -> str:
    return html.escape(str(text), quote=True)


def _nice_number(value: float) -> float:
    """取 1/2/5×10^n 里第一个 ≥ value 的「整齐」步长。"""
    if value <= 0 or not math.isfinite(value):
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / (10 ** exponent)
    if fraction <= 1:
        nice = 1.0
    elif fraction <= 2:
        nice = 2.0
    elif fraction <= 5:
        nice = 5.0
    else:
        nice = 10.0
    return nice * (10 ** exponent)


def _nice_bounds(vmin: float, vmax: float, target_ticks: int = 5) -> tuple[float, float, list[float]]:
    """计算 y 轴上下界与刻度（含 0 基线，便于柱状图比较）。"""
    if not math.isfinite(vmin) or not math.isfinite(vmax):
        vmin, vmax = 0.0, 1.0
    if vmin > 0:
        vmin = 0.0
    if vmax < 0:
        vmax = 0.0
    if vmin == vmax:
        pad = abs(vmin) * 0.1 or 1.0
        vmin, vmax = vmin - pad, vmax + pad
    span = vmax - vmin
    step = _nice_number(span / max(target_ticks - 1, 1))
    lo = math.floor(vmin / step) * step
    hi = math.ceil(vmax / step) * step
    count = int(round((hi - lo) / step))
    ticks = [round(lo + index * step, 10) for index in range(count + 1)]
    return lo, hi, ticks


def _y_mapper(lo: float, hi: float):
    plot_height = _SVG_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM
    span = hi - lo or 1.0

    def to_y(value: float) -> float:
        return _MARGIN_TOP + plot_height * (hi - value) / span

    return to_y


def _x_label_markup(label: str, x: float, y: float) -> str:
    """x 轴标签；过长时旋转 -35°，纯 SVG transform 实现。"""
    text = _escape(label)
    if len(label) > 8:
        return (
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="10" fill="#4b5563" '
            f'text-anchor="end" transform="rotate(-35 {x:.1f} {y:.1f})">{text}</text>'
        )
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="11" fill="#4b5563" '
        f'text-anchor="middle" dominant-baseline="hanging">{text}</text>'
    )


def _axis_markup(lo: float, hi: float, ticks: list[float], to_y) -> list[str]:
    parts: list[str] = []
    plot_left = _MARGIN_LEFT
    plot_right = _SVG_WIDTH - _MARGIN_RIGHT
    baseline = to_y(0.0)
    for tick in ticks:
        y = to_y(tick)
        parts.append(
            f'<line x1="{plot_left}" y1="{y:.1f}" x2="{plot_right}" y2="{y:.1f}" '
            f'stroke="#e5e7eb" stroke-dasharray="4 4" stroke-width="1" />'
        )
        parts.append(
            f'<text x="{plot_left - 8}" y="{y:.1f}" font-size="10" fill="#6b7280" '
            f'text-anchor="end" dominant-baseline="middle">{_escape(_format_number(tick))}</text>'
        )
    parts.append(
        f'<line x1="{plot_left}" y1="{baseline:.1f}" x2="{plot_right}" y2="{baseline:.1f}" '
        f'stroke="#9ca3af" stroke-width="1" />'
    )
    parts.append(
        f'<line x1="{plot_left}" y1="{_MARGIN_TOP}" x2="{plot_left}" y2="{_MARGIN_TOP + (_SVG_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM)}" '
        f'stroke="#9ca3af" stroke-width="1" />'
    )
    return parts


def _title_markup(title: str, ylabel: str) -> list[str]:
    parts: list[str] = []
    if title:
        parts.append(
            f'<text x="{_SVG_WIDTH / 2:.1f}" y="30" font-size="16" font-weight="600" '
            f'fill="#1f2430" text-anchor="middle">{_escape(title)}</text>'
        )
    if ylabel:
        center_y = _MARGIN_TOP + (_SVG_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM) / 2
        parts.append(
            f'<text x="18" y="{center_y:.1f}" font-size="11" fill="#4b5563" '
            f'text-anchor="middle" transform="rotate(-90 18 {center_y:.1f})">{_escape(ylabel)}</text>'
        )
    return parts


def _x_positions(count: int) -> list[float]:
    plot_width = _SVG_WIDTH - _MARGIN_LEFT - _MARGIN_RIGHT
    if count <= 1:
        return [_MARGIN_LEFT + plot_width / 2]
    return [_MARGIN_LEFT + index * plot_width / (count - 1) for index in range(count)]


def _svg_line(labels: list[str], values: list[float], title: str, ylabel: str) -> str:
    lo, hi, ticks = _nice_bounds(min(values), max(values))
    to_y = _y_mapper(lo, hi)
    parts: list[str] = _title_markup(title, ylabel) + _axis_markup(lo, hi, ticks, to_y)
    xs = _x_positions(len(values))
    points = " ".join(f"{x:.1f},{to_y(value):.1f}" for x, value in zip(xs, values))
    parts.append(
        f'<polyline points="{points}" fill="none" stroke="{_PRIMARY}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />'
    )
    for x, value in zip(xs, values):
        parts.append(
            f'<circle cx="{x:.1f}" cy="{to_y(value):.1f}" r="3.5" fill="#ffffff" '
            f'stroke="{_PRIMARY}" stroke-width="1.6" />'
        )
    label_y = _SVG_HEIGHT - _MARGIN_BOTTOM + 20
    for x, label in zip(xs, labels):
        parts.append(_x_label_markup(label, x, label_y))
    return _svg_document("line chart", parts)


def _svg_bar(labels: list[str], values: list[float], title: str, ylabel: str) -> str:
    lo, hi, ticks = _nice_bounds(min(values), max(values))
    to_y = _y_mapper(lo, hi)
    parts: list[str] = _title_markup(title, ylabel) + _axis_markup(lo, hi, ticks, to_y)
    plot_width = _SVG_WIDTH - _MARGIN_LEFT - _MARGIN_RIGHT
    band = plot_width / max(len(values), 1)
    bar_width = band * 0.6
    baseline = to_y(0.0)
    for index, (label, value) in enumerate(zip(labels, values)):
        center = _MARGIN_LEFT + band * (index + 0.5)
        top = min(to_y(value), baseline)
        height = max(abs(to_y(value) - baseline), 1.0)
        parts.append(
            f'<rect x="{center - bar_width / 2:.1f}" y="{top:.1f}" width="{bar_width:.1f}" '
            f'height="{height:.1f}" fill="{_PRIMARY}" rx="2" />'
        )
        if len(values) <= _MAX_BAR_ANNOTATIONS:
            parts.append(
                f'<text x="{center:.1f}" y="{top - 6:.1f}" font-size="10" fill="#374151" '
                f'text-anchor="middle">{_escape(_format_number(value))}</text>'
            )
        parts.append(
            _x_label_markup(label, center, _SVG_HEIGHT - _MARGIN_BOTTOM + 20)
        )
    return _svg_document("bar chart", parts)


def _arc_path(cx: float, cy: float, radius: float, start_deg: float, end_deg: float) -> str:
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    x1 = cx + radius * math.cos(start)
    y1 = cy + radius * math.sin(start)
    x2 = cx + radius * math.cos(end)
    y2 = cy + radius * math.sin(end)
    delta = abs(end_deg - start_deg)
    large_flag = 1 if delta > 180 else 0
    return (
        f"M {cx:.2f} {cy:.2f} L {x1:.2f} {y1:.2f} "
        f"A {radius:.2f} {radius:.2f} 0 {large_flag} 1 {x2:.2f} {y2:.2f} Z"
    )


def _svg_pie(labels: list[str], values: list[float], title: str) -> str:
    total = sum(values)
    parts: list[str] = _title_markup(title, "")
    plot_width = _SVG_WIDTH - _MARGIN_LEFT - _MARGIN_RIGHT
    center_x = _MARGIN_LEFT + plot_width * 0.34
    center_y = _MARGIN_TOP + (_SVG_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM) / 2
    radius = min(_SVG_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM, plot_width * 0.55) / 2 - 8

    angle = -90.0
    for index, (label, value) in enumerate(zip(labels, values)):
        ratio = value / total
        delta = ratio * 360.0
        color = _PALETTE[index % len(_PALETTE)]
        if len(values) == 1 or delta >= 359.999:
            parts.append(
                f'<circle cx="{center_x:.2f}" cy="{center_y:.2f}" r="{radius:.2f}" '
                f'fill="{color}" stroke="#ffffff" stroke-width="1" />'
            )
        else:
            parts.append(
                f'<path d="{_arc_path(center_x, center_y, radius, angle, angle + delta)}" '
                f'fill="{color}" stroke="#ffffff" stroke-width="1" />'
            )
        if ratio >= 0.04:
            mid = math.radians(angle + delta / 2)
            text_x = center_x + radius * 0.62 * math.cos(mid)
            text_y = center_y + radius * 0.62 * math.sin(mid)
            parts.append(
                f'<text x="{text_x:.1f}" y="{text_y:.1f}" font-size="11" fill="#ffffff" '
                f'text-anchor="middle" dominant-baseline="middle">{_escape(f"{ratio * 100:.1f}%")}</text>'
            )
        angle += delta

    # 图例（右侧）
    legend_x = center_x + radius + 24
    legend_top = max(_MARGIN_TOP, center_y - len(values) * 11)
    for index, (label, value) in enumerate(zip(labels, values)):
        item_y = legend_top + index * 22
        color = _PALETTE[index % len(_PALETTE)]
        parts.append(
            f'<rect x="{legend_x:.1f}" y="{item_y - 9:.1f}" width="12" height="12" '
            f'rx="2" fill="{color}" />'
        )
        parts.append(
            f'<text x="{legend_x + 18:.1f}" y="{item_y:.1f}" font-size="11" fill="#374151" '
            f'dominant-baseline="middle">{_escape(label)} ({_escape(_format_number(value))})</text>'
        )
    return _svg_document("pie chart", parts)


def _svg_document(chart_name: str, body: list[str]) -> str:
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" '
        f'viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" role="img" aria-label="{_escape(chart_name)}">',
        f'  <rect width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" fill="#ffffff" />',
        '  <g font-family="Segoe UI, Arial, sans-serif">',
    ]
    lines.extend("  " + part for part in body)
    lines.extend(["  </g>", "</svg>", ""])
    return "\n".join(lines)


def _render_with_stdlib(
    kind: str,
    labels: list[str],
    values: list[float],
    title: str,
    target: Path,
    ylabel: str,
) -> None:
    if kind == "line":
        content = _svg_line(labels, values, title, ylabel)
    elif kind == "bar":
        content = _svg_bar(labels, values, title, ylabel)
    else:
        content = _svg_pie(labels, values, title)
    target.write_text(content, encoding="utf-8")


# ================================================================ 公共 API


def render_chart(
    chart_type: str,
    labels: list[str],
    values: list[float],
    title: str = "",
    output_path: Path | None = None,
    ylabel: str = "",
) -> Path:
    """把一组标签/数值渲染成 SVG 文件，返回输出路径。

    参数
    ----
    chart_type: ``line`` / ``bar`` / ``pie``（大小写不敏感）
    labels: x 轴标签（日期、月份或英文维度名，避免中文以规避字体依赖）
    values: 与 labels 等长的数值序列
    title: 图表标题（可选）
    output_path: 输出 .svg 路径；缺省写到 ``<repo>/artifacts/chart_<时间戳>.svg``
    ylabel: y 轴单位/含义（英文，可选）

    实现
    ----
    有 matplotlib 时走 Figure + Agg；没有时自动降级到纯标准库 SVG 渲染，
    两条路径输出同样的 .svg 结构与配色。

    异常
    ----
    ValueError: 数据为空、长度不一致、含非数值/NaN、类型不支持、饼图为负等。
    """
    kind = str(chart_type or "").strip().lower()
    if kind not in CHART_TYPES:
        raise ValueError(
            f"不支持的图表类型: {chart_type!r}（可选 line / bar / pie）"
        )

    clean_labels, clean_values = _coerce_inputs(labels, values)
    if kind == "pie":
        if any(value < 0 for value in clean_values):
            raise ValueError("饼图不支持负数，请改用 bar 或 line")
        if sum(clean_values) <= 0:
            raise ValueError("饼图数据总和必须大于 0")

    target = Path(output_path) if output_path is not None else _default_output_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    title_text = str(title or "")

    if _HAS_MATPLOTLIB:
        _render_with_matplotlib(kind, clean_labels, clean_values, title_text, target, ylabel)
    else:
        _render_with_stdlib(kind, clean_labels, clean_values, title_text, target, ylabel)
    return target


__all__ = [
    "CHART_TYPES",
    "RENDER_BACKENDS",
    "active_backend",
    "matplotlib_available",
    "render_chart",
]
