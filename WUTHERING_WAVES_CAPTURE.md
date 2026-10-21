# 鸣潮 PC 抓帧：核心修改说明

> 适用：本仓库（GraphicsDebugger-Wuthering-Waves / TinecmaTool，基于 RenderDoc v1.44）
> 目标：用自建 RDC 在 **PC 端**稳定抓到 `Client-Win64-Shipping.exe` 的画面帧
> 结论：**2 处代码改动 + 3 处配套改动 + 2 个环境变量**，已在实机跑通

---

## 0. TL;DR

| 类别 | 内容 | 必要性 |
|---|---|---|
| 代码 A | 注入方式改为 **Early Bird APC**，彻底不创建远程线程 | 必需（否则被反作弊杀） |
| 代码 B | 子进程命令行追加 `-krqlv=<档位>`（`TCM_CHILD_ARGS`） | 必需（否则 kuro 直接 abort） |
| 代码 C | `TCM_FORCE_CHILD_INJECT` 旁路 `hookIntoChildren` | 必需（APC 注入使选项晚到） |
| 代码 D | ACE / CrashSight 子进程注入黑名单 | 必需（否则连反作弊安装器一起注入） |
| 操作 | `tools\launch_rdc.bat` 起 GUI | 必需（环境变量必须被 GUI 继承） |

---

## 1. 失败根因

之前「改了名字还是不行」，是因为存在**两条互不相干**的拦截，只解决一条都不够。

### 1.1 kuro 的启动校验其实是命令行参数校验

`Client-Win64-Shipping.exe` 偏移 `0x7B7B74A` 起有一段连续的 UTF-16 字符串表：

```
... ignore FKuroPakPlatform::StartupMount() finished.
    launch not by commandlet, use kuro quality!
    -krqlv=      HD   %sHD/      SD   %sSD/      UHD   %sUHD/
    kuro: Use launcher to start game!
```

执行逻辑：读 `-krqlv=` → 匹配 `hd/sd/uhd` → 拼 `%sHD/` 档位资源路径 → 匹配不上就抛
`Fatal error: [File:Unknown] [Line: 54] kuro: Use launcher to start game!` 并终止。

- 3.7 起客户端资源分档（`Client/Content/HD|SD|UHD`），档位**只能由命令行告知**
- 转发壳 `Wuthering Waves.exe`（484 KB）里**没有任何 `krqlv` 字样**（ASCII / UTF-16 均为 0 命中），即它不会自己补这个参数
- 结论：这条报错和「反作弊 / 父进程链」**无关**，名字改得再干净也没用

### 1.2 注入方式本身是签名

改名前（原版 RenderDoc）的注入路径：

```cpp
// win32_process.cpp
CreateRemoteThread(hProcess, NULL, 1024*1024, (LPTHREAD_START_ROUTINE)LoadLibraryW, remoteMem, 0, NULL);
```

问题不止一处：

| 现象 | 原因 |
|---|---|
| 一次 Launch 产生 **5 个远程线程** | `InjectDLL` 一个，`InjectFunctionCall` 还要为 `INTERNAL_SetCaptureFile` / `SetDebugLogFile` / `SetCaptureOptions` / `GetTargetControlIdent` 各建一个 |
| 远程线程入口是 `kernel32!LoadLibraryW` | 这是注入检测里最经典的一条签名，ACE 的内核回调可以直接看到 |
| 注入成功后几秒进程消失、无 WER、UE 日志 0 字节 | `TerminateProcess`，不是崩溃；同时 `ACE-BASE.sys` 的 mtime 正好被重写 |

---

## 2. 核心修改 A：Early Bird APC 注入（无远程线程）

### 2.1 原理

进程以 `CREATE_SUSPENDED` 创建后，主线程尚未执行任何用户代码。此时：

1. 把 DLL 路径写进目标进程内存
2. `QueueUserAPC(LoadLibraryW, hThread, 路径)`
3. `ResumeThread` —— ntdll 在 loader 初始化完成之后、exe 入口点之前投递这个 APC

好处：

- loader 状态合法（这是关键，见 2.3）
- 我们的 `DllMain` 会在目标执行**第一条指令之前**完成，所有 API 钩子已就位
- 全程 **不创建任何远程线程**

### 2.2 代码

`renderdoc/os/win32/win32_process.cpp`：

```cpp
uintptr_t FindRemoteDLL(DWORD pid, rdcstr libName);   // 前向声明

static bool InjectDLLViaAPC(HANDLE hProcess, HANDLE hThread, const wchar_t *dllPath)
{
  const size_t pathLen = (wcslen(dllPath) + 1) * sizeof(wchar_t);

  HMODULE kernel32 = GetModuleHandleA("kernel32.dll");
  uintptr_t loadLibrary = (uintptr_t)GetProcAddress(kernel32, "LoadLibraryW");

  void *remoteMem = VirtualAllocEx(hProcess, NULL, pathLen, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
  WriteProcessMemory(hProcess, remoteMem, dllPath, pathLen, NULL);

  QueueUserAPC((PAPCFUNC)loadLibrary, hThread, (ULONG_PTR)remoteMem);

  ResumeThread(hThread);          // 让 loader 跑，APC 在入口点前落地

  // 轮询模块是否出现，顺便判目标是否提前退出
  for(int i = 0; i < 200; i++)
  {
    if(FindRemoteDLL(pid, STRINGIZE(RDOC_BASE_NAME) ".dll") != 0)
      return true;
    DWORD exitCode = 0;
    if(GetExitCodeProcess(hProcess, &exitCode) && exitCode != STILL_ACTIVE)
      break;
    Sleep(50);
  }
  return false;
}

void InjectDLL(HANDLE hProcess, rdcwstr libName, HANDLE hSuspendedThread)
{
  if(hSuspendedThread != NULL)
  {
    if(InjectDLLViaAPC(hProcess, hSuspendedThread, dllPath))
    {
      RDCLOG("Injected %ls via early-bird APC, no remote thread created", dllPath);
      return;                     // 成功就不走远程线程
    }
    RDCWARN("APC injection failed, falling back to CreateRemoteThread");
  }

  ... 原 CreateRemoteThread 逻辑保持不变（回退路径）
}
```

### 2.3 为什么不用 SetThreadContext 直接改 RIP（踩过的坑）

第一版实现是：`GetThreadContext` → 把 RIP 指到一段本地生成的 stub（`push` 保寄存器 → `call LoadLibraryW` → 恢复 → `jmp` 回原入口）。

实机结果（`RenderDoc_2026.10.22_01.37.08.log`）：

```
01:37:26  Injected ... via thread context, no remote thread created
01:37:26  WER 1000: Wuthering Waves.exe 0xc0000005, 模块 unknown, 偏移 0x7fff59de80c0
01:37:55  第二次：0xc0000005, 模块 ntdll.dll, 偏移 0xc3ab6
```

原因：`CREATE_SUSPENDED` 状态下初始线程的 context 仍在 `LdrInitializeThunk` 里，loader **还没有初始化该进程**。在这个时机调用 `LoadLibraryW`，直接访问冲突。表现就是「注入日志成功、钩子装上、进程立刻炸，连子进程都没拉起来」。

**结论：挂起线程劫持只适用于已经跑起来的线程，不适用于进程刚创建时的初始线程。**

### 2.4 参数贯通（线程句柄怎么送到注入点）

| 文件 | 改动 |
|---|---|
| `renderdoc/os/os_specific.h` | `InjectIntoProcess(..., bool waitForExit, void *hSuspendedThread = NULL)` |
| `renderdoc/os/win32/win32_process.cpp` | `Process::InjectIntoProcess` 增加同名参数；内部把 `hSuspendedThread` 传给 `InjectDLL` |
| `renderdoc/os/win32/win32_process.cpp` | `LaunchAndInjectIntoProcess` → `InjectIntoProcess(..., (void*)pi.hThread)` |
| `renderdoc/os/win32/sys_win32_hooks.cpp` | `Hooked_CreateProcess` → `InjectIntoProcess(..., (void*)lpProcessInformation->hThread)` |
| `renderdoc/os/posix/posix_process.cpp` | 形参补上 + `(void)hSuspendedThread;`，忽略即可 |

用 `void *` 而不是 `HANDLE` 是为了不破坏 posix 共用头；默认参数保证 `RENDERDOC_InjectIntoProcess`（对运行中进程注入）等既有调用点零改动。

---

## 3. 核心修改 B：子进程命令行注入

### 3.1 为什么要在 hook 里做

抓帧链条是 `Wuthering Waves.exe`（壳）→ `Client-Win64-Shipping.exe`（渲染本体）。

- 工具只能直接 Launch **壳**（Client 直启会被保护层拒绝）
- 壳不补 `-krqlv`，所以 Client 必然 abort
- 只能在我们 hook 到 `CreateProcessW`、真正转发之前，把参数塞进子进程命令行

### 3.2 代码

`renderdoc/os/win32/sys_win32_hooks.cpp`：

```cpp
static LPCWSTR PatchChildCommandLine(LPCWSTR lpCommandLine, std::wstring &storage)
{
  static const std::wstring extra = []() -> std::wstring {
    char buf[1024] = {0};
    const DWORD len = GetEnvironmentVariableA("TCM_CHILD_ARGS", buf, (DWORD)sizeof(buf));
    if(len > 0 && len < (DWORD)sizeof(buf))
      return std::wstring(StringFormat::UTF82Wide(rdcstr(buf, len)).c_str());
    return std::wstring();
  }();

  if(extra.empty())
    return lpCommandLine;

  // 壳自己已经转发过就不重复追加
  if(lpCommandLine != NULL && wcsstr(lpCommandLine, extra.c_str()) != NULL)
    return lpCommandLine;

  if(lpCommandLine != NULL)
  {
    storage = lpCommandLine;
    storage.push_back(L' ');
  }
  storage += extra;
  return storage.c_str();
}
```

四个 W 变体全部接入（`CreateProcessW_hook`、`API110/111/112CreateProcessW_hook`），每个 hook 结构：

```cpp
const bool inject = ShouldInject(lpApplicationName, lpCommandLine);
std::wstring patchedCmd;
LPCWSTR cmd = inject ? PatchChildCommandLine(lpCommandLine, patchedCmd) : lpCommandLine;

return Hooked_CreateProcess("CreateProcessW",
    [=](DWORD flags, LPVOID env, LPPROCESS_INFORMATION pi) {
      return syshooks.CreateProcessW()(lpApplicationName, (LPWSTR)cmd, /* ... */);
    },
    dwCreationFlags, inject, lpEnvironment, lpProcessInformation);
```

注意点：

- `lpCommandLine` 是 `__inout_opt LPWSTR`，但**缓冲区容量未知**，不能原地追加；必须用局部 `std::wstring` 复制后传指针（lambda 同步执行，生命周期安全）
- 这个 codebase 的 `rdcwstr` 是私有继承 `rdcarray<wchar_t>` 的类，没有 `empty/push_back/+=`，只能用 `std::wstring`

---

## 4. 核心修改 C：子注入强制开关

APC 注入会让目标**立即开跑**，而 `INTERNAL_SetCaptureOptions`（含 `hookIntoChildren`）是注入之后才推送的，可能晚于壳创建 Client 的时刻。所以子注入判定不能依赖那个选项：

```cpp
static bool ForceChildInject()
{
  static const bool force = []() -> bool {
    char buf[8] = {0};
    const DWORD len = GetEnvironmentVariableA("TCM_FORCE_CHILD_INJECT", buf, (DWORD)sizeof(buf));
    return len > 0 && buf[0] == '1';
  }();
  return force;
}

static bool ShouldInject(LPCWSTR lpApplicationName, LPCWSTR lpCommandLine)
{
  if(!RenderDoc::Inst().GetCaptureOptions().hookIntoChildren && !ForceChildInject())
    return false;
  ...
}
```

`ShouldInject` 的 A / W 两个重载都要改（A 版有自己的 early-return）。

---

## 5. 核心修改 D：ACE / CrashSight 黑名单

```cpp
static bool IsSkipInjectTarget(const rdcstr &lowerpath)
{
  static const char *skip[] = {
      "tinecmatoolcmd.exe", "qtinecmatool.exe", "platformprocess.exe",
      "ace-setup", "ace-tray", "ace-game", "ace-service", "aceservice", "ace-base",
      "anticheatexpert", "sguard", "sgutray",
      "crashsight", "crashreport", "kurosdk",
  };
  for(const char *s : skip)
    if(lowerpath.contains(s)) return true;
  return false;
}
```

原因：把 DLL 注进 `ACE-Setup64.exe` 会让 ACE 重新部署内核驱动（`ACE-BASE.sys` mtime 变化），随后游戏进程被终止。历史上 01:25 / 02:35 两次「注入成功但 8 秒后消失」都是这个原因。

---

## 6. 使用流程

```bat
tools\launch_rdc.bat            :: -krqlv=hd（默认）
tools\launch_rdc.bat sd         :: -krqlv=sd
```

脚本内部：

```bat
taskkill /f /im qTinecmaTool.exe        :: 旧实例会保留旧环境变量，必须先清
set "TCM_CHILD_ARGS=-krqlv=hd"
set "TCM_FORCE_CHILD_INJECT=1"
start "" "...\x64\Development\qTinecmaTool.exe"
```

然后在 GUI 里：

| 字段 | 值 |
|---|---|
| Executable | `D:\Wuthering Waves\Wuthering Waves Game\Wuthering Waves.exe`（**壳**，不是 Client、不是 launcher） |
| Capture Child Processes | ✔ |
| Global hook | ✘ |

进游戏后按 **F12** 抓帧。

> 环境变量必须由 GUI 继承，所以不要"先开 GUI 再设变量"，也不要双击 bat 之外手动开一个干净 GUI 再指望它生效。

---

## 7. 验证判据

`%TEMP%\TinecmaTool\RenderDoc_*.log` 里应依次出现：

```
Running process ...\Wuthering Waves.exe
Injecting renderdoc into process <壳PID>
Injected ... via early-bird APC, no remote thread created     ← 关键行
Got remote handshake: Wuthering Waves [<壳PID>]
Intercepting CreateProcessW
Got a new child process: <ClientPID> 38921
```

| 现象 | 含义 |
|---|---|
| `APC injection failed, falling back to CreateRemoteThread` | APC 未投递，日志带错误码，会退回远程线程（可能被杀） |
| 有 handshake、没有 `Intercepting CreateProcessW` | 壳没创建子进程，或 `TCM_FORCE_CHILD_INJECT` 未生效 |
| Client 起来但弹 `Use launcher to start game!` | `TCM_CHILD_ARGS` 没继承到 / 档位值与安装资源不符（`Client/Content/` 下只有 HD 就必须用 `hd`） |

---

## 8. 已知限制 / 后续可做

1. **仍有 4 个远程线程**：`INTERNAL_SetCaptureFile` / `SetDebugLogFile` / `SetCaptureOptions` / `GetTargetControlIdent` 还是走 `InjectFunctionCall` 的 `CreateRemoteThread`。当前实测能过，但要更彻底，可改成同一套 APC 握手流程（需要额外的一轮 rendezvous，我留了注释没动）。
2. **不支持"启动即抓帧"**（frame 0 queue capture）：因为 APC 路径让目标提前运行，`INTERNAL_SetCaptureOptions` 可能晚到，队列抓帧参数会丢。F12 手动抓帧不受影响。
3. **导出表未清理**：`TinecmaTool.dll` 仍导出 53 个 `RENDERDOC_*`（`RENDERDOC_GetAPI`、`RENDERDOC_StartGlobalHook`…）和 7 个 `VK_LAYER_TINECMATOOL_*`。如果后续遇到模块级扫描再次被杀，这是第一个该动的地方（等长改名即可，注意同步 `qTinecmaTool.exe` / `TinecmaToolcmd.exe` 的导入名）。
4. **对运行中进程注入仍不可用**：ACE 锁定后 `OpenProcess` 直接 `ERROR_ACCESS_DENIED(5)`，只能走"创建时注入"。

---

## 9. 回滚

任何一步出问题都可单独退回原行为：

| 改动 | 回滚方式 |
|---|---|
| APC 注入 | 不传 `hSuspendedThread`（或让 `InjectDLLViaAPC` 返回 false）即自动回退 `CreateRemoteThread` |
| 命令行注入 | 不设 `TCM_CHILD_ARGS` |
| 强制子注入 | 不设 `TCM_FORCE_CHILD_INJECT` |
| 黑名单 | 从 `IsSkipInjectTarget` 的数组里删相应项 |

---

## 附录：改动文件清单

| 文件 | 改动 |
|---|---|
| `renderdoc/os/win32/win32_process.cpp` | 新增 `InjectDLLViaAPC()`；`InjectDLL()` 增加 `hSuspendedThread` 参数与分派；`Process::InjectIntoProcess` 增加参数；`LaunchAndInjectIntoProcess` 传 `pi.hThread` |
| `renderdoc/os/win32/sys_win32_hooks.cpp` | 新增 `IsSkipInjectTarget()` / `ForceChildInject()` / `PatchChildCommandLine()`；4 个 W `CreateProcess` hook 接入参数改写；子进程注入传 `hThread` |
| `renderdoc/os/os_specific.h` | `InjectIntoProcess` 签名增加可选 `void *hSuspendedThread = NULL` |
| `renderdoc/os/posix/posix_process.cpp` | 形参同步（忽略） |
| `tools/launch_rdc.bat` | 新增：一键设置环境变量并启动 GUI |
| `tools/diag_wuwa_launch.ps1` | 新增：启动链 / 退出码 / 模块时间线诊断 |
