# DF + Bread 工具使用说明书

本手册对应本分支集成的 RenderDocBread `1.46.142`。架构见[工程架构](Bread_DF_工程架构.md)，初次集成记录见[历史验证报告](Bread_DF_验证报告_2026-11-16.md)，本次功能说明见[圈选分析与网格查看器](Bread_DF_圈选分析与网格查看器_2026-11-24.md)。

## 1. 启动与打开抓帧

运行仓库根目录的 `run_bread.ps1`，或直接启动 `x64\BreadDevelopment\qTinecmaTool.exe`。界面仍使用 qTinecmaTool 配置目录。

```powershell
.\run_bread.ps1
.\run_bread.ps1 -Capture 'C:\Users\Administrator\Desktop\DF-Wen4.rdc'
```

首次启动若出现设置向导，完成后从文件菜单打开 RDC。确认事件列表、Graphics API 和抓帧路径正确，再选定 DrawCall。不同窗口同时打开时，外部自动化应指定实例 ID。

原 `x64\Development` 可继续用于旧版本；本次新增版本放在 `BreadDevelopment`。要切换版本，关闭相应窗口再启动目标程序。不要在程序使用 DLL 时覆盖它。

## 2. 内嵌 AI

打开主菜单 **Bread AI**，在连接设置中新建 profile。模型 API 连接使用 DeepAgents；Codex 等订阅连接使用提供方原生 Agent 协议。选择连接后执行诊断，再进行对话。

- API 模式：填写端点、模型和凭据；凭据在本机保存，不要写进仓库或 Shader 注释。
- 订阅模式：在该 profile 中执行登录。Bread 会隔离自己的 provider 配置目录，本机其他 Codex 应用已登录不代表此 profile 自动登录。
- 诊断中“工具可列出”和“模型能真实往返”是不同检查；完整检查使用 `doctor --execution-test`。

```powershell
.\x64\BreadDevelopment\replaylab\replaylab-ai.exe profile list
.\x64\BreadDevelopment\replaylab\replaylab-ai.exe doctor --profile '<连接ID>' --execution-test --json
```

建议首个任务：选定一个 DrawCall，询问“解释当前事件的输入、输出和 Shader 作用，并引用实际资源与 EID”。修改任务则明确需要的效果和范围，例如“只在当前事件把这个颜色常量改成蓝色，保留回滚入口”。涉及第三方编辑器时先连接对应 MCP 工具。

## 3. 工程管理与资源关联

在 Bread 的工程界面新建工程，关联 RDC 和可用的解包资源目录。使用短、稳定的本地路径，例如 `C:\BreadProjects\DF`，给 Shader 编译任务预留路径长度。

工程保存抓帧目录、Shader 工作区、符号/材质线索和资源导出记录。先完成目录扫描或建立当前抓帧的符号索引，再做资源匹配。捕获字节码、ABI 与资源使用证据更可靠；同名文件不能自动视为同一 Shader。

更换抓帧后重新核对 EID 和资源身份。旧工作区只有通过当前抓帧校验才可恢复应用，不要手动修改清单中的哈希来强行复用。

## 4. Shader 阅读、整理与热替换

1. 在事件列表选 DrawCall，在流水线面板打开相应阶段的 Shader。
2. 使用 Shader Workspace 准备工作区。检查当前入口、阶段、原始 ResourceId，以及得到的是可编辑源码还是仅用于解释的表示。
3. 可执行函数/变量整理、注释和调用关系分析；这些工作保留结构证据，不能把未经验证的重建当成原始源码。
4. 编辑并保存源码，编译候选；查看编译器诊断。
5. 执行 ABI 验证，成功后应用到当前事件或明确选择的共享资源范围。
6. 在同一事件、同一输出附件观察结果；需要时用原图/修改图比较，并执行回滚。

`compile` 不会自动证明可替换；ABI 通过也不证明视觉正确。入口不匹配、常量布局变化、资源绑定变化会使验证拒绝候选。遇到错误先读编译日志与回执，保留原始基线，不要放宽校验条件。

可视化变量调试用于把 Shader 中的值映射成输出预览；预览拥有独立生命周期。结束预览应恢复此前替换状态，避免把调试输出误当最终修复。

## 5. CBuffer 实时编辑与别名

从流水线 Shader 阶段的常量缓冲区入口打开参数面板。先选择实际绑定的块，再修改可编辑字段。修改受反射类型、分量数量、字节范围和版本约束。

事件级修改只作用于目标事件。用别的事件截图可能看不到变化，因此比较时必须固定同一个 EID。重置/回滚会恢复原始值；显示名称与别名只影响阅读，不改变真实寄存器或字段布局。

本次示例把 `Tint=(1,0.8,0.4,1)` 改为 `(0.1,0.3,1,1)`，保存修改图后回滚；基线和恢复图逐字节相同。

## 6. 帧流程、纹理与标注

帧流程功能从当前抓帧的 Draw/Dispatch、资源读写与事件顺序构建输入，再组织阶段图。点击阶段或资源回到实际 EID 检查。图上的用途解释须由真实事件和资源支持，不能仅按常见渲染管线名称猜测。

纹理查看时确认 ResourceId、mip、slice、sample 和通道。比较两次结果时固定显示范围，防止自动曝光掩盖差异。RT 标注可在图像上做圈选、定位和说明；Mesh/UV 视图可对应检查顶点、三角形与 UV。保留显示范围和附件标识，便于复验。

圈选入口位于 **Bread AI 输入框左侧的画笔按钮**：先选中目标 RT，再画圈/箭头，添加到对话后输入问题。网格查看器顶部 **UV** 按钮开启 3D/UV 并排视图；点、边、面按钮切换选择模式，纹理下拉框与 RGBA 菜单控制贴图显示。完整操作和示例问题见[专项说明](Bread_DF_圈选分析与网格查看器_2026-11-24.md)。

## 7. 导出到 Unity

选择要导出的 DrawCall，使用资源导出功能或导出工作流。输出可包含 Mesh、纹理、Shader/常量信息、材质描述和恢复清单。只导出当前事件并不能恢复整个游戏场景。

OBJ/FBX 可以直接导入 Unity 的 Assets。要自动建立工程或操作编辑器，应安装/启用相应 Unity 插件并连接随应用提供的 Unity MCP。仅有 MCP 服务可执行文件不等于编辑器已建立连接。

随源码提供 `replaylab/unity/com.bread.renderdoc`；按该包说明安装和使用导入器。传统单 DrawCall 导出还会生成 `*_UnityImport.cs`，放入 `Assets/Editor` 后使用其 RenderDoc 导入菜单。复杂材质需要针对源游戏着色逻辑恢复，不能把自动映射材质当成逐像素复刻。

本次验证已由 Unity 2022.3.62f3 实际导入测试 OBJ/FBX，并核对顶点、索引、法线及 UV。

## 8. 解包资源与外部工具

应用 `replaylab/tools/unpackers` 提供资源读取工具，UEX 为自包含版本。通过工程/AI 工具设置选择资源目录和相应读取器。必须提供实际可读取的资源文件；工具不能从截图生成未提供的原始资产。

```powershell
.\x64\BreadDevelopment\replaylab\tools\unpackers\uex\uex.exe --help
```

标准能力包包含作者提供的工具/技能组合；Office 与浏览器运行时按作者标准发行配置不在此包内。外部账号、编辑器插件、资源密钥等仍需按具体工具配置。

## 9. 从源码完整构建

需要 Windows x64、Visual Studio 2022 C++ 工具、有效 Python 3.12 可执行文件和 .NET SDK 10.0.302。构建脚本从已封存输入恢复其余锁定依赖，不使用系统 `python3` 的商店占位程序。

```powershell
.\build_bread.ps1 `
  -PythonExecutable 'C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe' `
  -DotNetExecutable 'C:\Users\Administrator\AppData\Local\BreadBuild\dotnet-10.0.302\dotnet.exe'
```

这些是本机当前路径，换机器请传入实际路径。可用 `-ApplicationDirectory` 指定独立输出。`-SkipNativeBuild` / `-SkipRuntimeBuild` 仅用于已有对应产物的增量部署；常规首次构建不要跳过。

脚本会拒绝覆盖正在使用的输出目录。它不会重装共享 `renderdoc_mcp`，不会提交或推送代码，也不会生成上游发布 ZIP。

## 10. 验证与排错

- 桥接连接失败：确认目标 GUI 已启动，查看内置实例目录及 `instance.json` 心跳；全局 RenderDoc MCP 和 Bread 内置 IPC 分开诊断。
- AI 无法调用：先 `profile list`，再运行对应 profile 的 doctor；未登录/未配密钥属于连接未就绪。
- Shader 编译失败：查看工作区 `jobs` 下的 stderr/stdout；确认完整工具目录和目标 profile，保留入口名。
- ABI 拒绝：核对原始抓帧、阶段、入口、布局和候选版本；不要手改校验哈希。
- 修改画面没变化：确认观察的 EID、附件及覆盖作用范围；使用事件绑定截图接口。
- Windows 路径过长：工程和验证临时目录使用短路径。
- 构建缺 DLL：使用完整 `build_bread.ps1`，不要只复制 GUI exe。

真实验证脚本位于 `integrations/renderdoc-bread/verification`。`live_smoke.py` 必须传入明确的 `--instance`、`--capture` 和 `--output`；Shader 验证脚本只用于配套的测试三角形 EID 11，不应套用到用户任意抓帧。它们会修改自己的测试回放并在 finally 中回滚。

复验本次已经生成的三角形时，先在一个专用窗口中打开 `replaylab/output/df-verification-20261116/triangle_capture.rdc`，从 `%LOCALAPPDATA%\ReplayLab\ipc-v2\instances` 中选择该窗口对应且心跳仍活动的实例 ID，再执行：

```powershell
$breadPython = '.\replaylab\output\build-inputs\environment\Scripts\python.exe'
$breadInstance = '<专用测试窗口的实例ID>'
$breadOutput = Join-Path $env:TEMP ('BreadVerify-' + [Guid]::NewGuid().ToString('N'))
& $breadPython .\integrations\renderdoc-bread\verification\live_smoke.py `
  --instance $breadInstance `
  --capture .\replaylab\output\df-verification-20261116\triangle_capture.rdc `
  --output "$breadOutput\live"
& $breadPython .\integrations\renderdoc-bread\verification\shader_smoke.py `
  --instance $breadInstance --app .\x64\BreadDevelopment --output "$breadOutput\shader"
& $breadPython .\integrations\renderdoc-bread\verification\frozen_mcp_smoke.py `
  --instance $breadInstance --host .\x64\BreadDevelopment\replaylab\BreadMCP.exe `
  --output "$breadOutput\mcp.json"
```

每一步检查退出码，再继续下一步；成功输出为 `LIVE_SMOKE_PASS`、`SHADER_SMOKE_PASS` 和 MCP 回执的 `status: passed`。自动生成的抓帧未纳入源码；换机器需要在 VS x64 开发者终端编译 `capture_triangle.cpp`（链接 `d3d11.lib`、`d3dcompiler.lib`、`dxguid.lib`），运行时传入本机构建的 `TinecmaTool.dll` 绝对路径和抓帧文件名前缀。测试脚本与 C++ fixture 共同定义测试内容，不用于任意游戏帧。
