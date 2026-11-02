// ============================================================================
// Naraka proxy layer - shared implementation
// ============================================================================
//
// The three proxies (dxgi.dll / d3d11.dll / d3d12.dll) are the only way into
// NarakaBladepoint.exe. Every injection route is closed:
//
//   * Injecting LauncherGame.exe is pointless - it is an x86 .NET shell that
//     exits within a second by design.
//   * Injecting StartGame.exe works, but blocks Neac from initialising, so the
//     game never launches (docs/NARAKA_挂载方案_StartGame_2026-10-24.md §9).
//   * Injecting the game itself afterwards is too late - the device exists.
//   * Patching the game's IAT from inside is a dead end: VMProtect reinstates
//     its own import table during unpacking, so the patch is gone before the
//     first call (same doc, §7 - "Graphics function resolved" count was 0).
//
// So we stop trying to rewrite the game's imports and become one of them
// instead. The Windows loader searches the executable's own directory first,
// and UnityPlayer.dll imports CreateDXGIFactory statically while d3d11.dll /
// d3d12.dll are resolved by name at runtime - all three resolve to these DLLs.
// Nothing in the game's image is modified, so the packer has nothing to undo.
//
// Each proxy re-exports its entire system export table and forwards everything
// it does not care about to %SystemRoot%\System32. Only the device/factory
// creation entry points are routed into the TinecmaTool core.
//
// If the core does not export the bridge (an older TinecmaTool.dll), the proxy
// forwards to the real function instead of failing. That keeps a half-finished
// deployment playable rather than crashing the game - and it is a visible,
// logged condition, not a silent one.
// ============================================================================

#ifndef NARAKA_PROXY_COMMON_H
#define NARAKA_PROXY_COMMON_H

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>
#include <share.h>
#include <shlobj.h>
#include <intrin.h>

#include <cstdio>
#include <cstdarg>
#include <cstring>
#include <string>

// ---------------------------------------------------------------------------
// Where things go
// ---------------------------------------------------------------------------
//
// The ini is read from the directory of the *host* executable (for the game:
// F:\Naraka\program). Logs and captures never go there - writing into the game
// install is exactly the kind of side effect that gets a directory flagged, and
// it also survives an incomplete uninstall.

namespace np {

enum class CaptureMode
{
  Off,
  RenderDoc,
  Pix,
  Both,
};

struct Config
{
  CaptureMode mode = CaptureMode::RenderDoc;
  std::string targetProcess = "narakabladepoint.exe";  // lowercase, exe base name
  std::string rdocDll;                                  // empty = probe defaults
  std::string captureDir;
  std::string logDir;
  std::string iniPath;
};

struct State
{
  Config cfg;

  FILE *log = nullptr;
  CRITICAL_SECTION logLock;

  std::string exePath;    // full path of the host executable
  std::string exeDir;     // directory of the host executable
  std::string exeName;    // lowercase base name, e.g. "narakabladepoint.exe"
  DWORD pid = 0;

  bool isTarget = false;  // this process is the renderer we capture

  HMODULE rdocModule = nullptr;
  bool rdocLoaded = false;
  void *rdocApi = nullptr;

  // Bridge exports. Null when the core predates them, see the header comment.
  void *pfnCreateDXGIFactory = nullptr;
  void *pfnCreateDXGIFactory1 = nullptr;
  void *pfnCreateDXGIFactory2 = nullptr;
  void *pfnD3D11CreateDevice = nullptr;
  void *pfnD3D11CreateDeviceAndSwapChain = nullptr;
  void *pfnD3D12CreateDevice = nullptr;

  volatile bool threadRunning = false;
  volatile bool capturing = false;
  bool monitorStarted = false;
};

inline State &S()
{
  static State s;
  return s;
}

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

inline std::string ToLower(std::string v)
{
  for(char &c : v)
    c = (char)tolower((unsigned char)c);
  return v;
}

inline bool EndsWith(const std::string &v, const char *suffix)
{
  size_t n = strlen(suffix);
  if(v.size() < n)
    return false;
  return _stricmp(v.c_str() + v.size() - n, suffix) == 0;
}

inline std::string StripFilename(const std::string &path)
{
  size_t p = path.find_last_of("\\/");
  return p == std::string::npos ? std::string() : path.substr(0, p);
}

inline std::string Basename(const std::string &path)
{
  size_t p = path.find_last_of("\\/");
  return p == std::string::npos ? path : path.substr(p + 1);
}

inline std::string JoinPath(const std::string &dir, const char *leaf)
{
  if(dir.empty())
    return std::string(leaf);
  std::string out = dir;
  if(out.back() != '\\' && out.back() != '/')
    out.push_back('\\');
  out += leaf;
  return out;
}

inline bool DirExists(const std::string &dir)
{
  DWORD a = GetFileAttributesA(dir.c_str());
  return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) != 0;
}

inline bool FileExists(const std::string &path)
{
  DWORD a = GetFileAttributesA(path.c_str());
  return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

inline void EnsureDir(const std::string &dir)
{
  if(dir.empty() || DirExists(dir))
    return;
  CreateDirectoryA(dir.c_str(), nullptr);
}

// %LOCALAPPDATA%\Temp\Naraka\proxy, falling back to %TEMP% and then the exe dir.
inline std::string DefaultWorkDir(const std::string &exeDir)
{
  char buf[MAX_PATH] = {0};
  DWORD n = GetEnvironmentVariableA("LOCALAPPDATA", buf, MAX_PATH);
  if(n > 0 && n < MAX_PATH)
    return JoinPath(JoinPath(buf, "Temp"), "Naraka\\proxy");

  n = GetEnvironmentVariableA("TEMP", buf, MAX_PATH);
  if(n > 0 && n < MAX_PATH)
    return JoinPath(buf, "Naraka\\proxy");

  return JoinPath(exeDir, "NarakaProxy");
}

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------
//
// Single shared log across all three proxies of one process, tagged with the DLL
// that wrote the line, so the ordering of dxgi/d3d11/d3d12 traffic is visible in
// one file instead of three.

inline void LogReady()
{
  State &s = S();
  if(s.log)
    return;

  InitializeCriticalSection(&s.logLock);

  std::string dir = s.cfg.logDir.empty() ? DefaultWorkDir(s.exeDir) : s.cfg.logDir;
  EnsureDir(dir);

  std::string path = JoinPath(dir, "naraka_proxy.log");
  s.log = _fsopen(path.c_str(), "a", _SH_DENYNO);

  if(s.log)
  {
    SYSTEMTIME st;
    GetLocalTime(&st);
    fprintf(s.log, "\n=== Naraka proxy layer attached [pid %lu %s] %04d-%02d-%02d %02d:%02d:%02d ===\n",
            s.pid, s.exeName.c_str(), st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    fflush(s.log);
  }
}

inline void Logf(const char *tag, const char *fmt, ...)
{
  State &s = S();
  if(!s.log)
    return;

  EnterCriticalSection(&s.logLock);
  fprintf(s.log, "[%lu:%s][%s] ", s.pid, s.exeName.c_str(), tag);
  va_list args;
  va_start(args, fmt);
  vfprintf(s.log, fmt, args);
  va_end(args);
  fprintf(s.log, "\n");
  fflush(s.log);
  LeaveCriticalSection(&s.logLock);
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

inline std::string ReadIniString(const std::string &ini, const char *key, const char *dflt)
{
  char buf[MAX_PATH * 2] = {0};
  GetPrivateProfileStringA("naraka", key, "", buf, sizeof(buf), ini.c_str());
  if(buf[0] == '\0')
    GetPrivateProfileStringA("proxy", key, dflt, buf, sizeof(buf), ini.c_str());
  if(buf[0] == '\0' && dflt)
    return std::string(dflt);
  return std::string(buf);
}

inline void LoadConfig()
{
  State &s = S();

  // <exe dir>\naraka_proxy.ini first, then the work dir, so a game install can
  // stay pristine and the config can live with the tooling instead.
  s.cfg.iniPath = JoinPath(s.exeDir, "naraka_proxy.ini");
  if(!FileExists(s.cfg.iniPath))
  {
    std::string alt = JoinPath(DefaultWorkDir(s.exeDir), "naraka_proxy.ini");
    if(FileExists(alt))
      s.cfg.iniPath = alt;
  }

  std::string mode = ToLower(ReadIniString(s.cfg.iniPath, "mode", "renderdoc"));
  if(mode == "off" || mode == "none")
    s.cfg.mode = CaptureMode::Off;
  else if(mode == "pix")
    s.cfg.mode = CaptureMode::Pix;
  else if(mode == "both")
    s.cfg.mode = CaptureMode::Both;
  else
    s.cfg.mode = CaptureMode::RenderDoc;

  std::string target = ToLower(ReadIniString(s.cfg.iniPath, "target_process", "narakabladepoint.exe"));
  if(!target.empty())
    s.cfg.targetProcess = target;

  s.cfg.rdocDll = ReadIniString(s.cfg.iniPath, "rdoc_dll", "");
  s.cfg.logDir = ReadIniString(s.cfg.iniPath, "log_dir", "");
  s.cfg.captureDir = ReadIniString(s.cfg.iniPath, "capture_dir", "");

  if(!s.cfg.logDir.empty())
    EnsureDir(s.cfg.logDir);
  if(s.cfg.captureDir.empty())
    s.cfg.captureDir = DefaultWorkDir(s.exeDir);
  EnsureDir(s.cfg.captureDir);
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

inline void InitState(const char *dllTag)
{
  State &s = S();

  s.pid = GetCurrentProcessId();

  char exe[MAX_PATH] = {0};
  GetModuleFileNameA(nullptr, exe, MAX_PATH);
  s.exePath = exe;
  s.exeDir = StripFilename(s.exePath);
  s.exeName = ToLower(Basename(s.exePath));

  LoadConfig();
  LogReady();

  s.isTarget = (s.exeName == s.cfg.targetProcess);

  Logf(dllTag, "loaded into %s (pid %lu)", s.exePath.c_str(), s.pid);
  Logf(dllTag, "gate: target_process=%s -> %s", s.cfg.targetProcess.c_str(),
       s.isTarget ? "CAPTURE" : "pass-through only");
  Logf(dllTag, "config: %s", s.cfg.iniPath.c_str());
  Logf(dllTag, "capture_dir: %s", s.cfg.captureDir.c_str());
}

// ---------------------------------------------------------------------------
// System DLL resolution
// ---------------------------------------------------------------------------
//
// Always by full System32 path. A bare LoadLibraryA("dxgi.dll") would find this
// proxy again and recurse.

inline HMODULE LoadSystemDll(const wchar_t *leaf)
{
  wchar_t sysDir[MAX_PATH];
  GetSystemDirectoryW(sysDir, MAX_PATH);
  wcscat_s(sysDir, L"\\");
  wcscat_s(sysDir, leaf);

  HMODULE mod = LoadLibraryW(sysDir);
  if(!mod)
    Logf("sysdll", "FAILED to load %ls (error %lu)", sysDir, GetLastError());
  return mod;
}

inline FARPROC ResolveSystemProc(HMODULE mod, const char *name)
{
  if(!mod)
    return nullptr;
  return GetProcAddress(mod, name);
}

// ---------------------------------------------------------------------------
// PEB unlinking
// ---------------------------------------------------------------------------
//
// Removes the TinecmaTool module from the three loader lists so a module-walking
// watchdog does not see a foreign DLL inside a protected process. The module
// stays mapped and callable; only the bookkeeping entries go.
//
// This is deliberately the only "stealth" step here. Nothing else about the
// process is altered - no handles, no threads hidden, no memory protection
// changes.

typedef struct _NP_UNICODE_STRING
{
  USHORT Length;
  USHORT MaximumLength;
  PWSTR Buffer;
} NP_UNICODE_STRING;

typedef struct _NP_PEB_LDR_DATA
{
  ULONG Length;
  BOOLEAN Initialized;
  PVOID SsHandle;
  LIST_ENTRY InLoadOrderModuleList;
  LIST_ENTRY InMemoryOrderModuleList;
  LIST_ENTRY InInitializationOrderModuleList;
} NP_PEB_LDR_DATA;

typedef struct _NP_LDR_DATA_TABLE_ENTRY
{
  LIST_ENTRY InLoadOrderLinks;
  LIST_ENTRY InMemoryOrderLinks;
  LIST_ENTRY InInitializationOrderLinks;
  PVOID DllBase;
  PVOID EntryPoint;
  ULONG SizeOfImage;
  NP_UNICODE_STRING FullDllName;
  NP_UNICODE_STRING BaseDllName;
} NP_LDR_DATA_TABLE_ENTRY;

inline bool UnlinkFromPEB(HMODULE mod)
{
  if(!mod)
    return false;

  unsigned char *peb = (unsigned char *)__readgsqword(0x60);
  if(!peb)
    return false;

  NP_PEB_LDR_DATA *ldr = *(NP_PEB_LDR_DATA **)(peb + 0x18);
  if(!ldr)
    return false;

  LIST_ENTRY *head = &ldr->InLoadOrderModuleList;
  LIST_ENTRY *cur = head->Flink;

  while(cur != head)
  {
    NP_LDR_DATA_TABLE_ENTRY *entry = (NP_LDR_DATA_TABLE_ENTRY *)cur;
    if(entry->DllBase == (PVOID)mod)
    {
      entry->InLoadOrderLinks.Blink->Flink = entry->InLoadOrderLinks.Flink;
      entry->InLoadOrderLinks.Flink->Blink = entry->InLoadOrderLinks.Blink;
      entry->InMemoryOrderLinks.Blink->Flink = entry->InMemoryOrderLinks.Flink;
      entry->InMemoryOrderLinks.Flink->Blink = entry->InMemoryOrderLinks.Blink;
      entry->InInitializationOrderLinks.Blink->Flink = entry->InInitializationOrderLinks.Flink;
      entry->InInitializationOrderLinks.Flink->Blink = entry->InInitializationOrderLinks.Blink;
      return true;
    }
    cur = cur->Flink;
  }

  return false;
}

// ---------------------------------------------------------------------------
// TinecmaTool core
// ---------------------------------------------------------------------------

#include "renderdoc_app.h"

typedef int(RENDERDOC_CC *PFN_TCM_GetAPI)(RENDERDOC_Version version, void **outAPIPointers);

// Candidate locations, most specific first. The build injects the repo path so a
// dev tree works with no ini at all; the ini rdoc_dll always wins.
#ifndef NARAKA_RDOC_DEFAULT
#define NARAKA_RDOC_DEFAULT ""
#endif

inline void LoadCore(const char *dllTag)
{
  State &s = S();

  if(s.cfg.mode == CaptureMode::Off || s.cfg.mode == CaptureMode::Pix)
  {
    Logf(dllTag, "capture mode is not renderdoc - core not loaded");
    return;
  }

  if(!s.isTarget)
  {
    Logf(dllTag, "not the target renderer - core not loaded (inert pass-through)");
    return;
  }

  std::string candidates[4];
  int count = 0;

  if(!s.cfg.rdocDll.empty())
    candidates[count++] = s.cfg.rdocDll;
  candidates[count++] = JoinPath(s.exeDir, "TinecmaTool.dll");
  if(NARAKA_RDOC_DEFAULT[0] != '\0')
    candidates[count++] = NARAKA_RDOC_DEFAULT;

  for(int i = 0; i < count; i++)
  {
    if(!FileExists(candidates[i]))
    {
      Logf(dllTag, "core not at %s", candidates[i].c_str());
      continue;
    }

    // Peek at the export table before committing to load the core.
    //
    // DONT_RESOLVE_DLL_REFERENCES maps the image and parses its exports but does
    // not run DllMain, so this costs a file mapping instead of a full 76 MB DLL
    // initialisation inside a protected process. A core without the bridge can
    // only forward-and-do-nothing, and having it resident is pure downside: it
    // installs its own hooks, which in this process means hooking the same entry
    // points the proxy is already fronting.
    //
    // If the peek fails for any reason we fall through and load normally - an
    // unreadable export table is not evidence that the bridge is missing.
    {
      HMODULE peek = LoadLibraryExA(candidates[i].c_str(), NULL, DONT_RESOLVE_DLL_REFERENCES);
      if(peek)
      {
        bool hasBridge = GetProcAddress(peek, "TINECMATOOL_CreateDXGIFactory") != NULL ||
                         GetProcAddress(peek, "TINECMATOOL_D3D11CreateDevice") != NULL ||
                         GetProcAddress(peek, "TINECMATOOL_D3D12CreateDevice") != NULL;
        FreeLibrary(peek);

        if(!hasBridge)
        {
          Logf(dllTag,
               "REFUSING to load %s: it predates the proxy bridge exports. Nothing would be "
               "captured, and a resident core would only add risk. Rebuild the core.",
               candidates[i].c_str());
          return;
        }
      }
    }

    // Tell the core not to install its own graphics IAT hooks *before* it loads.
    //
    // Without this the proxy and the core hook the same entry points from two
    // directions: the host resolves D3D11CreateDevice via GetProcAddress, the
    // core's Hooked_GetProcAddress answers with its own hook, and that hook then
    // tries to find the "real" function - loading a second copy of d3d11.dll
    // from System32, which triggers another hook sweep, which resolves the
    // function again. Measured 2026-11-02: the fixture spun logging
    // "Graphics function resolved: d3d11.dll ..." and never created a device.
    //
    // The exe-name check inside the core already covers narakabladepoint.exe.
    // This covers everything else - a mis-set target_process, a renamed build, a
    // test harness - so the failure mode is "not captured", never "hung".
    SetEnvironmentVariableA("TCM_SKIP_GRAPHICS_IAT", "1");

    HMODULE mod = LoadLibraryA(candidates[i].c_str());
    if(!mod)
    {
      Logf(dllTag, "LoadLibrary(%s) failed: %lu", candidates[i].c_str(), GetLastError());
      continue;
    }

    s.rdocModule = mod;
    s.rdocLoaded = true;

    if(UnlinkFromPEB(mod))
      Logf(dllTag, "core loaded and unlinked from PEB: %s", candidates[i].c_str());
    else
      Logf(dllTag, "core loaded: %s (PEB unlink returned false)", candidates[i].c_str());

    PFN_TCM_GetAPI getApi = (PFN_TCM_GetAPI)GetProcAddress(mod, "TINECMATOOL_GetAPI");
    if(!getApi)
      getApi = (PFN_TCM_GetAPI)GetProcAddress(mod, "RENDERDOC_GetAPI");

    if(!getApi)
    {
      Logf(dllTag, "core has no TINECMATOOL_GetAPI - unusable");
      s.rdocLoaded = false;
      return;
    }

    RENDERDOC_API_1_4_1 *api = nullptr;
    int res = getApi(eRENDERDOC_API_Version_1_4_1, (void **)&api);
    s.rdocApi = api;
    Logf(dllTag, "TINECMATOOL_GetAPI(1.4.1) -> %d, api=%p", res, api);

    if(api && api->SetCaptureFilePathTemplate)
    {
      std::string clean = s.exeName;
      size_t dot = clean.find('.');
      if(dot != std::string::npos)
        clean = clean.substr(0, dot);

      std::string tmpl = JoinPath(s.cfg.captureDir, ("naraka_" + clean).c_str());
      api->SetCaptureFilePathTemplate(tmpl.c_str());
      Logf(dllTag, "capture template: %s", tmpl.c_str());
    }

    // Route RenderDoc's own log next to ours - the default location is the
    // user temp directory, which makes correlating the two logs guesswork.
    typedef void(__cdecl * PFN_SetDebugLogFile)(const char *);
    PFN_SetDebugLogFile setLog =
        (PFN_SetDebugLogFile)GetProcAddress(mod, "INTERNAL_SetDebugLogFile");
    if(setLog)
    {
      std::string coreLog = JoinPath(s.cfg.logDir.empty() ? DefaultWorkDir(s.exeDir) : s.cfg.logDir,
                                     "renderdoc_core.log");
      setLog(coreLog.c_str());
      Logf(dllTag, "core debug log: %s", coreLog.c_str());
    }

    s.pfnCreateDXGIFactory = (void *)GetProcAddress(mod, "TINECMATOOL_CreateDXGIFactory");
    s.pfnCreateDXGIFactory1 = (void *)GetProcAddress(mod, "TINECMATOOL_CreateDXGIFactory1");
    s.pfnCreateDXGIFactory2 = (void *)GetProcAddress(mod, "TINECMATOOL_CreateDXGIFactory2");
    s.pfnD3D11CreateDevice = (void *)GetProcAddress(mod, "TINECMATOOL_D3D11CreateDevice");
    s.pfnD3D11CreateDeviceAndSwapChain =
        (void *)GetProcAddress(mod, "TINECMATOOL_D3D11CreateDeviceAndSwapChain");
    s.pfnD3D12CreateDevice = (void *)GetProcAddress(mod, "TINECMATOOL_D3D12CreateDevice");

    Logf(dllTag, "bridge exports: dxgi=%p/%p/%p d3d11=%p/%p d3d12=%p", s.pfnCreateDXGIFactory,
         s.pfnCreateDXGIFactory1, s.pfnCreateDXGIFactory2, s.pfnD3D11CreateDevice,
         s.pfnD3D11CreateDeviceAndSwapChain, s.pfnD3D12CreateDevice);

    if(!s.pfnCreateDXGIFactory && !s.pfnD3D11CreateDevice && !s.pfnD3D12CreateDevice)
    {
      Logf(dllTag,
           "WARNING: this TinecmaTool.dll predates the proxy bridge exports. Everything will be "
           "forwarded to the real system DLL and NOTHING will be captured. Rebuild the core.");
    }

    return;
  }

  Logf(dllTag, "WARNING: TinecmaTool.dll not found in any candidate location");
}

// ---------------------------------------------------------------------------
// Trigger monitor
// ---------------------------------------------------------------------------
//
// F12, or a trigger file. The file has to be newer than the snapshot taken at
// startup, otherwise a leftover file from an earlier session fires a capture on
// frame 0 - while the engine is still compiling PSOs - which stalls the
// watchdog and gets the process killed.

inline DWORD WINAPI TriggerMonitorThread(LPVOID)
{
  State &s = S();

  std::string clean = s.exeName;
  size_t dot = clean.find('.');
  if(dot != std::string::npos)
    clean = clean.substr(0, dot);

  std::string generic = JoinPath(s.cfg.captureDir, "trigger_capture");
  std::string specific = JoinPath(s.cfg.captureDir, ("trigger_capture_" + clean).c_str());

  Logf("trigger", "watching F12 and %s", specific.c_str());

  FILETIME lastGeneric = {0, 0};
  WIN32_FILE_ATTRIBUTE_DATA init = {};
  if(GetFileAttributesExA(generic.c_str(), GetFileExInfoStandard, &init))
    lastGeneric = init.ftLastWriteTime;

  while(s.threadRunning)
  {
    bool fire = (GetAsyncKeyState(VK_F12) & 1) != 0;

    if(GetFileAttributesA(specific.c_str()) != INVALID_FILE_ATTRIBUTES)
    {
      fire = true;
      DeleteFileA(specific.c_str());
      Logf("trigger", "file trigger: %s", specific.c_str());
    }

    WIN32_FILE_ATTRIBUTE_DATA now = {};
    if(GetFileAttributesExA(generic.c_str(), GetFileExInfoStandard, &now))
    {
      if(CompareFileTime(&now.ftLastWriteTime, &lastGeneric) > 0)
      {
        lastGeneric = now.ftLastWriteTime;
        DeleteFileA(generic.c_str());
        fire = true;
        Logf("trigger", "generic file trigger");
      }
    }

    if(fire && !s.capturing)
    {
      s.capturing = true;

      RENDERDOC_API_1_4_1 *api = (RENDERDOC_API_1_4_1 *)s.rdocApi;
      if(api && api->TriggerCapture)
      {
        uint32_t before = api->GetNumCaptures ? api->GetNumCaptures() : 0;
        api->TriggerCapture();
        Logf("capture", "TriggerCapture() issued (previous captures: %u)", before);

        if(api->GetNumCaptures && api->GetCapture)
        {
          for(int w = 0; w < 400; w++)
          {
            Sleep(500);
            if(api->GetNumCaptures() > before)
            {
              char file[MAX_PATH] = {0};
              uint32_t len = MAX_PATH;
              uint64_t ts = 0;
              api->GetCapture(before, file, &len, &ts);
              Logf("capture", "DONE: %s", file);

              std::string done = JoinPath(s.cfg.captureDir, "capture_done.txt");
              FILE *fp = fopen(done.c_str(), "w");
              if(fp)
              {
                fprintf(fp, "%s\n", file);
                fclose(fp);
              }
              break;
            }
          }
        }
      }
      else
      {
        Logf("capture", "no in-app API available - cannot trigger");
      }

      Sleep(2000);
      s.capturing = false;
    }

    Sleep(100);
  }

  return 0;
}

inline void StartMonitor(const char *dllTag)
{
  State &s = S();
  if(s.monitorStarted || !s.isTarget || s.cfg.mode == CaptureMode::Off)
    return;

  s.monitorStarted = true;
  s.threadRunning = true;
  CreateThread(nullptr, 0, TriggerMonitorThread, nullptr, 0, nullptr);
  Logf(dllTag, "trigger monitor started");
}

inline void Shutdown(const char *dllTag)
{
  State &s = S();
  s.threadRunning = false;
  if(s.log)
    Logf(dllTag, "proxy detaching");
  // The FILE* is deliberately left open until process exit: FreeLibrary ordering
  // during DLL_PROCESS_DETACH can otherwise pull the CRT out from under it.
}

// ---------------------------------------------------------------------------
// Generic forwarders
// ---------------------------------------------------------------------------
//
// Used for the exports we do not intercept. Every function in the dxgi, d3d11
// and d3d12 export tables takes integral args and returns an integral or
// pointer-sized value - no struct returns, no floating point - so a six-argument
// pointer-shaped thunk is ABI-correct on x64 (surplus registers are ignored by
// the callee). Functions that break that assumption are implemented explicitly.

typedef void *(WINAPI *PFN_GenericThunk)(void *, void *, void *, void *, void *, void *);

inline void *ForwardByName(HMODULE real, const char *name, void *a1, void *a2, void *a3, void *a4,
                           void *a5, void *a6)
{
  static int s_forwardCount = 0;
  FARPROC fn = ResolveSystemProc(real, name);
  if(!fn)
  {
    if(s_forwardCount++ < 16)
      Logf("forward", "MISSING export %s in system DLL", name);
    return nullptr;
  }
  return ((PFN_GenericThunk)fn)(a1, a2, a3, a4, a5, a6);
}

// For the handful of exports that are only addressable by ordinal (d3d12 ordinal 99 is unnamed).
inline void *ForwardByOrdinal(HMODULE real, WORD ordinal, void *a1, void *a2, void *a3, void *a4,
                              void *a5, void *a6)
{
  if(!real)
    return nullptr;
  FARPROC fn = GetProcAddress(real, MAKEINTRESOURCEA(ordinal));
  if(!fn)
  {
    Logf("forward", "MISSING export ordinal %u in system DLL", (unsigned)ordinal);
    return nullptr;
  }
  return ((PFN_GenericThunk)fn)(a1, a2, a3, a4, a5, a6);
}

}  // namespace np

#endif  // NARAKA_PROXY_COMMON_H
