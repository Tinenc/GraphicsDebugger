# Bread AI 实现审计与瘦身重构蓝图

> 本文记录重构前的审计与设计依据；其中的“当前实现”与迁移顺序描述的是审计时状态。实际运行边界请看 [AI_RUNTIME_ARCHITECTURE_2026-09-24.md](AI_RUNTIME_ARCHITECTURE_2026-09-24.md)。历史耗时数字是旧版本样本，不能当作本次修改后的性能结果。

审计日期：2026-09-24。范围是当前工作树中的 qrenderdoc UI、AgentHost 的 API/ACP 路径、任务 MCP、Host、Shader Workspace 和相关 Skill。这是一份设计审计；本文没有改变运行逻辑，也没有把旧任务日志当作当前版本的性能基准。

## 核心判断

问题并非“AI 不够自主”或“所有验证都多余”，而是三类责任被混在了一起：

1. 用户决定目标：例如分析 EID 11384 的 PS、整理整份源码、给资源命名、最终替换回放。
2. AI 决定方法：读哪些事件、纹理、CBuffer 和资产，如何命名、选择函数边界、分段、合并与解释。
3. Host 保护效果：精确目标和当前绑定、源码版本冲突、用户已有内容、编辑作用域、最终编译/ABI/回放状态。

目前第 2 类方法被长提示、Skill、质量阈值和报告续跑反复规定；第 3 类中又有一些前置检查妨碍了尚未发布的草稿工作。目标是让 Agent 拥有方法选择权，让 Host 只对真实操作提供工具、事实和确定性校验。用户过去明确要求的语义命名、整份整理、最终可编辑源码和函数调用流程图仍是交付目标，不能因为删除固定步骤而消失。

## 当前真实调用链

~~~text
qrenderdoc UI / 聊天与按钮
  ├─ 当前捕获快照、会话历史、选中 EID、用户意图
  └─ replaylab-ai CLI
      ├─ API profile → DeepAgentsEngine → 主 Agent / 可选 Skill 子 Agent
      └─ ACP / Codex profile → NativeAgentEngine → 厂商原生单 Agent
          ↓
      SkillGateway：目录/搜索 → 精确 schema → invoke_bread_tool
          ↓
      任务 MCP proxy：工具 allowlist、效果上限、目标范围、调用预算
          ↓
      MCP Host server → Shader Workspace / 项目 / 资产读器
          ↓
      qrenderdoc 内置 Python bridge → 当前 replay、编辑器与 UI
          ↑
      Host 收据、任务 JSONL、ReportGate → 自动续跑或最终答复
~~~

入口见 [PipelineStateViewer.cpp](../../qrenderdoc/Windows/PipelineState/PipelineStateViewer.cpp) 1550–1569 行、[AIAssistant.cpp](../../qrenderdoc/Windows/AI/AIAssistant.cpp) 7307–7336、12786–13169 行；引擎分流见 [cli.py](../src/replaylab_agent/cli.py) 1314–1417 行。API 和原生 Agent 各自运行模型循环；共同使用任务 MCP 与 Host 的物理操作。MCP 和 bridge 之间还存在一次到 qrenderdoc 实例的受控 IPC。

UI 的“Organize and Apply Shader”按钮明确承诺整理、注册、替换和打开实时编辑。自由聊天不应自动继承这个完整效果范围；按钮触发的最终应用也不应意味着必须按固定工具顺序执行。

能力边界也要如实呈现：[source_language.py](../mcp_host/src/shader_workspace/source_language.py) 目前只对 HLSL 提供完整结构提取；GLSL 的安全重命名、注释等能力更窄。反编译与资源读取覆盖多个图形 API，资产读器可识别部分 Unreal/Unity 资源，但这不等于任何引擎、任何 Shader 语言都能自动完成同级别重构。ACP 与 API 共用 Host 工具和物理检查，模型运行器与提示装配仍有差异。

## 谁在控制，以及控制是否放对了位置

| 层 | 现在的作用 | 重构后的边界 |
| --- | --- | --- |
| UI intent | 按钮选择 Skill，并生成 required whole-shader completion contract | 只记录用户要求的目标、范围和是否包含回放应用；显式对话目标优先于 UI 焦点 |
| Agent 提示与 Skill | 详细规定资产调查、命名、分段、审查、交付顺序 | 只写任务结果、证据要求、语言/可读性规范和不可违反的事实边界；方法由 AI 选择 |
| 工具目录 | 196 个 Host 工具隐藏在目录、精确 schema、统一 invoke 后 | 少量高频 EID/源码/编辑/状态工具直接暴露；长尾工具按需发现，仍受同一 grant |
| Host 编辑 | 维护 baseline、proposal、结构 ledger，并反复重放全源码 | 单一版本化 draft 加源码 SHA CAS；增量解析/局部作用域检查；保留可回滚编辑记录 |
| Host 审查 | 技术校验和主观可读性评分一起计算 organizationComplete | 编译/ABI/当前候选绑定是硬校验；可读性指标只提供诊断和待办 |
| 报告门槛 | 可据完成状态自动追加最多 64 个 Shader 模型轮次 | 从工具收据自动形成事实状态；模型自行继续或如实报告未完成，报告格式不能驱动源码操作 |
| 回放效果 | review、finish 等工具可能同时验证、提升源码、注册、应用 | 显式分开“验证草稿”“发布源码”“应用回放”“启动实时编辑” |

实现位置：[tool_scope.py](../src/replaylab_agent/tool_scope.py) 419–448 行创建完成契约；[task_report.py](../mcp_host/src/task_report.py) 2938–3011 行把它转成后续任务；[workflow_followup.py](../src/replaylab_agent/workflow_followup.py) 154–241 行允许最多 64 个 Shader 续跑轮次。即使 Host 文案称“Agent 自主”，这一组接受标准仍能持续改变模型下一步的工作。

## 已确认的主要复杂度和耗时来源

### 1. 常驻指令过长且重复

[agent_prompts.py](../src/replaylab_agent/agent_prompts.py) 16–94 行中的 Shader 流程约 29,220 字符，无条件放入原生 Agent、API 主 Agent、API 子 Agent 的系统提示（307、690、770 行）。常驻 [replaylab-core](../../.agents/skills/replaylab-core/SKILL.md) 指令约 23,412 字符，按需 [Shader Skill](../../.agents/skills/renderdoc-shader-workbench/SKILL.md) 又约 16,762 字符。一个简单问候在原生路径上仍可能携带六万多字符系统提示。提示进度里的 promptChars 主要统计原始请求，不能代表真实模型输入。

三层文字重复规定资产先行、分段建议、名称发布、最终 review 和回放交付；核心 Skill 的流程图模板规定与 Shader Skill 的“从最终函数调用自动生成”也不一致。删除重复句子之前，需要把测试中匹配原句的断言改为行为和物理不变量测试。

### 2. 完成契约混合了用户目标和质量启发式

用户要求“整份整理”应当被记住，但 [quality.py](../mcp_host/src/shader_workspace/quality.py) 764–928 行的“函数超过 160 条语句”“匿名名计数”“函数注释数”“临时变量链”等并不能单独判定真实可读性。[organization.py](../mcp_host/src/shader_workspace/organization.py) 5136–5152 行允许技术上有效的源码发布，同时把组织完成状态记为 false；[task_report.py](../mcp_host/src/task_report.py) 2938–3011 行可能因此继续推动模型工作。质量指标应成为可定位的提示，不应自动构成无止境的模型续跑。

### 3. 工具发现多次往返

[skill_gateway.py](../src/replaylab_agent/skill_gateway.py) 530–774 行先返回目录或搜索结果，再加载单个工具的完整 schema；[mcp_proxy.py](../src/replaylab_agent/mcp_proxy.py) 672–704 行和 [deep_engine.py](../src/replaylab_agent/deep_engine.py) 914–952 行都拒绝未先加载 schema 的调用。对首次使用的高频操作，这至少多一轮模型与工具往返。延迟加载长尾工具有意义，但高频操作不必每次走三段式。

### 4. 草稿有两套互斥的编辑权威

Agent 可以自由写 proposal，但随后的重命名和结构提取要求它逐字等于“baseline + 全部 ledger 重放”；否则认为 draft 漂移。[organization.py](../mcp_host/src/shader_workspace/organization.py) 3702–3709、4084–4092 行。每次 rename/restructure 还读取整份源码，重放已有操作并重建静态报告。这样机械操作会随 ledger 增长而变慢，也让一份已经合理改动的草稿反而难以继续。应选择当前 draft + SHA CAS 为唯一编辑权威，编辑日志用于撤销和审计，而非每次从初始基线重算。

现行 Host trace 的小样本中，6 次 rename 合计约 11.2 秒、2 次 restructure 合计约 11.2 秒；主要子阶段是 proposal replay 和静态分析，写入事务本身较短。这个样本不能外推到所有 RDC。更早一项长任务的可见 7 小时 44 分中，Host 变更事务覆盖约 61% 的时段；它也不能被解释为纯 CPU 时间或全部归因于最新代码。

### 5. 技术检查发生得过早，工具效果混合

[organization.py](../mcp_host/src/shader_workspace/organization.py) 3461–3478 行在 begin 时先编译和验 ABI，失败便不能开启组织草稿。单纯阅读、命名或修复源码并不需要先通过这一门槛。局部 rename/restructure 已延迟整份编译到最终 review，这是正确方向；但 begin、review、finish 仍需分开对待。[server.py](../mcp_host/src/server.py) 8421–8504 行的 segment verify 默认可编译整份并打开预览；finish 可以连带注册、应用与 live edit；review 自身会提升通过的 canonical 源码。调用名和副作用应一一对应。

### 6. 语义名称被单一路径的证据锁住

[server.py](../mcp_host/src/server.py) 22202–22333 行只允许带同平台 compiledLayoutReceipt 的语义 CBuffer 别名，否则只能用机械 offset 名。精确 byteOffset、大小、bind/space、Shader 原始身份与布局仍须由 Host 核对；但同平台已编译材质布局不应是所有来源的唯一命名路径。可把名称区分为“已验证映射”和“AI 推断”，连同证据与置信度呈现；不支持的成员保留未知。`shaderResourceId` 是应用到当前捕获时的物理目标标识，不适合充当跨捕获语义匹配键。现有 [aliases.py](../mcp_host/src/shader_workspace/aliases.py) 已允许旧 alias 缺失该 ID；损坏的旧条目应隔离并报告，继续用原始字节、阶段、当前绑定和布局处理新捕获。

模型也不应负责生成满足严格格式的 operation_id；Host 可以生成 ID，并从请求内容与当前源码版本保证幂等。

### 7. 运行记录、会话记忆与文档存在落差

UI 可携带大量历史，Agent 提示只保留最多 60,000 字符旧消息（[agent_prompts.py](../src/replaylab_agent/agent_prompts.py) 326–341 行）；跨用户回合主要靠重放历史，而不是持久的模型线程。应保存简短结构化目标记忆：捕获、明确 EID/阶段、原始 Shader 身份、当前 draft SHA、已完成函数、未完成尾部和已应用名称。它是提示上下文，不是 Host 权限。

Host 每个工具调用都记录 sidecar，利于审计，却也有同步 I/O；需要分段计时才能判断它是否成为瓶颈。[ARCHITECTURE.md](../ARCHITECTURE.md) 还描述默认 480 次工具预算和 API 主 Agent 无直接捕获工具，而当前 [engine_config.py](../src/replaylab_agent/engine_config.py) 39–50 行默认 0 表示不限、主 Agent 已有 lazy gateway。重构时应让文档和测试以实际行为为准。

## 目标架构

一条任务只需要一个清晰的目标与一个真实状态记录：

~~~text
用户目标/EID/阶段
    ↓
AI 自选证据与编辑策略
    ↓
Host 读工具 + IDE 编辑工具
    ↓
可见、可恢复的版本化 draft（未验证也可保存）
    ↓ 仅当准备交付或应用时
最终候选：编译 + ABI + 必要的有限行为比较
    ↓
显式发布源码 → 显式替换当前回放 → 显式打开实时编辑
    ↓
从实际收据生成结果；AI 解释已完成和未知项
~~~

Host 每次草稿编辑只需检查目标、source SHA、作用域/语法与用户内容保护。全源码编译只针对要交付的确切候选以及修正后的新候选；同一 SHA 的有效收据可以复用。行为比较只能说明已测输入/输出及容差范围，不应被说成数学上的完全等价。草稿要在 Shader Workspace 中可见，并明确标注“未验证”；用户无需等最后一次 review 才看到整理进度。

语义质量由 Agent 对整份源码负责：从实际表达式与消费者命名、减少无意义临时量、把相关计算组成函数、处理所有请求范围内的大函数。不能证明语义时用精确的操作角色名或保留未知。读取窗口由上下文和语法决定，不固定 100、200 或 320 行。流程图在源码与函数说明稳定后，从真实调用关系生成；若用户任务不包含流程图，则不让图表阻断源码交付。注释遵循 RenderDoc UI/系统语言。

## 建议的迁移顺序

1. **建立对照样本与计时。** 固定同一 RDC/EID，在 API 和至少一种 ACP 连接上记录任务结果、工具调用数、模型输入/输出量、目录/schema 往返、Host 各子阶段、编译/ABI、源码与回放/IDE 状态。区分旧版本日志和当前版本。
2. **先减提示。** 常驻 core 只留证据真实性、效果授权、用户内容保护和诚实报告；移走无条件 Shader 长段。Shader Skill 保留目标和质量标准，工具操作细节放工具说明。更新按钮 tooltip，删除“资产先行”等固定顺序。
3. **拆开报告与续跑。** 将 receipt/progress 自动投影为事实状态；报告格式错误不再重做 Shader 工作。删除质量阈值驱动的自动 64 轮续跑。若用户要求整份整理，Agent 自行维护覆盖记录并如实说明剩余部分。
4. **改工具表面。** 直接提供常用 EID 上下文、绑定资源、源码概要/分页、符号作用域、批量重命名、局部提取、draft 状态和最终验证工具；长尾仍按需加载。保留 effect grant 和 schema 校验，移除“必须先查目录”这类非安全前置条件。
5. **统一 draft 编辑。** 当前版本化源码为真相；局部补丁、重命名、提取走同一 CAS API。缓存符号/函数索引，仅更新受影响区域；跨作用域时扩大分析范围并给出具体拒绝原因。先允许未编译 draft，再把 validate、publish、apply、live edit 分成清晰效果。
6. **改名称证据模型。** Texture 名对当前绑定与内容匹配；CBuffer 名对当前物理 offset/layout，附“已验证/推断/未知”证据级别。旧文件迁移失败只影响相应行，不阻断源码工作。Host 生成幂等操作 ID。
7. **回归再部署。** 用行为测试替换对长提示原句的测试；验证旧项目、别名和收据兼容后，分别打包 AgentHost、MCP Host 与 qrenderdoc，执行仓库规定的翻译和构建检查。

最先实施第 1–3 步；它们可以明显减少模型上下文和续跑，而不需要先重写全部 Shader 编辑器。第 4–6 步是解决 Host 机械编辑耗时与草稿可见性的主要工程工作。

## 不应删的物理边界

- Effect grant、当前捕获与用户指定 EID/阶段的准确绑定；UI 当前选中事件只是回退提示。
- 原始捕获 Shader 身份、当前绑定资源、源码版本 CAS、用户已有源码和别名保护。
- 提取时不能破坏分支、循环、局部变量作用域、求值顺序与资源副作用。
- 最终回放应用前对确切候选编译、检查 ABI 和当前 replay 锚点；已应用效果需要精确 rollback/live-edit 收据。
- 用实际工具结果说明观察与推断；不能把编译成功或有限样本比较称为完整语义等价。

这些边界只在对应操作发生时检查。旧会话 ID、草稿所有者、资产索引状态、流程图模板、报告字段和任意代码行数阈值，都不应单独否决一个安全的源码编辑。

## 验收场景

| 场景 | 期望 |
| --- | --- |
聊天指定 EID，与 UI 当前 EID 不同 | AI 读取指定 EID 的绑定并在对该目标的效果调用中使用其实际 Shader 身份 |
仅请求解释或离线整理 | 不启动组织/回放全流程；可以交付说明或可见 draft |
有资源内容匹配但旧 alias 缺 ResourceId | 重新绑定当前捕获资源、发布可证明名称；坏行跳过并诊断 |
有 CBuffer 名但没有同平台 ShaderMap | 保留物理 offset 校验，清楚标识推断，继续独立源码整理 |
2000 行 Shader | AI 自选完整计算边界并批量局部编辑；不会每段编译、注册或替换 |
起始源码不能编译 | 仍可读写未验证 draft；发布/回放时才必须修正编译 |
未通过最终 ABI 或目标已变化 | 具体候选不应用，保留草稿和错误；不会用旧成功候选冒充新成果 |
最终通过并要求应用 | 同一确切源码版本注册、替换当前回放，在 Shader Workspace 中可见并可编辑 |
GLSL 或不具备提取能力的路线 | 做已支持的命名/注释，明确限制，不强行套用 HLSL 流程 |
ACP 与 API 同任务 | 目标、工具能力、物理检查和完成判断一致；只允许运行时实现差异 |

本方案的外部设计依据是：[MCP 工具规范](https://modelcontextprotocol.io/specification/draft/server/index)将工具列为模型可选择的能力；[OpenAI Agent 运行文档](https://developers.openai.com/api/docs/guides/agents/running-agents)与[Skill 文档](https://developers.openai.com/api/docs/guides/tools-skills)将工具选择和任务说明放在 Agent 层；[Tree-sitter 增量解析文档](https://tree-sitter.github.io/tree-sitter/using-parsers/3-advanced-parsing.html)说明语法树可按编辑增量更新。这些是设计参考，不构成对当前 RenderDoc/Shader 支持能力的保证。
