// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d11_test.h"

RD_TEST(D3D11_Shared_Buffer, D3D11GraphicsTest)
{
  static constexpr const char *Description =
      "Four draws share a constant buffer with a non-zero member offset; captured uploads and "
      "copies overwrite it between consumers. Persistent edits must survive both writes.";

  int main()
  {
    if(!Init())
      return 3;
    ID3DBlobPtr vsblob = Compile(D3DDefaultVertex, "main", "vs_4_0");
    ID3DBlobPtr psblob = Compile(
        "cbuffer Shared : register(b0) { float4 padding[16]; float4 tint; };"
        "float4 main(float4 p:SV_Position,float4 c:COLOR0,float2 uv:TEXCOORD0):SV_Target0"
        "{ return tint; }", "main", "ps_4_0");
    CreateDefaultInputLayout(vsblob);
    ID3D11VertexShaderPtr vs = CreateVS(vsblob);
    ID3D11PixelShaderPtr ps = CreatePS(psblob);
    ID3D11BufferPtr vb = MakeBuffer().Vertex().Data(DefaultTri);
    float data[128] = {};
    data[64] = 0.2f; data[65] = 0.45f; data[66] = 0.7f; data[67] = 1.0f;
    ID3D11BufferPtr cb = MakeBuffer().Constant().Mappable().Data(data);
    ID3D11BufferPtr source = MakeBuffer().Constant().Data(data);
    const char name[] = "SharedParameterBuffer";
    cb->SetPrivateData(WKPDID_D3DDebugObjectName, sizeof(name) - 1, name);
    while(Running())
    {
      ClearRenderTargetView(bbRTV, {0.03f, 0.04f, 0.05f, 1.0f});
      ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
      ctx->IASetInputLayout(defaultLayout);
      IASetVertexBuffer(vb, sizeof(DefaultA2V), 0);
      ctx->VSSetShader(vs, NULL, 0);
      ctx->PSSetShader(ps, NULL, 0);
      ctx->PSSetConstantBuffers(0, 1, &cb.GetInterfacePtr());
      ctx->OMSetRenderTargets(1, &bbRTV.GetInterfacePtr(), NULL);
      for(uint32_t region = 0; region < 4; region++)
      {
        if((region % 2) == 0)
        {
          D3D11_MAPPED_SUBRESOURCE mapped = Map(cb, 0, D3D11_MAP_WRITE_DISCARD);
          memcpy(mapped.pData, data, sizeof(data));
          ctx->Unmap(cb, 0);
        }
        else
          ctx->CopyResource(cb, source);
        const float width = float(screenWidth) / 4.0f;
        RSSetViewport({region * width, 0.0f, width, float(screenHeight), 0.0f, 1.0f});
        setMarker("Shared parameter region " + std::to_string(region));
        ctx->Draw(3, 0);
      }
      Present();
    }
    return 0;
  }
};

REGISTER_TEST();
