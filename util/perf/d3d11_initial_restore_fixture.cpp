// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// Deterministic hidden-window fixture for bounded native initial-restore batching.
#define WIN32_LEAN_AND_MEAN
#include <d3d11.h>
#include <d3dcompiler.h>
#include <windows.h>
#include <wrl/client.h>
#include <vector>
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "d3dcompiler.lib")
#pragma comment(lib, "user32.lib")
using Microsoft::WRL::ComPtr;

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int)
{
  WNDCLASSW windowClass = {};
  windowClass.hInstance = instance;
  windowClass.lpfnWndProc = DefWindowProcW;
  windowClass.lpszClassName = L"BreadInitialRestoreFixture";
  if(!RegisterClassW(&windowClass))
    return 2;
  HWND window = CreateWindowW(windowClass.lpszClassName, L"Initial restore fixture",
                              WS_OVERLAPPEDWINDOW, 0, 0, 320, 180, NULL, NULL, instance, NULL);
  if(!window)
    return 3;
  DXGI_SWAP_CHAIN_DESC swapDescription = {};
  swapDescription.BufferDesc.Width = 320;
  swapDescription.BufferDesc.Height = 180;
  swapDescription.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  swapDescription.SampleDesc.Count = 1;
  swapDescription.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
  swapDescription.BufferCount = 1;
  swapDescription.OutputWindow = window;
  swapDescription.Windowed = TRUE;
  ComPtr<ID3D11Device> device;
  ComPtr<ID3D11DeviceContext> context;
  ComPtr<IDXGISwapChain> swapchain;
  const D3D_FEATURE_LEVEL requested[] = {D3D_FEATURE_LEVEL_11_0};
  if(FAILED(D3D11CreateDeviceAndSwapChain(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, requested, 1,
                                          D3D11_SDK_VERSION, &swapDescription, &swapchain, &device,
                                          NULL, &context)))
    return 4;
  ComPtr<ID3D11Texture2D> backbuffer;
  ComPtr<ID3D11RenderTargetView> target;
  if(FAILED(swapchain->GetBuffer(0, IID_PPV_ARGS(&backbuffer))) ||
     FAILED(device->CreateRenderTargetView(backbuffer.Get(), NULL, &target)))
    return 5;
  const char vertexSource[] =
      "float4 main(uint v:SV_VertexID):SV_Position {"
      "float2 p=float2((v<<1)&2,v&2);return float4(p*float2(2,-2)+float2(-1,1),0,1);}";
  const char pixelSource[] =
      "cbuffer Parameters:register(b0){float4 tint;}"
      "float4 main():SV_Target0{return tint;}";
  ComPtr<ID3DBlob> vertexCode, pixelCode, diagnostics;
  if(FAILED(D3DCompile(vertexSource, sizeof(vertexSource) - 1, NULL, NULL, NULL, "main", "vs_5_0",
                       0, 0, &vertexCode, &diagnostics)) ||
     FAILED(D3DCompile(pixelSource, sizeof(pixelSource) - 1, NULL, NULL, NULL, "main", "ps_5_0", 0,
                       0, &pixelCode, &diagnostics)))
    return 6;
  ComPtr<ID3D11VertexShader> vertex;
  ComPtr<ID3D11PixelShader> pixel;
  if(FAILED(device->CreateVertexShader(vertexCode->GetBufferPointer(), vertexCode->GetBufferSize(),
                                       NULL, &vertex)) ||
     FAILED(device->CreatePixelShader(pixelCode->GetBufferPointer(), pixelCode->GetBufferSize(),
                                      NULL, &pixel)))
    return 7;
  std::vector<ComPtr<ID3D11Buffer>> parameters(1024);
  std::vector<float> values(1024, 0.0f);
  for(size_t index = 0; index < parameters.size(); ++index)
  {
    values[0] = float(index % 7 + 1) / 8.0f;
    values[1] = 0.375f;
    values[2] = 0.625f;
    values[3] = 1.0f;
    D3D11_BUFFER_DESC description = {};
    description.ByteWidth = UINT(values.size() * sizeof(float));
    description.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    description.Usage = D3D11_USAGE_DEFAULT;
    D3D11_SUBRESOURCE_DATA data = {values.data(), 0, 0};
    if(FAILED(device->CreateBuffer(&description, &data, &parameters[index])))
      return 8;
  }
  const float clear[4] = {0.03125f, 0.0625f, 0.125f, 1.0f};
  for(unsigned frame = 0; frame < 3600; ++frame)
  {
    context->ClearRenderTargetView(target.Get(), clear);
    context->OMSetRenderTargets(1, target.GetAddressOf(), NULL);
    context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context->VSSetShader(vertex.Get(), NULL, 0);
    context->PSSetShader(pixel.Get(), NULL, 0);
    for(size_t index = 0; index < parameters.size(); ++index)
    {
      const D3D11_VIEWPORT viewport = {
          float(index % 32) * 10.0f, float(index / 32) * 5.0f, 10.0f, 5.0f, 0.0f, 1.0f};
      context->RSSetViewports(1, &viewport);
      context->PSSetConstantBuffers(0, 1, parameters[index].GetAddressOf());
      context->Draw(3, 0);
      // Read before writing so each buffer genuinely needs a restorable frame
      // initial source. Read-only buffers are already omitted by capture/replay.
      values[0] = float(index % 7 + 1) / 8.0f + 0.0625f;
      context->UpdateSubresource(parameters[index].Get(), 0, NULL, values.data(), 0, 0);
      context->Draw(3, 0);
      values[0] -= 0.0625f;
      context->UpdateSubresource(parameters[index].Get(), 0, NULL, values.data(), 0, 0);
    }
    if(FAILED(swapchain->Present(0, 0)))
      break;
    Sleep(16);
  }
  context->ClearState();
  DestroyWindow(window);
  return 0;
}
