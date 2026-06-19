## ✅ drilldown 模块（已实现）

### 当前状态

`drilldown/` 模块已完成开发，包含：

```
drilldown/
├── __init__.py
├── cli.py         # register_subparser() + CLI 入口（--session, --text, --list, --no-recurse）
├── graph.py       # SessionGraph 构建（AgentStep → ToolCall, SpawnGroup 并行检测）
├── render.py      # HTML（内嵌 SVG/CSS/JS）+ ANSI 终端树渲染
└── storage.py     # 输出文件命名与存储管理（drilldown@och 目录）
```

### 已实现功能

- `och drilldown` — 最新 session 拓扑可视化（HTML，自动打开浏览器）
- `och drilldown --session ses_xxx` — 指定 session
- `och drilldown --text` — 终端树图（ANSI）
- `och drilldown --list` — 列出可深挖的 session
- `och drilldown --no-recurse` — 不递归子 agent session
- 子 agent 递归（通过 `session.parent_id` CTE 查询）
- 并行检测（SpawnGroup，基于 parentID 分组）
- 零依赖策略保持（纯 stdlib）

### 已知局限

- `drilldown` 未纳入 CI（`.github/workflows/smoke.yml` 未验证 `--help` 和模块导入）
- 暂无单元测试（不依赖真实 DB 的 mock 测试尚未编写，计划见 `tests/test_drilldown_graph.py`）
- `pyproject.toml` 的 `find.packages.include` 尚未包含 `drilldown*`

### 待办

1. 将 drilldown 纳入 CI
2. 编写 drilldown 单元测试
3. 多 session 对比（v2）
4. 对话面板（v2）
