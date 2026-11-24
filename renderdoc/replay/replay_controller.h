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

#include <set>
#include "api/replay/renderdoc_replay.h"
#include "common/common.h"
#include "core/core.h"
#include "replay/replay_driver.h"

#define CHECK_REPLAY_THREAD() RDCASSERT(Threading::GetCurrentID() == m_ThreadID);

struct ReplayController;

// Routing identity for historical displays that cannot bind a persistent snapshot (custom
// shader/overlay/YUV or unsupported texture topology). This owns no image or captured resource.
// A dirty output must replay this source instead of rendering the restored live configuration.
struct ReplayHistoryDisplaySource
{
  bool active = false;
  uint32_t eventId = 0;
  uint64_t generation = 0;
  TextureDisplay display;
  rdcpair<int32_t, int32_t> dimensions = {0, 0};

  void Clear();
  void Publish(uint32_t event, const TextureDisplay &source, uint64_t mutationGeneration,
               rdcpair<int32_t, int32_t> renderedDimensions, bool succeeded);
  bool NeedsReplay(bool dirty, rdcpair<int32_t, int32_t> currentDimensions,
                   uint64_t mutationGeneration) const;
};

struct ReplayOutput : public IReplayOutput
{
public:
  void Shutdown();
  void SetTextureDisplay(const TextureDisplay &o);
  void SetMeshDisplay(const MeshDisplay &o);
  void SetMeshComponentOverlay(const FloatVector *vertices, uint32_t count, Topology topology,
                               uint32_t selectedVertices);
  void SetDimensions(int32_t width, int32_t height);
  bytebuf ReadbackOutputTexture();
  rdcpair<int32_t, int32_t> GetDimensions();

  void ClearThumbnails();
  ResultDetails AddThumbnail(WindowingData window, ResourceId texID, const Subresource &sub,
                             CompType typeCast);
  bytebuf DrawThumbnail(int32_t width, int32_t height, ResourceId textureId, const Subresource &sub,
                        CompType typeCast);

  void Display();
  void DisplayTextureAtEvent(uint32_t eventId, const TextureDisplay &config);
  rdcarray<bytebuf> ReadbackTextureAtEvents(const rdcarray<uint32_t> &eventIds,
                                            const TextureDisplay &config,
                                            RENDERDOC_KillCallback cancelled = {}) override;

  ReplayOutputType GetType() { return m_Type; }
  ResultDetails SetPixelContext(WindowingData window);
  void SetPixelContextLocation(uint32_t x, uint32_t y);
  void DisablePixelContext();

  ResourceId GetCustomShaderTexID();
  ResourceId GetDebugOverlayTexID();
  rdcpair<uint32_t, uint32_t> PickVertex(uint32_t x, uint32_t y);

private:
  ReplayOutput(ReplayController *parent, WindowingData window, ReplayOutputType type);
  virtual ~ReplayOutput();

  void SetTextureDisplayInternal(const TextureDisplay &o, bool reapRetiredSnapshots);
  void SetFrameEvent(int eventId);
  void SetScrubFrameEvent(int eventId);
  void RefreshOverlay();

  void ClearBackground(uint64_t outputID, const FloatVector &backgroundColor);

  // ReplayOutput normally only renders already-live resources. Overlay and mesh preparation are
  // exceptions that replay capture commands directly, so route those calls through one helper to
  // invalidate a pending no-replay scrub commit first.
  void ReplayCapture(uint32_t eventId, ReplayLogType type);

  void DisplayContext();
  void DisplayTex();

  void DisplayMesh();

  uint64_t m_ThreadID;

  ReplayController *m_pController;

  bool m_CustomDirty;
  bool m_OverlayDirty;
  bool m_ForceOverlayRefresh;
  // The main backbuffer can temporarily contain another event while the persistent output state
  // remains unchanged. The next ordinary SetTextureDisplay must rebuild event-derived resources.
  bool m_TransientTextureEventDisplay = false;
  // A presentation-only history view binds a persistent snapshot instead of the mutable captured
  // texture. Keep the original request so direct controller mutations can refresh the binding on
  // the next Display(), even when the caller has no qrenderdoc viewer notification layer.
  bool m_HistorySnapshotActive = false;
  uint32_t m_HistorySnapshotEventID = 0;
  uint64_t m_HistorySnapshotGeneration = 0;
  TextureDisplay m_HistorySnapshotSource;
  ReplayHistoryDisplaySource m_HistoryReplaySource;
  IReplayDriver *m_pDevice;

  struct OutputPair
  {
    ResourceId texture;
    bool depthMode;
    Subresource sub;
    uint64_t wndHandle;
    CompType typeCast;
    uint64_t outputID;

    bool dirty;
  } m_MainOutput;

  rdcpair<uint32_t, uint32_t> m_TextureDim = {0, 0};

  ResourceId m_OverlayResourceId;
  ResourceId m_CustomShaderResourceId;

  rdcarray<OutputPair> m_Thumbnails;
  rdcarray<rdcpair<uint64_t, uint64_t>> m_ThumbnailGenerators;
  // keep 8 generators to avoid churn, but most thumbnails should be the same size so this means
  // during resize we don't create and destroy too many
  static const size_t MaxThumbnailGenerators = 8;

  float m_ContextX;
  float m_ContextY;
  OutputPair m_PixelContext;

  uint32_t m_EventID;
  ReplayOutputType m_Type;
  bool m_HeadlessWindow = false;

  rdcarray<uint32_t> passEvents;

  int32_t m_Width;
  int32_t m_Height;

  struct
  {
    TextureDisplay texDisplay;
    MeshDisplay meshDisplay;
  } m_RenderData;

  MeshComponentHighlight m_ComponentHighlight;

  friend struct ReplayController;
};

struct ReplayController : public IReplayController
{
public:
  ReplayController();

  APIProperties GetAPIProperties();

  RDResult CreateDevice(RDCFile *rdc, const ReplayOptions &opts);
  RDResult SetDevice(IReplayDriver *device);

  void FileChanged();

  void SetFrameEvent(uint32_t eventId, bool force);
  uint32_t GetCurrentEventID();
  void ScrubFrameEvent(uint32_t eventId);
  void CommitScrubFrameEvent(uint32_t eventId);
  void ConfigureReplayCheckpoints(bool enabled, uint64_t byteBudget = 0,
                                   uint32_t maxCheckpoints = 0);
  rdcstr GetReplayCheckpointStatus();
  rdcstr RunIdleCheckpointMaintenance(uint32_t expectedEventId,
                                      uint64_t expectedMutationGeneration,
                                      uint64_t expectedBufferGeneration);
  static bool EIDTimingEnabled();
  void ReplayFrameEventForAnalysis(uint32_t eventId);
  void RestoreFrameEventAfterAnalysis();
  ShaderPairComparison ComparePixelShaderPair(uint32_t eventId, ResourceId expectedOriginal,
                                               ResourceId baseline, ResourceId candidate);
  // Historical API name: this is the render-output mutation generation for shader, CBuffer, and
  // arbitrary resource replacements.
  uint64_t GetShaderReplacementGeneration(uint32_t eventId);
  bool ResolveEventTextureSnapshot(uint32_t eventId, const TextureDisplay &source,
                                   TextureDisplay &snapshot);
  rdcstr SetEventShaderOverride(uint32_t eventId, ShaderStage stage, ResourceId expectedOriginal,
                                ResourceId replacement, uint32_t outputTarget = ~0U,
                                bool independentFloatOutput = false);
  EventShaderOverrideState GetEventShaderOverride(uint32_t eventId, ShaderStage stage);
  TextureDescription GetEventShaderPreviewTexture(uint32_t eventId, ShaderStage stage);
  void ClearEventShaderOverride(uint32_t eventId, ShaderStage stage);
  void ClearEventShaderOverrides();
  rdcstr SetEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                 ResourceId expectedShader, ResourceId expectedBuffer,
                                 uint64_t expectedByteOffset, uint64_t expectedByteSize,
                                 bytebuf data, uint32_t registerSpace = 0);
  void ClearEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                 uint32_t registerSpace = 0);
  void ClearEventCBufferOverrides();
  rdcstr SetSharedBufferPatch(ResourceId buffer, uint64_t byteOffset, bytebuf data);
  rdcstr ClearSharedBufferPatch(ResourceId buffer, uint64_t byteOffset, uint64_t byteLength);
  rdcstr ResetSharedBufferRange(ResourceId buffer, uint64_t byteOffset, uint64_t byteLength);
  rdcstr ClearSharedBufferPatches();
  uint64_t GetSharedBufferPatchGeneration();
  void SetSelectedEventID(uint32_t selectedEventId)
  {
    m_PrevSelectedEventID = m_SelectedEventID;
    m_SelectedEventID = selectedEventId;
  }
  uint32_t GetSelectedEventID() const { return m_SelectedEventID; }

  const D3D11Pipe::State *GetD3D11PipelineState();
  const D3D12Pipe::State *GetD3D12PipelineState();
  const GLPipe::State *GetGLPipelineState();
  const VKPipe::State *GetVulkanPipelineState();
  const PipeState &GetPipelineState();
  rdcarray<Descriptor> GetDescriptors(ResourceId descriptorStore,
                                      const rdcarray<DescriptorRange> &ranges);
  rdcarray<SamplerDescriptor> GetSamplerDescriptors(ResourceId descriptorStore,
                                                    const rdcarray<DescriptorRange> &ranges);
  const rdcarray<DescriptorAccess> &GetDescriptorAccess();
  rdcarray<DescriptorLogicalLocation> GetDescriptorLocations(ResourceId descriptorStore,
                                                             const rdcarray<DescriptorRange> &ranges);

  rdcarray<rdcstr> GetDisassemblyTargets(bool withPipeline);
  rdcstr DisassembleShader(ResourceId pipeline, const ShaderReflection *refl, const rdcstr &target);

  void SetCustomShaderIncludes(const rdcarray<rdcstr> &directories);
  rdcpair<ResourceId, rdcstr> BuildCustomShader(const rdcstr &entry, ShaderEncoding sourceEncoding,
                                                bytebuf source,
                                                const ShaderCompileFlags &compileFlags,
                                                ShaderStage type);
  void FreeCustomShader(ResourceId id);

  rdcarray<ShaderEncoding> GetCustomShaderEncodings();
  rdcarray<ShaderSourcePrefix> GetCustomShaderSourcePrefixes();
  rdcarray<ShaderEncoding> GetTargetShaderEncodings();
  rdcpair<ResourceId, rdcstr> BuildTargetShader(const rdcstr &entry, ShaderEncoding sourceEncoding,
                                                bytebuf source,
                                                const ShaderCompileFlags &compileFlags,
                                                ShaderStage type);
  void ReplaceResource(ResourceId from, ResourceId to);
  void RemoveReplacement(ResourceId id);
  rdcstr ReplaceTargetShaderChecked(ResourceId from, ResourceId to);
  uint64_t BeginShaderReplacementBatch();
  bool EndShaderReplacementBatch(uint64_t token);
  ResourceId GetTargetShaderReplacement(ResourceId original);
  rdcstr RemoveTargetShaderReplacementChecked(ResourceId id);
  void FreeTargetResource(ResourceId id);
  void ClearReplayCache();
  void ReloadShaderDebugInformation();

  FrameDescription GetFrameInfo();
  const SDFile &GetStructuredFile();
  const rdcarray<ActionDescription> &GetRootActions();
  void AddFakeMarkers();
  rdcarray<CounterResult> FetchCounters(const rdcarray<GPUCounter> &counters);
  rdcarray<GPUCounter> EnumerateCounters();
  CounterDescription DescribeCounter(GPUCounter counterID);
  const rdcarray<TextureDescription> &GetTextures();
  const rdcarray<BufferDescription> &GetBuffers();
  const rdcarray<DescriptorStoreDescription> &GetDescriptorStores();
  const rdcarray<ResourceDescription> &GetResources();
  rdcarray<DebugMessage> GetDebugMessages();
  ResultDetails GetFatalErrorStatus()
  {
    // don't reconvert this on return every time, or we would cache many strings if this function is
    // repeatedly called (which it is intended to be)
    if(m_FatalError.code != m_FatalErrorResult.code)
      m_FatalErrorResult = m_FatalError;
    return m_FatalErrorResult;
  }
  rdcarray<ShaderEntryPoint> GetShaderEntryPoints(ResourceId shader);
  const ShaderReflection *GetShader(ResourceId pipeline, ResourceId shader, ShaderEntryPoint entry);

  PixelValue PickPixel(ResourceId textureId, uint32_t x, uint32_t y, const Subresource &sub,
                       CompType typeCast);
  rdcpair<PixelValue, PixelValue> GetMinMax(ResourceId textureId, const Subresource &sub,
                                            CompType typeCast);
  rdcarray<uint32_t> GetHistogram(ResourceId textureId, const Subresource &sub, CompType typeCast,
                                  float minval, float maxval, const rdcfixedarray<bool, 4> &channels);
  rdcarray<PixelModification> PixelHistory(ResourceId target, uint32_t x, uint32_t y,
                                           const Subresource &sub, CompType typeCast);
  ShaderDebugTrace *DebugVertex(uint32_t vertid, uint32_t instid, uint32_t idx, uint32_t view);
  ShaderDebugTrace *DebugPixel(uint32_t x, uint32_t y, const DebugPixelInputs &inputs);
  ShaderDebugTrace *DebugThread(const rdcfixedarray<uint32_t, 3> &groupid,
                                const rdcfixedarray<uint32_t, 3> &threadid);
  ShaderDebugTrace *DebugMeshThread(const rdcfixedarray<uint32_t, 3> &groupid,
                                    const rdcfixedarray<uint32_t, 3> &threadid);
  rdcarray<ShaderDebugState> ContinueDebug(ShaderDebugger *debugger);
  void FreeTrace(ShaderDebugTrace *trace);

  MeshFormat GetPostVSData(uint32_t instID, uint32_t viewID, MeshDataStage stage);

  rdcarray<EventUsage> GetUsage(ResourceId id);

  bytebuf GetBufferData(ResourceId buff, uint64_t offset, uint64_t len);
  bytebuf GetTextureData(ResourceId buff, const Subresource &sub);

  ResultDetails SaveTexture(const TextureSave &saveData, const rdcstr &path);

  rdcarray<ShaderVariable> GetCBufferVariableContents(ResourceId pipeline, ResourceId shader,
                                                      ShaderStage stage, const rdcstr &entryPoint,
                                                      uint32_t cbufslot, ResourceId buffer,
                                                      uint64_t offset, uint64_t length);

  rdcarray<WindowingSystem> GetSupportedWindowSystems();

  void ReplayLoop(WindowingData window, ResourceId texid);
  void CancelReplayLoop();

  rdcstr CreateRGPProfile(WindowingData window);

  ReplayOutput *CreateOutput(WindowingData window, ReplayOutputType type);

  void ShutdownOutput(IReplayOutput *output);
  void Shutdown();

  bool FatalErrorCheck();

private:
  virtual ~ReplayController();
  RDResult PostCreateInit(IReplayDriver *device, RDCFile *rdc);

  void FetchPipelineState(uint32_t eventId);
  void ReplayEvent(uint32_t eventId, bool updateOutputs, bool fetchPipelineState,
                   bool allowFullReplay = true);
  void ReplayScrubFrameEvent(uint32_t eventId);
  double ReplayDriverBoundary(uint32_t eventId, ReplayLogType replayType);
  enum class AnalysisReplayState
  {
    None,
    PendingOrFailed,
    Ready,
  };
  enum class ExplicitResourceReadSource
  {
    CurrentEvent,
    AnalysisReplay,
    Unavailable,
  };
  bool IsCurrentEventMaterialized() const
  {
    return m_AnalysisReplayState == AnalysisReplayState::None && m_MaterializedEventValid &&
           m_MaterializedEventID == m_EventID;
  }
  bool EnsureMaterializedCurrentEvent(const char *reason = "unspecified");
  ExplicitResourceReadSource GetExplicitResourceReadSource() const;
  bool EnsureExplicitResourceRead(const char *reason);
  void EndAnalysisForMutation();
  bool ApplyAnalysisEventOverrides(uint32_t eventId);
  void ReapplyAnalysisEventOverrides(uint32_t eventId);
  void RestoreAnalysisEventOverrides();
  bool IsShaderResource(ResourceId id) const;
  void MarkGlobalShaderReplacementChanged();
  void MarkEventShaderReplacementChanged(uint32_t eventId);
  bool HasEventOverrides(uint32_t eventId) const;
  void InvalidateScrubReplay() { m_ScrubReplayValid = false; }
  void RetireEventTextureSnapshots(const rdcarray<ResourceId> &snapshots);
  void ReapRetiredEventTextureSnapshots();
  void TrimEventTextureSnapshots(ResourceId keepSnapshot);
  void PlanReplayCheckpoints();
  void ConfigureInitialReplayCheckpoints();
  void ResetAutomaticCheckpointCosts(uint32_t firstAffectedEvent);
  struct AutomaticCheckpointRequest
  {
    bool active = false, reference = false, previousBypass = false;
    bool previousNaturalReference = false;
    uint64_t key = 0;
    double configurationMS = 0.0;
    ReplayCheckpointStats before;
  };
  AutomaticCheckpointRequest BeginAutomaticCheckpointRequest(uint32_t eventId, uint32_t path);
  void EndAutomaticCheckpointRequest(const AutomaticCheckpointRequest &request, double driverMS);
  void ObserveAutomaticCheckpointRequest(const AutomaticCheckpointRequest &request, double driverMS,
                                         const ReplayCheckpointStats &after);
  void ApplyAutomaticCheckpointConfiguration();

  ActionDescription *GetActionByEID(uint32_t eventId);
  bool ContainsMarker(const rdcarray<ActionDescription> &actions);
  bool PassEquivalent(const ActionDescription &a, const ActionDescription &b);

  IReplayDriver *GetDevice() { return m_pDevice; }
  FrameRecord m_FrameRecord;
  rdcarray<ActionDescription *> m_Actions;

  uint64_t m_ThreadID;

  APIProperties m_APIProps;
  rdcarray<rdcstr> m_GCNTargets;

  int32_t m_ReplayLoopCancel = 0;
  int32_t m_ReplayLoopFinished = 0;

  RDResult m_FatalError = ResultCode::Succeeded;
  ResultDetails m_FatalErrorResult = {ResultCode::Succeeded};

  uint32_t m_EventID;
  rdcstr m_LastEventOverrideError;
  uint32_t m_SelectedEventID = 0;
  uint32_t m_PrevSelectedEventID = 0;
  uint32_t m_ScrubReplayEventID = 0;
  bool m_ScrubReplayValid = false;
  // Analysis and overlay replay can temporarily change the actual device state. Ordinary operations
  // restore the selected event; only explicit resource readbacks may consume a completed analysis.
  uint32_t m_MaterializedEventID = 0;
  bool m_MaterializedEventValid = false;
  // Resource contents and fetched pipeline metadata have separate lifetimes:
  // analysis may restore the event without fetching its pipeline again.
  bool m_PipelineStateValid = false;
  uint32_t m_PipelineStateEventID = 0;
  uint64_t m_PipelineStateShaderGeneration = 0, m_PipelineStateBufferGeneration = 0;
  AnalysisReplayState m_AnalysisReplayState = AnalysisReplayState::None;
  uint32_t m_AnalysisReplayEventID = 0;

  std::map<uint32_t, uint32_t> m_EventRemap;

  D3D11Pipe::State m_D3D11PipelineState;
  D3D12Pipe::State m_D3D12PipelineState;
  GLPipe::State m_GLPipelineState;
  VKPipe::State m_VulkanPipelineState;
  PipeState m_PipeState;

  rdcarray<ReplayOutput *> m_Outputs;

  rdcarray<ResourceDescription> m_Resources;
  rdcarray<BufferDescription> m_Buffers;
  rdcarray<DescriptorStoreDescription> m_DescriptorStores;
  rdcarray<TextureDescription> m_Textures;

  IReplayDriver *m_pDevice;
  ReplayCheckpointConfig m_CheckpointConfig;
  rdcarray<ReplayCheckpointCandidate> m_CheckpointCandidates;
  uint64_t m_CheckpointCaptureBytes = 0;
  uint64_t m_CheckpointResourceBytes = 0;
  bool m_CheckpointAutomatic = false;
  // CPU planning happens at load. GPU prewarming is admitted only by idle maintenance.
  bool m_CheckpointIdlePrewarm = false;
  const char *m_CheckpointControlMode = "unconfigured";
  const char *m_CheckpointAutoState = "observing";
  const char *m_CheckpointAutoReason = "awaitingNaturalReferenceVisits";
  bool m_CheckpointAutoConfigurationDirty = false;
  bool m_IdleCheckpointMaintenance = false;
  bool m_IdleCheckpointAttempted = false;
  uint64_t m_IdleCheckpointRevision = 0, m_IdleCheckpointLastRevision = 0;
  uint64_t m_IdleCheckpointLastMutation = 0, m_IdleCheckpointLastBuffer = 0;
  uint32_t m_IdleCheckpointLastEvent = 0;
  uint64_t m_IdleCheckpointWorkUnits = 0;
  double m_IdleCheckpointTotalMS = 0.0;
  uint32_t m_CheckpointAutoCapacity = 0;
  uint64_t m_CheckpointAutoReferences = 0;
  uint32_t m_CheckpointAutoTrials = 0;
  bool m_SharedBufferPatchReplay = false;
  double m_CheckpointAutoDebtMS = 0.0, m_CheckpointAutoSavingsMS = 0.0;
  struct CheckpointReferenceCost
  {
    double milliseconds[2] = {};
    uint32_t count = 0, next = 0, hitsSinceReference = 0;
    void Add(double ms);
    double Baseline() const;
  };
  struct AutomaticCheckpointPoint
  {
    ReplayCheckpointCandidate candidate;
    std::set<uint32_t> nativeEvents;
    bool retained = false;
    bool bufferEdit = false;
    bool prewarmed = false;
    uint32_t requests = 0, builds = 0, hits = 0, profitableHits = 0;
    uint32_t unmeasuredHits = 0;
    double baselineMS = 0.0, lastHitMS = 0.0, debtMS = 0.0;
  };
  std::map<uint64_t, CheckpointReferenceCost> m_CheckpointReferenceCosts;
  rdcarray<AutomaticCheckpointPoint> m_AutomaticCheckpointPoints;
  std::set<uint32_t> m_RejectedCheckpointCandidates;
  // Memory rejection is reversible only after a real backend budget change. Safety/performance
  // rejections remain in the ordinary set. Checks and successful recoveries are capture-bounded.
  std::set<uint32_t> m_MemoryRejectedCheckpointCandidates;
  uint32_t m_IdleCheckpointMemoryChecks = 0, m_IdleCheckpointMemoryRecoveries = 0;
  uint64_t m_IdleCheckpointMemoryNextCheck = 0;
  ReplayBufferPatchSet m_SharedBufferPatches;
  uint64_t m_SharedBufferPatchGeneration = 0;
  rdcstr CommitSharedBufferPatches(const ReplayBufferPatchSet &patches);

  rdcarray<ShaderDebugger *> m_Debuggers;

  // Native shader creation can return an existing ResourceId with another reference (D3D12
  // deduplicates identical bytecode). Track each BuildTargetShader acquisition independently.
  std::map<ResourceId, uint32_t> m_TargetResources;
  std::set<ResourceId> m_CustomShaders;

  uint64_t m_ShaderReplacementSerial = 1;
  uint64_t m_GlobalShaderReplacementGeneration = 1;
  bool m_AnalysisEventOverridesActive = false;
  std::map<uint32_t, uint64_t> m_EventShaderReplacementGenerations;
  std::map<ResourceId, ResourceId> m_GlobalShaderReplacements;
  uint64_t m_ShaderBatchSerial = 0;
  uint64_t m_ShaderBatchToken = 0;
  bool m_ShaderBatchDirty = false;
  struct EventShaderReplacement
  {
    ResourceId original;
    ResourceId replacement;
    uint32_t outputTarget = ~0U;
    bool independentFloatOutput = false;
  };
  std::map<std::pair<uint32_t, ShaderStage>, EventShaderReplacement> m_EventShaderReplacements;

  struct EventCBufferOverrideKey
  {
    uint32_t eventId;
    ShaderStage stage;
    uint32_t slot;
    uint32_t registerSpace;

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
  std::set<EventCBufferOverrideKey> m_EventCBufferOverrides;

  struct EventTextureSnapshot
  {
    uint32_t eventId = 0;
    ResourceId source;
    Subresource sourceSubresource;
    CompType sourceTypeCast = CompType::Typeless;
    ResourceId snapshot;
    Subresource snapshotSubresource;
    CompType snapshotTypeCast = CompType::Typeless;
    uint64_t outputMutationGeneration = 0;
    uint64_t allocationBytes = 0;
    uint64_t lastUseSerial = 0;
    bool nativeGPUSnapshot = false;
    bool unavailable = false;
  };
  rdcarray<EventTextureSnapshot> m_EventTextureSnapshots;
  // Cache replacement and LRU eviction are two-phase. ReplayOutputs can keep a snapshot bound
  // across repaint/resize, so resources are destroyed only after no output and no live cache entry
  // references them.
  std::set<ResourceId> m_RetiredEventTextureSnapshots;
  uint64_t m_EventTextureSnapshotBytes = 0;
  uint64_t m_EventTextureSnapshotUseSerial = 0;

  friend struct ReplayOutput;
#if ENABLED(ENABLE_UNIT_TESTS)
  friend struct ReplayControllerTestAccess;
#endif
};
