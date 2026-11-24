// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d12_test.h"

RD_TEST(D3D12_Sparse_Initial_Residency, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Six completed submissions read one fully mapped reserved texture, then copy new contents "
      "and switch its physical heap. Tests sparse initial residency and mutation invalidation.";

  int main()
  {
    const uint32_t width = 128, height = 128;
    screenWidth = 6 * width;
    screenHeight = height;
    if(!Init())
      return 3;

    D3D12_FEATURE_DATA_D3D12_OPTIONS options = {};
    const HRESULT supported =
        dev->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS, &options, sizeof(options));
    if(FAILED(supported) || options.TiledResourcesTier < D3D12_TILED_RESOURCES_TIER_2)
    {
      TEST_ERROR("Sparse initial residency fixture requires tiled resources tier 2");
      return 4;
    }

    D3D12_RESOURCE_DESC desc = {};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width;
    desc.Height = height;
    desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_64KB_UNDEFINED_SWIZZLE;
    ID3D12ResourcePtr texture;
    CHECK_HR(dev->CreateReservedResource(&desc, D3D12_RESOURCE_STATE_COMMON, NULL,
                                        __uuidof(ID3D12Resource), (void **)&texture));
    texture->SetName(L"SparseInitial_Texture");

    UINT tiles = 0, subresourceCount = 1;
    D3D12_PACKED_MIP_INFO packed = {};
    D3D12_TILE_SHAPE shape = {};
    D3D12_SUBRESOURCE_TILING tiling = {};
    dev->GetResourceTiling(texture, &tiles, &packed, &shape, &subresourceCount, 0, &tiling);
    if(tiles != 1 || packed.NumPackedMips != 0 || subresourceCount != 1)
    {
      TEST_ERROR("Sparse initial residency fixture requires exactly one standard 64 KiB tile");
      return 4;
    }

    D3D12_HEAP_DESC heapDesc = {};
    heapDesc.SizeInBytes = D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT;
    heapDesc.Flags = D3D12_HEAP_FLAG_ALLOW_ONLY_NON_RT_DS_TEXTURES;
    heapDesc.Properties.Type = D3D12_HEAP_TYPE_DEFAULT;
    heapDesc.Properties.CreationNodeMask = heapDesc.Properties.VisibleNodeMask = 1;
    ID3D12HeapPtr heaps[2];
    for(uint32_t heap = 0; heap < 2; ++heap)
      CHECK_HR(dev->CreateHeap(&heapDesc, __uuidof(ID3D12Heap), (void **)&heaps[heap]));
    heaps[0]->SetName(L"SparseInitial_HeapA");
    heaps[1]->SetName(L"SparseInitial_HeapB");
    // No placed resources or second reserved resource use either heap. Every texel is mapped;
    // nothing in this fixture relies on undefined unmapped or freshly remapped memory reads.
    auto mapHeap = [&](uint32_t heap) {
      D3D12_TILED_RESOURCE_COORDINATE start = {};
      D3D12_TILE_REGION_SIZE region = {1, FALSE, 1, 1, 1};
      D3D12_TILE_RANGE_FLAGS rangeFlags = D3D12_TILE_RANGE_FLAG_NONE;
      UINT heapOffset = 0, tileCount = 1;
      queue->UpdateTileMappings(texture, 1, &start, &region, heaps[heap], 1, &rangeFlags,
                                &heapOffset, &tileCount, D3D12_TILE_MAPPING_FLAG_NONE);
    };

    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint = {};
    UINT64 uploadBytes = 0;
    dev->GetCopyableFootprints(&desc, 0, 1, 0, &footprint, NULL, NULL, &uploadBytes);
    if(uploadBytes == 0 || uploadBytes == ~0ULL || uploadBytes > 1024 * 1024)
    {
      TEST_ERROR("Unexpected sparse fixture upload footprint");
      return 4;
    }
    ID3D12ResourcePtr uploads[3];
    for(uint32_t pattern = 0; pattern < 3; ++pattern)
    {
      std::vector<uint8_t> data((size_t)uploadBytes, 0);
      for(uint32_t y = 0; y < height; ++y)
      {
        for(uint32_t x = 0; x < width; ++x)
        {
          const size_t offset = size_t(footprint.Offset) + size_t(y) * footprint.Footprint.RowPitch + x * 4;
          data[offset + 0] = uint8_t(pattern == 1 ? 255 - x : x);
          data[offset + 1] = uint8_t(pattern == 2 ? 255 - y : y);
          data[offset + 2] = uint8_t(pattern == 0 ? 17 : pattern == 1 ? 101 : 203);
          data[offset + 3] = 255;
        }
      }
      uploads[pattern] = MakeBuffer().Upload().Data(data);
    }
    uploads[0]->SetName(L"SparseInitial_UploadA");
    uploads[1]->SetName(L"SparseInitial_UploadB");
    uploads[2]->SetName(L"SparseInitial_UploadC");

    auto copyPattern = [&](ID3D12GraphicsCommandListPtr cmd, uint32_t pattern) {
      ResourceBarrier(cmd, texture, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
      D3D12_TEXTURE_COPY_LOCATION source = {}, destination = {};
      source.pResource = uploads[pattern];
      source.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
      source.PlacedFootprint = footprint;
      destination.pResource = texture;
      destination.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
      destination.SubresourceIndex = 0;
      cmd->CopyTextureRegion(&destination, 0, 0, 0, &source, NULL);
      ResourceBarrier(cmd, texture, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
    };

    ID3DBlobPtr vs = Compile(
        "float4 main(uint id : SV_VertexID) : SV_Position { "
        "float2 uv = float2((id << 1) & 2, id & 2); "
        "return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1); }", "main", "vs_4_0");
    ID3DBlobPtr ps = Compile(
        "Texture2D<float4> source : register(t0); "
        "float4 main(float4 position : SV_Position) : SV_Target0 { "
        "uint2 pixel = uint2(position.xy); pixel.x %= 128; "
        "return source.Load(int3(pixel, 0)); }", "main", "ps_4_0");
    if(!vs || !ps)
      return 4;
    MakeSRV(texture).CreateGPU(0);
    ID3D12RootSignaturePtr signature = MakeSig({
        tableParam(D3D12_SHADER_VISIBILITY_PIXEL, D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 0, 0, 1, 0),
    });
    ID3D12PipelineStatePtr pso =
        MakePSO().RootSig(signature).VS(vs).PS(ps).RTVs({DXGI_FORMAT_R8G8B8A8_UNORM});
    ID3D12ResourcePtr output = MakeTexture(DXGI_FORMAT_R8G8B8A8_UNORM, screenWidth, screenHeight)
                                  .RTV().InitialState(D3D12_RESOURCE_STATE_RENDER_TARGET);
    output->SetName(L"SparseInitial_Output");
    const D3D12_CPU_DESCRIPTOR_HANDLE rtv = MakeRTV(output).CreateCPU(0);

    // The first capture starts with the entire single-mip texture initialized to A, in heap A.
    // Its first captured access is a full SRV read, so capture must preserve full initial Copy
    // contents. Later writes make it mutable over the capture; no production gate is bypassed.
    mapHeap(0);
    ID3D12GraphicsCommandListPtr init = GetCommandBuffer();
    Reset(init);
    copyPattern(init, 0);
    ClearRenderTargetView(init, rtv, {0.0f, 0.0f, 0.0f, 1.0f});
    CHECK_HR(init->Close());
    Submit({init});
    GPUSync();

    while(Running())
    {
      for(uint32_t region = 0; region < 6; ++region)
      {
        // The previous submission is complete before changing the physical mapping. The first
        // operation on the new mapping is a full initialization, never an undefined read.
        if(region == 4)
          mapHeap(1);
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
          ClearRenderTargetView(cmd, rtv, {0.0f, 0.0f, 0.0f, 1.0f});
        if(region == 2 || region == 4)
        {
          setMarker(cmd, region == 2 ? "Sparse initial copy B" : "Sparse initial initialize mapped C");
          copyPattern(cmd, region == 2 ? 1 : 2);
        }
        ResourceBarrier(cmd, texture, D3D12_RESOURCE_STATE_COMMON,
                        D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
        cmd->SetPipelineState(pso);
        cmd->SetGraphicsRootSignature(signature);
        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ID3D12DescriptorHeap *descriptorHeaps[] = {m_CBVUAVSRV};
        cmd->SetDescriptorHeaps(1, descriptorHeaps);
        cmd->SetGraphicsRootDescriptorTable(0, m_CBVUAVSRV->GetGPUDescriptorHandleForHeapStart());
        cmd->OMSetRenderTargets(1, &rtv, TRUE, NULL);
        D3D12_VIEWPORT viewport = {float(region * width), 0.0f, float(width), float(height), 0.0f, 1.0f};
        D3D12_RECT scissor = {LONG(region * width), 0, LONG((region + 1) * width), LONG(height)};
        cmd->RSSetViewports(1, &viewport);
        cmd->RSSetScissorRects(1, &scissor);
        setMarker(cmd, "Sparse initial region " + std::to_string(region));
        cmd->DrawInstanced(3, 1, 0, 0);
        ResourceBarrier(cmd, texture, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                        D3D12_RESOURCE_STATE_COMMON);
        CHECK_HR(cmd->Close());
        Submit({cmd});
        GPUSync();
      }

      // Restore A for the NEXT frame after all six acceptance targets. Doing this at frame start
      // would make every positive target execute a write/remap and defeat the no-change proof.
      mapHeap(0);
      ID3D12GraphicsCommandListPtr finish = GetCommandBuffer();
      Reset(finish);
      setMarker(finish, "Sparse initial reset next frame");
      copyPattern(finish, 0);
      ID3D12ResourcePtr backbuffer = StartUsingBackbuffer(finish, D3D12_RESOURCE_STATE_COPY_DEST);
      ResourceBarrier(finish, output, D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COPY_SOURCE);
      finish->CopyResource(backbuffer, output);
      ResourceBarrier(finish, output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_RENDER_TARGET);
      FinishUsingBackbuffer(finish, D3D12_RESOURCE_STATE_COPY_DEST);
      CHECK_HR(finish->Close());
      Submit({finish});
      GPUSync();
      Present();
    }
    return 0;
  }
};

REGISTER_TEST();
