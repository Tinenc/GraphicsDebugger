// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d12_test.h"

RD_TEST(D3D12_Checkpoint_Buffer_Alias, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Four submissions modify one physical float4 through two partially overlapping DEFAULT "
      "buffers. A second alias component stays unchanged, testing checkpoint union copies and sharing.";

  int main()
  {
    if(!Init())
      return 3;
    const UINT64 unit = D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT;
    D3D12_HEAP_DESC heapDesc = {};
    heapDesc.SizeInBytes = 6 * unit;
    heapDesc.Flags = D3D12_HEAP_FLAG_ALLOW_ONLY_BUFFERS;
    heapDesc.Properties.Type = D3D12_HEAP_TYPE_DEFAULT;
    heapDesc.Properties.CreationNodeMask = heapDesc.Properties.VisibleNodeMask = 1;
    ID3D12HeapPtr heap;
    CHECK_HR(dev->CreateHeap(&heapDesc, __uuidof(ID3D12Heap), (void **)&heap));
    heap->SetName(L"CheckpointPhysicalBufferHeap");
    D3D12_RESOURCE_DESC desc = {};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Height = desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12ResourcePtr aliases[4];
    for(UINT i = 0; i < 4; ++i)
    {
      desc.Width = (i < 2 ? 2 : 1) * unit;
      CHECK_HR(dev->CreatePlacedResource(heap, (i < 2 ? i : 4) * unit, &desc,
          D3D12_RESOURCE_STATE_COMMON, NULL, __uuidof(ID3D12Resource), (void **)&aliases[i]));
      aliases[i]->SetName((L"CheckpointBufferAlias" + std::to_wstring(i)).c_str());
    }
    ID3D12ResourcePtr source = MakeBuffer().Upload().Size(3 * unit);
    void *mapped = NULL;
    D3D12_RANGE noRead = {0, 0};
    CHECK_HR(source->Map(0, &noRead, &mapped));
    memset(mapped, 0, size_t(3 * unit));
    const float original[4] = {0.2f, 0.45f, 0.7f, 1.0f};
    memcpy((byte *)mapped + unit + 256, original, sizeof(original));
    const float values[4] = {0.15f, 0.35f, 0.6f, 0.85f};
    memcpy(mapped, values, sizeof(values));
    const float stable[4] = {0.03f, 0.02f, 0.01f, 0.0f};
    memcpy((byte *)mapped + 512, stable, sizeof(stable));
    source->Unmap(0, NULL);
    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);
    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_4_0");
    ID3DBlobPtr ps = Compile(
        "cbuffer Dynamic : register(b0) { float4 tint; };"
        "cbuffer Stable : register(b1) { float4 bias; };"
        "float4 main() : SV_Target0 { return tint + bias; }", "main", "ps_4_0");
    ID3D12RootSignaturePtr sig = MakeSig({cbvParam(D3D12_SHADER_VISIBILITY_PIXEL, 0, 0),
                                        cbvParam(D3D12_SHADER_VISIBILITY_PIXEL, 0, 1)});
    ID3D12PipelineStatePtr pso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(ps);
    while(Running())
    {
      ID3D12ResourcePtr bb;
      D3D12_CPU_DESCRIPTOR_HANDLE rtv = {};
      for(UINT region = 0; region < 4; ++region)
      {
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
        {
          bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
          rtv = MakeRTV(bb).Format(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB).CreateCPU(0);
          ClearRenderTargetView(cmd, rtv, {0.03f, 0.04f, 0.05f, 1.0f});
          for(UINT i = 0; i < 3; ++i)
          {
            D3D12_RESOURCE_BARRIER alias = {};
            alias.Type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
            alias.Aliasing.pResourceAfter = aliases[i];
            cmd->ResourceBarrier(1, &alias);
            ResourceBarrier(cmd, aliases[i], D3D12_RESOURCE_STATE_COMMON,
                            D3D12_RESOURCE_STATE_COPY_DEST);
            cmd->CopyBufferRegion(aliases[i], 0, source, (i == 1 ? unit : 0),
                                  (i < 2 ? 2 : 1) * unit);
            ResourceBarrier(cmd, aliases[i], D3D12_RESOURCE_STATE_COPY_DEST,
                            D3D12_RESOURCE_STATE_COMMON);
          }
          // Read the stable component through the other view in every later draw. Its bytes
          // must persist through checkpoint capture, restores, and the dynamic alias changes.
          D3D12_RESOURCE_BARRIER alias = {};
          alias.Type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
          alias.Aliasing.pResourceBefore = aliases[2];
          alias.Aliasing.pResourceAfter = aliases[3];
          cmd->ResourceBarrier(1, &alias);
        }
        ID3D12Resource *current = aliases[region % 2];
        D3D12_RESOURCE_BARRIER alias = {};
        alias.Type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
        alias.Aliasing.pResourceBefore = aliases[1 - region % 2];
        alias.Aliasing.pResourceAfter = current;
        cmd->ResourceBarrier(1, &alias);
        ResourceBarrier(cmd, current, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
        const UINT64 offset = (region % 2 == 0 ? unit : 0) + 256;
        // Change one channel per submission. Other channels retain earlier writes through the
        // other alias, so a checkpoint that substitutes frame-start bytes cannot pass later draws.
        cmd->CopyBufferRegion(current, offset + (region % 3) * sizeof(float), source,
                              region * sizeof(float), sizeof(float));
        ResourceBarrier(cmd, current, D3D12_RESOURCE_STATE_COPY_DEST,
                        D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);
        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        IASetVertexBuffer(cmd, vb, sizeof(DefaultA2V), 0);
        cmd->SetPipelineState(pso);
        cmd->SetGraphicsRootSignature(sig);
        cmd->SetGraphicsRootConstantBufferView(0, current->GetGPUVirtualAddress() + offset);
        cmd->SetGraphicsRootConstantBufferView(1, aliases[3]->GetGPUVirtualAddress() + 512);
        const float width = float(screenWidth) / 4.0f;
        RSSetViewport(cmd, {region * width, 0.0f, width, float(screenHeight), 0.0f, 1.0f});
        RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
        OMSetRenderTargets(cmd, {rtv}, {});
        setMarker(cmd, "Physical buffer alias region " + std::to_string(region));
        cmd->DrawInstanced(3, 1, 0, 0);
        ResourceBarrier(cmd, current, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER,
                        D3D12_RESOURCE_STATE_COMMON);
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
