# 项目共享内容：存什么、何时存、何时用、谁让谁

适用范围：`D:\...\<项目>\`（replay project）。所有跨 rdc 复用的内容只有这一处，
`%LOCALAPPDATA%\ReplayLab` 下只保留每台机器自己的配置、日志和 IPC。

## 1. 存什么（四类，各一处）

| 内容 | 位置 | 键 | 写入者 |
|---|---|---|---|
| 着色器知识（整理后的源码、流程图、CBuffer 名、替换候选） | `.replaylab/replay-project/project.json` → `knowledge.workspaces`，实体在 `workspaces/shaders/v2/<ws>/` | 原始字节 `rawSha256` | AI 整理完成、用户保存 |
| CBuffer 别名（块名 + 成员名） | `shared/dictionaries/cbuffer-aliases/<layoutSha256>.json` | 反射布局（块名 + `name@offset`） | AI 重命名、用户在面板里命名 |
| 材质知识 / 参数快照 | `shared/…`（材质面板） | 资产 id / 事件 | AI、用户 |
| 恢复记录（每个 rdc 上一次恢复发生了什么） | `.replaylab/replay-project/derived/restore-results/<rdcPathSha256>.json` | rdc 路径 | 内部服务 |
| 解包资产索引（项目解包目录里有什么、谁引用谁） | `shared/dictionaries/assets/asset-catalog-v1.sqlite3`（一个就地更新的 SQLite 索引） | 相对路径 + (大小, mtime, 内容哈希) | 后台索引任务 |

规则：
* **一个 buffer 一个别名文件、一个 shader 一份知识**。文件名就是键；不存在"用户版"和"AI 版"两棵树。
* 别名文件内部记录每个名字的来源（`user` / `ai`）与时间。**用户名字永远压过 AI 名字**：AI 只补用户没命名的成员，不覆盖。
* 旧的每用户别名目录（`%LOCALAPPDATA%\ReplayLab\aliases\cbuffer`）在项目第一次接触到同一布局时一次性并入项目共享，之后不再读取。
* **解包资产只建索引，不拷贝**。目录（`SilverPalace_Unpacked` 之类）本身就是事实来源；索引里每行只有路径、指纹（大小/mtime/SHA-256）和一小段解析摘要（类名、引用、贴图指纹、少量参数值）。
  重新扫描时指纹没变的行原样保留，变了的行就地重写，文件没了的行在整轮走完后删掉；每行带自己的摘要哈希，交付时逐行校验，目录身份是所有行哈希的异或——读的时候不再把整个索引重新哈希一遍。
  批量数据（`.ubulk`、音频、字体、代码、本地化、管线缓存）只登记名字，不读内容。
* **不用捕获里的贴图名找资产**。发行版游戏的 GPU 资源基本没有资产名，有也不是美术的名字；Host 的排序不再用它们。资产匹配靠可比较的东西：draw 的 cbuffer 数值 ↔ 材质参数值、绑定贴图的像素指纹 ↔ 资产贴图（容忍缩放/镜像）、shader map / vertex factory / 混合模式 ↔ 代码结构、网格类型、以及用户自己说的词。每一项只增减分数，不一票否决。
* **先看目录，再定范围**。`profile_unpacked_asset_directories` 从名字索引给出每个目录里材质/贴图/网格/shader 的数量；AI 据此选出可能的文件夹，`retrieve_shader_asset_candidates(scope_prefixes=[…])` 只解析这些文件夹里的描述性文件（包头、材质导出、JSON、贴图指纹），把其中每个材质都当候选去比。匹配确定后 `submit_shader_asset_resolution(links_json=…)` 把"这个 draw 的贴图槽/cbuffer/网格/函数 ↔ 资产里的名字"记进解析库（按游戏、shader 哈希、结构键），下次同一个或同形状的 shader 直接从 `memory` 拿到，用来重命名。
* **解包后的资产不需要密钥**。已解包的目录就是散文件，CUE4Parse 直接挂载这个目录即可；uex 需要的只有"挂哪个目录"和"哪个引擎版本"两件事，由 Host 从项目的资产根目录自动生成配置（`%LOCALAPPDATA%\ReplayLab\tools\uex\profiles.json`，`unpacked-<sha8>`，keys 为空），引擎版本直接从 cooked 包头读出来（`FPackageFileSummary` 里的 UE4/UE5 object version，按 CUE4Parse 的 `EGame.GetVersion` 反查；读几个不同目录的 .uasset 即可，不挂载也不穷举），读不出来才按从新到旧依次试；AI 或用户也可以用 `set_unpacked_asset_engine_version` 直接定版。挂载目录就用项目里配置的解包路径，只有当它下面还隔着一层（extract-raw 的 `raw/`）时才往下找一层。`profiles.json`/AES 密钥只在"挂原始 .pak/.utoc"这个可选模式下才需要。配方里的 `{uexConfig}`/`{uexProfile}`/`{uexPath}` 是 Host 填的槽位，调用方既不用知道也传不进来。
* **解包器随程序走**。四个解包器放在 `replaylab/third_party/unpackers/<kind>/`（部署后为 `<程序目录>/replaylab/tools/unpackers/`），注册表里只记 `%BREAD_TOOLS%/...` 这样的相对令牌，加载时按当前程序目录展开；AgentHost 启动时自动登记/更新/移除捆绑条目（`source: bundled`），不会覆盖用户自己登记的解包器。UnityPy 内嵌在 AgentHost 里（`replaylab-ai unpacker unitypy`），Skills 也在冻结的 AgentHost 内。整个程序目录拷给别人即可用，无需再配路径。
* **先找、再索引，不是先索引完再找**。第一次接触一个解包目录只做一次"报名字"的遍历（路径词，不哈希不解析，几十万个文件约一分钟）。之后每次 AI 问某个 shader，用当前上下文的词（用户提示、绑定的贴图名、shader 自己的资源名、pass/mesh）在名字索引里挑出几十个可能相关的文件（材质及其 .uexp、.mat、JSON 描述），只把这些文件解析进索引，再排序、比对 cbuffer 数值和贴图指纹。全树的深度索引（`index_active_unpacked_assets`）变成可选的后台加速，不再是前提。旧的"每个检查点整库重写"留下的 `.asset-catalog-v1.sqlite3.tmp-*` 会在下次扫描开始时清掉。

## 2. 何时存

* AI 整理：`rename_shader_workspace_symbols` 一次调用同时写块名和成员名 → 立即发布到别名文件；整理完成 → `register_replay_project_workspace` 把工作区登记为该 `rawSha256` 的知识头。
* 用户：面板里改名 → 立即写别名文件（来源 `user`）；编辑器 Ctrl+S → 新 revision 进工作区，经 ABI 门禁后替换。
* 恢复：从不写知识，只写恢复记录。

## 3. 何时用（打开 rdc）

1. 读 rdc 内的项目索引缓存；没有或过期 → 按项目里全部知识头的 `rawSha256` 在当前捕获里做一次有界扫描（`begin_rdc_project_shader_resource_index`，只找这些哈希，不做全帧）。
2. 命中的每个 shader：解析/准备工作区 → 用项目候选编译 → ABI 门禁 → 替换到当前捕获。
3. 别名不需要"恢复"：qrenderdoc 打开任何 CBuffer 时按布局键直接读项目共享文件。
4. 结束时把摘要写进控制台和恢复记录（含带时间戳的阶段轨迹）。

## 4. 谁让谁（冲突规则）

| 场景 | 规则 |
|---|---|
| 恢复 vs 编辑器实时监听（同一工作区） | 恢复从拿到工作区起到应用/回滚结束，**暂停该工作区的监听**；恢复完成后监听器把磁盘上的源码当作新基线，不补编译。 |
| 恢复 vs 用户已经改过的工作区 | **项目版本压过本地编辑**：项目里已登记的 shader 就是每个 rdc 打开时统一替换进去的版本。工作区源码若与项目知识不同，先把本地源码原样备份到 `<工作区>/ai/local-edits/<时间>-<sha8>.hlsl`，再用项目源码播种、编译、替换；恢复记录的该项带 `supersededLocalEdit`（备份路径），控制台摘要注明"其中 N 个替换了你的本地编辑"。想让某次本地改动成为项目版本：在编辑器里保存并登记（Ctrl+S → 整理/登记），它就成了新的知识头。 |
| 恢复 vs 恢复（自动路径与项目窗口同时触发） | 内部服务按（项目, rdc）去重，只跑一个任务；第二个请求拿到同一个 job。 |
| AI 别名 vs 用户别名 | 同一成员：`user` 覆盖 `ai`；AI 重命名时先读现有文件，只填空位。 |
| AI 整理 vs 用户正在编辑 | **没有独占锁**。编辑器、AI 整理、项目恢复、工具链刷新都可以直接写 `source/shader.hlsl`；谁写晚了以哈希比对（读到的版本 ≠ 磁盘当前版本）被拒绝并提示重新读取，而不是被一把锁挡在门外。`.workspace-watch.lock` 只剩下 `watch/participants/` 里的"谁在参与"记录；旧版遗留的独占锁文件会被移到 `watch/retired-exclusive-lease-*` 里，不再生效。监听器发现别人已经用同一份源码编译过（manifest 的 latestCompiled 命中）就直接采用（`compileAdopted`），不重复编译。 |
| 客户端超时 vs 后台仍在跑 | 轮询超时只重试直到总期限；不再把一次慢响应当作恢复失败。 |

## 5. 审核尺度（放宽到"物理上说得通"）

* ABI 门禁：布局不完整（stripped）的 block，候选的读取集合是原始读取集合的**子集**即兼容；新增偏移或改类型仍拒绝。
* 历史拒绝记录：接口与物理证据逐字节一致即可接受，即使当前规则会放行；旧裁决保留到下一次保存重新过门禁。诊断记录损坏只让 `lastAbiAttempt` 显示"不可读"，不再让整个工作区 status 失败。
* 捕获身份检查（后台工具）：只核对会话/路径/API，不冻结 UI 当前事件；EID 一律来自工具参数（即提问时冻结的快照）。
