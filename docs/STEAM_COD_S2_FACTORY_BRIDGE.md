# S2 · D3D12 factory 链路实测（原假设被证伪 + 修正结论）

- 分支：`Steam-Cod`，工具：`x64/Development/TinecmaTool`（22:31 构建）
- 触发原因：`cod.exe` 不导入 `dxgi.dll`，怀疑 swapchain 的 factory 走
  `device → IDXGIDevice → GetAdapter → GetParent`，从而绕过包装（Naraka 时期的 M4 疑虑）

**先给结论：那条路在 D3D12 上根本不存在。** 上一版为此写的补丁已回退。

---

## 0 一句话

| 问题 | 答案 |
|---|---|
| D3D12 设备能拿到 adapter 吗 | **不能**。原生 `ID3D12Device` 对 `IDXGIDevice` 和 `IDXGIObject` 都返回 `E_NOINTERFACE (0x80004002)` |
| 那 factory 只能从哪来 | `dxgi.dll` 的 `CreateDXGIFactory*`，或者调用方自己 `EnumAdapters` 后**传给** `D3D12CreateDevice` |
| 最坏情况是什么 | 不是「抓不到帧」，而是 **`CreateSwapChainForHwnd` 直接 `E_NOINTERFACE`，游戏崩** |
| 触发条件 | factory 是**原生**的，而传给它的 device/queue 是 **wrapped** 的 |

---

## 1 实测矩阵（四模式，全部真跑）

工具链：`TinecmaToolcmd capture -w -d <dir> -c <template> d3d12_null_adapter_factory.exe`
夹具入口与 `cod.exe` 同形：`D3D12CreateDevice` 按 **序号 101** 延迟导入。

| 模式 | adapter | factory 来源 | 结果 | 产物 |
|---|---|---|---|---|
| `explicit-parent` | 传入 | `adapter->GetParent()`（原生） | **captured=1** | 3,444,280 B `.rdc` |
| `hooked-null` | NULL | `CreateDXGIFactory2`（被 hook 包装） | **captured=1** | 3,444,308 B `.rdc` |
| `null-native` | NULL | `LdrGetProcedureAddress` 原生 | **`E_NOINTERFACE`** | 无 |
| `null-parent` | NULL | 同 `null-native`（device 侧拿不到 adapter） | **`E_NOINTERFACE`** | 无 |

`hooked-null` 的抓帧回放正常（`thumb` 出图，2.3 KB PNG），不是空壳。

## 2 三条关键日志

**设备入口被接管（delay import → GetProcAddress 被拦）**

```
Graphics function resolved: library=d3d12.dll function=D3D12CreateDevice real=... hook=...
```

`cod.exe` 的 `d3d12.dll!ordinal:101` 走的是 MSVC delay-load helper，helper 内部用 `GetProcAddress`
→ 被本树的 `Hooked_GetProcAddress` 拦下并改写成 hook。这条路对 COD **确定有效**。

**桥生效（传 adapter 时）**

```
Graphics native factory bridge installed: factory=... methods=1
Graphics native factory bridge installed: factory=... methods=4
Graphics swapchain wrapped: swapchain=... real=... device=... window=...
```

**报错出口只有一处**（两个 null 模式）：

```
FAIL factory->CreateSwapChainForHwnd(...) hr=0x80004002
```

原因很直白：原生 factory 不认识 wrapped 的 queue/device，直接拒绝。这就是当年 Naraka
记成「M4 工厂桥 E_NOINTERFACE」的确切机理 —— 不是桥写错了，是**原生 factory 与 wrapped 设备不能混用**。

## 3 对 `cod.exe` 的三种可能，以及怎么判

| cod.exe 的实际做法 | 结果 | 现场判据 |
|---|---|---|
| 自己 `EnumAdapters` 后把 adapter 传给 `D3D12CreateDevice` | 绿 | 日志有 `bridge installed` + `swapchain wrapped` |
| 传 `NULL`，dxgi 通过 `GetProcAddress` 解析（被 hook） | 绿 | 日志有 `Graphics function resolved: library=dxgi.dll` |
| 传 `NULL`，且用 `LdrGetProcedureAddress` 或自建 PE 解析器绕开 hook | **注入即崩**（swapchain `E_NOINTERFACE`） | 日志里 `CreateDXGIFactory` 无 resolved 记录，随后 `E_NOINTERFACE` |

第三种是唯一需要警惕的。`cod.exe` 静态导入表为空，说明它确实自建了解析层 —— 但**自建解析不等于会绕 Ldr**，
实际用哪条要跑起来才知道。这一条只能实测，静态判不了。

## 4 被证伪的东西（诚实记录）

上一版（提交 `5b1e7f7b4`）在 `d3d12_hooks.cpp:917` 加了「`pAdapter == NULL` 时从 device 反查 adapter」的分支。
**这段代码永远不可达**，已回退到原始两行。

错在把 D3D11 的经验带到了 D3D12：`ID3D11Device` 可以 QI 到 `IDXGIDevice`，`ID3D12Device` **明确不实现**
（实测 `0x80004002`，`probe_dxgi_device.cpp` 可复现）。

留下的教训：**D3D12 上任何「从设备找 adapter」的思路都不成立**，adapter 只有两个来源 ——
dxgi 的 `EnumAdapters`，或别人传进来。

## 5 复现

```bat
cd artifacts\steam-cod-recon
build-local.cmd                     :: 手搓 INCLUDE/LIB，绕开被拦的 vcvarsall
probe_dxgi_device.exe               :: 裸机探针：原生设备对 IDXGIDevice 的回答

set TINECMATOOL_CMD=..\..\x64\Development\TinecmaToolcmd.exe
run-null-adapter-factory.cmd explicit-parent
run-null-adapter-factory.cmd hooked-null
run-null-adapter-factory.cmd null-native
```

模式由环境变量 `GOP_FACTORY_MODE` 选定。夹具同时写 `d3d12_null_adapter_factory.log`
（printf 会丢，别只看 stdout）。

## 6 资产

| 文件 | 作用 |
|---|---|
| `probe_dxgi_device.cpp` | 裸机能力探针，不注入，证明 `ID3D12Device` → `IDXGIDevice` 不可用 |
| `d3d12_null_adapter_factory.cpp` | 四模式夹具，序号导入复刻 `cod.exe` 入口 |
| `build-local.cmd` | 本机可用的编译脚本（`vcvarsall` 会因 `reg.exe` 被拦而失败） |
| `{explicit-parent,hooked-null,null-native}.log` | 三份实测日志 |
| `hooked-null-thumb.png` | 抓帧回放证据 |

## 7 状态

- [x] 原假设「device → adapter → factory」被证伪
- [x] 补丁回退
- [x] 四模式矩阵实跑，`explicit-parent` / `hooked-null` 均 captured=1
- [x] 原生 factory + wrapped 设备 = `E_NOINTERFACE`，机理复现
- [ ] `cod.exe` 实际走哪条：待 S4（只读跑一次 + 看日志判据）
