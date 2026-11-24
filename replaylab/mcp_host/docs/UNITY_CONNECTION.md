# Unity MCP 自动准备与恢复

Unity 效果还原包含目标项目的连接准备。AI 使用
`start_unity_project_connection(project_path=...)` 安装随包的固定版本 Unity MCP
和 Bread Editor 包，复用或打开正常的 Unity Editor，再通过
`get_unity_integration_job(job_id=...)` 查看准备进度。Unity Editor 和有效许可证
仍需由用户提供；准备过程不会升级项目的 Unity 版本。

`call_unity_project_tool` 在发现缺少连接、端口变化、编译重载或传输中断时，
会自动尝试恢复对应项目的连接。短暂断线可在当前调用内恢复；较长的安装或
编译返回 `pending`、`jobId` 和 `nextAction`，由 AI 继续观察该任务。
同一项目已有准备任务时复用它，避免重复安装。已连接的外部 MCP 保持原有
适配器，通过原连接重新握手，不被静默替换。

Editor 定时检查已启用的项目连接：健康连接保持运行，停止的桥接服务重新启动，
编译或资源导入期间等待。显式重连使用独立的 `reconnectRequest`；只有 Editor
确认处理该请求，Host 才继续发送项目操作。项目路径、连接 nonce 和已完成的
导出、资产及任务进度不因重连而丢弃。

## 丢失回复与继续工作

- 尚未发送的操作可在恢复后继续发送；明确的只读观察可以重新读取。
- 已发送的写入若丢失回复，返回 `operationOutcome: unknown` 和
  `safeToRetry: false`。AI 应读取场景、资产或原导入请求的状态，确认结果后
  再决定下一步；连接恢复本身不能证明该写入是否完成。
- 导入任务继续查询同一个请求的回执，不重新创建导入请求。
- 参数、方法或应用错误保留具体诊断，不按断线处理、重装或重启健康连接。
- 自动恢复失败后保留诊断，不为每次轮询重复启动相同安装。AI 检查日志并
  修复可行原因后，可调用 `start_unity_project_connection(..., reconnect=True)`
  再试。只有无法自行解决的条件才交给用户处理。

Editor 保持图形界面，自动连接不反复激活窗口。工具操作和准备任务按项目协调，
不会把调用转到其他项目；取消等待会释放连接等待资源，也不会关闭用户的 Editor。
具体 AI 工作流见 [Unity 实现指南](../../../.agents/skills/renderdoc-resource-export/references/unity-implementation-guidance.md)。

## 验证范围

针对性测试位于：

- `tests/test_unity_connection_recovery.py`：自动准备、断线续接、未知写入、
  有界失败、超时、取消及重连确认。
- `tests/test_unity_bridge_reconnect.py`：提取实际 C# 连接类，在隔离的 Editor/
  Transport 替身环境中编译执行连接生命周期。
- `tests/test_unity_integration.py`：项目与实例路由、包安装及现有连接兼容。

这些自动测试不替代真实 Unity Editor 中的场景与最终画面验收。
