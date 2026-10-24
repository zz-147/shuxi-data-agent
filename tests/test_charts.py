# -*- coding: utf-8 -*-
"""charts.render_chart 单测：line/bar/pie 各生成一个 SVG，非法输入抛 ValueError。

覆盖两条渲染路径：
- 有 matplotlib 时走 Figure + Agg（默认）；
- 没有 matplotlib 时自动降级为纯标准库 SVG（测试里通过 mock 强制走降级路径）。
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

# 支持 `python -m unittest discover -s tests`（discover 不会执行 tests/__init__.py）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shuxi.charts import (  # noqa: E402
    CHART_TYPES,
    RENDER_BACKENDS,
    active_backend,
    matplotlib_available,
    render_chart,
)

LABELS = ["2025-06", "2025-07", "2025-08", "2025-09"]
VALUES = [120_000.0, 132_500.0, 128_800.0, 158_400.0]


def _assert_valid_svg(testcase: unittest.TestCase, content: str) -> None:
    """基本合法性断言：含 svg 根元素，且能被标准库 XML 解析。"""
    testcase.assertIn("<svg", content)
    testcase.assertIn("</svg>", content)
    testcase.assertGreater(len(content), 500, "SVG 文件内容过小，可能没画上内容")
    ET.fromstring(content.encode("utf-8"))


class RenderChartTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------------------------------------------------------------- happy path

    def test_line_chart_writes_svg(self) -> None:
        output = self.tmpdir / "line.svg"
        path = render_chart("line", LABELS, VALUES, title="Monthly Sales", output_path=output, ylabel="CNY")
        self.assertEqual(Path(path), output)
        self.assertTrue(output.exists(), "SVG 文件未生成")
        _assert_valid_svg(self, output.read_text(encoding="utf-8"))

    def test_bar_chart_writes_svg(self) -> None:
        output = self.tmpdir / "bar.svg"
        path = render_chart("bar", LABELS, VALUES, title="Monthly Sales", output_path=output)
        self.assertTrue(Path(path).exists())
        content = output.read_text(encoding="utf-8")
        _assert_valid_svg(self, content)
        # 柱状图应有矩形（matplotlib 输出 path，标准库降级输出 rect）
        self.assertTrue("<rect" in content or "<path" in content)

    def test_pie_chart_writes_svg(self) -> None:
        output = self.tmpdir / "pie.svg"
        labels = ["East", "North", "South", "West"]
        values = [38.0, 27.0, 20.0, 15.0]
        path = render_chart("pie", labels, values, title="Regional Share", output_path=output)
        self.assertTrue(Path(path).exists())
        _assert_valid_svg(self, Path(path).read_text(encoding="utf-8"))

    def test_chart_type_is_case_insensitive(self) -> None:
        output = self.tmpdir / "line_upper.svg"
        path = render_chart("LINE", LABELS, VALUES, output_path=output)
        self.assertTrue(Path(path).exists())

    def test_output_parent_dirs_are_created(self) -> None:
        output = self.tmpdir / "nested" / "deeper" / "chart.svg"
        path = render_chart("line", LABELS, VALUES, output_path=output)
        self.assertTrue(Path(path).exists())
        self.assertTrue(output.parent.is_dir())

    def test_default_output_path_used_when_missing(self) -> None:
        path = render_chart("bar", ["Mon", "Tue"], [3.0, 5.0], title="demo")
        try:
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).suffix.lower(), ".svg")
            self.assertIn("<svg", Path(path).read_text(encoding="utf-8"))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_all_supported_types_declared(self) -> None:
        self.assertEqual(set(CHART_TYPES), {"line", "bar", "pie"})

    def test_unicode_labels_do_not_crash(self) -> None:
        """不依赖中文字体：即便标题/标签含中文也不能崩（渲染成方块可接受）。"""
        output = self.tmpdir / "unicode.svg"
        path = render_chart("bar", ["华东", "华北"], [10.0, 20.0], title="区域销售额", output_path=output)
        self.assertTrue(Path(path).exists())
        self.assertIn("<svg", Path(path).read_text(encoding="utf-8"))

    def test_values_accept_ints_and_numeric_strings(self) -> None:
        output = self.tmpdir / "coerced.svg"
        path = render_chart("line", ["A", "B", "C"], [1, "2.5", 3.4], output_path=output)  # type: ignore[list-item]
        self.assertIn("<svg", Path(path).read_text(encoding="utf-8"))

    def test_backend_helpers(self) -> None:
        self.assertIsInstance(matplotlib_available(), bool)
        self.assertIn(active_backend(), RENDER_BACKENDS)

    # ---------------------------------------------------------------- error path

    def test_empty_labels_raise_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            render_chart("line", [], [])
        self.assertIn("没有可用于绘图的数据", str(ctx.exception))

    def test_empty_values_raise_value_error(self) -> None:
        with self.assertRaises(ValueError):
            render_chart("bar", ["A", "B"], [])

    def test_none_input_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            render_chart("pie", None, None)  # type: ignore[arg-type]
        self.assertIn("没有可用于绘图的数据", str(ctx.exception))

    def test_length_mismatch_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            render_chart("bar", ["A", "B", "C"], [1.0, 2.0])

    def test_unknown_chart_type_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            render_chart("radar", LABELS, VALUES)

    def test_nan_value_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            render_chart("line", ["A", "B"], [1.0, float("nan")])

    def test_non_numeric_value_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            render_chart("line", ["A", "B"], [1.0, "not-a-number"])  # type: ignore[list-item]

    def test_negative_pie_values_raise_value_error(self) -> None:
        with self.assertRaises(ValueError):
            render_chart("pie", ["A", "B"], [-5.0, 10.0])

    def test_zero_sum_pie_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            render_chart("pie", ["A", "B"], [0.0, 0.0])

    def test_error_path_does_not_leave_file(self) -> None:
        output = self.tmpdir / "should_not_exist.svg"
        with self.assertRaises(ValueError):
            render_chart("line", [], [], output_path=output)
        self.assertFalse(output.exists())

    def test_svg_is_well_formed_xml(self) -> None:
        output = self.tmpdir / "xml.svg"
        render_chart("line", LABELS, VALUES, title="XML Check", output_path=output)
        content = output.read_text(encoding="utf-8")
        self.assertTrue(content.lstrip().startswith("<?xml"), "SVG 应带 XML 声明")
        self.assertIsNotNone(re.search(r"<svg[^>]*>", content))


class StdlibFallbackTest(unittest.TestCase):
    """强制走纯标准库 SVG 降级路径（不依赖 matplotlib 也要能出合法图）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _render(self, kind: str, labels, values, **kwargs) -> Path:
        with mock.patch("shuxi.charts._HAS_MATPLOTLIB", False):
            return render_chart(kind, labels, values, **kwargs)

    def test_fallback_renders_all_three_types(self) -> None:
        cases = {
            "line": (LABELS, VALUES),
            "bar": (LABELS, VALUES),
            "pie": (["East", "North", "South", "West"], [38.0, 27.0, 20.0, 15.0]),
        }
        for kind, (labels, values) in cases.items():
            with self.subTest(kind=kind):
                output = self.tmpdir / f"fallback_{kind}.svg"
                path = self._render(kind, labels, values, title="Fallback Chart", output_path=output)
                self.assertTrue(Path(path).exists())
                content = Path(path).read_text(encoding="utf-8")
                _assert_valid_svg(self, content)
                self.assertEqual(Path(path).suffix.lower(), ".svg")

    def test_fallback_keeps_text_labels(self) -> None:
        output = self.tmpdir / "fallback_text.svg"
        self._render("bar", ["2025-06", "2025-07"], [10.0, 20.0], title="Sales Trend", output_path=output)
        content = output.read_text(encoding="utf-8")
        self.assertIn("<text", content)
        self.assertIn("Sales Trend", content, "降级路径应保留标题文本")
        self.assertIn("2025-06", content, "降级路径应保留 x 轴标签文本")

    def test_fallback_escapes_unsafe_text(self) -> None:
        output = self.tmpdir / "fallback_escape.svg"
        self._render("pie", ["华东", "A&B"], [60.0, 40.0], title="区域 <占比>", output_path=output)
        content = output.read_text(encoding="utf-8")
        ET.fromstring(content.encode("utf-8"))  # 转义正确才能解析
        self.assertIn("&amp;", content)
        self.assertIn("&lt;占比&gt;", content)

    def test_fallback_single_slice_pie(self) -> None:
        output = self.tmpdir / "fallback_single.svg"
        path = self._render("pie", ["Only One"], [100.0], title="Single", output_path=output)
        _assert_valid_svg(self, Path(path).read_text(encoding="utf-8"))

    def test_fallback_negative_values_bar_ok(self) -> None:
        output = self.tmpdir / "fallback_negative.svg"
        path = self._render("bar", ["A", "B"], [-5.0, 10.0], title="Negatives", output_path=output)
        _assert_valid_svg(self, Path(path).read_text(encoding="utf-8"))

    def test_fallback_line_single_point(self) -> None:
        output = self.tmpdir / "fallback_point.svg"
        path = self._render("line", ["Only"], [42.0], output_path=output)
        _assert_valid_svg(self, Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
