# -*- coding: utf-8 -*-
"""Agent 单元测试：ReAct 循环 / 解析重试 / HITL 挂起与恢复 / 步数上限.

测试用「自定义工具 + 轻量 guard」注入，不依赖真实数据库与网络。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# 允许不安装直接 `python -m unittest discover -s tests`（顶层目录不是仓库根时）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shuxi.agent import AgentError, ApprovalRequest, DataAnalysisAgent
from shuxi.config import Settings
from shuxi.llm import MockProvider
from shuxi.trace import TraceRecorder


def make_settings(tmp: Path, **overrides) -> Settings:
    return Settings.from_env(
        provider="mock",
        root_dir=tmp,
        db_path=tmp / "data" / "demo.db",
        log_dir=tmp / "logs",
        artifacts_dir=tmp / "artifacts",
        **overrides,
    )


class FakeTool:
    def __init__(self, name: str):
        self.name = name
        self.description = f"{name} 测试工具"
        self.parameters = {"type": "object", "properties": {}}
        self.risk = "low"
        self.calls: list[dict] = []

    def handler(self, args, ctx):  # noqa: ANN001 - 测试桩
        self.calls.append(args)
        return {
            "ok": True,
            "summary": f"{self.name} executed",
            "path": f"/tmp/{self.name}.svg" if self.name == "generate_chart" else None,
        }


class FakeGuard:
    """只实现 agent 需要的 4 个方法；默认全部 low 风险."""

    def __init__(self, high_tools: dict[str, str] | None = None):
        self.high_tools = high_tools or {}
        self.audit: list[dict] = []

    def validate_call(self, name, args):
        if not isinstance(args, dict):
            return False, "参数必须是 JSON 对象"
        return True, ""

    def assess_risk(self, name, args):
        level = self.high_tools.get(name, "low")
        reasons = ["全量导出敏感数据"] if level == "high" else []
        return SimpleNamespace(level=level, reasons=reasons)

    def requires_approval(self, risk):
        return risk.level in {"high"}

    def record_audit(self, **kwargs):
        self.audit.append(kwargs)
        return Path("audit.jsonl")


def response(**payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def build_agent(tmp: Path, script: list[str], *, tools=None, guard=None, **settings_kw):
    settings = make_settings(tmp, **settings_kw).ensure_dirs()
    provider = MockProvider(script)
    trace = TraceRecorder(settings.log_dir, query="test")
    tool_map = tools or {
        "inspect_schema": FakeTool("inspect_schema"),
        "compute_metrics": FakeTool("compute_metrics"),
        "generate_chart": FakeTool("generate_chart"),
    }
    return DataAnalysisAgent(
        settings,
        provider,
        store=SimpleNamespace(),
        guard=guard or FakeGuard(),
        trace=trace,
        tools=tool_map,
    )


class AgentTest(unittest.TestCase):
    def test_full_react_flow(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            script = [
                response(
                    thought="先看结构",
                    plan=[
                        {"step": 1, "tool": "inspect_schema", "reason": "看字段"},
                        {"step": 2, "tool": "compute_metrics", "reason": "算指标"},
                        {"step": 3, "tool": "generate_chart", "reason": "画图"},
                    ],
                    action="inspect_schema",
                    action_input={"table": "sales"},
                ),
                response(thought="算指标", action="compute_metrics", action_input={"metric": "sales_total"}),
                response(thought="画图", action="generate_chart", action_input={"chart_type": "line"}),
                response(thought="完成", final_answer="9 月华东区销售额见工具结果，趋势图已生成。"),
            ]
            agent = build_agent(tmp, script)
            result = agent.run("查询 2025 年 9 月华东区销售额")

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(result.plan), 3)
            self.assertEqual(len(result.steps), 3)
            self.assertEqual([step["tool"] for step in result.steps], ["inspect_schema", "compute_metrics", "generate_chart"])
            self.assertIn("趋势图", result.answer)
            self.assertEqual(len(result.artifacts), 1)
            self.assertTrue(Path(result.trace_path).exists())

            kinds = {span["kind"] for span in result.trace["spans"]}
            self.assertTrue({"plan", "llm", "tool"}.issubset(kinds))
            trace_events = {event["kind"] for event in result.trace["events"]}
            self.assertIn("plan_ready", trace_events)
            self.assertIn("tool_end", trace_events)

    def test_parse_error_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            script = [
                "这不是 JSON，我会重新输出。",
                response(thought="直接回答", final_answer="完成。"),
            ]
            agent = build_agent(tmp, script)
            result = agent.run("问题")
            self.assertEqual(result.status, "completed")
            events = {event["kind"] for event in result.trace["events"]}
            self.assertIn("llm_parse_error", events)

    def test_parse_always_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            agent = build_agent(Path(raw_tmp), ["坏输出一", "坏输出二"])
            with self.assertRaises(AgentError):
                agent.run("问题")

    def test_hitl_awaiting_and_resume_approved(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            export = FakeTool("export_report")
            guard = FakeGuard(high_tools={"export_report": "high"})
            script = [
                response(
                    thought="导出",
                    plan=[{"step": 1, "tool": "export_report", "reason": "全量导出"}],
                    action="export_report",
                    action_input={"include_all_rows": True},
                ),
                response(thought="完成", final_answer="导出完成。"),
            ]
            agent = build_agent(
                tmp,
                script,
                tools={"export_report": export},
                guard=guard,
            )
            result = agent.run("导出全部客户明细")
            self.assertEqual(result.status, "awaiting_approval")
            self.assertIsNotNone(result.pending_approval)
            self.assertEqual(result.pending_approval["tool"], "export_report")
            self.assertEqual(export.calls, [])

            resumed = agent.resume(True)
            self.assertEqual(resumed.status, "completed")
            self.assertEqual(len(export.calls), 1)
            self.assertTrue(any(item["decision"] == "approved" for item in guard.audit))

    def test_hitl_reject(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            guard = FakeGuard(high_tools={"export_report": "high"})
            script = [
                response(
                    thought="导出",
                    plan=[{"step": 1, "tool": "export_report", "reason": "全量导出"}],
                    action="export_report",
                    action_input={"include_all_rows": True},
                ),
                response(thought="完成", final_answer="不会到这里。"),
            ]
            agent = build_agent(
                tmp,
                script,
                tools={"export_report": FakeTool("export_report")},
                guard=guard,
            )
            result = agent.run("导出", approve=lambda request: False)
            self.assertEqual(result.status, "rejected")
            self.assertIn("拒绝", result.error)
            self.assertTrue(any(item["decision"] == "rejected" for item in guard.audit))

    def test_max_steps(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            script = [
                response(thought="继续", action="inspect_schema", action_input={}),
                response(thought="继续", action="inspect_schema", action_input={}),
                response(thought="继续", action="inspect_schema", action_input={}),
            ]
            agent = build_agent(tmp, script, max_steps=2)
            result = agent.run("问题")
            self.assertEqual(result.status, "failed")
            self.assertIn("最大步数", result.error)

    def test_plan_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            script = [
                response(
                    thought="规划",
                    plan=[
                        {"step": 1, "tool": "inspect_schema", "reason": "结构"},
                        {"step": 2, "tool": "unknown_tool", "reason": "非法工具应被剔除"},
                    ],
                )
            ]
            agent = build_agent(tmp, script)
            payload = agent.plan_only("问题")
            self.assertEqual([item["tool"] for item in payload["plan"]], ["inspect_schema"])

    def test_approval_request_shape(self) -> None:
        request = ApprovalRequest(
            trace_id="trace_1", tool="export_report", args={"a": 1}, risk="high", reasons=["r"]
        )
        self.assertEqual(request.tool, "export_report")


if __name__ == "__main__":
    unittest.main()
