# EID 交互回放：资源版本、跨 API 增量与验收进度（2026-09-08）

本文记录独立 worktree `D:\Study\GitHub\renderdocUltra-eid-perf` 本轮的源码与证据状态，
补充 [此前的开发记录](Interactive-Replay-20260908.md)，不覆盖其历史测量。
**目前没有完成“大型 RDC 任意来回拖动均实时刷新”的最终目标。** 已验证可继续区间的真实增量回放，
并减少部分重复恢复；`1.rdc` 的反向/跨阶段恢复仍有主要成本。

记录截点包含 v7b 至 v22 的实际验收，具体证据分别列于后文。
旧二进制通过不自动等于新二进制通过。
本轮回放与编辑作用于独立回放环境及替换层，原始 `.rdc`、捕获资产初值和原工作区的 Shader 项目
不会因为调节参数而被改写。

## 方案与当前完成边界

目标是保留可继续执行的资源状态，而不是用某个 EID 的截图代替渲染。捕获帧虽然固定，回放命令
仍会写入 RT、深度、UAV、计数器和部分 buffer；“文件静态”不等于“工作资源内容始终不变”。

| 内容 | 当前状态 | 适用边界 |
|---|---|---|
| 资源版本公共策略 | 已实现 | 表达原始资源 ID、子资源/字节范围、内容版本、编辑 generation、别名失效和待恢复债务；不自行执行 GPU 复制 |
| D3D12 检查点驻留版本复用 | 已接入并有 GPU 验证 | 当前工作版本与目标检查点版本相同才省略复制；无法证明时恢复 |
| D3D12 完整 Clear 前省略恢复 | 实验开关，小 fixture 已验证 | 有完整“先清空、后读取”证明的独立 committed 纹理；不支持任意 alias/sparse 阶段 |
| D3D12 descriptor overlay | 实验开关，正确性已验证 | 避免把检查点将覆盖的描述符槽先还原初始值再覆盖；不代表减少纹理/buffer 的 3.47 GB 初值复制 |
| D3D12 SM6.6 有限 UAV 声明裁剪 | v10 独立正例与主帧命中已验证，实验开关 | 完整原始执行程序证明，静态 buffer 正例每次省 8 MiB；主帧本轮无检查点条件仅省一小部分恢复量 |
| D3D11 / GL / GLES / Vulkan 普通 EID 向前继续 | 显式开启/关闭与共享参数测试已验证 | 各 API 的严格安全区间；不是任意命令序列或跨提交继续 |
| 跨 API live-forward 默认开启 | v8b 四 API 默认模式已验证 | 环境变量为 `0` 仍关闭；实际移除开启变量后验证，不能用显式 `1` 代替默认配置验收 |
| GL 静态资产共享 | 已有原生实现 | 需要捕获完整写入来源证明；不是根据 GetUsage 没返回某项就判定只读 |
| 历史 fallback 保持显示身份 | v7b 负例与 v8b 修复正例成对验证；v12b/v13 小帧真实 Qt 回归通过 | headless 四 API 与小帧标签/历史输入通过；大型历史列表、跨组 RT1 记忆和实际拖动延迟仍需验收 |
| 固定 EID 共享参数覆盖与空闲维护 | 已有基础实现，本轮继续验证普通增量中的共享参数 | 原始 buffer 与绝对字节范围定位；前台前缀重绘、空闲限额维护已存在，更细粒度阶段依赖仍待实现 |
| 大型 RDC 按深度/GBuffer 等阶段建立更多有效检查点 | **尚未完成** | 尤其后续阶段包含 alias/placed/sparse 写入时的内容与环境保存、恢复仍未实现 |
| 两套可轮换的阶段工作资源 | **尚未实现** | 不能把现有检查点缓存、空闲维护或公共版本表称为这个功能 |

公共策略见 [replay_resource_versions.h](../../renderdoc/replay/replay_resource_versions.h) 和
[实现与测试](../../renderdoc/replay/replay_resource_versions.cpp)。它区分四种操作：
版本相同则保留；缺失/未知则恢复；完整覆盖之前可延迟恢复；编辑使目标旧版本失效则重建前缀。
待恢复项必须由真实复制或完整覆盖完成后清除，不能在一次回放结束时无条件清空。
别名关系未完整证明时，相关存储必须一起失效或走原有恢复路径。

建议继续采用的流程为：

```mermaid
flowchart TD
    A[打开 RDC：初始化资源与命令索引] --> B[证明只读资产共享；保存可支持的检查点]
    B --> C{当前请求}
    C -->|合法向前区间| D[沿用当前工作状态，执行缺少的命令]
    C -->|往回或区间不支持| E[选择有效检查点，逐资源决定保留或恢复]
    E --> D
    C -->|固定 EID 调整参数| F[更新独立参数覆盖层，失效受影响的下游版本]
    F --> E
    D --> G[完成目标 draw，发布新的可继续状态并显示]
    G --> C
```

该流程中的“更多阶段检查点”仍需要后端实现。两个工作环境可以减少切换时的准备，但各自被写过的
内容仍需验证或恢复；增加第二套资源并不能自动消除旧参数产生的下游 RT。

## 通用资源访问索引与加载时检查点：主路线补充

本节是用户进一步明确要求后的设计与源码审计，不代表新增原生功能已经实现或完成验收。
`1.rdc`、`6.rdc` 只作为测试输入。生产策略不得包含这些文件名、路径、资源 ID、固定 EID，
也不得依靠特定项目的 pass 名称判断资源静态性。各后端输出一致的访问证据，由公共层规划，
实际资源复制、布局/绑定/队列状态恢复仍由对应 API 执行。

### 当前实现与要求的差距

- `ReplayController::PlanReplayCheckpointCandidates()` 已按输出绑定和 Draw/Dispatch 类型变化
  自动规划候选，但仍主要依据 EID 间距和总资源字节数，不是阶段活跃资源和真实恢复成本。
- D3D12 `BuildReplayWriteTracking()` 已在加载时建立潜在写入和首次写入事件索引，实际提交再
  解析可变描述符；首次写入不能回答任意阶段之间发生过哪些写入。公共 `GetUsage()` 也没有
  承诺完整字节范围、物理别名、所有 CPU/GPU 隐式访问。
- `ReplayResourceVersions` 能表达范围和内容版本，完整账本目前主要接入 D3D12；GL 采用部分
  公共策略，D3D11/Vulkan 仍有自己的粗粒度恢复路径。不能将公共类存在视为全后端接入完成。
- 自动配置中的 `Loading never builds checkpoints` 仍有效，先通过普通访问采集成本后才试建。
  新要求需要增加加载分析及受预算约束的预建流程，不能继续称现有行为为“开帧就准备好检查点”。

### 加载时建立的统一快速查找表

利用读取捕获和第一次原生初始化已有的信息建立索引；不在每次鼠标移动时重新扫描全帧。
索引应双向支持“资源范围查事件”和“事件区间查资源范围”，并记录：

| 信息 | 用途 |
|---|---|
| 原始资源 ID、子资源/字节范围、底层内存区间及映射版本 | 区分共用 Buffer 的不同块；追踪不同资源是否实际重叠 |
| 所有已知读写事件区间、CPU 更新、Clear/Copy/Resolve、间接写入 | 查首次变化和阶段内变化；不能只保留首次写入 |
| 实际提交实例、队列顺序及同步依赖 | 同一个命令列表重复提交时，不能套用另一次提交的相对 EID |
| 首次使用是读取、完整覆盖还是未使用；证据完整性和未知原因 | 决定是否需要旧内容，未知不能当作静态 |
| Shader/参数范围到输出及间接命令的依赖、内容与编辑版本 | 支持共享参数和 Shader 编辑后局部失效 |

可证明全帧不写的内容只保留一份，未发生写入之前的资源也可保留当前正确初值。普通纹理、网格、
材质 Buffer 的名字和创建标志不能独立构成证明；动态索引无法收窄时保留可访问范围的安全上界。
加载时尽量完成分析，实际提交补全必须依赖 GPU/可变绑定的信息；编辑后重新验证受影响访问，
不承诺仅靠静态扫描就能确定所有着色器实际写入的每一个字节。

### 由索引生成检查点和恢复集合

1. 按真实附件变化、原生可恢复的 pass/提交边界产生候选，估计后续重放耗时、必须保存的活跃
   内容、实际可复制粒度和显存预算。文件大小只能作初始提示，不能决定备份哪些资源。
2. 在加载后的预备阶段，按预算预建少量高收益检查点，允许用户交互打断后续预建。构建与恢复
   使用同一回放队列的合法顺序；后台调度不意味着可以无同步地同时改写工作资源。
3. 检查点保存执行环境和所需内容的版本引用。相同版本共享不可变备份；只为新版本分配必要副本，
   有界限制继承链和淘汰成本。只读内容共享不等于共享可随意改写的备份。
4. 切换时比较当前实际内容版本与目标版本：一致则保留；不一致且会在完整覆盖前被读取的范围才
   恢复；先完整清屏/覆盖的范围交给原生命令生成；未使用范围可以延后恢复，但保留欠账。
5. 差集必须包括此前访问的其他阶段、历史预览、分析和恢复操作自身造成的变化，而不仅是目标
   阶段的写集合。例如目标阶段只读某 RT，后面的阶段却改过它，返回后仍必须恢复正确旧版本。
   任意纹理检查也会产生读取需求，不能显示仍欠恢复的内容。
6. 底层重叠区间和稀疏页需要包括恢复写入的影响闭包。不能恢复一个别名时覆盖另一个刚判定
   “可以保留”的资源；数据内容、布局/绑定和激活资源身份分别跟踪。

CBuffer 仍以原始 Buffer、绑定基址加变量/数组偏移及字节长度定位共享范围；编辑层保留原始值。
连续调节优先重算当前输出的受影响依赖，停止后限额维护下游检查点。值变化不自动销毁所有执行
环境，但可能改变输出、间接命令和动态资源访问；这些内容和访问证明必须按依赖重新验证。
Shader 替换同样更新访问证据和下游版本，不能只替换程序后沿用旧输出。

### “已有初始化备份”仍可能耗时的实测解释

初值源本来就会在加载时准备好，普通切换并不等于重新从磁盘解压并创建全部资产。保留原生状态
可以省去重建，但从初值源复制到工作资源、同步、重放中间命令仍有成本。

v13 `sparse-main1-on-v13.12.timing.log` 的单次 `1->24917 WithoutDraw` 记录为：
实际复制 3,569,894,010 B，其中 UPLOAD buffer 来源 1,219,964,660 B，UPLOAD texture footprint
来源 2,349,929,350 B，DEFAULT 来源 0，CPU memcpy 0。这表示命令从已有上传源搬运数据，
**不表示显存到显存复制，也不表示没有数据传输**。另有 4,112,675,102 B 已被静态/驻留过滤省略。

该次 `applyInitial_ms=11.750` 是 CPU 路径计时，整体 `total_ms=474.929`；
`explicitWait_ms=361.224` 是其中的等待，`chunkDecode_self_ms=35.891`、处理 56,187 个 chunk。
异步提交后，GPU 工作可能在稍后的描述符安全等待点才被计入 CPU 等待；不能把 361 ms 全算成
描述符复制，也不能凭此精确归因到初值复制 GPU 执行。这是共享 GPU 的一次 WithoutDraw 样本，
不是独占性能、完整 EID 延迟或鼠标到屏幕延迟。

把频繁恢复的必要备份放到设备本地内存可能缩短复制，但需要实际驻留预算；不是无条件再复制
整个捕获。每次恢复几 GB 本身仍是问题。优化优先级为：不复制正确现存内容、缩小必要范围、
缩短重放前缀，再按设备预算选择备份所在内存。GPU 执行时间需用对应原生计时另外验证。

验收同时要求跨 API 的同一类场景、改变资源 ID/大小/提交布局后的自动识别、未知访问回退、
前进/后退/跨检查点、历史检查和连续编辑；每项都比对强制回放的完整相关内容。报告分别列出
加载索引时间、预建成本、共享/独占备份量、实际恢复字节及原因、CPU 命令处理、GPU/同步和
输入到显示延迟。`1.rdc` 用于检验收益，不能替代这些通用正确性门槛。

## `1.rdc`：已经省了什么，仍然慢在哪里

### 驻留版本省掉约 480 MB 的检查点复制

v5 的 EID 24917 恢复检查点 10535 时，检查点有 183 项资源，快照数据量 488,964,096 B。
原生日志给出 `residentCopiesSkipped=178`、`residentAllocationBytesSkipped=480378880`，
即 178/183 项已经是目标版本，约 480 MB 无须再从检查点复制。EID 29385 的首次记录为
179 项、480,444,416 B；不能把不同目标的计数混作同一测量。

这是已经接入的真实恢复优化，但省掉的是**检查点副本到工作资源**这部分。
另外的初值恢复仍记录 3,467,411,402 B，不能将两者相减后声称原来的 3.47 GB 已消除。
日志字节是原生分配/复制范围计量，不是 PCIe 硬件带宽计数器。
证据：[v5 原生日志](../../output/eid-perf-20260908/resident-1-on-v5.native.log)、
[开启验收](../../output/eid-perf-20260908/resident-1-on-v5.json)、
[同版本对照](../../output/eid-perf-20260908/resident-v5-comparison.md)。

### v7b 整体仍约 187 ms，初值复制量没有变

同一 v7b 二进制、同一 capture、每个 EID 4 次 steady 访问，p50 用 nearest-rank：

| 目标 EID | 恢复检查点 | 实验组合关闭 p50 | descriptor overlay + full-clear 开启 p50 | 初值复制/次 |
|---|---:|---:|---:|---:|
| 24917 | 10535 | 187.052 ms | 186.928 ms | 3,467,411,402 B，未变 |
| 29385 | 10535 | 179.841 ms | 181.109 ms | 3,103,389,812 B，未变 |

开启组内的典型 `descriptors_ms` 为 0.459 ms，关闭组为 19.355 ms；同时描述符改写量从
53,341 项降为 0。**这是局部阶段的约 19 ms → 0.4 ms，不是整体接口省了约 19 ms。**
表中整体没有体现相应收益，不能将嵌套计时相加，也不能仅凭该阶段数字给出加速结论。
该组 `1.rdc` 日志的 `fullClearInitialCopiesDeferred=0`，没有获得完整 Clear 的跳过收益。

开启、关闭、故意拒绝检查点、禁用检查点初值源四组的完整绑定输出及 pipeline witness，均与
两次独立强制回放参考一致。覆盖范围不是所有未绑定 UAV，也不是鼠标到屏幕的端到端延迟。
证据：[开启](../../output/eid-perf-20260908/clear-overlay-1-on-v7b.json)、
[关闭](../../output/eid-perf-20260908/clear-overlay-1-off-v7b.json)、
[计时对照](../../output/eid-perf-20260908/clear-overlay-v7b-comparison.md)、
[四组交叉参考](../../output/eid-perf-20260908/clear-overlay-v7b-cross-reference.json)、
[开启原生日志](../../output/eid-perf-20260908/clear-overlay-1-on-v7b.native.log)、
[关闭原生日志](../../output/eid-perf-20260908/clear-overlay-1-off-v7b.native.log)。

3.47 GB 之所以仍大，是因为恢复候选尚含大量潜在写入资源，后续 alias 阶段又无法建立有效的新起点。
UAV 表可绑定的范围通常大于着色器实际使用范围；表中的普通纹理也可能因此被保守纳入。
目前有 256 MB 的纹理数组、多个约 236 MB 的 3D RT 等大项，不能一概解释成网格重复上传。
下一步应减少有证据可排除的候选和建立后续阶段状态，不能直接按“看起来像材质纹理”跳过。

UAV 声明范围裁剪目前为实验源码，开关为 `RENDERDOC_D3D12_ENABLE_UAV_DECLARATION_RANGES=1`。
v8b 已改为取**原始可执行** SHEX/SHDR/DXIL 中的完整声明，并保留数组、space 和阶段可见性；
外部调试文件或 ILDB 不能单独构成少写证明。原始完整根签名范围仍须保留，供替换着色器、
无界/直接 heap 访问及证明不完整时回退。v8b 单元测试覆盖原始程序选择与不一致调试数据，
本记录不提前赋予这条新路径 `1.rdc` 的 v8b 性能或像素验收结论。
代码入口：[d3d12_commands.cpp](../../renderdoc/driver/d3d12/d3d12_commands.cpp)。

### 完整 Clear fixture 已证明真实跳过 524,288 B

专门的 `D3D12_Checkpoint_Triangle` capture 中，检查点 17 → 目标 48 和检查点 33 → 目标 64
的日志记录 `fullClearInitialCopiesDeferred=1`、`fullClearAllocationBytes=524288`，
即一项 512 KiB 资源的恢复被省略，随后由已证明的完整 Clear 建立正确内容。
它不是只有计划字段：原生恢复路径出现该跳过计数，后续输出与强制参考像素一致，整份报告 `ok=true`。
报告还覆盖重新配置、禁用/重新启用及小预算路径。
证据：[fixture 验收](../../output/eid-perf-20260908/clear-fixture-on-v7b.json)、
[原生日志](../../output/eid-perf-20260908/clear-fixture-on-v7b.native.log)、
[fixture 事件发现](../../output/eid-perf-20260908/clear-fixture-v8.json)。

该结果仅证明受限拓扑可行，不能外推为 `1.rdc` 已省掉恢复。延迟恢复必须同时维护内容债务、失败回退、
后续检查点保存和资源读取；Discard、GetUsage 没有记录或局部覆盖都不能代替完整 Clear 证明。
开关保持 `RENDERDOC_D3D12_CHECKPOINT_FULL_CLEAR_PREFIX=1`；descriptor overlay 另由
`RENDERDOC_D3D12_ENABLE_CHECKPOINT_DESCRIPTOR_OVERLAY=1` 控制。
实现见 [d3d12_device.cpp](../../renderdoc/driver/d3d12/d3d12_device.cpp) 与
[描述符恢复](../../renderdoc/driver/d3d12/d3d12_resources.cpp)。

## 跨 API 向前继续与共享参数：已完成的实际验收

普通向前继续协议已进入 D3D11、GL/GLES、Vulkan 的原生回放路径：只有普通目标 draw 成功后
发布 cursor；使用前先失效，只有完成后才能发布新 cursor。强制参考、分析、编辑、错误及不支持的
隐藏状态会使旧 cursor 失效或强制回退。D3D11 对含 Map 等区间仍保守拒绝，Vulkan 仍受同一
primary/pass/submission 等资格限制。D3D12 的实景与独立 fixture 增量正例见此前开发记录。

以下都是 **Windows / NVIDIA GeForce RTX 3080 Ti 单 GPU** 的离线回放验收。
OpenGLES capture 是 Windows 上的 GLES 路径，**不是 Android 设备验收**；不包含 Linux、
移动 GPU、远程回放或多 GPU。各 API 使用自己的实际 EID，不能把不同 fixture 的耗时作横向加速比。

| API / 二进制 | 四个 draw EID（A/B/C/D） | 开启命中区间及总数 | 关闭命中数 | 共享参数 V1 / V2 / 撤销阶段命中数 |
|---|---|---|---:|---|
| D3D11 / v6 | 13 / 17 / 22 / 26 | AB、CD：2 | 0 | 2 / 2 / 2 |
| OpenGL / v7b | 10 / 17 / 22 / 29 | AB、BD、AC、CD：4 | 0 | 5 / 5 / 4 |
| OpenGLES / v7b | 10 / 17 / 22 / 29 | AB、BD、AC、CD：4 | 0 | 5 / 5 / 4 |
| Vulkan / v7b | 17 / 20 / 23 / 26 | AB、BD、AC、CD：4 | 0 | 5 / 5 / 4 |

普通序列为 A→B→D→A→C→D；共享编辑阶段另含编辑后的 BD 访问，所以阶段计数可达 5，
不是同一个四区间分母。所有列出的报告 `ok=true`，缺少原生正命中的 fallback-only 运行不能通过。
GL/Vulkan v6 的零命中失败记录保留，不作为成功证据。

验收读取四个事件出现过的固定输出集合，包含后来未绑定的输出与其子资源，并与每个 EID 两次
独立强制回放逐字节/hash 比较。B/C 的视口中心像素必须在本次 draw 改变并保留到 D，以排除
“中间 draw 被漏掉但上一帧残留让结果看似相同”的假通过。读回会同步 GPU，以上不是 UI 帧率保证。

| API | 开启证据 | 关闭证据 | 共享编辑证据 |
|---|---|---|---|
| D3D11 | [on v6](../../output/eid-perf-20260908/portable-d3d11-on-v6.json) | [off v6](../../output/eid-perf-20260908/portable-d3d11-off-v6.json) | [edit v6](../../output/eid-perf-20260908/portable-d3d11-edit-v6.json) |
| OpenGL | [on v7b](../../output/eid-perf-20260908/portable-gl-on-v7b.json) | [off v7b](../../output/eid-perf-20260908/portable-gl-off-v7b.json) | [edit v7b](../../output/eid-perf-20260908/portable-gl-edit-v7b.json) |
| OpenGLES | [on v7b](../../output/eid-perf-20260908/portable-gles-on-v7b.json) | [off v7b](../../output/eid-perf-20260908/portable-gles-off-v7b.json) | [edit v7b](../../output/eid-perf-20260908/portable-gles-edit-v7b.json) |
| Vulkan | [on v7b](../../output/eid-perf-20260908/portable-vulkan-on-v7b.json) | [off v7b](../../output/eid-perf-20260908/portable-vulkan-off-v7b.json) | [edit v7b](../../output/eid-perf-20260908/portable-vulkan-edit-v7b.json) |

参数身份是**原始 buffer ID + 绑定起始偏移 + 变量/数组偏移 + 字节长度**。同一大 buffer 的
不同区间不能被误并成同一个参数。测试在固定 B 先后提交两个 16 字节值，提交后不切换 EID 即读取
结果，随后核对四个共享 draw、邻接字节窗口和强制参考，撤销后恢复原始效果。
覆盖层改变回放内存，不改写 capture 中的资产初值。输入字节变化虽小，其影响的下游 RT/UAV
仍需重新渲染；当前没有“只写 CBuffer 就自动修复所有既存阶段输出”的实现。

上述共享参数报告的 `shaderAcceptance` 均为 `notRequestedNotTested`，不能据此声称着色器替换
已经完成同等 GPU 验收。D3D11 checked shader replacement 在本 worktree 尚缺原生实现，
需要与独立 Shader ABI 工作衔接；不得为了测通而绕过 ABI 检查使用未校验替换。

默认开启源码已改为 D3D11/GL/Vulkan 对应 `ENABLE_LIVE_FORWARD` 环境变量不等于 `0` 即启用，
但原生资格校验仍执行。新 [portable 验收脚本](../../util/perf/eid_portable_forward_profile.py) 的
`--default-live-forward` 会主动移除三个环境变量，记录 `environment=null` 对应项及
`environmentPresence=false`，并要求 `--expect-hit-ranges`。原无参数模式仍显式设置 `1`；
`--disable-live-forward` 显式设置 `0`。这三个模式需要不同进程。
v8b 已另行完成四 API 默认配置验收；四份报告均 `measurementComplete=true`、`ok=true`，
三个开启变量的实际 `environmentPresence` 都为 `false`，同时完整输出和两次强制参考一致：

| API | 默认配置原生命中数 | v8b 独立证据 |
|---|---:|---|
| D3D11 | 2（AB、CD） | [default v8b](../../output/eid-perf-20260908/portable-d3d11-default-v8b.json) |
| OpenGL | 4（AB、BD、AC、CD） | [default v8b](../../output/eid-perf-20260908/portable-gl-default-v8b.json) |
| OpenGLES | 4（AB、BD、AC、CD） | [default v8b](../../output/eid-perf-20260908/portable-gles-default-v8b.json) |
| Vulkan | 4（AB、BD、AC、CD） | [default v8b](../../output/eid-perf-20260908/portable-vulkan-default-v8b.json) |

这是本机、本 fixture 的默认配置证据；关闭及共享编辑的版本仍以此前矩阵标出的 v6/v7b 为准。

## 历史显示：负例与修复正例已成对验证

[v7b 负例](../../output/eid-perf-20260908/history-fallback-d3d11-before-v7b.json)
在 headless 输出选历史 B、控制器停 D 后，执行 `SetFrameEvent(D,true)` 再 `Display()`，
确实出现历史 fallback 重绘证据缺失、完整 RGB 不等于 B 强制参考。固定 D 的参数修改/撤销也
触发相同问题。该失败用于证明回归测试能发现原缺陷，不能算为通过。

新源码在 [replay_output.cpp](../../renderdoc/replay/replay_output.cpp) 中保留历史显示的
EID、显示配置、编辑 generation 和画布尺寸。普通刷新、尺寸或编辑变化会重绘该历史 EID，
然后恢复控制器当前 EID；显式 `SetTextureDisplay` 清除历史身份并回到实时输出。
保留的是重绘来源，不增加一份历史截图。像素上下文走相同来源，绘制前清除递归入口；致命错误时
清理该身份。原有可用快照路径仍存在，不能说本轮取消了所有历史缓存。

[headless 回归脚本](../../util/perf/eid_history_fallback_profile.py) 检查完整显示 RGB、
两次独立参考、控制器仍为 D、dirty 后仍为 B、显式退出后为 D，以及可选共享参数修改/撤销。
它用强制 EID 刷新制造 dirty，不声称覆盖实际原生窗口 resize、Qt 标签切换和鼠标拖动。
后续 [v8b 修复正例](../../output/eid-perf-20260908/history-fallback-d3d11-v8b.json)
已实际 GPU 通过，`verdict=passedPersistentHistoricalFallback`、`issues=[]`。
覆盖强制 D 后的 dirty 刷新、固定 EID 共享参数更新、撤销，以及显式退出历史回到 D；
与 v7b 失败结果组成同一测试的修复前后证据。
[OpenGL](../../output/eid-perf-20260908/history-fallback-gl-v8b.json)、
[Windows GLES](../../output/eid-perf-20260908/history-fallback-gles-v8b.json) 和
[Vulkan](../../output/eid-perf-20260908/history-fallback-vulkan-v8b.json) 也通过相同的历史来源、
共享参数修改及撤销验收。**原生窗口和 Qt 面板端到端仍需验收；Windows GLES 不代表 Android 设备验收。**

## v9：扩大证明范围后的实际结果

在不改变检查点与查询恢复资格的前提下，纹理内容读取扫描允许 `BeginQuery` / `EndQuery`
经过；`ResolveQueryData` 仍不按无写入操作处理。增加“查询结束后完整清屏”的 GPU 测试帧：
[v8b 对照](../../output/eid-perf-20260908/clear-query-before-v8b.json) 像素一致但实际省略次数为 0，
因未命中优化而拒绝；[v9 正例](../../output/eid-perf-20260908/clear-query-after-v9.json)
通过 8 次实际完整清屏恢复省略，并通过临时原生着色器替换/撤销后的检查点重建与像素检查。
此项使用测试用 `BuildTargetShader` / `ReplaceResource`，不代表 Shader Workbench 的 ABI 验收，
该 D3D12 测试帧也没有运行 CBuffer 修改。

UAV 声明裁剪也允许已知、单个末尾 Draw/Dispatch 的 `ExecuteIndirect` 签名：此类命令不改变
PSO、根签名、描述符表或 Root UAV。未知命令、间接 Root UAV 和射线命令继续回退。
不过 `1.rdc` 使用的原始 SM6.6 程序还不在 v9 的证明范围，实际声明裁剪为 0。
[v9 开关对照](../../output/eid-perf-20260908/declared-uav-v9-comparison.md) 显示：

| EID | 关闭声明裁剪 p50 | 开启声明裁剪 p50 | 初始复制量是否下降 |
|---|---:|---:|---|
| 24917 | 188.553 ms | 189.946 ms | 否，仍约 3.47 GB |
| 29385 | 182.577 ms | 183.348 ms | 否，仍约 3.10 GB |

两组均通过 11 次检查点恢复与输出参考比较，但不构成性能提升。内容扫描进一步走到 10544 的
alias/split barrier 后停止，5 个候选仍无获准省略。这明确区分了独立测试帧已经执行的优化和主场景
仍未覆盖的情况。下一步的 SM6.6 支持必须依据原始 executable 的资源元数据和 handle 来源，不能
根据调试反射或一次观察到的数组下标推断所有访问。

## v10：SM6.6 静态 buffer 恢复省略已实测

SM6.6 VS/PS/CS 的有限根绑定现在使用原始 DXIL 的完整声明和真实 handle 来源证明。
只支持已知 `createHandleFromBinding` / `annotateHandle` 链，动态索引保留整个声明数组；
直接堆寻址、未知 handle、元数据错配、未知解析记录及截断字节流继续回退。新增的解析完整性
标志只作为此优化的拒绝条件，不改变普通着色器执行语义。声明裁剪仍需实验开关显式启用。

[D3D12_UAV_Declarations](../../util/test/demos/d3d12/d3d12_uav_declarations.cpp) 把两个可写 buffer
和两个 4 MiB 静态 buffer 放进同一个 4 项 UAV 表。像素着色器动态访问前两个 UAV，静态资源
通过 SRV 参与最终像素。固定四个 draw 的 EID 为 25、50、75、101。
[关闭对照](../../output/eid-perf-20260908/uav-fixture-off-v10.json) 和
[开启正例](../../output/eid-perf-20260908/uav-fixture-on-v10.json) 使用同一 capture、DLL、pyd；
关闭检查点、prepared replay 和 live-forward。五次普通往返导航均有以下真实原生结果：

| 项目 | 关闭声明裁剪 | 开启声明裁剪 |
|---|---:|---:|
| 已录制初始恢复复制字节 | 8,781,824 | 393,216 |
| 脏资源数 | 5 | 3 |
| Static2 的复制字节 | 4,194,304 | 0 |
| Static3 的复制字节 | 4,194,304 | 0 |

每次实际少录制 8 MiB `CopyBufferRegion`，不是资源尺寸估算。完整四 buffer 和 RGBA8 输出都与
两次强制参考逐字节一致，两组 35,127,296 字节的参考文件也完全相等。12 条声明裁剪记录是
6 次访问分别产生的 `WithoutDraw` / `OnlyDraw` 记录，不能解释成 12 次资源恢复节省；初次 seed
仍完整恢复。小测试的切换耗时未明显下降，因此这里只证明复制省略与正确性。

测试设备的系统运行库报告 SM6.5；测试目录单独加入微软签名有效的
[Agility SDK 1.614.0](https://www.nuget.org/packages/Microsoft.Direct3D.D3D12/1.614.0) 后，
实际设备查询返回 SM6.6 并成功捕获。DLL 仅位于本 worktree 的 `util/test/D3D12`，
[依赖收据](../../output/eid-perf-20260908/tools/agility-1.614.0/receipt.json) 保留下载来源、版本、
SHA256 和签名结果；未修改系统或主分支运行库。

### 与主分支并行运行 `1.rdc` 的限制

按用户要求，主分支 GUI 保持运行，隔离 runner 的 `-AllowForeignGui` 先确认其加载模块不属于
本 worktree，再允许独立测试；配置、日志、程序、输出路径与进程清理仍隔离。
[进程收据](../../output/eid-perf-20260908/replay-idle-declared-uav-1-on-v10.json) 明确记录同卡
GPU 条件。这不是独占显卡性能测量。

[主帧 v10 开关对照](../../output/eid-perf-20260908/declared-uav-v10-comparison.md) 的两组运行均
报告 `Insufficient GPU memory headroom for checkpoints`，检查点构建和命中均为 0，故两份
报告均正确保留 `ok=false`。两组各 14 项输出比较通过，
[跨运行参考](../../output/eid-perf-20260908/declared-uav-v10-cross-reference.json) 也一致。
在同样没有检查点的普通切换中：

| EID | 关闭时实际复制字节 | 开启时实际复制字节 | 少复制字节 |
|---|---:|---:|---:|
| 24917 | 3,955,498,234 | 3,838,329,466 | 117,168,768 |
| 29385 | 3,591,476,644 | 3,556,383,656 | 35,092,988 |

主帧确有声明裁剪命中，但没有通过检查点速度验收；也不能将此轮复制量或约 380–460 ms 的
共享 GPU 耗时与 v9 的检查点命中结果直接相比。这个小比例的省略进一步说明，剩余阶段恢复
范围和 alias 存储的处理仍是主问题。

## v11：无关别名屏障之后的完整清屏

对已经证明为独立 committed 存储的纹理，内容扫描现在允许穿过普通 alias barrier，继续寻找
完整清屏。它没有允许保存可写 alias 存储，也没有改变实际屏障执行。清屏前读取旧内容、split
barrier、未知屏障以及不在预先批准集合中的资源仍然拒绝省略。

三份同版本 GPU 验收分别覆盖：

- [完整清屏](../../output/eid-perf-20260908/alias-clear-verdict-v11b.json)：52 次纹理见证，
  17 条实际 restore 记录，其中 3 次普通恢复省略了清屏前的旧内容复制。
- [先读取后清屏](../../output/eid-perf-20260908/alias-read-verdict-v11b.json)：52 次见证，
  17 条 restore，省略为 0；见证纹理确实保留上一段完整输出。
- [混合 split barrier](../../output/eid-perf-20260908/alias-split-verdict-v11b.json)：52 次见证，
  17 条 restore，省略为 0。

两张 256×128 RGBA8 纹理的完整原生字节哈希，在所有对应普通访问与两次独立强制参考中一致。
原生 Shader 替换与复原也通过；这不是 Shader Workbench ABI 或 D3D12 CBuffer 编辑验收。
[关闭开关、启用及注入恢复失败的跨运行参考](../../output/eid-perf-20260908/alias-clear-cross-reference-v11.json)
使用同一捕获和二进制，四个 EID 的双强制参考均一致。注入失败时检查点命中为 0，正常回退仍通过。

最初自动限制 2 个检查点的正例只实际省略 1 次，未达到要求，失败报告保留；后续显式上限为 4，
避免所需早期检查点被 LRU 淘汰。这是测试条件修正，不能把之前的失败改写为通过。

## `1.rdc` 剩余复制量的资源归因

v10 开关两组的前八项实际恢复量均为 **1,286,602,752 字节（1227 MiB）**，其中：

| 资源 | 单项恢复量 | 已核实的原因/限制 |
|---|---:|---|
| 17366069 | 256 MiB | reserved 稀疏纹理，16 个 backing heap；当前初值恢复预先标脏完整组件，即使此前未写也会恢复 |
| 18993341、18993351 | 各 225 MiB | 3D 纹理，10566/10567 的 Discard 在回放中实际填充内容，不是纯状态记录 |
| 18993368 | 225 MiB | 与 18993341 使用同一 heap 偏移，不能作为独立静态纹理跳过 |
| 17368929 | 112 MiB | 10803/10810 的 CS 间接调度实际可能写入，是上游计算结果 |
| 18992991、18992992 | 各 64 MiB | 共用 placed heap；后者在 10546 被 Discard，前者还受到按整 heap 保守判断的影响 |
| 17368930 | 56 MiB | 10775/10795 的 CS 间接调度可能写入，后续确实读取 |

证据在 [实际复制日志](../../output/eid-perf-20260908/declared-uav-1-on-v10.12.timing.log) 和
[完整资源用法目录](../../output/eid-perf-20260908/inventory-1-v2.resources.json)。这里没有通过
GetUsage 缺项推断静态。所有条目的 CPU memcpy 为 0，表示此处从 GPU 可访问初值源复制，
不能据此断言捕获帧没有 CPU 更新。

后续可以保留已经建立且未再改变的完整稀疏初始组件；placed 资源则还要处理初值恢复自身的
重叠写入闭包。仅按“目标与本段脏区间不重叠”跳过仍可能被另一资源的恢复覆盖，不能直接放宽。

## v12b：独立 GUI 与真实控件验收

性能分支现在构建自己的 `x64/Development/q_render_soco_doc.exe`，并使用全新私有配置、
临时文件、ReplayLab IPC 和扩展目录。启动器核验自己的 DLL/Qt/Python 和固定 bridge，
不覆盖主工程输出，不复制个人配置，不关闭另一分支 GUI。主分支 PID 37160 在本轮始终运行。

[无捕获隔离探针](../../output/gui-isolation/probe-v12b/ui-result.json) 验证实际配置路径、
Python user-site、Python/Qt 临时路径、扩展及已加载模块；[真实界面验收](../../output/gui-isolation/presentation-v12b/ui-result.json)
在本分支 GUI 内发送实际 Qt 控件鼠标事件，完成 10 项检查、11 条控件输入路径和 48 次完整
现有输出图像读取：默认 RT0 在 held scrub 期间发布；历史单击预览保持全局 EID、双击跳转；
当前/历史标签往返及快速取消旧请求；返回当前页后 held 切换与松手画面一致。
两次运行都自行关闭测试实例，runner 记录的原用户配置前后哈希一致。

首轮 [presentation-v12](../../output/gui-isolation/presentation-v12/ui-result.json) 失败，原因是
测试计时器在 `LoadCapture` 嵌套事件循环中提前开始，随后正常加载收尾把选择恢复到末尾 EID。
脚本改为等待该调用真正返回后重测；没有把失败改写为通过，也没有为了此测试修改产品选择逻辑。

这些是小型 D3D12 捕获的 Qt 功能验收，不是 OS 鼠标/屏幕扫描输出、RT1 跨绑定记忆、
`1.rdc` 实时性能或所有平台的 GUI 验收。报告中的每阶段时长包含测试主动等待画面稳定和
读取图像的开销，不能当成输入延迟。此轮明确关闭内部服务验收，未打包的 Shader/Agent sidecar
不能据此标记可用；GUI/bridge 隔离和服务功能是不同的验收范围。

[v12b 构建](../../output/eid-perf-20260908/build-resource-versions-v12b-receipt.json)、
[原生运行身份](../../output/eid-perf-20260908/runtime-isolation-v12b.json) 和
[1,043 assertions / 56 cases](../../output/eid-perf-20260908/unit-resource-versions-v12b.log) 均通过。
新增稀疏初值驻留实验仍默认关闭，只允许单 reserved 纹理、无 placed alias 的完整组件；
真实复制提交成功才发布可复用初值，写入/映射/编辑/失败取消它。中间批次 Close 和复用列表 Reset
失败也显式取消证明，避免错误发布部分初值。是否在主帧产生收益必须另附实际 retain 和复制下降证据。

## v13：稀疏初值驻留的实际收益与失效验收

v12b 的主帧试验没有实际复用，原因是资格判断只接受 `{~0U}` 哨兵；捕获代码对非 MSAA
稀疏纹理保留显式子资源列表。v13 改为核对真实 mip、数组和 plane 总数，要求列表按序完整覆盖
`0..n-1`，仍拒绝缺项、重复、乱序和未知格式/plane。主帧的列表恰为 `[0, 1]`。

[主帧同版本开关对照](../../output/eid-perf-20260908/sparse-main1-comparison-v13.json) 通过：
两组各 10 次输出比较、每个 EID 两次独立强制参考及跨进程完整见证均一致。开启组记录 13 次
真实 `retain`；稳态两次往返中每次省略 268,435,456 字节，正好对应资源 17366069。

| EID | 关闭时实际恢复字节 | 开启时实际恢复字节 | 每次减少 |
|---|---:|---:|---:|
| 24917 | 3,838,329,466 | 3,569,894,010 | 256 MiB |
| 29385 | 3,556,383,656 | 3,287,948,200 | 256 MiB |

此对照刻意使用 1 MiB 检查点预算、两组命中均为 0，隔离“未变化的稀疏初值”这一项。
主 GUI 同时运行，共享 GPU 下观测到的稳态中位数分别为 698.75→665.20 ms、
710.66→694.35 ms；这些数据不构成独占 GPU 的速度验收，也不能和此前命中检查点的约 190 ms
直接比较。该优化没有增加这张纹理的另一份 GPU 备份，只保留尚未被改变的现有内容及映射。

专用 [关闭控制](../../output/eid-perf-20260908/sparse-fixture-off-v13b.json) 和
[开启验收](../../output/eid-perf-20260908/sparse-fixture-on-v13b.json) 也通过。六个目标的两次
独立强制参考、九次普通访问以及跨运行保存的原始数据逐字节一致：每次比较完整 128×128
稀疏纹理与 768×128 输出，共 458,752 字节。排除首次建立初值后，六次正例均实际少复制
65,536 字节；写入新内容后返回、改变物理映射后返回两次负例均重新复制并发布有效初值，
随后又能复用。没有通过忽略画面、只比较局部像素或仅看“批准候选”来判定成功。

这份脚本首次冷启动因为底层仍处于 Loading 而缺少 Active 专用日志，原报告保留为失败；
后续把冷启动单独列出并排除，然后仍要求完整的两次参考和实际优化证据。该实验继续默认关闭，
尚未把每种 Shader/共享 CBuffer 编辑与所有稀疏/placed 别名组合完成 GPU 验收。

[v13 构建](../../output/eid-perf-20260908/build-resource-versions-v13-receipt.json)、
[运行身份](../../output/eid-perf-20260908/runtime-isolation-v13.json)、
[1,055 assertions / 57 cases](../../output/eid-perf-20260908/unit-resource-versions-v13.log)、
[同版本 GUI](../../output/gui-isolation/presentation-v13/ui-result.json) 全部通过。
GUI 隔离改动已独立提交为 `1510e633a`，仍未合并或部署到原工程。

## v14：固定 EID 调值，只使实际变化的参数范围失效

本轮将编辑从“整张补丁配置变化了”细分成“哪些原始 buffer 的哪些绝对字节变化了”。
同样的数值不再触发重放；一次修改只影响配置中的后段参数时，不再因前段未变的参数仍在配置里，
把前段检查点一起清掉。原始资源与撤销层仍保留，不覆盖 RDC 文件。

公共 `ReplayResourceAccessIndex` 已实现带范围的读取、写入、未知访问和按事件顺序传播，
并分别声明读取覆盖与完整读写覆盖。它有记录条数、资源数量和查询工作量上限；别名不明确、
覆盖不完整或超过上限时不发布“可以跳过”的结论。**公共索引已实现，不等于所有后端已接完整依赖图。**

实际接入范围如下：

| 部分 | 本轮行为 | 尚未覆盖 |
|---|---|---|
| 公共编辑层 | 比较原始 buffer + 绝对字节范围的有效值，保留分层撤销语义 | 不把相同 buffer ID 当成同一个变量 |
| D3D11 | 加载时记录实际绑定的 CB 窗口，连续调值按改变范围查最早可能读取事件 | 未获得可靠窗口时仍按整 buffer，未接完整 RT/UAV 传播 |
| D3D12、OpenGL/GLES、Vulkan | 同布局连续调值按实际改变的 buffer 缩小失效起点 | 同 buffer 的不同绑定窗口暂未像 D3D11 一样细分 |
| Shader 替换 | 使旧执行状态和检查点失效，当前 EID 重新渲染 | 尚未按 Shader 的完整依赖图局部重建 |

这里按实际绑定窗口记录 CB 的可能读取，避免把原 Shader 反射出的某次实际读取当成永久边界。
修改数值即使没有改变结构，也可能让分支写入另一 UAV，或影响多个后续输出；不能只凭旧画面的
使用记录断言“只更新两个 RT 就够”。完整依赖图接入前继续保守处理这些未知项。

新建两个四次绘制的 D3D11 小捕获作为独立可控试验：一个使用两个 buffer，另一个使用同一 buffer
中不相交的绑定窗口。资源编号和事件来自实际捕获发现，仅作为脚本输入，生产代码没有这些常量。

- [两个 buffer，开启](../../output/eid-perf-20260908/delta-separate-on-v14b.json) 与
  [关闭](../../output/eid-perf-20260908/delta-separate-off-v14b.json) 均通过。
- [同 buffer 不同范围，开启](../../output/eid-perf-20260908/delta-ranges-on-v14b.json) 与
  [关闭](../../output/eid-perf-20260908/delta-ranges-off-v14b.json) 均通过。

两种开启试验均先实际创建 EID 19 的中间检查点，再在固定 EID 27 连续两次调节后段参数。
失效起点从 15 缩小到 23，两次 setter 都从原有检查点 19 恢复，未重建该点；改变字节分别为 2 和 1。
关闭试验先证明同一中间点存在，然后证明它按原策略失效。四份报告各自的五项完整输出及 buffer
窗口比较全部通过，并有两次独立强制参考。这里的 EID 仅解释试验，不能外推主帧耗时。

首次 `delta-separate-on-v14.json` 保留为失败：画面和 cutoff 已通过，但测试准备时增量前进绕过了
中间检查点构建，因而不能证明真实复用。修正的是准备流程：暂时关闭 live-forward，正常导航建立
检查点，再恢复设置进行连续 setter；没有放宽“旧中间点实际命中且零重建”的门槛。

Shader 与共享 CBuffer 组合已通过
[OpenGL](../../output/eid-perf-20260908/portable-gl-shader-cbuffer-v14.json)、
[OpenGL ES](../../output/eid-perf-20260908/portable-gles-shader-cbuffer-v14.json)、
[Vulkan](../../output/eid-perf-20260908/portable-vulkan-shader-cbuffer-v14.json)。
流程为：替换 Shader → 固定 EID 连续调两个值 → 各消费者完整参考 → 撤销 CB 恢复 Shader-only →
撤销 Shader 恢复原始结果。读回若触发隐式补绘，即使补绘后的像素正确也判失败。
这些是 Windows 图形 API 验证，不是 Android/Linux 的设备验收；原生 checked 替换验证身份及管线
安装条件，完整 Shader ABI 检查仍由工作台承担。

D3D11 组合首轮被其未实现的 checked native 入口拒绝；这份失败报告保留，后续补齐入口后另测。
D3D12 本轮还实现了同布局参数上传对象复用，但在 GPU fence 的完成证明和新构建验收完成前，
不将其计为已验收收益。上述 v14 构建通过，原生过滤测试为 4,270 assertions / 106 cases。
复现与判定规则见 [Buffer delta 验收指南](../../util/perf/BUFFER_DELTA_ACCEPTANCE.md)。

## v15–v17：Shader 组合、GPU 完成证明与主帧回归

D3D11 已补齐 checked native Shader 替换和撤销。它核验原始身份、目标对象所属控制器、实际原生
stage、创建调用、替换归属，事务失败保留旧映射；不支持普通 BuildTargetShader 重建的
stream-output、class linkage 和 vendor-extension 创建仍明确拒绝。完整 Shader ABI 校验继续留在
工作台，未用这项原生事务代替它。共享 CB 也补了反向替换归属检查，避免通过另一个资源 ID 使用
同一目标时绕过原先的消费者索引。

[D3D11 v15 组合验收](../../output/eid-perf-20260908/portable-d3d11-shader-cbuffer-v15.json) 通过，
还在已有合法 PS 替换时尝试安装生成的 CS，证明拒绝后当前 EID、两个编辑代次、PS 绑定、参数字节及
完整输出均未改变。没有通过切 EID 或 getter 隐式补绘修复结果。
[D3D12 v17 组合验收](../../output/eid-perf-20260908/d3d12-shader-cbuffer-v17.json) 也通过：
固定 EID 连续调值、四个共享消费者的两次独立参考、CB 撤销恢复 Shader-only、Shader 撤销恢复原始
结果全部成立。D3D12 专用脚本只验正确性，不把跨四个提交的小帧误当成 live-forward 加速正例。

D3D12 同布局调值复用原有上传对象。CPU 覆写前仍必须证明旧 GPU 使用已经完成；同时修复原等待
代码忽略超时/失败的问题，核验 Signal、事件注册、等待返回值和实际 fence 完成值。
超时不会被当成成功，设备移除返回的最大 fence 值也不会被当成完成。这遵循
[GetCompletedValue](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12fence-getcompletedvalue)
与 [WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)
的返回语义。未确认完成时，参数覆写、命令分配器复用、检查点发布和旧资源退休均停止；保存途中失败
的检查点保留 GPU 对象持有，但不发布为有效检查点。此轮没有模拟真实设备永久挂起，也没有重构
整个设备异常析构/延迟 COM 销毁，不能宣称覆盖完整故障恢复。

复审发现并撤回了 v15 通用等待成功后更新“已完成”标记的改动：批量 raw command-list 提交可能在
该标记之后继续提交工作。v17 只在参数编辑事务的明确静止边界使用完成证明；Shader PSO 清理若再次
等待失败，也会在修改映射前退出。保留 v15/v16 构建记录，最终交付使用 v17。

[v17 构建](../../output/eid-perf-20260908/build-buffer-delta-v17-receipt.json)、
[运行隔离](../../output/eid-perf-20260908/runtime-isolation-buffer-delta-v17.json)、
[4,319 assertions / 108 cases](../../output/eid-perf-20260908/unit-buffer-delta-v17.log)、
[95 项脚本 CPU 测试](../../output/eid-perf-20260908/python-buffer-delta-v17.log) 均通过。
构建含独立 GUI；原工作区程序 PID 37160 未关闭，加载模块核验未引用本工作区二进制。

[1.rdc v17 回归](../../output/eid-perf-20260908/main1-checkpoints-v17.json) 的八项完整输出比较通过，
覆盖 EID 24917/29385 的往返、禁用、重新启用及小预算，并拒绝读回中的隐式重放。
**检查点性能验收仍为失败/未证实**：本次 DXGI 报告预算约 3.8 GB、回放进程使用约 7.8 GB，
原生准入返回 `Insufficient GPU memory headroom for checkpoints`，实际命中为 0。
往返约 658–693 ms 的观测来自共享 GPU 的完整恢复路径，不代表检查点加速结果，更不能宣称达成
实时拖动。没有为拿到通过结果而绕过显存门槛或关闭另一分支的程序。

最终 v17 还重跑了两个 buffer 和同 buffer 不同窗口的四份开关试验：
[separate-on](../../output/eid-perf-20260908/delta-separate-on-v17.json)、
[separate-off](../../output/eid-perf-20260908/delta-separate-off-v17.json)、
[ranges-on](../../output/eid-perf-20260908/delta-ranges-on-v17.json)、
[ranges-off](../../output/eid-perf-20260908/delta-ranges-off-v17.json)，全部通过，
保留上述 cutoff 15→23、恢复点 14→19 和零重建证据。

D3D12 单个 float32 的 [上传复用开启](../../output/eid-perf-20260908/d3d12-upload-on-v17.json) 与
[关闭对照](../../output/eid-perf-20260908/d3d12-upload-off-v17.json) 均通过。固定 EID 74 的四次 setter
中，开启组 `uploadReused` 为 `0,1,1,1`，后续三次不再分配新上传对象；每次上传 4 字节。
它仍会上传该补丁的完整 4 字节，不把“实际改变 1–2 字节”误报为“只上传 1–2 字节”。
两组都验证参数邻域、所有绑定输出、三次可见值变化及清除补丁恢复原始结果。
这份小帧没有可复用的编辑前提交检查点，`steadyHitUpdates=0`，上传分配减少与检查点提速不能混为一项。

最终 [Qt 纹理面板验收](../../output/gui-isolation/presentation-v17b/ui-result.json) 十项通过：
按住拖动状态时 RT0 发布、真实预览控件点击、历史单击预览、历史/当前标签往返、双击切 EID、
返回及释放后画面保持正确。[独立运行收据](../../output/gui-isolation/presentation-v17b/runner-receipt.json)
通过，原用户配置哈希未变。这是可控小帧的控件/像素验收，脚本轮询间隔也计入观察时间；
没有拿它代替 `1.rdc` 大量历史条目或真实鼠标输入到屏幕呈现的延迟测试。

## 为什么投入较久，后续应集中在哪里

目前的收益证明“保留正确的工作状态并增量执行”方向可行；没有证明“多存几个现有检查点”就能解决
`1.rdc`。现有可保存点停留在 10535，后续大量候选因不支持的可写存储被拒绝。最重要的缺口是
**后续 alias 阶段检查点**，公共版本类、候选分段和 480 MB 驻留复用都不能代替它。

耗时主要花在把“可以跳过”的想法变成不会漏写的证明：真实提交发生前后绑定可以变，数组可以动态
索引，多个资源可别名到同一内存，编辑会改变后续依赖；某些回放还包含 query、隐藏状态和间接命令。
此外，新增测试曾真实拒绝零命中的 GL/Vulkan 路径，也复现了历史画面错误。保留这些失败证据，
可以防止把“图像看着没坏”误作优化已经执行。

下一步以本篇“通用资源访问索引与加载时检查点”要求推进，以下取代 v8b 时的旧待办；四 API
基础共享编辑、UAV 主帧实验与小帧 Qt 标签输入已有上文证据，不再列为尚未开始：

1. 补全跨 API 加载访问索引和完整性接口，由阶段活跃范围与成本规划预建；实现前不得把现有
   首次写入表和启发式选点描述为完整通用方案。
2. 实现 alias 存储组件的阶段检查点：记录 backing heap/页映射、激活资源、布局/状态、内容版本、
   query/执行环境以及实际可恢复范围。先选择一个明确的后续深度或 GBuffer 边界，在 A→B→A、
   跨检查点和受控编辑后验证完整状态；选点由通用规则产生，不硬编码测试帧 EID。
3. 基于真实依赖图维护检查点失效：固定 EID 参数滑动优先完成当前输出，从最早受影响的有效前缀
   重算；结束调节后再限额后台重建下游检查点。只读资产继续共享，受影响输出必须更新。
4. 将阶段范围恢复和预建接入各 API 的原生资源与执行环境，补大帧真实拖动、历史大量条目、
   跨输出组 RT 选择记忆及输入到显示延迟；Windows 各 API 测试之外，再补 Linux、Android/GLES、
   其他厂商及远程环境。

因此本轮状态是“增量执行和局部恢复优化已成立，主场景的后续阶段恢复仍未解决”，不能标记全部开发完成。

## v19 实测与 v20 后续实现（2026-09-08）

本节区分已经运行的二进制证据与后续源码，避免把正在开发的功能当成已经验收。
隔离目录和主分支进程仍然分开；物理 GPU、系统显存预算仍共享。

v19 已实际通过：

- [构建](../../output/eid-perf-20260908/build-stage-v19-receipt.json)，
  [原生测试](../../output/eid-perf-20260908/unit-stage-v19.log)：4736 assertions / 123 cases。
- D3D11 多阶段 RT 检查点：完整比较三张阶段 RT 和当前输出，5 次原生命中；
  一次恢复日志中复制 481,024 字节，驻留版本省略 1,441,024 字节。保存的是可继续渲染的资源状态，
  不是截图。[报告](../../output/eid-perf-20260908/checkpoints-d3d11-stages-v19.json)
- D3D12 缓冲区物理别名：开启/关闭路径都通过，四个完整 buffer 和当前颜色/深度输出逐次比较，
  独立完整回放作参照。此时还不包含重叠纹理的 typed 恢复。
  [开启](../../output/eid-perf-20260908/checkpoints-d3d12-buffer-alias-on-v19.json)、
  [关闭](../../output/eid-perf-20260908/checkpoints-d3d12-buffer-alias-off-v19.json)。
- 自动空闲预建：D3D11、GL、GLES 各 2 次真实构建，Vulkan 静态/动态 UBO fixture 各 1 次；
  每份报告均有 4 次随后切换的原生命中，当前图像与完整回放一致。
  [D3D11](../../output/eid-perf-20260908/prewarm-d3d11-stages-v19.json)、
  [GL](../../output/eid-perf-20260908/prewarm-gl-ranges-v19.json)、
  [GLES](../../output/eid-perf-20260908/prewarm-gles-ranges-v19.json)、
  [Vulkan](../../output/eid-perf-20260908/prewarm-vk-ranges-v19.json)。
- [Qt 纹理控件](../../output/gui-isolation/presentation-v19/ui-result.json) 10 项通过；
  独立运行收据确认原用户配置未变，未结束其他分支进程。

v19 失败也保留：

- GL/GLES 同 buffer 范围的修改 cutoff 和图像正确，但严格要求的中间检查点复用没有命中。
  定位到 live-forward 开关被首次调用的静态变量缓存，测试准备阶段无法关掉它；v20 改为逐次读取。
- Vulkan 动态 UBO fixture 的修改未命中范围索引，退回从头重算。加载时只记录 descriptor set，
  未记录实际 dynamic offsets；v20 补齐两版绑定命令的加载路径，重新测试前不称修复已验收。
- D3D12 自动预建首选阶段落在第一次完整命令提交结束之前，未创建检查点；v20 增加原生候选规划
  证据，先跳过无效位置的 GPU 重绘，再在有限的后续空闲单元尝试其他位置。
- [1.rdc](../../output/eid-perf-20260908/prewarm-main1-v19.json) 输出比较通过，要求构建/命中的验收失败。
  此次进程 CurrentUsage 为 7,814,643,712 字节、可用预算为 5,653,356,544 字节，原生保护因此拒绝
  新检查点。CurrentUsage 是该进程的用量；其他进程会影响它获得的预算。不可把此结果解释为
  后续阶段已流畅，也不可取消预算保护强行宣布优化命中。

v20 源码增加 GL/GLES 和 Vulkan 的不可变 GPU 内容版本共享、弱引用驻留身份和实际写入失效；
D3D12 增加原生 CBV 字节范围索引与重叠纹理活跃 owner 的 typed 备份路径。CBV table 的视图大小
来自原生描述符，root CBV 无显式大小时保守使用剩余 buffer。描述符槽保存所有捕获版本的并集，
不会用录制时或最后一次堆内容假装证明实际提交的绑定；这可能降低精度，但保证不漏掉消费者。

仍需继续验收：多阶段 RT 共享正例、反向/跨点切换、固定 EID 连续调参及撤销、Shader 修改后重建、
失败/预算/未知资源回退。特殊 alias、CPU 写入、bindless/generated addresses 等证据不足时保留完整
恢复；Windows 的几种 API 通过不等于 Linux、Android、其他 GPU 或任意捕获全部已测试。

## v21 统一验证与尚未通过的主场景

隔离 v21 [构建收据](../../output/eid-perf-20260908/build-stage-v21-receipt.json) 为
`success=true/sourceStable=true`；[原生检查](../../output/eid-perf-20260908/unit-stage-v21.log)
为 4949 assertions / 139 cases，[Python 检查](../../output/eid-perf-20260908/python-v21-all.log)
为 278 tests / 1 skipped；翻译同步检查为 4899 finished / 0 unfinished。后续 v22 源码不属于此构建。

实际 GPU 证据（Windows / 当前 GPU 驱动）包括：

- D3D12 同 buffer 的两个 CBV 范围：修改后段参数的最早消费者从提交基点 6 精确到 draw 50，
  保留并命中中间原生检查点 35。两次连续固定 EID 修改、邻居字节、全部输出和撤销均通过。
  [开启](../../output/eid-perf-20260908/delta-d3d12-ranges-on-v21-r2.json)、
  [关闭差异索引对照](../../output/eid-perf-20260908/delta-d3d12-ranges-off-v21-r2.json)。
  检查点是原生 Close/提交边界，不要求它本身是 draw；绑定和显示 EID 仍要求真实 draw。
- GL/GLES 多阶段纹理各 19 次命中，完整比较绑定输出、两张阶段 RT 与 D24S8。
  后段每次保存复制 480,000 B，共享 1,800,000 B；重复配置保留状态，1 B 预算正确回退。
  [GL](../../output/eid-perf-20260908/checkpoint-gl-texture-versions-on-v21.json)、
  [GLES](../../output/eid-perf-20260908/checkpoint-gles-texture-versions-on-v21.json)。
  v21 所有恢复的 `textureResidentBytes=0`：公共控制器无条件绕过 OnlyDraw，清除了驻留证明。
  v22 才修复此接入；这里的保存共享不能当作已经省去恢复复制的证据。
- Vulkan 多阶段 RT 保存 3 点、命中 15 次，包含所有当前输出和两张额外 RT；
  [报告](../../output/eid-perf-20260908/checkpoint-vk-stages-on-v21.json)。最初 fixture 在第一 EID
  读取未来尚未初始化的 RT1，后改为第一 draw 前初始化两张 RT，再重新捕获；未放宽画面验收。
- v20b 的 D3D11/D3D12/GL/GLES/Vulkan 着色器替换＋共享 CBuffer 连续修改＋撤销全部通过，
  即时输出先记录，之后才进行强制参考。它们仍需在改变公共 OnlyDraw 行为后的版本回归。
  [D3D12](../../output/eid-perf-20260908/d3d12-shader-cbuffer-v20b.json)、
  [D3D11](../../output/eid-perf-20260908/portable-d3d11-shader-cbuffer-v20b.json)、
  [GL](../../output/eid-perf-20260908/portable-gl-shader-cbuffer-v20b.json)、
  [GLES](../../output/eid-perf-20260908/portable-gles-shader-cbuffer-v20b.json)、
  [Vulkan](../../output/eid-perf-20260908/portable-vulkan-shader-cbuffer-v20b.json)。
- 自动空闲预建已在 D3D12 buffer 别名 fixture 上创建 2 点、后续命中 3 次；
  [报告](../../output/eid-perf-20260908/prewarm-d3d12-buffer-alias-v20b-r2.json)。

`1.rdc` 的 [v21 连续往返](../../output/eid-perf-20260908/prewarm-main1-continuous-v21-r2.json)
画面均通过双参考比较，但仍未创建/命中检查点，不能称主场景已流畅。第一次 682.59 ms，后续
486.13–501.77 ms；独立进程仍受主 GUI 的物理 GPU 占用影响。旧 prewarm 工具在每个 visit 后
插入 force，导致每次都测完整恢复；现在先完成连续 visit 和即时读取，再统一进行双参考比较。
返回 24917 的默认稳态恢复量为 3,955,498,234 B，与 v17 默认一致。旧约 3.57 GB 数字额外启用了
UAV 声明范围和稀疏驻留实验选项，不能混作默认基线。约 7.68 GB 是 force 后完整恢复搬运量，
不是检查点显存占用。保留捕获 UPLOAD heap 的 A/B 只减少约 149 MiB 本地显存，且更慢，维持关闭。

重叠 RT 的 v21 仍被旧 NULL-before 别名准入规则拒绝，画面只证明了完整回退正确；v22 源码仅在
该原生边界已有完整 typed owner 证明时放开明确 after 的激活。后续仍需验证实际命中，并处理
非活动 RT 的初值复制与中断后的恢复责任。公共模型适用于任意捕获，但未知访问/别名/同步必须
保守回退；这些 Windows 测试不代表所有平台、所有捕获均已完成物理验收。

## v22：跨 API 实际恢复、连续调参与主帧复测

[构建收据](../../output/eid-perf-20260908/build-stage-v22-receipt.json) 为
`success=true/sourceStable=true`；[原生测试](../../output/eid-perf-20260908/unit-stage-v22.log)
通过 5537 assertions / 171 cases，[Python](../../output/eid-perf-20260908/python-v22-all.log)
通过 280 tests / 1 skipped。其后追加的上传验收门槛另有
[20 项纯测试](../../output/eid-perf-20260908/python-upload-gate-v22.log)，不是重新计算原生测试数。

这次补齐了以下实际 GPU 证据：

- D3D12 typed alias：2 个原生检查点（25、42）、13 次命中，绑定颜色/深度全字节与双参考一致。
  分配 2,105,344 B，共享 1,056,876 B。
  [报告](../../output/eid-perf-20260908/checkpoint-d3d12-typed-alias-on-v22.json)。
  **v22 仍未省略 inactive typed RT 的初值复制**，不能把命中当成这项带宽优化已完成。
- GL/GLES：各 19 次命中；公共 OnlyDraw 绕过修复后，后段恢复实际复制 480,000 B，
  `textureResidentBytes=1,800,000`，两张额外阶段 RT、D24S8 和全部当前输出均与双参考一致。
  [GL](../../output/eid-perf-20260908/checkpoint-gl-texture-versions-on-v22.json)、
  [GLES](../../output/eid-perf-20260908/checkpoint-gles-texture-versions-on-v22.json)。
  弱引用只证明当前工作内容仍为目标版本；写入、分析/强制回放、替换仍清除相应证明。
- Vulkan 多阶段 RT：15 次命中，完整绑定输出及两张上游 RT 比较通过。
  [报告](../../output/eid-perf-20260908/checkpoint-vk-stages-on-v22.json)。
- GL/GLES 固定布局连续调参复用上传对象，实写 2 B、1 B；独立关闭对照写完整 8 B 补丁。
  新验收门槛要求原生上传字节和实际检查点复用同时成立，四份报告均通过：
  [GL 开启](../../output/eid-perf-20260908/delta-gl-upload-compact-v22.json)、
  [GL 关闭](../../output/eid-perf-20260908/delta-gl-upload-full-v22.json)、
  [GLES 开启](../../output/eid-perf-20260908/delta-gles-upload-compact-v22.json)、
  [GLES 关闭](../../output/eid-perf-20260908/delta-gles-upload-full-v22.json)。
  这里只减少参数上传，不代表整个前缀重算只有 1–2 B 工作量。
- 捕获 CPU 写入：新的 2 MiB Buffer fixture 中，序列化写入虽覆盖整个 Buffer，加载时独立
  初始源比较只得到 0–8 个实际差异字节。检查点关闭的稳态普通恢复实际从 2,097,152 B
  减至 1、2 或 8 B；首次配置后的恢复仍完整执行。整个 2 MiB Buffer 与所有绑定输出通过，
  开启/关闭各 13 次检查点命中。
  [开启](../../output/eid-perf-20260908/checkpoint-cpu-ranges-on-v22.json)、
  [独立关闭对照](../../output/eid-perf-20260908/checkpoint-cpu-ranges-off-v22.json)。
  原始捕获及初始源未修改，GPU/UAV/别名/来源不明或预算耗尽继续走完整恢复。
- 五条 API 路径的 Shader 替换＋固定 EID 连续共享参数修改＋两层撤销均通过：
  [D3D11](../../output/eid-perf-20260908/portable-d3d11-shader-cbuffer-v22.json)、
  [D3D12](../../output/eid-perf-20260908/d3d12-shader-cbuffer-v22.json)、
  [GL](../../output/eid-perf-20260908/portable-gl-shader-cbuffer-v22.json)、
  [GLES](../../output/eid-perf-20260908/portable-gles-shader-cbuffer-v22.json)、
  [Vulkan](../../output/eid-perf-20260908/portable-vulkan-shader-cbuffer-v22.json)。
- 另测 Shader 替换后检查点失效、实际重建/恢复和撤销：
  [D3D11](../../output/eid-perf-20260908/checkpoint-d3d11-shader-mutations-v22-r2.json)、
  [D3D12](../../output/eid-perf-20260908/checkpoint-d3d12-shader-mutations-v22.json)、
  [GL](../../output/eid-perf-20260908/checkpoint-gl-shader-mutations-v22.json)、
  [GLES](../../output/eid-perf-20260908/checkpoint-gles-shader-mutations-v22.json)、
  [Vulkan](../../output/eid-perf-20260908/checkpoint-vulkan-shader-mutations-v22.json)。
  D3D11 第一次采用正确的 live-forward，因未满足“目标必须用检查点”的门槛而失败；第二次
  显式关闭 live-forward 后通过。没有修改门槛或把另一个阶段的命中冒充目标命中。
- [Qt 回归](../../output/gui-isolation/presentation-v22/ui-result.json) 10 项通过；
  [运行隔离](../../output/gui-isolation/presentation-v22/runner-receipt.json) 确认没有结束其他分支进程。
  200 ms 轮询的控件测试不等于实际鼠标到显示延迟测试。

`1.rdc` 的 [v22 连续往返](../../output/eid-perf-20260908/prewarm-main1-continuous-v22.json)
完成五次即时画面与双参考比较，全部一致，但建点/命中门槛仍失败。进程本身的 DXGI
`CurrentUsage=7,814,643,712 B`，系统给它的 `Budget=3,840,815,102 B`，安全保护拒绝新快照。
这不是把两个进程的显存简单相加；其他应用竞争会影响当前进程获得的预算。
首次 1192.73 ms，后续 722.68–1084.13 ms；共享 GPU 状态与 v21 不同，不能当作独占速度回归。
返回 24917 的稳态实际恢复量为 3,942,915,473 B，比 v21 默认减少 12,582,761 B；
CPU 字节差异优化有收益，但远不足以消除主帧恢复量。

后续具体缺口：inactive typed 初值省略必须证明“读旧值之前完整覆盖”，并在中断/强制回放时
补齐独立恢复债务；显存后来释放时需要有限空闲重新准入；Vulkan 可选绑定证据的生产端需要
总量上限。公共 `PropagateEdit` 仍未生产接入完整逐 RT 依赖图，当前 Shader 保守全失效，
共享 CBuffer 则按已证明的首次消费者保留此前检查点。不能称任意下游 RT 均已局部重算。
所有规则必须从原生捕获证据形成，禁止依赖此处 fixture 的 EID、资源 ID 或文件名；
Windows 五条 API 的通过仍不代表 Linux、Android、其他厂商设备均已物理验收。

## 测试与复现入口

- [v7b 原生单元测试](../../output/eid-perf-20260908/unit-resource-versions-v7b.log)：
  641 assertions / 36 cases 通过，过滤组含资源版本、Clear 前缀、写入阶段、live-forward 和描述符恢复。
  这份收据只对应 v7b，当时尚未包含全部后续 v8b 修改。
- [v8b 原生单元测试](../../output/eid-perf-20260908/unit-resource-versions-v8b.log)：
  813 assertions / 46 cases 通过，新增覆盖原始 executable UAV 声明、GL 异常恢复 RAII 和历史显示身份。
- [v9 原生单元测试](../../output/eid-perf-20260908/unit-resource-versions-v9.log)：
  885 assertions / 48 cases 通过，另有 [v9 构建收据](../../output/eid-perf-20260908/build-resource-versions-v9-receipt.json)
  和 [v9 运行隔离](../../output/eid-perf-20260908/runtime-isolation-v9.json)。
- v10 的 [原有过滤组](../../output/eid-perf-20260908/unit-resource-versions-v10.log) 907 assertions / 49 cases，
  [DXIL root-bindings](../../output/eid-perf-20260908/unit-dxil-root-v10.log) 61 assertions / 3 cases 均通过；
  [v10 构建收据](../../output/eid-perf-20260908/build-resource-versions-v10-receipt.json)、
  [运行隔离](../../output/eid-perf-20260908/runtime-isolation-v10.json) 和
  [UAV 验收脚本的 16 项纯测试](../../output/eid-perf-20260908/python-uav-v10.log) 也通过。
- [v11 原生测试](../../output/eid-perf-20260908/unit-resource-versions-v11.log)：997 assertions / 53 cases；
  [构建](../../output/eid-perf-20260908/build-resource-versions-v11-receipt.json)、
  [运行隔离](../../output/eid-perf-20260908/runtime-isolation-v11.json) 均通过。
  alias 发现脚本 7 项、独立报告验收器 9 项纯测试通过。
- [portable 纯测试](../../util/perf/test_eid_portable_forward_profile.py)：默认配置模式加入后 16 项通过；
  [共享编辑纯测试](../../util/perf/test_eid_portable_forward_edit_profile.py) 12 项、
  [历史 fallback 纯测试](../../util/perf/test_eid_history_fallback_profile.py) 6 项通过。
  纯测试没有调用 GPU，不能代替上述真实捕获验收。
- 构建身份与环境隔离参考 [v7b 构建收据](../../output/eid-perf-20260908/build-resource-versions-v7b-receipt.json)、
  [v7b 运行隔离](../../output/eid-perf-20260908/runtime-isolation-v7b.json) 和
  [隔离回放说明](../../util/perf/ISOLATED_REPLAY.md)。
- [v8b 构建收据](../../output/eid-perf-20260908/build-resource-versions-v8b-receipt.json) 与
  [v8b 运行隔离](../../output/eid-perf-20260908/runtime-isolation-v8b.json) 已通过；
  v8b 报告各自保留加载二进制身份，不复用 v7b 身份作为依据。

默认配置验收命令示例（在本 worktree 中使用隔离宿主，需要当前没有其他回放占用 GPU，
并使用新输出文件名）：

```powershell
& ./util/perf/run_eid_isolated_python.ps1 `
  -Script util/perf/eid_portable_forward_profile.py `
  -ScriptArguments @('--capture', 'output/eid-perf-20260908/shared-capture-v5/20260908-154734/D3D11_Shared_Buffer_frame5.rdc',
                     '--output', 'output/eid-perf-20260908/portable-d3d11-default-v8b-rerun.json',
                     '--build-label', 'resource-versions-v8b', '--default-live-forward',
                     '--expect-hit-ranges', 'AB,CD', '--min-hits', '2') `
  -RunLabel portable-d3d11-default-v8b-rerun -RequireIdleReplay

& ./util/perf/run_eid_isolated_python.ps1 `
  -Script util/perf/eid_history_fallback_profile.py `
  -ScriptArguments @('--capture', 'output/eid-perf-20260908/shared-capture-v5/20260908-154734/D3D11_Shared_Buffer_frame5.rdc',
                     '--output', 'output/eid-perf-20260908/history-fallback-d3d11-v8b-rerun.json',
                     '--build-label', 'resource-versions-v8b', '--validate-shared-edit') `
  -RunLabel history-fallback-d3d11-v8b-rerun -RequireIdleReplay
```

示例使用新的 `-rerun` 输出名，避免覆盖已验收证据。后续测量须另附实际文件、二进制身份和限制。
