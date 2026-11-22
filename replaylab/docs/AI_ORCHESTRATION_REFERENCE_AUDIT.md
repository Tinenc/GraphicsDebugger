# ReplayLab AI 旧编排参考审查（已废止）

本文件名保留用于修复历史链接。原文描述的 orchestration registry、Custom Agents、manager/supervisor/reviewer 拓扑和 v1–v5 迁移均已从运行时删除，不再是产品契约，也不得作为新增代码的模板。

当前实现只有两条固定路径：Model API 使用 GitHub deepagents；ACP/Codex 订阅使用厂商原生单 Agent。两者共享任务级 ReplayLab MCP、文件系统 Skills 和三层确定性约束。详见 [`REBUILD_PLAN.md`](REBUILD_PLAN.md) 与 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)。
