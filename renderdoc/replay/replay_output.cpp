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

#include "common/common.h"
#include "common/formatting.h"
#include "common/timing.h"
#include "maths/formatpacking.h"
#include "maths/matrix.h"
#include "os/os_specific.h"
#include "strings/string_utils.h"
#include "replay_controller.h"
#include "replay_timing.h"

void ReplayHistoryDisplaySource::Clear()
{
  active = false;
}

void ReplayHistoryDisplaySource::Publish(uint32_t event, const TextureDisplay &source,
                                         uint64_t mutationGeneration,
                                         rdcpair<int32_t, int32_t> renderedDimensions,
                                         bool succeeded)
{
  active = succeeded;
  if(!active)
    return;
  eventId = event;
  display = source;
  generation = mutationGeneration;
  dimensions = renderedDimensions;
}

bool ReplayHistoryDisplaySource::NeedsReplay(bool dirty,
                                              rdcpair<int32_t, int32_t> currentDimensions,
                                              uint64_t mutationGeneration) const
{
  return active && (dirty || !(dimensions == currentDimensions) ||
                    generation != mutationGeneration);
}

static uint64_t GetHandle(WindowingData window)
{
#if ENABLED(RDOC_LINUX)

  if(window.system == WindowingSystem::Xlib)
  {
#if ENABLED(RDOC_XLIB)
    return (uint64_t)window.xlib.window;
#else
    RDCERR("Xlib windowing system data passed in, but support is not compiled in");
#endif
  }

  if(window.system == WindowingSystem::XCB)
  {
#if ENABLED(RDOC_XCB)
    return (uint64_t)window.xcb.window;
#else
    RDCERR("XCB windowing system data passed in, but support is not compiled in");
#endif
  }

  if(window.system == WindowingSystem::Wayland)
  {
#if ENABLED(RDOC_WAYLAND)
    return (uint64_t)window.wayland.window;
#else
    RDCERR("Wayland windowing system data passed in, but support is not compiled in");
#endif
  }

  RDCERR("Unrecognised window system %s", ToStr(window.system).c_str());

  return 0;

#elif ENABLED(RDOC_WIN32)

  RDCASSERT(window.system == WindowingSystem::Win32);
  return (uint64_t)window.win32.window;    // HWND

#elif ENABLED(RDOC_ANDROID)

  RDCASSERT(window.system == WindowingSystem::Android);
  return (uint64_t)window.android.window;    // ANativeWindow *

#elif ENABLED(RDOC_APPLE)

  RDCASSERT(window.system == WindowingSystem::MacOS);
  return (uint64_t)window.macOS.view;    // NSView *

#else
  RDCFATAL("No windowing data defined for this platform! Must be implemented for replay outputs");
#endif
}

ReplayOutput::ReplayOutput(ReplayController *parent, WindowingData window, ReplayOutputType type)
{
  m_ThreadID = Threading::GetCurrentID();

  m_pController = parent;

  m_MainOutput.dirty = true;

  m_CustomDirty = false;
  m_OverlayDirty = false;
  m_ForceOverlayRefresh = false;

  m_pDevice = parent->GetDevice();

  m_EventID = parent->m_EventID;

  m_OverlayResourceId = ResourceId();

  RDCEraseEl(m_RenderData);

  m_PixelContext.outputID = 0;
  m_PixelContext.texture = ResourceId();
  m_PixelContext.depthMode = false;

  m_ContextX = -1.0f;
  m_ContextY = -1.0f;

  m_Type = type;
  m_HeadlessWindow = window.system == WindowingSystem::Headless;

  if(window.system != WindowingSystem::Unknown)
    m_MainOutput.outputID = m_pDevice->MakeOutputWindow(window, type == ReplayOutputType::Mesh);
  else
    m_MainOutput.outputID = 0;
  m_MainOutput.texture = ResourceId();

  m_pController->FatalErrorCheck();
  m_pDevice = parent->GetDevice();

  m_pDevice->GetOutputWindowDimensions(m_MainOutput.outputID, m_Width, m_Height);

  m_CustomShaderResourceId = ResourceId();

  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(ReplayController));
}

ReplayOutput::~ReplayOutput()
{
  CHECK_REPLAY_THREAD();

  m_CustomShaderResourceId = ResourceId();

  m_pDevice->DestroyOutputWindow(m_MainOutput.outputID);
  m_pDevice->DestroyOutputWindow(m_PixelContext.outputID);

  ClearThumbnails();
}

void ReplayOutput::Shutdown()
{
  CHECK_REPLAY_THREAD();

  m_pController->ShutdownOutput(this);
}

bytebuf ReplayOutput::ReadbackOutputTexture()
{
  CHECK_REPLAY_THREAD();

  bytebuf data;
  m_pDevice->GetOutputWindowData(m_MainOutput.outputID, data);
  m_pController->FatalErrorCheck();
  return data;
}

rdcpair<int32_t, int32_t> ReplayOutput::GetDimensions()
{
  CHECK_REPLAY_THREAD();

  return make_rdcpair(m_Width, m_Height);
}

void ReplayOutput::SetTextureDisplay(const TextureDisplay &o)
{
  CHECK_REPLAY_THREAD();

  if(!m_pController->EnsureMaterializedCurrentEvent(__FUNCTION__))
    return;

  m_HistorySnapshotActive = false;
  m_HistoryReplaySource.Clear();
  SetTextureDisplayInternal(o, false);
}

void ReplayOutput::SetTextureDisplayInternal(const TextureDisplay &o, bool reapRetiredSnapshots)
{
  CHECK_REPLAY_THREAD();

  const bool resetTransientEventDisplay = m_TransientTextureEventDisplay;
  m_TransientTextureEventDisplay = false;

  bool wasClearBeforeDraw = (m_RenderData.texDisplay.overlay == DebugOverlay::ClearBeforeDraw ||
                             m_RenderData.texDisplay.overlay == DebugOverlay::ClearBeforePass);

  if(o.overlay != m_RenderData.texDisplay.overlay ||
     (o.overlay != DebugOverlay::NoOverlay && (o.subresource != m_RenderData.texDisplay.subresource ||
                                               o.typeCast != m_RenderData.texDisplay.typeCast ||
                                               o.resourceId != m_RenderData.texDisplay.resourceId)))
  {
    if(wasClearBeforeDraw)
    {
      // by necessity these overlays modify the actual texture, not an
      // independent overlay texture. So if we disable them, we must
      // refresh the log.
      m_ForceOverlayRefresh = true;
    }
    m_OverlayDirty = true;
  }
  if(wasClearBeforeDraw && o.backgroundColor != m_RenderData.texDisplay.backgroundColor)
    m_OverlayDirty = true;
  if(resetTransientEventDisplay && o.overlay != DebugOverlay::NoOverlay)
    m_OverlayDirty = true;
  m_CustomDirty = true;
  m_RenderData.texDisplay = o;
  m_MainOutput.dirty = true;

  m_TextureDim = {0, 0};
  for(size_t t = 0; t < m_pController->m_Textures.size(); t++)
  {
    if(m_pController->m_Textures[t].resourceId == m_RenderData.texDisplay.resourceId)
    {
      m_TextureDim = {m_pController->m_Textures[t].width, m_pController->m_Textures[t].height};
    }
  }

  // Replacing the persistent display releases this output's pin on its previous event snapshot.
  // Internal history rendering can temporarily restore the previous display, so that path reaps
  // only after it has committed its final persistent binding.
  if(reapRetiredSnapshots)
    m_pController->ReapRetiredEventTextureSnapshots();
}

void ReplayOutput::SetMeshDisplay(const MeshDisplay &o)
{
  CHECK_REPLAY_THREAD();

  if(!m_pController->EnsureMaterializedCurrentEvent(__FUNCTION__))
    return;

  if(o.showWholePass != m_RenderData.meshDisplay.showWholePass)
    m_OverlayDirty = true;
  m_RenderData.meshDisplay = o;
  m_MainOutput.dirty = true;
}

extern "C" RENDERDOC_API void RENDERDOC_CC RENDERDOC_SetMeshComponentHighlight(
    IReplayOutput *output, const FloatVector *vertices, uint32_t count, Topology topology)
{
  RENDERDOC_SetMeshComponentOverlay(output, vertices, count, topology, count);
}

extern "C" RENDERDOC_API void RENDERDOC_CC
RENDERDOC_SetMeshComponentOverlay(IReplayOutput *output, const FloatVector *vertices,
                                  uint32_t count, Topology topology, uint32_t selectedVertices)
{
  if(output)
    static_cast<ReplayOutput *>(output)->SetMeshComponentOverlay(vertices, count, topology,
                                                                 selectedVertices);
}

void ReplayOutput::SetMeshComponentOverlay(const FloatVector *vertices, uint32_t count,
                                           Topology topology, uint32_t selectedVertices)
{
  CHECK_REPLAY_THREAD();

  const uint32_t stride = topology == Topology::PointList      ? 6U
                          : topology == Topology::LineList     ? 2U
                          : topology == Topology::TriangleList ? 3U
                                                               : 0U;
  if(vertices && stride && count >= stride)
    m_ComponentHighlight.vertices.assign(vertices, count - count % stride);
  else
    m_ComponentHighlight.vertices.clear();
  m_ComponentHighlight.topology = topology;
  m_ComponentHighlight.selectedVertices =
      uint32_t(RDCMIN(size_t(selectedVertices), m_ComponentHighlight.vertices.size()));
  m_MainOutput.dirty = true;
}

void ReplayOutput::SetFrameEvent(int eventId)
{
  CHECK_REPLAY_THREAD();

  SetScrubFrameEvent(eventId);
  static const bool eagerMesh =
      Process::GetEnvVariable("RENDERDOC_REPLAY_EAGER_MESH_OUTPUTS") == "1";
  // An empty or hidden mesh preview has no consumer for post-VS extraction. Preserve its dirty
  // event state: DisplayMesh prepares the latest event when a visible, bound preview needs it.
  if(!eagerMesh && m_Type == ReplayOutputType::Mesh &&
     (m_RenderData.meshDisplay.position.vertexResourceId == ResourceId() ||
      (m_MainOutput.outputID != 0 && !m_pDevice->IsOutputWindowVisible(m_MainOutput.outputID))))
    return;
  RefreshOverlay();
}

void ReplayOutput::SetScrubFrameEvent(int eventId)
{
  CHECK_REPLAY_THREAD();

  m_TransientTextureEventDisplay = false;
  m_EventID = eventId;

  // Mesh output must rebuild event-specific post-VS data after the viewer supplies its new
  // bindings. NaN/clipping are evaluated by the texture display shader and need no replay.
  const DebugOverlay overlay = m_RenderData.texDisplay.overlay;
  m_OverlayDirty = m_Type == ReplayOutputType::Mesh ||
                   (overlay != DebugOverlay::NoOverlay && overlay != DebugOverlay::NaN &&
                    overlay != DebugOverlay::Clipping);

  m_CustomDirty = true;
  m_MainOutput.dirty = true;

  for(size_t i = 0; i < m_Thumbnails.size(); i++)
    m_Thumbnails[i].dirty = true;
}

void ReplayOutput::RefreshOverlay()
{
  CHECK_REPLAY_THREAD();

  ActionDescription *action = m_pController->GetActionByEID(m_EventID);

  // RenderOverlay and InitPostVSBuffers are driver operations that may perform their own replay in
  // addition to the explicit ReplayLog calls below. Do not let a later scrub commit assume the
  // exact state from ScrubFrameEvent survived any overlay preparation.
  if((m_Type == ReplayOutputType::Texture &&
      m_RenderData.texDisplay.overlay != DebugOverlay::NoOverlay) ||
     (m_Type == ReplayOutputType::Mesh && m_OverlayDirty))
    m_pController->InvalidateScrubReplay();

  passEvents = m_pDevice->GetPassEvents(m_EventID);

  bool postVSBuffers = false;
  bool postVSWholePass = false;

  if(m_Type == ReplayOutputType::Mesh && m_OverlayDirty)
  {
    postVSBuffers = true;
    postVSWholePass = m_RenderData.meshDisplay.showWholePass != 0;
  }

  if(m_Type == ReplayOutputType::Texture)
  {
    postVSBuffers = m_RenderData.texDisplay.overlay == DebugOverlay::TriangleSizePass ||
                    m_RenderData.texDisplay.overlay == DebugOverlay::TriangleSizeDraw;
    postVSWholePass = m_RenderData.texDisplay.overlay == DebugOverlay::TriangleSizePass;
  }

  if(postVSBuffers)
  {
    if(m_Type == ReplayOutputType::Mesh)
      m_OverlayDirty = false;

    if(action != NULL && (action->flags & (ActionFlags::MeshDispatch | ActionFlags::Drawcall)))
    {
      m_pDevice->InitPostVSBuffers(action->eventId);
      m_pController->FatalErrorCheck();

      if(postVSWholePass && !passEvents.empty())
      {
        m_pDevice->InitPostVSBuffers(passEvents);
        m_pController->FatalErrorCheck();

        ReplayCapture(m_EventID, eReplay_WithoutDraw);
        m_pController->FatalErrorCheck();
      }
    }
  }

  if(m_Type == ReplayOutputType::Texture && m_RenderData.texDisplay.overlay != DebugOverlay::NoOverlay)
  {
    ResourceId id = m_RenderData.texDisplay.resourceId;

    if(id != ResourceId() && action && m_pDevice->IsRenderOutput(id))
    {
      FloatVector f = m_RenderData.texDisplay.backgroundColor;

      // when user selects a MultiAction parent, the effective EID maps to the last child.
      // detect this by comparing selectedEventID with m_EventID.
      m_pDevice->m_OverlayMultiActionEvents.clear();
      uint32_t selectedEID = m_pController->GetSelectedEventID();
      if(selectedEID != m_EventID && action->parent &&
         (action->parent->flags & ActionFlags::MultiAction))
      {
        for(const ActionDescription &child : action->parent->children)
        {
          if(child.flags & ActionFlags::Drawcall)
            m_pDevice->m_OverlayMultiActionEvents.push_back(child.eventId);
        }
      }

      m_OverlayResourceId =
          m_pDevice->RenderOverlay(id, f, m_RenderData.texDisplay.overlay, m_EventID, passEvents);
      m_pController->FatalErrorCheck();
      m_OverlayDirty = false;

      m_pDevice->m_OverlayMultiActionEvents.clear();
    }
    else
    {
      m_OverlayResourceId = ResourceId();
    }
  }
  else
  {
    m_OverlayDirty = false;
  }
}

ResourceId ReplayOutput::GetCustomShaderTexID()
{
  CHECK_REPLAY_THREAD();

  if(m_CustomDirty)
  {
    TextureDisplay texDisplay = m_RenderData.texDisplay;
    texDisplay.rawOutput = false;

    m_CustomShaderResourceId = m_pDevice->ApplyCustomShader(texDisplay);
    m_pController->FatalErrorCheck();

    m_CustomDirty = false;
  }

  return m_CustomShaderResourceId;
}

ResourceId ReplayOutput::GetDebugOverlayTexID()
{
  CHECK_REPLAY_THREAD();

  if(!m_pController->EnsureMaterializedCurrentEvent(__FUNCTION__))
    return ResourceId();

  if(m_OverlayDirty)
  {
    ReplayCapture(m_EventID, eReplay_WithoutDraw);
    m_pController->FatalErrorCheck();
    m_pController->ReapplyAnalysisEventOverrides(m_EventID);
    RefreshOverlay();
    m_pController->ReapplyAnalysisEventOverrides(m_EventID);
    ReplayCapture(m_EventID, eReplay_OnlyDraw);
    m_pController->FatalErrorCheck();
  }

  return m_OverlayResourceId;
}

void ReplayOutput::ClearThumbnails()
{
  CHECK_REPLAY_THREAD();

  for(size_t i = 0; i < m_Thumbnails.size(); i++)
    m_pDevice->DestroyOutputWindow(m_Thumbnails[i].outputID);

  for(auto it = m_ThumbnailGenerators.begin(); it != m_ThumbnailGenerators.end(); ++it)
    m_pDevice->DestroyOutputWindow(it->second);

  m_Thumbnails.clear();
}

ResultDetails ReplayOutput::SetPixelContext(WindowingData window)
{
  CHECK_REPLAY_THREAD();

  m_PixelContext.outputID = m_pDevice->MakeOutputWindow(window, false);
  m_PixelContext.texture = ResourceId();
  m_PixelContext.depthMode = false;

  m_pController->FatalErrorCheck();

  if(m_PixelContext.outputID == 0)
  {
    RETURN_ERROR_RESULT(ResultCode::InternalError, "Window creation failed");
  }

  return RDResult();
}

bytebuf ReplayOutput::DrawThumbnail(int32_t width, int32_t height, ResourceId textureId,
                                    const Subresource &sub, CompType typeCast)
{
  bytebuf ret;

  ResourceId displayTextureId = textureId;
  Subresource displaySubresource = sub;
  CompType displayTypeCast = typeCast;

  if(!m_pController->EnsureMaterializedCurrentEvent(__FUNCTION__))
    return ret;

  width = RDCMAX(width, 1);
  height = RDCMAX(height, 1);

  uint64_t key = (uint64_t(width) << 32) | height;
  int idx = -1;
  uint64_t outputID = 0;
  for(int i = 0; i < m_ThumbnailGenerators.count(); i++)
  {
    if(m_ThumbnailGenerators[i].first == key)
    {
      idx = i;
      outputID = m_ThumbnailGenerators[i].second;
      break;
    }
  }

  if(idx < 0)
  {
    // release oldest generator if we have hit the max
    if(m_ThumbnailGenerators.size() == MaxThumbnailGenerators)
    {
      m_pDevice->DestroyOutputWindow(m_ThumbnailGenerators.back().second);
      m_ThumbnailGenerators.pop_back();
    }

    outputID = m_pDevice->MakeOutputWindow(CreateHeadlessWindowingData(width, height), false);
  }
  else
  {
    // remove the found one, so it can get inserted into the front
    m_ThumbnailGenerators.erase(idx);
  }
  // make this the most recent generator, so the oldest one will be kicked out if we have too many
  m_ThumbnailGenerators.insert(0, {key, outputID});

  bool depthMode = false;

  for(size_t t = 0; t < m_pController->m_Textures.size(); t++)
  {
    if(m_pController->m_Textures[t].resourceId == textureId)
    {
      depthMode = (m_pController->m_Textures[t].creationFlags & TextureCategory::DepthTarget) ||
                  (m_pController->m_Textures[t].format.compType == CompType::Depth);
      break;
    }
  }

  if(textureId == ResourceId())
  {
    m_pDevice->BindOutputWindow(outputID, false);

    FloatVector dark = RenderDoc::Inst().DarkCheckerboardColor();
    FloatVector light = RenderDoc::Inst().LightCheckerboardColor();

    FloatVector dark2;
    dark2.x = light.x;
    dark2.y = dark.y;
    dark2.z = dark.z;
    dark2.w = 1.0f;
    FloatVector light2;
    light2.x = dark.x;
    light2.y = light.y;
    light2.z = light.z;
    light2.w = 1.0f;
    m_pDevice->RenderCheckerboard(dark2, light2);
    m_pController->FatalErrorCheck();

    m_pDevice->FlipOutputWindow(outputID);
    m_pController->FatalErrorCheck();
  }
  else
  {
    m_pDevice->BindOutputWindow(outputID, false);
    m_pDevice->ClearOutputWindowColor(outputID, FloatVector());

    TextureDisplay disp;

    disp.red = disp.green = disp.blue = true;
    disp.alpha = false;
    disp.hdrMultiplier = -1.0f;
    disp.linearDisplayAsGamma = true;
    disp.flipY = false;
    disp.subresource = displaySubresource;
    disp.subresource.sample = 0;
    disp.customShaderId = ResourceId();
    disp.resourceId = displayTextureId;
    disp.typeCast = displayTypeCast;
    disp.scale = -1.0f;
    disp.rangeMin = 0.0f;
    disp.rangeMax = 1.0f;
    disp.xOffset = 0.0f;
    disp.yOffset = 0.0f;
    disp.rawOutput = false;
    disp.overlay = DebugOverlay::NoOverlay;

    if(displayTypeCast == CompType::SNorm)
      disp.rangeMin = -1.0f;

    if(depthMode)
      disp.green = disp.blue = false;

    m_pDevice->RenderTexture(disp);
    m_pController->FatalErrorCheck();

    m_pDevice->FlipOutputWindow(outputID);
    m_pController->FatalErrorCheck();
  }

  m_pDevice->GetOutputWindowData(outputID, ret);
  m_pController->FatalErrorCheck();

  return ret;
}

ResultDetails ReplayOutput::AddThumbnail(WindowingData window, ResourceId texID,
                                         const Subresource &sub, CompType typeCast)
{
  CHECK_REPLAY_THREAD();

  OutputPair p;

  RDCASSERT(window.system != WindowingSystem::Unknown && window.system != WindowingSystem::Headless);

  bool depthMode = false;

  for(size_t t = 0; t < m_pController->m_Textures.size(); t++)
  {
    if(m_pController->m_Textures[t].resourceId == texID)
    {
      depthMode = (m_pController->m_Textures[t].creationFlags & TextureCategory::DepthTarget) ||
                  (m_pController->m_Textures[t].format.compType == CompType::Depth);
      break;
    }
  }

  for(size_t i = 0; i < m_Thumbnails.size(); i++)
  {
    if(m_Thumbnails[i].wndHandle == GetHandle(window))
    {
      m_Thumbnails[i].texture = texID;
      m_Thumbnails[i].depthMode = depthMode;
      m_Thumbnails[i].sub = sub;
      m_Thumbnails[i].typeCast = typeCast;
      m_Thumbnails[i].dirty = true;

      return RDResult();
    }
  }

  p.wndHandle = GetHandle(window);
  p.outputID = m_pDevice->MakeOutputWindow(window, false);
  p.texture = texID;
  p.depthMode = depthMode;
  p.sub = sub;
  p.typeCast = typeCast;
  p.dirty = true;

  m_pController->FatalErrorCheck();

  m_Thumbnails.push_back(p);

  if(p.outputID == 0)
  {
    RETURN_ERROR_RESULT(ResultCode::InternalError, "Window creation failed");
  }

  return RDResult();
}

rdcpair<uint32_t, uint32_t> ReplayOutput::PickVertex(uint32_t x, uint32_t y)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(!m_pController->EnsureMaterializedCurrentEvent(__FUNCTION__))
    return {~0U, ~0U};

  ActionDescription *action = m_pController->GetActionByEID(m_EventID);

  const rdcpair<uint32_t, uint32_t> errorReturn = {~0U, ~0U};

  if(!action)
    return errorReturn;
  if(!(action->flags & (ActionFlags::MeshDispatch | ActionFlags::Drawcall)))
    return errorReturn;

  MeshDisplay cfg = m_RenderData.meshDisplay;

  if(cfg.position.vertexResourceId == ResourceId() || cfg.position.numIndices == 0)
    return errorReturn;

  cfg.position.vertexResourceId = cfg.position.vertexResourceId;
  cfg.position.indexResourceId = cfg.position.indexResourceId;
  cfg.second.vertexResourceId = cfg.second.vertexResourceId;
  cfg.second.indexResourceId = cfg.second.indexResourceId;

  // input data either doesn't vary with instance, or is trivial (all verts the same for that
  // element), so only care about fetching the right instance for post-VS stages
  if((action->flags & ActionFlags::Instanced) && m_RenderData.meshDisplay.type != MeshDataStage::VSIn)
  {
    // if no special options are enabled, just look at the current instance
    uint32_t firstInst = m_RenderData.meshDisplay.curInstance;
    uint32_t maxInst = m_RenderData.meshDisplay.curInstance + 1;

    if(m_RenderData.meshDisplay.showPrevInstances)
    {
      firstInst = 0;
    }

    if(m_RenderData.meshDisplay.showAllInstances)
    {
      firstInst = 0;
      maxInst = RDCMAX(1U, action->numInstances);
    }

    rdcarray<uint32_t> instIDs;
    instIDs.reserve(maxInst - firstInst + 1);
    for(uint32_t inst = firstInst; inst < maxInst; inst++)
      instIDs.push_back(inst);
    instIDs.push_back(m_RenderData.meshDisplay.curInstance);

    rdcarray<MeshFormat> fmts = m_pDevice->GetBatchPostVSBuffers(
        action->eventId, instIDs, m_RenderData.meshDisplay.curView, m_RenderData.meshDisplay.type);
    m_pController->FatalErrorCheck();

    // used for post-VS output, calculate the offset of the element we're using as position,
    // relative to 0
    // the last element in fmts corresponds to curInstance
    const uint64_t elemOffset = cfg.position.vertexByteOffset - fmts.back().vertexByteOffset;
    fmts.pop_back();

    uint32_t inst = firstInst;
    for(MeshFormat &fmt : fmts)
    {
      // find the start of this buffer, and apply the element offset, then pick in that instance
      if(fmt.vertexResourceId != ResourceId())
        cfg.position.vertexByteOffset = fmt.vertexByteOffset + elemOffset;

      uint32_t vert = m_pDevice->PickVertex(m_EventID, m_Width, m_Height, cfg, x, y);
      m_pController->FatalErrorCheck();

      if(vert != ~0U)
        return make_rdcpair(vert, inst);

      inst++;
    }

    return errorReturn;
  }
  else
  {
    uint32_t vert = m_pDevice->PickVertex(m_EventID, m_Width, m_Height, cfg, x, y);
    m_pController->FatalErrorCheck();

    if(vert != ~0U)
      return make_rdcpair(vert, m_RenderData.meshDisplay.curInstance);

    return errorReturn;
  }
}

void ReplayOutput::SetPixelContextLocation(uint32_t x, uint32_t y)
{
  CHECK_REPLAY_THREAD();

  m_ContextX = RDCMAX((float)x, 0.0f);
  m_ContextY = RDCMAX((float)y, 0.0f);

  DisplayContext();
}

void ReplayOutput::DisablePixelContext()
{
  CHECK_REPLAY_THREAD();

  m_ContextX = -1.0f;
  m_ContextY = -1.0f;

  DisplayContext();
}

void ReplayOutput::ClearBackground(uint64_t outputID, const FloatVector &backgroundColor)
{
  CHECK_REPLAY_THREAD();

  if(backgroundColor.x == 0.0f && backgroundColor.y == 0.0f && backgroundColor.z == 0.0f &&
     backgroundColor.w == 0.0f)
    m_pDevice->RenderCheckerboard(RenderDoc::Inst().DarkCheckerboardColor(),
                                  RenderDoc::Inst().LightCheckerboardColor());
  else
    m_pDevice->ClearOutputWindowColor(outputID, ConvertSRGBToLinear(backgroundColor));

  m_pController->FatalErrorCheck();
}

void ReplayOutput::DisplayContext()
{
  CHECK_REPLAY_THREAD();

  if(m_PixelContext.outputID == 0)
    return;

  if(m_HistoryReplaySource.active && !(m_ContextX < 0.0f && m_ContextY < 0.0f))
  {
    // The normal configuration was restored after the historical draw. Picking a new context
    // location must evaluate the selected historical source too. DisplayTextureAtEvent clears
    // this route while it draws, preventing recursion, and restores the controller event after.
    const ReplayHistoryDisplaySource source = m_HistoryReplaySource;
    DisplayTextureAtEvent(source.eventId, source.display);
    return;
  }

  m_pDevice->BindOutputWindow(m_PixelContext.outputID, false);
  m_pController->FatalErrorCheck();

  ClearBackground(m_PixelContext.outputID, m_RenderData.texDisplay.backgroundColor);

  if((m_Type != ReplayOutputType::Texture) || (m_ContextX < 0.0f && m_ContextY < 0.0f) ||
     (m_RenderData.texDisplay.resourceId == ResourceId()))
  {
    m_pDevice->FlipOutputWindow(m_PixelContext.outputID);
    m_pController->FatalErrorCheck();
    return;
  }

  TextureDisplay disp = m_RenderData.texDisplay;
  disp.rawOutput = false;
  disp.customShaderId = ResourceId();

  if(m_RenderData.texDisplay.customShaderId != ResourceId())
  {
    disp.resourceId = m_CustomShaderResourceId;

    disp.typeCast = CompType::Typeless;
    disp.customShaderId = ResourceId();
    disp.subresource.slice = 0;
  }

  if((m_RenderData.texDisplay.overlay == DebugOverlay::QuadOverdrawDraw ||
      m_RenderData.texDisplay.overlay == DebugOverlay::QuadOverdrawPass ||
      m_RenderData.texDisplay.overlay == DebugOverlay::TriangleSizeDraw ||
      m_RenderData.texDisplay.overlay == DebugOverlay::TriangleSizePass) &&
     m_OverlayResourceId != ResourceId())
  {
    disp.resourceId = m_OverlayResourceId;
    disp.typeCast = CompType::Typeless;
    disp.red = disp.green = disp.blue = disp.alpha = true;
    disp.rawOutput = false;
    disp.customShaderId = ResourceId();
    disp.hdrMultiplier = -1.0f;
    disp.rangeMin = 0.0f;
    disp.rangeMax = 1.0f;
    disp.linearDisplayAsGamma = false;
  }

  const float contextZoom = 8.0f;

  disp.scale = contextZoom / float(1 << disp.subresource.mip);

  int32_t width = 0, height = 0;
  m_pDevice->GetOutputWindowDimensions(m_PixelContext.outputID, width, height);

  float w = (float)width;
  float h = (float)height;

  int x = (int)m_ContextX;
  int y = (int)m_ContextY;

  if(m_TextureDim.first > 0 && m_TextureDim.second > 0)
  {
    rdcpair<uint32_t, uint32_t> mipDim = {RDCMAX(1U, m_TextureDim.first >> disp.subresource.mip),
                                          RDCMAX(1U, m_TextureDim.second >> disp.subresource.mip)};

    x = int((float(x) / float(m_TextureDim.first) + 1e-6f) * mipDim.first);
    y = int((float(y) / float(m_TextureDim.second) + 1e-6f) * mipDim.second);

    float fx = float(x) / mipDim.first;
    float fy = float(y) / mipDim.second;

    float mipAdjustFactor =
        RDCMAX(float(mipDim.first) / float(m_TextureDim.first) * float(1 << disp.subresource.mip),
               float(mipDim.second) / float(m_TextureDim.second) * float(1 << disp.subresource.mip));

    disp.scale *= mipAdjustFactor;

    disp.xOffset = -(float)(fx * m_TextureDim.first) * disp.scale;
    disp.yOffset = -(float)(fy * m_TextureDim.second) * disp.scale;
  }
  else
  {
    x >>= disp.subresource.mip;
    x <<= disp.subresource.mip;

    y >>= disp.subresource.mip;
    y <<= disp.subresource.mip;

    disp.xOffset = -(float)x * disp.scale;
    disp.yOffset = -(float)y * disp.scale;
  }

  disp.xOffset += w / 2.0f;
  disp.yOffset += h / 2.0f;

  m_pDevice->RenderTexture(disp);
  m_pController->FatalErrorCheck();

  m_pDevice->RenderHighlightBox(w, h, contextZoom);
  m_pController->FatalErrorCheck();

  m_pDevice->FlipOutputWindow(m_PixelContext.outputID);
  m_pController->FatalErrorCheck();
}

rdcarray<bytebuf> ReplayOutput::ReadbackTextureAtEvents(const rdcarray<uint32_t> &eventIds,
                                                        const TextureDisplay &config,
                                                        RENDERDOC_KillCallback cancelled)
{
  CHECK_REPLAY_THREAD();
  if(eventIds.empty() || eventIds.size() > 24 || (cancelled && cancelled()))
    return {};
  bool ordinary = m_HeadlessWindow && config.customShaderId == ResourceId() &&
                  config.overlay == DebugOverlay::NoOverlay && !config.decodeYUV &&
                  std::is_sorted(eventIds.begin(), eventIds.end());
  for(uint32_t event : eventIds)
    ordinary &= !m_pController->HasEventOverrides(event);
  if(!ordinary || Process::GetEnvVariable("RENDERDOC_DISABLE_HISTORY_BATCH") == "1")
    return IReplayOutput::ReadbackTextureAtEvents(eventIds, config, cancelled);

  PerformanceTimer timer;
  double replayMS = 0.0, displayMS = 0.0;
  double firstCompletedMS = 0.0;
  const uint32_t savedEvent = m_EventID;
  const TextureDisplay savedDisplay = m_RenderData.texDisplay;
  const rdcpair<uint32_t, uint32_t> savedTextureDim = m_TextureDim;
  const bool savedMaintenance = m_pController->m_IdleCheckpointMaintenance;
  m_pController->m_IdleCheckpointMaintenance = true;
  rdcarray<bytebuf> result;
  bool replayed = false;
  const bool forwardBatch = Process::GetEnvVariable("RENDERDOC_ENABLE_HISTORY_FORWARD_BATCH") == "1";
  result.reserve(eventIds.size());
  // Use the same analysis replay as DisplayTextureAtEvent, but restore the live selection once.
  // Forward two-phase replay remains experimental: White's historical target has shown differing
  // pixels versus the reference Full replay even where repeated reference images were stable.
  for(uint32_t requested : eventIds)
  {
    if(cancelled && cancelled())
      break;
    if(result.size() >= 2 && timer.GetMilliseconds() - firstCompletedMS >= 100.0)
      break;
    const auto remap = m_pController->m_EventRemap.find(requested);
    const uint32_t event = remap == m_pController->m_EventRemap.end() ? requested : remap->second;
    const double start = timer.GetMilliseconds();
    m_pController->InvalidateScrubReplay();
    replayed = true;
    if(forwardBatch)
      m_pController->ReplayEvent(event, false, false, false);
    else
      m_pController->ReplayFrameEventForAnalysis(event);
    replayMS += timer.GetMilliseconds() - start;
    if(m_pController->m_FatalError != ResultCode::Succeeded)
      break;
    m_pController->m_MaterializedEventValid = false;
    m_pController->m_AnalysisReplayState = ReplayController::AnalysisReplayState::Ready;
    m_pController->m_AnalysisReplayEventID = event;
    m_EventID = event;
    SetTextureDisplayInternal(config, false);
    m_OverlayDirty = false;
    m_ForceOverlayRefresh = false;
    const double drawStart = timer.GetMilliseconds();
    if(m_pDevice->CheckResizeOutputWindow(m_MainOutput.outputID))
      m_pDevice->GetOutputWindowDimensions(m_MainOutput.outputID, m_Width, m_Height);
    DisplayTex();
    m_pDevice->FlipOutputWindow(m_MainOutput.outputID);
    m_pController->FatalErrorCheck();
    if(m_pController->m_FatalError != ResultCode::Succeeded)
      break;
    result.push_back(ReadbackOutputTexture());
    if(result.size() == 1)
      firstCompletedMS = timer.GetMilliseconds();
    displayMS += timer.GetMilliseconds() - drawStart;
  }
  const double restoreStart = timer.GetMilliseconds();
  if(replayed && m_pController->m_FatalError == ResultCode::Succeeded)
    m_pController->RestoreFrameEventAfterAnalysis();
  m_pController->m_IdleCheckpointMaintenance = savedMaintenance;
  m_EventID = savedEvent;
  m_RenderData.texDisplay = savedDisplay;
  m_OverlayDirty = m_CustomDirty = true;
  m_ForceOverlayRefresh = false;
  m_TextureDim = savedTextureDim;
  m_MainOutput.dirty = true;
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    AppendReplayTimingLog(
        StringFormat::Fmt("api=TextureHistoryBatch requested=%zu completed=%zu replay_ms=%.3f "
                          "displayReadback_ms=%.3f restore_ms=%.3f total_ms=%.3f",
                          eventIds.size(), result.size(), replayMS, displayMS,
                          timer.GetMilliseconds() - restoreStart, timer.GetMilliseconds()));
  return result;
}

void ReplayOutput::Display()
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  static const bool eagerMesh =
      Process::GetEnvVariable("RENDERDOC_REPLAY_EAGER_MESH_OUTPUTS") == "1";
  // Keep dirty state while hidden. Native/headless output windows report their own visibility;
  // headless mesh rendering remains available and is not treated as a hidden GUI tab.
  if(!eagerMesh && m_Type == ReplayOutputType::Mesh && m_MainOutput.outputID != 0 &&
     !m_pDevice->IsOutputWindowVisible(m_MainOutput.outputID))
    return;

  if(!m_pController->EnsureMaterializedCurrentEvent(__FUNCTION__))
    return;

  if(m_HistorySnapshotActive)
  {
    const uint64_t generation = m_pController->GetShaderReplacementGeneration(0);
    if(generation != m_HistorySnapshotGeneration)
    {
      TextureDisplay refreshed;
      if(m_pController->ResolveEventTextureSnapshot(m_HistorySnapshotEventID,
                                                    m_HistorySnapshotSource, refreshed))
      {
        SetTextureDisplayInternal(refreshed, true);
        const TextureDescription refreshedDesc = m_pDevice->GetTexture(refreshed.resourceId);
        m_TextureDim = {refreshedDesc.width, refreshedDesc.height};
        // NaN and clipping are evaluated by the texture display shader. They are included in the
        // presentation-only snapshot path and must not fall through to event-derived overlay
        // replay just because the refreshed ResourceId differs from the previous snapshot.
        m_OverlayDirty = false;
        m_HistorySnapshotGeneration = generation;
      }
      else
      {
        // Preserve correctness for an unsupported topology. DisplayTextureAtEvent owns the
        // analysis replay/restore fallback and clears persistent snapshot mode if it cannot cache.
        // Drop the old snapshot binding first: Resolve may have retired it, and saving/restoring
        // that stale ResourceId in the slow path would keep stale pixels alive after the edit.
        const uint32_t historyEvent = m_HistorySnapshotEventID;
        const TextureDisplay historySource = m_HistorySnapshotSource;
        m_HistorySnapshotActive = false;
        SetTextureDisplayInternal(historySource, true);
        DisplayTextureAtEvent(historyEvent, historySource);
        return;
      }
    }
  }

  if(m_pDevice->CheckResizeOutputWindow(m_MainOutput.outputID))
  {
    m_pDevice->GetOutputWindowDimensions(m_MainOutput.outputID, m_Width, m_Height);
    m_MainOutput.dirty = true;
  }

  if(m_pDevice->CheckResizeOutputWindow(m_PixelContext.outputID))
    m_MainOutput.dirty = true;

  for(size_t i = 0; i < m_Thumbnails.size(); i++)
    if(m_pDevice->CheckResizeOutputWindow(m_Thumbnails[i].outputID))
      m_Thumbnails[i].dirty = true;

  m_pController->FatalErrorCheck();

  if(m_HistoryReplaySource.NeedsReplay(
         m_MainOutput.dirty, {m_Width, m_Height},
         m_pController->GetShaderReplacementGeneration(0)))
  {
    // Resize/expose and edits must not redraw the saved live-event configuration into a history
    // tab. Retain only the requested event/configuration and rerender it through analysis replay.
    // The existing presented output can still be flipped when no content or canvas changed.
    const ReplayHistoryDisplaySource source = m_HistoryReplaySource;
    DisplayTextureAtEvent(source.eventId, source.display);
  }
  else if(m_MainOutput.dirty)
  {
    m_MainOutput.dirty = false;

    switch(m_Type)
    {
      case ReplayOutputType::Mesh: DisplayMesh(); break;
      case ReplayOutputType::Texture: DisplayTex(); break;
      default: RDCERR("Unexpected display type! %d", m_Type); break;
    }

    m_pDevice->FlipOutputWindow(m_MainOutput.outputID);
    m_pController->FatalErrorCheck();

    DisplayContext();
  }
  else
  {
    m_pDevice->BindOutputWindow(m_MainOutput.outputID, false);
    m_pDevice->FlipOutputWindow(m_MainOutput.outputID);
    m_pController->FatalErrorCheck();
    m_pDevice->BindOutputWindow(m_PixelContext.outputID, false);
    m_pDevice->FlipOutputWindow(m_PixelContext.outputID);
    m_pController->FatalErrorCheck();
  }

  for(size_t i = 0; i < m_Thumbnails.size(); i++)
  {
    if(!m_Thumbnails[i].dirty)
    {
      m_pDevice->BindOutputWindow(m_Thumbnails[i].outputID, false);
      m_pDevice->FlipOutputWindow(m_Thumbnails[i].outputID);
      continue;
    }

    if(!m_pDevice->IsOutputWindowVisible(m_Thumbnails[i].outputID))
      continue;

    FloatVector color;

    if(m_Thumbnails[i].texture == ResourceId())
    {
      m_pDevice->BindOutputWindow(m_Thumbnails[i].outputID, false);

      FloatVector dark = RenderDoc::Inst().DarkCheckerboardColor();
      FloatVector light = RenderDoc::Inst().LightCheckerboardColor();

      FloatVector dark2;
      dark2.x = light.x;
      dark2.y = dark.y;
      dark2.z = dark.z;
      dark2.w = 1.0f;
      FloatVector light2;
      light2.x = dark.x;
      light2.y = light.y;
      light2.z = light.z;
      light2.w = 1.0f;
      m_pDevice->RenderCheckerboard(dark2, light2);
      m_pController->FatalErrorCheck();

      m_pDevice->FlipOutputWindow(m_Thumbnails[i].outputID);
      m_pController->FatalErrorCheck();
      continue;
    }

    m_pDevice->BindOutputWindow(m_Thumbnails[i].outputID, false);
    m_pDevice->ClearOutputWindowColor(m_Thumbnails[i].outputID, color);

    TextureDisplay disp;

    disp.red = disp.green = disp.blue = true;
    disp.alpha = false;
    disp.hdrMultiplier = -1.0f;
    disp.linearDisplayAsGamma = true;
    disp.flipY = false;
    disp.subresource = m_Thumbnails[i].sub;
    disp.subresource.sample = 0;
    disp.customShaderId = ResourceId();
    disp.resourceId = m_Thumbnails[i].texture;
    disp.typeCast = m_Thumbnails[i].typeCast;
    disp.scale = -1.0f;
    disp.rangeMin = 0.0f;
    disp.rangeMax = 1.0f;
    disp.xOffset = 0.0f;
    disp.yOffset = 0.0f;
    disp.rawOutput = false;
    disp.overlay = DebugOverlay::NoOverlay;

    if(m_Thumbnails[i].typeCast == CompType::SNorm)
      disp.rangeMin = -1.0f;

    if(m_Thumbnails[i].depthMode)
      disp.green = disp.blue = false;

    m_pDevice->RenderTexture(disp);
    m_pController->FatalErrorCheck();

    m_pDevice->FlipOutputWindow(m_Thumbnails[i].outputID);
    m_pController->FatalErrorCheck();

    m_Thumbnails[i].dirty = false;
  }
}

void ReplayOutput::ReplayCapture(uint32_t eventId, ReplayLogType type)
{
  // CommitScrubFrameEvent may skip ReplayLog only while the capture state left by ScrubFrameEvent
  // is still live. Overlay/custom/mesh preparation can replace that state between presentation and
  // commit, even when it happens to replay the same EID.
  m_pController->InvalidateScrubReplay();
  m_pController->m_MaterializedEventValid = false;
  const bool previousCheckpointBypass = m_pDevice->m_ReplayCheckpointBypass;
  const bool previousReference = m_pDevice->m_ReplayCheckpointNaturalReference;
  m_pDevice->m_ReplayCheckpointBypass = true;
  m_pDevice->m_ReplayCheckpointNaturalReference = false;
  m_pDevice->ReplayLog(eventId, type);
  m_pDevice->m_ReplayCheckpointBypass = previousCheckpointBypass;
  m_pDevice->m_ReplayCheckpointNaturalReference = previousReference;
  if(m_pController->m_FatalError == ResultCode::Succeeded &&
     (type == eReplay_Full || type == eReplay_OnlyDraw))
  {
    m_pController->m_MaterializedEventID = eventId;
    m_pController->m_MaterializedEventValid = true;
  }
}

void ReplayController::RetireEventTextureSnapshots(const rdcarray<ResourceId> &snapshots)
{
  CHECK_REPLAY_THREAD();

  for(ResourceId snapshot : snapshots)
    if(snapshot != ResourceId())
      m_RetiredEventTextureSnapshots.insert(snapshot);
}

void ReplayController::ReapRetiredEventTextureSnapshots()
{
  CHECK_REPLAY_THREAD();

  if(m_pDevice == NULL || m_RetiredEventTextureSnapshots.empty())
    return;

  std::set<ResourceId> pinned;

  // A history snapshot remains the persistent TextureDisplay for repaint, resize and pixel
  // context work. Never destroy it while any ReplayOutput still holds that binding.
  for(const ReplayOutput *output : m_Outputs)
  {
    if(output != NULL && output->m_Type == ReplayOutputType::Texture)
      pinned.insert(output->m_RenderData.texDisplay.resourceId);
  }

  // Retired resources should no longer be cache values, but keep this guard so a duplicate return
  // from a backend cannot turn a bookkeeping mistake into a live-resource destruction.
  for(const EventTextureSnapshot &entry : m_EventTextureSnapshots)
    if(entry.snapshot != ResourceId())
      pinned.insert(entry.snapshot);

  rdcarray<ResourceId> resourcesToFree;
  for(auto it = m_RetiredEventTextureSnapshots.begin();
      it != m_RetiredEventTextureSnapshots.end();)
  {
    if(pinned.find(*it) != pinned.end())
    {
      ++it;
      continue;
    }

    resourcesToFree.push_back(*it);
    it = m_RetiredEventTextureSnapshots.erase(it);
  }

  if(!resourcesToFree.empty())
    m_pDevice->FreeTextureSnapshots(resourcesToFree);
}

void ReplayController::TrimEventTextureSnapshots(ResourceId keepSnapshot)
{
  CHECK_REPLAY_THREAD();

  // Bound output images must remain in the lookup cache as well as alive. Removing a still-bound
  // entry makes the next visit rebuild an identical image and causes cache churn.
  std::set<ResourceId> pinned;
  pinned.insert(keepSnapshot);
  for(const ReplayOutput *output : m_Outputs)
  {
    if(output != NULL && output->m_Type == ReplayOutputType::Texture)
      pinned.insert(output->m_RenderData.texDisplay.resourceId);
  }

  constexpr uint64_t SnapshotHighBytes = 512ULL * 1024ULL * 1024ULL;
  constexpr uint64_t SnapshotLowBytes = 384ULL * 1024ULL * 1024ULL;
  constexpr size_t SnapshotEntryHighCount = 96;
  constexpr size_t SnapshotEntryLowCount = 72;

  rdcarray<ResourceId> snapshotsToRetire;
  if(m_EventTextureSnapshotBytes > SnapshotHighBytes ||
     m_EventTextureSnapshots.size() > SnapshotEntryHighCount)
  {
    while(m_EventTextureSnapshotBytes > SnapshotLowBytes ||
          m_EventTextureSnapshots.size() > SnapshotEntryLowCount)
    {
      size_t oldestIndex = size_t(-1);
      uint64_t oldestUse = ~0ULL;
      for(size_t i = 0; i < m_EventTextureSnapshots.size(); i++)
      {
        const EventTextureSnapshot &entry = m_EventTextureSnapshots[i];
        if(pinned.find(entry.snapshot) != pinned.end())
          continue;
        if(entry.lastUseSerial < oldestUse)
        {
          oldestUse = entry.lastUseSerial;
          oldestIndex = i;
        }
      }

      if(oldestIndex == size_t(-1))
        break;

      const EventTextureSnapshot &oldest = m_EventTextureSnapshots[oldestIndex];
      if(oldest.snapshot != ResourceId())
        snapshotsToRetire.push_back(oldest.snapshot);
      if(oldest.allocationBytes >= m_EventTextureSnapshotBytes)
        m_EventTextureSnapshotBytes = 0;
      else
        m_EventTextureSnapshotBytes -= oldest.allocationBytes;
      m_EventTextureSnapshots.erase(oldestIndex);
    }
  }

  // Batch retired history resource destruction at a replay boundary. Presentation-only changes
  // must not introduce a queue drain merely to trim the history cache.
  RetireEventTextureSnapshots(snapshotsToRetire);
}

bool ReplayController::ResolveEventTextureSnapshot(uint32_t eventId, const TextureDisplay &source,
                                                   TextureDisplay &snapshotDisplay)
{
  CHECK_REPLAY_THREAD();

  if(source.resourceId == ResourceId())
    return false;

  const TextureDescription sourceDesc = m_pDevice->GetTexture(source.resourceId);
  if(sourceDesc.resourceId == ResourceId() || sourceDesc.width == 0 || sourceDesc.height == 0)
    return false;

  CompType sourceTypeCast = source.typeCast;
  if(sourceTypeCast == CompType::Typeless)
    sourceTypeCast = sourceDesc.format.compType;
  if(sourceTypeCast == CompType::Typeless || sourceTypeCast == CompType::Depth)
    sourceTypeCast = CompType::Float;

  EventTextureSnapshot *cached = NULL;
  for(EventTextureSnapshot &entry : m_EventTextureSnapshots)
  {
    if(entry.eventId == eventId && entry.source == source.resourceId &&
       entry.sourceSubresource == source.subresource && entry.sourceTypeCast == sourceTypeCast)
    {
      cached = &entry;
      break;
    }
  }

  // A replacement at an earlier event can affect this event's output even if the replacement was
  // installed through an event-scoped workbench operation. Conservatively use the global mutation
  // serial until replacement dependency ranges are tracked explicitly.
  const uint64_t outputMutationGeneration = GetShaderReplacementGeneration(0);
  if(cached == NULL)
  {
    EventTextureSnapshot entry;
    entry.eventId = eventId;
    entry.source = source.resourceId;
    entry.sourceSubresource = source.subresource;
    entry.sourceTypeCast = sourceTypeCast;
    m_EventTextureSnapshots.push_back(entry);
    cached = &m_EventTextureSnapshots.back();
  }

  cached->lastUseSerial = ++m_EventTextureSnapshotUseSerial;

  if(cached->unavailable && cached->outputMutationGeneration == outputMutationGeneration)
    return false;

  rdcarray<ResourceId> snapshotsToRetire;
  auto removeTrackedBytes = [this](uint64_t bytes) {
    if(bytes >= m_EventTextureSnapshotBytes)
      m_EventTextureSnapshotBytes = 0;
    else
      m_EventTextureSnapshotBytes -= bytes;
  };

  if(cached->snapshot == ResourceId() ||
     cached->outputMutationGeneration != outputMutationGeneration)
  {
    GetTextureDataParams params;
    params.standardLayout = true;
    params.typeCast = sourceTypeCast;
    params.resolve = source.subresource.sample == TextureDisplay::ResolveSamples;
    params.remap = RemapTexture::RGBA32;

    // A snapshot of the already materialized current event only reads its captured resources.
    // Native copies restore source barriers/state and write a replay-owned destination. Keep the
    // normal replay/restore path for analysis state or presentation requiring event-derived work.
    const bool currentState =
        eventId == m_EventID && IsCurrentEventMaterialized() && !m_AnalysisEventOverridesActive &&
        source.customShaderId == ResourceId() && !source.decodeYUV &&
        (source.overlay == DebugOverlay::NoOverlay || source.overlay == DebugOverlay::NaN ||
         source.overlay == DebugOverlay::Clipping);
    if(!currentState)
      ReplayFrameEventForAnalysis(eventId);

    // A fatal replay error swaps the driver and clears the snapshot cache. In that case cached is
    // no longer valid, and every resource created by the old driver is released by its Shutdown.
    if(m_FatalError != ResultCode::Succeeded)
      return false;

    // Prefer a native, replay-owned GPU image. It survives subsequent frame replays, so zoom,
    // channel/range and gamma changes can be drawn straight from this resource without staging the
    // texture through CPU memory. Drivers return null for topology/format cases they cannot copy
    // safely, preserving the portable readback fallback below.
    const ResourceId staleNativeSnapshot =
        cached->nativeGPUSnapshot ? cached->snapshot : ResourceId();
    const uint64_t staleNativeBytes = cached->nativeGPUSnapshot ? cached->allocationBytes : 0;

    Subresource nativeSnapshotSubresource;
    ResourceId nativeSnapshot;
    // Once a driver has fallen back to a CPU-upload proxy for this exact topology, keep updating
    // that proxy after edits instead of orphaning it by retrying the same unsupported native copy.
    if(cached->snapshot == ResourceId() || cached->nativeGPUSnapshot)
      nativeSnapshot = m_pDevice->CreateTextureSnapshot(
          source.resourceId, source.subresource, sourceTypeCast, nativeSnapshotSubresource);

    // Snapshot creation can report a deferred device failure. FatalErrorCheck swaps the driver and
    // clears m_EventTextureSnapshots, so do not touch cached afterwards in that case.
    if(FatalErrorCheck())
      return false;

    if(nativeSnapshot != ResourceId())
    {
      if(!currentState)
        RestoreFrameEventAfterAnalysis();

      // Restore can itself detect a fatal error and clear m_EventTextureSnapshots.
      if(m_FatalError != ResultCode::Succeeded)
        return false;

      if(staleNativeSnapshot != ResourceId())
      {
        removeTrackedBytes(staleNativeBytes);
        if(staleNativeSnapshot != nativeSnapshot)
          snapshotsToRetire.push_back(staleNativeSnapshot);
      }

      const TextureDescription nativeDesc = m_pDevice->GetTexture(nativeSnapshot);

      cached->snapshot = nativeSnapshot;
      cached->snapshotSubresource = nativeSnapshotSubresource;
      cached->snapshotTypeCast = sourceTypeCast;
      cached->outputMutationGeneration = outputMutationGeneration;
      cached->allocationBytes = nativeDesc.byteSize;
      cached->nativeGPUSnapshot = true;
      cached->unavailable = false;
      m_EventTextureSnapshotBytes += cached->allocationBytes;
    }
    else
    {
      // Vulkan, remote replay, image viewers, and dummy drivers have no replay-owned upload proxy.
      // If their native copy rejects an exotic texture topology, fail closed here. Reading the
      // whole texture to CPU only to discover that it cannot be uploaded again adds a large stall
      // and cannot produce a persistent snapshot.
      if(!m_pDevice->SupportsProxyTextureSnapshots())
      {
        if(!currentState)
          RestoreFrameEventAfterAnalysis();

        if(m_FatalError != ResultCode::Succeeded)
          return false;

        if(staleNativeSnapshot != ResourceId())
        {
          snapshotsToRetire.push_back(staleNativeSnapshot);
          removeTrackedBytes(staleNativeBytes);
          cached->snapshot = ResourceId();
          cached->snapshotSubresource = Subresource();
          cached->snapshotTypeCast = CompType::Typeless;
          cached->allocationBytes = 0;
          cached->nativeGPUSnapshot = false;
        }

        cached->unavailable = true;
        cached->outputMutationGeneration = outputMutationGeneration;
        RetireEventTextureSnapshots(snapshotsToRetire);
        return false;
      }

      bytebuf pixels;
      m_pDevice->GetTextureData(source.resourceId, source.subresource, params, pixels);

      // As with snapshot creation, check before restoring or dereferencing the cache entry.
      if(FatalErrorCheck())
        return false;

      if(!currentState)
        RestoreFrameEventAfterAnalysis();

      // As above, cached belongs to m_EventTextureSnapshots and is invalid after a fatal swap.
      if(m_FatalError != ResultCode::Succeeded)
        return false;

      // A native snapshot is not a CPU-upload proxy. Retire the old GPU image before entering the
      // portable fallback so SetProxyTextureData is never called on a driver-owned native image.
      if(staleNativeSnapshot != ResourceId())
      {
        snapshotsToRetire.push_back(staleNativeSnapshot);
        removeTrackedBytes(staleNativeBytes);
        cached->snapshot = ResourceId();
        cached->snapshotSubresource = Subresource();
        cached->snapshotTypeCast = CompType::Typeless;
        cached->allocationBytes = 0;
        cached->nativeGPUSnapshot = false;
      }

      const uint32_t mip = RDCMIN(source.subresource.mip, 31U);
      const uint32_t width = RDCMAX(1U, sourceDesc.width >> mip);
      const uint32_t height = RDCMAX(1U, sourceDesc.height >> mip);
      const uint64_t expectedSize = uint64_t(width) * uint64_t(height) * 4ULL * sizeof(uint32_t);
      if(pixels.size() != expectedSize)
      {
        // A generation change may be rebuilding an existing CPU proxy. It still contains pixels
        // from the previous generation, so remove it from the active entry and retire it instead
        // of leaving stale output reachable through an unavailable cache entry.
        if(cached->snapshot != ResourceId())
        {
          snapshotsToRetire.push_back(cached->snapshot);
          removeTrackedBytes(cached->allocationBytes);
          cached->snapshot = ResourceId();
          cached->snapshotSubresource = Subresource();
          cached->snapshotTypeCast = CompType::Typeless;
          cached->allocationBytes = 0;
          cached->nativeGPUSnapshot = false;
        }

        cached->unavailable = true;
        cached->outputMutationGeneration = outputMutationGeneration;
        RetireEventTextureSnapshots(snapshotsToRetire);
        return false;
      }

      if(cached->snapshot == ResourceId())
      {
        TextureDescription snapshotDesc = {};
        snapshotDesc.format.type = ResourceFormatType::Regular;
        snapshotDesc.format.compCount = 4;
        snapshotDesc.format.compByteWidth = 4;
        snapshotDesc.format.compType = BaseRemapType(RemapTexture::RGBA32, sourceTypeCast);
        if(snapshotDesc.format.compType == CompType::Typeless ||
           snapshotDesc.format.compType == CompType::Depth)
          snapshotDesc.format.compType = CompType::Float;
        snapshotDesc.dimension = 2;
        snapshotDesc.type = TextureType::Texture2D;
        snapshotDesc.width = width;
        snapshotDesc.height = height;
        snapshotDesc.depth = 1;
        snapshotDesc.cubemap = false;
        snapshotDesc.mips = 1;
        snapshotDesc.arraysize = 1;
        snapshotDesc.creationFlags = TextureCategory::ShaderRead;
        snapshotDesc.msQual = 0;
        snapshotDesc.msSamp = 1;
        snapshotDesc.byteSize = expectedSize;

        const ResourceId proxySnapshot = m_pDevice->CreateProxyTextureSnapshot(snapshotDesc);

        // FatalErrorCheck clears the entry containing cached. Assign only after the driver remains
        // valid so an old-driver ResourceId can never enter the new driver's cache.
        if(FatalErrorCheck())
          return false;

        cached->snapshot = proxySnapshot;
        cached->snapshotSubresource = Subresource();
        cached->snapshotTypeCast = snapshotDesc.format.compType;
        if(cached->snapshot != ResourceId())
        {
          cached->allocationBytes = expectedSize;
          m_EventTextureSnapshotBytes += cached->allocationBytes;
        }
        cached->nativeGPUSnapshot = false;
      }

      if(cached->snapshot == ResourceId())
      {
        cached->unavailable = true;
        cached->outputMutationGeneration = outputMutationGeneration;
        RetireEventTextureSnapshots(snapshotsToRetire);
        return false;
      }

      m_pDevice->SetProxyTextureData(cached->snapshot, Subresource(), pixels.data(), pixels.size());

      // Upload may surface device removal. The fatal path owns and destroys the proxy with the old
      // driver, and clears cached, so stop before publishing generation metadata.
      if(FatalErrorCheck())
        return false;

      cached->outputMutationGeneration = outputMutationGeneration;
      cached->nativeGPUSnapshot = false;
      cached->unavailable = false;
    }
  }

  snapshotDisplay = source;
  snapshotDisplay.resourceId = cached->snapshot;
  snapshotDisplay.subresource = cached->snapshotSubresource;
  snapshotDisplay.typeCast = cached->snapshotTypeCast;
  snapshotDisplay.decodeYUV = false;
  // RGBA32 remapping samples an sRGB source through an sRGB view, so the cached float pixels are
  // linear. Preserve the original presentation by applying the display gamma curve to the float
  // proxy even though the source TextureDisplay normally leaves this disabled for an sRGB format.
  if(!cached->nativeGPUSnapshot &&
     (sourceDesc.format.SRGBCorrected() || sourceTypeCast == CompType::UNormSRGB))
    snapshotDisplay.linearDisplayAsGamma = true;

  RetireEventTextureSnapshots(snapshotsToRetire);
  TrimEventTextureSnapshots(cached->snapshot);

  return true;
}

void ReplayOutput::DisplayTextureAtEvent(uint32_t eventId, const TextureDisplay &config)
{
  CHECK_REPLAY_THREAD();

  if(m_Type != ReplayOutputType::Texture)
    return;

  RENDERDOC_PROFILEFUNCTION();

  const bool timing = m_pController->EIDTimingEnabled();
  const double timerFrequency = timing ? Timing::GetTickFrequency() : 1.0;
  const uint64_t startTick = timing ? Timing::GetTick() : 0;
  const auto elapsedMS = [&]() {
    return timing ? double(Timing::GetTick() - startTick) / timerFrequency : 0.0;
  };

  auto remapIt = m_pController->m_EventRemap.find(eventId);
  if(remapIt != m_pController->m_EventRemap.end())
    eventId = remapIt->second;

  // Event-derived overlays and custom display shaders require the original event state. The
  // ordinary channel/range/gamma/zoom path in a native window is rendered from a persistent
  // replay-owned snapshot instead, so presentation-only changes never replay the frame after the
  // first capture. Headless thumbnail callers consume their small output with ReadbackOutputTexture
  // immediately; retain only that result instead of allocating a full-size source snapshot.
  const bool presentationOnly =
      config.customShaderId == ResourceId() && !config.decodeYUV &&
      (config.overlay == DebugOverlay::NoOverlay || config.overlay == DebugOverlay::NaN ||
       config.overlay == DebugOverlay::Clipping);
  const bool currentState =
      presentationOnly && eventId == m_pController->m_EventID &&
      m_pController->IsCurrentEventMaterialized() && !m_pController->m_AnalysisEventOverridesActive;
  TextureDisplay snapshotDisplay;
  const bool haveSnapshot =
      !m_HeadlessWindow && presentationOnly &&
      m_pController->ResolveEventTextureSnapshot(eventId, config, snapshotDisplay);
  const double resolveMS = elapsedMS();

  // Publish a new persistent identity only after the snapshot path succeeds below. A slow
  // overlay/custom/YUV fallback must not leave a stale historical source attached to Display().
  m_HistorySnapshotActive = false;
  m_HistoryReplaySource.Clear();

  const uint32_t savedEventID = m_EventID;
  const TextureDisplay savedDisplay = m_RenderData.texDisplay;
  const bool savedCustomDirty = m_CustomDirty;
  const bool savedOverlayDirty = m_OverlayDirty;
  const bool savedForceOverlayRefresh = m_ForceOverlayRefresh;
  const ResourceId savedOverlayResource = m_OverlayResourceId;
  const ResourceId savedCustomResource = m_CustomShaderResourceId;
  const rdcpair<uint32_t, uint32_t> savedTextureDim = m_TextureDim;
  const rdcarray<uint32_t> savedPassEvents = passEvents;

  const bool replayForDisplay = !haveSnapshot && !(m_HeadlessWindow && currentState);
  if(replayForDisplay)
    m_pController->ReplayFrameEventForAnalysis(eventId);
  const double replayEndMS = elapsedMS();

  m_EventID = eventId;
  SetTextureDisplayInternal(haveSnapshot ? snapshotDisplay : config, false);
  // NaN/clipping are evaluated by the display shader and never need capture replay.
  m_OverlayDirty = !haveSnapshot && config.overlay != DebugOverlay::NoOverlay &&
                   config.overlay != DebugOverlay::NaN && config.overlay != DebugOverlay::Clipping;
  m_CustomDirty = true;
  m_ForceOverlayRefresh = false;

  if(haveSnapshot)
  {
    const TextureDescription snapshotDesc = m_pDevice->GetTexture(snapshotDisplay.resourceId);
    m_TextureDim = {snapshotDesc.width, snapshotDesc.height};
  }

  if(m_pDevice->CheckResizeOutputWindow(m_MainOutput.outputID))
    m_pDevice->GetOutputWindowDimensions(m_MainOutput.outputID, m_Width, m_Height);
  if(m_pDevice->CheckResizeOutputWindow(m_PixelContext.outputID))
    m_MainOutput.dirty = true;

  m_pController->FatalErrorCheck();

  DisplayTex();
  m_pDevice->FlipOutputWindow(m_MainOutput.outputID);
  m_pController->FatalErrorCheck();
  DisplayContext();
  const double renderEndMS = elapsedMS();

  if(replayForDisplay)
    m_pController->RestoreFrameEventAfterAnalysis();
  const double restoreEndMS = elapsedMS();

  const auto logTiming = [&]() {
    if(!timing)
      return;

    const rdcstr line = StringFormat::Fmt(
        "api=HistoryPreview eid=%u headless=%u snapshotroute=%u currentstate=%u resolve_ms=%.3f "
        "replay_ms=%.3f render_ms=%.3f restore_ms=%.3f total_ms=%.3f",
        eventId, m_HeadlessWindow ? 1U : 0U, haveSnapshot ? 1U : 0U, currentState ? 1U : 0U, resolveMS,
        replayForDisplay ? replayEndMS - resolveMS : 0.0, renderEndMS - replayEndMS,
        replayForDisplay ? restoreEndMS - renderEndMS : 0.0,
        elapsedMS());
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  };

  // A snapshot is an ordinary replay-owned texture. Keep it as this output's persistent display so
  // resize, pixel-context refresh, and repaint continue to use static data. Leaving the history tab
  // sends a normal SetTextureDisplay with the live texture and replaces it.
  if(haveSnapshot)
  {
    m_EventID = savedEventID;
    passEvents = savedPassEvents;
    m_MainOutput.dirty = false;
    m_TransientTextureEventDisplay = false;
    m_HistorySnapshotSource = config;
    m_HistorySnapshotEventID = eventId;
    m_HistorySnapshotGeneration = m_pController->GetShaderReplacementGeneration(0);
    m_HistorySnapshotActive = true;
    m_pController->ReapRetiredEventTextureSnapshots();
    logTiming();
    return;
  }

  // Headless thumbnails and the overlay/custom-shader fallback retain the rendered output image.
  // RestoreFrameEventAfterAnalysis restores captured resources without presenting ReplayOutputs,
  // so restoring configuration here leaves that image intact for ReadbackOutputTexture.
  m_EventID = savedEventID;
  m_RenderData.texDisplay = savedDisplay;
  m_CustomDirty = savedCustomDirty;
  m_OverlayDirty = savedOverlayDirty;
  m_ForceOverlayRefresh = savedForceOverlayRefresh;
  m_OverlayResourceId = savedOverlayResource;
  m_CustomShaderResourceId = savedCustomResource;
  m_TextureDim = savedTextureDim;
  passEvents = savedPassEvents;
  m_MainOutput.dirty = false;
  // Preserve the history overlay/custom result for picking while the embedded history remains
  // active. The next ordinary SetTextureDisplay consumes this flag and forces event-derived
  // resources to be rebuilt before the persistent current-event output is shown again.
  m_TransientTextureEventDisplay = true;
  // Preserve the source, not a screenshot: the next resize, context refresh or mutation redraws
  // this exact historical request and then restores the controller's current event again.
  m_HistoryReplaySource.Publish(
      eventId, config, m_pController->GetShaderReplacementGeneration(0), {m_Width, m_Height},
      m_pController->GetFatalErrorStatus().code == ResultCode::Succeeded);
  m_pController->ReapRetiredEventTextureSnapshots();
  logTiming();
}

void ReplayOutput::DisplayTex()
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  ActionDescription *action = m_pController->GetActionByEID(m_EventID);

  if(m_MainOutput.outputID == 0)
    return;
  if(m_RenderData.texDisplay.resourceId == ResourceId() || m_Width <= 0 || m_Height <= 0)
  {
    FloatVector color;
    m_pDevice->BindOutputWindow(m_MainOutput.outputID, false);
    m_pDevice->ClearOutputWindowColor(m_MainOutput.outputID, color);
    return;
  }

  TextureDisplay texDisplay = m_RenderData.texDisplay;
  texDisplay.rawOutput = false;

  if(m_RenderData.texDisplay.overlay != DebugOverlay::NoOverlay && action)
  {
    if(m_OverlayDirty)
    {
      ReplayCapture(m_EventID, eReplay_WithoutDraw);
      m_pController->FatalErrorCheck();
      m_pController->ReapplyAnalysisEventOverrides(m_EventID);
      RefreshOverlay();
      m_pController->ReapplyAnalysisEventOverrides(m_EventID);
      ReplayCapture(m_EventID, eReplay_OnlyDraw);
      m_pController->FatalErrorCheck();
    }
  }
  else if(m_ForceOverlayRefresh)
  {
    m_ForceOverlayRefresh = false;
    if(m_pController->m_AnalysisEventOverridesActive)
      m_pController->ReplayFrameEventForAnalysis(m_EventID);
    else
      ReplayCapture(m_EventID, eReplay_Full);
    m_pController->FatalErrorCheck();
  }

  if(m_RenderData.texDisplay.customShaderId != ResourceId())
  {
    m_CustomShaderResourceId = m_pDevice->ApplyCustomShader(texDisplay);
    m_pController->FatalErrorCheck();

    texDisplay.resourceId = m_CustomShaderResourceId;
    texDisplay.typeCast = CompType::Typeless;
    texDisplay.customShaderId = ResourceId();
    texDisplay.subresource.slice = 0;

    m_CustomDirty = false;
  }

  FloatVector color;

  m_pDevice->BindOutputWindow(m_MainOutput.outputID, false);
  m_pDevice->ClearOutputWindowColor(m_MainOutput.outputID, color);

  ClearBackground(m_MainOutput.outputID, texDisplay.backgroundColor);

  // of the overlay isn't one that's applied while rendering the base texture - NaN/inf/-ve or
  // clipping - then don't try and render any overlay. This prevents underlying code from trying to
  // e.g. decode overlay data as a heatmap for quad overdraw/triangle size overlays
  if(texDisplay.overlay != DebugOverlay::NaN && texDisplay.overlay != DebugOverlay::Clipping)
  {
    texDisplay.overlay = DebugOverlay::NoOverlay;
  }

  m_pDevice->RenderTexture(texDisplay);
  m_pController->FatalErrorCheck();

  ResourceId id = m_RenderData.texDisplay.resourceId;

  if(m_RenderData.texDisplay.overlay != DebugOverlay::NoOverlay && action &&
     m_pDevice->IsRenderOutput(id) && m_RenderData.texDisplay.overlay != DebugOverlay::NaN &&
     m_RenderData.texDisplay.overlay != DebugOverlay::Clipping && m_OverlayResourceId != ResourceId())
  {
    texDisplay.resourceId = m_OverlayResourceId;
    texDisplay.red = texDisplay.green = texDisplay.blue = texDisplay.alpha = true;
    texDisplay.rawOutput = false;
    texDisplay.overlay = m_RenderData.texDisplay.overlay;
    texDisplay.customShaderId = ResourceId();
    texDisplay.scale = m_RenderData.texDisplay.scale;
    texDisplay.hdrMultiplier = -1.0f;
    texDisplay.flipY = m_RenderData.texDisplay.flipY;
    texDisplay.rangeMin = 0.0f;
    texDisplay.rangeMax = 1.0f;
    texDisplay.linearDisplayAsGamma = false;
    texDisplay.typeCast = CompType::Typeless;

    m_pDevice->RenderTexture(texDisplay);
    m_pController->FatalErrorCheck();
  }
}

void ReplayOutput::DisplayMesh()
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(!m_pController->EnsureMaterializedCurrentEvent(__FUNCTION__))
    return;

  ActionDescription *action = m_pController->GetActionByEID(m_EventID);

  if(action == NULL || m_MainOutput.outputID == 0 || m_Width <= 0 || m_Height <= 0 ||
     (m_RenderData.meshDisplay.position.vertexResourceId == ResourceId()) ||
     !(action->flags & (ActionFlags::MeshDispatch | ActionFlags::Drawcall)))
  {
    FloatVector color;
    m_pDevice->BindOutputWindow(m_MainOutput.outputID, false);
    m_pDevice->ClearOutputWindowColor(m_MainOutput.outputID, color);
    m_pDevice->ClearOutputWindowDepth(m_MainOutput.outputID, 1.0f, 0);
    m_pDevice->RenderCheckerboard(RenderDoc::Inst().DarkCheckerboardColor(),
                                  RenderDoc::Inst().LightCheckerboardColor());
    m_pController->FatalErrorCheck();

    return;
  }

  if(m_OverlayDirty)
  {
    ReplayCapture(m_EventID, eReplay_WithoutDraw);
    m_pController->FatalErrorCheck();
    m_pController->ReapplyAnalysisEventOverrides(m_EventID);
    RefreshOverlay();
    m_pController->ReapplyAnalysisEventOverrides(m_EventID);
    ReplayCapture(m_EventID, eReplay_OnlyDraw);
    m_pController->FatalErrorCheck();
  }

  m_pDevice->BindOutputWindow(m_MainOutput.outputID, true);
  m_pDevice->ClearOutputWindowDepth(m_MainOutput.outputID, 1.0f, 0);

  m_pDevice->RenderCheckerboard(RenderDoc::Inst().DarkCheckerboardColor(),
                                RenderDoc::Inst().LightCheckerboardColor());
  m_pController->FatalErrorCheck();

  m_pDevice->ClearOutputWindowDepth(m_MainOutput.outputID, 1.0f, 0);
  m_pController->FatalErrorCheck();

  MeshDisplay mesh = m_RenderData.meshDisplay;

  rdcarray<MeshFormat> secondaryDraws;

  // we choose a pallette here so that the colours stay consistent (i.e the
  // current action is always the same colour), but also to indicate somewhat
  // the relationship - ie. instances are closer in colour than other draws
  // in the pass

  // very slightly dark red
  FloatVector drawItself(0.06f, 0.0f, 0.0f, 1.0f);

  // more desaturated/lighter, but still reddish
  FloatVector otherInstances(0.18f, 0.1f, 0.1f, 1.0f);

  // lighter grey with blue tinge to contrast from main/instance draws
  FloatVector passDraws(0.2f, 0.2f, 0.25f, 1.0f);

  if(RenderDoc::Inst().IsDarkTheme())
  {
    drawItself = FloatVector(1.0f, 0.8f, 0.8f, 1.0f);
    otherInstances = FloatVector(0.78f, 0.6f, 0.6f, 1.0f);
    passDraws = FloatVector(0.4f, 0.4f, 0.45f, 1.0f);
  }

  if(m_RenderData.meshDisplay.type != MeshDataStage::VSIn)
  {
    for(size_t i = 0; m_RenderData.meshDisplay.showWholePass && i < passEvents.size(); i++)
    {
      ActionDescription *d = m_pController->GetActionByEID(passEvents[i]);

      if(d)
      {
        rdcarray<uint32_t> instIDs;
        instIDs.reserve(RDCMAX(1U, d->numInstances));
        for(uint32_t inst = 0; inst < RDCMAX(1U, d->numInstances); inst++)
          instIDs.push_back(inst);

        // pass MeshDataStage::Count to get the 'most final' stage
        secondaryDraws.append(m_pDevice->GetBatchPostVSBuffers(
            passEvents[i], instIDs, m_RenderData.meshDisplay.curView, MeshDataStage::Count));
        m_pController->FatalErrorCheck();
      }
    }

    // all secondary draws so far are previous pass elements
    for(MeshFormat &fmt : secondaryDraws)
      fmt.meshColor = passDraws;

    // action previous instances in the current action
    if(action->flags & ActionFlags::Instanced)
    {
      uint32_t maxInst = 0;
      if(m_RenderData.meshDisplay.showPrevInstances)
        maxInst = RDCMAX(1U, m_RenderData.meshDisplay.curInstance);
      if(m_RenderData.meshDisplay.showAllInstances)
        maxInst = RDCMAX(1U, action->numInstances);

      if(maxInst > 0)
      {
        rdcarray<uint32_t> instIDs;
        instIDs.reserve(maxInst);
        for(uint32_t inst = 0; inst < maxInst; inst++)
          instIDs.push_back(inst);

        size_t prevInstancesStart = secondaryDraws.size();

        // pass MeshDataStage::Count to get the 'most final' stage
        secondaryDraws.append(m_pDevice->GetBatchPostVSBuffers(
            action->eventId, instIDs, m_RenderData.meshDisplay.curView, MeshDataStage::Count));
        m_pController->FatalErrorCheck();

        // set the colour for other instances
        for(size_t i = prevInstancesStart; i < secondaryDraws.size(); i++)
          secondaryDraws[i].meshColor = otherInstances;
      }
    }

    // only if unproject is marked did this output have a 'real' system position output
    secondaryDraws.removeIf([](const MeshFormat &fmt) { return fmt.unproject == false; });
  }

  mesh.position.meshColor = drawItself;

  m_pDevice->RenderMesh(m_EventID, secondaryDraws, mesh, m_ComponentHighlight);
  m_pController->FatalErrorCheck();
}

#if ENABLED(ENABLE_UNIT_TESTS)

#include "catch/catch.hpp"

TEST_CASE("Historical fallback redraws its source after canvas or mutation changes",
          "[replay][history][presentation]")
{
  ReplayHistoryDisplaySource source;
  TextureDisplay historical;
  historical.resourceId = ResourceIDGen::GetNewUniqueID();
  historical.customShaderId = ResourceIDGen::GetNewUniqueID();
  historical.subresource = {2, 3, 1};
  historical.typeCast = CompType::UInt;
  historical.overlay = DebugOverlay::Drawcall;
  historical.decodeYUV = true;
  historical.rangeMin = 0.125f;
  historical.rangeMax = 0.875f;
  source.Publish(42, historical, 7, {674, 722}, true);
  REQUIRE(source.active);
  CHECK_FALSE(source.NeedsReplay(false, {674, 722}, 7));

  // Reproduce the tab's native resize: current output state was restored after drawing EID 42,
  // so the larger canvas must replay EID 42, not redraw that restored current configuration.
  CHECK(source.NeedsReplay(false, {674, 957}, 7));
  CHECK(source.NeedsReplay(true, {674, 722}, 7));
  CHECK(source.NeedsReplay(false, {674, 722}, 8));
  CHECK(source.eventId == 42);
  CHECK((source.display.resourceId == historical.resourceId));
  CHECK((source.display.customShaderId == historical.customShaderId));
  CHECK((source.display.subresource == historical.subresource));
  CHECK((source.display.typeCast == CompType::UInt));
  CHECK((source.display.overlay == DebugOverlay::Drawcall));
  CHECK(source.display.decodeYUV);
  CHECK(source.display.rangeMin == 0.125f);
  CHECK(source.display.rangeMax == 0.875f);

  // Source identity is copied at request completion, not borrowed from changing viewer controls.
  historical.resourceId = ResourceId();
  historical.rangeMax = 1.0f;
  CHECK((source.display.resourceId != ResourceId()));
  CHECK(source.display.rangeMax == 0.875f);
  const TextureDisplay redraw = source.display;
  source.Publish(source.eventId, redraw, 8, {674, 957}, true);
  CHECK_FALSE(source.NeedsReplay(false, {674, 957}, 8));
  CHECK(source.eventId == 42);
}

TEST_CASE("Live binding and failed historical draws cannot retain a fallback route",
          "[replay][history][presentation]")
{
  ReplayHistoryDisplaySource source;
  TextureDisplay historical;
  historical.resourceId = ResourceIDGen::GetNewUniqueID();
  source.Publish(42, historical, 7, {128, 72}, true);
  source.Clear();
  CHECK_FALSE(source.active);
  CHECK_FALSE(source.NeedsReplay(true, {640, 480}, 8));

  // Re-entering another historical source publishes that source, never the previous tab's EID.
  const ResourceId nextResource = ResourceIDGen::GetNewUniqueID();
  historical.resourceId = nextResource;
  source.Publish(84, historical, 8, {640, 480}, true);
  CHECK(source.active);
  CHECK(source.eventId == 84);
  CHECK((source.display.resourceId == nextResource));
  source.Publish(100, historical, 9, {640, 480}, false);
  CHECK_FALSE(source.active);
  CHECK_FALSE(source.NeedsReplay(true, {640, 480}, 10));
}

#endif
