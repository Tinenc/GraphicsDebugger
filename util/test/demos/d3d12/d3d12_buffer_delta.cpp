// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d12_test.h"

RD_TEST(D3D12_Buffer_Delta_Ranges, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Four completed DIRECT submissions read two independent CBV ranges of one DEFAULT buffer. "
      "The first two draws use byte offset 256 and the last two use byte offset 512.";

  int main()
  {
    if(!Init())
      return 3;

    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_4_0");
    ID3DBlobPtr ps = Compile(
        "cbuffer Shared : register(b0) { float4 tint; };"
        "float4 main(float4 p:SV_Position,float4 c:COLOR0,float2 uv:TEXCOORD0):SV_Target0"
        "{ return tint; }", "main", "ps_4_0");

    // Padding remains observable in buffer readbacks, including both sides of either parameter.
    // Both CBVs have valid 256-byte extents; only their first float4 is read by the shader.
    float data[256];
    for(size_t i = 0; i < 256; i++)
      data[i] = 0.09375f;
    data[64] = 0.125f;
    data[65] = 0.25f;
    data[66] = 0.5f;
    data[67] = 1.0f;
    data[128] = 0.625f;
    data[129] = 0.5f;
    data[130] = 0.25f;
    data[131] = 1.0f;

    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    ID3D12ResourcePtr cb = MakeBuffer().Data(data);
    cb->SetName(L"SharedParameterBuffer");
    vb->SetName(L"DeltaTriangleVertices");
    MakeCBV(cb).SizeBytes(256).Offset(256).CreateGPU(0);
    MakeCBV(cb).SizeBytes(256).Offset(512).CreateGPU(1);
    const UINT descriptorSize =
        dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);

    ID3D12RootSignaturePtr sig = MakeSig({tableParam(
        D3D12_SHADER_VISIBILITY_PIXEL, D3D12_DESCRIPTOR_RANGE_TYPE_CBV, 0, 0, 1, 0)});
    ID3D12PipelineStatePtr pso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(ps);
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);
    ResourceBarrier(cb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);

    while(Running())
    {
      ID3D12ResourcePtr bb;
      D3D12_CPU_DESCRIPTOR_HANDLE rtv = {};
      for(uint32_t region = 0; region < 4; region++)
      {
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
        {
          bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
          rtv = MakeRTV(bb).Format(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB).CreateCPU(0);
          ClearRenderTargetView(cmd, rtv, {0.03125f, 0.0625f, 0.09375f, 1.0f});
        }

        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        IASetVertexBuffer(cmd, vb, sizeof(DefaultA2V), 0);
        cmd->SetPipelineState(pso);
        cmd->SetGraphicsRootSignature(sig);
        cmd->SetDescriptorHeaps(1, &m_CBVUAVSRV.GetInterfacePtr());
        D3D12_GPU_DESCRIPTOR_HANDLE cbv = m_CBVUAVSRV->GetGPUDescriptorHandleForHeapStart();
        cbv.ptr += UINT64(region < 2 ? 0 : 1) * descriptorSize;
        cmd->SetGraphicsRootDescriptorTable(0, cbv);

        const float width = float(screenWidth) / 4.0f;
        RSSetViewport(cmd, {region * width, 0.0f, width, float(screenHeight), 0.0f, 1.0f});
        RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
        OMSetRenderTargets(cmd, {rtv}, {});
        setMarker(cmd, "Delta parameter region " + std::to_string(region));
        cmd->DrawInstanced(3, 1, 0, 0);

        if(region == 3)
          FinishUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
        CHECK_HR(cmd->Close());
        Submit({cmd});
        GPUSync();
      }
      Present();
    }
    return 0;
  }
};

REGISTER_TEST();
