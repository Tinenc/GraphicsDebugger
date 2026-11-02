// ============================================================================
// d3d12.dll proxy - Naraka D3D12 device bridge
// ============================================================================
//
// Pairs with dxgi.dll. Unity resolves D3D12CreateDevice by name at runtime, so
// the loader picks this DLL up from the game directory.
//
// Why both halves are required: the wrapped factory created through dxgi.dll
// validates the device it is handed. If the device came from native
// D3D12CreateDevice the factory refuses to attach a swapchain -
// "Creating swap chain with non-hooked device!" - and the capture pipeline dies
// with a black HUD. Routing the device creation through
// TINECMATOOL_D3D12CreateDevice keeps the two sides of the handshake wrapped.
//
// Agility SDK note: the game ships D3D12\D3D12Core.dll and the system d3d12.dll
// is only a loader shim for it. Forwarding to the system export (not to
// D3D12Core directly) is what preserves that. Ordinal 99 is unnamed on this
// machine and is re-exported at the same ordinal so ordinal-based imports keep
// resolving.
// ============================================================================

#include "naraka_proxy_common.h"

#include <d3d12.h>
#include <dxgi.h>

namespace {

HMODULE g_realD3D12 = nullptr;

thread_local bool t_inCreate = false;

typedef HRESULT(WINAPI *PFN_D3D12CreateDevice)(IUnknown *pAdapter,
                                              D3D_FEATURE_LEVEL MinimumFeatureLevel, REFIID riid,
                                              void **ppDevice);

bool CaptureActive()
{
  np::State &s = np::S();
  return s.isTarget && s.rdocLoaded &&
         (s.cfg.mode == np::CaptureMode::RenderDoc || s.cfg.mode == np::CaptureMode::Both);
}

}  // namespace

extern "C" {

HRESULT WINAPI NarakaImpl_D3D12CreateDevice(IUnknown *pAdapter,
                                                       D3D_FEATURE_LEVEL MinimumFeatureLevel,
                                                       REFIID riid, void **ppDevice)
{
  np::State &s = np::S();

  if(!t_inCreate && CaptureActive() && s.pfnD3D12CreateDevice)
  {
    t_inCreate = true;
    HRESULT hr = ((PFN_D3D12CreateDevice)s.pfnD3D12CreateDevice)(pAdapter, MinimumFeatureLevel,
                                                                 riid, ppDevice);
    t_inCreate = false;
    np::Logf("d3d12", "BRIDGE D3D12CreateDevice -> hr=0x%08X device=%p level=0x%X", (uint32_t)hr,
             ppDevice ? *ppDevice : nullptr, (uint32_t)MinimumFeatureLevel);
    return hr;
  }

  PFN_D3D12CreateDevice real =
      (PFN_D3D12CreateDevice)np::ResolveSystemProc(g_realD3D12, "D3D12CreateDevice");
  if(!real)
  {
    np::Logf("d3d12", "FATAL: no real D3D12CreateDevice");
    return E_FAIL;
  }

  if(CaptureActive() && !s.pfnD3D12CreateDevice)
    np::Logf("d3d12",
             "WARNING: TinecmaTool.dll has no TINECMATOOL_D3D12CreateDevice - forwarding "
             "unwrapped, the dxgi factory will refuse the swapchain");

  HRESULT hr = real(pAdapter, MinimumFeatureLevel, riid, ppDevice);
  np::Logf("d3d12", "SYSTEM D3D12CreateDevice -> hr=0x%08X device=%p", (uint32_t)hr,
           ppDevice ? *ppDevice : nullptr);
  return hr;
}

// --- pass-through ----------------------------------------------------------
//
// The remaining 18 named exports. D3D12GetInterface / D3D12EnableExperimentalFeatures
// look tempting to intercept, but the core has no bridge for them and the real
// implementations already handle the Agility SDK negotiation correctly.

#define NARAKA_D3D12_FORWARD(name)                                                           \
  void *WINAPI NarakaImpl_##name(void *a1, void *a2, void *a3, void *a4, void *a5, void *a6) \
  {                                                                                          \
    return np::ForwardByName(g_realD3D12, #name, a1, a2, a3, a4, a5, a6);                    \
  }

// Exported at ordinal 99 with no name, matching the system DLL.
void *WINAPI NarakaImpl_NarakaOrdinal99(void *a1, void *a2, void *a3, void *a4, void *a5,
                                        void *a6)
{
  return np::ForwardByOrdinal(g_realD3D12, 99, a1, a2, a3, a4, a5, a6);
}

NARAKA_D3D12_FORWARD(SetAppCompatStringPointer)
NARAKA_D3D12_FORWARD(D3D12GetDebugInterface)
NARAKA_D3D12_FORWARD(D3D12CoreCreateLayeredDevice)
NARAKA_D3D12_FORWARD(D3D12CoreGetLayeredDeviceSize)
NARAKA_D3D12_FORWARD(D3D12CoreRegisterLayers)
NARAKA_D3D12_FORWARD(D3D12CreateRootSignatureDeserializer)
NARAKA_D3D12_FORWARD(D3D12CreateVersionedRootSignatureDeserializer)
NARAKA_D3D12_FORWARD(D3D12DeviceRemovedExtendedData)
NARAKA_D3D12_FORWARD(D3D12EnableExperimentalFeatures)
NARAKA_D3D12_FORWARD(D3D12GetInterface)
NARAKA_D3D12_FORWARD(D3D12PIXEventsReplaceBlock)
NARAKA_D3D12_FORWARD(D3D12PIXGetThreadInfo)
NARAKA_D3D12_FORWARD(D3D12PIXNotifyWakeFromFenceSignal)
NARAKA_D3D12_FORWARD(D3D12PIXReportCounter)
NARAKA_D3D12_FORWARD(D3D12SerializeRootSignature)
NARAKA_D3D12_FORWARD(D3D12SerializeVersionedRootSignature)
NARAKA_D3D12_FORWARD(GetBehaviorValue)

}  // extern "C"

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID)
{
  if(reason == DLL_PROCESS_ATTACH)
  {
    DisableThreadLibraryCalls(hinst);

    np::InitState("d3d12");

    g_realD3D12 = np::LoadSystemDll(L"d3d12.dll");
    if(!g_realD3D12)
    {
      np::Logf("d3d12", "FATAL: no system d3d12.dll, proxy cannot function");
      return FALSE;
    }
    np::Logf("d3d12", "system d3d12 loaded at %p", g_realD3D12);

    np::LoadCore("d3d12");
    np::StartMonitor("d3d12");
  }
  else if(reason == DLL_PROCESS_DETACH)
  {
    np::Shutdown("d3d12");
  }

  return TRUE;
}
