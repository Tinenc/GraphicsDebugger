# S2 · D3D12 原生 factory 桥（M4）现状与补丁

- 分支：`Steam-Cod`
- 触发原因：`cod.exe` **不导入 `dxgi.dll`**，swapchain 的 factory 只能来自 `adapter->GetParent()`，
  这正是 Naraka 时期记录为「M4 工厂桥未触发」的漏点（`docs/STEAM_COD_RECON.md` §5 R2）
- 本报告结论：**本树（DF研究 基线上）已经有这套桥，并已跑通；但有一个前提未覆盖，已补**

---

## 1 代码事实：桥已经存在

| 位置 | 内容 |
|---|---|
| `renderdoc/driver/dxgi/dxgi_wrapped.cpp:181` | `HookNativeDXGIFactoriesForAdapter(IUnknown *unknown)`：对 adapter 逐个 QI 出 8 个 `IDXGIFactory`/`1`…`7` 接口，`GetParent` 拿原生 factory |
| `dxgi_wrapped.cpp:63` | `nativeFactorySlots[] = {10, 15, 16, 24}` |
| `dxgi_wrapped.cpp:142` | `HookNativeFactoryTable(factory, supportsFactory2)`：给**原生 vtable** 打补丁 |
| `dxgi_wrapped.cpp:72-86` | `CreateNativeFactorySwapchain()`：把 wrapped device 换成 real device 转发，成功后把返回的 swapchain 包成 `WrappedIDXGISwapChain4` |
| `dxgi_wrapped.cpp:146-149` | 保护：`page.AllocationBase != dxgi` 直接 return —— 只碰 dxgi.dll 自己的表，不误伤第三方 wrapper |

槽位对应的 4 个方法：

- `10` → `IDXGIFactory::CreateSwapChain`（legacy）
- `15` → `IDXGIFactory2::CreateSwapChainForHwnd`
- `16` → `IDXGIFactory2::CreateSwapChainForCoreWindow`
- `24` → `IDXGIFactory2::CreateSwapChainForComposition`

（`24` 与 `dxgi1_2.h` 的声明顺序不一致，但 `raw-factory-fixed-composition.log` 里 composition 模式实测拿到了
`native factory swapchain ... result=0x00000000`，说明作者按实际 vtable 校准过，不是笔误。）

## 2 已跑通的实测证据

`artifacts/iat-diagnostics-test/` 里三份日志都是同一条链：

```
Graphics native factory bridge installed: factory=... table=... methods=1
Graphics native factory bridge installed: factory=... table=... methods=4
Graphics native factory swapchain: device=... window=... result=0x00000000
Graphics swapchain wrapped: swapchain=... real=... device=... window=...
```

`methods=1` 是 `IDXGIFactory`（只 hook legacy），`methods=4` 是 `IDXGIFactory2`（4 个出口全 hook）。
`raw-factory-fixed-composition.log` 的 `window=0000000000000000` 证明 composition 分支也覆盖到了。

**所以 M4 在本树不是「未实现」，而是「已实现 + 已验证」。**

## 3 剩下的缺口：`pAdapter == NULL`

原来的挂载点（`renderdoc/driver/d3d12/d3d12_hooks.cpp:917`）：

```cpp
    if(SUCCEEDED(ret) && ppDevice && *ppDevice && pAdapter)
      HookNativeDXGIFactoriesForAdapter(pAdapter);
```

`&& pAdapter` 意味着：**只有调用方自己传了 adapter，桥才装。**

`cod.exe` 的 `D3D12CreateDevice` 是按序号延迟导入的，静态无法判定它传 adapter 还是 `NULL`。两种可能：

- 传 adapter（引擎通常 `EnumAdapters` 选卡）→ 桥生效，R2 直接绿
- 传 `NULL`（让 D3D12 自己选默认卡）→ **桥不装**，若随后又从 `device → IDXGIDevice → GetAdapter → GetParent` 取 factory，swapchain 就是原生的，抓不到帧

这是一个精确的分支条件，不是模糊的「可能有问题」。

## 4 补丁

`d3d12_hooks.cpp:917` 起：

```cpp
    if(SUCCEEDED(ret) && ppDevice && *ppDevice)
    {
      if(pAdapter)
      {
        HookNativeDXGIFactoriesForAdapter(pAdapter);
      }
      else if(WrappedID3D12Device::IsAlloc(*ppDevice))
      {
        // pAdapter == NULL means D3D12 picked the adapter itself. Recover it from the real
        // device so engines that never pass an adapter still get the native factory bridge.
        ID3D12Device *realDevice = ((WrappedID3D12Device *)*ppDevice)->GetReal();
        IDXGIDevice *dxgiDevice = NULL;

        if(realDevice &&
           SUCCEEDED(realDevice->QueryInterface(__uuidof(IDXGIDevice), (void **)&dxgiDevice)) &&
           dxgiDevice)
        {
          IDXGIAdapter *adapter = NULL;

          if(SUCCEEDED(dxgiDevice->GetAdapter(&adapter)) && adapter)
          {
            RDCLOG("Graphics adapter recovered from device for native factory bridge: device=%p "
                   "adapter=%p",
                   realDevice, adapter);
            HookNativeDXGIFactoriesForAdapter(adapter);
            adapter->Release();
          }

          dxgiDevice->Release();
        }
      }
    }
```

三处设计取舍：

1. **必须用 `GetReal()` 拿真设备**。传 wrapped device 进去，`QueryInterface(IDXGIDevice)` 回来的是 wrapped
   adapter，它的 `GetParent` 返回 wrapped factory，而 `HookNativeFactoryTable` 的 `AllocationBase != dxgi`
   保护会把表直接跳过 —— 白干。
2. **`WrappedID3D12Device::IsAlloc(*ppDevice)` 先做类型确认**。`WRAPPED_POOL_INST` 生成的
   `IsAlloc(const void *)`（`common/wrapped_pool.h:228`）对任何 `ID3D12DeviceN*` 都命中，避免对非本池指针强转。
3. **失败静默**。拿不到 adapter 就什么都不做，不改变原有返回值和错误语义（反作弊环境里少改行为是好事）。

## 5 验证夹具

`artifacts/steam-cod-recon/d3d12_null_adapter_factory.cpp` —— 复刻 COD 的入口形态：

- 通过 `d3d12-ordinal.lib`（`/DELAYLOAD:d3d12.dll` + `D3D12CreateDevice @101 NONAME`）导入设备创建，**和 cod.exe 一样按序号**
- 三种模式（环境变量 `GOP_FACTORY_MODE`）：

| 模式 | 路径 | 预期 `captured` | 含义 |
|---|---|---|---|
| `null-parent` | `D3D12CreateDevice(NULL)` → `QI(IDXGIDevice)` → `GetAdapter` → `GetParent` → `CreateSwapChainForHwnd` | **1** | 新增分支生效，COD 的可能路径被覆盖 |
| `explicit-parent` | 传 adapter，同样 `GetParent` 取 factory | 1 | 基线，补丁前就是绿的 |
| `null-native` | `NULL` 设备 + 原生 `LdrGetProcedureAddress` 取 `CreateDXGIFactory1` | **0** | 对照组，证明「没走桥就抓不到」 |

判据不只看 `captured`，还要看 capture 侧日志里是否出现：

```
Graphics adapter recovered from device for native factory bridge: device=... adapter=...
Graphics native factory bridge installed: factory=... methods=4
Graphics swapchain wrapped: swapchain=...
```

`null-native` 的 `captured=0` 是**预期结果**，不是失败 —— 它证明夹具确实有分辨力，不是怎么写都能过。

## 6 编译与运行

```bat
cd artifacts\steam-cod-recon
build-null-adapter-factory.cmd
set TINECMATOOL_CMD=C:\path\to\TinecmaToolcmd.exe
run-null-adapter-factory.cmd null-parent
run-null-adapter-factory.cmd explicit-parent
run-null-adapter-factory.cmd null-native
```

`build-null-adapter-factory.cmd` 若在 `vcvarsall` 处失败（本机 `reg.exe` 被策略拦的老问题），
按 `MEMORY.md` 里的办法手工设 `INCLUDE`/`LIB`，再单独跑 `cl` 那行。

## 7 状态声明

- [x] 代码面结论：桥已实现，槽位与保护逻辑已核对
- [x] 缺口定位：`&& pAdapter` 是唯一未覆盖前提
- [x] 补丁已写入 `d3d12_hooks.cpp`
- [ ] **补丁未编译**（本机 `MSBuild` 被安全策略拦，需要手工构建）
- [ ] **夹具未运行**（本树没有构建好的 `TinecmaTool.dll` / `TinecmaToolcmd.exe`）

也就是说：R2 从「未知风险」收敛为「一个已修补的确定条件」，但**尚未拿到实测证据**。
先编出工具，再跑三模式矩阵，才能把 R2 判绿。
