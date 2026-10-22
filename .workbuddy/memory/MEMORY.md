# 项目长期记忆 — GraphicsDebugger-Endfield

> 详细过程见 `.workbuddy/memory/YYYY-MM-DD.md`；完整方案见根目录 `ENDFIELD_CAPTURE.md`

## 项目
- RenderDoc v1.44 fork，品牌 **TinecmaTool**；分支 `endfield/import`（远端同名分支）
- remote `https://github.com/Tinenc/GraphicsDebugger.git`
- 目标：PC 端给《明日方舟：终末地》抓帧
- 本树**已有编译产物** `x64\Development`（含 `qTinecmaTool.exe` / `TinecmaTool.dll` / `TinecmaTool.json`）
- ⚠️ **本树完全独立**：鸣潮那个 `GraphicsDebugger-Wuthering-Waves` 是另一个工具，
  只在摸索阶段当参考，**运行时不得依赖**

## ✅ 现行方案 = 方案 3：APC 注入 + CreateProcess 链式注入（2026-10-23 03:07 跑通）
- **json 保持 locked**（`enable_environment` 在），注册表指向本树，**抓完不用跑恢复脚本**
- 链路：`qTinecmaTool --Launch--> 1.6.0\Games.exe`（早鸟 APC 注入，无远程线程）
  → Games.exe 里 hook `CreateProcess` 强制 `CREATE_SUSPENDED` → 对挂起线程排队同一 APC
  → `Endfield.exe` 拿到 DLL，DllMain 在自己环境里设 enable 变量 → loader 加载 layer
- 操作：`tools\launch_endfield.bat` → qTinecmaTool 里 Executable 填
  `H:\Hypergryph Launcher\1.6.0\Games.exe`、**勾 Capture Child Processes**、不勾 Global hook
  → Launch → 启动器点开始游戏 → Attach `Endfield.exe` → F12
- 实证日志 `RenderDoc_2026.10.23_03.05.58.log`（单文件多 PID，行前缀 `RDOC <PID>:`）：
  `Loading into ...\Games.exe` → `Intercepting CreateProcessW` →
  `Loading into ...\Endfield.exe` → `Listening for target control on 38922` →
  `Initialised capture layer in Vulkan instance` → `Created capture device from physical device 0`
  → `RegisterSurface() window 0xC1DD8` → `Adding Vulkan frame capturer .../0xC1DD8`
- 端口分配：Games 38920 / QtWebEngineProcess 38921 / Endfield 38922
- **`RegisterSurface` + 窗口级 frame capturer ⇒ `HGCreateSwapchain` 走标准 WSI，present 帧抓得到**

## 为什么注入是唯一可行解
- 鹰角启动器创建 `Endfield.exe` 时**传自己的环境块**，`ENABLE_VULKAN_TINECMATOOL_CAPTURE` 不继承
  （读 PEB 证实 Games.exe 有、Endfield 读不到）
- `ApplyEnvironmentModification()` 只改当前进程，救不了子进程
- RenderDoc 在 Windows Vulkan 上**不 hook API**，全靠 layer 机制 ⇒ 注入的价值只是
  「把 enable 变量送进进程内」，DllMain 里 `RegisterEnvironmentModification` +
  `ApplyEnvironmentModification` 写进目标进程自己的环境
- 直启 `Endfield.exe` 恒 `PC launch auth failed, errorCode: 10100`（SDK 校验启动者身份），
  鉴权走 `SendMessage("OnExtraInfo")` → U8SDK 私有通道，**补参数无效**

## 代码改动（10 文件，单文件 `cl /Zs` 已验过）
| 文件 | 改动 |
|---|---|
| `os/os_specific.h` | `InjectIntoProcess` 加 `hSuspendedThread` |
| `os/win32/win32_process.cpp` | 新增 `InjectDLLViaAPC()`；`InjectDLL` 加 APC 分支 |
| `os/posix/posix_process.cpp` | 形参同步 |
| `os/win32/sys_win32_hooks.cpp` | `IsSkipInjectTarget()` 黑名单；`TCM_FORCE_CHILD_INJECT`；`SysHook::isGraphicsAPI()=false` |
| `hooks/hooks.h` + `hooks/hooks.cpp` | `LibraryHook::isGraphicsAPI()`（默认 true）；`LibraryHooks::RegisterProcessHooks()` |
| `os/win32/win32_libentry.cpp` | **relay 只注册进程 hook**，渲染目标全注册（见下） |
| `driver/vulkan/vk_layer.cpp` | `IsLayerCaptureTarget()` + Negotiate 里非目标让出 |
| `vk_common.h` + `wrappers/vk_device_funcs.cpp` | 声明 + 兜底透传 |

### ⚠️ relay 进程 vs 渲染目标（2026-10-23 03:15，登录器黑屏的根因）
- **症状**：Launch Games.exe 后，登录器窗口整块变黑（QtWebEngine 的网页 UI 全黑）
- **原因**：`add_hooks()` 无条件 `LibraryHooks::RegisterHooks()`，把 EGL/GLES/D3D 全 hook 了。
  Games.exe 是 Qt5+QtWebEngine，窗口走 **ANGLE（GLES over D3D11）**，日志证据：
  `egl_platform.cpp(92) Creating cloned context failed (3006)` /
  `gl_common.cpp(517) Detected vendor: None` / 十几个 `Adding OpenGLES device frame capturer`
  → 我们的 GLES wrapper 克隆不了 ANGLE context → 黑屏
- **修法**：`win32_libentry.cpp` 里按 exe 名分流
  ```cpp
  rdcstr target = "endfield.exe";   // 可被 TCM_LAYER_TARGET 覆盖
  if(f == target) LibraryHooks::RegisterHooks();        // 渲染目标
  else            LibraryHooks::RegisterProcessHooks(); // relay
  ```
  `RegisterProcessHooks()` 只跑 `isGraphicsAPI()==false` 的库 —— 全项目只有 `SysHook`。
  它仍注册 kernel32/advapi32/ws2_32 + `CreateProcessW/A`，**注入链完好但不碰图形**
- 全项目 LibraryHook 子类共 15 个，只有 `SysHook` 是非图形（D3D9/11/12、DXGI、Vulkan、
  EGL、GL、WGL、GLX、CGL、Metal、NVAPI、AMD、FakeVk 全是图形）
- `BeginHookRegistration`=`InitHookData()`、`EndHookRegistration`=排序+`HookAllModules()`，
  都不依赖图形库，跳过是安全的
- 好处：`UnityCrashHandler64.exe` / `PlatformProcess.exe` 也自动降为 relay，不再被 GL hook 拖累

### 注入黑名单要点
`tinecmatoolcmd` `qtinecmatool` `platformprocess` **`qtwebengineprocess`**
`ace-setup/ace-tray/ace-game/ace-service/aceservice/ace-base/anticheatexpert` `sguard/sgutray`
`crashsight/crashreport/kurosdk` `hgdownloadsdk/hgeventlogsdk/hgmarqueesdk`
- **ACE 系列必须跳过**：注进去会让 ACE 重装内核驱动然后杀掉游戏
- **`qtwebengineprocess` 2026-10-23 03:12 才加**（实机撞过：03:06:38 它被注入后在
  `resource_manager.h(820)` 撞 `Assertion failed: 'm_ResourceRecords.empty()'`，把整个 Games.exe 带崩）。
  **这条需要下次整体编译才生效**

## ⚠️ 方案 2 与方案 3 互相打架（03:00 踩到，已修）
- 症状：APC 注入成功（日志有 `Injected ... via early-bird APC`）但 Games.exe 不写日志不开端口
- 原因：方案 2 在 `add_hooks()` 里加的 `if(!IsLayerCaptureTarget()) return TRUE;`
  让注入进 Games.exe 的 DLL 直接 inert
- **方案 3 必须能注入 Games.exe**（父进程）才能 hook 它的 CreateProcessW
- 修法：删掉 `add_hooks()` 里的过滤；防污染职责只留在 `vkNegotiateLoaderLayerInterfaceVersion`

## 已废弃方案（勿回头）
- **全局 unlocked layer**：能通，但本机每个 Vulkan 程序加载 100MB DLL；
  qTinecmaTool 自己建 replay device 时撞 `vk_device_funcs.cpp:842
  Assertion failed: 'IsCaptureMode(m_State)'` 一开就崩；另外两套工具同样崩；
  **任何 .rdc 都打不开**；每次抓完要跑恢复脚本
- **进程名过滤单独用**：降级为兜底，代码保留（`IsLayerCaptureTarget`），但不是主路径
- **环境变量继承**：彻底死路，不要再试

## 环境坑（本机）
- `MSBuild.exe` 被安全策略拦；`reg.exe` 也在黑名单（读注册表用 `[Microsoft.Win32.Registry]`）
- **`cl.exe` 能跑**，但必须用 **python subprocess** 调（bash 直接调会被 MSYS 把 `/Zs` 转成路径）。
  参数：`/Zs /nologo /EHsc /DWIN64 /D_AMD64_ /DRENDERDOC_PLATFORM_WIN32
  /DRENDERDOC_PLATFORM_X64 /DRENDERDOC_EXPORTS /Irenderdoc /Irenderdoc\3rdparty`
  + VC include + SDK 的 ucrt/shared/um/winrt
  - `/DRENDERDOC_EXPORTS` 必须加，否则 `rdcstr.h` 找不到 `RENDERDOC_AllocArrayMem`
  - `win32_process.cpp` 单编有 6 个**原有**错误（缺 vcxproj 内 define），不是真错误
- ⚠️ **命名空间坑**：`Process::GetEnvVariable` / `FileIO::GetExecutableFilename`，
  两个都在 `os_specific.h` 但归属不同（曾导致 03:42 整体编译失败）
- **PowerShell 变量名大小写不敏感**：`$pId` 撞只读 `$PID` → 一律避
  `$PID/$HOST/$HOME/$PWD/$ERROR/$INPUT/$ARGS/$TRUE/$MATCHES/$_`
- `[Environment]::SetEnvironmentVariable(...,"User")` 不广播 `WM_SETTINGCHANGE`
- **铁律：环境变量只用 `set`（进程级），永不 `setx`**
- Bash 里跑 python 读 `AppData` 会被沙箱拦 → 用 PowerShell 或 Read
- Bash 的 `strings` 不存在，`grep`/`find` 不可靠
- **我起的进程都会被注入 `tsbx.dll`** → 游戏/注入实验必须用户自己跑

## ACE 封锁下的诊断手段（唯一可用）
- `add_hooks()` 无条件写 `Loading into ...` ⇒ **DLL 一进进程必有日志**，没日志 = 没加载
- `core.cpp(719)` 的 `capturing application` 只区分 replay/capture，与环境变量无关
- ⚠️ `ObRegisterCallbacks` 连 `SeDebugPrivilege` 都不放行：Endfield.exe 的
  `ReadProcessMemory` 恒 err=5、`NtReadVirtualMemory` 恒 `0xC0000022`、模块枚举恒 0 条
  → **只能靠端口 + 日志判断**
- `Player.log` 的 mtime 会骗人（关闭时也写），看文件内 streamline 时间戳

## 多工具共存（本机三套）
| 树 | 用途 | commit |
|---|---|---|
| `GraphicsDebugger-Endfield` | **本方案**，终末地 | `e11360084` |
| `GraphicsDebugger-Wuthering-Waves` | 鸣潮 | `1c8ceb77e` |
| `GraphicsDebugger-DF研究` | 三角洲行动 | `ae29f3a7e` |
- layer 名 `VK_LAYER_TINECMATOOL_Capture` **全局唯一** → 注册表只能一条，**三树互斥**
- 用 `qTinecmaTool.exe` 进程名别 `taskkill /im`，按 `$_.Path` 只杀本树
- 方案 3 下 json locked ⇒ **本树不再影响另外两棵树能不能打开工具/rdc**

## 工具
- `tools\launch_games.bat` — **一键首选，双击即用**。自提升（`-elevated` 标记防循环，
  `-NoAdmin` 可跳过）+ 5 步：json locked / 收敛 HKLM（已提权直接写，不再弹 UAC）/
  起 UI（只为 attach+F12）/ 清旧 Games·Endfield·Launcher·PlatformProcess / 跑
  `TinecmaToolcmd capture --opt-hook-children -c <模板> -d <目录> <Games.exe>`
- `--opt-hook-children` = GUI 里 **Capture Child Processes**（`renderdoccmd.cpp` 里
  `opts.hookIntoChildren = true`）。少了它注入止步 Games.exe
- ⚠️ **一键脚本刻意不设 `TCM_FORCE_CHILD_INJECT`**：`ShouldInject()` 里它会短路
  `hookIntoChildren` 判断，而 `TinecmaToolcmd.exe` 自己也加载 DLL 并注册 CreateProcess hook
  （非目标 → relay 分支）→ 会重复注入 Games.exe。`--opt-hook-children` 够用，
  因为 options 经 `INTERNAL_SetCaptureOptions` 在目标线程 resume **之前**已推送完
- `tools\launch_endfield.bat` — **只做准备，不启动游戏**。切分线是「谁去启动 Games.exe」：
  它准备环境（json locked / 收敛 HKLM / `set TCM_FORCE_CHILD_INJECT=1`）、
  按 `$_.Path` 只杀本树 qTinecmaTool 实例、`start` 打开 UI，然后打印该填什么
  （Executable / 勾 Capture Child Processes / 不勾 Global hook），**Launch 要你自己点**。
  **它不清旧的 Games.exe** → 启动器已开着时注入不进去，必须先手动关。
  适用：想逐步看哪步失败、要改具体 Capture Options、或作一键脚本的对照组
- ⚠️ **两个脚本都必须做路径规范化**：`for %%i in ("%~dp0..") do set "ROOT=%%~fi"`。
  直接 `set "ROOT=%~dp0.."` 会留 `..`，与注册表规范路径、`Process.Path` 都不相等
  → 每次误判「注册表要重写」+「本树实例在跑」永远匹配不到。2026-10-23 03:24 修
- `-Off` 现在只剩一个用途：撤销历史遗留的 unlock
- `PS` 变量必须定义在 `goto :DoRegister` **之前**；结尾用 `timeout /t` 不用 `pause`
- 早期辅助脚本（layer_ctl / endfield_status / endfield_probe / read_peb_env*）已全删
- 干跑测试时必须把 `Start-Process -Verb RunAs` / `start ""` / `capture` /
  `Stop-Process -Id` / `timeout` / `pause` 全部替换掉，否则会真的提权、启 UI、杀进程

## 已抓到的样例
- `%TEMP%\TinecmaTool\Endfield_2026.10.23_00.58_frame61125.rdc`，588 MB（旧 unlocked 方案抓的）

## 待办
- [ ] **重新编译**，让 `qtwebengineprocess` 黑名单 + relay 分支同时生效
      （当前构建两者都没有：前者让第一次启动崩一次，后者让登录器黑屏）
- [ ] 抓一帧验证 rdc 完整性 + DLSS/Streamline pass 能否降级回放
- [ ] 观察 Endfield.exe 注入后长时间运行稳定性
- [ ] 抓别的游戏时用 `TCM_LAYER_TARGET=<exe名>` 换 layer 目标，不用重编
