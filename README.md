# TinecmaTool

TinecmaTool 是基于 RenderDoc 的图形捕获与回放研究分支，用于自有程序、最小复现程序，或已经取得明确授权的测试环境。

本仓库不以规避反作弊、安全产品、签名策略或进程保护为目标，也不把第三方受保护程序的兼容性作为发布承诺。涉及授权边界、稳定构建和可审计测试的要求，以 [授权捕获研究线](docs/DF_AUTHORIZED_CAPTURE_RESEARCH.md) 为准。

## Windows 构建

使用 Visual Studio 2022 Developer PowerShell：

```powershell
msbuild.exe .\renderdoc.sln /m /p:Configuration=Release /p:Platform=x64
.\util\df-research\Test-BuildIdentity.ps1 -Platform x64 -Configuration Release
```

若需要支持 32 位自有目标，再单独构建：

```powershell
msbuild.exe .\renderdoc.sln /m /p:Configuration=Release /p:Platform=Win32
.\util\df-research\Test-BuildIdentity.ps1 -Platform Win32 -Configuration Release
```

主要输出：

```text
x64\Release\qTinecmaTool.exe
x64\Release\TinecmaToolcmd.exe
x64\Release\TinecmaTool.dll
x64\Release\TinecmaToolshim64.dll
x64\Release\TinecmaTool.json
```

## 干净构建

历史版本曾使用复数名 `TinecmaTools*`。不要把不同版本的 DLL、EXE 或 Vulkan manifest 放在同一输出目录。

在重新构建前运行：

```powershell
.\clean_and_rebuild.bat
```

该脚本只清理仓库内已知构建输出目录，并移除旧的复数名和上游默认名产物。随后在 Visual Studio 中执行 Clean/Rebuild。

`Test-BuildIdentity.ps1` 会检查：

- Replay Marker 的导出与检测名称一致；
- Shim 使用核心 DLL 实际导出的捕获文件接口；
- 标准 `RENDERDOC_GetAPI` 和旧品牌兼容入口同时存在于源码；
- 输出目录不存在旧的复数名或默认名核心产物；
- Vulkan manifest 指向当前 `TinecmaTool.dll`。

## 授权目标捕获

推荐使用可记录提交、工具路径、目标路径和捕获哈希的脚本：

```powershell
.\util\df-research\Invoke-AuthorizedCapture.ps1 `
  -TargetExecutable C:\Lab\OwnedSample\Sample.exe `
  -ToolDirectory .\x64\Release `
  -AcknowledgeAuthorization
```

如果自有启动器会创建子进程，并且授权范围覆盖整个启动链，可使用 `-CaptureChildProcesses`。不要把该选项用于未授权的第三方受保护启动器。

## 打包

```powershell
.\util\df-research\New-StablePackage.ps1 -Platform x64 -Configuration Release
```

打包器会拒绝包含 `TinecmaTools*` 或上游默认核心二进制的混合输出目录，并生成带提交号、文件清单和 SHA-256 的包。

## API 兼容性

Windows 产品文件名使用 `TinecmaTool*`，Replay Marker 也使用品牌化名称。公共进程内 API 保留标准 `RENDERDOC_GetAPI`，同时导出 `TINECMATOOL_GetAPI` 作为旧品牌客户端的兼容别名。

CMake 平台继续使用上游 `renderdoc` 文件名，以避免破坏 Linux/macOS 发行包、插件和 Python 模块路径；Windows 品牌构建以 `renderdoc.sln` 为准。

## 相关文档

- [授权捕获、构建与验证](docs/DF_AUTHORIZED_CAPTURE_RESEARCH.md)
- [品牌与产物命名](BRANDING_TINECMATOOL.md)
- [编译故障排除](docs/BUILD_TROUBLESHOOTING.md)
- [RenderDoc 官方文档](https://renderdoc.org/docs/)

根目录中的早期 `QUICK_START.md`、修改指南和示例脚本属于历史研究材料，可能引用旧的复数名产物。它们不应作为当前构建或部署说明。
