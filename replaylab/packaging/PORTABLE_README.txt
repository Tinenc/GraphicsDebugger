RenderDocBread / Bread AI
=======================
中文
1. 完整解压到本地目录，运行 RenderDocBread.exe。不要直接从压缩包内运行。
2. 目标平台：Windows 10 22H2 / Windows 11，x64。GPU 回放仍需要显卡驱动支持捕获所用的 API。
3. Qt、嵌入式 Python、AI/MCP 服务、Shader 工具、技能、Unity MCP 服务端及编辑器连接包随应用提供。
   UEX 自带 .NET 运行时。AssetStudio 使用系统自带的 .NET Framework 4.8。
4. 首次使用 Bread AI 时添加自己的模型/API/订阅连接。发行包不包含他人的密钥、登录态、
   对话、项目和游戏数据。订阅供应商可能要求登录或安装其官方 CLI；模型 API 不依赖这些 CLI。
5. UI 设置保存在 %LOCALAPPDATA%\RenderDocBread\ui；Bread 项目、连接和运行时数据继续使用
   %LOCALAPPDATA%\ReplayLab，保持已有数据兼容。移动应用目录不会重命名或迁移项目。
6. 内置技能也可在 replaylab\skills 中查看；运行时使用经验证、冻结在 AI 服务中的同版技能。
   Unity 包在 replaylab\unity，Unity Editor 仍需自行安装，再由 Bread 工具配置项目连接。
   Blender、Unity Editor、第三方订阅账号、GPU 驱动及目标游戏不属于此发行包。
7. 应用从自身目录加载 DLL、Python 集成和 AI 服务。请勿把其他 RenderDoc 的 DLL 混入目录。
   通常不需要管理员权限或文件关联；不要为普通回放启用全局钩子。
   Vulkan 捕获如需注册捕获层，使用本程序的诊断/设置按需配置；注册会影响系统，回放无需注册。
8. 遇到问题先检查 ZIP 的 .sha256 校验值，并保留报错和 GPU/Windows 版本。
   不要将个人连接配置或运行缓存放回发行目录后再次压缩分享。
9. 默认包保留 23 个技能（含核心、RDC/UEX、通用文档和桌面功能），全部标记为内置。
   着色器、模型纹理导出、内嵌流程图/文档窗口、解包器及 Unity MCP 仍完整提供。
   仅移除 Office 和捆绑 Chromium；文档 SDK（python-docx、openpyxl 等）、Python/Node 环境、
   桌面适配器、Unity MCP 及其运行时完整保留。浏览器扩展可使用系统浏览器或另加 MCP。
   可在“AI 设置 → MCP 服务器”添加扩展。启用的服务器可由 AI 搜索调用，无需每次手动 @。
10. “检测连接”会消耗少量当前模型额度，验证真实模型调用和 MCP 工具执行回执；
    普通后台配置检查仍不发送提示。检测通过证明当前调用链可用，不代表所有外部服务均在线。

English
1. Extract the entire archive, then run RenderDocBread.exe from the extracted folder.
2. Target: Windows 10 22H2 / Windows 11, x64. Replay requires an appropriate GPU driver/API.
3. Qt, embedded Python, AI/MCP hosts, Shader tools, skills, the Unity MCP server and connection packages travel
   with the application. UEX includes .NET; AssetStudio uses the OS .NET Framework 4.8.
4. Add your own AI connection on first use. No credentials, login state, conversations, projects
   or game data are shipped. Subscription providers may need their official CLI and login.
5. UI settings: %LOCALAPPDATA%\RenderDocBread\ui. Existing Bread data and connections retain
   %LOCALAPPDATA%\ReplayLab for compatibility. Relocating the application does not move projects.
6. Skills: replaylab\skills (the AI host embeds the verified same version). Unity packages:
   replaylab\unity. Unity Editor, Blender, accounts, drivers and games are separate installations.
7. Keep this folder intact. Normal replay needs no administrator rights, global hook or registry
   file association. Vulkan capture layer registration is an explicit system-level operation.
8. Check the accompanying ZIP SHA-256 when troubleshooting. Keep personal settings out of release inputs.
9. The default edition includes 23 built-in skills, document SDKs (python-docx, openpyxl and others),
   Python/Node environments, desktop adapters, Shader/resource tools, unpackers and Unity MCP.
   Only Office and bundled Chromium are omitted. Browser extensions can use an installed
   system browser or an additional MCP server.
   Enabled user MCP servers are searchable without an @ mention.
