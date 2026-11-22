# 参考设计：高星开源 Agent 系统如何处理"编排 / 工具错误 / 结构化输出 / 人类介入 / 预算 / 确定性门禁"

> 历史调研资料，不是当前实现规范。当前架构只以 `../../ARCHITECTURE.md` 和 `../REBUILD_PLAN.md` 为准。

> 用途：作为 RenderDoc 调试助手（main agent → child agents → host tool server → skills，约 93k 行 Python）重构评审的对照参考。核心问题诊断为：过多的确定性校验门禁、僵硬的 JSON 输出契约、通用（无信息量）错误串、以及 Host 越权覆盖模型的 plan/route。下文以 GitHub 官方文档 / README / 源码为主，提炼这些主流系统"实际怎么做"。
>
> Star 数来自 Presenc AI 对 GitHub REST API 的抓取，日期 **2026-05-14**（[来源](https://presenc.ai/research/ai-agent-framework-github-rankings-2026)），仅供量级参考。

---

## 1. Claude Code / Claude Agent SDK（anthropics/claude-agent-sdk-python，约 6.9k★）

**核心哲学（Anthropic 工程博客）。** "Building effective agents" 明确主张：agent 的本质就是 *"LLMs using tools based on environmental feedback in a loop"*——一个简单循环；*"you should consider adding complexity only when it demonstrably improves outcomes"*；三大原则之一是 *"Prioritize transparency by explicitly showing the agent's planning steps"*，即让模型的计划**可见**，而不是由 harness 代替模型规划（[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)）。它把"workflow（预定义代码路径）"与"agent（模型动态决定流程与工具）"区分开来，orchestrator-workers 模式的关键是 *"subtasks aren't pre-defined, but determined by the orchestrator based on the specific input"*——由 orchestrator LLM 而非代码决定子任务。

**Subagent 模型。** Claude Code 的 Task/Agent 工具在**隔离上下文**里跑子 agent，完成后**只把一段自由文本结果**返回给编排者；没有"host 侧收据校验（receipt validation）"这种东西——编排 LLM 自己读结果、自己判断质量。这正是多 agent research 系统的做法：lead agent 用自然语言把子任务派发给 subagent，再综合它们返回的文本。

**工具错误如何回给模型。** 错误作为普通工具结果（带 `is_error` 语义）回流给模型，而不是抛给用户的通用字符串。"Writing tools for agents" 讲得很直接：*"If a tool call raises an error (for example, during input validation), you can prompt-engineer your error responses to clearly communicate specific and actionable improvements, rather than opaque error codes or tracebacks"*，并建议错误里给出**正确输入的示例**，把报错变成"coaching moment"（[Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)）。

**唯一的确定性门禁 = 权限 hook。** SDK 里真正"确定性"的闸门是 `PreToolUse` / `PostToolUse` hook 和 `allowed_tools` / `disallowed_tools` / `can_use_tool`。关键设计：`allowed_tools` 只是自动批准清单，**不改变模型的工具集**；hook 返回 `permissionDecision: "deny"` 加 `permissionDecisionReason`，"provide deterministic processing and automated feedback for Claude"（[README](https://github.com/anthropics/claude-agent-sdk-python)）。也就是说，确定性逻辑被限制在**安全/权限**这一层，其余交给模型循环。

**Agent Skills 规范。** Skill 是 `SKILL.md` + YAML frontmatter（`name` / `description`，可选 `allowed-tools`），走**渐进式披露（progressive disclosure）**：先只加载元数据，命中才加载正文，再按需加载引用文件——*"a skill's body loads only when it's used, so long reference material costs almost nothing until you need it"*（[Skills 文档](https://code.claude.com/docs/en/skills)）。要点：**Skill 是"按需加载的指令/能力"，不是验收标准（acceptance criteria），也不是校验门禁。**

**AskUserQuestion。** 提问用户是一个**一等工具调用**，带可选项透传给用户，而非硬编码分支。

---

## 2. OpenAI Agents SDK（openai/openai-agents-python，约 26.3k★）+ Codex CLI

**agents-as-tools vs handoffs。** Handoff 把**整段对话**移交给专家 agent，*"the new agent takes over the conversation, and gets to see the entire previous conversation history"*；agents-as-tools（`Agent.as_tool(...)`）则**不转移对话**，把子专家当作一个内嵌工具调用、由原 agent 保持控制（[Handoffs 文档](https://openai.github.io/openai-agents-python/handoffs/)）。两种都不是"host 覆盖路由"，而是模型自己选择委派方式。

**工具错误处理（关键对照点）。** function tool 报错时，默认跑 `default_tool_error_function`，把"出错了"这件事**作为响应回给 LLM**让它重试/恢复；可以自定义 `failure_error_function` 返回更友好的、面向模型的报错文本；显式传 `None` 才会把异常重新抛出让宿主处理（[Tools 文档](https://openai.github.io/openai-agents-python/tools/)）。**默认路径就是"错误回给模型"，而不是给用户吐通用串。**

**结构化输出（`output_type`）与重试。** 用 `output_type` 声明结构化输出；当输出不符合 schema / guardrail 拒绝时，SDK 会保留已完成的 tool call 与 tool output，只剔除被拒的候选最终输出——从而支持带着校验错误再走一轮，而不是直接崩掉（[Guardrails 文档](https://openai.github.io/openai-agents-python/guardrails/)）。

**Guardrails / tripwire。** input/output guardrail 三段式：跑 guardrail → 产出 `GuardrailFunctionOutput` → 检查 tripwire；触发时 *"the runner immediately raises an exception and halts agent execution"*（`InputGuardrailTripwireTriggered` 等）。注意这是**显式、稀疏的安全闸**（可 blocking 也可 parallel），而不是每步都插一道 JSON 校验。

**max_turns / tracing。** `Runner.run(..., max_turns=N)` 是**显式预算**，超限抛 `MaxTurnsExceeded`；内置 tracing 全程记录 spans，可观测性开箱即用。

**Codex CLI。** 通过 app-server / MCP 暴露工具，采用**审批（approval）**模型（如 on-request / on-failure 审批），并支持 `dynamicTools`——工具集与审批策略是配置项，错误同样以文本反馈回模型，人类审批是显式一等交互。

---

## 3. LangGraph（约 32k★）+ langchain-ai/deepagents

**Supervisor / subagent 隔离上下文。** LangGraph 的 supervisor 图把子 agent 当节点，subagent 在**独立 state/上下文**里运行，返回消息给上层。

**`interrupt()` 做人类介入。** `interrupt()` 在图中间**暂停**，把待决动作交给人类，`Command(resume=...)` 恢复；**必须配 checkpointer** 来持久化中断/恢复之间的状态（*"Checkpointer is REQUIRED for human-in-the-loop"*）。deepagents 用 `interrupt_on` 配置哪些工具需要审批，决策类型有 approve / edit / reject / **respond**（把人类回复当作合成的 tool result，正是"ask user"工具）（[deepagents HITL 文档](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)）。

**ToolNode 错误处理。** `ToolNode(handle_tool_errors=...)`：默认把工具异常**捕获并作为 ToolMessage（错误字符串）回给模型**让它自我修正，而不是让图崩溃；可传自定义字符串或函数定制这段面向模型的错误文本。

**deepagents 的极简主义。** deepagents 刻意只有四件套：**planning tool（写 TODO 的"无操作"工具）+ 虚拟 filesystem + subagents + 一段详尽 system prompt**。它证明"强 agent"不需要一堆确定性门禁，靠好的提示 + 少量通用工具即可。

---

## 4. HuggingFace smolagents（约 27.3k★）

**CodeAgent 循环 + 错误即观测。** 每步捕获 `AgentError` 后**把错误挂到该 step 上、写进 memory 作为观测**继续迭代，而不是终止：`except AgentError as e: action_step.error = e`（[agents.py](https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py)）。错误因此成为下一步的可见上下文——模型据此改。

**`max_steps` 有清晰终止语义。** 触顶时 `_handle_max_steps_reached` 生成一条带 `AgentMaxStepsError` 的最终 memory step，**把"超预算"这件事记录进记忆**并产出一个 final answer，而不是静默失败。

**`final_answer_checks` 轻量验证钩子。** 交付前跑一组校验函数（拿到 answer / memory / agent），是**轻量、可选**的验证层——不是僵硬 schema，也不阻断整条链。

**managed_agents。** 子 agent 作为**可调用工具**挂进父 agent 的工具集，输入是自然语言 `task` + 可选 `additional_args`，返回文本。

---

## 5. OpenHands（All-Hands-AI/OpenHands）/ cline（约 61.8k★）等编码 agent

**错误逐字回流、避免"静默失败"。** 这类编码 agent 的共同点是把工具/命令的失败（stderr、非零退出码、异常）**原样作为 observation** 喂回模型与用户，模型据此改代码；用户在 UI 里也看到同一份原文。

**OpenHands 的 condenser / 记忆。** 上下文过长时用 condenser 对历史做压缩/摘要（`AgentCondensationAction` 记录压缩事件），在预算内保留关键信息——是**记忆管理**而非校验门禁。

**Stuck detection（卡死检测）。** OpenHands 的 Stuck Detector 默认开启，用**语义比较**识别五类卡死：同一 action→同一 observation 重复 4+ 次、同一 action→error 重复 3+ 次、agent 独白 3+ 条、两组 pair ping-pong 6+ 轮、反复的 context-window 错误；命中可自动**停机**避免死循环（[Stuck Detector 文档](https://docs.openhands.dev/sdk/guides/agent-stuck-detector)）。注意：这是"检测无进展→停下/上报"，不是"host 替模型改计划"。

---

## 6. Microsoft AutoGen / Magentic-One（autogen 约 58k★）

**Task ledger / Progress ledger——但都是"轻量文本"。** Orchestrator 先建 **Task Ledger**（收集事实与猜测的计划），每步再写 **Progress Ledger** 自省进度与是否完成。关键：两个 ledger 都是 **LLM 写的自然语言记录**，*"function as natural language records—LLM-written summaries of facts, observations, and plans...rather than relying on fixed computational structures"*（[Magentic-One 文档](https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/magentic-one.html)）——**不是 typed hash / 严格 schema**。

**Stall detection & replanning。** 外层循环更新 Task Ledger，内层循环更新 Progress Ledger；*"If the Orchestrator finds that progress is not being made for enough steps, it can update the Task Ledger and create a new plan"*——重规划是**由 orchestrator LLM 自己写新计划**，而非确定性代码路由。

---

## 汇总表

| Framework | subagent 结果 | 工具错误 | 结构化输出失败 | 人类介入 | 预算 | 确定性门禁 |
|---|---|---|---|---|---|---|
| **Claude Agent SDK** (~6.9k★) | Task/Agent 隔离上下文，返回自由文本，编排 LLM 自判 | `is_error` 工具结果回模型；error 里给可执行修复建议 | 无硬 JSON 契约；模型循环内自纠 | AskUserQuestion 一等工具 | 上下文/轮次由循环控制 | **仅** PreToolUse/PostToolUse hook + allow/deny 权限 |
| **OpenAI Agents SDK** (~26.3k★) | handoff（转对话）或 `as_tool`（不转），模型选 | `default_/failure_error_function` 把错误回 LLM 重试 | `output_type` 校验失败→保留 tool 结果、剔除被拒输出、再走一轮 | 可用 tool/approval + guardrail | `max_turns`→`MaxTurnsExceeded` | input/output **guardrail tripwire**（稀疏安全闸） |
| **LangGraph / deepagents** (~32k★) | supervisor 子图，隔离 state，返回消息 | `ToolNode.handle_tool_errors` → ToolMessage 错误串回模型 | 用 structured output + 重试；校验错误回模型 | `interrupt()`+checkpointer；`interrupt_on`（approve/edit/reject/respond） | 递归/步数上限 | 仅 `interrupt_on` 指定的敏感工具审批 |
| **smolagents** (~27.3k★) | `managed_agents` 作为可调用工具，返回文本 | `AgentError` 挂到 step、写入 memory 作观测 | 无强 schema；`final_answer_checks` 轻量验证 | 可插工具/回调 | `max_steps`→`AgentMaxStepsError` 记入 memory + 出 final | 无重门禁；靠 prompt + 轻校验 |
| **OpenHands / cline** (~62k★) | 子任务/命令 observation 逐字回流 | stderr/退出码/异常原样作 observation | 以自由文本为主，少用硬契约 | UI 内确认/编辑；ask-user | condenser 控上下文；stuck detector 停机 | 命令审批 + 权限；stuck 检测停机 |
| **AutoGen / Magentic-One** (~58k★) | orchestrator 派发，综合子 agent 文本 | 错误进 Progress Ledger 供重规划 | 无 typed hash；LLM 文本 ledger | group chat / 人类 agent 可插入 | 步数上限 + stall 阈值 | 轻量：stall→LLM 重写计划，非代码路由 |

---

## Principles with sources（每条 1-2 个来源）

**(a) 工具/校验错误逐字作为观测回给模型，绝不换成通用串。** 报错应"clearly communicate specific and actionable improvements, rather than opaque error codes or tracebacks"，并附正确输入示例（[Anthropic, Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)）；smolagents 把 `AgentError` 写进 memory 作观测续跑（[smolagents agents.py](https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py)）。

**(b) harness 不代替模型重规划——要么问用户、要么让模型带着错误重试。** OpenAI 默认 `default_/failure_error_function` 把错误回 LLM 让它自己恢复（[OpenAI Agents, Tools](https://openai.github.io/openai-agents-python/tools/)）；Magentic-One 的重规划是 orchestrator **LLM 自己写新计划**而非代码路由（[Magentic-One](https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/magentic-one.html)）。

**(c) 结构化输出校验 = 带着校验错误重试模型，且有界 N 次。** `output_type` 校验失败时保留已完成 tool 结果、剔除被拒输出再走一轮（[OpenAI Agents, Guardrails](https://openai.github.io/openai-agents-python/guardrails/)）；LangGraph 用 structured output + 错误回模型重试。

**(d) subagent 返回自由文本/markdown，由编排 LLM（而非代码）判断质量。** Claude Code 的 Task 子 agent 在隔离上下文返回文本，orchestrator *"determines subtasks based on the specific input"*（[Anthropic, Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)）；smolagents `managed_agents` 作为可调用工具返回文本（[smolagents agents.py](https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py)）。

**(e) skills = 指令的渐进式披露，不是验收标准。** *"a skill's body loads only when it's used"*，`SKILL.md` frontmatter + 按需加载引用文件（[Claude Code Skills](https://code.claude.com/docs/en/skills)）。

**(f) ask-user 是一等工具调用，选项透传。** deepagents 的 `respond` 决策把人类回复当作合成 tool result；`interrupt()` 暂停/恢复（[deepagents HITL](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)）。

**(g) 确定性门禁只留给安全/权限。** Claude SDK 唯一确定性闸是 PreToolUse/PostToolUse hook + allow/deny，且 `allowed_tools` 不改变模型工具集（[claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)）；OpenAI 的确定性闸是稀疏的 guardrail tripwire（[Guardrails](https://openai.github.io/openai-agents-python/guardrails/)）。

**(h) 少量显式预算（max_turns/max_steps）+ 清晰终止消息。** OpenAI `max_turns`→`MaxTurnsExceeded`；smolagents `max_steps`→把 `AgentMaxStepsError` 记入 memory 再出 final（[smolagents agents.py](https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py)）；OpenHands stuck detector 检测无进展后停机而非静默死循环（[Stuck Detector](https://docs.openhands.dev/sdk/guides/agent-stuck-detector)）。

**(i) 可观测性：完整 transcript/trace 持久化。** OpenAI Agents 内置 tracing 记录全程 spans（[Guardrails/Tracing 文档](https://openai.github.io/openai-agents-python/guardrails/)）；LangGraph 用 checkpointer 持久化状态、OpenHands 用 event/condenser 记录历史。

---

## 对该 RenderDoc 系统重构的直接映射

1. **删掉 host 侧"收据校验（receipt validation）"** —— 上述所有系统里，subagent 只回文本，由编排 LLM 判优劣（原则 d）。
2. **把僵硬 JSON 契约降级为"结构化输出 + 有界重试"** —— schema 失败时把校验错误回给模型再走一次，而非直接失败（原则 c）。
3. **通用错误串 → 逐字、可执行的错误观测** —— 附正确用法示例（原则 a、b）。
4. **Host 不再覆盖 plan/route** —— 让模型规划、必要时 `interrupt()`/ask-user，重规划交给模型（原则 b、f）。
5. **确定性门禁收敛到权限/安全一层**，其余用 prompt + skills 的渐进式披露替代（原则 e、g）。
6. 保留**少量显式预算**与**全量 trace**（原则 h、i）。
