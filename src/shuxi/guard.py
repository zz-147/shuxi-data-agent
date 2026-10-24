"""工具护栏层：白名单 / 参数校验 / 注入检测 / 风险分级 / HITL / 审计。

职责边界（为什么 HITL 放在工具层）：
- **不信任模型输出**：模型只能产出「工具名 + JSON 参数」，参数在这里过白名单、
  危险指令黑名单和体积限制，模型没有机会直接执行代码或拼接任意 SQL；
- **风险分级前置**：在真正执行工具之前就能判断 low / medium / high，从而决定
  是否需要人类批准（HITL 断点），而不是执行完了再补救；
- **审计只落摘要**：``logs/audit.jsonl`` 记录 ``args_digest``（sha256 前 16 位），
  不落原始参数，避免把 name / phone 等敏感数据写进日志。

本模块不依赖 pandas / LLM SDK，可独立单测。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
TOOL_WHITELIST: Tuple[str, ...] = (
    "inspect_schema",
    "query_data",
    "compute_metrics",
    "detect_anomaly_drivers",
    "generate_chart",
    "export_report",
)
LOW_RISK_TOOLS = frozenset(
    {"inspect_schema", "compute_metrics", "detect_anomaly_drivers", "generate_chart"}
)
MEDIUM_RISK_TOOLS = frozenset({"query_data", "export_report"})
EXPORT_TOOLS = frozenset({"export_report"})

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_LEVELS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)

# 敏感字段：customers.name / customers.phone。用词边界避免误伤 store_name / product_name
SENSITIVE_COLUMN_RE = re.compile(r"\b(?:name|phone)\b", re.IGNORECASE)
SENSITIVE_ROW_THRESHOLD = 20
DEFAULT_QUERY_LIMIT = 100

# 注入/危险指令黑名单（大小写不敏感，正则）
INJECTION_PATTERNS: Tuple[str, ...] = (
    r"import\s+",
    r"__import__",
    r"eval\s*\(",
    r"exec\s*\(",
    r"os\.system",
    r"subprocess",
    r"shutil\.rmtree",
    r"open\s*\([^)]*['\"]w",
    r"DROP\s+TABLE",
    r"DELETE\s+FROM",
    r"TRUNCATE",
    r"ATTACH\s+DATABASE",
    r"LOAD_EXTENSION",
    r"xp_cmdshell",
)
_COMPILED_INJECTION: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (pattern, re.compile(pattern, re.IGNORECASE)) for pattern in INJECTION_PATTERNS
)

# 参数体积限制（防止用超大参数淹没 prompt / 日志）
MAX_ARGS_CHARS = 20_000
MAX_STRING_CHARS = 8_000
MAX_SQL_CHARS = 5_000
MAX_REASON_CHARS = 500

AUDIT_DECISIONS = ("allow", "deny", "approved", "rejected")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RiskAssessment:
    """一次工具调用的风险评级.

    ``tool`` 是额外补充字段（默认空串），让 :meth:`ToolGuard.requires_approval`
    在只有 risk 对象时也能识别「medium 且导出类」的场景。
    """

    level: str
    reasons: List[str] = field(default_factory=list)
    tool: str = ""


class ApprovalRequired(Exception):
    """需要人工审批（HITL 断点）时抛出，携带完整上下文供 agent 挂起/恢复.

    注意：``BaseException.args`` 是内置属性，这里用 property 让 ``.args`` 返回
    「工具参数 dict」（契约要求），异常消息改由 ``message`` 字段与 ``__str__`` 提供。
    """

    def __init__(self, tool: str, args: dict, risk: Any, reasons: Optional[Sequence[str]] = None):
        super().__init__()
        self.tool = tool
        self._tool_args = args
        self.risk = risk
        if reasons is None:
            reasons = getattr(risk, "reasons", None) or []
        self.reasons: List[str] = [str(item) for item in reasons]
        level = risk.level if hasattr(risk, "level") else str(risk)
        self.message = (
            f"工具 {tool} 需要人工审批（风险等级：{level}）："
            + ("；".join(self.reasons) if self.reasons else "未说明原因")
        )

    @property
    def args(self) -> dict:
        return self._tool_args

    @args.setter
    def args(self, value: dict) -> None:
        self._tool_args = value

    def __str__(self) -> str:
        return self.message


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def detect_injection(text: str) -> List[str]:
    """返回 ``text`` 命中的危险模式列表（正则原文，大小写不敏感）；未命中返回空列表."""
    if not isinstance(text, str) or not text:
        return []
    hits: List[str] = []
    for pattern, compiled in _COMPILED_INJECTION:
        if compiled.search(text):
            hits.append(pattern)
    return hits


def args_digest(args: Any) -> str:
    """参数摘要：sha256(json.dumps(sort_keys))[:16]，审计中不落原始参数."""
    try:
        serialized = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 极端不可序列化兜底
        serialized = repr(args)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _iter_string_values(value: Any, path: str = "") -> Iterable[Tuple[str, str]]:
    """递归产出参数树中的所有字符串值（含 dict 的键名路径，便于定位）. """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_string_values(item, child_path)
    elif isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            yield from _iter_string_values(item, f"{path}[{index}]")


def _scan_injection(args: Any) -> List[str]:
    """扫描参数树里的所有字符串，返回命中的危险模式（去重、保持顺序）."""
    hits: List[str] = []
    for _, text in _iter_string_values(args):
        for pattern in detect_injection(text):
            if pattern not in hits:
                hits.append(pattern)
    return hits


def _check_arg_sizes(args: Any) -> str:
    """检查参数体积；返回错误文案，合法时返回空串."""
    try:
        serialized = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover
        serialized = repr(args)
    if len(serialized) > MAX_ARGS_CHARS:
        return f"参数体积过大（{len(serialized)} 字符 > 上限 {MAX_ARGS_CHARS}）"
    for path, text in _iter_string_values(args):
        if len(text) > MAX_STRING_CHARS:
            return f"参数 {path or '<string>'} 过长（{len(text)} 字符 > 上限 {MAX_STRING_CHARS}）"
        if path.split(".")[-1].lower() == "sql" and len(text) > MAX_SQL_CHARS:
            return f"SQL 过长（{len(text)} 字符 > 上限 {MAX_SQL_CHARS}）"
    return ""


def _requested_query_limit(args: Dict[str, Any]) -> int:
    """query_data 预计返回行数：取参数 limit，缺省 100."""
    raw = args.get("limit", DEFAULT_QUERY_LIMIT) if isinstance(args, dict) else DEFAULT_QUERY_LIMIT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_QUERY_LIMIT


# --------------------------------------------------------------------------- #
# 护栏主体
# --------------------------------------------------------------------------- #
class ToolGuard:
    """工具调用的统一入口护栏：校验 → 分级 → 审批判断 → 审计."""

    def __init__(self, settings: Any, audit_path: Optional[Path] = None):
        self.settings = settings
        log_dir = Path(getattr(settings, "log_dir", Path("logs")))
        self.audit_path = Path(audit_path) if audit_path is not None else log_dir / "audit.jsonl"

    # ------------------------------------------------------------------ 校验
    def validate_call(self, name: str, args: dict) -> Tuple[bool, str]:
        """执行前校验；返回 ``(是否允许, 说明)``.

        规则顺序：
        1. 工具名必须注册在白名单内；
        2. args 必须是 dict；
        3. 任何参数命中危险指令黑名单 → 直接拒绝（不走 HITL，因为这不是「风险高」而是「恶意」）；
        4. 参数体积限制；
        5. 未声明参数宽松放行，但在返回文案里记录（agent 会写入 trace）。

        Returns:
            tuple[bool, str]: 允许时第二项是 ``"校验通过"`` 或警告文案；拒绝时是原因。
        """
        if not isinstance(name, str) or name not in TOOL_WHITELIST:
            return False, f"未知工具 {name!r}：不在白名单 {list(TOOL_WHITELIST)} 内"
        if not isinstance(args, dict):
            return False, "工具参数必须是 JSON 对象（dict）"
        hits = _scan_injection(args)
        if hits:
            return False, "参数命中危险指令黑名单：" + "、".join(hits)
        size_error = _check_arg_sizes(args)
        if size_error:
            return False, size_error

        declared = self._declared_params(name)
        if declared is not None:
            unknown = sorted(key for key in args if key not in declared)
            if unknown:
                return True, "警告：存在未声明参数（已记录）：" + "、".join(unknown)
        return True, "校验通过"

    def _declared_params(self, name: str) -> Optional[set]:
        """从 tools 注册表读该工具声明的参数名；注册表不可用时返回 None（跳过该规则）."""
        try:
            from .tools import get_tools

            spec = get_tools().get(name)
        except Exception:  # pragma: no cover - tools 包异常时降级为「不检查参数名」
            return None
        if spec is None:
            return None
        parameters = getattr(spec, "parameters", None) or {}
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if not isinstance(properties, dict):
            return None
        return set(properties)

    # ------------------------------------------------------------------ 风险
    def assess_risk(self, name: str, args: dict) -> RiskAssessment:
        """给工具调用分级，返回 :class:`RiskAssessment`.

        规则（对应规格 3.6）：
        - low：inspect_schema / compute_metrics / detect_anomaly_drivers / generate_chart；
        - medium：query_data、export_report；
        - high：export_report 且 include_all_rows=true；query_data 的 SQL 命中敏感字段
          （name / phone）且预计返回行数 > 20；任何参数命中黑名单（validate_call 已先拒绝，
          这里再兜底一次）。
        """
        if not isinstance(name, str) or name not in TOOL_WHITELIST:
            return RiskAssessment(RISK_HIGH, [f"未知工具 {name!r}"], str(name))
        if not isinstance(args, dict):
            return RiskAssessment(RISK_HIGH, ["工具参数不是 JSON 对象"], name)
        hits = _scan_injection(args)
        if hits:
            return RiskAssessment(
                RISK_HIGH, ["参数命中危险指令黑名单：" + "、".join(hits)], name
            )

        if name in LOW_RISK_TOOLS:
            return RiskAssessment(RISK_LOW, [f"{name} 为只读分析类工具，默认 low 风险"], name)

        if name == "query_data":
            reasons = ["query_data 为只读查询，默认 medium 风险"]
            level = RISK_MEDIUM
            sql = args.get("sql", "")
            if isinstance(sql, str) and SENSITIVE_COLUMN_RE.search(sql):
                rows = _requested_query_limit(args)
                if rows > SENSITIVE_ROW_THRESHOLD:
                    level = RISK_HIGH
                    reasons.append(
                        f"SQL 涉及敏感字段 name/phone 且预计返回 {rows} 行"
                        f"（> {SENSITIVE_ROW_THRESHOLD}），升级为 high 需人工审批"
                    )
                else:
                    reasons.append(
                        f"SQL 涉及敏感字段 name/phone，但返回行数受限（{rows} ≤ "
                        f"{SENSITIVE_ROW_THRESHOLD}），维持 medium"
                    )
            return RiskAssessment(level, reasons, name)

        if name == "export_report":
            reasons = ["export_report 为导出类工具，默认 medium 且需要人工确认"]
            level = RISK_MEDIUM
            if bool(args.get("include_all_rows")):
                level = RISK_HIGH
                reasons.append("include_all_rows=true：全量导出可能包含敏感数据，升级为 high")
            return RiskAssessment(level, reasons, name)

        return RiskAssessment(RISK_HIGH, ["未配置风险规则的工具，按 high 处理"], name)

    def requires_approval(self, risk: RiskAssessment) -> bool:
        """high → 必须审批；medium 且导出类 → 需要审批；其余不需要."""
        if risk is None:
            return False
        level = str(getattr(risk, "level", risk)).lower()
        if level == RISK_HIGH:
            return True
        if level != RISK_MEDIUM:
            return False
        tool = str(getattr(risk, "tool", "") or "")
        reasons = " ".join(str(item) for item in (getattr(risk, "reasons", None) or []))
        haystack = f"{tool} {reasons}".lower()
        return any(marker in haystack for marker in ("export", "导出"))

    # ------------------------------------------------------------------ 审计
    def record_audit(
        self,
        *,
        trace_id: str,
        tool: str,
        args: dict,
        risk: Any,
        decision: str,
        reason: str = "",
    ) -> Path:
        """追加一行审计 JSON（不落原始敏感参数），返回审计文件路径.

        ``risk`` 既接受 :class:`RiskAssessment`，也接受 ``"low"/"medium"/"high"`` 字符串
        （agent 断点续跑时 pending 里存的是 level 字符串）。
        """
        risk_level = str(getattr(risk, "level", risk))
        entry = {
            "ts": _now_iso(),
            "trace_id": str(trace_id),
            "tool": str(tool),
            "args_digest": args_digest(args),
            "risk": risk_level,
            "decision": str(decision),
            "reason": str(reason)[:MAX_REASON_CHARS],
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return self.audit_path


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "ApprovalRequired",
    "RiskAssessment",
    "ToolGuard",
    "detect_injection",
    "args_digest",
    "TOOL_WHITELIST",
    "INJECTION_PATTERNS",
    "LOW_RISK_TOOLS",
    "MEDIUM_RISK_TOOLS",
    "RISK_LEVELS",
    "SENSITIVE_ROW_THRESHOLD",
    "AUDIT_DECISIONS",
]
