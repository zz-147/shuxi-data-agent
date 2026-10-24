"""工具 1：inspect_schema —— 查看数仓表结构 / 行数 / 样例值."""

from __future__ import annotations

from .base import ToolContext, ToolSpec, register

PARAMETERS = {
    "type": "object",
    "properties": {
        "table": {
            "type": "string",
            "description": "表名，可选 stores / products / customers / sales；省略则返回全部表结构",
        }
    },
    "required": [],
    "additionalProperties": False,
}


def inspect_schema(args: dict, ctx: ToolContext) -> dict:
    """返回表清单；指定 table 时附带该表 3 行样例."""
    try:
        table = args.get("table")
        if table is None:
            tables = ctx.store.list_tables()
            summary = "数仓共 {count} 张表：{detail}".format(
                count=len(tables),
                detail="、".join(f"{item['name']}({item['row_count']} 行)" for item in tables),
            )
            return {"ok": True, "tables": tables, "summary": summary}

        if not isinstance(table, str) or not table.strip():
            return {"ok": False, "error": "参数 table 必须是表名字符串（如 sales），不能为空"}

        detail = ctx.store.describe_table(table.strip())
        columns = "、".join(column["name"] for column in detail["columns"])
        summary = (
            f"表 {detail['name']} 共 {detail['row_count']} 行，字段：{columns}；"
            f"已返回 {len(detail.get('sample_rows', []))} 行样例"
        )
        return {"ok": True, "tables": [detail], "summary": summary}
    except Exception as exc:  # noqa: BLE001 - 工具失败统一返回 ok=False
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


register(
    ToolSpec(
        name="inspect_schema",
        description=(
            "查看演示数仓的表结构：表名、字段名与类型、行数；传入 table 时返回该表 3 行样例值。"
            "做任何分析前先调用本工具确认字段与取值口径。"
        ),
        parameters=PARAMETERS,
        risk="low",
        handler=inspect_schema,
    )
)

__all__ = ["inspect_schema", "PARAMETERS"]
