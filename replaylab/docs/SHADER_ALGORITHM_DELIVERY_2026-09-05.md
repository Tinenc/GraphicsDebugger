# Shader 算法整理与编辑器交付记录

本文件续接 `HANDOFF_2026-09-04.md` §9.6。这里记录本轮真实结果，不将自动测试或一次捕获观察扩展为所有模型、所有 shader 的保证。

## 已实现的行为

- API 使用固定版本的 GitHub deepagents；ACP 与 Codex 使用厂家自己的 Agent。旧 planner/reviewer/WorkflowGuard 运行路径已删除。本轮再删除无消费者的五份阶段 3 Python 草稿及孤立导出，保留仍被用户配置和历史会话读取的兼容代码。
- 所有 Agent 必须加载 `replaylab-core`，域 Skill 补充具体工作流程。Host 提供统一分析、模块与流程图模板，检查资源、源码位置及产物，返回严重程度、原因、目标与例子。报告默认最多追问 2 轮；shader organization 有独立的 3 次审核上限。严重错误不能靠耗尽次数放行。
- 流程图使用自上而下的算法数据流。节点定位到函数体内计算语句，数据边来自源码中实际的读写关系。同名局部变量不会跨函数误连；经 helper 传递的 `inout` 字段参与依赖分析。首次显示和自动适应模式下的窗口缩放会整体适应视口；手动缩放或拖动后保留用户视角，适应按钮恢复自动模式。
- 大型反编译函数只加注释或改名字不能声称已完成算法整理。模板要求按有意义的计算阶段拆函数，数学简写须有源码依据；不为消除 `_数字` 名称猜测资源或语义。可复用的数学函数目前可放在同一受版本管理的 HLSL 中；外置 `math.hlsl` 不是此次单文件事务的交付内容。
- 原生着色器编辑器支持 F12、跨已打开文档定位、补全、符号着色与当前词出现位置高亮、基础编辑诊断、编译诊断显示及跳转。编辑后清除过期编译错误。高级语义诊断取决于已配置语言服务；保存时由真实编译器检查，不宣称后台完整 HLSL 语义分析已始终开启。
- 可直接编译的原生源码或工具产物走快速路径；只有真实接口不兼容才恢复 ABI。实质 AI 改写使用有限输出观察。后续用户编辑保留编译、当前接口和替换目标检查，不再要求通过 AI 等价审核。哈希绑定具体版本及证据，不表示两段代码在数学上相同。

## 本次真实算法整理

捕获为 `D:\Study\抓帧\白银之城\6.rdc`，EID 11329，fragment/MainPS，原始 shader SHA-256 为 `3c4afb434934308919e884efa26523f832807f2adc839d3a3754ceb559255205`。

原先核心函数约 2600 行、1911 条执行语句。本轮按原始顺序整理为 12 个主要计算阶段、32 个算法函数，并增加 4 个有源码依据的数学 helper。恢复了 146 个有依据的名字，合并了 23 处同控制块中的单次使用纯标量表达式。未知临时量仍保留编号，避免编造原始含义。入口通过阶段函数组织几何、数据采样、光照和输出处理，不引入 Unity 专属宏或未捕获资源。

已审核源码 SHA-256 为 `3ded73edddac108ae065698b5ecd7c1782570014fbd9d84cb177c95783227ca3`；候选字节码也发生变化。项目知识条目为 `90ad4366bd7e01964972b841893c8f03`。该源码及 12 节点、21 边的流程图已登记进当前项目。

`output/shader-algorithm-20260905/review_shader_workspace_organization.json` 记录了旧的已验证 HLSL 与新整理 HLSL 的真实观察：

- FP16 颜色 8,294,400 个分量，最大绝对差 `0.000244140625`，超过固定容差的分量为 0。
- D32S8 深度/模板共 16,588,800 字节完全一致。
- 回放状态成功恢复，最终门为 `accepted-executable-editable` / `observational-pass`。

这证明该次实际输入下输出在容差内；不证明所有输入等价，也不把不同格式计数相加称为像素数。恢复过程中仍保持物理 CBuffer、资源绑定及输入输出接口。

随后新 Release 会话还直接比较了原捕获字节码与最终整理候选，避免只依赖两次间接观察：`final-original-observation.json` 为 accepted/replayRestored，约 37.9 秒，颜色最大差仍为 0.000244140625、超差 0，深度/模板完全一致。

## 已完成的验证与发布

- Agent 回归：563 passed、4 skipped；最终 Host 回归：1548 passed、13 skipped、583 subtests。最终 Host 回归使用独立临时目录通过；此前默认 pytest 临时目录清理遇到权限错误的运行不记为通过。
- Release 原生编辑器实际检查 18/18 通过，包括自动适应、手动视角保留、算法定位、F12、补全插入、诊断显示/跳转/过期清理。编译错误显示使用注入的 UI 诊断，不能代替实际保存编译结果。
- 翻译目录 4861 条完整，同步检查、完整 QM 构建和 lint 均通过。
- Release 标准 C++ 构建成功，0 warning / 0 error。最终冻结运行时 1.13.0 已部署到 Development 和 Release；审计核对了 54 个冻结模块/入口的代码、15 份 Skill/引用、三份程序的清单身份，以及两套安装各 16 份规范 Bridge 文件，并检查已删除的旧 Agent 模块没有残留在冻结包中。证据为 `final-runtime-delivery.json`。
- API、Codex、Cursor 对同一 EID 的真实只读证据报告均在首次 Host 验收通过，耗时分别约 51、72、39 秒。Claude 实际返回 `Authentication required`，未改变登录状态。此矩阵只覆盖一个证据报告任务，不能声称各家已跑完所有 shader 工作流。详见 `VENDOR_VALIDATION_2026-09-05.md`。

## 收尾验收记录

从旧 Development 捕获切到新 Release 实例时，同一原始 shader 的运行时 ResourceId 从 `1000000000000000860` 变成 `1000000000000000853`。旧 catalog 的精确身份门拒绝继续是正确保护，不能直接改 manifest 或放宽哈希检查；需要使用当前捕获的精确绑定，再从项目复制已审核源码，并重新编译、验证当前 ABI。该步骤和最终编辑/恢复读回另以实际收尾收据记录。

首次 `1.rdc` 跨捕获验证执行了完整帧索引，共 7528 个工作项，在 1342 秒后 IPC 超时、实例注册失效，未取得成功匹配证据。进程仍存在，正常关闭窗口未能在 10 秒内退出，随后只结束该任务创建的只读测试进程；失败收据和进度已保留。没有重复全帧导出，也不将此结果记为跨 RDC 通过。

Release 当前捕获的单事件准备在 24.6 秒返回：首选工具报告无法处理动态 CBuffer 偏移；已有注册备用路线在 15.5 秒内成功。随后导入新 catalog 并从现有项目 seed 已整理 HLSL，`matchMode=raw-exact`，复制约 1.9 秒，源码 SHA 与已审核版本一致，未导入旧候选或 ABI 授权。证据为 `release-rebind-complete.json`。这证明不同运行时资源编号下的项目源码复用，仍不替代另一 RDC 的实际匹配验证。

同仓库另一个任务正在改动 Development 四 API 检查点回放、公共 API 和 SWIG；为避免争用其对象文件，本任务暂停 Development 整合构建，使用已经通过构建与原生 UI 验收的 Release 进行本轮交付。不能将本次 Release 的通过声明为那个任务后续核心改动也已通过。

## 重开与实际保存测试发现的补充修复

1. 自动工具发现原来让源码 Host 和三个冻结入口重写同一个全局配置，适配器命令因此在 Python、Internal 与 MCP 之间改变。现按安装目录隔离，并统一固定到同目录 `ReplayLabMCP.exe`；并发启动受有界锁保护。工作区仍严格核验已选工具文件、参数与校验器，无关 route 或 JSON 格式更新不再单独让旧工作区失效。旧全局配置工作区需一次明确迁移，不能把真实选定工具变化当无关更新。
2. 实际保存测试先提交不存在的标识符，真实编译失败且旧替换保持；恢复相同正确源码后再修改 alpha，暴露 Bridge 在仅更新版本信息时没有同步四个 ABI lease 字段。Host 记录新完整校验，Bridge 却保留旧记录，导致下一版被误拒。补丁让完整新校验续期 lease，而 inline 更新保留已匹配的 lease；不同代码仍检查当前物理接口并执行 checked replace。对应合同测试覆盖续期、inline 保留、伪造拒绝、新字节码替换和 GUI 失败回滚。
3. 冻结 Internal 启动入口现在先配置与 MCP 相同的安装目录工具环境，再导入服务。单独部署运行时不会更新应用的规范 Python Bridge，发布必须同时执行 `deploy_replaylab_integration.ps1`；最终审计因此增加全部 Bridge 文件核验，避免新 Host 配旧 Bridge。

失败实验原源码与实际成功替换均已恢复，证据目录 `final-live-save-20260905-180010-27772-16161b` 明确为 passed=false，不能冒充修复后的通过结果。上述补充修复的最终冻结部署与重跑结果在收尾收据中追加。

修复后实际连续保存已通过：`final-live-save-20260905-190225-34968-b7825b/summary.json` 为 `passed=true/failures=[]`。使用已部署编辑器的 HTTP/CAS 接口，从独立进程提交不存在的标识符，真实编译失败且旧替换保持；恢复正确源码后，将 alpha 改成 0.125，真实候选字节码变为 `40f02b...` 并成功替换；最后源码和候选均恢复到本文件记录的已审核版本。`noAIReviewNeeded=true/aiReviewToolCalls=0`，组织门和审核计数未变。

该轮历史交付工作区为 `D:\Study\抓帧\ReplayLab Project\workspaces\shaders\v2\recovery-11329-algorithm-current-20260905`，workspaceId 为 `fcbc21a6136a4c1caf7a1c022207d6ec`；`final-binding.json` 保存当时具体项目登记和授权记录。Release 测试进程关闭后的 `post-success-exit-confirmation.json` 为 exited=true，未强制结束。此收据只证明进程已消失，不能代替正常 `ExitCode=0`、无 WER 崩溃或重启恢复验证；后续真实崩溃见本文末尾。

`bridge-verified-ui.json` 确认实际文档可编辑、显示文本与规范 HLSL 一致，六个原生 CBuffer 窗口均显示预期别名。但它的整体 `ok=false`：Bridge 的旧读盘图校验器尚未接受新的算法图字段，导致空图。不能将空场景的 `wholeGraphFits=true` 当作真实算法图适应验收。该遗漏需补齐后独立重跑。

## 应用边界与跨 RDC 的最终补齐

Bridge 的读盘图校验器现已对齐 Host 的严格算法图协议。应用编辑器还移除了旧的统一 `codeSha256Equal=true` 入门条件，改为读取 Host 的 exact-code、rename-only、observational-pass 或安全恢复基线记录，同时核对 session、初始源产物及有限观察绑定。首次实质整理的历史记录实际为 `observational-pass/codeSha256Equal=false`，经新 Bridge 只读核验通过，12 节点、21 边可读；原始记录未修改。证据为 `bridge-historical-organization-readback.json`。

项目入库的另一个旧 decision 门也已对齐：审核失败但安全恢复的 baseline 可携带自己的图入库，不能提升失败的 AI 提案。真实 Host review→baseline 恢复→项目注册回归及 28 个篡改拒绝场景已通过。

`1.rdc` 后续有界实测发现，全资源索引虽然不遍历 EID，仍对所有 DXIL 做反汇编和接口分析，290.984 秒只处理到 742/1092，按预算停止且当前/选中 EID 和会话未变。该失败收据保存在 `cross-rdc-single-20260905-191901-a6b20b4f`，没有重复这条慢路径。

新增可选的目标原始 SHA 模式先读取真实字节码身份，仅为命中项计算接口并查代表 EID。它明确返回目标范围，不写完整帧缓存或发布全量 symbol snapshot；读取失败保留未知，空/非法目标拒绝，默认全索引行为不变。

修复后 **真实另一 RDC 源码复用已通过**：`cross-rdc-single-20260905-194254-8484bb99/result.json` 为 `passed=true/failures=[]`。19.015 秒扫描 1092 个原始 Shader、只分析 1 个目标，得到 `1.rdc` 自身的 ResourceId `1000000000000000747`、fragment/MainPS、代表 EID **61523**。随后仅导出该 EID/stage，准备固定注册路线，从项目知识 `c2197452ae865ec76d956d491009ce56` 载入已整理 HLSL，源码 SHA 与 `3ded73...` 完全一致。总耗时 **76.719 秒**，无模型请求，原来的 Present 66327 和会话保持不变。

跨 RDC 实测范围是身份匹配和实际源码载入；未向 `1.rdc` 应用替换，也未借用 `6.rdc` 的 ABI 授权。使用时仍由当前捕获的编译/物理接口检查决定能否替换。测试进程正常关闭退出，未强制结束。

## 主路线接通后的实际结果与失败历史

本节续接上述历史算法和跨 RDC 记录，当前产品路线以 `SHADER_MAIN_ROUTE_2026-09-05.md` 为准。生产 restore 已接有界目标原始 SHA 查询；已有项目源码可在精确 raw/ABI/目标 profile 绑定后直接进入可编译工作区，不再要求先反编译成功再 seed，也不要求重新 begin/review/register 才能显示原分析和算法图。

已部署公开 MCP 对 `6.rdc` EID 11329 的源码优先 prepare、当前 compile/ABI/apply、live start 实际通过，0 反编译、0 模型、0 organization、0 重复 register。产品管理的工作区为 `...\workspaces\shaders\v2\08b16a6460d02c538896d2fec5d028a8fe769bf374c2ef8997b61ed818606283`，workspaceId `8972028a5ac14b8ab0ab0ef550289fca`，源码仍是本文的 `3ded73...`。本轮真实 WinHTTP/CAS 保存测试通过了错误编译保留替换、alpha 修改与恢复，项目来源 receipt 和不可变输入不变，未新建组织 gate。完整收据为 `output/shader-main-route-20260905/product-live-save-20260905-205147-38708-a0df6d/summary.json`。

主路线完整回归为 Host **1604 passed / 13 skipped / 596 subtests**、Agent **563 passed / 4 skipped**；冻结包审计核对 **81 个模块、15 份 Skill/引用**。这些是下面 WER 修复之前已完成的验证快照，不替代新修复的构建和真实运行验收。

修复 Internal 的旧实例绑定后，冷启动 PID 33096 和同一 Internal PID 43960 下的热启动 PID 12692，均在任何 MCP 调用之前自动创建属于当前实例/session 的新 liveId，durable authorization 保持，旧 replacement 不继承。两份 `restart-final/automatic-resume.json`、`restart-hot/automatic-resume.json` 均通过；公开 restart 收据随后再次核对同一工作区和单事件 catalog。该轮自动恢复本身已通过，不能单独证明恢复后的保存；最终独立保存证据见本文末尾。

算法图的窄节点、重复说明和大间距已修正为更紧凑的 UI 投影；接受的 graph 和计算位置未改。`product-ui-final-hot.json` 为 `ok=true`：547×1097 视口以约 **1.033** 比例整体显示 12 节点 / 21 边，12 个计算节点逐一跳转成功，六个原 CBuffer 外部别名及真实数值模型全部加载。这更新此前 `bridge-verified-ui.json` 的空图失败状态，但不删除原失败文件。

随后 WER 证实两类真实崩溃：PID 50640 在图拖动时因 `setSceneRect → scrollbar → mouseMoveEvent → setPos` 递归栈溢出；PID 50500、33096、12692 在集成退出清理时因 Shiboken 包装再次销毁原生 QObject 而 `purecall/abort`。相关本地 dump 已用微软 DbgEng 得到 `crash-<PID>-stack.txt`。退出栈不能当作正常退出，也不证明是 GPU 或 shader 算法造成。

图已修复为“拖动期间固定范围、释放后合并更新、防重入”；原生删除前撤销所有权并使 Python Qt 包装失效，避免二次析构。图组件/console/Host graph 测试为 **81 passed / 94 subtests**，原生专项 **48 passed / 45 subtests**，翻译同步保持 4861 条完整。`native-probes-20260905-223458.json` 已实际验证向场景外拖动、缩放/刷新/fit、连续 32 次原生编辑器创建销毁和 GC，PID 23724 正常退出 **ExitCode=0**；这些项目不再待验收。

PID 12692 的 `resumed-live-save-20260905-213647-47520-8b05f8/summary.json` 仍为 **passed=false**：已保存 alpha 修改，随后 GUI/服务不可用，未验证新捕获中的实际替换，也未由该探针恢复源码和候选。后续恢复和最终通过另写收据，不得用自动恢复或截图通过替代该失败。

随后 PID 43572 在加载捕获时因新 qdoc 与旧 core 的虚表接口错配崩溃：`AddFakeMarkers` 的 `0x170` 位置调用成 `FetchCounters`，缺少参数而访问 `0x10`。独立 dump 栈和反汇编已保存，不归因于 shader ABI 或原生退出修复。启用项目依赖重建匹配 Release core/qdoc 已 **0 警告、0 错误**通过，原失败 cycle 保留。

一致构建的 `native-fixed-cycle-20260905-225736` 中，cold PID 40148、hot PID 42432 共用 Internal PID 47812；两次都在 MCP 前自动恢复，公开绑定和 UI 全通过，cold 正常退出 `ExitCode=0`。hot 的保存探针 `resumed-live-save-20260905-225845-32088-d1289e/summary.json` 却因 Host heartbeat stale 报失败，此时 GUI 和 Host 仍在运行。根代理随后实际读回 alpha 候选 `40f02b...` 在约 54 秒后以 revision 7 应用；这不改变该探针及 cycle 的 `passed=false`。

另一次真实 WinHTTP/CAS 恢复见 `hot-source-restoration-20260905-230609.json`；源写回不单独证明候选应用。该 cycle 的 `restored-normal-close.json` 独立确认源码 `3ded73...`、revision 8 的原候选 `249ad5...` 在当前热捕获实际替换，hot PID 42432 正常退出 `ExitCode=0`。该次剩余的 owner monitor 阻塞等待暂停心跳问题随后独立修复并重做整轮，不将分段成功提升为原探针通过。

同目录 `closed-hot-live-audit.json` 已固定保留 revision 7/8 的两次 `applySucceeded`，均实际执行新鲜物理校验，分别为 full-replay-reflection 和 inline-live-physical-interface。其最后 capture no longer current/stop 是正常关闭后状态，不是另一次原生崩溃。

## 最终公开主流程与热重启保存通过

Host owner reconciliation 改为独立单 worker，避免长任务阻塞心跳；92 项专项通过，含真实阻塞 5.35 秒期间的连续心跳。最终 Runtime 构建、两配置部署和 `check-heartbeat-final-runtime.log` 均通过，核对 81 个模块和 15 份 Skill/引用。最新无并发打包全量 `tests-host-heartbeat-final-clean.log` / XML 为 **1614 passed / 13 skipped / 598 subtests，124.21 秒，ExitCode=0**。此前公共 Temp 清理失败、隔离一轮假校验器超时和该测试两个参数重跑通过均保留。

`native-fixed-cycle-20260905-231920/summary.json` 为 **passed=true/hotSavePassed=true**：cold PID 42692 和 hot PID 53756 共用 Internal PID 54764，MCP 前自动恢复、公开状态与实际 UI 全通过，两次正常退出均 **ExitCode=0**。

`resumed-live-save-20260905-232030-24472-033a39/summary.json` 为 **passed=true/failures=[]**。初始无 replacement，独立安装版 WinHTTP/CAS 保存 alpha 修改，候选 `40f02b...` 实际应用于新热捕获；随后源码精确恢复为 `3ded73...`、候选恢复为 `249ad5...`，项目来源与不可变输入保持。`intentionalEditApplied/restoredSource/restoredCandidate/newCaptureReplacementVerified` 全为 true，**0 次显式 start/apply、0 次 AI review**。

本轮 Shader 主流程已实际验收完成，历史失败不改写。此结论不扩展为所有 shader 或所有输入的语义等价；API/Codex/Cursor 真实报告通过不等于完整厂商 Shader 矩阵，Claude 认证仍缺失；基础编辑器功能不称为完整 HLSL 语义语言服务器。跨 RDC 实测证明原始身份匹配及已整理源码载入，替换仍须当前捕获的 ABI 与作用域授权。

最终 cycle 的 `final-source-state.json` 再次核对 revision 10、源码 `3ded73...` 和候选 `249ad5...`，新 qdoc WER 事件 0。两个验收 GUI 正常关闭，用户已整理源码保持。
