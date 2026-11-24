# 完整依赖与再分发核对清单

本清单针对 Windows x64 完整发行配置。项目所有者已确认具有再分发授权，
因此保留完整功能依赖；只排除 LibreOffice 和 Chromium 浏览器本体。
这不包括任何用户的登录凭据、连接配置、聊天、项目或捕获文件。

## 优先核对的专用组件

下表的许可证名称来自随附元数据，不代表重新授予许可证。
公开源码仓库（含封存分卷）和最终程序包都属于需要覆盖的分发方式。
原始通知、对应源码要求及服务使用条件应与二进制一起保留。

| 组件 | 快照版本 | 元数据声明 | 需要核对的具体内容 |
| --- | --- | --- | --- |
| template-creator | 26.1026.10807 | Proprietary | 插件、技能及附属资源的公开仓库和程序包分发范围；快照未发现独立许可证文件 |
| visualize | 1.0.66 | Proprietary | 同上；交互输出依赖的服务与通用适配器能力分别列明 |
| codex-app-tools | 26.1027.30824 | Proprietary | 客户端分发范围；连接需要当前提供商实际公布的 `CODEX_APP_TOOLS_PIPE_PATH` 服务 |
| browser | 26.1027.30824 | Proprietary | 保留客户端和技能；不包含 Chromium；客户端不等于可独立使用的浏览器服务 |
| computer-use | 26.1027.30824 | Proprietary | 桌面控制客户端、技能、协议及附属运行库的分发范围 |
| unified-computer-use | 26.1027.30824 | see-original-notices | 顶层元数据没有明确 SPDX 许可证；需要保留并核对原始通知 |
| `@oai/artifact-tool` | 2.8.106 | package.json 未声明 | 核对归档内 `LICENSE.md` 及其包含的 SDK、原生库、字体等通知；不能按外层文档插件的 MIT 推断 |
| `@oai/walnut` | 0.1.269 | package.json 未声明 | Node 和 Python artifact 工具内部各有副本，均须覆盖 |
| `@oai/browser-desktop` | 0.1.1 | package.json 未声明 | 保留客户端；核对服务、浏览器接入与再分发条件 |
| `@oai/cua` / `@oai/cua-repl` / `@oai/sky` | 0.2.6 / 0.1.2 / 0.7.13 | package.json 未声明 | 核对私有客户端与依赖通知；CUA 服务通过当前连接的 `SKY_CUA_NATIVE_PIPE_DIRECTORY` 接入 |
| UEX 原生解码库 | OodleUE 2.9.16、zlib-ng2 1.0.0、Detex | 随各自上游条款 | 已从现有已授权工具缓存保留，需一起核对原生库的通知、来源及分发范围；实际哈希在 `uex-native` 输入中 |

“未声明”只表示对应 package.json 中缺少该字段，不表示没有许可证或禁止使用。
完整包继续保留这些文件。登录一个 AI 连接不会自动创建该连接没有提供的专用服务；
Bread 通用 MCP 适配器与提供商专用服务应分别进行健康检查。

## 其余需要一起保留的功能

| 功能 | 依赖输入 | 核对项 |
| --- | --- | --- |
| 文档、PDF、表格、演示文稿 | documents / pdf / spreadsheets / presentations 26.1026.10807，元数据 MIT | 保留技能、脚本、SDK 和各自的原始通知；文档 SDK 不需要 Office 本体 |
| Python 文档环境 | CPython、python-docx、openpyxl、python-pptx、pypdf、Pillow、artifact_tool_v2 等 | 每个发行包的版本、许可证与原生扩展；不把本机 venv 当作唯一来源 |
| Node 文档与图像环境 | Node、docx、pptxgenjs、pdf-lib、pdfjs-dist、sharp、tesseract.js、Playwright SDK 等 | 保留 SDK、平台 DLL 和传递依赖；Playwright SDK 与 Chromium 本体是不同输入 |
| 桌面与辅助命令 | PowerShell、Git、Poppler、libheif、jxrlib 等 | 可再分发运行库、许可证和运行所需 DLL 一起保留 |
| 发行技能 | Bread 内置技能在 `.agents/skills` 和 `replaylab/portable_skills`；custom-skills 胶囊保留已批准的兼容路由及供应商系统技能 | 保留正式技能的参考文件、脚本及依赖；本机个人技能与用户覆盖副本留在仓库外 |
| Shader 工具 | DXC、FXC、glslang、SPIRV-Tools、SPIRV-Cross、RenoDX decomp、DXIL-SPIRV、3Dmigoto HLSL decompiler | 编译器和相邻 DLL、固定版本、文件哈希、完整许可证；GPL 组件还需对应源码及构建说明 |
| Shader 语言服务 | Slang 2026.20 | 归档、选用的二进制、原始通知以及 `provider.json` 中的哈希 |
| Unity 接入 | Coplay Unity MCP v10.3.0，提交 aa5fc638d623f56178d50329d9ad8c541a57fe66；Bread 两个 Unity 包 | 上游源码、锁文件、Python 服务运行环境、Editor 包均应可恢复；不包含用户 Unity 工程 |
| Unreal / Unity 解包 | UEX 源码提交 08f15ba67a55b8c46f482295e8de16448adb36df 及本仓库补丁、CUE4Parse、AssetStudioModCLI v0.19.0、umodel、UnityPy | 固定来源与哈希；UEX 对应源码、原生解码库通知、NuGet 依赖；发布时自带 .NET 运行库 |
| GUI 构建 SDK | 仓库内 Qt、Python 3.6、PySide2、SWIG、Detours 等 | 实际参与编译的头文件、LIB、DLL、PYD 都是依赖输入，不能被通用二进制忽略规则排除 |
| AI Host 构建 | `build-requirements.lock` 对应的 Python 包与构建工具 | 与两个 pyproject 的约束一致；PyInstaller、hatchling 等构建依赖也要可恢复 |

## 依赖输入与产物边界

`replaylab/dependencies` 保存锁文件、清单和封存的第三方依赖输入。
封存归档以每个不超过 48 MiB 的普通 Git 二进制分卷保存在 `archives/`，随源码
一起 clone；这些是构建输入，不是 RenderDocBread 用户发布包，不上传到 Release。
锁文件记录各分卷的路径、顺序、大小与 SHA-256，并保留完整 ZIP 和内部文件清单。
首次 clone 后执行 `./replaylab/packaging/Restore-BuildEnvironment.ps1`，自动离线还原
缺少的 ZIP 并校验全部内容，再恢复构建环境。不需要 Git LFS 或单独下载依赖。
只还原归档并验证可运行：

```powershell
python replaylab/packaging/restore_dependencies.py --verify-only
```

还原后的 ZIP 在本地忽略，源码分卷保持只读。分卷缺失、损坏或校验不符会停止，
不会使用或覆盖已有的有效输入。更新封存依赖时先更新归档及其锁定文件清单，再运行
`python replaylab/packaging/seal_dependency_parts.py`，把锁文件和新增分卷一并提交。
源码自带依赖不包含开发机基础工具链：仍需安装下文列出的 VS、Windows SDK、
64 位 CPython 3.12 和 .NET SDK。

`3dmigoto-source.zip` 包含与现有工具来源记录对应的提交
`cbc4993ca67b6d639e06c578d4e3dc8b3780243f`，其中含原始构建说明。
打包时将该源码归档与现有 GPL/COPYING 通知一起保留在程序包的 `licenses` 下。

以下仍是可重建产物：`.venv`、展开的依赖、`node_modules` 工作目录、下载缓存、
`bin/obj`、PyInstaller 工作目录、`x64`、`output`、`dist` 和最终发布 ZIP。
它们不应成为不可替代的输入，打包脚本应从封存输入恢复私有构建目录。

构建机器仍需安装 Visual Studio C++ 工具链、Windows SDK、.NET 10 SDK 和
CPython 3.12。这些开发工具不应作为用户应用运行时安装进程序包。
模型登录、提供商服务、Unity Editor、Blender 和 GPU 驱动也不属于随包账号或工程数据。

## 本轮审查发现的缺口

- 原完整技能快照仅在被忽略的 `replaylab/output` 中；现已转为封存依赖输入。
- 原打包入口要求已有 `.venv`、Development 程序和导入库；现改为恢复独立环境并构建项目引用及两个 PYD。
- 原 UEX 获取路径使用最新源码，部分 Shader 工具只有本机副本；现已封存固定源码、工具及本地 NuGet feed。
- Python 项目原来只有版本区间；现已补充精确版本、哈希和离线 wheels。
- UEX 的 3 个原生解码库原来会被缓存过滤误删；现作为明确的封存输入保留，并补上 UEX 自己的 VC 运行库和原生加载检查。
- 所有新增技能、Unity 包、补丁和打包脚本必须一起提交；文件在本机可见不等于已提交。

额外发现：UEX 上游固定的 ImageSharp 3.1.12 在实际 NuGet 恢复时报告了多项
NU1902/NU1903。已核对其中的 [TIFF 解码问题](https://github.com/advisories/GHSA-v76p-62qx-wwq2)
及 [HistogramEqualization 问题](https://github.com/advisories/GHSA-j3p4-wp97-rph4)，
公告均列出 3.2.0 修复版本。当前封存输入保留原上游版本，不能将“离线恢复成功”
报告为“没有已知漏洞”；升级应连同 UEX 图像导出回归一起单独验收。

机器清单记录每个组件和归档的版本、SHA-256、许可证位置与待核对项。
本轮独立本地克隆已通过恢复、编译、组包、重定位及最终 ZIP 核对，
详见 [验证记录及范围](VALIDATION.md)。这不代表远端仓库已经提交了这些新增内容。

原生 GUI 仍沿用 Qt 5.9.4、CPython 3.6.4、PySide2 2.0.0~alpha0、OpenSSL 1.1.1l
等旧 SDK，已逐文件记录 75 个 SDK/通知输入的哈希。它们与 AI Host 的 CPython 3.12
环境分开；本轮没有进行原生 ABI 升级，应另行安排兼容迁移。
