# TinecmaTool (Stealth RenderDoc) - 工具集与控制中心手册

本目录 (`C:\Program Files\GraphicsDebugger-Steam-Cod\tools`) 包含 TinecmaTool 图形调试器的所有启动控制中心、双代理劫持层、诊断监测脚本以及反作弊免杀工具。

---

## 目录结构概览

```text
tools/
├── TinecmaTool启动向导.bat       # [推荐] 双击直达启动入口 (自动调用 launch_tinecmatool.bat)
├── launch_tinecmatool.bat          # [核心] 一键管理员提权启动器 (支持 COD HQ / MW2019 / 自定义游戏)
├── gui_countdown.ps1               # [核心] 图形抓帧控制中心面板 (集成部署、启动、抓帧、预览、诊断)
├── launch_tinecmatool_steam.bat    # 向下兼容启动脚本 (自动重定向至 launch_tinecmatool.bat)
│
├── proxy/                          # [双代理劫持层] DXGI 与 D3D12 双代理源代码与编译输出
│   ├── build.bat                   # 一键 MSVC x64 编译脚本
│   ├── dxgi_proxy.cpp              # DXGI 工厂拦截与帧捕获触发桥接 (生成 dxgi.dll)
│   ├── dxgi.def                    # DXGI 导出表定义
│   ├── d3d12_proxy.cpp             # D3D12 设备创建桥接与 Win7 兼容层替代 (生成 d3d12.dll)
│   ├── d3d12.def                   # D3D12 导出表定义
│   ├── dxgi.dll                    # 预编译 DXGI 代理动态库 (25 KB)
│   └── d3d12.dll                   # 预编译 D3D12 代理动态库 (16 KB)
│
├── diagnostics/                    # [诊断与测试工具集] 实机联调、线程分析、崩溃反解与截屏
│   ├── test_capture.py             # 自动化抓帧触发与 .rdc 生成校验脚本
│   ├── run_live_capture.py         # 实机抓帧监控与阶段性断言测试工具
│   ├── capture_screen.py           # 游戏窗口与渲染帧缓冲截屏诊断脚本
│   ├── check_desktop_windows.py    # 桌面所有窗口/错误弹窗枚举工具
│   ├── check_windows.py            # 快速查找特定标题窗口
│   ├── inspect_crash_dialog.py     # 崩溃弹窗自动捕获与文本提取
│   ├── measure_threads.ps1         # 游戏进程线程活动与看门狗周期采样器
│   ├── check_threads.ps1           # 线程数快速巡检脚本
│   ├── etw_capture.py              # ETW 图形/反作弊事件链路追踪
│   ├── read_rdc.ps1                # RDC 捕获文件完整性与头信息校验
│   ├── resolve.exe / resolve.cpp   # 崩溃地址符号反解工具 (解析 APPCRASH 偏移)
│   ├── resolve_symbol.py           # Python 版符号反解辅助工具
│   ├── run_resolve.bat             # 符号反解便捷批处理
│   ├── diagnose.ps1                # 环境依赖与驱动状态巡检
│   └── check_strings.ps1           # 二进制特征字符串残留扫描
│
├── auto_bypass.py                  # 自动化文件特征抹除与启动工具
├── string_replacer.py              # 二进制字符串批量替换器
├── rename_tool.ps1                 # 文件重命名与备份工具
├── inject_dll.ps1                  # 手动 DLL 远程注入脚本
├── run-steam-cod.bat               # 传统命令行启动脚本
├── inject-steam.bat                # Steam 专用注入脚本
├── hook_process_list.cpp           # 进程钩子白名单配置源码
└── TINECMATOOL_COD_D3D12_CAPTURE_DESIGN.md # 核心技术架构设计白皮书
```

---

## 🚀 核心控制中心 (Control Center)

### 1. 启动方式
双击 `tools\TinecmaTool启动向导.bat`（或 `tools\launch_tinecmatool.bat`），或在命令行运行：
```cmd
"C:\Program Files\GraphicsDebugger-Steam-Cod\tools\launch_tinecmatool.bat"
```
脚本会自动请求管理员权限（UAC 提权）并呼出图形控制中心面板 [gui_countdown.ps1](file:///C:/Program%20Files/GraphicsDebugger-Steam-Cod/tools/gui_countdown.ps1)。

---

### 2. 面板功能一览

#### 选项 1：目标游戏选择与路径 (Target Game & Directory)
* **现代战争 2019 (战网: MW2019 / ModernWarfare.exe)**：
  - 自动检测并定位 `D:\Call of Duty Modern Warfare`；
  - 自动将原厂 1.9MB 的 Win7 兼容库 `d3d12.dll` 备份为 `d3d12_win7.dll.bak`；
  - 部署 DXGI + D3D12 双代理，并设置免杀策略（关闭 TargetControl 端口、屏蔽 IAT Hook、PEB 模块链摘除）；
  - 支持直接拉起 `ModernWarfare.exe`，战网客户端亦可直接点击“进入游戏”。
* **Call of Duty HQ (Steam: cod.exe / sp26-cod.exe)**：
  - 自动检测 Steam 库（如 `F:\SteamLibrary\...`）；
  - 自动将双代理同步部署至游戏根目录及单人战役子目录 `sp26\`；
  - 通过 Steam URL (`steam://rungameid/1938090`) 唤醒拉起。
* **自定义... (Custom D3D12 Game)**：
  - 支持任意 DirectX 12 商业游戏，提供目录浏览选择。

#### 选项 2：抓帧后端选择 (Capture Backend)
* **TinecmaTool (RenderDoc v1.44) [推荐]**：
  - 深度逆向与 Shader 调试首选。具备 1.33s 同步直写与 SEH 容错，完整抓取所有 DrawCall、PSO、根签名、CBV/SRV/UAV 资源与着色器字节码。
* **Microsoft PIX (WinPixGpuCapturer)**：
  - 微软官方性能剖析首选。支持 GPU 时间线、GPU 时钟周期、绘制阶段耗时分析，生成 `.wpix` 帧文件。

#### 抓帧与调试工具箱 (Integrated Panel Toolbox)
面板下方集成了 6 项核心调试功能，**无需打开黑屏控制台，在面板内一键操作**：
1. 📸 **立即触发抓帧 (F12)**：向游戏发送 `trigger_capture` 信号，自动轮询抓帧完成状态，并显示生成的截帧文件名。
2. 🖼️ **预览最新截帧缩略图**：调用 `qTinecmaTool.exe thumb` 秒级提取最新 `.rdc` 的渲染画面缩略图并自动弹出查看。
3. 🔍 **在 qTinecmaTool 中打开**：自动寻找桌面最新生成的 `COD_RDC_*.rdc` 并在调试器 UI 中直接载入。
4. 📦 **重新部署双代理 DLL**：一键将编译好的 `dxgi.dll` 与 `d3d12.dll` 覆盖到当前选中的游戏目录。
5. 📋 **查看代理运行日志**：在记事本中打开桌面 `dxgi_proxy.log`，实时查看 DXGI/D3D12 初始化与截帧事件。
6. 🧹 **一键清理反作弊/环境**：停止 `atvi-randgrid_sr` 反作弊驱动与清理桌面残留的触发信号文件。

#### 启动游戏 (Launch Game)
* 点击 **`🚀 部署代理并启动游戏`**（或按回车键）；
* 面板默认勾选 **“启动后保持控制面板开启”**，游戏运行期间控制面板常驻桌面，随时可点击抓帧与查看缩略图。

---

## 🛠️ 双代理劫持层技术细节 (`tools/proxy/`)

COD 系列（COD HQ 与 MW2019）采用了极为严格的启动完整性校验与反作弊机制，传统 Hook 方案会触发严重故障：
1. **未受控设备错误 (`Creating swap chain with non-hooked device`)**：
   - 游戏使用动态加载的 `d3d12.dll!D3D12CreateDevice` 创建底层设备，却使用静态绑定的 `dxgi.dll!CreateDXGIFactory` 创建交换链。
   - 若只放 `dxgi.dll` 代理，交换链接收到的是未经 RenderDoc 包装的原生原生 D3D12 设备，导致 RenderDoc 判定设备未受控而拒绝呈现 HUD 和抓帧。
   - **解决方案**：必须在游戏目录同时部署 `dxgi.dll` 和 `d3d12.dll`，D3D12 代理拦截 `D3D12CreateDevice` 桥接至 `TINECMATOOL_D3D12CreateDevice`，DXGI 代理拦截 `CreateDXGIFactory*` 桥接至 `TINECMATOOL_CreateDXGIFactory*`，实现双向设备匹配。
2. **D3D12onWin7 遗留库避坑**：
   - 《现代战争 2019》根目录自带 1.9MB 的微软旧版 Win7 运行时（`d3d12.dll`）。
   - 该旧库在 Windows 10/11 上无法提供现代 D3D12 选项与 DXGI 交换链接口。
   - **解决方案**：启动器自动将原文件重命名备份为 `d3d12_win7.dll.bak`，并部署我们的自研 D3D12 代理（透传 Windows 11 `System32\d3d12.dll` 并桥接 TinecmaTool）。
3. **PEB 模块链物理隐匿 (UnlinkModuleFromPEB)**：
   - 代理 DLL 在 `LoadLibraryA(g_rdocPath)` 之后，立刻遍历 PEB（`PEB->Ldr`）并将 TinecmaTool.dll 从 `InLoadOrderModuleList`、`InMemoryOrderModuleList` 与 `InInitializationOrderModuleList` 中双向摘链。
   - 使得反作弊驱动枚举已加载模块时完全看不见调试器 DLL。
4. **反作弊端口与 IAT 保护**：
   - 在 `renderdoc/core/core.cpp` 中识别到 COD 进程时，跳过打开 TargetControl TCP 监听端口（防止端口扫描看门狗报警）；
   - 在 `renderdoc/os/win32/win32_libentry.cpp` 中识别到 COD 进程时，设置 `g_SkipGraphicsDriverHooks = true`，跳过改写 PE 导入表（防止内存完整性校验 APPCRASH）。

### 重新编译代理 DLL
若修改了 `dxgi_proxy.cpp` 或 `d3d12_proxy.cpp`，执行以下批处理即可秒级完成 x64 编译：
```cmd
cd /d "C:\Program Files\GraphicsDebugger-Steam-Cod\tools\proxy"
build.bat
```

---

## 🔬 诊断与测试工具集 (`tools/diagnostics/`)

| 脚本文件 | 说明与用途 |
| :--- | :--- |
| **`test_capture.py`** | 命令行自动化截帧工具。监控目标进程，写入触发信号，等待 `.rdc` 生成并验证文件头。 |
| **`run_live_capture.py`** | 实机联调抓帧全流程测试脚本，包含驱动清理、代理部署、进程拉起与断言验证。 |
| **`capture_screen.py`** | 利用 Windows GDI/DIB 截取游戏窗口与全屏画面的诊断工具，输出 `.jpg` 验证 HUD 显示。 |
| **`check_desktop_windows.py`** | 递归枚举当前桌面所有顶级窗口的 HWND、PID、类名与标题，用于排查弹窗与隐蔽窗口。 |
| **`inspect_crash_dialog.py`** | 自动查找并截获游戏崩溃报告对话框文本（如 0xC0000005 错误详情）。 |
| **`measure_threads.ps1`** | 采样并统计目标进程在启动与运行期的线程创建波动，监测看门狗线程行为。 |
| **`resolve.exe`** | 崩溃分析工具。根据崩溃模块基址与相对偏移（RVA），反解出对应的符号名与源代码行号。 |
| **`read_rdc.ps1`** | 读取并解析 `.rdc` 捕获文件头部数据块，校验截帧是否完整。 |
| **`diagnose.ps1`** | 检查当前系统的显卡驱动版本、Vulkan 层、注册表 AppInit 以及依赖环境。 |
| **`check_strings.ps1`** | 扫描编译输出的二进制文件中是否还残留有原版敏感特征字符串。 |

---

## 📜 传统免安装工具 (Legacy Tools)

* **`auto_bypass.py`**：早期用于非代理环境下的特征二进制自动重命名与注入脚本。
* **`string_replacer.py`**：通用的二进制文件字符串查找与等长替换工具。
* **`rename_tool.ps1`**：批量将 RenderDoc 原生文件名替换为 TinecmaTools 的辅助脚本。
* **`inject_dll.ps1`**：利用 `CreateRemoteThread` 执行 DLL 延迟注入的 PowerShell 脚本。

---

## 常见问题与排坑手册 (FAQ)

### Q1: 游戏左上角未出现 HUD，提示 `Creating swap chain with non-hooked device`？
* **原因**：游戏目录下只放了 `dxgi.dll`，缺少 `d3d12.dll` 代理，导致设备未经拦截包装。
* **处理**：在面板中点击 **`📦 重新部署双代理 DLL`**，确保游戏目录下同时存在 `dxgi.dll` 与 `d3d12.dll`。

### Q2: 现代战争 2019 启动闪退或报错 `E_PIX_DEVICE_D3D12_FOR_WIN7_DETECTED`？
* **原因**：游戏目录下存在原厂的 1.9MB `d3d12.dll`（Win7 遗留运行时），与 Windows 11 不兼容。
* **处理**：确保原 1.9MB 文件被重命名为 `d3d12_win7.dll.bak`，且游戏根目录下的 `d3d12.dll` 是由我们编译的 16KB 代理库。

### Q3: 为什么在游戏刚拉起黑屏（Frame 0-10）时按 F12 会闪退？
* **原因**：启动极早期着色器管线未完全就绪，盲发抓帧信号触发瞬间 GPU Readback 会导致反作弊看门狗卡顿超时（0x00001337）。
* **处理**：请等待游戏着色器加载完成，平稳进入 **3D 主菜单角色展示** 或 **战斗对战画面** 后，再按 F12 抓帧。

### Q4: 如何查看抓帧文件并调试着色器？
* **处理**：在面板中直接点击 **`🔍 在 qTinecmaTool 中打开`**，调试器将自动打开桌面最新的 `.rdc` 文件。在 **Pipeline State** 视图中即可逐行调试 HLSL 反编译着色器并导出纹理资源。
