// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d12_test.h"

RD_TEST(D3D12_Buffer_CPU_Dirty_Ranges, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Four completed DIRECT draws consume captured CPU writes to a 2 MiB committed UPLOAD "
      "buffer. Finite Unmap ranges are application evidence, not proof of capture blob size.";

  int main()
  {
    if(!Init())
      return 3;

    const UINT bufferBytes = 2 * 1024 * 1024;
    const UINT earlyOffset = 256;
    const UINT lateOffset = 1024 * 1024 + 512;
    const float earlyInitial[4] = {0.125f, 0.25f, 0.5f, 1.0f};
    const float lateInitial[4] = {0.625f, 0.5f, 0.25f, 1.0f};
    // Every byte is initialized. At any draw the full expected buffer is this sentinel pattern,
    // the two initial float4s below, and the finite writes preceding that draw. No GPU/UAV writes.
    std::vector<float> data(bufferBytes / sizeof(float), 0.09375f);
    memcpy(data.data() + earlyOffset / sizeof(float), earlyInitial, sizeof(earlyInitial));
    memcpy(data.data() + lateOffset / sizeof(float), lateInitial, sizeof(lateInitial));
    ID3D12ResourcePtr cb = MakeBuffer().Upload().Data(data);
    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    cb->SetName(L"CPUWriteParameterBuffer");
    vb->SetName(L"CPUWriteTriangleVertices");
    MakeCBV(cb).SizeBytes(256).Offset(earlyOffset).CreateGPU(0);
    MakeCBV(cb).SizeBytes(256).Offset(lateOffset).CreateGPU(1);
    const UINT descriptorSize =
        dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);

    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_4_0");
    ID3DBlobPtr ps = Compile(
        "cbuffer Shared : register(b0) { float4 tint; };"
        "float4 main(float4 p:SV_Position,float4 c:COLOR0,float2 uv:TEXCOORD0):SV_Target0"
        "{ return tint; }", "main", "ps_4_0");
    ID3D12RootSignaturePtr sig = MakeSig({tableParam(
        D3D12_SHADER_VISIBILITY_PIXEL, D3D12_DESCRIPTOR_RANGE_TYPE_CBV, 0, 0, 1, 0)});
    ID3D12PipelineStatePtr pso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(ps);
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);

    const auto write = [&](UINT offset, const void *value, UINT bytes) {
      void *mapped = NULL;
      const D3D12_RANGE noRead = {0, 0};
      CHECK_HR(cb->Map(0, &noRead, &mapped));
      memcpy((byte *)mapped + offset, value, bytes);
      const D3D12_RANGE written = {offset, SIZE_T(offset) + bytes};
      cb->Unmap(0, &written);
      // RenderDoc currently captures Unmap conservatively as a whole-buffer write. A verifier
      // must inspect the captured chunks before claiming that this exercised a narrow restore.
    };

    const float earlyValues[2] = {0.375f, 0.75f};
    const float lateValues[2][4] = {{0.25f, 0.375f, 0.5f, 1.0f},
                                   {0.75f, 0.625f, 0.125f, 1.0f}};
    while(Running())
    {
      ID3D12ResourcePtr bb;
      D3D12_CPU_DESCRIPTOR_HANDLE rtv = {};
      for(uint32_t region = 0; region < 4; region++)
      {
        // The previous submission is complete before any CPU mutation of its parameter buffer.
        if(region < 2)
          write(earlyOffset, &earlyValues[region], sizeof(float));
        else
          write(lateOffset, lateValues[region - 2], sizeof(lateValues[0]));

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
        setMarker(cmd, "CPU dirty range region " + std::to_string(region));
        cmd->DrawInstanced(3, 1, 0, 0);
        if(region == 3)
          FinishUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
        CHECK_HR(cmd->Close());
        Submit({cmd});
        GPUSync();
      }
      // Reset after the last tested draw, so the next captured frame has the same known baseline.
      write(earlyOffset, earlyInitial, sizeof(earlyInitial));
      write(lateOffset, lateInitial, sizeof(lateInitial));
      Present();
    }
    return 0;
  }
};

REGISTER_TEST();
