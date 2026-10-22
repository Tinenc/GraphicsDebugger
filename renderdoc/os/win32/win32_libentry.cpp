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
#include "common/common.h"
#include "core/core.h"
#include "hooks/hooks.h"
#include "strings/string_utils.h"

static BOOL add_hooks()
{
  wchar_t curFile[512];
  GetModuleFileNameW(NULL, curFile, 512);

  rdcstr f = get_basename(strlower(StringFormat::Wide2UTF8(curFile)));

  // bail immediately if we're in a system process. We don't want to hook, log, anything -
  // this instance is being used for a shell extension.
  if(f == "dllhost.exe" || f == "explorer.exe")
  {
#if ENABLED(RDOC_RELEASE)
    OutputDebugStringA(
        "Detecting shell process! Disabling hooking in dllhost.exe or explorer.exe\n");
#endif
    return TRUE;
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

  // NOTE: no capture-target filtering here.
  //
  // With the injection setup the tool deliberately injects into Games.exe - the
  // launcher UI - because that is the process which spawns Endfield.exe, and the
  // CreateProcess hook only exists where our DLL is. Games.exe is obviously not
  // "endfield.exe", so filtering on the target name here would make the injected
  // DLL inert and leave nothing to carry the injection down to the game.
  //
  // The "do not disturb unrelated Vulkan processes" job belongs to the loader
  // negotiation in vk_layer.cpp (VK_ERROR_INITIALIZATION_FAILED), which is where
  // a layer can actually opt out. The layer json stays locked for this setup, so
  // unrelated processes never load this DLL in the first place.

  RenderDoc::Inst().Initialise();

  RDCLOG("Loading into %ls", curFile);

  // Relay vs. render target.
  //
  // A relay process exists only to carry the injection onward through CreateProcess.
  // Games.exe is one: it is a Qt5 + QtWebEngine UI, and its window is drawn through
  // ANGLE (GLES over D3D11). Registering the graphics hooks there breaks that:
  //
  //   egl_platform.cpp(92) - Creating cloned context failed (3006)
  //   gl_common.cpp(517)   - Detected vendor: None
  //
  // and the launcher window goes completely black. It also parks a dozen "OpenGLES
  // device frame capturer" objects on the UI's contexts, which is pure noise.
  //
  // So: only the real render target gets the full hook set. Anything else gets the
  // process hook alone - enough to intercept CreateProcess, nothing that touches GL.
  rdcstr target = "endfield.exe";
  {
    wchar_t envbuf[256] = {};
    DWORD envlen = GetEnvironmentVariableW(L"TCM_LAYER_TARGET", envbuf, ARRAY_COUNT(envbuf));
    if(envlen > 0 && envlen < ARRAY_COUNT(envbuf))
      target = strlower(StringFormat::Wide2UTF8(envbuf));
  }

  if(f == target)
  {
    LibraryHooks::RegisterHooks();
  }
  else
  {
    RDCLOG("Relay process (not %s), registering process hooks only", target.c_str());
    LibraryHooks::RegisterProcessHooks();
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
