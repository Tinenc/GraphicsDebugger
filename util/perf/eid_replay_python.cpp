// Minimal Python 3.6 host for isolated offline replay profiling. Keeping the host
// beside renderdoc.dll keeps native EID timing logs inside the selected build.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string>

// Mark this process as a replay host before Python loads any RenderDoc module.
extern "C" __declspec(dllexport) void renderdoc__replay__marker()
{
}

int wmain(int argc, wchar_t **argv)
{
  wchar_t executable[32768] = {};
  DWORD length = GetModuleFileNameW(NULL, executable, 32768);
  if(length == 0 || length >= 32768)
    return 120;
  std::wstring directory(executable, length);
  directory.resize(directory.find_last_of(L"\\/"));
  if(!SetDllDirectoryW(directory.c_str()))
    return 121;
  std::wstring libraryPath = directory + L"\\python36.dll";
  HMODULE library = LoadLibraryExW(libraryPath.c_str(), NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
  if(!library)
  {
    fwprintf(stderr, L"Cannot load bundled Python: %ls (%lu)\n", libraryPath.c_str(), GetLastError());
    return 122;
  }
  typedef void(__cdecl *SetWide)(const wchar_t *);
  typedef int(__cdecl *PythonMain)(int, wchar_t **);
  SetWide setHome = (SetWide)GetProcAddress(library, "Py_SetPythonHome");
  SetWide setPath = (SetWide)GetProcAddress(library, "Py_SetPath");
  SetWide setProgram = (SetWide)GetProcAddress(library, "Py_SetProgramName");
  PythonMain pythonMain = (PythonMain)GetProcAddress(library, "Py_Main");
  if(!setHome || !setPath || !setProgram || !pythonMain)
    return 123;
  std::wstring searchPath = directory + L"\\python36.zip;" + directory + L";" + directory + L"\\pymodules";
  setHome(directory.c_str());
  setPath(searchPath.c_str());
  setProgram(executable);
  return pythonMain(argc, argv);
}
