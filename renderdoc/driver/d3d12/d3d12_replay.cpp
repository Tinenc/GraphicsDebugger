/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2016-2026 Baldur Karlsson
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

#include "d3d12_replay.h"
#include <algorithm>
#include "core/plugins.h"
#include "core/settings.h"
#include "driver/dx/official/d3dcompiler.h"
#include "driver/dxgi/dxgi_common.h"
#include "driver/ihv/amd/amd_counters.h"
#include "driver/ihv/amd/amd_rgp.h"
#include "driver/ihv/nv/nv_aftermath.h"
#include "driver/ihv/nv/nv_d3d12_counters.h"
#include "driver/shaders/dxbc/dxbc_common.h"
#include "maths/camera.h"
#include "maths/formatpacking.h"
#include "maths/matrix.h"
#include "replay/dummy_driver.h"
#include "replay/replay_timing.h"
#include "serialise/rdcfile.h"
#include "strings/string_utils.h"
#include "d3d12_command_list.h"
#include "d3d12_command_queue.h"
#include "d3d12_commit_audit.inl"
#include "d3d12_debug.h"
#include "d3d12_device.h"
#include "d3d12_hooks.h"
#include "d3d12_resources.h"
#include "d3d12_rootsig.h"
#include "d3d12_shader_cache.h"

#include "data/hlsl/hlsl_cbuffers.h"

RDOC_CONFIG(bool, D3D12_HardwareCounters, true,
            "Enable support for IHV-specific hardware counters on D3D12.");

RDOC_CONFIG(bool, D3D12_AttemptExperimentalGPUUploadEnable, true,
            "Try to enable 'experimental' support for GPU upload heaps on D3D12.");

// this is global so we can free it even after D3D12Replay is destroyed
static HMODULE D3D12Lib = NULL;

static const char *LiveDriverDisassemblyTarget = "Live driver disassembly";

ID3DDevice *GetD3D12DeviceIfAlloc(IUnknown *dev);

static const char *DXBCDXILDisassemblyTarget = "DXBC/DXIL";
static const char *DXCDXILDisassemblyTarget = "DXC DXIL";

D3D12Replay::D3D12Replay(WrappedID3D12Device *d)
{
  m_pDevice = d;
  m_HighlightCache.driver = this;
}

void D3D12Replay::Shutdown()
{
  bool apiValidation = m_pDevice->GetReplayOptions().apiValidation;

  DrainTextureDisplaySubmit();
  if(!m_GPUPhaseAudits.empty())
  {
    if(m_pDevice->DeviceWaitForIdle())
      FinishGPUPhaseAudit(true);
  }

  // ReplayLab event CBuffer resources are replay-owned and may still be referenced by the cached
  // CPU render state. Restore that state and release every shadow before destroying the device.
  ClearEventShaderOverrides();
  ReleaseEventShaderPipelines();
  ReleaseEventShaderScratch();
  ClearEventCBufferOverrides();

  if(!m_TextureSnapshots.empty() || !m_InitialStateSourceCache.empty() ||
     !m_ShaderPairUnconfirmed.empty())
  {
    // Snapshot creation is intentionally asynchronous during use. Teardown must drain that work
    // before dropping the application's explicit D3D12 resource references.
    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists(true);
  }

  for(size_t i = 0; i < m_ProxyResources.size(); i++)
    m_ProxyResources[i]->Release();
  m_ProxyResources.clear();
  m_TextureSnapshots.clear();
  for(ID3D12DeviceChild *resource : m_ShaderPairUnconfirmed)
    resource->Release();
  m_ShaderPairUnconfirmed.clear();

  for(auto &entry : m_InitialStateSourceCache)
    SAFE_RELEASE(entry.second);
  m_InitialStateSourceCache.clear();
  SAFE_RELEASE(m_InitialStateCacheAdapter);

  DXBC::ResetSearchDirsCache();

  SAFE_DELETE(m_RGP);

  if(m_DevConfig)
  {
    SAFE_RELEASE(m_DevConfig->debug);
    SAFE_RELEASE(m_DevConfig->devconfig);
    SAFE_RELEASE(m_DevConfig->devfactory);
    SAFE_RELEASE(m_DevConfig->dred);

    m_DevConfig->sdkconfig->FreeUnusedSDKs();
    SAFE_RELEASE(m_DevConfig->sdkconfig);
    SAFE_DELETE(m_DevConfig);
  }

  // this destroys the replay object
  m_pDevice->Release();

  // the this pointer is free'd after this point

  FreeLibrary(D3D12Lib);
  D3D12_CleanupReplaySDK();

  // we should have unloaded both modules here by now. If we haven't - we probably leaked some D3D12
  // objects. This can cause subsequent captures to fail to open.
  // Unless API validation is enabled, as the validation layers don't refcount the DLL properly
  if(!apiValidation)
  {
    RDCASSERT(GetModuleHandleA("d3d12.dll") == NULL);
    RDCASSERT(GetModuleHandleA("d3d12core.dll") == NULL);
  }

  Threading::JobSystem::Shutdown();
}

ID3D12Resource *D3D12Replay::ResolveInitialStateCopySource(
    ID3D12Resource *wrappedSource, ID3D12GraphicsCommandListX *wrappedList, bool allowLoading)
{
  ID3D12Resource *source = Unwrap(wrappedSource);
  const D3D12_RESOURCE_DESC desc = source->GetDesc();

  // Loading uses the original upload source, so all capture allocations are known before we
  // reserve extra VRAM. Validation/conservative replay also retains the original path for A/B QA.
  if(Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_INITIAL_SOURCE_CACHE") == "1" ||
     m_pDevice->ShouldUseConservativeReplaySync(allowLoading) ||
     desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
    return source;

  const ResourceId id = GetResID(wrappedSource);
  auto found = m_InitialStateSourceCache.find(id);
  if(found != m_InitialStateSourceCache.end())
  {
    if(found->second)
    {
      AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheHits);
      AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheBytes, desc.Width);
      return found->second;
    }
    AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheFallbackBytes, desc.Width);
    return source;
  }

  D3D12_HEAP_PROPERTIES sourceHeap = {};
  if(FAILED(source->GetHeapProperties(&sourceHeap, NULL)) ||
     sourceHeap.Type != D3D12_HEAP_TYPE_UPLOAD)
    return source;

  // Cache a rejected source too: repeated interaction must not retry allocations or query DXGI
  // per resource when this capture has exhausted its spare budget. No eviction or GPU drain is
  // performed while dragging. A new capture gets a fresh budget and cache.
  m_InitialStateSourceCache[id] = NULL;
  const auto fallback = [&]() {
    AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheFallbackBytes, desc.Width);
    return source;
  };

  if(desc.Width < 64ULL * 1024)
    return fallback();

  const bool firstBudgetQuery = !m_InitialStateCacheAdapterChecked;
  if(!m_InitialStateCacheAdapterChecked)
  {
    m_InitialStateCacheAdapterChecked = true;
    IDXGIAdapter *adapter = NULL;
    if(m_pFactory && SUCCEEDED(EnumAdapterByLuid(m_pFactory, m_pDevice->GetAdapterLuid(), &adapter)))
    {
      adapter->QueryInterface(__uuidof(IDXGIAdapter3), (void **)&m_InitialStateCacheAdapter);
      adapter->Release();
    }
  }

  DXGI_QUERY_VIDEO_MEMORY_INFO memory = {};
  if(!m_InitialStateCacheAdapter ||
     FAILED(m_InitialStateCacheAdapter->QueryVideoMemoryInfo(
         0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &memory)))
    return fallback();

  const D3D12_RESOURCE_ALLOCATION_INFO allocation =
      m_pDevice->GetReal()->GetResourceAllocationInfo(0, 1, &desc);
  const uint64_t reserve = RDCMAX(512ULL * 1024 * 1024, memory.Budget / 8);
  if(firstBudgetQuery && (Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1" ||
                          Process::GetEnvVariable("RENDERDOC_D3D12_SOURCE_BUDGET_LOG") == "1"))
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=D3D12InitialSourceBudget localBudgetBytes=%llu currentUsageBytes=%llu "
        "reserveBytes=%llu cacheLimitBytes=%llu requestedBytes=%llu",
        (unsigned long long)memory.Budget, (unsigned long long)memory.CurrentUsage,
        (unsigned long long)reserve, (unsigned long long)(memory.Budget / 3),
        (unsigned long long)allocation.SizeInBytes));
  if(allocation.SizeInBytes == ~0ULL || memory.CurrentUsage >= memory.Budget ||
     memory.Budget - memory.CurrentUsage <= reserve ||
     allocation.SizeInBytes > memory.Budget - memory.CurrentUsage - reserve ||
     m_InitialStateSourceCacheBytes + allocation.SizeInBytes > memory.Budget / 3)
    return fallback();

  D3D12_HEAP_PROPERTIES heap = {};
  heap.Type = D3D12_HEAP_TYPE_DEFAULT;
  heap.CreationNodeMask = heap.VisibleNodeMask = 1;
  ID3D12Resource *resident = NULL;
  D3D12CommitAudit commitAudit("fastInitialSource", desc.Width, D3D12_HEAP_TYPE_DEFAULT,
                               desc.Dimension);
  HRESULT hr = m_pDevice->GetReal()->CreateCommittedResource(
      &heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
      __uuidof(ID3D12Resource), (void **)&resident);
  if(FAILED(hr))
    return fallback();

  resident->SetName(L"RenderDoc immutable initial-state source");
  ID3D12GraphicsCommandList *list = Unwrap(wrappedList);
  list->CopyBufferRegion(resident, 0, source, 0, desc.Width);
  D3D12_RESOURCE_BARRIER barrier = {};
  barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
  barrier.Transition.pResource = resident;
  barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
  barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
  barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
  list->ResourceBarrier(1, &barrier);

  // The copy and its first consumer share this list/queue. The existing initial-state dependencies
  // order later captured queues, and the next full replay drains prior work before reusing it.
  // These are capture initial bytes, not event outputs: live edits never make this source stale.
  m_InitialStateSourceCache[id] = resident;
  m_InitialStateSourceCacheBytes += allocation.SizeInBytes;
  AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCachePromotedBytes, desc.Width);
  return resident;
}

void D3D12Replay::Initialise(IDXGIFactory1 *factory, D3D12DevConfiguration *config)
{
  m_pFactory = factory;
  m_DevConfig = config;

  RDCEraseEl(m_DriverInfo);

  if(m_pFactory)
  {
    RefCountDXGIObject::HandleWrap("D3D12Replay", __uuidof(IDXGIFactory1), (void **)&m_pFactory);

    LUID luid = m_pDevice->GetAdapterLuid();

    IDXGIAdapter *pDXGIAdapter = NULL;
    HRESULT hr = EnumAdapterByLuid(m_pFactory, luid, &pDXGIAdapter);

    if(FAILED(hr))
    {
      RDCERR("Couldn't get DXGI adapter by LUID from D3D device");
    }
    else
    {
      DXGI_ADAPTER_DESC desc = {};
      pDXGIAdapter->GetDesc(&desc);
      LARGE_INTEGER version = {};
      pDXGIAdapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &version);

      m_DriverInfo.vendor = GPUVendorFromPCIVendor(desc.VendorId);

      rdcstr descString = GetDriverVersion(desc, version);
      descString.resize(RDCMIN(descString.size(), ARRAY_COUNT(m_DriverInfo.version) - 1));
      memcpy(m_DriverInfo.version, descString.c_str(), descString.size());

      RDCLOG("Running replay on %s / %s", ToStr(m_DriverInfo.vendor).c_str(), m_DriverInfo.version);

      SAFE_RELEASE(pDXGIAdapter);
    }
  }

  m_pDevice->SetDriverInfo(m_DriverInfo);

  if(!m_Proxy)
    Threading::JobSystem::Init();
}

RDResult D3D12Replay::FatalErrorCheck()
{
  return m_pDevice->FatalErrorCheck();
}

IReplayDriver *D3D12Replay::MakeDummyDriver()
{
  // gather up the shaders we've allocated to pass to the dummy driver
  rdcarray<const ShaderReflection *> shaders;
  WrappedID3D12Shader::GetReflections(shaders);

  IReplayDriver *dummy = new DummyDriver(this, shaders, m_pDevice->DetachStructuredFile(),
                                         m_pDevice->DetachAnnotations());

  return dummy;
}

void D3D12Replay::CreateResources()
{
  m_DebugManager = new D3D12DebugManager(m_pDevice);

  for(uint64_t i = FIRST_WIN_RTV; i <= LAST_WIN_RTV; i++)
    m_OutputWindowIDs.insert(0, i);
  for(uint64_t i = FIRST_WIN_DSV; i <= LAST_WIN_DSV; i++)
    m_DSVIDs.insert(0, i);

  if(RenderDoc::Inst().IsReplayApp())
  {
    CreateSOBuffers();

    if(m_pDevice->UsedDXIL())
      RDCLOG("Replaying with DXIL enabled");

    m_General.Init(m_pDevice, m_DebugManager);
    m_TexRender.Init(m_pDevice, m_DebugManager);
    m_Overlay.Init(m_pDevice, m_DebugManager);
    m_VertexPick.Init(m_pDevice, m_DebugManager);
    m_PixelPick.Init(m_pDevice, m_DebugManager);
    m_PixelHistory.Init(m_pDevice, m_DebugManager);
    m_Histogram.Init(m_pDevice, m_DebugManager);

    if(!m_Proxy && D3D12_HardwareCounters())
    {
      if(m_DriverInfo.vendor == GPUVendor::AMD || m_DriverInfo.vendor == GPUVendor::Samsung)
      {
        RDCLOG("AMD GPU detected - trying to initialise AMD counters");
        AMDCounters *countersAMD = new AMDCounters(m_pDevice->IsDebugLayerEnabled());

        ID3D12Device *d3dDevice = m_pDevice->GetReal();

        if(countersAMD && countersAMD->Init(AMDCounters::ApiType::Dx12, (void *)d3dDevice))
        {
          m_pAMDCounters = countersAMD;
        }
        else
        {
          delete countersAMD;
        }
      }

      if(m_DriverInfo.vendor == GPUVendor::nVidia)
      {
        RDCLOG("NVIDIA GPU detected - trying to initialise NVIDIA counters");

        NVD3D12Counters *countersNV = new NVD3D12Counters();

        bool initSuccess = false;
        if(countersNV && countersNV->Init(*m_pDevice))
        {
          m_pNVCounters = countersNV;
          initSuccess = true;
        }
        else
        {
          delete countersNV;
        }

        RDCLOG("NVIDIA D3D12 counter initialisation: %s", initSuccess ? "SUCCEEDED" : "FAILED");
      }
    }
  }
}

void D3D12Replay::DestroyResources()
{
  ClearPostVSCache();
  ClearFeedbackCache();

  m_General.Release();
  m_TexRender.Release();
  m_Overlay.Release();
  m_VertexPick.Release();
  m_PixelPick.Release();
  m_PixelHistory.Release();
  m_Histogram.Release();

  SAFE_RELEASE(m_BindlessFeedback.FeedbackBuffer);
  SAFE_RELEASE(m_BindlessFeedback.PipeStatsHeap);

  SAFE_RELEASE(m_SOBuffer);
  SAFE_RELEASE(m_SOStagingBuffer);
  SAFE_RELEASE(m_SOPatchedIndexBuffer);
  SAFE_RELEASE(m_SOQueryHeap);

  SAFE_RELEASE(m_pFactory);

  SAFE_RELEASE(m_CustomShaderTex);

  SAFE_DELETE(m_DebugManager);

  SAFE_DELETE(m_pAMDCounters);

  SAFE_DELETE(m_pNVCounters);
}

struct D3D12InitialSourceWarmupCandidate
{
  ResourceId source;
  ID3D12Resource *wrappedSource = NULL;
  uint64_t sourceBufferBytes = 0;
  uint32_t firstObservedWrite = ~0U;
};

static bool InitialSourceWarmupWriteUsage(ResourceUsage usage)
{
  return (usage >= ResourceUsage::VS_RWResource && usage <= ResourceUsage::All_RWResource) ||
         usage == ResourceUsage::CPUWrite || usage == ResourceUsage::CopyDst ||
         usage == ResourceUsage::Copy || usage == ResourceUsage::ResolveDst ||
         usage == ResourceUsage::Resolve || usage == ResourceUsage::Clear ||
         usage == ResourceUsage::ColorTarget || usage == ResourceUsage::DepthStencilTarget ||
         usage == ResourceUsage::StreamOut || usage == ResourceUsage::GenMips;
}

static void AddInitialSourceWarmupWrite(std::map<ResourceId, uint32_t> &writes, ResourceId id,
                                       uint32_t eventId)
{
  if(id == ResourceId() || eventId == ~0U)
    return;
  auto found = writes.insert({id, eventId});
  found.first->second = RDCMIN(found.first->second, eventId);
}

static rdcarray<D3D12InitialSourceWarmupCandidate> OrderInitialSourceWarmup(
    const rdcarray<D3D12InitialSourceWarmupCandidate> &candidates, bool firstWrite)
{
  // The legacy mode preserves even duplicate source visits and their exact original order.
  if(!firstWrite)
    return candidates;
  rdcarray<D3D12InitialSourceWarmupCandidate> result;
  std::map<ResourceId, size_t> sources;
  for(const D3D12InitialSourceWarmupCandidate &candidate : candidates)
  {
    auto source = sources.insert({candidate.source, result.size()});
    if(source.second)
      result.push_back(candidate);
    else
      result[source.first->second].firstObservedWrite =
          RDCMIN(result[source.first->second].firstObservedWrite, candidate.firstObservedWrite);
  }
  std::sort(result.begin(), result.end(), [](const D3D12InitialSourceWarmupCandidate &a,
                                           const D3D12InitialSourceWarmupCandidate &b) {
    if(a.firstObservedWrite != b.firstObservedWrite)
      return a.firstObservedWrite < b.firstObservedWrite;
    return a.source < b.source;
  });
  return result;
}

#include "3rdparty/zstd/xxhash.h"
#include "d3d12_initial_source_storage.inl"
#include "d3d12_memory_audit.inl"

RDResult D3D12Replay::ReadLogInitialisation(RDCFile *rdc, bool storeStructuredBuffers)
{
  RDResult result = m_pDevice->ReadLogInitialisation(rdc, storeStructuredBuffers);
  if(result != ResultCode::Succeeded || m_pDevice->ShouldUseConservativeReplaySync(true) ||
     Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_INITIAL_SOURCE_CACHE") == "1")
    return result;

  // All captured allocations and the writable-resource set are now known. Pay the one-time
  // upload cost while opening the capture, before the first user gesture, without replaying or
  // changing any captured resource contents. A separate timing API keeps this out of scrub stats.
  ScopedReplayTiming timing("D3D12InitialWarmup", 0, 0, eReplay_Full, true);
  const rdcstr sourceOrder = Process::GetEnvVariable("RENDERDOC_D3D12_INITIAL_SOURCE_CACHE_ORDER");
  // Prefer positive observed writes within the existing budget. This changes cache admission
  // order only: unknown sources remain candidates and every required restore still executes.
  // Explicit legacy/unknown controls retain the original order for independent comparisons.
  const bool firstWrite = sourceOrder.empty() || sourceOrder == "first-write";
  const bool logPlan = IsReplayTimingActive();
  D3D12ResourceManager *manager = m_pDevice->GetResourceManager();
  std::map<ResourceId, uint32_t> writes;
  if(firstWrite || logPlan)
  {
    // Positive, already-loaded write-use records are only a cache-priority heuristic. Missing
    // usages, bindless accesses, and future edited dependencies never justify skipping a restore.
    for(const auto &resource : m_pDevice->GetQueue()->GetCommandData()->m_ResourceUses)
      for(const EventUsage &usage : resource.second)
        if(InitialSourceWarmupWriteUsage(usage.usage))
          AddInitialSourceWarmupWrite(writes, resource.first, usage.eventId);
    // A placed resource obtains its immutable source from the physical heap. Aggregate evidence
    // to that source owner without relying on negative GetUsage(heap) results or changing aliases.
    for(const auto &resource : m_pDevice->GetResourceList())
    {
      auto observed = writes.find(resource.first);
      if(observed != writes.end() && resource.second->GetHeap())
        AddInitialSourceWarmupWrite(writes, GetResID(resource.second->GetHeap()), observed->second);
    }
  }
  rdcarray<D3D12InitialSourceWarmupCandidate> candidates;
  for(ResourceId id : manager->GetInitialContentResourceIDs())
  {
    ID3D12DeviceChild *live = manager->GetResource(id);
    if(!live || m_pDevice->IsReadOnlyResource(GetResID(live)))
      continue;

    D3D12InitialContents data = manager->GetInitialContents(id);
    if((data.resourceType != Resource_Resource && data.resourceType != Resource_Heap) ||
       (data.tag != D3D12InitialContents::Copy && data.tag != D3D12InitialContents::ForceCopy) ||
       !data.resource)
      continue;

    D3D12InitialSourceWarmupCandidate candidate;
    candidate.wrappedSource = (ID3D12Resource *)data.resource;
    candidate.source = GetResID(candidate.wrappedSource);
    if(firstWrite || logPlan)
    {
      const D3D12_RESOURCE_DESC desc = Unwrap(candidate.wrappedSource)->GetDesc();
      if(desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER)
        candidate.sourceBufferBytes = desc.Width;
      auto observed = writes.find(GetResID(live));
      if(observed != writes.end())
        candidate.firstObservedWrite = observed->second;
    }
    candidates.push_back(candidate);
  }
  const rdcarray<D3D12InitialSourceWarmupCandidate> ordered =
      OrderInitialSourceWarmup(candidates, firstWrite);
  const uint64_t residentBefore = m_InitialStateSourceCacheBytes;
  ID3D12GraphicsCommandListX *list = NULL;
  for(const D3D12InitialSourceWarmupCandidate &candidate : ordered)
  {
    if(!list)
    {
      list = m_pDevice->GetNewList();
      if(!list)
        return m_pDevice->FatalErrorCheck();
    }
    ResolveInitialStateCopySource(candidate.wrappedSource, list, true);
  }

  if(list)
  {
    HRESULT hr = list->Close();
    CHECK_HR(m_pDevice, hr);
    if(FAILED(hr))
      return m_pDevice->FatalErrorCheck();
    // Keep the conservative default: there may also be loading-time debug lists pending.
    m_pDevice->ExecuteLists(NULL, true);
    const bool drained = m_pDevice->FlushLists(true);
    if(drained && !m_pDevice->HasFatalError() && !storeStructuredBuffers)
    {
      RetireD3D12InitialUploadDuplicates(m_pDevice, m_InitialStateSourceCache);
      DeduplicateD3D12InitialUploadSources(m_pDevice);
    }
  }
  if(logPlan)
  {
    // Unique source counts are comparable even though the legacy loop can visit one source twice.
    const auto unique = OrderInitialSourceWarmup(candidates, true);
    uint64_t bytes = 0, observedBytes = 0;
    size_t observed = 0;
    for(const D3D12InitialSourceWarmupCandidate &candidate : unique)
    {
      bytes += candidate.sourceBufferBytes;
      if(candidate.firstObservedWrite != ~0U)
      {
        observed++;
        observedBytes += candidate.sourceBufferBytes;
      }
    }
    rdcstr firstSources;
    for(size_t i = 0; i < RDCMIN(size_t(8), ordered.size()); i++)
      firstSources += StringFormat::Fmt("%s%s:%u:%llu", i ? "," : "",
                                         ToStr(ordered[i].source).c_str(),
                                         ordered[i].firstObservedWrite,
                                         (unsigned long long)ordered[i].sourceBufferBytes);
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12InitialSourceWarmup order=%s candidates=%zu observedWriteCandidates=%zu "
        "candidateSourceBufferBytes=%llu observedWriteSourceBufferBytes=%llu visits=%zu "
        "residentAllocationBytesBefore=%llu residentAllocationBytesAfter=%llu "
        "firstSources=%s fatal=%u", firstWrite ? "first-write" : "legacy", unique.size(), observed,
        (unsigned long long)bytes, (unsigned long long)observedBytes, ordered.size(),
        (unsigned long long)residentBefore, (unsigned long long)m_InitialStateSourceCacheBytes,
        firstSources.empty() ? "none" : firstSources.c_str(), m_pDevice->HasFatalError() ? 1U : 0U);
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
  WarmD3D12ParameterReadIndex(m_pDevice);
  AuditD3D12Memory(m_pDevice);
  return m_pDevice->FatalErrorCheck();
}

rdcarray<GPUDevice> D3D12Replay::GetAvailableGPUs()
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
      dev.apis = {GraphicsAPI::D3D12};

      // don't add duplicate devices even if they get enumerated. Don't add WARP, we'll do that
      // manually since it's inconsistently enumerated
      if(ret.indexOf(dev) == -1 && dev.vendor != GPUVendor::Software)
        ret.push_back(dev);
    }

    SAFE_RELEASE(adapter);
  }

  // add WARP as long as we're not 12On7 (where we can't use WARP)
  if(!m_D3D12On7)
  {
    GPUDevice dev;
    dev.vendor = GPUVendor::Software;
    dev.deviceID = 0;
    dev.driver = "";    // D3D doesn't have multiple drivers per API
    dev.name = "WARP Rasterizer";
    dev.apis = {GraphicsAPI::D3D12};
    ret.push_back(dev);
  }

  return ret;
}

APIProperties D3D12Replay::GetAPIProperties()
{
  APIProperties ret = m_pDevice->APIProps;

  ret.pipelineType = GraphicsAPI::D3D12;
  ret.localRenderer = GraphicsAPI::D3D12;
  ret.vendor = m_DriverInfo.vendor;
  ret.degraded = false;
  ret.rgpCapture =
      (m_DriverInfo.vendor == GPUVendor::AMD || m_DriverInfo.vendor == GPUVendor::Samsung) &&
      m_RGP != NULL && m_RGP->DriverSupportsInterop();
  ret.shaderDebugging = true;
  ret.pixelHistory = true;

  return ret;
}

void D3D12Replay::ConfigureReplayCheckpoints(
    const ReplayCheckpointConfig &config, const rdcarray<ReplayCheckpointCandidate> &candidates)
{
  m_pDevice->ConfigureReplayCheckpoints(config, candidates);
}

void D3D12Replay::InvalidateReplayCheckpoints(uint32_t firstAffectedEvent)
{
  m_pDevice->InvalidateReplayCheckpoints(firstAffectedEvent);
}

ReplayCheckpointStats D3D12Replay::GetReplayCheckpointStats()
{
  return m_pDevice->GetReplayCheckpointStats();
}

bool D3D12Replay::RefreshReplayCheckpointMemoryBudget()
{
  return m_pDevice->RefreshReplayCheckpointMemoryBudget();
}

rdcstr D3D12Replay::SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches)
{
  return m_pDevice->SetReplayBufferPatches(patches);
}

uint32_t D3D12Replay::GetReplayBufferPatchFirstAffectedEvent() const
{
  return m_pDevice->GetReplayBufferPatchFirstAffectedEvent();
}

void D3D12Replay::ReplayLog(uint32_t endEventID, ReplayLogType replayType)
{
  const bool ordinaryTarget = replayType == eReplay_OnlyDraw && !m_EventShaderStateApplied &&
                              !m_EventCBufferStateApplied &&
                              m_EventCBufferOverrides.empty() &&
                              m_pDevice->GetQueue()->GetCommandData()->m_ActionCallback == NULL;
  const bool ordinaryRoot = replayType == eReplay_WithoutDraw && !m_ReplayCheckpointBypass &&
                            !m_EventCBufferStateApplied && m_EventCBufferOverrides.empty() &&
                            m_pDevice->GetQueue()->GetCommandData()->m_ActionCallback == NULL;
  if(replayType == eReplay_OnlyDraw && !m_EventShaderStateApplied)
  {
    const bool previousFeedbackScope = m_KnownOrdinaryTargetFeedback;
    m_KnownOrdinaryTargetFeedback = ordinaryTarget;
    bool replayed = FetchShaderFeedback(endEventID);
    m_KnownOrdinaryTargetFeedback = previousFeedbackScope;
    if(replayed)
    {
      if(ordinaryTarget)
        m_pDevice->CompleteLiveForwardTarget(endEventID);
      return;
    }
  }

  // Internal analysis paths call the device directly and retain its default checkpoint bypass.
  // Only the ordinary driver entry may reuse a prefix; the shared controller additionally
  // bypasses this entry for canonical verification, analysis, and temporary event overrides.
  // Prefix reuse and target provenance are separate: an unmodified target can preserve its
  // matching root's complete dirty-write proof, including a natural reference replay.
  const bool naturalReference = m_ReplayCheckpointBypass && m_ReplayCheckpointNaturalReference &&
                                !m_EventCBufferStateApplied &&
                                m_pDevice->GetQueue()->GetCommandData()->m_ActionCallback == NULL;
  const bool bufferMutationRoot = m_ReplayBufferMutationRoot && !m_EventShaderStateApplied &&
                                  !m_EventCBufferStateApplied && m_EventCBufferOverrides.empty() &&
                                  m_pDevice->GetQueue()->GetCommandData()->m_ActionCallback == NULL;
  const bool shaderMutationRoot =
      m_ReplayShaderMutationRoot && m_pDevice->CanTrackCheckedShaderMutation() &&
      !m_EventShaderStateApplied && !m_EventCBufferStateApplied && m_EventCBufferOverrides.empty() &&
      m_pDevice->GetQueue()->GetCommandData()->m_ActionCallback == NULL;
  m_pDevice->ReplayLog(0, endEventID, replayType, !m_ReplayCheckpointBypass, false, ordinaryTarget,
                       naturalReference, ordinaryRoot, bufferMutationRoot, shaderMutationRoot);
  m_pDevice->AuditReplayDescriptorDecodeCache();

  if(ordinaryTarget)
    m_pDevice->CompleteLiveForwardTarget(endEventID);

  if(replayType == eReplay_WithoutDraw)
  {
    MarkGPUPhaseAudit(2);
    const bool defer = ordinaryRoot && !m_pDevice->GetReplayOptions().apiValidation &&
                       Process::GetEnvVariable("RENDERDOC_D3D12_DEFER_PREFIX_COMPLETION") == "1";
    PerformanceTimer timer;
    if(!defer)
      m_pDevice->ReplayWorkWaitForIdle();
    if(!defer)
      FinishGPUPhaseAudit(!m_pDevice->HasFatalError());
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      AppendReplayTimingLog(
          StringFormat::Fmt("api=D3D12PostPrefixWait event=%u deferred=%u duration_ms=%.6f",
                            endEventID, defer ? 1U : 0U, timer.GetMilliseconds()));
  }
}

void D3D12Replay::BeginGPUPhaseAudit(uint32_t eventId)
{
  // The caller has drained the previous full prefix before entering initial restoration.
  if(!m_GPUPhaseAudits.empty())
    FinishGPUPhaseAudit(true);
  if(Process::GetEnvVariable("RENDERDOC_D3D12_GPU_PHASE_TIMING") != "1" ||
     Process::GetEnvVariable("RENDERDOC_EID_TIMING") != "1")
    return;
  m_GPUPhaseAuditEvent = eventId;
  D3D12_HEAP_PROPERTIES heap = {};
  heap.Type = D3D12_HEAP_TYPE_READBACK;
  heap.CreationNodeMask = heap.VisibleNodeMask = 1;
  D3D12_RESOURCE_DESC buffer = {};
  buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
  buffer.Width = sizeof(uint64_t) * 3;
  buffer.Height = buffer.SampleDesc.Count = 1;
  buffer.DepthOrArraySize = buffer.MipLevels = 1;
  buffer.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
  // Captured COPY queues need optional copy-timestamp support. Skip them rather than assume it.
  // Every allocation is fixed-size and retired at the existing completion boundary.
  const auto &queues = m_pDevice->GetQueues();
  for(WrappedID3D12CommandQueue *wrapped : queues)
  {
    if(m_GPUPhaseAudits.size() >= 16)
      break;
    if(!wrapped)
      continue;
    GPUPhaseAudit probe;
    probe.queue = wrapped->GetReal();
    probe.queueId = wrapped->GetResourceID();
    const D3D12_COMMAND_LIST_TYPE type = probe.queue->GetDesc().Type;
    if((type != D3D12_COMMAND_LIST_TYPE_DIRECT && type != D3D12_COMMAND_LIST_TYPE_COMPUTE) ||
       FAILED(probe.queue->GetTimestampFrequency(&probe.frequency)) || !probe.frequency)
      continue;
    D3D12_QUERY_HEAP_DESC query = {};
    query.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    query.Count = 3;
    bool ok = SUCCEEDED(m_pDevice->GetReal()->CreateQueryHeap(&query, __uuidof(ID3D12QueryHeap),
                                                              (void **)&probe.queries));
    ok = ok && SUCCEEDED(m_pDevice->GetReal()->CreateCommittedResource(
                   &heap, D3D12_HEAP_FLAG_NONE, &buffer, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                   __uuidof(ID3D12Resource), (void **)&probe.readback));
    ok = ok && SUCCEEDED(m_pDevice->GetReal()->CreateCommandAllocator(
                   type, __uuidof(ID3D12CommandAllocator), (void **)&probe.allocator));
    for(uint32_t i = 0; ok && i < 3; i++)
    {
      ok = SUCCEEDED(m_pDevice->GetReal()->CreateCommandList(0, type, probe.allocator, NULL,
                                                             __uuidof(ID3D12GraphicsCommandList),
                                                             (void **)&probe.markers[i]));
      if(ok)
      {
        probe.markers[i]->EndQuery(probe.queries, D3D12_QUERY_TYPE_TIMESTAMP, i);
        if(i == 2)
          probe.markers[i]->ResolveQueryData(probe.queries, D3D12_QUERY_TYPE_TIMESTAMP, 0, 3,
                                             probe.readback, 0);
        ok = SUCCEEDED(probe.markers[i]->Close());
      }
    }
    if(ok)
      m_GPUPhaseAudits.push_back(probe);
    else
    {
      for(auto &marker : probe.markers)
        SAFE_RELEASE(marker);
      SAFE_RELEASE(probe.allocator);
      SAFE_RELEASE(probe.readback);
      SAFE_RELEASE(probe.queries);
    }
  }
  MarkGPUPhaseAudit(0);
}

void D3D12Replay::MarkGPUPhaseAudit(uint32_t index)
{
  for(GPUPhaseAudit &probe : m_GPUPhaseAudits)
    if(index < 3 && probe.issued == index)
    {
      ID3D12CommandList *list = probe.markers[index];
      probe.queue->ExecuteCommandLists(1, &list);
      probe.issued++;
      m_pDevice->MarkReplayWorkSubmitted(false);
    }
}

void D3D12Replay::FinishGPUPhaseAudit(bool completed)
{
  if(!completed)
    return;    // A failed fence never authorises releasing live query storage/allocators.
  for(GPUPhaseAudit &probe : m_GPUPhaseAudits)
  {
    uint64_t *ticks = NULL;
    D3D12_RANGE read = {0, 3 * sizeof(uint64_t)}, written = {0, 0};
    if(completed && probe.issued == 3 && SUCCEEDED(probe.readback->Map(0, &read, (void **)&ticks)))
    {
      if(ticks[0] <= ticks[1] && ticks[1] <= ticks[2])
        AppendReplayTimingLog(StringFormat::Fmt(
            "api=D3D12GPUPhases event=%u queue=%s type=%u initial_interval_ms=%.6f "
            "prefix_interval_ms=%.6f total_interval_ms=%.6f scope=queueIntervalsIncludingGaps",
            m_GPUPhaseAuditEvent, ToStr(probe.queueId).c_str(),
            (uint32_t)probe.queue->GetDesc().Type, (ticks[1] - ticks[0]) * 1000.0 / probe.frequency,
            (ticks[2] - ticks[1]) * 1000.0 / probe.frequency,
            (ticks[2] - ticks[0]) * 1000.0 / probe.frequency));
      probe.readback->Unmap(0, &written);
    }
    for(auto &marker : probe.markers)
      SAFE_RELEASE(marker);
    SAFE_RELEASE(probe.allocator);
    SAFE_RELEASE(probe.readback);
    SAFE_RELEASE(probe.queries);
  }
  m_GPUPhaseAudits.clear();
}

namespace
{
// Paired observations are deliberately smaller than general resource checkpoints. A direct VS/PS
// draw may change only its color/depth attachments; every other input remains from one prefix.
struct D3D12ShaderPairAttachment
{
  ID3D12Resource *source = NULL;
  ID3D12Resource *before = NULL;
  ID3D12Resource *a = NULL;
  ID3D12Resource *b = NULL;
  ShaderPairSnapshot snapshot;
  uint64_t bytes = 0;
};

struct D3D12ShaderPairScratch
{
  D3D12ShaderPairScratch(WrappedID3D12Device *d, rdcarray<ID3D12DeviceChild *> &retained)
      : device(d), unconfirmed(retained)
  {
  }
  WrappedID3D12Device *device;
  rdcarray<ID3D12DeviceChild *> &unconfirmed;
  rdcarray<ID3D12DeviceChild *> owned;
  bool submitted = false;

  ~D3D12ShaderPairScratch()
  {
    // Never free command-list dependencies while the GPU might still be using them. A device-loss
    // path retains only this bounded transaction's resources until the device itself is destroyed.
    if(submitted && !device->ReplayWorkWaitForIdle())
    {
      for(ID3D12DeviceChild *resource : owned)
        unconfirmed.push_back(resource);
      return;
    }
    for(ID3D12DeviceChild *resource : owned)
      resource->Release();
  }
};

bool ShaderPairResourcesOverlap(WrappedID3D12Device *device, ID3D12Resource *left,
                                ID3D12Resource *right)
{
  if(left == right)
    return true;
  WrappedID3D12Resource *a = (WrappedID3D12Resource *)left;
  WrappedID3D12Resource *b = (WrappedID3D12Resource *)right;
  if(a->GetHeap() == NULL || a->GetHeap() != b->GetHeap())
    return false;
  const D3D12_RESOURCE_DESC ad = left->GetDesc(), bd = right->GetDesc();
  const uint64_t as = device->GetReal()->GetResourceAllocationInfo(0, 1, &ad).SizeInBytes;
  const uint64_t bs = device->GetReal()->GetResourceAllocationInfo(0, 1, &bd).SizeInBytes;
  const uint64_t ao = a->GetHeapOffset(), bo = b->GetHeapOffset();
  if(as == ~0ULL || bs == ~0ULL || ao > ~0ULL - as || bo > ~0ULL - bs)
    return true;
  return ao < bo + bs && bo < ao + as;
}

rdcstr CheckShaderPairInputs(WrappedID3D12Device *device, const D3D12RenderState &state,
                            const rdcarray<D3D12ShaderPairAttachment> &attachments)
{
  D3D12ResourceManager *rm = device->GetResourceManager();
  WrappedID3D12RootSignature *sig =
      rm->GetResAs<WrappedID3D12RootSignature>(state.graphics.rootsig, true);
  if(!sig)
    return "The graphics root signature is unavailable";
  if(sig->sig.Flags & (D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED |
                      D3D12_ROOT_SIGNATURE_FLAG_SAMPLER_HEAP_DIRECTLY_INDEXED))
    return "Paired observations do not support directly indexed descriptor heaps";

  auto checkResource = [&](ResourceId id) -> bool {
    if(id == ResourceId())
      return true;
    ID3D12Resource *resource = rm->GetResAs<ID3D12Resource>(id, true);
    if(!resource)
      return false;
    // Reserved inputs can alias an output through tile mappings without exposing a placed heap.
    // Require a conventional allocation before relying on the heap-range overlap check below.
    D3D12_HEAP_PROPERTIES heap = {};
    D3D12_HEAP_FLAGS flags = D3D12_HEAP_FLAG_NONE;
    if(FAILED(resource->GetHeapProperties(&heap, &flags)))
      return false;
    for(const D3D12ShaderPairAttachment &attachment : attachments)
      if(ShaderPairResourcesOverlap(device, resource, attachment.source))
        return false;
    return true;
  };

  if(!checkResource(state.ibuffer.buf))
    return "An index buffer is unavailable, sparse, or aliases a paired output";
  for(const D3D12RenderState::VertBuffer &vb : state.vbuffers)
    if(!checkResource(vb.buf))
      return "A vertex buffer is unavailable, sparse, or aliases a paired output";

  uint64_t checkedDescriptors = 0;
  for(size_t parameter = 0; parameter < state.graphics.sigelems.size(); parameter++)
  {
    const D3D12RenderState::SignatureElement &element = state.graphics.sigelems[parameter];
    if(element.type == eRootUnknown || element.type == eRootConst)
      continue;
    if(parameter >= sig->sig.Parameters.size())
      return "The graphics root elements do not match their signature";
    if(element.type == eRootCBV || element.type == eRootSRV || element.type == eRootUAV)
    {
      if(!checkResource(element.id))
        return "A root descriptor is unavailable, sparse, or aliases a paired output";
      continue;
    }
    if(element.type != eRootTable)
      return "The graphics root element type is unsupported";
    const D3D12RootSignatureParameter &root = sig->sig.Parameters[parameter];
    if(root.ParameterType != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE)
      return "A graphics descriptor table does not match its signature";
    WrappedID3D12DescriptorHeap *heap = rm->GetResAs<WrappedID3D12DescriptorHeap>(element.id, true);
    if(!heap)
      return "A bound descriptor heap is unavailable";
    uint64_t appendOffset = 0;
    for(UINT rangeIndex = 0; rangeIndex < root.DescriptorTable.NumDescriptorRanges; rangeIndex++)
    {
      const D3D12_DESCRIPTOR_RANGE1 &range = root.DescriptorTable.pDescriptorRanges[rangeIndex];
      const uint64_t offset = range.OffsetInDescriptorsFromTableStart ==
                                      D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND
                                  ? appendOffset
                                  : range.OffsetInDescriptorsFromTableStart;
      if(range.NumDescriptors == ~0U || range.NumDescriptors > 4096 ||
         checkedDescriptors + range.NumDescriptors > 4096 ||
         element.offset > heap->GetDesc().NumDescriptors ||
         offset > heap->GetDesc().NumDescriptors - element.offset ||
         range.NumDescriptors > heap->GetDesc().NumDescriptors - element.offset - offset)
        return "The paired descriptor closure is unbounded or unavailable";
      appendOffset = offset + range.NumDescriptors;
      checkedDescriptors += range.NumDescriptors;
      if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SAMPLER)
        continue;
      for(UINT i = 0; i < range.NumDescriptors; i++)
      {
        const D3D12Descriptor &descriptor =
            heap->GetDescriptors()[size_t(element.offset + offset + i)];
        if(descriptor.GetType() == D3D12DescriptorType::Undefined)
          continue;
        const ResourceId resource = descriptor.GetType() == D3D12DescriptorType::CBV
                                        ? WrappedID3D12Resource::GetResIDFromAddr(
                                              descriptor.GetCBV().BufferLocation)
                                        : descriptor.GetResResourceId();
        if(!checkResource(resource) ||
           (descriptor.GetType() == D3D12DescriptorType::UAV &&
            !checkResource(descriptor.GetCounterResourceId())))
          return "A shader descriptor is unavailable, sparse, or aliases a paired output";
      }
    }
  }
  return {};
}

void ShaderPairCopy(WrappedID3D12Device *device, ID3D12GraphicsCommandListX *list,
                    ID3D12Resource *original, ID3D12Resource *snapshot, bool restore)
{
  BarrierSet barriers;
  barriers.Configure(original, device->GetSubresourceStates(GetResID(original)),
                     restore ? BarrierSet::CopyDestAccess : BarrierSet::CopySourceAccess);
  barriers.Apply(list);
  list->CopyResource(restore ? original : snapshot, restore ? snapshot : original);
  barriers.Unapply(list);
  if(!restore)
  {
    D3D12_RESOURCE_BARRIER ready = {};
    ready.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    ready.Transition.pResource = snapshot;
    ready.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    ready.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    ready.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    list->ResourceBarrier(1, &ready);
    for(D3D12ResourceLayout &layout : device->GetSubresourceStates(GetResID(snapshot)))
      layout = D3D12ResourceLayout::FromStates(D3D12_RESOURCE_STATE_COPY_SOURCE);
  }
}
}

ShaderPairComparison D3D12Replay::ComparePixelShaderPair(uint32_t eventId,
                                                        ResourceId expectedOriginal,
                                                        ResourceId baseline, ResourceId candidate)
{
  ShaderPairComparison result;
  result.eventId = eventId;
  const ActionDescription *action = m_pDevice->GetAction(eventId);
  D3D12CommandData &cmd = *m_pDevice->GetQueue()->GetCommandData();
  auto reject = [&](const rdcstr &reason) -> ShaderPairComparison {
    result.reason = reason;
    return std::move(result);
  };
  if(!action || !(action->flags & ActionFlags::Drawcall) ||
     (action->flags & (ActionFlags::Indirect | ActionFlags::MeshDispatch)) || action->drawIndex != 0)
    return reject("Paired observations require one direct Draw or DrawIndexed action");
  if(cmd.m_ActionCallback || m_EventCBufferStateApplied || !m_EventCBufferOverrides.empty())
    return reject("Paired observations cannot nest analysis callbacks or event overrides");
  if(!m_ShaderPairUnconfirmed.empty())
  {
    result.supported = true;
    return reject("A previous paired observation has unconfirmed GPU work");
  }

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  if(!WrappedID3D12Shader::IsShader(expectedOriginal) || rm->HasReplacement(expectedOriginal))
    return reject("The original pixel shader must be live and have no shared replacement");
  if(baseline == ResourceId())
    baseline = expectedOriginal;
  if(candidate == ResourceId())
    candidate = expectedOriginal;
  if(!WrappedID3D12Shader::IsShader(baseline) || !WrappedID3D12Shader::IsShader(candidate))
    return reject("Both paired shaders must be live native shader resources");

  // Exactly one canonical prefix, with no target execution, shader-feedback pass, checkpoint
  // reuse, or viewer refresh. Every A/B/A draw below sees the very same input resource bytes.
  m_pDevice->ReplayLog(0, eventId, eReplay_WithoutDraw);
  if(!m_pDevice->ReplayWorkWaitForIdle() || m_pDevice->HasFatalError())
  {
    result.supported = true;
    return reject("The paired observation prefix did not complete");
  }
  const D3D12RenderState state = cmd.m_RenderState;
  WrappedID3D12PipelineState *pipe = rm->GetResAs<WrappedID3D12PipelineState>(state.pipe, true);
  if(!pipe || !pipe->IsGraphics() || !pipe->VS() || !pipe->PS() ||
     pipe->PS()->GetResourceID() != expectedOriginal)
    return reject("The selected pipeline does not bind the expected original pixel shader");
  bool streamout = false;
  for(const D3D12RenderState::StreamOut &stream : state.streamouts)
    streamout |= stream.buf != ResourceId() || stream.countbuf != ResourceId();
  if(state.renderpass || state.stateobj != ResourceId() || streamout ||
     state.predication.buffer != ResourceId() || state.shadingRateImage != ResourceId() ||
     pipe->HS() || pipe->DS() || pipe->GS() || pipe->AS() || pipe->MS())
    return reject("Paired observations require a VS/PS draw without render passes, stream-out, "
                  "predication, shading-rate images, or additional shader stages");

  WrappedID3D12Shader *aShader = rm->GetResAs<WrappedID3D12Shader>(baseline, true);
  WrappedID3D12Shader *bShader = rm->GetResAs<WrappedID3D12Shader>(candidate, true);
  if(!aShader || !bShader || aShader->GetDetails().stage != ShaderStage::Pixel ||
     bShader->GetDetails().stage != ShaderStage::Pixel)
    return reject("Both paired replacements must be pixel shaders");
  for(WrappedID3D12Shader *shader : {pipe->VS(), pipe->PS(), aShader, bShader})
  {
    uint32_t extensionSlot = ~0U, extensionSpace = ~0U;
    shader->GetShaderExtSlot(extensionSlot, extensionSpace);
    if(!shader->GetDetails().readWriteResources.empty() || extensionSlot != ~0U)
      return reject("Paired observations cannot repeat shaders with UAV or vendor-extension effects");
  }

  rdcarray<D3D12ShaderPairAttachment> attachments;
  uint64_t totalBytes = 0;
  auto addAttachment = [&](const D3D12Descriptor &view, uint32_t index, bool depth) -> rdcstr {
    const ResourceId id = view.GetResResourceId();
    if(id == ResourceId())
      return {};
    ID3D12Resource *resource = rm->GetResAs<ID3D12Resource>(id, true);
    if(!resource)
      return "A paired output resource is unavailable";
    const D3D12_RESOURCE_DESC desc = resource->GetDesc();
    if(desc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D || desc.MipLevels != 1 ||
       desc.DepthOrArraySize != 1 || desc.SampleDesc.Count != 1 || desc.Width == 0 ||
       desc.Height == 0 || desc.Width > 8192 || desc.Height > 8192)
      return "Paired outputs must be single-sample 2D textures with one mip and one array slice";
    if(depth ? (view.GetDSV().ViewDimension != D3D12_DSV_DIMENSION_TEXTURE2D ||
                view.GetDSV().Texture2D.MipSlice != 0)
             : (view.GetRTV().ViewDimension != D3D12_RTV_DIMENSION_TEXTURE2D ||
                view.GetRTV().Texture2D.MipSlice != 0))
      return "The paired output view does not cover its supported subresource";
    D3D12_HEAP_PROPERTIES heap = {};
    D3D12_HEAP_FLAGS flags = D3D12_HEAP_FLAG_NONE;
    if(FAILED(resource->GetHeapProperties(&heap, &flags)))
      return "Paired observations do not support sparse output resources";
    const uint64_t bytes = GetByteSize((int)desc.Width, (int)desc.Height, 1, desc.Format, 0);
    if(bytes == 0 || bytes > 32ULL * 1024 * 1024 ||
       totalBytes + bytes > 64ULL * 1024 * 1024 || attachments.size() >= 9)
      return "Paired output data exceeds the 32 MiB per-output or 64 MiB per-leg budget";
    for(const D3D12ShaderPairAttachment &existing : attachments)
      if(ShaderPairResourcesOverlap(m_pDevice, existing.source, resource))
        return "Paired output resources overlap or alias each other";
    D3D12ShaderPairAttachment attachment;
    attachment.source = resource;
    attachment.bytes = bytes;
    attachment.snapshot.resourceId = id;
    attachment.snapshot.index = index;
    attachment.snapshot.depth = depth;
    attachment.snapshot.width = (uint32_t)desc.Width;
    attachment.snapshot.height = desc.Height;
    attachment.snapshot.format = GetTexture(id).format;
    const DXGI_FORMAT viewFormat = depth ? view.GetDSV().Format : view.GetRTV().Format;
    if(viewFormat != DXGI_FORMAT_UNKNOWN)
      attachment.snapshot.format = MakeResourceFormat(viewFormat);
    attachments.push_back(std::move(attachment));
    totalBytes += bytes;
    return {};
  };
  for(uint32_t i = 0; i < state.rts.size(); i++)
  {
    rdcstr error = addAttachment(state.rts[i], i, false);
    if(!error.empty())
      return reject(error);
  }
  if(state.dsv.GetType() == D3D12DescriptorType::DSV)
  {
    rdcstr error = addAttachment(state.dsv, 0, true);
    if(!error.empty())
      return reject(error);
  }
  if(attachments.empty())
    return reject("The paired draw has no stored output attachments");
  rdcstr inputError = CheckShaderPairInputs(m_pDevice, state, attachments);
  if(!inputError.empty())
    return reject(inputError);

  result.supported = true;
  D3D12ShaderPairScratch scratch(m_pDevice, m_ShaderPairUnconfirmed);
  D3D12_EXPANDED_PIPELINE_STATE_STREAM_DESC description;
  pipe->Fill(description);
  description.CachedPSO = {};
  ID3D12PipelineState *aPipeline = NULL, *bPipeline = NULL;
  description.PS = aShader->GetDesc();
  HRESULT hr = m_pDevice->CreatePipeState(description, &aPipeline);
  if(FAILED(hr) || !aPipeline)
    return reject("Failed to create the isolated baseline pixel pipeline");
  scratch.owned.push_back(aPipeline);
  description.PS = bShader->GetDesc();
  hr = m_pDevice->CreatePipeState(description, &bPipeline);
  if(FAILED(hr) || !bPipeline)
    return reject("Failed to create the isolated candidate pixel pipeline");
  scratch.owned.push_back(bPipeline);
  for(D3D12ShaderPairAttachment &attachment : attachments)
  {
    D3D12_RESOURCE_DESC desc = attachment.source->GetDesc();
    desc.Alignment = 0;
    D3D12_HEAP_PROPERTIES heap = {};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    for(ID3D12Resource **destination : {&attachment.before, &attachment.a, &attachment.b})
    {
      hr = m_pDevice->CreateCommittedResource(
          &heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
          __uuidof(ID3D12Resource), (void **)destination);
      if(FAILED(hr) || !*destination)
        return reject("Failed to allocate bounded paired output snapshots");
      scratch.owned.push_back(*destination);
    }
  }
  // Keep this list out of the generic pending queue until it is completely recorded and closed.
  // A failed Close must never leave a later unrelated ExecuteLists holding released scratch.
  ID3D12GraphicsCommandListX *list = m_pDevice->StealNewList();
  if(!list)
    return reject("Failed to create the paired observation command list");
  for(D3D12ShaderPairAttachment &attachment : attachments)
    ShaderPairCopy(m_pDevice, list, attachment.source, attachment.before, false);

  state.ApplyState(m_pDevice, list);
  list->SetPipelineState(aPipeline);
  m_pDevice->ReplayDraw(list, *action);
  for(D3D12ShaderPairAttachment &attachment : attachments)
  {
    ShaderPairCopy(m_pDevice, list, attachment.source, attachment.a, false);
    ShaderPairCopy(m_pDevice, list, attachment.source, attachment.before, true);
  }
  list->SetPipelineState(bPipeline);
  m_pDevice->ReplayDraw(list, *action);
  for(D3D12ShaderPairAttachment &attachment : attachments)
  {
    ShaderPairCopy(m_pDevice, list, attachment.source, attachment.b, false);
    ShaderPairCopy(m_pDevice, list, attachment.source, attachment.before, true);
  }
  list->SetPipelineState(aPipeline);
  m_pDevice->ReplayDraw(list, *action);
  // Keep the captured CPU state authoritative. Only the local command list used the trial PSOs.
  state.ApplyState(m_pDevice, list);
  if(FAILED(list->Close()) || m_pDevice->HasFatalError())
  {
    list->Release();
    return reject("Failed to close the paired observation command list");
  }
  scratch.submitted = true;
  m_pDevice->ExecuteList(list);
  if(!m_pDevice->FlushLists(true) || !m_pDevice->ReplayWorkWaitForIdle() || m_pDevice->HasFatalError())
    return reject("The paired observation GPU work did not complete");

  for(D3D12ShaderPairAttachment &attachment : attachments)
  {
    ShaderPairSnapshot &output = attachment.snapshot;
    GetTextureData(GetResID(attachment.a), output.subresource, GetTextureDataParams(), output.baseline);
    GetTextureData(GetResID(attachment.b), output.subresource, GetTextureDataParams(), output.candidate);
    GetTextureData(output.resourceId, output.subresource, GetTextureDataParams(), output.baselineRepeat);
    if(output.baseline.size() != attachment.bytes || output.candidate.size() != attachment.bytes ||
       output.baselineRepeat.size() != attachment.bytes || m_pDevice->HasFatalError())
      return reject("The paired observation did not return complete native attachment data");
    result.outputs.push_back(std::move(output));
  }
  result.completed = true;
  return result;
}

SDFile *D3D12Replay::GetStructuredFile()
{
  return m_pDevice->GetStructuredFile();
}

ResourceDescription &D3D12Replay::GetResourceDesc(ResourceId id)
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

rdcarray<ResourceDescription> D3D12Replay::GetResources()
{
  return m_Resources;
}

rdcarray<DescriptorStoreDescription> D3D12Replay::GetDescriptorStores()
{
  return m_DescriptorStores;
}

void D3D12Replay::RegisterDescriptorStore(const DescriptorStoreDescription &desc)
{
  m_DescriptorStores.push_back(desc);
}

rdcarray<BufferDescription> D3D12Replay::GetBuffers()
{
  rdcarray<BufferDescription> ret;

  for(auto it = m_pDevice->GetResourceList().begin(); it != m_pDevice->GetResourceList().end(); it++)
    if(it->second->GetDesc().Dimension == D3D12_RESOURCE_DIMENSION_BUFFER &&
       !ResourceIDGen::IsReplayOnlyID(it->first))
      ret.push_back(GetBuffer(it->first));

  return ret;
}

rdcarray<TextureDescription> D3D12Replay::GetTextures()
{
  rdcarray<TextureDescription> ret;

  for(auto it = m_pDevice->GetResourceList().begin(); it != m_pDevice->GetResourceList().end(); it++)
  {
    if(it->second->GetDesc().Dimension != D3D12_RESOURCE_DIMENSION_BUFFER &&
       !ResourceIDGen::IsReplayOnlyID(it->first))
      ret.push_back(GetTexture(it->first));
  }

  return ret;
}

BufferDescription D3D12Replay::GetBuffer(ResourceId id)
{
  BufferDescription ret = {};
  ret.resourceId = id;

  auto it = m_pDevice->GetResourceList().find(id);

  if(it == m_pDevice->GetResourceList().end() || it->second == NULL)
    return ret;

  D3D12_RESOURCE_DESC desc = it->second->GetDesc();

  ret.length = desc.Width;

  ret.creationFlags = BufferCategory::NoFlags;
  ret.gpuAddress = it->second->GetOriginalVA();

  if(it->second->GetHeap())
  {
    ret.memory = it->second->GetHeap()->GetResourceID();
    ret.memoryOffset = it->second->GetHeapOffset();
  }

  const rdcarray<EventUsage> &usage = m_pDevice->GetQueue()->GetUsage(id);

  for(size_t i = 0; i < usage.size(); i++)
  {
    if(usage[i].usage == ResourceUsage::VS_RWResource ||
       usage[i].usage == ResourceUsage::HS_RWResource ||
       usage[i].usage == ResourceUsage::DS_RWResource ||
       usage[i].usage == ResourceUsage::GS_RWResource ||
       usage[i].usage == ResourceUsage::PS_RWResource ||
       usage[i].usage == ResourceUsage::CS_RWResource)
      ret.creationFlags |= BufferCategory::ReadWrite;
    else if(usage[i].usage == ResourceUsage::VertexBuffer)
      ret.creationFlags |= BufferCategory::Vertex;
    else if(usage[i].usage == ResourceUsage::IndexBuffer)
      ret.creationFlags |= BufferCategory::Index;
    else if(usage[i].usage == ResourceUsage::Indirect)
      ret.creationFlags |= BufferCategory::Indirect;
  }

  if(desc.Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS)
    ret.creationFlags |= BufferCategory::ReadWrite;

  return ret;
}

TextureDescription D3D12Replay::GetTexture(ResourceId id)
{
  TextureDescription ret = {};
  ret.resourceId = id;

  auto it = m_pDevice->GetResourceList().find(id);

  if(it == m_pDevice->GetResourceList().end() || it->second == NULL)
    return ret;

  D3D12_RESOURCE_DESC desc = it->second->GetDesc();

  ret.format = MakeResourceFormat(desc.Format);
  ret.dimension = desc.Dimension - D3D12_RESOURCE_DIMENSION_BUFFER;

  ret.width = (uint32_t)desc.Width;
  ret.height = desc.Height;
  ret.depth = desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D ? desc.DepthOrArraySize : 1;
  ret.arraysize = desc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE3D ? desc.DepthOrArraySize : 1;
  ret.mips = desc.MipLevels;
  ret.msQual = desc.SampleDesc.Quality;
  ret.msSamp = RDCMAX(1U, desc.SampleDesc.Count);

  if(desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER)
  {
    ret.byteSize = desc.Width;
  }
  else
  {
    ret.byteSize = m_pDevice->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
  }

  if(ret.byteSize == 0)
  {
    for(uint32_t i = 0; i < ret.mips; i++)
      ret.byteSize += GetByteSize(ret.width, ret.height, ret.depth, desc.Format, i);
    ret.byteSize *= ret.arraysize;
    ret.byteSize *= ret.msSamp;
  }

  if(it->second->GetHeap())
  {
    ret.memory = it->second->GetHeap()->GetResourceID();
    ret.memoryOffset = it->second->GetHeapOffset();
  }

  switch(ret.dimension)
  {
    case 1:
      ret.type = ret.arraysize > 1 ? TextureType::Texture1DArray : TextureType::Texture1D;
      break;
    case 2:
      if(ret.msSamp > 1)
        ret.type = ret.arraysize > 1 ? TextureType::Texture2DMSArray : TextureType::Texture2DMS;
      else
        ret.type = ret.arraysize > 1 ? TextureType::Texture2DArray : TextureType::Texture2D;
      break;
    case 3: ret.type = TextureType::Texture3D; break;
  }

  ret.cubemap = m_pDevice->IsCubemap(id);

  ret.creationFlags = TextureCategory::ShaderRead;

  if(desc.Flags & D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET)
    ret.creationFlags |= TextureCategory::ColorTarget;
  if(desc.Flags & D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL)
    ret.creationFlags |= TextureCategory::DepthTarget;
  if(desc.Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS)
    ret.creationFlags |= TextureCategory::ShaderReadWrite;

  {
    auto resit = m_ResourceIdx.find(ret.resourceId);
    if(resit != m_ResourceIdx.end() && m_Resources[resit->second].type == ResourceType::SwapchainImage)
    {
      ret.format = MakeResourceFormat(GetTypedFormat(desc.Format, CompType::UNorm));
      ret.creationFlags |= TextureCategory::SwapBuffer;
    }
  }

  return ret;
}

rdcarray<ShaderEntryPoint> D3D12Replay::GetShaderEntryPoints(ResourceId shader)
{
  if(!WrappedID3D12Shader::IsShader(shader))
    return {};

  ID3D12DeviceChild *res = m_pDevice->GetResourceManager()->GetResource(shader);

  if(!res)
    return {};

  WrappedID3D12Shader *sh = (WrappedID3D12Shader *)res;
  const DXBC::DXBCContainer *container = sh->GetDXBC();

  if(container != NULL)
  {
    rdcarray<ShaderEntryPoint> entries = container->GetEntryPoints();
    if(!entries.empty())
      return entries;
  }

  const ShaderReflection &ret = sh->GetDetails();
  return {{ret.entryPoint.empty() ? "main" : ret.entryPoint, ret.stage}};
}

const ShaderReflection *D3D12Replay::GetShader(ResourceId pipeline, ResourceId shader,
                                               ShaderEntryPoint entry)
{
  WrappedID3D12Shader *sh = m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12Shader>(shader);

  if(sh)
    return &sh->GetDetails();

  return NULL;
}

rdcarray<rdcstr> D3D12Replay::GetDisassemblyTargets(bool withPipeline)
{
  rdcarray<rdcstr> ret;

  // DXBC/DXIL is always first
  ret.push_back(DXBCDXILDisassemblyTarget);
  // DXC DXIL
  ret.push_back(DXCDXILDisassemblyTarget);

  if(!m_ISAChecked && m_TexRender.BlendPipe)
  {
    m_ISAChecked = true;

    UINT size = 0;
    m_TexRender.BlendPipe->GetPrivateData(WKPDID_CommentStringW, &size, NULL);

    if(size > 0)
    {
      byte *isa = new byte[size + 1];
      m_TexRender.BlendPipe->GetPrivateData(WKPDID_CommentStringW, &size, isa);

      if(strstr((const char *)isa, "disassembly requires a participating driver") == NULL &&
         wcsstr((const wchar_t *)isa, L"disassembly requires a participating driver") == NULL)
      {
        m_ISAAvailable =
            strstr((const char *)isa, "<shader") || wcsstr((const wchar_t *)isa, L"<shader");
      }

      delete[] isa;
    }
  }

  if(m_ISAAvailable)
    ret.push_back(LiveDriverDisassemblyTarget);

  return ret;
}

rdcstr D3D12Replay::DisassembleShader(ResourceId pipeline, const ShaderReflection *refl,
                                      const rdcstr &target)
{
  WrappedID3D12Shader *sh =
      m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12Shader>(refl->resourceId);

  if(!sh)
    return "; Invalid Shader Specified";

  if(target == DXBCDXILDisassemblyTarget || target.empty())
    return sh->GetWriteableDXBC()->GetDisassembly(false);

  if(target == DXCDXILDisassemblyTarget)
    return sh->GetWriteableDXBC()->GetDisassembly(true);

  if(target == LiveDriverDisassemblyTarget)
  {
    if(pipeline == ResourceId())
    {
      return "; No pipeline specified, live driver disassembly is not available\n"
             "; Shader must be disassembled with a specific pipeline to get live driver assembly.";
    }

    WrappedID3D12PipelineState *pipe =
        m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12PipelineState>(pipeline);

    UINT size = 0;
    pipe->GetPrivateData(WKPDID_CommentStringW, &size, NULL);

    if(size == 0)
      return "; Unknown error fetching disassembly, empty string returned\n";

    byte *data = new byte[size + 1];
    memset(data, 0, size);
    pipe->GetPrivateData(WKPDID_CommentStringW, &size, data);

    byte *iter = data;

    // need to handle wide and ascii strings to some extent. We assume that it doesn't change from
    // character to character, we just handle the initial <comments> being either one, then assume
    // the xml within is all consistent.
    if(!strncmp((char *)iter, "<comments", 9))
    {
      iter += 9;

      // find the end of the tag, advance past it
      iter = (byte *)strchr((char *)iter, '>') + 1;

      if(*(char *)iter == '\n')
        iter++;
    }
    else if(!wcsncmp((wchar_t *)iter, L"<comments", 9))
    {
      iter += 9 * sizeof(wchar_t);

      // find the end of the tag
      iter = (byte *)wcschr((wchar_t *)iter, L'>') + sizeof(wchar_t);

      if(*(wchar_t *)iter == L'\n')
        iter += sizeof(wchar_t);
    }
    else
      return "; Unknown error fetching disassembly, invalid string returned\n\n\n" +
             rdcstr((char *)data, size);

    rdcstr contents;

    // decode the <shader><comment> tags
    if(!strncmp((char *)iter, "<shader", 7))
      contents = (char *)iter;
    else if(!wcsncmp((wchar_t *)iter, L"<shader", 7))
      contents = StringFormat::Wide2UTF8((wchar_t *)iter);

    delete[] data;

    const char *search = "stage=\"?S\"";

    switch(refl->stage)
    {
      case ShaderStage::Vertex: search = "stage=\"VS\""; break;
      case ShaderStage::Hull: search = "stage=\"HS\""; break;
      case ShaderStage::Domain: search = "stage=\"DS\""; break;
      case ShaderStage::Geometry: search = "stage=\"GS\""; break;
      case ShaderStage::Pixel: search = "stage=\"PS\""; break;
      case ShaderStage::Compute: search = "stage=\"CS\""; break;
      case ShaderStage::Amplification: search = "stage=\"AS\""; break;
      case ShaderStage::Mesh: search = "stage=\"MS\""; break;
      default: return "; Unknown shader stage in shader reflection\n";
    }

    int32_t idx = contents.find(search);

    if(idx < 0)
      return "; Couldn't find disassembly for given shader stage in returned string\n\n\n" + contents;

    idx += 11;    // stage=".S">

    if(strncmp(contents.c_str() + idx, "<comment>", 9) != 0)
      return "; Unknown error fetching disassembly, invalid string returned\n\n\n" + contents;

    idx += 9;    // <comment>

    if(strncmp(contents.c_str() + idx, "<![CDATA[\n", 10) != 0)
      return "; Unknown error fetching disassembly, invalid string returned\n\n\n" + contents;

    idx += 10;    // <![CDATA[\n

    int32_t end = contents.find("]]></comment>", idx);

    if(end < 0)
      return "; Unknown error fetching disassembly, invalid string returned\n\n\n" + contents;

    return contents.substr(idx, end - idx);
  }

  return StringFormat::Fmt("; Invalid disassembly target %s", target.c_str());
}

void D3D12Replay::FreeTargetResource(ResourceId id)
{
  if(m_pDevice->GetResourceManager()->HasResource(id))
  {
    ID3D12DeviceChild *resource = m_pDevice->GetResourceManager()->GetResource(id);

    SAFE_RELEASE(resource);
  }
}

void D3D12Replay::FreeCustomShader(ResourceId id)
{
  if(m_pDevice->GetResourceManager()->HasResource(id))
  {
    ID3D12DeviceChild *resource = m_pDevice->GetResourceManager()->GetResource(id);

    SAFE_RELEASE(resource);
  }
}

rdcarray<EventUsage> D3D12Replay::GetUsage(ResourceId id)
{
  // Shader bytecode is exposed through GetResources(), but it is not an ID3D12Resource and
  // therefore is intentionally absent from the device resource list. Keep the ordinary resource
  // gate while allowing the complete Shader action-usage index populated during action
  // construction.
  if(m_pDevice->GetResourceList().find(id) == m_pDevice->GetResourceList().end() &&
     !WrappedID3D12Shader::IsShader(id))
  {
    return {EventUsage(0, ResourceUsage::Unused)};
  }

  return m_pDevice->GetQueue()->GetUsage(id);
}

void D3D12Replay::FillDescriptor(Descriptor &dst, const D3D12Descriptor *src)
{
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  if(src->GetType() == D3D12DescriptorType::Sampler || src->GetType() == D3D12DescriptorType::CBV)
  {
    return;
  }

  if(src->GetHeap() && src->GetHeap()->HasValidDescriptorCache(src->GetHeapIndex()))
  {
    src->GetHeap()->GetFromDescriptorCache(src->GetHeapIndex(), dst);
    return;
  }

  dst = {};
  dst.resource = src->GetResResourceId();

  if(dst.resource == ResourceId())
  {
    // TLASs annoyingly don't have a resource
    if(src->GetType() != D3D12DescriptorType::SRV ||
       src->GetSRV().ViewDimension != D3D12_SRV_DIMENSION_RAYTRACING_ACCELERATION_STRUCTURE)
    {
      if(src->GetHeap())
        src->GetHeap()->GetFromDescriptorCache(src->GetHeapIndex(), dst);
      else
        dst = {};
      return;
    }
  }

  D3D12_RESOURCE_DESC res = {};

  {
    ID3D12Resource *r = rm->GetResAs<ID3D12Resource>(src->GetResResourceId());
    if(r)
      res = r->GetDesc();
  }

  {
    DXGI_FORMAT fmt = DXGI_FORMAT_UNKNOWN;

    uint64_t firstElement = UINT64_MAX;
    uint32_t numElements = UINT32_MAX;

    if(src->GetType() == D3D12DescriptorType::SRV)
    {
      D3D12_SHADER_RESOURCE_VIEW_DESC srv = src->GetSRV();

      if(srv.ViewDimension == D3D12_SRV_DIMENSION_UNKNOWN)
        srv = MakeSRVDesc(res);

      if(srv.ViewDimension == D3D12_SRV_DIMENSION_BUFFER ||
         srv.ViewDimension == D3D12_SRV_DIMENSION_BUFFER_BYTE_OFFSET)
        dst.type = DescriptorType::TypedBuffer;
      else
        dst.type = DescriptorType::Image;

      fmt = srv.Format;

      dst.textureType = MakeTextureDim(srv.ViewDimension);

      dst.swizzle.red =
          (TextureSwizzle)D3D12_DECODE_SHADER_4_COMPONENT_MAPPING(0, srv.Shader4ComponentMapping);
      dst.swizzle.green =
          (TextureSwizzle)D3D12_DECODE_SHADER_4_COMPONENT_MAPPING(1, srv.Shader4ComponentMapping);
      dst.swizzle.blue =
          (TextureSwizzle)D3D12_DECODE_SHADER_4_COMPONENT_MAPPING(2, srv.Shader4ComponentMapping);
      dst.swizzle.alpha =
          (TextureSwizzle)D3D12_DECODE_SHADER_4_COMPONENT_MAPPING(3, srv.Shader4ComponentMapping);

      if(srv.ViewDimension == D3D12_SRV_DIMENSION_BUFFER)
      {
        firstElement = srv.Buffer.FirstElement;
        numElements = srv.Buffer.NumElements;

        dst.flags = MakeDescriptorFlags(srv.Buffer.Flags);
        if(srv.Buffer.StructureByteStride > 0)
        {
          dst.elementByteSize = srv.Buffer.StructureByteStride;
          dst.type = DescriptorType::Buffer;
        }
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_BUFFER_BYTE_OFFSET)
      {
        dst.byteOffset = srv.BufferByteOffset.Offset;
        dst.byteSize = srv.BufferByteOffset.Size;
        dst.flags = MakeDescriptorFlags(srv.BufferByteOffset.Flags);
        if(srv.BufferByteOffset.StructureByteStride > 0)
        {
          dst.elementByteSize = srv.BufferByteOffset.StructureByteStride;
          dst.type = DescriptorType::Buffer;
        }
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_RAYTRACING_ACCELERATION_STRUCTURE)
      {
        dst.type = DescriptorType::AccelerationStructure;

        ResourceId asID;
        WrappedID3D12Resource::GetResIDFromAddr(srv.RaytracingAccelerationStructure.Location, asID,
                                                dst.byteOffset);

        WrappedID3D12Resource *asRes = rm->GetResAs<WrappedID3D12Resource>(asID);

        if(asRes)
        {
          // we *should* get an AS here
          D3D12AccelerationStructure *as = NULL;
          if(asRes->GetAccStructIfExist(dst.byteOffset, &as))
          {
            dst.resource = as->GetResourceID();
            dst.byteOffset = 0;
            dst.byteSize = as->Size();
          }
          else
          {
            dst.resource = asID;
          }
        }
        else
        {
          dst.resource = ResourceId();
        }
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE1D)
      {
        dst.firstMip = srv.Texture1D.MostDetailedMip & 0xff;
        dst.numMips = srv.Texture1D.MipLevels & 0xff;
        dst.minLODClamp = srv.Texture1D.ResourceMinLODClamp;
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE1DARRAY)
      {
        dst.numSlices = srv.Texture1DArray.ArraySize & 0xffff;
        dst.firstSlice = srv.Texture1DArray.FirstArraySlice & 0xffff;
        dst.firstMip = srv.Texture1DArray.MostDetailedMip & 0xff;
        dst.numMips = srv.Texture1DArray.MipLevels & 0xff;
        dst.minLODClamp = srv.Texture1DArray.ResourceMinLODClamp;
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE2D)
      {
        dst.firstMip = srv.Texture2D.MostDetailedMip & 0xff;
        dst.numMips = srv.Texture2D.MipLevels & 0xff;
        dst.minLODClamp = srv.Texture2D.ResourceMinLODClamp;
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE2DARRAY)
      {
        dst.numSlices = srv.Texture2DArray.ArraySize & 0xffff;
        dst.firstSlice = srv.Texture2DArray.FirstArraySlice & 0xffff;
        dst.firstMip = srv.Texture2DArray.MostDetailedMip & 0xff;
        dst.numMips = srv.Texture2DArray.MipLevels & 0xff;
        dst.minLODClamp = srv.Texture2DArray.ResourceMinLODClamp;
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE2DMS)
      {
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE2DMSARRAY)
      {
        dst.numSlices = srv.Texture2DMSArray.ArraySize & 0xffff;
        dst.firstSlice = srv.Texture2DMSArray.FirstArraySlice & 0xffff;
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURE3D)
      {
        dst.firstMip = srv.Texture3D.MostDetailedMip & 0xff;
        dst.numMips = srv.Texture3D.MipLevels & 0xff;
        dst.minLODClamp = srv.Texture3D.ResourceMinLODClamp;
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURECUBE)
      {
        dst.numSlices = 6;
        dst.firstMip = srv.TextureCube.MostDetailedMip & 0xff;
        dst.numMips = srv.TextureCube.MipLevels & 0xff;
        dst.minLODClamp = srv.TextureCube.ResourceMinLODClamp;
      }
      else if(srv.ViewDimension == D3D12_SRV_DIMENSION_TEXTURECUBEARRAY)
      {
        dst.numSlices = (srv.TextureCubeArray.NumCubes * 6) & 0xffff;
        dst.firstSlice = srv.TextureCubeArray.First2DArrayFace & 0xffff;
        dst.firstMip = srv.TextureCubeArray.MostDetailedMip & 0xff;
        dst.numMips = srv.TextureCubeArray.MipLevels & 0xff;
        dst.minLODClamp = srv.TextureCube.ResourceMinLODClamp;
      }
    }
    else if(src->GetType() == D3D12DescriptorType::UAV)
    {
      D3D12_UNORDERED_ACCESS_VIEW_DESC uav = src->GetUAV();

      if(uav.ViewDimension == D3D12_UAV_DIMENSION_UNKNOWN)
        uav = MakeUAVDesc(res);

      if(uav.ViewDimension == D3D12_UAV_DIMENSION_BUFFER ||
         uav.ViewDimension == D3D12_UAV_DIMENSION_BUFFER_BYTE_OFFSET)
        dst.type = uav.Format != DXGI_FORMAT_UNKNOWN ? DescriptorType::ReadWriteTypedBuffer
                                                     : DescriptorType::ReadWriteBuffer;
      else
        dst.type = DescriptorType::ReadWriteImage;

      fmt = uav.Format;

      dst.secondary = src->GetCounterResourceId();

      dst.textureType = MakeTextureDim(uav.ViewDimension);

      if(uav.ViewDimension == D3D12_UAV_DIMENSION_BUFFER)
      {
        firstElement = uav.Buffer.FirstElement;
        numElements = uav.Buffer.NumElements;

        dst.flags = MakeDescriptorFlags(uav.Buffer.Flags);
        if(uav.Buffer.StructureByteStride > 0)
          dst.elementByteSize = uav.Buffer.StructureByteStride;

        dst.counterByteOffset = uav.Buffer.CounterOffsetInBytes & 0xffffffff;
        RDCASSERT(uav.Buffer.CounterOffsetInBytes < 0xffffffff);

        if(dst.secondary != ResourceId())
        {
          bytebuf counterVal;
          GetDebugManager()->GetBufferData(rm->GetResAs<ID3D12Resource>(src->GetCounterResourceId()),
                                           uav.Buffer.CounterOffsetInBytes, 4, counterVal);
          uint32_t *val = (uint32_t *)&counterVal[0];
          dst.bufferStructCount = *val;
        }
      }
      else if(uav.ViewDimension == D3D12_UAV_DIMENSION_BUFFER_BYTE_OFFSET)
      {
        dst.byteOffset = uav.BufferByteOffset.Offset;
        dst.byteSize = uav.BufferByteOffset.Size;
        dst.flags = MakeDescriptorFlags(uav.BufferByteOffset.Flags);
        if(uav.BufferByteOffset.StructureByteStride > 0)
          dst.elementByteSize = uav.BufferByteOffset.StructureByteStride;

        dst.counterByteOffset = uav.BufferByteOffset.CounterOffsetInBytes & 0xffffffff;
        RDCASSERT(uav.BufferByteOffset.CounterOffsetInBytes < 0xffffffff);

        if(dst.secondary != ResourceId())
        {
          bytebuf counterVal;
          GetDebugManager()->GetBufferData(rm->GetResAs<ID3D12Resource>(src->GetCounterResourceId()),
                                           uav.BufferByteOffset.CounterOffsetInBytes, 4, counterVal);
          uint32_t *val = (uint32_t *)&counterVal[0];
          dst.bufferStructCount = *val;
        }
      }
      else if(uav.ViewDimension == D3D12_UAV_DIMENSION_TEXTURE1D)
      {
        dst.firstMip = uav.Texture1D.MipSlice & 0xff;
      }
      else if(uav.ViewDimension == D3D12_UAV_DIMENSION_TEXTURE1DARRAY)
      {
        dst.numSlices = uav.Texture1DArray.ArraySize & 0xffff;
        dst.firstSlice = uav.Texture1DArray.FirstArraySlice & 0xffff;
        dst.firstMip = uav.Texture1DArray.MipSlice & 0xff;
      }
      else if(uav.ViewDimension == D3D12_UAV_DIMENSION_TEXTURE2D)
      {
        dst.firstMip = uav.Texture2D.MipSlice & 0xff;
      }
      else if(uav.ViewDimension == D3D12_UAV_DIMENSION_TEXTURE2DARRAY)
      {
        dst.numSlices = uav.Texture2DArray.ArraySize & 0xffff;
        dst.firstSlice = uav.Texture2DArray.FirstArraySlice & 0xffff;
        dst.firstMip = uav.Texture2DArray.MipSlice & 0xff;
      }
      else if(uav.ViewDimension == D3D12_UAV_DIMENSION_TEXTURE2DMS)
      {
      }
      else if(uav.ViewDimension == D3D12_UAV_DIMENSION_TEXTURE2DMSARRAY)
      {
        dst.numSlices = uav.Texture2DMSArray.ArraySize & 0xffff;
        dst.firstSlice = uav.Texture2DMSArray.FirstArraySlice & 0xffff;
      }
      else if(uav.ViewDimension == D3D12_UAV_DIMENSION_TEXTURE3D)
      {
        dst.numSlices = uav.Texture3D.WSize & 0xffff;
        dst.firstSlice = uav.Texture3D.FirstWSlice & 0xffff;
        dst.firstMip = uav.Texture3D.MipSlice & 0xff;
      }
    }
    else if(src->GetType() == D3D12DescriptorType::RTV)
    {
      D3D12_RENDER_TARGET_VIEW_DESC rtv = src->GetRTV();

      if(rtv.ViewDimension == D3D12_RTV_DIMENSION_BUFFER)
        dst.type = rtv.Format != DXGI_FORMAT_UNKNOWN ? DescriptorType::ReadWriteTypedBuffer
                                                     : DescriptorType::ReadWriteBuffer;
      else
        dst.type = DescriptorType::ReadWriteImage;

      fmt = rtv.Format;

      dst.secondary = src->GetCounterResourceId();

      dst.textureType = MakeTextureDim(rtv.ViewDimension);

      if(rtv.ViewDimension == D3D12_RTV_DIMENSION_BUFFER)
      {
        firstElement = rtv.Buffer.FirstElement;
        numElements = rtv.Buffer.NumElements;
      }
      else if(rtv.ViewDimension == D3D12_RTV_DIMENSION_TEXTURE1D)
      {
        dst.firstMip = rtv.Texture1D.MipSlice & 0xff;
      }
      else if(rtv.ViewDimension == D3D12_RTV_DIMENSION_TEXTURE1DARRAY)
      {
        dst.numSlices = rtv.Texture1DArray.ArraySize & 0xffff;
        dst.firstSlice = rtv.Texture1DArray.FirstArraySlice & 0xffff;
        dst.firstMip = rtv.Texture1DArray.MipSlice & 0xff;
      }
      else if(rtv.ViewDimension == D3D12_RTV_DIMENSION_TEXTURE2D)
      {
        dst.firstMip = rtv.Texture2D.MipSlice & 0xff;
      }
      else if(rtv.ViewDimension == D3D12_RTV_DIMENSION_TEXTURE2DARRAY)
      {
        dst.numSlices = rtv.Texture2DArray.ArraySize & 0xffff;
        dst.firstSlice = rtv.Texture2DArray.FirstArraySlice & 0xffff;
        dst.firstMip = rtv.Texture2DArray.MipSlice & 0xff;
      }
      else if(rtv.ViewDimension == D3D12_RTV_DIMENSION_TEXTURE2DMS)
      {
      }
      else if(rtv.ViewDimension == D3D12_RTV_DIMENSION_TEXTURE2DMSARRAY)
      {
        dst.numSlices = rtv.Texture2DMSArray.ArraySize & 0xffff;
        dst.firstSlice = rtv.Texture2DMSArray.FirstArraySlice & 0xffff;
      }
      else if(rtv.ViewDimension == D3D12_RTV_DIMENSION_TEXTURE3D)
      {
        dst.numSlices = rtv.Texture3D.WSize & 0xffff;
        dst.firstSlice = rtv.Texture3D.FirstWSlice & 0xffff;
        dst.firstMip = rtv.Texture3D.MipSlice & 0xff;
      }
    }
    else if(src->GetType() == D3D12DescriptorType::DSV)
    {
      D3D12_DEPTH_STENCIL_VIEW_DESC dsv = src->GetDSV();

      dst.type = DescriptorType::ReadWriteImage;

      fmt = dsv.Format;

      dst.secondary = src->GetCounterResourceId();

      dst.textureType = MakeTextureDim(dsv.ViewDimension);
      if(dsv.ViewDimension == D3D12_DSV_DIMENSION_TEXTURE1D)
      {
        dst.firstMip = dsv.Texture1D.MipSlice & 0xff;
      }
      else if(dsv.ViewDimension == D3D12_DSV_DIMENSION_TEXTURE1DARRAY)
      {
        dst.numSlices = dsv.Texture1DArray.ArraySize & 0xffff;
        dst.firstSlice = dsv.Texture1DArray.FirstArraySlice & 0xffff;
        dst.firstMip = dsv.Texture1DArray.MipSlice & 0xff;
      }
      else if(dsv.ViewDimension == D3D12_DSV_DIMENSION_TEXTURE2D)
      {
        dst.firstMip = dsv.Texture2D.MipSlice & 0xff;
      }
      else if(dsv.ViewDimension == D3D12_DSV_DIMENSION_TEXTURE2DARRAY)
      {
        dst.numSlices = dsv.Texture2DArray.ArraySize & 0xffff;
        dst.firstSlice = dsv.Texture2DArray.FirstArraySlice & 0xffff;
        dst.firstMip = dsv.Texture2DArray.MipSlice & 0xff;
      }
      else if(dsv.ViewDimension == D3D12_DSV_DIMENSION_TEXTURE2DMS)
      {
      }
      else if(dsv.ViewDimension == D3D12_DSV_DIMENSION_TEXTURE2DMSARRAY)
      {
        dst.numSlices = dsv.Texture2DMSArray.ArraySize & 0xffff;
        dst.firstSlice = dsv.Texture2DMSArray.FirstArraySlice & 0xffff;
      }
    }

    if(fmt == DXGI_FORMAT_UNKNOWN)
      fmt = res.Format;

    if(dst.elementByteSize == 0)
      dst.elementByteSize = fmt == DXGI_FORMAT_UNKNOWN ? 1 : GetByteSize(1, 1, 1, fmt, 0);

    // decode elements into byte offsets
    if(firstElement != UINT64_MAX)
    {
      dst.byteOffset = firstElement * dst.elementByteSize;
      dst.byteSize = numElements * dst.elementByteSize;
    }

    if(res.MipLevels == 0 && res.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
      res.MipLevels = (uint16_t)CalcNumMips(
          (uint32_t)res.Width, res.Height,
          res.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D ? res.DepthOrArraySize : 1);
    dst.numMips = RDCMIN(dst.numMips, uint8_t(res.MipLevels & 0xff));
    dst.numSlices = RDCMIN(dst.numSlices, res.DepthOrArraySize);

    dst.format = MakeResourceFormat(fmt);
  }

  if(src->GetHeap())
    src->GetHeap()->SetToDescriptorCache(src->GetHeapIndex(), dst);
}

void D3D12Replay::FillSamplerDescriptor(SamplerDescriptor &dst, const D3D12_SAMPLER_DESC2 &src)
{
  dst.type = DescriptorType::Sampler;

  dst.addressU = MakeAddressMode(src.AddressU);
  dst.addressV = MakeAddressMode(src.AddressV);
  dst.addressW = MakeAddressMode(src.AddressW);

  dst.borderColorValue.uintValue = src.UintBorderColor;
  dst.borderColorType =
      ((src.Flags & D3D12_SAMPLER_FLAG_UINT_BORDER_COLOR) != 0 ? CompType::UInt : CompType::Float);

  dst.unnormalized = (src.Flags & D3D12_SAMPLER_FLAG_NON_NORMALIZED_COORDINATES) != 0;

  dst.compareFunction = MakeCompareFunc(src.ComparisonFunc);
  dst.filter = MakeFilter(src.Filter);
  dst.maxAnisotropy = 0;
  if(dst.filter.minify == FilterMode::Anisotropic)
    dst.maxAnisotropy = (float)src.MaxAnisotropy;
  dst.maxLOD = src.MaxLOD;
  dst.minLOD = src.MinLOD;
  dst.mipBias = src.MipLODBias;
}

void D3D12Replay::FillRootDescriptor(Descriptor &dst, const D3D12RenderState::SignatureElement &src)
{
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  if(src.type == eRootCBV)
  {
    dst.type = DescriptorType::ConstantBuffer;

    ID3D12Resource *buf = rm->GetResAs<ID3D12Resource>(src.id);

    dst.resource = src.id;
    dst.byteOffset = src.offset;
    if(buf)
      dst.byteSize = uint32_t(buf->GetDesc().Width - dst.byteOffset);
    else
      dst.byteSize = 0;
  }
  else if(src.type == eRootSRV)
  {
    dst.type = DescriptorType::Buffer;

    ID3D12Resource *buf = rm->GetResAs<ID3D12Resource>(src.id);

    // parameters from resource/view
    dst.resource = src.id;
    dst.textureType = TextureType::Buffer;
    dst.format = MakeResourceFormat(DXGI_FORMAT_R32_TYPELESS);

    dst.elementByteSize = sizeof(uint32_t);
    dst.byteOffset = src.offset;
    if(buf)
      dst.byteSize = uint32_t(buf->GetDesc().Width - src.offset);
    else
      dst.byteSize = 0;
  }
  else if(src.type == eRootUAV)
  {
    dst.type = DescriptorType::ReadWriteBuffer;

    ID3D12Resource *buf = rm->GetResAs<ID3D12Resource>(src.id);

    // parameters from resource/view
    dst.resource = src.id;
    dst.textureType = TextureType::Buffer;
    dst.format = MakeResourceFormat(DXGI_FORMAT_R32_TYPELESS);

    dst.elementByteSize = sizeof(uint32_t);
    dst.byteOffset = src.offset;
    if(buf)
      dst.byteSize = uint32_t(buf->GetDesc().Width - src.offset);
    else
      dst.byteSize = 0;
  }
}

void D3D12Replay::SavePipelineState(uint32_t eventId)
{
  if(!m_D3D12PipelineState)
    return;

  const D3D12RenderState &rs = m_pDevice->GetQueue()->GetCommandData()->m_RenderState;

  D3D12MarkerRegion::Begin(m_pDevice->GetQueue(),
                           StringFormat::Fmt("FetchShaderFeedback for %u", eventId));

  if(!HasEventShaderOverride(eventId))
    FetchShaderFeedback(eventId);

  D3D12MarkerRegion::End(m_pDevice->GetQueue());

  D3D12Pipe::State &state = *m_D3D12PipelineState;

  /////////////////////////////////////////////////
  // Input Assembler
  /////////////////////////////////////////////////

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  state.pipelineResourceId = rm->GetUnreplacedID(rs.pipe);

  WrappedID3D12PipelineState *pipe = NULL;

  if(rs.pipe != ResourceId())
    pipe = rm->GetResAs<WrappedID3D12PipelineState>(rs.pipe);

  if(pipe && pipe->IsGraphics())
  {
    const D3D12_INPUT_ELEMENT_DESC *inputEl = pipe->graphics->InputLayout.pInputElementDescs;
    UINT numInput = pipe->graphics->InputLayout.NumElements;

    state.inputAssembly.layouts.resize(numInput);
    for(UINT i = 0; i < numInput; i++)
    {
      D3D12Pipe::Layout &l = state.inputAssembly.layouts[i];

      l.byteOffset = inputEl[i].AlignedByteOffset;
      l.format = MakeResourceFormat(inputEl[i].Format);
      l.inputSlot = inputEl[i].InputSlot;
      l.perInstance = inputEl[i].InputSlotClass == D3D12_INPUT_CLASSIFICATION_PER_INSTANCE_DATA;
      l.instanceDataStepRate = inputEl[i].InstanceDataStepRate;
      l.semanticIndex = inputEl[i].SemanticIndex;
      l.semanticName = inputEl[i].SemanticName;
    }

    state.inputAssembly.indexStripCutValue = 0;
    switch(pipe->graphics->IBStripCutValue)
    {
      case D3D12_INDEX_BUFFER_STRIP_CUT_VALUE_0xFFFF:
        state.inputAssembly.indexStripCutValue = 0xFFFF;
        break;
      case D3D12_INDEX_BUFFER_STRIP_CUT_VALUE_0xFFFFFFFF:
        state.inputAssembly.indexStripCutValue = 0xFFFFFFFF;
        break;
      default: break;
    }

    state.inputAssembly.vertexBuffers.resize(rs.vbuffers.size());
    for(size_t i = 0; i < rs.vbuffers.size(); i++)
    {
      D3D12Pipe::VertexBuffer &vb = state.inputAssembly.vertexBuffers[i];

      vb.resourceId = rs.vbuffers[i].buf;
      vb.byteOffset = rs.vbuffers[i].offs;
      vb.byteSize = rs.vbuffers[i].size;
      vb.byteStride = rs.vbuffers[i].stride;
    }

    state.inputAssembly.indexBuffer.resourceId = rs.ibuffer.buf;
    state.inputAssembly.indexBuffer.byteOffset = rs.ibuffer.offs;
    state.inputAssembly.indexBuffer.byteSize = rs.ibuffer.size;
    state.inputAssembly.indexBuffer.byteStride = rs.ibuffer.bytewidth;

    state.inputAssembly.topology = MakePrimitiveTopology(rs.topo);
  }

  /////////////////////////////////////////////////
  // Shaders
  /////////////////////////////////////////////////

  if(pipe && pipe->IsCompute())
  {
    WrappedID3D12Shader *sh = (WrappedID3D12Shader *)pipe->compute->CS.pShaderBytecode;

    state.computeShader.resourceId = rm->GetUnreplacedID(sh->GetResourceID());
    state.computeShader.stage = ShaderStage::Compute;
    state.computeShader.reflection = &sh->GetDetails();
  }
  else if(pipe)
  {
    D3D12Pipe::Shader *dstArr[] = {
        &state.vertexShader,
        &state.hullShader,
        &state.domainShader,
        &state.geometryShader,
        &state.pixelShader,
        // compute
        NULL,
        &state.ampShader,
        &state.meshShader,
    };

    D3D12_SHADER_BYTECODE *srcArr[] = {
        &pipe->graphics->VS,
        &pipe->graphics->HS,
        &pipe->graphics->DS,
        &pipe->graphics->GS,
        &pipe->graphics->PS,
        // compute
        NULL,
        &pipe->graphics->AS,
        &pipe->graphics->MS,
    };

    for(size_t stage = 0; stage < ARRAY_COUNT(dstArr); stage++)
    {
      if(!dstArr[stage])
        continue;

      D3D12Pipe::Shader &dst = *dstArr[stage];
      D3D12_SHADER_BYTECODE &src = *srcArr[stage];

      dst.stage = (ShaderStage)stage;

      WrappedID3D12Shader *sh = (WrappedID3D12Shader *)src.pShaderBytecode;

      if(sh)
      {
        dst.resourceId = rm->GetUnreplacedID(sh->GetResourceID());
        dst.reflection = &sh->GetDetails();
      }
      else
      {
        dst.resourceId = ResourceId();
        dst.reflection = NULL;
      }
    }
  }

  /////////////////////////////////////////////////
  // Root Signature
  /////////////////////////////////////////////////
  {
    const D3D12RenderState::RootSignature &sig =
        (pipe && pipe->IsCompute()) ? rs.compute : rs.graphics;
    const rdcarray<D3D12RenderState::SignatureElement> &rootElems = sig.sigelems;

    WrappedID3D12RootSignature *rootSig = rm->GetResAs<WrappedID3D12RootSignature>(sig.rootsig);

    state.rootSignature.resourceId = GetResID(rootSig);
    state.rootSignature.parameters.clear();
    state.rootSignature.staticSamplers.clear();

    if(rootSig)
    {
      state.rootSignature.parameters.reserve(rootSig->sig.Parameters.size());
      for(size_t i = 0; i < rootSig->sig.Parameters.size(); i++)
      {
        const D3D12RootSignatureParameter &src = rootSig->sig.Parameters[i];
        D3D12Pipe::RootParam dst;
        dst.visibility = ConvertVisibility(src.ShaderVisibility);

        switch(src.ParameterType)
        {
          case D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE:
          {
            if(i < rootElems.size())
            {
              dst.heap = rootElems[i].id;
              dst.heapByteOffset = (uint32_t)rootElems[i].offset;
            }

            UINT prevTableOffset = 0;

            dst.tableRanges.reserve(src.DescriptorTable.NumDescriptorRanges);
            for(UINT r = 0; r < src.DescriptorTable.NumDescriptorRanges; r++)
            {
              const D3D12_DESCRIPTOR_RANGE1 &srcRange = src.DescriptorTable.pDescriptorRanges[r];

              D3D12Pipe::RootTableRange range;

              switch(srcRange.RangeType)
              {
                case D3D12_DESCRIPTOR_RANGE_TYPE_CBV:
                  range.category = DescriptorCategory::ConstantBlock;
                  break;
                case D3D12_DESCRIPTOR_RANGE_TYPE_SAMPLER:
                  range.category = DescriptorCategory::Sampler;
                  break;
                case D3D12_DESCRIPTOR_RANGE_TYPE_SRV:
                  range.category = DescriptorCategory::ReadOnlyResource;
                  break;
                case D3D12_DESCRIPTOR_RANGE_TYPE_UAV:
                  range.category = DescriptorCategory::ReadWriteResource;
                  break;
              }

              UINT offset = srcRange.OffsetInDescriptorsFromTableStart;

              if(srcRange.OffsetInDescriptorsFromTableStart == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND)
              {
                range.appended = true;
                offset = prevTableOffset;
              }

              range.space = srcRange.RegisterSpace;
              range.baseRegister = srcRange.BaseShaderRegister;
              range.count = srcRange.NumDescriptors;
              range.tableByteOffset = offset;

              prevTableOffset = offset + srcRange.NumDescriptors;

              dst.tableRanges.push_back(range);
            }

            break;
          }
          case D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS:
          {
            dst.constants.resize(src.Constants.Num32BitValues * 4);
            dst.space = src.Constants.RegisterSpace;
            dst.reg = src.Constants.ShaderRegister;

            if(i < rootElems.size())
            {
              memcpy(dst.constants.data(), rootElems[i].constants.data(),
                     RDCMIN(rootElems[i].constants.byteSize(), dst.constants.byteSize()));
            }

            break;
          }
          case D3D12_ROOT_PARAMETER_TYPE_CBV:
          {
            dst.descriptor.type = DescriptorType::ConstantBuffer;
            dst.space = src.Descriptor.RegisterSpace;
            dst.reg = src.Descriptor.ShaderRegister;

            if(i < rootElems.size())
              FillRootDescriptor(dst.descriptor, rootElems[i]);
            break;
          }
          case D3D12_ROOT_PARAMETER_TYPE_SRV:
          {
            dst.descriptor.type = DescriptorType::Buffer;
            dst.space = src.Descriptor.RegisterSpace;
            dst.reg = src.Descriptor.ShaderRegister;

            if(i < rootElems.size())
              FillRootDescriptor(dst.descriptor, rootElems[i]);
            break;
          }
          case D3D12_ROOT_PARAMETER_TYPE_UAV:
          {
            dst.descriptor.type = DescriptorType::ReadWriteBuffer;
            dst.space = src.Descriptor.RegisterSpace;
            dst.reg = src.Descriptor.ShaderRegister;

            if(i < rootElems.size())
              FillRootDescriptor(dst.descriptor, rootElems[i]);
            break;
          }
        }

        state.rootSignature.parameters.push_back(std::move(dst));
      }

      state.rootSignature.staticSamplers.reserve(rootSig->sig.StaticSamplers.size());
      for(const D3D12_STATIC_SAMPLER_DESC1 &src : rootSig->sig.StaticSamplers)
      {
        D3D12Pipe::StaticSampler dst;
        dst.visibility = ConvertVisibility(src.ShaderVisibility);

        dst.space = src.RegisterSpace;
        dst.reg = src.ShaderRegister;

        FillSamplerDescriptor(dst.descriptor, ConvertStaticSampler(src));

        state.rootSignature.staticSamplers.push_back(std::move(dst));
      }
    }
  }

  state.descriptorHeaps.clear();
  for(ResourceId id : rs.heaps)
    state.descriptorHeaps.push_back(id);

  if(pipe && pipe->IsGraphics())
  {
    /////////////////////////////////////////////////
    // Stream Out
    /////////////////////////////////////////////////

    state.streamOut.rasterizedStream = pipe->graphics->StreamOutput.RasterizedStream;

    state.streamOut.outputs.resize(rs.streamouts.size());
    for(size_t s = 0; s < rs.streamouts.size(); s++)
    {
      state.streamOut.outputs[s].resourceId = rs.streamouts[s].buf;
      state.streamOut.outputs[s].byteOffset = rs.streamouts[s].offs;
      state.streamOut.outputs[s].byteSize = rs.streamouts[s].size;

      state.streamOut.outputs[s].writtenCountResourceId = rs.streamouts[s].countbuf;
      state.streamOut.outputs[s].writtenCountByteOffset = rs.streamouts[s].countoffs;
    }

    /////////////////////////////////////////////////
    // Rasterizer
    /////////////////////////////////////////////////

    state.rasterizer.sampleMask = pipe->graphics->SampleMask;

    {
      D3D12Pipe::RasterizerState &dst = state.rasterizer.state;
      D3D12_RASTERIZER_DESC2 &src = pipe->graphics->RasterizerState;

      switch(src.LineRasterizationMode)
      {
        case D3D12_LINE_RASTERIZATION_MODE_ALIASED:
          dst.lineRasterMode = LineRaster::Bresenham;
          break;
        case D3D12_LINE_RASTERIZATION_MODE_ALPHA_ANTIALIASED:
          dst.lineRasterMode = LineRaster::RectangularSmooth;
          break;
        case D3D12_LINE_RASTERIZATION_MODE_QUADRILATERAL_WIDE:
          dst.lineRasterMode = LineRaster::RectangularD3D;
          break;
        case D3D12_LINE_RASTERIZATION_MODE_QUADRILATERAL_NARROW:
          dst.lineRasterMode = LineRaster::Rectangular;
          break;
        default: dst.lineRasterMode = LineRaster::Default; break;
      }

      dst.cullMode = CullMode::NoCull;
      if(src.CullMode == D3D12_CULL_MODE_FRONT)
        dst.cullMode = CullMode::Front;
      if(src.CullMode == D3D12_CULL_MODE_BACK)
        dst.cullMode = CullMode::Back;

      dst.fillMode = FillMode::Solid;
      if(src.FillMode == D3D12_FILL_MODE_WIREFRAME)
        dst.fillMode = FillMode::Wireframe;

      dst.depthBias = src.DepthBias;
      dst.depthBiasClamp = src.DepthBiasClamp;
      dst.depthClip = src.DepthClipEnable == TRUE;
      dst.frontCCW = src.FrontCounterClockwise == TRUE;
      dst.slopeScaledDepthBias = src.SlopeScaledDepthBias;
      dst.forcedSampleCount = src.ForcedSampleCount;

      // D3D only supports overestimate conservative raster (underestimated can be emulated using
      // coverage information in the shader)
      dst.conservativeRasterization =
          src.ConservativeRaster == D3D12_CONSERVATIVE_RASTERIZATION_MODE_ON
              ? ConservativeRaster::Overestimate
              : ConservativeRaster::Disabled;

      switch(rs.shadingRate)
      {
        default:
        case D3D12_SHADING_RATE_1X1: dst.baseShadingRate = {1, 1}; break;
        case D3D12_SHADING_RATE_1X2: dst.baseShadingRate = {1, 2}; break;
        case D3D12_SHADING_RATE_2X1: dst.baseShadingRate = {2, 1}; break;
        case D3D12_SHADING_RATE_2X2: dst.baseShadingRate = {2, 2}; break;
        case D3D12_SHADING_RATE_2X4: dst.baseShadingRate = {2, 4}; break;
        case D3D12_SHADING_RATE_4X2: dst.baseShadingRate = {4, 2}; break;
        case D3D12_SHADING_RATE_4X4: dst.baseShadingRate = {4, 4}; break;
      }

      ShadingRateCombiner combiners[2];

      for(int i = 0; i < 2; i++)
      {
        switch(rs.shadingRateCombiners[i])
        {
          default:
          case D3D12_SHADING_RATE_COMBINER_PASSTHROUGH:
            combiners[i] = ShadingRateCombiner::Passthrough;
            break;
          case D3D12_SHADING_RATE_COMBINER_OVERRIDE:
            combiners[i] = ShadingRateCombiner::Override;
            break;
          case D3D12_SHADING_RATE_COMBINER_MIN: combiners[i] = ShadingRateCombiner::Min; break;
          case D3D12_SHADING_RATE_COMBINER_MAX: combiners[i] = ShadingRateCombiner::Max; break;
          case D3D12_SHADING_RATE_COMBINER_SUM: combiners[i] = ShadingRateCombiner::Multiply; break;
        }
      }

      dst.shadingRateCombiners = {combiners[0], combiners[1]};

      dst.shadingRateImage = rs.shadingRateImage;
    }

    state.rasterizer.scissors.resize(rs.scissors.size());
    for(size_t i = 0; i < rs.scissors.size(); i++)
      state.rasterizer.scissors[i] = Scissor(rs.scissors[i].left, rs.scissors[i].top,
                                             rs.scissors[i].right - rs.scissors[i].left,
                                             rs.scissors[i].bottom - rs.scissors[i].top);

    state.rasterizer.viewports.resize(rs.views.size());
    for(size_t i = 0; i < rs.views.size(); i++)
      state.rasterizer.viewports[i] =
          Viewport(rs.views[i].TopLeftX, rs.views[i].TopLeftY, rs.views[i].Width,
                   rs.views[i].Height, rs.views[i].MinDepth, rs.views[i].MaxDepth);

    /////////////////////////////////////////////////
    // Output Merger
    /////////////////////////////////////////////////

    state.outputMerger.renderTargets.reserve(rs.rts.size());
    state.outputMerger.renderTargets.clear();
    for(size_t i = 0; i < rs.rts.size(); i++)
    {
      const D3D12Descriptor &desc = rs.rts[i];

      state.outputMerger.renderTargets.push_back(Descriptor());
      if(desc.GetResResourceId() != ResourceId())
      {
        FillDescriptor(state.outputMerger.renderTargets.back(), &desc);
      }
    }

    if(rs.dsv.GetResResourceId() != ResourceId())
    {
      FillDescriptor(state.outputMerger.depthTarget, &rs.dsv);

      state.outputMerger.depthReadOnly = false;
      state.outputMerger.stencilReadOnly = false;

      if(rs.dsv.GetDSV().Flags & D3D12_DSV_FLAG_READ_ONLY_DEPTH)
        state.outputMerger.depthReadOnly = true;
      if(rs.dsv.GetDSV().Flags & D3D12_DSV_FLAG_READ_ONLY_STENCIL)
        state.outputMerger.stencilReadOnly = true;
    }
    else
    {
      state.outputMerger.depthTarget = Descriptor();

      state.outputMerger.depthReadOnly = false;
      state.outputMerger.stencilReadOnly = false;
    }

    state.outputMerger.blendState.blendFactor = rs.blendFactor;

    {
      D3D12_BLEND_DESC &src = pipe->graphics->BlendState;

      state.outputMerger.blendState.alphaToCoverage = src.AlphaToCoverageEnable == TRUE;
      state.outputMerger.blendState.independentBlend = src.IndependentBlendEnable == TRUE;

      state.outputMerger.blendState.blends.resize(8);
      for(size_t i = 0; i < 8; i++)
      {
        ColorBlend &blend = state.outputMerger.blendState.blends[i];

        blend.enabled = src.RenderTarget[i].BlendEnable == TRUE;

        blend.logicOperationEnabled = src.RenderTarget[i].LogicOpEnable == TRUE;
        blend.logicOperation = MakeLogicOp(src.RenderTarget[i].LogicOp);

        blend.alphaBlend.source = MakeBlendMultiplier(src.RenderTarget[i].SrcBlendAlpha, true);
        blend.alphaBlend.destination = MakeBlendMultiplier(src.RenderTarget[i].DestBlendAlpha, true);
        blend.alphaBlend.operation = MakeBlendOp(src.RenderTarget[i].BlendOpAlpha);

        blend.colorBlend.source = MakeBlendMultiplier(src.RenderTarget[i].SrcBlend, false);
        blend.colorBlend.destination = MakeBlendMultiplier(src.RenderTarget[i].DestBlend, false);
        blend.colorBlend.operation = MakeBlendOp(src.RenderTarget[i].BlendOp);

        blend.writeMask = src.RenderTarget[i].RenderTargetWriteMask;
      }
    }

    {
      D3D12_DEPTH_STENCIL_DESC2 &src = pipe->graphics->DepthStencilState;

      state.outputMerger.depthStencilState.depthEnable = src.DepthEnable == TRUE;
      state.outputMerger.depthStencilState.depthFunction = MakeCompareFunc(src.DepthFunc);
      state.outputMerger.depthStencilState.depthWrites =
          src.DepthWriteMask == D3D12_DEPTH_WRITE_MASK_ALL;
      state.outputMerger.depthStencilState.stencilEnable = src.StencilEnable == TRUE;
      state.outputMerger.depthStencilState.depthBoundsEnable = src.DepthBoundsTestEnable == TRUE;

      state.outputMerger.depthStencilState.minDepthBounds = rs.depthBoundsMin;
      state.outputMerger.depthStencilState.maxDepthBounds = rs.depthBoundsMax;

      state.outputMerger.depthStencilState.frontFace.function =
          MakeCompareFunc(src.FrontFace.StencilFunc);
      state.outputMerger.depthStencilState.frontFace.depthFailOperation =
          MakeStencilOp(src.FrontFace.StencilDepthFailOp);
      state.outputMerger.depthStencilState.frontFace.passOperation =
          MakeStencilOp(src.FrontFace.StencilPassOp);
      state.outputMerger.depthStencilState.frontFace.failOperation =
          MakeStencilOp(src.FrontFace.StencilFailOp);
      state.outputMerger.depthStencilState.frontFace.reference = rs.stencilRefFront;
      state.outputMerger.depthStencilState.frontFace.compareMask = src.FrontFace.StencilReadMask;
      state.outputMerger.depthStencilState.frontFace.writeMask = src.FrontFace.StencilWriteMask;

      state.outputMerger.depthStencilState.backFace.function =
          MakeCompareFunc(src.BackFace.StencilFunc);
      state.outputMerger.depthStencilState.backFace.depthFailOperation =
          MakeStencilOp(src.BackFace.StencilDepthFailOp);
      state.outputMerger.depthStencilState.backFace.passOperation =
          MakeStencilOp(src.BackFace.StencilPassOp);
      state.outputMerger.depthStencilState.backFace.failOperation =
          MakeStencilOp(src.BackFace.StencilFailOp);
      state.outputMerger.depthStencilState.backFace.reference = rs.stencilRefBack;
      state.outputMerger.depthStencilState.backFace.compareMask = src.BackFace.StencilReadMask;
      state.outputMerger.depthStencilState.backFace.writeMask = src.BackFace.StencilWriteMask;
    }
  }

  /////////////////////////////////////////////////
  // Predication
  /////////////////////////////////////////////////

  state.predication.resourceId = rs.predication.buffer;
  state.predication.offset = rs.predication.offset;
  state.predication.skipIfZero = rs.predication.op == D3D12_PREDICATION_OP_EQUAL_ZERO;

  // resource states
  {
    const std::map<ResourceId, SubresourceStateVector> &states = m_pDevice->GetSubresourceStates();
    state.resourceStates.resize(states.size());
    size_t i = 0;
    for(auto it = states.begin(); it != states.end(); ++it)
    {
      D3D12Pipe::ResourceData &res = state.resourceStates[i];

      res.resourceId = it->first;

      res.states.resize(it->second.size());
      for(size_t l = 0; l < it->second.size(); l++)
        res.states[l].name = ToStr(it->second[l]);

      if(res.states.empty())
        res.states.push_back({"Unknown"});

      i++;
    }
  }
}

rdcarray<Descriptor> D3D12Replay::GetDescriptors(ResourceId descriptorStore,
                                                 const rdcarray<DescriptorRange> &ranges)
{
  if(descriptorStore == ResourceId())
    return {};

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  rdcarray<Descriptor> ret;
  ret.resize(count);

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  ID3D12DeviceChild *res = rm->GetResAs<ID3D12DeviceChild>(descriptorStore);

  if(WrappedID3D12RootSignature::IsAlloc(res))
  {
    // root signature descriptor storage is for static samplers
    for(Descriptor &d : ret)
      d.type = DescriptorType::Sampler;
    return ret;
  }

  if(WrappedID3D12PipelineState::IsAlloc(res))
  {
    const D3D12RenderState &rs = m_pDevice->GetQueue()->GetCommandData()->m_RenderState;

    WrappedID3D12PipelineState *pipe = (WrappedID3D12PipelineState *)res;

    // root constants
    size_t dst = 0;
    for(const DescriptorRange &r : ranges)
    {
      uint32_t rootIndex = r.offset;

      for(uint32_t i = 0; i < r.count; i++, rootIndex++, dst++)
      {
        const rdcarray<D3D12RenderState::SignatureElement> &rootElems =
            pipe->IsGraphics() ? rs.graphics.sigelems : rs.compute.sigelems;

        // either the application didn't set some root elements properly, or we're at an event where
        // the bindings aren't valid
        if(rootIndex >= rootElems.size())
          continue;

        const D3D12RenderState::SignatureElement &rootEl = rootElems[rootIndex];

        Descriptor &d = ret[dst];

        if(rootEl.type == eRootConst)
        {
          d.type = DescriptorType::ConstantBuffer;
          d.flags = DescriptorFlags::InlineData;
          d.view = ResourceId();
          // we pretend that the pipeline has all root constants appended together as its blob of
          // data, so calculate local 'offset' into the root constants
          d.resource = descriptorStore;

          d.byteOffset = 0;
          for(uint32_t root = 0; root < rootIndex; root++)
            if(rootElems[root].type == eRootConst)
              d.byteOffset += rootElems[root].constants.byteSize();
          d.byteSize = rootEl.constants.byteSize();
        }
        else
        {
          FillRootDescriptor(d, rootEl);
        }
      }
    }

    return ret;
  }

  if(!WrappedID3D12DescriptorHeap::IsAlloc(res))
  {
    RDCERR("Invalid/unrecognised descriptor store %s", ToStr(descriptorStore).c_str());
    return ret;
  }

  WrappedID3D12DescriptorHeap *heap = (WrappedID3D12DescriptorHeap *)res;

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    D3D12Descriptor *desc = (D3D12Descriptor *)heap->GetCPUDescriptorHandleForHeapStart().ptr;
    D3D12Descriptor *end = desc + heap->GetNumDescriptors();

    desc += r.offset;

    for(uint32_t i = 0; i < r.count; i++)
    {
      if(desc >= end)
      {
        // silently drop out of bounds descriptor reads
      }
      else if(desc->GetType() == D3D12DescriptorType::CBV)
      {
        const D3D12_CONSTANT_BUFFER_VIEW_DESC &cbv = desc->GetCBV();
        WrappedID3D12Resource::GetResIDFromAddr(cbv.BufferLocation, ret[dst].resource,
                                                ret[dst].byteOffset);

        ret[dst].type = DescriptorType::ConstantBuffer;
        ret[dst].byteSize = cbv.SizeInBytes;
      }
      else if(desc->GetType() == D3D12DescriptorType::Sampler)
      {
        ret[dst].type = DescriptorType::Sampler;
      }
      else
      {
        FillDescriptor(ret[dst], desc);
      }
      dst++;
      desc++;
    }
  }

  return ret;
}

rdcarray<SamplerDescriptor> D3D12Replay::GetSamplerDescriptors(ResourceId descriptorStore,
                                                               const rdcarray<DescriptorRange> &ranges)
{
  if(descriptorStore == ResourceId())
    return {};

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  rdcarray<SamplerDescriptor> ret;
  ret.resize(count);

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  ID3D12DeviceChild *res = rm->GetResAs<ID3D12DeviceChild>(descriptorStore);

  if(WrappedID3D12RootSignature::IsAlloc(res))
  {
    WrappedID3D12RootSignature *sig = (WrappedID3D12RootSignature *)res;
    size_t dst = 0;
    for(const DescriptorRange &r : ranges)
    {
      uint32_t staticIdx = r.offset;

      for(uint32_t i = 0; i < r.count; i++)
      {
        if(staticIdx >= sig->sig.StaticSamplers.size())
        {
          // silently drop out of bounds descriptor reads
        }
        else
        {
          FillSamplerDescriptor(ret[dst], ConvertStaticSampler(sig->sig.StaticSamplers[staticIdx]));
          ret[dst].creationTimeConstant = true;
        }
        dst++;
        staticIdx++;
      }
    }
    return ret;
  }

  if(WrappedID3D12PipelineState::IsAlloc(res))
  {
    // root constants, not sampler data
    return ret;
  }

  if(!WrappedID3D12DescriptorHeap::IsAlloc(res))
  {
    RDCERR("Invalid/unrecognised descriptor store %s", ToStr(descriptorStore).c_str());
    return ret;
  }

  WrappedID3D12DescriptorHeap *heap = (WrappedID3D12DescriptorHeap *)res;

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    D3D12Descriptor *desc = (D3D12Descriptor *)heap->GetCPUDescriptorHandleForHeapStart().ptr;
    D3D12Descriptor *end = desc + heap->GetNumDescriptors();

    desc += r.offset;

    for(uint32_t i = 0; i < r.count; i++)
    {
      if(desc >= end)
      {
        // silently drop out of bounds descriptor reads
      }
      else if(desc->GetType() == D3D12DescriptorType::Sampler)
      {
        const D3D12_SAMPLER_DESC2 &sampDesc = desc->GetSampler();
        FillSamplerDescriptor(ret[dst], sampDesc);
      }
      dst++;
      desc++;
    }
  }

  return ret;
}

rdcarray<DescriptorAccess> D3D12Replay::GetDescriptorAccess(uint32_t eventId)
{
  const D3D12RenderState &rs = m_pDevice->GetQueue()->GetCommandData()->m_RenderState;

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  WrappedID3D12PipelineState *pipe = NULL;

  if(rs.pipe != ResourceId())
    pipe = rm->GetResAs<WrappedID3D12PipelineState>(rs.pipe);

  rdcarray<DescriptorAccess> ret;

  if(pipe)
  {
    pipe->ProcessDescriptorAccess();

    ret = pipe->staticDescriptorAccess;

    const D3D12DynamicShaderFeedback &usage = m_BindlessFeedback.Usage[eventId];

    if(usage.valid)
      ret.append(usage.access);

    WrappedID3D12DescriptorHeap *resourceHeap = NULL;
    WrappedID3D12DescriptorHeap *samplerHeap = NULL;
    for(ResourceId id : rs.heaps)
    {
      WrappedID3D12DescriptorHeap *heap =
          (WrappedID3D12DescriptorHeap *)rm->GetResAs<ID3D12DescriptorHeap>(id);
      D3D12_DESCRIPTOR_HEAP_DESC desc = heap->GetDesc();
      if(desc.Type == D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV)
        resourceHeap = heap;
      else
        samplerHeap = heap;
    }

    for(DescriptorAccess &access : ret)
    {
      if(access.type == DescriptorType::Sampler)
        access.descriptorStore = samplerHeap ? samplerHeap->GetResourceID() : ResourceId();
      else
        access.descriptorStore = resourceHeap ? resourceHeap->GetResourceID() : ResourceId();

      // for direct heap access, don't do anything more
      if(access.index == DescriptorAccess::NoShaderBinding)
        continue;

      const D3D12RenderState::RootSignature &rootSig = pipe->IsGraphics() ? rs.graphics : rs.compute;

      uint32_t rootIndex = (uint32_t)access.byteSize;
      access.byteSize = 1;

      // access off the end of the root signature list indicates a static sampler. We virtualise
      // this as root signature descriptor storage
      if(access.type == DescriptorType::Sampler && rootIndex >= rootSig.sigelems.size())
      {
        access.descriptorStore = rootSig.rootsig;
        // the access byteOffset is the index of the static sampler
        continue;
      }

      // otherwise the access may be to a rootsig element that's not bound if the current event is
      // not a valid draw or dispatch
      if(rootIndex >= rootSig.sigelems.size())
      {
        access.descriptorStore = ResourceId();
        continue;
      }

      const D3D12RenderState::SignatureElement &rootEl = rootSig.sigelems[rootIndex];

      // this indicates a root parameter
      if(access.byteOffset == ~0U)
      {
        // root constants and descriptors specify the pipeline as the descriptor storage. The choice here is
        // somewhat arbitrary (we could use the command buffer, or the root signature), we just need
        // to be able to distinguish it in GetDescriptors and GetBufferData. Since we don't have
        // other types of virtual constants to handle we can use the pipeline state directly
        access.descriptorStore = pipe->GetResourceID();
        access.byteOffset = rootIndex;
      }
      else
      {
        // apply the per-parameter offset into the heap here
        access.byteOffset += (uint32_t)rootEl.offset;
      }
    }

    // remove any invalid / unbound root element accesses
    ret.removeIf(
        [](const DescriptorAccess &access) { return access.descriptorStore == ResourceId(); });
  }

  return ret;
}

rdcarray<DescriptorLogicalLocation> D3D12Replay::GetDescriptorLocations(
    ResourceId descriptorStore, const rdcarray<DescriptorRange> &ranges)
{
  rdcarray<DescriptorLogicalLocation> ret;

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  ID3D12DeviceChild *res = rm->GetResAs<ID3D12DeviceChild>(descriptorStore);

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  ret.resize(count);

  // for sort keys we have the top 32-bits be the lower 32-bits of the store ResourceID, or 1 or 2
  // for static samplers and root constants. Then the lower 32-bits are an index

  if(WrappedID3D12RootSignature::IsAlloc(res))
  {
    WrappedID3D12RootSignature *sig = (WrappedID3D12RootSignature *)res;
    size_t dst = 0;
    for(const DescriptorRange &r : ranges)
    {
      uint32_t staticIdx = r.offset;

      for(uint32_t i = 0; i < r.count; i++)
      {
        if(staticIdx >= sig->sig.StaticSamplers.size())
        {
          // silently drop out of bounds descriptor reads
        }
        else
        {
          ret[dst].fixedBindNumber = ~0U - 2048 + staticIdx;
          ret[dst].stageMask = ConvertVisibility(sig->sig.StaticSamplers[staticIdx].ShaderVisibility);
          ret[dst].category = DescriptorCategory::Sampler;
          ret[dst].logicalBindName = StringFormat::Fmt("Static #%u", staticIdx);
        }

        dst++;
        staticIdx++;
      }
    }
    return ret;
  }

  if(WrappedID3D12PipelineState::IsAlloc(res))
  {
    WrappedID3D12PipelineState *pipe = (WrappedID3D12PipelineState *)res;

    // root constants
    size_t dst = 0;
    for(const DescriptorRange &r : ranges)
    {
      uint32_t rootIndex = r.offset;

      for(uint32_t i = 0; i < r.count; i++, rootIndex++, dst++)
      {
        const D3D12RootSignatureParameter &param = pipe->usedSig.Parameters[rootIndex];

        DescriptorLogicalLocation &l = ret[dst];

        l.fixedBindNumber = ~0U - 2048 - 64 + rootIndex;
        l.stageMask = ConvertVisibility(param.ShaderVisibility);

        if(param.ParameterType == D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS)
        {
          l.category = DescriptorCategory::ConstantBlock;
          l.logicalBindName = StringFormat::Fmt("Consts #", rootIndex);
        }
        else if(param.ParameterType == D3D12_ROOT_PARAMETER_TYPE_CBV)
        {
          l.category = DescriptorCategory::ConstantBlock;
          l.logicalBindName = StringFormat::Fmt("Root CB #", rootIndex);
        }
        else if(param.ParameterType == D3D12_ROOT_PARAMETER_TYPE_SRV)
        {
          l.category = DescriptorCategory::ReadOnlyResource;
          l.logicalBindName = StringFormat::Fmt("Root SRV #", rootIndex);
        }
        else if(param.ParameterType == D3D12_ROOT_PARAMETER_TYPE_UAV)
        {
          l.category = DescriptorCategory::ReadWriteResource;
          l.logicalBindName = StringFormat::Fmt("Root UAV #", rootIndex);
        }
      }
    }

    return ret;
  }

  if(!WrappedID3D12DescriptorHeap::IsAlloc(res))
  {
    RDCERR("Invalid/unrecognised descriptor store %s", ToStr(descriptorStore).c_str());
    return ret;
  }

  WrappedID3D12DescriptorHeap *heap = (WrappedID3D12DescriptorHeap *)res;
  const bool sampler = (heap->GetDesc().Type == D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER);

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    uint32_t descriptorId = r.offset;

    for(uint32_t i = 0; i < r.count; i++, dst++, descriptorId++)
    {
      // can't set anything except the "bind number" which we just set as the offset.
      ret[dst].fixedBindNumber = descriptorId;
      if(heap->HasNames())
      {
        rdcstr name = heap->GetNames()[descriptorId];
        if(!name.empty())
        {
          ret[dst].logicalBindName = StringFormat::Fmt("%s[%u]", name.c_str(), descriptorId);
          continue;
        }
      }

      if(sampler)
        ret[dst].logicalBindName = StringFormat::Fmt("SamplerDescriptorHeap[%u]", descriptorId);
      else
        ret[dst].logicalBindName = StringFormat::Fmt("ResourceDescriptorHeap[%u]", descriptorId);
    }
  }

  return ret;
}

void D3D12Replay::RenderHighlightBox(float w, float h, float scale)
{
  OutputWindow &outw = m_OutputWindows[m_CurrentOutputWindow];

  {
    ID3D12GraphicsCommandList *list = m_pDevice->GetNewList();
    if(!list)
      return;

    float black[] = {0.0f, 0.0f, 0.0f, 1.0f};
    float white[] = {1.0f, 1.0f, 1.0f, 1.0f};

    // size of box
    LONG sz = LONG(scale);

    // top left, x and y
    LONG tlx = LONG(w / 2.0f + 0.5f);
    LONG tly = LONG(h / 2.0f + 0.5f);

    D3D12_RECT rect[4] = {
        {tlx, tly, tlx + 1, tly + sz},

        {tlx + sz, tly, tlx + sz + 1, tly + sz + 1},

        {tlx, tly, tlx + sz, tly + 1},

        {tlx, tly + sz, tlx + sz, tly + sz + 1},
    };

    // inner
    list->ClearRenderTargetView(outw.rtv, white, 4, rect);

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

    // outer
    list->ClearRenderTargetView(outw.rtv, black, 4, rect);

    list->Close();

    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists();
  }
}

void D3D12Replay::RenderCheckerboard(FloatVector dark, FloatVector light)
{
  CheckerboardCBuffer pixelData = {};

  pixelData.PrimaryColor = ConvertSRGBToLinear(dark);
  pixelData.SecondaryColor = ConvertSRGBToLinear(light);
  pixelData.CheckerSquareDimension = 64.0f;

  D3D12_GPU_VIRTUAL_ADDRESS ps = GetDebugManager()->UploadConstants(&pixelData, sizeof(pixelData));

  OutputWindow &outw = m_OutputWindows[m_CurrentOutputWindow];

  {
    ID3D12GraphicsCommandList *list = m_pDevice->GetNewList();
    if(!list)
      return;

    list->OMSetRenderTargets(1, &outw.rtv, TRUE, NULL);

    D3D12_VIEWPORT viewport = {0, 0, (float)outw.width, (float)outw.height, 0.0f, 1.0f};
    list->RSSetViewports(1, &viewport);

    D3D12_RECT scissor = {0, 0, outw.width, outw.height};
    list->RSSetScissorRects(1, &scissor);

    list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);

    list->SetPipelineState(outw.colResolve ? m_General.CheckerboardMSAAPipe
                                           : m_General.CheckerboardPipe);

    list->SetGraphicsRootSignature(m_General.CheckerboardRootSig);

    list->SetGraphicsRootConstantBufferView(0, ps);

    float factor[4] = {1.0f, 1.0f, 1.0f, 1.0f};
    list->OMSetBlendFactor(factor);

    list->DrawInstanced(3, 1, 0, 0);

    list->Close();

    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists();
  }
}

uint32_t D3D12Replay::PickVertex(uint32_t eventId, int32_t width, int32_t height,
                                 const MeshDisplay &cfg, uint32_t x, uint32_t y)
{
  if(cfg.position.numIndices == 0)
    return ~0U;

  MeshPickData cbuf = {};

  cbuf.PickCoords = Vec2f((float)x, (float)y);
  cbuf.PickViewport = Vec2f((float)width, (float)height);
  cbuf.PickIdx = cfg.position.indexByteStride && cfg.position.indexResourceId != ResourceId() ? 1 : 0;
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

  ID3D12Resource *vb = NULL, *ib = NULL;

  if(cfg.position.vertexResourceId != ResourceId())
    vb = m_pDevice->GetResourceManager()->GetResAs<ID3D12Resource>(cfg.position.vertexResourceId);

  if(cfg.position.indexResourceId != ResourceId())
    ib = m_pDevice->GetResourceManager()->GetResAs<ID3D12Resource>(cfg.position.indexResourceId);

  HRESULT hr = S_OK;

  // most IB/VBs will not be available as SRVs. So, we copy into our own buffers. In the case of VB
  // we also tightly pack and unpack the data. IB is upcast to R32 so it we can apply baseVertex
  // without risking overflow.

  uint32_t minIndex = 0;
  uint32_t maxIndex = cfg.position.numIndices;

  uint32_t idxclamp = 0;
  if(cfg.position.baseVertex < 0)
    idxclamp = uint32_t(-cfg.position.baseVertex);

  D3D12_SHADER_RESOURCE_VIEW_DESC sdesc = {};
  sdesc.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
  sdesc.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
  sdesc.Format = DXGI_FORMAT_R32_UINT;

  if(cfg.position.indexByteStride && ib)
  {
    // resize up on demand
    if(m_VertexPick.IB == NULL || m_VertexPick.IBSize < cfg.position.numIndices * sizeof(uint32_t))
    {
      SAFE_RELEASE(m_VertexPick.IB);

      m_VertexPick.IBSize = cfg.position.numIndices * sizeof(uint32_t);

      D3D12_HEAP_PROPERTIES heapProps;
      heapProps.Type = D3D12_HEAP_TYPE_UPLOAD;
      heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
      heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
      heapProps.CreationNodeMask = 1;
      heapProps.VisibleNodeMask = 1;

      D3D12_RESOURCE_DESC ibDesc;
      ibDesc.Alignment = 0;
      ibDesc.DepthOrArraySize = 1;
      ibDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
      ibDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
      ibDesc.Format = DXGI_FORMAT_UNKNOWN;
      ibDesc.Height = 1;
      ibDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
      ibDesc.MipLevels = 1;
      ibDesc.SampleDesc.Count = 1;
      ibDesc.SampleDesc.Quality = 0;
      ibDesc.Width = m_VertexPick.IBSize;

      hr = m_pDevice->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &ibDesc,
                                              D3D12_RESOURCE_STATE_GENERIC_READ, NULL,
                                              __uuidof(ID3D12Resource), (void **)&m_VertexPick.IB);

      if(FAILED(hr))
      {
        RDCERR("Couldn't create pick index buffer: HRESULT: %s", ToStr(hr).c_str());
        return ~0U;
      }

      m_VertexPick.IB->SetName(L"m_PickIB");

      sdesc.Buffer.FirstElement = 0;
      sdesc.Buffer.NumElements = cfg.position.numIndices;
      m_pDevice->CreateShaderResourceView(m_VertexPick.IB, &sdesc,
                                          GetDebugManager()->GetCPUHandle(PICK_IB_SRV));
    }

    RDCASSERT(cfg.position.indexByteOffset < 0xffffffff);

    if(m_VertexPick.IB)
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

      GetDebugManager()->FillBuffer(m_VertexPick.IB, 0, outidxs.data(),
                                    sizeof(uint32_t) * outidxs.size());
    }
  }
  else
  {
    if(cfg.position.indexByteStride)
    {
      maxIndex = 0;
      if(cfg.position.baseVertex > 0)
        minIndex = maxIndex = (uint32_t)cfg.position.baseVertex;
    }

    sdesc.Buffer.NumElements = 4;
    m_pDevice->CreateShaderResourceView(NULL, &sdesc, GetDebugManager()->GetCPUHandle(PICK_IB_SRV));
  }

  sdesc.Buffer.FirstElement = 0;
  sdesc.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;

  // unpack and linearise the data
  {
    bytebuf oldData;
    GetDebugManager()->GetBufferData(vb, cfg.position.vertexByteOffset, 0, oldData);

    // clamp maxIndex to upper bound in case we got invalid indices or primitive restart indices
    maxIndex = RDCMIN(maxIndex, uint32_t(oldData.size() / RDCMAX(1U, cfg.position.vertexByteStride)));

    if(vb)
    {
      if(m_VertexPick.VB == NULL || m_VertexPick.VBSize < (maxIndex + 1) * sizeof(Vec4f))
      {
        SAFE_RELEASE(m_VertexPick.VB);

        m_VertexPick.VBSize = (maxIndex + 1) * sizeof(Vec4f);

        D3D12_HEAP_PROPERTIES heapProps;
        heapProps.Type = D3D12_HEAP_TYPE_UPLOAD;
        heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
        heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
        heapProps.CreationNodeMask = 1;
        heapProps.VisibleNodeMask = 1;

        D3D12_RESOURCE_DESC vbDesc;
        vbDesc.Alignment = 0;
        vbDesc.DepthOrArraySize = 1;
        vbDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        vbDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
        vbDesc.Format = DXGI_FORMAT_UNKNOWN;
        vbDesc.Height = 1;
        vbDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        vbDesc.MipLevels = 1;
        vbDesc.SampleDesc.Count = 1;
        vbDesc.SampleDesc.Quality = 0;
        vbDesc.Width = m_VertexPick.VBSize;

        hr = m_pDevice->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &vbDesc,
                                                D3D12_RESOURCE_STATE_GENERIC_READ, NULL,
                                                __uuidof(ID3D12Resource), (void **)&m_VertexPick.VB);

        if(FAILED(hr))
        {
          RDCERR("Couldn't create pick vertex buffer: HRESULT: %s", ToStr(hr).c_str());
          return ~0U;
        }

        m_VertexPick.VB->SetName(L"m_PickVB");

        sdesc.Buffer.NumElements = (maxIndex + 1);
        m_pDevice->CreateShaderResourceView(m_VertexPick.VB, &sdesc,
                                            GetDebugManager()->GetCPUHandle(PICK_VB_SRV));
      }
    }
    else
    {
      sdesc.Buffer.NumElements = 4;
      m_pDevice->CreateShaderResourceView(NULL, &sdesc, GetDebugManager()->GetCPUHandle(PICK_VB_SRV));
    }

    rdcarray<FloatVector> vbData;
    vbData.resize(maxIndex + 1);

    byte *data = &oldData[0];
    byte *dataEnd = data + oldData.size();

    bool valid = true;

    // the index buffer may refer to vertices past the start of the vertex buffer, so we can't just
    // conver the first N vertices we'll need.
    // Instead we grab min and max above, and convert every vertex in that range. This might
    // slightly over-estimate but not as bad as 0-max or the whole buffer.
    for(uint32_t idx = minIndex; idx <= maxIndex; idx++)
      vbData[idx] = HighlightCache::InterpretVertex(data, idx, cfg.position.vertexByteStride,
                                                    cfg.position.format, dataEnd, valid);

    GetDebugManager()->FillBuffer(m_VertexPick.VB, 0, vbData.data(), sizeof(Vec4f) * (maxIndex + 1));
  }

  ID3D12GraphicsCommandList *list = m_pDevice->GetNewList();
  if(!list)
    return ~0U;

  list->SetPipelineState(m_VertexPick.Pipe);

  list->SetComputeRootSignature(m_VertexPick.RootSig);

  GetDebugManager()->SetDescriptorHeaps(list, true, false);

  list->SetComputeRootConstantBufferView(0, GetDebugManager()->UploadConstants(&cbuf, sizeof(cbuf)));
  list->SetComputeRootDescriptorTable(1, GetDebugManager()->GetGPUHandle(PICK_IB_SRV));
  list->SetComputeRootDescriptorTable(2, GetDebugManager()->GetGPUHandle(PICK_RESULT_UAV));

  list->Dispatch(cfg.position.numIndices / 1024 + 1, 1, 1);

  list->Close();
  m_pDevice->ExecuteLists();

  bytebuf results;
  GetDebugManager()->GetBufferData(m_VertexPick.ResultBuf, 0, 0, results);

  list = m_pDevice->GetNewList();
  if(!list)
    return ~0U;

  GetDebugManager()->SetDescriptorHeaps(list, true, false);

  UINT zeroes[4] = {0, 0, 0, 0};
  list->ClearUnorderedAccessViewUint(GetDebugManager()->GetGPUHandle(PICK_RESULT_CLEAR_UAV),
                                     GetDebugManager()->GetUAVClearHandle(PICK_RESULT_CLEAR_UAV),
                                     m_VertexPick.ResultBuf, zeroes, 0, NULL);

  list->Close();

  byte *data = &results[0];

  uint32_t numResults = *(uint32_t *)data;

  if(numResults > 0)
  {
    if(isTriangleMesh)
    {
      struct PickResult
      {
        uint32_t vertid;
        Vec3f intersectionPoint;
      };

      PickResult *pickResults = (PickResult *)(data + 64);

      PickResult *closest = pickResults;

      // distance from raycast hit to nearest worldspace position of the mouse
      float closestPickDistance = (closest->intersectionPoint - rayPos).Length();

      // min with size of results buffer to protect against overflows
      for(uint32_t i = 1; i < RDCMIN(VertexPicking::MaxMeshPicks, numResults); i++)
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

      PickResult *pickResults = (PickResult *)(data + 64);

      PickResult *closest = pickResults;

      // min with size of results buffer to protect against overflows
      for(uint32_t i = 1; i < RDCMIN(VertexPicking::MaxMeshPicks, numResults); i++)
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

void D3D12Replay::PickPixel(ResourceId texture, uint32_t x, uint32_t y, const Subresource &sub,
                            CompType typeCast, float pixel[4])
{
  SetOutputDimensions(1, 1);

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

    ID3D12Resource *resource = NULL;

    {
      auto it = m_pDevice->GetResourceList().find(texture);
      if(it != m_pDevice->GetResourceList().end())
        resource = it->second;
    }

    if(resource)
    {
      D3D12_RESOURCE_DESC desc = resource->GetDesc();

      uint32_t mipWidth = RDCMAX(1U, UINT(desc.Width >> sub.mip));
      uint32_t mipHeight = RDCMAX(1U, desc.Height >> sub.mip);

      texDisplay.xOffset = -(float(x) / float(mipWidth)) * desc.Width;
      texDisplay.yOffset = -(float(y) / float(mipHeight)) * desc.Height;
    }

    m_OutputViewport = {0, 0, 1, 1, 0.0f, 1.0f};
    RenderTextureInternal(GetDebugManager()->GetCPUHandle(PICK_PIXEL_RTV), texDisplay,
                          eTexDisplay_32Render);
  }

  ID3D12GraphicsCommandList *list = m_pDevice->GetNewList();
  if(!list)
    return;

  D3D12_RESOURCE_BARRIER barrier = {};

  barrier.Transition.pResource = m_PixelPick.Texture;
  barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
  barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;

  list->ResourceBarrier(1, &barrier);

  D3D12_TEXTURE_COPY_LOCATION dst = {}, src = {};

  src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
  src.pResource = m_PixelPick.Texture;
  src.SubresourceIndex = 0;

  dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
  dst.pResource = m_General.ResultReadbackBuffer;
  dst.PlacedFootprint.Offset = 0;
  dst.PlacedFootprint.Footprint.Width = sizeof(Vec4f);
  dst.PlacedFootprint.Footprint.Height = 1;
  dst.PlacedFootprint.Footprint.Depth = 1;
  dst.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
  dst.PlacedFootprint.Footprint.RowPitch = D3D12_TEXTURE_DATA_PITCH_ALIGNMENT;

  list->CopyTextureRegion(&dst, 0, 0, 0, &src, NULL);

  std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);

  list->ResourceBarrier(1, &barrier);

  list->Close();

  m_pDevice->ExecuteLists();
  m_pDevice->FlushLists();

  D3D12_RANGE range = {0, sizeof(Vec4f)};

  float *pix = NULL;
  HRESULT hr = m_General.ResultReadbackBuffer->Map(0, &range, (void **)&pix);
  CHECK_HR(m_pDevice, hr);

  if(FAILED(hr))
  {
    RDCERR("Failed to map picking stage tex HRESULT: %s", ToStr(hr).c_str());
  }

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

  range.End = 0;

  if(SUCCEEDED(hr))
    m_General.ResultReadbackBuffer->Unmap(0, &range);
}

bool D3D12Replay::GetMinMax(ResourceId texid, const Subresource &sub, CompType typeCast,
                            float *minval, float *maxval)
{
  ID3D12Resource *resource = NULL;

  {
    auto it = m_pDevice->GetResourceList().find(texid);
    if(it != m_pDevice->GetResourceList().end())
      resource = it->second;
  }

  if(resource == NULL)
    return false;

  D3D12_RESOURCE_DESC resourceDesc = resource->GetDesc();

  HistogramCBufferData cdata;
  cdata.HistogramTextureResolution.x = float(RDCMAX(1U, uint32_t(resourceDesc.Width >> sub.mip)));
  cdata.HistogramTextureResolution.y = float(RDCMAX(1U, uint32_t(resourceDesc.Height >> sub.mip)));
  cdata.HistogramTextureResolution.z =
      float(RDCMAX(1U, uint32_t(resourceDesc.DepthOrArraySize >> sub.mip)));

  if(resourceDesc.DepthOrArraySize > 1 && resourceDesc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE3D)
    cdata.HistogramTextureResolution.z = float(resourceDesc.DepthOrArraySize);

  if(resourceDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D)
    cdata.HistogramSlice =
        float(RDCCLAMP(sub.slice, 0U, uint32_t((resourceDesc.DepthOrArraySize >> sub.mip) - 1)));
  else
    cdata.HistogramSlice =
        float(RDCCLAMP(sub.slice, 0U, uint32_t(resourceDesc.DepthOrArraySize - 1)));

  cdata.HistogramMip = sub.mip;
  cdata.HistogramSample = (int)RDCCLAMP(sub.sample, 0U, resourceDesc.SampleDesc.Count - 1);
  if(sub.sample == ~0U)
    cdata.HistogramSample = -int(resourceDesc.SampleDesc.Count);
  cdata.HistogramMin = 0.0f;
  cdata.HistogramMax = 1.0f;
  cdata.HistogramChannels = 0xf;
  cdata.HistogramFlags = 0;

  Vec4u YUVDownsampleRate = {};
  Vec4u YUVAChannels = {};

  GetYUVShaderParameters(resourceDesc.Format, YUVDownsampleRate, YUVAChannels);

  cdata.HistogramYUVDownsampleRate = YUVDownsampleRate;
  cdata.HistogramYUVAChannels = YUVAChannels;

  int intIdx = 0;

  DXGI_FORMAT fmt = GetTypedFormat(resourceDesc.Format, typeCast);

  if(IsUIntFormat(fmt))
    intIdx = 1;
  else if(IsIntFormat(fmt))
    intIdx = 2;

  int blocksX = (int)ceil(cdata.HistogramTextureResolution.x /
                          float(HGRAM_PIXELS_PER_TILE * HGRAM_TILES_PER_BLOCK));
  int blocksY = (int)ceil(cdata.HistogramTextureResolution.y /
                          float(HGRAM_PIXELS_PER_TILE * HGRAM_TILES_PER_BLOCK));

  BarrierSet barriers;
  int resType = 0;
  GetDebugManager()->PrepareTextureSampling(resource, typeCast, resType, barriers);

  {
    ID3D12GraphicsCommandListX *list = m_pDevice->GetNewList();
    if(!list)
      return false;

    barriers.Apply(list);

    list->SetPipelineState(m_Histogram.TileMinMaxPipe[resType][intIdx]);

    list->SetComputeRootSignature(m_Histogram.HistogramRootSig);

    GetDebugManager()->SetDescriptorHeaps(list, true, true);

    D3D12_GPU_DESCRIPTOR_HANDLE uav = GetDebugManager()->GetGPUHandle(MINMAX_TILE_UAVS);
    D3D12_GPU_DESCRIPTOR_HANDLE srv = GetDebugManager()->GetGPUHandle(FIRST_TEXDISPLAY_SRV);

    list->SetComputeRootConstantBufferView(
        0, GetDebugManager()->UploadConstants(&cdata, sizeof(cdata)));
    list->SetComputeRootDescriptorTable(1, srv);
    list->SetComputeRootDescriptorTable(2, GetDebugManager()->GetGPUHandle(FIRST_SAMP));
    list->SetComputeRootDescriptorTable(3, uav);

    // discard the whole resource as we will overwrite it
    D3D12_DISCARD_REGION region = {};
    region.NumSubresources = 1;
    list->DiscardResource(m_Histogram.MinMaxTileBuffer, &region);

    list->Dispatch(blocksX, blocksY, 1);

    D3D12_RESOURCE_BARRIER tileBarriers[2] = {};

    // ensure UAV work is done. Transition to SRV
    tileBarriers[0].Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
    tileBarriers[0].UAV.pResource = m_Histogram.MinMaxTileBuffer;
    tileBarriers[1].Transition.pResource = m_Histogram.MinMaxTileBuffer;
    tileBarriers[1].Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    tileBarriers[1].Transition.StateAfter = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;

    list->ResourceBarrier(2, tileBarriers);

    // set up second dispatch
    srv = GetDebugManager()->GetGPUHandle(MINMAX_TILE_SRVS);
    uav = GetDebugManager()->GetGPUHandle(MINMAX_RESULT_UAVS);

    list->SetComputeRootDescriptorTable(1, srv);
    list->SetComputeRootDescriptorTable(3, uav);

    list->SetPipelineState(m_Histogram.ResultMinMaxPipe[intIdx]);

    list->Dispatch(1, 1, 1);

    // transition back to UAV for next time
    std::swap(tileBarriers[1].Transition.StateBefore, tileBarriers[1].Transition.StateAfter);

    list->ResourceBarrier(1, &tileBarriers[1]);

    // finish the UAV work, and transition to copy.
    tileBarriers[0].UAV.pResource = m_Histogram.MinMaxResultBuffer;
    tileBarriers[1].Transition.pResource = m_Histogram.MinMaxResultBuffer;
    tileBarriers[1].Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    tileBarriers[1].Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;

    list->ResourceBarrier(2, tileBarriers);

    // copy to readback
    list->CopyBufferRegion(m_General.ResultReadbackBuffer, 0, m_Histogram.MinMaxResultBuffer, 0,
                           sizeof(Vec4f) * 2);

    // transition back to UAV for next time
    std::swap(tileBarriers[1].Transition.StateBefore, tileBarriers[1].Transition.StateAfter);

    list->ResourceBarrier(1, &tileBarriers[1]);

    barriers.Unapply(list);

    list->Close();

    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists();
  }

  D3D12_RANGE range = {0, sizeof(Vec4f) * 2};

  void *data = NULL;
  HRESULT hr = m_General.ResultReadbackBuffer->Map(0, &range, &data);
  CHECK_HR(m_pDevice, hr);

  if(FAILED(hr))
  {
    RDCERR("Failed to map bufferdata buffer HRESULT: %s", ToStr(hr).c_str());
    return false;
  }
  else
  {
    Vec4f *minmax = (Vec4f *)data;

    minval[0] = minmax[0].x;
    minval[1] = minmax[0].y;
    minval[2] = minmax[0].z;
    minval[3] = minmax[0].w;

    maxval[0] = minmax[1].x;
    maxval[1] = minmax[1].y;
    maxval[2] = minmax[1].z;
    maxval[3] = minmax[1].w;

    range.End = 0;

    m_General.ResultReadbackBuffer->Unmap(0, &range);
  }

  return true;
}

bool D3D12Replay::GetHistogram(ResourceId texid, const Subresource &sub, CompType typeCast,
                               float minval, float maxval, const rdcfixedarray<bool, 4> &channels,
                               rdcarray<uint32_t> &histogram)
{
  if(minval >= maxval)
    return false;

  ID3D12Resource *resource = NULL;

  {
    auto it = m_pDevice->GetResourceList().find(texid);
    if(it != m_pDevice->GetResourceList().end())
      resource = it->second;
  }

  if(resource == NULL)
    return false;

  D3D12_RESOURCE_DESC resourceDesc = resource->GetDesc();

  HistogramCBufferData cdata;
  cdata.HistogramTextureResolution.x = float(RDCMAX(1U, uint32_t(resourceDesc.Width >> sub.mip)));
  cdata.HistogramTextureResolution.y = float(RDCMAX(1U, uint32_t(resourceDesc.Height >> sub.mip)));
  cdata.HistogramTextureResolution.z =
      float(RDCMAX(1U, uint32_t(resourceDesc.DepthOrArraySize >> sub.mip)));

  if(resourceDesc.DepthOrArraySize > 1 && resourceDesc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE3D)
    cdata.HistogramTextureResolution.z = float(resourceDesc.DepthOrArraySize);

  if(resourceDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D)
    cdata.HistogramSlice =
        float(RDCCLAMP(sub.slice, 0U, uint32_t((resourceDesc.DepthOrArraySize >> sub.mip) - 1)));
  else
    cdata.HistogramSlice =
        float(RDCCLAMP(sub.slice, 0U, uint32_t(resourceDesc.DepthOrArraySize - 1)));

  cdata.HistogramMip = sub.mip;
  cdata.HistogramSample = (int)RDCCLAMP(sub.sample, 0U, resourceDesc.SampleDesc.Count - 1);
  if(sub.sample == ~0U)
    cdata.HistogramSample = -int(resourceDesc.SampleDesc.Count);
  cdata.HistogramMin = minval;
  cdata.HistogramFlags = 0;

  Vec4u YUVDownsampleRate = {};
  Vec4u YUVAChannels = {};

  GetYUVShaderParameters(resourceDesc.Format, YUVDownsampleRate, YUVAChannels);

  cdata.HistogramYUVDownsampleRate = YUVDownsampleRate;
  cdata.HistogramYUVAChannels = YUVAChannels;

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

  int intIdx = 0;

  DXGI_FORMAT fmt = GetTypedFormat(resourceDesc.Format, typeCast);

  if(IsUIntFormat(fmt))
    intIdx = 1;
  else if(IsIntFormat(fmt))
    intIdx = 2;

  int tilesX = (int)ceil(cdata.HistogramTextureResolution.x /
                         float(HGRAM_PIXELS_PER_TILE * HGRAM_TILES_PER_BLOCK));
  int tilesY = (int)ceil(cdata.HistogramTextureResolution.y /
                         float(HGRAM_PIXELS_PER_TILE * HGRAM_TILES_PER_BLOCK));

  BarrierSet barriers;
  int resType = 0;
  GetDebugManager()->PrepareTextureSampling(resource, typeCast, resType, barriers);

  {
    ID3D12GraphicsCommandListX *list = m_pDevice->GetNewList();
    if(!list)
      return false;

    barriers.Apply(list);

    list->SetPipelineState(m_Histogram.HistogramPipe[resType][intIdx]);

    list->SetComputeRootSignature(m_Histogram.HistogramRootSig);

    GetDebugManager()->SetDescriptorHeaps(list, true, true);

    D3D12_GPU_DESCRIPTOR_HANDLE uav = GetDebugManager()->GetGPUHandle(HISTOGRAM_UAV);
    D3D12_GPU_DESCRIPTOR_HANDLE srv = GetDebugManager()->GetGPUHandle(FIRST_TEXDISPLAY_SRV);
    D3D12_CPU_DESCRIPTOR_HANDLE uavcpu = GetDebugManager()->GetUAVClearHandle(HISTOGRAM_UAV);

    UINT zeroes[] = {0, 0, 0, 0};
    list->ClearUnorderedAccessViewUint(uav, uavcpu, m_Histogram.MinMaxTileBuffer, zeroes, 0, NULL);

    list->SetComputeRootConstantBufferView(
        0, GetDebugManager()->UploadConstants(&cdata, sizeof(cdata)));
    list->SetComputeRootDescriptorTable(1, srv);
    list->SetComputeRootDescriptorTable(2, GetDebugManager()->GetGPUHandle(FIRST_SAMP));
    list->SetComputeRootDescriptorTable(3, uav);

    list->Dispatch(tilesX, tilesY, 1);

    D3D12_RESOURCE_BARRIER tileBarriers[2] = {};

    // finish the UAV work, and transition to copy.
    tileBarriers[0].Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
    tileBarriers[0].UAV.pResource = m_Histogram.MinMaxTileBuffer;
    tileBarriers[1].Transition.pResource = m_Histogram.MinMaxTileBuffer;
    tileBarriers[1].Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    tileBarriers[1].Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;

    list->ResourceBarrier(2, tileBarriers);

    // copy to readback
    list->CopyBufferRegion(m_General.ResultReadbackBuffer, 0, m_Histogram.MinMaxTileBuffer, 0,
                           sizeof(uint32_t) * HGRAM_NUM_BUCKETS);

    // transition back to UAV for next time
    std::swap(tileBarriers[1].Transition.StateBefore, tileBarriers[1].Transition.StateAfter);

    list->ResourceBarrier(1, &tileBarriers[1]);

    barriers.Unapply(list);

    list->Close();

    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists();
  }

  D3D12_RANGE range = {0, sizeof(uint32_t) * HGRAM_NUM_BUCKETS};

  void *data = NULL;
  HRESULT hr = m_General.ResultReadbackBuffer->Map(0, &range, &data);
  CHECK_HR(m_pDevice, hr);

  histogram.clear();
  histogram.resize(HGRAM_NUM_BUCKETS);

  if(FAILED(hr))
  {
    RDCERR("Failed to map bufferdata buffer HRESULT: %s", ToStr(hr).c_str());
    return false;
  }
  else
  {
    memcpy(&histogram[0], data, sizeof(uint32_t) * HGRAM_NUM_BUCKETS);

    range.End = 0;

    m_General.ResultReadbackBuffer->Unmap(0, &range);
  }

  return true;
}

rdcarray<uint32_t> D3D12Replay::GetPassEvents(uint32_t eventId)
{
  rdcarray<uint32_t> passEvents;

  const ActionDescription *action = m_pDevice->GetAction(eventId);

  if(!action)
    return passEvents;

  // for D3D12 a pass == everything writing to the same RTs in a command list.
  const ActionDescription *start = action;
  while(start)
  {
    // if we've come to the beginning of a list, break out of the loop, we've
    // found the start.
    if(start->flags & ActionFlags::BeginPass)
      break;

    // if we come to the END of a list, since we were iterating backwards that
    // means we started outside of a list, so return empty set.
    if(start->flags & ActionFlags::EndPass)
      return passEvents;

    // if we've come to the start of the log we were outside of a list
    // to start with
    if(start->previousAction == NULL)
      return passEvents;

    // step back
    const ActionDescription *prev = start->previousAction;

    // if the previous is a clear, we're done
    if(prev->flags & ActionFlags::Clear)
      break;

    // if the outputs changed, we're done
    if(start->outputs != prev->outputs || start->depthOut != prev->depthOut)
      break;

    start = prev;
  }

  // store all the action eventIDs up to the one specified at the start
  while(start)
  {
    if(start->eventId >= action->eventId)
      break;

    // include pass boundaries, these will be filtered out later
    // so we don't actually do anything (init postvs/action overlay)
    // but it's useful to have the first part of the pass as part
    // of the list
    if(start->flags & (ActionFlags::MeshDispatch | ActionFlags::Drawcall | ActionFlags::PassBoundary))
      passEvents.push_back(start->eventId);

    start = start->nextAction;
  }

  return passEvents;
}

bool D3D12Replay::IsTextureSupported(const TextureDescription &tex)
{
  return MakeDXGIFormat(tex.format) != DXGI_FORMAT_UNKNOWN;
}

bool D3D12Replay::NeedRemapForFetch(const ResourceFormat &format)
{
  return false;
}

void D3D12Replay::GetBufferData(ResourceId buff, uint64_t offset, uint64_t length, bytebuf &ret)
{
  ID3D12DeviceChild *res = m_pDevice->GetResourceManager()->GetResource(buff);
  if(WrappedID3D12PipelineState::IsAlloc(res))
  {
    const D3D12RenderState &rs = m_pDevice->GetQueue()->GetCommandData()->m_RenderState;

    WrappedID3D12PipelineState *pipe = (WrappedID3D12PipelineState *)res;

    const rdcarray<D3D12RenderState::SignatureElement> &rootElems =
        pipe->IsGraphics() ? rs.graphics.sigelems : rs.compute.sigelems;

    bytebuf inlineData;

    for(uint32_t i = 0; i < rootElems.size(); i++)
      if(rootElems[i].type == eRootConst)
        inlineData.append((byte *)rootElems[i].constants.data(), rootElems[i].constants.byteSize());

    if(offset >= inlineData.size())
      return;

    if(length == 0 || length > inlineData.size())
      length = inlineData.size() - offset;

    if(offset + length > inlineData.size())
    {
      RDCWARN(
          "Attempting to read off the end of current push constants (%llu %llu). Will be clamped "
          "(%llu)",
          offset, length, inlineData.size());
      length = RDCMIN(length, inlineData.size() - offset);
    }

    ret.resize((size_t)length);

    memcpy(ret.data(), inlineData.data() + offset, ret.size());

    return;
  }

  auto it = m_pDevice->GetResourceList().find(buff);

  if(it == m_pDevice->GetResourceList().end() || it->second == NULL)
  {
    RDCERR("Getting buffer data for unknown buffer %s!", ToStr(buff).c_str());
    return;
  }

  WrappedID3D12Resource *buffer = it->second;

  if(buffer->GetDesc().Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
  {
    RDCERR("Getting buffer data for non-buffer %s!", ToStr(buff).c_str());
    return;
  }

  RDCASSERT(buffer);

  GetDebugManager()->GetBufferData(buffer, offset, length, ret);
}

void D3D12Replay::FillCBufferVariables(ResourceId pipeline, ResourceId shader, ShaderStage stage,
                                       rdcstr entryPoint, uint32_t cbufSlot,
                                       rdcarray<ShaderVariable> &outvars, const bytebuf &data)
{
  if(shader == ResourceId())
    return;

  ID3D12DeviceChild *res = m_pDevice->GetResourceManager()->GetResource(shader);

  WrappedID3D12Shader *sh = (WrappedID3D12Shader *)res;

  const ShaderReflection &refl = sh->GetDetails();

  if(cbufSlot >= (uint32_t)refl.constantBlocks.count())
  {
    RDCERR("Invalid cbuffer slot");
    return;
  }

  const ConstantBlock &c = refl.constantBlocks[cbufSlot];

  // check if the data actually comes from root constants
  const D3D12RenderState &rs = m_pDevice->GetQueue()->GetCommandData()->m_RenderState;

  WrappedID3D12RootSignature *sig = NULL;
  const rdcarray<D3D12RenderState::SignatureElement> *sigElems = NULL;

  if(refl.stage == ShaderStage::Compute && rs.compute.rootsig != ResourceId())
  {
    sig = m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12RootSignature>(rs.compute.rootsig);
    sigElems = &rs.compute.sigelems;
  }
  else if(refl.stage != ShaderStage::Compute && rs.graphics.rootsig != ResourceId())
  {
    sig = m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12RootSignature>(rs.graphics.rootsig);
    sigElems = &rs.graphics.sigelems;
  }

  bytebuf rootData;

  ShaderStageMask reflMask = MaskForStage(refl.stage);

  for(size_t i = 0; sig && i < sig->sig.Parameters.size(); i++)
  {
    const D3D12RootSignatureParameter &p = sig->sig.Parameters[i];

    if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS &&
       (ConvertVisibility(p.ShaderVisibility) & reflMask) &&
       p.Constants.RegisterSpace == c.fixedBindSetOrSpace &&
       p.Constants.ShaderRegister == c.fixedBindNumber)
    {
      size_t dstSize = sig->sig.Parameters[i].Constants.Num32BitValues * sizeof(uint32_t);
      rootData.resize(dstSize);

      if(i < sigElems->size())
      {
        const D3D12RenderState::SignatureElement &el = (*sigElems)[i];

        if(el.type == eRootConst)
        {
          byte *dst = &rootData[0];
          const UINT *src = &el.constants[0];
          size_t srcSize = el.constants.size() * sizeof(uint32_t);

          memcpy(dst, src, RDCMIN(srcSize, dstSize));
        }
      }
    }
  }

  StandardFillCBufferVariables(refl.resourceId, c.variables, outvars,
                               rootData.empty() ? data : rootData);
}

rdcarray<DebugMessage> D3D12Replay::GetDebugMessages()
{
  return m_pDevice->GetDebugMessages();
}

void D3D12Replay::BuildShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                              const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                              const rdcarray<rdcstr> &includeDirs, ShaderStage type, ResourceId &id,
                              rdcstr &errors)
{
  bytebuf compiledDXBC;

  const byte *dxbcBytes = source.data();
  size_t dxbcLength = source.size();

  if(sourceEncoding == ShaderEncoding::HLSL)
  {
    rdcstr profile = DXBC::GetProfile(compileFlags);

    if(profile.empty())
    {
      switch(type)
      {
        case ShaderStage::Vertex: profile = "vs_5_1"; break;
        case ShaderStage::Hull: profile = "hs_5_1"; break;
        case ShaderStage::Domain: profile = "ds_5_1"; break;
        case ShaderStage::Geometry: profile = "gs_5_1"; break;
        case ShaderStage::Pixel: profile = "ps_5_1"; break;
        case ShaderStage::Compute: profile = "cs_5_1"; break;
        case ShaderStage::Amplification: profile = "as_6_5"; break;
        case ShaderStage::Mesh: profile = "ms_6_5"; break;
        default:
          RDCERR("Unexpected type in BuildShader!");
          id = ResourceId();
          return;
      }
    }

    rdcstr hlsl;
    hlsl.assign((const char *)source.data(), source.size());

    ID3DBlob *blob = NULL;
    errors = m_pDevice->GetShaderCache()->GetShaderBlob(hlsl.c_str(), entry.c_str(), compileFlags,
                                                        includeDirs, profile.c_str(), &blob);

    if(m_D3D12On7 && blob == NULL && errors.contains("unrecognized compiler target"))
    {
      profile.back() = '0';
      errors = m_pDevice->GetShaderCache()->GetShaderBlob(hlsl.c_str(), entry.c_str(), compileFlags,
                                                          includeDirs, profile.c_str(), &blob);
    }

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

  D3D12_SHADER_BYTECODE byteCode;
  byteCode.BytecodeLength = dxbcLength;
  byteCode.pShaderBytecode = dxbcBytes;

  bool created = false;
  WrappedID3D12Shader *sh =
      WrappedID3D12Shader::AddShader(ResourceId(), byteCode, m_pDevice, &created, true);

  // A new replay-only shader's constructor reference belongs to this build.
  // The lookup table is non-owning: keeping another baseline reference leaks
  // each temporary target until capture teardown. Existing/captured shaders
  // instead lend the build one additional reference, balanced by FreeTargetResource.
  if(!created)
    sh->AddRef();

  id = sh->GetResourceID();
}

void D3D12Replay::BuildTargetShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                                    const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                                    ShaderStage type, ResourceId &id, rdcstr &errors)
{
  ShaderCompileFlags debugCompileFlags = DXBC::EncodeFlags(
      DXBC::DecodeFlags(compileFlags) | D3DCOMPILE_DEBUG, DXBC::GetProfile(compileFlags));

  BuildShader(sourceEncoding, source, entry, debugCompileFlags, {}, type, id, errors);
}

static uint64_t ReplayLabD3D12RootCBVWindowSize(uint64_t reflectedByteSize,
                                                uint64_t resourceRemainder)
{
  if(reflectedByteSize == 0 || reflectedByteSize > 64ULL * 1024ULL)
    return 0;
  const uint64_t result = AlignUp(reflectedByteSize, 16ULL);
  return result <= resourceRemainder ? result : 0;
}

static bool ReplayLabD3D12StageVisible(ShaderStage stage, D3D12_SHADER_VISIBILITY visibility)
{
  if(visibility == D3D12_SHADER_VISIBILITY_ALL)
    return true;

  switch(stage)
  {
    case ShaderStage::Vertex: return visibility == D3D12_SHADER_VISIBILITY_VERTEX;
    case ShaderStage::Hull: return visibility == D3D12_SHADER_VISIBILITY_HULL;
    case ShaderStage::Domain: return visibility == D3D12_SHADER_VISIBILITY_DOMAIN;
    case ShaderStage::Geometry: return visibility == D3D12_SHADER_VISIBILITY_GEOMETRY;
    case ShaderStage::Pixel: return visibility == D3D12_SHADER_VISIBILITY_PIXEL;
    case ShaderStage::Amplification:
      return visibility == D3D12_SHADER_VISIBILITY_AMPLIFICATION;
    case ShaderStage::Mesh: return visibility == D3D12_SHADER_VISIBILITY_MESH;
    default: return false;
  }
}

static WrappedID3D12Shader *ReplayLabD3D12PipelineShader(WrappedID3D12PipelineState *pipe,
                                                         ShaderStage stage)
{
  if(pipe == NULL)
    return NULL;

  switch(stage)
  {
    case ShaderStage::Vertex: return pipe->VS();
    case ShaderStage::Hull: return pipe->HS();
    case ShaderStage::Domain: return pipe->DS();
    case ShaderStage::Geometry: return pipe->GS();
    case ShaderStage::Pixel: return pipe->PS();
    case ShaderStage::Amplification: return pipe->AS();
    case ShaderStage::Mesh: return pipe->MS();
    case ShaderStage::Compute: return pipe->CS();
    default: return NULL;
  }
}

rdcstr D3D12Replay::ResolveEventCBufferBinding(
    uint32_t eventId, ShaderStage stage, uint32_t slot, uint32_t registerSpace,
    ResourceId expectedShader,
    ResourceId expectedBuffer, uint64_t expectedByteOffset, uint64_t expectedByteSize,
    EventCBufferBinding &resolved)
{
  const ActionDescription *action = m_pDevice->GetAction(eventId);
  if(action == NULL)
    return StringFormat::Fmt("Event %u is not a replayable action", eventId);

  const bool compute = stage == ShaderStage::Compute;
  if(compute)
  {
    if(!(action->flags & ActionFlags::Dispatch))
      return StringFormat::Fmt("Event %u is not a compute Dispatch", eventId);
  }
  else if(!(action->flags & (ActionFlags::Drawcall | ActionFlags::MeshDispatch)))
  {
    return StringFormat::Fmt("Event %u is not a graphics Draw or MeshDispatch", eventId);
  }

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  D3D12CommandData &cmd = *m_pDevice->GetQueue()->GetCommandData();
  D3D12RenderState &state = cmd.m_RenderState;

  if(state.stateobj != ResourceId() || state.pipe == ResourceId())
    return "D3D12 event CBuffer overrides support graphics/compute PSOs, not ray state objects";

  ResourceId capturedPipeId = rm->GetUnreplacedID(state.pipe);
  ID3D12DeviceChild *pipeResource = rm->GetResourceWithoutReplacement(capturedPipeId, true);
  WrappedID3D12PipelineState *pipe = (WrappedID3D12PipelineState *)pipeResource;
  if(pipe == NULL)
    return "The captured D3D12 pipeline state is no longer available";

  WrappedID3D12Shader *boundShader = ReplayLabD3D12PipelineShader(pipe, stage);
  if(boundShader == NULL)
    return StringFormat::Fmt("No %s shader is bound by the captured D3D12 PSO",
                             ToStr(stage).c_str());
  if(boundShader->GetResourceID() != expectedShader)
    return StringFormat::Fmt("Expected shader %s at %s, but the captured PSO binds %s",
                             ToStr(expectedShader).c_str(), ToStr(stage).c_str(),
                             ToStr(boundShader->GetResourceID()).c_str());

  const ShaderReflection &reflection = boundShader->GetDetails();
  if(reflection.stage != stage)
    return "The captured D3D12 shader reflection stage does not match the requested stage";

  const D3D12RenderState::RootSignature &rootState = compute ? state.compute : state.graphics;
  WrappedID3D12RootSignature *rootSig =
      rm->GetResAs<WrappedID3D12RootSignature>(rootState.rootsig, true);
  if(rootSig == NULL)
    return "The live D3D12 root signature is unavailable";

  rdcarray<EventCBufferBinding> matches;
  rdcstr firstMismatch;
  bool sawReflectedBinding = false;
  bool sawRootConstants = false;

  for(const ConstantBlock &block : reflection.constantBlocks)
  {
    if(block.fixedBindNumber != slot || block.fixedBindSetOrSpace != registerSpace ||
       block.bindArraySize != 1 || block.compileConstants)
      continue;

    sawReflectedBinding = true;
    const uint32_t space = block.fixedBindSetOrSpace;
    const size_t numRoots = RDCMIN(rootSig->sig.Parameters.size(), rootState.sigelems.size());
    for(size_t root = 0; root < numRoots; root++)
    {
      const D3D12RootSignatureParameter &param = rootSig->sig.Parameters[root];
      const D3D12RenderState::SignatureElement &element = rootState.sigelems[root];
      if(!ReplayLabD3D12StageVisible(stage, param.ShaderVisibility))
        continue;

      EventCBufferBinding candidate;
      candidate.compute = compute;
      candidate.registerSpace = space;
      candidate.rootIndex = (uint32_t)root;

      if(param.ParameterType == D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS &&
         param.Constants.ShaderRegister == slot && param.Constants.RegisterSpace == space)
      {
        sawRootConstants = true;
        continue;
      }

      if(param.ParameterType == D3D12_ROOT_PARAMETER_TYPE_CBV &&
         param.Descriptor.ShaderRegister == slot && param.Descriptor.RegisterSpace == space &&
         element.type == eRootCBV)
      {
        candidate.kind = EventCBufferBindingKind::RootCBV;
        candidate.buffer = rm->GetUnreplacedID(element.id);
        candidate.byteOffset = element.offset;

        ID3D12Resource *buffer =
            (ID3D12Resource *)rm->GetResourceWithoutReplacement(candidate.buffer, true);
        if(buffer == NULL || buffer->GetDesc().Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
           candidate.byteOffset >= buffer->GetDesc().Width)
        {
          if(firstMismatch.empty())
            firstMismatch = "The matching root CBV does not resolve to a valid captured buffer";
          continue;
        }
        // A root CBV has no descriptor size, so the generic pipeline view reports the whole
        // resource remainder (often a multi-megabyte upload ring). The editable window is the
        // shader's reflected block size, rounded only to D3D constant packing. This preserves all
        // bytes the shader can legally address without cloning an unrelated ring-buffer tail.
        candidate.byteSize = ReplayLabD3D12RootCBVWindowSize(
            block.byteSize, buffer->GetDesc().Width - candidate.byteOffset);
        if(candidate.byteSize == 0)
        {
          if(firstMismatch.empty())
            firstMismatch = "The reflected root-CBV window is empty, exceeds 64 KiB, or lies "
                            "outside its captured buffer";
          continue;
        }
      }
      else if(param.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE &&
              element.type == eRootTable)
      {
        uint64_t nextRangeOffset = 0;
        bool foundRange = false;
        uint64_t descriptorOffset = 0;
        for(const D3D12_DESCRIPTOR_RANGE1 &range : param.ranges)
        {
          uint64_t rangeOffset = range.OffsetInDescriptorsFromTableStart;
          if(range.OffsetInDescriptorsFromTableStart == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND)
            rangeOffset = nextRangeOffset;

          const uint64_t count =
              range.NumDescriptors == UINT_MAX ? UINT64_MAX : uint64_t(range.NumDescriptors);
          nextRangeOffset = count == UINT64_MAX || rangeOffset > UINT64_MAX - count
                                ? UINT64_MAX
                                : rangeOffset + count;

          if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_CBV &&
             range.RegisterSpace == space && range.BaseShaderRegister <= slot &&
             (range.NumDescriptors == UINT_MAX ||
              slot - range.BaseShaderRegister < range.NumDescriptors))
          {
            const uint64_t registerOffset = uint64_t(slot - range.BaseShaderRegister);
            if(rangeOffset > UINT64_MAX - registerOffset)
            {
              if(firstMismatch.empty())
                firstMismatch =
                    "The matching CBV descriptor-table range overflows its table offset";
              continue;
            }
            descriptorOffset = rangeOffset + registerOffset;
            foundRange = true;
            break;
          }
        }

        if(!foundRange)
          continue;

        candidate.kind = EventCBufferBindingKind::DescriptorTableCBV;
        candidate.sourceHeap = element.id;
        WrappedID3D12DescriptorHeap *heap =
            rm->GetResAs<WrappedID3D12DescriptorHeap>(element.id, true);
        if(heap == NULL || descriptorOffset > UINT_MAX ||
           element.offset > UINT_MAX - descriptorOffset ||
           element.offset + descriptorOffset >= heap->GetNumDescriptors())
        {
          if(firstMismatch.empty())
            firstMismatch = "The matching CBV descriptor-table location is outside its heap";
          continue;
        }

        candidate.descriptorIndex = (uint32_t)(element.offset + descriptorOffset);
        D3D12Descriptor *descriptor = heap->GetDescriptors() + candidate.descriptorIndex;
        if(descriptor->GetType() != D3D12DescriptorType::CBV)
        {
          if(firstMismatch.empty())
            firstMismatch = "The matching descriptor-table entry is not a CBV";
          continue;
        }

        const D3D12_CONSTANT_BUFFER_VIEW_DESC &cbv = descriptor->GetCBV();
        ResourceId bufferId;
        UINT64 bufferOffset = 0;
        WrappedID3D12Resource::GetResIDFromAddr(cbv.BufferLocation, bufferId, bufferOffset);
        candidate.buffer = rm->GetUnreplacedID(bufferId);
        candidate.byteOffset = bufferOffset;
        candidate.byteSize = cbv.SizeInBytes;

        ID3D12Resource *buffer =
            (ID3D12Resource *)rm->GetResourceWithoutReplacement(candidate.buffer, true);
        if(buffer == NULL || buffer->GetDesc().Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
           candidate.byteOffset > buffer->GetDesc().Width ||
           candidate.byteSize > buffer->GetDesc().Width - candidate.byteOffset)
        {
          if(firstMismatch.empty())
            firstMismatch = "The matching table CBV window lies outside its captured buffer";
          continue;
        }
      }
      else
      {
        continue;
      }

      // ExecuteIndirect command signatures may contain CONSTANT_BUFFER_VIEW arguments. Those
      // arguments are consumed during OnlyDraw and can overwrite the CPU root element installed
      // above. Until the indirect argument buffer is transactionally patched by root index, a root
      // CBV override cannot truthfully claim to be active. Descriptor-table CBVs remain safe.
      if(candidate.kind == EventCBufferBindingKind::RootCBV &&
         (action->flags & ActionFlags::Indirect))
      {
        if(firstMismatch.empty())
          firstMismatch = "D3D12 ExecuteIndirect root-CBV overrides are excluded because the "
                          "command signature may overwrite the root descriptor during OnlyDraw";
        continue;
      }

      if(candidate.buffer == expectedBuffer && candidate.byteOffset == expectedByteOffset &&
         candidate.byteSize == expectedByteSize)
      {
        matches.push_back(candidate);
      }
      else if(firstMismatch.empty())
      {
        firstMismatch = StringFormat::Fmt(
            "Expected D3D12 CBuffer %s offset/size %llu/%llu, but b%u space%u resolves to %s "
            "%llu/%llu",
            ToStr(expectedBuffer).c_str(), (unsigned long long)expectedByteOffset,
            (unsigned long long)expectedByteSize, slot, space,
            ToStr(candidate.buffer).c_str(), (unsigned long long)candidate.byteOffset,
            (unsigned long long)candidate.byteSize);
      }
    }
  }

  if(matches.size() == 1)
  {
    resolved = matches[0];
    return {};
  }
  if(matches.size() > 1)
    return "The D3D12 CBuffer binding is ambiguous across root parameters";
  if(!firstMismatch.empty())
    return firstMismatch;
  if(sawRootConstants)
    return "The reflected D3D12 constant block is backed by root constants, which are excluded";
  if(!sawReflectedBinding)
    return StringFormat::Fmt("Shader %s has no fixed, buffer-backed b%u space%u CBuffer",
                             ToStr(expectedShader).c_str(), slot, registerSpace);
  return "The reflected D3D12 CBuffer has no supported root-CBV or descriptor-table-CBV binding";
}

rdcstr D3D12Replay::SetEventCBufferOverride(
    uint32_t eventId, ShaderStage stage, uint32_t slot, ResourceId expectedShader,
    ResourceId expectedBuffer, uint64_t expectedByteOffset, uint64_t expectedByteSize,
    const bytebuf &data, uint32_t registerSpace)
{
  RestoreEventCBufferOverrides();

  if(expectedShader == ResourceId() || expectedBuffer == ResourceId())
    return "D3D12 event CBuffer overrides require captured shader and buffer resources";
  if(data.empty() || data.size() > 64ULL * 1024ULL || (data.size() % 16) != 0)
    return "data must be non-empty, 16-byte aligned, and no larger than 64 KiB";
  if(expectedByteOffset % D3D12_CONSTANT_BUFFER_DATA_PLACEMENT_ALIGNMENT != 0 ||
     expectedByteSize == 0 || expectedByteSize > 64ULL * 1024ULL ||
     (expectedByteSize % 16) != 0)
    return "D3D12 CBuffer offsets must be 256-byte aligned and sizes must be aligned, non-empty, "
           "and no larger than 64 KiB";

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  if(rm->GetUnreplacedID(expectedShader) != expectedShader)
    return "expectedShader must identify the canonical captured shader, not a replacement";
  if(rm->GetUnreplacedID(expectedBuffer) != expectedBuffer)
    return "expectedBuffer must identify the canonical captured buffer, not a replacement";

  EventCBufferBinding binding;
  rdcstr error = ResolveEventCBufferBinding(eventId, stage, slot, registerSpace, expectedShader,
                                            expectedBuffer, expectedByteOffset, expectedByteSize,
                                            binding);
  if(!error.empty())
    return error;
  if(data.size() != binding.byteSize)
    return StringFormat::Fmt("data must contain the complete D3D12 CBV window (%llu bytes)",
                             (unsigned long long)binding.byteSize);

  EventCBufferOverrideKey key = {eventId, stage, slot, registerSpace};
  auto configuredIt = m_EventCBufferOverrides.find(key);
  ID3D12Resource *shadow =
      configuredIt == m_EventCBufferOverrides.end() ? NULL : configuredIt->second.shadow;
  const uint64_t shadowSize = AlignUp(
      uint64_t(data.size()), uint64_t(D3D12_CONSTANT_BUFFER_DATA_PLACEMENT_ALIGNMENT));
  if(shadow != NULL && shadow->GetDesc().Width != shadowSize)
    shadow = NULL;

  ID3D12Resource *newShadow = NULL;
  if(shadow == NULL)
  {
    newShadow = m_DebugManager->MakeCBuffer(shadowSize);
    if(newShadow == NULL)
      return "Failed to create the replay-owned D3D12 event CBuffer shadow";
  }

  EventCBufferOverride &configured = m_EventCBufferOverrides[key];
  if(newShadow != NULL)
  {
    // The previous OnlyDraw submission may still reference this upload buffer. Restore() only
    // repairs CPU render state; it is not a GPU fence.
    if(configured.shadow != NULL && !m_pDevice->DeviceWaitForIdle())
    {
      SAFE_RELEASE(newShadow);
      return "GPU completion could not be confirmed before retiring the D3D12 event CBuffer shadow";
    }
    SAFE_RELEASE(configured.shadow);
    configured.shadow = newShadow;
  }
  configured.expectedShader = expectedShader;
  configured.expectedBuffer = expectedBuffer;
  configured.expectedByteOffset = expectedByteOffset;
  configured.expectedByteSize = expectedByteSize;
  configured.data = data;
  configured.binding = binding;

  ClearPostVSCache();
  ClearFeedbackCache();
  return {};
}

void D3D12Replay::ReleaseEventCBufferShadowHeaps()
{
  for(auto it = m_EventCBufferShadowHeaps.begin(); it != m_EventCBufferShadowHeaps.end(); ++it)
    SAFE_RELEASE(it->second);
  m_EventCBufferShadowHeaps.clear();
}

void D3D12Replay::ClearEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                            uint32_t registerSpace)
{
  RestoreEventCBufferOverrides();
  EventCBufferOverrideKey key = {eventId, stage, slot, registerSpace};
  auto it = m_EventCBufferOverrides.find(key);
  if(it == m_EventCBufferOverrides.end())
    return;

  // ExecuteLists for the preceding OnlyDraw is asynchronous. Fence before releasing either its
  // upload buffer or the cloned shader-visible heap it may still have bound.
  if(!m_pDevice->DeviceWaitForIdle())
    return;
  SAFE_RELEASE(it->second.shadow);
  m_EventCBufferOverrides.erase(it);
  ReleaseEventCBufferShadowHeaps();
  ClearPostVSCache();
  ClearFeedbackCache();
}

void D3D12Replay::ClearEventCBufferOverrides()
{
  RestoreEventCBufferOverrides();
  if((!m_EventCBufferOverrides.empty() || !m_EventCBufferShadowHeaps.empty()) &&
     !m_pDevice->DeviceWaitForIdle())
    return;
  for(auto it = m_EventCBufferOverrides.begin(); it != m_EventCBufferOverrides.end(); ++it)
    SAFE_RELEASE(it->second.shadow);
  m_EventCBufferOverrides.clear();
  ReleaseEventCBufferShadowHeaps();
  ClearPostVSCache();
  ClearFeedbackCache();
}

rdcstr D3D12Replay::GetOrCreateEventCBufferShadowHeap(ResourceId sourceHeap,
                                                       ID3D12DescriptorHeap *&shadowHeap)
{
  shadowHeap = NULL;
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  WrappedID3D12DescriptorHeap *source =
      rm->GetResAs<WrappedID3D12DescriptorHeap>(sourceHeap, true);
  if(source == NULL)
    return "The captured D3D12 descriptor heap is no longer available";

  D3D12_DESCRIPTOR_HEAP_DESC desc = source->GetDesc();
  if(desc.Type != D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV ||
     !(desc.Flags & D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE))
    return "The CBuffer descriptor is not in a shader-visible CBV/SRV/UAV heap";
  if(desc.NumDescriptors == 0 || desc.NumDescriptors > 1000000)
    return "The CBuffer descriptor heap size is invalid or exceeds the safe clone limit";

  auto it = m_EventCBufferShadowHeaps.find(sourceHeap);
  if(it != m_EventCBufferShadowHeaps.end())
  {
    D3D12_DESCRIPTOR_HEAP_DESC existing = it->second->GetDesc();
    if(existing.Type == desc.Type && existing.Flags == desc.Flags &&
       existing.NumDescriptors == desc.NumDescriptors)
    {
      shadowHeap = it->second;
      return {};
    }
    SAFE_RELEASE(it->second);
    m_EventCBufferShadowHeaps.erase(it);
  }

  HRESULT hr = m_pDevice->CreateDescriptorHeap(&desc, __uuidof(ID3D12DescriptorHeap),
                                                (void **)&shadowHeap);
  if(FAILED(hr) || shadowHeap == NULL)
  {
    SAFE_RELEASE(shadowHeap);
    return StringFormat::Fmt("Failed to create the D3D12 event descriptor-heap shadow: %s",
                             ToStr(hr).c_str());
  }
  rm->SetInternalResource(shadowHeap);
  m_EventCBufferShadowHeaps[sourceHeap] = shadowHeap;
  return {};
}

rdcstr D3D12Replay::ApplyEventCBufferOverrides(uint32_t eventId)
{
  if(m_EventCBufferStateApplied)
    return "A previous D3D12 event CBuffer override was not restored";

  rdcarray<std::pair<const EventCBufferOverrideKey *, EventCBufferOverride *> > pending;
  for(auto it = m_EventCBufferOverrides.begin(); it != m_EventCBufferOverrides.end(); ++it)
    if(it->first.eventId == eventId)
      pending.push_back({&it->first, &it->second});
  if(pending.empty())
    return {};

  m_pDevice->InvalidateLiveForward("eventCBufferOverride");

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  D3D12CommandData &cmd = *m_pDevice->GetQueue()->GetCommandData();
  D3D12RenderState &state = cmd.m_RenderState;

  std::map<uint64_t, ResourceId> rootTargets;
  std::map<std::pair<ResourceId, uint32_t>, ResourceId> tableTargets;

  // Re-resolve every binding against the state produced by WithoutDraw. No shadow is written and
  // no root/heap state is changed until the full set has passed this validation.
  for(const auto &item : pending)
  {
    EventCBufferBinding live;
    rdcstr error = ResolveEventCBufferBinding(
        eventId, item.first->stage, item.first->slot, item.first->registerSpace,
        item.second->expectedShader,
        item.second->expectedBuffer, item.second->expectedByteOffset,
        item.second->expectedByteSize, live);
    if(!error.empty())
      return error;

    const EventCBufferBinding &saved = item.second->binding;
    if(live.compute != saved.compute || live.kind != saved.kind ||
       live.registerSpace != saved.registerSpace || live.rootIndex != saved.rootIndex ||
       live.sourceHeap != saved.sourceHeap || live.descriptorIndex != saved.descriptorIndex ||
       live.buffer != saved.buffer || live.byteOffset != saved.byteOffset ||
       live.byteSize != saved.byteSize)
      return "The exact D3D12 root/descriptor binding changed since the override was configured";
    if(item.second->shadow == NULL)
      return "A replay-owned D3D12 CBuffer shadow is no longer valid";

    const ResourceId shadowId = GetResID(item.second->shadow);
    if(live.kind == EventCBufferBindingKind::RootCBV)
    {
      const uint64_t key = (uint64_t(live.compute ? 1 : 0) << 32) | live.rootIndex;
      auto inserted = rootTargets.insert({key, shadowId});
      if(!inserted.second && inserted.first->second != shadowId)
        return "Two material sessions request different data for the same D3D12 root CBV";
    }
    else
    {
      auto key = std::make_pair(live.sourceHeap, live.descriptorIndex);
      auto inserted = tableTargets.insert({key, shadowId});
      if(!inserted.second && inserted.first->second != shadowId)
        return "Two material sessions request different data for the same D3D12 CBV descriptor";
    }
  }

  // Upload all buffers first. A map failure still leaves the captured state and descriptors
  // untouched.
  //
  // Shadows are deliberately reused between slider revisions. The previous OnlyDraw submission
  // is asynchronous, so fence it before overwriting upload memory or cloned heap descriptors.
  if(!m_pDevice->DeviceWaitForIdle())
    return "GPU completion could not be confirmed before updating D3D12 event CBuffer shadows";
  for(const auto &item : pending)
  {
    ID3D12Resource *realShadow = Unwrap(item.second->shadow);
    void *mapped = NULL;
    D3D12_RANGE noRead = {0, 0};
    HRESULT hr = realShadow->Map(0, &noRead, &mapped);
    if(FAILED(hr) || mapped == NULL)
    {
      if(SUCCEEDED(hr) && mapped == NULL)
        realShadow->Unmap(0, NULL);
      return StringFormat::Fmt("Failed to update the D3D12 event CBuffer shadow: %s",
                               ToStr(hr).c_str());
    }

    memset(mapped, 0, (size_t)item.second->shadow->GetDesc().Width);
    memcpy(mapped, item.second->data.data(), item.second->data.size());
    D3D12_RANGE written = {0, (SIZE_T)item.second->shadow->GetDesc().Width};
    realShadow->Unmap(0, &written);
  }

  // Clone each source heap in full. This preserves unrelated descriptors and supports direct heap
  // indexing without ever modifying the captured descriptor heap.
  std::map<ResourceId, ID3D12DescriptorHeap *> shadowHeaps;
  for(const auto &target : tableTargets)
  {
    const ResourceId sourceHeapId = target.first.first;
    if(shadowHeaps.find(sourceHeapId) != shadowHeaps.end())
      continue;

    ID3D12DescriptorHeap *shadowHeap = NULL;
    rdcstr error = GetOrCreateEventCBufferShadowHeap(sourceHeapId, shadowHeap);
    if(!error.empty())
      return error;

    WrappedID3D12DescriptorHeap *source =
        rm->GetResAs<WrappedID3D12DescriptorHeap>(sourceHeapId, true);
    WrappedID3D12DescriptorHeap *destination = (WrappedID3D12DescriptorHeap *)shadowHeap;
    D3D12_CPU_DESCRIPTOR_HANDLE dst = destination->GetCPUDescriptorHandleForHeapStart();
    for(uint32_t i = 0; i < source->GetNumDescriptors(); i++)
    {
      source->GetDescriptors()[i].Create(source->GetDesc().Type, m_pDevice, dst);
      dst.ptr += sizeof(D3D12Descriptor);
    }
    shadowHeaps[sourceHeapId] = shadowHeap;
  }

  for(const auto &target : tableTargets)
  {
    ID3D12DescriptorHeap *shadowHeap = shadowHeaps[target.first.first];
    ID3D12Resource *shadowBuffer = rm->GetResAs<ID3D12Resource>(target.second, true);
    if(shadowHeap == NULL || shadowBuffer == NULL)
      return "A D3D12 event descriptor or buffer shadow disappeared during apply";

    D3D12_CONSTANT_BUFFER_VIEW_DESC cbv = {};
    cbv.BufferLocation = shadowBuffer->GetGPUVirtualAddress();
    cbv.SizeInBytes = (UINT)AlignUp(
        shadowBuffer->GetDesc().Width,
        uint64_t(D3D12_CONSTANT_BUFFER_DATA_PLACEMENT_ALIGNMENT));
    D3D12_CPU_DESCRIPTOR_HANDLE dst = shadowHeap->GetCPUDescriptorHandleForHeapStart();
    dst.ptr += target.first.second * sizeof(D3D12Descriptor);
    m_pDevice->CreateConstantBufferView(&cbv, dst);
  }

  if(m_pDevice->HasFatalError())
    return "Creating the D3D12 event descriptor shadow caused a replay device error";

  m_AppliedEventCBufferOriginalState = state;
  m_EventCBufferStateApplied = true;

  for(const auto &item : pending)
  {
    const EventCBufferBinding &binding = item.second->binding;
    if(binding.kind != EventCBufferBindingKind::RootCBV)
      continue;
    D3D12RenderState::RootSignature &root = binding.compute ? state.compute : state.graphics;
    root.sigelems[binding.rootIndex] = D3D12RenderState::SignatureElement(
        eRootCBV, GetResID(item.second->shadow), 0);
  }

  for(const auto &heap : shadowHeaps)
  {
    const ResourceId shadowId = GetResID(heap.second);
    bool heapBound = false;
    for(ResourceId &boundHeap : state.heaps)
    {
      if(boundHeap == heap.first)
      {
        boundHeap = shadowId;
        heapBound = true;
      }
    }
    if(!heapBound)
    {
      RestoreEventCBufferOverrides();
      return "The source CBV descriptor heap is not bound in the live D3D12 state";
    }

    for(D3D12RenderState::SignatureElement &element : state.graphics.sigelems)
      if(element.type == eRootTable && element.id == heap.first)
        element.id = shadowId;
    for(D3D12RenderState::SignatureElement &element : state.compute.sigelems)
      if(element.type == eRootTable && element.id == heap.first)
        element.id = shadowId;
  }

  return {};
}

void D3D12Replay::RestoreEventCBufferOverrides()
{
  if(!m_EventCBufferStateApplied || m_pDevice == NULL || m_pDevice->GetQueue() == NULL)
    return;

  m_pDevice->GetQueue()->GetCommandData()->m_RenderState = m_AppliedEventCBufferOriginalState;
  m_AppliedEventCBufferOriginalState = D3D12RenderState();
  m_EventCBufferStateApplied = false;
}

static bool D3D12ReadOnlyPixelWriteFootprint(const ShaderReflection &original,
                                             const ShaderReflection &active)
{
  return original.stage == ShaderStage::Pixel && active.stage == ShaderStage::Pixel &&
         !original.rawBytes.empty() && !active.rawBytes.empty() &&
         (original.encoding == ShaderEncoding::DXBC || original.encoding == ShaderEncoding::DXIL) &&
         (active.encoding == ShaderEncoding::DXBC || active.encoding == ShaderEncoding::DXIL) &&
         original.readWriteResources.empty() && active.readWriteResources.empty();
}

static bool D3D12PixelReadBoundsPreserved(const ShaderReflection &original,
                                          const ShaderReflection &active)
{
  if(!D3D12ReadOnlyPixelWriteFootprint(original, active))
    return false;
  // Loading records whole native CBV declarations, not individual field names or observed
  // dynamic indices. A new PS may read any byte in that old envelope, but may not grow it.
  // SRV tables/root descriptors already retain their complete bound ranges in the read proof.
  for(const ConstantBlock &block : active.constantBlocks)
  {
    if(!block.bufferBacked || block.inlineDataBytes || !block.byteSize || block.byteSize > 65536 ||
       !block.bindArraySize || block.bindArraySize > 65536)
      return false;
    bool covered = false;
    for(const ConstantBlock &captured : original.constantBlocks)
      if(captured.bufferBacked && !captured.inlineDataBytes &&
         captured.fixedBindSetOrSpace == block.fixedBindSetOrSpace &&
         captured.byteSize >= block.byteSize && captured.bindArraySize &&
         block.fixedBindNumber >= captured.fixedBindNumber &&
         uint64_t(block.fixedBindNumber) - captured.fixedBindNumber + block.bindArraySize <=
             captured.bindArraySize)
        covered = true;
    if(!covered)
      return false;
  }
  return true;
}

#if ENABLED(ENABLE_UNIT_TESTS)

#include "3rdparty/catch/catch.hpp"

TEST_CASE("D3D12 immutable-source sharing requires complete byte equality within its budget",
          "[d3d12][replay][initial-source]")
{
  bytebuf a, b;
  a.resize(2 * 1024 * 1024 + 17);
  memset(a.data(), 0, a.size());
  b = a;
  CHECK(D3D12InitialSourceBytesEqual(a.data(), b.data(), a.size(), []() { return true; }));
  // Identical prefix/middle/suffix samples must not hide an interior difference.
  b[12345] = 1;
  CHECK_FALSE(D3D12InitialSourceBytesEqual(a.data(), b.data(), a.size(), []() { return true; }));
  b = a;
  b.back() = 1;
  CHECK_FALSE(D3D12InitialSourceBytesEqual(a.data(), b.data(), a.size(), []() { return true; }));
  b = a;
  size_t chunks = 0;
  CHECK_FALSE(
      D3D12InitialSourceBytesEqual(a.data(), b.data(), a.size(), [&]() { return ++chunks < 3; }));
  CHECK(chunks == 3);
  alignas(16) byte alignedA[64] = {}, alignedB[64] = {};
  CHECK(D3D12InitialUploadByteCompare(alignedA, alignedB, sizeof(alignedA)));
  CHECK(D3D12InitialUploadByteCompare(alignedA + 1, alignedB + 1, 31));
  alignedB[30] = 1;
  CHECK_FALSE(D3D12InitialUploadByteCompare(alignedA, alignedB, 31));
  CHECK_FALSE(D3D12InitialUploadByteCompare(alignedA + 1, alignedB + 1, 31));
  CHECK(D3D12InitialUploadByteCompare(alignedA, alignedB, 16));
}

TEST_CASE("D3D12 read-only pixel replacements exclude unclassified write footprints",
          "[d3d12][replay][shader-dirty]")
{
  ShaderReflection original, active;
  original.stage = active.stage = ShaderStage::Pixel;
  original.encoding = ShaderEncoding::DXIL;
  active.encoding = ShaderEncoding::DXBC;
  CHECK_FALSE(D3D12ReadOnlyPixelWriteFootprint(original, active));
  original.rawBytes = {1};
  active.rawBytes = {2};
  CHECK(D3D12ReadOnlyPixelWriteFootprint(original, active));
  SECTION("The old Shader could have written UAV contents")
  {
    original.readWriteResources.resize(1);
    CHECK_FALSE(D3D12ReadOnlyPixelWriteFootprint(original, active));
  }
  SECTION("The new Shader introduces writes outside the attachment set")
  {
    active.readWriteResources.resize(1);
    CHECK_FALSE(D3D12ReadOnlyPixelWriteFootprint(original, active));
  }
  SECTION("Compute and unknown encodings retain conservative invalidation")
  {
    active.stage = ShaderStage::Compute;
    CHECK_FALSE(D3D12ReadOnlyPixelWriteFootprint(original, active));
    active.stage = ShaderStage::Pixel;
    active.encoding = ShaderEncoding::SPIRV;
    CHECK_FALSE(D3D12ReadOnlyPixelWriteFootprint(original, active));
  }
}

TEST_CASE("D3D12 pixel replay reuse retains only captured constant-buffer read envelopes",
          "[d3d12][replay][shader-dirty]")
{
  ShaderReflection original, active;
  original.stage = active.stage = ShaderStage::Pixel;
  original.encoding = active.encoding = ShaderEncoding::DXIL;
  original.rawBytes = {1};
  active.rawBytes = {2};
  ConstantBlock block;
  block.bufferBacked = true;
  block.fixedBindNumber = 3;
  block.fixedBindSetOrSpace = 2;
  block.byteSize = 256;
  block.bindArraySize = 2;
  original.constantBlocks = {block};
  active.constantBlocks = {block};
  CHECK(D3D12PixelReadBoundsPreserved(original, active));
  active.constantBlocks[0].byteSize = 128;
  active.constantBlocks[0].fixedBindNumber = 4;
  active.constantBlocks[0].bindArraySize = 1;
  CHECK(D3D12PixelReadBoundsPreserved(original, active));
  SECTION("Growing or relocating reads invalidates captured envelopes")
  {
    active.constantBlocks[0].byteSize = 260;
    CHECK_FALSE(D3D12PixelReadBoundsPreserved(original, active));
    active.constantBlocks[0] = block;
    active.constantBlocks[0].fixedBindSetOrSpace++;
    CHECK_FALSE(D3D12PixelReadBoundsPreserved(original, active));
    active.constantBlocks[0] = block;
    active.constantBlocks[0].fixedBindNumber++;
    CHECK_FALSE(D3D12PixelReadBoundsPreserved(original, active));
  }
  SECTION("Unknown and inline declarations are not buffer proofs")
  {
    active.constantBlocks[0].byteSize = 0;
    CHECK_FALSE(D3D12PixelReadBoundsPreserved(original, active));
    active.constantBlocks[0] = block;
    active.constantBlocks[0].inlineDataBytes = true;
    CHECK_FALSE(D3D12PixelReadBoundsPreserved(original, active));
    active.constantBlocks[0] = block;
    active.constantBlocks[0].bindArraySize = UINT_MAX;
    CHECK_FALSE(D3D12PixelReadBoundsPreserved(original, active));
  }
  SECTION("Removing unused reads does not grow an envelope")
  {
    active.constantBlocks.clear();
    CHECK(D3D12PixelReadBoundsPreserved(original, active));
    active.readWriteResources.resize(1);
    CHECK_FALSE(D3D12PixelReadBoundsPreserved(original, active));
  }
}

TEST_CASE("D3D12 first-write source warmup never removes unknown sources",
          "[d3d12][replay][initial-source-warmup]")
{
  const ResourceId late = ResourceIDGen::GetNewUniqueID();
  const ResourceId early = ResourceIDGen::GetNewUniqueID();
  const ResourceId unknown = ResourceIDGen::GetNewUniqueID();
  D3D12InitialSourceWarmupCandidate a, b, c, duplicate;
  a.source = late;
  a.firstObservedWrite = 300;
  a.sourceBufferBytes = 100;
  b.source = early;
  b.firstObservedWrite = 40;
  b.sourceBufferBytes = 200;
  c.source = unknown;
  c.sourceBufferBytes = 300;
  duplicate = a;
  duplicate.firstObservedWrite = 5;
  const rdcarray<D3D12InitialSourceWarmupCandidate> input = {a, c, b, duplicate};
  const auto legacy = OrderInitialSourceWarmup(input, false);
  REQUIRE(legacy.size() == 4);
  for(size_t i = 0; i < input.size(); i++)
  {
    CHECK(legacy[i].source == input[i].source);
    CHECK(legacy[i].firstObservedWrite == input[i].firstObservedWrite);
  }
  const auto ordered = OrderInitialSourceWarmup(input, true);
  REQUIRE(ordered.size() == 3);
  CHECK(ordered[0].source == late);
  CHECK(ordered[0].firstObservedWrite == 5);
  CHECK(ordered[0].sourceBufferBytes == 100);
  CHECK(ordered[1].source == early);
  CHECK(ordered[2].source == unknown);
  CHECK(ordered[2].firstObservedWrite == ~0U);
  // Sorting the private plan cannot alter the shared initial-content application sequence.
  CHECK(input[0].firstObservedWrite == 300);
  CHECK(input[1].source == unknown);
}

TEST_CASE("D3D12 source warmup uses only positive write-use evidence and deterministic ties",
          "[d3d12][replay][initial-source-warmup]")
{
  CHECK(InitialSourceWarmupWriteUsage(ResourceUsage::CPUWrite));
  CHECK(InitialSourceWarmupWriteUsage(ResourceUsage::Clear));
  CHECK(InitialSourceWarmupWriteUsage(ResourceUsage::CS_RWResource));
  CHECK(InitialSourceWarmupWriteUsage(ResourceUsage::CopyDst));
  CHECK_FALSE(InitialSourceWarmupWriteUsage(ResourceUsage::CopySrc));
  CHECK_FALSE(InitialSourceWarmupWriteUsage(ResourceUsage::PS_Resource));
  CHECK_FALSE(InitialSourceWarmupWriteUsage(ResourceUsage::PS_Constants));
  CHECK_FALSE(InitialSourceWarmupWriteUsage(ResourceUsage::Unused));
  CHECK_FALSE(InitialSourceWarmupWriteUsage(ResourceUsage::Discard));
  std::map<ResourceId, uint32_t> writes;
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  AddInitialSourceWarmupWrite(writes, heap, 80);
  AddInitialSourceWarmupWrite(writes, heap, 20);
  AddInitialSourceWarmupWrite(writes, heap, 120);
  AddInitialSourceWarmupWrite(writes, ResourceId(), 1);
  REQUIRE(writes.size() == 1);
  CHECK(writes[heap] == 20);
  D3D12InitialSourceWarmupCandidate a, b;
  a.source = ResourceIDGen::GetNewUniqueID();
  b.source = ResourceIDGen::GetNewUniqueID();
  a.firstObservedWrite = b.firstObservedWrite = 20;
  const auto forward = OrderInitialSourceWarmup({a, b}, true);
  const auto reverse = OrderInitialSourceWarmup({b, a}, true);
  REQUIRE(forward.size() == 2);
  REQUIRE(reverse.size() == 2);
  CHECK(forward[0].source == reverse[0].source);
  CHECK(forward[1].source == reverse[1].source);
}

TEST_CASE("ReplayLab D3D12 root CBV window is reflection-bounded",
          "[d3d12][replaylab][material]")
{
  CHECK(ReplayLabD3D12RootCBVWindowSize(48, 8ULL * 1024ULL * 1024ULL) == 48);
  CHECK(ReplayLabD3D12RootCBVWindowSize(33, 8ULL * 1024ULL * 1024ULL) == 48);
  CHECK(ReplayLabD3D12RootCBVWindowSize(0, 4096) == 0);
  CHECK(ReplayLabD3D12RootCBVWindowSize(64ULL * 1024ULL + 1, 128ULL * 1024ULL) == 0);
  CHECK(ReplayLabD3D12RootCBVWindowSize(64, 32) == 0);
  CHECK(ReplayLabD3D12StageVisible(ShaderStage::Amplification,
                                   D3D12_SHADER_VISIBILITY_AMPLIFICATION));
  CHECK(ReplayLabD3D12StageVisible(ShaderStage::Mesh, D3D12_SHADER_VISIBILITY_MESH));
}

TEST_CASE("ReplayLab event CBuffer identity includes register space",
          "[d3d12][replaylab][material]")
{
  EventCBufferOverrideKey legacy = {77, ShaderStage::Pixel, 3};
  EventCBufferOverrideKey space0 = {77, ShaderStage::Pixel, 3, 0};
  EventCBufferOverrideKey space7 = {77, ShaderStage::Pixel, 3, 7};

  CHECK(legacy.registerSpace == 0);
  // Force a boolean Catch expression here. Decomposing the custom key
  // comparison would require a production DoStringise overload solely for
  // this unit test.
  CHECK_FALSE((legacy < space0));
  CHECK_FALSE((space0 < legacy));
  CHECK((space0 < space7));

  std::map<EventCBufferOverrideKey, uint32_t> sessions;
  sessions[space0] = 10;
  sessions[space7] = 20;
  CHECK(sessions.size() == 2);
  CHECK(sessions[space0] == 10);
  CHECK(sessions[space7] == 20);
}

#endif    // ENABLED(ENABLE_UNIT_TESTS)

void D3D12Replay::ReplaceResource(ResourceId from, ResourceId to)
{
  if(WrappedID3D12Shader::IsShader(from) && WrappedID3D12Shader::IsShader(to))
  {
    rdcstr error = ReplaceTargetShaderChecked(from, to);
    if(!error.empty())
      RDCERR("D3D12 shader replacement failed closed: %s", error.c_str());
    return;
  }

  // A non-shader replacement cannot affect a derived shader PSO.
  m_pDevice->GetResourceManager()->ReplaceResource(from, to);
  m_pDevice->InvalidatePreparedReplayCache();

  ClearPostVSCache();
  ClearFeedbackCache();
}

void D3D12Replay::RemoveReplacement(ResourceId id)
{
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  if(rm->HasReplacement(id) && WrappedID3D12Shader::IsShader(id))
  {
    rdcstr error = RemoveTargetShaderReplacementChecked(id);
    if(!error.empty())
      RDCERR("D3D12 shader replacement removal failed closed: %s", error.c_str());
    return;
  }

  if(!rm->HasReplacement(id))
    return;

  rm->RemoveReplacement(id);
  m_pDevice->InvalidatePreparedReplayCache();

  ClearPostVSCache();
  ClearFeedbackCache();
}

rdcstr D3D12Replay::ReplaceTargetShaderChecked(ResourceId from, ResourceId to)
{
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  if(!WrappedID3D12Shader::IsShader(from) || !WrappedID3D12Shader::IsShader(to))
    return "Checked D3D12 replacement requires two shader resources";
  if(from == to)
    return "The target shader must differ from the captured original";
  if(rm->GetUnreplacedID(from) != from)
    return "The original shader must be the canonical captured ResourceId";
  if(!rm->HasResource(from) || !rm->HasResource(to))
    return "The original or target shader resource is no longer live";

  const bool hadPrevious = rm->HasReplacement(from);
  ID3D12DeviceChild *capturedResource =
      rm->GetResourceWithoutReplacement(from, true);
  if(capturedResource == NULL || GetResID(capturedResource) != from ||
     !WrappedID3D12Shader::IsShader(GetResID(capturedResource)))
    return "The canonical captured shader object could not be resolved";
  WrappedID3D12Shader *captured = (WrappedID3D12Shader *)capturedResource;

  ID3D12DeviceChild *previousResource = rm->GetResource(from, true);
  if(previousResource == NULL)
    return "The original or active replacement resource could not be resolved";
  ResourceId previousId = GetResID(previousResource);
  if(!WrappedID3D12Shader::IsShader(previousId))
    return "The captured shader is currently mapped to a non-shader resource";
  if(hadPrevious && previousId == from)
    return "The captured shader has an invalid self-replacement mapping";

  // Idempotence must be checked before rejecting a reverse-owned target: the
  // current target is of course already owned by this exact original.
  if(hadPrevious && previousId == to)
    return {};
  if(rm->GetUnreplacedID(to) != to)
    return "The target shader is already owned by another resource replacement";
  if(rm->HasReplacement(to))
    return "The target shader is itself a resource replacement key";

  ID3D12DeviceChild *targetResource = rm->GetResource(to, true);
  if(targetResource == NULL || GetResID(targetResource) != to ||
     !WrappedID3D12Shader::IsShader(GetResID(targetResource)))
    return "The original or target shader object could not be resolved";
  WrappedID3D12Shader *previous = (WrappedID3D12Shader *)previousResource;
  WrappedID3D12Shader *target = (WrappedID3D12Shader *)targetResource;
  if(previous->GetDetails().stage != captured->GetDetails().stage)
    return StringFormat::Fmt(
        "The active replacement stage %s does not match captured stage %s",
        ToStr(previous->GetDetails().stage).c_str(),
        ToStr(captured->GetDetails().stage).c_str());
  if(captured->GetDetails().stage != target->GetDetails().stage)
    return StringFormat::Fmt("Replacement stage %s does not match original stage %s",
                             ToStr(target->GetDetails().stage).c_str(),
                             ToStr(captured->GetDetails().stage).c_str());

  ResourceId restoreId = hadPrevious ? previousId : ResourceId();

  uint32_t slot = ~0U, space = ~0U;
  captured->GetShaderExtSlot(slot, space);
  target->SetShaderExtSlot(slot, space);

  // ResourceManager replacement supersedes are not self-cleaning: remove the previous reverse
  // mapping first, then install the candidate. The old derived PSOs remain mapped until the
  // transactional refresh commits.
  if(hadPrevious)
    rm->RemoveReplacement(from);
  rm->ReplaceResource(from, to);

  rdcstr error = RefreshDerivedReplacementsChecked(from);
  if(!error.empty())
  {
    rm->RemoveReplacement(from);
    if(hadPrevious)
      rm->ReplaceResource(from, restoreId);
    return error;
  }

  ClearPostVSCache();
  ClearFeedbackCache();
  return {};
}

rdcstr D3D12Replay::RemoveTargetShaderReplacementChecked(ResourceId id)
{
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  if(!WrappedID3D12Shader::IsShader(id))
    return "Checked D3D12 replacement removal requires a shader ResourceId";
  if(rm->GetUnreplacedID(id) != id)
    return "The shader to restore must be the canonical captured ResourceId";
  if(!rm->HasReplacement(id))
    return {};

  ID3D12DeviceChild *capturedResource =
      rm->GetResourceWithoutReplacement(id, true);
  if(capturedResource == NULL || GetResID(capturedResource) != id ||
     !WrappedID3D12Shader::IsShader(GetResID(capturedResource)))
    return "The canonical captured shader object could not be resolved";

  ID3D12DeviceChild *previousResource = rm->GetResource(id, true);
  if(previousResource == NULL)
    return "The active target shader object could not be resolved";
  ResourceId previousId = GetResID(previousResource);
  if(!WrappedID3D12Shader::IsShader(previousId))
    return "The captured shader is currently mapped to a non-shader resource";
  if(previousId == id)
    return "The captured shader has an invalid self-replacement mapping";
  WrappedID3D12Shader *captured = (WrappedID3D12Shader *)capturedResource;
  WrappedID3D12Shader *previous = (WrappedID3D12Shader *)previousResource;
  if(previous->GetDetails().stage != captured->GetDetails().stage)
    return "The active replacement stage differs from the canonical captured shader";

  rm->RemoveReplacement(id);
  rdcstr error = RefreshDerivedReplacementsChecked(id);
  if(!error.empty())
  {
    rm->ReplaceResource(id, previousId);
    return error;
  }

  ClearPostVSCache();
  ClearFeedbackCache();
  return {};
}

void D3D12Replay::ClearScrubReplayCache()
{
  // Lightweight scrub used by hot replay paths (for example buffer-patch EID
  // timing) that must keep the initial-state source cache resident.
  ClearPostVSCache();
  ClearFeedbackCache();
}

void D3D12Replay::ReleaseRebuildableReplayResources()
{
  // Drop rebuildable replay-time scratch so a long Host-driven session (many
  // shader edits/observations from the AI workflow) does not accumulate
  // committed memory. Every resource dropped here is a pure optimization the
  // next replay recreates on demand; correctness is unaffected.
  if(!m_InitialStateSourceCache.empty())
  {
    // These cached initial-state copies may still be referenced by queued
    // replay work, so drain it before releasing the application's resources,
    // mirroring Shutdown's ordering.
    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists(true);

    m_InitialStateSourceCacheBytes =
        RetainD3D12CanonicalInitialSources(m_pDevice, m_InitialStateSourceCache);
  }

  m_PatchedPSCache.clear();
}

void D3D12Replay::ClearReplayCache()
{
  ClearScrubReplayCache();
  ReleaseRebuildableReplayResources();
}

void D3D12Replay::ReloadShaderDebugInformation()
{
  DXBC::ResetSearchDirsCache();
  WrappedID3D12Shader::ReloadShaderDebugInformation();
  ClearReplayCache();
}

static bool D3D12RootSignaturesEqual(WrappedID3D12Device *device,
                                     const D3D12RootSignature &a,
                                     const D3D12RootSignature &b)
{
  bytebuf encodedA = EncodeRootSig(device->RootSigVersion(), a);
  bytebuf encodedB = EncodeRootSig(device->RootSigVersion(), b);
  return !encodedA.empty() && encodedA == encodedB;
}

static rdcstr VerifyImplicitD3D12RootSignature(
    WrappedID3D12Device *device, WrappedID3D12PipelineState *captured,
    const D3D12_EXPANDED_PIPELINE_STATE_STREAM_DESC &desc)
{
  if(desc.GetRootSigIfPresent() != NULL ||
     desc.GetRootSigBlob().SerializedBlobSizeInBytes > 0)
    return {};

  const D3D12_SHADER_BYTECODE shaders[] = {
      desc.VS, desc.HS, desc.DS, desc.GS, desc.PS, desc.CS, desc.AS, desc.MS,
  };

  bool found = false;
  for(const D3D12_SHADER_BYTECODE &shader : shaders)
  {
    if(shader.pShaderBytecode == NULL || shader.BytecodeLength == 0 ||
       !DXBC::DXBCContainer::CheckForRootSig(shader.pShaderBytecode, shader.BytecodeLength))
      continue;

    D3D12RootSignature candidate =
        DecodeRootSig(shader.pShaderBytecode, shader.BytecodeLength);
    if(!D3D12RootSignaturesEqual(device, captured->usedSig, candidate))
      return "A replacement shader's embedded root signature differs from the captured PSO";
    found = true;
  }

  if(!found)
    return "The captured PSO used an implicit root signature, but no replacement-stage shader "
           "contains the exact captured signature";

  return {};
}

rdcstr D3D12Replay::SetEventShaderOverride(uint32_t eventId, ShaderStage stage,
                                          ResourceId expectedOriginal, ResourceId replacement,
                                          uint32_t outputTarget, bool independentFloatOutput)
{
  RestoreEventShaderOverrides();
  const ActionDescription *action = m_pDevice->GetAction(eventId);
  if(!action || !(action->flags & (ActionFlags::Drawcall | ActionFlags::Dispatch | ActionFlags::MeshDispatch)))
    return "The event is not a graphics draw or compute dispatch";
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  if(!WrappedID3D12Shader::IsShader(expectedOriginal) || !WrappedID3D12Shader::IsShader(replacement) ||
     rm->GetUnreplacedID(expectedOriginal) != expectedOriginal ||
     rm->GetUnreplacedID(replacement) != replacement)
    return "Event Shader override requires a captured original and an unowned target Shader";
  WrappedID3D12Shader *target = rm->GetResAs<WrappedID3D12Shader>(replacement, true);
  if(!target || target->GetDetails().stage != stage)
    return "The target Shader stage does not match the event override";
  if(outputTarget != ~0U && (stage != ShaderStage::Pixel || outputTarget >= 8))
    return "A blend bypass requires a valid pixel Shader color target";
  if(independentFloatOutput && (stage != ShaderStage::Pixel || outputTarget == ~0U))
    return "Independent float preview requires a pixel Shader color target";
  m_EventShaderOverrides[{eventId, stage}] = {expectedOriginal, replacement, outputTarget,
                                           independentFloatOutput};
  ClearPostVSCache();
  return {};
}

void D3D12Replay::ReleaseEventShaderPipelines(ResourceId target)
{
  if(m_EventShaderPipelines.empty())
    return;
  // PSOs used by an asynchronous OnlyDraw must retire before destruction.
  if(!m_pDevice->DeviceWaitForIdle())
    return;
  for(auto it = m_EventShaderPipelines.begin(); it != m_EventShaderPipelines.end();)
  {
    if(target == ResourceId() || it->first.Uses(target))
    {
      SAFE_RELEASE(it->second);
      it = m_EventShaderPipelines.erase(it);
    }
    else
      ++it;
  }
}

void D3D12Replay::ClearEventShaderOverride(uint32_t eventId, ShaderStage stage)
{
  RestoreEventShaderOverrides();
  m_EventShaderOverrides.erase({eventId, stage});
  ClearPostVSCache();
}

void D3D12Replay::ClearEventShaderOverrides()
{
  RestoreEventShaderOverrides();
  m_EventShaderOverrides.clear();
  ClearPostVSCache();
}

void D3D12Replay::ClearEventShaderOverridesForResource(ResourceId id)
{
  RestoreEventShaderOverrides();
  for(auto it = m_EventShaderOverrides.begin(); it != m_EventShaderOverrides.end();)
    if(it->second.replacement == id)
      it = m_EventShaderOverrides.erase(it);
    else
      ++it;
  ReleaseEventShaderPipelines(id);
}

rdcstr D3D12Replay::ApplyEventShaderOverrides(uint32_t eventId)
{
  if(m_EventShaderStateApplied)
    return "A previous D3D12 event Shader override was not restored";
  bool configured = false;
  for(const auto &item : m_EventShaderOverrides)
    configured |= item.first.first == eventId;
  if(!configured)
    return {};
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  D3D12RenderState &state = m_pDevice->GetQueue()->GetCommandData()->m_RenderState;
  WrappedID3D12PipelineState *pipe = rm->GetResAs<WrappedID3D12PipelineState>(state.pipe, true);
  if(!pipe)
    return "The selected event has no live graphics or compute pipeline";
  EventShaderPipelineKey key;
  key.pipeline = pipe->GetResourceID();
  D3D12_EXPANDED_PIPELINE_STATE_STREAM_DESC desc;
  pipe->Fill(desc);
  desc.CachedPSO = {};
  WrappedID3D12Shader *bound[] = {pipe->VS(), pipe->HS(), pipe->DS(), pipe->GS(), pipe->PS(),
                                  pipe->CS(), pipe->AS(), pipe->MS()};
  D3D12_SHADER_BYTECODE *bytecode[] = {&desc.VS, &desc.HS, &desc.DS, &desc.GS, &desc.PS,
                                      &desc.CS, &desc.AS, &desc.MS};
  for(const auto &item : m_EventShaderOverrides)
  {
    if(item.first.first != eventId)
      continue;
    const uint32_t stage = uint32_t(item.first.second);
    if(stage >= ARRAY_COUNT(bound) || !bound[stage] ||
       rm->GetUnreplacedID(bound[stage]->GetResourceID()) != item.second.original)
      return "The selected event no longer binds the expected Shader";
    WrappedID3D12Shader *target = rm->GetResAs<WrappedID3D12Shader>(item.second.replacement, true);
    if(!target || target->GetResourceID() != item.second.replacement ||
       target->GetDetails().stage != item.first.second)
      return "The event Shader target is no longer available";
    uint32_t slot = ~0U, space = ~0U;
    bound[stage]->GetShaderExtSlot(slot, space);
    target->SetShaderExtSlot(slot, space);
    *bytecode[stage] = target->GetDesc();
    key.replacements[stage] = item.second.replacement;
    if(item.second.outputTarget != ~0U)
      key.outputTarget = item.second.outputTarget;
    key.independentFloatOutput |= item.second.independentFloatOutput;
  }
  if(key.outputTarget != ~0U)
  {
    if(!pipe->IsGraphics() || key.outputTarget >= desc.RTVFormats.NumRenderTargets)
      return "The requested debug color target is not bound by this pipeline";
    if(!desc.BlendState.IndependentBlendEnable)
      for(size_t i = 1; i < ARRAY_COUNT(desc.BlendState.RenderTarget); i++)
        desc.BlendState.RenderTarget[i] = desc.BlendState.RenderTarget[0];
    desc.BlendState.IndependentBlendEnable = TRUE;
    desc.BlendState.AlphaToCoverageEnable = FALSE;
    desc.BlendState.RenderTarget[key.outputTarget].BlendEnable = FALSE;
    desc.BlendState.RenderTarget[key.outputTarget].LogicOpEnable = FALSE;
    desc.BlendState.RenderTarget[key.outputTarget].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    if(key.independentFloatOutput)
      desc.RTVFormats.RTFormats[key.outputTarget] = DXGI_FORMAT_R32G32B32A32_FLOAT;
  }
  auto cached = m_EventShaderPipelines.find(key);
  if(cached == m_EventShaderPipelines.end())
  {
    rdcstr error = VerifyImplicitD3D12RootSignature(m_pDevice, pipe, desc);
    if(!error.empty())
      return error;
    ID3D12PipelineState *variant = NULL;
    HRESULT hr = m_pDevice->CreatePipeState(desc, &variant);
    if(FAILED(hr) || !variant)
      return StringFormat::Fmt("Creating the event Shader pipeline failed: %s", ToStr(hr).c_str());
    if(m_EventShaderPipelines.size() >= 64)
      ReleaseEventShaderPipelines();
    cached = m_EventShaderPipelines.insert({key, variant}).first;
  }
  EventShaderScratch *scratch = NULL;
  if(key.independentFloatOutput)
  {
    rdcstr error = PrepareEventShaderScratch(eventId, key.outputTarget, state, scratch);
    if(!error.empty())
      return error;
  }
  m_pDevice->InvalidateLiveForward("eventShaderOverride");
  m_EventShaderPreviousPipeline = state.pipe;
  if(scratch)
  {
    m_EventShaderPreviousRTs = state.rts;
    m_EventShaderPreviousDSV = state.dsv;
    state.rts = scratch->renderRTs;
    state.dsv = scratch->renderDSV;
    m_EventShaderAttachmentsApplied = true;
  }
  state.pipe = GetResID(cached->second);
  m_EventShaderStateApplied = true;
  return {};
}

void D3D12Replay::RestoreEventShaderOverrides()
{
  if(!m_EventShaderStateApplied)
    return;
  D3D12RenderState &state = m_pDevice->GetQueue()->GetCommandData()->m_RenderState;
  state.pipe = m_EventShaderPreviousPipeline;
  if(m_EventShaderAttachmentsApplied)
  {
    state.rts = m_EventShaderPreviousRTs;
    state.dsv = m_EventShaderPreviousDSV;
    m_EventShaderPreviousRTs.clear();
    m_EventShaderAttachmentsApplied = false;
  }
  m_EventShaderPreviousPipeline = ResourceId();
  m_EventShaderStateApplied = false;
}

void D3D12Replay::ReleaseEventShaderScratch()
{
  if(m_EventShaderScratch.empty() || !m_pDevice->DeviceWaitForIdle())
    return;
  for(auto &item : m_EventShaderScratch)
  {
    for(ID3D12Resource *resource : item.second.colors)
      SAFE_RELEASE(resource);
    SAFE_RELEASE(item.second.depth);
    SAFE_RELEASE(item.second.clearHeap);
  }
  m_EventShaderScratch.clear();
}

TextureDescription D3D12Replay::GetEventShaderPreviewTexture(uint32_t eventId, ShaderStage stage)
{
  auto binding = m_EventShaderOverrides.find({eventId, stage});
  if(binding == m_EventShaderOverrides.end() || !binding->second.independentFloatOutput)
    return {};
  auto scratch = m_EventShaderScratch.find({eventId, binding->second.outputTarget});
  if(scratch == m_EventShaderScratch.end() ||
     binding->second.outputTarget >= scratch->second.colors.size())
    return {};
  return GetTexture(GetResID(scratch->second.colors[binding->second.outputTarget]));
}

rdcstr D3D12Replay::PrepareEventShaderScratch(uint32_t eventId, uint32_t outputTarget,
                                             const D3D12RenderState &state,
                                             EventShaderScratch *&scratch)
{
  scratch = NULL;
  if(state.renderpass)
    return "Independent float preview does not support D3D12 render-pass resolve semantics";
  if(outputTarget >= state.rts.size() ||
     state.rts[outputTarget].GetResResourceId() == ResourceId())
    return "The selected independent preview attachment is not bound";
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  const auto cacheKey = std::make_pair(eventId, outputTarget);
  rdcarray<ResourceId> liveSources;
  for(size_t i = 0; i <= state.rts.size(); i++)
  {
    ResourceId source = i == state.rts.size() ? state.dsv.GetResResourceId() :
                                               state.rts[i].GetResResourceId();
    ID3D12Resource *resource = source == ResourceId() ? NULL :
                               rm->GetResAs<ID3D12Resource>(source, true);
    if(source != ResourceId() && !resource)
      return "An independent preview attachment is unavailable";
    liveSources.push_back(GetResID(resource));
  }
  auto found = m_EventShaderScratch.find(cacheKey);
  bool matches = found != m_EventShaderScratch.end();
  auto sameView = [](const D3D12Descriptor &a, const D3D12Descriptor &b, bool depth) {
    if(a.GetResResourceId() != b.GetResResourceId())
      return false;
    // An unbound DSV is deliberately Undefined. ContentsEqual rejects it and
    // compares unused union bytes for other views, which is appropriate for
    // descriptor write elision but not for scratch attachment compatibility.
    if(a.GetResResourceId() == ResourceId())
      return true;
    if(depth)
    {
      const D3D12_DEPTH_STENCIL_VIEW_DESC &x = a.GetDSV(), &y = b.GetDSV();
      if(x.Format != y.Format || x.Flags != y.Flags || x.ViewDimension != y.ViewDimension)
        return false;
      switch(x.ViewDimension)
      {
        case D3D12_DSV_DIMENSION_TEXTURE2D: return x.Texture2D.MipSlice == y.Texture2D.MipSlice;
        case D3D12_DSV_DIMENSION_TEXTURE2DARRAY:
          return x.Texture2DArray.MipSlice == y.Texture2DArray.MipSlice &&
                 x.Texture2DArray.FirstArraySlice == y.Texture2DArray.FirstArraySlice &&
                 x.Texture2DArray.ArraySize == y.Texture2DArray.ArraySize;
        case D3D12_DSV_DIMENSION_TEXTURE2DMS: return true;
        case D3D12_DSV_DIMENSION_TEXTURE2DMSARRAY:
          return x.Texture2DMSArray.FirstArraySlice == y.Texture2DMSArray.FirstArraySlice &&
                 x.Texture2DMSArray.ArraySize == y.Texture2DMSArray.ArraySize;
        default: return false;
      }
    }
    const D3D12_RENDER_TARGET_VIEW_DESC &x = a.GetRTV(), &y = b.GetRTV();
    if(x.Format != y.Format || x.ViewDimension != y.ViewDimension)
      return false;
    switch(x.ViewDimension)
    {
      case D3D12_RTV_DIMENSION_TEXTURE2D:
        return x.Texture2D.MipSlice == y.Texture2D.MipSlice &&
               x.Texture2D.PlaneSlice == y.Texture2D.PlaneSlice;
      case D3D12_RTV_DIMENSION_TEXTURE2DARRAY:
        return x.Texture2DArray.MipSlice == y.Texture2DArray.MipSlice &&
               x.Texture2DArray.PlaneSlice == y.Texture2DArray.PlaneSlice &&
               x.Texture2DArray.FirstArraySlice == y.Texture2DArray.FirstArraySlice &&
               x.Texture2DArray.ArraySize == y.Texture2DArray.ArraySize;
      case D3D12_RTV_DIMENSION_TEXTURE2DMS: return true;
      case D3D12_RTV_DIMENSION_TEXTURE2DMSARRAY:
        return x.Texture2DMSArray.FirstArraySlice == y.Texture2DMSArray.FirstArraySlice &&
               x.Texture2DMSArray.ArraySize == y.Texture2DMSArray.ArraySize;
      default: return false;
    }
  };
  if(matches)
  {
    matches = found->second.sourceRTs.size() == state.rts.size() &&
              sameView(found->second.sourceDSV, state.dsv, true) &&
              found->second.liveSources == liveSources;
    for(size_t i = 0; matches && i < state.rts.size(); i++)
      matches = sameView(found->second.sourceRTs[i], state.rts[i], false);
  }
  auto destroy = [](EventShaderScratch &entry) {
    for(ID3D12Resource *resource : entry.colors)
      SAFE_RELEASE(resource);
    SAFE_RELEASE(entry.depth);
    SAFE_RELEASE(entry.clearHeap);
  };
  if(!matches && found != m_EventShaderScratch.end())
  {
    if(!m_pDevice->DeviceWaitForIdle())
      return "The previous float preview GPU work did not complete";
    destroy(found->second);
    m_EventShaderScratch.erase(found);
    found = m_EventShaderScratch.end();
  }
  if(!matches)
  {
    EventShaderScratch entry;
    entry.sourceRTs = state.rts;
    entry.liveSources = liveSources;
    entry.sourceDSV = state.dsv;
    entry.renderRTs = state.rts;
    entry.renderDSV = state.dsv;
    entry.colors.resize(state.rts.size());
    rdcarray<D3D12_RESOURCE_DESC> descriptions;
    descriptions.resize(state.rts.size() + 1);
    D3D12_RENDER_TARGET_VIEW_DESC selectedView = state.rts[outputTarget].GetRTV();
    for(size_t i = 0; i < descriptions.size(); i++)
    {
      const bool depth = i == state.rts.size();
      ResourceId source = depth ? state.dsv.GetResResourceId() : state.rts[i].GetResResourceId();
      if(source == ResourceId())
        continue;
      ID3D12Resource *resource = rm->GetResAs<ID3D12Resource>(source, true);
      if(!resource)
        return "An independent preview attachment is unavailable";
      D3D12_RESOURCE_DESC desc = resource->GetDesc();
      if(desc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D)
        return "Independent float preview requires 2D or 2D-array attachments";
      D3D12_HEAP_PROPERTIES originalHeap = {};
      D3D12_HEAP_FLAGS originalFlags = D3D12_HEAP_FLAG_NONE;
      if(FAILED(resource->GetHeapProperties(&originalHeap, &originalFlags)))
        return "Independent float preview does not support sparse attachments";
      desc.Alignment = 0;
      desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
      desc.Flags = depth ? D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL :
                           D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
      if(i == outputTarget)
      {
        UINT mip = 0, layers = 1;
        switch(selectedView.ViewDimension)
        {
          case D3D12_RTV_DIMENSION_TEXTURE2D:
            mip = selectedView.Texture2D.MipSlice;
            selectedView.Texture2D.MipSlice = 0;
            selectedView.Texture2D.PlaneSlice = 0;
            break;
          case D3D12_RTV_DIMENSION_TEXTURE2DARRAY:
            mip = selectedView.Texture2DArray.MipSlice;
            layers = selectedView.Texture2DArray.ArraySize;
            selectedView.Texture2DArray.MipSlice = 0;
            selectedView.Texture2DArray.FirstArraySlice = 0;
            selectedView.Texture2DArray.PlaneSlice = 0;
            break;
          case D3D12_RTV_DIMENSION_TEXTURE2DMS: break;
          case D3D12_RTV_DIMENSION_TEXTURE2DMSARRAY:
            layers = selectedView.Texture2DMSArray.ArraySize;
            selectedView.Texture2DMSArray.FirstArraySlice = 0;
            break;
          default: return "The independent float preview attachment view is unsupported";
        }
        if(mip >= desc.MipLevels || layers == 0 || layers > desc.DepthOrArraySize)
          return "The independent float preview attachment has an invalid subresource range";
        desc.Width = RDCMAX(1ULL, desc.Width >> mip);
        desc.Height = RDCMAX(1U, desc.Height >> mip);
        desc.MipLevels = 1;
        desc.DepthOrArraySize = UINT16(layers);
        desc.Format = selectedView.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
        D3D12_FEATURE_DATA_FORMAT_SUPPORT support = {};
        support.Format = desc.Format;
        if(FAILED(m_pDevice->CheckFeatureSupport(D3D12_FEATURE_FORMAT_SUPPORT, &support,
                                                 sizeof(support))) ||
           !(support.Support1 & D3D12_FORMAT_SUPPORT1_RENDER_TARGET) ||
           !(support.Support1 & D3D12_FORMAT_SUPPORT1_SHADER_LOAD))
          return "This device cannot render and inspect an RGBA32F preview";
        if(desc.SampleDesc.Count > 1)
        {
          D3D12_FEATURE_DATA_MULTISAMPLE_QUALITY_LEVELS samples = {};
          samples.Format = desc.Format;
          samples.SampleCount = desc.SampleDesc.Count;
          if(FAILED(m_pDevice->CheckFeatureSupport(D3D12_FEATURE_MULTISAMPLE_QUALITY_LEVELS,
                                                   &samples, sizeof(samples))) ||
             samples.NumQualityLevels == 0 || desc.SampleDesc.Quality >= samples.NumQualityLevels)
            return "RGBA32F does not support the captured multisample count and quality";
        }
      }
      const D3D12_RESOURCE_ALLOCATION_INFO allocation =
          m_pDevice->GetResourceAllocationInfo(0, 1, &desc);
      const uint64_t budget = 512ULL * 1024 * 1024;
      if(allocation.SizeInBytes > budget || entry.bytes > budget - allocation.SizeInBytes)
        return "Independent float preview attachments exceed the 512 MiB scratch budget";
      entry.bytes += allocation.SizeInBytes;
      descriptions[i] = desc;
    }
    uint64_t total = entry.bytes;
    for(const auto &item : m_EventShaderScratch)
      total += item.second.bytes;
    while(m_EventShaderScratch.size() >= 8 || total > 512ULL * 1024 * 1024)
    {
      auto oldest = m_EventShaderScratch.end();
      for(auto it = m_EventShaderScratch.begin(); it != m_EventShaderScratch.end(); ++it)
      {
        // A different event may have a user-owned override. Preserve its preview resource.
        auto active = m_EventShaderOverrides.find({it->first.first, ShaderStage::Pixel});
        if(active != m_EventShaderOverrides.end() && active->second.independentFloatOutput)
          continue;
        if(oldest == m_EventShaderScratch.end() || it->second.used < oldest->second.used)
          oldest = it;
      }
      if(oldest == m_EventShaderScratch.end() || !m_pDevice->DeviceWaitForIdle())
        return "Independent float preview cache is full of active or unfinished GPU resources";
      total -= oldest->second.bytes;
      destroy(oldest->second);
      m_EventShaderScratch.erase(oldest);
    }
    D3D12_HEAP_PROPERTIES heap = {};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    for(size_t i = 0; i < descriptions.size(); i++)
    {
      if(descriptions[i].Width == 0)
        continue;
      const bool depth = i == state.rts.size();
      ID3D12Resource **destination = depth ? &entry.depth : &entry.colors[i];
      HRESULT hr = m_pDevice->CreateCommittedResource(
          &heap, D3D12_HEAP_FLAG_NONE, &descriptions[i],
          depth ? D3D12_RESOURCE_STATE_DEPTH_WRITE : D3D12_RESOURCE_STATE_RENDER_TARGET, NULL,
          __uuidof(ID3D12Resource), (void **)destination);
      if(FAILED(hr) || !*destination)
      {
        destroy(entry);
        return StringFormat::Fmt("Allocating independent float preview failed: %s", ToStr(hr).c_str());
      }
      (*destination)->SetName(L"Bread independent Shader preview");
      if(depth)
        entry.renderDSV.Init(*destination, &state.dsv.GetDSV());
      else
        entry.renderRTs[i].Init(*destination,
                               i == outputTarget ? &selectedView : &state.rts[i].GetRTV());
    }
    D3D12_DESCRIPTOR_HEAP_DESC heapDesc = {};
    heapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    heapDesc.NumDescriptors = 1;
    HRESULT hr = m_pDevice->CreateDescriptorHeap(&heapDesc, __uuidof(ID3D12DescriptorHeap),
                                                 (void **)&entry.clearHeap);
    if(FAILED(hr) || !entry.clearHeap)
    {
      destroy(entry);
      return "Allocating the independent float preview RTV failed";
    }
    m_pDevice->CreateRenderTargetView(entry.colors[outputTarget], &selectedView,
                                    entry.clearHeap->GetCPUDescriptorHandleForHeapStart());
    found = m_EventShaderScratch.insert({cacheKey, std::move(entry)}).first;
  }
  EventShaderScratch &entry = found->second;
  // Do not put an unclosed copy list in the device's pending submission queue.
  ID3D12GraphicsCommandListX *list = m_pDevice->StealNewList();
  if(!list)
    return "Creating the independent float preview copy list failed";
  for(size_t i = 0; i <= state.rts.size(); i++)
  {
    if(i == outputTarget)
      continue;
    const bool depth = i == state.rts.size();
    ID3D12Resource *destination = depth ? entry.depth : entry.colors[i];
    if(!destination)
      continue;
    ResourceId sourceId = depth ? state.dsv.GetResResourceId() : state.rts[i].GetResResourceId();
    ID3D12Resource *source = rm->GetResAs<ID3D12Resource>(sourceId, true);
    BarrierSet sourceBarriers, destinationBarriers;
    sourceBarriers.Configure(source, m_pDevice->GetSubresourceStates(GetResID(source)),
                             BarrierSet::CopySourceAccess);
    destinationBarriers.Configure(destination,
                                  m_pDevice->GetSubresourceStates(GetResID(destination)),
                                  BarrierSet::CopyDestAccess);
    sourceBarriers.Apply(list);
    destinationBarriers.Apply(list);
    list->CopyResource(destination, source);
    destinationBarriers.Unapply(list);
    sourceBarriers.Unapply(list);
  }
  const FLOAT clear[] = {0.0f, 0.0f, 0.0f, 0.0f};
  list->ClearRenderTargetView(entry.clearHeap->GetCPUDescriptorHandleForHeapStart(), clear, 0, NULL);
  HRESULT hr = list->Close();
  if(FAILED(hr))
  {
    list->Release();
    return "Closing the independent float preview copy list failed";
  }
  m_pDevice->ExecuteList(list);
  entry.used = ++m_EventShaderScratchClock;
  scratch = &entry;
  return {};
}

rdcstr D3D12Replay::RefreshDerivedReplacementsChecked(ResourceId requiredShader)
{
  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();
  bool readOnlyPixelShader = false;
  bool readBoundsPreserved = false;
  if(requiredShader != ResourceId() && WrappedID3D12Shader::IsShader(requiredShader) &&
     Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_READ_ONLY_SHADER_REPLAY_REUSE") != "1")
  {
    ID3D12DeviceChild *original = rm->GetResourceWithoutReplacement(requiredShader, true);
    ID3D12DeviceChild *active = rm->GetResource(requiredShader, true);
    if(original && active && WrappedID3D12Shader::IsShader(GetResID(active)))
    {
      readOnlyPixelShader =
          D3D12ReadOnlyPixelWriteFootprint(((WrappedID3D12Shader *)original)->GetDetails(),
                                           ((WrappedID3D12Shader *)active)->GetDetails());
      readBoundsPreserved =
          D3D12PixelReadBoundsPreserved(((WrappedID3D12Shader *)original)->GetDetails(),
                                        ((WrappedID3D12Shader *)active)->GetDetails());
    }
  }

  struct PendingPipeline
  {
    ResourceId original;
    ID3D12PipelineState *previous = NULL;
    ID3D12PipelineState *replacement = NULL;
    bool hadPrevious = false;
  };

  rdcarray<WrappedID3D12PipelineState *> capturedPipelines;
  for(WrappedID3D12PipelineState *pipe : m_pDevice->GetPipelineList())
    if(!ResourceIDGen::IsReplayOnlyID(pipe->GetResourceID()))
      capturedPipelines.push_back(pipe);

  rdcarray<PendingPipeline> pending;
  uint32_t requiredUses = 0;

  for(WrappedID3D12PipelineState *pipe : capturedPipelines)
  {
    ResourceId pipesrcid = pipe->GetResourceID();
    bool usesReplacedShader = false;
    bool usesRequiredShader = false;

    if(pipe->IsGraphics())
    {
      WrappedID3D12Shader *shaders[] = {
          pipe->VS(), pipe->HS(), pipe->DS(), pipe->GS(), pipe->PS(), pipe->AS(), pipe->MS(),
      };
      for(WrappedID3D12Shader *shader : shaders)
      {
        if(shader == NULL)
          continue;
        ResourceId shaderId = shader->GetResourceID();
        usesReplacedShader |= rm->HasReplacement(shaderId);
        usesRequiredShader |= (shaderId == requiredShader);
      }
    }
    else
    {
      WrappedID3D12Shader *shader = pipe->CS();
      if(shader != NULL)
      {
        ResourceId shaderId = shader->GetResourceID();
        usesReplacedShader = rm->HasReplacement(shaderId);
        usesRequiredShader = (shaderId == requiredShader);
      }
    }

    if(usesRequiredShader)
    {
      requiredUses++;
      // Direct heap indexing can conceal UAV writes from ordinary reflection. Implicit/unknown
      // signatures and other stages also retain the existing permanent fallback. Fixed RT/DS
      // bindings are already included in the captured command's possible-write upper bound.
      ID3D12RootSignature *nativeRoot =
          pipe->IsGraphics() ? pipe->graphics->GetRootSigIfPresent() : NULL;
      WrappedID3D12RootSignature *root =
          nativeRoot ? rm->GetResAs<WrappedID3D12RootSignature>(GetResID(nativeRoot)) : NULL;
      if(!root || (root->sig.Flags & (D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED |
                                      D3D12_ROOT_SIGNATURE_FLAG_SAMPLER_HEAP_DIRECTLY_INDEXED)))
        readOnlyPixelShader = false;
    }

    PendingPipeline item;
    item.original = pipesrcid;
    item.hadPrevious = rm->HasReplacement(pipesrcid);
    if(item.hadPrevious)
    {
      ID3D12DeviceChild *previousResource = rm->GetResource(pipesrcid, true);
      ResourceId previousId =
          previousResource ? GetResID(previousResource) : ResourceId();
      if(previousId == pipesrcid || rm->GetUnreplacedID(previousId) != pipesrcid)
      {
        for(PendingPipeline &created : pending)
          SAFE_RELEASE(created.replacement);
        return StringFormat::Fmt(
            "Captured PSO %s has an invalid or multiply-owned replacement target",
            ToStr(pipesrcid).c_str());
      }
      for(WrappedID3D12PipelineState *candidate : m_pDevice->GetPipelineList())
      {
        if(candidate->GetResourceID() == previousId)
        {
          item.previous = candidate;
          break;
        }
      }
      if(item.previous == NULL)
      {
        for(PendingPipeline &created : pending)
          SAFE_RELEASE(created.replacement);
        return StringFormat::Fmt(
            "Captured PSO %s is mapped to a missing or non-pipeline resource",
            ToStr(pipesrcid).c_str());
      }
    }

    // If no shader replacement remains, committing this item simply removes the old derived PSO.
    if(!usesReplacedShader)
    {
      if(item.hadPrevious)
        pending.push_back(item);
      continue;
    }

    D3D12_EXPANDED_PIPELINE_STATE_STREAM_DESC desc =
        pipe->IsGraphics() ? *pipe->graphics : *pipe->compute;
    desc.CachedPSO = {NULL, 0};

    if(usesReplacedShader)
    {
      if(pipe->IsGraphics())
      {
        D3D12_SHADER_BYTECODE *shaders[] = {
            &desc.VS, &desc.HS, &desc.DS, &desc.GS, &desc.PS, &desc.AS, &desc.MS,
        };

        for(size_t s = 0; s < ARRAY_COUNT(shaders); s++)
        {
          if(shaders[s]->BytecodeLength > 0)
          {
            WrappedID3D12Shader *stage = (WrappedID3D12Shader *)shaders[s]->pShaderBytecode;

            // this will pick up any replacements
            ID3D12DeviceChild *stageResource =
                rm->GetResource(stage->GetResourceID(), true);
            ResourceId stageId = stageResource ? GetResID(stageResource) : ResourceId();
            if(stageResource == NULL || !WrappedID3D12Shader::IsShader(stageId))
            {
              for(PendingPipeline &created : pending)
                SAFE_RELEASE(created.replacement);
              return StringFormat::Fmt(
                  "A replacement stage for PSO %s is missing or mapped to a non-shader resource",
                  ToStr(pipesrcid).c_str());
            }
            stage = (WrappedID3D12Shader *)stageResource;

            *shaders[s] = stage->GetDesc();
          }
        }
      }
      else
      {
        WrappedID3D12Shader *stage = pipe->CS();

        // this will pick up any replacements
        ID3D12DeviceChild *stageResource =
            stage ? rm->GetResource(stage->GetResourceID(), true) : NULL;
        ResourceId stageId = stageResource ? GetResID(stageResource) : ResourceId();
        if(stageResource == NULL || !WrappedID3D12Shader::IsShader(stageId))
        {
          for(PendingPipeline &created : pending)
            SAFE_RELEASE(created.replacement);
          return StringFormat::Fmt(
              "A replacement compute stage for PSO %s is missing or mapped to a non-shader "
              "resource",
              ToStr(pipesrcid).c_str());
        }
        stage = (WrappedID3D12Shader *)stageResource;

        desc.CS = stage->GetDesc();
      }

      rdcstr rootError = VerifyImplicitD3D12RootSignature(m_pDevice, pipe, desc);
      if(!rootError.empty())
      {
        for(PendingPipeline &created : pending)
          SAFE_RELEASE(created.replacement);
        return StringFormat::Fmt("PSO %s: %s", ToStr(pipesrcid).c_str(), rootError.c_str());
      }

      HRESULT hr = m_pDevice->CreatePipeState(desc, &item.replacement);
      if(FAILED(hr) || item.replacement == NULL)
      {
        SAFE_RELEASE(item.replacement);
        for(PendingPipeline &created : pending)
          SAFE_RELEASE(created.replacement);
        return StringFormat::Fmt("Rebuilding D3D12 PSO %s failed: %s", ToStr(pipesrcid).c_str(),
                                 ToStr(hr).c_str());
      }

      pending.push_back(item);
    }
  }

  if(requiredShader != ResourceId() && requiredUses == 0)
  {
    for(PendingPipeline &created : pending)
      SAFE_RELEASE(created.replacement);
    return "The captured shader is not referenced by any captured graphics/compute PSO";
  }

  // Nothing above changed a mapping. Commit only after every required PSO and root signature has
  // succeeded, then retire old derived PSOs after the device is idle.
  if(!m_pDevice->DeviceWaitForIdle())
  {
    for(PendingPipeline &created : pending)
      SAFE_RELEASE(created.replacement);
    return "GPU completion could not be confirmed before installing replacement D3D12 pipelines";
  }
  m_pDevice->InvalidatePreparedReplayCache(readOnlyPixelShader,
                                           readOnlyPixelShader && readBoundsPreserved);

  if(m_pDevice->HasFatalError())
  {
    for(PendingPipeline &created : pending)
      SAFE_RELEASE(created.replacement);
    return "D3D12 pipeline cache retirement failed before replacement mappings could be installed";
  }

  for(PendingPipeline &item : pending)
  {
    if(item.hadPrevious)
      rm->RemoveReplacement(item.original);
    if(item.replacement != NULL)
      rm->ReplaceResource(item.original, GetResID(item.replacement));
  }

  for(PendingPipeline &item : pending)
  {
    SAFE_RELEASE(item.previous);
  }

  return {};
}

void D3D12Replay::GetTextureData(ResourceId tex, const Subresource &sub,
                                 const GetTextureDataParams &params, bytebuf &data)
{
  bool wasms = false;
  bool resolve = params.resolve;

  if(!m_pDevice->ReplayWorkWaitForIdle())
  {
    data.clear();
    return;
  }

  ID3D12Resource *resource = NULL;

  {
    auto it = m_pDevice->GetResourceList().find(tex);
    if(it != m_pDevice->GetResourceList().end())
      resource = it->second;
  }

  if(resource == NULL)
  {
    RDCERR("Trying to get texture data for unknown ID %s!", ToStr(tex).c_str());
    return;
  }

  if(resource->GetDesc().Dimension == D3D12_RESOURCE_DIMENSION_BUFFER)
  {
    RDCERR("Getting texture data for buffer %s!", ToStr(tex).c_str());
    return;
  }

  D3D12MarkerRegion region(m_pDevice->GetQueue(),
                           StringFormat::Fmt("GetTextureData(%u, %u, %u, remap=%d)", sub.mip,
                                             sub.slice, sub.sample, params.remap));

  Subresource s = sub;

  HRESULT hr = S_OK;

  D3D12_RESOURCE_DESC resDesc = resource->GetDesc();

  s.mip = RDCMIN(uint32_t(resDesc.MipLevels - 1), s.mip);
  s.sample = RDCMIN(resDesc.SampleDesc.Count - 1, s.sample);
  if(resDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D)
    s.slice = 0;
  else
    s.slice = RDCMIN(uint32_t(resDesc.DepthOrArraySize - 1), s.slice);

  D3D12_RESOURCE_DESC copyDesc = resDesc;
  copyDesc.Alignment = 0;
  copyDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
  copyDesc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;

  D3D12_HEAP_PROPERTIES defaultHeap;
  defaultHeap.Type = D3D12_HEAP_TYPE_DEFAULT;
  defaultHeap.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
  defaultHeap.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
  defaultHeap.CreationNodeMask = 1;
  defaultHeap.VisibleNodeMask = 1;

  bool isDepth = IsDepthFormat(resDesc.Format) ||
                 (resDesc.Flags & D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL) != 0;
  bool isStencil = IsDepthAndStencilFormat(resDesc.Format);

  UINT sampleCount = copyDesc.SampleDesc.Count;

  if(copyDesc.SampleDesc.Count > 1)
  {
    // make image n-array instead of n-samples
    copyDesc.DepthOrArraySize *= (UINT16)copyDesc.SampleDesc.Count;
    copyDesc.SampleDesc.Count = 1;
    copyDesc.SampleDesc.Quality = 0;
    copyDesc.MipLevels = 1;

    wasms = true;
  }

  if(wasms && (isDepth || isStencil))
    resolve = false;

  // don't resolve integer textures.
  if(resolve && (IsIntFormat(resDesc.Format) || IsUIntFormat(resDesc.Format)))
  {
    resolve = false;
    s.sample = 0;
  }

  uint32_t slice3DCopy = 0;

  // arrayIdx isn't used for anything except copying the slice out at the end, so save the index we
  // want to copy and then set arrayIdx to 0 to simplify subresource calculations
  if(copyDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D)
  {
    slice3DCopy = s.slice;
    s.slice = 0;
  }

  ID3D12Resource *srcTexture = resource;
  ID3D12Resource *tmpTexture = NULL;

  ID3D12GraphicsCommandListX *list = NULL;

  if(params.remap != RemapTexture::NoRemap)
  {
    if(params.remap == RemapTexture::RGBA8)
    {
      copyDesc.Format = GetTypedFormat(DXGI_FORMAT_R8G8B8A8_TYPELESS, BaseRemapType(params));
      if(IsSRGBFormat(copyDesc.Format) && params.typeCast == CompType::Typeless)
        copyDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
    }
    else if(params.remap == RemapTexture::RGBA16)
    {
      copyDesc.Format = GetTypedFormat(DXGI_FORMAT_R16G16B16A16_TYPELESS, BaseRemapType(params));
    }
    else if(params.remap == RemapTexture::RGBA32)
    {
      copyDesc.Format = GetTypedFormat(DXGI_FORMAT_R32G32B32A32_TYPELESS, BaseRemapType(params));
    }

    // force to 1 mip
    copyDesc.MipLevels = 1;
    copyDesc.Flags |= D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;

    // Keep 3D texture depth, set array size to 1.
    if(copyDesc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE3D)
      copyDesc.DepthOrArraySize = 1;

    SetOutputDimensions(uint32_t(copyDesc.Width), copyDesc.Height);
    m_OutputViewport = {0, 0, (float)copyDesc.Width, (float)copyDesc.Height, 0.0f, 1.0f};

    copyDesc.Width = RDCMAX(1ULL, copyDesc.Width >> s.mip);
    copyDesc.Height = RDCMAX(1U, copyDesc.Height >> s.mip);

    ID3D12Resource *remapTexture;
    hr = m_pDevice->CreateCommittedResource(&defaultHeap, D3D12_HEAP_FLAG_NONE, &copyDesc,
                                            D3D12_RESOURCE_STATE_RENDER_TARGET, NULL,
                                            __uuidof(ID3D12Resource), (void **)&remapTexture);
    if(FAILED(hr))
    {
      RDCERR("Couldn't create remap texture: %s", ToStr(hr).c_str());
      return;
    }

    TexDisplayFlags flags =
        IsSRGBFormat(copyDesc.Format) ? eTexDisplay_None : eTexDisplay_LinearRender;

    if(GetTypelessFormat(copyDesc.Format) == DXGI_FORMAT_R16G16B16A16_TYPELESS)
      flags = eTexDisplay_16Render;
    else if(GetTypelessFormat(copyDesc.Format) == DXGI_FORMAT_R32G32B32A32_TYPELESS)
      flags = eTexDisplay_32Render;

    if(IsUIntFormat(copyDesc.Format))
      flags = TexDisplayFlags(flags | eTexDisplay_RemapUInt);
    else if(IsIntFormat(copyDesc.Format))
      flags = TexDisplayFlags(flags | eTexDisplay_RemapSInt);
    else
      flags = TexDisplayFlags(flags | eTexDisplay_RemapFloat);

    uint32_t loopCount = 1;

    D3D12_RENDER_TARGET_VIEW_DESC rtvDesc = {};
    rtvDesc.Format = copyDesc.Format;
    if(copyDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D)
    {
      rtvDesc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE1D;
    }
    else if(copyDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D)
    {
      rtvDesc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
    }
    else if(copyDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D)
    {
      rtvDesc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE3D;
      rtvDesc.Texture3D.WSize = 1;
      loopCount = RDCMAX(0U, uint32_t(copyDesc.DepthOrArraySize >> s.mip));
    }

    // we only loop for 3D slices, other types we just do one remap for the desired mip/slice
    for(uint32_t loop = 0; loop < loopCount; loop++)
    {
      if(copyDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D)
        rtvDesc.Texture3D.FirstWSlice = loop;

      m_pDevice->CreateRenderTargetView(remapTexture, &rtvDesc,
                                        GetDebugManager()->GetCPUHandle(GET_TEX_RTV));

      TextureDisplay texDisplay;

      texDisplay.red = texDisplay.green = texDisplay.blue = texDisplay.alpha = true;
      texDisplay.hdrMultiplier = -1.0f;
      texDisplay.linearDisplayAsGamma = false;
      texDisplay.overlay = DebugOverlay::NoOverlay;
      texDisplay.flipY = false;
      texDisplay.subresource.mip = s.mip;
      texDisplay.subresource.slice = s.slice;
      if(copyDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D)
        texDisplay.subresource.slice = loop;
      texDisplay.subresource.sample = resolve ? ~0U : s.sample;
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

      RenderTextureInternal(GetDebugManager()->GetCPUHandle(GET_TEX_RTV), texDisplay, flags);
    }

    tmpTexture = srcTexture = remapTexture;

    list = m_pDevice->GetNewList();
    if(!list)
      return;

    D3D12_RESOURCE_BARRIER b = {};
    b.Transition.pResource = remapTexture;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
    b.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    list->ResourceBarrier(1, &b);

    // these have already been selected, don't need to fetch that subresource
    // when copying back to readback buffer
    s.slice = 0;
    s.mip = 0;

    // no longer depth, if it was
    isDepth = false;
    isStencil = false;
  }
  else if(wasms && resolve)
  {
    // force to 1 array slice, 1 mip
    copyDesc.DepthOrArraySize = 1;
    copyDesc.MipLevels = 1;

    copyDesc.Width = RDCMAX(1ULL, copyDesc.Width >> s.mip);
    copyDesc.Height = RDCMAX(1U, copyDesc.Height >> s.mip);

    ID3D12Resource *resolveTexture;
    hr = m_pDevice->CreateCommittedResource(&defaultHeap, D3D12_HEAP_FLAG_NONE, &copyDesc,
                                            D3D12_RESOURCE_STATE_RESOLVE_DEST, NULL,
                                            __uuidof(ID3D12Resource), (void **)&resolveTexture);
    if(FAILED(hr))
    {
      RDCERR("Couldn't create resolve texture: %s", ToStr(hr).c_str());
      return;
    }

    RDCASSERT(!isDepth && !isStencil);

    list = m_pDevice->GetNewList();
    if(!list)
      return;

    // put source texture into resolve source state
    BarrierSet barriers;
    barriers.Configure(resource, m_pDevice->GetSubresourceStates(tex),
                       BarrierSet::ResolveSourceAccess);

    barriers.Apply(list);

    list->ResolveSubresource(resolveTexture, 0, srcTexture,
                             s.slice * resDesc.DepthOrArraySize + s.mip, resDesc.Format);

    barriers.Unapply(list);

    D3D12_RESOURCE_BARRIER b = {};
    b.Transition.pResource = resolveTexture;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = D3D12_RESOURCE_STATE_RESOLVE_DEST;
    b.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    list->ResourceBarrier(1, &b);

    tmpTexture = srcTexture = resolveTexture;

    // these have already been selected, don't need to fetch that subresource
    // when copying back to readback buffer
    s.slice = 0;
    s.mip = 0;
  }
  else if(wasms)
  {
    // copy/expand multisampled live texture to array readback texture
    if(isDepth)
      copyDesc.Flags |= D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL;
    else
      copyDesc.Flags |= D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    copyDesc.Format = GetTypelessFormat(copyDesc.Format);

    ID3D12Resource *arrayTexture;
    hr = m_pDevice->CreateCommittedResource(
        &defaultHeap, D3D12_HEAP_FLAG_NONE, &copyDesc,
        isDepth ? D3D12_RESOURCE_STATE_DEPTH_WRITE : D3D12_RESOURCE_STATE_RENDER_TARGET, NULL,
        __uuidof(ID3D12Resource), (void **)&arrayTexture);
    if(FAILED(hr))
    {
      RDCERR("Couldn't create array texture: %s", ToStr(hr).c_str());
      return;
    }

    list = m_pDevice->GetNewList();
    if(!list)
      return;

    // put source texture into shader read state
    BarrierSet barriers;
    barriers.Configure(resource, m_pDevice->GetSubresourceStates(tex), BarrierSet::SRVAccess);

    barriers.Apply(list);

    list->Close();
    list = NULL;

    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists();

    // expand multisamples out to array
    GetDebugManager()->CopyTex2DMSToArray(NULL, Unwrap(arrayTexture), Unwrap(srcTexture));

    tmpTexture = srcTexture = arrayTexture;

    list = m_pDevice->GetNewList();
    if(!list)
      return;

    barriers.Unapply(list);

    D3D12_RESOURCE_BARRIER b = {};
    b.Transition.pResource = arrayTexture;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore =
        isDepth ? D3D12_RESOURCE_STATE_DEPTH_WRITE : D3D12_RESOURCE_STATE_RENDER_TARGET;
    b.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    list->ResourceBarrier(1, &b);

    s.slice = s.slice * sampleCount + s.sample;
    s.sample = 0;
  }

  if(list == NULL)
    list = m_pDevice->GetNewList();
  if(!list)
    return;

  BarrierSet barriers;

  // if we have no tmpImage, we're copying directly from the real image
  if(tmpTexture == NULL)
  {
    barriers.Configure(resource, m_pDevice->GetSubresourceStates(tex), BarrierSet::CopySourceAccess);

    barriers.Apply(list);
  }

  D3D12_FEATURE_DATA_FORMAT_INFO formatInfo = {};
  formatInfo.Format = copyDesc.Format;
  m_pDevice->CheckFeatureSupport(D3D12_FEATURE_FORMAT_INFO, &formatInfo, sizeof(formatInfo));

  UINT planes = RDCMAX((UINT8)1, formatInfo.PlaneCount);

  UINT numSubresources = copyDesc.MipLevels;
  if(copyDesc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE3D)
    numSubresources *= copyDesc.DepthOrArraySize;

  numSubresources *= planes;

  D3D12_RESOURCE_DESC readbackDesc;
  readbackDesc.Alignment = 0;
  readbackDesc.DepthOrArraySize = 1;
  readbackDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
  readbackDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
  readbackDesc.Format = DXGI_FORMAT_UNKNOWN;
  readbackDesc.Height = 1;
  readbackDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
  readbackDesc.MipLevels = 1;
  readbackDesc.SampleDesc.Count = 1;
  readbackDesc.SampleDesc.Quality = 0;
  readbackDesc.Width = 0;

  // we only actually want to copy the specified array index/mip.
  // But we do need to copy all planes
  D3D12_PLACED_SUBRESOURCE_FOOTPRINT *layouts = new D3D12_PLACED_SUBRESOURCE_FOOTPRINT[planes];
  UINT *rowcounts = new UINT[planes];

  UINT arrayStride = copyDesc.MipLevels;
  UINT planeStride = copyDesc.DepthOrArraySize * copyDesc.MipLevels;

  for(UINT p = 0; p < planes; p++)
  {
    readbackDesc.Width = AlignUp(readbackDesc.Width, 512ULL);

    UINT64 subSize = 0;
    m_pDevice->GetCopyableFootprints(&copyDesc, s.mip + s.slice * arrayStride + p * planeStride, 1,
                                     readbackDesc.Width, layouts + p, rowcounts + p, NULL, &subSize);
    readbackDesc.Width += subSize;
  }

  UINT rowcount = rowcounts[0];

  D3D12_HEAP_PROPERTIES heapProps;
  heapProps.Type = D3D12_HEAP_TYPE_READBACK;
  heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
  heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
  heapProps.CreationNodeMask = 1;
  heapProps.VisibleNodeMask = 1;

  ID3D12Resource *readbackBuf = NULL;
  hr = m_pDevice->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &readbackDesc,
                                          D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                                          __uuidof(ID3D12Resource), (void **)&readbackBuf);
  if(FAILED(hr))
  {
    RDCERR("Couldn't create readback buffer: %s", ToStr(hr).c_str());
    return;
  }

  for(UINT p = 0; p < planes; p++)
  {
    D3D12_TEXTURE_COPY_LOCATION dst, src;

    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    src.pResource = srcTexture;
    src.SubresourceIndex = s.mip + s.slice * arrayStride + p * planeStride;

    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.pResource = readbackBuf;
    dst.PlacedFootprint = layouts[p];

    list->CopyTextureRegion(&dst, 0, 0, 0, &src, NULL);
  }

  // if we have no tmpImage, we're copying directly from the real image
  if(tmpTexture == NULL)
    barriers.Unapply(list);

  list->Close();

  m_pDevice->ExecuteLists();
  m_pDevice->FlushLists();

  // map the buffer and copy to return buffer
  byte *pData = NULL;
  hr = readbackBuf->Map(0, NULL, (void **)&pData);
  CHECK_HR(m_pDevice, hr);
  if(FAILED(hr))
  {
    RDCERR("Couldn't map readback buffer: %s", ToStr(hr).c_str());
    readbackBuf->Unmap(0, NULL);
    return;
  }
  RDCASSERTEQUAL(hr, S_OK);

  RDCASSERT(pData != NULL);

  data.resize(GetByteSize(layouts[0].Footprint.Width, layouts[0].Footprint.Height,
                          layouts[0].Footprint.Depth, copyDesc.Format, 0));

  // for depth-stencil need to merge the planes pixel-wise
  if(isDepth && isStencil)
  {
    UINT dstRowPitch = GetByteSize(layouts[0].Footprint.Width, 1, 1, copyDesc.Format, 0);

    if(copyDesc.Format == DXGI_FORMAT_D32_FLOAT_S8X24_UINT ||
       copyDesc.Format == DXGI_FORMAT_R32_FLOAT_X8X24_TYPELESS ||
       copyDesc.Format == DXGI_FORMAT_R32G8X24_TYPELESS)
    {
      for(UINT z = 0; z < layouts[0].Footprint.Depth; z++)
      {
        for(UINT y = 0; y < layouts[0].Footprint.Height; y++)
        {
          UINT row = y + z * layouts[0].Footprint.Height;

          uint32_t *dSrc = (uint32_t *)(pData + layouts[0].Footprint.RowPitch * row);
          uint8_t *sSrc =
              (uint8_t *)(pData + layouts[1].Offset + layouts[1].Footprint.RowPitch * row);

          uint32_t *dDst = (uint32_t *)(data.data() + dstRowPitch * row);
          uint32_t *sDst = dDst + 1;    // interleaved, next pixel

          for(UINT i = 0; i < layouts[0].Footprint.Width; i++)
          {
            *dDst = *dSrc;
            *sDst = *sSrc;

            // increment source pointers by 1 since they're separate, and dest pointers by 2 since
            // they're interleaved
            dDst += 2;
            sDst += 2;

            sSrc++;
            dSrc++;
          }
        }
      }
    }
    else    // D24_S8
    {
      for(UINT z = 0; z < layouts[0].Footprint.Depth; z++)
      {
        for(UINT y = 0; y < rowcount; y++)
        {
          UINT row = y + z * rowcount;

          // we can copy the depth from D24 as a 32-bit integer, since the remaining bits are
          // garbage
          // and we overwrite them with stencil
          uint32_t *dSrc = (uint32_t *)(pData + layouts[0].Footprint.RowPitch * row);
          uint8_t *sSrc =
              (uint8_t *)(pData + layouts[1].Offset + layouts[1].Footprint.RowPitch * row);

          uint32_t *dst = (uint32_t *)(data.data() + dstRowPitch * row);

          for(UINT i = 0; i < layouts[0].Footprint.Width; i++)
          {
            // pack the data together again, stencil in top bits
            *dst = (*dSrc & 0x00ffffff) | (uint32_t(*sSrc) << 24);

            dst++;
            sSrc++;
            dSrc++;
          }
        }
      }
    }
  }
  else
  {
    UINT dstRowPitch = GetByteSize(layouts[0].Footprint.Width, 1, 1, copyDesc.Format, 0);

    // copy row by row
    for(UINT z = 0; z < layouts[0].Footprint.Depth; z++)
    {
      for(UINT y = 0; y < rowcount; y++)
      {
        UINT row = y + z * rowcount;

        byte *src = pData + layouts[0].Footprint.RowPitch * row;
        byte *dst = data.data() + dstRowPitch * row;

        memcpy(dst, src, dstRowPitch);
      }
    }
  }

  SAFE_DELETE_ARRAY(layouts);
  SAFE_DELETE_ARRAY(rowcounts);

  D3D12_RANGE range = {0, 0};
  readbackBuf->Unmap(0, &range);

  // clean up temporary objects
  SAFE_RELEASE(readbackBuf);
  SAFE_RELEASE(tmpTexture);
}

rdcarray<ShaderSourcePrefix> D3D12Replay::GetCustomShaderSourcePrefixes()
{
  return {
      {ShaderEncoding::HLSL, HLSL_CUSTOM_PREFIX},
      {ShaderEncoding::Slang, HLSL_CUSTOM_PREFIX},
  };
}

void D3D12Replay::SetCustomShaderIncludes(const rdcarray<rdcstr> &directories)
{
  m_CustomShaderIncludes = directories;
}

void D3D12Replay::BuildCustomShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                                    const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                                    ShaderStage type, ResourceId &id, rdcstr &errors)
{
  BuildShader(sourceEncoding, source, entry, compileFlags, m_CustomShaderIncludes, type, id, errors);
}

ResourceId D3D12Replay::ApplyCustomShader(TextureDisplay &display)
{
  m_pDevice->InvalidateLiveForward("customShaderAnalysis");
  ID3D12Resource *resource = NULL;

  {
    auto it = m_pDevice->GetResourceList().find(display.resourceId);
    if(it != m_pDevice->GetResourceList().end())
      resource = it->second;
  }

  if(resource == NULL)
    return ResourceId();

  D3D12_RESOURCE_DESC resDesc = resource->GetDesc();

  resDesc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
  resDesc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
  resDesc.Alignment = 0;
  resDesc.DepthOrArraySize = 1;
  resDesc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
  resDesc.MipLevels = (UINT16)CalcNumMips((int)resDesc.Width, (int)resDesc.Height, 1);
  resDesc.SampleDesc.Count = 1;
  resDesc.SampleDesc.Quality = 0;
  resDesc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;

  D3D12_RESOURCE_DESC customTexDesc = {};

  if(m_CustomShaderTex)
    customTexDesc = m_CustomShaderTex->GetDesc();

  if(customTexDesc.Width != resDesc.Width || customTexDesc.Height != resDesc.Height)
  {
    SAFE_RELEASE(m_CustomShaderTex);

    D3D12_HEAP_PROPERTIES heapProps;
    heapProps.Type = D3D12_HEAP_TYPE_DEFAULT;
    heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    heapProps.CreationNodeMask = 1;
    heapProps.VisibleNodeMask = 1;

    HRESULT hr = m_pDevice->CreateCommittedResource(
        &heapProps, D3D12_HEAP_FLAG_NONE, &resDesc, D3D12_RESOURCE_STATE_RENDER_TARGET, NULL,
        __uuidof(ID3D12Resource), (void **)&m_CustomShaderTex);
    if(FAILED(hr))
    {
      RDCERR("Couldn't create custom shader texture: %s", ToStr(hr).c_str());
      return ResourceId();
    }

    if(m_CustomShaderTex)
    {
      m_CustomShaderTex->SetName(L"m_CustomShaderTex");
      m_CustomShaderResourceId = GetResID(m_CustomShaderTex);
    }
    else
    {
      m_CustomShaderResourceId = ResourceId();
    }
  }

  if(m_CustomShaderResourceId == ResourceId())
    return m_CustomShaderResourceId;

  D3D12_RENDER_TARGET_VIEW_DESC rtvDesc = {};
  rtvDesc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
  rtvDesc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
  rtvDesc.Texture2D.MipSlice = display.subresource.mip;

  m_pDevice->CreateRenderTargetView(m_CustomShaderTex, &rtvDesc,
                                    GetDebugManager()->GetCPUHandle(CUSTOM_SHADER_RTV));

  ID3D12GraphicsCommandList *list = m_pDevice->GetNewList();
  if(!list)
    return ResourceId();

  float clr[] = {0.0f, 0.0f, 0.0f, 0.0f};
  list->ClearRenderTargetView(GetDebugManager()->GetCPUHandle(CUSTOM_SHADER_RTV), clr, 0, NULL);

  list->Close();

  TextureDisplay disp;
  disp.red = disp.green = disp.blue = disp.alpha = true;
  disp.flipY = false;
  disp.xOffset = 0.0f;
  disp.yOffset = 0.0f;
  disp.customShaderId = display.customShaderId;
  disp.resourceId = display.resourceId;
  disp.typeCast = display.typeCast;
  disp.hdrMultiplier = -1.0f;
  disp.linearDisplayAsGamma = false;
  disp.subresource = display.subresource;
  disp.overlay = DebugOverlay::NoOverlay;
  disp.rangeMin = 0.0f;
  disp.rangeMax = 1.0f;
  disp.rawOutput = false;
  disp.scale = 1.0f;

  SetOutputDimensions(RDCMAX(1U, (UINT)resDesc.Width), RDCMAX(1U, resDesc.Height));

  m_OutputViewport = {
      0,
      0,
      (float)RDCMAX(1ULL, resDesc.Width >> display.subresource.mip),
      (float)RDCMAX(1U, resDesc.Height >> display.subresource.mip),
      0.0f,
      1.0f,
  };
  RenderTextureInternal(GetDebugManager()->GetCPUHandle(CUSTOM_SHADER_RTV), disp,
                        eTexDisplay_BlendAlpha);

  return m_CustomShaderResourceId;
}

#pragma region proxy resources

ResourceId D3D12Replay::CreateProxyTexture(const TextureDescription &templateTex)
{
  // ReplayController's persistent event snapshots deliberately use this small common subset. Keep
  // the proxy implementation equally strict instead of silently changing unsupported topology.
  if(templateTex.dimension != 2 || templateTex.type != TextureType::Texture2D ||
     templateTex.depth != 1 || templateTex.arraysize != 1 || templateTex.mips != 1 ||
     templateTex.msSamp != 1 || templateTex.width == 0 || templateTex.height == 0)
  {
    RDCERR("Unsupported D3D12 proxy texture description");
    return ResourceId();
  }

  const DXGI_FORMAT format = MakeDXGIFormat(templateTex.format);
  if(format == DXGI_FORMAT_UNKNOWN)
  {
    RDCERR("Unsupported D3D12 proxy texture format %s", templateTex.format.Name().c_str());
    return ResourceId();
  }

  D3D12_HEAP_PROPERTIES heapProps = {};
  heapProps.Type = D3D12_HEAP_TYPE_DEFAULT;
  heapProps.CreationNodeMask = 1;
  heapProps.VisibleNodeMask = 1;

  D3D12_RESOURCE_DESC desc = {};
  desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
  desc.Width = templateTex.width;
  desc.Height = templateTex.height;
  desc.DepthOrArraySize = 1;
  desc.MipLevels = 1;
  desc.Format = format;
  desc.SampleDesc.Count = 1;
  desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;

  ID3D12Resource *proxy = NULL;
  HRESULT hr = m_pDevice->CreateCommittedResource(
      &heapProps, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE, NULL,
      __uuidof(ID3D12Resource), (void **)&proxy);
  if(FAILED(hr) || proxy == NULL)
  {
    RDCERR("Couldn't create D3D12 proxy texture: %s", ToStr(hr).c_str());
    return ResourceId();
  }

  proxy->SetName(L"RenderDoc persistent event texture snapshot");
  m_ProxyResources.push_back(proxy);
  return GetResID(proxy);
}

ResourceId D3D12Replay::CreateProxyTextureSnapshot(const TextureDescription &templateTex)
{
  ResourceId snapshot = CreateProxyTexture(templateTex);
  if(snapshot != ResourceId())
    m_TextureSnapshots.push_back(snapshot);
  return snapshot;
}

ResourceId D3D12Replay::CreateTextureSnapshot(ResourceId source, const Subresource &sourceSub,
                                              CompType typeCast, Subresource &snapshotSub)
{
  (void)typeCast;

  auto sourceIt = m_pDevice->GetResourceList().find(source);
  if(sourceIt == m_pDevice->GetResourceList().end() || sourceIt->second == NULL)
    return ResourceId();

  ID3D12Resource *sourceResource = sourceIt->second;
  const D3D12_RESOURCE_DESC sourceDesc = sourceResource->GetDesc();
  if(sourceDesc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER || sourceDesc.MipLevels == 0 ||
     sourceDesc.Width == 0 || sourceDesc.Height == 0 || sourceDesc.SampleDesc.Count != 1 ||
     (sourceDesc.Flags & D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE))
    return ResourceId();

  // Reserved (sparse) resources don't expose heap properties and require tile-by-tile residency
  // handling. Keep them on the portable fallback instead of taking a potentially incomplete copy.
  D3D12_HEAP_PROPERTIES sourceHeap = {};
  D3D12_HEAP_FLAGS sourceHeapFlags = D3D12_HEAP_FLAG_NONE;
  if(FAILED(sourceResource->GetHeapProperties(&sourceHeap, &sourceHeapFlags)))
    return ResourceId();

  D3D12_FEATURE_DATA_FORMAT_INFO formatInfo = {};
  formatInfo.Format = sourceDesc.Format;
  if(FAILED(m_pDevice->CheckFeatureSupport(D3D12_FEATURE_FORMAT_INFO, &formatInfo,
                                            sizeof(formatInfo))) ||
     formatInfo.PlaneCount != 1)
    return ResourceId();

  const UINT mip = RDCMIN(sourceSub.mip, UINT(sourceDesc.MipLevels - 1));
  const bool is3D = sourceDesc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D;
  const UINT arraySize = is3D ? 1U : UINT(sourceDesc.DepthOrArraySize);
  const UINT slice = is3D ? 0U : RDCMIN(sourceSub.slice, arraySize - 1);

  D3D12_RESOURCE_DESC snapshotDesc = sourceDesc;
  snapshotDesc.Alignment = 0;
  snapshotDesc.Width = RDCMAX(1ULL, sourceDesc.Width >> mip);
  snapshotDesc.Height = RDCMAX(1U, sourceDesc.Height >> mip);
  snapshotDesc.DepthOrArraySize =
      is3D ? UINT16(RDCMAX(1U, UINT(sourceDesc.DepthOrArraySize) >> mip)) : 1;
  snapshotDesc.MipLevels = 1;
  snapshotDesc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;

  D3D12_HEAP_PROPERTIES heapProps = {};
  heapProps.Type = D3D12_HEAP_TYPE_DEFAULT;
  heapProps.CreationNodeMask = 1;
  heapProps.VisibleNodeMask = 1;

  ID3D12Resource *snapshot = NULL;
  HRESULT hr = m_pDevice->CreateCommittedResource(
      &heapProps, D3D12_HEAP_FLAG_NONE, &snapshotDesc, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
      __uuidof(ID3D12Resource), (void **)&snapshot);
  if(FAILED(hr) || snapshot == NULL)
    return ResourceId();

  ID3D12GraphicsCommandListX *list = m_pDevice->GetNewList();
  if(list == NULL)
  {
    snapshot->Release();
    return ResourceId();
  }

  BarrierSet sourceBarriers;
  sourceBarriers.Configure(sourceResource, m_pDevice->GetSubresourceStates(source),
                           BarrierSet::CopySourceAccess);
  sourceBarriers.Apply(list);

  D3D12_TEXTURE_COPY_LOCATION dst = {};
  dst.pResource = snapshot;
  dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
  dst.SubresourceIndex = 0;

  D3D12_TEXTURE_COPY_LOCATION src = {};
  src.pResource = sourceResource;
  src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
  src.SubresourceIndex =
      D3D12CalcSubresource(mip, slice, 0, sourceDesc.MipLevels, arraySize);

  list->CopyTextureRegion(&dst, 0, 0, 0, &src, NULL);
  sourceBarriers.Unapply(list);

  D3D12_RESOURCE_BARRIER ready = {};
  ready.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
  ready.Transition.pResource = snapshot;
  ready.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
  ready.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
  ready.Transition.StateAfter = D3D12_RESOURCE_STATE_ALL_SHADER_RESOURCE;
  list->ResourceBarrier(1, &ready);
  list->Close();

  m_pDevice->ExecuteLists();

  const ResourceId snapshotId = GetResID(snapshot);
  SubresourceStateVector &snapshotStates = m_pDevice->GetSubresourceStates(snapshotId);
  for(D3D12ResourceLayout &state : snapshotStates)
    state = D3D12ResourceLayout::FromStates(D3D12_RESOURCE_STATE_ALL_SHADER_RESOURCE);

  snapshot->SetName(L"RenderDoc GPU event texture snapshot");
  m_ProxyResources.push_back(snapshot);
  m_TextureSnapshots.push_back(snapshotId);

  snapshotSub = Subresource();
  if(is3D)
    snapshotSub.slice =
        RDCMIN(sourceSub.slice, UINT(snapshotDesc.DepthOrArraySize) - 1);
  return snapshotId;
}

void D3D12Replay::FreeTextureSnapshot(ResourceId snapshot)
{
  rdcarray<ResourceId> snapshots;
  snapshots.push_back(snapshot);
  FreeTextureSnapshots(snapshots);
}

void D3D12Replay::FreeTextureSnapshots(const rdcarray<ResourceId> &snapshots)
{
  bool ownedSnapshot = false;
  for(ResourceId snapshot : snapshots)
  {
    if(m_TextureSnapshots.contains(snapshot))
    {
      ownedSnapshot = true;
      break;
    }
  }

  if(!ownedSnapshot)
    return;

  // D3D12 requires the application to keep resources alive while submitted command lists may
  // reference them. Batch all LRU victims behind one synchronization point.
  m_pDevice->ExecuteLists();
  m_pDevice->FlushLists(true);

  for(ResourceId snapshot : snapshots)
  {
    for(size_t snapshotIndex = 0; snapshotIndex < m_TextureSnapshots.size(); snapshotIndex++)
    {
      if(m_TextureSnapshots[snapshotIndex] != snapshot)
        continue;

      for(size_t resourceIndex = 0; resourceIndex < m_ProxyResources.size(); resourceIndex++)
      {
        if(GetResID(m_ProxyResources[resourceIndex]) == snapshot)
        {
          ID3D12Resource *resource = m_ProxyResources[resourceIndex];
          m_ProxyResources.erase(resourceIndex);
          resource->Release();
          break;
        }
      }

      m_TextureSnapshots.erase(snapshotIndex);
      break;
    }
  }
}

void D3D12Replay::SetProxyTextureData(ResourceId texid, const Subresource &sub, byte *data,
                                      size_t dataSize)
{
  m_pDevice->InvalidateLiveForward("proxyTextureWrite");
  auto it = m_pDevice->GetResourceList().find(texid);
  if(it == m_pDevice->GetResourceList().end() || it->second == NULL || data == NULL ||
     sub != Subresource())
  {
    RDCERR("Invalid texture or subresource passed to D3D12 SetProxyTextureData");
    return;
  }

  ID3D12Resource *proxy = it->second;
  bool ownedProxy = false;
  for(ID3D12Resource *resource : m_ProxyResources)
    ownedProxy |= resource == proxy;
  if(!ownedProxy)
  {
    RDCERR("Non-proxy texture passed to D3D12 SetProxyTextureData");
    return;
  }

  const D3D12_RESOURCE_DESC texDesc = proxy->GetDesc();
  if(texDesc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D || texDesc.MipLevels != 1 ||
     texDesc.DepthOrArraySize != 1 || texDesc.SampleDesc.Count != 1)
  {
    RDCERR("Unsupported D3D12 proxy texture topology");
    return;
  }

  D3D12_PLACED_SUBRESOURCE_FOOTPRINT layout = {};
  UINT numRows = 0;
  UINT64 rowSize = 0;
  UINT64 uploadSize = 0;
  m_pDevice->GetCopyableFootprints(&texDesc, 0, 1, 0, &layout, &numRows, &rowSize, &uploadSize);

  const UINT64 requiredDataSize = rowSize * UINT64(numRows) * layout.Footprint.Depth;
  if(dataSize < requiredDataSize || uploadSize == 0)
  {
    RDCERR("Insufficient data provided to D3D12 SetProxyTextureData");
    return;
  }

  D3D12_HEAP_PROPERTIES uploadHeap = {};
  uploadHeap.Type = D3D12_HEAP_TYPE_UPLOAD;
  uploadHeap.CreationNodeMask = 1;
  uploadHeap.VisibleNodeMask = 1;

  D3D12_RESOURCE_DESC uploadDesc = {};
  uploadDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
  uploadDesc.Width = uploadSize;
  uploadDesc.Height = 1;
  uploadDesc.DepthOrArraySize = 1;
  uploadDesc.MipLevels = 1;
  uploadDesc.SampleDesc.Count = 1;
  uploadDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;

  ID3D12Resource *upload = NULL;
  HRESULT hr = m_pDevice->CreateCommittedResource(
      &uploadHeap, D3D12_HEAP_FLAG_NONE, &uploadDesc, D3D12_RESOURCE_STATE_GENERIC_READ, NULL,
      __uuidof(ID3D12Resource), (void **)&upload);
  if(FAILED(hr) || upload == NULL)
  {
    RDCERR("Couldn't create D3D12 proxy upload buffer: %s", ToStr(hr).c_str());
    return;
  }

  byte *mapped = NULL;
  const D3D12_RANGE noRead = {0, 0};
  hr = upload->Map(0, &noRead, (void **)&mapped);
  if(FAILED(hr) || mapped == NULL)
  {
    RDCERR("Couldn't map D3D12 proxy upload buffer: %s", ToStr(hr).c_str());
    SAFE_RELEASE(upload);
    return;
  }

  const byte *src = data;
  for(UINT z = 0; z < layout.Footprint.Depth; z++)
  {
    byte *dstSlice = mapped + layout.Offset + UINT64(z) * layout.Footprint.RowPitch * numRows;
    for(UINT row = 0; row < numRows; row++)
    {
      memcpy(dstSlice + UINT64(row) * layout.Footprint.RowPitch, src, (size_t)rowSize);
      src += rowSize;
    }
  }

  const D3D12_RANGE written = {SIZE_T(layout.Offset), SIZE_T(uploadSize)};
  upload->Unmap(0, &written);

  ID3D12GraphicsCommandListX *list = m_pDevice->GetNewList();
  if(list == NULL)
  {
    SAFE_RELEASE(upload);
    return;
  }

  BarrierSet barriers;
  barriers.Configure(proxy, m_pDevice->GetSubresourceStates(texid), BarrierSet::CopyDestAccess);
  barriers.Apply(list);

  D3D12_TEXTURE_COPY_LOCATION dst = {};
  dst.pResource = proxy;
  dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
  dst.SubresourceIndex = 0;

  D3D12_TEXTURE_COPY_LOCATION srcLocation = {};
  srcLocation.pResource = upload;
  srcLocation.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
  srcLocation.PlacedFootprint = layout;

  list->CopyTextureRegion(&dst, 0, 0, 0, &srcLocation, NULL);
  barriers.Unapply(list);
  list->Close();

  m_pDevice->ExecuteLists();
  m_pDevice->FlushLists();
  SAFE_RELEASE(upload);
}

#pragma endregion

#pragma region not yet implemented

ResourceId D3D12Replay::CreateProxyBuffer(const BufferDescription &templateBuf)
{
  return ResourceId();
}

void D3D12Replay::SetProxyBufferData(ResourceId bufid, byte *data, size_t dataSize)
{
  m_pDevice->InvalidateLiveForward("proxyBufferWrite");
}

#pragma endregion

RDResult D3D12_CreateReplayDevice(RDCFile *rdc, const ReplayOptions &opts, IReplayDriver **driver)
{
  RDCDEBUG("Creating a D3D12 replay device");

  WrappedIDXGISwapChain4::RegisterD3DDeviceCallback(GetD3D12DeviceIfAlloc);

  // this needs to be static, because we don't unload d3d12.dll so the plain LoadLibraryA will
  // succeed on subsequent capture loads.
  static bool d3d12on7 = false;

  D3D12Lib = LoadLibraryA("d3d12.dll");
  if(D3D12Lib == NULL)
  {
    // if it fails try to find D3D12On7 DLLs
    d3d12on7 = true;

    // if it fails, try in the plugin directory
    D3D12Lib = (HMODULE)Process::LoadModule(LocatePluginFile("d3d12", "d3d12.dll"));

    // if that succeeded, also load dxilconv7.dll from there
    if(D3D12Lib)
    {
      HMODULE dxilconv = (HMODULE)Process::LoadModule(LocatePluginFile("d3d12", "dxilconv7.dll"));

      if(!dxilconv)
      {
        RETURN_ERROR_RESULT(ResultCode::APIInitFailed,
                            "Found d3d12.dll in plugin path, but couldn't load dxilconv7.dll");
      }
    }
    else
    {
      // if it failed, try one more time in MS's subfolder convention
      D3D12Lib = LoadLibraryA("12on7/d3d12.dll");

      if(D3D12Lib)
      {
        RDCWARN(
            "Loaded d3d12.dll from 12on7 subfolder."
            "Please use RenderDoc's plugins/d3d12/ subfolder instead");
      }
      else
      {
        if(rdc)
          RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Failed to load d3d12.dll");
        else
          RETURN_WARNING_RESULT(ResultCode::APIInitFailed, "Failed to load d3d12.dll");
      }
    }
  }

  PFN_D3D12_CREATE_DEVICE createDevicePtr =
      (PFN_D3D12_CREATE_DEVICE)GetProcAddress(D3D12Lib, "D3D12CreateDevice");

  typedef HRESULT(WINAPI * PFN_D3D12_ENABLE_EXPERIMENTAL_FEATURES)(
      UINT NumFeatures, const IID *pIIDs, void *pConfigurationStructs,
      UINT *pConfigurationStructSizes);

  PFN_D3D12_ENABLE_EXPERIMENTAL_FEATURES enableExperimentalPtr =
      (PFN_D3D12_ENABLE_EXPERIMENTAL_FEATURES)GetProcAddress(D3D12Lib,
                                                             "D3D12EnableExperimentalFeatures");

  RealD3D12CreateFunction createDevice = createDevicePtr;

  HMODULE dxgilib = LoadLibraryA("dxgi.dll");
  if(dxgilib == NULL)
  {
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Failed to load dxgi.dll");
  }

  if(GetD3DCompiler() == NULL)
  {
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Failed to load d3dcompiler_??.dll");
  }

  D3D12InitParams initParams;
  bytebuf D3D12Core, D3D12SDKLayers;

  uint64_t ver = D3D12InitParams::CurrentVersion;

  // if we have an RDCFile, open the frame capture section and serialise the init params.
  // if not, we're creating a proxy-capable device so use default-initialised init params.
  if(rdc)
  {
    int sectionIdx = rdc->SectionIndex(SectionType::FrameCapture);

    if(sectionIdx < 0)
      RETURN_ERROR_RESULT(ResultCode::FileCorrupted, "File does not contain captured API data");

    ver = rdc->GetSectionProperties(sectionIdx).version;

    if(!D3D12InitParams::IsSupportedVersion(ver))
    {
      RETURN_ERROR_RESULT(ResultCode::APIIncompatibleVersion,
                          "D3D12 capture is incompatible version %llu, newest supported by this "
                          "build of RenderDoc is %llu",
                          ver, D3D12InitParams::CurrentVersion);
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

    if(initParams.AdapterDesc.DeviceId != 0)
      RDCLOG("Capture was created on %s / %ls",
             ToStr(GPUVendorFromPCIVendor(initParams.AdapterDesc.VendorId)).c_str(),
             initParams.AdapterDesc.Description);

    sectionIdx = rdc->SectionIndex(SectionType::D3D12Core);

    if(sectionIdx >= 0)
    {
      SectionProperties props = rdc->GetSectionProperties(sectionIdx);
      reader = rdc->ReadSection(sectionIdx);

      D3D12Core.resize((size_t)props.uncompressedSize);
      reader->Read(D3D12Core.data(), props.uncompressedSize);

      RDCLOG("Got D3D12Core.dll of size %llu (decompressed from %llu)", props.uncompressedSize,
             props.compressedSize);

      RDCASSERT(reader->AtEnd());
      delete reader;
    }

    sectionIdx = rdc->SectionIndex(SectionType::D3D12SDKLayers);

    if(sectionIdx >= 0)
    {
      SectionProperties props = rdc->GetSectionProperties(sectionIdx);
      reader = rdc->ReadSection(sectionIdx);

      D3D12SDKLayers.resize((size_t)props.uncompressedSize);
      reader->Read(D3D12SDKLayers.data(), props.uncompressedSize);

      RDCLOG("Got D3D12SDKLayers.dll of size %llu (decompressed from %llu)", props.uncompressedSize,
             props.compressedSize);

      RDCASSERT(reader->AtEnd());
      delete reader;
    }
  }

  if(initParams.MinimumFeatureLevel < D3D_FEATURE_LEVEL_11_0)
    initParams.MinimumFeatureLevel = D3D_FEATURE_LEVEL_11_0;

  D3D12DevConfiguration *config = D3D12_PrepareReplaySDKVersion(
      rdc && rdc->IsUntrusted(), initParams.SDKVersion, D3D12Core, D3D12SDKLayers, D3D12Lib);

  const bool isProxy = (rdc == NULL);

  AMDRGPControl *rgp = NULL;

  if(!isProxy)
  {
    rgp = new AMDRGPControl();

    if(!rgp->Initialised())
      SAFE_DELETE(rgp);
  }

  typedef HRESULT(WINAPI * PFN_CREATE_DXGI_FACTORY)(REFIID, void **);

  PFN_CREATE_DXGI_FACTORY createFunc =
      (PFN_CREATE_DXGI_FACTORY)GetProcAddress(GetModuleHandleA("dxgi.dll"), "CreateDXGIFactory1");

  IDXGIFactory1 *factory = NULL;
  HRESULT hr = createFunc(__uuidof(IDXGIFactory1), (void **)&factory);

  if(FAILED(hr))
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Couldn't create DXGI factory! HRESULT: %s",
                        ToStr(hr).c_str());

  IDXGIAdapter *adapter = NULL;

  if(!isProxy)
    ChooseBestMatchingAdapter(GraphicsAPI::D3D12, factory, initParams.AdapterDesc, opts, NULL,
                              &adapter);

  INVAPID3DDevice *nvapiDev = NULL;
  IAGSD3DDevice *agsDev = NULL;

  if(!isProxy)
    NVAftermath_Init();

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
  else if(initParams.VendorExtensions == GPUVendor::AMD ||
          initParams.VendorExtensions == GPUVendor::Samsung)
  {
    agsDev = InitialiseAGSReplay(initParams.VendorUAVSpace, initParams.VendorUAV);

    if(!agsDev)
    {
      RETURN_ERROR_RESULT(
          ResultCode::APIHardwareUnsupported,
          "Capture requires ags to replay, but it's not available or can't be initialised");
    }

    createDevice = [agsDev](IUnknown *pAdapter, D3D_FEATURE_LEVEL MinimumFeatureLevel, REFIID riid,
                            void **ppDevice) {
      return agsDev->CreateD3D12(pAdapter, MinimumFeatureLevel, riid, ppDevice);
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

  bool debugLayerEnabled = false;

  if(!isProxy)
    RDCLOG("Creating D3D12 replay device, minimum feature level %s",
           ToStr(initParams.MinimumFeatureLevel).c_str());

  bool shouldEnableDebugLayer = opts.apiValidation;

  if(shouldEnableDebugLayer && !D3D12Core.empty() && D3D12SDKLayers.empty() && !config)
  {
    RDCWARN(
        "Not enabling D3D debug layers because we captured a D3D12Core.dll but not a matching "
        "D3D12SDKLayers.dll, so this may crash");
    shouldEnableDebugLayer = false;
  }

  if(shouldEnableDebugLayer)
  {
    debugLayerEnabled = EnableD3D12DebugLayer(config, NULL);

    if(!debugLayerEnabled && !isProxy)
    {
      RDCLOG(
          "Enabling the D3D debug layers failed, ensure you have the windows SDK or windows "
          "feature needed or if using a locally distributed D3D12 dll ensure you have "
          "D3D12SDKLayers.dll available next to it.");
    }
  }

  if(EnableDRED(config, NULL))
  {
    RDCLOG("DRED enabled");
  }

  if(D3D12_AttemptExperimentalGPUUploadEnable())
  {
    if(config)
    {
      config->devfactory->EnableExperimentalFeatures(1, &D3D12GPUUploadHeapsOnUnsupportedOS, NULL,
                                                     NULL);
    }
    else if(enableExperimentalPtr)
    {
      enableExperimentalPtr(1, &D3D12GPUUploadHeapsOnUnsupportedOS, NULL, NULL);
    }
  }

  ID3D12Device *dev = NULL;
  if(config)
    hr = config->devfactory->CreateDevice(adapter, initParams.MinimumFeatureLevel,
                                          __uuidof(ID3D12Device), (void **)&dev);
  else
    hr = createDevice(adapter, initParams.MinimumFeatureLevel, __uuidof(ID3D12Device), (void **)&dev);

  if((FAILED(hr) || !dev) && adapter)
  {
    SAFE_RELEASE(dev);

    RDCWARN("Couldn't replay on selected adapter, falling back to default adapter");

    SAFE_RELEASE(adapter);
    if(config)
      hr = config->devfactory->CreateDevice(adapter, initParams.MinimumFeatureLevel,
                                            __uuidof(ID3D12Device), (void **)&dev);
    else
      hr = createDevice(adapter, initParams.MinimumFeatureLevel, __uuidof(ID3D12Device),
                        (void **)&dev);
  }

  SAFE_RELEASE(adapter);

  if(FAILED(hr))
  {
    SAFE_DELETE(rgp);

    RETURN_ERROR_RESULT(ResultCode::APIHardwareUnsupported, "Couldn't create a d3d12 device: %s",
                        ToStr(hr).c_str());
  }

  if(nvapiDev)
  {
    BOOL ok = nvapiDev->SetReal(dev);
    if(!ok)
    {
      SAFE_RELEASE(dev);
      SAFE_RELEASE(nvapiDev);
      SAFE_RELEASE(factory);
      SAFE_DELETE(rgp);
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
      SAFE_RELEASE(dev);
      SAFE_RELEASE(nvapiDev);
      SAFE_RELEASE(factory);
      SAFE_DELETE(rgp);
      RETURN_ERROR_RESULT(
          ResultCode::APIHardwareUnsupported,
          "This capture needs AGS extensions to replay, but device selected for replay can't "
          "support AGS extensions");
    }
  }

  NVAftermath_EnableD3D12(dev);

  WrappedID3D12Device *wrappedDev = new WrappedID3D12Device(dev, initParams, debugLayerEnabled);
  wrappedDev->SetInitParams(initParams, ver, opts, nvapiDev, agsDev);

  if(!isProxy)
    RDCLOG("Created device.");
  D3D12Replay *replay = wrappedDev->GetReplay();

  replay->Set12On7(d3d12on7);
  replay->SetProxy(isProxy);
  replay->SetRGP(rgp);

  replay->Initialise(factory, config);

  *driver = (IReplayDriver *)replay;
  return ResultCode::Succeeded;
}

static DriverRegistration D3D12DriverRegistration(RDCDriver::D3D12, &D3D12_CreateReplayDevice);

RDResult D3D12_ProcessStructured(RDCFile *rdc, SDFile &output)
{
  WrappedID3D12Device device(NULL, D3D12InitParams(), false);

  int sectionIdx = rdc->SectionIndex(SectionType::FrameCapture);

  if(sectionIdx < 0)
    RETURN_ERROR_RESULT(ResultCode::FileCorrupted, "File does not contain captured API data");

  device.SetStructuredExport(rdc->GetSectionProperties(sectionIdx).version);
  RDResult result = device.ReadLogInitialisation(rdc, true);

  if(result == ResultCode::Succeeded)
    device.GetStructuredFile()->Swap(output);

  return result;
}

static StructuredProcessRegistration D3D12ProcessRegistration(RDCDriver::D3D12,
                                                              &D3D12_ProcessStructured);
