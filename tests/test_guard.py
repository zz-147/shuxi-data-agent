# -*- coding: utf-8 -*-
"""guard 单测：白名单 / 危险指令 / 敏感字段高风险 / 导出全量高风险 / 审计 / 审批.

不依赖 pandas、网络与真实数据库，可在任何环境单独运行。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shuxi.config import Settings  # noqa: E402
from shuxi.guard import (  # noqa: E402
    INJECTION_PATTERNS,
    TOOL_WHITELIST,
    ApprovalRequired,
    RiskAssessment,
    ToolGuard,
    args_digest,
    detect_injection,
)


def make_settings(tmp: Path, **overrides) -> Settings:
    values = {
        "provider": "mock",
        "root_dir": tmp,
        "db_path": tmp / "data" / "demo.db",
        "log_dir": tmp / "logs",
        "artifacts_dir": tmp / "artifacts",
    }
    values.update(overrides)
    return Settings.from_env(**values)


class GuardTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = make_settings(self.tmp)
        self.guard = ToolGuard(self.settings)
        self.addCleanup(self.guard.record_audit, trace_id="cleanup", tool="t", args={}, risk="low", decision="allow")


class TestValidateCall(GuardTestBase):
    def test_all_whitelisted_tools_pass(self):
        for name in TOOL_WHITELIST:
            ok, reason = self.guard.validate_call(name, {})
            self.assertTrue(ok, f"{name} 应通过校验，实际被拒绝：{reason}")

    def test_unknown_tool_rejected(self):
        ok, reason = self.guard.validate_call("rm_rf", {})
        self.assertFalse(ok)
        self.assertIn("白名单", reason)

    def test_non_string_tool_name_rejected(self):
        ok, reason = self.guard.validate_call(None, {})  # type: ignore[arg-type]
        self.assertFalse(ok)

    def test_args_must_be_dict(self):
        for bad in ("SELECT 1", None, [1, 2], 42):
            ok, reason = self.guard.validate_call("query_data", bad)  # type: ignore[arg-type]
            self.assertFalse(ok, f"args={bad!r} 应被拒绝")
            self.assertIn("JSON 对象", reason)

    def test_injection_in_sql_rejected(self):
        ok, reason = self.guard.validate_call(
            "query_data", {"sql": "SELECT * FROM sales; DROP TABLE sales"}
        )
        self.assertFalse(ok)
        self.assertIn("黑名单", reason)

    def test_injection_in_nested_args_rejected(self):
        ok, reason = self.guard.validate_call(
            "export_report",
            {
                "title": "报告",
                "sections": [{"heading": "x", "body": "__import__('os').system('x')"}],
            },
        )
        self.assertFalse(ok)
        self.assertIn("黑名单", reason)

    def test_undeclared_param_warns_but_passes(self):
        ok, reason = self.guard.validate_call("inspect_schema", {"table": "sales", "unexpected": 1})
        self.assertTrue(ok)
        self.assertIn("unexpected", reason)
        self.assertIn("未声明", reason)

    def test_oversized_string_rejected(self):
        ok, reason = self.guard.validate_call("query_data", {"sql": "SELECT '" + "x" * 9000 + "'"})
        self.assertFalse(ok)
        self.assertIn("过长", reason)

    def test_oversized_args_rejected(self):
        payload = {"sql": "SELECT 1", "a": "y" * 7000, "b": "z" * 7000, "c": "w" * 7000}
        ok, reason = self.guard.validate_call("query_data", payload)
        self.assertFalse(ok)
        self.assertIn("体积", reason)


class TestRiskAssessment(GuardTestBase):
    def test_low_tools(self):
        cases = {
            "inspect_schema": {"table": "sales"},
            "compute_metrics": {"metric": "sales_total"},
            "detect_anomaly_drivers": {"period": "2025-09"},
            "generate_chart": {"metric": "sales_total"},
        }
        for name, args in cases.items():
            risk = self.guard.assess_risk(name, args)
            self.assertEqual(risk.level, "low", name)
            self.assertEqual(risk.tool, name)
            self.assertFalse(self.guard.requires_approval(risk), name)

    def test_query_data_default_medium_no_approval(self):
        risk = self.guard.assess_risk(
            "query_data", {"sql": "SELECT region, SUM(amount) FROM sales GROUP BY region LIMIT 10"}
        )
        self.assertEqual(risk.level, "medium")
        self.assertFalse(self.guard.requires_approval(risk))

    def test_sensitive_fields_with_large_result_is_high(self):
        risk = self.guard.assess_risk(
            "query_data", {"sql": "SELECT name, phone FROM customers", "limit": 100}
        )
        self.assertEqual(risk.level, "high")
        self.assertTrue(self.guard.requires_approval(risk))
        self.assertTrue(any("敏感" in reason for reason in risk.reasons))

    def test_sensitive_fields_with_small_limit_stays_medium(self):
        risk = self.guard.assess_risk(
            "query_data", {"sql": "SELECT name, phone FROM customers", "limit": 20}
        )
        self.assertEqual(risk.level, "medium")
        self.assertFalse(self.guard.requires_approval(risk))

    def test_non_sensitive_large_query_stays_medium(self):
        risk = self.guard.assess_risk(
            "query_data", {"sql": "SELECT store_id, region FROM stores", "limit": 100}
        )
        self.assertEqual(risk.level, "medium")

    def test_export_default_medium_requires_approval(self):
        risk = self.guard.assess_risk(
            "export_report",
            {"format": "markdown", "title": "周报", "sections": [{"heading": "结论", "body": "..."}]},
        )
        self.assertEqual(risk.level, "medium")
        self.assertTrue(self.guard.requires_approval(risk))

    def test_export_all_rows_is_high(self):
        risk = self.guard.assess_risk(
            "export_report",
            {
                "format": "markdown",
                "title": "客户全量明细",
                "sections": [{"heading": "说明", "body": "全量导出"}],
                "include_all_rows": True,
            },
        )
        self.assertEqual(risk.level, "high")
        self.assertTrue(self.guard.requires_approval(risk))

    def test_injection_args_assessed_high(self):
        risk = self.guard.assess_risk("query_data", {"sql": "SELECT 1; os.system('rm -rf /')"})
        self.assertEqual(risk.level, "high")

    def test_unknown_tool_assessed_high(self):
        risk = self.guard.assess_risk("shell", {"cmd": "ls"})
        self.assertEqual(risk.level, "high")

    def test_requires_approval_accepts_plain_risk_objects(self):
        self.assertTrue(self.guard.requires_approval(RiskAssessment("high", ["x"])))
        self.assertTrue(self.guard.requires_approval(RiskAssessment("medium", ["export 类工具"])))
        self.assertFalse(self.guard.requires_approval(RiskAssessment("medium", ["只读查询"])))
        self.assertFalse(self.guard.requires_approval(RiskAssessment("low", [])))
        self.assertFalse(self.guard.requires_approval(None))  # type: ignore[arg-type]


class TestDetectionAndDigest(GuardTestBase):
    def test_detect_injection_all_patterns(self):
        samples = {
            "import os": r"import\s+",
            "__import__('os')": "__import__",
            "eval('1+1')": r"eval\s*\(",
            "exec('x')": r"exec\s*\(",
            "os.system('id')": r"os\.system",
            "subprocess.run(['ls'])": "subprocess",
            "shutil.rmtree('/tmp')": r"shutil\.rmtree",
            "open('/tmp/x', 'w')": r"open\s*\([^)]*['\"]w",
            "DROP TABLE sales": r"DROP\s+TABLE",
            "DELETE FROM sales": r"DELETE\s+FROM",
            "TRUNCATE sales": "TRUNCATE",
            "ATTACH DATABASE 'x' AS y": r"ATTACH\s+DATABASE",
            "LOAD_EXTENSION('x')": r"LOAD_EXTENSION",
            "EXEC xp_cmdshell 'dir'": "xp_cmdshell",
        }
        for text, expected in samples.items():
            hits = detect_injection(text)
            self.assertTrue(hits, f"{text!r} 应命中黑名单")
            self.assertIn(expected, hits)
        self.assertGreaterEqual(len(detect_injection("import os")), 1)
        self.assertEqual(len(INJECTION_PATTERNS), 14)

    def test_detect_injection_is_case_insensitive(self):
        self.assertTrue(detect_injection("drop table users"))
        self.assertTrue(detect_injection("PaSsWoRd; LOAD_EXTENSION"))

    def test_detect_injection_clean_text(self):
        for text in ("SELECT region, SUM(amount) FROM sales", "华东区销售额", "", None, 123):
            self.assertEqual(detect_injection(text), [])  # type: ignore[arg-type]

    def test_args_digest_stable_and_short(self):
        first = args_digest({"sql": "SELECT 1", "limit": 10})
        second = args_digest({"limit": 10, "sql": "SELECT 1"})
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertNotEqual(first, args_digest({"sql": "SELECT 2", "limit": 10}))
        self.assertNotIn("SELECT", first)

    def test_args_digest_handles_non_json_values(self):
        self.assertEqual(len(args_digest({"path": Path("x"), "s": {1, 2}})), 16)


class TestApprovalAndAudit(GuardTestBase):
    def test_approval_required_carries_context(self):
        risk = RiskAssessment("high", ["全量导出敏感数据"], "export_report")
        args = {"title": "客户明细", "include_all_rows": True}
        error = ApprovalRequired("export_report", args, risk)
        self.assertEqual(error.tool, "export_report")
        self.assertEqual(error.args, args)
        self.assertIs(error.risk, risk)
        self.assertEqual(error.reasons, ["全量导出敏感数据"])
        self.assertIn("export_report", str(error))
        self.assertIn("全量导出敏感数据", str(error))

    def test_approval_required_explicit_reasons(self):
        error = ApprovalRequired("query_data", {}, "high", ["自定义原因"])
        self.assertEqual(error.reasons, ["自定义原因"])

    def test_record_audit_writes_jsonl_without_raw_args(self):
        sensitive_args = {"sql": "SELECT name, phone FROM customers", "limit": 50}
        path = self.guard.record_audit(
            trace_id="trace-abc",
            tool="query_data",
            args=sensitive_args,
            risk=RiskAssessment("high", ["敏感字段"]),
            decision="approved",
            reason="HITL 审批",
        )
        self.assertEqual(path, self.settings.log_dir / "audit.jsonl")
        self.assertTrue(path.exists())
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        for field in ("ts", "trace_id", "tool", "args_digest", "risk", "decision", "reason"):
            self.assertIn(field, entry)
        self.assertEqual(entry["trace_id"], "trace-abc")
        self.assertEqual(entry["tool"], "query_data")
        self.assertEqual(entry["risk"], "high")
        self.assertEqual(entry["decision"], "approved")
        self.assertEqual(entry["args_digest"], args_digest(sensitive_args))
        self.assertEqual(len(entry["args_digest"]), 16)
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("SELECT name", raw)
        self.assertNotIn("phone", raw)

    def test_record_audit_accepts_level_string_and_appends(self):
        path = self.guard.record_audit(
            trace_id="t1", tool="query_data", args={"sql": "SELECT 1"}, risk="medium", decision="allow"
        )
        self.guard.record_audit(
            trace_id="t1",
            tool="export_report",
            args={"title": "x"},
            risk=RiskAssessment("high", ["全量导出"], "export_report"),
            decision="rejected",
            reason="用户拒绝",
        )
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["risk"], "medium")
        self.assertEqual(json.loads(lines[1])["decision"], "rejected")
        self.assertEqual(json.loads(lines[1])["risk"], "high")

    def test_custom_audit_path_nested_dir(self):
        guard = ToolGuard(self.settings, audit_path=self.tmp / "custom" / "audit.jsonl")
        path = guard.record_audit(
            trace_id="t2", tool="inspect_schema", args={}, risk=RiskAssessment("low", []), decision="deny"
        )
        self.assertEqual(path, self.tmp / "custom" / "audit.jsonl")
        self.assertTrue(path.exists())

    def test_audit_decision_values_are_kept(self):
        path = self.settings.log_dir / "audit_decisions.jsonl"
        guard = ToolGuard(self.settings, audit_path=path)
        for decision in ("allow", "deny", "approved", "rejected"):
            guard.record_audit(
                trace_id="t3", tool="query_data", args={"sql": "SELECT 1"},
                risk="medium", decision=decision, reason="test",
            )
        decisions = [json.loads(line)["decision"] for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(decisions, ["allow", "deny", "approved", "rejected"])


if __name__ == "__main__":
    unittest.main()
