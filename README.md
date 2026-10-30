# TinecmaTool (Stealth RenderDoc v1.44)

> 🎮 **面向 DirectX 12 商业游戏与《使命召唤》全系列的高可靠性图形调试与抓帧控制中心**

TinecmaTool 是基于 RenderDoc v1.44 深度重构的隐匿性图形调试系统（Stealth Graphics Debugger）。针对现代商业大型 3D 游戏（如《使命召唤：现代战争 2019》战网版、《使命召唤 HQ》Steam 版等）严苛的反作弊监测与 D3D12/DXGI 设备拆分架构，彻底抛弃了易被拦截崩溃的全局钩子（Global Hook）与外部注入模式，构建了 **自研 DXGI + D3D12 双代理劫持层（Dual-Proxy）**、**PEB 模块链物理摘除**、**免杀安全门控** 以及 **一键式图形控制中心面板**。

---

## 🌟 核心特性与技术优势

### 1. 🛡️ 深度反作弊免杀与隐匿架构 (Anti-Cheat Safe Chain)
* **PEB 模块链物理摘除 (`UnlinkModuleFromPEB`)**：
  代理 DLL 在 `LoadLibraryA` 装载调试核心后，毫秒级遍历进程环境块（`PEB->Ldr`），将 `TinecmaTool.dll` 从三大加载链表（`InLoadOrderModuleList`、`InMemoryOrderModuleList`、`InInitializationOrderModuleList`）中双向抹除，彻底隐蔽调试器模块的存在。
* **屏蔽 TargetControl TCP 监听端口**：
  在 `renderdoc/core/core.cpp` 识别到 COD 系列进程时，跳过打开 `38920+` 调试端口，杜绝反作弊看门狗的本地端口扫描警报。
* **目标差异化免杀门控 (`g_SkipGraphicsDriverHooks`)**：
  针对 COD 系列启动器壳进程（如 `cod.exe`），自动启用 `g_SkipGraphicsDriverHooks = true`，跳过改写壳进程自带完整性校验的导入表，仅在真实子渲染器中挂载；对于本体单体游戏进程（如 `ModernWarfare.exe`），则精准放行 IAT 拦截以确保 `amd_ags_x64.dll!agsDriverExtensionsDX12_CreateDevice` 与 `D3D12CreateDevice` 得到完整包装。
* **Frame 0 黑屏保护机制**：
  在游戏初始化与着色器预编译阶段阻止盲目抓帧，防止过早的 GPU Readback 触发反作弊线程看门狗超时（`0x00001337`）。

### 2. ⚡ 双代理协同劫持 (Dual-Proxy Architecture)
现代 D3D12 游戏通常将设备创建与窗口呈现分离：使用 `d3d12.dll!D3D12CreateDevice` 初始化底层图形硬件，但通过 `dxgi.dll!CreateDXGIFactory` 构建交换链（SwapChain）。
* 传统方案仅代理 `dxgi.dll` 会导致严重错误：`Creating swap chain with non-hooked device`，调试器因未能包装底层设备而拒绝抓帧；
* **TinecmaTool 方案**：在游戏目录同时部署编译好的 `dxgi.dll` 与 `d3d12.dll`，分别拦截设备创建与交换链生成，实现双向设备透明包装与帧捕获无缝协同。

### 3. 🎯 现代游戏多端原生适配
* **《使命召唤：现代战争 (2019)》 (战网 Battle.net: `ModernWarfare.exe`)**：
  - **战网联动唤醒**：控制面板自动探测战网安装路径，设置正确的工作目录，并通过 `battlenet://ODIN` 协议与 `--exec="launch ODIN"` 唤醒战网直接定位至游戏页面，杜绝单独启动时的授权退出；
  - **Win7 遗留库避坑**：自动识别游戏根目录自带的 1.9MB 微软旧版 Win7 运行时（`d3d12.dll`），将其安全重命名备份为 `d3d12_win7.dll.bak`；
  - **AMD AGS DX12 与设备创建全拦截**：精准拦截 `ModernWarfare.exe` 调用的 `amd_ags_x64.dll!agsDriverExtensionsDX12_CreateDevice` 与 `GetProcAddress("D3D12CreateDevice")`，实现底层硬件设备 100% 包装匹配；
  - **DXGI 递归防死锁锁**：DXGI 代理层内置 `thread_local` 递归状态锁，底层核心直接调用系统原生 `GetSystemDXGIModule()`，彻底根除代理与 Hook 之间的回环无限递归爆栈。
* **《使命召唤 HQ》 (Steam: `cod.exe` / `sp26-cod.exe`)**：
  - 自动感知 Steam 库目录，同步部署双代理至主根目录及单人战役子目录（`sp26\`）；
  - 支持通过 Steam 协议 (`steam://rungameid/1938090`) 唤醒拉起。
* **自定义 D3D12 商业游戏**：
  - 支持任意 64 位 DirectX 12 游戏路径浏览、一键部署与无感捕获。

---

## 🚀 快速上手 (Quick Start)

### 步骤 1：启动控制中心
双击运行：
```cmd
tools\TinecmaTool启动向导.bat
```
*(或直接运行 [`tools\launch_tinecmatool.bat`](tools/launch_tinecmatool.bat))*

脚本会自动请求管理员权限（UAC 提权），并在几秒内呼出统一图形控制中心面板：

```
+-------------------------------------------------------------+
|    TinecmaTool - D3D12 游戏抓帧控制中心                     |
+-------------------------------------------------------------+
| [1] 目标游戏选择与路径:                                     |
|   (*) 现代战争 2019 (战网: MW2019 / ModernWarfare.exe)      |
|   ( ) Call of Duty HQ (Steam: cod.exe / sp26-cod.exe)       |
|   ( ) 自定义... (Custom D3D12 Game)                         |
|   游戏目录: [ D:\Call of Duty Modern Warfare        ] [浏览]|
|                                                             |
| [2] 抓帧后端配置:                                           |
|   (*) TinecmaTool (RenderDoc v1.44) [推荐]                  |
|   ( ) Microsoft PIX (WinPixGpuCapturer)                     |
|                                                             |
| [3] 抓帧与调试工具箱:                                       |
|   [📸 立即抓帧(F12)]  [🖼️ 预览缩略图]  [🔍 打开截帧]       |
|   [📦 重新部署代理]   [📋 查看日志]    [🧹 清理反作弊]      |
|                                                             |
|   [x] 启动后保持控制面板开启 (推荐)                         |
|   [ 🚀 部署代理并启动游戏 ]                  [ 退出 ]       |
+-------------------------------------------------------------+
```

### 步骤 2：部署并启动游戏
1. 选中目标游戏（面板会自动检测并填充游戏安装路径）；
2. 确认抓帧后端为 **TinecmaTool**；
3. 点击 **`🚀 部署代理并启动游戏`**（或按回车键）；
4. 游戏自动拉起，控制面板常驻桌面，游戏左上角将呈现稳定的 TinecmaTool HUD 信息（如 `Capturing D3D12...`）。

### 步骤 3：一键抓帧与逆向分析
进入游戏 3D 渲染画面（如主菜单展示、单人战役或战斗对局）后：
1. **触发抓帧**：在面板中点击 **`📸 立即触发抓帧 (F12)`**（或在游戏窗口中按下 F12）；
2. **预览画面**：点击 **`🖼️ 预览最新截帧缩略图`**，控制中心自动调用 `qTinecmaTool thumb` 秒级提取渲染帧画面供即时确认；
3. **深入调试**：点击 **`🔍 在 qTinecmaTool 中打开`**，调试器图形界面随即载入桌面最新的 `.rdc` 文件，即可进行完整 DrawCall 拓扑追踪、CBV/SRV/UAV 资源检查与 HLSL 反编译着色器逐行调试。

---

## 💡 GUI 与启动排坑实战总结 (Pitfalls & Debugging Log)

在研发与实机适配过程中，总结了以下关键坑点与解决方案：

| 序号 | 坑点现象 | 根本原因 (Root Cause) | 解决方案 (Solution) |
| :---: | :--- | :--- | :--- |
| **1** | **PowerShell 界面解析报错 / 语法乱码** | Windows PowerShell 5.1 在中文环境下默认以 GBK (CP936) 解码无 BOM 的 UTF-8 脚本，导致中文注释和字符串切片解析错位，引发 `Token Expected` 异常。 | 必须使用带有 **UTF-8 BOM 头**（`EF BB BF`）的编码格式保存所有 `.ps1` 脚本文件，杜绝非 ASCII 字符解析崩溃。 |
| **2** | **现代战争 2019 启动闪退 / Win7 兼容层冲突** | MW2019 游戏根目录自带 1.9MB 的过时微软运行时（`d3d12.dll`），在 Win10/Win11 上无法正确提供现代 D3D12 与 DXGI 接口。 | 启动器自动将原文件安全重命名备份为 `d3d12_win7.dll.bak`，并替换为我们自研编译的 16KB D3D12 代理库，透传系统原生图形驱动。 |
| **3** | **现代战争 2019 启动无限递归爆栈闪退 (0xC00000FD)** | `dxgi_proxy.dll` 拦截 `CreateDXGIFactory*` 转发给 TinecmaTool 时，底层 Hook 查找 real factory 若再次调用回 proxy 导出函数，缺乏递归守卫导致无限回环爆栈崩溃。 | 在 `dxgi_proxy.cpp` 中引入 `thread_local bool t_inCreateFactory` 递归保护锁；并在 `dxgi_hooks.cpp` 中强制通过 `GetSystemDXGIModule()` 直接获取系统原生 DLL 导出，彻底斩断递归调用链。 |
| **4** | **现代战争 2019 没挂上 / `Creating swap chain with non-hooked device`** | `ModernWarfare.exe` 通过 `amd_ags_x64.dll!agsDriverExtensionsDX12_CreateDevice` 与 `GetProcAddress("D3D12CreateDevice")` 创建底层 D3D12 设备。此前错误地将 `modernwarfare` 设为 `g_SkipGraphicsDriverHooks = true` 并跳过了 IAT Hook，导致游戏绕过调试器拿到原生硬件设备，交换链接收到非 Hook 设备而拒绝抓帧。 | 在 `win32_libentry.cpp` 与 `win32_hook.cpp` 中解除对 `ModernWarfare.exe` 的限制（仅对壳进程 `cod.exe` 跳过），恢复对 `ModernWarfare.exe` 导入表 `agsDriverExtensionsDX12_CreateDevice` 与 `GetProcAddress` 的精准包装，实现 D3D12 设备与 DXGI 交换链 100% 成功挂载。 |
| **5** | **点击启动未能唤醒战网或游戏迅速退出** | 脚本未拉起战网客户端，且调用战网时未显式指定战网安装目录作为 `WorkingDirectory`，导致战网因加载不到配置而静默退出；游戏缺少战网凭据也随之退出。 | `gui_countdown.ps1` 自动探测注册表与 Program Files 中的 `Battle.net Launcher.exe`，指定工作目录拉起，并通过 `battlenet://ODIN` URI 协议及 `--exec="launch ODIN"` 唤醒战网定位至现代战争。 |
| **6** | **Frame 0 黑屏阶段按 F12 闪退 (0x00001337)** | 在游戏刚拉起、着色器未完成预编译前盲目触发抓帧，瞬时同步 GPU Readback 会导致反作弊线程看门狗超时判定为作弊挂起。 | 严禁在黑屏和着色器加载阶段按键，必须待游戏进入 **3D 主菜单角色展示** 或战斗画面、左上角 HUD 稳定呈现后触发。 |
| **7** | **UAC 管理员提权后找不到文件** | 批处理通过 `RunAs` 提权后，系统默认将当前工作目录（Working Directory）重置为 `C:\Windows\System32`。 | 启动脚本在提权前后均显式加入 `cd /d "%~dp0"`，确保工作目录严格锚定在 `tools\` 目录内。 |
| **8** | **反作弊服务挂起导致游戏无法拉起** | 前序测试残留的 `atvi-randgrid_sr` 反作弊驱动状态异常。 | 控制中心集成了 **`🧹 一键清理反作弊/环境`** 功能，一键停止驱动并清除残留信号，恢复干净启动环境。 |

---

## 📂 项目结构概览

```text
GraphicsDebugger-Steam-Cod/
├── renderdoc/                          # RenderDoc v1.44 核心源码 (C++)
│   ├── core/core.cpp                   # 扩展 isCod 免杀门控，屏蔽 TargetControl 端口
│   ├── os/win32/win32_libentry.cpp     # 跳过 IAT Hook 保护
│   └── driver/d3d12/                   # D3D12 设备包装与捕获管线
├── qrenderdoc/                         # Qt 图形界面前端源码
│
├── x64/Development/                    # 编译输出目录
│   ├── TinecmaTool.dll                 # 调试核心动态库 (76.5 MB, 无作弊特征)
│   ├── qTinecmaTool.exe                # 官方调试器 GUI (免安装版)
│   └── TinecmaToolcmd.exe              # 命令行分析与缩略图导出工具
│
├── tools/                              # [核心] 控制中心与工具集
│   ├── TinecmaTool启动向导.bat        # [推荐] 双击直达启动向导 (自动提权)
│   ├── launch_tinecmatool.bat           # 核心提权启动器
│   ├── gui_countdown.ps1                # 图形控制中心面板 (UTF-8 BOM)
│   ├── README.md                        # 工具集详细技术手册与排坑指引
│   │
│   ├── proxy/                           # [双代理劫持层]
│   │   ├── dxgi_proxy.cpp / dxgi.def    # DXGI 代理源码 (拦截 CreateDXGIFactory)
│   │   ├── d3d12_proxy.cpp / d3d12.def  # D3D12 代理源码 (拦截 D3D12CreateDevice)
│   │   ├── build.bat                    # 一键 MSVC x64 编译脚本
│   │   ├── dxgi.dll                     # 预编译 DXGI 代理动态库 (25 KB)
│   │   └── d3d12.dll                    # 预编译 D3D12 代理动态库 (16 KB)
│   │
│   └── diagnostics/                     # [诊断与测试工具集]
│       ├── test_capture.py              # 自动化抓帧与 .rdc 校验脚本
│       ├── run_live_capture.py          # 实机抓帧阶段断言监控
│       ├── capture_screen.py            # GDI/DIB 窗口与渲染截屏诊断
│       ├── check_desktop_windows.py     # 桌面所有窗口/错误弹窗枚举
│       ├── inspect_crash_dialog.py      # 崩溃弹窗自动捕获与文本提取
│       ├── measure_threads.ps1          # 目标进程线程采样与看门狗监控
│       ├── resolve.exe / resolve.cpp    # 崩溃地址符号反解工具 (APPCRASH 偏移分析)
│       └── read_rdc.ps1                 # RDC 捕获文件完整性校验
│
├── build_renderdoc.bat                  # 一键增量编译调试器核心
└── renderdoc.sln                        # Visual Studio 2022 解决方案
```

---

## 🔨 编译与构建 (Build from Source)

### 1. 编译调试器核心 (`TinecmaTool.dll`)
使用内置增量构建脚本或 Visual Studio 2022：
```cmd
build_renderdoc.bat
```
*(或在 MSBuild 环境下指定工程：`msbuild renderdoc.sln /p:Configuration=Development /p:Platform=x64`)*

### 2. 编译双代理动态库 (`dxgi.dll` & `d3d12.dll`)
在 Visual Studio 开发者命令提示符（x64 Native Tools Command Prompt）下执行：
```cmd
cd /d "C:\Program Files\GraphicsDebugger-Steam-Cod\tools\proxy"
build.bat
```
脚本将自动调用 `cl.exe` 和 `link.exe`，输出仅数十 KB 的纯 C 原生代理 DLL。

---

## ⚠️ 免责声明 (Disclaimer)

本项目仅供图形学学习、渲染架构研究、着色器逆向分析及正当的性能优化用途。严禁用于任何网络对战作弊或侵犯知识产权之非法行为。使用者需自行承担使用风险与遵守相关软件许可协议。
