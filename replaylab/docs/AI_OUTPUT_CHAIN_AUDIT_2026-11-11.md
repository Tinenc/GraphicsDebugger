# Bread AI 输出截断与空答案链路检查（2026-11-11）

## 结论与证据边界

用户报告的完整组合为：

> 主 Agent 没有返回文本答案。
>
> 模型达到本轮输出上限，回答可能被截断。已完成的修改仍保留。

这两句话由 `src/replaylab_agent/deep_engine.py` 组合生成，可以精确定位到
API / DeepAgents 主 Agent 的结束处理：没有可显示的最终正文，同时最后一条
主模型回复的结束原因被识别为输出截断。它们不是 ACP 登录失败或 HTTP 鉴权
失败的通用提示。ACP 的相邻空答案提示使用的是另一句话。

没有对方的版本、模型、配置及当次日志，因此不能判定当次究竟是用户设置的
输出额度过低、提供商默认限制、上下文剩余额度不足，还是推理已耗尽额度。
OpenAI 官方明确说明推理也消耗输出额度，可能在产生可见正文之前就耗尽：
[Reasoning — Allocating space for reasoning](https://developers.openai.com/api/docs/guides/reasoning#allocating-space-for-reasoning)。
本地已用真实 DeepAgents 图和 SDK 模拟服务复现这类状态；未使用对方或本机的
真实账号发起模型请求，也未对每家供应商进行远端可用性认证。

余额决定是否还有消费额度，不会消除模型的单次生成上限、上下文窗口或请求
速率限制。长任务应跨多次模型请求完成；单次 `max_tokens` 不能直接等同于
整个任务已完成或无法继续。若额度每次都耗在推理而没有新答案/工作，继续
发送完全相同的请求也不能保证完成，应识别这种空转。

ACP 也存在同类限制。其标准定义了 `max_tokens`（token 上限）和
`max_turn_requests`（单轮模型请求次数上限），并允许在同一 session 再发
`session/prompt` 延续上下文：
[ACP Prompt Turn](https://agentclientprotocol.com/protocol/v1/prompt-turn#stop-reasons)。

## 检查范围

入口为 `cli.py` 的 profile 分流，随后检查模型构造、流式解析、工具边界、
会话续答、结束状态、最终事件输出与 qrenderdoc 显示缓冲。当前目录包含
21 个 API 预设与 9 个订阅/Agent 预设，按共享协议实现覆盖：

| 协议 | 当前预设 | 截断/结束处理 |
| --- | --- | --- |
| OpenAI Responses | openai、xai | `incomplete_details.reason=max_output_tokens` |
| OpenAI Chat | openrouter、mistral、groq、deepseek、qwen、qwen-coding-plan、kimi、glm、doubao、hunyuan、qianfan、siliconflow、ollama、custom-openai | `finish_reason=length`，支持选择 Chat / Responses |
| Anthropic Messages | anthropic、minimax | `stop_reason=max_tokens` |
| Gemini | gemini | `finish_reason=MAX_TOKENS` |
| Vertex AI | vertex | `finish_reason=MAX_TOKENS` |
| Azure OpenAI | azure-openai | legacy 与 v1 路由；共享 OpenAI 回复处理 |
| Codex app-server | codex | `final_answer`、单条完成事件与终态快照 |
| ACP | copilot-acp、cursor-acp、gemini-acp、qwen-acp、kimi-acp、codebuddy-acp、claude-code-acp、custom-acp | `end_turn` / `max_tokens` / `max_turn_requests`、会话内续答、工具状态与传输恢复 |

共享协议覆盖不等于逐一验证各供应商当前线上实现。Gemini / Vertex 的截断
结束原因通过真实图事件回归验证；本轮完整 SDK + SSE 请求解析验证覆盖
OpenAI Chat、Responses 和 Anthropic。ACP / Codex 使用本地协议模拟进程或会话。

## 已修复的问题

### API 截断后直接退出

原实现遇到明确的输出截断就退出主循环，即使工具已经全部完成、图检查点
可安全续答，也不会恢复。只产生推理而没有正文时，会直接得到上述两句话。

初次修复补了最多两次续答；用户随后指出，长任务仍可能被这个固定总次数
截断。当前实现已移除总续答次数限制，使用同一个图、thread ID、检查点与工具会话。
不会重发原始任务、重建整张图或由运行时重放已完成的工具；后续模型仍负责
决定剩余工作。每次仍使用原配置的输出额度，不擅自调高费用相关参数。
续答要求把任务与工具参数拆成较小步骤，大文件在授权工作区分段保存，最终
给出自包含的简洁说明和文件链接；不重复已有推理或已完成工具。

以下情况保留部分结果，不尝试追加续答：存在实际未完成的图节点、主工具、
子任务，或无法读取检查点。最新的工具参数截断保护会在执行前隔离整条提案，
保存“未执行”记录后才允许在原图中改用小调用恢复，见
[长度恢复设计](AI_LENGTH_RECOVERY_DESIGN_2026-11-11.md)。只有连续三次达到单轮限制而没有
新的工作事实或新的正文时，才因空转以 `partial` 结束；任一次取得进展都会
清零这个连续空转计数，不存在累计的两次/三次任务额度。工作事实使用去重
的 ledger `workFingerprint`，不使用工具次数、时间戳或模型自称进度。
重复正文和复制过来的回退正文不会制造进展。重复空截断不会覆盖上一段已有的部分正文；新工具边界会
作废这段回退，防止把工具之前的进度当作最终答案。

新增日志记录协议、标准结束原因、正文字符数、配置的输出额度与提供商返回的
输出/推理 token 数。没有返回 usage 时记录 `unknown`，不会猜测实际用量。

### ACP 后一次空截断覆盖已有部分答案

ACP 本来就支持同会话最多两次续答，但只保留最后一次 `PromptResult`。
若第一次已有部分答案、后两次只有推理而正文为空，最后会丢失已有正文。
现保留最后一段可显示的部分答案；发生新工具调用时使旧回退失效。
正常完成、提问、输出耗尽与传输中断仍保留各自的结束处理。

补查还发现：`ACPMainAgentSession` 先前只接受 `end_turn` 和 `max_tokens`，
把标准的 `max_turn_requests` 当作错误。现将两种轮次额度耗尽都作为可续作
结果处理，在同一 session 延续，不重启供应商进程。ACP 与 API 复用同一
进展检测规则，也取消了有进展时的累计续作次数上限。连续空转或工具尚未
结束时保留 `partial`；拒绝、取消、鉴权和真实传输错误不伪装成额度耗尽。
`max_turn_requests` 不再借用“输出被截断”的说明；具体停顿原因保留在日志。

### Codex 仅在终态快照出现的答案被遗漏

原收集器只从流式片段及 `item/completed` 收集最终答案，没有读取
`turn/completed.turn.items` 中的显式 `final_answer`。本地模拟可复现单条
事件缺失、终态快照已有答案但最终文本仍为空的情况。

现读取匹配且已完成的 turn 快照，并保持原答案大小限制。
`commentary`、推理与未知 phase 均不会成为最终答案。

## 其余检查结果

- `max_output_tokens` 由 profile 传入对应 SDK；未发现统一给所有 API 写死的小额度。
  未显式配置时仍可能受 SDK、模型或网关默认额度约束。
- Anthropic 原有 thinking budget 与输出额度关系检查保留；本轮没有改变用户的
  推理设置、模型、端点或凭据。
- 鉴权、权限、余额、限流、模型容量、上下文、网络与超时诊断有独立分类。
  这类错误不会自动当作正常的最终答案；请求级恢复与工具执行保持分离。
- 内部压缩输出与子 Agent 草稿不会被提升为主 Agent 最终正文。
- 输入预算、原始长输入分页、精确历史保留、摘要空裁剪恢复及 ACP 显式声明的
  压缩命令已补齐，开源实现比较和覆盖场景见上述长度恢复设计。
- 当前固定的 DeepAgents 依赖已有自动上下文摘要、旧历史卸载与上下文溢出
  恢复，避免仅靠叠加请求无限增长输入。自动压缩阈值依赖模型元数据，未知
  模型使用框架回退阈值；不能保证任意自定义网关的实际窗口都被自动识别。
  ACP / Codex 的模型与上下文压缩由供应商 Agent 管理，Bread 维持同一会话。
  本轮没有提高用户的单次输出额度、改变推理档位或声称余额充足就没有其他
  服务限制。连续空转、未完成工具、真实服务错误、用户取消与配置的运行时限
  仍然各自生效；网络故障重试次数与正常的输出分轮续作是两件事。
- `ProgressReporter.final_answer()` 通过 `stream=final, reset=true` 输出完整最终
  文本；`completed` 携带终态。qrenderdoc 清空旧的临时缓冲后接收最终文本，
  并识别 `terminalStatus=partial`。本轮未发现这里造成所报告两句话的证据。
- 提示中的“已完成的修改仍保留”不证明当次实际修改过文件；是否有修改应以
  Host 收据和恢复记录为准。

## 验证与交付

回归覆盖构造参数、API 格式协商、SSE 中断、截断恢复、ACP 启动与清理、
Codex JSONL、账号/容量恢复、工具与检查点、profile、对话传输、CLI JSON、
最终消息事件及原生 trace 模型。完整长度恢复后的结果为 **931 passed、6 skipped**，另有
6 个 subtest 通过。3 个跳过项需显式启用本机 Codex 生命周期检查，另 3 个
因 Windows 当前进程没有创建符号链接的权限而跳过。现有 MCP SDK 另有
3 条弃用警告，没有失败。原生输入存储权限收紧后，针对性回归另有
68 passed、1 skipped。用量日志测试也验证了推理 token 计数且不记录推理正文。

合入最新主分支并补充摘要质量门禁后的完整 Agent/打包回归为 **2018 passed、
20 skipped、6 个 subtest 通过**；相关 MCP 回归为 **246 passed、7 个
subtest 通过**。新的持续集成长度恢复专项为 **133 passed**。完整回归的
跳过项主要涉及 Windows 符号链接权限、显式启用的 Codex 生命周期、需要
发行环境的依赖包/Qt 对象以及其他平台的编译器；发行包另外执行实际重定位
检查。原生相机测试使用源码未变化的已验证 1.13.135 核心工件，Qt 探针使用
仓库 SDK 的相邻运行库。3 条既有 MCP 弃用警告保留，没有新增失败。

最终正文范围复查另补齐跨轮文本拼接、边界空白保留和空正常结束的 partial
判定；对应长度专项 **139 passed**。后续工具调用仍清除旧进度文字。
安装版 Codex 的 3 项隔离、仅本机合成模型响应的生命周期检查也已通过。
完整真实长输入验收曾两次遭 API 上游过载，不能将其写为线上长输入通过。

在装好项目测试依赖的 Python 环境中，从仓库根目录复现上述链路子集：

```powershell
$chainTests = Get-ChildItem -LiteralPath replaylab/tests -File -Filter 'test_*.py' |
    Where-Object { $_.Name -match 'langchain|openai|acp|codex|external_connectors|subscription|profiles|models|provider|conversation|task_review|vendor_matrix|deep_engine|deepagents|native_engine|agent_chain|agent_checkpoint|agent_trace|cli_machine_json|cli_prompt_stdin|output_continuation|context_management|task_input|owned_tool_output|owned_work_output|task_mcp_proxy|external_mcp|model_image_transport|shader_file_middleware|shader_draft_recovery|tool_media|doctor|agent_target_context|cli_context' } |
    ForEach-Object { $_.FullName }
python -m pytest @chainTests -ra --tb=short
```

新增关键场景包括：推理耗尽但正文为空、不同协议结束原因、有新正文或新工作
的 API/ACP 五阶段任务跨过原两次上限、连续空转与恢复进展后重置计数、
`max_turn_requests` 协议解析、检查点不可读/缺失/仍有待执行节点、未完成工具参数、续答后工具只
执行一次、ACP 旧进度失效，以及 Codex 终态快照的 phase 和大小边界。

测试使用系统临时目录。本地化 `update_translations.ps1 -Check` 通过
（合入当前主分支后为 5808 个活动条目，0 unfinished），`git diff --check`
通过；没有修改 UI 源字符串。发行版本为 1.13.136，最终构建提交、完整
回归和工件检查以发布回执记录为准。没有更改现有应用安装或用户数据。

### 现有连接的线上检查

2026-11-11 使用现有配置执行有界握手，以及一次无项目效果的真实模型/MCP
工具往返。没有修改模型、凭据或登录状态。握手可用不等于模型生成有余额：

| 连接 | 握手 | 模型调用、MCP 收据与最终正文 |
| --- | --- | --- |
| 自定义 OpenAI 兼容 API / gpt-6.1-sol | 通过 | 通过 |
| Codex app-server / gpt-6.1-sol | 通过，账号有效 | 通过 |
| Claude ACP / claude-sonnet-5 | 通过 | 供应商拒绝：403 Request not allowed |
| 自定义 OpenAI 兼容 API / k3 | 通过 | 上游账号耗尽：403 All available accounts exhausted |
| OpenAI API / gpt-6-astra | 通过 | 余额不足：429 credit_balance_exhausted |
| Cursor ACP | 需要登录 | 未请求生成 |

鉴权和余额错误不属于长度恢复，不能通过自动续作消除。其他厂商的协议路径
经过本地真实 SDK/协议回归，不等于所有供应商账号均已完成线上生成测试。
压缩质量以原任务引用、原历史存档、实际工具收据及最终交付内容检查共同约束，
不能把摘要无格式错误描述为语义绝对无损。

若后续仍在现场出现同样提示，收集错误附近的 `agent-core.log` 中
`API output limit` 和 `continuing the same checkpoint` 记录，并结合当次
task JSONL、版本、预设、模型、输出额度及推理设置判断。日志中不要包含密钥。
