"""分析报告生成：把 Agent 产出的分节内容渲染成 Markdown 或自包含 HTML。

约定：
1. **零外部依赖**，只用标准库，保证在 Streamlit / CLI / 单测里都能调用；
2. HTML 产物完全自包含（样式内联在 ``<style>`` 中，不引用任何 CDN/字体/图片），
   因此既可以被 Streamlit ``st.components.v1.html`` 直接渲染，也可以单独用浏览器打开；
3. Markdown 产物是标准 Markdown，可直接被 ``st.markdown`` 渲染或粘贴到 GitHub。
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

REPORT_FORMATS = ("markdown", "html")
_EXTENSIONS = {"markdown": "md", "html": "html"}
_DEFAULT_TITLE = "数析分析报告"
_CSS = """
:root {
  --ink: #1f2430;
  --muted: #6b7280;
  --line: #e5e7eb;
  --brand: #2f6fed;
  --bg: #f6f8fb;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 32px 16px;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  line-height: 1.7;
  font-size: 15px;
}
.report {
  max-width: 880px;
  margin: 0 auto;
  background: #ffffff;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 36px 44px 28px;
  box-shadow: 0 8px 28px rgba(31, 36, 48, 0.06);
}
header { border-bottom: 2px solid var(--brand); padding-bottom: 16px; margin-bottom: 8px; }
header h1 { margin: 0 0 8px; font-size: 26px; line-height: 1.35; }
.meta { margin: 0; color: var(--muted); font-size: 13px; }
section { padding: 18px 0 6px; border-bottom: 1px dashed var(--line); }
section:last-of-type { border-bottom: none; }
section h2 {
  margin: 0 0 12px;
  padding-left: 10px;
  border-left: 4px solid var(--brand);
  font-size: 19px;
  line-height: 1.4;
}
h3, h4, h5, h6 { margin: 16px 0 8px; font-size: 16px; }
p { margin: 8px 0; }
ul, ol { margin: 8px 0; padding-left: 24px; }
li { margin: 4px 0; }
code {
  background: #f1f3f7;
  border-radius: 4px;
  padding: 1px 5px;
  font-family: Consolas, "Courier New", monospace;
  font-size: 13px;
}
pre {
  background: #1f2430;
  color: #f6f8fb;
  border-radius: 8px;
  padding: 14px 16px;
  overflow-x: auto;
}
pre code { background: transparent; color: inherit; padding: 0; }
blockquote {
  margin: 10px 0;
  padding: 8px 14px;
  color: var(--muted);
  background: #f8fafc;
  border-left: 3px solid var(--line);
}
a { color: var(--brand); }
table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }
th, td { border: 1px solid var(--line); padding: 7px 10px; text-align: left; }
th { background: #f8fafc; }
footer { margin-top: 26px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
""".strip()


def _repo_root() -> Path:
    # src/shuxi/report.py -> 仓库根目录
    return Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_output_path(fmt: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    extension = _EXTENSIONS[fmt]
    return _repo_root() / "artifacts" / f"report_{stamp}_{os.getpid()}.{extension}"


def _normalize_format(fmt: str) -> str:
    value = str(fmt or "markdown").strip().lower()
    if value in {"markdown", "md"}:
        return "markdown"
    if value in {"html", "htm"}:
        return "html"
    raise ValueError(f"不支持的报告格式: {fmt!r}（可选 markdown / html）")


def _normalize_sections(sections: Sequence[Any] | None) -> list[dict[str, str]]:
    """把 [{"heading","body"}] 统一成字符串字段；容忍字符串/多级内容。"""
    if sections is None:
        return []
    if isinstance(sections, (str, dict)):
        sections = [sections]

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(sections, start=1):
        if isinstance(item, str):
            heading, body = f"章节 {index}", item
        elif isinstance(item, dict):
            heading = item.get("heading") or item.get("title") or f"章节 {index}"
            body = item.get("body")
            if body is None:
                body = item.get("content", "")
        else:
            raise TypeError(
                f"sections[{index - 1}] 必须是 dict 或 str，实际是 {type(item).__name__}"
            )
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, indent=2)
        normalized.append(
            {
                "heading": str(heading).strip() or f"章节 {index}",
                "body": "" if body is None else str(body),
            }
        )
    return normalized


# ---------------------------------------------------------------- Markdown


def _render_markdown(title: str, sections: list[dict[str, str]], generated_at: str) -> str:
    lines: list[str] = [f"# {title}", "", f"> 生成时间：{generated_at} · 由「数析」智能数据分析平台生成", ""]
    if not sections:
        lines += ["_本报告暂无章节内容。_", ""]
    for section in sections:
        lines += [f"## {section['heading']}", "", section["body"].strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- HTML


def _inline_markdown(text: str) -> str:
    """极简行内 Markdown -> HTML（转义后再替换，避免注入）。"""
    escaped = html_lib.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\(((?:https?://|/)[^)\s]+)\)",
        r'<a href="\2" rel="noopener noreferrer">\1</a>',
        escaped,
    )
    return escaped


def _body_to_html(body: str) -> str:
    """把章节正文（Markdown 子集）转成 HTML 片段：标题/列表/代码块/段落/引用。"""
    lines = str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + "<br />".join(paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items and list_tag:
            out.append(f"<{list_tag}>" + "".join(f"<li>{item}</li>" for item in list_items) + f"</{list_tag}>")
        list_items.clear()
        list_tag = None

    for raw_line in lines:
        stripped = raw_line.strip()

        if in_code:
            if stripped.startswith("```"):
                in_code = False
                out.append("<pre><code>" + html_lib.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines.clear()
            else:
                code_lines.append(raw_line)
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            in_code = True
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = min(len(heading.group(1)) + 2, 6)  # 章节内标题从 h3 起，避免与 h2 抢层级
            out.append(f"<h{level}>{_inline_markdown(heading.group(2).strip())}</h{level}>")
            continue

        unordered = re.match(r"^[-*+]\s+(.*)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if unordered or ordered:
            flush_paragraph()
            item = (unordered or ordered).group(1)
            wanted = "ul" if unordered else "ol"
            if list_tag != wanted:
                flush_list()
                list_tag = wanted
            list_items.append(_inline_markdown(item))
            continue

        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            flush_paragraph()
            flush_list()
            out.append(f"<blockquote>{_inline_markdown(quote.group(1))}</blockquote>")
            continue

        paragraph.append(_inline_markdown(stripped))

    if in_code:
        out.append("<pre><code>" + html_lib.escape("\n".join(code_lines)) + "</code></pre>")
    flush_paragraph()
    flush_list()
    return "\n".join(out)


def _render_html(title: str, sections: list[dict[str, str]], generated_at: str) -> str:
    section_html: list[str] = []
    if not sections:
        section_html.append("<section><p><em>本报告暂无章节内容。</em></p></section>")
    for section in sections:
        section_html.append(
            "<section>\n"
            f"  <h2>{html_lib.escape(section['heading'])}</h2>\n"
            f"  {_body_to_html(section['body'])}\n"
            "</section>"
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"  <title>{html_lib.escape(title)} - 数析分析报告</title>\n"
        f"  <style>\n{_CSS}\n  </style>\n"
        "</head>\n"
        "<body>\n"
        '  <main class="report">\n'
        "    <header>\n"
        f"      <h1>{html_lib.escape(title)}</h1>\n"
        f'      <p class="meta">生成时间：{html_lib.escape(generated_at)} · 由「数析」智能数据分析平台生成</p>\n'
        "    </header>\n"
        + "\n".join(section_html)
        + "\n"
        f'    <footer>共 {len(sections)} 个章节 · 本报告由工具结果自动汇总生成，请结合统计口径核对后使用。</footer>\n'
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )


# ---------------------------------------------------------------- 公共 API


def build_report(
    sections: list[dict],
    title: str,
    fmt: str = "markdown",
    output_path: Path | None = None,
) -> Path:
    """生成分析报告，返回写入的文件路径。

    参数
    ----
    sections: ``[{"heading": str, "body": str}, ...]``；body 允许 Markdown 子集
    title: 报告标题（为空时使用默认标题）
    fmt: ``markdown``（别名 ``md``）或 ``html``
    output_path: 输出路径；缺省写到 ``<repo>/artifacts/report_<时间戳>.<md|html>``

    异常
    ----
    ValueError: 不支持的格式；TypeError: sections 结构非法。
    """
    normalized_fmt = _normalize_format(fmt)
    normalized_sections = _normalize_sections(sections)
    title_text = str(title).strip() if title is not None else ""
    if not title_text:
        title_text = _DEFAULT_TITLE
    title_text = " ".join(title_text.split())

    generated_at = _now_iso()
    if normalized_fmt == "markdown":
        content = _render_markdown(title_text, normalized_sections, generated_at)
    else:
        content = _render_html(title_text, normalized_sections, generated_at)

    target = Path(output_path) if output_path is not None else _default_output_path(normalized_fmt)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
