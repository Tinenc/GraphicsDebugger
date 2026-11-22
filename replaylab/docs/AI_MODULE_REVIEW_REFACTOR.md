# ReplayLab AI 模块旧架构审查（已废止）

这份文件曾记录 2026-09-02 对自研 planner/reviewer/WorkflowGuard 运行时的故障审查。被审查的模块和修补路线已经整体删除；其中的文件位置、开关、执行步骤和建议均不是当前实现说明，不得据此恢复旧代码。

当前规范：

- 设计与最终接线：[`REBUILD_PLAN.md`](REBUILD_PLAN.md)
- 当前状态与剩余外部验收：[`HANDOFF_2026-09-04.md`](HANDOFF_2026-09-04.md)
- 运行架构：[`../ARCHITECTURE.md`](../ARCHITECTURE.md)

旧审查得出的有效结论已经体现在当前架构：ReplayLab 不再用自研 Agent 流程替模型决定路线，确定性 Host 只负责工具权限、事实证据、工件门、报告结构和有界验收。
