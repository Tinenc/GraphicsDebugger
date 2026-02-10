# DLL 注入工具
# 用于手动注入 RenderDoc DLL 到目标进程

param(
    [Parameter(Mandatory=$true)]
    [string]$ProcessName,
    
    [Parameter(Mandatory=$true)]
    [string]$DllPath,
    
    [Parameter(Mandatory=$false)]
    [int]$Delay = 5
)

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "DLL 注入工具" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# 检查 DLL 是否存在
if (-not (Test-Path $DllPath)) {
    Write-Host "[错误] DLL 文件不存在: $DllPath" -ForegroundColor Red
    exit 1
}

# 等待延迟
if ($Delay -gt 0) {
    Write-Host "[*] 等待 $Delay 秒后注入..." -ForegroundColor Yellow
    Start-Sleep -Seconds $Delay
}

# 查找目标进程
Write-Host "[*] 查找进程: $ProcessName" -ForegroundColor Yellow
$process = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue

if (-not $process) {
    Write-Host "[错误] 进程未找到: $ProcessName" -ForegroundColor Red
    exit 1
}

$processId = $process.Id
Write-Host "[+] 找到进程 PID: $processId" -ForegroundColor Green

# 创建注入代码
$injectorCode = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public class DllInjector
{
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr VirtualAllocEx(IntPtr hProcess, IntPtr lpAddress, uint dwSize, uint flAllocationType, uint flProtect);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool WriteProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, uint nSize, out int lpNumberOfBytesWritten);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr CreateRemoteThread(IntPtr hProcess, IntPtr lpThreadAttributes, uint dwStackSize, IntPtr lpStartAddress, IntPtr lpParameter, uint dwCreationFlags, out IntPtr lpThreadId);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    public static extern IntPtr GetProcAddress(IntPtr hModule, string procName);

    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern IntPtr GetModuleHandle(string lpModuleName);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

    const uint PROCESS_ALL_ACCESS = 0x1F0FFF;
    const uint MEM_COMMIT = 0x1000;
    const uint MEM_RESERVE = 0x2000;
    const uint PAGE_READWRITE = 0x04;
    const uint INFINITE = 0xFFFFFFFF;

    public static bool Inject(int processId, string dllPath)
    {
        IntPtr hProcess = OpenProcess(PROCESS_ALL_ACCESS, false, processId);
        if (hProcess == IntPtr.Zero)
        {
            Console.WriteLine("[错误] 无法打开进程");
            return false;
        }

        byte[] dllBytes = Encoding.ASCII.GetBytes(dllPath);
        IntPtr pRemoteBuf = VirtualAllocEx(hProcess, IntPtr.Zero, (uint)dllBytes.Length + 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        
        if (pRemoteBuf == IntPtr.Zero)
        {
            Console.WriteLine("[错误] 无法分配内存");
            CloseHandle(hProcess);
            return false;
        }

        int bytesWritten;
        if (!WriteProcessMemory(hProcess, pRemoteBuf, dllBytes, (uint)dllBytes.Length, out bytesWritten))
        {
            Console.WriteLine("[错误] 无法写入内存");
            CloseHandle(hProcess);
            return false;
        }

        IntPtr hKernel32 = GetModuleHandle("kernel32.dll");
        IntPtr pLoadLibrary = GetProcAddress(hKernel32, "LoadLibraryA");

        if (pLoadLibrary == IntPtr.Zero)
        {
            Console.WriteLine("[错误] 无法获取 LoadLibraryA 地址");
            CloseHandle(hProcess);
            return false;
        }

        IntPtr threadId;
        IntPtr hThread = CreateRemoteThread(hProcess, IntPtr.Zero, 0, pLoadLibrary, pRemoteBuf, 0, out threadId);

        if (hThread == IntPtr.Zero)
        {
            Console.WriteLine("[错误] 无法创建远程线程");
            CloseHandle(hProcess);
            return false;
        }

        WaitForSingleObject(hThread, INFINITE);
        
        CloseHandle(hThread);
        CloseHandle(hProcess);

        return true;
    }
}
"@

try {
    Add-Type -TypeDefinition $injectorCode -Language CSharp
    
    Write-Host "[*] 开始注入 DLL..." -ForegroundColor Yellow
    $result = [DllInjector]::Inject($processId, $DllPath)
    
    if ($result) {
        Write-Host "[+] DLL 注入成功!" -ForegroundColor Green
    } else {
        Write-Host "[错误] DLL 注入失败" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "[错误] 注入过程出错: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[完成] 注入流程结束" -ForegroundColor Cyan
