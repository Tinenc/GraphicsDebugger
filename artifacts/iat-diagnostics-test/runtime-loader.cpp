#include <windows.h>
#include <cstdio>
int wmain(int argc, wchar_t **argv) {
  SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
  if(argc != 2) return 2;
  HMODULE runtime = LoadLibraryExW(argv[1], NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
  printf("runtime=%p error=%lu\n", runtime, runtime ? 0 : GetLastError()); fflush(stdout);
  if(!runtime) return 3;
  HMODULE capture = LoadLibraryW(L"E:\\GraphicsDebugger\\Win32\\Development\\TinecmaTool.dll");
  printf("capture=%p error=%lu\n", capture, capture ? 0 : GetLastError()); fflush(stdout);
  ExitProcess(capture ? 0 : 4);
}
