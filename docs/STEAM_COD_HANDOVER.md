# Steam-Cod 交接（2026-10-26 03:30）

## 一行结论

图形侧全部打通、目标进程已锁定；唯一障碍是反作弊不让 DLL 进渲染进程。
**突破口已找到并验证一半：注入 `steam.exe`（它没有反作弊）成功且初始化完整，
其 `CreateProcess` 钩子会在子进程创建瞬间注入。已补 `steamwebhelper` 黑名单，待重编译。**

---

## 一、当前唯一待办

```bat
:: 1. 重编译（只改了 sys_win32_hooks.cpp，增量很快）
cd /d "C:\Program Files\GraphicsDebugger-Steam-Cod"
msbuild renderdoc.sln /m /p:Configuration=Development /p:Platform=x64

:: 2. 冷启动 Steam（确保没有残留 steam.exe / steamwebhelper.exe）
:: 3. 等库界面出现后注入
tools\inject-steam.bat

:: 4. 从 Steam 启动游戏，ETW 同时盯（或用 setup_hook_chain.py 一步到位）
python artifacts\steam-cod-recon\setup_hook_chain.py
```

判据（缺一不可）：

| 检查 | 通过的样子 |
|---|---|
| 注入返回 | `rc=0` |
| 日志出现 | `%TEMP%\TinecmaTool\` 新增 log，含 `Loading into ...\steam.exe` + `Registering D3D12 hooks` |
| 游戏进程 | ETW 里 `cod.exe` 有 `TinecmaTool.dll` 的 **ImageLoad**，且**没有**紧随其后的 ImageUnload |
| 最终 | 游戏画面出现叠加层，按热键能出 `.rdc` |

---

## 二、已验证的事实（不用重做）

### 目标与图形入口

- **渲染进程是 `sp26\sp26-cod.exe`，不是 `cod.exe`**。`cod.exe` 只是 HQ 启动壳：加载 `dxgi.dll`（+0.5s）但**从不加载 `d3d12.dll`**。
- 两者 PE 同构（空导入表 / `d3d12.dll!ordinal:101` / 116 导出 / 自报名 `game_steam_ship.exe`），静态结论通用。
- `ordinal:101` = **`D3D12CreateDevice`**（系统 d3d12.dll 导出 base=99 核过）。
- 该路径 DF 树已验证过：`artifacts/iat-diagnostics-test/raw-delay-fixed.log` 有完整链路。
- `sp26-cod.exe` 加载时序：dxgi +0.51s → dwmapi +34.7s → **D3D12 +40.7s** → D3D12Core +40.7s → nvwgf2umx +41.7s → DXILConv +42.7s → DXGIDebug +43.2s → vulkan-1 +44.8s。

### factory 链路（实测通过，有 .rdc）

`docs/STEAM_COD_S2_FACTORY_BRIDGE.md`：四模式矩阵实跑，
`explicit-parent` 和 `hooked-null` 各产出 **3.44 MB `.rdc`**；
只有「原生 factory + wrapped device」组合会 `E_NOINTERFACE`。
（先前那版补丁假设 D3D12 设备可 QI 到 `IDXGIDevice` 是错的，已实测 `0x80004002` 并 revert。）

### 注入通道（逐条验死）

| 路径 | 结果 |
|---|---|
| `inject` 打 `sp26-cod.exe` | **`VirtualAllocEx` err=5**，始终被拒 |
| `inject` 打 `cod.exe` | 放行率 **1/44（2.3%）**，时刻不可预测 → 竞态不是时间窗 |
| `capture` 启动 `cod.exe` | 缺 Steam 票据，2 秒内 `0xC0000409` |
| AppInit_DLLs 全局钩子 | 注册表全对但**系统不执行** —— Windows 11 **26300 / 26H2** 已废弃该机制 |
| **`inject` 打 `steam.exe`** | ✅ **rc=0，loc 解析成功，`INTERNAL_*` 全执行，hooks 注册** |

`steam.exe` 是 **x64**（`machine=0x8664`），无任何反作弊，模块可枚举 —— 这是整条链上唯一能干净注入的进程。
**它重启时被旧实例注入后仍能完整初始化**（日志涨到 14 MB），证明 `hookIntoChildProcesses` 真的有效。

### 硬约束（踩过的坑）

1. **拿不到 `loc`（模块基址）就绝不能把 DLL 留在进程里** —— 只加载不初始化 = 空指针 = 游戏崩在 `TinecmaTool.dll`（`0xC0000005`）。已在 `0a1b059cf` 把这种情况改成报 `InjectionFailed`。
2. **`steamwebhelper.exe` 必须排除注入**，否则 Steam UI 起不来（Chromium）。已在 `f951c5ba2` 补进 `ShouldInject()` 黑名单，**待重编译**。
3. 判断注入成败要三方对照：rc + ETW ImageLoad **+ `%TEMP%\TinecmaTool\` 日志**。只看 rc 或只看 ETW 都会误判。

---

## 三、脚本清单

| 文件 | 用途 |
|---|---|
| `tools/inject-steam.bat` | 注入 Steam（`check` 只看目标） |
| `artifacts/steam-cod-recon/setup_hook_chain.py` | **推荐**：启动 Steam → 等 UI → 注入 → 校验日志 → arm ETW |
| `artifacts/steam-cod-recon/inject_steam.py` | 幂等注入（已注入则跳过） |
| `artifacts/steam-cod-recon/verify_hook.py` | 纯 ETW 观察某进程有没有加载我们的 DLL |
| `artifacts/steam-cod-recon/diag_inject.py` | 注入结果验证（含自动启动游戏） |
| `artifacts/steam-cod-recon/probe_va.py` | 测 `VirtualAllocEx` 放行率 |
| `artifacts/steam-cod-recon/parse_etw_images.py` | ETW XML → 按进程的模块时间线 |
| `artifacts/steam-cod-recon/module_timeline.py` | 轮询模块（对受保护进程无用，留作对照） |
| `artifacts/steam-cod-recon/globalhook_guard.py` | 无 GUI 驱动 AppInit 全局钩子（本机不可用，换机可复用） |

## 四、文档索引

- `docs/STEAM_COD_RECON.md` — 静态侦察（含目标修正提示）
- `docs/STEAM_COD_S2_FACTORY_BRIDGE.md` — factory 四模式实测
- `docs/STEAM_COD_S4_TARGET_AND_LOAD_ORDER.md` — 目标身份 + 加载顺序
- `docs/STEAM_COD_S5_INJECT_BLOCKED.md` — 注入被拒的定案
- `docs/STEAM_COD_S6_GLOBAL_HOOK_BLOCKED.md` — 全局钩子 + 放行率定量

## 五、如果这条路也不通

`steam.exe` 注入成功但 `cod.exe` 仍拒 → 说明 Ricochet 在进程创建回调里就完成了保护，没有可抢的时间点。
那时只剩：**换机**（Win10 / 早期 Win11）或**转静态分析**（`.xsub` 资源包、着色器包）。
