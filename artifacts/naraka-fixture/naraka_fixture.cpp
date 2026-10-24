// naraka_fixture.cpp - D3D11 reproduction fixture for the Naraka capture investigation.
//
// Purpose
// -------
// The Naraka client creates its D3D11 device in a way our hooks never observe:
// the 01:14 capture logged zero "Graphics function resolved" lines and no wrapped
// device, even though hooks were installed and the game definitely created a
// device. The suspicion is that UnityPlayer.dll (VMProtect) resolves
// D3D11CreateDevice without going through an import slot we can patch or through
// GetProcAddress we can intercept.
//
// This fixture isolates exactly that variable. Same rendering workload, three
// resolution strategies:
//
//   --resolve=import    call D3D11CreateDevice through the PE import table
//   --resolve=dynamic   LoadLibraryA("d3d11.dll") + GetProcAddress
//   --resolve=manual    LoadLibraryA + walk the export directory by hand
//                       (no GetProcAddress at all - the VMProtect model)
//
// Run each variant under `TinecmaToolcmd capture` and compare the tool log:
// a wrapped device means the hook path worked; no wrapped device means the
// resolution strategy escaped it.
//
// The render workload is deliberately game-shaped (shadow -> gbuffer -> lighting
// -> bloom -> UI) so the resulting .rdc has meaningful draw calls, dispatches
// and named passes to analyse with the MCP tooling.
//
// Build: see build-fixture.cmd in this directory (cl.exe directly, MSBuild is
// blocked by local security policy).

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <d3d11_1.h>
#include <d3dcompiler.h>
#include <dxgi.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>

#include "renderdoc_app.h"

// When the fixture is launched by TinecmaToolcmd (or by any parent that does not
// hand down a console), stdout is not connected to anything and printf output is
// silently dropped. Every diagnostic therefore also goes to fixture.log next to
// the executable, which is what the harness reads.
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
  _snprintf_s(logPath, sizeof(logPath), _TRUNCATE, "%sfixture.log", exePath);
  g_logFile = fopen(logPath, "w");
}

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d3dcompiler.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "gdi32.lib")

// ---------------------------------------------------------------------------
// options

struct Options
{
  const char *api = "d3d11";
  const char *resolve = "import";
  int frames = 8;
  int captureFrame = 4;
  const char *captureDir = nullptr;
};

static Options g_opts;
static RENDERDOC_API_1_4_0 *g_rdoc = nullptr;
static bool g_captureTriggered = false;

// ---------------------------------------------------------------------------
// RenderDoc in-application API. The tool injects TinecmaTool.dll before our
// entry point, so we can ask it to trigger a capture without a hotkey - this is
// what makes the fixture runnable unattended.

static void InitRenderDoc()
{
  HMODULE mod = GetModuleHandleA("TinecmaTool.dll");
  if(!mod)
  {
    printf("[fixture] TinecmaTool.dll not loaded - running without capture\n");
    return;
  }

  // The branded fork exports TINECMATOOL_GetAPI, not RENDERDOC_GetAPI.
  pTINECMATOOL_GetAPI getAPI = (pTINECMATOOL_GetAPI)GetProcAddress(mod, "TINECMATOOL_GetAPI");
  if(!getAPI)
  {
    printf("[fixture] TinecmaTool.dll found but TINECMATOOL_GetAPI missing\n");
    return;
  }

  void *api = nullptr;
  if(getAPI(eRENDERDOC_API_Version_1_4_0, &api) != 1 || !api)
  {
    printf("[fixture] TINECMATOOL_GetAPI(1.4.0) failed\n");
    return;
  }

  g_rdoc = (RENDERDOC_API_1_4_0 *)api;

  int maj = 0, min = 0, pat = 0;
  g_rdoc->GetAPIVersion(&maj, &min, &pat);

  char templ[1024] = {};
  if(g_opts.captureDir)
    _snprintf_s(templ, sizeof(templ), _TRUNCATE, "%s\\fixture", g_opts.captureDir);
  else
    _snprintf_s(templ, sizeof(templ), _TRUNCATE, "%s\\fixture", getenv("TEMP") ? getenv("TEMP") : "C:\\Temp");

  g_rdoc->SetCaptureFilePathTemplate(templ);
  printf("[fixture] RenderDoc in-app API acquired (version %d.%d.%d), template=%s\n", maj, min, pat, templ);
}

static void MaybeCapture(int frame)
{
  if(!g_rdoc || g_captureTriggered)
    return;
  if(frame != g_opts.captureFrame)
    return;

  g_rdoc->TriggerCapture();
  g_captureTriggered = true;
  printf("[fixture] TriggerCapture() at frame %d\n", frame);
}

// Reports what RenderDoc ended up saving, so the harness does not have to guess
// a filename from a timestamp.
static void ReportCaptures()
{
  if(!g_rdoc)
    return;

  uint32_t n = g_rdoc->GetNumCaptures();
  printf("[fixture] captures recorded: %u\n", n);
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
      printf("[fixture] rdc[%u] = %s\n", i, path);
    free(path);
  }
}

// ---------------------------------------------------------------------------
// symbol resolution strategies

typedef HRESULT(WINAPI *PFN_D3D11CreateDevice)(IDXGIAdapter *pAdapter, D3D_DRIVER_TYPE DriverType,
                                               HMODULE Software, UINT Flags,
                                               const D3D_FEATURE_LEVEL *pFeatureLevels,
                                               UINT FeatureLevels, UINT SDKVersion,
                                               ID3D11Device **ppDevice, D3D_FEATURE_LEVEL *pFeatureLevel,
                                               ID3D11DeviceContext **ppImmediateContext);

// Walk the export directory by hand. This deliberately does not touch
// kernel32!GetProcAddress, which is the behaviour we want to reproduce: a
// protection layer that resolves its own imports so that API interception on
// LoadLibrary/GetProcAddress never sees the request.
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
    // Forwarded exports live inside the export directory; d3d11 does not
    // forward D3D11CreateDevice but be defensive.
    if(rva >= dir.VirtualAddress && rva < dir.VirtualAddress + dir.Size)
      return NULL;

    return (void *)(base + rva);
  }

  return NULL;
}

static PFN_D3D11CreateDevice ResolveCreateDevice(const char *mode, HMODULE *outModule)
{
  HMODULE d3d11 = NULL;

  if(strcmp(mode, "import") == 0)
  {
    // Through the PE import table: RenderDoc's IAT hook owns this slot.
    printf("[fixture] resolve=import (IAT slot)\n");
    return &D3D11CreateDevice;
  }

  d3d11 = LoadLibraryA("d3d11.dll");
  if(!d3d11)
  {
    printf("[fixture] LoadLibraryA(d3d11.dll) failed: %lu\n", GetLastError());
    return NULL;
  }
  *outModule = d3d11;

  if(strcmp(mode, "dynamic") == 0)
  {
    printf("[fixture] resolve=dynamic (LoadLibraryA + GetProcAddress)\n");
    return (PFN_D3D11CreateDevice)GetProcAddress(d3d11, "D3D11CreateDevice");
  }

  if(strcmp(mode, "manual") == 0)
  {
    printf("[fixture] resolve=manual (hand-walked export directory, no GetProcAddress)\n");
    return (PFN_D3D11CreateDevice)ManualExportLookup(d3d11, "D3D11CreateDevice");
  }

  printf("[fixture] unknown resolve mode '%s'\n", mode);
  return NULL;
}

// ---------------------------------------------------------------------------
// shaders

static const char *kVS = R"(
struct VSOut { float4 pos : SV_POSITION; float3 nrm : NORMAL; float2 uv : TEXCOORD0; };
VSOut main(uint id : SV_VertexID)
{
  // Fullscreen triangle, reused by every pass. Sized to cover the viewport.
  float2 p = float2((id << 1) & 2, id & 2);
  VSOut o;
  o.pos = float4(p * float2(2, -2) + float2(-1, 1), 0.0, 1.0);
  o.nrm = normalize(float3(p, 1.0));
  o.uv  = p;
  return o;
}
)";

static const char *kPSGBuffer = R"(
struct VSOut { float4 pos : SV_POSITION; float3 nrm : NORMAL; float2 uv : TEXCOORD0; };
struct GBuf { float4 albedo : SV_Target0; float4 normal : SV_Target1; };
GBuf main(VSOut i)
{
  GBuf o;
  o.albedo = float4(i.uv, 0.6, 1.0);
  o.normal = float4(normalize(i.nrm) * 0.5 + 0.5, 1.0);
  return o;
}
)";

static const char *kCSAlbedoTint = R"(
Texture2D<float4> g_albedo : register(t0);
RWTexture2D<float4> g_out : register(u0);
[numthreads(8, 8, 1)]
void main(uint3 dtid : SV_DispatchThreadID)
{
  float4 c = g_albedo.Load(int3(dtid.xy, 0));
  g_out[dtid.xy] = float4(c.rgb * 1.05, c.a);
}
)";

static const char *kPSLighting = R"(
Texture2D<float4> g_albedo : register(t0);
Texture2D<float4> g_normal : register(t1);
SamplerState g_samp : register(s0);
struct VSOut { float4 pos : SV_POSITION; float3 nrm : NORMAL; float2 uv : TEXCOORD0; };
float4 main(VSOut i) : SV_Target
{
  float4 alb = g_albedo.Sample(g_samp, i.uv);
  float3 nrm = g_normal.Sample(g_samp, i.uv).xyz * 2.0 - 1.0;
  float ndl = saturate(dot(normalize(nrm), normalize(float3(0.4, 0.8, 0.3))));
  return float4(alb.rgb * (0.15 + 0.85 * ndl), 1.0);
}
)";

static const char *kPSBloom = R"(
Texture2D<float4> g_src : register(t0);
SamplerState g_samp : register(s0);
struct VSOut { float4 pos : SV_POSITION; float3 nrm : NORMAL; float2 uv : TEXCOORD0; };
float4 main(VSOut i) : SV_Target
{
  float4 c = g_src.Sample(g_samp, i.uv);
  return float4(saturate(c.rgb - 0.7) * 1.5, 1.0);
}
)";

static const char *kPSUI = R"(
struct VSOut { float4 pos : SV_POSITION; float3 nrm : NORMAL; float2 uv : TEXCOORD0; };
float4 main(VSOut i) : SV_Target
{
  return float4(0.9, 0.85, 0.2, 0.55);
}
)";

// ---------------------------------------------------------------------------
// helpers

static ID3DBlob *CompileShader(const char *src, const char *entry, const char *target)
{
  ID3DBlob *blob = NULL;
  ID3DBlob *errs = NULL;
  HRESULT hr = D3DCompile(src, strlen(src), NULL, NULL, NULL, entry, target, D3DCOMPILE_OPTIMIZATION_LEVEL1, 0,
                          &blob, &errs);
  if(FAILED(hr))
  {
    printf("[fixture] D3DCompile(%s/%s) failed hr=0x%08x\n", entry, target, (unsigned)hr);
    if(errs)
    {
      printf("[fixture] %.*s\n", (int)errs->GetBufferSize(), (const char *)errs->GetBufferPointer());
      errs->Release();
    }
    return NULL;
  }
  if(errs)
    errs->Release();
  return blob;
}

static ID3D11VertexShader *MakeVS(ID3D11Device *dev, const char *src, const char *entry)
{
  ID3DBlob *b = CompileShader(src, entry, "vs_5_0");
  if(!b)
    return NULL;
  ID3D11VertexShader *vs = NULL;
  dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), NULL, &vs);
  b->Release();
  return vs;
}

static ID3D11PixelShader *MakePS(ID3D11Device *dev, const char *src, const char *entry)
{
  ID3DBlob *b = CompileShader(src, entry, "ps_5_0");
  if(!b)
    return NULL;
  ID3D11PixelShader *ps = NULL;
  dev->CreatePixelShader(b->GetBufferPointer(), b->GetBufferSize(), NULL, &ps);
  b->Release();
  return ps;
}

static ID3D11ComputeShader *MakeCS(ID3D11Device *dev, const char *src, const char *entry)
{
  ID3DBlob *b = CompileShader(src, entry, "cs_5_0");
  if(!b)
    return NULL;
  ID3D11ComputeShader *cs = NULL;
  dev->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), NULL, &cs);
  b->Release();
  return cs;
}

static ID3D11Texture2D *MakeRT(ID3D11Device *dev, UINT w, UINT h, DXGI_FORMAT fmt, UINT bindFlags,
                               UINT miscFlags = 0)
{
  D3D11_TEXTURE2D_DESC d = {};
  d.Width = w;
  d.Height = h;
  d.MipLevels = 1;
  d.ArraySize = 1;
  d.Format = fmt;
  d.SampleDesc.Count = 1;
  d.Usage = D3D11_USAGE_DEFAULT;
  d.BindFlags = bindFlags;
  d.MiscFlags = miscFlags;
  ID3D11Texture2D *t = NULL;
  dev->CreateTexture2D(&d, NULL, &t);
  return t;
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

// ---------------------------------------------------------------------------
// main

int main(int argc, char **argv)
{
  InitLog();

  // Accept both -opt=value and --opt=value. Single-dash is the only form that
  // survives renderdoccmd's cmdline parser: it treats a leading "--" as either
  // an end-of-options marker or an option-with-value, and neither lands in
  // rest() where our arguments are forwarded from.
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

  printf("[fixture] api=%s resolve=%s frames=%d captureFrame=%d\n", g_opts.api, g_opts.resolve, g_opts.frames,
         g_opts.captureFrame);

  if(strcmp(g_opts.api, "d3d11") != 0)
  {
    printf("[fixture] only --api=d3d11 is implemented in this fixture\n");
    return 3;
  }

  InitRenderDoc();

  const UINT W = 1280, H = 720;

  WNDCLASSA wc = {};
  wc.lpfnWndProc = WndProc;
  wc.hInstance = GetModuleHandleA(NULL);
  wc.lpszClassName = "NarakaFixtureWnd";
  wc.hCursor = LoadCursor(NULL, IDC_ARROW);
  RegisterClassA(&wc);

  HWND hwnd = CreateWindowExA(0, wc.lpszClassName, "Naraka Fixture (D3D11)", WS_OVERLAPPEDWINDOW, 64, 64, W, H,
                              NULL, NULL, wc.hInstance, NULL);
  if(!hwnd)
  {
    printf("[fixture] CreateWindow failed: %lu\n", GetLastError());
    return 4;
  }
  ShowWindow(hwnd, SW_SHOW);

  HMODULE d3d11mod = NULL;
  PFN_D3D11CreateDevice createDevice = ResolveCreateDevice(g_opts.resolve, &d3d11mod);
  if(!createDevice)
  {
    printf("[fixture] could not resolve D3D11CreateDevice\n");
    return 5;
  }

  // Note: no DXGI factory / swapchain is created before the device, matching the
  // order Unity uses on the D3D11 path.
  DXGI_SWAP_CHAIN_DESC scd = {};
  scd.BufferCount = 1;
  scd.BufferDesc.Width = W;
  scd.BufferDesc.Height = H;
  scd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  scd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
  scd.OutputWindow = hwnd;
  scd.SampleDesc.Count = 1;
  scd.Windowed = TRUE;
  scd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

  IDXGIFactory1 *factory = NULL;
  HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void **)&factory);
  if(FAILED(hr))
  {
    printf("[fixture] CreateDXGIFactory1 failed hr=0x%08x\n", (unsigned)hr);
    return 6;
  }

  IDXGIAdapter1 *adapter = NULL;
  IDXGIOutput *output = NULL;
  if(factory->EnumAdapters1(0, &adapter) == S_OK)
  {
    adapter->EnumOutputs(0, &output);
  }

  ID3D11Device *device = NULL;
  ID3D11DeviceContext *ctx = NULL;
  D3D_FEATURE_LEVEL gotLevel = D3D_FEATURE_LEVEL_11_0;
  const D3D_FEATURE_LEVEL wantLevels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};

  UINT createFlags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
  hr = createDevice(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, createFlags, wantLevels, 2, D3D11_SDK_VERSION, &device,
                    &gotLevel, &ctx);

  if(FAILED(hr) || !device)
  {
    printf("[fixture] D3D11CreateDevice failed hr=0x%08x - falling back to swapchain-less path\n", (unsigned)hr);
    return 7;
  }

  printf("[fixture] device created, feature level 0x%04x\n", (unsigned)gotLevel);

  // If the device came from the IAT slot or GetProcAddress, RenderDoc has
  // already wrapped it and QueryInterface for the wrapped interfaces differs.
  // We do not test that here - the tool log is the authority.

  // swapchain for the device
  IDXGISwapChain *swap = NULL;
  hr = factory->CreateSwapChain(device, &scd, &swap);
  if(FAILED(hr))
    printf("[fixture] CreateSwapChain(device) failed hr=0x%08x\n", (unsigned)hr);

  // ---- resources
  ID3D11Texture2D *shadowTex = MakeRT(device, 1024, 1024, DXGI_FORMAT_D32_FLOAT,
                                      D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE);
  ID3D11DepthStencilView *shadowDSV = NULL;
  device->CreateDepthStencilView(shadowTex, NULL, &shadowDSV);

  ID3D11Texture2D *albTex = MakeRT(device, W, H, DXGI_FORMAT_R8G8B8A8_UNORM,
                                   D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE);
  ID3D11Texture2D *nrmTex = MakeRT(device, W, H, DXGI_FORMAT_R8G8B8A8_UNORM,
                                   D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE);
  ID3D11Texture2D *albTintTex = MakeRT(device, W, H, DXGI_FORMAT_R8G8B8A8_UNORM,
                                       D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE);
  ID3D11Texture2D *litTex = MakeRT(device, W, H, DXGI_FORMAT_R8G8B8A8_UNORM,
                                   D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE);
  ID3D11Texture2D *bloomTex = MakeRT(device, W / 4, H / 4, DXGI_FORMAT_R8G8B8A8_UNORM,
                                     D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE);
  ID3D11Texture2D *depthTex = MakeRT(device, W, H, DXGI_FORMAT_D24_UNORM_S8_UINT, D3D11_BIND_DEPTH_STENCIL);

  ID3D11RenderTargetView *albRTV = NULL, *nrmRTV = NULL, *litRTV = NULL, *bloomRTV = NULL, *backRTV = NULL;
  ID3D11ShaderResourceView *albSRV = NULL, *nrmSRV = NULL, *albTintSRV = NULL, *litSRV = NULL, *bloomSRV = NULL;
  ID3D11UnorderedAccessView *albTintUAV = NULL;
  ID3D11DepthStencilView *depthDSV = NULL;

  device->CreateRenderTargetView(albTex, NULL, &albRTV);
  device->CreateRenderTargetView(nrmTex, NULL, &nrmRTV);
  device->CreateRenderTargetView(litTex, NULL, &litRTV);
  device->CreateRenderTargetView(bloomTex, NULL, &bloomRTV);
  device->CreateShaderResourceView(albTex, NULL, &albSRV);
  device->CreateShaderResourceView(nrmTex, NULL, &nrmSRV);
  device->CreateShaderResourceView(albTintTex, NULL, &albTintSRV);
  device->CreateShaderResourceView(litTex, NULL, &litSRV);
  device->CreateShaderResourceView(bloomTex, NULL, &bloomSRV);
  device->CreateUnorderedAccessView(albTintTex, NULL, &albTintUAV);
  device->CreateDepthStencilView(depthTex, NULL, &depthDSV);

  D3D11_TEXTURE2D_DESC backDesc = {};
  {
    ID3D11Texture2D *back = NULL;
    if(swap && SUCCEEDED(swap->GetBuffer(0, __uuidof(ID3D11Texture2D), (void **)&back)) && back)
    {
      device->CreateRenderTargetView(back, NULL, &backRTV);
      back->Release();
    }
  }

  ID3D11SamplerState *samp = NULL;
  {
    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sd.MaxLOD = D3D11_FLOAT32_MAX;
    device->CreateSamplerState(&sd, &samp);
  }

  ID3D11VertexShader *vs = MakeVS(device, kVS, "main");
  ID3D11PixelShader *psGBuffer = MakePS(device, kPSGBuffer, "main");
  ID3D11PixelShader *psLighting = MakePS(device, kPSLighting, "main");
  ID3D11PixelShader *psBloom = MakePS(device, kPSBloom, "main");
  ID3D11PixelShader *psUI = MakePS(device, kPSUI, "main");
  ID3D11ComputeShader *csTint = MakeCS(device, kCSAlbedoTint, "main");

  if(!vs || !psGBuffer || !psLighting || !psBloom || !psUI || !csTint)
  {
    printf("[fixture] shader creation failed\n");
    return 8;
  }

  ID3DUserDefinedAnnotation *annot = NULL;
  ctx->QueryInterface(__uuidof(ID3DUserDefinedAnnotation), (void **)&annot);

  D3D11_VIEWPORT vp = {0.f, 0.f, (float)W, (float)H, 0.f, 1.f};
  D3D11_VIEWPORT vpSmall = {0.f, 0.f, (float)(W / 4), (float)(H / 4), 0.f, 1.f};

  for(int frame = 0; frame < g_opts.frames; frame++)
  {
    MSG msg;
    while(PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE))
    {
      TranslateMessage(&msg);
      DispatchMessageA(&msg);
    }

    // --- pass 1: shadow (depth only)
    if(annot)
      annot->BeginEvent(L"ShadowPass");
    ctx->OMSetRenderTargets(0, NULL, shadowDSV);
    ctx->ClearDepthStencilView(shadowDSV, D3D11_CLEAR_DEPTH, 1.f, 0);
    ctx->RSSetViewports(1, &vpSmall);
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(vs, NULL, 0);
    ctx->PSSetShader(NULL, NULL, 0);
    ctx->Draw(3, 0);
    if(annot)
      annot->EndEvent();

    // --- pass 2: gbuffer (MRT)
    if(annot)
      annot->BeginEvent(L"GBufferPass");
    ID3D11RenderTargetView *rtv2[2] = {albRTV, nrmRTV};
    ctx->OMSetRenderTargets(2, rtv2, depthDSV);
    float clear[4] = {0.f, 0.f, 0.f, 1.f};
    ctx->ClearRenderTargetView(albRTV, clear);
    ctx->ClearRenderTargetView(nrmRTV, clear);
    ctx->ClearDepthStencilView(depthDSV, D3D11_CLEAR_DEPTH, 1.f, 0);
    ctx->RSSetViewports(1, &vp);
    ctx->PSSetShader(psGBuffer, NULL, 0);
    ctx->Draw(3, 0);
    if(annot)
      annot->EndEvent();

    // --- pass 3: compute tint
    if(annot)
      annot->BeginEvent(L"AlbedoTintCS");
    ID3D11UnorderedAccessView *uav = albTintUAV;
    ctx->CSSetShader(csTint, NULL, 0);
    ctx->CSSetShaderResources(0, 1, &albSRV);
    ctx->CSSetUnorderedAccessViews(0, 1, &uav, NULL);
    ctx->Dispatch(W / 8, H / 8, 1);
    ID3D11UnorderedAccessView *nullUAV = NULL;
    ID3D11ShaderResourceView *nullSRV = NULL;
    ctx->CSSetUnorderedAccessViews(0, 1, &nullUAV, NULL);
    ctx->CSSetShaderResources(0, 1, &nullSRV);
    ctx->CSSetShader(NULL, NULL, 0);
    if(annot)
      annot->EndEvent();

    // --- pass 4: deferred lighting
    if(annot)
      annot->BeginEvent(L"LightingPass");
    ctx->OMSetRenderTargets(1, &litRTV, NULL);
    ctx->PSSetShader(psLighting, NULL, 0);
    ctx->PSSetShaderResources(0, 1, &albTintSRV);
    ctx->PSSetShaderResources(1, 1, &nrmSRV);
    ctx->PSSetSamplers(0, 1, &samp);
    ctx->Draw(3, 0);
    if(annot)
      annot->EndEvent();

    // --- pass 5: bloom downsample
    if(annot)
      annot->BeginEvent(L"BloomPass");
    ctx->OMSetRenderTargets(1, &bloomRTV, NULL);
    ctx->RSSetViewports(1, &vpSmall);
    ctx->PSSetShader(psBloom, NULL, 0);
    ctx->PSSetShaderResources(0, 1, &litSRV);
    ctx->Draw(3, 0);
    if(annot)
      annot->EndEvent();

    // --- pass 6: composite + UI overlay to the backbuffer
    if(annot)
      annot->BeginEvent(L"CompositeUI");
    ctx->OMSetRenderTargets(1, &backRTV, NULL);
    ctx->RSSetViewports(1, &vp);
    ctx->PSSetShader(psLighting, NULL, 0);
    ctx->PSSetShaderResources(0, 1, &litSRV);
    ctx->PSSetShaderResources(1, 1, &bloomSRV);
    ctx->Draw(3, 0);

    D3D11_BLEND_DESC bd = {};
    bd.RenderTarget[0].BlendEnable = TRUE;
    bd.RenderTarget[0].SrcBlend = D3D11_BLEND_SRC_ALPHA;
    bd.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    bd.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    bd.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    bd.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ZERO;
    bd.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    bd.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    ID3D11BlendState *blend = NULL;
    device->CreateBlendState(&bd, &blend);
    float bf[4] = {1, 1, 1, 1};
    ctx->OMSetBlendState(blend, bf, 0xffffffff);
    ctx->PSSetShader(psUI, NULL, 0);
    ctx->Draw(3, 0);
    ctx->OMSetBlendState(NULL, NULL, 0xffffffff);
    blend->Release();
    if(annot)
      annot->EndEvent();

    MaybeCapture(frame);

    if(swap)
      swap->Present(0, 0);
    else
      ctx->Flush();

    Sleep(16);
  }

  printf("[fixture] rendered %d frames, exiting\n", g_opts.frames);

  ReportCaptures();

  if(annot)
    annot->Release();
  if(swap)
    swap->Release();
  if(factory)
    factory->Release();
  if(adapter)
    adapter->Release();
  if(output)
    output->Release();
  ctx->Release();
  device->Release();

  DestroyWindow(hwnd);
  return 0;
}
