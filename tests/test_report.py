# -*- coding: utf-8 -*-
"""report.build_report 单测：Markdown / 自包含 HTML 内容与结构。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# 支持 `python -m unittest discover -s tests`（discover 不会执行 tests/__init__.py）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shuxi.report import REPORT_FORMATS, build_report  # noqa: E402

TITLE = "2025 年 9 月华东区销售分析报告"
SECTIONS = [
    {
        "heading": "核心结论",
        "body": "2025 年 9 月华东区销售额为 **1,234,567 元**，环比增长 8.6%。\n\n"
        "口径：sales 表 amount 求和，order_date 在 2025-09-01~2025-09-30，region=华东。",
    },
    {
        "heading": "变化原因",
        "body": "- 品类 A 贡献 +6.2 个百分点\n- 品类 B 贡献 +2.4 个百分点\n- 品类 C 拖累 -1.1 个百分点",
    },
    {"heading": "后续建议", "body": "建议下钻门店维度，重点复盘品类 A 的增长来源。"},
]


class BuildReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------------------------------------------------------------- Markdown

    def test_markdown_report_contains_title_and_headings(self) -> None:
        output = self.tmpdir / "report.md"
        path = build_report(SECTIONS, title=TITLE, fmt="markdown", output_path=output)
        self.assertEqual(Path(path), output)
        self.assertTrue(output.exists())
        content = output.read_text(encoding="utf-8")
        self.assertIn(TITLE, content)
        for section in SECTIONS:
            self.assertIn(section["heading"], content)
            self.assertIn(section["body"].splitlines()[0], content)
        # 一级标题 + 每节二级标题
        self.assertTrue(content.startswith("# "))
        self.assertEqual(content.count("\n## "), len(SECTIONS))

    def test_markdown_alias_md(self) -> None:
        output = self.tmpdir / "report_alias.md"
        path = build_report(SECTIONS, title=TITLE, fmt="md", output_path=output)
        self.assertTrue(Path(path).exists())
        self.assertIn(TITLE, Path(path).read_text(encoding="utf-8"))

    def test_default_format_is_markdown(self) -> None:
        output = self.tmpdir / "report_default.md"
        path = build_report(SECTIONS, title=TITLE, output_path=output)
        self.assertEqual(Path(path).suffix, ".md")

    def test_empty_title_falls_back_to_default(self) -> None:
        output = self.tmpdir / "report_untitled.md"
        path = build_report(SECTIONS, title="", output_path=output)
        self.assertIn("数析分析报告", Path(path).read_text(encoding="utf-8"))

    def test_no_sections_is_still_valid(self) -> None:
        output = self.tmpdir / "report_empty.md"
        path = build_report([], title="空报告", output_path=output)
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("空报告", content)
        self.assertIn("暂无章节", content)

    def test_output_parent_dirs_are_created(self) -> None:
        output = self.tmpdir / "nested" / "reports" / "report.md"
        path = build_report(SECTIONS, title=TITLE, output_path=output)
        self.assertTrue(Path(path).exists())

    def test_default_output_path_used_when_missing(self) -> None:
        path = build_report(SECTIONS, title=TITLE, fmt="markdown")
        try:
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).suffix.lower(), ".md")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_sections_accept_strings_and_extra_keys(self) -> None:
        output = self.tmpdir / "report_loose.md"
        sections = ["纯文本章节", {"title": "别名标题", "content": "用 content 字段"}]
        path = build_report(sections, title="宽松输入", output_path=output)
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("纯文本章节", content)
        self.assertIn("别名标题", content)
        self.assertIn("用 content 字段", content)

    def test_invalid_format_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            build_report(SECTIONS, title=TITLE, fmt="pdf", output_path=self.tmpdir / "x.pdf")

    def test_invalid_sections_type_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            build_report([42], title=TITLE, output_path=self.tmpdir / "x.md")  # type: ignore[list-item]

    # ---------------------------------------------------------------- HTML

    def test_html_report_is_self_contained(self) -> None:
        output = self.tmpdir / "report.html"
        path = build_report(SECTIONS, title=TITLE, fmt="html", output_path=output)
        content = Path(path).read_text(encoding="utf-8")

        self.assertIn("<!DOCTYPE html>", content)
        self.assertIn("<html", content)
        self.assertIn("<style>", content, "HTML 报告必须内联 CSS")
        self.assertIn(TITLE, content)
        for section in SECTIONS:
            self.assertIn(section["heading"], content)

        # 自包含：不能引用外部样式/脚本/字体/图片
        lower = content.lower()
        self.assertNotIn("<link", lower)
        self.assertNotIn("<script", lower)
        self.assertNotIn("http://", lower)
        self.assertNotIn("https://", lower)
        self.assertNotIn("cdn.", lower)

    def test_html_renders_markdown_lite(self) -> None:
        output = self.tmpdir / "report_lite.html"
        build_report(SECTIONS, title=TITLE, fmt="html", output_path=output)
        content = Path(output).read_text(encoding="utf-8")
        self.assertIn("<strong>", content)  # **1,234,567 元**
        self.assertIn("<ul>", content)  # 无序列表
        self.assertIn("<li>", content)
        self.assertIn("<h2>核心结论</h2>", content)

    def test_html_escapes_unsafe_content(self) -> None:
        output = self.tmpdir / "report_escape.html"
        sections = [{"heading": "XSS <script>", "body": '<script>alert("x")</script> & **bold**'}]
        build_report(sections, title="Safe <Report>", fmt="html", output_path=output)
        content = Path(output).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert", content)
        self.assertIn("&lt;script&gt;", content)
        self.assertIn("&amp;", content)
        self.assertIn("Safe &lt;Report&gt;", content)
        self.assertIn("<strong>bold</strong>", content)

    def test_html_format_alias_htm(self) -> None:
        output = self.tmpdir / "report_htm.html"
        path = build_report(SECTIONS, title=TITLE, fmt="htm", output_path=output)
        self.assertIn("<!DOCTYPE html>", Path(path).read_text(encoding="utf-8"))

    def test_html_code_fence_is_escaped_block(self) -> None:
        output = self.tmpdir / "report_code.html"
        sections = [{"heading": "查询", "body": "```sql\nSELECT 1 < 2;\n```"}]
        build_report(sections, title="代码块", fmt="html", output_path=output)
        content = Path(output).read_text(encoding="utf-8")
        self.assertIn("<pre><code>", content)
        self.assertIn("SELECT 1 &lt; 2;", content)

    def test_all_supported_formats_declared(self) -> None:
        self.assertEqual(set(REPORT_FORMATS), {"markdown", "html"})

    def test_html_bytes_are_utf8_and_reasonable(self) -> None:
        output = self.tmpdir / "report_bytes.html"
        build_report(SECTIONS, title=TITLE, fmt="html", output_path=output)
        raw = output.read_bytes()
        self.assertGreater(len(raw), 1500, "HTML 报告过小，可能缺样式/正文")
        raw.decode("utf-8")  # 不抛异常即 UTF-8 合法


if __name__ == "__main__":
    unittest.main()
