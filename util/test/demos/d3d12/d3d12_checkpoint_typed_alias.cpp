// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d12_test.h"

RD_TEST(D3D12_Checkpoint_Typed_Alias, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Two overlapping UNKNOWN-layout render targets: explicit activation, full Clear/Copy "
      "initialization, then two accumulating draws per owner in separate completed submissions.";

  int main()
  {
    if(!Init())
      return 3;
    D3D12_RESOURCE_DESC desc = {};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = screenWidth;
    desc.Height = screenHeight;
    desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
    desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    D3D12_HEAP_DESC heapDesc = {};
    heapDesc.SizeInBytes = dev->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
    heapDesc.Flags = D3D12_HEAP_FLAG_ALLOW_ONLY_RT_DS_TEXTURES;
    heapDesc.Properties.Type = D3D12_HEAP_TYPE_DEFAULT;
    heapDesc.Properties.CreationNodeMask = heapDesc.Properties.VisibleNodeMask = 1;
    ID3D12HeapPtr heap;
    CHECK_HR(dev->CreateHeap(&heapDesc, __uuidof(ID3D12Heap), (void **)&heap));
    heap->SetName(L"CheckpointTypedAliasHeap");
    ID3D12ResourcePtr targets[2], seed;
    D3D12_CPU_DESCRIPTOR_HANDLE views[2];
    for(UINT i = 0; i < 2; i++)
    {
      CHECK_HR(dev->CreatePlacedResource(heap, 0, &desc, D3D12_RESOURCE_STATE_COMMON, NULL,
                                         __uuidof(ID3D12Resource), (void **)&targets[i]));
      targets[i]->SetName((L"CheckpointTypedAlias" + std::to_wstring(i)).c_str());
      views[i] = MakeRTV(targets[i]).Format(desc.Format).CreateCPU(i + 1);
    }
    CHECK_HR(dev->CreateCommittedResource(&heapDesc.Properties, D3D12_HEAP_FLAG_NONE, &desc,
                                          D3D12_RESOURCE_STATE_COMMON, NULL,
                                          __uuidof(ID3D12Resource), (void **)&seed));
    seed->SetName(L"CheckpointTypedAliasCopySeed");
    const auto seedView = MakeRTV(seed).Format(desc.Format).CreateCPU(3);
    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);
    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_4_0");
    ID3DBlobPtr ps = Compile("cbuffer Color : register(b0) { float4 tint; };"
                            "float4 main():SV_Target0 { return tint; }", "main", "ps_4_0");
    ID3D12RootSignaturePtr sig = MakeSig({constParam(D3D12_SHADER_VISIBILITY_PIXEL, 0, 0, 4)});
    ID3D12PipelineStatePtr pso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(ps);
    while(Running())
    {
      for(UINT region = 0; region < 4; region++)
      {
        const UINT owner = region / 2;
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
        {
          auto bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
          auto rtv = MakeRTV(bb).Format(desc.Format).CreateCPU(0);
          ClearRenderTargetView(cmd, rtv, {0.04f, 0.04f, 0.04f, 1.0f});
          ResourceBarrier(cmd, seed, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_RENDER_TARGET);
          ClearRenderTargetView(cmd, seedView, {0.04f, 0.1f, 0.35f, 1.0f});
          ResourceBarrier(cmd, seed, D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COMMON);
        }
        if(region % 2 == 0)
        {
          D3D12_RESOURCE_BARRIER activation = {};
          activation.Type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
          activation.Aliasing.pResourceBefore = targets[1 - owner];
          activation.Aliasing.pResourceAfter = targets[owner];
          cmd->ResourceBarrier(1, &activation);
          if(owner == 1)
          {
            ResourceBarrier(cmd, seed, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
            ResourceBarrier(cmd, targets[owner], D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
            cmd->CopyResource(targets[owner], seed);
            ResourceBarrier(cmd, seed, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
            ResourceBarrier(cmd, targets[owner], D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
          }
        }
        ResourceBarrier(cmd, targets[owner], D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_RENDER_TARGET);
        if(region == 0)
          ClearRenderTargetView(cmd, views[owner], {0.1f, 0.25f, 0.05f, 1.0f});
        const float tint[4] = {0.15f + region * 0.18f, 0.6f - region * 0.1f, 0.2f + region * 0.12f, 1.0f};
        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        IASetVertexBuffer(cmd, vb, sizeof(DefaultA2V), 0);
        cmd->SetGraphicsRootSignature(sig);
        cmd->SetPipelineState(pso);
        cmd->SetGraphicsRoot32BitConstants(0, 4, tint, 0);
        const float half = float(screenWidth) / 2.0f;
        RSSetViewport(cmd, {(region % 2) * half, 0.0f, half, float(screenHeight), 0.0f, 1.0f});
        RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
        OMSetRenderTargets(cmd, {views[owner]}, {});
        setMarker(cmd, "Typed alias accumulating draw " + std::to_string(region));
        cmd->DrawInstanced(3, 1, 0, 0);
        ResourceBarrier(cmd, targets[owner], D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COMMON);
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
