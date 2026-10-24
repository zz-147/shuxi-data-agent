"""三级容错 JSON 解析。

大模型（尤其是中文提示下）经常返回「带解释文字 / code fence / 尾逗号 / 单引号」的
伪 JSON。Agent 的 ReAct 协议又强依赖 JSON，因此这里做三级降级：

1. ``direct``：直接 ``json.loads``；解析前先去 BOM、去 ``` 代码围栏、修尾逗号，
   单引号场景用 ``ast.literal_eval`` 兜底；
2. ``regex``：扫描文本中第一个「花括号配对平衡」的 ``{...}`` 片段再解析；
3. ``fallback``：都失败时返回 ``{"raw": 原文}``，由上层决定如何回灌给模型。

只有输入为空或不是字符串时才抛 :class:`JSONParseError`——「坏 JSON」本身不抛异常，
这样才能把 ``level`` 作为可观测信号记录到 trace 里。
"""

from __future__ import annotations

import ast
import json
import re
from typing import Dict, Iterator, List, Optional, Tuple

Level = str  # direct | regex | fallback

DIRECT: Level = "direct"
REGEX: Level = "regex"
FALLBACK: Level = "fallback"


class JSONParseError(ValueError):
    """输入不是字符串或为空文本时抛出."""


# ``` 围栏：允许 ```json / ```JSON / ``` 以及语言标记后无换行的写法
_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON|javascript|js)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n?")
_TRAILING_COMMA_RE = re.compile(r",[ \t\r\n]*([}\]])")
_PY_TRUE_RE = re.compile(r"\btrue\b", re.IGNORECASE)
_PY_FALSE_RE = re.compile(r"\bfalse\b", re.IGNORECASE)
_PY_NULL_RE = re.compile(r"\bnull\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _strip_code_fences(text: str) -> str:
    """取出第一个完整 ``` 围栏内的内容；围栏未闭合时去掉开头标记."""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    if "```" in text:
        parts = _FENCE_OPEN_RE.split(text, maxsplit=1)
        if len(parts) == 2:
            tail = parts[1].strip()
            # 未闭合围栏：去掉行尾可能残留的 ``` 标记
            return tail.rstrip("` \t\r\n")
    return text.strip()


def _repair_json_text(text: str) -> str:
    """常见小毛病修复：BOM、尾逗号（不改变字符串字面量语义的保守做法）."""
    fixed = text.lstrip("\ufeff").strip()
    fixed = _TRAILING_COMMA_RE.sub(r"\1", fixed)
    return fixed


def _pythonize(text: str) -> str:
    """把 JSON 字面量改写成 Python 字面量，供 ``ast.literal_eval`` 兜底单引号场景."""
    fixed = _repair_json_text(text)
    fixed = _PY_TRUE_RE.sub("True", fixed)
    fixed = _PY_FALSE_RE.sub("False", fixed)
    fixed = _PY_NULL_RE.sub("None", fixed)
    return fixed


def _loads_dict(candidate: str) -> Optional[dict]:
    """尝试把候选文本解析成 dict；解析成非 dict 视为失败（协议要求对象）."""
    for text in (candidate, _repair_json_text(candidate)):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            continue
        return value if isinstance(value, dict) else None
    return None


def _literal_dict(candidate: str) -> Optional[dict]:
    """单引号 / Python 字面量兜底（只接受 dict）."""
    if not candidate.lstrip().startswith("{"):
        return None
    try:
        value = ast.literal_eval(_pythonize(candidate))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _parse_candidate(candidate: str) -> Optional[dict]:
    payload = _loads_dict(candidate)
    if payload is not None:
        return payload
    return _literal_dict(candidate)


# --------------------------------------------------------------------------- #
# 花括号配对扫描
# --------------------------------------------------------------------------- #
def _iter_json_blocks(text: str) -> Iterator[str]:
    """按出现顺序产出文本里每个「花括号配对平衡」的 {...} 片段（忽略字符串内的括号）."""
    if not isinstance(text, str):
        return
    length = len(text)
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start < 0:
            return
        depth = 0
        in_string = False
        escaped = False
        index = start
        while index < length:
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[start : index + 1]
                        break
            index += 1
        cursor = start + 1


def extract_json_block(text: str) -> Optional[str]:
    """返回文本中第一个配对平衡的 ``{...}`` 片段；没有则返回 ``None``.

    注意：只做「括号配对」级别的提取，不保证内容一定是合法 JSON。
    """
    if not isinstance(text, str) or "{" not in text:
        return None
    return next(_iter_json_blocks(text), None)


# --------------------------------------------------------------------------- #
# 公共 API
# --------------------------------------------------------------------------- #
def parse_json_lenient(text: str) -> Tuple[Dict, Level]:
    """三级容错解析，返回 ``(payload, level)``.

    - ``level == "direct"``：原文/去围栏/修尾逗号后可直接 ``json.loads``（含单引号兜底）；
    - ``level == "regex"``：从解释文字中提取第一个配对平衡的 ``{...}`` 后解析成功；
    - ``level == "fallback"``：以上都失败，返回 ``{"raw": 原文}``。

    Raises:
        JSONParseError: 输入不是 ``str``，或去掉空白后为空。
    """
    if not isinstance(text, str):
        raise JSONParseError(f"JSON 解析失败：输入必须是字符串，实际为 {type(text).__name__}")
    raw = text.strip()
    if not raw:
        raise JSONParseError("JSON 解析失败：输入文本为空")

    # 第 1 级：direct —— 原文 / 去围栏后的文本（含尾逗号、单引号修复）
    candidates: List[str] = []
    for candidate in (raw, _strip_code_fences(raw)):
        cleaned = candidate.strip().lstrip("\ufeff")
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    for candidate in candidates:
        payload = _parse_candidate(candidate)
        if payload is not None:
            return payload, DIRECT

    # 第 2 级：regex —— 从解释文字里捞出第一个能解析的 {...}
    for block in _iter_json_blocks(raw):
        payload = _parse_candidate(block)
        if payload is not None:
            return payload, REGEX

    # 第 3 级：fallback —— 原文回传，交由上层决定重试或终止
    return {"raw": text}, FALLBACK


__all__ = [
    "JSONParseError",
    "parse_json_lenient",
    "extract_json_block",
    "DIRECT",
    "REGEX",
    "FALLBACK",
]
