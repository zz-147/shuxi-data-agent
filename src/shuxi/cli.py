"""命令行入口：python -m shuxi ..."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import AgentError, AgentResult, ApprovalRequest, DataAnalysisAgent
from .config import Settings
from .guard import ApprovalRequired, ToolGuard
from .llm import MockProvider, ProviderError, build_provider
from .store import DemoStore
from .trace import TraceRecorder, list_traces, replay_trace

DEFAULT_SCRIPT = "examples/mock_transcript.json"


def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，输出 ¥/中文混排会抛 UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 非标准流
            pass



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shuxi",
        description="数析 · 智能数据分析平台（自然语言 -> 受控工具 -> 结论/图表/报告）",
    )
    parser.add_argument("query", nargs="?", help="自然语言分析问题")
    parser.add_argument("--mock", action="store_true", help="离线演示模式（MockProvider 剧本）")
    parser.add_argument(
        "--mock-script",
        default=None,
        help=f"Mock 剧本路径（默认 {DEFAULT_SCRIPT}）",
    )
    parser.add_argument("--provider", help="覆盖 provider：mock / dashscope")
    parser.add_argument("--yes", action="store_true", help="自动批准所有 HITL 高风险动作")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--trace", action="store_true", help="分析结束后打印 trace 时间线")
    parser.add_argument("--replay", metavar="TRACE_ID", help="回放指定 trace")
    parser.add_argument("--list-traces", action="store_true", help="列出最近的 trace")
    parser.add_argument("--list-tools", action="store_true", help="列出所有受控工具")
    return parser


def _print_result(result: AgentResult) -> None:
    print("\n===== 分析计划 =====")
    if result.plan:
        for step in result.plan:
            print(f"  {step['step']}. [{step['tool']}] {step['reason']}")
    else:
        print("  （模型未给出计划，直接执行）")

    print("\n===== 执行步骤 =====")
    if result.steps:
        for index, step in enumerate(result.steps, start=1):
            flag = "OK " if step.get("ok") else "ERR"
            print(f"  {index}. {flag} [{step['tool']}] {step.get('summary', '')}")
    else:
        print("  （无工具调用）")

    print("\n===== 最终结论 =====")
    print(result.answer or "（空）")

    print("\n===== 产物 =====")
    if result.artifacts:
        for path in result.artifacts:
            print(f"  - {path}")
    else:
        print("  （无文件产物）")

    print("\n===== Trace =====")
    print(f"  trace_id : {result.trace.get('trace_id', '')}")
    if result.trace_path:
        print(f"  file     : {result.trace_path}")
    if result.risk_summary:
        print(f"  risk     : {result.risk_summary}")
    print(f"  status   : {result.status}")


def _interactive_approval(request: ApprovalRequest) -> bool:
    print("\n[人工审批] 高风险操作需要确认")
    print(f"  工具: {request.tool}")
    print(f"  风险: {request.risk} ({'; '.join(request.reasons) or '未说明'})")
    print(f"  参数: {json.dumps(request.args, ensure_ascii=False)}")
    try:
        answer = input("  批准执行？[y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    args = build_parser().parse_args(argv)
    settings = Settings.from_env(provider=args.provider).ensure_dirs()

    if args.list_tools:
        from .tools import get_tools

        for name, spec in get_tools().items():
            print(f"{name:24s} risk={getattr(spec, 'risk', '?'):6s} {getattr(spec, 'description', '')}")
        return 0

    if args.list_traces:
        traces = list_traces(settings.log_dir)
        if not traces:
            print("暂无 trace")
            return 0
        for item in traces:
            print(
                f"{item['trace_id']}  {item['status']:16s} {item['duration_ms']:>8.1f}ms  {item['query'][:60]}"
            )
        return 0

    if args.replay:
        try:
            print(replay_trace(args.replay, settings.log_dir))
            return 0
        except FileNotFoundError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2

    if not args.query:
        print("错误: 缺少分析问题。示例：python -m shuxi \"查询 2025 年 9 月华东区销售额\" --mock", file=sys.stderr)
        return 2

    # provider
    try:
        if args.mock or settings.provider.lower() == "mock":
            script_path = Path(args.mock_script) if args.mock_script else settings.root_dir / DEFAULT_SCRIPT
            provider = MockProvider.from_file(script_path) if script_path.exists() else MockProvider()
        else:
            provider = build_provider(settings)
    except (ProviderError, ValueError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    audit_path = settings.log_dir / "audit.jsonl"
    try:
        store = DemoStore(settings.db_path)
        guard = ToolGuard(settings, audit_path=audit_path)
        trace = TraceRecorder(settings.log_dir, query=args.query)
        agent = DataAnalysisAgent(settings, provider, store, guard, trace)
    except FileNotFoundError as exc:
        print(f"错误: {exc}\n提示：先运行 python scripts/init_demo_data.py 生成演示数据。", file=sys.stderr)
        return 2

    approve = (lambda _request: True) if args.yes else None
    if approve is None and sys.stdin.isatty():
        approve = _interactive_approval
    try:
        result = agent.run(args.query, approve=approve)
        if result.status == "awaiting_approval":
            if approve is None:
                print("存在高风险操作，当前为非交互环境，未自动批准。", file=sys.stderr)
                print(json.dumps(result.pending_approval, ensure_ascii=False), file=sys.stderr)
                return 3
            # 交互审批流：终端里逐次确认
            while result.status == "awaiting_approval":
                assert result.pending_approval is not None
                request = ApprovalRequest(
                    trace_id=result.trace["trace_id"],
                    tool=result.pending_approval["tool"],
                    args=result.pending_approval["args"],
                    risk=result.pending_approval["risk"],
                    reasons=result.pending_approval.get("reasons", []),
                )
                result = agent.resume(_interactive_approval(request))
    except AgentError as exc:
        print(f"分析失败: {exc}", file=sys.stderr)
        return 3
    except ApprovalRequired as exc:  # 理论上由 agent 内部处理，兜底
        print(f"审批异常: {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2, default=str))
    else:
        _print_result(result)

    if args.trace and result.trace.get("trace_id"):
        print("\n===== Trace 回放 =====")
        print(replay_trace(result.trace["trace_id"], settings.log_dir))

    return 0 if result.status == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
