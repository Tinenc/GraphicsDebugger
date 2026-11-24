// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d12_test.h"

RD_TEST(D3D12_Shared_Buffer, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Four completed submissions share a DEFAULT CBV at byte offset 256, overwritten by "
      "captured CopyBufferRegion between draws. Supports checkpoint and persistent edit tests.";

  int main()
  {
    const char *uploadSetting = getenv("RENDERDOC_SHARED_BUFFER_UPLOAD");
    const bool upload = uploadSetting && uploadSetting[0] == '1';
    const char *lateSetting = getenv("RENDERDOC_SHARED_BUFFER_LATE_CONSUMER");
    const bool lateConsumer = lateSetting && lateSetting[0] == '1';
    if(!Init())
      return 3;
    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_4_0");
    ID3DBlobPtr ps = Compile(
        "cbuffer Shared : register(b0) { float4 tint; };"
        "float4 main(float4 p:SV_Position,float4 c:COLOR0,float2 uv:TEXCOORD0):SV_Target0"
        "{ return tint; }", "main", "ps_4_0");
    float data[128] = {};
    data[64] = 0.2f; data[65] = 0.45f; data[66] = 0.7f; data[67] = 1.0f;
    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    ID3D12ResourcePtr cb = upload ? MakeBuffer().Upload().Data(data) : MakeBuffer().Data(data);
    ID3D12ResourcePtr source = MakeBuffer().Upload().Size(sizeof(data));
    void *mapped = NULL;
    D3D12_RANGE noRead = {0, 0};
    CHECK_HR(source->Map(0, &noRead, &mapped));
    memcpy(mapped, data, sizeof(data));
    source->Unmap(0, NULL);
    cb->SetName(L"SharedParameterBuffer");
    ID3D12RootSignaturePtr sig = MakeSig({cbvParam(D3D12_SHADER_VISIBILITY_PIXEL, 0, 0)});
    ID3D12PipelineStatePtr pso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(ps);
    ID3D12RootSignaturePtr independentSig;
    ID3D12PipelineStatePtr independentPSO;
    if(lateConsumer)
    {
      independentSig = MakeSig({});
      ID3DBlobPtr independentPS = Compile(
          "float4 main():SV_Target0 { return float4(0.2,0.45,0.7,1.0); }", "main", "ps_4_0");
      independentPSO = MakePSO().RootSig(independentSig).InputLayout().VS(vs).PS(independentPS);
    }
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);
    if(!upload)
      ResourceBarrier(cb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);
    while(Running())
    {
      ID3D12ResourcePtr bb;
      D3D12_CPU_DESCRIPTOR_HANDLE rtv = {};
      for(uint32_t region = 0; region < 4; region++)
      {
        if(upload)
        {
          CHECK_HR(cb->Map(0, &noRead, &mapped));
          memcpy(mapped, data, sizeof(data));
          cb->Unmap(0, NULL);
        }
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
        {
          bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
          rtv = MakeRTV(bb).Format(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB).CreateCPU(0);
          ClearRenderTargetView(cmd, rtv, {0.03f, 0.04f, 0.05f, 1.0f});
          if(lateConsumer)
            setMarker(cmd, "Shared parameter late-consumer variant");
        }
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = cb;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
        if(!upload)
        {
          cmd->ResourceBarrier(1, &barrier);
          cmd->CopyBufferRegion(cb, 0, source, 0, sizeof(data));
          std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
          cmd->ResourceBarrier(1, &barrier);
        }
        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        IASetVertexBuffer(cmd, vb, sizeof(DefaultA2V), 0);
        if(!lateConsumer || region >= 2)
        {
          cmd->SetPipelineState(pso);
          cmd->SetGraphicsRootSignature(sig);
          cmd->SetGraphicsRootConstantBufferView(0, cb->GetGPUVirtualAddress() + 256);
        }
        else
        {
          // Still overwrite the parameter above: the preceding checkpoint contains real old
          // overlay bytes, which must be repaired after restore before the later consumers.
          cmd->SetPipelineState(independentPSO);
          cmd->SetGraphicsRootSignature(independentSig);
        }
        const float width = float(screenWidth) / 4.0f;
        RSSetViewport(cmd, {region * width, 0.0f, width, float(screenHeight), 0.0f, 1.0f});
        RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
        OMSetRenderTargets(cmd, {rtv}, {});
        setMarker(cmd, "Shared parameter region " + std::to_string(region));
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
