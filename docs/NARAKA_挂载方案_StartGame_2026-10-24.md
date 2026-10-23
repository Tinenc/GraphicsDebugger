# Naraka：以 StartGame.exe 为锚点挂载抓帧（方案 S）

> 状态：**待验证**（2026-10-24）
> 分支 `Naraka`，fork 自 `origin/DF研究 @1726b53b4`
> 目标：`F:\Naraka\program\StartGame.exe`（x64）→ 派生 `NarakaBladepoint.exe`（x64, VMProtect）

## 1. 为什么不是 LauncherGame.exe

`LauncherGame.exe` 是 .NET 引导壳，体内只有两条有意义的字符串：

```
\tool\LootHoarder.exe
--url=http://loot-hoarder/ui/l22html/main.html
```

它的行为是：起 Electron 界面 `LootHoarder.exe`，然后**自己立刻退出**。

实测证据（`RenderDoc_2026.10.24_00.58.11.log` / `_02.43.21.log`）：

```
[00:58:40] win32_process.cpp(610) - Injecting renderdoc into process 147460
[00:58:40] win32_libentry.cpp( 68) - Loading into F:\Naraka\LauncherGame.exe
[00:58:41] target_control.cpp(642) - Got remote handshake: LauncherGame [147460]
[00:58:41] win32_network.cpp(264) - Warning - recv peek failed: WSAECONNRESET
```

1 秒断连，事件日志里**没有任何崩溃记录** —— 它没崩，也不是 ACE 杀的，是设计如此。它 spawn 的 LootHoarder 及之后所有进程都没有我们的 DLL。

完整链路：

```
LauncherGame.exe (x86, 引导壳, 立即退出)
  └─ tool\LootHoarder.exe (Electron 界面)
       └─ program\StartGame.exe (x64, 校验票据 + 拉起反作弊)
            └─ program\NarakaBladepoint.exe (x64, VMProtect)
```

`LootHoarder.exe` 是 Electron/CEF，**不能对它开子进程注入** —— 2026-10-24 02:14 的全局钩子事故就是这一类进程造成的（注入 CEF 后 renderer 重启循环，每秒约 4200 次注入尝试、147 MB 日志）。它已被加入 `IsSkipInjectTarget()` 黑名单。

所以：**跳过前两级，直接以 `StartGame.exe` 为注入起点**。

## 2. 为什么这样能拿到设备创建前的时机

`StartGame.exe` 用 `CreateProcess` 派生游戏进程（体内 UTF-16 串：`NarakaBladepoint.exe -L -naraka&#@$$(%#$`）。开启 `--opt-hook-children` 后，我们的 `CreateProcessW` 钩子会在**子进程创建并被挂起时**完成注入，即游戏还没跑起来就有 DLL —— 这是唯一能覆盖 `D3D11CreateDevice` / `D3D12CreateDevice` 等设备创建的时机。

事后 attach 无效：设备已经在跑，RenderDoc 无从包装。

`StartGame.exe` 自身是 x64，所以不依赖 shim32；但**仍建议两个平台都建好**，避免误走启动器。

## 3. 前置条件（缺一不可）

| # | 条件 | 检查方式 | 不满足的后果 |
|---|---|---|---|
| 1 | x64 产物齐全 | `x64\Development\TinecmaToolcmd.exe` + `TinecmaToolshim64.dll` | 无法注入 |
| 2 | **用最新源码重编** | `sys_win32_hooks.cpp` 的 mtime 早于 `TinecmaTool.dll` | 黑名单改动不生效（含 CEF 防护） |
| 3 | 无残留工具进程 | `Get-Process qTinecmaTool` | 多实例抢同一份日志/共享内存 |
| 4 | `AppInit_DLLs` 为空且 `LoadAppInit_DLLs=0` | 脚本预检 | 全局钩子残留会污染所有新进程 |
| 5 | CC 录制未运行 | `Get-Process CCGameRecord` | 与我们的 DXGI hook 抢交换链，表现为无 `first present` |
| 6 | `GameInit` 票据存在 | `StreamingAssets\GameInit\GameInit`（实测 221 B, 2026-10-20） | StartGame 判「启动不合法」 |
| 7 | 期望后端已知 | `tempdx12.txt` / `ExtraVars.unityargs` | 判据用错（D3D11 与 D3D12 的日志行不同） |

## 4. 渲染后端：优先推到 D3D12

实测最近一次会话（`Player.log` 2026-10-24 02:44:50）仍是：

```
GfxDevice: creating device client; threaded=1
Direct3D:
    Version:  Direct3D 11.0 [level 11.1]
```

`F:\Naraka\program\ExtraVars.unityargs` 在 **02:48** 被写成 ` -force-d3d12`，但那次会话 02:44 就起来了，**这个改动还没被验证过**。

**为什么值得优先走 D3D12**：DF 的成功抓帧是 D3D12，为此做的两项改造（M3 接管延迟导入槽、M4 桥接原生 DXGI factory）都在本分支里。Naraka 如果走 D3D12，等于复用一条已跑通的路；而 D3D11 侧已实测出现结构性死结（见 §7）。

两条把游戏推到 D3D12 的路：

1. **优先**：在 `LootHoarder` 界面里找 DX12 开关（`StartGame.exe` 里有串 `Are you sure to use DirectX12 to run the game?`）。走这个开关，启动器会自己写 `tempdx12.txt` 并做 MD5↔注册表登记。
2. 次选：保留 `ExtraVars.unityargs` 里的 `-force-d3d12`，正常经启动器启动一次，然后看 `Player.log` 里是否出现 `Direct3D 12`。

> **红线**：不要手工伪造 `tempdx12.txt`。`StartGame.exe` 会做 MD5↔注册表比对，不等就删文件并标记 `treat as copied/forged`。这不是技术难点，是不能碰的线。

## 5. 执行

管理员 PowerShell：

```powershell
cd "C:\Program Files\GraphicsDebugger-Naraka\util\naraka"

# 先只预检（不启动游戏、零副作用）
.\Invoke-NarakaCapture.ps1 -PreflightOnly

# 确认无误后正式抓
.\Invoke-NarakaCapture.ps1
```

脚本等价的手工命令：

```bat
cd /d "C:\Program Files\GraphicsDebugger-Naraka\x64\Development"
TinecmaToolcmd.exe capture --opt-hook-children --opt-disallow-fullscreen ^
  -c "%LOCALAPPDATA%\Temp\Naraka\naraka" ^
  -d "F:\Naraka\program" ^
  "F:\Naraka\program\StartGame.exe"
```

人工动作只有三个：进游戏、窗口化、到可操作画面按 **F12**，然后退出。

## 6. 判据（五级，逐级收敛）

| 级 | 日志特征 | 未出现的含义 |
|---|---|---|
| 1 | `Loading into ...StartGame.exe` | 注入没生效，检查产物与权限 |
| 2 | `Loading into ...NarakaBladepoint.exe` | 子进程没被接管：`--opt-hook-children` 未生效，或 StartGame 没派生游戏 |
| 3 | `Registering D3D12 hooks` / `Registering D3D11 hooks` / `Registering DXGI hooks` | 图形 hook 未注册 |
| 4 | `created wrapped device.` | 设备没进包装 —— **本轮的核心判据** |
| 5 | `Graphics first present: swapchain=... window=...` | 交换链没接通，F12 抓不到 |

补充观察点：

- `Graphics call entered: api=D3D1xCreateDevice` —— 出现即证明钩子真的被调用（**这是 01:14 那次没出现的关键行**）
- `Graphics resolved delay hook installed` / `Graphics native factory bridge installed` —— D3D12 路径下 M3/M4 是否生效

## 7. 已知风险与止损

**D3D11 的结构性死结（已实测）**：01:14 直启游戏那次，钩子全部注册、`UnityPlayer.dll` 的 `dxgi!CreateDXGIFactory` 也打上了，但整场 `Graphics function resolved` 计数为 **0**，没有任何 `Graphics call entered`。原因是 `RegisterFunctionHook` 只改**导入表**（`hook.orig = GetProcAddress(...)`），而 VMProtect 在自己的解包阶段把 IAT 槽重写了 —— 我们的补丁在第一次调用之前就失效了。主镜像 `delayImportRVA=0x0` 也说明延迟导入表被抹掉，DF 的 M3 在 D3D11 侧无表可接。

因此在 D3D11 下，除非再做一层改造（导出存根 inline hook，或 VMP 解包后重打 IAT），否则到了第 3 级就会止步。

| 现象 | 判定 | 动作 |
|---|---|---|
| `Failed to inject` / `Couldn't get first module ... 0x12` | 被 ACE/Neac 拒绝，或进程早退 | **停**，记 `status: "unsupported-protected"` |
| 第 2 级过、第 3 级不过，进程秒退 | VMProtect 在 DLL 初始化阶段自毁 | **停**，记录时间戳 |
| 第 3 级过、无 `Graphics call entered` | VMP 重写 IAT（D3D11 已证实的死结） | **停**，不要再换注入姿势 |
| 第 4 级过、无 `first present` | CC 录制 / 帧生成抢交换链 | 关游戏内录制重试 1 次，仍无则停 |
| ACE 弹窗 / 「环境异常」 | 触碰保护边界 | **立即**退出，本次终止 |

同一失败特征最多重试 1 次。不做：脱壳、改节区/导入表、伪造 `tempdx12.txt`、停/改 ACE 服务、进程隐藏。

## 8. 与 DF 方案的差异

| 项 | DF（已跑通） | Naraka（本方案） |
|---|---|---|
| 后端 | D3D12 | 待确认，目标 D3D12 |
| 注入起点 | WeGame 启动器 | `StartGame.exe`（跳过引导壳与 Electron） |
| 注入机制 | 远程线程 + 子进程传播 | 同（本分支未引入 APC） |
| 保护 | ACE | Neac + ACE + VMProtect |
| 关键改造 | M3 延迟导入、M4 原生 factory 桥 | 已有；D3D11 侧需额外做导出存根 hook |
