# Bread AI 崩溃、任务执行与 Shader 恢复审计（2026-10-13）

本次依据用户运行 PID 22656 的实际任务日志、GUI 心跳、原生日志、Windows 事件和崩溃转储分析。修复前应用 SHA-256 为 `4ebb34f3e810600d9cc791f32fce0533909994c7fff6d6775de8da7db3be7478`。没有运行新的付费模型任务，也没有改写用户捕获或 Shader 工作区。

## 这轮实际处理了什么

任务 `de87081366b042929439447392f3bee6` 使用 `gpt-6-sol` 整理 EID 11402 的 `MainPS`。本轮握手已确认 owned workspace 可写，不是此前权限配置重载失败。

| 本地时间 | 事实 |
| --- | --- |
| 17:16:04 | 任务开始，读取捕获、Shader 与现有工作区。 |
| 17:34:07、17:54:07 | 宿主各续接一次同一 Agent 会话，原因是自检仍有待办。 |
| 18:11:47–18:13:22 | 一次上下文压缩，约 95 秒。 |
| 18:44:03 | 编译报缺少 `specularNDotVRaw`；保留草稿并回滚未通过的交付。 |
| 18:46:47 | 修复后验证和应用成功。 |
| 19:00:30 | 向已关闭草稿写入失败；重新开始草稿后继续。 |
| 19:04:14 | 最新 Shader 自检仍为 `needs_work`。 |
| 19:04:24–19:05:37 | 最后一次 finish 用时 73.79 秒，revision 21 保存、验证并应用。 |
| 19:06:22 | 崩溃栈内聊天渲染的 `nowMs`；第 1009 次结构渲染。 |
| 19:06:43 / 19:07:33 | Windows 记录访问异常 / 生成崩溃 dump。 |
| 19:15:05 | 后台任务最后一条记录；没有 completed、failed 或 cancelled 终态。 |

公开 MCP 调用共 125 次：12 次 finish、9 次 begin、7 次显式编译、6 次显式候选验证、9 次应用、5 次项目登记。12 次 finish 都显式指定了选项，前 7 次仅保存，后 5 次主动启用验证/应用，其中 1 次编译失败；Host 默认不是自动验证。12 次 Shader 自检全部为 `needs_work`，最新仍指出主函数约 2160 行、变体 1/3/6 的材质、阴影和颜色合流未完成。大量循环来自 Agent 自身选工具，并非宿主不断重发主请求。

最后成功保存的 `source/shader.hlsl` 为 318143 字节、4386 行，SHA-256 为 `b5326791…98499c5`。成功的色差阈值/深度比较只说明当时测试事件的输出，不证明所有输入等价，也不代表可读性完成或崩溃后仍有活跃绑定。通用自检所绑定工作事实已过期，不能当最终完成证明。

截至最后一次 Host 工具完成，总墙钟时间约 109 分 33 秒，公开 MCP 区间并集约 18 分 06 秒。其余约 91 分 27 秒包含模型生成、网络、上下文处理和 CodeMode 执行，不能全部计作工具或 GUI 阻塞。6 次完整 Shader 读取约 2.14 MB，其中首次两次完全重复；可解析写入回执没有使用 `inputFile`，没有 native command/fileChange 事件。已有权限不代表 Agent 实际使用了文件编辑路线。

GUI 最后一次大间隔日志在 19:05:41，实例心跳仍更新至 19:06:21，不能把前者视为进程退出时间。之后后台还有 CodeMode 操作，但没有新 Bread Host 调用。provider 的 `outcome=interrupted` 在崩溃前也存在，不能据此判定崩溃或取消。

任务实际加载了 Skill v83 及完整 `source-operations.md`、`self-review.md`，引用 hash 与当前文件一致。工具配置启用了 shell/unified exec，但没有观察到原生文件工具尝试或拒绝。能力信息存在一个缺口：成功可写握手只发给 UI，只有降级说明才发给模型。此次把成功握手确认的精确 owned 路径、权限和限制也写入模型本轮上下文，指明完整工作副本及 `source_file` 导入路线，不扩大权限、不再堆叠 Skill。后续实际采用情况仍取决于 Agent，不能把告知改动视为已证明任务总耗时下降。

新增 GUI 父进程精确句柄观察：确认该进程退出后取消对应 engine 并复用 provider 清理，不以正常 stdin EOF 或短心跳超时杀任务。queued 写入 owner PID/创建身份，能落盘的终态使用 `owner-process-exited` 而非“用户取消”。37 项生命周期测试和 24 项能力告知测试通过。GUI 线程崩溃但进程仍在崩溃报告窗口时句柄未退出；Windows Job 硬杀也可能早于 Python 终态记录。因此没有声称本例九分钟间隙已完全解决。

## 崩溃与卡顿是哪些路径

Windows 异常为 `0xc0000005`，模块 `Qt5Core.dll 5.9.4`，偏移 `0xa2294`。用系统 dbgeng 和该安装版本的本地 PDB 解出的主线程栈为：

```text
AIAssistant conversation render timer (single shot, 60 ms)
  -> renderConversation -> AIConversationView::setMessages -> render
  -> QTextEdit::setHtml -> QTextDocument::setHtml
  -> QCss::StyleSelector / selectorMatches -> QString comparison access violation
```

不是 elapsed 自身一秒 timer。崩溃时原生 replay 线程停在 `ReplayManager::run` 的空闲条件变量，未在执行 Shader 编译或替换。dump 没有 HTML 对应的 heap 页面，无法直接恢复损坏对象，因此该栈确定了崩溃路径，但不能单独证明最初内存损坏原因。原程序和 PDB 已另存 OS 临时目录，避免后续构建覆盖诊断符号。

当前任务共记录 159 次超过 1.5 秒的 GUI 间隔，累计约 820.5 秒，单次约 3–12 秒。部分发生在没有新工具调用的阶段，不能只归因于回放线程。

另一个可直接从实现确认的阻塞：`CaptureContext::RefreshStatus()` 原先同步调用 `SetEventID()`，其中 GUI 的 sleep 循环在当前任务标签匹配时可以无上限等待回放。Python 在 GUI 调用它时还可能持有 GIL，前方 replay Python 回调又需要 GIL。`RegisterReplacement` / `UnregisterReplacement` 原先也同步等待并重复通知查看器。单纯把编译器放在后台不能消除这种回到 GUI 后的等待。

此次把这些状态刷新改为 GUI 短队列合并、replay 线程执行、拥有独立生命周期的 pipeline 快照回传。GUI 不等回放；新请求、事件切换、捕获关闭会淘汰旧发布；拖动事件期间延后到查看器队列完成，避免重放旧显示事件。实际 Qt 5.9.4 测试使用 400 ms 的慢 replay 回调：刷新调用 0 ms 返回，最大心跳间隔 11 ms；同批合并、force 升级、后续请求覆盖、导航、拖动延后及关闭失效均通过。这是该机制的验证，不是完整 AI 任务卡顿已消失的证明。

异步发布同时修复反射信息的所有权。原 API state 的拷贝仍含 driver 所有的 `ShaderReflection*`，旧替换资源释放后可能失效。现在 publication 深拷贝并去重四种 API 的反射；ShaderViewer、编辑菜单、异步反汇编、资源查看入口持有自己的副本，模态调试入口保留当时的反射及 pipeline ID，保存对话框提前复制字节。避免把 replay 线程中的有效指针当作 GUI 长期可用的数据。真实 API 类型的原生测试覆盖 28 个 stage、D3D11 layout、嵌套数组、原反射释放及关闭查看器期间异步调用。此项是代码中独立确认的生命周期风险，不能直接宣称它就是本次 Qt 崩溃的最初原因。

聊天结构渲染改为预展开首方简单 CSS，绕开此次堆栈中每节点 class selector 匹配；在独立 QTextDocument 中准备内容后再替换显示，并释放旧文档与游标。大活动列表的结构更新合并间隔为 250/750 ms，实时正文和 elapsed 保留原地更新。同一实际会话、Qt 5.9.4、Development `/Od` 对照中，重复结构渲染约 160 ms 降到 96 ms；首次布局仍约 400 ms。24 项针对性检查包含真实生产源码 Qt fixture，覆盖链接、图片、代码高亮、选区、滚动锚点、实时文本、计时和文档释放。旧版 1200 次结构重建没有复现 AV；这项是有测量的路径规避，尚未证明最初 QString 损坏来源。

## 为什么项目恢复仍用了 151 秒

真正的项目恢复在 16:41:09–16:43:40，19:05 时段是 AI 整理交付。

| 恢复阶段 | 已知耗时与证据 |
| --- | --- |
| Capture loaded 到 Host 开始恢复 | 约 23 秒；现有记录不足以继续细分。 |
| Host 整体恢复 | 128.422 秒。 |
| 三个 Shader 的替换 | 7.393 / 12.578 / 10.387 秒，共 30.358 秒。 |
| 三个编辑会话接入 | 整体 59.57 秒；单项 18.499 / 15.396 / 19.387 秒。 |

三个 Shader 全部使用 `verified-rdc-restore` 且 `reusedValidation=true`，没有重新执行 HLSL 编译器。缓存中有编译字节码和验证证明；旧进程的 GPU 资源句柄不能跨进程复用，仍需要建立新资源和替换关系。

替换流程重复定位原始 Shader、验证绑定、调用 checked replacement、恢复可见事件，原生日志在每个 Shader 对应区间各出现 5 次 initial-contents 回放。它们是回放到指定事件，不能一概称作整帧回放。之后 59.57 秒编辑接入区间没有这种回放日志，不能把这 60 秒也归到前述回放。

同一次 editor owner-start 已建立严格状态 snapshot，native 恢复却再次执行完整 `service.status()`。现已复用同操作 snapshot，保持文件内容指纹、操作锁、candidate/manifest 前后检查和实时 native mapping 核对；后续普通恢复仍重新验证。复制真实工作区至 OS 临时目录的 profile 显示：cold status 0.838 秒、warm 0.315 秒、snapshot 1.544 秒；这项复用只把局部 0.308 秒降至 0.245 秒，不能宣传为解决全部 60 秒。

增加 owner-start 阶段计时，用来区分身份检查、snapshot、watcher/native 恢复与编辑注册发布，成功和失败均保留对应阶段。当前不知道的等待会明确留作待测项，不用“重复编译”代替证据。

## 证据位置

- `%LOCALAPPDATA%/ReplayLab/ai-tasks/de87081366b042929439447392f3bee6.jsonl`
- `%LOCALAPPDATA%/ReplayLab/logs/gui-responsiveness-22656.jsonl`
- `%LOCALAPPDATA%/ReplayLab/logs/host-memory-trace.jsonl`
- `%LOCALAPPDATA%/CrashDumps/q_render_soco_doc.exe.22656.dmp`
- `%TEMP%/RenderDoc/RenderDoc_2026.10.13_16.40.42.log`
- `%TEMP%/bread-crash-22656-analysis/dump-stacks.txt` 和 `symbols/`
- `%TEMP%/bread-ai-de87081366b042929439447392f3bee6-read-only-timeline.json`
- `%TEMP%/bread-restore-profile-7e91953488aa477a8b0b80e6bf41be3c/report.json`

## 已交付与安装后验证

- 已构建、部署并校验 Bread AI/runtime **1.13.22**，应用目录为 `D:/Study/GitHub/renderdocUltra/x64/Development`。原生程序 SHA-256：`be055295675aa172db2fb0053f63cbb86b859ee26cef85f22028a0ff9ab3955e`。
- MSBuild `/m:2`、显式 `SolutionDir` 完成，零警告、零错误。翻译同步、完整 QM 构建及 source-literal lint 均通过：5104 条全部完成，无新增未翻译字面量。
- 部署验证核对四个冻结运行时产物的 manifest/hash，以及固定首方路径的 21 个 Python 集成文件；启动冒烟确认实际应用、集成源码 hash 和 32 个原生 GIL release 绑定。
- 恢复/编辑接入针对性测试 284 项、生命周期 37 项、成功权限告知 24 项、聊天更新针对性检查 24 项通过；异步状态刷新、publication 反射与 ShaderViewer 所有权另有真实 C++/Qt 行为测试。七处模态调试入口的生产函数 fixture 模拟释放原反射、切换 pipeline、清空消息列表，均保留原请求数据；空反射安全返回。测试使用 OS 临时目录。
- 安装后的实际程序以隔离配置只读打开 `6.rdc`，加载 20.832 秒，定位 EID 11402：`RefreshStatus` 0.017 毫秒返回；151 毫秒后发布新 pipeline；期间 26 个心跳样本，最大间隔 25.49 毫秒。事件未改变，捕获正常关闭、进程退出码 0；捕获大小和修改时间未变。
- 实测报告：`%TEMP%/bread-async-refresh-installed-ot1mma74/result.json`；安装启动报告：`%TEMP%/bread-connect-freeze-installed-u73fzgov/result.json`；构建与部署日志：`%TEMP%/bread-crash-11322-*.log`。

未重新运行一次完整付费模型整理，也未把只读状态刷新实测当作完整项目恢复速度的对照。原版聊天渲染 AV 尚未在压力复现中重现，故不能宣称已证明初始内存破坏来源；恢复中原有 59.57 秒接入等待还需新版本的阶段计时进一步归因。以上用户工作成果只读保留，不把中间成功应用改写成任务完成。
