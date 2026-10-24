"""数析 · 智能数据分析平台 —— Streamlit 前端。

启动方式（在仓库根目录执行）：
    .venv\\Scripts\\streamlit.exe run app.py --server.headless true --server.port 8601

界面能力：
1. 自然语言提问 -> DataAnalysisAgent 的 ReAct 循环 -> 执行计划 / 工具调用明细 / 最终结论；
2. 每一步工具调用的工具名、参数、结果摘要、耗时（数据来自 trace span）；
3. 产物预览与下载：SVG 图表（line / bar / pie）、Markdown / HTML 报告；
4. 高风险操作的 Human-in-the-Loop 审批：status=awaiting_approval 时展示风险与参数，
   「批准 / 拒绝」两个按钮分别调用 agent.resume(True/False)；
5. trace_id、trace JSON、「查看 trace JSON」expander、历史 trace 列表与 replay 回放；
6. 侧边栏：provider 状态、演示数据概览（DemoStore.list_tables）、工具清单（tool_schemas）。

说明：Agent 实例与运行结果都放在 st.session_state 里，Streamlit 每次交互重跑脚本时不会丢失
待审批状态；页面刷新（新会话）后历史 trace 仍可从磁盘回放。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shuxi.agent import AgentError, AgentResult, DataAnalysisAgent  # noqa: E402
from shuxi.config import Settings  # noqa: E402
from shuxi.guard import ToolGuard  # noqa: E402
from shuxi.llm import MockProvider, ProviderError, build_provider  # noqa: E402
from shuxi.store import DemoStore  # noqa: E402
from shuxi.tools import tool_schemas  # noqa: E402
from shuxi.trace import TraceRecorder, list_traces, load_trace, replay_trace  # noqa: E402

EXAMPLES_DIR = ROOT / "examples"
MOCK_SCENARIOS = {
    "完整分析（计划 → 多个工具 → 结论）": "mock_transcript.json",
    "高风险审批（HITL 断点演示）": "mock_transcript_hitl.json",
    "空剧本（直接给结论）": None,
}
EXAMPLE_QUERIES = [
    "查询 2025 年 9 月华东区销售额，并和上月对比",
    "分析 2025 年第四季度各区域销售额占比，并生成图表",
    "导出 2025 年客户全量明细报告",
]
STATUS_BADGE = {
    "completed": ("✅ 分析完成", "success"),
    "awaiting_approval": ("⏳ 等待人工审批（高风险操作已挂起）", "warning"),
    "rejected": ("⛔ 审批被拒绝，工具链已终止", "warning"),
    "failed": ("❌ 分析失败", "error"),
}


# ---------------------------------------------------------------- 基础设施


def _init_session() -> None:
    defaults = {
        "agent": None,
        "result": None,
        "current_query": "",
        "plan_preview": None,
        "last_error": "",
        "query_input": EXAMPLE_QUERIES[0],
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _build_settings(provider: str, auto_approve: bool = False) -> Settings:
    settings = Settings.from_env(provider=provider, auto_approve=auto_approve)
    return settings.ensure_dirs()


def _make_provider(settings: Settings, scenario_label: str):
    """mock 模式按选中的剧本构造 MockProvider；dashscope 模式走真实 HTTP。"""
    if settings.provider.lower() != "mock":
        return build_provider(settings)

    script_file = MOCK_SCENARIOS.get(scenario_label)
    if script_file:
        path = EXAMPLES_DIR / script_file
        if path.exists():
            return MockProvider.from_file(path)
        st.warning(f"Mock 剧本不存在：{path}，本次将直接返回占位结论。")
    return MockProvider()


def _open_store(settings: Settings) -> DemoStore:
    """打开只读演示库；调用方负责 close（或交给 Agent 持有到本次运行结束）。"""
    store = DemoStore(settings.db_path)
    store.list_tables()  # 立即触发连接校验，数据缺失时尽早报错
    return store


def _rerun() -> None:
    if hasattr(st, "rerun"):
        st.rerun()
    else:  # pragma: no cover - 兼容老版本 Streamlit
        st.experimental_rerun()


def _display_path(raw: str) -> Path:
    path = Path(str(raw))
    return path if path.is_absolute() else (ROOT / path)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- 侧边栏


def _render_provider_status(provider: str, settings: Settings) -> None:
    if provider == "mock":
        st.success("MockProvider · 离线演示模式，无需网络与 API Key")
    elif settings.api_key:
        st.success(f"DashScope · {settings.model}（API Key 已配置）")
    else:
        st.error("DashScope 缺少 API Key：请在 .env 配置 DASHSCOPE_API_KEY，或切回 mock")


def _render_data_overview(settings: Settings) -> None:
    if not settings.db_path.exists():
        st.warning(
            "演示数据库不存在：`data/demo.db`\n\n"
            "先执行 `python scripts/init_demo_data.py` 生成演示数据。"
        )
        return
    try:
        store = _open_store(settings)
        try:
            tables = store.list_tables()
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - 侧边栏不能因数据问题炸掉整个页面
        st.error(f"读取演示数据失败：{type(exc).__name__}: {exc}")
        return

    st.dataframe(
        [
            {
                "表名": table["name"],
                "行数": table.get("row_count", 0),
                "说明": table.get("description", ""),
            }
            for table in tables
        ],
        use_container_width=True,
        hide_index=True,
    )
    with st.expander("字段明细", expanded=False):
        for table in tables:
            columns = ", ".join(str(col.get("name")) for col in table.get("columns", []))
            st.markdown(f"**{table['name']}**（{table.get('row_count', 0)} 行）：{columns}")


def _render_tool_inventory() -> None:
    try:
        schemas = tool_schemas()
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取工具清单失败：{exc}")
        return
    for spec in schemas:
        with st.expander(str(spec.get("name", "unknown")), expanded=False):
            st.caption(str(spec.get("description", "")))
            st.json(spec.get("parameters", {}))


def _render_sidebar() -> tuple[str, bool, str]:
    with st.sidebar:
        st.title("📊 数析")
        st.caption("可控 ReAct 数据分析 Agent · 只读数仓 · 全链路 Trace")
        st.divider()

        provider = st.radio(
            "LLM Provider",
            options=["mock", "dashscope"],
            index=0,
            help="mock：离线剧本演示；dashscope：真实调用通义千问（需 API Key）",
        )
        scenario = st.selectbox(
            "Mock 演示剧本",
            options=list(MOCK_SCENARIOS.keys()),
            index=0,
            disabled=provider != "mock",
            help="剧本决定 mock 模式下模型每一步的输出，便于复现完整分析/审批流程",
        )
        auto_approve = st.checkbox(
            "自动批准高风险操作（仅演示）",
            value=False,
            help="勾选后跳过人工审批，直接执行中/高风险工具",
        )

        base_settings = _build_settings(provider)
        _render_provider_status(provider, base_settings)

        st.divider()
        st.subheader("演示数据概览")
        _render_data_overview(base_settings)

        st.divider()
        st.subheader("工具清单（6 个）")
        _render_tool_inventory()
    return provider, auto_approve, scenario


# ---------------------------------------------------------------- 运行 Agent


def _try_run(query: str, provider: str, auto_approve: bool, scenario: str) -> None:
    if not query.strip():
        st.warning("请先输入分析问题。")
        return

    settings = _build_settings(provider, auto_approve)
    try:
        store = _open_store(settings)
    except Exception as exc:  # noqa: BLE001
        st.error(
            f"演示数据不可用：{type(exc).__name__}: {exc}\n\n"
            "请先在仓库根目录执行 `python scripts/init_demo_data.py`。"
        )
        return

    provider_impl = None
    try:
        provider_impl = _make_provider(settings, scenario)
        trace = TraceRecorder(settings.log_dir, query=query)
        agent = DataAnalysisAgent(
            settings=settings,
            provider=provider_impl,
            store=store,
            guard=ToolGuard(settings),
            trace=trace,
        )
        with st.spinner("Agent 正在规划并调用工具（mock 剧本约 1~3 秒）..."):
            result = agent.run(query, approve=None)
    except (AgentError, ProviderError) as exc:
        st.error(f"运行失败：{exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 页面层兜底，避免白屏
        st.error(f"运行异常：{type(exc).__name__}: {exc}")
        st.exception(exc)
        return

    st.session_state["agent"] = agent
    st.session_state["result"] = result
    st.session_state["current_query"] = query
    st.session_state["plan_preview"] = None


def _try_preview_plan(query: str, provider: str, scenario: str) -> None:
    if not query.strip():
        st.warning("请先输入分析问题。")
        return
    settings = _build_settings(provider)
    try:
        store = _open_store(settings)
    except Exception as exc:  # noqa: BLE001
        st.error(f"演示数据不可用：{type(exc).__name__}: {exc}")
        return
    try:
        agent = DataAnalysisAgent(
            settings=settings,
            provider=_make_provider(settings, scenario),
            store=store,
            guard=ToolGuard(settings),
            trace=TraceRecorder(settings.log_dir, query=query),
        )
        with st.spinner("生成计划中..."):
            st.session_state["plan_preview"] = agent.plan_only(query)
    except Exception as exc:  # noqa: BLE001
        st.error(f"计划预览失败：{type(exc).__name__}: {exc}")


def _handle_approval(approved: bool) -> None:
    agent = st.session_state.get("agent")
    if agent is None:
        st.error("没有可继续的 Agent 实例，请重新发起分析。")
        return
    try:
        with st.spinner("正在执行人工决策并继续工具链..."):
            result = agent.resume(approved)
    except AgentError as exc:
        st.error(f"续跑失败：{exc}")
        return
    except Exception as exc:  # noqa: BLE001
        st.error(f"续跑异常：{type(exc).__name__}: {exc}")
        return
    st.session_state["result"] = result
    _rerun()


# ---------------------------------------------------------------- 结果展示


def _tool_call_rows(trace: dict) -> list[dict]:
    rows: list[dict] = []
    for span in trace.get("spans", []):
        if span.get("kind") != "tool":
            continue
        attrs = span.get("attrs") or {}
        ok = attrs.get("ok")
        rows.append(
            {
                "工具": span.get("name", ""),
                "参数": attrs.get("args", ""),
                "结果摘要": attrs.get("summary", ""),
                "状态": "成功" if ok else "失败/未知",
                "耗时(ms)": round(float(span.get("duration_ms", 0.0) or 0.0), 1),
            }
        )
    return rows


def _timeline_rows(trace: dict) -> list[dict]:
    rows: list[dict] = []
    for span in trace.get("spans", []):
        rows.append(
            {
                "类型": span.get("kind", ""),
                "名称": span.get("name", ""),
                "开始(ms)": round(float(span.get("start_ms", 0.0) or 0.0), 1),
                "耗时(ms)": round(float(span.get("duration_ms", 0.0) or 0.0), 1),
                "状态": span.get("status", ""),
            }
        )
    return rows


def _render_plan(result: AgentResult) -> None:
    st.subheader("① 执行计划")
    if not result.plan:
        st.caption("本次运行没有显式计划（模型未返回 plan 字段，或计划为空）。")
        return
    st.dataframe(
        [
            {
                "步骤": item.get("step", index),
                "工具": item.get("tool", ""),
                "理由": item.get("reason", ""),
            }
            for index, item in enumerate(result.plan, start=1)
        ],
        use_container_width=True,
        hide_index=True,
    )


def _render_steps(result: AgentResult) -> None:
    st.subheader("② 工具调用明细")
    trace = result.trace or {}
    rows = _tool_call_rows(trace)
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    elif result.steps:
        # trace 缺失时退回 AgentResult.steps（信息更少，但保证有展示）
        st.dataframe(result.steps, use_container_width=True, hide_index=True)
    else:
        st.caption("本次运行没有工具调用。")

    if result.risk_summary:
        risk_text = " · ".join(f"{level}: {count}" for level, count in result.risk_summary.items())
        st.caption(f"风险分布：{risk_text}")

    timeline = _timeline_rows(trace)
    if timeline:
        with st.expander("查看完整 trace 时间线（span 按开始时间）", expanded=False):
            timeline.sort(key=lambda row: row["开始(ms)"])
            st.dataframe(timeline, use_container_width=True, hide_index=True)


def _render_artifact(path: Path) -> None:
    suffix = path.suffix.lower()
    st.markdown(f"**产物**：`{_repo_relative(path)}`")
    try:
        if suffix == ".svg":
            svg_text = path.read_text(encoding="utf-8")
            components.html(svg_text, height=460, scrolling=True)
            st.download_button(
                "⬇️ 下载 SVG 图表",
                data=svg_text,
                file_name=path.name,
                mime="image/svg+xml",
                key=f"dl_svg_{path.name}",
            )
        elif suffix in {".md", ".markdown"}:
            text = path.read_text(encoding="utf-8")
            with st.expander("预览 Markdown 报告", expanded=False):
                st.markdown(text)
            st.download_button(
                "⬇️ 下载 Markdown 报告",
                data=text,
                file_name=path.name,
                mime="text/markdown",
                key=f"dl_md_{path.name}",
            )
        elif suffix in {".html", ".htm"}:
            text = path.read_text(encoding="utf-8")
            with st.expander("预览 HTML 报告", expanded=False):
                components.html(text, height=560, scrolling=True)
            st.download_button(
                "⬇️ 下载 HTML 报告",
                data=text,
                file_name=path.name,
                mime="text/html",
                key=f"dl_html_{path.name}",
            )
        else:
            st.info("该产物类型暂不支持在线预览。")
            st.download_button(
                "⬇️ 下载产物",
                data=path.read_bytes(),
                file_name=path.name,
                key=f"dl_other_{path.name}",
            )
    except OSError as exc:
        st.warning(f"产物读取失败：{exc}")


def _render_artifacts(result: AgentResult) -> None:
    st.subheader("④ 产物")
    if not result.artifacts:
        st.caption("本次运行没有生成图表/报告产物。")
        return
    for raw in result.artifacts:
        path = _display_path(raw)
        if not path.exists():
            st.warning(f"产物文件不存在：{raw}")
            continue
        _render_artifact(path)


def _render_approval_panel(result: AgentResult) -> None:
    pending = result.pending_approval or {}
    st.subheader("③ 最终答案")
    st.caption("高风险操作已挂起，人工审批通过后 Agent 才会继续执行；下面是待审批的动作详情。")

    col_risk, col_tool = st.columns([1, 2])
    col_risk.metric("风险等级", str(pending.get("risk", "unknown")).upper())
    col_tool.metric("待审批工具", str(pending.get("tool", "unknown")))

    reasons = pending.get("reasons") or []
    if reasons:
        st.markdown("**风险原因**")
        for reason in reasons:
            st.markdown(f"- {reason}")

    with st.expander("查看待审批参数", expanded=True):
        st.json(pending.get("args", {}))

    left, right = st.columns(2)
    if left.button("✅ 批准执行", type="primary", use_container_width=True, key="approve_button"):
        _handle_approval(True)
    if right.button("⛔ 拒绝执行", use_container_width=True, key="reject_button"):
        _handle_approval(False)


def _render_trace(result: AgentResult) -> None:
    st.subheader("⑤ 运行 Trace")
    trace = result.trace or {}
    trace_id = str(trace.get("trace_id") or Path(result.trace_path).stem or "-")
    col_id, col_path = st.columns([1, 2])
    col_id.code(trace_id, language=None)
    col_path.caption(f"trace 文件：{result.trace_path or '（未落盘）'}")

    with st.expander("查看 trace JSON", expanded=False):
        st.json(trace)
        st.download_button(
            "⬇️ 下载 trace JSON",
            data=json.dumps(trace, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name=f"{trace_id}.json",
            mime="application/json",
            key=f"dl_trace_{trace_id}",
        )


def _render_result(result: AgentResult) -> None:
    label, level = STATUS_BADGE.get(result.status, (f"状态：{result.status}", "info"))
    getattr(st, level, st.info)(label)
    if result.error:
        st.caption(f"error：{result.error}")

    _render_plan(result)
    _render_steps(result)

    if result.status == "awaiting_approval":
        _render_approval_panel(result)
    else:
        st.subheader("③ 最终答案")
        st.markdown(result.answer or "_（本次运行没有生成最终结论）_")

    _render_artifacts(result)
    _render_trace(result)


# ---------------------------------------------------------------- 历史 trace / 计划预览


def _render_plan_preview() -> None:
    preview = st.session_state.get("plan_preview")
    if not preview:
        return
    st.subheader("🧭 计划预览（未执行工具）")
    plan = preview.get("plan") or []
    if preview.get("thought"):
        st.caption(f"模型思考：{preview['thought']}")
    if plan:
        st.dataframe(
            [
                {
                    "步骤": item.get("step", index),
                    "工具": item.get("tool", ""),
                    "理由": item.get("reason", ""),
                }
                for index, item in enumerate(plan, start=1)
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("模型没有返回可用的计划。")
    with st.expander("原始响应", expanded=False):
        st.json(preview.get("raw", {}))
    if st.button("清除计划预览", key="clear_plan_preview"):
        st.session_state["plan_preview"] = None
        _rerun()


def _render_history(settings: Settings) -> None:
    with st.expander("⑥ 历史 Trace 与回放", expanded=False):
        try:
            traces = list_traces(settings.log_dir, limit=20)
        except Exception as exc:  # noqa: BLE001
            st.error(f"读取历史 trace 失败：{exc}")
            return
        if not traces:
            st.caption(f"暂无历史 trace（目录：{settings.log_dir / 'traces'}）。执行一次分析后即可回放。")
            return

        st.dataframe(
            [
                {
                    "trace_id": item.get("trace_id", ""),
                    "问题": item.get("query", ""),
                    "状态": item.get("status", ""),
                    "耗时(ms)": round(float(item.get("duration_ms", 0.0) or 0.0), 1),
                    "开始时间": item.get("started_at", ""),
                }
                for item in traces
            ],
            use_container_width=True,
            hide_index=True,
        )

        options = {
            f"{item.get('trace_id', '')} · {item.get('status', '')} · "
            f"{str(item.get('query', ''))[:40]}": item.get("trace_id", "")
            for item in traces
        }
        selected_label = st.selectbox("选择要回放的 trace", options=list(options.keys()))
        trace_id = options[selected_label]
        col_replay, col_json = st.columns(2)
        if col_replay.button("▶️ 回放时间线", use_container_width=True, key="replay_button"):
            try:
                st.session_state["replay_text"] = replay_trace(trace_id, settings.log_dir)
            except Exception as exc:  # noqa: BLE001
                st.error(f"回放失败：{exc}")
        if col_json.button("🧾 查看该 trace JSON", use_container_width=True, key="trace_json_button"):
            try:
                st.session_state["history_trace_json"] = load_trace(trace_id, settings.log_dir)
            except Exception as exc:  # noqa: BLE001
                st.error(f"读取失败：{exc}")

        if st.session_state.get("replay_text"):
            st.code(st.session_state["replay_text"], language=None)
        if st.session_state.get("history_trace_json"):
            st.json(st.session_state["history_trace_json"])


# ---------------------------------------------------------------- 页面主体


def main() -> None:
    st.set_page_config(
        page_title="数析 · 智能数据分析平台",
        page_icon="📊",
        layout="wide",
    )
    _init_session()
    base_settings = _build_settings("mock")
    provider, auto_approve, scenario = _render_sidebar()

    st.title("📊 数析 · 智能数据分析平台")
    st.caption(
        "自然语言 → ReAct Agent 规划 → 6 个只读分析工具（schema / SQL / 指标 / 归因 / 图表 / 报告）"
        "→ 结论与产物；全链路 trace 可回放，高风险操作走人工审批。"
        " 演示模式使用 MockProvider 剧本，不依赖网络与大模型 Key。"
    )

    with st.form("analysis_form", clear_on_submit=False):
        st.text_area(
            "分析问题",
            key="query_input",
            height=90,
            placeholder="例如：查询 2025 年 9 月华东区销售额，并和上月对比",
        )
        col_run, col_plan = st.columns([1, 1])
        submitted = col_run.form_submit_button(
            "🚀 开始分析", type="primary", use_container_width=True
        )
        preview = col_plan.form_submit_button("🧭 只生成计划（不执行工具）", use_container_width=True)

    st.caption("示例问题：" + " ｜ ".join(EXAMPLE_QUERIES))

    if submitted:
        _try_run(st.session_state["query_input"], provider, auto_approve, scenario)
    elif preview:
        _try_preview_plan(st.session_state["query_input"], provider, scenario)

    _render_plan_preview()

    result = st.session_state.get("result")
    if result is None:
        st.info(
            "输入问题后点击「开始分析」。Mock 模式下将按 `examples/mock_transcript.json` "
            "剧本执行：计划 → 工具调用 → 结论 → 图表/报告产物。"
        )
    else:
        st.divider()
        _render_result(result)

    st.divider()
    _render_history(base_settings)

    st.caption(
        "提示：高风险操作（如全量导出客户敏感字段）会返回 awaiting_approval，"
        "在上方审批面板选择「批准 / 拒绝」后 Agent 通过 resume() 继续或终止。"
    )


main()
