# 演示脚本（Demo Playbook）

本文是「数析 · 智能数据分析平台」的**现场演示操作手册**：从零启动 Streamlit 工作台，
完整走一遍「自然语言提问 → 计划 → 工具链 → 图表/报告 → 高危审批 → trace 回放」。

演示模式（`provider=mock`）使用 `examples/mock_transcript.json` 剧本模拟大模型输出，
**离线、无需 API Key、结果可复现**；真实模式只需在 `.env` 填入 `DASHSCOPE_API_KEY`。

---

## 0. 演示前准备（约 2 分钟）

```bash
cd shuxi-data-agent

# 1) 建虚拟环境并安装依赖（首次）
python -m venv .venv
# Windows: .venv\Scripts\activate     Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 2) 生成演示数仓（幂等，随机种子固定 42）
python scripts/init_demo_data.py
# 产出：data/raw/{stores,products,customers,sales}.csv 与 data/demo.db（4 张表，约 4000 行销售明细）
```

启动 Streamlit 工作台（固定端口 8601，方便提前开好浏览器页签）：

```bash
streamlit run app.py --server.headless true --server.port 8601
```

看到 `You can now view your Streamlit app in your browser.` 后，浏览器打开
<http://127.0.0.1:8601>。健康检查：`curl -I http://127.0.0.1:8601` 返回 `200 OK`。

> 端口被占用时换 8602；`--server.headless true` 用于服务器/CI 环境，本地演示可省略。
>
> 环境兜底：若运行环境没有 matplotlib（例如离线 CI），`charts.py` 会自动降级为**纯标准库 SVG 渲染**，
> line / bar / pie 仍可正常出图，界面预览与报告产物不受影响（详见 `src/shuxi/charts.py`）。

---

## 1. 演示路线总览（建议 8~10 分钟）

| 步骤 | 操作 | 讲解点 |
| --- | --- | --- |
| 1 | 打开首页，看侧边栏 | Provider 状态、演示数据 4 张表、6 个工具清单 |
| 2 | 输入问题 →「开始分析」 | 自然语言入参；Agent 先规划再执行 |
| 3 | 看「执行计划」表 | 计划先于执行；计划中工具名过白名单 |
| 4 | 看「工具调用明细」表 | 每步工具名/参数/结果摘要/耗时，全部来自 trace span |
| 5 | 看「最终答案」 | 结论来自工具 observation，不是模型凭空编 |
| 6 | 看「产物」 | SVG 图表在线预览 + Markdown/HTML 报告下载 |
| 7 | 看「运行 Trace」 | trace_id 贯穿；展开 trace JSON |
| 8 | 历史 Trace 与回放 | 读磁盘 trace 列表，replay_trace 时间线文本 |
| 9 | 切「高风险审批」剧本 | HITL：awaiting_approval → 批准 / 拒绝 → resume() |
| 10 | （可选）CLI 演示 | `python -m shuxi "..." --mock` 同一条链路的命令行形态 |

---

## 2. 完整分析演示（Mock 剧本，约 2 分钟）

### 2.1 界面初始状态

打开 <http://127.0.0.1:8601>，应看到：

- 顶部标题 **📊 数析 · 智能数据分析平台** 与一段能力说明；
- 中间「分析问题」输入框（默认填入示例问题）+「🚀 开始分析」「🧭 只生成计划」两个按钮；
- 侧边栏从上到下依次为：
  - **LLM Provider** 单选：`mock` / `dashscope`，默认 `mock`；
  - **Mock 演示剧本** 下拉：`完整分析（计划 → 多个工具 → 结论）` / `高风险审批（HITL 断点演示）` / `空剧本`；
  - **自动批准高风险操作（仅演示）** 复选框（默认不勾，演示 HITL 时保持不勾）；
  - Provider 状态：绿色 `MockProvider · 离线演示模式，无需网络与 API Key`；
  - **演示数据概览**：`stores / products / customers / sales` 四张表的行数与说明，可展开「字段明细」；
  - **工具清单（6 个）**：每个工具可展开看 JSON Schema 参数定义。

### 2.2 发起分析

输入并保留默认问题后点击 **🚀 开始分析**：

```text
查询 2025 年 9 月华东区销售额，并和上月对比
```

预期（Mock 剧本逐步回放）：

1. 状态提示 **✅ 分析完成**；
2. **① 执行计划**：4 行表格 —— `inspect_schema` → `compute_metrics` →
   `detect_anomaly_drivers` → `generate_chart`，每行带「理由」列；
3. **② 工具调用明细**：4 行工具调用记录，列为 工具 / 参数（JSON 预览）/ 结果摘要 /
   状态 / 耗时(ms)，下方小字显示风险分布（如 `low: 3 · medium: 1`）；
4. **③ 最终答案**：Markdown 渲染的中文结论，包含统计口径（时间范围、筛选条件）；
5. **④ 产物**：
   - `artifacts/chart_*.svg` 在线预览（折线图：华东区月度销售额趋势）；
   - 若剧本调用 `export_report`，会出现 `artifacts/report_*.md|html`，可展开预览 + 下载；
6. **⑤ 运行 Trace**：`trace_xxx` 形式的 trace_id、trace 文件路径、「查看 trace JSON」expander（含下载按钮）；
7. **⑥ 历史 Trace 与回放**：磁盘上的 trace 列表（trace_id / 问题 / 状态 / 耗时 / 开始时间），
   选中后点「▶️ 回放时间线」输出 `replay_trace` 文本，点「🧾 查看该 trace JSON」展示原始 JSON。

### 2.3 计划预览（不执行工具）

在输入框下点击 **🧭 只生成计划（不执行工具）**：调用 `agent.plan_only(query)`，
只渲染计划表格与模型 thought，不产生任何工具调用与产物。用于讲解「先规划后执行」时很直观。

### 2.4 讲解口径（建议原话）

> 模型只负责输出 `{"thought","plan","action","action_input"}` 这样的 JSON，
> 真正执行的是工具层：工具名白名单、参数校验、注入检测、风险分级都在执行前完成；
> 模型没有代码执行权限，所以「prompt 注入 → 任意代码执行」这条路是堵死的；
> 每一步都有 trace span，耗时、参数摘要、结果摘要都能回放。

---

## 3. 高风险审批（HITL）演示（约 2 分钟）

1. 侧边栏 **Mock 演示剧本** 切换为 `高风险审批（HITL 断点演示）`；
2. 输入问题（示例）：

   ```text
   导出 2025 年客户全量明细报告
   ```

3. 点击 **🚀 开始分析**，预期状态为 **⏳ 等待人工审批（高风险操作已挂起）**：
   - 审批面板显示：**风险等级 HIGH**、**待审批工具 export_report**；
   - 「风险原因」列表（例如：`include_all_rows=true 属于全量导出`）；
   - 「查看待审批参数」expander 展示 `format / title / sections / include_all_rows` 等 JSON；
   - 下方 **✅ 批准执行** 与 **⛔ 拒绝执行** 两个按钮。

4. 分支演示：

   | 点击 | 调用 | 预期结果 |
   | --- | --- | --- |
   | ⛔ 拒绝执行 | `agent.resume(False)` | 状态 **⛔ 审批被拒绝，工具链已终止**，无产物；`logs/audit.jsonl` 新增一条 `decision=rejected` |
   | ✅ 批准执行 | `agent.resume(True)` | Agent 继续执行挂起的 `export_report`，随后输出最终答案，状态 **✅ 分析完成**，产物中可下载报告；审计日志新增 `decision=approved` |

5. 审计证据（可选）：演示时打开 `logs/audit.jsonl`，展示每行包含
   `ts / trace_id / tool / args_digest / risk / decision / reason`，
   原始敏感参数不落盘（只有 sha256 摘要前 16 位）。

> 说明：勾选侧边栏「自动批准高风险操作（仅演示）」等价于 `--yes`，可跳过人工点击；
> 正式演示建议保持不勾选，突出 HITL。

---

## 4. Trace 回放演示（约 1 分钟）

方式 A（界面）：展开 **⑥ 历史 Trace 与回放** → 选中某条 trace →
「▶️ 回放时间线」，屏幕出现类似：

```text
trace_id : trace_xxxxxxxxxxxx
query    : 查询 2025 年 9 月华东区销售额，并和上月对比
status   : completed  duration: 8.3 ms
------------------------------------------------------------------------
[     0.1 ms] event  agent_thought  {"iteration": 1, "parse_level": "direct", ...}
...
[     2.4 ms] tool   inspect_schema  1.2ms  ok  {"args": "{\"table\": \"sales\"}", ...}
[...]
answer:
2025 年 9 月华东区销售额为……
artifacts: artifacts/chart_xxx.svg
```

方式 B（CLI，同一份 trace 目录）：

```bash
python -m shuxi --list-traces
python -m shuxi --replay <trace_id>
```

---

## 5. 真实模型（DashScope）演示（可选，约 1 分钟）

```bash
# .env 中配置
# SHUXI_PROVIDER=dashscope
# DASHSCOPE_API_KEY=sk-xxxx
# SHUXI_MODEL=qwen-plus
streamlit run app.py --server.headless true --server.port 8601
```

侧边栏把 **LLM Provider** 切到 `dashscope`，状态区变绿显示模型名；重复第 2 节流程即可。
真实模式下计划、工具选择由通义千问生成，界面/审计/trace 完全一致；
若 Key 缺失，侧边栏会红字提示，且运行按钮会给出 `ProviderError` 说明。

> 口径诚实：演示模式是脚本化 Mock 模型，用于展示**工程链路**，不代表真实模型效果；
> 真实效果取决于 `DASHSCOPE_API_KEY`、模型版本与网络。

---

## 6. 命令行等价演示（备选，30 秒）

如果现场浏览器不方便，用 CLI 演示同一条 Agent 链路：

```bash
python -m shuxi "查询 2025 年 9 月华东区销售额，并和上月对比，再画趋势图" --mock
```

输出包含：计划表 → 每步工具调用与结果摘要 → 最终答案 → trace_id → 产物路径。
随后 `python -m shuxi --replay <trace_id>` 打印时间线。

---

## 7. 截图清单（README / 简历配图建议）

建议按下面 6 张截图准备（保存在 `docs/images/`，README 中按需引用；图片非必需，但面试演示更直观）：

| # | 截图内容 | 必须出现的元素 | 建议命名 |
| --- | --- | --- | --- |
| 1 | 工作台首页 | 标题、输入框、侧边栏 4 张表概览 + 6 个工具 | `ui_home.png` |
| 2 | 分析执行完成 | 状态 ✅、4 步计划表、工具调用明细（含耗时） | `ui_plan_steps.png` |
| 3 | 最终答案 + 产物 | Markdown 结论、SVG 折线图预览、报告下载按钮 | `ui_answer_artifacts.png` |
| 4 | HITL 审批面板 | ⏳ 状态、HIGH 风险、工具名、风险原因、批准/拒绝按钮 | `ui_hitl.png` |
| 5 | Trace 详情 | trace_id、展开的 trace JSON（spans/events） | `ui_trace.png` |
| 6 | 历史回放 | 历史 trace 列表 + replay 时间线文本 | `ui_replay.png` |

截图小技巧：浏览器缩放到 90%~100%，窗口宽度 ≥1280px；先点「开始分析」再截图，
避免 spinner 状态；图表预览 iframe 高度固定 460px，若被裁切可在组件内滚动。

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 侧边栏提示「演示数据库不存在」 | 未生成数据：执行 `python scripts/init_demo_data.py` |
| 点「开始分析」直接报 `演示数据不可用` | 同上；确认 `data/demo.db` 存在且未被占用 |
| DashScope 报缺少 API Key | 在 `.env` 配置 `DASHSCOPE_API_KEY`，或切回 `mock` |
| 端口 8601 被占用 | 换 `--server.port 8602`，或结束占用进程 |
| 图表中文变方块 | 预期行为：图表标签统一用英文/日期，避免依赖服务器中文字体；报告正文仍为中文 |
| 页面刷新后看不到上次结果 | Streamlit 会话状态随刷新重置，用「历史 Trace 与回放」从磁盘恢复 |
| 审批按钮点击无反应 | 确认当前状态是「等待人工审批」；若已审批过，按钮不再出现 |

---

## 9. 演示前 30 秒自查清单

- [ ] `data/demo.db` 存在（`python scripts/init_demo_data.py` 可重建）
- [ ] `streamlit run app.py --server.headless true --server.port 8601` 已启动，`curl -I http://127.0.0.1:8601` 为 200
- [ ] 侧边栏 provider = `mock`，剧本 = 完整分析；数据概览有 4 张表
- [ ] 先跑一次完整分析，确认历史 trace 列表有内容（回放演示不留白）
- [ ] 已准备 HITL 剧本演示（切换后重新发起查询）
- [ ] `logs/audit.jsonl` 可在文件管理器打开（审批演示证据）
