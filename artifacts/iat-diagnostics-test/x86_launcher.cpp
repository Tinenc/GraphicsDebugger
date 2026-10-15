#include <windows.h>
#include <cstdio>
int wmain(int argc, wchar_t **argv) {
  if(argc != 2) return 1;
  STARTUPINFOW startup = {}; startup.cb = sizeof(startup);
  PROCESS_INFORMATION child = {};
  if(!CreateProcessW(argv[1], NULL, NULL, NULL, FALSE, 0, NULL, NULL, &startup, &child)) {
    printf("CreateProcess failed=%lu\n", GetLastError()); return 2;
  }
  DWORD waited = WaitForSingleObject(child.hProcess, 30000);
  DWORD result = 1; GetExitCodeProcess(child.hProcess, &result);
  CloseHandle(child.hThread); CloseHandle(child.hProcess);
  printf("x86 launcher -> x64 child: wait=%lu exit=%lu\n", waited, result);
  return waited == WAIT_OBJECT_0 ? (int)result : 3;
}
