#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <cstdio>
#include <cwchar>
#include "E:/GraphicsDebugger/renderdoc/api/app/renderdoc_app.h"
#define HR(x) do { HRESULT result = (x); if(FAILED(result)) { printf("FAIL %s HRESULT=%08lx\n", #x, (unsigned long)result); return 1; } } while(0)
#include <delayimp.h>
struct DiagnosticAnsiString { USHORT Length, MaximumLength; PCHAR Buffer; };
static FARPROC WINAPI RawDelayNotify(unsigned event, PDelayLoadInfo info) {
  if(event != dliNotePreGetProcAddress || _stricmp(info->szDll, "d3d12.dll")) return NULL;
  using RawGetProc = LONG (WINAPI *)(HMODULE, DiagnosticAnsiString *, ULONG, void **);
  auto rawGetProc = (RawGetProc)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "LdrGetProcedureAddress");
  if(!rawGetProc) return NULL;
  DiagnosticAnsiString name = {};
  if(info->dlp.fImportByName) {
    name.Buffer = (PCHAR)info->dlp.szProcName;
    name.Length = (USHORT)strlen(name.Buffer); name.MaximumLength = name.Length;
  }
  void *address = NULL;
  LONG status = rawGetProc(info->hmodCur, info->dlp.fImportByName ? &name : NULL,
                          info->dlp.fImportByName ? 0 : info->dlp.dwOrdinal, &address);
  return status >= 0 ? (FARPROC)address : NULL;
}
extern "C" const PfnDliHook __pfnDliNotifyHook2 = RawDelayNotify;
int main(int argc, char **) {
  if(argc > 1) {
    wchar_t path[MAX_PATH] = {}, command[MAX_PATH + 4] = {};
    GetModuleFileNameW(NULL, path, MAX_PATH);
    swprintf_s(command, L"\"%s\"", path);
    STARTUPINFOW startup = {}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION child = {};
    if(!CreateProcessW(path, command, NULL, NULL, FALSE, 0, NULL, NULL, &startup, &child)) return 2;
    WaitForSingleObject(child.hProcess, 30000);
    DWORD result = 1; GetExitCodeProcess(child.hProcess, &result);
    CloseHandle(child.hThread); CloseHandle(child.hProcess);
    printf("Parent child exit=%lu\n", result); return (int)result;
  }
  HMODULE captureModule = GetModuleHandleW(L"TinecmaTool.dll");
  if(!captureModule) { puts("FAIL: capture DLL not loaded"); return 3; }
  auto getAPI = (pTINECMATOOL_GetAPI)GetProcAddress(captureModule, "TINECMATOOL_GetAPI");
  RENDERDOC_API_1_6_0 *api = NULL;
  if(!getAPI || !getAPI(eRENDERDOC_API_Version_1_6_0, (void **)&api)) return 4;
  auto setLog = (void (__cdecl *)(const char *))GetProcAddress(captureModule, "INTERNAL_SetDebugLogFile");
  if(setLog) setLog("E:/GraphicsDebugger/artifacts/iat-diagnostics-test/graphics-entry-smoke.log");
  // First call resolves the delay slot without the ordinary GetProcAddress IAT path.
  HRESULT probe = D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), NULL);
  printf("Unwrapped capability probe HRESULT=%08lx\n", (unsigned long)probe);
  if(FAILED(probe)) return 7;
  // An ordinary library load requests the existing module scan before real device creation.
  HMODULE scanTrigger = LoadLibraryW(L"version.dll");
  if(!scanTrigger) return 8;
  // Resolve the factory through the native resolver so this factory is deliberately unwrapped.
  using RawGetProc = LONG (WINAPI *)(HMODULE, DiagnosticAnsiString *, ULONG, void **);
  auto rawGetProc = (RawGetProc)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "LdrGetProcedureAddress");
  char exportName[] = "CreateDXGIFactory1";
  DiagnosticAnsiString factoryName = {(USHORT)(sizeof(exportName)-1), (USHORT)sizeof(exportName), exportName};
  void *factoryExport = NULL;
  if(!rawGetProc || rawGetProc(GetModuleHandleW(L"dxgi.dll"), &factoryName, 0, &factoryExport) < 0) return 9;
  auto rawCreateFactory = (HRESULT (WINAPI *)(REFIID,void **))factoryExport;
  IDXGIFactory4 *factory = NULL; HR(rawCreateFactory(IID_PPV_ARGS(&factory)));
  IDXGIAdapter1 *adapter = NULL; HR(factory->EnumAdapters1(0, &adapter));
  ID3D12Device *device = NULL;
  HR(D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)));
  D3D12_COMMAND_QUEUE_DESC queueDesc = {};
  ID3D12CommandQueue *queue = NULL;
  HR(device->CreateCommandQueue(&queueDesc, IID_PPV_ARGS(&queue)));
  WNDCLASSW cls = {}; cls.lpfnWndProc = DefWindowProcW; cls.hInstance = GetModuleHandleW(NULL);
  cls.lpszClassName = L"IATDiagnosticSmoke"; RegisterClassW(&cls);
  HWND window = CreateWindowW(cls.lpszClassName, L"D3D12 capture diagnostic", WS_OVERLAPPEDWINDOW,
                               0, 0, 320, 240, NULL, NULL, cls.hInstance, NULL);
  if(!window) return 5;

  DXGI_SWAP_CHAIN_DESC1 desc = {}; desc.Width = 320; desc.Height = 240;
  desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM; desc.SampleDesc.Count = 1;
  desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; desc.BufferCount = 2;
  desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
  IDXGISwapChain1 *swap = NULL;
  char method[32] = {};
  GetEnvironmentVariableA("GRAPHICS_FACTORY_TEST_METHOD", method, sizeof(method));
  HWND captureWindow = window;
  if(!strcmp(method, "legacy")) {
    DXGI_SWAP_CHAIN_DESC legacyDesc = {};
    legacyDesc.BufferDesc.Width = 320; legacyDesc.BufferDesc.Height = 240;
    legacyDesc.BufferDesc.Format = desc.Format; legacyDesc.SampleDesc = desc.SampleDesc;
    legacyDesc.BufferUsage = desc.BufferUsage; legacyDesc.BufferCount = desc.BufferCount;
    legacyDesc.OutputWindow = window; legacyDesc.Windowed = TRUE; legacyDesc.SwapEffect = desc.SwapEffect;
    IDXGISwapChain *legacy = NULL;
    HR(factory->CreateSwapChain(queue, &legacyDesc, &legacy));
    HR(legacy->QueryInterface(IID_PPV_ARGS(&swap))); legacy->Release();
  } else if(!strcmp(method, "composition")) {
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
    HR(factory->CreateSwapChainForComposition(queue, &desc, NULL, &swap));
    captureWindow = NULL;
  } else {
    HR(factory->CreateSwapChainForHwnd(queue, window, &desc, NULL, NULL, &swap));
  }
  api->StartFrameCapture(NULL, captureWindow);
  HR(swap->Present(0, 0));
  uint32_t captured = api->EndFrameCapture(NULL, captureWindow);
  printf("D3D12 device + swapchain + Present; capture=%u count=%u\n", captured, api->GetNumCaptures());
  Sleep(20000); adapter->Release(); swap->Release(); factory->Release(); queue->Release(); device->Release(); DestroyWindow(window);
  return captured ? 0 : 6;
}
