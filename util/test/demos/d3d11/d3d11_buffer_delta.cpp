// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d11_test.h"

struct BufferDeltaFixture : D3D11GraphicsTest
{
  int Run(bool sharedRanges, bool stages = false)
  {
    if(!Init())
      return 3;
    if(sharedRanges && !ctx1)
      return 4;
    ID3DBlobPtr vsblob = Compile(D3DDefaultVertex, "main", "vs_5_0");
    ID3DBlobPtr psblob = Compile(
        "cbuffer Delta : register(b0) { float4 tint; };"
        "float4 main(float4 p:SV_Position,float4 c:COLOR0,float2 uv:TEXCOORD0):SV_Target0"
        "{ return tint; }", "main", "ps_5_0");
    CreateDefaultInputLayout(vsblob);
    ID3D11VertexShaderPtr vs = CreateVS(vsblob);
    ID3D11PixelShaderPtr ps = CreatePS(psblob);
    ID3D11BufferPtr vb = MakeBuffer().Vertex().Data(DefaultTri);
    float earlyData[256] = {}, lateData[256] = {};
    for(size_t i = 0; i < 256; i += 4)
    {
      earlyData[i] = 0.125f; earlyData[i + 1] = 0.25f;
      earlyData[i + 2] = 0.5f; earlyData[i + 3] = 1.0f;
      lateData[i] = 0.625f; lateData[i + 1] = 0.5f;
      lateData[i + 2] = 0.25f; lateData[i + 3] = 1.0f;
    }
    if(sharedRanges)
      memcpy(earlyData + 128, lateData, 512);
    ID3D11BufferPtr early = MakeBuffer().Constant().Mappable().Data(earlyData);
    ID3D11BufferPtr late = MakeBuffer().Constant().Mappable().Data(lateData);
    const char earlyName[] = "Delta_Early_Or_Shared";
    const char lateName[] = "Delta_Late";
    early->SetPrivateData(WKPDID_D3DDebugObjectName, sizeof(earlyName) - 1, earlyName);
    late->SetPrivateData(WKPDID_D3DDebugObjectName, sizeof(lateName) - 1, lateName);
    ID3D11Texture2DPtr stageTextures[3];
    ID3D11RenderTargetViewPtr stageTargets[3];
    if(stages)
      for(uint32_t i = 0; i < 3; ++i)
      {
        stageTextures[i] = MakeTexture(DXGI_FORMAT_R8G8B8A8_UNORM, screenWidth, screenHeight).RTV();
        stageTargets[i] = MakeRTV(stageTextures[i]);
        const std::string name = "CheckpointStage" + std::to_string(i);
        stageTextures[i]->SetPrivateData(WKPDID_D3DDebugObjectName, (UINT)name.size(), name.c_str());
      }
    while(Running())
    {
      D3D11_MAPPED_SUBRESOURCE a = Map(early, 0, D3D11_MAP_WRITE_DISCARD);
      memcpy(a.pData, earlyData, sizeof(earlyData));
      ctx->Unmap(early, 0);
      D3D11_MAPPED_SUBRESOURCE b = Map(late, 0, D3D11_MAP_WRITE_DISCARD);
      memcpy(b.pData, lateData, sizeof(lateData));
      ctx->Unmap(late, 0);
      ClearRenderTargetView(bbRTV, {0.03125f, 0.0625f, 0.09375f, 1.0f});
      if(stages)
        for(uint32_t i = 0; i < 3; ++i)
          ClearRenderTargetView(stageTargets[i], {0.03125f, 0.0625f, 0.09375f, 1.0f});
      ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
      ctx->IASetInputLayout(defaultLayout);
      IASetVertexBuffer(vb, sizeof(DefaultA2V), 0);
      ctx->VSSetShader(vs, NULL, 0);
      ctx->PSSetShader(ps, NULL, 0);
      ctx->OMSetRenderTargets(1, &bbRTV.GetInterfacePtr(), NULL);
      for(uint32_t region = 0; region < 4; region++)
      {
        if(stages)
        {
          ID3D11RenderTargetView *target =
              region < 3 ? stageTargets[region].GetInterfacePtr() : bbRTV.GetInterfacePtr();
          ctx->OMSetRenderTargets(1, &target, NULL);
        }
        ID3D11Buffer *buffer = sharedRanges || region < 2 ? early.GetInterfacePtr() : late.GetInterfacePtr();
        if(sharedRanges)
        {
          const UINT first = region < 2 ? 0 : 32, count = 16;
          ctx1->PSSetConstantBuffers1(0, 1, &buffer, &first, &count);
        }
        else
          ctx->PSSetConstantBuffers(0, 1, &buffer);
        const float width = float(screenWidth) / 4.0f;
        RSSetViewport({region * width, 0.0f, width, float(screenHeight), 0.0f, 1.0f});
        setMarker("Delta parameter region " + std::to_string(region));
        ctx->Draw(3, 0);
      }
      Present();
    }
    return 0;
  }
};

namespace SeparateBuffers
{
RD_TEST(D3D11_Buffer_Delta, BufferDeltaFixture)
{
  static constexpr const char *Description = "Two parameter buffers with distinct first consumers";
  int main() { return Run(false); }
};
REGISTER_TEST();
}

namespace StageOutputs
{
RD_TEST(D3D11_Checkpoint_Stages, BufferDeltaFixture)
{
  static constexpr const char *Description =
      "Independent stage outputs retain upstream resource versions";
  int main()
  {
    return Run(true, true);
  }
};
REGISTER_TEST();
}

namespace BoundRanges
{
RD_TEST(D3D11_Buffer_Delta_Ranges, BufferDeltaFixture)
{
  static constexpr const char *Description = "Two parameter views at different offsets of one buffer";
  int main() { return Run(true); }
};
REGISTER_TEST();
}
