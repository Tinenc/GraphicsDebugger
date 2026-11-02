// ============================================================================
// dxgi.dll proxy - Naraka capture entry point
// ============================================================================
//
// UnityPlayer.dll imports CreateDXGIFactory statically, so this DLL is the one
// the Windows loader picks up from the game directory. That makes it both the
// factory bridge and the natural owner of the trigger monitor thread.
//
// It also matters for D3D12: the app-local Agility SDK (D3D12\D3D12Core.dll)
// imports CreateDXGIFactory2, so that route lands here too.
// ============================================================================

#include "naraka_proxy_common.h"

#include <dxgi.h>
#include <dxgi1_6.h>

namespace {

// Real system dxgi.
HMODULE g_realDxgi = nullptr;

// Re-entrancy guard for the factory trio. If the core ever calls back into us
// (rather than straight to System32) we must forward instead of looping.
thread_local bool t_inFactory = false;

typedef HRESULT(WINAPI *PFN_CreateDXGIFactory)(REFIID, void **);
typedef HRESULT(WINAPI *PFN_CreateDXGIFactory2)(UINT, REFIID, void **);

bool UseBridge()
{
  np::State &s = np::S();
  return s.isTarget && s.rdocLoaded &&
         (s.cfg.mode == np::CaptureMode::RenderDoc || s.cfg.mode == np::CaptureMode::Both) &&
         s.pfnCreateDXGIFactory != nullptr;
}

template <typename Fn>
HRESULT CallFactory(const char *which, void *bridge, Fn realCall, bool twoArg, UINT flags,
                    REFIID riid, void **ppFactory)
{
  if(!t_inFactory && bridge)
  {
    t_inFactory = true;
    HRESULT hr = twoArg ? ((HRESULT(WINAPI *)(REFIID, void **))bridge)(riid, ppFactory)
                        : ((HRESULT(WINAPI *)(UINT, REFIID, void **))bridge)(flags, riid, ppFactory);
    t_inFactory = false;
    np::Logf("dxgi", "BRIDGE %s -> hr=0x%08X factory=%p", which, (uint32_t)hr,
             ppFactory ? *ppFactory : nullptr);
    return hr;
  }

  if(t_inFactory)
    np::Logf("dxgi", "re-entry on %s, forwarding to system dxgi", which);

  if(!realCall)
    return E_FAIL;

  HRESULT hr = twoArg ? ((HRESULT(WINAPI *)(REFIID, void **))realCall)(riid, ppFactory)
                      : ((HRESULT(WINAPI *)(UINT, REFIID, void **))realCall)(flags, riid, ppFactory);
  np::Logf("dxgi", "SYSTEM %s -> hr=0x%08X factory=%p", which, (uint32_t)hr,
           ppFactory ? *ppFactory : nullptr);
  return hr;
}

}  // namespace

extern "C" {

// --- intercepted -----------------------------------------------------------

HRESULT WINAPI NarakaImpl_CreateDXGIFactory(REFIID riid, void **ppFactory)
{
  if(!g_realDxgi)
  {
    // Should not happen - DllMain loads it - but never dereference null.
    return E_FAIL;
  }
  np::State &s = np::S();
  return CallFactory("CreateDXGIFactory", s.pfnCreateDXGIFactory,
                     (void *)GetProcAddress(g_realDxgi, "CreateDXGIFactory"), true, 0, riid,
                     ppFactory);
}

HRESULT WINAPI NarakaImpl_CreateDXGIFactory1(REFIID riid, void **ppFactory)
{
  if(!g_realDxgi)
    return E_FAIL;
  np::State &s = np::S();
  return CallFactory("CreateDXGIFactory1", s.pfnCreateDXGIFactory1,
                     (void *)GetProcAddress(g_realDxgi, "CreateDXGIFactory1"), true, 0, riid,
                     ppFactory);
}

HRESULT WINAPI NarakaImpl_CreateDXGIFactory2(UINT Flags, REFIID riid, void **ppFactory)
{
  if(!g_realDxgi)
    return E_FAIL;
  np::State &s = np::S();
  return CallFactory("CreateDXGIFactory2", s.pfnCreateDXGIFactory2,
                     (void *)GetProcAddress(g_realDxgi, "CreateDXGIFactory2"), false, Flags, riid,
                     ppFactory);
}

// --- pass-through ----------------------------------------------------------
//
// Kept so the proxy's export table matches system dxgi exactly (20 exports on
// this machine). A missing export is a hard load failure for anything importing
// by name, so "we only needed the three creators" is not a safe assumption.

#define NARAKA_DXGI_FORWARD(name) \
  void *WINAPI NarakaImpl_##name(void *a1, void *a2, void *a3, void *a4, void *a5, \
                                 void *a6, void *a7, void *a8, void *a9, \
                                 void *a10, void *a11, void *a12) \
  { \
    return np::ForwardByName(g_realDxgi, #name, a1, a2, a3, a4, a5, a6, a7, a8, \
                             a9, a10, a11, a12); \
  }

NARAKA_DXGI_FORWARD(ApplyCompatResolutionQuirking)
NARAKA_DXGI_FORWARD(CompatString)
NARAKA_DXGI_FORWARD(CompatValue)
NARAKA_DXGI_FORWARD(DXGIDumpJournal)
NARAKA_DXGI_FORWARD(PIXBeginCapture)
NARAKA_DXGI_FORWARD(PIXEndCapture)
NARAKA_DXGI_FORWARD(PIXGetCaptureState)
NARAKA_DXGI_FORWARD(SetAppCompatStringPointer)
NARAKA_DXGI_FORWARD(UpdateHMDEmulationStatus)
NARAKA_DXGI_FORWARD(DXGID3D10CreateDevice)
NARAKA_DXGI_FORWARD(DXGID3D10CreateLayeredDevice)
NARAKA_DXGI_FORWARD(DXGID3D10GetLayeredDeviceSize)
NARAKA_DXGI_FORWARD(DXGID3D10RegisterLayers)
NARAKA_DXGI_FORWARD(DXGIDeclareAdapterRemovalSupport)
NARAKA_DXGI_FORWARD(DXGIDisableVBlankVirtualization)
NARAKA_DXGI_FORWARD(DXGIGetDebugInterface1)
NARAKA_DXGI_FORWARD(DXGIReportAdapterConfiguration)

}  // extern "C"

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID)
{
  if(reason == DLL_PROCESS_ATTACH)
  {
    DisableThreadLibraryCalls(hinst);

    np::InitState("dxgi");

    g_realDxgi = np::LoadSystemDll(L"dxgi.dll");
    if(!g_realDxgi)
    {
      np::Logf("dxgi", "FATAL: no system dxgi.dll, proxy cannot function");
      return FALSE;
    }
    np::Logf("dxgi", "system dxgi loaded at %p", g_realDxgi);

    np::LoadCore("dxgi");

    // Only one proxy starts the monitor; dxgi is the first of the three to load.
    np::StartMonitor("dxgi");
  }
  else if(reason == DLL_PROCESS_DETACH)
  {
    np::Shutdown("dxgi");
  }

  return TRUE;
}
