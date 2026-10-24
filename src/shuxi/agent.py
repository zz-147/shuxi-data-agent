"""数析 Agent：可控 ReAct 循环 + 计划 + 工具白名单 + 风险分级 + HITL 断点续跑.

设计要点（面试可讲）：
1. 不让模型直接写 Python 执行，只让它输出「工具名 + JSON 参数」，由工具层做白名单/参数校验；
2. 先出计划再执行，计划里的工具名先过白名单，再算风险等级；
3. 高风险动作走 Human-in-the-Loop：无审批回调时挂起（awaiting_approval），可 resume；
4. 全链路 span/event 落盘，可 replay；
5. 模型输出用三级容错 JSON 解析，解析失败把错误回灌给模型重试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .config import Settings
from .json_parser import JSONParseError, parse_json_lenient
from .llm import LLMProvider, ProviderError
from .trace import TraceRecorder

APPROVAL_RISK_LEVELS = {"medium", "high"}


class AgentError(RuntimeError):
    """Agent 无法继续执行的错误（配置、协议、步数上限等）."""


@dataclass(frozen=True)
class ApprovalRequest:
    trace_id: str
    tool: str
    args: dict
    risk: str
    reasons: list[str]


@dataclass
class AgentResult:
    status: str  # completed | awaiting_approval | rejected | failed
    answer: str = ""
    plan: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    trace: dict = field(default_factory=dict)
    trace_path: str = ""
    pending_approval: dict | None = None
    error: str = ""
    risk_summary: dict = field(default_factory=dict)


class DataAnalysisAgent:
    """自然语言 -> 计划 -> 工具调用 -> 结论的数据分析智能体."""

    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        store: Any,
        guard: Any,
        trace: TraceRecorder,
        tools: dict[str, Any] | None = None,
    ):
        self.settings = settings
        self.provider = provider
        self.store = store
        self.guard = guard
        self.trace = trace
        self._custom_tools = tools is not None
        if tools is None:
            from .tools import get_tools  # 延迟导入，便于单独测试 agent

            tools = get_tools()
        self.tools = tools
        self._state: dict | None = None

    # ------------------------------------------------------------------ 公共 API

    def run(
        self,
        query: str,
        approve: Callable[[ApprovalRequest], bool] | None = None,
    ) -> AgentResult:
        """执行一次完整分析；approve 为 None 时遇到高风险动作会挂起等待 resume."""
        if not query or not query.strip():
            raise AgentError("query 不能为空")
        self.trace.query = query.strip()
        self._state = {
            "messages": self._initial_messages(query.strip()),
            "plan": [],
            "steps": [],
            "artifacts": [],
            "iteration": 0,
            "pending": None,
            "risk_summary": {},
            "answer": "",
            "error": "",
        }
        return self._loop(approve)

    def resume(self, approved: bool) -> AgentResult:
        """对挂起的审批做决定并继续执行."""
        state = self._state
        if state is None or state.get("pending") is None:
            raise AgentError("当前没有待审批的动作")
        pending = state["pending"]
        decision = "approved" if approved else "rejected"
        self.guard.record_audit(
            trace_id=self.trace.trace_id,
            tool=pending["action"],
            args=pending["args"],
            risk=pending["risk"],
            decision=decision,
            reason="resume() 人工决定",
        )
        self.trace.event(
            "hitl_decision",
            tool=pending["action"],
            risk=pending["risk"],
            approved=approved,
        )
        state["pending"] = None
        if not approved:
            state["error"] = "用户拒绝了高风险操作，工具链终止"
            return self._finish("rejected")

        # 审批通过：补执行挂起的动作，再继续循环
        result = self._execute_tool(pending["action"], pending["args"])
        self._append_observation(pending["raw"], pending["action"], result)
        for path in self._relative_artifacts(self._extract_artifacts(result)):
            if path not in state["artifacts"]:
                state["artifacts"].append(path)
        return self._loop(None)

    def plan_only(self, query: str) -> dict:
        """只生成计划不执行，供 Streamlit 预览使用."""
        self.trace.query = query
        messages = self._initial_messages(query)
        payload, level = self._ask(messages)
        plan = self._normalize_plan(payload.get("plan"))
        self.trace.event("plan_preview", level=level, steps=len(plan))
        return {"plan": plan, "thought": payload.get("thought", ""), "raw": payload}

    # ------------------------------------------------------------------ 内部循环

    def _loop(self, approve: Callable[[ApprovalRequest], bool] | None) -> AgentResult:
        state = self._state
        assert state is not None

        while state["iteration"] < self.settings.max_steps:
            state["iteration"] += 1
            payload, level = self._ask(state["messages"])
            state["messages"].append(
                {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}
            )
            self.trace.event(
                "agent_thought",
                iteration=state["iteration"],
                parse_level=level,
                thought=str(payload.get("thought", ""))[:300],
            )

            if payload.get("plan") and not state["plan"]:
                with self.trace.span("plan", "derive_plan") as attrs:
                    state["plan"] = self._normalize_plan(payload["plan"])
                    attrs["steps"] = [step["tool"] for step in state["plan"]]
                self.trace.event(
                    "plan_ready",
                    steps=[step["tool"] for step in state["plan"]],
                )

            if payload.get("final_answer"):
                state["answer"] = str(payload["final_answer"])
                return self._finish("completed")

            action = payload.get("action")
            action_input = payload.get("action_input") or {}
            if not action or not isinstance(action, str):
                self._append_error(
                    "响应既没有 final_answer 也没有合法的 action，请按协议只输出 JSON。"
                )
                continue

            ok, reason = self.guard.validate_call(action, action_input)
            if not ok:
                self.trace.event("tool_rejected", tool=action, reason=reason)
                self._append_error(f"工具调用被拒绝: {reason}")
                continue

            risk = self.guard.assess_risk(action, action_input)
            self._count_risk(state, risk.level)

            if self.settings.auto_approve and risk.level in APPROVAL_RISK_LEVELS:
                self.trace.event("auto_approve", tool=action, risk=risk.level)
                approve = lambda _request: True  # noqa: E731 - 演示用自动批准

            if self.guard.requires_approval(risk):
                request = ApprovalRequest(
                    trace_id=self.trace.trace_id,
                    tool=action,
                    args=action_input,
                    risk=risk.level,
                    reasons=list(getattr(risk, "reasons", []) or []),
                )
                if approve is None:
                    state["pending"] = {
                        "action": action,
                        "args": action_input,
                        "risk": risk.level,
                        "raw": json.dumps(payload, ensure_ascii=False),
                    }
                    return self._finish("awaiting_approval", pending=request)
                decision = self._ask_human(request, approve)
                self.guard.record_audit(
                    trace_id=self.trace.trace_id,
                    tool=action,
                    args=action_input,
                    risk=risk.level,
                    decision="approved" if decision else "rejected",
                    reason="HITL 审批",
                )
                if not decision:
                    state["error"] = "用户拒绝了高风险操作，工具链终止"
                    return self._finish("rejected")

            result = self._execute_tool(action, action_input)
            self._append_observation(json.dumps(payload, ensure_ascii=False), action, result)
            artifacts = self._relative_artifacts(self._extract_artifacts(result))
            for path in artifacts:
                if path not in state["artifacts"]:
                    state["artifacts"].append(path)

        state["error"] = f"超过最大步数 {self.settings.max_steps}，仍未得到最终结论"
        return self._finish("failed")

    # ------------------------------------------------------------------ LLM 交互

    def _ask(self, messages: list[dict]) -> tuple[dict, str]:
        """调用 LLM 并用三级容错解析；解析失败时把错误回灌后重试一次."""
        last_error = ""
        for attempt in range(2):
            try:
                with self.trace.span("llm", f"complete#{attempt + 1}") as attrs:
                    response = self.provider.complete(
                        messages, temperature=self.settings.temperature
                    )
                    attrs["chars"] = len(response.content or "")
                    attrs["usage"] = response.usage
            except ProviderError as exc:
                self.trace.event("llm_error", error=str(exc))
                raise AgentError(str(exc)) from exc

            try:
                payload, level = parse_json_lenient(response.content or "")
                if level == "fallback" or not isinstance(payload, dict):
                    raise JSONParseError(
                        "模型输出无法解析为 JSON 对象（三级容错均未命中）"
                    )
                self.trace.event("llm_parse", level=level)
                return payload, level
            except JSONParseError as exc:
                last_error = str(exc)
                self.trace.event("llm_parse_error", error=last_error, attempt=attempt + 1)
                messages.append({"role": "assistant", "content": response.content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "你上次的输出不是合法 JSON：" + last_error + "。"
                        "请严格只输出一个 JSON 对象（不要代码块、不要解释）。",
                    }
                )
        raise AgentError("模型连续两次输出无法解析为 JSON: " + last_error)

    # ------------------------------------------------------------------ 工具执行

    def _execute_tool(self, action: str, action_input: dict) -> dict:
        if self._custom_tools:
            # 测试/嵌入式场景：直接把最小上下文交给自定义工具，不依赖 tools 包的注册表
            ctx = SimpleNamespace(
                store=self.store,
                settings=self.settings,
                trace=self.trace,
                artifacts_dir=self.settings.artifacts_dir,
            )
            runner = self.tools[action].handler
        else:
            from .tools import ToolContext, execute_tool as execute_registered

            ctx = ToolContext(
                store=self.store,
                settings=self.settings,
                trace=self.trace,
                artifacts_dir=self.settings.artifacts_dir,
            )
            runner = lambda args, context: execute_registered(action, args, context)  # noqa: E731

        self.trace.event("tool_start", tool=action, args=_preview(action_input))
        try:
            with self.trace.span("tool", action, args=_preview(action_input)) as attrs:
                result = runner(action_input, ctx)
                attrs["ok"] = bool(result.get("ok")) if isinstance(result, dict) else False
                attrs["summary"] = str(result.get("summary", ""))[:300] if isinstance(result, dict) else ""
        except Exception as exc:  # noqa: BLE001 - 工具异常不炸整条链，回灌给模型
            self.trace.event("tool_error", tool=action, error=f"{type(exc).__name__}: {exc}")
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.trace.event(
            "tool_end",
            tool=action,
            ok=bool(result.get("ok")) if isinstance(result, dict) else False,
        )
        return result

    # ------------------------------------------------------------------ 收尾

    def _finish(
        self,
        status: str,
        pending: ApprovalRequest | None = None,
    ) -> AgentResult:
        state = self._state
        assert state is not None
        answer = state.get("answer") or ""
        error = state.get("error") or ""
        if status == "completed" and not answer:
            answer = "分析完成，但模型没有给出最终结论。"
        if status == "failed" and not answer:
            answer = f"分析失败：{error or '未知原因'}"

        self.trace.finish(
            status,
            answer=answer,
            artifacts=state["artifacts"],
            error=error,
            steps=len(state["steps"]),
        )
        trace_path = ""
        try:
            trace_path = str(self.trace.save())
        except OSError as exc:  # 磁盘异常不影响结果返回
            self.trace.event("trace_save_error", error=str(exc))

        return AgentResult(
            status=status,
            answer=answer,
            plan=state["plan"],
            steps=state["steps"],
            artifacts=state["artifacts"],
            trace=self.trace.to_dict(),
            trace_path=trace_path,
            pending_approval=(
                {
                    "tool": pending.tool,
                    "args": pending.args,
                    "risk": pending.risk,
                    "reasons": pending.reasons,
                }
                if pending is not None
                else None
            ),
            error=error,
            risk_summary=state["risk_summary"],
        )

    def _append_observation(self, assistant_raw: str, action: str, result: dict) -> None:
        state = self._state
        assert state is not None
        observation = json.dumps(result, ensure_ascii=False)
        if len(observation) > 4000:
            observation = observation[:4000] + "...(已截断)"
        summary = ""
        if isinstance(result, dict):
            summary = str(result.get("summary") or result.get("error") or "")
        state["steps"].append(
            {
                "iteration": state["iteration"],
                "tool": action,
                "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
                "summary": summary[:300],
            }
        )
        state["messages"].append({"role": "assistant", "content": assistant_raw})
        state["messages"].append(
            {
                "role": "user",
                "content": f"工具 {action} 的观察结果（JSON）:\n{observation}\n"
                "请基于该结果继续：需要更多证据就调用下一个工具，否则输出 final_answer。",
            }
        )

    def _append_error(self, error: str) -> None:
        state = self._state
        assert state is not None
        state["messages"].append(
            {"role": "user", "content": f"协议错误：{error}"}
        )

    # ------------------------------------------------------------------ 辅助

    def _ask_human(self, request: ApprovalRequest, approve: Callable[[ApprovalRequest], bool]) -> bool:
        with self.trace.span("hitl", "approval", tool=request.tool, risk=request.risk) as attrs:
            approved = bool(approve(request))
            attrs["approved"] = approved
        return approved

    @staticmethod
    def _count_risk(state: dict, level: str) -> None:
        summary = state["risk_summary"]
        summary[level] = summary.get(level, 0) + 1

    def _normalize_plan(self, plan: Any) -> list[dict]:
        if not isinstance(plan, list):
            return []
        normalized: list[dict] = []
        for index, item in enumerate(plan, start=1):
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool", ""))
            if tool and tool not in self.tools:
                self.trace.event("plan_tool_rejected", tool=tool)
                continue
            normalized.append(
                {
                    "step": index,
                    "tool": tool,
                    "reason": str(item.get("reason", "")),
                }
            )
        return normalized

    @staticmethod
    def _extract_artifacts(result: Any) -> list[str]:
        if not isinstance(result, dict):
            return []
        paths: list[str] = []
        for key in ("path", "artifacts"):
            value = result.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
            elif isinstance(value, list):
                paths.extend(str(item) for item in value if isinstance(item, str) and item)
        return paths

    def _relative_artifacts(self, paths: list[str]) -> list[str]:
        """把仓库内产物路径转成相对路径，trace/输出里不泄露本机绝对路径."""
        root = self.settings.root_dir.resolve()
        relative: list[str] = []
        for raw in paths:
            try:
                relative.append(str(Path(raw).resolve().relative_to(root)).replace("\\", "/"))
            except (OSError, ValueError):
                relative.append(raw)
        return relative

    def _initial_messages(self, query: str) -> list[dict]:
        return [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": f"分析问题：{query}"},
        ]

    def _system_prompt(self) -> str:
        schemas = []
        for name, spec in self.tools.items():
            schemas.append(
                {
                    "name": name,
                    "description": getattr(spec, "description", ""),
                    "parameters": getattr(spec, "parameters", {}),
                }
            )
        return SYSTEM_PROMPT_TEMPLATE.format(tools_json=json.dumps(schemas, ensure_ascii=False, indent=2))


def _preview(data: Any, limit: int = 300) -> str:
    text = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    return text[:limit]


SYSTEM_PROMPT_TEMPLATE = """你是「数析」数据分析智能体，服务对象是不懂 SQL 的业务人员。
你可以使用的工具（只能从下列工具中选，不能编造工具）：
{tools_json}

【工作方式】
1. 第一轮响应必须给出计划 plan（2~5 步），并在同一轮选择第一步 action：
   {{"thought": "简短推理", "plan": [{{"step": 1, "tool": "工具名", "reason": "为什么"}}],
     "action": "工具名", "action_input": {{...}}}}
2. 后续每轮：要么继续调用工具
   {{"thought": "...", "action": "工具名", "action_input": {{...}}}}
   要么给出最终回答
   {{"thought": "...", "final_answer": "给业务人员看的中文结论"}}
3. 只输出一个 JSON 对象，不要 markdown 代码块，不要多余解释。
4. 只能引用工具返回的数据，绝对不要自己编造数字；结论里要写清统计口径（时间范围/筛选条件）。
5. 需要图表或报告时调用对应工具，并在 final_answer 里给出产物路径。
6. 如果工具返回 ok=false，先根据 error 调整参数重试一次；仍失败则在 final_answer 里说明限制。
"""
