# 真实供应商验收与旧 Agent 清理（2026-09-05）

本轮按用户明确授权，仅使用现有配置和已有凭据执行有界验收。未登录新账号、安装 CLI、修改模型选择、购买额度或保存 profile。测试程序为 `packaging/validate_vendor_matrix.py`，它直接调用产品的 `DeepAgentsEngine` / `NativeAgentEngine`，使用同一个任务 MCP 与 Host report gate，没有另一套 Agent loop。

## 真实运行范围

绑定隔离实例 `43060-216596490bf4425f9c4fd3495def97a0`，capture session `fc141937b0b848d3951d9e4a4802fa10`，`6.rdc`，显式目标 EID `11329`。原始冻结快照仍保留真实当前 Present EID `13353`，未伪造当前选择；任务 `intent.arguments.eventId` 指明用户指定目标。仅请求一次 `inspect_event` 和标准证据报告，禁止编辑、编译、替换、改变当前事件或创建图像/文件。

每条任务限制为 180 秒总时限、8 次 Host 工具调用、2 次报告提交和 1 次额外修正回合。API 使用原配置模型，临时限制每次输出 1200 token、最多 6 次共享主/子 Agent 模型调用，SDK `max_retries=0`。预算 callback 在实际 LangChain 模型调用前拒绝第 7 次请求，离线测试验证了模型生成函数不会被执行。订阅 Agent 的内部模型请求数由厂商管理，不能将 API 的 token/请求上限冒充订阅上限。

只读范围支持 Host 标准任务报告。此项验收不等于 Shader 组织、数值等价、附件、提问 UI 或跨 RDC 复用全部通过；这些由各自真实证据和离线回归单独覆盖。

## 结果

| 路线 | 已配置模型 | 实测结果 | 证据与边界 |
| --- | --- | --- | --- |
| API → GitHub deepagents | `gpt-5.6-sol` | completed，51.125 秒 | Host accepted=true，1 次报告提交；仅 inspect_event；5 次模型请求，63735 input / 1051 output token（SDK 返回用量） |
| Codex → 厂商 app-server Agent | `gpt-5.6-sol` | completed，71.875 秒 | 无生成 account_read 通过；Host accepted=true，1 次报告提交；仅 inspect_event；厂商 token 用量未计量 |
| Cursor → 厂商 ACP Agent | `composer-2.5[fast=true]` | 修复后 completed，39.235 秒 | Host accepted=true，1 次报告提交；仅 inspect_event；早先冗余设置相同模型超时已修复并有真实任务验证 |
| Claude Code → 厂商 ACP adapter | `claude-opus-5` | failed，3.641 秒 | 单次真实任务明确返回 `RequestError: Authentication required`，没有登录或重试；早先无生成会话握手通过不能证明生成权限 |

真实输出位于仓库 `output/vendor-matrix-20260905/` 下。成功结果分别为 `matrix-custom-openai-7c40906f023b/result.json`、`matrix-codex-ab057d7ac15d/result.json`、`matrix-cursor-acp-6a764bf960b8/result.json`；Claude 认证失败为 `matrix-claude-code-acp-571d2b854d3c/result.json`。各成功任务保留 Host `.report-state.json`，其中 `toolsUsed` 是 Host 实际观察；不能用 native UI 事件流缺少 `tool-started` 推断没有调用工具。

三条已完成报告一致保留了 `output-affecting-candidate` 边界：只能证明绑定和绘制条件，未声称验证真实像素贡献。完成来自 Host 验收，而不是供应商输出了一句“成功”。

Cursor 的会话握手存在间歇结果：初次 ConnectorError、随后无生成握手成功（10.422 秒，13 models），真实任务在选模阶段超时；独立模型 metadata 会话又成功。`cursor-model-metadata.json` 证明 `currentValue` 精确为 `composer-2.5[fast=true]`。最小修复只省略相同字符串的冗余 `set_config_option`，不忽略不同 suffix，不允许失败回退默认模型。修复后一次 no-generation open 在 `session/new` 的 30 秒限额处失败，随后按授权直接运行产品任务（原有 session/new 上限 60 秒、任务总预算 180 秒）完整成功。没有为此修改账号、配置或产品超时；保留曾发生的厂商启动间歇超时记录。

第三条任务后通过同实例 Bridge 读取当前上下文，0.344 秒完成，capture session、当前/选中 EID 13353 和 snapshotId 均与初始完全相同：`bridge-after-matrix.json`。Cursor 启动失败没有损坏 Bridge 或改变用户选择。

脚手架 `inventory` 输出仅允许 profile id/kind/preset/model 与安装/凭据可用性布尔值；未知保留 null。`handshake` 不发送生成请求，产物不保存账户详情、凭据、endpoint 或 auth URL。未来诊断错误经过凭据规则和 URL 清理。不得把 ACP session 成功通用标记当作已认证账户证明；只有 Codex 的 account_read 或实际生成结果支持相应结论。

## 旧实现清理的证据

- 删除 `docs/handoff/wip-layer3/` 五份 Python 草稿：只存在相互引用和历史文档引用，无运行时、打包或测试消费者。
- 删除未使用的 `agent_protocol.TerminalStatus`、ACP module 的旧导出别名，测试直接使用其真实 owner `native_vendor.write_claude_settings`。
- 删除 probe 中只被旧单测引用的 `_model_list_request` 包装；测试改验证产品实际使用的 `_model_list_requests`，保留 Google API key 不进入 URL 的安全断言。
- 移除 LangChain adapter 对必需 thinking helper 的宽泛异常吞噬，不再在 helper 失败时静默改用 0。没有引入自研模型循环。
- 修复 ACP `session/new` 已精确选中请求模型时的冗余 setter：不同值、缺少 setter、拒绝和超时仍严格阻止 prompt；测试涵盖相同模型与不同 option suffix。
- 保留 profile v1、conversation v2/v3/v4 和 vendor ACP response shape 兼容：前两者有真实持久化用户配置及 qrenderdoc 发包消费者，后者对应有效厂商协议差异。它们不是旧 Agent loop；删除需要完整迁移与 UI 协调。

最终配置、probe、ACP、厂商 engine、CLI、deepagents 与新脚手架回归：151 项通过（1.79 秒）。此文件不宣称完整 Release 构建已完成；构建由主任务统一执行。

## 再次验收

使用已有虚拟环境运行 `python packaging/validate_vendor_matrix.py inventory`；生成测试必须明确指定 `capture --profile <id> --context-file <path> --instance-id <id> --capture-session <id> --event-id <eid>`。一次只测一个 profile，不自动遍历账号，不修改默认连接。新实例必须提供新的真实冻结上下文，不得复用本页已经结束的会话 ID。
