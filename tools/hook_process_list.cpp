// Hook 进程列表以隐藏 RenderDoc
// 编译: cl /LD hook_process_list.cpp /link /DEF:exports.def

#include <windows.h>
#include <tlhelp32.h>
#include <string>
#include <vector>

// 要隐藏的进程名列表
std::vector<std::wstring> hiddenProcesses = {
    L"renderdoc.exe",
    L"qrenderdoc.exe",
    L"renderdoccmd.exe"
};

// 原始函数指针
typedef HANDLE (WINAPI *pCreateToolhelp32Snapshot)(DWORD, DWORD);
typedef BOOL (WINAPI *pProcess32First)(HANDLE, LPPROCESSENTRY32);
typedef BOOL (WINAPI *pProcess32Next)(HANDLE, LPPROCESSENTRY32);

pCreateToolhelp32Snapshot oCreateToolhelp32Snapshot = NULL;
pProcess32First oProcess32First = NULL;
pProcess32Next oProcess32Next = NULL;

// 检查是否是要隐藏的进程
BOOL IsHiddenProcess(LPCWSTR processName) {
    for (const auto& hidden : hiddenProcesses) {
        if (_wcsicmp(processName, hidden.c_str()) == 0) {
            return TRUE;
        }
    }
    return FALSE;
}

// Hook CreateToolhelp32Snapshot
HANDLE WINAPI HookedCreateToolhelp32Snapshot(DWORD dwFlags, DWORD th32ProcessID) {
    if (!oCreateToolhelp32Snapshot) {
        oCreateToolhelp32Snapshot = (pCreateToolhelp32Snapshot)GetProcAddress(
            GetModuleHandleW(L"kernel32.dll"), "CreateToolhelp32Snapshot");
    }
    return oCreateToolhelp32Snapshot(dwFlags, th32ProcessID);
}

// Hook Process32First
BOOL WINAPI HookedProcess32First(HANDLE hSnapshot, LPPROCESSENTRY32 lppe) {
    if (!oProcess32First) {
        oProcess32First = (pProcess32First)GetProcAddress(
            GetModuleHandleW(L"kernel32.dll"), "Process32First");
    }
    
    BOOL result;
    do {
        result = oProcess32First(hSnapshot, lppe);
        if (!result) return FALSE;
        
        // 如果不是要隐藏的进程,返回
        if (!IsHiddenProcess(lppe->szExeFile)) {
            return TRUE;
        }
        
        // 是要隐藏的进程,继续下一个
        oProcess32First = (pProcess32First)GetProcAddress(
            GetModuleHandleW(L"kernel32.dll"), "Process32Next");
            
    } while (result);
    
    return FALSE;
}

// Hook Process32Next
BOOL WINAPI HookedProcess32Next(HANDLE hSnapshot, LPPROCESSENTRY32 lppe) {
    if (!oProcess32Next) {
        oProcess32Next = (pProcess32Next)GetProcAddress(
            GetModuleHandleW(L"kernel32.dll"), "Process32Next");
    }
    
    BOOL result;
    do {
        result = oProcess32Next(hSnapshot, lppe);
        if (!result) return FALSE;
        
        // 如果不是要隐藏的进程,返回
        if (!IsHiddenProcess(lppe->szExeFile)) {
            return TRUE;
        }
        
        // 是要隐藏的进程,继续下一个
    } while (result);
    
    return FALSE;
}

// DLL 入口点
BOOL APIENTRY DllMain(HMODULE hModule, DWORD ul_reason_for_call, LPVOID lpReserved) {
    switch (ul_reason_for_call) {
    case DLL_PROCESS_ATTACH:
        DisableThreadLibraryCalls(hModule);
        
        // 获取原始函数地址
        HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
        oCreateToolhelp32Snapshot = (pCreateToolhelp32Snapshot)GetProcAddress(hKernel32, "CreateToolhelp32Snapshot");
        oProcess32First = (pProcess32First)GetProcAddress(hKernel32, "Process32First");
        oProcess32Next = (pProcess32Next)GetProcAddress(hKernel32, "Process32Next");
        
        break;
        
    case DLL_PROCESS_DETACH:
        break;
    }
    return TRUE;
}

// 导出函数
extern "C" __declspec(dllexport) void InstallHooks() {
    // 这里可以使用 Detours 或 MinHook 库来安装 Hook
    // 示例代码仅展示思路
}

extern "C" __declspec(dllexport) void RemoveHooks() {
    // 移除 Hook
}

extern "C" __declspec(dllexport) void AddHiddenProcess(LPCWSTR processName) {
    hiddenProcesses.push_back(processName);
}
