# -*- coding: utf-8 -*-
"""tools 单测：6 个工具 happy path + 失败路径 + store 只读护栏.

核心链路（schema/query/metrics/anomaly/store）**零第三方依赖**，只装标准库即可跑；
chart / report 通过 monkeypatch 替换渲染函数（不依赖 matplotlib / py-ui 文件），
pandas 相关用例用 ``skipUnless`` 降级。
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shuxi.config import Settings  # noqa: E402
from shuxi.store import (  # noqa: E402
    DEFAULT_ROW_LIMIT,
    MAX_ROW_LIMIT,
    DemoStore,
    QueryResult,
    SQLGuardError,
)
from shuxi.tools import ToolContext, execute_tool, get_tools, tool_schemas  # noqa: E402
from shuxi.tools import chart_tool, report_tool  # noqa: E402
from shuxi.tools.base import TOOL_ORDER, ToolSpec, safe_trace_id  # noqa: E402

DB_PATH = ROOT / "data" / "demo.db"
PANDAS_AVAILABLE = importlib.util.find_spec("pandas") is not None
_DB_READY = False


def ensure_demo_db() -> None:
    """demo.db 已提交到仓库；万一缺失则调用生成脚本（幂等、seed 固定）."""
    global _DB_READY
    if _DB_READY:
        return
    if not DB_PATH.exists():  # pragma: no cover - 仓库已提交 data/demo.db
        module_spec = importlib.util.spec_from_file_location(
            "shuxi_init_demo_data", ROOT / "scripts" / "init_demo_data.py"
        )
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        module.init_demo_data()
    _DB_READY = True


def make_settings(tmp: Path, **overrides) -> Settings:
    values = {
        "provider": "mock",
        "root_dir": tmp,
        "db_path": DB_PATH,
        "log_dir": tmp / "logs",
        "artifacts_dir": tmp / "artifacts",
    }
    values.update(overrides)
    return Settings.from_env(**values).ensure_dirs()


class ToolTestBase(unittest.TestCase):
    """每个用例独立 tmp 产物目录 + 独立只读 store 连接."""

    @classmethod
    def setUpClass(cls) -> None:
        ensure_demo_db()
        if not DB_PATH.exists():  # pragma: no cover
            raise unittest.SkipTest(f"演示数据库不存在：{DB_PATH}")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = make_settings(self.tmp)
        self.store = DemoStore(DB_PATH)
        self.addCleanup(self.store.close)
        self.trace = SimpleNamespace(trace_id="testtrace123")
        self.ctx = ToolContext(
            store=self.store,
            settings=self.settings,
            trace=self.trace,
            artifacts_dir=self.settings.artifacts_dir,
        )

    def scalar(self, sql: str, params=()):
        result = self.store.execute_readonly(sql, params, limit=1)
        return result.rows[0][0]

    def call(self, name: str, args: dict) -> dict:
        return execute_tool(name, args, self.ctx)


class TestToolRegistry(ToolTestBase):
    def test_tool_order_is_fixed(self):
        self.assertEqual(tuple(get_tools().keys()), TOOL_ORDER)
        self.assertEqual(len(get_tools()), 6)

    def test_tool_schemas_shape(self):
        schemas = tool_schemas()
        self.assertEqual([item["name"] for item in schemas], list(TOOL_ORDER))
        for schema in schemas:
            self.assertIn("description", schema)
            parameters = schema["parameters"]
            self.assertEqual(parameters["type"], "object")
            self.assertIn("properties", parameters)
            self.assertIsInstance(schema["description"], str)
            self.assertTrue(schema["description"])
        self.assertEqual(set(schemas[1]["parameters"]["properties"]), {"sql", "limit"})
        self.assertEqual(schemas[1]["parameters"]["required"], ["sql"])

    def test_specs_expose_risk_levels(self):
        levels = {name: spec.risk for name, spec in get_tools().items()}
        self.assertEqual(levels["query_data"], "medium")
        self.assertEqual(levels["export_report"], "medium")
        for name in ("inspect_schema", "compute_metrics", "detect_anomaly_drivers", "generate_chart"):
            self.assertEqual(levels[name], "low")

    def test_execute_unknown_tool(self):
        result = self.call("drop_database", {})
        self.assertFalse(result["ok"])
        self.assertIn("未知工具", result["error"])

    def test_execute_non_dict_args(self):
        result = self.call("query_data", "SELECT 1")  # type: ignore[arg-type]
        self.assertFalse(result["ok"])
        self.assertIn("JSON 对象", result["error"])

    def test_execute_wraps_handler_exception(self):
        result = self.call("query_data", {"sql": "DELETE FROM sales"})
        self.assertFalse(result["ok"])
        self.assertIn("只允许", result["error"])

    def test_execute_requires_ok_field(self):
        fake = ToolSpec(
            name="fake_tool",
            description="测试用",
            parameters={"type": "object", "properties": {}},
            risk="low",
            handler=lambda args, ctx: {"summary": "no ok"},
        )
        with mock.patch.dict("shuxi.tools.base._REGISTRY", {"fake_tool": fake}):
            result = execute_tool("fake_tool", {}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertIn("ok", result["error"])

    def test_safe_trace_id_sanitizes(self):
        ctx = SimpleNamespace(trace=SimpleNamespace(trace_id="a/b c:中文"))
        self.assertEqual(safe_trace_id(ctx), "a_b_c")
        self.assertEqual(safe_trace_id(SimpleNamespace(trace=None)), "notrace")

    def test_all_error_results_are_json_serializable(self):
        results = [
            self.call("inspect_schema", {"table": "nope"}),
            self.call("query_data", {"sql": ""}),
            self.call("compute_metrics", {}),
        ]
        for result in results:
            self.assertFalse(result["ok"])
            json.dumps(result, ensure_ascii=False)


class TestInspectSchema(ToolTestBase):
    def test_inspect_all_tables(self):
        result = self.call("inspect_schema", {})
        self.assertTrue(result["ok"])
        names = [item["name"] for item in result["tables"]]
        self.assertEqual(names, ["stores", "products", "customers", "sales"])
        counts = {item["name"]: item["row_count"] for item in result["tables"]}
        self.assertEqual(counts, {"stores": 30, "products": 40, "customers": 800, "sales": 4000})
        customers = next(item for item in result["tables"] if item["name"] == "customers")
        column_names = [column["name"] for column in customers["columns"]]
        self.assertIn("name", column_names)
        self.assertIn("phone", column_names)
        self.assertIn("4 张表", result["summary"])

    def test_inspect_single_table_has_samples(self):
        result = self.call("inspect_schema", {"table": "sales"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["tables"]), 1)
        detail = result["tables"][0]
        self.assertEqual(detail["name"], "sales")
        self.assertEqual(len(detail["sample_rows"]), 3)
        self.assertEqual(len(detail["columns"]), 9)
        self.assertIn("sales", result["summary"])

    def test_inspect_unknown_table_fails(self):
        result = self.call("inspect_schema", {"table": "employees"})
        self.assertFalse(result["ok"])
        self.assertIn("白名单", result["error"])

    def test_inspect_bad_table_type_fails(self):
        result = self.call("inspect_schema", {"table": 123})
        self.assertFalse(result["ok"])


class TestQueryData(ToolTestBase):
    def test_query_happy_path_and_json_safe(self):
        result = self.call(
            "query_data",
            {"sql": "SELECT store_id, region FROM stores ORDER BY store_id", "limit": 5},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["columns"], ["store_id", "region"])
        self.assertEqual(result["row_count"], 5)
        self.assertEqual(len(result["rows"]), 5)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["sql"], "SELECT store_id, region FROM stores ORDER BY store_id")
        self.assertIn("5 行", result["summary"])
        json.dumps(result, ensure_ascii=False)

    def test_query_default_limit_is_100(self):
        result = self.call("query_data", {"sql": "SELECT * FROM sales"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["row_count"], 100)
        self.assertTrue(result["truncated"])

    def test_query_limit_clamped_to_100(self):
        result = self.call("query_data", {"sql": "SELECT * FROM sales", "limit": 500})
        self.assertTrue(result["ok"])
        self.assertEqual(result["row_count"], 100)
        self.assertLessEqual(result["row_count"], 100)

    def test_query_cells_are_truncated_to_200_chars(self):
        sql = "SELECT '" + "x" * 300 + "' AS long_text"
        result = self.call("query_data", {"sql": sql})
        self.assertTrue(result["ok"])
        cell = result["rows"][0][0]
        self.assertEqual(len(cell), 201)
        self.assertTrue(cell.endswith("…"))

    def test_query_rejects_writes(self):
        for sql in (
            "DELETE FROM sales",
            "UPDATE sales SET amount = 0",
            "DROP TABLE sales",
            "INSERT INTO stores VALUES ('x')",
        ):
            result = self.call("query_data", {"sql": sql})
            self.assertFalse(result["ok"], sql)

    def test_query_rejects_unknown_table(self):
        result = self.call("query_data", {"sql": "SELECT * FROM employees"})
        self.assertFalse(result["ok"])
        self.assertIn("白名单", result["error"])

    def test_query_rejects_stacked_statements(self):
        result = self.call("query_data", {"sql": "SELECT * FROM sales; DROP TABLE sales"})
        self.assertFalse(result["ok"])

    def test_query_rejects_sqlite_internal_tables(self):
        result = self.call("query_data", {"sql": "SELECT * FROM sqlite_master"})
        self.assertFalse(result["ok"])

    def test_query_bad_inputs(self):
        for args in ({}, {"sql": ""}, {"sql": 123}, {"sql": "SELECT 1", "limit": "abc"}):
            result = self.call("query_data", args)
            self.assertFalse(result["ok"], args)
            self.assertIn("error", result)

    def test_query_accepts_trailing_semicolon_and_cte(self):
        semicolon = self.call("query_data", {"sql": "SELECT COUNT(*) AS n FROM sales;"})
        self.assertTrue(semicolon["ok"])
        cte = self.call(
            "query_data",
            {
                "sql": (
                    "WITH t AS (SELECT region, COUNT(*) AS n FROM stores GROUP BY region) "
                    "SELECT * FROM t ORDER BY n DESC"
                ),
                "limit": 10,
            },
        )
        self.assertTrue(cte["ok"])
        self.assertEqual(cte["columns"], ["region", "n"])

    def test_query_semicolon_inside_string_literal_is_allowed(self):
        result = self.call(
            "query_data", {"sql": "SELECT COUNT(*) AS n FROM stores WHERE region = '华东'"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"][0][0], 10)


class TestComputeMetrics(ToolTestBase):
    def expected_total(self, start: str, end: str, extra: str = "", params=()) -> float:
        sql = (
            "SELECT ROUND(SUM(s.amount), 2) FROM sales s "
            "JOIN stores st ON s.store_id = st.store_id "
            "JOIN products p ON s.product_id = p.product_id "
            "JOIN customers c ON s.customer_id = c.customer_id "
            "WHERE s.order_date >= ? AND s.order_date <= ?" + extra
        )
        return float(self.scalar(sql, (start, end) + tuple(params)))

    def test_sales_total_scalar_matches_sql(self):
        result = self.call(
            "compute_metrics",
            {"metric": "sales_total", "start_date": "2025-01-01", "end_date": "2025-12-31"},
        )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["current"], self.expected_total("2025-01-01", "2025-12-31"), places=2)
        self.assertIsNone(result["previous"])
        self.assertIsNone(result["change_pct"])
        self.assertIn("销售额", result["summary"])

    def test_order_count_and_avg_order_value(self):
        total = self.call(
            "compute_metrics",
            {"metric": "sales_total", "start_date": "2025-09-01", "end_date": "2025-09-30"},
        )["current"]
        orders = self.call(
            "compute_metrics",
            {"metric": "order_count", "start_date": "2025-09-01", "end_date": "2025-09-30"},
        )["current"]
        avg = self.call(
            "compute_metrics",
            {"metric": "avg_order_value", "start_date": "2025-09-01", "end_date": "2025-09-30"},
        )["current"]
        self.assertEqual(orders, self.scalar(
            "SELECT COUNT(DISTINCT order_id) FROM sales WHERE order_date >= '2025-09-01' AND order_date <= '2025-09-30'"
        ))
        self.assertGreater(orders, 0)
        self.assertAlmostEqual(avg, round(total / orders, 2), places=2)

    def test_month_series_complete_and_ordered(self):
        result = self.call(
            "compute_metrics",
            {
                "metric": "sales_total",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "grain": "month",
            },
        )
        self.assertTrue(result["ok"])
        series = result["current"]
        self.assertEqual(len(series), 12)
        self.assertEqual([item["period"] for item in series], [f"2025-{month:02d}" for month in range(1, 13)])
        self.assertTrue(all(item["value"] > 0 for item in series))
        self.assertAlmostEqual(sum(item["value"] for item in series), result["current_total"], places=2)

    def test_day_and_week_grain(self):
        day = self.call(
            "compute_metrics",
            {
                "metric": "sales_total",
                "start_date": "2025-09-01",
                "end_date": "2025-09-30",
                "grain": "day",
            },
        )
        self.assertEqual(len(day["current"]), 30)
        week = self.call(
            "compute_metrics",
            {
                "metric": "sales_total",
                "start_date": "2025-09-01",
                "end_date": "2025-09-30",
                "grain": "week",
            },
        )
        labels = [item["period"] for item in week["current"]]
        self.assertTrue(labels)
        for label in labels:
            self.assertRegex(label, r"^\d{4}-W\d{2}$")

    def test_mom_compare_full_month(self):
        result = self.call(
            "compute_metrics",
            {
                "metric": "sales_total",
                "start_date": "2025-09-01",
                "end_date": "2025-09-30",
                "compare": "mom",
            },
        )
        self.assertTrue(result["ok"])
        august = self.call(
            "compute_metrics",
            {"metric": "sales_total", "start_date": "2025-08-01", "end_date": "2025-08-31"},
        )["current"]
        self.assertAlmostEqual(result["previous"], august, places=2)
        expected_change = round((result["current"] - august) / august * 100, 2)
        self.assertAlmostEqual(result["change_pct"], expected_change, places=2)
        self.assertEqual(result["previous_period"], {"start": "2025-08-01", "end": "2025-08-31"})

    def test_mom_series_has_previous_series(self):
        result = self.call(
            "compute_metrics",
            {
                "metric": "sales_total",
                "start_date": "2025-07-01",
                "end_date": "2025-09-30",
                "grain": "month",
                "compare": "mom",
            },
        )
        self.assertEqual(len(result["current"]), 3)
        self.assertEqual(len(result["previous"]), 3)
        self.assertIsInstance(result["change_pct"], float)

    def test_yoy_without_baseline_is_graceful(self):
        result = self.call(
            "compute_metrics",
            {
                "metric": "sales_total",
                "start_date": "2025-09-01",
                "end_date": "2025-09-30",
                "compare": "yoy",
            },
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["previous"])
        self.assertIsNone(result["change_pct"])
        self.assertIn("无可比基期", result["summary"])

    def test_filters_region_and_channel(self):
        unfiltered = self.call(
            "compute_metrics",
            {"metric": "sales_total", "start_date": "2025-01-01", "end_date": "2025-12-31"},
        )["current"]
        filtered = self.call(
            "compute_metrics",
            {
                "metric": "sales_total",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "filters": {"region": "华东"},
            },
        )
        self.assertTrue(filtered["ok"])
        self.assertEqual(filtered["filters"], {"region": "华东"})
        expected = self.expected_total(
            "2025-01-01", "2025-12-31", " AND st.region = ?", ("华东",)
        )
        self.assertAlmostEqual(filtered["current"], expected, places=2)
        self.assertLess(filtered["current"], unfiltered)
        both = self.call(
            "compute_metrics",
            {
                "metric": "sales_total",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "filters": {"region": "华东", "channel": "线上"},
            },
        )
        self.assertTrue(both["ok"])
        self.assertLessEqual(both["current"], filtered["current"])

    def test_conversion_rate_and_repeat_rate(self):
        buyers = self.scalar(
            "SELECT COUNT(DISTINCT customer_id) FROM sales "
            "WHERE order_date >= '2025-09-01' AND order_date <= '2025-09-30'"
        )
        registered = self.scalar("SELECT COUNT(*) FROM customers WHERE register_date <= '2025-09-30'")
        conversion = self.call(
            "compute_metrics",
            {"metric": "conversion_rate", "start_date": "2025-09-01", "end_date": "2025-09-30"},
        )
        self.assertTrue(conversion["ok"])
        self.assertAlmostEqual(conversion["current"], round(buyers / registered * 100, 2), places=2)
        self.assertLessEqual(conversion["current"], 100)

        repeat = self.call(
            "compute_metrics",
            {"metric": "repeat_rate", "start_date": "2025-09-01", "end_date": "2025-09-30"},
        )
        self.assertTrue(repeat["ok"])
        self.assertGreaterEqual(repeat["current"], 0)
        self.assertLessEqual(repeat["current"], 100)
        self.assertIn("%", repeat["summary"])

    def test_rate_grain_series(self):
        result = self.call(
            "compute_metrics",
            {
                "metric": "conversion_rate",
                "start_date": "2025-07-01",
                "end_date": "2025-09-30",
                "grain": "month",
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["current"]), 3)
        for item in result["current"]:
            self.assertGreaterEqual(item["value"], 0)
            self.assertLessEqual(item["value"], 100)

    def test_invalid_metric_and_dates(self):
        bad_calls = [
            {"metric": "gmv", "start_date": "2025-01-01", "end_date": "2025-01-31"},
            {"metric": "sales_total", "start_date": "2025/01/01", "end_date": "2025-01-31"},
            {"metric": "sales_total", "start_date": "2025-02-01", "end_date": "2025-01-31"},
            {"metric": "sales_total", "start_date": "2025-01-01", "end_date": "2025-01-31", "grain": "year"},
            {"metric": "sales_total", "start_date": "2025-01-01", "end_date": "2025-01-31", "compare": "wow"},
            {
                "metric": "sales_total",
                "start_date": "2025-01-01",
                "end_date": "2025-01-31",
                "filters": {"city": "上海"},
            },
            {"metric": "sales_total", "start_date": "2025-01-01"},
        ]
        for args in bad_calls:
            result = self.call("compute_metrics", args)
            self.assertFalse(result["ok"], args)

    def test_no_data_range_returns_zero(self):
        result = self.call(
            "compute_metrics",
            {"metric": "sales_total", "start_date": "2024-01-01", "end_date": "2024-03-31"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["current"], 0)
        self.assertIn("无数据", result["summary"])


class TestAnomalyDrivers(ToolTestBase):
    def test_region_attribution(self):
        result = self.call(
            "detect_anomaly_drivers",
            {"metric": "sales_total", "period": "2025-09", "dimension": "region", "top_n": 5},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["period"], "2025-09")
        self.assertEqual(result["baseline_period"], "2025-08")
        self.assertEqual(result["dimension"], "region")
        self.assertTrue(1 <= len(result["drivers"]) <= 5)
        for driver in result["drivers"]:
            for field in ("value", "current", "previous", "change_pct", "contribution_pct"):
                self.assertIn(field, driver)
            self.assertAlmostEqual(
                driver["current"] - driver["previous"], driver["change"], places=2
            )
        changes = [abs(driver["change"]) for driver in result["drivers"]]
        self.assertEqual(changes, sorted(changes, reverse=True))
        self.assertIn("2025-09", result["summary"])

    def test_contributions_sum_to_100_for_all_dimensions(self):
        result = self.call(
            "detect_anomaly_drivers",
            {"metric": "sales_total", "period": "2025-09", "dimension": "region", "top_n": 20},
        )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(
            abs(sum(driver["contribution_pct"] for driver in result["drivers"])), 100.0, places=1
        )

    def test_channel_and_category_dimensions(self):
        for dimension in ("channel", "category"):
            result = self.call(
                "detect_anomaly_drivers",
                {"metric": "sales_total", "period": "2025-11", "dimension": dimension, "top_n": 3},
            )
            self.assertTrue(result["ok"], dimension)
            self.assertLessEqual(len(result["drivers"]), 3)
            expected_values = {"channel": {"线上", "线下"}}
            if dimension == "channel":
                self.assertTrue({driver["value"] for driver in result["drivers"]} <= expected_values["channel"])

    def test_order_count_metric(self):
        result = self.call(
            "detect_anomaly_drivers",
            {"metric": "order_count", "period": "2025-10", "dimension": "region"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["metric"], "order_count")
        for driver in result["drivers"]:
            self.assertIsInstance(driver["current"], int)

    def test_top_n_clamped(self):
        result = self.call(
            "detect_anomaly_drivers",
            {"period": "2025-09", "dimension": "region", "top_n": 3},
        )
        self.assertLessEqual(len(result["drivers"]), 3)

    def test_invalid_inputs(self):
        bad_calls = [
            {"period": "2025-13"},
            {"period": "not-a-month"},
            {"period": "2025-09", "dimension": "city"},
            {"period": "2025-09", "metric": "conversion_rate"},
            {"period": "2025-09", "top_n": "many"},
        ]
        for args in bad_calls:
            result = self.call("detect_anomaly_drivers", args)
            self.assertFalse(result["ok"], args)

    def test_no_data_period_fails(self):
        result = self.call(
            "detect_anomaly_drivers", {"period": "2024-09", "dimension": "region"}
        )
        self.assertFalse(result["ok"])
        self.assertIn("没有销售数据", result["error"])


class TestGenerateChart(ToolTestBase):
    def _fake_renderer(self, recorded: dict):
        def fake_render_chart(*, chart_type, labels, values, title="", output_path=None, ylabel=""):
            self.assertEqual(len(labels), len(values))
            recorded.update(
                {"chart_type": chart_type, "labels": list(labels), "values": list(values),
                 "title": title, "ylabel": ylabel}
            )
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<svg>fake</svg>", encoding="utf-8")
            return path

        return fake_render_chart

    def test_line_chart_happy_path(self):
        recorded: dict = {}
        with mock.patch.object(chart_tool, "_load_render_chart", return_value=self._fake_renderer(recorded)):
            result = self.call(
                "generate_chart",
                {
                    "chart_type": "line",
                    "metric": "sales_total",
                    "start_date": "2025-06-01",
                    "end_date": "2025-09-30",
                    "grain": "month",
                    "filters": {"region": "华东"},
                    "title": "华东区月度销售额趋势",
                },
            )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["chart_type"], "line")
        self.assertEqual(result["points"], 4)
        self.assertEqual(recorded["labels"], ["2025-06", "2025-07", "2025-08", "2025-09"])
        self.assertEqual(recorded["chart_type"], "line")
        self.assertEqual(recorded["ylabel"], "sales_total")
        path = Path(result["path"])
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "chart_testtrace123.svg")
        self.assertEqual(path.parent, self.settings.artifacts_dir)
        self.assertIn("4 个数据点", result["summary"])
        json.dumps(result, ensure_ascii=False)

    def test_bar_and_pie_charts(self):
        for chart_type in ("bar", "pie"):
            with mock.patch.object(
                chart_tool, "_load_render_chart", return_value=self._fake_renderer({})
            ):
                result = self.call(
                    "generate_chart",
                    {
                        "chart_type": chart_type,
                        "metric": "order_count",
                        "start_date": "2025-01-01",
                        "end_date": "2025-12-31",
                    },
                )
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(result["points"], 12)
            self.assertEqual(result["chart_type"], chart_type)

    def test_empty_data_fails(self):
        with mock.patch.object(
            chart_tool, "_load_render_chart", return_value=self._fake_renderer({})
        ):
            result = self.call(
                "generate_chart",
                {
                    "metric": "sales_total",
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                },
            )
        self.assertFalse(result["ok"])
        self.assertIn("没有可用于绘图的数据", result["error"])

    def test_invalid_chart_type_and_metric(self):
        bad_calls = [
            {"chart_type": "scatter", "metric": "sales_total",
             "start_date": "2025-01-01", "end_date": "2025-01-31"},
            {"chart_type": "line", "metric": "gmv",
             "start_date": "2025-01-01", "end_date": "2025-01-31"},
            {"chart_type": "line", "metric": "sales_total"},
        ]
        for args in bad_calls:
            result = self.call("generate_chart", args)
            self.assertFalse(result["ok"], args)


class TestExportReport(ToolTestBase):
    def _fake_builder(self, recorded: dict):
        def fake_build_report(*, sections, title, fmt="markdown", output_path=None):
            recorded.update({"sections": sections, "title": title, "fmt": fmt})
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            body = f"# {title}\n\n" + "\n\n".join(
                f"## {section['heading']}\n{section['body']}" for section in sections
            )
            path.write_text(body, encoding="utf-8")
            return path

        return fake_build_report

    def test_markdown_report(self):
        recorded: dict = {}
        sections = [
            {"heading": "核心结论", "body": "9 月华东区销售额环比下降 62.3%。"},
            {"heading": "建议", "body": "关注家电品类下滑。"},
        ]
        with mock.patch.object(report_tool, "_load_build_report", return_value=self._fake_builder(recorded)):
            result = self.call(
                "export_report",
                {"format": "markdown", "title": "9 月经营分析", "sections": sections},
            )
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(recorded["title"], "9 月经营分析")
        self.assertEqual(recorded["fmt"], "markdown")
        self.assertEqual(recorded["sections"], sections)
        path = Path(result["path"])
        self.assertEqual(path.name, "report_testtrace123.md")
        self.assertTrue(path.exists())
        self.assertGreater(result["bytes"], 0)
        self.assertIn("9 月经营分析", path.read_text(encoding="utf-8"))
        json.dumps(result, ensure_ascii=False)

    def test_html_report(self):
        recorded: dict = {}
        with mock.patch.object(report_tool, "_load_build_report", return_value=self._fake_builder(recorded)):
            result = self.call(
                "export_report",
                {
                    "format": "html",
                    "title": "周报",
                    "sections": [{"heading": "概览", "body": "..."}],
                },
            )
        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["path"]).suffix, ".html")
        self.assertEqual(recorded["fmt"], "html")

    def test_invalid_format_and_sections(self):
        bad_calls = [
            {"format": "pdf", "title": "x", "sections": [{"heading": "a", "body": "b"}]},
            {"title": "", "sections": [{"heading": "a", "body": "b"}]},
            {"title": "x", "sections": []},
            {"title": "x", "sections": "not-a-list"},
            {"title": "x", "sections": [{"body": "b"}]},
            {"title": "x", "sections": [{"heading": "a"}]},
            {"title": 123, "sections": [{"heading": "a", "body": "b"}]},
        ]
        for args in bad_calls:
            result = self.call("export_report", args)
            self.assertFalse(result["ok"], args)

    def test_include_all_rows_flag_passes_through(self):
        with mock.patch.object(report_tool, "_load_build_report", return_value=self._fake_builder({})):
            result = self.call(
                "export_report",
                {
                    "format": "markdown",
                    "title": "客户全量明细导出",
                    "sections": [{"heading": "说明", "body": "全量导出"}],
                    "include_all_rows": True,
                },
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["include_all_rows"])
        self.assertIn("全量", result["summary"])


class TestStoreReadonly(ToolTestBase):
    def test_list_tables_describe_sample_profile(self):
        tables = self.store.list_tables()
        self.assertEqual([item["name"] for item in tables], ["stores", "products", "customers", "sales"])
        sales = self.store.describe_table("sales")
        self.assertEqual(sales["row_count"], 4000)
        self.assertEqual(len(sales["sample_rows"]), 3)
        regions = self.store.sample_values("stores", "region")
        self.assertEqual(set(regions) >= {"华东", "华北"}, True)
        profile = self.store.profile_table("sales")
        self.assertEqual(profile["row_count"], 4000)
        amount = next(item for item in profile["columns"] if item["name"] == "amount")
        self.assertEqual(amount["null_count"], 0)
        self.assertIn("min", amount)
        self.assertIn("max", amount)
        self.assertIn("mean", amount)
        self.assertAlmostEqual(amount["mean"], self.scalar("SELECT ROUND(AVG(amount), 4) FROM sales"), places=3)
        customers = self.store.profile_table("customers")
        phone = next(item for item in customers["columns"] if item["name"] == "phone")
        self.assertEqual(phone["distinct_count"], 800)

    def test_query_result_dataclass(self):
        result = self.store.run_readonly_query("SELECT 1 AS n")
        self.assertIsInstance(result, QueryResult)
        self.assertEqual(result.columns, ["n"])
        self.assertEqual(result.rows, [[1]])
        self.assertEqual(result.row_count, 1)
        self.assertFalse(result.truncated)
        self.assertGreaterEqual(result.elapsed_ms, 0.0)

    def test_run_readonly_query_auto_limit(self):
        result = self.store.run_readonly_query("SELECT * FROM sales")
        self.assertEqual(result.row_count, DEFAULT_ROW_LIMIT)
        self.assertTrue(result.truncated)
        capped = self.store.run_readonly_query("SELECT * FROM sales", limit=999)
        self.assertEqual(capped.row_count, MAX_ROW_LIMIT)
        self.assertTrue(capped.truncated)

    def test_run_readonly_query_explicit_limit(self):
        result = self.store.run_readonly_query("SELECT * FROM sales LIMIT 5")
        self.assertEqual(result.row_count, 5)
        self.assertTrue(result.truncated)

    def test_execute_readonly_with_params(self):
        result = self.store.execute_readonly(
            "SELECT COUNT(*) AS n FROM stores WHERE region = ?", ("华东",)
        )
        self.assertEqual(result.rows[0][0], 10)
        self.assertFalse(result.truncated)

    def test_readonly_guard_rejections(self):
        bad_sqls = [
            "UPDATE sales SET amount = 0",
            "DELETE FROM sales",
            "DROP TABLE sales",
            "INSERT INTO stores VALUES ('x')",
            "PRAGMA table_info(sales)",
            "ATTACH DATABASE 'x.db' AS x",
            "SELECT * FROM sales; DROP TABLE sales;",
            "SELECT * FROM sqlite_master",
            "SELECT * FROM employees",
            "SELECT * FROM sales JOIN employees ON 1 = 1",
            "",
            "   ",
        ]
        for sql in bad_sqls:
            with self.assertRaises((SQLGuardError, ValueError), msg=sql):
                self.store.run_readonly_query(sql)
        with self.assertRaises((SQLGuardError, ValueError)):
            self.store.run_readonly_query(None)  # type: ignore[arg-type]

    def test_guard_rejects_unknown_table_in_aggregate(self):
        with self.assertRaises(SQLGuardError):
            self.store.execute_readonly("SELECT COUNT(*) FROM unknown_table")

    def test_unknown_table_accessors_raise(self):
        with self.assertRaises(SQLGuardError):
            self.store.describe_table("employees")
        with self.assertRaises(SQLGuardError):
            self.store.sample_values("employees", "id")
        with self.assertRaises(SQLGuardError):
            self.store.sample_values("sales", "not_a_column")
        with self.assertRaises(SQLGuardError):
            self.store.profile_table("employees")

    def test_sqlite_connection_is_readonly(self):
        connection = self.store._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE hack (id INTEGER)")
        finally:
            connection.close()

    def test_missing_database_file(self):
        with self.assertRaises(FileNotFoundError):
            DemoStore(self.tmp / "not_exists.db")

    def test_close_blocks_further_queries(self):
        store = DemoStore(DB_PATH)
        store.close()
        with self.assertRaises(RuntimeError):
            store.list_tables()
        with self.assertRaises(RuntimeError):
            store.run_readonly_query("SELECT 1")

    def test_context_manager_closes(self):
        with DemoStore(DB_PATH) as store:
            self.assertEqual(len(store.list_tables()), 4)
        with self.assertRaises(RuntimeError):
            store.list_tables()

    @unittest.skipUnless(PANDAS_AVAILABLE, "pandas 未安装（可选增强，核心链路不依赖）")
    def test_query_dataframe_optional(self):
        frame = self.store.query_dataframe("SELECT region FROM stores")
        self.assertEqual(len(frame), 30)
        self.assertIn("region", frame.columns)


if __name__ == "__main__":
    unittest.main()
