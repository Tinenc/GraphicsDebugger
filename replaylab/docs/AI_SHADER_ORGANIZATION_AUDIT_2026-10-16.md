# 2026-10-16 Shader 整理与解释任务审计

本次审计只读取日志、已保存源码和工具回执，没有修改用户 Shader、重新编译、触发捕获操作或停止正在运行的任务。下列时间均为北京时间（UTC+8）；截图控制台的 `02:05` 对应北京时间 `10:05`。

## 1. 两次请求必须区分

| 项目 | 实际整理 | 后续解释 |
|---|---|---|
| Task ID | `54e6cb1001394af096e6a00b1a8ec7aa` | `b24e96e19621431e82b905ff80c32766` |
| 用户原文 | 帮我重新整理这个shader | 这次整理修改了什么内容，为什么函数还是没有按照我的要求进行嵌套处理，中间依然有大量的变量声明? |
| 开始—结束 | 09:27:22—10:08:12 | 14:50:09—14:57:33 |
| 用时 | 40 分 50 秒 | 7 分 24 秒 |
| Provider / model | Codex app-server / gpt-6-sol | 同左 |
| 源码结果 | 修订 25 → 26 | 没有修改、编译或应用源码 |
| 终态 | `completed`，Agent 自评完成 | `partial`，触发自检续接后判定无进展 |

原始用户请求来自 qrenderdoc 的捕获会话存档 `conversation-232d05bdfdf344898779a5cab4853696`，不是从 Agent 的解释反推。会话文件为 `%APPDATA%/q_render_soco_doc/ReplayLab/conversations/11353dae52c609bdfe4aa35cb66e48ba74390754776206c43ac4dbe69d4566d0.json` 的 base64 payload。

两轮目标都为 EID 11402、fragment、`MainPS`，工作区：

`D:/Study/抓帧/ReplayLab Project/workspaces/shaders/v2/d2e9720e2ab36db6b7e813460d36e9d36021ad04c69c3b34923e3ad0b2ecb48d`

审计完成时源码仍为修订 26，SHA-256 `9be1e3e4db956427c36a4fcf2f08ed0b3e435ec2f764bc51c0556dc65bb2dd4d`。

## 2. 实际整理过程及证据

日志目录为 `%LOCALAPPDATA%/ReplayLab/ai-tasks/`。以下行号指 `54e6cb1001394af096e6a00b1a8ec7aa.jsonl`，工具的实际返回摘要和统计另见同名 `.report-state.json`。

1. **09:27:39—09:27:46：已加载 Skill 92。** 行 47 是入口读取回执；行 118、120、121、123 分别是 `asset-naming.md`、`algorithm-design.md`、`self-review.md`、`source-operations.md`。不是没有加载新规范。
2. **09:28:00：创建同一整理草稿。** organization ID 为 `65fd90175aee4fe1a207537d4915979d`。开始回执已提供 `proposalEditing`（`edits`、`source_file`、`content`）、资产准备状态及源码观察信息。
3. **09:28:07：读取已有资产关联。** `ai/asset-resolution.json` 完整返回 2899 字节，匹配本 EID 的 Munin 帽子材质、纹理和部分 b5 参数。回执显示解包根目录 `E:/SilverPalace_Unpacked/raw` 存在，旧记录与 Shader 原字节和事件阶段匹配；Agent 复用已有记录，没有全量扫描资源。回执中的 freshness/sourceNames/liveAliases 仍为 `not-checked`，因此不能把“记录存在”单独当成所有名称已适配的证明。
4. **09:28:23：完整读取草稿源码。** `read_shader_workspace_file(mode=complete)` 返回 240988 字节，`coverage.complete=true`、`truncated=false`。审计报告摘要本身有长度截断，不能混淆为 Agent 没有收到完整源码。
5. **09:31:56—09:58:52：十次草稿写入。** 全部通过 `content`，没有用 `edits` 或 `source_file`；回执均有新的 proposal SHA，修改处于同一 organization 中。Agent 的公开进度（行 139 起）称本地 shell 被策略拒绝，改用 Bread 源码工具。当前保留日志没有具体拒绝请求和原始错误，故只能确认这一公开说明，不能断言底层拒绝原因。恢复出来的 native 工作副本仍是开始时的 240988 字节旧版；受管 proposal 才是实际编辑结果。
6. **09:59:24：第一次源自检。** 行 799—800；五个范围全部判定满足，未列剩余工作。
7. **10:00:35：第一次集中编译失败。** 行 809；编译器在 input.hlsl:2921:93 报 `expected ';' at end of declaration`。草稿保留，已发布源码恢复；不是编译失败导致任务永久中断。
8. **10:03:54—10:04:35：修复公式括号，再次源自检。** 第十一次全文草稿写入后，第二次 review 仍为 `complete`，`findings=[]`、`nextActions=[]`。
9. **10:05:13—10:05:32：第二次 finish。** 编译、ABI 和捕获事件输出比较通过，修订 26 发布为可编辑源码。颜色输出 8294400 个分量中失败数为 0，最大绝对差 0.001953125；深度逐字节一致。只覆盖本捕获事件，不证明所有输入等价。
10. **应用阶段存在重复路径。** finish 尝试 shared-resource apply 时，当前 live-edit 会话已占用该资源，因此返回“stop the exact Shader live-edit session before applying its shared replacement”。随后 Agent 查询 live-edit、当前绑定和 UI，发现保存触发的 live-edit 已应用同一个修订 26，于是不再重复替换；最后登记项目关联。这次没有重新打开捕获。
11. **10:08:06—10:08:12：任务自评完成并结束。** `record_task_review` 认为完整整理、编译/ABI、交付、项目关联均满足，未记录可修复的可读性遗漏。

应用冲突有明确的时序窗口：`apply_shader_workspace_shared_resource` 入口已经会调用 `_observe_current_shared_shader_candidate`，但随后重新验证和准备 apply，直到 live-edit admission 才检查活动写入者。若 watcher 在这段时间完成相同候选的替换，入口时的“尚未应用”观察已过时，仍会抛占用。控制台 `events.qrenderdoc.13020.a88cd03f1ab8401cbfce394600cad004.0000.jsonl` 第 35—42 行记录：10:05:26.039 比较完成，27.371 展示完成，28.262—29.163 已有 apply 成功，29.506—29.591 又有一次反射检查。窄范围修复应在 admission 拒绝时再做一次精确候选的幂等回读：若同一工作区、当前捕获、源码与候选确实已经生效，返回现有效果；否则保留冲突结果。无需停止会话、强占其他写入者或无限重试。

工具统计：29 条 Bread Host 工作事实、累计执行 41934 毫秒；11 次草稿写入总计 1702 毫秒。该统计不包含模型生成、调度、网络往返和原生工具，因此不能以它计算模型的精确耗时；但不能把 40 分 50 秒主要解释为 Host 编译或工具执行。仅发生末段两次编译（一次语法错误修复），不是每拆一个函数就编译。多次传递全文是可以减少的开销，但日志不足以证明全文由模型逐次重新生成。

## 3. 真实修改量与未完成内容

比较该 organization 的 `baseline-source.hlsl` 和 `organized-proposal.hlsl`：

| 指标 | 修订 25 / 基线 | 修订 26 / 本次结果 |
|---|---:|---:|
| UTF-8 字节 | 240988 | 229668 |
| 行数 | 2982 | 3057 |
| 函数数 | 51 | 58 |
| `float` 声明/参数词法计数 | 1275 | 1248 |
| `float2` 声明/参数词法计数 | 22 | 23 |
| `float3` 声明/参数词法计数 | 314 | 348 |
| `float4` 声明/参数词法计数 | 69 | 87 |
| `MainPS_inner` 从声明到闭括号 | 608 行 | 629 行 |
| `MainPS_inner` 直接调用的不同本地函数 | 29 | 35 |
| `ShadeVariant2` 从声明到闭括号 | 387 行 | 372 行 |

按同名函数体 token 比较，13 个改变、37 个未变，新增 8 个、删除 1 个。新增包括滴水法线、Cookie 亮度、细节高光、间接光通道、探针通道和视角缩放等辅助函数；两份重复湿材质函数合为一份。行数包含注释，因此增加行数本身不表示退步；但主入口没有完成责任拆分，不能仅用新增函数数量证明达到层次化结构。

用户指出的两个反例确实保留在修订 26：

- `lightDetailWeightX/Y`：同一 `halfDetailAdjusted.xy`、相同 clamp/abs/系数仍展开为两个标量。
- `lightDetailShiftX/Y`：对应两个通道，仍逐分量声明；合并时需保持原先零值分支的 `+1` 规则，不能无条件改成 `sign()`。
- `secondarySpecularHalfVector`：`float3(s*x, s*y, s*z)` 仍逐分量列出，可直接写成相同操作数次序的 `s * vector`，不需要交换律或结合律。

这些并非编译/ABI 问题，也不是需要收集更多捕获证据才能识别的问题，属于首次整理和整体源码自检都应发现的可读性遗漏。

## 4. 为什么自检仍然放行

实际 `self-review.json` 为 `complete`，将 `abi`、`surface`、`lighting`、`branches`、`base` 五组全部标为 `satisfied`，每组 `remaining_work=[]`。它已用“体、调用、共享算法、共享结果”四个角度写出说明，说明仅增加检查项名称并不足以保证正确判断。

例如 `base` 范围声称 MainPS_inner 组织几何、湿材质、基础光照与变体分派；保留理由为深度采样/discard 应保留原分支和阶段值被多处消费。这些理由可以解释部分代码，但没有回答为什么上述双通道展开、向量乘法展开和大段主函数计算仍需保留。`redundant_temporaries` 的总评也只列已删除内容和探针/防御公式的特殊保留理由，没有覆盖未处理片段。

开始回执实际上提供了 24 组分量命名线索、重复湿材质函数、复杂公式和缺少紧邻函数注释的样例。旧观察器严格通道识别以 XYZ/RGB 为主，双通道和向量构造展开存在盲区；样例有意限量，不是完整待办清单。不能把“观察器未提示”或“已处理几个样例”当成整份源码达到规范。

结论：本轮真实改过源码，资产记录及 Skill 也读到了；问题在于**没有把整份源文件的要求落实到未处理函数体，随后自检对完成度作了过宽判断**。主入口仍负责多个算法子系统，局部函数抽取尚未形成用户要求的职责层级。

## 5. 解释性追问的额外流程问题

`b24e96e19621431e82b905ff80c32766` 的原文只询问上次修改和原因。该轮共 38 条 Host 事实，包括 31 次 `read_shader_workspace_file`；没有 write/source-review/finish/compile/apply。它却为读取和对比创建了新 organization `5dd6bc6a3eb14e6e8885416f19062080`，草稿始终等于修订 26。

该轮最终任务 review 的唯一要求是“解释上次 Shader 整理的实际改动，以及函数层级和中间变量仍不符合要求的原因”，状态 `satisfied`；证据明确“本轮只核查解释、未修改源码”。然而 jsonl 行 769 在 14:56:46 仍触发 `reviewReason=self-review-required`，最终因工作事实或自检状态未推进停止续接，向用户报告 partial。

需区分两件事：原整理结果尚未达到质量要求；新的解释请求可以已完整回答。不能因历史 Shader 未完全整理，就把每一次只读解释都自动扩大为新的整理任务或要求其再编译。最终 report-state 的 `pendingShaderWork={}`，因此根因应结合 source-review 候选和 continuation 的实际选择逻辑判断，不能仅凭 pending 字段推断。

## 6. 证据路径

- 主任务日志与回执：`%LOCALAPPDATA%/ReplayLab/ai-tasks/54e6cb1001394af096e6a00b1a8ec7aa.{jsonl,report-state.json}`。
- 解释任务日志与回执：`%LOCALAPPDATA%/ReplayLab/ai-tasks/b24e96e19621431e82b905ff80c32766.{jsonl,report-state.json}`。
- 源码、源自检、编译/ABI/观察结果：目标工作区 `ai/organization-sessions/65fd90175aee4fe1a207537d4915979d/`。
- 完整 begin 回执：`%TEMP%/replaylab-tool-output/result-spill/0e9a0c5544833676133925500854aa7677872c4cbadffae59424279e33c14783.json`。

本报告仅引用公开消息、工具事实、编译结果和文件差异，不使用隐藏推理作为证据。

## 7. 本次开发修复

- **编辑阶段的定位补全**：既有 begin/write 回执中的源码观察支持 2–4 通道，新增相同操作顺序的展开向量乘法识别。当前真实源码检测到 8 组连续通道、23 组命名族和 10 处展开向量乘法；约 125 毫秒冷扫描、4 毫秒缓存读取，回执仍不超过 6 KiB。没有新增编译或逐表达式工具调用。
- **完成声明回应实际残留项**：可见候选携带当前源码绑定的 ID；在现有 `scope_review.observation_dispositions` 中关联实际函数范围，说明具体保留理由或 `needs_work`。遗漏候选、使用旧 ID、关联错误范围，或把仍存在的候选标成已解决，不能通过完成记录。可行问题应先在工作副本集中修复；旧完整记录仍可读取，但缺少这部分覆盖时不能授权新一次最终验证和应用。
- **解释范围与编辑范围分开**：显式整理请求继续要求源码自检；非显式整理任务若仅打开了当前未修改草稿、无源码操作或源码自检、无冲突或读取诊断，则不额外产生整理义务。实际写入或原生文件变化仍进入源码自检，普通任务自检也仍保留。Skill 93 引导解释性问题直接使用读取工具。
- **应用竞态**：若 fresh validation 期间 live-edit 已应用同一候选，最终守卫拒绝时只再读取一次，核对已验证 revision、candidate SHA 和 source SHA，精确一致便复用已有应用回执。不同候选、版本、工作区或读取失败仍保留原拒绝；不停止已有会话、不强占、不重复应用。
- **整理规范**：Skill 93 将双通道、展开向量构造纳入首次函数体整理示例；保留零值符号选择，避免把向量化误写成改变数学边界的替换。继续要求真实职责层级、完整注释和共享结果，而非用函数数量或固定深度判定质量。

这次真实旧 `complete` 回执在当前源码哈希仍相同的情况下，已被新校验因缺少具体候选处置拒绝。Host 验证覆盖和事实一致性，不证明 Agent 的每条保留理由在语义上正确；有限样例也不能取代整份源码审阅。

回归结果：394 项通过、1 项跳过；Skill 格式验证通过。范围涵盖源码观察、具体候选处置、旧记录与最终发布、解释性续接、API/原生共用任务自检、编辑回执、Skill 工具引用、版本一致性及重复应用竞态。没有为本次开发再次执行用户的整轮 Shader 整理。

## 8. 安装与恢复结果

已安装运行时 **1.13.34 / Skill 93**。18 个冻结模块和全部 Shader Skill 文件与仓库内容一致，固定路径 Python integration 的 21 个文件部署验证通过。

安装前确认没有活动 AI 进程和未保存编辑，正常关闭应用后安装，再打开原捕获。新 PID 为 73780。恢复回执：Shader 3/3 应用，0 失败、0 警告；编辑会话 2 个启动，1 个因 `organization-draft-open` 暂缓。暂缓项就是解释追问留下的 `5dd6bc6…` 草稿，未删除或伪造其完成状态；已通过 `show_shader_workspace_source` 显示 EID 11402 的已保存源码只读预览，未编译、启动 live-edit 或再次应用。

用户目标源码仍为 `9be1e3e4db956427c36a4fcf2f08ed0b3e435ec2f764bc51c0556dc65bb2dd4d`，本次开发没有改写它。恢复回执保存在 `%TEMP%/bread-1.13.34-restart-acceptance.json`，显示回执为 `%TEMP%/bread-1.13.34-show-target.json`。本次安装和回归不能替代下一轮真实 Agent 整理的质量验收。
