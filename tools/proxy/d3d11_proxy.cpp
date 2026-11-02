// ============================================================================
// d3d11.dll proxy - Naraka D3D11 device bridge
// ============================================================================
//
// This is the DLL that answers the one failure the Naraka branch could not get
// past. docs/NARAKA_挂载方案_StartGame_2026-10-24.md §7: UnityPlayer resolves
// D3D11CreateDevice dynamically, the hook registers, and then nothing happens -
// "Graphics function resolved" stays at 0 for the whole session, because
// VMProtect rewrites the import slots during unpacking and our patch is gone
// before the first call.
//
// Being the import removes the entire problem. Unity asks for "d3d11.dll", the
// loader finds this one first, and D3D11CreateDevice here hands the call to
// TINECMATOOL_D3D11CreateDevice so the device comes back wrapped.
//
// If the loaded TinecmaTool.dll has no bridge export (older build), the call is
// forwarded to the real device creation instead. The game keeps working, the log
// says why, and nothing is captured - a loud, benign failure rather than a crash.
// ============================================================================

#include "naraka_proxy_common.h"

#include <d3d11.h>
#include <dxgi.h>

namespace {

HMODULE g_realD3D11 = nullptr;

thread_local bool t_inCreate = false;

typedef HRESULT(WINAPI *PFN_D3D11CreateDevice)(
    IDXGIAdapter *pAdapter, D3D_DRIVER_TYPE DriverType, HMODULE Software, UINT Flags,
    const D3D_FEATURE_LEVEL *pFeatureLevels, UINT FeatureLevels, UINT SDKVersion,
    ID3D11Device **ppDevice, D3D_FEATURE_LEVEL *pFeatureLevel, ID3D11DeviceContext **ppContext);

typedef HRESULT(WINAPI *PFN_D3D11CreateDeviceAndSwapChain)(
    IDXGIAdapter *pAdapter, D3D_DRIVER_TYPE DriverType, HMODULE Software, UINT Flags,
    const D3D_FEATURE_LEVEL *pFeatureLevels, UINT FeatureLevels, UINT SDKVersion,
    const DXGI_SWAP_CHAIN_DESC *pSwapChainDesc, IDXGISwapChain **ppSwapChain,
    ID3D11Device **ppDevice, D3D_FEATURE_LEVEL *pFeatureLevel, ID3D11DeviceContext **ppContext);

bool CaptureActive()
{
  np::State &s = np::S();
  return s.isTarget && s.rdocLoaded &&
         (s.cfg.mode == np::CaptureMode::RenderDoc || s.cfg.mode == np::CaptureMode::Both);
}

}  // namespace

extern "C" {

HRESULT WINAPI NarakaImpl_D3D11CreateDevice(
    __in_opt IDXGIAdapter *pAdapter, D3D_DRIVER_TYPE DriverType, HMODULE Software, UINT Flags,
    __in_ecount_opt(FeatureLevels) CONST D3D_FEATURE_LEVEL *pFeatureLevels, UINT FeatureLevels,
    UINT SDKVersion, __out_opt ID3D11Device **ppDevice,
    __out_opt D3D_FEATURE_LEVEL *pFeatureLevel, __out_opt ID3D11DeviceContext **ppImmediateContext)
{
  np::State &s = np::S();

  if(!t_inCreate && CaptureActive() && s.pfnD3D11CreateDevice)
  {
    t_inCreate = true;
    HRESULT hr = ((PFN_D3D11CreateDevice)s.pfnD3D11CreateDevice)(
        pAdapter, DriverType, Software, Flags, pFeatureLevels, FeatureLevels, SDKVersion, ppDevice,
        pFeatureLevel, ppImmediateContext);
    t_inCreate = false;
    np::Logf("d3d11", "BRIDGE D3D11CreateDevice -> hr=0x%08X device=%p level=0x%X", (uint32_t)hr,
             ppDevice ? *ppDevice : nullptr, pFeatureLevel ? (uint32_t)*pFeatureLevel : 0u);
    return hr;
  }

  PFN_D3D11CreateDevice real =
      (PFN_D3D11CreateDevice)np::ResolveSystemProc(g_realD3D11, "D3D11CreateDevice");
  if(!real)
  {
    np::Logf("d3d11", "FATAL: no real D3D11CreateDevice");
    return E_FAIL;
  }

  if(CaptureActive() && !s.pfnD3D11CreateDevice)
    np::Logf("d3d11",
             "WARNING: TinecmaTool.dll has no TINECMATOOL_D3D11CreateDevice - forwarding "
             "unwrapped, nothing will be captured");

  HRESULT hr = real(pAdapter, DriverType, Software, Flags, pFeatureLevels, FeatureLevels,
                    SDKVersion, ppDevice, pFeatureLevel, ppImmediateContext);
  np::Logf("d3d11", "SYSTEM D3D11CreateDevice -> hr=0x%08X device=%p", (uint32_t)hr,
           ppDevice ? *ppDevice : nullptr);
  return hr;
}

HRESULT WINAPI NarakaImpl_D3D11CreateDeviceAndSwapChain(
    __in_opt IDXGIAdapter *pAdapter, D3D_DRIVER_TYPE DriverType, HMODULE Software, UINT Flags,
    __in_ecount_opt(FeatureLevels) CONST D3D_FEATURE_LEVEL *pFeatureLevels, UINT FeatureLevels,
    UINT SDKVersion, __in_opt CONST DXGI_SWAP_CHAIN_DESC *pSwapChainDesc,
    __out_opt IDXGISwapChain **ppSwapChain, __out_opt ID3D11Device **ppDevice,
    __out_opt D3D_FEATURE_LEVEL *pFeatureLevel, __out_opt ID3D11DeviceContext **ppImmediateContext)
{
  np::State &s = np::S();

  if(!t_inCreate && CaptureActive() && s.pfnD3D11CreateDeviceAndSwapChain)
  {
    t_inCreate = true;
    HRESULT hr = ((PFN_D3D11CreateDeviceAndSwapChain)s.pfnD3D11CreateDeviceAndSwapChain)(
        pAdapter, DriverType, Software, Flags, pFeatureLevels, FeatureLevels, SDKVersion,
        pSwapChainDesc, ppSwapChain, ppDevice, pFeatureLevel, ppImmediateContext);
    t_inCreate = false;
    np::Logf("d3d11", "BRIDGE D3D11CreateDeviceAndSwapChain -> hr=0x%08X device=%p swapchain=%p",
             (uint32_t)hr, ppDevice ? *ppDevice : nullptr,
             ppSwapChain ? *ppSwapChain : nullptr);
    return hr;
  }

  PFN_D3D11CreateDeviceAndSwapChain real = (PFN_D3D11CreateDeviceAndSwapChain)
      np::ResolveSystemProc(g_realD3D11, "D3D11CreateDeviceAndSwapChain");
  if(!real)
  {
    np::Logf("d3d11", "FATAL: no real D3D11CreateDeviceAndSwapChain");
    return E_FAIL;
  }

  HRESULT hr = real(pAdapter, DriverType, Software, Flags, pFeatureLevels, FeatureLevels,
                    SDKVersion, pSwapChainDesc, ppSwapChain, ppDevice, pFeatureLevel,
                    ppImmediateContext);
  np::Logf("d3d11", "SYSTEM D3D11CreateDeviceAndSwapChain -> hr=0x%08X", (uint32_t)hr);
  return hr;
}

// --- pass-through ----------------------------------------------------------
//
// All 51 exports of the system d3d11.dll on this machine. Anything the game
// imports by name that we do not export is a hard load failure, so the table is
// kept complete rather than "just what Naraka seems to need".

#define NARAKA_D3D11_FORWARD(name) \
  void *WINAPI NarakaImpl_##name(void *a1, void *a2, void *a3, void *a4, void *a5, \
                                 void *a6, void *a7, void *a8, void *a9, \
                                 void *a10, void *a11, void *a12) \
  { \
    return np::ForwardByName(g_realD3D11, #name, a1, a2, a3, a4, a5, a6, a7, a8, \
                             a9, a10, a11, a12); \
  }

// ordinal 1
NARAKA_D3D11_FORWARD(D3D11CreateDeviceForD3D12)
// ordinals 2-15 (D3DKMT* kernel thunks and the UMD openers)
NARAKA_D3D11_FORWARD(D3DKMTCloseAdapter)
NARAKA_D3D11_FORWARD(D3DKMTDestroyAllocation)
NARAKA_D3D11_FORWARD(D3DKMTDestroyContext)
NARAKA_D3D11_FORWARD(D3DKMTDestroyDevice)
NARAKA_D3D11_FORWARD(D3DKMTDestroySynchronizationObject)
NARAKA_D3D11_FORWARD(D3DKMTPresent)
NARAKA_D3D11_FORWARD(D3DKMTQueryAdapterInfo)
NARAKA_D3D11_FORWARD(D3DKMTSetDisplayPrivateDriverFormat)
NARAKA_D3D11_FORWARD(D3DKMTSignalSynchronizationObject)
NARAKA_D3D11_FORWARD(D3DKMTUnlock)
NARAKA_D3D11_FORWARD(D3DKMTWaitForSynchronizationObject)
NARAKA_D3D11_FORWARD(EnableFeatureLevelUpgrade)
NARAKA_D3D11_FORWARD(OpenAdapter10)
NARAKA_D3D11_FORWARD(OpenAdapter10_2)
// ordinals 16-21
NARAKA_D3D11_FORWARD(CreateDirect3D11DeviceFromDXGIDevice)
NARAKA_D3D11_FORWARD(CreateDirect3D11SurfaceFromDXGISurface)
NARAKA_D3D11_FORWARD(D3D11CoreCreateDevice)
NARAKA_D3D11_FORWARD(D3D11CoreCreateLayeredDevice)
NARAKA_D3D11_FORWARD(D3D11CoreGetLayeredDeviceSize)
NARAKA_D3D11_FORWARD(D3D11CoreRegisterLayers)
// ordinal 24
NARAKA_D3D11_FORWARD(D3D11On12CreateDevice)
// ordinals 25-47
NARAKA_D3D11_FORWARD(D3DKMTCreateAllocation)
NARAKA_D3D11_FORWARD(D3DKMTCreateContext)
NARAKA_D3D11_FORWARD(D3DKMTCreateDevice)
NARAKA_D3D11_FORWARD(D3DKMTCreateSynchronizationObject)
NARAKA_D3D11_FORWARD(D3DKMTEscape)
NARAKA_D3D11_FORWARD(D3DKMTGetContextSchedulingPriority)
NARAKA_D3D11_FORWARD(D3DKMTGetDeviceState)
NARAKA_D3D11_FORWARD(D3DKMTGetDisplayModeList)
NARAKA_D3D11_FORWARD(D3DKMTGetMultisampleMethodList)
NARAKA_D3D11_FORWARD(D3DKMTGetRuntimeData)
NARAKA_D3D11_FORWARD(D3DKMTGetSharedPrimaryHandle)
NARAKA_D3D11_FORWARD(D3DKMTLock)
NARAKA_D3D11_FORWARD(D3DKMTOpenAdapterFromHdc)
NARAKA_D3D11_FORWARD(D3DKMTOpenResource)
NARAKA_D3D11_FORWARD(D3DKMTQueryAllocationResidency)
NARAKA_D3D11_FORWARD(D3DKMTQueryResourceInfo)
NARAKA_D3D11_FORWARD(D3DKMTRender)
NARAKA_D3D11_FORWARD(D3DKMTSetAllocationPriority)
NARAKA_D3D11_FORWARD(D3DKMTSetContextSchedulingPriority)
NARAKA_D3D11_FORWARD(D3DKMTSetDisplayMode)
NARAKA_D3D11_FORWARD(D3DKMTSetGammaRamp)
NARAKA_D3D11_FORWARD(D3DKMTSetVidPnSourceOwner)
NARAKA_D3D11_FORWARD(D3DKMTWaitForVerticalBlankEvent)
// ordinals 48-51
NARAKA_D3D11_FORWARD(D3DPerformance_BeginEvent)
NARAKA_D3D11_FORWARD(D3DPerformance_EndEvent)
NARAKA_D3D11_FORWARD(D3DPerformance_GetStatus)
NARAKA_D3D11_FORWARD(D3DPerformance_SetMarker)

}  // extern "C"

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID)
{
  if(reason == DLL_PROCESS_ATTACH)
  {
    DisableThreadLibraryCalls(hinst);

    np::InitState("d3d11");

    g_realD3D11 = np::LoadSystemDll(L"d3d11.dll");
    if(!g_realD3D11)
    {
      np::Logf("d3d11", "FATAL: no system d3d11.dll, proxy cannot function");
      return FALSE;
    }
    np::Logf("d3d11", "system d3d11 loaded at %p", g_realD3D11);

    np::LoadCore("d3d11");
    np::StartMonitor("d3d11");
  }
  else if(reason == DLL_PROCESS_DETACH)
  {
    np::Shutdown("d3d11");
  }

  return TRUE;
}
