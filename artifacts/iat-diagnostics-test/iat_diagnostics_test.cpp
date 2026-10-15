#include <windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstdarg>
#include <map>
#include <mutex>
#include <string>
#define ENABLED(x) 0
#define SCOPED_LOCK(x) std::lock_guard<std::mutex> guard(x)
namespace Threading { using CriticalSection = std::mutex; }
struct FunctionHook { std::string function; void *hook; };
static std::string lastLog;
static void TestLog(const char *fmt, ...) {
  char buffer[4096]; va_list args; va_start(args, fmt);
  vsnprintf(buffer, sizeof(buffer), fmt, args); va_end(args);
  lastLog = buffer; puts(buffer); SetLastError(1234);
}
#define RDCERR(...) TestLog(__VA_ARGS__)
#define CHECK(x) do { if(!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); exit(1); } } while(0)

#define RDCLOG(...) TestLog(__VA_ARGS__)
// map from address of IAT entry, to original contents
std::map<void **, void *> s_InstalledHooks;
Threading::CriticalSection installedLock;

static bool IsGraphicsCreationFunction(const char *function)
{
  return !strcmp(function, "D3D12CreateDevice") || !strcmp(function, "D3D12GetInterface") ||
         !strcmp(function, "D3D11CreateDevice") ||
         !strcmp(function, "D3D11CreateDeviceAndSwapChain") ||
         !strcmp(function, "CreateDXGIFactory") || !strcmp(function, "CreateDXGIFactory1") ||
         !strcmp(function, "CreateDXGIFactory2");
}

static void LogIATProtectionFailure(const char *operation, void **IATentry, DWORD error,
                                    const char *moduleName, HMODULE module, const char *dllName,
                                    const char *function)
{
  // Preserve the VirtualProtect error before querying the page or writing the log. Do not
  // dereference the entry here: a module may have been unloaded when removing hooks.
  MEMORY_BASIC_INFORMATION page = {};
  SIZE_T queried = VirtualQuery(IATentry, &page, sizeof(page));
  DWORD queryError = queried ? ERROR_SUCCESS : GetLastError();

  RDCERR(
      "IAT protection failure: operation=%s error=%lu module=%s moduleBase=%p "
      "import=%s!%s entry=%p VirtualQueryBytes=%llu VirtualQueryError=%lu "
      "allocationBase=%p regionBase=%p regionSize=%llu state=0x%lx "
      "protect=0x%lx allocationProtect=0x%lx type=0x%lx",
      operation, error, moduleName, module, dllName, function, IATentry, (unsigned long long)queried,
      queryError, page.AllocationBase, page.BaseAddress, (unsigned long long)page.RegionSize,
      page.State, page.Protect, page.AllocationProtect, page.Type);

  SetLastError(error);
}

bool ApplyHook(FunctionHook &hook, void **IATentry, bool &already, const char *moduleName,
               HMODULE module, const char *dllName)
{
  DWORD oldProtection = PAGE_EXECUTE;

  if(*IATentry == hook.hook)
  {
    already = true;
    return true;
  }

#if ENABLED(VERBOSE_DEBUG_HOOK)
  RDCDEBUG("Patching IAT for %s: %p to %p", hook.function.c_str(), IATentry, hook.hook);
#endif

  {
    SCOPED_LOCK(installedLock);
    if(s_InstalledHooks.find(IATentry) == s_InstalledHooks.end())
      s_InstalledHooks[IATentry] = *IATentry;
  }

  BOOL success = VirtualProtect(IATentry, sizeof(void *), PAGE_READWRITE, &oldProtection);
  if(!success)
  {
    const DWORD error = GetLastError();
    LogIATProtectionFailure("install/make-writable", IATentry, error, moduleName, module, dllName,
                            hook.function.c_str());
    return false;
  }

  *IATentry = hook.hook;

  success = VirtualProtect(IATentry, sizeof(void *), oldProtection, &oldProtection);
  if(!success)
  {
    const DWORD error = GetLastError();
    LogIATProtectionFailure("install/restore-protection", IATentry, error, moduleName, module,
                            dllName, hook.function.c_str());
    return false;
  }

  if(IsGraphicsCreationFunction(hook.function.c_str()))
    RDCLOG("Graphics IAT hook installed: module=%s moduleBase=%p import=%s!%s entry=%p hook=%p",
           moduleName, module, dllName, hook.function.c_str(), IATentry, hook.hook);

  return true;
}

int main() {
  FunctionHook hook = {"DiagnosticFunction", (void *)(uintptr_t)0x12345678};
  HMODULE owner = GetModuleHandleW(NULL);
  void **entry = (void **)VirtualAlloc(NULL, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
  CHECK(entry); *entry = (void *)(uintptr_t)0x87654321;
  DWORD oldProtect = 0;
  CHECK(VirtualProtect(entry, 4096, PAGE_READONLY, &oldProtect));
  bool already = false;
  CHECK(ApplyHook(hook, entry, already, "test.exe", owner, "test.dll"));
  CHECK(!already && *entry == hook.hook);
  MEMORY_BASIC_INFORMATION page = {};
  CHECK(VirtualQuery(entry, &page, sizeof(page)) == sizeof(page));
  CHECK(page.Protect == PAGE_READONLY);
  CHECK(s_InstalledHooks[entry] == (void *)(uintptr_t)0x87654321);
  CHECK(ApplyHook(hook, entry, already, "test.exe", owner, "test.dll") && already);
  CHECK(VirtualFree(entry, 0, MEM_RELEASE));
  s_InstalledHooks.clear();
  HANDLE mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READONLY, 0, 4096, NULL);
  CHECK(mapping);
  entry = (void **)MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 4096);
  CHECK(entry); already = false;
  CHECK(!VirtualProtect(entry, sizeof(void *), PAGE_READWRITE, &oldProtect));
  const DWORD expectedError = GetLastError();
  CHECK(!ApplyHook(hook, entry, already, "readonly-test.exe", owner, "test.dll"));
  CHECK(*entry == NULL && !already);
  CHECK(GetLastError() == expectedError);
  CHECK(lastLog.find("operation=install/make-writable error=" + std::to_string(expectedError)) != std::string::npos);
  CHECK(lastLog.find("module=readonly-test.exe") != std::string::npos);
  CHECK(lastLog.find("import=test.dll!DiagnosticFunction") != std::string::npos);
  CHECK(lastLog.find("state=0x1000 protect=0x2") != std::string::npos);
  CHECK(UnmapViewOfFile(entry)); CHECK(CloseHandle(mapping));
  void *reserved = VirtualAlloc(NULL, 4096, MEM_RESERVE, PAGE_NOACCESS);
  CHECK(reserved);
  LogIATProtectionFailure("remove/make-writable", (void **)reserved, ERROR_INVALID_ADDRESS,
                          "unknown", NULL, "unknown", "unknown");
  CHECK(GetLastError() == ERROR_INVALID_ADDRESS);
  CHECK(lastLog.find("state=0x2000") != std::string::npos);
  CHECK(VirtualFree(reserved, 0, MEM_RELEASE));
  LogIATProtectionFailure("query-test", (void **)(uintptr_t)-1, ERROR_ACCESS_DENIED,
                          "unknown", NULL, "unknown", "unknown");
  CHECK(GetLastError() == ERROR_ACCESS_DENIED);
  CHECK(lastLog.find("VirtualQueryBytes=0") != std::string::npos);
  puts("PASS: install, already-installed, restored protection, denied write, reserved page, query failure, preserved errors");
}
