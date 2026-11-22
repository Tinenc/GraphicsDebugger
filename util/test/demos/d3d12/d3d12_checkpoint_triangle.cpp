/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 RenderDoc contributors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#include "d3d12_test.h"

RD_TEST(D3D12_Checkpoint_Triangle, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Four completed submissions draw persistent, disjoint triangle regions with a shared "
      "pixel shader. Exercises native replay checkpoints and shader replacement invalidation.";

  int AliasFixture(const std::string &mode)
  {
    const bool readBeforeClear = mode == "read";
    const bool mixedSplit = mode == "split";
    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_4_0");
    ID3DBlobPtr ps = Compile(
        "float4 main(float4 pos : SV_Position, float4 col : COLOR0, float2 uv : TEXCOORD0) "
        ": SV_Target0 { return float4(0.1 + 0.7 * uv.x, 0.15 + 0.6 * uv.y, 0.9, 1.0); }",
        "main", "ps_4_0");
    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    ID3D12RootSignaturePtr sig = MakeSig({});
    ID3D12PipelineStatePtr pso = MakePSO()
                                   .RootSig(sig)
                                   .InputLayout()
                                   .VS(vs)
                                   .PS(ps)
                                   .RTVs({DXGI_FORMAT_R8G8B8A8_UNORM});
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);

    ID3D12ResourcePtr target = MakeTexture(DXGI_FORMAT_R8G8B8A8_UNORM, screenWidth, screenHeight)
                                  .RTV()
                                  .InitialState(D3D12_RESOURCE_STATE_RENDER_TARGET);
    ID3D12ResourcePtr witness = MakeTexture(DXGI_FORMAT_R8G8B8A8_UNORM, screenWidth, screenHeight)
                                   .RTV()
                                   .InitialState(D3D12_RESOURCE_STATE_RENDER_TARGET);
    target->SetName(L"CheckpointAliasTarget");
    witness->SetName(L"CheckpointAliasWitness");
    const D3D12_CPU_DESCRIPTOR_HANDLE targetRTV = MakeRTV(target).CreateCPU(0);
    const D3D12_CPU_DESCRIPTOR_HANDLE witnessRTV = MakeRTV(witness).CreateCPU(1);
    MakeSRV(target).CreateGPU(0);

    ID3DBlobPtr witnessVS = Compile(
        "float4 main(uint id : SV_VertexID) : SV_Position { "
        "float2 uv = float2((id << 1) & 2, id & 2); "
        "return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1); }",
        "main", "vs_4_0");
    ID3DBlobPtr witnessPS = Compile(
        "Texture2D<float4> source : register(t0); "
        "float4 main(float4 pos : SV_Position) : SV_Target0 { "
        "return source.Load(int3(uint2(pos.xy), 0)); }",
        "main", "ps_4_0");
    ID3D12RootSignaturePtr witnessSig = MakeSig({
        tableParam(D3D12_SHADER_VISIBILITY_PIXEL, D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 0, 0, 1, 0),
    });
    ID3D12PipelineStatePtr witnessPSO = MakePSO()
                                          .RootSig(witnessSig)
                                          .VS(witnessVS)
                                          .PS(witnessPS)
                                          .RTVs({DXGI_FORMAT_R8G8B8A8_UNORM});

    D3D12_HEAP_DESC heapDesc = {};
    heapDesc.SizeInBytes = D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT;
    heapDesc.Flags = D3D12_HEAP_FLAG_ALLOW_ONLY_BUFFERS;
    heapDesc.Properties.Type = D3D12_HEAP_TYPE_DEFAULT;
    heapDesc.Properties.CreationNodeMask = heapDesc.Properties.VisibleNodeMask = 1;
    ID3D12HeapPtr aliasHeap;
    CHECK_HR(dev->CreateHeap(&heapDesc, __uuidof(ID3D12Heap), (void **)&aliasHeap));
    D3D12_RESOURCE_DESC aliasDesc = {};
    aliasDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    aliasDesc.Width = heapDesc.SizeInBytes;
    aliasDesc.Height = aliasDesc.DepthOrArraySize = aliasDesc.MipLevels = 1;
    aliasDesc.SampleDesc.Count = 1;
    aliasDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12ResourcePtr aliases[2];
    for(uint32_t i = 0; i < 2; ++i)
      CHECK_HR(dev->CreatePlacedResource(aliasHeap, 0, &aliasDesc, D3D12_RESOURCE_STATE_COMMON,
                                        NULL, __uuidof(ID3D12Resource), (void **)&aliases[i]));
    aliases[0]->SetName(L"CheckpointAliasUnusedX");
    aliases[1]->SetName(L"CheckpointAliasUnusedY");
    // Neither alias is read or written. Their bytes are deliberately outside the pixel witness;
    // this fixture proves only that an unrelated alias barrier cannot invalidate committed T.
    ID3D12ResourcePtr splitBuffer = MakeBuffer().Size(65536);
    splitBuffer->SetName(L"CheckpointAliasSplitBuffer");

    ID3D12GraphicsCommandListPtr init = GetCommandBuffer();
    Reset(init);
    ClearRenderTargetView(init, targetRTV, {0.025f, 0.04f, 0.06f, 1.0f});
    ClearRenderTargetView(init, witnessRTV, {0.0f, 0.0f, 0.0f, 1.0f});
    CHECK_HR(init->Close());
    Submit({init});
    GPUSync();

    while(Running())
    {
      for(uint32_t region = 0; region < 4; ++region)
      {
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
        {
          setMarker(cmd, "Checkpoint alias fixture " + mode);
          ClearRenderTargetView(cmd, targetRTV, {0.025f, 0.04f, 0.06f, 1.0f});
          ClearRenderTargetView(cmd, witnessRTV, {0.0f, 0.0f, 0.0f, 1.0f});
        }
        if(region >= 2)
        {
          // The first two completed submissions precede every alias/split operation and leave
          // a legal checkpoint containing real, nonuniform T pixels. Later alias prefixes may
          // be rejected by checkpoint creation without preventing restoration of that point.
          setMarker(cmd, "Checkpoint alias barrier region " + std::to_string(region));
          D3D12_RESOURCE_BARRIER barriers[2] = {};
          barriers[0].Type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
          barriers[0].Aliasing.pResourceBefore = aliases[region == 2 ? 0 : 1];
          barriers[0].Aliasing.pResourceAfter = aliases[region == 2 ? 1 : 0];
          if(mixedSplit)
          {
            barriers[1].Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
            barriers[1].Flags = D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY;
            barriers[1].Transition.pResource = splitBuffer;
            barriers[1].Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
            barriers[1].Transition.StateBefore = D3D12_RESOURCE_STATE_COMMON;
            barriers[1].Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
          }
          cmd->ResourceBarrier(mixedSplit ? 2 : 1, barriers);
          if(mixedSplit)
          {
            // A legal matching END follows, but the mixed EID must still veto the clear proof.
            barriers[1].Flags = D3D12_RESOURCE_BARRIER_FLAG_END_ONLY;
            cmd->ResourceBarrier(1, &barriers[1]);
            ResourceBarrier(cmd, splitBuffer, D3D12_RESOURCE_STATE_COPY_DEST,
                            D3D12_RESOURCE_STATE_COMMON);
          }
          if(readBeforeClear)
          {
            ResourceBarrier(cmd, target, D3D12_RESOURCE_STATE_RENDER_TARGET,
                            D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
            ID3D12DescriptorHeap *heaps[] = {m_CBVUAVSRV};
            cmd->SetDescriptorHeaps(1, heaps);
            cmd->SetGraphicsRootSignature(witnessSig);
            cmd->SetGraphicsRootDescriptorTable(0, m_CBVUAVSRV->GetGPUDescriptorHandleForHeapStart());
            cmd->SetPipelineState(witnessPSO);
            cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            RSSetViewport(cmd, {0.0f, 0.0f, float(screenWidth), float(screenHeight), 0.0f, 1.0f});
            RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
            OMSetRenderTargets(cmd, {witnessRTV}, {});
            setMarker(cmd, "Checkpoint alias read-before-clear region " + std::to_string(region));
            cmd->DrawInstanced(3, 1, 0, 0);
            ResourceBarrier(cmd, target, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                            D3D12_RESOURCE_STATE_RENDER_TARGET);
          }
          setMarker(cmd, "Checkpoint alias full-clear region " + std::to_string(region));
          ClearRenderTargetView(cmd, targetRTV, {0.05f * region, 0.04f, 0.08f * region, 1.0f});
        }

        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        IASetVertexBuffer(cmd, vb, sizeof(DefaultA2V), 0);
        cmd->SetGraphicsRootSignature(sig);
        cmd->SetPipelineState(pso);
        const float width = float(screenWidth) / 4.0f;
        RSSetViewport(cmd, {float(region) * width, 0.0f, width, float(screenHeight), 0.0f, 1.0f});
        RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
        OMSetRenderTargets(cmd, {targetRTV}, {});
        setMarker(cmd, "Checkpoint region " + std::to_string(region));
        cmd->DrawInstanced(3, 1, 0, 0);

        if(region == 3)
        {
          ResourceBarrier(cmd, target, D3D12_RESOURCE_STATE_RENDER_TARGET,
                          D3D12_RESOURCE_STATE_COPY_SOURCE);
          ID3D12ResourcePtr bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_COPY_DEST);
          cmd->CopyResource(bb, target);
          FinishUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_COPY_DEST);
          ResourceBarrier(cmd, target, D3D12_RESOURCE_STATE_COPY_SOURCE,
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

  int main()
  {
    const bool clearFixture = GetEnvVar("RENDERDOC_CHECKPOINT_CLEAR_FIXTURE") == "1";
    const std::string aliasFixture = GetEnvVar("RENDERDOC_CHECKPOINT_ALIAS_FIXTURE");
    if(!aliasFixture.empty() && aliasFixture != "off" && aliasFixture != "clear" &&
       aliasFixture != "read" && aliasFixture != "split")
    {
      TEST_ERROR("Unknown checkpoint alias fixture mode");
      return 4;
    }
    const bool aliasEnabled = !aliasFixture.empty() && aliasFixture != "off";
    if(aliasEnabled && clearFixture)
    {
      TEST_ERROR("Checkpoint clear and alias fixture modes are mutually exclusive");
      return 4;
    }
    if(aliasEnabled)
    {
      screenWidth = 256;
      screenHeight = 128;
    }
    if(!Init())
      return 3;
    if(aliasEnabled)
      return AliasFixture(aliasFixture);

    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_4_0");
    // A constant replacement must change every triangle, including those in saved prefixes.
    ID3DBlobPtr ps = Compile(
        "float4 main(float4 pos : SV_Position, float4 col : COLOR0, float2 uv : TEXCOORD0) "
        ": SV_Target0 { return float4(0.1 + 0.7 * uv.x, 0.15 + 0.6 * uv.y, 0.9, 1.0); }",
        "main", "ps_4_0");
    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    ID3D12RootSignaturePtr sig = MakeSig({});
    ID3D12PipelineStatePtr pso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(ps);
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);

    ID3D12QueryHeapPtr clearQueries;
    if(clearFixture)
    {
      D3D12_QUERY_HEAP_DESC queryDesc = {};
      queryDesc.Type = D3D12_QUERY_HEAP_TYPE_OCCLUSION;
      queryDesc.Count = 1;
      CHECK_HR(dev->CreateQueryHeap(&queryDesc, __uuidof(ID3D12QueryHeap), (void **)&clearQueries));
    }

    while(Running())
    {
      ID3D12ResourcePtr bb;
      D3D12_CPU_DESCRIPTOR_HANDLE rtv = {};
      // Four draws also leave action-planner candidates after two completed submissions:
      // a candidate at a draw precedes that draw's own closing queue boundary.
      for(uint32_t region = 0; region < 4; region++)
      {
        // Each following recording starts after its predecessor completes; there is no
        // open recording, query, bundle, alias, or split barrier across a checkpoint boundary.
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
        {
          bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
          bb->SetName(L"CheckpointTriangleOutput");
          rtv = MakeRTV(bb).Format(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB).CreateCPU(0);
          ClearRenderTargetView(cmd, rtv, {0.025f, 0.04f, 0.06f, 1.0f});
        }
        else if(clearFixture)
        {
          // A complete overwrite after a closed submission makes the preceding color contents
          // unnecessary. Different values in each region expose stale or omitted clear commands.
          setMarker(cmd, "Checkpoint complete-clear fixture");
          cmd->BeginQuery(clearQueries, D3D12_QUERY_TYPE_BINARY_OCCLUSION, 0);
          cmd->EndQuery(clearQueries, D3D12_QUERY_TYPE_BINARY_OCCLUSION, 0);
          ClearRenderTargetView(cmd, rtv, {0.05f * region, 0.04f, 0.08f * region, 1.0f});
        }

        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        IASetVertexBuffer(cmd, vb, sizeof(DefaultA2V), 0);
        cmd->SetPipelineState(pso);
        cmd->SetGraphicsRootSignature(sig);
        const float width = float(screenWidth) / 4.0f;
        RSSetViewport(cmd, {float(region) * width, 0.0f, width, float(screenHeight), 0.0f, 1.0f});
        RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
        OMSetRenderTargets(cmd, {rtv}, {});
        setMarker(cmd, std::string("Checkpoint region ") + std::to_string(region));
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
