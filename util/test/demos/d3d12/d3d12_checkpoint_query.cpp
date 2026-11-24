// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "d3d12_test.h"

RD_TEST(D3D12_Checkpoint_Query, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Four completed submissions preserve binary occlusion query results across a checkpoint. "
      "A mixed two-slot Resolve feeds a pixel shader. RENDERDOC_DEMO_QUERY_REPEAT=1 also submits "
      "one unchanged command list twice with different query-source dependencies.";

  int main()
  {
    if(!Init())
      return 3;
    const char *repeatSetting = getenv("RENDERDOC_DEMO_QUERY_REPEAT");
    const bool repeat = repeatSetting && repeatSetting[0] == '1';

    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_5_0");
    ID3DBlobPtr ps = Compile(
        "float4 main(float4 p:SV_Position,float4 c:COLOR0,float2 uv:TEXCOORD0):SV_Target0"
        "{ return float4(0.15,0.4,0.85,1); }", "main", "ps_5_0");
    ID3DBlobPtr inspectPS = Compile(
        "ByteAddressBuffer queryResults : register(t0);"
        "float4 main(float4 p:SV_Position,float4 c:COLOR0,float2 uv:TEXCOORD0):SV_Target0 {"
        "uint2 a=queryResults.Load2(16), b=queryResults.Load2(24);"
        "if(a.y!=0 || a.x>1 || any(b!=uint2(0,0))) return float4(1,0,1,1);"
        "return a.x==1 ? float4(0.1,0.8,0.2,1) : float4(0.9,0.25,0.05,1); }",
        "main", "ps_5_0");
    ID3D12RootSignaturePtr sig = MakeSig({srvParam(D3D12_SHADER_VISIBILITY_PIXEL, 0, 0)});
    ID3D12PipelineStatePtr pso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(ps);
    ID3D12PipelineStatePtr inspectPso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(inspectPS);
    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);

    // Only bytes [16,32) are resolved. The surrounding sentinels check request bounds as well
    // as the concatenation of one saved prefix slot and one newly produced suffix slot.
    const uint64_t initial[8] = {0x1122334455667788ULL, 0x8877665544332211ULL,
                                0xeeeeeeeeeeeeeeeeULL, 0xddddddddddddddddULL,
                                0x1020304050607080ULL, 0x8070605040302010ULL,
                                0xabcdef0123456789ULL, 0x9876543210fedcbaULL};
    ID3D12ResourcePtr results = MakeBuffer().Data(initial);
    results->SetName(L"QueryCheckpointResults");
    ResourceBarrier(results, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);

    D3D12_QUERY_HEAP_DESC queryDesc = {};
    queryDesc.Type = D3D12_QUERY_HEAP_TYPE_OCCLUSION;
    queryDesc.Count = 2;
    ID3D12QueryHeapPtr queries;
    CHECK_HR(dev->CreateQueryHeap(&queryDesc, __uuidof(ID3D12QueryHeap), (void **)&queries));
    queries->SetName(L"QueryCheckpointOcclusionSlots");
    const D3D12_QUERY_TYPE queryType = D3D12_QUERY_TYPE_BINARY_OCCLUSION;

    while(Running())
    {
      ID3D12ResourcePtr bb;
      D3D12_CPU_DESCRIPTOR_HANDLE rtv = {};
      ID3D12GraphicsCommandListPtr repeated;
      for(uint32_t phase = 0; phase < 4; phase++)
      {
        if(repeat && phase == 3)
        {
          // No Reset or re-record: the second execution must resolve slot 0 from the native
          // End in the first execution, while the first execution needs the checkpoint value.
          Submit({repeated});
          GPUSync();
          continue;
        }
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(phase == 0)
        {
          bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
          bb->SetName(L"QueryCheckpointOutput");
          rtv = MakeRTV(bb).Format(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB).CreateCPU(0);
          ClearRenderTargetView(cmd, rtv, {0.025f, 0.04f, 0.06f, 1.0f});
          setMarker(cmd, repeat ? "Query checkpoint variant repeated-submit"
                                : "Query checkpoint variant mixed-prefix-suffix");
        }
        else if(repeat && phase == 2)
          ResourceBarrier(cmd, bb, D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_RENDER_TARGET);

        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        IASetVertexBuffer(cmd, vb, sizeof(DefaultA2V), 0);
        cmd->SetPipelineState(phase >= 2 ? inspectPso : pso);
        cmd->SetGraphicsRootSignature(sig);
        cmd->SetGraphicsRootShaderResourceView(0, results->GetGPUVirtualAddress());
        const float width = float(screenWidth) / 4.0f;
        RSSetViewport(cmd, {float(phase) * width, 0.0f,
                            repeat && phase == 2 ? width * 2.0f : width,
                            float(screenHeight), 0.0f, 1.0f});
        RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
        OMSetRenderTargets(cmd, {rtv}, {});

        if(phase == 0)
        {
          setMarker(cmd, "Query checkpoint prefix slot 0 = 1");
          cmd->BeginQuery(queries, queryType, 0);
          cmd->DrawInstanced(3, 1, 0, 0);
          cmd->EndQuery(queries, queryType, 0);
        }
        else if(phase == 1)
        {
          if(repeat)
          {
            cmd->BeginQuery(queries, queryType, 1);
            cmd->EndQuery(queries, queryType, 1);
          }
          setMarker(cmd, "Query checkpoint gap draw");
          cmd->DrawInstanced(3, 1, 0, 0);
          if(repeat)
            ResourceBarrier(cmd, bb, D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_PRESENT);
        }
        else if(phase == 2)
        {
          if(!repeat)
          {
            setMarker(cmd, "Query checkpoint suffix slot 1 = 0");
            cmd->BeginQuery(queries, queryType, 1);
            cmd->EndQuery(queries, queryType, 1);
          }
          ResourceBarrier(cmd, results, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                          D3D12_RESOURCE_STATE_COPY_DEST);
          setMarker(cmd, repeat ? "Query checkpoint repeated mixed resolve"
                                : "Query checkpoint mixed resolve expected 1,0");
          cmd->ResolveQueryData(queries, queryType, 0, 2, results, 16);
          ResourceBarrier(cmd, results, D3D12_RESOURCE_STATE_COPY_DEST,
                          D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
          setMarker(cmd, "Query checkpoint inspect resolved buffer");
          cmd->DrawInstanced(3, 1, 0, 0);
          if(repeat)
          {
            setMarker(cmd, "Query checkpoint overwrite native slot 0 = 0");
            cmd->BeginQuery(queries, queryType, 0);
            cmd->EndQuery(queries, queryType, 0);
            FinishUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
            repeated = cmd;
          }
        }
        else
        {
          // Deliberately poison the native slot after its result was resolved. A later replay
          // resumed at the earlier checkpoint must use the saved 1 rather than this native 0.
          setMarker(cmd, "Query checkpoint overwrite native slot 0 = 0");
          cmd->BeginQuery(queries, queryType, 0);
          cmd->EndQuery(queries, queryType, 0);
          setMarker(cmd, "Query checkpoint final draw expected 1,0");
          cmd->DrawInstanced(3, 1, 0, 0);
          FinishUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
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
