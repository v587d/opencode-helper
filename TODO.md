# 风险/待办

## 已解决 ✅

1. ✅ **没有测试** — 已添加 `tests/test_utilities.py`（17 个 pure-function 测试）+ CI smoke test（`.github/workflows/smoke.yml`，覆盖 Python 3.10-3.13）
2. ✅ **强耦合 opencode 二进制** — README 已添加 `Limitations` 章节说明依赖，`--no-ai` 模式不受影响
3. ✅ **直接读 OpenCode 内部 SQLite 库** — 已明确文档化在 README Limitations 中
4. ✅ **新项目状态** — 已添加 `CONTRIBUTING.md`、Issue templates（bug + feature request）
5. ✅ **pyproject.toml + console_scripts** — `pip install -e .` → 全局可用 `osh` 命令

## 后续建议

- 补充 DB 操作集成测试（需要 opencode.db fixture，投入较大）
- Unix 平台进程检测（`pgrep` / `ps` 替代 `tasklist`）
- JSONC 解析考虑用真正的解析器（当前正则无法处理字符串内的 `//`）
- CI 加状态徽章到 README
