# 数析 · 架构与设计说明

本文是「数析」智能数据分析平台的架构与设计决策说明，配套面试讲解使用。目标读者：想快速理解这个项目为什么这么拆的人。

## 1. 一句话架构

```
自然语言问题
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ DataAnalysisAgent（可控 ReAct 循环）                          │
│  ① 生成计划 plan（2~5 步，工具名先过白名单）                    │
│  ② 每一步：LLM 输出 {action, action_input} → 解析 → 校验 → 风险分级 │
│  ③ 低风险直接执行；高风险挂起等待人工审批（HITL）                │
│  ④ 工具结果作为 observation 回填上下文 → 继续循环                │
│  ⑤ 输出 final_answer + 产物路径                                │
└───────────────┬───────────────────────────┬─────────────────┘
                │                           │
                ▼                           ▼
      ┌──────────────────┐        ┌──────────────────────┐
      │ ToolGuard 护栏层   │        │ TraceRecorder 全链路   │
      │ 白名单/参数/注入检测 │        │ span + event + 落盘    │
      │ 风险分级/审计日志   │        │ replay 回放            │
      └────────┬─────────┘        └──────────────────────┘
               ▼
      ┌──────────────────┐
      │ 6 个受控工具       │
      │ schema / SQL 查询  │
      │ 指标 / 异常归因    │
      │ 图表 / 报告导出    │
      └────────┬─────────┘
               ▼
      ┌──────────────────┐
      │ SQLite 演示数仓    │  只读连接 + 单条 SELECT + 表白名单 + LIMIT
      │ stores/products/   │
      │ customers/sales    │
      └──────────────────┘
```

## 2. 为什么这样分层

| 层 | 文件 | 职责 | 面试要点 |
| --- | --- | --- | --- |
| 交互层 | `cli.py` / `app.py`（Streamlit） | 接收问题、展示计划/步骤/产物、处理审批与 trace 回放 | 同一套 agent API 同时服务 CLI 与 Web |
| 编排层 | `agent.py` | ReAct 循环、计划、HITL 断点、结果汇总 | 为什么不用 AgentExecutor 黑盒 |
| 护栏层 | `guard.py` | 工具白名单、参数校验、注入检测、风险分级、审计 | 安全边界不能只靠 prompt |
| 工具层 | `tools/` | 6 个原子分析工具，统一入参/返回契约 | 工具是「动词」，不是函数堆 |
| 数据层 | `store.py` | 只读 SQL 护栏 + schema/profile | 让模型看结构，不让模型碰写操作 |
| 可观测层 | `trace.py` | trace_id 贯穿、span 树、replay | 线上排障与面试自证 |
| 模型层 | `llm.py` | Provider 抽象：Mock / DashScope（OpenAI 兼容） | 可测试性：演示零网络 |

## 3. ReAct 协议与容错

模型每一轮只能输出一个 JSON 对象：

```json
{"thought": "简短推理", "plan": [{"step": 1, "tool": "inspect_schema", "reason": "..."}],
 "action": "inspect_schema", "action_input": {"table": "sales"}}
```

或：

```json
{"thought": "...", "final_answer": "给业务人员看的中文结论"}
```

解析采用**三级容错**（`json_parser.parse_json_lenient`）：

| 级别 | 触发条件 | 处理 |
| --- | --- | --- |
| direct | 输出就是合法 JSON（允许 ```` ```json ```` 包裹） | `json.loads` |
| regex | 输出夹带解释文字 | 正则提取第一个平衡 `{...}` 再解析 |
| fallback | 完全解析不了 | 返回 `{"raw": text}`，由 agent 把错误回灌给模型重试（≤2 次） |

为什么不用 `json.loads` 一把梭：真实模型会输出代码块、前后解释、尾逗号；工程上要么重试要么修复，这里把「修复」和「重试」都做了。

## 4. 工具契约

每个工具：`name / description / parameters(JSON Schema) / risk / handler(args, ctx) -> dict`。
返回统一含 `ok`、`summary`（一句中文结论），产物类工具再带 `path`。这样：
- agent 的 observation 有稳定结构；
- 最终结论可以引用 `summary`，减少模型编数；
- CLI / Streamlit 都能直接展示。

6 个工具：

| 工具 | 作用 | 风险基线 |
| --- | --- | --- |
| `inspect_schema` | 查看表结构 / 样例值 | low |
| `query_data` | 受控只读 SQL（表白名单 + LIMIT） | low（敏感字段/大批量升 high） |
| `compute_metrics` | 指标口径计算（销售额/订单数/客单价/转化/复购，支持同比环比） | low |
| `detect_anomaly_drivers` | 按维度拆解指标变化，输出贡献度 | low |
| `generate_chart` | 生成 SVG 趋势/柱状/饼图 | low |
| `export_report` | 导出 Markdown/HTML 分析报告 | medium（全量导出升 high） |

## 5. 人在环路（HITL）

风险分级规则（`guard.py`）：

- **low**：读结构、算指标、画图 → 直接执行；
- **medium**：默认导出、普通查询 → 记录审计；
- **high**：全量明细导出、敏感字段批量读取、参数命中危险指令 → 必须人工确认。

流程：

```
agent 判断需要审批
  ├─ 有 approve 回调（CLI 交互 / Streamlit 按钮）→ 用户决定 → 审计 → 继续/终止
  └─ 无回调 → 返回 status=awaiting_approval（trace 已落盘）
                └─ 前端展示风险详情 → 用户点批准/拒绝 → agent.resume(True/False)
```

断点续跑靠 `_state` 保存消息历史与待执行动作；拒绝时写入审计日志并终止工具链，不再调用模型。

## 6. SQL 安全护栏

只读不是靠 prompt 声明，而是三层硬约束：

1. `store.py`：SQLite 以 `mode=ro` 打开；只允许单条 `SELECT`/`WITH`；禁止分号追加多语句；危险关键字（INSERT/UPDATE/DROP/PRAGMA/ATTACH...）直接拒绝；表白名单 `{stores, products, customers, sales}`；自动补 `LIMIT`（默认 100，上限 200）。
2. `guard.py`：工具参数级注入检测（`import`、`exec`、`os.system`、`subprocess`、`DROP TABLE` 等黑名单）。
3. `agent.py`：只允许调用注册表中的工具名，模型无法凭空发明工具或执行任意代码。

## 7. Trace 模型

一次分析 = 一个 trace，包含：

```json
{
  "trace_id": "trace_xxx",
  "query": "...",
  "status": "completed",
  "started_at": "...", "duration_ms": 1234,
  "spans": [
    {"span_id": "...", "parent_id": null, "kind": "llm", "name": "complete#1",
     "start_ms": 0, "duration_ms": 800, "status": "ok", "attrs": {"chars": 320}},
    {"span_id": "...", "parent_id": "...", "kind": "tool", "name": "compute_metrics",
     "start_ms": 900, "duration_ms": 50, "attrs": {"ok": true, "summary": "..."}}
  ],
  "events": [{"kind": "plan_ready", "offset_ms": 810, "attrs": {"steps": ["..."]}}],
  "result": {"answer": "...", "artifacts": ["artifacts/..."], "error": "", "steps": 4}
}
```

`python -m shuxi --replay <trace_id>` 按时间线打印 LLM / plan / tool / hitl 事件，便于复现一次「模型为什么这么答」。

## 8. 可测试性设计

- `MockProvider`：按剧本返回固定响应，`--mock` 下不依赖网络与 API Key，演示和 CI 稳定；
- `DataAnalysisAgent(tools=...)` 支持注入自定义工具，单测用假工具验证循环而不碰数据库；
- `TraceRecorder(log_dir=tmp)`：测试写临时目录；
- 数据层用固定种子的演示数仓，`data/demo.db` 可重复生成。

## 9. 已知限制

- 演示数仓是模拟电商数据（约 4000 行），不是生产规模；SQL 引擎是 SQLite，生产可换 MySQL/Doris。
- 指标口径只实现了 5 个常用指标，维度下钻仅限 region/channel/category。
- 未做多轮对话记忆（每次分析独立 trace），生产可加 session 级上下文。
- 未接真实权限系统（行级/列级权限），生产需要把用户身份接入 guard 的风险策略。
