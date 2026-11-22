# RenderDocBread 打包与交付规范

本文件保存项目的长期打包要求，供 AI 和维护者在每次打包前读取。
根目录 `AGENTS.md`、`CLAUDE.md` 和打包操作说明均链接到这里。
当前用户明确提出的要求优先；没有新的变更要求时，沿用本规范，
无需让用户重新转述其他对话中的打包决定。

只有用户明确授权本次打包，才开始构建发行包。开发完成、验收通过和更新本机
Development 应用均不构成打包或发布授权。

构建入口及参数见 [打包操作说明](README.md)，
完整依赖、来源和再分发清单见 [依赖说明](../dependencies/README.md)，
实际文件清单以 [发布配置](RenderDocBread.package.json)、依赖锁文件及封存清单为准。
修改打包方式时同步更新这些说明，不能只记录在某个聊天或本机临时目录中。

## 1. 默认发行内容

- 默认交付完整的 `standard` 配置。保留当前配置声明的插件、技能、引用资源、
  脚本、SDK、工具及其运行依赖；不能因包体、空间不足或某个依赖恢复失败，
  自动改成交付 `core`、`rdc` 或只含少量 Bread 技能的版本。
- 沿用已确认的范围：保留文档、PDF、表格、演示文稿 SDK、Python/Node、
  桌面适配器和提供商客户端、Shader 工具及语言服务、解包工具、Unity 接入。
  标准版排除 LibreOffice/Office 本体及内置 Chromium 浏览器本体；
  相应文档 SDK、浏览器客户端和 Playwright SDK 仍按清单保留。
  更小或不同的配置仅在用户明确选择时使用。
- 项目所有者已确认完整依赖的再分发授权；按依赖说明保留版本、SHA-256、
  许可证、原始通知，以及适用的对应源码和构建说明。已有决定无需每次重新询问。
  发现新的、未覆盖的依赖时，准确记录其来源和缺口，不能静默删除现有功能。
- 技能清单须覆盖 `.agents/skills`、`replaylab/portable_skills` 和选定能力胶囊声明的
  技能及依赖。按当前清单和引用文件核对，不把历史上的“5 个”或“23 个”当作永久数量。
  区分可用工作流、兼容副本和依赖包内的 `SKILL.md`，原始文件总数不能代替功能完整性检查。
- 公开发行从发行提交的干净检出构建。仓库内正式技能随应用收录，个人技能和用户覆盖副本
  存放在仓库外的本机配置中。公开能力快照只允许已批准的兼容与供应商系统技能，
  并核对内层归档的实际成员。新增正式技能无需修改桌面技能清单。
- 提供商客户端与提供商实际提供的服务分别核对。保留客户端不代表私有服务已连接，
  也不代表用户无需登录。外部 Unity Editor、Blender、GPU 驱动、游戏及用户账号
  仍是外部条件，不冒充已经随包交付。

## 2. 构建输入必须可恢复

- 完整封存输入随源码保存：`replaylab/dependencies/archives` 中的普通 Git 二进制
  分卷每个不超过 48 MiB；锁文件记录顺序、大小、分卷和完整归档的 SHA-256。
  普通 clone 后运行 `Restore-BuildEnvironment.ps1`，离线还原缺少的 ZIP 并验证精确
  文件清单；不依赖 Git LFS 或 Release 下载。不要漏提交分卷，也不要把本机缓存当作
  唯一来源。更新输入后运行 `seal_dependency_parts.py` 并一起提交清单和新分卷。
- 源码、技能及引用文件、插件、工具获取/校验脚本、补丁、必要 SDK 输入、锁文件和
  已授权的封存依赖应能纳入 Git。缓存、展开的依赖、虚拟环境、编译中间文件、
  发布产物继续忽略。不能把被忽略的 `output`、旧 Development 安装、个人 Codex
  插件目录或机器上的 `.venv` 作为唯一的构建来源。
- `Restore-BuildEnvironment.ps1` 从封存输入恢复独立构建环境；默认主入口会调用它。
  缺少输入时先定位并修复恢复路径，不通过删除功能、放宽哈希检查或依赖本机账户来掩盖。
- Visual Studio C++、Windows SDK、64 位 CPython 3.12、.NET SDK 等构建机前提
  按当前操作说明准备。发布包应自带清单要求的应用运行库；UEX 使用
  `win-x64 --self-contained true`，Unity MCP 使用随包的私有 Python 环境。
- 只有在独立干净克隆实际通过恢复、编译、打包和重定位验证后，才能报告
  “干净克隆可完整恢复”。本机一次打包通过不足以证明这一点。

## 3. 源码、版本和二进制一致

- 版本采用 `<RenderDoc major>.<RenderDoc minor>.<Bread 扩展修订号>`，例如
  `v1.46.137`。前两段只读取实际已集成源码的 `renderdoc/api/replay/version.h`，
  不依据网上最新 Release、下载文件名或部分功能的 cherry-pick 提前提高版本。
  核心自己的版本/ABI 检查保持上游 major/minor，不用 Bread 修订号替换它。
- `RenderDocBread.package.json` 的 `versioning` 是规则和扩展修订号的唯一配置入口。
  `extensionRevision` 显式递增，升级上游时不重置；重复构建不会自行消耗版本号。
  Windows 版本资源要求各段不超过 65535。发布过的标签和包不能移动或覆盖。
- 修改修订号或集成上游后运行 `python replaylab/packaging/version_policy.py --write`，
  自动同步 `qrenderdoc/Code/BreadVersion.h` 和四处 Python 版本声明。打包入口在恢复依赖
  和编译前执行 `--check`，遇到旧生成文件先停止，不从旧 EXE 猜版本或修改运行程序。
  窗口标题、关于窗口、`--version`、EXE 文件/产品版本读取同一个生成头；
  完整和增量组包校验实际 GUI、冻结 Host 与配置版本一致，并写入包内 `versioning`
  元数据。缺少 GUI 完整三段版本或版本不同均阻止交付。
- 交付须包含本次修复。版本生成器同步 `replaylab/pyproject.toml`、
  `replaylab/mcp_host/pyproject.toml`、`replaylab/src/replaylab_agent/__init__.py`
  和 `replaylab/mcp_host/src/version.py`，并核对冻结 Host 和发布清单中的实际版本。
- Native 修改需构建相关驱动/依赖，并重新链接受影响的 `renderdoc.dll`、
  `system_load.dll`、GUI 和辅助程序。不能只重编 GUI 后携带旧核心；
  `renderdoc_no_export` 的源码清单仍须与核心工程保持同步。
- 构建与当前核心头文件匹配的 `renderdoc.pyd`，放入发布包的 `pymodules`。
  导入成功或时间戳接近都不能单独证明 ABI 一致；记录实际构建输入和工件哈希。
- AI/MCP/Internal/Shader Host、Unity 包及其内嵌技能必须对应当前源码。
  校验冻结运行时的 `sourceInputsSha256` 和逐文件哈希；不通过手改指纹让旧 EXE 冒充新源码。
- 首方 Python bridge 使用仓库 `replaylab/renderdoc_mcp_bridge` 的完整内容，
  发布位置固定为 `integrations/replaylab/python/renderdoc_mcp_bridge`。
  保留兼容用的 ReplayLab 标识；UI 和发行名称使用 RenderDocBread / Bread / Bread AI。
- UI/bridge 有变化时遵守根目录本地化规范，完成翻译同步、完整性检查及 GUI 重链接。
  单独修改本文等构建说明，不要求重建已经交付的二进制包。

## 4. 隔离构建，不覆盖运行程序

- 打包与部署是两个操作。打包时运行中的应用目录是只读输入，不停止其他实例，
  不覆盖已加载的 EXE/DLL/PYD、bridge 或 Host，也不调用部署脚本修改它。
  只有任务需要部署且目标会话已关闭时才进行本地部署；保留项目、聊天、源码及历史。
- Native 构建使用 `/m:2` 和显式 `SolutionDir`，不并发运行多个完整 Native 构建。
  使用 `ReplayOptimizeDevelopment=true` 时记录该设置，性能比较保持一致。
- 主入口默认 Native 输出为 `output/RenderDocBread-native`，组包在系统临时目录完成。
  空间不足时先核对空间，再使用有空间磁盘的独立生成目录。
  `-BuildInputRoot` 只调整恢复的构建输入，`-Destination` 只调整成品位置；
  两者都不会自动移动全部 Native 中间文件。
- 需要手动指定 Native `OutDir`/`IntDir` 时，记录等价构建命令、依赖库来源和
  配套绑定，并执行相同交付门禁。不要把旧目录中恰好存在的库当作来源证明。
  低空间冻结 Host 可用 `Build-ReplayLabRuntime.ps1 -TemporaryBuildRoot`，
  按脚本要求将输出和工作目录放在系统临时目录下的同一个 `bread-runtime-*` 根目录内。
- 不因空间不足擅自删除用户的其他工程、捕获或历史发布包。清理仅针对明确由本次
  构建创建、已核对归属和绝对路径的中间目录。Pytest 使用操作系统临时目录。

## 5. 选择构建与组包路径

获得本次打包授权后，从仓库根目录执行默认完整入口：

```powershell
./replaylab/packaging/Build-RenderDocBread.ps1
```

也可使用 `PACKAGE_RENDERDOCBREAD.cmd`。主入口负责依赖恢复、翻译门禁、
Native/绑定构建、冻结 Host、UEX 自包含发布及最终组包。
具体参数以脚本当前 `param` 定义为准，不假设存在历史版本的开关。

- 新修复默认不带 `-SkipNativeBuild` / `-SkipRuntimeBuild`。
  复用已有工件时，先证明它们对应未变化的源码和依赖，记录复用范围。
  `-AuditOnly` 只完成审核，不代表已经生成可交付 ZIP。
- `build_compact_release.py` 适用于 Native 核心、注入库和外部依赖未变化的更新。
  必须验证基包的 `.sha256`、当前冻结源码指纹和能力清单，重写一个新 ZIP，
  然后执行相同审核及重定位检查。不得使用追加 ZIP 的方式留下已被替代的载荷。
- 修改渲染驱动、核心、注入库、公共 ABI、Native 依赖或发行内容时，使用完整构建/
  审核路径。手工从已验证基包准备只读依赖目录也必须完整替换本次受影响的配套工件，
  不把复用基包误称为干净克隆构建。

## 6. 交付前门禁

以下检查针对实际交付工件；失败时处理具体原因，再检查受影响部分。
不绕过门禁或把未完成检查写成通过。

| 检查 | 必须保留的证据 |
| --- | --- |
| 修复效果 | 与本次修改相关的测试或真实操作结果；渲染问题需观察受影响的显示路径 |
| 翻译和构建 | 所需构建成功；分发目录无未完成翻译；核心、注入库及绑定来源一致 |
| 内容和依赖 | 对照当前发布配置、技能与依赖清单审核；SDK、工具、相邻 DLL、许可证和适用源码齐全 |
| 冻结运行时 | Host 源码指纹及文件哈希匹配；内嵌与外置技能均正确 |
| Shader 工具 | 固定版本和哈希正确；实际编译验证需要的 DXIL/SPIR-V 能力及语言服务完整性 |
| 重定位运行 | 独立目录（含中文路径）、全新用户配置、去掉开发机 PATH/venv 的主程序、Host、UEX、AssetStudio、Unity Python 和可移植工具实际启动检查 |
| 隐私和路径 | 发布清单使用相对路径；无凭据、登录状态、连接配置、聊天、用户项目、RDC、源码工作区、日志和构建缓存 |
| 最终 ZIP | 校验所有清单成员的大小和 SHA-256、精确归档成员集合、无重复/越界路径；确认本次关键二进制的哈希一致；计算最终 ZIP 的 SHA-256 |

原生窗口、后台回读、缩略图和导出是不同路径。比如主视图色彩问题，
仅验证 headless 图片正确不能证明窗口显示已修复，应覆盖最终窗口呈现。
构建机上的重定位启动检查也不等于 Windows 10/11 干净机器或所有 GPU/API 均已验收。
在交付说明中如实区分已经验证的范围与外部条件。

完整入口已执行组包目录审核、编译器能力检查和重定位启动检查；仍需核对最终 ZIP
内的文件与清单及本次构建一致。增量组包的验证入口见
[`verify_compact_release.py`](verify_compact_release.py)，其 `--receipt` 和 `--output`
接收实际发布回执与验证报告路径。检查要覆盖最终交付版本，不能引用另一版的通过记录。

## 7. 命名、校验文件和交付记录

- 可执行文件名为 `RenderDocBread.exe`，ZIP 中仅有一个 `RenderDocBread` 顶层目录。
  标准发行使用与实际版本一致的 `RenderDocBread-<version>-win-x64.zip`。
  其他配置的本地包仍可使用时间戳名称。保留旧版，避免同名覆盖。
- GitHub Release 只上传用户直接使用的完整应用程序包及 SHA-256 校验文件。
  源码构建所需的封存依赖随源码的普通 Git 分卷保存，不另行上传到 Release。
- 默认成品目录为 `dist`；已有对话约定其他交付目录时沿用。
  跨盘移动、复制或重命名后，重新校对目标文件 SHA-256，并同步 `.zip.sha256`
  中的文件名和发布回执中的路径；不能只改 ZIP 名称。
- 保存机器可读回执：实际版本、平台/配置、源码或输入指纹、成品绝对路径、
  字节数、ZIP SHA-256、逐文件清单、关键二进制哈希及已完成检查。
  日志、性能报告、捕获证据和回执留在构建/报告目录，不混入应用程序包。
- 向用户提供成品和校验文件链接，简述修复、相关验证及实际限制。
  明确当前运行实例是否已部署更新，不能把“发布包已生成”说成“正在运行的应用已更新”。
- 仅修改说明文档时，不自动重新打包或改动已经交付的程序包。

### 自动更新发布约定

[打包配置](RenderDocBread.package.json) 的 `autoUpdate` 保存 GitHub 仓库、目标分支、
版本格式、标签和附件命名、安装包配置，以及发布渠道和哈希要求。
标准打包入口使用该配置，从实际冻结 Host 版本生成 ZIP 名称并校验版本格式。

1. 按本规范第 3 节修改 `extensionRevision` 并生成、核对版本文件。从已合并到配置指定分支的干净提交构建完整
   Windows x64 标准包。核对冻结 Host 和包内清单版本一致，包内清单使用
   `renderdocbread-release/v1`，且 `profile` 为 `standard`。
2. 标签为 `v<major>.<minor>.<patch>`，指向实际构建提交。上传一个
   `RenderDocBread-<version>-win-x64.zip` 和同名 `.zip.sha256` 文件。校验文件
   必须包含实际 ZIP 的 SHA-256 和准确的文件名。同一个 Release 只上传一个符合
   Windows x64 更新命名的 ZIP，确保应用能唯一选择安装包。
3. 先创建草稿并上传。使用 GitHub Releases API 核对两个附件的名称、字节数和
   `state: uploaded`，以及 GitHub 计算的 `digest: sha256:<hash>` 与本地一致。
   应用优先使用 ZIP 附件的 GitHub `digest` 元数据验证下载。API 受限时，应用通过
   公开发布页面和同名 `.sha256` 附件确认更新。校验文件须包含一行 `SHA-256  ZIP文件名`，
   其中散列和文件名都与上传的 ZIP 一致。发布正文写明此次更新内容，应用会在弹窗内展示它。
4. 校验通过后将草稿发布为正式版并设为 Latest。复查
   `GET /repos/Straw1997/renderdocBread/releases/latest` 返回本次标签和已核验的 ZIP，
   且 `draft` 与 `prerelease` 均为 `false`。
5. 首次引入更新器的 `v1.13.129` 的说明须注明，`v1.13.128` 及更早版本需要
   手动下载并升级一次。安装带更新器的版本后，后续稳定版本可以在应用内更新。
6. 更新器改动须使用隔离安装验收。覆盖主程序退出后独立助手继续安装、同一安装目录的
   后台 Host 持有运行库、安装中再次启动、替换后重启和暂存清理。检查其他安装实例和
   用户数据仍可用，并验证遇到外部文件占用时能回滚并重启旧版。
   验证主程序退出后独立进度窗口仍可见，显示当前安装阶段和进度，并保留到新版窗口打开。

## 8. GitHub 主分支合并与分支清理

分支及合并操作以根目录
[CLAUDE.md — Branch and release completion](../../CLAUDE.md#branch-and-release-completion)
为规范入口。开发期间保留本地分支和未提交改动。等用户确认后，只执行其明确授权的
提交、推送、创建 PR、合并、删除分支、打包、打标签、上传或发布步骤。用户可以一次
授权多项。打包授权本身只覆盖组包。纯文档更新和草稿 Release 也遵守对应的授权要求。

- 发布目的地为已核实的 `origin` 仓库；本项目是 `Straw1997/renderdocBread`，
  当前默认分支为 `v1.x`。所有仓库级 `gh` 命令显式传入 `--repo`，
  API 路径使用同一仓库，不能因 CLI 默认选择上游而发布到其他仓库。
- 选择版本前核对远程最新提交、标签与 Release。先验证候选包。经授权公开发布前，
  确认主分支已包含实际发布的功能修复、必需检查通过，以及标签、
  冻结输入和工件一致；集成改变构建输入时重建受影响工件。上传后核对大小和哈希。
- 获得删除授权后，先确认默认分支包含任务提交，再清理本次任务的指定分支。
  保留已发布标签、Release、成品、回执、其他任务的分支和已有工作区改动。
- 已发布版本之后的纯规则或说明补充也留在本地等待相应授权。文档补充无需重新打包、
  提升版本、移动标签或覆盖既有成品。交付说明记录实际完成的操作。
