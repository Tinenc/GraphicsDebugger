# S4 · 目标身份修正与真实加载顺序（ETW 实测）

- 分支：`Steam-Cod`，方法：内核 ETW（`Microsoft-Windows-Kernel-Process`，keyword `0x40` / ImageLoad）
- 全程未注入，未打开目标进程句柄读内存

---

## 0 最重要的一条

**抓帧目标不是 `cod.exe`。**

`cod.exe` 只是 Call of Duty HQ 的启动壳：它加载 `dxgi.dll`（+0.51s）做能力探测，**从头到尾不加载 `d3d12.dll`**。

真正渲染的是：

```
F:\SteamLibrary\steamapps\common\Call of Duty HQ\sp26\sp26-cod.exe
```

| 项 | `cod.exe`（HQ 壳） | `sp26-cod.exe`（真实游戏） |
|---|---|---|
| PID（本次运行） | 126448 | **165580** |
| 启动时间 | 23:10:08.753 | 23:09:03.160 |
| 模块总数 | 82 | **207** |
| `dxgi.dll` | +0.511s | +0.506s |
| `d3d12.dll` | **从未加载** | **+40.718s** |
| `D3D12Core.dll` | 从未加载 | **+40.723s** |
| 其他 | — | `DXGIDebug.dll` +43.164s、`GFSDK_Aftermath`、`amd_ags`、`AzureAttest*` |
| 记录终止 | +1.74s | +99.1s |

## 1 但两者 PE 结构同构（之前的图形分析仍然有效）

| | `cod.exe` | `sp26-cod.exe` |
|---|---|---|
| 大小 | 220.79 MB | 218.73 MB |
| 静态导入表 | **0 dll** | **0 dll** |
| 延迟导入 | `d3d12.dll`（`#101` + GetInterface + 2×Serialize）、`thx_sdk.dll` ×11 | **完全相同** |
| 导出目录自报名 | `game_steam_ship.exe` | `game_steam_ship.exe` |
| 导出数 | 116（含 `D3D12SDKPath` / `D3D12SDKVersion`） | 116，相同 |
| 节区 | 9（含尾部 WX `.text`） | 9，相同 |
| 签名 | Activision EV | Activision EV |

结论：`docs/STEAM_COD_RECON.md` 里关于「延迟导入 `d3d12.dll!ordinal:101` = `D3D12CreateDevice`」的结论**直接适用于 `sp26-cod.exe`**，不需要重做。

## 2 加载时序的两个要点

**① `dxgi.dll` 比 `d3d12.dll` 早 40 秒**

`sp26-cod.exe` 启动 0.5 秒就加载 `dxgi.dll`，但 40 秒后才加载 `d3d12.dll`。这段间隔是资源初始化/校验。
含义：注入若发生在 `dxgi.dll` 加载之后、`d3d12.dll` 加载之前，hook 仍然能挂上；但**越早越稳**，
否则会错过 `dxgi` 侧的 `CreateDXGIFactory` 调用。

**② `DXGIDebug.dll` 被加载**

+43.2s 出现 `DXGIDebug.dll`。正常发布版游戏不会加载它 —— 说明游戏检测到了 debug 环境
（例如本机装了 SDK 的 Graphics Tools，或注册表里 DebugLayer 开关为开）。这一条对抓帧是**有利**的，
但对反作弊是敏感信号，值得单独确认。

## 3 R1 第一手实证：外部读不到模块表

在游戏运行时（PID 182108，另一次运行）尝试常规只读手段：

```
OpenProcess(QUERY_INFORMATION|VM_READ)   handle=612  OK          <- 句柄能拿到
EnumProcessModulesEx(LIST_MODULES_ALL)   ok=0  need=0  err=5     <- ERROR_ACCESS_DENIED
OpenProcess(ALL 0x1FFFFF)                handle=612  OK
EnumProcessModulesEx                     ok=0  need=0  err=5
```

句柄存在，但模块枚举被拒。这是 Ricochet 在剥离句柄权限 —— **任务管理器级别的只读观察都做不到**。

所以本次改用 ETW：内核侧产生事件，不需要碰目标进程。这也是唯一可行的外部观察通道。

## 4 干扰项（记录一下，免得误判）

同一 trace 里出现的其他进程，都不是游戏：

| PID | 进程 | 说明 |
|---|---|---|
| 61380 / 46692 / 19828 | `dxdiag.exe` | 三次，分别加载了 D3D12/D3D12Core —— 别把这些当成游戏 |
| 181656 | `UnrealEditor-Cmd.exe` | 另一个项目在跑 |
| 114096 | `UnrealEditor.exe` | 同上，1554 个模块 |

如果只按「谁加载了 d3d12.dll」筛，会全筛错。正确筛法是按**镜像路径**过滤（`...\Call of Duty HQ\sp26\sp26-cod.exe`）。

## 5 对后续动作的影响

1. **注入目标改为 `sp26-cod.exe`**，`cod.exe` 只需作为启动链上的一环存在
2. 时机：`dxgi.dll` 之后、`d3d12.dll` 之前（+0.5s ～ +40s 之间都有窗口，但早期更安全）
3. 单人战役（SP26）已实测；多人包 `cod26\` 目录当前没有 exe（未安装），若以后测 MP 需重新确认目标名
4. `DXGIDebug.dll` 的存在需要在注入前确认来源，避免它和我们的 hook 抢 debug layer

## 6 复现

```bat
:: 采集（管理员）
logman start codrun -p "Microsoft-Windows-Kernel-Process" 0x40 5 -o "%~dp0cod-run.etl" -ets
:: ... 启动游戏 ...
logman stop codrun -ets
tracerpt cod-run.etl -o cod-run.xml -of XML -y
python parse_etw_images.py cod-run.xml cod-modules.csv
```

`cod-run.xml` 81 MB / 26,331 个 ImageLoad 事件。踩过的坑：`tracerpt` 的 CSV 输出对变长 payload 会错列，
必须走 XML；XML 带 `http://schemas.microsoft.com/win/2004/08/events/event` 命名空间；
时间戳是 9 位纳秒小数（`2026-10-25T23:10:08.753777500+07:59`），`strptime` 的 `%f` 只吃 6 位，要用 `fromisoformat`。

## 7 状态

- [x] 确认真实渲染进程是 `sp26-cod.exe`，不是 `cod.exe`
- [x] 确认两者 PE 同构，图形入口结论沿用
- [x] 拿到真实加载顺序（dxgi 早 40 秒，d3d12 后到）
- [x] 确认外部模块枚举被 Ricochet 拒绝（err=5），ETW 是唯一外部通道
- [ ] `DXGIDebug.dll` 来源待查
- [ ] 注入尚未尝试（R1 未评估完）
