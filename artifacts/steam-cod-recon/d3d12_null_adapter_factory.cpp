// Verifies the native DXGI factory bridge on the path Call of Duty HQ likely takes.
//
// COD's cod.exe imports D3D12CreateDevice by ordinal (#101, see d3d12-ordinal.def), never
// imports dxgi.dll statically, and exports D3D12SDKPath/Version. The plausible swapchain
// route is therefore:
//
//   D3D12CreateDevice(NULL, ...) -> QI(IDXGIDevice) -> GetAdapter -> GetParent(IDXGIFactory)
//
// If the factory comes back native, the swapchain is never wrapped and EndFrameCapture
// returns 0. The adapter-recovery branch added to D3D12CreateDevice_hook is what keeps that
// from happening; this fixture is the check for it.
//
// GOP_FACTORY_MODE:
//   null-parent       (default) NULL adapter, factory via adapter->GetParent      <- new case
//   explicit-parent   adapter passed in, factory via adapter->GetParent           <- known green
//   null-native       NULL adapter, factory via raw LdrGetProcedureAddress        <- control
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <cstdio>
#include <cstdarg>
#include <cstring>
#include "../../renderdoc/api/app/renderdoc_app.h"

static FILE *g_log = NULL;

static void Log(const char *fmt, ...)
{
  va_list args;
  va_start(args, fmt);
  vfprintf(stdout, fmt, args);
  va_end(args);
  fputc('\n', stdout);
  fflush(stdout);

  if(g_log)
  {
    va_start(args, fmt);
    vfprintf(g_log, fmt, args);
    va_end(args);
    fputc('\n', g_log);
    fflush(g_log);
  }
}

#define HR(x)                                                                                      \
  do                                                                                               \
  {                                                                                                \
    HRESULT hr_ = (x);                                                                             \
    if(FAILED(hr_))                                                                                \
    {                                                                                              \
      Log("FAIL %s hr=0x%08lx", #x, (unsigned long)hr_);                                           \
      return 1;                                                                                    \
    }                                                                                              \
  } while(0)

struct DiagnosticAnsiString
{
  USHORT Length, MaximumLength;
  PCHAR Buffer;
};

// Bypass the dxgi.dll export hooks and hand back the real CreateDXGIFactory1.
static HRESULT RawCreateFactory1(REFIID riid, void **ppFactory)
{
  using RawGetProc = LONG(WINAPI *)(HMODULE, DiagnosticAnsiString *, ULONG, void **);
  auto rawGetProc = (RawGetProc)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "LdrGetProcedureAddress");
  if(!rawGetProc)
    return E_FAIL;

  char exportName[] = "CreateDXGIFactory1";
  DiagnosticAnsiString name = {(USHORT)(sizeof(exportName) - 1), (USHORT)sizeof(exportName),
                               exportName};
  void *address = NULL;
  if(rawGetProc(GetModuleHandleW(L"dxgi.dll"), &name, 0, &address) < 0 || !address)
    return E_FAIL;

  auto create = (HRESULT(WINAPI *)(REFIID, void **))address;
  return create(riid, ppFactory);
}

int main()
{
  g_log = fopen("d3d12_null_adapter_factory.log", "w");

  char mode[64] = {};
  GetEnvironmentVariableA("GOP_FACTORY_MODE", mode, sizeof(mode));
  if(mode[0] == '\0')
    strcpy_s(mode, "null-parent");

  HMODULE captureModule = GetModuleHandleW(L"TinecmaTool.dll");
  if(!captureModule)
  {
    Log("FAIL: capture DLL not loaded into the process");
    return 3;
  }

  auto getAPI = (pTINECMATOOL_GetAPI)GetProcAddress(captureModule, "TINECMATOOL_GetAPI");
  RENDERDOC_API_1_6_0 *api = NULL;
  if(!getAPI || !getAPI(eRENDERDOC_API_Version_1_6_0, (void **)&api))
  {
    Log("FAIL: TINECMATOOL_GetAPI unavailable");
    return 4;
  }

  auto setLog = (void(__cdecl *)(const char *))GetProcAddress(captureModule,
                                                              "INTERNAL_SetDebugLogFile");
  if(setLog)
    setLog("d3d12_null_adapter_factory_capture.log");

  Log("mode=%s", mode);

  IDXGIAdapter1 *adapter = NULL;

  if(!strcmp(mode, "explicit-parent"))
  {
    IDXGIFactory1 *ownFactory = NULL;
    HR(RawCreateFactory1(IID_PPV_ARGS(&ownFactory)));
    HR(ownFactory->EnumAdapters1(0, &adapter));
    ownFactory->Release();
    Log("explicit adapter=%p", adapter);
  }

  ID3D12Device *device = NULL;
  HR(D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)));
  Log("device=%p", device);

  if(!adapter)
  {
    IDXGIDevice *dxgiDevice = NULL;
    HR(device->QueryInterface(IID_PPV_ARGS(&dxgiDevice)));
    HR(dxgiDevice->GetAdapter(&adapter));
    dxgiDevice->Release();
    Log("adapter from device=%p", adapter);
  }

  IDXGIFactory2 *factory = NULL;

  if(!strcmp(mode, "null-native"))
  {
    IDXGIFactory1 *raw = NULL;
    HR(RawCreateFactory1(IID_PPV_ARGS(&raw)));
    HR(raw->QueryInterface(IID_PPV_ARGS(&factory)));
    raw->Release();
    Log("factory via raw CreateDXGIFactory1=%p", factory);
  }
  else
  {
    HR(adapter->GetParent(IID_PPV_ARGS(&factory)));
    Log("factory via adapter GetParent=%p", factory);
  }

  D3D12_COMMAND_QUEUE_DESC queueDesc = {};
  ID3D12CommandQueue *queue = NULL;
  HR(device->CreateCommandQueue(&queueDesc, IID_PPV_ARGS(&queue)));

  WNDCLASSW cls = {};
  cls.lpfnWndProc = DefWindowProcW;
  cls.hInstance = GetModuleHandleW(NULL);
  cls.lpszClassName = L"NullAdapterFactoryFixture";
  RegisterClassW(&cls);
  HWND window = CreateWindowW(cls.lpszClassName, L"D3D12 null-adapter factory fixture",
                              WS_OVERLAPPEDWINDOW, 0, 0, 320, 240, NULL, NULL, cls.hInstance, NULL);
  if(!window)
  {
    Log("FAIL: CreateWindow");
    return 5;
  }

  DXGI_SWAP_CHAIN_DESC1 desc = {};
  desc.Width = 320;
  desc.Height = 240;
  desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  desc.SampleDesc.Count = 1;
  desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
  desc.BufferCount = 2;
  desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;

  IDXGISwapChain1 *swapchain = NULL;
  HR(factory->CreateSwapChainForHwnd(queue, window, &desc, NULL, NULL, &swapchain));
  Log("swapchain=%p", swapchain);

  api->StartFrameCapture(NULL, window);
  HR(swapchain->Present(0, 0));
  HR(swapchain->Present(0, 0));
  uint32_t captured = api->EndFrameCapture(NULL, window);

  Log("RESULT mode=%s captured=%u total=%u", mode, captured, api->GetNumCaptures());

  Sleep(15000);

  swapchain->Release();
  queue->Release();
  factory->Release();
  adapter->Release();
  device->Release();
  DestroyWindow(window);
  if(g_log)
    fclose(g_log);

  return captured ? 0 : 6;
}
