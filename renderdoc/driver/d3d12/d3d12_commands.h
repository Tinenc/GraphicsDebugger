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

#pragma once

#include <functional>
#include "common/common.h"
#include "replay/replay_resource_versions.h"
#include "d3d12_common.h"
#include "d3d12_device.h"
#include "d3d12_resources.h"
#include "d3d12_state.h"

// Test the caller's original target, including a later OnlyDraw after WithoutDraw. A resolve at
// the target itself already needs native query state that an ordinary resource checkpoint lacks.
inline bool IsCheckpointQueryTargetSafe(uint32_t requestedEvent, uint32_t firstNativeResolveEID)
{
  return firstNativeResolveEID == ~0U || requestedEvent < firstNativeResolveEID;
}

struct D3D12ExecuteData
{
  ID3D12Resource *argBuf = NULL;
  ID3D12Resource *countBuf = NULL;
  uint64_t argOffs = 0;
  uint64_t countOffs = 0;
  WrappedID3D12CommandSignature *sig = NULL;
  UINT maxCount = 0;
  UINT reservedCount = 0;
  ResourceId sourceArgs, sourceCount;
  uint64_t sourceArgsOffset = 0, sourceCountOffset = 0;
  bool countResolved = false;
};

struct D3D12EventNode
{
  D3D12EventNode() = default;
  ~D3D12EventNode() { SAFE_DELETE(state); }
  D3D12EventNode(const D3D12EventNode &other) { *this = other; }
  D3D12EventNode &operator=(const D3D12EventNode &a)
  {
    if(this == &a)
      return *this;
    SAFE_DELETE(state);
    event = a.event;
    action = a.action;
    if(a.state)
    {
      state = new D3D12RenderState();
      *state = *a.state;
    }
    executeData = a.executeData;
    resourceUsage = a.resourceUsage;
    debugMessages = a.debugMessages;
    annotations = a.annotations;
    cmdListID = a.cmdListID;
    primaryCmdId = a.primaryCmdId;
    childCmdBufId = a.childCmdBufId;
    addActionUse = a.addActionUse;
    addPrimaryExecute = a.addPrimaryExecute;
    hasExecuteData = a.hasExecuteData;
    return *this;
  }

  // eventId is not used
  APIEvent event;

  ActionDescription action;

  D3D12RenderState *state = NULL;

  D3D12ExecuteData executeData;

  rdcarray<rdcpair<ResourceId, ResourceUsage>> resourceUsage;

  // eventId is not used
  rdcarray<DebugMessage> debugMessages;
  rdcarray<PendingAnnotation> annotations;

  ResourceId cmdListID;
  ResourceId primaryCmdId;
  ResourceId childCmdBufId;
  bool addActionUse = false;
  bool addPrimaryExecute = false;
  bool hasExecuteData = false;
};

struct D3D12ActionCallback
{
  // the three callbacks are used to allow the callback implementor to either
  // do a modified draw before or after the real thing.
  //
  // PreDraw()
  // do draw call as specified by the log
  // PostDraw()
  // if PostDraw() returns true:
  //   do draw call again
  //   PostRedraw()
  //
  // So either the modification happens in PreDraw, the modified draw happens,
  // then in PostDraw() the implementation can elect to undo the modifications
  // and do the real draw by returning true. OR they can do nothing in PreDraw,
  // do the real draw, then in PostDraw return true to apply the modifications
  // which are then undone in PostRedraw.
  virtual void PreDraw(uint32_t eid, ID3D12GraphicsCommandListX *cmd) = 0;
  virtual bool PostDraw(uint32_t eid, ID3D12GraphicsCommandListX *cmd) = 0;
  virtual void PostRedraw(uint32_t eid, ID3D12GraphicsCommandListX *cmd) = 0;

  // same principle as above, but for dispatch calls
  virtual void PreDispatch(uint32_t eid, ID3D12GraphicsCommandListX *cmd) = 0;
  virtual bool PostDispatch(uint32_t eid, ID3D12GraphicsCommandListX *cmd) = 0;
  virtual void PostRedispatch(uint32_t eid, ID3D12GraphicsCommandListX *cmd) = 0;

  // finally, these are for copy/blit/resolve/clear/etc
  virtual void PreMisc(uint32_t eid, ActionFlags flags, ID3D12GraphicsCommandListX *cmd) = 0;
  virtual bool PostMisc(uint32_t eid, ActionFlags flags, ID3D12GraphicsCommandListX *cmd) = 0;
  virtual void PostRemisc(uint32_t eid, ActionFlags flags, ID3D12GraphicsCommandListX *cmd) = 0;

  // called immediately before a command list is closed
  virtual void PreCloseCommandList(ID3D12GraphicsCommandListX *cmd) = 0;
  // if a command list is recorded once and submitted N > 1 times, then the same
  // action will have several EIDs that refer to it. We'll only do the full
  // callbacks above for the first EID, then call this function for the others
  // to indicate that they are the same.
  virtual void AliasEvent(uint32_t primary, uint32_t alias) = 0;

  // helper functions to downcast command list because we know it's wrapped
  void PreDraw(uint32_t eid, ID3D12GraphicsCommandList *cmd)
  {
    return PreDraw(eid, (ID3D12GraphicsCommandListX *)cmd);
  }
  bool PostDraw(uint32_t eid, ID3D12GraphicsCommandList *cmd)
  {
    return PostDraw(eid, (ID3D12GraphicsCommandListX *)cmd);
  }
  void PostRedraw(uint32_t eid, ID3D12GraphicsCommandList *cmd)
  {
    return PostRedraw(eid, (ID3D12GraphicsCommandListX *)cmd);
  }
  void PreDispatch(uint32_t eid, ID3D12GraphicsCommandList *cmd)
  {
    return PreDispatch(eid, (ID3D12GraphicsCommandListX *)cmd);
  }
  bool PostDispatch(uint32_t eid, ID3D12GraphicsCommandList *cmd)
  {
    return PostDispatch(eid, (ID3D12GraphicsCommandListX *)cmd);
  }
  void PostRedispatch(uint32_t eid, ID3D12GraphicsCommandList *cmd)
  {
    return PostRedispatch(eid, (ID3D12GraphicsCommandListX *)cmd);
  }
  void PreCloseCommandList(ID3D12GraphicsCommandList *cmd)
  {
    return PreCloseCommandList((ID3D12GraphicsCommandListX *)cmd);
  }
};

class WrappedID3D12CommandSignature;
struct AccStructPatchInfo;

enum class PreparedReplayRejectReason : uint32_t
{
  None,
  UnsupportedChunk,
  InterleavedChunk,
  InterleavedReset,
  APICommand,
  IncompleteRange,
  NativeQueries,
};

// The native query commands can be retained with a complete command list. Validate paired query
// scopes at load time so the prepared path never relies on Close repairing a truncated scope.
struct D3D12PreparedQueryTracker
{
  struct Query
  {
    ResourceId heap;
    D3D12_QUERY_TYPE type;
    UINT index;
  };
  rdcarray<Query> pending;

  bool Begin(ResourceId heap, D3D12_QUERY_TYPE type, UINT index);
  bool End(ResourceId heap, D3D12_QUERY_TYPE type, UINT index);
  bool Complete() const { return pending.empty(); }
  void Reset() { pending.clear(); }
};

struct BakedCmdListInfo
{
  ~BakedCmdListInfo() {}

  SubresourceStateVector GetState(WrappedID3D12Device *device, ResourceId id);

  rdcarray<D3D12EventNode> eventNodes;

  rdcarray<PendingAnnotation> pendingAnnotations;

  struct PatchRaytracing
  {
    bool m_patched = false;
    uint32_t numDescs = 0;
    D3D12GpuBuffer *patchedInstanceBuffer = NULL;
    D3D12GpuBuffer *unpatchedInstanceBufferReadback = NULL;
    ResourceId destinationAS;
  };

  rdcflatmap<uint32_t, PatchRaytracing> m_patchRaytracingInfo;

  struct OutstandingQuery
  {
    ID3D12QueryHeap *heap;
    D3D12_QUERY_TYPE Type;
    UINT Index;

    bool operator==(const OutstandingQuery &q) const
    {
      return heap == q.heap && Type == q.Type && Index == q.Index;
    }
  };

  rdcarray<OutstandingQuery> m_OutstandingQueries;

  ResourceId allocator;
  D3D12_COMMAND_LIST_TYPE type;
  UINT nodeMask;
  D3D12RenderState state;

  BarrierSet barriers;

  ResourceId parentList;

  // modified during recording to ensure we end any markers that should be ended but weren't due to
  // a partial replay
  int markerCount;

  uint32_t beginChunk = 0;
  uint32_t endChunk = 0;

  // A prepared replay entry may skip the serialised Reset..Close range only when loading proved
  // that range is contiguous and contains no commands whose replay allocates transient resources
  // or requires CPU-side callbacks. These offsets refer to the persistent frame reader.
  bool preparedReplaySafe = false;
  uint64_t preparedReplayBegin = 0;
  uint64_t preparedReplayEnd = 0;

  // First blocker only: diagnose why an otherwise eligible DIRECT list cannot be cached without
  // adding per-command work to active replay or retaining a second copy of its command stream.
  PreparedReplayRejectReason preparedReplayRejectReason = PreparedReplayRejectReason::None;
  D3D12Chunk preparedReplayRejectChunk = D3D12Chunk::List_Reset;
  uint64_t preparedReplayRejectOffset = 0;
  ResourceId preparedReplayRejectList;
  D3D12PreparedQueryTracker preparedReplayQueries;

  struct CheckpointQueryOp
  {
    ResourceId heap;
    uint32_t index = 0, count = 1, relativeEvent = 0;
    bool resolve = false;
    D3D12_QUERY_TYPE type = D3D12_QUERY_TYPE_OCCLUSION;
    // Loading proves a timestamp End, or a matched Begin/End in this command list.
    bool completed = false;
  };
  rdcarray<CheckpointQueryOp> checkpointQueries;
  struct CheckpointBarrierOp
  {
    ResourceId before, after;
    uint32_t relativeEvent = 0, subresource = 0;
    D3D12_RESOURCE_BARRIER_TYPE type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    D3D12_RESOURCE_BARRIER_FLAGS flags = D3D12_RESOURCE_BARRIER_FLAG_NONE;
    D3D12_RESOURCE_STATES stateBefore = D3D12_RESOURCE_STATE_COMMON;
    D3D12_RESOURCE_STATES stateAfter = D3D12_RESOURCE_STATE_COMMON;
  };
  rdcarray<CheckpointBarrierOp> checkpointBarriers;
  // Owner proof needs ordinary transitions too: they may touch a texture's hidden layout.
  // Preserve the original barrier-array order independently of the existing split/alias index.
  rdcarray<CheckpointBarrierOp> replayTypedBarriers;
  bool replayTypedBarriersComplete = true;
  // Content-only proof: unsafe flags/types may not enter checkpointBarriers (e.g. an unknown
  // barrier type or a filtered NULL transition). Keep their event identity without reading the
  // native barrier union. This does not change checkpoint or continuation eligibility.
  rdcarray<uint32_t> replayClearUnsafeBarriers;
  static bool IsReplayClearBarrierReadFree(D3D12_RESOURCE_BARRIER_TYPE type,
                                          D3D12_RESOURCE_BARRIER_FLAGS flags);
  struct ReplayClearOp
  {
    ResourceId resource;
    uint32_t relativeEvent = 0;
    bool unconditional = false;
    bool fullResource = false;
  };
  // Exact consumed RTV/DSV coverage from loading, independent of coarse EventUsage::Clear.
  // A partial or predicated clear stays in the index so it cannot masquerade as read-free work.
  rdcarray<ReplayClearOp> replayClearOps;
  struct ReplayTextureCopyOp
  {
    ResourceId source, destination;
    uint32_t relativeEvent = 0;
    bool unconditional = false, fullResource = false;
  };
  // Explicit source and destination identity. Whole-resource initialization is a positive
  // native coverage proof, not inferred from ResourceUsage::CopyDst.
  rdcarray<ReplayTextureCopyOp> replayTextureCopies;
  bool replayTextureCopiesComplete = true;
  void RecordReplayTextureCopy(const ReplayTextureCopyOp &copy)
  {
    if(!replayTextureCopiesComplete)
      return;
    if(replayTextureCopies.size() >= 65536)
    {
      replayTextureCopiesComplete = false;
      replayTextureCopies.clear();
      return;
    }
    replayTextureCopies.push_back(copy);
  }
  struct CheckpointRecordingChunk
  {
    ResourceId baked;
    uint64_t offset = 0, endOffset = 0;
    uint32_t eventAfter = 0;
    D3D12Chunk type = D3D12Chunk::List_Reset;
    // Loading proves the original signature needs no replay-time GPU address patching. Direct
    // draw/dispatch arguments and root constants read the original argument buffer at submission.
    bool directIndirect = false;
    // Set only on a target-specific continuation plan, never on the loading index. Reset/Close
    // still run normally; an unsubmitted list's other chunks need only their final event cursor.
    bool metadataOnly = false;
  };
  rdcarray<CheckpointRecordingChunk> checkpointChunks;
  uint64_t checkpointBegin = 0, checkpointEnd = 0;

  // All explicit native root-buffer bindings, independent of reflected shader usage. The first
  // submission of a list is a conservative consumer boundary even if a binding is unused.
  std::set<ResourceId> bufferPatchConsumers;
  // Descriptor tables and generated addresses can name buffers absent from reflected GetUsage.
  // Until their complete descriptor history is indexed, this list may consume any patched buffer.
  bool bufferPatchUnknownConsumers = false;
  struct BufferPatchReadOp
  {
    struct Table
    {
      ResourceId heap;
      uint32_t first = 0, count = 0;
    };
    uint32_t relativeEvent = 0;
    bool generatedAddresses = false;
    rdcarray<ReplayResourceRange> direct;
    rdcarray<Table> tables;
  };
  rdcarray<BufferPatchReadOp> bufferPatchReads;
  bool bufferPatchReadsComplete = true;
  const char *bufferPatchReadFailure = "";
  uint32_t bufferPatchReadFailureChunk = 0, bufferPatchReadFailureEvent = 0;

  struct ReplayUAVRange
  {
    ResourceId heap;
    uint32_t first = 0, count = 0;
    bool operator<(const ReplayUAVRange &other) const
    {
      if(heap != other.heap)
        return heap < other.heap;
      if(first != other.first)
        return first < other.first;
      return count < other.count;
    }
  };
  struct ReplayTextureReadOp
  {
    uint32_t relativeEvent = 0;
    bool unknown = false;
    // RTV/DSV blending/depth tests and the shading-rate image can read independent textures.
    rdcarray<ResourceId> directResources;
    // Entire declared SRV/UAV table ranges. Descriptor history, not final heap contents or shader
    // reflection, proves whether a candidate texture could have been referenced at execution.
    rdcarray<ReplayUAVRange> descriptorRanges;
    // CBV/sampler locations matter for descriptor overwrite hazards, but do not read texture
    // contents. Keep them separate from the texture-restore dependency proof above.
    rdcarray<ReplayUAVRange> otherDescriptorRanges;
    bool unknownDescriptorUses = false;
  };
  rdcarray<ReplayTextureReadOp> replayTextureReadOps;
  uint32_t replayTextureReadEntries = 0;
  bool replayTextureReadsComplete = true;
  // Loading records potential writes, never actual dirtiness. Shader-visible descriptors are
  // retained as locations and resolved against the live heap only when a list is submitted.
  std::set<ResourceId> replayWrites;
  std::set<ResourceId> replayRootUAVWrites;
  std::set<ReplayUAVRange> replayUAVRanges;
  // A submitted occurrence executes a prefix of its baked recording. Keep the first potential
  // write for each union member, rather than copying all bound descriptors for every action.
  // The original unions remain the fallback when this loading-time index is incomplete.
  std::map<ResourceId, uint32_t> replayWriteFirstEvents;
  std::map<ResourceId, uint32_t> replayRootUAVFirstEvents;
  std::map<ReplayUAVRange, uint32_t> replayUAVFirstEvents;
  // Optional executable-declaration upper bound. Keep the original root-signature union above
  // intact: loading declarations cannot describe a later replacement shader.
  std::set<ReplayUAVRange> replayDeclaredUAVRanges;
  std::map<ReplayUAVRange, uint32_t> replayDeclaredUAVFirstEvents;
  uint64_t replayUAVSlotsBefore = 0, replayUAVSlotsAfter = 0;
  uint32_t replayUAVDeclarationHits = 0, replayUAVDeclarationFallbacks = 0;
  uint32_t replayFirstUnknownWriteEvent = ~0U;
  bool replayWriteEventsKnown = false;
  bool replayWritesKnown = false;
  bool replayUnknownWrites = false;
  D3D12Chunk replayUnknownWriteChunk = D3D12Chunk::List_Reset;
  uint32_t eventCount;    // how many events are in this cmd list, for quick skipping
  uint32_t curEventID;    // current event ID while replaying, not used during loading

  bool hasExecuteDatas = false;
  uint32_t replayLastIndirectEvent = 0;
  WrappedID3D12CommandSignature *replayLastIndirectSignature = NULL;
  std::map<uint32_t, std::set<ResourceId>> bufferPatchCopyReads;
  void ShiftReplayMetadata(uint32_t firstFollowingEvent, int64_t delta);
};

class WrappedID3D12Device;

using D3D12CheckpointQuerySlot = std::pair<ResourceId, uint32_t>;
using D3D12CheckpointQueryResolve =
    std::pair<std::pair<ResourceId, uint32_t>, D3D12CheckpointQuerySlot>;

struct D3D12CheckpointQueryVersion
{
  D3D12_QUERY_TYPE type = D3D12_QUERY_TYPE_OCCLUSION;
  uint32_t endEvent = 0;
};

struct D3D12CheckpointQueryPlan
{
  // Only slots whose completed prefix values can be observed by the suffix need storage.
  std::map<D3D12CheckpointQuerySlot, D3D12CheckpointQueryVersion> slots;
  std::set<D3D12CheckpointQueryResolve> resolves;
  uint32_t firstUnsupportedResolve = ~0U;
  D3D12_QUERY_TYPE firstUnsupportedType = D3D12_QUERY_TYPE_OCCLUSION;
  D3D12CheckpointQuerySlot unsupportedSlot;
  rdcstr unsupportedReason;
};

struct D3D12CommandData
{
  D3D12CommandData();

  WrappedID3D12Device *m_pDevice;

  D3D12ActionCallback *m_ActionCallback;

  ResourceId m_LastCmdListID;

  RDResult m_FailedReplayResult = ResultCode::APIReplayFailed;

  rdcarray<ID3D12Resource *> m_IndirectBuffers;
  static const uint64_t m_IndirectSize = 4 * 1024 * 1024;
  uint64_t m_IndirectOffset;

  std::map<ResourceId, BakedCmdListInfo> m_BakedCmdListInfo;

  D3D12RenderState m_RenderState;

  rdcarray<SDObject *> m_EventAnnotations;
  SDObject *m_RootAnnotation = NULL;

  D3D12RenderState &GetCurRenderState()
  {
    return m_LastCmdListID == ResourceId() ? m_RenderState
                                           : m_BakedCmdListInfo[m_LastCmdListID].state;
  }
  enum PartialReplayIndex
  {
    Primary,
    Secondary,
    ePartialNum
  };

  // by definition, when replaying we must have N completely submitted command lists, and at most
  // two partially-submitted command lists. One primary, that we're part-way through, and then
  // if we're part-way through a ExecuteBundle inside that primary then there's one
  // secondary.
  struct PartialReplayData
  {
    PartialReplayData() { Reset(); }
    void Reset()
    {
      partialParent = ResourceId();
      baseEvent = 0;
      renderPassActive = false;
    }

    // this records where in the frame a command list was executed, so that we know if our replay
    // range ends in one of these ranges we need to construct a partial command list for future
    // replaying. Note that we always have the complete command list around - it's the bakeID
    // itself.
    // Since we only ever record a bakeID once the key is unique - note that the same command list
    // could be reset multiple times a frame, so the parent command list ID (the one recorded in
    // CmdList chunks) is NOT unique.
    // However, a single baked command list can be executed multiple times - so we have to have a
    // list of base events
    // Map from bakeID -> vector<baseEventID>
    std::map<ResourceId, rdcarray<uint32_t>> cmdListExecs;

    // This is just the baked ID of the parent command list that's partially replayed
    // If we are in the middle of a partial replay - allows fast checking in all CmdList chunks,
    // with the iteration through the above list only in Reset.
    // partialParent gets reset to ResourceId() in the Close so that other baked command lists from
    // the same parent don't pick it up
    // Also reset each overall replay
    ResourceId partialParent;

    // If a partial replay is detected, this records the base of the
    // range. This both allows easily and uniquely identifying it in the
    // executecmdlists, but also allows the recording to 'rebase' the
    // last event ID by subtracting this, to know how far to record
    uint32_t baseEvent;

    // if a render pass is active when we early-close a list, we need to end it
    bool renderPassActive;
  } m_Partial[ePartialNum];

  // Loading-time identity of each actual primary submission occurrence. Queue creation alone
  // does not imply concurrent work in a particular checkpoint continuation.
  std::map<uint32_t, ResourceId> m_ReplaySubmissionQueues;

  void MarkReplaySubmittedOverwrites(ResourceId baked, uint32_t baseEvent, uint32_t lastEvent);

  // if we're replaying just a single action or a particular command
  // list subsection of command events, we don't go through the
  // whole original command lists to set up the partial replay,
  // so we just set this command list
  ID3D12GraphicsCommandListX *m_OutsideCmdList = NULL;

  SDObject *InsertEventNodes(WrappedID3D12GraphicsCommandList *replayList, ResourceId cmd,
                             BakedCmdListInfo &cmdListInfo);

  // this is a list of uint64_t file offset -> uint32_t EIDs of where each
  // action is used. E.g. the action at offset 873954 is EID 50. If a
  // command list is executed more than once, there may be more than
  // one entry here - the action will be aliased among several EIDs, with
  // the first one being the 'primary'
  struct ActionUse
  {
    ActionUse(uint64_t offs, uint32_t eid, ResourceId cmd = ResourceId(), uint32_t rel = 0)
        : fileOffset(offs), cmdList(cmd), eventId(eid), relativeEID(rel)
    {
    }
    uint64_t fileOffset;
    ResourceId cmdList;
    uint32_t eventId;
    uint32_t relativeEID;
    bool operator<(const ActionUse &o) const
    {
      if(fileOffset != o.fileOffset)
        return fileOffset < o.fileOffset;
      return eventId < o.eventId;
    }
  };
  rdcarray<ActionUse> m_ActionUses;

  rdcarray<DebugMessage> m_EventMessages;

  std::map<ResourceId, ID3D12GraphicsCommandListX *> m_RerecordCmds;
  rdcarray<ID3D12GraphicsCommandListX *> m_RerecordCmdList;

  // One recorded list can have completed submissions before the occurrence containing the
  // selected event. That completed version must not share the target's truncated native list.
  // Companions are rebuilt through current replay handlers on every prefix replay, so shader
  // replacements and shared-buffer patch upload addresses cannot survive into a later generation.
  struct FullReplayCompanion
  {
    ResourceId parent, initialPipeline;
    D3D12RenderState resetState;
    uint32_t partialBase = 0;
    ID3D12CommandAllocator *allocator = NULL;
    ID3D12GraphicsCommandListX *list = NULL;
    bool ready = false;
  };
  std::map<ResourceId, FullReplayCompanion> m_FullReplayCompanions;
  void PrepareFullReplayCompanion(ResourceId baked, ResourceId parent,
                                  ID3D12PipelineState *initialPipeline);
  RDResult RecordFullReplayCompanion(
      ResourceId baked, uint64_t endOffset,
      const std::function<RDResult(const BakedCmdListInfo::CheckpointRecordingChunk &)> &record);
  ID3D12GraphicsCommandListX *RerecordCmdListForSubmission(ResourceId baked, uint32_t baseEvent);
  void ClearFullReplayCompanions();

  struct PreparedCommandList
  {
    ID3D12CommandAllocator *allocator = NULL;
    ID3D12GraphicsCommandListX *list = NULL;
    ResourceId parentList;
    uint64_t beginOffset = 0;
    uint64_t endOffset = 0;
    uint64_t sourceBytes = 0;
    bool ready = false;
  };

  std::map<ResourceId, PreparedCommandList> m_PreparedCmdLists;
  ResourceId m_PreparedScanList;
  ResourceId m_PreparedBuildingList;
  uint64_t m_PreparedSkipEnd = 0;
  uint64_t m_PreparedCacheBytes = 0;
  uint64_t m_PreparedSkippedBytes = 0;
  uint32_t m_PreparedCacheHits = 0;
  uint32_t m_PreparedCacheBuilds = 0;
  uint32_t m_PreparedSkippedChunks = 0;
  uint32_t m_PreparedAttempts = 0;
  uint32_t m_PreparedRejectedType = 0;
  uint32_t m_PreparedRejectedUnsafe = 0;
  uint32_t m_PreparedRejectedExisting = 0;
  uint32_t m_PreparedRejectedBudget = 0;
  bool m_PreparedReplayEnabled = false;
  bool m_PreparedTimingEnabled = false;

  struct PreparedReplayRejectSummary
  {
    PreparedReplayRejectReason reason = PreparedReplayRejectReason::None;
    D3D12Chunk chunk = D3D12Chunk::List_Reset;
    ResourceId firstBaked;
    ResourceId firstParent;
    ResourceId observedList;
    ResourceId observedParent;
    uint64_t firstBeginOffset = 0;
    uint64_t firstOffset = 0;
    uint32_t attempts = 0;
  };
  // Keyed by reason and chunk, so diagnostic output is bounded by the fixed D3D12 chunk vocabulary.
  std::map<uint64_t, PreparedReplayRejectSummary> m_PreparedRejectSummary;

  rdcarray<APIEvent> m_Events;
  D3D12EventNode m_LoadingEventNode;
  rdcarray<D3D12EventNode> m_EventNodes;
  bool m_AddedEventNode;

  uint64_t m_CurChunkOffset;
  SDChunkMetaData m_ChunkMetadata;
  uint32_t m_RootEventID;
  uint32_t m_FirstEventID, m_LastEventID;
  D3D12Chunk m_LastChunk;

  struct CheckpointBoundary
  {
    uint32_t eventId = 0, nextEvent = 0, nextAction = 0;
    uint64_t offset = 0;
    // Exclusive upper bound for restore/resume. ~0U means no un-restored native query is consumed.
    uint32_t firstNativeResolveEID = ~0U;
    D3D12_QUERY_TYPE firstNativeResolveType = D3D12_QUERY_TYPE_OCCLUSION;
    D3D12CheckpointQueryPlan queries;
    // Loading-time storage at this exact stream boundary, relative to capture initial contents.
    // Unknown/budget-exhausted observations use the full-heap admission fallback.
    uint64_t descriptorBytes = ~0ULL, fullDescriptorBytes = ~0ULL;
  };
  rdcarray<CheckpointBoundary> m_CheckpointBoundaries;
  struct ReplayTypedRootChunk
  {
    uint64_t offset = 0, endOffset = 0;
    uint32_t nextEvent = 0, chunkIndex = 0;
    D3D12Chunk type = D3D12Chunk::Max;
  };
  // APIEvent intentionally omits some root calls (e.g. Queue_EndEvent) and adds synthetic
  // command-list boundaries. Keep exact decoded root calls for the texture-owner proof.
  rdcarray<ReplayTypedRootChunk> m_ReplayTypedRootChunks;
  uint64_t m_ReplayTypedFrameBegin = 0, m_ReplayTypedFrameEnd = 0;
  bool m_ReplayTypedRootComplete = false;
  rdcstr m_CheckpointUnsupported;
  typedef std::map<D3D12Chunk, std::set<uint64_t>> CheckpointUnsupportedChunks;
  void ObserveCheckpointChunk(D3D12Chunk chunk, uint64_t chunkEndOffset);
  bool IsCheckpointBoundarySafe(CheckpointBoundary &boundary, rdcstr &reason,
                                CheckpointUnsupportedChunks *unsupported = NULL) const;
  bool GetCheckpointContinuation(uint64_t offset, uint32_t nextEvent,
                                 rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> &chunks,
                                 rdcstr &reason, CheckpointUnsupportedChunks *unsupported = NULL,
                                 uint32_t targetEvent = ~0U) const;
  bool ApplyCheckpointContinuationMetadata(const BakedCmdListInfo::CheckpointRecordingChunk &chunk);
  // A live range is confined to one occurrence of one primary DIRECT list. It consumes the
  // already materialised resources and must never be mistaken for a root-stream checkpoint.
  bool ValidateLiveForwardRange(ResourceId baked, uint32_t baseEvent, uint32_t currentEvent,
                                uint32_t targetEvent, rdcstr &reason) const;
  bool HasResolvedLiveForwardState() const;
  uint32_t GetBufferPatchFirstConsumer(ResourceId buffer) const;
  void ObserveBufferPatchReads(BakedCmdListInfo &info, D3D12Chunk chunk, uint32_t relativeEvent);
  bool GetBufferPatchRangeConsumer(const std::map<ResourceId, uint64_t> &buffers,
                                   const rdcarray<ReplayBufferPatchRange> &changes, uint32_t &first);
  ReplayResourceAccessIndex m_BufferPatchReadIndex;
  std::map<ResourceId, uint64_t> m_BufferPatchReadResources;
  bool m_BufferPatchReadIndexPrepared = false;
  size_t m_BufferPatchReadEntries = 0;
  // Content proof only: full clear before the first possible resource read in executed EID
  // order. throughEvent is the actual WithoutDraw end, not its following OnlyDraw target.
  // The caller supplies only independent committed textures, after rejecting buffers,
  // alias/sparse storage, edits and resource replacements. No other resource can be returned.
  // Pure alias barriers cannot expose their old bytes; split/unknown barriers still stop proof.
  // Root GPU addresses are buffer-only.
  std::set<ResourceId> GetReplayFullClearPrefix(uint32_t checkpointEvent,
                                               uint32_t throughEvent,
                                               const std::set<ResourceId> &eligibleTextures) const;
  // Complete prefix proof for non-inheriting UNKNOWN-layout placed texture aliases. The
  // caller supplies a symmetric physical-overlap graph; output owners never overlap.
  bool GetReplayTextureAliasOwners(uint32_t throughEvent,
                                   const std::map<ResourceId, std::set<ResourceId>> &conflicts,
                                   std::set<ResourceId> &owners, rdcstr &reason) const;
  void ObserveReplayWriteBindings(BakedCmdListInfo &info, const D3D12RenderState &state,
                                  uint32_t relativeEvent, bool graphics = true,
                                  bool compute = true, bool declarationBindingsKnown = true);
  struct ReplayShaderUAVBinding
  {
    ShaderStage stage = ShaderStage::Count;
    uint32_t space = 0, first = 0, count = 0;
  };
  struct ReplayPSOUAVProof
  {
    bool complete = false, graphics = false;
    bool constantBuffersComplete = false;
    const char *reason = "unvalidated";
    rdcarray<ReplayShaderUAVBinding> bindings;
    struct ConstantBuffer
    {
      ShaderStage stage = ShaderStage::Count;
      uint32_t space = 0, first = 0, count = 0, byteSize = 0;
    };
    rdcarray<ConstantBuffer> constantBuffers;
  };
  const ReplayPSOUAVProof &GetReplayPSOUAVProof(ResourceId pipeline);
  std::map<ResourceId, ReplayPSOUAVProof> m_ReplayPSOUAVProofs;
  std::map<rdcpair<ResourceId, ShaderStage>, ReplayPSOUAVProof> m_ReplayShaderUAVProofs;
  void BuildReplayWriteTracking();
  void MarkReplaySubmittedWrites(ResourceId baked, uint32_t baseEvent = ~0U,
                                  uint32_t lastEvent = ~0U);
  bool m_ReplayWriteCutoffEnabled = true;

  ResourceId m_LastPresentedImage;

  uint64_t m_TimeBase = 0;
  double m_TimeFrequency = 1.0f;
  SDFile *m_StructuredFile;

  rdcarray<PatchedRayDispatch> m_RayDispatches;

  std::map<ResourceId, rdcarray<EventUsage>> m_ResourceUses;

  struct IndirectReplayData
  {
    ID3D12CommandSignature *commandSig = NULL;
    ID3D12Resource *argsBuffer = NULL;
    UINT64 argsOffset = 0;
  } m_IndirectData;

  void GetIndirectBuffer(size_t size, ID3D12Resource **buf, uint64_t *offs);

  // util function to handle fetching the right eventId, calling any
  // aliases then calling PreDraw/PreDispatch.
  uint32_t HandlePreCallback(ID3D12GraphicsCommandListX *list,
                             ActionFlags type = ActionFlags::Drawcall, uint32_t multiDrawOffset = 0);

  bool InRerecordRange(ResourceId cmdid);
  bool HasRerecordCmdList(ResourceId cmdid);
  bool IsPartialCmdList(ResourceId cmdid);
  ID3D12GraphicsCommandListX *RerecordCmdList(ResourceId cmdid,
                                              PartialReplayIndex partialType = ePartialNum);

  void ObservePreparedReplayChunk(D3D12Chunk chunk, uint64_t chunkEndOffset);
  ID3D12GraphicsCommandListX *BeginPreparedCommandList(ResourceId baked, ResourceId parent,
                                                       D3D12_COMMAND_LIST_TYPE type,
                                                       ID3D12PipelineState *initialState,
                                                       bool &reused);
  bool DescriptorSlotUnusedBefore(ResourceId heap, uint32_t index, uint32_t beforeEvent,
                                  const char *&reason) const;
  void FinishPreparedCommandList(ResourceId baked, uint64_t chunkEndOffset);
  void ClearPreparedReplayCache();
  bool HasPreparedReplayCache() const { return !m_PreparedCmdLists.empty(); }

  void AddAction(const ActionDescription &a);
  void AddEvent();

  D3D12EventNode &GetLastEventNode()
  {
    rdcarray<D3D12EventNode> &eventNodes =
        (m_LastCmdListID != ResourceId() ? m_BakedCmdListInfo[m_LastCmdListID].eventNodes
                                         : m_EventNodes);
    return eventNodes.back();
  }

  void BakeEventNodes(ActionDescription &rootAction);

  void AddUsage(const D3D12RenderState &state, D3D12EventNode &eventNode);

  void AddUsageForBindInRootSig(const D3D12RenderState &state, D3D12EventNode &eventNode,
                                const D3D12RenderState::RootSignature *rootsig,
                                D3D12_DESCRIPTOR_RANGE_TYPE type, uint32_t space, uint32_t bind,
                                uint32_t rangeSize);

  void AddResourceUsage(D3D12EventNode &eventNode, ResourceId id, ResourceUsage usage);
  void AddCPUUsage(ResourceId id, ResourceUsage usage);
};
