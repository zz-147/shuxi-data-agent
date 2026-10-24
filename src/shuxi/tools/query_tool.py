"""工具 2：query_data —— 受控只读 SQL 查询（结果限行、字段限长）."""

from __future__ import annotations

from .base import ToolContext, ToolSpec, register

MAX_RESULT_ROWS = 100
MAX_CELL_CHARS = 200

PARAMETERS = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "单条只读 SELECT / WITH 查询；只允许表 stores/products/customers/sales，"
                "禁止 INSERT/UPDATE/DELETE/DROP 等写操作。"
            ),
        },
        "limit": {
            "type": "integer",
            "description": "最多返回行数，默认 100，最大 100",
            "minimum": 1,
            "maximum": MAX_RESULT_ROWS,
        },
    },
    "required": ["sql"],
    "additionalProperties": False,
}


def _shrink_cell(value):
    """单行字段截断到 200 字符，控制 observation 体积."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    if len(text) <= MAX_CELL_CHARS:
        return text
    return text[:MAX_CELL_CHARS] + "…"


def query_data(args: dict, ctx: ToolContext) -> dict:
    """执行受控只读 SQL，返回列名 / 行数据 / 是否截断."""
    try:
        sql = args.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return {"ok": False, "error": "参数 sql 必须是非空字符串"}

        settings_limit = int(getattr(ctx.settings, "query_row_limit", MAX_RESULT_ROWS) or MAX_RESULT_ROWS)
        effective = min(MAX_RESULT_ROWS, settings_limit)
        raw_limit = args.get("limit", effective)
        try:
            raw_limit = int(raw_limit)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"参数 limit 必须是整数，实际为 {args.get('limit')!r}"}
        effective = max(1, min(raw_limit, effective))

        result = ctx.store.run_readonly_query(sql.strip(), limit=effective)
        rows = [[_shrink_cell(cell) for cell in row] for row in result.rows[:MAX_RESULT_ROWS]]
        truncated = bool(result.truncated) or len(result.rows) > MAX_RESULT_ROWS
        summary = f"只读查询成功，返回 {len(rows)} 行 × {len(result.columns)} 列"
        if truncated:
            summary += "（已按行数上限截断）"
        return {
            "ok": True,
            "columns": list(result.columns),
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": result.elapsed_ms,
            "sql": sql.strip(),
            "summary": summary,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


register(
    ToolSpec(
        name="query_data",
        description=(
            "执行单条只读 SQL 查询（SELECT / WITH），用于精确取数、核对明细。"
            "只能访问 stores/products/customers/sales 四张白名单表；"
            "查询含 name/phone 等敏感字段且返回行数 > 20 时会被升级为高风险并触发人工审批。"
        ),
        parameters=PARAMETERS,
        risk="medium",
        handler=query_data,
    )
)

__all__ = ["query_data", "PARAMETERS", "MAX_RESULT_ROWS", "MAX_CELL_CHARS"]
