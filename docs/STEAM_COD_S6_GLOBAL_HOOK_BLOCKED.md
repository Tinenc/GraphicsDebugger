# S6 · 三条注入路径全部封闭（全局钩子也不行）

- 分支：`Steam-Cod`，系统：Windows 11 build 26300 / 26H2
- 结论：**在保持反作弊不被绕过、不改游戏文件的前提下，本机没有可用的注入通道。**

---

## 0 三条路，逐条实测

| 路径 | 机制 | 结果 | 证据 |
|---|---|---|---|
| `inject --PID` | `VirtualAllocEx` + `CreateRemoteThread` | ❌ `err=5` ACCESS_DENIED | `Couldn't allocate remote memory ... : 5` |
| `capture <exe>` | 启动时注入；但游戏必须由 Steam 创建 | ❌ `cod.exe` 裸启 `0xC0000409` | 直启 2 秒退出；capture 报 `toolhelp 0x12b` |
| AppInit_DLLs + shim | 进程初始化时由加载器加载 | ❌ 注册表正确但**系统根本不加载** | 见下方实验 |

## 1 AppInit 实验：配置全对，机制不触发

**注册表写入完全正确**（`globalhook_guard.py start` 的结果）：

```
native  AppInit_DLLs        = 'C:\PROGRA~1\GR52BB~1\x64\DEVELO~1\TINECM~1.DLL'  (REG_SZ)
native  LoadAppInit_DLLs    = 1        (REG_DWORD)
native  RequireSignedAppInit_DLLs = 0  (REG_DWORD)
wow32   同上，指向 TinecmaToolshim32.dll
```

**短路径本身有效**：

```python
LoadLibrary(r'C:\PROGRA~1\GR52BB~1\x64\DEVELO~1\TINECM~1.DLL')  -> OK
```

**共享内存会话活着**：`TinecmaToolGlobalHookData64` 可 `OpenFileMapping`。

**但三个进程一个都没加载 shim**：

| 测试进程 | 模块数 | user32 已加载 | 有 shim |
|---|---|---|---|
| `python.exe`（控制台，不加载 user32） | 41 | 否 | 否（符合预期，AppInit 依赖 user32） |
| `mstsc.exe`（Win32 GUI） | 103 | **是** | **否** ← 决定性 |
| `sp26-cod.exe`（真实目标，ETW 观测） | 81 | — | **否** |

`mstsc.exe` 加载了 user32、注册表完全正确、DLL 可加载 —— shim 仍然没出现。
**说明 AppInit_DLLs 在本系统上不执行。**

## 2 根因

```
platform : Windows-11-10.0.26300-SP0
Display  : 26H2   Build 26300   UBR 9711
```

排除项：

- HVCI（内存完整性）`Enabled = 0`
- VBS `EnableVirtualizationBasedSecurity = 0`
- CI 策略 `VerifiedAndReputablePolicyState = 0`
- `RequireSignedAppInit_DLLs = 0`（未签名 shim 本应被允许）

排除后剩下的解释：**26H2 这一代已经不再执行 AppInit_DLLs**。微软长期在淘汰这个机制
（它本质是"任意进程加载任意 DLL"，与代码完整性方向冲突），到这里已经被关掉。

顺带记：本机 `sc.exe` 被 WorkBuddy 的 Program Blacklist 拦住，无法列内核驱动，
因此没能直接确认 Ricochet 驱动是否额外参与拦截 —— 但 AppInit 的失败发生在**系统加载器层**，
与反作弊无关（`mstsc.exe` 不带任何反作弊）。

## 2.5 定量：`cod.exe` 的内存分配放行率

`race_inject.py` 里 `cod.exe` 有一次性注入「成功」（ETW 能看到 DLL），为了判断那是时间窗
还是偶然，用 `probe_va.py` 在一次游戏运行里持续探测（只分配 1 字节再释放，不注入）：

```
每 2 秒一次，共 44 次采样
  +  66.7s  ALLOW             <- 唯一一次
  其余 43 次  denied at VirtualAllocEx  err=5
```

**放行率 1/44 ≈ 2.3%，且落在不可预测的时刻。** 这不是可用窗口，是竞态——反作弊驱动
某一刻的内部状态让权限短暂可见。结论：**不存在值得抢的注入时机**，自建注入器（用 shellcode
带回模块基址以完成 `INTERNAL_*` 初始化）也不会有稳定收益。

顺带说明 `race_inject` 那次「成功」为什么没用：DLL 虽被 `LoadLibraryW` 加载，但
`loc == 0`（模块枚举被拒）使 `INTERNAL_SetCaptureFile/SetDebugLogFile/SetCaptureOptions`
全部跳过，`Core::Initialise` 从未执行。结果是一个**惰性 DLL**，随后 `cod.exe` 直接崩在
`TinecmaTool.dll` 里：

```
错误代码: 0xC0000005 (0x00000000)
模块:     TinecmaTool.dll
可执行文件: cod.exe
```

**硬约束（已写进代码）**：拿不到 `loc` 就不能把 DLL 留在进程里——只加载不初始化等于给游戏埋一颗空指针。

## 3 那还剩什么

| 方案 | 可行性 | 风险 |
|---|---|---|
| **dxgi.dll 代理/劫持**（游戏目录放代理 DLL，转发到系统 dxgi 再加载 TinecmaTool.dll） | 技术上可行 | **高**：改游戏目录文件；反作弊校验 DLL 签名/完整性；可能直接判定作弊 |
| IFEO `Debugger` 重定向 | 可行 | 高：改变启动方式，反作弊必查 |
| 关闭/绕过 Ricochet | —— | **不做**，这是本项目一贯的边界 |
| 换抓帧工具（PIX / Nsight） | 同样需要注入，问题相同 | —— |
| 只做静态/离线分析（解析 .xsub 资源、着色器包） | 可行 | 低，但拿不到帧 |

按 `docs/DF_AUTHORIZED_CAPTURE_RESEARCH.md` 的边界，第 1、2 条都属于"改游戏文件/改启动链"，
本文不做推荐，只记录可行性。

## 4 本次沉淀的工具

| 文件 | 作用 |
|---|---|
| `globalhook_guard.py` | 无 GUI 驱动全局钩子：起 `globalhook` 会话、算 8.3 短路径、写/还原注册表、状态查询 |
| `verify_hook.py` | 纯 ETW 观察：等渲染进程、看 shim / TinecmaTool.dll 有没有进去 |
| `diag_inject.py` | 注入结果验证（同样走 ETW） |

`globalhook_guard.py` 的关键细节：`TinecmaToolcmd globalhook` 的 **`--capopts` 是必填项**，
缺了会 `need option: --capopts` 然后 `0xC0000409` 崩溃 —— **这也是 GUI 里 Start Global Hook
点了没反应的原因**（走的是同一条命令）。

## 5 收尾状态

- [x] 注册表已还原：`AppInit_DLLs=""`、`LoadAppInit_DLLs=0`（native + wow32）
- [x] 会话/共享内存已清理（`session alive: False`）
- [x] 原始状态备份留在 `globalhook-state.json`
- [ ] 三条路均不可行，未尝试文件级方案（需用户决策）
