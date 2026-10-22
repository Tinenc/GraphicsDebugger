# 终末地 PC 抓帧 —— 现行方案

> **状态：已实机跑通**（2026-10-23 03:07，RTX 4090 / Vulkan）
> **现行方案 = 方案 3：APC 注入 + CreateProcess 链式注入**
> 本文档已整体替换掉之前的「全局 unlocked layer」方案。那个方案已废弃，原因见 §7。

---

## 1. TL;DR

### 首选：一键

```bat
tools\launch_games.bat
```

双击即可。脚本会自己请求管理员权限（UAC 一次），然后完成全部准备并**直接拉起
`Games.exe` 进入抓帧状态**——不需要在 qTinecmaTool 里填任何东西、不需要点 Launch。

之后你只需要：

1. 启动器窗口里点「开始游戏」
2. 进到能动的画面
3. qTinecmaTool 里 **Attach to Running Instance** → `Endfield.exe`
4. 游戏里按 **F12**

抓帧文件落在 `%TEMP%\TinecmaTool\endfield_frame*.rdc`。

> 加 `-NoAdmin` 可跳过提权（用于对比排查，此时无法写 HKLM）。

### 备选：手动 GUI 流程

```bat
tools\launch_endfield.bat
```

**它不是启动器**。它只做三件事：把环境准备好（json 状态、HKLM 注册、环境变量）、
杀掉本树的旧工具实例、打开 qTinecmaTool 窗口并告诉你该填什么。
**`Games.exe` 要你自己在 GUI 里点 Launch 拉起来。**

| 字段 | 值 |
|---|---|
| Executable | `H:\Hypergryph Launcher\1.6.0\Games.exe` |
| Capture Child Processes | **勾上** |
| Global hook | 不勾 |
| 然后 | 点 **Launch** → 启动器窗口里点「开始游戏」 |

适合想看清每一步、要临时改 Capture Options、或者一键脚本出问题拿来对照的场合。
详见 §3.0 的对比表和 §3.2。

**核心优势：抓完不用跑任何恢复脚本。** json 一直是 locked 状态，机器上其他 Vulkan 程序
（包括另外两套 TinecmaTool、任何 .rdc 双击打开）完全不受影响。

---

## 2. 工作原理

### 2.1 为什么不能直启游戏

```
Player.log:  [SDK] PC launch auth failed, errorCode: 10100,
             filePath: ...\qTinecmaTool.exe
             → NullReferenceException at Beyond.GameApp._UnInitGameStateMachine
```

`filePath` 报的是**当前进程自己的路径**，说明 SDK 校验的是「谁启动了我」，
不是命令行参数。鉴权数据走 `SendMessage("OnExtraInfo")` → `U8SDK` 私有通道，
不在命令行里，补参数无效。

**结论：必须由 `1.6.0\Games.exe` 拉起 `Endfield.exe`。** 我们的注入点因此必须放在
`Games.exe` 上——它是唯一能合法创建游戏进程的角色。

### 2.2 注入解决了什么问题

之前所有失败都卡在同一处：`ENABLE_VULKAN_TINECMATOOL_CAPTURE` 到不了游戏进程。

```
在 bat 里 set ENABLE_VULKAN_TINECMATOOL_CAPTURE=1
  → Launcher.exe           ✓ 继承
  → Games.exe              ✓ 继承（读 PEB 确认过）
  → Endfield.exe           ✗ 没了
```

鹰角启动器创建游戏进程时**传自己的环境块**，不继承父进程环境。
`ApplyEnvironmentModification()` 只改当前进程，救不了子进程。

注入绕开了这个问题：DLL 进入目标进程后，在 `DllMain` 里执行
`RegisterEnvironmentModification(ENABLE_VULKAN_TINECMATOOL_CAPTURE, "1")` +
`ApplyEnvironmentModification()`，把变量写进**进程自己的环境**。
之后 Unity 创建 Vulkan instance 时，loader 自然就能读到它。

> 关键认知：RenderDoc 在 Windows 的 Vulkan 上**不 hook 任何 API**，
> 它完全依赖 layer 机制（见 `vk_layer.cpp` 里 Negotiate 附近的注释）。
> 注入的唯一价值就是「把 enable 变量送到进程内」。

### 2.3 五步链

```
1. qTinecmaTool  ──Launch──▶  1.6.0\Games.exe
                                    ▲
2.                                  │  早鸟 APC 注入（不建远程线程）
   qTinecmaTool 把 TinecmaTool.dll 塞进 Games.exe 的挂起状态

3. Games.exe 里 DllMain 跑起来 → 开 target control 端口
   → hook CreateProcess（因为勾了 Capture Child Processes）

4. 你点「开始游戏」时 Games.exe 调 CreateProcess 创建 Endfield.exe
   → hook 强制 |= CREATE_SUSPENDED
   → 对挂起线程排队同一个 APC → resume
   → Endfield.exe 拿到 TinecmaTool.dll，DllMain 在自己环境里设 enable 变量

5. Unity 建 Vulkan instance → loader 读到变量 → 加载 layer
   → RegisterSurface + frame capturer → present 帧可抓
```

**不建远程线程**是重点：ACE 的 `ObRegisterCallbacks` 盯的就是
`CreateRemoteThread` / `WriteProcessMemory` 这类动作。早鸟 APC
（`QueueUserAPC` 到刚创建还没跑的挂起线程）在 ACE 看来与正常进程启动无异。

> `Games.exe` 在这里只是**中继（relay）**，不是抓帧目标。它自己不需要任何图形 hook，
> 只需要 `CreateProcess`。区分这两种角色是必要的——给 relay 注册图形 hook 会让
> QtWebEngine 绘制的登录器窗口变黑，详见 §5.1。

---

## 3. 脚本

两个脚本都是自包含的，只认本树的产物：

```
..\x64\Development\qTinecmaTool.exe + TinecmaToolcmd.exe + TinecmaTool.json + TinecmaTool.dll
```

### 3.0 两个脚本分别干什么

分工只有一条分界线：**谁去启动 `Games.exe`**。

| | `launch_games.bat` | `launch_endfield.bat` |
|---|---|---|
| 定位 | **一键跑完整条链** | **只做准备，启动交给你** |
| 准备环境（json / 注册表 / 环境变量） | 做 | 做 |
| 打开 qTinecmaTool UI | 做 | 做 |
| **启动 `Games.exe`** | **做**（`TinecmaToolcmd capture`） | **不做**，要你在 GUI 里点 Launch |
| 清理旧游戏进程 | 做 | 不做 |
| 需要管理员 | 自己请求，一次 UAC | 只在注册表跑偏时弹一次 |
| 点完「开始游戏」之前要手动做的事 | 无 | 填 Executable / 勾 Capture Child Processes / 点 Launch |
| 适用 | 日常抓帧 | 想看清每一步、要临时改 Capture Options、一键脚本出问题时的对照组 |

两者可以先后用：`launch_endfield.bat` 起好 UI 之后，照样可以在 GUI 里 Launch；
反过来一键脚本跑过一次，UI 已经开着，也不需要再跑 `launch_endfield.bat`。

### 3.1 `launch_games.bat` —— 一键（首选）

原理是把 GUI 里那三个手动动作换成命令行：

```bat
TinecmaToolcmd.exe capture --opt-hook-children -c "<模板>" -d "<工作目录>" "<Games.exe>"
```

`--opt-hook-children` 就是 GUI 里 **Capture Child Processes** 的命令行名字
（`renderdoccmd.cpp` 里 `opts.hookIntoChildren = true`）。少了它注入止步于 `Games.exe`。

| 步骤 | 动作 |
|---|---|
| 0 | 提权检查，不是管理员就 `Start-Process -Verb RunAs` 重跑自己（带 `-elevated` 防循环） |
| 1/5 | 确保 json **locked**，有历史 unlock 就从 `.bak` 还原 |
| 2/5 | 校验/收敛 HKLM 条目为「只有一条且指向本树」。**已提权，直接写，不再弹 UAC** |
| 3/5 | 起 qTinecmaTool UI（只为 attach 和按 F12，**不用它 Launch**） |
| 4/5 | 清掉旧的 `Endfield / Games / Launcher / PlatformProcess` 进程 |
| 5/5 | `TinecmaToolcmd capture --opt-hook-children` 拉起 `Games.exe` |

第 4 步为什么必须做：`CreateProcess` hook 存在于**进程内**，一个在我们注入之前就已经起来的
`Games.exe` 永远不会把注入传给它的子进程。这时候点「开始游戏」只会得到一个没被 hook 的
`Endfield.exe`。

**刻意不设 `TCM_FORCE_CHILD_INJECT`。** 那个变量在 `ShouldInject()` 里会短路
`hookIntoChildren` 判断，而 `TinecmaToolcmd.exe` 自己也会加载 DLL 并注册 `CreateProcess` hook
（它作为非目标进程走 relay 分支）。设了它，`Games.exe` 会被注入两次——一次来自 cmd 自己的
hook，一次来自 `ExecuteAndInject`。`--opt-hook-children` 在这条路上已经够用：options 通过
`INTERNAL_SetCaptureOptions` 在目标线程 resume **之前**就推送完了，不存在竞速。

### 3.2 `launch_endfield.bat` —— 准备环境 + 打开工具，不启动游戏

这个脚本解决的是「**环境对不对**」，不是「**怎么启动**」。

它最后停在一个已经开着的 qTinecmaTool 窗口上，并打印出该填什么：

```
Executable : H:\Hypergryph Launcher\1.6.0\Games.exe
[x] Capture Child Processes
[ ] Global hook
```

然后由你在 GUI 里点 **Launch**，启动器才会出现。

**它明确不做的事**：不启动 `Games.exe`、不做注入、不碰游戏进程、不按 F12。
启动这一步完全交给你，因为 GUI 里有几个东西是命令行覆盖不到的——Capture Options 的
逐项开关、要不要顺带抓 CPU callstacks、临时换目标进程。想调这些就得走 GUI。

什么时候用它：

- 一键脚本报错，想逐步看清是哪一步失败（它的输出把 4 步拆开打印）
- 要改 Capture Options 里的具体选项，`--opt-*` 那一串参数太长
- 临时想抓别的进程，直接在 Executable 栏改路径
- 只是想确认注册表、json 状态是否正常（跑一遍看 `[1/4]` `[2/4]` 的输出）

| 步骤 | 动作 | 为什么 |
|---|---|---|
| 1/4 | 确保 json 是 **locked**（`enable_environment` 存在），有过 unlock 就从 `.bak` 还原 | 注入负责送变量，layer 不该再全局加载 |
| 2/4 | 校验 HKLM 里 TinecmaTool 条目**只有一条且指向本树**，不对就 UAC 自提升收敛 | layer 名 `VK_LAYER_TINECMATOOL_Capture` 全局唯一，多条目会让 loader 取错 DLL |
| 3/4 | `set TCM_FORCE_CHILD_INJECT=1` | 这条路径下 qTinecmaTool 是 replay 应用、不注册 CreateProcess hook，不存在重复注入风险 |
| 4/4 | 按 `$_.Path` 只杀本树的 qTinecmaTool 实例，再 `start` | 不能 `taskkill /im`，会误杀另外两套工具 |

之后打印 qTinecmaTool 里该怎么填，20 秒后自动关窗口。

> 它**不清理旧的 `Games.exe`**，所以如果启动器已经开着，必须先手动关掉再 Launch——
> 否则那个进程里没有我们的 hook，点「开始游戏」注入不到 `Endfield.exe`。
> 一键脚本的第 4 步就是替你做这件事。

> **两个脚本都会做路径规范化**（`for %%i in ("%~dp0..") do set "ROOT=%%~fi"`）。
> 少了它 `%ROOT%` 里留着 `..`，和注册表里的规范路径、`Process.Path` 都不相等，
> 结果每次运行都误判成「注册表需要重写」，而且「本树实例是否在跑」永远匹配不到。

### 3.3 `-Off` 的现状

```bat
tools\launch_endfield.bat -Off
```

现在的用途只剩一个：**撤销历史遗留的 unlock**。方案 3 本身不需要靠它收工。
如果你从没跑过旧的 unlocked 方案，这条命令基本用不上。

---

## 4. 成功判据

### 4.1 实测日志（2026-10-23 03:05:58，本树产物 `e11360084`）

```
RDOC 059912: [03:06:04] win32_process.cpp(629) - Running process H:\...\1.6.0\Games.exe
RDOC 059912: [03:06:05] win32_process.cpp(353) - Injected ...\TinecmaTool.dll
                                               via early-bird APC, no remote thread created
RDOC 090628: [03:06:05] core.cpp(734)          - Listening for target control on 38920
RDOC 090628: [03:06:05] win32_libentry.cpp(81) - Loading into H:\...\1.6.0\Games.exe
RDOC 077592: [03:06:49] win32_process.cpp(299) - Intercepting CreateProcessW
RDOC 077592: [03:06:49] win32_process.cpp(714) - Injecting renderdoc into process 75612
RDOC 075612: [03:06:49] win32_libentry.cpp(81) - Loading into ...\Endfield Game\Endfield.exe
RDOC 075612: [03:06:49] core.cpp(734)          - Listening for target control on 38922
RDOC 075612: [03:06:55] vk_device_funcs.cpp(971)  - Initialised capture layer in Vulkan instance.
RDOC 075612: [03:06:55] vk_device_funcs.cpp(1555) - physical device 0: NVIDIA GeForce RTX 4090
RDOC 075612: [03:07:01] vk_device_funcs.cpp(4962) - Created capture device from physical device 0
RDOC 075612: [03:07:02] vk_core.cpp(5069)         - RegisterSurface() window 00000000000C1DD8
RDOC 075612: [03:07:02] core.cpp(2346)            - Adding Vulkan frame capturer for .../0xC1DD8
```

最后两行是根本判据：**`RegisterSurface()` + 窗口级 frame capturer**
说明 `HGCreateSwapchain` 走的是标准 WSI，present 帧抓得到。
这是从第一天起悬着的最大技术风险，已经排除。

### 4.2 三秒判定

```bat
netstat -ano | findstr "3892"
```

正常会看到三个端口（PID 每次不同）：

```
TCP  0.0.0.0:38920  ...  PID 77592   Games.exe
TCP  0.0.0.0:38921  ...  PID 68928   QtWebEngineProcess.exe
TCP  0.0.0.0:38922  ...  PID 75612   Endfield.exe     ← 要的是这个
```

日志现在是**单个文件包含所有进程**，每行前缀是 `RDOC <PID>:`。
搜 `Loading into` 就能看到整条链：

```powershell
Select-String -Path "$env:TEMP\TinecmaTool\RenderDoc*.log" -Pattern "Loading into"
```

---

## 5. 代码改动清单

相对 upstream RenderDoc v1.44，本树有 7 处改动。全部已在单文件级别编译验证通过。

| 文件 | 改动 |
|---|---|
| `os/os_specific.h` | `InjectIntoProcess` 增加 `hSuspendedThread` 参数 |
| `os/win32/win32_process.cpp` | 新增 `InjectDLLViaAPC()`；`InjectDLL` 加 APC 分支；签名贯通 |
| `os/posix/posix_process.cpp` | 同步形参（Unix 上仍是 unimplemented） |
| `os/win32/sys_win32_hooks.cpp` | `IsSkipInjectTarget()` 黑名单；`TCM_FORCE_CHILD_INJECT`；两处 `ShouldInject`；`SysHook::isGraphicsAPI()` 返回 false |
| `hooks/hooks.h` + `hooks/hooks.cpp` | 新增 `LibraryHooks::RegisterProcessHooks()`，遍历时跳过所有图形 API |
| `os/win32/win32_libentry.cpp` | 区分 **relay 进程**（只注册进程 hook）与**渲染目标**（全注册） |
| `driver/vulkan/vk_layer.cpp` | 新增 `IsLayerCaptureTarget()`；`...NegotiateLoaderLayerInterfaceVersion` 里非目标直接 `VK_ERROR_INITIALIZATION_FAILED` |
| `driver/vulkan/vk_common.h` + `wrappers/vk_device_funcs.cpp` | 声明；`vkCreateInstance` 兜底透传 |

### 5.1 relay 进程 vs 渲染目标

这是最容易忽略、但一踩就会毁掉整个体验的一处。

`Games.exe` 是 **relay**：它存在的唯一意义是把注入传下去。它是 Qt5 + QtWebEngine 的 UI，
窗口通过 **ANGLE（GLES over D3D11）** 绘制。如果给它注册完整的 hook 集：

```
egl_platform.cpp(92)  - Creating cloned context failed (3006)
gl_common.cpp(517)    - Detected vendor: None
core.cpp(2310)        - Adding OpenGLES device frame capturer ...  (十几个)
```

我们的 GLES wrapper 克隆不了 ANGLE 的 context → **登录器窗口整块变黑**。

改法：`LibraryHook` 加一个 `virtual bool isGraphicsAPI()`（默认 true），
只有 `SysHook` 覆盖成 false。然后：

```cpp
// win32_libentry.cpp
rdcstr target = "endfield.exe";          // 可被 TCM_LAYER_TARGET 覆盖
if(f == target)
  LibraryHooks::RegisterHooks();         // 渲染目标：全注册
else
  LibraryHooks::RegisterProcessHooks();  // relay：只 hook CreateProcess 家族
```

`RegisterProcessHooks()` 只跑 `isGraphicsAPI() == false` 的库——全项目只有 `SysHook` 一个。
它仍然注册 kernel32 / advapi32 / ws2_32 并挂 `CreateProcessW/A` 等，
所以注入链完好，但**一个图形 API 都不碰**。

副作用是好的：`UnityCrashHandler64.exe`、`PlatformProcess.exe` 这类也被当作 relay，
不再被 GL hook 拖累。

### 5.2 注入黑名单（`IsSkipInjectTarget`）

```cpp
tinecmatoolcmd.exe  qtinecmatool.exe          // 自己，注进去无限递归
platformprocess.exe qtwebengineprocess        // 启动器辅助进程
ace-setup ace-tray ace-game ace-service aceservice ace-base anticheatexpert
sguard sgutray                                // ACE 全家
crashsight crashreport kurosdk                // 崩溃上报
hgdownloadsdk hgeventlogsdk hgmarqueesdk      // 鹰角自己的 SDK
```

- **ACE 系列必须跳过**：注进 ACE 的安装/托盘进程会让它重新装内核驱动，
  然后 ACE 直接杀掉被保护的游戏进程。
- **`qtwebengineprocess` 是实机加的**：第一次跑（03:06:38）它被注入后
  在 `resource_manager.h(820)` 撞 `Assertion failed: 'm_ResourceRecords.empty()'`，
  把整个 `Games.exe` 带崩，只能重启一次。加入黑名单后这条路径消失。
  > 这一条改完、单文件编译通过，**需要下次整体编译才生效**。

### 5.3 编译

```bat
:: "x64 Native Tools Command Prompt for VS 2022"
cd /d "C:\Program Files\GraphicsDebugger-Endfield"
msbuild renderdoc.sln /p:Configuration=Development /p:Platform=x64 /m
```

本机 `MSBuild.exe` 被安全策略拦，AI 跑不了，只能手动执行。
`TinecmaTool.dll` 和 `qTinecmaTool.exe` 都在这一份 solution 里，一次编完。

---

## 6. 排障

| 现象 | 原因 | 处置 |
|---|---|---|
| 一键脚本报 `Failed to create & inject` | 权限不足 / 目标拒绝以管理员启动 | 先确认 UAC 点的是「是」；用 `-NoAdmin` 对比一次 |
| 一键脚本跑完启动器窗口没出现 | `Games.exe` 已经由外层 `Launcher.exe` 拉起过 | 关掉全部启动器，重跑脚本（第 4 步会清理残留） |
| 注入成功但点开始游戏后没反应 | 老 `Games.exe` 在我们注入前就在跑，进程内没有 hook | 同上，必须让脚本自己拉起 |
| Launch 后无反应 | `TCM_FORCE_CHILD_INJECT` 没设，且没勾 Capture Child Processes | 用 `launch_endfield.bat` 起工具（它会设该变量）；一键脚本则已带 `--opt-hook-children` |
| 有 `Injected ... APC` 但没有 `Loading into` | 构建里 `add_hooks()` 被加了进程名过滤 | 删掉那个过滤，注入依赖无条件 `Initialise()` |
| 有 `Loading into Games.exe` 但没有 Endfield | 没勾 **Capture Child Processes** / 少了 `--opt-hook-children` | 勾上或加参数，重启一次 |
| 有 `Loading into Games.exe`，点开始游戏后崩 | QtWebEngine 被误注入 | 把 `qtwebengineprocess` 加进黑名单（§5.2） |
| **登录器窗口整块变黑** | relay 进程被注册了图形 hook（GLES 克隆 ANGLE context 失败） | 确保走 §5.1 的 relay 分支 |
| Endfield 有 `Loading into` 但没 `RegisterSurface` | `HGCreateSwapchain` 没走标准 WSI | 看日志里有没有 `Initialised capture layer in Vulkan instance` |
| 游戏弹 `10100` | 不是从启动器启动的 | 必须让 `Games.exe` 拉起，别直启 `Endfield.exe` |
| Attach 列表里没有 Endfield | 端口没起 | `netstat -ano \| findstr "3892"` 看归属 |
| 双击 rdc 打不开 | 有人把 json 改成 unlocked 了 | `launch_endfield.bat -Off` |
| 别的 TinecmaTool 一开就崩 | 同上 | 同上 |

日志位置：`%TEMP%\TinecmaTool\RenderDoc_<日期>_<时间>.log`

### 6.1 ACE 封锁下只能看端口和日志

`Endfield.exe` 被 ACE 的 `ObRegisterCallbacks` 锁死，即使提权到 `SeDebugPrivilege`：

```
ReadProcessMemory       恒 err=5
NtReadVirtualMemory     恒 0xC0000022
Module 枚举             恒 0 条
```

**不要浪费时间尝试读游戏进程内存或模块列表，只能靠端口 + 日志判断。**

---

## 7. 已废弃方案（防止走回头路）

### 7.1 全局 unlocked layer（已废弃）

做法：删掉 json 的 `enable_environment`，让 implicit layer 无条件加载。

**能通，但代价不可接受**：

- 本机每个新起的 Vulkan 程序都加载 100 MB `TinecmaTool.dll`
- qTinecmaTool 自己也会被自己的 layer 注入，建 replay device 时
  在 `vk_device_funcs.cpp:842` 撞 `Assertion failed: 'IsCaptureMode(m_State)'`，**一开就崩**
- 另外两套 TinecmaTool（鸣潮 / DF研究）同样崩
- **任何 .rdc 都打不开**（打开 rdc 本身要建 Vulkan replay device）
- 每次抓完必须手动跑恢复脚本

后三条在实机上确认过，这是方案 3 存在的直接原因。

### 7.2 进程名过滤（降级为兜底，保留代码）

`IsLayerCaptureTarget()` 让 layer 在非目标进程里 `vkCreateInstance` 直接透传。

**它和方案 3 有一个必须记住的冲突**：这个过滤不能放进 `win32_libentry.cpp`
的 `add_hooks()`。因为方案 3 必须能注入 `Games.exe`，而 `Games.exe` 不是
`endfield.exe`——一旦在 `add_hooks()` 里拦掉，注入进去的 DLL 完全静默，
`CreateProcess` hook 不成立，链路断在第一步。实测撞过这个坑。

现在的分工：

- `add_hooks()`：**不做**进程过滤，无条件 `Initialise()`（注入链的基础）
- `vkNegotiateLoaderLayerInterfaceVersion`：非目标让出（防止 layer 污染无关进程）

因为 json 是 locked 的，无关进程本来就不会加载这个 DLL，这一层过滤只是保险。

### 7.3 环境变量继承（已证明不可行）

实测 `Games.exe` 的 PEB 环境块里能看到 `ENABLE_VULKAN_TINECMATOOL_CAPTURE=1`
（说明 bat 的 `set` 生效了），但 `Endfield.exe` 读不到——鹰角启动器传自己的环境块。
这条路彻底堵死，不要再试。

---

## 8. 多工具共存

本机有三套 TinecmaTool 产物：

| 树 | 用途 | commit |
|---|---|---|
| `GraphicsDebugger-Endfield` | **本方案**，终末地 | `e11360084` |
| `GraphicsDebugger-Wuthering-Waves` | 鸣潮 | `1c8ceb77e` |
| `GraphicsDebugger-DF研究` | 三角洲行动 | `ae29f3a7e` |

| 资源 | 会不会打架 | 处理 |
|---|---|---|
| layer 名 `VK_LAYER_TINECMATOOL_Capture` | **会**（全局唯一） | 注册表只能一条，bat 每次收敛成本树 → **三树互斥** |
| `ENABLE_VULKAN_TINECMATOOL_CAPTURE` | 会（同名） | 方案 3 不需要全局设它；变量只由注入在目标进程内设置 |
| `DISABLE_VULKAN_TINECMATOOL_CAPTURE_1_44` | 会（共享 kill switch） | 本树残留了一处同名引用，实际不靠它工作 |
| `qTinecmaTool.exe` 进程名 | 会 | 不用 `taskkill /im`，按 `$_.Path` 只杀本树 |

**铁律：所有环境变量只用 `set`（进程级），永不 `setx`。** 写进 User 会污染全机。

**互斥是硬限制**：切到鸣潮那边抓帧，本树的 layer 就失效，反之亦然。
切回来重跑一次 `launch_games.bat` 即可（UAC 弹一次）。

方案 3 的附带好处：因为 json 是 locked，**本树不再影响另外两棵树能不能打开**。
它们各自的工具和 rdc 都能正常用，只是抓终末地时要先让注册表归本树。

---

## 9. 待办

- [ ] **重新编译一次**，让 §5.2 的 `qtwebengineprocess` 黑名单和 §5.1 的 relay 分支同时生效
      （当前构建里都还没有：前者会让第一次启动崩一次，后者会让登录器黑屏）
- [ ] 抓一帧完整验证：rdc 里 present 帧是否完整、DLSS / Streamline 的 pass 能否降级回放
- [ ] 观察 `Endfield.exe` 注入后长时间运行是否稳定（目前只验证了启动 + 进画面）
- [ ] 若要抓别的游戏：设 `TCM_LAYER_TARGET=<exe名>` 可让 layer 换目标，不用重编

---

## 附录：环境事实

```
引擎        Unity 2021.3.34f5 + IL2CPP（GameAssembly.dll 320 MB）
渲染        Vulkan，UnityPlayer.dll 引用系统 vulkan-1.dll
自定义 WSI  HGSetupCustomVulkan / HGCreateSwapchain / GfxHGPresentWaitThread
            日志首行 "Could not load symbol HGSetupCustomVulkan" 是非致命 Error
启动链      Launcher.exe（= 1.6.0\Launcher.exe 同文件）
              → 1.6.0\Games.exe（Qt5 + WebEngine）
                → games\Endfield Game\Endfield.exe
                  → PlatformProcess.exe（SDK 旁路，不要注入）
反作弊      AntiCheatExpert\ACE-BASE.sys / ACE-CORE.sys / ACE-Service64.exe
GPU         RTX 4090（device 0）+ AMD 核显（device 1，会被 clamp 1.4→1.2）
日志        C:\Users\<user>\AppData\LocalLow\Hypergryph\Endfield\Player.log
            C:\Users\<user>\AppData\Local\Temp\TinecmaTool\RenderDoc_*.log
```
