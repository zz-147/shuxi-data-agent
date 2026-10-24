"""SQLite 演示数仓访问层：schema / 样例值 / 只读查询 / 数据画像。

护栏原则（「工具层也要有一层护栏」，不依赖上层 guard 一定被调用）：
1. 只允许单条 ``SELECT`` / ``WITH`` 语句，禁止分号追加第二条语句；
2. 关键字黑名单拦截 INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA/ATTACH/LOAD_EXTENSION 等；
3. 表名白名单 ``{stores, products, customers, sales}``；CTE 名允许（WITH 查询）；
4. 自动补 ``LIMIT``：默认 100，上限 200；
5. 连接使用 SQLite 只读 URI（``mode=ro``），从驱动层面再加一道锁。

本模块对 pandas 的依赖是**延迟导入**的，只有 ``profile_table`` / ``query_dataframe``
需要 pandas；无 pandas 时仍可做 schema 探查与普通行查询。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

TABLE_WHITELIST: Tuple[str, ...] = ("stores", "products", "customers", "sales")
TABLE_DESCRIPTIONS: Dict[str, str] = {
    "stores": "门店维度表（区域 / 城市 / 渠道 / 开业日期）",
    "products": "商品维度表（品类 / 单价 / 成本）",
    "customers": "客户维度表（含敏感字段 name / phone，用于 HITL 演示）",
    "sales": "销售事实表（订单 / 日期 / 门店 / 商品 / 客户 / 渠道 / 数量 / 金额 / 成本）",
}

DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 200
MAX_ANALYTIC_ROWS = 5000
MAX_SQL_LENGTH = 8000

_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "pragma",
    "attach",
    "detach",
    "vacuum",
    "reindex",
    "load_extension",
    "truncate",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "release",
    "analyze",
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in _FORBIDDEN_KEYWORDS) + r")\b",
    re.IGNORECASE,
)
_READONLY_HEAD_RE = re.compile(r"^\s*(?:select|with)\b", re.IGNORECASE)
_SQLITE_INTERNAL_RE = re.compile(r"\bsqlite_[a-z0-9_]*", re.IGNORECASE)
_TABLE_REF_RE = re.compile(
    r'\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.\"`\[\]]*)',
    re.IGNORECASE,
)
_CTE_NAME_RE = re.compile(
    r"(?:\bwith|,)\s+([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)


class SQLGuardError(ValueError):
    """SQL 未通过只读护栏校验时抛出."""


@dataclass
class QueryResult:
    """一次只读查询的结构化结果."""

    columns: List[str] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0


def _readonly_uri(db_path: Path) -> str:
    """构造 SQLite 只读 URI（Windows 路径需转成 file: 形式）."""
    posix = db_path.resolve().as_posix()
    return f"file:{quote(posix, safe='/:')}?mode=ro"


def _mask_string_literals(sql: str) -> str:
    """把 SQL 单引号字符串里的字符替换成 x（保持长度），用于语句/关键字检测."""
    chars = list(sql)
    in_string = False
    index = 0
    while index < len(chars):
        char = chars[index]
        if in_string:
            if char == "'":
                if index + 1 < len(chars) and chars[index + 1] == "'":
                    chars[index] = "x"
                    chars[index + 1] = "x"
                    index += 2
                    continue
                in_string = False
                index += 1
                continue
            chars[index] = "x"
        elif char == "'":
            in_string = True
        index += 1
    return "".join(chars)


def _normalize_table_token(token: str) -> str:
    cleaned = token.strip().strip('"`[]')
    if "." in cleaned:
        schema, cleaned = cleaned.split(".", 1)
        if schema.strip('"`[]').lower() not in ("main",):
            return "".join(c for c in token if c.isalnum() or c in "._")
    return cleaned.strip('"`[]').lower()


def validate_readonly_sql(sql: str) -> str:
    """校验 SQL 是单条只读查询，返回去掉尾部 ``;`` 后的语句.

    Raises:
        SQLGuardError: 非字符串 / 空 / 非 SELECT|WITH / 多语句 / 危险关键字 /
            非白名单表 / 访问 sqlite 内部表。
    """
    if not isinstance(sql, str):
        raise SQLGuardError("SQL 必须是字符串")
    text = sql.strip()
    if not text:
        raise SQLGuardError("SQL 不能为空")
    if len(text) > MAX_SQL_LENGTH:
        raise SQLGuardError(f"SQL 过长（>{MAX_SQL_LENGTH} 字符）")

    masked = _mask_string_literals(text)
    trimmed = masked.rstrip()
    if trimmed.endswith(";"):
        # 允许且仅允许一个结尾分号；同步裁剪原文
        text = text[: len(trimmed) - 1].rstrip()
        masked = masked[: len(trimmed) - 1].rstrip()
        if not text:
            raise SQLGuardError("SQL 不能为空")
    if ";" in masked:
        raise SQLGuardError("只允许单条 SQL 语句，禁止用分号拼接多条语句")
    if not _READONLY_HEAD_RE.match(masked):
        raise SQLGuardError("只允许 SELECT / WITH 只读查询")
    keyword = _FORBIDDEN_RE.search(masked)
    if keyword:
        raise SQLGuardError(f"禁止使用的 SQL 关键字：{keyword.group(0).upper()}")
    internal = _SQLITE_INTERNAL_RE.search(masked)
    if internal:
        raise SQLGuardError("禁止访问 sqlite 内部表")

    cte_names = {match.group(1).lower() for match in _CTE_NAME_RE.finditer(masked)}
    for match in _TABLE_REF_RE.finditer(masked):
        table = _normalize_table_token(match.group(1))
        if not table:
            continue
        if table in cte_names or table in TABLE_WHITELIST:
            continue
        raise SQLGuardError(f"表 {table} 不在白名单 {list(TABLE_WHITELIST)} 内")
    return text


def _enforce_limit(sql: str, limit: int) -> Tuple[str, int, bool]:
    """自动补/收紧 LIMIT，返回 ``(实际执行 SQL, 生效行数, 是否为用户显式 LIMIT)``."""
    cap = max(1, min(int(limit), MAX_ROW_LIMIT))
    matches = list(_LIMIT_RE.finditer(sql))
    if not matches:
        # 多取 1 行用于判断是否被截断
        return f"{sql} LIMIT {cap + 1}", cap, False
    last = matches[-1]
    requested = int(last.group(1))
    if requested <= cap:
        return sql, max(requested, 0), True
    executed = sql[: last.start(1)] + str(cap + 1) + sql[last.end(1) :]
    return executed, cap, True


class DemoStore:
    """demo.db 的只读访问对象；查询连接即用即关，避免跨线程共享连接."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._closed = False
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"演示数据库不存在：{self.db_path}；请先运行 `python scripts/init_demo_data.py`"
            )

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> sqlite3.Connection:
        self._require_open()
        connection = sqlite3.connect(_readonly_uri(self.db_path), uri=True, timeout=5.0)
        connection.row_factory = None
        return connection

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("DemoStore 已关闭，不能再执行查询")

    # ------------------------------------------------------------------ 元数据
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Tuple[List[str], List[List[Any]]]:
        self._require_open()
        started = time.perf_counter()
        connection = self._connect()
        try:
            cursor = connection.execute(sql, tuple(params))
            columns = [item[0] for item in (cursor.description or [])]
            rows = [list(row) for row in cursor.fetchall()]
        finally:
            connection.close()
        _ = started
        return columns, rows

    def _table_columns(self, table: str) -> List[Dict[str, str]]:
        columns, rows = self._execute(f'PRAGMA table_info("{table}")')
        _ = columns
        result: List[Dict[str, str]] = []
        for row in rows:
            result.append({"name": str(row[1]), "type": (str(row[2]) or "TEXT").upper()})
        return result

    def _row_count(self, table: str) -> int:
        _, rows = self._execute(f'SELECT COUNT(*) FROM "{table}"')
        return int(rows[0][0]) if rows else 0

    def _require_table(self, table: str) -> str:
        if not isinstance(table, str) or not table.strip():
            raise SQLGuardError("表名不能为空")
        cleaned = table.strip().lower()
        if cleaned not in TABLE_WHITELIST:
            raise SQLGuardError(f"表 {table!r} 不在白名单 {list(TABLE_WHITELIST)} 内")
        return cleaned

    def list_tables(self) -> List[dict]:
        """列出白名单表的字段、行数与用途说明."""
        tables: List[dict] = []
        for table in TABLE_WHITELIST:
            tables.append(
                {
                    "name": table,
                    "columns": self._table_columns(table),
                    "row_count": self._row_count(table),
                    "description": TABLE_DESCRIPTIONS[table],
                }
            )
        return tables

    def describe_table(self, table: str) -> dict:
        """单表结构 + 3 行样例值."""
        name = self._require_table(table)
        columns = self._table_columns(name)
        sample_columns, sample_rows = self._execute(f'SELECT * FROM "{name}" LIMIT 3')
        samples = [dict(zip(sample_columns, row)) for row in sample_rows]
        return {
            "name": name,
            "columns": columns,
            "row_count": self._row_count(name),
            "description": TABLE_DESCRIPTIONS[name],
            "sample_rows": samples,
        }

    def sample_values(self, table: str, column: str, limit: int = 5) -> List[Any]:
        """取某列去重后的样例值（用于取值域探查）."""
        name = self._require_table(table)
        valid_columns = {item["name"] for item in self._table_columns(name)}
        if column not in valid_columns:
            raise SQLGuardError(f"列 {column!r} 不存在于表 {name}（可用列：{sorted(valid_columns)}）")
        effective = max(1, min(int(limit), 50))
        _, rows = self._execute(
            f'SELECT DISTINCT "{column}" FROM "{name}" WHERE "{column}" IS NOT NULL '
            f'ORDER BY "{column}" LIMIT ?',
            (effective,),
        )
        return [row[0] for row in rows]

    # ------------------------------------------------------------------ 查询
    def _run_rows(
        self,
        sql: str,
        params: Sequence[Any],
        cap: int,
    ) -> Tuple[List[str], List[List[Any]], bool]:
        """执行已校验 SQL，最多取 cap 行；返回 ``(columns, rows, 是否还有更多)``."""
        connection = self._connect()
        try:
            cursor = connection.execute(sql, tuple(params))
            columns = [item[0] for item in (cursor.description or [])]
            rows = [list(row) for row in cursor.fetchmany(cap + 1)]
        except sqlite3.Error as error:
            raise SQLGuardError(f"查询执行失败：{error}") from error
        finally:
            connection.close()
        truncated = len(rows) > cap
        if truncated:
            rows = rows[:cap]
        return columns, rows, truncated

    def run_readonly_query(self, sql: str, limit: int = DEFAULT_ROW_LIMIT) -> QueryResult:
        """执行单条只读 SQL，自动施加行数上限（默认 100，最大 200）."""
        started = time.perf_counter()
        validated = validate_readonly_sql(sql)
        executed_sql, effective_limit, user_limited = _enforce_limit(validated, limit)
        if user_limited:
            # 用户显式给了 LIMIT：返回行数达到该限制即认为可能被截断
            columns, rows, _ = self._run_rows(executed_sql, (), max(effective_limit, 0))
            truncated = effective_limit > 0 and len(rows) >= effective_limit
        else:
            # 自动补的 LIMIT 会多查 1 行，用于准确判断是否截断
            columns, rows, _ = self._run_rows(executed_sql, (), effective_limit + 1)
            truncated = len(rows) > effective_limit
            if truncated:
                rows = rows[:effective_limit]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    def execute_readonly(
        self,
        sql: str,
        params: Sequence[Any] = (),
        limit: int = MAX_ANALYTIC_ROWS,
    ) -> QueryResult:
        """工具内部聚合查询用：走同一套只读护栏，但允许更大的行数上限（默认 5000）.

        与 :meth:`run_readonly_query` 的区别：不自动补 LIMIT（聚合查询本身行数就小），
        行数上限只作为安全阀。
        """
        started = time.perf_counter()
        validated = validate_readonly_sql(sql)
        cap = max(1, int(limit))
        columns, rows, truncated = self._run_rows(validated, params, cap)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    def query_dataframe(self, sql: str, params: Sequence[Any] = (), max_rows: Optional[int] = None):
        """只读 SQL → pandas.DataFrame（**可选增强**，供画像/外部复用）.

        pandas 为延迟导入：没装 pandas 时核心查询（run_readonly_query /
        execute_readonly / profile_table）仍然可用，本方法会抛 RuntimeError 提示安装依赖。
        """
        validated = validate_readonly_sql(sql)
        try:
            import pandas as pd  # 延迟导入，避免无 pandas 环境下核心功能不可用
        except ImportError as error:  # pragma: no cover - 依赖缺失时的显式提示
            raise RuntimeError("query_dataframe 需要 pandas，请先安装依赖（pip install -r requirements.txt）") from error
        connection = self._connect()
        try:
            frame = pd.read_sql_query(validated, connection, params=tuple(params))
        except sqlite3.Error as error:
            raise SQLGuardError(f"查询执行失败：{error}") from error
        finally:
            connection.close()
        if max_rows is not None and len(frame) > max_rows:
            frame = frame.iloc[:max_rows].copy()
        return frame

    def profile_table(self, table: str) -> dict:
        """数据画像：行数、各列空值率、去重值数、数值列 min/max/mean.

        **零依赖实现**：直接用 sqlite3 聚合，不需要 pandas。
        """
        name = self._require_table(table)
        row_count = self._row_count(name)
        profiles: List[dict] = []
        numeric_types = ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")
        for column in self._table_columns(name):
            column_name = column["name"]
            _, aggregate_rows = self._execute(
                f'SELECT SUM(CASE WHEN "{column_name}" IS NULL THEN 1 ELSE 0 END), '
                f'COUNT(DISTINCT "{column_name}") FROM "{name}"'
            )
            null_count = int(aggregate_rows[0][0] or 0) if aggregate_rows else 0
            distinct_count = int(aggregate_rows[0][1] or 0) if aggregate_rows else 0
            profile: Dict[str, Any] = {
                "name": column_name,
                "type": column["type"],
                "null_count": null_count,
                "null_rate": round(null_count / row_count, 4) if row_count else 0.0,
                "distinct_count": distinct_count,
            }
            if any(marker in column["type"].upper() for marker in numeric_types):
                _, stats_rows = self._execute(
                    f'SELECT MIN("{column_name}"), MAX("{column_name}"), AVG("{column_name}") '
                    f'FROM "{name}"'
                )
                if stats_rows:
                    minimum, maximum, mean = stats_rows[0]
                    if minimum is not None:
                        profile["min"] = round(float(minimum), 4)
                    if maximum is not None:
                        profile["max"] = round(float(maximum), 4)
                    if mean is not None:
                        profile["mean"] = round(float(mean), 4)
            profiles.append(profile)
        return {
            "table": name,
            "row_count": row_count,
            "columns": profiles,
        }

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        """标记关闭（连接即用即关，这里只做幂等的状态切换）."""
        self._closed = True

    def __enter__(self) -> "DemoStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


__all__ = [
    "DemoStore",
    "QueryResult",
    "SQLGuardError",
    "validate_readonly_sql",
    "TABLE_WHITELIST",
    "DEFAULT_ROW_LIMIT",
    "MAX_ROW_LIMIT",
    "MAX_ANALYTIC_ROWS",
]
