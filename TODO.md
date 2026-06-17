## 🧪 drilldown 模块（新增，单 session 深挖）

### 定位

和现有 `analysis/` 平级，不做子模块。`analysis/` 回答 "跨 session 泛分析"，`drilldown/` 回答 "单个 session 里 agent 怎么调用的"（深挖）。

### CLI 入口

```bash
och drilldown                           # 最新 session → session_flow.html（自动打开）
och drilldown --session ses_abc123      # 指定 session
och drilldown -o report.html --no-open  # 指定输出，不打开浏览器
och drilldown --list                    # 列出最近可深挖的 session
```

### 目录结构

```
drilldown/
├── __init__.py
├── cli.py         # register_subparser() + CLI 入口
├── graph.py       # part 表 → AgentNode/ToolCall 拓扑模型
└── render.py      # 拓扑模型 → 单文件 HTML（内嵌 CSS/JS）
```

> **v1 不做 `templates/` 目录。** 零依赖策略要求所有 HTML/CSS/JS 内嵌在 `render.py` 中。等模板变复杂（>500 行 JS）再考虑拆分。

### 数据模型（graph.py）

从 `part` 表还原 agent 调用链：

```
AgentNode: agent_type, session_id, messages
MessageNode: role, agent, tools, timestamp
ToolCall: name, input, output, status, duration_ms
SessionGraph: session_id, title, agents, edges, stats
```

核心数据结构是**有向图**（多 agent 并行 + 依赖），不是 agentcanvas 的顺序树。

#### 已验证的 `part.data` JSON 结构

基于 `analysis/*.py` 中对 `part` 表的实际查询（`json_extract(p.data, ...)`），`part` 表结构为：

```
part(id INTEGER, session_id TEXT, message_id TEXT, time_created INTEGER, data TEXT)
```

`data` JSON blob 已知字段：

| 字段 | 类型 | 说明 | 来源 |
|---|---|---|---|
| `type` | string | part 类型：`"tool"` 表示工具调用（其它值待确认） | `tools.py` L69 |
| `tool` | string | 工具名：`read`, `write`, `bash`, `skill` 等 | `tools.py` L67, `skills.py` L54 |
| `state.status` | string | `"success"` / `"error"` | `tools.py` L69 |
| `state.input` | object | 工具调用入参（如 `{"name": "skill-name"}`） | `skills.py` L49 |
| `state.output` | ? | 工具调用输出（结构待确认） | — |

> **实现前必须先跑一次**：选一个真实 session，用 `analysis/common.py` 的 `get_parts(session_id)` 拉数据，确认 part 的完整 JSON 结构，尤其关注：
> - 是否存在 `agent` 相关的 part type（如 `"agent_start"`, `"agent_end"`）
> - `state` 是否包含 `duration_ms` 或时间戳用于计算调用耗时
> - `message_id` 如何关联到 `message` 表获取 agent 信息
>
> **agent 信息来源**：`message` 表的 `data` JSON 包含 `role`, `agent`, `modelID` 等字段（见 `common.py` 的 `get_messages()`）。agent 类型通过 `message.data.agent` 获取，而非 `part` 表。

### 渲染策略

- **零依赖**：纯 HTML + Vanilla JS 内嵌，无 npm/CDN
- **渲染技术**：优先用 **SVG**（DOM 内嵌，可交互，无额外依赖）。备选：Canvas（性能更好但交互麻烦）或纯 DOM 布局（flexbox/grid 自上而下时间线）。决策依据：session 通常 ≤50 个节点，SVG 完全够用。
- **核心视图**：agent 并行拓扑时间线 + tool 调用序列
- **可选面板**：节点检查器（token/cost）、对话记录
- **输出**：单个 `session_flow.html`，离线可用
- **自动打开**：`webbrowser.open()` 打开生成的 HTML（`--no-open` 关闭）

### 与 agentcanvas 的关键差异

| 维度 | agentcanvas | och drilldown |
|---|---|---|
| 数据源 | Logfire 外部 API | `opencode.db` 本地 SQLite |
| 受众 | 客户（非技术） | 开发者（自己） |
| 核心视图 | 顺序流 User→Agent→Model→Tool | 并行拓扑（有向图） |
| 目的 | 演示/叙事 | 调试/优化 |

### MVP 范围

**v1 只做**：单 session 的 agent 调用时间线图。~800 行代码以内。

**v1 不做**：
- 多 session 对比
- 成本归属分析
- 对话面板
- Obsidian 集成
- `templates/` 目录拆分
- `--list` 复用的 session 表格 UI（直接复用 `analysis/common.py` 的 `query()` 打印即可）

### 实现路径

1. `graph.py`：参考 `analysis/common.py` 的 `get_parts()` + `get_messages()`，查询 part/message 表 → 构建 SessionGraph
2. `render.py`：参考 agentcanvas 的 render 概念（非代码），替换为 OpenCode 的有向图 SVG 布局
3. `cli.py`：沿 `register_subparser()` 模式注册 `drilldown` 子命令
4. `main.py`：加 `from drilldown.cli import register_subparser as register_drilldown`
5. `tests/test_drilldown_graph.py`：覆盖 SessionGraph 构建逻辑

### 参考现有模式

所有 `analysis/` 模块的 `register_subparser()` 模式一致（以 `tools.py` 为模板）：

```python
def register_subparser(subparsers):
    p = subparsers.add_parser("drilldown", help="...")
    p.add_argument("--session", ...)
    p.set_defaults(func=run)
```

可复用 `analysis/common.py` 的：
- `get_parts(session_id)` — 获取 session 的所有 part 数据
- `get_messages(session_id)` — 获取 session 的所有 message 数据
- `query(sql, params)` — 执行只读 SQL 查询
- `get_db_connection()` — 获取数据库连接（已在 `utilities.py` 中定义）

### 边缘情况与错误处理

| 场景 | 处理策略 |
|---|---|
| session_id 不存在 | 输出错误信息 `f"Session {session_id} not found."`，exit 1 |
| session 无 part 数据 | 输出 `"No agent/tool call data for this session."`，不生成 HTML |
| 数据库无法连接 | 输出 `utilities.py` 已有的 `FileNotFoundError` 提示，exit 1 |
| `--list` 无可用 session | 输出 `"No sessions found."` |
| `--output` 路径无写权限 | 捕获 `OSError`，输出友好错误信息，exit 1 |
| `--list` 复用 | 直接用 `analysis/common.py` 的 `query()` 查 session 表，不做新的 UI |

### 测试计划

文件：`tests/test_drilldown_graph.py`

| 测试用例 | 覆盖内容 |
|---|---|
| `test_build_empty_graph` | 空 parts 列表 → 空 SessionGraph |
| `test_build_single_agent` | 单个 agent 无 tool 调用 → 单节点图 |
| `test_build_parallel_agents` | 多 agent 并行 → 正确边和节点数 |
| `test_tool_call_parsing` | tool part → ToolCall 对象字段正确 |
| `test_duration_calculation` | 已知时间戳 → 计算 duration_ms |
| `test_error_status_handling` | status=error 的 tool → ToolCall.status="error" |

> 测试使用 stdlib `unittest`，遵循 `tests/test_utilities.py` 的现有模式。用 mock 数据，不依赖真实数据库。
