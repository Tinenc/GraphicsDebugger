# ReplayLab Lean Agent Core（已删除）

Lean Agent Core 是 2026-09-03 重构过程中的临时过渡实现。其 `src/replaylab_agent/core/`、legacy 切换开关、delegate 工具、pydantic-ai 路径和对应测试现已全部删除。

`docs/handoff/wip-layer3/` 中的五份合入前 Python 草稿也已删除。当前代码使用顶层 `deep_engine.py`、`native_engine.py`、`agent_*` 模块和 MCP Host 的 `task_report.py`。详见 [`HANDOFF_2026-09-04.md`](HANDOFF_2026-09-04.md)。
