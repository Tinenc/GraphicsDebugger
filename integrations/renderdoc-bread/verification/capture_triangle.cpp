// Deterministic D3D11 capture used by the DF/Bread integration smoke test.
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <cstdio>
#include <stdexcept>
#include <string>
#include "../../../renderdoc/api/app/renderdoc_app.h"
using Microsoft::WRL::ComPtr;
static void check(HRESULT hr) { if(FAILED(hr)) throw std::runtime_error("D3D call failed: " + std::to_string(hr)); }
int wmain(int argc, wchar_t **argv)
{
  if(argc != 3) { puts("capture_triangle <TinecmaTool.dll> <capture-prefix>"); return 2; }
  try {
    HMODULE module = LoadLibraryW(argv[1]);
    auto getAPI = module ? (pTINECMATOOL_GetAPI)GetProcAddress(module, "TINECMATOOL_GetAPI") : nullptr;
    RENDERDOC_API_1_6_0 *api = nullptr;
    if(!getAPI || !getAPI(eRENDERDOC_API_Version_1_6_0, (void **)&api)) throw std::runtime_error("capture API unavailable");
    char prefix[4096];
    WideCharToMultiByte(CP_UTF8, 0, argv[2], -1, prefix, sizeof(prefix), nullptr, nullptr);
    api->SetCaptureFilePathTemplate(prefix);
    api->SetCaptureOptionU32(eRENDERDOC_Option_DebugOutputMute, 0);
    api->MaskOverlayBits(0, 0);
    ComPtr<ID3D11Device> device; ComPtr<ID3D11DeviceContext> ctx;
    check(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
      D3D11_SDK_VERSION, &device, nullptr, &ctx));
    const char *source = R"(
cbuffer TestColor : register(b0) { float4 Tint; };
struct VIn { float3 pos:POSITION; float3 normal:NORMAL; float2 uv:TEXCOORD0; };
struct VOut { float4 pos:SV_POSITION; float3 normal:NORMAL; float2 uv:TEXCOORD0; };
VOut VS(VIn i) { VOut o; o.pos=float4(i.pos,1); o.normal=i.normal; o.uv=i.uv; return o; }
float4 PS(VOut i):SV_TARGET { return Tint * float4(i.uv,abs(i.normal.z),1); }
)";
    ComPtr<ID3DBlob> vsCode, psCode, errors;
    UINT flags = D3DCOMPILE_DEBUG | D3DCOMPILE_SKIP_OPTIMIZATION;
    check(D3DCompile(source, strlen(source), "BreadTriangle.hlsl", nullptr, nullptr, "VS", "vs_5_0", flags, 0, &vsCode, &errors));
    check(D3DCompile(source, strlen(source), "BreadTriangle.hlsl", nullptr, nullptr, "PS", "ps_5_0", flags, 0, &psCode, &errors));
    ComPtr<ID3D11VertexShader> vs; ComPtr<ID3D11PixelShader> ps; ComPtr<ID3D11InputLayout> layout;
    check(device->CreateVertexShader(vsCode->GetBufferPointer(), vsCode->GetBufferSize(), nullptr, &vs));
    check(device->CreatePixelShader(psCode->GetBufferPointer(), psCode->GetBufferSize(), nullptr, &ps));
    D3D11_INPUT_ELEMENT_DESC elements[] = {
      {"POSITION",0,DXGI_FORMAT_R32G32B32_FLOAT,0,0,D3D11_INPUT_PER_VERTEX_DATA,0},
      {"NORMAL",0,DXGI_FORMAT_R32G32B32_FLOAT,0,12,D3D11_INPUT_PER_VERTEX_DATA,0},
      {"TEXCOORD",0,DXGI_FORMAT_R32G32_FLOAT,0,24,D3D11_INPUT_PER_VERTEX_DATA,0}};
    check(device->CreateInputLayout(elements, 3, vsCode->GetBufferPointer(), vsCode->GetBufferSize(), &layout));
    float vertices[] = {-0.8f,-0.8f,0, 0,0,1, 0,0, 0,0.8f,0, 0,0,1, .5f,1, .8f,-.8f,0, 0,0,1, 1,0};
    unsigned short indices[] = {0,1,2}; float tint[] = {1,.8f,.4f,1};
    auto buffer = [&](UINT size, UINT bind, const void *data) {
      D3D11_BUFFER_DESC desc = {}; desc.ByteWidth=size; desc.Usage=D3D11_USAGE_DEFAULT; desc.BindFlags=bind;
      D3D11_SUBRESOURCE_DATA init = {}; init.pSysMem=data; ComPtr<ID3D11Buffer> result;
      check(device->CreateBuffer(&desc, &init, &result)); return result;
    };
    auto vb=buffer(sizeof(vertices),D3D11_BIND_VERTEX_BUFFER,vertices);
    auto ib=buffer(sizeof(indices),D3D11_BIND_INDEX_BUFFER,indices);
    auto cb=buffer(sizeof(tint),D3D11_BIND_CONSTANT_BUFFER,tint);
    D3D11_TEXTURE2D_DESC td = {}; td.Width=256; td.Height=256; td.MipLevels=1; td.ArraySize=1;
    td.Format=DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count=1; td.BindFlags=D3D11_BIND_RENDER_TARGET|D3D11_BIND_SHADER_RESOURCE;
    ComPtr<ID3D11Texture2D> target; ComPtr<ID3D11RenderTargetView> rtv;
    check(device->CreateTexture2D(&td,nullptr,&target)); check(device->CreateRenderTargetView(target.Get(),nullptr,&rtv));
    const char *name="BreadVerificationColor"; target->SetPrivateData(WKPDID_D3DDebugObjectName, (UINT)strlen(name), name);
    api->StartFrameCapture(device.Get(), nullptr);
    auto rtvPtr=rtv.Get(); ctx->OMSetRenderTargets(1,&rtvPtr,nullptr);
    float clear[]={.03f,.04f,.06f,1}; ctx->ClearRenderTargetView(rtv.Get(),clear);
    D3D11_VIEWPORT vp={0,0,256,256,0,1}; ctx->RSSetViewports(1,&vp);
    UINT stride=32,offset=0; auto vbPtr=vb.Get(); ctx->IASetVertexBuffers(0,1,&vbPtr,&stride,&offset);
    ctx->IASetIndexBuffer(ib.Get(),DXGI_FORMAT_R16_UINT,0); ctx->IASetInputLayout(layout.Get());
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(vs.Get(),nullptr,0); ctx->PSSetShader(ps.Get(),nullptr,0);
    auto cbPtr=cb.Get(); ctx->PSSetConstantBuffers(0,1,&cbPtr); ctx->DrawIndexed(3,0,0); ctx->Flush();
    if(!api->EndFrameCapture(device.Get(),nullptr)) throw std::runtime_error("EndFrameCapture failed");
    char path[4096]; uint32_t length=sizeof(path); uint64_t timestamp=0;
    if(!api->GetCapture(0,path,&length,&timestamp)) throw std::runtime_error("capture output missing");
    puts(path); return 0;
  } catch(const std::exception &e) { fprintf(stderr,"%s\n",e.what()); return 1; }
}
