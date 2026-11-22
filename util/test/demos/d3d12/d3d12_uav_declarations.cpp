// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d12_test.h"

RD_TEST(D3D12_UAV_Declarations, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Four completed submissions dynamically index a finite SM6.6 UAV array of two resources "
      "inside a four-entry root table. The unused UAV descriptors refer to static SRV assets.";

  std::string vertex = R"EOSHADER(
float4 main(uint vertexId : SV_VertexID) : SV_Position
{
  float2 uv = float2((vertexId << 1) & 2, vertexId & 2);
  return float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
}
)EOSHADER";

  std::string pixel = R"EOSHADER(
cbuffer Region : register(b0)
{
  uint region;
};

// Deliberately smaller than the root signature's u0..u3 range. Keep a real dynamic array
// selection: trimming to one statically observed element would lose half the writes.
RWStructuredBuffer<uint> Written[2] : register(u0);
StructuredBuffer<uint> Assets[2] : register(t0);

float4 main(float4 position : SV_Position) : SV_Target0
{
  uint2 pixel = uint2(position.xy);
  uint element = pixel.y * 256u + pixel.x;
  uint index = NonUniformResourceIndex((pixel.x ^ region) & 1u);
  uint left = Assets[0][element];
  uint right = Assets[1][element];
  uint value = Written[index][element] + (region + 1u) * 65536u +
               ((left & 255u) << 8u) + (right & 255u) + 1u;
  Written[index][element] = value;
  return float4(float(left & 255u), float(right & 255u), float((value >> 4u) & 255u),
                255.0) / 255.0;
}
)EOSHADER";

  void Prepare(int argc, char **argv)
  {
    D3D12GraphicsTest::Prepare(argc, argv);
    if(!Avail.empty())
      return;
    if(!m_DXILSupport)
      Avail = "DXC is required for the SM6.6 finite UAV declaration test";
  }

  int main()
  {
    // Fixed dimensions make complete byte witnesses independent of window-size arguments.
    screenWidth = 256;
    screenHeight = 128;
    if(!Init())
      return 3;

    // Prepare's capability snapshot is shared across demo enumeration and uses a temporary
    // device. Check the actual selected device/runtime before compiling the required model.
    D3D12_FEATURE_DATA_SHADER_MODEL model = {D3D_SHADER_MODEL_6_6};
    const HRESULT modelResult =
        dev->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &model, sizeof(model));
    TEST_LOG("UAV declaration shader model: requested=0x66 actual=0x%x prepared=0x%x result=0x%08x",
             uint32_t(model.HighestShaderModel), uint32_t(m_HighestShaderModel),
             uint32_t(modelResult));
    if(FAILED(modelResult) || model.HighestShaderModel < D3D_SHADER_MODEL_6_6)
    {
      TEST_ERROR("The selected D3D12 device/runtime does not support shader model 6.6");
      return 4;
    }

    ID3DBlobPtr vs = Compile(vertex, "main", "vs_6_6");
    ID3DBlobPtr ps = Compile(pixel, "main", "ps_6_6");
    if(!vs || !ps)
      return 4;

    const uint32_t writtenElements = 256 * 128;
    const uint32_t assetElements = 1024 * 1024;
    const uint32_t writtenBytes = writtenElements * sizeof(uint32_t);
    std::vector<uint32_t> zeros(writtenElements, 0);
    ID3D12ResourcePtr reset = MakeBuffer().Upload().Data(zeros);
    reset->SetName(L"UAVDeclarations_ZeroSource");
    ID3D12ResourcePtr buffers[4];
    buffers[0] = MakeBuffer().UAV().Data(zeros);
    buffers[1] = MakeBuffer().UAV().Data(zeros);
    for(uint32_t index = 2; index < 4; ++index)
    {
      std::vector<uint32_t> data(assetElements);
      for(uint32_t element = 0; element < assetElements; ++element)
        data[element] = index == 2 ? 0x10000000U + 3U * element : 0x20000000U + 5U * element;
      // UAV permission and a bound UAV descriptor do not imply that any captured shader writes
      // this resource. These bytes are only initialized before capture and read through SRVs.
      buffers[index] = MakeBuffer().UAV().Data(data);
    }
    buffers[0]->SetName(L"UAVDeclarations_Written0");
    buffers[1]->SetName(L"UAVDeclarations_Written1");
    buffers[2]->SetName(L"UAVDeclarations_Static2");
    buffers[3]->SetName(L"UAVDeclarations_Static3");

    for(uint32_t index = 0; index < 4; ++index)
      MakeUAV(buffers[index]).StructureStride(sizeof(uint32_t)).CreateGPU(index);
    MakeSRV(buffers[2]).StructureStride(sizeof(uint32_t)).CreateGPU(4);
    MakeSRV(buffers[3]).StructureStride(sizeof(uint32_t)).CreateGPU(5);

    ID3D12RootSignaturePtr sig = MakeSig({
        tableParam(D3D12_SHADER_VISIBILITY_PIXEL, D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 0, 0, 4, 0),
        tableParam(D3D12_SHADER_VISIBILITY_PIXEL, D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 0, 0, 2, 4),
        constParam(D3D12_SHADER_VISIBILITY_PIXEL, 0, 0, 1),
    });
    sig->SetName(L"UAVDeclarations_FourDescriptorRootTable");
    ID3D12PipelineStatePtr pso =
        MakePSO().RootSig(sig).VS(vs).PS(ps).RTVs({DXGI_FORMAT_R8G8B8A8_UNORM});
    pso->SetName(L"UAVDeclarations_SM66FiniteArray");
    ID3D12ResourcePtr output = MakeTexture(DXGI_FORMAT_R8G8B8A8_UNORM, 256, 128)
                                  .RTV()
                                  .InitialState(D3D12_RESOURCE_STATE_RENDER_TARGET);
    output->SetName(L"UAVDeclarations_Output");
    const D3D12_CPU_DESCRIPTOR_HANDLE rtv = MakeRTV(output).CreateCPU(0);

    while(Running())
    {
      for(uint32_t region = 0; region < 4; ++region)
      {
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
        {
          setMarker(cmd, "UAV declarations reset frame");
          ClearRenderTargetView(cmd, rtv, {0.0f, 0.0f, 0.0f, 1.0f});
        }
        for(uint32_t index = 0; index < 4; ++index)
        {
          if(index < 2 && region == 0)
          {
            ResourceBarrier(cmd, buffers[index], D3D12_RESOURCE_STATE_COMMON,
                            D3D12_RESOURCE_STATE_COPY_DEST);
            cmd->CopyBufferRegion(buffers[index], 0, reset, 0, writtenBytes);
            ResourceBarrier(cmd, buffers[index], D3D12_RESOURCE_STATE_COPY_DEST,
                            D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
          }
          else
          {
            ResourceBarrier(cmd, buffers[index], D3D12_RESOURCE_STATE_COMMON,
                            index < 2 ? D3D12_RESOURCE_STATE_UNORDERED_ACCESS
                                      : D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
          }
        }

        ID3D12DescriptorHeap *heaps[] = {m_CBVUAVSRV};
        cmd->SetDescriptorHeaps(1, heaps);
        cmd->SetGraphicsRootSignature(sig);
        cmd->SetPipelineState(pso);
        cmd->SetGraphicsRootDescriptorTable(0, m_CBVUAVSRV->GetGPUDescriptorHandleForHeapStart());
        cmd->SetGraphicsRootDescriptorTable(1, m_CBVUAVSRV->GetGPUDescriptorHandleForHeapStart());
        cmd->SetGraphicsRoot32BitConstant(2, region, 0);
        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        RSSetViewport(cmd, {float(region * 64), 0.0f, 64.0f, 128.0f, 0.0f, 1.0f});
        RSSetScissorRect(cmd, {int(region * 64), 0, int((region + 1) * 64), 128});
        OMSetRenderTargets(cmd, {rtv}, {});
        setMarker(cmd, "UAV declarations region " + std::to_string(region));
        cmd->DrawInstanced(3, 1, 0, 0);

        for(uint32_t index = 0; index < 4; ++index)
        {
          // Explicit COMMON boundaries avoid depending on implicit buffer-state decay between
          // completed submissions. These barriers change state, never the static asset bytes.
          ResourceBarrier(cmd, buffers[index],
                          index < 2 ? D3D12_RESOURCE_STATE_UNORDERED_ACCESS
                                    : D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                          D3D12_RESOURCE_STATE_COMMON);
        }
        if(region == 3)
        {
          ResourceBarrier(cmd, output, D3D12_RESOURCE_STATE_RENDER_TARGET,
                          D3D12_RESOURCE_STATE_COPY_SOURCE);
          ID3D12ResourcePtr bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_COPY_DEST);
          cmd->CopyResource(bb, output);
          FinishUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_COPY_DEST);
          ResourceBarrier(cmd, output, D3D12_RESOURCE_STATE_COPY_SOURCE,
                          D3D12_RESOURCE_STATE_RENDER_TARGET);
        }
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
