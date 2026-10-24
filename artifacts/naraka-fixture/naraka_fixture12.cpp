// naraka_fixture12.cpp - D3D12 counterpart of naraka_fixture.cpp.
//
// Same question, different API: can our hooks observe the device creation, and
// does the wrapped device survive the swapchain path?
//
// Three resolution routes for D3D12CreateDevice, mirroring the D3D11 fixture:
//   -resolve=import    through the PE import table
//   -resolve=dynamic   LoadLibraryA("d3d12.dll") + GetProcAddress
//   -resolve=manual    hand-walked export directory, no GetProcAddress
//
// It also exercises the scene DF研究 had to fix in M4: the swapchain is created
// by handing the command queue to the DXGI factory
// (IDXGIFactory2::CreateSwapChainForHwnd). If the factory is not bridged to the
// native one while the device is wrapped, that call fails with
// 0x80004002 E_NOINTERFACE and the game dies at startup - which is exactly what
// happened on the DF client until dxgi_wrapped.cpp grew the bridge.
//
// Build: build-fixture.cmd builds both fixtures.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d12.h>
#include <d3dcompiler.h>
#include <dxgi1_4.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>

#include "renderdoc_app.h"

#pragma comment(lib, "d3d12.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d3dcompiler.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "gdi32.lib")

struct Options
{
  const char *api = "d3d12";
  const char *resolve = "import";
  int frames = 10;
  int captureFrame = 4;
  const char *captureDir = nullptr;
};

static Options g_opts;
static RENDERDOC_API_1_4_0 *g_rdoc = nullptr;
static bool g_captureTriggered = false;
static FILE *g_logFile = NULL;

static void LogF(const char *fmt, ...)
{
  va_list ap;
  va_start(ap, fmt);
  vprintf(fmt, ap);
  va_end(ap);
  fflush(stdout);
  if(g_logFile)
  {
    va_start(ap, fmt);
    vfprintf(g_logFile, fmt, ap);
    va_end(ap);
    fflush(g_logFile);
  }
}

#define printf(...) LogF(__VA_ARGS__)

static void InitLog()
{
  char exePath[MAX_PATH] = {};
  GetModuleFileNameA(NULL, exePath, MAX_PATH);
  char *slash = strrchr(exePath, '\\');
  if(slash)
    *(slash + 1) = '\0';
  char logPath[MAX_PATH * 2] = {};
  _snprintf_s(logPath, sizeof(logPath), _TRUNCATE, "%sfixture12.log", exePath);
  g_logFile = fopen(logPath, "w");
}

// ---------------------------------------------------------------------------
// RenderDoc in-app API. This fork exports TINECMATOOL_GetAPI.

static void InitRenderDoc()
{
  HMODULE mod = GetModuleHandleA("TinecmaTool.dll");
  if(!mod)
  {
    printf("[fx12] TinecmaTool.dll not loaded - running without capture\n");
    return;
  }

  pTINECMATOOL_GetAPI getAPI = (pTINECMATOOL_GetAPI)GetProcAddress(mod, "TINECMATOOL_GetAPI");
  if(!getAPI)
  {
    printf("[fx12] TinecmaTool.dll found but TINECMATOOL_GetAPI missing\n");
    return;
  }

  void *api = nullptr;
  if(getAPI(eRENDERDOC_API_Version_1_4_0, &api) != 1 || !api)
  {
    printf("[fx12] TINECMATOOL_GetAPI(1.4.0) failed\n");
    return;
  }

  g_rdoc = (RENDERDOC_API_1_4_0 *)api;

  int maj = 0, min = 0, pat = 0;
  g_rdoc->GetAPIVersion(&maj, &min, &pat);

  char templ[1024] = {};
  if(g_opts.captureDir)
    _snprintf_s(templ, sizeof(templ), _TRUNCATE, "%s\\fixture12", g_opts.captureDir);
  else
    _snprintf_s(templ, sizeof(templ), _TRUNCATE, "%s\\fixture12", getenv("TEMP") ? getenv("TEMP") : "C:\\Temp");

  g_rdoc->SetCaptureFilePathTemplate(templ);
  printf("[fx12] RenderDoc in-app API acquired (%d.%d.%d), template=%s\n", maj, min, pat, templ);
}

static void MaybeCapture(int frame)
{
  if(!g_rdoc || g_captureTriggered || frame != g_opts.captureFrame)
    return;
  g_rdoc->TriggerCapture();
  g_captureTriggered = true;
  printf("[fx12] TriggerCapture() at frame %d\n", frame);
}

static void ReportCaptures()
{
  if(!g_rdoc)
    return;
  uint32_t n = g_rdoc->GetNumCaptures();
  printf("[fx12] captures recorded: %u\n", n);
  for(uint32_t i = 0; i < n; i++)
  {
    uint32_t len = 0;
    uint64_t t = 0;
    g_rdoc->GetCapture(i, nullptr, &len, &t);
    if(len == 0 || len > 4096)
      continue;
    char *path = (char *)malloc(len + 1);
    memset(path, 0, len + 1);
    if(g_rdoc->GetCapture(i, path, &len, &t))
      printf("[fx12] rdc[%u] = %s\n", i, path);
    free(path);
  }
}

// ---------------------------------------------------------------------------
// resolution

typedef HRESULT(WINAPI *PFN_D3D12CreateDevice)(IUnknown *pAdapter, D3D_FEATURE_LEVEL MinimumFeatureLevel,
                                               REFIID riid, void **ppDevice);

static void *ManualExportLookup(HMODULE mod, const char *name)
{
  BYTE *base = (BYTE *)mod;
  IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
  if(dos->e_magic != IMAGE_DOS_SIGNATURE)
    return NULL;
  IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
  if(nt->Signature != IMAGE_NT_SIGNATURE)
    return NULL;
  IMAGE_DATA_DIRECTORY dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];
  if(dir.VirtualAddress == 0 || dir.Size == 0)
    return NULL;

  IMAGE_EXPORT_DIRECTORY *exp = (IMAGE_EXPORT_DIRECTORY *)(base + dir.VirtualAddress);
  DWORD *names = (DWORD *)(base + exp->AddressOfNames);
  WORD *ordinals = (WORD *)(base + exp->AddressOfNameOrdinals);
  DWORD *functions = (DWORD *)(base + exp->AddressOfFunctions);

  for(DWORD i = 0; i < exp->NumberOfNames; i++)
  {
    const char *candidate = (const char *)(base + names[i]);
    if(strcmp(candidate, name) != 0)
      continue;
    DWORD rva = functions[ordinals[i]];
    if(rva >= dir.VirtualAddress && rva < dir.VirtualAddress + dir.Size)
      return NULL;    // forwarded export
    return (void *)(base + rva);
  }
  return NULL;
}

static PFN_D3D12CreateDevice ResolveCreateDevice(const char *mode)
{
  if(strcmp(mode, "import") == 0)
  {
    printf("[fx12] resolve=import (IAT slot)\n");
    return &D3D12CreateDevice;
  }

  HMODULE d3d12 = LoadLibraryA("d3d12.dll");
  if(!d3d12)
  {
    printf("[fx12] LoadLibraryA(d3d12.dll) failed: %lu\n", GetLastError());
    return NULL;
  }

  if(strcmp(mode, "dynamic") == 0)
  {
    printf("[fx12] resolve=dynamic (LoadLibraryA + GetProcAddress)\n");
    return (PFN_D3D12CreateDevice)GetProcAddress(d3d12, "D3D12CreateDevice");
  }

  if(strcmp(mode, "manual") == 0)
  {
    printf("[fx12] resolve=manual (hand-walked export directory, no GetProcAddress)\n");
    return (PFN_D3D12CreateDevice)ManualExportLookup(d3d12, "D3D12CreateDevice");
  }

  printf("[fx12] unknown resolve mode '%s'\n", mode);
  return NULL;
}

// ---------------------------------------------------------------------------
// shaders

static const char *kVS = R"(
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };
VSOut main(uint id : SV_VertexID)
{
  float2 p = float2((id << 1) & 2, id & 2);
  VSOut o;
  o.pos = float4(p * float2(2, -2) + float2(-1, 1), 0.0, 1.0);
  o.uv  = p;
  return o;
}
)";

static const char *kPSGBuffer = R"(
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };
struct GBuf { float4 albedo : SV_Target0; float4 normal : SV_Target1; };
GBuf main(VSOut i)
{
  GBuf o;
  o.albedo = float4(i.uv, 0.7, 1.0);
  o.normal = float4(normalize(float3(i.uv - 0.5, 1.0)) * 0.5 + 0.5, 1.0);
  return o;
}
)";

static const char *kPSComposite = R"(
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };
float4 main(VSOut i) : SV_Target
{
  return float4(i.uv, 0.4, 1.0);
}
)";

static const char *kCS = R"(
RWTexture2D<float4> g_out : register(u0);
[numthreads(8, 8, 1)]
void main(uint3 dtid : SV_DispatchThreadID)
{
  float2 t = float2(dtid.xy & 63u) / 63.0;
  g_out[dtid.xy] = float4(t, 0.75, 1.0);
}
)";

static ID3DBlob *CompileShader(const char *src, const char *entry, const char *target)
{
  ID3DBlob *blob = NULL, *errs = NULL;
  HRESULT hr = D3DCompile(src, strlen(src), NULL, NULL, NULL, entry, target, D3DCOMPILE_OPTIMIZATION_LEVEL1, 0,
                          &blob, &errs);
  if(FAILED(hr))
  {
    printf("[fx12] D3DCompile(%s/%s) failed hr=0x%08x\n", entry, target, (unsigned)hr);
    if(errs)
    {
      printf("[fx12] %.*s\n", (int)errs->GetBufferSize(), (const char *)errs->GetBufferPointer());
      errs->Release();
    }
    return NULL;
  }
  if(errs)
    errs->Release();
  return blob;
}

static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l)
{
  if(m == WM_DESTROY)
  {
    PostQuitMessage(0);
    return 0;
  }
  return DefWindowProc(h, m, w, l);
}

static void SetName(ID3D12Object *obj, const wchar_t *name)
{
  if(obj)
    obj->SetName(name);
}

// ---------------------------------------------------------------------------

int main(int argc, char **argv)
{
  InitLog();

  for(int i = 1; i < argc; i++)
  {
    const char *a = argv[i];
    while(*a == '-')
      a++;
    if(strncmp(a, "api=", 4) == 0)
      g_opts.api = a + 4;
    else if(strncmp(a, "resolve=", 8) == 0)
      g_opts.resolve = a + 8;
    else if(strncmp(a, "frames=", 7) == 0)
      g_opts.frames = atoi(a + 7);
    else if(strncmp(a, "capture-frame=", 14) == 0)
      g_opts.captureFrame = atoi(a + 14);
    else if(strncmp(a, "capture-dir=", 12) == 0)
      g_opts.captureDir = a + 12;
  }

  printf("[fx12] api=%s resolve=%s frames=%d captureFrame=%d\n", g_opts.api, g_opts.resolve, g_opts.frames,
         g_opts.captureFrame);

  if(strcmp(g_opts.api, "d3d12") != 0)
  {
    printf("[fx12] only -api=d3d12 is implemented here; use naraka_fixture.exe for D3D11\n");
    return 3;
  }

  InitRenderDoc();

  const UINT W = 1280, H = 720;

  WNDCLASSA wc = {};
  wc.lpfnWndProc = WndProc;
  wc.hInstance = GetModuleHandleA(NULL);
  wc.lpszClassName = "NarakaFixture12Wnd";
  wc.hCursor = LoadCursor(NULL, IDC_ARROW);
  RegisterClassA(&wc);

  HWND hwnd = CreateWindowExA(0, wc.lpszClassName, "Naraka Fixture (D3D12)", WS_OVERLAPPEDWINDOW, 64, 64, W, H,
                              NULL, NULL, wc.hInstance, NULL);
  if(!hwnd)
  {
    printf("[fx12] CreateWindow failed: %lu\n", GetLastError());
    return 4;
  }
  ShowWindow(hwnd, SW_SHOW);

  PFN_D3D12CreateDevice createDevice = ResolveCreateDevice(g_opts.resolve);
  if(!createDevice)
  {
    printf("[fx12] could not resolve D3D12CreateDevice\n");
    return 5;
  }

  IDXGIFactory4 *factory = NULL;
  HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory4), (void **)&factory);
  if(FAILED(hr))
  {
    printf("[fx12] CreateDXGIFactory1 failed hr=0x%08x\n", (unsigned)hr);
    return 6;
  }

  IDXGIAdapter1 *adapter = NULL;
  factory->EnumAdapters1(0, &adapter);

  ID3D12Device *device = NULL;
  hr = createDevice(adapter, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), (void **)&device);
  if(FAILED(hr) || !device)
  {
    printf("[fx12] D3D12CreateDevice failed hr=0x%08x\n", (unsigned)hr);
    return 7;
  }
  printf("[fx12] device created\n");

  D3D12_COMMAND_QUEUE_DESC qd = {};
  qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  ID3D12CommandQueue *queue = NULL;
  hr = device->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), (void **)&queue);
  if(FAILED(hr))
  {
    printf("[fx12] CreateCommandQueue failed hr=0x%08x\n", (unsigned)hr);
    return 8;
  }
  printf("[fx12] command queue created\n");

  // M4 scenario: the queue is handed to the DXGI factory to create the
  // swapchain. With the device wrapped and the factory not bridged to the
  // native one, this returns 0x80004002.
  DXGI_SWAP_CHAIN_DESC1 scd = {};
  scd.Width = W;
  scd.Height = H;
  scd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  scd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
  scd.BufferCount = 2;
  scd.SampleDesc.Count = 1;
  scd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;

  IDXGISwapChain1 *swap1 = NULL;
  hr = factory->CreateSwapChainForHwnd(queue, hwnd, &scd, NULL, NULL, &swap1);
  printf("[fx12] CreateSwapChainForHwnd -> 0x%08x%s\n", (unsigned)hr, FAILED(hr) ? "  (FAILED)" : "");
  if(FAILED(hr))
  {
    printf("[fx12] swapchain failed - this is the M4 symptom\n");
    return 9;
  }

  IDXGISwapChain3 *swap = NULL;
  hr = swap1->QueryInterface(__uuidof(IDXGISwapChain3), (void **)&swap);
  printf("[fx12] QI(IDXGISwapChain3) -> 0x%08x\n", (unsigned)hr);
  if(FAILED(hr) || !swap)
  {
    printf("[fx12] swapchain3 unavailable\n");
    return 20;
  }
  swap1->Release();
  swap1 = NULL;

  // ---- descriptor heaps
  D3D12_DESCRIPTOR_HEAP_DESC rtvHeapDesc = {};
  rtvHeapDesc.NumDescriptors = 8;
  rtvHeapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
  ID3D12DescriptorHeap *rtvHeap = NULL;
  device->CreateDescriptorHeap(&rtvHeapDesc, __uuidof(ID3D12DescriptorHeap), (void **)&rtvHeap);
  UINT rtvSize = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);

  D3D12_DESCRIPTOR_HEAP_DESC uavHeapDesc = {};
  uavHeapDesc.NumDescriptors = 4;
  uavHeapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
  uavHeapDesc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
  ID3D12DescriptorHeap *uavHeap = NULL;
  hr = device->CreateDescriptorHeap(&uavHeapDesc, __uuidof(ID3D12DescriptorHeap), (void **)&uavHeap);
  printf("[fx12] uavHeap -> 0x%08x ptr=%p\n", (unsigned)hr, (void *)uavHeap);
  if(!uavHeap)
    return 22;

  D3D12_CPU_DESCRIPTOR_HANDLE rtvBase = rtvHeap->GetCPUDescriptorHandleForHeapStart();

  // ---- render targets
  D3D12_RESOURCE_DESC texDesc = {};
  texDesc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
  texDesc.Width = W;
  texDesc.Height = H;
  texDesc.DepthOrArraySize = 1;
  texDesc.MipLevels = 1;
  texDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  texDesc.SampleDesc.Count = 1;
  texDesc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;

  D3D12_HEAP_PROPERTIES heapProps = {};
  heapProps.Type = D3D12_HEAP_TYPE_DEFAULT;
  heapProps.CreationNodeMask = 1;
  heapProps.VisibleNodeMask = 1;

  D3D12_CLEAR_VALUE clearVal = {};
  clearVal.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  clearVal.Color[3] = 1.f;

  ID3D12Resource *gbufA = NULL, *gbufB = NULL;
  hr = device->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &texDesc, D3D12_RESOURCE_STATE_RENDER_TARGET,
                                       &clearVal, __uuidof(ID3D12Resource), (void **)&gbufA);
  printf("[fx12] CreateCommittedResource(A) -> 0x%08x ptr=%p\n", (unsigned)hr, (void *)gbufA);
  hr = device->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &texDesc, D3D12_RESOURCE_STATE_RENDER_TARGET,
                                       &clearVal, __uuidof(ID3D12Resource), (void **)&gbufB);
  printf("[fx12] CreateCommittedResource(B) -> 0x%08x ptr=%p\n", (unsigned)hr, (void *)gbufB);
  if(!gbufA || !gbufB)
    return 21;
  SetName(gbufA, L"NarakaFixture_GBufferA");
  SetName(gbufB, L"NarakaFixture_GBufferB");

  D3D12_CPU_DESCRIPTOR_HANDLE hA = rtvBase;
  D3D12_CPU_DESCRIPTOR_HANDLE hB = {rtvBase.ptr + rtvSize};
  device->CreateRenderTargetView(gbufA, NULL, hA);
  device->CreateRenderTargetView(gbufB, NULL, hB);
  printf("[fx12] RTVs created\n");

  // compute target
  D3D12_RESOURCE_DESC csDesc = texDesc;
  csDesc.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
  ID3D12Resource *csOut = NULL;
  hr = device->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &csDesc, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                       NULL, __uuidof(ID3D12Resource), (void **)&csOut);
  printf("[fx12] CreateCommittedResource(csOut) -> 0x%08x ptr=%p\n", (unsigned)hr, (void *)csOut);
  if(!csOut)
    return 23;
  SetName(csOut, L"NarakaFixture_ComputeOut");
  D3D12_UNORDERED_ACCESS_VIEW_DESC uavDesc = {};
  uavDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  uavDesc.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
  D3D12_CPU_DESCRIPTOR_HANDLE uavHandle = uavHeap->GetCPUDescriptorHandleForHeapStart();
  device->CreateUnorderedAccessView(csOut, NULL, &uavDesc, uavHandle);
  printf("[fx12] UAV created\n");

  // backbuffers
  ID3D12Resource *backbuf[2] = {};
  D3D12_CPU_DESCRIPTOR_HANDLE backHandle[2];
  for(int i = 0; i < 2; i++)
  {
    hr = swap->GetBuffer(i, __uuidof(ID3D12Resource), (void **)&backbuf[i]);
    printf("[fx12] GetBuffer(%d) -> 0x%08x ptr=%p\n", i, (unsigned)hr, (void *)backbuf[i]);
    if(!backbuf[i])
      return 24;
    backHandle[i].ptr = rtvBase.ptr + rtvSize * (2 + i);
    device->CreateRenderTargetView(backbuf[i], NULL, backHandle[i]);
    SetName(backbuf[i], L"NarakaFixture_BackBuffer");
  }
  printf("[fx12] backbuffers ready\n");

  // ---- root signature (empty), PSOs
  D3D12_ROOT_SIGNATURE_DESC rsDesc = {};
  rsDesc.Flags = D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT;
  ID3DBlob *rsBlob = NULL, *rsErr = NULL;
  hr = D3D12SerializeRootSignature(&rsDesc, D3D_ROOT_SIGNATURE_VERSION_1, &rsBlob, &rsErr);
  if(FAILED(hr))
  {
    printf("[fx12] SerializeRootSignature failed hr=0x%08x\n", (unsigned)hr);
    return 10;
  }

  ID3D12RootSignature *rootSig = NULL;
  hr = device->CreateRootSignature(0, rsBlob->GetBufferPointer(), rsBlob->GetBufferSize(),
                                   __uuidof(ID3D12RootSignature), (void **)&rootSig);
  printf("[fx12] root signature -> 0x%08x\n", (unsigned)hr);
  rsBlob->Release();
  if(!rootSig)
    return 25;

  ID3DBlob *vsBlob = CompileShader(kVS, "main", "vs_5_0");
  ID3DBlob *psGBufBlob = CompileShader(kPSGBuffer, "main", "ps_5_0");
  ID3DBlob *psCompBlob = CompileShader(kPSComposite, "main", "ps_5_0");
  ID3DBlob *csBlob = CompileShader(kCS, "main", "cs_5_0");
  printf("[fx12] shaders compiled  vs=%zu psG=%zu psC=%zu cs=%zu\n", (size_t)vsBlob->GetBufferSize(),
         (size_t)psGBufBlob->GetBufferSize(), (size_t)psCompBlob->GetBufferSize(),
         (size_t)csBlob->GetBufferSize());
  if(!vsBlob || !psGBufBlob || !psCompBlob || !csBlob)
    return 11;

  auto makePSO = [&](ID3DBlob *ps, DXGI_FORMAT rtv0, DXGI_FORMAT rtv1, const wchar_t *name) -> ID3D12PipelineState * {
    D3D12_GRAPHICS_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = rootSig;
    pd.VS = {vsBlob->GetBufferPointer(), vsBlob->GetBufferSize()};
    pd.PS = {ps->GetBufferPointer(), ps->GetBufferSize()};
    pd.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    pd.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    pd.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    pd.SampleMask = UINT_MAX;
    pd.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    pd.NumRenderTargets = (rtv1 == DXGI_FORMAT_UNKNOWN) ? 1 : 2;
    pd.RTVFormats[0] = rtv0;
    if(pd.NumRenderTargets == 2)
      pd.RTVFormats[1] = rtv1;
    pd.SampleDesc.Count = 1;
    ID3D12PipelineState *pso = NULL;
    HRESULT r = device->CreateGraphicsPipelineState(&pd, __uuidof(ID3D12PipelineState), (void **)&pso);
    if(FAILED(r))
    {
      printf("[fx12] CreateGraphicsPipelineState failed hr=0x%08x\n", (unsigned)r);
      return NULL;
    }
    SetName(pso, name);
    return pso;
  };

  ID3D12PipelineState *psoGBuffer = makePSO(psGBufBlob, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_R8G8B8A8_UNORM,
                                            L"NarakaFixture_PSO_GBuffer");
  printf("[fx12] psoGBuffer=%p\n", (void *)psoGBuffer);
  ID3D12PipelineState *psoComposite = makePSO(psCompBlob, DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_UNKNOWN,
                                              L"NarakaFixture_PSO_Composite");
  printf("[fx12] psoComposite=%p\n", (void *)psoComposite);
  if(!psoGBuffer || !psoComposite)
    return 12;

  // A compute PSO cannot share the graphics root signature flags (the IA layout
  // flag is rejected there), so give compute its own empty root signature.
  D3D12_ROOT_SIGNATURE_DESC crsDesc = {};
  crsDesc.Flags = D3D12_ROOT_SIGNATURE_FLAG_NONE;
  ID3DBlob *crsBlob = NULL, *crsErr = NULL;
  hr = D3D12SerializeRootSignature(&crsDesc, D3D_ROOT_SIGNATURE_VERSION_1, &crsBlob, &crsErr);
  printf("[fx12] serialize compute root sig -> 0x%08x\n", (unsigned)hr);
  ID3D12RootSignature *computeRootSig = NULL;
  if(SUCCEEDED(hr))
  {
    hr = device->CreateRootSignature(0, crsBlob->GetBufferPointer(), crsBlob->GetBufferSize(),
                                     __uuidof(ID3D12RootSignature), (void **)&computeRootSig);
    printf("[fx12] create compute root sig -> 0x%08x ptr=%p\n", (unsigned)hr, (void *)computeRootSig);
    crsBlob->Release();
  }

  D3D12_COMPUTE_PIPELINE_STATE_DESC cd = {};
  cd.pRootSignature = computeRootSig;
  cd.CS.pShaderBytecode = csBlob->GetBufferPointer();
  cd.CS.BytecodeLength = csBlob->GetBufferSize();
  printf("[fx12] compute bytecode %zu bytes\n", (size_t)csBlob->GetBufferSize());
  ID3D12PipelineState *psoCompute = NULL;
  hr = device->CreateComputePipelineState(&cd, __uuidof(ID3D12PipelineState), (void **)&psoCompute);
  printf("[fx12] psoCompute -> 0x%08x ptr=%p\n", (unsigned)hr, (void *)psoCompute);
  SetName(psoCompute, L"NarakaFixture_PSO_Compute");

  // ---- allocator / list / fence
  ID3D12CommandAllocator *alloc = NULL;
  hr = device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                                      (void **)&alloc);
  printf("[fx12] allocator -> 0x%08x\n", (unsigned)hr);
  ID3D12GraphicsCommandList *list = NULL;
  hr = device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, NULL,
                                 __uuidof(ID3D12GraphicsCommandList), (void **)&list);
  printf("[fx12] command list -> 0x%08x\n", (unsigned)hr);
  if(!list)
    return 26;
  // CreateCommandList returns an open list; close it so the first Reset is legal.
  list->Close();

  ID3D12Fence *fence = NULL;
  device->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void **)&fence);
  HANDLE fenceEvent = CreateEventA(NULL, FALSE, FALSE, NULL);
  UINT64 fenceVal = 0;

  D3D12_VIEWPORT vp = {0.f, 0.f, (float)W, (float)H, 0.f, 1.f};
  D3D12_RECT scissor = {0, 0, (LONG)W, (LONG)H};

  for(int frame = 0; frame < g_opts.frames; frame++)
  {
    MSG msg;
    while(PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE))
    {
      TranslateMessage(&msg);
      DispatchMessageA(&msg);
    }

    alloc->Reset();
    list->Reset(alloc, NULL);

    // --- gbuffer pass (MRT), two draws to make the pass non-trivial
    D3D12_CPU_DESCRIPTOR_HANDLE gbufHandles[2] = {hA, hB};
    list->OMSetRenderTargets(2, gbufHandles, FALSE, NULL);
    list->RSSetViewports(1, &vp);
    list->RSSetScissorRects(1, &scissor);
    list->SetGraphicsRootSignature(rootSig);
    list->SetPipelineState(psoGBuffer);
    list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    list->DrawInstanced(3, 1, 0, 0);
    list->DrawInstanced(3, 1, 0, 0);

    // --- compute pass
    if(psoCompute)
    {
      list->SetPipelineState(psoCompute);
      list->SetComputeRootSignature(computeRootSig);
      ID3D12DescriptorHeap *heaps[] = {uavHeap};
      list->SetDescriptorHeaps(1, heaps);
      list->Dispatch(W / 8, H / 8, 1);
    }

    // --- composite to backbuffer
    D3D12_RESOURCE_BARRIER pre = {};
    pre.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    pre.Transition.pResource = backbuf[swap->GetCurrentBackBufferIndex()];
    pre.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    pre.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
    pre.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
    list->ResourceBarrier(1, &pre);

    list->OMSetRenderTargets(1, &backHandle[swap->GetCurrentBackBufferIndex()], FALSE, NULL);
    list->SetPipelineState(psoComposite);
    list->SetGraphicsRootSignature(rootSig);
    list->DrawInstanced(3, 1, 0, 0);

    D3D12_RESOURCE_BARRIER post = pre;
    post.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
    post.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
    list->ResourceBarrier(1, &post);

    list->Close();

    ID3D12CommandList *lists[] = {list};
    queue->ExecuteCommandLists(1, lists);

    swap->Present(1, 0);

    queue->Signal(fence, ++fenceVal);
    if(fence->GetCompletedValue() < fenceVal)
    {
      fence->SetEventOnCompletion(fenceVal, fenceEvent);
      WaitForSingleObject(fenceEvent, 2000);
    }

    MaybeCapture(frame);
    Sleep(8);
  }

  printf("[fx12] rendered %d frames, exiting\n", g_opts.frames);
  ReportCaptures();

  CloseHandle(fenceEvent);
  if(fence) fence->Release();
  if(list) list->Release();
  if(alloc) alloc->Release();
  if(psoCompute) psoCompute->Release();
  if(psoComposite) psoComposite->Release();
  if(psoGBuffer) psoGBuffer->Release();
  if(rootSig) rootSig->Release();
  if(csBlob) csBlob->Release();
  if(psCompBlob) psCompBlob->Release();
  if(psGBufBlob) psGBufBlob->Release();
  if(vsBlob) vsBlob->Release();
  if(swap) swap->Release();
  if(backbuf[0]) backbuf[0]->Release();
  if(backbuf[1]) backbuf[1]->Release();
  if(csOut) csOut->Release();
  if(gbufB) gbufB->Release();
  if(gbufA) gbufA->Release();
  if(uavHeap) uavHeap->Release();
  if(rtvHeap) rtvHeap->Release();
  if(queue) queue->Release();
  if(device) device->Release();
  if(adapter) adapter->Release();
  if(factory) factory->Release();

  DestroyWindow(hwnd);
  return 0;
}
