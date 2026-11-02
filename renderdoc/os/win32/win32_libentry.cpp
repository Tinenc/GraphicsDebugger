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
