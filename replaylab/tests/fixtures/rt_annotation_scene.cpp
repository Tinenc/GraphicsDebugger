// Controlled capture for real-model annotation acceptance. No native window is created.
#include <d3d11_1.h>
#include <d3dcompiler.h>
#include <windows.h>
#include <wrl/client.h>
#include <cmath>
#include <cstdio>
#include <stdexcept>
#include <vector>
#include "renderdoc/api/app/renderdoc_app.h"

using Microsoft::WRL::ComPtr;
static void check(HRESULT result)
{
  if(FAILED(result))
    throw std::runtime_error("D3D operation failed");
}
template <class T>
static void name(T *object, const char *text)
{
  object->SetPrivateData(WKPDID_D3DDebugObjectName, UINT(strlen(text)), text);
}
struct Vertex
{
  float p[3], n[3];
};
struct Target
{
  ComPtr<ID3D11Texture2D> texture;
  ComPtr<ID3D11RenderTargetView> rtv;
  ComPtr<ID3D11ShaderResourceView> srv;
};
const char *shader = R"hlsl(
cbuffer ObjectParameters : register(b0) { float4 BaseColor; float4 Placement; };
struct Geometry { float3 position : POSITION; float3 normal : NORMAL; };
struct Surface { float4 position : SV_Position; float3 normal : NORMAL; };
Surface GeometryVS(Geometry input) {
  Surface o;
  o.position = float4((input.position.x * .8 + input.position.z * .35) * Placement.z + Placement.x,
      (input.position.y * .9 + input.position.z * .2) * Placement.z + Placement.y,
      .5 + input.position.z * .05 + input.position.x * .005, 1);
  o.normal = input.normal; return o;
}
float4 MaterialPS(Surface input) : SV_Target {
  float lighting = .3 + .7 * saturate(dot(normalize(input.normal), normalize(float3(-.3,.7,-.6))));
  return float4(BaseColor.rgb * lighting, 1);
}
Texture2D<float4> SceneColor : register(t0);
Texture2D<float4> GlowColor : register(t1);
SamplerState LinearClamp : register(s0);
struct Fullscreen { float4 position : SV_Position; float2 uv : TEXCOORD0; };
Fullscreen FullscreenVS(uint id : SV_VertexID) {
  Fullscreen o; o.uv = float2((id << 1) & 2, id & 2);
  o.position = float4(o.uv * float2(2,-2) + float2(-1,1), 0, 1); return o;
}
float4 BloomPS(Fullscreen input) : SV_Target {
  float3 glow = 0; float weightSum = 0;
  for(int y = -4; y <= 4; ++y) for(int x = -4; x <= 4; ++x) {
    float weight = exp(-(x*x+y*y)/10.0);
    float3 scene = SceneColor.Sample(LinearClamp, input.uv + float2(x*8.0/960, y*8.0/640)).rgb;
    glow += max(scene - .9, 0) * weight; weightSum += weight;
  }
  return float4(glow / weightSum, 1);
}
float4 CompositePS(Fullscreen input) : SV_Target {
  float3 scene = SceneColor.Sample(LinearClamp, input.uv).rgb;
  float3 glow = GlowColor.Sample(LinearClamp, input.uv).rgb;
  float3 hdr = (scene + 1.25 * glow) * float3(1.06, .96, .86);
  float3 mapped = hdr / (1 + hdr);
  return float4(pow(saturate(mapped), 1.0/2.2), 1);
}
)hlsl";

int main(int argc, char **argv)
{
  try
  {
    if(argc != 3)
      return 2;
    HMODULE module = LoadLibraryA(argv[1]);
    auto getAPI = reinterpret_cast<pRENDERDOC_GetAPI>(GetProcAddress(module, "RENDERDOC_GetAPI"));
    RENDERDOC_API_1_6_0 *api = nullptr;
    if(!getAPI || !getAPI(eRENDERDOC_API_Version_1_6_0, reinterpret_cast<void **>(&api)))
      return 3;
    api->SetCaptureFilePathTemplate(argv[2]);
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> ctx;
    check(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
                            D3D11_SDK_VERSION, &device, nullptr, &ctx));
    ComPtr<ID3DUserDefinedAnnotation> markers;
    check(ctx.As(&markers));
    auto compile = [&](const char *entry, const char *profile) {
      ComPtr<ID3DBlob> result, error;
      HRESULT code =
          D3DCompile(shader, strlen(shader), "AnnotationAcceptance.hlsl", nullptr, nullptr, entry,
                     profile, D3DCOMPILE_DEBUG | D3DCOMPILE_SKIP_OPTIMIZATION, 0, &result, &error);
      if(FAILED(code) && error)
        fprintf(stderr, "%s\n", static_cast<const char *>(error->GetBufferPointer()));
      check(code);
      return result;
    };
    auto geometry = compile("GeometryVS", "vs_5_0"), full = compile("FullscreenVS", "vs_5_0");
    auto material = compile("MaterialPS", "ps_5_0"), bloom = compile("BloomPS", "ps_5_0"),
         composite = compile("CompositePS", "ps_5_0");
    ComPtr<ID3D11VertexShader> geometryVS, fullVS;
    ComPtr<ID3D11PixelShader> materialPS, bloomPS, compositePS;
    check(device->CreateVertexShader(geometry->GetBufferPointer(), geometry->GetBufferSize(),
                                     nullptr, &geometryVS));
    check(device->CreateVertexShader(full->GetBufferPointer(), full->GetBufferSize(), nullptr,
                                     &fullVS));
    check(device->CreatePixelShader(material->GetBufferPointer(), material->GetBufferSize(),
                                    nullptr, &materialPS));
    check(device->CreatePixelShader(bloom->GetBufferPointer(), bloom->GetBufferSize(), nullptr,
                                    &bloomPS));
    check(device->CreatePixelShader(composite->GetBufferPointer(), composite->GetBufferSize(),
                                    nullptr, &compositePS));
    name(geometryVS.Get(), "GeometryVS");
    name(materialPS.Get(), "LambertMaterialPS");
    name(bloomPS.Get(), "BloomThresholdBlurPS");
    name(compositePS.Get(), "ToneMapCompositePS");
    D3D11_INPUT_ELEMENT_DESC elements[] = {
        {"POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0},
        {"NORMAL", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 12, D3D11_INPUT_PER_VERTEX_DATA, 0}};
    ComPtr<ID3D11InputLayout> layout;
    check(device->CreateInputLayout(elements, 2, geometry->GetBufferPointer(),
                                    geometry->GetBufferSize(), &layout));
    auto target = [&](DXGI_FORMAT format, const char *label) {
      Target t;
      D3D11_TEXTURE2D_DESC d = {};
      d.Width = 960;
      d.Height = 640;
      d.MipLevels = 1;
      d.ArraySize = 1;
      d.Format = format;
      d.SampleDesc.Count = 1;
      d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
      check(device->CreateTexture2D(&d, nullptr, &t.texture));
      name(t.texture.Get(), label);
      check(device->CreateRenderTargetView(t.texture.Get(), nullptr, &t.rtv));
      check(device->CreateShaderResourceView(t.texture.Get(), nullptr, &t.srv));
      return t;
    };
    Target scene = target(DXGI_FORMAT_R16G16B16A16_FLOAT, "SceneHDR");
    Target glow = target(DXGI_FORMAT_R16G16B16A16_FLOAT, "BloomBlur");
    Target final = target(DXGI_FORMAT_R8G8B8A8_UNORM, "FinalAnnotatedOutput");
    D3D11_TEXTURE2D_DESC dd = {};
    dd.Width = 960;
    dd.Height = 640;
    dd.MipLevels = dd.ArraySize = 1;
    dd.SampleDesc.Count = 1;
    dd.Format = DXGI_FORMAT_D32_FLOAT;
    dd.BindFlags = D3D11_BIND_DEPTH_STENCIL;
    ComPtr<ID3D11Texture2D> depth;
    ComPtr<ID3D11DepthStencilView> dsv;
    check(device->CreateTexture2D(&dd, nullptr, &depth));
    check(device->CreateDepthStencilView(depth.Get(), nullptr, &dsv));
    auto buffer = [&](const void *data, UINT size, UINT flags) {
      D3D11_BUFFER_DESC d = {};
      d.ByteWidth = size;
      d.BindFlags = flags;
      D3D11_SUBRESOURCE_DATA initial = {};
      initial.pSysMem = data;
      ComPtr<ID3D11Buffer> result;
      check(device->CreateBuffer(&d, data ? &initial : nullptr, &result));
      return result;
    };
    std::vector<Vertex> cube;
    std::vector<unsigned> cubeIndices;
    for(int axis = 0; axis < 3; ++axis)
      for(int sign : {-1, 1})
      {
        unsigned start = unsigned(cube.size());
        int u = (axis + 1) % 3, v = (axis + 2) % 3;
        for(int corner = 0; corner < 4; ++corner)
        {
          Vertex vert = {};
          vert.p[axis] = float(sign);
          vert.n[axis] = float(sign);
          vert.p[u] = (corner == 0 || corner == 3) ? -1.f : 1.f;
          vert.p[v] = corner < 2 ? -1.f : 1.f;
          cube.push_back(vert);
        }
        for(unsigned index : {0u, 1u, 2u, 0u, 2u, 3u})
          cubeIndices.push_back(start + index);
      }
    std::vector<Vertex> sphere;
    std::vector<unsigned> sphereIndices;
    for(int y = 0; y <= 12; ++y)
      for(int x = 0; x <= 24; ++x)
      {
        float latitude = float(y) * 3.14159265f / 12, longitude = float(x) * 6.2831853f / 24;
        Vertex vert = {
            {sinf(latitude) * cosf(longitude), cosf(latitude), sinf(latitude) * sinf(longitude)}, {}};
        memcpy(vert.n, vert.p, sizeof(vert.n));
        sphere.push_back(vert);
      }
    for(unsigned y = 0; y < 12; ++y)
      for(unsigned x = 0; x < 24; ++x)
      {
        unsigned a = y * 25 + x, b = a + 25;
        for(unsigned i : {a, b, a + 1, a + 1, b, b + 1})
          sphereIndices.push_back(i);
      }
    auto cubeVB = buffer(cube.data(), UINT(cube.size() * sizeof(Vertex)), D3D11_BIND_VERTEX_BUFFER);
    auto cubeIB = buffer(cubeIndices.data(), UINT(cubeIndices.size() * 4), D3D11_BIND_INDEX_BUFFER);
    auto sphereVB =
        buffer(sphere.data(), UINT(sphere.size() * sizeof(Vertex)), D3D11_BIND_VERTEX_BUFFER);
    auto sphereIB =
        buffer(sphereIndices.data(), UINT(sphereIndices.size() * 4), D3D11_BIND_INDEX_BUFFER);
    auto constants = buffer(nullptr, 32, D3D11_BIND_CONSTANT_BUFFER);
    name(constants.Get(), "ObjectParameters");
    D3D11_RASTERIZER_DESC raster = {};
    raster.FillMode = D3D11_FILL_SOLID;
    raster.CullMode = D3D11_CULL_NONE;
    raster.DepthClipEnable = TRUE;
    ComPtr<ID3D11RasterizerState> rs;
    check(device->CreateRasterizerState(&raster, &rs));
    D3D11_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    ComPtr<ID3D11SamplerState> ss;
    check(device->CreateSamplerState(&sampler, &ss));
    api->StartFrameCapture(device.Get(), nullptr);
    D3D11_VIEWPORT viewport = {0, 0, 960, 640, 0, 1};
    ctx->RSSetViewports(1, &viewport);
    ctx->RSSetState(rs.Get());
    float clear[] = {.015f, .025f, .05f, 1};
    ctx->ClearRenderTargetView(scene.rtv.Get(), clear);
    ctx->ClearDepthStencilView(dsv.Get(), D3D11_CLEAR_DEPTH, 1, 0);
    ID3D11RenderTargetView *rt = scene.rtv.Get();
    ctx->OMSetRenderTargets(1, &rt, dsv.Get());
    ctx->IASetInputLayout(layout.Get());
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(geometryVS.Get(), nullptr, 0);
    ctx->PSSetShader(materialPS.Get(), nullptr, 0);
    ID3D11Buffer *cb = constants.Get();
    ctx->VSSetConstantBuffers(0, 1, &cb);
    ctx->PSSetConstantBuffers(0, 1, &cb);
    auto draw = [&](const wchar_t *label, ID3D11Buffer *vb, ID3D11Buffer *ib, UINT count,
                    const float *parameters) {
      markers->BeginEvent(label);
      ctx->UpdateSubresource(constants.Get(), 0, nullptr, parameters, 0, 0);
      UINT stride = sizeof(Vertex), offset = 0;
      ctx->IASetVertexBuffers(0, 1, &vb, &stride, &offset);
      ctx->IASetIndexBuffer(ib, DXGI_FORMAT_R32_UINT, 0);
      ctx->DrawIndexed(count, 0, 0);
      markers->EndEvent();
    };
    const float yellow[] = {3.6f, 2.4f, .12f, 1, -.45f, 0, .28f, 0},
                blue[] = {.12f, .5f, .9f, 1, .5f, 0, .29f, 0};
    draw(L"Scene geometry: left cube", cubeVB.Get(), cubeIB.Get(), UINT(cubeIndices.size()), yellow);
    draw(L"Scene geometry: right sphere", sphereVB.Get(), sphereIB.Get(),
         UINT(sphereIndices.size()), blue);
    ctx->IASetInputLayout(nullptr);
    ctx->VSSetShader(fullVS.Get(), nullptr, 0);
    ID3D11SamplerState *sample = ss.Get();
    ctx->PSSetSamplers(0, 1, &sample);
    markers->BeginEvent(L"Bloom bright-pass and blur");
    rt = glow.rtv.Get();
    ctx->OMSetRenderTargets(1, &rt, nullptr);
    ctx->PSSetShader(bloomPS.Get(), nullptr, 0);
    ID3D11ShaderResourceView *inputs[] = {scene.srv.Get(), nullptr};
    ctx->PSSetShaderResources(0, 2, inputs);
    ctx->Draw(3, 0);
    markers->EndEvent();
    markers->BeginEvent(L"Final tone mapping and glow composite");
    rt = final.rtv.Get();
    ctx->OMSetRenderTargets(1, &rt, nullptr);
    ctx->PSSetShader(compositePS.Get(), nullptr, 0);
    inputs[1] = glow.srv.Get();
    ctx->PSSetShaderResources(0, 2, inputs);
    ctx->Draw(3, 0);
    markers->EndEvent();
    ctx->Flush();
    if(!api->EndFrameCapture(device.Get(), nullptr))
      return 4;
    printf("captures=%u cubeIndices=%u sphereIndices=%u\n", api->GetNumCaptures(),
           unsigned(cubeIndices.size()), unsigned(sphereIndices.size()));
    return api->GetNumCaptures() == 1 ? 0 : 5;
  }
  catch(const std::exception &error)
  {
    fprintf(stderr, "%s\n", error.what());
    return 1;
  }
}
