/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2015-2026 Baldur Karlsson
 * Copyright (c) 2014 Crytek
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

#include "d3d11_replay.h"
#include <set>
#include "common/timing.h"
#include "core/settings.h"
#include "driver/dx/official/d3dcompiler.h"
#include "driver/ihv/amd/amd_counters.h"
#include "driver/ihv/intel/intel_counters.h"
#include "driver/ihv/nv/nv_counters.h"
#include "driver/ihv/nv/nv_d3d11_counters.h"
#include "driver/shaders/dxbc/dxbc_common.h"
#include "maths/camera.h"
#include "maths/formatpacking.h"
#include "maths/matrix.h"
#include "replay/dummy_driver.h"
#include "replay/replay_timing.h"
#include "serialise/rdcfile.h"
#include "strings/string_utils.h"
#include "d3d11_context.h"
#include "d3d11_debug.h"
#include "d3d11_device.h"
#include "d3d11_hooks.h"
#include "d3d11_renderstate.h"
#include "d3d11_resources.h"
#include "d3d11_shader_cache.h"

#include "data/hlsl/hlsl_cbuffers.h"

RDOC_CONFIG(bool, D3D11_HardwareCounters, true,
            "Enable support for IHV-specific hardware counters on D3D11.");

static const char *DXBCDisassemblyTarget = "DXBC";

struct D3D11Replay::CheckpointResource
{
  ResourceId id;
  ID3D11Resource *live = NULL;    // Borrowed from the capture resource manager.
  D3D11_RESOURCE_DIMENSION dimension = D3D11_RESOURCE_DIMENSION_UNKNOWN;
  D3D11_BUFFER_DESC buffer = {};
  D3D11_TEXTURE1D_DESC tex1 = {};
  D3D11_TEXTURE2D_DESC tex2 = {};
  D3D11_TEXTURE3D_DESC tex3 = {};
  uint64_t bytes = 0;
  bool mutableContents = false;
  bool minLOD = false;
  rdcarray<uint32_t> writes;
};

struct D3D11Replay::CheckpointVersion
{
  ResourceId id;
  ID3D11Resource *live = NULL;
  ID3D11Resource *copy = NULL;
  uint64_t bytes = 0;
  uint32_t lastWrite = 0;
  ~CheckpointVersion()
  {
    SAFE_RELEASE(copy);
    SAFE_RELEASE(live);
  }
};

struct D3D11Replay::ReplayCheckpoint
{
  ReplayCheckpoint(const D3D11RenderState &pipeline) : state(pipeline) {}
  struct Resource
  {
    std::shared_ptr<CheckpointVersion> version;
    ResourceId id;
    ID3D11Resource *live = NULL;
    float minLOD = 0.0f;
    bool restoreMinLOD = false;
  };
  struct Counter
  {
    ResourceId id;
    ID3D11UnorderedAccessView *view = NULL;
    uint32_t value = 0;
  };
  ~ReplayCheckpoint()
  {
    for(Counter &counter : counters)
      SAFE_RELEASE(counter.view);
  }
  uint32_t eventId = 0;
  uint64_t lastUsed = 0;
  D3D11RenderState state;
  rdcarray<Resource> resources;
  rdcarray<Counter> counters;
};

namespace
{
struct D3D11CheckpointMetadataBudget
{
  explicit D3D11CheckpointMetadataBudget(uint64_t budget)
      : limit(RDCMIN<uint64_t>(budget, 64ULL * 1024 * 1024))
  {
  }
  uint64_t bytes = 0;
  const uint64_t limit;
  bool Add(uint64_t count, uint64_t stride)
  {
    if(bytes > limit || (stride && count > (limit - bytes) / stride))
    {
      bytes = ~0ULL;
      return false;
    }
    bytes += count * stride;
    return true;
  }
};

bool D3D11CheckpointAddBytes(uint64_t &bytes, uint64_t amount, uint64_t limit)
{
  if(bytes > limit || amount > limit - bytes)
    return false;
  bytes += amount;
  return true;
}

bool CheckpointWritesResource(ResourceUsage usage)
{
  return (usage >= ResourceUsage::VS_RWResource && usage <= ResourceUsage::All_RWResource) ||
         usage == ResourceUsage::StreamOut || usage == ResourceUsage::ColorTarget ||
         usage == ResourceUsage::DepthStencilTarget || usage == ResourceUsage::Clear ||
         usage == ResourceUsage::Discard || usage == ResourceUsage::GenMips ||
         usage == ResourceUsage::Resolve || usage == ResourceUsage::ResolveDst ||
         usage == ResourceUsage::Copy || usage == ResourceUsage::CopyDst ||
         usage == ResourceUsage::CPUWrite;
}

uint32_t CheckpointLastWrite(const rdcarray<uint32_t> &writes, uint32_t eventId)
{
  uint32_t last = 0;
  for(uint32_t write : writes)
    if(write <= eventId)
      last = RDCMAX(last, write);
  return last;
}

bool D3D11LiveForwardAction(D3D11Chunk chunk)
{
  return chunk == D3D11Chunk::Draw || chunk == D3D11Chunk::DrawIndexed ||
         chunk == D3D11Chunk::DrawInstanced || chunk == D3D11Chunk::DrawIndexedInstanced ||
         chunk == D3D11Chunk::Dispatch;
}

bool D3D11LiveForwardChunk(D3D11Chunk chunk)
{
  switch(chunk)
  {
    case D3D11Chunk::IASetInputLayout:
    case D3D11Chunk::IASetVertexBuffers:
    case D3D11Chunk::IASetIndexBuffer:
    case D3D11Chunk::IASetPrimitiveTopology:
    case D3D11Chunk::VSSetConstantBuffers:
    case D3D11Chunk::VSSetConstantBuffers1:
    case D3D11Chunk::VSSetShaderResources:
    case D3D11Chunk::VSSetSamplers:
    case D3D11Chunk::VSSetShader:
    case D3D11Chunk::HSSetConstantBuffers:
    case D3D11Chunk::HSSetConstantBuffers1:
    case D3D11Chunk::HSSetShaderResources:
    case D3D11Chunk::HSSetSamplers:
    case D3D11Chunk::HSSetShader:
    case D3D11Chunk::DSSetConstantBuffers:
    case D3D11Chunk::DSSetConstantBuffers1:
    case D3D11Chunk::DSSetShaderResources:
    case D3D11Chunk::DSSetSamplers:
    case D3D11Chunk::DSSetShader:
    case D3D11Chunk::GSSetConstantBuffers:
    case D3D11Chunk::GSSetConstantBuffers1:
    case D3D11Chunk::GSSetShaderResources:
    case D3D11Chunk::GSSetSamplers:
    case D3D11Chunk::GSSetShader:
    case D3D11Chunk::PSSetConstantBuffers:
    case D3D11Chunk::PSSetConstantBuffers1:
    case D3D11Chunk::PSSetShaderResources:
    case D3D11Chunk::PSSetSamplers:
    case D3D11Chunk::PSSetShader:
    case D3D11Chunk::CSSetConstantBuffers:
    case D3D11Chunk::CSSetConstantBuffers1:
    case D3D11Chunk::CSSetShaderResources:
    case D3D11Chunk::CSSetSamplers:
    case D3D11Chunk::CSSetShader:
    case D3D11Chunk::CSSetUnorderedAccessViews:
    case D3D11Chunk::RSSetViewports:
    case D3D11Chunk::RSSetScissorRects:
    case D3D11Chunk::RSSetState:
    case D3D11Chunk::OMSetRenderTargets:
    case D3D11Chunk::OMSetRenderTargetsAndUnorderedAccessViews:
    case D3D11Chunk::OMSetBlendState:
    case D3D11Chunk::OMSetDepthStencilState:
    case D3D11Chunk::CopyResource:
    case D3D11Chunk::CopySubresourceRegion:
    case D3D11Chunk::CopySubresourceRegion1:
    case D3D11Chunk::CopyStructureCount:
    case D3D11Chunk::ResolveSubresource:
    case D3D11Chunk::UpdateSubresource:
    case D3D11Chunk::UpdateSubresource1:
    case D3D11Chunk::GenerateMips:
    case D3D11Chunk::ClearDepthStencilView:
    case D3D11Chunk::ClearRenderTargetView:
    case D3D11Chunk::ClearUnorderedAccessViewUint:
    case D3D11Chunk::ClearUnorderedAccessViewFloat:
    case D3D11Chunk::ClearView:
    case D3D11Chunk::ClearState:
    case D3D11Chunk::SetResourceMinLOD:
    case D3D11Chunk::PushMarker:
    case D3D11Chunk::PopMarker:
    case D3D11Chunk::SetMarker:
    case D3D11Chunk::SetCommandAnnotation: return true;
    default: return D3D11LiveForwardAction(chunk);
  }
}

bool D3D11LiveForwardInterval(const rdcarray<APIEvent> &events, const SDFile &file,
                              uint32_t from, uint32_t target, rdcstr &reason)
{
  if(!from || target <= from || target >= events.size() || events[from].eventId != from ||
     events[target].eventId != target || events[target].chunkIndex >= file.chunks.size() ||
     events[from].chunkIndex >= events[target].chunkIndex)
  {
    reason = "nonForwardOrMissingEvent";
    return false;
  }
  if(!D3D11LiveForwardAction(D3D11Chunk(file.chunks[events[from].chunkIndex]->metadata.chunkID)) ||
     !D3D11LiveForwardAction(D3D11Chunk(file.chunks[events[target].chunkIndex]->metadata.chunkID)))
  {
    reason = "nonDirectDrawOrDispatchTarget";
    return false;
  }
  uint32_t eid = from + 1;
  uint64_t previousOffset = events[from].fileOffset;
  for(uint32_t chunkIndex = events[from].chunkIndex + 1;
      chunkIndex <= events[target].chunkIndex; ++chunkIndex)
  {
    const D3D11Chunk type = D3D11Chunk(file.chunks[chunkIndex]->metadata.chunkID);
    if(!D3D11LiveForwardChunk(type))
    {
      reason = "unsupportedIntervalChunk:" + ToStr(type);
      return false;
    }
    if(type == D3D11Chunk::SetCommandAnnotation)
      continue;
    if(eid > target || events[eid].eventId != eid || events[eid].chunkIndex != chunkIndex ||
       events[eid].fileOffset <= previousOffset)
    {
      reason = "nonLinearEventChunkMapping";
      return false;
    }
    previousOffset = events[eid].fileOffset;
    ++eid;
  }
  reason = "linearImmediateContextRange";
  return eid == target + 1;
}

bool D3D11LiveForwardCanPublish(uint32_t target, uint32_t pending, uint64_t serial,
                               uint64_t pendingSerial, bool success, bool ordinary)
{
  return ordinary && success && target != 0 && target == pending && serial == pendingSerial;
}

bool D3D11LiveForwardCaptureChunkSafe(D3D11Chunk chunk)
{
  switch(chunk)
  {
    case D3D11Chunk::Begin:
    case D3D11Chunk::End:
    case D3D11Chunk::CreateQuery:
    case D3D11Chunk::CreateQuery1:
    case D3D11Chunk::CreatePredicate:
    case D3D11Chunk::CreateCounter:
    case D3D11Chunk::SOSetTargets:
    case D3D11Chunk::DrawAuto:
    case D3D11Chunk::SetPredication:
    case D3D11Chunk::ExecuteCommandList:
    case D3D11Chunk::FinishCommandList:
    case D3D11Chunk::PostExecuteCommandList:
    case D3D11Chunk::PostFinishCommandListSet:
    case D3D11Chunk::SwapDeviceContextState:
    case D3D11Chunk::SetShaderExtUAV: return false;
    default: return true;
  }
}
}

D3D11Replay::D3D11Replay(WrappedID3D11Device *d)
{
  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(D3D11Replay));

  m_pDevice = d;
  m_pImmediateContext = d->GetImmediateContext();

  m_Proxy = false;
  m_WARP = false;

  m_HighlightCache.driver = this;

  RDCEraseEl(m_DriverInfo);
}

D3D11Replay::~D3D11Replay()
{
  RenderDoc::Inst().UnregisterMemoryRegion(this);
}

void D3D11Replay::Shutdown()
{
  for(auto &scratch : m_BufferPatchScratch)
    SAFE_RELEASE(scratch.second);
  m_BufferPatchScratch.clear();
  m_BufferPatches.clear();
  m_Checkpoints.clear();
  m_CheckpointResources.clear();
  ClearEventCBufferOverrides();
  ClearEventShaderOverrides();
  ReleaseEventShaderPreviews();
  for(auto &item : m_EventShaderBlendStates)
    SAFE_RELEASE(item.second);
  m_EventShaderBlendStates.clear();

  for(size_t i = 0; i < m_ProxyResources.size(); i++)
    m_ProxyResources[i]->Release();
  m_ProxyResources.clear();
  m_TextureSnapshots.clear();

  m_RealState.state.Clear();

  DXBC::ResetSearchDirsCache();

  // explicitly delete the device, as all the replay resources created will be keeping refs on it
  delete m_pDevice;
}

RDResult D3D11Replay::FatalErrorCheck()
{
  return m_pDevice->FatalErrorCheck();
}

IReplayDriver *D3D11Replay::MakeDummyDriver()
{
  // gather up the shaders we've allocated to pass to the dummy driver
  rdcarray<const ShaderReflection *> shaders;
  WrappedID3D11Shader<ID3D11ComputeShader>::GetReflections(shaders);

  IReplayDriver *dummy = new DummyDriver(this, shaders, m_pDevice->DetachStructuredFile(),
                                         m_pDevice->GetImmediateContext()->DetachAnnotations());

  return dummy;
}

void D3D11Replay::InitReplayOnDevice(IDXGIFactory *factory)
{
  bool wrapped =
      RefCountDXGIObject::HandleWrap("D3D11Replay", __uuidof(IDXGIFactory), (void **)&factory);
  RDCASSERT(wrapped);
  m_pFactory = factory;

  HRESULT hr = S_OK;

  IDXGIDevice *pDXGIDevice;
  hr = m_pDevice->QueryInterface(__uuidof(IDXGIDevice), (void **)&pDXGIDevice);

  if(FAILED(hr))
  {
    RDCERR("Couldn't get DXGI device from D3D device");
  }
  else
  {
    IDXGIAdapter *pDXGIAdapter;
    hr = pDXGIDevice->GetParent(__uuidof(IDXGIAdapter), (void **)&pDXGIAdapter);

    if(FAILED(hr))
    {
      RDCERR("Couldn't get DXGI adapter from DXGI device");
      SAFE_RELEASE(pDXGIDevice);
    }
    else
    {
      DXGI_ADAPTER_DESC desc = {};
      pDXGIAdapter->GetDesc(&desc);
      LARGE_INTEGER version = {};
      pDXGIAdapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &version);

      RDCEraseEl(m_DriverInfo);

      m_DriverInfo.vendor = GPUVendorFromPCIVendor(desc.VendorId);

      rdcstr descString = GetDriverVersion(desc, version);
      descString.resize(RDCMIN(descString.size(), ARRAY_COUNT(m_DriverInfo.version) - 1));
      memcpy(m_DriverInfo.version, descString.c_str(), descString.size());

      RDCLOG("Running replay on %s / %s", ToStr(m_DriverInfo.vendor).c_str(), m_DriverInfo.version);

      if(m_WARP)
        m_DriverInfo.vendor = GPUVendor::Software;

      SAFE_RELEASE(pDXGIDevice);
      SAFE_RELEASE(pDXGIAdapter);

      if(FAILED(hr))
      {
        RDCERR("Couldn't get DXGI factory from DXGI adapter");
      }
    }
  }

  if(!m_Proxy && D3D11_HardwareCounters())
  {
    AMDCounters *countersAMD = NULL;
    IntelCounters *countersIntel = NULL;

    ID3D11Device *d3dDevice = m_pDevice->GetReal();

    if(m_DriverInfo.vendor == GPUVendor::AMD)
    {
      RDCLOG("AMD GPU detected - trying to initialise AMD counters");
      countersAMD = new AMDCounters();
    }
    else if(m_DriverInfo.vendor == GPUVendor::nVidia)
    {
      RDCLOG("nVidia GPU detected - trying to initialise nVidia counters");
      m_pNVCounters = NULL;
      m_pNVPerfCounters = NULL;
      // Legacy NVPMAPI counters
      NVCounters *countersNVPMAPI = new NVCounters();
      if(countersNVPMAPI && countersNVPMAPI->Init(d3dDevice))
      {
        m_pNVCounters = countersNVPMAPI;
      }
      else
      {
        delete countersNVPMAPI;

        // Nsight Perf SDK counters
        NVD3D11Counters *countersNvPerf = new NVD3D11Counters();
        if(countersNvPerf && countersNvPerf->Init(m_pDevice))
        {
          m_pNVPerfCounters = countersNvPerf;
        }
        else
        {
          delete countersNvPerf;
        }
      }
    }
    else if(m_DriverInfo.vendor == GPUVendor::Intel)
    {
      RDCLOG("Intel GPU detected - trying to initialize Intel counters");
      countersIntel = new IntelCounters();
    }
    else
    {
      RDCLOG("%s GPU detected - no counters available", ToStr(m_DriverInfo.vendor).c_str());
    }

    if(countersAMD && countersAMD->Init(AMDCounters::ApiType::Dx11, (void *)d3dDevice))
    {
      m_pAMDCounters = countersAMD;
    }
    else
    {
      delete countersAMD;
      m_pAMDCounters = NULL;
    }

    if(countersIntel && countersIntel->Init(d3dDevice))
    {
      m_pIntelCounters = countersIntel;
    }
    else
    {
      delete countersIntel;
      m_pIntelCounters = NULL;
    }
  }
}

void D3D11Replay::CreateResources()
{
  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.0f);

  m_pDevice->GetShaderCache()->SetCaching(true);

  InitStreamOut();

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.1f);

  m_General.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.2f);

  m_TexRender.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.3f);

  m_Overlay.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.4f);

  m_MeshRender.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.5f);

  m_VertexPick.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.6f);

  m_PixelPick.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.65f);

  m_ShaderDebug.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.7f);

  m_Histogram.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.8f);

  m_PixelHistory.Init(m_pDevice);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 0.9f);

  m_pDevice->GetShaderCache()->SetCaching(false);

  RenderDoc::Inst().SetProgress(LoadProgress::DebugManagerInit, 1.0f);
}

void D3D11Replay::DestroyResources()
{
  SAFE_RELEASE(m_CustomShaderTex);

  m_General.Release();
  m_TexRender.Release();
  m_Overlay.Release();
  m_MeshRender.Release();
  m_VertexPick.Release();
  m_PixelPick.Release();
  m_ShaderDebug.Release();
  m_Histogram.Release();
  m_PixelHistory.Release();

  SAFE_DELETE(m_pAMDCounters);
  SAFE_DELETE(m_pNVCounters);
  SAFE_DELETE(m_pNVPerfCounters);
  SAFE_DELETE(m_pIntelCounters);

  ShutdownStreamOut();
  ClearPostVSCache();

  SAFE_RELEASE(m_pFactory);
}

rdcarray<ShaderEntryPoint> D3D11Replay::GetShaderEntryPoints(ResourceId shader)
{
  auto it = WrappedShader::m_ShaderList.find(shader);

  if(it == WrappedShader::m_ShaderList.end())
    return {};

  ShaderReflection &ret = it->second->GetDetails();

  // Debug information can preserve a named HLSL entry. Match the reflection returned by
  // GetShader; callers use this name to select and validate the exact bound shader.
  return {{ret.entryPoint.empty() ? "main" : ret.entryPoint, ret.stage}};
}

ShaderReflection *D3D11Replay::GetShader(ResourceId pipeline, ResourceId shader,
                                         ShaderEntryPoint entry)
{
  auto it = WrappedShader::m_ShaderList.find(shader);

  if(it == WrappedShader::m_ShaderList.end())
    return NULL;

  ShaderReflection &ret = it->second->GetDetails();

  return &ret;
}

rdcarray<rdcstr> D3D11Replay::GetDisassemblyTargets(bool withPipeline)
{
  return {DXBCDisassemblyTarget};
}

rdcstr D3D11Replay::DisassembleShader(ResourceId pipeline, const ShaderReflection *refl,
                                      const rdcstr &target)
{
  auto it = WrappedShader::m_ShaderList.find(refl->resourceId);

  if(it == WrappedShader::m_ShaderList.end())
    return "; Invalid Shader Specified";

  DXBC::DXBCContainer *dxbc = it->second->GetDXBC();

  if(target == DXBCDisassemblyTarget || target.empty())
    return dxbc->GetDisassembly(false);

  return StringFormat::Fmt("; Invalid disassembly target %s", target.c_str());
}

void D3D11Replay::FreeTargetResource(ResourceId id)
{
  if(m_pDevice->GetResourceManager()->HasResource(id))
  {
    ID3D11DeviceChild *resource = m_pDevice->GetResourceManager()->GetResource(id);

    SAFE_RELEASE(resource);
  }
}

void D3D11Replay::FreeCustomShader(ResourceId id)
{
  if(m_pDevice->GetResourceManager()->HasResource(id))
  {
    ID3D11DeviceChild *resource = m_pDevice->GetResourceManager()->GetResource(id);

    SAFE_RELEASE(resource);
  }
}

rdcarray<EventUsage> D3D11Replay::GetUsage(ResourceId id)
{
  if(WrappedID3D11Buffer::m_BufferList.find(id) == WrappedID3D11Buffer::m_BufferList.end() &&
     WrappedID3D11Texture1D::m_TextureList.find(id) == WrappedID3D11Texture1D::m_TextureList.end() &&
     WrappedID3D11Texture2D1::m_TextureList.find(id) == WrappedID3D11Texture2D1::m_TextureList.end() &&
     WrappedID3D11Texture3D1::m_TextureList.find(id) == WrappedID3D11Texture3D1::m_TextureList.end() &&
     WrappedShader::m_ShaderList.find(id) == WrappedShader::m_ShaderList.end())
  {
    return {EventUsage(0, ResourceUsage::Unused)};
  }

  return m_pDevice->GetImmediateContext()->GetUsage(id);
}

rdcarray<DebugMessage> D3D11Replay::GetDebugMessages()
{
  return m_pDevice->GetDebugMessages();
}

rdcarray<GPUDevice> D3D11Replay::GetAvailableGPUs()
{
  rdcarray<GPUDevice> ret;

  for(UINT i = 0; i < 10; i++)
  {
    IDXGIAdapter *adapter = NULL;

    HRESULT hr = m_pFactory->EnumAdapters(i, &adapter);

    if(SUCCEEDED(hr) && adapter)
    {
      DXGI_ADAPTER_DESC desc;
      adapter->GetDesc(&desc);

      GPUDevice dev;
      dev.vendor = GPUVendorFromPCIVendor(desc.VendorId);
      dev.deviceID = desc.DeviceId;
      dev.driver = "";    // D3D doesn't have multiple drivers per API
      dev.name = StringFormat::Wide2UTF8(desc.Description);
      dev.apis = {GraphicsAPI::D3D11};

      // don't add duplicate devices even if they get enumerated. Don't add WARP, we'll do that
      // manually since it's inconsistently enumerated
      if(ret.indexOf(dev) == -1 && dev.vendor != GPUVendor::Software)
        ret.push_back(dev);
    }

    SAFE_RELEASE(adapter);
  }

  {
    GPUDevice dev;
    dev.vendor = GPUVendor::Software;
    dev.deviceID = 0;
    dev.driver = "";    // D3D doesn't have multiple drivers per API
    dev.name = "WARP Rasterizer";
    dev.apis = {GraphicsAPI::D3D11};
    ret.push_back(dev);
  }

  return ret;
}

APIProperties D3D11Replay::GetAPIProperties()
{
  APIProperties ret = m_pDevice->APIProps;

  ret.pipelineType = GraphicsAPI::D3D11;
  ret.localRenderer = GraphicsAPI::D3D11;
  ret.vendor = m_DriverInfo.vendor;
  ret.degraded = m_WARP;
  ret.shaderDebugging = true;
  ret.pixelHistory = true;

  return ret;
}

ResourceDescription &D3D11Replay::GetResourceDesc(ResourceId id)
{
  auto it = m_ResourceIdx.find(id);
  if(it == m_ResourceIdx.end())
  {
    m_ResourceIdx[id] = m_Resources.size();
    m_Resources.push_back(ResourceDescription());
    m_Resources.back().resourceId = id;
    return m_Resources.back();
  }

  return m_Resources[it->second];
}

rdcarray<ResourceDescription> D3D11Replay::GetResources()
{
  return m_Resources;
}

BufferDescription D3D11Replay::GetBuffer(ResourceId id)
{
  BufferDescription ret = {};
  ret.resourceId = ResourceId();

  auto it = WrappedID3D11Buffer::m_BufferList.find(id);

  if(it == WrappedID3D11Buffer::m_BufferList.end())
    return ret;

  WrappedID3D11Buffer *d3dbuf = it->second.m_Buffer;

  rdcstr str = GetDebugName(d3dbuf);

  ret.resourceId = it->first;

  D3D11_BUFFER_DESC desc;
  it->second.m_Buffer->GetDesc(&desc);

  ret.length = desc.ByteWidth;

  ret.creationFlags = BufferCategory::NoFlags;
  if(desc.BindFlags & D3D11_BIND_VERTEX_BUFFER)
    ret.creationFlags |= BufferCategory::Vertex;
  if(desc.BindFlags & D3D11_BIND_INDEX_BUFFER)
    ret.creationFlags |= BufferCategory::Index;
  if(desc.BindFlags & D3D11_BIND_UNORDERED_ACCESS)
    ret.creationFlags |= BufferCategory::ReadWrite;
  if(desc.MiscFlags & D3D11_RESOURCE_MISC_DRAWINDIRECT_ARGS)
    ret.creationFlags |= BufferCategory::Indirect;

  return ret;
}

TextureDescription D3D11Replay::GetTexture(ResourceId id)
{
  TextureDescription tex = {};
  tex.resourceId = ResourceId();

  auto it1D = WrappedID3D11Texture1D::m_TextureList.find(id);
  if(it1D != WrappedID3D11Texture1D::m_TextureList.end())
  {
    WrappedID3D11Texture1D *d3dtex = (WrappedID3D11Texture1D *)it1D->second.m_Texture;

    rdcstr str = GetDebugName(d3dtex);

    D3D11_TEXTURE1D_DESC desc;
    d3dtex->GetDesc(&desc);

    tex.resourceId = it1D->first;
    tex.dimension = 1;
    tex.width = desc.Width;
    tex.height = 1;
    tex.depth = 1;
    tex.cubemap = false;
    tex.format = MakeResourceFormat(desc.Format);

    tex.creationFlags = TextureCategory::NoFlags;
    if(desc.BindFlags & D3D11_BIND_SHADER_RESOURCE)
      tex.creationFlags |= TextureCategory::ShaderRead;
    if(desc.BindFlags & D3D11_BIND_RENDER_TARGET)
      tex.creationFlags |= TextureCategory::ColorTarget;
    if(desc.BindFlags & D3D11_BIND_DEPTH_STENCIL)
      tex.creationFlags |= TextureCategory::DepthTarget;
    if(desc.BindFlags & D3D11_BIND_UNORDERED_ACCESS)
      tex.creationFlags |= TextureCategory::ShaderReadWrite;

    tex.mips = desc.MipLevels;

    if(desc.MipLevels == 0)
      tex.mips = CalcNumMips(desc.Width, 1, 1);

    tex.arraysize = desc.ArraySize;

    tex.type = tex.arraysize > 1 ? TextureType::Texture1DArray : TextureType::Texture1D;

    tex.msQual = 0;
    tex.msSamp = 1;

    tex.byteSize = 0;
    for(uint32_t s = 0; s < tex.mips * tex.arraysize; s++)
      tex.byteSize += GetByteSize(d3dtex, s);

    return tex;
  }

  auto it2D = WrappedID3D11Texture2D1::m_TextureList.find(id);
  if(it2D != WrappedID3D11Texture2D1::m_TextureList.end())
  {
    WrappedID3D11Texture2D1 *d3dtex = (WrappedID3D11Texture2D1 *)it2D->second.m_Texture;

    rdcstr str = GetDebugName(d3dtex);

    D3D11_TEXTURE2D_DESC desc;
    d3dtex->GetDesc(&desc);

    if(d3dtex->m_RealDescriptor)
      desc.Format = d3dtex->m_RealDescriptor->Format;

    tex.resourceId = it2D->first;
    tex.dimension = 2;
    tex.width = desc.Width;
    tex.height = desc.Height;
    tex.depth = 1;
    tex.format = MakeResourceFormat(desc.Format);

    tex.creationFlags = TextureCategory::NoFlags;
    if(desc.BindFlags & D3D11_BIND_SHADER_RESOURCE)
      tex.creationFlags |= TextureCategory::ShaderRead;
    if(desc.BindFlags & D3D11_BIND_RENDER_TARGET)
      tex.creationFlags |= TextureCategory::ColorTarget;
    if(desc.BindFlags & D3D11_BIND_DEPTH_STENCIL)
      tex.creationFlags |= TextureCategory::DepthTarget;
    if(desc.BindFlags & D3D11_BIND_UNORDERED_ACCESS)
      tex.creationFlags |= TextureCategory::ShaderReadWrite;
    if(d3dtex->m_RealDescriptor)
      tex.creationFlags |= TextureCategory::SwapBuffer;

    tex.cubemap = false;
    if(desc.MiscFlags & D3D11_RESOURCE_MISC_TEXTURECUBE)
      tex.cubemap = true;

    tex.mips = desc.MipLevels;

    if(desc.MipLevels == 0)
      tex.mips = CalcNumMips(desc.Width, desc.Height, 1);

    tex.arraysize = desc.ArraySize;

    tex.msQual = desc.SampleDesc.Quality;
    tex.msSamp = RDCMAX(1U, desc.SampleDesc.Count);

    tex.type = tex.arraysize > 1 ? TextureType::Texture2DArray : TextureType::Texture2D;
    if(tex.cubemap)
      tex.type = tex.arraysize > 1 ? TextureType::TextureCubeArray : TextureType::TextureCube;
    if(tex.msSamp > 1)
      tex.type = tex.arraysize > 1 ? TextureType::Texture2DMSArray : TextureType::Texture2DMS;

    tex.byteSize = 0;
    for(uint32_t s = 0; s < tex.arraysize * tex.mips; s++)
      tex.byteSize += GetByteSize(d3dtex, s);
    tex.byteSize *= tex.msSamp;

    return tex;
  }

  auto it3D = WrappedID3D11Texture3D1::m_TextureList.find(id);
  if(it3D != WrappedID3D11Texture3D1::m_TextureList.end())
  {
    WrappedID3D11Texture3D1 *d3dtex = (WrappedID3D11Texture3D1 *)it3D->second.m_Texture;

    rdcstr str = GetDebugName(d3dtex);

    D3D11_TEXTURE3D_DESC desc;
    d3dtex->GetDesc(&desc);

    tex.resourceId = it3D->first;
    tex.dimension = 3;
    tex.width = desc.Width;
    tex.height = desc.Height;
    tex.depth = desc.Depth;
    tex.cubemap = false;
    tex.format = MakeResourceFormat(desc.Format);

    tex.type = TextureType::Texture3D;

    tex.creationFlags = TextureCategory::NoFlags;
    if(desc.BindFlags & D3D11_BIND_SHADER_RESOURCE)
      tex.creationFlags |= TextureCategory::ShaderRead;
    if(desc.BindFlags & D3D11_BIND_RENDER_TARGET)
      tex.creationFlags |= TextureCategory::ColorTarget;
    if(desc.BindFlags & D3D11_BIND_DEPTH_STENCIL)
      tex.creationFlags |= TextureCategory::DepthTarget;
    if(desc.BindFlags & D3D11_BIND_UNORDERED_ACCESS)
      tex.creationFlags |= TextureCategory::ShaderReadWrite;

    tex.mips = desc.MipLevels;

    if(desc.MipLevels == 0)
      tex.mips = CalcNumMips(desc.Width, desc.Height, desc.Depth);

    tex.msQual = 0;
    tex.msSamp = 1;

    tex.arraysize = 1;

    tex.byteSize = 0;
    for(uint32_t s = 0; s < tex.arraysize * tex.mips; s++)
      tex.byteSize += GetByteSize(d3dtex, s);

    return tex;
  }

  RDCERR("Unrecognised/unknown texture %s", ToStr(id).c_str());

  tex.byteSize = 0;
  tex.dimension = 2;
  tex.type = TextureType::Texture2D;
  tex.width = 1;
  tex.height = 1;
  tex.depth = 1;
  tex.cubemap = false;
  tex.mips = 1;
  tex.arraysize = 1;
  tex.msQual = 0;
  tex.msSamp = 1;

  return tex;
}

rdcarray<BufferDescription> D3D11Replay::GetBuffers()
{
  rdcarray<BufferDescription> ret;

  ret.reserve(WrappedID3D11Buffer::m_BufferList.size());

  for(auto it = WrappedID3D11Buffer::m_BufferList.begin();
      it != WrappedID3D11Buffer::m_BufferList.end(); ++it)
  {
    // skip buffers that aren't from the log
    if(ResourceIDGen::IsReplayOnlyID(it->first))
      continue;

    ret.push_back(GetBuffer(it->first));
  }

  GetDebugManager()->GetCounterBuffers(ret);

  return ret;
}

rdcarray<TextureDescription> D3D11Replay::GetTextures()
{
  rdcarray<TextureDescription> ret;

  ret.reserve(WrappedID3D11Texture1D::m_TextureList.size() +
              WrappedID3D11Texture2D1::m_TextureList.size() +
              WrappedID3D11Texture3D1::m_TextureList.size());

  for(auto it = WrappedID3D11Texture1D::m_TextureList.begin();
      it != WrappedID3D11Texture1D::m_TextureList.end(); ++it)
  {
    // skip textures that aren't from the log
    if(ResourceIDGen::IsReplayOnlyID(it->first))
      continue;

    ret.push_back(GetTexture(it->first));
  }

  for(auto it = WrappedID3D11Texture2D1::m_TextureList.begin();
      it != WrappedID3D11Texture2D1::m_TextureList.end(); ++it)
  {
    // skip textures that aren't from the log
    if(ResourceIDGen::IsReplayOnlyID(it->first))
      continue;

    ret.push_back(GetTexture(it->first));
  }

  for(auto it = WrappedID3D11Texture3D1::m_TextureList.begin();
      it != WrappedID3D11Texture3D1::m_TextureList.end(); ++it)
  {
    // skip textures that aren't from the log
    if(ResourceIDGen::IsReplayOnlyID(it->first))
      continue;

    ret.push_back(GetTexture(it->first));
  }

  return ret;
}

void D3D11Replay::SavePipelineState(uint32_t eventId)
{
  if(!m_D3D11PipelineState)
    return;

  D3D11RenderState *rs = m_pDevice->GetImmediateContext()->GetCurrentPipelineState();

  m_RenderStateOM = rs->OM;

  D3D11Pipe::State &ret = *m_D3D11PipelineState;

  /////////////////////////////////////////////////
  // Input Assembler
  /////////////////////////////////////////////////

  D3D11ResourceManager *rm = m_pDevice->GetResourceManager();

  ret.descriptorStore = m_pImmediateContext->GetDescriptorsID();
  ret.descriptorCount =
      EncodeD3D11DescriptorIndex({ShaderStage::Compute, D3D11DescriptorMapping::Count, 0});
  ret.descriptorByteSize = 1;

  ret.inputAssembly.bytecode = NULL;
  ret.inputAssembly.resourceId = ResourceId();
  ret.inputAssembly.layouts.clear();

  if(rs->IA.Layout)
  {
    const rdcarray<D3D11_INPUT_ELEMENT_DESC> &vec = m_pDevice->GetLayoutDesc(rs->IA.Layout);

    ResourceId layoutId = GetIDForDeviceChild(rs->IA.Layout);

    ret.inputAssembly.resourceId = layoutId;
    ret.inputAssembly.bytecode = GetShader(ResourceId(), layoutId, ShaderEntryPoint());

    ret.inputAssembly.layouts.resize(vec.size());
    for(size_t i = 0; i < vec.size(); i++)
    {
      D3D11Pipe::Layout &l = ret.inputAssembly.layouts[i];

      l.byteOffset = vec[i].AlignedByteOffset;
      l.format = MakeResourceFormat(vec[i].Format);
      l.inputSlot = vec[i].InputSlot;
      l.perInstance = vec[i].InputSlotClass == D3D11_INPUT_PER_INSTANCE_DATA;
      l.instanceDataStepRate = vec[i].InstanceDataStepRate;
      l.semanticIndex = vec[i].SemanticIndex;
      l.semanticName = vec[i].SemanticName;
    }
  }

  ret.inputAssembly.vertexBuffers.resize(ARRAY_COUNT(rs->IA.VBs));
  for(size_t i = 0; i < ARRAY_COUNT(rs->IA.VBs); i++)
  {
    D3D11Pipe::VertexBuffer &vb = ret.inputAssembly.vertexBuffers[i];

    vb.resourceId = GetIDForDeviceChild(rs->IA.VBs[i]);
    vb.byteOffset = rs->IA.Offsets[i];
    vb.byteStride = rs->IA.Strides[i];
  }

  ret.inputAssembly.indexBuffer.resourceId = GetIDForDeviceChild(rs->IA.IndexBuffer);
  ret.inputAssembly.indexBuffer.byteOffset = rs->IA.IndexOffset;
  switch(rs->IA.IndexFormat)
  {
    case DXGI_FORMAT_R32_UINT: ret.inputAssembly.indexBuffer.byteStride = 4; break;
    case DXGI_FORMAT_R16_UINT: ret.inputAssembly.indexBuffer.byteStride = 2; break;
    case DXGI_FORMAT_R8_UINT: ret.inputAssembly.indexBuffer.byteStride = 1; break;
    default: ret.inputAssembly.indexBuffer.byteStride = 0; break;
  }

  ret.inputAssembly.topology = MakePrimitiveTopology(rs->IA.Topo);

  /////////////////////////////////////////////////
  // Shaders
  /////////////////////////////////////////////////

  {
    D3D11Pipe::Shader *dstArr[] = {&ret.vertexShader,   &ret.hullShader,  &ret.domainShader,
                                   &ret.geometryShader, &ret.pixelShader, &ret.computeShader};
    const D3D11RenderState::Shader *srcArr[] = {&rs->VS, &rs->HS, &rs->DS,
                                                &rs->GS, &rs->PS, &rs->CS};

    for(size_t stage = 0; stage < ARRAY_COUNT(dstArr); stage++)
    {
      D3D11Pipe::Shader &dst = *dstArr[stage];
      const D3D11RenderState::Shader &src = *srcArr[stage];

      dst.stage = (ShaderStage)stage;

      ResourceId id = GetIDForDeviceChild(src.Object);

      WrappedShader *shad = (WrappedShader *)(WrappedID3D11Shader<ID3D11VertexShader> *)src.Object;

      ShaderReflection *refl = NULL;

      if(shad != NULL)
        refl = &shad->GetDetails();

      dst.resourceId = rm->GetUnreplacedID(id);
      dst.reflection = refl;

      dst.classInstances.reserve(src.NumInstances);
      for(UINT s = 0; s < src.NumInstances; s++)
      {
        D3D11_CLASS_INSTANCE_DESC desc;
        src.Instances[s]->GetDesc(&desc);

        char typeName[256] = {0};
        SIZE_T count = 255;
        src.Instances[s]->GetTypeName(typeName, &count);

        char instName[256] = {0};
        count = 255;
        src.Instances[s]->GetInstanceName(instName, &count);

        dst.classInstances.push_back(instName);
      }
    }
  }

  /////////////////////////////////////////////////
  // Stream Out
  /////////////////////////////////////////////////

  {
    ret.streamOut.outputs.resize(D3D11_SO_BUFFER_SLOT_COUNT);
    for(size_t s = 0; s < D3D11_SO_BUFFER_SLOT_COUNT; s++)
    {
      ret.streamOut.outputs[s].resourceId = GetIDForDeviceChild(rs->SO.Buffers[s]);
      ret.streamOut.outputs[s].byteOffset = rs->SO.Offsets[s];
    }

    const SOShaderData &soshader = m_pDevice->GetSOShaderData(GetIDForDeviceChild(rs->GS.Object));

    ret.streamOut.rasterizedStream = soshader.rastStream;
  }

  /////////////////////////////////////////////////
  // Rasterizer
  /////////////////////////////////////////////////

  {
    D3D11_RASTERIZER_DESC desc;

    if(rs->RS.State)
    {
      rs->RS.State->GetDesc(&desc);

      ret.rasterizer.state.antialiasedLines = desc.AntialiasedLineEnable == TRUE;

      ret.rasterizer.state.cullMode = CullMode::NoCull;
      if(desc.CullMode == D3D11_CULL_FRONT)
        ret.rasterizer.state.cullMode = CullMode::Front;
      if(desc.CullMode == D3D11_CULL_BACK)
        ret.rasterizer.state.cullMode = CullMode::Back;

      ret.rasterizer.state.fillMode = FillMode::Solid;
      if(desc.FillMode == D3D11_FILL_WIREFRAME)
        ret.rasterizer.state.fillMode = FillMode::Wireframe;

      ret.rasterizer.state.depthBias = desc.DepthBias;
      ret.rasterizer.state.depthBiasClamp = desc.DepthBiasClamp;
      ret.rasterizer.state.depthClip = desc.DepthClipEnable == TRUE;
      ret.rasterizer.state.frontCCW = desc.FrontCounterClockwise == TRUE;
      ret.rasterizer.state.multisampleEnable = desc.MultisampleEnable == TRUE;
      ret.rasterizer.state.scissorEnable = desc.ScissorEnable == TRUE;
      ret.rasterizer.state.slopeScaledDepthBias = desc.SlopeScaledDepthBias;
      ret.rasterizer.state.forcedSampleCount = 0;

      D3D11_RASTERIZER_DESC1 desc1;
      RDCEraseEl(desc1);

      if(CanQuery<ID3D11RasterizerState1>(rs->RS.State))
      {
        ((ID3D11RasterizerState1 *)rs->RS.State)->GetDesc1(&desc1);
        ret.rasterizer.state.forcedSampleCount = desc1.ForcedSampleCount;
      }

      D3D11_RASTERIZER_DESC2 desc2;
      RDCEraseEl(desc2);

      if(CanQuery<ID3D11RasterizerState2>(rs->RS.State))
      {
        ((ID3D11RasterizerState2 *)rs->RS.State)->GetDesc2(&desc2);

        // D3D only supports overestimate conservative raster (underestimated can be emulated using
        // coverage information in the shader)
        ret.rasterizer.state.conservativeRasterization =
            desc2.ConservativeRaster == D3D11_CONSERVATIVE_RASTERIZATION_MODE_ON
                ? ConservativeRaster::Overestimate
                : ConservativeRaster::Disabled;
      }

      ret.rasterizer.state.resourceId = GetIDForDeviceChild(rs->RS.State);
    }
    else
    {
      ret.rasterizer.state.antialiasedLines = FALSE;
      ret.rasterizer.state.cullMode = CullMode::Back;
      ret.rasterizer.state.depthBias = 0;
      ret.rasterizer.state.depthBiasClamp = 0.0f;
      ret.rasterizer.state.depthClip = TRUE;
      ret.rasterizer.state.fillMode = FillMode::Solid;
      ret.rasterizer.state.frontCCW = FALSE;
      ret.rasterizer.state.multisampleEnable = FALSE;
      ret.rasterizer.state.scissorEnable = FALSE;
      ret.rasterizer.state.slopeScaledDepthBias = 0.0f;
      ret.rasterizer.state.forcedSampleCount = 0;
      ret.rasterizer.state.resourceId = ResourceId();
    }

    size_t i = 0;
    ret.rasterizer.scissors.resize(D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE);
    for(i = 0; i < rs->RS.NumScissors; i++)
      ret.rasterizer.scissors[i] = Scissor(rs->RS.Scissors[i].left, rs->RS.Scissors[i].top,
                                           rs->RS.Scissors[i].right - rs->RS.Scissors[i].left,
                                           rs->RS.Scissors[i].bottom - rs->RS.Scissors[i].top,
                                           ret.rasterizer.state.scissorEnable);

    for(; i < D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE; i++)
      ret.rasterizer.scissors[i] = Scissor(0, 0, 0, 0, ret.rasterizer.state.scissorEnable);

    ret.rasterizer.viewports.resize(D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE);
    for(i = 0; i < rs->RS.NumViews; i++)
      ret.rasterizer.viewports[i] =
          Viewport(rs->RS.Viewports[i].TopLeftX, rs->RS.Viewports[i].TopLeftY,
                   rs->RS.Viewports[i].Width, rs->RS.Viewports[i].Height,
                   rs->RS.Viewports[i].MinDepth, rs->RS.Viewports[i].MaxDepth, true);

    for(; i < D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE; i++)
      ret.rasterizer.viewports[i] = Viewport(0, 0, 0, 0, 0, 0, false);
  }

  /////////////////////////////////////////////////
  // Output Merger
  /////////////////////////////////////////////////

  {
    ret.outputMerger.renderTargets.resize(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT);
    for(size_t i = 0; i < D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT; i++)
    {
      Descriptor &descriptor = ret.outputMerger.renderTargets[i];

      descriptor.view = GetIDForDeviceChild(rs->OM.RenderTargets[i]);

      if(descriptor.view != ResourceId())
      {
        D3D11_RENDER_TARGET_VIEW_DESC desc;
        rs->OM.RenderTargets[i]->GetDesc(&desc);

        ID3D11Resource *res = NULL;
        rs->OM.RenderTargets[i]->GetResource(&res);

        descriptor.bufferStructCount = 0;
        descriptor.elementByteSize =
            desc.Format == DXGI_FORMAT_UNKNOWN ? 1 : GetByteSize(1, 1, 1, desc.Format, 0);

        descriptor.resource = GetIDForDeviceChild(res);

        descriptor.type = DescriptorType::ReadWriteImage;
        descriptor.format = MakeResourceFormat(desc.Format);
        descriptor.textureType = MakeTextureDim(desc.ViewDimension);

        if(desc.ViewDimension == D3D11_RTV_DIMENSION_BUFFER)
        {
          descriptor.byteOffset = desc.Buffer.FirstElement * descriptor.elementByteSize;
          descriptor.byteSize = desc.Buffer.NumElements * descriptor.elementByteSize;
        }
        else if(desc.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE1D)
        {
          descriptor.numSlices = 1;
          descriptor.firstSlice = 0;
          descriptor.firstMip = desc.Texture1D.MipSlice & 0xff;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE1DARRAY)
        {
          descriptor.numSlices = desc.Texture1DArray.ArraySize & 0xffff;
          descriptor.firstSlice = desc.Texture1DArray.FirstArraySlice & 0xffff;
          descriptor.firstMip = desc.Texture1DArray.MipSlice & 0xff;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE2D)
        {
          descriptor.numSlices = 1;
          descriptor.firstSlice = 0;
          descriptor.firstMip = desc.Texture2D.MipSlice & 0xff;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE2DARRAY)
        {
          descriptor.numSlices = desc.Texture2DArray.ArraySize & 0xffff;
          descriptor.firstSlice = desc.Texture2DArray.FirstArraySlice & 0xffff;
          descriptor.firstMip = desc.Texture2DArray.MipSlice & 0xff;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE2DMS)
        {
          descriptor.firstMip = 0;
          descriptor.numMips = 1;
          descriptor.firstSlice = 0;
          descriptor.numSlices = 1;
        }
        else if(desc.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE2DMSARRAY)
        {
          descriptor.numSlices = desc.Texture2DMSArray.ArraySize & 0xffff;
          descriptor.firstSlice = desc.Texture2DMSArray.FirstArraySlice & 0xffff;
          descriptor.firstMip = 0;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE3D)
        {
          descriptor.numSlices = desc.Texture3D.WSize & 0xffff;
          descriptor.firstSlice = desc.Texture3D.FirstWSlice & 0xffff;
          descriptor.firstMip = desc.Texture3D.MipSlice & 0xff;
          descriptor.numMips = 1;
        }

        SAFE_RELEASE(res);
      }
      else
      {
        descriptor = Descriptor();
      }
    }

    ret.outputMerger.uavStartSlot = rs->OM.UAVStartSlot;

    {
      Descriptor &descriptor = ret.outputMerger.depthTarget;

      descriptor.view = GetIDForDeviceChild(rs->OM.DepthView);

      if(descriptor.view != ResourceId())
      {
        D3D11_DEPTH_STENCIL_VIEW_DESC desc;
        rs->OM.DepthView->GetDesc(&desc);

        ID3D11Resource *res = NULL;
        rs->OM.DepthView->GetResource(&res);

        descriptor.bufferStructCount = 0;
        descriptor.elementByteSize =
            desc.Format == DXGI_FORMAT_UNKNOWN ? 1 : GetByteSize(1, 1, 1, desc.Format, 0);

        ret.outputMerger.depthReadOnly = false;
        ret.outputMerger.stencilReadOnly = false;

        if(desc.Flags & D3D11_DSV_READ_ONLY_DEPTH)
          ret.outputMerger.depthReadOnly = true;
        if(desc.Flags & D3D11_DSV_READ_ONLY_STENCIL)
          ret.outputMerger.stencilReadOnly = true;

        descriptor.resource = GetIDForDeviceChild(res);

        descriptor.type = DescriptorType::ReadWriteImage;
        descriptor.format = MakeResourceFormat(desc.Format);
        descriptor.textureType = MakeTextureDim(desc.ViewDimension);

        if(desc.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE1D)
        {
          descriptor.numSlices = 1;
          descriptor.firstSlice = 0;
          descriptor.firstMip = desc.Texture1D.MipSlice & 0xff;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE1DARRAY)
        {
          descriptor.numSlices = desc.Texture1DArray.ArraySize & 0xffff;
          descriptor.firstSlice = desc.Texture1DArray.FirstArraySlice & 0xffff;
          descriptor.firstMip = desc.Texture1DArray.MipSlice & 0xff;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE2D)
        {
          descriptor.numSlices = 1;
          descriptor.firstSlice = 0;
          descriptor.firstMip = desc.Texture2D.MipSlice & 0xff;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE2DARRAY)
        {
          descriptor.numSlices = desc.Texture2DArray.ArraySize & 0xffff;
          descriptor.firstSlice = desc.Texture2DArray.FirstArraySlice & 0xffff;
          descriptor.firstMip = desc.Texture2DArray.MipSlice & 0xff;
          descriptor.numMips = 1;
        }
        else if(desc.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE2DMS)
        {
          descriptor.firstMip = 0;
          descriptor.numMips = 1;
          descriptor.firstSlice = 0;
          descriptor.numSlices = 1;
        }
        else if(desc.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE2DMSARRAY)
        {
          descriptor.numSlices = desc.Texture2DMSArray.ArraySize & 0xffff;
          descriptor.firstSlice = desc.Texture2DMSArray.FirstArraySlice & 0xffff;
          descriptor.firstMip = 0;
          descriptor.numMips = 1;
        }

        SAFE_RELEASE(res);
      }
      else
      {
        descriptor = Descriptor();
      }
    }

    ret.outputMerger.blendState.sampleMask = rs->OM.SampleMask;

    ret.outputMerger.blendState.blendFactor = rs->OM.BlendFactor;

    if(rs->OM.BlendState)
    {
      D3D11_BLEND_DESC desc;
      rs->OM.BlendState->GetDesc(&desc);

      ret.outputMerger.blendState.resourceId = GetIDForDeviceChild(rs->OM.BlendState);

      ret.outputMerger.blendState.alphaToCoverage = desc.AlphaToCoverageEnable == TRUE;
      ret.outputMerger.blendState.independentBlend = desc.IndependentBlendEnable == TRUE;

      bool state1 = false;
      D3D11_BLEND_DESC1 desc1;
      RDCEraseEl(desc1);

      if(CanQuery<ID3D11BlendState1>(rs->OM.BlendState))
      {
        ((WrappedID3D11BlendState1 *)rs->OM.BlendState)->GetDesc1(&desc1);

        state1 = true;
      }

      ret.outputMerger.blendState.blends.resize(8);
      for(size_t i = 0; i < 8; i++)
      {
        ColorBlend &blend = ret.outputMerger.blendState.blends[i];

        blend.enabled = desc.RenderTarget[i].BlendEnable == TRUE;

        blend.logicOperationEnabled = state1 && desc1.RenderTarget[i].LogicOpEnable == TRUE;
        blend.logicOperation =
            state1 ? MakeLogicOp(desc1.RenderTarget[i].LogicOp) : LogicOperation::NoOp;

        blend.alphaBlend.source = MakeBlendMultiplier(desc.RenderTarget[i].SrcBlendAlpha, true);
        blend.alphaBlend.destination = MakeBlendMultiplier(desc.RenderTarget[i].DestBlendAlpha, true);
        blend.alphaBlend.operation = MakeBlendOp(desc.RenderTarget[i].BlendOpAlpha);

        blend.colorBlend.source = MakeBlendMultiplier(desc.RenderTarget[i].SrcBlend, false);
        blend.colorBlend.destination = MakeBlendMultiplier(desc.RenderTarget[i].DestBlend, false);
        blend.colorBlend.operation = MakeBlendOp(desc.RenderTarget[i].BlendOp);

        blend.writeMask = desc.RenderTarget[i].RenderTargetWriteMask;
      }
    }
    else
    {
      ret.outputMerger.blendState.resourceId = ResourceId();

      ret.outputMerger.blendState.alphaToCoverage = false;
      ret.outputMerger.blendState.independentBlend = false;

      ColorBlend blend;

      blend.enabled = false;

      blend.alphaBlend.source = BlendMultiplier::One;
      blend.alphaBlend.destination = BlendMultiplier::Zero;
      blend.alphaBlend.operation = BlendOperation::Add;

      blend.colorBlend.source = BlendMultiplier::One;
      blend.colorBlend.destination = BlendMultiplier::Zero;
      blend.colorBlend.operation = BlendOperation::Add;

      blend.logicOperationEnabled = false;
      blend.logicOperation = LogicOperation::NoOp;

      blend.writeMask = D3D11_COLOR_WRITE_ENABLE_ALL;

      ret.outputMerger.blendState.blends.resize(8);
      for(size_t i = 0; i < 8; i++)
        ret.outputMerger.blendState.blends[i] = blend;
    }

    if(rs->OM.DepthStencilState)
    {
      D3D11_DEPTH_STENCIL_DESC desc;
      rs->OM.DepthStencilState->GetDesc(&desc);

      ret.outputMerger.depthStencilState.depthEnable = desc.DepthEnable == TRUE;
      ret.outputMerger.depthStencilState.depthFunction = MakeCompareFunc(desc.DepthFunc);
      ret.outputMerger.depthStencilState.depthWrites =
          desc.DepthWriteMask == D3D11_DEPTH_WRITE_MASK_ALL;
      ret.outputMerger.depthStencilState.stencilEnable = desc.StencilEnable == TRUE;
      ret.outputMerger.depthStencilState.resourceId = GetIDForDeviceChild(rs->OM.DepthStencilState);

      ret.outputMerger.depthStencilState.frontFace.function =
          MakeCompareFunc(desc.FrontFace.StencilFunc);
      ret.outputMerger.depthStencilState.frontFace.depthFailOperation =
          MakeStencilOp(desc.FrontFace.StencilDepthFailOp);
      ret.outputMerger.depthStencilState.frontFace.passOperation =
          MakeStencilOp(desc.FrontFace.StencilPassOp);
      ret.outputMerger.depthStencilState.frontFace.failOperation =
          MakeStencilOp(desc.FrontFace.StencilFailOp);

      ret.outputMerger.depthStencilState.backFace.function =
          MakeCompareFunc(desc.BackFace.StencilFunc);
      ret.outputMerger.depthStencilState.backFace.depthFailOperation =
          MakeStencilOp(desc.BackFace.StencilDepthFailOp);
      ret.outputMerger.depthStencilState.backFace.passOperation =
          MakeStencilOp(desc.BackFace.StencilPassOp);
      ret.outputMerger.depthStencilState.backFace.failOperation =
          MakeStencilOp(desc.BackFace.StencilFailOp);

      // due to shared structs, this is slightly duplicated - D3D doesn't have separate states for
      // front/back.
      ret.outputMerger.depthStencilState.frontFace.reference = rs->OM.StencRef;
      ret.outputMerger.depthStencilState.frontFace.compareMask = desc.StencilReadMask;
      ret.outputMerger.depthStencilState.frontFace.writeMask = desc.StencilWriteMask;
      ret.outputMerger.depthStencilState.backFace.reference = rs->OM.StencRef;
      ret.outputMerger.depthStencilState.backFace.compareMask = desc.StencilReadMask;
      ret.outputMerger.depthStencilState.backFace.writeMask = desc.StencilWriteMask;
    }
    else
    {
      ret.outputMerger.depthStencilState.depthEnable = true;
      ret.outputMerger.depthStencilState.depthFunction = CompareFunction::Less;
      ret.outputMerger.depthStencilState.depthWrites = true;
      ret.outputMerger.depthStencilState.stencilEnable = false;
      ret.outputMerger.depthStencilState.resourceId = ResourceId();

      ret.outputMerger.depthStencilState.frontFace.function = CompareFunction::AlwaysTrue;
      ret.outputMerger.depthStencilState.frontFace.depthFailOperation = StencilOperation::Keep;
      ret.outputMerger.depthStencilState.frontFace.passOperation = StencilOperation::Keep;
      ret.outputMerger.depthStencilState.frontFace.failOperation = StencilOperation::Keep;

      ret.outputMerger.depthStencilState.backFace.function = CompareFunction::AlwaysTrue;
      ret.outputMerger.depthStencilState.backFace.depthFailOperation = StencilOperation::Keep;
      ret.outputMerger.depthStencilState.backFace.passOperation = StencilOperation::Keep;
      ret.outputMerger.depthStencilState.backFace.failOperation = StencilOperation::Keep;

      // due to shared structs, this is slightly duplicated - D3D doesn't have separate states for
      // front/back.
      ret.outputMerger.depthStencilState.frontFace.reference = rs->OM.StencRef;
      ret.outputMerger.depthStencilState.frontFace.compareMask = D3D11_DEFAULT_STENCIL_READ_MASK;
      ret.outputMerger.depthStencilState.frontFace.writeMask = D3D11_DEFAULT_STENCIL_WRITE_MASK;
      ret.outputMerger.depthStencilState.backFace.reference = rs->OM.StencRef;
      ret.outputMerger.depthStencilState.backFace.compareMask = D3D11_DEFAULT_STENCIL_READ_MASK;
      ret.outputMerger.depthStencilState.backFace.writeMask = D3D11_DEFAULT_STENCIL_WRITE_MASK;
    }
  }

  /////////////////////////////////////////////////
  // Predication
  /////////////////////////////////////////////////

  ret.predication.resourceId = GetIDForDeviceChild(rs->Predicate);
  ret.predication.value = rs->PredicateValue == TRUE ? true : false;
  ret.predication.isPassing = rs->PredicationWouldPass();
}

rdcarray<Descriptor> D3D11Replay::GetDescriptors(ResourceId descriptorStore,
                                                 const rdcarray<DescriptorRange> &ranges)
{
  rdcarray<Descriptor> ret;

  if(descriptorStore != m_pImmediateContext->GetDescriptorsID())
  {
    RDCERR(
        "Descriptors query for invalid descriptor descriptorStore on fixed bindings API (D3D11)");
    return ret;
  }

  D3D11RenderState *rs = m_pDevice->GetImmediateContext()->GetCurrentPipelineState();

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  ret.resize(count);

  const D3D11RenderState::Shader *srcArr[] = {&rs->VS, &rs->HS, &rs->DS, &rs->GS, &rs->PS, &rs->CS};

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    uint32_t descriptorId = r.offset;

    for(uint32_t i = 0; i < r.count; i++, dst++, descriptorId++)
    {
      D3D11DescriptorLocation idx = DecodeD3D11DescriptorIndex(descriptorId);
      const D3D11RenderState::Shader &src = *srcArr[(uint32_t)idx.stage];

      if(idx.type == D3D11DescriptorMapping::CBs)
      {
        ret[dst].type = DescriptorType::ConstantBuffer;

        ret[dst].resource = GetIDForDeviceChild(src.ConstantBuffers[idx.idx]);
        ret[dst].byteOffset = src.CBOffsets[idx.idx] * sizeof(Vec4f);
        ret[dst].byteSize = src.CBCounts[idx.idx] * sizeof(Vec4f);
      }
      else if(idx.type == D3D11DescriptorMapping::SRVs)
      {
        ID3D11ShaderResourceView *view = src.SRVs[idx.idx];

        ret[dst].view = GetIDForDeviceChild(view);

        ret[dst].type = DescriptorType::Image;
        if(ret[dst].view != ResourceId())
        {
          D3D11_SHADER_RESOURCE_VIEW_DESC desc;
          view->GetDesc(&desc);

          ret[dst].format = MakeResourceFormat(desc.Format);

          ID3D11Resource *res = NULL;
          view->GetResource(&res);

          ret[dst].elementByteSize =
              desc.Format == DXGI_FORMAT_UNKNOWN ? 1 : GetByteSize(1, 1, 1, desc.Format, 0);

          ret[dst].resource = GetIDForDeviceChild(res);

          ret[dst].textureType = MakeTextureDim(desc.ViewDimension);

          if(desc.ViewDimension == D3D11_SRV_DIMENSION_BUFFER)
          {
            ret[dst].type = DescriptorType::TypedBuffer;

            D3D11_BUFFER_DESC bufdesc;
            ((ID3D11Buffer *)res)->GetDesc(&bufdesc);

            if(bufdesc.StructureByteStride > 0 && desc.Format == DXGI_FORMAT_UNKNOWN)
            {
              ret[dst].elementByteSize = bufdesc.StructureByteStride;
              ret[dst].type = DescriptorType::Buffer;
            }

            ret[dst].byteOffset = desc.Buffer.FirstElement * ret[dst].elementByteSize;
            ret[dst].byteSize = desc.Buffer.NumElements * ret[dst].elementByteSize;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_BUFFEREX)
          {
            ret[dst].type = DescriptorType::TypedBuffer;

            ret[dst].flags = DescriptorFlags(desc.BufferEx.Flags);

            D3D11_BUFFER_DESC bufdesc;
            ((ID3D11Buffer *)res)->GetDesc(&bufdesc);

            if(bufdesc.StructureByteStride > 0 && desc.Format == DXGI_FORMAT_UNKNOWN)
            {
              ret[dst].elementByteSize = bufdesc.StructureByteStride;
              ret[dst].type = DescriptorType::Buffer;
            }

            ret[dst].byteOffset = desc.BufferEx.FirstElement * ret[dst].elementByteSize;
            ret[dst].byteSize = desc.BufferEx.NumElements * ret[dst].elementByteSize;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE1D)
          {
            ret[dst].firstMip = desc.Texture1D.MostDetailedMip & 0xff;
            ret[dst].numMips = desc.Texture1D.MipLevels & 0xff;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE1DARRAY)
          {
            ret[dst].firstMip = desc.Texture1DArray.MostDetailedMip & 0xff;
            ret[dst].numMips = desc.Texture1DArray.MipLevels & 0xff;
            ret[dst].numSlices = desc.Texture1DArray.ArraySize & 0xffff;
            ret[dst].firstSlice = desc.Texture1DArray.FirstArraySlice & 0xffff;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE2D)
          {
            ret[dst].firstMip = desc.Texture2D.MostDetailedMip & 0xff;
            ret[dst].numMips = desc.Texture2D.MipLevels & 0xff;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE2DARRAY)
          {
            ret[dst].firstMip = desc.Texture2DArray.MostDetailedMip & 0xff;
            ret[dst].numMips = desc.Texture2DArray.MipLevels & 0xff;
            ret[dst].firstSlice = desc.Texture2DArray.FirstArraySlice & 0xffff;
            ret[dst].numSlices = desc.Texture2DArray.ArraySize & 0xffff;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE2DMS)
          {
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE2DMSARRAY)
          {
            ret[dst].numSlices = desc.Texture2DMSArray.ArraySize & 0xffff;
            ret[dst].firstSlice = desc.Texture2DMSArray.FirstArraySlice & 0xffff;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURE3D)
          {
            ret[dst].firstMip = desc.Texture3D.MostDetailedMip & 0xff;
            ret[dst].numMips = desc.Texture3D.MipLevels & 0xff;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURECUBE)
          {
            ret[dst].firstMip = desc.TextureCube.MostDetailedMip & 0xff;
            ret[dst].numMips = desc.TextureCube.MipLevels & 0xff;
            ret[dst].numSlices = 6;
          }
          else if(desc.ViewDimension == D3D11_SRV_DIMENSION_TEXTURECUBEARRAY)
          {
            ret[dst].firstMip = desc.TextureCubeArray.MostDetailedMip & 0xff;
            ret[dst].numMips = desc.TextureCubeArray.MipLevels & 0xff;
            ret[dst].firstSlice = desc.TextureCubeArray.First2DArrayFace & 0xffff;
            ret[dst].numSlices = (desc.TextureCubeArray.NumCubes * 6) & 0xffff;
          }

          SAFE_RELEASE(res);
        }
      }
      else if(idx.type == D3D11DescriptorMapping::UAVs)
      {
        ID3D11UnorderedAccessView *view = NULL;

        if(idx.stage == ShaderStage::Compute)
          view = rs->CSUAVs[idx.idx];
        else if(idx.idx >= rs->OM.UAVStartSlot)
          view = rs->OM.UAVs[idx.idx - rs->OM.UAVStartSlot];

        ret[dst].view = GetIDForDeviceChild(view);

        ret[dst].type = DescriptorType::ReadWriteImage;
        if(ret[dst].view != ResourceId())
        {
          D3D11_UNORDERED_ACCESS_VIEW_DESC desc;
          view->GetDesc(&desc);

          ID3D11Resource *res = NULL;
          view->GetResource(&res);

          ret[dst].bufferStructCount = 0;

          ret[dst].elementByteSize =
              desc.Format == DXGI_FORMAT_UNKNOWN ? 1 : GetByteSize(1, 1, 1, desc.Format, 0);

          ret[dst].textureType = MakeTextureDim(desc.ViewDimension);

          ret[dst].secondary = ResourceId();

          if(desc.ViewDimension == D3D11_UAV_DIMENSION_BUFFER &&
             (desc.Buffer.Flags & (D3D11_BUFFER_UAV_FLAG_APPEND | D3D11_BUFFER_UAV_FLAG_COUNTER)))
          {
            ret[dst].bufferStructCount = GetDebugManager()->GetStructCount(view);

            ret[dst].secondary = GetDebugManager()->GetCounterBufferID(view);
          }

          ret[dst].resource = GetIDForDeviceChild(res);

          ret[dst].format = MakeResourceFormat(desc.Format);

          if(desc.ViewDimension == D3D11_UAV_DIMENSION_BUFFER)
          {
            ret[dst].type = DescriptorType::ReadWriteBuffer;

            if(desc.Format != DXGI_FORMAT_UNKNOWN)
              ret[dst].type = DescriptorType::ReadWriteTypedBuffer;

            ret[dst].flags = DescriptorFlags(desc.Buffer.Flags);

            D3D11_BUFFER_DESC bufdesc;
            ((ID3D11Buffer *)res)->GetDesc(&bufdesc);

            if(bufdesc.StructureByteStride > 0 && desc.Format == DXGI_FORMAT_UNKNOWN)
              ret[dst].elementByteSize = bufdesc.StructureByteStride;

            ret[dst].byteOffset = desc.Buffer.FirstElement * ret[dst].elementByteSize;
            ret[dst].byteSize = desc.Buffer.NumElements * ret[dst].elementByteSize;
          }
          else if(desc.ViewDimension == D3D11_UAV_DIMENSION_TEXTURE1D)
          {
            ret[dst].firstMip = desc.Texture1D.MipSlice & 0xff;
          }
          else if(desc.ViewDimension == D3D11_UAV_DIMENSION_TEXTURE1DARRAY)
          {
            ret[dst].numSlices = desc.Texture1DArray.ArraySize & 0xffff;
            ret[dst].firstSlice = desc.Texture1DArray.FirstArraySlice & 0xffff;
            ret[dst].firstMip = desc.Texture1DArray.MipSlice & 0xff;
          }
          else if(desc.ViewDimension == D3D11_UAV_DIMENSION_TEXTURE2D)
          {
            ret[dst].firstMip = desc.Texture2D.MipSlice & 0xff;
          }
          else if(desc.ViewDimension == D3D11_UAV_DIMENSION_TEXTURE2DARRAY)
          {
            ret[dst].numSlices = desc.Texture2DArray.ArraySize & 0xffff;
            ret[dst].firstSlice = desc.Texture2DArray.FirstArraySlice & 0xffff;
            ret[dst].firstMip = desc.Texture2DArray.MipSlice & 0xff;
          }
          else if(desc.ViewDimension == D3D11_UAV_DIMENSION_TEXTURE3D)
          {
            ret[dst].numSlices = desc.Texture3D.WSize & 0xffff;
            ret[dst].firstSlice = desc.Texture3D.FirstWSlice & 0xffff;
            ret[dst].firstMip = desc.Texture3D.MipSlice & 0xff;
          }

          SAFE_RELEASE(res);
        }
      }
    }
  }

  return ret;
}

rdcarray<SamplerDescriptor> D3D11Replay::GetSamplerDescriptors(ResourceId descriptorStore,
                                                               const rdcarray<DescriptorRange> &ranges)
{
  rdcarray<SamplerDescriptor> ret;

  if(descriptorStore != m_pImmediateContext->GetDescriptorsID())
  {
    RDCERR(
        "Descriptors query for invalid descriptor descriptorStore on fixed bindings API (D3D11)");
    return ret;
  }

  D3D11RenderState *rs = m_pDevice->GetImmediateContext()->GetCurrentPipelineState();

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  ret.resize(count);

  const D3D11RenderState::Shader *srcArr[] = {&rs->VS, &rs->HS, &rs->DS, &rs->GS, &rs->PS, &rs->CS};

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    uint32_t descriptorId = r.offset;

    for(uint32_t i = 0; i < r.count; i++, dst++, descriptorId++)
    {
      D3D11DescriptorLocation idx = DecodeD3D11DescriptorIndex(descriptorId);

      if(idx.type != D3D11DescriptorMapping::Samplers)
        continue;

      ID3D11SamplerState *samp = srcArr[(uint32_t)idx.stage]->Samplers[idx.idx];

      ret[dst].type = DescriptorType::Sampler;
      ret[dst].object = GetIDForDeviceChild(samp);

      if(ret[dst].object != ResourceId())
      {
        D3D11_SAMPLER_DESC desc;
        samp->GetDesc(&desc);

        ret[dst].addressU = MakeAddressMode(desc.AddressU);
        ret[dst].addressV = MakeAddressMode(desc.AddressV);
        ret[dst].addressW = MakeAddressMode(desc.AddressW);

        ret[dst].borderColorValue.floatValue = desc.BorderColor;
        ret[dst].borderColorType = CompType::Float;

        ret[dst].compareFunction = MakeCompareFunc(desc.ComparisonFunc);
        ret[dst].filter = MakeFilter(desc.Filter);
        ret[dst].maxAnisotropy = 0;
        if(ret[dst].filter.mip == FilterMode::Anisotropic)
          ret[dst].maxAnisotropy = (float)desc.MaxAnisotropy;
        ret[dst].maxLOD = desc.MaxLOD;
        ret[dst].minLOD = desc.MinLOD;
        ret[dst].mipBias = desc.MipLODBias;
      }
    }
  }

  return ret;
}

rdcarray<DescriptorAccess> D3D11Replay::GetDescriptorAccess(uint32_t eventId)
{
  D3D11RenderState *rs = m_pDevice->GetImmediateContext()->GetCurrentPipelineState();

  rdcarray<DescriptorAccess> ret;

  const D3D11RenderState::Shader *srcArr[] = {&rs->VS, &rs->HS, &rs->DS, &rs->GS, &rs->PS, &rs->CS};

  for(size_t stage = 0; stage < ARRAY_COUNT(srcArr); stage++)
  {
    const D3D11RenderState::Shader &src = *srcArr[stage];

    WrappedShader *shad = (WrappedShader *)(WrappedID3D11Shader<ID3D11VertexShader> *)src.Object;

    if(shad)
      ret.append(shad->GetDescriptorAccess());
  }

  return ret;
}

rdcarray<DescriptorLogicalLocation> D3D11Replay::GetDescriptorLocations(
    ResourceId descriptorStore, const rdcarray<DescriptorRange> &ranges)
{
  rdcarray<DescriptorLogicalLocation> ret;

  if(descriptorStore != m_pImmediateContext->GetDescriptorsID())
  {
    RDCERR("Descriptors query for invalid descriptor store on fixed bindings API (D3D11)");
    return ret;
  }

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  ret.resize(count);

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    uint32_t descriptorByteOffset = r.offset;

    for(uint32_t i = 0; i < r.count; i++, dst++, descriptorByteOffset++)
    {
      DescriptorLogicalLocation &dstLoc = ret[dst];
      D3D11DescriptorLocation srcLoc = DecodeD3D11DescriptorIndex(descriptorByteOffset);

      dstLoc.stageMask = MaskForStage(srcLoc.stage);
      char typePrefix = '?';
      switch(srcLoc.type)
      {
        case D3D11DescriptorMapping::CBs:
          typePrefix = 'b';
          dstLoc.category = DescriptorCategory::ConstantBlock;
          break;
        case D3D11DescriptorMapping::Samplers:
          typePrefix = 's';
          dstLoc.category = DescriptorCategory::Sampler;
          break;
        case D3D11DescriptorMapping::SRVs:
          typePrefix = 't';
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case D3D11DescriptorMapping::UAVs:
          typePrefix = 'u';
          dstLoc.category = DescriptorCategory::ReadWriteResource;
          break;
        case D3D11DescriptorMapping::Count:
        case D3D11DescriptorMapping::Invalid: dstLoc.category = DescriptorCategory::Unknown; break;
      }
      dstLoc.fixedBindNumber = srcLoc.idx;

      dstLoc.logicalBindName = StringFormat::Fmt("%c%u", typePrefix, srcLoc.idx);
    }
  }

  return ret;
}

RDResult D3D11Replay::ReadLogInitialisation(RDCFile *rdc, bool storeStructuredBuffers)
{
  InvalidateLiveForward();
  m_LiveForwardCaptureScanned = m_LiveForwardCaptureSafe = false;
  m_BufferPatchFirstConsumers.clear();
  m_BufferPatchReadIndex.Clear();
  m_BufferPatchReadRegistered.clear();
  m_BufferPatchReadLastEvent = 0;
  m_CheckpointPotentialWrites.clear();
  m_CheckpointPotentialWriteCount = 0;
  m_CheckpointResidentVersions.clear();
  RDResult result = m_pDevice->ReadLogInitialisation(rdc, storeStructuredBuffers);
  // This scope proves reads of ordinary constant buffers only, not writes or output dependencies.
  // Every physical CB view is recorded without shader reflection, and copies conservatively read
  // the entire source. Deferred contexts and arbitrary resource replacement are not range proofs.
  if(m_BufferPatchReadLastEvent)
    m_BufferPatchReadIndex.FinalizeReads(1, m_BufferPatchReadLastEvent,
                                        result == ResultCode::Succeeded && !m_Proxy &&
                                            m_pDevice->GetNumDeferredContexts() == 0);
  return result;
}

void D3D11Replay::ConfigureReplayCheckpoints(
    const ReplayCheckpointConfig &config, const rdcarray<ReplayCheckpointCandidate> &candidates)
{
  InvalidateLiveForward();
  if(m_CheckpointStats.enabled && ReplayCheckpointStorageCompatible(m_CheckpointConfig, config))
  {
    for(auto it = m_Checkpoints.begin(); it != m_Checkpoints.end();)
      if(it->first == m_BufferPatchAnchorEvent ||
         ReplayCheckpointCandidateContains(candidates, it->first))
        ++it;
      else
        it = m_Checkpoints.erase(it);
    m_CheckpointConfig = config;
    m_CheckpointCandidates = candidates;
    std::sort(m_CheckpointCandidates.begin(), m_CheckpointCandidates.end(),
              [](const ReplayCheckpointCandidate &a, const ReplayCheckpointCandidate &b) {
                return a.eventId < b.eventId;
              });
    UpdateCheckpointAccounting();
    return;
  }
  m_Checkpoints.clear();
  m_CheckpointResidentVersions.clear();
  m_CheckpointResources.clear();
  m_CheckpointConfig = config;
  m_CheckpointCandidates = candidates;
  std::sort(m_CheckpointCandidates.begin(), m_CheckpointCandidates.end(),
            [](const ReplayCheckpointCandidate &a, const ReplayCheckpointCandidate &b) {
              return a.eventId < b.eventId;
            });
  m_CheckpointStats = {};
  m_CheckpointStats.supported = true;
  m_CheckpointStats.enabled = config.enabled && config.byteBudget > 0 && config.maxCheckpoints > 0;
  m_CheckpointPrepared = false;
  m_CheckpointPreparingFailed = false;
}

void D3D11Replay::InvalidateReplayCheckpoints(uint32_t firstAffectedEvent)
{
  InvalidateLiveForward();
  (void)firstAffectedEvent;
  // A replacement shader can start writing a binding that the captured shader never used.
  // Recorded GetUsage/last-write indices no longer prove immutability, even after rescanning.
  // Drop all saved prefixes and stop version reuse until the capture is reloaded.
  m_Checkpoints.clear();
  m_CheckpointResidentVersions.clear();
  m_CheckpointStats.invalidations++;
  m_CheckpointConservativeWrites = true;
  m_CheckpointPrepared = false;
  m_CheckpointPreparingFailed = false;
  m_CheckpointResources.clear();
  UpdateCheckpointAccounting();
}

void D3D11Replay::UpdateCheckpointAccounting()
{
  std::set<const CheckpointVersion *> versions;
  m_CheckpointStats.events.clear();
  m_CheckpointStats.allocatedBytes = 0;
  m_CheckpointStats.sharedBytes = 0;
  uint64_t logical = 0;
  for(const auto &entry : m_Checkpoints)
  {
    const ReplayCheckpoint &checkpoint = *entry.second;
    m_CheckpointStats.events.push_back(checkpoint.eventId);
    m_CheckpointStats.allocatedBytes += sizeof(ReplayCheckpoint) +
        checkpoint.resources.size() * sizeof(ReplayCheckpoint::Resource) +
        checkpoint.counters.size() * sizeof(ReplayCheckpoint::Counter);
    for(const ReplayCheckpoint::Resource &resource : checkpoint.resources)
      if(resource.version)
      {
        logical += resource.version->bytes;
        if(versions.insert(resource.version.get()).second)
          m_CheckpointStats.allocatedBytes += resource.version->bytes;
      }
  }
  uint64_t uniqueBytes = 0;
  for(const CheckpointVersion *version : versions)
    uniqueBytes += version->bytes;
  m_CheckpointStats.sharedBytes = logical - uniqueBytes;
  for(const CheckpointResource &resource : m_CheckpointResources)
    if(!resource.mutableContents)
      m_CheckpointStats.sharedBytes += resource.bytes;
  m_CheckpointStats.checkpointCount = (uint32_t)m_Checkpoints.size();
}

ReplayCheckpointStats D3D11Replay::GetReplayCheckpointStats()
{
  UpdateCheckpointAccounting();
  return m_CheckpointStats;
}

void D3D11Replay::EvictOldestCheckpoint()
{
  auto oldest = m_Checkpoints.end();
  for(auto it = m_Checkpoints.begin(); it != m_Checkpoints.end(); ++it)
  {
    if(it->first == m_BufferPatchAnchorEvent && m_Checkpoints.size() > 1)
      continue;
    if(oldest == m_Checkpoints.end() || it->second->lastUsed < oldest->second->lastUsed)
      oldest = it;
  }
  if(oldest != m_Checkpoints.end())
    m_Checkpoints.erase(oldest);
  UpdateCheckpointAccounting();
}

bool D3D11Replay::PrepareCheckpointResources()
{
  if(m_CheckpointPrepared || m_CheckpointPreparingFailed)
    return m_CheckpointPrepared;
  auto reject = [this](const rdcstr &reason) {
    m_CheckpointStats.reason = reason;
    m_CheckpointStats.rejected++;
    m_CheckpointPreparingFailed = true;
    m_CheckpointResources.clear();
    return false;
  };
  if(m_Proxy || m_pDevice->GetFeatureLevel() < D3D_FEATURE_LEVEL_10_1)
    return reject("D3D11 checkpoints require a local feature-level 10.1+ replay");
  if(m_pDevice->GetNumDeferredContexts() != 0)
    return reject("D3D11 deferred-context checkpoint state is not yet restorable");
  D3D11CheckpointMetadataBudget metadata(m_CheckpointConfig.byteBudget);
  // Include preparation records, candidate arrays (including growth), and temporary accounting
  // nodes before duplicating anything from the already-loaded resource/usage tables.
  if(!metadata.Add(1, sizeof(ReplayCheckpoint) + 4096) ||
     !metadata.Add(m_Resources.size(), 2 * sizeof(CheckpointResource) +
                                           2 * sizeof(ReplayCheckpoint::Resource) +
                                           2 * sizeof(ReplayCheckpoint::Counter) + 256))
    return reject("D3D11 checkpoint metadata exceeds its bounded allocation budget");
  SDFile *file = GetStructuredFile();
  if(!file)
    return reject("D3D11 checkpoint safety scan requires structured capture chunks");
  for(const SDChunk *chunk : file->chunks)
  {
    const D3D11Chunk type = D3D11Chunk(chunk->metadata.chunkID);
    if(type == D3D11Chunk::SOSetTargets || type == D3D11Chunk::DrawAuto ||
       type == D3D11Chunk::ExecuteCommandList || type == D3D11Chunk::FinishCommandList ||
       type == D3D11Chunk::SwapDeviceContextState || type == D3D11Chunk::SetShaderExtUAV)
      return reject("D3D11 capture contains stream-output, deferred/context-swap or vendor extension state");
  }
  D3D11ResourceManager *manager = m_pDevice->GetResourceManager();
  for(const ResourceDescription &description : m_Resources)
  {
    ID3D11DeviceChild *wrapped = manager->GetResource(description.resourceId, true);
    if(!wrapped)
      continue;
    const D3D11ResourceType type = IdentifyTypeByPtr(wrapped);
    // Ordinary queries/counters are not reissued by D3D11 replay. Predicate queries are reissued,
    // however, so their begin/end state and result would need a separate restorable mechanism.
    if(type == Resource_Predicate)
      return reject("D3D11 predicate query results are not yet checkpointable");
    if(type != Resource_Buffer && type != Resource_Texture1D && type != Resource_Texture2D &&
       type != Resource_Texture3D)
      continue;
    CheckpointResource resource;
    resource.id = description.resourceId;
    resource.live = (ID3D11Resource *)UnwrapResource(wrapped);
    resource.live->GetType(&resource.dimension);
    D3D11_USAGE usage = D3D11_USAGE_DEFAULT;
    UINT misc = 0;
    if(type == Resource_Buffer)
    {
      ((ID3D11Buffer *)resource.live)->GetDesc(&resource.buffer);
      usage = resource.buffer.Usage;
      misc = resource.buffer.MiscFlags;
      resource.bytes = resource.buffer.ByteWidth;
    }
    else if(type == Resource_Texture1D)
    {
      ((ID3D11Texture1D *)resource.live)->GetDesc(&resource.tex1);
      usage = resource.tex1.Usage;
      misc = resource.tex1.MiscFlags;
      for(UINT mip = 0; mip < resource.tex1.MipLevels; mip++)
        resource.bytes += uint64_t(GetByteSize(resource.tex1.Width, 1, 1, resource.tex1.Format, mip)) *
                          resource.tex1.ArraySize;
    }
    else if(type == Resource_Texture2D)
    {
      ((ID3D11Texture2D *)resource.live)->GetDesc(&resource.tex2);
      usage = resource.tex2.Usage;
      misc = resource.tex2.MiscFlags;
      for(UINT mip = 0; mip < resource.tex2.MipLevels; mip++)
        resource.bytes += uint64_t(GetByteSize(resource.tex2.Width, resource.tex2.Height, 1,
                                              resource.tex2.Format, mip)) *
                          resource.tex2.ArraySize * resource.tex2.SampleDesc.Count;
    }
    else
    {
      ((ID3D11Texture3D *)resource.live)->GetDesc(&resource.tex3);
      usage = resource.tex3.Usage;
      misc = resource.tex3.MiscFlags;
      for(UINT mip = 0; mip < resource.tex3.MipLevels; mip++)
        resource.bytes += GetByteSize(resource.tex3.Width, resource.tex3.Height, resource.tex3.Depth,
                                       resource.tex3.Format, mip);
    }
    const UINT unsupported = D3D11_RESOURCE_MISC_TILED | D3D11_RESOURCE_MISC_TILE_POOL |
                             D3D11_RESOURCE_MISC_HW_PROTECTED | D3D11_RESOURCE_MISC_RESTRICTED_CONTENT |
                             D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX |
                             D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
    if(misc & unsupported)
      return reject("D3D11 tiled, shared or protected resource contents are not checkpointable");
    resource.minLOD = (misc & D3D11_RESOURCE_MISC_RESOURCE_CLAMP) != 0;
    const rdcarray<EventUsage> *uses = m_pImmediateContext->GetCheckpointUsage(resource.id);
    if(uses)
    {
      if(!metadata.Add(uses->size(), 4 * sizeof(uint32_t)))
        return reject("D3D11 checkpoint write metadata exceeds its bounded allocation budget");
      for(const EventUsage &event : *uses)
        if(CheckpointWritesResource(event.usage))
          resource.writes.push_back(event.eventId);
    }
    // Scalar branches and replacement shaders may use any physically bound UAV. Reflection's
    // currently used slots do not bound potential writes; include the native binding envelope.
    auto potential = m_CheckpointPotentialWrites.find(resource.id);
    if(potential != m_CheckpointPotentialWrites.end())
    {
      if(!metadata.Add(potential->second.size(), 4 * sizeof(uint32_t)))
        return reject("D3D11 checkpoint write metadata exceeds its bounded allocation budget");
      resource.writes.append(potential->second);
    }
    std::sort(resource.writes.begin(), resource.writes.end());
    bool patched = false;
    for(const ReplayBufferPatch &patch : m_BufferPatches)
      patched |= patch.buffer == resource.id;
    resource.mutableContents =
        usage != D3D11_USAGE_IMMUTABLE &&
        (m_CheckpointConservativeWrites || !resource.writes.empty() || patched);
    if(resource.bytes == 0 && resource.mutableContents)
      return reject("D3D11 checkpoint resource has no reliable byte-size estimate");
    m_CheckpointResources.push_back(resource);
  }
  m_CheckpointPrepared = true;
  m_CheckpointStats.reason.clear();
  UpdateCheckpointAccounting();
  return true;
}

bool D3D11Replay::SaveReplayCheckpoint(uint32_t eventId)
{
  if(m_ReplayCheckpointPrefixOnly && !m_BufferPatches.empty() &&
     (m_BufferPatchAnchorEvent == 0 || eventId != m_BufferPatchAnchorEvent))
    return false;
  if(m_ReplayCheckpointBypass || !m_CheckpointStats.enabled || m_RealState.active ||
     !m_AppliedEventShaderOverrides.empty() || !m_AppliedEventCBufferOverrides.empty() ||
     !PrepareCheckpointResources() || m_Checkpoints.find(eventId) != m_Checkpoints.end())
    return false;
  const bool checkpointTiming = Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1";
  PerformanceTimer checkpointTimer;
  D3D11CheckpointMetadataBudget metadata(m_CheckpointConfig.byteBudget);
  if(!metadata.Add(1, sizeof(ReplayCheckpoint) + 4096) ||
     !metadata.Add(m_Resources.size(), 2 * sizeof(CheckpointResource) +
                                           2 * sizeof(ReplayCheckpoint::Resource) +
                                           2 * sizeof(ReplayCheckpoint::Counter) + 256))
  {
    m_CheckpointStats.rejected++;
    m_CheckpointStats.reason = "D3D11 checkpoint metadata exceeds its bounded allocation budget";
    return false;
  }
  for(const CheckpointResource &source : m_CheckpointResources)
    if(!metadata.Add(source.writes.size(), 4 * sizeof(uint32_t)))
    {
      m_CheckpointStats.rejected++;
      m_CheckpointStats.reason =
          "D3D11 checkpoint write metadata exceeds its bounded allocation budget";
      return false;
    }
  while(m_Checkpoints.size() >= m_CheckpointConfig.maxCheckpoints)
    EvictOldestCheckpoint();
  std::unique_ptr<ReplayCheckpoint> checkpoint(
      new ReplayCheckpoint(*m_pImmediateContext->GetCurrentPipelineState()));
  checkpoint->eventId = eventId;
  uint64_t required = sizeof(ReplayCheckpoint) +
                      m_CheckpointResources.size() * sizeof(ReplayCheckpoint::Resource);
  for(const CheckpointResource &source : m_CheckpointResources)
  {
    ReplayCheckpoint::Resource resource;
    resource.id = source.id;
    resource.live = source.live;
    resource.restoreMinLOD = source.minLOD;
    if(source.minLOD)
      resource.minLOD = m_pImmediateContext->GetReal()->GetResourceMinLOD(source.live);
    if(source.mutableContents)
    {
      const uint32_t lastWrite = CheckpointLastWrite(source.writes, eventId);
      // Only the checkpoint actually restored in this replay is a proven content version.
      // Re-executing the same write EID from the frame head does not prove byte identity.
      auto resident = m_CheckpointResidentVersions.find(source.id);
      if(!m_CheckpointConservativeWrites && resident != m_CheckpointResidentVersions.end())
      {
        std::shared_ptr<CheckpointVersion> version = resident->second.lock();
        if(version && version->lastWrite == lastWrite && version->live == source.live)
          resource.version = version;
      }
      if(!resource.version &&
         !D3D11CheckpointAddBytes(required, source.bytes, m_CheckpointConfig.byteBudget))
      {
        m_CheckpointStats.rejected++;
        m_CheckpointStats.reason = "D3D11 writable checkpoint working set exceeds its byte budget";
        return false;
      }
    }
    checkpoint->resources.push_back(resource);
  }
  D3D11ResourceManager *manager = m_pDevice->GetResourceManager();
  for(const ResourceDescription &description : m_Resources)
  {
    ID3D11DeviceChild *wrapped = manager->GetResource(description.resourceId, true);
    if(!wrapped || IdentifyTypeByPtr(wrapped) != Resource_UnorderedAccessView)
      continue;
    ID3D11UnorderedAccessView *view = (ID3D11UnorderedAccessView *)UnwrapResource(wrapped);
    D3D11_UNORDERED_ACCESS_VIEW_DESC desc = {};
    view->GetDesc(&desc);
    if(desc.ViewDimension == D3D11_UAV_DIMENSION_BUFFER &&
       (desc.Buffer.Flags & (D3D11_BUFFER_UAV_FLAG_COUNTER | D3D11_BUFFER_UAV_FLAG_APPEND)))
    {
      ReplayCheckpoint::Counter counter;
      counter.id = description.resourceId;
      counter.view = view;
      view->AddRef();
      checkpoint->counters.push_back(counter);
    }
  }
  const uint64_t counterBytes =
      checkpoint->counters.empty() ? 0 : RDCMAX(16ULL, uint64_t(checkpoint->counters.size()) * 4);
  if(counterBytes > UINT_MAX ||
     !D3D11CheckpointAddBytes(required,
                              checkpoint->counters.size() * sizeof(ReplayCheckpoint::Counter),
                              m_CheckpointConfig.byteBudget) ||
     !D3D11CheckpointAddBytes(required, counterBytes, m_CheckpointConfig.byteBudget))
  {
    m_CheckpointStats.rejected++;
    m_CheckpointStats.reason = "D3D11 checkpoint counter storage exceeds its byte budget";
    return false;
  }
  UpdateCheckpointAccounting();
  if(required > m_CheckpointConfig.byteBudget ||
     m_CheckpointStats.allocatedBytes > m_CheckpointConfig.byteBudget - required)
  {
    checkpoint.reset();
    if(!m_Checkpoints.empty())
    {
      EvictOldestCheckpoint();
      return SaveReplayCheckpoint(eventId);
    }
    m_CheckpointStats.rejected++;
    m_CheckpointStats.reason = "D3D11 writable checkpoint working set exceeds its byte budget";
    return false;
  }
  ID3D11Device *device = m_pDevice->GetReal();
  ID3D11DeviceContext *context = m_pImmediateContext->GetReal();
  for(size_t i = 0; i < m_CheckpointResources.size(); i++)
  {
    const CheckpointResource &source = m_CheckpointResources[i];
    ReplayCheckpoint::Resource &resource = checkpoint->resources[i];
    if(!source.mutableContents || resource.version)
      continue;
    std::shared_ptr<CheckpointVersion> version = std::make_shared<CheckpointVersion>();
    version->id = source.id;
    version->live = source.live;
    version->live->AddRef();
    version->bytes = source.bytes;
    version->lastWrite = CheckpointLastWrite(source.writes, eventId);
    HRESULT hr = E_FAIL;
    if(source.dimension == D3D11_RESOURCE_DIMENSION_BUFFER)
    {
      D3D11_BUFFER_DESC desc = source.buffer;
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.CPUAccessFlags = desc.BindFlags = desc.MiscFlags = desc.StructureByteStride = 0;
      ID3D11Buffer *copy = NULL;
      hr = device->CreateBuffer(&desc, NULL, &copy);
      version->copy = copy;
    }
    else if(source.dimension == D3D11_RESOURCE_DIMENSION_TEXTURE1D)
    {
      D3D11_TEXTURE1D_DESC desc = source.tex1;
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.CPUAccessFlags = desc.MiscFlags = 0;
      desc.BindFlags &= D3D11_BIND_RENDER_TARGET | D3D11_BIND_DEPTH_STENCIL;
      ID3D11Texture1D *copy = NULL;
      hr = device->CreateTexture1D(&desc, NULL, &copy);
      version->copy = copy;
    }
    else if(source.dimension == D3D11_RESOURCE_DIMENSION_TEXTURE2D)
    {
      D3D11_TEXTURE2D_DESC desc = source.tex2;
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.CPUAccessFlags = desc.MiscFlags = 0;
      desc.BindFlags &= D3D11_BIND_RENDER_TARGET | D3D11_BIND_DEPTH_STENCIL;
      ID3D11Texture2D *copy = NULL;
      hr = device->CreateTexture2D(&desc, NULL, &copy);
      version->copy = copy;
    }
    else if(source.dimension == D3D11_RESOURCE_DIMENSION_TEXTURE3D)
    {
      D3D11_TEXTURE3D_DESC desc = source.tex3;
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.CPUAccessFlags = desc.MiscFlags = 0;
      desc.BindFlags &= D3D11_BIND_RENDER_TARGET;
      ID3D11Texture3D *copy = NULL;
      hr = device->CreateTexture3D(&desc, NULL, &copy);
      version->copy = copy;
    }
    if(FAILED(hr) || !version->copy)
    {
      m_CheckpointStats.rejected++;
      m_CheckpointStats.reason = "D3D11 checkpoint resource allocation failed: " + ToStr(hr);
      return false;
    }
    context->CopyResource(version->copy, source.live);
    resource.version = version;
  }
  const double checkpointResourcesMS = checkpointTiming ? checkpointTimer.GetMilliseconds() : 0.0;
  if(!checkpoint->counters.empty())
  {
    D3D11_BUFFER_DESC desc = {};
    desc.ByteWidth = RDCMAX(16U, (UINT)checkpoint->counters.size() * 4U);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ID3D11Buffer *readback = NULL;
    HRESULT hr = device->CreateBuffer(&desc, NULL, &readback);
    if(SUCCEEDED(hr) && !readback)
      hr = E_FAIL;
    if(SUCCEEDED(hr) && readback)
    {
      for(size_t i = 0; i < checkpoint->counters.size(); i++)
        context->CopyStructureCount(readback, (UINT)i * 4U, checkpoint->counters[i].view);
      D3D11_MAPPED_SUBRESOURCE map = {};
      hr = context->Map(readback, 0, D3D11_MAP_READ, 0, &map);
      if(SUCCEEDED(hr))
      {
        for(size_t i = 0; i < checkpoint->counters.size(); i++)
          checkpoint->counters[i].value = ((const uint32_t *)map.pData)[i];
        context->Unmap(readback, 0);
      }
    }
    SAFE_RELEASE(readback);
    if(FAILED(hr))
    {
      m_CheckpointStats.rejected++;
      m_CheckpointStats.reason = "D3D11 checkpoint UAV counter readback failed: " + ToStr(hr);
      return false;
    }
  }
  if(device->GetDeviceRemovedReason() != S_OK)
    return false;
  const double checkpointCountersMS = checkpointTiming ? checkpointTimer.GetMilliseconds() : 0.0;
  checkpoint->lastUsed = ++m_CheckpointUseSerial;
  for(const ReplayCheckpoint::Resource &resource : checkpoint->resources)
    if(resource.version)
      m_CheckpointResidentVersions[resource.id] = resource.version;
  m_Checkpoints[eventId] = std::move(checkpoint);
  m_CheckpointCurrentEvent = eventId;
  m_CheckpointStats.builds++;
  m_CheckpointStats.lastSaveEvent = eventId;
  m_CheckpointStats.reason.clear();
  UpdateCheckpointAccounting();
  if(checkpointTiming)
  {
    const double totalMS = checkpointTimer.GetMilliseconds();
    const rdcstr line = StringFormat::Fmt(
        "api=D3D11Checkpoint op=save event=%u attempt=successful resource_prepare_copy_ms=%.3f "
        "counter_readback_ms=%.3f finalize_ms=%.3f total_ms=%.3f allocatedBytes=%llu sharedBytes=%llu",
        eventId, checkpointResourcesMS, checkpointCountersMS - checkpointResourcesMS,
        totalMS - checkpointCountersMS, totalMS, (unsigned long long)m_CheckpointStats.allocatedBytes,
        (unsigned long long)m_CheckpointStats.sharedBytes);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return true;
}

bool D3D11Replay::RestoreReplayCheckpoint(ReplayCheckpoint &checkpoint)
{
  if(m_ReplayCheckpointBypass || m_RealState.active ||
     !m_AppliedEventShaderOverrides.empty() || !m_AppliedEventCBufferOverrides.empty())
    return false;
  const bool checkpointTiming = Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1";
  PerformanceTimer checkpointTimer;
  D3D11ResourceManager *manager = m_pDevice->GetResourceManager();
  // Validate every identity before modifying any live resource. Resource replacements have a
  // shared invalidation hook as well, but a stale pointer must never silently copy to a new object.
  for(const ReplayCheckpoint::Resource &resource : checkpoint.resources)
    if(UnwrapResource(manager->GetResource(resource.id, true)) != resource.live)
      return false;
  for(const ReplayCheckpoint::Counter &counter : checkpoint.counters)
    if(UnwrapResource(manager->GetResource(counter.id, true)) != counter.view)
      return false;
  const double checkpointValidateMS = checkpointTiming ? checkpointTimer.GetMilliseconds() : 0.0;
  ID3D11DeviceContext *context = m_pImmediateContext->GetReal();
  m_pImmediateContext->ClearState();
  uint64_t restoredBytes = 0, retainedBytes = 0;
  for(const ReplayCheckpoint::Resource &resource : checkpoint.resources)
  {
    if(resource.version)
    {
      auto resident = m_CheckpointResidentVersions.find(resource.id);
      const bool retained =
          !m_CheckpointConservativeWrites &&
          Process::GetEnvVariable("RENDERDOC_D3D11_DISABLE_CHECKPOINT_RESIDENT_SKIP") != "1" &&
          resident != m_CheckpointResidentVersions.end() &&
          resident->second.lock() == resource.version;
      if(retained)
        retainedBytes += resource.version->bytes;
      else
      {
        context->CopyResource(resource.live, resource.version->copy);
        restoredBytes += resource.version->bytes;
      }
      m_CheckpointResidentVersions[resource.id] = resource.version;
    }
    if(resource.restoreMinLOD)
      context->SetResourceMinLOD(resource.live, resource.minLOD);
  }
  for(const ReplayCheckpoint::Counter &counter : checkpoint.counters)
  {
    const UINT count = counter.value;
    context->CSSetUnorderedAccessViews(0, 1, &counter.view, &count);
  }
  const double checkpointCopyMS = checkpointTiming ? checkpointTimer.GetMilliseconds() : 0.0;
  checkpoint.state.ApplyState(m_pImmediateContext);
  m_pImmediateContext->RestoreCheckpointEvent(checkpoint.eventId);
  m_RenderStateOM = checkpoint.state.OM;
  m_CheckpointCurrentEvent = checkpoint.eventId;
  checkpoint.lastUsed = ++m_CheckpointUseSerial;
  const bool success = m_pDevice->GetReal()->GetDeviceRemovedReason() == S_OK;
  if(checkpointTiming)
  {
    const double totalMS = checkpointTimer.GetMilliseconds();
    const rdcstr line = StringFormat::Fmt(
        "api=D3D11Checkpoint op=restore event=%u success=%u validate_ms=%.3f "
        "resource_restore_ms=%.3f "
        "bind_finalize_ms=%.3f total_ms=%.3f restoredBytes=%llu retainedBytes=%llu",
        checkpoint.eventId, success ? 1U : 0U, checkpointValidateMS,
        checkpointCopyMS - checkpointValidateMS, totalMS - checkpointCopyMS, totalMS, restoredBytes,
        retainedBytes);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return success;
}

void D3D11Replay::RecordCheckpointPotentialWrite(ResourceId resource, uint32_t eventId)
{
  if(resource == ResourceId() || eventId == 0)
    return;
  if(m_CheckpointPotentialWriteCount >= 1024 * 1024)
  {
    m_CheckpointConservativeWrites = true;
    m_CheckpointPotentialWrites.clear();
    return;
  }
  rdcarray<uint32_t> &writes = m_CheckpointPotentialWrites[resource];
  if(writes.empty() || writes.back() != eventId)
  {
    writes.push_back(eventId);
    ++m_CheckpointPotentialWriteCount;
  }
}

void D3D11Replay::ForgetCheckpointWrites(uint32_t firstEvent, uint32_t lastEvent)
{
  if(m_CheckpointConservativeWrites || firstEvent == 0)
  {
    m_CheckpointResidentVersions.clear();
    return;
  }
  if(firstEvent > lastEvent)
    return;
  for(const CheckpointResource &resource : m_CheckpointResources)
  {
    auto first = std::lower_bound(resource.writes.begin(), resource.writes.end(), firstEvent);
    if(first != resource.writes.end() && *first <= lastEvent)
      m_CheckpointResidentVersions.erase(resource.id);
  }
  // CPU overlay hooks can write at more events than captured CPU writes. They are cheap buffers;
  // never reuse a backup as the resident version without reestablishing its exact bytes.
  for(const ReplayBufferPatch &patch : m_BufferPatches)
    m_CheckpointResidentVersions.erase(patch.buffer);
}

void D3D11Replay::RecordBufferPatchConsumer(ResourceId buffer, uint32_t eventId,
                                           uint64_t byteOffset, uint64_t byteLength)
{
  // D3D11 wrappers retain their captured IDs; this manager has no separate live-ID namespace.
  if(buffer == ResourceId() || eventId == 0)
    return;
  auto previous = m_BufferPatchFirstConsumers.find(buffer);
  if(previous == m_BufferPatchFirstConsumers.end())
    m_BufferPatchFirstConsumers[buffer] = eventId;
  else
    previous->second = RDCMIN(previous->second, eventId);

  D3D11ResourceManager *manager = m_pDevice->GetResourceManager();
  ID3D11DeviceChild *resource = manager->GetResource(buffer, true);
  if(!resource)
  {
    m_BufferPatchReadIndex.RecordUnknown(eventId);
    return;
  }
  if(IdentifyTypeByPtr(resource) != Resource_Buffer)
    return;
  D3D11_BUFFER_DESC desc = {};
  ((ID3D11Buffer *)UnwrapResource(resource))->GetDesc(&desc);
  if(desc.BindFlags != D3D11_BIND_CONSTANT_BUFFER)
    return;
  const UINT shared = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX |
                      D3D11_RESOURCE_MISC_SHARED_NTHANDLE | D3D11_RESOURCE_MISC_TILED |
                      D3D11_RESOURCE_MISC_TILE_POOL;
  if(m_BufferPatchReadRegistered.insert(buffer).second)
    m_BufferPatchReadIndex.Register(buffer, 0, desc.ByteWidth, (desc.MiscFlags & shared) != 0);
  m_BufferPatchReadLastEvent = RDCMAX(m_BufferPatchReadLastEvent, eventId);
  // SetConstantBuffers1 exposes the intersection with the physical buffer; outside reads are
  // zero. The default copy-source call uses the whole-buffer sentinel, never a reflected range.
  if(byteOffset < desc.ByteWidth && byteLength)
    m_BufferPatchReadIndex.RecordRead(
        eventId, {buffer, 0, byteOffset, RDCMIN(byteLength, uint64_t(desc.ByteWidth) - byteOffset)},
        true);
}

rdcstr D3D11Replay::SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches)
{
  rdcstr error = ValidateReplayBufferPatches(patches);
  if(!error.empty())
    return error;
  if(!patches.empty() && (m_Proxy || m_pDevice->GetNumDeferredContexts() != 0))
    return "D3D11 persistent buffer edits require replay without deferred contexts";

  D3D11ResourceManager *manager = m_pDevice->GetResourceManager();
  std::map<ResourceId, D3D11_BUFFER_DESC> descriptions;
  uint64_t scratchBytes = 0;
  for(const ReplayBufferPatch &patch : patches)
  {
    ID3D11DeviceChild *wrapped = manager->GetResource(patch.buffer, true);
    if(!wrapped || IdentifyTypeByPtr(wrapped) != Resource_Buffer ||
       manager->HasReplacement(patch.buffer) ||
       manager->GetUnreplacedID(patch.buffer) != patch.buffer)
      return "D3D11 persistent edit requires an original unreplaced buffer";
    D3D11_BUFFER_DESC desc = {};
    ((ID3D11Buffer *)UnwrapResource(wrapped))->GetDesc(&desc);
    if(patch.byteOffset > desc.ByteWidth || patch.data.size() > desc.ByteWidth - patch.byteOffset)
      return "D3D11 persistent edit is outside the buffer";
    if(desc.BindFlags & (D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_STREAM_OUTPUT))
      return "D3D11 shader-writable and stream-output buffers require additional edit dependencies";
    const UINT unsupported = D3D11_RESOURCE_MISC_TILED | D3D11_RESOURCE_MISC_TILE_POOL |
                             D3D11_RESOURCE_MISC_HW_PROTECTED | D3D11_RESOURCE_MISC_RESTRICTED_CONTENT |
                             D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX |
                             D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
    if((desc.MiscFlags & unsupported) || desc.Usage == D3D11_USAGE_IMMUTABLE)
      return "D3D11 buffer lacks a restorable ordinary initial-content source";
    if(manager->GetInitialContents(patch.buffer).resource == NULL)
    {
      // Captures omit redundant initial contents for buffers that are only read.
      // Their current bytes are also their frame-start bytes, but only with a
      // complete constant-only usage proof. Preserve one GPU source before the
      // first edit; never read it back or reconstruct it from an edited value.
      const rdcarray<EventUsage> usages = GetUsage(patch.buffer);
      bool constantOnly = !usages.empty() && desc.BindFlags == D3D11_BIND_CONSTANT_BUFFER;
      for(const EventUsage &usage : usages)
        constantOnly &= usage.usage >= ResourceUsage::VS_Constants &&
                        usage.usage <= ResourceUsage::All_Constants;
      const uint64_t budget = 64ULL * 1024 * 1024;
      const uint64_t accountedBytes = (uint64_t(desc.ByteWidth) + 65535) & ~65535ULL;
      if(!constantOnly || desc.ByteWidth > 16ULL * 1024 * 1024 ||
         m_BufferPatchImmutableInitialCount >= 256 ||
         accountedBytes > budget - m_BufferPatchImmutableInitialBytes)
        return "D3D11 buffer lacks a restorable ordinary initial-content source";
      D3D11_BUFFER_DESC sourceDesc = {};
      sourceDesc.ByteWidth = desc.ByteWidth;
      sourceDesc.Usage = D3D11_USAGE_DEFAULT;
      ID3D11Buffer *source = NULL;
      HRESULT hr = m_pDevice->GetReal()->CreateBuffer(&sourceDesc, NULL, &source);
      if(FAILED(hr) || !source)
        return "D3D11 buffer lacks a restorable ordinary initial-content source";
      m_pImmediateContext->GetReal()->CopyResource(source, (ID3D11Resource *)UnwrapResource(wrapped));
      manager->SetInitialContents(patch.buffer, D3D11InitialContents(Resource_Buffer, source));
      m_BufferPatchImmutableInitialBytes += accountedBytes;
      ++m_BufferPatchImmutableInitialCount;
    }
    if(descriptions.find(patch.buffer) == descriptions.end())
      scratchBytes += desc.ByteWidth;
    if(scratchBytes > 256ULL * 1024 * 1024)
      return "D3D11 persistent buffer edit scratch storage exceeds 256 MiB";
    descriptions[patch.buffer] = desc;
  }

  std::map<ResourceId, ID3D11Buffer *> nextScratch;
  for(const auto &description : descriptions)
  {
    auto existing = m_BufferPatchScratch.find(description.first);
    if(existing != m_BufferPatchScratch.end())
    {
      existing->second->AddRef();
      nextScratch[description.first] = existing->second;
      continue;
    }
    D3D11_BUFFER_DESC desc = {};
    desc.ByteWidth = description.second.ByteWidth;
    desc.Usage = D3D11_USAGE_DEFAULT;
    ID3D11Buffer *scratch = NULL;
    HRESULT hr = m_pDevice->GetReal()->CreateBuffer(&desc, NULL, &scratch);
    if(FAILED(hr) || !scratch)
    {
      for(auto &entry : nextScratch)
        SAFE_RELEASE(entry.second);
      return StringFormat::Fmt("Could not allocate D3D11 buffer edit scratch: %s", ToStr(hr).c_str());
    }
    nextScratch[description.first] = scratch;
  }
  for(auto &scratch : m_BufferPatchScratch)
    SAFE_RELEASE(scratch.second);
  m_BufferPatchScratch.swap(nextScratch);

  const bool valueOnly = SameReplayBufferPatchLayout(m_BufferPatches, patches);
  uint32_t firstConsumer = ~0U;
  for(size_t i = 0; i < patches.size(); ++i)
  {
    auto consumer = m_BufferPatchFirstConsumers.find(patches[i].buffer);
    if(consumer == m_BufferPatchFirstConsumers.end() ||
       descriptions[patches[i].buffer].BindFlags != D3D11_BIND_CONSTANT_BUFFER)
      firstConsumer = 0;
    else
      firstConsumer = RDCMIN(firstConsumer, consumer->second);
  }
  rdcarray<ReplayBufferPatchRange> changes;
  const bool deltaKnown = GetReplayBufferPatchChanges(m_BufferPatches, patches, changes).empty();
  const bool deltaEnabled =
      Process::GetEnvVariable("RENDERDOC_REPLAY_DISABLE_BUFFER_PATCH_DELTA") != "1";
  std::set<ResourceId> changedBuffers;
  uint64_t changedBytes = 0;
  uint32_t changedConsumer = ~0U;
  bool rangeIndexKnown = false;
  for(const ReplayBufferPatchRange &change : changes)
  {
    changedBytes += change.byteLength;
    changedBuffers.insert(change.buffer);
    auto consumer = m_BufferPatchFirstConsumers.find(change.buffer);
    uint32_t first = consumer == m_BufferPatchFirstConsumers.end() ? 0U : consumer->second;
    if(valueOnly && deltaEnabled && firstConsumer > 0 && firstConsumer != ~0U)
    {
      const ReplayResourceReadQuery read = m_BufferPatchReadIndex.FirstRead(
          {change.buffer, 0, change.byteOffset, change.byteLength}, 1, m_BufferPatchReadLastEvent);
      if(read.complete && read.found)
      {
        first = read.eventId;
        rangeIndexKnown = true;
      }
    }
    changedConsumer = RDCMIN(changedConsumer, first);
  }
  const uint32_t affected = valueOnly && deltaEnabled && deltaKnown && !changes.empty() &&
                                   firstConsumer > 0 && firstConsumer != ~0U
                               ? changedConsumer
                               : firstConsumer;
  m_BufferPatchFirstAffected = valueOnly && affected != ~0U ? affected : 0;
  m_BufferPatchAnchorEvent = firstConsumer > 1 && firstConsumer != ~0U ? firstConsumer - 1 : 0;
  m_BufferPatches = patches;
  InvalidateBufferPatchContents();
  m_CheckpointResidentVersions.clear();
  if(m_BufferPatchFirstAffected)
  {
    // Same byte ranges fully cover every old overlay byte. Complete prefixes before the first
    // possible consumer have unchanged outputs, and applying the new overlay repairs their CBs.
    m_Checkpoints.erase(m_Checkpoints.lower_bound(m_BufferPatchFirstAffected), m_Checkpoints.end());
    m_CheckpointStats.invalidations++;
    m_CheckpointCurrentEvent = 0;
    UpdateCheckpointAccounting();
  }
  else
  {
    // Editing bytes does not change the command stream or the set of physically bound outputs.
    // Preserve that write proof; the general shader/resource replacement hook intentionally
    // remains more conservative. Rebuild metadata to include newly patched CPU-only buffers.
    InvalidateLiveForward();
    m_Checkpoints.clear();
    m_CheckpointStats.invalidations++;
    m_CheckpointPrepared = false;
    m_CheckpointPreparingFailed = false;
    m_CheckpointResources.clear();
    UpdateCheckpointAccounting();
  }
  ClearPostVSCache();
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D11BufferPatch op=set firstAffected=%u firstConsumer=%u anchor=%u "
        "deltaKnown=%u deltaEnabled=%u changedRanges=%zu changedBytes=%llu changedBuffers=%zu "
        "rangeIndexKnown=%u retainedCheckpoints=%zu",
        m_BufferPatchFirstAffected, firstConsumer, m_BufferPatchAnchorEvent, deltaKnown ? 1U : 0U,
        deltaEnabled ? 1U : 0U, changes.size(), (unsigned long long)changedBytes,
        changedBuffers.size(), rangeIndexKnown ? 1U : 0U, m_Checkpoints.size());
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return {};
}

bool D3D11Replay::ApplyPendingBufferPatches()
{
  if(!m_BufferPatchesDirty)
    return true;
  D3D11ResourceManager *manager = m_pDevice->GetResourceManager();
  ID3D11DeviceContext *context = m_pImmediateContext->GetReal();
  // Internal copies must not inherit captured conditional rendering.
  ID3D11Predicate *predicate = NULL;
  BOOL predicateValue = FALSE;
  context->GetPredication(&predicate, &predicateValue);
  context->SetPredication(NULL, FALSE);
  bool success = true;
  for(const auto &entry : m_BufferPatchScratch)
  {
    ID3D11DeviceChild *wrapped = manager->GetResource(entry.first, true);
    if(!wrapped || IdentifyTypeByPtr(wrapped) != Resource_Buffer ||
       manager->HasReplacement(entry.first) || manager->GetUnreplacedID(entry.first) != entry.first)
    {
      success = false;
      break;
    }
    ID3D11Buffer *target = (ID3D11Buffer *)UnwrapResource(wrapped);
    // UpdateSubresource cannot partially update a constant buffer. Patch an ordinary DEFAULT
    // scratch buffer and copy the complete contents back, preserving every unedited byte on GPU.
    context->CopyResource(entry.second, target);
    for(const ReplayBufferPatch &patch : m_BufferPatches)
    {
      if(patch.buffer != entry.first)
        continue;
      D3D11_BOX box = {(UINT)patch.byteOffset, 0, 0,
                      (UINT)(patch.byteOffset + patch.data.size()), 1, 1};
      context->UpdateSubresource(entry.second, 0, &box, patch.data.data(), 0, 0);
    }
    context->CopyResource(target, entry.second);
  }
  context->SetPredication(predicate, predicateValue);
  SAFE_RELEASE(predicate);
  if(success)
    m_BufferPatchesDirty = false;
  else
    RDCERR("A persistent D3D11 buffer edit lost its validated native resource");
  return success;
}

void D3D11Replay::InvalidateLiveForward()
{
  m_LiveForward = {};
}

bool D3D11Replay::IsLiveForwardContextSafe()
{
  if(m_Proxy || m_pDevice->GetNumDeferredContexts() || m_pDevice->HasFatalError() ||
     m_pDevice->GetReplayOptions().apiValidation || m_pImmediateContext->HasOpenReplayMaps())
    return false;
  const D3D11RenderState *state = m_pImmediateContext->GetCurrentPipelineState();
  if(!state || state->Predicate)
    return false;
  for(ID3D11Buffer *buffer : state->SO.Buffers)
    if(buffer)
      return false;
  if(!m_LiveForwardCaptureScanned)
  {
    m_LiveForwardCaptureScanned = true;
    SDFile *file = GetStructuredFile();
    if(!file)
      return false;
    // Queries, deferred streams and stream-output counters have hidden state. Do not publish
    // a seed for these captures even when the next small interval has no explicit query call.
    for(const SDChunk *chunk : file->chunks)
      if(!D3D11LiveForwardCaptureChunkSafe(D3D11Chunk(chunk->metadata.chunkID)))
        return false;
    m_LiveForwardCaptureSafe = true;
  }
  return m_LiveForwardCaptureSafe;
}

void D3D11Replay::ReplayLog(uint32_t endEventID, ReplayLogType replayType)
{
  PerformanceTimer liveTimer;
  const bool enabled = Process::GetEnvVariable("RENDERDOC_D3D11_ENABLE_LIVE_FORWARD") != "0";
  const bool unmodified = !m_RealState.active && m_EventShaderOverrides.empty() &&
                          m_AppliedEventShaderOverrides.empty() && m_EventCBufferOverrides.empty() &&
                          m_AppliedEventCBufferOverrides.empty();
  const bool ordinaryRoot = enabled && replayType == eReplay_WithoutDraw &&
                            !m_ReplayCheckpointBypass && !m_ReplayCheckpointNaturalReference &&
                            unmodified && IsLiveForwardContextSafe();
  const uint64_t entrySerial = m_pDevice->GetReplayExecutionSerial();
  if(entrySerial != m_CheckpointResidentSerial || !unmodified ||
     (m_ReplayCheckpointBypass && replayType != eReplay_OnlyDraw))
    m_CheckpointResidentVersions.clear();
  const LiveForwardCursor previous = m_LiveForward;
  // Consume before executing any command. Force/analysis and failed attempts cannot leave a
  // half-updated cursor available; native analysis callers also change entrySerial.
  InvalidateLiveForward();
  rdcstr liveReason = enabled ? "noOrdinaryCursor" : "disabled";
  bool liveForward = false;
  SDFile *structured = GetStructuredFile();
  if(ordinaryRoot && previous.valid && previous.deviceSerial == entrySerial && structured)
    liveForward = D3D11LiveForwardInterval(m_pImmediateContext->GetReplayEvents(), *structured,
                                           previous.eventId, endEventID, liveReason);
  else if(previous.valid && previous.deviceSerial != entrySerial)
    liveReason = "interveningNativeReplay";
  else if(enabled && !ordinaryRoot)
    liveReason = "bypassOrUnsafeContext";
  // A fresh replay, including force/bypass and OnlyDraw, cannot inherit version provenance.
  m_CheckpointCurrentEvent = 0;
  const bool checkpointReplay = !m_ReplayCheckpointBypass && m_CheckpointStats.enabled &&
      (replayType == eReplay_Full || replayType == eReplay_WithoutDraw) &&
      !m_RealState.active && m_AppliedEventShaderOverrides.empty() &&
      m_AppliedEventCBufferOverrides.empty() && PrepareCheckpointResources();
  uint32_t startEvent = liveForward ? previous.eventId + 1 : 0;
  const uint32_t finalEvent = replayType == eReplay_WithoutDraw ? RDCMAX(1U, endEventID) - 1 : endEventID;
  if(!liveForward && checkpointReplay && finalEvent > 0)
  {
    auto best = m_Checkpoints.upper_bound(finalEvent);
    if(best != m_Checkpoints.begin())
    {
      --best;
      if(RestoreReplayCheckpoint(*best->second))
      {
        startEvent = best->first + 1;
        m_CheckpointStats.hits++;
        m_CheckpointStats.lastResumeEvent = best->first;
      }
      else
      {
        InvalidateReplayCheckpoints();
      }
    }
    if(startEvent == 0)
      m_CheckpointStats.misses++;
    uint32_t saveEvent = 0;
    for(const ReplayCheckpointCandidate &candidate : m_CheckpointCandidates)
    {
      if(candidate.eventId == 0 || candidate.eventId > finalEvent)
        continue;
      if(candidate.eventId >= startEvent &&
         (startEvent == 0 || candidate.eventId - (startEvent - 1) >= m_CheckpointConfig.minEventGap) &&
         m_Checkpoints.find(candidate.eventId) == m_Checkpoints.end())
        saveEvent = candidate.eventId;
    }
    // Keep one complete edit-start state instead of repeatedly replacing it with a late
    // checkpoint whose outputs depend on the edited value. Budget failure remains a fallback.
    if(m_BufferPatchAnchorEvent >= startEvent && m_BufferPatchAnchorEvent > 0 &&
       m_BufferPatchAnchorEvent <= finalEvent &&
       m_Checkpoints.find(m_BufferPatchAnchorEvent) == m_Checkpoints.end())
      saveEvent = m_BufferPatchAnchorEvent;
    // A live parameter edit must not pay to recreate its invalidated downstream outputs.
    // Ordinary navigation and idle maintenance may build those points under the same policy.
    if(m_ReplayCheckpointPrefixOnly && !m_BufferPatches.empty() &&
       saveEvent != m_BufferPatchAnchorEvent)
      saveEvent = 0;
    if(saveEvent > 0)
    {
      ForgetCheckpointWrites(startEvent, saveEvent);
      if(!m_pDevice->ReplayLog(startEvent, saveEvent, eReplay_Full))
      {
        m_CheckpointResidentVersions.clear();
        return;
      }
      SaveReplayCheckpoint(saveEvent);
      startEvent = saveEvent + 1;
    }
  }
  bool success = true;
  if(startEvent == 0 || startEvent <= finalEvent || replayType == eReplay_OnlyDraw)
  {
    ForgetCheckpointWrites(replayType == eReplay_OnlyDraw ? endEventID : startEvent, finalEvent);
    success = m_pDevice->ReplayLog(startEvent, endEventID, replayType);
  }
  // Save/restore is exclusively on complete prefix states. OnlyDraw may use a temporary edited
  // shader/CBuffer and therefore never creates a reusable suffix checkpoint.
  if(replayType == eReplay_WithoutDraw || replayType == eReplay_Full)
    m_RenderStateOM = m_pDevice->GetImmediateContext()->GetCurrentPipelineState()->OM;

  const bool safeResult = success && !m_pDevice->HasFatalError();
  if(!safeResult)
    m_CheckpointResidentVersions.clear();
  m_CheckpointResidentSerial = m_pDevice->GetReplayExecutionSerial();
  if(ordinaryRoot && safeResult && IsLiveForwardContextSafe())
  {
    const rdcarray<APIEvent> &events = m_pImmediateContext->GetReplayEvents();
    if(structured && endEventID < events.size() && events[endEventID].eventId == endEventID &&
       events[endEventID].chunkIndex < structured->chunks.size() &&
       D3D11LiveForwardAction(D3D11Chunk(
           structured->chunks[events[endEventID].chunkIndex]->metadata.chunkID)))
    {
      m_LiveForward.pendingTarget = endEventID;
      m_LiveForward.pendingDeviceSerial = m_pDevice->GetReplayExecutionSerial();
    }
  }
  // A target-only call cannot establish a cursor on its own. Publication requires its preceding
  // ordinary root and unchanged native execution serial, including across bypassed references.
  else if(D3D11LiveForwardCanPublish(endEventID, previous.pendingTarget, entrySerial,
                                    previous.pendingDeviceSerial, safeResult,
                                    enabled && unmodified && replayType == eReplay_OnlyDraw) &&
          IsLiveForwardContextSafe())
  {
    m_LiveForward.valid = true;
    m_LiveForward.eventId = endEventID;
    m_LiveForward.deviceSerial = m_pDevice->GetReplayExecutionSerial();
  }
  if(replayType == eReplay_WithoutDraw && Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D11LiveForward from=%u target=%u enabled=%u hit=%u initialRestoreSkipped=%u "
        "replayedEvents=%u range=%u-%u success=%u reason=%s total_ms=%.3f",
        previous.eventId, endEventID, enabled ? 1U : 0U, liveForward && safeResult ? 1U : 0U,
        liveForward && safeResult ? 1U : 0U,
        liveForward ? endEventID - previous.eventId - 1 : 0,
        liveForward ? previous.eventId + 1 : 0, finalEvent, safeResult ? 1U : 0U,
        liveReason.c_str(), liveTimer.GetMilliseconds());
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

SDFile *D3D11Replay::GetStructuredFile()
{
  return m_pDevice->GetStructuredFile();
}

rdcarray<uint32_t> D3D11Replay::GetPassEvents(uint32_t eventId)
{
  rdcarray<uint32_t> passEvents;

  const ActionDescription *action = m_pDevice->GetAction(eventId);

  const ActionDescription *start = action;
  while(start && start->previousAction && !(start->previousAction->flags & ActionFlags::Clear))
  {
    const ActionDescription *prev = start->previousAction;

    if(start->outputs != prev->outputs || start->depthOut != prev->depthOut)
      break;

    start = prev;
  }

  while(start)
  {
    if(start == action)
      break;

    if(start->flags & ActionFlags::Drawcall)
      passEvents.push_back(start->eventId);

    start = start->nextAction;
  }

  return passEvents;
}

void D3D11Replay::PickPixel(ResourceId texture, uint32_t x, uint32_t y, const Subresource &sub,
                            CompType typeCast, float pixel[4])
{
  D3D11RenderStateTracker tracker(m_pImmediateContext);

  D3D11MarkerRegion marker("PickPixel");

  m_pImmediateContext->OMSetRenderTargets(1, &m_PixelPick.RTV, NULL);

  float color[4] = {0.0f, 0.0f, 0.0f, 0.0f};

  m_pImmediateContext->ClearRenderTargetView(m_PixelPick.RTV, color);

  D3D11_VIEWPORT viewport;
  RDCEraseEl(viewport);

  SetOutputDimensions(100, 100);

  viewport.TopLeftX = 0;
  viewport.TopLeftY = 0;
  viewport.Width = 100;
  viewport.Height = 100;

  m_pImmediateContext->RSSetViewports(1, &viewport);

  {
    TextureDisplay texDisplay;

    texDisplay.red = texDisplay.green = texDisplay.blue = texDisplay.alpha = true;
    texDisplay.hdrMultiplier = -1.0f;
    texDisplay.linearDisplayAsGamma = true;
    texDisplay.flipY = false;
    texDisplay.subresource = sub;
    texDisplay.customShaderId = ResourceId();
    texDisplay.rangeMin = 0.0f;
    texDisplay.rangeMax = 1.0f;
    texDisplay.scale = 1.0f;
    texDisplay.resourceId = texture;
    texDisplay.typeCast = typeCast;
    texDisplay.rawOutput = true;

    uint32_t texWidth = 1, texHeight = 1;

    auto it1 = WrappedID3D11Texture1D::m_TextureList.find(texture);
    auto it2 = WrappedID3D11Texture2D1::m_TextureList.find(texture);
    auto it3 = WrappedID3D11Texture3D1::m_TextureList.find(texture);
    if(it1 != WrappedID3D11Texture1D::m_TextureList.end())
    {
      WrappedID3D11Texture1D *wrapTex1D = (WrappedID3D11Texture1D *)it1->second.m_Texture;

      D3D11_TEXTURE1D_DESC desc1d = {0};
      wrapTex1D->GetDesc(&desc1d);

      texWidth = desc1d.Width;
    }
    else if(it2 != WrappedID3D11Texture2D1::m_TextureList.end())
    {
      WrappedID3D11Texture2D1 *wrapTex2D = (WrappedID3D11Texture2D1 *)it2->second.m_Texture;

      D3D11_TEXTURE2D_DESC desc2d = {0};
      wrapTex2D->GetDesc(&desc2d);

      texWidth = desc2d.Width;
      texHeight = desc2d.Height;
    }
    else if(it3 != WrappedID3D11Texture3D1::m_TextureList.end())
    {
      WrappedID3D11Texture3D1 *wrapTex3D = (WrappedID3D11Texture3D1 *)it3->second.m_Texture;

      D3D11_TEXTURE3D_DESC desc3d = {0};
      wrapTex3D->GetDesc(&desc3d);

      texWidth = desc3d.Width;
      texHeight = desc3d.Height;
    }

    uint32_t mipWidth = RDCMAX(1U, texWidth >> sub.mip);
    uint32_t mipHeight = RDCMAX(1U, texHeight >> sub.mip);

    texDisplay.xOffset = -(float(x) / float(mipWidth)) * texWidth;
    texDisplay.yOffset = -(float(y) / float(mipHeight)) * texHeight;

    RenderTextureInternal(texDisplay, eTexDisplay_None);
  }

  D3D11_BOX box;
  box.front = 0;
  box.back = 1;
  box.left = 0;
  box.right = 1;
  box.top = 0;
  box.bottom = 1;

  m_pImmediateContext->CopySubresourceRegion(m_PixelPick.StageTexture, 0, 0, 0, 0,
                                             m_PixelPick.Texture, 0, &box);

  D3D11_MAPPED_SUBRESOURCE mapped;
  mapped.pData = NULL;
  HRESULT hr = m_pImmediateContext->Map(m_PixelPick.StageTexture, 0, D3D11_MAP_READ, 0, &mapped);

  m_pDevice->CheckHRESULT(hr);

  if(FAILED(hr))
  {
    RDCERR("Failed to map stage buff HRESULT: %s", ToStr(hr).c_str());
  }

  float *pix = (float *)mapped.pData;

  if(pix == NULL)
  {
    RDCERR("Failed to map pick-pixel staging texture.");
  }
  else
  {
    pixel[0] = pix[0];
    pixel[1] = pix[1];
    pixel[2] = pix[2];
    pixel[3] = pix[3];
  }

  m_pImmediateContext->Unmap(m_PixelPick.StageTexture, 0);
}

bool D3D11Replay::GetMinMax(ResourceId texid, const Subresource &sub, CompType typeCast,
                            float *minval, float *maxval)
{
  TextureShaderDetails details = GetDebugManager()->GetShaderDetails(texid, typeCast, true);

  if(details.texFmt == DXGI_FORMAT_UNKNOWN)
    return false;

  D3D11RenderStateTracker tracker(m_pImmediateContext);

  HistogramCBufferData cdata;
  cdata.HistogramTextureResolution.x = (float)RDCMAX(details.texWidth >> sub.mip, 1U);
  cdata.HistogramTextureResolution.y = (float)RDCMAX(details.texHeight >> sub.mip, 1U);
  cdata.HistogramTextureResolution.z = (float)RDCMAX(details.texDepth >> sub.mip, 1U);
  if(details.texType == eTexType_3D)
    cdata.HistogramSlice = (float)RDCCLAMP(sub.slice, 0U, (details.texDepth >> sub.mip) - 1);
  else
    cdata.HistogramSlice = (float)RDCCLAMP(sub.slice, 0U, details.texArraySize - 1);
  cdata.HistogramMip = sub.mip;
  cdata.HistogramSample = (int)RDCCLAMP(sub.sample, 0U, details.sampleCount - 1);
  if(sub.sample == ~0U)
    cdata.HistogramSample = -int(details.sampleCount);
  cdata.HistogramMin = 0.0f;
  cdata.HistogramMax = 1.0f;
  cdata.HistogramChannels = 0xf;
  cdata.HistogramFlags = 0;

  Vec4u YUVDownsampleRate = {};
  Vec4u YUVAChannels = {};

  GetYUVShaderParameters(details.texFmt, YUVDownsampleRate, YUVAChannels);

  cdata.HistogramYUVDownsampleRate = YUVDownsampleRate;
  cdata.HistogramYUVAChannels = YUVAChannels;

  int srvOffset = 0;
  int intIdx = 0;

  DXGI_FORMAT fmt = GetTypedFormat(details.texFmt, typeCast);

  if(IsUIntFormat(fmt))
  {
    cdata.HistogramFlags |= TEXDISPLAY_UINT_TEX;
    srvOffset = 10;
    intIdx = 1;
  }
  if(IsIntFormat(fmt))
  {
    cdata.HistogramFlags |= TEXDISPLAY_SINT_TEX;
    srvOffset = 20;
    intIdx = 2;
  }

  ID3D11Buffer *cbuf = GetDebugManager()->MakeCBuffer(&cdata, sizeof(cdata));

  m_pImmediateContext->OMSetRenderTargetsAndUnorderedAccessViews(0, NULL, NULL, 0, 0, NULL, NULL);

  m_pImmediateContext->CSSetConstantBuffers(0, 1, &cbuf);

  ID3D11UnorderedAccessView *uavs[D3D11_1_UAV_SLOT_COUNT] = {NULL};
  const UINT numUAVs =
      m_pImmediateContext->IsFL11_1() ? D3D11_1_UAV_SLOT_COUNT : D3D11_PS_CS_UAV_REGISTER_COUNT;
  uavs[intIdx] = m_Histogram.TileResultUAV[intIdx];
  m_pImmediateContext->CSSetUnorderedAccessViews(0, numUAVs, uavs, NULL);

  m_pImmediateContext->CSSetShaderResources(srvOffset, eTexType_Max, details.srv);

  m_pImmediateContext->CSSetShader(m_Histogram.TileMinMaxCS[details.texType][intIdx], NULL, 0);

  int blocksX = (int)ceil(cdata.HistogramTextureResolution.x /
                          float(HGRAM_PIXELS_PER_TILE * HGRAM_TILES_PER_BLOCK));
  int blocksY = (int)ceil(cdata.HistogramTextureResolution.y /
                          float(HGRAM_PIXELS_PER_TILE * HGRAM_TILES_PER_BLOCK));

  m_pImmediateContext->Dispatch(blocksX, blocksY, 1);

  m_pImmediateContext->CSSetUnorderedAccessViews(intIdx, 1, &m_Histogram.ResultUAV[intIdx], NULL);
  m_pImmediateContext->CSSetShaderResources(intIdx, 1, &m_Histogram.TileResultSRV[intIdx]);

  m_pImmediateContext->CSSetShader(m_Histogram.ResultMinMaxCS[intIdx], NULL, 0);

  m_pImmediateContext->Dispatch(1, 1, 1);

  m_pImmediateContext->CopyResource(m_Histogram.ResultStageBuff, m_Histogram.ResultBuff);

  D3D11_MAPPED_SUBRESOURCE mapped;

  HRESULT hr = m_pImmediateContext->Map(m_Histogram.ResultStageBuff, 0, D3D11_MAP_READ, 0, &mapped);

  m_pDevice->CheckHRESULT(hr);

  if(FAILED(hr))
  {
    RDCERR("Failed to map minmax results buffer HRESULT: %s", ToStr(hr).c_str());
  }
  else
  {
    Vec4f *minmax = (Vec4f *)mapped.pData;

    minval[0] = minmax[0].x;
    minval[1] = minmax[0].y;
    minval[2] = minmax[0].z;
    minval[3] = minmax[0].w;

    maxval[0] = minmax[1].x;
    maxval[1] = minmax[1].y;
    maxval[2] = minmax[1].z;
    maxval[3] = minmax[1].w;

    m_pImmediateContext->Unmap(m_Histogram.ResultStageBuff, 0);
  }

  return true;
}

bool D3D11Replay::GetHistogram(ResourceId texid, const Subresource &sub, CompType typeCast,
                               float minval, float maxval, const rdcfixedarray<bool, 4> &channels,
                               rdcarray<uint32_t> &histogram)
{
  if(minval >= maxval)
    return false;

  TextureShaderDetails details = GetDebugManager()->GetShaderDetails(texid, typeCast, true);

  if(details.texFmt == DXGI_FORMAT_UNKNOWN)
    return false;

  D3D11RenderStateTracker tracker(m_pImmediateContext);

  HistogramCBufferData cdata;
  cdata.HistogramTextureResolution.x = (float)RDCMAX(details.texWidth >> sub.mip, 1U);
  cdata.HistogramTextureResolution.y = (float)RDCMAX(details.texHeight >> sub.mip, 1U);
  cdata.HistogramTextureResolution.z = (float)RDCMAX(details.texDepth >> sub.mip, 1U);
  if(details.texType == eTexType_3D)
    cdata.HistogramSlice = (float)RDCCLAMP(sub.slice, 0U, (details.texDepth >> sub.mip) - 1);
  else
    cdata.HistogramSlice = (float)RDCCLAMP(sub.slice, 0U, details.texArraySize - 1);
  cdata.HistogramMip = sub.mip;
  cdata.HistogramSample = (int)RDCCLAMP(sub.sample, 0U, details.sampleCount - 1);
  if(sub.sample == ~0U)
    cdata.HistogramSample = -int(details.sampleCount);
  cdata.HistogramMin = minval;

  // The calculation in the shader normalises each value between min and max, then multiplies by the
  // number of buckets.
  // But any value equal to HistogramMax must go into NUM_BUCKETS-1, so add a small delta.
  cdata.HistogramMax = maxval + maxval * 1e-6f;

  cdata.HistogramChannels = 0;
  if(channels[0])
    cdata.HistogramChannels |= 0x1;
  if(channels[1])
    cdata.HistogramChannels |= 0x2;
  if(channels[2])
    cdata.HistogramChannels |= 0x4;
  if(channels[3])
    cdata.HistogramChannels |= 0x8;
  cdata.HistogramFlags = 0;

  Vec4u YUVDownsampleRate = {};
  Vec4u YUVAChannels = {};

  GetYUVShaderParameters(details.texFmt, YUVDownsampleRate, YUVAChannels);

  cdata.HistogramYUVDownsampleRate = YUVDownsampleRate;
  cdata.HistogramYUVAChannels = YUVAChannels;

  int srvOffset = 0;
  int intIdx = 0;

  DXGI_FORMAT fmt = GetTypedFormat(details.texFmt, typeCast);

  if(IsUIntFormat(fmt))
  {
    cdata.HistogramFlags |= TEXDISPLAY_UINT_TEX;
    srvOffset = 10;
    intIdx = 1;
  }
  if(IsIntFormat(fmt))
  {
    cdata.HistogramFlags |= TEXDISPLAY_SINT_TEX;
    srvOffset = 20;
    intIdx = 2;
  }

  if(details.texType == eTexType_3D)
    cdata.HistogramSlice = float(sub.slice);

  ID3D11Buffer *cbuf = GetDebugManager()->MakeCBuffer(&cdata, sizeof(cdata));

  UINT zeroes[] = {0, 0, 0, 0};
  m_pImmediateContext->ClearUnorderedAccessViewUint(m_Histogram.HistogramUAV, zeroes);

  m_pImmediateContext->OMSetRenderTargetsAndUnorderedAccessViews(0, NULL, NULL, 0, 0, NULL, NULL);

  ID3D11UnorderedAccessView *uavs[D3D11_1_UAV_SLOT_COUNT] = {0};
  UINT UAV_keepcounts[D3D11_1_UAV_SLOT_COUNT];
  memset(&UAV_keepcounts[0], 0xff, sizeof(UAV_keepcounts));

  const UINT numUAVs =
      m_pImmediateContext->IsFL11_1() ? D3D11_1_UAV_SLOT_COUNT : D3D11_PS_CS_UAV_REGISTER_COUNT;
  uavs[0] = m_Histogram.HistogramUAV;
  m_pImmediateContext->CSSetUnorderedAccessViews(0, numUAVs, uavs, UAV_keepcounts);

  m_pImmediateContext->CSSetConstantBuffers(0, 1, &cbuf);

  m_pImmediateContext->CSSetShaderResources(srvOffset, eTexType_Max, details.srv);

  m_pImmediateContext->CSSetShader(m_Histogram.HistogramCS[details.texType][intIdx], NULL, 0);

  int tilesX = (int)ceil(cdata.HistogramTextureResolution.x /
                         float(HGRAM_PIXELS_PER_TILE * HGRAM_TILES_PER_BLOCK));
  int tilesY = (int)ceil(cdata.HistogramTextureResolution.y /
                         float(HGRAM_PIXELS_PER_TILE * HGRAM_TILES_PER_BLOCK));

  m_pImmediateContext->Dispatch(tilesX, tilesY, 1);

  m_pImmediateContext->CopyResource(m_Histogram.ResultStageBuff, m_Histogram.ResultBuff);

  D3D11_MAPPED_SUBRESOURCE mapped;

  HRESULT hr = m_pImmediateContext->Map(m_Histogram.ResultStageBuff, 0, D3D11_MAP_READ, 0, &mapped);

  m_pDevice->CheckHRESULT(hr);

  histogram.clear();
  histogram.resize(HGRAM_NUM_BUCKETS);

  if(FAILED(hr))
  {
    RDCERR("Can't map histogram stage buff HRESULT: %s", ToStr(hr).c_str());
  }
  else
  {
    memcpy(&histogram[0], mapped.pData, sizeof(uint32_t) * HGRAM_NUM_BUCKETS);

    m_pImmediateContext->Unmap(m_Histogram.ResultStageBuff, 0);
  }

  return true;
}

void D3D11Replay::GetBufferData(ResourceId buff, uint64_t offset, uint64_t length, bytebuf &retData)
{
  ID3D11UnorderedAccessView *counterUAV = GetDebugManager()->GetCounterBufferUAV(buff);
  if(counterUAV)
  {
    uint32_t count = GetDebugManager()->GetStructCount(counterUAV);

    // copy the uint first
    retData.resize(4U);
    memcpy(retData.data(), &count, retData.size());

    // remove offset bytes, up to 4
    retData.erase(0, (size_t)RDCMIN(4ULL, offset));
    return;
  }

  auto it = WrappedID3D11Buffer::m_BufferList.find(buff);

  if(it == WrappedID3D11Buffer::m_BufferList.end())
  {
    RDCERR("Getting buffer data for unknown buffer %s!", ToStr(buff).c_str());
    return;
  }

  ID3D11Buffer *buffer = it->second.m_Buffer;

  RDCASSERT(buffer);

  GetDebugManager()->GetBufferData(buffer, offset, length, retData);
}

void D3D11Replay::GetTextureData(ResourceId tex, const Subresource &sub,
                                 const GetTextureDataParams &params, bytebuf &data)
{
  D3D11RenderStateTracker tracker(m_pImmediateContext);

  ID3D11Resource *dummyTex = NULL;

  uint32_t subresource = 0;
  uint32_t mips = 0;

  Subresource s = sub;

  size_t bytesize = 0;

  if(WrappedID3D11Texture1D::m_TextureList.find(tex) != WrappedID3D11Texture1D::m_TextureList.end())
  {
    WrappedID3D11Texture1D *wrapTex =
        (WrappedID3D11Texture1D *)WrappedID3D11Texture1D::m_TextureList[tex].m_Texture;

    D3D11_TEXTURE1D_DESC desc = {0};
    wrapTex->GetDesc(&desc);

    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;
    desc.Usage = D3D11_USAGE_STAGING;

    ID3D11Texture1D *d = NULL;

    mips = desc.MipLevels ? desc.MipLevels : CalcNumMips(desc.Width, 1, 1);

    s.mip = RDCMIN(mips - 1, s.mip);
    s.slice = RDCMIN(desc.ArraySize - 1, s.slice);

    if(params.remap != RemapTexture::NoRemap)
    {
      if(params.remap == RemapTexture::RGBA8)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R8G8B8A8_TYPELESS, BaseRemapType(params));
        if(IsSRGBFormat(desc.Format) && params.typeCast == CompType::Typeless)
          desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
      }
      else if(params.remap == RemapTexture::RGBA16)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R16G16B16A16_TYPELESS, BaseRemapType(params));
      }
      else if(params.remap == RemapTexture::RGBA32)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R32G32B32A32_TYPELESS, BaseRemapType(params));
      }

      desc.ArraySize = 1;
    }

    subresource = s.slice * mips + s.mip;

    HRESULT hr = m_pDevice->CreateTexture1D(&desc, NULL, &d);

    dummyTex = d;

    if(FAILED(hr))
    {
      RDCERR("Couldn't create staging texture to retrieve data. HRESULT: %s", ToStr(hr).c_str());
      return;
    }

    bytesize = GetByteSize(desc.Width, 1, 1, desc.Format, s.mip);

    if(params.remap != RemapTexture::NoRemap)
    {
      subresource = s.mip;

      desc.CPUAccessFlags = 0;
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.BindFlags = D3D11_BIND_RENDER_TARGET;

      ID3D11Texture1D *rtTex = NULL;

      hr = m_pDevice->CreateTexture1D(&desc, NULL, &rtTex);

      if(FAILED(hr))
      {
        RDCERR("Couldn't create target texture to downcast texture. HRESULT: %s", ToStr(hr).c_str());
        SAFE_RELEASE(d);
        return;
      }

      D3D11_RENDER_TARGET_VIEW_DESC rtvDesc;
      rtvDesc.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE1D;
      rtvDesc.Format = desc.Format;
      rtvDesc.Texture1D.MipSlice = s.mip;

      ID3D11RenderTargetView *wrappedrtv = NULL;
      hr = m_pDevice->CreateRenderTargetView(rtTex, &rtvDesc, &wrappedrtv);
      if(FAILED(hr))
      {
        RDCERR("Couldn't create target rtv to downcast texture. HRESULT: %s", ToStr(hr).c_str());
        SAFE_RELEASE(d);
        SAFE_RELEASE(rtTex);
        return;
      }

      ID3D11RenderTargetView *rtv = wrappedrtv;

      m_pImmediateContext->OMSetRenderTargets(1, &rtv, NULL);
      float color[4] = {0.0f, 0.0f, 0.0f, 0.0f};
      m_pImmediateContext->ClearRenderTargetView(rtv, color);

      D3D11_VIEWPORT viewport = {
          0, 0, (float)desc.Width, 1.0f, 0.0f, 1.0f,
      };
      m_pImmediateContext->RSSetViewports(1, &viewport);
      SetOutputDimensions(desc.Width, 1);

      TexDisplayFlags flags = eTexDisplay_None;

      if(IsUIntFormat(desc.Format))
        flags = eTexDisplay_RemapUInt;
      else if(IsIntFormat(desc.Format))
        flags = eTexDisplay_RemapSInt;
      else
        flags = eTexDisplay_RemapFloat;

      {
        TextureDisplay texDisplay;

        texDisplay.red = texDisplay.green = texDisplay.blue = texDisplay.alpha = true;
        texDisplay.hdrMultiplier = -1.0f;
        texDisplay.linearDisplayAsGamma = false;
        texDisplay.overlay = DebugOverlay::NoOverlay;
        texDisplay.flipY = false;
        texDisplay.subresource = s;
        texDisplay.subresource.sample = 0;
        texDisplay.customShaderId = ResourceId();
        texDisplay.rangeMin = params.blackPoint;
        texDisplay.rangeMax = params.whitePoint;
        texDisplay.resourceId = tex;
        texDisplay.typeCast = params.typeCast;
        texDisplay.rawOutput = false;
        texDisplay.xOffset = 0;
        texDisplay.yOffset = 0;

        // we scale our texture rendering by output dimension. To counteract that, add a manual
        // scale here
        texDisplay.scale = 1.0f / float(1 << s.mip);

        RenderTextureInternal(texDisplay, flags);
      }

      m_pImmediateContext->CopyResource(d, rtTex);
      SAFE_RELEASE(rtTex);

      SAFE_RELEASE(wrappedrtv);
    }
    else
    {
      m_pImmediateContext->CopyResource(d, wrapTex);
    }
  }
  else if(WrappedID3D11Texture2D1::m_TextureList.find(tex) !=
          WrappedID3D11Texture2D1::m_TextureList.end())
  {
    WrappedID3D11Texture2D1 *wrapTex =
        (WrappedID3D11Texture2D1 *)WrappedID3D11Texture2D1::m_TextureList[tex].m_Texture;

    D3D11_TEXTURE2D_DESC desc = {0};
    wrapTex->GetDesc(&desc);

    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;
    desc.Usage = D3D11_USAGE_STAGING;

    bool wasms = false;

    UINT sampleCount = desc.SampleDesc.Count;

    s.mip = RDCMIN(mips - 1, s.mip);
    s.slice = RDCMIN(desc.ArraySize - 1, s.slice);
    s.sample = RDCMIN(sampleCount - 1, s.sample);

    if(desc.SampleDesc.Count > 1)
    {
      desc.ArraySize *= desc.SampleDesc.Count;
      desc.SampleDesc.Count = 1;
      desc.SampleDesc.Quality = 0;

      wasms = true;
    }

    ID3D11Texture2D *d = NULL;

    mips = desc.MipLevels ? desc.MipLevels : CalcNumMips(desc.Width, desc.Height, 1);

    if(params.remap != RemapTexture::NoRemap)
    {
      if(params.remap == RemapTexture::RGBA8)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R8G8B8A8_TYPELESS, BaseRemapType(params));
        if((IsSRGBFormat(desc.Format) || wrapTex->m_RealDescriptor) &&
           params.typeCast == CompType::Typeless)
          desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
      }
      else if(params.remap == RemapTexture::RGBA16)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R16G16B16A16_TYPELESS, BaseRemapType(params));
      }
      else if(params.remap == RemapTexture::RGBA32)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R32G32B32A32_TYPELESS, BaseRemapType(params));
      }

      desc.ArraySize = 1;
    }

    subresource = s.slice * mips + s.mip;

    HRESULT hr = m_pDevice->CreateTexture2D(&desc, NULL, &d);

    dummyTex = d;

    if(FAILED(hr))
    {
      RDCERR("Couldn't create staging texture to retrieve data. HRESULT: %s", ToStr(hr).c_str());
      return;
    }

    bytesize = GetByteSize(desc.Width, desc.Height, 1, desc.Format, s.mip);

    if(params.remap != RemapTexture::NoRemap)
    {
      subresource = s.mip;

      desc.CPUAccessFlags = 0;
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.BindFlags = D3D11_BIND_RENDER_TARGET;

      ID3D11Texture2D *rtTex = NULL;

      hr = m_pDevice->CreateTexture2D(&desc, NULL, &rtTex);

      if(FAILED(hr))
      {
        RDCERR("Couldn't create target texture to downcast texture. HRESULT: %s", ToStr(hr).c_str());
        SAFE_RELEASE(d);
        return;
      }

      D3D11_RENDER_TARGET_VIEW_DESC rtvDesc;
      rtvDesc.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
      rtvDesc.Format = desc.Format;
      rtvDesc.Texture2D.MipSlice = s.mip;

      ID3D11RenderTargetView *wrappedrtv = NULL;
      hr = m_pDevice->CreateRenderTargetView(rtTex, &rtvDesc, &wrappedrtv);
      if(FAILED(hr))
      {
        RDCERR("Couldn't create target rtv to downcast texture. HRESULT: %s", ToStr(hr).c_str());
        SAFE_RELEASE(d);
        SAFE_RELEASE(rtTex);
        return;
      }

      ID3D11RenderTargetView *rtv = wrappedrtv;

      m_pImmediateContext->OMSetRenderTargets(1, &rtv, NULL);
      float color[4] = {0.0f, 0.0f, 0.0f, 0.0f};
      m_pImmediateContext->ClearRenderTargetView(rtv, color);

      D3D11_VIEWPORT viewport = {
          0, 0, (float)desc.Width, (float)desc.Height, 0.0f, 1.0f,
      };

      SetOutputDimensions(desc.Width, desc.Height);
      m_pImmediateContext->RSSetViewports(1, &viewport);

      TexDisplayFlags flags = eTexDisplay_None;

      if(IsUIntFormat(desc.Format))
        flags = eTexDisplay_RemapUInt;
      else if(IsIntFormat(desc.Format))
        flags = eTexDisplay_RemapSInt;
      else
        flags = eTexDisplay_RemapFloat;

      {
        TextureDisplay texDisplay;

        texDisplay.red = texDisplay.green = texDisplay.blue = texDisplay.alpha = true;
        texDisplay.hdrMultiplier = -1.0f;
        texDisplay.linearDisplayAsGamma = false;
        texDisplay.overlay = DebugOverlay::NoOverlay;
        texDisplay.flipY = false;
        texDisplay.subresource.mip = s.mip;
        texDisplay.subresource.slice = s.slice;
        texDisplay.subresource.sample = params.resolve ? ~0U : s.sample;
        texDisplay.customShaderId = ResourceId();
        texDisplay.rangeMin = params.blackPoint;
        texDisplay.rangeMax = params.whitePoint;
        texDisplay.resourceId = tex;
        texDisplay.typeCast = params.typeCast;
        texDisplay.rawOutput = false;
        texDisplay.xOffset = 0;
        texDisplay.yOffset = 0;

        // we scale our texture rendering by output dimension. To counteract that, add a manual
        // scale here
        texDisplay.scale = 1.0f / float(1 << s.mip);

        RenderTextureInternal(texDisplay, flags);
      }

      m_pImmediateContext->CopyResource(d, rtTex);
      SAFE_RELEASE(rtTex);

      SAFE_RELEASE(wrappedrtv);
    }
    else if(wasms && params.resolve)
    {
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.CPUAccessFlags = 0;

      ID3D11Texture2D *resolveTex = NULL;

      hr = m_pDevice->CreateTexture2D(&desc, NULL, &resolveTex);

      if(FAILED(hr))
      {
        RDCERR("Couldn't create target texture to resolve texture. HRESULT: %s", ToStr(hr).c_str());
        SAFE_RELEASE(d);
        return;
      }

      m_pImmediateContext->ResolveSubresource(resolveTex, s.slice, wrapTex, s.slice, desc.Format);
      m_pImmediateContext->CopyResource(d, resolveTex);

      SAFE_RELEASE(resolveTex);
    }
    else if(wasms)
    {
      GetDebugManager()->CopyTex2DMSToArray(UNWRAP(WrappedID3D11Texture2D1, d), wrapTex->GetReal());

      subresource = (s.slice * sampleCount + s.sample) * mips + s.mip;
    }
    else
    {
      m_pImmediateContext->CopyResource(d, wrapTex);
    }
  }
  else if(WrappedID3D11Texture3D1::m_TextureList.find(tex) !=
          WrappedID3D11Texture3D1::m_TextureList.end())
  {
    WrappedID3D11Texture3D1 *wrapTex =
        (WrappedID3D11Texture3D1 *)WrappedID3D11Texture3D1::m_TextureList[tex].m_Texture;

    D3D11_TEXTURE3D_DESC desc = {0};
    wrapTex->GetDesc(&desc);

    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;
    desc.Usage = D3D11_USAGE_STAGING;

    ID3D11Texture3D *d = NULL;

    mips = desc.MipLevels ? desc.MipLevels : CalcNumMips(desc.Width, desc.Height, desc.Depth);

    s.mip = RDCMIN(mips - 1, s.mip);

    if(params.remap != RemapTexture::NoRemap)
    {
      if(params.remap == RemapTexture::RGBA8)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R8G8B8A8_TYPELESS, BaseRemapType(params));
        if(IsSRGBFormat(desc.Format) && params.typeCast == CompType::Typeless)
          desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
      }
      else if(params.remap == RemapTexture::RGBA16)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R16G16B16A16_TYPELESS, BaseRemapType(params));
      }
      else if(params.remap == RemapTexture::RGBA32)
      {
        desc.Format = GetTypedFormat(DXGI_FORMAT_R32G32B32A32_TYPELESS, BaseRemapType(params));
      }
    }

    subresource = s.mip;

    HRESULT hr = m_pDevice->CreateTexture3D(&desc, NULL, &d);

    dummyTex = d;

    if(FAILED(hr))
    {
      RDCERR("Couldn't create staging texture to retrieve data. HRESULT: %s", ToStr(hr).c_str());
      return;
    }

    bytesize = GetByteSize(desc.Width, desc.Height, desc.Depth, desc.Format, s.mip);

    if(params.remap != RemapTexture::NoRemap)
    {
      subresource = s.mip;

      desc.CPUAccessFlags = 0;
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.BindFlags = D3D11_BIND_RENDER_TARGET;

      ID3D11Texture3D *rtTex = NULL;

      hr = m_pDevice->CreateTexture3D(&desc, NULL, &rtTex);

      if(FAILED(hr))
      {
        RDCERR("Couldn't create target texture to downcast texture. HRESULT: %s", ToStr(hr).c_str());
        SAFE_RELEASE(d);
        return;
      }

      D3D11_RENDER_TARGET_VIEW_DESC rtvDesc;
      rtvDesc.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE3D;
      rtvDesc.Format = desc.Format;
      rtvDesc.Texture3D.MipSlice = s.mip;
      rtvDesc.Texture3D.FirstWSlice = 0;
      rtvDesc.Texture3D.WSize = 1;
      ID3D11RenderTargetView *wrappedrtv = NULL;
      ID3D11RenderTargetView *rtv = NULL;

      D3D11_VIEWPORT viewport = {
          0, 0, (float)desc.Width, (float)desc.Height, 0.0f, 1.0f,
      };

      TexDisplayFlags flags = eTexDisplay_None;

      if(IsUIntFormat(desc.Format))
        flags = eTexDisplay_RemapUInt;
      else if(IsIntFormat(desc.Format))
        flags = eTexDisplay_RemapSInt;
      else
        flags = eTexDisplay_RemapFloat;

      for(UINT i = 0; i < (desc.Depth >> s.mip); i++)
      {
        rtvDesc.Texture3D.FirstWSlice = i;
        hr = m_pDevice->CreateRenderTargetView(rtTex, &rtvDesc, &wrappedrtv);
        if(FAILED(hr))
        {
          RDCERR("Couldn't create target rtv to downcast texture. HRESULT: %s", ToStr(hr).c_str());
          SAFE_RELEASE(d);
          SAFE_RELEASE(rtTex);
          return;
        }

        rtv = wrappedrtv;

        m_pImmediateContext->OMSetRenderTargets(1, &rtv, NULL);
        float color[4] = {0.0f, 0.5f, 0.0f, 0.0f};
        m_pImmediateContext->ClearRenderTargetView(rtv, color);

        SetOutputDimensions(desc.Width, desc.Height);
        m_pImmediateContext->RSSetViewports(1, &viewport);

        TextureDisplay texDisplay;

        texDisplay.red = texDisplay.green = texDisplay.blue = texDisplay.alpha = true;
        texDisplay.hdrMultiplier = -1.0f;
        texDisplay.linearDisplayAsGamma = false;
        texDisplay.overlay = DebugOverlay::NoOverlay;
        texDisplay.flipY = false;
        texDisplay.subresource.mip = s.mip;
        texDisplay.subresource.slice = i;
        texDisplay.subresource.sample = 0;
        texDisplay.customShaderId = ResourceId();
        texDisplay.rangeMin = params.blackPoint;
        texDisplay.rangeMax = params.whitePoint;
        texDisplay.resourceId = tex;
        texDisplay.typeCast = params.typeCast;
        texDisplay.rawOutput = false;
        texDisplay.xOffset = 0;
        texDisplay.yOffset = 0;

        // we scale our texture rendering by output dimension. To counteract that, add a manual
        // scale here
        texDisplay.scale = 1.0f / float(1 << s.mip);

        RenderTextureInternal(texDisplay, flags);

        SAFE_RELEASE(wrappedrtv);
      }

      m_pImmediateContext->CopyResource(d, rtTex);
      SAFE_RELEASE(rtTex);
    }
    else
    {
      m_pImmediateContext->CopyResource(d, wrapTex);
    }
  }
  else
  {
    RDCERR("Trying to get texture data for unknown ID %s!", ToStr(tex).c_str());
    return;
  }

  MapIntercept intercept;

  D3D11_MAPPED_SUBRESOURCE mapped = {0};
  HRESULT hr = m_pImmediateContext->Map(dummyTex, subresource, D3D11_MAP_READ, 0, &mapped);

  if(SUCCEEDED(hr))
  {
    data.resize(bytesize);
    intercept.InitWrappedResource(dummyTex, subresource, data.data());
    intercept.SetD3D(mapped);
    intercept.CopyFromD3D();
  }
  else
  {
    RDCERR("Couldn't map staging texture to retrieve data. HRESULT: %s", ToStr(hr).c_str());
  }

  SAFE_RELEASE(dummyTex);
}

rdcarray<ShaderSourcePrefix> D3D11Replay::GetCustomShaderSourcePrefixes()
{
  return {
      {ShaderEncoding::HLSL, HLSL_CUSTOM_PREFIX},
  };
}

static D3D11RenderState::Shader *GetEventOverrideStage(D3D11RenderState *state, ShaderStage stage)
{
  switch(stage)
  {
    case ShaderStage::Vertex: return &state->VS;
    case ShaderStage::Hull: return &state->HS;
    case ShaderStage::Domain: return &state->DS;
    case ShaderStage::Geometry: return &state->GS;
    case ShaderStage::Pixel: return &state->PS;
    case ShaderStage::Compute: return &state->CS;
    default: break;
  }

  return NULL;
}

static void BindEventOverrideStage(WrappedID3D11DeviceContext *context, ShaderStage stage,
                                   ID3D11DeviceChild *shader,
                                   ID3D11ClassInstance *const *instances, UINT numInstances)
{
  switch(stage)
  {
    case ShaderStage::Vertex:
      context->VSSetShader((ID3D11VertexShader *)shader, instances, numInstances);
      break;
    case ShaderStage::Hull:
      context->HSSetShader((ID3D11HullShader *)shader, instances, numInstances);
      break;
    case ShaderStage::Domain:
      context->DSSetShader((ID3D11DomainShader *)shader, instances, numInstances);
      break;
    case ShaderStage::Geometry:
      context->GSSetShader((ID3D11GeometryShader *)shader, instances, numInstances);
      break;
    case ShaderStage::Pixel:
      context->PSSetShader((ID3D11PixelShader *)shader, instances, numInstances);
      break;
    case ShaderStage::Compute:
      context->CSSetShader((ID3D11ComputeShader *)shader, instances, numInstances);
      break;
    default: RDCERR("Unsupported D3D11 event shader override stage %s", ToStr(stage).c_str()); break;
  }
}

static UINT EventCBufferShadowConstants(uint64_t byteSize)
{
  // D3D11.1 range binding counts are in 16-byte constants, but must be aligned to 16
  // constants (256 bytes). The range may exceed the physical shadow allocation: reads beyond
  // that allocation return zero. Keep the shadow itself equal to the validated original bytes.
  return AlignUp(UINT(byteSize / 16), 16U);
}

static void BindEventOverrideCBuffer(WrappedID3D11DeviceContext *context, ShaderStage stage,
                                     uint32_t slot, ID3D11Buffer *buffer, UINT firstConstant,
                                     UINT numConstants)
{
  switch(stage)
  {
    case ShaderStage::Vertex:
      context->VSSetConstantBuffers1(slot, 1, &buffer, &firstConstant, &numConstants);
      break;
    case ShaderStage::Hull:
      context->HSSetConstantBuffers1(slot, 1, &buffer, &firstConstant, &numConstants);
      break;
    case ShaderStage::Domain:
      context->DSSetConstantBuffers1(slot, 1, &buffer, &firstConstant, &numConstants);
      break;
    case ShaderStage::Geometry:
      context->GSSetConstantBuffers1(slot, 1, &buffer, &firstConstant, &numConstants);
      break;
    case ShaderStage::Pixel:
      context->PSSetConstantBuffers1(slot, 1, &buffer, &firstConstant, &numConstants);
      break;
    case ShaderStage::Compute:
      context->CSSetConstantBuffers1(slot, 1, &buffer, &firstConstant, &numConstants);
      break;
    default:
      RDCERR("Unsupported D3D11 event CBuffer override stage %s", ToStr(stage).c_str());
      break;
  }
}

rdcstr D3D11Replay::SetEventShaderOverride(uint32_t eventId, ShaderStage stage,
                                           ResourceId expectedOriginal, ResourceId replacement,
                                           uint32_t outputTarget, bool independentFloatOutput)
{
  RestoreEventShaderOverrides();
  if(outputTarget != ~0U && (stage != ShaderStage::Pixel || outputTarget >= 8))
    return "A blend bypass requires a valid pixel Shader color target";
  if(independentFloatOutput && (stage != ShaderStage::Pixel || outputTarget >= 8))
    return "An independent FP32 preview requires a pixel Shader color target";

  if(GetEventOverrideStage(m_pImmediateContext->GetCurrentPipelineState(), stage) == NULL)
    return StringFormat::Fmt("Shader stage %s is not supported by D3D11 event overrides",
                             ToStr(stage).c_str());

  const ActionDescription *action = m_pDevice->GetAction(eventId);
  if(action == NULL)
    return StringFormat::Fmt("Event %u is not a replayable action", eventId);

  if(stage == ShaderStage::Compute)
  {
    if(!(action->flags & ActionFlags::Dispatch))
      return StringFormat::Fmt("Event %u is not a compute dispatch", eventId);
  }
  else if(!(action->flags & ActionFlags::Drawcall))
  {
    return StringFormat::Fmt("Event %u is not a graphics draw", eventId);
  }

  D3D11ResourceManager *rm = m_pDevice->GetResourceManager();

  if(rm->GetUnreplacedID(expectedOriginal) != expectedOriginal)
    return "expectedOriginal must identify the canonical captured shader, not a replacement";

  auto expectedIt = WrappedShader::m_ShaderList.find(expectedOriginal);
  if(expectedIt == WrappedShader::m_ShaderList.end())
    return StringFormat::Fmt("Expected original %s is not a D3D11 shader",
                             ToStr(expectedOriginal).c_str());

  ShaderStage expectedStage = expectedIt->second->GetDetails().stage;
  if(expectedStage != stage)
    return StringFormat::Fmt("Expected original %s is a %s shader, not %s",
                             ToStr(expectedOriginal).c_str(), ToStr(expectedStage).c_str(),
                             ToStr(stage).c_str());

  auto replacementIt = WrappedShader::m_ShaderList.find(replacement);
  if(replacementIt == WrappedShader::m_ShaderList.end())
    return StringFormat::Fmt("Replacement %s is not a D3D11 shader", ToStr(replacement).c_str());

  const uint32_t shaderExtSlot = expectedIt->second->GetShaderExtSlot();

  // Match stock ReplaceResource behaviour. One target resource cannot safely represent original
  // shaders using different vendor-extension UAV slots because the DXBC metadata is per shader.
  for(auto it = m_EventShaderOverrides.begin(); it != m_EventShaderOverrides.end(); ++it)
  {
    if(it->second.replacementId == replacement && it->second.shaderExtSlot != shaderExtSlot)
      return "A replacement shader cannot be shared across different vendor extension UAV slots";
  }

  replacementIt->second->SetShaderExtSlot(shaderExtSlot);

  ShaderStage replacementStage = replacementIt->second->GetDetails().stage;
  if(replacementStage != stage)
    return StringFormat::Fmt("Replacement %s is a %s shader, not %s", ToStr(replacement).c_str(),
                             ToStr(replacementStage).c_str(), ToStr(stage).c_str());

  if(!rm->HasResource(replacement))
    return StringFormat::Fmt("Replacement shader %s is no longer live", ToStr(replacement).c_str());

  ID3D11DeviceChild *replacementObject = rm->GetResource(replacement, true);
  if(replacementObject == NULL || GetIDForDeviceChild(replacementObject) != replacement)
    return "Replacement shader does not resolve to the exact requested target resource";

  std::pair<uint32_t, ShaderStage> key = {eventId, stage};
  EventShaderOverride &configured = m_EventShaderOverrides[key];

  if(configured.replacement != replacementObject)
  {
    IntAddRef(replacementObject);
    IntRelease(configured.replacement);
    configured.replacement = replacementObject;
  }

  configured.expectedOriginal = expectedOriginal;
  configured.replacementId = replacement;
  configured.shaderExtSlot = shaderExtSlot;
  configured.outputTarget = outputTarget;
  configured.independentFloatOutput = independentFloatOutput;

  ClearPostVSCache();
  return {};
}

void D3D11Replay::ClearEventShaderOverride(uint32_t eventId, ShaderStage stage)
{
  RestoreEventShaderOverrides();

  auto it = m_EventShaderOverrides.find({eventId, stage});
  if(it == m_EventShaderOverrides.end())
    return;

  IntRelease(it->second.replacement);
  m_EventShaderOverrides.erase(it);
  ClearPostVSCache();
}

void D3D11Replay::ClearEventShaderOverrides()
{
  RestoreEventShaderOverrides();

  for(auto it = m_EventShaderOverrides.begin(); it != m_EventShaderOverrides.end(); ++it)
    IntRelease(it->second.replacement);

  m_EventShaderOverrides.clear();
  ClearPostVSCache();
}

void D3D11Replay::ClearEventShaderOverridesForResource(ResourceId id)
{
  RestoreEventShaderOverrides();

  bool removed = false;
  for(auto it = m_EventShaderOverrides.begin(); it != m_EventShaderOverrides.end();)
  {
    if(it->second.replacementId == id)
    {
      IntRelease(it->second.replacement);
      it = m_EventShaderOverrides.erase(it);
      removed = true;
    }
    else
    {
      ++it;
    }
  }

  if(removed)
    ClearPostVSCache();
}

rdcstr D3D11Replay::ApplyEventShaderOverrides(uint32_t eventId)
{
  if(!m_AppliedEventShaderOverrides.empty())
    return "A previous event-scoped shader override was not restored";

  rdcarray<std::pair<ShaderStage, const EventShaderOverride *> > pending;
  for(auto it = m_EventShaderOverrides.begin(); it != m_EventShaderOverrides.end(); ++it)
  {
    if(it->first.first == eventId)
      pending.push_back({it->first.second, &it->second});
  }

  if(pending.empty())
    return {};

  D3D11RenderState *state = m_pImmediateContext->GetCurrentPipelineState();
  D3D11ResourceManager *rm = m_pDevice->GetResourceManager();

  // Validate every configured stage before changing any of them so a stale expected shader fails
  // atomically instead of producing a partially overridden draw.
  for(const std::pair<ShaderStage, const EventShaderOverride *> &item : pending)
  {
    D3D11RenderState::Shader *bound = GetEventOverrideStage(state, item.first);
    if(bound == NULL)
      return StringFormat::Fmt("Shader stage %s is not supported", ToStr(item.first).c_str());

    // Dynamic linkage has additional class/interface-slot ABI which the initial override gate does
    // not validate. Do not carry captured class instances into an independently built shader.
    if(bound->NumInstances > 0)
      return StringFormat::Fmt(
          "Event %u uses %u dynamic class instances at %s; dynamic linkage overrides are not "
          "supported",
          eventId, bound->NumInstances, ToStr(item.first).c_str());

    ResourceId current = rm->GetUnreplacedID(GetIDForDeviceChild(bound->Object));
    if(current != item.second->expectedOriginal)
      return StringFormat::Fmt("Expected %s at %s, but event %u currently binds %s",
                               ToStr(item.second->expectedOriginal).c_str(),
                               ToStr(item.first).c_str(), eventId, ToStr(current).c_str());

    if(item.second->replacement == NULL ||
       GetIDForDeviceChild(item.second->replacement) != item.second->replacementId)
      return StringFormat::Fmt("Replacement shader %s is no longer valid",
                               ToStr(item.second->replacementId).c_str());

    auto replacementIt = WrappedShader::m_ShaderList.find(item.second->replacementId);
    if(replacementIt == WrappedShader::m_ShaderList.end() ||
       replacementIt->second->GetShaderExtSlot() != item.second->shaderExtSlot)
      return StringFormat::Fmt("Replacement shader %s vendor extension slot changed",
                               ToStr(item.second->replacementId).c_str());
  }

  uint32_t outputTarget = ~0U;
  bool independentFloatOutput = false;
  for(const auto &item : pending)
  {
    if(item.second->outputTarget != ~0U)
      outputTarget = item.second->outputTarget;
    independentFloatOutput |= item.second->independentFloatOutput;
  }
  EventShaderPreview *preview = NULL;
  if(independentFloatOutput)
  {
    rdcstr error = PrepareEventShaderPreview(eventId, outputTarget, preview);
    if(!error.empty())
      return error;
  }
  if(outputTarget != ~0U)
  {
    if(state->OM.RenderTargets[outputTarget] == NULL)
      return "The requested debug color target is not bound";
    const auto key = std::make_pair(GetIDForDeviceChild(state->OM.BlendState), outputTarget);
    auto cached = m_EventShaderBlendStates.find(key);
    if(cached == m_EventShaderBlendStates.end())
    {
      ID3D11BlendState *variant = NULL;
      HRESULT hr = E_FAIL;
      if(state->OM.BlendState && CanQuery<ID3D11BlendState1>(state->OM.BlendState))
      {
        D3D11_BLEND_DESC1 desc = {};
        ((WrappedID3D11BlendState1 *)state->OM.BlendState)->GetDesc1(&desc);
        if(!desc.IndependentBlendEnable)
          for(size_t i = 1; i < ARRAY_COUNT(desc.RenderTarget); i++)
            desc.RenderTarget[i] = desc.RenderTarget[0];
        desc.IndependentBlendEnable = TRUE;
        desc.AlphaToCoverageEnable = FALSE;
        desc.RenderTarget[outputTarget].BlendEnable = FALSE;
        desc.RenderTarget[outputTarget].LogicOpEnable = FALSE;
        desc.RenderTarget[outputTarget].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
        ID3D11BlendState1 *state1 = NULL;
        hr = m_pDevice->CreateBlendState1(&desc, &state1);
        variant = state1;
      }
      else
      {
        D3D11_BLEND_DESC desc = {};
        if(state->OM.BlendState)
          state->OM.BlendState->GetDesc(&desc);
        else
        {
          for(auto &target : desc.RenderTarget)
          {
            target.SrcBlend = target.SrcBlendAlpha = D3D11_BLEND_ONE;
            target.DestBlend = target.DestBlendAlpha = D3D11_BLEND_ZERO;
            target.BlendOp = target.BlendOpAlpha = D3D11_BLEND_OP_ADD;
            target.RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
          }
        }
        if(!desc.IndependentBlendEnable)
          for(size_t i = 1; i < ARRAY_COUNT(desc.RenderTarget); i++)
            desc.RenderTarget[i] = desc.RenderTarget[0];
        desc.IndependentBlendEnable = TRUE;
        desc.AlphaToCoverageEnable = FALSE;
        desc.RenderTarget[outputTarget].BlendEnable = FALSE;
        desc.RenderTarget[outputTarget].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
        hr = m_pDevice->CreateBlendState(&desc, &variant);
      }
      if(FAILED(hr) || !variant)
        return "Creating the event Shader blend state failed";
      if(m_EventShaderBlendStates.size() >= 64)
      {
        for(auto &item : m_EventShaderBlendStates)
          SAFE_RELEASE(item.second);
        m_EventShaderBlendStates.clear();
      }
      cached = m_EventShaderBlendStates.insert({key, variant}).first;
    }
    m_EventShaderPreviousBlend = state->OM.BlendState;
    IntAddRef(m_EventShaderPreviousBlend);
    memcpy(m_EventShaderPreviousBlendFactor, state->OM.BlendFactor, sizeof(m_EventShaderPreviousBlendFactor));
    m_EventShaderPreviousSampleMask = state->OM.SampleMask;
    m_EventShaderBlendApplied = true;
    m_pImmediateContext->OMSetBlendState(cached->second, state->OM.BlendFactor, state->OM.SampleMask);
  }

  m_AppliedEventShaderOverrides.reserve(pending.size());
  if(preview)
  {
    m_EventShaderPreviousTargetCount = 0;
    for(UINT i = 0; i < 8; i++)
    {
      m_EventShaderPreviousTargets[i] = state->OM.RenderTargets[i];
      IntAddRef(m_EventShaderPreviousTargets[i]);
      if(m_EventShaderPreviousTargets[i])
        m_EventShaderPreviousTargetCount = i + 1;
    }
    m_EventShaderPreviousDepth = state->OM.DepthView;
    IntAddRef(m_EventShaderPreviousDepth);
    m_EventShaderTargetsApplied = true;
    m_pImmediateContext->OMSetRenderTargetsAndUnorderedAccessViews(
        m_EventShaderPreviousTargetCount, preview->views, preview->depthView,
        0, D3D11_KEEP_UNORDERED_ACCESS_VIEWS, NULL, NULL);
  }

  for(const std::pair<ShaderStage, const EventShaderOverride *> &item : pending)
  {
    D3D11RenderState::Shader *bound = GetEventOverrideStage(state, item.first);

    AppliedEventShaderOverride applied;
    applied.stage = item.first;
    applied.original = bound->Object;
    applied.numInstances = bound->NumInstances;

    IntAddRef(applied.original);
    for(UINT i = 0; i < applied.numInstances; ++i)
    {
      applied.instances[i] = bound->Instances[i];
      IntAddRef(applied.instances[i]);
    }

    m_AppliedEventShaderOverrides.push_back(applied);
    BindEventOverrideStage(m_pImmediateContext, item.first, item.second->replacement,
                           applied.instances, applied.numInstances);
  }

  return {};
}

void D3D11Replay::RestoreEventShaderOverrides()
{
  if(m_EventShaderTargetsApplied)
  {
    m_pImmediateContext->OMSetRenderTargetsAndUnorderedAccessViews(
        m_EventShaderPreviousTargetCount, m_EventShaderPreviousTargets, m_EventShaderPreviousDepth,
        0, D3D11_KEEP_UNORDERED_ACCESS_VIEWS, NULL, NULL);
    for(ID3D11RenderTargetView *&view : m_EventShaderPreviousTargets)
    {
      IntRelease(view);
      view = NULL;
    }
    IntRelease(m_EventShaderPreviousDepth);
    m_EventShaderPreviousDepth = NULL;
    m_EventShaderTargetsApplied = false;
  }
  if(m_EventShaderBlendApplied)
  {
    m_pImmediateContext->OMSetBlendState(m_EventShaderPreviousBlend,
        m_EventShaderPreviousBlendFactor, m_EventShaderPreviousSampleMask);
    IntRelease(m_EventShaderPreviousBlend);
    m_EventShaderPreviousBlend = NULL;
    m_EventShaderBlendApplied = false;
  }
  for(size_t i = m_AppliedEventShaderOverrides.size(); i > 0; --i)
  {
    AppliedEventShaderOverride &applied = m_AppliedEventShaderOverrides[i - 1];

    BindEventOverrideStage(m_pImmediateContext, applied.stage, applied.original, applied.instances,
                           applied.numInstances);

    IntRelease(applied.original);
    for(UINT inst = 0; inst < applied.numInstances; ++inst)
      IntRelease(applied.instances[inst]);
  }

  m_AppliedEventShaderOverrides.clear();
}

void D3D11Replay::ReleaseEventShaderPreviews()
{
  for(auto &item : m_EventShaderPreviews)
  {
    for(ID3D11RenderTargetView *view : item.second.views)
      SAFE_RELEASE(view);
    for(ID3D11Resource *resource : item.second.colors)
      SAFE_RELEASE(resource);
    SAFE_RELEASE(item.second.depthView);
    SAFE_RELEASE(item.second.depth);
  }
  m_EventShaderPreviews.clear();
}

TextureDescription D3D11Replay::GetEventShaderPreviewTexture(uint32_t eventId, ShaderStage stage)
{
  auto binding = m_EventShaderOverrides.find({eventId, stage});
  auto preview = m_EventShaderPreviews.find({eventId, stage});
  if(binding == m_EventShaderOverrides.end() || !binding->second.independentFloatOutput ||
     preview == m_EventShaderPreviews.end() ||
     preview->second.outputTarget != binding->second.outputTarget)
    return {};
  return preview->second.description;
}

rdcstr D3D11Replay::PrepareEventShaderPreview(uint32_t eventId, uint32_t outputTarget,
                                            EventShaderPreview *&preview)
{
  preview = NULL;
  D3D11RenderState *state = m_pImmediateContext->GetCurrentPipelineState();
  if(outputTarget >= 8 || !state->OM.RenderTargets[outputTarget])
    return "The independent float preview attachment is not bound";
  struct SourceReferences
  {
    ID3D11Resource *resources[9] = {};
    ~SourceReferences()
    {
      for(ID3D11Resource *resource : resources)
        SAFE_RELEASE(resource);
    }
  } sources;
  ResourceId viewIds[8] = {}, resourceIds[9] = {};
  for(UINT i = 0; i < 8; i++)
  {
    if(state->OM.RenderTargets[i])
    {
      state->OM.RenderTargets[i]->GetResource(&sources.resources[i]);
      viewIds[i] = GetIDForDeviceChild(state->OM.RenderTargets[i]);
    }
    resourceIds[i] = GetIDForDeviceChild(sources.resources[i]);
  }
  if(state->OM.DepthView)
    state->OM.DepthView->GetResource(&sources.resources[8]);
  resourceIds[8] = GetIDForDeviceChild(sources.resources[8]);
  ResourceId depthId = GetIDForDeviceChild(state->OM.DepthView);
  const auto key = std::make_pair(eventId, ShaderStage::Pixel);
  auto found = m_EventShaderPreviews.find(key);
  bool matches = found != m_EventShaderPreviews.end();
  if(matches)
  {
    matches = found->second.outputTarget == outputTarget && found->second.sourceDepthView == depthId;
    for(UINT i = 0; matches && i < 8; i++)
      matches = found->second.sourceViews[i] == viewIds[i];
    for(UINT i = 0; matches && i < 9; i++)
      matches = found->second.sourceResources[i] == resourceIds[i];
  }
  auto destroy = [](EventShaderPreview &entry) {
    for(ID3D11RenderTargetView *view : entry.views)
      SAFE_RELEASE(view);
    for(ID3D11Resource *resource : entry.colors)
      SAFE_RELEASE(resource);
    SAFE_RELEASE(entry.depthView);
    SAFE_RELEASE(entry.depth);
  };
  if(!matches && found != m_EventShaderPreviews.end())
  {
    destroy(found->second);
    m_EventShaderPreviews.erase(found);
  }
  if(!matches)
  {
    EventShaderPreview entry;
    entry.outputTarget = outputTarget;
    entry.sourceDepthView = depthId;
    for(UINT i = 0; i < 8; i++)
      entry.sourceViews[i] = viewIds[i];
    for(UINT i = 0; i < 9; i++)
      entry.sourceResources[i] = resourceIds[i];
    D3D11_TEXTURE2D_DESC descriptions[9] = {};
    D3D11_RENDER_TARGET_VIEW_DESC selectedView = {};
    state->OM.RenderTargets[outputTarget]->GetDesc(&selectedView);
    for(UINT i = 0; i < 9; i++)
    {
      if(!sources.resources[i])
        continue;
      D3D11_RESOURCE_DIMENSION dimension;
      sources.resources[i]->GetType(&dimension);
      if(dimension != D3D11_RESOURCE_DIMENSION_TEXTURE2D)
        return "Independent float preview requires 2D or 2D-array attachments";
      D3D11_TEXTURE2D_DESC desc = {};
      ((ID3D11Texture2D *)sources.resources[i])->GetDesc(&desc);
      if(desc.MiscFlags & D3D11_RESOURCE_MISC_TILED)
        return "Independent float preview does not support tiled attachments";
      desc.Usage = D3D11_USAGE_DEFAULT;
      desc.CPUAccessFlags = 0;
      desc.MiscFlags = 0;
      desc.BindFlags = i == 8 ? D3D11_BIND_DEPTH_STENCIL : D3D11_BIND_RENDER_TARGET;
      if(i == outputTarget)
      {
        UINT mip = 0, layers = 1;
        switch(selectedView.ViewDimension)
        {
          case D3D11_RTV_DIMENSION_TEXTURE2D:
            mip = selectedView.Texture2D.MipSlice;
            selectedView.Texture2D.MipSlice = 0;
            break;
          case D3D11_RTV_DIMENSION_TEXTURE2DARRAY:
            mip = selectedView.Texture2DArray.MipSlice;
            layers = selectedView.Texture2DArray.ArraySize;
            selectedView.Texture2DArray.MipSlice = 0;
            selectedView.Texture2DArray.FirstArraySlice = 0;
            break;
          case D3D11_RTV_DIMENSION_TEXTURE2DMS: break;
          case D3D11_RTV_DIMENSION_TEXTURE2DMSARRAY:
            layers = selectedView.Texture2DMSArray.ArraySize;
            selectedView.Texture2DMSArray.FirstArraySlice = 0;
            break;
          default: return "The independent float preview view is unsupported";
        }
        if(mip >= desc.MipLevels || layers == 0 || layers > desc.ArraySize)
          return "The independent float preview view has an invalid subresource range";
        desc.Width = RDCMAX(1U, desc.Width >> mip);
        desc.Height = RDCMAX(1U, desc.Height >> mip);
        desc.MipLevels = 1;
        desc.ArraySize = layers;
        desc.Format = selectedView.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
        desc.BindFlags |= D3D11_BIND_SHADER_RESOURCE;
        UINT support = 0;
        if(FAILED(m_pDevice->CheckFormatSupport(desc.Format, &support)) ||
           !(support & D3D11_FORMAT_SUPPORT_RENDER_TARGET) ||
           !(support & D3D11_FORMAT_SUPPORT_SHADER_LOAD))
          return "This device cannot render and inspect an RGBA32F preview";
        if(desc.SampleDesc.Count > 1)
        {
          UINT quality = 0;
          if(FAILED(m_pDevice->CheckMultisampleQualityLevels(desc.Format, desc.SampleDesc.Count,
                                                           &quality)) ||
             quality == 0 || desc.SampleDesc.Quality >= quality)
            return "RGBA32F does not support the captured multisample count and quality";
        }
      }
      uint64_t bytes = 0;
      for(UINT mip = 0; mip < desc.MipLevels; mip++)
        bytes += GetByteSize(desc.Width, desc.Height, 1, desc.Format, mip);
      bytes *= uint64_t(desc.ArraySize) * desc.SampleDesc.Count;
      const uint64_t budget = 512ULL * 1024 * 1024;
      if(bytes == 0 || bytes > budget || entry.bytes > budget - bytes)
        return "Independent float preview attachments exceed the 512 MiB scratch budget";
      entry.bytes += bytes;
      descriptions[i] = desc;
    }
    uint64_t total = entry.bytes;
    for(const auto &item : m_EventShaderPreviews)
      total += item.second.bytes;
    while(m_EventShaderPreviews.size() >= 8 || total > 512ULL * 1024 * 1024)
    {
      auto oldest = m_EventShaderPreviews.end();
      for(auto it = m_EventShaderPreviews.begin(); it != m_EventShaderPreviews.end(); ++it)
      {
        auto active = m_EventShaderOverrides.find(it->first);
        if(active != m_EventShaderOverrides.end() && active->second.independentFloatOutput)
          continue;
        if(oldest == m_EventShaderPreviews.end() || it->second.used < oldest->second.used)
          oldest = it;
      }
      if(oldest == m_EventShaderPreviews.end())
        return "Independent float preview cache is full of active resources";
      total -= oldest->second.bytes;
      destroy(oldest->second);
      m_EventShaderPreviews.erase(oldest);
    }
    for(UINT i = 0; i < 9; i++)
    {
      if(!sources.resources[i])
        continue;
      ID3D11Texture2D *texture = NULL;
      HRESULT hr = m_pDevice->CreateTexture2D(&descriptions[i], NULL, &texture);
      if(FAILED(hr) || !texture)
      {
        destroy(entry);
        return StringFormat::Fmt("Allocating independent float preview failed: %s", ToStr(hr).c_str());
      }
      if(i == 8)
      {
        entry.depth = texture;
        D3D11_DEPTH_STENCIL_VIEW_DESC view = {};
        state->OM.DepthView->GetDesc(&view);
        hr = m_pDevice->CreateDepthStencilView(texture, &view, &entry.depthView);
      }
      else
      {
        entry.colors[i] = texture;
        D3D11_RENDER_TARGET_VIEW_DESC view = {};
        state->OM.RenderTargets[i]->GetDesc(&view);
        hr = m_pDevice->CreateRenderTargetView(texture, i == outputTarget ? &selectedView : &view,
                                              &entry.views[i]);
      }
      if(FAILED(hr))
      {
        destroy(entry);
        return "Creating the independent float preview attachment view failed";
      }
    }
    entry.description = GetTexture(GetIDForDeviceChild(entry.colors[outputTarget]));
    found = m_EventShaderPreviews.insert({key, std::move(entry)}).first;
  }
  EventShaderPreview &entry = found->second;
  for(UINT i = 0; i < 8; i++)
    if(i != outputTarget && entry.colors[i])
      m_pImmediateContext->CopyResource(entry.colors[i], sources.resources[i]);
  if(entry.depth)
    m_pImmediateContext->CopyResource(entry.depth, sources.resources[8]);
  const FLOAT clear[] = {0.0f, 0.0f, 0.0f, 0.0f};
  m_pImmediateContext->ClearRenderTargetView(entry.views[outputTarget], clear);
  entry.used = ++m_EventShaderPreviewSerial;
  preview = &entry;
  return {};
}

rdcstr D3D11Replay::ValidateEventCBufferBinding(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                                uint32_t registerSpace,
                                                ResourceId expectedShader,
                                                ResourceId expectedBuffer,
                                                uint64_t expectedByteOffset,
                                                uint64_t expectedByteSize)
{
  if(registerSpace != 0)
    return "D3D11 constant-buffer overrides require register space 0";

  D3D11RenderState::Shader *bound =
      GetEventOverrideStage(m_pImmediateContext->GetCurrentPipelineState(), stage);
  if(bound == NULL)
    return StringFormat::Fmt("Shader stage %s is not supported", ToStr(stage).c_str());

  D3D11ResourceManager *rm = m_pDevice->GetResourceManager();
  ResourceId currentShader = rm->GetUnreplacedID(GetIDForDeviceChild(bound->Object));
  if(currentShader != expectedShader)
    return StringFormat::Fmt("Expected shader %s at %s, but event %u currently binds %s",
                             ToStr(expectedShader).c_str(), ToStr(stage).c_str(), eventId,
                             ToStr(currentShader).c_str());

  ID3D11Buffer *currentBuffer = bound->ConstantBuffers[slot];
  ResourceId currentBufferId = rm->GetUnreplacedID(GetIDForDeviceChild(currentBuffer));

  if(expectedBuffer == ResourceId())
  {
    if(currentBuffer != NULL)
      return StringFormat::Fmt("Expected an unbound CBuffer at %s slot %u, but found %s",
                               ToStr(stage).c_str(), slot, ToStr(currentBufferId).c_str());
  }
  else
  {
    if(currentBuffer == NULL || currentBufferId != expectedBuffer)
      return StringFormat::Fmt("Expected CBuffer %s at %s slot %u, but found %s",
                               ToStr(expectedBuffer).c_str(), ToStr(stage).c_str(), slot,
                               ToStr(currentBufferId).c_str());

    const uint64_t currentByteOffset = uint64_t(bound->CBOffsets[slot]) * 16ULL;
    const uint64_t currentByteSize = uint64_t(bound->CBCounts[slot]) * 16ULL;
    if(currentByteOffset != expectedByteOffset || currentByteSize != expectedByteSize)
      return StringFormat::Fmt(
          "Expected CBuffer byte offset/size %llu/%llu at %s slot %u, but found %llu/%llu",
          (unsigned long long)expectedByteOffset, (unsigned long long)expectedByteSize,
          ToStr(stage).c_str(), slot, (unsigned long long)currentByteOffset,
          (unsigned long long)currentByteSize);
  }

  return {};
}

rdcstr D3D11Replay::SetEventCBufferOverride(
    uint32_t eventId, ShaderStage stage, uint32_t slot, ResourceId expectedShader,
    ResourceId expectedBuffer, uint64_t expectedByteOffset, uint64_t expectedByteSize,
    const bytebuf &data, uint32_t registerSpace)
{
  RestoreEventCBufferOverrides();

  if(slot >= D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT)
    return "D3D11 constant-buffer slot must be between 0 and 13";
  if(registerSpace != 0)
    return "D3D11 constant-buffer overrides require register space 0";

  if(expectedShader == ResourceId())
    return "expectedShader must be a non-null captured shader resource";

  if(data.empty() || data.size() > D3D11_REQ_CONSTANT_BUFFER_ELEMENT_COUNT * 16ULL ||
     (data.size() % 16) != 0)
    return "data must be non-empty, 16-byte aligned, and no larger than 64 KiB";

  if((expectedByteOffset % 16) != 0 || (expectedByteSize % 16) != 0 ||
     expectedByteSize > D3D11_REQ_CONSTANT_BUFFER_ELEMENT_COUNT * 16ULL)
    return "The expected offset and size must be 16-byte aligned, and size must not exceed 64 KiB";

  if(expectedBuffer == ResourceId() && (expectedByteOffset != 0 || expectedByteSize != 0))
    return "An expected unbound slot must use a zero byte offset and size";

  if(GetEventOverrideStage(m_pImmediateContext->GetCurrentPipelineState(), stage) == NULL)
    return StringFormat::Fmt("Shader stage %s is not supported by D3D11 event overrides",
                             ToStr(stage).c_str());

  const ActionDescription *action = m_pDevice->GetAction(eventId);
  if(action == NULL)
    return StringFormat::Fmt("Event %u is not a replayable action", eventId);

  if(stage == ShaderStage::Compute)
  {
    if(!(action->flags & ActionFlags::Dispatch))
      return StringFormat::Fmt("Event %u is not a compute dispatch", eventId);
  }
  else if(!(action->flags & ActionFlags::Drawcall))
  {
    return StringFormat::Fmt("Event %u is not a graphics draw", eventId);
  }

  D3D11ResourceManager *rm = m_pDevice->GetResourceManager();
  if(rm->GetUnreplacedID(expectedShader) != expectedShader)
    return "expectedShader must identify the canonical captured shader, not a replacement";
  if(expectedBuffer != ResourceId() && rm->GetUnreplacedID(expectedBuffer) != expectedBuffer)
    return "expectedBuffer must identify the canonical captured buffer, not a replacement";

  auto expectedShaderIt = WrappedShader::m_ShaderList.find(expectedShader);
  if(expectedShaderIt == WrappedShader::m_ShaderList.end())
    return StringFormat::Fmt("Expected shader %s is not a D3D11 shader",
                             ToStr(expectedShader).c_str());

  ShaderStage expectedStage = expectedShaderIt->second->GetDetails().stage;
  if(expectedStage != stage)
    return StringFormat::Fmt("Expected shader %s is a %s shader, not %s",
                             ToStr(expectedShader).c_str(), ToStr(expectedStage).c_str(),
                             ToStr(stage).c_str());

  rdcstr validation = ValidateEventCBufferBinding(eventId, stage, slot, registerSpace, expectedShader,
                                                  expectedBuffer, expectedByteOffset,
                                                  expectedByteSize);
  if(!validation.empty())
    return validation;

  if(expectedBuffer != ResourceId())
  {
    D3D11RenderState::Shader *bound =
        GetEventOverrideStage(m_pImmediateContext->GetCurrentPipelineState(), stage);
    ID3D11Buffer *currentBuffer = bound->ConstantBuffers[slot];
    D3D11_BUFFER_DESC currentDesc = {};
    currentBuffer->GetDesc(&currentDesc);

    if(expectedByteOffset > uint64_t(currentDesc.ByteWidth))
      return StringFormat::Fmt(
          "Expected CBuffer byte offset %llu exceeds buffer %s byte width %u",
          (unsigned long long)expectedByteOffset, ToStr(expectedBuffer).c_str(),
          currentDesc.ByteWidth);

    const uint64_t availableBytes = uint64_t(currentDesc.ByteWidth) - expectedByteOffset;
    const uint64_t validWindowBytes = RDCMIN(expectedByteSize, availableBytes);
    if(data.size() != validWindowBytes)
      return StringFormat::Fmt(
          "data must contain the complete valid CBuffer window (%llu bytes), but contains %llu",
          (unsigned long long)validWindowBytes, (unsigned long long)data.size());
  }

  EventCBufferOverrideKey key = {eventId, stage, slot, registerSpace};
  auto configuredIt = m_EventCBufferOverrides.find(key);

  ID3D11Buffer *shadow =
      configuredIt == m_EventCBufferOverrides.end() ? NULL : configuredIt->second.shadow;
  if(shadow != NULL)
  {
    D3D11_BUFFER_DESC desc = {};
    shadow->GetDesc(&desc);
    if(desc.ByteWidth != data.size())
      shadow = NULL;
  }

  ID3D11Buffer *newShadow = NULL;
  if(shadow == NULL)
  {
    D3D11_BUFFER_DESC desc = {};
    desc.ByteWidth = (UINT)data.size();
    desc.Usage = D3D11_USAGE_DYNAMIC;
    desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;

    HRESULT hr = m_pDevice->CreateBuffer(&desc, NULL, &newShadow);
    if(FAILED(hr) || newShadow == NULL)
      return StringFormat::Fmt("Failed to create the event CBuffer shadow: %s", ToStr(hr).c_str());
  }

  EventCBufferOverride &configured = m_EventCBufferOverrides[key];
  if(newShadow != NULL)
  {
    SAFE_RELEASE(configured.shadow);
    configured.shadow = newShadow;
  }

  configured.expectedShader = expectedShader;
  configured.expectedBuffer = expectedBuffer;
  configured.expectedByteOffset = expectedByteOffset;
  configured.expectedByteSize = expectedByteSize;
  configured.data = data;

  ClearPostVSCache();
  return {};
}

void D3D11Replay::ClearEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                            uint32_t registerSpace)
{
  RestoreEventCBufferOverrides();

  EventCBufferOverrideKey key = {eventId, stage, slot, registerSpace};
  auto it = m_EventCBufferOverrides.find(key);
  if(it == m_EventCBufferOverrides.end())
    return;

  SAFE_RELEASE(it->second.shadow);
  m_EventCBufferOverrides.erase(it);
  ClearPostVSCache();
}

void D3D11Replay::ClearEventCBufferOverrides()
{
  RestoreEventCBufferOverrides();

  for(auto it = m_EventCBufferOverrides.begin(); it != m_EventCBufferOverrides.end(); ++it)
    SAFE_RELEASE(it->second.shadow);

  m_EventCBufferOverrides.clear();
  ClearPostVSCache();
}

rdcstr D3D11Replay::ApplyEventCBufferOverrides(uint32_t eventId)
{
  if(!m_AppliedEventCBufferOverrides.empty())
    return "A previous event-scoped CBuffer override was not restored";

  rdcarray<std::pair<const EventCBufferOverrideKey *, EventCBufferOverride *> > pending;
  for(auto it = m_EventCBufferOverrides.begin(); it != m_EventCBufferOverrides.end(); ++it)
  {
    if(it->first.eventId == eventId)
      pending.push_back({&it->first, &it->second});
  }

  if(pending.empty())
    return {};

  // Validate every binding against the captured pipeline before updating or binding any shadow.
  for(const std::pair<const EventCBufferOverrideKey *, EventCBufferOverride *> &item : pending)
  {
    rdcstr validation = ValidateEventCBufferBinding(
        eventId, item.first->stage, item.first->slot, item.first->registerSpace,
        item.second->expectedShader,
        item.second->expectedBuffer, item.second->expectedByteOffset,
        item.second->expectedByteSize);
    if(!validation.empty())
      return validation;

    if(item.second->shadow == NULL)
      return "An event-scoped CBuffer shadow is no longer valid";

    if(item.second->expectedBuffer != ResourceId())
    {
      D3D11RenderState::Shader *bound = GetEventOverrideStage(
          m_pImmediateContext->GetCurrentPipelineState(), item.first->stage);
      D3D11_BUFFER_DESC currentDesc = {};
      bound->ConstantBuffers[item.first->slot]->GetDesc(&currentDesc);

      if(item.second->expectedByteOffset > uint64_t(currentDesc.ByteWidth))
        return "The expected CBuffer byte offset now exceeds the live buffer byte width";

      const uint64_t availableBytes =
          uint64_t(currentDesc.ByteWidth) - item.second->expectedByteOffset;
      const uint64_t validWindowBytes = RDCMIN(item.second->expectedByteSize, availableBytes);
      if(item.second->data.size() != validWindowBytes)
        return "The event CBuffer data no longer covers the complete valid binding window";
    }
  }

  // Upload every shadow first. A Map failure leaves the captured pipeline completely untouched.
  for(const std::pair<const EventCBufferOverrideKey *, EventCBufferOverride *> &item : pending)
  {
    D3D11_MAPPED_SUBRESOURCE mapped = {};
    ID3D11Buffer *realShadow = UNWRAP(WrappedID3D11Buffer, item.second->shadow);
    HRESULT hr = m_pImmediateContext->GetReal()->Map(realShadow, 0, D3D11_MAP_WRITE_DISCARD, 0,
                                                     &mapped);
    m_pDevice->CheckHRESULT(hr);
    if(FAILED(hr))
      return StringFormat::Fmt("Failed to update the event CBuffer shadow: %s", ToStr(hr).c_str());

    if(mapped.pData == NULL)
    {
      m_pImmediateContext->GetReal()->Unmap(realShadow, 0);
      return "Updating the event CBuffer shadow returned a null mapping";
    }

    memcpy(mapped.pData, item.second->data.data(), item.second->data.size());
    m_pImmediateContext->GetReal()->Unmap(realShadow, 0);
  }

  D3D11RenderState *state = m_pImmediateContext->GetCurrentPipelineState();
  m_AppliedEventCBufferOverrides.reserve(pending.size());

  for(const std::pair<const EventCBufferOverrideKey *, EventCBufferOverride *> &item : pending)
  {
    D3D11RenderState::Shader *bound = GetEventOverrideStage(state, item.first->stage);

    AppliedEventCBufferOverride applied;
    applied.stage = item.first->stage;
    applied.slot = item.first->slot;
    applied.original = bound->ConstantBuffers[applied.slot];
    applied.firstConstant = bound->CBOffsets[applied.slot];
    applied.numConstants = bound->CBCounts[applied.slot];
    IntAddRef(applied.original);

    m_AppliedEventCBufferOverrides.push_back(applied);

    UINT firstConstant = 0;
    UINT numConstants = EventCBufferShadowConstants(item.second->data.size());
    BindEventOverrideCBuffer(m_pImmediateContext, applied.stage, applied.slot, item.second->shadow,
                             firstConstant, numConstants);
  }

  return {};
}

void D3D11Replay::RestoreEventCBufferOverrides()
{
  for(size_t i = m_AppliedEventCBufferOverrides.size(); i > 0; --i)
  {
    AppliedEventCBufferOverride &applied = m_AppliedEventCBufferOverrides[i - 1];

    BindEventOverrideCBuffer(m_pImmediateContext, applied.stage, applied.slot, applied.original,
                             applied.firstConstant, applied.numConstants);
    IntRelease(applied.original);
  }

  m_AppliedEventCBufferOverrides.clear();
}

static ShaderStage D3D11NativeShaderStage(ID3D11DeviceChild *resource)
{
  if(!resource)
    return ShaderStage::Count;
  if(WrappedID3D11Shader<ID3D11VertexShader>::IsAlloc(resource))
    return ShaderStage::Vertex;
  if(WrappedID3D11Shader<ID3D11HullShader>::IsAlloc(resource))
    return ShaderStage::Hull;
  if(WrappedID3D11Shader<ID3D11DomainShader>::IsAlloc(resource))
    return ShaderStage::Domain;
  if(WrappedID3D11Shader<ID3D11GeometryShader>::IsAlloc(resource))
    return ShaderStage::Geometry;
  if(WrappedID3D11Shader<ID3D11PixelShader>::IsAlloc(resource))
    return ShaderStage::Pixel;
  if(WrappedID3D11Shader<ID3D11ComputeShader>::IsAlloc(resource))
    return ShaderStage::Compute;
  return ShaderStage::Count;
}

// BuildTargetShader creates an ordinary shader with no class linkage or SO declaration. Check
// the captured creation call, rather than inferring absence of those structures from reflection.
static bool D3D11CheckedShaderCreationMatches(const SDChunk &chunk, ResourceId shader,
                                              ShaderStage stage)
{
  D3D11Chunk expected;
  switch(stage)
  {
    case ShaderStage::Vertex: expected = D3D11Chunk::CreateVertexShader; break;
    case ShaderStage::Hull: expected = D3D11Chunk::CreateHullShader; break;
    case ShaderStage::Domain: expected = D3D11Chunk::CreateDomainShader; break;
    case ShaderStage::Geometry: expected = D3D11Chunk::CreateGeometryShader; break;
    case ShaderStage::Pixel: expected = D3D11Chunk::CreatePixelShader; break;
    case ShaderStage::Compute: expected = D3D11Chunk::CreateComputeShader; break;
    default: return false;
  }
  const SDObject *created = chunk.FindChild("pShader");
  const SDObject *linkage = chunk.FindChild("pClassLinkage");
  return D3D11Chunk(chunk.metadata.chunkID) == expected && created && created->IsResource() &&
         created->AsResourceId() == shader && linkage && linkage->IsResource() &&
         linkage->AsResourceId() == ResourceId();
}

rdcstr D3D11Replay::ReplaceTargetShaderChecked(ResourceId from, ResourceId to)
{
  D3D11ResourceManager *rm = m_pDevice->GetResourceManager();
  if(from == ResourceId() || to == ResourceId() || from == to)
    return "Checked D3D11 replacement requires distinct non-null shader resources";
  if(m_Proxy || rm->GetUnreplacedID(from) != from || ResourceIDGen::IsReplayOnlyID(from))
    return "The original D3D11 shader must be the canonical captured ResourceId";
  if(!m_AppliedEventShaderOverrides.empty() || !m_AppliedEventCBufferOverrides.empty() ||
     m_RealState.active)
    return "Temporary D3D11 event overrides must be restored before replacing a shared shader";

  auto capturedEntry = WrappedShader::m_ShaderList.find(from);
  auto targetEntry = WrappedShader::m_ShaderList.find(to);
  ID3D11DeviceChild *captured = rm->GetResourceWithoutReplacement(from, true);
  ID3D11DeviceChild *target = rm->GetResourceWithoutReplacement(to, true);
  if(capturedEntry == WrappedShader::m_ShaderList.end() ||
     targetEntry == WrappedShader::m_ShaderList.end() || !captured || !target ||
     GetIDForDeviceChild(captured) != from || GetIDForDeviceChild(target) != to)
    return "The original or target D3D11 shader object is not live at its exact resource identity";
  const ShaderStage stage = D3D11NativeShaderStage(captured);
  if(stage == ShaderStage::Count || D3D11NativeShaderStage(target) != stage ||
     capturedEntry->second->GetDetails().stage != stage ||
     targetEntry->second->GetDetails().stage != stage)
    return "The target D3D11 shader stage does not match the captured native shader stage";

  const auto resource = m_ResourceIdx.find(from);
  SDFile *file = GetStructuredFile();
  if(resource == m_ResourceIdx.end() || resource->second >= m_Resources.size() || !file)
    return "The captured D3D11 shader creation evidence is unavailable";
  bool ordinaryCreation = false;
  for(uint32_t chunkIndex : m_Resources[resource->second].initialisationChunks)
  {
    if(chunkIndex >= file->chunks.size() || !file->chunks[chunkIndex])
      return "The captured D3D11 shader creation evidence is incomplete";
    ordinaryCreation |= D3D11CheckedShaderCreationMatches(*file->chunks[chunkIndex], from, stage);
  }
  if(!ordinaryCreation || !capturedEntry->second->GetDetails().interfaces.empty() ||
     !targetEntry->second->GetDetails().interfaces.empty())
    return "Checked D3D11 replacement requires an ordinary shader without class linkage or stream "
           "output";
  // Vendor UAV setup affects native creation, and cannot be recreated by changing cached DXBC
  // metadata after BuildTargetShader. Keep this transaction fail-closed for that separate route.
  if(capturedEntry->second->GetShaderExtSlot() != ~0U ||
     targetEntry->second->GetShaderExtSlot() != ~0U)
    return "Checked D3D11 vendor-extension shader replacement requires native extension creation "
           "support";

  const bool hadPrevious = rm->HasReplacement(from);
  ID3D11DeviceChild *previous = rm->GetResource(from, true);
  if(!previous || D3D11NativeShaderStage(previous) != stage)
    return "The active D3D11 replacement is not a live shader of the captured stage";
  const ResourceId previousId = GetIDForDeviceChild(previous);
  if(hadPrevious ? (previousId == from || rm->GetUnreplacedID(previousId) != from)
                 : previousId != from)
    return "The captured D3D11 shader has an invalid replacement ownership mapping";
  if(rm->HasReplacement(to))
    return "The target D3D11 shader is itself a resource replacement key";
  if(hadPrevious && previousId == to)
    return {};
  if(rm->GetUnreplacedID(to) != to)
    return "The target D3D11 shader is already owned by another replacement";

  // D3D11 binds shader objects directly: there is no derived PSO to recreate. The workbench's
  // full ABI gate remains separate; this native transaction proves identity, stage and creation
  // structure. Do not delegate to the unchecked ReplaceResource entry point.
  if(hadPrevious)
    rm->RemoveReplacement(from);
  rm->ReplaceResource(from, to);
  if(!rm->HasReplacement(from) || rm->GetResource(from, true) != target ||
     rm->GetUnreplacedID(to) != from)
  {
    rm->RemoveReplacement(from);
    if(hadPrevious)
      rm->ReplaceResource(from, previousId);
    return "Installing the checked D3D11 shader mapping failed; the previous mapping was restored";
  }
  InvalidateReplayCheckpoints(0);
  ClearPostVSCache();
  return {};
}

rdcstr D3D11Replay::RemoveTargetShaderReplacementChecked(ResourceId id)
{
  D3D11ResourceManager *rm = m_pDevice->GetResourceManager();
  if(id == ResourceId() || rm->GetUnreplacedID(id) != id || ResourceIDGen::IsReplayOnlyID(id))
    return "The D3D11 shader to restore must be the canonical captured ResourceId";
  if(!m_AppliedEventShaderOverrides.empty() || !m_AppliedEventCBufferOverrides.empty() ||
     m_RealState.active)
    return "Temporary D3D11 event overrides must be restored before restoring a shared shader";
  ID3D11DeviceChild *captured = rm->GetResourceWithoutReplacement(id, true);
  if(!captured || GetIDForDeviceChild(captured) != id ||
     D3D11NativeShaderStage(captured) == ShaderStage::Count ||
     WrappedShader::m_ShaderList.find(id) == WrappedShader::m_ShaderList.end())
    return "Checked D3D11 shader restoration requires a live captured shader object";
  if(!rm->HasReplacement(id))
    return {};
  ID3D11DeviceChild *previous = rm->GetResource(id, true);
  if(!previous || D3D11NativeShaderStage(previous) != D3D11NativeShaderStage(captured))
    return "The active D3D11 replacement is not a live shader of the captured stage";
  const ResourceId previousId = GetIDForDeviceChild(previous);
  if(previousId == id || rm->GetUnreplacedID(previousId) != id)
    return "The captured D3D11 shader has an invalid replacement ownership mapping";

  rm->RemoveReplacement(id);
  if(rm->HasReplacement(id) || rm->GetResource(id, true) != captured)
  {
    rm->ReplaceResource(id, previousId);
    return "Restoring the captured D3D11 shader mapping failed; the previous mapping was restored";
  }
  InvalidateReplayCheckpoints(0);
  ClearPostVSCache();
  return {};
}

void D3D11Replay::ReplaceResource(ResourceId from, ResourceId to)
{
  auto fromit = WrappedShader::m_ShaderList.find(from);

  if(fromit != WrappedShader::m_ShaderList.end())
  {
    auto toit = WrappedShader::m_ShaderList.find(to);

    // copy the shader ext slot
    toit->second->SetShaderExtSlot(fromit->second->GetShaderExtSlot());
  }

  m_pDevice->GetResourceManager()->ReplaceResource(from, to);
  ClearPostVSCache();
}

void D3D11Replay::RemoveReplacement(ResourceId id)
{
  m_pDevice->GetResourceManager()->RemoveReplacement(id);
  ClearPostVSCache();
}

void D3D11Replay::ClearReplayCache()
{
  ClearPostVSCache();
}

void D3D11Replay::ReloadShaderDebugInformation()
{
  DXBC::ResetSearchDirsCache();
  WrappedID3D11Shader<ID3D11ComputeShader>::ReloadShaderDebugInformation();
  ClearReplayCache();
}

D3D11DebugManager *D3D11Replay::GetDebugManager()
{
  return m_pDevice->GetDebugManager();
}

void D3D11Replay::BuildShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                              const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                              const rdcarray<rdcstr> &includeDirs, ShaderStage type, ResourceId &id,
                              rdcstr &errors)
{
  bytebuf compiledDXBC;

  const byte *dxbcBytes = source.data();
  size_t dxbcLength = source.size();

  if(sourceEncoding == ShaderEncoding::HLSL)
  {
    uint32_t flags = DXBC::DecodeFlags(compileFlags);
    rdcstr profile = DXBC::GetProfile(compileFlags);

    if(profile.empty())
    {
      switch(type)
      {
        case ShaderStage::Vertex: profile = "vs_5_0"; break;
        case ShaderStage::Hull: profile = "hs_5_0"; break;
        case ShaderStage::Domain: profile = "ds_5_0"; break;
        case ShaderStage::Geometry: profile = "gs_5_0"; break;
        case ShaderStage::Pixel: profile = "ps_5_0"; break;
        case ShaderStage::Compute: profile = "cs_5_0"; break;
        default:
          RDCERR("Unexpected type in BuildShader!");
          id = ResourceId();
          return;
      }
    }

    rdcstr hlsl;
    hlsl.assign((const char *)source.data(), source.size());

    ID3DBlob *blob = NULL;

    errors = m_pDevice->GetShaderCache()->GetShaderBlob(hlsl.c_str(), entry.c_str(), flags,
                                                        includeDirs, profile.c_str(), &blob);

    if(blob == NULL)
    {
      id = ResourceId();
      return;
    }

    compiledDXBC.assign((byte *)blob->GetBufferPointer(), blob->GetBufferSize());

    dxbcBytes = compiledDXBC.data();
    dxbcLength = compiledDXBC.size();

    SAFE_RELEASE(blob);
  }

  switch(type)
  {
    case ShaderStage::Vertex:
    {
      ID3D11VertexShader *sh = NULL;
      HRESULT hr = m_pDevice->CreateVertexShader(dxbcBytes, dxbcLength, NULL, &sh);

      if(sh != NULL)
      {
        id = ((WrappedID3D11Shader<ID3D11VertexShader> *)sh)->GetResourceID();
      }
      else
      {
        errors = StringFormat::Fmt("Failed to create shader: %s", ToStr(hr).c_str());
        id = ResourceId();
      }
      return;
    }
    case ShaderStage::Hull:
    {
      ID3D11HullShader *sh = NULL;
      HRESULT hr = m_pDevice->CreateHullShader(dxbcBytes, dxbcLength, NULL, &sh);

      if(sh != NULL)
      {
        id = ((WrappedID3D11Shader<ID3D11HullShader> *)sh)->GetResourceID();
      }
      else
      {
        errors = StringFormat::Fmt("Failed to create shader: %s", ToStr(hr).c_str());
        id = ResourceId();
      }
      return;
    }
    case ShaderStage::Domain:
    {
      ID3D11DomainShader *sh = NULL;
      HRESULT hr = m_pDevice->CreateDomainShader(dxbcBytes, dxbcLength, NULL, &sh);

      if(sh != NULL)
      {
        id = ((WrappedID3D11Shader<ID3D11DomainShader> *)sh)->GetResourceID();
      }
      else
      {
        errors = StringFormat::Fmt("Failed to create shader: %s", ToStr(hr).c_str());
        id = ResourceId();
      }
      return;
    }
    case ShaderStage::Geometry:
    {
      ID3D11GeometryShader *sh = NULL;
      HRESULT hr = m_pDevice->CreateGeometryShader(dxbcBytes, dxbcLength, NULL, &sh);

      if(sh != NULL)
      {
        id = ((WrappedID3D11Shader<ID3D11GeometryShader> *)sh)->GetResourceID();
      }
      else
      {
        errors = StringFormat::Fmt("Failed to create shader: %s", ToStr(hr).c_str());
        id = ResourceId();
      }
      return;
    }
    case ShaderStage::Pixel:
    {
      ID3D11PixelShader *sh = NULL;
      HRESULT hr = m_pDevice->CreatePixelShader(dxbcBytes, dxbcLength, NULL, &sh);

      if(sh != NULL)
      {
        id = ((WrappedID3D11Shader<ID3D11PixelShader> *)sh)->GetResourceID();
      }
      else
      {
        errors = StringFormat::Fmt("Failed to create shader: %s", ToStr(hr).c_str());
        id = ResourceId();
      }
      return;
    }
    case ShaderStage::Compute:
    {
      ID3D11ComputeShader *sh = NULL;
      HRESULT hr = m_pDevice->CreateComputeShader(dxbcBytes, dxbcLength, NULL, &sh);

      if(sh != NULL)
      {
        id = ((WrappedID3D11Shader<ID3D11ComputeShader> *)sh)->GetResourceID();
      }
      else
      {
        errors = StringFormat::Fmt("Failed to create shader: %s", ToStr(hr).c_str());
        id = ResourceId();
      }
      return;
    }
    default: break;
  }

  errors = "Unexpected type in BuildShader!";
  id = ResourceId();
}

void D3D11Replay::BuildTargetShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                                    const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                                    ShaderStage type, ResourceId &id, rdcstr &errors)
{
  ShaderCompileFlags debugCompileFlags = DXBC::EncodeFlags(
      DXBC::DecodeFlags(compileFlags) | D3DCOMPILE_DEBUG, DXBC::GetProfile(compileFlags));

  BuildShader(sourceEncoding, source, entry, debugCompileFlags, {}, type, id, errors);
}

void D3D11Replay::SetCustomShaderIncludes(const rdcarray<rdcstr> &directories)
{
  m_CustomShaderIncludes = directories;
}

void D3D11Replay::BuildCustomShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                                    const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                                    ShaderStage type, ResourceId &id, rdcstr &errors)
{
  BuildShader(sourceEncoding, source, entry, compileFlags, m_CustomShaderIncludes, type, id, errors);
}

bool D3D11Replay::RenderTexture(TextureDisplay cfg)
{
  return RenderTextureInternal(cfg, eTexDisplay_BlendAlpha);
}

void D3D11Replay::RenderCheckerboard(FloatVector dark, FloatVector light)
{
  D3D11RenderStateTracker tracker(m_pImmediateContext);

  CheckerboardCBuffer pixelData = {};

  pixelData.PrimaryColor = ConvertSRGBToLinear(dark);
  pixelData.SecondaryColor = ConvertSRGBToLinear(light);
  pixelData.CheckerSquareDimension = 64.0f;

  ID3D11Buffer *psBuf = GetDebugManager()->MakeCBuffer(&pixelData, sizeof(pixelData));

  // can't just clear state because we need to keep things like render targets.
  {
    m_pImmediateContext->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    m_pImmediateContext->IASetInputLayout(NULL);

    m_pImmediateContext->VSSetShader(m_General.FullscreenVS, NULL, 0);

    m_pImmediateContext->HSSetShader(NULL, NULL, 0);
    m_pImmediateContext->DSSetShader(NULL, NULL, 0);
    m_pImmediateContext->GSSetShader(NULL, NULL, 0);

    m_pImmediateContext->RSSetState(m_General.RasterState);

    m_pImmediateContext->PSSetShader(m_General.CheckerboardPS, NULL, 0);
    m_pImmediateContext->PSSetConstantBuffers(0, 1, &psBuf);

    float factor[4] = {1.0f, 1.0f, 1.0f, 1.0f};
    m_pImmediateContext->OMSetBlendState(NULL, factor, 0xffffffff);
    m_pImmediateContext->OMSetDepthStencilState(NULL, 0);

    m_pImmediateContext->Draw(4, 0);
  }
}

void D3D11Replay::RenderHighlightBox(float w, float h, float scale)
{
  D3D11RenderStateTracker tracker(m_pImmediateContext);

  ID3D11Buffer *pconst = NULL;

  m_pImmediateContext->HSSetShader(NULL, NULL, 0);
  m_pImmediateContext->DSSetShader(NULL, NULL, 0);
  m_pImmediateContext->GSSetShader(NULL, NULL, 0);

  m_pImmediateContext->RSSetState(m_General.RasterScissorState);

  m_pImmediateContext->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
  m_pImmediateContext->IASetInputLayout(NULL);

  m_pImmediateContext->VSSetShader(m_General.FullscreenVS, NULL, 0);
  m_pImmediateContext->PSSetShader(m_General.FixedColPS, NULL, 0);
  m_pImmediateContext->OMSetBlendState(NULL, NULL, 0xffffffff);

  float black[] = {0.0f, 0.0f, 0.0f, 1.0f};
  float white[] = {1.0f, 1.0f, 1.0f, 1.0f};

  // size of box
  LONG sz = LONG(scale);

  // top left, x and y
  LONG tlx = LONG(w / 2.0f + 0.5f);
  LONG tly = LONG(h / 2.0f + 0.5f);

  D3D11_RECT rect[4] = {
      {tlx, tly, tlx + 1, tly + sz},

      {tlx + sz, tly, tlx + sz + 1, tly + sz + 1},

      {tlx, tly, tlx + sz, tly + 1},

      {tlx, tly + sz, tlx + sz, tly + sz + 1},
  };

  // inner
  pconst = GetDebugManager()->MakeCBuffer(white, sizeof(white));

  // render the rects
  for(size_t i = 0; i < ARRAY_COUNT(rect); i++)
  {
    m_pImmediateContext->RSSetScissorRects(1, &rect[i]);
    m_pImmediateContext->PSSetConstantBuffers(0, 1, &pconst);
    m_pImmediateContext->Draw(4, 0);
  }

  // shift both sides to just translate the rect without changing its size
  rect[0].left--;
  rect[0].right--;
  rect[1].left++;
  rect[1].right++;
  rect[2].left--;
  rect[2].right--;
  rect[3].left--;
  rect[3].right--;

  rect[0].top--;
  rect[0].bottom--;
  rect[1].top--;
  rect[1].bottom--;
  rect[2].top--;
  rect[2].bottom--;
  rect[3].top++;
  rect[3].bottom++;

  // now increase the 'size' of the rects
  rect[0].bottom += 2;
  rect[1].bottom += 2;
  rect[2].right += 2;
  rect[3].right += 2;

  // render the rects
  pconst = GetDebugManager()->MakeCBuffer(black, sizeof(black));

  for(size_t i = 0; i < ARRAY_COUNT(rect); i++)
  {
    m_pImmediateContext->RSSetScissorRects(1, &rect[i]);
    m_pImmediateContext->PSSetConstantBuffers(0, 1, &pconst);
    m_pImmediateContext->Draw(4, 0);
  }
}

void D3D11Replay::FillCBufferVariables(ResourceId pipeline, ResourceId shader, ShaderStage stage,
                                       rdcstr entryPoint, uint32_t cbufSlot,
                                       rdcarray<ShaderVariable> &outvars, const bytebuf &data)
{
  auto it = WrappedShader::m_ShaderList.find(shader);

  if(it == WrappedShader::m_ShaderList.end())
    return;

  const ShaderReflection &refl = it->second->GetDetails();

  if(cbufSlot >= (uint32_t)refl.constantBlocks.count())
  {
    RDCERR("Invalid cbuffer slot");
    return;
  }

  StandardFillCBufferVariables(refl.resourceId, refl.constantBlocks[cbufSlot].variables, outvars,
                               data);
}

uint32_t D3D11Replay::PickVertex(uint32_t eventId, int32_t width, int32_t height,
                                 const MeshDisplay &cfg, uint32_t x, uint32_t y)
{
  if(cfg.position.numIndices == 0)
    return ~0U;

  D3D11RenderStateTracker tracker(m_pImmediateContext);

  MeshPickData cbuf = {};

  cbuf.PickCoords = Vec2f((float)x, (float)y);
  cbuf.PickViewport = Vec2f((float)width, (float)height);
  cbuf.PickIdx = cfg.position.indexByteStride ? 1 : 0;
  cbuf.PickNumVerts = cfg.position.numIndices;
  cbuf.PickUnproject = cfg.position.unproject ? 1 : 0;
  cbuf.PickFlipY = cfg.position.flipY;
  cbuf.PickOrtho = cfg.ortho;

  Matrix4f projMat = Camera::MeshProjection(cfg.cam, float(width) / float(height));

  Matrix4f camMat = cfg.cam ? ((Camera *)cfg.cam)->GetMatrix() : Matrix4f::Identity();

  Matrix4f pickMVP = projMat.Mul(camMat);
  if(!cfg.position.unproject)
  {
    pickMVP = pickMVP.Mul(Matrix4f(cfg.axisMapping));
  }

  bool reverseProjection = false;
  Matrix4f guessProj;
  Matrix4f guessProjInverse;
  if(cfg.position.unproject)
  {
    // the derivation of the projection matrix might not be right (hell, it could be an
    // orthographic projection). But it'll be close enough likely.
    if(cfg.position.farPlane != FLT_MAX)
    {
      guessProj =
          Matrix4f::Perspective(cfg.fov, cfg.position.nearPlane, cfg.position.farPlane, cfg.aspect);
    }
    else
    {
      reverseProjection = true;
      guessProj = Matrix4f::ReversePerspective(cfg.fov, cfg.position.nearPlane, cfg.aspect);
    }

    if(cfg.ortho)
      guessProj = Matrix4f::Orthographic(cfg.position.nearPlane, cfg.position.farPlane);

    if(cfg.position.flipY)
      guessProj[5] *= -1.0f;

    guessProjInverse = guessProj.Inverse();
  }

  Vec3f rayPos;
  Vec3f rayDir;
  // convert mouse pos to world space ray
  {
    float pickX = ((float)x) / ((float)width);
    float pickXCanonical = RDCLERP(-1.0f, 1.0f, pickX);

    float pickY = ((float)y) / ((float)height);
    // flip the Y axis by default for Y-up
    float pickYCanonical = RDCLERP(1.0f, -1.0f, pickY);

    if(cfg.position.flipY && !cfg.ortho)
      pickYCanonical = -pickYCanonical;

    // x/y is inside the window. Since we're not using the window projection we need to correct
    // for the aspect ratio here.
    if(cfg.position.unproject && !cfg.ortho)
      pickXCanonical *= (float(width) / float(height)) / cfg.aspect;

    // set up the NDC near/far pos
    Vec3f nearPosNDC = Vec3f(pickXCanonical, pickYCanonical, 0);
    // A small preview near plane can round the depth at infinity to 1.
    // A second interior sample gives the same ray without a zero homogeneous W.
    Vec3f farPosNDC = Vec3f(pickXCanonical, pickYCanonical, 0.999f);

    if(cfg.position.unproject && cfg.ortho)
    {
      // orthographic projections we raycast in NDC space
      Matrix4f inversePickMVP = pickMVP.Inverse();

      // transform from the desired NDC co-ordinates into camera space
      Vec3f nearPosCamera = inversePickMVP.Transform(nearPosNDC, 1);
      Vec3f farPosCamera = inversePickMVP.Transform(farPosNDC, 1);

      Vec3f testDir = (farPosCamera - nearPosCamera);
      testDir.Normalise();

      Matrix4f pickMVPguessProjInverse = guessProj.Mul(inversePickMVP);

      Vec3f nearPosProj = pickMVPguessProjInverse.Transform(nearPosNDC, 1);
      Vec3f farPosProj = pickMVPguessProjInverse.Transform(farPosNDC, 1);

      rayDir = (farPosProj - nearPosProj);
      rayDir.Normalise();

      // Calculate the ray direction first in the regular way (above), so we can use the
      // the output for testing if the ray we are picking is negative or not. This is similar
      // to checking against the forward direction of the camera, but more robust
      if(testDir.z < 0)
      {
        rayDir = -rayDir;
      }
      rayPos = nearPosProj;
    }
    else if(cfg.position.unproject)
    {
      // projected data we pick in world-space to avoid problems with handling unusual transforms

      if(reverseProjection)
      {
        farPosNDC.z = 1e-6f;
        nearPosNDC.z = 1e+6f;
      }

      // invert the guessed projection matrix to get the near/far pos in camera space
      Vec3f nearPosCamera = guessProjInverse.Transform(nearPosNDC, 1.0f);
      Vec3f farPosCamera = guessProjInverse.Transform(farPosNDC, 1.0f);

      // normalise and generate the ray
      rayDir = (farPosCamera - nearPosCamera);
      rayDir.Normalise();

      farPosCamera = nearPosCamera + rayDir;

      // invert the camera transform to transform the ray as camera-relative into world space
      Matrix4f inverseCamera = camMat.Inverse();

      Vec3f nearPosWorld = inverseCamera.Transform(nearPosCamera, 1);
      Vec3f farPosWorld = inverseCamera.Transform(farPosCamera, 1);

      // again normalise our final ray
      rayDir = (farPosWorld - nearPosWorld);
      rayDir.Normalise();

      rayPos = nearPosWorld;
    }
    else
    {
      Matrix4f inversePickMVP = pickMVP.Inverse();

      // transform from the desired NDC co-ordinates into model space
      Vec3f nearPosCamera = inversePickMVP.Transform(nearPosNDC, 1);
      Vec3f farPosCamera = inversePickMVP.Transform(farPosNDC, 1);

      rayDir = (farPosCamera - nearPosCamera);
      rayDir.Normalise();
      rayPos = nearPosCamera;
    }
  }

  cbuf.PickRayPos = rayPos;
  cbuf.PickRayDir = rayDir;

  bool isTriangleMesh = true;
  switch(cfg.position.topology)
  {
    case Topology::TriangleList:
    {
      cbuf.PickMeshMode = MESH_TRIANGLE_LIST;
      break;
    }
    case Topology::TriangleStrip:
    {
      cbuf.PickMeshMode = MESH_TRIANGLE_STRIP;
      break;
    }
    case Topology::TriangleList_Adj:
    {
      cbuf.PickMeshMode = MESH_TRIANGLE_LIST_ADJ;
      break;
    }
    case Topology::TriangleStrip_Adj:
    {
      cbuf.PickMeshMode = MESH_TRIANGLE_STRIP_ADJ;
      break;
    }
    default:    // points, lines, patchlists, unknown
    {
      cbuf.PickMeshMode = MESH_OTHER;
      isTriangleMesh = false;
    }
  }

  if(cfg.position.unproject && isTriangleMesh)
  {
    // projected triangle meshes we transform the vertices into world space, and ray-cast against
    // that
    //
    // NOTE: for ortho, this matrix is not used and we just do the perspective W division on model
    // vertices. The ray is cast in NDC
    if(cfg.ortho)
      cbuf.PickTransformMat = Matrix4f::Identity();
    else
      cbuf.PickTransformMat = guessProjInverse;
  }
  else if(cfg.position.unproject)
  {
    // projected non-triangles are just point clouds, so we transform the vertices into world space
    // then project them back onto the output and compare that against the picking 2D co-ordinates
    cbuf.PickTransformMat = pickMVP.Mul(guessProjInverse);
  }
  else
  {
    // plain meshes of either type, we just transform from model space to the output, and raycast or
    // co-ordinate check
    cbuf.PickTransformMat = pickMVP;
  }

  ID3D11Buffer *vb = NULL, *ib = NULL;

  {
    auto it = WrappedID3D11Buffer::m_BufferList.find(cfg.position.vertexResourceId);

    if(it != WrappedID3D11Buffer::m_BufferList.end())
      vb = it->second.m_Buffer;

    it = WrappedID3D11Buffer::m_BufferList.find(cfg.position.indexResourceId);

    if(it != WrappedID3D11Buffer::m_BufferList.end())
      ib = it->second.m_Buffer;
  }

  HRESULT hr = S_OK;

  // most IB/VBs will not be available as SRVs. So, we copy into our own buffers. In the case of VB
  // we also tightly pack and unpack the data. IB is upcast to R32 so it we can apply baseVertex
  // without risking overflow.

  uint32_t minIndex = 0;
  uint32_t maxIndex = cfg.position.numIndices;

  uint32_t idxclamp = 0;
  if(cfg.position.baseVertex < 0)
    idxclamp = uint32_t(-cfg.position.baseVertex);

  if(cfg.position.indexByteStride)
  {
    // resize up on demand
    if(m_VertexPick.PickIBBuf == NULL ||
       m_VertexPick.PickIBSize < cfg.position.numIndices * sizeof(uint32_t))
    {
      SAFE_RELEASE(m_VertexPick.PickIBBuf);
      SAFE_RELEASE(m_VertexPick.PickIBSRV);

      D3D11_BUFFER_DESC desc = {cfg.position.numIndices * sizeof(uint32_t),
                                D3D11_USAGE_DEFAULT,
                                D3D11_BIND_SHADER_RESOURCE,
                                0,
                                0,
                                0};

      m_VertexPick.PickIBSize = cfg.position.numIndices * sizeof(uint32_t);

      hr = m_pDevice->CreateBuffer(&desc, NULL, &m_VertexPick.PickIBBuf);

      if(FAILED(hr))
      {
        RDCERR("Failed to create PickIBBuf HRESULT: %s", ToStr(hr).c_str());
        return ~0U;
      }

      D3D11_SHADER_RESOURCE_VIEW_DESC sdesc;
      sdesc.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
      sdesc.Format = DXGI_FORMAT_R32_UINT;
      sdesc.Buffer.FirstElement = 0;
      sdesc.Buffer.NumElements = cfg.position.numIndices;

      hr = m_pDevice->CreateShaderResourceView(m_VertexPick.PickIBBuf, &sdesc,
                                               &m_VertexPick.PickIBSRV);

      if(FAILED(hr))
      {
        SAFE_RELEASE(m_VertexPick.PickIBBuf);
        RDCERR("Failed to create PickIBSRV HRESULT: %s", ToStr(hr).c_str());
        return ~0U;
      }
    }

    RDCASSERT(cfg.position.indexByteOffset < 0xffffffff);

    if(ib)
    {
      bytebuf idxs;
      GetBufferData(cfg.position.indexResourceId, cfg.position.indexByteOffset, 0, idxs);

      rdcarray<uint32_t> outidxs;
      outidxs.resize(cfg.position.numIndices);

      uint16_t *idxs16 = (uint16_t *)&idxs[0];
      uint32_t *idxs32 = (uint32_t *)&idxs[0];

      if(cfg.position.indexByteStride == 2)
      {
        size_t bufsize = idxs.size() / 2;

        for(uint32_t i = 0; i < bufsize && i < cfg.position.numIndices; i++)
        {
          uint32_t idx = idxs16[i];

          if(idx < idxclamp)
            idx = 0;
          else if(cfg.position.baseVertex < 0)
            idx -= idxclamp;
          else if(cfg.position.baseVertex > 0)
            idx += cfg.position.baseVertex;

          if(i == 0)
          {
            minIndex = maxIndex = idx;
          }
          else
          {
            minIndex = RDCMIN(idx, minIndex);
            maxIndex = RDCMAX(idx, maxIndex);
          }

          outidxs[i] = idx;
        }
      }
      else
      {
        uint32_t bufsize = uint32_t(idxs.size() / 4);

        minIndex = maxIndex = idxs32[0];

        for(uint32_t i = 0; i < RDCMIN(bufsize, cfg.position.numIndices); i++)
        {
          uint32_t idx = idxs32[i];

          if(idx < idxclamp)
            idx = 0;
          else if(cfg.position.baseVertex < 0)
            idx -= idxclamp;
          else if(cfg.position.baseVertex > 0)
            idx += cfg.position.baseVertex;

          minIndex = RDCMIN(idx, minIndex);
          maxIndex = RDCMAX(idx, maxIndex);

          outidxs[i] = idx;
        }
      }

      D3D11_BOX box;
      box.top = 0;
      box.bottom = 1;
      box.front = 0;
      box.back = 1;
      box.left = 0;
      box.right = UINT(outidxs.size() * sizeof(uint32_t));

      m_pImmediateContext->UpdateSubresource(m_VertexPick.PickIBBuf, 0, &box, outidxs.data(), 0, 0);
    }
  }

  // unpack and linearise the data
  if(vb)
  {
    bytebuf oldData;
    GetDebugManager()->GetBufferData(vb, cfg.position.vertexByteOffset, 0, oldData);

    // clamp maxIndex to upper bound in case we got invalid indices or primitive restart indices
    maxIndex = RDCMIN(maxIndex, uint32_t(oldData.size() / RDCMAX(1U, cfg.position.vertexByteStride)));

    if(m_VertexPick.PickVBBuf == NULL || m_VertexPick.PickVBSize < (maxIndex + 1) * sizeof(Vec4f))
    {
      SAFE_RELEASE(m_VertexPick.PickVBBuf);
      SAFE_RELEASE(m_VertexPick.PickVBSRV);

      D3D11_BUFFER_DESC desc = {
          (maxIndex + 1) * sizeof(Vec4f), D3D11_USAGE_DEFAULT, D3D11_BIND_SHADER_RESOURCE, 0, 0, 0};

      m_VertexPick.PickVBSize = (maxIndex + 1) * sizeof(Vec4f);

      hr = m_pDevice->CreateBuffer(&desc, NULL, &m_VertexPick.PickVBBuf);

      if(FAILED(hr))
      {
        RDCERR("Failed to create PickVBBuf HRESULT: %s", ToStr(hr).c_str());
        return ~0U;
      }

      D3D11_SHADER_RESOURCE_VIEW_DESC sdesc;
      sdesc.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
      sdesc.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
      sdesc.Buffer.FirstElement = 0;
      sdesc.Buffer.NumElements = (maxIndex + 1);

      hr = m_pDevice->CreateShaderResourceView(m_VertexPick.PickVBBuf, &sdesc,
                                               &m_VertexPick.PickVBSRV);

      if(FAILED(hr))
      {
        SAFE_RELEASE(m_VertexPick.PickVBBuf);
        RDCERR("Failed to create PickVBSRV HRESULT: %s", ToStr(hr).c_str());
        return ~0U;
      }
    }

    rdcarray<FloatVector> vbData;
    vbData.resize(maxIndex + 1);

    byte *data = &oldData[0];
    byte *dataEnd = data + oldData.size();

    bool valid;

    // the index buffer may refer to vertices past the start of the vertex buffer, so we can't just
    // conver the first N vertices we'll need.
    // Instead we grab min and max above, and convert every vertex in that range. This might
    // slightly over-estimate but not as bad as 0-max or the whole buffer.
    for(uint32_t idx = minIndex; idx <= maxIndex; idx++)
      vbData[idx] = HighlightCache::InterpretVertex(data, idx, cfg.position.vertexByteStride,
                                                    cfg.position.format, dataEnd, valid);

    D3D11_BOX box;
    box.top = 0;
    box.bottom = 1;
    box.front = 0;
    box.back = 1;
    box.left = 0;
    box.right = (maxIndex + 1) * sizeof(Vec4f);

    m_pImmediateContext->UpdateSubresource(m_VertexPick.PickVBBuf, 0, &box, vbData.data(),
                                           sizeof(Vec4f), sizeof(Vec4f));
  }

  ID3D11ShaderResourceView *srvs[2] = {m_VertexPick.PickIBSRV, m_VertexPick.PickVBSRV};

  ID3D11Buffer *buf = GetDebugManager()->MakeCBuffer(&cbuf, sizeof(cbuf));

  m_pImmediateContext->CSSetConstantBuffers(0, 1, &buf);

  m_pImmediateContext->CSSetShaderResources(0, 2, srvs);

  UINT reset = 0;
  m_pImmediateContext->CSSetUnorderedAccessViews(0, 1, &m_VertexPick.PickResultUAV, &reset);

  m_pImmediateContext->CSSetShader(m_VertexPick.MeshPickCS, NULL, 0);

  m_pImmediateContext->Dispatch(cfg.position.numIndices / 1024 + 1, 1, 1);

  uint32_t numResults = GetDebugManager()->GetStructCount(m_VertexPick.PickResultUAV);

  if(numResults > 0)
  {
    bytebuf results;

    if(isTriangleMesh)
    {
      struct PickResult
      {
        uint32_t vertid;
        Vec3f intersectionPoint;
      };

      GetDebugManager()->GetBufferData(m_VertexPick.PickResultBuf, 0, 0, results);

      PickResult *pickResults = (PickResult *)&results[0];

      PickResult *closest = pickResults;

      // distance from raycast hit to nearest worldspace position of the mouse
      float closestPickDistance = (closest->intersectionPoint - rayPos).Length();

      // min with size of results buffer to protect against overflows
      for(uint32_t i = 1; i < RDCMIN((uint32_t)VertexPicking::MaxMeshPicks, numResults); i++)
      {
        float pickDistance = (pickResults[i].intersectionPoint - rayPos).Length();
        if(pickDistance < closestPickDistance)
        {
          closest = pickResults + i;
        }
      }

      return closest->vertid;
    }
    else
    {
      struct PickResult
      {
        uint32_t vertid;
        uint32_t idx;
        float len;
        float depth;
      };

      GetDebugManager()->GetBufferData(m_VertexPick.PickResultBuf, 0, 0, results);

      PickResult *pickResults = (PickResult *)&results[0];

      PickResult *closest = pickResults;

      // min with size of results buffer to protect against overflows
      for(uint32_t i = 1; i < RDCMIN((uint32_t)VertexPicking::MaxMeshPicks, numResults); i++)
      {
        // We need to keep the picking order consistent in the face
        // of random buffer appends, when multiple vertices have the
        // identical position (e.g. if UVs or normals are different).
        //
        // We could do something to try and disambiguate, but it's
        // never going to be intuitive, it's just going to flicker
        // confusingly.
        if(pickResults[i].len < closest->len ||
           (pickResults[i].len == closest->len && pickResults[i].depth < closest->depth) ||
           (pickResults[i].len == closest->len && pickResults[i].depth == closest->depth &&
            pickResults[i].vertid < closest->vertid))
          closest = pickResults + i;
      }

      return closest->vertid;
    }
  }

  return ~0U;
}

void D3D11Replay::CreateCustomShaderTex(uint32_t w, uint32_t h)
{
  D3D11_TEXTURE2D_DESC texdesc;

  texdesc.ArraySize = 1;
  texdesc.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
  texdesc.CPUAccessFlags = 0;
  texdesc.MipLevels = CalcNumMips((int)w, (int)h, 1);
  texdesc.MiscFlags = 0;
  texdesc.SampleDesc.Count = 1;
  texdesc.SampleDesc.Quality = 0;
  texdesc.Usage = D3D11_USAGE_DEFAULT;
  texdesc.Width = w;
  texdesc.Height = h;
  texdesc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;

  if(m_CustomShaderTex)
  {
    D3D11_TEXTURE2D_DESC customTexDesc;
    m_CustomShaderTex->GetDesc(&customTexDesc);

    if(customTexDesc.Width == w && customTexDesc.Height == h)
      return;

    SAFE_RELEASE(m_CustomShaderTex);
  }

  HRESULT hr = m_pDevice->CreateTexture2D(&texdesc, NULL, &m_CustomShaderTex);

  if(FAILED(hr))
  {
    RDCERR("Failed to create custom shader tex HRESULT: %s", ToStr(hr).c_str());
  }
  else
  {
    m_CustomShaderResourceId = GetIDForDeviceChild(m_CustomShaderTex);
  }
}

ResourceId D3D11Replay::ApplyCustomShader(TextureDisplay &display)
{
  TextureShaderDetails details =
      GetDebugManager()->GetShaderDetails(display.resourceId, display.typeCast, false);

  CreateCustomShaderTex(details.texWidth, details.texHeight);

  D3D11RenderStateTracker tracker(m_pImmediateContext);

  ID3D11RenderTargetView *customRTV = NULL;

  {
    D3D11_RENDER_TARGET_VIEW_DESC desc;

    desc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    desc.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
    desc.Texture2D.MipSlice = display.subresource.mip;

    WrappedID3D11Texture2D1 *wrapped = (WrappedID3D11Texture2D1 *)m_CustomShaderTex;
    HRESULT hr = m_pDevice->CreateRenderTargetView(wrapped, &desc, &customRTV);

    if(FAILED(hr))
    {
      RDCERR("Failed to create custom shader rtv HRESULT: %s", ToStr(hr).c_str());
      return m_CustomShaderResourceId;
    }
  }

  m_pImmediateContext->OMSetRenderTargets(1, &customRTV, NULL);

  float clr[] = {0.0f, 0.0f, 0.0f, 0.0f};
  m_pImmediateContext->ClearRenderTargetView(customRTV, clr);

  SAFE_RELEASE(customRTV);

  D3D11_VIEWPORT viewport;
  RDCEraseEl(viewport);

  viewport.TopLeftX = 0;
  viewport.TopLeftY = 0;
  viewport.Width = (float)RDCMAX(1U, details.texWidth >> display.subresource.mip);
  viewport.Height = (float)RDCMAX(1U, details.texHeight >> display.subresource.mip);

  m_pImmediateContext->RSSetViewports(1, &viewport);

  TextureDisplay disp;
  disp.red = disp.green = disp.blue = disp.alpha = true;
  disp.flipY = false;
  disp.xOffset = 0.0f;
  disp.yOffset = 0.0f;
  disp.customShaderId = display.customShaderId;
  disp.resourceId = display.resourceId;
  disp.typeCast = display.typeCast;
  disp.backgroundColor = FloatVector(0, 0, 0, 1.0);
  disp.hdrMultiplier = -1.0f;
  disp.linearDisplayAsGamma = false;
  disp.subresource = display.subresource;
  disp.overlay = DebugOverlay::NoOverlay;
  disp.rangeMin = display.rangeMin;
  disp.rangeMax = display.rangeMax;
  disp.rawOutput = false;
  disp.scale = 1.0f;

  SetOutputDimensions(RDCMAX(1U, details.texWidth), RDCMAX(1U, details.texHeight));

  RenderTextureInternal(disp, eTexDisplay_BlendAlpha);

  return m_CustomShaderResourceId;
}

ResourceId D3D11Replay::CreateProxyTexture(const TextureDescription &templateTex)
{
  ResourceId ret;

  ID3D11Resource *resource = NULL;

  if(templateTex.dimension == 1)
  {
    ID3D11Texture1D *throwaway = NULL;
    D3D11_TEXTURE1D_DESC desc;

    desc.ArraySize = templateTex.arraysize;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    desc.CPUAccessFlags = 0;
    desc.Format = GetTypelessFormat(MakeDXGIFormat(templateTex.format));
    desc.MipLevels = templateTex.mips;
    desc.MiscFlags = 0;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.Width = RDCMAX(1U, templateTex.width);

    if(IsDepthFormat(desc.Format))
      desc.BindFlags |= D3D11_BIND_DEPTH_STENCIL;

    HRESULT hr = m_pDevice->CreateTexture1D(&desc, NULL, &throwaway);
    if(FAILED(hr))
    {
      RDCERR("Failed to create 1D proxy texture");
      return ResourceId();
    }

    resource = throwaway;

    ret = ((WrappedID3D11Texture1D *)throwaway)->GetResourceID();

    if(templateTex.creationFlags & TextureCategory::DepthTarget)
      WrappedID3D11Texture1D::m_TextureList[ret].m_Type = TEXDISPLAY_DEPTH_TARGET;
  }
  else if(templateTex.dimension == 2)
  {
    ID3D11Texture2D *throwaway = NULL;
    D3D11_TEXTURE2D_DESC desc;

    desc.ArraySize = templateTex.arraysize;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    desc.CPUAccessFlags = 0;
    desc.Format = GetTypelessFormat(MakeDXGIFormat(templateTex.format));
    desc.MipLevels = templateTex.mips;
    desc.MiscFlags = 0;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.Width = RDCMAX(1U, templateTex.width);
    desc.Height = RDCMAX(1U, templateTex.height);
    desc.SampleDesc.Count = RDCMAX(1U, templateTex.msSamp);
    desc.SampleDesc.Quality = templateTex.msQual;

    if(IsDepthFormat(desc.Format))
      desc.BindFlags |= D3D11_BIND_DEPTH_STENCIL;

    if(templateTex.cubemap)
      desc.MiscFlags |= D3D11_RESOURCE_MISC_TEXTURECUBE;

    if(IsBlockFormat(desc.Format))
    {
      desc.Width = AlignUp4(desc.Width);
      desc.Height = AlignUp4(desc.Height);
    }

    HRESULT hr = m_pDevice->CreateTexture2D(&desc, NULL, &throwaway);
    if(FAILED(hr))
    {
      RDCERR("Failed to create 2D proxy texture");
      return ResourceId();
    }

    resource = throwaway;

    ret = ((WrappedID3D11Texture2D1 *)throwaway)->GetResourceID();
    if(templateTex.creationFlags & TextureCategory::DepthTarget)
      WrappedID3D11Texture2D1::m_TextureList[ret].m_Type = TEXDISPLAY_DEPTH_TARGET;
  }
  else if(templateTex.dimension == 3)
  {
    ID3D11Texture3D *throwaway = NULL;
    D3D11_TEXTURE3D_DESC desc;

    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    desc.CPUAccessFlags = 0;
    desc.Format = GetTypelessFormat(MakeDXGIFormat(templateTex.format));
    desc.MipLevels = templateTex.mips;
    desc.MiscFlags = 0;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.Width = RDCMAX(1U, templateTex.width);
    desc.Height = RDCMAX(1U, templateTex.height);
    desc.Depth = RDCMAX(1U, templateTex.depth);

    HRESULT hr = m_pDevice->CreateTexture3D(&desc, NULL, &throwaway);
    if(FAILED(hr))
    {
      RDCERR("Failed to create 3D proxy texture");
      return ResourceId();
    }

    resource = throwaway;

    ret = ((WrappedID3D11Texture3D1 *)throwaway)->GetResourceID();
  }
  else
  {
    RDCERR("Invalid texture dimension: %d", templateTex.dimension);
  }

  if(resource)
    m_ProxyResources.push_back(resource);

  m_ProxyResourceOrigInfo[ret] = templateTex;

  return ret;
}

ResourceId D3D11Replay::CreateProxyTextureSnapshot(const TextureDescription &templateTex)
{
  ResourceId snapshot = CreateProxyTexture(templateTex);
  if(snapshot != ResourceId())
    m_TextureSnapshots.push_back(snapshot);
  return snapshot;
}

ResourceId D3D11Replay::CreateTextureSnapshot(ResourceId source, const Subresource &sourceSub,
                                              CompType typeCast, Subresource &snapshotSub)
{
  (void)typeCast;

  TextureDescription snapshotDesc = GetTexture(source);
  if(snapshotDesc.resourceId == ResourceId() || snapshotDesc.width == 0 || snapshotDesc.height == 0)
    return ResourceId();

  ID3D11Resource *sourceReal = NULL;
  UINT sourceSubresource = 0;
  snapshotSub = Subresource();

  auto tex1D = WrappedID3D11Texture1D::m_TextureList.find(source);
  auto tex2D = WrappedID3D11Texture2D1::m_TextureList.find(source);
  auto tex3D = WrappedID3D11Texture3D1::m_TextureList.find(source);

  if(tex1D != WrappedID3D11Texture1D::m_TextureList.end())
  {
    D3D11_TEXTURE1D_DESC desc = {};
    tex1D->second.m_Texture->GetDesc(&desc);
    if(desc.MiscFlags & D3D11_RESOURCE_MISC_TILED)
      return ResourceId();
    const UINT mips = RDCMAX(1U, desc.MipLevels);
    const UINT mip = RDCMIN(sourceSub.mip, mips - 1);
    const UINT slice = RDCMIN(sourceSub.slice, desc.ArraySize - 1);
    sourceSubresource = D3D11CalcSubresource(mip, slice, mips);
    sourceReal = ((WrappedID3D11Texture1D *)tex1D->second.m_Texture)->GetReal();
    snapshotDesc.width = RDCMAX(1U, desc.Width >> mip);
    snapshotDesc.height = snapshotDesc.depth = 1;
    snapshotDesc.arraysize = snapshotDesc.mips = 1;
    snapshotDesc.cubemap = false;
    snapshotDesc.type = TextureType::Texture1D;
  }
  else if(tex2D != WrappedID3D11Texture2D1::m_TextureList.end())
  {
    D3D11_TEXTURE2D_DESC desc = {};
    tex2D->second.m_Texture->GetDesc(&desc);
    // A selected MSAA sample is not a copyable D3D11 subresource. Keep it on the existing
    // readback/resolve fallback instead of silently snapshotting all samples.
    if(desc.SampleDesc.Count > 1 || (desc.MiscFlags & D3D11_RESOURCE_MISC_TILED))
      return ResourceId();
    const UINT mips = RDCMAX(1U, desc.MipLevels);
    const UINT mip = RDCMIN(sourceSub.mip, mips - 1);
    const UINT slice = RDCMIN(sourceSub.slice, desc.ArraySize - 1);
    sourceSubresource = D3D11CalcSubresource(mip, slice, mips);
    sourceReal = ((WrappedID3D11Texture2D1 *)tex2D->second.m_Texture)->GetReal();
    snapshotDesc.width = RDCMAX(1U, desc.Width >> mip);
    snapshotDesc.height = RDCMAX(1U, desc.Height >> mip);
    snapshotDesc.depth = 1;
    snapshotDesc.arraysize = snapshotDesc.mips = 1;
    snapshotDesc.cubemap = false;
    snapshotDesc.type = TextureType::Texture2D;
  }
  else if(tex3D != WrappedID3D11Texture3D1::m_TextureList.end())
  {
    D3D11_TEXTURE3D_DESC desc = {};
    tex3D->second.m_Texture->GetDesc(&desc);
    if(desc.MiscFlags & D3D11_RESOURCE_MISC_TILED)
      return ResourceId();
    const UINT mips = RDCMAX(1U, desc.MipLevels);
    const UINT mip = RDCMIN(sourceSub.mip, mips - 1);
    sourceSubresource = mip;
    sourceReal = ((WrappedID3D11Texture3D1 *)tex3D->second.m_Texture)->GetReal();
    snapshotDesc.width = RDCMAX(1U, desc.Width >> mip);
    snapshotDesc.height = RDCMAX(1U, desc.Height >> mip);
    snapshotDesc.depth = RDCMAX(1U, desc.Depth >> mip);
    snapshotDesc.arraysize = snapshotDesc.mips = 1;
    snapshotDesc.cubemap = false;
    snapshotDesc.type = TextureType::Texture3D;
    snapshotSub.slice = RDCMIN(sourceSub.slice, snapshotDesc.depth - 1);
  }
  else
  {
    return ResourceId();
  }

  ResourceId snapshot = CreateProxyTextureSnapshot(snapshotDesc);
  if(snapshot == ResourceId())
    return ResourceId();

  ID3D11Resource *snapshotReal = NULL;
  auto dst1D = WrappedID3D11Texture1D::m_TextureList.find(snapshot);
  auto dst2D = WrappedID3D11Texture2D1::m_TextureList.find(snapshot);
  auto dst3D = WrappedID3D11Texture3D1::m_TextureList.find(snapshot);
  if(dst1D != WrappedID3D11Texture1D::m_TextureList.end())
    snapshotReal = ((WrappedID3D11Texture1D *)dst1D->second.m_Texture)->GetReal();
  else if(dst2D != WrappedID3D11Texture2D1::m_TextureList.end())
    snapshotReal = ((WrappedID3D11Texture2D1 *)dst2D->second.m_Texture)->GetReal();
  else if(dst3D != WrappedID3D11Texture3D1::m_TextureList.end())
    snapshotReal = ((WrappedID3D11Texture3D1 *)dst3D->second.m_Texture)->GetReal();

  if(snapshotReal == NULL)
  {
    FreeTextureSnapshot(snapshot);
    return ResourceId();
  }

  // The destination contains exactly one mip/array subresource. Depth/stencil resources use the
  // whole-subresource copy required by D3D11.
  m_pImmediateContext->GetReal()->CopySubresourceRegion(snapshotReal, 0, 0, 0, 0, sourceReal,
                                                        sourceSubresource, NULL);
  return snapshot;
}

void D3D11Replay::FreeTextureSnapshot(ResourceId snapshot)
{
  rdcarray<ResourceId> snapshots;
  snapshots.push_back(snapshot);
  FreeTextureSnapshots(snapshots);
}

void D3D11Replay::FreeTextureSnapshots(const rdcarray<ResourceId> &snapshots)
{
  for(ResourceId snapshot : snapshots)
  {
    for(size_t snapshotIndex = 0; snapshotIndex < m_TextureSnapshots.size(); snapshotIndex++)
    {
      if(m_TextureSnapshots[snapshotIndex] != snapshot)
        continue;

      for(size_t resourceIndex = 0; resourceIndex < m_ProxyResources.size(); resourceIndex++)
      {
        if(GetIDForDeviceChild(m_ProxyResources[resourceIndex]) == snapshot)
        {
          ID3D11Resource *resource = m_ProxyResources[resourceIndex];
          m_ProxyResources.erase(resourceIndex);
          resource->Release();
          break;
        }
      }

      m_ProxyResourceOrigInfo.erase(snapshot);
      m_TextureSnapshots.erase(snapshotIndex);
      break;
    }
  }
}

void D3D11Replay::SetProxyTextureData(ResourceId texid, const Subresource &sub, byte *data,
                                      size_t dataSize)
{
  if(texid == ResourceId())
    return;

  ID3D11DeviceContext *ctx = m_pDevice->GetImmediateContext()->GetReal();

  if(WrappedID3D11Texture1D::m_TextureList.find(texid) != WrappedID3D11Texture1D::m_TextureList.end())
  {
    WrappedID3D11Texture1D *tex =
        (WrappedID3D11Texture1D *)WrappedID3D11Texture1D::m_TextureList[texid].m_Texture;

    D3D11_TEXTURE1D_DESC desc;
    tex->GetDesc(&desc);

    uint32_t mips = desc.MipLevels ? desc.MipLevels : CalcNumMips(desc.Width, 1, 1);

    uint32_t mip = RDCMIN(sub.mip, mips - 1);
    uint32_t slice = RDCMIN(sub.slice, desc.ArraySize - 1);

    if(dataSize < GetByteSize(desc.Width, 1, 1, desc.Format, mip))
    {
      RDCERR("Insufficient data provided to SetProxyTextureData");
      return;
    }

    ctx->UpdateSubresource(tex->GetReal(), slice * mips + mip, NULL, data,
                           GetRowPitch(desc.Width, desc.Format, mip),
                           GetByteSize(desc.Width, 1, 1, desc.Format, mip));
  }
  else if(WrappedID3D11Texture2D1::m_TextureList.find(texid) !=
          WrappedID3D11Texture2D1::m_TextureList.end())
  {
    WrappedID3D11Texture2D1 *tex =
        (WrappedID3D11Texture2D1 *)WrappedID3D11Texture2D1::m_TextureList[texid].m_Texture;

    D3D11_TEXTURE2D_DESC desc;
    tex->GetDesc(&desc);

    UINT width = desc.Width;
    UINT height = desc.Height;

    // for block formats we might have padded up to a multiple of four if the proxy texture wasn't
    // originally a multiple of 4x4 in the top mip. We need to use the original dimensions for
    // calculating expected data sizes and pitches
    auto it = m_ProxyResourceOrigInfo.find(texid);
    if(it == m_ProxyResourceOrigInfo.end())
    {
      RDCERR(
          "Expected proxy resource original info for texture being set with SetProxyTextureData");
    }
    else
    {
      width = it->second.width;
      height = it->second.height;
    }

    uint32_t mips = desc.MipLevels ? desc.MipLevels : CalcNumMips(width, height, 1);

    UINT sampleCount = RDCMAX(1U, desc.SampleDesc.Count);

    uint32_t mip = RDCMIN(sub.mip, mips - 1);
    uint32_t slice = RDCMIN(sub.slice, desc.ArraySize - 1);
    uint32_t sample = RDCMIN(sub.sample, sampleCount - 1);

    if(dataSize < GetByteSize(width, height, 1, desc.Format, mip))
    {
      RDCERR("Insufficient data provided to SetProxyTextureData");
      return;
    }

    if(sampleCount > 1)
    {
      D3D11_TEXTURE2D_DESC uploadDesc = desc;
      uploadDesc.MiscFlags = 0;
      uploadDesc.CPUAccessFlags = 0;
      uploadDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
      uploadDesc.Usage = D3D11_USAGE_DEFAULT;
      uploadDesc.SampleDesc.Count = 1;
      uploadDesc.SampleDesc.Quality = 0;
      uploadDesc.ArraySize *= desc.SampleDesc.Count;

      UINT unpackedSlice = slice * desc.SampleDesc.Count + sample;

      // create an unwrapped texture to upload the data into a slice of
      ID3D11Texture2D *uploadTex = NULL;
      m_pDevice->GetReal()->CreateTexture2D(&uploadDesc, NULL, &uploadTex);

      ctx->UpdateSubresource(uploadTex, unpackedSlice, NULL, data,
                             GetRowPitch(width, desc.Format, mip),
                             GetByteSize(width, desc.Height, 1, desc.Format, mip));

      // copy that slice into MSAA sample
      GetDebugManager()->CopyArrayToTex2DMS(tex->GetReal(), uploadTex, unpackedSlice);

      uploadTex->Release();
    }
    else
    {
      ctx->UpdateSubresource(tex->GetReal(), slice * mips + mip, NULL, data,
                             GetRowPitch(width, desc.Format, mip),
                             GetByteSize(width, height, 1, desc.Format, mip));
    }
  }
  else if(WrappedID3D11Texture3D1::m_TextureList.find(texid) !=
          WrappedID3D11Texture3D1::m_TextureList.end())
  {
    WrappedID3D11Texture3D1 *tex =
        (WrappedID3D11Texture3D1 *)WrappedID3D11Texture3D1::m_TextureList[texid].m_Texture;

    D3D11_TEXTURE3D_DESC desc;
    tex->GetDesc(&desc);

    uint32_t mips =
        desc.MipLevels ? desc.MipLevels : CalcNumMips(desc.Width, desc.Height, desc.Depth);

    uint32_t mip = RDCMIN(sub.mip, mips - 1);

    if(dataSize < GetByteSize(desc.Width, desc.Height, desc.Depth, desc.Format, mip))
    {
      RDCERR("Insufficient data provided to SetProxyTextureData");
      return;
    }

    ctx->UpdateSubresource(tex->GetReal(), mip, NULL, data, GetRowPitch(desc.Width, desc.Format, mip),
                           GetByteSize(desc.Width, desc.Height, 1, desc.Format, mip));
  }
  else
  {
    RDCERR("Invalid texture id passed to SetProxyTextureData");
  }
}

bool D3D11Replay::IsTextureSupported(const TextureDescription &tex)
{
  DXGI_FORMAT f = MakeDXGIFormat(tex.format);

  if(f == DXGI_FORMAT_UNKNOWN)
    return false;

  // if we get a typeless format back for a non-typeless format descriptor then we don't support
  // this component type.
  if(IsTypelessFormat(f) && tex.format.compType != CompType::Typeless)
    return false;

  if(!IsDepthFormat(f))
    f = GetTypelessFormat(f);
  else
    f = GetDepthTypedFormat(f);

  // CheckFormatSupport doesn't like returning MSAA support for typeless formats, if we're thinking
  // about MSAA ensure we query a typed format.
  if(tex.msSamp > 1)
    f = GetTypedFormat(f);

  UINT supp = 0;
  m_pDevice->CheckFormatSupport(f, &supp);

  if(tex.dimension == 1 && (supp & D3D11_FORMAT_SUPPORT_TEXTURE1D) == 0)
    return false;
  if(tex.dimension == 2 && (supp & D3D11_FORMAT_SUPPORT_TEXTURE2D) == 0)
    return false;
  if(tex.dimension == 3 && (supp & D3D11_FORMAT_SUPPORT_TEXTURE3D) == 0)
    return false;
  if(tex.msSamp > 1 && (supp & (D3D11_FORMAT_SUPPORT_MULTISAMPLE_LOAD |
                                D3D11_FORMAT_SUPPORT_MULTISAMPLE_RENDERTARGET)) == 0)
    return false;

  return true;
}

bool D3D11Replay::NeedRemapForFetch(const ResourceFormat &format)
{
  return false;
}

ResourceId D3D11Replay::CreateProxyBuffer(const BufferDescription &templateBuf)
{
  ResourceId ret;

  ID3D11Resource *resource = NULL;

  {
    ID3D11Buffer *throwaway = NULL;
    D3D11_BUFFER_DESC desc;

    // D3D11_BIND_CONSTANT_BUFFER size must be 16-byte aligned.
    desc.ByteWidth = AlignUp16((UINT)templateBuf.length);
    desc.CPUAccessFlags = 0;
    desc.MiscFlags = 0;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_VERTEX_BUFFER | D3D11_BIND_INDEX_BUFFER;
    desc.StructureByteStride = 0;

    HRESULT hr = m_pDevice->CreateBuffer(&desc, NULL, &throwaway);
    if(FAILED(hr))
    {
      RDCERR("Failed to create proxy buffer");
      return ResourceId();
    }

    resource = throwaway;

    ret = ((WrappedID3D11Buffer *)throwaway)->GetResourceID();
  }

  m_ProxyResources.push_back(resource);

  return ret;
}

void D3D11Replay::SetProxyBufferData(ResourceId bufid, byte *data, size_t dataSize)
{
  if(bufid == ResourceId())
    return;

  ID3D11DeviceContext *ctx = m_pDevice->GetImmediateContext()->GetReal();

  if(WrappedID3D11Buffer::m_BufferList.find(bufid) != WrappedID3D11Buffer::m_BufferList.end())
  {
    WrappedID3D11Buffer *buf =
        (WrappedID3D11Buffer *)WrappedID3D11Buffer::m_BufferList[bufid].m_Buffer;

    D3D11_BUFFER_DESC desc;
    buf->GetDesc(&desc);

    if(AlignUp16(dataSize) < desc.ByteWidth)
    {
      RDCERR("Insufficient data provided to SetProxyBufferData");
      return;
    }

    ctx->UpdateSubresource(buf->GetReal(), 0, NULL, data, (UINT)dataSize, (UINT)dataSize);
  }
  else
  {
    RDCERR("Invalid buffer id passed to SetProxyBufferData");
  }
}

ID3DDevice *GetD3D11DeviceIfAlloc(IUnknown *dev);

RDResult D3D11_CreateReplayDevice(RDCFile *rdc, const ReplayOptions &opts, IReplayDriver **driver)
{
  RDCDEBUG("Creating a D3D11 replay device");

  IntelCounters::Load();

  HMODULE lib = NULL;
  lib = LoadLibraryA("d3d11.dll");
  if(lib == NULL)
  {
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Failed to load d3d11.dll");
  }

  PFN_D3D11_CREATE_DEVICE_AND_SWAP_CHAIN CreateDeviceAndSwapChainPtr =
      (PFN_D3D11_CREATE_DEVICE_AND_SWAP_CHAIN)GetProcAddress(lib, "D3D11CreateDeviceAndSwapChain");

  RealD3D11CreateFunction CreateDeviceAndSwapChain = CreateDeviceAndSwapChainPtr;

  lib = LoadLibraryA("dxgi.dll");
  if(lib == NULL)
  {
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Failed to load dxgi.dll");
  }

  if(GetD3DCompiler() == NULL)
  {
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Failed to load d3dcompiler_??.dll");
  }

  D3D11InitParams initParams;

  uint64_t ver = D3D11InitParams::CurrentVersion;

  WrappedIDXGISwapChain4::RegisterD3DDeviceCallback(GetD3D11DeviceIfAlloc);

  // if we have an RDCFile, open the frame capture section and serialise the init params.
  // if not, we're creating a proxy-capable device so use default-initialised init params.
  if(rdc)
  {
    int sectionIdx = rdc->SectionIndex(SectionType::FrameCapture);

    if(sectionIdx < 0)
      RETURN_ERROR_RESULT(ResultCode::FileCorrupted, "File does not contain captured API data");

    ver = rdc->GetSectionProperties(sectionIdx).version;

    if(!D3D11InitParams::IsSupportedVersion(ver))
    {
      RETURN_ERROR_RESULT(ResultCode::APIIncompatibleVersion,
                          "D3D11 capture is incompatible version %llu, newest supported by this "
                          "build of RenderDoc is %llu",
                          ver, D3D11InitParams::CurrentVersion);
    }

    StreamReader *reader = rdc->ReadSection(sectionIdx);

    ReadSerialiser ser(reader, Ownership::Stream);

    ser.SetVersion(ver);

    SystemChunk chunk = ser.ReadChunk<SystemChunk>();

    if(chunk != SystemChunk::DriverInit)
    {
      RETURN_ERROR_RESULT(ResultCode::FileCorrupted,
                          "Expected to get a DriverInit chunk, instead got %u", chunk);
    }

    SERIALISE_ELEMENT(initParams);

    if(ser.IsErrored())
    {
      return ser.GetError();
    }

    if(initParams.AdapterDesc.Description[0])
      RDCLOG("Capture was created on %s / %ls",
             ToStr(GPUVendorFromPCIVendor(initParams.AdapterDesc.VendorId)).c_str(),
             initParams.AdapterDesc.Description);
  }

  IDXGIFactory *factory = NULL;

  // first try to use DXGI 1.1 as MSDN has vague warnings about trying to 'mix' DXGI 1.0 and 1.1
  {
    typedef HRESULT(WINAPI * PFN_CREATE_DXGI_FACTORY)(REFIID, void **);

    PFN_CREATE_DXGI_FACTORY createFunc =
        (PFN_CREATE_DXGI_FACTORY)GetProcAddress(GetModuleHandleA("dxgi.dll"), "CreateDXGIFactory1");

    if(createFunc)
    {
      IDXGIFactory1 *tmpFactory = NULL;
      HRESULT hr = createFunc(__uuidof(IDXGIFactory1), (void **)&tmpFactory);

      if(SUCCEEDED(hr) && tmpFactory)
      {
        factory = tmpFactory;
      }
      else
      {
        RDCERR("Error creating IDXGIFactory1: %s", ToStr(hr).c_str());
      }
    }
    else
    {
      RDCWARN("Couldn't get CreateDXGIFactory1");
    }
  }

  if(!factory)
  {
    RDCWARN("Couldn't create IDXGIFactory1, falling back to CreateDXGIFactory");

    typedef HRESULT(WINAPI * PFN_CREATE_DXGI_FACTORY)(REFIID, void **);

    PFN_CREATE_DXGI_FACTORY createFunc =
        (PFN_CREATE_DXGI_FACTORY)GetProcAddress(GetModuleHandleA("dxgi.dll"), "CreateDXGIFactory");

    if(createFunc)
    {
      HRESULT hr = createFunc(__uuidof(IDXGIFactory), (void **)&factory);

      if(FAILED(hr) || !factory)
      {
        SAFE_RELEASE(factory);
        RETURN_ERROR_RESULT(ResultCode::APIInitFailed,
                            "Couldn't create DXGI factory from CreateDXGIFactory: %s",
                            ToStr(hr).c_str());
      }
    }
    else
    {
      RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Couldn't find CreateDXGIFactory in dxgi.dll");
    }
  }

  // we won't use the SDKVersion from the init params, so warn if it's different.
  if(initParams.SDKVersion != D3D11_SDK_VERSION)
  {
    RDCWARN(
        "Capture file used a different SDK version %lu from replay app %lu. Results may be "
        "undefined",
        initParams.SDKVersion, D3D11_SDK_VERSION);
  }

  INVAPID3DDevice *nvapiDev = NULL;
  IAGSD3DDevice *agsDev = NULL;

  if(initParams.VendorExtensions == GPUVendor::nVidia)
  {
    nvapiDev = InitialiseNVAPIReplay();

    if(!nvapiDev)
    {
      RETURN_ERROR_RESULT(
          ResultCode::APIHardwareUnsupported,
          "Capture requires nvapi to replay, but it's not available or can't be initialised");
    }
  }
  else if(initParams.VendorExtensions == GPUVendor::AMD)
  {
    agsDev = InitialiseAGSReplay(~0U, initParams.VendorUAV);

    if(!agsDev)
    {
      RETURN_ERROR_RESULT(
          ResultCode::APIHardwareUnsupported,
          "Capture requires ags to replay, but it's not available or can't be initialised");
    }

    CreateDeviceAndSwapChain =
        [agsDev](IDXGIAdapter *pAdapter, D3D_DRIVER_TYPE DriverType, HMODULE Software, UINT Flags,
                 CONST D3D_FEATURE_LEVEL *pFeatureLevels, UINT FeatureLevels, UINT SDKVersion,
                 CONST DXGI_SWAP_CHAIN_DESC *pSwapChainDesc, IDXGISwapChain **ppSwapChain,
                 ID3D11Device **ppDevice, D3D_FEATURE_LEVEL *pFeatureLevel,
                 ID3D11DeviceContext **ppImmediateContext) {
          return agsDev->CreateD3D11(pAdapter, DriverType, Software, Flags, pFeatureLevels,
                                     FeatureLevels, SDKVersion, pSwapChainDesc, ppSwapChain,
                                     ppDevice, pFeatureLevel, ppImmediateContext);
        };
  }
  else if(initParams.VendorExtensions != GPUVendor::Unknown)
  {
    RETURN_ERROR_RESULT(
        ResultCode::APIInitFailed,
        "Capture requires vendor extensions by %s to replay, but no support for that is "
        "available.",
        ToStr(initParams.VendorExtensions).c_str());
  }

  IDXGIAdapter *adapter = NULL;
  ID3D11Device *device = NULL;
  HRESULT hr = E_FAIL;

  const bool isProxy = (rdc == NULL);

  // This says whether we're using warp at all
  bool useWarp = false;
  // This says if we've fallen back to warp after failing to find anything better (hence degraded
  // support because using warp was not deliberate)
  bool warpFallback = false;

  if(!isProxy)
    ChooseBestMatchingAdapter(GraphicsAPI::D3D11, factory, initParams.AdapterDesc, opts, &useWarp,
                              &adapter);

  if(useWarp)
    SAFE_RELEASE(adapter);

  DXGI_ADAPTER_DESC chosenAdapter = {};
  if(adapter)
    adapter->GetDesc(&chosenAdapter);

  // check that the adapter supports at least feature level 11_0
  D3D_FEATURE_LEVEL maxFeatureLevel = D3D_FEATURE_LEVEL_9_1;

  // check for feature level 11 support - passing NULL feature level array implicitly checks for
  // 11_0 before others
  {
    D3D_DRIVER_TYPE type = adapter ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE;

    if(useWarp)
      type = D3D_DRIVER_TYPE_WARP;

    ID3D11Device *dev = NULL;
    hr = CreateDeviceAndSwapChain(adapter, type, NULL, 0, NULL, 0, D3D11_SDK_VERSION, NULL, NULL,
                                  &dev, &maxFeatureLevel, NULL);
    SAFE_RELEASE(dev);

    // if the device doesn't support 11_0, we can't use it.
    if(SUCCEEDED(hr) && maxFeatureLevel < D3D_FEATURE_LEVEL_11_0)
    {
      // If we were using a specific adapter try falling back to default selection in case the
      // adapter chosen isn't the default one in the system
      if(adapter)
      {
        RDCWARN(
            "Selected %ls for replay, but it does not support D3D_FEATURE_LEVEL_11_0. "
            "Falling back to NULL adapter",
            chosenAdapter.Description);

        SAFE_RELEASE(adapter);

        hr = CreateDeviceAndSwapChain(adapter, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, NULL, 0,
                                      D3D11_SDK_VERSION, NULL, NULL, &dev, &maxFeatureLevel, NULL);
        SAFE_RELEASE(dev);
      }

      // if it's still not 11_0, assume there is no 11_0 GPU and fall back to WARP.
      if(SUCCEEDED(hr) && maxFeatureLevel < D3D_FEATURE_LEVEL_11_0)
      {
        RDCWARN(
            "Couldn't create FEATURE_LEVEL_11_0 device - RenderDoc requires FEATURE_LEVEL_11_0 "
            "availability - falling back to WARP rasterizer");
        useWarp = warpFallback = true;
      }
    }
  }

  UINT flags = initParams.Flags;

  // we control the debug flag ourselves
  flags &= ~D3D11_CREATE_DEVICE_DEBUG;

  if(opts.apiValidation)
    flags |= D3D11_CREATE_DEVICE_DEBUG;
  else
    flags &= ~D3D11_CREATE_DEVICE_DEBUG;

  // we should now be set up to try creating feature level 11 devices either with a selected
  // adapter, a NULL (any) adapter, or WARP.

  // The retry paths are like this:
  //
  // try first with the adapter, then if we were using a specific adapter try without, then last try
  // with WARP
  //      within that, try to create a device at descending feature levels
  //           for each attempt, if the debug layer is enabled try without it

  for(int adapterPass = 0; adapterPass < 3; adapterPass++)
  {
    // if we don't have an adapter we're trying, just skip straight to pass 1
    if(adapterPass == 0 && adapter == NULL)
      continue;

    // if we're already falling back (or deliberately using) WARP, skip to pass 2
    if(adapterPass == 1 && useWarp)
      continue;

    // don't use the adapter except in the first pass
    if(adapterPass > 0)
      SAFE_RELEASE(adapter);

    D3D_DRIVER_TYPE driverType = D3D_DRIVER_TYPE_UNKNOWN;

    // if we're on the WARP pass, use the WARP driver type, otherwise use the driver type implied by
    // whether or not we have an adapter specified
    if(adapterPass == 2)
      driverType = D3D_DRIVER_TYPE_WARP;
    else
      driverType = adapter ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE;

    D3D_FEATURE_LEVEL featureLevels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};

    // try first to create with the whole array, then with all but the top, then all but the next
    // top, etc
    for(int featureLevelPass = 0; featureLevelPass < ARRAY_COUNT(featureLevels); featureLevelPass++)
    {
      D3D_FEATURE_LEVEL *featureLevelSubset = featureLevels + featureLevelPass;
      UINT numFeatureLevels = ARRAY_COUNT(featureLevels) - featureLevelPass;

      RDCDEBUG(
          "Creating D3D11 replay device, %ls adapter, driver type %s, flags %x, %d feature levels "
          "(first %s)",
          adapter ? chosenAdapter.Description : L"NULL", ToStr(driverType).c_str(), flags,
          numFeatureLevels, ToStr(featureLevelSubset[0]).c_str());

      hr = CreateDeviceAndSwapChain(adapter, driverType, NULL, flags, featureLevelSubset,
                                    numFeatureLevels, D3D11_SDK_VERSION, NULL, NULL, &device, NULL,
                                    NULL);

      if(SUCCEEDED(hr) && device)
        break;

      SAFE_RELEASE(device);

// in release try to fall back to a non-debug device
#if ENABLED(RDOC_RELEASE)
      if(flags & D3D11_CREATE_DEVICE_DEBUG)
      {
        UINT noDebugFlags = flags & ~D3D11_CREATE_DEVICE_DEBUG;

        HRESULT hr2 = CreateDeviceAndSwapChain(adapter, driverType, NULL, noDebugFlags,
                                               featureLevelSubset, numFeatureLevels,
                                               D3D11_SDK_VERSION, NULL, NULL, &device, NULL, NULL);

        // if we can manage to create it without debug active, do that since it's extremely unlikely
        // that any other configuration will have better luck with debug active.
        if(SUCCEEDED(hr2) && device)
        {
          RDCLOG(
              "Device creation failed with validation active - check that you have the "
              "SDK installed or Windows feature enabled to get the D3D debug layers.");

          hr = hr2;

          break;
        }
      }
#endif

      RDCLOG("Device creation failed, %s", ToStr(hr).c_str());
    }

    if(SUCCEEDED(hr) && device)
      break;
  }

  SAFE_RELEASE(adapter);

  if(SUCCEEDED(hr) && device)
  {
    if(nvapiDev)
    {
      BOOL ok = nvapiDev->SetReal(device);
      if(!ok)
      {
        SAFE_RELEASE(device);
        SAFE_RELEASE(nvapiDev);
        SAFE_RELEASE(factory);
        RETURN_ERROR_RESULT(
            ResultCode::APIHardwareUnsupported,
            "This capture needs nvapi extensions to replay, but device selected for replay can't "
            "support nvapi extensions");
      }
    }

    if(agsDev)
    {
      if(!agsDev->ExtensionsSupported())
      {
        SAFE_RELEASE(device);
        SAFE_RELEASE(nvapiDev);
        SAFE_RELEASE(factory);
        RETURN_ERROR_RESULT(
            ResultCode::APIHardwareUnsupported,
            "This capture needs AGS extensions to replay, but device selected for replay can't "
            "support nvapi extensions");
      }
    }

    WrappedID3D11Device *wrappedDev = new WrappedID3D11Device(device, initParams);
    wrappedDev->SetInitParams(initParams, ver, opts, nvapiDev, agsDev);

    if(!isProxy)
      RDCLOG("Created device.");
    D3D11Replay *replay = wrappedDev->GetReplay();

    replay->SetProxy(isProxy, warpFallback);
    replay->InitReplayOnDevice(factory);
    if(isProxy)
      replay->CreateResources();
    if(warpFallback)
    {
      wrappedDev->AddDebugMessage(
          MessageCategory::Initialization, MessageSeverity::High, MessageSource::RuntimeWarning,
          "Couldn't create FEATURE_LEVEL_11_0 device - RenderDoc requires FEATURE_LEVEL_11_0 "
          "availability - falling back to WARP rasterizer.\n"
          "Performance and usability will be significantly degraded.");
    }

    *driver = (IReplayDriver *)replay;
    return ResultCode::Succeeded;
  }

  SAFE_RELEASE(factory);

  rdcstr error = "Couldn't create any compatible d3d11 device.";

  if(flags & D3D11_CREATE_DEVICE_DEBUG)
    error +=
        "\n\nDevelopment RenderDoc builds require D3D debug layers available, "
        "ensure you have the windows SDK or windows feature needed.";

  RETURN_ERROR_RESULT(ResultCode::APIHardwareUnsupported, "%s", error.c_str());
}

static DriverRegistration D3D11DriverRegistration(RDCDriver::D3D11, &D3D11_CreateReplayDevice);

RDResult D3D11_ProcessStructured(RDCFile *rdc, SDFile &output)
{
  WrappedID3D11Device device(NULL, D3D11InitParams());

  int sectionIdx = rdc->SectionIndex(SectionType::FrameCapture);

  if(sectionIdx < 0)
    RETURN_ERROR_RESULT(ResultCode::FileCorrupted, "File does not contain captured API data");

  device.SetStructuredExport(rdc->GetSectionProperties(sectionIdx).version);
  RDResult result = device.ReadLogInitialisation(rdc, true);

  if(result == ResultCode::Succeeded)
    device.GetStructuredFile()->Swap(output);

  return result;
}

static StructuredProcessRegistration D3D11ProcessRegistration(RDCDriver::D3D11,
                                                              &D3D11_ProcessStructured);

#if ENABLED(ENABLE_UNIT_TESTS)
#include "catch/catch.hpp"

TEST_CASE("D3D11 live forward validates the exact immediate-context interval",
          "[d3d11][replay][live-forward]")
{
  SDFile file;
  rdcarray<APIEvent> events;
  events.resize(6);
  const D3D11Chunk types[] = {D3D11Chunk::Draw, D3D11Chunk::OMSetRenderTargets,
                              D3D11Chunk::DrawIndexed, D3D11Chunk::CopyResource,
                              D3D11Chunk::Dispatch};
  for(uint32_t i = 0; i < 5; ++i)
  {
    SDChunk *chunk = new SDChunk("forward-interval"_lit);
    chunk->metadata.chunkID = uint32_t(types[i]);
    file.chunks.push_back(chunk);
    events[i + 1].eventId = i + 1;
    events[i + 1].chunkIndex = i;
    events[i + 1].fileOffset = 100 + i * 32;
  }
  rdcstr reason;
  CHECK(D3D11LiveForwardInterval(events, file, 1, 3, reason));
  CHECK(D3D11LiveForwardInterval(events, file, 1, 5, reason));
  CHECK(D3D11LiveForwardInterval(events, file, 3, 5, reason));
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 5, 3, reason));
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 3, 3, reason));
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 0, 3, reason));
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 1, 6, reason));
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 1, 2, reason));
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 2, 5, reason));

  for(D3D11Chunk type : {D3D11Chunk::Map, D3D11Chunk::Unmap, D3D11Chunk::Begin,
                         D3D11Chunk::End, D3D11Chunk::ExecuteCommandList,
                         D3D11Chunk::DrawInstancedIndirect, D3D11Chunk::DispatchIndirect,
                         D3D11Chunk::SetPredication, D3D11Chunk::CreateBuffer,
                         D3D11Chunk(0xffffffffU)})
  {
    file.chunks[3]->metadata.chunkID = uint32_t(type);
    CHECK_FALSE(D3D11LiveForwardInterval(events, file, 1, 5, reason));
  }
  file.chunks[3]->metadata.chunkID = uint32_t(D3D11Chunk::CopyResource);
  events[4].eventId = 0;
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 1, 5, reason));
  events[4].eventId = 4;
  events[4].fileOffset = events[3].fileOffset;
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 1, 5, reason));
  events[4].fileOffset = 196;
  events[4].chunkIndex = 2;
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 1, 5, reason));
  events[4].chunkIndex = 3;

  // Annotations are physical chunks but have no event ID; every executable chunk still maps
  // exactly once. Omitting an executable chunk from the event table must fail closed.
  SDChunk *annotation = new SDChunk("annotation"_lit);
  annotation->metadata.chunkID = uint32_t(D3D11Chunk::SetCommandAnnotation);
  file.chunks.insert(1, annotation);
  for(uint32_t eid = 2; eid <= 5; ++eid)
    ++events[eid].chunkIndex;
  CHECK(D3D11LiveForwardInterval(events, file, 1, 5, reason));
  annotation->metadata.chunkID = uint32_t(D3D11Chunk::CopyResource);
  CHECK_FALSE(D3D11LiveForwardInterval(events, file, 1, 5, reason));
}

TEST_CASE("D3D11 live forward publishes only the matching successful ordinary target",
          "[d3d11][replay][live-forward]")
{
  CHECK(D3D11LiveForwardCanPublish(75, 75, 34, 34, true, true));
  CHECK_FALSE(D3D11LiveForwardCanPublish(75, 75, 35, 34, true, true));
  CHECK_FALSE(D3D11LiveForwardCanPublish(76, 75, 34, 34, true, true));
  CHECK_FALSE(D3D11LiveForwardCanPublish(75, 0, 34, 34, true, true));
  CHECK_FALSE(D3D11LiveForwardCanPublish(0, 0, 34, 34, true, true));
  CHECK_FALSE(D3D11LiveForwardCanPublish(75, 75, 34, 34, false, true));
  CHECK_FALSE(D3D11LiveForwardCanPublish(75, 75, 34, 34, true, false));
  CHECK(D3D11LiveForwardCaptureChunkSafe(D3D11Chunk::Draw));
  CHECK(D3D11LiveForwardCaptureChunkSafe(D3D11Chunk::UpdateSubresource));
  for(D3D11Chunk chunk : {D3D11Chunk::Begin, D3D11Chunk::End,
                          D3D11Chunk::CreateQuery, D3D11Chunk::CreatePredicate,
                          D3D11Chunk::SOSetTargets, D3D11Chunk::DrawAuto,
                          D3D11Chunk::SetShaderExtUAV, D3D11Chunk::SwapDeviceContextState,
                          D3D11Chunk::ExecuteCommandList})
    CHECK_FALSE(D3D11LiveForwardCaptureChunkSafe(chunk));
}

TEST_CASE("D3D11 checkpoint versions include CPU writes and use AFTER-event boundaries",
          "[d3d11][checkpoint]")
{
  CHECK(CheckpointWritesResource(ResourceUsage::CPUWrite));
  CHECK(CheckpointWritesResource(ResourceUsage::CopyDst));
  CHECK(CheckpointWritesResource(ResourceUsage::ResolveDst));
  CHECK(CheckpointWritesResource(ResourceUsage::CS_RWResource));
  CHECK(CheckpointWritesResource(ResourceUsage::ColorTarget));
  CHECK(CheckpointWritesResource(ResourceUsage::Discard));
  CHECK_FALSE(CheckpointWritesResource(ResourceUsage::CopySrc));
  CHECK_FALSE(CheckpointWritesResource(ResourceUsage::VS_Constants));
  CHECK_FALSE(CheckpointWritesResource(ResourceUsage::PS_Resource));
  const rdcarray<uint32_t> writes = {9, 3, 17};
  CHECK(CheckpointLastWrite(writes, 2) == 0);
  CHECK(CheckpointLastWrite(writes, 3) == 3);
  CHECK(CheckpointLastWrite(writes, 8) == 3);
  CHECK(CheckpointLastWrite(writes, 9) == 9);
  CHECK(CheckpointLastWrite(writes, 18) == 17);
}
TEST_CASE("D3D11 checked shader replacement requires exact ordinary creation evidence",
          "[d3d11][shader][checked-replacement]")
{
  const ResourceId shader = ResourceIDGen::GetNewUniqueID();
  const ResourceId other = ResourceIDGen::GetNewUniqueID();
  const D3D11Chunk creations[] = {D3D11Chunk::CreateVertexShader, D3D11Chunk::CreateHullShader,
                                  D3D11Chunk::CreateDomainShader, D3D11Chunk::CreateGeometryShader,
                                  D3D11Chunk::CreatePixelShader,  D3D11Chunk::CreateComputeShader};
  for(size_t i = 0; i < ARRAY_COUNT(creations); i++)
  {
    SDChunk chunk("shader creation"_lit);
    chunk.metadata.chunkID = uint32_t(creations[i]);
    chunk.AddAndOwnChild(makeSDResourceId("pShader"_lit, shader));
    SDObject *linkage = chunk.AddAndOwnChild(makeSDResourceId("pClassLinkage"_lit, ResourceId()));
    CHECK(D3D11CheckedShaderCreationMatches(chunk, shader, ShaderStage(i)));
    CHECK_FALSE(D3D11CheckedShaderCreationMatches(chunk, other, ShaderStage(i)));
    CHECK_FALSE(D3D11CheckedShaderCreationMatches(chunk, shader, ShaderStage((i + 1) % 6)));
    CHECK_FALSE(D3D11CheckedShaderCreationMatches(chunk, shader, ShaderStage::Mesh));
    linkage->data.basic.id = other;
    CHECK_FALSE(D3D11CheckedShaderCreationMatches(chunk, shader, ShaderStage(i)));
    linkage->data.basic.id = ResourceId();
    linkage->type.basetype = SDBasic::UnsignedInteger;
    CHECK_FALSE(D3D11CheckedShaderCreationMatches(chunk, shader, ShaderStage(i)));
  }
  SDChunk streamOutput("stream output shader"_lit);
  streamOutput.metadata.chunkID = uint32_t(D3D11Chunk::CreateGeometryShaderWithStreamOutput);
  streamOutput.AddAndOwnChild(makeSDResourceId("pShader"_lit, shader));
  streamOutput.AddAndOwnChild(makeSDResourceId("pClassLinkage"_lit, ResourceId()));
  CHECK_FALSE(D3D11CheckedShaderCreationMatches(streamOutput, shader, ShaderStage::Geometry));
  SDChunk missing("incomplete shader creation"_lit);
  missing.metadata.chunkID = uint32_t(D3D11Chunk::CreatePixelShader);
  missing.AddAndOwnChild(makeSDResourceId("pShader"_lit, shader));
  CHECK_FALSE(D3D11CheckedShaderCreationMatches(missing, shader, ShaderStage::Pixel));
  CHECK(D3D11NativeShaderStage(NULL) == ShaderStage::Count);
}

TEST_CASE("D3D11 checkpoint metadata rejects before overflowing or exceeding its cap",
          "[d3d11][replay][checkpoint]")
{
  D3D11CheckpointMetadataBudget tiny(64);
  CHECK(tiny.Add(2, 16));
  CHECK(tiny.Add(1, 32));
  CHECK_FALSE(tiny.Add(1, 1));
  CHECK_FALSE(tiny.Add(0, 1));
  D3D11CheckpointMetadataBudget capped(~0ULL);
  CHECK(capped.limit == 64ULL * 1024 * 1024);
  CHECK_FALSE(capped.Add(~0ULL, ~0ULL));
  uint64_t bytes = ~0ULL - 4;
  CHECK_FALSE(D3D11CheckpointAddBytes(bytes, 8, ~0ULL));
  CHECK(bytes == ~0ULL - 4);
  bytes = 32;
  CHECK(D3D11CheckpointAddBytes(bytes, 32, 64));
  CHECK_FALSE(D3D11CheckpointAddBytes(bytes, 1, 64));
}

TEST_CASE("D3D11 event CBuffer shadows bind legal 256-byte ranges", "[d3d11][cbuffer]")
{
  CHECK(EventCBufferShadowConstants(16) == 16);
  CHECK(EventCBufferShadowConstants(256) == 16);
  CHECK(EventCBufferShadowConstants(272) == 32);
  CHECK(EventCBufferShadowConstants(64 * 1024) == 4096);
  for(uint64_t bytes : {16ULL, 256ULL, 272ULL, 64ULL * 1024ULL})
  {
    const uint64_t constants = EventCBufferShadowConstants(bytes);
    CHECK(constants % 16 == 0);
    CHECK(constants * 16 >= bytes);
    CHECK(constants * 16 - bytes < 256);
  }
}

#endif
