# DF 授权捕获研究线

## 目标与边界

`DF研究` 分支把 TinecmaTool 固化为可重复构建、可校验、可回滚的稳定研究线。研究对象只能是团队自有程序、最小复现程序，或已经取得书面授权的测试环境。

本分支不实现也不验证以下能力：

- 绕过、关闭、欺骗或规避 ACE 等反作弊/安全产品；
- 向受保护的第三方进程注入或隐藏调试组件；
- 修改驱动、启动链、签名、进程句柄或遥测以逃避检测；
- 对在线服务或其他用户产生影响的攻防测试。

如需研究兼容性，应由软件/反作弊厂商提供许可、测试账号、隔离服务器与允许的测试方法。没有这些前提时，只记录“受保护环境不支持”，不继续尝试规避。

## 稳定线门禁

每个可部署工件应满足：

1. 从固定 Git 提交构建 `Release|x64` 和需要时的 `Release|Win32`。
2. 核心与 UI 单元测试通过。
3. 包内文件清单和 SHA-256 可复验。
4. 在自有图形程序上通过“启动式捕获”生成并重新打开 `.rdc`。
5. 测试记录包含提交、工具路径、目标路径、时间和 `.rdc` 哈希。
6. 不使用 `inject` 命令，不测试安全产品绕过。

## 构建与测试

在 Visual Studio 2022 Developer PowerShell 中执行：

```powershell
.\clean_and_rebuild.bat
msbuild.exe .\renderdoc.sln /m /p:Configuration=Release /p:Platform=x64
.\util\df-research\Test-BuildIdentity.ps1 -Platform x64 -Configuration Release
.\x64\Release\TinecmaToolcmd.exe test unit -o .\artifacts\core-tests.log
.\x64\Release\qTinecmaTool.exe --unittest log=.\artifacts\ui-tests.log
```

开发期可把 `Release` 改为 `Development`。清理脚本会移除历史复数品牌和上游默认名残留；身份检查会拒绝混合产物，并核对 Replay Marker、Shim 接口、公共 API 和 Vulkan manifest。CI 的 Windows 测试入口已经使用品牌化后的 `TinecmaToolcmd.exe` 和 `qTinecmaTool.exe`。

## 打包

```powershell
.\util\df-research\New-StablePackage.ps1 -Platform x64 -Configuration Release
```

脚本输出 ZIP、同名 `.sha256` 和包内 `manifest.json`。工件名包含版本、平台和 Git 提交短哈希。仅验证开发构建时可显式传入 `-Configuration Development`，但该工件不应对外发布。

## 在授权目标上生成 `.rdc`

```powershell
.\util\df-research\Invoke-AuthorizedCapture.ps1 `
  -TargetExecutable C:\Lab\OwnedSample\Sample.exe `
  -ToolDirectory .\x64\Release `
  -AcknowledgeAuthorization
```

目标运行并渲染时按捕获热键（通常为 F12 或 Print Screen），随后正常退出。脚本只使用 `TinecmaToolcmd capture` 启动目标，不附加到已运行进程；成功后会在 `captures\authorized` 写入 `.rdc` 和 JSON 回执。

如果目标由自有启动器创建，并且授权范围覆盖所有子进程，可增加 `-CaptureChildProcesses`。不要把这个选项用于第三方受保护启动器。

## RenderDocMCP2 分析适配

本机使用 `Hengle/RenderDocMCP2` 1.5.0（提交 `4e8581b3674723ce8306fbad52f2b140fdc7b7c9`）分析已有 `.rdc`。为适配 Codex 与本项目 v1.44，需要固定以下兼容条件：

- 在独立 Python 3.12 虚拟环境安装，依赖固定为 `mcp[cli]>=1.0.0,<2`；上游代码使用 MCP SDK 1.x 的 `FastMCP` API，直接安装当前 2.x 会导入失败。
- 扩展安装到 `%APPDATA%\qTinecmaTool\extensions\renderdoc_mcp`，并在 UI 配置的 `AlwaysLoad_Extensions` 中加入 `renderdoc_mcp`。
- v1.44 的加载调用为 `LoadCapture(path, rd.ReplayOptions(), path, False, True)`；旧版字符串参数顺序不兼容。
- `qTinecmaTool` 启动时会移除继承到 UI 自身的 `ENABLE_VULKAN_TINECMATOOL_CAPTURE`、历史复数品牌变量和上游变量；捕获子进程所需环境仍由正式启动路径单独设置。外部脚本也应避免给分析 UI 注入这些变量，以免旧版 UI 发生运行时混载。
- MCP 的文件 IPC 目录是 `%TEMP%\renderdoc_mcp_ipc`，同一用户会话只运行一个 MCP Server 实例。配置变更后重启 Codex，避免热重载期间多个服务竞争同一组文件。

验证顺序：`ping` → `open_capture` → `get_capture_status` → `get_draw_calls`。当前验证样例为 Vulkan 帧，已成功加载并返回 17 个 action。

## 攻防演练替代方案

需要验证防御边界时，搭建一个自有的 D3D11/D3D12/Vulkan 测试程序和一个只记录事件的模拟保护层，覆盖以下可审计场景：

- 启动式捕获成功与失败；
- 子进程捕获开关；
- 位数不匹配与缺失依赖；
- 超时、崩溃和无捕获帧；
- `.rdc` 生成、哈希、回放和清理。

这种夹具可以验证工具的错误处理和部署质量，同时不触碰真实反作弊系统。
