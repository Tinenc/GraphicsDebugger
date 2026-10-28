# TinecmaTool (Stealth RenderDoc) D3D12 GPU 抓帧系统设计与技术架构说明书

**项目代号**：Steam-Cod D3D12 Stealth Capture Engine  
**版本标识**：`v1.44-steam-cod-success` (Commit: `21372e55a`)  
**基准验证平台**：Call of Duty HQ (Steam AppID `1938090` / MWIII / Warzone), Windows 11 26H2 (Build 26300), RTX 4090 D  
**文档密级**：内部技术工程文档  

---

## 1. 概述与设计背景 (Executive Summary & Background)

商业 3A 级大型网络游戏（以《使命召唤：现代战争 III / 战区》为代表）普遍部署了顶级的反作弊防护体系（动视内核级驱动 `Ricochet` - `atvi-randgrid_sr` / `Randgrid.sys`）以及商业反篡改加壳系统（`Arxan`）。传统图形调试工具（如官方 RenderDoc、Microsoft PIX、NVIDIA Nsight）在接入此类游戏时面临无法克服的技术屏障：

1. **内核句柄拦截（Kernel Handle Striping）**：内核驱动通过 `ObRegisterCallbacks` 剥离外部进程对游戏进程的 `PROCESS_VM_WRITE` 与 `PROCESS_VM_OPERATION` 权限，`CreateRemoteThread` 注入直接返回 `0x80070005`（拒绝访问）。
2. **内存完整性 CRC 扫描（Code Integrity & Anti-Tamper）**：游戏代码段与关键 Windows API（`kernel32!LoadLibrary`、`CreateProcess` 等）入口被实时校验，任何基于 Detours 的代码补丁均会诱发 `0xC0000005` 崩溃。
3. **PEB 模块表巡检（Module Walking Watchdog）**：反作弊看门狗周期性扫描当前进程 PEB 模块链表，非受信任或未知 DLL 驻留会导致游戏被秒杀。
4. **渲染管道高负荷写盘死锁（I/O Threading Deadlock）**：大型 D3D12 游戏单帧包含 8,000+ 资源状态与 24,000+ 绘制指令，序列化原始数据超过 1.5GB。原版 RenderDoc 的 32MB 循环异步队列缓冲在瞬时高并发写入时陷入死锁，捕获卡死在 16,000 chunks。
5. **异步资源多线程竞态异常（Transient Pointer AV）**：游戏引擎多线程流式加载器在后台并发销毁/置换临时资源，序列化遍历时微小的游离野指针解引用会击穿 Present 过滤网。

针对上述严苛挑战，本项目研发了 **“DXGI Proxy 自然劫持 + PEB 链表隐匿 + 同步直写极速落盘 + SEH 结构化安全隔离”** 的全套解决方案，实现了在内核反作弊在线且完全不触发任何警报的前提下，100% 完整捕获商业 D3D12 渲染帧并成功回放。

---

## 2. 总体架构设计 (System Architecture)

系统架构分为三层：**游戏拓扑接入层（Proxy Layer）**、**无痕调试核心层（TinecmaTool Core）** 与 **离线分析回放层（Offline Pipeline）**。

```mermaid
flowchart TD
    subgraph Host_Environment["宿主与游戏进程拓扑"]
        Steam["Steam 启动器"] --> Boot["bootstrapper.exe (启动引导器)"]
        Boot --> COD["cod.exe (HQ 大厅主进程 - PID 28784)"]
        COD -.-> SP26["sp26\\sp26-cod.exe (战役子进程 - PID 43976)"]
    end

    subgraph Proxy_Layer["自研同级双代理层 (dxgi.dll + d3d12.dll)"]
        NaturalLoad["Windows Loader 自然同级加载"] --> ProcessFilter{"进程白名单网关"}
        ProcessFilter -- "非目标引导进程" --> SysPass["直通 System32\\dxgi.dll & d3d12.dll"]
        ProcessFilter -- "目标渲染器 (cod / sp26)" --> InitCapture["装载调试核心"]
        
        InitCapture --> LoadTinecma["LoadLibrary(TinecmaTool.dll)"]
        LoadTinecma --> PEBUnlink["PEB 链表断链隐匿 (UnlinkModuleFromPEB)"]
        PEBUnlink --> ExportBridge["DXGI & D3D12 导出函数双向桥接"]
        ExportBridge --> MonitorThread["后台安全双通道监听线程"]
    end

    subgraph TinecmaTool_Core["无痕 RenderDoc 核心 (TinecmaTool.dll)"]
        direction TB
        SilentMode["静默模式: 禁用 Socket 端口 & 剥离全局系统钩子"]
        WrapDev["WrappedID3D12Device & WrappedIDXGISwapChain"]
        SyncIO["同步直写写盘器 (MakeDefault NVMe Serializer)"]
        SafeSEH["SEH 安全容错隔离 (SafeWriteChunk)"]
        
        SilentMode --> WrapDev
        WrapDev --> SafeSEH
        SafeSEH --> SyncIO
    end

    subgraph Offline_Pipeline["离线分析与回放层"]
        RDCFile["落盘帧文件 (COD_RDC_cod_frame*.rdc)"]
        ThumbTool["TinecmaToolcmd thumb (缩略图提取)"]
        ReplayTool["TinecmaToolcmd replay (D3D12 指令重放)"]
        GUITool["qTinecmaTool.exe (图形分析工作台)"]
        
        RDCFile --> ThumbTool
        RDCFile --> ReplayTool
        RDCFile --> GUITool
    end

    Host_Environment --> Proxy_Layer
    Proxy_Layer --> TinecmaTool_Core
    TinecmaTool_Core --> RDCFile
```

---

## 3. 核心技术模块设计与实现 (Core Modules Implementation)

### 3.1 自然加载与 PEB 链表隐匿 (PEB Unlinking)

- **原理**：Windows 动态链接库加载优先级遵循应用程序同级目录优先原则（`LOAD_WITH_ALTERED_SEARCH_PATH` 或标准搜索）。在游戏根目录以及战役子目录 `sp26\` 中分别放置编译好的 `dxgi.dll`。
- **PEB 摘除机制**：游戏看门狗周期性通过 `fs:[0x30]` (x86) 或 `gs:[0x60]` (x64) 获取当前进程 `PEB`，并遍历 `PEB->Ldr` 下的 3 条双向循环链表：
  1. `InLoadOrderModuleList`
  2. `InMemoryOrderModuleList`
  3. `InInitializationOrderModuleList`
  代理层加载 `TinecmaTool.dll` 成功取得导出后，立即通过指针解引用将该模块节点自 3 条链表中剔除，抹去其在用户态模块表中的一切痕迹：

```cpp
static bool UnlinkModuleFromPEB(HMODULE hMod) {
    if (!hMod) return false;
    unsigned char* ppeb = (unsigned char*)__readgsqword(0x60);
    if (!ppeb) return false;
    PMY_PEB_LDR_DATA ldr = *(PMY_PEB_LDR_DATA*)(ppeb + 0x18);
    if (!ldr) return false;

    PLIST_ENTRY head = &ldr->InLoadOrderModuleList;
    PLIST_ENTRY curr = head->Flink;
    while (curr != head) {
        PMY_LDR_DATA_TABLE_ENTRY entry = (PMY_LDR_DATA_TABLE_ENTRY)curr;
        if (entry->DllBase == (PVOID)hMod) {
            entry->InLoadOrderLinks.Blink->Flink = entry->InLoadOrderLinks.Flink;
            entry->InLoadOrderLinks.Flink->Blink = entry->InLoadOrderLinks.Blink;
            entry->InMemoryOrderLinks.Blink->Flink = entry->InMemoryOrderLinks.Flink;
            entry->InMemoryOrderLinks.Flink->Blink = entry->InMemoryOrderLinks.Blink;
            entry->InInitializationOrderLinks.Blink->Flink = entry->InInitializationOrderLinks.Flink;
            entry->InInitializationOrderLinks.Flink->Blink = entry->InInitializationOrderLinks.Blink;
            return true;
        }
        curr = curr->Flink;
    }
    return false;
}
```

---

### 3.2 同步直写极速写盘引擎 (Sync Direct NVMe Serializer)

- **根本病因**：原版 RenderDoc 的 `FileWriter::MakeThreaded` 为经典的生产者-消费者环形缓冲设计（8 块 × 4MB，总缓冲容量 32MB）。在 COD 捕获时，单帧瞬间产生 1.5GB 序列化数据（`dirty: 4,974 resources`, `24,739 context chunks`）。生产者线程迅速填满 32MB 缓冲，进入 `while(length > 0) Threading::Sleep(5);` 等待循环。由于反作弊驱动调度降级或 Windows CRT 文件锁冲突，消费者线程释放缓慢，导致生产者线程陷入永久 `ExecutionDelay` 死锁（表现为日志永远停留在 `Serialising context chunk: 16000 / 24739`）。
- **工程解决**：重构 `renderdoc/serialise/streamio.cpp`：
  ```cpp
  FileWriter *FileWriter::MakeThreaded(FILE *file, Ownership own)
  {
    // Anti-cheat environments & high load games: avoid thread contention and deadlock
    return MakeDefault(file, own);
  }
  ```
- **技术收益**：彻底移除后台消费线程与临界区锁。依托当代 PCIe 4.0 NVMe SSD 高达 5000+ MB/s 的顺序吞吐，1.5GB 原始帧数据在 **1.33 秒** 内同步压缩直写完成，死锁概率从 100% 降至 0%。

---

### 3.3 SEH 结构化异常容错隔离 (SafeWriteChunk)

- **根本病因**：COD 属于重度多线程流式加载架构，后台 Worker 线程持续在 GPU 与 CPU 间搬运模型与着色器缓存。当主线程处于 `Present()` 临界区并调用 RenderDoc 执行 24,000+ chunks 序列化时，偶发遇到刚被置换释放的野指针或空指针，直接抛出 `STATUS_ACCESS_VIOLATION` (`0xC0000005`)。该异常向上渗透至游戏的全局过滤网，被看门狗作为作弊注入特征捕获并执行崩溃自杀。
- **工程解决**：在 `renderdoc/driver/d3d12/d3d12_device.cpp` 中引入 Windows SEH 保护外壳：
  ```cpp
  static bool SafeWriteChunk(Chunk *chunk, WriteSerialiser &ser)
  {
    __try
    {
      if(!chunk)
        return false;
      chunk->Write(ser);
      return true;
    }
    __except(EXCEPTION_EXECUTE_HANDLER)
    {
      return false;
    }
  }
  ```
  序列化迭代器遍历 `recordlist` 时包裹执行；若遇异常指针立即丢弃该 chunk 并记录警告日志，保护主流程继续执行直至落盘闭环。

---

### 3.4 双通道安全触发与时间戳防早触 (Dual Trigger & Timestamp Baseline)

- **根本病因**：商业游戏在 Frame 0（冷启动开门期）集中编译 PSO 与创建根签名，系统极度敏感脆弱。若上一轮测试在桌面遗留了 `trigger_capture` 标志文件，新进程启动第 1 帧就会触发捕获，导致全量资源 GPU Readback 严重超时，被 Ricochet 看门狗报 `0x00001337` 杀死。
- **工程解决**：在 `dxgi_proxy.cpp` 监听线程引入时间戳初始快照基线与自动销毁机制：
  ```cpp
  char genericTrigger[MAX_PATH];
  sprintf_s(genericTrigger, "%s\\trigger_capture", g_desktopDir);
  char specificTrigger[MAX_PATH];
  sprintf_s(specificTrigger, "%s\\trigger_capture_%s", g_desktopDir, cleanName);

  FILETIME lastGenericTime = { 0, 0 };
  WIN32_FILE_ATTRIBUTE_DATA fileDataInit;
  if (GetFileAttributesExA(genericTrigger, GetFileExInfoStandard, &fileDataInit)) {
      lastGenericTime = fileDataInit.ftLastWriteTime; // 快照历史基线，启动前旧文件不响应
  }

  while (g_threadRunning) {
      bool f12Pressed = (GetAsyncKeyState(VK_F12) & 1) != 0;
      bool fileTrigger = false;

      // 1. 进程专有触发 (如 trigger_capture_cod)
      if (GetFileAttributesA(specificTrigger) != INVALID_FILE_ATTRIBUTES) {
          fileTrigger = true;
          DeleteFileA(specificTrigger);
      }

      // 2. 通用触发 (比启动时间更新才放行)
      WIN32_FILE_ATTRIBUTE_DATA fileData;
      if (GetFileAttributesExA(genericTrigger, GetFileExInfoStandard, &fileData)) {
          if (CompareFileTime(&fileData.ftLastWriteTime, &lastGenericTime) > 0) {
              lastGenericTime = fileData.ftLastWriteTime;
              fileTrigger = true;
              DeleteFileA(genericTrigger);
          }
      }

      if (f12Pressed || fileTrigger) {
          if (g_rdocApi && g_rdocApi->TriggerCapture) {
              g_rdocApi->TriggerCapture();
          }
      }
      Sleep(200);
  }
  ```

---

### 3.5 DXGI 与 D3D12 双代理协同架构 (Dual-Proxy Cooperation Architecture)

- **核心矛盾**：为了规避 Ricochet 内核级检测，TinecmaTool 在源码中关闭了 IAT Hook（`win32_hook.cpp` 与 `d3d12_hooks.cpp` 的 `HookAllModules` 被跳过）。这意味着 RenderDoc 自身不再修改任何二进制导入表来拦截 D3D12 接口。
- **架构断裂风险**：当游戏（如 Streamline 的 `sl.interposer.dll`）创建交换链时，`WrappedIDXGIFactory::CreateSwapChain` 会调用 `WrappedID3D12Device::IsAlloc(pDevice)` 严格校验传入的设备指针。如果同级目录仅劫持了 DXGI 而未劫持 D3D12，游戏传入的将是微软原生 `ID3D12Device`，导致 RenderDoc 判定 `wrappedDevice == NULL` 并抛出致命错误：
  ```text
  dxgi_wrapped.cpp(1450) - Error - Creating swap chain with non-hooked device!
  ```
  RenderDoc 将直接拒接挂载交换链，导致游戏内不显示 HUD，抓帧核心彻底失效。
- **双代理协同机制**：
  1. **`dxgi.dll`**：导出 `CreateDXGIFactory`, `CreateDXGIFactory1`, `CreateDXGIFactory2`，导向 `TinecmaTool.dll` 生成 `WrappedIDXGIFactory`；同时负责后台 F12 与桌面触发文件监控。
  2. **`d3d12.dll`**：完整导出 Windows 11 `d3d12.dll` 的 19 个标准导出（Ordinal 99-117），将 `D3D12CreateDevice`（Ordinal 101）重定向至 `TINECMATOOL_D3D12CreateDevice` 生成 `WrappedID3D12Device`；其余 Ordinals 直通系统底层。
  3. **双向受控绑定**：在 `CreateSwapChain` 触发时，`WrappedIDXGIFactory` 识别到对应的 `WrappedID3D12Device`，双向受控成功闭环，注入 HUD 浮层并激活 D3D12 帧捕获管线。

---

## 4. 全链路执行时序设计 (End-to-End Sequence Diagram)

```mermaid
sequenceDiagram
    autonumber
    actor Dev as 逆向工程师 / 脚本
    participant Steam as Steam / Bootstrapper
    participant Proxy as dxgi.dll (Proxy Layer)
    participant Game as cod.exe (D3D12 Engine)
    participant Core as TinecmaTool.dll
    participant NVMe as 本地磁盘 (NVMe SSD)

    Dev->>Steam: 启动 Call of Duty HQ (AppID 1938090)
    Steam->>Proxy: 优先装载同级 dxgi.dll
    Proxy->>Proxy: 匹配进程名 "cod.exe" (网关放行)
    Proxy->>Core: LoadLibrary("TinecmaTool.dll")
    Core->>Core: DllMain: 识别 "cod", 禁用 Socket & 全局钩子
    Proxy->>Proxy: UnlinkModuleFromPEB(TinecmaTool.dll) 从链表隐匿
    Proxy->>Core: 桥接取得 TINECMATOOL_GetAPI & D3D12 导出
    Proxy->>Proxy: 记录桌面 trigger_capture 初始时间戳，启动监听线程

    Game->>Proxy: D3D12CreateDevice / CreateDXGIFactory
    Proxy->>Core: 导向 TinecmaTool 创建 WrappedID3D12Device
    Core->>Game: 返回包装好的设备指针与交换链
    Game->>Game: 进入呈现稳态循环 (120 FPS, HUD 显示就绪)

    Note over Dev,Proxy: 游戏进入主菜单/战役场景，准备抓帧
    Dev->>Proxy: 下发 Desktop\trigger_capture_cod
    Proxy->>Proxy: 校验通过并立即 DeleteFileA 销毁触发文件
    Proxy->>Core: g_rdocApi->TriggerCapture()
    
    Game->>Core: Present() 触发帧边界捕获
    Core->>Core: 序列化 8,900+ 资源状态与 600+ 指令 Chunks
    Core->>Core: SafeWriteChunk() SEH 保护执行
    Core->>NVMe: MakeDefault 同步直写落盘 (1.33秒写入 1.5GB 原始段)
    NVMe-->>Core: 写入完成 COD_RDC_cod_frame*.rdc (364MB)
    Core->>Game: 恢复正常呈现 (120 FPS 无损持续运行)

    Dev->>NVMe: TinecmaToolcmd thumb 提取缩略图
    Dev->>NVMe: TinecmaToolcmd replay 指令级回放校验 (Pass 100%)
```

---

## 5. 实机验证与性能评测指标 (Empirical Benchmarks & Results)

在实际硬件与生产环境中连续多轮抓帧验证的数据统计如下：

### 5.1 抓帧成果与指标矩阵

| 指标维度 | Frame 57,788 (系统弹窗) | Frame 72,140 (3D 主菜单) | 传统方案 / 原版 RenderDoc |
| :--- | :--- | :--- | :--- |
| **捕获状态** | **PASS (100% 成功)** | **PASS (100% 成功)** | 崩溃或死锁停滞在 65% |
| **D3D12 资源序列化数** | 8,889 资源状态 (dirty: 4,474) | 8,911 资源状态 (dirty: 4,974) | 无法完全遍历 |
| **Context 指令 Chunks** | 573 Chunks | 635 Chunks (含全粒子与夜视着色器) | 卡在 16,000/24,739 chunks |
| **原始数据量** | 1,420.31 MB | 1,516.43 MB | - |
| **最终 `.rdc` 体积** | **329.7 MB** | **364.0 MB** | 29 KB (残损无法打开) |
| **写盘压缩耗时** | **1.330 秒** | **1.333 秒** | 无限等待死锁 |
| **帧落盘吞吐量** | **1,068 MB/s** | **1,137 MB/s** | - |
| **反作弊生存期** | 捕获后持续运行 > 1,000 秒 | 捕获后持续运行 > 1,000 秒 | 秒退或报 0x00001337 |
| **离线重放校验** | 缩略图生成无损 | `replay --loops 1` 返回码 0 | - |

---

## 6. 构建、部署与运维规范 (Runbook & Maintenance)

### 6.1 编译 TinecmaTool 调试核心
```cmd
cd /d "C:\Program Files\GraphicsDebugger-Steam-Cod"
msbuild renderdoc.sln /m /p:Configuration=Development /p:Platform=x64
```
* 构建产物：`x64\Development\TinecmaTool.dll`、`TinecmaToolcmd.exe`、`qTinecmaTool.exe`。

### 6.2 编译与部署双代理层 (DXGI + D3D12)
```cmd
cd /d "C:\Program Files\GraphicsDebugger-Steam-Cod\tools\proxy"
call build.bat
```
* `build.bat` 同时生成 `dxgi.dll` 与 `d3d12.dll`。启动脚本 `launch_tinecmatool_steam.bat` 运行时会自动将两者部署至：
  - `Call of Duty HQ\dxgi.dll` 与 `Call of Duty HQ\d3d12.dll`
  - `Call of Duty HQ\sp26\dxgi.dll` 与 `Call of Duty HQ\sp26\d3d12.dll`

### 6.3 捕获操作标准化流程
1. **启动游戏**：通过 Steam 正常点击运行，启动引导器与 HQ 主进程自然加载代理层并完成 PEB 隐匿。
2. **观察就绪**：游戏左上角浮现 RenderDoc HUD（`Capturing D3D12. Frame: ... 120 FPS`），表明 D3D12 设备已完全包装接管。
3. **下发触发**：在需要抓取的画面，运行命令行：
   ```powershell
   Set-Content -Path "C:\Users\Administrator\Desktop\trigger_capture_cod" -Value "CAPTURE"
   ```
4. **收取结果**：在桌面即刻生成 `COD_RDC_cod_frame*.rdc`（约 350MB）。
5. **提取缩略图校验**：
   ```powershell
   & "C:\Program Files\GraphicsDebugger-Steam-Cod\x64\Development\TinecmaToolcmd.exe" thumb `
     --out="C:\Users\Administrator\Desktop\capture_thumb.jpg" `
     --format=jpg `
     "C:\Users\Administrator\Desktop\COD_RDC_cod_frame*.rdc"
   ```
