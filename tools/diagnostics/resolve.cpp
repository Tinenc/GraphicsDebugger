#include <windows.h>
#include <dbghelp.h>
#include <stdio.h>

#pragma comment(lib, "dbghelp.lib")

int main() {
    HANDLE hProcess = GetCurrentProcess();
    SymInitialize(hProcess, NULL, FALSE);

    const char* dllPath = "C:\\Program Files\\GraphicsDebugger-Steam-Cod\\x64\\Development\\TinecmaTool.dll";
    DWORD64 base = 0x180000000;
    DWORD64 mod = SymLoadModuleEx(hProcess, NULL, dllPath, NULL, base, 0, NULL, 0);
    printf("SymLoadModuleEx: %llx\n", mod);

    DWORD64 target = base + 0x840E02; // RVA 0x840E02

    char buffer[sizeof(SYMBOL_INFO) + MAX_SYM_NAME * sizeof(TCHAR)] = {0};
    PSYMBOL_INFO pSym = (PSYMBOL_INFO)buffer;
    pSym->SizeOfStruct = sizeof(SYMBOL_INFO);
    pSym->MaxNameLen = MAX_SYM_NAME;

    DWORD64 disp = 0;
    if (SymFromAddr(hProcess, target, &disp, pSym)) {
        printf("Symbol: %s + 0x%llX (addr: 0x%llX)\n", pSym->Name, disp, pSym->Address);
    } else {
        printf("SymFromAddr failed: %lu\n", GetLastError());
    }

    IMAGEHLP_LINE64 line = {0};
    line.SizeOfStruct = sizeof(IMAGEHLP_LINE64);
    DWORD dispLine = 0;
    if (SymGetLineFromAddr64(hProcess, target, &dispLine, &line)) {
        printf("Line: %s:%lu (+0x%lx)\n", line.FileName, line.LineNumber, dispLine);
    } else {
        printf("SymGetLineFromAddr64 failed: %lu\n", GetLastError());
    }

    return 0;
}
