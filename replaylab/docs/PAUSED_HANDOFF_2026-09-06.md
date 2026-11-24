# RenderDoc Ultra 开发暂停与接手清单

更新时间：2026-09-06 18:06（北京时间）。仓库：`D:\Study\GitHub\renderdocUltra`。

**用户已要求暂停开发和测试，交由其他 AI 接手。不要自动恢复，也不要安排后台续跑。** 本文件是最新状态；旧文档中“已完成主线”只对应当时的构建和测试窗口。

## 0. 接手复验结果（2026-09-06 18:57，新 AI 接手后补做）

接手 AI 已在**当前最新 Development 构建**（qdoc SHA `24b1115e61120c6271b92d9cbeed4d6432766cb94e54003aadb07074c843df6b`，即 §7 `build-v17-receipt.json` 记录的 async/独立 Host 寿命修复包，比 164104/165902 的旧包 `7734...` 更新）上补齐了 §8.2/§8.3 之前暂停的两项实机复验，均通过：

- **6.rdc / EID 11329 三次真实 Ctrl+S**：`output/shader-workspace-20260906/real-save-20260906-185209/verification/summary.json` 为 `passed=true / failures=[] / hostBoundarySimulated=false`。改 alpha（源 `97f79c...`→候选 `25412d0b...`）实际替换；注入 `#error`（`7dbacf...`）编译失败、错误可见且保留上一有效替换；恢复 `593c...`/`740...` 重新替换。验证期间显式 start/compile/apply/AI-review/register 均为 0；`reviewAndProjectAssetsPreserved=true`，项目已接受源码/分析/图与 head generation 8、knowledgeId `2cbef5ce...` 未变。三次保存 UI 收据 `saved=true`，`#error` 轮 `compilerErrorsVisible=true`。
- **1.rdc / EID 61523 跨捕获默认复用**：`real-save-20260906-185420/cross-rdc-verification/summary.json` 为 `passed=true / failures=[]`。当前 RID `1000000000000000754`、新 workspaceId `a054bb820ef14e91b90a4f2f45954be3`、源 `593c...`、候选 `740...`、原始 raw `3c4afb...`，来源为 6.rdc 项目知识 `2cbef5ce...`；`sourceEdits=0`、显式 mutation 工具 0、12 节点图、generation 8 保持。
- 编排入口：`output/shader-workspace-20260906/orchestrate_real_save_verification.ps1`（`RUN_VERIFICATION.cmd` 启动），总结果 `orchestrate-20260906-185209/orchestrate-summary.json` 为 `overallPassed=true`。它内联复刻了 `start_real_save_ui.ps1` 的启动/环境，并在跑 discover 前显式等待 `startup.json` 定型与 `auto-restore.json` 的 `outcome==returned`（editing enabled≥1）。
- **已知非产品瑕疵**：编排脚本 Close-Gui 未在 25 秒窗口内确认关闭，两次 `close.json` 为 `exited=false / fixtureCleanupVerified=false`，两个 qrenderdoc 进程（pid 39748/34800）需另行关闭（`output/shader-workspace-20260906/CLOSE_LEFTOVER.cmd`）。但两个 fixture 自身均 `cleanup=true / timerDestroyed=true / cleanupReason=success`，源码已恢复 `593c...`，属编排脚本关闭时序问题，非 Host/产品回归；159902/164104 的既有正常退出收据不受影响。
- **代码静态审查（接手 AI）**：§6 关键修复均在当前工作树在位且正确——ABI 兼容门明确允许两 digest 不同（只校验 `status==compatible`、`differences==[]`）、WinHTTP 12002 仅对同 job/cursor 的 GET 有界重试不重发变更、冻结子进程 `PYINSTALLER_RESET_ENVIRONMENT=1` 寿命修复、organization 仅 blocker 提前拒绝、`shader_console` 用 `threading.local` 做取消 scope 隔离。Host/Bridge/Agent 三处 Python 语法编译通过，无 TODO/FIXME、无裸 `except:`。未发现代码缺陷。

**本轮 Development Shader 主线（含三次 Ctrl+S 与跨捕获复用）至此在最新构建上完成实证验收。** EID/检查点/拖动性能线仍按 `output/interactive-replay-20260906/PAUSED_BY_USER.md` 保持暂停，未恢复。Release 未同步本轮版本；完整厂商矩阵、所有输入语义证明与完整 HLSL 语言服务器仍是事实边界。

## 1. 先读什么

1. `AGENTS.md` 与 `CLAUDE.md`。
2. 本文件，然后 `replaylab/docs/HANDOFF_2026-09-04.md`，再读 `replaylab/docs/REBUILD_PLAN.md`。后两份包含历史排障，不能用旧失败描述覆盖新收据。
3. `.agents/skills/replaylab-core/SKILL.md` 和 `.agents/skills/renderdoc-shader-workbench/SKILL.md`。
4. EID 性能单独看 `output/interactive-replay-20260906/PAUSED_BY_USER.md`；这一任务需要用户明确恢复，不能因继续 Shader 验收而顺带恢复。

本次涉及两个现有任务：“修复Shader ABI校验与工作区流程”和“优化EID拖拽播放速度”。较早“重构 AI 模块三层输出”“修复反编译失败与权限阻塞”的决策已合并进当前 HANDOFF/REBUILD_PLAN，不需要重新启动一套架构。

## 2. 总体进度

| 开发项 | 已做内容与证据 | 尚待处理 |
|---|---|---|
| AI / Agent 架构 | API 固定走 GitHub deepagents 0.3.3；ACP/Codex 订阅走厂商 Agent。删除旧自研 planner/reviewer/orchestration/runtime 推理链；冻结包也检查无旧模块 | 各厂商完整 Shader、提问、退回耗尽的实测矩阵未完成；Claude 登录不可用，不能冒称通过 |
| 统一 Skill、模板、验收 | replaylab-core 注入 API 主/子 Agent 与厂商 Agent；Host 控制 HLSL/图/报告模板、证据和最多 3 轮修正，严重错误不能靠耗尽次数通过。REBUILD_PLAN §3.5 第 3 行已按 HANDOFF 决策改写 | 已接线并有自动测试；完整模型实测仍按上项补齐 |
| Shader 主路线 / ABI | 可直接编译的源码或工具反编译结果走快路；真正不兼容时才修 ABI。当前会话完整 ABI 与资源身份决定能否替换，源码哈希不表示语义等价 | 最新版本的 `6.rdc` 自动匹配→编译→ABI→替换→可编辑 UI 已实际通过；最后三次 Ctrl+S 与 `1.rdc` 复验被用户暂停 |
| HLSL 整理与项目复用 | 已接受整理源码保存到项目，外部 CBuffer 别名复用。该样例匿名 `_数字` 标识符已清零，主要算法分成 12 阶段；保留有精度依据的表达式 | 不能把该样例的完成推广成任意 Shader 都可自动正确还原；不同模型的新输出仍必须走同一验收 |
| 着色器工作区 | 无 RDC 不显示旧文件/图；当前 RDC 匹配并按实际 Shader 去重；隐藏项目库；原生可编辑文档、Ctrl+S Host CAS、编译错误保留最后有效替换 | 最新后台刷新补丁后，完整三次保存复验待继续 |
| 流程图 / IDE | 节点固定、默认适应视框、上下数据流；单击书签式锚点、双击函数局部展开，独立前进后退；行号、F12 定义/引用选择、基础补全/高亮/诊断 | 基础 IDE 已有测试，不是完整外部 HLSL 语言服务器；未证明任意复杂语法的全部语义引用 |
| 卡顿与退出修复 | 工作区文件/授权检查移后台，结果按捕获和编辑状态隔离；关闭可取消。DXIL 解析移出 replay 回调并去掉平方级扫描和重复解析；冻结 Host 独立解包寿命修复 | 新 `6.rdc` 窗口有约 0.594 秒最大 500ms 心跳间隔；这只是该窗口观察，不能宣称全局 UI 或 History 延迟已验收 |
| 构建 / 中文 | Development v17 原生、内置 Bridge、4 个运行 EXE 已统一部署并核验；4884 条翻译完成 | Release 尚未同步本轮版本；不要混用 Release/Development 路径、工具链和旧会话 |
| EID / 检查点 / 拖动性能 | 保留 v17 代码、Windows/Linux/Android 构建和已有单测/Material 实测 | **单独暂停，未完成“Unity 般实时拖动”目标。** v17 GPU live-forward 正例和最新 History 复验未做，详见独立暂停文档 |

## 3. 暂停时的安全状态

- 本任务最后测试进程 **11528** 已正常关闭：`real-save-20260906-180122/close.json` 的 `fixtureCleanupVerified=true / exited=true / exitCode=0`。
- 两个 fixture 均 `cleanup=true / timerDestroyed=true`；公开 discovery 也正常完成。没有继续运行的本任务 GUI/GPU、构建、验证脚本或自动续跑安排。
- 180122 的 `ui.json` 为 `commands=[]`：没有执行三次保存，没有用户源码修改。源码仍是下节 `593c...`，项目工作区 head 仍为 generation **8** / knowledgeId **2cbef5ce749b366d0e37aaf96583b2f0**。
- 最新 `ui.json` 的 `ok=false` 是未执行 fixture 的完整 finish/保存验收；不能改为 passed。自动恢复与编辑 ready 的独立收据已经通过，不等同于完整保存通过。
- 工作树包含大量当前及其他任务的未提交修改。没有创建提交；不得 reset、整仓清理、覆盖未提交工作或直接手改用户项目的授权/验收记录。

## 4. 当前样例、源码与身份

项目：`D:\Study\抓帧\ReplayLab Project`（白银）。

- 主样例：`D:\Study\抓帧\白银之城\6.rdc`，EID **11329**，fragment / MainPS，DXIL / ps_6_6。
- 跨 RDC 样例：同目录 `1.rdc`，EID **61523**。
- 原始 Shader raw：`3c4afb434934308919e884efa26523f832807f2adc839d3a3754ceb559255205`。
- 已接受 HLSL：`593cbe25a817b680426e34f3bb1f5551710b0824d26be6157a7fcfdb6433aab9`。
- 已接受编译候选：`740adf26008aff2dc7998b1b94decd20b51c0dc028022a9e3e5a743683cc6268`。
- 6.rdc 最近工作区：`12fd011e408849a38b6b765638a4be63`，目录 `workspaces/shaders/v2/6272e04b1bfa0e9a03ae3df5e5f25e076782c893044af4b084e08172f773c72e`，相对上述项目根。
- 最近 6.rdc 原始 ResourceId 为 `1000000000000000860`；最近 1.rdc 为 `1000000000000000754`。**它们是历史收据中的值，接手后必须重新发现当前 ID/session/授权，不能照抄。**
- 6.rdc 原 ABI digest `b6020009d362764b604f9abd81b0e1880ecfcca4d997816d632ccd7845a2219e` 与候选 `2efd2ed6bbfe19536f32e879b7a1260602c6291289e7e57dba1fca23b32d625b` 不同，但已由物理接口和 DXIL CBuffer 访问证据证明 compatible、differences=[]。不要恢复“两个 digest 必须相同”的错误门槛。
- HLSL 提案/统计：`output/shader-workspace-20260906/readability-proposal.hlsl` 和 `READABILITY_REVIEW.md`。5462 行、12 顶层阶段，73 个主要计算模块；总计 129 个函数，保留资源绑定/采样数量等约束。物理 CBuffer 名称与偏移不为可读性乱改，使用现有外部别名。

数值证据来自 `public-review-20260906-132912-48408/summary.json`：旧重建版→新整理版在实际 6.rdc 的 FP16 1920×1080 输出比较中，8,294,400 分量均在允许误差内，最大绝对差 0.0001983642578125；D32S8 精确相同。它不是丢失的原始高级源码证明，也不是所有输入空间证明。

## 5. 哪些实机验收已做

以下目录均相对 `output/shader-workspace-20260906/`。

| 运行 | 验收范围 | 结果 |
|---|---|---|
| `real-save-20260906-164104` | 较早修复包，6.rdc 自动恢复；原生 Ctrl+S 改 alpha 并替换；保存 `#error` 后编译失败仍保留上版；恢复源码/候选；正常退出 | `verification-normalized-binding/summary.json` passed=true。并非模拟 Host，显式 start/compile/apply/review/register 工具调用为 0；正常产品流程完成工作 |
| `real-save-20260906-165902` | 较早修复包，1.rdc 正常打开，从项目自动匹配新 HLSL，当前编译/ABI/替换，可编辑唯一文档及 12 节点图；无源码修改 | `cross-rdc-verification/summary.json` passed=true；三次保存没有在 1.rdc 重复执行 |
| **`real-save-20260906-180122`** | **当前 v17 + 最终异步/解析修复包**，6.rdc 正常自动恢复、公开 discovery、可编辑文档/图 | `auto-restore.json` completed=true、applied=1/failed=0、editing enabled=1/failed=0；`discovery/summary.json` passed=true；UI ready=true/readOnly=false、当前唯一文档、无项目库、12 节点。用户暂停前未开始三次保存，随后正常退出 |

180122 的最大 UI 心跳间隔约 0.594 秒，122 个 500ms timer 样本，无超过 2 秒的间隔。fixture 自身也计入间隔，窗口有限；不能当作独立公平性能基准，更不能覆盖暂停的 History/EID 测试。

## 6. 代码位置与最近修复

| 位置 | 作用 / 接手注意 |
|---|---|
| `replaylab/mcp_host/src/project_restore.py`、`shader_workspace/project_source.py` | 项目源码复用、强制当前编译/ABI；反射不完整只允许 sourcePreparationOnly，不假造完整执行缓存 |
| `shader_workspace/workspace.py`、`shader_live_edit.py`、`internal_tool_service.py` | 当前身份、保存 CAS、应用/失败保留、去重与 live 会话 |
| `replaylab/renderdoc_mcp_bridge/project_shader_workspace.py` | 后台 snapshot 同时准备授权源码/stat；UI 以 capture/session/generation/documentToken/textRevision/save 状态发布；close 停 save 后取消 snapshot |
| `replaylab/renderdoc_mcp_bridge/shader_console.py` | 严格项目/源码读取；新增 worker-local `cancellable_console_reads`，不改变原校验、不污染其他线程或保存路径 |
| `replaylab/renderdoc_mcp_bridge/__init__.py` | Catalog 的 replay 原生采集与纯 CPU finalize 两阶段；不让 SWIG 对象跨出回调；256MiB/job DXIL 快照上限，超出标 unavailable；取消/换捕获/关闭不得发布迟到结果 |
| Bridge 与 Host 的 `dxil_access_signature.py` | 逐 Shader 的 SSA 使用索引代替每次 load 全文扫描；新 `extract_dxil_cbuffer_evidence` 只做一次访问解析，layout 无法证明仍为 unavailable；旧严格 API 保留 |
| `project_shader_flow_graph.py`、`project_shader_flow_hierarchy.py`、`qrenderdoc/Widgets/ShaderCodeEditor.*` | 固定布局、锚点、函数内部层次、代码导航/引用/行号等；图和代码历史独立 |
| `internal_host.py`、`replaylab_host.py`、`packaging/tests/Test-FrozenHostLifetime.ps1` | 冻结子 Host 用独立 PyInstaller 解包；不是“缺失 idna 打包”，不要用预热 import 替代寿命修复 |
| `replaylab/src/replaylab_agent/`、`.agents/skills/` | 新引擎与共同契约；不重新引入旧 AI 调度循环 |

真实 DXIL 新旧结果完整一致，旧双 API 耗时约 11.82/18.46 秒，新约 0.215/0.238 秒；收据 `dxil-parser-before.json`、`dxil-parser-after.json`、`dxil-parser-comparison.json`。这是纯解析观察，不是 GPU 加载速度。旧 parser 在 `dxil_parser_before_async.py` 仅作为输出目录里的对照证据，不参与产品。

## 7. 当前构建与测试收据

- 使用 **`x64/Development/q_render_soco_doc.exe`**。Release 未同步。
- `output/interactive-replay-20260906/build-v17-receipt.json`：qdoc SHA `24b1115e61120c6271b92d9cbeed4d6432766cb94e54003aadb07074c843df6b`；core SHA `75bd613b41140a9c4b8dcec381efec64ad421d8ebc5962ed19c919a1ed17df70`。公共 Python ABI 未变，内部协议 5。
- Runtime 构建清单时间 **2026-09-06T09:54:23.8388421Z**；构建与部署日志为 `runtime-async-final-build.log`、`runtime-async-final-deploy.log`。
- `final-runtime-delivery-development.json` passed=true：**99 冻结模块 / 15 Skill与引用 / 20 Bridge / 4 EXE** 与最终源码一致；旧 96 模块快照另存 `final-runtime-delivery-development-before-async.json`。
- `frozen-async-host-lifetime-final/result.json`：Internal/MCP 实际长期子进程在 launcher 退出后依然具有 Python/Unicode/IDNA 资源，双方正常 ExitCode 0。
- Host 全量：`replaylab/output/host-async-final-20260906.xml`，**1868 testcase / 2579 JUnit 含子测试，0 failures/errors，13 skipped，140.899 秒**。
- 随后只改 panel 关闭顺序，专项 `panel-close-final-20260906.xml`：**67 testcase / 110 含子测试，0 failures/errors/skips**。没有把这次微调后的全量冒称重跑过。
- 异步/解析/取消专项 `async-freeze-20260906.xml`：122 testcase / 148 含子测试，无失败；覆盖线程边界、取消、换捕获、迟到源覆盖保护、严格解析结果等。
- Agent 全量 `replaylab/output/agent-final-cwd-20260906.xml`：571 个用例、4 skipped、0 failures/errors。首次错误 cwd 导致 tests 包导入失败的日志保留，随后在 `replaylab/` 正确 cwd 重跑通过。
- 原生 v17：1650 断言/75 用例；qdoc 89/4；4884 翻译全部完成。平台构建范围与 GPU 缺口以独立性能暂停文件为准。

## 8. 用户让新 AI 继续时的顺序

1. 先确认接手范围是 Shader。EID 性能继续保持暂停，除非用户另外明确恢复。
2. 不要立即重新反编译或改写已有正确 HLSL。先在当前最终包重跑 **6.rdc 三次真实 Ctrl+S**：改 alpha→编译并替换；加入受控 `#error`→显示错误且保留上版；恢复 `593c.../740...`→重新替换。全过程保护项目接受资产、审核次数与 head。
3. 再正常打开 **1.rdc / EID 61523**，验证项目源码自动复用、当前 ABI/替换、可编辑唯一文档与图。重新发现当前 resourceId/workspaceId/session，不能复用 180122 已关闭的 target。
4. 完成后更新本文件/HANDOFF/REBUILD_PLAN 的当前状态，保留所有历史失败，不覆盖旧失败收据。如果功能已通过就结束本次修复，不为追求“全部 AI 等价”扩展无限测试。
5. 完整厂商 Shader/退回矩阵、更多 API/RDC，以及 Release 同步属于后续明确范围；按实际证据逐项补，不自动把性能线重新启动。

现成脚本均在 `output/shader-workspace-20260906/`：

- `start_real_save_ui.ps1`：默认 6.rdc/11329；1.rdc 传 `-CapturePath 'D:\Study\抓帧\白银之城\1.rdc' -EventId 61523`。脚本保持运行并管理正常退出，不要强杀 GUI。
- `discover_real_save_target.py <新运行目录>`：等正常自动恢复，公开只读发现并写 fresh target。
- `verify_real_ui_save.py --ui-receipt <run>/ui.json --ui-command <run>/command.json --workspace-id <fresh-id> --target <run>/target.json --host <绝对Development/ReplayLabMCP.exe> --output <全新验证目录>`：务必显式传 Development Host，默认值仍为 Release，不能漏传。
- `verify_cross_rdc_ui.py <新运行目录>`：只适用于 1.rdc 跨捕获验证。
- 完成或取消后向 `<run>/close.request` 写入说明，等 launcher 的 `close.json` 确认 ExitCode 0、fixture timer 已销毁。

Python 用仓库 `replaylab/.venv/Scripts/python.exe -X utf8`。Host tests 在 `replaylab/mcp_host/` 运行；Agent tests 在 `replaylab/` 运行；每轮新建短 basetemp。原生构建只允许一个 `/m:2`，显式 SolutionDir。用户项目里的 managed HLSL/manifest/授权/gate 只能经公开 Host 或原生 Ctrl+S 变更。

预计：最终两组实机复验顺利约 10–20 分钟；一旦出现新故障需先根据新收据定位，不能保证时长。完整厂商矩阵、更多 API 或完整语言服务器不属于这个估时。

## 0b. 实时语义错误检测（编译级 live diagnostics，2026-09-06 新增）

用户反馈：删除一整块代码（括号仍配对、语法仍合法）时编辑器没有任何提示。定位结论：C++ 词法解析器（`localDiagnosticsFor`）本来就在脏缓冲上实时跑，能报缺引号/括号不配对/注释未闭合；缺的是**语义错误**，只有真正的编译器能发现。而编译器此前只在 Ctrl+S 保存后由 watcher 跑（watcher 只编译不应用）。因此新增「对未保存缓冲的防抖后台编译检查」。

实现（无需 C++ 重编，走 Bridge 部署 + Host 冻结）：
- `shader_workspace/workspace.py` 新增 `compile_check(workspace_path, text)`：完全复用 `compile()` 的编译算子/模板值/`run_operation`，但输入是缓冲文本、在一次性 job 目录里编译，**不发布 revision、不改 manifest、不写历史、不产生候选、不应用**，只返回 `{ok, diagnostics}`。route-plan（部分 GL/Vulkan 多步链）返回 `ok=None`，调用方回退到保存后诊断，避免半程误报。任何内部异常也返回 `ok=None`，绝不伪造报错。
- `shader_workspace/diagnostics.py` 新增共享 `parse_compiler_diagnostics(text, input_name)`（放低层避免与 watcher 循环依赖），同时解析 DXC(clang 式 `file:line:col:`) 与 FXC(MSVC 式 `file(line,col):`)，note→hint，clean→[]。已在 Linux 上单测通过。
- `shader_workspace/watcher.py`：`ShaderWorkspaceWatchManager.compile_check` 委托（新建 service，无需 watch job）。
- `shader_live_edit.py`：`ShaderLiveEditManager.check_authorized_source`，与 save 相同的授权校验，但**在管理器锁外**跑慢编译，避免阻塞 stop/save/prune。
- `internal_tool_service.py`：`parse_shader_editor_check_request`（比 save 少 `expectedRawSha256`）、`shader_editor_check_response`、`check_shader_source`；`internal_host.py`：新增 `POST /replaylab/shader-source/check`（不新增 health capability，客户端 health 校验不变）。
- Bridge `shader_editor_client.py`：`check(...)` 方法 + `CHECK_PATH` 独立 transport；`project_shader_workspace.py`：脏缓冲编辑防抖（`LIVE_CHECK_DEBOUNCE_MS=600`，单击停顿后才触发）→ 后台 check worker → 结果按 `textRevision` 守卫（缓冲已变则丢弃）→ 喂给**既有** `SetDiagnostics`（红波浪线 + 悬停提示）与问题面板。脏缓冲不再一律清空诊断，而是显示与当前编辑版本匹配的 live-check 结果；无新结果时不显示旧位置的陈旧波浪线。
- 优先级：check 与 save 共用进程级 WinHTTP 单请求槽（`_ACTIVE_REQUEST_THREAD`），互相礼让；编译多为亚秒级，Ctrl+S 至多短暂排在一次 check 之后。check 出错/超时只丢弃该次（下次停顿重试），不影响保存。

部署：`output/shader-workspace-20260906/REBUILD_HOST_AND_BRIDGE.cmd`（bridge 部署会拷 `project_shader_workspace.py`/`shader_editor_client.py`，Host 冻结覆盖全部 Host 改动）。**尚未实机验证**——需在真实 dxc + 打开的 workspace 上确认删除一块代码后出现红波浪线；单文件解包 shader（如 593c）无 #include，include 解析非问题。

F12 单引用直接跳转仍是唯一需要 qrenderdoc(C++) 重编的项（`CodeEditorService.cpp::showReferences`，`hits.size()==1 -> navigateToDefinition` 已在位）。

## 0c. 第一次部署复盘（2026-09-06 晚）

用户跑 `REBUILD_HOST_AND_BRIDGE.cmd` 后反馈：控制台窗口仍在、流程图单击"和之前一样"。截图定位：
- **Host 冻结失败**（`build-runtime exit=1`）：`对路径 "ReplayLabInternal.exe" 的访问被拒绝` → `Test-FrozenHostLifetime.ps1:40 Frozen ensure failed`。根因是 qrenderdoc 关闭后仍有**残留 Host 进程**占着 exe。原脚本只检查 qrenderdoc，且**构建失败后第 3 步照样把旧包部署了一遍**，所以 Host 侧改动（控制台窗口）全部未生效。已修：脚本第 0 步 `taskkill` 四个 Host 进程并二次确认；任一步失败立即停止，绝不部署旧包。
- **Bridge 部署其实成功**（部署文件与源码哈希一致、live-check 代码在位）。流程图"没变"是因为 250→180ms 的差别感知不到。用户要的是"系统双击图标那种即时"。复查发现双击展开（`_flow_graph_subgraph_requested`）**只换流程图场景、从不 `GoToLine`**，与单击的编辑器跳转作用在不同控件、并不冲突；先前"双击闪跳"的实质是单击跳到了错误的声明位置，如今已跳到正确的调用位置，双击时编辑器停在调用处 + 图展开内部反而是连贯的。因此去掉了单击的等待：`_activate_node` 改为 `self._click_timer.start(0)`（下一事件循环即发出，感知即时，且不在鼠标处理器内同步重入）。`_expand_node` 保留 stop 作为兜底。

若下次 Host 冻结仍报错，先看是否需要清理被保留的 `replaylab/output/.pyinstaller-work`（上次构建因无法证明安全清理而保留了工作目录）。

## 0d. 更正：控制台窗口"修复"是误判，且它导致 Host 冻结失败（2026-09-06 晚，第二次部署）

第二次部署（脚本已先 taskkill 残留进程，且**确认无残留**）Host 冻结仍以完全相同的 `Frozen ensure failed` 失败，而 `ReplayLabInternal.exe --help` 冒烟测试通过、所有改动模块在 Linux 上 `import` 干净。时间线：上午 09:5x 最后一次成功冻结用的是原始 `_spawn_detached` 标志；今晚两次失败都含本 AI 改的标志。`ensure` 的逻辑是 `_spawn_detached` 拉起 `serve` 后轮询 `health()`，超时即非 0 退出——即 serve 子进程在新标志下起不来。

根因是本 AI 早先对 Windows 标志的理解有误：微软文档明确 *CREATE_NO_WINDOW is ignored if used with DETACHED_PROCESS*，所以原始的 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` 实际效果就是 `DETACHED_PROCESS`（子进程**无控制台**，本来就不会弹窗）。去掉 `DETACHED_PROCESS` 后子进程改为附着隐藏控制台，进程生命周期语义改变，导致 serve 不健康。**已用 git diff 精确还原 `internal_host.py`/`replaylab_host.py` 两处 `_spawn_detached` 为原始三标志**（未触碰同文件里之前 AI 的其他未提交 hunk）。

关于"启动时两个常驻命令行窗口"的真实来源：逐一排查后，Bridge 拉起两个 `ensure` 用 `_hidden_process_flags()`（隐藏）；serve 为 DETACHED（无控制台）；`replaylab-ai.exe` 隐藏；C++ 侧不拉起 ReplayLab exe；Host 内 8 处未加隐藏标志的 subprocess 调用（`tools.py:370` 为 taskkill 回退、编译器真正入口 `tools.py:786` 已隐藏）均为短命工具/探针进程，无一在启动时长驻。**代码内未能定位来源，需要用户提供两个窗口的标题栏（含 exe 路径/命令行）再定位，不要再凭猜测改动。**

## 0e. 两个控制台窗口的真实机制 + ensure 失败与之无关（2026-09-06 晚，第三次构建后）

用户提供了窗口标题：`…\x64\Development\replaylab\ReplayLabMCP.exe` 与 `…\ReplayLabInternal.exe`——即两个 **serve 长驻进程本身**。同时，把 `_spawn_detached` 标志还原为原始三标志后，Host 冻结仍以完全相同的 `Frozen ensure failed` 失败。两条事实合起来：

- **窗口机制**：`_spawn_detached` 用 `DETACHED_PROCESS` 启动的是 PyInstaller **onefile 引导器**；引导器本身无控制台，但它会再拉起真正运行 Python 的**子进程**，该子进程是控制台程序、父进程又没有控制台可继承，Windows 便为其**新分配一个可见控制台**。所以 0d 里"原始标志不会弹窗"的推断对引导器成立、对其子进程不成立。本 AI 最初"去掉 DETACHED_PROCESS、让引导器持有隐藏控制台、子进程继承之"的思路**在机制上是对的**，它没生效只是因为 Host 从未成功重新打包。**待 ensure 问题解决后应重新应用该修复**（当前为隔离变量，仍保持原始标志）。
- **ensure 失败与标志无关**：还原后照样失败，且 Internal serve 启动路径中本 AI 的全部改动仅为新增定义（方法/路由注册/函数），启动时不执行，模块导入干净；SKILL.md 只打进 agent 且其自检在探针之后。故原因不在本 AI 的 Host 代码，需拿到 serve 的真实 stderr。探针日志每次被 `Remove-ControlledBuildSession` 的递归删除先行清掉（删到被僵尸 serve 占住的 exe 才失败），因此改为让用户在 PowerShell **前台直接运行** `session-e21f…\internal\dist\ReplayLabInternal.exe serve --runtime-dir … --port 47331`（隔离 LOCALAPPDATA / REPLAYLAB_INSTANCE_ID / 注册表环境变量）以直接观察报错或卡点。每次失败的探针会遗留一个不健康的 serve 占着 exe，`REBUILD_HOST_AND_BRIDGE.cmd` 第 0 步会在下次构建前清掉。

## 0f. 根因定位：`Frozen ensure failed` 是探针脚本自身的 ExitCode 竞态，Host 完全健康（2026-09-06 深夜）

用户手动运行 `Test-FrozenHostLifetime.ps1`（Bypass）并保留探针目录后，`result.json` 给出决定性证据：
`launcherExited=true, launcherExitCode=null, childExited=true, childExitCode=0`；`ensure.json` 是 ensure **成功**输出（`started:true`），`ensure-stderr.log` 为空。即 ensure 成功、serve 健康并按要求以 0 退出，但探针读到的 `$launcher.ExitCode` 为 `$null`，PowerShell 中 `$null -ne 0` 为真，于是误抛 "Frozen ensure failed."。

根因是 PowerShell 5.1 已知坑：`Start-Process -PassThru` 得到的进程若退出很快且未提前缓存句柄，`.ExitCode` 读回 `$null`。探针对 `$child` 已正确做了 `$null = $child.Handle`，唯独漏了 `$launcher`。这是探针的潜在竞态，今早未触发、今晚 ensure 退出更快后每次触发；**与本 AI 的 Host 改动无关**（手动 ensure 三次全部成功，`/health` 正常）。

修复：`packaging/tests/Test-FrozenHostLifetime.ps1` 在 `Start-Process` 之后立即 `$null = $launcher.Handle`，并在 `WaitForExit` 后显式拒绝 `$null` ExitCode（给出明确错误而非误导性的 "ensure failed"）。

同时**重新应用**了控制台窗口修复（0e 已确认机制正确）：`internal_host.py`/`replaylab_host.py` 的 `_spawn_detached` 改为 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`（去掉 `DETACHED_PROCESS`），注释已改为准确描述 onefile 引导器/子进程的控制台继承机制。此修复由修好的探针在下次构建中验证。

验证顺序：先用修好的探针手动跑一遍当前已打包（原始标志）的 exe 确认探针通过 → 再跑 `REBUILD_HOST_AND_BRIDGE.cmd` 完整构建（含控制台修复 + 实时编译检查 + cbuffer 命名技能）。

## 0g. 第四次构建成功后的两项修复（2026-09-06 深夜）

构建成功、控制台窗口消失、单击即时。用户反馈两项：删代码无红波浪线；双击节点触发了单击的代码定位。

- **实时诊断无效的原因**：用户 shader 是 SPIRV-Cross 反编译 + 接口修复产物，其工作区走 **route-plan 多跳编译**（routePlan 在工作区创建时设定，是常态而非特例），而 `compile_check` 之前对 route-plan 直接返回 `ok=None` 回退——等于主力场景根本没启用。修复：`compile_check` 重写为双 lane：直接 lane 用 `run_operation`；route-plan lane 直接复用 `_run_route_chain(…, "compile", …)`（它只跑编译跳、不写 manifest、不发布，发布是 `_compile_route_chain` 在其返回后做的），传一次性 `history` 并跳过验证器/发布。失败时沿异常链解析 `see <job_dir>` 提示读取 stderr/stdout（与 watcher 同一线索），按"文件位于 root/jobs 内"判定缓冲本地诊断，其余折叠为 line-0 非导航错误；`ToolConfigurationError`/`ManifestValidationError`/无法启动/超时一律返回 `ok=None`（环境问题不伪造报错）。结束时按"检查前后 jobs 目录差集"清理本次新建的 job 目录（持有工作区锁，无并发干扰）。同时 Host 响应与客户端新增 `reason` 透传，Bridge 对"检查出错"与"不可检查原因"各打印一次 `[ReplayLab] Shader Workspace live check …`，便于下次定位。
- **顺手修掉一个真正致命的疏漏**：`workspace.py` 新增的模块级 `re.compile` 依赖 `re`，但该文件原本未导入 `re`（py_compile 查不出，冻结后 Host 导入即 NameError、ensure 必失败）。已补 `import re`。同理 Bridge `project_shader_workspace.py` 使用 `time.monotonic()` 补 `import time`。对全部改动文件做了"使用但未导入"静态检查 + 全模块真实 import，其余 4 处报告均为 docstring/注释/局部变量误报。
- **双击触发单击定位**：单击改为 0ms 即时后，第一击的跳转必然先于第二击发生，且双击展开只换图不动编辑器，故不引入延迟的解法是**双击时恢复单击前的光标**：`_flow_graph_node_activated` 在 `GoToLine` 前用 `GetCursorLine/GetCursorColumn` 记录 `preClickCursor`（含单调时间戳）；`_flow_graph_subgraph_requested` 在 `open_subgraph` 后若该记录在系统双击间隔 + 0.75s 内，则 `GoToLine` 回原位并清除。净效果：单击即时跳转、双击只展开图、编辑器位置不变。纯 Bridge 实现，无需重编 C++。

部署：再跑一次 `REBUILD_HOST_AND_BRIDGE.cmd`（Bridge：双击恢复 + 日志；Host 冻结：route-plan 检查 + reason）。验证：删一块导致语义错误的代码、停顿约 0.6s 应出现红波浪线；若仍无，看 qrenderdoc Python 控制台的 `[ReplayLab] Shader Workspace live check …` 行。

## 0h. 新功能：左侧面板显示使用该 Shader 的 EID + 右键跳转（item 11，2026-09-07 凌晨）

用户选定下一项为 item 11。实现（纯 Bridge，`project_shader_workspace.py`）：
- **数据源**：不用 RenderDoc `GetUsage`（shader 资源无 usage 条目）也不逐 draw 回放（太慢），而是读每个 workspace 的 `<workspacePath>/manifest.json` 取 `capture.catalogPath` 与 `shader.resourceId`，再读 capture catalog（`renderdoc-ultra-capture-symbols/v1`）的 `uses[]`（每条 = `eventId` + ShaderKey 字段），按 `resourceId`（capture 内唯一）分组去重排序。放在**已有的文件工作线程**（`_build_workspace_snapshot_files`）里完成，结果以 `eventIds` 随快照返回；catalog 按 `(path, mtime_ns, size)` 缓存（≤32 条），manifest/catalog 读取有大小上限，任何异常只导致该 shader 无 EID 列表，绝不影响快照。
- **显示**：叶子标签追加 `[EID a, b, c, d +n]`（前 4 个），tooltip 列出前 64 个与总数；EID 集合并入树签名，变化时自动重绘。
- **右键菜单**：`Open Shader`；`Used by N event(s)`；每个 EID 一项 `Jump to EID N`（最多 48 项，超出提示看 tooltip）。跳转 = `ctx.SetEventID([], eid, eid)`（Bridge 已有先例）。仅当 workspace 的 `capturePath` 与当前 capture binding 的 `capturePath`（均 `_normal_path`）一致时可用，否则置灰并提示先打开对应 capture——EID 是 capture 局部的，跨 capture 跳转无意义。
- 新增翻译串（`Used by %1 event(s): %2` / `Right-click to jump to an event` / `Open Shader` / `Jump to EID %1` / `… and %1 more (see tooltip)` / `Open the capture this Shader belongs to first` / `No event uses this Shader in the capture catalog`）当前为英文回退，待下次 QM 重建补中文。
- 单测：合成 manifest+catalog 验证分组/去重/坏数据过滤/备用键名/缓存命中与失效，通过。
- 限制：EID 列表来自 catalog 的覆盖范围——单事件 catalog 只知道那一个事件，完整列表需全帧 catalog（`prepare_active_capture_project`）。

## 0i. 第五次部署后的三项反馈处理（2026-09-07 凌晨）

用户反馈：删代码仍无波浪线但问题面板出现 "The Shader does not compile (error outside the edited source)"；双击节点触发单击定位；EID 只显示一个。处理：

1. **实时诊断位置映射 bug（Host `workspace.py` `compile_check`）**：面板那条提示证明检查已在跑、编译器也报了错，只是位置未被识别为缓冲本地。原因：route 跳的输入名为 `input.hlsl`，但编译器报的是相对/混合斜杠路径，`Path(...).resolve()` 按 Host cwd 解析落在 jobs 外，而完整字符串又不等于 `"input.hlsl"`。改为按**文件名**判定（`input.<文本扩展名>`/`shader.hlsl`），并仅当扩展名为文本源（.hlsl/.glsl/.slang/.frag/.vert/.comp…）才视为缓冲本地——二进制中间产物（input.spv）的行号对 HLSL 缓冲无意义，仍折叠为 line-0。
2. **本地即时语义分析（Bridge，新增，无需编译）**：用户指出 IDE 的"瞬间"提示来自本地语法/引用分析。新增 `hlsl_local_diagnostics(text)`：去注释/字符串/预处理行（保留偏移，收集 `#define` 名）→ 标识符 token（正则带负向后顾，排除数字后缀 `0.5f/1e5f/0x1F` 与 `.member` 访问）→ 第一遍收集用户类型（struct/class/interface/cbuffer/typedef）→ 第二遍收集声明（`<类型>[修饰符] NAME`、`Texture2D<float4> NAME`、同深度逗号续声明、宏）→ 第三遍报告"使用但全文件从未声明"的标识符，跳过 keyword/intrinsic/类型/semantic（`:` 后）/`register()`/`packoffset()`/语句级 `[attribute]`/`::`。**刻意 scope-insensitive**（任意处声明即算声明），以换取几乎零误报；编译检查兜底其余。单测：SPIRV-Cross 风格样例零误报；删声明/拼错名/删函数均定位到行列；4680 行 40ms。集成：独立分析线程（latest-wins）+ 150ms 防抖（`LOCAL_ANALYSIS_DEBOUNCE_MS`），结果按 `documentToken`+`textRevision` 守卫，`_current_problems` 在脏缓冲时先列本地分析、再合并同版本编译诊断（按行+文本去重），带 `length` 使波浪线覆盖整个标识符。
3. **流程图交互重做（Bridge `project_shader_flow_graph.py`）**：撤销 0h 的"双击复位光标"临时方案（删除 `_remember_pre_click_cursor`/`_restore_pre_click_cursor_if_double_click`）。取消节点主体单击跳转：主体单击只选中；节点**内部右缘**新增 → 图标（`_NODE_ARROW_STRIP=26`，标题/描述宽度相应收窄），悬停高亮 + 手型光标，**点击箭头才跳转**；双击主体展开图，双击落在箭头上视为再次跳转而非展开；回车键仍可跳转。跳转仍经 `_flow_graph_node_activated` 的行锚点（Scintilla marker）并校验锚点仍在所属函数内且仍为该调用，满足"书签式绑定、不受编辑偏移影响"。
4. **EID 全帧列表（Bridge）**：RenderDoc 有 `ResourceUsage::Shader`，对当前打开的 capture 用 `ctx.GetResources()` 把 int 映射回 `ResourceId`，`Replay().AsyncInvoke('replaylab-shader-usage', cb)` 在回放线程调 `ctrl.GetUsage(rid)`（帧记录查表，便宜，且替换 shader 不影响——usage 按原始 id 记录），结果经队列在 30ms 定时器落回 UI，按 `(capturePath, resourceId)` 缓存直到 capture 切换；live 结果覆盖 catalog 列表（catalog 可能只是单事件目录）并重绘树、保留选中项。回答用户：**是，整帧所有绑定该 shader 的事件都会列出，含替换后**。

部署：再跑一次 `REBUILD_HOST_AND_BRIDGE.cmd`（Host：位置映射修复；Bridge：本地分析 + 箭头 + 全帧 EID）。

可选下一步（用户提到"开源实现"）：C++ `CodeEditorService` 已内置 LSP 客户端（`ShaderEditor_LanguageServer` 配置项，didOpen/didChange/publishDiagnostics/definition），接入开源 `shader-language-server`（antaalt，MIT，Windows 二进制）可获得进程内 DXC 诊断 + 悬停类型 + 补全 + 转到定义，即 item 12 的大部分。需用户下载二进制并配置路径，未做。

## 0j. 悬浮为空/行尾无波浪线（C++）+ item 10 流程图 main→阶段→输出（2026-09-07）

用户截图证实两层诊断均已工作（本地 `Undefined identifier` + 编译器四条、行号正确、有波浪线）。两个残留问题与修法：
- **悬浮提示空白**：`showDiagnosticAt` 确实查了 local+lsp+external 三个列表并 `callTipShow`，问题是**从未设置 calltip 颜色**——深色主题下默认白底 + 继承的浅色前景，文字不可见。`CodeEditorService.cpp::configureIndicators` 末尾按 `dark` 设置 `callTipUseStyle(0)`/`callTipSetBack`/`callTipSetFore`/`callTipSetForeHlt`。
- **缺分号无波浪线**：`ShaderCodeEditor::SetDiagnostics` 把超出行内容的列 `qBound` 到 `lineEnd`（换行符位置），指示器画在无字形处不可见。改为 `start >= lineEnd && lineEnd > lineStart` 时退到 `lineEnd - 1`（最后一个可见字符），与主流 IDE 一致。
- 以上两项 + 之前的 F12 单引用直接跳转需重编 qrenderdoc：新增 `output/shader-workspace-20260906/REBUILD_QRENDERDOC.cmd`（只编 `qrenderdoc_local.vcxproj` Development x64 `/m:2 SkipReplayLabRuntime=true`，不编核心/pyd/cmd，跳过 i18n 门禁——无新增 C++ tr()；随后重部署 Bridge）。

**item 10（用户选 B）**：流程图 main → 阶段 → 输出，且内部节点标明输入/输出，用背景色区分。全部 Bridge：
- `project_shader_flow_hierarchy.py` 新增数据流推导：`file_scope()`（文件级全局/资源/cbuffer 成员，跟踪大括号+小括号深度、按语句起始词识别 `cbuffer … {`）、`struct_fields()`、`function_params()`、`span_io(start,end,params)`（token 区间内 `base.field` 读/写、swizzle 折叠到 base、资源单列、局部写单列；赋值判定覆盖单 token `= += -= *= /=` 与双 token `%= &= |= ^= <<= >>=` 及 `++/--`）、`function_io()`（函数级：参数+函数体，`out` 参数保底计入写；不含局部）、`entry_interface()`（结构体参数/返回值展开为字段与 semantic，如 `gl_FragCoord : SV_Position` / `SV_Target : SV_Target0`）。
- 节点：`_FunctionProjection._node` 为每个节点附 `io`（function-call 节点用被调函数的 `function_io`，语句节点用自身区间 `span_io`）；`build()` 新增 `_boundary_nodes()`：`Input · <fn>`（入口接口，无参数则用读取的全局/资源）与 `Output`（输出接口，void 则用写出的全局/字段），Input 锚在声明、Output 锚在右大括号，边：Input→无入边节点、无出边/return 节点→Output；`project_root()` 对 AI 接受图为阶段节点附 `function_io` 并 `_wrap_accepted_graph()` 同样加首尾节点（幂等）。
- 视图 `project_shader_flow_graph.py`：规范化放行 `io`（`_normalized_io` 有界），Input/Output 标签在视图侧本地化；节点内描述下方渲染 `in: … / res: … / out: …`（各最多 5 项 +n，语句节点的 out 含其定义的局部；边界节点不重复渲染）；`_NODE_TINTS` 按 kind 混入调色板 Base 色（input 蓝 / output 琥珀 / function-call 绿 / condition·loop 紫，强度 0.16，深浅主题均可读），左侧色条同色作图例。
- 单测：SPIRV-Cross 风格样例的 file_scope/params/struct_fields/function_io/entry_interface 全部符合预期；函数图首尾为 input/output 且边完整；AI 接受图包裹正确；原有 4 个 call-site 测试通过。

## 0k. 行尾波浪线加宽 + 节点内容重构 + item 3：AI 全自动跑通 ABI（2026-09-07）

**行尾波浪线**：用户"删 `;` 仍无波浪线，或者多夹几个字符"。C++ `ShaderCodeEditor::SetDiagnostics` 与 Bridge `_clamp_diagnostics_to_text` 都改为：诊断列落在行尾/超出时，回退覆盖**最后一个 token**（≤40 字符的字母数字/`_`/`()[].`），而不是只画 1 个字符。

**流程图节点内容**（Bridge `project_shader_flow_hierarchy.py` / `project_shader_flow_graph.py`）：去掉内部节点下方 in/out 小字（只保留 Input/Output 边界节点的接口说明）；节点标题 = 紧邻上方的 `//` 注释（无则一行代码摘录），描述 = 去缩进的 ≤3 行代码 + `...`；function-call 节点 = 调用点/被调函数上方注释 + `Call(args)` 一行；`leading_comment()` 新增。

**item 3（Cursor 反编译 EID 11402 卡在 ABI）根因与修复**：
- 现象链：反编译 → Host 固定修复 `repair_spirv_cross_hlsl_interface` 抛 "unsupported SPIRV-Cross input declaration tail" → 基线编译过但回放 ABI 不符 → `begin_shader_workspace_organization` 建了 session 目录后在基线门禁失败（只写 `failure.json`，无 `session.json`）→ 后续用该 id 报 "session.json 不是正规文件" → 诊断给出 `repair-source-within-captured-abi` 但**没有任何工具能让 AI 提交修好的源码**（`promote_shader_workspace_explanation` 只对无 source 的 workspace 有效，`write_shader_workspace_organization_file` 需要先 begin）→ AI 只能问用户。
- 固定修复加宽：`adapters/hlsl_interface_abi_repair.py::_signature` 的字段正则原只认 `(float|uint|int)[1-4]?`，SPIRV-Cross 对 PS 会输出 `bool gl_FrontFacing : SV_IsFrontFace;`（以及 half/min16float/precise 等），整个 struct 尾巴因此不匹配。现在：类型表 `_INPUT_TYPE_COMP_TYPES`（bool→uint/int，half/min16float/float16_t→half/float，double，min16int/uint，dword…）、限定词加 `precise`、struct 体内注释先抹空、错误信息带上未识别的那段文本。单测 +2（bool frontface/half/precise/注释；float4x4 报错文本）。
- **新 Host 工具 `repair_shader_workspace_source_abi(workspace_path, expected_source_sha256, source, acknowledge_semantic_equivalence_unverified)`**（`organization.py::ShaderOrganizationManager.repair_baseline_source`，server.py 注册）：AI 提交完整修复后的 HLSL。流程：校验文本/`#include|#line`/大小 → 取 `WorkspaceSourceTransactionLease`（有 watcher/live-edit/review 时拒绝）→ 源哈希 CAS 校验 → 首次前保存 `ai/decompiler-original.hlsl`（与 organization begin 同一规则，保证后续 begin 的溯源链）→ 先 `compile_check`（编不过直接返回行列诊断，**不碰源文件**）→ `_source_compare_and_swap` 换入 → 复用 `_compile_and_validate`（编译 + 回放反射 ABI）→ 通过则保留并返回 `nextTool=begin_shader_workspace_organization`；否则**换回原字节**并返回 `failure`/`evidence.abiComparison`/`baseline.compiledInterface`。每次尝试写 `ai/abi-repair-attempts/<id>.hlsl|.json` 回执。单测 4 项（拒绝并还原、编译检查拦截、接受后 begin 成功、旧 session 提示）。
- `diagnostics.py`：凡 `allowedActions` 含 `repair-source-within-captured-abi` 且 AI 可写源，附 `sourceRepairTool` 与 `autonomousNext`。
- `organization.py::_read_session_file`：session 目录只有 `failure.json` 时给出可执行的提示（"never opened: begin failed at baseline-compile-abi；先 repair 再 begin 取新 id"），替代 "不是正规文件"。
- **全 AI 路线统一行为**：`replaylab-core` SKILL v3 新增 §6 自主性（一轮内跑完 prepare→decompile→compile/ABI→repair→organize→review→register→apply；失败门禁是工作不是提问；只在缺 grant / 预算耗尽 / 请求真的留了不可逆选择时停）与 §7 用户可读回答（1–3 段对话式中文/用户语言：结果、做了什么及为何修复、失败/省略原因、至多一个下一步；不重复、不用标题/列表/表格/代码块/emoji/工具名/哈希/路径，除非用户要）。`renderdoc-shader-workbench` SKILL v14：步骤 5 的 ABI 失败改为"当轮自修最多 3 次"并给出典型修法（按捕获行序重声明 `SPIRV_Cross_Input`、`bool : SV_IsFrontFace` 反射为 uint、补齐未用输入、cbuffer 44→48 用尾部 pad 保持成员偏移）；步骤 6 说明 `organization-abi-failed` 回到修复通道；§3 禁止以"如何处理？"结束；§6 报告分为 Host 表单与用户摘要。`src/replaylab_agent/agent_prompts.py` 三个系统提示（vendor-native/Cursor/Codex/Claude ACP、API main、API child）同步：ask 规则限定为"请求留下的真实选择"，明确"是否继续/如何处理失败门禁"不是用户问题；Answer 段改为对话式摘要规则。六个角色的提示装配自检通过（core 只出现一次且含新工具名）。

未完成/待用户：仍未拿到 `D:\Study\抓帧\ReplayLab Project\workspaces\shaders\v2\MainPS-dxil-spirv-hlsl-abi-02` 的实际源码（文件夹授权对话框超时），bool/SV_IsFrontFace 是基于代码结构的高置信推断；即使固定修复仍不覆盖，新工具已让 AI 能自己修。部署：`REBUILD_HOST_AND_BRIDGE.cmd`（Host + agent + Bridge）与 `REBUILD_QRENDERDOC.cmd`（C++）。

## 0l. ABI 失败"哪里、为什么、怎么改"+ 固定修复分阶段（2026-09-07）

用户两点：反编译时就该拿捕获的 cbuffer 大小/偏移去对声明；Host 校验失败时要说清哪里失败、为什么、差异是什么，再交给 AI 修。

- **固定修复分阶段**（`workspace.py::_repair_spirv_cross_hlsl_from_dxil_evidence`）：原来 cbuffer 修复和接口修复在同一个 try 里，接口修复认不出一个字段就整体放弃，连已证明的 cbuffer 修复也丢掉（这正是 44→48 没被修的原因）。现在接口修复独立 try：成功 → `interfaceRepairVersion=1` + `interfaceRepair={"status":"applied"}`；失败 → 保留 cbuffer 修复，`interfaceRepair={"status":"not-applied","reason":"unsupported … tail: 'bool front : SV_IsFrontFace;'"}`。溯源校验 `_validated_hlsl_cbuffer_repair_provenance` 放行并校验这个可选字段（applied 必须配 version 1；not-applied 必须无 version 且 reason ≤4096）。状态里 `source.hlslCBufferAbiRepair.interfaceRepair` 直接告诉 AI 哪一条声明没修。
- **ABI 差异解释器**（新模块 `shader_workspace/abi_explain.py::explain_abi_differences(baseline, candidate)`）：原 `compare_shader_abi` 只给按数组下标的 path diff（少一行签名后面全部错位，AI 很难读）。解释器按稳定身份重新配对——签名按 `semanticIdxName`（+stream），cbuffer/资源按 `space:bind`，成员按 `byteOffset`——输出 findings：`missing-row / extra-row / moved-row / type-mismatch / system-value-mismatch / missing-block / size-mismatch / member-missing / member-type-mismatch / member-extra / missing-binding / binding-type-mismatch / extra-binding / changed`，每条含 `identity / captured / candidate / message / hint`（hint 是具体 HLSL 改法：补 `uint <name> : SV_IsFrontFace;`、按 regIndex 顺序重排、尾成员改 float3 使大小停在 44、`packoffset(c2.x)`、`StructuredBuffer<float2>` 代替 `Buffer<uint4>` 等）。opaque/ambiguous 布局只报大小。只在读取时派生、不入库、不影响报告哈希。
- **接入点**：`status.lastAbiAttempt.explanation`（`_current_rejected_abi_attempt`）；`validate_shader_workspace_candidate` 不兼容时返回 `explanation` + `sourceRepairTool`；`repair_shader_workspace_source_abi` 失败结果带 `evidence.abiExplanation` 与扁平的 `whatToFix[{where,why,how}]`；organization begin 的 ABI 失败改为 `_abi_failure_error`：错误文本追加摘要与首条 finding，异常携带 `abi_explanation`，`_shader_err` 输出 `abiExplanation` + `whatToFix`。
- **技能**：`renderdoc-shader-workbench` 步骤 4 新增"首次编译前先对声明"：看 `hlslCBufferAbiRepair.interfaceRepair`，为 not-applied 或 DXIL 路线无记录时，直接把 `SPIRV_Cross_Input/Output`、cbuffer、资源声明与 `baseline.compiledInterface` 对齐后走修复通道，省掉编译→失败→修一轮；步骤 5 说明 `whatToFix` 的三个字段并要求逐条处理。
- 单测：`test_abi_explain.py` 3 项；`test_shader_abi` 31 项、organization 58 项、repair 4 项通过；routes 全套在跑。

## 0m. 解包资产进入固定修复（Host 侧）+ 智能匹配设计（2026-09-07）

用户决定把"捕获反射 → 解包资产 → 反编译代码"的顺序做进 Host，并指出：资产匹配不能只靠哈希强校验（不同引擎格式不同、常常对不上），应当是检索器 + AI 的智能匹配。参考资产：`E:\SilverPalace_Unpacked`（与现有 rdc 同一游戏，UE5，已授权读取）。

**已做（Host）：**
- `shader_workspace/asset_layout.py`：`renderdoc-ultra-asset-cbuffer-layout-hint/v1`（按 `"space:bind"` 给出资产声明的成员布局 + relation 溯源），`normalize_layout_hint`、`hint_from_asset_blocks`、`hint_from_relation_view`（候选优先级：程序同一性 > layoutHash 相等且元数据可信 > AI/规则已选中的匹配）。
- `adapters/hlsl_cbuffer_abi_repair.py`：`repair_spirv_cross_hlsl_cbuffers(..., asset_layout=None)`。命名抽象为 `_LaneNaming`（原行为）与 `_AssetNaming`：块字节数必须等于捕获值、每个 DXIL 访问 lane 必须落在资产成员内且标量类别一致（f32↔float/half，i32↔int/uint/bool）、成员名不得与 HLSL 已有标识符冲突、矩阵/结构体成员不支持——任一不满足即该块回退 lane 命名并在 `RepairedCBuffer.asset_fallback_reason` 说明原因（如"asset block byte size differs from the captured block"= 资产版本不对）。通过时声明就是资产的真实成员（含代码未读的成员），引用改写为 `BaseColor.xyz`/`asfloat(BlendMode)` 等。
- `workspace.py`：`ShaderWorkspaceService(asset_layout_provider=…)`；`_asset_layout_hint` 取 hint 后**固定副本**写入 `source-inputs/asset-cbuffer-layout-<sha>.json`；记录 `hlslCBufferAbiRepair.assetLayout{path,sha256,relation}`（仅在真的用到时）；`repairedBlocks[]` 新增 `namingSource/assetMemberCount/assetFallbackReason`（lane 且无原因时省略，兼容旧记录）；溯源重算 `_validated_hlsl_cbuffer_repair_provenance` 从固定副本读 hint 并要求重算结果确实使用了它。
- `server.py`：`_workspace_asset_layout_provider(manifest)` 只读活动项目的 `symbols-v3.sqlite3`（前置：资产根已配置可用、库中有该 capture shader、rawSha256 一致），经 `_shader_asset_relation_view` → `hint_from_relation_view`；任何缺失都返回 None，不阻塞反编译。
- 单测 `test_hlsl_cbuffer_asset_naming.py` 4 项（命名与未用成员保留、错版本/尺寸/类型/重名回退原因、无 hint 结果逐字节一致、relation view 选择）。

**现状诊断（为什么哈希在这个游戏上永远对不上）：** SilverPalace 是 UE5（Substrate 配置），shader 在 `Content/ShaderArchive-*.ushaderbytecode`，`r.Shaders.CompressionFormat=2`（Oodle）。对 Global-SM6 归档前 40MB 跑 Host 的 `_find_dxbc_containers` 结果为 **0 个有效容器**——扫描器索引不到任何 shader，symbol DB 为空，所有基于 raw/code/layout hash 的匹配都不可能触发。真正的"完整内容"分布在：(1) 归档（需 Oodle 解压，游戏自带 oo2core DLL）；(2) cooked 材质 `.uexp`——实测 `M_CharacterPBR.uexp` 含 `FMaterialShaderMapContent`、`FMaterialNumericParameterInfo`、`FMaterialUniformPreshaderField`（= Material cbuffer 的成员布局与参数名）、`FMaterialTextureParameterInfo`、shader 类型名（`FDepthOnlyPS/FOutlinePS/FLumenCardPS/FAnisotropyPS`…）、顶点工厂名；(3) 引擎固定 UB（View/Primitive/Scene）按 UE 版本的已知布局；(4) `analysis/inventory.db` 是 UEX 报告库 schema（assets/relations 含 material→texture 的 slot/parameter），当前为空。

**智能匹配设计（待做，需用户确认数据源）：**
1. 捕获侧特征：stage、pass 分类、入口名、cbuffer 大小向量、纹理/采样器数量、**绑定纹理的调试名**（UE 会 SetName 为资产名）、RT 名、输入布局（骨骼权重→Skeletal）、shader model。
2. 资产侧"材质文档"：材质名/父材质、domain/blend/shading model、纹理参数名 + 引用的纹理资产名、标量/向量参数名（有序）、shader 类型集合、顶点工厂、`bUsedWithSkeletalMesh` 等（来源：UEX inventory/material_json 和/或 Host 自带的 cooked 材质读取器）。
3. 检索打分（不需要哈希）：纹理名重叠（IDF 加权 Jaccard）、参数数量 ↔ Material cbuffer 大小（按 UE 打包规则预测）、pass ↔ shader 类型一致性、网格类型一致性、名字 token 相似度；输出 top-k 与逐信号证据。
4. AI 裁决：复用 `get_symbol_match_cases`/`submit_symbol_match`，决策落为该游戏 build 的规则，跨 shader/capture 复用；选中后写入 asset_shaders 元数据 → 上面的 hint provider 自动生效。
5. 引擎 UB 字典（View/Primitive…）按 UE 版本，首次由 AI 确认后成为规则。
哈希同一性保留为 tier 0（若某 provider 能解压归档则直接精确链接）。

## 0n. 智能资产匹配 v1：引擎感知目录 + 检索器 + 决策记忆（2026-09-07）

用户进一步要求：不只 UE，任何引擎的解包都要能正确读取；用 AI 找到对应资产文件；匹配成功后建立查找表避免重复搜索。选择"Host 自带读取器 + UEX 关系并用"。

**已做：**
- `symbols/engines/`：`detect_engine(relative_paths)`（按文件名判定 unreal/unity/godot/unknown，UE 时解析 `/Game`、`/Engine`、`/<Plugin>` 挂载 → 目录相对路径）；`parse_engine_asset(context, relative, data)` 分发。
- `symbols/engines/unreal_package.py`：只读 UE 4.2x–5.5 稳定的包头/名字表/导入表/导出表（不解 tagged property、不解冻结 shader map、不解 bulk）：主导出类名、导入的纹理/父材质/材质函数/参数集合/网格（对象路径经挂载映射为目录内 `.uasset` 相对路径 → 目录关系 `uses-texture`/`parent-material`/…，SilverPalace 实测 200/200 解析成功）、"有意义的名字"（参数名等，过滤属性类型/引擎样板）。`.uexp` 做字符串扫描：shader 类型（`FDepthOnlyPS`、`TBasePassPSFNoLightMapPolicy`…）、顶点工厂（`FGPUSkinVertexFactoryShaderParameters`）、宏、是否含内联 shader map。每文件约 10ms。
- `asset_catalog.py`：新 kind `package`（.uasset/.umap/.uexp）；扫描前先做一次只看文件名的引擎探测，引擎与挂载写入目录 metadata；`PARSER_REVISION` 进入每条记录，增量扫描对旧解析版本重新解析；新增 FTS5 令牌表 `asset_tokens`（路径词、标识符、驼峰拆分、纹理名、shader 类型…，无 FTS5 时退化为 LIKE）与 `search_tokens(tokens, kinds, class_names, limit)`（IDF 权重：稀有令牌分高；返回每个候选命中的令牌）；`status()` 报告 `engine`/`tokenIndex`。
- `shader_asset_retrieval.py`：`texture_name_tokens`（RenderDoc 纹理调试名 → 去前后缀的检索令牌，过滤 white/default/SceneDepth 等泛名）；`retrieve(catalog, features, top_k)`：令牌检索 → 候选文档（类、名字、纹理、父材质、shader 类型、宏）→ 一致性检查（stage 有对应 PS/VS、pass 提示 ↔ shader 类型、skeletal ↔ GPUSkin 工厂、纹理数量合理）→ `adjustedScore` 与 `why`、`decisionHint`（clear leader / ambiguous）。`ShaderAssetResolutionStore`（`shared/dictionaries/assets/shader-asset-resolutions-v1.sqlite3`）：按 `(gameKey, rawSha256, stage, entry)` 记正/负决策，按 `structuralKey`（stage + cbuffer 大小集合 + 纹理/采样器/UAV 数 + 入口）给同形状建议；`lookup/record/forget`。
- server 工具：`retrieve_shader_asset_candidates(event_id, stage, top_k, pass_hint, mesh_hint, extra_tokens, force_search)`（先查记忆，命中直接返回 `resolvedDocument`；否则检索）、`submit_shader_asset_resolution(event_id, stage, asset_id, confidence, rationale, reject)`、`forget_shader_asset_resolution`。特征来自活动目录选中的 shader（raw hash、ABI 的 cbuffer 大小）与 Bridge `get_bound_textures` 的 `texName`。gameKey = `scope.gameId@buildId`（未配置则资产根哈希）。
- 技能：`renderdoc-shader-workbench` v15 步骤 4(b) 加入检索通道与记忆流程；`renderdoc-unpacked-asset-intelligence` v4 的 Capture correlation 说明检索器与记忆。
- 单测：`test_shader_asset_retrieval.py` 4 项（合成 UE5.4 包：包头/名字/导入/挂载/令牌；.uexp 字符串；目录扫描的引擎/关系/FTS；纹理名驱动检索 + 记忆命中/否决/同形状建议/遗忘）；`test_asset_catalog` 6 项仍通过。实测 SilverPalace 角色材质子树：绑定纹理 `T_Crow_Body_D/N + T_Matcap` → `M_CharacterPBR_Crow` 为 clear leader，四项一致性全部吻合。

**下一步（未做）：** 引擎 UB 字典（View/Primitive 按 UE 版本）与"AI 确认一次即成规则"；把已解决的材质文档接进 organization 的 `cbufferAliases`/hint provider（参数名 ↔ 成员由 AI 按用法对齐；若 UEX/CUE4Parse 提供 preshader 布局则可直接给偏移）；Unity/Godot 专用提取器（目前靠通用文本/路径令牌）；`.ushaderbytecode` 解压 provider（需游戏 oo2core）以获得 tier-0 哈希同一性。

## 0o. 按需索引、分层深度、置信度与局部匹配（2026-09-07）

用户要求：配置解包目录时不要立刻索引；AI 反编译/分析 EID 需要资产时自动触发查找与分析，并记录正确匹配；没有强规则，靠 AI 结合参数、cbuffer、纹理数量、甚至纹理内容比对来匹配；要有置信度，部分匹配（如只有纹理或部分函数对上）也应作为反编译、cbuffer 别名、整理 HLSL 的参考，而不是因为一小部分不匹配就全部丢弃。

- `asset_catalog.scan(depth=full|headers|names, scope_prefixes=[...])`：`names` 只遍历（路径令牌，秒级）；`headers` 只哈希+解析小的描述性文件（包头 ≤2MB、材质/着色器/元数据、材质类的 `.uexp` ≤8MB——同目录 `.uasset` 先于 `.uexp` 排序，用 `material_stems` 判定）并把 bulk/大文件记为 `deferred`（hashStatus/parseStatus=deferred，仍进令牌索引）；`full` 保持原行为。`scope_prefixes` 只对前缀内的文件做深度工作，其余复用旧记录或 deferred。`hydrate(root, relative_paths)` = 增量 + full + scope，用于按需补齐候选。`resolve` 对未哈希资产给出明确拒绝。
- server：`index_active_unpacked_assets(depth="headers")` 默认改为 headers；新增 `_ensure_shader_asset_catalog()`：`retrieve_shader_asset_candidates` 首次调用时若无目录（或目录早于令牌/引擎解析）自动做 headers 索引，结果里 `indexing` 回执说明动作与耗时；候选的 `.uexp` 若为 deferred 则自动 hydrate（≤16 个）后重新检索。`configure_active_project_asset_root` 不触发索引。
- 置信度与局部匹配（`shader_asset_retrieval.retrieve`）：每个候选 `confidence`（令牌 IDF 覆盖率 + 一致性比例 + 领先度 + 唯一领先加成；纹理 facet <0.5 时封顶 0.75，整体上限 0.95）与 `facets`：`textures.explained/unexplained`（绑定纹理名是否出现在该材质或其父材质的纹理引用里）、`shaderTypes.agrees/disagrees`、`names.available`；文档新增 `parent`（父材质名字/纹理）、`instanceCount/instances`（反向关系，`AssetCatalogService.referrers`）、`exportParseStatus`。`policy.partialEvidenceUsable=true`。
- 记忆：`lookup` 返回全部正决策 `resolutions`（实例 + 父材质 + 共享函数可并存）；`submit_shader_asset_resolution(..., facets_json)` 记录分面置信度。
- 技能 `renderdoc-shader-workbench` v16：无需预先索引；"局部匹配是证据不是失败"规则；分面置信度进入推断命名的置信度；允许多条正决策。
- 待做：纹理内容比对（RenderDoc `get_texture_data` 最小 mip 的感知哈希 ↔ 资产侧解码；UE 需 BC 解码器，PNG/DDS 引擎可直接做）；Unity/Godot 提取器；引擎 UB 字典；与 organization 命名的自动衔接。

## 0p. 数值匹配、失败说明、成员规则记忆（2026-09-07）

用户三点：解析失败（加密等）要说明原因；用 cbuffer 数值与资产数值比对（复杂数字 + 相邻若干个都对上 ≈ 确定），由此顺着关联资产扩展，纹理同理（抽像素）；每一步给 AI 足够说明；并做"AI 确认一次的参数↔成员对应落成该游戏的规则"。

- **Bridge** 新增 IPC `get_constant_buffer_values(event_id, stage)`：按反射的每个常量块取绑定缓冲原始字节（`GetBufferData`，≤64KB），返回 float32/uint32 lane 数组、bind/space/byteSize、缓冲名；无绑定/失败按块给 `reason`。
- **资产侧数值指纹**（`unreal_package.numeric_fingerprints`）：对材质类 `.uexp` 前 2MB 按 4 种字节对齐扫 float32，`is_distinctive_value`（有限、1e-4~1e6、不是 3 位小数以内的"圆"数）过滤，重复 >32 次视为填充剔除；产出 `numericValues`（`%.7g` 键，≤512）与 `numericRuns`（连续可辨值的串，含偏移）。文本资产（Unity YAML/.mat/.mtl）用 `_text_numeric_values` 提取 ≥4 位小数的字面量并按 float32 归一。目录新表 `asset_values(value_key, asset_id)`；`AssetCatalogService.search_values(values)` 返回命中键、键流行度与命中的 runs。
- **检索融合**（`shader_asset_retrieval.value_evidence` + `retrieve`）：Host 侧 `_shader_asset_capture_features` 从 Bridge 取 lanes（只留可辨值，≤1024，含 `constantBufferValueStatus` 说明）；`.uexp` 命中映射到所属 `.uasset`；`rareMatchedCount`（流行度 ≤3 的值）、`adjacentRuns`（资产连续值 ↔ cbuffer 连续 lane）；强度 = rare + 2×adjacency，置信 = 1−0.5^强度（上限 0.98；只命中流行值封顶 0.2）；仅靠数值命中的资产也会加入候选；`adjustedScore` 加成 3×rare + 4×adjacency；candidate 的 `valueEvidence` / `facets.values`（含 `laneRefs`、`viaParent`）与 `why` 文本。无纹理名但有 lanes 时照样检索（合成测试：3 个连续复杂值 → 置信 ≥0.9，单个 0.5 → 无候选）。
- **失败/覆盖说明**：`AssetCatalogService.coverage()`：按 kind×parseStatus 计数、解析失败原因 Top（含示例路径）、仍打包的归档、deferred 数、引擎信息，生成 `limitations` 文案（"N 个 .ushaderbytecode 为压缩归档，哈希同一性不可用，改用名字/关系/数值"、"N 个文件解析失败：package tag mismatch（可能加密/仍压缩/IoStore 缺上下文）…"）。`retrieve` 结果带 `coverage`、`limitations`，每个候选带 `parseIssues`。UE 包头 tag 不匹配时错误信息给出可能原因。
- **成员规则记忆**：`aliases_from_renames` 输出带 bind/space/byteSize/original；`_publish_cbuffer_aliases` 返回 `memberRenames`；server `rename_shader_workspace_symbols` 之后 `_remember_member_rules`：若该 shader 已有资产决策，把成员重命名按 `(gameKey, familyKey, space, bind, byteOffset)` 写入 `member_rules`（familyKey = 决策资产路径 + 其父材质路径；高置信覆盖低置信）；结果 `memberRules` 说明记录情况或为何没记。hint provider `_workspace_asset_layout_provider` 先查记忆（`_shader_asset_memory` → 家族规则 → 用捕获 `compiledInterface.constantBlocks` 的 byteSize 组成 hint，`relation.retriever="member-rules"`），再回落 symbol DB。
- **局部资产命名**：`_AssetNaming(allow_partial=True)`：资产/规则未覆盖的访问 lane 生成 `__replaylab_*` 合成成员，其余按资产名声明，`naming_source="asset-partial"`；不再因一个 lane 无对应而整块放弃。
- 技能 v17：步骤 2b（有资产根时反编译前先检索/提交，让固定修复带上真实名字；读 `limitations`）；"数值证据优先于名字"；"你确认的名字会成为规则"。
- 单测：retrieval 8 项、asset naming 9 项、catalog 6 项、organization 58 项、repair 4 项通过。未做：纹理内容比对（需 RenderDoc 读像素 + 资产侧解码；UE 需 BC 解码器）。

## 0q. 纹理内容比对（2026-09-07）

用户：数值匹配作为额外补充；继续做纹理内容比对（要考虑压缩格式、镜像等，用快速的开源式算法判断"是不是同一张"），也是高价值信号；这些都由 Host 快速处理以省 token；但 AI 仍要自己检索，不能完全依赖。

- `symbols/imaging.py`（纯 Python，冻结 Host 无第三方依赖）：BC1/BC2/BC3/BC4/BC5 块解码、B8G8R8A8/R8G8B8A8/G8/R8/A8/R8G8/G16、PNG（8 位、非交错）与 DDS（fourcc/DX10）解码；`gray_thumbnail` 16×16 亮度缩略图（盒滤波，支持上采样）+ 均色；`dhash`（9×8 差分哈希 64 位）在 8 个二面体变换（镜像/旋转）上取最小汉明距离，≤6 视为同图、≤12 可疑；`flat` 标记纯色图（能匹配一切，不作身份证据）。`symbols/bc7.py`：BC7 全 8 模式解码（分区表/锚点/权重按 D3D11 规范）。用 SilverPalace 真实纹理做自洽验证：同一纹理 mip32 与 mip16 的缩略图哈希距离 0–5，BC7 与 DXT1/DXT5 分布一致、均色一致 → 解码正确。
- `symbols/engines/unreal_texture.py`：在 Texture2D 的 `.uexp` 中定位 `FTexturePlatformData`（PF_ 像素格式串 → 前 12 字节 SizeX/SizeY/PackedData），遍历 mip 表——UE5.3+ 的"数据资源索引 + 内联载荷"样式与旧式 `FByteBulkData` 头都支持，通过"下一组 (SizeX,SizeY,1) 是否等于预期尺寸"对齐——选 8~128px 间最接近 32px 的内联 mip 解码指纹（~1ms）；.ubulk 中的大 mip 不读。150 个真实纹理样本：DXT1 56、DXT5 53、BC5 22、BC7 15、G8 4 全部成功。不可解码格式（ASTC/浮点）按名给出原因。
- 目录：`.uexp` 依同目录先解析的 `.uasset` 类（`stem_classes`）判定纹理导出，header 深度即指纹化（不做数值扫描，避免像素噪声）；PNG/DDS 文件 ≤2MB 直接指纹化；新表 `asset_thumbnails`（8 个变换哈希 + 均色 + 尺寸 + flat + 格式）；`search_thumbnails(dhash, mean_color)` 全表汉明比较（4 万纹理级别 <1s），均色差 >96 的"精确命中"降级为 plausible（亮度哈希分不清通道互换）。`coverage()` 报告 `thumbnailRows` 与未指纹化纹理的原因分布。`PARSER_REVISION=3`。
- Bridge 新增 IPC `get_texture_fingerprints(event_id, resource_ids ≤32)`：选最大边最接近 32（≤64）的 mip，用 RenderDoc `SaveTexture` 转 PNG（任何 GPU 格式含 BC7/浮点都由 RenderDoc 转换），base64 回传（≤256KB），逐纹理给失败原因。Host 侧用自己的 PNG 解码算指纹。
- 检索融合：`texture_identity_evidence`：每个绑定纹理 → 命中的资产（距离、变换、verdict、被多少材质引用）→ 经 `uses-texture` 反向关系归到材质；`facets.textureIdentity`（sameImage/plausible/textures/confidence，父材质命中标 viaParent）；`adjustedScore` +6/同图 +2/可疑；仅靠像素命中的材质也进候选；数值与像素同时 ≥0.75 时置信 0.97；`signals.textureIdentity` 列出每张绑定纹理的结论或原因（纯色、快照失败、无命中）。`policy.aiMustStillSearch=true`。
- 技能 v18："内容同一性是最强的廉价信号"；"这些 Host 信号是省 token 的补充，不替代 AI 自己的检索"。
- 单测 `test_texture_identity.py` 4 项（BC1/DDS、PNG 往返 + 镜像/缩放不变、合成 UE 纹理导出解析、目录扫描指纹 + 镜像捕获匹配 + 仅像素检索）；retrieval 8、catalog 6 通过。

## 0r. 通用优先：引擎无关的内容探针（2026-09-07）

用户问为什么要分 UE/Unity 专用提取器，希望通用提取 + 识别到引擎时的专项增强。结构改为 **通用层是底座、引擎层只加精度**：
- `symbols/generic_probe.py`：对任何文件（前 4MB）提取：标识符字符串（ASCII + UTF-16LE，频次排序，≤384）、路径样字符串、可辨数值指纹（复用 `numeric_fingerprints`，仅 ≤1MB 的文件——大文件多为几何/动画）、内嵌图片（PNG/DDS 魔数定位 → 指纹，≤512²）、内嵌 shader 容器计数（DXBC/SPIR-V/MetalLib/#version）；`mention_references`：文件内字符串等于目录中另一文件的 stem（≥6 字符、全树唯一、同目录 .uasset/.uexp 视为一体）→ `mentions-asset` 启发式关系（confidence 0.5、标 heuristic）。
- 目录：`EngineContext.stem_index` 在文件名预扫描时建好；`_parse_asset` 对 other/binary/未被引擎解析的 package/非文本 metadata 走通用探针；header 深度也解析 ≤512KB 的 other/binary/metadata（Unity .asset/.prefab、自定义描述文件）；`asset_tokens` 纳入 `strings`/`pathReferences`。
- 检索：类过滤只在引擎为 unreal 时默认启用（其它引擎没有 className），kinds 覆盖 other/binary/scene/mesh；未知引擎合成测试（二进制材质 blob + PNG 纹理）：名字令牌、数值相邻串、mentions 关系、纹理指纹全部生效。
- 数值置信收敛：单个孤立命中在大目录里可能是巧合（网格/动画浮点也"罕见"），改为 strength = (rare−1) + 2×adjacency；单命中 0.25、两个 0.5、三个 0.75、相邻串主导；UE 导出仅对材质/函数/参数集合类做数值扫描。
- 技能 `renderdoc-unpacked-asset-intelligence` v5 说明通用层/引擎层关系。单测 19 项通过。

## 0s. 结构/数值/像素/网格/代码逻辑优先，名字只作参考；后台索引；打包前状态（2026-09-07）

用户：匹配主要靠参数结构、参数数值、纹理同一性、网格、代码逻辑结构（Host 查找 + AI 查询）；名字在真实抓帧里通常不存在，只能参考；引擎专项可选，主要靠 AI 与 Host 协作（Host 给调试信息，AI 调方向）；接下来打包找一个 EID 测试，要求内置 AI 自动跑完整条链路。

- Bridge 新增 `get_draw_geometry(event_id)`：索引/顶点/实例数、拓扑、已用顶点输入（semantic/format/slot/perInstance）、顶点/索引缓冲身份与名字、`meshHint`（BLENDINDICES/WEIGHT→skeletal，perInstance→instanced，否则 static）。
- server 特征新增 `drawGeometry`（自动补 `mesh_hint`）与 `codeHints`（若该 shader 的 workspace 已存在：`usesDiscard`、`textureSampleCount`、`usesDerivatives`、`outputTargetCount`）。
- UE 提取：非材质包（网格/蓝图）导入的材质记为 `uses-material`（材质实例的才是 `parent-material`），元数据 `usesMaterials`；文档新增 `usedByMeshes/usedByMeshCount`（反向关系）。
- 一致性检查新增：`blend-mode`（discard ↔ 资产名含 BLEND_Masked/OpacityMaskClipValue 或宏 MATERIALBLENDING_MASKED；discard 却只有 Translucent/Additive → 矛盾）、`sample-count-plausible`、`usage-flag`（skeletal ↔ bUsedWithSkeletalMesh/GPUSkin）。
- 名字降权：仅名字命中（纹理未被解释或根本没有纹理名）置信上限 0.6。
- 结构记忆进候选：`memory.sameShapeSuggestions`（同 cbuffer 大小集合/纹理数的已决 shader）在 server 侧合并为 `facets.structure` 与 `retrieval.structureSuggestions`，加分并抬置信。
- `retrieval.debug`：逐通道（names/texturePixels/cbufferValues/geometry/codeHints）说明可观测与失败原因，`nextSteps` 给 AI 下一步（无名字→靠像素/数值/结构；无指纹命中→查 coverage.limitations；无可辨数值→数值通道失效；无候选→自己 query_active_unpacked_assets；候选接近→比父链/usedByMeshes/blend-mode；workspace 尚无→prepare 后再调用以获得 codeHints）。
- 无资产根：`retrieve_shader_asset_candidates` 返回 `ready:false` + 原因，而非错误。
- **后台索引**：首次使用时的 headers 扫描放到后台线程（`_asset_index_job`，带 progress 回调：walked/hashed/reused/deferred），前台最多等 25s；未完成则返回 `ready:false, indexing:{status:running, elapsedSeconds, progress, retryAfterSeconds:30}`，AI 继续后续步骤、命名前重试（技能 v20 已写明），避免 MCP 客户端超时。
- 单测 28 项通过。**打包前检查清单**：`REBUILD_HOST_AND_BRIDGE.cmd`（Host+agent+Bridge，含新 IPC）→（可选）`REBUILD_QRENDERDOC.cmd`（早前 C++ 改动）→ 配置活动项目 `configure_active_replay_project`（gameId/buildId）与 `configure_active_project_asset_root(E:\SilverPalace_Unpacked)`（不索引）→ 选一个 EID 让内置 AI"反编译并整理"。预期链路：context → workspace path → retrieve（首次触发后台索引，返回 running）→ prepare/decompile（固定修复）→ 声明对照/ABI 修复通道 → compile/validate → retrieve 重试 → submit 决策 → begin organization → rename（成员规则落库）→ review → register → apply → live edit → report + 对话式总结。


## 0t. 2026-09-07 运行时故障定位与"常驻 Host 统一"决策（未实施）

- 现象：内置 AI（Cursor ACP 连接）`ConnectorError: ACP session/new timed out`（60 s 预算，`acp_session.py`）。
- 探针 `output/shader-workspace-20260906/PROBE_MCP_ROUTE.cmd` 结果：冻结 `ReplayLabMCP.exe --help` 0.8 s，源代理→冻结 Host initialize 1.8 s / list_tools 1.9 s，144 工具与 `policy.py` 清单一致。**MCP 路线不是原因**，超时在 Cursor 自身的 `session/new`。探针第二阶段现已改为自动测试全部 `*-acp` 连接（Cursor 优先），分别测"不带 / 带 ReplayLab MCP"两种 `session/new` 并打印 cursor-agent stderr；等待用户回传。
- 架构现状：qrenderdoc 启动时拉起 `ReplayLabInternal.exe`（私有服务）和 `ReplayLabMCP.exe ensure`（全机回环单例，供外部 IDE）；**内置 AI 不用该单例**，而是由厂商进程在 `session/new` 时启动 `replaylab_agent.mcp_proxy`，代理再 spawn 任务私有的 `ReplayLabMCP.exe stdio`，任务上下文（`REPLAYLAB_TASK_ID`/`REPLAYLAB_TASK_EVENT_ROOT`、qrenderdoc 实例 pin、live-edit 注册表、`REPLAYLAB_TOOL_SCOPE`）经进程环境变量传入。
- 用户决策：**先修 Cursor、先完成 SilverPalace EID 端到端测试，之后再做"常驻 Host 统一"重构。** 重构方案要点（已与用户确认方向）：每个 qrenderdoc 进程一个常驻 Host（项目打开即就绪），内置 AI / 外部 IDE / 外部 agent 全部经代理连接同一 Host；工具调用记录移入代理；任务上下文改为请求头按会话传递，Host 从当前请求上下文读取而非环境变量；后台资产索引、工具链与目录缓存、工作区监听随 Host 常驻；需补每次调用的取消/超时。改动面：`replaylab_host.py`（按实例 serve）、`server.py`（请求级上下文）、`mcp_proxy.py`（连接模式）、`renderdoc_mcp_bridge`（按实例拉起）、`native_mcp.py`（URL 路由）、打包脚本、文档，随后重新冻结。

- 第二次探针（全部 ACP 连接）：Cursor `session/new` 不带 MCP 5.3 s、带 MCP 7.1 s；Claude Code ACP 2.0 s / 0.5 s。**本地与厂商侧当前都正常**，20:15/20:17 的两次超时是瞬时故障（Cursor 后端/网络/首启自更新），日志（`agent-core.log`）在 02:12–20:15 之间无任何记录，无法进一步归因。
- 加固（源码 Agent，无需重新冻结）：`acp_session.py` 的 `open()` 改为按 `_ACP_SESSION_NEW_TIMEOUT=60 s` 首试，`initialize`/`session/new` 超时则用**全新厂商进程**重试一次（`_ACP_SESSION_NEW_RETRY_TIMEOUT=90 s`）；两次都超时时先跑 `_diagnose_local_mcp_route`（20 s 内 initialize+list_tools 本地 ReplayLab MCP 路线），错误信息明确指出是"本地路线健康→厂商侧停滞"还是"本地路线失败：<真实错误>"。测试 `tests/test_agent_acp_session.py` +3（19 通过，在云容器 Python 3.11 + pydantic/mcp 下运行）。
- 观察：cursor-agent.cmd 正常关闭后 node 子进程仍短暂持有工作目录（探针清理 WinError 32），与 `_force_stop_agent_process_tree` 文档描述一致，非本次故障原因。


## 0u. 2026-09-07 EID 11402 复测复盘 → 五项改造（已实现，待重新冻结 + 复测）

复测事实（`ai-tasks/a925…` 8.5 min/24 调用，`5186…` 29 min/31 调用 + 3 个 Cursor 子 agent 共 15.5 min）：RenoDX 拒绝 `Unexpected dynamic cbuffer offset` → 候选 1（dxil-spirv+SPIRV-Cross）成功；第一次工作区建在 `%LOCALAPPDATA%` 导致注册被拒、第二次在工程内重跑整条链；live-edit 持久授权 8 个满；组织评审 3 轮全被模板格式错误吃掉，用尽后 AI 又连写 5 次；资产索引在任务进程内后台跑、随进程死亡；344 KB HLSL 被分块读 8 次 + 子 agent 啃读。

1. **路线记忆**（`shader_workspace/route_memory.py`，`shared/dictionaries/shaders/route-memory.json`）：key=(api|encoding|stage|profile|editable)；`apply_shader_workspace_shared_resource` 成功 → `record_success`；`decompile_shader_workspace` / `try_shader_workspace_recovery_candidate` 失败 → `record_decompile_failure`；`ShaderWorkspaceService(route_memory_provider=…)` 在 `create()` 自动选路时 `prefer()`（成功优先 > 跳过已知失败 > 目录默认），manifest/返回值含 `routeMemory`；`plan_shader_recovery` 也返回 `routeMemory`。测试 `test_shader_route_memory.py`（7）。
2. **工作区位置 + 授权回收**：`server._enforce_active_project_workspace_location`（create/prepare[无 binding 时]/recovery attempt 三处，`ShaderWorkspaceLocationError` 给出正确路径）；`shader_live_edit._PersistentLiveEditRegistry.reclaim_for(identity, protected_keys)`（dead → 同物理 shader 旧尝试 → 其他捕获最久未用），`_start` 在 `authorize_for_start` 前调用，结果含 `reclaimedAuthorizations`。测试 `test_shader_live_edit_reclaim.py`（6）。
3. **评审预算**：`equivalence.ReviewRounds` 新增 `templateErrors`（独立计数，limit=max(6,3×base)）、`extraRounds`/`grants`（用户授予，≤3 次/≤9 轮）、`userSoftPublishAllowed`；`build_verdict(workspace_path=…)` 用尽时附 `userDecision{required, question, options[extend|publish-baseline|stop], tool, toolArguments}`；`organization.review` 在用户选择 publish-baseline 时 `_waive_blockers` 并 soft-accept；`server.write_shader_workspace_organization_file` 模板错误走 `record_template_error`（不耗内容轮次），用尽后 `_ReviewBudgetStop` 不再计数；新工具 `decide_shader_organization_review_budget(workspace_path, decision, user_confirmation, extra_rounds=3)`；模板错误信息列出缺失/多余键。Agent：`policy.py` 登记；`agent_prompts.py` ask 规则加"userDecision.required 是唯一例外"。测试 `test_shader_equivalence.py` +3，`test_shader_template_contract.py` 改为新契约。
4. **资产索引持久化**：`asset_catalog.scan(checkpoint_seconds=90)` 周期性发布部分目录（metadata `complete=false`+`progressJson`，未走到的文件沿用上次记录，覆盖不回退），`status()` 返回 `complete`/`progress`；`asset_index_jobs.py`（跨进程通用 job 表）；`ReplayLabInternal` 新路由 `POST /replaylab/asset-index`（`ensure|status`，不接受任何路径，自己解析活动工程），health capabilities 增加 `asset-index`；`asset_index_client.py`；`server._asset_index_job` 优先内部服务、不可达时回退进程内；`_ensure_shader_asset_catalog` 对 checkpoint 目录返回 `partial:true` 可用结果；`get_active_unpacked_asset_index_status` 增加 `backgroundJob`。测试 `test_asset_catalog.py` +1，`test_asset_index_jobs.py`（4）。**需要重启 qrenderdoc 才有新的 ReplayLabInternal；否则自动回退旧行为。**
5. **大 shader 结构化视图**：`shader_workspace/outline.py` + 工具 `get_shader_workspace_outline(workspace_path, relative_path, function, lines)`（声明/函数/调用图/采样点/成员使用点/匿名度 + 按函数或行段切片，≤400 行）；policy 只读清单登记。测试 `test_shader_outline.py`（2）。技能：`renderdoc-shader-workbench` v21、`replaylab-core` v4、`renderdoc-unpacked-asset-intelligence` v6（路线记忆、位置、授权、预算提问、outline 优先、禁子 agent/grep 读 Host 文件、索引进行中不放弃资产线）。

验证：云容器（Py3.11 + pydantic/mcp）跑 Host 相关 223 unittest + 61 pytest 风格（shim）全部通过；Agent `test_agent_acp_session` 19 通过、`test_native_engine` 13 通过（需 PATH 上有 claude-agent-acp）；`test_task_mcp_proxy` 中一个用例因容器 `sys.executable` 是符号链接被代理拒绝（环境问题）；Host 工具 146 个，policy 清单模拟校验通过。

下一步：`REBUILD_HOST_AND_BRIDGE.cmd` 重新冻结（Host + Internal），重启 qrenderdoc，再对 EID 11402 复测；观察 `routeMemory.applied`、工作区路径、`reclaimedAuthorizations`、`userDecision` 提问、`partialCatalog`、outline 使用与总 token。

- 追加（同日）：**路线记忆按物理特征分键**——`route_memory.dxil_route_features(ll)` 从 DXIL 反汇编识别动态下标的 cbuffer 访问（`cbufferLoadLegacy/cbufferLoad` 第三操作数为 `%值`），签名 `dyncb|static|nocb` 进入 route key；`ShaderWorkspaceService.create()` 在 staging 中先跑一次专用反汇编探针（`_probe_route_features`，不留 ABI 证据记录、删除探针 job），再按 (key+特征) 应用记忆并在需要时重建 routePlan/recoveryPlan；manifest/status 含 `routeFeatures`。内置 `KNOWN_LANE_INCOMPATIBILITIES = {(community-renodx-dxil-hlsl, dyncb)}`：即使没有工程记忆，动态 cbuffer 的 DXIL 也直接跳过 RenoDX；静态的仍然 RenoDX 优先（用户的原始排序）。结论：EID 11402 之前从未在 RenoDX 成功过（09-05 诊断已记录 `Unexpected dynamic cbuffer offset`），成功的是别的 EID；流程与以前相同（decomp.exe 直接吃 DXIL 容器，内部用 dxcompiler.dll 反汇编）。
- 追加：**组织文档草稿模式**——`write_shader_workspace_organization_file(..., mode="draft")` 把部分 JSON 合并到 `ai/organization-sessions/<id>/drafts/<kind>.json`（modules/stages 按名合并），返回全部问题（≤64）且不计数；`mode="final"` + 空 content 发布草稿；flow_spec/analysis 的最终错误信息也改为列出全部问题。测试 `test_shader_template_contract.py` +1。修了两个早前漂移的测试（`test_internal_host` 路由集合含 shader-source/check 与 asset-index；`test_cbuffer_aliases` 条目含 byteSize/original）。
- 待用户决定：**Host 侧结构性重构操作**（按行段抽函数、Host 做活跃变量分析并回编译验证），因为 300 KB 级单函数 shader 无法由 AI 整体重写提交（输出 token 上限）。

- 追加：**Host 侧结构性重构**（`shader_workspace/restructure.py`）：`extract_function(source, start, end, name, comment)` 在一个函数体内按完整语句范围抽函数——校验大括号平衡/语句边界/if-else 切割/`return`/逃逸的 `break`、`continue`；数据流分析得到 in / inout / out / 单返回值，参数类型取自声明或函数签名，数组局部量拒绝，参数上限 12；新函数插到调用者上方，范围替换为调用；`apply_operations` 顺序重放；`structure_report` 给每个函数尺寸/参数/调用/匿名量与启发式 flags（large/tiny/many-parameters/anonymous-locals/placeholder-name）。会话集成：`ShaderOrganizationManager._regenerate_proposal`（baseline → renames → structureOps → comments，固定顺序，`rename()` 也走它），`restructure(workspace, org, operation=extract|undo|remove|rename_function|list, ...)` 重放后 `service.compile_check` 通过才落盘并封入 session（`structureOps`），拒绝时会话不变、不计预算。工具 `restructure_shader_workspace_source`（policy: workspace）。评审：结构变更多数编译成同一份可执行码（exact-code），否则走既有的回放观测比较。测试 `test_shader_restructure.py`（5）、`test_shader_organization.py` +2。技能 step 7 改为迭代流程（切分 → 结构自审 2–3 轮 → 内部命名/注释），明确"AI 永远不整体重写文件"。Host 工具 147 个，清单校验通过。
- 追加：**函数账本（ledger）**——`restructure` 新增 `note` 操作（session `functionNotes`），每次结果返回 `ledger`：每函数一行 {purpose, lines, origin, inputs/inouts/outputs, calls, callers, globalsUsed, anonymousLocals, flags, status}；`structure_report` 增加 `globalsUsed` 与 `many-globals` 标记；`list` 只返回账本与汇总。技能 step 7(b) 改为"从账本而非代码做合并/拆分决策"。

## 0v. 2026-09-08 启动加载失败 + 对话内实时推理 + 结论优先

- **项目加载失败 `ReplayLab internal Host health identity is invalid`**：原因是 0u 给 ReplayLabInternal 的 health `capabilities` 加了 `asset-index`，而 bridge 的 `project_restore_client.py` / `shader_editor_client.py` 用精确列表比较 health 身份。修复：两者改为 `_has_required_capabilities()`（要求四个已知能力为子集，允许新增），测试 fixture 改为带 `asset-index` 的列表。需要重新部署 bridge（`REBUILD_HOST_AND_BRIDGE.cmd` 含 bridge 部署）。注：`test_project_restore_client` 里另有 3 个"restore result identity"用例在改动前就失败（工作区未提交改动的漂移），与本次无关。
- **对话内实时推理**（qrenderdoc C++）：`AIAssistant.cpp` 的 `reasoning-delta` 分支不再只改状态栏；主 Agent（无 planTaskId）的原始推理增量通过 `appendTaskActivityText` 写入活动组里一条 `reasoning-raw` 实时行（标题"推理过程"，上限 256 KB，超出提示见监视器），任务结束时该行完成并随活动组折叠（`setTaskState` 折叠判定排除 `reasoning-raw`）；`AIConversationView::activityKindLabel` 增加 `reasoning-raw`；新增 `m_TaskReasoningChars`。子 Agent 推理仍只在监视器。
- **结论优先**：`finishTask` 的最终消息改为 Agent 的对话式总结在前，结构化结果表作为"详情"在后（`renderTaskReport(..., includeSummary)` 在有独立总结时不再重复 Conclusion）；`agent_prompts.py` 两处总结规则细化为"面向没看过程的人：第一句结果，然后做了什么/修了什么，未完成及原因，至多一个下一步"。翻译：TS 手工加入 `Reasoning`（AIAssistant、AIConversationView 两个上下文）、`[reasoning continues in the Agent monitor]`、`Details`（AIAssistant）。需要 `REBUILD_QRENDERDOC.cmd`。
- 智能体监视器三页含义：原始推理 = 厂商思维流（ACP thought chunks / channel raw）；推理摘要 = 厂商给的推理摘要（Codex reasoning summary、API 路线 activityKind=reasoning）；消息 = 助手消息增量（含子 Agent 草稿）。可用性状态 not-emitted / opaque / limit-reached 说明为何某页为空。

## 0w. 2026-09-08 第二轮复测复盘（Cursor 连接 + 九项）→ 已实现，待冻结 + 复测

复测事实（`ai-tasks/afa11…` 与 Cursor "1?" 任务）：ACP `session/new` 首次超时、重试用新进程 7 s 打开（怀疑残留 cursor-agent node 进程）；"1?" 被强行当成抓帧分析；资源 id `1000000000000000755` 在 Cursor/Node 端被 IEEE-754 改写成 `…800`，AI 反复对拍浪费 ~90 s；`prepare_shader_workspace` 返回 94 KB、status 43 KB，`organizationId`/recovery 数据被截断丢失；文件工具被拒后放弃；资产索引 "not ready" 就跳过资产线；抽函数因 `\r\n` 混合换行错位报 "function definition is not allowed here"；cbuffer 未按资产重命名；最终消息带"详情"表且无产物入口。

1. **Cursor 连接**（`connectors/acp.py`）：`_WindowsJob`（CreateJobObjectW + KILL_ON_JOB_CLOSE，`_managed_agent_process` 把 agent 进程放进作业对象，退出时整棵树随作业关闭，不再留 node 孤儿）；`acp_session.open()` 保留 60 s/90 s 两段重试与 `_diagnose_startup_timeout`（说明卡在 Host 侧还是 IDE 侧）。
2. **意图优先**（`agent_registry.rank_skills(require_match=True)` + `native_engine._select_skill`）：只有请求真的命中领域词才挂技能；中文/简短请求通过 `_CJK_TERM_HINTS`（着色器→shader、导出→export…）与 `has_domain_signal` 兜底；短 ASCII 词要求整词匹配（"ok" 不再命中 "token"）。"1?"/"你好"/"继续"/"刚才那个结果是什么意思" → 无技能，只带 replaylab-core 作答或反问；`agent_prompts.py` API 路线加 "Intent first"。测试 `test_skills.py` +1、`test_native_engine` 5/5。
3. **资源 id 精度**（`server._accept_string_resource_ids` / `_coerce_resource_id`）：所有 `*resource_id` 参数 schema 变为 `integer | string`（含 `ResourceId::` 前缀、可选参数为 `| null`），字符串无损转 int；FastMCP 建 147 个工具通过，`"1000000000000000755"` 与 int 结果一致。
4. **输出压缩 + 恢复句柄**：`MAX_AGENT_RESULT_CHARS=28000`，`_ok_compact/_compact_value` 截长列表与大字符串并标注；`_shader_workspace_response` 注入 `recoveryHandoff{workspacePath, organizationId, routeMemory, nextTool…}`，即使正文被截断也能继续。`begin_shader_workspace_organization(restart=False)` 默认**续接**已打开会话（`resumed=True, progress`），`get_shader_workspace_status` 带 `organization` 续接块（新对话/中断后不重跑）。测试 `test_shader_template_contract` 更新为续接语义。
5. **评审预算第四选项**：`userDecision.options` 增加 `finish`（"无视评审预算，整理到完成为止"，`ReviewRounds.grant(unlimited=True)` → `UNLIMITED_ROUNDS=60`），`decide_shader_organization_review_budget(decision="finish")`。
6. **资产线**：索引未就绪不再是终点——`retrieve_shader_asset_candidates` 对 checkpoint 目录返回 `partialCatalog`，前台最多等 `ASSET_INDEX_PARTIAL_FOREGROUND_BUDGET_SECONDS=5`；index status 给出 `requiredAction`（retrieve / configure_active_project_asset_root）；技能 v22 要求 cbuffer/纹理命名必须先过资产检索并把命中写入工程决策记忆。
7. **抽函数换行修复**（`restructure.extract_function`）：按 `\n` 切分并去 `\r`，用主导换行重组；混合换行的真实反编译文件不再错位。
8. **大小分级**（`outline.sizeClass small|large` + `strategy`）：≤500 行/函数 ≤160 行直接整理；否则分段抽函数→账本→合并。技能 step 7 按此分支。
9. **产物与总结 UI**（qrenderdoc）：`task_report.artifact_with_stats` 给每个产物 `stats{bytes,chars,lines,addedChars,removedChars}`（对比 `ai/decompiler-original.hlsl`）；`finishTask` 只保留对话式总结，产物以 pill chip 显示（`+新增 / -删除` 字符数），左键打开、右键"打开文件/复制路径/在文件夹中显示"（`replaylab-artifact://open|reveal/<path>`），持久化 `row["artifacts"]`；活动组内 `reasoning-raw` 实时推理行。Shader 工作区库树右键"把这个 Shader 加入对话"（`project_shader_workspace._add_workspace_to_chat`）；`AIConversationView` 选中文字右键"Add to chat" 已有。翻译 TS + `replaylab_zh_CN.json` 补齐，`REBUILD_QRENDERDOC.cmd` 增加翻译同步步骤与 `SolutionDir` 引号修正。
10. **路线记忆特征**补充 `createHandle/createHandleFromBinding` 动态下标识别（今天 RenoDX 仍被先试的原因）。

验证（云容器 Py3.11）：Host 相关 233 unittest 通过（`test_project_restore_client` 仍是那 3 个既有失败）；Agent：`test_agent_acp_session` 19、`test_native_engine` 5、`test_skills` 23（1 个 `sys.frozen` 用例是 shim 限制）、`test_task_mcp_proxy` 10（需真实解释器路径）、engine_config/task_report/cli 全部通过；FastMCP 建 147 工具、policy 清单一致。**qrenderdoc C++ 未编译**——需 `REBUILD_QRENDERDOC.cmd`。

下一步：`REBUILD_HOST_AND_BRIDGE.cmd` → `REBUILD_QRENDERDOC.cmd` → 重启 qrenderdoc → EID 11402 复测（观察 `recoveryHandoff`、`resumed`、`partialCatalog`、chips、"1?" 不再分析）。仍待办：常驻 Host 统一（0t）。

## 0x. 2026-09-08 意图交给 AI + Skill/MCP 可配置 + 资产先索引后匹配（已实现，待冻结 + 复测）

用户四点：意图识别应由 AI 自己判断（给它技能/MCP 目录，让它决定是否加载）；设置面板恢复 skill/mcp 配置，支持 @ 加载；整理 shader 时必须等资产索引跑完再匹配，并把匹配写进工程；以上对所有连接（API 与 ACP）一视同仁。

1. **能力目录 + 按需加载（两条路线同一套）**：新增 `replaylab_agent/skill_gateway.py`（`SkillGateway.directory()/load()`，工具按家族分组 shader-workspace / unpacked-assets / shader-inspection / capture-analysis / resources / materials / project）。两个工具 `list_replaylab_capabilities`、`load_replaylab_skill` 由**任务级 MCP 过滤器自己**提供（`mcp_proxy.py` 本地处理，不转发 Host，不计工具预算、不触发任务报告门），因此 ACP（Claude/Cursor/Codex/Gemini/…）与 API/deepagents 的**子 agent**都能看到；API 主 agent 由 `deep_engine.capabilities_tool()/load_skill_tool()` 提供同样两个工具。`policy.AGENT_LOCAL_TOOLS` 声明它们无 effect，并排除在 Host 注册清单校验之外。
2. **删除硬编码意图**：`native_engine._select_skill` 只在用户显式选择（面板按钮 / `--skill` / `@skill:`）时预加载技能，其余一律不猜；`rank_skills` 只用于目录排序。提示词两处改写：`native_agent_system_prompt` 的 "Loaded Skill" 分支变成"你自己决定 + 目录 + 加载工具"，`deep_main_system_prompt` 的 Intent first 点名这两个工具。技能 `replaylab-core` v6 §5b 写入同一条规则。
3. **用户配置（设置面板 + @）**：新增 `agent_tools_config.py`（`%LOCALAPPDATA%/ReplayLab/config/agent-tools.json`：`skillRoots` / `disabledSkills` / `mcpServers`，逐项校验，坏条目丢弃并进 diagnostics，永不阻断任务）；CLI `replaylab-agenthost tools list|skill-enable|skill-disable|add-skill-root|remove-skill-root|mcp-set|mcp-remove|mcp-enable|mcp-disable`；`cli._catalog()` 按配置装载并过滤（`replaylab-core` 不可禁用）。qrenderdoc：`AIAssistant::createToolCatalogSection()` 在 设置 > AI 里加"技能与 MCP 服务器"分区（技能勾选框 + 添加/移除技能文件夹；MCP 列表 + 添加/编辑/移除，编辑对话框含 id/名称/命令/参数/环境变量）。聊天框输入 `@` 弹出选择器插入 `@skill:<id>` / `@mcp:<id>`；`mentionedSkillIds/mentionedMcpServerIds` 解析后转成 `--skill` / `--enable-mcp`（后者只对已配置但未启用的服务器生效，仅限该次请求）。
4. **外部 MCP 服务器接入**：ACP 侧 `ACPMainAgentSession.extra_mcp_servers` → `session/new` 追加声明 + Claude `.mcp.json` 追加条目；权限回调新增 `_user_mcp_server_for_permission`（只放行用户已启用服务器的工具，file/terminal 等非 MCP 类型仍拒绝）。API 侧 `ReplayLabMCPTools._attach_user_mcp_servers` 另开 stdio 会话，工具以 `<id>__<tool>` 并入工具表（子 agent 也可用），起不来的服务器记入 `failed_mcp_servers` 并在目录里标注，绝不拖垮抓帧工具。
5. **资产：先索引完成，再匹配，再写回工程**：新增 Host 工具 `wait_for_unpacked_asset_index(timeout_seconds=180)`（启动/加入后台扫描并阻塞等待，上限 30 分钟）；`_asset_index_state()` 统一给出 ready/indexing/failed/absent；`_shader_asset_gate()` 进入 `prepare/begin_shader_workspace_organization/get_shader_workspace_status` 的 `assetGate`；`retrieve_shader_asset_candidates` 默认 `require_complete_index=True`，索引未完成直接返回 gate 而不是拿半成品目录乱命名；`retrieve_*`/`submit_shader_asset_resolution` 新增 `workspace_path`，把候选与决定写进 `<workspace>/ai/asset-resolution.json`（合并写入，供新会话/中断后秒读，权威记忆仍在工程的 resolutions sqlite）；`get_active_unpacked_asset_index_status` 的 requiredAction 改为 `wait_for_unpacked_asset_index`。技能：workbench v23（步骤 2b 改为"3–5 步不依赖资产先跑，然后等索引完成再匹配"）、unpacked-asset-intelligence v7 新增 §0。
6. **工作区编辑器悬浮按钮**：`ShaderCodeEditor` 增加 `m_AddToChatButton`（圆角 13px 药丸，选中即出现在光标附近，滚动/取消选择自动跟随或隐藏），与 ShaderViewer 里的一致；右键菜单项保留。

验证（云容器 Py3.11）：Host 518 个单测通过（含新增 `test_shader_asset_gate` 5 个）；Agent：`test_skill_gateway`（新增 8）、`test_native_engine` 6、`test_task_mcp_proxy` 12（含新增两个本地工具用例，需真实解释器路径）、`test_agent_acp_session` 19、engine_config/task_report/cli/credentials 全绿；FastMCP 建 148 个工具、effect 清单与 Host 完全一致、`AGENT_LOCAL_TOOLS` 正确排除；TS 目录 4933 条全部已完成翻译（新增 26 条中文）。**qrenderdoc C++ 未编译。**

已知环境性失败（与本次无关）：`test_project_restore_client` 3 个 identity 用例、`test_skills` 的 `sys.frozen` 用例（shim 限制）、`test_subscription`/`test_external_connectors_optional` 需要 Windows 控制台或真实 pytest、`test_vendor_matrix`/`test_deep_engine` 需要 langchain/deepagents。

下一步：`REBUILD_HOST_AND_BRIDGE.cmd` → `REBUILD_QRENDERDOC.cmd` → 重启 qrenderdoc → 复测（"1?" 不应触发分析；EID 11402 应看到 `assetGate`、等待索引、`ai/asset-resolution.json`；设置 > AI 出现技能/MCP 分区；工作区选中代码出现悬浮按钮）。仍待办：常驻 Host 统一（0t）。

## 0y. 2026-09-08 48 分钟复盘：Host 被同步 sleep 卡死 + 导出 9 次重试（已修）

日志（`ai-tasks/ddcf993e…` 48.4 min / `ed214adc…` 12.5 min）读出的事实：

**A. 30 分钟是 Host 完全无响应，不是 AI 在思考。** 7–45 分钟之间每个 MCP 调用（包括 `ping`）都超时，只有 `list_replaylab_capabilities` 正常——那是 0w 新加的**代理本地工具**，根本不经过 Host。根因是我在 0w 加的 `wait_for_unpacked_asset_index` 是**同步函数里 `time.sleep(180)`**：FastMCP 1.x 对非协程工具直接在事件循环上调用，一个睡眠工具就锁死整台 Host。修复：改成 `async def` + `await asyncio.sleep`，单次上限 60 s（默认 20 s），"还没好"就返回让 Agent 再调；`ASSET_INDEX_FOREGROUND_BUDGET_SECONDS` 25 → 6。
**B. 等待的对象已经死了。** 索引 job 状态是 `running:false` 但目录 `complete:false`，`_asset_index_state` 仍报 `indexing` → 无限等。新增 `stalled` 状态（未在跑且未完成）：带上真实错误与 `requiredAction: index_active_unpacked_assets`，`wait_*` 立即返回，`retrieve_*` 也照此拒绝并给出正确动作。
**C. 索引为什么死的。** 扫完 191285 个文件、189824 条记录后，发布目录时 `os.replace` 撞上 WinError 5——AI 当时正在反复轮询 `get_active_unpacked_asset_index_status`，读端句柄占着目标文件。修复：`asset_catalog._replace_with_retry`（退避重试 ~5 s，覆盖 WinError 5/32）。
**D. 两个状态工具互相矛盾。** `get_active_unpacked_asset_index_status` 报 `ready:true`，新门禁报 `indexing`，AI 来回打转（status ×5、ping ×5）。现在状态工具统一走 `_asset_index_state()`，返回 `indexState`/`complete`，未完成时 `ready:false` 且给同一个 requiredAction。
**E. `index_active_unpacked_assets` 直接被 max_files 拒绝**（191285 > 200000 的检测路径），而后台扫描却照走。`DEFAULT_MAX_FILES` 提到 400 000（HARD 仍 500 000），错误信息改为报出实际数量与可行动作。

**F. 资源导出：9 次 finalize 失败，每次都重新 prepare 一遍整个 draw。** 报错是一次只报一个：POSITION 缺失 ×4、`textureMappings 需要 sourceResourceId`、`attributeMappings 需要 source/target/合法语义/唯一来源`。两处修复：
  1. `_transfer_spec_problems()` 一次列出全部问题，`finalize` 捕获首个异常后合并成一条错误，并明确写"捕获包仍然有效，不要重新 prepare"。
  2. **顶点通道由 Host 从数据推断**（`_channel_statistics` + `_infer_attribute_targets`）：SPIR-V 重建出来的流叫 `__replaylab_s0_b0_c0_x_f32`，名字里没有语义，于是按数值形状识别 POSITION（最大跨度的非单位长 3 分量流）、NORMAL（单位长）、TANGENT（单位长 + w=±1）、TEXCOORD（2 分量落在单位方块）、WEIGHTS（4 分量和为 1）、COLOR（0..1 归一化），每条决定带 `basis`/`confidence` 写进 `inferredAttributeTargets`，可用 `attributeMappings` 覆盖。POSITION 仍缺失时的错误改为列出所有观测到的流和一个可直接照抄的映射示例。
  3. 新增 **`obj` 目标**：Host 自己写 OBJ + MTL（位置/法线/一套 UV），不依赖外部转换器；`fbx` 仍需外部 provider，但错误里会建议 `obj`。

**G. 会话卡片。** `task_report.artifact_with_stats` 对目录产物列出内部文件（最大在前，最多 24 条，带字节数与 `childrenTruncated`）；`AIConversationView` 的产物区从一排 chip 改成一张卡片：标题「已编辑/已产出 N 个文件」+ 合计 `+新增 / -删除`，下面每个文件一行（点击打开，右键仍是打开/复制路径/在文件夹中显示），目录产物把内部文件缩进列出。

验证：Host 295 个相关单测通过（新增 `test_shader_asset_gate` 的 stalled/重试用例、`test_resource_export_channels` 8 个）；FastMCP 148 工具、清单一致；TS 4938 条全部已完成。**qrenderdoc C++ 未编译。**

仍待办（用户已提出，未实现）：纹理按 RenderDoc 原生支持的各种格式导出（PNG/DDS/EXR…）、FBX 通过检测到的 Blender 自动转换、着色器导出为 HLSL 函数 + 目标引擎模板、常驻 Host 统一（0t）。

## 0z. 2026-09-08 链路纠正：资产改为搜索优先，导出改为顶点着色器驱动 + 原样透传

用户要求先审视链路而不是继续打补丁。两条链路的形状都改了（用户在选择题里确认了两项推荐方案）。

**为什么会卡死（根因，不是表象）**：Host 是单线程事件循环，FastMCP 对非协程工具**直接在循环上调用**。0w 加的 `wait_for_unpacked_asset_index` 是同步函数里 `time.sleep(180)`，它一被调用整条队列就停摆，连 `ping` 都超时；唯一还能返回的是代理本地的 `list_replaylab_capabilities`（不经过 Host），日志里正是这个现象。**规则：Host 工具里永远不要同步 sleep；需要等待就写成 `async def` + `await asyncio.sleep`，并且单次上限 60 s。**

**A. 资产链路：搜索优先，索引降级为加速器**
- 新增三个只读工具，直接读配置的资产根，不需要任何目录、不需要扫描、不会等待：`search_unpacked_assets`（按名/通配/内容片段/子树/后缀，时间与条数双限，返回 `cursor` 续walk）、`list_unpacked_asset_directory`、`read_unpacked_asset`（返回 ASCII/UTF-16 字符串抽取，引擎包的引用名就在里面）。三者都做了越界防护（`..`、绝对路径、盘符一律拒绝）。
- `retrieve_shader_asset_candidates` 的 `require_complete_index` 默认改为 `False`；索引未完成/停摆时 `requiredAction` 一律指向 `search_unpacked_assets`，不再指向等待。`_shader_asset_gate` 同步改写。
- `wait_for_unpacked_asset_index` 保留但不再是任何路径的必经步骤。
- 技能：`renderdoc-unpacked-asset-intelligence` v9 §0 改写为"搜索优先"，`renderdoc-shader-workbench` v25 相应改写。

**B. 导出链路：顶点着色器驱动 + 原样透传**
真实失败链（从推理日志读出）：`inspect_resource_export` 响应太大读不出来 → AI 只能靠"故意报错"反推 transfer_spec 字段名 → 某个规格通过校验但在组装阶段失败后 **operation 被锁死在该规格**（`finalizeIntent`）→ 只能新建 operation 重新 prepare → 9 次。
- 删除 0y 加的数值统计推断（`_channel_statistics` / `_infer_attribute_targets`）——那正是"Host 硬编码猜语义"。
- 未映射的通道一律按捕获槽位透传成 `_ATTR<attributeIndex>`，附 `passthroughAttributes` 槽位表；POSITION 缺失时不再报错，而是取"顶点着色器确实读取的、分量≥3 的第一个流"并在收据里标 `assumed`（依据是槽位顺序与 `used` 标志，不是数值形状）。
- `finalizeIntent` 只有在**真正产出 target 之后**才具约束力；失败路径主动释放绑定 → 同一个捕获包可以反复修正规格重试。
- 新增 `get_resource_export_spec_template(operation_id)`：列出每个通道（槽位/名字/声明语义/格式/分量数/是否被 VS 读取/是否可解码）+ 可直接照抄的示例 + 规则；`prepare_resource_export` 的响应里直接带上它。所有导出工具响应改走 `_ok_compact`。
- 目标默认 `gltf + unity-urp + blender`（用户打开就有引擎侧材质脚手架）；`fbx` 由检测到的无头 Blender 转换（`_write_fbx`，收据记录可执行文件、退出码、sha256；没有 Blender 就在收据里说明而不是失败）；`obj` 保留为显式选项并说明它会丢蒙皮/顶点色/多套 UV。
- 技能 `renderdoc-resource-export` v5 §0 重写。

**C. 编辑器为什么不能保存**：live-edit 会话冻结在 `(instanceId, captureSessionId, capturePath)`；捕获被重新加载后 sessionId 变化 → `require_current_session()` 拒绝一切保存，但面板仍显示可编辑。现在 live-edit 快照带 `sessionState`，`shader_console` 的 `autoApplyReady` 要求 `sessionState == "current"` 并透出 `liveEditSessionState`，面板在保存被拒时给出针对性提示（"这个编辑器属于一个已被重新加载的捕获会话…"）。**自动重新绑定尚未实现**。

验证：Host 328 个相关单测通过（新增 `test_unpacked_asset_search` 8 个；`test_resource_export_channels` 改为透传契约）；152 个工具、effect 清单一致；TS 4939 条全部已完成（新增 1 条 ReplayLabProjects 翻译并同步了 registry）。`test_shader_console` 系列在 Linux 下因固定 `C:/` 路径而失败，与本次无关。**qrenderdoc C++ 未编译。**

仍待办：纹理按 RenderDoc 原生格式导出（PNG/DDS/EXR…）、着色器导出为 HLSL 函数 + 引擎模板、live-edit 自动重绑、常驻 Host 统一（0t）。

## 1a. 2026-09-08 资产按类型智能匹配 / 顶点角色由 VS 判定 / 一次调用导出 / 自动重绑

1. **资产：先分类，再按内容匹配，再走关系链**。`search_unpacked_assets` 的每条命中和 `read_unpacked_asset` 都带 `kind`（texture/material/mesh/shader/animation/package…），由扩展名 + 头部类名字符串（Texture2D、MaterialInstanceConstant、StaticMesh、AnimSequence…）判定。新增 `find_unpacked_asset_relations(relative_path, direction=references|referenced-by|both)`：读出资产自身引用的名字并在树里定位，同时反查谁引用了它。技能 v10 写明四步——分类 → 按存活证据匹配（贴图像素/材质参数值/网格计数/着色器函数体，名字只作提示）→ 走关系链找到父材质 → 用其参数块与捕获 cbuffer 按偏移和数值对齐后改名，然后才读代码。
2. **顶点通道由顶点着色器判定**。新增 `analyze_vertex_input_roles(event_id)`：逐输入寄存器返回读取它的源码行 + 命中的信号（normalize / cross / float3x3 基 / 乘世界矩阵 / 到达 SV_Position / 蒙皮）。位置本来就由输入布局声明；法线与切线按向量运算判定（单位化→法线或切线；参与叉积或构成 TBN→法线+切线；w 只有 ±1 的四分量→切线）。Host 不再做任何数值统计推断。
3. **一次调用导出**。新增 `export_draw_resources(event_id, position_attribute, normal_attribute=None, tangent_attribute=None, extra_mappings=None, formats=None, texture_formats=None, ...)`：内部完成 prepare + finalize，失败时返回同一个 `operation_id` 与模板提示，捕获包保持有效可继续修正。两步式工具保留给需要中途检查的场景。
4. **格式**。`obj` 彻底移除（丢蒙皮/顶点色/多套 UV）；目标为 `gltf`（证据格式，始终产出）+ `unity-urp` + `blender` 脚手架，`fbx` 由检测到的无头 Blender 转换并留收据。纹理：DDS 仍是精确证据（全 mip/全 slice），另外按 `texture_formats`（默认 `auto`）写可读副本——LDR 出 TGA + PNG，浮点出 EXR；bridge 侧 `_save_texture_copy` 走 RenderDoc `SaveTexture` 的 FileType，单图格式只含 mip0/slice0 并在收据里标注。
5. **live-edit 自动重绑**。`_shader_capture_session_state`：同一 qrenderdoc 实例、同一 `capturePath`、只是 `captureSessionId` 变了（同一个 rdc 被重新加载）→ 直接采纳新会话 id 并继续，不再判定 `expired`；`apply_candidate` 从 binding 读会话 id。capturePath 变了或实例死了才过期。持久化 binding 仍严格保持三个键。

验证：Host 相关 243 + 151 个单测通过；155 个工具、effect 清单一致。**qrenderdoc C++ 未编译。**
仍待办：着色器导出为 HLSL 函数 + 引擎模板；把 glTF 的 images 直接指向导出的 PNG；常驻 Host 统一（0t）。

## 1b. 2026-09-08 解包资产按容器读取（不再看扩展名）+ 可注册解读器

用户指出 1a 的分类是按扩展名做的，而解包后的 UE 工程里**所有东西都是 `.uasset`**（已确认：`E:\SilverPalace_Unpacked/raw/SilverPalace/Content/**`，全是 uasset/uexp/ubulk），扩展名只说明"这是引擎包"。修正：

- `_read_unpacked_asset_description()` 取代扩展名表，按容器分派：`.uasset/.umap/.uexp` 走**已有的** `symbols/engines/unreal_package.py`（Host 的资产目录本来就在用它：读 summary/names/imports/exports、主类名、被引用对象路径、纹理平台数据），独立文件走 magic（DDS/PNG/JPEG/EXR/HDR/FBX/glTF/UnityFS/zip），文本类走后缀，其余标记 `kind:"unknown"` 并提示注册解读器。
- 新工具 `describe_unpacked_asset(relative_path, deep=False)`：返回 reader、engine、className、kind、exports、references（含 `objectPath` 与 class）。`search_unpacked_assets` 新增 `kinds=[...]` 与 `identify`（默认开），按**读出来的类型**过滤。`read_unpacked_asset` / `find_unpacked_asset_relations` 也改走同一读取。
- **用户可注册解读器**：新增 `mcp_host/src/asset_readers.py`（`%LOCALAPPDATA%/ReplayLab/config/asset-readers.json`）。一条注册 = 一个命令：拿一个文件路径、在 stdout 打印一个 JSON 描述（kind/className/references/metadata）。用 `patterns` 和/或 `magic` 声明它认领哪些文件，`timeoutSeconds` 1–300。这样 CUE4Parse/FModel/umodel/AssetStudio 这类现成工具是**被调用**而不是被重写。新工具 `list_unpacked_asset_readers()` 报告内置 + 已注册 + 注册契约。
- 真实数据验证（用户的包）：`MI_Paraffin_01_A.uasset` → material / MaterialInstanceConstant，并读出 parent-material `MIT_BossPBR_Emissive_TwoSide` 与 4 张贴图的完整对象路径；`T_ParaffinLighter_01_D.uasset` → texture / Texture2D；`SM_AuroraPanel.uasset` → mesh / StaticMesh。
- 技能 `renderdoc-unpacked-asset-intelligence` v11 第 1 步改写为"读容器，不看文件名"。

验证：Host 252 个相关单测通过（`test_unpacked_asset_search` 17 个，含用真实 UE 包字节构造的解析用例、未知容器提示、注册解读器只被问它认领的文件、注册校验）；157 个工具、effect 清单一致。
待办：qrenderdoc 设置面板里加"资产解读器"分区（目前只能改 JSON）；着色器导出为 HLSL 函数 + 引擎模板；glTF images 指向导出的 PNG。

## 1c. 2026-09-08 解析器验证与项目记忆 + 设置面板并排三栏 + AI 自行配置

用户要求：内置各引擎解析器；解析前先拿真实资产验证，全部失败就**直说没有解析器可用**并请用户提供或跳过解包匹配；把选中的解析器写进项目配置（该项目下的 rdc 共享）并优先查配置；设置面板里 skill 要能查看编辑、mcp 能开关并测试连接、资产解读器要能添加且让 AI 用得上；AI 自己也要能配置这些（给一个目录和要求就能设置好，包括加 skill）。

### Host（`replaylab/mcp_host`）

- `verify_unpacked_asset_readers(sample_size=12)`：按树形结构判定引擎 → 跨目录抽样真实文件 → 内置解析器与已注册解读器逐个试读并计数 → 胜出者写入项目资产库旁的 `asset-reader-selection.json`（`ASSET_READER_SELECTION_FILENAME`），同项目的每个 rdc、每个后续任务直接读它。`state:"ready"` 带 `selectedReader`；`state:"no-reader"` 带 `requiredAction:"register_asset_reader_or_skip"`、`presets`（uex/AssetStudio/UnityPy/umodel）和新增的 `mcpServerPresets`（uex 的 `uex.exe mcp` 模式——对 UE 来说这比逐文件读更划算，服务器把索引常驻，不必每个文件重新挂载 pak）。
- `_shader_asset_gate` 在没有已验证解读器时返回 `verify_unpacked_asset_readers`（`readerUnverified: True`），而不是直接进匹配。
- `asset_readers.py` 的 uex 预设改成真实命令行：`uex.exe --config profiles.json --profile <name> preview "<虚拟路径>"` —— `preview` 正是"一个资产 → 一个 JSON 到 stdout"，与解读器契约完全一致；注意它按**虚拟包路径**寻址，profile 要指向 pak/IoStore 集合而非散装目录。
- `update_replaylab_configuration(..., write_skills=[{id, content, replace}])`：把整份 `SKILL.md` 写进用户自己的技能目录（`%LOCALAPPDATA%/ReplayLab/config/skills/<id>/SKILL.md`），登记该目录并启用该技能；**从不写安装包目录**，所以升级不会抹掉它；覆盖已有技能必须显式 `replace`。仍然要求 `user_confirmation` 引用用户原话。

### Agent CLI（`replaylab/src/replaylab_agent`）

设置面板通过 `agenthost tools …` 读写配置，本批新增：

- `skill-show <id>` / `skill-write <id> --content-stdin|--content-file` / `skill-new <id> [--title]`。编辑随包安装的技能时**写时复制**：内容写入用户覆盖目录、该目录自动登记为技能根，此后加载的是用户副本，安装包原件不动。
- `mcp-test <id> [--timeout]`：复用 `acp_session._diagnose_local_mcp_route`，真启动一次服务器并报告 initialize + tools/list 的工具数与耗时；起不来时返回 `ok:false` 与原因，而不是让命令失败。
- `reader-list` / `reader-set --reader-json|--json-stdin` / `reader-remove` / `reader-enable` / `reader-disable`，配合新模块 `agent_asset_readers.py`（与 Host 的 `asset_readers.py` 共用同一个 `asset-readers.json`）。
- `agent_tools_config.user_skill_overlay_root()`：用户技能覆盖目录。

Agent 与 Host 各有一份解读器校验实现（打包方式不同，不能互相 import），`replaylab/tests/test_asset_reader_contract.py` 用同一组样本把两者钉死：接受集、拒绝集、写入后另一侧能读出、预设列表逐项相等、默认路径相同。面板接受而 Host 丢弃的注册＝用户以为配好了、AI 却永远用不上，这条测试就是防这个。

### qrenderdoc 设置面板（`Windows/AI/AIAssistant.cpp`）

`createToolCatalogSection` 改为并排三栏：**技能**（勾选启用 / 查看·编辑 / 新建 / 添加文件夹 / 移除文件夹）、**MCP 服务器**（勾选启用 / 添加 / 编辑 / 移除 / **测试**，结果写在状态行）、**资产解读器**（勾选启用 / 添加 / 编辑 / 移除）。解读器编辑对话框带预设下拉（选中即填好 id/命令/参数/匹配式/备注），并把契约写在对话框里。技能查看编辑用等宽编辑器展示整份 `SKILL.md`，随包安装的技能会说明"保存会写到你的副本 %2，此后加载它，升级不会覆盖"。

两处新增私有方法 `showSkillDocument` / `presentSkillDocument` / `editAssetReader`。**注意**：`showSkillDocument` 拿到 CLI 结果后用 `QTimer::singleShot(0, …)` 把弹窗推迟到下一轮事件循环再开——`runCLIAsync` 的 `finish` 闭包由 QProcess 的连接持有，在回调里开模态窗会在它未展开时进入嵌套事件循环，`deleteLater` 触发后可能把正在执行的 `std::function` 释放掉。任何在 CLI 回调里开窗的新代码都要照此处理。

### i18n

新增 33 条 AIAssistant 源串并给出简体中文译文（`qrenderdoc/Translations/qrenderdoc_zh_CN.ts`）。裸文本门禁此前对本轮未提交的 AI 面板代码有 121 条新增告警（`createToolCatalogSection` 整块在 HEAD 里并不存在，基线自然没有收录）：已逐行补 `// i18n-ignore: …` 具体理由（协议字段名、CLI 子命令、结构化 HTML、Windows shell 名、composer @ 语法），现在 `lint_ts.py --require-complete --source-baseline …` 输出 4972 条全部完成、**0 new**。用户仍需在 Windows 上跑 `update_translations.ps1 -QtBin <Qt5.15 bin>` 让 lupdate 回填 location 并生成 `.qm`。

### 验证

- Host：`test_asset_reader_registry`（含 3 条新的 write_skills 用例）、`test_shader_asset_gate`、`test_unpacked_asset_search`、`test_resource_export_channels`、`test_integration_manifest` 共 49 个通过；`test_shader_asset_retrieval` / `test_asset_catalog` / `test_active_project_mcp` / `test_bridge_resource_export` / `test_replaylab_host` / `test_internal_host` / `test_shader_workspace` 共 222 个通过（1 skip）。**160 个工具，effect 清单一致。**
- Agent：`test_cli_tools_settings`（8 个新用例）+ `test_asset_reader_contract`（7 个新用例）+ `test_skill_gateway` 共 23 个通过；`test_skills` / `test_catalog` / `test_agent_standards` 60 个通过（`test_runtime_packaging_embeds_the_canonical_workflow_skill_sources` 在容器里因缺 `packaging/` 目录失败，与本次无关）。
- 技能：`replaylab-core` v7 新增 5c 节（配置工作区本身也是工作）、`renderdoc-unpacked-asset-intelligence` v12 新增第 0 步（先验证解读器；`no-reader` 就直说并给两个选择，绝不按扩展名猜）。
- **qrenderdoc C++ 仍未编译**——本会话的执行环境跑不了 MSVC。需要用户依次执行 `REBUILD_HOST_AND_BRIDGE.cmd` → `REBUILD_QRENDERDOC.cmd`。

### 关于 uex 与其他引擎解析器（回答用户提问）

uex（https://github.com/Straw1997/uex）已经把 UE 这条线覆盖得很完整：多 UE 版本 profile、自动挑版本/密钥组合、StaticMesh/SkeletalMesh/Anim/Material/MaterialInstance/MaterialFunction/Texture/Sound/Niagara/World/Blueprint、`preview` 单资产 JSON、`export` FModel 风格 JSON+PNG、`export-blender` 骨骼网格包、`asset-report` 分布统计，还有 `mcp` 与 `serve` 两种服务器模式。**UE 侧不需要再补**；接进来的正确方式是两条并行：解读器（`preview`，逐文件）+ MCP 服务器（`uex.exe mcp`，索引常驻、能查关系和指标）。

还缺的是非 UE 引擎：

- **Unity**：Host 已内置 `symbols/engines/unity_serialized.py`（SerializedFile 头、字节序、class id → Texture2D/Mesh/Material/AnimationClip/SkinnedMeshRenderer…，UnityFS/UnityWeb/UnityRaw/UnityArchive 包体识别），够做"这是什么"的分类；要取到像素、顶点和材质参数值仍需 AssetStudio（CLI）或几十行 UnityPy 脚本，两者都已在预设里。
- **Frostbite / RE Engine / Source 2 / CryEngine / Godot / 自研引擎**：不打算内置。这些格式随版本改动大且多为逆向产物，内置解析器会长期落后于社区工具，还会给出"看起来能读其实读错"的结果——比没有解读器更糟。走同一条注册路径：用户提供命令，`verify_unpacked_asset_readers` 用真实文件证明它能读，证明不了就如实告诉用户。这也是本批把"验证"而不是"内置更多解析器"作为主线的原因。

## 1d. 2026-09-08 设置面板改标签页 + 解读器"只给路径" + 技能可删除（修 bug）

用户反馈：三栏挤在一起应改成标签页；双击要能打开编辑；内置的解读器/MCP 要显示出来；每一项都要记录用途，让 AI 能检索并自行选择加载；解读器要能自动配置好已装的；配置要参考成熟 AI 的做法精简（截图为 Claude 的 Create a skill / 连接至自定义 MCP）；解包工具应只填程序入口、由 AI 自行识别调用（"类似一个 MCP 接口"）；**新建的测试技能删不掉**。

### 修的 bug：技能删不掉

`removeFolder` 用 `QFileInfo(path).dir().absolutePath()` 推登记根，对 `…/config/skills/fg/SKILL.md` 得到 `…/config/skills/fg`，而登记的是父目录 `…/config/skills` —— `remove-skill-root` 匹配不到，静默失败。两处修正：

1. `describe_skills` 新增 `root`（由 `_owning_root()` 在**配置里登记的根**中查找，不再由路径推导）和 `removable`，UI 直接用它。
2. 新增 `tools skill-remove <id>`：只删这一个技能的目录，拒绝随包安装的技能（"关掉它，文件归安装程序管"），删完若覆盖根空了就顺手取消登记。移除文件夹会带走整个目录下的技能，本来就不该是删单个技能的路径。

### 解读器：注册只需要一个路径

- `asset_readers.py` 新增 `KNOWN_TOOLS`（按 exe 名认 uex/AssetStudio/umodel/FModel/QuickBMS，各带调用模板、引擎、用途）、`GENERIC_INVOCATIONS`、`candidate_invocations()`（认识的模板 → help 里广告的子命令 → 通用形状）、`discovery_locations()` / `discover_installed_readers()`（PATH、%LOCALAPPDATA%、Program Files、Downloads 浅扫）、`BUILTIN_READERS`（内置覆盖范围 + 各自的局限，面板和 AI 读同一份）。
- Host 新增 `discover_asset_readers()`（只报告，不登记）与 **`probe_asset_reader(command, args=None, name="", profile="")`**：抽本项目 3 个真实资产，逐个候选形状实跑，第一个「退出码 0 且打印 JSON 对象」的即胜出，返回**完整可注册项**（命令、参数、实际解析成功的后缀作为 patterns）。跑不通则返回 `needs-arguments` + `helpText`，让 AI 读帮助自己提参数。这就是"类似 MCP 接口"的落法：给入口，其余自己认。
- Agent 侧 `registration_from_command()` + `tools reader-add <command> [--name] [--description]`：认识的工具直接补全调用与 patterns；不认识的按 `{path}` 存下并标记未验证，交给下次任务里的 `probe_asset_reader` 证明。面板的"添加"对话框因此只有**程序 / 名称 / 说明**三格，其余收进可折叠的"我自己指定调用方式"。
- `tools reader-list` 增加 `builtins`，`tools list` 增加 `mcpPresets`；新增 `tools reader-discover`。

### 能力目录

`get_replaylab_configuration()` 新增 `capabilities`：技能、MCP 服务器、内置与自建解读器各一行，统一带 `kind/id/title/description/enabled/source/loadWith`。AI 检索一份表就能决定加载什么，不用在三个字段名不同的列表之间猜。`replaylab-core` v8 指到这里。

### 面板

`createToolCatalogSection` 改为 `QTabWidget` 三页（技能 / MCP 服务器 / 资产解读器），每页一个 `QTreeWidget`（名称 / 来源 / 用途），**双击打开编辑**，内置项以"内置"来源列出且不可勾选、不可删除。技能页按钮：编辑 / 新建 / **删除** / 添加文件夹 / 移除文件夹。MCP 页：添加 / 编辑 / 移除 / 测试，下方按 `mcpPresets` 生成"添加 uex MCP 服务器"一键按钮。解读器页：添加 / **自动检测** / 编辑 / 移除。

对话框按用户给的参考精简：**MCP 不再要求填 Id**（`stableConfigurationId()` 由名称派生，编辑时保持不变），字段收敛为 名称 / 命令(带浏览) / 参数 / 说明 / 环境变量(可选)。新增成员 `editMcpServer`、`detectAssetReaders`、`presentDetectedAssetReaders`、`runConfigurationEdit`（统一跑一次配置写入、失败弹原因、成功刷新列表），以及 `m_ToolCatalogReload`。

**沿用 1c 的那条纪律**：任何在 `runCLIAsync` 回调里开窗的地方都先 `QTimer::singleShot(0, …)`，`detectAssetReaders` 同样如此。

### 没做的一件事（明确取舍）

参考截图里的"流式 HTTP" MCP **没有做**。`validate_mcp_server` 虽然接受 `transport: "http"`，但 `deep_engine.py:247` 和 `acp_session.py:318` 在运行时会跳过非 stdio 的服务器（后者还会记一条 "this ACP SDK has no HTTP transport"）。把它放进 UI 只会让用户配出一个永远不会被调用的服务器。对话框里改为写明"目前尚不支持通过 HTTP 访问的服务器"。要真支持，得先在 ACP/deep engine 两条路由上接 HTTP 传输。

### 验证

- Host：272 个单测通过（1 skip），含 `ReaderProbeTests` 4 个新用例（形状证明成、证明不了就要参数、找不到程序、发现已装工具）；**162 个工具**、effect 清单一致（新增 `probe_asset_reader` 归 `asset` 效果、`discover_asset_readers` 归只读）。
- Agent：70 个通过，含 `skill-remove` 三个用例（只删这一个 / 邻居和登记根还在 / 随包技能拒删）、`reader-add` 四个用例（认识的只要路径、不认识的存未验证形状、面板拿到内置与推荐、检测标记已登记），以及契约测试新增的 `BUILTIN_READERS`/`KNOWN_TOOLS`/`discover_installed_readers` 逐项比对。
- i18n：新增 44 条译文，`lint_ts --require-complete` 5013 条全部完成、裸文本 0 new。**注意**：修重复项时把 `<source>` 里的裸引号统一转义成了 `&apos;/&quot;`（lupdate 自己也是这么写的），XML 等价，但 diff 会显得很大；下次 `update_translations.ps1` 会把整份文件写回 lupdate 的规范形式。
- **qrenderdoc C++ 未编译。**

## 1e. 2026-09-08 工具模型统一：MCP 与命令行合成一份清单，接口由 AI 发现并写回

用户的判断是对的：解读器那套是我们自己发明的土协议，而同一个工具（uex）既有 MCP 又有命令行，却要在两个面板里各登记一次。这批把两者合成一个"工具"概念。

### 一个概念，三种连接

`%LOCALAPPDATA%/ReplayLab/config/tools.json`（`mcp_host/src/tool_providers.py`，schema `replaylab-tools/v1`）：

```
{ id, title, description, enabled,
  roles: ["asset-reader" | "general"],
  connection: {kind: "mcp-stdio" | "mcp-http" | "cli", …},
  interface: {source, discoveredAt, operations: [{role, kind, name|argv, patterns, proved}]} }
```

**人只填 connection**。`interface` 一律由发现写回，任何 `proved` 为空的 operation 都不会被使用——猜出来的调用形状对每个资产都报 unknown，用户还查不出原因。`upsert_tool` 在 connection 未变时保留已发现的接口，connection 变了就丢弃（那份接口属于旧的那个程序）。

`http://` 只允许 localhost：明文 HTTP 上带 bearer token 是不能接受的。token 只存环境变量名（`bearerEnv`），不写进配置文件。

### Host 变成 MCP 客户端

新增 `mcp_host/src/mcp_tool_client.py`：每个工具一条常驻会话（`AsyncExitStack`，按 connection 指纹失效），stdio 与 streamable HTTP 都走它。`tools/list` 是带类型的自描述回答，所以 MCP 工具**完全不需要猜接口**；而且识别一万个资产不再是启动一万个进程。

**事件循环纪律**（就是当初卡死 30 分钟那条）：`_read_unpacked_asset_description` 及六个会走到外部读取的工具全部改为 `async`——`search_unpacked_assets` / `describe_unpacked_asset` / `read_unpacked_asset` / `find_unpacked_asset_relations` / `verify_unpacked_asset_readers` / `discover_tool_interface`。命令行读取用 `asyncio.to_thread` 推到工作线程，否则每个文件都会把 Host 的事件循环卡住一次。同步的 `_read_builtin_asset_description` 单独拆出来，只做本地解析，兼容 shim 用它。

### 接口发现

`discover_tool_interface(tool_id, sample_size, extra_argv)`：
- MCP → `tools/list`，用 `_ranked_asset_reader_candidates` 按名字/描述/参数形状排序（preview/describe/inspect 加分，export/scan/batch 减分，必须有 path 类参数），再拿本项目真实资产逐个试调，成功的写回。
- CLI → 读 `--help`，按"认识的模板 → help 里广告的子命令 → 通用形状"逐个实跑，第一个退出码 0 且打印 JSON 对象的胜出。
- 都不成 → `no-operation` + `helpText`，让 AI 读帮助自己提 `extra_argv`。

`verify_unpacked_asset_readers` 现在会先给"已登记但没接口"的工具跑一次发现，再统计谁读得最多；`no-reader` 时同时给出 `builtInCoverage`、`installed`（本机已装的提取器）、`presets` 和 `mcpServerPresets`。

新工具：`list_replaylab_tools`、`discover_tool_interface`、`test_tool_connection`、`discover_asset_readers`（**163 个工具**，effect 清单一致）。`update_replaylab_configuration` 新增 `upsert_tools` / `remove_tools`。

### Agent：两种传输都通了

- `deep_engine.py`：非 stdio 不再被跳过，HTTP 走 `streamablehttp_client`，`_http_headers` 从环境变量取 token。
- `acp_session.py`：`mcp_server_http_type` 现在带 headers（SDK 不接受时降级为不带并记一条日志）。
- `agent_tool_providers.py` 是 Host 那份的镜像（打包方式不同，不能互相 import），`tests/test_tool_provider_contract.py` 用同一组样本把两者钉死：接受集、拒绝集、写入后另一侧能读出、未验证的 operation 两边都不提供、connection 变更丢弃接口、proved 后自动获得 asset-reader 角色、runtime 形状、旧两份注册表的迁移。
- CLI：`tool-list` / `tool-add` / `tool-set` / `tool-remove` / `tool-enable` / `tool-disable` / `tool-test` / `tool-migrate`。`tool-add` 只要 `--kind` 加一个路径或 URL。
- 运行时把 `tools.json` 里 `general` 角色的 MCP 工具和旧 `mcpServers` 合并挂载，同 id 以旧的为准，避免迁移中途重复启动。

### 面板

两个标签页：**技能** | **工具**。工具页一棵树：名称 / 连接 / 用于 / 用途，内置解读器以"内置"行列出（悬停显示读不了什么），自建工具带勾选启用和"已验证 / 尚未验证"。按钮：添加 / 自动检测 / 编辑 / 移除 / 测试。添加对话框按连接方式切换字段：MCP(程序) 要命令+参数+环境变量，MCP(HTTP) 要 URL+Bearer 环境变量+标头，命令行只要程序路径——并在框里写明"其余由 RenderDoc AI 自行摸清"。

新增 **"让 RenderDoc AI 配置…"**：写一句需求，文本进入聊天输入框而不是直接发送——配置是用户的，发不发由他决定。

新成员：`editToolProvider`、`connectionSummary`、`connectionDetail`、`toolRoleSummary`、`askAIToConfigure`；`editMcpServer` / `editAssetReader` 已合并进 `editToolProvider`。

### 迁移

`migrate_legacy()` 把旧的 `asset-readers.json` 和 `agent-tools.json` 里的 `mcpServers` 折进 `tools.json`，只跑一次，旧文件不删（回滚不该让用户丢配置）。旧解读器的调用形状**按未验证导入**——旧注册表从来没验证过任何东西，第一次任务会重新发现。

### 验证

- Host **274** 个单测通过（1 skip），163 个工具、effect 清单一致。新增 `ToolInterfaceDiscoveryTests` 5 个（证明成/证明不了要参数/找不到程序/MCP 候选排序/发现已装工具），`test_unpacked_asset_search` 增加"未验证的 operation 绝不被使用"。
- Agent：`test_tool_provider_contract` 9 个 + `test_cli_tools_settings` 23 个全过；整套失败只剩 21 个，全部是容器环境缺 `langchain_core` / `packaging/` / 真实解释器所致（与改动无关，改动前同样失败）。
- 顺带修了一条**先前就存在**的失配：`test_qrenderdoc_ai_streaming_contract` 断言 `renderTaskReport(report, verification)`，而代码早已是三参数的 `(report, verification, true)`。改为断言它仍然把 verification 传进去，不再钉死参数个数。

### 没做的

命令行工具无法承担 `general` 角色（它不是 MCP 服务器，没有可枚举的操作）。这是有意的：`runtime_servers` 只挂 MCP 连接。

## 1f. 2026-09-09 绘制意图假设：先知道在画什么，再去检索和读代码

用户提出：做解包资源匹配和整理 shader 时，他可能会直接说明"这是丝袜渲染"；没说的时候，AI 也应该自己看网格、输入/输出 RT、画在屏幕哪个位置，先猜出这一笔大概在画什么，然后**用这个方向去驱动资源检索和算法识别**。

这条补的是两个流程共同的起点问题：二十万个包在没有猜测之前长得一模一样，shader 在不知道该期待哪个算法之前就是一堵算术墙。

### `characterize_draw(event_id, user_hint="", include_shader=True)`

证据全部来自这一笔绘制自己已经带着的东西，不新增捕获副作用：

- `inspect_event` → 颜色目标数量与格式（MRT≥3 → GBuffer；FLOAT → HDR；全 UNORM → LDR 输出）、深度目标、depth write/test/func、blend、cull、目标分辨率与帧分辨率之比（减半 → 后处理链）
- `get_post_vs_data` → **变换后顶点的 NDC 包围盒**。这比数顶点数可靠得多：三个顶点铺满裁剪空间是全屏 pass，同样三个顶点缩在角落就不是。给出 `ndcBounds`、`ndcCoverage`、`fullscreen`
- `get_bound_textures` → 数量、是否有 cubemap、是否有 LUT（3D 或 256×16 这类窄图）
- `analyze_vertex_input_roles` → 是否蒙皮、有无切线
- `get_current_shader_representation` → 代码里的算法标记（sheen/Charlie、SSS/curvature、Kajiya/shift、parallax/refract、ramp/SDF、dither、fresnel、unproject、history/velocity…）

### 知识表 `mcp_host/src/draw_intent.py`

18 条：shadow-depth、gbuffer、skin(SSS)、hair、**cloth（含丝袜/丝质/天鹅绒）**、eye、toon、outline、decal、particle、water、sky、ui、tonemap、bloom、AO、temporal、blit。每条带：

- `aliases`（中英双语，用户说"丝袜"能落到 cloth 这一行）
- `signals`（带权重的状态/代码信号）
- `assetNameTerms` / `parameterNames` / `codeTerms` / `algorithms` / `confirmWith`

丝袜那条给的算法方向是：Charlie/Ashikhmin sheen 叠在普通高光之上；**反向 Fresnel**——掠射角更不透明、正对时通透；thickness/opacity 遮罩驱动 alpha，常配高频平铺法线；dither/stipple alpha 以便通过深度预渲染。`confirmWith` 是"alpha 随 (1-NdotV) 上升而不是下降"。

### 三条纪律（写进工具、模块和技能）

1. **假设不是观察。** 结果带 `evidenceClass: "ai-interpretation"`，每条候选带 `because`（哪些信号支持它）和 `confirmWith`（什么能坐实它）。报告里也必须这样标注。
2. **用户的话压过一切推断。** `user_hint` 命中的条目直接 +100 分，`because` 首项写"stated by the user"；命中不了也照样把用户的原词塞进 `assetNameTerms` —— 没有任何表知道一个项目的私有命名。
3. **词表是提示，不是过滤器。** 是先试哪些词的顺序，不是只许用这些词；用它找不到不等于资产不存在。
4. 补充一条：**信号必须来自真正读到的事实**。没有记录到 blend 状态不等于"不透明"，空证据不产生任何假设（`MIN_SCORE = 2`，低于此报 `unknown` 并建议直接问用户）。

### 技能

- `renderdoc-shader-workbench` v26 新增 §0b「读之前先知道在看什么」：把 `algorithms` 当**阅读顺序**（期待"Charlie sheen + 反向 Fresnel"，一页算术就变成两个具名波瓣加一个 alpha 项）；`parameterNames` 是那块常量原本可能的名字，**按偏移和数值对齐后再改名——假设只提供候选，偏移做决定**；证据与假设冲突时明说并丢弃。
- `renderdoc-unpacked-asset-intelligence` v15 新增第 0b 步：`assetNameTerms` → `name_contains`，`parameterNames` → `text_contains`（cooked 材质把参数名以明文字符串带着，所以 `SheenColor` 能在文件名什么都没说的情况下找到材质），`algorithms` 用来判断候选是真的对还是只是名字像。

### 验证

Host **331** 个单测通过（1 skip），新增 `test_draw_intent` 17 个：假设带证据类别与理由、depth-only→阴影、全屏 float→byte→tonemap、front-cull+法线外扩→描边、sheen+掠射角不透明→布料、中文提示落到同一行、表里没有的词照样进检索、空证据报 unknown、全屏三角形与角落三角形的区分、透视除法、坏 event_id 被拒。**164 个工具**，effect 清单一致。

## 1g. 2026-09-09 绘制意图改为 AI 自行判断：证据（含图像）给它，结论由它写，可修订可反验

用户指出上一版是硬编码：一张带权重的打分表替 AI 做了判断。这跟他此前反对硬编码资产识别是同一个问题——**打分表是穿着"假设"外衣的分类器**：它看不见画面、不懂这个游戏，而且一旦它说了 GBuffer，后面的阅读就会往那个方向弯。

### 拆掉的

`draw_intent.py` 里的 `INTENTS` 权重表、`_score`、`characterize`、`code_signals` 全部删除，工具 `characterize_draw` 移除。领域知识没有丢，而是搬到**技能引用文件** `.agents/skills/renderdoc-shader-workbench/references/draw-intent-vocabulary.md`——AI 读它并自行判断，而不是代码替它判断。开头就写明"是起点不是分类法，一笔绘制可以同时是好几种、也可以一种都不是"，结尾写明"用户或 marker 给的私有命名压过本文所有词"。

### 换上的：证据工具

`collect_draw_intent_evidence(event_id, neighbours=6, capture_images=True)` **只给证据，不给结论**：

- **`neighbourhood`** —— 前后各若干 DC，带引擎自己写的 debug marker 与 markerPath。夹在 `ShadowDepth` 和 `BasePass` 之间的一笔，光靠这个就基本定位了；一串共享同一 marker 的绘制就是一个 pass。工具里明写"marker 的权重高于任何推断"。
- **`images`** —— 用 `capture_event_texture_evidence` 存下**本 EID 的输出 RT** 和 **上一个 EID 的同一 RT** 两张 PNG。两张图之差就是"这一笔在屏幕上画了什么、画在哪"，这正是用户说的网格覆盖显示要回答的问题，而且直接把图交给 AI 看，比让它读一堆状态推断可靠。输入纹理列出 resourceId 并提示用 `save_texture` 存出来看。
- **`state`** / **`footprint`** —— 纯测量：目标格式、深度/混合/剔除、以及变换后顶点真实覆盖的屏幕面积（NDC 包围盒）。

返回里**没有 hypotheses、没有 vocabulary**，`evidenceClass: "observed"`，`next` 直接要求 AI 看图看 marker 然后自己写。

### 记录、修订、反向验证

`record_draw_intent` / `get_draw_intent` / `verify_draw_intent`，落盘在项目资产库旁的 `draw-intents.json`。

**意图是自由文本**，没有枚举——"the stockings"、"second specular pass on hair"、"whatever writes velocity"、"不确定，看着像布料"都合法，含糊也合法，因为诚实的含糊好过自信的错标。

**标注关系**（用户要求）：每条记录带 `capturePath`、`eventIds`、`shaderFingerprint`、`targetSignature`、`markerPath`。

**重复性检测**（用户要求）三层：
1. 同一 shader 在另一个 EID 出现 → **并入已有记录的 `eventIds`**，不新建第二条。
2. 对同一作用域重新记录 → **修订**（`revision` +1，旧措辞存进 `supersededIntent`），不重复。查找以 shader 指纹优先、EID 次之，因为 EID 会漂而 shader 不会。
3. 反向验证的 check 按**归一化文本**去重：同一问题同样答案再问一次报 `repeated` 且不改动任何东西；换了答案才算新结果。

**反向验证**：`verify_draw_intent(event_id, checks=[{expected, outcome}])`，outcome 取 `found/absent/contradicted/inconclusive`。一条 `contradicted` 就把整条记录降为 `contradicted`，`next` 明确要求"在报告里说出来并记一条修正的意图"——**证据反了还留着标签，比没有标签更糟，因为下游会继续信它**。修订意图时旧 check 不继承（它们针对的是旧主张）。

置信度只有四档（`contradicted/working/corroborated/user-stated`），刻意粗糙：更细的刻度只会引来"0.6 和 0.7 有什么区别"的争论。

### 技能

- `renderdoc-shader-workbench` v27 §0b 重写：先 `get_draw_intent`（已记录的、尤其用户说过的，是起点，重推一遍是浪费）→ `collect_draw_intent_evidence` → **看图、看 marker** → 自己下判断并 `record_draw_intent` → **决定之后才读词表**（"先决定再找词有用；先挑一行再照着读 shader，是错标签活下来的方式"）→ 整理完成后 `verify_draw_intent` 反验，矛盾就修订并写进报告。
- `renderdoc-unpacked-asset-intelligence` v16 第 0b 步同步改写：用记录里的 `searchTerms` 驱动 `name_contains` 与 `text_contains`，`algorithms` 用来判断候选是真对还是名字像；确认的匹配与记录冲突时**修订记录，而不是把匹配掰过来迎合它**。

### 验证

Host **342** 个单测通过（1 skip），`test_draw_intent` 重写为 28 个：自由文本与空意图、作用域标注、同 shader 并入、改措辞算修订、用户来源、found 提升/contradicted 降级、同问题重复报 repeat、换答案不算重复、修订不继承旧 check、shader 指纹优先于 EID、跨 capture 不误命中、坏文件不抛异常、全屏与角落三角形、透视除法、邻域 marker、证据工具**不产出结论**、before/after 两张图、已有记录作为起点、录入读回往返、未记录先验证被拒、坏参数被拒。**167 个工具**，effect 清单一致（`collect_draw_intent_evidence`/`get_draw_intent` 只读，`record_draw_intent`/`verify_draw_intent` 归 asset 效果）。

## 1h. 2026-09-09 彻底去掉硬编码判断：Host 只给关系与处理，判断与取数都由 AI 发起

用户指出上一版仍有硬编码：`collect_draw_intent_evidence` 打包了一份"我认为重要"的证据（`_draw_intent_state` 挑 blendMode/cullMode/…，`_draw_intent_neighbourhood` 固定取前后 6 个 DC）。**"挑哪些字段重要"本身就是判断**，而且 AI 并没有要这些。

### 拆掉的

`collect_draw_intent_evidence`、`_draw_intent_state`、`_draw_intent_neighbourhood` 全部删除（222 行）。Host 不再打包任何证据。

### 换上的：关系

`get_event_resource_graph(event_id, depth=1, max_events_per_resource=12)` —— **只有关系**：

- 该事件绑定的每个资源，带 RenderDoc 自己的 usage 字符串（`ColorTarget` / `PS_Resource` / `DepthStencilTarget`…）
- 每个资源被哪些其他事件触及，按 EID 分成 `before` / `after`，带引擎自己的 actionName 与 markerPath

这就是读写链：谁生产了本事件消费的东西，谁消费了本事件生产的东西。一笔绘制的颜色目标随后被一个叫 `Tonemap` 的东西读取——光这一条关系就基本定位了它。`depth=2` 再走一跳。返回里 `limitations` 明写"before/after 是事件顺序，不是因果"，且**没有任何 hypotheses / vocabulary / 挑选过的 state**。

固定窗口（前后 6 个 DC）换成了资源关系，因为"相邻"是我的假设，"共享资源"是事实。

### 换上的：处理（为上下文预算服务）

`capture_draw_image(event_id, attachment_kind, crop_to_draw=True, margin=0.25, max_dimension=512)`：

- 用 post-VS 的 NDC 包围盒 × 目标尺寸算出**这一笔真正落在哪**，按 `margin`（自身尺寸的比例，小绘制自动获得更多周边）外扩，得到像素矩形
- 交给 bridge 裁剪并缩放到 `max_dimension`

bridge 侧新增 `_event_texture_evidence_reduce`，用 **PySide2 的 QImage**（qrenderdoc 自带，零新依赖）在**发布之前**处理临时 PNG —— 因此哈希校验、字节预算、硬链接原子发布这些既有保证全部仍然作用在真正留下的那个文件上。字节预算改为：先用 8× 的上限挡住大到不该打开的图，缩减后再用正式预算判定，这样一张 4K RT 也能以小图交付。receipt 里带 `reduction`（原尺寸、裁剪矩形、缩放后尺寸）。

`capture_event_texture_evidence` 增加 `crop` / `max_dimension` 两个参数，`_event_texture_crop` 校验"四个整像素，否则拒绝"。

### AI 主导 + 允许质疑

技能 `renderdoc-shader-workbench` v28 §0b 重写为"**按需取数**，不要打包"：`get_draw_intent` → `get_event_resource_graph` → `capture_draw_image`（**看图**，再要 `event_id-1` 的同一张对比）→ 需要状态时才 `inspect_event` / `get_pipeline_state` / `get_bound_textures`。明写"上下文有限，一张裁剪图加资源图通常就能定案"。

**用户提示引导方向但不停用判断**：技能里明确写了允许质疑并给了例句——"你说是丝袜，但这一笔写了三个目标而且完全不混合，这是不是整个角色的 base pass？"，并说明"安静地读错东西，比提出这个问题更糟"。资产技能 v17 同样写明：确认的匹配与用户提示冲突时，**告诉用户你发现了什么并发问，而不是自己默默选一个**。

### 验证

Host **359** 个单测通过（1 skip）。`test_draw_intent` 37 个，其中新增：生产者/消费者按事件顺序分开且带 usage 与 marker、图工具**不产出任何结论**且承认局限、depth=2 只沿绘制事件走一跳、NDC→像素矩形（**Y 轴翻转**）、margin 按自身尺寸外扩、矩形永不越界、已覆盖全屏则不裁剪、退化包围盒不裁剪、裁剪参数由绘制足迹算出并传对、`crop_to_draw=False` 保留整张、目标存不下时如实报告、crop 校验四个整像素。**168 个工具**，effect 清单一致（`get_event_resource_graph` 只读，`capture_draw_image` 与 `capture_event_texture_evidence` 同效果）。

### 遗留

`capture_draw_image` 只处理 RTV/DSV。输入纹理仍需 AI 自己用 `save_texture` 取，且那条路径没有裁剪缩放——想给输入图也上同样的预算控制，需要给 `save_texture` 接一条同样的 QImage 后处理。

## 1i. 2026-09-09 图像预算：输入纹理同样裁剪缩放，以及两条路线能不能看图的实测结论

用户问：该不该补输入纹理的缩略图，以及"把大图直接作为上下文发给 API 或 ACP"是否支持、效果是否不如先压缩裁剪。查了代码，结论明确。

### 实测：两条路线看图能力不同

| 路线 | 能看到保存下来的 PNG 吗 |
|---|---|
| ACP / 原生（Claude Code、Codex、Cursor） | **能**。agent 自带文件工具，给路径即可打开 |
| API / `deep_engine` | **不能**。`deep_engine._mcp_result_text` 只取工具结果里的 text 块，工具签名是 `-> str`；MCP 的 ImageContent 会被丢弃，而且这条路的 agent 没有读文件的工具 |

已有的图像通道是**用户在输入框附加**的图（`deep_engine` 把它编码成 base64 image 块，`native_engine` 走 `attachments` / `path_attachments`），那是首轮用户输入，不是任务中途工具取回的图。

这带来一个我上一批引入的真实缺陷：`capture_draw_image` 的 `next` 写着"打开文件看它"，在 API 路线上是让模型做它做不到的事——而模型很可能会**编造**一张没看过的图。已修正。

### 我的判断：压缩裁剪优于直接发大图

三条理由，与路线无关：

1. **厂商本来就会缩**。图片超过长边约 1568 像素会在服务端被降采样，所以发 4K 只是多花上传和延迟，模型看到的还是缩过的。
2. **代价按像素算**。图像 token 约等于 宽×高/750：512×512 约 350 token，一张全屏图约 1700+，两张图乘以多个 draw 很快就吃掉预算。另有单图约 5MB、单边 8000 像素的硬上限。
3. **裁剪增加信息密度，缩放摧毁它**。判断"这一笔在画什么"时，一张 512 像素的**绘制区域裁剪图**比一张全屏缩略图信息多得多——后者里目标可能只有 40 像素宽。

反面情形是细节问题（有没有 dither、是不是法线贴图、织物纹理），这时**原分辨率的小块裁剪**胜过两者。所以正确的控制是"先裁后缩，由 AI 决定"，也就是现有的形状。

### 做的

- bridge 的 `_event_texture_evidence_reduce` 更名为 `_reduce_saved_image` 并共享给 `_save_texture`；`save_texture` 新增 `crop` / `max_dimension`，只作用于单文件 2D 保存（cubemap 六文件不动），receipt 带 `reduction`。
- Host 新增 `capture_texture_image(resource_id, crop, max_dimension=512, mip, slice)`：面向 AI 的版本，不用自己编路径，写进临时目录。
- 两个图工具的 `next` 都加了诚实条款："若你的运行时打不开图片文件，就说出来并改用其他证据；**绝不描述你没看过的图**。"
- 技能 v29 写入"**裁剪优于缩放**"：把 4K 全帧缩到 512 会毁掉正好要回答问题的细节，而 512 的局部裁剪保留它；细节是问题时提高 `max_dimension`，布局是问题时压低它。并写明路线差异与"最贵的错误是编造一张渲染目标"。

### 验证

Host **309** 个单测通过（1 skip），`test_draw_intent` 42 个，新增 5 个：预算随保存一起下传、细节问题用裁剪配大 `max_dimension`、存不下如实报告、坏 resource_id 被拒、两个图工具都带诚实条款。**169 个工具**，effect 清单一致。

### 下一步（未做，需要设计）

让 API 路线也能看图，要改 `deep_engine`：`_mcp_result_text` 之外增加一条把 MCP ImageContent 转成框架多模态内容的路径（pydantic-ai 的 `BinaryContent` 或等价物），并把工具签名从 `-> str` 放宽。这依赖框架版本的支持，容器里没有 langchain 无法验证，所以没有半做。在那之前，API 路线上的图像证据应当如实报告为不可用。

## 1j. 2026-09-09 API 路线也能看图（图直接进工具结果）+ AI 路线梳理

用户要求：让 API 路线也能看图和其他格式的文件，并重新梳理 AI 路线，看看哪里可以优化；同时问"目前是 skill 来控制 AI 思考处理的方式?"。

### 先把 1i 的"下一步"做完：证实哪种图块能到达

不是猜的，是读 `.venv` 里已装版本的源码（langchain-anthropic 1.7.1 / langchain-core 1.6.1）：

- `langchain_core.tools.base._format_output` → `_normalize_message_content`：返回值是 list 且每个元素的 `type` 在 `TOOL_MESSAGE_BLOCK_TYPES`（含 `text` / `image`）里时，原样作为 `ToolMessage.content`，不字符串化。
- `langchain_anthropic.chat_models._merge_messages`：list 内容的 `ToolMessage` 变成 `HumanMessage([{type: tool_result, content: <原样>}])`。
- `_format_messages` 的块转换**只走顶层**，`tool_result.content` 里的嵌套块原样发出。

所以 **LangChain 自己的标准图块 `{"type":"image","base64":…}` 在工具结果里传不到位**（不会被翻译，发出去 Anthropic 会拒）；能用的是 Anthropic 原生形状 `{"type":"image","source":{"type":"base64",…}}`。OpenAI 的 chat / responses 两条 wire 都要求函数结果是文本，图塞不进去。

### 做的

1. **`src/replaylab_agent/tool_media.py`**（新）：`image_mode(protocol)` 是一张只有一行的表（`anthropic-messages` → `anthropic`）加安全默认；`collect_images(result)` 取 MCP 结果里的 ImageContent；`build_tool_result(text, images, protocol)` 在能内联时返回 Anthropic 原生块，否则返回**文本 + 一句明说**："N 张图产生了但没有显示……不要描述你没看过的图"。没有图时返回纯字符串，所以既有路线逐字节不变。
2. **`deep_engine._mcp_result_payload(result, protocol)`**：接在两处工具闭包（ReplayLab 路线 + 用户自己的 MCP 服务器）上，协议取自 `request.profile.provider.protocol`。工具返回类型从 `-> str` 放宽到 `-> Any`。错误结果仍然只走文本。
3. **Host `read_evidence_file(path, max_bytes, offset, inline_image)`**：读回 Host 自己产出的文件——文本按 offset/max_bytes 分段，图片整张内联，其他类型只给大小/摘要/类型不给字节。**只允许** Host 自己的证据目录（event-texture-evidence、texture-images 临时目录、当前项目库所在目录、当前 shader 工作区）；这不是通用文件读取器，docstring 里明写了。已登记进 `policy.READ_ONLY_TOOLS`。
4. **两个图工具直接把图放进结果**：`capture_draw_image` / `capture_texture_image` 改为 `@mcp.tool(structured_output=False) -> list[Any]`，返回 `[receipt, Image]`，新增 `inline`（默认 `True`）。共享助手 `_saved_image_result(summary, inline)` 在图太大（> `INLINE_IMAGE_BYTE_BUDGET` = 1.2 MB）、文件不在、或没要图时，把原因写进 receipt 的 `inlineImage` 字段——**不静默丢**。路径仍然返回，ACP 路线可以继续自己打开。
   - 这条比"再调一次 `read_evidence_file`"少一次往返，而且两条路线同一套调用。
5. **技能**：`replaylab-core` v10 新增 §2b"一张图只有真的看见了才算 observed"（三种情况：结果里就有 / `read_evidence_file` / 没到就如实说不可用）；`renderdoc-shader-workbench` v30 改写图像段落，`read_evidence_file` 降级为"给别的文件用"。
6. **子 agent 提示词**：`deep_child_system_prompt` 增加一句——它**可以**再 `load_replaylab_skill` 加载别的 Skill。这个能力一直存在（`_tools_for` 里 `| AGENT_LOCAL_TOOLS`），但提示词从没说过，等于白给。

### 梳理：AI 路线现在长什么样，"是不是 skill 在控制思考方式"

是，但准确说是**四层**，Skill 只是其中一层：

| 层 | 位置 | 决定什么 | 谁能改 | 进上下文的频率 |
|---|---|---|---|---|
| 系统提示 | `agent_prompts.py` | 角色、边界、委派方式、回答契约 | 只能改代码 | 每轮 |
| Skill | `.agents/skills/*/SKILL.md` | 程序、判断标准、领域词汇 | 用户可在设置面板改 | 常驻的每轮；其余按需加载一次 |
| 工具 docstring | `mcp_host/src/server.py` | 单个工具的契约、参数含义、下一步 | 只能改代码 | **每次列工具都在，最高频** |
| 权限与预算 | `policy.py` + `mcp_proxy.py` | 什么调得动、能调多少次、effect 天花板 | 连接的 effect 设置 | 不进上下文，直接拒 |

两条执行路线共用后两层：

- **API 路线**（`deep_engine` / deepagents）：主 agent 没有任何 Host 工具，只有 `task` / `ask_user` / `report_task_result` / 目录 / 加载技能；每个非常驻 Skill 生成一个 subagent，子 agent 的系统提示里内联常驻 Skill + 它自己那一个 Skill。工具经任务级 `mcp_proxy` 到 Host。
- **原生 / ACP 路线**（`acp_session` / `native_engine`）：Claude Code / Codex / Cursor 直接连同一个 `mcp_proxy`，没有主/子分层，Skill 全靠 `list_replaylab_capabilities` + `load_replaylab_skill` 自取。

### 梳理发现的问题与建议

1. **docstring 与 Skill 会漂移，且 docstring 更高频。** 今天就修了一处实例：两个图工具的 `next` 还写着"打开文件看它"，而 Skill 已经改了。原则应固定为——**docstring 只写这个工具自己的契约，跨工具的方法论只写在 Skill 里**；否则改一处忘一处，而模型信的是 docstring。
2. **`capture_event_texture_evidence` 与两个新图工具语义重叠。** 前者有 hash / 字节预算 / 证据链 receipt，是"取证"；后两者是"看一眼"。建议保留两者但在文档上分工写清，不要再让 AI 在两者之间猜。（未做，需要你点头。）
3. **subagent 数量随 Skill 线性增长。** 再加几个就该按 category 归并了。（未做。）
   *（1k 更正：`.agents/skills` 下确实有 10 个目录，但其中 5 个是空目录残留。真实技能只有 5 个——`replaylab-core` 常驻，另外 4 个各生成一个 subagent。打包脚本 `Build-ReplayLabRuntime.ps1` 也正好只打这 5 个，没有遗漏。）*
4. **主 agent 一个 Host 工具都没有**，所以"这个 EID 画了什么"这种小问题也要起一个子 agent。这是"证据只能来自子 agent 报告"这条契约的代价，我建议**保持现状**——但要知道成本在这里。
5. **绘制意图的闭环缺一个提醒。** `record_draw_intent` → `verify_draw_intent` 现在完全靠 Skill 文字提醒。可以在 report gate 上加一条**纯流程**检查：本次任务读过 `get_draw_intent` 却没有 `verify_draw_intent` 时，在报告里提示一句。它不判断内容，只提醒流程，因此不违反"不许硬编码判断"——但仍需你确认再做。（未做。）

### 验证

- 新增 `mcp_host/tests/test_evidence_file_reader.py`（8 个）：分段读文本、图片真的以图片回来（base64 与原字节相等）、超预算时改要更小的裁剪、可以只要事实不要图、非文本非图只描述、**证据目录之外一律拒**、相对路径与目录被拒、roots 就是 Host 自己写的那几个目录。
- 新增 `tests/test_tool_media.py`（13 个）：找图、bytes 被编码而非字符串化、不支持的媒体类型不上报、无图时逐字节不变、Anthropic 拿到嵌套原生形状、其他路线降级成明说、超预算同样降级、超数量的图被计数而非隐藏，外加三个直接打 `deep_engine._mcp_result_payload` 的接线测试。
- `test_draw_intent.py` 由 42 → 46：图直接进结果、可以只要路径、超预算要求更紧的裁剪、路径不存在时如实报告。全部调用点改为读 `blocks[0]`。
- 工具总数 **170**，`validate_registered_tool_effect_manifest` 对齐通过；`test_integration_manifest` / `test_server_composite_readonly` / `test_active_project_mcp` 全绿。
- 容器里跑全量套件有大量失败，逐个查过**全部是容器环境限制**，不是回归：`/usr/bin/python3` 是符号链接（`providers` 的"必须是常规文件"整组、`mcp_proxy` 子进程起不来）、`uuid_utils` / `jiter` 是 Windows 编译扩展（deepagents/langchain 整组）、WIC 与 codex 绝对路径是 Windows 专有。**Windows venv 上的套件仍需你在重建前跑一次。**

### 你要做的

1. `replaylab/.venv/Scripts/python.exe -X utf8 -m pytest mcp_host/tests tests`（真正的门禁）。
2. `REBUILD_HOST_AND_BRIDGE.cmd`。
3. 本轮没动 C++，i18n 不需要重跑；如果你要一起重建 qrenderdoc，流程照旧。

## 1k. 2026-09-09 任意格式的读取（按各家 API 真实能力）+ 去掉替 AI 做判断的地方

用户要求两件事：(1) API 与 ACP 都能读取图片、视频、GIF 等其他格式，除非模型本身不支持；(2)"我不应该过分地规范 AI 做一些事情，而是他自己控制做一些处理，我只是给他提供一些建议、流程和验收标准" —— 审视整个 AI 实现，确保灵活性。

### 各家 API 到底能在 tool result 里装什么（读源码，不是猜）

| 协议 | 文本 | 图片 | PDF / 文件 | 音频 | 视频 |
|---|---|---|---|---|---|
| `anthropic-messages` | ✔ | ✔ png/jpeg/gif/webp，嵌套 `{"type":"image","source":{"type":"base64",…}}` | ✔ `{"type":"document","source":{…}}`，`Base64PDFSourceParam` 只认 `application/pdf` | ✘ | ✘ |
| `openai-responses` | ✔ | ✔ 必须写成 chat 风格 `image_url`（langchain-openai 再转 `input_image`） | ✔ chat 风格 `file` → `input_file` | ✘ | ✘ |
| `openai-chat` | ✔ | ✘ | ✘ | ✘ | ✘ |

依据：
- `langchain_core.tools.base`：工具返回 list 时，**每一项**的 `type` 必须在 `TOOL_MESSAGE_BLOCK_TYPES`（`text` / `image_url` / `image` / `json` / `search_result` / `custom_tool_call_output` / `document` / `file`）里，否则整个 list 被 JSON 字符串化。所以 `input_image` 这种 Responses 原生形状**过不了这一层**，必须写 chat 风格。
- `langchain_anthropic._merge_messages` + `_format_messages`：嵌套在 `tool_result.content` 里的块原样发出，因此必须已经是 Anthropic 原生形状。
- `anthropic.types.ToolResultBlockParam.Content` 允许 text / image / document。
- `langchain_openai._ensure_valid_tool_message_content`：Responses 路线上，全部块属于 `input_text/input_image/input_file/text/image_url/file` 才保留，并由 `_convert_chat_completions_blocks_to_responses` 转形。
- `openai.types.chat.ChatCompletionToolMessageParam.content` 只接受字符串或文本块 —— Chat Completions 根本装不下图。

**视频与音频没有任何一条路线支持**，这就是"除非模型本身不支持"的那条线。不硬塞，而是如实说明并给出出路（见下）。

### 做的

1. **`tool_media.py` 重写**为能力表驱动：`PROTOCOL_MEDIA` 按协议 × 媒体种类列出**具体 media type**；`collect_media` 收集 MCP 结果里的 ImageContent / AudioContent / EmbeddedResource(blob)；`build_tool_result` 只内联这条连接真的能带的东西，其余在文本里逐条点名原因（"这条连接带不了 video"、"这条连接的 image 只收 png/jpeg/gif/webp，不是 image/tiff"、"超出预算"），并提示"可以用你注册过的工具转换"。
   - 预算从"最多 2 张图"改为**按字节**：单件 3.5 MB、整个结果 6 MB。数量上限是第二条会绊人的规则，字节才是真实成本。
2. **`read_evidence_file` 去掉扩展名白名单**。现在按**字节嗅探**判定类型（PNG/JPEG/GIF/WEBP/BMP/PDF/OggS/fLaC/ID3/RIFF-WAVE/ftyp 魔数，UTF-8 增量解码判定文本），名字只作兜底。返回最贴切的 MCP 内容块：文本→文本，栅格图→`Image`，音频→`Audio`，其余→`EmbeddedResource(BlobResourceContents)` 带真实 media type。**没有任何格式因为"不认识"被拒**。`inline_image` 更名 `inline`，默认 `max_bytes` 提到 3 MB。
   - 证据目录新增**已配置的解包资产根**：用户为这个项目选定的目录，其中的贴图和渲染目标在性质上没有区别，拒了只会把工作赶到更差的工具上。
3. **`capture_event_texture_evidence` 与两个看图工具的分工**写进 docstring：前者是取证（哈希、字节预算、可引用 receipt、精确子资源与色调范围），后者是"看一眼"。不再让 AI 在两者之间猜。

### 去掉替 AI 做判断的地方（第 2 件事）

1. **删掉 `agent_registry` 里的意图判断装置**：`_DOMAIN_SIGNAL_RE`（一个"这算不算抓帧工作"的正则）、`_CJK_TERM_HINTS`（中文关键词→英文技能词表）、`_strong_score`、`rank_skills(require_match=True)` 的过滤分支，全部删除。这套东西在生产代码里**从来没被调用过**（只有一个测试在用），却是 Host 在替 AI 判断意图，而且对表里没有的说法一律判错。
   - `rank_skills` 现在是**纯排序**：用请求自己的词在技能元数据里数命中，不带任何领域词表，**不过滤、不截断**，全部技能都列出。提示词里明说"这是排序不是推荐，而且经常是错的"。
2. **语言提示从命令改成信号**："Reply in Simplified Chinese." → "Their messages read as Simplified Chinese, so reply in that unless they have since switched."。启发式判断错了的时候，AI 能自己纠正。
3. **能力目录说清它是摘要**：新增 `toolFamiliesAreASummary`，说明 family 里列的是例子不是全部，连接自带的工具清单才是完整的 —— 免得 AI 以为某个 family 只有 8 个工具。
4. **`replaylab-core` v11 新增第 0 节"这些文档是什么，不是什么"**：明确分成两类 —— **边界**（effect 上限、证据标注、报告闸门、不得声称没做过的观察）少而硬；**其余全是建议、可行的流程、以及验收标准**，不是必须逐条执行的脚本。抓帧与技能假设不符时，走更好的路并在报告里说明偏离了哪一步、为什么；"技能里是这么写的"不构成一个结论。同时写明工具 docstring 与技能冲突时谁说了算（工具的契约归 docstring，方法归技能，真冲突就说出来）。
5. **子 agent 列表从"分区"改成"起点"**：主 agent 提示词说明子 agent 自己可以再加载技能，跨领域的任务交给它开始的那一个，而不是为了迁就列表把任务切开。子 agent 提示词上一轮已经补了"你可以加载别的技能"。
6. **`get_draw_intent` 的陈旧指引修掉**（还指着已删除的 `collect_draw_intent_evidence`），并新增 `checkCount` / `neverChecked` —— 这是**记录本身的属性**，不是流程规则：没人验证过的意图只是被重复了一遍的猜测，值不值得在意由读的人判断。上一轮提的"报告闸门强制反验"**没有做**，因为那正是用户说的过度规范。

### 验证

- `tests/test_tool_media.py` 22 个（原 13）：新增 EmbeddedResource 收集、音视频照样收集然后如实拒绝、能力表与各 SDK 对齐、Anthropic 的 document 块、OpenAI Responses 的 `image_url`/`file` 形状、未知协议不猜、按字节而非数量的预算（6 张小图全到）、总预算仍能刹住失控结果。
- `mcp_host/tests/test_evidence_file_reader.py` 11 个（原 8）：陌生格式（.pak）作为资源交出而不是被拒、PDF 带真实 media type、**字节决定类型而不是名字**（叫 .dat 的 PNG 仍是 PNG，.usf 与中文 .md 仍是文本）、超预算时给出选项而不是命令、根目录包含解包资产根、没有项目时只是少几个根。
- `tests/test_skills.py`：`require_match` 那个测试改写为"排序但绝不选择或隐藏"，并留下注释说明删掉的是什么、为什么。
- 工具总数 **170**，effect 清单对齐。
- 两套件在容器里跑完，**失败集合与改动前逐条相同**（agent 21 项、host 105 项），全部是容器环境限制：`/usr/bin/python3` 是符号链接、`uuid_utils`/`jiter` 是 Windows 编译扩展、`claude-agent-acp` 未安装、WIC 与 codex 路径是 Windows 专有。**Windows venv 上的套件仍需你跑一次。**

### 已知的、故意没做的

- **视频/音频无法内联**是各家 API 的限制，不是这里能修的。出路是让 AI 自己用注册过的程序（ffmpeg 之类）抽帧/转码 —— 这条写进了 core 与 workbench 技能，但**没有内置任何 ffmpeg 硬编码**。
- **报告闸门不强制反验**，见上。
- **`rank_skills` 的排序仍在**：它不带领域词表，只是把请求里的词在技能元数据里数一遍，纯粹为了可读性，并且已声明为"经常是错的"。

## 1l. 2026-09-09 跨 rdc 复用（身份 + 共享库 + 已适配表）、解包器按 MCP 形状声明、AI 设置四标签页

### 根因（现场数据，不是推理）

工作区打开 rdc 后为空，是因为 `captureId = hash(capturePath, api, [resourceId:stage:entryPoint:rawSha256])` 把 **RenderDoc 的 ResourceId** 哈希了进去，而它每次打开重放都重新分配。

把项目里 `captures/catalogs` 全扫了一遍，同一个 fragment `MainPS` 出现在 **`1.rdc` 和 `6.rdc` 两个抓帧文件**、八次导出里，ResourceId 是 747 / 754 / 853 / 860 / 3162 / 12212 六个值，而 `rawSha256` `3c4afb4349…`、`interfaceHash` `7bb7ee6d2f…`、`layoutHash` `c4b2add76c…`、`codeSha256` `2a80b22e6d…` 全部一字节未变。这是跨 rdc 的直接证据。

又读了 `symbols/scanner.py` 的 `_normalise_bindings`：只取 space、bind、arraySize、descriptorType、textureType、variableType 和布尔标志，**全部来自反射，无名字无 ResourceId**。签名与 cbuffer 布局同理。

### 做的（Host 178 个工具，effect 清单对齐）

1. **`stableCaptureId`**（bridge + Host）：`captureId` 去掉 ResourceId 的版本，重绑门禁改用它；use 证据的 `shaderKey` 也有去 ResourceId 的投影。**不需要迁移任何存档**——Host 在重绑时本来就要读工作区自己存的 catalog.json，改成当场重算。bridge 会话缓存加了一份 stable 授权对，`get_capture_status` 多报 `knownCatalogStablePairs`，旧 bridge 读不到时退回导出。
2. **`shader_reuse.py`**：三层身份。字节（`rawSha256`，**原始捕获字节，不是重编的**）→ ABI（`interfaceHash`，已现成且 name-free）→ 源码形状（逐函数指纹，标识符按首次出现替换成 id0/id1，关键字与内建保留）。`component_identity` 把 ABI 拆成 signature / binding / layout 三个分项，用**同一批 scanner 助手**算，所以分项与总摘要不可能矛盾。
   - **加权相似度**：真实数据上两个无关 shader 共享 10 个反编译样板函数，计数相似度 0.099；按 token 质量加权后 0.0065。加权那个才是能看的。
   - 修了我自己写的一个 bug：swizzle 字母被当成关键字，导致变量名叫 `a`/`r` 时形状对不上；改成只有跟在点号后才当 swizzle。
3. **`shared_shader_library.py`**：`<项目>/shared/shaders/<rawSha256>/entry.json`，两级散列目录，带来源，**默认合并不覆盖**（旧来源进 history），另有 index.json 供按 interfaceHash/stage/gameId 筛候选。
4. **已适配表** `shared/shaders/applied.json`：按抓帧路径分组、组内按 shader 字节索引。`plan_capture_shader_reuse` 一次把整个 capture 分成 alreadyApplied / exact / interface / unmatched；`record_shader_reuse_applied` 应用后记一笔。它是缓存不是权威。
5. **解包器 = MCP 形状的声明**（`tool_runner.py`）：每条用例有 `name` / `description` / `inputSchema`，AI 调用时看不出底下是 exe 还是服务器。argv 是带具名槽位的模板，**每个槽位必须是声明过的参数**（注册时校验），调用前按 schema 类型与枚举检查，未声明参数**拒绝而非忽略**，值里禁换行与空字节，argv 是列表、不过 shell、输出只落 Host 目录、带超时。
   - **单次正则替换**：原来"每参数 replace 一遍"会把先替进去的值里的 `{outputDir}` 再展开一次——正是要防的注入。测试抓到的。
   - `knownFailures`（失败也记）、`programSha256`（exe 升级后用例标记为需重验）、未验证的不上架。
   - **内置三个用同样的形状写死**（`BUILTIN_OPERATIONS`），带 description / purpose / limits / patterns / inputSchema，从一开始就是完整的。
6. **AI 设置改四标签页**（C++）：连接 / 技能 / MCP 服务 / 解包器。连接列表整体搬进标签栏（`createToolCatalogSection` 多收一个 `connectionsPage`）；工具按 connection kind 分流到 MCP 或解包器两棵树；按钮行移到标签下方共用一套（避免两份 handler），随标签显隐。新增 **"让 AI 自己搞定…"**：填样本目录/文件 + 用途说明 → **新开一个对话并自动发送**一段说明完整流程的提示词（探测 → 用真实文件试 → `record_asset_tool_operation` 记录成功与失败）。用 `QTimer::singleShot(0, ...)` 延后一拍，避开设置对话框还在关闭。

### 验证

- 新增/更新测试：`test_shader_reuse.py` **41**、`test_tool_runner.py` **26**、`test_bridge_catalog_rebind.py` 14（含把 853/860/12212 钉死的那组）、`test_shader_workspace_mcp.py` 34 全绿。工具清单 178，effect 对齐。
- 修了我引入的一个回归：新的严格 operation schema 要求 `name`，`discover_tool_interface` 构造的用例没有——现在它也生成完整声明。
- **C++ 在这里编不了**（容器没有 MSVC/Qt）。做了能做的静态检查：与改动前的原文件对比，花括号/圆括号/方括号收支**完全一致**；用到的 API（`RDDialog::getExistingDirectory`、`QTimer::singleShot`、`QLineEdit`、`createConversation`、`m_Send`）在本文件里都已有先例。首次编译若有报错，把报错发我。
- **i18n**：新增了一批 `tr()` 字符串，`REBUILD_QRENDERDOC.cmd` 会跑 lupdate + lrelease，构建不会被挡，但**新字符串在翻译好之前显示为英文**。跑完 `update_translations.ps1` 后把 .ts 里新增的未完成条目告诉我，我来补中文。

### 一次事故

写补丁时给 `open()` 传了错的 `newline` 参数，把 `mcp_host/src/server.py` 截断成 0 字节。设备上那份是当前的，已从那儿恢复并校验（838811 字节、语法正常）。临时文件移到了 `ai-tasks\_to_delete\`（挂载不允许删除），可以整个删掉。

## 1m. Test triage of 2026-09-10 (55 host + 5 agent failures)

Both suites were re-run in the Linux container against a captured baseline
(`105` host failures / `21` agent failures, all environment-caused: no Windows,
no d3dcompiler, no real provider executables). The container could finally run
`test_deep_engine.py` and `test_agent_verification.py` after standing in for the
three compiled wheels it cannot install (`uuid_utils`, `jiter`, `xxhash`) and
copying the two pure-python ones from the developer venv (`langsmith`,
`requests_toolbelt`). Those stand-ins live only in the container.

Diffing the new run against the baseline named every regression exactly:

* **5 agent failures - one cause.** `capabilities_tool()` and `load_skill_tool()
  returned bare coroutine functions. Everything that inspects the main agent's
  tool list by name (`tool.name`) broke on the first entry, which took down
  `ask_user`, the report gate and the sub-agent listing with it. Both now return
  `_langchain_tool(...)` like every other tool, and `test_deep_engine` asserts
  the full four-tool list rather than the two it predates.
* **7 `test_project_auto_restore` failures - the status-bar rewrite.** The test
  execs a chosen subset of bridge functions into a stub namespace, so the new
  `_report_shared_shader_reuse` had no stub and the `_show_project_background_status`
  stub did not accept the new `current`/`total` keywords. Both stubs were added;
  the malformed-progress test now also asserts the counts reach the status bar.
* **2 `test_unpacked_asset_search` failures - `validate_operation` got stricter.**
  Requiring `name` and rejecting every undeclared `argv` slot would have silently
  dropped tool registrations the user already has on disk. An operation recorded
  without a name is now given a derived one (`<role>_<argv digest>`), and `path`
  joins `program`/`outputDir` as a Host-supplied slot, because an asset reader
  never receives the file being described as a caller argument.

After the fixes both suites match their baselines exactly - no new failures, and
nothing that used to pass now fails.

**Still unexplained: 16 Windows-only host failures** (`test_shader_workspace_mcp`
15, `test_asset_catalog` 1). All 34 `test_shader_workspace_mcp` tests pass in the
container, so whatever fails is platform- or machine-state-dependent and cannot
be reproduced from the container. It needs a `--tb=long` run on the Windows box
before anything about it should be believed.

## 1n. The 16 Windows-only failures were the developer's own machine

The traceback named it immediately:

    create_shader_workspace refused: the active project 'x' owns Shader
    workspaces under D:\...\ReplayLab Project\workspaces\shaders, but
    C:\Users\...\Temp\tmp2g5t1ddq\created-only was requested.

`src.server` resolves its state locations **once, at import time**, from
`%LOCALAPPDATA%`: the active project, the Shader tools config, the live-edit
registry, the console-event directory, the IPC root, the unpacked-asset job
root. On a machine that actually runs ReplayLab those point at real data, so
fifteen workspace tests were being refused by the developer's *own* active
project, and a run's result depended on which capture was last opened. The
container has no `%LOCALAPPDATA%`, which is the only reason it never saw this.

`mcp_host/tests/conftest.py` now redirects that root to a throwaway directory
before any test module imports the server, and clears the explicit path
overrides that would defeat the redirect. Tests that need an active project
patch `_active_project_service` themselves and are unaffected.

The sixteenth, `test_asset_catalog::test_interrupted_scan_leaves_a_resumable_checkpoint`,
was a race: with `checkpoint_seconds=0.0001` a fast disk finishes twelve cached
files inside one tick, so no checkpoint fires and the expected `KeyboardInterrupt`
never arrives. The checkpoint clock is now stubbed to advance one second per
call, which is what "every file trips the checkpoint" was always supposed to mean.

## 1o. Discovery-path audit (tools and Skills)

Walking the whole find-it/load-it/run-it path found three dead ends:

* **`list_replaylab_capabilities` and `load_replaylab_skill` were bare
  functions**, not tools. Anything inspecting the main agent's tool list by name
  broke on the first entry (see 1m).
* **`load_replaylab_skill("replaylab-core")` answered "unknown Skill id".** The
  directory advertises it under `alwaysLoadedSkills`, so asking for it is the
  obvious next move, and the answer sent the Agent hunting for a typo that was
  not there. A standing Skill is now handed over with `alwaysLoaded: true` and a
  note saying it is already in the system prompt. It is still never offered as a
  choice.
* **`build_argv` ran the recipe's own first argument as the program.** Reader
  recipes are recorded as *arguments only*, because the Host prepends the
  registered command itself (`_run_cli_reader_operation`); the named catalog
  passes the whole template to `subprocess.run`. The registered program is now
  always argv[0], so a recipe cannot execute anything but the tool the user
  registered, and an arguments-only reader recipe called by name is told what it
  is instead of failing on a missing `{path}`.

Ranking (`CatalogView.rank_skills`) is deliberately weak and stays that way: it
is an ordering hint over a directory that always lists every Skill with its full
description. The Agent chooses by reading those descriptions, which is why
removing the CJK keyword table did not need replacing with another one.

## 1p. First C++ build passed; the language pack did not

`REBUILD_QRENDERDOC.cmd` on 2026-09-10: `msbuild exit=0`. The four-tab AI
settings panel, the "Let AI Work It Out" flow and the permanent status widget
compile. Step [0/2] failed instead: `update_translations.ps1` refused because
the new status string had no reviewed zh_CN translation, and 21 further new
`AIAssistant` strings were unfinished, so a Chinese UI would have shown them in
English.

Both are fixed: the reviewed entry is in `util/i18n/replaylab_zh_CN.json` (380
entries, committed order preserved so the diff stays reviewable) and all 21
`AIAssistant` strings are translated in the `.ts`. `lint_ts.py` now reports
5028 active, 5028 finished, 0 unfinished.

## 1q. Sub-agents were promised two tools they did not have

`deep_child_system_prompt` tells every sub-agent that
`list_replaylab_capabilities` and `load_replaylab_skill` are free and are how to
reach another domain's procedure. They were not in any sub-agent's tool list:
`_tools_for` selects from `renderdoc_tool_map`, which holds only what the Host
MCP server serves, and these two are answered in the AgentHost process. The
ACP/native route serves them from the task-scoped proxy, so only the deepagents
(model API) route was affected -- which is every model API profile.

They are now built once per run and appended to every sub-agent's tools and to
the main agent's, so one gateway instance is shared: a Skill the main Agent has
already read shows as loaded to the sub-agent too, instead of each agent keeping
a private idea of what has been read.

## 1r. Watch-AiJobs.ps1

`util/ai/Watch-AiJobs.ps1` runs a fixed allowlist of jobs
(`build-qrenderdoc`, `build-host-bridge`, `i18n`, `test-host`, `test-agent`) on
request, so a build or a Windows test run does not need a person to start it.
A request file's content is a job *name* looked up in a table in the script;
nothing from a request is ever executed or used to build a command line. Logs
and exit codes land beside the request in `output/ai-jobs/`.

## 1s. Cross-route audit: does every connection reach a Skill the same way?

Measured on the real Skill set (4 domain Skills, 161,503 characters of procedure):

| route | entry prompt | Skill bodies in it |
|---|---|---|
| deepagents (model API) | 21,362 chars | none |
| ACP / vendor-native | 22,156 chars | none |
| one sub-agent | 47,234 chars | its own, in full |

Both entry prompts carry `- <id>: <description>` lines and nothing else; the
21K is the standing `replaylab-core` contract, which is meant to be there. So
the intended shape -- short descriptions first, full procedure on demand -- holds
on every route. Three defects were found around it:

* **The Codex-style route could not ask a multiple-choice question.** ACP maps an
  array elicitation schema to `selection_mode="multiple"`, but the numbered text
  fallback (the only channel a vendor without a question tool has) hard-coded
  single selection, so on that route "which of these should I export" had to be
  asked one option at a time or guessed. The block now accepts
  `[REPLAYLAB QUESTION multiple]`. `tests/test_clarification_routes.py` holds all
  three routes to the same panel contract.
* **`native_agent_system_prompt` promised the Skill tools even with no Skill roots.**
  The task-scoped proxy serves the directory and the loader only when a gateway
  was built (`_skill_gateway` returns None for missing or broken roots), so a
  damaged install produced a prompt naming two tools the connection did not have.
  The rule is now conditional on there being a Skill to load.
* **Two state paths had no non-Windows fallback.** `console_events._default_journal_directory`
  raised without `%LOCALAPPDATA%` -- and because the producer is best-effort by
  design, that silently dropped the whole diagnostic journal on any non-Windows
  Host. `draw_intent.default_intents_path` fell back to `Path(".")`, scattering a
  project's draw intents across whatever directory the Host was started from.
  Both now use the XDG data root, like every other resolver in the Host.

## 1t. Decided, not open: the main Agent stays tool-free

Asked and answered on 2026-09-10: the main Agent keeps **no** capture tools.
Every fact about the capture goes through `task` and a sub-agent, so the tool
budget and the effect ceiling stay attached to the sub-agent that spends them.
Do not "helpfully" grant it a read-only set later -- that trade was considered
and declined.

## 1u. Watch-AiJobs.ps1 parameter binding

`$PSScriptRoot` is not populated while `param()` defaults are evaluated in
Windows PowerShell 5.1, so the repository root is now resolved in the body
(falling back to `$MyInvocation.MyCommand.Path`) and checked against
`renderdoc.sln`. `Set-StrictMode` was dropped and the job run sets
`$ErrorActionPreference = 'Continue'`, because a build writes warnings to stderr
and `2>&1` turns those into error records that must not abort the watcher.
Verify with `-List` before trusting it: nothing here could be executed from the
container, and no PowerShell exists on either side of the bridge.

## 1v. Scratch moved to _to_delete

`renderdocUltra\_to_delete\` (261 MB) holds the stray root `.obj` files, the
codex probe script, `replaylab\tmp-events`, the root `__pycache__`, the second
virtualenv `.venv-py310` (nothing references it), the unpacked
`qrenderdoc_3rdparty_inspect_20260821`, and 35 scratch entries from `output/`
(`*.tgz`, `t-scope-*`, `tpre-*`, `tsa-*`, `hb-*`, `toolchain-regression-tmp-*`).
`output/` keeps every named work-session directory, including all `eid-*` --
those are the paused line's failure receipts.

## 1w. The directory is now the live state, not a fixed summary

Skills were already dynamic: short descriptions in the entry prompt, full
procedure on demand. Tools were not -- `tool_families()` returned a count and
eight example names per family with no way to see the rest, and the extractors
the user registered were not in the directory at all, so an Agent could not
know an unpacker for this game existed.

* `list_replaylab_capabilities(family=<name>)` returns every granted tool in one
  family with its own description. The families stay a table of contents; they
  are no longer a wall. Both routes wire it: the API route reads descriptions off
  the `StructuredTool`s, and the task proxy calls the new
  `SkillGateway.describe_tools()` once the upstream `tools/list` has answered
  (the gateway is built before the Host has spoken).
* The directory gained `assetTools`: id, title, the user's own purpose note, and
  how many of its operations were actually proved. What each can do still lives
  in `list_asset_tool_operations`; this only says who is there.

**And the drift this uncovered.** `agent_tool_providers.validate_operation`
(AgentHost, which Settings writes with) had not been updated alongside
`tool_providers.validate_operation` (the Host, which the AI records with). The
Host's version writes `name`, `description`, `inputSchema`, `result`,
`knownFailures` and a richer `proved`; the Agent's silently dropped every one of
those fields. So the AI would work out how to drive an extractor, the Host would
record the recipe -- and the next time the user edited that tool in Settings,
`upsert_tool` would rewrite the file through the Agent's validator and delete it.
The whole discovery would then be repeated. The shared block is now identical in
both packages, and `test_tool_provider_contract.py` compares a full rich
operation and asserts a recorded recipe survives a Settings rewrite, instead of
only comparing registrations with no interface at all.

## 1x. Whole-chain audit, 2026-09-10 (three parallel reviews)

Three reviewers went through the API route, the ACP/vendor route, and the
Settings panel plus everything that applies what it saves. 24 findings; 13 were
real and are fixed, the rest are recorded below as known and unfixed.

### Fixed

1. **Every sub-agent was handed each Skill tool twice.** The task proxy serves
   `list_replaylab_capabilities` / `load_replaylab_skill` and `route.allowed_tools`
   includes them, so they entered `renderdoc_tool_map` -- and 1q then appended the
   in-process pair as well. Providers reject a tool array with a repeated function
   name, so *every delegated task would have failed*. They are now excluded at
   ingest alongside `REPORT_TOOL`; the in-process pair is the only one, which also
   means one shared gateway rather than two in different processes.
2. **The plain-text question block was never parsed on the ACP route.** Only
   `claude-code-acp` is told to use a vendor question tool; the other seven ACP
   presets are told to emit `[REPLAYLAB QUESTION]`, and only `_run_codex` parsed
   it. On Gemini/Copilot/Cursor/Qwen/Kimi/CodeBuddy/custom the run ended
   "completed" with the raw markup as the answer. `_run_acp` now falls back to the
   same parser.
3. **A disabled Skill was still listed and loadable through the task proxy.** The
   run-side catalog is filtered in `cli.py`; the proxy rebuilt an unfiltered one
   from `--skill-root`. `--disabled-skill` now travels with the roots.
4. **Editing a Skill in Settings deleted it.** Two roots holding one name were both
   quarantined, so Save -- which writes the edited copy into a second root, exactly
   as the dialog promises -- removed the Skill; doing it to `replaylab-core` stopped
   every task from starting. A later root now overrides an earlier one. Two copies
   in one root cannot happen (a Skill's name must be its folder name).
5. **The unpacker purpose never reached the AI.** `validate_tool` writes the user's
   text as `description`; `tool_runner` and the capability directory both read
   `note`, which is the interface block's field and is always absent. Every
   registered extractor looked purposeless. The fixtures hand-wrote `note`, which
   no writer produces, which is why the tests passed.
6. **Re-saving a tool in Settings deleted its discovered recipes.** `upsert_tool`
   compared whole connection objects; the dialog rewrites every field including
   ones it does not show (`headerEnv`), so an unchanged OK looked like a different
   program. Identity is now kind + url, or kind + command + args. The dialog also
   starts from the stored connection so unshown fields survive.
7. **A cancelled run leaked its turn workspace.** `contextlib.suppress(Exception)`
   does not hold `CancelledError`, so `cleanup_turn_workspace` never ran and every
   cancel left a directory and a live-edit registry behind.
8. **One question had two ids.** `event_payload()` minted a fresh id per call and is
   called at least twice, so the panel event and the run result disagreed.
9. **A tool that started and never ended left a spinner forever.** `_settle_children`
   and `_finish_child` now close every in-flight activity.
10. **Verification rounds concatenated answers in the panel.** The reporter's
    character counter was not reset and every delta said `reset: False`.
11. **"READ-ONLY" was not true when user MCP tools were attached.** Those bypass the
    effect ceiling by design (ReplayLab cannot know what they do); the sub-agent
    prompt now says so instead of promising read-only.
12. **A command-line unpacker added from the Unpackers tab got roles nothing reads.**
    The role defaults were seeded from the *edited* tool's kind, which is empty when
    adding. They now follow the combo box, and Add on that tab opens on "Command
    line program".
13. **Two non-Latin tool names collided on the id `server`,** so the second silently
    replaced the first. The fallback id is now derived from the name.

### Known and not fixed (deliberate)

* `_child_for` treats an event whose parent is not a live task run as the main
  Agent's, so a nested run id the framework does not chain would stream a
  sub-agent's prose into the answer. Changing the rule risks losing real main-agent
  output; it needs a deepagents version bump to test against.
* `agent-` delivery ids embed `time_ns()`, so the event de-duplication machinery in
  `events.py` can never match. Harmless today (nothing replays), wrong in principle.
* `report_task_result` calls `flock` + `time.sleep` from an async tool, so a
  contended lock can stall the event loop for up to 5 s.
* Settings cannot see or remove MCP servers that live in `agent-tools.json` (the
  ones the AI adds via `update_replaylab_configuration`), and `@mcp:<id>` cannot
  re-enable a disabled one.
* "Detect Installed" writes to the legacy `asset-readers.json`, which nothing reads
  once `tools.json` exists.
* Unticking every model on a subscription connection silently keeps the old list.

## 1y. Second whole-chain audit, 2026-09-10 (Host, bridge, connection layer)

Three more reviewers: the MCP Host and its policy layer, the RenderDoc bridge and
the shared Shader library, and the connection/vendor/credential layer. 30-odd
findings; 17 fixed. The worst ones were features that had never worked at all.

### Fixed -- things that were silently dead

1. **`_evidence_roots()` lost three of its five scopes.** Two calls raised
   `TypeError` on every invocation -- a keyword-only `require_catalog` omitted, and
   the *tool* `get_active_shader_workspace_path` called without its event id -- and
   two bare `except Exception` swallowed both. So the active project, its unpacked
   assets and the Shader workspace were never evidence roots, and
   `publish_shader_work(organized_source_path=...)` could never accept a path:
   **the entire shared-Shader-library flow could not be given an organized source.**
   The workspace root now comes from the project status, which is also read-only --
   the tool carries `{project, replay}` effects a read-only helper must not take.
2. **Draw intents never travelled with the project.** Same wrong-arity call, same
   bare `except`, so every project shared one user-level file and Game B answered
   with Game A's intents.
3. **A partial republish erased what a Shader *is*.** `publish_entry` merged parts
   but rebuilt `interfaceHash`/`layoutHash`/`codeSha256`/`gameId`/`buildId` from the
   new record alone, so adding a note dropped them -- and the entry then vanished
   from every interface-tier lookup. Identity is now carried forward like the parts.
4. **The exact-bytes entry could be filtered out by a weaker key.** `candidates()`
   rejected it on a stale interface hash, a different stage spelling or another game
   id. Bytes are conclusive, so the exact match is now never filtered and is
   returned first.
5. **`plan_capture_shader_reuse` shortlisted 4096 of up to 20000 entries**, in JSON
   insertion order, silently -- past ~4096 organized Shaders it reported stored work
   as new. It now reads the whole index and reports `shadersTruncated`.
6. **An accepted report was destroyed by resubmitting it.** Past the budget, the
   idempotency check compared raw arguments against `model_dump()`, which never
   matched (the dump carries defaults the call omits), so `self.report = None` ran
   and the sidecar was unlinked. A finished task reported partial with nothing to
   show. An accepted report is now a result, not a submission.
7. **`_open_capture` invalidated the restore it had just scheduled.** On builds that
   deliver CaptureViewer callbacks synchronously, `OnCaptureLoaded` minted session
   S1 and scheduled the restore against it; `_open_capture` then minted S2
   unconditionally, so the restore ran and discarded its own result at the session
   check -- status stuck on "matching this RDC", nothing restored, nothing logged.
   It now resets only when the callbacks really did not run.

### Fixed -- correctness and safety

8. `fetch_counters` was the only tool with no error handling: `fetch_counters("")`
   reached the Agent as a raw protocol error and spent a report round.
9. `texture_id` was not covered by the ResourceId string-widening, so a Node/Cursor
   client lost precision on `analyze_pixel` and `inspect_pixel_object`.
10. The shared library's index and applied ledger were read-modify-written with no
    lock, in a library explicitly designed to be shared between RenderDoc instances;
    a hand-edited ledger also crashed the LRU eviction.
11. `_project_status_parts` validated only the container widget with `_sip_deleted`,
    so a dead child label made every later status message vanish silently; and the
    permanent status widget was never removed on shutdown, so a module reload added
    a second one.
12. **The model the user picked was discarded on Claude Code ACP.** `ANTHROPIC_MODEL`
    was built from the *saved profile*, and that adapter exposes no ACP model option,
    so the run happened on the saved model while the panel reported the picked one.
13. **"Test model" passed without testing the model.** It never selected one and
    never reported which one answered, so a plan-restricted id "responded
    successfully" and failed on the first real task. It now names the model that
    answered, or says plainly that the agent does not report one.
14. **A Gemini profile's base URL was honoured by discovery and dropped at request
    time**, sending the user's gateway key to Google. Now refused, naming the
    protocol that does carry an endpoint.
15. `REPLAYLAB_MCP_HOST` was missing from the reserved-credential env names, so a
    profile could inject an API key under it and `find_replaylab_mcp` would print
    the value into the doctor panel and the log.
16. `redact()` kept the first twelve characters of the *match*, leaving the head of
    a short secret in the log (and a short enough one whole), and redacted the
    scheme of `Authorization: Bearer <token>` while printing the token.
17. `capture_draw_image`'s documented crop could never happen (see below -- reported,
    not yet fixed).

### Known and not fixed

* `capture_draw_image(crop_to_draw=True)` silently returns the whole render target:
  it calls `get_post_vs_data` with no event id, and that handler returns counts
  rather than vertices, so the footprint is always empty. Needs a bridge-side
  handler change.
* `get_draw_calls` / `get_resources` / `get_textures` are unbounded and go through
  `_ok`, which does not truncate -- one default call on a AAA frame is ~10 MB.
* A report may cite a tool call that returned an error: the gate checks the name
  against `_tools_used` and never consults the recorded outcome.
* A corrupt report sidecar makes every one of the 178 tools raise, not just the
  report gate.
* `test_tool_connection` is classified read-only but executes the user's binary.
* `get_buffer_contents` documents `length=0 = all`; the bridge rejects it and
  silently caps at 4096 bytes.
* The `interface` reuse tier has no tie-break; the plan now says so
  (`ambiguous`/`candidateCount`) instead of presenting an arbitrary winner.
* Declared `vision` capability is decorative -- images are gated on
  `options.image_input`, which is unset by default, so a vision model advertised in
  the panel silently receives a filename instead of the picture.
* The ACP launch probe sends the vendor's stderr to DEVNULL and calls a 600 ms
  auth failure a success.

## 1z. Third audit round: the Skills themselves, and the panel

Two reviewers: every Skill checked mechanically against the 178 real tool
registrations and the effect manifest, and the C++ panel checked against the
event contract it consumes.

### The Skills told the AI to call tools that do not exist

83 distinct tool names across 163 references were extracted from all 17 Skill
files; 78 resolved. The two that did not are both in *recovery* branches, so
they failed exactly when something had already gone wrong:

* `renderdoc-unpacked-asset-intelligence` -> `list_unpacked_asset_readers()`.
  Now `discover_asset_readers()` plus `get_replaylab_configuration()`'s
  `readerContract`.
* `renderdoc-shader-workbench` -> `read_recovery_regular_file`, which is an
  internal Python helper, never a tool. Now `read_unpacked_asset`.

Three more Skill defects, all of which would have stopped the AI mid-procedure:

* §0 called `get_current_shader_representation` "read-only". It declares
  `{workspace}`, so on the explain-only path -- the one case that runs at a
  read-only ceiling -- the tool is simply absent from the AI's list.
* §0 stated the pipeline needs `workspace, project, ui`, "review also requires
  `replay`". `get_active_shader_workspace_path` declares `{project, replay}` and
  the apply step `{replay}`, so a grant without `replay` dies at the *first*
  mandatory call with no diagnostic pointing at the ceiling.
* Step 3 claimed "all seven identity arguments are mandatory (the Host refuses a
  partial set)". `prepare_shader_workspace` validates each independently; the AI
  could report a blocker and stop on a call that would have succeeded.
* `references/draw-intent-vocabulary.md` was sent to but not inlined (no
  `replaylab-required-references`), and the Skill folder is not an evidence root,
  so the §0b vocabulary loop was unfollowable. Declared; the Skill is now v33.
* `replaylab-core` §5b named `@skill:<id>`, a syntax nothing parses, and told the
  AI such a Skill was already loaded -- so it would skip loading it.

`tests/test_skill_tool_references.py` now checks this mechanically: both
`` `name(` `` calls **and** bare backticked snake_case identifiers (which is how
half the dead names were written), against the AST of the Host's registrations,
plus a check that no Skill calls an effectful tool read-only. Verified by
reintroducing both dead names and watching it fail.

**Note for future sessions:** the container's `~/.agents/skills` was three
versions behind `renderdocUltra/.agents/skills` (29 vs 32). The repo is the
source of truth; patch it surgically rather than copying a container snapshot
over it.

### Panel fixes

* **A failed conversation load destroyed the history.** `Load()` returns false
  for recoverable causes -- file held by AV or backup, payload over the 4 MiB cap,
  a schema a newer build wrote -- and the panel then created one blank thread and
  let the next save replace the whole store, with no backup. A store this session
  could not read is now never written (`m_ConversationStoreReadOnly`), and the
  message says so.
* **A pending question was destroyed and its answer discarded** by any capture
  skill action: those run with `includeConversation=false`, and the envelope is
  the only carrier for `clarificationResponse`. The envelope is now also built
  whenever a clarification response is present.
* **Child progress went permanently silent after 128 message streams.** The
  producer mints a new item id per 8 KB chunk and never marks a child message's
  lifecycle terminal, so the map filled and every later message was dropped -- on
  a long run the activity group goes quiet while the task is still working, which
  reads as a hang. The least-recently-touched stream is now evicted instead.
* **A `needs-user-input` run whose clarification failed validation** showed
  "Waiting for input" with no question and no buttons, silently. It now says so.
* **The terminal error was cut at 1000 characters**: the producer bounds `summary`
  to 1000 and `data.error` to 4000, and the panel read only `summary`, usually
  losing the actionable tail.

### Panel findings left open
* A restored conversation replays a stale `clarificationId` (and a frozen context
  for a capture session that no longer exists) if the user answers days later.
* Past 512 tasks the tombstone compaction can let one answer be appended twice.
* A re-`queued` attempt concatenates onto the previous attempt's streamed text
  (`m_TaskTexts` is not the live buffer). Only reachable if AgentHost re-emits
  `queued` on the same task id, which `ProgressReporter.queued()` currently does not.

## 2a. Fourth round: the shader pipeline, and a review of this session's own changes

### The equivalence gate could be defeated by a rename

`equivalence.apply_renames` deliberately renames identifiers after a `.` (struct
and CBuffer fields are legitimate targets) -- but a **swizzle** tokenizes as a
plain identifier, and `is_reserved()` never consulted the `_SWIZZLE` pattern that
sits unused in the same file. Reproduced against the real code:

    baseline   float c = uv.xy.x;
    renames    {"xy": "xz"}          -> validate_renames accepts
    proposal   float c = uv.xz.x;    -> compiles, ABI byte-identical
    verdict    rename_only_equivalent -> {"equal": true, "level": "rename-only"}

It proves equal because the *same map* is applied to the baseline before the
comparison. So a shader reading a different texture channel passed the compile
gate, the ABI gate and the equivalence review, was published, registered and
applied to the replay. The far more common accidental form -- renaming a
decompiler local called `x` or `r` -- corrupted every swizzle in the file.

`shader_reuse.py` already handled this correctly (`after_dot and _SWIZZLE_RE`);
the workspace path was the outlier. A swizzle is now never renamed, and a rename
whose source only ever appears as one is refused with an explanation rather than
silently doing nothing. A variable genuinely named `x` still renames, and its
swizzles are left alone. `tests/test_shader_equivalence.py` pins all four cases.

### Bugs introduced earlier in this session, found by reviewing it

The self-review was worth more than any other single check this session:

* **CRITICAL -- every Gemini profile was broken by the base-URL guard added in
  1y.** `Profile.endpoint` resolves to the *preset's* default
  (`https://generativelanguage.googleapis.com`), so the guard fired on a stock
  profile with no override at all. It now tests `profile.base_url`, and runs
  before the package load so the message is the actionable one.
* **`plan_capture_shader_reuse` was still capped at 4096.** The 1y edit was in a
  patch block whose third assertion failed, so the whole write was skipped and
  only the follow-up line-insert landed. Re-applied, with the ambiguity flag.
* **`test_model` refused connections that work.** It reported a model mismatch as
  a failure, but the real task path *does* select the model
  (`acp_session._select_model`), and `--model Opus` vs a resolved
  `claude-opus-...` differs on case and alias alone. It now reports which model
  answered the probe and says the pick applies on a real run.
* **`ReportGate` still discarded an accepted report on the non-exhausted path.**
  The 1y fix covered only `exhausted`; a schema slip on a *verification follow-up*
  -- which by definition happens after a report was accepted -- still set
  `self.report = None`.
* The clarification-envelope fix reached one of the two send paths; the
  "steer a running request" path still dropped the answer.
* The "question could not be displayed" message reached the task-event path but
  not the stdout path.
* `deep_child_system_prompt` still promised the two Skill tools to the
  capture-free sub-agent, which is built with no tools at all.
* `build_argv` could produce `uex uex extract ...` when a recorded recipe names
  the program in argv[0]; and leaving the bare name would have run whatever PATH
  resolved rather than the registered binary. argv[0] is now replaced with the
  resolved program when it names it, and prepended when it does not.
* Editing a `cli` tool still rewrote `args`/`env` the dialog does not show, which
  changed its connection identity and discarded its proved recipes -- undoing the
  1y fix for exactly the tools it was written for.

### Shader-pipeline findings left open

* Live edit reports `applied: true, reused: true` for a candidate that
  `rollback_shader_workspace_shared_resource` already removed from the replay:
  the rollback never clears `_active_apply`, so a later comment-only edit whose
  bytes match short-circuits without calling the bridge.
* `refresh_toolchain` sets `latestCompiled = None` while keeping `revision > 0`;
  one failed compile then appends a history entry and every subsequent tool
  raises "latestCompiled is missing", with no in-product recovery.
* "Soft accept" of a non-equivalent proposal is reached by burning the review
  budget: `differences_to_reasons` emits everything at `severity="warning"`, so
  `soft_accept` reduces to "exhausted" and `verdict.userDecision` is unreachable.
  `validate_publication_gate` then accepts it without consulting
  `ReviewRounds.user_soft_publish_allowed`.
* Override/replacement handles live only in the MCP host's memory; if it restarts
  while the capture stays open, the applied shader cannot be rolled back.
* `decide_shader_organization_review_budget("finish")` returns before the
  `MAX_BUDGET_GRANTS` check and zeroes the template-error counter.
* `write_shader_workspace_organization_file` is the one session consumer that
  skips the HMAC seal `_validate_session` enforces everywhere else.

## 2b. Fifth round: persistence across reopen, AI autonomy over settings, connections

Request: keep the AI's parsed shader/buffer work when the .rdc is reopened, keep
the matching index, let the AI decide tool loading and change any setting itself,
and make every API and ACP connection work. Both suites are clean against the
environment baselines (host 105 env-only failures, agent 9 env-only failures,
`test_vendor_matrix` / `test_deepagents_framework_integration` need packages
this container cannot install). Development build only.

### Persistence: the capture identity, the alias store, the shared library

* `_stable_capture_id` (host and `_catalog_stable_capture_id` in the bridge) no
  longer hashes the capture path: identity is `api` + sorted
  `(stage, entryPoint, rawSha256)`. Opening the file by another spelling, from a
  moved folder, or a second capture of the same build now keys to the same
  records. The live workspace binding still compares the path separately
  (`_workspace_live_capture_status`) -- an open editor stays with its file.
  `fs` was added to the host stage table so it normalises like the bridge.
* Aliases (CBuffer/member/shader names the user approved) were stranded under
  the old `capture_id` on every reopen, because RenderDoc reassigns ResourceIds.
  `SymbolStore.save_capture_catalog` now inherits alias rows by
  `(stage, entry_point, raw_sha256)` from the most recent capture that had them,
  rebinding `metadata.captureId`/`shaderResourceId` to the new identity and
  recording `inheritedFrom`. The review found the first version was a no-op --
  `match_capture_catalog` called `clear_aliases(capture_id)` one line later, and
  the export path (`export_alias_table`) filters on `metadata.captureId` -- so:
  the matcher now clears per Shader only where it can prove a new match, a
  re-save prunes only aliases of Shaders that left the catalog, a Shader that
  already has names under this identity inherits nothing (a rescan cannot
  resurrect an older name), and `test_alias_inheritance.py` covers the store,
  the matcher and the export path.
* Shared shader library: `_library_lock` raises `SharedShaderLibraryError` on
  timeout or when the lock file cannot be opened instead of writing unlocked;
  `rebuild_index()` (exported) rebuilds `index.json` from the entry folders;
  readers (`candidates`, `library_summary`) get an in-memory rebuilt view when
  the index is unreadable *or* holds fewer entries than there are entry folders
  (the shrunk index the old publish left behind); the on-disk index is never
  shrunk. `publish_shader_work` accepts `draw_intents` and `algorithm_graph`.
* `plan_capture_shader_reuse`: whole index (`MAX_ENTRIES`), stages normalised
  on both sides of the interface comparison, `alreadyApplied` carries
  `entryId`/`libraryParts` or `libraryEntryMissing`, `restoredOnOpen: False` and
  the docstring say plainly that the ledger saves the matching, not the
  applying; ambiguous ABI buckets say `candidateCount`/`ambiguous`/`decideYourself`.
  The bridge's status line now reports library size, not a per-capture overlap it
  never computed.
* `_draw_intents_path()` derives `<sharedDictionaries>/assets/draw-intents.json`
  from the project when no asset catalog context exists, so a project without an
  asset root still keeps its own intents.

### AI autonomy over settings

* `update_replaylab_configuration.upsert_mcp_servers` accepts `transport: http`
  with an `https://` url, and `{id, enabled}` toggles a server in place. The host
  now mirrors the agent loader's rules (`validate_mcp_server`): id shape,
  `replaylab` reserved, single-line fields, env names/count, arg count -- an
  entry the loader would drop is refused with the reason instead of being
  written and silently dropped at the next load.
* `write_skills` validates with the loader's rules (`skill_document_problem`):
  front matter, `name` == id, id shape `^[a-z0-9]+(?:-[a-z0-9]+)*$`, description,
  metadata counts/sizes, `replaylab-category`, version numbers, body size, and
  the root list is kept within `MAX_SKILL_ROOTS` (the user's overlay goes first).
  `test_configuration_skill_writes.py` proves every accepted server and Skill
  loads through the real loader.
* Reads roam: `ToolCallScope.validate` freezes the UI target for effectful tools
  only, with an error that names the field and both values.
* Capability directory shows `toolCallBudget/Used/Remaining`; the deep route now
  counts every RenderDoc call into it (it stayed at zero before).
* `describe_unpacked_asset` discloses `skippedReaders` when a built-in reader
  answered but a registered tool claims the file; engine detection was given
  relative paths as its API expects.

### Connections

* Responses: `reasoning` only when thinking is requested; `reasoning_effort`
  travels inside the same `reasoning` object on that surface (a separate
  `reasoning_effort` next to `reasoning` is forwarded as an unknown argument by
  some langchain-openai versions); on the chat surface it stays `reasoning_effort`.
* Anthropic: `max_tokens` is raised above `budget_tokens` instead of failing;
  a custom Anthropic-protocol profile thinks when asked by the same switches.
  `text/plain` documents in tool results use the API's `text` source, not base64
  (which is PDF-only and a 400).
* Gemini: the base-URL guard tests `profile.base_url` before loading the package.
* `_effective_protocol` honours `options.api`; native image input defaults on
  where the protocol carries it; `ask_user` has a fully typed schema.
* ACP: permission denials are logged with the label; `test_model` reports
  `probeModelDiffers`/`modelUnverified` instead of refusing.
* One data root on every platform: `LOCALAPPDATA`, else `XDG_DATA_HOME`, else
  `~/.local/share` -- host config files, bridge/host IPC root, agent modules,
  and the three C++ sites (`AIAssistant.cpp` instance discovery and
  `taskEventRoot`, `ReplayLabIntegration.cpp`) that fell back to the temp
  directory. On Windows nothing changes; on Linux/macOS the agent and the panel
  now find each other's files.

### Also fixed

* A rejected resubmission after an accepted report keeps the report (gate and
  the `.report.json` mirror agree).
* Swizzles after a dot are never renamed by the equivalence gate.
* `build_argv` receives the registered program spelling as well as the resolved
  path, so a launcher whose file name differs from the recipe's argv[0] is not
  prepended a second time.

### Left open (recorded, not fixed)

* `_draw_intents_path` creates the directory on a read path.
* `plan_capture_shader_reuse` decides interface candidates by ABI only; ranking
  stays a hint by decision.
* Everything in the earlier "left open" lists (1y, 1z, 2a) still stands.

## 2c. After the first run of 2b: project window timeout, CBuffer aliases, packaged Skills, panel

What the user hit, with the evidence behind each fix (`console-events`,
`internal-runtime`, the project tree and the alias store on the machine).

### "ReplayLab Host request outcome is uncertain: [WinError 12002]"

`project-list` from the project window is a POST to ReplayLabInternal.exe with a
90 s client timeout; 12002 is WinHTTP's timeout. The Host was alive (heartbeat
fresh until the GUI closed at 15:26) but the read queued behind a long
operation: `ProjectOperationRegistry.execute` serialised every operation under
one RLock, `ReplayProjectService.status()` took the same per-project RLock a
mutation holds for its whole duration, and `ActiveProjectService.list_projects`
waited on the selection thread lock. Afterwards the host could not exit until
the stuck thread returned (state file removed at 15:33), so a restarted
RenderDoc found the same wedged host by its still-answering `/health`.

* `list-projects` runs outside the registry lock (requestId dedup kept);
  mutations wait at most 20 s and then get `503 project-service-busy` naming
  the running operation and its age, instead of a transport timeout.
* `status()` waits 2 s for a consistent snapshot, then reads the atomically
  written manifest as it is (`busy: true`).
* `list_projects` waits 2 s for the selection guard, then reads the registry
  and selection files unguarded (`busy: true`).
* Which operation held the lock is not proven; the next occurrence names it.
  Candidates: `register_workspace` at the end of an organization (the replay
  project was written at 15:28:58, after the GUI closed).

### CBuffer aliases did not come back

`ReplayLab/aliases/cbuffer/*.json` showed the AI's publishes carrying a block
alias and `"aliases": []`, and the project's knowledge holds `aliases: 0`.
Cause: `renderdoc_mcp_bridge/cbuffer_alias_identity.reflection_alias_blocks`
mapped no rows for stripped DXIL -- the placeholder `unknown[N]` variables every
shipped game reflects -- because arrays were skipped and placeholder
annotations were "incomplete", so every member rename came back `unmapped`.
The panel itself keys those rows as `unknown[e]` at `byteOffset + stride * e`
(`BufferViewer::UI_AddFixedVariables`), and the user's own typed alias in the
store (`unknown[3]` @48) proves it. Array elements are now expanded to those
exact paths, placeholder rows map when their shape is sane (real offset and
size, inside the block, not overlapping), overlapping placeholders map nothing,
and `memberMappingKind` says which case applied. Host `aliases_from_renames`
then matches a decompiled `float4 v3 : packoffset(c3)` to `unknown[3]` and the
panel shows the alias on reopen through the unchanged layout key. Arrays of
structs remain unmapped. Not fixed: nothing registers alias tables into the
project (`register_replay_project_alias_table` is never called by any Skill),
so the project-restore alias route stays empty; the per-user store is what
carries CBuffer names across reopen today.

### The doctor's "1 packaged skill definition(s) quarantined"

Two separate things. (1) `packaging/Build-ReplayLabRuntime.ps1` listed five
Skill folders by hand, so the frozen runtime shipped 5 Skills; it now bundles
every folder under `.agents/skills` that holds a `SKILL.md`, asserts
`replaylab-core` is among them, and runs the post-build doctor with an empty
`--tools-config` so the build machine's own Skill folders cannot skew the
count. (Five folders on this machine are empty stubs with no `SKILL.md`; git
tracks only the five real ones.) (2) The quarantined Skill was the user's own
overlay Skill `2`, created from the panel template: `name: 2` is an integer to
YAML and the loader refused it as "name must be a string". The template now
quotes `name`/`title`, the CLI's id rule is the loader's, integer names load as
their folder, and the doctor detail names the folder and whether it is bundled
or a user folder instead of calling everything "packaged".

### Settings panel

MCP servers and Unpackers each carry their own buttons (Add Server / Edit /
Remove / Test Connection; Add Program / Find Installed / Edit / Remove / Run
Test / Let AI Work It Out) and their own presets: MCP presets (uex's own MCP
mode) on the MCP tab, reader presets on the Unpackers tab. The shared bar under
the tabs and the dead `id == "uex"` special case in the detect dialog are gone.
Nine new source strings were added to `qrenderdoc_zh_CN.ts` under `AIAssistant`.

## 2d. CBuffer names travel with the project; panel switches and feedback

* **Alias store: one file per block layout.** `shader_workspace/aliases.py`
  and `ReplayLabCBufferAliases` resolve one root: the selected project's
  `shared/dictionaries/cbuffer-aliases/<layoutSha256>.json` while
  `config/active-project.json` names a project, else the per-user
  `ReplayLab/aliases/cbuffer`. No merging of two stores: the first time a
  layout is touched under a project, a per-user file for it (names made before
  the project existed) is copied into the project once and not read again.
  Inside the file a `user` entry always beats an `ai` publish (`keptUser`).
  AI publishes (`rename_shader_workspace_symbols`) and names typed in the
  panel both land in the project, so any other .rdc opened under it shows
  them. `REPLAYLAB_ALIAS_PROJECT_ROOT` pins the project root for tests. The
  rename result says `store: project|user` and `memberMappingKind` per block.
* **Skill (shader-workbench v35).** User-typed aliases rank above the asset
  match and above the AI's own reading: a named block or member keeps its
  name in the rename map; the AI names only what the user has not. For a layout-/program-verified unpacked
  asset match the AI uses the source's block and member names directly (block
  and members in the same rename call); a block left as `cb1` next to named
  members is called unfinished; the result's `unmapped`/`memberMappingKind`
  is explained.
* **Panel.** Servers from `agent-tools.json` (`mcpServers`, what the AI
  registers) now appear in the MCP servers tab with the same enable switch as
  tools and Skills (`mcp-enable/disable/remove/test` routed by a row marker);
  Run Test / Test Connection set the button to "Testing..." and end in a
  passed/failed dialog as well as the status line; Edit on a server row
  explains how to change it. Every Skill, server and tool row is checkable and
  defaults to on. Ten new strings added to `qrenderdoc_zh_CN.ts`.
* Not done: editing a server entry in place (remove + Add Server, or ask the
  AI); project-restore's alias route (`register_replay_project_alias_table`)
  is still never called -- the shared store above is what carries names.

## 2e. Why 1.rdc did not pick up 6.rdc's organized Shader (open)

Evidence on the machine: the replay project holds heads for two Shaders
(`3c4afb…`, the EID 11329 one, 8 records; `96ce18…`, 4 records), both returned
by `restorable_shader_raw_hashes()` (run on the machine). The shared Shader
library (`shared/shaders`) is empty -- nothing publishes to it, so the status
line that quoted its size was misleading. Opening 1.rdc from the project window
at 18:00 ran the restore: it imported a catalog holding only `96ce18…`, planned
one raw-exact match and seeded workspace `2720fe70…` for it. `3c4afb…` was not
in that catalog although catalogs exported from 1.rdc on 09-05/09-06 contain
it (resource ids 747/754/3162 -- ResourceIds move between opens). Whether the
target scan (`_inventory_project_shader_targets`) reported it unobserved, and
with which diagnostics, was not recorded anywhere; that is the gap this round
closes rather than the root cause, which needs the next run's record.

* The internal host now persists every restore outcome to
  `<project>/.replaylab/replay-project/derived/restore-results/<capturePathSha256>.json`
  (restoreMode, cache reasons, `targetLookup` observed/unobserved heads, per-Shader
  items, error).
* The project-window restore path publishes the same `projectRestoreFinished`
  console event the automatic path does (with `targetLookup`), and both paths
  end with a status line built from the replay project's heads and that record:
  "the project holds organized work for N Shaders; this capture binds F of them
  (A restored), M not found here -- details in derived/restore-results".
* Next step: reopen 1.rdc from the project window and read that JSON: if
  `3c4afb…` is in `unobservedTargetRawSha256s`, the bridge scan missed a live
  resource (check `diagnostics` codes: shader-raw-bytes-unavailable,
  shader-entry-points-unavailable, resource-type filter); if it is observed but
  not applied, the per-Shader item says why.

## 2f. Console panes, restore trail, freeze noise, bridge note, wording

* Console (`shader_console.py`): the message list and the detail box sit in a
  vertical `QSplitter` (drag the handle; the detail box no longer has a fixed
  130 px cap).
* Restore visibility: `internal_tool_service._append_progress` persists a
  throttled running trail into the same `derived/restore-results/<sha>.json`
  (status `running`, `progress` list), so a restore that never finishes still
  leaves its last phase on disk. The project-window restore callable publishes
  `projectRestoreFinished` on success, on `capture-changed-during-restore`, and
  on exception (the exception still propagates to the window's status task).
* `currentContextFreezeCompleted` (the "AI 冻结已完成" flood): the AI panel
  freezes the current RenderDoc context on every refresh; completed freezes are
  no longer published to the console (failures/abandonments still are).
* `bridge.not-connected`: the in-process bridge is built into qrenderdoc (there
  is no extension to enable). The stdio MCP process's startup ping can miss
  while qrenderdoc's UI thread is busy or no capture is open, and every later
  tool call re-checks, so the event is now `info` with wording that says that.
  The old text ("请确认 ReplayLab 扩展已启用") was legacy wording only; the
  bridge itself is not legacy code.
* "ReplayLab" removed from user-facing text: Python project UI (menu group is
  now "Workspace"/"工作区", window "Project Workspace"/"项目工作区", "Console"/
  "控制台", status lines lose the "ReplayLab:" prefix, host "Host" wording is
  "project service"/"项目服务"), console module texts, host console lifecycle
  messages ("AI 服务…", "MCP 工具服务…"), and the C++ `tr()` strings in
  AIAssistant/AIAgentMonitorWindow/AIConversationView/BufferViewer ("AI host",
  "RenderDoc will run…", "Task logs", "AI (live)", "Expose as Material
  Parameter"). `replaylab_zh_CN.json`, `ReplayLabProjects.cpp` (regenerated with
  the repo extractor) and `qrenderdoc_zh_CN.ts` are updated together; the
  extractor + `sync_replaylab_catalog.py --check` pass on the committed set. A
  stray raw literal ("none") in the restore summary now carries `i18n-ignore`.
* Not done on purpose: internal identifiers (`ReplayLab*` classes, module
  names, `ReplayLabInternal.exe`/`ReplayLabMCP.exe`, `%LOCALAPPDATA%\ReplayLab`,
  `replaylab-*` schemas, env vars, the `ReplayLabProjects` translation context,
  `.agents/skills/replaylab-core`). Renaming those is a repository-wide pass
  that also moves user data and authorization records; it needs a chosen
  replacement name and its own round.
* Rebuild: host + bridge (`REBUILD_HOST_AND_BRIDGE.cmd`) and qrenderdoc
  (`REBUILD_QRENDERDOC.cmd`, which regenerates the language pack).

## 2g. Bread AI naming, freeze noise root cause, gate subset rule, poll timeouts

* Product name in the UI is now "Bread AI" (menu group, AI panel title, host
  console lifecycle messages, agent identity in prompts); "RenderDoc AI" is
  gone from `tr()` strings, TS, Python UI strings and the translation JSON.
  Internal identifiers still say ReplayLab -- the repository-wide rename
  (5.7k occurrences, 206 schema tags, 99 env vars, the `replaylab/` tree,
  `%LOCALAPPDATA%\ReplayLab`, project `.replaylab/`, RDC section names) is
  its own round and needs the data-migration decision recorded there.
* Freeze flood root cause: `server._active_live_context` (20 call sites) asked
  the bridge for `get_current_renderdoc_context`, which in dispatch mode runs a
  full fixed-EID context-freeze job (UI barrier + console event) -- once per
  host tool call. Those callers only verify capture identity (session/path/API)
  and take EIDs from their own arguments. The bridge now serves
  `{"identity_only": true}` with a plain identity read (`_capture_identity_context`)
  and the host uses it. The AI request itself is still frozen exactly once, at
  submit time, by the AI panel (`contextFreezeRequired`).
* ABI gate: an ambiguous (stripped) block whose candidate loads are a strict
  subset of the captured loads (same offset/width/type) is now compatible
  (`access-signature-subset`). Dead-code elimination after an edit such as
  `SV_Target.x = 1.0` no longer trips the gate (rev 0004 of b0df6ad6… was
  rejected for exactly this: 209→177 and 133→128 loads, all subsets).
* Restore client: a job-status poll that hits the client-side deadline is now
  reported as a 12002-class transport timeout and retried until the overall
  deadline, instead of failing the restore (06:28:08 "Host request timed out"
  while the Host went on to finish at 06:37:24). Timed progress trail is kept
  in the final `derived/restore-results/<sha>.json` (`progress[]` with unixMs).
* Aliases: the four 2026-09-08 project alias files were keyed with N duplicate
  `unknown@0` members (old expansion bug); qrenderdoc keys the block by its
  reflected variables (one `unknown`). Collapsed-key copies were added beside
  them in `shared/dictionaries/cbuffer-aliases/` (`rekeyedFrom`); nothing was
  deleted. They hold block aliases only -- member aliases need a fresh AI pass.
* Console header is one row (scope, severity toggles, status, progress when
  active, refresh, clear).

## 2h. Restore vs. live-edit watcher race; stale ABI rejections; timing

Run of 2026-09-11 07:24 (1.rdc, project-target-inventory): inventory 8 s,
then `resource-anchor` for Shader 751 took 147 s and its compile 47 s; the
apply then failed with "workspace no longer contains the expected saved
candidate" because the GUI's live-edit watcher on the same workspace compiled
the restore's own source write as revisions 8/9/10 and applied them itself.
Shader 754 failed in prepare with "candidate ABI report does not prove its
recorded comparison": its rev 0004 rejection was recorded under the exact-set
rule, and re-verification under the new subset rule now disagreed with the
recorded verdict.

* `ShaderWorkspaceWatchManager.suspend_workspace(path)` pauses every active
  watcher of one workspace (`watchSuspended` / `watchResumed` audit events);
  on resume the watcher adopts the on-disk source as its baseline without
  compiling. `project_restore` holds that suspension from the end of
  `_prepare_reuse_workspace` through apply/rollback (`finally`).
* `_validate_abi_report`: a recorded *rejection* whose interfaces and DXIL
  evidence still re-verify byte for byte is accepted even when the current
  comparison rule would now pass it (stale rejection); the revision stays
  rejected until the next save.
* `_prepare_reuse_workspace` emits two timed sub-phases ("Resolving the target
  Shader workspace (bounded catalog export)", "Preparing the target Shader
  workspace (baseline reflection and DXIL evidence)") so the next
  `restore-results` trail says where the 147 s go.

## 2i. Shared-knowledge model written down; built-in unpackers; settings panel

* `docs/SHARED_KNOWLEDGE_MODEL_2026-09-11.md` is the one-page contract for the
  project share: what is stored where, when it is written, how it is applied
  on open, and who yields to whom (restore vs watcher, restore vs user edit,
  AI vs user aliases, duplicate restores, client timeouts) plus the relaxed
  verification rules now in force.
* Built-in unpackers (`src/replaylab_agent/builtin_unpackers.py`): uex
  (CUE4Parse; `--config profiles.json --profile <name> preview|list|search|
  export`, plus the `uex mcp` server), AssetStudioModCLI (`-m info|export|dump`,
  `-t shader,monoBehaviour,tex2d,...`, `--filter-by-name`), UnityPy (bundled
  `unitypy_reader.py` materialised under `%LOCALAPPDATA%\ReplayLab\tools`,
  `describe` = reader JSON contract, `export` = .shader/.material.json/.png),
  umodel (`-list`, `-export -out= -png [-game=] [-aes=]`). Each ships its
  formats and operations; the Settings dialog asks only for the program (+
  profile file/name for uex). `tools tool-list` returns `builtinUnpackers`;
  `tools tool-set` with `builtinKind` builds the full record; `tools
  tool-detect <kind>` finds the program. Recipes carry a built-in proof record
  so every existing "proved only" gate offers them unchanged.
* Settings > AI: top paragraph, "Let Bread AI set this up", "Known servers/
  tools" preset rows and "Let AI work it out" are gone. MCP tab opens a server
  dialog (transport, program/args/env or URL/bearer/headers); Unpackers tab
  lists the four built-ins (program set or not) and opens the per-unpacker
  dialog, or a custom-program dialog. Find Installed routes recognised
  programs into the built-in dialog pre-filled.
* Skill `renderdoc-shader-workbench` v36: unpacked-asset search (retrieval +
  direct search with the user's hints + reading packed containers through the
  configured unpacker's operations) is the mandatory first step before
  organizing or naming.
* Rebuild: host + bridge and qrenderdoc.

## 2j. Local edits are a remembered skip, not a failure; prepare timings

Run 08:20: both Shaders failed with "prepared project source differs from the
current original Shader or active knowledge" -- the 1.rdc workspaces hold
sources that are not the project knowledge (one is the user's `SV_Target.x`
edit, the other the state left by the earlier watcher race). By contract the
local source wins, so:
* `RestoreLocalEdit` turns both guards (`modifiedSinceDecompile`, prepared
  source ≠ knowledge) into a `skipped` item with `reason: local-edit`,
  `workspacePath`, `workspaceSourceSha256`, `knowledgeSourceSha256`.
* `_local_edit_memo_hit`: when the previous record for this capture consists
  only of such skips and nothing moved (same restorable heads, same knowledge
  source hashes, same workspace source bytes), the restore returns at once
  (`restoreMode: local-edit-memo`) instead of re-running the 2.5-minute
  inventory + preparation on every open.
* Open-time summary gains "…%3 restored, %4 kept your local edits instead".
* `prepare_shader_workspace` reuses the admission `status()` for an admitted,
  unchanged workspace (two of four full re-validations gone) and reports phase
  timings (`timings` in the result; console event
  `shaderWorkspacePrepareTimings`) so the remaining ~50 s per Shader can be
  located next run.
* To have the project version applied again in 1.rdc, the user re-organizes or
  resets those workspaces (the restore never overwrites a local source).

## 2k. Editor switch original/compiled; AI re-organize failure; live timers; receipt card

User run 08:47 ("read the unpacked assets and re-organize the HLSL") failed in
two places, both self-inflicted by the user's own open editor:
* "登记 Shader 工作区: project publication requires the exclusive workspace
  source lease" -- `_register_reviewed_workspace_with_readback` tried to take
  the source lease that this process's live-edit watcher already holds for the
  open workspace. It now asks `ShaderWorkspaceWatchManager.owns_source_lease`
  and, when true, borrows the lease by `suspend_workspace(root,
  "project-publication")` for the duration (publication never writes the
  source; it only needs the watcher quiet). A foreign lease is still refused.
* "启动 Shader 实时更新: workspace already has an active live-edit session"
  -- `ShaderLiveEditManager.start` on a workspace with a live session now
  returns that session's snapshot with `joinedExistingSession: true` instead
  of raising (one workspace, one watcher; the second asker gets the id).
* The "1,352 files" card: the task receipt listed a whole Shader workspace
  directory. `task_report.artifact_with_stats` recognises a workspace
  (`manifest.json` + `source/`) and reports `source/shader.hlsl` with diff
  stats against `ai/decompiler-original.hlsl` plus the `ai/*.md|json|txt`
  notes; generic directories skip `jobs/ revisions/ watch/ live-edit/
  toolchain-history/`.

Editor switch (the user's "使用源码 / 现在的 hlsl 编译的代码"):
* Internal Host live-edit endpoint gains two closed operations: `apply`
  (`workspace_path`) and `restore` (`workspace_path`, `replacement_id`).
  `apply` reuses `latestCompiled` when its `sourceSha256` equals the source on
  disk (no recompile) and compiles only a changed source, then runs the normal
  frozen-binding `_shader_live_edit_apply_candidate`; `restore` calls
  `rollback_shader_workspace_shared_resource`. Replies are trimmed to
  scope/replacement/rollback/status (64 KB client cap).
* Bridge `ReplayLabShaderSaveClient.live_edit_operation` POSTs them; the
  workspace header has one button "Use Compiled HLSL" / "Use Original Shader"
  (`_toggle_replacement`, worker thread, `_drain_switch_results`), state per
  document in `appliedReplacementId` (seeded from `liveEdit.lastApply`).
* The restore flow itself was already compile+apply; what the user saw as
  "not replaced" were the local-edit skips of 2j.

AI panel:
* Elapsed counters of running groups/items are named anchors
  (`elapsed-g-<id>`, `elapsed-i-<id>-<n>`); a 1 s timer replaces just those
  characters through QTextCursor (`advanceElapsedText`) -- no `setHtml`, so a
  text selection survives.
* Produced-files receipt: opaque rounded dark card (`#181818`, outline
  `#3c3c3c`, radius 10) painted by `AIRoundedTextBrowser` between
  `replaylab-card-start/end-<n>` anchors; header with count and +added
  -removed; 3 rows visible, the rest behind "Show %1 more files" / "Show fewer"
  (`replaylab-artifacts://toggle/<id>?expanded=`), remembered per message id.

Tests: `test_shader_live_edit_internal.py::test_switch_operations_reuse_the_current_candidate_and_trim_the_receipt`,
`test_shader_live_edit.py::test_one_live_edit_owner_per_workspace` (join).
Translations: 393 project messages; AIConversationView gains "Show fewer",
"Show %1 more files". Both rebuild scripts are needed (Host+bridge and
qrenderdoc).

## 2l. Project version wins; the asset catalog becomes an index; unpacker drop folder

User (after 2k): "加载项目 shader 后应该统一使用项目 shader 进行替换（而不是源
shader）"; "产出文件应该是建立动态索引关系（失效时重新搜索建立），而不是直接拷
贝文件进来"; are the 4 external unpackers configured; explain 08:47 plainly.

Restore policy reversed from 2j:
* `ReplayProjectService.seed_workspace(..., supersede_local_edit=)` and
  `seed_workspace_from_inventory(..., supersede_local_edit=)`: with the flag
  a `modifiedSinceDecompile` destination is allowed; the current bytes go to
  `<workspace>/ai/local-edits/<utc>-<sha8>.hlsl` first (`_backup_local_edit`),
  result carries `supersededLocalEdit {backupPath, sourceSha256, bytes}`.
  `ActiveProjectService.seed_workspace` and server
  `_seed_active_shader_workspace(..., supersede_local_edit=)` (shared body;
  the MCP tool `seed_active_shader_workspace` never passes it) thread it.
* `project_restore`: a differing workspace source no longer raises
  `RestoreLocalEdit`; it emits "Project Shader n/m supersedes a local edit
  (kept as a backup)", seeds with the flag (kwarg passed only when
  superseding, so test doubles keep their shape), and the applied item gains
  `supersededLocalEdit`; `shaders.supersededLocalEdits` counts them. The
  local-edit memo fast path is disabled (old records would have
  short-circuited the new policy). `source_preparation_only` catches the
  validation's RestoreLocalEdit and takes the same seed path.
* Bridge `project_restore_client`: item contract accepts the optional
  `supersededLocalEdit` (applied) and the 2j skip fields; summary string is
  now "...%4 of which replaced a local edit (backup kept in the workspace's
  ai/local-edits)".

Asset catalog (`mcp_host/src/asset_catalog.py`, schema v2):
* Root cause of "解包索引仍在构建 / retrieve timed out": every checkpoint
  rebuilt the whole SQLite from an in-memory record list (3.1 GB for 356k
  files, 1.1-1.4 GB abandoned `.tmp-*` copies), every read re-hashed the
  entire catalog (`_validate_catalog_identity`), `hydrate` re-walked the
  tree, and `.ubulk` bulk data was probed for numeric values (10.8M
  `asset_values` rows).
* Now one live WAL database updated in place (`_CatalogWriter`): per-file
  lookup by path, unchanged fingerprint → generation stamp; changed → delete
  row + derived rows, insert; complete walk → rows with an older generation
  are deleted; references resolved by SQL lookups after the walk;
  `same-content` rebuilt from a single ordered pass. Checkpoints are commits.
  Per-row `row_sha256` verified on delivery (`_row_record`; query/resolve);
  catalog identity = XOR of row digests (content-derived, so an unchanged
  rescan keeps the same `catalogSha256`). `verify()` audits everything.
  `hydrate` writes only the named paths and uses the stored engine context.
  `_BULK_SUFFIXES` are names-only in headers mode; numeric probe only ≤256 KB
  non-bulk files; `numericValues` ≤256 / `numericRuns` ≤64 per asset. Stale
  `.tmp-*` older than 6 h are removed at scan start; a v1 catalog is
  replaced (the tree is the source of truth). `CHECKPOINT_SECONDS` 30.
  Bench: 30k files first scan 7.8 s, rescan 2.1 s, 44 MB, status 3 ms.
* Bridge `_stable_action_name`: catalog `uses[].actionName` drops replay-time
  indirect arguments (`IndirectDispatch(<2, 2, 123>)` → `(<indirect>)`), so a
  re-index of the same capture no longer creates a second 75 MB revision.
  Existing duplicate revisions under `captures/catalogs/e7632cf8…/revisions`
  were left in place (user data).

Unpackers: none of the four programs exist on this machine (`tools.json`
empty). `discovery_locations` now includes
`%LOCALAPPDATA%\ReplayLab\tools\unpackers\<kind>` and its children; the
built-in dialog names that folder with an "Open Folder" button and the
UnityPy dialog says only the pip package is needed.
`agent_asset_readers.py`: removed an exact duplicate older copy of
KNOWN_TOOLS/discovery (the second definition was the live one).

Tests: `test_asset_catalog.py::test_index_is_updated_in_place_not_rewritten`,
`test_projects.py` (supersede seed), `test_project_restore.py::
test_project_shader_supersedes_a_local_edit_and_keeps_a_backup_receipt`.

Two round-18 regressions caught by the full host suite and fixed in this round:
`prepare_shader_workspace`'s timing setup had been inserted above its
docstring (the MCP tool lost its description); the workspace panel's
`_tab_changed` had lost its trailing `_sync_flow_graph / _save_state /
_refresh_current_view` to the new switch methods. Both restored.

## 2m. No exclusive source lock; targeted asset search; unpacker installer

User: why is there a "源码独占锁" at all -- anything may edit the file; the AI
must search unpacked assets by context first and index only what matches,
not build the whole index; download/configure the four third-party unpackers.

* `shader_workspace/leases.py` rewritten: `WorkspaceSourceTransactionLease`
  keeps its API but `acquire()` never refuses -- it writes a participant
  record under `watch/participants/<token>.json` (`participants(root)` lists
  them) and archives any old exclusive `.workspace-watch.lock` as
  `watch/retired-exclusive-lease-*.lock`. `recover_orphaned/recover_legacy`
  are accepted and ignored. Concurrency is settled by the existing source
  CAS (hash the writer read vs. hash on disk) and the manifest transaction
  lock. Server publication no longer borrows/suspends anything. The watcher
  adopts a candidate another writer already compiled from the same source
  (`_already_compiled_candidate` reads manifest.latestCompiled; event
  `compileAdopted`). Tests that pinned exclusivity were rewritten to the
  shared contract (watcher, organization, routes, mcp).
* Asset retrieval is targeted-first: `_ensure_shader_asset_catalog` starts a
  *names* walk on first use (90 s foreground budget) instead of the
  whole-tree headers scan; `_targeted_asset_hydration` turns the draw's words
  (user tokens, bound textures, resource names) into a name search over the
  index, hydrates only those files (+ `.uexp` siblings, ≤48 hits) and then
  `shader_asset_retrieval.retrieve` ranks with full documents.
  `search_tokens` candidates now carry `parseStatus`. Deep indexing remains
  opt-in (`index_active_unpacked_assets` / `wait_for_unpacked_asset_index`).
  Test: `test_shader_asset_gate.py::TargetedHydrationTests`.
* `output/shader-workspace-20260906/INSTALL_UNPACKERS.cmd|.ps1`: downloads
  and registers AssetStudioModCLI (latest GitHub release, net472 zip), umodel
  (gildor umodel_win32.zip), UnityPy (pip into replaylab\.venv), and builds
  uex from source (github.com/Straw1997/uex has no binary releases; needs
  the .NET 10 SDK, offered via winget) into
  `%LOCALAPPDATA%\ReplayLab\tools\unpackers\<kind>`; writes a uex
  profiles.json with `GAME_SilverPalace` (uex's CUE4Parse has that game's
  decryption built in) and the Steam paks folder when found (`-PaksDir` to
  set it), then `tools tool-set` for all four. Each download prints URL,
  size and SHA-256; every step asks unless `-Yes`.

## 2n. Folder-first, content-matched asset retrieval; installer without the GitHub API

User: never search assets by the draw's texture names (they do not exist in a
shipped capture); the AI should read the directory structure (type-filtered),
pick likely locations, then match by texture colour, cbuffer ↔ material
parameters, shader functions, mesh info -- tolerant, no single veto -- then
rename textures/cbuffers/shader from the asset and index the match for next
time. Also `INSTALL_UNPACKERS.cmd` failed on the GitHub API rate limit.

* `shader_asset_retrieval.retrieve(..., scope_prefixes=())`: texture-name
  tokens dropped from ranking (`signals.boundTextures.usedForRanking=false`);
  candidates = user/resource-word hits + value/pixel owners + **every parsed
  material under the scope** (`catalog.list_scope`, `fromScope=True`);
  scoring `(score+1) * max(0.25, 1+0.15*agree-0.25*disagree)` so a
  disagreement lowers, never removes.
* `asset_catalog`: `directory_profile(depth, prefix, kinds)` (counts per
  folder from the names index), `list_scope(prefixes, kinds, class_names,
  parsed_only/deferred_only)`, `hydrate_scope(root, prefixes, kinds,
  max_files)` (headers first, then the `.uexp` of recognised materials; bulk
  never). `search_tokens` candidates carry `parseStatus`.
* server: tools `profile_unpacked_asset_directories`,
  `hydrate_unpacked_asset_scope`; `retrieve_shader_asset_candidates(...,
  scope_prefixes=)`; `_targeted_asset_hydration` uses scope + user words only;
  `submit_shader_asset_resolution(..., links_json=)` stores the draw↔asset
  correspondences in the resolution's evidence and the workspace record
  (returned in `memory.resolution.evidence.links`).
* Skill `renderdoc-shader-workbench` v37: step 2b rewritten (profile → choose
  folders → scoped retrieval → submit with links → rename from the asset).
* Installer: `Latest-Release-Tag` reads the `/releases/latest` redirect (no
  API, no rate limit), API used only when it answers; falls back to known
  file names (`AssetStudioModCLI_net472_win32_64.zip`, then net9 win64).
Tests: `test_shader_asset_retrieval.py` (words+folders, scope seeds),
`test_shader_asset_gate.py::TargetedHydrationTests`.

Addendum 2n: the redirect probe also failed on the user's network ("no latest
release"). `Latest-Release-Tag` now tries, in order, the final URL after
following `/releases/latest`, the `releases.atom` feed, the raw redirect
header, then asks for `-AssetStudioTag <tag>`; `-GitHubProxyPrefix <mirror>`
prefixes every github.com download (and the uex clone) for networks where
github.com is throttled. uex recipe gained `uex_preview_texture`,
`uex_extract_raw`, `uex_export_blender`, `uex_relations`, `uex_discover`;
AssetStudio gained `assetstudio_container_export` and the `animator` /
`movieTexture` / `videoClip` types (timeouts clamped to the 300 s validator
bound).

## 2o. The four unpackers travel with the program (bundle, tokens, auto-registration)

User: the installer failed again (the Y/n prompt swallowed the next typed
command, so AssetStudio was skipped; umodel's gildor.org download answered
404), and the four programs must ship *inside* the product -- bound by
relative directory -- because the build is shared with other people; all
tools, Skills included, live in the program. Also "finish the placeholder"
(the uex `paksDir`).

* Bundle layout (source tree `replaylab/third_party/unpackers/`, deployed to
  `<app>/replaylab/tools/unpackers/` by `packaging/Deploy-BundledTools.ps1`,
  step [3b] of `REBUILD_HOST_AND_BRIDGE.cmd`): `uex/uex.exe` + `uex/profiles.json`,
  `assetstudio/AssetStudioModCLI.exe`, `umodel/umodel_64.exe|umodel.exe`,
  `manifest.json`. `third_party/unpackers/*/` is git-ignored (binaries are
  fetched, the layout is not).
* `replaylab_agent/bundled_paths.py`: `%BREAD_TOOLS%` (frozen: `<exe dir>/tools`;
  source: `replaylab/third_party`; env `BREAD_TOOLS_ROOT`) and `%BREAD_APP%`
  (exe dir / `replaylab` tree; env `BREAD_APP_ROOT`); `expand_tool_path` /
  `fold_tool_path`. Tool registries (agent `agent_tool_providers`, host
  `tool_providers`, MCP `agent_tools_config`) **store tokens** and expand on
  load, so a copied application folder keeps working wherever it lands.
  (`{X}` could not be used as the token: it reads as an undeclared argv
  placeholder and the tool was silently dropped.)
* `builtin_unpackers.sync_bundled_unpackers()` runs on AgentHost start and on
  the `tools` commands: adds a bundled entry for every kind whose binary is
  present (`source: bundled`), updates entries whose folded command/options
  changed, removes bundled entries whose binary vanished, never displaces a
  user-registered unpacker; uex also gets the `uex-mcp` MCP server.
* UnityPy is embedded in the AgentHost: `replaylab-ai unpacker unitypy
  describe|export ...`; `Build-ReplayLabRuntime.ps1` collects the package when
  the build venv can import it (`pip install -e replaylab[all]`, or the
  fetch script's UnityPy step). Registered as `embedded` (no external path).
* Skills were already inside the frozen AgentHost (`_MEIPASS/.agents/skills`).
* `third_party/unpackers/Fetch-Unpackers.cmd` replaces `INSTALL_UNPACKERS.cmd`
  (the old name forwards): non-interactive unless `-Ask` (the prompt that ate
  the next command is gone), umodel fetched with browser UA + Referer and
  non-fatal with manual guidance, `-AssetStudioTag`, `-GitHubProxyPrefix`,
  `-PaksDir`, `-ProfileOnly` (only rewrites the uex profile of an existing
  build: fills a `<put ...>` / missing / non-existent `paksDir`, keeps the rest),
  `Find-PaksDir` (Steam libraries + fixed drives, `Silver|白银`, `Paks` with
  *.pak/*.utoc), then manifest → Deploy-Bundle → `replaylab-ai tools tool-list`.
* Placeholder: no `Content\Paks` path exists in any connected folder
  (`SilverPalace_Unpacked` is extract-raw output with an empty uex inventory),
  so the profile is filled by `Find-PaksDir` at fetch time or by
  `Fetch-Unpackers.cmd -ProfileOnly -PaksDir <...\Content\Paks>`.
Tests: `tests/test_builtin_unpackers.py::test_bundled_unpackers_register_by_token_and_follow_the_bundle`
(token on disk, expanded in memory for agent and host, idempotent, user entry
kept, removal on vanish). Neighbouring agent/host suites: no new failures.

## 2p. Unpacked means no keys: uex profiles are generated, and the build is shareable

User: "this is already unpacked, it should need no key -- why does uex have to be
configured at all?" (uex is the user's own project, `D:\Study\GitHub\uex`), plus:
make sure every tool and Skill is inside the program so a zip handed to someone
else works with no path errors; and the fetch script failed again with
`error CS5001: no static Main`.

* **Why there is nothing to configure.** An unpacked tree is loose files, so
  CUE4Parse mounts the *directory* (`DefaultFileProvider(paksDir,
  SearchOption.AllDirectories)`, every known-extension file becomes an
  `OsGameFile` whose key is its path relative to that directory, `IsEncrypted =>
  false`). The only two facts uex still needs are the mount directory and an
  engine version. Both come from the project.
* `mcp_host/src/uex_profiles.py`: `locate_mount_root` (shallowest directory with a
  `<Game>/Content` child, so `raw/` and not the tree top -- otherwise `/Game/...`
  resolves against nothing), `profile_name_for` (`unpacked-<sha8>` of the mount
  path), `ensure_profile` (writes `%LOCALAPPDATA%\ReplayLab\tools\uex\profiles.json`
  -- per user, never in the program directory, `keys: []`, `usmap: null`, version
  candidates `GAME_<Game>` then `latest`/UE5/UE4), `record_discovery` (a **Strong**
  `uex discover` pins the version and clears the candidate list),
  `uex_relative_path`.
* Host slots: `tool_runner.HOST_SLOTS` adds `assetRoot`, `uexConfig`, `uexProfile`,
  `uexPath`; `build_argv(..., host_slots=)` fills them, `resolve_arguments` still
  refuses them from a caller; `tool_providers.HOST_SUPPLIED_SLOTS` (and the agent
  registry's copy) let an argv name them without declaring a parameter.
  `server._uex_host_slots` derives them from the active project,
  `run_asset_tool_operation` passes them and pins after `uex_discover`,
  `_run_cli_reader_operation` fills `{uexPath}`, and a tool that prints uex's
  export *array* is folded into the reader contract's one object
  (`_describe_from_uex_exports`). `profile_unpacked_asset_directories` reports the
  profile under `unpacker.uex` (with the note that each CLI call re-mounts, so
  many lookups belong on the uex MCP server).
* `builtin_unpackers`: uex's `configPath`/`profile` are now optional and **pak
  mode only** (both or neither, or the registration is refused); with neither, the
  recipes read `{uexConfig}`/`{uexProfile}`/`{uexPath}` and the reader claims only
  `*.uasset`/`*.umap`. The bundle no longer needs `unpackers/uex/profiles.json`;
  `uex-mcp` is registered with a pak-mode config when there is one, else with the
  generated file once it actually has a profile (uex refuses an empty one).
* **CS5001 root cause** (worth remembering): the script published with
  `-o <bundle>\uex` while the clone sat in `<bundle>\uex\src`. `--output` sets the
  published project's `OutputPath`, and MSBuild excludes the output path from the
  default `Compile` glob -- the output directory *contained the project's own
  sources*, so every `.cs` of Uex was excluded and an empty `Exe` compiles to
  exactly one error, CS5001. (ProjectReferences build to their own `bin/`, which is
  why CUE4Parse compiled fine.) Fix: source in `unpackers\_src\uex` (a **sibling**
  of the output), publish into `<source>\publish` the way the project's README
  does, then copy. `-UexSource` builds from a checkout you already have (probed:
  `-UexSource`, `%USERPROFILE%\source\uex`, a sibling of the repo root -- which is
  `D:\Study\GitHub\uex` here), `-UexExe` bundles a build you already made (the
  whole publish folder, since `uex.exe` needs `CUE4Parse.dll` beside it).
  `Fetch-UexNatives` prefetches Oodle + zlib-ng into `uex\.uex-cache` so the shared
  bundle needs no network. `Deploy-BundledTools.ps1` skips `_src`.
* `packaging/New-BreadPackage.ps1`: audits a built application (the three exes, the
  bundled unpackers, `uex.exe` not shipped without `CUE4Parse.dll`, and **no
  drive-letter path in any shipped .json/.ini/.cfg**), then zips it. Everything
  per-machine lives in `%LOCALAPPDATA%\ReplayLab`, Skills are frozen inside
  `replaylab-ai.exe`, tools are addressed through `%BREAD_TOOLS%` -- so unzip and run.
* Two round-21 misses found by the full suites and fixed here:
  `profile_unpacked_asset_directories` / `hydrate_unpacked_asset_scope` had no
  effect declaration (`policy.py`: read-only / effectful+`project`), and the Skill
  named recipe operations the tool sweep did not know -- `test_skill_tool_references`
  now collects the built-in unpackers' operation names instead of exempting them.
Skill `renderdoc-shader-workbench` v38: step 2b(iii) says the unpackers ship with
the program, the Host fills uex's profile, and "no paks / no profiles" means the
project's asset root is not configured, not a missing key.
Tests: `mcp_host/tests/test_uex_profiles.py` (generation, mount root, pinning, the
slots, and that a caller cannot pass one), `tests/test_builtin_unpackers.py`
(unpacked mode, half-written pak profile refused). Full host and agent suites: no
new failures.

## 2q. Version read from a header, mount root from the project, packaging with no step

User, after the fetch succeeded: the mount directory should just come from the
project's unpacked path; the engine version the AI can decide by testing one asset
(newest first) or by asking; and the share package must not need a manual step --
configure the packaging rules so the tools are always in it, in the right folders.

* Mount root = the configured path when it already is one (`<Game>/Content` under
  it), otherwise the shallowest directory under it that is (extract-raw writes
  `raw/`). `ensure_profile` reports which of the two happened (`mountRootSource`).
* **The version is read, not tried.** `FPackageFileSummary` carries
  `fileVersionUE4`/`fileVersionUE5`, and CUE4Parse maps every EGame to exactly one
  such pair (`EGame.GetVersion`); `uex_profiles._UE5_VERSION_TO_GAME` /
  `_UE4_VERSION_TO_GAME` are that mapping inverted (newest EGame that writes a
  number; 5.4 = 1012, 5.5 = 1013, UE4.27 = 522 ...). `detect_engine_version`
  reads up to six packages from different folders (the Host's own summary parser,
  no mounting) and returns the version with its evidence; `ensure_profile` puts it
  in the profile as `detectedVersion` and as the first non-specific candidate, and
  the generic fallback list is newest-first. New tool
  `set_unpacked_asset_engine_version(version=, redetect=)` for the AI's or the
  user's answer: it pins the version and clears the candidates (effectful+project
  in `policy.py`). `profile_unpacked_asset_directories` reports `engineVersion`,
  `engineVersionFromHeaders` and `mountRootSource` under `unpacker.uex`.
* **Packaging.** `New-BreadPackage.ps1` now deploys the bundle into the application
  itself before auditing, so a package can never miss the tools; one click:
  `output\shader-workspace-20260906\PACKAGE_SHARE.cmd`. For the MSI, the whole
  `replaylab\` tree (runtime + `tools\unpackers\`) is **harvested** rather than
  listed: `make_package_win32.sh` runs `heat dir dist/Release<bits>/replaylab -cg
  BreadRuntimeFiles -dr BreadRuntimeDirRef -var var.BreadRuntimeDir -srd -sreg
  -scom -sfrag -ag -g1`, candle takes `-dBreadRuntimeDir=...`, light links the
  extra wixobj; `Installer32/64.wxs` gained `<Directory Id='BreadRuntimeDirRef'
  Name='replaylab' />` and a `<ComponentGroupRef Id='BreadRuntimeFiles' />`. The
  folder is `mkdir -p`'d first so an installer built without a deployed runtime
  still links. **Not verified here** (no WiX, no Windows in this environment); the
  zip path is the tested one.
Tests: `test_uex_profiles.py` gained the version-mapping table, a synthetic package
header (one asset decides the version), the pinning path and the
already-at-its-mount-point case. Full host and agent suites: no new failures.

## 2r. The tools are part of the build, not of a packaging script

User: why is `PACKAGE_SHARE.cmd` a step at all -- an attached program is still part
of the product, and a standard build should do the copying itself.

Right, and the build was already doing exactly this for the runtime.
`qrenderdoc_local.vcxproj` has had `DeployReplayLabIntegration`,
`BuildReplayLabRuntime` and `DeployReplayLabRuntime` after `Build`; the unpackers
were the odd one out. Added alongside them:

* Target `DeployBundledUnpackers` (`AfterTargets="Build"`,
  `DependsOnTargets="DeployReplayLabRuntime"`), inputs = the bundle's kind folders
  and `manifest.json` (named, not a wildcard: `_src` holds a full uex checkout and
  a glob would walk thousands of files every build), output =
  `$(OutDir)replaylab\tools\unpackers\deployed.json`, which
  `Deploy-BundledTools.ps1` now writes as its stamp. `ContinueOnError` + a warning
  naming the re-run (`/t:DeployBundledUnpackers`), because a tool exe can be locked
  by a running session -- the same failure mode the runtime deploy already had.
  An empty bundle warns once per build with the one command that fixes it
  (`Fetch-Unpackers.cmd`); downloads stay out of the build.
* Everything downstream inherits it, because everything downstream is made from the
  build output: `make_package_win32.sh` copies `x64\Release` into `dist\Release64`,
  the zip is that folder, and `heat` harvests `dist\Release64\replaylab` into the
  MSI (2q). So no packaging path has to know the unpackers exist.
* `PACKAGE_SHARE.cmd` is no longer a build step: it audits the built application and
  zips it for handing to someone. `New-BreadPackage.ps1` still re-runs the tools
  deploy, now as a safety net for a hand-assembled folder rather than the mechanism.
* `REBUILD_HOST_AND_BRIDGE.cmd` keeps its step [3b]: it refreshes the runtime
  without building qrenderdoc, so nothing else would deploy the tools on that path.

Verified: the vcxproj parses and its four targets are in order. The MSBuild run
itself and the WiX harvest are still unverified here (no Windows, no WiX).

## 2r+. What two real AI runs got stuck on (transcript review)

The user ran two tasks and sent the transcripts: "reorganize this HLSL" and "can
you read the unpacked assets for this EID". Both produced partial results after
17 and 44 minutes. Reading `%LOCALAPPDATA%\ReplayLab\ai-tasks\*.jsonl` (the
reasoning stream, not just the answer) found six distinct blockers, five of them
ours:

1. **uex could not be called at all.** `--config` is global in uex but
   `--profile` is declared by each subcommand, so `--profile` has to follow the
   subcommand. Every recipe put it before. The AI worked this out on its own
   ("Host 的 argv 顺序确实有误") and gave up on uex. Recipes rebuilt against the
   real CLI (`uex --config X <sub> --profile Y ...`), and the wrong options
   corrected while there: `export` has no `--out`/`--root` (only `--only`;
   files land in the profile's outputDir), `search` takes `--limit`, `preview`
   `--max-bytes`, `preview-texture --out <file> <asset>`.
2. **Indexing starved the whole bridge.** `index_active_unpacked_assets` ran the
   scan inline, so for five minutes *every* tool call timed out, including ping
   ("索引阻塞了整个 MCP 服务器"). It now starts/joins the same background job
   `_ensure_shader_asset_catalog` already used, returns immediately, and takes an
   explicit opt-in `wait_seconds` for callers that truly need completeness.
3. **A 19-digit ResourceId cannot survive a JSON number.** Cursor's ACP client
   parses tool arguments as doubles, so `1000000000000000755` arrived as
   `...800` and every identity check refused it; the AI spent a large part of
   those 44 minutes trying to defeat its own client's serializer. ResourceId
   parameters now accept a **string**, and a numeric value past 2^53 is compared
   as the double it necessarily became (verification of one already-selected
   shader only -- never to choose between candidates). The rejection message now
   names the exact id and says to pass it as a string.
4. **The review budget stopped the job.** Organization is no longer given three
   rounds and a permission prompt: `max_rounds("shader-organization")` is
   `ORGANIZATION_ROUNDS` (400) unless explicitly configured, so it runs until it
   is accepted or the user stops it. Every other review kind keeps its small
   bounded budget.
5. **The rename ledger froze at 512.** `validate_renames` bounded the *accumulated*
   session map by the per-request limit, so after ~500 names no further rename was
   possible -- on a shader with 2,400 anonymous locals, i.e. exactly the case the
   feature exists for. The per-request cap stays 512
   (`MAX_RENAMES_PER_REQUEST`); the merged ledger is bounded by
   `MAX_RENAMES_TOTAL` (20,000).
6. **The Skill was delivered and never read.** `load_replaylab_skill` returned the
   whole 62 KB procedure; Cursor spills a result that large to a file, and its own
   Read tool is denied on that path -- "读取技能文件时遇到权限拒绝", three runs in
   a row. A long Skill now arrives head-first (~11 KB: preamble + the first
   sections) with its section list, and `load_replaylab_skill(skill_id,
   section=...)` returns any section by name.

Also fixed: `HELP_FLAGS` ended with `()` -- probing a registered tool with **no
arguments**. umodel started bare opens its graphical package chooser, which is the
modal "Choose a package to open" window the user saw during an automated read; the
bare probe is gone (`-help` instead) and `UNREAL_LEGACY_PATTERNS` no longer claims
`.uasset`/`.umap`/`.pak`, so cooked UE4/5 files never route to umodel.

Still open, reported honestly to the user: the duplicated unit in "已用时 14 秒 秒".
Traced through `elapsedText`, the `.ts`, the on-disk `.qm` and the catalog embedded
in the running exe (parsed out of the binary): all single-unit, and there is exactly
one seconds formatter per view. Most likely the screenshot came from a qrenderdoc
instance older than the 05:55 rebuild. Needs one live datum to pin.
Also untouched here: "active project selection is locked; its owner could not be
proven inactive" and "project is not registered on this machine", which blocked
registration in both runs.
Tests: `test_skill_gateway.py` (sectioned delivery), `test_builtin_unpackers.py`
(argv shape), `test_active_project_mcp.py` (background index + `wait_seconds`, and
the file-limit contract moved onto the catalog service where it now lives),
`test_shader_equivalence.py` (organization rounds). Full host and agent suites: no
new failures.

### Addendum 2r+: the doubled unit, found; and the recipes proved against the real CLI

The container copy of `qrenderdoc/` was stale, which is why the first pass through
the elapsed label found nothing: the device has the round-18 live timer
(`m_ElapsedTimer`, `advanceElapsedText`, `rebuildElapsedCursors`, the `\x01`
placeholder), and that is where "已用时 14 秒 秒" comes from -- it only ever
appeared *while a task was running*, which is exactly when that timer is the only
thing writing the label.

`rebuildElapsedCursors` stored **one fragment** per anchor (`QHash::insert`, last
one wins). A label like `14 秒` is routinely two fragments -- Latin digits and a
CJK unit resolve to different fonts, so Qt splits the run and both halves carry
the anchor name. The stored span then covered only half the label, and the next
tick wrote the whole new label over that half, leaving the other half behind:
`14 秒` + `秒`. Fixed on the device copy: the rebuild merges every fragment
carrying the anchor into one span, `ElapsedCursor` remembers the exact text it
last wrote, and a tick replaces the span **only** while it still holds that text
-- anything else (a re-render between ticks, a span spelled differently) makes the
tick rebuild instead of overwriting. No new tr() strings, so no catalog change.

Proved rather than argued: `tests/test_uex_cli_contract.py` parses uex's own
`Program.cs` out of the bundle (`third_party/unpackers/_src/uex/...`, skipped when
absent) and checks every recipe against the CLI it declares -- global options
before the subcommand, declared options after it, `--profile` after the
subcommand, no excess positionals. Run against the real source on the device: all
ten recipes pass. A dry run of the whole chain (unpacked tree → generated profile
→ recipe → both registries → Host slot filling) prints the exact command lines and
was verified the same way.

### Addendum 2r+ (2): verified against the real tree, and "assets first" made unavoidable

Run on the user's actual `E:\SilverPalace_Unpacked`, not a fixture:

* The Host's own package parser reads `MI_Vulture01_Body_A01.uasset`: class
  `MaterialInstanceConstant`, parent `MIT_BossPBR_AnisotropyHighLight_Emissive`,
  and its five textures (D / N / MRA / WetMaskSp / AnisoMaskSP). No uex needed
  for that.
* A scoped scan of `ArtRes/CharRes/Boss` (2,219 files, 23 s through the device
  mount) then `list_scope(["Vulture"], class_names=["MaterialInstanceConstant"])`
  returns the 12 Vulture materials.
* **The match works on values alone.** Feeding 40 of `MI_Vulture01_Body_A01`'s
  own parameter values back as constant-buffer lanes, with no name evidence at
  all, `retrieve` ranks it 28.0 / confidence 0.98 against 1.0 for every sibling
  material -- 9 of the 40 values are rare in that catalog. `MI_..._Feather_A01`
  shares 31 values but none rare, and correctly stays at 1.0.
* Word+folder retrieval behaves too: with `extraTokens=["boss","feather","wet"]`
  and `scope_prefixes=["Vulture"]`, `MI_Vulture01_Feather_A01` leads at 8.86 and
  the scope's other materials follow as seeds; without the scope the same words
  spread across every boss folder, which is why scoping matters.
* uex itself was **not** executed (Windows binary, no .NET in either sandbox);
  what is proved about it is that all ten recipes are valid commands for the CLI
  uex declares (`tests/test_uex_cli_contract.py`, run against the vendored
  `Program.cs` on the device: pass).

A value-sharing change to `asset_catalog` (mirroring a `.uexp`'s values onto its
`.uasset`) was written, tested, **and then reverted**: `_owner_path` already maps
payload → package for this lane, so it bought nothing, and storing each value
twice would have inflated `keyPopularity` -- which is what decides rarity, and
rarity is the whole discriminator. The reasoning is now a comment where the
values are written, so it is not "fixed" again.

"Assets first" is no longer only in the Skill (which the AI never read that run):
`_shader_asset_gate` now states `assetsRead`, and when a root is configured with
no resolution yet it returns `requiredFirstStep` plus the ordered
`firstSteps` (profile → hydrate scope → retrieve → uex_preview_asset → submit).
`begin_shader_workspace_organization` lifts `requiredFirstStep` to the top of its
own result, so the tool that must be called to organize is the one that says the
assets come first. The not-ready branch no longer suggests waiting for an index.

Budget and ledger, measured rather than asserted: organization limit 400 (was 3 +
a prompt), 40 consecutive rejected rounds do not exhaust it, other review kinds
still 3; an accumulated ledger of 2,400 renames validates while one request of
that size is still refused at 512.
## 2s. Names from the material: a run that is recognised is a run that is named

User, first pass: once a few values match, the rest of that constant buffer
follows from its structure and order -- name those too; do the same for the
matched textures and keep both in the project's shared library; and let the AI
decide which folders to look in, because folder names and layout change
completely from game to game.

User, second pass, on reading what the first pass built: it was too timid. The
lanes holding `0`, `1` and `0.5` are the ones a reader most needs named, and they
are exactly the ones a value comparison can never reach, because every material
holds them. If the AI has recognised which material a cbuffer is, it should name
**the whole thing, in order**, and mark the names it is not sure of rather than
withhold them -- a marked name still gives the HLSL organising pass a direction;
no name gives it nothing.

That is what the module does now. A cooked `MaterialInstanceConstant` carries
`ScalarParameterValues`, `VectorParameterValues` and `TextureParameterValues`,
each with the artist's own parameter name, and an engine lays the material's
uniform buffer out in that parameter order.

* `mcp_host/src/material_naming.py` (pure, unit-tested): `parse_material_parameters`
  reads the uex/FModel export in any of its three spellings.
  `propose_member_names` first matches parameters against the event's floats -- a
  vector must match all four components, a scalar must be distinctive *and* occur
  at exactly one lane. Each match is an **anchor**: a (lane, parameter index)
  pair. `_align_run` then groups the anchors by `lane - index`; if they all agree
  on one offset, every parameter in the material is named at `index + offset`,
  round numbers included.
* Three bases, and the difference is stated on every proposal rather than
  averaged away: `matched` (this lane holds that parameter's value, distinctively),
  `aligned` (two or more anchors agree on the offset and this lane falls in the
  run), `ordered` (one anchor only -- the offset is a reading, not a proof).
  Everything that is not `matched` also carries `provisional: true` and
  `markedName` = name + `provisionalSuffix` (`_u` by default, caller-settable).
  The marker is a marker, not a convention: `provisional_suffix="?guess"` works
  and the tests pin that it does.
* One case is still refused, and only one: anchors that imply *different* offsets.
  Then the block is not that parameter array laid out end to end, and a name read
  from its order would be wrong rather than uncertain -- nothing is named from
  order and `ambiguous` says "matched lanes imply N different parameter offsets".
  A value found at several lanes is reported the same way instead of being
  assigned to one of them. `fill_gaps=False` returns only what the values prove.
* `propose_texture_names` runs the identical rule on the texture slots: a
  recorded pixel link is an anchor (`matched`), the slots around it are named
  from the material's texture-parameter order (`aligned` / `ordered`, marked),
  links implying different offsets name nothing from order, and with no link at
  all an exact count match is the reading.
* Tool `propose_shader_names_from_asset(event_id, stage, workspace_path=,
  asset_path=, material_json=, fill_gaps=, provisional_suffix=)`: reads the
  resolved material (running the bundled `uex_preview_asset` itself, or taking
  `material_json`), pulls this event's constant buffers and bound textures, and
  returns both proposal sets. It proposes only;
  `rename_shader_workspace_symbols` applies them, and that already publishes
  CBuffer display aliases for every capture of the game that binds the block and
  records `_remember_member_rules` for the material family -- the shared library
  the user is asking for. Texture slots go the same way through
  `submit_shader_asset_resolution(links_json=...)`.
* Skill v40 -> v41: 2b(i) says to work out what this tree's folders *are* rather
  than to look for `Characters` / `Materials` (no convention survives across
  games); 2b(v) now describes matched/aligned/ordered, tells the reader to
  **apply** the marked names rather than skip them, and says in one line that the
  program holds no table of what a name means.
Tests: `mcp_host/tests/test_material_naming.py` (15): parsing, a matched lane,
two anchors naming the whole run *including* the round numbers (`Roughness_u`,
`AnisotropyStrength_u`), one anchor still naming the run but reported as
`ordered`, contradictory anchors naming nothing, `fill_gaps=False`, a vector
needing all four components, a value at several lanes, round numbers never
counting as evidence, a configurable marker, and four texture cases including a
single link naming the whole slot run and two links that contradict each other.

## 2t. No name tables: what the Host may say about a name

User: make sure no skill and no part of the AI logic contains hard-coded name
encodings -- every match is the AI's to make, from what it is looking at.

The rule applied: **the Host may report what it observed; it may not emit a name
or a role the AI did not decide.** A table of words is fine in a skill, where it
is search vocabulary the AI reads after it has already formed a view; it is not
fine in code, where it silently becomes an answer. Swept `mcp_host/src` and the
five skills; three offenders, all removed:

* `rd_wrapper._infer_texture_role` -- twenty keyword lists (`albedo`, `_nrm`,
  `_spc`, ...) plus "BC5 means normal", producing an `inferredRole` attached to
  **every** bound texture the Agent reads, and baked into exported PNG file
  names. Deleted. The entry now carries what is actually known about the
  resource -- name as the capture spells it, format, size, mips -- and the Agent
  decides what the slot is, with the unpacked material in front of it.
* `shader_asset_retrieval._TEXTURE_PREFIXES` / `_TEXTURE_SUFFIXES` /
  `_GENERIC_TEXTURE_NAMES` -- stripped `t_`/`tex_` prefixes and `_d`/`_n`/`_mra`
  suffixes and dismissed names like `white` or `depth` as "generic or too short".
  The tokens they produced were already unused for ranking, but the verdicts
  still reached the Agent as evidence. Replaced by `bound_texture_names`, which
  undoes RenderDoc's own decoration (`Texture2D 1234 <name>`, paths) -- the
  Host's format, not the game's -- and reports the rest verbatim.
* `shader_workspace.flow_graph._default_description` -- ten word lists turning a
  function's identifier into a sentence about what it does ("Evaluates a lighting
  or material-response contribution"), written into the flow-graph node as if
  someone had established it. Now: the entry point says it is the entry point (a
  fact about the program), everything else says `Not yet described.` until the
  Agent describes it through `describe_shader_modules`.

Kept, with the reason: `_VERTEX_ROLE_SIGNALS` (server.py) reports observations
about code -- "passed through normalize()", "used in a cross product" -- next to
the source lines themselves, and its `howToDecide` hands the decision to the
Agent; `asset_catalog._MATERIAL_CLASS_NAMES` / `_TEXTURE_CLASS_NAMES` /
`_BULK_SUFFIXES` and `shader_asset_retrieval._MATERIAL_CLASSES` are Unreal type
names and file extensions -- facts of the container format, not artist
convention; the HLSL keyword sets are language syntax; `is_distinctive_value` is
arithmetic. In the skills, `draw-intent-vocabulary.md` and the GBuffer section of
`visual-evidence.md` are both explicitly search vocabulary read *after* looking,
and the latter is a prohibition against assigning a channel meaning from a name
at all -- both stay.
Tests: `test_shader_asset_retrieval.py` now pins that all four names of
`["Texture2D 812 T_Crow_Body_D", "T_Crow_Body_N", "SceneDepth", "white"]` come
back untouched and unjudged.

## 2u. Anchors: several ways to place a run, all of them heuristics

User: there are many ways to find an anchor -- match the textures a material
references and you have indexed the material; or take a *range* of parameter
values and compare it against a range of cbuffer values, because even when every
individual number is 1 or 0.5 the window as a whole can match. Anchoring should
be heuristic, a reference for the AI, not a hard rule. And use the relationships
between unpacked assets: shader, material parameters, textures are linked, so
matching any part of it anchors the rest. Texture colours and meshes are the most
robust, because they do not change much with format or structure.

Right on all counts, and it exposed how narrow §2s still was: it had exactly one
kind of anchor (a distinctive value equality) and treated its absence as the end
of the road. New module `mcp_host/src/shader_anchors.py` makes "anchor" the
concept and gives it four kinds. An anchor fixes one correspondence; an alignment
is the offset a group of anchors agrees on; naming is what falls out.

* **`value`** -- what §2s had. Cheap, strong, and usually absent.
* **`window`** -- a run of lanes equal to a run of parameters, in order. This is
  the user's idea and it is the one that reaches the round numbers: `1, 0.5, 0,
  1, 2` proves nothing element by element but is rare as an ordered window of
  five. The rarity is *measured*, not asserted: `_offset_votes` collects every
  offset any value pair suggests (a value occurring at more than 32 lanes does
  not get to vote -- it says nothing about position), `_runs_at_offset` finds the
  maximal consecutive agreements, and each anchor carries `competingOffsets` --
  how many other offsets produced a run as long. A run of one repeated value is
  never an anchor (`min_distinct = 2`), which is what keeps eight lanes of 1.0
  from "matching" anything.
* **`texture`** -- a bound texture matched to an asset the material names. The
  most robust kind, exactly as the user said: pixels survive repacking,
  recompression and an engine upgrade while every float drifts. Retrieval already
  proved these (`texture_identity_evidence` -> perceptual hash -> `uses-texture`
  referrers); what was missing was carrying the result forward, so
  `_identity_links` now persists every `same-image` match into the workspace
  record and `propose_shader_names_from_asset` turns them back into slot anchors
  by resource id. No one re-derives what retrieval proved.
* **`agent`** -- `anchors_json=[{"fixes":"cbuffer-lane","lane":12,
  "parameter":"Roughness","why":"…"}]`. The Agent sees the picture, the mesh, the
  marker path, what the code does with a register; the Host does not. Its anchor
  enters the same machinery with the same weight. `normalize_anchors` refuses one
  that names a parameter the material has not, and lists the ones it has.

Two further consequences, both of them the user's "heuristic, not a hard rule":

* **The layout is read, not assumed.** `parameter_layouts` offers the plausible
  orders a cook might have used (scalars only; vectors expanded then scalars; the
  reverse), each is searched, `layoutsTried` reports them, readings that agree
  wherever they overlap are merged, readings that disagree are ranked. The Host
  never asserts an engine's packing rule -- it reports which order the lanes
  actually agree with.
* **A contradiction is a question, not a dead end.** §2s refused outright when
  anchors implied different offsets. Now every offset becomes an alignment with
  its supporting anchors, and `decisive` is true only when the best is *strictly*
  better than the next. A tie names nothing and says so -- and one anchor from
  the Agent settles it. Demonstrated: a block where the same three values occur
  twice yields offsets 0 and 4, both undecided; adding
  `{"lane":4,"parameter":"A"}` makes offset 4 decisive and names the whole run.

The relationship walk the user describes already exists as
`find_unpacked_asset_relations` (references / referenced-by); the skill now says
to use it as the way to *get* an anchor when nothing anchors anything -- match one
thing, walk to its neighbours. The one kind not implemented is the mesh: comparing
a draw's geometry to an unpacked mesh asset needs the asset side parsed, which
only uex can do today. Until then a mesh match is an Agent anchor like any other,
which is why `agent` anchors were built to be first-class rather than a fallback.

Tests: `test_material_naming.py` grows to 22. The new seven: an ordered window
naming six all-round parameters no value could reach; a run of one repeated value
refused; a window that fits in two places reported weak and naming nothing; an
Agent anchor settling exactly that case; an anchor naming a parameter the material
lacks refused with the list of real ones; the layout chosen from the data
(vectors-then-scalars) rather than assumed; and an Agent texture anchor placing
the slot run. Skill v41 -> v42. Full Host suite: 105 failures, identical to the
pre-existing baseline, no new ones.

## 2v. Packing, and what the frame knows without any asset

Two things this round, one from the user and one from §2u's own "next lever".

### The buffer is 16-byte rows, and the padding is real

User: when the material's parameters are handed to the GPU they may be *packed* --
four float properties stored as one float4 -- so check how that works.

Checked, and it mattered. A constant buffer is an array of 16-byte rows and HLSL
packs into them with one rule: a value may not straddle a row boundary. Microsoft's
own wording is "Variables are packed into a given four-component vector until the
variable will straddle a 4-vector boundary; the next variables will be bounced to
the next four-component vector", and DXC's legacy layout says the same from the
other side -- small types pack into the open row at their aligned offset, aggregates
are always row-aligned. (Arrays are the sharp edge nobody expects: by default every
element gets its own whole row, so `float x[4]` is 16 floats, not 4. Material
parameters are not arrays, so that one does not bite here -- but it is the reason
the packing must be modelled rather than assumed.)

So four scalar parameters do share one register -- but the flat lane model §2s and
§2u used was still right for *scalars*, because a lane is a float and four floats
in a row are exactly one register. Where it was wrong is the vector: a float4 must
begin on a row boundary, so any order that puts scalars before a vector leaves a
gap. `shader_anchors._pack` now lays every candidate layout out under the packing
rule and each entry carries an explicit `laneOffset` instead of its position in a
list; `scalars-then-vectors` for five scalars and one colour places the colour at
lane 8, not lane 5, leaving 5..7 as padding no parameter occupies. Without this the
whole run after the first vector is named three lanes early and looks perfectly
self-consistent doing it -- which is the worst possible failure mode here.

The same rule also *eliminates* readings rather than merely disfavouring them: a
run that begins with a four-component value cannot begin off a row boundary, so for
those layouts an unaligned offset is reported `possible: false` with the reason. It
is opt-in (`register_tiebreak`), because it says nothing about a run of scalars,
which pack into whatever row is open, and nothing at all about texture slots. And
when packing rules an offset out while strong anchors still point at it, the
alignment is **not** silently decided from the survivor -- the contradiction is
reported, because either the layout is wrong or one of those matches is a
coincidence, and naming over it would bury that.

What is deliberately *not* encoded is any engine's own material-to-buffer mapping.
UE4 wrote the vector expressions as float4s and then the scalars after them; UE5
records explicit per-preshader buffer offsets and has changed within its own 5.x
line. Pinning either would be exactly the hardcoding §2t removed. The Host lays out
the plausible orders, searches all of them, reports `layoutsTried`, and lets the
lanes choose.

### The frame describes its own buffer

The lever §2u pointed at, now built, and it needs no unpacked asset at all. A block
bound to many draws holds several kinds of thing side by side and they give
themselves away by how they behave:

* `shader_anchors.lane_variance(samples)` -- the same block read from several
  events, per lane: how many distinct values, constant or not, collapsed into
  contiguous `regions`.
* `shader_anchors.material_lane_candidates(same, other)` -- two eliminations, each
  sound on its own and each from a different comparison the *Agent* chooses. A lane
  that changes between draws of the **same material** is per-object data (a
  transform, an instance value, time). A lane that does not change between draws of
  **different materials** through the same shader is a frame or view constant.
  Neither can be a material parameter. The Host does not decide what the draws have
  in common -- it reports behaviour and the Agent supplies the comparison.
* Tool `profile_constant_buffer_variance(event_ids, stage)` to look at the
  partition directly, and `propose_shader_names_from_asset(...,
  same_material_event_ids=, other_material_event_ids=)` to search only what
  survives. `laneNarrowing` in the result says what was removed and why.

The payoff is not just speed: a shorter search makes an ordered window *rarer*, so
the anchors that survive it mean more. Pinned by a test where a material's four
values appear twice in the same block -- once as the real parameters, once by
coincidence inside the per-object region. Values alone name nothing (two offsets,
undecided); one comparison against a second draw of the same material removes the
per-object half and the whole run names correctly.

Tests: `test_material_naming.py` 22 -> 29. Packing: the vector bounced to lane 8
with 5..7 left unnamed; the layout placing the padding; an unaligned vector-leading
offset reported impossible. Frame evidence: lanes partitioned into regions by
behaviour; one draw cannot say anything varies; two comparisons narrowing from both
ends; and the coincidence case above. `profile_constant_buffer_variance` added to
`policy.READ_ONLY_TOOLS`. Skill v42 -> v43. Both suites at their pre-existing
baselines, no new failures.

Sources for the packing rules: Microsoft, *Packing rules for constant variables*
(learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-packing-rules);
microsoft/DirectXShaderCompiler wiki, *Buffer Packing*.

## 2w. Every platform's packing, the frame-to-file differences, and the chain run end to end

### Packing, for real this time

User: when the parameters are handed to the GPU they may be packed -- four floats
stored as one float4 -- handle it for every platform and engine.

§2v had the D3D rule. Two things were still wrong or missing.

*Missing*: the other platforms. `shader_anchors.PACKING_RULES` now holds
`d3d-cbuffer` (4-byte alignment, nothing may cross a 16-byte row), `std140` and
`std430` (GLSL/Vulkan: scalar 4, float2 8, float3 and float4 16), `metal` (C
alignment, same shape as std140 for vectors) and `tight` (an engine that
hand-packs a float4 array and indexes it itself). `_pack` places a layout under
any of them.

The honest finding, and the reason this is not five times the noise: **for the
value shapes a material actually exposes -- single floats and float4 colours --
d3d, std140, std430 and metal all produce the identical layout.** So
`parameter_layouts` computes all of them, groups by the resulting lane placement,
and emits *one* candidate carrying the list of rules it stands for. They part
company on float2 and float3 (D3D puts a float2 at lane 5 where std140 insists on
lane 6) and only then is there a second candidate. `packings=` narrows the set
when the platform is known; the default is all of them.

*Wrong*: `_pack` was aligning every component of a vector to a row boundary
instead of only its first. Caught by laying a real material out and reading the
placement back.

### What differs between the file and the frame

User, same message: look at the differences between the unpacked asset's data and
what RenderDoc actually captures, and handle those too.

Four, each now handled and each *named on the anchor it produced*, because "equal
after an sRGB conversion" is a different claim from "equal":

* **Precision.** `float16` -- a mobile or console buffer stores a half. `near` --
  a value that went through a preshader comes back a few ulp out. `near` buckets
  on the float32 *bit pattern* (64 ulps, plus the neighbouring buckets), not on a
  printed number of decimals: a fixed decimal window is tighter for a big number
  than a small one and splits any pair straddling a bucket edge. Both are on by
  default, because neither involves anyone deciding anything.
* **Colour space.** `srgb-to-linear` / `linear-to-srgb`, off by default: a claim
  about what the engine did on upload.
* **Semantics.** `one-minus`, off by default: engines disagree about roughness
  against smoothness. A guess about meaning, so it waits to be asked for.
* **Type.** A constant buffer is not all floats. `implausible_float_lanes` drops
  the lanes whose bits read as a denormal, a huge exponent or a NaN -- an index, a
  bitfield, a packed pair of halves -- and it keeps zero, because an empty integer
  and a zero parameter look alike and zero is an ordinary parameter value. This
  needs no comparison draws and is always applied.

And one that is not the Host's to fix but is now said out loud in the skill: an
instance stores only the parameters it *overrides*, so a buffer value the instance
does not list belongs to the parent material -- walk to it with
`find_unpacked_asset_relations` and read that too.

`narrowingGaps` is the other half of that honesty. The lane eliminations are sound
in general and wrong in particular: two materials that happen to share a value at
one lane make it look like a frame constant. Any lane the chosen run wanted but
the filter had removed is now reported by name and lane, so the miss is visible
instead of silent.

### The chain, on the user's own files

uex is a Windows .NET binary and neither sandbox can run it, and a 2 GB `.rdc`
cannot be replayed here either. What *can* be done was done, and the part that is
real is worth being precise about.

*Real capture structure*, from the workspace `eid-11402-ps-mainps-v1`: the
material block is an anonymous `uint4[13]` (208 bytes) and `uint4[33]` (528
bytes) -- no member names at all, which is why a lane index rather than a member
name is the right addressing. Reading `renderdoc_mcp_bridge._get_constant_buffer_values`
confirms `floats` is the raw buffer unpacked at every 4 bytes, so lane =
register x 4 + component, exactly the model here. The workspace's own
`packoffset(c2.z)` declarations include a `uint` member, which is where the type
elimination above came from.

*Real asset data*: five cooked `MI_AshleyBoss_*` materials read out of the user's
own tree with a scanning reader written for this check (`ue_scan.py`, scratch, not
shipped -- UE 5.4 reworked `FPropertyTag` into a nested `FPropertyTypeName`, which
is a project of its own to walk generically, so it finds the three tags that matter
by their name ids instead). `MI_AshleyBoss_Body01_A`: 26 scalars, 10 vectors, 9
textures, real artist names (`AnisotropyHighLight01 Color`, `Drop Tilling&Offset`,
`PowerRimLight_LightIntensity`).

**Zero of its 26 scalars is distinctive.** Value equality alone could not have
anchored a single lane in this material -- which is the case the user described
two rounds ago, met in the wild on the first real file tried.

The run: the real parameters laid out under the real packing rule at lane 16 of a
132-lane block (the real b4 size), a per-object transform in front, frame
constants and three integer bit patterns behind, one parameter pushed through half
precision and one nudged a few ulp. Two draws of that material, one of a different
material.

* narrowing: 132 lanes -> 64 (16 per-object, 52 frame constants, 3 non-float bits)
* naming: **64 of 66 parameter lanes recovered, 0 wrong** -- 20 matched (the
  colours), 44 aligned
* the 2 misses: `Fire Color 3` and `RefractionDepthBias`, both 0.0 in *both*
  materials, so the cross-material rule called them frame constants. Reported in
  `narrowingGaps` by name -- which is exactly what that field is for.
* textures: 9 slots, 1 proved by a pixel link, the other 8 named from the run
* retrieval: a catalog built over those real assets, queried with the real
  material's 66 lane values, ranks `MI_AshleyBoss_Body01_A` first at 45.3,
  confidence 0.98, `clear leader`, nothing else listed.

What was *not* proved end to end: uex's own CLI output (the recipes were verified
against its `Program.cs` in an earlier round, 10/10) and a live replay of an
`.rdc`. Those need Windows.

Tests: `test_material_naming.py` 29 -> 36. New: a half-precision round trip; a
value a few ulp out at two magnitudes and a 1% difference refused; a colour
matched only when `srgb-to-linear` is asked for; `one-minus` off by default; the
platform rules merged where they agree and split on a float2; bits that cannot be
a parameter; those bits narrowing the search. Skill v43 -> v44. Both suites at
their pre-existing baselines, no new failures.

Sources for the packing rules: Microsoft, *Packing rules for constant variables*;
microsoft/DirectXShaderCompiler wiki, *Buffer Packing*.

## 2x. The last manual step: proposals to renames

Asked whether the chain was complete end to end -- identify the parameter block,
match, rename members and textures, then organize the HLSL from that. It was not,
by two links, and both only showed up by looking at a real workspace.

**A parameter name is not an identifier.** The real names in this game are
`Drop Tilling&Offset`, `SDF Shadow HighLight Intensity Light`, `Wet Mask`. None of
them can be written into HLSL. `material_naming.hlsl_identifier` now produces the
spelling that can, and every proposal carries both: `name` / `markedName` for the
report, `identifier` / `markedIdentifier` for the source. A vector component gets
its own (`SDF_DividingLine_Color_R`), and collisions are numbered.

**Nothing mapped a lane to the symbol that holds it.** `rename_shader_workspace_symbols`
takes `{old identifier: new identifier}` over the workspace source; the proposals
spoke in (block, lane). `_workspace_declarations` now reads the workspace's own
HLSL and maps `cbuffer X : register(bN, spaceM) { T ident : packoffset(cK.c); }`
to (space, bind, K*4+component), and `T ident : register(tN)` to the slot, so
`propose_shader_names_from_asset` returns a `renames` dict that is ready to pass
straight through. Verified against the real `eid-11402-ps-mainps-v1-alt1/ai/organized.hlsl`:
6 blocks, 385 declared lanes, 23 resource slots -- the 23 matching the capture's
own resource count exactly.

That reading also produced the honest caveat this whole feature needed.
**A cooked shader declares only the lanes it reads.** In that real workspace: b0
11 members, b1 211, b2 16 (of 52 lanes), b3 20, **b4 3 (of 132 lanes)**, b5 124.
So a run of sixty named lanes may have three places to put a name. `renameSummary`
reports `applicable`, `declaredLanes`, and `lanesThisShaderDoesNotRead` with
examples. Those proposals are not wrong -- the lane really is that parameter --
there is simply nothing in this source to rename. Saying so is the difference
between a tool that looks like it failed and one that tells you what it did.

Tests: `test_material_naming.py` 36 -> 40 (identifier sanitising incl. a leading
digit, an all-punctuation name and a collision; both name forms on every proposal;
per-component identifiers; texture proposals carrying `bindPoint`). Skill v44 ->
v45. Host suite at its 105-failure baseline, no new failures.

## 2y. Alias and rename are two different reaches

User, on reading §2x: a lane the shader does not read should still get a name --
the cbuffer name is only an *alias* -- and separately the names actually used
inside the organized shader have to change too, so the code reads.

Both correct, and they are two different mechanisms with different reach. §2x had
only wired the narrow one.

* A **rename** (`rename_shader_workspace_symbols`) changes an identifier in the
  organized HLSL. Reach: the lanes this shader declares, which is only what it
  reads. Effect: the code becomes readable.
* An **alias** (`shader_workspace/aliases.py`) is a display name in RenderDoc's
  constant-buffer panel, keyed by the block's layout SHA and a byte offset. Reach:
  **every row of the block**, read or not, in every shader and every capture of
  this game that binds a block with that layout. Effect: nothing about the shader
  or its reflection changes.

Until now aliases only existed as a by-product of a rename (`aliases_from_renames`
walks the *source's* cbuffer members), so exactly the rows the user is asking
about -- the ones the shader never reads -- could not get one.

* `material_naming.alias_rows` groups proposals into the rows a panel can label
  and composes each label: a row that is one vector says the vector's name once, a
  row of scalars lists them, and an over-long label is trimmed rather than
  dropped. `propose_shader_names_from_asset` returns them as `cbufferAliasRows`
  with `aliasSummary.rowsThisShaderDoesNotRead`.
* New tool `publish_cbuffer_aliases(workspace_path, rows_json, block_alias)`:
  reads the captured alias identity, maps each byte offset to the reflected row
  that covers it, and publishes. A name the user typed in the panel is kept.
  Registered in `policy` as effectful.

The granularity is the **16-byte row**, and that is not a choice: a shipped game
ships stripped DXIL, whose blocks reflect as `unknown[N]` placeholders, one row
per register (`cbuffer_alias_identity.py` says so in its own header). A row is
what a name can sit on, so four scalars sharing a register share one label. Real
example from `MI_AshleyBoss_Body01_A`: 21 rows, `AnisotropyHighLight01 Color` on
its own row, `Drop Scale_u | Drop Distortion_u | Drop Random Mask_u | Ink Str…`
on another.

### A real bug this found

Checking the alias rows against a real material showed 82 lanes named where the
run is 66. Cause: `_merge_readings` only refused a rival reading when it put a
*different parameter on a lane the winner had claimed*. A rival layout can explain
the same vectors at the same lanes and then place the scalars somewhere else
entirely -- `scalars-then-vectors` at offset -12 explained the same 40 vector
lanes as `vectors-then-scalars` at 16, and smuggled in twenty wrong scalar lanes
that collided with nothing. The conflict test now also refuses a reading that puts
**the same parameter at a different lane**, which is the same principle as the
offset contradiction inside one layout: a parameter cannot be in two places. After
the fix the same run names exactly its 66 lanes and nothing else.

Tests: `test_material_naming.py` 40 -> 43 (row composition incl. a vector row and
a trimmed label; the rival-layout smuggling case). Skill v45 -> v46. Both suites
at their pre-existing baselines.

## 2z. Every engine, every API -- and the build command

User: make sure all engines and all platforms (DX, OpenGL, ...) are covered. Also:
`msbuild` is not a command.

### The build

`msbuild` is not on PATH in a plain prompt, and the repo already answers this:
`build-dev.ps1` at the root locates MSBuild through `vswhere`, builds
Development|x64 and deploys. That is the command, not a raw msbuild line:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\build-dev.ps1

### Engines

`parse_material_parameters` was Unreal-shaped. It now tries three readers in turn
and reports which one recognised the document in `parameterSource`:

* **unreal** -- uex / FModel `ScalarParameterValues` / `VectorParameterValues` /
  `TextureParameterValues`, in any of its three wrappers.
* **unity** -- AssetStudio's `m_SavedProperties` with `m_Floats`, `m_Colors` /
  `m_Vectors`, `m_TexEnvs`, accepting `first`/`second`, `Key`/`Value` and
  `name`/`value` pair spellings.
* **generic** -- a plain `{scalars, vectors, textures}`, each either a mapping or
  a list of pairs. This is also how an Agent hands over a material it read out of
  a format nothing here knows, which is what keeps the door open rather than
  requiring a code change per engine.

Vector components are read from RGBA, rgba, XYZW or xyzw, or a plain list, and a
two- or three-component parameter is kept as such -- which is exactly what the
float2/float3 packing divergence in §2w exists for. The failure message now names
all three shapes instead of only Unreal's.

### APIs

A constant block is raw bytes whatever the API: the bridge reads the bound buffer
and unpacks floats at every four bytes, so D3D11/12 constant buffers, OpenGL UBOs
and Vulkan descriptor-set uniform buffers all arrive as the same lane array, and
`std140`/`std430`/`metal`/`d3d-cbuffer` packing is already covered (§2w).

What genuinely differs is that **some blocks are not memory at all** -- OpenGL
default (non-block) uniforms, D3D12 root constants, Vulkan push constants live in
the command stream. The bridge already reported that per block (`status`,
`reason`); `propose_member_names` was silently skipping them. They now come back
in `blocksUnreadable` with the block, its size and why, because "this block has no
buffer to read" is a fact about the API and reads nothing like "the material was
wrong", which is what an empty result looks like.

Tests: `test_material_naming.py` 43 -> 48 (a Unity material; a plain shape; Unreal
still recognised and labelled; the failure message naming all three; an unreadable
block reported while a readable one in the same call is still named). Skill v46 ->
v47. Host suite at its 105-failure baseline.

## 2aa. A build I broke, and a priority order that was a decision in disguise

### C4819

The Development build failed on `AIConversationView.cpp` with C4819 -- "characters
that cannot be represented in code page 936" -- promoted to an error by `/WX`.
Mine. The elapsed-timer fix (§ the doubled-unit bug) carried a comment containing
a CJK character, written as UTF-8 into a file with no BOM. Every other compiled
source in `qrenderdoc` is ASCII, so MSVC reads them as cp936 and never notices;
this one had bytes cp936 cannot decode.

The fix is the one that keeps the codebase's own convention rather than papering
over it: the comment is ASCII now, so the file decodes as cp936 cleanly and needs
no BOM and no `/utf-8`. `AIAssistant.cpp` had one stray em dash from the same hand
and was cleaned the same way. A sweep of every compiled `.cpp`/`.h` under
`qrenderdoc` (excluding `3rdparty`) now reports **zero** files that would trip
C4819. `Translations/ReplayLabProjects.cpp` is full of `·` and is deliberately
left alone -- its own header says it is scanned by lupdate and never compiled.

Also corrected in the previous round's advice: `msbuild` is not on PATH in a plain
prompt. The repo's `build-dev.ps1` finds it through `vswhere`; that is the command.

### The reader cascade was a hidden decision

User, on the same message: matching *recognition* belongs to the Agent, not to
hard-coded rules. Fair, and it landed on something added one round earlier.

`parse_material_parameters` had gained three readers -- unreal, unity, generic --
run in a fixed order, first match wins, silently. That order is a claim about
which interpretation of an ambiguous document is correct, made by whoever typed
the tuple. A document that two readers both parse is not hypothetical: an export
carrying an Unreal block *and* a plain one, or an Agent's hand-assembled hand-off
sitting next to the original fields.

Now every reader runs. `parameterSourceCandidates` lists each one that found
something with its counts; `parameterSource` is the largest reading;
`parameterSourceDecided` states in words why -- "generic read 3 parameters and
unreal read 1, the larger reading was taken", or, when they tie, "nothing here can
tell which is the material, so the first was taken -- look and pass source=".
`parse_material_parameters(document, source=...)` and the tool's
`parameter_source=` overrule it, and an unknown name is refused with the list of
real ones.

The rule this follows is the one from §2t: the Host reports what it observed and
how far it constrains; a choice it cannot ground is handed over stated, not made
quietly.

Tests: `test_material_naming.py` 48 -> 52 (both readers reported on a document
both parse; the caller overruling; a tie described as a tie; an unknown source
naming the real ones). Skill v47 -> v48. Host suite at its 105-failure baseline.

## 2ab. The lifetime probe's unreadable failure

C4819 is gone; the Development build now reaches `BuildReplayLabRuntime` and fails
there. The three sidecars froze fine -- the failure is the post-freeze check,
`packaging/tests/Test-FrozenHostLifetime.ps1`, and what it printed was:

    Where-Object : property ModuleName not found on this object
    PropertyNotFoundStrict

which says nothing. The cause is a PowerShell shape: `$child.Modules` throws when
the target process has exited, cannot be opened, or sits across a bitness
boundary; property access swallows that into `$null`; `$null | Where-Object {...}`
pipes one `$null`; and `Set-StrictMode -Version Latest` then complains about the
missing property instead of the missing process.

The probe now checks `HasExited`, wraps the `Modules` read, and refuses an empty
module list -- each with a message naming the actual condition and the usual
cause. It also reports how many Python runtimes it found instead of just
"expected one".

The usual cause, and what the surrounding log supports: a ReplayLab process left
over from an earlier session. `Build work was preserved because safe cleanup could
not be proven: access to ReplayLabInternal.exe is denied` appears in the same run,
which is a file held open. The detached internal Host is *designed* to outlive its
launcher (that is what this probe tests), so a stale one is exactly the hazard.
`replaylab/output/replaylab` still holds the 05:39 exes, so nothing new reached
the output directory and there is no partial build to deploy -- the build has to
pass.

## 2ac. The lifetime probe sampled a process that had not finished starting

With the readable message in place, the next build said exactly what was wrong:

    Expected one loaded Python runtime in the detached Host; found 0 among 1 modules.

**One** module. A running Windows process has dozens; one means only its own image
is mapped and the loader has not run yet. The probe read `$child.Modules` the
instant `ensure` returned -- and a PyInstaller onefile unpacks itself before Python
is loaded at all, so the sample landed in the gap.

What the probe asserts is a *steady state*: that the extraction directory outlives
the launcher. So it now polls for up to 60 seconds, refreshing the process each
time, and stops as soon as the Python runtime appears -- checking `HasExited` every
round so a Host that dies is reported as dying rather than as a missing DLL.

### And the leak that was poisoning the next build

The `finally` requested a graceful shutdown and waited 20 seconds, then gave up
**without killing anything**. A Host that failed to start (exactly the case above)
therefore survived the build still holding `ReplayLabInternal.exe` open -- which is
the `Build work was preserved because safe cleanup could not be proven: access to
ReplayLabInternal.exe is denied` in the same log, and which then makes the *next*
build fail somewhere unrelated. Each failed run leaked one more.

The probe now kills it after the graceful wait fails, and records `childKilled`.
It started that process itself, in its own private runtime directory and its own
LOCALAPPDATA, so nothing else owns it. A Host that ignored a shutdown request is
already a failure this probe reports; it should not also become tomorrow's mystery.

Before the next build, the leftovers from the two failed runs need clearing:
`taskkill /F /IM ReplayLabInternal.exe /IM ReplayLabMCP.exe /IM replaylab-ai.exe`.

## 2ad. A 32-bit build process cannot see inside a 64-bit Host

Third failure, and this time the message was decisive:

    Expected one loaded Python runtime in the detached Host; found 0.
    Saw 1 loaded module(s) after waiting 60 seconds.

Alive for the full sixty seconds and still exactly **one** module. Not a race --
a process whose modules cannot be enumerated at all.

Checked the PE headers on disk: `ReplayLabInternal.exe`, the venv's `python.exe`
and `q_render_soco_doc.exe` are all x64. And the build's own header says
`MSBuild : ...\MSBuild\Current\Bin\MSBuild.exe` -- Visual Studio's default
MSBuild, which is a **32-bit** process. Everything it launches inherits that:
`powershell.exe` is redirected by WOW64 to the 32-bit copy, and a 32-bit process
calling `EnumProcessModules` on a 64-bit target gets back only the main image.
`Process.Modules` therefore reports exactly one entry, forever, for a perfectly
healthy Host. The probe was structurally unable to observe what it asserts.

Fixed at both ends, because either alone would leave the trap set:

* `build-dev.ps1` now asks vswhere for `MSBuild\**\Bin\amd64\MSBuild.exe` first
  and falls back to the 32-bit one with a visible note. This is the root cause and
  it affects every child process of the build, not just this probe.
* `Test-FrozenHostLifetime.ps1` re-launches itself through
  `%WINDIR%\SysNative\WindowsPowerShell\v1.0\powershell.exe` when it finds itself
  32-bit on a 64-bit OS, and reports that run's exit code as its own. So the probe
  is correct however it is invoked -- from the IDE, from a 32-bit MSBuild, or by
  hand.

Worth keeping in mind for anything else the packaging scripts inspect about a
running process: bitness of the *observer* is part of the contract, and nothing in
the failure message tells you that unless someone puts it there.
