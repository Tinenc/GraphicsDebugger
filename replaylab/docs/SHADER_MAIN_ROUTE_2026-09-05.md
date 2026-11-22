# Shader 主路线复核与收敛

本记录响应“先从头梳理主路线，再执行”的要求。先读 `HANDOFF_2026-09-04.md`，再按 `REBUILD_PLAN.md` 核对实际入口、工作区、项目与编辑器；不把 `output/` 下人工串接脚本的成功当作产品入口已完成。

最终状态：公开源码优先主路径、冷/热自动恢复、真实编辑保存/替换/恢复及正常退出已实际通过。最终整轮收据为 `native-fixed-cycle-20260905-231920/summary.json`，详见本文末尾；以下保留问题定位和失败历史。

## 路线决定

API 继续使用 GitHub deepagents，ACP/Codex 继续使用厂商原生 Agent。统一 Skill、Host 模板和有界验收保持，不再增加自研推理调度器。

```text
选中的捕获 / EID / shader
  │
  ├─ 项目已有可靠源码 → 直接载入 HLSL 和原有分析 / 流程图
  │                       │
  └─ 首次处理 → 原生源码或已注册工具 → 可编译源码
                  │                   │
                  └─ 实际 ABI 不兼容才修复
                                      │
                    首次需要整理时：AI 按计算阶段整理、填模板
                    编译 / 接口检查，实质改写做有容差的有限输出对比
                    严重问题退回，次数用尽仍不能接收错误提案
                                      │
                    接受的源码、说明和算法图保存到项目
                                      │
                当前捕获编译 / ABI → 替换 → 持久编辑工作区
                                      │
                用户以后保存：编译 / 当前接口 / 替换目标检查
```

“只有字节码”不能决定是否修复 ABI；工具能够直接正确转换的字节码走工具路径。精度、采样、编译器优化造成的小差异由输出容差处理。数值观察覆盖记录的捕获输入，不承诺数学上的所有输入等价。分段以完整计算阶段为单位；不逐行重复验证。

## 为什么先前会反复绕路

1. 适配器固定到了整个 `ReplayLabMCP.exe`。这只解决了入口名称漂移，Host 中报告或项目代码的重打包仍会改变适配器 SHA，进而强迫工作区迁移。真正应固定的是 shader 适配器执行程序及实际编译器依赖。
2. 项目复用要求先成功反编译，然后覆盖为项目 HLSL。已有可靠源码不该被当前反编译器能力挡住。
3. 组织审核的随机密钥随 Host 进程退出消失，已完成记录因此不能再次作为发布凭证；未完成会话应保守失效，但完成源码的可信记录应持久保存。
4. 快速目标 Shader 查询只接到测试脚本，正常项目恢复仍调用全资源分析。这次真实 `1.rdc` 测试证明了方法可行，尚不能单独证明产品入口已使用它。
5. 编辑器还要求本工作区存在一次组织审核。项目源码直接复用后，必须读取项目自身的源码凭据和算法图，而不是为了显示再开一次 AI 整理。

## 三种记录分开

| 记录 | 保存的内容 | 何时需要更新 |
|---|---|---|
| 源码资产 | HLSL、分析、图、项目知识及已接受快照 | 用户或 AI 实际修改源码 / 说明 |
| 编译结果 | 源码版本、真实编译工具与参数、生成字节码 | 源码或实际编译工具改变 |
| 当前捕获授权 | RDC 会话、资源编号、当前物理 ABI、替换及保存授权 | 捕获 / 会话 / 替换目标或候选改变 |

Host 升级本身不使源码失效。Hash 继续用于识别这三类记录的具体版本和过期证据，不作为任意两份源码“必须相等”的要求。旧候选不能因更新工具绑定而获得新授权。

## 本轮收敛范围

只修上述入口和生命周期问题，再做一次产品主路径验收；不重复已有的原捕获→最终候选数值对比，也不再通过复制工作区、原样重新组织来适配每次打包。保留所有原始失败收据。

当前已完成的组织结果在 Host 私有目录保存完整接受快照的摘要。重开进程只能识别完全一致的完成记录；未完成会话、缺少本地凭据的历史文件、被改动的记录不能借此通过。当前源码、图、候选和 ABI 的检查仍独立执行。这是本机 Host 的已接受记录，不是跨机器签名或对任意源码的等价证明。

最终实现、回归和真实产品验收结果在本文件末尾追加；历史交付记录继续保留在 `SHADER_ALGORITHM_DELIVERY_2026-09-05.md`。

## 冻结代码与部署验收

本轮 Host 全量测试：1604 passed、13 skipped、596 subtests；Agent 全量测试：563 passed、4 skipped。翻译同步通过，4861 条完成、0 条未完成。来源核验覆盖 81 个冻结模块和 15 个 Skill / 引用文件。这是下述 WER 崩溃修复前已完成的完整回归和部署快照；后续修复不自动继承尚未执行的实际退出、拖动和保存验收。

`ReplayLabShaderTools.exe` 现为独立执行程序，只打包适配器及其必要运行环境。Host 重打包不再改变 shader 工具身份；真实编译器或适配器变化时，公开接口支持在原工作区刷新工具绑定，保留源码及工作区身份，清除旧候选授权。已导入项目源码的凭据也随刷新保留。

四个运行程序及 canonical Bridge 已部署并核验到 Release 和 Development。首轮构建中独立工具缺少 Windows 用户目录环境，已修复并验证真实构建和缓存复用；一次部署顺序调用提前退出、一次文件占用被部署器拒绝，均保留失败日志，补部署后的完整来源检查通过。没有强行终止其他任务或覆盖其渲染器改动。

证据目录：`output/shader-main-route-20260905/`。`final-runtime-delivery.json` 为冻结包与两配置部署来源检查；上述完整测试快照为 `tests-host-final.log`、`tests-agent-full.log`。真实 GUI 产品主路径结果单独记录，不由离线测试替代；WER 和心跳修复后的最终构建、部署和验收见本文末尾。

## 真实产品路径与重启中发现的问题

新 Release 实例加载 `6.rdc` 后，实际部署的 `ReplayLabMCP.exe stdio` 公开接口成功定位 EID 11329 / fragment / MainPS，并直接载入项目 HLSL：定位 17.328 秒、prepare 3.468 秒；反编译、模型、organization 和项目注册调用均为 0。新工作区由产品入口返回，未手工拼接迁移目录：`D:\Study\抓帧\ReplayLab Project\workspaces\shaders\v2\08b16a6460d02c538896d2fec5d028a8fe769bf374c2ef8997b61ed818606283`，workspaceId `8972028a5ac14b8ab0ab0ef550289fca`。

编译 1.203 秒，首次实际回放 ABI 与资源替换 54.750 秒，持久 live edit 启动 18.594 秒。`product-prepare-summary.json`、`product-deliver-summary.json` 均 passed。保存测试使用安装版编辑客户端的真实 WinHTTP/CAS 接口：未知标识符确实编译失败并保留旧替换；alpha 从 0 改为 0.125 后实际应用，再恢复最终 HLSL / 原候选；0 次 AI 审核，项目来源凭据和 immutable input 不变、没有新建组织 gate。证据为 `product-live-save-20260905-205147-38708-a0df6d/summary.json`。

正常重开 GUI 后，真实面板已读回 canonical 可编辑文档、12 个节点 / 21 条边，12 个节点逐一通过实际点击信号跳到当前计算代码；六个外部 CBuffer 别名及实际数值模型全部加载。`product-ui-restart.json` 为 `ok=true`。首次 UI 探针因路径 / ID 参数混用超时，原失败保留；第二次才是正确参数的真实读回。

重启验收又发现了两个问题：Internal 后台进程仍在 idle grace 内存活时，只恢复一次持久会话，导致新 GUI 的 list 返回旧 running liveId；12 层图虽然整体 fit，但 76px 层间距和重复说明使文字过小。两处已完成有界修复并得到下面的实际结果。原失败 `product-restart-summary.json` 仍保留，不能覆盖为成功。

## 已通过的自动恢复与真实算法图

修复后的冷启动 PID 33096 自动创建 L1 `cc9c3a1aabba49d6b1600af380ad6b8c`；仅关闭 GUI 后热启动 PID 12692，在同一 Internal PID 43960 下自动创建 L2 `994a0e473ac74ab59c42a614ecca07b9`。两次均在任何 MCP 调用之前读到新实例、新 capture session 的 running 会话，保留同一个 durable authorization，不继承旧 replacement，源码仍为 `3ded73...`。证据为 `restart-final/automatic-resume.json`、`restart-hot/automatic-resume.json`，均为 `passed=true/mcpCalls=0/explicitStartCalls=0/explicitApplyCalls=0`。

之后的两份 `product-restart-summary.json` 又通过公开 context、工作区定位、list 和 status 核对同一工作区。定位仅覆盖 EID 11329 / fragment 的小 catalog，`fullFrame=false`。这证明自动恢复与当前绑定，尚不能代替恢复会话中的实际保存应用。

算法图只改变 UI 布局投影：节点宽 420px、最小高 64px、层距 22px、标题至少 10.5pt；完全重复的标题和说明只显示一次，非重复说明仍保留。接受的图、源码范围及数据边未改。`product-ui-final-hot.json` 为 `ok=true`：547×1097 视口下比例为 **1.0330812854**，12 节点 / 21 边整体显示，12 次计算位置跳转、六个外部 CBuffer 别名及真实数值模型均通过。该截图结果不证明拖动和退出安全。

## WER 证明的崩溃与冻结修复

本地 WER 与 `CrashDumps/q_render_soco_doc.exe.<PID>.dmp` 证明了两个独立的真实崩溃，不再把进程消失笼统记录为正常关闭：

| 实例与时间 | 转储结论 | 已冻结的修复 |
|---|---|---|
| 50640，21:28:21 | `c00000fd` 栈溢出；图拖动的 `setPos → itemChange → setSceneRect → scrollbar → scrollContentsBy → mouseMoveEvent` 同步递归 | 节点移动只合并排队更新；mouse grab 期间固定 sceneRect，释放后提交；增加重入保护，范围不变时不重设 |
| 50500，21:20:48；33096，21:32:56；12692，21:37:44 | 同一退出栈：`ShutdownInternalIntegrations → QWidget.close → SbkDeallocWrapper → QObject::~QObject → purecall → abort`，`c0000409/FAST_FAIL_FATAL_APP_EXIT(7)` | 原生编辑器删除前撤销 Shiboken 包装所有权并使包装失效，防止最后 Python 引用释放时再次销毁同一 QObject |

栈文件为 `crash-50640-stack.txt`、`crash-50500-stack.txt`、`crash-33096-stack.txt`、`crash-12692-stack.txt`。使用本机微软 DbgEng 读取既有转储，没有启动被调试程序。退出栈证明清理阶段崩溃；它本身不能解释最初为何进入退出流程，也不能归因于 GPU 或模型。

PID 12692 的热重启保存复验明确失败：`resumed-live-save-20260905-213647-47520-8b05f8/summary.json` 为 `passed=false`。它初始没有 replacement，CAS 已提交 alpha 修改后的 `70b41d...`，随后 GUI/服务不可用，未证明新捕获实际替换，探针也未能完成恢复。项目来源和不可变输入保持，但这不能提升该次保存为成功；后续源恢复应单独留收据。

图修复后的组件、console 和 Host graph 回归为 **81 passed / 94 subtests**。原生编辑器契约、面板和通用契约专项为 **48 passed / 45 subtests**；翻译同步仍为 0 新文案、4861 条完成。最终运行时来源检查重新通过，81 个模块、15 份 Skill / 引用及两配置 canonical Bridge 一致。

## 原生修复实测与一次构建依赖错误

`native-probes-20260905-223458.json` 为 `passed=true`，PID 23724：真实 Qt mouse grab 下向场景范围外拖动八次，拖动期间范围固定，松开后更新，手动缩放、刷新位置记忆、整体 fit/reset 全部通过；实际原生编辑器连续创建/销毁 32 次，两种父引用状态交替，已删除包装访问被拒绝，GC 安全，父 stack 仍可用。最后通过正常关闭得到 **ExitCode=0**。此前 PID 53992 探针使用启动脚本的代理 context，返回空 editor；改用产品 Bridge 的真实 `_ctx` 后重测通过，没有为测试改变产品接口。

首次带捕获复验 PID 43572 在加载阶段失败，没有到达保存、没有改动源码。`crash-43572-stack.txt` 与 `crash-43572-caller.txt` 证明：新 qdoc 的 `AddFakeMarkers()` 调用 IReplayController 虚表偏移 `0x170`，却进入旧 core 的 `FetchCounters()`，缺少参数而访问 `0x10`。这是跳过项目依赖后混用新界面与旧 DLL 的二进制接口错配，不是 Shader ABI、GPU 计数器业务或退出修复故障。失败目录 `native-fixed-cycle-20260905-224221` 保留。

已启用 qdoc 项目依赖，以 `/m:2` 和明确 `SolutionDir` 重建匹配的 Release core；日志 `build-qdoc-native-with-dependencies-release.log` 为 0 警告、0 错误。并行任务关于“近期未改公开接口”的说明不能替代对已有部署版本的核验；公开头或其既有二进制来源不确定时，不得跳过项目依赖。

此前中断保存的源码通过真实 source transaction lease 和精确 CAS 恢复，收据 `source-restoration-20260905-134825-70cb9d34.json`；随后安装版公开 MCP compile 恢复候选，`recompiled-restored-source.json` 为 passed，revision 6，源码 `3ded73...`、候选 `249ad5...`，没有写入伪造的回放接受状态。

## 一致构建后的冷/热复验与 Host 心跳阻塞

`native-fixed-cycle-20260905-225736` 使用匹配的 Release core/qdoc，冷启动 PID 40148、热启动 PID 42432 共用 Internal PID 47812。两阶段的 `automatic-resume.json` 均在 MCP 调用前取得当前实例/session 的新 liveId，公开 restart 和实际 UI 收据全部通过，仍为 12 个计算节点跳转、完整图和六个 CBuffer 别名。冷会话正常关闭，`exitCode=0`。这是原生修复后新的带捕获证据，不再将实际拖动或 32 次编辑器销毁列为待执行。

热会话的保存探针 `resumed-live-save-20260905-225845-32088-d1289e/summary.json` 仍为 **passed=false**，整个 cycle 的 `summary.json` 也保持失败。它没有预先 start/apply/AI review，初始无 replacement，真实 WinHTTP/CAS 已提交 alpha 修改；随后 poll 和恢复请求因 Internal Host heartbeat stale 被拒。此时 GUI 与 Host 进程仍在运行，不能将 stale 诊断当作进程崩溃。根代理随后实际读回 revision 7 的 `40f02b...` 候选已在约 54 秒后应用；这证明修改最终生效，但不把先前探针的失败改成成功。

根代理另经真实 WinHTTP/CAS 恢复 `3ded73...` 源码，`hot-source-restoration-20260905-230609.json` 记录写入和编译调度成功；该收据自身的 `candidateApplyVerified=false` 不冒充替换验证。随后 `native-fixed-cycle-20260905-225736/restored-normal-close.json` 独立确认当前热捕获 session 的 revision 8、原候选 `249ad5...` 已实际替换、源码恢复，并正常关闭 PID 42432，**exitCode=0**。这些恢复收据补全用户状态，不覆盖失败历史。

同目录 `closed-hot-live-audit.json` 保存稳定审计：sequence 4→5 记录 revision 7 在 53.983 秒完成，使用 `full-replay-reflection`；sequence 7→8 记录 revision 8 恢复约 22.4 秒，使用 `inline-live-physical-interface`。两次均为 `applySucceeded/freshPhysicalValidationPerformed=true`。sequence 9→10 的 capture no longer current 与 stop 发生于正常关闭之后，不是新原生崩溃；顶层停止后的 `activeReplacement=null` 也不抹掉此前真实应用。

该次剩余故障已定位为 Internal owner monitor 等待阻塞任务期间暂停心跳，长耗时 apply 因而被其他入口误判 stale。它需要独立修复和重新完成整轮验收，不能用此前分段成功提升失败收据；最终结果如下。

## 最终主流程验收通过

Host 将 owner session reconciliation 放入独立的单 worker，只允许一个在途任务；心跳循环不再等待长时间的工作区协调或回放锁，HTTP worker 也不承担这项后台等待。相关 92 项专项通过，包含真实阻塞 5.35 秒仍持续心跳的测试。`build-runtime-heartbeat.log`、两配置 `deploy-heartbeat-*.log` 和 `check-heartbeat-final-runtime.log` 均通过，最终核对 81 个冻结模块、15 份 Skill/引用，未因 Host 重打包改变独立 shader 工具身份。

新的 `native-fixed-cycle-20260905-231920/summary.json` 为 **passed=true/hotSavePassed=true**。cold PID 42692 和 hot PID 53756 共用 Internal PID 54764，先后获得 liveId `4eef72b130d74e8f8a52f16b95b1e177`、`0a152f7c43d1483cb91b9b3d7590a006`。两次 `automatic-resume.json` 均在任何 MCP 调用前恢复，公开 context/path/list/status 和实际 UI 全通过；两次正常关闭均在 20 秒内退出，**ExitCode=0**。

`resumed-live-save-20260905-232030-24472-033a39/summary.json` 为 **passed=true/failures=[]**。独立调用者 PID 24472 使用安装版 WinHTTP/CAS，对新热捕获 session `c247df0197a8481b932cf7b2344a979a` 保存 alpha 0→0.125→0；初始没有 replacement，没有预先 apply。`intentionalEditApplied`、`newCaptureReplacementVerified`、`restoredSource`、`restoredCandidate` 全为 true。源码恢复为 `3ded73edddac108ae065698b5ecd7c1782570014fbd9d84cb177c95783227ca3`，候选恢复为 `249ad5e1427def11898a8d3a9884e216004e1442715f906081b5c015ad862fbb`；项目来源和不可变输入始终保持。**0 次显式 start、0 次显式 apply、0 次 AI review**，证明自动恢复的会话可以直接接住后续用户保存。

最终 Host 全量测试首轮受公共临时目录清理 `WinError 5` 阻断；隔离目录一轮为 1613 passed / 1 个测试假校验器超时 / 13 skipped / 598 subtests。该用例两个参数单独重跑均通过（2 passed，1.43 秒），日志 `tests-host-heartbeat-validator-retry.log` 保留。随后无并发打包的独立完整回归为 **1614 passed / 13 skipped / 598 subtests，124.21 秒，ExitCode=0**，日志及 XML 为 `tests-host-heartbeat-final-clean.*`。保留前两轮失败，不以单测重跑替代这次完整通过。

最终 cycle 的 `final-source-state.json` 再次确认 revision **10**、源码 `3ded73...`、候选 `249ad5...`，新 qdoc WER 崩溃事件数为 **0**。两个验收 GUI 已正常关闭，用户文件保持已整理原版本。

本轮要求的 Shader 主路线已完成实际验收，全部失败收据保留。验证范围仍限定于已记录捕获与输入：有限输出观察不是所有输入的数学等价证明；API/Codex/Cursor 的真实报告通过不等于各厂商完整 Shader 矩阵，Claude 尚缺认证；18 项基础 IDE 能力不冒充完整 HLSL 语义语言服务器。其他 RDC 的源码匹配/载入证据也不等于替换时可借用旧捕获的 ABI 授权。
