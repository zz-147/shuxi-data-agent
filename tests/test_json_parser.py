# -*- coding: utf-8 -*-
"""json_parser 三级容错解析单测（覆盖规格 3.4 / 4 节用例）."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:  # 支持 unittest discover / pytest 两种跑法
    sys.path.insert(0, str(SRC))

from shuxi.json_parser import (  # noqa: E402
    JSONParseError,
    extract_json_block,
    parse_json_lenient,
)


class TestParseJsonLenient(unittest.TestCase):
    def test_bare_json_is_direct(self):
        payload, level = parse_json_lenient(
            '{"thought": "看看结构", "action": "inspect_schema", "action_input": {}}'
        )
        self.assertEqual(level, "direct")
        self.assertEqual(payload["action"], "inspect_schema")
        self.assertEqual(payload["action_input"], {})

    def test_code_fence_with_language_is_direct(self):
        text = '```json\n{"action": "query_data", "action_input": {"sql": "SELECT 1"}}\n```'
        payload, level = parse_json_lenient(text)
        self.assertEqual(level, "direct")
        self.assertEqual(payload["action"], "query_data")

    def test_code_fence_without_language_is_direct(self):
        text = '```\n{"final_answer": "完成"}\n```'
        payload, level = parse_json_lenient(text)
        self.assertEqual(level, "direct")
        self.assertEqual(payload["final_answer"], "完成")

    def test_unclosed_code_fence_is_direct(self):
        payload, level = parse_json_lenient('```json\n{"a": 1}')
        self.assertEqual(level, "direct")
        self.assertEqual(payload, {"a": 1})

    def test_explanation_around_json_is_regex(self):
        text = (
            "好的，我先分析一下现有数据：\n"
            '{"thought": "先看表结构", "action": "inspect_schema", "action_input": {"table": "sales"}}\n'
            "以上就是我的第一步，请稍等。"
        )
        payload, level = parse_json_lenient(text)
        self.assertEqual(level, "regex")
        self.assertEqual(payload["action"], "inspect_schema")
        self.assertEqual(payload["action_input"], {"table": "sales"})

    def test_braces_in_explanation_are_skipped(self):
        text = '说明：{这不是JSON} 然后才是 {"a": 1, "b": [2, 3]} 结束'
        payload, level = parse_json_lenient(text)
        self.assertEqual(level, "regex")
        self.assertEqual(payload, {"a": 1, "b": [2, 3]})

    def test_trailing_commas_are_repaired(self):
        payload, level = parse_json_lenient('{"a": [1, 2,], "b": {"c": 3,},}')
        self.assertEqual(level, "direct")
        self.assertEqual(payload, {"a": [1, 2], "b": {"c": 3}})

    def test_single_quotes_are_repaired(self):
        payload, level = parse_json_lenient("{'thought': 'ok', 'action': 'query_data', 'n': 2}")
        self.assertEqual(level, "direct")
        self.assertEqual(payload["action"], "query_data")
        self.assertEqual(payload["n"], 2)

    def test_python_literals_true_false_null(self):
        payload, level = parse_json_lenient("{'ok': True, 'bad': False, 'empty': None}")
        self.assertEqual(level, "direct")
        self.assertIs(payload["ok"], True)
        self.assertIs(payload["bad"], False)
        self.assertIsNone(payload["empty"])

    def test_nested_payload_is_direct(self):
        text = (
            '{"thought": "计划", "plan": [{"step": 1, "tool": "compute_metrics"}],'
            ' "action": "compute_metrics",'
            ' "action_input": {"metric": "sales_total", "filters": {"region": "华东"}}}'
        )
        payload, level = parse_json_lenient(text)
        self.assertEqual(level, "direct")
        self.assertEqual(payload["plan"][0]["tool"], "compute_metrics")
        self.assertEqual(payload["action_input"]["filters"]["region"], "华东")

    def test_escaped_characters_are_preserved(self):
        text = '{"thought": "第一行\\n第二行 \\"引用\\"", "value": "C:\\\\tmp"}'
        payload, level = parse_json_lenient(text)
        self.assertEqual(level, "direct")
        self.assertIn("第二行", payload["thought"])
        self.assertIn('"引用"', payload["thought"])
        self.assertEqual(payload["value"], "C:\\tmp")

    def test_chinese_content_is_preserved(self):
        payload, level = parse_json_lenient('{"final_answer": "2025 年 9 月华东区销售额环比增长 8.2%"}')
        self.assertEqual(level, "direct")
        self.assertIn("华东区", payload["final_answer"])

    def test_multiple_json_objects_returns_first(self):
        payload, level = parse_json_lenient('{"a": 1} 中间文字 {"b": 2}')
        self.assertEqual(level, "regex")
        self.assertEqual(payload, {"a": 1})

    def test_bad_input_falls_back_with_raw(self):
        text = "抱歉，我无法根据这些数据完成分析。"
        payload, level = parse_json_lenient(text)
        self.assertEqual(level, "fallback")
        self.assertEqual(payload, {"raw": text})

    def test_json_array_falls_back_because_payload_must_be_dict(self):
        payload, level = parse_json_lenient("[1, 2, 3]")
        self.assertEqual(level, "fallback")
        self.assertEqual(payload, {"raw": "[1, 2, 3]"})

    def test_empty_text_raises(self):
        with self.assertRaises(JSONParseError):
            parse_json_lenient("   \n\t ")

    def test_non_string_raises(self):
        for bad in (None, 123, {"a": 1}, [1, 2]):
            with self.assertRaises(JSONParseError):
                parse_json_lenient(bad)  # type: ignore[arg-type]

    def test_level_is_always_known(self):
        samples = ['{"a": 1}', "解释 {\"a\": 2}", "没有 json"]
        for sample in samples:
            _, level = parse_json_lenient(sample)
            self.assertIn(level, {"direct", "regex", "fallback"})


class TestExtractJsonBlock(unittest.TestCase):
    def test_extracts_first_balanced_block(self):
        text = '前面的解释 {"a": {"b": 1}, "c": 2} 后面的解释'
        block = extract_json_block(text)
        self.assertEqual(block, '{"a": {"b": 1}, "c": 2}')

    def test_braces_inside_strings_do_not_break_pairing(self):
        block = extract_json_block('前缀 {"text": "}{", "ok": true} 后缀')
        self.assertEqual(block, '{"text": "}{", "ok": true}')

    def test_returns_none_when_no_block(self):
        self.assertIsNone(extract_json_block("没有任何花括号"))
        self.assertIsNone(extract_json_block(""))
        self.assertIsNone(extract_json_block(None))  # type: ignore[arg-type]

    def test_returns_none_when_unclosed(self):
        self.assertIsNone(extract_json_block('前缀 {"a": 1 没有闭合'))


if __name__ == "__main__":
    unittest.main()
