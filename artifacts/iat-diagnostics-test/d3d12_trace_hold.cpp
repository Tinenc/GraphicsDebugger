#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <cstdio>
#include <cwchar>
#include "E:/GraphicsDebugger/renderdoc/api/app/renderdoc_app.h"
#define HR(x) do { HRESULT result = (x); if(FAILED(result)) { printf("FAIL %s HRESULT=%08lx\n", #x, (unsigned long)result); return 1; } } while(0)
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
  ID3D12Device *device = NULL;
  HR(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)));
  D3D12_COMMAND_QUEUE_DESC queueDesc = {};
  ID3D12CommandQueue *queue = NULL;
  HR(device->CreateCommandQueue(&queueDesc, IID_PPV_ARGS(&queue)));
  WNDCLASSW cls = {}; cls.lpfnWndProc = DefWindowProcW; cls.hInstance = GetModuleHandleW(NULL);
  cls.lpszClassName = L"IATDiagnosticSmoke"; RegisterClassW(&cls);
  HWND window = CreateWindowW(cls.lpszClassName, L"D3D12 capture diagnostic", WS_OVERLAPPEDWINDOW,
                               0, 0, 320, 240, NULL, NULL, cls.hInstance, NULL);
  if(!window) return 5;
  IDXGIFactory4 *factory = NULL; HR(CreateDXGIFactory1(IID_PPV_ARGS(&factory)));
  DXGI_SWAP_CHAIN_DESC1 desc = {}; desc.Width = 320; desc.Height = 240;
  desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM; desc.SampleDesc.Count = 1;
  desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; desc.BufferCount = 2;
  desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
  IDXGISwapChain1 *swap = NULL;
  HR(factory->CreateSwapChainForHwnd(queue, window, &desc, NULL, NULL, &swap));
  api->StartFrameCapture(NULL, window);
  HR(swap->Present(0, 0));
  uint32_t captured = api->EndFrameCapture(NULL, window);
  printf("D3D12 device + swapchain + Present; capture=%u count=%u\n", captured, api->GetNumCaptures());
  Sleep(20000); swap->Release(); factory->Release(); queue->Release(); device->Release(); DestroyWindow(window);
  return captured ? 0 : 6;
}
