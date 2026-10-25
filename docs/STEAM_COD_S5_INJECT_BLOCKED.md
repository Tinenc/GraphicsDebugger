# S5 · 运行中注入被反作弊阻断（实测定案）

- 分支：`Steam-Cod`，工具：`x64\Development\TinecmaTool`（02:20 含诊断修复）
- 方式：`TinecmaToolcmd inject --PID=<sp26-cod.exe>`，同时开 ETW 监控 ImageLoad

---

## 0 一句话

**`VirtualAllocEx` 被拒（`GetLastError() = 5`，ERROR_ACCESS_DENIED）。** 注入在第一步就断了，
不是验证问题、不是可见性问题、也不是工具拼路径的问题。Ricochet 在剥离目标句柄的
`PROCESS_VM_OPERATION` 权限，`VirtualAllocEx` 之外的三步（`WriteProcessMemory` /
`CreateRemoteThread` / 模块枚举）也全都不可用。

## 1 硬证据（修复后的日志）

```
win32_process.cpp(635) - Log   - Injecting renderdoc into process 138108
win32_process.cpp(318) - Error - Couldn't allocate remote memory for DLL
                                 'C:\...\x64\Development\TinecmaTool.dll': 5
win32_process.cpp(339) - Warning - CreateToolhelp32Snapshot(138108) -> 0x00000005
win32_process.cpp(1016) - Error - Remote LoadLibraryW failed ... not a visibility problem.
```

ETW 侧独立确认（内核事件，不需要目标句柄）：

```
pid 138108 loaded 81 modules in this window
graphics modules seen: dxgi.dll
[RESULT] NOT INJECTED - no TinecmaTool.dll in the ImageLoad stream
```

两条独立通道（用户态 API + 内核 ETW）给出同一个结论，没有含糊空间。

## 2 修复到底改了什么（`a5a50cc51`）

修复前，这件事**不可能被诊断出来**：

- `InjectDLL` 丢弃了远程 `LoadLibraryW` 的返回值
- 成功判据只有 `FindRemoteDLL`（`TH32CS_SNAPMODULE` 枚举）
- 枚举被拒 → 无论注入真假，都只报同一句
  `Failed to inject ... did not crash or exit early ... working directory is incorrectly set`
- `VirtualAllocEx` 的失败**只进 `RDCERR`，不进日志文件**，等于完全静默

修复后：

| 改动 | 作用 |
|---|---|
| `InjectDLL` 返回 `HMODULE`（远程线程退出码） | 区分「DLL 加载失败」与「加载成功但看不到」 |
| 三步分别记录 `GetLastError` | 直接指出是哪一步被拒（这次就是 `VirtualAllocEx: 5`） |
| 路径超 `MAX_PATH` 提前拒绝 | 避免 `wcscpy_s` abort（疑似 `0xC0000409` 的来源） |
| 调用处三分支 | 真失败 / 保护进程 / 正常，各有独立文案 |

**收益：从「注入失败，原因不明」变成「VirtualAllocEx 被拒，err=5」。** 这是本次修复的全部价值 ——
它没有让注入成功，而是让「不可能」这件事有了证据。

## 3 三条路，只剩一条

| 路径 | 状态 |
|---|---|
| `inject --PID`（运行中注入） | ❌ `VirtualAllocEx` 被拒 |
| `capture` 启动目标 exe | ❌ `cod.exe` 裸启动 2 秒内 `0xC0000409`，游戏必须由 Steam 创建 |
| **AppInit_DLLs + shim（进程创建时加载）** | ⬜ 未尝试，是唯一剩下的 |

前两条的共同点：都需要我们**主动往目标进程里塞东西**。第三条不同 —— DLL 是由 Windows
加载器在进程初始化阶段自己加载的，不涉及 `VirtualAllocEx`，反作弊的句柄权限剥离管不到。

## 4 全局钩子怎么开（项目自带，不用写程序）

1. 打开 `x64\Development\TinecmaToolui.exe`
2. Settings 里把 `allowGlobalHook` 置 true（默认 false，不置 true 界面里不显示该区域）
3. 捕获对话框 → Global Hook 区域，match 填 `sp26-cod.exe`
4. Start Global Hook
5. 从 Steam 正常启动游戏

机制：

- `TinecmaToolcmd globalhook --match=<子串>` 建立共享内存（`TinecmaToolGlobalHookData64`）
  后阻塞等 stdin
- `BackupAndChangeRegistry`（`win32_process.cpp:1571`）备份并改写
  `HKLM\...\Windows\AppInit_DLLs` + `LoadAppInit_DLLs`
- 每个新进程加载 `TinecmaToolshim64.dll`，shim 用 `FindStringOrdinal` 比对**完整 exe 路径子串**，
  命中才 `LoadLibraryW` 真正的 `TinecmaTool.dll` —— 所以只有 `sp26-cod.exe` 会被挂上
- match 长度必须 ≥ 4，否则 shim 直接判非法（防止误匹配全系统）

## 5 风险（必须先说清楚）

1. **AppInit_DLLs 是系统级的**。所有新进程都会加载 shim 做一次字符串匹配，开销极小但确实是全局的。
2. **反作弊可能直接盯这个注册表项**。Ricochet 有内核驱动，检测 AppInit_DLLs 是常规手段。
   这一步的风险我无法预判，只能试，且建议试完立刻 Stop Global Hook。
3. **别忘了还原**。`%TEMP%\TinecmaTool_RestoreGlobalHook.reg` 是清空用的备份，
   `Stop Global Hook` 会自动恢复；万一没恢复，双击那个 reg 也能回到干净状态。
4. 上一次这套机制留在系统里的痕迹已经确认被清过（当前 `AppInit_DLLs` 为空、`LoadAppInit_DLLs=0`）。

## 6 复现

```bat
:: 复验注入（会启动游戏，跑完自动分析）
python artifacts\steam-cod-recon\diag_inject.py --wait 200

:: 只看日志里的失败点
findstr /C:"Couldn't allocate remote memory" "%TEMP%\TinecmaTool\*.log"
```

## 7 状态

- [x] 运行中注入 = 不可能（`VirtualAllocEx` err=5，双通道印证）
- [x] 诊断链路修复并重新编译验证
- [x] `hq` 模式（capture 启动 cod.exe）确认不可行（`0xC0000409`）
- [ ] 全局钩子未尝试
- [ ] 残留：`TinecmaToolcmd` 注入失败后仍以 `0xC0000409` 退出（不影响错误输出，未处理）
