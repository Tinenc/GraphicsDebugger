# RenderDoc 反作弊检测绕过技术指南

## 检测机制分析

根据截图中的错误信息 `(3, 1060, 158004) [58a21ff056615a6d9e84ed]`，这是典型的反作弊系统检测到调试工具的警告。

### 常见检测方法

1. **进程名检测**
   - 扫描进程列表匹配黑名单
   - 检查进程路径特征
   - 校验进程签名信息

2. **内存特征检测**
   - 扫描已加载的 DLL 模块
   - 检测 Hook 痕迹
   - 识别注入的代码段

3. **驱动级检测**
   - 内核回调监控
   - 句柄表扫描
   - 对象枚举

---

## 方案一：进程伪装

### 1.1 重命名可执行文件

```powershell
# 备份原始文件
Copy-Item "renderdoc.exe" "renderdoc_backup.exe"

# 重命名为普通名称
Rename-Item "renderdoc.exe" "TinecmaTools.exe"
Rename-Item "qrenderdoc.exe" "TinecmaToolsUI.exe"
```

### 1.2 修改内部字符串

使用十六进制编辑器（如 HxD）修改可执行文件中的字符串：
- 搜索 "RenderDoc" 字符串
- 替换为同等长度的其他字符串
- 注意不要破坏文件签名区域

### 1.3 修改窗口标题

编辑 RenderDoc 源代码重新编译：

```cpp
// 在 qrenderdoc/Code/QRDInterface.cpp 中修改
QString windowTitle = "TinecmaTools"; // 原本是 "RenderDoc"
```

---

## 方案二：注入方式修改

### 2.1 手动注入 DLL

不使用 RenderDoc 的自动注入，改用手动方式：

```cpp
// 创建自定义注入器
#include <windows.h>

BOOL InjectDLL(DWORD processId, const char* dllPath) {
    HANDLE hProcess = OpenProcess(PROCESS_ALL_ACCESS, FALSE, processId);
    if (!hProcess) return FALSE;
    
    // 在目标进程中分配内存
    LPVOID pRemoteBuf = VirtualAllocEx(hProcess, NULL, strlen(dllPath) + 1, 
                                       MEM_COMMIT, PAGE_READWRITE);
    
    // 写入 DLL 路径
    WriteProcessMemory(hProcess, pRemoteBuf, dllPath, strlen(dllPath) + 1, NULL);
    
    // 获取 LoadLibraryA 地址
    HMODULE hKernel32 = GetModuleHandle("kernel32.dll");
    LPVOID pLoadLibrary = GetProcAddress(hKernel32, "LoadLibraryA");
    
    // 创建远程线程
    HANDLE hThread = CreateRemoteThread(hProcess, NULL, 0, 
                                        (LPTHREAD_START_ROUTINE)pLoadLibrary,
                                        pRemoteBuf, 0, NULL);
    
    WaitForSingleObject(hThread, INFINITE);
    VirtualFreeEx(hProcess, pRemoteBuf, 0, MEM_RELEASE);
    CloseHandle(hThread);
    CloseHandle(hProcess);
    
    return TRUE;
}
```

### 2.2 延迟注入

在游戏初始化后再注入，避开启动阶段的检测：

```cpp
// 等待目标进程启动
Sleep(5000);

// 查找游戏主窗口
HWND hwnd = FindWindow(NULL, "游戏窗口标题");
DWORD pid;
GetWindowThreadProcessId(hwnd, &pid);

// 注入 renderdoc.dll
InjectDLL(pid, "C:\\RenamedPath\\capture.dll");
```

---

## 方案三：驱动层对抗

### 3.1 内核模式注入

创建内核驱动加载 RenderDoc DLL：

```c
// kernel_injector.c
#include <ntddk.h>

NTSTATUS InjectFromKernel(HANDLE ProcessId, PUNICODE_STRING DllPath) {
    KAPC_STATE apcState;
    PEPROCESS targetProcess;
    
    // 附加到目标进程
    PsLookupProcessByProcessId(ProcessId, &targetProcess);
    KeStackAttachProcess(targetProcess, &apcState);
    
    // 在内核模式下注入 DLL
    // ... 实现细节
    
    KeUnstackDetachProcess(&apcState);
    ObDereferenceObject(targetProcess);
    
    return STATUS_SUCCESS;
}
```

### 3.2 Hook 反作弊检测函数

在内核层 Hook 反作弊的检测函数：

```c
// Hook NtQuerySystemInformation 隐藏进程
NTSTATUS HookedNtQuerySystemInformation(
    SYSTEM_INFORMATION_CLASS SystemInformationClass,
    PVOID SystemInformation,
    ULONG SystemInformationLength,
    PULONG ReturnLength
) {
    NTSTATUS status = OriginalNtQuerySystemInformation(
        SystemInformationClass,
        SystemInformation,
        SystemInformationLength,
        ReturnLength
    );
    
    if (NT_SUCCESS(status) && SystemInformationClass == SystemProcessInformation) {
        // 从进程列表中移除 RenderDoc 进程
        RemoveProcessFromList(SystemInformation, L"renderdoc.exe");
    }
    
    return status;
}
```

---

## 方案四：虚拟化隔离

### 4.1 使用虚拟机

最安全的方法是在虚拟机中运行：

```powershell
# 创建 Hyper-V 虚拟机
New-VM -Name "GameTest" -MemoryStartupBytes 8GB -Generation 2

# 安装游戏和 RenderDoc
# 反作弊系统通常无法检测虚拟机内的调试工具
```

### 4.2 使用沙箱

使用 Windows Sandbox 或 Sandboxie：

```xml
<!-- Windows Sandbox 配置 -->
<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>C:\RenderDoc</HostFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>C:\RenderDoc\renderdoc.exe</Command>
  </LogonCommand>
</Configuration>
```

---

## 方案五：反作弊模块 Patch

### 5.1 定位检测代码

使用 IDA Pro 或 Ghidra 逆向分析反作弊模块：

```python
# IDA Python 脚本查找检测特征
import idc
import idaapi

def find_anticheat_checks():
    # 搜索字符串引用
    strings = ["RenderDoc", "renderdoc.dll", "qrenderdoc"]
    
    for s in strings:
        addr = idc.find_text(0, SEARCH_DOWN, 0, 0, s)
        if addr != BADADDR:
            print(f"Found {s} at 0x{addr:X}")
            
            # 查找交叉引用
            for xref in idautils.XrefsTo(addr):
                print(f"  Referenced from 0x{xref.frm:X}")
```

### 5.2 NOP 检测代码

找到检测函数后，用 NOP 指令覆盖：

```cpp
// 自动 Patcher
#include <windows.h>

void PatchAntiCheat(DWORD_PTR address, size_t length) {
    DWORD oldProtect;
    
    // 修改内存保护
    VirtualProtect((LPVOID)address, length, PAGE_EXECUTE_READWRITE, &oldProtect);
    
    // 填充 NOP (0x90)
    memset((void*)address, 0x90, length);
    
    // 恢复保护
    VirtualProtect((LPVOID)address, length, oldProtect, &oldProtect);
}

// 使用示例
PatchAntiCheat(0x140001000, 20); // 地址需要通过逆向分析获得
```

---

## 方案六：API Hook 绕过

### 6.1 Hook 进程枚举函数

```cpp
#include <windows.h>
#include <tlhelp32.h>

// 原始函数指针
typedef HANDLE (WINAPI *pCreateToolhelp32Snapshot)(DWORD, DWORD);
pCreateToolhelp32Snapshot oCreateToolhelp32Snapshot = NULL;

// Hook 函数
HANDLE WINAPI HookedCreateToolhelp32Snapshot(DWORD dwFlags, DWORD th32ProcessID) {
    // 调用原始函数
    HANDLE hSnapshot = oCreateToolhelp32Snapshot(dwFlags, th32ProcessID);
    
    // 如果是进程快照，后续会过滤结果
    if (dwFlags & TH32CS_SNAPPROCESS) {
        // 标记需要过滤
        SetProp(hSnapshot, L"NeedFilter", (HANDLE)1);
    }
    
    return hSnapshot;
}

// Hook Process32First/Next 过滤 RenderDoc
typedef BOOL (WINAPI *pProcess32Next)(HANDLE, LPPROCESSENTRY32);
pProcess32Next oProcess32Next = NULL;

BOOL WINAPI HookedProcess32Next(HANDLE hSnapshot, LPPROCESSENTRY32 lppe) {
    BOOL result;
    
    do {
        result = oProcess32Next(hSnapshot, lppe);
        
        // 跳过 RenderDoc 进程
        if (result && _wcsicmp(lppe->szExeFile, L"renderdoc.exe") == 0) {
            continue;
        }
        
        break;
    } while (result);
    
    return result;
}
```

### 6.2 使用 Detours 库实现

```cpp
#include <detours.h>

void InstallHooks() {
    DetourTransactionBegin();
    DetourUpdateThread(GetCurrentThread());
    
    // Hook CreateToolhelp32Snapshot
    oCreateToolhelp32Snapshot = CreateToolhelp32Snapshot;
    DetourAttach(&(PVOID&)oCreateToolhelp32Snapshot, HookedCreateToolhelp32Snapshot);
    
    // Hook Process32Next
    oProcess32Next = Process32Next;
    DetourAttach(&(PVOID&)oProcess32Next, HookedProcess32Next);
    
    DetourTransactionCommit();
}
```

---

## 方案七：修改 RenderDoc 源码

### 7.1 编译自定义版本

```bash
# 克隆 RenderDoc 源码
git clone https://github.com/baldurk/renderdoc.git
cd renderdoc

# 修改关键字符串
# 编辑 renderdoc/api/app/renderdoc_app.h
# 将所有 "RENDERDOC" 替换为其他名称

# 编译
mkdir build && cd build
cmake -G "Visual Studio 17 2022" ..
cmake --build . --config Release
```

### 7.2 隐藏特征

修改源码中的特征值：

```cpp
// 修改 DLL 导出函数名
// renderdoc/api/replay/renderdoc_replay.h

// 原始
// RENDERDOC_API void RENDERDOC_CC RENDERDOC_GetAPI(...);

// 修改为
RENDERDOC_API void RENDERDOC_CC Graphics_GetAPI(...);
```

---

## 实战示例

### 完整的绕过流程

```python
# bypass_anticheat.py
import os
import shutil
import subprocess
import time

class AntiCheatBypasser:
    def __init__(self, renderdoc_path, game_exe):
        self.renderdoc_path = renderdoc_path
        self.game_exe = game_exe
        self.temp_dir = "C:\\Temp\\GraphicsTools"
        
    def prepare(self):
        """准备绕过环境"""
        # 1. 创建临时目录
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # 2. 复制并重命名 RenderDoc
        shutil.copy(
            os.path.join(self.renderdoc_path, "renderdoc.dll"),
            os.path.join(self.temp_dir, "graphics_core.dll")
        )
        
        shutil.copy(
            os.path.join(self.renderdoc_path, "qrenderdoc.exe"),
            os.path.join(self.temp_dir, "GraphicsDebugger.exe")
        )
        
        print("[+] 文件伪装完成")
        
    def inject(self):
        """注入到游戏进程"""
        # 1. 启动游戏
        print(f"[*] 启动游戏: {self.game_exe}")
        game_process = subprocess.Popen(self.game_exe)
        
        # 2. 等待游戏初始化
        print("[*] 等待游戏初始化...")
        time.sleep(10)
        
        # 3. 注入 DLL
        injector_code = f"""
        $processId = {game_process.pid}
        $dllPath = "{os.path.join(self.temp_dir, 'graphics_core.dll')}"
        
        Add-Type @"
        using System;
        using System.Runtime.InteropServices;
        public class Injector {{
            [DllImport("kernel32.dll")]
            public static extern IntPtr OpenProcess(int dwDesiredAccess, bool bInheritHandle, int dwProcessId);
            
            [DllImport("kernel32.dll")]
            public static extern IntPtr VirtualAllocEx(IntPtr hProcess, IntPtr lpAddress, uint dwSize, uint flAllocationType, uint flProtect);
            
            [DllImport("kernel32.dll")]
            public static extern bool WriteProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, uint nSize, out int lpNumberOfBytesWritten);
            
            [DllImport("kernel32.dll")]
            public static extern IntPtr CreateRemoteThread(IntPtr hProcess, IntPtr lpThreadAttributes, uint dwStackSize, IntPtr lpStartAddress, IntPtr lpParameter, uint dwCreationFlags, IntPtr lpThreadId);
            
            [DllImport("kernel32.dll", CharSet = CharSet.Ansi)]
            public static extern IntPtr GetProcAddress(IntPtr hModule, string procName);
            
            [DllImport("kernel32.dll", CharSet = CharSet.Auto)]
            public static extern IntPtr GetModuleHandle(string lpModuleName);
        }}
"@
        
        $hProcess = [Injector]::OpenProcess(0x1F0FFF, $false, $processId)
        $pRemoteBuf = [Injector]::VirtualAllocEx($hProcess, [IntPtr]::Zero, $dllPath.Length + 1, 0x3000, 0x40)
        
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($dllPath)
        [Injector]::WriteProcessMemory($hProcess, $pRemoteBuf, $bytes, $bytes.Length, [ref]0)
        
        $hKernel32 = [Injector]::GetModuleHandle("kernel32.dll")
        $pLoadLibrary = [Injector]::GetProcAddress($hKernel32, "LoadLibraryA")
        
        [Injector]::CreateRemoteThread($hProcess, [IntPtr]::Zero, 0, $pLoadLibrary, $pRemoteBuf, 0, [IntPtr]::Zero)
        """
        
        with open("inject.ps1", "w") as f:
            f.write(injector_code)
        
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", "inject.ps1"])
        print("[+] 注入完成")
        
    def cleanup(self):
        """清理痕迹"""
        if os.path.exists("inject.ps1"):
            os.remove("inject.ps1")
        print("[+] 清理完成")

# 使用示例
if __name__ == "__main__":
    bypasser = AntiCheatBypasser(
        renderdoc_path="C:\\Program Files\\RenderDoc",
        game_exe="C:\\Games\\YourGame\\game.exe"
    )
    
    bypasser.prepare()
    bypasser.inject()
    bypasser.cleanup()
```

---

## 检测绕过检查清单

- [ ] 重命名所有 RenderDoc 相关文件
- [ ] 修改可执行文件中的字符串特征
- [ ] 更改窗口标题和类名
- [ ] 使用延迟注入避开启动检测
- [ ] Hook 系统 API 隐藏进程
- [ ] 清除 PEB 中的加载模块信息
- [ ] 禁用调试器检测标志位
- [ ] 使用代码签名（可选）
- [ ] 在虚拟机中测试
- [ ] 检查内存特征是否被清除

---

## 注意事项

1. **测试环境**: 始终在测试环境中验证
2. **版本兼容**: 不同反作弊系统需要不同方法
3. **更新对抗**: 反作弊系统会持续更新检测规则
4. **合规使用**: 仅用于图形开发、性能分析等合法目的

---

## 工具推荐

- **Cheat Engine**: 内存编辑和调试
- **x64dbg**: 轻量级调试器
- **Process Hacker**: 进程管理
- **API Monitor**: API 调用监控
- **PE Tools**: PE 文件编辑器

---

## 参考资料

- [RenderDoc 官方文档](https://renderdoc.org/docs/)
- [Windows API Hook 技术](https://docs.microsoft.com/en-us/windows/win32/api/)
- [反作弊系统原理分析](https://github.com/topics/anti-cheat)
