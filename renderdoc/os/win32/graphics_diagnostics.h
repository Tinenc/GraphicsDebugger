#pragma once

#include <windows.h>
#if defined(_MSC_VER)
#include <intrin.h>
#define GRAPHICS_DIAGNOSTICS_CALLER() _ReturnAddress()
#else
#define GRAPHICS_DIAGNOSTICS_CALLER() __builtin_return_address(0)
#endif
#include "common/common.h"
#include "strings/string_utils.h"

// Read-only attribution of calls reaching existing graphics wrappers. Resolve
// the address without loading modules or changing their reference counts.
inline void LogGraphicsCreationCaller(const char *api, void *caller)
{
  DWORD savedError = GetLastError();
  MEMORY_BASIC_INFORMATION page = {};
  SIZE_T queried = VirtualQuery(caller, &page, sizeof(page));
  DWORD queryError = queried ? ERROR_SUCCESS : GetLastError();
  wchar_t modulePath[1024] = {};
  if(queried && page.Type == MEM_IMAGE)
    GetModuleFileNameW((HMODULE)page.AllocationBase, modulePath, 1023);

  rdcstr path = modulePath[0] ? StringFormat::Wide2UTF8(modulePath) : "unknown";
  RDCLOG(
      "Graphics call entered: api=%s caller=%p callerModule=%s allocationBase=%p "
      "offset=0x%llx memoryType=0x%lx queryError=%lu",
      api, caller, path.c_str(), page.AllocationBase,
      queried ? (unsigned long long)((uintptr_t)caller - (uintptr_t)page.AllocationBase) : 0ULL,
      page.Type, queryError);
  SetLastError(savedError);
}
