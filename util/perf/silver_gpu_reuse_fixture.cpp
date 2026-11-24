// Owned offscreen D3D12 capture: three closed direct lists, optional timestamp writes.
#include <d3d12.h>
#include <d3dcompiler.h>
#include <windows.h>
#include <wrl/client.h>
#include <cstdio>
#include "renderdoc/api/app/renderdoc_app.h"
using Microsoft::WRL::ComPtr;
#define HR(call)                                        \
  do                                                    \
  {                                                     \
    HRESULT value = (call);                             \
    if(FAILED(value))                                   \
    {                                                   \
      printf("HRESULT %08lx at %d\n", value, __LINE__); \
      return 2;                                         \
    }                                                   \
  } while(0)

int main(int argc, char **argv)
{
  if(argc < 3 || argc > 8)
    return 1;
  bool timestamps = false, descriptorTable = false, largeDescriptors = false, indirect = false;
  bool splitSubmissions = false, countedIndirect = false, zeroCount = false;
  for(int i = 3; i < argc; i++)
  {
    if(!strcmp(argv[i], "--timestamps"))
      timestamps = true;
    else if(!strcmp(argv[i], "--descriptors"))
      descriptorTable = true;
    else if(!strcmp(argv[i], "--large-descriptors"))
      largeDescriptors = descriptorTable = true;
    else if(!strcmp(argv[i], "--indirect"))
      indirect = true;
    else if(!strcmp(argv[i], "--counted-indirect"))
      countedIndirect = indirect = true;
    else if(!strcmp(argv[i], "--zero-count"))
      zeroCount = true;
    else if(!strcmp(argv[i], "--split-submissions"))
      splitSubmissions = true;
    else
      return 1;
  }
  HMODULE module = LoadLibraryA(argv[1]);
  if(!module)
    return 2;
  auto getAPI = (pRENDERDOC_GetAPI)GetProcAddress(module, "RENDERDOC_GetAPI");
  RENDERDOC_API_1_6_0 *api = NULL;
  if(!getAPI || !getAPI(eRENDERDOC_API_Version_1_6_0, (void **)&api))
    return 3;
  api->SetCaptureFilePathTemplate(argv[2]);
  ComPtr<ID3D12Device> device;
  HR(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)));
  D3D12_COMMAND_QUEUE_DESC queueDesc = {};
  ComPtr<ID3D12CommandQueue> queue;
  HR(device->CreateCommandQueue(&queueDesc, IID_PPV_ARGS(&queue)));
  D3D12_ROOT_PARAMETER parameter = {};
  parameter.ParameterType = D3D12_ROOT_PARAMETER_TYPE_CBV;
  parameter.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
  D3D12_DESCRIPTOR_RANGE cbvRange = {};
  cbvRange.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_CBV;
  cbvRange.NumDescriptors = 1;
  if(descriptorTable)
  {
    parameter.ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    parameter.DescriptorTable.NumDescriptorRanges = 1;
    parameter.DescriptorTable.pDescriptorRanges = &cbvRange;
  }
  D3D12_ROOT_SIGNATURE_DESC rootDesc = {};
  rootDesc.NumParameters = 1;
  rootDesc.pParameters = &parameter;
  ComPtr<ID3DBlob> serialized, error;
  HR(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, &error));
  ComPtr<ID3D12RootSignature> root;
  HR(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                 IID_PPV_ARGS(&root)));
  const char *vsSource =
      "float4 VSMain(uint id:SV_VertexID):SV_Position {float2 "
      "p[3]={float2(-1,-1),float2(-1,3),float2(3,-1)};return float4(p[id],0,1);}";
  const char *psSource =
      "cbuffer Params:register(b0){float4 Tint;} float4 PSMain(float4 "
      "p:SV_Position):SV_Target0{return Tint;}";
  ComPtr<ID3DBlob> vs, ps;
  HR(D3DCompile(vsSource, strlen(vsSource), "owned-fixture-vs.hlsl", NULL, NULL, "VSMain", "vs_5_0",
                0, 0, &vs, &error));
  HR(D3DCompile(psSource, strlen(psSource), "owned-fixture-ps.hlsl", NULL, NULL, "PSMain", "ps_5_0",
                0, 0, &ps, &error));
  D3D12_GRAPHICS_PIPELINE_STATE_DESC pipeline = {};
  pipeline.pRootSignature = root.Get();
  pipeline.VS = {vs->GetBufferPointer(), vs->GetBufferSize()};
  pipeline.PS = {ps->GetBufferPointer(), ps->GetBufferSize()};
  pipeline.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
  pipeline.SampleMask = ~0U;
  pipeline.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
  pipeline.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
  pipeline.RasterizerState.DepthClipEnable = TRUE;
  pipeline.DepthStencilState.DepthEnable = FALSE;
  pipeline.DepthStencilState.StencilEnable = FALSE;
  pipeline.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
  pipeline.NumRenderTargets = 1;
  pipeline.RTVFormats[0] = DXGI_FORMAT_R8G8B8A8_UNORM;
  pipeline.SampleDesc.Count = 1;
  ComPtr<ID3D12PipelineState> pso;
  HR(device->CreateGraphicsPipelineState(&pipeline, IID_PPV_ARGS(&pso)));
  D3D12_HEAP_PROPERTIES heap = {};
  heap.Type = D3D12_HEAP_TYPE_DEFAULT;
  D3D12_RESOURCE_DESC texture = {};
  texture.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
  texture.Width = texture.Height = 128;
  texture.DepthOrArraySize = texture.MipLevels = 1;
  texture.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  texture.SampleDesc.Count = 1;
  texture.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
  ComPtr<ID3D12Resource> rt;
  HR(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &texture,
                                     D3D12_RESOURCE_STATE_RENDER_TARGET, NULL, IID_PPV_ARGS(&rt)));
  D3D12_DESCRIPTOR_HEAP_DESC descriptors = {};
  descriptors.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
  descriptors.NumDescriptors = 1;
  ComPtr<ID3D12DescriptorHeap> rtHeap;
  HR(device->CreateDescriptorHeap(&descriptors, IID_PPV_ARGS(&rtHeap)));
  auto rtv = rtHeap->GetCPUDescriptorHandleForHeapStart();
  device->CreateRenderTargetView(rt.Get(), NULL, rtv);
  heap.Type = D3D12_HEAP_TYPE_UPLOAD;
  D3D12_RESOURCE_DESC buffer = {};
  buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
  buffer.Width = 768;
  buffer.Height = buffer.DepthOrArraySize = buffer.MipLevels = buffer.SampleDesc.Count = 1;
  buffer.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
  ComPtr<ID3D12Resource> constants;
  HR(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
                                     D3D12_RESOURCE_STATE_GENERIC_READ, NULL,
                                     IID_PPV_ARGS(&constants)));
  float *mapped = NULL;
  HR(constants->Map(0, NULL, (void **)&mapped));
  memset(mapped, 0, 768);
  for(int i = 0; i < 3; i++)
  {
    mapped[i * 64 + i] = 1;
    mapped[i * 64 + 3] = 1;
  }
  constants->Unmap(0, NULL);
  ComPtr<ID3D12Resource> indirectArguments;
  ComPtr<ID3D12CommandSignature> indirectSignature;
  if(indirect)
  {
    buffer.Width = 512;
    HR(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
                                       D3D12_RESOURCE_STATE_GENERIC_READ, NULL,
                                       IID_PPV_ARGS(&indirectArguments)));
    D3D12_DRAW_ARGUMENTS *arguments = NULL;
    HR(indirectArguments->Map(0, NULL, (void **)&arguments));
    memset(arguments, 0, 512);
    for(uint32_t i = 0; i < 16; ++i)
      arguments[i] = {3, 1, 0, 0};
    *(uint32_t *)((byte *)arguments + 256) = zeroCount ? 0 : 1;
    indirectArguments->Unmap(0, NULL);
    D3D12_INDIRECT_ARGUMENT_DESC argument = {};
    argument.Type = D3D12_INDIRECT_ARGUMENT_TYPE_DRAW;
    D3D12_COMMAND_SIGNATURE_DESC signature = {};
    signature.ByteStride = sizeof(D3D12_DRAW_ARGUMENTS);
    signature.NumArgumentDescs = 1;
    signature.pArgumentDescs = &argument;
    HR(device->CreateCommandSignature(&signature, NULL, IID_PPV_ARGS(&indirectSignature)));
  }
  ComPtr<ID3D12DescriptorHeap> cpuDescriptors, gpuDescriptors;
  UINT descriptorIncrement =
      device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
  if(descriptorTable)
  {
    D3D12_DESCRIPTOR_HEAP_DESC heapDesc = {};
    heapDesc.NumDescriptors = largeDescriptors ? 1000000 : 3;
    HR(device->CreateDescriptorHeap(&heapDesc, IID_PPV_ARGS(&cpuDescriptors)));
    heapDesc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    HR(device->CreateDescriptorHeap(&heapDesc, IID_PPV_ARGS(&gpuDescriptors)));
    for(int i = 0; i < 3; i++)
    {
      D3D12_CONSTANT_BUFFER_VIEW_DESC cbv = {constants->GetGPUVirtualAddress() + i * 256, 256};
      auto cpu = cpuDescriptors->GetCPUDescriptorHandleForHeapStart();
      cpu.ptr += i * descriptorIncrement;
      device->CreateConstantBufferView(&cbv, cpu);
    }
    if(largeDescriptors)
    {
      // Immutable dispersed views exercise inspector page eviction independently
      // of the three mutable descriptors used by the actual RGB draws.
      for(UINT page = 1; page <= 192; page++)
      {
        D3D12_SHADER_RESOURCE_VIEW_DESC srv = {};
        srv.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
        srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        srv.Buffer.NumElements = 48;
        srv.Buffer.StructureByteStride = 16;
        ID3D12DescriptorHeap *targets[] = {cpuDescriptors.Get(), gpuDescriptors.Get()};
        for(ID3D12DescriptorHeap *target : targets)
        {
          auto cpu = target->GetCPUDescriptorHandleForHeapStart();
          cpu.ptr += SIZE_T(page * 256) * descriptorIncrement;
          device->CreateShaderResourceView(constants.Get(), &srv, cpu);
        }
      }
    }
  }
  const int listCount = timestamps ? 4 : 3;
  ComPtr<ID3D12CommandAllocator> allocators[4];
  ComPtr<ID3D12GraphicsCommandList> lists[4];
  for(int i = 0; i < listCount; i++)
  {
    HR(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocators[i])));
    HR(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocators[i].Get(), pso.Get(),
                                 IID_PPV_ARGS(&lists[i])));
    HR(lists[i]->Close());
  }
  ComPtr<ID3D12Fence> fence;
  ComPtr<ID3D12QueryHeap> queryHeap;
  ComPtr<ID3D12Resource> queryResults;
  if(timestamps)
  {
    D3D12_QUERY_HEAP_DESC queries = {};
    queries.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    queries.Count = 6;
    HR(device->CreateQueryHeap(&queries, IID_PPV_ARGS(&queryHeap)));
    heap.Type = D3D12_HEAP_TYPE_READBACK;
    buffer.Width = sizeof(UINT64) * 6;
    HR(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
                                       D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                                       IID_PPV_ARGS(&queryResults)));
    queryResults->SetName(L"fixture-query-readback");
  }
  HR(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)));
  HANDLE completion = CreateEvent(NULL, FALSE, FALSE, NULL);
  api->StartFrameCapture(device.Get(), NULL);
  ID3D12CommandList *submit[4];
  for(int i = 0; i < 3; i++)
  {
    if(descriptorTable)
    {
      auto dst = gpuDescriptors->GetCPUDescriptorHandleForHeapStart();
      auto src = cpuDescriptors->GetCPUDescriptorHandleForHeapStart();
      dst.ptr += i * descriptorIncrement;
      src.ptr += i * descriptorIncrement;
      device->CopyDescriptorsSimple(1, dst, src, D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    }
    HR(allocators[i]->Reset());
    HR(lists[i]->Reset(allocators[i].Get(), pso.Get()));
    if(timestamps)
      lists[i]->EndQuery(queryHeap.Get(), D3D12_QUERY_TYPE_TIMESTAMP, i * 2);
    lists[i]->SetGraphicsRootSignature(root.Get());
    if(descriptorTable)
    {
      ID3D12DescriptorHeap *heaps[] = {gpuDescriptors.Get()};
      lists[i]->SetDescriptorHeaps(1, heaps);
      auto gpu = gpuDescriptors->GetGPUDescriptorHandleForHeapStart();
      gpu.ptr += i * descriptorIncrement;
      lists[i]->SetGraphicsRootDescriptorTable(0, gpu);
    }
    else
      lists[i]->SetGraphicsRootConstantBufferView(0, constants->GetGPUVirtualAddress() + i * 256);
    D3D12_VIEWPORT viewport = {0, 0, 128, 128, 0, 1};
    D3D12_RECT scissor = {i * 128 / 3, 0, (i + 1) * 128 / 3, 128};
    lists[i]->RSSetViewports(1, &viewport);
    lists[i]->RSSetScissorRects(1, &scissor);
    lists[i]->OMSetRenderTargets(1, &rtv, FALSE, NULL);
    lists[i]->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    if(indirect && i < 2)
      lists[i]->ExecuteIndirect(indirectSignature.Get(), countedIndirect ? 16 : 1,
                                indirectArguments.Get(), 0,
                                countedIndirect ? indirectArguments.Get() : NULL, 256);
    else
      lists[i]->DrawInstanced(3, 1, 0, 0);
    if(timestamps)
      lists[i]->EndQuery(queryHeap.Get(), D3D12_QUERY_TYPE_TIMESTAMP, i * 2 + 1);
    HR(lists[i]->Close());
    submit[i] = lists[i].Get();
  }
  if(timestamps)
  {
    HR(allocators[3]->Reset());
    HR(lists[3]->Reset(allocators[3].Get(), NULL));
    lists[3]->ResolveQueryData(queryHeap.Get(), D3D12_QUERY_TYPE_TIMESTAMP, 0, 6,
                               queryResults.Get(), 0);
    HR(lists[3]->Close());
    submit[3] = lists[3].Get();
  }
  if(splitSubmissions)
    for(int i = 0; i < listCount; i++)
      queue->ExecuteCommandLists(1, &submit[i]);
  else
    queue->ExecuteCommandLists(listCount, submit);
  HR(queue->Signal(fence.Get(), 1));
  HR(fence->SetEventOnCompletion(1, completion));
  if(WaitForSingleObject(completion, 30000) != WAIT_OBJECT_0)
    return 4;
  CloseHandle(completion);
  if(!api->EndFrameCapture(device.Get(), NULL))
    return 5;
  uint32_t length = 4096;
  char capture[4096] = {};
  if(!api->GetCapture(0, capture, &length, NULL))
    return 6;
  printf("capture=%s\n", capture);
  return 0;
}
