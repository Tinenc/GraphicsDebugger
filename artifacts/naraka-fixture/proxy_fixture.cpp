// ============================================================================
// Naraka proxy fixture
// ============================================================================
//
// Reproduces, without the game, the exact way NarakaBladepoint.exe reaches the
// graphics API:
//
//   * dxgi.dll is a STATIC import (UnityPlayer.dll imports CreateDXGIFactory /
//     CreateDXGIFactory2), so the loader resolves it from the executable's own
//     directory before System32 is ever consulted.
//   * d3d11.dll / d3d12.dll are resolved at RUNTIME by name, the way Unity does
//     it. That path only finds our proxy because the application directory is
//     first in the LoadLibrary search order.
//
// The fixture reports which file each of the three names actually resolved to,
// so "the proxy was used" is established evidence rather than an assumption.
// It then creates a device and a swapchain and presents a few frames: if the
// proxies forward correctly nothing observable changes, which is the point.
//
// It deliberately does NOT load TinecmaTool.dll itself. Everything capture
// related has to come from the proxy layer or not at all.
//
//   proxy_fixture.exe -api=d3d11 -frames=60
//   proxy_fixture.exe -api=d3d12 -frames=60
// ============================================================================

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <d3d11.h>
#include <d3d12.h>
#include <dxgi1_6.h>

#include <cstdio>
#include <cstring>

static const wchar_t *kWndClass = L"NarakaProxyFixture";

static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l)
{
  if(m == WM_DESTROY)
  {
    PostQuitMessage(0);
    return 0;
  }
  return DefWindowProcW(h, m, w, l);
}

static HWND MakeWindow(int w, int h)
{
  WNDCLASSEXW wc = {};
  wc.cbSize = sizeof(wc);
  wc.lpfnWndProc = WndProc;
  wc.hInstance = GetModuleHandleW(nullptr);
  wc.lpszClassName = kWndClass;
  RegisterClassExW(&wc);

  HWND wnd = CreateWindowExW(0, kWndClass, L"Naraka proxy fixture", WS_OVERLAPPEDWINDOW, 80, 80, w,
                             h, nullptr, nullptr, wc.hInstance, nullptr);
  if(wnd)
    ShowWindow(wnd, SW_SHOWNORMAL);
  return wnd;
}

static void PumpMessages()
{
  MSG msg;
  while(PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE))
  {
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }
}

// Reports where a module name actually resolved from. This is the core evidence
// of the test: "dxgi.dll" must map to the fixture's own directory, not System32.
static void ReportModule(const char *name)
{
  HMODULE mod = GetModuleHandleA(name);
  if(!mod)
  {
    printf("  resolve %-10s : NOT LOADED\n", name);
    return;
  }

  char path[MAX_PATH] = {0};
  GetModuleFileNameA(mod, path, MAX_PATH);
  printf("  resolve %-10s : %s\n", name, path);
}

static int RunD3D11(HWND wnd, int frames)
{
  typedef HRESULT(WINAPI * PFN_CreateFactory2)(UINT, REFIID, void **);
  typedef HRESULT(WINAPI * PFN_D3D11CreateDeviceAndSwapChain)(
      IDXGIAdapter *, D3D_DRIVER_TYPE, HMODULE, UINT, const D3D_FEATURE_LEVEL *, UINT, UINT,
      const DXGI_SWAP_CHAIN_DESC *, IDXGISwapChain **, ID3D11Device **, D3D_FEATURE_LEVEL *,
      ID3D11DeviceContext **);

  // Runtime resolution of d3d11.dll, exactly like UnityPlayer does it.
  HMODULE d3d11 = LoadLibraryA("d3d11.dll");
  if(!d3d11)
  {
    printf("  FAIL: LoadLibraryA(d3d11.dll) -> error %lu\n", GetLastError());
    return 1;
  }

  PFN_D3D11CreateDeviceAndSwapChain create =
      (PFN_D3D11CreateDeviceAndSwapChain)GetProcAddress(d3d11, "D3D11CreateDeviceAndSwapChain");
  if(!create)
  {
    printf("  FAIL: no D3D11CreateDeviceAndSwapChain export\n");
    return 1;
  }
  printf("  d3d11 create  : %p\n", (void *)create);

  ReportModule("dxgi.dll");
  ReportModule("d3d11.dll");

  DXGI_SWAP_CHAIN_DESC desc = {};
  desc.BufferCount = 2;
  desc.BufferDesc.Width = 0;
  desc.BufferDesc.Height = 0;
  desc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
  desc.OutputWindow = wnd;
  desc.SampleDesc.Count = 1;
  desc.Windowed = TRUE;
  desc.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

  const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1};
  IDXGISwapChain *swap = nullptr;
  ID3D11Device *dev = nullptr;
  ID3D11DeviceContext *ctx = nullptr;
  D3D_FEATURE_LEVEL got = D3D_FEATURE_LEVEL_11_0;

  HRESULT hr = create(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, levels, 2, D3D11_SDK_VERSION,
                      &desc, &swap, &dev, &got, &ctx);
  printf("  create device : hr=0x%08X device=%p swapchain=%p level=0x%X\n", (unsigned)hr,
         (void *)dev, (void *)swap, (unsigned)got);

  if(FAILED(hr))
    return 2;

  ID3D11Texture2D *backbuffer = nullptr;
  ID3D11RenderTargetView *rtv = nullptr;
  if(SUCCEEDED(swap->GetBuffer(0, __uuidof(ID3D11Texture2D), (void **)&backbuffer)) && backbuffer)
  {
    dev->CreateRenderTargetView(backbuffer, nullptr, &rtv);
    backbuffer->Release();
  }

  for(int i = 0; i < frames; i++)
  {
    PumpMessages();
    if(rtv)
    {
      const FLOAT clear[4] = {0.1f, 0.2f, 0.3f, 1.0f};
      ctx->ClearRenderTargetView(rtv, clear);
      ctx->OMSetRenderTargets(1, &rtv, nullptr);
    }
    swap->Present(1, 0);
    Sleep(16);
  }

  printf("  presented     : %d frames\n", frames);

  if(rtv)
    rtv->Release();
  if(swap)
    swap->Release();
  if(ctx)
    ctx->Release();
  if(dev)
    dev->Release();

  return 0;
}

static int RunD3D12(HWND wnd, int frames)
{
  typedef HRESULT(WINAPI * PFN_CreateFactory2)(UINT, REFIID, void **);
  typedef HRESULT(WINAPI * PFN_D3D12CreateDevice)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);

  HMODULE d3d12 = LoadLibraryA("d3d12.dll");
  if(!d3d12)
  {
    printf("  FAIL: LoadLibraryA(d3d12.dll) -> error %lu\n", GetLastError());
    return 1;
  }

  PFN_D3D12CreateDevice create =
      (PFN_D3D12CreateDevice)GetProcAddress(d3d12, "D3D12CreateDevice");
  if(!create)
  {
    printf("  FAIL: no D3D12CreateDevice export\n");
    return 1;
  }
  printf("  d3d12 create  : %p\n", (void *)create);

  ReportModule("dxgi.dll");
  ReportModule("d3d12.dll");

  ID3D12Device *dev = nullptr;
  HRESULT hr = create(nullptr, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), (void **)&dev);
  printf("  create device : hr=0x%08X device=%p\n", (unsigned)hr, (void *)dev);

  if(FAILED(hr) || !dev)
    return 2;

  IDXGIFactory2 *factory = nullptr;
  hr = CreateDXGIFactory2(0, __uuidof(IDXGIFactory2), (void **)&factory);
  printf("  factory2      : hr=0x%08X factory=%p\n", (unsigned)hr, (void *)factory);

  if(FAILED(hr) || !factory)
  {
    dev->Release();
    return 3;
  }

  ID3D12CommandQueue *queue = nullptr;
  D3D12_COMMAND_QUEUE_DESC qd = {};
  qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  hr = dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void **)&queue);
  printf("  queue         : hr=0x%08X queue=%p\n", (unsigned)hr, (void *)queue);

  // --- ten-argument forwarder regression -----------------------------------
  //
  // D3D11On12CreateDevice takes 10 arguments and is forwarded by the d3d11
  // proxy's generic thunk. This is not a contrived case: it is exactly what
  // killed the real game. Unity creates the D3D12 device, NVIDIA Aftermath
  // immediately sets up D3D11On12, the thunk passed four garbage stack slots,
  // and d3d11.dll faulted at +0xAC321 with 0xC0000005 before the first frame.
  //
  // A forwarding test that only exercises two-argument factories would never
  // have caught that, so the fixture calls this one for real.
  {
    HMODULE d3d11 = LoadLibraryA("d3d11.dll");
    typedef HRESULT(WINAPI * PFN_D3D11On12)(IUnknown *, UINT, const D3D_FEATURE_LEVEL *, UINT,
                                            IUnknown *const *, UINT, UINT, ID3D11Device **,
                                            ID3D11DeviceContext **, D3D_FEATURE_LEVEL *);

    PFN_D3D11On12 on12 =
        d3d11 ? (PFN_D3D11On12)GetProcAddress(d3d11, "D3D11On12CreateDevice") : nullptr;

    if(!on12)
    {
      printf("  d3d11on12     : SKIP (export missing)\n");
    }
    else
    {
      IUnknown *queues[1] = {queue ? (IUnknown *)queue : (IUnknown *)dev};
      ID3D11Device *d11 = nullptr;
      ID3D11DeviceContext *c11 = nullptr;
      D3D_FEATURE_LEVEL got11 = D3D_FEATURE_LEVEL_11_0;

      HRESULT hr12 =
          on12((IUnknown *)dev, 0, nullptr, 0, queues, 1, 0, &d11, &c11, &got11);
      printf("  d3d11on12     : hr=0x%08X device=%p ctx=%p level=0x%X\n", (unsigned)hr12,
             (void *)d11, (void *)c11, (unsigned)got11);

      if(c11)
        c11->Release();
      if(d11)
        d11->Release();
    }
  }

  DXGI_SWAP_CHAIN_DESC1 sd = {};
  sd.Width = 0;
  sd.Height = 0;
  sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  sd.SampleDesc.Count = 1;
  sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
  sd.BufferCount = 2;
  sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
  sd.AlphaMode = DXGI_ALPHA_MODE_IGNORE;

  IDXGISwapChain1 *swap = nullptr;
  hr = factory->CreateSwapChainForHwnd(queue ? (IUnknown *)queue : (IUnknown *)dev, wnd, &sd,
                                       nullptr, nullptr, &swap);
  printf("  swapchain     : hr=0x%08X swapchain=%p\n", (unsigned)hr, (void *)swap);

  if(FAILED(hr))
  {
    if(queue)
      queue->Release();
    factory->Release();
    dev->Release();
    return 4;
  }

  for(int i = 0; i < frames; i++)
  {
    PumpMessages();
    swap->Present(1, 0);
    Sleep(16);
  }
  printf("  presented     : %d frames\n", frames);

  swap->Release();
  if(queue)
    queue->Release();
  factory->Release();
  dev->Release();
  return 0;
}

int main(int argc, char **argv)
{
  // Unbuffered: when this process is killed (or dies) mid-run the harness still
  // needs to see how far it got. A pipe-buffered stdout would lose everything.
  setvbuf(stdout, nullptr, _IONBF, 0);

  const char *api = "d3d11";
  int frames = 60;

  for(int i = 1; i < argc; i++)
  {
    if(strncmp(argv[i], "-api=", 5) == 0)
      api = argv[i] + 5;
    else if(strncmp(argv[i], "-frames=", 8) == 0)
      frames = atoi(argv[i] + 8);
  }

  HWND wnd = MakeWindow(640, 360);
  if(!wnd)
  {
    printf("  FAIL: no window (error %lu)\n", GetLastError());
    return 1;
  }

  printf("proxy_fixture: api=%s frames=%d exe=%s\n", api, frames, argv[0]);

  int rc = 1;
  if(_stricmp(api, "d3d11") == 0)
    rc = RunD3D11(wnd, frames);
  else if(_stricmp(api, "d3d12") == 0)
    rc = RunD3D12(wnd, frames);
  else
    printf("  FAIL: unknown api %s\n", api);

  DestroyWindow(wnd);

  printf("proxy_fixture: exit=%d\n", rc);
  fflush(stdout);

  // Let the proxy's trigger monitor thread observe the last frame before the
  // process tears down; a fast exit truncates the capture it may have started.
  Sleep(300);
  return rc;
}
