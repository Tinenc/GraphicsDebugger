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
#include <set>
#include <unordered_map>
#include "api/replay/renderdoc_replay.h"
#include "core/core.h"
#include "replay/replay_driver.h"
#include "gl_common.h"

class AMDCounters;
class ARMCounters;
class IntelGlCounters;
class WrappedOpenGL;
struct GLCounterContext;
class NVGLCounters;
struct GLCheckpointBufferVersion;
struct GLCheckpointTextureVersion;
struct GLResource;
struct GLRenderState;

// Ordinary replay provenance, independent of texture-display caches and checkpoint allocations.
// Only the matching unmodified target may publish a prefix completed by WithoutDraw.
struct GLLiveForwardCursor
{
  uint32_t event = 0;
  uint32_t pendingTarget = 0;
  void Invalidate() { event = pendingTarget = 0; }
  bool CanAdvance(uint32_t target) const { return event != 0 && target > event; }
  void Arm(uint32_t target)
  {
    event = 0;
    pendingTarget = target;
  }
  bool Complete(uint32_t target, bool success)
  {
    const bool publish = success && target != 0 && pendingTarget == target;
    Invalidate();
    if(publish)
      event = target;
    return publish;
  }
};

struct GLPostVSData
{
  struct InstData
  {
    uint32_t numVerts = 0;
    uint32_t bufOffset = 0;
  };

  struct StageData
  {
    GLuint buf = 0;
    Topology topo = Topology::Unknown;

    uint32_t vertStride = 0;

    // simple case - uniform
    uint32_t numVerts = 0;
    uint32_t instStride = 0;

    // complex case - expansion per instance
    rdcarray<InstData> instData;

    bool useIndices = false;
    GLuint idxBuf = 0;
    uint32_t idxByteWidth = 0;

    bool flipY = false;

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

struct CompleteCacheKey
{
  GLuint tex;
  GLuint samp;

  bool operator<(const CompleteCacheKey &o) const
  {
    if(tex != o.tex)
      return tex < o.tex;
    return samp < o.samp;
  }
};

enum TexDisplayFlags
{
  eTexDisplay_None = 0x0,
  eTexDisplay_BlendAlpha = 0x1,
  eTexDisplay_MipShift = 0x2,
  eTexDisplay_RemapFloat = 0x4,
  eTexDisplay_RemapUInt = 0x8,
  eTexDisplay_RemapSInt = 0x10,
};

struct SamplingProgramConfig
{
  enum
  {
    TexBuffer,
    Tex1D,
    Tex2D,
    Tex3D,
    TexCube,
    Tex1DArray,
    Tex2DArray,
    Tex3DArray,
    TexCubeArray,
    Tex2DRect,
    Tex2DMS,
    Tex2DMSArray,
  } dim = Tex2D;

  enum
  {
    Fetch,
    QueryLod,
    Sample,
    SampleDref,
    Gather,
    GatherDref,
  } op = Fetch;

  enum
  {
    Float,
    UInt,
    SInt,
  } resType = Float;

  uint32_t gatherChannel = 0;
  bool useGatherOffs = false;
  bool useGrad = false;
  bool manualBias = false;
  rdcfixedarray<int32_t, 8> gatherOffsets = {};
  Vec3i fetchOffset;

  uint32_t hashKey() const
  {
    uint32_t hash = 5381;
    hash = ((hash << 5) + hash) + (uint32_t)dim;
    hash = ((hash << 5) + hash) + (uint32_t)op;
    hash = ((hash << 5) + hash) + (uint32_t)resType;
    hash = ((hash << 5) + hash) + gatherChannel;
    hash = ((hash << 5) + hash) + useGatherOffs;
    hash = ((hash << 5) + hash) + useGrad;
    hash = ((hash << 5) + hash) + manualBias;
    hash = ((hash << 5) + hash) + fetchOffset.x;
    hash = ((hash << 5) + hash) + fetchOffset.y;
    hash = ((hash << 5) + hash) + fetchOffset.z;
    for(size_t i = 0; i < gatherOffsets.size(); i++)
      hash = ((hash << 5) + hash) + gatherOffsets[i];
    return hash;
  }
};

class GLReplay : public IReplayDriver
{
public:
  GLReplay(WrappedOpenGL *d);
  virtual ~GLReplay();
  void SetProxy(bool p) { m_Proxy = p; }
  bool IsRemoteProxy() { return m_Proxy; }
  void Shutdown();

  RDResult FatalErrorCheck();
  IReplayDriver *MakeDummyDriver();

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

  rdcarray<ShaderEntryPoint> GetShaderEntryPoints(ResourceId shader);
  const ShaderReflection *GetShader(ResourceId pipeline, ResourceId shader, ShaderEntryPoint entry);

  rdcarray<rdcstr> GetDisassemblyTargets(bool withPipeline);
  rdcstr DisassembleShader(ResourceId pipeline, const ShaderReflection *refl, const rdcstr &target);

  rdcarray<DebugMessage> GetDebugMessages();

  rdcarray<EventUsage> GetUsage(ResourceId id);

  FrameRecord &WriteFrameRecord() { return m_FrameRecord; }
  FrameRecord GetFrameRecord() { return m_FrameRecord; }
  void SetPipelineStates(D3D11Pipe::State *d3d11, D3D12Pipe::State *d3d12, GLPipe::State *gl,
                         VKPipe::State *vk)
  {
    m_GLPipelineState = gl;
  }
  void SavePipelineState(uint32_t eventId);
  rdcarray<Descriptor> GetDescriptors(ResourceId descriptorStore,
                                      const rdcarray<DescriptorRange> &ranges);
  rdcarray<SamplerDescriptor> GetSamplerDescriptors(ResourceId descriptorStore,
                                                    const rdcarray<DescriptorRange> &ranges);
  rdcarray<DescriptorAccess> GetDescriptorAccess(uint32_t eventId);
  rdcarray<DescriptorLogicalLocation> GetDescriptorLocations(ResourceId descriptorStore,
                                                             const rdcarray<DescriptorRange> &ranges);
  void FreeTargetResource(ResourceId id);

  RDResult ReadLogInitialisation(RDCFile *rdc, bool storeStructuredBuffers);
  void ReplayLog(uint32_t endEventID, ReplayLogType replayType);
  void ConfigureReplayCheckpoints(const ReplayCheckpointConfig &config,
                                  const rdcarray<ReplayCheckpointCandidate> &candidates) override;
  void InvalidateReplayCheckpoints(uint32_t firstAffectedEvent = 0) override;
  ReplayCheckpointStats GetReplayCheckpointStats() override;
  SDFile *GetStructuredFile();

  rdcarray<uint32_t> GetPassEvents(uint32_t eventId);

  rdcarray<WindowingSystem> GetSupportedWindowSystems();

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

  void GetBufferData(ResourceId buff, uint64_t offset, uint64_t len, bytebuf &ret);
  void GetTextureData(ResourceId tex, const Subresource &sub, const GetTextureDataParams &params,
                      bytebuf &data);

  void ReplaceResource(ResourceId from, ResourceId to);
  void RemoveReplacement(ResourceId id);
  rdcstr ReplaceTargetShaderChecked(ResourceId from, ResourceId to);
  rdcstr RemoveTargetShaderReplacementChecked(ResourceId id);
  rdcstr SetEventShaderOverride(uint32_t eventId, ShaderStage stage, ResourceId expectedOriginal,
                                ResourceId replacement, uint32_t outputTarget = ~0U,
                                bool independentFloatOutput = false) override;
  TextureDescription GetEventShaderPreviewTexture(uint32_t eventId, ShaderStage stage) override;
  void ClearEventShaderOverride(uint32_t eventId, ShaderStage stage) override;
  void ClearEventShaderOverrides() override;
  void ClearEventShaderOverridesForResource(ResourceId id) override;
  rdcstr ApplyEventShaderOverrides(uint32_t eventId) override;
  void RestoreEventShaderOverrides() override;
  void ClearReplayCache();
  void ReloadShaderDebugInformation();

  rdcarray<GPUCounter> EnumerateCounters();
  CounterDescription DescribeCounter(GPUCounter counterID);
  rdcarray<CounterResult> FetchCounters(const rdcarray<GPUCounter> &counters);

  void RenderMesh(uint32_t eventId, const rdcarray<MeshFormat> &secondaryDraws,
                  const MeshDisplay &cfg, const MeshComponentHighlight &components);

  rdcarray<ShaderEncoding> GetCustomShaderEncodings() { return {ShaderEncoding::GLSL}; }
  rdcarray<ShaderSourcePrefix> GetCustomShaderSourcePrefixes()
  {
    // this is a complete hack, since we *do* want to define a prefix for GLSL. However GLSL sucks
    // and has the #version as the first thing, so we can't do a simple prepend of some defines.
    // Instead we will return no prefix and insert our own in BuildCustomShader if we see GLSL
    // coming in.
    return {};
  }
  rdcarray<ShaderEncoding> GetTargetShaderEncodings()
  {
    rdcarray<ShaderEncoding> encodings = {ShaderEncoding::GLSL};
    if(HasExt[ARB_gl_spirv])
      encodings.push_back(ShaderEncoding::OpenGLSPIRV);
    return encodings;
  }
  void BuildTargetShader(ShaderEncoding sourceEncoding, const bytebuf &source, const rdcstr &entry,
                         const ShaderCompileFlags &compileFlags, ShaderStage type, ResourceId &id,
                         rdcstr &errors);
  void SetCustomShaderIncludes(const rdcarray<rdcstr> &directories);
  void BuildCustomShader(ShaderEncoding sourceEncoding, const bytebuf &source, const rdcstr &entry,
                         const ShaderCompileFlags &compileFlags, ShaderStage type, ResourceId &id,
                         rdcstr &errors);
  void FreeCustomShader(ResourceId id);

  bool RenderTexture(TextureDisplay cfg);
  bool RenderTextureInternal(TextureDisplay cfg, TexDisplayFlags flags);

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

  void BindFramebufferTexture(RenderOutputSubresource &sub, GLenum texBindingEnum, GLint numSamples);

  ResourceId ApplyCustomShader(TextureDisplay &display);

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
  rdcstr SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches) override;
  uint32_t GetReplayBufferPatchFirstAffectedEvent() const override
  {
    return m_ReplayPatchFirstAffectedEvent;
  }

  RenderOutputSubresource GetRenderOutputSubresource(ResourceId id);
  bool IsRenderOutput(ResourceId id) { return GetRenderOutputSubresource(id).mip != ~0U; }
  void FileChanged() {}
  void SetReplayData(GLWindowingData data);

  void UseReplayContext() { MakeCurrentReplayContext(&m_ReplayCtx); }
  bool IsReplayContext(void *ctx) { return m_ReplayCtx.ctx == NULL || ctx == m_ReplayCtx.ctx; }
  bool HasDebugContext() { return m_DebugCtx != NULL; }
  void FillWithDiscardPattern(DiscardType type, GLuint framebuffer, GLsizei numAttachments,
                              const GLenum *attachments, GLint x, GLint y, GLsizei width,
                              GLsizei height);
  void FillWithDiscardPattern(DiscardType type, ResourceId id, GLuint mip, GLint xoffset = 0,
                              GLint yoffset = 0, GLint zoffset = 0, GLsizei width = 65536,
                              GLsizei height = 65536, GLsizei depth = 65536);

  bool CreateShaderReplacementProgram(GLuint srcProgram, GLuint srcPipeline, GLuint replacedProgram,
                                      ShaderStage stage, GLuint newShader, GLuint newShaderSPIRV);

  GLuint GetShaderDebugMathProg();
  GLuint MakeShaderDebugSampleProg(const SamplingProgramConfig &config);

private:
  friend class WrappedOpenGL;
  void InvalidateLiveForward()
  {
    m_LiveForward.Invalidate();
    ResetCheckpointTextureLineage();
  }
  bool PrepareLiveForward();
  bool LiveForwardEndpoint(uint32_t eventId) const;
  GLLiveForwardCursor m_LiveForward;
  bool m_LiveForwardScanned = false;
  bool m_LiveForwardCaptureSafe = false;
  rdcstr m_LiveForwardReason;
  void TrackCheckpointBufferWrites(GLChunk chunk);
  void ResetCheckpointBufferLineage();
  void InvalidateCheckpointBufferVersion(const GLResource &buffer);
  void ResetCheckpointTextureLineage();
  void ResetCheckpointTextureTracking();
  void TrackCheckpointTextureWrites(GLChunk chunk);
  void RecordCheckpointTextureWrite(const GLResource &resource);
  bool QueryCheckpointFramebufferWrites(GLuint framebuffer, rdcarray<ResourceId> &writes);
  bool PrepareCheckpointTextureSharing();
  uint32_t m_ReplayPatchFirstAffectedEvent = 0;
  uint32_t m_ReplayPatchAnchorEvent = 0;
  struct ReplayCheckpoint;
  bool PrepareReplayCheckpoints();
  void PrepareCheckpointStaticResources(bool completeBufferEvidence, bool completeTextureEvidence);
  bool SaveReplayCheckpoint(uint32_t eventId);
  bool RestoreReplayCheckpoint(ReplayCheckpoint &checkpoint);
  void ApplyCheckpointBindings(ReplayCheckpoint &checkpoint);
  void UpdateCheckpointAccounting();
  void EvictOldestCheckpoint();
  uint64_t CheckpointStorageBytes(const ReplayCheckpoint *building, uint64_t &sharedBytes) const;
  ReplayCheckpointConfig m_CheckpointConfig;
  ReplayCheckpointStats m_CheckpointStats;
  bool m_ReplayPatchReadOnlyEvidence = false;
  uint64_t m_ReplayPatchImmutableInitialBytes = 0;
  uint32_t m_ReplayPatchImmutableInitialCount = 0;
  rdcarray<ReplayCheckpointCandidate> m_CheckpointCandidates;
  std::map<uint32_t, std::unique_ptr<ReplayCheckpoint>> m_Checkpoints;
  // Weak references never keep an evicted GPU allocation outside the checkpoint byte budget.
  std::map<ResourceId, std::weak_ptr<GLCheckpointBufferVersion>> m_CheckpointBufferVersions;
  std::map<ResourceId, std::weak_ptr<GLCheckpointTextureVersion>> m_CheckpointTextureVersions;
  struct TextureWriteRecord
  {
    uint32_t chunk = 0;
    bool complete = false;
    rdcarray<ResourceId> writes;
  };
  std::map<uint64_t, TextureWriteRecord> m_CheckpointTextureWrites;
  size_t m_CheckpointTextureWriteResources = 0;
  bool m_CheckpointTextureWriteOverflow = false;
  bool m_CheckpointTextureBindingEdited = false;
  bool m_CheckpointTextureSharingScanned = false;
  bool m_CheckpointTextureSharingSafe = false;
  bool m_CheckpointTextureReplayManaged = false;
  bool m_CheckpointTextureAnalysis = false;
  uint32_t m_CheckpointTexturePendingTarget = 0;
  struct TextureAnalysisScope
  {
    GLReplay &replay;
    bool previous;
    explicit TextureAnalysisScope(GLReplay &driver)
        : replay(driver), previous(driver.m_CheckpointTextureAnalysis)
    {
      replay.m_CheckpointTextureAnalysis = true;
      replay.ResetCheckpointTextureLineage();
    }
    ~TextureAnalysisScope()
    {
      replay.m_CheckpointTextureAnalysis = previous;
      replay.ResetCheckpointTextureLineage();
    }
  };
  // Original live contents proven unchanged for the complete captured frame. Object/sampler
  // state still belongs to each checkpoint. Unknown calls and aliases invalidate this proof.
  std::set<ResourceId> m_CheckpointStaticResources;
  uint64_t m_CheckpointUseSerial = 0;
  bool m_CheckpointPrepared = false;
  bool m_CheckpointPreparingFailed = false;

  void OpenGLFillCBufferVariables(ResourceId shader, GLuint prog, bool bufferBacked, rdcstr prefix,
                                  const rdcarray<ShaderConstant> &variables,
                                  rdcarray<ShaderVariable> &outvars, const bytebuf &data);

  bool GetMinMax(ResourceId texid, const Subresource &sub, CompType typeCast, bool stencil,
                 float *minval, float *maxval);

  void CreateCustomShaderTex(uint32_t w, uint32_t h);

  struct OutputWindow : public GLWindowingData
  {
    OutputWindow(const GLWindowingData &data) : GLWindowingData(data) {}
    OutputWindow() {}
    struct
    {
      // used to blit from defined FBO (VAOs not shared)
      GLuint emptyVAO = 0;

      // textures for the below FBO. Resize with the window
      GLuint backbuffer = 0;
      GLuint depthstencil = 0;

      // this FBO is on the debug GL context, not the window's GL context
      // when rendering a texture or mesh etc, we render onto this FBO on
      // the debug GL context, then blit onto the default framebuffer
      // on the window's GL context.
      // This is so we don't have to re-create any non-shared resource we
      // need for debug rendering on the window's GL context.
      GLuint windowFBO = 0;

      // this FBO is the same as the above, but on the replay context,
      // for any cases where we need to use the replay context (like
      // re-rendering an action).
      GLuint replayFBO = 0;

      // read FBO for blit to window
      GLuint readFBO = 0;
    } BlitData;

    WindowingSystem system = WindowingSystem::Headless;
    int width = 1, height = 1;
  };

  enum class TextureSamplerMode
  {
    Point,
    PointNoMip,
    Linear,
  };

  struct TextureSamplerState
  {
    GLenum minFilter = eGL_NEAREST;
    GLenum magFilter = eGL_NEAREST;
    GLenum wrapS = eGL_CLAMP_TO_EDGE;
    GLenum wrapT = eGL_CLAMP_TO_EDGE;
    GLenum wrapR = eGL_CLAMP_TO_EDGE;
    GLenum compareMode = eGL_NONE;
    float lodBias = 0.0f;
  };

  // sets the desired parameters, and returns the previous ones ready to restore
  TextureSamplerState SetSamplerParams(GLenum target, GLuint texname, TextureSamplerMode mode);
  void RestoreSamplerParams(GLenum target, GLuint texname, TextureSamplerState state);

  // any objects that are shared between contexts, we just initialise
  // once
  struct DebugRenderData
  {
    float outWidth, outHeight;

    int glslVersion;

    rdcstr texSampleDefines;

    // min/max data
    GLuint minmaxTileResult;          // tile result buffer
    GLuint minmaxResult;              // Vec4f[2] final result buffer
    GLuint histogramBuf;              // uint32_t * num buckets buffer
    GLuint minmaxResultProgram[3];    // float/uint/sint tile result -> final result program
    GLuint minmaxTileProgram[64];     // RESTYPE indexed (see glsl_ubos.h, 1d/2d/3d etc |
                                      // uint/sint) src tex -> tile result buf program
    GLuint histogramProgram[64];      // RESTYPE indexed (see glsl_ubos.h, 1d/2d/3d etc |
                                      // uint/sint) src tex -> histogram result buf program

    GLuint texDisplayVertexShader;
    GLuint texDisplayProg[3];    // float/uint/sint
    GLuint texRemapProg[3];      // float/uint/sint

    GLuint customFBO;
    GLuint customTex;
    ResourceId CustomShaderTexID;

    static const int maxMeshPicks = 500;

    GLuint meshPickProgram;
    GLuint pickIBBuf, pickVBBuf;
    uint32_t pickIBSize, pickVBSize;
    GLuint pickResultBuf;

    GLuint checkerProg;

    GLuint fixedcolFragShader;
    GLuint fixedcolFragShaderSPIRV;

    // 0 = both floats, 1 = position doubles, 2 = secondary doubles, 3 = both doubles
    GLuint meshProg[4];
    GLuint meshgsProg[4];
    GLuint trisizeProg;

    GLuint meshVAO;
    GLuint axisVAO;
    GLuint frustumVAO;
    GLuint triHighlightVAO;

    GLuint axisFrustumBuffer;
    GLuint triHighlightBuffer;

    GLuint feedbackObj;
    rdcarray<GLuint> feedbackQueries;
    GLuint feedbackBuffer;
    uint64_t feedbackBufferSize = 32 * 1024 * 1024;

    GLuint pickPixelTex;
    GLuint pickPixelFBO;

    GLuint dummyTexBuffer;
    GLuint dummyTexBufferStore;

    GLuint quadoverdrawFragShader;
    GLuint quadoverdrawFragShaderSPIRV;
    GLuint quadoverdrawResolveProg;

    GLuint fullScreenFixedColProg;
    GLuint fullScreenCopyDepth;
    GLuint fullScreenCopyDepthMS;

    GLuint discardProg[3][4];
    GLuint discardPatternBuffer;

    GLuint shaderDebugMathProg;

    ResourceId overlayTexId;
    GLuint overlayTex;
    GLuint overlayFBO;
    GLuint overlayProg;
    GLint overlayTexWidth = 0, overlayTexHeight = 0, overlayTexSamples = 0, overlayTexMips = 0,
          overlayTexSlices = 0;

    GLuint UBOs[3];

    GLuint emptyVAO;
  } DebugData;

  bool m_Degraded;

  HighlightCache m_HighlightCache;

  std::unordered_map<uint32_t, GLuint> m_ShaderDebugSampleProg;

  std::map<GLenum, bytebuf> m_DiscardPatterns;

  // eventId -> data
  std::map<uint32_t, GLPostVSData> m_PostVSData;

  void ClearPostVSCache();

  // cache the previous data returned
  ResourceId m_GetTexturePrevID;
  byte *m_GetTexturePrevData[16];
  rdcarray<ResourceId> m_TextureSnapshots;

  GLuint CreateMeshProgram(GLuint vs, GLuint fs, GLuint gs = 0);
  void ConfigureTexDisplayProgramBindings(GLuint program);
  void BindUBO(GLuint program, const char *name, GLuint binding);

  void InitDebugData();
  void DeleteDebugData();

  void CheckGLSLVersion(const char *sl, int &glslVersion);

  void FillTimers(GLCounterContext &ctx, const ActionDescription &actionnode,
                  const rdcarray<GPUCounter> &counters);

  void InitOutputWindow(OutputWindow &outwin);
  void CreateOutputWindowBackbuffer(OutputWindow &outwin, bool depth);

  GLWindowingData m_ReplayCtx;
  uint64_t m_DebugID;
  OutputWindow *m_DebugCtx;

  void MakeCurrentReplayContext(GLWindowingData *ctx);
  void SwapBuffers(GLWindowingData *ctx);
  void CloseReplayContext();

  uint64_t m_OutputWindowID;
  std::map<uint64_t, OutputWindow> m_OutputWindows;

  std::map<std::pair<uint32_t, ShaderStage>, EventShaderOverrideBinding> m_EventShaderOverrides;
  std::map<EventShaderPipelineKey, GLuint> m_EventShaderPrograms;
  std::map<EventShaderPipelineKey, GLuint> m_EventShaderPipelines;
  std::unique_ptr<GLRenderState> m_EventShaderPreviousState;
  void ReleaseEventShaderPrograms(ResourceId target = ResourceId());
  struct EventShaderFloatAttachment
  {
    ResourceId source;
    GLenum attachment = eGL_NONE, sourceTarget = eGL_NONE, format = eGL_NONE;
    GLuint sourceName = 0, texture = 0;
    GLint mip = 0, layer = 0, layers = 1, width = 0, height = 0, samples = 1;
    bool layered = false, multiview = false, fixedSamples = true;
  };
  struct EventShaderFloatPreview
  {
    GLuint framebuffer = 0;
    rdcarray<EventShaderFloatAttachment> attachments;
    rdcarray<GLenum> drawBuffers;
    size_t selected = 0;
    rdcstr signature;
    uint64_t bytes = 0, used = 0;
  };
  std::map<std::pair<uint32_t, uint32_t>, EventShaderFloatPreview> m_EventShaderFloatPreviews;
  uint64_t m_EventShaderFloatClock = 0;
  void DestroyEventShaderFloatPreview(EventShaderFloatPreview &preview);
  void ReleaseEventShaderFloatPreviews();
  rdcstr PrepareEventShaderFloatPreview(uint32_t eventId, uint32_t outputTarget,
                                        GLRenderState &original, EventShaderFloatPreview *&preview);

  bool m_Proxy;

  void CacheTexture(ResourceId id);

  std::map<ResourceId, TextureDescription> m_CachedTextures;

  WrappedOpenGL *m_pDriver;

  rdcarray<ResourceDescription> m_Resources;
  std::map<ResourceId, size_t> m_ResourceIdx;

  GLPipe::State *m_GLPipelineState = NULL;

  rdcarray<DescriptorAccess> m_Access;

  FrameRecord m_FrameRecord;

  DriverInformation m_DriverInfo;

  std::map<CompleteCacheKey, rdcstr> m_CompleteCache;

  // AMD counter instance
  AMDCounters *m_pAMDCounters = NULL;

  void FillTimersAMD(uint32_t *eventStartID, uint32_t *sampleIndex, rdcarray<uint32_t> *eventIDs,
                     const ActionDescription &actionnode);

  rdcarray<CounterResult> FetchCountersAMD(const rdcarray<GPUCounter> &counters);

  // Intel counter instance
  IntelGlCounters *m_pIntelCounters = NULL;

  void FillTimersIntel(uint32_t *eventStartID, uint32_t *sampleIndex, rdcarray<uint32_t> *eventIDs,
                       const ActionDescription &actionnode);

  rdcarray<CounterResult> FetchCountersIntel(const rdcarray<GPUCounter> &counters);

  // ARM counter instance
  ARMCounters *m_pARMCounters = NULL;

  void FillTimersARM(uint32_t *eventStartID, uint32_t *sampleIndex, rdcarray<uint32_t> *eventIDs,
                     const ActionDescription &actionnode);

  rdcarray<CounterResult> FetchCountersARM(const rdcarray<GPUCounter> &counters);

#if DISABLED(RDOC_ANDROID) && DISABLED(RDOC_APPLE)
  // NVIDIA counter instance
  NVGLCounters *m_pNVCounters = NULL;
#endif
};
