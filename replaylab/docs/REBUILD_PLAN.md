# ReplayLab AI 重建方案：deepagents API + 厂商原生订阅 Agent

日期：2026-09-04，2026-09-05 补做统一标准审计，2026-09-06 更新当前捕获工作区与完整整理。决策人：scarecrow。本文按当前实现更新；“完成”指代码接线，真实供应商矩阵和发布验收另列于 §9。

**当前按用户要求暂停，接手先读 [PAUSED_HANDOFF_2026-09-06.md](PAUSED_HANDOFF_2026-09-06.md)。** 9/6 的工作区后台读取/取消与 DXIL 解析优化已打包部署到最终 Development v17，180122 的 6.rdc 已实际通过自动编译、ABI、替换和可编辑 UI。最后三次保存与新包 1.rdc 复验尚未做；§9 的 164104/165902 仍是修改前通过的功能窗口，不能覆盖当前未完成项。EID 性能另行暂停。


> **2026-09-06 18:57 接手复验补记：** §8.2/§8.3 之前暂停的 6.rdc 三次 Ctrl+S 与 1.rdc 跨捕获复用已在当前最新 Development 构建（qdoc `24b1115e...`）上补做并通过（`real-save-20260906-185209/verification/summary.json`、`real-save-20260906-185420/cross-rdc-verification/summary.json` 均 `passed=true`）。详见 `PAUSED_HANDOFF_2026-09-06.md` §0。EID/检查点/拖动性能线仍暂停。
## 0. 一句话

**一致性靠确定性的边界契约，不靠 ReplayLab 自己控制模型的推理流程。** API 连接固定使用 GitHub `deepagents`；ACP/Codex 订阅固定使用厂商自己的 Agent 循环；产物、报告和验收由同一 Host 代码约束。

## 1. 不可变前提

1. 订阅身份必须留在厂商正式 CLI/Agent 协议中，不能被当作普通模型令牌交给 deepagents。
2. Model API 与订阅 Agent 可以有不同推理循环，但必须调用同一个 ReplayLab MCP Host。
3. 捕获上下文只是路由快照，不是事实证据；事实必须来自本任务 Host 工具调用。
4. Skill 是文件系统 Agent Skills；运行时不再加载 Custom Agents、外部 MCP 注册表或用户提供的 Agent 代码。
5. 任务事件继续使用 `replaylab-ai-task-event/v1`，以保持 qrenderdoc 面板兼容。

## 2. 目标架构（已实现）

```text
qrenderdoc AI panel
       │ frozen context + task event JSONL
       ▼
AgentHost
  ├─ kind=model ──► DeepAgentsEngine
  │                  main Agent ──task──► Skill-derived sub-agents
  │                                      │
  │                                      └── task-scoped ReplayLab MCP
  │
  └─ kind=agent ──► NativeAgentEngine
                     ACP / Codex vendor-native single Agent
                                      │
                                      └── task-scoped ReplayLab MCP
                                                   │
                         RenderDoc facts, artifact gates, report form
```

引擎选择写死在 `replaylab_agent.cli._run_once()`：model profile 创建 `DeepAgentsEngine`，agent profile 创建 `NativeAgentEngine`。没有 `REPLAYLAB_AGENT_CORE`、legacy/lean/deepagents 选择器或运行时 fallback。

## 3. 输出一致性：三层边界

### 3.1 第 1 层：产物一致

Host 工具是产物进入产品的唯一通道：

- 首次 AI 整理产物经过 compile、当前 ABI 与适用的等价检查、organization verdict；已有项目源码直接复用，后续用户编辑不重做 AI 等价审核；
- 流程图由 Host 根据受约束的 flow spec/事实固定渲染；
- Shader 分析通过 begin 返回的 `analysisTemplate` JSON 填写，由 Host 渲染固定章节；模块说明、流程图同样使用当前接口模板，不接收模型手写 Mermaid 旁路；导出 manifest 使用对应 schema；
- 共用 `replaylab-review-verdict/v1`，原因包含 severity、code、location、direction、target 和 example；blocker 必须修正，warning 可注明后接受。轮次耗尽不能放行错误候选；Shader 的降级发布只能使用已有验证基线和通过检查的说明，未通过稿保持 provisional。
- 首次/显式 AI 整理检查所有可执行函数和计算字段：匿名编号、机械 `StageN` 切块、缺少计算模块说明的长函数须修正；业务含义不明时用有依据的表达式/用途名字。少量精度敏感密集表达式可保留为 warning，继续经过 compile、ABI 与适用的等价/数值检查，并在最终 verdict 保留警告。该质量门不加入后来用户自由编辑的保存流程。

第 1 层只判断可验证事实和工件状态，不用另一个 LLM 评价“回答质量”。

### 3.2 第 2 层：回答结构一致

`report_task_result` 把“交答案”变成固定表单。规范模型和校验器只存在于 `replaylab/mcp_host/src/task_report.py`；AgentHost 通过 `agent_report.py` 复用它。

`replaylab-task-report/v1` 固定以下字段：

- `summary`、`status: completed|partial|failed`；
- `findings[]`：claim、observed/inferred、confidence、tool evidence；
- `artifacts[]`：kind、path、status、note；
- `unknowns[]`、`next_steps[]`。

Host 检查 schema、本任务实际 Host 工具调用、推断置信度、路径存在性和状态矛盾。不存在的工具证据、产物和完成状态矛盾不会因次数耗尽而软收；缺少推断置信度等轻微问题可保守标注并接受。工具调用名只能证明该工具被调用，不能自动证明自然语言中的每个结论；保留的结果摘要用于修报告和追溯，不宣称通用语义证明。接受的报告、Host 调用结果摘要和验收状态由 task-id sidecar/事件承载，重新打开相同任务不能清零次数。

报告与 Shader 内容审核共用 `REPLAYLAB_REVIEW_MAX_ROUNDS`，默认 3、范围 1–10；原报告变量 `REPLAYLAB_REPORT_MAX_ROUNDS` 和本地 `review.json` 设置作为兼容回退。schema 错误也计入尝试数。它与下面的“补交报告追问次数”是两个不同预算。

### 3.3 第 3 层：验收打回一致

Agent 已做捕获工作却没有提交已接受表单时，Host 保留结果并生成有界、可定位、自包含的后续提示。追问包含原请求、上一答案、有大小和脱敏限制的 Host/子任务结果、真实工具名及具体修正要求。摘要被截断时可做必要的窄范围只读查询；不能为了补表重复已完成的副作用。每个 Codex 新 thread 都重新携带完整 system/核心 Skill 契约。

### 3.4 别名与源码边界

cbuffer 块与成员别名是显示层数据，按布局哈希跨 shader/捕获共享，用户条目优先于 AI 条目。Shader Workspace 的规范编辑文档仍是实际编译/替换的源码；`ai/organized.hlsl` 与 `ai/aliases.json` 是可审阅工件，不做隐式双向映射。这样诊断、watch、CAS 保存、编译 lineage 与 rollback 始终绑定同一组字节。

项目库是后台匹配来源，不是 Shader Workspace 的文件树。未加载 RDC 时树、可见文档和图为空；加载后只投影当前实例/session/path 下已匹配并获 Host 授权的源码。同一物理 Shader 的历史工作区去重，不因为字节相同合并不同 ResourceId。切换捕获保留未保存缓冲区，但禁止旧文档参与保存、F12 和补全；当前来源重新授权后才恢复。

普通直接打开 RDC 也进入后台项目恢复；以当前 inventory/catalog 匹配已接受的项目 HLSL，创建或复用当前资源工作区。进入 live start/恢复前验证当前范围；同一原始 raw 在新进程中出现不同 ResourceId 时，旧工作区授权不能继承。物理 Shader 去重覆盖持久授权与 live 会话，完整/有界 catalog 的历史记录不重复创建面板项。新建或复用已运行 watcher 都检查实例/session/路径，异步结果重新检查绑定，等待采用有界轮询。这些新增代码已经通过 Host 自动测试。旧 GraphicsAPI 枚举名仅在派生匹配中规范；反射不完整但原始身份精确匹配时只复用源码，强制新编译和当前 ABI，不继承旧执行授权、不重复发布项目 head、不写完整接口的执行缓存。164104 真实运行已完成当前工作区的自动编译/ABI/替换、编辑启动及原生 Ctrl+S 保存/错误保护/恢复，详见 §9；165902 也已完成 `1.rdc` 的新源码默认跨捕获复用，范围与证据见 §9。

### 3.5 三层验收统一的最终接线

| 层 | 统一内容 | 确定性机制 | 后端接法 | 状态 |
|---|---|---|---|---|
| 1 | HLSL、流程图、分析、导出等产物 | Host 工具生成/校验；固定模板渲染、全函数组织质量、ABI/有界输出观察、共用 severity/目标/样例 verdict；warning 保留且不阻断正确性检查，严重错误不能耗尽放行 | 所有 Agent 调同一任务级 ReplayLab MCP | 已接线；9/6 新 HLSL 已观察通过并发布，范围见 §9；各家全域矩阵不作泛化 |
| 2 | 最终回答字段、证据、状态、产物清单 | MCP Host 的 `report_task_result` + `replaylab-task-report/v1`；task-id sidecar 记录真实工具与报告；终态事件携带 `report/artifacts` | deepagents 主 Agent 与厂商原生 Agent 调同一个工具和校验器 | 已完成 |
| 3 | 做过工作但缺少已接受报告时有界追问，保留已有结果 | 各引擎结束一轮后调用同一 `ReportGate.verify()`；共享提示携带原请求、上一答案、有界 Host 事实/子任务结果、真实工具名与具体原因/目标/样例。`REPLAYLAB_VERIFY_MAX_ROUNDS` 默认 2、范围 0–5，0 关闭追问；未解决项保留在 `verification`，不得误报 completed，已有 failed 不能被缺表覆盖 | deepagents 对**同一个已编译 graph/Agent 实例**再运行一轮；ACP 对**同一个 session**再发 `session/prompt`；Codex app-server **新建 thread**，重带 system/核心 Skill 契约和任务 sidecar 事实。不使用 Claude Stop hook、另建模型 reviewer 或自研调度循环 | 已接线且有自动化覆盖；API/Codex/Cursor 同一真实报告均首轮通过，故不冒充真机已触发追问分支 |

“一致”指 Host 可检查的结构和工件一致，不要求不同模型写出相同措辞。

未完成 Shader 组织 session 使用进程 HMAC；原 stdio Host 退出后不能复用旧 seal。显式 Host 升级恢复可通过公开 begin 重新验证基线，但按工作区持久化的已用审核次数必须保留。正常存活会话内修正仍使用同 organizationId；不能用新 session 或进程清零预算。完成凭证的持久化与未完成 session 的生命周期是不同机制。

### 3.6 所有模型必须加载的统一 Skill

`.agents/skills/replaylab-core/SKILL.md` 是唯一通用标准，完整注入 API 主 Agent、域/通用子 Agent、原生 Agent 及其新 thread。核心缺失、损坏或未标记 always 时停止执行；可选域 Skill 可单独隔离。域 Skill 只增加具体操作，不另建报告格式或重试规则。模板字段来自 Host 工具返回值，样式由 Host 控制；模型不自行设计 Mermaid/CSS 或编造 schema。

## 4. 删除与保留边界（已执行）

已删除：

- `runtime.py`、`adaptive_orchestration.py`、`orchestration.py`、`planning.py`；
- 旧 capabilities/context/inner-context/ToolService 和 specialist contract；
- planner/reviewer/WorkflowGuard/工具收据/流程检查点相关测试；
- 过渡期 `src/replaylab_agent/core/`；
- qrenderdoc 旧 AI orchestration/Custom Agents 设置入口；
- pydantic-ai、FastMCP Agent 层和旧扩展注册表的打包收集逻辑。

继续保留：

- profile、凭据、模型发现、连接检测与订阅登录；
- ACP/Codex 的协议、进程生命周期和安全事件投影；
- task-scoped MCP proxy，但只负责 allowlist、效果上限、冻结 scope 和调用预算；
- `policy.py` 的纯工具效果清单；
- MCP Host、内置 qrenderdoc bridge、Skills、任务事件和打包脚本。

## 5. API 模式：deepagents

1. 所有 model profile 都由 `connectors/langchain_models.py` 构建 LangChain 模型，再交给固定版本 deepagents。
2. 主 Agent 只有 deepagents 委派、用户提问和报告工具，不直接读取 RenderDoc。
3. 每个域 Skill 形成一个受限子 Agent；`replaylab-core` standing 规则完整进入主 Agent 和所有子 Agent。按有意义的完整目标委派并合并相关查询，不要求每个事实启动一个任务。
4. 子 Agent 的 ReplayLab 工具按连接级 effects 裁剪，并共享同一个任务级 MCP 会话。
5. deepagents 的真实 event stream 映射为主/子 Agent、工具和 reasoning 事件；未完成子 Agent 会被确定性收束为 failed/cancelled。
6. 当前提问通过任务终态 `needs-user-input` 与对话信封恢复；没有以未实现的 checkpointer 作为正确性前提。
7. 离线框架集成测试必须实际 import 安装的 deepagents 并编译/运行 graph，防止只用 fake 接口造成漂移。

## 6. 订阅模式：厂商原生单 Agent

| 后端 | 会话与工具 | Skill | 用户提问 |
|---|---|---|---|
| Claude Code ACP | 隔离工作区中的严格项目 MCP 配置；同一 ACP session 多轮 | 复制到厂商项目 Skill 目录 | ACP `elicitation.form` → 面板选项 |
| 其他 ACP | `session/new` 注入唯一任务级 stdio MCP，权限请求必须映射到已允许的 ReplayLab 工具 | 提示中提供匹配 Skill/standing rules；厂商支持时使用原生加载 | 支持 elicitation 时转面板，否则按厂商协议收束 |
| Codex app-server | 启动参数只注册经过 allowlist/effect/scope/预算裁剪的任务级 ReplayLab MCP；turn 全局审批保持 `never`，仅该任务代理设为 trusted/approve；shell、Apps、multi-agent 等能力关闭 | 传入选中 Skill 路径 | 严格 `[REPLAYLAB QUESTION]` 编号块退化方案 |

每个路由固定 task-id、event root、qrenderdoc instance、scope JSON、工具 allowlist、effects 和最大调用次数。厂商 Agent 不能扩大这些边界，也不能启动 ReplayLab 子 Agent。

## 7. 面板与本地化

- qrenderdoc 展示 main/child，而不是旧 planner/specialist 角色。
- 终态先渲染结构化报告和产物，再把自然语言放入 Notes。
- `verification.accepted=false` 时显示 Host 已追问但仍有未解决项。
- Shader Workspace 只展示当前 RDC 的匹配文档，项目库留在后台；主文档绑定实际编译源码，保存/全部保存/Ctrl+S 走同一 Host CAS 与当前捕获授权。
- 图默认展示主要算法阶段、自上而下数据流并整体 fit；节点固定，空白允许平移。单击通过 native 书签定位计算行，前方增删行或目标文字修改仍跟随；删除目标整行/函数后失效，不按旧行号猜近邻。双击展开当前 buffer 的函数计算、调用和控制流，代码与图有各自前进/后退历史。
- 声明处 F12 显示可选择的本地引用，调用处跳定义；行号、符号高亮、补全与基础/编译诊断沿用原生编辑服务。局部图和本地符号索引是导航辅助，不是完整 HLSL 语言服务器或新语义证明；局部图不改写已审核项目图。
- UI 文本必须直接使用 Qt 可提取翻译调用；ReplayLab Python UI 使用 `QCoreApplication.translate("ReplayLabProjects", ...)`。
- C++ 构建使用 `/m:2`；分发前必须同步 TS、RequireComplete 构建 QM，并通过 source-literal lint。

## 8. 实施记录

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | Host shader/flow/report 工件门与统一终态产物 | 完成 |
| 1 | Model API 固定迁移到 deepagents，事件、Skill 子 Agent、任务级 MCP、打包 | 完成 |
| 2 | ACP/Codex 改为厂商原生单 Agent；报告工具迁入 MCP Host；同会话/新 thread 第三层接线 | 完成 |
| 3 | 删除 legacy 与过渡 core，CLI 收敛到 `run/profile/doctor`，清除旧 UI 配置 | 完成 |
| 4 | 自动化回归、真实 deepagents graph、Development 冻结运行时/部署/qrenderdoc 构建 | 完成 |
| 5 | cbuffer 块名别名；决定 Shader Workspace 主编辑视图 | 完成 |

`docs/handoff/wip-layer3/` 的五份阶段 3 前 Python 草稿已删除；没有运行时、打包或测试消费者，当前两条引擎路线是唯一实现依据。

## 9. 剩余风险与发布验收

**当前状态（2026-09-06，以 164104 与 165902 收据为准）：本轮 Development Shader 主线已完成实证验收。** `6.rdc / EID 11329` 已通过正常自动恢复、当前唯一可编辑文档与 12 阶段图、三次原生 Ctrl+S（修改替换、编译错误保留最后有效替换、恢复 `593c.../740...`）及正常退出；`1.rdc / EID 61523` 已通过从同一项目自动匹配新 `593c...`、当前新鲜编译/ABI/替换及可编辑文档/图加载。主收据分别是 `output/shader-workspace-20260906/real-save-20260906-164104/verification-normalized-binding/summary.json` 和 `real-save-20260906-165902/cross-rdc-verification/summary.json`，均 passed=true。后者不改源码、不重复三次保存；项目已接受资产与 generation 8 head 保持。当前更新的是 **Development**，Release 未同步本轮新版本；完整厂商矩阵、所有输入语义证明和完整 HLSL 语言服务器仍是事实边界，不列为已通过。

下面的 9/5 与 135311–162713 记录是排障历史，保留原失败/通过状态；当前状态以上段及 164104/165902 新收据为准。厂商完整矩阵、其他 API 和完整语言服务器仍是各自能力边界，不是本轮 `6.rdc` 保存流程回退。

2026-09-05 用户要求先复核主路线。本次发现的工具身份、源码复用前置、已完成凭证寿命及产品入口断点，以 `SHADER_MAIN_ROUTE_2026-09-05.md` 为当前收敛记录。`output/` 人工脚本跑通只证明该组合调用，不能代替默认产品入口的验收；不为适应每次 Host 打包重复迁移和原样组织。

**历史 2026-09-05：** 当时的公开 Shader 主路线已实际通过：项目源码直接准备、当前编译/ABI/替换、冷/热自动恢复、真实保存与恢复、图/源跳转/别名和正常退出。`native-fixed-cycle-20260905-231920/summary.json` 与 `resumed-live-save-20260905-232030-24472-033a39/summary.json` 均 passed，保存阶段显式 start/apply/AI review 均为 0；细节、原生崩溃/心跳失败历史和最终测试记录见 `SHADER_MAIN_ROUTE_2026-09-05.md` 及 HANDOFF §9.10。该交付不等于所有厂商完整 Shader 矩阵、所有输入语义等价或完整 HLSL 语言服务器。

上一段是 2026-09-05 的主路线快照，不覆盖 2026-09-06 新交互。新 HLSL `593cbe25...` 已去除执行代码的匿名数字/phi 和机械 StageN，保留 12 个主要阶段并增加有依据的计算子段。`output/shader-workspace-20260906/public-review-20260906-132912-48408/summary.json` 证明第 2/3 轮 `observational-pass`、新鲜 ABI、项目发布、revision 11 候选 `740adf26...` 的共享资源替换及持久 live start；两条可读性 warning 保留，预算没有被重置。比较仅为 `6.rdc` EID 11329 的旧重建基线 `3ded...` 与新整理版：FP16 8,294,400 数值分量超差 0，最大绝对差 0.0001983642578125，D32S8 字节完全一致，回放恢复。它不是对缺失的原始高级源码或所有输入的证明。

**历史 2026-09-06 较早回归：** 较早的完整 Host 回归为 1703 passed / 13 skipped / 601 subtests。当前范围/自动恢复 Host 修复后较早的全量 `replaylab/output/host-suite-current-rdc-final-20260906.{log,xml}` 为进程 21172 **ExitCode 0**，JUnit 计数 **2420（含子测试）**、**0 errors / 0 failures / 13 skipped**，134.469 秒。runtime 构建进程 66981 ExitCode 0，日志为 `output/shader-workspace-20260906/runtime-final-auto-restore-build.log`，构建清单为 `replaylab/output/replaylab/manifest.json`。该较早构建不覆盖后续 capture-load、源码准备和 ABI 修复；最新完整构建/部署结果见下方，`final-runtime-delivery-development.json` 已更新为最新实物。

2026-09-06 13:52 的 `native-ui-20260906-135240` 中 IDE **43 项**、固定图/函数展开 **22 项**通过，包含逗号多声明及 Return 保持 LF/CRLF；PID 61812 正常 ExitCode 0。`scope-ui-20260906-135251` 中 scope **25 项**通过、PID 61728 正常退出，但其 `hostBoundarySimulated=true`，只证明真实无捕获面板与模拟 Host 的 UI 接线，不是捕获授权/实际替换证明。版本以确切收据目录及构建实物为准；旧 34 项和可拖动节点收据仍保留为历史。

以下是 164104 最终通过之前的独立失败记录，均保留原结论。`real-save-20260906-135311` 因 Release → Development 的 route catalog 路径绑定不同返回 `refresh-required`，源码保持 `593c...`；配套 `real-ui-save-20260906-135340-50044` 等待 UI ready 超时、passed=false、tools=[]，UI commands=[]，没有执行源码修改。随后 PID 31232 退出为 `0xc0000005`；`close-crash-diagnosis.json` 证实 QApplication 在 Py_Finalize 后销毁 QTimerWrapper，强支持 fixture timer 生命周期问题，未见 shader/ABI 故障栈。

后续 `real-save-20260906-141250/toolchain-recovery/summary.json` 仍为 passed=false：公开工具链刷新后，watch 编译 `593c...` 成功，revision 12、候选 `740...`，但 apply 因 `Live Shader catalog did not match expected_capture_id` 失败；原 Release 工作区的 ResourceId 为 `1000000000000000853`，本次 Development context 为 `1000000000000000860`。UI ready=true、commands=[]，不是实际编辑成功。仅刷新工具链不能修复过期资源范围，下一步须用当前 context/catalog 重新匹配项目 `593c...` 到正确工作区。

fixture 的 timer 已改为 Python 持有，并在 UI 线程 stop/disconnect/deleteLater，收到 destroyed 后确认清理；141250 的 ui/close 收据证明 cleanup=true、timerDestroyed=true、fixtureCleanupVerified=true，PID 56804 正常 ExitCode 0。原生编辑 fixture 也改为当前 target 授权和有界分段按键，三方案内存校验仅需 6/44/39 对按键；静态/内存测试不是 GUI 保存验收。旧失败收据仍保持失败；后续 164104 已取得实际自动恢复、修改/编译替换、错误保留、恢复 `593c.../740...` 与正常退出的新收据，不以 HLSL 数值审核或全量单测替代。详见 HANDOFF §9.11。

`real-save-20260906-145657` 又暴露了直接加载入口的遗漏：公开发现无 live、没有生成 target，UI ready=false、commands=[]，源码未变；fixture 清理/删除确认完成，PID **61360 ExitCode 0**。真正原因是 `OnCaptureLoaded` 未调用 `_schedule_project_auto_activation`，旧 `test_contracts` 还错误断言 schedule=0。现已把实际生命周期入口 → timer → 项目匹配 → restore 串联，并增加 `project-load-lifecycle-tests.xml`：**56 cases / 0 errors / 0 failures / 0 skipped，5.070 秒**。executor ensure 只是依赖补强，原 start 注册也能创建 executor，不要求用户先打开 launcher。新测试不等于真实 GUI 自动恢复成功。

随后 `real-save-20260906-150923` 已实际启动恢复，状态显示完成但公开 list 无 live；`real-save-20260906-151713` 进一步证明扫描 556 个 Shader、原始 raw 目标命中当前 RID 860 共 1 个，原始身份搜索与目标 inventory 完整。索引有 4 条 CBuffer 字段路径模糊诊断，两次仍 ready=false、commands=[]，没有源码编辑；PID 42740 / 57884 各自 cleanup/timerDestroyed=true、ExitCode 0。它们不再是生命周期未启动，但也没有恢复出可编辑 Shader。

`real-save-20260906-153737/auto-restore.json` 保留原始 Host completed=true、shaders attempted=0/applied=0、editing attempted=0/enabled=0 的失败。该目录 index 中 raw 命中 1，interface/layout/物理 ABI 与旧项目一致且 physicalAbiValidated=true，但 interfaceComplete=false。确切零候选原因现已查明为旧 `GraphicsAPI.D3D12` 与新 `d3d12` 拼写不同；派生匹配已规范已知枚举名，保留历史 identity/head。该次 UI commands=[]、源码未改，PID **49776** cleanup/timerDestroyed=true、ExitCode 0，不能追记为成功。

源码准备与应用资格已分离：`sourcePreparationOnly` 只复用精确匹配的原始 raw/API/encoding/stage/entry/ABI/layout 对应项目源码，强制新编译和当前实时 ABI；不采用旧 binary/replacement、不把 incomplete 升级、不重复注册原项目 head，也不写完整接口执行缓存。`project-source-preparation-tests-final.xml` 为 **51 测试 + 59 子测试，0 errors/failures/skips，4.448 秒**。此前结果诊断 65 项、透明 observer 9 项继续保留其原范围。

`real-save-20260906-160040` 通过普通 `6.rdc` 入口建立当前工作区 **12fd011e408849a38b6b765638a4be63**（`6272e04b...`），从已验收项目导入 `593c...`，revision 1 编译得到 `740...`，真实 ABI 报告 compatible=true、differences=[]、baseline `b602...` / candidate `2efd...`，底层 shared apply 已成功。Bridge GET job 轮询 WinHTTP **12002** 提前报错，restore 又错误要求两个 ABI 摘要相同，导致未启用编辑器。auto-restore 收据仍是异常、UI ready=false/commands=[]，PID **60548** 清理确认且 ExitCode 0；没有 Ctrl+S 操作。

两处现已修复并冻结：只对已知 GET job 的结构化 12002 有界重试同 job/cursor、不重发 POST；兼容 ABI 改为读取当前严格报告，核对 report/request/source/候选/绑定后验收，真正不兼容仍拒绝。`restore-abi-digest-tests.xml` 为 **129 测试 / JUnit 198（含子测试）**，0 errors/failures/skips，8.101 秒；实际 UI 进度专项 `auto-restore-progress-tests.xml` 为 **45 测试 + 23 子测试**，0 errors/failures/skips，5.331 秒。修复前整体通过快照 `replaylab/output/host-suite-source-restore-delivery-20260906.{log,xml}` 为 **2506 JUnit（含子测试）/1822 testcase 节点，0 errors/failures/13 skipped，133.090 秒**；之前 final/frozen 的各 1 项竞争期间失败仍保留。

最新完整回归 `replaylab/output/host-suite-compatible-abi-final-20260906.xml` 已通过：**2540 JUnit（含子测试）/1833 testcase 节点，0 errors/failures/13 skipped，152.634 秒**。`output/shader-workspace-20260906/runtime-compatible-abi-final-build.log` 的打包 ExitCode 0，该阶段 Development runtime/Bridge 已部署；该阶段静态检查为 93 个模块、15 份 Skill/引用、20 份 Bridge 文件与 4 份 EXE，构建清单时间 2026-09-06T08:26:42.8745651Z；`final-runtime-delivery-development-before-async.json` 现已更新为下方独立 Host 修复后的 96 模块实物。这份构建早于下面独立 Host 寿命修复，不覆盖该新补丁。

**162713 已完成当前 Shader 自动恢复，编辑会话仍失败。** `real-save-20260906-162713/auto-restore.json` 正常 returned、completed=true，shaders **attempted=1 / applied=1 / failed=0**，source-only cache 保留，RDC 执行 index 未写；当前工作区为 `12fd011e408849a38b6b765638a4be63`。editing **attempted=1 / enabled=0 / failed=1**，报 `unknown encoding: idna`，未执行 Ctrl+S。`ui.json` 的 timer 清理完成，`close.json` 确认 PID **34332** fixtureCleanupVerified=true、**ExitCode 0**。前一轮轮询和 ABI 摘要误拒绝已通过真实恢复结果复核；不能把此次 Shader 恢复成功扩大为编辑保存成功。

**独立冻结 Host 的解包目录寿命是已复现根因。** ensure 用同一冻结 EXE 启动长期 serve 时，没有要求独立解包；ensure 退出后清理共享目录，尚未按需加载的 `unicodedata.pyd` 消失。`frozen-lifetime-before-unicode/result.json` 记录旧冻结反例：launcherExited=true、resourcesPresent=true，但 **unicodeDatabasePresent=false**；base ZIP 因进程仍打开而保留，child PID **25992** 已正常关闭。此前只检查 ZIP 的 probe 漏掉此故障，不能作为修复通过证据；这不是漏打包，预热 codec 也不是根因修复。

**Host 启动修复及实际冻结寿命回归已通过。** `internal_host.py` 与 `replaylab_host.py` 的 `_spawn_detached` 各自复制环境，仅冻结子进程设置 `PYINSTALLER_RESET_ENVIRONMENT=1`，让长期 Host 拥有独立解包目录；不修改父进程环境。`independent-host-lifetime-tests.xml` 为 **77 testcase 节点 / JUnit 112（含子测试）**，0 errors/failures/skips，2.619 秒。`runtime-independent-host-build.log` 的打包 **ExitCode 0**；`frozen-independent-host-lifetime-final/result.json` 中 internal 与 MCP 两个实际冻结 Host 均 passed=true，ensure 退出后 base ZIP、Unicode 数据库和 IDNA 模块仍存在，launcherExitCode=0、childExitCode=0（子进程 PID **46088 / 14380**）。`replaylab/packaging/tests/Test-FrozenHostLifetime.ps1` 已接入 Build。Development 已部署，最新 `final-runtime-delivery-development-before-async.json` 为 passed=true，核验 **96 个冻结模块、15 份 Skill/引用、20 份 Bridge 文件与 4 份 EXE**，构建清单时间为 2026-09-06T08:38:56.7330911Z。旧 unicodeDatabasePresent=false 与 ZIP-only 漏检反例原样保留。

**164104 的真实自动恢复、原生 Ctrl+S 与正常退出完整通过。** `real-save-20260906-164104/auto-restore.json` 正常 returned，shaders **applied=1 / failed=0**、editing **enabled=1 / failed=0**，仍走 source-only 分支、未写完整接口执行 index。`verification-normalized-binding/summary.json` 为 **passed=true / failures=[] / hostBoundarySimulated=false**，范围是 `6.rdc / EID 11329 / fragment / MainPS` 的当前捕获保存/编译/替换/恢复。验证 driver 的显式 start、compile、apply、AI review、project register 调用均为 **0**；这些工作由正常自动恢复和保存 watcher 执行，不是跳过编译或 ABI。原生键盘/Ctrl+S 将源从 `593c...` 改为 alpha 版本 `97f79c...`，实际应用新候选 **25412d0b...**；保存 `#error` 版本 `7dbacf...` 后，真实编译失败、错误可见，最后有效替换保留；再通过相同原生保存恢复源码 **593c...** 与候选 **740...** 并实际替换。`reviewAndProjectAssetsPreserved=true`，原审核 gate/轮次、项目已接受源码/分析/图及 generation 8 head 未重审改写。

`real-save-20260906-164104/ui.json` 确认 **readOnly=false、allDocumentsCurrentCapture=true、noProjectLibrary=true、graphNodes=12**，唯一可见文档为当前 workspaceId `12fd011e408849a38b6b765638a4be63`；`ui-initial.png` 是实际原生 HLSL/12 阶段图。三次保存的 native buffer 与磁盘 SHA 一致，各为 6/44/39 对按键；编译错误 UI 显示“编译失败；仍保留上一个有效替换”，恢复后显示“已编译并应用”。fixture cleanup=true、timerDestroyed=true，`close.json` 确认 PID **59784 ExitCode 0**，不宣称该验收窗口仍在运行。

**同轮测试脚本误判保留为失败。** 最初 `discovery.log` 仍假定当前 context 必须返回原始 raw `3c4afb...`，而恢复已生效、context 返回当前候选 `740...`；校正后通过当前工作区/替换的原始身份重新确认目标，结果见 `discovery-active-replacement/summary.json`。首次 `verification/summary.json` 因 Windows 路径大小写采用严格字符串比较而失败，未执行编辑；仅测试绑定比较改为规范路径、保持 instance/session 精确一致，随后 `verification-normalized-binding/summary.json` 才完整通过。两份旧失败不覆盖为成功，也不归类为产品回归。

**165902 的 `1.rdc` 默认跨捕获复用已通过。** `real-save-20260906-165902/cross-rdc-verification/summary.json` 为 passed=true、failures=[]、hostBoundarySimulated=false；正常打开 `1.rdc` 后定位 **EID 61523 / fragment / MainPS / 当前 ResourceId 1000000000000000754**，未沿用历史 RID 747。新 workspaceId **a054bb820ef14e91b90a4f2f45954be3**，路径 `.../workspaces/shaders/v2/7198fbe3bad13df517fd4505c5805f07b3fc389465ec21fa28123bd22f82b1f4`，源码 **593c...**、新编译候选 **740...**、原始 raw **3c4afb...**；来源明确为 `6.rdc` 的项目接受知识 `2cbef5ce749b366d0e37aaf96583b2f0`，head **generation 8** 保持。auto restore 为 **applied=1 / editing enabled=1 / failures=0**；唯一当前文档 readOnly=false、allDocumentsCurrentCapture=true、noProjectLibrary=true、graphNodes=12。验证期间 **sourceEdits=0、explicitMutationTools=0、sourceUntouched=true**，没有显式 prepare/compile/apply/start/review/register；实际编译和当前 ABI/替换由正常恢复执行。`ui.json` cleanup/timerDestroyed=true、`close.json` 确认 PID **48988 ExitCode 0**。这证明跨捕获复用与编辑准备，三次 Ctrl+S 仍只由 `6.rdc` 的 164104 收据证明。

`real-save-20260906-165902/launch.json` 记录该次 Development 原生实物：qdoc SHA **7734278518fb0c60f23333be4467f30b38bb51349e38b49d2be0b05917d231d3**，core **3f2d0f9601d70ad088cd6c40cdf93047bc0383212065caa1688bc8472fc4fdce**，renderdoc.pyd **342eaf08ec4cc0cd3c16bd2611a8023322bc703b4f6cdd4c406d0554c94403b3**。独立 Host 寿命修复后的最终全量 `replaylab/output/host-suite-independent-lifetime-final-20260906.xml` 为 **2546 JUnit（含子测试）/1835 testcase 节点、0 errors/failures/13 skipped，151.927 秒**，进程会话 30201 ExitCode 0。该新全量与实际冻结 probe、Development 部署及两次真实捕获收据共同收口本轮主线；不扩展为未测试输入或其他厂商完整矩阵。

本轮 Development 的当前捕获保存和项目跨捕获复用主线均已完成；API/厂商原生 Agent 与统一标准架构完成状态保持，下面列出能力与发布范围边界。

- deepagents/LangChain 是固定版本的运行依赖；升级必须先跑真实 graph 集成测试和 PyInstaller 打包。
- 构建和冻结自检核对实际 deepagents 发行版本与规范 pyproject 的精确 pin；仅可 import 不能算版本验收。没有消费者的 LiteLLM 打包选项已移除。
- 不同厂商 ACP 对 MCP、Skill、elicitation 和模型选择的支持并不完全一致；Host 必须继续 fail closed。
- Codex 的第三层使用新 thread，因此自包含提示的长度与脱敏上限是正确性边界。
- Codex 会把缺少 annotations 的 MCP 工具默认送入审批门；任务代理必须保留保守 annotations，并只在任务级过滤边界内启用 `default_tools_approval_mode = "approve"`。不得把该设置提升到全局 Host 或任意外部 MCP。
- 真实 API/订阅账号验收需要用户已有凭据和可能产生费用，不属于自动化测试默认动作。
- 同一真实 EID 证据报告已由 API、Codex 和 Cursor 首轮通过；Claude 返回认证缺失。此结果不等于各家真实模型已达到相同 shader 整理质量。直接反编译、ABI 恢复、实质整理、模板退回/耗尽、缺证据报告和后续自由编辑的 Host 分支有自动化覆盖；真实 shader 观察只代表已记录的捕获与输入。厂商矩阵范围见 `VENDOR_VALIDATION_2026-09-05.md`。
- 新 `593c...` 的 HLSL/分析/图同项目跨 RDC 默认复用已由 165902 实证；本轮新 RID 754 不沿用历史 747。9/5 仅证明旧源码的显式准备，原收据继续保留。该通过仅限已记录目标，不宣称所有捕获/Shader/输入语义均验证。
- **历史 2026-09-05 Release：** 当时的标准构建、冻结运行时部署、翻译门与 18 项原生编辑器检查已通过，见 `SHADER_ALGORITHM_DELIVERY_2026-09-05.md`。本轮 2026-09-06 修复交付到 Development，未同步 Release；其他任务后续 core/API/SWIG/qdoc 组合须独立验证，不由这些历史结果覆盖。
