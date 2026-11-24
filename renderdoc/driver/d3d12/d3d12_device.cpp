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

#include "d3d12_device.h"
#include <algorithm>
#include <memory>
#include "core/api_monitor.h"
#include "core/core.h"
#include "core/settings.h"
#include "data/hlsl/hlsl_cbuffers.h"
#include "driver/dxgi/dxgi_common.h"
#include "driver/dxgi/dxgi_wrapped.h"
#include "driver/ihv/amd/amd_rgp.h"
#include "driver/ihv/amd/official/DXExt/AmdExtD3D.h"
#include "driver/ihv/nv/nv_aftermath.h"
#include "jpeg-compressor/jpge.h"
#include "maths/formatpacking.h"
#include "replay/replay_resource_versions.h"
#include "replay/replay_timing.h"
#include "serialise/rdcfile.h"
#include "strings/string_utils.h"
#include "d3d12_command_list.h"
#include "d3d12_command_queue.h"
#include "d3d12_commit_audit.inl"
#include "d3d12_debug.h"
#include "d3d12_rendertext.h"
#include "d3d12_replay.h"
#include "d3d12_resources.h"
#include "d3d12_shader_cache.h"

RDOC_EXTERN_CONFIG(bool, Replay_Debug_PrintChunkTimings);

RDOC_EXTERN_CONFIG(bool, Replay_Debug_SingleThreadedCompilation);

RDOC_DEBUG_CONFIG(bool, D3D12_Debug_SingleSubmitFlushing, false,
                  "Every command buffer is submitted and fully flushed to the GPU, to narrow down "
                  "the source of problems.");
RDOC_DEBUG_CONFIG(bool, D3D12_Debug_RT_Overlay, false, "Add some RT tracking to the overlay.");
RDOC_EXTERN_CONFIG(bool, D3D12_Debug_RT_Auditing);

RDOC_CONFIG(uint32_t, D3D12_Hack_MaxDeviceVersion, 0,
            "If non-zero, caps the maximum ID3D12Device version exposed to the application "
            "during capture. For example, setting to 8 prevents the app from using Device10+ "
            "features, improving replay portability. Set to 0 to disable (default).");

WRAPPED_POOL_INST(WrappedID3D12Device);

static bool D3D12MayRetainCheckpointContents(bool initialSources, bool writeProof,
                                            bool independentStorage, bool exactVersion)
{
  return initialSources && writeProof && independentStorage && exactVersion;
}

Threading::CriticalSection WrappedID3D12Device::m_DeviceWrappersLock;
std::map<ID3D12Device *, WrappedID3D12Device *> WrappedID3D12Device::m_DeviceWrappers;

struct D3D12ReplayCheckpointResource
{
  ResourceId id;
  ID3D12Resource *gpu = NULL;    // unwrapped, immutable COPY_SOURCE after capture
  bytebuf cpu;
  uint64_t bytes = 0;
  uint32_t version = 0;
  ReplayResourceVersion contentVersion;
  ~D3D12ReplayCheckpointResource() { SAFE_RELEASE(gpu); }
};

// Buffer layouts have specified byte identity across aliases. UNKNOWN-layout textures do not:
// their compression metadata cannot be captured through this physical-memory view.
struct D3D12ReplayBufferAliasComponent
{
  ResourceId heap;
  uint64_t begin = 0, end = 0, allocationBytes = 0;
  std::set<ResourceId> members;
  ReplayResourceRange Range() const { return {heap, 0, begin, end - begin}; }
};

struct D3D12ReplayCheckpointBufferAliases
{
  D3D12ReplayBufferAliasComponent component;
  ID3D12Resource *gpu = NULL;
  ReplayResourceVersion version;
  ~D3D12ReplayCheckpointBufferAliases() { SAFE_RELEASE(gpu); }
};

struct D3D12ReplayCheckpointHeap
{
  ResourceId id;
  ResourceId baseline;
  D3D12_DESCRIPTOR_HEAP_TYPE type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
  uint32_t descriptorCount = 0;
  rdcarray<D3D12DescriptorRestoreRange> ranges;
  // Packed complete dirty blocks relative to the immutable initial heap. With no reliable
  // baseline, ranges contains the whole heap instead. This is not a shader-usage subset.
  rdcarray<D3D12Descriptor> descriptors;
};

struct D3D12ReplayCheckpointQueryData
{
  ID3D12Resource *gpu = NULL;    // unwrapped immutable COPY_SOURCE; never CPU read back
  uint64_t bytes = 0;
  ~D3D12ReplayCheckpointQueryData() { SAFE_RELEASE(gpu); }
};

struct D3D12ReplayCheckpointQuery
{
  std::shared_ptr<D3D12ReplayCheckpointQueryData> data;
  uint64_t offset = 0;
  D3D12CheckpointQueryVersion version;
};

struct D3D12ReplayCheckpoint
{
  uint32_t eventId = 0, nextEvent = 0, nextAction = 0;
  uint32_t firstNativeResolveEID = ~0U;
  D3D12_QUERY_TYPE firstNativeResolveType = D3D12_QUERY_TYPE_OCCLUSION;
  uint64_t offset = 0;
  uint64_t lastUse = 0;
  std::map<ResourceId, SubresourceStateVector> states;
  std::map<ResourceId, std::shared_ptr<D3D12ReplayCheckpointResource>> resources;
  std::map<ResourceId, std::shared_ptr<D3D12ReplayCheckpointBufferAliases>> bufferAliases;
  std::set<ResourceId> typedAliasOwners;
  std::set<ResourceId> typedAliasMembers;
  std::map<ResourceId, std::shared_ptr<D3D12ReplayCheckpointHeap>> heaps;
  std::map<D3D12CheckpointQuerySlot, D3D12ReplayCheckpointQuery> queries;
  std::set<D3D12CheckpointQueryResolve> queryResolves;
};

struct D3D12ReplayCheckpointCache
{
  ReplayCheckpointConfig config;
  ReplayCheckpointStats stats;
  std::map<uint32_t, D3D12CommandData::CheckpointBoundary> selected;
  std::map<uint32_t, uint64_t> selectedBytes;
  std::map<uint32_t, D3D12ReplayCheckpoint> checkpoints;
  // A failed completion wait cannot publish a usable checkpoint or release its submitted copy
  // destinations. Keep the failed candidate alive for the remaining device lifetime.
  D3D12ReplayCheckpoint unconfirmedSave;
  // Only a version actually materialised during this replay may be shared. An equal write EID
  // from a different full replay is not proof that nondeterministic GPU contents are identical.
  std::map<ResourceId, std::shared_ptr<D3D12ReplayCheckpointResource>> currentVersions;
  std::map<ResourceId, std::shared_ptr<D3D12ReplayCheckpointBufferAliases>> currentBufferAliases;
  std::map<ResourceId, D3D12ReplayBufferAliasComponent> bufferAliasComponents;
  std::map<ResourceId, ResourceId> bufferAliasMembers;
  std::map<ResourceId, std::set<ResourceId>> typedAliasConflicts;
  std::map<uint32_t, std::set<ResourceId>> selectedTypedOwners;
  std::set<uint32_t> provedTypedBoundaries;
  // Content identity survives an EID request, unlike the per-replay snapshot sharing map. Only
  // actual checkpoint copies establish a known identity; potential writes invalidate it.
  ReplayResourceVersions residentVersions;
  std::map<ResourceId, uint64_t> residentExtents;
  uint64_t contentSerial = 0;
  bool markingRestoredContents = false;
  // Initial contents intentionally omitted for a proved full clear. This debt is carried into
  // the ordinary dirty set even if a decode/submission aborts before executing that clear.
  std::set<ResourceId> deferredInitialResources;
  ReplayResourceRange ResidentRange(ResourceId id) const
  {
    auto found = residentExtents.find(id);
    return {id, 0, 0, found == residentExtents.end() ? 0 : found->second};
  }
  void PublishResidentVersions(const D3D12ReplayCheckpoint &checkpoint)
  {
    for(const auto &entry : checkpoint.resources)
      if(!deferredInitialResources.count(entry.first))
        residentVersions.MarkResident(ResidentRange(entry.first), entry.second->contentVersion);
    for(const auto &entry : checkpoint.bufferAliases)
      residentVersions.MarkResident(entry.second->component.Range(), entry.second->version);
  }
  // These references also keep unwrapped query-copy sources alive until submitted work is idle.
  std::map<D3D12CheckpointQuerySlot, D3D12ReplayCheckpointQuery> activeQueries;
  std::set<D3D12CheckpointQueryResolve> activeQueryResolves;
  std::set<ResourceId> overlappingPlaced;
  bool directHeapAccess = false;
  bool editedDependencies = false;
  // Shared values cannot affect output before their first possible consumer. Shader/resource
  // replacements still use editedDependencies for the entire frame.
  uint32_t bufferEditFirstEvent = ~0U;
  uint32_t bufferEditAnchorConsumer = 0;
  uint64_t budget = 0;
  bool memoryBudgetBlocked = false;
  uint64_t serial = 0;
  bool savedThisReplay = false;
  // Keep the original API request: WithoutDraw's queue end is one event earlier, but its
  // following OnlyDraw must also be safe without native query state from the skipped prefix.
  uint32_t targetEvent = 0;
  struct RestorePlan
  {
    uint32_t eventId = 0;
    uint32_t queueEndEvent = 0;
    // Half-open physical byte intervals supplied by independent placed-buffer copies or whole
    // qualified buffer-alias components. All remaining bytes retain their original initial source.
    std::map<ResourceId, rdcarray<rdcpair<uint64_t, uint64_t>>> heapExclusions;
    std::set<ResourceId> retainedResources;
    std::set<ResourceId> fullClearResources;
    std::map<ResourceId, rdcarray<D3D12DescriptorRestoreRange>> descriptorExclusions;
    uint64_t retainedBytes = 0;
    uint64_t fullClearBytes = 0;
    bool skippedInitialContents = false;
    uint64_t skippedResourceBytes = 0, skippedHeapBytes = 0;
    uint32_t skippedResources = 0;
  } restorePlan;

  uint64_t AllocatedBytes(const D3D12ReplayCheckpoint *extra = NULL,
                         const std::set<uint32_t> *excluded = NULL) const
  {
    std::set<const void *> seen;
    uint64_t bytes = 0;
    const auto count = [&](const D3D12ReplayCheckpoint &checkpoint) {
      for(const auto &resource : checkpoint.resources)
        if(seen.insert(resource.second.get()).second)
          bytes += resource.second->bytes;
      for(const auto &aliases : checkpoint.bufferAliases)
        if(seen.insert(aliases.second.get()).second)
          bytes += aliases.second->component.allocationBytes;
      for(const auto &heap : checkpoint.heaps)
        if(seen.insert(heap.second.get()).second)
          bytes += heap.second->descriptors.byteSize();
      for(const auto &query : checkpoint.queries)
        if(seen.insert(query.second.data.get()).second)
          bytes += query.second.data->bytes;
    };
    for(const auto &checkpoint : checkpoints)
      if(!excluded || excluded->count(checkpoint.first) == 0)
        count(checkpoint.second);
    if(extra)
      count(*extra);
    for(const auto &query : activeQueries)
      if(seen.insert(query.second.data.get()).second)
        bytes += query.second.data->bytes;
    return bytes;
  }
};

static uint32_t D3D12CheckpointBufferAnchor(const D3D12ReplayCheckpointCache &cache,
                                           uint32_t firstConsumer, uint32_t targetEvent)
{
  if(firstConsumer <= 1 || firstConsumer == ~0U)
    return 0;
  auto point = cache.checkpoints.lower_bound(firstConsumer);
  while(point != cache.checkpoints.begin())
  {
    --point;
    if(IsCheckpointQueryTargetSafe(targetEvent, point->second.firstNativeResolveEID))
      return point->first;
  }
  return 0;
}

static uint32_t D3D12MissingCheckpointToSave(const D3D12ReplayCheckpointCache &cache,
                                           uint32_t reachedEvent, uint32_t saveBefore,
                                           uint32_t targetEvent, uint32_t firstConsumer,
                                           bool prefixOnly = false)
{
  if(cache.savedThisReplay || cache.config.maxCheckpoints == 0)
    return 0;
  const auto nearestMissing = [&](uint32_t limit) {
    auto point = cache.selected.lower_bound(limit);
    while(point != cache.selected.begin())
    {
      --point;
      if(point->first < reachedEvent)
        break;
      if(cache.checkpoints.count(point->first) == 0 &&
         IsCheckpointQueryTargetSafe(targetEvent, point->second.firstNativeResolveEID))
        return point->first;
    }
    return 0U;
  };
  if(firstConsumer > 1 && firstConsumer != ~0U)
  {
    // Once a reusable prefix exists, later output checkpoints may be built from it. Repeated
    // edits invalidate only those downstream versions; they must not force the prefix to rebuild.
    if(!D3D12CheckpointBufferAnchor(cache, firstConsumer, targetEvent))
    {
      const uint32_t prefix = nearestMissing(RDCMIN(saveBefore, firstConsumer));
      if(prefix)
        return prefix;
    }
    if(cache.config.maxCheckpoints == 1 &&
       D3D12CheckpointBufferAnchor(cache, firstConsumer, firstConsumer))
      return 0;
  }
  // Prefix-only requests without a proven usable prefix deliberately save nothing. They may
  // still render through the normal fallback, without synchronously rebuilding edited outputs.
  if(prefixOnly)
    return 0;
  return nearestMissing(saveBefore);
}

static bool D3D12PlanCheckpointEvictions(const D3D12ReplayCheckpointCache &cache,
                                         const D3D12ReplayCheckpoint &candidate,
                                         uint64_t pendingBytes, uint32_t protectedAnchor,
                                         std::set<uint32_t> &victims)
{
  victims.clear();
  if(cache.config.maxCheckpoints == 0 || pendingBytes > cache.budget)
    return false;
  while(cache.checkpoints.size() - victims.size() >= cache.config.maxCheckpoints ||
        cache.AllocatedBytes(&candidate, &victims) > cache.budget - pendingBytes)
  {
    auto oldest = cache.checkpoints.end();
    for(auto point = cache.checkpoints.begin(); point != cache.checkpoints.end(); ++point)
      if(point->first != protectedAnchor && victims.count(point->first) == 0 &&
         (oldest == cache.checkpoints.end() || point->second.lastUse < oldest->second.lastUse))
        oldest = point;
    if(oldest == cache.checkpoints.end())
    {
      victims.clear();
      return false;
    }
    victims.insert(oldest->first);
  }
  return true;
}

static bool CheckpointResourceWrite(ResourceUsage usage)
{
  return (usage >= ResourceUsage::VS_RWResource && usage <= ResourceUsage::All_RWResource) ||
         usage == ResourceUsage::CopyDst || usage == ResourceUsage::Copy ||
         usage == ResourceUsage::ResolveDst || usage == ResourceUsage::Resolve ||
         usage == ResourceUsage::GenMips || usage == ResourceUsage::Clear ||
         usage == ResourceUsage::Discard || usage == ResourceUsage::CPUWrite ||
         usage == ResourceUsage::ColorTarget || usage == ResourceUsage::DepthStencilTarget ||
         usage == ResourceUsage::StreamOut;
}

static void LogD3D12Checkpoint(const rdcstr &line)
{
  // Standalone replay can run from an unwritable executable directory. Preserve the same
  // checkpoint evidence in the caller-selected diagnostic log as well as the optional timing file.
  RDCLOG("[EIDTiming] %s", line.c_str());
  AppendReplayTimingLog(line);
}

static D3D12_RESOURCE_DESC CheckpointQueryBufferDesc(uint64_t bytes)
{
  D3D12_RESOURCE_DESC desc = {};
  desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
  desc.Width = bytes;
  desc.Height = 1;
  desc.DepthOrArraySize = 1;
  desc.MipLevels = 1;
  desc.SampleDesc.Count = 1;
  desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
  return desc;
}

static bool D3D12CheckpointStatesEqual(const SubresourceStateVector &a,
                                      const SubresourceStateVector &b)
{
  if(a.size() != b.size())
    return false;
  for(size_t i = 0; i < a.size(); i++)
    if(!a[i].IsStates() || !b[i].IsStates() || a[i].ToStates() != b[i].ToStates())
      return false;
  return true;
}

static bool D3D12ProvedTypedAliasActivation(
    bool boundaryProven, ResourceId after,
    const std::map<ResourceId, std::set<ResourceId>> &conflicts)
{
  return boundaryProven && after != ResourceId() && conflicts.count(after) != 0;
}

static WrappedID3D12Heap *D3D12CheckpointOriginalHeap(D3D12ResourceManager *manager, ResourceId id)
{
  if(id == ResourceId() || !manager->HasResource(id) || manager->HasReplacement(id) ||
     manager->GetUnreplacedID(id) != id)
    return NULL;
  ID3D12DeviceChild *object = manager->GetResourceWithoutReplacement(id, true);
  return object && TryIdentifyTypeByPtr(object) == Resource_Heap ? (WrappedID3D12Heap *)object : NULL;
}

struct D3D12CheckpointPlacedRange
{
  ResourceId resource;
  uint64_t begin = 0, end = 0;
};

static bool D3D12MayShareBufferAliasVersion(bool writesKnown, bool trackingAllowed, bool exactVersion)
{
  // Restoration can establish a known version even when the next shader/analysis replay has
  // no complete potential-write tracking. That identity alone cannot prove it stayed unchanged.
  return writesKnown && trackingAllowed && exactVersion;
}

static rdcarray<D3D12ReplayBufferAliasComponent> D3D12BufferAliasComponents(
    ResourceId heap, uint64_t heapBytes, bool completeBufferHeap,
    rdcarray<D3D12CheckpointPlacedRange> ranges)
{
  rdcarray<D3D12ReplayBufferAliasComponent> result;
  if(!completeBufferHeap || heap == ResourceId() || !heapBytes)
    return result;
  std::set<ResourceId> seen;
  for(const auto &range : ranges)
    if(range.resource == ResourceId() || !seen.insert(range.resource).second ||
       range.begin >= range.end || range.end > heapBytes)
      return result;
  std::sort(ranges.begin(), ranges.end(),
            [](const D3D12CheckpointPlacedRange &a, const D3D12CheckpointPlacedRange &b) {
              return a.begin < b.begin;
            });
  for(size_t i = 0; i < ranges.size();)
  {
    D3D12ReplayBufferAliasComponent component;
    component.heap = heap;
    component.begin = ranges[i].begin;
    component.end = ranges[i].end;
    component.members.insert(ranges[i++].resource);
    // A-B-C overlap forms one physical component even when A and C do not overlap directly.
    // Adjacent independent allocations remain separate and need no alias checkpoint storage.
    while(i < ranges.size() && ranges[i].begin < component.end)
    {
      component.end = RDCMAX(component.end, ranges[i].end);
      component.members.insert(ranges[i++].resource);
    }
    if(component.members.size() > 1)
      result.push_back(std::move(component));
  }
  return result;
}

static void D3D12CheckpointAliasBarrier(ID3D12GraphicsCommandList *list, ID3D12Resource *after = NULL)
{
  D3D12_RESOURCE_BARRIER alias = {};
  alias.Type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
  alias.Aliasing.pResourceAfter = after;
  // Synchronize previous physical-memory accesses before using a typed checkpoint copy or an
  // explicitly qualified buffer-only physical view. This alone does not initialize texture data.
  list->ResourceBarrier(1, &alias);
}

static void D3D12BufferAliasCopy(ID3D12GraphicsCommandList *list, ID3D12Resource *wholeHeap,
                                 const D3D12ReplayCheckpointBufferAliases &saved, bool restore)
{
  D3D12CheckpointAliasBarrier(list);
  D3D12_RESOURCE_BARRIER transition = {};
  transition.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
  transition.Transition.pResource = wholeHeap;
  transition.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
  transition.Transition.StateBefore = D3D12_RESOURCE_STATE_COMMON;
  transition.Transition.StateAfter =
      restore ? D3D12_RESOURCE_STATE_COPY_DEST : D3D12_RESOURCE_STATE_COPY_SOURCE;
  list->ResourceBarrier(1, &transition);
  const auto &component = saved.component;
  if(restore)
    list->CopyBufferRegion(wholeHeap, component.begin, saved.gpu, 0, component.end - component.begin);
  else
    list->CopyBufferRegion(saved.gpu, 0, wholeHeap, component.begin, component.end - component.begin);
  std::swap(transition.Transition.StateBefore, transition.Transition.StateAfter);
  list->ResourceBarrier(1, &transition);
  D3D12CheckpointAliasBarrier(list);
}

static bool IsReplayBufferPatchCopy(D3D12Chunk chunk)
{
  return chunk == D3D12Chunk::List_CopyBufferRegion || chunk == D3D12Chunk::List_CopyResource ||
         chunk == D3D12Chunk::List_CopyTextureRegion;
}

static bool SameD3D12BufferPatchLayout(const rdcarray<ReplayBufferPatch> &previous,
                                       const rdcarray<ReplayBufferPatch> &next)
{
  return SameReplayBufferPatchLayout(previous, next);
}

static bool IsD3D12BufferPatchReadUsage(ResourceUsage usage)
{
  // One upload-ring allocation can contain CBVs, SRVs and geometry data. These bindings only
  // read bytes; rejecting their entire allocation makes ordinary material constants uneditable.
  // Keep generated-command arguments and every GPU-write usage outside this whitelist.
  return (usage >= ResourceUsage::VS_Constants && usage <= ResourceUsage::All_Constants) ||
         (usage >= ResourceUsage::VS_Resource && usage <= ResourceUsage::All_Resource) ||
         usage == ResourceUsage::VertexBuffer || usage == ResourceUsage::IndexBuffer ||
         usage == ResourceUsage::CopySrc;
}

static uint32_t D3D12BufferPatchConsumerFromUsage(uint32_t first, const EventUsage &usage)
{
  if(IsD3D12BufferPatchReadUsage(usage.usage) || usage.usage == ResourceUsage::Copy)
    return RDCMIN(first, usage.eventId);
  return first;
}

static uint32_t D3D12CheckpointConservativeWriteStart(bool directHeap, bool editedDependencies,
                                                     uint32_t bufferFirstConsumer)
{
  return directHeap || editedDependencies ? 1U : bufferFirstConsumer;
}

static bool KeepD3D12ValueEditDirtyProof(bool bindingsUnchanged, bool completeReads,
                                         bool dirtyValid, bool shaderEdited, bool disabled)
{
  return bindingsUnchanged && completeReads && dirtyValid && !shaderEdited && !disabled;
}

rdcstr WrappedID3D12Device::SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches)
{
  PerformanceTimer phaseTimer;
  rdcstr error = ValidateReplayBufferPatches(patches);
  if(!error.empty())
    return error;
  std::set<ResourceId> buffers;
  std::map<ResourceId, uint32_t> consumers;
  std::map<ResourceId, uint64_t> bufferExtents;
  uint64_t bytes = 0;
  uint32_t firstConsumer = ~0U;
  for(const ReplayBufferPatch &patch : patches)
  {
    auto found = GetResourceList().find(patch.buffer);
    if(found == GetResourceList().end() || ResourceIDGen::IsReplayOnlyID(patch.buffer) ||
       GetResourceManager()->HasReplacement(patch.buffer) ||
       GetResourceManager()->GetUnreplacedID(patch.buffer) != patch.buffer)
      return "Persistent D3D12 buffer edit requires an original captured resource";
    WrappedID3D12Resource *resource = found->second;
    D3D12_RESOURCE_DESC desc = resource->GetReal()->GetDesc();
    D3D12_HEAP_PROPERTIES properties = {};
    if(desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
       patch.byteOffset > desc.Width || patch.data.size() > desc.Width - patch.byteOffset ||
       (desc.Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS) ||
       IsSparseResource(patch.buffer) || resource->IsAccelerationStructureResource() ||
       FAILED(resource->GetReal()->GetHeapProperties(&properties, NULL)) ||
       (properties.Type != D3D12_HEAP_TYPE_UPLOAD && properties.Type != D3D12_HEAP_TYPE_DEFAULT))
      return "Persistent D3D12 edits require a non-sparse UPLOAD or DEFAULT parameter buffer without UAV access";
    bytes += patch.data.size();
    if(!buffers.insert(patch.buffer).second)
      continue;
    bufferExtents[patch.buffer] = desc.Width;

    const uint32_t nativeConsumer =
        m_Queue->GetCommandData()->GetBufferPatchFirstConsumer(patch.buffer);
    // Missing complete native binding evidence cannot be repaired by the reflected usage list:
    // large descriptor ranges are intentionally omitted from that UI/analysis list.
    uint32_t bufferConsumer = nativeConsumer == ~0U ? 0U : nativeConsumer;

    bool constants = false;
    for(const EventUsage &usage : m_Queue->GetUsage(patch.buffer))
    {
      bufferConsumer = D3D12BufferPatchConsumerFromUsage(bufferConsumer, usage);
      if(usage.usage >= ResourceUsage::VS_Constants && usage.usage <= ResourceUsage::All_Constants)
      {
        constants = true;
      }
      else if(usage.usage == ResourceUsage::CopyDst || usage.usage == ResourceUsage::Copy)
      {
        const APIEvent &event = m_Queue->GetEvent(usage.eventId);
        if(event.eventId != usage.eventId || !m_StructuredFile ||
           event.chunkIndex >= m_StructuredFile->chunks.size() ||
           !IsReplayBufferPatchCopy(
               (D3D12Chunk)m_StructuredFile->chunks[event.chunkIndex]->metadata.chunkID))
          return "Persistent D3D12 buffer has a write command without a range-patch replay hook";
      }
      else if(!IsD3D12BufferPatchReadUsage(usage.usage) &&
              usage.usage != ResourceUsage::CPUWrite &&
              usage.usage != ResourceUsage::Barrier && usage.usage != ResourceUsage::Unused)
        return StringFormat::Fmt(
            "Persistent D3D12 parameter buffer has unsupported %s use at event %u",
            ToStr(usage.usage).c_str(), usage.eventId);
    }
    if(!constants)
      return "Persistent D3D12 edit target has no captured constant-buffer consumer";
    const auto state = m_ResourceStates.find(patch.buffer);
    if(state == m_ResourceStates.end())
      return "Persistent D3D12 edit target has no tracked resource state";
    for(const auto &subresource : state->second)
      if(!subresource.IsStates())
        return "Persistent D3D12 buffer edits require legacy resource-state barriers";

    ResourceId initialId = patch.buffer;
    if(resource->GetHeap())
    {
      initialId = GetResID(resource->GetHeap());
      const uint64_t start = resource->GetHeapOffset();
      const uint64_t size = GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
      if(size == ~0ULL || size > ~0ULL - start)
        return "Persistent D3D12 placed-buffer allocation range is unavailable";
      // Mapped writes through any overlapping placed resource could overwrite these bytes without
      // naming this buffer. Accept only independent physical storage, not merely different IDs.
      for(const auto &other : GetResourceList())
      {
        if(other.first == patch.buffer || ResourceIDGen::IsReplayOnlyID(other.first) ||
           other.second->GetHeap() != resource->GetHeap())
          continue;
        const D3D12_RESOURCE_DESC otherDesc = other.second->GetReal()->GetDesc();
        const uint64_t otherStart = other.second->GetHeapOffset();
        const uint64_t otherSize = GetReal()->GetResourceAllocationInfo(0, 1, &otherDesc).SizeInBytes;
        if(otherSize == ~0ULL || otherSize > ~0ULL - otherStart ||
           (start < otherStart + otherSize && otherStart < start + size))
          return "Persistent D3D12 buffer edits cannot yet share overlapping placed allocations";
      }
    }
    consumers[patch.buffer] = bufferConsumer;
    firstConsumer = RDCMIN(firstConsumer, bufferConsumer);
    const D3D12InitialContents initial = GetResourceManager()->GetInitialContents(initialId);
    if((!initial.resource && !initial.srcData) || initial.sparseBinds ||
       (initial.tag != D3D12InitialContents::Copy && initial.tag != D3D12InitialContents::ForceCopy))
      return "Persistent D3D12 buffer edit has no immutable initial contents for clearing the edit";
  }
  bool identical = patches.size() == m_ReplayBufferPatches.size();
  for(size_t i = 0; i < patches.size() && identical; i++)
    identical = patches[i].buffer == m_ReplayBufferPatches[i].buffer &&
                patches[i].byteOffset == m_ReplayBufferPatches[i].byteOffset &&
                patches[i].data == m_ReplayBufferPatches[i].data;
  if(identical)
    return {};
  const double validateMS = phaseTimer.GetMilliseconds();
  // Refine even the first edit's whole-buffer binding bound with the immutable native byte
  // consumer index. Unknown/generated cases keep their conservative union or fail closed.
  rdcarray<ReplayBufferPatchRange> allRanges;
  for(const ReplayBufferPatch &patch : patches)
    allRanges.push_back({patch.buffer, patch.byteOffset, patch.data.size()});
  uint32_t preciseFirst = ~0U;
  if(!allRanges.empty() &&
     m_Queue->GetCommandData()->GetBufferPatchRangeConsumer(bufferExtents, allRanges, preciseFirst) &&
     preciseFirst != ~0U)
    firstConsumer = preciseFirst;
  const bool sameLayout = SameD3D12BufferPatchLayout(m_ReplayBufferPatches, patches);
  const bool firstOverlay = m_ReplayBufferPatches.empty() && !patches.empty();
  rdcarray<ReplayBufferPatchRange> changes;
  const bool deltaKnown = GetReplayBufferPatchChanges(m_ReplayBufferPatches, patches, changes).empty();
  const bool deltaEnabled =
      Process::GetEnvVariable("RENDERDOC_REPLAY_DISABLE_BUFFER_PATCH_DELTA") != "1";
  uint64_t changedBytes = 0;
  std::set<ResourceId> changedBuffers;
  uint32_t changedConsumer = ~0U;
  for(const ReplayBufferPatchRange &change : changes)
  {
    changedBytes += change.byteLength;
    changedBuffers.insert(change.buffer);
    auto consumer = consumers.find(change.buffer);
    changedConsumer = RDCMIN(changedConsumer, consumer == consumers.end() ? 0U : consumer->second);
  }
  uint32_t rangeConsumer = ~0U;
  const bool rangeIndexKnown =
      deltaKnown && !changes.empty() &&
      m_Queue->GetCommandData()->GetBufferPatchRangeConsumer(bufferExtents, changes, rangeConsumer);
  if(rangeIndexKnown && rangeConsumer != ~0U)
    changedConsumer = rangeConsumer;
  // The full-table consumer continues to identify the immutable edit-start anchor. Only the
  // invalidation cutoff may advance when a later parameter changes and earlier overlays do not.
  const uint32_t affectedConsumer = sameLayout && firstConsumer > 0 && firstConsumer != ~0U &&
                                           deltaKnown && !changes.empty() && deltaEnabled
                                       ? changedConsumer
                                       : firstConsumer;
  const double indexMS = phaseTimer.GetMilliseconds();

  ID3D12Resource *upload = NULL;
  rdcarray<uint64_t> offsets;
  const bool reuseUpload = sameLayout && m_ReplayBufferPatchUpload &&
                          m_ReplayBufferPatchOffsets.size() == patches.size() &&
                          Process::GetEnvVariable(
                              "RENDERDOC_D3D12_DISABLE_BUFFER_PATCH_UPLOAD_REUSE") != "1";
  // A fixed-layout slider does not need another GPU allocation. Drain all lists that can refer
  // to the old payload before touching it. Allocation/layout changes retain the transactional
  // new-object path below; this reuse does not remove or weaken the existing GPU wait.
  if(reuseUpload)
  {
    if(!DeviceWaitForIdle() || HasFatalError())
      return "D3D12 GPU completion could not be confirmed before updating persistent edit data";
    upload = m_ReplayBufferPatchUpload;
    upload->AddRef();
  }
  uint64_t uploadWrittenBytes = 0;
  if(bytes)
  {
    D3D12_HEAP_PROPERTIES properties = {};
    properties.Type = D3D12_HEAP_TYPE_UPLOAD;
    properties.CreationNodeMask = properties.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC desc = {};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = bytes;
    desc.Height = 1;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    HRESULT hr = S_OK;
    if(!reuseUpload)
      hr = GetReal()->CreateCommittedResource(
          &properties, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_GENERIC_READ, NULL,
          __uuidof(ID3D12Resource), (void **)&upload);
    void *mapped = NULL;
    const D3D12_RANGE noRead = {0, 0};
    if(SUCCEEDED(hr))
      hr = upload->Map(0, &noRead, &mapped);
    if(FAILED(hr) || !mapped)
    {
      SAFE_RELEASE(upload);
      return StringFormat::Fmt("Cannot allocate persistent D3D12 edit data: %s", ToStr(hr).c_str());
    }
    uint64_t offset = 0;
    for(const ReplayBufferPatch &patch : patches)
    {
      offsets.push_back(offset);
      if(!reuseUpload || !deltaKnown || changedBuffers.count(patch.buffer))
      {
        memcpy((byte *)mapped + offset, patch.data.data(), patch.data.size());
        uploadWrittenBytes += patch.data.size();
      }
      offset += patch.data.size();
    }
    const D3D12_RANGE written = {0, (SIZE_T)bytes};
    upload->Unmap(0, &written);
  }

  // Retire old immutable upload bytes only after all native lists that reference them complete.
  // The new-object path has not changed accepted bytes; the reuse path already drained its users.
  if((!reuseUpload && !DeviceWaitForIdle()) || HasFatalError())
  {
    SAFE_RELEASE(upload);
    return "D3D12 GPU completion could not be confirmed before installing persistent edit data";
  }
  // This transaction has just drained every queue, and only mapped upload bytes since that
  // drain. Let source retirement below use that proof without another fallible wait after the
  // accepted patch data changes. Do not publish this flag in generic DeviceWaitForIdle: callers
  // there can submit further raw command lists before their batch is marked as pending.
  m_WaitedForIdleAfterReplay = true;
  const double payloadMS = phaseTimer.GetMilliseconds();
  for(WrappedID3D12CommandQueue *queue : m_Queues)
    if(queue)
      queue->ClearPreparedReplayCache();
  const bool keepDirty = KeepD3D12ValueEditDirtyProof(
      sameLayout || m_ReplayBufferPatches.empty(), rangeIndexKnown, m_ReplayDirtyValid,
      m_ReplayDirtyDependenciesEdited,
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_BUFFER_PATCH_DIRTY_REUSE") == "1");
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=D3D12BufferPatchDirtyProof sameLayout=%u completeReads=%u valid=%u edited=%u "
        "checkpoints=%u retained=%u dirtyBlocked=%u previousReason=%s",
        sameLayout ? 1U : 0U, rangeIndexKnown ? 1U : 0U, m_ReplayDirtyValid ? 1U : 0U,
        m_CheckpointDependenciesEdited ? 1U : 0U,
        m_ReplayCheckpoints && m_ReplayCheckpoints->config.enabled ? 1U : 0U, keepDirty ? 1U : 0U,
        m_ReplayDirtyDependenciesEdited ? 1U : 0U, m_ReplayDirtyReason.c_str()));
  // A complete existing write proof already includes all declared UAV array elements and
  // every possible bound RT/DS/SO destination, regardless of the previous parameter's branch.
  // The first validated parameter overlay and same-layout data updates introduce no native
  // binding/PSO/descriptor-table changes. The complete read index and declared write union also
  // cover parameter-dependent branches/indices and known indirect signatures. Keep this dirty set
  // instead of declaring every writable resource unknown on each slider value. Other edits,
  // Shader changes and incomplete proofs retain the full reset. Checkpoint restoration seeds
  // every restored resource/alias into this same union in MarkCheckpointReplayWrites; merely
  // enabling bounded checkpoint storage must not discard an otherwise complete write proof.
  if(!keepDirty)
    InvalidateReplayDirtyTracking("sharedBufferEdit");
  else
    m_ReplayDirtyReason = "sharedBufferValueKnownWrites";
  InvalidateLiveForward("sharedBufferEdit");
  for(const ReplayBufferPatch &patch : m_ReplayBufferPatches)
    buffers.insert(patch.buffer);
  for(ResourceId id : buffers)
  {
    m_ModResources.insert(id);
    auto resource = GetResourceList().find(id);
    if(resource != GetResourceList().end() && resource->second->GetHeap())
      m_ModResources.insert(GetResID(resource->second->GetHeap()));
    if(keepDirty)
      MarkReplayResourceDirty(id);
  }
  // A new branch can write a previously unused bound UAV. Restore such destinations from their
  // immutable initial source on future root replays, even if original usage marked them readonly.
  if(!patches.empty() && !keepDirty)
    for(const auto &entry : GetResourceList())
      if(!ResourceIDGen::IsReplayOnlyID(entry.first) &&
         (entry.second->GetReal()->GetDesc().Flags &
          (D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS | D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET |
           D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL)))
      {
        m_ModResources.insert(entry.first);
        if(entry.second->GetHeap())
          m_ModResources.insert(GetResID(entry.second->GetHeap()));
      }
  SAFE_RELEASE(m_ReplayBufferPatchUpload);
  m_ReplayBufferPatchUpload = upload;
  m_ReplayBufferPatchOffsets = std::move(offsets);
  m_ReplayBufferPatches = patches;
  m_ReplayBufferPatchFirstConsumer = patches.empty() ? ~0U : firstConsumer;
  m_ReplayBufferPatchFirstAffected =
      (sameLayout || (firstOverlay && rangeIndexKnown)) && !m_CheckpointDependenciesEdited &&
              affectedConsumer != ~0U &&
              Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_BUFFER_PATCH_PREFIX") != "1"
          ? affectedConsumer
          : 0;
  // First installation has no old overlay bytes to undo. A complete native read proof preserves
  // earlier canonical points; ApplyCheckpointReplayBufferPatches installs the new bytes after
  // restoration. Removing/changing existing ranges still needs the original-source reset.
  InvalidateReplayCheckpoints(m_ReplayBufferPatchFirstAffected);
  m_ReplayBufferPatchAnchorEvent = 0;
  if(m_ReplayCheckpoints)
  {
    auto &cache = *m_ReplayCheckpoints;
    cache.bufferEditFirstEvent = patches.empty() ? ~0U : RDCMAX(1U, firstConsumer);
    if(firstConsumer > 1 && firstConsumer != ~0U && !m_CheckpointDependenciesEdited &&
       cache.config.enabled && cache.config.maxCheckpoints && cache.budget &&
       Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_BUFFER_PATCH_PREFIX") != "1")
    {
      // The general module planner may not propose an edit-start boundary. Add at most one real
      // completed submission, with the original query/alias/continuation validation. Allocation
      // and complete-resource budget validation remain in SaveReplayCheckpoint.
      if(cache.bufferEditAnchorConsumer != firstConsumer)
      {
        cache.bufferEditAnchorConsumer = firstConsumer;
        D3D12CommandData &cmd = *m_Queue->GetCommandData();
        for(auto it = cmd.m_CheckpointBoundaries.end(); it != cmd.m_CheckpointBoundaries.begin();)
        {
          --it;
          if(!it->eventId || it->eventId >= firstConsumer)
            continue;
          rdcstr reason;
          if(!cmd.IsCheckpointBoundarySafe(*it, reason))
            continue;
          cache.selected[it->eventId] = *it;
          break;
        }
      }
      // Reuse the immutable plan across slider values. A candidate rejected by Save for memory
      // or storage must stay rejected, rather than being reinserted and rebuilt on every update.
      auto anchor = cache.selected.lower_bound(firstConsumer);
      if(anchor != cache.selected.begin())
        m_ReplayBufferPatchAnchorEvent = (--anchor)->first;
    }
  }
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=D3D12BufferPatchPhases validate_ms=%.3f readIndex_ms=%.3f payloadAndDrain_ms=%.3f "
        "invalidation_ms=%.3f rangeProof=%u firstConsumer=%u affected=%u uploadReused=%u "
        "dirtyProofRetained=%u",
        validateMS, indexMS - validateMS, payloadMS - indexMS,
        phaseTimer.GetMilliseconds() - payloadMS, rangeIndexKnown ? 1U : 0U, firstConsumer,
        affectedConsumer, reuseUpload ? 1U : 0U, keepDirty ? 1U : 0U));
  // Buffer-patch scrub deliberately preserves the initial-state source cache
  // (initialSourcesPreserved=1 below), so use the lightweight scrub rather than
  // the full release that trims rebuildable replay resources.
  m_Replay->ClearScrubReplayCache();
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    LogD3D12Checkpoint(StringFormat::Fmt(
        "api=D3D12BufferPatch op=set ranges=%zu bytes=%llu resetBuffers=%zu firstConsumer=%u "
        "sameLayout=%u firstAffected=%u anchor=%u retainedCheckpoints=%zu "
        "deltaKnown=%u deltaEnabled=%u changedRanges=%zu changedBytes=%llu changedBuffers=%zu "
        "uploadReused=%u uploadWrittenBytes=%llu rangeIndexKnown=%u rangeConsumer=%u "
        "initialSourcesPreserved=1 frameInitialFastPath=0 consumerBoundary=nativeRangeOrSubmitBase",
        patches.size(), (unsigned long long)bytes, buffers.size(), firstConsumer,
        sameLayout ? 1U : 0U, m_ReplayBufferPatchFirstAffected, m_ReplayBufferPatchAnchorEvent,
        m_ReplayCheckpoints ? m_ReplayCheckpoints->checkpoints.size() : 0, deltaKnown ? 1U : 0U,
        deltaEnabled ? 1U : 0U, changes.size(), (unsigned long long)changedBytes,
        changedBuffers.size(), reuseUpload ? 1U : 0U, (unsigned long long)uploadWrittenBytes,
        rangeIndexKnown ? 1U : 0U, rangeConsumer));
  return {};
}

void WrappedID3D12Device::PatchReplayBufferCPU(ResourceId buffer, uint64_t offset, void *data,
                                              uint64_t length)
{
  if(!m_ReplayBufferPatches.empty())
    ApplyReplayBufferPatches(m_ReplayBufferPatches, buffer, offset, data, length);
}

void WrappedID3D12Device::RecordReplayBufferPatches(ID3D12GraphicsCommandList *list,
                                                   ID3D12Resource *buffer)
{
  if(!m_ReplayBufferPatchUpload || !buffer)
    return;
  const ResourceId id = GetResID(buffer);
  for(size_t i = 0; i < m_ReplayBufferPatches.size(); i++)
  {
    const ReplayBufferPatch &patch = m_ReplayBufferPatches[i];
    if(patch.buffer == id)
    {
      // Map replay re-applies every persistent patch, including bytes outside the CPU-write
      // chunk's range. These writes belong to the same dirty interval union.
      if(!m_ReplayDirtyApplyingInitial)
        MarkReplayBufferRangeDirty(id, patch.byteOffset, patch.data.size());
      Unwrap(list)->CopyBufferRegion(Unwrap(buffer), patch.byteOffset, m_ReplayBufferPatchUpload,
                                      m_ReplayBufferPatchOffsets[i], patch.data.size());
    }
  }
}

void WrappedID3D12Device::ApplyInitialReplayBufferPatches()
{
  ResourceId previous;
  for(const ReplayBufferPatch &patch : m_ReplayBufferPatches)
  {
    if(patch.buffer == previous)
      continue;
    previous = patch.buffer;
    if(m_UseCheckpointInitialSources && m_ReplayCheckpoints)
    {
      const auto point = m_ReplayCheckpoints->checkpoints.find(m_ReplayCheckpoints->restorePlan.eventId);
      if(point != m_ReplayCheckpoints->checkpoints.end() &&
         point->second.resources.find(patch.buffer) != point->second.resources.end())
        continue;
    }
    auto found = GetResourceList().find(patch.buffer);
    if(found == GetResourceList().end())
      continue;
    WrappedID3D12Resource *resource = found->second;
    D3D12_HEAP_PROPERTIES properties = {};
    resource->GetReal()->GetHeapProperties(&properties, NULL);
    if(properties.Type == D3D12_HEAP_TYPE_UPLOAD)
    {
      void *mapped = NULL;
      const D3D12_RANGE noRead = {0, 0};
      HRESULT hr = resource->GetReal()->Map(0, &noRead, &mapped);
      CHECK_HR(this, hr);
      if(SUCCEEDED(hr) && mapped)
      {
        const uint64_t width = resource->GetReal()->GetDesc().Width;
        PatchReplayBufferCPU(patch.buffer, 0, mapped, width);
        const D3D12_RANGE written = {0, (SIZE_T)width};
        resource->GetReal()->Unmap(0, &written);
      }
    }
    else
    {
      ID3D12GraphicsCommandListX *list = GetInitialStateList();
      if(!list)
        return;
      BarrierSet barriers;
      barriers.Configure(resource, GetSubresourceStates(patch.buffer), BarrierSet::CopyDestAccess);
      barriers.Apply(list);
      RecordReplayBufferPatches(list, resource);
      barriers.Unapply(list);
    }
  }
}

bool WrappedID3D12Device::ApplyCheckpointReplayBufferPatches()
{
  if(!m_ReplayCheckpoints || m_ReplayBufferPatches.empty())
    return true;
  auto &cache = *m_ReplayCheckpoints;
  std::set<ResourceId> buffers;
  for(const ReplayBufferPatch &patch : m_ReplayBufferPatches)
    if(cache.currentVersions.count(patch.buffer))
      buffers.insert(patch.buffer);
  if(buffers.empty())
    return true;    // ApplyInitialReplayBufferPatches already repaired shared initial-source buffers

  PerformanceTimer timer;
  bool cpu = false;
  for(ResourceId id : buffers)
  {
    const auto resource = GetResourceList().find(id);
    D3D12_HEAP_PROPERTIES properties = {};
    if(resource == GetResourceList().end() ||
       FAILED(resource->second->GetReal()->GetHeapProperties(&properties, NULL)))
      return false;
    cpu |= properties.Type == D3D12_HEAP_TYPE_UPLOAD;
  }
  if(cpu && !DeviceWaitForIdle())
    return false;
  if(HasFatalError())
    return false;
  const bool descriptorIndependent = m_InternalCmds.pendingcmds.empty();
  ID3D12GraphicsCommandListX *list = NULL;
  uint64_t bytes = 0;
  for(ResourceId id : buffers)
  {
    WrappedID3D12Resource *resource = GetResourceList().find(id)->second;
    MarkReplayResourceDirty(id);
    D3D12_HEAP_PROPERTIES properties = {};
    resource->GetReal()->GetHeapProperties(&properties, NULL);
    if(properties.Type == D3D12_HEAP_TYPE_UPLOAD)
    {
      void *mapped = NULL;
      const D3D12_RANGE noRead = {0, 0};
      HRESULT hr = resource->GetReal()->Map(0, &noRead, &mapped);
      CHECK_HR(this, hr);
      if(FAILED(hr) || !mapped)
        return false;
      PatchReplayBufferCPU(id, 0, mapped, resource->GetReal()->GetDesc().Width);
      const D3D12_RANGE written = {0, (SIZE_T)resource->GetReal()->GetDesc().Width};
      resource->GetReal()->Unmap(0, &written);
    }
    else
    {
      if(!list)
        list = GetNewList();
      if(!list)
        return false;
      BarrierSet barriers;
      barriers.Configure(resource, GetSubresourceStates(id), BarrierSet::CopyDestAccess);
      barriers.Apply(list);
      RecordReplayBufferPatches(list, resource);
      barriers.Unapply(list);
    }
    for(const ReplayBufferPatch &patch : m_ReplayBufferPatches)
      if(patch.buffer == id)
        bytes += patch.data.size();
    // The saved version is immutable and still contains the preceding edit's values. It must
    // not be shared as the current buffer version when a later point is built in this replay.
    cache.currentVersions.erase(id);
    cache.residentVersions.Invalidate(cache.ResidentRange(id));
  }
  if(list)
  {
    CHECK_HR(this, list->Close());
    ExecuteLists(NULL, false, descriptorIndependent);
    if(!InstallInitialStateQueueDependencies() && !FlushLists(true))
      return false;
  }
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    LogD3D12Checkpoint(StringFormat::Fmt(
        "api=D3D12BufferPatch op=checkpointRepair event=%u buffers=%zu bytes=%llu cpu=%u total_ms=%.3f",
        cache.stats.lastResumeEvent, buffers.size(), (unsigned long long)bytes, cpu ? 1U : 0U,
        timer.GetMilliseconds()));
  return !HasFatalError();
}

void WrappedID3D12Device::RemoveQueue(WrappedID3D12CommandQueue *queue)
{
  const int32_t index = m_Queues.indexOf(queue);
  if(index < 0)
    return;

  m_Queues.erase((size_t)index);

  // Replay queues and their private synchronisation fences are parallel arrays. Keeping the old
  // fence index after a non-primary queue is destroyed can make later GPU queue hand-offs wait on
  // the wrong fence.
  if((size_t)index < m_QueueFences.size())
  {
    SAFE_RELEASE(m_QueueFences[(size_t)index]);
    m_QueueFences.erase((size_t)index);
  }
}

rdcstr WrappedID3D12Device::GetChunkName(uint32_t idx)
{
  if((SystemChunk)idx < SystemChunk::FirstDriverChunk)
    return ToStr((SystemChunk)idx);

  return ToStr((D3D12Chunk)idx);
}

D3D12ShaderCache *WrappedID3D12Device::GetShaderCache()
{
  if(m_ShaderCache == NULL)
    m_ShaderCache = new D3D12ShaderCache(this);
  return m_ShaderCache;
}

D3D12DebugManager *WrappedID3D12Device::GetDebugManager()
{
  return m_Replay->GetDebugManager();
}

ULONG STDMETHODCALLTYPE DummyID3D12InfoQueue::AddRef()
{
  m_pDevice->AddRef();
  return 1;
}

ULONG STDMETHODCALLTYPE DummyID3D12InfoQueue::Release()
{
  m_pDevice->Release();
  return 1;
}

ULONG STDMETHODCALLTYPE DummyID3D12DebugDevice::AddRef()
{
  m_pDevice->AddRef();
  return 1;
}

ULONG STDMETHODCALLTYPE DummyID3D12DebugDevice::Release()
{
  m_pDevice->Release();
  return 1;
}

HRESULT STDMETHODCALLTYPE DummyID3D12DebugDevice::QueryInterface(REFIID riid, void **ppvObject)
{
  if(riid == __uuidof(ID3D12InfoQueue) || riid == __uuidof(ID3D12InfoQueue1) ||
     riid == __uuidof(ID3D12DebugDevice) || riid == __uuidof(ID3D12Device) ||
     riid == __uuidof(ID3D12Device1) || riid == __uuidof(ID3D12Device2) ||
     riid == __uuidof(ID3D12Device3) || riid == __uuidof(ID3D12Device4) ||
     riid == __uuidof(ID3D12Device5) || riid == __uuidof(ID3D12Device6) ||
     riid == __uuidof(ID3D12Device7) || riid == __uuidof(ID3D12Device8) ||
     riid == __uuidof(ID3D12Device9) || riid == __uuidof(ID3D12Device10) ||
     riid == __uuidof(ID3D12Device11) || riid == __uuidof(ID3D12Device12) ||
     riid == __uuidof(ID3D12Device13) || riid == __uuidof(ID3D12Device14) ||
     riid == __uuidof(ID3D12Device15))
    return m_pDevice->QueryInterface(riid, ppvObject);

  if(riid == __uuidof(IUnknown))
  {
    *ppvObject = (IUnknown *)(ID3D12DebugDevice *)this;
    AddRef();
    return S_OK;
  }

  WarnUnknownGUID("ID3D12DebugDevice", riid);

  return E_NOINTERFACE;
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugDevice::AddRef()
{
  m_pDevice->AddRef();
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugDevice::Release()
{
  m_pDevice->Release();
  return 1;
}

HRESULT STDMETHODCALLTYPE WrappedID3D12DebugDevice::QueryInterface(REFIID riid, void **ppvObject)
{
  if(riid == __uuidof(ID3D12InfoQueue) || riid == __uuidof(ID3D12InfoQueue1) ||
     riid == __uuidof(ID3D12DebugDevice) || riid == __uuidof(ID3D12Device) ||
     riid == __uuidof(ID3D12Device1) || riid == __uuidof(ID3D12Device2) ||
     riid == __uuidof(ID3D12Device3) || riid == __uuidof(ID3D12Device4) ||
     riid == __uuidof(ID3D12Device5) || riid == __uuidof(ID3D12Device6) ||
     riid == __uuidof(ID3D12Device7) || riid == __uuidof(ID3D12Device8) ||
     riid == __uuidof(ID3D12Device9) || riid == __uuidof(ID3D12Device10) ||
     riid == __uuidof(ID3D12Device11) || riid == __uuidof(ID3D12Device12) ||
     riid == __uuidof(ID3D12Device13) || riid == __uuidof(ID3D12Device14) ||
     riid == __uuidof(ID3D12Device15))
    return m_pDevice->QueryInterface(riid, ppvObject);

  if(riid == __uuidof(IUnknown))
  {
    *ppvObject = (IUnknown *)(ID3D12DebugDevice *)this;
    AddRef();
    return S_OK;
  }

  WarnUnknownGUID("ID3D12DebugDevice", riid);

  return m_pDebug->QueryInterface(riid, ppvObject);
}

HRESULT STDMETHODCALLTYPE WrappedDownlevelDevice::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pDevice.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedDownlevelDevice::AddRef()
{
  return m_pDevice.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedDownlevelDevice::Release()
{
  return m_pDevice.Release();
}

HRESULT STDMETHODCALLTYPE WrappedDownlevelDevice::QueryVideoMemoryInfo(
    UINT NodeIndex, DXGI_MEMORY_SEGMENT_GROUP MemorySegmentGroup,
    _Out_ DXGI_QUERY_VIDEO_MEMORY_INFO *pVideoMemoryInfo)
{
  return m_pDevice.QueryVideoMemoryInfo(NodeIndex, MemorySegmentGroup, pVideoMemoryInfo);
}

HRESULT STDMETHODCALLTYPE WrappedDeviceTools::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pDevice.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedDeviceTools::AddRef()
{
  return m_pDevice.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedDeviceTools::Release()
{
  return m_pDevice.Release();
}

void STDMETHODCALLTYPE WrappedDeviceTools::SetNextAllocationAddress(UINT64 pVirtualAddress)
{
  return m_pDevice.SetNextAllocationAddress(pVirtualAddress);
}
HRESULT STDMETHODCALLTYPE
WrappedDeviceTools::GetApplicationSpecificDriverState(_COM_Outptr_ ID3DBlob **ppBlob)
{
  return m_pDevice.GetApplicationSpecificDriverState(ppBlob);
}

D3D12_APPLICATION_SPECIFIC_DRIVER_BLOB_STATUS STDMETHODCALLTYPE
WrappedDeviceTools::GetApplicationSpecificDriverBlobStatus(void)
{
  return m_pDevice.GetApplicationSpecificDriverBlobStatus();
}

HRESULT STDMETHODCALLTYPE WrappedID3D12SharingContract::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pDevice.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedID3D12SharingContract::AddRef()
{
  return m_pDevice.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedID3D12SharingContract::Release()
{
  return m_pDevice.Release();
}

ID3DDevice *WrappedID3D12SharingContract::GetD3DDevice()
{
  return &m_pDevice;
}

UINT WrappedID3D12SharingContract::GetWidth()
{
  D3D12_RESOURCE_DESC desc = m_pPresentSource->GetDesc();
  return (UINT)desc.Width;
}

UINT WrappedID3D12SharingContract::GetHeight()
{
  D3D12_RESOURCE_DESC desc = m_pPresentSource->GetDesc();
  return desc.Height;
}

DXGI_FORMAT WrappedID3D12SharingContract::GetFormat()
{
  D3D12_RESOURCE_DESC desc = m_pPresentSource->GetDesc();
  return desc.Format;
}

HWND WrappedID3D12SharingContract::GetHWND()
{
  return m_pPresentHWND;
}

void STDMETHODCALLTYPE WrappedID3D12SharingContract::Present(_In_ ID3D12Resource *pResource,
                                                             UINT Subresource, _In_ HWND window)
{
  // this is similar to the downlevel queue present

  if(m_pPresentHWND != NULL)
  {
    // don't let the device actually release any refs on the resource, just make it release internal
    // resources
    m_pPresentSource->AddRef();
    m_pDevice.ReleaseSwapchainResources(this, 0, NULL, NULL);
  }

  if(m_pPresentHWND != window)
  {
    if(m_pPresentHWND != NULL)
    {
      Keyboard::RemoveInputWindow(WindowingSystem::Win32, m_pPresentHWND);
      RenderDoc::Inst().RemoveFrameCapturer(
          DeviceOwnedWindow(m_pDevice.GetFrameCapturerDevice(), m_pPresentHWND));
    }

    Keyboard::AddInputWindow(WindowingSystem::Win32, window);

    RenderDoc::Inst().AddFrameCapturer(
        DeviceOwnedWindow(m_pDevice.GetFrameCapturerDevice(), window), m_pDevice.GetFrameCapturer());
  }

  m_pPresentSource = pResource;
  m_pPresentHWND = window;

  m_pDevice.WrapSwapchainBuffer(this, GetFormat(), 0, m_pPresentSource);

  m_pDevice.Present(NULL, this, 0, 0);

  if(m_pReal != NULL)
    m_pReal->Present(Unwrap(pResource), Subresource, window);
}

void STDMETHODCALLTYPE WrappedID3D12SharingContract::SharedFenceSignal(_In_ ID3D12Fence *pFence,
                                                                       UINT64 FenceValue)
{
  if(m_pReal != NULL)
    m_pReal->SharedFenceSignal(Unwrap(pFence), FenceValue);
}

void STDMETHODCALLTYPE WrappedID3D12SharingContract::BeginCapturableWork(_In_ REFGUID guid)
{
  // undocumented what this does
  if(m_pReal != NULL)
    m_pReal->BeginCapturableWork(guid);
}

void STDMETHODCALLTYPE WrappedID3D12SharingContract::EndCapturableWork(_In_ REFGUID guid)
{
  // undocumented what this does
  if(m_pReal != NULL)
    m_pReal->EndCapturableWork(guid);
}

HRESULT STDMETHODCALLTYPE WrappedDRED::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pDevice.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedDRED::AddRef()
{
  return m_pDevice.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedDRED::Release()
{
  return m_pDevice.Release();
}

HRESULT STDMETHODCALLTYPE WrappedDREDSettings::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pDevice.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedDREDSettings::AddRef()
{
  return m_pDevice.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedDREDSettings::Release()
{
  return m_pDevice.Release();
}

HRESULT STDMETHODCALLTYPE WrappedDeviceStatistics::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pDevice.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedDeviceStatistics::AddRef()
{
  return m_pDevice.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedDeviceStatistics::Release()
{
  return m_pDevice.Release();
}

HRESULT STDMETHODCALLTYPE
WrappedDeviceStatistics::GetStateObjectStatistics(D3D12_STATE_OBJECT_STATISTICS *pStatistics)
{
  return m_pReal->GetStateObjectStatistics(pStatistics);
}

HRESULT STDMETHODCALLTYPE WrappedCompatibilityDevice::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pDevice.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedCompatibilityDevice::AddRef()
{
  return m_pDevice.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedCompatibilityDevice::Release()
{
  return m_pDevice.Release();
}

WriteSerialiser &WrappedCompatibilityDevice::GetThreadSerialiser()
{
  return m_pDevice.GetThreadSerialiser();
}

HRESULT STDMETHODCALLTYPE WrappedCompatibilityDevice::CreateSharedResource(
    _In_ const D3D12_HEAP_PROPERTIES *pHeapProperties, D3D12_HEAP_FLAGS HeapFlags,
    _In_ const D3D12_RESOURCE_DESC *pDesc, D3D12_RESOURCE_STATES InitialResourceState,
    _In_opt_ const D3D12_CLEAR_VALUE *pOptimizedClearValue,
    _In_opt_ const D3D11_RESOURCE_FLAGS *pFlags11,
    D3D12_COMPATIBILITY_SHARED_FLAGS CompatibilityFlags,
    _In_opt_ ID3D12LifetimeTracker *pLifetimeTracker,
    _In_opt_ ID3D12SwapChainAssistant *pOwningSwapchain, REFIID riid,
    _COM_Outptr_opt_ void **ppResource)
{
  if(ppResource == NULL)
    return E_INVALIDARG;

  HRESULT hr;

  // not exactly sure what to do with these...
  RDCASSERT(pLifetimeTracker == NULL);
  RDCASSERT(pOwningSwapchain == NULL);

  SERIALISE_TIME_CALL(
      hr = m_pReal->CreateSharedResource(pHeapProperties, HeapFlags, pDesc, InitialResourceState,
                                         pOptimizedClearValue, pFlags11, CompatibilityFlags,
                                         pLifetimeTracker, pOwningSwapchain, riid, ppResource));

  if(FAILED(hr))
  {
    IUnknown *unk = (IUnknown *)*ppResource;
    SAFE_RELEASE(unk);
    return hr;
  }

  return m_pDevice.OpenSharedHandleInternal(D3D12Chunk::CompatDevice_CreateSharedResource,
                                            HeapFlags, riid, ppResource);
}

HRESULT STDMETHODCALLTYPE WrappedCompatibilityDevice::CreateSharedHeap(
    _In_ const D3D12_HEAP_DESC *pHeapDesc, D3D12_COMPATIBILITY_SHARED_FLAGS CompatibilityFlags,
    REFIID riid, _COM_Outptr_opt_ void **ppHeap)
{
  if(ppHeap == NULL)
    return E_INVALIDARG;

  HRESULT hr;

  SERIALISE_TIME_CALL(hr = m_pReal->CreateSharedHeap(pHeapDesc, CompatibilityFlags, riid, ppHeap));

  if(FAILED(hr))
  {
    IUnknown *unk = (IUnknown *)*ppHeap;
    SAFE_RELEASE(unk);
    return hr;
  }

  return m_pDevice.OpenSharedHandleInternal(D3D12Chunk::CompatDevice_CreateSharedHeap,
                                            pHeapDesc->Flags, riid, ppHeap);
}

HRESULT STDMETHODCALLTYPE WrappedCompatibilityDevice::ReflectSharedProperties(
    _In_ ID3D12Object *pHeapOrResource, D3D12_REFLECT_SHARED_PROPERTY ReflectType,
    _Out_writes_bytes_(DataSize) void *pData, UINT DataSize)
{
  return m_pReal->ReflectSharedProperties(Unwrap(pHeapOrResource), ReflectType, pData, DataSize);
}

HRESULT STDMETHODCALLTYPE WrappedNVAPI12::QueryInterface(REFIID riid, void **ppvObject)
{
  return E_NOINTERFACE;
}

ULONG STDMETHODCALLTYPE WrappedNVAPI12::AddRef()
{
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedNVAPI12::Release()
{
  return 1;
}

BOOL STDMETHODCALLTYPE WrappedNVAPI12::SetReal(IUnknown *)
{
  // shouldn't be called on capture, do nothing
  return FALSE;
}

IUnknown *STDMETHODCALLTYPE WrappedNVAPI12::GetReal()
{
  return m_pDevice.GetReal();
}

BOOL STDMETHODCALLTYPE WrappedNVAPI12::SetShaderExtUAV(DWORD space, DWORD reg, BOOL global)
{
  m_pDevice.SetShaderExtUAV(GPUVendor::nVidia, reg, space, global ? true : false);
  return TRUE;
}

void WrappedNVAPI12::UnwrapDesc(D3D12_GRAPHICS_PIPELINE_STATE_DESC *pDesc)
{
  pDesc->pRootSignature = Unwrap(pDesc->pRootSignature);
}

void WrappedNVAPI12::UnwrapDesc(D3D12_COMPUTE_PIPELINE_STATE_DESC *pDesc)
{
  pDesc->pRootSignature = Unwrap(pDesc->pRootSignature);
}

ID3D12PipelineState *WrappedNVAPI12::ProcessCreatedGraphicsPipelineState(
    const D3D12_GRAPHICS_PIPELINE_STATE_DESC *pDesc, uint32_t reg, uint32_t space,
    ID3D12PipelineState *realPSO)
{
  ID3D12PipelineState *ret = NULL;
  m_pDevice.SetShaderExt(GPUVendor::nVidia);
  m_pDevice.ProcessCreatedGraphicsPSO(realPSO, reg, space, pDesc, __uuidof(ID3D12PipelineState),
                                      (void **)&ret);
  return ret;
}

ID3D12PipelineState *WrappedNVAPI12::ProcessCreatedComputePipelineState(
    const D3D12_COMPUTE_PIPELINE_STATE_DESC *pDesc, uint32_t reg, uint32_t space,
    ID3D12PipelineState *realPSO)
{
  ID3D12PipelineState *ret = NULL;
  m_pDevice.SetShaderExt(GPUVendor::nVidia);
  m_pDevice.ProcessCreatedComputePSO(realPSO, reg, space, pDesc, __uuidof(ID3D12PipelineState),
                                     (void **)&ret);
  return ret;
}

HRESULT STDMETHODCALLTYPE WrappedAGS12::QueryInterface(REFIID riid, void **ppvObject)
{
  return E_NOINTERFACE;
}

ULONG STDMETHODCALLTYPE WrappedAGS12::AddRef()
{
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedAGS12::Release()
{
  return 1;
}

IUnknown *STDMETHODCALLTYPE WrappedAGS12::GetReal()
{
  return m_pDevice.GetReal();
}

BOOL STDMETHODCALLTYPE WrappedAGS12::SetShaderExtUAV(DWORD space, DWORD reg)
{
  m_pDevice.SetShaderExtUAV(GPUVendor::AMD, reg, space, true);
  return TRUE;
}

HRESULT STDMETHODCALLTYPE WrappedAGS12::CreateD3D11(IDXGIAdapter *, D3D_DRIVER_TYPE, HMODULE, UINT,
                                                    CONST D3D_FEATURE_LEVEL *, UINT FeatureLevels,
                                                    UINT, CONST DXGI_SWAP_CHAIN_DESC *,
                                                    IDXGISwapChain **, ID3D11Device **,
                                                    D3D_FEATURE_LEVEL *, ID3D11DeviceContext **)
{
  // shouldn't be called on capture
  return E_NOTIMPL;
}
HRESULT STDMETHODCALLTYPE WrappedAGS12::CreateD3D12(IUnknown *pAdapter,
                                                    D3D_FEATURE_LEVEL MinimumFeatureLevel,
                                                    REFIID riid, void **ppDevice)
{
  // shouldn't be called on capture
  return E_NOTIMPL;
}

BOOL STDMETHODCALLTYPE WrappedAGS12::ExtensionsSupported()
{
  return FALSE;
}

WrappedID3D12Device::WrappedID3D12Device(ID3D12Device *realDevice, D3D12InitParams params,
                                         bool enabledDebugLayer)
    : m_RefCounter(realDevice, false),
      m_DevConfig(realDevice, this),
      m_SoftRefCounter(NULL, false),
      m_pDevice(realDevice),
      m_debugLayerEnabled(enabledDebugLayer),
      m_WrappedDownlevel(*this),
      m_WrappedDeviceTools(*this),
      m_DRED(*this),
      m_DREDSettings(*this),
      m_SharingContract(*this),
      m_DeviceStats(*this),
      m_CompatDevice(*this),
      m_WrappedNVAPI(*this),
      m_WrappedAGS(*this)
{
  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(WrappedID3D12Device));

  m_SectionVersion = D3D12InitParams::CurrentVersion;

  m_Replay = new D3D12Replay(this);

  m_StructuredFile = m_StoredStructuredData = new SDFile;

  RDCEraseEl(m_D3D12Opts);
  RDCEraseEl(m_D3D12Opts1);
  RDCEraseEl(m_D3D12Opts2);
  RDCEraseEl(m_D3D12Opts3);
  RDCEraseEl(m_D3D12Opts5);
  RDCEraseEl(m_D3D12Opts6);
  RDCEraseEl(m_D3D12Opts7);
  RDCEraseEl(m_D3D12Opts9);
  RDCEraseEl(m_D3D12Opts12);
  RDCEraseEl(m_D3D12Opts14);
  RDCEraseEl(m_D3D12Opts15);
  RDCEraseEl(m_D3D12Opts16);

  m_pDevice1 = NULL;
  m_pDevice2 = NULL;
  m_pDevice3 = NULL;
  m_pDevice4 = NULL;
  m_pDevice5 = NULL;
  m_pDevice6 = NULL;
  m_pDevice7 = NULL;
  m_pDevice8 = NULL;
  m_pDevice9 = NULL;
  m_pDevice10 = NULL;
  m_pDevice11 = NULL;
  m_pDevice12 = NULL;
  m_pDevice13 = NULL;
  m_pDevice14 = NULL;
  m_pDevice15 = NULL;
  m_pDownlevel = NULL;
  if(m_pDevice)
  {
    m_pDevice->QueryInterface(__uuidof(ID3D12Device1), (void **)&m_pDevice1);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device2), (void **)&m_pDevice2);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device3), (void **)&m_pDevice3);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device4), (void **)&m_pDevice4);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device5), (void **)&m_pDevice5);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device6), (void **)&m_pDevice6);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device7), (void **)&m_pDevice7);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device8), (void **)&m_pDevice8);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device9), (void **)&m_pDevice9);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device10), (void **)&m_pDevice10);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device11), (void **)&m_pDevice11);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device12), (void **)&m_pDevice12);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device13), (void **)&m_pDevice13);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device14), (void **)&m_pDevice14);
    m_pDevice->QueryInterface(__uuidof(ID3D12Device15), (void **)&m_pDevice15);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedData), (void **)&m_DRED.m_pReal);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedData1), (void **)&m_DRED.m_pReal1);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedDataSettings),
                              (void **)&m_DREDSettings.m_pReal);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedDataSettings1),
                              (void **)&m_DREDSettings.m_pReal1);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedDataSettings2),
                              (void **)&m_DREDSettings.m_pReal2);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceDownlevel), (void **)&m_pDownlevel);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceTools), (void **)&m_pDeviceTools);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceTools1), (void **)&m_pDeviceTools1);
    m_pDevice->QueryInterface(__uuidof(ID3D12CompatibilityDevice), (void **)&m_CompatDevice.m_pReal);
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceStatistics), (void **)&m_DeviceStats.m_pReal);
    m_pDevice->QueryInterface(__uuidof(ID3D12SharingContract), (void **)&m_SharingContract.m_pReal);

    for(size_t i = 0; i < ARRAY_COUNT(m_DescriptorIncrements); i++)
      m_DescriptorIncrements[i] =
          m_pDevice->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE(i));

    HRESULT hr = S_OK;

    m_RootSigVersion = D3D_ROOT_SIGNATURE_VERSION_1_0;
    for(D3D_ROOT_SIGNATURE_VERSION verToCheck :
        {D3D_ROOT_SIGNATURE_VERSION_1_2, D3D_ROOT_SIGNATURE_VERSION_1_1,
         D3D_ROOT_SIGNATURE_VERSION_1_0})
    {
      D3D12_FEATURE_DATA_ROOT_SIGNATURE rootSigVer = {verToCheck};
      hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_ROOT_SIGNATURE, &rootSigVer,
                                          sizeof(rootSigVer));
      if(hr == S_OK)
      {
        m_RootSigVersion = rootSigVer.HighestVersion;
        break;
      }
    }

    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS, &m_D3D12Opts,
                                        sizeof(m_D3D12Opts));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS1, &m_D3D12Opts1,
                                        sizeof(m_D3D12Opts1));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts1);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS2, &m_D3D12Opts2,
                                        sizeof(m_D3D12Opts2));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts2);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS3, &m_D3D12Opts3,
                                        sizeof(m_D3D12Opts3));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts3);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS5, &m_D3D12Opts5,
                                        sizeof(m_D3D12Opts5));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts5);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS6, &m_D3D12Opts6,
                                        sizeof(m_D3D12Opts6));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts6);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS7, &m_D3D12Opts7,
                                        sizeof(m_D3D12Opts7));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts7);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS9, &m_D3D12Opts9,
                                        sizeof(m_D3D12Opts9));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts9);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS12, &m_D3D12Opts12,
                                        sizeof(m_D3D12Opts12));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts12);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS14, &m_D3D12Opts14,
                                        sizeof(m_D3D12Opts14));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts14);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS15, &m_D3D12Opts15,
                                        sizeof(m_D3D12Opts15));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts15);
    hr = m_pDevice->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS16, &m_D3D12Opts16,
                                        sizeof(m_D3D12Opts16));
    if(hr != S_OK)
      RDCEraseEl(m_D3D12Opts16);
  }

  // refcounters implicitly construct with one reference, but we don't start with any soft
  // references.
  m_SoftRefCounter.Release();
  m_InternalRefcount = 0;
  m_Alive = true;

  m_DummyInfoQueue.m_pDevice = this;
  m_DummyDebug.m_pDevice = this;
  m_WrappedDebug.m_pDevice = this;

  threadSerialiserTLSSlot = Threading::AllocateTLSSlot();
  tempMemoryTLSSlot = Threading::AllocateTLSSlot();

  m_HeaderChunk = NULL;

  m_Alloc = m_DataUploadAlloc = NULL;
  m_WFIFence = NULL;
  m_WFIHandle = NULL;
  m_WFICounter = 0;
  m_OverlaySyncHandle = NULL;

  initStateCurBatch = 0;
  initStateCurList = NULL;

  m_InitParams = params;

  if(RenderDoc::Inst().IsReplayApp())
  {
    m_State = CaptureState::LoadingReplaying;

    if(realDevice)
    {
      m_ResourceList = new std::map<ResourceId, WrappedID3D12Resource *>();
      m_PipelineList = new rdcarray<WrappedID3D12PipelineState *>();
    }

    m_FrameCaptureRecord = NULL;

    ResourceIDGen::SetReplayResourceIDs();

    HMODULE D3D12Core = GetModuleHandleA("D3D12Core.dll");
    if(D3D12Core != NULL)
    {
      DWORD *ver_ptr = (DWORD *)GetProcAddress(D3D12Core, "D3D12SDKVersion");

      if(ver_ptr && *ver_ptr >= 700 && *ver_ptr <= 706)
      {
        RDCLOG("Disabling new barrier support on older beta SDK");
        m_D3D12Opts12.EnhancedBarriersSupported = FALSE;
      }
    }
  }
  else
  {
    m_State = CaptureState::BackgroundCapturing;

    if(m_pDevice)
    {
      typedef HRESULT(WINAPI * PFN_CREATE_DXGI_FACTORY)(REFIID, void **);

      PFN_CREATE_DXGI_FACTORY createFunc = (PFN_CREATE_DXGI_FACTORY)GetProcAddress(
          GetModuleHandleA("dxgi.dll"), "CreateDXGIFactory1");

      IDXGIFactory1 *tmpFactory = NULL;
      HRESULT hr = createFunc(__uuidof(IDXGIFactory1), (void **)&tmpFactory);

      if(FAILED(hr))
      {
        RDCERR("Couldn't create DXGI factory! HRESULT: %s", ToStr(hr).c_str());
      }

      if(tmpFactory)
      {
        IDXGIAdapter *pDXGIAdapter = NULL;
        hr = EnumAdapterByLuid(tmpFactory, m_pDevice->GetAdapterLuid(), &pDXGIAdapter);

        if(FAILED(hr))
        {
          RDCERR("Couldn't get DXGI adapter by LUID from D3D12 device");
        }
        else
        {
          DXGI_ADAPTER_DESC desc = {};
          pDXGIAdapter->GetDesc(&desc);
          LARGE_INTEGER version = {};
          pDXGIAdapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &version);

          m_InitParams.AdapterDesc = desc;

          GPUVendor vendor = GPUVendorFromPCIVendor(desc.VendorId);
          rdcstr descString = GetDriverVersion(desc, version);

          RDCLOG("New D3D12 device created: %s / %s", ToStr(vendor).c_str(), descString.c_str());

          SAFE_RELEASE(pDXGIAdapter);
        }
      }

      SAFE_RELEASE(tmpFactory);
    }
  }

  m_ResourceManager = new D3D12ResourceManager(m_State, this);

  m_ResourceID = ResourceIDGen::GetNewUniqueID();

  m_DeviceRecord = NULL;

  m_Queue = NULL;

  if(!RenderDoc::Inst().IsReplayApp())
  {
    m_DeviceRecord = GetResourceManager()->AddResourceRecord(m_ResourceID);
    m_DeviceRecord->type = Resource_Device;
    m_DeviceRecord->DataInSerialiser = false;
    m_DeviceRecord->InternalResource = true;
    m_DeviceRecord->Length = 0;

    m_FrameCaptureRecord = GetResourceManager()->AddResourceRecord(ResourceIDGen::GetNewUniqueID());
    m_FrameCaptureRecord->DataInSerialiser = false;
    m_FrameCaptureRecord->InternalResource = true;
    m_FrameCaptureRecord->Length = 0;

    RenderDoc::Inst().AddDeviceFrameCapturer((ID3D12Device *)this, this);
  }

  m_pInfoQueue = NULL;
  m_WrappedDebug.m_pDebug = NULL;
  m_WrappedDebug.m_pDebug1 = NULL;
  m_WrappedDebug.m_pDebug2 = NULL;
  if(m_pDevice)
  {
    m_pDevice->QueryInterface(__uuidof(ID3D12InfoQueue), (void **)&m_pInfoQueue);
    m_pDevice->QueryInterface(__uuidof(ID3D12DebugDevice), (void **)&m_WrappedDebug.m_pDebug);
    m_pDevice->QueryInterface(__uuidof(ID3D12DebugDevice1), (void **)&m_WrappedDebug.m_pDebug1);
    m_pDevice->QueryInterface(__uuidof(ID3D12DebugDevice2), (void **)&m_WrappedDebug.m_pDebug2);
  }

  if(m_pInfoQueue)
  {
    if(RenderDoc::Inst().GetCaptureOptions().debugOutputMute)
      m_pInfoQueue->SetMuteDebugOutput(true);

    UINT size = m_pInfoQueue->GetStorageFilterStackSize();

    while(size > 1)
    {
      m_pInfoQueue->ClearStorageFilter();
      size = m_pInfoQueue->GetStorageFilterStackSize();
    }

    size = m_pInfoQueue->GetRetrievalFilterStackSize();

    while(size > 1)
    {
      m_pInfoQueue->ClearRetrievalFilter();
      size = m_pInfoQueue->GetRetrievalFilterStackSize();
    }

    m_pInfoQueue->ClearStoredMessages();

    if(RenderDoc::Inst().IsReplayApp())
    {
      m_pInfoQueue->SetMuteDebugOutput(false);

      D3D12_MESSAGE_ID mute[] = {
          // the runtime cries foul when you use normal APIs in expected ways (for simple markers)
          D3D12_MESSAGE_ID_CORRUPTED_PARAMETER2,

          // super spammy, mostly just perf warning, and impossible to fix for our cases
          D3D12_MESSAGE_ID_CLEARRENDERTARGETVIEW_MISMATCHINGCLEARVALUE,
          D3D12_MESSAGE_ID_CLEARDEPTHSTENCILVIEW_MISMATCHINGCLEARVALUE,

          // caused often by an over-declaration in the root signature to match between
          // different shaders, and in some descriptors are entirely skipped. We rely on
          // the user to get this right - if the error is non-fatal, any real problems
          // will be potentially highlighted in the pipeline view
          D3D12_MESSAGE_ID_INVALID_DESCRIPTOR_HANDLE,
          D3D12_MESSAGE_ID_COMMAND_LIST_DESCRIPTOR_TABLE_NOT_SET,

          // message about a NULL range to map for reading/writing the whole resource
          // which is "inefficient" but for our use cases it's almost always what we mean.
          D3D12_MESSAGE_ID_MAP_INVALID_NULLRANGE,

          // D3D12_MESSAGE_ID_COMMAND_LIST_STATIC_DESCRIPTOR_RESOURCE_DIMENSION_MISMATCH
          // message about mismatched SRV dimensions, which it seems to get wrong with the
          // dummy NULL descriptors on the texture sampling code
          D3D12_MESSAGE_ID_COMMAND_LIST_STATIC_DESCRIPTOR_RESOURCE_DIMENSION_MISMATCH,

          // This error has a bug, it doesn't properly count events
          D3D12_MESSAGE_ID_PIX_EVENT_UNDERFLOW,
      };

      D3D12_INFO_QUEUE_FILTER filter = {};
      filter.DenyList.NumIDs = ARRAY_COUNT(mute);
      filter.DenyList.pIDList = mute;

      m_pInfoQueue->AddStorageFilterEntries(&filter);
    }
  }
  else
  {
    RDCDEBUG("Couldn't get ID3D12InfoQueue.");
  }

  {
    SCOPED_LOCK(m_DeviceWrappersLock);
    m_DeviceWrappers[m_pDevice] = this;
  }
}

WrappedID3D12Device::~WrappedID3D12Device()
{
  InvalidateReplayCheckpoints();
  SAFE_DELETE(m_ReplayCheckpoints);
  if(!m_Replay->IsRemoteProxy())
  {
    Threading::JobSystem::SyncAllJobs();
    GetResourceManager()->ResolveDeferredWrappers();
  }

  {
    SCOPED_LOCK(m_DeviceWrappersLock);
    m_DeviceWrappers.erase(m_pDevice);
  }

  SAFE_DELETE(m_StoredStructuredData);

  for(auto it = m_Annotations.begin(); it != m_Annotations.end(); ++it)
    delete it->second;

  RenderDoc::Inst().RemoveDeviceFrameCapturer((ID3D12Device *)this);

  if(!m_InternalCmds.pendingcmds.empty())
    ExecuteLists(m_Queue);

  if(!m_InternalCmds.submittedcmds.empty())
    FlushLists(true);

  if(m_ReplayBufferPatchUpload)
  {
    DeviceWaitForIdle();
    SAFE_RELEASE(m_ReplayBufferPatchUpload);
  }

  for(size_t i = 0; i < m_InternalCmds.freecmds.size(); i++)
    SAFE_RELEASE(m_InternalCmds.freecmds[i]);

  DeviceWaitForIdle();
  for(size_t i = 0; i < m_QueueFences.size(); i++)
    SAFE_RELEASE(m_QueueFences[i]);

  for(auto it = m_UploadBuffers.begin(); it != m_UploadBuffers.end(); ++it)
  {
    SAFE_RELEASE(it->second);
  }

  SAFE_RELEASE(m_blasAddressBufferResource);
  SAFE_RELEASE(m_blasAddressBufferUploadResource);

  m_Replay->DestroyResources();

  DestroyInternalResources();

  SAFE_RELEASE(m_Queue);

  if(m_DeviceRecord)
  {
    RDCASSERT(m_DeviceRecord->GetRefCount() == 1);
    m_DeviceRecord->Delete(GetResourceManager());
  }

  if(m_FrameCaptureRecord)
  {
    RDCASSERT(m_FrameCaptureRecord->GetRefCount() == 1);
    m_FrameCaptureRecord->Delete(GetResourceManager());
  }

  m_ResourceManager->Shutdown();

  SAFE_DELETE(m_ResourceManager);

  SAFE_RELEASE(m_DRED.m_pReal);
  SAFE_RELEASE(m_DRED.m_pReal1);
  SAFE_RELEASE(m_DREDSettings.m_pReal);
  SAFE_RELEASE(m_DREDSettings.m_pReal1);
  SAFE_RELEASE(m_DREDSettings.m_pReal2);
  SAFE_RELEASE(m_CompatDevice.m_pReal);
  SAFE_RELEASE(m_SharingContract.m_pReal);
  SAFE_RELEASE(m_DeviceStats.m_pReal);
  SAFE_RELEASE(m_pDownlevel);
  SAFE_RELEASE(m_pDeviceTools);
  SAFE_RELEASE(m_pDeviceTools1);
  SAFE_RELEASE(m_pDevice15);
  SAFE_RELEASE(m_pDevice14);
  SAFE_RELEASE(m_pDevice13);
  SAFE_RELEASE(m_pDevice12);
  SAFE_RELEASE(m_pDevice11);
  SAFE_RELEASE(m_pDevice10);
  SAFE_RELEASE(m_pDevice9);
  SAFE_RELEASE(m_pDevice8);
  SAFE_RELEASE(m_pDevice7);
  SAFE_RELEASE(m_pDevice6);
  SAFE_RELEASE(m_pDevice5);
  SAFE_RELEASE(m_pDevice4);
  SAFE_RELEASE(m_pDevice3);
  SAFE_RELEASE(m_pDevice2);
  SAFE_RELEASE(m_pDevice1);

  SAFE_RELEASE(m_pInfoQueue);
  SAFE_RELEASE(m_WrappedDebug.m_pDebug);
  SAFE_RELEASE(m_WrappedDebug.m_pDebug1);
  SAFE_RELEASE(m_WrappedDebug.m_pDebug2);
  SAFE_RELEASE(m_pDevice);

  for(size_t i = 0; i < m_ThreadSerialisers.size(); i++)
    delete m_ThreadSerialisers[i];

  for(size_t i = 0; i < m_ThreadTempMem.size(); i++)
  {
    delete[] m_ThreadTempMem[i]->memory;
    delete m_ThreadTempMem[i];
  }

  SAFE_DELETE(m_ResourceList);
  SAFE_DELETE(m_PipelineList);

  delete m_Replay;

  SAFE_RELEASE(m_ReplayNVAPI);
  SAFE_RELEASE(m_ReplayAGS);

  RenderDoc::Inst().UnregisterMemoryRegion(this);
}

WrappedID3D12Device *WrappedID3D12Device::Create(ID3D12Device *realDevice, D3D12InitParams params,
                                                 bool enabledDebugLayer)
{
  {
    SCOPED_LOCK(m_DeviceWrappersLock);

    auto it = m_DeviceWrappers.find(realDevice);
    if(it != m_DeviceWrappers.end())
    {
      it->second->AddRef();
      return it->second;
    }
  }

  return new WrappedID3D12Device(realDevice, params, enabledDebugLayer);
}

HRESULT WrappedID3D12Device::QueryInterface(REFIID riid, void **ppvObject)
{
  // RenderDoc UUID {A7AA6116-9C8D-4BBA-9083-B4D816B71B78}
  static const GUID IRenderDoc_uuid = {
      0xa7aa6116, 0x9c8d, 0x4bba, {0x90, 0x83, 0xb4, 0xd8, 0x16, 0xb7, 0x1b, 0x78}};

  static const GUID ID3D12CompatibilityDevice_uuid = {
      0x8f1c0e3c, 0xfae3, 0x4a82, {0xb0, 0x98, 0xbf, 0xe1, 0x70, 0x82, 0x07, 0xff}};

  // unknown/undocumented internal interface
  // {de18ef3a-0x2089-0x4936-0xa3 0xf3- 0xec 0x78 0x7a 0xc6 0xa4 0x0d}
  static const GUID ID3D12DeviceDriverDetails_RS5_uuid = {
      0xde18ef3a, 0x2089, 0x4936, {0xa3, 0xf3, 0xec, 0x78, 0x7a, 0xc6, 0xa4, 0x0d}};

  HRESULT hr = S_OK;

  // capture-side device version cap: if configured, reject QueryInterface for device versions
  // above the cap to force applications to fall back to older API calls
  uint32_t maxDevVer = D3D12_Hack_MaxDeviceVersion();
  bool capDeviceCap = IsCaptureMode(m_State) && maxDevVer > 0;

  if(riid == __uuidof(IUnknown))
  {
    *ppvObject = (IUnknown *)(ID3D12Device *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(IDXGIDevice))
  {
    hr = m_pDevice->QueryInterface(riid, ppvObject);

    if(SUCCEEDED(hr))
    {
      IDXGIDevice *real = (IDXGIDevice *)(*ppvObject);
      *ppvObject = (IDXGIDevice *)(new WrappedIDXGIDevice4(real, this));
      return S_OK;
    }
    else
    {
      *ppvObject = NULL;
      return hr;
    }
  }
  else if(riid == __uuidof(IDXGIDevice1))
  {
    hr = m_pDevice->QueryInterface(riid, ppvObject);

    if(SUCCEEDED(hr))
    {
      IDXGIDevice1 *real = (IDXGIDevice1 *)(*ppvObject);
      *ppvObject = (IDXGIDevice1 *)(new WrappedIDXGIDevice4(real, this));
      return S_OK;
    }
    else
    {
      *ppvObject = NULL;
      return hr;
    }
  }
  else if(riid == __uuidof(IDXGIDevice2))
  {
    hr = m_pDevice->QueryInterface(riid, ppvObject);

    if(SUCCEEDED(hr))
    {
      IDXGIDevice2 *real = (IDXGIDevice2 *)(*ppvObject);
      *ppvObject = (IDXGIDevice2 *)(new WrappedIDXGIDevice4(real, this));
      return S_OK;
    }
    else
    {
      *ppvObject = NULL;
      return hr;
    }
  }
  else if(riid == __uuidof(IDXGIDevice3))
  {
    hr = m_pDevice->QueryInterface(riid, ppvObject);

    if(SUCCEEDED(hr))
    {
      IDXGIDevice3 *real = (IDXGIDevice3 *)(*ppvObject);
      *ppvObject = (IDXGIDevice3 *)(new WrappedIDXGIDevice4(real, this));
      return S_OK;
    }
    else
    {
      *ppvObject = NULL;
      return hr;
    }
  }
  else if(riid == __uuidof(IDXGIDevice4))
  {
    hr = m_pDevice->QueryInterface(riid, ppvObject);

    if(SUCCEEDED(hr))
    {
      IDXGIDevice4 *real = (IDXGIDevice4 *)(*ppvObject);
      *ppvObject = (IDXGIDevice4 *)(new WrappedIDXGIDevice4(real, this));
      return S_OK;
    }
    else
    {
      *ppvObject = NULL;
      return hr;
    }
  }
  else if(riid == __uuidof(ID3D12Device))
  {
    AddRef();
    *ppvObject = (ID3D12Device *)this;
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Device1))
  {
    if(m_pDevice1 && !(capDeviceCap && 1 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device1 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device2))
  {
    if(m_pDevice2 && !(capDeviceCap && 2 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device2 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device3))
  {
    if(m_pDevice3 && !(capDeviceCap && 3 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device3 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device4))
  {
    if(m_pDevice4 && !(capDeviceCap && 4 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device4 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device5))
  {
    if(m_pDevice5 && !(capDeviceCap && 5 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device5 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device6))
  {
    if(m_pDevice6 && !(capDeviceCap && 6 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device6 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device7))
  {
    if(m_pDevice7 && !(capDeviceCap && 7 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device7 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device8))
  {
    if(m_pDevice8 && !(capDeviceCap && 8 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device8 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device9))
  {
    if(m_pDevice9 && !(capDeviceCap && 9 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device9 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device10))
  {
    if(m_pDevice10 && !(capDeviceCap && 10 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device10 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device11))
  {
    if(m_pDevice11 && !(capDeviceCap && 11 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device11 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device12))
  {
    if(m_pDevice12 && !(capDeviceCap && 12 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device12 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device13))
  {
    if(m_pDevice13 && !(capDeviceCap && 13 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device13 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device14))
  {
    if(m_pDevice14 && !(capDeviceCap && 14 > maxDevVer))
    {
      AddRef();
      *ppvObject = (ID3D12Device14 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12Device15))
  {
    if(m_pDevice15)
    {
      AddRef();
      *ppvObject = (ID3D12Device15 *)this;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceConfiguration))
  {
    if(m_DevConfig.IsValid())
    {
      *ppvObject = (ID3D12DeviceConfiguration *)&m_DevConfig;
      AddRef();
      return S_OK;
    }

    return E_NOINTERFACE;
  }
  else if(riid == __uuidof(ID3D12DeviceConfiguration1))
  {
    if(m_DevConfig.IsValid1())
    {
      *ppvObject = (ID3D12DeviceConfiguration1 *)&m_DevConfig;
      AddRef();
      return S_OK;
    }

    return E_NOINTERFACE;
  }
  else if(riid == __uuidof(ID3D12DeviceDownlevel))
  {
    if(m_pDownlevel)
    {
      *ppvObject = &m_WrappedDownlevel;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceTools))
  {
    if(m_pDeviceTools)
    {
      *ppvObject = &m_WrappedDeviceTools;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceTools1))
  {
    if(m_pDeviceTools1)
    {
      *ppvObject = &m_WrappedDeviceTools;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12InfoQueue))
  {
    RDCWARN(
        "Returning a dummy ID3D12InfoQueue that does nothing. This ID3D12InfoQueue will not work!");
    *ppvObject = (ID3D12InfoQueue *)&m_DummyInfoQueue;
    m_DummyInfoQueue.AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12InfoQueue1))
  {
    RDCWARN(
        "Returning a dummy ID3D12InfoQueue1 that does nothing. This ID3D12InfoQueue1 will not "
        "work!");
    *ppvObject = (ID3D12InfoQueue1 *)&m_DummyInfoQueue;
    m_DummyInfoQueue.AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(INVAPID3DDevice))
  {
    // don't addref, this is an internal interface so we just don't addref at all
    *ppvObject = (INVAPID3DDevice *)&m_WrappedNVAPI;
    return S_OK;
  }
  else if(riid == __uuidof(IAGSD3DDevice))
  {
    // don't addref, this is an internal interface so we just don't addref at all
    *ppvObject = (IAGSD3DDevice *)&m_WrappedAGS;
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DebugDevice))
  {
    // we queryinterface for this at startup, so if it's present we can
    // return our wrapper
    if(m_WrappedDebug.m_pDebug)
    {
      AddRef();
      *ppvObject = (ID3D12DebugDevice *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      RDCWARN(
          "Returning a dummy ID3D12DebugDevice that does nothing. This ID3D12DebugDevice will not "
          "work!");
      *ppvObject = (ID3D12DebugDevice *)&m_DummyDebug;
      m_DummyDebug.AddRef();
      return S_OK;
    }
  }
  else if(riid == __uuidof(ID3D12DebugDevice1))
  {
    // we queryinterface for this at startup, so if it's present we can
    // return our wrapper
    if(m_WrappedDebug.m_pDebug1)
    {
      AddRef();
      *ppvObject = (ID3D12DebugDevice1 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      RDCWARN(
          "Returning a dummy ID3D12DebugDevice1 that does nothing. This ID3D12DebugDevice1 will "
          "not "
          "work!");
      *ppvObject = (ID3D12DebugDevice1 *)&m_DummyDebug;
      m_DummyDebug.AddRef();
      return S_OK;
    }
  }
  else if(riid == __uuidof(ID3D12DebugDevice2))
  {
    // we queryinterface for this at startup, so if it's present we can
    // return our wrapper
    if(m_WrappedDebug.m_pDebug1)
    {
      AddRef();
      *ppvObject = (ID3D12DebugDevice2 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      RDCWARN(
          "Returning a dummy ID3D12DebugDevice2 that does nothing. This ID3D12DebugDevice2 will "
          "not "
          "work!");
      *ppvObject = (ID3D12DebugDevice2 *)&m_DummyDebug;
      m_DummyDebug.AddRef();
      return S_OK;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceRemovedExtendedData))
  {
    if(m_DRED.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12DeviceRemovedExtendedData *)&m_DRED;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceRemovedExtendedData1))
  {
    if(m_DRED.m_pReal1)
    {
      AddRef();
      *ppvObject = (ID3D12DeviceRemovedExtendedData1 *)&m_DRED;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceRemovedExtendedDataSettings))
  {
    if(m_DREDSettings.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12DeviceRemovedExtendedDataSettings *)&m_DREDSettings;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceRemovedExtendedDataSettings1))
  {
    if(m_DREDSettings.m_pReal1)
    {
      AddRef();
      *ppvObject = (ID3D12DeviceRemovedExtendedDataSettings1 *)&m_DREDSettings;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceRemovedExtendedDataSettings2))
  {
    if(m_DREDSettings.m_pReal2)
    {
      AddRef();
      *ppvObject = (ID3D12DeviceRemovedExtendedDataSettings2 *)&m_DREDSettings;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == IRenderDoc_uuid)
  {
    AddRef();
    *ppvObject = (IUnknown *)this;
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12CompatibilityDevice))
  {
    if(m_CompatDevice.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12CompatibilityDevice *)&m_CompatDevice;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DeviceStatistics))
  {
    if(m_DeviceStats.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12DeviceStatistics *)&m_DeviceStats;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12SharingContract))
  {
    if(m_SharingContract.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12SharingContract *)&m_SharingContract;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == ID3D12DeviceDriverDetails_RS5_uuid)
  {
    static bool printed = false;
    if(!printed)
    {
      RDCWARN(
          "Querying ID3D12Device for unsupported/undocumented interface: "
          "ID3D12DeviceDriverDetails_RS5");
      printed = true;
    }
    // return the real thing unwrapped and hope this is OK
    return m_pDevice->QueryInterface(riid, ppvObject);
  }

  return m_RefCounter.QueryInterface("ID3D12Device", riid, ppvObject);
}

HRESULT WrappedID3D12Device::CreateInitialStateBuffer(const D3D12_RESOURCE_DESC &desc,
                                                      ID3D12Resource **buf)
{
  const UINT64 InitialStateHeapSize = 128 * 1024 * 1024;

  HRESULT ret = S_OK;

  // if desc.Width is greater than InitialStateHeapSize this will forcibly be true, regardless of
  // m_LastInitialStateHeapOffset
  // it will create a dedicated heap for that resource, and then when m_LastInitialStateHeapOffset
  // is updated the next resource (regardless of size) will go in a new heap because
  // m_LastInitialStateHeapOffset >= InitialStateHeapSize will be true
  if(m_InitialStateHeaps.empty() || m_LastInitialStateHeapOffset >= InitialStateHeapSize ||
     desc.Width > InitialStateHeapSize - m_LastInitialStateHeapOffset)
  {
    D3D12_HEAP_DESC heapDesc = {};
    heapDesc.Alignment = D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT;
    heapDesc.SizeInBytes = RDCMAX(InitialStateHeapSize, desc.Width);
    heapDesc.Flags = D3D12_HEAP_FLAG_ALLOW_ONLY_BUFFERS;

    heapDesc.Properties.Type = D3D12_HEAP_TYPE_READBACK;
    heapDesc.Properties.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    heapDesc.Properties.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    heapDesc.Properties.CreationNodeMask = 1;
    heapDesc.Properties.VisibleNodeMask = 1;

    ID3D12Heap *heap = NULL;

    ret = GetReal()->CreateHeap(&heapDesc, __uuidof(ID3D12Heap), (void **)&heap);

    if(FAILED(ret))
    {
      CHECK_HR(this, ret);
      RDCERR("Couldn't create new initial state heap #%zu: %s", m_InitialStateHeaps.size(),
             ToStr(ret).c_str());
      SAFE_RELEASE(heap);
      return ret;
    }

    m_InitialStateHeaps.push_back(heap);
    m_LastInitialStateHeapOffset = 0;
  }

  ret = GetReal()->CreatePlacedResource(m_InitialStateHeaps.back(), m_LastInitialStateHeapOffset,
                                        &desc, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                                        __uuidof(ID3D12Resource), (void **)buf);

  // all D3D12 buffers are 64kb aligned
  m_LastInitialStateHeapOffset = AlignUp(m_LastInitialStateHeapOffset + desc.Width, 64 * 1024LLU);

  return ret;
}

ID3D12Resource *WrappedID3D12Device::GetUploadBuffer(uint64_t chunkOffset, uint64_t byteSize)
{
  ID3D12Resource *buf = m_UploadBuffers[chunkOffset];

  if(buf != NULL)
    return buf;

  D3D12_RESOURCE_DESC soBufDesc;
  soBufDesc.Alignment = 0;
  soBufDesc.DepthOrArraySize = 1;
  soBufDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
  soBufDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
  soBufDesc.Format = DXGI_FORMAT_UNKNOWN;
  soBufDesc.Height = 1;
  soBufDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
  soBufDesc.MipLevels = 1;
  soBufDesc.SampleDesc.Count = 1;
  soBufDesc.SampleDesc.Quality = 0;
  soBufDesc.Width = byteSize;

  D3D12_HEAP_PROPERTIES heapProps;
  heapProps.Type = D3D12_HEAP_TYPE_UPLOAD;
  heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
  heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
  heapProps.CreationNodeMask = 1;
  heapProps.VisibleNodeMask = 1;

  HRESULT hr = CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &soBufDesc,
                                       D3D12_RESOURCE_STATE_GENERIC_READ, NULL,
                                       __uuidof(ID3D12Resource), (void **)&buf);
  RemoveReplayResource(GetResID(buf));

  m_UploadBuffers[chunkOffset] = buf;

  RDCASSERT(hr == S_OK, hr, S_OK, byteSize);
  return buf;
}

ID3D12RootSignature *WrappedID3D12Device::CreateImplicitRootSig(
    D3D12_SERIALIZED_ROOT_SIGNATURE_DESC &RootSigBlob)
{
  rdcfixedarray<uint32_t, 4> hash;
  DXBC::DXBCContainer::GetHash(hash, false, RootSigBlob.pSerializedBlob,
                               RootSigBlob.SerializedBlobSizeInBytes);

  ID3D12RootSignature *cacheEntry = m_ImplicitRootSigs[hash];

  // if we've already cached this root sig, return it! check for collisions by not trusting th
  if(cacheEntry)
    return cacheEntry;

  // otherwise create it
  HRESULT hr =
      CreateRootSignature(0, RootSigBlob.pSerializedBlob, RootSigBlob.SerializedBlobSizeInBytes,
                          __uuidof(ID3D12RootSignature), (void **)&cacheEntry);
  if(cacheEntry)
    InternalRef();
  if(FAILED(hr))
    RDCERR("Failed to create implicit root signature from blob: %s", ToStr(hr).c_str());

  m_ImplicitRootSigs[hash] = cacheEntry;

  return NULL;
}

static rdcstr ReplayInitialCopySourceCounters(const D3D12ReplayInitialCopyStats &stats)
{
  rdcstr counters = StringFormat::Fmt("cpuMemcpyBytes=%llu", (unsigned long long)stats.cpuMemcpyBytes);
  const D3D12ReplayInitialSourceCopyStats *sources[] = {&stats.upload, &stats.gpuDefault, &stats.other};
  const char *names[] = {"Upload", "Default", "Other"};
  for(size_t i = 0; i < ARRAY_COUNT(sources); i++)
    counters += StringFormat::Fmt(
        " gpu%sBufferBytes=%llu gpu%sFootprintBytes=%llu gpu%sWholeCopies=%llu", names[i],
        (unsigned long long)sources[i]->bufferBytes, names[i],
        (unsigned long long)sources[i]->footprintBytes, names[i],
        (unsigned long long)sources[i]->wholeCopies);
  return counters;
}

static rdcstr ReplayInitialCopySourceTuple(const D3D12ReplayInitialCopyStats &stats)
{
  return StringFormat::Fmt("%llu/%llu/%llu/%llu/%llu/%llu/%llu/%llu/%llu/%llu",
                          (unsigned long long)stats.cpuMemcpyBytes,
                          (unsigned long long)stats.upload.bufferBytes,
                          (unsigned long long)stats.upload.footprintBytes,
                          (unsigned long long)stats.upload.wholeCopies,
                          (unsigned long long)stats.gpuDefault.bufferBytes,
                          (unsigned long long)stats.gpuDefault.footprintBytes,
                          (unsigned long long)stats.gpuDefault.wholeCopies,
                          (unsigned long long)stats.other.bufferBytes,
                          (unsigned long long)stats.other.footprintBytes,
                          (unsigned long long)stats.other.wholeCopies);
}

void WrappedID3D12Device::ApplyInitialContents(bool useCheckpointSources)
{
  RENDERDOC_PROFILEFUNCTION();

  // A prepared retention decision is valid only for this reset. Other resident identities are
  // discarded before any initial writes, including the canonical retry after checkpoint failure.
  if(m_ReplayCheckpoints)
  {
    m_ReplayCheckpoints->residentVersions.InvalidateAll();
    m_ReplayCheckpoints->deferredInitialResources.clear();
  }

  initStateCurBatch = 0;
  initStateCurList = NULL;

  // Only the first reset of a normal root replay may use the previous submission proof. A
  // checkpoint failure's second ApplyInitialContents must always complete the canonical baseline.
  const bool firstReset = m_ReplayDirtyResetRequested;
  if(!firstReset)
    m_UseReplayDirtyInitialContents = false;
  m_ReplayDirtyResetRequested = false;
  m_ReplayDirtyCopyStats.clear();
  m_ReplayDirtyApplyingInitial = true;
  m_ReplayDirtyInitialFailed = false;

  // Decide before the canonical sparse copies mark their own physical components dirty. The
  // retained set is fixed for this one transaction; a fallback reset must not inherit it.
  PrepareReplaySparseInitialBaselines(firstReset);

  // A sparse texture's own initial copy writes physical heap pages, even when no captured action
  // wrote it. Select every affected alias before visiting any initial-content entry so the
  // canonical order cannot leave an earlier, skipped alias with the wrong contents.
  PrepareReplaySparseInitialWrites();

  m_UseCheckpointInitialSources = useCheckpointSources;
  if(!m_UseCheckpointInitialSources && m_ReplayCheckpoints)
  {
    // With initial-source substitution disabled, the baseline overwrites these same working
    // resources. A previous match with the checkpoint cannot survive that write.
    m_ReplayCheckpoints->restorePlan.retainedResources.clear();
    m_ReplayCheckpoints->restorePlan.retainedBytes = 0;
    m_ReplayCheckpoints->restorePlan.fullClearResources.clear();
    m_ReplayCheckpoints->restorePlan.fullClearBytes = 0;
    m_ReplayCheckpoints->restorePlan.descriptorExclusions.clear();
  }
  GetResourceManager()->ApplyInitialContents();
  ApplyInitialReplayBufferPatches();
  m_UseCheckpointInitialSources = false;

  // Upload all buffer addresses as all of the referenced buffer resources would have been
  // created and addresses had been tracked
  UploadBLASBufferAddresses();

  // close the final list
  if(initStateCurList)
  {
    ScopedReplayTimingSection timing(ReplayTimingSection::InitialListClose);
    D3D12MarkerRegion::End(initStateCurList);
    const HRESULT closeResult = initStateCurList->Close();
    if(FAILED(closeResult))
    {
      InvalidateReplayDirtyTracking("initialListCloseFailed");
      CHECK_HR(this, closeResult);
    }
  }

  initStateCurBatch = 0;
  initStateCurList = NULL;

  if(m_ReplayDirtyTiming && IsActiveReplaying(m_State))
  {
    uint64_t actual = 0, skipped = 0, whole = 0;
    D3D12ReplayInitialCopyStats sourceTotals;
    typedef rdcpair<ResourceId, const ReplayDirtyCopyStats *> CopyEntry;
    rdcarray<CopyEntry> details;
    details.reserve(m_ReplayDirtyCopyStats.size());
    for(const auto &entry : m_ReplayDirtyCopyStats)
    {
      actual += entry.second.actualBytes;
      skipped += entry.second.skippedBytes;
      whole += entry.second.wholeCopies;
      sourceTotals.Add(entry.second.sources);
      details.push_back({entry.first, &entry.second});
    }
    // Keep the diagnostic line bounded even for captures with tens of thousands of resources.
    // Totals cover every resource; omitted sums make truncation explicit and checkable.
    const size_t detailCount = RDCMIN(size_t(24), details.size());
    if(detailCount)
      std::partial_sort(details.begin(), details.begin() + detailCount, details.end(),
                        [](const CopyEntry &a, const CopyEntry &b) {
                          if(a.second->actualBytes != b.second->actualBytes)
                            return a.second->actualBytes > b.second->actualBytes;
                          if(a.second->wholeCopies != b.second->wholeCopies)
                            return a.second->wholeCopies > b.second->wholeCopies;
                          if(a.second->skippedBytes != b.second->skippedBytes)
                            return a.second->skippedBytes > b.second->skippedBytes;
                          return a.first < b.first;
                        });
    uint64_t omittedActual = actual, omittedSkipped = skipped, omittedWhole = whole;
    rdcstr resources, sourceResources, storageResources;
    for(size_t i = 0; i < detailCount; i++)
    {
      const ReplayDirtyCopyStats &stats = *details[i].second;
      omittedActual -= stats.actualBytes;
      omittedSkipped -= stats.skippedBytes;
      omittedWhole -= stats.wholeCopies;
      const rdcstr resource = ToStr(details[i].first);
      resources += StringFormat::Fmt(" %s:%llu/%llu/%llu", resource.c_str(),
                                     (unsigned long long)stats.actualBytes,
                                     (unsigned long long)stats.skippedBytes,
                                     (unsigned long long)stats.wholeCopies);
      sourceResources += StringFormat::Fmt("%s%s:%s", i ? "," : "", resource.c_str(),
                                           ReplayInitialCopySourceTuple(stats.sources).c_str());
      const auto live = GetResourceList().find(details[i].first);
      if(live != GetResourceList().end())
      {
        const D3D12_RESOURCE_DESC desc = live->second->GetReal()->GetDesc();
        storageResources += StringFormat::Fmt(
            "%s%s:%s:%llu:%u:%u", storageResources.empty() ? "" : ",", resource.c_str(),
            ToStr(GetResID(live->second->GetHeap())).c_str(),
            (unsigned long long)live->second->GetHeapOffset(), uint32_t(desc.Flags),
            IsReadOnlyResource(details[i].first) ? 1U : 0U);
      }
    }
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12DirtyRestore enabled=%u shadow=%u actualCopyBytes=%llu skippedStaticBytes=%llu "
        "wholeResourceCopies=%llu dirtyResources=%zu dirtyHeaps=%zu reason=%s "
        "restoreTimingVersion=2 %s resourceDetailCount=%zu omittedResourceCount=%zu "
        "omittedActualCopyBytes=%llu omittedSkippedStaticBytes=%llu omittedWholeResourceCopies=%llu "
        "resourceSourceCopies=%s resourceHeapOffsetFlagsReadOnly=%s "
        "resourceActualSkippedWhole=%s", m_UseReplayDirtyInitialContents ? 1U : 0U,
        m_ReplayDirtyShadow ? 1U : 0U, (unsigned long long)actual, (unsigned long long)skipped,
        (unsigned long long)whole, m_ReplayDirtyResources.size(), m_ReplayDirtyHeapRanges.size(),
        m_ReplayDirtyReason.c_str(), ReplayInitialCopySourceCounters(sourceTotals).c_str(), detailCount,
        details.size() - detailCount, (unsigned long long)omittedActual,
        (unsigned long long)omittedSkipped, (unsigned long long)omittedWhole,
        sourceResources.empty() ? "none" : sourceResources.c_str(),
        storageResources.empty() ? "none" : storageResources.c_str(), resources.c_str());
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
  m_UseReplayDirtyInitialContents = false;
  m_ReplayDirtyResources.clear();
  m_ReplayDirtyBufferRanges.clear();
  if(m_ReplayCheckpoints)
    m_ReplayDirtyResources.insert(m_ReplayCheckpoints->deferredInitialResources.begin(),
                                 m_ReplayCheckpoints->deferredInitialResources.end());
  m_ReplayDirtyHeapRanges.clear();
  m_ReplaySparseDirtyComponents.clear();
  m_ReplayDirtyApplyingInitial = false;
  m_ReplayDirtyValid = m_ReplayDirtyRecordingAllowed && !m_ReplayDirtyInitialFailed && !HasFatalError();
  if(m_ReplayDirtyValid)
  {
    m_ReplayDirtyReason = "submittedWrites";
    // Initial patch application changes the immutable capture baseline too. Keep those bytes in
    // the new union even when this replay never encounters a captured CPU write to the buffer.
    for(const ReplayBufferPatch &patch : m_ReplayBufferPatches)
      MarkReplayBufferRangeDirty(patch.buffer, patch.byteOffset, patch.data.size());
  }
}

static void MergeReplayDirtyRange(rdcarray<rdcpair<uint64_t, uint64_t>> &ranges,
                                 uint64_t first, uint64_t end)
{
  if(first >= end)
    return;
  size_t begin = 0;
  while(begin < ranges.size() && ranges[begin].second < first)
    begin++;
  size_t finish = begin;
  while(finish < ranges.size() && ranges[finish].first <= end)
  {
    first = RDCMIN(first, ranges[finish].first);
    end = RDCMAX(end, ranges[finish].second);
    finish++;
  }
  if(finish > begin)
    ranges.erase(begin, finish - begin);
  ranges.insert(begin, {first, end});
}

static rdcarray<rdcpair<uint64_t, uint64_t>> IntersectReplayDirtyRanges(
    const rdcarray<rdcpair<uint64_t, uint64_t>> &source,
    const rdcarray<rdcpair<uint64_t, uint64_t>> &dirty)
{
  rdcarray<rdcpair<uint64_t, uint64_t>> result;
  size_t a = 0, b = 0;
  while(a < source.size() && b < dirty.size())
  {
    const uint64_t first = RDCMAX(source[a].first, dirty[b].first);
    const uint64_t end = RDCMIN(source[a].second, dirty[b].second);
    if(first < end)
      result.push_back({first, end});
    if(source[a].second < dirty[b].second)
      a++;
    else
      b++;
  }
  return result;
}

static bool MergeReplayBufferDirtyWrite(bool previouslyDirty, bool eligible, uint64_t width,
                                        uint64_t offset, uint64_t length,
                                        rdcarray<rdcpair<uint64_t, uint64_t>> &ranges,
                                        size_t maxRanges = 256)
{
  // Absence of ranges on an already dirty resource represents an unknown/whole-buffer write.
  // Check subtraction before addition so malformed native ranges can only widen restoration.
  if(!eligible || (previouslyDirty && ranges.empty()) || !width || !length || offset > width ||
     length > width - offset)
  {
    ranges.clear();
    return false;
  }
  MergeReplayDirtyRange(ranges, offset, offset + length);
  if(ranges.size() > maxRanges)
  {
    ranges.clear();
    return false;
  }
  return true;
}

static rdcarray<rdcpair<uint64_t, uint64_t>> SelectReplayBufferDirtyCopies(
    bool valid, uint64_t width, const rdcarray<rdcpair<uint64_t, uint64_t>> &dirty,
    const rdcarray<rdcpair<uint64_t, uint64_t>> &requested)
{
  // The original initial-content/alias planner remains authoritative when proof is missing.
  if(!valid || dirty.empty())
    return requested;
  uint64_t previousEnd = 0;
  for(const auto &range : dirty)
  {
    if(range.first < previousEnd || range.first >= range.second || range.second > width)
      return requested;
    previousEnd = range.second;
  }
  return IntersectReplayDirtyRanges(requested, dirty);
}

static bool ReplayCPUWriteBaselineDifferences(
    const byte *baseline, const byte *written, uint64_t offset, uint64_t length,
    rdcarray<rdcpair<uint64_t, uint64_t>> &differences, size_t maxRanges = 256)
{
  differences.clear();
  if(!baseline || !written || !length || length > SIZE_MAX || offset > ~0ULL - length)
    return false;
  size_t cursor = 0;
  while(cursor < size_t(length))
  {
    if(baseline[cursor] == written[cursor])
    {
      cursor++;
      continue;
    }
    const size_t first = cursor++;
    while(cursor < size_t(length) && baseline[cursor] != written[cursor])
      cursor++;
    if(differences.size() >= maxRanges)
    {
      differences.clear();
      return false;
    }
    differences.push_back({offset + first, offset + cursor});
  }
  return true;
}

template <typename WholeWrite, typename RangeWrite>
static void ApplyReplayCPUWriteBaselineDebt(
    ResourceId id, const rdcarray<rdcpair<uint64_t, uint64_t>> &differences,
    std::set<ResourceId> &dirtyResources,
    std::map<ResourceId, rdcarray<rdcpair<uint64_t, uint64_t>>> &dirtyRanges, const bool &valid,
    WholeWrite wholeWrite, RangeWrite rangeWrite)
{
  const bool previouslyDirty = dirtyResources.count(id) != 0;
  rdcarray<rdcpair<uint64_t, uint64_t>> previousRanges;
  auto previous = dirtyRanges.find(id);
  if(previous != dirtyRanges.end())
    previousRanges.swap(previous->second);
  // Even a blob equal to initial bytes overwrites the current checkpoint/live resource version.
  // Preserve every real-write side effect, narrowing only the debt against immutable initial data.
  wholeWrite();
  if(!valid)
    return;
  if(!previouslyDirty)
    dirtyResources.erase(id);
  else if(!previousRanges.empty())
    dirtyRanges[id] = std::move(previousRanges);
  for(const auto &range : differences)
    rangeWrite(range.first, range.second - range.first);
}

static bool ReplayBufferDirtyEligible(const D3D12_RESOURCE_DESC &desc, bool placed, bool sparse,
                                      bool replaced, bool heapKnown, D3D12_HEAP_FLAGS heapFlags)
{
  return desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER && desc.Width && !placed && !sparse &&
         !replaced && heapKnown &&
         !(heapFlags & (D3D12_HEAP_FLAG_SHARED | D3D12_HEAP_FLAG_SHARED_CROSS_ADAPTER)) &&
         !(desc.Flags & (D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
                         D3D12_RESOURCE_FLAG_ALLOW_CROSS_ADAPTER |
                         D3D12_RESOURCE_FLAG_RAYTRACING_ACCELERATION_STRUCTURE));
}

static bool D3D12ReplayPreservesDirtyProof(ReplayLogType type, bool allowCheckpoints,
                                          bool knownShaderFeedback, bool knownOrdinaryTarget,
                                          bool callback, bool editedDependencies,
                                          bool naturalReference = false)
{
  const bool target = type == eReplay_OnlyDraw;
  return (allowCheckpoints || naturalReference ||
          (target && (knownShaderFeedback || knownOrdinaryTarget))) &&
         (!callback || (target && knownShaderFeedback)) && !editedDependencies;
}

static void AddReplaySparseLink(std::map<ResourceId, std::set<ResourceId>> &links,
                               ResourceId a, ResourceId b)
{
  if(a == ResourceId())
    return;
  links[a];
  if(b != ResourceId())
  {
    links[a].insert(b);
    links[b].insert(a);
  }
}

static bool ReplaySparseInputsComplete(const std::set<ResourceId> &sparse,
                                      const std::set<ResourceId> &initial,
                                      const std::set<ResourceId> &mappingResources)
{
  for(ResourceId id : sparse)
    if(initial.count(id) == 0)
      return false;
  for(ResourceId id : mappingResources)
    if(sparse.count(id) == 0)
      return false;
  return true;
}

static bool ReplaySparseMappingKnown(const std::map<ResourceId, std::set<ResourceId>> &links,
                                    ResourceId destination, ResourceId sourceOrHeap)
{
  auto found = links.find(destination);
  return destination != ResourceId() && found != links.end() &&
         (sourceOrHeap == ResourceId() || found->second.count(sourceOrHeap) != 0);
}

static bool ReplaySparseInitialWritesMemory(bool readOnly, D3D12InitialContents::Tag tag)
{
  return !readOnly && (tag == D3D12InitialContents::Copy || tag == D3D12InitialContents::ForceCopy);
}

static rdcarray<std::set<ResourceId>> ReplaySparseConnectedComponents(
    const std::map<ResourceId, std::set<ResourceId>> &links)
{
  rdcarray<std::set<ResourceId>> result;
  std::set<ResourceId> visited;
  for(const auto &entry : links)
  {
    if(visited.count(entry.first))
      continue;
    std::set<ResourceId> component;
    rdcarray<ResourceId> pending = {entry.first};
    while(!pending.empty())
    {
      ResourceId id = pending.back();
      pending.pop_back();
      if(!visited.insert(id).second)
        continue;
      component.insert(id);
      auto next = links.find(id);
      if(next != links.end())
        for(ResourceId neighbour : next->second)
          if(!visited.count(neighbour))
            pending.push_back(neighbour);
    }
    result.push_back(component);
  }
  return result;
}

void WrappedID3D12Device::BuildReplaySparseDirtyGraph()
{
  if(m_ReplaySparseGraphBuilt)
    return;
  m_ReplaySparseGraphBuilt = true;
  m_ReplaySparseGraphComplete = false;
  if(m_SparseResources.empty() && m_ReplaySparseMappingResources.empty())
  {
    m_ReplaySparseGraphComplete = true;
    return;
  }
  std::set<ResourceId> sparse(m_SparseResources.begin(), m_SparseResources.end());
  std::set<ResourceId> initialKnown;
  for(ResourceId id : sparse)
  {
    AddReplaySparseLink(m_ReplaySparseLinks, id, ResourceId());
    const D3D12InitialContents initial = GetResourceManager()->GetInitialContents(id);
    if(initial.sparseBinds)
    {
      // An explicitly all-NULL initial table is known and empty, unlike a missing table.
      initialKnown.insert(id);
      std::set<ResourceId> heaps;
      initial.sparseBinds->GetReferencedHeaps(heaps);
      for(ResourceId heap : heaps)
        AddReplaySparseLink(m_ReplaySparseLinks, id, heap);
    }
  }

  if(!ReplaySparseInputsComplete(sparse, initialKnown, m_ReplaySparseMappingResources))
    m_ReplaySparseGraphReason = "missingInitialMappingOrUnknownSparseResource";

  // Whole heaps deliberately include every placed allocation, irrespective of its size, format,
  // offset or reported usage. CopyTileMappings edges also merge all old and future source pools.
  for(const auto &resource : GetResourceList())
    if(!ResourceIDGen::IsReplayOnlyID(resource.first) && resource.second->GetHeap())
      AddReplaySparseLink(m_ReplaySparseLinks, resource.first,
                          GetResID(resource.second->GetHeap()));

  const auto components = ReplaySparseConnectedComponents(m_ReplaySparseLinks);
  for(const auto &members : components)
  {
    bool hasSparse = false;
    for(ResourceId id : members)
      hasSparse |= sparse.count(id) != 0;
    if(!hasSparse)
      continue;

    ReplaySparseDirtyComponent component;
    for(ResourceId id : members)
    {
      if(!GetResourceManager()->HasResource(id) || GetResourceManager()->HasReplacement(id))
      {
        m_ReplaySparseGraphReason = "missingOrReplacedSparseAlias";
        continue;
      }
      auto resource = GetResourceList().find(id);
      if(resource != GetResourceList().end())
      {
        // Every resource vertex must be a reserved resource or a placed allocation. A malformed
        // mapping cannot introduce a committed resource as a hidden physical heap.
        if(!sparse.count(id) && !resource->second->GetHeap())
          m_ReplaySparseGraphReason = "unknownSparseAliasResource";
        component.resources.insert(id);
      }
      else
      {
        ID3D12DeviceChild *object = GetResourceManager()->GetResource(id);
        if(TryIdentifyTypeByPtr(object) != Resource_Heap)
        {
          m_ReplaySparseGraphReason = "unknownSparseAliasHeap";
          continue;
        }
        const uint64_t size = ((WrappedID3D12Heap *)object)->GetDesc().SizeInBytes;
        if(size == 0 || size == ~0ULL)
          m_ReplaySparseGraphReason = "unknownSparseAliasHeapSize";
        component.heaps[id] = size;
      }
      m_ReplaySparseComponentIndex[id] = m_ReplaySparseComponents.size();
    }
    m_ReplaySparseComponents.push_back(component);
  }
  m_ReplaySparseGraphComplete = m_ReplaySparseGraphReason.empty();

  // One compact line per reserved resource exposes the actual backing pools (GetHeap() is NULL
  // for reserved resources) and bounds the cost before any local-dirty optimisation is used.
  if(m_ReplayDirtyTiming)
  {
    for(ResourceId id : sparse)
    {
      rdcstr heaps;
      uint64_t bytes = 0;
      size_t aliases = 0, reserved = 0;
      auto found = m_ReplaySparseComponentIndex.find(id);
      if(found != m_ReplaySparseComponentIndex.end())
      {
        const auto &component = m_ReplaySparseComponents[found->second];
        aliases = component.resources.size();
        for(ResourceId member : component.resources)
          reserved += sparse.count(member);
        for(const auto &heap : component.heaps)
        {
          heaps += StringFormat::Fmt("%s%s:%llu", heaps.empty() ? "" : ",",
                                      ToStr(heap.first).c_str(), (unsigned long long)heap.second);
          bytes = heap.second > ~0ULL - bytes ? ~0ULL : bytes + heap.second;
        }
      }
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12SparseDirty op=plan enabled=%u complete=%u resource=%s "
          "closureHeapBytes=%llu reservedAliases=%zu placedAliases=%zu heaps=%s reason=%s",
          m_ReplaySparseDirtyEnabled ? 1U : 0U, m_ReplaySparseGraphComplete ? 1U : 0U,
          ToStr(id).c_str(), (unsigned long long)bytes, reserved, aliases - reserved,
          heaps.c_str(), m_ReplaySparseGraphReason.empty() ? "completeWholeFrameHeapClosure"
                                                         : m_ReplaySparseGraphReason.c_str());
      RDCLOG("%s", line.c_str());
      AppendReplayTimingLog(line);
    }
  }
}

void WrappedID3D12Device::InvalidateReplaySparseInitialBaselines(const char *reason)
{
  for(auto &component : m_ReplaySparseComponents)
    component.baseline.Invalidate(reason);
}

static bool ReplaySparseInitialSubresourcesComplete(const rdcarray<uint32_t> &included,
                                                   uint32_t expected)
{
  if(expected == 0 || included.empty())
    return false;
  if(included.size() == 1 && included[0] == ~0U)
    return true;
  // Non-MSAA sparse captures deliberately retain an explicit list, even when every subresource
  // was mapped and copied. Require exact complete coverage, not merely the right list length.
  if(included.size() != expected)
    return false;
  for(uint32_t sub = 0; sub < expected; sub++)
    if(included[sub] != sub)
      return false;
  return true;
}

void WrappedID3D12Device::PrepareReplaySparseInitialBaselines(bool firstReset)
{
  m_ReplaySparseInitialResidencyEnabled =
      Process::GetEnvVariable("RENDERDOC_D3D12_ENABLE_SPARSE_INITIAL_RESIDENCY") == "1";
  const bool recordingAllowed = m_ReplaySparseInitialResidencyEnabled &&
      IsActiveReplaying(m_State) && m_ReplayDirtyRecordingAllowed &&
      m_ReplaySparseDirtyEnabled && m_ReplaySparseGraphComplete && !m_ReplayDirtyShadow &&
      !HasFatalError() && (!m_ReplayCheckpoints || !m_ReplayCheckpoints->restorePlan.eventId);

  for(size_t index = 0; index < m_ReplaySparseComponents.size(); index++)
  {
    auto &component = m_ReplaySparseComponents[index];
    const ResourceId resource = component.resources.size() == 1
                                    ? *component.resources.begin() : ResourceId();
    const char *eligibility = "eligible";
    uint32_t expectedSubresources = 0, includedSubresources = 0, firstSubresource = ~0U;
    // Limit this first opt-in path to one reserved texture with no placed aliases. A known full
    // serialized Copy source lets the real recorded footprint byte count certify the seed.
    if(!m_ReplaySparseGraphComplete)
      eligibility = "incompleteSparseGraph";
    else if(resource == ResourceId() || !m_SparseResources.count(resource))
      eligibility = "multipleReservedOrPlacedAliases";
    else if(component.heaps.empty())
      eligibility = "noPhysicalHeaps";
    else
    {
      auto found = GetResourceList().find(resource);
      const D3D12InitialContents initial = GetResourceManager()->GetInitialContents(resource);
      if(found == GetResourceList().end() || GetResourceManager()->HasReplacement(resource) ||
         found->second->GetHeap() || IsReadOnlyResource(resource))
        eligibility = "missingReplacedOrReadOnlyResource";
      else
      {
        const D3D12_RESOURCE_DESC desc = found->second->GetReal()->GetDesc();
        D3D12_FEATURE_DATA_FORMAT_INFO format = {};
        format.Format = desc.Format;
        const HRESULT formatResult =
            CheckFeatureSupport(D3D12_FEATURE_FORMAT_INFO, &format, sizeof(format));
        const uint64_t subresources = uint64_t(desc.MipLevels) * format.PlaneCount *
            (desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D ? 1 : desc.DepthOrArraySize);
        if(SUCCEEDED(formatResult) && format.PlaneCount > 0 && subresources <= ~0U &&
           (GetCaptureVersion() >= 0x6 || format.PlaneCount == 1))
          expectedSubresources = uint32_t(subresources);
        includedSubresources = uint32_t(initial.subresources.size());
        if(!initial.subresources.empty())
          firstSubresource = initial.subresources[0];
        if(desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER || desc.SampleDesc.Count != 1 ||
           initial.resourceType != Resource_Resource || initial.tag != D3D12InitialContents::Copy ||
           !initial.sparseBinds || !initial.resource || initial.srcData ||
           ((ID3D12Resource *)initial.resource)->GetDesc().Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
           !ReplaySparseInitialSubresourcesComplete(initial.subresources, expectedSubresources))
          eligibility = "initialTextureCopyNotComplete";
      }
      for(const auto &heap : component.heaps)
      {
        const D3D12InitialContents initialHeap = GetResourceManager()->GetInitialContents(heap.first);
        if(!GetResourceManager()->HasResource(heap.first) ||
           GetResourceManager()->HasReplacement(heap.first) ||
           (initialHeap.resourceType != Resource_Unknown &&
            (initialHeap.resourceType != Resource_Heap ||
             initialHeap.tag != D3D12InitialContents::Copy || initialHeap.sparseBinds ||
             (!initialHeap.resource && !initialHeap.srcData))))
          eligibility = "unknownPhysicalHeapInitialContents";
      }
    }
    bool dirty = m_ReplaySparseDirtyComponents.count(index) != 0;
    for(ResourceId member : component.resources)
      dirty |= m_ReplayDirtyResources.count(member) != 0 ||
               m_ReplayDirtyHeapRanges.count(member) != 0;
    for(const auto &heap : component.heaps)
      dirty |= m_ReplayDirtyResources.count(heap.first) != 0 ||
               m_ReplayDirtyHeapRanges.count(heap.first) != 0;
    const bool eligible = strcmp(eligibility, "eligible") == 0;
    const rdcstr previousReason = component.baseline.reason;
    const bool retained = component.baseline.Begin(eligible, recordingAllowed, firstReset,
                                                   m_UseReplayDirtyInitialContents, dirty);
    if(m_ReplayDirtyTiming && IsActiveReplaying(m_State))
    {
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12SparseInitial op=prepare enabled=%u component=%zu resource=%s "
          "eligible=%u firstReset=%u recordingAllowed=%u preInitialDirty=%u retained=%u "
          "residentCopyBytes=%llu generation=%llu expectedSubresources=%u "
          "includedSubresources=%u firstSubresource=%u reason=%s previousReason=%s",
          m_ReplaySparseInitialResidencyEnabled ? 1U : 0U, index, ToStr(resource).c_str(),
          eligible ? 1U : 0U, firstReset ? 1U : 0U, recordingAllowed ? 1U : 0U,
          dirty ? 1U : 0U, retained ? 1U : 0U,
          (unsigned long long)component.baseline.residentBytes,
          (unsigned long long)component.baseline.generation,
          expectedSubresources, includedSubresources, firstSubresource,
          !eligible ? eligibility : !m_ReplaySparseInitialResidencyEnabled ? "disabled" :
              !firstReset ? "secondCanonicalReset" : !recordingAllowed ? "untrackedOrCheckpointReset" :
              dirty ? "componentDirty" : retained ? "residentInitialContents" : "needsSubmittedBaseline",
          previousReason.c_str());
      RDCLOG("%s", line.c_str());
      AppendReplayTimingLog(line);
    }
  }
}

void WrappedID3D12Device::CompleteReplaySparseInitialBaselines()
{
  const bool succeeded = m_ReplayDirtyValid && !m_ReplayDirtyInitialFailed && !HasFatalError();
  for(size_t index = 0; index < m_ReplaySparseComponents.size(); index++)
  {
    auto &baseline = m_ReplaySparseComponents[index].baseline;
    const bool pending = baseline.pending;
    const uint64_t bytes = baseline.recordedBytes;
    const bool published = baseline.Submit(succeeded);
    if(pending && m_ReplayDirtyTiming)
    {
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12SparseInitial op=submit component=%zu published=%u recordedCopyBytes=%llu "
          "generation=%llu reason=%s", index, published ? 1U : 0U,
          (unsigned long long)bytes, (unsigned long long)baseline.generation, baseline.reason.c_str());
      RDCLOG("%s", line.c_str());
      AppendReplayTimingLog(line);
    }
  }
}

bool WrappedID3D12Device::UseReplaySparseInitialBaseline(ResourceId resource)
{
  const auto found = m_ReplaySparseComponentIndex.find(resource);
  if(!m_ReplayDirtyApplyingInitial || !m_UseReplayDirtyInitialContents ||
     !m_ReplaySparseInitialResidencyEnabled || found == m_ReplaySparseComponentIndex.end())
    return false;
  const auto &component = m_ReplaySparseComponents[found->second];
  if(!component.baseline.retained || !component.baseline.resident || HasFatalError())
    return false;
  // Includes any backing-heap initial content entry. Skipping only the sparse texture while
  // subsequently copying its physical heap would destroy the very baseline being retained.
  if(m_ReplayDirtyTiming)
  {
    const auto copy = component.baseline.residentResourceBytes.find(resource);
    const uint64_t bytes = copy == component.baseline.residentResourceBytes.end() ? 0 : copy->second;
    // Count each initial entry's actual previously recorded copies, rather than assigning the
    // component heap allocation size to every alias or estimating physical GPU bus traffic.
    m_ReplayDirtyCopyStats[resource].skippedBytes += bytes;
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12SparseInitial op=retain component=%zu resource=%s avoidedRecordedCopyBytes=%llu "
        "generation=%llu reason=unchangedSubmittedComponent", found->second, ToStr(resource).c_str(),
        (unsigned long long)bytes, (unsigned long long)component.baseline.generation);
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return true;
}

bool WrappedID3D12Device::MarkReplaySparseComponentDirty(ResourceId resource, const char *reason)
{
  if(!m_ReplaySparseDirtyEnabled || !m_ReplaySparseGraphComplete)
    return false;
  auto found = m_ReplaySparseComponentIndex.find(resource);
  if(found == m_ReplaySparseComponentIndex.end())
    return false;
  // PrepareReplaySparseInitialWrites deliberately widens the restoration selection itself. It
  // must not masquerade as a submitted captured write and cancel the seed being recorded.
  if(!m_ReplayDirtyApplyingInitial)
    m_ReplaySparseComponents[found->second].baseline.Invalidate(reason);
  if(!m_ReplaySparseDirtyComponents.insert(found->second).second)
    return true;
  const auto &component = m_ReplaySparseComponents[found->second];
  // Only widen the per-replay selection; retain the existing canonical m_ModResources upper
  // bound and initial-content order. Do not manufacture initial contents for undefined memory.
  m_ReplayDirtyResources.insert(component.resources.begin(), component.resources.end());
  for(const auto &heap : component.heaps)
    MergeReplayDirtyRange(m_ReplayDirtyHeapRanges[heap.first], 0, heap.second);
  return true;
}

void WrappedID3D12Device::PrepareReplaySparseInitialWrites()
{
  if(!m_UseReplayDirtyInitialContents || !m_ReplaySparseDirtyEnabled || m_SparseResources.empty())
    return;
  if(!m_ReplaySparseGraphComplete)
  {
    InvalidateReplayDirtyTracking("incompleteSparseInitialAliasClosure");
    return;
  }
  for(ResourceId id : m_SparseResources)
  {
    const auto component = m_ReplaySparseComponentIndex.find(id);
    if(component != m_ReplaySparseComponentIndex.end() &&
       m_ReplaySparseComponents[component->second].baseline.retained)
      continue;
    const D3D12InitialContents initial = GetResourceManager()->GetInitialContents(id);
    if(ReplaySparseInitialWritesMemory(IsReadOnlyResource(id), initial.tag))
      if(!MarkReplaySparseComponentDirty(id))
      {
        InvalidateReplayDirtyTracking("unknownSparseInitialWrite");
        return;
      }
  }
}

void WrappedID3D12Device::ObserveReplaySparseBind(ResourceId resource, ResourceId heap)
{
  if(IsLoading(m_State) && !m_ReplaySparseGraphBuilt)
  {
    m_ReplaySparseMappingResources.insert(resource);
    AddReplaySparseLink(m_ReplaySparseLinks, resource, heap);
  }
  else if(IsActiveReplaying(m_State) && !m_ReplayDirtyApplyingInitial)
  {
    if(!m_SparseResources.count(resource) ||
       !ReplaySparseMappingKnown(m_ReplaySparseLinks, resource, heap))
    {
      // A new, uncaptured relation invalidates the graph permanently. A later canonical reset
      // cannot turn an incomplete all-frame dependency graph into a complete one.
      m_ReplaySparseGraphComplete = false;
      m_ReplaySparseGraphReason = "uncapturedSparseMapping";
      InvalidateReplayDirtyTracking("sparseQueueMapping");
    }
    else if(!MarkReplaySparseComponentDirty(resource, "tileMapping"))
      InvalidateReplayDirtyTracking("sparseQueueMapping");
  }
}

void WrappedID3D12Device::ObserveReplaySparseCopy(ResourceId destination, ResourceId source)
{
  if(IsLoading(m_State) && !m_ReplaySparseGraphBuilt)
  {
    m_ReplaySparseMappingResources.insert(destination);
    m_ReplaySparseMappingResources.insert(source);
    AddReplaySparseLink(m_ReplaySparseLinks, destination, source);
  }
  else if(IsActiveReplaying(m_State) && !m_ReplayDirtyApplyingInitial)
  {
    if(!m_SparseResources.count(destination) || !m_SparseResources.count(source) ||
       !ReplaySparseMappingKnown(m_ReplaySparseLinks, destination, source))
    {
      m_ReplaySparseGraphComplete = false;
      m_ReplaySparseGraphReason = "uncapturedSparseMappingCopy";
      InvalidateReplayDirtyTracking("sparseQueueMapping");
    }
    else if(!MarkReplaySparseComponentDirty(destination, "tileMappingCopy"))
      InvalidateReplayDirtyTracking("sparseQueueMapping");
  }
}

void WrappedID3D12Device::BeginReplayDirtyRestore(bool allowed)
{
  m_ReplayDirtyRecordingAllowed =
      allowed && !m_ReplayDirtyDependenciesEdited && !m_ReplayOptions.apiValidation &&
      !D3D12_Debug_SingleSubmitFlushing() && !D3D12_Debug_RT_Auditing() &&
      m_InitParams.VendorExtensions == GPUVendor::Unknown;
  m_ReplayDirtyTiming = Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1";
  m_ReplayDirtyShadow = Process::GetEnvVariable("RENDERDOC_D3D12_DIRTY_RESTORE_SHADOW") == "1";
  m_ReplayDirtyBufferRangesEnabled =
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_CPU_WRITE_DIRTY_RANGES") != "1";
  m_ReplaySparseDirtyEnabled =
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_SPARSE_DIRTY_CLOSURE") != "1";
  BuildReplaySparseDirtyGraph();
  m_UseReplayDirtyInitialContents = m_ReplayDirtyValid && m_ReplayDirtyRecordingAllowed &&
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_DIRTY_RESTORE") != "1" &&
      Process::GetEnvVariable("RENDERDOC_D3D12_CONSERVATIVE_REPLAY_SYNC") != "1";
  m_ReplayDirtyResetRequested = true;
}

void WrappedID3D12Device::InvalidateReplayDirtyTracking(const char *reason)
{
  InvalidateReplaySparseInitialBaselines(reason);
  if(m_ReplayCheckpoints)
  {
    m_ReplayCheckpoints->residentVersions.InvalidateAll();
    m_ReplayCheckpoints->restorePlan.retainedResources.clear();
    m_ReplayCheckpoints->restorePlan.retainedBytes = 0;
  }
  m_ReplayDirtyValid = false;
  m_UseReplayDirtyInitialContents = false;
  m_ReplayDirtyBufferRanges.clear();
  if(m_ReplayDirtyApplyingInitial)
    m_ReplayDirtyInitialFailed = true;
  m_ReplayDirtyReason = reason;
}

void WrappedID3D12Device::InvalidateLiveForward(const char *reason)
{
  m_LiveForward.valid = false;
  m_LiveForward.pendingTarget = 0;
  m_LiveForward.generation++;
  m_LiveForward.reason = reason;
}

static bool IsOrdinaryLiveForwardRoot(uint32_t start, ReplayLogType type, bool ordinaryRoot,
                                      bool allowCheckpoints, bool naturalReference, bool callback)
{
  // allowCheckpoints is the request's bypass permission, not the cache configuration. A natural
  // cost reference must measure its initial replay even though it may retain dirty-resource proof.
  return start == 0 && type == eReplay_WithoutDraw && ordinaryRoot && allowCheckpoints &&
         !naturalReference && !callback;
}

static bool CanPublishNaturalReferenceRoot(uint32_t start, ReplayLogType type,
                                           bool naturalReference, bool callback)
{
  // The cost reference still executes its entire canonical prefix. It may publish that result
  // for the next forward visit, without reusing any checkpoint or old live state itself.
  return start == 0 && type == eReplay_WithoutDraw && naturalReference && !callback;
}

static bool CanPublishLiveForwardTarget(uint32_t event, uint32_t pending, uint64_t generation,
                                        uint64_t pendingGeneration, bool failed)
{
  return !failed && event != 0 && event == pending && generation == pendingGeneration;
}

void WrappedID3D12Device::CompleteLiveForwardTarget(uint32_t eventId)
{
  if(!CanPublishLiveForwardTarget(eventId, m_LiveForward.pendingTarget, m_LiveForward.generation,
                                   m_LiveForward.pendingGeneration, HasFatalError()))
  {
    InvalidateLiveForward("targetWithoutOrdinaryRoot");
    return;
  }
  const D3D12CommandData &cmd = *m_Queue->GetCommandData();
  if(!cmd.HasResolvedLiveForwardState())
  {
    InvalidateLiveForward("unresolvedIndirectState");
    return;
  }
  const auto &partial = cmd.m_Partial[D3D12CommandData::Primary];
  const auto info = cmd.m_BakedCmdListInfo.find(partial.partialParent);
  const auto submissions = partial.cmdListExecs.find(partial.partialParent);
  if(partial.partialParent == ResourceId() || info == cmd.m_BakedCmdListInfo.end() ||
     submissions == partial.cmdListExecs.end() || !submissions->second.contains(partial.baseEvent) ||
     eventId < partial.baseEvent ||
     uint64_t(eventId) >= uint64_t(partial.baseEvent) + info->second.eventCount)
  {
    InvalidateLiveForward("targetOutsidePrimarySubmission");
    return;
  }
  // Called by the driver only after ordinary OnlyDraw (including feedback's temporary state
  // restoration) succeeds. No resource bytes or old per-event images are retained here.
  m_LiveForward.eventId = eventId;
  m_LiveForward.baked = partial.partialParent;
  m_LiveForward.baseEvent = partial.baseEvent;
  m_LiveForward.publishedGeneration = m_LiveForward.generation;
  m_LiveForward.pendingTarget = 0;
  m_LiveForward.valid = true;
  m_LiveForward.reason = "ordinaryTarget";
}

void WrappedID3D12Device::MarkReplayResourceDirty(ResourceId id, bool unboundedRootUAV)
{
  if(!IsActiveReplaying(m_State) || id == ResourceId() || ResourceIDGen::IsReplayOnlyID(id))
    return;
  if(unboundedRootUAV)
    InvalidateReplaySparseInitialBaselines("unboundedRootUAV");
  if(m_LiveForward.valid)
    InvalidateLiveForward("resourceWriteOutsideReplay");
  m_ModResources.insert(id);
  m_ReplayDirtyResources.insert(id);
  m_ReplayDirtyBufferRanges.erase(id);
  if(m_ReplayCheckpoints && !m_ReplayCheckpoints->markingRestoredContents)
  {
    if(unboundedRootUAV || GetResourceList().find(id) == GetResourceList().end())
      m_ReplayCheckpoints->residentVersions.InvalidateAll();
    else
    {
      m_ReplayCheckpoints->residentVersions.Invalidate(m_ReplayCheckpoints->ResidentRange(id));
      auto component = m_ReplayCheckpoints->bufferAliasMembers.find(id);
      if(component != m_ReplayCheckpoints->bufferAliasMembers.end())
        m_ReplayCheckpoints->residentVersions.Invalidate(
            m_ReplayCheckpoints->bufferAliasComponents[component->second].Range());
    }
  }
  const auto resource = GetResourceList().find(id);
  if(resource == GetResourceList().end())
  {
    // MapDataWrite may target a whole placed heap rather than an individual resource.
    if(GetResourceManager()->HasResource(id) &&
       TryIdentifyTypeByPtr(GetResourceManager()->GetResAs<ID3D12DeviceChild>(id)) == Resource_Heap)
    {
      MarkReplaySparseComponentDirty(id);
      MergeReplayDirtyRange(m_ReplayDirtyHeapRanges[id], 0, ~0ULL);
    }
    else
      InvalidateReplayDirtyTracking("unknownWrittenResource");
    return;
  }
  const bool replaced = GetResourceManager()->HasReplacement(id);
  const bool sparse = m_SparseResources.count(id) != 0;
  // Descriptor-bounded sparse writes affect only mapped pages in this complete all-frame heap
  // closure. Unbounded root addresses and replacements retain the old whole-replay fallback.
  const bool unknownSparse = sparse &&
      (unboundedRootUAV || !MarkReplaySparseComponentDirty(id));
  if(replaced || unknownSparse)
  {
    if(m_ReplayDirtyValid && m_ReplayDirtyTiming)
    {
      // Record only the first loss of an otherwise complete proof. The whole-list potential-write
      // superset may include an action beyond a partial target; these are uses, not proof that
      // each listed action actually ran. Preserve both identities instead of blaming live-ID maps.
      const D3D12CommandData &cmd = *m_Queue->GetCommandData();
      const D3D12_RESOURCE_DESC desc = resource->second->GetDesc();
      const ResourceId resolved = GetResID(GetResourceManager()->GetResAs<ID3D12Resource>(id));
      const auto &usage = m_Queue->GetUsage(id);
      rdcstr uses;
      for(size_t i = 0; i < RDCMIN(size_t(16), usage.size()); i++)
        uses += StringFormat::Fmt("%s%u:%s", i ? "," : "", usage[i].eventId,
                                  ToStr(usage[i].usage).c_str());
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12DirtyProof op=invalidate reason=replacedOrSparseWrite resource=%s "
          "resolvedResource=%s unreplacedResource=%s replaced=%u sparse=%u "
          "streamEvent=%u requestedEnd=%u lastRecordedList=%s targetPartialParent=%s "
          "rootUAV=%u dimension=%s width=%llu height=%u depthOrArraySize=%u "
          "format=%s flags=%s heap=%s heapOffset=%llu usageCount=%zu uses=%s",
          ToStr(id).c_str(), ToStr(resolved).c_str(),
          ToStr(GetResourceManager()->GetUnreplacedID(id)).c_str(), replaced ? 1U : 0U,
          sparse ? 1U : 0U, cmd.m_RootEventID, cmd.m_LastEventID,
          ToStr(cmd.m_LastCmdListID).c_str(),
          ToStr(cmd.m_Partial[D3D12CommandData::Primary].partialParent).c_str(),
          unboundedRootUAV ? 1U : 0U, ToStr(desc.Dimension).c_str(),
          (unsigned long long)desc.Width, desc.Height, uint32_t(desc.DepthOrArraySize),
          ToStr(desc.Format).c_str(), ToStr(desc.Flags).c_str(),
          ToStr(GetResID(resource->second->GetHeap())).c_str(),
          (unsigned long long)resource->second->GetHeapOffset(), usage.size(), uses.c_str());
      RDCLOG("%s", line.c_str());
      AppendReplayTimingLog(line);
    }
    InvalidateReplayDirtyTracking("replacedOrSparseWrite");
  }
  WrappedID3D12Heap *heap = resource->second->GetHeap();
  if(heap)
  {
    const ResourceId heapID = GetResID(heap);
    MarkReplaySparseComponentDirty(heapID);
    m_ModResources.insert(heapID);
    auto allocation = m_ReplayDirtyAllocations.find(id);
    if(allocation == m_ReplayDirtyAllocations.end())
    {
      const D3D12_RESOURCE_DESC desc = resource->second->GetDesc();
      const uint64_t first = resource->second->GetHeapOffset();
      const uint64_t size = GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
      const uint64_t heapSize = heap->GetDesc().SizeInBytes;
      if(!size || size == ~0ULL || first > heapSize || size > heapSize - first)
      {
        InvalidateReplayDirtyTracking("unknownPlacedAllocation");
        return;
      }
      allocation = m_ReplayDirtyAllocations.insert({id, {first, first + size}}).first;
    }
    // Root UAVs have no descriptor bounds; preserve the old conservative whole-heap treatment
    // for addresses which can access past the logical placed buffer's view.
    if(unboundedRootUAV)
      MergeReplayDirtyRange(m_ReplayDirtyHeapRanges[heapID], 0, ~0ULL);
    else
      MergeReplayDirtyRange(m_ReplayDirtyHeapRanges[heapID], allocation->second.first,
                           allocation->second.second);
  }
}

bool WrappedID3D12Device::ReplayBufferRangeTrackingEligible(ResourceId id, uint64_t &width)
{
  auto resource = GetResourceList().find(id);
  if(resource == GetResourceList().end())
    return false;
  const D3D12_RESOURCE_DESC desc = resource->second->GetDesc();
  width = desc.Width;
  D3D12_HEAP_PROPERTIES properties = {};
  D3D12_HEAP_FLAGS flags = D3D12_HEAP_FLAG_NONE;
  const bool heapKnown = SUCCEEDED(resource->second->GetReal()->GetHeapProperties(&properties, &flags));
  return ReplayBufferDirtyEligible(desc, resource->second->GetHeap() != NULL,
                                   m_SparseResources.count(id) != 0,
                                   GetResourceManager()->HasReplacement(id), heapKnown, flags);
}

void WrappedID3D12Device::CacheReplayCPUWriteBaseline(uint64_t chunk, ResourceId id, uint64_t offset,
                                                     const byte *data, uint64_t length)
{
  // Captured Unmap blobs may conservatively contain the whole buffer. Compare only while loading,
  // against serialized immutable bytes, never against the live resource's current GPU contents.
  const uint64_t byteBudget = 64ULL * 1024 * 1024;
  if(!IsLoading(m_State) || !data || !length || offset > SIZE_MAX || length > SIZE_MAX - offset ||
     length > byteBudget - m_ReplayCPUWriteComparedBytes ||
     m_ReplayCPUWriteBaselines.size() >= 4096 ||
     Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_CPU_WRITE_DIRTY_RANGES") == "1")
    return;
  uint64_t width = 0;
  if(!ReplayBufferRangeTrackingEligible(id, width) || offset > width || length > width - offset)
    return;
  const D3D12InitialContents initial = GetResourceManager()->GetInitialContents(id);
  if(initial.tag != D3D12InitialContents::Copy || initial.resourceType != Resource_Resource ||
     initial.sparseBinds || initial.dataSize < width)
    return;

  m_ReplayCPUWriteComparedBytes += length;
  const byte *baseline = NULL;
  bytebuf copied;
  if(initial.srcData)
    baseline = initial.srcData + size_t(offset);
  else if(initial.resource)
  {
    ID3D12Resource *source = (ID3D12Resource *)initial.resource;
    D3D12_HEAP_PROPERTIES properties = {};
    const D3D12_RESOURCE_DESC desc = source->GetDesc();
    if(desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER || desc.Width < width ||
       FAILED(source->GetHeapProperties(&properties, NULL)) || properties.Type != D3D12_HEAP_TYPE_UPLOAD)
      return;
    const D3D12_RANGE read = {SIZE_T(offset), SIZE_T(offset + length)};
    void *mapped = NULL;
    // The original Copy source was created by Serialise_InitialState and retains its wrapper.
    // Bypass wrapper bookkeeping for this read-only CPU inspection of that independent source.
    ID3D12Resource *nativeSource = Unwrap(source);
    if(FAILED(nativeSource->Map(0, &read, &mapped)))
      return;
    const D3D12_RANGE noWrite = {0, 0};
    if(!mapped)
    {
      nativeSource->Unmap(0, &noWrite);
      return;
    }
    copied.resize(size_t(length));
    memcpy(copied.data(), (const byte *)mapped + size_t(offset), size_t(length));
    nativeSource->Unmap(0, &noWrite);
    baseline = copied.data();
  }
  ReplayCPUWriteBaseline evidence;
  evidence.resource = id;
  evidence.offset = offset;
  evidence.length = length;
  if(ReplayCPUWriteBaselineDifferences(baseline, data, offset, length, evidence.differences))
  {
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    {
      uint64_t bytes = 0;
      for(const auto &range : evidence.differences)
        bytes += range.second - range.first;
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12CPUWriteBaseline chunk=%llu resource=%s offset=%llu writtenBytes=%llu "
          "differentBytes=%llu ranges=%zu comparedBytes=%llu",
          (unsigned long long)chunk, ToStr(id).c_str(), (unsigned long long)offset,
          (unsigned long long)length, (unsigned long long)bytes, evidence.differences.size(),
          (unsigned long long)m_ReplayCPUWriteComparedBytes);
      RDCLOG("%s", line.c_str());
      AppendReplayTimingLog(line);
    }
    m_ReplayCPUWriteBaselines[chunk] = std::move(evidence);
  }
}

bool WrappedID3D12Device::MarkReplayCPUWriteBaseline(uint64_t chunk, ResourceId id, uint64_t offset,
                                                    uint64_t length)
{
  if(!m_ReplayDirtyBufferRangesEnabled || !m_ReplayDirtyValid || m_ReplayDirtyApplyingInitial ||
     HasFatalError())
    return false;
  const auto found = m_ReplayCPUWriteBaselines.find(chunk);
  if(found == m_ReplayCPUWriteBaselines.end() || found->second.resource != id ||
     found->second.offset != offset || found->second.length != length)
    return false;
  uint64_t width = 0;
  if(!ReplayBufferRangeTrackingEligible(id, width))
    return false;
  ApplyReplayCPUWriteBaselineDebt(
      id, found->second.differences, m_ReplayDirtyResources, m_ReplayDirtyBufferRanges,
      m_ReplayDirtyValid, [&]() { MarkReplayResourceDirty(id); },
      [&](uint64_t first, uint64_t bytes) { MarkReplayBufferRangeDirty(id, first, bytes); });
  return true;
}

void WrappedID3D12Device::MarkReplayBufferRangeDirty(ResourceId id, uint64_t offset, uint64_t length)
{
  if(!IsActiveReplaying(m_State) || id == ResourceId() || ResourceIDGen::IsReplayOnlyID(id))
    return;
  const bool previouslyDirty = m_ReplayDirtyResources.count(id) != 0;
  rdcarray<rdcpair<uint64_t, uint64_t>> ranges;
  auto previous = m_ReplayDirtyBufferRanges.find(id);
  if(previous != m_ReplayDirtyBufferRanges.end())
    ranges.swap(previous->second);
  // Preserve the existing live-state, checkpoint-version and unknown/alias invalidation hooks.
  // Whole writes erase the interval entry, and can never be narrowed by a later precise write.
  MarkReplayResourceDirty(id);
  uint64_t width = 0;
  const bool eligible = m_ReplayDirtyBufferRangesEnabled && m_ReplayDirtyValid &&
                        !m_ReplayDirtyApplyingInitial && !HasFatalError() &&
                        ReplayBufferRangeTrackingEligible(id, width);
  if(MergeReplayBufferDirtyWrite(previouslyDirty, eligible, width, offset, length, ranges))
    m_ReplayDirtyBufferRanges[id] = std::move(ranges);
}

void WrappedID3D12Device::FilterReplayDirtyBufferCopyRanges(
    ResourceId id, uint64_t byteCount, rdcarray<rdcpair<uint64_t, uint64_t>> &ranges)
{
  const auto dirty = m_ReplayDirtyBufferRanges.find(id);
  if(!m_UseReplayDirtyInitialContents || !m_ReplayDirtyBufferRangesEnabled ||
     dirty == m_ReplayDirtyBufferRanges.end())
    return;
  uint64_t width = 0;
  const bool eligible = ReplayBufferRangeTrackingEligible(id, width) && byteCount == width;
  auto selected = SelectReplayBufferDirtyCopies(eligible, byteCount, dirty->second, ranges);
  if(m_ReplayDirtyTiming)
  {
    uint64_t originalBytes = 0, copiedBytes = 0;
    for(const auto &range : ranges)
      originalBytes += range.second - range.first;
    for(const auto &range : selected)
      copiedBytes += range.second - range.first;
    m_ReplayDirtyCopyStats[id].skippedBytes += originalBytes - copiedBytes;
  }
  if(!m_ReplayDirtyShadow)
    ranges.swap(selected);
}

bool WrappedID3D12Device::IsReplayInitialContentsUnchanged(ResourceId resource, uint64_t sourceBytes)
{
  if(!m_UseReplayDirtyInitialContents || m_ReplayDirtyResources.count(resource) ||
     m_ReplayDirtyHeapRanges.count(resource))
    return false;
  auto found = GetResourceList().find(resource);
  if(found != GetResourceList().end() && found->second->GetHeap())
  {
    // Ordinary placed resources normally use the whole-heap initial path. Do not introduce a
    // second assumption for custom/created per-resource initial contents of an aliasing heap.
    if(m_ReplayDirtyHeapRanges.count(GetResID(found->second->GetHeap())))
      return false;
  }
  if(m_ReplayDirtyTiming)
    m_ReplayDirtyCopyStats[resource].skippedBytes += sourceBytes;
  return !m_ReplayDirtyShadow;
}

static D3D12ReplayInitialCopySource ReplayInitialCopyHeapClass(bool known, D3D12_HEAP_TYPE type)
{
  if(known && type == D3D12_HEAP_TYPE_UPLOAD)
    return D3D12ReplayInitialCopySource::Upload;
  if(known && type == D3D12_HEAP_TYPE_DEFAULT)
    return D3D12ReplayInitialCopySource::Default;
  return D3D12ReplayInitialCopySource::Other;
}

D3D12ReplayInitialCopySource WrappedID3D12Device::GetReplayInitialCopySource(
    ID3D12Resource *nativeSource) const
{
  // The disabled path must not query the driver or create any timing storage.
  if(!m_ReplayDirtyTiming)
    return D3D12ReplayInitialCopySource::Other;
  D3D12_HEAP_PROPERTIES properties = {};
  const bool known = nativeSource && SUCCEEDED(nativeSource->GetHeapProperties(&properties, NULL));
  return ReplayInitialCopyHeapClass(known, properties.Type);
}

void WrappedID3D12Device::RecordReplayInitialCopy(ResourceId resource, uint64_t bytes,
                                                D3D12ReplayInitialCopySource source,
                                                bool footprint, bool wholeResource)
{
  if(m_ReplaySparseInitialResidencyEnabled && m_ReplayDirtyApplyingInitial)
  {
    const auto component = m_ReplaySparseComponentIndex.find(resource);
    if(component != m_ReplaySparseComponentIndex.end())
    {
      auto &baseline = m_ReplaySparseComponents[component->second].baseline;
      if(wholeResource)
        baseline.Invalidate("unmeasuredInitialWholeResourceCopy");
      else
        baseline.Record(resource, bytes);
    }
  }
  if(!m_ReplayDirtyTiming)
    return;
  ReplayDirtyCopyStats &stats = m_ReplayDirtyCopyStats[resource];
  stats.actualBytes += wholeResource ? 0 : bytes;
  stats.wholeCopies += wholeResource ? 1 : 0;
  stats.sources.Record(bytes, source, footprint, wholeResource);
}

void WrappedID3D12Device::MarkCheckpointReplayWrites()
{
  InvalidateReplaySparseInitialBaselines("checkpointContents");
  if(m_ReplayCheckpoints)
  {
    m_ReplayCheckpoints->markingRestoredContents = true;
    for(const auto &resource : m_ReplayCheckpoints->currentVersions)
      MarkReplayResourceDirty(resource.first);
    for(const auto &aliases : m_ReplayCheckpoints->currentBufferAliases)
      for(ResourceId member : aliases.second->component.members)
        MarkReplayResourceDirty(member);
    m_ReplayCheckpoints->markingRestoredContents = false;
  }
}

void WrappedID3D12Device::MarkReplayFullOverwriteSubmitted(ResourceId id)
{
  if(!m_ReplayCheckpoints || HasFatalError() || !IsActiveReplaying(m_State))
    return;
  auto &cache = *m_ReplayCheckpoints;
  if(!cache.deferredInitialResources.erase(id))
    return;
  // The full overwrite is now ordered on the real replay queue. It supersedes the old initial
  // bytes; later writes in this submission may change its contents again, so publish a fresh
  // identity rather than reusing the initial/checkpoint identity. Save still waits for the queue.
  cache.residentVersions.MarkResident(cache.ResidentRange(id),
                                     {++cache.contentSerial, cache.residentVersions.EditGeneration()});
  m_ReplayDirtyResources.insert(id);
  m_ReplayDirtyBufferRanges.erase(id);
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12OverwriteMaterialize resource=%s pending=%zu", ToStr(id).c_str(),
        cache.deferredInitialResources.size());
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

void WrappedID3D12Device::AddCaptureSubmission()
{
  if(IsActiveCapturing(m_State))
  {
    // 15 is quite a lot of submissions.
    const int expectedMaxSubmissions = 15;

    RenderDoc::Inst().SetProgress(CaptureProgress::FrameCapture,
                                  FakeProgress(m_SubmitCounter, expectedMaxSubmissions));
    m_SubmitCounter++;
  }
}

void WrappedID3D12Device::CheckForDeath()
{
  if(!m_Alive)
    return;

  if(m_RefCounter.GetRefCount() == 0)
  {
    RDCASSERT(m_SoftRefCounter.GetRefCount() >= m_InternalRefcount);

    // MEGA HACK
    if(m_SoftRefCounter.GetRefCount() <= m_InternalRefcount || IsReplayMode(m_State))
    {
      m_Alive = false;
      delete this;
    }
  }
}

void WrappedID3D12Device::FirstFrame(IDXGISwapper *swapper)
{
  // if we have to capture the first frame, begin capturing immediately
  if(IsBackgroundCapturing(m_State) && RenderDoc::Inst().ShouldTriggerCapture(0))
  {
    RenderDoc::Inst().StartFrameCapture(
        DeviceOwnedWindow((ID3D12Device *)this, swapper ? swapper->GetHWND() : NULL));

    m_FirstFrameCapture = true;
    m_FirstFrameCaptureWindow = swapper ? swapper->GetHWND() : NULL;
    m_AppControlledCapture = false;
    m_CapturedFrames.back().frameNumber = 0;
  }
}

void WrappedID3D12Device::ApplyBarriers(BarrierSet &barriers)
{
  SCOPED_LOCK(m_ResourceStatesLock);
  GetResourceManager()->ApplyBarriers(barriers, m_ResourceStates);
}

void WrappedID3D12Device::ReleaseSwapchainResources(IDXGISwapper *swapper, UINT QueueCount,
                                                    IUnknown *const *ppPresentQueue,
                                                    IUnknown **unwrappedQueues)
{
  if(m_LastSwap == swapper)
    m_LastSwap = NULL;

  if(ppPresentQueue)
  {
    for(UINT i = 0; i < QueueCount; i++)
    {
      WrappedID3D12CommandQueue *wrappedQ = (WrappedID3D12CommandQueue *)ppPresentQueue[i];
      RDCASSERT(WrappedID3D12CommandQueue::IsAlloc(wrappedQ));

      unwrappedQueues[i] = wrappedQ->GetReal();
    }
  }

  for(int i = 0; i < swapper->GetNumBackbuffers(); i++)
  {
    ID3D12Resource *res = (ID3D12Resource *)swapper->GetBackbuffers()[i];

    if(!res)
      continue;

    WrappedID3D12Resource *wrapped = (WrappedID3D12Resource *)res;
    wrapped->ReleaseInternalRef();
    SAFE_RELEASE(wrapped);
  }

  auto it = m_SwapChains.find(swapper);
  if(it != m_SwapChains.end())
  {
    for(int i = 0; i < swapper->GetNumBackbuffers(); i++)
      FreeRTV(it->second.rtvs[i]);

    m_SwapChains.erase(it);
  }
}

void WrappedID3D12Device::NewSwapchainBuffer(IUnknown *backbuffer)
{
  ID3D12Resource *pRes = (ID3D12Resource *)backbuffer;

  if(pRes)
  {
    WrappedID3D12Resource *wrapped = (WrappedID3D12Resource *)pRes;
    wrapped->AddInternalRef();
  }
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_WrapSwapchainBuffer(SerialiserType &ser, IDXGISwapper *swapper,
                                                        DXGI_FORMAT bufferFormat, UINT Buffer,
                                                        IUnknown *realSurface)
{
  WrappedID3D12Resource *pRes =
      (WrappedID3D12Resource *)(WrappedDeviceChild12<ID3D12Resource, ID3D12Resource1, ID3D12Resource2> *)
          realSurface;

  SERIALISE_ELEMENT(Buffer);
  SERIALISE_ELEMENT_LOCAL(SwapbufferID, GetResID(pRes)).TypedAs("ID3D12Resource *"_lit);
  SERIALISE_ELEMENT_LOCAL(BackbufferDescriptor, pRes->GetDesc()).Important();

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading())
  {
    ID3D12Resource *fakeBB = NULL;

    DXGI_FORMAT SwapbufferFormat = BackbufferDescriptor.Format;

    // DXGI swap chain back buffers can be freely cast as a special-case.
    // translate the format to a typeless format to allow for this.
    // the original type is stored separately below
    BackbufferDescriptor.Format = GetTypelessFormat(BackbufferDescriptor.Format);

    HRESULT hr = S_OK;

    D3D12_HEAP_PROPERTIES heapProps;
    heapProps.Type = D3D12_HEAP_TYPE_DEFAULT;
    heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    heapProps.CreationNodeMask = 1;
    heapProps.VisibleNodeMask = 1;

    // create in common, which is the same as present
    hr = m_pDevice->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &BackbufferDescriptor,
                                            D3D12_RESOURCE_STATE_COMMON, NULL,
                                            __uuidof(ID3D12Resource), (void **)&fakeBB);

    AddResource(SwapbufferID, ResourceType::SwapchainImage, "Swapchain Image");

    if(FAILED(hr))
    {
      RDCERR("Failed to create fake back buffer, HRESULT: %s", ToStr(hr).c_str());
    }
    else
    {
      WrappedID3D12Resource *wrapped = new WrappedID3D12Resource(SwapbufferID, fakeBB, NULL, 0, this);
      fakeBB = wrapped;

      fakeBB->SetName(L"Swap Chain Buffer");

      m_BackbufferFormat[wrapped->GetResourceID()] = SwapbufferFormat;

      SubresourceStateVector &states = m_ResourceStates[wrapped->GetResourceID()];

      states = {D3D12ResourceLayout::FromStates(D3D12_RESOURCE_STATE_PRESENT)};
    }
  }

  return true;
}

IUnknown *WrappedID3D12Device::WrapSwapchainBuffer(IDXGISwapper *swapper, DXGI_FORMAT bufferFormat,
                                                   UINT buffer, IUnknown *realSurface)
{
  ID3D12Resource *pRes = NULL;

  ID3D12Resource *query = NULL;
  realSurface->QueryInterface(__uuidof(ID3D12Resource), (void **)&query);
  if(query)
    query->Release();

  if(GetResourceManager()->HasWrapper(query))
  {
    pRes = (ID3D12Resource *)GetResourceManager()->GetWrapper(query);
    pRes->AddRef();

    realSurface->Release();
  }
  else if(WrappedID3D12Resource::IsAlloc(query))
  {
    // this could be possible if we're doing downlevel presenting
    pRes = query;
  }
  else
  {
    pRes = new WrappedID3D12Resource(ResourceId(), (ID3D12Resource *)realSurface, NULL, 0, this);

    ResourceId id = GetResID(pRes);

    // there shouldn't be a resource record for this texture as it wasn't created via
    // Create*Resource
    RDCASSERT(id != ResourceId() && !GetResourceManager()->HasResourceRecord(id));

    if(IsCaptureMode(m_State))
    {
      D3D12ResourceRecord *record = GetResourceManager()->AddResourceRecord(id);
      record->type = Resource_Resource;
      record->DataInSerialiser = false;
      record->Length = 0;

      WrappedID3D12Resource *wrapped = (WrappedID3D12Resource *)pRes;

      wrapped->SetResourceRecord(record);

      WriteSerialiser &ser = GetThreadSerialiser();

      SCOPED_SERIALISE_CHUNK(D3D12Chunk::CreateSwapBuffer);

      Serialise_WrapSwapchainBuffer(ser, swapper, bufferFormat, buffer, pRes);

      record->AddChunk(scope.Get());

      {
        SCOPED_LOCK(m_ResourceStatesLock);
        SubresourceStateVector &states = m_ResourceStates[id];

        states = {D3D12ResourceLayout::FromStates(D3D12_RESOURCE_STATE_PRESENT)};
      }
    }
  }

  if(IsCaptureMode(m_State))
  {
    D3D12_RENDER_TARGET_VIEW_DESC rtvDesc;
    rtvDesc.Format = GetSRGBFormat(bufferFormat);
    rtvDesc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
    rtvDesc.Texture2D.MipSlice = 0;
    rtvDesc.Texture2D.PlaneSlice = 0;

    D3D12_CPU_DESCRIPTOR_HANDLE rtv = AllocRTV();

    if(rtv.ptr != 0)
      CreateRenderTargetView(pRes, NULL, rtv);

    FreeRTV(m_SwapChains[swapper].rtvs[buffer]);
    m_SwapChains[swapper].rtvs[buffer] = rtv;

    ID3DDevice *swapDev = swapper->GetD3DDevice();
    if(WrappedID3D12CommandQueue::IsAlloc(swapDev))
      m_SwapChains[swapper].queue = (WrappedID3D12CommandQueue *)swapDev;
    else if(swapDev == this)
      m_SwapChains[swapper].queue = m_Queue;
    else
      RDCERR("Unexpected swapper D3D device");
  }

  return pRes;
}

IDXGIResource *WrappedID3D12Device::WrapExternalDXGIResource(IDXGIResource *res)
{
  SCOPED_LOCK(m_WrapDeduplicateLock);

  ID3D12Resource *d3d12res;
  res->QueryInterface(__uuidof(ID3D12Resource), (void **)&d3d12res);
  if(GetResourceManager()->HasWrapper(d3d12res))
  {
    ID3D12DeviceChild *wrapper = GetResourceManager()->GetWrapper(d3d12res);
    IDXGIResource *ret = NULL;
    wrapper->QueryInterface(__uuidof(IDXGIResource), (void **)&ret);
    res->Release();
    return ret;
  }

  void *voidRes = (void *)res;
  OpenSharedHandleInternal(D3D12Chunk::Device_ExternalDXGIResource, D3D12_HEAP_FLAG_NONE,
                           __uuidof(IDXGIResource), &voidRes);
  return (IDXGIResource *)voidRes;
}

void WrappedID3D12Device::Map(ID3D12Resource *Resource, UINT Subresource)
{
  MapState map;
  map.res = Resource;
  map.subres = Subresource;

  D3D12_RESOURCE_DESC desc = Resource->GetDesc();

  D3D12_HEAP_PROPERTIES heapProps;
  Resource->GetHeapProperties(&heapProps, NULL);

  // ignore maps of readback resources, these cannot ever reach the GPU because the resource is
  // stuck in COPY_DEST state.
  if(heapProps.Type == D3D12_HEAP_TYPE_READBACK)
    return;

  m_pDevice->GetCopyableFootprints(&desc, Subresource, 1, 0, NULL, NULL, NULL, &map.totalSize);

  {
    SCOPED_LOCK(m_MapsLock);
    m_Maps.push_back(map);
  }
}

void WrappedID3D12Device::Unmap(ID3D12Resource *Resource, UINT Subresource, byte *mapPtr,
                                const D3D12_RANGE *pWrittenRange)
{
  MapState map = {};

  D3D12_HEAP_PROPERTIES heapProps;
  Resource->GetHeapProperties(&heapProps, NULL);

  // ignore maps of readback resources, these cannot ever reach the GPU because the resource is
  // stuck in COPY_DEST state.
  if(heapProps.Type == D3D12_HEAP_TYPE_READBACK)
    return;

  map.res = Resource;
  map.subres = Subresource;

  {
    SCOPED_LOCK(m_MapsLock);
    int32_t idx = m_Maps.indexOf(map);

    if(idx < 0)
      return;

    map = m_Maps.takeAt(idx);
  }

  bool capframe = false;
  {
    SCOPED_READLOCK(m_CapTransitionLock);
    capframe = IsActiveCapturing(m_State);
  }

  D3D12_RANGE range = {0, (SIZE_T)map.totalSize};

  // unfortunately this can't be trusted - e.g. imgui maps/unmaps with empty written range. We have
  // to assume the worst and assume it's all modified
#if 0
  if(pWrittenRange)
  {
    range = *pWrittenRange;

    // clamp end if it's lower than begin
    if(range.End < range.Begin)
      range.End = range.Begin;
  }
#endif

  if(capframe)
    MapDataWrite(Resource, Subresource, mapPtr, range, false);
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_MapDataWrite(SerialiserType &ser, ID3D12Resource *Resource,
                                                 UINT Subresource, byte *MappedData,
                                                 D3D12_RANGE range, bool coherentFlush)
{
  SERIALISE_ELEMENT(Resource).Important();
  SERIALISE_ELEMENT(Subresource);

  // tracks if we're uploading the data to a persistent buffer and don't do the upload from CPU each
  // time. If this is true, during load we init the buffer and upload, then on replay each time we
  // just do a GPU-side copy. If this is false, during init we save the range (since currently it's
  // serialised after the data), then during replay we map the resource and serialise directly into
  // it.
  bool gpuUpload = false;

  ResourceId id;
  if(IsReplayingAndReading() && Resource)
  {
    id = GetResID(Resource);
    if(m_UploadResourceIds.find(id) != m_UploadResourceIds.end())
      gpuUpload = true;
  }

  MappedData += range.Begin;

  const D3D12_RANGE nopRange = {0, 0};

  // we serialise MappedData manually, so that when we don't need it we just skip instead of
  // actually allocating and memcpy'ing the buffer.
  SerialiserFlags flags = SerialiserFlags::AllocateMemory;
  if(IsActiveReplaying(m_State))
  {
    if(gpuUpload)
    {
      // set the array to explicitly NULL (so it doesn't crash on deserialise) and don't allocate.
      // This will cause it to be skipped
      MappedData = NULL;
      flags = SerialiserFlags::NoFlags;
    }
    else
    {
      // The serialised blob is written directly through a CPU mapping. Any earlier captured or
      // internal GPU use of this resource must be complete before the CPU can mutate it.
      ScopedReplayTimingContext timingContext(ReplayTimingContext::MapDataWrite);
      if(!ReplayWorkWaitForIdle())
        return false;

      HRESULT hr = Resource->Map(Subresource, &nopRange, (void **)&MappedData);
      CHECK_HR(this, hr);
      if(FAILED(hr))
      {
        RDCERR("Failed to map resource on replay HRESULT: %s", ToStr(hr).c_str());
      }

      // write into the appropriate offset, don't allocate
      if(MappedData != NULL)
        MappedData += m_UploadRanges[m_Queue->GetCommandData()->m_CurChunkOffset].Begin;
      flags = SerialiserFlags::NoFlags;
    }
  }

  ser.Serialise("MappedData"_lit, MappedData, range.End - range.Begin, flags).Important();

  size_t dataOffset = 0;
  if(ser.IsWriting())
    dataOffset = size_t(ser.GetWriter()->GetOffset() - (range.End - range.Begin));

  SERIALISE_ELEMENT(range);

  uint64_t rangeSize = range.End - range.Begin;

  if(!gpuUpload && ser.IsReading())
  {
    if(IsLoading(m_State))
    {
      byte *writePtr = NULL;

      // Loading can also submit work before a later direct-map chunk. Keep this path fail-closed;
      // consecutive CPU-only writes coalesce through ReplayWorkWaitForIdle().
      ScopedReplayTimingContext timingContext(ReplayTimingContext::MapDataWrite);
      if(!ReplayWorkWaitForIdle())
        return false;

      HRESULT hr = Resource->Map(Subresource, &nopRange, (void **)&writePtr);
      CHECK_HR(this, hr);
      if(FAILED(hr))
      {
        RDCERR("Failed to map resource on replay HRESULT: %s", ToStr(hr).c_str());
      }

      if(writePtr)
      {
        writePtr += range.Begin;
        memcpy(writePtr, MappedData, (size_t)rangeSize);
        Resource->Unmap(Subresource, &range);
      }
    }
    else if(IsActiveReplaying(m_State))
    {
      // if we mapped the resource for a cpu upload, we can unmap now
      // set the pointer to NULL so that it doesn't get freed later
      PatchReplayBufferCPU(id, range.Begin, MappedData, rangeSize);
      MappedData = NULL;
      Resource->Unmap(Subresource, &range);
    }
  }

  SERIALISE_CHECK_READ_ERRORS();

  if(ser.IsWriting() && coherentFlush)
  {
    byte *ref = GetWrapped(Resource)->GetShadow(Subresource);
    const byte *serialisedData = ser.GetWriter()->GetData() + dataOffset;

    if(ref)
    {
      memcpy(ref + range.Begin, serialisedData, size_t(range.End - range.Begin));
    }
    else
    {
      RDCERR("Shadow data not allocated when coherent map flush is processed!");
    }
  }

  // don't do anything if end <= begin because the range is empty.
  if(IsReplayingAndReading() && Resource && range.End > range.Begin)
  {
    D3D12CommandData &cmd = *m_Queue->GetCommandData();
    if(Subresource == 0)
    {
      if(IsLoading(m_State))
        CacheReplayCPUWriteBaseline(cmd.m_CurChunkOffset, GetResID(Resource), range.Begin, MappedData,
                                   range.End - range.Begin);
      else if(!MarkReplayCPUWriteBaseline(cmd.m_CurChunkOffset, GetResID(Resource), range.Begin,
                                         range.End - range.Begin))
        MarkReplayBufferRangeDirty(GetResID(Resource), range.Begin, range.End - range.Begin);
    }
    else
      MarkReplayResourceDirty(GetResID(Resource));

    if(IsLoading(m_State))
    {
      cmd.AddCPUUsage(GetResID(Resource), ResourceUsage::CPUWrite);

      // when CPU uploading we just save the range (wouldn't be necessary if the range was
      // serialised before the blob)
      if(!gpuUpload)
      {
        m_UploadRanges[cmd.m_CurChunkOffset] = range;
      }
      else
      {
        ID3D12Resource *uploadBuf = GetUploadBuffer(cmd.m_CurChunkOffset, rangeSize);

        if(!uploadBuf)
        {
          RDCERR("Couldn't get upload buffer");
          return false;
        }

        SetObjName(uploadBuf,
                   StringFormat::Fmt("Map data write, %llu bytes for %s/%u @ %llu", rangeSize,
                                     ToStr(id).c_str(), Subresource, cmd.m_CurChunkOffset));

        D3D12_RANGE maprange = {0, 0};
        void *dst = NULL;
        HRESULT hr = uploadBuf->Map(Subresource, &maprange, &dst);
        CHECK_HR(this, hr);

        if(SUCCEEDED(hr))
        {
          maprange.Begin = 0;
          maprange.End = SIZE_T(rangeSize);

          memcpy(dst, MappedData, maprange.End);

          uploadBuf->Unmap(Subresource, &maprange);
        }
        else
        {
          RDCERR("Failed to map resource on replay HRESULT: %s", ToStr(hr).c_str());
        }
      }
    }

    // now whether we are loading or replaying, if we're GPU uploading do the actual copy
    if(gpuUpload)
    {
      ID3D12Resource *uploadBuf = GetUploadBuffer(cmd.m_CurChunkOffset, rangeSize);

      if(!uploadBuf)
      {
        RDCERR("Couldn't get upload buffer");
        return false;
      }

      // then afterwards just execute a list to copy the result
      ID3D12GraphicsCommandList *uploadList = AcquireDataUploadList();
      if(uploadList == NULL)
        return false;

      HRESULT hr = uploadList->Reset(m_DataUploadAlloc, NULL);
      CHECK_HR(this, hr);
      if(FAILED(hr))
        return false;

      uploadList->CopyBufferRegion(Resource, range.Begin, uploadBuf, 0, range.End - range.Begin);
      RecordReplayBufferPatches(uploadList, Resource);

      hr = uploadList->Close();
      CHECK_HR(this, hr);
      if(FAILED(hr))
        return false;

      ID3D12CommandList *l = uploadList;

      // The upload copy runs on RenderDoc's internal queue. Chain it after the last captured queue
      // first so that overwriting a resource cannot race an earlier draw/dispatch that consumes it.
      m_Queue->SyncLastReplayQueueToInternal();
      GetQueue()->ExecuteCommandListsInternal(1, &l, false, false, true);

      m_DataUploadPending = true;
    }
  }

  // on loading we always alloc'd for this, whether gpu upload or not
  if(IsLoading(m_State))
    FreeAlignedBuffer(MappedData);

  return true;
}

template bool WrappedID3D12Device::Serialise_MapDataWrite(ReadSerialiser &ser,
                                                          ID3D12Resource *Resource,
                                                          UINT Subresource, byte *MappedData,
                                                          D3D12_RANGE range, bool coherentFlush);
template bool WrappedID3D12Device::Serialise_MapDataWrite(WriteSerialiser &ser,
                                                          ID3D12Resource *Resource,
                                                          UINT Subresource, byte *MappedData,
                                                          D3D12_RANGE range, bool coherentFlush);

void WrappedID3D12Device::MapDataWrite(ID3D12Resource *Resource, UINT Subresource, byte *mapPtr,
                                       D3D12_RANGE range, bool coherentFlush)
{
  CACHE_THREAD_SERIALISER();

  SCOPED_SERIALISE_CHUNK(coherentFlush ? D3D12Chunk::CoherentMapWrite : D3D12Chunk::Resource_Unmap);
  Serialise_MapDataWrite(ser, Resource, Subresource, mapPtr, range, coherentFlush);

  m_FrameCaptureRecord->AddChunk(scope.Get());

  GetResourceManager()->MarkResourceFrameReferenced(GetResID(Resource), eFrameRef_PartialWrite);
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_WriteToSubresource(SerialiserType &ser, ID3D12Resource *Resource,
                                                       UINT Subresource, const D3D12_BOX *pDstBox,
                                                       const void *pSrcData, UINT SrcRowPitch,
                                                       UINT SrcDepthPitch)
{
  SERIALISE_ELEMENT(Resource).Important();
  SERIALISE_ELEMENT(Subresource);
  SERIALISE_ELEMENT_OPT(pDstBox);

  uint64_t dataSize = 0;

  if(ser.IsWriting())
  {
    D3D12_RESOURCE_DESC desc = Resource->GetDesc();

    // for buffers the data size is just the width of the box/resource
    if(desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER)
    {
      if(pDstBox)
        dataSize = RDCMIN(desc.Width, UINT64(pDstBox->right - pDstBox->left));
      else
        dataSize = desc.Width;
    }
    else
    {
      UINT width = RDCMAX(1U, UINT(desc.Width) >> Subresource);
      UINT height = RDCMAX(1U, desc.Height >> Subresource);
      // only 3D textures have a depth, array slices are separate subresources.
      UINT depth = (desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D
                        ? RDCMAX(1U, UINT(desc.DepthOrArraySize) >> Subresource)
                        : 1);

      // if we have a box, use its dimensions
      if(pDstBox)
      {
        width = RDCMIN(width, pDstBox->right - pDstBox->left);
        height = RDCMIN(height, pDstBox->bottom - pDstBox->top);
        depth = RDCMIN(depth, pDstBox->back - pDstBox->front);
      }

      if(IsBlockFormat(desc.Format))
        height = RDCMAX(1U, AlignUp4(height) / 4);
      else if(IsYUVPlanarFormat(desc.Format))
        height = GetYUVNumRows(desc.Format, height);

      dataSize = 0;

      // if we're copying multiple slices, all but the last one consume SrcDepthPitch bytes
      if(depth > 1)
        dataSize += SrcDepthPitch * (depth - 1);

      // similarly if we're copying multiple rows (possibly block-sized rows) in the final slice
      if(height > 1)
        dataSize += SrcRowPitch * (height - 1);

      // lastly, the final row (or block row) consumes just a tightly packed amount of data
      dataSize += GetRowPitch(width, desc.Format, 0);
    }
  }

  SERIALISE_ELEMENT_ARRAY(pSrcData, dataSize).Important();
  SERIALISE_ELEMENT(dataSize).Hidden();

  SERIALISE_ELEMENT(SrcRowPitch);
  SERIALISE_ELEMENT(SrcDepthPitch);

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading() && Resource)
  {
    D3D12CommandData &cmd = *m_Queue->GetCommandData();

    MarkReplayResourceDirty(GetResID(Resource));

    if(IsLoading(m_State))
      cmd.AddCPUUsage(GetResID(Resource), ResourceUsage::CPUWrite);

    ResourceId id = GetResID(Resource);
    if(m_UploadResourceIds.find(id) != m_UploadResourceIds.end())
    {
      ID3D12Resource *uploadBuf = GetUploadBuffer(cmd.m_CurChunkOffset, dataSize);

      // during reading, fill out the buffer itself
      if(IsLoading(m_State))
      {
        D3D12_RANGE range = {0, 0};
        void *dst = NULL;
        HRESULT hr = uploadBuf->Map(Subresource, &range, &dst);
        CHECK_HR(this, hr);

        if(SUCCEEDED(hr))
        {
          memcpy(dst, pSrcData, (size_t)dataSize);

          range.Begin = 0;
          range.End = (size_t)dataSize;

          uploadBuf->Unmap(Subresource, &range);
        }
        else
        {
          RDCERR("Failed to map resource on replay HRESULT: %s", ToStr(hr).c_str());
        }
      }

      // then afterwards just execute a list to copy the result
      ID3D12GraphicsCommandList *uploadList = AcquireDataUploadList();
      if(uploadList == NULL)
        return false;

      HRESULT hr = uploadList->Reset(m_DataUploadAlloc, NULL);
      CHECK_HR(this, hr);
      if(FAILED(hr))
        return false;

      UINT64 copySize = dataSize;
      if(pDstBox)
        copySize = RDCMIN(copySize, UINT64(pDstBox->right - pDstBox->left));
      uploadList->CopyBufferRegion(Resource, pDstBox ? pDstBox->left : 0, uploadBuf, 0, copySize);
      RecordReplayBufferPatches(uploadList, Resource);

      hr = uploadList->Close();
      CHECK_HR(this, hr);
      if(FAILED(hr))
        return false;

      ID3D12CommandList *l = uploadList;

      // See Serialise_MapDataWrite: order the internal copy after the captured queue tail before
      // submitting it, then the next captured submission will consume it through DataUploadSync.
      m_Queue->SyncLastReplayQueueToInternal();
      GetQueue()->ExecuteCommandListsInternal(1, &l, false, false, true);

      m_DataUploadPending = true;
    }
    else
    {
      // Unlike the upload-buffer path below, this mutates the live resource directly from the CPU.
      // Complete all replay work first; a later GPU-side dependency cannot repair a CPU/GPU race.
      ScopedReplayTimingContext timingContext(ReplayTimingContext::WriteToSubresource);
      if(!ReplayWorkWaitForIdle())
        return false;

      HRESULT hr = Resource->Map(Subresource, NULL, NULL);
      CHECK_HR(this, hr);

      if(SUCCEEDED(hr))
      {
        Resource->WriteToSubresource(Subresource, pDstBox, pSrcData, SrcRowPitch, SrcDepthPitch);

        if(!m_ReplayBufferPatches.empty() &&
           Resource->GetDesc().Dimension == D3D12_RESOURCE_DIMENSION_BUFFER)
        {
          void *mapped = NULL;
          const D3D12_RANGE noRead = {0, 0};
          HRESULT patchHR = Resource->Map(Subresource, &noRead, &mapped);
          CHECK_HR(this, patchHR);
          if(SUCCEEDED(patchHR) && mapped)
          {
            PatchReplayBufferCPU(id, 0, mapped, Resource->GetDesc().Width);
            Resource->Unmap(Subresource, NULL);
          }
        }

        Resource->Unmap(Subresource, NULL);
      }
      else
      {
        RDCERR("Failed to map resource on replay HRESULT: %s", ToStr(hr).c_str());
      }
    }
  }

  return true;
}

template bool WrappedID3D12Device::Serialise_WriteToSubresource(
    ReadSerialiser &ser, ID3D12Resource *Resource, UINT Subresource, const D3D12_BOX *pDstBox,
    const void *pSrcData, UINT SrcRowPitch, UINT SrcDepthPitch);
template bool WrappedID3D12Device::Serialise_WriteToSubresource(
    WriteSerialiser &ser, ID3D12Resource *Resource, UINT Subresource, const D3D12_BOX *pDstBox,
    const void *pSrcData, UINT SrcRowPitch, UINT SrcDepthPitch);

void WrappedID3D12Device::WriteToSubresource(ID3D12Resource *Resource, UINT Subresource,
                                             const D3D12_BOX *pDstBox, const void *pSrcData,
                                             UINT SrcRowPitch, UINT SrcDepthPitch)
{
  bool capframe = false;
  {
    SCOPED_READLOCK(m_CapTransitionLock);
    capframe = IsActiveCapturing(m_State);
  }

  if(capframe)
  {
    CACHE_THREAD_SERIALISER();

    SCOPED_SERIALISE_CHUNK(D3D12Chunk::Resource_WriteToSubresource);
    Serialise_WriteToSubresource(ser, Resource, Subresource, pDstBox, pSrcData, SrcRowPitch,
                                 SrcDepthPitch);

    m_FrameCaptureRecord->AddChunk(scope.Get());

    GetResourceManager()->MarkResourceFrameReferenced(GetResID(Resource), eFrameRef_PartialWrite);
  }
}

HRESULT WrappedID3D12Device::Present(ID3D12GraphicsCommandList *pOverlayCommandList,
                                     IDXGISwapper *swapper, UINT SyncInterval, UINT Flags)
{
  if((Flags & DXGI_PRESENT_TEST) != 0)
    return S_OK;

  if(IsBackgroundCapturing(m_State))
    RenderDoc::Inst().Tick();

  m_FrameCounter++;    // first present becomes frame #1, this function is at the end of the frame

#if RENDERDOC_ENABLE_API_MONITOR
  ApiMonitor::Inst().SetFrameNumber(m_FrameCounter);
#endif

  DeviceOwnedWindow devWnd((ID3D12Device *)this, swapper->GetHWND());

  bool activeWindow = RenderDoc::Inst().IsActiveWindow(devWnd);

  m_LastSwap = swapper;

  if(IsBackgroundCapturing(m_State))
  {
    uint32_t overlay = RenderDoc::Inst().GetOverlayBits();

    if(overlay & eRENDERDOC_Overlay_Enabled)
    {
      SwapPresentInfo &swapInfo = m_SwapChains[swapper];
      D3D12_CPU_DESCRIPTOR_HANDLE rtv = swapInfo.rtvs[swapper->GetLastPresentedBuffer()];

      if(rtv.ptr)
      {
        m_TextRenderer->SetOutputDimensions(swapper->GetWidth(), swapper->GetHeight(),
                                            swapper->GetFormat());

        ID3D12GraphicsCommandList *list = pOverlayCommandList;
        bool submitlist = false;

        if(!list)
        {
          // if we've done at least one round, check if the fence has been signalled for our last
          // use or wait on it if not
          if(m_CurOverlay > MaxOverlayInFlight)
          {
            const UINT64 waitCounter = m_CurOverlay - MaxOverlayInFlight;
            UINT64 counter = m_OverlayFence->GetCompletedValue();
            if(counter < waitCounter)
            {
              m_OverlayFence->SetEventOnCompletion(waitCounter, m_OverlaySyncHandle);

              WaitForSingleObject(m_OverlaySyncHandle, 10000);
            }
          }

          // reset the allocator
          m_OverlayAllocs[m_CurOverlay % MaxOverlayInFlight]->Reset();
          list = m_OverlayLists[m_CurOverlay % MaxOverlayInFlight];
          submitlist = true;

          list->Reset(m_OverlayAllocs[m_CurOverlay % MaxOverlayInFlight], NULL);
        }

        // buffer will be in common for presentation, transition to render target
        D3D12_RESOURCE_BARRIER barrier = {};
        barrier.Transition.pResource =
            (ID3D12Resource *)swapper->GetBackbuffers()[swapper->GetLastPresentedBuffer()];
        barrier.Transition.Subresource = 0;
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COMMON;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;

        list->ResourceBarrier(1, &barrier);

        list->OMSetRenderTargets(1, &rtv, FALSE, NULL);

        rdcstr overlayText =
            RenderDoc::Inst().GetOverlayText(RDCDriver::D3D12, devWnd, m_FrameCounter, 0);

        if(m_LastCaptureFailed > 0 && Timing::GetUnixTimestamp() - m_LastCaptureFailed < 5)
          overlayText += StringFormat::Fmt("\nCapture failed: %s",
                                           ResultDetails(m_LastCaptureError).Message().c_str());

        if(D3D12_Debug_RT_Overlay() && m_UsedRT)
        {
          ASStats blasStats = {}, tlasStats = {};
          RTGPUPatchingStats gpuStats = {};

          GetResourceManager()->GetRTManager()->GatherRTStatistics(blasStats, tlasStats, gpuStats);

          overlayText += "       TLAS               BLAS\n";

          for(size_t i = 0; i < ARRAY_COUNT(tlasStats.bucket); i++)
          {
            if(tlasStats.bucket[i].msThreshold == ~0U)
              overlayText += "  older    ";
            else
              overlayText += StringFormat::Fmt("<=% 4ums   ", tlasStats.bucket[i].msThreshold);

            overlayText += StringFormat::Fmt(
                "% 4u (% 3.2f MB)   % 4u (%.2f MB)\n", tlasStats.bucket[i].count,
                float(tlasStats.bucket[i].bytes) / 1048576.0f, blasStats.bucket[i].count,
                float(blasStats.bucket[i].bytes) / 1048576.0f);
          }

          overlayText += StringFormat::Fmt(
              "%.2f MB overhead, %.2f MB (%u BLAS %u TLAS) cached to disk\n",
              float(blasStats.overheadBytes + tlasStats.overheadBytes) / 1048576.0f,
              float(blasStats.diskBytes + tlasStats.diskBytes) / 1048576.0f, blasStats.diskCached,
              tlasStats.diskCached);

          overlayText += StringFormat::Fmt(
              "%3u BLAS input copies with %9.2f KB in %5.2f ms = %9.2f MB/s\n"
              "%2u dispatches patched in %4.2f ms\n",
              gpuStats.builds, float(gpuStats.buildBytes) / 1024.0f, gpuStats.totalBuildMS,
              gpuStats.totalBuildMS == 0.0
                  ? 0.0
                  : (float(gpuStats.buildBytes) / 1048576.0f) / (gpuStats.totalBuildMS / 1024.0f),
              gpuStats.dispatches, gpuStats.totalDispatchesMS);
        }

        m_TextRenderer->RenderText(list, 0.0f, 0.0f, overlayText);

        // transition backbuffer back again
        std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
        list->ResourceBarrier(1, &barrier);

        if(submitlist)
        {
          list->Close();

          // submit and signal this fence
          ID3D12CommandList *c = list;
          swapInfo.queue->ExecuteCommandListsInternal(1, &c, false, false);
          swapInfo.queue->GetReal()->Signal(Unwrap(m_OverlayFence), m_CurOverlay);

          m_CurOverlay++;
        }
      }
    }
  }

  RenderDoc::Inst().AddActiveDriver(RDCDriver::D3D12, true);

  // serialise the present call, even for inactive windows
  if(IsActiveCapturing(m_State))
  {
    SERIALISE_TIME_CALL();

    ID3D12Resource *backbuffer = NULL;

    if(swapper != NULL)
      backbuffer = (ID3D12Resource *)swapper->GetBackbuffers()[swapper->GetLastPresentedBuffer()];

    CACHE_THREAD_SERIALISER();

    SCOPED_SERIALISE_CHUNK(D3D12Chunk::Swapchain_Present);
    Serialise_Present(ser, backbuffer, SyncInterval, Flags);

    m_FrameCaptureRecord->AddChunk(scope.Get());
  }

  if(!activeWindow)
  {
    // first present to *any* window, even inactive, terminates frame 0
    if(m_FirstFrameCapture && IsActiveCapturing(m_State))
    {
      RenderDoc::Inst().EndFrameCapture(
          DeviceOwnedWindow((ID3D12Device *)this, m_FirstFrameCaptureWindow));
      m_FirstFrameCaptureWindow = NULL;
      m_FirstFrameCapture = false;
    }

    return S_OK;
  }

  // kill any current capture that isn't application defined
  if(IsActiveCapturing(m_State) && !m_AppControlledCapture)
    RenderDoc::Inst().EndFrameCapture(devWnd);

  if(IsBackgroundCapturing(m_State) && RenderDoc::Inst().ShouldTriggerCapture(m_FrameCounter))
  {
    RenderDoc::Inst().StartFrameCapture(devWnd);

    m_AppControlledCapture = false;
    m_CapturedFrames.back().frameNumber = m_FrameCounter;
  }

  return S_OK;
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_Present(SerialiserType &ser, ID3D12Resource *PresentedImage,
                                            UINT SyncInterval, UINT Flags)
{
  SERIALISE_ELEMENT_LOCAL(PresentedBackbuffer, GetResID(PresentedImage))
      .TypedAs("ID3D12Resource *"_lit)
      .Important();

  // we don't do anything with these parameters, they're just here to store
  // them for user benefits
  SERIALISE_ELEMENT(SyncInterval);
  SERIALISE_ELEMENT(Flags);

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading() && IsLoading(m_State))
  {
    D3D12CommandData &cmd = *m_Queue->GetCommandData();
    cmd.AddEvent();

    ActionDescription action;

    action.customName = StringFormat::Fmt("Present(%s)", ToStr(PresentedBackbuffer).c_str());
    action.flags |= ActionFlags::Present;

    cmd.m_LastPresentedImage = PresentedBackbuffer;
    action.copyDestination = PresentedBackbuffer;

    cmd.AddAction(action);
  }

  return true;
}

template bool WrappedID3D12Device::Serialise_Present(ReadSerialiser &ser,
                                                     ID3D12Resource *PresentedImage,
                                                     UINT SyncInterval, UINT Flags);
template bool WrappedID3D12Device::Serialise_Present(WriteSerialiser &ser,
                                                     ID3D12Resource *PresentedImage,
                                                     UINT SyncInterval, UINT Flags);

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_CaptureScope(SerialiserType &ser)
{
  SERIALISE_ELEMENT_LOCAL(frameNumber, m_CapturedFrames.back().frameNumber);

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayMode(m_State))
  {
    GetReplay()->WriteFrameRecord().frameInfo.frameNumber = frameNumber;
    RDCEraseEl(GetReplay()->WriteFrameRecord().frameInfo.stats);
  }

  return true;
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_BeginCaptureFrame(SerialiserType &ser)
{
  BarrierSet barriers;

  if(IsReplayingAndReading() && IsLoading(m_State))
  {
    m_InitialResourceStates = m_ResourceStates;

    GetDebugManager()->PrepareExecuteIndirectPatching(m_OrigGPUAddresses);
    GetResourceManager()->GetRTManager()->PrepareRayDispatchBuffer(&m_OrigGPUAddresses);
  }

  std::map<ResourceId, SubresourceStateVector> initialStates;

  {
    SCOPED_LOCK(m_ResourceStatesLock);    // not needed on replay, but harmless also
    GetResourceManager()->SerialiseResourceStates(ser, barriers, m_ResourceStates,
                                                  m_InitialResourceStates);
  }

  if(ser.VersionAtLeast(0x17))
  {
    SCOPED_LOCK(m_AnnotationsLock);

    SERIALISE_ELEMENT_LOCAL(numAnnotations, uint32_t(m_Annotations.size()));

    auto it = m_Annotations.begin();
    for(uint32_t i = 0; i < numAnnotations; i++)
    {
      SERIALISE_ELEMENT_LOCAL(id, it->first);
      SDObject *annotation = NULL;
      if(ser.IsReading())
      {
        annotation = new SDObject(""_lit, ""_lit);    // will be overwritten below
      }
      else
      {
        annotation = it->second;
        it++;
      }
      ser.Serialise("annotation"_lit, *annotation);

      if(ser.IsReading() && IsLoading(m_State))
      {
        m_Annotations[id] = annotation;
        m_Replay->GetResourceDesc(id).annotations = annotation;
      }
    }

    if(numAnnotations > 0)
      m_Replay->WriteFrameRecord().frameInfo.containsAnnotations = true;
  }

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading() && !barriers.empty())
  {
    ScopedReplayTimingContext timingContext(ReplayTimingContext::InitialRestore);
    ScopedReplayTimingSection timing(ReplayTimingSection::InitialBarriers);
    AddReplayTimingCounter(ReplayTimingCounter::InitialBarriers,
                          barriers.barriers.size() + barriers.newBarriers.size() +
                              barriers.newToOldBarriers.size());
    const bool descriptorIndependent = m_InternalCmds.pendingcmds.empty();
    // apply initial resource states
    ID3D12GraphicsCommandListX *list = GetNewList();
    if(!list)
      return false;

    barriers.Apply(list);

    list->Close();

    ExecuteLists(NULL, false, descriptorIndependent);

    // The initial-content copies and these final state transitions all execute on the internal
    // queue. Captured queues must wait for the last barrier, not only the earlier content-copy
    // submission. Keep their dependency on the GPU while retaining submitted command lists and
    // the shared allocator until the usual presentation or full-replay retirement point.
    // Loading, validation, and an explicit A/B override retain the original CPU wait.
    if(ShouldUseConservativeReplaySync() || !InstallInitialStateQueueDependencies())
    {
      ScopedReplayTimingContext fallbackContext(ReplayTimingContext::InitialFallback);
      if(!FlushLists())
        return false;
    }
  }

  return true;
}

template bool WrappedID3D12Device::Serialise_BeginCaptureFrame(ReadSerialiser &ser);
template bool WrappedID3D12Device::Serialise_BeginCaptureFrame(WriteSerialiser &ser);

void WrappedID3D12Device::EndCaptureFrame()
{
  WriteSerialiser &ser = GetThreadSerialiser();
  ser.SetActionChunk();
  SCOPED_SERIALISE_CHUNK(SystemChunk::CaptureEnd);

  // here for compatibility reasons, this used to store the presented Resource.
  SERIALISE_ELEMENT_LOCAL(PresentedBackbuffer, ResourceId()).Hidden();

  m_FrameCaptureRecord->AddChunk(scope.Get());
}

void WrappedID3D12Device::StartFrameCapture(DeviceOwnedWindow devWnd)
{
  if(!IsBackgroundCapturing(m_State))
    return;

  m_CaptureFailure = false;

  RDCLOG("Starting capture");

  if(m_Queue == NULL)
  {
    RDCLOG("Creating direct queue as none was found in the application");

    // pretend this is the application's call and just release - everything else will be handled the
    // same (the queue will be kept alive internally)
    ID3D12CommandQueue *q = NULL;
    D3D12_COMMAND_QUEUE_DESC desc = {};
    desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    CreateCommandQueue(&desc, __uuidof(ID3D12CommandQueue), (void **)&q);
    q->Release();
  }

  WrappedID3D12CommandAllocator::PauseResets();

  m_CaptureTimer.Restart();

  m_AppControlledCapture = true;

  m_SubmitCounter = 0;

  FrameDescription frame;
  frame.frameNumber = ~0U;
  frame.captureTime = Timing::GetUnixTimestamp();
  m_CapturedFrames.push_back(frame);

  GetDebugMessages();
  m_DebugMessages.clear();

  GetResourceManager()->ClearReferencedResources();

  // need to do all this atomically so that no other commands
  // will check to see if they need to markdirty or markpendingdirty
  // and go into the frame record.
  {
    SCOPED_WRITELOCK(m_CapTransitionLock);

    initStateCurBatch = 0;
    initStateCurList = NULL;

    if(!DeviceWaitForIdle())
      return;

    // wait until we've synced all queues to check for these
    GetResourceManager()->GetRTManager()->TickASManagement();
    GetResourceManager()->GetRTManager()->FlushDiskCacheThread();

    GetResourceManager()->PrepareInitialContents();

    if(initStateCurList)
    {
      // close the final list
      initStateCurList->Close();
    }

    initStateCurBatch = 0;
    initStateCurList = NULL;

    ExecuteLists(NULL, true);
    if(!FlushLists())
      return;

    RDCDEBUG("Attempting capture");
    m_FrameCaptureRecord->DeleteChunks();

    // fetch and discard debug messages so we don't serialise any messages of our own.
    (void)GetDebugMessages();

    {
      CACHE_THREAD_SERIALISER();

      SCOPED_SERIALISE_CHUNK(SystemChunk::CaptureBegin);

      Serialise_BeginCaptureFrame(ser);

      // need to hold onto this as it must come right after the capture chunk,
      // before any command lists
      m_HeaderChunk = scope.Get();
    }

    m_State = CaptureState::ActiveCapturing;

    // keep all queues alive during the capture, by adding a refcount. Also reference the creation
    // record so that it's pulled in as initialisation chunks.
    for(auto it = m_Queues.begin(); it != m_Queues.end(); ++it)
    {
      (*it)->AddRef();
      GetResourceManager()->MarkResourceFrameReferenced((*it)->GetCreationRecord()->GetResourceID(),
                                                        eFrameRef_Read);
    }

    if(m_BindlessResourceUseActive)
    {
      SCOPED_LOCK(m_ResourceStatesLock);

      for(auto it = m_BindlessFrameRefs.begin(); it != m_BindlessFrameRefs.end(); ++it)
        GetResourceManager()->MarkResourceFrameReferenced(it->first, it->second);
    }

    m_RefQueues = m_Queues;
    m_RefBuffers = WrappedID3D12Resource::AddRefBuffersBeforeCapture(GetResourceManager());
  }

  GetResourceManager()->MarkResourceFrameReferenced(m_ResourceID, eFrameRef_Read);

  rdcarray<D3D12ResourceRecord *> forced = GetForcedReferences();

  for(auto it = forced.begin(); it != forced.end(); ++it)
    GetResourceManager()->MarkResourceFrameReferenced((*it)->GetResourceID(), eFrameRef_Read);
}

bool WrappedID3D12Device::EndFrameCapture(DeviceOwnedWindow devWnd)
{
  if(!IsActiveCapturing(m_State))
    return true;

  if(m_CaptureFailure)
  {
    m_LastCaptureFailed = Timing::GetUnixTimestamp();
    return DiscardFrameCapture(devWnd);
  }

  m_CaptureFailure = false;

  IDXGISwapper *swapper = NULL;
  SwapPresentInfo swapInfo = {};

  if(devWnd.windowHandle)
  {
    for(auto it = m_SwapChains.begin(); it != m_SwapChains.end(); ++it)
    {
      if(it->first->GetHWND() == devWnd.windowHandle)
      {
        swapper = it->first;
        swapInfo = it->second;
        break;
      }
    }

    if(swapper == NULL)
    {
      RDCERR("Output window %p provided for frame capture corresponds with no known swap chain",
             devWnd.windowHandle);
      return false;
    }
  }

  RDCLOG("Finished capture, Frame %u", m_CapturedFrames.back().frameNumber);

  ID3D12Resource *backbuffer = NULL;

  if(swapper == NULL)
  {
    swapper = m_LastSwap;
    swapInfo = m_SwapChains[swapper];
  }

  if(swapper != NULL)
    backbuffer = (ID3D12Resource *)swapper->GetBackbuffers()[swapper->GetLastPresentedBuffer()];

  rdcarray<WrappedID3D12CommandQueue *> queues;

  rdcarray<ID3D12Resource *> refBuffers;
  rdcarray<WrappedID3D12CommandQueue *> refQueues;

  // transition back to IDLE and readback initial states atomically
  {
    SCOPED_WRITELOCK(m_CapTransitionLock);
    EndCaptureFrame();

    queues = m_Queues;

    refBuffers.swap(m_RefBuffers);
    refQueues.swap(m_RefQueues);

    bool ContainsExecuteIndirect = false;

    for(auto it = queues.begin(); it != queues.end(); ++it)
      ContainsExecuteIndirect |= (*it)->GetResourceRecord()->ContainsExecuteIndirect;

    // There is no easy way to mark resource referenced in the AS input and the dispatch tables.
    // We could be more selective and only force-reference acceleration structure buffers -
    // resources in the AS state - but this would not include scratch buffers (which could be done
    // by hand) and build input buffers (which can't). we don't do this with the forced reference
    // system as we want this to be retroactive - only after seeing an AS build do we mark all
    // buffers referenced but buffers could be created before an AS is built.
    if(ContainsExecuteIndirect || m_HaveSeenASBuild)
      WrappedID3D12Resource::RefBuffers(GetResourceManager());

    m_State = CaptureState::BackgroundCapturing;

    if(!DeviceWaitForIdle())
      return false;
  }

  rdcarray<MapState> maps = GetMaps();
  for(auto it = maps.begin(); it != maps.end(); ++it)
    GetWrapped(it->res)->FreeShadow();

  const uint32_t maxSize = 2048;
  RenderDoc::FramePixels fp;

  // gather backbuffer screenshot
  if(backbuffer != NULL)
  {
    D3D12_HEAP_PROPERTIES heapProps;
    heapProps.Type = D3D12_HEAP_TYPE_READBACK;
    heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    heapProps.CreationNodeMask = 1;
    heapProps.VisibleNodeMask = 1;

    D3D12_RESOURCE_DESC bufDesc;
    bufDesc.Alignment = 0;
    bufDesc.DepthOrArraySize = 1;
    bufDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bufDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
    bufDesc.Format = DXGI_FORMAT_UNKNOWN;
    bufDesc.Height = 1;
    bufDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    bufDesc.MipLevels = 1;
    bufDesc.SampleDesc.Count = 1;
    bufDesc.SampleDesc.Quality = 0;
    bufDesc.Width = 1;

    D3D12_RESOURCE_DESC desc = backbuffer->GetDesc();

    D3D12_PLACED_SUBRESOURCE_FOOTPRINT layout = {};

    m_pDevice->GetCopyableFootprints(&desc, 0, 1, 0, &layout, NULL, NULL, &bufDesc.Width);

    ID3D12Resource *copyDst = NULL;
    HRESULT hr = m_pDevice->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &bufDesc,
                                                    D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                                                    __uuidof(ID3D12Resource), (void **)&copyDst);

    if(SUCCEEDED(hr))
    {
      ID3D12GraphicsCommandList *list = Unwrap(GetNewList());

      D3D12_RESOURCE_BARRIER barrier = {};

      // we know there's only one subresource, and it will be in PRESENT state
      barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
      barrier.Flags = D3D12_RESOURCE_BARRIER_FLAG_NONE;
      barrier.Transition.pResource = Unwrap(backbuffer);
      barrier.Transition.Subresource = 0;
      barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
      barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;

      list->ResourceBarrier(1, &barrier);

      // copy to readback buffer
      D3D12_TEXTURE_COPY_LOCATION dst, src;

      src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
      src.pResource = Unwrap(backbuffer);
      src.SubresourceIndex = 0;

      dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
      dst.pResource = copyDst;
      dst.PlacedFootprint = layout;

      list->CopyTextureRegion(&dst, 0, 0, 0, &src, NULL);

      // transition back
      std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
      list->ResourceBarrier(1, &barrier);

      list->Close();

      ExecuteLists(NULL, true);
      if(!FlushLists())
        return false;

      byte *data = NULL;
      hr = copyDst->Map(0, NULL, (void **)&data);

      if(SUCCEEDED(hr) && data)
      {
        fp.len = (uint32_t)bufDesc.Width;
        fp.data = new uint8_t[fp.len];
        memcpy(fp.data, data, fp.len);

        ResourceFormat fmt = MakeResourceFormat(desc.Format);
        fp.width = (uint32_t)desc.Width;
        fp.height = (uint32_t)desc.Height;
        fp.pitch = layout.Footprint.RowPitch;
        fp.stride = fmt.compByteWidth * fmt.compCount;
        fp.bpc = fmt.compByteWidth;
        fp.bgra = fmt.BGRAOrder();
        fp.max_width = maxSize;
        fp.pitch_requirement = 8;
        switch(fmt.type)
        {
          case ResourceFormatType::R10G10B10A2:
            fp.stride = 4;
            fp.buf1010102 = true;
            break;
          case ResourceFormatType::R5G6B5:
            fp.stride = 2;
            fp.buf565 = true;
            break;
          case ResourceFormatType::R5G5B5A1:
            fp.stride = 2;
            fp.buf5551 = true;
            break;
          default: break;
        }
        copyDst->Unmap(0, NULL);
      }
      else
      {
        RDCERR("Couldn't map readback buffer: HRESULT: %s", ToStr(hr).c_str());
      }

      SAFE_RELEASE(copyDst);
    }
    else
    {
      RDCERR("Couldn't create readback buffer: HRESULT: %s", ToStr(hr).c_str());
    }
  }

  RDCFile *rdc =
      RenderDoc::Inst().CreateRDC(RDCDriver::D3D12, m_CapturedFrames.back().frameNumber, fp);

  StreamWriter *captureWriter = NULL;

  HMODULE D3D12Core = GetModuleHandleA("D3D12Core.dll");
  if(D3D12Core)
  {
    m_InitParams.SDKVersion = *(DWORD *)GetProcAddress(D3D12Core, "D3D12SDKVersion");
  }

  if(rdc)
  {
    SectionProperties props;

    // Compress with LZ4 so that it's fast
    props.flags = SectionFlags::LZ4Compressed;
    props.version = m_SectionVersion;
    props.type = SectionType::FrameCapture;

    captureWriter = rdc->WriteSection(props);
  }
  else
  {
    captureWriter = new StreamWriter(StreamWriter::InvalidStream);
  }

  uint64_t captureSectionSize = 0;

  {
    WriteSerialiser ser(captureWriter, Ownership::Stream);

    ser.SetChunkMetadataRecording(GetThreadSerialiser().GetChunkMetadataRecording());

    ser.SetUserData(GetResourceManager());

    m_InitParams.usedDXIL = m_UsedDXIL;

    if(m_UsedDXIL)
    {
      RDCLOG("Capture used DXIL");
    }

    {
      SCOPED_SERIALISE_CHUNK(SystemChunk::DriverInit, sizeof(D3D12InitParams));

      SERIALISE_ELEMENT(m_InitParams);
    }

    RDCDEBUG("Inserting Resource Serialisers");

    GetResourceManager()->InsertReferencedChunks(ser);

    GetResourceManager()->InsertInitialContentsChunks(ser);

    RDCDEBUG("Creating Capture Scope");

    GetResourceManager()->Serialise_InitialContentsNeeded(ser);

    {
      SCOPED_SERIALISE_CHUNK(SystemChunk::CaptureScope, 16);

      Serialise_CaptureScope(ser);
    }

    m_HeaderChunk->Write(ser);

    // don't need to lock access to m_CmdListRecords as we are no longer
    // in capframe (the transition is thread-protected) so nothing will be
    // pushed to the vector

    std::map<int64_t, Chunk *> recordlist;

    for(auto it = queues.begin(); it != queues.end(); ++it)
    {
      WrappedID3D12CommandQueue *q = *it;

      const rdcarray<D3D12ResourceRecord *> &cmdListRecords = q->GetCmdLists();

      RDCDEBUG("Flushing %u command list records from queue %s", (uint32_t)cmdListRecords.size(),
               ToStr(q->GetResourceID()).c_str());

      for(size_t i = 0; i < cmdListRecords.size(); i++)
      {
        uint32_t prevSize = (uint32_t)recordlist.size();
        cmdListRecords[i]->Insert(recordlist);

        // prevent complaints in release that prevSize is unused
        (void)prevSize;

        RDCDEBUG("Adding %u chunks to file serialiser from command list %s",
                 (uint32_t)recordlist.size() - prevSize,
                 ToStr(cmdListRecords[i]->GetResourceID()).c_str());
      }

      q->GetResourceRecord()->Insert(recordlist);
    }

    m_FrameCaptureRecord->Insert(recordlist);

    RDCDEBUG("Flushing %u chunks to file serialiser from context record",
             (uint32_t)recordlist.size());

    float num = float(recordlist.size());
    float idx = 0.0f;

    for(auto it = recordlist.begin(); it != recordlist.end(); ++it)
    {
      RenderDoc::Inst().SetProgress(CaptureProgress::SerialiseFrameContents, idx / num);
      idx += 1.0f;
      it->second->Write(ser);
    }

    RDCDEBUG("Done");

    captureSectionSize = captureWriter->GetOffset();
  }

  if(m_CaptureFailure)
  {
    m_LastCaptureFailed = Timing::GetUnixTimestamp();
    SAFE_DELETE(rdc);
  }
  else
  {
    RDCLOG("Captured D3D12 frame with %f MB capture section in %f seconds",
           double(captureSectionSize) / (1024.0 * 1024.0), m_CaptureTimer.GetMilliseconds() / 1000.0);
  }

  m_CaptureFailure = false;

  if(D3D12Core)
  {
    if(rdc)
    {
      wchar_t wide_core_filename[MAX_PATH + 1] = {};
      GetModuleFileNameW(D3D12Core, wide_core_filename, MAX_PATH);

      rdcstr core_filename = StringFormat::Wide2UTF8(wide_core_filename);

      bytebuf buf;
      FileIO::ReadAll(core_filename, buf);

      {
        SectionProperties props;

        props.flags = SectionFlags::ZstdCompressed;
        props.version = 1;
        props.type = SectionType::D3D12Core;

        captureWriter = rdc->WriteSection(props);

        captureWriter->Write(buf.data(), buf.size());

        captureWriter->Finish();
        SAFE_DELETE(captureWriter);
      }

      buf.clear();

      // try to grab the SDK layers which will be needed for debug on replay

      rdcstr sdklayers_filename = get_dirname(core_filename) + "/d3d12sdklayers.dll";

      FileIO::ReadAll(sdklayers_filename, buf);

      if(!buf.empty())
      {
        SectionProperties props;

        props.flags = SectionFlags::ZstdCompressed;
        props.version = 1;
        props.type = SectionType::D3D12SDKLayers;

        captureWriter = rdc->WriteSection(props);

        captureWriter->Write(buf.data(), buf.size());

        captureWriter->Finish();
        SAFE_DELETE(captureWriter);
      }
    }
  }

  RenderDoc::Inst().FinishCaptureWriting(rdc, m_CapturedFrames.back().frameNumber);

  m_HeaderChunk->Delete();
  m_HeaderChunk = NULL;

  for(auto it = queues.begin(); it != queues.end(); ++it)
    (*it)->ClearAfterCapture();

  // remove the references held during capture, potentially releasing the queue/buffer.
  for(WrappedID3D12CommandQueue *q : refQueues)
    q->Release();

  for(ID3D12Resource *r : refBuffers)
    r->Release();

  for(ID3D12Heap *h : m_InitialStateHeaps)
    h->Release();
  m_InitialStateHeaps.clear();

  WrappedID3D12CommandAllocator::ResumeResets();

  GetResourceManager()->MarkUnwrittenResources();

  GetResourceManager()->ClearReferencedResources();

  GetResourceManager()->FreeInitialContents();

  return true;
}

bool WrappedID3D12Device::DiscardFrameCapture(DeviceOwnedWindow devWnd)
{
  if(!IsActiveCapturing(m_State))
    return true;

  m_CaptureFailure = false;

  RDCLOG("Discarding frame capture.");

  RenderDoc::Inst().FinishCaptureWriting(NULL, m_CapturedFrames.back().frameNumber);

  m_CapturedFrames.pop_back();

  rdcarray<WrappedID3D12CommandQueue *> queues;

  rdcarray<WrappedID3D12CommandQueue *> refQueues;
  rdcarray<ID3D12Resource *> refBuffers;

  // transition back to IDLE and readback initial states atomically
  {
    SCOPED_WRITELOCK(m_CapTransitionLock);

    m_State = CaptureState::BackgroundCapturing;

    if(!DeviceWaitForIdle())
      return false;

    queues = m_Queues;

    refQueues.swap(m_RefQueues);
    refBuffers.swap(m_RefBuffers);
  }

  rdcarray<MapState> maps = GetMaps();
  for(auto it = maps.begin(); it != maps.end(); ++it)
    GetWrapped(it->res)->FreeShadow();

  m_HeaderChunk->Delete();
  m_HeaderChunk = NULL;

  for(auto it = queues.begin(); it != queues.end(); ++it)
    (*it)->ClearAfterCapture();

  // remove the references held during capture, potentially releasing the queue/buffer.
  for(WrappedID3D12CommandQueue *q : refQueues)
    q->Release();

  for(ID3D12Resource *r : refBuffers)
    r->Release();

  for(ID3D12Heap *h : m_InitialStateHeaps)
    h->Release();
  m_InitialStateHeaps.clear();

  WrappedID3D12CommandAllocator::ResumeResets();

  GetResourceManager()->MarkUnwrittenResources();

  GetResourceManager()->ClearReferencedResources();

  GetResourceManager()->FreeInitialContents();

  return true;
}

uint32_t WrappedID3D12Device::SetObjectAnnotation(void *object, const char *key,
                                                  RENDERDOC_AnnotationType valueType,
                                                  uint32_t valueVectorWidth,
                                                  const RENDERDOC_AnnotationValue *value)
{
  ID3D12Object *d3d12Obj = (ID3D12Object *)object;

  D3D12ResourceType type = TryIdentifyTypeByPtr(d3d12Obj);

  if(type == Resource_Unknown)
    return 2;

  ResourceId id = GetResID(d3d12Obj);

  if(id != ResourceId())
  {
    RENDERDOC_AnnotationValue val = value ? *value : RENDERDOC_AnnotationValue();

    if(valueType == eRENDERDOC_APIObject)
    {
      ResourceId valId = GetResID((ID3D12Object *)val.apiObject);
      RDCCOMPILE_ASSERT(sizeof(val.uint64) == sizeof(valId), "ResourceId isn't 64-bit!");
      memcpy(&val.uint64, &valId, sizeof(valId));
    }

    SDObject *root = NULL;
    {
      SCOPED_LOCK(m_AnnotationsLock);
      root = m_Annotations[id];
      if(!root)
        root = m_Annotations[id] = new SDObject("Object Annotations"_lit, "Object Annotations"_lit);
    }

    if(valueType == eRENDERDOC_Empty)
    {
      root->EraseChildByKeyPath(key);
    }
    else
    {
      WriteAnnotation(root->CreateChildByKeyPath(key), valueType, valueVectorWidth, val);
    }

    return 0;
  }

  return 2;
}

uint32_t WrappedID3D12Device::SetCommandAnnotation(void *queueOrCommandBuffer, const char *key,
                                                   RENDERDOC_AnnotationType valueType,
                                                   uint32_t valueVectorWidth,
                                                   const RENDERDOC_AnnotationValue *value)
{
  ID3D12Object *d3d12Obj = (ID3D12Object *)queueOrCommandBuffer;

  D3D12ResourceType type = TryIdentifyTypeByPtr(d3d12Obj);

  if(type == Resource_CommandQueue)
  {
    WrappedID3D12CommandQueue *queue = (WrappedID3D12CommandQueue *)d3d12Obj;

    if(IsActiveCapturing(m_State))
    {
      CACHE_THREAD_SERIALISER();
      ser.SetActionChunk();
      SCOPED_SERIALISE_CHUNK(D3D12Chunk::SetQueueAnnotation);

      RENDERDOC_AnnotationValue val = value ? *value : RENDERDOC_AnnotationValue();

      if(valueType == eRENDERDOC_APIObject)
      {
        ResourceId id = GetResID((ID3D12Object *)val.apiObject);
        RDCCOMPILE_ASSERT(sizeof(val.uint64) == sizeof(id), "ResourceId isn't 64-bit!");
        memcpy(&val.uint64, &id, sizeof(id));
      }

      queue->Serialise_SetQueueAnnotation(ser, key, valueType, valueVectorWidth, val);

      m_FrameCaptureRecord->AddChunk(scope.Get());
    }

    return 0;
  }
  else if(type == Resource_GraphicsCommandList)
  {
    WrappedID3D12GraphicsCommandList *list = (WrappedID3D12GraphicsCommandList *)d3d12Obj;

    if(IsCaptureMode(m_State))
    {
      CACHE_THREAD_SERIALISER();
      ser.SetActionChunk();
      SCOPED_SERIALISE_CHUNK(D3D12Chunk::SetCommandAnnotation);

      RENDERDOC_AnnotationValue val = value ? *value : RENDERDOC_AnnotationValue();

      if(valueType == eRENDERDOC_APIObject)
      {
        ResourceId id = GetResID((ID3D12Object *)val.apiObject);
        RDCCOMPILE_ASSERT(sizeof(val.uint64) == sizeof(id), "ResourceId isn't 64-bit!");
        memcpy(&val.uint64, &id, sizeof(id));
      }

      list->Serialise_SetCommandAnnotation(ser, key, valueType, valueVectorWidth, val);

      GetRecord(list)->AddChunk(scope.Get());
    }

    return 0;
  }
  else
  {
    return 2;
  }
}

void WrappedID3D12Device::UploadBLASBufferAddresses()
{
  if(m_addressBufferUploaded)
    return;

  rdcarray<BlasAddressPair> blasAddressPair;
  D3D12ResourceManager *resManager = GetResourceManager();

  for(GPUAddressRange addressRange : m_OrigGPUAddresses.GetAddresses())
  {
    ResourceId resId = addressRange.id;
    if(resManager->HasResource(resId))
    {
      WrappedID3D12Resource *wrappedRes = (WrappedID3D12Resource *)resManager->GetResource(resId);
      {
        BlasAddressPair addressPair;
        addressPair.oldAddress.start = addressRange.start;
        addressPair.oldAddress.end = addressRange.realEnd;

        addressPair.newAddress.start = wrappedRes->GetGPUVirtualAddress();
        addressPair.newAddress.end = addressPair.newAddress.start + wrappedRes->GetDesc().Width;

        // ASB addresses are far more likely to be used so put them at the front to be found first
        // as this isn't sorted.
        // The only time we are looking up 'normal' buffers on the GPU to patch is when we're
        // unrolling an ARRAY_OF_POINTERS list on replay when building a TLAS
        if(wrappedRes->IsAccelerationStructureResource())
          blasAddressPair.insert(0, addressPair);
        else
          blasAddressPair.push_back(addressPair);
      }
    }
  }

  m_blasAddressCount = (uint32_t)blasAddressPair.size();

  uint64_t requiredSize = blasAddressPair.size() * sizeof(BlasAddressPair);

  D3D12_RESOURCE_DESC addressBufferResDesc;
  addressBufferResDesc.Alignment = 0;
  addressBufferResDesc.DepthOrArraySize = 1;
  addressBufferResDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
  addressBufferResDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
  addressBufferResDesc.Format = DXGI_FORMAT_UNKNOWN;
  addressBufferResDesc.Height = 1;
  addressBufferResDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
  addressBufferResDesc.MipLevels = 1;
  addressBufferResDesc.SampleDesc.Count = 1;
  addressBufferResDesc.SampleDesc.Quality = 0;
  addressBufferResDesc.Width = RDCMAX(8ULL, requiredSize);

  D3D12_HEAP_PROPERTIES heapProps;
  heapProps.Type = D3D12_HEAP_TYPE_UPLOAD;
  heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
  heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
  heapProps.CreationNodeMask = 1;
  heapProps.VisibleNodeMask = 1;

  HRESULT hr = CreateCommittedResource(
      &heapProps, D3D12_HEAP_FLAG_NONE, &addressBufferResDesc, D3D12_RESOURCE_STATE_GENERIC_READ,
      NULL, __uuidof(ID3D12Resource), (void **)&m_blasAddressBufferUploadResource);

  if(!SUCCEEDED(hr))
  {
    RDCERR("Unable to create upload buffer for BLAS address");
  }
  else
  {
    D3D12_RANGE readRange = {0, 0};
    void *ptr = NULL;
    HRESULT result = m_blasAddressBufferUploadResource->Map(0, &readRange, &ptr);

    if(!SUCCEEDED(result))
    {
      RDCERR("Unable to map the resource for uploading old addresses");
    }
    else
    {
      memcpy((byte *)ptr, blasAddressPair.data(), (size_t)requiredSize);
      m_blasAddressBufferUploadResource->Unmap(0, NULL);
    }
  }

  heapProps.Type = D3D12_HEAP_TYPE_DEFAULT;
  hr = CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &addressBufferResDesc,
                               D3D12_RESOURCE_STATE_COPY_DEST, NULL, __uuidof(ID3D12Resource),
                               (void **)&m_blasAddressBufferResource);

  if(!SUCCEEDED(hr))
  {
    RDCERR("Unable to create upload buffer for BLAS address");
  }
  else
  {
    ID3D12GraphicsCommandList *initList = GetInitialStateList();
    initList->CopyBufferRegion(m_blasAddressBufferResource, 0, m_blasAddressBufferUploadResource, 0,
                               requiredSize);

    D3D12_RESOURCE_BARRIER resBarrier;
    resBarrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    resBarrier.Flags = D3D12_RESOURCE_BARRIER_FLAG_NONE;
    resBarrier.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    resBarrier.Transition.StateAfter = D3D12_RESOURCE_STATE_GENERIC_READ;
    resBarrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    resBarrier.Transition.pResource = m_blasAddressBufferResource;
    initList->ResourceBarrier(1, &resBarrier);
  }

  m_addressBufferUploaded = true;
}

void WrappedID3D12Device::AddForcedReference(D3D12ResourceRecord *record)
{
  {
    SCOPED_LOCK(m_ForcedReferencesLock);
    m_ForcedReferences.insert(record);
  }

  // in case we're currently capturing, immediately consider the resource as referenced. If we're
  // not capturing this will naturally be cleared before the frame capture starts and we don't have
  // to consider races as this is internally locked. If we're racing with a frame capture starting
  // we will either add this redundantly (after clear but before forced references are added) or as
  // required (after references are cleared and after forced references are added)
  GetResourceManager()->MarkResourceFrameReferenced(record->GetResourceID(), eFrameRef_Read);
}

void WrappedID3D12Device::ReleaseResource(ID3D12DeviceChild *res)
{
  ResourceId id = GetResID(res);

  {
    SCOPED_LOCK(m_ResourceStatesLock);
    m_ResourceStates.erase(id);
  }

  {
    SCOPED_LOCK(m_ForcedReferencesLock);
    m_ForcedReferences.erase(GetRecord(res));
  }

  {
    SCOPED_LOCK(m_SparseLock);
    m_SparseResources.erase(id);
    m_SparseHeaps.erase(id);
  }

  {
    SCOPED_LOCK(m_AnnotationsLock);
    m_Annotations.erase(id);
  }

  D3D12ResourceRecord *record = GetRecord(res);

  if(record)
    record->Delete(GetResourceManager());
}

HRESULT WrappedID3D12Device::CreatePipeState(D3D12_EXPANDED_PIPELINE_STATE_STREAM_DESC &desc,
                                             ID3D12PipelineState **state)
{
  if(m_pDevice3)
  {
    D3D12_PACKED_PIPELINE_STATE_STREAM_DESC packedDesc = desc;
    return CreatePipelineState(packedDesc.AsDescStream(), __uuidof(ID3D12PipelineState),
                               (void **)state);
  }

  if(desc.CS.BytecodeLength > 0)
  {
    D3D12_COMPUTE_PIPELINE_STATE_DESC compDesc;
    compDesc.pRootSignature = desc.GetOrCreateRootSig(this);
    compDesc.CS = desc.CS;
    compDesc.NodeMask = desc.NodeMask;
    compDesc.CachedPSO = desc.CachedPSO;
    compDesc.Flags = desc.Flags;
    return CreateComputePipelineState(&compDesc, __uuidof(ID3D12PipelineState), (void **)state);
  }
  else
  {
    D3D12_GRAPHICS_PIPELINE_STATE_DESC graphicsDesc;
    graphicsDesc.pRootSignature = desc.GetOrCreateRootSig(this);
    graphicsDesc.VS = desc.VS;
    graphicsDesc.PS = desc.PS;
    graphicsDesc.DS = desc.DS;
    graphicsDesc.HS = desc.HS;
    graphicsDesc.GS = desc.GS;
    graphicsDesc.StreamOutput = desc.StreamOutput;
    graphicsDesc.BlendState = desc.BlendState;
    graphicsDesc.SampleMask = desc.SampleMask;

    // can't create mesh shaders with old function, so we should not be trying
    RDCASSERT(desc.AS.BytecodeLength == 0);
    RDCASSERT(desc.MS.BytecodeLength == 0);

    // graphicsDesc.RasterizerState = desc.RasterizerState;
    {
      graphicsDesc.RasterizerState.FillMode = desc.RasterizerState.FillMode;
      graphicsDesc.RasterizerState.CullMode = desc.RasterizerState.CullMode;
      graphicsDesc.RasterizerState.FrontCounterClockwise = desc.RasterizerState.FrontCounterClockwise;
      graphicsDesc.RasterizerState.DepthBias = INT(desc.RasterizerState.DepthBias);
      graphicsDesc.RasterizerState.DepthBiasClamp = desc.RasterizerState.DepthBiasClamp;
      graphicsDesc.RasterizerState.SlopeScaledDepthBias = desc.RasterizerState.SlopeScaledDepthBias;
      graphicsDesc.RasterizerState.DepthClipEnable = desc.RasterizerState.DepthClipEnable;
      graphicsDesc.RasterizerState.ForcedSampleCount = desc.RasterizerState.ForcedSampleCount;
      graphicsDesc.RasterizerState.ConservativeRaster = desc.RasterizerState.ConservativeRaster;

      switch(desc.RasterizerState.LineRasterizationMode)
      {
        case D3D12_LINE_RASTERIZATION_MODE_ALIASED:
          graphicsDesc.RasterizerState.MultisampleEnable = FALSE;
          graphicsDesc.RasterizerState.AntialiasedLineEnable = FALSE;
          break;
        case D3D12_LINE_RASTERIZATION_MODE_ALPHA_ANTIALIASED:
          graphicsDesc.RasterizerState.MultisampleEnable = FALSE;
          graphicsDesc.RasterizerState.AntialiasedLineEnable = TRUE;
          break;
        case D3D12_LINE_RASTERIZATION_MODE_QUADRILATERAL_WIDE:
        case D3D12_LINE_RASTERIZATION_MODE_QUADRILATERAL_NARROW:
          graphicsDesc.RasterizerState.MultisampleEnable = TRUE;
          graphicsDesc.RasterizerState.AntialiasedLineEnable = FALSE;
          break;
        default:
          graphicsDesc.RasterizerState.MultisampleEnable = FALSE;
          graphicsDesc.RasterizerState.AntialiasedLineEnable = FALSE;
          break;
      }
    }

    // graphicsDesc.DepthStencilState = desc.DepthStencilState;
    {
      graphicsDesc.DepthStencilState.DepthEnable = desc.DepthStencilState.DepthEnable;
      graphicsDesc.DepthStencilState.DepthWriteMask = desc.DepthStencilState.DepthWriteMask;
      graphicsDesc.DepthStencilState.DepthFunc = desc.DepthStencilState.DepthFunc;
      graphicsDesc.DepthStencilState.StencilEnable = desc.DepthStencilState.StencilEnable;
      graphicsDesc.DepthStencilState.StencilReadMask =
          desc.DepthStencilState.FrontFace.StencilReadMask;
      graphicsDesc.DepthStencilState.StencilWriteMask =
          desc.DepthStencilState.FrontFace.StencilWriteMask;
      graphicsDesc.DepthStencilState.FrontFace.StencilFunc =
          desc.DepthStencilState.FrontFace.StencilFunc;
      graphicsDesc.DepthStencilState.FrontFace.StencilPassOp =
          desc.DepthStencilState.FrontFace.StencilPassOp;
      graphicsDesc.DepthStencilState.FrontFace.StencilFailOp =
          desc.DepthStencilState.FrontFace.StencilFailOp;
      graphicsDesc.DepthStencilState.FrontFace.StencilDepthFailOp =
          desc.DepthStencilState.FrontFace.StencilDepthFailOp;
      graphicsDesc.DepthStencilState.BackFace.StencilFunc =
          desc.DepthStencilState.BackFace.StencilFunc;
      graphicsDesc.DepthStencilState.BackFace.StencilPassOp =
          desc.DepthStencilState.BackFace.StencilPassOp;
      graphicsDesc.DepthStencilState.BackFace.StencilFailOp =
          desc.DepthStencilState.BackFace.StencilFailOp;
      graphicsDesc.DepthStencilState.BackFace.StencilDepthFailOp =
          desc.DepthStencilState.BackFace.StencilDepthFailOp;
      // no DepthBoundsTestEnable
    }
    graphicsDesc.InputLayout = desc.InputLayout;
    graphicsDesc.IBStripCutValue = desc.IBStripCutValue;
    graphicsDesc.PrimitiveTopologyType = desc.PrimitiveTopologyType;
    graphicsDesc.NumRenderTargets = desc.RTVFormats.NumRenderTargets;
    memcpy(graphicsDesc.RTVFormats, desc.RTVFormats.RTFormats, 8 * sizeof(DXGI_FORMAT));
    graphicsDesc.DSVFormat = desc.DSVFormat;
    graphicsDesc.SampleDesc = desc.SampleDesc;
    graphicsDesc.NodeMask = desc.NodeMask;
    graphicsDesc.CachedPSO = desc.CachedPSO;
    graphicsDesc.Flags = desc.Flags;
    return CreateGraphicsPipelineState(&graphicsDesc, __uuidof(ID3D12PipelineState), (void **)state);
  }
}

void WrappedID3D12Device::AddDebugMessage(MessageCategory c, MessageSeverity sv, MessageSource src,
                                          rdcstr d)
{
  D3D12CommandData &cmd = *m_Queue->GetCommandData();

  DebugMessage msg;
  msg.eventId = 0;
  msg.messageID = 0;
  msg.source = src;
  msg.category = c;
  msg.severity = sv;
  msg.description = d;
  if(IsActiveReplaying(m_State))
  {
    // look up the EID this action came from
    D3D12CommandData::ActionUse use(cmd.m_CurChunkOffset, 0);
    auto it = std::lower_bound(cmd.m_ActionUses.begin(), cmd.m_ActionUses.end(), use);
    RDCASSERT(it != cmd.m_ActionUses.end());

    if(it != cmd.m_ActionUses.end())
      msg.eventId = it->eventId;
    else
      RDCERR("Couldn't locate action use for current chunk offset %llu", cmd.m_CurChunkOffset);

    AddDebugMessage(msg);
  }
  else
  {
    cmd.m_EventMessages.push_back(msg);
  }
}

void WrappedID3D12Device::AddDebugMessage(const DebugMessage &msg)
{
  m_DebugMessages.push_back(msg);
}

rdcarray<DebugMessage> WrappedID3D12Device::GetDebugMessages()
{
  rdcarray<DebugMessage> ret;

  if(IsActiveReplaying(m_State))
  {
    // once we're active replaying, m_DebugMessages will contain all the messages from loading
    // (either from the captured serialised messages, or from ourselves during replay).
    ret.swap(m_DebugMessages);

    return ret;
  }

  if(IsCaptureMode(m_State))
  {
    // add any manually-added messages before fetching those from the API
    ret.swap(m_DebugMessages);
  }

  // during loading only try and fetch messages if we're doing that deliberately during replay
  if(IsLoading(m_State) && !m_ReplayOptions.apiValidation)
    return ret;

  // during capture, and during loading if the option is enabled, we fetch messages from the API

  if(!m_pInfoQueue)
    return ret;

  UINT64 numMessages = m_pInfoQueue->GetNumStoredMessagesAllowedByRetrievalFilter();

  for(UINT64 i = 0; i < m_pInfoQueue->GetNumStoredMessagesAllowedByRetrievalFilter(); i++)
  {
    SIZE_T len = 0;
    m_pInfoQueue->GetMessage(i, NULL, &len);

    char *msgbuf = new char[len];
    D3D12_MESSAGE *message = (D3D12_MESSAGE *)msgbuf;

    m_pInfoQueue->GetMessage(i, message, &len);

    DebugMessage msg;
    msg.eventId = 0;
    msg.source = MessageSource::API;
    msg.category = MessageCategory::Miscellaneous;
    msg.severity = MessageSeverity::Medium;

    switch(message->Category)
    {
      case D3D12_MESSAGE_CATEGORY_APPLICATION_DEFINED:
        msg.category = MessageCategory::Application_Defined;
        break;
      case D3D12_MESSAGE_CATEGORY_MISCELLANEOUS:
        msg.category = MessageCategory::Miscellaneous;
        break;
      case D3D12_MESSAGE_CATEGORY_INITIALIZATION:
        msg.category = MessageCategory::Initialization;
        break;
      case D3D12_MESSAGE_CATEGORY_CLEANUP: msg.category = MessageCategory::Cleanup; break;
      case D3D12_MESSAGE_CATEGORY_COMPILATION: msg.category = MessageCategory::Compilation; break;
      case D3D12_MESSAGE_CATEGORY_STATE_CREATION:
        msg.category = MessageCategory::State_Creation;
        break;
      case D3D12_MESSAGE_CATEGORY_STATE_SETTING:
        msg.category = MessageCategory::State_Setting;
        break;
      case D3D12_MESSAGE_CATEGORY_STATE_GETTING:
        msg.category = MessageCategory::State_Getting;
        break;
      case D3D12_MESSAGE_CATEGORY_RESOURCE_MANIPULATION:
        msg.category = MessageCategory::Resource_Manipulation;
        break;
      case D3D12_MESSAGE_CATEGORY_EXECUTION: msg.category = MessageCategory::Execution; break;
      case D3D12_MESSAGE_CATEGORY_SHADER: msg.category = MessageCategory::Shaders; break;
      default: RDCWARN("Unexpected message category: %d", message->Category); break;
    }

    switch(message->Severity)
    {
      case D3D12_MESSAGE_SEVERITY_CORRUPTION: msg.severity = MessageSeverity::High; break;
      case D3D12_MESSAGE_SEVERITY_ERROR: msg.severity = MessageSeverity::High; break;
      case D3D12_MESSAGE_SEVERITY_WARNING: msg.severity = MessageSeverity::Medium; break;
      case D3D12_MESSAGE_SEVERITY_INFO: msg.severity = MessageSeverity::Low; break;
      case D3D12_MESSAGE_SEVERITY_MESSAGE: msg.severity = MessageSeverity::Info; break;
      default: RDCWARN("Unexpected message severity: %d", message->Severity); break;
    }

    msg.messageID = (uint32_t)message->ID;
    msg.description = rdcstr(message->pDescription);

    // during capture add all messages. Otherwise only add this message if it's different to the
    // last one - we can sometimes get duplicated messages
    if(!IsLoading(m_State) || ret.empty() || !(ret.back() == msg))
      ret.push_back(msg);

    SAFE_DELETE_ARRAY(msgbuf);
  }

  // Docs are fuzzy on the thread safety of the info queue, but I'm going to assume it should only
  // ever be accessed on one thread since it's tied to the device & immediate context.
  // There doesn't seem to be a way to lock it for access and without that there's no way to know
  // that a new message won't be added between the time you retrieve the last one and clearing the
  // queue. There is also no way to pop a message that I can see, which would presumably be the
  // best way if its member functions are thread safe themselves (if the queue is protected
  // internally).
  RDCASSERT(numMessages == m_pInfoQueue->GetNumStoredMessagesAllowedByRetrievalFilter());

  m_pInfoQueue->ClearStoredMessages();

  return ret;
}

void WrappedID3D12Device::DumpDREDPageFault(const D3D12_DRED_PAGE_FAULT_OUTPUT &DredPageFaultOutput)
{
  if(DredPageFaultOutput.PageFaultVA == 0)
  {
    RDCLOG("No DRED page fault information");
    return;
  }

  ResourceId lower, upper;
  D3D12_GPU_VIRTUAL_ADDRESS lowerVA = 0, upperVA = 0;
  WrappedID3D12Resource::GetResIDBoundForAddr(DredPageFaultOutput.PageFaultVA, lower, lowerVA,
                                              upper, upperVA);

  RDCLOG("DRED Page fault at VA %llx, between %s at %llx and %s at %llx",
         DredPageFaultOutput.PageFaultVA, ToStr(lower).c_str(), lowerVA, ToStr(upper).c_str(),
         upperVA);

  const D3D12_DRED_ALLOCATION_NODE *existing = DredPageFaultOutput.pHeadExistingAllocationNode;
  const D3D12_DRED_ALLOCATION_NODE *freed = DredPageFaultOutput.pHeadRecentFreedAllocationNode;

  while(existing)
  {
    RDCLOG("Existing allocation %s (%s / %ls)", ToStr(existing->AllocationType).c_str(),
           existing->ObjectNameA, existing->ObjectNameW);
    existing = existing->pNext;
  }

  while(freed)
  {
    RDCLOG("Free'd allocation %s (%s / %ls)", ToStr(freed->AllocationType).c_str(),
           freed->ObjectNameA, freed->ObjectNameW);
    freed = freed->pNext;
  }
}

void WrappedID3D12Device::DumpDRED(D3D12_AUTO_BREADCRUMB_NODE *node,
                                   D3D12_DRED_BREADCRUMB_CONTEXT *contexts, UINT numContexts)
{
  rdcstr cmdName = "";
  rdcstr qName = "";

  if(node->pCommandListDebugNameA)
    cmdName = node->pCommandListDebugNameA;
  else if(node->pCommandListDebugNameW)
    cmdName = StringFormat::Wide2UTF8(node->pCommandListDebugNameW);

  if(cmdName.empty() && node->pCommandList)
  {
    ID3D12CommandList *cmd =
        (ID3D12CommandList *)GetResourceManager()->GetWrapper(node->pCommandList);
    if(cmd)
      cmdName = ToStr(GetResID(cmd));
  }

  if(node->pCommandQueueDebugNameA)
    qName = node->pCommandListDebugNameA;
  else if(node->pCommandQueueDebugNameW)
    qName = StringFormat::Wide2UTF8(node->pCommandQueueDebugNameW);

  if(qName.empty() && node->pCommandQueue)
  {
    ID3D12CommandQueue *q =
        (ID3D12CommandQueue *)GetResourceManager()->GetWrapper(node->pCommandList);
    if(q)
      qName = ToStr(GetResID(q));
  }

  uint32_t lastExecuted = *node->pLastBreadcrumbValue;
  D3D12_DRED_BREADCRUMB_CONTEXT *ctx = contexts;

  RDCLOG("DRED Node on queue '%s' executing cmd '%s'", qName.c_str(), cmdName.c_str());

  for(uint32_t i = 0; i < node->BreadcrumbCount; i++)
  {
    if(ctx && numContexts > 0 && ctx->BreadcrumbIndex == i)
    {
      RDCLOG("  [%u]: %s -- %ls", i, ToStr(node->pCommandHistory[i]).c_str(), ctx->pContextString);
      // sometimes get duplicates?
      while(ctx->BreadcrumbIndex == i)
      {
        ctx++;
        numContexts--;
      }
    }
    else
    {
      RDCLOG("  [%u]: %s", i, ToStr(node->pCommandHistory[i]).c_str());
    }

    if(lastExecuted == i)
      RDCLOG("------ Last executed ------");
  }
}

void WrappedID3D12Device::CheckHRESULT(const char *file, int line, HRESULT hr)
{
  if(SUCCEEDED(hr) || HasFatalError())
    return;

  if(hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET ||
     hr == DXGI_ERROR_DEVICE_HUNG || hr == DXGI_ERROR_DRIVER_INTERNAL_ERROR)
  {
    SET_ERROR_RESULT(m_FatalError, ResultCode::DeviceLost,
                     "Logging device lost fatal error at %s:%d: %s", file, line, ToStr(hr).c_str());

    ID3D12DeviceRemovedExtendedData *dred = NULL;
    m_pDevice->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedData), (void **)&dred);

    D3D12_DRED_AUTO_BREADCRUMBS_OUTPUT DredAutoBreadcrumbsOutput = {};
    D3D12_DRED_AUTO_BREADCRUMBS_OUTPUT1 DredAutoBreadcrumbsOutput1 = {};
    D3D12_DRED_PAGE_FAULT_OUTPUT DredPageFaultOutput = {};
    if(dred)
    {
      dred->GetAutoBreadcrumbsOutput(&DredAutoBreadcrumbsOutput);
      dred->GetPageFaultAllocationOutput(&DredPageFaultOutput);

      DumpDREDPageFault(DredPageFaultOutput);

      ID3D12DeviceRemovedExtendedData1 *dred1 = NULL;
      dred->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedData1), (void **)&dred1);
      if(dred1)
      {
        dred1->GetAutoBreadcrumbsOutput1(&DredAutoBreadcrumbsOutput1);
        SAFE_RELEASE(dred1);
        DumpDRED(DredAutoBreadcrumbsOutput1.pHeadAutoBreadcrumbNode);
      }
      else
      {
        DumpDRED(DredAutoBreadcrumbsOutput.pHeadAutoBreadcrumbNode);
      }

      SAFE_RELEASE(dred);
    }

    NVAftermath_DumpRTValidation(m_pDevice5);
    NVAftermath_DumpCrash();
  }
  else if(hr == E_OUTOFMEMORY)
  {
    if(m_OOMHandler)
    {
      RDCLOG("Ignoring out of memory error at %s:%d that will be handled");
    }
    else
    {
      RDCLOG("Logging out of memory error at %s:%d: %s", file, line, ToStr(hr).c_str());
      m_FatalError = ResultCode::OutOfMemory;
    }
  }
  else
  {
    RDCLOG("Ignoring return code at %s:%d: %s", file, line, ToStr(hr).c_str());
  }
}

void WrappedID3D12Device::CheckDeferredResult(const RDResult &res)
{
  if(res == ResultCode::Succeeded)
    return;

  SCOPED_LOCK(m_DeferredResultLock);
  m_DeferredResult = res;
}

void WrappedID3D12Device::AddDeferredTime(double ms)
{
  SCOPED_LOCK(m_DeferredResultLock);
  m_DeferredTime += ms;
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_SetShaderDebugPath(SerialiserType &ser,
                                                       ID3D12DeviceChild *pResource, const char *Path)
{
  SERIALISE_ELEMENT(pResource);
  SERIALISE_ELEMENT(Path);

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading() && pResource)
  {
    RDCERR("SetDebugInfoPath doesn't work as-is because it can't specify a shader specific path");
  }

  return true;
}

template bool WrappedID3D12Device::Serialise_SetShaderDebugPath(ReadSerialiser &ser,
                                                                ID3D12DeviceChild *pResource,
                                                                const char *Path);
template bool WrappedID3D12Device::Serialise_SetShaderDebugPath(WriteSerialiser &ser,
                                                                ID3D12DeviceChild *pResource,
                                                                const char *Path);

HRESULT WrappedID3D12Device::SetShaderDebugPath(ID3D12DeviceChild *pResource, const char *Path)
{
  if(IsCaptureMode(m_State))
  {
    D3D12ResourceRecord *record = GetRecord(pResource);

    if(record == NULL)
    {
      RDCERR("Setting shader debug path on object %p of type %d that has no resource record.",
             pResource, IdentifyTypeByPtr(pResource));
      return E_INVALIDARG;
    }

    {
      WriteSerialiser &ser = GetThreadSerialiser();
      SCOPED_SERIALISE_CHUNK(D3D12Chunk::SetShaderDebugPath);
      Serialise_SetShaderDebugPath(ser, pResource, Path);
      record->AddChunk(scope.Get());
    }
  }

  return S_OK;
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_SetName(SerialiserType &ser, ID3D12DeviceChild *pResource,
                                            const char *Name)
{
  SERIALISE_ELEMENT(pResource);
  SERIALISE_ELEMENT(Name);

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading() && pResource)
  {
    ResourceId id = GetResID(pResource);

    ResourceDescription &descr = GetReplay()->GetResourceDesc(id);
    if(Name && Name[0])
    {
      descr.SetCustomName(Name);

      // defer setting names on real resources so we can do this independent of any jobs without
      // needing to push more jobs
      m_CustomNames.push_back({pResource, Name});
    }
    AddResourceCurChunk(descr);
  }

  return true;
}

void WrappedID3D12Device::SetName(ID3D12DeviceChild *pResource, const char *Name)
{
  if(IsCaptureMode(m_State))
  {
    D3D12ResourceRecord *record = GetRecord(pResource);

    if(WrappedID3D12CommandQueue::IsAlloc(pResource))
      record = ((WrappedID3D12CommandQueue *)pResource)->GetCreationRecord();
    if(WrappedID3D12GraphicsCommandList::IsAlloc(pResource))
      record = ((WrappedID3D12GraphicsCommandList *)pResource)->GetCreationRecord();
    if(record == NULL)
      record = m_DeviceRecord;

    {
      WriteSerialiser &ser = GetThreadSerialiser();
      SCOPED_SERIALISE_CHUNK(D3D12Chunk::SetName);

      Serialise_SetName(ser, pResource, Name);

      // don't serialise many SetName chunks to the object record, but we can't afford to drop any.
      if(record != m_DeviceRecord)
      {
        record->LockChunks();
        while(record->HasChunks())
        {
          Chunk *end = record->GetLastChunk();

          if(end->GetChunkType<D3D12Chunk>() == D3D12Chunk::SetName)
          {
            end->Delete();
            record->PopChunk();
            continue;
          }

          break;
        }
        record->UnlockChunks();
      }

      record->AddChunk(scope.Get());
    }
  }
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_CreateAS(SerialiserType &ser, ID3D12Resource *pResource,
                                             UINT64 resourceOffset,
                                             D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE type,
                                             UINT64 byteSize, D3D12AccelerationStructure *as)
{
  SERIALISE_ELEMENT(pResource);
  SERIALISE_ELEMENT(resourceOffset);
  SERIALISE_ELEMENT(type);
  SERIALISE_ELEMENT(byteSize);
  SERIALISE_ELEMENT_LOCAL(asId, as->GetResourceID());

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading() && pResource)
  {
    WrappedID3D12Resource *asbWrappedResource = (WrappedID3D12Resource *)pResource;
    D3D12AccelerationStructure *accStructAtOffset = NULL;
    if(asbWrappedResource->CreateAccStruct(asId, resourceOffset, type, byteSize, &accStructAtOffset))
    {
      if(D3D12_Debug_RT_Auditing())
      {
        RDCLOG("Creating %s AS %s at %s + %llu (%llu bytes): %llx remapped to %llx",
               type == D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL ? "blas" : "tlas",
               ToStr(asId).c_str(), ToStr(GetResID(pResource)).c_str(), resourceOffset, byteSize,
               asbWrappedResource->GetOriginalVA() + resourceOffset,
               accStructAtOffset->GetVirtualAddress());

        RDCASSERTEQUAL(accStructAtOffset->GetVirtualAddress(),
                       asbWrappedResource->GetGPUVirtualAddress() + resourceOffset);

        m_ASDebugTracking.update(accStructAtOffset->GetVirtualAddress(),
                                 accStructAtOffset->GetVirtualAddress() + accStructAtOffset->Size(),
                                 asId, [](ResourceId x, ResourceId y) {
                                   if(x == ResourceId())
                                     return y;
                                   if(y == ResourceId())
                                     return x;
                                   // not necessarily an error if one is stale or overwritten partway
                                   // through the frame, but something to watch out for in tracking
                                   RDCWARN("AS Overlap between %s and %s", ToStr(x).c_str(),
                                           ToStr(y).c_str());
                                   return x;
                                 });
      }

      AddResource(asId, ResourceType::AccelerationStructure, "Acceleration Structure");
      // ignored if there's no heap
      DerivedResource(pResource, asId);
    }
    else
    {
      SET_ERROR_RESULT(m_FailedReplayResult, ResultCode::APIReplayFailed,
                       "Couldn't recreate acceleration structure object");
      return false;
    }
  }

  return true;
}

template bool WrappedID3D12Device::Serialise_CreateAS(
    ReadSerialiser &ser, ID3D12Resource *pResource, UINT64 resourceOffset,
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE type, UINT64 byteSize,
    D3D12AccelerationStructure *as);
template bool WrappedID3D12Device::Serialise_CreateAS(
    WriteSerialiser &ser, ID3D12Resource *pResource, UINT64 resourceOffset,
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE type, UINT64 byteSize,
    D3D12AccelerationStructure *as);

void WrappedID3D12Device::CreateAS(ID3D12Resource *pResource, UINT64 resourceOffset,
                                   D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE type,
                                   UINT64 byteSize, D3D12AccelerationStructure *as)
{
  if(IsCaptureMode(m_State))
  {
    D3D12ResourceRecord *record = as->GetResourceRecord();

    if(D3D12_Debug_RT_Auditing())
    {
      RDCLOG("Creating %s at %s + %llx (%llx)", ToStr(as->GetResourceID()).c_str(),
             ToStr(GetResID(pResource)).c_str(), resourceOffset, as->GetVirtualAddress());
    }

    m_HaveSeenASBuild = true;

    {
      WriteSerialiser &ser = GetThreadSerialiser();
      SCOPED_SERIALISE_CHUNK(D3D12Chunk::CreateAS);
      Serialise_CreateAS(ser, pResource, resourceOffset, type, byteSize, as);
      record->AddChunk(scope.Get());
    }
  }
}

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_SetShaderExtUAV(SerialiserType &ser, GPUVendor vendor,
                                                    uint32_t reg, uint32_t space, bool global)
{
  SERIALISE_ELEMENT(vendor);
  SERIALISE_ELEMENT(reg);
  SERIALISE_ELEMENT(space);

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading())
  {
    m_VendorEXT = vendor;
    m_GlobalEXTUAV = reg;
    if(vendor == GPUVendor::nVidia)
    {
      if(!m_ReplayNVAPI)
      {
        SET_ERROR_RESULT(m_FailedReplayResult, ResultCode::APIUnsupported,
                         "This capture uses nvapi extensions but it failed to initialise.");
        return false;
      }
      m_ReplayNVAPI->SetShaderExtUAV(space, reg, true);
    }
    else if(vendor == GPUVendor::AMD || vendor == GPUVendor::Samsung)
    {
      m_GlobalEXTUAVSpace = space;
      // do nothing, it was configured at device create time. This is purely informational
    }
    else
    {
      SET_ERROR_RESULT(m_FailedReplayResult, ResultCode::APIUnsupported,
                       "This capture uses %s extensions which are not supported.",
                       ToStr(vendor).c_str());
      return false;
    }
  }

  return true;
}

void WrappedID3D12Device::SetShaderExtUAV(GPUVendor vendor, uint32_t reg, uint32_t space, bool global)
{
  // just overwrite, we don't expect to switch back and forth on a given device.
  m_VendorEXT = vendor;
  if(global)
  {
    SCOPED_LOCK(m_EXTUAVLock);
    m_GlobalEXTUAV = reg;
    m_GlobalEXTUAVSpace = space;
    m_InitParams.VendorUAV = reg;
    m_InitParams.VendorUAVSpace = space;
  }
  else
  {
    if(m_ThreadLocalEXTUAVSlot == ~0ULL)
      m_ThreadLocalEXTUAVSlot = Threading::AllocateTLSSlot();

    uint64_t packedVal = 0U;

// on 64-bit we can store both values in the pointer fine.
#if ENABLED(RDOC_X64)
    packedVal |= space;
    packedVal <<= 32U;
    packedVal |= reg;
#else
    // no-one should be writing D3D12 applications in 32-bit, but if they are we assume that the
    // space and register are less than 16-bit. We error if this would truncate.

    // AMD's space is too big, so we steal 0xFFFF for that space
    if(reg > 0xFFFFU || space >= 0xFFFFU)
    {
      RDCERR("Register %u space %u is too large! Capture this as an 64-bit application", reg, space);

      space = RDCMIN(0xFFFEU, space);
      reg = RDCMIN(0xFFFFU, space);
    }

    if(space == 2147420894U)
      space = 0xFFFFU;

    packedVal |= space;
    packedVal <<= 16U;
    packedVal |= reg;
#endif

    Threading::SetTLSValue(m_ThreadLocalEXTUAVSlot, (void *)(uintptr_t)packedVal);
  }
}

INSTANTIATE_FUNCTION_SERIALISED(void, WrappedID3D12Device, SetShaderExtUAV, GPUVendor vendor,
                                uint32_t reg, uint32_t space, bool global);

template <typename SerialiserType>
bool WrappedID3D12Device::Serialise_SetPipelineStackSize(SerialiserType &ser,
                                                         ID3D12StateObject *pStateObject,
                                                         UINT64 StackSize)
{
  SERIALISE_ELEMENT(pStateObject);
  SERIALISE_ELEMENT(StackSize);

  SERIALISE_CHECK_READ_ERRORS();

  if(IsReplayingAndReading() && pStateObject)
  {
    auto setSize = [pStateObject, StackSize]() {
      ID3D12StateObjectProperties *properties = NULL;
      pStateObject->QueryInterface(__uuidof(ID3D12StateObjectProperties), (void **)&properties);

      properties->SetPipelineStackSize(StackSize);

      SAFE_RELEASE(properties);
    };

    if(Replay_Debug_SingleThreadedCompilation())
    {
      setSize();
    }
    else
    {
      Threading::JobSystem::AddJob([setSize]() { setSize(); },
                                   {GetWrapped(pStateObject)->deferredJob});
    }
  }

  return true;
}

void WrappedID3D12Device::SetPipelineStackSize(ID3D12StateObject *pStateObject, UINT64 StackSize)
{
  if(IsCaptureMode(m_State))
  {
    D3D12ResourceRecord *record = GetRecord(pStateObject);

    {
      WriteSerialiser &ser = GetThreadSerialiser();
      SCOPED_SERIALISE_CHUNK(D3D12Chunk::StateObject_SetPipelineStackSize);
      Serialise_SetPipelineStackSize(ser, pStateObject, StackSize);
      record->AddChunk(scope.Get());
    }
  }
}

INSTANTIATE_FUNCTION_SERIALISED(void, WrappedID3D12Device, SetPipelineStackSize,
                                ID3D12StateObject *pStateObject, UINT64 StackSize);

void WrappedID3D12Device::SetShaderExt(GPUVendor vendor)
{
  // just overwrite, we don't expect to switch back and forth on a given device.
  m_VendorEXT = vendor;
}

void WrappedID3D12Device::GetShaderExtUAV(uint32_t &reg, uint32_t &space)
{
  if(m_ThreadLocalEXTUAVSlot != ~0ULL)
  {
    // see just above in SetShaderExtUAV for the packing
    uint64_t threadVal = (uint64_t)(uintptr_t)Threading::GetTLSValue(m_ThreadLocalEXTUAVSlot);

#if ENABLED(RDOC_X64)
    uint32_t threadReg = (threadVal & 0xFFFFFFFFU);
    uint32_t threadSpace = ((threadVal >> 32U) & 0xFFFFFFFFU);
#else
    uint32_t threadReg = (threadVal & 0xFFFFU);
    uint32_t threadSpace = ((threadVal >> 16U) & 0xFFFFU);

    if(threadSpace == 0xFFFFU)
      threadSpace = 2147420894U;
#endif

    if(threadReg != ~0U)
    {
      reg = threadReg;
      space = threadSpace;
      return;
    }
  }

  SCOPED_LOCK(m_EXTUAVLock);
  reg = m_GlobalEXTUAV;
  space = m_GlobalEXTUAVSpace;
}

HRESULT STDMETHODCALLTYPE WrappedID3D12Device::QueryVideoMemoryInfo(
    UINT NodeIndex, DXGI_MEMORY_SEGMENT_GROUP MemorySegmentGroup,
    _Out_ DXGI_QUERY_VIDEO_MEMORY_INFO *pVideoMemoryInfo)
{
  return m_pDownlevel->QueryVideoMemoryInfo(NodeIndex, MemorySegmentGroup, pVideoMemoryInfo);
}

FrameRefType WrappedID3D12Device::BindlessRefTypeForRes(ID3D12Resource *wrapped)
{
  // if the resource could be used in any mutable way, assume it's written. Otherwise assume it's
  // read-only (if it's modified some other way like with a copy etc, this will naturally become a
  // read-before-write)
  return (wrapped->GetDesc().Flags &
          (D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET | D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL |
           D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS)) != 0
             ? eFrameRef_ReadBeforeWrite
             : eFrameRef_Read;
}

byte *WrappedID3D12Device::GetTempMemory(size_t s)
{
  TempMem *mem = (TempMem *)Threading::GetTLSValue(tempMemoryTLSSlot);
  if(mem && mem->size >= s)
    return mem->memory;

  // alloc or grow alloc
  TempMem *newmem = mem;

  if(!newmem)
    newmem = new TempMem();

  // free old memory, don't need to keep contents
  if(newmem->memory)
    delete[] newmem->memory;

  // alloc new memory
  newmem->size = s;
  newmem->memory = new byte[s];

  Threading::SetTLSValue(tempMemoryTLSSlot, (void *)newmem);

  // if this is entirely new, save it for deletion on shutdown
  if(!mem)
  {
    SCOPED_LOCK(m_ThreadTempMemLock);
    m_ThreadTempMem.push_back(newmem);
  }

  return newmem->memory;
}

WriteSerialiser &WrappedID3D12Device::GetThreadSerialiser()
{
  WriteSerialiser *ser = (WriteSerialiser *)Threading::GetTLSValue(threadSerialiserTLSSlot);
  if(ser)
    return *ser;

  // slow path, but rare

  ser = new WriteSerialiser(new StreamWriter(1024), Ownership::Stream);

  uint32_t flags = WriteSerialiser::ChunkDuration | WriteSerialiser::ChunkTimestamp |
                   WriteSerialiser::ChunkThreadID;

  if(RenderDoc::Inst().GetCaptureOptions().captureCallstacks)
    flags |= WriteSerialiser::ChunkCallstack;

  ser->SetChunkMetadataRecording(flags);
  ser->SetUserData(GetResourceManager());
  ser->SetVersion(D3D12InitParams::CurrentVersion);

  Threading::SetTLSValue(threadSerialiserTLSSlot, (void *)ser);

  {
    SCOPED_LOCK(m_ThreadSerialisersLock);
    m_ThreadSerialisers.push_back(ser);
  }

  return *ser;
}

D3D12_CPU_DESCRIPTOR_HANDLE WrappedID3D12Device::AllocRTV()
{
  if(m_FreeRTVs.empty())
  {
    RDCERR("Not enough free RTVs for swapchain overlays!");
    return D3D12_CPU_DESCRIPTOR_HANDLE();
  }

  D3D12_CPU_DESCRIPTOR_HANDLE ret = m_FreeRTVs.back();
  m_FreeRTVs.pop_back();

  m_UsedRTVs.push_back(ret);

  return ret;
}

void WrappedID3D12Device::FreeRTV(D3D12_CPU_DESCRIPTOR_HANDLE handle)
{
  if(handle.ptr == 0)
    return;

  int32_t idx = m_UsedRTVs.indexOf(handle);

  if(idx >= 0)
  {
    m_UsedRTVs.erase(idx);
    m_FreeRTVs.push_back(handle);
  }
  else
  {
    RDCERR("Unknown RTV %zu being freed", handle.ptr);
  }
}

void QueueReadbackData::Resize(uint64_t size)
{
  size = AlignUp(size, 4096ULL);

  if(readbackSize >= size && size != 0)
    return;

  if(unwrappedReadbackBuf)
  {
    unwrappedReadbackBuf->Unmap(0, NULL);
    SAFE_RELEASE(unwrappedReadbackBuf);
    readbackMapped = NULL;
  }

  readbackSize = size;

  if(size == 0)
    return;

  RDCLOG("Resizing GPU readback window to %llu", size);

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
  readbackDesc.Width = size;

  D3D12_HEAP_PROPERTIES heapProps;
  heapProps.Type = D3D12_HEAP_TYPE_READBACK;
  heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
  heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
  heapProps.CreationNodeMask = 1;
  heapProps.VisibleNodeMask = 1;

  // create this unwrapped to avoid intercepting the map during capture or having locking issues
  // when creating this resource
  device->GetReal()->CreateCommittedResource(
      &heapProps, D3D12_HEAP_FLAG_NONE, &readbackDesc, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
      __uuidof(ID3D12Resource), (void **)&unwrappedReadbackBuf);
  unwrappedReadbackBuf->Map(0, NULL, (void **)&readbackMapped);
}

void WrappedID3D12Device::CreateInternalResources()
{
  if(IsReplayMode(m_State) && m_DriverInfo.vendor == GPUVendor::AMD)
  {
    // Initialise AMD extension, if possible
    HMODULE mod = GetModuleHandleA("amdxc64.dll");

    m_pAMDExtObject = NULL;

    if(mod)
    {
      PFNAmdExtD3DCreateInterface pAmdExtD3dCreateFunc =
          (PFNAmdExtD3DCreateInterface)GetProcAddress(mod, "AmdExtD3DCreateInterface");

      if(pAmdExtD3dCreateFunc != NULL)
      {
        // Initialize extension object
        pAmdExtD3dCreateFunc(m_pDevice, __uuidof(IAmdExtD3DFactory), (void **)&m_pAMDExtObject);
      }
    }
  }

  // we don't want replay-only shaders added in WrappedID3D12Shader to pollute the list of resources
  WrappedID3D12Shader::InternalResources(true);

  {
    m_QueueReadbackData.device = this;
    m_QueueReadbackData.Resize(4 * 1024 * 1024);
    InternalRef();

    {
      D3D12_COMMAND_QUEUE_DESC desc = {};
      desc.Type = D3D12_COMMAND_LIST_TYPE_COPY;
      // make this queue as unwrapped so that it doesn't get included in captures
      GetReal()->CreateCommandQueue(&desc, __uuidof(ID3D12CommandQueue),
                                    (void **)&m_QueueReadbackData.unwrappedQueue);
      InternalRef();
      m_QueueReadbackData.unwrappedQueue->SetName(L"m_QueueReadbackData.queue");
      CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_COPY, __uuidof(ID3D12CommandAllocator),
                             (void **)&m_QueueReadbackData.alloc);
      m_QueueReadbackData.alloc->SetName(L"m_QueueReadbackData.alloc");
      InternalRef();
      CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_COPY, m_QueueReadbackData.alloc, NULL,
                        __uuidof(ID3D12GraphicsCommandList), (void **)&m_QueueReadbackData.list);
      InternalRef();
      m_QueueReadbackData.list->Close();
      CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence),
                  (void **)&m_QueueReadbackData.fence);
      m_QueueReadbackData.fence->SetName(L"m_QueueReadbackData.fence");
      InternalRef();
    }
  }

  CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                         (void **)&m_Alloc);
  InternalRef();
  CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void **)&m_WFIFence);
  m_WFIFence->SetName(L"m_WFIFence");
  InternalRef();
  m_WFIHandle = ::CreateEvent(NULL, FALSE, FALSE, NULL);

  CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), (void **)&m_OverlayFence);
  m_OverlayFence->SetName(L"m_OverlayFence");
  InternalRef();
  m_OverlaySyncHandle = ::CreateEvent(NULL, FALSE, FALSE, NULL);

  for(uint64_t i = 0; i < MaxOverlayInFlight; i++)
  {
    CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                           (void **)&m_OverlayAllocs[i]);
    GetResourceManager()->SetInternalResource(m_OverlayAllocs[i]);

    CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, m_OverlayAllocs[i], NULL,
                      __uuidof(ID3D12GraphicsCommandList), (void **)&m_OverlayLists[i]);
    GetResourceManager()->SetInternalResource(m_OverlayLists[i]);
    InternalRef();

    m_OverlayLists[i]->Close();
  }

  GetResourceManager()->SetInternalResource(m_Alloc);
  GetResourceManager()->SetInternalResource(m_WFIFence);

  CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                         (void **)&m_DataUploadAlloc);
  InternalRef();

  GetResourceManager()->SetInternalResource(m_DataUploadAlloc);

  m_DataUploadLists.reserve(dataUploadInitialPoolSize);
  for(size_t i = 0; i < dataUploadInitialPoolSize; i++)
  {
    if(CreateDataUploadList() == NULL)
      return;
  }
  m_CurDataUpload = 0;
  m_DataUploadPending = false;

  D3D12_DESCRIPTOR_HEAP_DESC desc;
  desc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_NONE;
  desc.NodeMask = 1;
  desc.NumDescriptors = 1024;
  desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;

  CreateDescriptorHeap(&desc, __uuidof(ID3D12DescriptorHeap), (void **)&m_RTVHeap);
  InternalRef();

  GetResourceManager()->SetInternalResource(m_RTVHeap);

  D3D12_CPU_DESCRIPTOR_HANDLE handle;

  if(m_RTVHeap)
  {
    handle = m_RTVHeap->GetCPUDescriptorHandleForHeapStart();

    UINT step = GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);

    for(size_t i = 0; i < 1024; i++)
    {
      m_FreeRTVs.push_back(handle);

      handle.ptr += step;
    }
  }
  else
  {
    RDCERR("Failed to create RTV heap");
  }

  m_WFICounter = 0;

  if(m_TextRenderer == NULL)
    m_TextRenderer = new D3D12TextRenderer(this);

  m_Replay->CreateResources();

  WrappedID3D12Shader::InternalResources(false);
  GetResourceManager()->GetRTManager()->InitInternalResources();
}

void WrappedID3D12Device::DestroyInternalResources()
{
  if(m_WFIHandle == NULL)
    return;

  SAFE_RELEASE(m_pAMDExtObject);

  ExecuteLists();
  if(!FlushLists(true))
    return;

  SAFE_RELEASE(m_RTVHeap);

  SAFE_DELETE(m_TextRenderer);
  SAFE_DELETE(m_ShaderCache);

  for(ID3D12GraphicsCommandList *list : m_DataUploadLists)
    SAFE_RELEASE(list);
  m_DataUploadLists.clear();
  SAFE_RELEASE(m_DataUploadAlloc);

  SAFE_RELEASE(m_QueueReadbackData.unwrappedQueue);
  SAFE_RELEASE(m_QueueReadbackData.alloc);
  SAFE_RELEASE(m_QueueReadbackData.list);
  SAFE_RELEASE(m_QueueReadbackData.fence);
  m_QueueReadbackData.Resize(0);

  for(uint64_t i = 0; i < MaxOverlayInFlight; i++)
  {
    SAFE_RELEASE(m_OverlayLists[i]);
    SAFE_RELEASE(m_OverlayAllocs[i]);
  }

  SAFE_RELEASE(m_Alloc);
  SAFE_RELEASE(m_WFIFence);
  SAFE_RELEASE(m_OverlayFence);
  CloseHandle(m_WFIHandle);
  CloseHandle(m_OverlaySyncHandle);
}

rdcarray<SDObject *> WrappedID3D12Device::DetachAnnotations()
{
  rdcarray<SDObject *> ret;
  if(m_Queue)
    ret.swap(m_Queue->GetCommandData()->m_EventAnnotations);
  for(auto it = m_Annotations.begin(); it != m_Annotations.end(); ++it)
    ret.push_back(it->second);
  m_Annotations.clear();
  return ret;
}

bool WrappedID3D12Device::DataUploadSync(ID3D12CommandQueue *targetQueue)
{
  if(HasFatalError())
    return false;
  ScopedReplayTimingContext timingContext(ReplayTimingContext::DataUploadSync);
  if(m_DataUploadPending)
  {
    // Upload lists execute on RenderDoc's internal queue. The captured queue only needs a GPU-side
    // dependency before it consumes that data; blocking this CPU thread adds pure scrub latency.
    if(targetQueue == NULL || !ReplayQueueSync(GetQueue()->GetResourceID(), targetQueue))
    {
      if(!InternalQueueWaitForIdle())
        return false;
    }
    m_DataUploadPending = false;
    // Do not reuse a submitted command-list slot merely because the consumer has a GPU wait. The
    // pool grows for the rest of this replay epoch, and the next full-replay boundary resets the
    // cursor only after its existing device drain.
  }
  return true;
}

bool WrappedID3D12Device::InternalQueueWaitForIdle()
{
  return QueueWaitForIdle(GetQueue(), m_WFIFence);
}

static bool D3D12IdleWaitSucceeded(DWORD waitResult, HRESULT deviceStatus, uint64_t completed,
                                  uint64_t required)
{
  // The event may belong to an earlier timed-out wait. Only the requested fence value proves
  // that this submission is finished. UINT64_MAX is the device-removal sentinel, not success.
  return waitResult == WAIT_OBJECT_0 && SUCCEEDED(deviceStatus) && required != 0 &&
         completed != ~0ULL && completed >= required;
}

static bool D3D12GPUCompletionFailure(WrappedID3D12Device *device, const rdcstr &detail)
{
  // CHECK_HR may already have reported actual device removal. A timeout or a stale event is
  // instead an internal replay failure: it provides no licence to overwrite or retire GPU data.
  if(!device->HasFatalError())
  {
    RDResult result;
    SET_ERROR_RESULT(result, ResultCode::InternalError, "D3D12 GPU completion unproven: %s",
                     detail.c_str());
    device->ReportFatalError(result);
  }
  return false;
}

bool WrappedID3D12Device::QueueWaitForIdle(ID3D12CommandQueue *queue, ID3D12Fence *fence)
{
  if(HasFatalError())
    return false;
  if(queue == NULL || fence == NULL || m_WFIHandle == NULL || m_WFICounter >= ~0ULL - 1)
    return D3D12GPUCompletionFailure(this, "idle wait has no valid queue, fence, event or fence value");

  const uint64_t required = ++m_WFICounter;
  PerformanceTimer auditTimer;
  const bool auditWait = Process::GetEnvVariable("RENDERDOC_D3D12_WAIT_AUDIT") == "1";
  const uint64_t completedBefore = auditWait ? fence->GetCompletedValue() : 0;

  HRESULT hr = queue->Signal(fence, required);
  CHECK_HR(this, hr);
  if(FAILED(hr))
    return D3D12GPUCompletionFailure(this, "queue idle signal failed: " + ToStr(hr));

  hr = fence->SetEventOnCompletion(required, m_WFIHandle);
  CHECK_HR(this, hr);
  if(FAILED(hr))
    return D3D12GPUCompletionFailure(this, "idle event registration failed: " + ToStr(hr));

  // wait 10s for hardware GPUs, 100s for CPU
  DWORD waitResult = WAIT_FAILED;
  {
    ScopedReplayTimingSection timing(ReplayTimingSection::ExplicitWait);
    if(m_Replay && m_Replay->GetDriverInfo().vendor == GPUVendor::Software)
      waitResult = WaitForSingleObject(m_WFIHandle, 100000);
    else
      waitResult = WaitForSingleObject(m_WFIHandle, 10000);
  }

  hr = m_pDevice->GetDeviceRemovedReason();
  CHECK_HR(this, hr);
  const uint64_t completed = fence->GetCompletedValue();
  const bool succeeded = D3D12IdleWaitSucceeded(waitResult, hr, completed, required);
  if(auditWait)
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=D3D12QueueWait queue=%s type=%u completedBefore=%llu required=%llu "
        "completedAfter=%llu "
        "duration_ms=%.6f success=%u",
        ToStr(GetResID(queue)).c_str(), (uint32_t)queue->GetDesc().Type,
        (unsigned long long)completedBefore, (unsigned long long)required,
        (unsigned long long)completed, auditTimer.GetMilliseconds(), succeeded ? 1U : 0U));
  if(!succeeded)
    return D3D12GPUCompletionFailure(this, StringFormat::Fmt(
        "idle wait timed out or failed validation: wait=%u completed=%llu required=%llu device=%s",
        waitResult, (unsigned long long)completed, (unsigned long long)required, ToStr(hr).c_str()));
  return succeeded;
}

bool WrappedID3D12Device::ReplayQueueSync(ResourceId previousQueue,
                                           ID3D12CommandQueue *nextQueue)
{
  if(previousQueue == ResourceId() || nextQueue == NULL)
    return true;

  ID3D12CommandQueue *next = Unwrap(nextQueue);
  for(size_t i = 0; i < m_Queues.size(); i++)
  {
    WrappedID3D12CommandQueue *previous = m_Queues[i];
    if(previous == NULL || previous->GetResourceID() != previousQueue)
      continue;

    if(previous->GetReal() == next)
      return true;

    const UINT64 value = ++m_WFICounter;
    HRESULT hr = previous->GetReal()->Signal(Unwrap(m_QueueFences[i]), value);
    CHECK_HR(this, hr);
    if(FAILED(hr))
      return false;

    hr = next->Wait(Unwrap(m_QueueFences[i]), value);
    CHECK_HR(this, hr);
    return SUCCEEDED(hr);
  }

  return false;
}

bool WrappedID3D12Device::InstallInitialStateQueueDependencies()
{
  if(HasFatalError() || m_Queue == NULL || m_Queue->GetResourceID() == ResourceId() ||
     m_Queues.empty() || m_QueueFences.size() != m_Queues.size())
    return false;

  const ResourceId internalQueue = m_Queue->GetResourceID();
  bool foundInternalQueue = false;

  // ApplyInitialContents records its GPU work on m_Queue. Put a wait for that work at the head of
  // every other captured queue before the serialised frame is allowed to submit anything. This
  // preserves the old reset-before-frame ordering without making the replay thread wait for the
  // copies itself.
  for(size_t i = 0; i < m_Queues.size(); i++)
  {
    WrappedID3D12CommandQueue *queue = m_Queues[i];
    if(queue == NULL || m_QueueFences[i] == NULL)
      return false;

    foundInternalQueue |= queue == m_Queue;

    if(!ReplayQueueSync(internalQueue, queue))
      return false;
  }

  return foundInternalQueue;
}

void WrappedID3D12Device::ReplayQueueWaitForPreviousWork(ID3D12CommandQueue *queue)
{
  if(queue == NULL)
    return;

  ID3D12CommandQueue *current = Unwrap(queue);
  for(size_t i = 0; i < m_Queues.size(); i++)
  {
    WrappedID3D12CommandQueue *previous = m_Queues[i];
    if(previous == NULL || previous->GetReal() == current)
      continue;

    const UINT64 value = ++m_WFICounter;
    HRESULT hr = previous->GetReal()->Signal(Unwrap(m_QueueFences[i]), value);
    CHECK_HR(this, hr);
    if(FAILED(hr))
      return;

    hr = current->Wait(Unwrap(m_QueueFences[i]), value);
    CHECK_HR(this, hr);
    if(FAILED(hr))
      return;
  }
}

bool WrappedID3D12Device::ReplayWorkWaitForIdle()
{
  if(HasFatalError())
    return false;
  if(m_WaitedForIdleAfterReplay)
    return true;

  if(!DeviceWaitForIdle())
    return false;

  m_WaitedForIdleAfterReplay = true;
  return true;
}

void WrappedID3D12Device::AuditReplayDescriptorUsers(ResourceId heap, uint32_t index)
{
  D3D12CommandData &cmd = *m_Queue->GetCommandData();
  uint32_t matches = 0, unknown = 0, firstEvent = 0, firstRange = 0, firstCount = 0;
  for(const auto &entry : cmd.m_Partial[D3D12CommandData::Primary].cmdListExecs)
  {
    const auto found = cmd.m_BakedCmdListInfo.find(entry.first);
    if(found == cmd.m_BakedCmdListInfo.end())
      continue;
    const BakedCmdListInfo &info = found->second;
    for(uint32_t base : entry.second)
    {
      if(base >= cmd.m_RootEventID || uint64_t(base) + info.eventCount >= cmd.m_RootEventID)
        continue;
      unknown += info.replayTextureReadsComplete ? 0 : 1;
      for(const auto &read : info.replayTextureReadOps)
      {
        unknown += read.unknown ? 1 : 0;
        for(const auto &range : read.descriptorRanges)
          if(range.heap == heap && index >= range.first && index - range.first < range.count)
          {
            matches++;
            if(!firstEvent || base + read.relativeEvent < firstEvent)
            {
              firstEvent = base + read.relativeEvent;
              firstRange = range.first;
              firstCount = range.count;
            }
          }
      }
    }
  }
  AppendReplayTimingLog(StringFormat::Fmt(
      "api=D3D12DescriptorUsers rootEvent=%u heap=%s index=%u declaredRangeMatches=%u "
      "unknownReads=%u firstEvent=%u firstRange=%u firstCount=%u scope=precedingCaptureSubmissions",
      cmd.m_RootEventID, ToStr(heap).c_str(), index, matches, unknown, firstEvent, firstRange,
      firstCount));
}

bool WrappedID3D12Device::ShouldUseConservativeReplaySync(bool allowLoading) const
{
  // A same-binary switch supports capture-specific A/B timing and image comparisons.
  static const bool conservative =
      Process::GetEnvVariable("RENDERDOC_D3D12_CONSERVATIVE_REPLAY_SYNC") == "1";
  return conservative || (!allowLoading && !IsActiveReplaying(m_State)) ||
         m_ReplayOptions.apiValidation || D3D12_Debug_SingleSubmitFlushing() ||
         D3D12_Debug_RT_Auditing();
}

bool WrappedID3D12Device::ReplayDescriptorSlotMutationNeedsWait(const D3D12Descriptor &dst,
                                                                const D3D12Descriptor &replacement)
{
  if(!m_ReplayDescriptorWorkPending && !ShouldUseConservativeReplaySync())
    return false;
  const auto shaderView = [](D3D12DescriptorType type) {
    return type == D3D12DescriptorType::SRV || type == D3D12DescriptorType::UAV ||
           type == D3D12DescriptorType::CBV || type == D3D12DescriptorType::Sampler;
  };
  if(!m_PreciseDescriptorWaitScope || !shaderView(dst.GetType()) ||
     !shaderView(replacement.GetType()))
    return true;
  const char *reason = "unknown";
  D3D12CommandData &cmd = *m_Queue->GetCommandData();
  const rdcpair<ResourceId, uint64_t> key = {
      dst.GetHeapResourceId(), (uint64_t(dst.GetHeapIndex()) << 32) | cmd.m_RootEventID};
  const auto found = m_DescriptorWaitProofCache.find(key);
  bool unused = false;
  if(found != m_DescriptorWaitProofCache.end())
  {
    unused = found->second.first;
    reason = found->second.second;
  }
  else
  {
    unused = cmd.DescriptorSlotUnusedBefore(dst.GetHeapResourceId(), dst.GetHeapIndex(),
                                            cmd.m_RootEventID, reason);
    if(m_DescriptorWaitProofCache.size() < 65536)
      m_DescriptorWaitProofCache[key] = {unused, reason};
  }
  if(m_DescriptorWaitAudit)
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=D3D12DescriptorHazard rootEvent=%u heap=%s index=%u wait=%u reason=%s", cmd.m_RootEventID,
        ToStr(dst.GetHeapResourceId()).c_str(), dst.GetHeapIndex(), unused ? 0U : 1U, reason));
  return !unused;
}

void WrappedID3D12Device::InvalidatePreparedReplayCache(bool readOnlyPixelShader,
                                                        bool readBoundsPreserved)
{
  m_ReplayDescriptorCopyCache.clear();
  m_ReplayDescriptorCopyCacheBytes = 0;
  m_DescriptorWaitProofCache.clear();
  if(HasFatalError())
    return;
  InvalidateLiveForward("resourceOrShaderReplacement");
  m_ShaderMutationWriteProofAllowed =
      readOnlyPixelShader && readBoundsPreserved && !m_ReplayDirtyDependenciesEdited &&
      !m_CheckpointDependenciesEdited &&
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_SHADER_DIRTY_REUSE") != "1";
  const bool keepDirty = m_ShaderMutationWriteProofAllowed && m_ReplayDirtyValid;
  if(!keepDirty)
    InvalidateReplayDirtyTracking("resourceOrShaderReplacement");
  else
    m_ReplayDirtyReason = "readOnlyPixelShaderKnownWrites";
  // Every previously rendered result is invalidated below. Rebuilding under this Shader
  // version may still reuse complete loading-time bounds when both read and write envelopes
  // were proved unchanged; names and field-level reflection are not that proof.
  m_CheckpointDependenciesEdited |= !readBoundsPreserved;
  // The complete native write union covers all bound RT/DS destinations, independent of the
  // old pixel values or discard branches. An admitted read-only PS cannot enlarge that union.
  // Keep the exact dirty set for its scoped mutation replay, without admitting old result
  // checkpoints, Shader-derived read extents, descriptor proofs or native command lists.
  // Once an unclassified edit occurs, this capture keeps the existing conservative fallback.
  m_ReplayDirtyDependenciesEdited |= !readOnlyPixelShader;
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    AppendReplayTimingLog(
        StringFormat::Fmt("api=D3D12DependencyMutation readOnlyPixel=%u readBoundsPreserved=%u "
                          "dirtyBlocked=%u checkpointBlocked=%u dirtyProofRetained=%u",
                          readOnlyPixelShader ? 1U : 0U, readBoundsPreserved ? 1U : 0U,
                          m_ReplayDirtyDependenciesEdited ? 1U : 0U,
                          m_CheckpointDependenciesEdited ? 1U : 0U, keepDirty ? 1U : 0U));
  InvalidateReplayCheckpoints();
  if(HasFatalError())
    return;
  if(m_ReplayCheckpoints)
    m_ReplayCheckpoints->editedDependencies = m_CheckpointDependenciesEdited;
  // Resource/PSO replacement can change descriptors even when no native list was cached. Force
  // their next initial-state restore before the prepared-list early return below.
  if(m_Replay)
  {
    for(const DescriptorStoreDescription &store : m_Replay->GetDescriptorStores())
    {
      if(GetResourceManager()->HasResource(store.resourceId))
        GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(store.resourceId)
            ->InvalidateInitialDescriptorRestore();
    }
  }

  bool hasPreparedLists = false;
  for(WrappedID3D12CommandQueue *queue : m_Queues)
    hasPreparedLists |= queue && queue->HasPreparedReplayCache();

  if(!hasPreparedLists)
    return;

  // Native command lists retain direct PSO/resource addresses. Do not release those references
  // until all prior submissions have completed, then rebuild lazily from the capture stream.
  ScopedReplayTimingContext timingContext(ReplayTimingContext::PreparedCacheInvalidate);
  if(!ReplayWorkWaitForIdle())
    return;
  for(WrappedID3D12CommandQueue *queue : m_Queues)
    if(queue)
      queue->ClearPreparedReplayCache();
}

bool WrappedID3D12Device::DeviceWaitForIdle()
{
  if(HasFatalError())
    return false;
  // Capture and structured export do not create replay queue fences. Preserve their existing
  // empty-fence no-op; an incomplete replay fence table must never authorise memory reuse.
  if(m_QueueFences.empty() &&
     (m_Queues.empty() || IsCaptureMode(m_State) || IsStructuredExporting(m_State)))
  {
    m_ReplayDescriptorWorkPending = false;
    return true;
  }
  if(m_QueueFences.size() != m_Queues.size())
    return D3D12GPUCompletionFailure(this, "replay queue/fence table is incomplete");
  for(size_t i = 0; i < m_QueueFences.size(); i++)
    if(!QueueWaitForIdle(m_Queues[i], m_QueueFences[i]))
      return false;
  m_ReplayDescriptorWorkPending = false;
  return true;
}

ID3D12GraphicsCommandList *WrappedID3D12Device::CreateDataUploadList()
{
  if(m_DataUploadAlloc == NULL || HasFatalError())
    return NULL;

  ID3D12GraphicsCommandList *list = NULL;
  HRESULT hr = CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, m_DataUploadAlloc, NULL,
                                 __uuidof(ID3D12GraphicsCommandList), (void **)&list);
  CHECK_HR(this, hr);
  if(FAILED(hr) || list == NULL)
    return NULL;

  GetResourceManager()->SetInternalResource(list);
  InternalRef();
  AddReplayTimingCounter(ReplayTimingCounter::DataUploadListsCreated);

  // Put the new list under the pool's single release path before closing it. A Close failure is
  // fatal and the list will never be acquired for replay.
  m_DataUploadLists.push_back(list);
  hr = list->Close();
  CHECK_HR(this, hr);
  if(FAILED(hr))
    return NULL;

  return list;
}

ID3D12GraphicsCommandList *WrappedID3D12Device::AcquireDataUploadList()
{
  if(HasFatalError())
    return NULL;

  if(m_CurDataUpload > m_DataUploadLists.size())
  {
    RDCERR("D3D12 data-upload list cursor moved past the pool high-water mark");
    return NULL;
  }

  // Grow to the replay's high-water mark instead of reusing upload command lists after the original
  // 64 chunks. Only the defensive hard-cap path below synchronises and reuses within one replay.
  if(m_CurDataUpload == m_DataUploadLists.size())
  {
    if(m_DataUploadLists.size() < dataUploadMaxPoolSize)
    {
      if(CreateDataUploadList() == NULL)
        return NULL;
    }
    else
    {
      // Bound retained memory for malformed or extreme captures. All data-upload lists execute on
      // the internal queue, so waiting for that queue makes it safe to reset their dedicated
      // allocator and reuse the pool from the beginning.
      ScopedReplayTimingContext timingContext(ReplayTimingContext::DataUploadPoolReuse);
      AddReplayTimingCounter(ReplayTimingCounter::DataUploadPoolResets);
      if(!InternalQueueWaitForIdle() || HasFatalError())
        return NULL;

      HRESULT hr = m_DataUploadAlloc->Reset();
      CHECK_HR(this, hr);
      if(FAILED(hr))
        return NULL;

      m_CurDataUpload = 0;
      m_DataUploadPending = false;
    }
  }

  return m_DataUploadLists[m_CurDataUpload++];
}

ID3D12GraphicsCommandListX *WrappedID3D12Device::GetNewList()
{
  if(HasFatalError())
    return NULL;
  ID3D12GraphicsCommandListX *ret = NULL;

  m_WaitedForIdleAfterReplay = false;

  if(!m_InternalCmds.freecmds.empty())
  {
    AddReplayTimingCounter(ReplayTimingCounter::InternalListsReused);
    ret = m_InternalCmds.freecmds.back();
    m_InternalCmds.freecmds.pop_back();

    const HRESULT resetResult = ret->Reset(m_Alloc, NULL);
    if(FAILED(resetResult))
    {
      // A reused list that did not reset cannot certify any subsequently recorded initial data.
      InvalidateReplayDirtyTracking("internalCommandListResetFailed");
      CHECK_HR(this, resetResult);
    }
  }
  else
  {
    ID3D12GraphicsCommandList *list = NULL;
    HRESULT hr = CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, m_Alloc, NULL,
                                   __uuidof(ID3D12GraphicsCommandList), (void **)&list);
    InternalRef();
    AddReplayTimingCounter(ReplayTimingCounter::InternalListsCreated);

    // safe to upcast because this is a wrapped object.
    ret = (ID3D12GraphicsCommandListX *)list;

    RDCASSERTEQUAL(hr, S_OK);
    CHECK_HR(this, hr);

    if(ret == NULL)
      return NULL;

    if(IsReplayMode(m_State))
    {
      // add a reference here so that when we release our internal resources on destruction we don't
      // free this too soon before the resource manager can. We still want to have it tracked as a
      // resource in the manager though.
      ret->AddRef();
    }
  }

  m_InternalCmds.pendingcmds.push_back(ret);

  return ret;
}

ID3D12GraphicsCommandListX *WrappedID3D12Device::GetInitialStateList()
{
  if(initStateCurBatch >= initialStateMaxBatch)
  {
    CloseInitialStateList();
  }

  if(initStateCurList == NULL)
  {
    AddReplayTimingCounter(ReplayTimingCounter::InitialCommandLists);
    initStateCurList = GetNewList();

    if(IsReplayMode(m_State))
    {
      D3D12MarkerRegion::Begin(initStateCurList,
                               "!!!!RenderDoc Internal: ApplyInitialContents batched list");
    }
  }

  initStateCurBatch++;

  return initStateCurList;
}

ID3D12GraphicsCommandListX *WrappedID3D12Device::StealNewList()
{
  ID3D12GraphicsCommandListX *ret = GetNewList();
  m_InternalCmds.pendingcmds.removeOne(ret);
  return ret;
}

void WrappedID3D12Device::ReturnStolenList(ID3D12GraphicsCommandListX *list)
{
  m_InternalCmds.freecmds.push_back(list);
}

void WrappedID3D12Device::CloseInitialStateList()
{
  ScopedReplayTimingSection timing(ReplayTimingSection::InitialListClose);
  D3D12MarkerRegion::End(initStateCurList);
  const HRESULT closeResult = initStateCurList->Close();
  if(FAILED(closeResult))
  {
    // Intermediate batches contribute to the same initial-state residency transaction as the
    // final batch. A failure in any of them must discard the entire pending baseline.
    InvalidateReplayDirtyTracking("initialListCloseFailed");
    CHECK_HR(this, closeResult);
  }
  initStateCurList = NULL;
  initStateCurBatch = 0;
}

void WrappedID3D12Device::ExecuteList(ID3D12GraphicsCommandListX *list,
                                      WrappedID3D12CommandQueue *queue, bool InFrameCaptureBoundary)
{
  if(HasFatalError())
    return;

  if(queue == NULL)
    queue = GetQueue();

  ID3D12CommandList *l = list;
  AddReplayTimingCounter(ReplayTimingCounter::InternalListsSubmitted);
  queue->ExecuteCommandListsInternal(1, &l, InFrameCaptureBoundary, false);

  MarkListExecuted(list);
}

void WrappedID3D12Device::MarkListExecuted(ID3D12GraphicsCommandListX *list)
{
  m_InternalCmds.pendingcmds.removeOne(list);
  m_InternalCmds.submittedcmds.push_back(list);
}

void WrappedID3D12Device::ExecuteLists(WrappedID3D12CommandQueue *queue, bool InFrameCaptureBoundary,
                                       bool descriptorIndependent)
{
  if(HasFatalError())
    return;

  // nothing to do
  if(m_InternalCmds.pendingcmds.empty())
    return;

  rdcarray<ID3D12CommandList *> cmds;
  cmds.resize(m_InternalCmds.pendingcmds.size());
  for(size_t i = 0; i < cmds.size(); i++)
    cmds[i] = m_InternalCmds.pendingcmds[i];

  if(queue == NULL)
    queue = GetQueue();

  for(size_t i = 0; i < cmds.size(); i += executeListsMaxSize)
  {
    UINT cmdCount = RDCMIN(executeListsMaxSize, (UINT)(cmds.size() - i));
    AddReplayTimingCounter(ReplayTimingCounter::InternalListsSubmitted, cmdCount);
    queue->ExecuteCommandListsInternal(cmdCount, &cmds[i], InFrameCaptureBoundary, false,
                                      descriptorIndependent);
  }

  m_InternalCmds.submittedcmds.append(m_InternalCmds.pendingcmds);
  m_InternalCmds.pendingcmds.clear();
}

bool WrappedID3D12Device::FlushLists(bool forceSync)
{
  if(HasFatalError())
    return false;

  if(!m_InternalCmds.submittedcmds.empty() || forceSync)
  {
    if(!QueueWaitForIdle(m_Queue, m_WFIFence))
      return false;

    if(!m_InternalCmds.submittedcmds.empty())
      m_InternalCmds.freecmds.append(m_InternalCmds.submittedcmds);
    m_InternalCmds.submittedcmds.clear();

    if(m_InternalCmds.pendingcmds.empty())
      m_Alloc->Reset();
  }
  return true;
}

const ActionDescription *WrappedID3D12Device::GetAction(uint32_t eventId)
{
  if(eventId >= m_Actions.size())
    return NULL;

  return m_Actions[eventId];
}

bool WrappedID3D12Device::ProcessChunk(ReadSerialiser &ser, D3D12Chunk context)
{
  switch(context)
  {
    case D3D12Chunk::Device_CreateCommandQueue:
      return Serialise_CreateCommandQueue(ser, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateCommandAllocator:
      return Serialise_CreateCommandAllocator(ser, D3D12_COMMAND_LIST_TYPE_DIRECT, IID(), NULL);
    case D3D12Chunk::Device_CreateCommandList:
      return Serialise_CreateCommandList(ser, 0, D3D12_COMMAND_LIST_TYPE_DIRECT, NULL, NULL, IID(),
                                         NULL);

    case D3D12Chunk::Device_CreateGraphicsPipeline:
      return Serialise_CreateGraphicsPipelineState(ser, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateComputePipeline:
      return Serialise_CreateComputePipelineState(ser, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateDescriptorHeap:
      return Serialise_CreateDescriptorHeap(ser, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateRootSignature:
      return Serialise_CreateRootSignature(ser, 0, NULL, 0, IID(), NULL);
    case D3D12Chunk::Device_CreateCommandSignature:
      return Serialise_CreateCommandSignature(ser, NULL, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateHeap: return Serialise_CreateHeap(ser, NULL, IID(), NULL); break;
    case D3D12Chunk::Device_CreateCommittedResource:
      return Serialise_CreateCommittedResource(ser, NULL, D3D12_HEAP_FLAG_NONE, NULL,
                                               D3D12_RESOURCE_STATE_COMMON, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreatePlacedResource:
      return Serialise_CreatePlacedResource(ser, NULL, 0, NULL, D3D12_RESOURCE_STATE_COMMON, NULL,
                                            IID(), NULL);
    case D3D12Chunk::Device_CreateReservedResource:
      return Serialise_CreateReservedResource(ser, NULL, D3D12_RESOURCE_STATE_COMMON, NULL, IID(),
                                              NULL);

    case D3D12Chunk::Device_CreateQueryHeap:
      return Serialise_CreateQueryHeap(ser, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateFence:
      return Serialise_CreateFence(ser, 0, D3D12_FENCE_FLAG_NONE, IID(), NULL);
    case D3D12Chunk::SetName: return Serialise_SetName(ser, 0x0, "");
    case D3D12Chunk::SetShaderDebugPath: return Serialise_SetShaderDebugPath(ser, NULL, NULL);
    case D3D12Chunk::CreateSwapBuffer:
      return Serialise_WrapSwapchainBuffer(ser, NULL, DXGI_FORMAT_UNKNOWN, 0, NULL);
    case D3D12Chunk::Device_CreatePipelineState:
      return Serialise_CreatePipelineState(ser, NULL, IID(), NULL);
    // these functions are serialised as-if they are a real heap.
    case D3D12Chunk::Device_CreateHeapFromAddress:
    case D3D12Chunk::Device_CreateHeapFromAddress1:
    case D3D12Chunk::Device_CreateHeapFromFileMapping:
      return Serialise_CreateHeap(ser, NULL, IID(), NULL);
    case D3D12Chunk::Device_OpenSharedHandle:
      return Serialise_OpenSharedHandle(ser, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateCommandList1:
      return Serialise_CreateCommandList1(ser, 0, D3D12_COMMAND_LIST_TYPE_DIRECT,
                                          D3D12_COMMAND_LIST_FLAG_NONE, IID(), NULL);
    case D3D12Chunk::Device_CreateCommittedResource1:
      return Serialise_CreateCommittedResource1(ser, NULL, D3D12_HEAP_FLAG_NONE, NULL,
                                                D3D12_RESOURCE_STATE_COMMON, NULL, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateHeap1: return Serialise_CreateHeap1(ser, NULL, NULL, IID(), NULL);
    case D3D12Chunk::Device_ExternalDXGIResource:
      return Serialise_OpenSharedHandle(ser, NULL, IID(), NULL);
    case D3D12Chunk::CompatDevice_CreateSharedResource:
    case D3D12Chunk::CompatDevice_CreateSharedHeap:
      return Serialise_OpenSharedHandle(ser, NULL, IID(), NULL);
    case D3D12Chunk::SetShaderExtUAV:
      return Serialise_SetShaderExtUAV(ser, GPUVendor::Unknown, 0, 0, true);
    case D3D12Chunk::Device_CreateCommittedResource2:
      return Serialise_CreateCommittedResource2(ser, NULL, D3D12_HEAP_FLAG_NONE, NULL,
                                                D3D12_RESOURCE_STATE_COMMON, NULL, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreatePlacedResource1:
      return Serialise_CreatePlacedResource1(ser, NULL, 0, NULL, D3D12_RESOURCE_STATE_COMMON, NULL,
                                             IID(), NULL);
    case D3D12Chunk::Device_CreateCommandQueue1:
      return Serialise_CreateCommandQueue1(ser, NULL, IID(), IID(), NULL);
    case D3D12Chunk::Device_CreateCommittedResource3:
      return Serialise_CreateCommittedResource3(ser, NULL, D3D12_HEAP_FLAG_NONE, NULL,
                                                D3D12_BARRIER_LAYOUT_COMMON, NULL, NULL, 0, NULL,
                                                IID(), NULL);
    case D3D12Chunk::Device_CreatePlacedResource2:
      return Serialise_CreatePlacedResource2(ser, NULL, 0, NULL, D3D12_BARRIER_LAYOUT_COMMON, NULL,
                                             0, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateReservedResource1:
      return Serialise_CreateReservedResource1(ser, NULL, D3D12_RESOURCE_STATE_COMMON, NULL, NULL,
                                               IID(), NULL);
    case D3D12Chunk::Device_CreateReservedResource2:
      return Serialise_CreateReservedResource2(ser, NULL, D3D12_BARRIER_LAYOUT_COMMON, NULL, NULL,
                                               0, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateStateObject:
      return Serialise_CreateStateObject(ser, NULL, IID(), NULL);
    case D3D12Chunk::Device_AddToStateObject:
      return Serialise_AddToStateObject(ser, NULL, NULL, IID(), NULL);
    case D3D12Chunk::CreateAS:
      return Serialise_CreateAS(ser, NULL, 0,
                                D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL, 0, NULL);
    case D3D12Chunk::StateObject_SetPipelineStackSize:
      return Serialise_SetPipelineStackSize(ser, NULL, 0);
    case D3D12Chunk::Device_CreateRootSignatureFromSubobjectInLibrary:
      return Serialise_CreateRootSignatureFromSubobjectInLibrary(ser, 0, NULL, 0, NULL, IID(), NULL);
    case D3D12Chunk::Device_CreateQueryHeap1:
      return Serialise_CreateQueryHeap1(ser, NULL, D3D12_QUERY_HEAP_FLAG_NONE, IID(), NULL);

    // in order to get a warning if we miss a case, we explicitly handle the list/queue chunks here.
    // If we actually encounter one it's an error (we should hit CaptureBegin first and switch to
    // D3D12CommandData::ProcessChunk)
    case D3D12Chunk::Device_CreateConstantBufferView:
    case D3D12Chunk::Device_CreateShaderResourceView:
    case D3D12Chunk::Device_CreateUnorderedAccessView:
    case D3D12Chunk::Device_CreateRenderTargetView:
    case D3D12Chunk::Device_CreateDepthStencilView:
    case D3D12Chunk::Device_CreateSampler:
    case D3D12Chunk::Device_CopyDescriptors:
    case D3D12Chunk::Device_CopyDescriptorsSimple:
    case D3D12Chunk::Queue_ExecuteCommandLists:
    case D3D12Chunk::Queue_Signal:
    case D3D12Chunk::Queue_Wait:
    case D3D12Chunk::Queue_UpdateTileMappings:
    case D3D12Chunk::Queue_CopyTileMappings:
    case D3D12Chunk::Queue_BeginEvent:
    case D3D12Chunk::Queue_SetMarker:
    case D3D12Chunk::Queue_EndEvent:
    case D3D12Chunk::List_Close:
    case D3D12Chunk::List_Reset:
    case D3D12Chunk::List_ResourceBarrier:
    case D3D12Chunk::List_BeginQuery:
    case D3D12Chunk::List_EndQuery:
    case D3D12Chunk::List_ResolveQueryData:
    case D3D12Chunk::List_SetPredication:
    case D3D12Chunk::List_DrawIndexedInstanced:
    case D3D12Chunk::List_DrawInstanced:
    case D3D12Chunk::List_Dispatch:
    case D3D12Chunk::List_ExecuteIndirect:
    case D3D12Chunk::List_ExecuteBundle:
    case D3D12Chunk::List_CopyBufferRegion:
    case D3D12Chunk::List_CopyTextureRegion:
    case D3D12Chunk::List_CopyResource:
    case D3D12Chunk::List_ResolveSubresource:
    case D3D12Chunk::List_ClearRenderTargetView:
    case D3D12Chunk::List_ClearDepthStencilView:
    case D3D12Chunk::List_ClearUnorderedAccessViewUint:
    case D3D12Chunk::List_ClearUnorderedAccessViewFloat:
    case D3D12Chunk::List_DiscardResource:
    case D3D12Chunk::List_IASetPrimitiveTopology:
    case D3D12Chunk::List_IASetIndexBuffer:
    case D3D12Chunk::List_IASetVertexBuffers:
    case D3D12Chunk::List_SOSetTargets:
    case D3D12Chunk::List_RSSetViewports:
    case D3D12Chunk::List_RSSetScissorRects:
    case D3D12Chunk::List_SetPipelineState:
    case D3D12Chunk::List_SetDescriptorHeaps:
    case D3D12Chunk::List_OMSetRenderTargets:
    case D3D12Chunk::List_OMSetStencilRef:
    case D3D12Chunk::List_OMSetBlendFactor:
    case D3D12Chunk::List_SetGraphicsRootDescriptorTable:
    case D3D12Chunk::List_SetGraphicsRootSignature:
    case D3D12Chunk::List_SetGraphicsRoot32BitConstant:
    case D3D12Chunk::List_SetGraphicsRoot32BitConstants:
    case D3D12Chunk::List_SetGraphicsRootConstantBufferView:
    case D3D12Chunk::List_SetGraphicsRootShaderResourceView:
    case D3D12Chunk::List_SetGraphicsRootUnorderedAccessView:
    case D3D12Chunk::List_SetComputeRootDescriptorTable:
    case D3D12Chunk::List_SetComputeRootSignature:
    case D3D12Chunk::List_SetComputeRoot32BitConstant:
    case D3D12Chunk::List_SetComputeRoot32BitConstants:
    case D3D12Chunk::List_SetComputeRootConstantBufferView:
    case D3D12Chunk::List_SetComputeRootShaderResourceView:
    case D3D12Chunk::List_SetComputeRootUnorderedAccessView:
    case D3D12Chunk::List_CopyTiles:
    case D3D12Chunk::List_AtomicCopyBufferUINT:
    case D3D12Chunk::List_AtomicCopyBufferUINT64:
    case D3D12Chunk::List_OMSetDepthBounds:
    case D3D12Chunk::List_ResolveSubresourceRegion:
    case D3D12Chunk::List_SetSamplePositions:
    case D3D12Chunk::List_SetViewInstanceMask:
    case D3D12Chunk::List_WriteBufferImmediate:
    case D3D12Chunk::List_BeginRenderPass:
    case D3D12Chunk::List_EndRenderPass:
    case D3D12Chunk::List_RSSetShadingRate:
    case D3D12Chunk::List_RSSetShadingRateImage:
    case D3D12Chunk::PushMarker:
    case D3D12Chunk::PopMarker:
    case D3D12Chunk::SetMarker:
    case D3D12Chunk::Resource_Unmap:
    case D3D12Chunk::Resource_WriteToSubresource:
    case D3D12Chunk::List_IndirectSubCommand:
    case D3D12Chunk::Swapchain_Present:
    case D3D12Chunk::List_ClearState:
    case D3D12Chunk::CoherentMapWrite:
    case D3D12Chunk::Device_CreateSampler2:
    case D3D12Chunk::List_OMSetFrontAndBackStencilRef:
    case D3D12Chunk::List_RSSetDepthBias:
    case D3D12Chunk::List_IASetIndexBufferStripCutValue:
    case D3D12Chunk::List_Barrier:
    case D3D12Chunk::List_DispatchMesh:
    case D3D12Chunk::List_BuildRaytracingAccelerationStructure:
    case D3D12Chunk::List_CopyRaytracingAccelerationStructure:
    case D3D12Chunk::List_EmitRaytracingAccelerationStructurePostbuildInfo:
    case D3D12Chunk::List_DispatchRays:
    case D3D12Chunk::List_SetPipelineState1:
    case D3D12Chunk::List_SetProgram:
    case D3D12Chunk::List_DispatchGraph:
    case D3D12Chunk::SetCommandAnnotation:
    case D3D12Chunk::SetQueueAnnotation:
    case D3D12Chunk::Device_TryCreateShaderResourceView:
    case D3D12Chunk::Device_TryCreateUnorderedAccessView:
    case D3D12Chunk::Device_TryCreateConstantBufferView:
    case D3D12Chunk::Device_TryCreateSampler2:
    case D3D12Chunk::Device_TryCreateRenderTargetView:
    case D3D12Chunk::Device_TryCreateDepthStencilView:
      RDCERR("Unexpected chunk while processing initialisation: %s", ToStr(context).c_str());
      return false;

    // no explicit default so that we have compiler warnings if a chunk isn't explicitly handled.
    case D3D12Chunk::Max: break;
  }

  {
    SystemChunk system = (SystemChunk)context;
    if(system == SystemChunk::DriverInit)
    {
      D3D12InitParams InitParams;
      SERIALISE_ELEMENT(InitParams);

      SERIALISE_CHECK_READ_ERRORS();
    }
    else if(system == SystemChunk::InitialContentsList)
    {
      GetResourceManager()->CreateInitialContents(ser);

      if(initStateCurList)
      {
        CloseInitialStateList();

        ExecuteLists(NULL, true);
        if(!FlushLists())
          return false;
      }

      SERIALISE_CHECK_READ_ERRORS();
    }
    else if(system == SystemChunk::InitialContents)
    {
      return GetResourceManager()->Serialise_InitialState(ser, ResourceId(), NULL, NULL);
    }
    else if(system == SystemChunk::CaptureScope)
    {
      return Serialise_CaptureScope(ser);
    }
    else if(system < SystemChunk::FirstDriverChunk)
    {
      RDCERR("Unexpected system chunk in capture data: %u", system);
      ser.SkipCurrentChunk();

      SERIALISE_CHECK_READ_ERRORS();
    }
    else
    {
      RDCERR("Unexpected chunk %s", ToStr(context).c_str());
      return false;
    }
  }

  return true;
}

ResourceDescription &WrappedID3D12Device::GetResourceDesc(ResourceId id)
{
  return GetReplay()->GetResourceDesc(id);
}

void WrappedID3D12Device::AddResource(ResourceId id, ResourceType type, const char *defaultNamePrefix)
{
  ResourceDescription &descr = GetReplay()->GetResourceDesc(id);

  uint64_t num;
  memcpy(&num, &id, sizeof(uint64_t));
  descr.name = defaultNamePrefix + (" " + ToStr(num));
  descr.autogeneratedName = true;
  descr.type = type;
  AddResourceCurChunk(descr);
}

void WrappedID3D12Device::DerivedResource(ID3D12DeviceChild *parent, ResourceId child)
{
  if(!parent)
    return;

  ResourceId parentId = GetResID(parent);

  DerivedResource(parentId, child);
}

void WrappedID3D12Device::DerivedResource(ResourceId parent, ResourceId child)
{
  if(GetReplay()->GetResourceDesc(parent).derivedResources.contains(child))
    return;

  GetReplay()->GetResourceDesc(parent).derivedResources.push_back(child);
  GetReplay()->GetResourceDesc(child).parentResources.push_back(parent);
}

void WrappedID3D12Device::AddResourceCurChunk(ResourceDescription &descr)
{
  descr.initialisationChunks.push_back((uint32_t)m_StructuredFile->chunks.size() - 1);
}

void WrappedID3D12Device::AddResourceCurChunk(ResourceId id)
{
  AddResourceCurChunk(GetReplay()->GetResourceDesc(id));
}

RDResult WrappedID3D12Device::ReadLogInitialisation(RDCFile *rdc, bool storeStructuredBuffers)
{
  int sectionIdx = rdc->SectionIndex(SectionType::FrameCapture);

  if(sectionIdx < 0)
    RETURN_ERROR_RESULT(ResultCode::FileCorrupted, "File does not contain captured API data");

  StreamReader *reader = rdc->ReadSection(sectionIdx);

  if(IsStructuredExporting(m_State))
  {
    // when structured exporting don't do any timebase conversion
    m_TimeBase = 0;
    m_TimeFrequency = 1.0;
  }
  else
  {
    m_TimeBase = rdc->GetTimestampBase();
    m_TimeFrequency = rdc->GetTimestampFrequency();
  }

  if(reader->IsErrored())
  {
    RDResult result = reader->GetError();
    delete reader;
    return result;
  }

  ReadSerialiser ser(reader, Ownership::Stream);

  APIProps.DXILShaders = m_UsedDXIL = m_InitParams.usedDXIL;

  ser.SetStringDatabase(&m_StringDB);
  ser.SetUserData(GetResourceManager());

  ser.ConfigureStructuredExport(&GetChunkName, storeStructuredBuffers, m_TimeBase, m_TimeFrequency);

  m_StructuredFile = &ser.GetStructuredFile();

  m_StoredStructuredData->version = m_StructuredFile->version = m_SectionVersion;

  ser.SetVersion(m_SectionVersion);

  int chunkIdx = 0;

  struct chunkinfo
  {
    chunkinfo() : count(0), totalsize(0), total(0.0) {}
    int count;
    uint64_t totalsize;
    double total;
  };

  std::map<D3D12Chunk, chunkinfo> chunkInfos;

  SCOPED_TIMER("chunk initialisation");

  uint64_t frameDataSize = 0;

  for(;;)
  {
    PerformanceTimer timer;

    uint64_t offsetStart = reader->GetOffset();

    D3D12Chunk context = ser.ReadChunk<D3D12Chunk>();

    chunkIdx++;

    if(reader->IsErrored())
      return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);

    UINT64 startMessage = 0;

    // store how many messages existed before this chunk. If it fails we'll only print messages
    // related to/generated by it
    if(m_pInfoQueue)
      startMessage = m_pInfoQueue->GetNumStoredMessagesAllowedByRetrievalFilter();

    bool success = ProcessChunk(ser, context);

    ser.EndChunk();

    if(reader->IsErrored())
      return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);

    // if there wasn't a serialisation error, but the chunk didn't succeed, then it's an API replay
    // failure.
    if(!success)
    {
      rdcstr extra;

      if(m_pInfoQueue)
      {
        extra += "\n";

        for(UINT64 i = startMessage;
            i < m_pInfoQueue->GetNumStoredMessagesAllowedByRetrievalFilter(); i++)
        {
          SIZE_T len = 0;
          m_pInfoQueue->GetMessage(i, NULL, &len);

          char *msgbuf = new char[len];
          D3D12_MESSAGE *message = (D3D12_MESSAGE *)msgbuf;

          m_pInfoQueue->GetMessage(i, message, &len);

          extra += "\n";
          extra += message->pDescription;

          delete[] msgbuf;
        }
      }
      else
      {
        extra +=
            "\n\nMore debugging information may be available by enabling API validation on replay "
            "via `File` -> `Open Capture with Options`";
      }

      if(HasFatalError())
      {
        m_FatalError.message = rdcstr(m_FatalError.message) + extra;
        return m_FatalError;
      }

      m_FailedReplayResult.message = rdcstr(m_FailedReplayResult.message) + extra;
      return m_FailedReplayResult;
    }

    if(m_FatalError != ResultCode::Succeeded)
      return m_FatalError;

    uint64_t offsetEnd = reader->GetOffset();

    RenderDoc::Inst().SetProgress(LoadProgress::FileInitialRead,
                                  float(offsetEnd) / float(reader->GetSize()));

    if((SystemChunk)context == SystemChunk::CaptureScope)
    {
      GetReplay()->WriteFrameRecord().frameInfo.fileOffset = offsetStart;

      // read the remaining data into memory and pass to immediate context
      frameDataSize = reader->GetSize() - reader->GetOffset();

      if(IsStructuredExporting(m_State))
      {
        m_Queue = new WrappedID3D12CommandQueue(ResourceId(), NULL, this, m_State);
        m_Queues.push_back(m_Queue);
      }

      m_Queue->SetFrameReader(new StreamReader(reader, frameDataSize));

      if(!IsStructuredExporting(m_State))
      {
        rdcarray<DebugMessage> savedDebugMessages;

        // save any debug messages we built up
        savedDebugMessages.swap(m_DebugMessages);

        ApplyInitialContents();

        {
          SCOPED_TIMER("Syncing deferred jobs");
          Threading::JobSystem::SyncAllJobs();
          RDCLOG("Total deferred CPU time: %.2fms", m_DeferredTime);
        }

        GetResourceManager()->ResolveDeferredWrappers();

        if(m_DeferredResult != ResultCode::Succeeded)
          return m_DeferredResult;

        for(rdcpair<ID3D12DeviceChild *, rdcstr> &name : m_CustomNames)
          name.first->SetName(StringFormat::UTF82Wide(name.second).c_str());

        // restore saved messages - which implicitly discards any generated while applying initial
        // contents
        savedDebugMessages.swap(m_DebugMessages);
      }

      D3D12CommitAudit frameMemory("frameLoad", 0, 0);
      RDResult result = m_Queue->ReplayLog(m_State, 0, 0, false);

      if(result != ResultCode::Succeeded)
        return result;
    }

    chunkInfos[context].total += timer.GetMilliseconds();
    chunkInfos[context].totalsize += offsetEnd - offsetStart;
    chunkInfos[context].count++;

    if((SystemChunk)context == SystemChunk::CaptureScope || reader->IsErrored() || reader->AtEnd())
      break;
  }

  // steal the structured data for ourselves
  m_StructuredFile->Swap(*m_StoredStructuredData);

  // and in future use this file.
  m_StructuredFile = m_StoredStructuredData;

  if(!IsStructuredExporting(m_State))
  {
    ActionDescription rootAction;
    m_Queue->BakeEventNodes(rootAction);
    rootAction.children.swap(GetReplay()->WriteFrameRecord().actionList);
    SetupActionPointers(m_Actions, GetReplay()->WriteFrameRecord().actionList);
  }

  {
    D3D12CommandData &cmd = *m_Queue->GetCommandData();

    for(auto it = cmd.m_ResourceUses.begin(); it != cmd.m_ResourceUses.end(); ++it)
    {
      if(m_ModResources.find(it->first) != m_ModResources.end())
        continue;

      for(const EventUsage &use : it->second)
      {
        if(use.usage == ResourceUsage::CopyDst || use.usage == ResourceUsage::Copy ||
           use.usage == ResourceUsage::ResolveDst || use.usage == ResourceUsage::Resolve ||
           use.usage == ResourceUsage::GenMips || use.usage == ResourceUsage::Clear ||
           use.usage == ResourceUsage::Discard || use.usage == ResourceUsage::CPUWrite ||
           use.usage == ResourceUsage::ColorTarget ||
           use.usage == ResourceUsage::DepthStencilTarget || use.usage == ResourceUsage::StreamOut)
        {
          m_ModResources.insert(it->first);
          break;
        }
      }
    }
  }

  const bool develMode =
#if ENABLED(RDOC_DEVEL)
      true;
#else
      false;
#endif

  if(Replay_Debug_PrintChunkTimings() || develMode)
  {
    for(auto it = chunkInfos.begin(); it != chunkInfos.end(); ++it)
    {
      double dcount = double(it->second.count);

      RDCLOG(
          "| % 5d chunks - Time: %9.3fms total/%9.3fms avg - Size: %8.3fMB total/%7.3fMB avg - %s "
          "(%u)",
          it->second.count, it->second.total, it->second.total / dcount,
          double(it->second.totalsize) / (1024.0 * 1024.0),
          double(it->second.totalsize) / (dcount * 1024.0 * 1024.0),
          GetChunkName((uint32_t)it->first).c_str(), uint32_t(it->first));
    }
  }

  GetReplay()->WriteFrameRecord().frameInfo.uncompressedFileSize =
      rdc->GetSectionProperties(sectionIdx).uncompressedSize;
  GetReplay()->WriteFrameRecord().frameInfo.compressedFileSize =
      rdc->GetSectionProperties(sectionIdx).compressedSize;
  GetReplay()->WriteFrameRecord().frameInfo.persistentSize = frameDataSize;
  GetReplay()->WriteFrameRecord().frameInfo.initDataSize =
      chunkInfos[(D3D12Chunk)SystemChunk::InitialContents].totalsize;

  RDCDEBUG("Allocating %llu persistent bytes of memory for the log.",
           GetReplay()->WriteFrameRecord().frameInfo.persistentSize);

  if(m_FatalError != ResultCode::Succeeded)
    return m_FatalError;

  if(m_pDevice && m_pDevice->GetDeviceRemovedReason() != S_OK)
    RETURN_ERROR_RESULT(ResultCode::DeviceLost, "Device lost during load: %s",
                        ToStr(m_pDevice->GetDeviceRemovedReason()).c_str());

  return ResultCode::Succeeded;
}

static bool D3D12QueryCheckpointMemory(ID3D12Device *device, DXGI_QUERY_VIDEO_MEMORY_INFO &memory)
{
  IDXGIFactory4 *factory = NULL;
  IDXGIAdapter3 *adapter = NULL;
  typedef HRESULT(WINAPI * PFN_CREATE_DXGI_FACTORY)(REFIID, void **);
  PFN_CREATE_DXGI_FACTORY createFactory = (PFN_CREATE_DXGI_FACTORY)GetProcAddress(
      GetModuleHandleA("dxgi.dll"), "CreateDXGIFactory1");
  if(createFactory && SUCCEEDED(createFactory(__uuidof(IDXGIFactory4), (void **)&factory)))
    factory->EnumAdapterByLuid(device->GetAdapterLuid(), __uuidof(IDXGIAdapter3),
                               (void **)&adapter);
  const bool known = adapter && SUCCEEDED(adapter->QueryVideoMemoryInfo(
      0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &memory));
  SAFE_RELEASE(adapter);
  SAFE_RELEASE(factory);
  return known;
}

static uint64_t D3D12CheckpointHeadroom(uint64_t requested,
                                      const DXGI_QUERY_VIDEO_MEMORY_INFO &memory)
{
  const uint64_t reserve = RDCMAX(256ULL * 1024 * 1024, memory.Budget / 16);
  if(memory.CurrentUsage >= memory.Budget || memory.Budget - memory.CurrentUsage <= reserve)
    return 0;
  return RDCMIN(requested, memory.Budget - memory.CurrentUsage - reserve);
}

static bool D3D12RecoverCheckpointBudget(bool enabled, bool stored, uint64_t requested,
                                        bool queryKnown, const DXGI_QUERY_VIDEO_MEMORY_INFO &memory,
                                        bool &blocked, uint64_t &budget)
{
  if(!enabled || stored || !blocked || budget || !queryKnown)
    return false;
  const uint64_t next = D3D12CheckpointHeadroom(requested, memory);
  if(!next)
    return false;
  budget = next;
  blocked = false;
  return true;
}

static uint64_t D3D12CheckpointDescriptorAdmissionBytes(uint64_t fullBytes,
                                                        uint64_t capturedFullBytes,
                                                        uint64_t capturedBytes, bool edited)
{
  // Descriptor stores are append-only for this loaded capture. Any newly observed store or
  // replacement invalidates the boundary's accounting, never making missing evidence zero bytes.
  return !edited && capturedFullBytes == fullBytes && capturedBytes <= fullBytes ? capturedBytes
                                                                                 : fullBytes;
}

static bool D3D12ConsumeCheckpointDescriptorPlanWork(uint64_t &used, uint64_t heapCount)
{
  const uint64_t work = RDCMAX<uint64_t>(1, heapCount);
  if(used > 1000000 || work > 1000000 - used)
  {
    used = 1000001;
    return false;
  }
  used += work;
  return true;
}

void WrappedID3D12Device::RecordCheckpointDescriptorBytes(uint64_t &bytes, uint64_t &fullBytes)
{
  bytes = fullBytes = ~0ULL;
  if(!m_Replay || !IsLoading(m_State) || HasFatalError())
    return;
  const auto &stores = m_Replay->GetReplayDescriptorStores();
  // Count the same native dirty blocks as Save, without copying the store list or its ranges.
  // A finite whole-load scan budget bounds even captures with many queues and descriptor heaps.
  if(!D3D12ConsumeCheckpointDescriptorPlanWork(m_CheckpointDescriptorPlanWork, stores.size()))
    return;
  uint64_t counted = 0, full = 0;
  for(const DescriptorStoreDescription &store : stores)
  {
    if(!GetResourceManager()->HasResource(store.resourceId))
      return;
    WrappedID3D12DescriptorHeap *heap =
        GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(store.resourceId);
    if(!heap || heap->GetNumDescriptors() != store.descriptorCount)
      return;
    const uint64_t heapFull = uint64_t(store.descriptorCount) * sizeof(D3D12Descriptor);
    const uint64_t heapBytes = heap->GetCheckpointDescriptorBytes();
    if(heapBytes > heapFull || full > ~0ULL - heapFull || counted > ~0ULL - heapBytes)
      return;
    full += heapFull;
    counted += heapBytes;
  }
  bytes = counted;
  fullBytes = full;
}

void WrappedID3D12Device::ConfigureReplayCheckpoints(
    const ReplayCheckpointConfig &config, const rdcarray<ReplayCheckpointCandidate> &candidates)
{
  if(HasFatalError())
    return;
  const bool preserve = m_ReplayCheckpoints &&
      ReplayCheckpointStorageCompatible(m_ReplayCheckpoints->config, config);
  if(!preserve)
  {
    if(m_ReplayCheckpoints && !m_ReplayCheckpoints->checkpoints.empty() && !DeviceWaitForIdle())
      return;
    InvalidateReplayCheckpoints();
    if(HasFatalError())
      return;
  }
  if(!m_ReplayCheckpoints)
    m_ReplayCheckpoints = new D3D12ReplayCheckpointCache;
  D3D12ReplayCheckpointCache &cache = *m_ReplayCheckpoints;
  D3D12CommandData::CheckpointBoundary retainedAnchor;
  uint64_t retainedAnchorBytes = 0;
  if(preserve && !m_ReplayBufferPatches.empty() &&
     cache.bufferEditAnchorConsumer == m_ReplayBufferPatchFirstConsumer)
  {
    auto anchor = cache.selected.find(m_ReplayBufferPatchAnchorEvent);
    if(anchor != cache.selected.end() && anchor->first < m_ReplayBufferPatchFirstConsumer)
    {
      retainedAnchor = anchor->second;
      retainedAnchorBytes = cache.selectedBytes[anchor->first];
    }
  }
  cache.config = config;
  cache.editedDependencies = m_CheckpointDependenciesEdited;
  if(!preserve)
    cache.bufferEditAnchorConsumer = 0;
  cache.bufferEditFirstEvent = m_ReplayBufferPatches.empty()
                                  ? ~0U
                                  : RDCMAX(1U, m_ReplayBufferPatchFirstConsumer);
  cache.selected.clear();
  cache.selectedBytes.clear();
  cache.overlappingPlaced.clear();
  cache.bufferAliasComponents.clear();
  cache.bufferAliasMembers.clear();
  cache.typedAliasConflicts.clear();
  cache.selectedTypedOwners.clear();
  cache.provedTypedBoundaries.clear();
  cache.stats.supported = true;
  cache.stats.enabled = config.enabled;
  cache.stats.reason.clear();
  if(!preserve)
    cache.budget = config.byteBudget;
  DXGI_QUERY_VIDEO_MEMORY_INFO memory = {};
  uint32_t firstUnsupportedWrite = ~0U;
  ResourceId firstUnsupportedResource;
  rdcstr firstUnsupportedStorage;
  uint64_t descriptorBytes = 0;
  const auto retainSelectedCheckpoints = [&]() {
    if(retainedAnchor.eventId && cache.budget)
    {
      cache.selected[retainedAnchor.eventId] = retainedAnchor;
      cache.selectedBytes[retainedAnchor.eventId] = retainedAnchorBytes;
    }
    bool removing = false;
    for(const auto &point : cache.checkpoints)
      removing |= !cache.selected.count(point.first);
    if(removing)
    {
      // Saved resource copies can be referenced by the last restore. Their GPU work must retire
      // before removing the final immutable source owner. Active query sources remain held by
      // the current materialised state until the next normal root reset.
      if(!DeviceWaitForIdle())
        return false;
      cache.currentVersions.clear();
      cache.currentBufferAliases.clear();
      for(auto point = cache.checkpoints.begin(); point != cache.checkpoints.end();)
      {
        if(!cache.selected.count(point->first))
        {
          point = cache.checkpoints.erase(point);
          cache.stats.invalidations++;
        }
        else
          ++point;
      }
      if(cache.checkpoints.empty())
        cache.stats.sharedBytes = 0;
    }
    return true;
  };
  const auto logConfiguration = [&]() {
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    {
      rdcstr selected, queryLimits, queryPlans;
      for(const auto &boundary : cache.selected)
      {
        selected += StringFormat::Fmt("%s%u:%llu", selected.empty() ? "" : ",", boundary.first,
                                     (unsigned long long)cache.selectedBytes[boundary.first]);
        if(boundary.second.firstNativeResolveEID != ~0U)
          queryLimits += StringFormat::Fmt("%s%u:%u:%s", queryLimits.empty() ? "" : ",",
                                           boundary.first, boundary.second.firstNativeResolveEID,
                                           ToStr(boundary.second.firstNativeResolveType).c_str());
        const auto &queries = boundary.second.queries;
        queryPlans += StringFormat::Fmt(
            "%s%u:slots=%zu:resolves=%zu:unsupported=%s/%u:%s", queryPlans.empty() ? "" : ";",
            boundary.first, queries.slots.size(), queries.resolves.size(),
            ToStr(queries.unsupportedSlot.first).c_str(), queries.unsupportedSlot.second,
            queries.unsupportedReason.c_str());
      }
      LogD3D12Checkpoint(StringFormat::Fmt(
          "api=D3D12Checkpoint op=configure enabled=%u requestedBytes=%llu effectiveBytes=%llu "
          "videoBudget=%llu videoUsage=%llu maxCheckpoints=%u requestedCandidates=%zu "
          "safeCandidates=%zu overlappingPlaced=%zu sparseResources=%zu descriptorBytes=%llu "
          "bufferAliasComponents=%zu bufferAliasMembers=%zu typedAliasMembers=%zu "
          "firstUnsupportedWrite=%u unsupportedResource=%s events=%s queryLimits=%s "
          "queryPlans=%s preserveStorage=%u retainedCheckpoints=%zu reason=%s",
          config.enabled ? 1U : 0U, (unsigned long long)config.byteBudget,
          (unsigned long long)cache.budget, (unsigned long long)memory.Budget,
          (unsigned long long)memory.CurrentUsage, config.maxCheckpoints, candidates.size(),
          cache.selected.size(), cache.overlappingPlaced.size(), m_SparseResources.size(),
          (unsigned long long)descriptorBytes, cache.bufferAliasComponents.size(),
          cache.bufferAliasMembers.size(), cache.typedAliasConflicts.size(), firstUnsupportedWrite,
          ToStr(firstUnsupportedResource).c_str(), selected.c_str(), queryLimits.c_str(),
          queryPlans.c_str(), preserve ? 1U : 0U, cache.checkpoints.size(),
          cache.stats.reason.c_str()));
      if(!firstUnsupportedStorage.empty())
        LogD3D12Checkpoint(firstUnsupportedStorage);
    }
  };
  if(!config.enabled || !config.maxCheckpoints || !config.byteBudget)
  {
    cache.budget = 0;
    cache.memoryBudgetBlocked = false;
    if(!retainSelectedCheckpoints())
      return;
    logConfiguration();
    return;
  }

  // Cap the caller's budget by current DXGI headroom. The immutable initial-state source cache
  // and every existing capture allocation are already included in CurrentUsage.
  // A compatible candidate update keeps its existing reservation: subtracting CurrentUsage
  // again would charge the retained checkpoint allocations against their own available budget.
  if(!preserve)
  {
    cache.memoryBudgetBlocked = false;
    if(!D3D12QueryCheckpointMemory(GetReal(), memory))
    {
      cache.budget = 0;
      cache.stats.reason = "DXGI checkpoint memory budget is unavailable";
    }
    else
    {
      cache.budget = D3D12CheckpointHeadroom(cache.budget, memory);
      if(!cache.budget)
      {
        cache.memoryBudgetBlocked = true;
        cache.stats.reason = "Insufficient GPU memory headroom for checkpoints";
      }
    }
  }
  else if(cache.memoryBudgetBlocked)
    cache.stats.reason = "Insufficient GPU memory headroom for checkpoints";
  if(!cache.budget)
  {
    if(!retainSelectedCheckpoints())
      return;
    logConfiguration();
    return;
  }

  // Placed resources are supported when their storage is independent. Heap flags alone cannot
  // establish this: retain HeapOffset from resource creation and test actual allocation ranges.
  using PlacedRange = D3D12CheckpointPlacedRange;
  std::map<ResourceId, rdcarray<PlacedRange>> heaps;
  std::set<ResourceId> unsupportedBufferHeaps;
  std::set<ResourceId> unsupportedTypedHeaps;
  BuildReplaySparseDirtyGraph();
  for(const auto &entry : GetResourceList())
  {
    if(ResourceIDGen::IsReplayOnlyID(entry.first) || !entry.second->GetHeap())
      continue;
    const D3D12_RESOURCE_DESC desc = entry.second->GetReal()->GetDesc();
    const uint64_t bytes = GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
    const uint64_t begin = entry.second->GetHeapOffset();
    WrappedID3D12Heap *heap = entry.second->GetHeap();
    const ResourceId heapId = GetResID(heap);
    const D3D12_HEAP_DESC heapDesc = heap->GetDesc();
    const UINT allowedHeapFlags = D3D12_HEAP_FLAG_DENY_RT_DS_TEXTURES |
                                  D3D12_HEAP_FLAG_DENY_NON_RT_DS_TEXTURES |
                                  D3D12_HEAP_FLAG_CREATE_NOT_ZEROED;
    const bool independentHeap =
        D3D12CheckpointOriginalHeap(GetResourceManager(), heapId) == heap &&
        heapDesc.Properties.Type == D3D12_HEAP_TYPE_DEFAULT &&
        heapDesc.Properties.CreationNodeMask == 1 && heapDesc.Properties.VisibleNodeMask == 1 &&
        !(UINT(heapDesc.Flags) & ~(allowedHeapFlags | D3D12_HEAP_FLAG_DENY_BUFFERS)) &&
        m_ReplaySparseGraphComplete && !m_ReplaySparseComponentIndex.count(heapId);
    if(!independentHeap)
      unsupportedTypedHeaps.insert(heapId);
    if(desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
       desc.Layout != D3D12_TEXTURE_LAYOUT_ROW_MAJOR || desc.Format != DXGI_FORMAT_UNKNOWN ||
       entry.second->IsAccelerationStructureResource() ||
       GetResourceManager()->HasReplacement(entry.first) ||
       GetResourceManager()->GetUnreplacedID(entry.first) != entry.first ||
       heapDesc.Properties.Type != D3D12_HEAP_TYPE_DEFAULT ||
       heapDesc.Properties.CreationNodeMask != 1 || heapDesc.Properties.VisibleNodeMask != 1 ||
       (UINT(heapDesc.Flags) & ~allowedHeapFlags) || !heap->GetUnwrappedWholeMemBuffer() ||
       !m_ReplaySparseGraphComplete || m_ReplaySparseComponentIndex.count(heapId) || !independentHeap)
      unsupportedBufferHeaps.insert(heapId);
    if(!bytes || bytes == ~0ULL || begin > heapDesc.SizeInBytes ||
       bytes > heapDesc.SizeInBytes - begin)
    {
      cache.overlappingPlaced.insert(entry.first);
      unsupportedBufferHeaps.insert(heapId);
      unsupportedTypedHeaps.insert(heapId);
      continue;
    }
    heaps[heapId].push_back({entry.first, begin, begin + bytes});
  }
  for(auto &heap : heaps)
  {
    auto &ranges = heap.second;
    std::sort(ranges.begin(), ranges.end(),
              [](const PlacedRange &a, const PlacedRange &b) { return a.begin < b.begin; });
    for(size_t i = 0; i < ranges.size(); i++)
      for(size_t j = i + 1; j < ranges.size() && ranges[j].begin < ranges[i].end; j++)
      {
        cache.overlappingPlaced.insert(ranges[i].resource);
        cache.overlappingPlaced.insert(ranges[j].resource);
      }
  }
  uint64_t bufferAliasBytes = 0;
  if(Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_CHECKPOINT_BUFFER_ALIASES") != "1")
    for(const auto &heap : heaps)
    {
      WrappedID3D12Heap *nativeHeap = D3D12CheckpointOriginalHeap(GetResourceManager(), heap.first);
      if(!nativeHeap || unsupportedBufferHeaps.count(heap.first))
        continue;
      for(auto component :
          D3D12BufferAliasComponents(heap.first, nativeHeap->GetDesc().SizeInBytes,
                                     unsupportedBufferHeaps.count(heap.first) == 0, heap.second))
      {
        const D3D12_RESOURCE_DESC copyDesc =
            CheckpointQueryBufferDesc(component.end - component.begin);
        component.allocationBytes = GetReal()->GetResourceAllocationInfo(0, 1, &copyDesc).SizeInBytes;
        if(!component.allocationBytes || component.allocationBytes == ~0ULL ||
           component.allocationBytes > cache.budget ||
           bufferAliasBytes > cache.budget - component.allocationBytes)
          continue;
        const ResourceId key = *component.members.begin();
        cache.bufferAliasComponents[key] = component;
        for(ResourceId member : component.members)
          cache.bufferAliasMembers[member] = key;
        cache.residentVersions.Register(heap.first, 0, nativeHeap->GetDesc().SizeInBytes);
        bufferAliasBytes += component.allocationBytes;
      }
    }
  uint64_t typedEdges = 0;
  if(Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_CHECKPOINT_TYPED_ALIASES") != "1")
    for(const auto &heap : heaps)
    {
      WrappedID3D12Heap *nativeHeap = D3D12CheckpointOriginalHeap(GetResourceManager(), heap.first);
      if(!nativeHeap || unsupportedTypedHeaps.count(heap.first))
        continue;
      // Reuse only the geometry closure helper. Unlike buffer components these allocations are
      // never copied as raw heap bytes: every accepted point needs a complete typed owner proof.
      for(const auto &component : D3D12BufferAliasComponents(
              heap.first, nativeHeap->GetDesc().SizeInBytes, true, heap.second))
      {
        bool qualified = true;
        for(ResourceId member : component.members)
        {
          auto resource = GetResourceList().find(member);
          if(resource == GetResourceList().end())
          {
            qualified = false;
            break;
          }
          const auto desc = resource->second->GetDesc();
          const UINT allowed =
              D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET | D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL |
              D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS | D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE;
          qualified &= (desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D ||
                        desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D) &&
                       desc.Layout == D3D12_TEXTURE_LAYOUT_UNKNOWN && desc.SampleDesc.Count == 1 &&
                       desc.Format != DXGI_FORMAT_UNKNOWN && !(UINT(desc.Flags) & ~allowed) &&
                       !IsSparseResource(member) &&
                       !resource->second->IsAccelerationStructureResource() &&
                       !GetResourceManager()->HasReplacement(member) &&
                       GetResourceManager()->GetUnreplacedID(member) == member;
        }
        if(!qualified || cache.typedAliasConflicts.size() + component.members.size() > 4096)
          continue;
        std::map<ResourceId, std::set<ResourceId>> componentGraph;
        uint64_t componentEdges = 0;
        for(size_t i = 0; qualified && i < heap.second.size(); i++)
          if(component.members.count(heap.second[i].resource))
            for(size_t j = i + 1;
                j < heap.second.size() && heap.second[j].begin < heap.second[i].end; j++)
            {
              componentEdges += 2;
              if(typedEdges + componentEdges > 65536)
              {
                qualified = false;
                break;
              }
              componentGraph[heap.second[i].resource].insert(heap.second[j].resource);
              componentGraph[heap.second[j].resource].insert(heap.second[i].resource);
            }
        if(qualified)
        {
          cache.typedAliasConflicts.insert(componentGraph.begin(), componentGraph.end());
          typedEdges += componentEdges;
        }
      }
    }
  cache.directHeapAccess = false;
  for(WrappedID3D12PipelineState *pipe : *m_PipelineList)
    if(pipe->usedSig.Flags & D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED)
      cache.directHeapAccess = true;

  // Estimate complete point storage before selecting its stream boundary, including resources
  // whose last use is in the prefix. A later request must be able to save an affordable earlier
  // point on its first replay instead of discovering the nearest point is too large after passing
  // every useful fallback boundary. GetUsage is traversed once, not once per candidate.
  std::map<uint32_t, uint64_t> firstWriteBytes;
  for(const DescriptorStoreDescription &store : m_Replay->GetDescriptorStores())
    descriptorBytes += uint64_t(store.descriptorCount) * sizeof(D3D12Descriptor);
  for(const auto &entry : GetResourceList())
  {
    if(ResourceIDGen::IsReplayOnlyID(entry.first))
      continue;
    if(cache.bufferAliasMembers.count(entry.first) || cache.typedAliasConflicts.count(entry.first))
      continue;    // the entire physical component is saved, independent of shader usage
    uint32_t firstWrite = ~0U;
    for(const EventUsage &usage : m_Queue->GetUsage(entry.first))
      if(CheckpointResourceWrite(usage.usage))
        firstWrite = RDCMIN(firstWrite, usage.eventId);
    const D3D12_RESOURCE_DESC desc = entry.second->GetReal()->GetDesc();
    if((cache.directHeapAccess || cache.editedDependencies || cache.bufferEditFirstEvent != ~0U) &&
       (!IsReadOnlyResource(entry.first) ||
        (desc.Flags & (D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
                       D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET |
                       D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL))))
      firstWrite = RDCMIN(firstWrite,
                          D3D12CheckpointConservativeWriteStart(cache.directHeapAccess,
                                                                 cache.editedDependencies,
                                                                 cache.bufferEditFirstEvent));
    if(firstWrite == ~0U)
      continue;
    D3D12_HEAP_PROPERTIES properties = {};
    D3D12_HEAP_FLAGS flags = {};
    const bool propertiesKnown =
        SUCCEEDED(entry.second->GetReal()->GetHeapProperties(&properties, &flags));
    const uint64_t bytes = propertiesKnown && properties.Type == D3D12_HEAP_TYPE_UPLOAD
                               ? desc.Width
                               : GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
    if(bytes == ~0ULL || !propertiesKnown || IsSparseResource(entry.first) ||
       entry.second->IsAccelerationStructureResource() ||
       cache.overlappingPlaced.find(entry.first) != cache.overlappingPlaced.end() ||
       (properties.Type != D3D12_HEAP_TYPE_DEFAULT &&
        properties.Type != D3D12_HEAP_TYPE_UPLOAD && properties.Type != D3D12_HEAP_TYPE_READBACK) ||
       ((properties.Type == D3D12_HEAP_TYPE_UPLOAD || properties.Type == D3D12_HEAP_TYPE_READBACK) &&
        desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER))
    {
      if(firstWrite < firstUnsupportedWrite)
      {
        firstUnsupportedWrite = firstWrite;
        firstUnsupportedResource = entry.first;
        if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
        {
          // Keep the exact rejected storage and event uses beside the aggregate counts. An
          // overlapping-resource count cannot identify which heap/range blocked this point, and
          // a Discard is not evidence that the capture produced valid bytes at that event.
          rdcstr uses, aliases;
          uint32_t useCount = 0, aliasCount = 0;
          for(const EventUsage &usage : m_Queue->GetUsage(entry.first))
            if(useCount++ < 32)
              uses += StringFormat::Fmt("%s%u:%s", uses.empty() ? "" : ",", usage.eventId,
                                       ToStr(usage.usage).c_str());
          const ResourceId heapId = GetResID(entry.second->GetHeap());
          const uint64_t begin = entry.second->GetHeapOffset();
          auto heapRanges = heaps.find(heapId);
          if(heapRanges != heaps.end() && bytes != ~0ULL && begin + bytes >= begin)
            for(const PlacedRange &range : heapRanges->second)
              if(range.resource != entry.first && range.begin < begin + bytes && begin < range.end)
                if(aliasCount++ < 16)
                  aliases += StringFormat::Fmt("%s%s:%llu:%llu", aliases.empty() ? "" : ",",
                                              ToStr(range.resource).c_str(),
                                              (unsigned long long)range.begin,
                                              (unsigned long long)range.end);
          firstUnsupportedStorage = StringFormat::Fmt(
              "api=D3D12Checkpoint op=unsupportedStorage resource=%s firstWrite=%u heap=%s "
              "offset=%llu bytes=%llu overlap=%u sparse=%u accelerationStructure=%u "
              "propertiesKnown=%u heapType=%s dimension=%s useCount=%u uses=%s "
              "aliasCount=%u aliases=%s",
              ToStr(entry.first).c_str(), firstWrite, ToStr(heapId).c_str(),
              (unsigned long long)begin, (unsigned long long)bytes,
              cache.overlappingPlaced.count(entry.first) ? 1U : 0U,
              IsSparseResource(entry.first) ? 1U : 0U,
              entry.second->IsAccelerationStructureResource() ? 1U : 0U,
              propertiesKnown ? 1U : 0U, ToStr(properties.Type).c_str(),
              ToStr(desc.Dimension).c_str(), useCount, uses.c_str(), aliasCount, aliases.c_str());
        }
      }
    }
    else
      firstWriteBytes[firstWrite] += bytes;
  }
  // Every participating buffer alias is saved, even when no reflected write was reported.
  // Accounting is the union allocation once, not the sum of overlapping logical buffer sizes.
  const uint64_t fixedPointBytes = bufferAliasBytes;
  uint64_t prefixBytes = fixedPointBytes;
  for(auto &entry : firstWriteBytes)
  {
    prefixBytes += entry.second;
    entry.second = prefixBytes;
  }
  const auto completePointBytes = [&](const D3D12CommandData::CheckpointBoundary &boundary) {
    auto after = firstWriteBytes.upper_bound(boundary.eventId);
    const uint64_t resourceBytes =
        after == firstWriteBytes.begin() ? fixedPointBytes : (--after)->second;
    const uint64_t descriptorStorage =
        D3D12CheckpointDescriptorAdmissionBytes(descriptorBytes, boundary.fullDescriptorBytes,
                                                boundary.descriptorBytes, cache.editedDependencies);
    return resourceBytes > ~0ULL - descriptorStorage ? ~0ULL : resourceBytes + descriptorStorage;
  };

  D3D12CommandData &cmd = *m_Queue->GetCommandData();
  D3D12CommandData::CheckpointUnsupportedChunks continuationUnsupported;
  std::set<uint32_t> inspectedBoundaries;
  struct TypedOwnerProof
  {
    bool complete = false;
    std::set<ResourceId> owners;
    rdcstr reason;
  };
  std::map<uint32_t, TypedOwnerProof> typedProofs;
  for(const ReplayCheckpointCandidate &candidate : candidates)
  {
    if(cache.selected.size() >= 64)
      break;
    for(auto boundary = cmd.m_CheckpointBoundaries.end();
        boundary != cmd.m_CheckpointBoundaries.begin();)
    {
      --boundary;
      if(boundary->eventId > candidate.eventId || boundary->eventId < config.minEventGap)
        continue;
      if(boundary->eventId >= firstUnsupportedWrite || completePointBytes(*boundary) > cache.budget)
      {
        cache.stats.reason = boundary->eventId >= firstUnsupportedWrite
                                 ? "Later checkpoint prefix contains unsupported writable storage"
                                 : "Later checkpoint prefix exceeds the effective memory budget";
        cache.stats.rejected++;
        continue;
      }
      bool nearExisting = false;
      for(const auto &selected : cache.selected)
        if(RDCMAX(selected.first, boundary->eventId) - RDCMIN(selected.first, boundary->eventId) <
           config.minEventGap)
          nearExisting = true;
      if(nearExisting)
        continue;
      rdcstr reason;
      std::set<ResourceId> typedOwners;
      if(!cache.typedAliasConflicts.empty())
      {
        auto proof = typedProofs.find(boundary->eventId);
        if(proof == typedProofs.end())
        {
          // Candidate fallback can visit the same complete prefix many times. Memoize it and
          // bound cold planning; no drag/edit may initiate an unbounded whole-frame proof sweep.
          if(typedProofs.size() >= 32)
          {
            cache.stats.reason = "Texture alias candidate analysis budget exhausted";
            break;
          }
          TypedOwnerProof checked;
          checked.complete = cmd.GetReplayTextureAliasOwners(
              boundary->eventId, cache.typedAliasConflicts, checked.owners, checked.reason);
          proof = typedProofs.insert({boundary->eventId, std::move(checked)}).first;
        }
        if(!proof->second.complete)
        {
          cache.stats.reason = proof->second.reason;
          cache.stats.rejected++;
          continue;
        }
        typedOwners = proof->second.owners;
        cache.provedTypedBoundaries.insert(boundary->eventId);
      }
      uint64_t typedBytes = 0;
      for(ResourceId owner : typedOwners)
      {
        const auto desc = GetResourceList().find(owner)->second->GetDesc();
        const uint64_t bytes = GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
        if(!bytes || bytes == ~0ULL || bytes > cache.budget || typedBytes > cache.budget - bytes)
        {
          typedBytes = ~0ULL;
          break;
        }
        typedBytes += bytes;
      }
      if(typedBytes > cache.budget || completePointBytes(*boundary) > cache.budget - typedBytes)
      {
        cache.stats.reason = "Active texture alias owners exceed the effective memory budget";
        cache.stats.rejected++;
        continue;
      }
      inspectedBoundaries.insert(boundary->eventId);
      if(!cmd.IsCheckpointBoundarySafe(*boundary, reason, &continuationUnsupported))
      {
        cache.stats.reason = reason;
        cache.stats.rejected++;
        continue;
      }
      uint64_t queryBytes = 0;
      for(const auto &slot : boundary->queries.slots)
        queryBytes += GetResourceManager()
                          ->GetResAs<WrappedID3D12QueryHeap>(slot.first.first)
                          ->GetResolveDataSize();
      if(queryBytes)
      {
        const D3D12_RESOURCE_DESC desc = CheckpointQueryBufferDesc(queryBytes);
        queryBytes = GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
      }
      if(queryBytes > cache.budget - typedBytes ||
         completePointBytes(*boundary) > cache.budget - typedBytes - queryBytes)
      {
        cache.stats.reason = "Checkpoint query results exceed the effective memory budget";
        cache.stats.rejected++;
        continue;
      }
      cache.selected[boundary->eventId] = *boundary;
      cache.selectedTypedOwners[boundary->eventId] = typedOwners;
      cache.selectedBytes[boundary->eventId] =
          completePointBytes(*boundary) + typedBytes + queryBytes;
      if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      {
        const uint64_t descriptorStorage = D3D12CheckpointDescriptorAdmissionBytes(
            descriptorBytes, boundary->fullDescriptorBytes, boundary->descriptorBytes,
            cache.editedDependencies);
        const rdcstr line = StringFormat::Fmt(
            "api=D3D12Checkpoint op=admit event=%u descriptorBytes=%llu fullDescriptorBytes=%llu "
            "descriptorPlanKnown=%u estimatedBytes=%llu",
            boundary->eventId, (unsigned long long)descriptorStorage,
            (unsigned long long)descriptorBytes,
            !cache.editedDependencies && boundary->fullDescriptorBytes == descriptorBytes &&
                    boundary->descriptorBytes <= descriptorBytes
                ? 1U
                : 0U,
            (unsigned long long)cache.selectedBytes[boundary->eventId]);
        RDCLOG("[EIDTiming] %s", line.c_str());
        AppendReplayTimingLog(line);
      }
      break;
    }
  }
  if(cache.selected.empty() && cache.stats.reason.empty())
    cache.stats.reason =
        "No complete command-list submission boundary matches the requested modules";
  if(!continuationUnsupported.empty() && Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    rdcstr details;
    uint64_t sourceChunks = 0;
    for(const auto &entry : continuationUnsupported)
    {
      sourceChunks += entry.second.size();
      details += StringFormat::Fmt("%s%s:%zu@%llu", details.empty() ? "" : ",",
                                   ToStr(entry.first).c_str(), entry.second.size(),
                                   (unsigned long long)*entry.second.begin());
    }
    // One aggregate per configuration. Offsets are unique across overlapping candidate prefixes,
    // and every unsupported type is collected, so one replay diagnoses the whole whitelist gap.
    LogD3D12Checkpoint(StringFormat::Fmt(
        "api=D3D12Checkpoint op=continuationRejects inspectedBoundaries=%zu types=%zu "
        "sourceChunks=%llu details=%s",
        inspectedBoundaries.size(), continuationUnsupported.size(),
        (unsigned long long)sourceChunks, details.c_str()));
  }
  if(!retainSelectedCheckpoints())
    return;
  logConfiguration();
}

bool WrappedID3D12Device::IsReplayCheckpointAliasingSafe(ResourceId before, ResourceId after,
                                                        uint32_t boundaryEvent) const
{
  if(!m_ReplayCheckpoints)
    return false;
  const auto &overlap = m_ReplayCheckpoints->overlappingPlaced;
  const auto &bufferAliases = m_ReplayCheckpoints->bufferAliasMembers;
  const auto &typed = m_ReplayCheckpoints->typedAliasConflicts;
  const bool typedProven = m_ReplayCheckpoints->provedTypedBoundaries.count(boundaryEvent) != 0;
  const auto restorable = [&](ResourceId id) {
    return !m_SparseResources.count(id) && (!overlap.count(id) || bufferAliases.count(id) ||
                                            (typedProven && typed.count(id)));
  };
  // NULL-before with an explicit destination activates that destination's exact physical
  // overlap component. It is not NULL-after's unknown destination. The completed owner proof
  // for this exact boundary already checked every activation/init/access in the prefix.
  if(before == ResourceId() && !m_SparseResources.count(after) &&
     D3D12ProvedTypedAliasActivation(typedProven, after, typed))
    return true;
  // Wildcards may include any storage. Only independent allocations and completely covered
  // buffer alias components have a restoration proof; sparse/texture aliases retain fallback.
  if(before == ResourceId() || after == ResourceId())
  {
    if(!m_SparseResources.empty())
      return false;
    for(ResourceId id : overlap)
      if(!bufferAliases.count(id))
        return false;
    return true;
  }
  return restorable(before) && restorable(after);
}

void WrappedID3D12Device::InvalidateReplayCheckpoints(uint32_t firstAffectedEvent)
{
  if(!m_ReplayCheckpoints || HasFatalError())
    return;
  D3D12ReplayCheckpointCache &cache = *m_ReplayCheckpoints;
  cache.residentVersions.InvalidateAll();
  // Both texture/buffer copies and query sources may still be read by the last restored suffix.
  // A preceding patch/shader transaction's completed device drain can be reused here.
  const bool removingSources =
      !cache.currentVersions.empty() || !cache.currentBufferAliases.empty() ||
      !cache.activeQueries.empty() ||
      cache.checkpoints.lower_bound(firstAffectedEvent) != cache.checkpoints.end();
  if(removingSources && !ReplayWorkWaitForIdle())
    return;
  if(!cache.activeQueries.empty())
  {
    cache.activeQueryResolves.clear();
    cache.activeQueries.clear();
  }
  cache.currentVersions.clear();
  cache.currentBufferAliases.clear();
  for(auto it = cache.checkpoints.begin(); it != cache.checkpoints.end();)
  {
    if(firstAffectedEvent == 0 || it->first >= firstAffectedEvent)
    {
      it = cache.checkpoints.erase(it);
      cache.stats.invalidations++;
    }
    else
      ++it;
  }
  if(cache.checkpoints.empty())
    cache.stats.sharedBytes = 0;
}

ReplayCheckpointStats WrappedID3D12Device::GetReplayCheckpointStats()
{
  if(!m_ReplayCheckpoints)
  {
    ReplayCheckpointStats stats;
    stats.supported = true;
    return stats;
  }
  auto &cache = *m_ReplayCheckpoints;
  cache.stats.events.clear();
  cache.stats.candidatePlanKnown = true;
  cache.stats.canTryOtherCandidates = cache.config.enabled && cache.budget > 0;
  cache.stats.memoryBudgetBlocked = cache.config.enabled && cache.memoryBudgetBlocked;
  cache.stats.candidateEvents.clear();
  for(const auto &entry : cache.selected)
    cache.stats.candidateEvents.push_back(entry.first);
  for(const auto &entry : cache.checkpoints)
    cache.stats.events.push_back(entry.first);
  cache.stats.checkpointCount = (uint32_t)cache.checkpoints.size();
  cache.stats.allocatedBytes = cache.AllocatedBytes();
  if(cache.checkpoints.empty())
    cache.stats.sharedBytes = 0;
  return cache.stats;
}

bool WrappedID3D12Device::RefreshReplayCheckpointMemoryBudget()
{
  // This optional entry is invoked only by bounded idle maintenance. It never waits for the GPU,
  // creates a resource, changes an existing allocation, or bypasses native candidate admission.
  if(HasFatalError() || !m_ReplayCheckpoints)
    return false;
  auto &cache = *m_ReplayCheckpoints;
  if(!cache.config.enabled || !cache.memoryBudgetBlocked || cache.budget ||
     !cache.checkpoints.empty())
    return false;
  DXGI_QUERY_VIDEO_MEMORY_INFO memory = {};
  const bool known = D3D12QueryCheckpointMemory(GetReal(), memory);
  const bool recovered = D3D12RecoverCheckpointBudget(
      cache.config.enabled, !cache.checkpoints.empty(), cache.config.byteBudget, known, memory,
      cache.memoryBudgetBlocked, cache.budget);
  if(recovered)
    cache.stats.reason.clear();
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    LogD3D12Checkpoint(StringFormat::Fmt(
        "api=D3D12Checkpoint op=memoryProbe known=%u recovered=%u requestedBytes=%llu "
        "effectiveBytes=%llu videoBudget=%llu videoUsage=%llu",
        known ? 1U : 0U, recovered ? 1U : 0U, (unsigned long long)cache.config.byteBudget,
        (unsigned long long)cache.budget, (unsigned long long)memory.Budget,
        (unsigned long long)memory.CurrentUsage));
  return recovered;
}

void WrappedID3D12Device::GetReplayCheckpointQueryResult(
    ResourceId baked, uint32_t relativeEvent, ResourceId heap, uint32_t index,
    ID3D12Resource *&resource, UINT64 &offset) const
{
  resource = NULL;
  offset = 0;
  if(!m_ReplayCheckpoints || baked == ResourceId())
    return;
  const auto &cache = *m_ReplayCheckpoints;
  if(cache.activeQueryResolves.empty())
    return;
  const D3D12CheckpointQuerySlot slot = {heap, index};
  const D3D12CheckpointQueryResolve resolve = {{baked, relativeEvent}, slot};
  if(cache.activeQueryResolves.find(resolve) == cache.activeQueryResolves.end())
    return;
  const auto saved = cache.activeQueries.find(slot);
  RDCASSERT(saved != cache.activeQueries.end());
  if(saved != cache.activeQueries.end())
  {
    resource = saved->second.data->gpu;
    offset = saved->second.offset;
  }
}

void WrappedID3D12Device::SaveReplayCheckpoint(uint32_t eventID, uint64_t offset,
                                              uint32_t nextEvent, uint32_t nextAction)
{
  if(HasFatalError() || !m_ReplayCheckpoints || m_Replay->m_ReplayCheckpointBypass ||
     m_Queue->GetCommandData()->m_ActionCallback || m_ReplayOptions.apiValidation ||
     !m_ReplayCheckpoints->config.enabled)
    return;
  auto &cache = *m_ReplayCheckpoints;
  // A new checkpoint must not snapshot intentionally unmaterialised initial bytes. Subsequent
  // requests can build it after a canonical reset; existing checkpoints remain usable now.
  if(cache.savedThisReplay || !cache.deferredInitialResources.empty())
    return;
  const uint32_t saveBefore = m_Queue->GetCommandData()->m_LastEventID;
  uint32_t firstConsumer = 0;
  if(!m_ReplayBufferPatches.empty() && m_ReplayBufferPatchFirstConsumer > 1 &&
     m_ReplayBufferPatchFirstConsumer != ~0U && !m_CheckpointDependenciesEdited &&
     Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_BUFFER_PATCH_PREFIX") != "1")
    firstConsumer = m_ReplayBufferPatchFirstConsumer;
  const uint32_t targetEvent = RDCMAX(cache.targetEvent, m_Queue->GetCommandData()->m_LastEventID);
  const bool prefixOnly = m_Replay->m_ReplayCheckpointPrefixOnly && !m_ReplayBufferPatches.empty();
  if(D3D12MissingCheckpointToSave(cache, eventID, saveBefore, targetEvent, firstConsumer,
                                 prefixOnly) != eventID)
    return;
  const uint32_t protectedAnchor =
      D3D12CheckpointBufferAnchor(cache, firstConsumer, firstConsumer);
  auto selected = cache.selected.find(eventID);
  if(selected == cache.selected.end() || selected->second.offset != offset ||
     cache.checkpoints.find(eventID) != cache.checkpoints.end())
    return;

  PerformanceTimer totalTimer;
  const auto reject = [&](const rdcstr &reason) {
    cache.stats.rejected++;
    cache.stats.reason = reason;
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      LogD3D12Checkpoint(StringFormat::Fmt(
          "api=D3D12Checkpoint op=reject event=%u effectiveBytes=%llu reason=%s", eventID,
          (unsigned long long)cache.budget, reason.c_str()));
    // Do not repeatedly allocate an unaffordable or unsupported point on every mouse movement.
    cache.selected.erase(eventID);
  };
  D3D12ReplayCheckpoint checkpoint;
  checkpoint.eventId = eventID;
  checkpoint.nextEvent = nextEvent;
  // Active replay does not rebuild the action tree or reset its action counter like loading
  // does. Resume from the authoritative loading-time counter, not a previous replay's residue.
  checkpoint.nextAction = selected->second.nextAction;
  checkpoint.firstNativeResolveEID = selected->second.firstNativeResolveEID;
  checkpoint.firstNativeResolveType = selected->second.firstNativeResolveType;
  checkpoint.queryResolves = selected->second.queries.resolves;
  (void)nextAction;
  checkpoint.offset = offset;
  checkpoint.lastUse = ++cache.serial;
  const auto typedOwners = cache.selectedTypedOwners.find(eventID);
  if(!cache.typedAliasConflicts.empty() && typedOwners == cache.selectedTypedOwners.end())
  {
    reject("Checkpoint texture alias owner evidence is missing");
    return;
  }
  if(typedOwners != cache.selectedTypedOwners.end())
    checkpoint.typedAliasOwners = typedOwners->second;
  for(const auto &entry : cache.typedAliasConflicts)
    checkpoint.typedAliasMembers.insert(entry.first);

  struct PendingResource
  {
    WrappedID3D12Resource *resource;
    uint32_t version;
    uint64_t bytes;
    bool cpu;
    bool readback;
  };
  rdcarray<PendingResource> pending;
  rdcarray<ResourceId> pendingBufferAliases;
  uint64_t newBytes = 0, sharedBytes = 0;
  uint64_t descriptorBytes = 0, fullDescriptorBytes = 0, descriptorRanges = 0;
  uint32_t descriptorDeltaHeaps = 0;
  struct PendingQuery
  {
    D3D12CheckpointQuerySlot slot;
    D3D12CheckpointQueryVersion version;
    uint64_t offset;
  };
  rdcarray<PendingQuery> pendingQueries;
  uint64_t queryDataBytes = 0, queryAllocationBytes = 0;
  std::set<const void *> sharedQueryData;
  for(const auto &entry : selected->second.queries.slots)
  {
    WrappedID3D12QueryHeap *heap =
        GetResourceManager()->GetResAs<WrappedID3D12QueryHeap>(entry.first.first);
    if(!heap || GetResourceManager()->HasReplacement(entry.first.first) ||
       !heap->CanCheckpointQuery(entry.first.second, entry.second.type))
    {
      reject("Completed checkpoint query slot identity changed");
      return;
    }
    const auto previous = cache.activeQueries.find(entry.first);
    if(previous != cache.activeQueries.end() &&
       previous->second.version.endEvent == entry.second.endEvent &&
       previous->second.version.type == entry.second.type)
    {
      // A resumed prefix has no corresponding native query End in this replay. Share its real
      // saved result; resolving the untouched native heap here would capture a different value.
      checkpoint.queries[entry.first] = previous->second;
      if(sharedQueryData.insert(previous->second.data.get()).second)
        sharedBytes += previous->second.data->bytes;
    }
    else
    {
      pendingQueries.push_back({entry.first, entry.second, queryDataBytes});
      queryDataBytes += heap->GetResolveDataSize();
    }
  }
  if(queryDataBytes)
  {
    const D3D12_RESOURCE_DESC desc = CheckpointQueryBufferDesc(queryDataBytes);
    queryAllocationBytes = GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
    if(queryAllocationBytes > cache.budget)
    {
      reject("Completed query result storage exceeds the configured memory budget");
      return;
    }
    newBytes += queryAllocationBytes;
  }
  for(const auto &entry : cache.bufferAliasComponents)
  {
    const auto &component = entry.second;
    WrappedID3D12Heap *nativeHeap = D3D12CheckpointOriginalHeap(GetResourceManager(), component.heap);
    if(!nativeHeap || !nativeHeap->GetUnwrappedWholeMemBuffer())
    {
      reject("Checkpoint physical buffer heap identity changed");
      return;
    }
    for(ResourceId member : component.members)
    {
      const auto resource = GetResourceList().find(member);
      if(resource == GetResourceList().end() || !resource->second->IsResident() ||
         GetResourceManager()->HasReplacement(member) ||
         GetResourceManager()->GetUnreplacedID(member) != member)
      {
        reject("Checkpoint buffer alias member is absent, replaced, or non-resident");
        return;
      }
    }
    const auto previous = cache.currentBufferAliases.find(entry.first);
    if(previous != cache.currentBufferAliases.end() &&
       D3D12MayShareBufferAliasVersion(
           IsReplayDirtyTrackingValid(), m_ReplayDirtyRecordingAllowed,
           cache.residentVersions.IsResident(component.Range(), previous->second->version)))
    {
      checkpoint.bufferAliases[entry.first] = previous->second;
      sharedBytes += component.allocationBytes;
      continue;
    }
    if(component.allocationBytes > cache.budget || newBytes > cache.budget - component.allocationBytes)
    {
      reject("Checkpoint buffer alias union exceeds the configured memory budget");
      return;
    }
    newBytes += component.allocationBytes;
    pendingBufferAliases.push_back(entry.first);
  }
  for(const auto &entry : GetResourceList())
  {
    if(ResourceIDGen::IsReplayOnlyID(entry.first))
      continue;
    WrappedID3D12Resource *resource = entry.second;
    if(GetResourceManager()->HasReplacement(entry.first))
    {
      reject("A captured resource replacement needs checkpoint data rebinding support");
      return;
    }
    const auto states = m_ResourceStates.find(entry.first);
    if(states == m_ResourceStates.end())
    {
      reject("Captured resource has no tracked barrier state");
      return;
    }
    for(const auto &state : states->second)
      if(!state.IsStates())
      {
        reject("Enhanced initial resource layouts require additional checkpoint state");
        return;
      }
    checkpoint.states[entry.first] = states->second;

    const bool typedAlias = checkpoint.typedAliasMembers.count(entry.first) != 0;
    if(typedAlias && !checkpoint.typedAliasOwners.count(entry.first))
    {
      const auto initial = m_InitialResourceStates.find(entry.first);
      if(initial == m_InitialResourceStates.end() ||
         !D3D12CheckpointStatesEqual(initial->second, states->second))
      {
        reject("Inactive texture alias needs a state transition after restoring another owner");
        return;
      }
      continue;    // undefined inactive contents are not copied, and require no restore transition
    }
    if(typedAlias &&
       (GetResourceManager()->GetUnreplacedID(entry.first) != entry.first ||
        !D3D12CheckpointOriginalHeap(GetResourceManager(), GetResID(resource->GetHeap()))))
    {
      reject("Checkpoint texture alias identity changed");
      return;
    }

    if(cache.bufferAliasMembers.count(entry.first))
      continue;    // the component owns one immutable physical copy for every overlapping view

    uint32_t version = 0;
    for(const EventUsage &usage : m_Queue->GetUsage(entry.first))
      if(usage.eventId <= eventID && CheckpointResourceWrite(usage.usage))
        version = RDCMAX(version, usage.eventId);
    const D3D12_RESOURCE_DESC desc = resource->GetReal()->GetDesc();
    if(typedAlias)
      version = eventID;    // snapshot every proved owner, independent of GetUsage or dirty tracking
    // Direct heap indexing can omit precise per-resource usage. Save every writable candidate
    // in that case, rather than silently treating an unreported UAV as immutable.
    if(eventID >= D3D12CheckpointConservativeWriteStart(cache.directHeapAccess,
                                                        cache.editedDependencies,
                                                        cache.bufferEditFirstEvent) &&
       (!IsReadOnlyResource(entry.first) ||
        (desc.Flags & (D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
                       D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET |
                       D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL))))
      version = eventID;
    if(!version)
    {
      // These bytes come from the shared capture initial-state source during restore.
      const uint64_t bytes = desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER
                                 ? desc.Width
                                 : GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
      if(bytes != ~0ULL)
        sharedBytes += bytes;
      continue;
    }
    if(IsSparseResource(entry.first) || resource->IsAccelerationStructureResource() ||
       (cache.overlappingPlaced.find(entry.first) != cache.overlappingPlaced.end() && !typedAlias))
    {
      reject("A prefix-written resource has sparse, acceleration-structure, or overlapping placed storage");
      return;
    }
    if(!resource->IsResident())
    {
      reject("A checkpoint copy resource is currently non-resident");
      return;
    }
    auto previous = cache.currentVersions.find(entry.first);
    if(!typedAlias && previous != cache.currentVersions.end() && previous->second->version == version)
    {
      checkpoint.resources[entry.first] = previous->second;
      sharedBytes += previous->second->bytes;
      continue;
    }
    D3D12_HEAP_PROPERTIES properties = {};
    D3D12_HEAP_FLAGS flags;
    if(FAILED(resource->GetReal()->GetHeapProperties(&properties, &flags)))
    {
      reject("Cannot query checkpoint resource heap properties");
      return;
    }
    const bool cpu = properties.Type == D3D12_HEAP_TYPE_UPLOAD;
    const bool readback = properties.Type == D3D12_HEAP_TYPE_READBACK;
    if(properties.Type != D3D12_HEAP_TYPE_DEFAULT && !cpu && !readback)
    {
      reject("Prefix-written custom heaps are not supported by this checkpoint path");
      return;
    }
    if((cpu || readback) && desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
    {
      reject("Mapped texture checkpoint storage is not supported");
      return;
    }
    uint64_t bytes = cpu ? desc.Width : GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
    if(bytes == ~0ULL || bytes > cache.budget || newBytes > cache.budget - bytes)
    {
      reject("Checkpoint writable-resource storage exceeds the configured memory budget");
      return;
    }
    newBytes += bytes;
    pending.push_back({resource, version, bytes, cpu, readback});
  }
  for(const DescriptorStoreDescription &store : m_Replay->GetDescriptorStores())
  {
    WrappedID3D12DescriptorHeap *heap =
        GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(store.resourceId);
    if(!heap)
    {
      reject("Captured descriptor heap is unavailable");
      return;
    }
    ResourceId baseline;
    rdcarray<D3D12DescriptorRestoreRange> ranges;
    if(!heap->GetCheckpointDescriptorRanges(baseline, ranges))
      ranges.push_back({0, heap->GetNumDescriptors()});
    else
      descriptorDeltaHeaps++;
    uint64_t descriptorCount = 0;
    for(const D3D12DescriptorRestoreRange &range : ranges)
      descriptorCount += range.count;
    const uint64_t bytes = descriptorCount * sizeof(D3D12Descriptor);
    descriptorBytes += bytes;
    fullDescriptorBytes += uint64_t(heap->GetNumDescriptors()) * sizeof(D3D12Descriptor);
    descriptorRanges += ranges.size();
    std::shared_ptr<D3D12ReplayCheckpointHeap> saved;
    for(const auto &old : cache.checkpoints)
    {
      auto found = old.second.heaps.find(store.resourceId);
      if(found == old.second.heaps.end())
        continue;
      const D3D12ReplayCheckpointHeap &previous = *found->second;
      if(previous.baseline != baseline || previous.descriptorCount != heap->GetNumDescriptors() ||
         previous.type != heap->GetDesc().Type || previous.ranges.size() != ranges.size() ||
         previous.descriptors.byteSize() != bytes)
        continue;
      bool equal = true;
      size_t packed = 0;
      for(size_t r = 0; r < ranges.size() && equal; r++)
      {
        const D3D12DescriptorRestoreRange &range = ranges[r];
        equal = previous.ranges[r].first == range.first && previous.ranges[r].count == range.count;
        if(equal && range.count)
          equal = !memcmp(previous.descriptors.data() + packed,
                          heap->GetDescriptors() + range.first,
                          size_t(range.count) * sizeof(D3D12Descriptor));
        packed += range.count;
      }
      if(equal)
      {
        saved = found->second;
        sharedBytes += bytes;
        break;
      }
    }
    if(!saved)
    {
      if(bytes > cache.budget || newBytes > cache.budget - bytes)
      {
        reject("Checkpoint descriptor storage exceeds the configured memory budget");
        return;
      }
      newBytes += bytes;
      saved.reset(new D3D12ReplayCheckpointHeap);
      saved->id = store.resourceId;
      saved->baseline = baseline;
      saved->descriptorCount = heap->GetNumDescriptors();
      saved->type = heap->GetDesc().Type;
      saved->ranges = ranges;
      saved->descriptors.reserve((size_t)descriptorCount);
      for(const D3D12DescriptorRestoreRange &range : ranges)
        saved->descriptors.append(heap->GetDescriptors() + range.first, range.count);
    }
    checkpoint.heaps[store.resourceId] = saved;
  }
  uint64_t pendingBytes = queryAllocationBytes;
  for(const PendingResource &resource : pending)
    pendingBytes += resource.bytes;
  for(ResourceId key : pendingBufferAliases)
    pendingBytes += cache.bufferAliasComponents[key].allocationBytes;
  // Versions copied into the candidate above remain owned there. Drop obsolete per-replay
  // references before enforcing the physical budget and retiring LRU checkpoints.
  cache.currentVersions.clear();
  cache.currentBufferAliases.clear();
  std::set<uint32_t> evictionPlan;
  if(!D3D12PlanCheckpointEvictions(cache, checkpoint, pendingBytes, protectedAnchor, evictionPlan))
  {
    reject(protectedAnchor ? "Checkpoint budget cannot retain the shared-buffer prefix and new point"
                           : "Checkpoint set has exhausted its shared memory budget");
    return;
  }

  const double prepareMS = totalTimer.GetMilliseconds();
  PerformanceTimer timer;
  m_Queue->SyncLastReplayQueueToInternal();
  if(!DeviceWaitForIdle() || HasFatalError())
    return;
  // Planning is side-effect free. Retire source resources only after prior queue work completes,
  // and never evict the reusable pre-consumer anchor merely to make a downstream point fit.
  for(uint32_t victim : evictionPlan)
    cache.checkpoints.erase(victim);
  // Allocate every copy before recording any commands, so an allocation failure leaves the
  // original replay state untouched and can return to normal full replay without GPU rollback.
  for(ResourceId key : pendingBufferAliases)
  {
    auto saved = std::make_shared<D3D12ReplayCheckpointBufferAliases>();
    saved->component = cache.bufferAliasComponents[key];
    saved->version = {++cache.contentSerial, cache.residentVersions.EditGeneration()};
    const D3D12_RESOURCE_DESC desc =
        CheckpointQueryBufferDesc(saved->component.end - saved->component.begin);
    D3D12_HEAP_PROPERTIES properties = {};
    properties.Type = D3D12_HEAP_TYPE_DEFAULT;
    properties.CreationNodeMask = properties.VisibleNodeMask = 1;
    const HRESULT hr = GetReal()->CreateCommittedResource(
        &properties, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
        __uuidof(ID3D12Resource), (void **)&saved->gpu);
    if(FAILED(hr))
    {
      reject("GPU allocation for buffer alias checkpoint failed");
      return;
    }
    checkpoint.bufferAliases[key] = saved;
  }
  for(const PendingResource &item : pending)
  {
    auto saved = std::make_shared<D3D12ReplayCheckpointResource>();
    saved->id = GetResID(item.resource);
    saved->version = item.version;
    saved->contentVersion = {++cache.contentSerial, cache.residentVersions.EditGeneration()};
    saved->bytes = item.bytes;
    const D3D12_RESOURCE_DESC residentDesc = item.resource->GetReal()->GetDesc();
    const uint64_t extent = residentDesc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER
                                ? residentDesc.Width : 1;
    cache.residentExtents[saved->id] = extent;
    cache.residentVersions.Register(saved->id, 0, extent);
    if(item.cpu)
    {
      const uint64_t width = item.resource->GetReal()->GetDesc().Width;
      void *mapped = NULL;
      D3D12_RANGE range = {0, (SIZE_T)width};
      HRESULT hr = item.resource->GetReal()->Map(0, &range, &mapped);
      if(FAILED(hr) || !mapped)
      {
        reject("Cannot map the CPU upload buffer for a checkpoint");
        return;
      }
      saved->cpu.assign((byte *)mapped, (size_t)width);
      range = {0, 0};
      item.resource->GetReal()->Unmap(0, &range);
    }
    else
    {
      D3D12_HEAP_PROPERTIES properties = {};
      properties.Type = item.readback ? D3D12_HEAP_TYPE_UPLOAD : D3D12_HEAP_TYPE_DEFAULT;
      properties.CreationNodeMask = properties.VisibleNodeMask = 1;
      D3D12_RESOURCE_DESC desc = item.resource->GetReal()->GetDesc();
      if(item.readback)
        desc.Flags = D3D12_RESOURCE_FLAG_NONE;
      HRESULT hr = GetReal()->CreateCommittedResource(
          &properties, D3D12_HEAP_FLAG_NONE, &desc,
          item.readback ? D3D12_RESOURCE_STATE_GENERIC_READ : D3D12_RESOURCE_STATE_COPY_DEST, NULL,
          __uuidof(ID3D12Resource), (void **)&saved->gpu);
      if(FAILED(hr))
      {
        reject("GPU allocation for checkpoint failed; full replay remains available");
        return;
      }
      if(item.readback)
      {
        void *source = NULL, *destination = NULL;
        D3D12_RANGE read = {0, (SIZE_T)desc.Width}, noRead = {0, 0};
        HRESULT sourceHR = item.resource->GetReal()->Map(0, &read, &source);
        HRESULT destinationHR = saved->gpu->Map(0, &noRead, &destination);
        if(SUCCEEDED(sourceHR) && SUCCEEDED(destinationHR) && source && destination)
          memcpy(destination, source, (size_t)desc.Width);
        if(SUCCEEDED(sourceHR))
          item.resource->GetReal()->Unmap(0, &noRead);
        if(SUCCEEDED(destinationHR))
          saved->gpu->Unmap(0, &read);
        if(FAILED(sourceHR) || FAILED(destinationHR) || !source || !destination)
        {
          reject("Cannot save checkpoint READBACK buffer contents");
          return;
        }
      }
    }
    checkpoint.resources[saved->id] = saved;
  }
  std::shared_ptr<D3D12ReplayCheckpointQueryData> newQueryData;
  if(queryDataBytes)
  {
    newQueryData.reset(new D3D12ReplayCheckpointQueryData);
    newQueryData->bytes = queryAllocationBytes;
    D3D12_HEAP_PROPERTIES properties = {};
    properties.Type = D3D12_HEAP_TYPE_DEFAULT;
    properties.CreationNodeMask = properties.VisibleNodeMask = 1;
    const D3D12_RESOURCE_DESC desc = CheckpointQueryBufferDesc(queryDataBytes);
    const HRESULT hr = GetReal()->CreateCommittedResource(
        &properties, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COPY_DEST, NULL,
        __uuidof(ID3D12Resource), (void **)&newQueryData->gpu);
    if(FAILED(hr))
    {
      reject("GPU allocation for completed query results failed");
      return;
    }
    for(const PendingQuery &query : pendingQueries)
      checkpoint.queries[query.slot] = {newQueryData, query.offset, query.version};
  }
  ID3D12GraphicsCommandListX *list = GetNewList();
  if(!list)
    return;
  for(const PendingQuery &query : pendingQueries)
  {
    WrappedID3D12QueryHeap *heap =
        GetResourceManager()->GetResAs<WrappedID3D12QueryHeap>(query.slot.first);
    Unwrap(list)->ResolveQueryData(heap->GetReal(), query.version.type, query.slot.second, 1,
                                   newQueryData->gpu, query.offset);
  }
  if(newQueryData)
  {
    D3D12_RESOURCE_BARRIER ready = {};
    ready.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    ready.Transition.pResource = newQueryData->gpu;
    ready.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    ready.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    ready.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    Unwrap(list)->ResourceBarrier(1, &ready);
  }
  for(ResourceId key : pendingBufferAliases)
  {
    const auto &saved = checkpoint.bufferAliases[key];
    WrappedID3D12Heap *heap =
        GetResourceManager()->GetResAs<WrappedID3D12Heap>(saved->component.heap);
    D3D12BufferAliasCopy(Unwrap(list), heap->GetUnwrappedWholeMemBuffer(), *saved, false);
    D3D12_RESOURCE_BARRIER ready = {};
    ready.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    ready.Transition.pResource = saved->gpu;
    ready.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    ready.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    ready.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    Unwrap(list)->ResourceBarrier(1, &ready);
  }
  for(const PendingResource &item : pending)
  {
    const auto &saved = checkpoint.resources[GetResID(item.resource)];
    if(!saved->gpu || item.readback)
      continue;
    BarrierSet barriers;
    barriers.Configure(item.resource, checkpoint.states[saved->id], BarrierSet::CopySourceAccess);
    barriers.Apply(list);
    Unwrap(list)->CopyResource(saved->gpu, item.resource->GetReal());
    barriers.Unapply(list);
    D3D12_RESOURCE_BARRIER ready = {};
    ready.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    ready.Transition.pResource = saved->gpu;
    ready.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    ready.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    ready.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    Unwrap(list)->ResourceBarrier(1, &ready);
  }
  const HRESULT closeResult = list->Close();
  CHECK_HR(this, closeResult);
  if(FAILED(closeResult))
  {
    cache.unconfirmedSave = std::move(checkpoint);
    InvalidateReplayDirtyTracking("checkpointSaveCloseFailed");
    RDResult failure;
    SET_ERROR_RESULT(failure, ResultCode::APIReplayFailed,
                     "D3D12 checkpoint save command list failed to close: %s",
                     ToStr(closeResult).c_str());
    ReportFatalError(failure);
    return;
  }
  ExecuteLists();
  if(!FlushLists(true) || HasFatalError())
  {
    cache.unconfirmedSave = std::move(checkpoint);
    return;
  }
  cache.currentVersions = checkpoint.resources;
  cache.currentBufferAliases = checkpoint.bufferAliases;
  cache.checkpoints[eventID] = std::move(checkpoint);
  cache.PublishResidentVersions(cache.checkpoints[eventID]);
  cache.stats.builds++;
  cache.stats.lastSaveEvent = eventID;
  cache.savedThisReplay = true;
  cache.stats.sharedBytes = sharedBytes;
  cache.stats.reason.clear();
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    uint64_t aliasCopied = 0, aliasShared = 0;
    for(const auto &entry : cache.checkpoints[eventID].bufferAliases)
    {
      const uint64_t bytes = entry.second->component.end - entry.second->component.begin;
      if(pendingBufferAliases.contains(entry.first))
        aliasCopied += bytes;
      else
        aliasShared += bytes;
    }
    LogD3D12Checkpoint(StringFormat::Fmt(
        "api=D3D12Checkpoint op=save event=%u bytes=%llu sharedBytes=%llu total_ms=%.3f "
        "prepare_ms=%.3f copyPhase_ms=%.3f descriptorBytes=%llu fullDescriptorBytes=%llu "
        "descriptorRanges=%llu descriptorDeltaHeaps=%u firstResolve=%u queryType=%s "
        "savedQuerySlots=%zu queryDataBytes=%llu queryAllocationBytes=%llu querySavedResolves=%zu "
        "protectedAnchor=%u evictedPoints=%zu bufferAliasComponents=%zu "
        "bufferAliasCopyBytes=%llu bufferAliasSharedBytes=%llu typedAliasOwners=%zu "
        "typedAliasMembers=%zu",
        eventID, (unsigned long long)newBytes, (unsigned long long)sharedBytes,
        totalTimer.GetMilliseconds(), prepareMS, timer.GetMilliseconds(),
        (unsigned long long)descriptorBytes, (unsigned long long)fullDescriptorBytes,
        (unsigned long long)descriptorRanges, descriptorDeltaHeaps,
        selected->second.firstNativeResolveEID,
        ToStr(selected->second.firstNativeResolveType).c_str(), selected->second.queries.slots.size(),
        (unsigned long long)queryDataBytes, (unsigned long long)queryAllocationBytes,
        selected->second.queries.resolves.size(), protectedAnchor, evictionPlan.size(),
        cache.checkpoints[eventID].bufferAliases.size(), (unsigned long long)aliasCopied,
        (unsigned long long)aliasShared, cache.checkpoints[eventID].typedAliasOwners.size(),
        cache.checkpoints[eventID].typedAliasMembers.size()));
  }
}

static void CheckpointInitialCopyRanges(
    uint64_t byteCount, const rdcarray<rdcpair<uint64_t, uint64_t>> &exclusions,
    rdcarray<rdcpair<uint64_t, uint64_t>> &ranges)
{
  ranges.clear();
  uint64_t cursor = 0;
  // Exclusions are sorted and bounds-checked when the plan is prepared. Overlap/adjacency is
  // harmless here: each byte outside their union is copied exactly once from the initial source.
  for(const auto &excluded : exclusions)
  {
    const uint64_t begin = RDCMIN(excluded.first, byteCount);
    const uint64_t end = RDCMIN(excluded.second, byteCount);
    if(begin > cursor)
      ranges.push_back({cursor, begin});
    cursor = RDCMAX(cursor, end);
  }
  if(cursor < byteCount)
    ranges.push_back({cursor, byteCount});
}

#if ENABLED(ENABLE_UNIT_TESTS)
#include "catch/catch.hpp"

TEST_CASE("D3D12 checkpoint descriptor admission uses only the matching captured boundary",
          "[d3d12][checkpoint][memory][descriptor-restore]")
{
  const uint64_t mib = 1024ULL * 1024;
  const uint64_t full = 600 * mib, early = 64 * 1024, late = 2 * mib;
  const uint64_t resources = 96 * mib, budget = 512 * mib;
  CHECK(resources + full > budget);
  CHECK(resources + D3D12CheckpointDescriptorAdmissionBytes(full, full, early, false) < budget);
  CHECK(D3D12CheckpointDescriptorAdmissionBytes(full, full, late, false) == late);
  // Future writes are counted at their own boundary. A changed heap topology, shader/resource
  // replacement, or absent/malformed evidence always retains the complete admission charge.
  CHECK(D3D12CheckpointDescriptorAdmissionBytes(full, full - 1, early, false) == full);
  CHECK(D3D12CheckpointDescriptorAdmissionBytes(full, full, early, true) == full);
  CHECK(D3D12CheckpointDescriptorAdmissionBytes(full, ~0ULL, ~0ULL, false) == full);
  CHECK(D3D12CheckpointDescriptorAdmissionBytes(full, full, full + 1, false) == full);
  CHECK(D3D12CheckpointDescriptorAdmissionBytes(full, full, 0, false) == 0);
  CHECK(D3D12CheckpointDescriptorAdmissionBytes(full, ~0ULL, 0, false) == full);

  uint64_t work = 0;
  CHECK(D3D12ConsumeCheckpointDescriptorPlanWork(work, 999999));
  CHECK(D3D12ConsumeCheckpointDescriptorPlanWork(work, 0));
  CHECK(work == 1000000);
  CHECK_FALSE(D3D12ConsumeCheckpointDescriptorPlanWork(work, 1));
  CHECK_FALSE(D3D12ConsumeCheckpointDescriptorPlanWork(work, 0));
  work = 0;
  CHECK_FALSE(D3D12ConsumeCheckpointDescriptorPlanWork(work, ~0ULL));
  CHECK_FALSE(D3D12ConsumeCheckpointDescriptorPlanWork(work, 1));
}

TEST_CASE("D3D12 idle memory recovery requires a measured positive budget and no stored point",
          "[d3d12][checkpoint][memory]")
{
  const uint64_t mib = 1024ULL * 1024;
  DXGI_QUERY_VIDEO_MEMORY_INFO memory = {};
  memory.Budget = 4096 * mib;
  memory.CurrentUsage = 4096 * mib;
  CHECK(D3D12CheckpointHeadroom(512 * mib, memory) == 0);
  memory.CurrentUsage = 5000 * mib;
  CHECK(D3D12CheckpointHeadroom(512 * mib, memory) == 0);
  memory.CurrentUsage = 3840 * mib;
  CHECK(D3D12CheckpointHeadroom(512 * mib, memory) == 0);
  memory.CurrentUsage = 3800 * mib;
  CHECK(D3D12CheckpointHeadroom(512 * mib, memory) == 40 * mib);
  memory.CurrentUsage = 3000 * mib;
  CHECK(D3D12CheckpointHeadroom(512 * mib, memory) == 512 * mib);
  bool blocked = true;
  uint64_t budget = 0;
  CHECK_FALSE(D3D12RecoverCheckpointBudget(true, false, 512 * mib, false, memory, blocked, budget));
  CHECK(blocked);
  CHECK(budget == 0);
  CHECK_FALSE(D3D12RecoverCheckpointBudget(false, false, 512 * mib, true, memory, blocked, budget));
  CHECK_FALSE(D3D12RecoverCheckpointBudget(true, true, 512 * mib, true, memory, blocked, budget));
  CHECK_FALSE(D3D12RecoverCheckpointBudget(true, false, 0, true, memory, blocked, budget));
  CHECK(blocked);
  CHECK(budget == 0);
  CHECK(D3D12RecoverCheckpointBudget(true, false, 512 * mib, true, memory, blocked, budget));
  CHECK_FALSE(blocked);
  CHECK(budget == 512 * mib);
  CHECK_FALSE(D3D12RecoverCheckpointBudget(true, false, 512 * mib, true, memory, blocked, budget));
  // A compatible existing reservation is not grown by this narrow zero-budget recovery path.
  blocked = true;
  CHECK_FALSE(D3D12RecoverCheckpointBudget(true, false, 1024 * mib, true, memory, blocked, budget));
  CHECK(budget == 512 * mib);
  memory.Budget = ~0ULL;
  memory.CurrentUsage = ~0ULL - 1;
  CHECK(D3D12CheckpointHeadroom(~0ULL, memory) == 0);
}

TEST_CASE(
    "D3D12 buffer alias checkpoints close physical overlaps without absorbing adjacent storage",
    "[d3d12][checkpoint][buffer-alias]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId a = ResourceIDGen::GetNewUniqueID(), b = ResourceIDGen::GetNewUniqueID();
  const ResourceId c = ResourceIDGen::GetNewUniqueID(), d = ResourceIDGen::GetNewUniqueID();
  const uint64_t unit = 65536;
  const rdcarray<D3D12CheckpointPlacedRange> ranges = {
      {c, 3 * unit, 5 * unit}, {a, 0, 2 * unit}, {d, 5 * unit, 6 * unit}, {b, unit, 4 * unit}};
  const auto components = D3D12BufferAliasComponents(heap, 8 * unit, true, ranges);
  REQUIRE(components.size() == 1);
  CHECK(components[0].begin == 0);
  CHECK(components[0].end == 5 * unit);
  CHECK(components[0].members == std::set<ResourceId>({a, b, c}));
  CHECK(components[0].Range().offset == 0);
  CHECK(components[0].Range().size == 5 * unit);
  const auto offset = D3D12BufferAliasComponents(heap, 8 * unit, true,
                                                 {{a, unit, 3 * unit}, {b, 2 * unit, 4 * unit}});
  REQUIRE(offset.size() == 1);
  CHECK(offset[0].Range().offset == unit);
  CHECK(offset[0].Range().size == 3 * unit);

  // A texture anywhere in the heap, missing sparse closure, or another eligibility failure
  // must veto raw-buffer access even if that resource's reported usage is empty.
  CHECK(D3D12BufferAliasComponents(heap, 8 * unit, false, ranges).empty());
  CHECK(D3D12BufferAliasComponents(ResourceId(), 8 * unit, true, ranges).empty());
  CHECK(D3D12BufferAliasComponents(heap, 0, true, ranges).empty());
  CHECK(D3D12BufferAliasComponents(heap, 4 * unit, true, ranges).empty());
  CHECK(D3D12BufferAliasComponents(heap, 8 * unit, true, {{a, 0, unit}, {a, 0, unit}}).empty());
  CHECK(D3D12BufferAliasComponents(heap, 8 * unit, true, {{a, unit, unit}}).empty());
  CHECK(D3D12BufferAliasComponents(heap, 8 * unit, true, {{a, ~0ULL, unit}}).empty());
  CHECK(D3D12BufferAliasComponents(heap, 8 * unit, true, {{a, 0, unit}, {b, unit, 2 * unit}}).empty());
}

TEST_CASE("D3D12 explicit typed alias activation requires the exact boundary proof",
          "[d3d12][checkpoint][typed-alias]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID(), b = ResourceIDGen::GetNewUniqueID();
  const ResourceId unrelated = ResourceIDGen::GetNewUniqueID();
  const std::map<ResourceId, std::set<ResourceId>> conflicts = {{a, {b}}, {b, {a}}};
  CHECK(D3D12ProvedTypedAliasActivation(true, a, conflicts));
  CHECK(D3D12ProvedTypedAliasActivation(true, b, conflicts));
  CHECK_FALSE(D3D12ProvedTypedAliasActivation(false, a, conflicts));
  CHECK_FALSE(D3D12ProvedTypedAliasActivation(true, ResourceId(), conflicts));
  CHECK_FALSE(D3D12ProvedTypedAliasActivation(true, unrelated, conflicts));
  CHECK_FALSE(D3D12ProvedTypedAliasActivation(true, a, {}));
}

TEST_CASE("D3D12 buffer alias snapshots share only exact resident physical versions",
          "[d3d12][checkpoint][buffer-alias]")
{
  CHECK(D3D12MayShareBufferAliasVersion(true, true, true));
  CHECK_FALSE(D3D12MayShareBufferAliasVersion(false, true, true));
  CHECK_FALSE(D3D12MayShareBufferAliasVersion(true, false, true));
  CHECK_FALSE(D3D12MayShareBufferAliasVersion(true, true, false));
  D3D12ReplayCheckpointCache cache;
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId a = ResourceIDGen::GetNewUniqueID(), b = ResourceIDGen::GetNewUniqueID();
  const auto components = D3D12BufferAliasComponents(
      heap, 8 * 65536, true, {{a, 65536, 3 * 65536}, {b, 2 * 65536, 4 * 65536}});
  REQUIRE(components.size() == 1);
  auto saved = std::make_shared<D3D12ReplayCheckpointBufferAliases>();
  saved->component = components[0];
  saved->component.allocationBytes = 3 * 65536;
  saved->version = {7, 0};
  cache.residentVersions.Register(heap, 0, 8 * 65536);
  cache.checkpoints[10].bufferAliases[a] = saved;
  cache.checkpoints[20].bufferAliases[a] = saved;
  CHECK(cache.AllocatedBytes() == 3 * 65536);
  cache.PublishResidentVersions(cache.checkpoints[10]);
  CHECK(cache.residentVersions.IsResident(saved->component.Range(), saved->version));
  // An unrelated adjacent interval does not dirty this component. An overlapping alias write
  // must invalidate it as a whole, including bytes not visible through that particular alias.
  cache.residentVersions.Invalidate({heap, 0, 4 * 65536, 65536});
  CHECK(cache.residentVersions.IsResident(saved->component.Range(), saved->version));
  cache.residentVersions.Invalidate({heap, 0, 2 * 65536, 16});
  CHECK_FALSE(cache.residentVersions.IsResident(saved->component.Range(), saved->version));
  cache.PublishResidentVersions(cache.checkpoints[20]);
  cache.residentVersions.InvalidateAll();
  CHECK_FALSE(cache.residentVersions.IsResident(saved->component.Range(), saved->version));
  // A changed physical version receives a separate allocation and cannot be undercharged.
  auto changed = std::make_shared<D3D12ReplayCheckpointBufferAliases>();
  changed->component = saved->component;
  changed->version = {8, 1};
  cache.checkpoints[30].bufferAliases[a] = changed;
  CHECK(cache.AllocatedBytes() == 6 * 65536);
  cache.config.maxCheckpoints = 3;
  cache.budget = 5 * 65536;
  std::set<uint32_t> victims;
  CHECK_FALSE(D3D12PlanCheckpointEvictions(cache, cache.checkpoints[30], 0, 10, victims));
}

TEST_CASE("D3D12 upload reuse requires actual GPU fence completion", "[bufferpatch][gpu-idle]")
{
  CHECK(D3D12IdleWaitSucceeded(WAIT_OBJECT_0, S_OK, 7, 7));
  CHECK(D3D12IdleWaitSucceeded(WAIT_OBJECT_0, S_OK, 8, 7));
  // A stale event, timeout, or removed-device sentinel never authorises a write to in-flight data.
  CHECK_FALSE(D3D12IdleWaitSucceeded(WAIT_OBJECT_0, S_OK, 6, 7));
  CHECK_FALSE(D3D12IdleWaitSucceeded(WAIT_TIMEOUT, S_OK, 6, 7));
  CHECK_FALSE(D3D12IdleWaitSucceeded(WAIT_TIMEOUT, S_OK, 7, 7));
  CHECK_FALSE(D3D12IdleWaitSucceeded(WAIT_FAILED, S_OK, 7, 7));
  CHECK_FALSE(D3D12IdleWaitSucceeded(WAIT_ABANDONED, S_OK, 7, 7));
  CHECK_FALSE(D3D12IdleWaitSucceeded(WAIT_OBJECT_0, DXGI_ERROR_DEVICE_REMOVED, 7, 7));
  CHECK_FALSE(D3D12IdleWaitSucceeded(WAIT_OBJECT_0, S_OK, ~0ULL, 7));
  CHECK_FALSE(D3D12IdleWaitSucceeded(WAIT_OBJECT_0, S_OK, 0, 0));
}

TEST_CASE("D3D12 resident checkpoint copies require preserved baseline exclusions and storage",
          "[d3d12][checkpoint][resourceversions]")
{
  CHECK(D3D12MayRetainCheckpointContents(true, true, true, true));
  // Disabling the initial-source optimisation restores baseline bytes over the old checkpoint.
  CHECK_FALSE(D3D12MayRetainCheckpointContents(false, true, true, true));
  // Failed/analysis replay and writes through sparse aliases both invalidate reuse evidence.
  CHECK_FALSE(D3D12MayRetainCheckpointContents(true, false, true, true));
  CHECK_FALSE(D3D12MayRetainCheckpointContents(true, true, false, true));
  CHECK_FALSE(D3D12MayRetainCheckpointContents(true, true, true, false));
}

TEST_CASE("D3D12 initial source accounting counts selected copies instead of cache source extents",
          "[d3d12][replay][dirty-restore][source-copy-timing]")
{
  const uint64_t mb = 1024 * 1024;
  // A multi-gigabyte source is consulted, but the previous writes and checkpoint exclusions
  // leave only two two-megabyte ranges to copy. Cache extent counters cannot account for this.
  rdcarray<rdcpair<uint64_t, uint64_t>> requested;
  CheckpointInitialCopyRanges(4096 * mb, {{0, 4 * mb}, {8 * mb, 100 * mb},
                                         {104 * mb, 4096 * mb}}, requested);
  const auto selected = IntersectReplayDirtyRanges(requested, {{6 * mb, 102 * mb}});
  REQUIRE(selected.size() == 2);
  D3D12ReplayInitialCopyStats stats;
  for(const auto &range : selected)
    stats.Record(range.second - range.first, D3D12ReplayInitialCopySource::Upload, false, false);
  CHECK(stats.upload.bufferBytes == 4 * mb);
  CHECK(stats.upload.footprintBytes == 0);
  CHECK(stats.upload.wholeCopies == 0);
  CHECK(stats.gpuDefault.bufferBytes == 0);
  CHECK(stats.cpuMemcpyBytes == 0);

  // No selected ranges means no transfer, even though a source cache was queried.
  const auto empty = IntersectReplayDirtyRanges(requested, {{200 * mb, 201 * mb}});
  CHECK(empty.empty());
  for(const auto &range : empty)
    stats.Record(range.second - range.first, D3D12ReplayInitialCopySource::Upload, false, false);
  CHECK(stats.upload.bufferBytes == 4 * mb);

  // Texture footprints and CPU memcpy lengths use distinct units and never inherit source width.
  stats.Record(1536, D3D12ReplayInitialCopySource::Default, true, false);
  stats.Record(173, D3D12ReplayInitialCopySource::CPU, false, false);
  CHECK(stats.gpuDefault.bufferBytes == 0);
  CHECK(stats.gpuDefault.footprintBytes == 1536);
  CHECK(stats.cpuMemcpyBytes == 173);

  // Even an accidental byte estimate on a whole copy cannot masquerade as measured bytes.
  stats.Record(4096 * mb, D3D12ReplayInitialCopySource::Other, false, true);
  CHECK(stats.other.bufferBytes == 0);
  CHECK(stats.other.footprintBytes == 0);
  CHECK(stats.other.wholeCopies == 1);

  D3D12ReplayInitialCopyStats totals;
  totals.Add(stats);
  totals.Add(stats);
  CHECK(totals.upload.bufferBytes == 8 * mb);
  CHECK(totals.gpuDefault.footprintBytes == 3072);
  CHECK(totals.cpuMemcpyBytes == 346);
  CHECK(totals.other.wholeCopies == 2);
}

TEST_CASE("D3D12 initial source accounting preserves unknown heaps and fixed diagnostic field order",
          "[d3d12][replay][dirty-restore][source-copy-timing]")
{
  CHECK(uint32_t(ReplayInitialCopyHeapClass(true, D3D12_HEAP_TYPE_UPLOAD)) ==
        uint32_t(D3D12ReplayInitialCopySource::Upload));
  CHECK(uint32_t(ReplayInitialCopyHeapClass(true, D3D12_HEAP_TYPE_DEFAULT)) ==
        uint32_t(D3D12ReplayInitialCopySource::Default));
  CHECK(uint32_t(ReplayInitialCopyHeapClass(true, D3D12_HEAP_TYPE_CUSTOM)) ==
        uint32_t(D3D12ReplayInitialCopySource::Other));
  CHECK(uint32_t(ReplayInitialCopyHeapClass(true, D3D12_HEAP_TYPE_READBACK)) ==
        uint32_t(D3D12ReplayInitialCopySource::Other));
  CHECK(uint32_t(ReplayInitialCopyHeapClass(false, D3D12_HEAP_TYPE_DEFAULT)) ==
        uint32_t(D3D12ReplayInitialCopySource::Other));
  D3D12ReplayInitialCopyStats stats;
  stats.Record(1, D3D12ReplayInitialCopySource::CPU, false, false);
  stats.Record(2, D3D12ReplayInitialCopySource::Upload, false, false);
  stats.Record(3, D3D12ReplayInitialCopySource::Upload, true, false);
  stats.Record(0, D3D12ReplayInitialCopySource::Upload, false, true);
  stats.Record(4, D3D12ReplayInitialCopySource::Default, false, false);
  stats.Record(5, D3D12ReplayInitialCopySource::Default, true, false);
  stats.Record(0, D3D12ReplayInitialCopySource::Default, false, true);
  stats.Record(6, D3D12ReplayInitialCopySource::Other, false, false);
  stats.Record(7, D3D12ReplayInitialCopySource::Other, true, false);
  stats.Record(0, D3D12ReplayInitialCopySource::Other, false, true);
  CHECK(ReplayInitialCopySourceTuple(stats) == "1/2/3/1/4/5/1/6/7/1");
  CHECK(ReplayInitialCopySourceCounters(stats) ==
        "cpuMemcpyBytes=1 gpuUploadBufferBytes=2 gpuUploadFootprintBytes=3 gpuUploadWholeCopies=1 "
        "gpuDefaultBufferBytes=4 gpuDefaultFootprintBytes=5 gpuDefaultWholeCopies=1 "
        "gpuOtherBufferBytes=6 gpuOtherFootprintBytes=7 gpuOtherWholeCopies=1");
}

TEST_CASE("D3D12 shared-buffer checkpoints progress from a prefix to missing downstream points",
          "[d3d12][replay][checkpoint-buffer-patch][checkpoints]")
{
  D3D12ReplayCheckpointCache cache;
  cache.config.maxCheckpoints = 3;
  for(uint32_t event : {10U, 30U, 50U, 70U})
    cache.selected[event].eventId = event;
  CHECK(D3D12MissingCheckpointToSave(cache, 5, 79, 80, 40) == 30);
  CHECK(D3D12MissingCheckpointToSave(cache, 30, 79, 80, 40) == 30);
  CHECK(D3D12MissingCheckpointToSave(cache, 5, 25, 25, 40) == 10);
  CHECK(D3D12MissingCheckpointToSave(cache, 5, 79, 80, 0) == 70);
  CHECK(D3D12MissingCheckpointToSave(cache, 5, 79, 80, 40, true) == 30);
  CHECK(D3D12MissingCheckpointToSave(cache, 5, 79, 80, 0, true) == 0);
  CHECK(D3D12MissingCheckpointToSave(cache, 41, 79, 80, 40, true) == 0);

  cache.checkpoints[30].eventId = 30;
  CHECK(D3D12CheckpointBufferAnchor(cache, 40, 80) == 30);
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 80, 40) == 70);
  // The same edit must render without rebuilding its invalidated suffix. A later idle request
  // uses exactly the same candidates and retained anchor, but can now create that missing point.
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 80, 40, true) == 0);
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 80, 40, false) == 70);
  cache.checkpoints[70].eventId = 70;
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 80, 40) == 50);
  CHECK(D3D12MissingCheckpointToSave(cache, 51, 79, 80, 40) == 0);
  cache.savedThisReplay = true;
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 80, 40) == 0);
  cache.savedThisReplay = false;
  cache.config.maxCheckpoints = 1;
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 80, 40) == 0);
  cache.config.maxCheckpoints = 3;

  cache.checkpoints.erase(70);
  cache.selected[70].firstNativeResolveEID = 80;
  // WithoutDraw stops at 79, but the user's following OnlyDraw at 80 still crosses this horizon.
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 80, 40) == 50);
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 79, 40) == 70);
  cache.checkpoints[30].firstNativeResolveEID = 40;
  CHECK(D3D12CheckpointBufferAnchor(cache, 40, 40) == 0);
  cache.checkpoints[10].firstNativeResolveEID = 100;
  CHECK(D3D12CheckpointBufferAnchor(cache, 40, 80) == 10);
  CHECK(D3D12MissingCheckpointToSave(cache, 31, 79, 80, 40) == 50);
}

TEST_CASE("D3D12 checkpoint eviction protects the edit prefix and counts shared sources once",
          "[d3d12][replay][checkpoint-buffer-patch]")
{
  D3D12ReplayCheckpointCache cache;
  cache.config.maxCheckpoints = 3;
  cache.budget = 200;
  const ResourceId sharedID = ResourceIDGen::GetNewUniqueID();
  const auto source = [](uint64_t bytes) {
    auto result = std::make_shared<D3D12ReplayCheckpointResource>();
    result->bytes = bytes;    // CPU-only lifetime/size model: no native allocation
    return result;
  };
  cache.checkpoints[30].resources[sharedID] = source(64);
  cache.checkpoints[30].lastUse = 0;    // oldest, but the persistent-edit anchor must survive
  cache.checkpoints[60].resources[ResourceIDGen::GetNewUniqueID()] = source(16);
  cache.checkpoints[60].lastUse = 1;
  cache.checkpoints[70].resources[ResourceIDGen::GetNewUniqueID()] = source(16);
  cache.checkpoints[70].lastUse = 2;
  D3D12ReplayCheckpoint candidate;
  candidate.resources[sharedID] = cache.checkpoints[30].resources[sharedID];
  std::set<uint32_t> victims;

  SECTION("Point capacity evicts only the oldest unprotected version")
  {
    REQUIRE(D3D12PlanCheckpointEvictions(cache, candidate, 32, 30, victims));
    CHECK(victims.size() == 1);
    CHECK(victims.count(60) == 1);
    CHECK(cache.checkpoints.size() == 3);    // planning never releases a source still on the GPU
    CHECK(cache.AllocatedBytes(&candidate, &victims) == 80);
  }
  SECTION("Shared prefix data is charged once under an exact byte budget")
  {
    cache.budget = 96;
    REQUIRE(D3D12PlanCheckpointEvictions(cache, candidate, 32, 30, victims));
    CHECK(victims.size() == 2);
    CHECK(victims.count(30) == 0);
    CHECK(cache.AllocatedBytes(&candidate, &victims) == 64);
  }
  SECTION("An unaffordable downstream point leaves every old point and the anchor intact")
  {
    cache.budget = 95;
    CHECK_FALSE(D3D12PlanCheckpointEvictions(cache, candidate, 32, 30, victims));
    CHECK(victims.empty());
    CHECK(cache.checkpoints.size() == 3);
  }
  SECTION("One slot cannot replace a protected anchor with a downstream version")
  {
    cache.config.maxCheckpoints = 1;
    CHECK_FALSE(D3D12PlanCheckpointEvictions(cache, candidate, 32, 30, victims));
    CHECK(victims.empty());
  }
  SECTION("Without an active edit anchor ordinary oldest-point eviction still applies")
  {
    REQUIRE(D3D12PlanCheckpointEvictions(cache, candidate, 32, 0, victims));
    CHECK(victims.count(30) == 1);
    CHECK(cache.AllocatedBytes(&candidate, &victims) == 96);
  }
}

TEST_CASE("D3D12 ordinary final draws preserve dirty proof despite checkpoint bypass",
          "[d3d12][replay][dirty-restore]")
{
  // The controller's normal sequence is WithoutDraw with reuse allowed, then an explicitly
  // checkpoint-bypassed OnlyDraw. The final draw must not discard the root's complete proof.
  bool valid = D3D12ReplayPreservesDirtyProof(eReplay_WithoutDraw, true, false, false, false, false);
  REQUIRE(valid);
  const auto onlyDraw = [&](bool feedback, bool ordinary, bool callback, bool edited) {
    if(!D3D12ReplayPreservesDirtyProof(eReplay_OnlyDraw, false, feedback, ordinary, callback, edited))
      valid = false;
  };
  onlyDraw(false, true, false, false);    // feedback already cached: ordinary driver target
  CHECK(valid);
  onlyDraw(true, false, true, false);     // first feedback: the one known debug callback
  CHECK(valid);

  // A force-canonical root must reset fully. Its later ordinary final draw cannot turn an
  // invalid proof back into a valid one without another successful normal initial restoration.
  valid = D3D12ReplayPreservesDirtyProof(eReplay_WithoutDraw, false, false, false, false, false);
  CHECK_FALSE(valid);
  onlyDraw(false, true, false, false);
  CHECK_FALSE(valid);
  onlyDraw(true, false, true, false);
  CHECK_FALSE(valid);

  valid = true;
  onlyDraw(false, false, false, false);    // arbitrary internal analysis / event CBuffer override
  CHECK_FALSE(valid);
  valid = true;
  onlyDraw(false, true, true, false);      // ordinary token cannot authorise another callback
  CHECK_FALSE(valid);
  valid = true;
  onlyDraw(false, true, false, true);      // global shader/resource replacement remains conservative
  CHECK_FALSE(valid);
  CHECK_FALSE(D3D12ReplayPreservesDirtyProof(eReplay_WithoutDraw, false, true, true, false, false));

  // An automatic reference takes the same initial-resource path as an ordinary request, while
  // checkpoint reuse remains false. This must not borrow force-canonical's all-dirty policy.
  valid = D3D12ReplayPreservesDirtyProof(eReplay_WithoutDraw, false, false, false, false, false, true);
  REQUIRE(valid);
  onlyDraw(false, true, false, false);
  CHECK(valid);
  CHECK(D3D12ReplayPreservesDirtyProof(eReplay_Full, false, false, false, false, false, true));
  CHECK_FALSE(D3D12ReplayPreservesDirtyProof(eReplay_WithoutDraw, false, false, false, true, false,
                                            true));
  CHECK_FALSE(D3D12ReplayPreservesDirtyProof(eReplay_WithoutDraw, false, false, false, false, true,
                                            true));
  // Clearing the distinct reference permission restores the strict force/analysis behaviour.
  CHECK_FALSE(D3D12ReplayPreservesDirtyProof(eReplay_WithoutDraw, false, false, false, false, false,
                                            false));
}

TEST_CASE("D3D12 live forward requires an ordinary root and the matching successful final draw",
          "[d3d12][replay][live-forward]")
{
  CHECK(IsOrdinaryLiveForwardRoot(0, eReplay_WithoutDraw, true, true, false, false));
  CHECK_FALSE(IsOrdinaryLiveForwardRoot(0, eReplay_WithoutDraw, true, false, false, false));
  CHECK_FALSE(IsOrdinaryLiveForwardRoot(0, eReplay_WithoutDraw, true, false, true, false));
  CHECK_FALSE(IsOrdinaryLiveForwardRoot(0, eReplay_WithoutDraw, true, true, true, false));
  CHECK_FALSE(IsOrdinaryLiveForwardRoot(0, eReplay_WithoutDraw, false, true, false, false));
  CHECK_FALSE(IsOrdinaryLiveForwardRoot(0, eReplay_WithoutDraw, true, true, false, true));
  CHECK_FALSE(IsOrdinaryLiveForwardRoot(0, eReplay_Full, true, true, false, false));
  CHECK_FALSE(IsOrdinaryLiveForwardRoot(0, eReplay_OnlyDraw, true, true, false, false));
  CHECK_FALSE(IsOrdinaryLiveForwardRoot(1, eReplay_WithoutDraw, true, true, false, false));
  CHECK(CanPublishNaturalReferenceRoot(0, eReplay_WithoutDraw, true, false));
  CHECK_FALSE(CanPublishNaturalReferenceRoot(1, eReplay_WithoutDraw, true, false));
  CHECK_FALSE(CanPublishNaturalReferenceRoot(0, eReplay_WithoutDraw, false, false));
  CHECK_FALSE(CanPublishNaturalReferenceRoot(0, eReplay_WithoutDraw, true, true));
  CHECK_FALSE(CanPublishNaturalReferenceRoot(0, eReplay_Full, true, false));
  CHECK_FALSE(CanPublishNaturalReferenceRoot(0, eReplay_OnlyDraw, true, false));

  CHECK(CanPublishLiveForwardTarget(29385, 29385, 7, 7, false));
  // A force/analysis root stages no target; its later normal OnlyDraw cannot revive old state.
  CHECK_FALSE(CanPublishLiveForwardTarget(29385, 0, 7, 7, false));
  CHECK_FALSE(CanPublishLiveForwardTarget(29385, 28726, 7, 7, false));
  // Mutation between root and target invalidates the generation even if the EID is unchanged.
  CHECK_FALSE(CanPublishLiveForwardTarget(29385, 29385, 8, 7, false));
  CHECK_FALSE(CanPublishLiveForwardTarget(29385, 29385, 7, 7, true));
  CHECK_FALSE(CanPublishLiveForwardTarget(0, 0, 7, 7, false));
}

TEST_CASE("D3D12 sparse initial residency publishes only submitted complete transactions",
          "[d3d12][replay][dirty-restore][sparse-resident]")
{
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  D3D12ReplaySparseInitialBaseline baseline;

  // Cold loading and metadata alone are never a resident seed. Recording a copy also does not
  // publish it until its submission and queue dependency have actually succeeded.
  CHECK_FALSE(baseline.Begin(true, true, true, false, false));
  baseline.Record(texture, 128);
  baseline.Record(texture, 128);
  baseline.Record(heap, 64);
  CHECK_FALSE(baseline.resident);
  REQUIRE(baseline.Submit(true));
  CHECK(baseline.residentBytes == 320);
  CHECK(baseline.residentResourceBytes[texture] == 256);
  CHECK(baseline.residentResourceBytes[heap] == 64);
  REQUIRE(baseline.Begin(true, true, true, true, false));
  CHECK(baseline.retained);
  CHECK_FALSE(baseline.pending);
  CHECK_FALSE(baseline.Submit(true));    // a retained transaction creates no second copy seed
  CHECK(baseline.residentBytes == 320);

  // A write changes the entire component. After canonical restoration a new baseline may be
  // published, and its byte accounting replaces rather than accumulates the previous restore.
  baseline.Invalidate("resourceWrite");
  CHECK_FALSE(baseline.Begin(true, true, true, true, true));
  baseline.Record(texture, 256);
  REQUIRE(baseline.Submit(true));
  CHECK(baseline.residentBytes == 256);
  CHECK(baseline.residentResourceBytes.count(heap) == 0);
  REQUIRE(baseline.Begin(true, true, true, true, false));
  // The pre-Prepare dirty set is an independent safety check, even if a future caller omitted
  // the earlier eager invalidation hook for that known component write.
  CHECK_FALSE(baseline.Begin(true, true, true, true, true));
  CHECK_FALSE(baseline.resident);
  CHECK_FALSE(baseline.retained);
}

TEST_CASE("D3D12 sparse initial residency cancels stale mapping and analysis submissions",
          "[d3d12][replay][dirty-restore][sparse-resident]")
{
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  D3D12ReplaySparseInitialBaseline baseline;
  REQUIRE_FALSE(baseline.Begin(true, true, true, false, false));
  baseline.Record(texture, 256);
  const uint64_t recordedGeneration = baseline.generation;
  // Even a mapping call to an already known heap cancels the transaction. A delayed completion
  // cannot publish the old contents after UpdateTileMappings or CopyTileMappings.
  baseline.Invalidate("tileMapping");
  CHECK(baseline.generation != recordedGeneration);
  CHECK_FALSE(baseline.Submit(true));
  CHECK_FALSE(baseline.resident);
  REQUIRE_FALSE(baseline.Begin(true, true, true, true, true));
  baseline.Record(texture, 256);
  REQUIRE(baseline.Submit(true));
  baseline.Invalidate("tileMappingCopy");
  CHECK_FALSE(baseline.Begin(true, true, true, true, false));
  baseline.Record(texture, 256);
  REQUIRE(baseline.Submit(true));
  // Global analysis/edit invalidation must also discard an already published seed.
  baseline.Invalidate("analysisOrMutation");
  CHECK_FALSE(baseline.Begin(true, false, true, true, false));
  baseline.Record(texture, 256);
  CHECK_FALSE(baseline.Submit(true));
  CHECK_FALSE(baseline.resident);
}

TEST_CASE("D3D12 sparse explicit initial lists require every subresource exactly once",
          "[d3d12][replay][dirty-restore][sparse-resident]")
{
  CHECK(ReplaySparseInitialSubresourcesComplete({0}, 1));
  CHECK(ReplaySparseInitialSubresourcesComplete({0, 1}, 2));
  CHECK(ReplaySparseInitialSubresourcesComplete({0, 1, 2, 3}, 4));
  CHECK(ReplaySparseInitialSubresourcesComplete({~0U}, 4));
  CHECK_FALSE(ReplaySparseInitialSubresourcesComplete({}, 1));
  CHECK_FALSE(ReplaySparseInitialSubresourcesComplete({~0U}, 0));
  CHECK_FALSE(ReplaySparseInitialSubresourcesComplete({0}, 2));
  CHECK_FALSE(ReplaySparseInitialSubresourcesComplete({1}, 1));
  CHECK_FALSE(ReplaySparseInitialSubresourcesComplete({0, 0}, 2));
  CHECK_FALSE(ReplaySparseInitialSubresourcesComplete({1, 0}, 2));
  CHECK_FALSE(ReplaySparseInitialSubresourcesComplete({0, 2}, 2));
  CHECK_FALSE(ReplaySparseInitialSubresourcesComplete({~0U, 1}, 2));
}

TEST_CASE("D3D12 sparse initial residency rejects failed and second fallback baselines",
          "[d3d12][replay][dirty-restore][sparse-resident]")
{
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  D3D12ReplaySparseInitialBaseline baseline;
  REQUIRE_FALSE(baseline.Begin(true, true, true, false, false));
  CHECK_FALSE(baseline.Submit(true));    // a skipped/empty initial pass did not copy the contents
  REQUIRE_FALSE(baseline.Begin(true, true, true, false, false));
  baseline.Record(texture, 256);
  CHECK_FALSE(baseline.Submit(false));   // failed Close/Execute/dependency/fatal path
  CHECK_FALSE(baseline.resident);

  REQUIRE_FALSE(baseline.Begin(true, true, true, false, false));
  baseline.Record(texture, 256);
  REQUIRE(baseline.Submit(true));
  REQUIRE(baseline.Begin(true, true, true, true, false));
  // A checkpoint fallback invokes ApplyInitialContents a second time without a fresh root
  // reset request. Neither the previous retained set nor new copy recording may become a seed.
  CHECK_FALSE(baseline.Begin(true, true, false, true, false));
  CHECK_FALSE(baseline.retained);
  CHECK_FALSE(baseline.pending);
  baseline.Record(texture, 256);
  CHECK_FALSE(baseline.Submit(true));
  CHECK_FALSE(baseline.resident);

  REQUIRE_FALSE(baseline.Begin(true, true, true, false, false));
  baseline.Record(texture, ~0ULL);
  baseline.Record(texture, 1);
  CHECK_FALSE(baseline.Submit(true));    // accounting overflow cannot certify a complete seed
  CHECK_FALSE(baseline.resident);
}

TEST_CASE("D3D12 sparse dirty closure retains old pools and propagates copied mappings",
          "[d3d12][replay][dirty-restore][sparse-closure]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  const ResourceId c = ResourceIDGen::GetNewUniqueID();
  const ResourceId oldHeap = ResourceIDGen::GetNewUniqueID();
  const ResourceId newHeap = ResourceIDGen::GetNewUniqueID();
  const ResourceId oldPlaced = ResourceIDGen::GetNewUniqueID();
  const ResourceId newPlaced = ResourceIDGen::GetNewUniqueID();
  const ResourceId isolated = ResourceIDGen::GetNewUniqueID();
  const ResourceId isolatedHeap = ResourceIDGen::GetNewUniqueID();
  std::map<ResourceId, std::set<ResourceId>> links;
  AddReplaySparseLink(links, a, oldHeap);    // initial mapping; a writes before its later remap
  AddReplaySparseLink(links, a, newHeap);
  AddReplaySparseLink(links, a, ResourceId());    // a later NULL/SKIP must not erase either pool
  AddReplaySparseLink(links, b, a);          // CopyTileMappings chain, irrespective of load order
  AddReplaySparseLink(links, c, b);
  AddReplaySparseLink(links, a, c);          // a cycle cannot lose dependencies or loop forever
  AddReplaySparseLink(links, oldPlaced, oldHeap);
  AddReplaySparseLink(links, newPlaced, newHeap);
  AddReplaySparseLink(links, isolated, isolatedHeap);
  const auto components = ReplaySparseConnectedComponents(links);
  REQUIRE(components.size() == 2);
  std::set<ResourceId> shared;
  for(const auto &component : components)
    if(component.count(a))
      shared = component;
  REQUIRE(shared.size() == 7);
  CHECK(shared.count(oldHeap) == 1);
  CHECK(shared.count(newHeap) == 1);
  CHECK(shared.count(oldPlaced) == 1);
  CHECK(shared.count(newPlaced) == 1);
  CHECK(shared.count(b) == 1);
  CHECK(shared.count(c) == 1);
  CHECK(shared.count(isolated) == 0);
  // The same component is selected by a CPU heap write or a placed-resource write, not only by
  // a reserved resource write. A later mapping in another direction cannot hide an old alias.
  for(const auto &component : components)
    if(component.count(oldHeap) || component.count(newPlaced) || component.count(c))
      CHECK((component == shared));
}

TEST_CASE("D3D12 sparse dirty proof requires complete initial and captured mapping identities",
          "[d3d12][replay][dirty-restore][sparse-closure]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId unknown = ResourceIDGen::GetNewUniqueID();
  CHECK(ReplaySparseInputsComplete({a, b}, {a, b}, {a, b}));
  CHECK_FALSE(ReplaySparseInputsComplete({a, b}, {a}, {a, b}));
  CHECK_FALSE(ReplaySparseInputsComplete({a, b}, {a, b}, {a, unknown}));
  CHECK_FALSE(ReplaySparseInputsComplete({a}, {a}, {ResourceId()}));
  CHECK(ReplaySparseInputsComplete({}, {}, {}));

  std::map<ResourceId, std::set<ResourceId>> links;
  AddReplaySparseLink(links, a, ResourceId());    // explicitly all-NULL is known, not missing
  CHECK(ReplaySparseMappingKnown(links, a, ResourceId()));
  CHECK_FALSE(ReplaySparseMappingKnown(links, b, ResourceId()));
  CHECK_FALSE(ReplaySparseMappingKnown(links, a, heap));
  AddReplaySparseLink(links, a, heap);
  AddReplaySparseLink(links, b, a);
  CHECK(ReplaySparseMappingKnown(links, a, heap));
  CHECK(ReplaySparseMappingKnown(links, b, a));
  CHECK_FALSE(ReplaySparseMappingKnown(links, b, unknown));
  CHECK_FALSE(ReplaySparseMappingKnown(links, ResourceId(), heap));
  // An uncaptured direct bind remains unknown even if its heap happens to be transitively in
  // the same component. This makes unexpected runtime mapping changes fail closed.
  CHECK_FALSE(ReplaySparseMappingKnown(links, b, heap));
}

TEST_CASE("D3D12 sparse initial copies select aliases before any initial entry can be skipped",
          "[d3d12][replay][dirty-restore][sparse-closure]")
{
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId placed = ResourceIDGen::GetNewUniqueID();
  const ResourceId independent = ResourceIDGen::GetNewUniqueID();
  std::map<ResourceId, std::set<ResourceId>> links;
  AddReplaySparseLink(links, texture, heap);
  AddReplaySparseLink(links, placed, heap);
  AddReplaySparseLink(links, independent, ResourceId());
  std::set<ResourceId> dirty;    // no captured action wrote anything in this example
  REQUIRE(ReplaySparseInitialWritesMemory(false, D3D12InitialContents::Copy));
  for(const auto &component : ReplaySparseConnectedComponents(links))
    if(component.count(texture))
      dirty.insert(component.begin(), component.end());
  CHECK(dirty.count(placed) == 1);    // even when placed is visited before texture during reset
  CHECK(dirty.count(heap) == 1);
  CHECK(dirty.count(independent) == 0);
  CHECK(ReplaySparseInitialWritesMemory(false, D3D12InitialContents::ForceCopy));
  CHECK_FALSE(ReplaySparseInitialWritesMemory(false, D3D12InitialContents::SparseOnly));
  CHECK_FALSE(ReplaySparseInitialWritesMemory(true, D3D12InitialContents::Copy));
  CHECK_FALSE(ReplaySparseInitialWritesMemory(false, D3D12InitialContents::AccelerationStructure));

  SparseBinds nullInitial(0);
  std::set<ResourceId> initialHeaps;
  nullInitial.GetReferencedHeaps(initialHeaps);
  CHECK(initialHeaps.empty());

  const ResourceId secondHeap = ResourceIDGen::GetNewUniqueID();
  const uint32_t tileBytes = 64 * 1024;
  Sparse::PageTable table;
  table.Initialise(uint64_t(tileBytes) * 4, tileBytes);
  table.setBufferRange(0, heap, 0, uint64_t(tileBytes) * 4, true);    // reused single tile
  SparseBinds reusedInitial(table);
  reusedInitial.GetReferencedHeaps(initialHeaps);
  CHECK(initialHeaps.size() == 1);
  CHECK(initialHeaps.count(heap) == 1);
  table.setBufferRange(tileBytes, secondHeap, tileBytes, tileBytes, false);    // per-page map
  table.setBufferRange(uint64_t(tileBytes) * 2, ResourceId(), 0, tileBytes, false);
  initialHeaps.clear();
  SparseBinds mixedInitial(table);
  mixedInitial.GetReferencedHeaps(initialHeaps);
  CHECK(initialHeaps.size() == 2);
  CHECK(initialHeaps.count(heap) == 1);
  CHECK(initialHeaps.count(secondHeap) == 1);
  CHECK(initialHeaps.count(ResourceId()) == 0);
}

TEST_CASE("D3D12 dirty heap restoration composes alias ranges with checkpoint final sources",
          "[d3d12][replay][dirty-restore]")
{
  rdcarray<rdcpair<uint64_t, uint64_t>> dirty;
  MergeReplayDirtyRange(dirty, 128, 256);
  MergeReplayDirtyRange(dirty, 512, 768);
  MergeReplayDirtyRange(dirty, 192, 640);    // a physical alias joins both ranges
  REQUIRE(dirty.size() == 1);
  CHECK(dirty[0].first == 128);
  CHECK(dirty[0].second == 768);
  MergeReplayDirtyRange(dirty, 64, 128);
  CHECK(dirty[0].first == 64);
  MergeReplayDirtyRange(dirty, 900, 900);
  CHECK(dirty.size() == 1);

  rdcarray<rdcpair<uint64_t, uint64_t>> initial = {{0, 256}, {512, 1024}};
  const auto selected = IntersectReplayDirtyRanges(initial, dirty);
  REQUIRE(selected.size() == 2);
  CHECK(selected[0].first == 64);
  CHECK(selected[0].second == 256);
  CHECK(selected[1].first == 512);
  CHECK(selected[1].second == 768);
  CHECK(IntersectReplayDirtyRanges(initial, {}).empty());
  CHECK(IntersectReplayDirtyRanges({}, dirty).empty());
  MergeReplayDirtyRange(dirty, 0, ~0ULL);    // unbounded root UAV / unknown heap CPU write
  CHECK((IntersectReplayDirtyRanges(initial, dirty) == initial));
}

TEST_CASE("D3D12 unchanged CPU blobs invalidate live versions while preserving initial debt",
          "[d3d12][replay][dirty-restore][buffer-ranges][cpu-baseline]")
{
  const ResourceId id = ResourceIDGen::GetNewUniqueID();
  std::set<ResourceId> dirty;
  std::map<ResourceId, rdcarray<rdcpair<uint64_t, uint64_t>>> ranges;
  bool valid = true, residentVersion = true, liveCursor = true, invalidate = false;
  uint32_t wholeWrites = 0, preciseWrites = 0;
  const auto wholeWrite = [&]() {
    wholeWrites++;
    residentVersion = false;
    liveCursor = false;
    dirty.insert(id);
    ranges.erase(id);
    valid &= !invalidate;
  };
  const auto rangeWrite = [&](uint64_t, uint64_t) { preciseWrites++; };
  ApplyReplayCPUWriteBaselineDebt(id, {}, dirty, ranges, valid, wholeWrite, rangeWrite);
  CHECK(wholeWrites == 1);
  CHECK(preciseWrites == 0);
  CHECK_FALSE(residentVersion);
  CHECK_FALSE(liveCursor);
  CHECK(dirty.empty());    // proven equal to initial, but the cached live version was invalidated

  dirty.insert(id);
  ranges[id] = {{8, 12}};
  residentVersion = liveCursor = true;
  ApplyReplayCPUWriteBaselineDebt(id, {}, dirty, ranges, valid, wholeWrite, rangeWrite);
  CHECK(wholeWrites == 2);
  CHECK_FALSE(residentVersion);
  CHECK_FALSE(liveCursor);
  REQUIRE(ranges[id].size() == 1);
  CHECK(ranges[id][0].first == 8);
  CHECK(ranges[id][0].second == 12);    // unchanged write cannot remove an earlier dirty interval

  invalidate = true;
  ApplyReplayCPUWriteBaselineDebt(id, {{40, 44}}, dirty, ranges, valid, wholeWrite, rangeWrite);
  CHECK_FALSE(valid);
  CHECK(dirty.count(id) == 1);
  CHECK(ranges.empty());    // failure in the write side effects must keep the whole-buffer debt
  CHECK(preciseWrites == 0);
}

TEST_CASE("D3D12 full CPU blobs retain only differences from immutable initial bytes",
          "[d3d12][replay][dirty-restore][buffer-ranges][cpu-baseline]")
{
  bytebuf original, first, second;
  original.resize(64);
  for(size_t i = 0; i < original.size(); i++)
    original[i] = byte(i);
  first = original;
  first[8] = 0xfe;
  first[9] = 0xfe;
  first[40] = 0xfe;
  rdcarray<rdcpair<uint64_t, uint64_t>> differences, dirty;
  REQUIRE(ReplayCPUWriteBaselineDifferences(original.data(), first.data(), 0, 64, differences));
  REQUIRE(differences.size() == 2);
  bool previouslyDirty = false;
  for(const auto &range : differences)
  {
    REQUIRE(MergeReplayBufferDirtyWrite(previouslyDirty, true, 64, range.first,
                                        range.second - range.first, dirty));
    previouslyDirty = true;
  }
  // The next whole blob restores byte 8 to its initial value, but changes another isolated byte.
  // Keeping the union is conservative and avoids any assumption about intervening GPU writes.
  second = first;
  second[8] = original[8];
  second[55] = 0xfe;
  REQUIRE(ReplayCPUWriteBaselineDifferences(original.data(), second.data(), 0, 64, differences));
  for(const auto &range : differences)
    REQUIRE(MergeReplayBufferDirtyWrite(true, true, 64, range.first, range.second - range.first,
                                        dirty));
  auto working = second;
  uint64_t bytes = 0;
  for(const auto &range : SelectReplayBufferDirtyCopies(true, 64, dirty, {{0, 64}}))
  {
    memcpy(working.data() + size_t(range.first), original.data() + size_t(range.first),
           size_t(range.second - range.first));
    bytes += range.second - range.first;
  }
  CHECK(bytes == 4);
  CHECK((working == original));
  const auto previousDirty = dirty;
  REQUIRE(ReplayCPUWriteBaselineDifferences(original.data(), original.data(), 0, 64, differences));
  CHECK(differences.empty());
  for(const auto &range : differences)
    MergeReplayBufferDirtyWrite(true, true, 64, range.first, range.second - range.first, dirty);
  CHECK((dirty == previousDirty));    // an unchanged full blob never clears prior write evidence
}

TEST_CASE("D3D12 CPU baseline proof rejects missing bytes overflow and fragmented data",
          "[d3d12][replay][dirty-restore][buffer-ranges][cpu-baseline]")
{
  const byte original[] = {0, 1, 2, 3, 4, 5, 6, 7};
  const byte written[] = {8, 1, 8, 3, 8, 5, 6, 7};
  rdcarray<rdcpair<uint64_t, uint64_t>> differences;
  CHECK_FALSE(ReplayCPUWriteBaselineDifferences(NULL, written, 0, 8, differences));
  CHECK_FALSE(ReplayCPUWriteBaselineDifferences(original, NULL, 0, 8, differences));
  CHECK_FALSE(ReplayCPUWriteBaselineDifferences(original, written, ~0ULL - 4, 8, differences));
  CHECK_FALSE(ReplayCPUWriteBaselineDifferences(original, written, 0, 8, differences, 2));
  CHECK(differences.empty());
  REQUIRE(ReplayCPUWriteBaselineDifferences(original, written, 32, 8, differences));
  REQUIRE(differences.size() == 3);
  CHECK(differences[0].first == 32);
  CHECK(differences[0].second == 33);
  CHECK(differences[2].first == 36);
  CHECK(differences[2].second == 37);
  // An earlier unknown GPU write still dominates a subsequently verified CPU baseline blob.
  rdcarray<rdcpair<uint64_t, uint64_t>> wholeDirty;
  for(const auto &range : differences)
    CHECK_FALSE(MergeReplayBufferDirtyWrite(true, true, 64, range.first,
                                            range.second - range.first, wholeDirty));
  const rdcarray<rdcpair<uint64_t, uint64_t>> all = {{0, 64}};
  CHECK((SelectReplayBufferDirtyCopies(true, 64, wholeDirty, all) == all));
}

TEST_CASE("D3D12 exact CPU buffer writes restore the union without touching neighbours",
          "[d3d12][replay][dirty-restore][buffer-ranges]")
{
  rdcarray<rdcpair<uint64_t, uint64_t>> dirty;
  REQUIRE(MergeReplayBufferDirtyWrite(false, true, 64, 8, 4, dirty));
  REQUIRE(MergeReplayBufferDirtyWrite(true, true, 64, 10, 6, dirty));
  // A persistent patch is copied again outside the captured CPU write, and joins the union.
  REQUIRE(MergeReplayBufferDirtyWrite(true, true, 64, 40, 4, dirty));
  const auto copies = SelectReplayBufferDirtyCopies(true, 64, dirty, {{0, 64}});
  REQUIRE(copies.size() == 2);
  CHECK(copies[0].first == 8);
  CHECK(copies[0].second == 16);
  CHECK(copies[1].first == 40);
  CHECK(copies[1].second == 44);

  bytebuf original, working;
  original.resize(64);
  for(size_t i = 0; i < original.size(); i++)
    original[i] = byte(i);
  working = original;
  memset(working.data() + 8, 0xa5, 8);
  memset(working.data() + 40, 0xa5, 4);
  uint64_t copiedBytes = 0;
  for(const auto &range : copies)
  {
    memcpy(working.data() + size_t(range.first), original.data() + size_t(range.first),
           size_t(range.second - range.first));
    copiedBytes += range.second - range.first;
  }
  CHECK(copiedBytes == 12);
  CHECK((working == original));

  // Compose with a prior planner exclusion, without restoring bytes promised by another source.
  const auto excluded = SelectReplayBufferDirtyCopies(true, 64, dirty, {{0, 10}, {42, 64}});
  REQUIRE(excluded.size() == 2);
  CHECK(excluded[0].first == 8);
  CHECK(excluded[0].second == 10);
  CHECK(excluded[1].first == 42);
  CHECK(excluded[1].second == 44);
}

TEST_CASE("D3D12 unknown writes and invalidated baselines cannot inherit CPU buffer ranges",
          "[d3d12][replay][dirty-restore][buffer-ranges]")
{
  const rdcarray<rdcpair<uint64_t, uint64_t>> all = {{0, 64}};
  rdcarray<rdcpair<uint64_t, uint64_t>> dirty;
  // A GPU/unknown write first marks the resource dirty with no exact ranges.
  CHECK_FALSE(MergeReplayBufferDirtyWrite(true, true, 64, 8, 4, dirty));
  CHECK((SelectReplayBufferDirtyCopies(true, 64, dirty, all) == all));
  REQUIRE(MergeReplayBufferDirtyWrite(false, true, 64, 8, 4, dirty));
  // Eligibility can be lost after the first write (replacement, alias or unknown native state).
  CHECK_FALSE(MergeReplayBufferDirtyWrite(true, false, 64, 40, 4, dirty));
  CHECK(dirty.empty());
  CHECK_FALSE(MergeReplayBufferDirtyWrite(true, true, 64, 44, 4, dirty));
  CHECK((SelectReplayBufferDirtyCopies(true, 64, dirty, all) == all));

  REQUIRE(MergeReplayBufferDirtyWrite(false, true, 64, 8, 4, dirty));
  // Force, mutation/undo, failed initial restore and the disabled path all invalidate reuse.
  const auto reset = SelectReplayBufferDirtyCopies(false, 64, dirty, all);
  CHECK((reset == all));
  bytebuf original, working;
  original.resize(64);
  memset(original.data(), 0x31, original.size());
  working = original;
  working[8] = 0x7f;
  working[50] = 0x7f;    // retired patch outside the preceding CPU range
  for(const auto &range : reset)
    memcpy(working.data() + size_t(range.first), original.data() + size_t(range.first),
           size_t(range.second - range.first));
  CHECK((working == original));
}

TEST_CASE("D3D12 CPU buffer ranges reject overflow and cap fragmentation conservatively",
          "[d3d12][replay][dirty-restore][buffer-ranges]")
{
  rdcarray<rdcpair<uint64_t, uint64_t>> dirty;
  CHECK_FALSE(MergeReplayBufferDirtyWrite(false, true, 64, 65, 1, dirty));
  CHECK_FALSE(MergeReplayBufferDirtyWrite(false, true, 64, 63, 2, dirty));
  CHECK_FALSE(MergeReplayBufferDirtyWrite(false, true, ~0ULL, ~0ULL - 1, 4, dirty));
  CHECK_FALSE(MergeReplayBufferDirtyWrite(false, true, 64, 8, 0, dirty));
  CHECK_FALSE(MergeReplayBufferDirtyWrite(false, true, 0, 0, 1, dirty));
  REQUIRE(MergeReplayBufferDirtyWrite(false, true, ~0ULL, ~0ULL - 4, 4, dirty));
  CHECK(dirty[0].second == ~0ULL);
  dirty.clear();
  REQUIRE(MergeReplayBufferDirtyWrite(false, true, 64, 0, 1, dirty, 2));
  REQUIRE(MergeReplayBufferDirtyWrite(true, true, 64, 4, 1, dirty, 2));
  CHECK_FALSE(MergeReplayBufferDirtyWrite(true, true, 64, 8, 1, dirty, 2));
  CHECK(dirty.empty());
  CHECK_FALSE(MergeReplayBufferDirtyWrite(true, true, 64, 9, 1, dirty, 2));
  const rdcarray<rdcpair<uint64_t, uint64_t>> all = {{0, 64}};
  CHECK((SelectReplayBufferDirtyCopies(true, 64, {{8, 65}}, all) == all));
  CHECK((SelectReplayBufferDirtyCopies(true, 64, {{8, 8}}, all) == all));
  CHECK((SelectReplayBufferDirtyCopies(true, 64, {{40, 44}, {8, 12}}, all) == all));
}

TEST_CASE("D3D12 CPU range restoration excludes physical alias and GPU-write capabilities",
          "[d3d12][replay][dirty-restore][buffer-ranges]")
{
  D3D12_RESOURCE_DESC desc = {};
  desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
  desc.Width = 64;
  CHECK(ReplayBufferDirtyEligible(desc, false, false, false, true, D3D12_HEAP_FLAG_NONE));
  CHECK_FALSE(ReplayBufferDirtyEligible(desc, true, false, false, true, D3D12_HEAP_FLAG_NONE));
  CHECK_FALSE(ReplayBufferDirtyEligible(desc, false, true, false, true, D3D12_HEAP_FLAG_NONE));
  CHECK_FALSE(ReplayBufferDirtyEligible(desc, false, false, true, true, D3D12_HEAP_FLAG_NONE));
  CHECK_FALSE(ReplayBufferDirtyEligible(desc, false, false, false, false, D3D12_HEAP_FLAG_NONE));
  CHECK_FALSE(ReplayBufferDirtyEligible(desc, false, false, false, true, D3D12_HEAP_FLAG_SHARED));
  CHECK_FALSE(ReplayBufferDirtyEligible(desc, false, false, false, true,
                                       D3D12_HEAP_FLAG_SHARED_CROSS_ADAPTER));
  desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
  CHECK_FALSE(ReplayBufferDirtyEligible(desc, false, false, false, true, D3D12_HEAP_FLAG_NONE));
  desc.Flags = D3D12_RESOURCE_FLAG_NONE;
  desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
  CHECK_FALSE(ReplayBufferDirtyEligible(desc, false, false, false, true, D3D12_HEAP_FLAG_NONE));
}

TEST_CASE("D3D12 shared value edits retain only same-layout pre-consumer checkpoints",
          "[d3d12][replay][checkpoint-buffer-patch]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  rdcarray<ReplayBufferPatch> previous = {{buffer, 256, {1, 2, 3, 4}}};
  auto next = previous;
  next[0].data = {5, 6, 7, 8};
  CHECK(SameD3D12BufferPatchLayout(previous, next));
  next[0].byteOffset++;
  CHECK_FALSE(SameD3D12BufferPatchLayout(previous, next));
  next = previous;
  next[0].data.push_back(5);
  CHECK_FALSE(SameD3D12BufferPatchLayout(previous, next));
  next = previous;
  next[0].buffer = ResourceIDGen::GetNewUniqueID();
  CHECK_FALSE(SameD3D12BufferPatchLayout(previous, next));
  CHECK_FALSE(SameD3D12BufferPatchLayout({}, previous));
  CHECK_FALSE(SameD3D12BufferPatchLayout(previous, {}));
  CHECK_FALSE(SameD3D12BufferPatchLayout({}, {}));

  CHECK(D3D12BufferPatchConsumerFromUsage(100, EventUsage(50, ResourceUsage::CopySrc)) == 50);
  CHECK(D3D12BufferPatchConsumerFromUsage(100, EventUsage(60, ResourceUsage::Copy)) == 60);
  CHECK(D3D12BufferPatchConsumerFromUsage(100, EventUsage(20, ResourceUsage::CopyDst)) == 100);
  CHECK(D3D12BufferPatchConsumerFromUsage(100, EventUsage(10, ResourceUsage::CPUWrite)) == 100);
  CHECK(D3D12BufferPatchConsumerFromUsage(100, EventUsage(90, ResourceUsage::CS_Constants)) == 90);
  const uint32_t cutoff = D3D12CheckpointConservativeWriteStart(false, false, 100);
  CHECK(cutoff == 100);
  CHECK_FALSE(99U >= cutoff);
  CHECK(100U >= cutoff);
  CHECK(D3D12CheckpointConservativeWriteStart(false, false, ~0U) == ~0U);
  CHECK(D3D12CheckpointConservativeWriteStart(true, false, 100) == 1);
  CHECK(D3D12CheckpointConservativeWriteStart(false, true, 100) == 1);
}

TEST_CASE("D3D12 slider dirty reuse rejects every missing safety condition",
          "[d3d12][replay][bufferpatch]")
{
  CHECK(KeepD3D12ValueEditDirtyProof(true, true, true, false, false));
  CHECK_FALSE(KeepD3D12ValueEditDirtyProof(false, true, true, false, false));
  CHECK_FALSE(KeepD3D12ValueEditDirtyProof(true, false, true, false, false));
  CHECK_FALSE(KeepD3D12ValueEditDirtyProof(true, true, false, false, false));
  CHECK_FALSE(KeepD3D12ValueEditDirtyProof(true, true, true, true, false));
  CHECK_FALSE(KeepD3D12ValueEditDirtyProof(true, true, true, false, true));
}

TEST_CASE("D3D12 checkpoint query storage accounts for shared allocations and active GPU sources",
          "[d3d12][replay][checkpoint-query]")
{
  D3D12ReplayCheckpointCache cache;
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  auto source = std::make_shared<D3D12ReplayCheckpointQueryData>();
  source->bytes = 65536;
  const D3D12ReplayCheckpointQuery first = {source, 0, {D3D12_QUERY_TYPE_TIMESTAMP, 5}};
  const D3D12ReplayCheckpointQuery second = {source, 8, {D3D12_QUERY_TYPE_TIMESTAMP, 8}};
  cache.checkpoints[10].queries[{heap, 0}] = first;
  cache.checkpoints[10].queries[{heap, 1}] = second;
  cache.checkpoints[20].queries[{heap, 0}] = first;
  cache.activeQueries[{heap, 1}] = second;
  CHECK(cache.AllocatedBytes() == 65536);
  cache.checkpoints.clear();
  CHECK(cache.AllocatedBytes() == 65536);    // submitted native copies still own this allocation
  D3D12ReplayCheckpoint next;
  auto replacement = std::make_shared<D3D12ReplayCheckpointQueryData>();
  replacement->bytes = 65536;
  next.queries[{heap, 0}] = {replacement, 0, {D3D12_QUERY_TYPE_TIMESTAMP, 25}};
  CHECK(cache.AllocatedBytes(&next) == 131072);
  cache.activeQueries.clear();
  CHECK(cache.AllocatedBytes(&next) == 65536);
  CHECK(cache.AllocatedBytes() == 0);
}

TEST_CASE("D3D12 persistent buffer edits accept only replay writes with installed patch hooks",
          "[d3d12][replay][buffer-patch]")
{
  CHECK(IsReplayBufferPatchCopy(D3D12Chunk::List_CopyBufferRegion));
  CHECK(IsReplayBufferPatchCopy(D3D12Chunk::List_CopyResource));
  CHECK(IsReplayBufferPatchCopy(D3D12Chunk::List_CopyTextureRegion));
  CHECK_FALSE(IsReplayBufferPatchCopy(D3D12Chunk::List_WriteBufferImmediate));
  CHECK_FALSE(IsReplayBufferPatchCopy(D3D12Chunk::List_ResolveQueryData));
  CHECK_FALSE(IsReplayBufferPatchCopy(D3D12Chunk::List_AtomicCopyBufferUINT));
  CHECK_FALSE(IsReplayBufferPatchCopy(D3D12Chunk::List_ExecuteIndirect));
}

TEST_CASE("D3D12 parameter edits accept mixed read-only rings and invalidate every consumer",
          "[d3d12][replay][buffer-patch]")
{
  for(ResourceUsage usage : {ResourceUsage::VertexBuffer, ResourceUsage::IndexBuffer,
                             ResourceUsage::VS_Constants, ResourceUsage::All_Constants,
                             ResourceUsage::VS_Resource, ResourceUsage::PS_Resource,
                             ResourceUsage::CS_Resource, ResourceUsage::All_Resource,
                             ResourceUsage::CopySrc})
  {
    INFO("read usage " << uint32_t(usage));
    CHECK(IsD3D12BufferPatchReadUsage(usage));
    CHECK(D3D12BufferPatchConsumerFromUsage(100, EventUsage(25, usage)) == 25);
    CHECK(D3D12BufferPatchConsumerFromUsage(10, EventUsage(25, usage)) == 10);
  }
  for(ResourceUsage usage : {ResourceUsage::StreamOut, ResourceUsage::VS_RWResource,
                             ResourceUsage::All_RWResource, ResourceUsage::Indirect,
                             ResourceUsage::Clear, ResourceUsage::Discard,
                             ResourceUsage::CopyDst, ResourceUsage::ResolveDst,
                             ResourceUsage::Shader, ResourceUsage(0xffffffffU)})
  {
    INFO("unproven/write usage " << uint32_t(usage));
    CHECK_FALSE(IsD3D12BufferPatchReadUsage(usage));
  }
}

TEST_CASE("D3D12 checkpoint initial sources cover each heap byte exactly once",
          "[d3d12][replay][checkpoint][restore-plan]")
{
  rdcarray<rdcpair<uint64_t, uint64_t>> ranges;
  CheckpointInitialCopyRanges(16, {}, ranges);
  REQUIRE(ranges.size() == 1);
  CHECK(ranges[0].first == 0);
  CHECK(ranges[0].second == 16);

  // Keep the original padding and other allocations around two saved placed buffers. Adjacent
  // or overlapping exclusions must not duplicate either the initial or checkpoint source.
  CheckpointInitialCopyRanges(16, {{1, 4}, {2, 4}, {4, 6}, {10, 12}}, ranges);
  REQUIRE(ranges.size() == 3);
  CHECK(ranges[0].first == 0);
  CHECK(ranges[0].second == 1);
  CHECK(ranges[1].first == 6);
  CHECK(ranges[1].second == 10);
  CHECK(ranges[2].first == 12);
  CHECK(ranges[2].second == 16);
  uint32_t writes[16] = {};
  for(const auto &range : ranges)
    for(uint64_t i = range.first; i < range.second; i++)
      writes[i]++;
  for(uint32_t i = 1; i < 6; i++)
    writes[i]++;    // checkpoint buffer A
  for(uint32_t i = 10; i < 12; i++)
    writes[i]++;    // checkpoint buffer B
  for(uint32_t count : writes)
    CHECK(count == 1);

  CheckpointInitialCopyRanges(16, {{0, 16}}, ranges);
  CHECK(ranges.empty());
  CheckpointInitialCopyRanges(0, {}, ranges);
  CHECK(ranges.empty());
  CheckpointInitialCopyRanges(16, {{0, 4}, {12, ~0ULL}}, ranges);
  REQUIRE(ranges.size() == 1);
  CHECK(ranges[0].first == 4);
  CHECK(ranges[0].second == 12);
}
#endif

void WrappedID3D12Device::GetCheckpointInitialDescriptorRanges(
    ResourceId heap, ResourceId initial, uint32_t count,
    rdcarray<D3D12DescriptorRestoreRange> &copies,
    rdcarray<D3D12DescriptorRestoreRange> &deferred)
{
  deferred.clear();
  if(!m_UseCheckpointInitialSources || !m_ReplayCheckpoints ||
     Process::GetEnvVariable("RENDERDOC_D3D12_ENABLE_CHECKPOINT_DESCRIPTOR_OVERLAY") != "1")
    return;
  auto &cache = *m_ReplayCheckpoints;
  auto point = cache.checkpoints.find(cache.restorePlan.eventId);
  if(point == cache.checkpoints.end())
    return;
  auto saved = point->second.heaps.find(heap);
  if(saved == point->second.heaps.end() || initial == ResourceId() ||
     saved->second->baseline != initial || saved->second->descriptorCount != count ||
     saved->second->ranges.empty())
    return;
  rdcarray<D3D12DescriptorRestoreRange> filtered;
  if(!D3D12SubtractDescriptorRanges(count, copies, saved->second->ranges, filtered))
    return;
  // The checkpoint's complete delta blocks are applied after initial contents. Avoid first
  // resetting those same blocks, preserving native descriptors already equal to the checkpoint.
  copies.swap(filtered);
  deferred = saved->second->ranges;
  cache.restorePlan.descriptorExclusions[heap] = deferred;
  cache.restorePlan.skippedInitialContents = true;
}

bool WrappedID3D12Device::UsesCheckpointInitialContents(ResourceId resource)
{
  if(!m_UseCheckpointInitialSources || !m_ReplayCheckpoints)
    return false;
  auto &cache = *m_ReplayCheckpoints;
  auto point = cache.checkpoints.find(cache.restorePlan.eventId);
  if(point == cache.checkpoints.end())
    return false;
  if(cache.restorePlan.fullClearResources.count(resource))
  {
    cache.restorePlan.skippedInitialContents = true;
    cache.deferredInitialResources.insert(resource);
    cache.residentExtents[resource] = 1;    // complete texture as one independently restored unit
    cache.residentVersions.Register(resource, 0, 1);
    const ReplayResourceAccessProof proof = {ReplayResourceFirstAccess::FullOverwrite, true, false};
    // All-ones is reserved for the shared capture initial texture version, independent of EID.
    cache.residentVersions.Plan(cache.ResidentRange(resource), {~0ULL, 0}, proof);
    return true;
  }
  auto version = point->second.resources.find(resource);
  if(version == point->second.resources.end())
    return false;
  cache.restorePlan.skippedInitialContents = true;
  cache.restorePlan.skippedResources++;
  cache.restorePlan.skippedResourceBytes += version->second->bytes;
  return true;
}

void WrappedID3D12Device::GetCheckpointInitialCopyRanges(
    ResourceId heap, uint64_t byteCount, rdcarray<rdcpair<uint64_t, uint64_t>> &ranges)
{
  ranges.clear();
  if(m_UseCheckpointInitialSources && m_ReplayCheckpoints)
  {
    auto &plan = m_ReplayCheckpoints->restorePlan;
    auto excluded = plan.heapExclusions.find(heap);
    if(plan.eventId && excluded != plan.heapExclusions.end())
    {
      CheckpointInitialCopyRanges(byteCount, excluded->second, ranges);
      uint64_t copied = 0;
      for(const auto &range : ranges)
        copied += range.second - range.first;
      plan.skippedInitialContents |= copied < byteCount;
      plan.skippedHeapBytes += byteCount - copied;
    }
  }
  if(ranges.empty() && byteCount &&
     !(m_UseCheckpointInitialSources && m_ReplayCheckpoints &&
       m_ReplayCheckpoints->restorePlan.eventId &&
       m_ReplayCheckpoints->restorePlan.heapExclusions.count(heap)))
    ranges.push_back({0, byteCount});

  if(m_UseReplayDirtyInitialContents)
  {
    auto found = m_ReplayDirtyHeapRanges.find(heap);
    rdcarray<rdcpair<uint64_t, uint64_t>> selected;
    if(found != m_ReplayDirtyHeapRanges.end())
      selected = IntersectReplayDirtyRanges(ranges, found->second);
    else if(m_ReplayDirtyResources.count(heap))
      selected = ranges;
    uint64_t original = 0, copied = 0;
    for(const auto &range : ranges)
      original += range.second - range.first;
    for(const auto &range : selected)
      copied += range.second - range.first;
    if(m_ReplayDirtyTiming)
      m_ReplayDirtyCopyStats[heap].skippedBytes += original - copied;
    if(!m_ReplayDirtyShadow)
      ranges.swap(selected);
  }
}

bool WrappedID3D12Device::ValidateReplayCheckpointRestore(uint32_t eventID, bool initialRestored,
                                                          rdcstr &reason)
{
  auto &cache = *m_ReplayCheckpoints;
  auto found = cache.checkpoints.find(eventID);
  if(found == cache.checkpoints.end())
  {
    reason = "Checkpoint was invalidated before restoration";
    return false;
  }
  const D3D12ReplayCheckpoint &checkpoint = found->second;
  for(const auto &entry : checkpoint.bufferAliases)
  {
    const auto &saved = *entry.second;
    const auto &component = saved.component;
    const auto known = cache.bufferAliasComponents.find(entry.first);
    WrappedID3D12Heap *heap = D3D12CheckpointOriginalHeap(GetResourceManager(), component.heap);
    if(!heap || !saved.gpu || !heap->GetUnwrappedWholeMemBuffer() ||
       GetResourceManager()->HasReplacement(component.heap) ||
       known == cache.bufferAliasComponents.end() || known->second.heap != component.heap ||
       known->second.begin != component.begin || known->second.end != component.end ||
       known->second.members != component.members || component.begin >= component.end ||
       component.end > heap->GetDesc().SizeInBytes ||
       saved.gpu->GetDesc().Width != component.end - component.begin ||
       !m_ReplaySparseGraphComplete || m_ReplaySparseComponentIndex.count(component.heap))
    {
      reason = "Checkpoint physical buffer alias identity or storage coverage changed";
      return false;
    }
    for(ResourceId member : component.members)
    {
      const auto resource = GetResourceList().find(member);
      if(resource == GetResourceList().end() || !resource->second->IsResident() ||
         GetResID(resource->second->GetHeap()) != component.heap ||
         resource->second->GetDesc().Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
         GetResourceManager()->HasReplacement(member) ||
         GetResourceManager()->GetUnreplacedID(member) != member || !checkpoint.states.count(member))
      {
        reason = "Checkpoint physical buffer alias member changed";
        return false;
      }
    }
  }
  if(!checkpoint.typedAliasMembers.empty())
  {
    std::set<ResourceId> currentMembers;
    for(const auto &entry : cache.typedAliasConflicts)
      currentMembers.insert(entry.first);
    if(currentMembers != checkpoint.typedAliasMembers)
    {
      reason = "Checkpoint texture alias closure changed";
      return false;
    }
    for(ResourceId member : checkpoint.typedAliasMembers)
    {
      const auto resource = GetResourceList().find(member);
      const auto savedState = checkpoint.states.find(member);
      if(resource == GetResourceList().end() || savedState == checkpoint.states.end() ||
         !resource->second->IsResident() || GetResourceManager()->HasReplacement(member) ||
         GetResourceManager()->GetUnreplacedID(member) != member ||
         !D3D12CheckpointOriginalHeap(GetResourceManager(), GetResID(resource->second->GetHeap())))
      {
        reason = "Checkpoint texture alias member identity changed";
        return false;
      }
      if(checkpoint.typedAliasOwners.count(member))
      {
        if(!checkpoint.resources.count(member) || !checkpoint.resources.at(member)->gpu)
        {
          reason = "Checkpoint active texture alias is missing its complete typed copy";
          return false;
        }
        for(ResourceId other : cache.typedAliasConflicts.at(member))
          if(checkpoint.typedAliasOwners.count(other))
          {
            reason = "Checkpoint active texture owners overlap";
            return false;
          }
      }
      else
      {
        const auto initial = m_InitialResourceStates.find(member);
        const auto actual = m_ResourceStates.find(member);
        if(checkpoint.resources.count(member) || initial == m_InitialResourceStates.end() ||
           !D3D12CheckpointStatesEqual(initial->second, savedState->second) ||
           (initialRestored &&
            (actual == m_ResourceStates.end() ||
             !D3D12CheckpointStatesEqual(actual->second, savedState->second))))
        {
          reason = "Inactive texture alias would need a hidden-layout state transition";
          return false;
        }
      }
    }
  }
  for(const auto &query : checkpoint.queries)
  {
    WrappedID3D12QueryHeap *heap =
        GetResourceManager()->GetResAs<WrappedID3D12QueryHeap>(query.first.first);
    if(!heap || GetResourceManager()->HasReplacement(query.first.first) ||
       !heap->CanCheckpointQuery(query.first.second, query.second.version.type) ||
       !query.second.data || !query.second.data->gpu ||
       query.second.offset > query.second.data->gpu->GetDesc().Width ||
       heap->GetResolveDataSize() > query.second.data->gpu->GetDesc().Width - query.second.offset)
    {
      reason = "Checkpoint completed query result identity changed";
      return false;
    }
  }
  for(const auto &resolve : checkpoint.queryResolves)
    if(checkpoint.queries.find(resolve.second) == checkpoint.queries.end())
    {
      reason = "Checkpoint query resolve has no saved slot value";
      return false;
    }
  for(const auto &heap : checkpoint.heaps)
  {
    WrappedID3D12DescriptorHeap *destination =
        GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(heap.first);
    if(!destination || GetResourceManager()->HasReplacement(heap.first) ||
       destination->GetNumDescriptors() != heap.second->descriptorCount ||
       destination->GetDesc().Type != heap.second->type)
    {
      reason = "Checkpoint descriptor heap identity changed";
      return false;
    }
    if(heap.second->baseline != ResourceId())
    {
      ResourceId baseline;
      rdcarray<D3D12DescriptorRestoreRange> dirty;
      if(initialRestored)
      {
        bool restored = destination->GetCheckpointDescriptorRanges(baseline, dirty);
        auto excluded = m_ReplayCheckpoints->restorePlan.descriptorExclusions.find(heap.first);
        if(restored && excluded != m_ReplayCheckpoints->restorePlan.descriptorExclusions.end())
        {
          rdcarray<D3D12DescriptorRestoreRange> remaining;
          restored = D3D12SubtractDescriptorRanges(destination->GetNumDescriptors(), dirty,
                                                   excluded->second, remaining);
          dirty.swap(remaining);
        }
        if(!restored || !dirty.empty())
        {
          reason = "Checkpoint descriptor baseline is not fully restored";
          return false;
        }
      }
      else
      {
        D3D12InitialContents initial = GetResourceManager()->GetInitialContents(heap.first);
        if(initial.resourceType == Resource_DescriptorHeap && initial.resource)
          baseline = GetResID(initial.resource);
      }
      if(baseline != heap.second->baseline)
      {
        reason = "Checkpoint descriptor initial source changed";
        return false;
      }
    }
  }
  for(const auto &entry : checkpoint.states)
  {
    const auto resource = GetResourceList().find(entry.first);
    const auto state = m_ResourceStates.find(entry.first);
    bool valid = resource != GetResourceList().end() && state != m_ResourceStates.end() &&
                 !GetResourceManager()->HasReplacement(entry.first) &&
                 state->second.size() == entry.second.size();
    if(valid)
      for(size_t i = 0; i < state->second.size(); i++)
        valid &= state->second[i].IsStates() && entry.second[i].IsStates();
    if(!valid)
    {
      reason = "Checkpoint resource barrier-state identity changed";
      return false;
    }
  }
  for(const auto &entry : checkpoint.resources)
  {
    const auto destination = GetResourceList().find(entry.first);
    if(destination == GetResourceList().end() ||
       checkpoint.states.find(entry.first) == checkpoint.states.end())
    {
      reason = "Checkpoint resource identity changed";
      return false;
    }
    const D3D12_RESOURCE_DESC desc = destination->second->GetReal()->GetDesc();
    if(entry.second->gpu)
    {
      const D3D12_RESOURCE_DESC source = entry.second->gpu->GetDesc();
      if(source.Dimension != desc.Dimension || source.Width != desc.Width ||
         source.Height != desc.Height || source.DepthOrArraySize != desc.DepthOrArraySize ||
         source.MipLevels != desc.MipLevels || source.Format != desc.Format ||
         source.SampleDesc.Count != desc.SampleDesc.Count ||
         source.SampleDesc.Quality != desc.SampleDesc.Quality)
      {
        reason = "Checkpoint resource copy shape changed";
        return false;
      }
    }
    else if(desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
            entry.second->cpu.size() != desc.Width)
    {
      reason = "Checkpoint CPU buffer copy size changed";
      return false;
    }
  }
  return true;
}

void WrappedID3D12Device::PrepareReplayCheckpointRestore(uint32_t requestedEvent,
                                                          ReplayLogType replayType,
                                                          bool allowCheckpoints)
{
  if(!m_ReplayCheckpoints)
    return;
  auto &cache = *m_ReplayCheckpoints;
  cache.restorePlan = {};
  cache.currentVersions.clear();
  cache.currentBufferAliases.clear();
  cache.savedThisReplay = false;
  if(!allowCheckpoints || !cache.config.enabled || m_Replay->m_ReplayCheckpointBypass ||
     m_Queue->GetCommandData()->m_ActionCallback || m_ReplayOptions.apiValidation)
    return;
  const uint32_t endEventID = replayType == eReplay_WithoutDraw ? RDCMAX(1U, requestedEvent) - 1
                                                              : requestedEvent;
  const uint32_t targetEvent = requestedEvent;
  auto found = cache.checkpoints.lower_bound(endEventID);
  bool usable = false;
  uint32_t limitedPoint = 0, firstResolve = ~0U;
  D3D12_QUERY_TYPE firstResolveType = D3D12_QUERY_TYPE_OCCLUSION;
  while(found != cache.checkpoints.begin())
  {
    --found;
    if(IsCheckpointQueryTargetSafe(targetEvent, found->second.firstNativeResolveEID))
    {
      usable = true;
      break;
    }
    limitedPoint = found->first;
    firstResolve = found->second.firstNativeResolveEID;
    firstResolveType = found->second.firstNativeResolveType;
  }
  if(!usable)
  {
    // Also explain a cold miss when query state prevents building the selected point for this
    // target. It can still be useful for an earlier request, so retain its candidate metadata.
    if(!limitedPoint)
      for(const auto &candidate : cache.selected)
        if(candidate.first < endEventID &&
           !IsCheckpointQueryTargetSafe(targetEvent, candidate.second.firstNativeResolveEID))
        {
          limitedPoint = candidate.first;
          firstResolve = candidate.second.firstNativeResolveEID;
          firstResolveType = candidate.second.firstNativeResolveType;
        }
    if(limitedPoint)
    {
      cache.stats.reason = StringFormat::Fmt(
          "Target crosses native query resolve at EID %u (%s) after checkpoint %u", firstResolve,
          ToStr(firstResolveType).c_str(), limitedPoint);
      if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
        LogD3D12Checkpoint(StringFormat::Fmt(
            "api=D3D12Checkpoint op=queryFallback event=%u target=%u firstResolve=%u queryType=%s",
            limitedPoint, targetEvent, firstResolve, ToStr(firstResolveType).c_str()));
    }
    cache.stats.misses++;
    return;
  }
  D3D12ReplayCheckpoint &checkpoint = found->second;
  PerformanceTimer timer;
  rdcstr reason;
  D3D12ReplayCheckpointCache::RestorePlan plan;
  plan.eventId = checkpoint.eventId;
  plan.queueEndEvent = endEventID;
  const auto reject = [&](const rdcstr &detail) {
    cache.stats.rejected++;
    cache.stats.misses++;
    cache.stats.reason = detail;
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      LogD3D12Checkpoint(StringFormat::Fmt(
          "api=D3D12Checkpoint op=planFallback event=%u target=%u reason=%s",
          checkpoint.eventId, targetEvent, detail.c_str()));
  };
  if(!ValidateReplayCheckpointRestore(checkpoint.eventId, false, reason))
  {
    reject(reason);
    return;
  }
  rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> continuation;
  if(!m_Queue->GetCommandData()->GetCheckpointContinuation(
         checkpoint.offset, checkpoint.nextEvent, continuation, reason, NULL,
         Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_TARGET_CONTINUATION") == "1" ? ~0U
                                                                                   : endEventID))
  {
    reject(reason);
    return;
  }
  uint64_t resourceBytes = 0;
  const bool retainResident = m_UseReplayDirtyInitialContents &&
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_CHECKPOINT_RESIDENT_REUSE") != "1";
  const bool initialSourcesEnabled =
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_CHECKPOINT_INITIAL_SOURCES") != "1";
  for(const auto &entry : checkpoint.bufferAliases)
  {
    const auto &component = entry.second->component;
    const D3D12InitialContents initial = GetResourceManager()->GetInitialContents(component.heap);
    if(initial.sparseBinds)
    {
      reject("Checkpoint buffer alias heap acquired sparse initial mappings");
      return;
    }
    plan.heapExclusions[component.heap].push_back({component.begin, component.end});
    resourceBytes += component.allocationBytes;
  }
  for(const auto &entry : checkpoint.resources)
  {
    WrappedID3D12Resource *resource = GetResourceList().find(entry.first)->second;
    const D3D12_RESOURCE_DESC desc = resource->GetReal()->GetDesc();
    const D3D12InitialContents initial = GetResourceManager()->GetInitialContents(entry.first);
    if(initial.sparseBinds ||
       (initial.resourceType == Resource_Resource &&
        initial.tag != D3D12InitialContents::Copy && initial.tag != D3D12InitialContents::ForceCopy))
    {
      reject("Checkpoint initial source needs sparse or non-copy restoration");
      return;
    }
    resourceBytes += entry.second->bytes;
    // This plan only retains exact immutable snapshot identities. It makes no assumption from
    // absent GetUsage entries, and all normal initial/checkpoint barriers still execute.
    const bool independentStorage =
        !m_ReplaySparseComponentIndex.count(entry.first) &&
        (!resource->GetHeap() || desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER) &&
        (!resource->GetHeap() || !m_ReplaySparseComponentIndex.count(GetResID(resource->GetHeap())));
    if(D3D12MayRetainCheckpointContents(initialSourcesEnabled, retainResident,
                                       independentStorage,
                                       cache.residentVersions.IsResident(
                                           cache.ResidentRange(entry.first),
                                           entry.second->contentVersion)))
    {
      plan.retainedResources.insert(entry.first);
      plan.retainedBytes += entry.second->bytes;
    }
    if(resource->GetHeap())
    {
      const ResourceId heapId = GetResID(resource->GetHeap());
      const D3D12InitialContents heapInitial = GetResourceManager()->GetInitialContents(heapId);
      if(heapInitial.resourceType == Resource_Heap &&
         (heapInitial.resource || heapInitial.srcData))
      {
        ID3D12Resource *wholeHeap = resource->GetHeap()->GetUnwrappedWholeMemBuffer();
        const uint64_t offset = resource->GetHeapOffset();
        if(!wholeHeap || heapInitial.sparseBinds)
        {
          reject("Checkpoint cannot separate this placed resource from its whole-heap initial copy");
          return;
        }
        if(desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER)
        {
          if(offset > wholeHeap->GetDesc().Width || desc.Width > wholeHeap->GetDesc().Width - offset)
          {
            reject("Checkpoint placed buffer exceeds its whole-heap initial copy");
            return;
          }
          plan.heapExclusions[heapId].push_back({offset, offset + desc.Width});
        }
        // An independent placed texture is restored by a full typed copy below. Keep its
        // original heap initialization: UNKNOWN layouts cannot supply byte-range exclusions.
        // Such textures are never retained across that physical initial copy.
      }
    }
    if(!entry.second->gpu)
    {
      // Verify every fallible CPU mapping before any initial or checkpoint contents are written.
      // Map again for the short actual copy; a later failure triggers a complete canonical reset.
      void *mapped = NULL;
      const D3D12_RANGE noRead = {0, 0};
      HRESULT hr = resource->GetReal()->Map(0, &noRead, &mapped);
      if(SUCCEEDED(hr))
        resource->GetReal()->Unmap(0, &noRead);
      if(FAILED(hr) || !mapped)
      {
        reject("Checkpoint CPU buffer preflight map failed");
        return;
      }
    }
  }
  uint64_t heapRanges = 0;
  // Narrow positive proof: a complete unconditional clear executes before any possible read.
  // This does not implement general absent-usage deferral or alias-heap snapshotting.
  if(m_UseReplayDirtyInitialContents && !m_CheckpointDependenciesEdited &&
     m_ReplayBufferPatches.empty() &&
     Process::GetEnvVariable("RENDERDOC_D3D12_CHECKPOINT_FULL_CLEAR_PREFIX") == "1")
  {
    // Filter native storage before the content query. Only these independent textures may
    // cross an alias barrier in that query; placed/sparse resources still use full restoration.
    std::set<ResourceId> eligibleTextures;
    for(const auto &resource : GetResourceList())
    {
      const ResourceId id = resource.first;
      if(resource.second->GetHeap() ||
         IsSparseResource(id) || m_ReplaySparseComponentIndex.count(id) ||
         GetResourceManager()->HasReplacement(id) ||
         resource.second->IsAccelerationStructureResource())
        continue;
      const D3D12_RESOURCE_DESC desc = resource.second->GetReal()->GetDesc();
      if(desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER)
        continue;
      const D3D12InitialContents initial = GetResourceManager()->GetInitialContents(id);
      if(initial.resourceType != Resource_Resource || initial.sparseBinds ||
         (initial.tag != D3D12InitialContents::Copy && initial.tag != D3D12InitialContents::ForceCopy))
        continue;
      eligibleTextures.insert(id);
    }
    for(ResourceId id : m_Queue->GetCommandData()->GetReplayFullClearPrefix(
            checkpoint.eventId, endEventID, eligibleTextures))
    {
      plan.fullClearResources.insert(id);
      const D3D12_RESOURCE_DESC desc = GetResourceList().find(id)->second->GetReal()->GetDesc();
      const uint64_t bytes = GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
      if(bytes != ~0ULL)
        plan.fullClearBytes += bytes;
    }
  }
  for(auto &heap : plan.heapExclusions)
  {
    std::sort(heap.second.begin(), heap.second.end());
    heapRanges += heap.second.size();
  }
  cache.restorePlan = std::move(plan);
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    LogD3D12Checkpoint(StringFormat::Fmt(
        "api=D3D12Checkpoint op=restorePlan event=%u target=%u resources=%zu resourceBytes=%llu "
        "heapExclusions=%llu continuationChunks=%zu total_ms=%.3f",
        checkpoint.eventId, requestedEvent, checkpoint.resources.size(),
        (unsigned long long)resourceBytes, (unsigned long long)heapRanges, continuation.size(),
        timer.GetMilliseconds()));
}

bool WrappedID3D12Device::RestoreReplayCheckpoint(uint32_t endEventID, uint64_t &offset,
                                                 uint32_t &nextEvent, uint32_t &nextAction)
{
  if(!m_ReplayCheckpoints || HasFatalError())
    return false;
  auto &cache = *m_ReplayCheckpoints;
  const auto fallback = [&](const rdcstr &reason) {
    const bool reset = cache.restorePlan.skippedInitialContents;
    bool resetCompleted = false;
    const uint32_t eventId = cache.restorePlan.eventId;
    cache.restorePlan = {};
    cache.currentVersions.clear();
    cache.currentBufferAliases.clear();
    cache.stats.reason = reason;
    cache.stats.rejected++;
    cache.stats.misses++;
    if(reset && !HasFatalError())
    {
      // BeginCaptureFrame has already put tracked states at the baseline. No checkpoint writes
      // have run on these recoverable failure paths, so complete the missing contents before
      // allowing the queue reader to replay its original stream from the beginning.
      if(!DeviceWaitForIdle())
        return false;
      ApplyInitialContents();
      ExecuteLists();
      if(!FlushLists(true) || !DeviceWaitForIdle())
        return false;
      resetCompleted = !HasFatalError();
    }
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      LogD3D12Checkpoint(StringFormat::Fmt(
          "api=D3D12Checkpoint op=restoreFallback event=%u target=%u resetRequired=%u "
          "canonicalReset=%u fatal=%u reason=%s", eventId, endEventID, reset ? 1U : 0U,
          resetCompleted ? 1U : 0U, HasFatalError() ? 1U : 0U, reason.c_str()));
    return false;
  };
  if(!cache.restorePlan.eventId)
    return false;
  if(!cache.config.enabled || m_Replay->m_ReplayCheckpointBypass ||
     m_Queue->GetCommandData()->m_ActionCallback || m_ReplayOptions.apiValidation ||
     cache.restorePlan.queueEndEvent != endEventID)
    return fallback("Checkpoint restore request changed after planning");
  if(Process::GetEnvVariable("RENDERDOC_D3D12_CHECKPOINT_FAIL_AFTER_INITIAL") == "1")
    return fallback("Injected checkpoint rejection after initial-source restoration");
  rdcstr reason;
  if(!ValidateReplayCheckpointRestore(cache.restorePlan.eventId, true, reason))
    return fallback(reason);
  D3D12ReplayCheckpoint &checkpoint = cache.checkpoints.find(cache.restorePlan.eventId)->second;
  checkpoint.lastUse = ++cache.serial;
  // Initial contents were restored only for resources/heap bytes absent from this checkpoint.
  // Initial barriers still establish a common state before each saved resource's one final copy.
  PerformanceTimer timer;
  bool overlapRestore = !ReplayDescriptorMutationNeedsWait() &&
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_ASYNC_CHECKPOINT_RESTORE") != "1";
  for(const auto &entry : checkpoint.resources)
    overlapRestore &= entry.second->gpu != NULL;
  // CPU upload restoration and AS initial builds can have additional VA/input dependencies.
  // Keep their established drain; the fast path is explicitly ordinary GPU copies only.
  if(overlapRestore)
    for(const auto &entry : GetResourceList())
      if(entry.second->IsAccelerationStructureResource())
      {
        overlapRestore = false;
        break;
      }
  if(!overlapRestore && !DeviceWaitForIdle())
    return false;
  const double preWaitEndMS = timer.GetMilliseconds();
  // Validate every fallible CPU map before writing any descriptor or resource. A failed map
  // can therefore complete a canonical initial-content reset without replaying its barriers.
  struct MappedRestore
  {
    ID3D12Resource *resource;
    void *pointer;
    const bytebuf *bytes;
  };
  rdcarray<MappedRestore> mappedResources;
  const auto unmapWithoutWrites = [&]() {
    const D3D12_RANGE range = {0, 0};
    for(const MappedRestore &mapped : mappedResources)
      mapped.resource->Unmap(0, &range);
  };
  for(const auto &entry : checkpoint.resources)
  {
    if(entry.second->gpu || cache.restorePlan.retainedResources.count(entry.first))
      continue;
    ID3D12Resource *resource = GetResourceList().find(entry.first)->second->GetReal();
    void *mapped = NULL;
    const D3D12_RANGE range = {0, 0};
    const HRESULT hr = resource->Map(0, &range, &mapped);
    if(FAILED(hr) || !mapped)
    {
      if(SUCCEEDED(hr))
        resource->Unmap(0, &range);
      unmapWithoutWrites();
      return fallback("Checkpoint CPU buffer map failed after initial-source planning");
    }
    mappedResources.push_back({resource, mapped, &entry.second->cpu});
  }
  const bool descriptorIndependent = m_InternalCmds.pendingcmds.empty();
  ID3D12GraphicsCommandListX *list = GetNewList();
  if(!list)
  {
    unmapWithoutWrites();
    return fallback("Checkpoint restore command-list allocation failed");
  }

  const double validateEndMS = timer.GetMilliseconds();
  uint64_t descriptorBytes = 0, descriptorChanges = 0, cpuBytes = 0;
  for(const auto &heap : checkpoint.heaps)
  {
    WrappedID3D12DescriptorHeap *destination =
        GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(heap.first);
    D3D12Descriptor *descriptors = destination->GetDescriptors();
    descriptorBytes += heap.second->descriptors.byteSize();
    size_t packed = 0;
    for(const D3D12DescriptorRestoreRange &range : heap.second->ranges)
    {
      for(uint32_t i = range.first; i < range.first + range.count; i++, packed++)
      {
        const D3D12Descriptor &source = heap.second->descriptors[packed];
        if(memcmp(&descriptors[i], &source, sizeof(D3D12Descriptor)))
        {
          descriptorChanges++;
          descriptors[i].CopyFrom(source);
          descriptors[i].Create(destination->GetDesc().Type, this, descriptors[i]);
          destination->MarkReplayDirtyRange(i, 1);
          destination->MarkMutableIndex(i);
        }
      }
    }
  }
  const double descriptorEndMS = timer.GetMilliseconds();
  for(const MappedRestore &mapped : mappedResources)
  {
    cpuBytes += mapped.bytes->size();
    memcpy(mapped.pointer, mapped.bytes->data(), mapped.bytes->size());
    const D3D12_RANGE range = {0, mapped.bytes->size()};
    mapped.resource->Unmap(0, &range);
  }
  const double cpuRestoreEndMS = timer.GetMilliseconds();
  for(const auto &entry : checkpoint.bufferAliases)
  {
    const auto &saved = *entry.second;
    WrappedID3D12Heap *heap = GetResourceManager()->GetResAs<WrappedID3D12Heap>(saved.component.heap);
    D3D12BufferAliasCopy(Unwrap(list), heap->GetUnwrappedWholeMemBuffer(), saved, true);
  }
  for(const auto &entry : checkpoint.resources)
  {
    if(!entry.second->gpu || cache.restorePlan.retainedResources.count(entry.first) ||
       cache.restorePlan.fullClearResources.count(entry.first))
      continue;
    WrappedID3D12Resource *resource = GetResourceList().find(entry.first)->second;
    if(resource->GetHeap())
      D3D12CheckpointAliasBarrier(
          Unwrap(list), checkpoint.typedAliasOwners.count(entry.first) ? resource->GetReal() : NULL);
    BarrierSet barriers;
    barriers.Configure(resource, m_ResourceStates[entry.first], BarrierSet::CopyDestAccess);
    barriers.Apply(list);
    Unwrap(list)->CopyResource(resource->GetReal(), entry.second->gpu);
    barriers.Unapply(list);
  }
  rdcarray<D3D12_RESOURCE_BARRIER> transitions;
  for(const auto &entry : checkpoint.states)
  {
    auto resource = GetResourceList().find(entry.first);
    SubresourceStateVector &before = m_ResourceStates[entry.first];
    if(checkpoint.typedAliasMembers.count(entry.first) &&
       !checkpoint.typedAliasOwners.count(entry.first))
      continue;    // preflight proved the canonical state exact; never touch inactive metadata here
    for(size_t i = 0; i < entry.second.size(); i++)
    {
      if(before[i] == entry.second[i])
        continue;
      D3D12_RESOURCE_BARRIER barrier = {};
      barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
      barrier.Transition.pResource = resource->second->GetReal();
      barrier.Transition.Subresource = (UINT)i;
      barrier.Transition.StateBefore = before[i].ToStates();
      barrier.Transition.StateAfter = entry.second[i].ToStates();
      transitions.push_back(barrier);
    }
    before = entry.second;
  }
  if(!transitions.empty())
    Unwrap(list)->ResourceBarrier((UINT)transitions.size(), transitions.data());
  const HRESULT closeResult = list->Close();
  CHECK_HR(this, closeResult);
  if(FAILED(closeResult))
  {
    InvalidateReplayDirtyTracking("checkpointRestoreCloseFailed");
    RDResult failure;
    SET_ERROR_RESULT(failure, ResultCode::APIReplayFailed,
                     "D3D12 checkpoint restore command list failed to close: %s",
                     ToStr(closeResult).c_str());
    ReportFatalError(failure);
    return false;
  }
  const double copyRecordEndMS = timer.GetMilliseconds();
  ExecuteLists(NULL, false, descriptorIndependent);
  // The replay boundary already drained the preceding replay. In the qualified path, in-flight
  // initial work does not read captured descriptor heaps, and no CPU upload bytes are changed.
  // Resource copies follow baseline barriers on the same internal queue. Publish a *new* fence
  // after these copies to every captured queue; earlier initial-state fences precede this work.
  // Retain submitted command lists/allocator until the normal replay retirement point.
  const bool asyncRestore = overlapRestore && InstallInitialStateQueueDependencies();
  if(!asyncRestore && !FlushLists(true))
    return false;
  const double submitWaitEndMS = timer.GetMilliseconds();
  if(HasFatalError())
    return false;
  // Restored bytes and barriers precede any resumed captured work through completion or GPU
  // dependencies. Queue chaining handles subsequent DIRECT/COMPUTE/COPY submissions normally.
  m_Queue->SyncLastReplayQueueToInternal();
  offset = checkpoint.offset;
  nextEvent = checkpoint.nextEvent;
  nextAction = checkpoint.nextAction;
  cache.currentVersions = checkpoint.resources;
  cache.currentBufferAliases = checkpoint.bufferAliases;
  cache.activeQueries = checkpoint.queries;
  cache.PublishResidentVersions(checkpoint);
  for(ResourceId id : cache.deferredInitialResources)
    cache.currentVersions.erase(id);
  cache.activeQueryResolves = checkpoint.queryResolves;
  cache.stats.hits++;
  cache.stats.lastResumeEvent = checkpoint.eventId;
  cache.stats.reason.clear();
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    uint64_t overlaySlots = 0;
    uint64_t aliasCopyBytes = 0;
    uint64_t typedCopyBytes = 0;
    for(const auto &entry : checkpoint.bufferAliases)
      aliasCopyBytes += entry.second->component.end - entry.second->component.begin;
    for(ResourceId id : checkpoint.typedAliasOwners)
      typedCopyBytes += checkpoint.resources.at(id)->bytes;
    for(const auto &heap : cache.restorePlan.descriptorExclusions)
      for(const auto &range : heap.second)
        overlaySlots += range.count;
    LogD3D12Checkpoint(StringFormat::Fmt(
        "api=D3D12Checkpoint op=restore event=%u target=%u prefixSkipped=1 total_ms=%.3f "
        "asyncRestore=%u preWait_ms=%.3f validate_ms=%.3f descriptors_ms=%.3f cpuRestore_ms=%.3f "
        "copyRecord_ms=%.3f submitWait_ms=%.3f descriptorBytes=%llu descriptorChanges=%llu "
        "cpuBytes=%llu "
        "initialResourceCopiesSkipped=%u initialResourceAllocationBytesSkipped=%llu "
        "initialHeapBytesSkipped=%llu restoredQuerySlots=%zu querySavedResolves=%zu "
        "residentCopiesSkipped=%zu residentAllocationBytesSkipped=%llu "
        "fullClearInitialCopiesDeferred=%zu fullClearAllocationBytes=%llu "
        "initialDescriptorOverlaySlots=%llu bufferAliasComponents=%zu bufferAliasCopyBytes=%llu "
        "typedAliasOwners=%zu typedAliasCopyAllocationBytes=%llu",
        checkpoint.eventId, endEventID, timer.GetMilliseconds(), asyncRestore ? 1U : 0U,
        preWaitEndMS, validateEndMS - preWaitEndMS, descriptorEndMS - validateEndMS,
        cpuRestoreEndMS - descriptorEndMS, copyRecordEndMS - cpuRestoreEndMS,
        submitWaitEndMS - copyRecordEndMS, (unsigned long long)descriptorBytes,
        (unsigned long long)descriptorChanges, (unsigned long long)cpuBytes,
        cache.restorePlan.skippedResources, (unsigned long long)cache.restorePlan.skippedResourceBytes,
        (unsigned long long)cache.restorePlan.skippedHeapBytes, checkpoint.queries.size(),
        checkpoint.queryResolves.size(), cache.restorePlan.retainedResources.size(),
        (unsigned long long)cache.restorePlan.retainedBytes, cache.deferredInitialResources.size(),
        (unsigned long long)cache.restorePlan.fullClearBytes, (unsigned long long)overlaySlots,
        checkpoint.bufferAliases.size(), (unsigned long long)aliasCopyBytes,
        checkpoint.typedAliasOwners.size(), (unsigned long long)typedCopyBytes));
  }
  cache.restorePlan = {};
  return true;
}

void WrappedID3D12Device::ReplayLog(uint32_t startEventID, uint32_t endEventID,
                                    ReplayLogType replayType, bool allowCheckpoints,
                                    bool knownShaderFeedback, bool knownOrdinaryTarget,
                                    bool naturalReference, bool ordinaryRoot,
                                    bool bufferMutationRoot, bool shaderMutationRoot)
{
  struct DescriptorWaitScope
  {
    bool &value;
    bool previous;
    explicit DescriptorWaitScope(bool &target) : value(target), previous(target) { value = false; }
    ~DescriptorWaitScope() { value = previous; }
  } descriptorScope(m_PreciseDescriptorWaitScope);
  m_DescriptorWaitAudit = Process::GetEnvVariable("RENDERDOC_D3D12_WAIT_AUDIT") == "1";
  m_ReplayDescriptorDecodeCacheAllowed =
      !ShouldUseConservativeReplaySync(true) &&
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_DESCRIPTOR_DECODE_CACHE") != "1";
  // The slot proof uses complete captured root-table ranges, not observed dynamic Shader
  // accesses or a parameter value. A validated persistent CBuffer overlay cannot change tables;
  // indirect signatures cannot set tables either. Unknown metadata still rejects in the proof.
  m_PreciseDescriptorWaitScope =
      (ordinaryRoot || bufferMutationRoot || shaderMutationRoot) && startEventID == 0 &&
      replayType == eReplay_WithoutDraw && !knownShaderFeedback && !m_ReplayDirtyDependenciesEdited &&
      !ShouldUseConservativeReplaySync() && m_InitParams.VendorExtensions == GPUVendor::Unknown &&
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_PRECISE_DESCRIPTOR_WAITS") != "1";
  if(HasFatalError())
    return;
  PerformanceTimer liveTimer;
  const bool rootRequest = startEventID == 0 && replayType == eReplay_WithoutDraw;
  const bool eligibleRoot = IsOrdinaryLiveForwardRoot(
      startEventID, replayType, ordinaryRoot, allowCheckpoints, naturalReference,
      m_Queue->GetCommandData()->m_ActionCallback != NULL);
  const bool publishRoot = eligibleRoot || CanPublishNaturalReferenceRoot(
                                               startEventID, replayType, naturalReference,
                                               m_Queue->GetCommandData()->m_ActionCallback != NULL);
  const uint32_t previousLiveEvent = m_LiveForward.eventId;
  const bool liveEnabled = Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_LIVE_FORWARD") != "1";
  bool liveForward = false;
  bool replaySucceeded = true;
  rdcstr liveReason = m_LiveForward.reason;
  if(eligibleRoot)
  {
    m_LiveForward.attempts++;
    const D3D12CommandData &cmd = *m_Queue->GetCommandData();
    if(!liveEnabled)
      liveReason = "disabled";
    else if(m_ReplayOptions.apiValidation || D3D12_Debug_SingleSubmitFlushing() ||
            D3D12_Debug_RT_Auditing() || m_InitParams.VendorExtensions != GPUVendor::Unknown)
      liveReason = "validationOrVendorMode";
    else if(!m_LiveForward.valid ||
            m_LiveForward.publishedGeneration != m_LiveForward.generation)
      liveReason = m_LiveForward.reason;
    else if(cmd.m_Partial[D3D12CommandData::Primary].partialParent != m_LiveForward.baked ||
            cmd.m_Partial[D3D12CommandData::Primary].baseEvent != m_LiveForward.baseEvent)
      liveReason = "primaryStateChanged";
    else
      liveForward = cmd.ValidateLiveForwardRange(m_LiveForward.baked, m_LiveForward.baseEvent,
                                                 previousLiveEvent, endEventID, liveReason);
    if(liveForward)
    {
      startEventID = previousLiveEvent + 1;
      m_LiveForward.hits++;
      liveReason = "samePrimarySubmission";
    }
    else
      m_LiveForward.fallbacks++;
    // Consume the old state before any new commands are recorded. Only a later successful
    // ordinary OnlyDraw can publish the next live point; early returns cannot leave it valid.
    m_LiveForward.valid = false;
    m_LiveForward.pendingTarget = 0;
  }
  else if(!(replayType == eReplay_OnlyDraw && (knownOrdinaryTarget || knownShaderFeedback)))
  {
    InvalidateLiveForward("forceOrAnalysisReplay");
    liveReason = "forceOrAnalysisReplay";
  }
  // The feedback whitelist executes the original target with an extra debug UAV/statistics
  // query. Arbitrary analysis/callback shaders can redirect writes and invalidate this proof.
  const bool trackedReplay = D3D12ReplayPreservesDirtyProof(
      replayType, allowCheckpoints, knownShaderFeedback, knownOrdinaryTarget,
      m_Queue->GetCommandData()->m_ActionCallback != NULL, m_ReplayDirtyDependenciesEdited,
      naturalReference || bufferMutationRoot || shaderMutationRoot);
  if(!trackedReplay)
  {
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      AppendReplayTimingLog(StringFormat::Fmt(
          "api=D3D12DirtyProofRejected type=%u checkpoints=%u feedback=%u target=%u callback=%u "
          "edited=%u natural=%u bufferMutation=%u",
          (uint32_t)replayType, allowCheckpoints ? 1U : 0U, knownShaderFeedback ? 1U : 0U,
          knownOrdinaryTarget ? 1U : 0U, m_Queue->GetCommandData()->m_ActionCallback ? 1U : 0U,
          m_CheckpointDependenciesEdited ? 1U : 0U, naturalReference ? 1U : 0U,
          bufferMutationRoot ? 1U : 0U));
    InvalidateReplayDirtyTracking("analysisOrMutation");
  }
  if(allowCheckpoints && m_ReplayCheckpoints)
    m_ReplayCheckpoints->targetEvent = endEventID;
  bool partial = true;

  m_WaitedForIdleAfterReplay = false;

  if(startEventID == 0 && (replayType == eReplay_WithoutDraw || replayType == eReplay_Full))
  {
    startEventID = 1;
    partial = false;
  }

  ScopedReplayTiming replayTiming("D3D12", startEventID, endEventID, replayType, partial);

  if(!partial)
  {
    ScopedReplayTimingContext timingContext(ReplayTimingContext::ReplayReset);
    m_WFICounter++;

    if(!DeviceWaitForIdle())
      return;

    // I'm not sure the reason for this, but the debug layer warns about being unable to resubmit
    // command lists due to the 'previous queue fence' not being ready yet, even if no fences are
    // signalled or waited. So instead we just signal a dummy fence each new 'frame'
    for(size_t i = 0; i < m_Queues.size(); i++)
      CHECK_HR(this, m_Queues[i]->Signal(m_QueueFences[i], m_WFICounter));

    if(!FlushLists(true))
      return;
    if(m_ReplayCheckpoints)
    {
      m_ReplayCheckpoints->activeQueryResolves.clear();
      m_ReplayCheckpoints->activeQueries.clear();
    }
    m_CurDataUpload = 0;
    m_DataUploadPending = false;

    {
      ScopedReplayTimingSection timing(ReplayTimingSection::ResetAllocators);
      // take this opportunity to reset command allocators to ensure we don't steadily leak over time.
      if(m_DataUploadAlloc)
        CHECK_HR(this, m_DataUploadAlloc->Reset());

      for(ID3D12CommandAllocator *alloc : m_CommandAllocators)
        CHECK_HR(this, alloc->Reset());
    }

    if(HasFatalError())
      return;
  }

  if(!partial)
  {
    if(ordinaryRoot)
      m_Replay->BeginGPUPhaseAudit(endEventID);
    BeginReplayDirtyRestore(trackedReplay);
    PrepareReplayCheckpointRestore(endEventID, replayType, allowCheckpoints);
    ScopedReplayTimingContext timingContext(ReplayTimingContext::InitialRestore);
    const bool descriptorIndependent = m_InternalCmds.pendingcmds.empty();
    {
      D3D12MarkerRegion apply(GetQueue(), "!!!!RenderDoc Internal: ApplyInitialContents");
      {
        ScopedReplayTimingSection timing(ReplayTimingSection::ApplyInitial);
        ApplyInitialContents(Process::GetEnvVariable(
                                 "RENDERDOC_D3D12_DISABLE_CHECKPOINT_INITIAL_SOURCES") != "1");
      }
    }

    ExecuteLists(NULL, false, descriptorIndependent);

    // The initial-state command lists use the shared internal allocator. Do not flush or reset it
    // here: closed command lists may remain submitted while subsequent lists are recorded, and a
    // later normal FlushLists() (typically presentation) retires the whole batch. A following full
    // replay starts with DeviceWaitForIdle(), so its existing FlushLists() also retires this batch
    // before resetting the allocator.
    //
    // Validation and auditing modes intentionally retain the conservative CPU synchronisation.
    // If any queue dependency cannot be installed, fail closed and use the old full waits.
    const bool deferInitialStateWait =
        !m_ReplayOptions.apiValidation &&
        Process::GetEnvVariable("RENDERDOC_D3D12_FORCE_INITIAL_COMPLETION") != "1" &&
        !D3D12_Debug_SingleSubmitFlushing() && !D3D12_Debug_RT_Auditing();
    if(!deferInitialStateWait || !InstallInitialStateQueueDependencies())
    {
      ScopedReplayTimingContext fallbackContext(ReplayTimingContext::InitialFallback);
      if(!FlushLists(true) || !DeviceWaitForIdle())
        return;
    }

    // Only now have the canonical initial commands actually been submitted and made visible to
    // every following replay queue. Subsequent captured writes/mappings invalidate these seeds.
    CompleteReplaySparseInitialBaselines();
    if(ordinaryRoot)
      m_Replay->MarkGPUPhaseAudit(1);

    // clear any previous ray dispatch references
    D3D12CommandData &cmd = *m_Queue->GetCommandData();

    for(PatchedRayDispatch &r : cmd.m_RayDispatches)
    {
      GetResourceManager()->GetRTManager()->Verify(r);
      r.resources.Release();
    }
    cmd.m_RayDispatches.clear();

    if(HasFatalError())
      return;
  }

  m_State = CaptureState::ActiveReplaying;

  D3D12MarkerRegion::Set(
      GetQueue(), StringFormat::Fmt("!!!!RenderDoc Internal: RenderDoc Replay %d (%d): %u->%u",
                                    (int)replayType, (int)partial, startEventID, endEventID));

  if(!partial)
  {
    const bool descriptorIndependent = m_InternalCmds.pendingcmds.empty();
    ID3D12GraphicsCommandList *beginList = GetNewList();
    if(!beginList)
      return;

    {
      rdcwstr text = StringFormat::UTF82Wide(AMDRGPControl::GetBeginMarker());
      UINT size = UINT(text.length() * sizeof(wchar_t));
      beginList->SetMarker(0, text.c_str(), size);
    }

    CHECK_HR(this, beginList->Close());
    ExecuteLists(NULL, false, descriptorIndependent);

    if(HasFatalError())
      return;
  }

  {
    D3D12CommandData &cmd = *m_Queue->GetCommandData();

    if(!partial)
    {
      cmd.m_Partial[D3D12CommandData::Primary].Reset();
      cmd.m_Partial[D3D12CommandData::Secondary].Reset();
      cmd.m_RenderState = D3D12RenderState();
      cmd.m_RenderState.m_ResourceManager = GetResourceManager();
      cmd.m_RenderState.m_DebugManager = m_Replay->GetDebugManager();
    }
    else
    {
      // Copy the state in case m_RenderState was modified externally for the partial replay.
      ResourceId cmdList = cmd.m_Partial[D3D12CommandData::Primary].partialParent;
      if(cmdList != ResourceId())
        cmd.m_BakedCmdListInfo[cmdList].state = cmd.m_RenderState;
    }

    // we'll need our own command list if we're replaying just a subsection
    // of events within a single command list record - always if it's only
    // one action, or if start event ID is > 0 we assume the outside code
    // has chosen a subsection that lies within a command list
    if(partial)
    {
      ID3D12GraphicsCommandListX *list = cmd.m_OutsideCmdList = GetNewList();

      cmd.m_BakedCmdListInfo[GetResID(cmd.m_OutsideCmdList)].barriers.clear();

      if(!list)
        return;

      cmd.m_RenderState.ApplyState(this, list);
    }

    RDResult result = ResultCode::Succeeded;
    const bool ordinaryPreparedPrefix =
        replayType == eReplay_WithoutDraw && ordinaryRoot && m_ReplayBufferPatches.empty() &&
        Process::GetEnvVariable("RENDERDOC_D3D12_ENABLE_PREFIX_COMMAND_REUSE") == "1";
    const bool allowPreparedReplay = (replayType == eReplay_Full || ordinaryPreparedPrefix) &&
                                     !partial && !m_ReplayOptions.apiValidation &&
                                     !D3D12_Debug_SingleSubmitFlushing() &&
                                     !D3D12_Debug_RT_Auditing();

    {
      ScopedReplayTimingContext timingContext(ReplayTimingContext::ContextReplay);
      ScopedReplayTimingSection timing(ReplayTimingSection::ContextReplay);

      if(replayType == eReplay_Full)
        result = m_Queue->ReplayLog(m_State, startEventID, endEventID, partial,
                                    allowPreparedReplay, allowCheckpoints);
      else if(replayType == eReplay_WithoutDraw)
        result = m_Queue->ReplayLog(m_State, startEventID, RDCMAX(1U, endEventID) - 1, partial,
                                    allowPreparedReplay, allowCheckpoints);
      else if(replayType == eReplay_OnlyDraw)
        result = m_Queue->ReplayLog(m_State, endEventID, endEventID, partial);
      else
        RDCFATAL("Unexpected replay type");
    }

    RDCASSERTEQUAL(result.code, ResultCode::Succeeded);
    if(result != ResultCode::Succeeded)
    {
      replaySucceeded = false;
      InvalidateLiveForward("replayFailed");
      InvalidateReplayDirtyTracking("replayFailed");
    }

    if(cmd.m_OutsideCmdList != NULL)
    {
      // OnlyDraw still conservatively includes the already executed prefix, but must not dirty
      // every future action in this baked list. Keep the original requested endpoint: an indirect
      // handler may have advanced its mutable event cursor past the whole expanded group.
      cmd.MarkReplaySubmittedWrites(cmd.m_Partial[D3D12CommandData::Primary].partialParent,
                                     cmd.m_Partial[D3D12CommandData::Primary].baseEvent, endEventID);
      if(replayType == eReplay_OnlyDraw || liveForward)
        ApplyBarriers(cmd.m_BakedCmdListInfo[GetResID(cmd.m_OutsideCmdList)].barriers);

      ID3D12GraphicsCommandList *list = cmd.m_OutsideCmdList;

      CHECK_HR(this, list->Close());

      // The partial list is submitted on the internal queue. If the serialised stream most
      // recently submitted another captured queue, preserve that dependency on the GPU first.
      m_Queue->SyncLastReplayQueueToInternal();
      ExecuteLists();

      cmd.m_OutsideCmdList = NULL;
    }

    if(HasFatalError())
      return;

    ResourceId cmdList = cmd.m_Partial[D3D12CommandData::Primary].partialParent;
    if(cmdList != ResourceId())
      cmd.m_RenderState = cmd.m_BakedCmdListInfo[cmdList].state;
    else
      cmd.m_RenderState = D3D12RenderState();
    cmd.m_RenderState.ResolvePendingIndirectState(this);

    if(D3D12_Debug_SingleSubmitFlushing())
    {
      ScopedReplayTimingContext timingContext(ReplayTimingContext::DebugSingleSubmit);
      if(!FlushLists(true) || HasFatalError())
        return;
    }
  }

  // The serialised stream may finish on any captured DIRECT/COMPUTE/COPY queue. The final barrier,
  // GPU snapshot copy and RenderTexture operations all use the internal queue, so chain that queue
  // after the full replay before submitting any of them.
  ScopedReplayTimingContext timingContext(ReplayTimingContext::ReplayFinish);
  m_Queue->SyncLastReplayQueueToInternal();

  D3D12MarkerRegion::Set(GetQueue(), "!!!!RenderDoc Internal: Done replay");

  // ensure all UAV writes have finished before subsequent work
  ID3D12GraphicsCommandList *list = GetNewList();

  if(list)
  {
    {
      rdcwstr text = StringFormat::UTF82Wide(AMDRGPControl::GetEndMarker());
      UINT size = UINT(text.length() * sizeof(wchar_t));
      list->SetMarker(0, text.c_str(), size);
    }

    D3D12_RESOURCE_BARRIER uavBarrier = {};
    uavBarrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
    list->ResourceBarrier(1, &uavBarrier);

    CHECK_HR(this, list->Close());

    ExecuteLists();
  }

  if(!list)
  {
    InvalidateLiveForward("replayFinishListMissing");
    InvalidateReplaySparseInitialBaselines("replayFinishListMissing");
  }
  if(publishRoot && list && !HasFatalError() && replaySucceeded)
  {
    m_LiveForward.pendingTarget = endEventID;
    m_LiveForward.pendingGeneration = m_LiveForward.generation;
  }
  if(rootRequest && Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12LiveForward from=%u target=%u enabled=%u hit=%u reason=%s "
        "baked=%s base=%u generation=%llu attempts=%llu hits=%llu fallbacks=%llu "
        "initialRestoreSkipped=%u replayedEvents=%u total_ms=%.3f",
        previousLiveEvent, endEventID, liveEnabled ? 1U : 0U, liveForward ? 1U : 0U,
        liveReason.c_str(), ToStr(m_LiveForward.baked).c_str(), m_LiveForward.baseEvent,
        (unsigned long long)m_LiveForward.generation, (unsigned long long)m_LiveForward.attempts,
        (unsigned long long)m_LiveForward.hits, (unsigned long long)m_LiveForward.fallbacks,
        liveForward ? 1U : 0U, liveForward ? endEventID - previousLiveEvent - 1 : 0,
        liveTimer.GetMilliseconds());
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

void WrappedID3D12Device::ReplayDraw(ID3D12GraphicsCommandListX *cmd, const ActionDescription &action)
{
  InvalidateLiveForward("analysisReplayDraw");
  InvalidateReplayDirtyTracking("analysisReplayDraw");
  if(action.drawIndex == 0)
  {
    if(action.flags & ActionFlags::MeshDispatch)
    {
      cmd->DispatchMesh(action.dispatchDimension[0], action.dispatchDimension[1],
                        action.dispatchDimension[2]);
    }
    else if(action.flags & ActionFlags::Indexed)
    {
      RDCASSERT(action.flags & ActionFlags::Drawcall);
      cmd->DrawIndexedInstanced(action.numIndices, action.numInstances, action.indexOffset,
                                action.baseVertex, action.instanceOffset);
    }
    else
    {
      RDCASSERT(action.flags & ActionFlags::Drawcall);
      cmd->DrawInstanced(action.numIndices, action.numInstances, action.vertexOffset,
                         action.instanceOffset);
    }
  }
  else
  {
    // TODO: support replay of draws not in callback
    D3D12CommandData *cmdData = m_Queue->GetCommandData();
    RDCASSERT(cmdData->m_IndirectData.commandSig != NULL);
    RDCASSERT(cmdData->m_IndirectData.argsBuffer != NULL);
    cmd->ExecuteIndirect(cmdData->m_IndirectData.commandSig, 1, cmdData->m_IndirectData.argsBuffer,
                         cmdData->m_IndirectData.argsOffset, NULL, 0);
  }
}
