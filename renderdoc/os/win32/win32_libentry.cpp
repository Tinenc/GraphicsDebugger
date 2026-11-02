/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2015-2026 Baldur Karlsson
 * Copyright (c) 2014 Crytek
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

// win32_libentry.cpp : Defines the entry point for the DLL
#include <tchar.h>
#include <windows.h>
#include <stdlib.h>
#include <intrin.h>
#include "common/common.h"
#include "core/core.h"
#include "hooks/hooks.h"
#include "strings/string_utils.h"

static BOOL add_hooks()
{
  wchar_t curFile[512];
  GetModuleFileNameW(NULL, curFile, 512);

  rdcstr f = get_basename(strlower(StringFormat::Wide2UTF8(curFile)));

  // Bail immediately if we're in a system process. We don't want to hook, log, anything -
  // this instance is being used for a shell extension.
  if(f == "dllhost.exe" || f == "explorer.exe")
  {
#if ENABLED(RDOC_RELEASE)
    OutputDebugStringA(
        "Detecting shell process! Disabling hooking in dllhost.exe or explorer.exe\n");
#endif
    return TRUE;
  }

  // Skip graphics IAT hooks only when something tells us to.
  //
  // Originally this also keyed off the process name being narakabladepoint.exe, on the assumption
  // that the proxy layer would always be the one loading us there. That assumption is what makes
  // injection look broken: an injected renderer got g_SkipGraphicsDriverHooks set purely because
  // of its name, registered nothing, and could never wrap a device. The proxy sets
  // TCM_SKIP_GRAPHICS_IAT itself right before LoadLibrary, so the name test was redundant for the
  // case it was written for and actively harmful for the other one.
  //
  // Read with GetEnvironmentVariableA, not getenv: the CRT snapshots the environment at startup,
  // so a value placed by SetEnvironmentVariableA moments earlier is invisible to getenv.
  {
    char skipIat[8] = {0};
    DWORD skipLen = GetEnvironmentVariableA("TCM_SKIP_GRAPHICS_IAT", skipIat, sizeof(skipIat));

    if(skipLen > 0 && skipLen < sizeof(skipIat) && skipIat[0] == '1')
    {
      g_SkipGraphicsDriverHooks = true;
      RDCLOG("Graphics IAT hooks disabled for %s - a proxy layer will supply the entry points",
             f.c_str());
    }
  }

  // search for an exported symbol with this name, typically TinecmaTool__replay__marker
  if(LibraryHooks::Detect("TinecmaTool__replay__marker"))
  {
    RDCDEBUG("Not creating hooks - in replay app");

    RenderDoc::Inst().SetReplayApp(true);

    RenderDoc::Inst().Initialise();

    LibraryHooks::ReplayInitialise();

    return true;
  }

  RenderDoc::Inst().Initialise();

  RDCLOG("Loading into %ls", curFile);

  LibraryHooks::RegisterHooks();

  // Queue the export redirects and hook the loader's DLL notification.
  //
  // At this point the graphics DLLs are usually not loaded yet - measured on Naraka, injected at
  // +1.1s, all six redirects reported "module not loaded". They have to be installed the moment the
  // module is mapped, because the caller resolves the export immediately afterwards and keeps the
  // pointer it got.
  LibraryHooks::InstallDeferredExportRedirects();

  // Hide from loader enumeration. A module-walking watchdog looks for exactly this - a DLL in the
  // process that is not in the PEB list is suspicious on its own, but a DLL that IS in the list and
  // was never part of the original image is equally easy to spot, and this is the step that costs
  // least to take. Nothing else about the process is touched: no handles, no threads, no memory
  // protection tricks.
  //
  // TCM_NO_PEB_UNLINK=1 keeps the module visible, for when a debugger or a stack walk is needed.
  {
    char opt[8] = {0};
    DWORD len = GetEnvironmentVariableA("TCM_NO_PEB_UNLINK", opt, sizeof(opt));
    if(!(len > 0 && len < sizeof(opt) && opt[0] == '1'))
    {
      HMODULE self = NULL;
      GetModuleHandleEx(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                        (LPCTSTR)&add_hooks, &self);

      if(self)
      {
#if defined(_WIN64)
        // Same layout the proxy layer uses, spelled out because this file cannot include it.
        typedef struct
        {
          ULONG Length;
          BOOLEAN Initialized;
          PVOID SsHandle;
          LIST_ENTRY InLoadOrderModuleList;
          LIST_ENTRY InMemoryOrderModuleList;
          LIST_ENTRY InInitializationOrderModuleList;
        } PebLdrData;

        typedef struct
        {
          LIST_ENTRY InLoadOrderLinks;
          LIST_ENTRY InMemoryOrderLinks;
          LIST_ENTRY InInitializationOrderLinks;
          PVOID DllBase;
        } LdrEntryHead;

        unsigned char *peb = (unsigned char *)__readgsqword(0x60);
        PebLdrData *ldr = peb ? *(PebLdrData **)(peb + 0x18) : NULL;
        bool unlinked = false;

        if(ldr)
        {
          LIST_ENTRY *head = &ldr->InLoadOrderModuleList;
          LdrEntryHead *entry = (LdrEntryHead *)head->Flink;

          while((LIST_ENTRY *)entry != head)
          {
            if(entry->DllBase == (PVOID)self)
            {
              entry->InLoadOrderLinks.Blink->Flink = entry->InLoadOrderLinks.Flink;
              entry->InLoadOrderLinks.Flink->Blink = entry->InLoadOrderLinks.Blink;
              entry->InMemoryOrderLinks.Blink->Flink = entry->InMemoryOrderLinks.Flink;
              entry->InMemoryOrderLinks.Flink->Blink = entry->InMemoryOrderLinks.Blink;
              entry->InInitializationOrderLinks.Blink->Flink = entry->InInitializationOrderLinks.Flink;
              entry->InInitializationOrderLinks.Flink->Blink = entry->InInitializationOrderLinks.Blink;
              unlinked = true;
              break;
            }
            entry = (LdrEntryHead *)entry->InLoadOrderLinks.Flink;
          }
        }

        RDCLOG("PEB unlink of our own module: %s", unlinked ? "done" : "not found in the list");
#else
        // x86 reaches the PEB through fs:[0x30] with a different field layout. The x86 build here
        // is the launcher shim, which has no business hiding itself, so it simply does not.
        RDCLOG("PEB unlink skipped on x86");
#endif
      }
    }
  }

  return TRUE;
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD ul_reason_for_call, LPVOID lpReserved)
{
  if(ul_reason_for_call == DLL_PROCESS_ATTACH)
  {
    BOOL ret = add_hooks();
    SetLastError(0);
    return ret;
  }

  return TRUE;
}
