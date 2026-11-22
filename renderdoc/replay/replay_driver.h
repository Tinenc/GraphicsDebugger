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

#pragma once

#include "api/replay/renderdoc_replay.h"
#include "core/core.h"
#include "maths/vec.h"

class Matrix4f;

struct MeshComponentHighlight
{
  Matrix4f DepthBiasedTransform(const Matrix4f &mvp, const ICamera *camera) const;

  rdcarray<FloatVector> vertices;
  uint32_t selectedVertices = 0;
  Topology topology = Topology::TriangleList;
};

template <typename T, BucketRecordType bucketType = T::BucketType>
struct BucketForRecord
{
  static size_t Get(size_t value);
};

template <typename T>
struct BucketForRecord<T, BucketRecordType::Linear>
{
  static size_t Get(size_t value)
  {
    const size_t size = T::BucketSize;
    const size_t count = T::BucketCount;
    const size_t maximum = size * count;
    const size_t index = (value < maximum) ? (value / size) : (count - 1);
    return index;
  }
};

template <typename T>
struct BucketForRecord<T, BucketRecordType::Pow2>
{
  static size_t Get(size_t value)
  {
    const size_t count = T::BucketCount;
    static_assert(count <= (sizeof(size_t) * 8),
                  "Unexpected correspondence between bucket size and sizeof(size_t)");
    const size_t maximum = (size_t)1 << count;
    const size_t index = (value < maximum) ? (size_t)(Log2Floor(value)) : (count - 1);
    return index;
  }
};

// Event overlays do not alter resource mappings. Keys bind cached derived pipelines to the
// exact live base pipeline (which may itself be a user's shared replacement).
struct EventShaderOverrideBinding
{
  ResourceId original;
  ResourceId replacement;
  uint32_t outputTarget = ~0U;
  bool independentFloatOutput = false;
};

struct EventShaderPipelineKey
{
  ResourceId pipeline;
  uint32_t eventId = 0;
  ResourceId replacements[8] = {};
  uint32_t outputTarget = ~0U;
  bool independentFloatOutput = false;
  bool operator<(const EventShaderPipelineKey &other) const
  {
    if(pipeline != other.pipeline)
      return pipeline < other.pipeline;
    if(eventId != other.eventId)
      return eventId < other.eventId;
    for(size_t i = 0; i < ARRAY_COUNT(replacements); i++)
      if(replacements[i] != other.replacements[i])
        return replacements[i] < other.replacements[i];
    if(outputTarget != other.outputTarget)
      return outputTarget < other.outputTarget;
    return independentFloatOutput < other.independentFloatOutput;
  }
  bool Uses(ResourceId target) const
  {
    for(ResourceId replacement : replacements)
      if(replacement == target)
        return true;
    return false;
  }
};

struct FrameRecord
{
  FrameDescription frameInfo;

  rdcarray<ActionDescription> actionList;
};

DECLARE_REFLECTION_STRUCT(FrameRecord);

// Checkpoints are replay-device state, never presentation images. The shared controller proposes
// useful module boundaries; each backend must validate an actual resumable boundary and owns all
// resource versions, command positions and synchronization needed to restore it.
struct ReplayCheckpointConfig
{
  bool enabled = true;
  uint64_t byteBudget = 2ULL * 1024 * 1024 * 1024;
  uint32_t maxCheckpoints = 3;
  uint32_t minEventGap = 128;
};

struct ReplayCheckpointCandidate
{
  // State AFTER this event. A backend may choose an earlier safe boundary within this module.
  uint32_t eventId = 0;
  uint32_t moduleId = 0;
  uint32_t priority = 0;
  uint64_t estimatedWritableBytes = 0;
};

// Candidate changes under the same storage policy must preserve still-selected native versions.
// Shrinking storage or explicitly disabling remains a full configuration change.
inline bool ReplayCheckpointStorageCompatible(const ReplayCheckpointConfig &previous,
                                               const ReplayCheckpointConfig &next)
{
  return previous.enabled && next.enabled && previous.byteBudget == next.byteBudget &&
         next.byteBudget > 0 && previous.minEventGap == next.minEventGap &&
         next.maxCheckpoints >= previous.maxCheckpoints && previous.maxCheckpoints > 0;
}

inline bool ReplayCheckpointCandidateContains(const rdcarray<ReplayCheckpointCandidate> &candidates,
                                               uint32_t eventId)
{
  for(const ReplayCheckpointCandidate &candidate : candidates)
    if(candidate.eventId == eventId)
      return true;
  return false;
}

struct ReplayCheckpointStats
{
  bool supported = false;
  bool enabled = false;
  // Event zero is a real saved post-initialisation state only when this flag is set.
  // A default lastResumeEvent of zero alone is never evidence of a checkpoint hit.
  bool hasInitialCheckpoint = false;
  uint32_t checkpointCount = 0;
  uint64_t allocatedBytes = 0;
  uint64_t sharedBytes = 0;
  uint64_t builds = 0;
  uint64_t hits = 0;
  uint64_t misses = 0;
  uint64_t invalidations = 0;
  uint64_t rejected = 0;
  uint32_t lastResumeEvent = 0;
  uint32_t lastSaveEvent = 0;
  rdcarray<uint32_t> events;
  // Optional native planning proof, separate from materialized GPU checkpoints. Controllers can
  // avoid a replay when no new boundary was admitted, and try another bounded idle candidate.
  bool candidatePlanKnown = false;
  bool canTryOtherCandidates = false;
  rdcarray<uint32_t> candidateEvents;
  // Optional local capability: native planning was stopped solely by a measured zero memory
  // budget. Old remote peers leave this false; a missing plan is not evidence of memory pressure.
  bool memoryBudgetBlocked = false;
  // Internal diagnostic, not a translated UI status. Empty means no current rejection.
  rdcstr reason;
};

// Persistent edits address captured storage, independently of an event, stage or API binding slot.
// The driver receives a sorted, non-overlapping array and must replace its configuration atomically.
// Initial contents remain immutable; captured writes must not silently remove an active patch.
struct ReplayBufferPatch
{
  ResourceId buffer;
  uint64_t byteOffset = 0;
  bytebuf data;
};

DECLARE_REFLECTION_STRUCT(ReplayBufferPatch);

rdcstr ValidateReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches);

// A half-open byte range in original captured buffer storage, independent of API binding slots.
struct ReplayBufferPatchRange
{
  ResourceId buffer;
  uint64_t byteOffset = 0;
  uint64_t byteLength = 0;
};

// Layout compatibility is intentionally false for empty or invalid configurations. It does not
// compare values, and split/merged representations of the same coverage have different layouts.
bool SameReplayBufferPatchLayout(const rdcarray<ReplayBufferPatch> &previous,
                                 const rdcarray<ReplayBufferPatch> &next);

// Compare validated, sorted, non-overlapping configurations by absolute byte address. An added or
// removed override is a change even when its value might equal the unknown original captured byte.
// Adjacent changed bytes are merged; equal bytes are excluded even across different patch layouts.
// Invalid input or excessive fragmentation returns an error and clears changes. Callers must then
// conservatively invalidate, not treat an empty result as proof of no changes or reject the edit.
rdcstr GetReplayBufferPatchChanges(const rdcarray<ReplayBufferPatch> &previous,
                                   const rdcarray<ReplayBufferPatch> &next,
                                   rdcarray<ReplayBufferPatchRange> &changes,
                                   uint32_t maxRanges = 4096);

void ApplyReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches, ResourceId buffer,
                              uint64_t byteOffset, void *data, uint64_t byteLength);

// Keep edit identities separately from the native disjoint ranges. Removing an overlapping edit
// reveals an earlier edit, rather than losing its bytes or depending on map traversal order.
class ReplayBufferPatchSet
{
public:
  rdcstr Set(ResourceId buffer, uint64_t byteOffset, const bytebuf &data);
  bool Remove(ResourceId buffer, uint64_t byteOffset, uint64_t byteLength);
  // Remove every layer covering these bytes, retaining other bytes and exact undo identities.
  bool ResetRange(ResourceId buffer, uint64_t byteOffset, uint64_t byteLength);
  void Clear() { m_Layers.clear(); }
  bool Empty() const { return m_Layers.empty(); }
  rdcarray<ReplayBufferPatch> Flatten() const;

private:
  struct Layer
  {
    ResourceId buffer;
    uint64_t byteOffset = 0;
    uint64_t byteLength = 0;
    rdcarray<ReplayBufferPatch> ranges;
  };
  rdcarray<Layer> m_Layers;
};

enum class RemapTexture : uint32_t
{
  NoRemap,
  RGBA8,
  RGBA16,
  RGBA32
};

DECLARE_REFLECTION_ENUM(RemapTexture);

struct GetTextureDataParams
{
  // this data is going to be saved to disk, so prepare it as needed. E.g. on GL flip Y order to
  // match conventional axis for file formats.
  bool forDiskSave = false;
  // this data is going to be transferred cross-API e.g. in replay proxying, so standardise bit
  // layout of any packed formats where API conventions differ (mostly only RGBA4 or other awkward
  // ones where our resource formats don't enumerate all possible iterations). Saving to disk is
  // also standardised to ensure the data matches any format description we also write to the
  // format.
  bool standardLayout = false;
  CompType typeCast = CompType::Typeless;
  bool resolve = false;
  RemapTexture remap = RemapTexture::NoRemap;
  float blackPoint = 0.0f;
  float whitePoint = 1.0f;
};

DECLARE_REFLECTION_STRUCT(GetTextureDataParams);

CompType BaseRemapType(RemapTexture remap, CompType typeCast);
inline CompType BaseRemapType(const GetTextureDataParams &params)
{
  return BaseRemapType(params.remap, params.typeCast);
}

class RDCFile;

class AMDRGPControl;

struct RenderOutputSubresource
{
  RenderOutputSubresource(uint32_t mip, uint32_t slice, uint32_t numSlices)
      : mip(mip), slice(slice), numSlices(numSlices)
  {
  }

  uint32_t mip, slice, numSlices;
};

// these two interfaces define what an API driver implementation must provide
// to the replay. At minimum it must implement IRemoteDriver which contains
// all of the functionality that cannot be achieved elsewhere. An IReplayDriver
// is more powerful and can be used as a local replay (with an IRemoteDriver
// proxied elsewhere if necessary).
//
// In this sense, IRemoteDriver is a strict subset of IReplayDriver functionality.
// Wherever at all possible functionality should be added as part of IReplayDriver,
// *not* as part of IRemoteDriver, to keep the burden on remote drivers to a minimum.

class IRemoteDriver
{
public:
  virtual void Shutdown() = 0;

  // Replay state belongs to the device, including when the UI is using a remote proxy.
  virtual uint32_t GetReplayControlVersion() const { return 0; }
  virtual bool ExchangeReplayCheckpointBypass(bool bypass) { return false; }
  virtual bool ExchangeReplayCheckpointNaturalReference(bool reference) { return false; }
  virtual bool ExchangeReplayCheckpointPrefixOnly(bool prefixOnly) { return false; }
  virtual void ConfigureReplayCheckpoints(const ReplayCheckpointConfig &config,
                                           const rdcarray<ReplayCheckpointCandidate> &candidates)
  {
  }
  virtual void InvalidateReplayCheckpoints(uint32_t firstAffectedEvent = 0) {}
  virtual ReplayCheckpointStats GetReplayCheckpointStats() { return {}; }
  // Idle-only, CPU-only probe. True means a previously measured zero budget became positive;
  // it does not admit a candidate or build/restore resources. Unsupported drivers return false.
  virtual bool RefreshReplayCheckpointMemoryBudget() { return false; }
  virtual rdcstr SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches)
  {
    return patches.empty() ? rdcstr() : rdcstr("Persistent buffer edits are not supported by this replay driver");
  }
  // Cutoff certified by the last successful Set. Zero requires canonical replay. A positive
  // value permits restoring a complete prefix strictly before this first possible consumer.
  // Only identical active byte ranges with new values can reuse old patched prefixes.
  virtual uint32_t GetReplayBufferPatchFirstAffectedEvent() const { return 0; }

  virtual APIProperties GetAPIProperties() = 0;

  virtual rdcarray<ResourceDescription> GetResources() = 0;

  virtual rdcarray<DescriptorStoreDescription> GetDescriptorStores() = 0;

  virtual rdcarray<BufferDescription> GetBuffers() = 0;
  virtual BufferDescription GetBuffer(ResourceId id) = 0;

  virtual rdcarray<TextureDescription> GetTextures() = 0;
  virtual TextureDescription GetTexture(ResourceId id) = 0;

  virtual rdcarray<DebugMessage> GetDebugMessages() = 0;

  virtual rdcarray<ShaderEntryPoint> GetShaderEntryPoints(ResourceId shader) = 0;
  virtual const ShaderReflection *GetShader(ResourceId pipeline, ResourceId shader,
                                            ShaderEntryPoint entry) = 0;

  virtual rdcarray<rdcstr> GetDisassemblyTargets(bool withPipeline) = 0;
  virtual rdcstr DisassembleShader(ResourceId pipeline, const ShaderReflection *refl,
                                   const rdcstr &target) = 0;

  virtual rdcarray<EventUsage> GetUsage(ResourceId id) = 0;

  virtual void SetPipelineStates(D3D11Pipe::State *d3d11, D3D12Pipe::State *d3d12,
                                 GLPipe::State *gl, VKPipe::State *vk) = 0;
  virtual void SavePipelineState(uint32_t eventId) = 0;

  virtual rdcarray<Descriptor> GetDescriptors(ResourceId descriptorStore,
                                              const rdcarray<DescriptorRange> &ranges) = 0;
  virtual rdcarray<SamplerDescriptor> GetSamplerDescriptors(
      ResourceId descriptorStore, const rdcarray<DescriptorRange> &ranges) = 0;
  virtual rdcarray<DescriptorAccess> GetDescriptorAccess(uint32_t eventId) = 0;
  virtual rdcarray<DescriptorLogicalLocation> GetDescriptorLocations(
      ResourceId descriptorStore, const rdcarray<DescriptorRange> &ranges) = 0;

  virtual FrameRecord GetFrameRecord() = 0;

  virtual RDResult ReadLogInitialisation(RDCFile *rdc, bool storeStructuredBuffers) = 0;
  virtual void ReplayLog(uint32_t endEventID, ReplayLogType replayType) = 0;
  virtual SDFile *GetStructuredFile() = 0;

  virtual rdcarray<uint32_t> GetPassEvents(uint32_t eventId) = 0;

  virtual void InitPostVSBuffers(uint32_t eventId) = 0;
  virtual void InitPostVSBuffers(const rdcarray<uint32_t> &passEvents) = 0;

  virtual MeshFormat GetPostVSBuffers(uint32_t eventId, uint32_t instID, uint32_t viewID,
                                      MeshDataStage stage) = 0;

  // this is a helper/batch query for the above, that's only necessary because Android is a shit
  // platform and its proxying has significant per-call overhead. This is overridden in the proxy,
  // but otherwise will call to this default implementation
  virtual rdcarray<MeshFormat> GetBatchPostVSBuffers(uint32_t eventId,
                                                     const rdcarray<uint32_t> &instIDs,
                                                     uint32_t viewID, MeshDataStage stage)
  {
    rdcarray<MeshFormat> ret;
    ret.reserve(instIDs.size());
    for(uint32_t instID : instIDs)
      ret.push_back(GetPostVSBuffers(eventId, instID, viewID, stage));
    return ret;
  }

  virtual void GetBufferData(ResourceId buff, uint64_t offset, uint64_t len, bytebuf &retData) = 0;
  virtual void GetTextureData(ResourceId tex, const Subresource &sub,
                              const GetTextureDataParams &params, bytebuf &data) = 0;

  virtual ShaderPairComparison ComparePixelShaderPair(uint32_t eventId, ResourceId expectedOriginal,
                                                       ResourceId baseline, ResourceId candidate)
  {
    ShaderPairComparison result;
    result.eventId = eventId;
    result.reason = "Single-prefix pixel Shader comparison is not supported by this replay driver";
    return result;
  }

  virtual void BuildTargetShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                                 const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                                 ShaderStage type, ResourceId &id, rdcstr &errors) = 0;
  virtual rdcarray<ShaderEncoding> GetTargetShaderEncodings() = 0;
  virtual void ReplaceResource(ResourceId from, ResourceId to) = 0;
  virtual void RemoveReplacement(ResourceId id) = 0;
  virtual rdcstr ReplaceTargetShaderChecked(ResourceId from, ResourceId to)
  {
    return "Checked target-shader replacement is not supported by this replay driver";
  }
  virtual rdcstr RemoveTargetShaderReplacementChecked(ResourceId id)
  {
    return "Checked target-shader replacement is not supported by this replay driver";
  }
  virtual void FreeTargetResource(ResourceId id) = 0;
  virtual void ClearReplayCache() = 0;
  virtual void ReloadShaderDebugInformation() = 0;

  // Event-scoped shader overrides are deliberately implemented below the resource replacement
  // layer. Unsupported drivers keep these no-op defaults, while a local driver can temporarily
  // bind configured shaders around ReplayController's eReplay_OnlyDraw call.
  virtual rdcstr SetEventShaderOverride(uint32_t eventId, ShaderStage stage,
                                        ResourceId expectedOriginal, ResourceId replacement,
                                        uint32_t outputTarget = ~0U,
                                        bool independentFloatOutput = false)
  {
    return "Event-scoped shader overrides are not supported by this replay driver";
  }
  virtual void ClearEventShaderOverride(uint32_t eventId, ShaderStage stage) {}
  virtual void ClearEventShaderOverrides() {}
  virtual void ClearEventShaderOverridesForResource(ResourceId id) {}
  virtual rdcstr ApplyEventShaderOverrides(uint32_t eventId) { return {}; }
  virtual void RestoreEventShaderOverrides() {}
  virtual TextureDescription GetEventShaderPreviewTexture(uint32_t eventId, ShaderStage stage)
  { return {}; }

  virtual rdcstr SetEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                         ResourceId expectedShader, ResourceId expectedBuffer,
                                         uint64_t expectedByteOffset, uint64_t expectedByteSize,
                                         const bytebuf &data, uint32_t registerSpace = 0)
  {
    return "Event-scoped constant-buffer overrides are not supported by this replay driver";
  }
  virtual void ClearEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                         uint32_t registerSpace = 0)
  {
  }
  virtual void ClearEventCBufferOverrides() {}
  virtual rdcstr ApplyEventCBufferOverrides(uint32_t eventId) { return {}; }
  virtual void RestoreEventCBufferOverrides() {}

  virtual rdcarray<GPUCounter> EnumerateCounters() = 0;
  virtual CounterDescription DescribeCounter(GPUCounter counterID) = 0;
  virtual rdcarray<CounterResult> FetchCounters(const rdcarray<GPUCounter> &counterID) = 0;

  virtual void FillCBufferVariables(ResourceId pipeline, ResourceId shader, ShaderStage stage,
                                    rdcstr entryPoint, uint32_t cbufSlot,
                                    rdcarray<ShaderVariable> &outvars, const bytebuf &data) = 0;

  virtual rdcarray<PixelModification> PixelHistory(rdcarray<EventUsage> events, ResourceId target,
                                                   uint32_t x, uint32_t y, const Subresource &sub,
                                                   CompType typeCast) = 0;
  virtual ShaderDebugTrace *DebugVertex(uint32_t eventId, uint32_t vertid, uint32_t instid,
                                        uint32_t idx, uint32_t view) = 0;
  virtual ShaderDebugTrace *DebugPixel(uint32_t eventId, uint32_t x, uint32_t y,
                                       const DebugPixelInputs &inputs) = 0;
  virtual ShaderDebugTrace *DebugThread(uint32_t eventId, const rdcfixedarray<uint32_t, 3> &groupid,
                                        const rdcfixedarray<uint32_t, 3> &threadid) = 0;
  virtual ShaderDebugTrace *DebugMeshThread(uint32_t eventId,
                                            const rdcfixedarray<uint32_t, 3> &groupid,
                                            const rdcfixedarray<uint32_t, 3> &threadid) = 0;
  virtual rdcarray<ShaderDebugState> ContinueDebug(ShaderDebugger *debugger) = 0;
  virtual void FreeDebugger(ShaderDebugger *debugger) = 0;

  virtual ResourceId RenderOverlay(ResourceId texid, FloatVector clearCol, DebugOverlay overlay,
                                   uint32_t eventId, const rdcarray<uint32_t> &passEvents) = 0;

  virtual bool IsRenderOutput(ResourceId id) = 0;

  virtual void FileChanged() = 0;
  virtual RDResult FatalErrorCheck() = 0;

  virtual bool NeedRemapForFetch(const ResourceFormat &format) = 0;

  virtual DriverInformation GetDriverInfo() = 0;

  virtual rdcarray<GPUDevice> GetAvailableGPUs() = 0;
};

// Event-scoped CBuffer ownership includes the register space. D3D11 accepts only space 0, while
// D3D12 may legally bind the same b-register in several spaces at one stage.
struct EventCBufferOverrideKey
{
  uint32_t eventId = 0;
  ShaderStage stage = ShaderStage::Count;
  uint32_t slot = 0;
  uint32_t registerSpace = 0;

  bool operator<(const EventCBufferOverrideKey &other) const
  {
    if(eventId != other.eventId)
      return eventId < other.eventId;
    if(stage != other.stage)
      return stage < other.stage;
    if(slot != other.slot)
      return slot < other.slot;
    return registerSpace < other.registerSpace;
  }
};

class IReplayDriver : public IRemoteDriver
{
public:
  // Canonical forced replay and temporary analysis mutations must not restore or create a
  // checkpoint. This flag does not discard existing checkpoints or change their budget.
  bool m_ReplayCheckpointBypass = false;
  // An automatic reference disables checkpoint reuse while retaining ordinary replay work
  // reduction (for example proven-clean initial resources). Force and analysis never set this.
  // This belongs to the private replay driver contract, not the public replay/bindings ABI.
  bool m_ReplayCheckpointNaturalReference = false;
  // A foreground shared-buffer edit may save its necessary pre-consumer anchor, but leaves
  // downstream rebuilding to idle maintenance. Ordinary event navigation keeps its full policy.
  // Bypass always takes precedence over this request-local restriction.
  bool m_ReplayCheckpointPrefixOnly = false;
  // A controller-owned persistent buffer edit can rebuild an original-pipeline write proof
  // even when its first forced replay deliberately bypasses result checkpoints. Analysis
  // scopes clear this provenance; it does not authorise cached output or native-list reuse.
  bool m_ReplayBufferMutationRoot = false;
  // Checked Shader replacement replay only. A backend must independently prove unchanged
  // read/write envelopes before retaining dirty-content evidence; this never permits checkpoints.
  bool m_ReplayShaderMutationRoot = false;
  uint32_t GetReplayControlVersion() const override { return 1; }
  bool ExchangeReplayCheckpointBypass(bool bypass) override
  {
    const bool previous = m_ReplayCheckpointBypass;
    m_ReplayCheckpointBypass = bypass;
    return previous;
  }
  bool ExchangeReplayCheckpointNaturalReference(bool reference) override
  {
    const bool previous = m_ReplayCheckpointNaturalReference;
    m_ReplayCheckpointNaturalReference = reference;
    return previous;
  }
  bool ExchangeReplayCheckpointPrefixOnly(bool prefixOnly) override
  {
    const bool previous = m_ReplayCheckpointPrefixOnly;
    m_ReplayCheckpointPrefixOnly = prefixOnly;
    return previous;
  }

  // When non-empty, overlay backends should render wireframe/drawcall for all listed child EIDs
  // instead of just the current eventId. Set by ReplayOutput::RefreshOverlay when user selects
  // a MultiAction parent node.
  rdcarray<uint32_t> m_OverlayMultiActionEvents;

  virtual bool IsRemoteProxy() = 0;

  virtual IReplayDriver *MakeDummyDriver() = 0;

  virtual rdcarray<WindowingSystem> GetSupportedWindowSystems() = 0;

  virtual AMDRGPControl *GetRGPControl() = 0;

  virtual uint64_t MakeOutputWindow(WindowingData window, bool depth) = 0;
  virtual void DestroyOutputWindow(uint64_t id) = 0;
  virtual bool CheckResizeOutputWindow(uint64_t id) = 0;
  virtual void GetOutputWindowDimensions(uint64_t id, int32_t &w, int32_t &h) = 0;
  virtual void GetOutputWindowData(uint64_t id, bytebuf &retData) = 0;
  virtual void ClearOutputWindowColor(uint64_t id, FloatVector col) = 0;
  virtual void ClearOutputWindowDepth(uint64_t id, float depth, uint8_t stencil) = 0;
  virtual void BindOutputWindow(uint64_t id, bool depth) = 0;
  virtual bool IsOutputWindowVisible(uint64_t id) = 0;
  virtual void FlipOutputWindow(uint64_t id) = 0;

  virtual bool GetMinMax(ResourceId texid, const Subresource &sub, CompType typeCast, float *minval,
                         float *maxval) = 0;
  virtual bool GetHistogram(ResourceId texid, const Subresource &sub, CompType typeCast,
                            float minval, float maxval, const rdcfixedarray<bool, 4> &channels,
                            rdcarray<uint32_t> &histogram) = 0;
  virtual void PickPixel(ResourceId texture, uint32_t x, uint32_t y, const Subresource &sub,
                         CompType typeCast, float pixel[4]) = 0;

  virtual ResourceId CreateProxyTexture(const TextureDescription &templateTex) = 0;
  // Create a CPU-uploadable proxy whose lifetime is managed by the texture snapshot cache.
  virtual ResourceId CreateProxyTextureSnapshot(const TextureDescription &templateTex)
  {
    return CreateProxyTexture(templateTex);
  }
  // Query before beginning the portable CPU readback fallback. Drivers that do not have a
  // replay-owned upload proxy must return false so an unsupported native snapshot fails closed
  // without first copying a full texture through CPU memory.
  virtual bool SupportsProxyTextureSnapshots() { return false; }
  virtual void SetProxyTextureData(ResourceId texid, const Subresource &sub, byte *data,
                                   size_t dataSize) = 0;
  // Create a replay-owned GPU copy of one texture subresource. The returned resource must remain
  // valid across subsequent ReplayLog()/ApplyInitialContents() calls. Drivers may preserve more of
  // the source topology than requested and describe the corresponding selection in snapshotSub.
  // Returning a null ResourceId asks the caller to use its portable readback fallback.
  virtual ResourceId CreateTextureSnapshot(ResourceId source, const Subresource &sourceSub,
                                           CompType typeCast, Subresource &snapshotSub)
  {
    return ResourceId();
  }
  // Release a resource returned by either snapshot creation method. Implementations must ignore
  // unknown or already-freed IDs so bounded controller caches can safely discard stale entries.
  virtual void FreeTextureSnapshot(ResourceId snapshot) {}
  virtual void FreeTextureSnapshots(const rdcarray<ResourceId> &snapshots)
  {
    for(ResourceId snapshot : snapshots)
      FreeTextureSnapshot(snapshot);
  }
  virtual bool IsTextureSupported(const TextureDescription &tex) = 0;

  virtual ResourceId CreateProxyBuffer(const BufferDescription &templateBuf) = 0;
  virtual void SetProxyBufferData(ResourceId bufid, byte *data, size_t dataSize) = 0;

  virtual void RenderMesh(uint32_t eventId, const rdcarray<MeshFormat> &secondaryDraws,
                          const MeshDisplay &cfg, const MeshComponentHighlight &components) = 0;
  virtual bool RenderTexture(TextureDisplay cfg) = 0;

  virtual void SetCustomShaderIncludes(const rdcarray<rdcstr> &directories) = 0;
  virtual void BuildCustomShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                                 const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                                 ShaderStage type, ResourceId &id, rdcstr &errors) = 0;
  virtual rdcarray<ShaderEncoding> GetCustomShaderEncodings() = 0;
  virtual rdcarray<ShaderSourcePrefix> GetCustomShaderSourcePrefixes() = 0;
  virtual ResourceId ApplyCustomShader(TextureDisplay &display) = 0;
  virtual void FreeCustomShader(ResourceId id) = 0;

  virtual void RenderCheckerboard(FloatVector dark, FloatVector light) = 0;

  virtual void RenderHighlightBox(float w, float h, float scale) = 0;

  virtual uint32_t PickVertex(uint32_t eventId, int32_t width, int32_t height,
                              const MeshDisplay &cfg, uint32_t x, uint32_t y) = 0;
};

// for protocols, we extend the public interface a bit to add callbacks for remapping connection
// ports (typically to some port forwarded on localhost)
struct IDeviceProtocolHandler : public IDeviceProtocolController
{
  virtual rdcstr GetDeviceID(const rdcstr &URL)
  {
    rdcstr ret = URL;
    int offs = ret.find("://");
    if(offs > 0)
      ret.erase(0, offs + 3);
    return ret;
  }

  virtual rdcstr RemapHostname(const rdcstr &deviceID) = 0;
  virtual uint16_t RemapPort(const rdcstr &deviceID, uint16_t srcPort) = 0;
  virtual IRemoteServer *CreateRemoteServer(Network::Socket *sock, const rdcstr &deviceID) = 0;
};

// utility functions useful in any driver implementation
void SetupActionPointers(rdcarray<ActionDescription *> &actionTable,
                         rdcarray<ActionDescription> &actions);

// for hardware/APIs that can't do line rasterization, manually expand any triangle input topology
// to a linestrip with strip restart indices.
void PatchLineStripIndexBuffer(const ActionDescription *action, Topology topology, uint8_t *idx8,
                               uint16_t *idx16, uint32_t *idx32, rdcarray<uint32_t> &patchedIndices);

void PatchTriangleFanRestartIndexBufer(rdcarray<uint32_t> &patchedIndices, uint32_t restartIndex);

uint64_t CalcMeshOutputSize(uint64_t curSize, uint64_t requiredOutput);

void StandardFillCBufferVariable(ResourceId shader, const ShaderConstantType &desc,
                                 uint32_t dataOffset, const bytebuf &data, ShaderVariable &outvar,
                                 uint32_t matStride);
void StandardFillCBufferVariables(ResourceId shader, const rdcarray<ShaderConstant> &invars,
                                  rdcarray<ShaderVariable> &outvars, const bytebuf &data);
void PreprocessLineDirectives(rdcarray<ShaderSourceFile> &sourceFiles);

// simple cache for when we need buffer data for highlighting
// vertices, typical use will be lots of vertices in the same
// mesh, not jumping back and forth much between meshes.
struct HighlightCache
{
  HighlightCache() : cacheKey(0), idxData(false) {}
  IRemoteDriver *driver = NULL;

  uint64_t cacheKey;

  bool idxData;
  bytebuf vertexData;
  rdcarray<uint32_t> indices;

  void CacheHighlightingData(uint32_t eventId, const MeshDisplay &cfg);

  bool FetchHighlightPositions(const MeshDisplay &cfg, FloatVector &activeVertex,
                               rdcarray<FloatVector> &activePrim,
                               rdcarray<FloatVector> &adjacentPrimVertices,
                               rdcarray<FloatVector> &inactiveVertices);

  static FloatVector InterpretVertex(const byte *data, uint32_t vert, uint32_t vertexByteStride,
                                     const ResourceFormat &fmt, const byte *end, bool &valid);

  FloatVector InterpretVertex(const byte *data, uint32_t vert, const MeshDisplay &cfg,
                              const byte *end, bool useidx, bool &valid);
};

extern const Vec4f colorRamp[22];
extern const uint32_t uniqueColors[48];

enum class DiscardType : int
{
  RenderPassLoad,         // discarded on renderpass load
  RenderPassStore,        // discarded after renderpass store
  UndefinedTransition,    // transition from undefined layout
  DiscardCall,            // explicit Discard() type API call
  InvalidateCall,         // explicit Invalidate() type API call
  Count,
};

static constexpr uint32_t DiscardPatternWidth = 64;
static constexpr uint32_t DiscardPatternHeight = 8;

// returns a pattern to fill the texture with
bytebuf GetDiscardPattern(DiscardType type, const ResourceFormat &fmt, uint32_t rowPitch = 1,
                          bool invert = false);

void DeriveNearFar(Vec4f pos, Vec4f pos0, float &nearp, float &farp, bool &found);
