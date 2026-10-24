"""工具 6：export_report —— 把分析结论导出为 Markdown / HTML 报告.

实际渲染交给 ``shuxi.report.build_report``（py-ui 模块）；
本工具负责参数校验、artifacts 路径拼装和返回值归一化。
``include_all_rows=true`` 属于 high risk，会由 guard 在调用前拦截并要求人工审批。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from .base import ToolContext, ToolSpec, ensure_artifacts_dir, register, safe_trace_id

FORMATS = ("markdown", "html")
_EXTENSIONS = {"markdown": "md", "html": "html"}
DEFAULT_FORMAT = "markdown"

PARAMETERS = {
    "type": "object",
    "properties": {
        "format": {
            "type": "string",
            "enum": list(FORMATS),
            "description": "导出格式：markdown（.md）或 html（自包含 .html），默认 markdown",
        },
        "title": {"type": "string", "description": "报告标题"},
        "sections": {
            "type": "array",
            "description": "报告小节列表",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["heading", "body"],
                "additionalProperties": False,
            },
        },
        "include_all_rows": {
            "type": "boolean",
            "description": "是否包含全量明细数据；true 会触发人工审批（high risk）",
        },
    },
    "required": ["title", "sections"],
    "additionalProperties": False,
}


def _load_build_report():
    """延迟导入报告构造函数：py-ui 的 report.py 未就绪时也能 import 本模块.

    测试可用 ``monkeypatch.setattr(report_tool, "_load_build_report", ...)`` 替换。
    """
    from ..report import build_report

    return build_report


def _normalize_sections(raw) -> List[dict]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("sections 必须是非空数组，元素形如 {\"heading\": ..., \"body\": ...}")
    sections: List[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"sections[{index}] 必须是对象")
        heading = item.get("heading", "")
        if not isinstance(heading, str) or not heading.strip():
            raise ValueError(f"sections[{index}].heading 必须是非空字符串")
        if "body" not in item:
            raise ValueError(f"sections[{index}].body 必须提供（可以是空字符串）")
        body = item["body"]
        if not isinstance(body, str):
            raise ValueError(f"sections[{index}].body 必须是字符串")
        sections.append({"heading": heading.strip(), "body": body})
    return sections


def _normalize_format(raw) -> str:
    normalized = str(raw or DEFAULT_FORMAT).strip().lower()
    aliases = {"md": "markdown", "htm": "html"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in FORMATS:
        raise ValueError(f"不支持的 format：{raw!r}，可选 {list(FORMATS)}")
    return normalized


def export_report(args: dict, ctx: ToolContext) -> dict:
    """生成并保存 Markdown / HTML 分析报告，返回路径与字节数."""
    try:
        fmt = _normalize_format(args.get("format"))
        title = args.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title 必须是非空字符串")
        sections = _normalize_sections(args.get("sections"))
        include_all_rows = bool(args.get("include_all_rows", False))

        output_path = ensure_artifacts_dir(ctx) / f"report_{safe_trace_id(ctx)}.{_EXTENSIONS[fmt]}"
        build_report = _load_build_report()
        written = build_report(
            sections=sections,
            title=title.strip(),
            fmt=fmt,
            output_path=output_path,
        )
        path = Path(written) if written is not None else output_path
        size = path.stat().st_size if path.exists() else 0
        summary = f"已导出 {fmt} 报告《{title.strip()}》：{len(sections)} 个小节，{size} 字节"
        if include_all_rows:
            summary += "（含全量明细，已通过人工审批）"
        return {
            "ok": True,
            "path": str(path),
            "format": fmt,
            "bytes": size,
            "sections": len(sections),
            "include_all_rows": include_all_rows,
            "summary": summary,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


register(
    ToolSpec(
        name="export_report",
        description=(
            "把分析结论导出为 Markdown（.md）或自包含 HTML（.html）报告，写入 "
            "artifacts/report_<trace_id>.<md|html>。参数含 title 与 sections 小节列表；"
            "include_all_rows=true（全量导出）属于高风险操作，会触发人工审批。"
        ),
        parameters=PARAMETERS,
        risk="medium",
        handler=export_report,
    )
)

__all__ = ["export_report", "PARAMETERS", "FORMATS"]
