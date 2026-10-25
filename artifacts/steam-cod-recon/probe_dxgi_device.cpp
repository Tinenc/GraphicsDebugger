// Minimal probe: does a plain (unwrapped) ID3D12Device answer QueryInterface(IDXGIDevice)?
// Run without the capture tool so the answer is about the OS/D3D12Core, not about RenderDoc.
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <cstdio>

static void Report(const char *what, HRESULT hr)
{
  printf("%-46s 0x%08lx %s\n", what, (unsigned long)hr, SUCCEEDED(hr) ? "OK" : "FAIL");
}

int main()
{
  ID3D12Device *device = NULL;
  HRESULT hr = D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device));
  Report("D3D12CreateDevice(NULL)", hr);
  if(FAILED(hr) || !device)
    return 1;

  IDXGIDevice *dxgiDevice = NULL;
  hr = device->QueryInterface(IID_PPV_ARGS(&dxgiDevice));
  Report("native device -> IDXGIDevice", hr);

  IDXGIObject *dxgiObject = NULL;
  hr = device->QueryInterface(IID_PPV_ARGS(&dxgiObject));
  Report("native device -> IDXGIObject", hr);

  ID3D12Device1 *device1 = NULL;
  hr = device->QueryInterface(IID_PPV_ARGS(&device1));
  Report("native device -> ID3D12Device1", hr);
  if(SUCCEEDED(hr))
    device1->Release();

  // Highest interface the installed SDK knows about - shows what the device really supports.
  ID3D12Device14 *device14 = NULL;
  hr = device->QueryInterface(IID_PPV_ARGS(&device14));
  Report("native device -> ID3D12Device14", hr);
  if(SUCCEEDED(hr))
    device14->Release();

  if(SUCCEEDED(device->QueryInterface(IID_PPV_ARGS(&dxgiDevice))) && dxgiDevice)
  {
    IDXGIAdapter *adapter = NULL;
    hr = dxgiDevice->GetAdapter(&adapter);
    Report("IDXGIDevice::GetAdapter", hr);
    if(SUCCEEDED(hr) && adapter)
    {
      IDXGIFactory2 *factory = NULL;
      hr = adapter->GetParent(IID_PPV_ARGS(&factory));
      Report("adapter -> GetParent(IDXGIFactory2)", hr);
      if(SUCCEEDED(hr))
        factory->Release();
      adapter->Release();
    }
    dxgiDevice->Release();
  }

  device->Release();
  return 0;
}
