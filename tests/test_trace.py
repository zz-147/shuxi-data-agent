# -*- coding: utf-8 -*-
"""Trace 单元测试：span 嵌套、事件、落盘、列表、回放."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shuxi.trace import TraceRecorder, list_traces, load_trace, replay_trace


class TraceTest(unittest.TestCase):
    def test_span_nesting_and_save(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            trace = TraceRecorder(tmp / "logs", query="测试问题")
            with trace.span("plan", "derive_plan") as plan_attrs:
                plan_attrs["steps"] = ["a", "b"]
                with trace.span("tool", "inspect_schema", args={"table": "sales"}) as tool_attrs:
                    tool_attrs["ok"] = True
            trace.event("note", msg="hello")
            data = trace.finish("completed", answer="结论", artifacts=["a.svg"])
            path = trace.save()

            self.assertTrue(path.exists())
            self.assertEqual(data["status"], "completed")
            self.assertEqual(len(data["spans"]), 2)
            tool_span = [span for span in data["spans"] if span["kind"] == "tool"][0]
            plan_span = [span for span in data["spans"] if span["kind"] == "plan"][0]
            self.assertEqual(tool_span["parent_id"], plan_span["span_id"])

            loaded = load_trace(trace.trace_id, tmp / "logs")
            self.assertEqual(loaded["query"], "测试问题")
            self.assertEqual(loaded["result"]["answer"], "结论")

            items = list_traces(tmp / "logs")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["trace_id"], trace.trace_id)

            text = replay_trace(trace.trace_id, tmp / "logs")
            self.assertIn("trace_id", text)
            self.assertIn("derive_plan", text)
            self.assertIn("结论", text)

    def test_error_span_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            trace = TraceRecorder(Path(raw_tmp) / "logs", query="q")
            with self.assertRaises(RuntimeError):
                with trace.span("tool", "boom"):
                    raise RuntimeError("failed")
            data = trace.finish("failed")
            self.assertEqual(data["spans"][0]["status"], "error")

    def test_load_missing_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            with self.assertRaises(FileNotFoundError):
                load_trace("trace_not_exist", Path(raw_tmp) / "logs")

    def test_list_traces_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            self.assertEqual(list_traces(Path(raw_tmp) / "logs"), [])


if __name__ == "__main__":
    unittest.main()
