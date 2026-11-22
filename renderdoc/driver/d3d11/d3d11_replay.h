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

#include <memory>
#include "api/replay/renderdoc_replay.h"
#include "core/core.h"
#include "replay/replay_driver.h"
#include "replay/replay_resource_versions.h"
#include "d3d11_common.h"
#include "d3d11_renderstate.h"

class D3D11DebugManager;

class WrappedID3D11Device;
class WrappedID3D11DeviceContext;

class AMDCounters;
class NVCounters;
class NVD3D11Counters;
class IntelCounters;
struct D3D11CounterContext;

struct D3D11PostVSData
{
  struct InstData
  {
    uint32_t numVerts = 0;
    uint32_t bufOffset = 0;
  };

  struct StageData
  {
    ID3D11Buffer *buf = NULL;
    D3D11_PRIMITIVE_TOPOLOGY topo = D3D_PRIMITIVE_TOPOLOGY_UNDEFINED;

    uint32_t vertStride = 0;

    // simple case - uniform
    uint32_t numVerts = 0;
    uint32_t instStride = 0;

    // complex case - expansion per instance
    rdcarray<InstData> instData;

    bool useIndices = false;
    ID3D11Buffer *idxBuf = NULL;
    DXGI_FORMAT idxFmt = DXGI_FORMAT_UNKNOWN;

    bool hasPosOut = false;

    float nearPlane = 0.0f;
    float farPlane = 0.0f;

    rdcstr status;
  } vsin, vsout, gsout;

  const StageData &GetStage(MeshDataStage type)
  {
    if(type == MeshDataStage::VSOut)
      return vsout;
    else if(type == MeshDataStage::GSOut)
      return gsout;

    if(type == MeshDataStage::Count)
    {
      if(gsout.buf)
        return gsout;

      return vsout;
    }

    RDCERR("Unexpected mesh data stage!");

    return vsin;
  }
};

enum TexDisplayFlags
{
  eTexDisplay_None = 0,
  eTexDisplay_BlendAlpha = 0x1,
  eTexDisplay_RemapFloat = 0x2,
  eTexDisplay_RemapUInt = 0x4,
  eTexDisplay_RemapSInt = 0x8,
};

struct ShaderDebugging
{
  void Init(WrappedID3D11Device *device);
  void Release();

  ID3D11PixelShader *GetSamplePS(const int8_t offsets[3]);

  ID3D11ComputeShader *MathCS = NULL;
  ID3D11Buffer *ParamBuf = NULL;
  ID3D11Buffer *OutBuf = NULL;
  ID3D11Buffer *OutStageBuf = NULL;
  ID3D11UnorderedAccessView *OutUAV = NULL;

  ID3D11VertexShader *SampleVS = NULL;
  ID3D11PixelShader *SamplePS = NULL;
  ID3D11Texture2D *DummyTex = NULL;
  ID3D11RenderTargetView *DummyRTV = NULL;

private:
  WrappedID3D11Device *m_pDevice;

  std::map<uint32_t, ID3D11PixelShader *> m_OffsetSamplePS;
};

class D3D11Replay : public IReplayDriver
{
public:
  D3D11Replay(WrappedID3D11Device *d);
  ~D3D11Replay();

  void SetProxy(bool p, bool warp)
  {
    m_Proxy = p;
    m_WARP = warp;
  }
  bool IsRemoteProxy() { return m_Proxy; }
  void Shutdown();

  RDResult FatalErrorCheck();
  IReplayDriver *MakeDummyDriver();

  void InitReplayOnDevice(IDXGIFactory *factory);

  void CreateResources();

  void DestroyResources();

  DriverInformation GetDriverInfo() { return m_DriverInfo; }
  rdcarray<GPUDevice> GetAvailableGPUs();
  APIProperties GetAPIProperties();

  ResourceDescription &GetResourceDesc(ResourceId id);
  rdcarray<ResourceDescription> GetResources();

  rdcarray<DescriptorStoreDescription> GetDescriptorStores() { return {}; }

  rdcarray<BufferDescription> GetBuffers();
  BufferDescription GetBuffer(ResourceId id);

  rdcarray<TextureDescription> GetTextures();
  TextureDescription GetTexture(ResourceId id);

  rdcarray<DebugMessage> GetDebugMessages();

  rdcarray<ShaderEntryPoint> GetShaderEntryPoints(ResourceId shader);
  ShaderReflection *GetShader(ResourceId pipeline, ResourceId shader, ShaderEntryPoint entry);

  rdcarray<rdcstr> GetDisassemblyTargets(bool withPipeline);
  rdcstr DisassembleShader(ResourceId pipeline, const ShaderReflection *refl, const rdcstr &target);

  rdcarray<EventUsage> GetUsage(ResourceId id);

  FrameRecord &WriteFrameRecord() { return m_FrameRecord; }
  FrameRecord GetFrameRecord() { return m_FrameRecord; }
  void SetPipelineStates(D3D11Pipe::State *d3d11, D3D12Pipe::State *d3d12, GLPipe::State *gl,
                         VKPipe::State *vk)
  {
    m_D3D11PipelineState = d3d11;
  }
  ShaderDebugging &GetShaderDebuggingData() { return m_ShaderDebug; }
  void SavePipelineState(uint32_t eventId);
  rdcarray<Descriptor> GetDescriptors(ResourceId descriptorStore,
                                      const rdcarray<DescriptorRange> &ranges);
  rdcarray<SamplerDescriptor> GetSamplerDescriptors(ResourceId descriptorStore,
                                                    const rdcarray<DescriptorRange> &ranges);
  rdcarray<DescriptorAccess> GetDescriptorAccess(uint32_t eventId);
  rdcarray<DescriptorLogicalLocation> GetDescriptorLocations(ResourceId descriptorStore,
                                                             const rdcarray<DescriptorRange> &ranges);
  void FreeTargetResource(ResourceId id);
  void FreeCustomShader(ResourceId id);

  RDResult ReadLogInitialisation(RDCFile *rdc, bool storeStructuredBuffers);
  void ReplayLog(uint32_t endEventID, ReplayLogType replayType);
  void ConfigureReplayCheckpoints(const ReplayCheckpointConfig &config,
                                  const rdcarray<ReplayCheckpointCandidate> &candidates) override;
  void InvalidateReplayCheckpoints(uint32_t firstAffectedEvent = 0) override;
  ReplayCheckpointStats GetReplayCheckpointStats() override;
  rdcstr SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches) override;
  uint32_t GetReplayBufferPatchFirstAffectedEvent() const override { return m_BufferPatchFirstAffected; }
  void RecordBufferPatchConsumer(ResourceId buffer, uint32_t eventId, uint64_t byteOffset = 0,
                                 uint64_t byteLength = ~0ULL);
  bool ApplyPendingBufferPatches();
  void InvalidateBufferPatchContents() { m_BufferPatchesDirty = !m_BufferPatches.empty(); }
  void RecordCheckpointPotentialWrite(ResourceId resource, uint32_t eventId);
  bool HasBufferPatches() const { return !m_BufferPatches.empty(); }
  void InvalidateLiveForward();
  SDFile *GetStructuredFile();

  rdcarray<uint32_t> GetPassEvents(uint32_t eventId);

  rdcarray<WindowingSystem> GetSupportedWindowSystems()
  {
    rdcarray<WindowingSystem> ret;
    ret.push_back(WindowingSystem::Win32);
    return ret;
  }

  AMDRGPControl *GetRGPControl() { return NULL; }
  uint64_t MakeOutputWindow(WindowingData window, bool depth);
  void DestroyOutputWindow(uint64_t id);
  bool CheckResizeOutputWindow(uint64_t id);
  void GetOutputWindowDimensions(uint64_t id, int32_t &w, int32_t &h);
  void GetOutputWindowData(uint64_t id, bytebuf &retData);
  void ClearOutputWindowColor(uint64_t id, FloatVector col);
  void ClearOutputWindowDepth(uint64_t id, float depth, uint8_t stencil);
  void BindOutputWindow(uint64_t id, bool depth);
  bool IsOutputWindowVisible(uint64_t id);
  void FlipOutputWindow(uint64_t id);

  void InitPostVSBuffers(uint32_t eventId);
  void InitPostVSBuffers(const rdcarray<uint32_t> &passEvents);

  void PickPixel(ResourceId texture, uint32_t x, uint32_t y, const Subresource &sub,
                 CompType typeCast, float pixel[4]);
  bool GetMinMax(ResourceId texid, const Subresource &sub, CompType typeCast, float *minval,
                 float *maxval);
  bool GetHistogram(ResourceId texid, const Subresource &sub, CompType typeCast, float minval,
                    float maxval, const rdcfixedarray<bool, 4> &channels,
                    rdcarray<uint32_t> &histogram);

  MeshFormat GetPostVSBuffers(uint32_t eventId, uint32_t instID, uint32_t viewID,
                              MeshDataStage stage);

  void GetBufferData(ResourceId buff, uint64_t offset, uint64_t len, bytebuf &retData);
  void GetTextureData(ResourceId tex, const Subresource &sub, const GetTextureDataParams &params,
                      bytebuf &data);

  rdcarray<ShaderEncoding> GetCustomShaderEncodings()
  {
    return {ShaderEncoding::DXBC, ShaderEncoding::HLSL};
  }
  rdcarray<ShaderSourcePrefix> GetCustomShaderSourcePrefixes();
  rdcarray<ShaderEncoding> GetTargetShaderEncodings()
  {
    return {ShaderEncoding::DXBC, ShaderEncoding::HLSL};
  }
  void BuildTargetShader(ShaderEncoding sourceEncoding, const bytebuf &source, const rdcstr &entry,
                         const ShaderCompileFlags &compileFlags, ShaderStage type, ResourceId &id,
                         rdcstr &errors);
  void ReplaceResource(ResourceId from, ResourceId to);
  void RemoveReplacement(ResourceId id);
  rdcstr ReplaceTargetShaderChecked(ResourceId from, ResourceId to) override;
  rdcstr RemoveTargetShaderReplacementChecked(ResourceId id) override;
  void ClearReplayCache();
  void ReloadShaderDebugInformation();
  rdcstr SetEventShaderOverride(uint32_t eventId, ShaderStage stage, ResourceId expectedOriginal,
                                ResourceId replacement, uint32_t outputTarget = ~0U,
                                bool independentFloatOutput = false);
  TextureDescription GetEventShaderPreviewTexture(uint32_t eventId, ShaderStage stage);
  void ClearEventShaderOverride(uint32_t eventId, ShaderStage stage);
  void ClearEventShaderOverrides();
  void ClearEventShaderOverridesForResource(ResourceId id);
  rdcstr ApplyEventShaderOverrides(uint32_t eventId);
  void RestoreEventShaderOverrides();
  rdcstr SetEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                 ResourceId expectedShader, ResourceId expectedBuffer,
                                 uint64_t expectedByteOffset, uint64_t expectedByteSize,
                                 const bytebuf &data, uint32_t registerSpace = 0);
  void ClearEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                 uint32_t registerSpace = 0);
  void ClearEventCBufferOverrides();
  rdcstr ApplyEventCBufferOverrides(uint32_t eventId);
  void RestoreEventCBufferOverrides();

  rdcarray<GPUCounter> EnumerateCounters();
  CounterDescription DescribeCounter(GPUCounter counterID);
  rdcarray<CounterResult> FetchCounters(const rdcarray<GPUCounter> &counters);

  ResourceId CreateProxyTexture(const TextureDescription &templateTex);
  ResourceId CreateProxyTextureSnapshot(const TextureDescription &templateTex);
  bool SupportsProxyTextureSnapshots() { return true; }
  void SetProxyTextureData(ResourceId texid, const Subresource &sub, byte *data, size_t dataSize);
  ResourceId CreateTextureSnapshot(ResourceId source, const Subresource &sourceSub,
                                   CompType typeCast, Subresource &snapshotSub);
  void FreeTextureSnapshot(ResourceId snapshot);
  void FreeTextureSnapshots(const rdcarray<ResourceId> &snapshots);
  bool IsTextureSupported(const TextureDescription &tex);
  bool NeedRemapForFetch(const ResourceFormat &format);

  ResourceId CreateProxyBuffer(const BufferDescription &templateBuf);
  void SetProxyBufferData(ResourceId bufid, byte *data, size_t dataSize);

  void RenderMesh(uint32_t eventId, const rdcarray<MeshFormat> &secondaryDraws,
                  const MeshDisplay &cfg, const MeshComponentHighlight &components);

  bool RenderTexture(TextureDisplay cfg);

  void RenderCheckerboard(FloatVector dark, FloatVector light);

  void RenderHighlightBox(float w, float h, float scale);

  void FillCBufferVariables(ResourceId pipeline, ResourceId shader, ShaderStage stage,
                            rdcstr entryPoint, uint32_t cbufSlot, rdcarray<ShaderVariable> &outvars,
                            const bytebuf &data);

  rdcarray<PixelModification> PixelHistory(rdcarray<EventUsage> events, ResourceId target, uint32_t x,
                                           uint32_t y, const Subresource &sub, CompType typeCast);
  ShaderDebugTrace *DebugVertex(uint32_t eventId, uint32_t vertid, uint32_t instid, uint32_t idx,
                                uint32_t view);
  ShaderDebugTrace *DebugPixel(uint32_t eventId, uint32_t x, uint32_t y,
                               const DebugPixelInputs &inputs);
  ShaderDebugTrace *DebugThread(uint32_t eventId, const rdcfixedarray<uint32_t, 3> &groupid,
                                const rdcfixedarray<uint32_t, 3> &threadid);
  ShaderDebugTrace *DebugMeshThread(uint32_t eventId, const rdcfixedarray<uint32_t, 3> &groupid,
                                    const rdcfixedarray<uint32_t, 3> &threadid);
  rdcarray<ShaderDebugState> ContinueDebug(ShaderDebugger *debugger);
  void FreeDebugger(ShaderDebugger *debugger);

  uint32_t PickVertex(uint32_t eventId, int32_t width, int32_t height, const MeshDisplay &cfg,
                      uint32_t x, uint32_t y);

  ResourceId RenderOverlay(ResourceId texid, FloatVector clearCol, DebugOverlay overlay,
                           uint32_t eventId, const rdcarray<uint32_t> &passEvents);

  void SetCustomShaderIncludes(const rdcarray<rdcstr> &directories);
  void BuildCustomShader(ShaderEncoding sourceEncoding, const bytebuf &source, const rdcstr &entry,
                         const ShaderCompileFlags &compileFlags, ShaderStage type, ResourceId &id,
                         rdcstr &errors);
  ResourceId ApplyCustomShader(TextureDisplay &display);

  RenderOutputSubresource GetRenderOutputSubresource(ResourceId id);
  bool IsRenderOutput(ResourceId id) { return GetRenderOutputSubresource(id).mip != ~0U; }
  void FileChanged() {}
private:
  bool m_WARP;
  bool m_Proxy;

  D3D11DebugManager *GetDebugManager();
  // shared by BuildCustomShader and BuildTargetShader
  void BuildShader(ShaderEncoding sourceEncoding, const bytebuf &source, const rdcstr &entry,
                   const ShaderCompileFlags &compileFlags, const rdcarray<rdcstr> &includeDirs,
                   ShaderStage type, ResourceId &id, rdcstr &errors);

  void ClearPostVSCache();

  void InitStreamOut();
  void CreateSOBuffers();
  void ShutdownStreamOut();

  rdcarray<CounterResult> FetchCountersAMD(const rdcarray<GPUCounter> &counters);
  rdcarray<CounterResult> FetchCountersNV(const rdcarray<GPUCounter> &counters);
  rdcarray<CounterResult> FetchCountersIntel(const rdcarray<GPUCounter> &counters);

  void FillTimers(D3D11CounterContext &ctx, const ActionDescription &actionnode);
  void FillTimersAMD(uint32_t &eventStartID, uint32_t &sampleIndex, rdcarray<uint32_t> &eventIDs,
                     const ActionDescription &actionnode);
  void FillTimersNV(uint32_t &eventStartID, uint32_t &sampleIndex, rdcarray<uint32_t> &eventIDs,
                    const ActionDescription &actionnode);
  void FillTimersIntel(uint32_t &eventStartID, uint32_t &sampleIndex, rdcarray<uint32_t> &eventIDs,
                       const ActionDescription &actionnode);

  void SerializeImmediateContext();

  bool RenderTextureInternal(TextureDisplay cfg, TexDisplayFlags flags);

  void CreateCustomShaderTex(uint32_t w, uint32_t h);

  void SetOutputDimensions(int w, int h)
  {
    m_OutputWidth = float(w);
    m_OutputHeight = float(h);
  }

  rdcarray<ID3D11Resource *> m_ProxyResources;
  rdcarray<ResourceId> m_TextureSnapshots;
  std::map<ResourceId, TextureDescription> m_ProxyResourceOrigInfo;

  struct OutputWindow
  {
    HWND wnd;
    IDXGISwapChain *swap;
    ID3D11RenderTargetView *rtv;
    ID3D11DepthStencilView *dsv;

    WrappedID3D11Device *dev;

    void MakeRTV();
    void MakeDSV();

    int width, height;
    bool multisampled;
  };

  float m_OutputWidth = 1.0f;
  float m_OutputHeight = 1.0f;

  uint64_t m_OutputWindowID = 1;
  std::map<uint64_t, OutputWindow> m_OutputWindows;

  IDXGIFactory *m_pFactory = NULL;

  DriverInformation m_DriverInfo;

  AMDCounters *m_pAMDCounters = NULL;
  NVCounters *m_pNVCounters = NULL;
  NVD3D11Counters *m_pNVPerfCounters = NULL;
  IntelCounters *m_pIntelCounters = NULL;

  WrappedID3D11Device *m_pDevice = NULL;
  WrappedID3D11DeviceContext *m_pImmediateContext = NULL;

  struct ReplayCheckpoint;
  struct CheckpointResource;
  struct CheckpointVersion;
  bool PrepareCheckpointResources();
  bool SaveReplayCheckpoint(uint32_t eventId);
  bool RestoreReplayCheckpoint(ReplayCheckpoint &checkpoint);
  void UpdateCheckpointAccounting();
  void EvictOldestCheckpoint();
  void ForgetCheckpointWrites(uint32_t firstEvent, uint32_t lastEvent);
  ReplayCheckpointConfig m_CheckpointConfig;
  ReplayCheckpointStats m_CheckpointStats;
  rdcarray<ReplayCheckpointCandidate> m_CheckpointCandidates;
  rdcarray<CheckpointResource> m_CheckpointResources;
  std::map<uint32_t, std::unique_ptr<ReplayCheckpoint>> m_Checkpoints;
  // Weak ownership cannot keep an evicted GPU backup outside the checkpoint memory budget.
  std::map<ResourceId, std::weak_ptr<CheckpointVersion>> m_CheckpointResidentVersions;
  std::map<ResourceId, rdcarray<uint32_t>> m_CheckpointPotentialWrites;
  uint64_t m_CheckpointPotentialWriteCount = 0;
  uint64_t m_CheckpointResidentSerial = 0;
  uint64_t m_CheckpointUseSerial = 0;
  uint32_t m_CheckpointCurrentEvent = 0;
  bool m_CheckpointPrepared = false;
  bool m_CheckpointPreparingFailed = false;
  // After editing, recorded usage is no longer proof that a replacement shader cannot write.
  bool m_CheckpointConservativeWrites = false;
  rdcarray<ReplayBufferPatch> m_BufferPatches;
  uint64_t m_BufferPatchImmutableInitialBytes = 0;
  uint32_t m_BufferPatchImmutableInitialCount = 0;
  std::map<ResourceId, ID3D11Buffer *> m_BufferPatchScratch;
  bool m_BufferPatchesDirty = false;
  std::map<ResourceId, uint32_t> m_BufferPatchFirstConsumers;
  ReplayResourceAccessIndex m_BufferPatchReadIndex;
  std::set<ResourceId> m_BufferPatchReadRegistered;
  uint32_t m_BufferPatchReadLastEvent = 0;
  uint32_t m_BufferPatchFirstAffected = 0;
  uint32_t m_BufferPatchAnchorEvent = 0;

  rdcarray<rdcstr> m_CustomShaderIncludes;

  struct EventShaderOverride
  {
    ResourceId expectedOriginal;
    ResourceId replacementId;
    ID3D11DeviceChild *replacement = NULL;
    uint32_t shaderExtSlot = ~0U;
    uint32_t outputTarget = ~0U;
    bool independentFloatOutput = false;
  };

  struct AppliedEventShaderOverride
  {
    ShaderStage stage = ShaderStage::Vertex;
    ID3D11DeviceChild *original = NULL;
    ID3D11ClassInstance *instances[D3D11_SHADER_MAX_INTERFACES] = {};
    UINT numInstances = 0;
  };

  std::map<std::pair<uint32_t, ShaderStage>, EventShaderOverride> m_EventShaderOverrides;
  rdcarray<AppliedEventShaderOverride> m_AppliedEventShaderOverrides;
  std::map<std::pair<ResourceId, uint32_t>, ID3D11BlendState *> m_EventShaderBlendStates;
  ID3D11BlendState *m_EventShaderPreviousBlend = NULL;
  bool m_EventShaderBlendApplied = false;
  FLOAT m_EventShaderPreviousBlendFactor[4] = {};
  UINT m_EventShaderPreviousSampleMask = ~0U;

  struct EventShaderPreview
  {
    ResourceId sourceViews[8] = {};
    ResourceId sourceResources[9] = {};
    ResourceId sourceDepthView;
    uint32_t outputTarget = ~0U;
    ID3D11Resource *colors[8] = {};
    ID3D11RenderTargetView *views[8] = {};
    ID3D11Resource *depth = NULL;
    ID3D11DepthStencilView *depthView = NULL;
    TextureDescription description;
    uint64_t bytes = 0, used = 0;
  };
  std::map<std::pair<uint32_t, ShaderStage>, EventShaderPreview> m_EventShaderPreviews;
  uint64_t m_EventShaderPreviewSerial = 0;
  ID3D11RenderTargetView *m_EventShaderPreviousTargets[8] = {};
  ID3D11DepthStencilView *m_EventShaderPreviousDepth = NULL;
  bool m_EventShaderTargetsApplied = false;
  UINT m_EventShaderPreviousTargetCount = 0;
  rdcstr PrepareEventShaderPreview(uint32_t eventId, uint32_t outputTarget,
                                  EventShaderPreview *&preview);
  void ReleaseEventShaderPreviews();


  struct EventCBufferOverride
  {
    ResourceId expectedShader;
    ResourceId expectedBuffer;
    uint64_t expectedByteOffset = 0;
    uint64_t expectedByteSize = 0;
    bytebuf data;
    ID3D11Buffer *shadow = NULL;
  };

  struct AppliedEventCBufferOverride
  {
    ShaderStage stage = ShaderStage::Vertex;
    uint32_t slot = 0;
    ID3D11Buffer *original = NULL;
    UINT firstConstant = 0;
    UINT numConstants = 0;
  };

  rdcstr ValidateEventCBufferBinding(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                     uint32_t registerSpace,
                                     ResourceId expectedShader, ResourceId expectedBuffer,
                                     uint64_t expectedByteOffset, uint64_t expectedByteSize);

  std::map<EventCBufferOverrideKey, EventCBufferOverride> m_EventCBufferOverrides;
  rdcarray<AppliedEventCBufferOverride> m_AppliedEventCBufferOverrides;
  struct LiveForwardCursor
  {
    bool valid = false;
    uint32_t eventId = 0, pendingTarget = 0;
    uint64_t deviceSerial = 0, pendingDeviceSerial = 0;
  } m_LiveForward;
  bool m_LiveForwardCaptureScanned = false, m_LiveForwardCaptureSafe = false;
  bool IsLiveForwardContextSafe();

  // used to track the real state so we can preserve it even across work done to the output windows
  struct RealState
  {
    RealState() : state(D3D11RenderState::Empty) { active = false; }
    bool active;
    D3D11RenderState state;
  } m_RealState;

  // event -> data
  std::map<uint32_t, D3D11PostVSData> m_PostVSData;

  HighlightCache m_HighlightCache;

  uint64_t m_SOBufferSize = 32 * 1024 * 1024;
  ID3D11Buffer *m_SOBuffer = NULL;
  ID3D11Buffer *m_SOStagingBuffer = NULL;
  rdcarray<ID3D11Query *> m_SOStatsQueries;

  ID3D11Texture2D *m_CustomShaderTex = NULL;
  ResourceId m_CustomShaderResourceId;

  // General use/misc items that are used in many places
  struct GeneralMisc
  {
    void Init(WrappedID3D11Device *device);
    void Release();

    ID3D11RasterizerState *RasterState = NULL;
    ID3D11RasterizerState *RasterClipState = NULL;
    ID3D11RasterizerState *RasterScissorState = NULL;

    ID3D11VertexShader *FullscreenVS = NULL;
    ID3D11PixelShader *FixedColPS = NULL;
    ID3D11PixelShader *CheckerboardPS = NULL;
  } m_General;

  struct TextureRendering
  {
    void Init(WrappedID3D11Device *device);
    void Release();

    ID3D11SamplerState *PointSampState = NULL;
    ID3D11SamplerState *LinearSampState = NULL;
    ID3D11BlendState *BlendState = NULL;
    ID3D11VertexShader *TexDisplayVS = NULL;
    ID3D11PixelShader *TexDisplayPS = NULL;
    ID3D11PixelShader *TexRemapPS[3] = {};
  } m_TexRender;

  struct OverlayRendering
  {
    void Init(WrappedID3D11Device *device);
    void Release();

    ID3D11VertexShader *FullscreenVS = NULL;
    ID3D11PixelShader *QuadOverdrawPS = NULL;
    ID3D11PixelShader *QOResolvePS = NULL;
    ID3D11PixelShader *TriangleSizePS = NULL;
    ID3D11PixelShader *DepthCopyPS = NULL;
    ID3D11PixelShader *DepthCopyArrayPS = NULL;
    ID3D11PixelShader *DepthCopyMSPS = NULL;
    ID3D11PixelShader *DepthCopyMSArrayPS = NULL;
    ID3D11GeometryShader *TriangleSizeGS = NULL;
    ID3D11BlendState *DepthBlendRTMaskZero = NULL;
    ID3D11DepthStencilState *DepthResolveDS = NULL;
    ID3D11Texture2D *Texture = NULL;
    ResourceId resourceId;
  } m_Overlay;

  struct MeshRendering
  {
    void Init(WrappedID3D11Device *device);
    void Release();

    ID3D11BlendState *WireframeHelpersBS = NULL;
    ID3D11RasterizerState *WireframeRasterState = NULL;
    ID3D11RasterizerState *SolidRasterState = NULL;
    ID3D11DepthStencilState *LessEqualDepthState = NULL;
    ID3D11DepthStencilState *NoDepthState = NULL;
    ID3D11VertexShader *MeshVS = NULL;
    ID3D11GeometryShader *MeshGS = NULL;
    ID3D11PixelShader *MeshPS = NULL;
    ID3D11Buffer *AxisHelper = NULL;
    ID3D11Buffer *FrustumHelper = NULL;
    ID3D11Buffer *TriHighlightHelper = NULL;
    ID3D11Buffer *ComponentHighlight = NULL;
    ID3D11InputLayout *GenericLayout = NULL;

    byte *MeshVSBytecode = NULL;
    uint32_t MeshVSBytelen = 0;

    // these gets updated to pull the elements selected out of the buffers
    ID3D11InputLayout *MeshLayout = NULL;

    // whenever these change
    ResourceFormat PrevPositionFormat;
    ResourceFormat PrevSecondaryFormat;
  } m_MeshRender;

  struct VertexPicking
  {
    void Init(WrappedID3D11Device *device);
    void Release();

    static const uint32_t MaxMeshPicks = 500;

    ID3D11ComputeShader *MeshPickCS = NULL;
    uint32_t PickIBSize = 0, PickVBSize = 0;
    ID3D11Buffer *PickIBBuf = NULL;
    ID3D11Buffer *PickVBBuf = NULL;
    ID3D11ShaderResourceView *PickIBSRV = NULL;
    ID3D11ShaderResourceView *PickVBSRV = NULL;
    ID3D11Buffer *PickResultBuf = NULL;
    ID3D11UnorderedAccessView *PickResultUAV = NULL;
  } m_VertexPick;

  struct PixelPicking
  {
    void Init(WrappedID3D11Device *device);
    void Release();

    ID3D11RenderTargetView *RTV = NULL;
    ID3D11Texture2D *Texture = NULL;
    ID3D11Texture2D *StageTexture = NULL;
  } m_PixelPick;

  ShaderDebugging m_ShaderDebug;

  struct HistogramMinMax
  {
    void Init(WrappedID3D11Device *device);
    void Release();

    ID3D11Buffer *TileResultBuff = NULL;
    ID3D11Buffer *ResultBuff = NULL;
    ID3D11Buffer *ResultStageBuff = NULL;
    ID3D11UnorderedAccessView *TileResultUAV[3] = {NULL};
    ID3D11UnorderedAccessView *ResultUAV[3] = {NULL};
    ID3D11ShaderResourceView *TileResultSRV[3] = {NULL};
    ID3D11ComputeShader *TileMinMaxCS[eTexType_Max][3] = {{NULL}};    // uint, sint, float
    ID3D11ComputeShader *HistogramCS[eTexType_Max][3] = {{NULL}};     // uint, sint, float
    ID3D11ComputeShader *ResultMinMaxCS[3] = {NULL};
    ID3D11UnorderedAccessView *HistogramUAV = NULL;
  } m_Histogram;

  struct PixelHistory
  {
    void Init(WrappedID3D11Device *device);
    void Release();

    ID3D11BlendState *NopBlendState = NULL;
    ID3D11DepthStencilState *NopDepthState = NULL;
    ID3D11DepthStencilState *AllPassDepthState = NULL;
    ID3D11DepthStencilState *AllPassIncrDepthState = NULL;
    ID3D11DepthStencilState *StencIncrEqDepthState = NULL;
    ID3D11PixelShader *PrimitiveIDPS = NULL;
  } m_PixelHistory;

  rdcarray<ResourceDescription> m_Resources;
  std::map<ResourceId, size_t> m_ResourceIdx;

  FrameRecord m_FrameRecord;

  D3D11Pipe::State *m_D3D11PipelineState = NULL;

  // save the clean replay-time render state OM to check against
  D3D11RenderState::OutputMerger m_RenderStateOM;
};
