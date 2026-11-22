# Shader Workspace 诊断与后续方案（2026-09-05）

本文区分运行代码的现状和待实现的产品方案。架构决定以先读的 `HANDOFF_2026-09-04.md` 为准，再参照 `REBUILD_PLAN.md`；旧 `handoff/wip-layer3/` Python 草稿已删除，不作为实现依据。

第 1–7 节保留早期诊断、历史工作区证据及当时的验证记录。用户随后明确的轻量流程、本轮源码修复和新增真实证据见第 8 节；历史部署结果不能用来证明新增能力已经通过真实捕获验收。

## 1. 当前判断与证据边界

反编译得到 HLSL、HLSL 编译成功、当前候选通过原捕获的 ABI 校验、证明整理未改变程序，以及把工件注册到项目，是不同结论。任何一步成功都不能代替后一步的证据。

当前项目注册入口要求已审核的工作区及对应源码、编译候选、ABI 报告和整理产物。因此，一个 ABI 不兼容的反编译结果即使已有可读 HLSL，也会在继续审核发布和项目注册前被拦截。这解释了“有 HLSL，但无法进入通常可编辑、保存、编译替换的 Shader Workspace 产品入口”的结构性原因；不能据此认定反编译已经完整成功，也不能归因为任意代码哈希变化。

本轮只读检查了真实工作区的 manifest、HLSL、编译输出及 DXIL 反汇编；没有运行模型任务，也没有向真实捕获应用替换。截图里的旧 watch lease 已在 HANDOFF 记录的迁移中处理，不能代替当前 ABI 错误的诊断。具体证据见第 7 节。

## 2. 三层 Host 约束的实际职责

| 层 | 当前负责什么 | 不能从通过该层推出什么 |
|---|---|---|
| 产物层 | 各领域 Host 工具执行编译、物理 ABI、整理等价、文件结构、发布绑定及事务校验 | 不代表整个用户请求已经完成，也不代表任意反编译与原捕获程序语义等价 |
| 报告层 | `report_task_result` 校验固定 schema、本任务真实工具名、推断置信度、产物路径及报告状态之间的矛盾 | 不逐句验证自然语言结论，也不把 `evidence.values` 与工具返回值逐字段比对 |
| 有界追问层 | 已做工作而没有已接受报告时调用 `ReportGate.verify()`，追问补交或修正表单 | 不规划 shader 流程，不修复 ABI，不重跑工作区步骤，也不把缺表自动判为 failed |

规范报告实现是 `replaylab/mcp_host/src/task_report.py`；AgentHost 的 `agent_report.py` 只复用该实现。`verify()` 在验收关闭、没有工作或已有报告时直接通过，否则给出 `report-missing` 或 `report-rejected`。`request_follow_up()` 消耗单独的有限轮次；`REPLAYLAB_VERIFY_MAX_ROUNDS` 默认 2，范围 0–5，0 完全关闭。

`ReportGate` 中的报告软接受只作用于报告表单。它不会给未通过的 shader 产物授予 ABI、替换或发布权限。Shader organization 的有界软发布也有自己的契约：保留通过验证的基线和未通过的解释稿，不能将真实 ABI 不兼容候选变为可替换候选。

Host 工具内部保留 source revision、candidate、ABI report 等关系，是产物的新鲜度和事务约束。它们与已经删除的自研 planner/reviewer/WorkflowGuard 不是同一层；不能因为存在这些绑定就认定旧 Agent 运行时仍在控制步骤顺序。

## 3. 三类 hash 必须按用途区分

字段名中都有 `Sha256`，但“拿什么与什么比较”才决定它的含义。同一字段可能参与身份绑定和新鲜度检查，不能仅按名字统一删除或统一放行。

| 用途 | 典型字段与对象 | 编辑代码时应怎样处理 |
|---|---|---|
| 捕获、工具链和对象身份 | 原捕获 shader 的 `rawSha256`、capture/shader key、工具可执行文件和固定文件指纹、`routePlanSha256` | 编辑派生源码不应改变原捕获身份。升级工具链应走显式迁移，保留来源并重新编译验证；不能篡改旧指纹以假装仍是同一来源 |
| 保存 CAS、revision 和证据链 | 编辑器 `expectedRawSha256`、当前源码 `sourceSha256`、候选源码快照、候选二进制 hash、ABI request/report hash、review 的最终绑定 | 修改代码后源码 hash 理应改变。保存时用旧 hash 检查磁盘是否仍是刚读取的版本；成功后记录新 hash，并让编译、ABI 和其他依赖证据绑定新 revision，旧结果不能自动授权新字节 |
| 整理等价及重建保真度 | `codeSha256` 的被比较对象、`codeSha256Equal`、`equivalenceLevel` | 仅整理时证明指定基线与提案之间允许的变化；功能编辑允许可执行代码变化，但需新编译和新 ABI，不能继续宣称它与整理前或原捕获程序等价 |

### 3.1 源码 hash 变化是正常保存路径

`shader_workspace/watcher.py` 的 `save_source()` 先检查磁盘源码 hash 是否等于编辑器读到的 `expected_raw_sha256`，再执行 compare-and-swap。新文本会得到新 hash，返回 `changed=true` 并调度编译。检查的不是“新文本必须等于旧文本”，而是“保存之前旧文本有没有被别的编辑者改过”。

`shader_live_edit.py` 只有在 watcher 已追上当前源码、没有待编译工作，并且 `lastCompiledSourceSha256 == observedSourceSha256` 时才调度候选应用。这防止用户保存了 N+1 版而 Host 仍将 N 版候选当作当前结果应用。

因此，删除 CAS 或 candidate/source/report 的绑定会带来覆盖并发编辑、复用过期 ABI 或应用错误候选的问题。正确处理是产生新 revision 并重新验证，保留最后成功替换和 rollback 信息。

### 3.2 codeSha256 不等于通用语义等价

DXBC/DXIL 的 `rawSha256` 对完整容器求 hash；`codeSha256` 对被选中的可执行 chunk 内容求 hash，规范实现在 `symbols/dxbc.py`。它避免把容器中任意外围变化都当作可执行块变化，但也不是去除一切名字和元数据后的语义规范形。DXIL 可执行块可能包含名字元数据，重命名也可能改变其 hash。

早期修复的两条快速审核路径为：

1. `exact-code`：在固定编译条件下，提案与该次重建基线的 `codeSha256` 相同。
2. `rename-only`：即使上述 hash 不同，也可用 Host 验证过的 rename map 和 token 比对，证明只改了允许的标识符、注释和空白。两边仍须通过新鲜的编译及 replay ABI 校验。

本轮增加 `observational-pass`，允许实质整理通过捕获输入下的有界数值比较；详见第 8 节。它不以 hash 相等作为放行条件，也不声称所有输入都等价。

该机制在发布前还会重新核对密封的 session/source 证据和 token 证明，不能仅相信可编辑 JSON 中的 `equivalenceLevel` 字段。

新增计算、改变常量、改变控制流等不属于 `rename-only`。这不说明用户不能修改 shader；它说明功能编辑应走编辑和替换路径，不能沿用“只整理未改行为”的审核声明。现有 rename-only 检查也不是对所有数学等价程序的证明器。

另一个容易混淆的比较是“原捕获程序 ↔ 未编辑反编译输出的重新编译结果”。它与“反编译基线 ↔ AI 整理提案”是两组对象。后者通过，不能倒推前者保真；ABI 一致只证明物理接口契约，不能证明像素计算、分支和采样结果与原程序一致。

## 4. 编辑草稿、可替换候选、可发布结果应分离

### 4.1 已有能力与当前耦合

现有 watcher/live edit 支持源码变化后的编译，以及失败后保留上次成功结果。当前问题集中在更早的产品入口：项目注册通过 `_register_reviewed_workspace_with_readback()`，需要 `validate_publication_gate()` 和 `_active_workspace_publication_gate()` 认可的工作区，不能把未通过 ABI 的反编译稿直接当作已审核项目成果。

主编辑文档仍应绑定实际编译/替换的规范源码。根据 HANDOFF 的决定，`ai/organized.hlsl` 和 `ai/aliases.json` 是可审阅工件，不做隐式双向映射；未来如需别名副视图，应明确是只读展示。

### 4.2 建议方案：新增草稿入口，保留应用和发布门

下表是后续产品方案，不代表已实现的新工具、字段或 UI。

| 能力 | 建议允许的状态 | 必须保留的边界 |
|---|---|---|
| 打开、保存、继续修复源码草稿 | 有来源和路径绑定的 HLSL，即使尚未编译或 ABI 不兼容，也可明确作为草稿进入编辑区 | 使用规范源码和 CAS；展示当前错误；不能因打开/保存成功而开启自动替换或冒充已审核成果 |
| 编译并应用当前候选 | 当前源码 revision 编译成功，精确目标重绑定完成，当前候选通过新鲜 ABI 校验，并满足现有作用域和效果授权 | 验证绑定到该候选；保留最后成功替换和 rollback；ABI 失败时仍可编辑草稿，但不能应用该候选 |
| 发布审核结果及项目知识 | 声明的具体成果满足自己的审核契约，源码/候选/报告/整理产物对应同一版本 | 整理稿继续满足 exact-code 或 rename-only；有意修改行为的编辑成果需要自己的状态和来源声明，不能借用旧 organization verdict |

建议把“添加到编辑区”与“发布已审核知识”拆成明确入口，而不是让现有 publication gate 接受 `compatible=false`。草稿入口需要独立、范围受限的 Host 工具或现有项目模型扩展；不能通过绕过 Host、手写已接受 gate 或直接复制记录进知识库来实现。

源码保存后，编译及 ABI 状态应先变为待更新；与旧 revision 绑定的整理说明和流程图应显示过期。编译失败和 ABI 失败是当前候选的状态，不能让草稿丢失，也不能抹掉上个成功候选的证据。

建议验收覆盖：ABI 失败稿可打开保存但不能应用；连续保存只应用追上当前源码的候选；注释/安全重命名允许 hash 变化；有意修改运算可走新候选验证但不继承整理等价；编译或 ABI 失败保留最后成功替换；过期 revision 的报告不能授权新候选。

## 5. 原生 Codex 新 thread 的事实保留缺口

### 5.1 已核对的运行行为

`deep_engine.py` 复用同一个已编译 graph/Agent 实例；ACP 在同一个 session 上继续 `session/prompt`；Codex 分支再次调用 connector，`CodexAppServerConnector.run()` 每次执行 `thread/start`。三条路径复用同一 `ReportGate` 和 `verification_follow_up_prompt()`。

共享提示当前包含原请求、拒绝原因及精确修正位置、真实工具名、上一答案的前 4000 字符。deepagents 另传子任务报告，使用约 12000 字符的总预算；native 两条路径都传 `children=()`。任务报告 sidecar 记录工具名、报告和拒绝状态，没有独立保存完整 Host 工具返回值。

因此，“已有 Host 事实”在 native 追问中实际上依赖上一答案的内容。若模型第一轮没有写出某个已观察字段，或关键事实位于第 4000 字符之后，新建 Codex thread 的追问就不会仅凭该机制获得它。ACP 仍有会话历史可以使用；Codex 新 thread 无法依赖旧 thread 的历史。提示当前却统一要求不重做捕获工作，这会放大事实遗漏时补交可靠报告的难度。

报告层检查 `evidence.tool` 确实在本任务调用过，但不校验 `evidence.values` 的具体值是否来自那次调用。这个事实保留缺口不能用“报告已经被接受”证明不存在。

### 5.2 建议补齐的最小契约

建议由任务级 Host 记录有界的工具结果事实摘要及受控产物引用，并由共享提示构造器读取。摘要需要保留准确工具名、调用标识、冻结 scope、关键返回字段、错误 code/location/direction，以及对应产物的版本绑定；它只传递证据，不新增自研推理循环或工具顺序状态机。

新 thread 的自包含提示应明确保留了哪些事实、截断了哪些内容，以及可通过哪个受限只读入口重新获取缺失证据。无法恢复的字段应允许标记 unknown，不能要求模型仅凭工具名补出 observed 值。

验收应直接检查新 thread 收到的输入，而不是在 fake 模型第二轮注入预设的正确报告。至少覆盖：第一轮没有复述关键字段、关键事实位于长答案截断之后、多次同名工具调用返回不同值、错误结果、产物更新后的旧摘要、提问中断，以及全部数据受 scope/大小/脱敏边界约束。

## 6. 已阅读的测试覆盖与尚未声明的结果

本节记录测试代码覆盖的意图，不代表本轮已执行通过。实际命令、通过结果和已部署版本由本轮验证汇总补充。

| 测试文件 | 已有覆盖 | 本文关注的不足 |
|---|---|---|
| `replaylab/tests/test_agent_verification.py` | 默认/关闭/上限、定位反馈、deepagents 复用子任务结果、预算耗尽不强制失败 | 未覆盖原生新 thread 独立 Host 事实包 |
| `replaylab/tests/test_native_engine.py` | ACP 同会话追问、Codex 多次 connector 调用与报告被接受 | `test_codex_native_agent_uses_new_thread_for_verification` 的 fake 第二轮直接提交预设报告，未证明必要事实被携带；connector 的真实 thread 行为须结合实现和连接器测试理解 |
| `replaylab/mcp_host/tests/test_shader_equivalence.py` | 安全重命名、注释空白、运算改动的定位拒绝 | 这些是受约束整理证明，不能代替一般功能编辑的验收 |
| `replaylab/mcp_host/tests/test_shader_workspace_watcher.py` | CAS 保存产生新 hash、旧 hash 冲突不覆盖、编译失败后新保存可恢复、最后成功候选保留 | 草稿注册与已审核发布分离尚需新增产品测试 |
| `replaylab/mcp_host/tests/test_shader_organization.py` | 发布绑定、篡改与并发写保护、代码改动保留为解释稿、review 后 live edit 失败保留最后成功替换 | 不能以此声称当前问题捕获的 ABI 或视觉保真已通过 |

## 7. 本轮证据与修改汇总

### 7.1 真实工作区的接口损失

捕获为 `D:\Study\抓帧\白银之城\6.rdc`；工作区绑定来自 EID 11329 的 `fragment/MainPS`，Shader ResourceId 为 `1000000000000000860`。截图顶部后来显示的 EID 5155 不能替换这个历史任务的绑定。

已检查工作区：

```text
D:\Study\抓帧\ReplayLab Project\workspaces\shaders\v2\7abd418e42c39ed8ca57efda4c7ab802581cef954cafa83947b39acf14687718-recovery-dxil-spirv-hlsl-migrated-20260904
```

该目录的 `manifest.json`、`source/shader.hlsl`、`revisions/0001/shader.bin` 以及 `jobs/baseline-dxil-disassembly-*/output.ll`、`jobs/candidate-dxil-disassembly-*/output.ll` 给出以下差异：

| 接口 | 捕获基线 | 重编译候选 |
|---|---|---|
| CBuffer b0 字节尺寸 | 44 | 48 |
| CBuffer b1 字节尺寸 | 7572 | 7584 |
| CBuffer b3 字节尺寸 | 5704 | 5712 |
| CBuffer b5 字节尺寸 | 892 | 896 |
| t0/t1/t3/t5/t11 | StructuredBuffer | typed `Buffer<uint4>` |
| 输入语义 | 包括 `TEXCOORD10_centroid`、`COLOR` | 被映射为 `TEXCOORD1`、`TEXCOORD3` 等 |
| 输入签名 | 包含 `SV_IsFrontFace` | 缺失 |

HLSL 把 CBuffer 重建为 `float4` 数组。这不仅涉及名字；现有严格 ABI 契约下，布局、资源种类和签名都不匹配。上述每项差异不都能单独推出画面必错（例如未使用输入可以被优化掉），但当前信息不足以证明这组转换满足替换契约，不能把全部差异降为警告。

对基线和候选反汇编调用固定 DXIL CBuffer 访问提取器，均遇到 `CBufferLoadLegacy is not a supported static DXC form`。原始非 recovery 工作区的 RenoDX 日志还记录了 `Unexpected dynamic cbuffer offset.`。因此不能把“改走 RenoDX”当作已验证的解决方法，也不能仅靠改 HLSL 声明或补齐尾部字节宣称 ABI 修复完成。

### 7.2 已修复的两处代码问题

1. **整理审核与注册的 hash 策略不一致。** 原 review 已接受 `rename-only`，但可信发布校验和 server 的后续结构校验仍要求 baseline/organized code hash 相等，且把 hash 不等误标为 baseline recovery。本轮统一 exact-code / rename-only 两条路径，将判断及 rename map 绑定到密封 session，发布时重新核验实际源码 token 证明，保留全部 revision/source/candidate/ABI 校验。更新了模型可见工具说明。此修复不接受新增运算冒充重命名，也不绕过实际 ABI 失败。
2. **ABI 失败只返回、不留存。** 旧实现失败后仍只显示 `pending-replay-reflection`。本轮新增只用于诊断的 `lastAbiAttempt` 和 `abiValidationStatus`，保存并重新验证绑定候选的失败报告。`incompatible` 表示当前源码对应候选已拒绝；`source-changed` 表示已有编译证据不再对应当前源码；新 revision 不继承旧失败。新鲜失败会撤销旧候选的缓存 compatible 授权，保留历史报告及最后成功替换记录；再次成功验证可恢复。旧的 `latestCompiled` 与顶层兼容布尔字段描述编译候选，当前源码是否仍匹配应读取新诊断和 source 绑定。

`REBUILD_PLAN.md` §3.5 第 3 行已按 HANDOFF 将确定性机制与各后端接法分开；仍是同 deepagents graph、同 ACP session、Codex 新 thread，没有恢复旧工作流控制器。

旧真实工作区不会凭空获得过去未保存的失败报告；新运行时需要一次新鲜验证才能写入该记录。本轮没有修改用户工作区，也没有声称当前捕获通过新的 ABI 验证。

### 7.3 早期后续实施顺序（进展更新见第 8 节）

1. **先拆草稿编辑入口。** 按第 4 节允许失败 HLSL 作为草稿打开、保存和手动编译，同时显示失败报告；不自动替换、不注册成已审核共享知识。这最直接解决“无法继续编辑修复”的产品阻塞。
2. **再修复反编译保真。** 增加可证明的动态 CBuffer 索引范围及访问类型分析，恢复原输入签名、StructuredBuffer 元素类型/stride，并逐类通过严格 ABI。保留这份实际捕获作为本地验收案例，用可分发的最小 shader 复现做回归；通过后再组织、生成图、注册及验证编辑替换闭环。
3. **补齐后续发布分支。** 当前 `softAccepted` 能保留基线和说明，但可信注册仍只接受已提升的 proposal，baseline/final/provisional 三者的发布绑定还需单独实现和验收；不能只放行一个状态枚举。需要一并检查拒绝后同一 organization session 的重提交流程。
4. **补齐新 thread 的证据传递。** 按第 5 节传递有界 Host 事实摘要，防止长答案截断后仅剩工具名。

以上是早期修复结束时的待办快照。新的软发布、重提及后续用户编辑同步已经有源码实现，见第 8 节；此处历史证据没有使这份 ABI 不兼容 HLSL 自动成为可替换或可发布结果。

### 7.4 验证与部署

最终 Host 回归在 `replaylab/mcp_host` 目录执行：

```powershell
..\.venv\Scripts\python.exe -m pytest -o addopts='' -q --basetemp=../output/pytest-shader-diagnosis-final-20260905-a32f tests/test_shader_abi.py tests/test_shader_workspace.py tests/test_shader_workspace_routes.py tests/test_shader_workspace_mcp.py tests/test_shader_organization.py tests/test_shader_equivalence.py tests/test_active_project_mcp.py tests/test_shader_live_edit.py tests/test_shader_workspace_watcher.py tests/test_shader_editor_client.py tests/test_dxil_access_signature.py tests/test_hlsl_cbuffer_abi_repair.py
```

结果：**328 passed、2 skipped、125 subtests passed**。

Agent/Skill 回归在 `replaylab` 目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -o addopts='' -q --basetemp=output/pytest-shader-diagnosis-agent-20260905-a32f tests/test_skills.py tests/test_agent_verification.py tests/test_native_engine.py
```

结果：**32 passed**。首次使用系统共享 pytest 临时目录时，测试体完成后退出清理触发 Windows 权限错误；使用上述独立目录重跑成功。相关修改通过 `git diff --check`。

另用已安装的真实 DXC 做了独立 `ps_6_6` 编译冒烟：只改 CBuffer、字段和参数名，两份源码均编译成功，Host token 证明为 `renameOnlyEquivalent=true`，实际二进制结果为 `codeSha256Equal=false`。这直接复现了合法重命名不能强制 code hash 恒等的情况。脚本、HLSL、DXIL 和结果保存在仓库 `output/shader-gate-smoke-20260905/`；该冒烟不包含 replay ABI 或真实捕获替换。

独立审查还覆盖了成功验证后编辑源码：新诊断必须显示 `source-changed`，不能把旧候选的 compatible 当作新源码验证通过。持久化故障注入说明另一条边界：若保存失败报告后写 manifest 失败，工具返回错误并保留旧 manifest 快照；这不是已提交的新验证，不能声称旧授权已持久撤销。本轮未增加磁盘故障恢复 journal，实际 apply 仍要求新鲜反射。

冻结运行时已完成 `Build-ReplayLabRuntime.ps1`，并通过 `Deploy-ReplayLabRuntime.ps1` 部署到 `x64/Development/replaylab/`（版本仍为 1.13.0）。三份可执行文件的部署字节与构建输出 SHA-256 一致；打包和部署的静态/参数安全测试通过。构建日志在 `output/shader-gate-runtime-build-20260905.log`，冻结 Host 的工具目录启动探针通过。

本轮没有修改 C++ UI 或 Qt 翻译目录，因此未重新链接 qrenderdoc。检查时没有运行中的捕获 Bridge，冻结探针明确报告未连接；没有启动模型、打开真实捕获或做运行中的 Shader 替换验收。下次启动 Development 应用会使用更新后的三个运行时。

## 8. 用户澄清后的轻量流程进展

本节记录新增实现及待验收边界；没有把早期测试、旧部署或候选编译成功当作真实流程已通过。

| 环节 | 本轮契约 |
|---|---|
| 获取 HLSL | 优先可编译原生源码、能直接成功的固定工具路线。字节码也可能直接恢复成功；只有具体 ABI 差异才需要修复，正常路线不预先要求 AI 修复或生成测试向量 |
| 整理快速路径 | `exact-code` 或 Host 证明的 `rename-only` 通过即可；当前基线编译和 ABI 证据仍有效就复用 |
| 实质 AI 整理 | 每个完整函数/计算阶段批次调用一次 review；Host 临时回放两份候选，对相同捕获输入比较输出，使用固定绝对/相对浮点容差、较严深度容差和整数精确比较，并恢复回放 |
| 测量结论 | `observational-pass` 只表示覆盖到的输出在容差内，`semanticEquivalenceVerified=false`；不证明所有输入，也不凭空推定工具结果与原始源码相同 |
| 修正与兜底 | 普通拒绝允许同一 session 修正重提；预算耗尽密封并注册 `soft-accepted-baseline`，可用源码是已编译且 ABI 兼容的基线，失败提案另存；采样不支持不算通过 |
| 交付与复用 | 整理 HLSL、分析和流程图保存到当前项目。注册展示面板后，还需 checked shared-resource apply、持久 live edit 和 canonical 可编辑文档读回，不能把只读面板当作完成；其他 RDC 可项目匹配复用，应用前仍重新绑定候选与接口 |
| 用户后续编辑 | 允许任意源码变化；保存 CAS、重新编译、当前 ABI 与替换检查仍保留。显式登记成功替换时，项目同步使用独立路径，不再要求旧 AI 整理 gate，也不发布过期说明和流程图；不自动把每次临时编辑覆盖成项目共享版本 |
| CBuffer 别名 | 新接线从原始反射取得名字和布局 key，按 bind/space 对应重建块，按唯一完整字节范围对应字段，再用原key写已有别名系统，用户优先；不明字段仅保留解释，实际原面板显示待验收 |

源码、字节码和报告 hash 仍用于确认测试了哪一份、保存是否冲突、候选是否过期。要求它们绑定正确版本，与要求新旧代码 hash 相等是两回事。本轮还修复了 deepagents 默认删除 replay 权限造成的能力缺口：子 Agent 现在服从配置的真实上限；review 标记 `workspace + replay`，项目注册打开 UI 标记 `project + ui`。默认可编辑保存替换的交付需要持久 live edit；解释或显式仅导出请求不因此获得 apply/watch 授权。

别名只读核查发现实际反编译块为 `_41_43` 等，原捕获 catalog 则为 `cbuffer0` 等，而且物理 ABI `fields=[]`/`ambiguous` 不能提供原面板的布局 key。旧按 HLSL 名字生成的别名文件即使写入成功，也不代表原面板会读取。新接线保留原始 ShaderReflection 顶层名字与偏移，不经过会删除这些名字的物理 ABI 投影；不明成员只允许已确认的块别名，不虚构字段对应关系。

已执行的局部回归包括 organization（39 通过、1 跳过）、equivalence / CBuffer aliases / flow graph（24 通过）、Skill / policy / deep engine（40 通过）及真实 deepagents graph 集成（1 通过）。这些是独立临时目录中的代码测试，没有模型消费；实际新增 Host/Bridge 的最终综合测试和部署结果待本轮完成后补记。

### 8.1 持久编辑必须由同一个内部 Host 持有

发现并修复了一处进程生命周期断点：旧 MCP 工具在当前 Agent/MCP 进程创建 live-edit manager，但 qrenderdoc 编辑器保存固定发往另一个 `ReplayLabInternal` 进程。内部 Host 启动时只恢复一次持久记录，随后创建的会话不会自动进入它的内存；因此会出现“已启动 live edit，但编辑器保存不认识 liveEditId”。

现在 start/list/poll/stop/migrate 五个既有 MCP 操作都经专用 `/replaylab/shader-live-edit` 接口转发给内部单例，同一 manager 接收编辑器 `/replaylab/shader-source/save`，持有 watcher 和持久授权。MCP 进程不再自行恢复持久会话。该接口只有固定的五种操作，不是通用工具执行入口；客户端校验固定 loopback、Host identity/generation、token 和返回绑定。每次操作绑定明确的 qrenderdoc instance、capture session 和路径；后台应用线程重新进入同一绑定，不能依赖 HTTP 线程的 ContextVar 自动传递。

内部 Host 启动恢复只读取精确实例清单和工作区身份，不同步调用仍在等待它启动的 qrenderdoc，避免互相等待。编译候选真正应用前仍有新鲜捕获与 ABI 检查。自动回归覆盖独立调用者、同一 manager 保存、线程绑定、错误 session、旧 generation、重定向拒绝及启动阶段不调用 replay。

### 8.2 UI catalog 身份与首次项目登记

UI 的 `captureId` 属于某次导出的 shader 集合。刚打开 RDC 时它可以为空；同一 RDC 的单事件 catalog 与全帧 symbol catalog 也可能有不同 ID。原来直接要求 UI ID 与工作区一致，会把有效工作区挡在 organization 或项目注册之前。

现在工作区操作使用自己的不可变 catalog，验证 exact `(captureId, evidenceSha256)`、原 shader 字节和事件使用集合，再核对当前 instance/session、路径和 API。通过后仅给本次操作使用 canonical catalog ID；不把空 UI ID 当作授权，不改写当前符号视图，也不从别的捕获继承 ABI。组织与别名读取使用同一精确工作区绑定。

首次保存项目时还可能没有导入该 catalog revision。源码中的 `register_active_project_workspace` 在两道 publication 校验通过、源码事务锁仍持有时检查项目是否已登记精确 revision；仅缺失时导入工作区已有 catalog，并在写入前后重新核验 catalog 与当前捕获。它不另导出整帧，也不放松项目匹配条件。真实首次登记和后续 HLSL/分析/流程图注册已分别留存于仓库 `output/shader-workflow-20260905/initial-project-capture-import.json` 与 `register_active_project_workspace.json`；首次导入本身没有执行替换。

初次整理会可靠保存项目共享源码。用户之后任意修改不再要求 AI 等价审核；需要更新共享版本时，既有显式 replacement 登记检查实际 active candidate、当前源码和新鲜 ABI，再提交项目快照，过期分析/流程图不随新版本冒充当前文档。跨 RDC 复用仍按原捕获 shader 身份匹配，并对目标捕获重新绑定和验证。

### 8.3 数值观察的格式、计数和恢复边界

数值比较在单个 replay callback 内执行，读取当前绑定 MRT/深度视图的完整输出数据，使用视图格式解释 typeless 资源。当前只支持单采样、单层二维输出，每个资源最多 32 MiB、总计最多 64 MiB；不支持的输出明确拒绝。RenderDoc 枚举使用名字规范化，不能把 `Regular=0` 误判为无格式；层数使用实际 output descriptor 的 `numSlices`。

- 浮点分量使用绝对 `1e-4` 加相对 `1e-3` 容差；半精度颜色的绝对下限为 `1/1024`，深度绝对容差 `1e-6`，整数精确比较，归一化颜色允许一个存储量化步长。NaN/Inf 不当作通过。每个分量都检查，少量坏点不会被平均误差掩盖。
- `comparisonKind=numeric-components` 的 `comparedComponents` 是解码后分量数。打包格式没有通用数值解码器：字节完全相同可以用 `comparisonKind=byte-exact` 通过，此时兼容字段 `comparedComponents` 记录字节数；字节改变则不支持。顶层 `sampleCount` 是这些计数的合计，不能统称像素数或浮点采样数。
- 采集或比较不支持时，只有确认恢复原 replay 映射后才返回 `accepted=false`、`sampleCount=0` 和原因。恢复失败必须返回 RPC 错误，不能宣称 `replayRestored=true`。原字节码被 `BuildTargetShader` 去重为原 ResourceId 时直接回放原管线，不执行自替换。

真实 `6.rdc`、EID 11329、`fragment/MainPS` 的修复候选已通过编译和新鲜 ABI。`final-recovery-observation.json` 比较原捕获与重新编译结果：1920×1080 的 `R16G16B16A16_FLOAT` 共 8,294,400 个分量，最大绝对误差 `6.103515625e-05`、超差分量 0；`D32S8` 的 16,588,800 个存储字节完全相同。该次观察耗时约 37.4 秒，`accepted=true`、`replayRestored=true`、`semanticEquivalenceVerified=false`。这证明该事件输出在容差内，不宣称所有输入都等价。后续整理独立通过 `exact-code` 快速审核，生成图与源码 revision 2 的绑定见 `review_shader_workspace_organization.json`。

### 8.4 六个外部 CBuffer 块别名已有写入证据

`original-cbuffer-alias-identity.json` 保留原反射身份；`rename_shader_workspace_symbols.json` 返回六个块的 `capturedIdentityMatched=true`、`blockAliasWritten=true`、`published=6`：

| 原反射块 | 外部 AI 显示别名 |
|---|---|
| `cbuffer0` | `GlobalsCB` |
| `cbuffer1` | `ViewCB` |
| `cbuffer2` | `ToonMainLightCB` |
| `cbuffer3` | `CartoonBasePassCB` |
| `cbuffer4` | `MaterialCollection0CB` |
| `cbuffer5` | `MaterialCB` |

它们写入既有 `%LOCALAPPDATA%/ReplayLab/aliases/cbuffer/`，按原布局 key 保存，条目来源标为 `ai`。本次原反射存在重复 `unknown` 和不可靠偏移，未虚构成员别名对应关系；成员 `written=[]`。块名推断不改变物理反射或 shader 字节，用户已有别名仍优先。文件写入证据不等同于原 CBuffer 面板显示验收。

### 8.5 独立进程保存、编译替换与恢复已通过

`test_live_save.py` 已实际运行：调用者 PID 25732、内部 Host PID 5060，使用已部署编辑器的 WinHTTP 客户端和真实 `/replaylab/shader-source/save`，没有直接写规范源码。将唯一 `SV_Target.w = 0.0f` 临时改为 `0.125f`，待新候选应用后再通过相同 HTTP/CAS 接口恢复原文。

| 阶段 | 原始源码 SHA-256 前缀 | 编译 revision | 已应用候选 SHA-256 前缀 |
|---|---|---|---|
| 初始整理源码 | `4e76aac1ea3d` | 2 | `185c9154be21` |
| 临时功能修改 | `788d15dd78a9` | 3 | `3b401dcdb5c0` |
| 恢复原文 | `4e76aac1ea3d` | 4 | `185c9154be21` |

两次保存均为 HTTP 200、`changed=true`、`compileScheduled=true`。每一步 poll 都核对当前源码、最新编译 revision、active candidate 和实际 apply 结果；汇总为 `editApplied=true`、`restoreApplied=true`、`sourceRestored=true`、`passed=true`、`failures=[]`。这直接验证了“后续用户功能编辑可以改变源码与字节码，而不需要重新接受 AI 等价审核”，同时保留编译、ABI 与精确替换检查。证据为 `live-save-summary.json`、`live-save-edit-http-response.json`、`live-save-restore-http-response.json` 及两份 `*-applied.json`。

`ui-editable.json` 从原生面板读回 `documentKind=live-edit`、规范 `source/shader.hlsl` 路径、`readOnly=false`；项目库条目是独立的只读文档。该次 UI hash 差异已精确定位：原文件有 3190 个 CRLF 和 35 个单独 LF，`_document_source_text` 按检测出的 CRLF 统一保存换行，233269 字节变为 233304 字节。对原文执行实际实现后得到的 `7f2b085cee74...` 与 UI 两条文档的 hash 完全相同，逐行内容不变。`ShaderCodeEditor::GetText()` 使用完整 `textLength()+1` 的 UTF-8 读回，没有固定长度截断；没有未决的 UI 内容差异。复核见 `ui-editable-normalization.json` 的 `allDocumentProjectionsMatch=true`、`nonNewlineContentEqual=true`。保存恢复的原始字节结论由 HTTP 回执及规范文件字节读回证明。验证使用单独的 `6.rdc` 实例，用户当前窗口中的捕获未因该测试切换。

### 8.6 最终部署与可编辑交付已完成

最后一版 runtime 已构建并部署到 Development，版本为 1.13.0；三个冻结可执行文件与构建输出字节一致，五个本轮关键 Bridge 模块与仓库源码字节一致，Bridge 部署共更新 16 个文件。证据为 `final-deployment.json`，构建日志为仓库 `output/shader-workflow-runtime-ready-20260905.log`。部署 guard 已按目标应用安装目录检查进程，不再把 `Program Files` 下另一个原版 RenderDoc 实例误判为占用；用户原版程序及其 `1.rdc` 保留。

重开后的 Development qrenderdoc（验收时 PID 27728）已加载 `6.rdc`。交付使用最终工具链下的新工作区：

`D:\Study\抓帧\ReplayLab Project\workspaces\shaders\v2\recovery-11329-20260905-ready`

`final-delivery-check.json` 确认 workspaceId 为 `64939d93b3f6454e9e11c21277b91909`、revision 2，`replayAbiValidated=true`、`applied=true`、`liveState=running`。可编辑文档绑定这份规范源码，`readOnly=false`、`uiTextMatchesCanonical=true`（使用实际换行投影）；项目保存的 HLSL 原字节匹配规范源码，`projectSourceMatchesCanonical=true`。当前 revision 的流程图重新生成并发布，`flowGraphCurrent=true`，分析和 Host 生成 Mermaid 的路径均已读回。新版内部 Host 的独立保存调用也成功；提交未改变的原文返回 `changed=false/compileScheduled=false`，没有无意义地重复编译。

本轮“反编译修复 → 整理/流程图 → 编译替换 → 项目保存 → 可编辑保存”的主流程已完成，live edit 保持运行供用户继续修改。此前 `...-final` 的功能修改/恢复测试与本节 `...-ready` 的最终部署交付分别保留证据，不混用 workspace/revision。没有额外声称对这个新 ready 会话又执行过一次重启自动恢复验收。其他 RDC 的实际项目复用尚未在本轮真机验收；其精确匹配、重新绑定和新鲜 ABI 约束仍保留。

通用 ABI 失败草稿入口与原生 Codex 新 thread 的独立事实包，仍不能因上述局部实现而宣称完成。
