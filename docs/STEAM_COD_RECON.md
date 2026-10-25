# Steam-Cod 树 · `cod.exe` 抓帧可行性侦察（第一次静态摸底）

- 分支：`Steam-Cod`，基线 `1726b53b4`（= origin/DF研究，0/0 对齐）
- 目标：`F:\SteamLibrary\steamapps\common\Call of Duty HQ\cod.exe`（Call of Duty HQ，cod26 资源集）
- 本报告全部为**只读静态解析**，未运行游戏、未注入、未改任何文件

---

## 0 一页结论

| 环节 | 判定 | 依据 |
|---|---|---|
| 图形 API | **D3D12 + 本地 Agility SDK** | 延迟导入 `d3d12.dll`；`cod.exe` 自身导出 `D3D12SDKPath` / `D3D12SDKVersion`；同目录 `D3D12\D3D12Core.dll` 1.619.2 |
| 设备创建入口 | **`d3d12.dll!ordinal:101` = `D3D12CreateDevice`** | `d3d12.dll` 导出 base=99，序号 101 对应 `D3D12CreateDevice`（见 §2.2） |
| 该入口能否挂钩 | **能，且本树已有同构的验证过的路径** | `artifacts/iat-diagnostics-test/raw-delay-fixed.log` 记录过 `import=d3d12.dll!ordinal:101` → IAT hook → `capture started` 全链路 |
| 静态导入表 | **空**（0 个 DLL） | import 目录 size=20，只有一个空描述符；全部 API 由运行时手动解析 |
| 延迟导入表 | 2 DLL / 15 函数，`rva_based=True` | `d3d12.dll` ×4、`thx_sdk.dll` ×11 |
| 反作弊 | **内核级**（`Randgrid.sys` + `CODBrokerService` + 远程证明） | 目录清单 + `AzureAttestManager.dll` / `CODSecureAttestationWizard.exe` |
| 注入可行性 | **未验证，是本树最大风险** | 静态无法判定，需要独立评估 |

一句话：**图形侧的门是开着的（入口明确、且 DF 树验证过同一条路），真正的闸门在反作弊和 Agility SDK 版本覆盖上。**

---

## 1 目标画像

| 项 | 值 |
|---|---|
| 大小 | 220.79 MB（231,518,712 B） |
| 架构 | x64，`machine=8664`，subsystem=2（GUI），非 DLL |
| ImageBase / Entry | `0x140000000` / `0x15791010` |
| PE 时间戳 | 2026-10-24 05:42:06Z |
| SHA-256 | `2da25084c6b3d59ac671bd7be978f04bff08ee56c3266e60e01e3b6fda3f23c9` |
| 签名 | **Valid**，`CN=Activision Publishing Inc`（EV，OID 2.5.4.15 Private Organization） |
| 版本资源 | `Call of Duty®` / Activision / FileVersion 2.0 |
| 导出目录自报名 | **`game_steam_ship.exe`**（即 Steam ship 镜像改名而来，导出表名未改） |
| 导出 | 116 个，全是 Intel IGCL（`ctl*`）+ `D3D12SDKPath` / `D3D12SDKVersion` / `NvOptimusEnablement` |

### 节区

| 节 | VA | VSize | RawSize | 属性 | 熵 |
|---|---|---|---|---|---|
| `.text` | `0x1000` | 213,786,624 | 213,786,624 | -X | 7.40 |
| `.rdata` | `0xcbe3000` | 10,850,304 | 10,847,232 | -- | 6.75 |
| `.data` | `0xd63c000` | 131,133,440 | 3,507,712 | W- | 4.14 |
| `.pdata` | `0x1534b000` | 3,567,616 | 1,847,296 | -- | 7.20 |
| `.tls` | `0x156b2000` | 32,768 | 30,720 | W- | 0.20 |
| `buildinf` | `0x156ba000` | 4,096 | 2,560 | W- | 0.59 |
| `.rsrc` | `0x156bb000` | 548,864 | 546,304 | -- | 6.40 |
| `.reloc` | `0x15741000` | 266,240 | 266,240 | -- | 1.63 |
| `.text`（第二个） | `0x15782000` | 671,780 | 672,256 | **WX** | 7.48 |

两点值得记：

- `.text` 203.9 MB 且熵 7.40 —— 远超正常代码（6.2–6.6），说明代码段内混了大量压缩数据或虚拟化代码；`.data` VSize 125 MB 但 RawSize 只有 3.3 MB，是运行时才展开的巨型数据区。
- 末尾多出一个**可写可执行**的 `.text`（~656 KB）。正常链接器不会产出这种节，是加壳/自保护层留的补丁区（Naraka 的 VMProtect 也有类似结构）。

---

## 2 图形入口：为什么这次比 Naraka 有戏

### 2.1 完整延迟导入表（这是本树唯一可接管的图形门）

```
d3d12.dll        rva_based=True attrs=0x1
    #101                                  <-- 按序号导入，无名
    +D3D12GetInterface
    +D3D12SerializeRootSignature
    +D3D12SerializeVersionedRootSignature

thx_sdk.dll      rva_based=True attrs=0x1
    +thxHpsCreate / Destroy / Prepare / Process / SetConfig
    +thxLssCreateWithProfile / Destroy / Prepare / Process / SetConfig
    +thxSdkSetAllocator
```

`thx_sdk.dll` 是 Intel oneAPI 的音频/语音库（`IPPCODE` / `IPPDATA` 节），与渲染无关，可以整体忽略。

**注意没有 `D3D12CreateDevice` 这个名字。** 设备创建是以序号导入的。

### 2.2 `#101` 是什么

对系统 `C:\Windows\System32\d3d12.dll` 解导出表：

```
base=99  functions=19  named=18
  100  SetAppCompatStringPointer
  101  D3D12CreateDevice          <-- 就是它
  102  D3D12GetDebugInterface
  ...
  110  D3D12GetInterface
  115  D3D12SerializeRootSignature
  116  D3D12SerializeVersionedRootSignature
```

即 `cod.exe` 的图形入口链是：

```
cod.exe（手动解析 API，无 IAT）
  └─ 延迟导入 d3d12.dll!ordinal:101  →  D3D12CreateDevice
       └─ D3D12GetInterface（按名，用于 debug / SDK 配置面）
       └─ D3D12Serialize*RootSignature（按名，签名序列化）
```

### 2.3 这条路本树已经验证过

`artifacts/iat-diagnostics-test/raw-delay-fixed.log` 里那条链与 COD 完全同构：

```
Graphics main delay target: moduleBase=... import=d3d12.dll!ordinal:101 entry=... target=...
Graphics IAT hook installed: module=main-image-delay-import import=d3d12.dll!D3D12CreateDevice ...
Graphics resolved delay hook installed: ... real=... hook=...
Graphics device returned: api=D3D12CreateDevice result=0x00000000 device=...
Adding D3D12 device frame capturer
Graphics swapchain wrapped: swapchain=... device=...
Starting capture
```

配套夹具 `d3d12-ordinal.def`（`D3D12CreateDevice @101 NONAME`）就是专门为「按序号延迟导入」造的原型。**COD 的 PE 结构与这个夹具一致，不是巧合，是可以直接复用的路径。**

### 2.4 `D3D12GetInterface` 不承担设备创建（好消息）

`renderdoc/driver/d3d12/d3d12_hooks.cpp:586` 的 `GetWrappedInterface()` 只处理 debug/tools/DRED/SDKConfiguration 六类接口，**没有 `CLSID_D3D12DeviceFactory` 分支**。

含义：本树不需要考虑「设备从 factory 出来」这条第二条路。设备创建只有 `D3D12CreateDevice` 一个入口，包装点唯一，行为可预测。

---

## 3 Agility SDK 细节

| 项 | 值 |
|---|---|
| `cod.exe` 导出 | `D3D12SDKPath`、`D3D12SDKVersion` —— 应用自己声明 Agility SDK 位置 |
| 本地 Core | `D3D12\D3D12Core.dll`，5,019,680 B，版本 **1.619.2.0.20260415.2** |
| SHA-256 | `90770bc8028ebb9f78051f903343b0aabc12fe7a2b886e007278b5312ba6f101` |
| 系统 `d3d12.dll` | 25 个依赖全是 api-ms-win-core / CRT，**不直接导入 dxgi.dll** |

两点判断：

1. Hook 点在 `system32\d3d12.dll` 的导出 thunk 上，位于本地 Core **之上**，所以「游戏自带 Agility SDK」这件事不影响 hook 生效。
2. 但 Core 版本决定设备实际支持到哪个 `ID3D12DeviceN`，**这决定了包装层够不够用**（见 §5 R3）。

---

## 4 与 Naraka 树的差异对照

| 维度 | Naraka（历史） | Steam-Cod（本树） |
|---|---|---|
| 图形 API | D3D11 level 11.1 | **D3D12**（Agility 1.619） |
| 静态导入表 | 有 | **空**（全部手动解析） |
| 延迟导入表 | **`delayImportRVA = 0x0`（被抹）** | **存在，15 个函数** |
| 设备入口 | `D3D11CreateDevice` 动态解析，VMP 隐藏导入 | **`D3D12CreateDevice` 延迟导入 #101** |
| 结论 | 结构性受阻，转静态资产解析 | **图形入口可用，瓶颈移到反作弊** |

Naraka 那条「delayImportRVA=0 ⇒ M3 无表可接」的死结，在这里不存在。这是本树最重要的好消息。

---

## 5 三个未验证项（按风险排序）

### R1 · 注入会不会被拦 —— 未知，且决定一切

目录里的反作弊/证明组件：

- `Randgrid.sys`（12.75 MB，内核驱动）
- `CODBrokerService.exe` / `CODBrokerInstaller.exe`
- `CODSecureAttestationWizard.exe`（67 MB）
- `AzureAttestManager.dll` / `AzureAttestNormal.dll`（硬件/远程证明）

Naraka 的经验在这里**不能直接套用**（那是 Neac + ACE）。Ricochet 系对进程内存、模块加载、D3D 设备完整性的检查行为没有本地样本，必须实测。

另：`C:\Program Files (x86)\Steam\steam.exe` 是 **32 位**启动器，和 Naraka 的 `LauncherGame.exe` 同理 —— **永远不作为注入目标**，真实目标只有 `cod.exe`。

### R2 · swapchain / factory 从哪来 —— **已定位并打补丁（见 S2 报告）**

`cod.exe` **没有导入 `dxgi.dll`**，系统 `d3d12.dll` 也不导入它。那么 `IDXGIFactory` 只可能来自：

- 本地 `D3D12Core.dll` 内部加载 dxgi，游戏通过 `ID3D12Device::QueryInterface(IDXGIDevice)` → `GetAdapter` → `GetParent(IDXGIFactory)` 拿到 factory；或
- 运行时自己 `LoadLibrary("dxgi.dll")` + 手动解析。

本树已经有这套桥（`dxgi_wrapped.cpp:181` `HookNativeDXGIFactoriesForAdapter`，给原生 vtable 的
`CreateSwapChain` / `ForHwnd` / `ForCoreWindow` / `ForComposition` 打补丁），并有实测日志
（`native factory bridge installed` → `swapchain wrapped`）。

唯一没覆盖的前提是 `D3D12CreateDevice` 的 `pAdapter == NULL`（原挂载点带 `&& pAdapter`）——
已补：失败时从真实 device 反查 adapter 再装桥。详见 **`docs/STEAM_COD_S2_FACTORY_BRIDGE.md`**，
配套夹具 `artifacts/steam-cod-recon/d3d12_null_adapter_factory.cpp`（三模式矩阵）。
补丁**尚未编译、夹具尚未运行**，R2 现在是「已修补待实测」，不是已知绿。

### R3 · 设备接口版本覆盖 —— 中等风险（黄）

- RenderDoc 侧最高到 **`ID3D12Device14`**（`renderdoc/driver/d3d12/d3d12_device_wrap14.cpp`）
- 本机 SDK（10.0.26100 / 10.0.22621）的 `d3d12.h` 里 **没有** `ID3D12Device15/16`
- 游戏带的是 **Agility 1.619**，比本机 SDK 新

如果游戏对设备 QI 到 15/16，包装层会返回 `E_NOINTERFACE`，轻则降级、重则创建失败。判据很直接：运行时日志出现 `Unknown UUID` / QI 失败即可确认。

---

## 6 下一步（按顺序，都不碰游戏）

| 步骤 | 动作 | 判据 |
|---|---|---|
| S1 | 只读运行观察：记录 `cod.exe` 的模块加载顺序与基址（`d3d12.dll` / `dxgi.dll` / `D3D12Core.dll` 谁先、谁后） | 能确定 dxgi 是 Core 内部加载还是游戏自加载 |
| S2 | 造「ordinal 101 + `pAdapter == NULL` → `GetParent` 取 factory」夹具，验证 M4 —— **已做，见 `docs/STEAM_COD_S2_FACTORY_BRIDGE.md`** | 桥已存在；补了 NULL adapter 分支；三模式夹具待编待跑 |
| S3 | 先编出 `TinecmaTool` / `TinecmaToolcmd`，再跑 `null-parent` / `explicit-parent` / `null-native` 三模式 | `null-parent` captured=1 且日志有 `adapter recovered` + `bridge installed` |
| S4 | 反作弊评估（R1）单独做，不与抓帧混在一起 | —— |

S3（编工具 + 跑矩阵）是现在唯一挡路的事：没有可用的 capture 二进制，任何运行时结论都拿不到。

---

## 7 复现

```bash
# cod.exe 全量画像（JSON: artifacts/steam-cod-recon/cod-exe-recon.json）
python artifacts/steam-cod-recon/pe_recon.py

# 任意 PE 的导出序号映射
python artifacts/steam-cod-recon/export_ordinals.py C:\Windows\System32\d3d12.dll

# 附带产物
artifacts/steam-cod-recon/thx-sdk-recon.json   # Intel oneAPI 音频库，可忽略
artifacts/steam-cod-recon/d3d12-system.json    # 系统 d3d12.dll 依赖面
```

抽样脚本不依赖第三方库（纯 `struct` + `seek`），220 MB 镜像只读头/表，毫秒级完成。
