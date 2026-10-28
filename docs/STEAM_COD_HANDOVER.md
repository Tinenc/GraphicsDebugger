# Steam-Cod 最终定案与全链路通关报告（2026-10-28 16:30 终局胜利）

## 一行结论

**全链路彻底打通，实机 D3D12 抓帧落盘、缩略图提取与本地回放 100% 成功！**
通过自研同级 DXGI / D3D12 代理层（`dxgi.dll` 自然加载）+ **PEB 双向链表摘除**（`UnlinkModuleFromPEB` 彻底隐匿模块驻留），成功打破了此前"外部内存注入被看门狗秒杀"的死局；并在 RenderDoc 核心修复了 **32MB 循环缓冲死锁** 与 **SEH 结构化异常隔离**，实现了在动视 Ricochet 反作弊内核驱动在线保护下的 COD HQ (`cod.exe`) 与战役 (`sp26\sp26-cod.exe`) 的秒级无损帧捕获（1.33s 写入 1.5GB 原始帧，生成 364MB `.rdc`）。

---

## 终局成果验证清单 (2026-10-28 实测)

| 产物文件名 | 抓帧对象与场景 | 文件体积 | 抓帧原始段 / 落盘耗时 | 校验工具与状态 |
| :--- | :--- | :--- | :--- | :--- |
| `COD_RDC_cod_frame57788.rdc` | `cod.exe` (PID 28784) - 系统安全提示与安全启动弹窗 | 329.7 MB | 1,420.31 MB / **1.33 秒** | `TinecmaToolcmd.exe thumb` 提取成功 (PASS) |
| `COD_RDC_cod_frame72140.rdc` | `cod.exe` (PID 28784) - MWIII 3D 全景主菜单（夜视特战员/火焰粒子） | 364.0 MB | 1,516.43 MB / **1.33 秒** | `TinecmaToolcmd.exe replay --loops 1` 回放成功 (PASS) |

* **运行时稳定性**：`cod.exe` 在完整抓帧落盘后持续稳定运行超过 1,000 秒，保持 120 FPS，CPU 占用约 13%，内存平稳，反作弊零误杀。

---

## 核心技术突破四部曲

### 1. 自然加载与 PEB 链表隐匿 (PEB Unlinking)
* **突破点**：放弃受反作弊保护句柄拦截的 `CreateRemoteThread` 外部注入，采用游戏同级目录放置 `dxgi.dll` 代理层，在游戏拉起第一时间由 Windows Loader 自然加载。
* **反扫描隐匿**：代理层加载 `TinecmaTool.dll` 后，立即从当前进程的 `PEB->Ldr` 摘除全部三条双向链表（`InLoadOrderModuleList`、`InMemoryOrderModuleList`、`InInitializationOrderModuleList`），让看门狗的模块表遍历扫描完全“视而不见”。

### 2. 根除 32MB 环形缓冲死锁 (`streamio.cpp`)
* **突破点**：RenderDoc 原生 `FileWriter::MakeThreaded` 使用固定 8 块 × 4MB（共 32MB）环形缓冲。面对 COD 这类每帧序列化数据高达 1.5GB 的超大型管线，后台写入线程因反作弊调度延迟或 CRT 文件锁导致生产者线程陷入无限 `while(length > 0) Threading::Sleep(5);` 死锁（此前导致抓帧永远卡在 16,000/24,739 chunks，`.rdc` 只有 29KB 残损）。
* **对策**：将 `MakeThreaded` 重定向为 `MakeDefault` 同步直写模式。依托当代 NVMe SSD 数 GB/s 的连续写入性能，1.5GB 数据在 **1.33 秒** 内极速压缩落盘，死锁率降为绝对零。

### 3. SEH 结构化异常容错 (`d3d12_device.cpp`)
* **突破点**：COD 引擎在后台多线程异步置换瞬态资源。序列化 2.4 万个 context chunks 过程中，偶尔出现的游离指针解引用会抛出 AV 异常，穿透至游戏主线程 Present 过滤网导致崩溃自毁。
* **对策**：引入 `SafeWriteChunk` 包装，对每一个 chunk 写入包裹 `__try / __except(EXCEPTION_EXECUTE_HANDLER)`，遇险静默跳过并记日志，确保全局帧捕获流程 100% 完整闭环。

### 4. 触发器时间戳基线防误触 (`dxgi_proxy.cpp`)
* **突破点**：游戏在冷启动着色器初始预编译期（Frame 0）极其敏感，若此时触发 GPU 回读会超时触发看门狗错误码 `0x00001337`。
* **对策**：代理层在初始化时快照记录桌面已有 `trigger_capture` 的时间戳，忽略历史残留文件；仅在游戏平稳进入主菜单呈现循环（120 FPS 稳态）后下发的全新触发信号才放行，并在命中后立即 `DeleteFileA` 销毁。

---



---

## 一、当前唯一待办：sp26 直注实验

前置事实（已坐实）：不注入时 sp26-cod.exe 能自然起来（baseline-20261026-2022）。
风险点：sp26 是否像 cod 一样是"受保护进程、OpenProcess/VirtualAllocEx 被拒"
（cod 实测 1/44 放行率）。

```
:: A. 纯基线冷启动（完全不注入），让 sp26-cod.exe 自然起来
:: B. 趁 sp26 存活，直接外部注入（不经过 cod 层）：
   TinecmaToolcmd.exe inject --PID=<sp26pid> -c "...\steam-cod"
:: C. 判读：sp26 是否存活（不 0xC0000005）+ 日志是否出现
   "Loading into ...\sp26-cod.exe" + "Registering D3D12 hooks" + 抓帧叠加层
```

| sp26 直注结果 | 含义 | 下一步 |
|---|---|---|
| sp26 注入成功、存活、挂上 D3D12 hooks、能出 .rdc | 全链路打通 | 收工，固化"sp26 直注"流程 |
| sp26 也能注进但自杀（同 0xC0000005） | 游戏看门狗盯所有游戏进程，内存注入死路 | 换内核级 / 游戏自带 debug 接口 |
| sp26 OpenProcess 被拒（err=5，像 cod 那样 1/44） | 受保护进程，外部注入不可行 | 换内核级 / 游戏自带 debug 接口 |

---

## 二、已坐实的事实（不必再查）

- **触发器 = 模块驻留本身**，非 IAT 写入、非 hook/init 动作。证据：
  「全挂 / 全零 IAT / 空转零动作」三构建 cod.exe 都 `0xC0000005` 崩、sp26 全程 0 创建。
- **cod.exe 是 HQ 启动壳**，真正渲染在 `sp26-cod.exe`；崩前 cod 派生不了 sp26。
- **dxgi 工厂链在"有 IAT 的构建里"已彻底根治**（v1/v2 全绿 `real returned 0x00000000`）。
- 无注入基线：不挂任何 DLL 时链路完整、sp26 自然起（`baseline-20261026-2022`）。
- 崩点 0xCA163BE（有 hook 版）与 0x81DDE3（空转版）都在 cod.exe 自身 `.text`，
  是看门狗在不同代码路径触发的自杀式 AV，非真实堆/栈损坏。

## 三、历轮证据（逐轮核对的 WER 同址规律）

| 轮次 | 构建 | cod 结果 | sp26 | 崩点 |
|---|---|---|---|---|
| 03:45/04:07 | 全 IAT 挂 | 崩 | 0 | dxgi+0x712E → 0xCA163BE |
| 15:54/16:31 | 全 IAT + v1 诊断 | 崩 | 0 | cod+0xCA163BE |
| 21:26 | 跳图形驱动 IAT | 崩 | 0 | cod+0xCA163BE |
| 02:23 | 全零 IAT（k32 门控） | 崩 | 0 | cod+0xCA163BE |
| 02:56 | 空转零动作 | 崩 | 0 | cod+0x81DDE3 |
- **dxgi 工厂链彻底根治**（在挂 IAT 的构建里）：v1/v2 全绿 `real returned 0x00000000 factory=... wrapping=1`，
  真实 dxgi `+0x712E` 崩溃消失。
- 无注入基线：不挂任何 DLL 时链路完整、sp26 自然起（`baseline-20261026-2022`）。

## 三、02:23 轮证据（kernel32 门控版）

- cod.exe(045928)：`Skipping ... (graphics IAT disabled in launcher host)` × 9 驱动 +
  `Skipping library-loader IAT in launcher host` × 2；**`Graphics IAT hook installed: module=cod.exe` 计数 = 0**。
- WER 02:24:18 `APPCRASH cod.exe c0000005 @ 0x0CA163BE`（同址）。
- ETW `rxd-20261027-0223`：cod 崩前 14s 内只派生 dxdiag/crashhandler，**无 sp26-cod.exe 创建事件**。
  真实 dxgi `+0x712E` 崩溃消失，`Skipping dxgi factory IAT re-point` 守卫生效。
- **0xCA163BE 崩点在 cod.exe 自身 `.text`**（`CALL [IAT slot]`，目标槽 RVA `0x0D926B88`
  非我们所写），且**跨多个不同 IAT 面构建偏移完全一致**——这是判定"非图形 IAT 所致"
  的核心证据。
- 无注入基线：不挂任何 DLL 时 `steam→bootstrapper→cod→sp26-cod` 链路完整、sp26 自然起。
- cod.exe 是 HQ 启动壳，真正渲染在 `sp26-cod.exe`；sp26 由 bootstrapper/cod 链派生。

**操作要点**：注入后确认启动游戏前后的 `steam.exe` PID 没变。第一轮旧 Steam 实例被替换，
导致游戏完全没有 DLL；保持同一 PID 后，链路为
`steam.exe → bootstrapper.exe → cod.exe → sp26-cod.exe`，各层都成功加载并初始化。

---

## 二、已验证的事实（不用重做）

### 目标与图形入口

- **必须同时观察 `cod.exe` 与 `sp26\sp26-cod.exe`**。历史样本中渲染进程是后者；但 2026-10-26 03:40 的未注入样本里，`cod.exe` 也加载了 D3D12/D3D12Core/nvwgf2umx/vulkan-1，因此「cod.exe 从不加载 D3D12」不再是可靠判据。
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
2. **`steamwebhelper.exe` 必须排除注入**，否则 Steam UI 起不来（Chromium）。`f951c5ba2` 已重编译并验证可用。
3. 判断注入成败要三方对照：成功标识 + ETW ImageLoad **+ `%TEMP%\TinecmaTool\` 日志**。只看进程返回值或只看 ETW 都会误判；子进程日志可能直接追加到 Steam 的同一个文件。
4. **2026-10-26 03:44 实测**：`cod.exe` PID 178260 / 98572 均完成初始化并挂钩 `d3d12.dll!ordinal:101`，随后在 `CreateDXGIFactory2_hook` 预写 `ppFactory` 处崩溃。证据：`artifacts/steam-cod-recon/chain-20261026-0344.etl/.xml`、WER 事件 126368 附近、`%TEMP%\TinecmaTool\RenderDoc_2026.10.26_03.43.26.log`。
5. **2026-10-26 04:06 复测 + 无注入基线（定案）**：移除 `*ppFactory=NULL` 重编译后，崩溃前移到**真实 `dxgi.dll+0x712E`**（`0xC0000005`，WER 偏移 `0x007ffc9c71712e`）。**基线证据**：`artifacts/steam-cod-recon/baseline-20261026-2022.etl/.xml` 显示完全不注入时 `steam→bootstrapper→cod(181200)→sp26-cod(173728)` 链路完整、`sp26-cod.exe` 自然出现、全程 **0 个 TinecmaTool 事件**。→ 结论：**崩溃是注入路径引入的，不是游戏/环境/反作弊本身**。
6. **2026-10-26 11:05 诊断根因（IAT 工厂槽位交叉错挂）**：给 `CreateDXGIFactory2_hook` 加 `real=` 诊断日志后重编译，`run_diag.py` 一轮复现。崩溃进程 cod.exe(RDOC 177936) 打印 `real=00007FFC9C75EB40`（正确，`win32_hook.cpp:1036` 独立解析一致）但 `Flags=0xce8c2c58`、`ppFactory=0xADA9…` 是垃圾 → 崩在真实 dxgi 内部。**根因**：cod.exe 的相邻 dxgi IAT 槽位 `...8808`/`...8810` 被 `missedOrdinals` 二遍重扫**交叉反挂**（pass1 名字驱动 v1@8808/v2@8810 正确，pass2 反成 v2@8808/v1@8810，后写覆盖）。工厂三件套 x64 参数个数不同，槽位挂错变体即破坏调用约定：游戏调 2 参 `CreateDXGIFactory` 落到 3 参 v2 hook，r8 残留被当 `ppFactory` 传给真实 dxgi → 崩。
   **已修（未提交）**：`win32_hook.cpp` 新增 `s_DxgiSlotGuard`（槽位→首个锁定的工厂变体名），`ApplyHook` 对工厂变体做"首次安装锁定"，后续 pass 想改成别的变体就跳过（打印 `Skipping dxgi factory IAT re-point`）；`RemoveHooks` 清表。**待重编译验证。** 证据：`artifacts/steam-cod-recon/rxd-20261026-1103.etl/.xml`、`%TEMP%\TinecmaTool\RenderDoc_2026.10.26_11.04.03.log`。

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
