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

#include "replay_controller.h"
#include <string.h>
#include <time.h>
#include <algorithm>
#include <atomic>
#include <unordered_set>
#include "common/dds_readwrite.h"
#include "common/timing.h"
#include "driver/ihv/amd/amd_isa.h"
#include "driver/ihv/amd/amd_rgp.h"
#include "jpeg-compressor/jpgd.h"
#include "jpeg-compressor/jpge.h"
#include "maths/formatpacking.h"
#include "os/os_specific.h"
#include "replay/replay_timing.h"
#include "serialise/rdcfile.h"
#include "serialise/serialiser.h"
#include "stb/stb_image.h"
#include "stb/stb_image_write.h"
#include "strings/string_utils.h"
#include "tinyexr/tinyexr.h"

static void fileWriteFunc(void *context, void *data, int size)
{
  FileIO::fwrite(data, 1, size, (FILE *)context);
}

static uint64_t NextRenderOutputMutationGeneration()
{
  // Cache entries deliberately survive ReplayController destruction. A process-wide serial keeps
  // two controllers' first render-output mutations from both being labelled generation 2 and
  // reusing stale images after the same capture is reopened. The historical function name is kept
  // because it is part of the public API.
  static std::atomic<uint64_t> processGeneration(1);
  return processGeneration.fetch_add(1, std::memory_order_relaxed) + 1;
}

struct ScopedCheckpointAnalysisBypass
{
  explicit ScopedCheckpointAnalysisBypass(IReplayDriver *&driver)
      : device(driver),
        previous(driver->m_ReplayCheckpointBypass),
        previousNaturalReference(driver->m_ReplayCheckpointNaturalReference),
        previousPrefixOnly(driver->m_ReplayCheckpointPrefixOnly),
        previousBufferMutation(driver->m_ReplayBufferMutationRoot),
        previousShaderMutation(driver->m_ReplayShaderMutationRoot)
  {
    device->m_ReplayCheckpointBypass = true;
    device->m_ReplayCheckpointNaturalReference = false;
    device->m_ReplayCheckpointPrefixOnly = false;
    device->m_ReplayBufferMutationRoot = false;
    device->m_ReplayShaderMutationRoot = false;
  }
  ~ScopedCheckpointAnalysisBypass()
  {
    // FatalErrorCheck can replace the driver with a dummy while the scope is active.
    device->m_ReplayCheckpointBypass = previous;
    device->m_ReplayCheckpointNaturalReference = previousNaturalReference;
    device->m_ReplayCheckpointPrefixOnly = previousPrefixOnly;
    device->m_ReplayBufferMutationRoot = previousBufferMutation;
    device->m_ReplayShaderMutationRoot = previousShaderMutation;
  }
  IReplayDriver *&device;
  bool previous;
  bool previousNaturalReference;
  bool previousPrefixOnly;
  bool previousBufferMutation;
  bool previousShaderMutation;
};

struct ScopedCheckedShaderReplay
{
  explicit ScopedCheckedShaderReplay(IReplayDriver *&driver)
      : device(driver), previous(driver->m_ReplayShaderMutationRoot)
  {
    device->m_ReplayShaderMutationRoot = true;
  }
  ~ScopedCheckedShaderReplay() { device->m_ReplayShaderMutationRoot = previous; }
  IReplayDriver *&device;
  bool previous;
};

struct ScopedCheckpointPrefixOnly
{
  ScopedCheckpointPrefixOnly(IReplayDriver *&driver, bool value)
      : device(driver), previous(driver->m_ReplayCheckpointPrefixOnly)
  {
    device->m_ReplayCheckpointPrefixOnly = value;
  }
  ~ScopedCheckpointPrefixOnly()
  {
    // Restore through the controller's current pointer if a fatal error installed a dummy.
    device->m_ReplayCheckpointPrefixOnly = previous;
  }
  IReplayDriver *&device;
  bool previous;
};

struct ScopedSharedBufferPatchReplay
{
  ScopedSharedBufferPatchReplay(IReplayDriver *&driver, bool &sharedReplay)
      : prefix(driver, true),
        replay(sharedReplay),
        previous(sharedReplay),
        device(driver),
        previousBufferMutation(driver->m_ReplayBufferMutationRoot)
  {
    replay = true;
    device->m_ReplayBufferMutationRoot = true;
  }
  ~ScopedSharedBufferPatchReplay()
  {
    replay = previous;
    device->m_ReplayBufferMutationRoot = previousBufferMutation;
  }
  ScopedCheckpointPrefixOnly prefix;
  bool &replay;
  bool previous;
  IReplayDriver *&device;
  bool previousBufferMutation;
};

ReplayController::ReplayController()
{
  m_ThreadID = Threading::GetCurrentID();

  m_pDevice = NULL;

  m_EventID = 100000;

  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(ReplayController));
}

ReplayController::~ReplayController()
{
  RenderDoc::Inst().UnregisterMemoryRegion(this);
  CHECK_REPLAY_THREAD();
}

void ReplayController::SetFrameEvent(uint32_t eventId, bool force)
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();

  // use remapped event if there's a match
  auto it = m_EventRemap.find(eventId);
  if(it != m_EventRemap.end())
    eventId = it->second;

  const bool pipelineMutationChanged =
      m_PipelineStateValid &&
      (m_PipelineStateShaderGeneration != GetShaderReplacementGeneration(eventId) ||
       m_PipelineStateBufferGeneration != m_SharedBufferPatchGeneration);
  if(eventId != m_EventID || force || !IsCurrentEventMaterialized() || pipelineMutationChanged)
  {
    InvalidateScrubReplay();
    m_EventID = eventId;
    const bool previousBypass = m_pDevice->m_ReplayCheckpointBypass;
    const bool previousReference = m_pDevice->m_ReplayCheckpointNaturalReference;
    ScopedCheckpointPrefixOnly prefix(m_pDevice,
                                      force ? false : m_pDevice->m_ReplayCheckpointPrefixOnly);
    m_pDevice->m_ReplayCheckpointBypass = previousBypass || force;
    if(force)
      m_pDevice->m_ReplayCheckpointNaturalReference = false;
    ReplayEvent(eventId, true, true);
    m_pDevice->m_ReplayCheckpointBypass = previousBypass;
    m_pDevice->m_ReplayCheckpointNaturalReference = previousReference;
  }
  else
  {
    if(!m_PipelineStateValid || m_PipelineStateEventID != eventId)
      FetchPipelineState(eventId);
    // effectiveEID didn't change but selectedEID did (e.g. switching between
    // a MultiAction parent and its last child). refresh overlays only.
    if(m_SelectedEventID != m_PrevSelectedEventID)
      for(size_t i = 0; i < m_Outputs.size(); i++)
        m_Outputs[i]->SetFrameEvent(eventId);
  }

  // Explicit history snapshots may have retired GPU resources. Reap them after event selection,
  // outside presentation-only zoom/range changes.
  ReapRetiredEventTextureSnapshots();
}

uint32_t ReplayController::GetCurrentEventID()
{
  CHECK_REPLAY_THREAD();
  return m_EventID;
}

bool ReplayController::EIDTimingEnabled()
{
  // Cached once per process. Set RENDERDOC_EID_TIMING=1 to log per-event replay timing via
  // RDCLOG (visible in the Diagnostic Log window). i18n-ignore: diagnostic env var + log tag.
  static int enabled = -1;
  if(enabled < 0)
    enabled = Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1" ? 1 : 0;
  return enabled != 0;
}

void ReplayController::ScrubFrameEvent(uint32_t eventId)
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();

  // Use the same effective EID as ordinary event selection.
  auto it = m_EventRemap.find(eventId);
  if(it != m_EventRemap.end())
    eventId = it->second;

  ReplayScrubFrameEvent(eventId);
}

void ReplayController::ReplayScrubFrameEvent(uint32_t eventId)
{
  CHECK_REPLAY_THREAD();

  InvalidateScrubReplay();
  m_EventID = eventId;

  const bool timing = EIDTimingEnabled();
  PerformanceTimer timer;

  // Keep the canonical WithoutDraw/OnlyDraw sequence on every API. OnlyDraw may perform shader
  // feedback instead of the target action, so Full followed by SavePipelineState is not generally
  // equivalent: feedback could execute a UAV-writing action a second time. This path also retains
  // the normal event-scoped CBuffer and shader override validation and restoration.
  // Do not prepare or present outputs with their previous event's resource bindings. The caller
  // commits the live pipeline state, updates viewer bindings, then presents the correct output.
  ReplayEvent(eventId, false, false, false);

  if(m_FatalError == ResultCode::Succeeded && m_MaterializedEventValid &&
     m_MaterializedEventID == eventId)
  {
    m_ScrubReplayEventID = eventId;
    m_ScrubReplayValid = true;

    for(ReplayOutput *output : m_Outputs)
      output->SetScrubFrameEvent(eventId);
  }

  if(timing)
  {
    const double replayMS = timer.GetMilliseconds();
    const rdcstr line = StringFormat::Fmt(
        "api=ScrubReplay eid=%u replay_ms=%.3f outputPresent_ms=0.000 total_ms=%.3f", eventId,
        replayMS, replayMS);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

bool ReplayController::EnsureMaterializedCurrentEvent(const char *reason)
{
  CHECK_REPLAY_THREAD();

  if(IsCurrentEventMaterialized())
    return true;

  const uint32_t previousMaterializedEvent = m_MaterializedEventID;
  const bool timing = EIDTimingEnabled();
  PerformanceTimer timer;

  // Analysis and overlay work can leave the replay device at a different event or phase.
  RestoreAnalysisEventOverrides();
  InvalidateScrubReplay();

  ReplayEvent(m_EventID, false, false, false);
  if(m_FatalError != ResultCode::Succeeded)
    return false;

  ReapRetiredEventTextureSnapshots();
  const bool materialized = m_MaterializedEventValid && m_MaterializedEventID == m_EventID;
  if(timing)
  {
    const rdcstr line =
        StringFormat::Fmt("api=MaterializeEvent eid=%u previous=%u reason=%s total_ms=%.3f ok=%u",
                          m_EventID, previousMaterializedEvent, reason ? reason : "unspecified",
                          timer.GetMilliseconds(), materialized ? 1U : 0U);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return materialized;
}

ReplayController::ExplicitResourceReadSource ReplayController::GetExplicitResourceReadSource() const
{
  if(m_FatalError != ResultCode::Succeeded ||
     m_AnalysisReplayState == AnalysisReplayState::PendingOrFailed)
    return ExplicitResourceReadSource::Unavailable;
  if(m_AnalysisReplayState == AnalysisReplayState::Ready)
    return ExplicitResourceReadSource::AnalysisReplay;
  return ExplicitResourceReadSource::CurrentEvent;
}

bool ReplayController::EnsureExplicitResourceRead(const char *reason)
{
  CHECK_REPLAY_THREAD();
  switch(GetExplicitResourceReadSource())
  {
    case ExplicitResourceReadSource::AnalysisReplay: return true;
    case ExplicitResourceReadSource::Unavailable: return false;
    case ExplicitResourceReadSource::CurrentEvent: return EnsureMaterializedCurrentEvent(reason);
  }
  return false;
}

double ReplayController::ReplayDriverBoundary(uint32_t eventId, ReplayLogType replayType)
{
  const bool timing = EIDTimingEnabled();
  const bool prefixOnly =
      m_pDevice->m_ReplayCheckpointPrefixOnly && !m_pDevice->m_ReplayCheckpointBypass;
  if(!timing && !m_CheckpointAutomatic)
  {
    m_pDevice->ReplayLog(eventId, replayType);
    return 0.0;
  }
  PerformanceTimer timer;
  m_pDevice->ReplayLog(eventId, replayType);
  const double elapsed = timer.GetMilliseconds();
  if(!timing)
    return elapsed;
  const char *type = replayType == eReplay_Full          ? "Full"
                     : replayType == eReplay_WithoutDraw ? "WithoutDraw"
                                                         : "OnlyDraw";
  // This outer wall-time scope includes driver epilogues/feedback beyond the native ReplayLog
  // scope. It is independent diagnostic evidence and must not be added to that nested scope.
  const rdcstr line =
      StringFormat::Fmt("api=ReplayDriverBoundary type=%s eid=%u total_ms=%.3f prefix_only=%u",
                        type, eventId, elapsed, prefixOnly ? 1U : 0U);
  RDCLOG("[EIDTiming] %s", line.c_str());
  AppendReplayTimingLog(line);
  return elapsed;
}

void ReplayController::CommitScrubFrameEvent(uint32_t eventId)
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();

  auto it = m_EventRemap.find(eventId);
  if(it != m_EventRemap.end())
    eventId = it->second;

  const bool timing = EIDTimingEnabled();
  PerformanceTimer timer;

  // A commit publishes actual replay state. Direct callers, mutations and intervening analysis
  // may require a replay, but the normal ScrubFrameEvent/CommitScrubFrameEvent pair executes the
  // target only once. Never publish cached pipeline state from a previous visit to this EID.
  const bool canCommitWithoutReplay =
      m_ScrubReplayValid && m_AnalysisReplayState == AnalysisReplayState::None &&
      !m_AnalysisEventOverridesActive && m_MaterializedEventValid &&
      eventId == m_ScrubReplayEventID && eventId == m_MaterializedEventID && eventId == m_EventID;
  if(!canCommitWithoutReplay)
    ReplayScrubFrameEvent(eventId);

  const double replayMS = timing ? timer.GetMilliseconds() : 0.0;
  if(m_FatalError != ResultCode::Succeeded || !m_MaterializedEventValid ||
     eventId != m_MaterializedEventID)
    return;

  FetchPipelineState(eventId);
  ReapRetiredEventTextureSnapshots();

  if(timing)
  {
    const rdcstr line = StringFormat::Fmt("api=ScrubCommit eid=%u pipeline_ms=%.3f replay_ms=%.3f",
                                          eventId, timer.GetMilliseconds() - replayMS,
                                          canCommitWithoutReplay ? 0.0 : replayMS);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

bool ReplayController::HasEventOverrides(uint32_t eventId) const
{
  for(const auto &replacement : m_EventShaderReplacements)
    if(replacement.first.first == eventId)
      return true;

  for(const EventCBufferOverrideKey &overrideKey : m_EventCBufferOverrides)
    if(overrideKey.eventId == eventId)
      return true;

  return false;
}

void ReplayController::ReplayFrameEventForAnalysis(uint32_t eventId)
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();
  ScopedCheckpointAnalysisBypass checkpointBypass(m_pDevice);

  // Match SetFrameEvent's public EID semantics, including MultiAction remapping, but deliberately
  // leave m_EventID, the cached pipeline states, and every ReplayOutput untouched. ReplayManager
  // serialises controller calls, so a caller can inspect resource contents and then restore the
  // effective event without exposing the transient state to qrenderdoc.
  auto it = m_EventRemap.find(eventId);
  if(it != m_EventRemap.end())
    eventId = it->second;

  RestoreAnalysisEventOverrides();
  InvalidateScrubReplay();
  m_MaterializedEventValid = false;
  m_AnalysisReplayState = AnalysisReplayState::PendingOrFailed;
  m_AnalysisReplayEventID = eventId;
  m_LastEventOverrideError.clear();

  // Captures without an event-scoped edit do not need the preparatory WithoutDraw + OnlyDraw
  // sequence. A Full replay produces the same resource contents in one pass and, unlike the normal
  // SetFrameEvent path, there are no ReplayOutputs to refresh between the two phases here.
  if(!HasEventOverrides(eventId))
  {
    ReplayDriverBoundary(eventId, eReplay_Full);
    FatalErrorCheck();
    if(m_FatalError == ResultCode::Succeeded)
      m_AnalysisReplayState = AnalysisReplayState::Ready;
    return;
  }

  ReplayDriverBoundary(eventId, eReplay_WithoutDraw);
  FatalErrorCheck();
  if(m_FatalError != ResultCode::Succeeded)
    return;

  if(!ApplyAnalysisEventOverrides(eventId))
    return;

  ReplayDriverBoundary(eventId, eReplay_OnlyDraw);
  FatalErrorCheck();
  if(m_FatalError == ResultCode::Succeeded)
    m_AnalysisReplayState = AnalysisReplayState::Ready;
}

void ReplayController::RestoreFrameEventAfterAnalysis()
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();

  // m_EventID is the effective event. In particular it can differ from m_SelectedEventID for
  // markers and MultiActions, so restoring the selected event would desynchronise device contents
  // from the cached pipeline state and replay outputs.
  RestoreAnalysisEventOverrides();
  ReplayEvent(m_EventID, false, false);
}

namespace
{
rdcstr ValidateShaderPairSnapshots(const ShaderPairComparison &result)
{
  if(!result.supported || result.outputs.empty() || result.outputs.size() > 9)
    return "Single-prefix comparison did not return a bounded complete output set";
  uint64_t bytesPerLeg = 0;
  std::set<std::pair<bool, uint32_t>> slots;
  for(const ShaderPairSnapshot &output : result.outputs)
  {
    if(output.resourceId == ResourceId() || output.width == 0 || output.height == 0 ||
       output.width > 8192 || output.height > 8192 || output.subresource.mip != 0 ||
       output.subresource.slice != 0 || output.subresource.sample != 0 ||
       (output.depth ? output.index != 0 : output.index >= 8) ||
       !slots.insert({output.depth, output.index}).second)
      return "Single-prefix comparison returned an invalid output identity or subresource";
    if(output.baseline.empty() || output.baseline.size() > 32ULL * 1024 * 1024 ||
       output.baseline.size() != output.candidate.size() ||
       output.baseline.size() != output.baselineRepeat.size())
      return "Single-prefix comparison returned incomplete or oversized output bytes";
    // D3D12 GetTextureData preserves D32S8's padded eight-byte native pixels; ElementSize
    // describes a tightly packed five-byte pixel and must not be used for this readback.
    const uint32_t pixelBytes =
        output.format.type == ResourceFormatType::D32S8 ? 8 : output.format.ElementSize();
    if(output.format.type == ResourceFormatType::Undefined || output.format.BlockFormat() ||
       pixelBytes == 0 ||
       uint64_t(output.width) * output.height * pixelBytes != output.baseline.size())
      return "Single-prefix comparison returned a format inconsistent with its output bytes";
    bytesPerLeg += output.baseline.size();
    if(bytesPerLeg > 64ULL * 1024 * 1024)
      return "Single-prefix comparison exceeds its aggregate output byte limit";
  }
  return {};
}
}

ShaderPairComparison ReplayController::ComparePixelShaderPair(uint32_t eventId,
                                                              ResourceId expectedOriginal,
                                                              ResourceId baseline,
                                                              ResourceId candidate)
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();
  auto remap = m_EventRemap.find(eventId);
  if(remap != m_EventRemap.end())
    eventId = remap->second;
  ShaderPairComparison result;
  result.eventId = eventId;
  if(!m_pDevice || m_pDevice->IsRemoteProxy())
  {
    result.reason = "Single-prefix pixel Shader comparison requires a local replay driver";
    return result;
  }
  if(eventId == 0 || expectedOriginal == ResourceId())
  {
    result.reason = "Single-prefix comparison requires a draw and the original Shader resource";
    return result;
  }
  if(baseline == ResourceId())
    baseline = expectedOriginal;
  if(candidate == ResourceId())
    candidate = expectedOriginal;
  if((baseline != expectedOriginal && m_TargetResources.find(baseline) == m_TargetResources.end()) ||
     (candidate != expectedOriginal && m_TargetResources.find(candidate) == m_TargetResources.end()))
  {
    result.reason = "Comparison baseline or candidate was not built on this replay controller";
    return result;
  }
  if(HasEventOverrides(eventId) ||
     m_GlobalShaderReplacements.find(expectedOriginal) != m_GlobalShaderReplacements.end())
  {
    result.reason =
        "Single-prefix comparison requires the original draw without Shader or CBuffer overrides";
    return result;
  }

  ScopedCheckpointAnalysisBypass checkpointBypass(m_pDevice);
  RestoreAnalysisEventOverrides();
  InvalidateScrubReplay();
  m_MaterializedEventValid = false;
  m_AnalysisReplayState = AnalysisReplayState::PendingOrFailed;
  m_AnalysisReplayEventID = eventId;
  m_LastEventOverrideError.clear();
  result = m_pDevice->ComparePixelShaderPair(eventId, expectedOriginal, baseline, candidate);
  FatalErrorCheck();
  if(m_FatalError != ResultCode::Succeeded)
  {
    result.completed = false;
    result.reason = "Replay driver failed during single-prefix pixel Shader comparison";
  }
  if(result.completed)
  {
    const rdcstr error = result.eventId != eventId
                             ? rdcstr("Single-prefix comparison returned a different event")
                             : ValidateShaderPairSnapshots(result);
    if(error.empty())
      m_AnalysisReplayState = AnalysisReplayState::Ready;
    else
    {
      result.completed = false;
      result.reason = error;
    }
  }
  if(!result.completed)
  {
    result.outputs.clear();
    if(result.reason.empty())
      result.reason = "Single-prefix pixel Shader comparison did not complete";
  }
  return result;
}

bool ReplayController::ApplyAnalysisEventOverrides(uint32_t eventId)
{
  auto remapIt = m_EventRemap.find(eventId);
  if(remapIt != m_EventRemap.end())
    eventId = remapIt->second;

  m_LastEventOverrideError = m_pDevice->ApplyEventCBufferOverrides(eventId);
  if(m_LastEventOverrideError.empty())
    m_LastEventOverrideError = m_pDevice->ApplyEventShaderOverrides(eventId);

  if(!m_LastEventOverrideError.empty())
  {
    m_pDevice->RestoreEventShaderOverrides();
    m_pDevice->RestoreEventCBufferOverrides();
    RDCERR("Event-scoped override was not applied at EID %u: %s", eventId,
           m_LastEventOverrideError.c_str());
    return false;
  }

  // Keep both layers installed until RestoreFrameEventAfterAnalysis. Overlay generation and other
  // driver-internal replays performed by the analysis operation must see the same shader and
  // CBuffer state as the draw that produced the inspected render target.
  m_AnalysisEventOverridesActive = true;
  return true;
}

void ReplayController::ReapplyAnalysisEventOverrides(uint32_t eventId)
{
  if(!m_AnalysisEventOverridesActive)
    return;

  // ReplayOutput's overlay paths issue another WithoutDraw replay after the analysis draw. That
  // replay restores captured bindings, so install the event layers again before its final draw.
  const bool hadAnalysis = m_AnalysisReplayState != AnalysisReplayState::None;
  RestoreAnalysisEventOverrides();
  if(hadAnalysis)
  {
    // An internal output replay is not a completed public analysis. In particular, losing Ready
    // here must not silently turn an explicit readback into a canonical current-event replay.
    m_AnalysisReplayState = AnalysisReplayState::PendingOrFailed;
    m_AnalysisReplayEventID = eventId;
  }
  ApplyAnalysisEventOverrides(eventId);
}

void ReplayController::RestoreAnalysisEventOverrides()
{
  m_AnalysisReplayState = AnalysisReplayState::None;
  m_AnalysisReplayEventID = 0;
  if(!m_AnalysisEventOverridesActive)
    return;

  m_pDevice->RestoreEventShaderOverrides();
  m_pDevice->RestoreEventCBufferOverrides();
  m_AnalysisEventOverridesActive = false;
}

void ReplayController::EndAnalysisForMutation()
{
  if(m_AnalysisReplayState == AnalysisReplayState::None && !m_AnalysisEventOverridesActive)
    return;
  RestoreAnalysisEventOverrides();
  // A mutation invalidates the analysis even if its ordinary API does not replay the current
  // event. Do not silently replace a later explicit read with unrelated canonical pixels.
  m_AnalysisReplayState = AnalysisReplayState::PendingOrFailed;
  m_MaterializedEventValid = false;
}

uint64_t ReplayController::GetShaderReplacementGeneration(uint32_t eventId)
{
  CHECK_REPLAY_THREAD();

  if(eventId == 0)
    return m_ShaderReplacementSerial;

  auto remapIt = m_EventRemap.find(eventId);
  if(remapIt != m_EventRemap.end())
    eventId = remapIt->second;

  uint64_t generation = m_GlobalShaderReplacementGeneration;
  auto eventIt = m_EventShaderReplacementGenerations.find(eventId);
  if(eventIt != m_EventShaderReplacementGenerations.end())
    generation = RDCMAX(generation, eventIt->second);

  return generation;
}

bool ReplayController::IsShaderResource(ResourceId id) const
{
  for(const ResourceDescription &resource : m_Resources)
  {
    if(resource.resourceId == id)
      return resource.type == ResourceType::Shader || resource.type == ResourceType::ShaderBinding;
  }

  return false;
}

void ReplayController::MarkGlobalShaderReplacementChanged()
{
  // Global shader/resource replacement can affect any prefix. Without a complete shader-to-
  // byte-range dependency index, retaining a later resource-state checkpoint would be unsafe.
  m_pDevice->InvalidateReplayCheckpoints(0);
  ResetAutomaticCheckpointCosts(0);
  m_GlobalShaderReplacementGeneration = NextRenderOutputMutationGeneration();
  m_ShaderReplacementSerial = m_GlobalShaderReplacementGeneration;
}

void ReplayController::MarkEventShaderReplacementChanged(uint32_t eventId)
{
  m_pDevice->InvalidateReplayCheckpoints(eventId);
  ResetAutomaticCheckpointCosts(eventId);
  m_EventShaderReplacementGenerations[eventId] = NextRenderOutputMutationGeneration();
  m_ShaderReplacementSerial = m_EventShaderReplacementGenerations[eventId];
}

void ReplayController::ReplayEvent(uint32_t eventId, bool updateOutputs, bool fetchPipelineState,
                                   bool allowFullReplay)
{
  const bool timing = EIDTimingEnabled();
  PerformanceTimer eidTimer;

  // Be defensive if an analysis caller exits early without its documented restore call.
  RestoreAnalysisEventOverrides();
  // A forward visit may continue the already materialized native state. Do not turn it into
  // a full-prefix replay merely to collect checkpoint cost evidence: it is a different path,
  // and its cheap result is not a comparable checkpoint/reference timing sample either.
  const bool forwardFromMaterialized =
      m_MaterializedEventValid && eventId > m_MaterializedEventID && !m_SharedBufferPatchReplay;
  m_MaterializedEventValid = false;
  m_PipelineStateValid = false;

  // Restoring the controller's event after a read-only analysis has no outputs to refresh between
  // phases. With no event-scoped edit, one Full replay is equivalent to the generic two-pass path
  // and avoids replaying the frame twice for every history snapshot.
  if(allowFullReplay && !updateOutputs && !fetchPipelineState && !HasEventOverrides(eventId))
  {
    m_LastEventOverrideError.clear();
    ReplayDriverBoundary(eventId, eReplay_Full);
    FatalErrorCheck();

    if(m_FatalError == ResultCode::Succeeded)
    {
      m_MaterializedEventID = eventId;
      m_MaterializedEventValid = true;
    }

    const double replayMS = timing ? eidTimer.GetMilliseconds() : 0.0;
    if(fetchPipelineState)
      FetchPipelineState(eventId);

    if(timing)
      RDCLOG("[EIDTiming] analysis restore EID %u: replay(1-pass) %.2fms, pipelineState %.2fms",
             eventId, replayMS, fetchPipelineState ? eidTimer.GetMilliseconds() - replayMS : 0.0);
    return;
  }

  const AutomaticCheckpointRequest checkpointRequest =
      forwardFromMaterialized
          ? AutomaticCheckpointRequest()
          : BeginAutomaticCheckpointRequest(eventId, (updateOutputs ? 1U : 0U) |
                                                         (fetchPipelineState ? 2U : 0U) |
                                                         (m_SharedBufferPatchReplay ? 4U : 0U));
  double driverMS = ReplayDriverBoundary(eventId, eReplay_WithoutDraw);
  FatalErrorCheck();

  if(updateOutputs)
    for(size_t i = 0; i < m_Outputs.size(); i++)
      m_Outputs[i]->SetFrameEvent(eventId);

  // CBuffer shadows are installed against the captured shader first. Shader replacement is
  // layered on top only after every CBuffer binding has passed its fail-closed validation.
  m_LastEventOverrideError = m_pDevice->ApplyEventCBufferOverrides(eventId);
  if(m_LastEventOverrideError.empty())
    m_LastEventOverrideError = m_pDevice->ApplyEventShaderOverrides(eventId);

  if(!m_LastEventOverrideError.empty())
  {
    // Apply methods validate atomically, but restore both layers here as a final rollback guard
    // before replaying the original action.
    m_pDevice->RestoreEventShaderOverrides();
    m_pDevice->RestoreEventCBufferOverrides();
    RDCERR("Event-scoped override was not applied at EID %u: %s", eventId,
           m_LastEventOverrideError.c_str());
  }

  const bool previousCheckpointBypass = m_pDevice->m_ReplayCheckpointBypass;
  // A single-event shadow is not part of other events' canonical prefixes. Never checkpoint its
  // transient bindings or outputs as a resource version for later unmodified event selection.
  // An ordinary unmodified target must retain its native write lineage so unchanged upstream
  // versions remain usable after this draw. Preserve any outer force/analysis bypass.
  m_pDevice->m_ReplayCheckpointBypass =
      previousCheckpointBypass || HasEventOverrides(eventId) || !m_LastEventOverrideError.empty();
  {
    ScopedCheckpointPrefixOnly prefix(m_pDevice, false);
    driverMS += ReplayDriverBoundary(eventId, eReplay_OnlyDraw);
  }
  m_pDevice->m_ReplayCheckpointBypass = previousCheckpointBypass;

  // Always restore before checking for a fatal error, since FatalErrorCheck may swap the replay
  // driver for a dummy implementation.
  m_pDevice->RestoreEventShaderOverrides();
  m_pDevice->RestoreEventCBufferOverrides();
  FatalErrorCheck();

  EndAutomaticCheckpointRequest(checkpointRequest, driverMS);

  if(m_FatalError == ResultCode::Succeeded)
  {
    m_MaterializedEventID = eventId;
    m_MaterializedEventValid = true;
  }

  const double replayMS = timing ? eidTimer.GetMilliseconds() : 0.0;

  if(fetchPipelineState)
    FetchPipelineState(eventId);

  if(timing)
    RDCLOG("[EIDTiming] settle EID %u: replay(2-pass)+outputs %.2fms, pipelineState %.2fms",
           eventId, replayMS, fetchPipelineState ? eidTimer.GetMilliseconds() - replayMS : 0.0);
}

rdcstr ReplayController::SetEventShaderOverride(uint32_t eventId, ShaderStage stage,
                                                ResourceId expectedOriginal, ResourceId replacement,
                                                uint32_t outputTarget, bool independentFloatOutput)
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  if(m_pDevice == NULL)
    return "No replay driver is available";

  const auto fatalMessage = [this]() {
    return m_FatalError.message.empty() ? ToStr(m_FatalError.code)
                                        : rdcstr(m_FatalError.message.c_str());
  };
  if(m_FatalError != ResultCode::Succeeded)
    return fatalMessage();

  if(expectedOriginal == ResourceId() || replacement == ResourceId())
    return "Both expectedOriginal and replacement must be non-null shader resources";

  if(independentFloatOutput && (stage != ShaderStage::Pixel || outputTarget == ~0U))
    return "Independent float preview requires an explicit pixel color target";

  if(m_TargetResources.find(replacement) == m_TargetResources.end())
    return "replacement was not created by BuildTargetShader on this replay controller";

  auto remapIt = m_EventRemap.find(eventId);
  if(remapIt != m_EventRemap.end())
    eventId = remapIt->second;

  if(eventId != m_EventID)
    return "eventId must be the currently selected effective event before configuring an override";

  const std::pair<uint32_t, ShaderStage> replacementKey = std::make_pair(eventId, stage);
  const auto priorIt = m_EventShaderReplacements.find(replacementKey);
  const bool hadPrior = priorIt != m_EventShaderReplacements.end();
  EventShaderReplacement prior;
  if(hadPrior)
    prior = priorIt->second;

  rdcstr error = m_pDevice->SetEventShaderOverride(eventId, stage, expectedOriginal, replacement,
                                                   outputTarget, independentFloatOutput);
  FatalErrorCheck();

  if(m_FatalError != ResultCode::Succeeded)
    return fatalMessage();
  if(!error.empty())
    return error;

  // Register before replay so the very first debug draw cannot become a canonical checkpoint.
  const bool changed = !hadPrior || prior.original != expectedOriginal ||
                       prior.replacement != replacement || prior.outputTarget != outputTarget ||
                       prior.independentFloatOutput != independentFloatOutput;
  m_EventShaderReplacements[replacementKey] = {expectedOriginal, replacement, outputTarget,
                                               independentFloatOutput};
  if(changed)
    MarkEventShaderReplacementChanged(eventId);

  if(eventId == m_EventID)
  {
    InvalidateScrubReplay();
    ReplayEvent(m_EventID, true, true);
    if(m_FatalError != ResultCode::Succeeded)
      return fatalMessage();

    // Registration performs structural validation, while ApplyEventShaderOverrides validates the
    // exact live binding after replaying WithoutDraw. Never report success if that final guard
    // rejected the override.
    if(!m_LastEventOverrideError.empty())
    {
      rdcstr applyError = m_LastEventOverrideError;
      m_pDevice->ClearEventShaderOverride(eventId, stage);
      FatalErrorCheck();
      if(m_FatalError != ResultCode::Succeeded)
        return fatalMessage();

      bool restoredPrior = false;
      if(hadPrior)
      {
        rdcstr restoreError =
            m_pDevice->SetEventShaderOverride(eventId, stage, prior.original, prior.replacement,
                                              prior.outputTarget, prior.independentFloatOutput);
        FatalErrorCheck();
        if(m_FatalError != ResultCode::Succeeded)
          return fatalMessage();
        if(restoreError.empty())
        {
          m_EventShaderReplacements[replacementKey] = prior;
          MarkEventShaderReplacementChanged(eventId);
          InvalidateScrubReplay();
          ReplayEvent(m_EventID, true, true);
          if(m_FatalError != ResultCode::Succeeded)
            return fatalMessage();
          restoredPrior = m_LastEventOverrideError.empty();
        }

        if(!restoredPrior)
        {
          m_pDevice->ClearEventShaderOverride(eventId, stage);
          FatalErrorCheck();
          if(m_FatalError != ResultCode::Succeeded)
            return fatalMessage();
          m_EventShaderReplacements.erase(replacementKey);
          MarkEventShaderReplacementChanged(eventId);
        }
      }

      if(!hadPrior || !restoredPrior)
      {
        m_EventShaderReplacements.erase(replacementKey);
        MarkEventShaderReplacementChanged(eventId);
        InvalidateScrubReplay();
        ReplayEvent(m_EventID, true, true);
        if(m_FatalError != ResultCode::Succeeded)
          return fatalMessage();
      }
      return applyError;
    }
  }

  // The selected event generation was published before replay and before presenting outputs.
  for(size_t i = 0; i < m_Outputs.size(); i++)
    if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
      m_Outputs[i]->Display();

  return {};
}

EventShaderOverrideState ReplayController::GetEventShaderOverride(uint32_t eventId, ShaderStage stage)
{
  CHECK_REPLAY_THREAD();
  auto remap = m_EventRemap.find(eventId);
  if(remap != m_EventRemap.end())
    eventId = remap->second;
  EventShaderOverrideState result;
  result.eventId = eventId;
  result.stage = stage;
  auto found = m_EventShaderReplacements.find({eventId, stage});
  if(found != m_EventShaderReplacements.end())
  {
    result.active = true;
    result.expectedOriginal = found->second.original;
    result.replacement = found->second.replacement;
    result.outputTarget = found->second.outputTarget;
    result.independentFloatOutput = found->second.independentFloatOutput;
  }
  return result;
}

TextureDescription ReplayController::GetEventShaderPreviewTexture(uint32_t eventId, ShaderStage stage)
{
  CHECK_REPLAY_THREAD();
  const EventShaderOverrideState current = GetEventShaderOverride(eventId, stage);
  if(!m_pDevice || m_FatalError != ResultCode::Succeeded || !current.active ||
     !current.independentFloatOutput)
    return {};
  TextureDescription result = m_pDevice->GetEventShaderPreviewTexture(current.eventId, stage);
  FatalErrorCheck();
  if(m_FatalError != ResultCode::Succeeded)
    return {};
  return result;
}

void ReplayController::ClearEventShaderOverride(uint32_t eventId, ShaderStage stage)
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  if(m_pDevice == NULL || m_FatalError != ResultCode::Succeeded)
    return;

  auto remapIt = m_EventRemap.find(eventId);
  if(remapIt != m_EventRemap.end())
    eventId = remapIt->second;

  const auto replacementIt = m_EventShaderReplacements.find(std::make_pair(eventId, stage));
  const bool changed = replacementIt != m_EventShaderReplacements.end();

  m_pDevice->ClearEventShaderOverride(eventId, stage);
  // A remote void mutation can fail without a replay, especially for an unselected EID.
  // Preserve the last acknowledged registration until the failure is visible to the caller.
  FatalErrorCheck();
  if(m_FatalError != ResultCode::Succeeded)
    return;

  if(changed)
  {
    m_EventShaderReplacements.erase(replacementIt);
    MarkEventShaderReplacementChanged(eventId);
  }

  if(eventId == m_EventID)
  {
    InvalidateScrubReplay();
    ReplayEvent(m_EventID, true, true);

    for(size_t i = 0; i < m_Outputs.size(); i++)
      if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
        m_Outputs[i]->Display();
  }
}

void ReplayController::ClearEventShaderOverrides()
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  if(m_pDevice == NULL || m_FatalError != ResultCode::Succeeded)
    return;

  std::set<uint32_t> affectedEvents;
  for(const auto &replacement : m_EventShaderReplacements)
    affectedEvents.insert(replacement.first.first);

  m_pDevice->ClearEventShaderOverrides();
  FatalErrorCheck();
  if(m_FatalError != ResultCode::Succeeded)
    return;
  m_EventShaderReplacements.clear();

  for(uint32_t eventId : affectedEvents)
    MarkEventShaderReplacementChanged(eventId);

  InvalidateScrubReplay();
  ReplayEvent(m_EventID, true, true);

  for(size_t i = 0; i < m_Outputs.size(); i++)
    if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
      m_Outputs[i]->Display();
}

rdcstr ReplayController::SetEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                                 ResourceId expectedShader, ResourceId expectedBuffer,
                                                 uint64_t expectedByteOffset,
                                                 uint64_t expectedByteSize, bytebuf data,
                                                 uint32_t registerSpace)
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  if(m_pDevice == NULL)
    return "No replay driver is available";

  if(m_pDevice->IsRemoteProxy())
    return "Event-scoped constant-buffer overrides require a local replay driver";

  if(m_APIProps.pipelineType != GraphicsAPI::D3D11 && m_APIProps.pipelineType != GraphicsAPI::D3D12)
    return "Event-scoped constant-buffer overrides currently support D3D11 and D3D12 captures";

  const uint32_t d3d11CBufferSlotCount = 14;
  const uint64_t d3d11CBufferMaxByteSize = 64ULL * 1024ULL;

  if(expectedShader == ResourceId())
    return "expectedShader must be a non-null captured shader resource";

  if(data.empty() || data.size() > d3d11CBufferMaxByteSize || (data.size() % 16) != 0)
    return "data must be non-empty, 16-byte aligned, and no larger than 64 KiB";

  if((expectedByteOffset % 16) != 0 || (expectedByteSize % 16) != 0 ||
     expectedByteSize > d3d11CBufferMaxByteSize)
    return "The expected offset and size must be 16-byte aligned, and size must not exceed 64 KiB";

  if(m_APIProps.pipelineType == GraphicsAPI::D3D11)
  {
    if(registerSpace != 0)
      return "D3D11 constant-buffer overrides require register space 0";
    if(slot >= d3d11CBufferSlotCount)
      return "D3D11 constant-buffer slot must be between 0 and 13";
    if(expectedBuffer == ResourceId() && (expectedByteOffset != 0 || expectedByteSize != 0))
      return "An expected unbound slot must use a zero byte offset and size";
  }
  else
  {
    if(expectedBuffer == ResourceId())
      return "D3D12 event CBuffer overrides require a bound captured buffer";
    if(expectedByteOffset % 256 != 0)
      return "D3D12 CBV byte offsets must be 256-byte aligned";
    if(expectedByteSize == 0)
      return "D3D12 CBV byte size must be non-zero";
  }

  auto remapIt = m_EventRemap.find(eventId);
  if(remapIt != m_EventRemap.end())
    eventId = remapIt->second;

  if(eventId != m_EventID)
    return "eventId must be the currently selected effective event before configuring an override";

  const EventCBufferOverrideKey overrideKey = {eventId, stage, slot, registerSpace};
  const bool hadPriorOverride =
      m_EventCBufferOverrides.find(overrideKey) != m_EventCBufferOverrides.end();

  // A snapshot-backed commit changes the logical EID without replaying the device. Both D3D11
  // and D3D12 validate this setter against live shader/CBV bindings before registering the shadow,
  // so materialise the selected event before that validation rather than rejecting a valid edit
  // against whichever event happened to be physically replayed last.
  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return "Failed to replay the current event before configuring a constant-buffer override";

  rdcstr error =
      m_pDevice->SetEventCBufferOverride(eventId, stage, slot, expectedShader, expectedBuffer,
                                         expectedByteOffset, expectedByteSize, data, registerSpace);
  FatalErrorCheck();

  if(!error.empty())
    return error;

  SetFrameEvent(m_EventID, true);

  if(!m_LastEventOverrideError.empty())
  {
    rdcstr applyError = m_LastEventOverrideError;
    m_pDevice->ClearEventCBufferOverride(eventId, stage, slot, registerSpace);
    // SetEventCBufferOverride replaces an existing configuration for this key. If validation of
    // the replacement failed, the driver cleared that key and the controller mirror must do the
    // same or a later scrub would incorrectly select the override replay path.
    m_EventCBufferOverrides.erase(overrideKey);
    if(hadPriorOverride)
      MarkEventShaderReplacementChanged(eventId);
    SetFrameEvent(m_EventID, true);
    return applyError;
  }

  m_EventCBufferOverrides.insert(overrideKey);
  // The mutation serial also guards persistent event texture snapshots. A CBuffer edit can change
  // the render target just as a shader replacement can, so both invalidate the same cache.
  MarkEventShaderReplacementChanged(eventId);

  for(size_t i = 0; i < m_Outputs.size(); i++)
    if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
      m_Outputs[i]->Display();

  return {};
}

void ReplayController::ClearEventCBufferOverride(uint32_t eventId, ShaderStage stage, uint32_t slot,
                                                 uint32_t registerSpace)
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  if(m_pDevice == NULL || m_pDevice->IsRemoteProxy() ||
     (m_APIProps.pipelineType != GraphicsAPI::D3D11 && m_APIProps.pipelineType != GraphicsAPI::D3D12))
    return;

  auto remapIt = m_EventRemap.find(eventId);
  if(remapIt != m_EventRemap.end())
    eventId = remapIt->second;

  m_pDevice->ClearEventCBufferOverride(eventId, stage, slot, registerSpace);
  const bool changed = m_EventCBufferOverrides.erase({eventId, stage, slot, registerSpace}) != 0;
  if(changed)
    MarkEventShaderReplacementChanged(eventId);

  if(eventId == m_EventID)
  {
    SetFrameEvent(m_EventID, true);

    for(size_t i = 0; i < m_Outputs.size(); i++)
      if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
        m_Outputs[i]->Display();
  }
}

void ReplayController::ClearEventCBufferOverrides()
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  if(m_pDevice == NULL || m_pDevice->IsRemoteProxy() ||
     (m_APIProps.pipelineType != GraphicsAPI::D3D11 && m_APIProps.pipelineType != GraphicsAPI::D3D12))
    return;

  std::set<uint32_t> affectedEvents;
  for(const EventCBufferOverrideKey &overrideKey : m_EventCBufferOverrides)
    affectedEvents.insert(overrideKey.eventId);

  m_pDevice->ClearEventCBufferOverrides();
  m_EventCBufferOverrides.clear();
  for(uint32_t eventId : affectedEvents)
    MarkEventShaderReplacementChanged(eventId);
  SetFrameEvent(m_EventID, true);

  for(size_t i = 0; i < m_Outputs.size(); i++)
    if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
      m_Outputs[i]->Display();
}

rdcstr ReplayController::SetSharedBufferPatch(ResourceId buffer, uint64_t byteOffset, bytebuf data)
{
  CHECK_REPLAY_THREAD();
  const BufferDescription *description = NULL;
  for(const BufferDescription &candidate : m_Buffers)
    if(candidate.resourceId == buffer)
      description = &candidate;
  if(!description || data.empty() || byteOffset > description->length ||
     data.size() > description->length - byteOffset)
    return "Persistent buffer edit is outside an original captured buffer";

  ReplayBufferPatchSet candidate = m_SharedBufferPatches;
  rdcstr error = candidate.Set(buffer, byteOffset, data);
  return error.empty() ? CommitSharedBufferPatches(candidate) : error;
}

rdcstr ReplayController::ClearSharedBufferPatch(ResourceId buffer, uint64_t byteOffset,
                                                uint64_t byteLength)
{
  CHECK_REPLAY_THREAD();
  ReplayBufferPatchSet candidate = m_SharedBufferPatches;
  if(!candidate.Remove(buffer, byteOffset, byteLength))
    return {};
  return CommitSharedBufferPatches(candidate);
}

rdcstr ReplayController::ClearSharedBufferPatches()
{
  CHECK_REPLAY_THREAD();
  return CommitSharedBufferPatches(ReplayBufferPatchSet());
}

rdcstr ReplayController::ResetSharedBufferRange(ResourceId buffer, uint64_t byteOffset,
                                                uint64_t byteLength)
{
  CHECK_REPLAY_THREAD();
  if(buffer == ResourceId() || byteLength == 0 || byteOffset > UINT64_MAX - byteLength)
    return "The captured buffer reset range is invalid";
  ReplayBufferPatchSet candidate = m_SharedBufferPatches;
  if(!candidate.ResetRange(buffer, byteOffset, byteLength))
    return {};
  return CommitSharedBufferPatches(candidate);
}

uint64_t ReplayController::GetSharedBufferPatchGeneration()
{
  CHECK_REPLAY_THREAD();
  return m_SharedBufferPatchGeneration;
}

rdcstr ReplayController::CommitSharedBufferPatches(const ReplayBufferPatchSet &patches)
{
  const bool timing = EIDTimingEnabled();
  PerformanceTimer timer;
  if(m_pDevice)
    FatalErrorCheck();
  if(!m_pDevice || m_FatalError != ResultCode::Succeeded)
    return "No healthy replay driver is available for persistent buffer edits";
  const rdcarray<ReplayBufferPatch> next = patches.Flatten();
  const rdcarray<ReplayBufferPatch> previous = m_SharedBufferPatches.Flatten();
  rdcarray<ReplayBufferPatchRange> changes;
  const bool deltaKnown = GetReplayBufferPatchChanges(previous, next, changes).empty();
  bool changed = next.size() != previous.size();
  for(size_t i = 0; !changed && i < next.size(); i++)
    changed = next[i].buffer != previous[i].buffer ||
              next[i].byteOffset != previous[i].byteOffset || next[i].data != previous[i].data;
  if(!changed || (deltaKnown && changes.empty()))
  {
    // Hidden layers still matter to later undo, even if the current native bytes are identical.
    m_SharedBufferPatches = patches;
    return {};
  }
  rdcstr error = ValidateReplayBufferPatches(next);
  if(!error.empty())
    return error;

  EndAnalysisForMutation();
  error = m_pDevice->SetReplayBufferPatches(next);
  // A failed upload rollback can leave the driver unusable even when the publication itself
  // was rejected. Observe that failure before another edit or a no-op can report success.
  FatalErrorCheck();
  if(!error.empty())
    return error;
  if(m_FatalError != ResultCode::Succeeded)
    return "No healthy replay driver is available for persistent buffer edits";
  const double commitMS = timing ? timer.GetMilliseconds() : 0.0;

  m_SharedBufferPatches = patches;
  m_SharedBufferPatchGeneration = NextRenderOutputMutationGeneration();
  const uint32_t firstAffected = m_pDevice->GetReplayBufferPatchFirstAffectedEvent();
  // Same-layout value edits retain proven upstream versions and their admission history. A layout
  // change has no valid consumer cutoff, so its following forced replay cannot train the policy.
  if(firstAffected == 0)
    ResetAutomaticCheckpointCosts(0);
  else if(previous.empty())
    ResetAutomaticCheckpointCosts(firstAffected);
  // Display/history generations still change globally: even a preserved prefix can expose the
  // patched buffer directly. The driver's complete consumer index independently proves which
  // upstream render-state checkpoints remain reusable. Shader replacements keep their separate
  // conservative invalidation path.
  m_GlobalShaderReplacementGeneration = m_SharedBufferPatchGeneration;
  m_ShaderReplacementSerial = m_SharedBufferPatchGeneration;
  m_MaterializedEventValid = false;
  {
    ScopedSharedBufferPatchReplay sharedReplay(m_pDevice, m_SharedBufferPatchReplay);
    SetFrameEvent(m_EventID, firstAffected == 0);
  }
  const double replayEndMS = timing ? timer.GetMilliseconds() : 0.0;
  if(m_FatalError != ResultCode::Succeeded)
    return "Persistent buffer edits were accepted, but replay failed; the device must be reopened";
  for(ReplayOutput *output : m_Outputs)
    if(output->GetType() != ReplayOutputType::Headless)
      output->Display();
  if(timing)
  {
    const double totalMS = timer.GetMilliseconds();
    uint64_t bytes = 0;
    for(const ReplayBufferPatch &patch : next)
      bytes += patch.data.size();
    uint64_t changedBytes = 0;
    for(const ReplayBufferPatchRange &change : changes)
      changedBytes += change.byteLength;
    const rdcstr line = StringFormat::Fmt(
        "api=ReplayController op=shared_buffer_patch event=%u ranges=%zu bytes=%llu "
        "first_affected_event=%u generation=%llu backend_commit_ms=%.3f replay_ms=%.3f "
        "display_ms=%.3f total_ms=%.3f delta_known=%u changed_ranges=%zu changed_bytes=%llu",
        m_EventID, next.size(), bytes, firstAffected, m_SharedBufferPatchGeneration, commitMS,
        replayEndMS - commitMS, totalMS - replayEndMS, totalMS, deltaKnown ? 1U : 0U,
        changes.size(), changedBytes);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return {};
}

const D3D11Pipe::State *ReplayController::GetD3D11PipelineState()
{
  CHECK_REPLAY_THREAD();

  return m_APIProps.pipelineType == GraphicsAPI::D3D11 ? &m_D3D11PipelineState : NULL;
}

const D3D12Pipe::State *ReplayController::GetD3D12PipelineState()
{
  CHECK_REPLAY_THREAD();

  return m_APIProps.pipelineType == GraphicsAPI::D3D12 ? &m_D3D12PipelineState : NULL;
}

const GLPipe::State *ReplayController::GetGLPipelineState()
{
  CHECK_REPLAY_THREAD();

  return m_APIProps.pipelineType == GraphicsAPI::OpenGL ? &m_GLPipelineState : NULL;
}

const VKPipe::State *ReplayController::GetVulkanPipelineState()
{
  CHECK_REPLAY_THREAD();

  return m_APIProps.pipelineType == GraphicsAPI::Vulkan ? &m_VulkanPipelineState : NULL;
}

const PipeState &ReplayController::GetPipelineState()
{
  CHECK_REPLAY_THREAD();

  return m_PipeState;
}

rdcarray<Descriptor> ReplayController::GetDescriptors(ResourceId descriptorStore,
                                                      const rdcarray<DescriptorRange> &ranges)
{
  CHECK_REPLAY_THREAD();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return {};

  return m_pDevice->GetDescriptors(descriptorStore, ranges);
}

const rdcarray<DescriptorAccess> &ReplayController::GetDescriptorAccess()
{
  CHECK_REPLAY_THREAD();

  return m_PipeState.GetDescriptorAccess();
}

rdcarray<DescriptorLogicalLocation> ReplayController::GetDescriptorLocations(
    ResourceId descriptorStore, const rdcarray<DescriptorRange> &ranges)
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetDescriptorLocations(descriptorStore, ranges);
}

rdcarray<SamplerDescriptor> ReplayController::GetSamplerDescriptors(
    ResourceId descriptorStore, const rdcarray<DescriptorRange> &ranges)
{
  CHECK_REPLAY_THREAD();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return {};

  return m_pDevice->GetSamplerDescriptors(descriptorStore, ranges);
}

rdcarray<rdcstr> ReplayController::GetDisassemblyTargets(bool withPipeline)
{
  CHECK_REPLAY_THREAD();

  rdcarray<rdcstr> ret;

  rdcarray<rdcstr> targets = m_pDevice->GetDisassemblyTargets(withPipeline);

  ret.reserve(targets.size());
  for(const rdcstr &t : targets)
    ret.push_back(t);

  for(const rdcstr &t : m_GCNTargets)
    ret.push_back(t);

  return ret;
}

rdcstr ReplayController::DisassembleShader(ResourceId pipeline, const ShaderReflection *refl,
                                           const rdcstr &target)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(refl == NULL)
    return "; Error: No shader specified";

  for(const rdcstr &t : m_GCNTargets)
    if(t == target)
      return GCNISA::Disassemble(refl->encoding, refl->stage, refl->rawBytes, target);

  rdcstr ret = m_pDevice->DisassembleShader(pipeline, refl, target);
  FatalErrorCheck();
  return ret;
}

FrameDescription ReplayController::GetFrameInfo()
{
  CHECK_REPLAY_THREAD();

  return m_FrameRecord.frameInfo;
}

const SDFile &ReplayController::GetStructuredFile()
{
  CHECK_REPLAY_THREAD();

  return *m_pDevice->GetStructuredFile();
}

ActionDescription *ReplayController::GetActionByEID(uint32_t eventId)
{
  CHECK_REPLAY_THREAD();

  if(eventId >= m_Actions.size())
    return NULL;

  return m_Actions[eventId];
}

const rdcarray<ActionDescription> &ReplayController::GetRootActions()
{
  CHECK_REPLAY_THREAD();

  return m_FrameRecord.actionList;
}

bool ReplayController::ContainsMarker(const rdcarray<ActionDescription> &actions)
{
  CHECK_REPLAY_THREAD();

  bool ret = false;

  for(const ActionDescription &a : actions)
  {
    ret |= (a.flags & ActionFlags::PushMarker) &&
           !(a.flags & (ActionFlags::CmdList | ActionFlags::MultiAction)) && !a.children.empty();
    ret |= ContainsMarker(a.children);

    if(ret)
      break;
  }

  return ret;
}

bool ReplayController::PassEquivalent(const ActionDescription &a, const ActionDescription &b)
{
  CHECK_REPLAY_THREAD();

  // don't group actions and compute executes
  if((a.flags & ActionFlags::Dispatch) != (b.flags & ActionFlags::Dispatch))
    return false;

  // don't group anything with raytracing either
  if((a.flags & ActionFlags::DispatchRay) != (b.flags & ActionFlags::DispatchRay))
    return false;

  // don't group present with anything
  if((a.flags & ActionFlags::Present) != (b.flags & ActionFlags::Present))
    return false;

  // don't group things with different depth outputs
  if(a.depthOut != b.depthOut)
    return false;

  int numAOuts = 0, numBOuts = 0;
  for(int i = 0; i < 8; i++)
  {
    if(a.outputs[i] != ResourceId())
      numAOuts++;
    if(b.outputs[i] != ResourceId())
      numBOuts++;
  }

  int numSame = 0;

  if(a.depthOut != ResourceId())
  {
    numAOuts++;
    numBOuts++;
    numSame++;
  }

  for(int i = 0; i < 8; i++)
  {
    if(a.outputs[i] != ResourceId())
    {
      for(int j = 0; j < 8; j++)
      {
        if(a.outputs[i] == b.outputs[j])
        {
          numSame++;
          break;
        }
      }
    }
    else if(b.outputs[i] != ResourceId())
    {
      for(int j = 0; j < 8; j++)
      {
        if(a.outputs[j] == b.outputs[i])
        {
          numSame++;
          break;
        }
      }
    }
  }

  // use a kind of heuristic to group together passes where the outputs are similar enough.
  // could be useful for example if you're rendering to a gbuffer and sometimes you render
  // without one target, but the actions are still batched up.
  if(numSame > RDCMAX(numAOuts, numBOuts) / 2 && RDCMAX(numAOuts, numBOuts) > 1)
    return true;

  if(numSame == RDCMAX(numAOuts, numBOuts))
    return true;

  return false;
}

void ReplayController::AddFakeMarkers()
{
  CHECK_REPLAY_THREAD();

  rdcarray<ActionDescription> &actions = m_FrameRecord.actionList;

  if(ContainsMarker(actions))
    return;

  uint32_t newEventId = actions.back().events.back().eventId + 1;
  uint32_t newActionId = actions.back().actionId + 1;

  rdcarray<ActionDescription> ret;

  int depthpassID = 1;
  int copypassID = 1;
  int computepassID = 1;
  int passID = 1;

  int start = 0;
  int refaction = 0;

  ActionFlags actionFlags =
      ActionFlags::Copy | ActionFlags::Resolve | ActionFlags::SetMarker | ActionFlags::CmdList;

  for(int32_t i = 1; i < actions.count(); i++)
  {
    if(actions[refaction].flags & actionFlags)
    {
      refaction = i;
      continue;
    }

    if(actions[i].flags & actionFlags)
      continue;

    if(PassEquivalent(actions[i], actions[refaction]))
      continue;

    int end = i - 1;

    if(end - start < 2 || !actions[i].children.empty() || !actions[refaction].children.empty())
    {
      for(int j = start; j <= end; j++)
        ret.push_back(actions[j]);

      start = i;
      refaction = i;
      continue;
    }

    int minOutCount = 100;
    int maxOutCount = 0;
    bool copyOnly = true;

    for(int j = start; j <= end; j++)
    {
      int outCount = 0;

      if(!(actions[j].flags & (ActionFlags::Copy | ActionFlags::Resolve | ActionFlags::Clear |
                               ActionFlags::PassBoundary | ActionFlags::SetMarker)))
        copyOnly = false;

      for(ResourceId o : actions[j].outputs)
        if(o != ResourceId())
          outCount++;
      minOutCount = RDCMIN(minOutCount, outCount);
      maxOutCount = RDCMAX(maxOutCount, outCount);
    }

    ActionDescription mark;

    mark.eventId = newEventId++;
    mark.actionId = newActionId++;

    mark.flags = ActionFlags::PushMarker;
    mark.outputs = actions[end].outputs;
    mark.depthOut = actions[end].depthOut;

    minOutCount = RDCMAX(1, minOutCount);

    const char *targets = actions[end].depthOut == ResourceId() ? "Targets" : "Targets + Depth";

    if(copyOnly)
      mark.customName = StringFormat::Fmt("Copy/Clear Pass #%d", copypassID++);
    else if(actions[refaction].flags & ActionFlags::Dispatch)
      mark.customName = StringFormat::Fmt("Compute Pass #%d", computepassID++);
    else if(actions[refaction].flags & ActionFlags::DispatchRay)
      mark.customName = StringFormat::Fmt("Raytracing Pass #%d", computepassID++);
    else if(maxOutCount == 0)
      mark.customName = StringFormat::Fmt("Depth-only Pass #%d", depthpassID++);
    else if(minOutCount == maxOutCount)
      mark.customName = StringFormat::Fmt("Colour Pass #%d (%d %s)", passID++, minOutCount, targets);
    else
      mark.customName = StringFormat::Fmt("Colour Pass #%d (%d-%d %s)", passID++, minOutCount,
                                          maxOutCount, targets);

    mark.children.resize(end - start + 1);

    for(int j = start; j <= end; j++)
      mark.children[j - start] = actions[j];

    APIEvent ev;
    ev.eventId = mark.eventId;
    ev.fileOffset = mark.children[0].events.back().fileOffset;
    ev.chunkIndex = APIEvent::NoChunk;
    mark.events.push_back(ev);

    // when this event is selected, instead select the first the event before the first child's
    // first event. This is effectively what would be the case if there was a real marker here.
    m_EventRemap[mark.eventId] = mark.children[0].events[0].eventId - 1;

    ret.push_back(mark);

    start = i;
    refaction = i;
  }

  if(start < actions.count())
  {
    for(int j = start; j < actions.count(); j++)
      ret.push_back(actions[j]);
  }

  m_FrameRecord.actionList = ret;

  // re-configure the previous/next pointeres
  m_Actions.clear();
  SetupActionPointers(m_Actions, m_FrameRecord.actionList);
}

rdcarray<CounterResult> ReplayController::FetchCounters(const rdcarray<GPUCounter> &counters)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return {};

  rdcarray<CounterResult> ret = m_pDevice->FetchCounters(counters);
  FatalErrorCheck();

  // Counter collection performs its own frame replays. Restore lazily-materialised state before
  // returning so a subsequent presentation of a captured resource cannot observe the last counter
  // pass instead of the selected event.
  m_MaterializedEventValid = false;
  EnsureMaterializedCurrentEvent(__FUNCTION__);
  return ret;
}

rdcarray<GPUCounter> ReplayController::EnumerateCounters()
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->EnumerateCounters();
}

CounterDescription ReplayController::DescribeCounter(GPUCounter counterID)
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->DescribeCounter(counterID);
}

const rdcarray<ResourceDescription> &ReplayController::GetResources()
{
  CHECK_REPLAY_THREAD();

  return m_Resources;
}

const rdcarray<BufferDescription> &ReplayController::GetBuffers()
{
  CHECK_REPLAY_THREAD();

  return m_Buffers;
}

const rdcarray<DescriptorStoreDescription> &ReplayController::GetDescriptorStores()
{
  CHECK_REPLAY_THREAD();

  return m_DescriptorStores;
}

const rdcarray<TextureDescription> &ReplayController::GetTextures()
{
  CHECK_REPLAY_THREAD();

  return m_Textures;
}

rdcarray<DebugMessage> ReplayController::GetDebugMessages()
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetDebugMessages();
}

rdcarray<ShaderEntryPoint> ReplayController::GetShaderEntryPoints(ResourceId shader)
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetShaderEntryPoints(shader);
}

const ShaderReflection *ReplayController::GetShader(ResourceId pipeline, ResourceId shader,
                                                    ShaderEntryPoint entry)
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetShader(pipeline, shader, entry);
}

rdcarray<EventUsage> ReplayController::GetUsage(ResourceId id)
{
  CHECK_REPLAY_THREAD();

  if(id == ResourceId())
    return {EventUsage(0, ResourceUsage::Unused)};
  return m_pDevice->GetUsage(id);
}

MeshFormat ReplayController::GetPostVSData(uint32_t instID, uint32_t viewID, MeshDataStage stage)
{
  CHECK_REPLAY_THREAD();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return MeshFormat();

  ActionDescription *action = GetActionByEID(m_EventID);

  if(action == NULL || !(action->flags & (ActionFlags::MeshDispatch | ActionFlags::Drawcall)))
    return MeshFormat();

  instID = RDCMIN(instID, action->numInstances - 1);

  m_pDevice->InitPostVSBuffers(action->eventId);
  FatalErrorCheck();

  MeshFormat ret = m_pDevice->GetPostVSBuffers(action->eventId, instID, viewID, stage);
  FatalErrorCheck();

  return ret;
}

bytebuf ReplayController::GetBufferData(ResourceId buff, uint64_t offset, uint64_t len)
{
  CHECK_REPLAY_THREAD();

  bytebuf retData;

  if(!EnsureExplicitResourceRead(__FUNCTION__))
    return retData;

  if(buff == ResourceId())
    return retData;

  m_pDevice->GetBufferData(buff, offset, len, retData);
  FatalErrorCheck();

  return retData;
}

bytebuf ReplayController::GetTextureData(ResourceId tex, const Subresource &sub)
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();

  bytebuf ret;

  if(!EnsureExplicitResourceRead(__FUNCTION__))
    return ret;

  if(tex == ResourceId())
    return ret;

  m_pDevice->GetTextureData(tex, sub, GetTextureDataParams(), ret);
  FatalErrorCheck();

  return ret;
}

ResultDetails ReplayController::SaveTexture(const TextureSave &saveData, const rdcstr &path)
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();

  if(!EnsureExplicitResourceRead(__FUNCTION__))
  {
    if(m_FatalError != ResultCode::Succeeded)
      return m_FatalError;
    RETURN_ERROR_RESULT(ResultCode::APIReplayFailed, "No completed analysis replay is available");
  }

  TextureSave sd = saveData;    // mutable copy

  if(sd.resourceId == ResourceId())
  {
    RETURN_ERROR_RESULT(ResultCode::InvalidParameter, "Invalid ID for %s getting texture data",
                        ToStr(sd.resourceId).c_str());
  }

  TextureDescription td = m_pDevice->GetTexture(sd.resourceId);

  // clamp sample/mip/slice indices
  if(td.msSamp == 1)
  {
    sd.sample.sampleIndex = 0;
    sd.sample.mapToArray = false;
  }
  else
  {
    if(sd.sample.sampleIndex != ~0U)
      sd.sample.sampleIndex = RDCCLAMP((uint32_t)sd.sample.sampleIndex, 0U, td.msSamp);
  }

  // don't support cube cruciform for non cubemaps, or
  // cubemap arrays
  if(!td.cubemap || td.arraysize != 6 || td.msSamp != 1)
    sd.slice.cubeCruciform = false;

  if(sd.mip != -1)
    sd.mip = RDCCLAMP(sd.mip, 0, (int32_t)td.mips);
  if(sd.slice.sliceIndex != -1)
    sd.slice.sliceIndex = RDCCLAMP(sd.slice.sliceIndex, 0, int32_t(td.arraysize * td.depth));

  if(td.arraysize * td.depth * td.msSamp == 1)
  {
    sd.slice.sliceIndex = 0;
    sd.slice.slicesAsGrid = false;
  }

  // can't extract a channel that's not in the source texture
  if(sd.channelExtract >= 0 && (uint32_t)sd.channelExtract >= td.format.compCount)
    sd.channelExtract = -1;

  sd.slice.sliceGridWidth = RDCMAX(sd.slice.sliceGridWidth, 1);

  // store sample count so we know how many 'slices' is one real slice
  // multisampled textures cannot have mips, subresource layout is same as would be for mips:
  // [slice0 sample0], [slice0 sample1], [slice1 sample0], [slice1 sample1]
  uint32_t sampleCount = RDCMAX(td.msSamp, 1U);
  bool multisampled = td.msSamp > 1;

  if(sd.sample.mapToArray)
    sd.sample.sampleIndex = 0;

  bool resolveSamples = (sd.sample.sampleIndex == ~0U);

  if(resolveSamples)
  {
    td.msSamp = 1;
    sd.sample.mapToArray = false;
    sd.sample.sampleIndex = 0;
  }

  // treat any multisampled texture as if it were an array
  // of <sample count> dimension (on top of potential existing array
  // dimension).
  if(td.msSamp > 1)
  {
    td.arraysize *= td.msSamp;
    td.msSamp = 1;
  }

  if(sd.destType != FileType::DDS && sd.sample.mapToArray && !sd.slice.slicesAsGrid &&
     sd.slice.sliceIndex == -1)
  {
    sd.sample.mapToArray = false;
    sd.sample.sampleIndex = 0;
  }

  // only DDS supports writing multiple mips, fall back to mip 0 if 'all mips' was specified
  if(sd.destType != FileType::DDS && sd.mip == -1)
    sd.mip = 0;

  // only DDS supports writing multiple slices, fall back to slice 0 if 'all slices' was specified
  if(sd.destType != FileType::DDS && sd.slice.sliceIndex == -1 && !sd.slice.slicesAsGrid &&
     !sd.slice.cubeCruciform)
    sd.slice.sliceIndex = 0;

  // fetch source data subresources (typically only one, possibly more
  // if we're writing to DDS (so writing multiple mips/slices) or resolving
  // down a multisampled texture for writing as a single 'image' elsewhere)
  uint32_t sliceOffset = 0;
  uint32_t sliceStride = 1;
  uint32_t numSlices = td.arraysize * td.depth;

  uint32_t mipOffset = 0;
  uint32_t numMips = td.mips;

  bool singleSlice = (sd.slice.sliceIndex != -1);

  // set which slices/mips we need
  if(multisampled)
  {
    bool singleSample = !sd.sample.mapToArray;

    // multisampled images have no mips
    mipOffset = 0;
    numMips = 1;

    if(singleSlice)
    {
      if(singleSample)
      {
        // we want a specific sample in a specific real slice
        sliceOffset = sd.slice.sliceIndex * sampleCount + sd.sample.sampleIndex;
        numSlices = 1;
      }
      else
      {
        // we want all the samples (now mapped to slices) in a specific real slice
        sliceOffset = sd.slice.sliceIndex;
        numSlices = sampleCount;
      }
    }
    else
    {
      if(singleSample)
      {
        // we want one sample in every slice, so we have to set the stride to sampleCount
        // to skip every other sample (mapped to slices), starting from the sample we want
        // in the first real slice
        sliceOffset = sd.sample.sampleIndex;
        sliceStride = sampleCount;
        numSlices = RDCMAX(1U, td.arraysize / sampleCount);
      }
      else
      {
        // we want all slices, all samples
        sliceOffset = 0;
        numSlices = td.arraysize;
      }
    }
  }
  else
  {
    if(singleSlice)
    {
      numSlices = 1;
      sliceOffset = sd.slice.sliceIndex;
    }
    // otherwise take all slices, as by default

    if(sd.mip != -1)
    {
      mipOffset = sd.mip;
      numMips = 1;
    }
    // otherwise take all mips, as by default
  }

  rdcarray<byte *> subdata;

  bool downcast = false;

  // don't support slice mappings for DDS - it supports slices natively
  if(sd.destType == FileType::DDS)
  {
    sd.slice.cubeCruciform = false;
    sd.slice.slicesAsGrid = false;
  }

  // force downcast to be able to do grid mappings
  if(sd.slice.cubeCruciform || sd.slice.slicesAsGrid)
    downcast = true;

  // we don't support any file formats that handle these block compression formats
  if(td.format.type == ResourceFormatType::ETC2 || td.format.type == ResourceFormatType::EAC ||
     td.format.type == ResourceFormatType::ASTC)
    downcast = true;

  // for non-HDR always downcast if we're not already RGBA8 unorm
  if(sd.destType != FileType::DDS && sd.destType != FileType::HDR && sd.destType != FileType::EXR &&
     (td.format.compByteWidth != 1 || td.format.compCount != 4 ||
      td.format.compType != CompType::UNorm || td.format.BGRAOrder() || td.format.Special()))
    downcast = true;

  // for HDR & EXR we can convert from most regular types as well as 10.10.10.2 and 11.11.10
  if(sd.destType != FileType::DDS && td.format.Special() &&
     td.format.type != ResourceFormatType::R10G10B10A2 &&
     td.format.type != ResourceFormatType::R11G11B10)
    downcast = true;

  // if we're downcasting, pick either RGBA8 or RGBA32 to downcast to
  RemapTexture remap = RemapTexture::NoRemap;

  if(downcast)
  {
    const bool destHDR = (sd.destType == FileType::DDS || sd.destType == FileType::HDR ||
                          sd.destType == FileType::EXR);

    const bool sourceHDR =
        td.format.compByteWidth > 1 || td.format.type == ResourceFormatType::D16S8 ||
        td.format.type == ResourceFormatType::D24S8 || td.format.type == ResourceFormatType::D32S8 ||
        td.format.type == ResourceFormatType::R11G11B10 ||
        td.format.type == ResourceFormatType::R10G10B10A2 ||
        td.format.type == ResourceFormatType::R9G9B9E5 || td.format.type == ResourceFormatType::BC6 ||
        td.format.type == ResourceFormatType::BC7 || td.format.type == ResourceFormatType::YUV10 ||
        td.format.type == ResourceFormatType::YUV12 || td.format.type == ResourceFormatType::YUV16;

    // if the source and destination have more than 1 byte per component, remap to RGBA32 to avoid
    // precision loss
    if(sourceHDR && destHDR)
    {
      remap = RemapTexture::RGBA32;
      td.format.compByteWidth = 4;
      td.format.compCount = 4;
      td.format.compType = CompType::Float;
      td.format.type = ResourceFormatType::Regular;
    }
    else
    {
      remap = RemapTexture::RGBA8;
      td.format.compByteWidth = 1;
      td.format.compCount = 4;
      td.format.compType = CompType::UNorm;
      td.format.type = ResourceFormatType::Regular;
    }
  }

  uint32_t rowPitch = 0;
  uint32_t slicePitch = 0;

  bool blockformat = false;
  int blockSize = 0;
  uint32_t bytesPerPixel = 1;

  td.width = RDCMAX(1U, td.width >> mipOffset);
  td.height = RDCMAX(1U, td.height >> mipOffset);
  td.depth = RDCMAX(1U, td.depth >> mipOffset);

  if(td.format.type == ResourceFormatType::BC1 || td.format.type == ResourceFormatType::BC2 ||
     td.format.type == ResourceFormatType::BC3 || td.format.type == ResourceFormatType::BC4 ||
     td.format.type == ResourceFormatType::BC5 || td.format.type == ResourceFormatType::BC6 ||
     td.format.type == ResourceFormatType::BC7)
  {
    blockSize =
        (td.format.type == ResourceFormatType::BC1 || td.format.type == ResourceFormatType::BC4)
            ? 8
            : 16;
    rowPitch = RDCMAX(1U, ((td.width + 3) / 4)) * blockSize;
    slicePitch = rowPitch * RDCMAX(1U, td.height / 4);
    blockformat = true;
  }
  else
  {
    switch(td.format.type)
    {
      case ResourceFormatType::S8:
      case ResourceFormatType::A8: bytesPerPixel = 1; break;
      case ResourceFormatType::R10G10B10A2:
      case ResourceFormatType::R9G9B9E5:
      case ResourceFormatType::R11G11B10:
      case ResourceFormatType::D24S8: bytesPerPixel = 4; break;
      case ResourceFormatType::R5G6B5:
      case ResourceFormatType::R5G5B5A1:
      case ResourceFormatType::R4G4B4A4: bytesPerPixel = 2; break;
      case ResourceFormatType::D32S8: bytesPerPixel = 8; break;
      case ResourceFormatType::D16S8:
      case ResourceFormatType::YUV8:
      case ResourceFormatType::YUV10:
      case ResourceFormatType::YUV12:
      case ResourceFormatType::YUV16:
      case ResourceFormatType::R4G4:
      {
        RETURN_ERROR_RESULT(ResultCode::ImageUnsupported, "Unsupported file format %s",
                            ToStr(td.format.type).c_str());
      }
      default: bytesPerPixel = td.format.compCount * td.format.compByteWidth;
    }

    rowPitch = td.width * bytesPerPixel;
    slicePitch = rowPitch * td.height;
  }

  // loop over fetching subresources
  for(uint32_t s = 0; s < numSlices; s++)
  {
    uint32_t slice = s * sliceStride + sliceOffset;

    for(uint32_t m = 0; m < numMips; m++)
    {
      uint32_t mip = m + mipOffset;

      GetTextureDataParams params;
      params.forDiskSave = true;
      params.standardLayout = true;
      params.typeCast = sd.typeCast;
      params.resolve = resolveSamples;
      params.remap = remap;
      params.blackPoint = sd.comp.blackPoint;
      params.whitePoint = sd.comp.whitePoint;

      Subresource sub = {mip, slice / sampleCount, slice % sampleCount};

      bytebuf data;
      m_pDevice->GetTextureData(sd.resourceId, sub, params, data);
      FatalErrorCheck();

      if(data.empty())
      {
        for(size_t i = 0; i < subdata.size(); i++)
          delete[] subdata[i];

        RETURN_ERROR_RESULT(ResultCode::DataNotAvailable,
                            "Couldn't readback bytes for mip %u, slice %u, sample %u", sub.mip,
                            sub.slice, sub.sample);
      }

      if(td.depth == 1)
      {
        byte *bytes = new byte[data.size()];
        memcpy(bytes, data.data(), data.size());
        subdata.push_back(bytes);
        continue;
      }

      uint32_t mipSlicePitch = slicePitch;

      uint32_t w = RDCMAX(1U, td.width >> m);
      uint32_t h = RDCMAX(1U, td.height >> m);
      uint32_t d = RDCMAX(1U, td.depth >> m);

      if(blockformat)
      {
        mipSlicePitch = RDCMAX(1U, ((w + 3) / 4)) * blockSize * RDCMAX(1U, h / 4);
      }
      else
      {
        mipSlicePitch = w * bytesPerPixel * h;
      }

      // we don't support slice ranges, only all-or-nothing
      // we're also not dealing with multisampled slices if
      // depth > 1. So if we only want one slice out of a 3D texture
      // then make sure we get it
      if(numSlices == 1)
      {
        byte *depthslice = new byte[mipSlicePitch];
        byte *b = data.data() + mipSlicePitch * sliceOffset;
        memcpy(depthslice, b, mipSlicePitch);
        subdata.push_back(depthslice);

        continue;
      }

      s += (d - 1);

      byte *b = data.data();

      // add each depth slice as a separate subdata
      for(uint32_t di = 0; di < d; di++)
      {
        byte *depthslice = new byte[mipSlicePitch];

        memcpy(depthslice, b, mipSlicePitch);

        subdata.push_back(depthslice);

        b += mipSlicePitch;
      }
    }
  }

  // should have been handled above, but verify incoming data is RGBA8 or RGBA32
  if(sd.slice.slicesAsGrid && (td.format.compByteWidth == 1 || td.format.compByteWidth == 4) &&
     td.format.compCount == 4 && !td.format.Special())
  {
    uint32_t sliceWidth = td.width;
    uint32_t sliceHeight = td.height;

    uint32_t sliceGridHeight = (td.arraysize * td.depth) / sd.slice.sliceGridWidth;
    if((td.arraysize * td.depth) % sd.slice.sliceGridWidth != 0)
      sliceGridHeight++;

    td.width *= sd.slice.sliceGridWidth;
    td.height *= sliceGridHeight;

    uint32_t pixelStride = td.format.compCount * td.format.compByteWidth;

    byte *combinedData = new byte[td.width * td.height * pixelStride];

    memset(combinedData, 0, td.width * td.height * pixelStride);

    for(size_t i = 0; i < subdata.size(); i++)
    {
      uint32_t gridx = (uint32_t)i % sd.slice.sliceGridWidth;
      uint32_t gridy = (uint32_t)i / sd.slice.sliceGridWidth;

      uint32_t yoffs = gridy * sliceHeight;
      uint32_t xoffs = gridx * sliceWidth;

      for(uint32_t y = 0; y < sliceHeight; y++)
      {
        for(uint32_t x = 0; x < sliceWidth; x++)
        {
          uint32_t *srcpix = (uint32_t *)&subdata[i][(y * sliceWidth + x) * pixelStride + 0];
          uint32_t *dstpix =
              (uint32_t *)&combinedData[((y + yoffs) * td.width + x + xoffs) * pixelStride + 0];

          memcpy(dstpix, srcpix, pixelStride);
        }
      }

      delete[] subdata[i];
    }

    subdata.resize(1);
    subdata[0] = combinedData;
    rowPitch = td.width * 4;
  }

  // should have been handled above, but verify incoming data is RGBA8 or RGBA32 and 6 slices
  if(sd.slice.cubeCruciform && (td.format.compByteWidth == 1 || td.format.compByteWidth == 4) &&
     td.format.compCount == 4 && !td.format.Special() && subdata.size() == 6)
  {
    uint32_t sliceWidth = td.width;
    uint32_t sliceHeight = td.height;

    td.width *= 4;
    td.height *= 3;

    uint32_t pixelStride = td.format.compCount * td.format.compByteWidth;

    byte *combinedData = new byte[td.width * td.height * pixelStride];

    memset(combinedData, 0, td.width * td.height * pixelStride);

    /*
     Y X=0   1   2   3
     =     +---+
     0     |+y |
           |[2]|
       +---+---+---+---+
     1 |-x |+z |+x |-z |
       |[1]|[4]|[0]|[5]|
       +---+---+---+---+
     2     |-y |
           |[3]|
           +---+

    */

    uint32_t gridx[6] = {2, 0, 1, 1, 1, 3};
    uint32_t gridy[6] = {1, 1, 0, 2, 1, 1};

    for(size_t i = 0; i < subdata.size(); i++)
    {
      uint32_t yoffs = gridy[i] * sliceHeight;
      uint32_t xoffs = gridx[i] * sliceWidth;

      for(uint32_t y = 0; y < sliceHeight; y++)
      {
        for(uint32_t x = 0; x < sliceWidth; x++)
        {
          uint32_t *srcpix = (uint32_t *)&subdata[i][(y * sliceWidth + x) * pixelStride + 0];
          uint32_t *dstpix =
              (uint32_t *)&combinedData[((y + yoffs) * td.width + x + xoffs) * pixelStride + 0];

          memcpy(dstpix, srcpix, pixelStride);
        }
      }

      delete[] subdata[i];
    }

    subdata.resize(1);
    subdata[0] = combinedData;
    rowPitch = td.width * 4;
  }

  int numComps = td.format.compCount;

  // if we want a grayscale image of one channel, splat it across all channels
  // and set alpha to full
  if(sd.channelExtract >= 0 && td.format.type == ResourceFormatType::Regular &&
     (td.format.compByteWidth == 1 || td.format.compByteWidth == 4) &&
     (uint32_t)sd.channelExtract < td.format.compCount)
  {
    uint32_t pixelStride = td.format.compCount * td.format.compByteWidth;
    uint32_t compWidth = td.format.compByteWidth;
    uint32_t compCount = td.format.compCount;

    uint32_t val = 0;
    uint32_t max = ~0U;

    for(uint32_t y = 0; y < td.height; y++)
    {
      for(uint32_t x = 0; x < td.width; x++)
      {
        memcpy(&val, &subdata[0][(y * td.width + x) * pixelStride + sd.channelExtract * compWidth],
               td.format.compByteWidth);

        switch(compCount)
        {
          case 4:
            memcpy(&subdata[0][(y * td.width + x) * pixelStride + 3 * compWidth], &max,
                   td.format.compByteWidth);
            DELIBERATE_FALLTHROUGH();
          case 3:
            memcpy(&subdata[0][(y * td.width + x) * pixelStride + 2 * compWidth], &val,
                   td.format.compByteWidth);
            DELIBERATE_FALLTHROUGH();
          case 2:
            memcpy(&subdata[0][(y * td.width + x) * pixelStride + 1 * compWidth], &val,
                   td.format.compByteWidth);
            DELIBERATE_FALLTHROUGH();
          case 1:
            memcpy(&subdata[0][(y * td.width + x) * pixelStride + 0 * compWidth], &val,
                   td.format.compByteWidth);
            break;
        }
      }
    }
  }

  // handle formats that don't support alpha
  if(numComps == 4 && (sd.destType == FileType::BMP || sd.destType == FileType::JPG))
  {
    byte *nonalpha = new byte[td.width * td.height * 3];

    for(uint32_t y = 0; y < td.height; y++)
    {
      for(uint32_t x = 0; x < td.width; x++)
      {
        byte r = subdata[0][(y * td.width + x) * 4 + 0];
        byte g = subdata[0][(y * td.width + x) * 4 + 1];
        byte b = subdata[0][(y * td.width + x) * 4 + 2];
        byte a = subdata[0][(y * td.width + x) * 4 + 3];

        if(sd.alpha != AlphaMapping::Discard)
        {
          Vec4f col = Vec4f(sd.alphaCol.x, sd.alphaCol.y, sd.alphaCol.z, 0.0f);
          if(sd.alpha == AlphaMapping::BlendToCheckerboard)
          {
            bool lightSquare = ((x / 64) % 2) == ((y / 64) % 2);
            col = lightSquare ? RenderDoc::Inst().LightCheckerboardColor()
                              : RenderDoc::Inst().DarkCheckerboardColor();
          }

          col.x = ConvertLinearToSRGB(col.x);
          col.y = ConvertLinearToSRGB(col.y);
          col.z = ConvertLinearToSRGB(col.z);

          FloatVector pixel = FloatVector(float(r) / 255.0f, float(g) / 255.0f, float(b) / 255.0f,
                                          float(a) / 255.0f);

          pixel.x = pixel.x * pixel.w + col.x * (1.0f - pixel.w);
          pixel.y = pixel.y * pixel.w + col.y * (1.0f - pixel.w);
          pixel.z = pixel.z * pixel.w + col.z * (1.0f - pixel.w);

          r = byte(pixel.x * 255.0f);
          g = byte(pixel.y * 255.0f);
          b = byte(pixel.z * 255.0f);
        }

        nonalpha[(y * td.width + x) * 3 + 0] = r;
        nonalpha[(y * td.width + x) * 3 + 1] = g;
        nonalpha[(y * td.width + x) * 3 + 2] = b;
      }
    }

    delete[] subdata[0];

    subdata[0] = nonalpha;

    numComps = 3;
    rowPitch = td.width * 3;
  }

  // assume that (R,G,0) is better mapping than (Y,A) for 2 component data
  if(numComps == 2 && (sd.destType == FileType::BMP || sd.destType == FileType::JPG ||
                       sd.destType == FileType::PNG || sd.destType == FileType::TGA))
  {
    byte *rg0 = new byte[td.width * td.height * 3];

    for(uint32_t y = 0; y < td.height; y++)
    {
      for(uint32_t x = 0; x < td.width; x++)
      {
        byte r = subdata[0][(y * td.width + x) * 2 + 0];
        byte g = subdata[0][(y * td.width + x) * 2 + 1];

        rg0[(y * td.width + x) * 3 + 0] = r;
        rg0[(y * td.width + x) * 3 + 1] = g;
        rg0[(y * td.width + x) * 3 + 2] = 0;

        // if we're greyscaling the image, then keep the greyscale here.
        if(sd.channelExtract >= 0)
          rg0[(y * td.width + x) * 3 + 2] = r;
      }
    }

    delete[] subdata[0];

    subdata[0] = rg0;

    numComps = 3;
    rowPitch = td.width * 3;
  }

  FILE *f = FileIO::fopen(path, FileIO::WriteBinary);

  RDResult res;

  if(!f)
  {
    RETURN_ERROR_RESULT(ResultCode::FileIOFailed, "Couldn't write to path %s, error: %s",
                        path.c_str(), FileIO::ErrorString().c_str());
  }
  else
  {
    if(sd.destType == FileType::DDS)
    {
      write_tex_data ddsData;

      ResourceFormat saveFmt = td.format;
      // use typeCast to inform typeless saving, otherwise it will get lost
      if(saveFmt.compType == CompType::Typeless)
        saveFmt.compType = sd.typeCast;

      ddsData.width = td.width;
      ddsData.height = td.height;
      ddsData.depth = td.depth;
      ddsData.format = saveFmt;
      ddsData.mips = numMips;
      ddsData.slices = numSlices / td.depth;
      ddsData.subresources = subdata;
      ddsData.cubemap = td.cubemap && numSlices == 6;

      if(singleSlice)
        ddsData.depth = ddsData.slices = 1;

      res = write_dds_to_file(f, ddsData);
    }
    else if(sd.destType == FileType::BMP)
    {
      int ret = stbi_write_bmp_to_func(fileWriteFunc, (void *)f, td.width, td.height, numComps,
                                       subdata[0]);

      if(ret == 0)
        SET_ERROR_RESULT(res, ResultCode::InternalError, "Failed to write BMP image");
    }
    else if(sd.destType == FileType::PNG)
    {
      int ret = stbi_write_png_to_func(fileWriteFunc, (void *)f, td.width, td.height, numComps,
                                       subdata[0], rowPitch);

      if(ret == 0)
        SET_ERROR_RESULT(res, ResultCode::InternalError, "Failed to write PNG image");
    }
    else if(sd.destType == FileType::TGA)
    {
      int ret = stbi_write_tga_to_func(fileWriteFunc, (void *)f, td.width, td.height, numComps,
                                       subdata[0]);

      if(ret == 0)
        SET_ERROR_RESULT(res, ResultCode::InternalError, "Failed to write TGA image");
    }
    else if(sd.destType == FileType::JPG)
    {
      jpge::params p;
      p.m_quality = sd.jpegQuality;

      int len = td.width * td.height * td.format.compCount;
      // ensure buffer is at least 1024
      if(len < 1024)
        len = 1024;

      char *jpgdst = new char[len];

      bool success = jpge::compress_image_to_jpeg_file_in_memory(jpgdst, len, td.width, td.height,
                                                                 numComps, subdata[0], p);

      if(success)
        fwrite(jpgdst, 1, len, f);
      else
        SET_ERROR_RESULT(res, ResultCode::InternalError, "Failed to write JPG image");

      delete[] jpgdst;
    }
    else if(sd.destType == FileType::HDR || sd.destType == FileType::EXR)
    {
      float *fldata = NULL;
      float *abgr[4] = {NULL, NULL, NULL, NULL};

      if(sd.destType == FileType::HDR)
      {
        fldata = new float[td.width * td.height * 4];
      }
      else
      {
        abgr[0] = new float[td.width * td.height];
        abgr[1] = new float[td.width * td.height];
        abgr[2] = new float[td.width * td.height];
        abgr[3] = new float[td.width * td.height];
      }

      byte *srcData = subdata[0];

      ResourceFormat saveFmt = td.format;
      if(saveFmt.compType == CompType::Typeless)
        saveFmt.compType = sd.typeCast;
      if(saveFmt.compType == CompType::Typeless)
        saveFmt.compType = saveFmt.compByteWidth == 4 ? CompType::Float : CompType::UNorm;

      uint32_t pixStride = saveFmt.ElementSize();

      // 24-bit depth still has a stride of 4 bytes.
      if(saveFmt.compType == CompType::Depth && pixStride == 3)
        pixStride = 4;

      for(uint32_t y = 0; y < td.height; y++)
      {
        for(uint32_t x = 0; x < td.width; x++)
        {
          FloatVector pixel = DecodeFormattedComponents(saveFmt, srcData);
          srcData += pixStride;

          // HDR can't represent negative values
          if(sd.destType == FileType::HDR)
          {
            pixel.x = RDCMAX(pixel.x, 0.0f);
            pixel.y = RDCMAX(pixel.y, 0.0f);
            pixel.z = RDCMAX(pixel.z, 0.0f);
            pixel.w = RDCMAX(pixel.w, 0.0f);
          }

          if(sd.channelExtract == 0)
          {
            pixel.y = pixel.z = pixel.x;
            pixel.w = 1.0f;
          }
          else if(sd.channelExtract == 1)
          {
            pixel.x = pixel.z = pixel.y;
            pixel.w = 1.0f;
          }
          else if(sd.channelExtract == 2)
          {
            pixel.x = pixel.y = pixel.z;
            pixel.w = 1.0f;
          }
          else if(sd.channelExtract == 3)
          {
            pixel.x = pixel.y = pixel.z = pixel.w;
            pixel.w = 1.0f;
          }

          if(fldata)
          {
            fldata[(y * td.width + x) * 4 + 0] = pixel.x;
            fldata[(y * td.width + x) * 4 + 1] = pixel.y;
            fldata[(y * td.width + x) * 4 + 2] = pixel.z;
            fldata[(y * td.width + x) * 4 + 3] = pixel.w;
          }
          else
          {
            abgr[0][(y * td.width + x)] = pixel.w;
            abgr[1][(y * td.width + x)] = pixel.z;
            abgr[2][(y * td.width + x)] = pixel.y;
            abgr[3][(y * td.width + x)] = pixel.x;
          }
        }
      }

      if(sd.destType == FileType::HDR)
      {
        int ret = stbi_write_hdr_to_func(fileWriteFunc, (void *)f, td.width, td.height, 4, fldata);

        if(ret == 0)
          SET_ERROR_RESULT(res, ResultCode::InternalError, "Failed to write HDR image");
      }
      else if(sd.destType == FileType::EXR)
      {
        const char *err = NULL;

        EXRHeader exrHeader;
        InitEXRHeader(&exrHeader);

        EXRImage exrImage;
        InitEXRImage(&exrImage);

        int pixTypes[4] = {TINYEXR_PIXELTYPE_FLOAT, TINYEXR_PIXELTYPE_FLOAT,
                           TINYEXR_PIXELTYPE_FLOAT, TINYEXR_PIXELTYPE_FLOAT};
        int reqTypes[4] = {TINYEXR_PIXELTYPE_HALF, TINYEXR_PIXELTYPE_HALF, TINYEXR_PIXELTYPE_HALF,
                           TINYEXR_PIXELTYPE_HALF};

        if(saveFmt.compByteWidth == 4)
        {
          for(size_t channel = 0; channel < 4; channel++)
          {
            reqTypes[channel] = TINYEXR_PIXELTYPE_FLOAT;
          }
        }

        // must be in this order as many viewers don't pay attention to channels and just assume
        // they are in this order
        EXRChannelInfo bgraChannels[4] = {
            {"A"},
            {"B"},
            {"G"},
            {"R"},
        };

        exrHeader.num_channels = 4;
        exrHeader.channels = bgraChannels;
        exrImage.images = (unsigned char **)abgr;
        exrImage.width = td.width;
        exrImage.height = td.height;
        exrHeader.pixel_types = pixTypes;
        exrHeader.requested_pixel_types = reqTypes;

        unsigned char *mem = NULL;

        size_t ret = SaveEXRImageToMemory(&exrImage, &exrHeader, &mem, &err);

        if(ret > 0)
          FileIO::fwrite(mem, 1, ret, f);
        else
          SET_ERROR_RESULT(res, ResultCode::InternalError, "Failed to write EXR image: %s", err);

        free(mem);
      }

      if(fldata)
      {
        delete[] fldata;
      }
      else
      {
        delete[] abgr[0];
        delete[] abgr[1];
        delete[] abgr[2];
        delete[] abgr[3];
      }
    }

    FileIO::fclose(f);
  }

  for(size_t i = 0; i < subdata.size(); i++)
    delete[] subdata[i];

  return res;
}

rdcarray<PixelModification> ReplayController::PixelHistory(ResourceId target, uint32_t x, uint32_t y,
                                                           const Subresource &sub, CompType typeCast)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  rdcarray<PixelModification> ret;

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return ret;

  Subresource subresource = sub;

  for(size_t t = 0; t < m_Textures.size(); t++)
  {
    if(m_Textures[t].resourceId == target)
    {
      if(x >= m_Textures[t].width || y >= m_Textures[t].height)
      {
        RDCDEBUG("PixelHistory out of bounds on %s (%u,%u) vs (%u,%u)", ToStr(target).c_str(), x, y,
                 m_Textures[t].width, m_Textures[t].height);
        return ret;
      }

      if(m_Textures[t].msSamp == 1)
        subresource.sample = ~0U;

      if(m_Textures[t].dimension == 3)
      {
        subresource.slice = RDCCLAMP(subresource.slice, 0U, m_Textures[t].depth >> subresource.mip);
      }
      else
      {
        subresource.slice = RDCCLAMP(subresource.slice, 0U, m_Textures[t].arraysize);
      }

      subresource.mip = RDCCLAMP(subresource.mip, 0U, m_Textures[t].mips - 1);

      break;
    }
  }

  ResourceId id = target;

  if(id == ResourceId())
    return ret;

  rdcarray<EventUsage> usage = m_pDevice->GetUsage(id);

  rdcarray<EventUsage> events;

  for(size_t i = 0; i < usage.size(); i++)
  {
    if(usage[i].eventId > m_EventID)
      continue;

    switch(usage[i].usage)
    {
      case ResourceUsage::VertexBuffer:
      case ResourceUsage::IndexBuffer:
      case ResourceUsage::VS_Constants:
      case ResourceUsage::HS_Constants:
      case ResourceUsage::DS_Constants:
      case ResourceUsage::GS_Constants:
      case ResourceUsage::PS_Constants:
      case ResourceUsage::CS_Constants:
      case ResourceUsage::TS_Constants:
      case ResourceUsage::MS_Constants:
      case ResourceUsage::All_Constants:
      case ResourceUsage::VS_Resource:
      case ResourceUsage::HS_Resource:
      case ResourceUsage::DS_Resource:
      case ResourceUsage::GS_Resource:
      case ResourceUsage::PS_Resource:
      case ResourceUsage::CS_Resource:
      case ResourceUsage::TS_Resource:
      case ResourceUsage::MS_Resource:
      case ResourceUsage::All_Resource:
      case ResourceUsage::InputTarget:
      case ResourceUsage::CopySrc:
      case ResourceUsage::ResolveSrc:
      case ResourceUsage::Barrier:
      case ResourceUsage::Indirect:
      case ResourceUsage::Shader:
        // read-only, not a valid pixel history event
        continue;

      case ResourceUsage::CPUWrite:
        // writing but CPU-only, don't include
        continue;

      case ResourceUsage::Discard:
        // writing but not something pixel history should handle
        continue;

      case ResourceUsage::Unused:
      case ResourceUsage::StreamOut:
      case ResourceUsage::VS_RWResource:
      case ResourceUsage::HS_RWResource:
      case ResourceUsage::DS_RWResource:
      case ResourceUsage::GS_RWResource:
      case ResourceUsage::PS_RWResource:
      case ResourceUsage::CS_RWResource:
      case ResourceUsage::TS_RWResource:
      case ResourceUsage::MS_RWResource:
      case ResourceUsage::All_RWResource:
      case ResourceUsage::ColorTarget:
      case ResourceUsage::DepthStencilTarget:
      case ResourceUsage::Clear:
      case ResourceUsage::Copy:
      case ResourceUsage::CopyDst:
      case ResourceUsage::Resolve:
      case ResourceUsage::ResolveDst:
      case ResourceUsage::GenMips:
        // writing - include in pixel history events
        break;
    }

    events.push_back(usage[i]);
  }

  if(events.empty())
  {
    RDCDEBUG("Target %s not written to before %u", ToStr(target).c_str(), m_EventID);
    return ret;
  }

  id = target;

  if(id == ResourceId())
    return ret;

  ret = m_pDevice->PixelHistory(events, id, x, y, subresource, typeCast);
  FatalErrorCheck();

  SetFrameEvent(m_EventID, true);

  return ret;
}

PixelValue ReplayController::PickPixel(ResourceId tex, uint32_t x, uint32_t y,
                                       const Subresource &sub, CompType typeCast)
{
  CHECK_REPLAY_THREAD();
  RENDERDOC_PROFILEFUNCTION();

  PixelValue ret;

  RDCEraseEl(ret.floatValue);

  if(tex == ResourceId())
    return ret;

  ResourceId queryTexture = tex;
  Subresource querySubresource = sub;
  CompType queryTypeCast = typeCast;
  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return ret;

  m_pDevice->PickPixel(queryTexture, x, y, querySubresource, queryTypeCast, ret.floatValue.data());
  FatalErrorCheck();

  return ret;
}

rdcpair<PixelValue, PixelValue> ReplayController::GetMinMax(ResourceId textureId,
                                                            const Subresource &sub, CompType typeCast)
{
  CHECK_REPLAY_THREAD();

  PixelValue minval = {{0.0f, 0.0f, 0.0f, 0.0f}};
  PixelValue maxval = {{1.0f, 1.0f, 1.0f, 1.0f}};

  ResourceId queryTexture = textureId;
  Subresource querySubresource = sub;
  CompType queryTypeCast = typeCast;
  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return make_rdcpair(minval, maxval);

  m_pDevice->GetMinMax(queryTexture, querySubresource, queryTypeCast, &minval.floatValue[0],
                       &maxval.floatValue[0]);
  FatalErrorCheck();

  return make_rdcpair(minval, maxval);
}

rdcarray<uint32_t> ReplayController::GetHistogram(ResourceId textureId, const Subresource &sub,
                                                  CompType typeCast, float minval, float maxval,
                                                  const rdcfixedarray<bool, 4> &channels)
{
  CHECK_REPLAY_THREAD();

  rdcarray<uint32_t> hist;

  ResourceId queryTexture = textureId;
  Subresource querySubresource = sub;
  CompType queryTypeCast = typeCast;
  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return hist;

  m_pDevice->GetHistogram(queryTexture, querySubresource, queryTypeCast, minval, maxval, channels,
                          hist);
  FatalErrorCheck();

  return hist;
}

ShaderDebugTrace *ReplayController::DebugVertex(uint32_t vertid, uint32_t instid, uint32_t idx,
                                                uint32_t view)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return new ShaderDebugTrace;

  ShaderDebugTrace *ret = m_pDevice->DebugVertex(m_EventID, vertid, instid, idx, view);
  FatalErrorCheck();

  SetFrameEvent(m_EventID, true);

  if(ret->debugger)
    m_Debuggers.push_back(ret->debugger);

  return ret;
}

ShaderDebugTrace *ReplayController::DebugPixel(uint32_t x, uint32_t y, const DebugPixelInputs &inputs)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return new ShaderDebugTrace;

  ShaderDebugTrace *ret = m_pDevice->DebugPixel(m_EventID, x, y, inputs);
  FatalErrorCheck();

  SetFrameEvent(m_EventID, true);

  if(ret->debugger)
    m_Debuggers.push_back(ret->debugger);

  return ret;
}

ShaderDebugTrace *ReplayController::DebugThread(const rdcfixedarray<uint32_t, 3> &groupid,
                                                const rdcfixedarray<uint32_t, 3> &threadid)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return new ShaderDebugTrace;

  ShaderDebugTrace *ret = m_pDevice->DebugThread(m_EventID, groupid, threadid);
  FatalErrorCheck();

  SetFrameEvent(m_EventID, true);

  if(ret->debugger)
    m_Debuggers.push_back(ret->debugger);

  return ret;
}

ShaderDebugTrace *ReplayController::DebugMeshThread(const rdcfixedarray<uint32_t, 3> &groupid,
                                                    const rdcfixedarray<uint32_t, 3> &threadid)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return new ShaderDebugTrace;

  ShaderDebugTrace *ret = m_pDevice->DebugMeshThread(m_EventID, groupid, threadid);
  FatalErrorCheck();

  SetFrameEvent(m_EventID, true);

  if(ret->debugger)
    m_Debuggers.push_back(ret->debugger);

  return ret;
}

rdcarray<ShaderDebugState> ReplayController::ContinueDebug(ShaderDebugger *debugger)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  rdcarray<ShaderDebugState> ret = m_pDevice->ContinueDebug(debugger);
  FatalErrorCheck();

  return ret;
}

void ReplayController::FreeTrace(ShaderDebugTrace *trace)
{
  CHECK_REPLAY_THREAD();

  if(trace)
  {
    m_Debuggers.removeOne(trace->debugger);
    m_pDevice->FreeDebugger(trace->debugger);
    delete trace;
  }
}

rdcarray<ShaderVariable> ReplayController::GetCBufferVariableContents(
    ResourceId pipeline, ResourceId shader, ShaderStage stage, const rdcstr &entryPoint,
    uint32_t cbufslot, ResourceId buffer, uint64_t offset, uint64_t length)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return {};

  bytebuf data;
  if(buffer != ResourceId())
  {
    if(buffer != ResourceId())
    {
      if(length > 0)
        m_pDevice->GetBufferData(buffer, offset, length, data);
      FatalErrorCheck();
    }
  }

  rdcarray<ShaderVariable> v;

  if(shader != ResourceId())
  {
    m_pDevice->FillCBufferVariables(pipeline, shader, stage, entryPoint, cbufslot, v, data);
    FatalErrorCheck();
  }

  return v;
}

rdcarray<WindowingSystem> ReplayController::GetSupportedWindowSystems()
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetSupportedWindowSystems();
}

rdcstr ReplayController::CreateRGPProfile(WindowingData window)
{
  CHECK_REPLAY_THREAD();
  ScopedCheckpointAnalysisBypass checkpointBypass(m_pDevice);

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return "";

  // This operation performs repeated direct full-frame replays and only restores the capture to a
  // broadly equivalent EID afterwards. A pending scrub commit must not assume its exact transient
  // replay state survived.
  InvalidateScrubReplay();
  m_MaterializedEventValid = false;

  AMDRGPControl *rgp = m_pDevice->GetRGPControl();

  if(!rgp)
  {
    RDCERR("RGP Capture is not supported on this API implementation");
    return "";
  }

  rdcstr path = FileIO::GetTempFolderFilename() + "/renderdoc_rgp_capture.rgp";

  FileIO::Delete(path);

  ReplayOutput *output = CreateOutput(window, ReplayOutputType::Texture);

  TextureDisplay d = {};
  output->SetTextureDisplay(d);

  // prime the pump
  for(int i = 0; i < 5; i++)
  {
    m_pDevice->ReplayLog(10000000, eReplay_Full);
    if(FatalErrorCheck())
      return "";
    output->Display();
  }

  bool captureTriggered = rgp->TriggerCapture(path);
  if(!captureTriggered)
  {
    RDCERR("Failed to trigger an RGP Capture.");
    return "";
  }

  // delay a while to make sure the profiling is ready to go
  Threading::Sleep(5000);

  // replay for capture. We do this a few times since doing it only once doesn't seem to pick up
  // (6-7 runs needed)
  for(int i = 0; i < 10; i++)
  {
    if(rgp->HasCapture())
    {
      RDCDEBUG("Got profile after %d runs", i);
      break;
    }

    output->Display();
    m_pDevice->ReplayLog(10000000, eReplay_Full);
    if(FatalErrorCheck())
      return "";
  }

  output->Display();

  // restore back to where we were
  m_pDevice->ReplayLog(m_EventID, eReplay_Full);
  if(FatalErrorCheck())
    return "";
  m_MaterializedEventID = m_EventID;
  m_MaterializedEventValid = true;

  ShutdownOutput(output);

  // wait for 5 seconds for the capture to become ready
  for(int i = 0; i < 50; i++)
  {
    if(rgp->HasCapture())
      return path;

    Threading::Sleep(100);
  }

  RDCERR("Didn't get capture after 5 seconds");

  return "";
}

void ReplayController::ReplayLoop(WindowingData window, ResourceId texid)
{
  CHECK_REPLAY_THREAD();
  ScopedCheckpointAnalysisBypass checkpointBypass(m_pDevice);

  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return;

  InvalidateScrubReplay();
  m_MaterializedEventValid = false;

  ReplayOutput *output = CreateOutput(window, ReplayOutputType::Texture);

  TextureDisplay d;
  d.resourceId = texid;
  d.subresource = {0, 0, ~0U};
  d.overlay = DebugOverlay::NoOverlay;
  d.typeCast = CompType::Typeless;
  d.hdrMultiplier = -1.0f;
  d.linearDisplayAsGamma = true;
  d.flipY = false;
  d.rangeMin = 0.0f;
  d.rangeMax = 1.0f;
  d.scale = 1.0f;
  d.xOffset = 0.0f;
  d.yOffset = 0.0f;
  d.rawOutput = false;
  d.red = d.green = d.blue = true;
  d.alpha = false;
  output->SetTextureDisplay(d);

  m_ReplayLoopCancel = 0;
  m_ReplayLoopFinished = 0;

  while(Atomic::CmpExch32(&m_ReplayLoopCancel, 0, 0) == 0)
  {
    m_pDevice->ReplayLog(10000000, eReplay_Full);
    FatalErrorCheck();

    output->Display();
  }

  // restore back to where we were
  m_pDevice->ReplayLog(m_EventID, eReplay_Full);
  if(!FatalErrorCheck())
  {
    m_MaterializedEventID = m_EventID;
    m_MaterializedEventValid = true;
  }

  ShutdownOutput(output);

  // mark that the loop is finished
  Atomic::Inc32(&m_ReplayLoopFinished);
}

void ReplayController::CancelReplayLoop()
{
  Atomic::Inc32(&m_ReplayLoopCancel);

  // wait for it to actually finish before returning
  while(Atomic::CmpExch32(&m_ReplayLoopFinished, 0, 0) == 0)
    Threading::Sleep(1);
}

ReplayOutput *ReplayController::CreateOutput(WindowingData window, ReplayOutputType type)
{
  CHECK_REPLAY_THREAD();

  // New outputs have no event snapshot binding to present from. Materialise before initialising
  // them so their first texture/mesh display cannot sample resources from an older physical EID.
  if(!EnsureMaterializedCurrentEvent(__FUNCTION__))
    return NULL;

  ReplayOutput *out = new ReplayOutput(this, window, type);

  m_Outputs.push_back(out);

  out->SetFrameEvent(m_EventID);

  return out;
}

void ReplayController::ShutdownOutput(IReplayOutput *output)
{
  CHECK_REPLAY_THREAD();

  size_t sz = m_Outputs.size();
  m_Outputs.removeOneIf([output](const ReplayOutput *o) {
    if((IReplayOutput *)o == output)
    {
      delete o;
      return true;
    }

    return false;
  });

  if(m_Outputs.size() == sz)
    RDCERR("Unrecognised output");
  else
    ReapRetiredEventTextureSnapshots();
}

void ReplayController::Shutdown()
{
  CHECK_REPLAY_THREAD();
  RestoreAnalysisEventOverrides();

  RDCLOG("Shutting down replay renderer");

  for(size_t i = 0; i < m_Outputs.size(); i++)
    SAFE_DELETE(m_Outputs[i]);

  m_Outputs.clear();

  // A failed CreateDevice still shuts down its controller, without a driver.
  // Keep all driver-owned cleanup inside this guard, including override buffers.
  if(m_pDevice)
  {
    for(auto it = m_CustomShaders.begin(); it != m_CustomShaders.end(); ++it)
      m_pDevice->FreeCustomShader(*it);

    // Drop replay-owned buffers and internal references before target shaders are freed.
    m_pDevice->ClearEventCBufferOverrides();
    m_pDevice->ClearEventShaderOverrides();

    for(auto it = m_TargetResources.begin(); it != m_TargetResources.end(); ++it)
      for(uint32_t reference = 0; reference < it->second; ++reference)
        m_pDevice->FreeTargetResource(it->first);

    m_pDevice->Shutdown();
  }
  m_CustomShaders.clear();
  m_TargetResources.clear();
  m_pDevice = NULL;

  RenderDoc::Inst().ClearTrackedFiles();
  delete this;
}

bool ReplayController::FatalErrorCheck()
{
  // we've already processed the fatal error if we have a status
  if(m_FatalError != ResultCode::Succeeded)
    return false;

  m_FatalError = m_pDevice->FatalErrorCheck();

  if(m_FatalError != ResultCode::Succeeded)
  {
    RDCLOG("Fatal error detected: %s (%s) at event %u", ToStr(m_FatalError.code).c_str(),
           m_FatalError.message.c_str(), m_EventID);

    IReplayDriver *old = m_pDevice;

    // replace our driver with a dummy
    m_pDevice = m_pDevice->MakeDummyDriver();

    std::set<ResourceId> staleSnapshotIds = m_RetiredEventTextureSnapshots;
    for(const EventTextureSnapshot &entry : m_EventTextureSnapshots)
      if(entry.snapshot != ResourceId())
        staleSnapshotIds.insert(entry.snapshot);

    // The old driver owns every persistent event snapshot and releases them during Shutdown.
    // Never retain their ResourceIds in the controller after swapping to the dummy driver.
    m_EventTextureSnapshots.clear();
    m_RetiredEventTextureSnapshots.clear();
    m_EventTextureSnapshotBytes = 0;
    m_AnalysisEventOverridesActive = false;
    m_AnalysisReplayState = AnalysisReplayState::PendingOrFailed;
    m_MaterializedEventValid = false;
    m_ScrubReplayValid = false;

    // replace the outputs as well
    for(size_t i = 0; i < m_Outputs.size(); i++)
    {
      old->DestroyOutputWindow(m_Outputs[i]->m_MainOutput.outputID);
      old->DestroyOutputWindow(m_Outputs[i]->m_PixelContext.outputID);
      m_Outputs[i]->ClearThumbnails();

      m_Outputs[i]->m_pDevice = m_pDevice;
      m_Outputs[i]->m_HistorySnapshotActive = false;
      m_Outputs[i]->m_HistoryReplaySource.Clear();
      if(staleSnapshotIds.find(m_Outputs[i]->m_RenderData.texDisplay.resourceId) !=
         staleSnapshotIds.end())
      {
        m_Outputs[i]->m_RenderData.texDisplay.resourceId = ResourceId();
        m_Outputs[i]->m_TextureDim = {0, 0};
        m_Outputs[i]->m_MainOutput.dirty = true;
      }
    }

    // delete the old replay
    old->Shutdown();

    // reset pipeline states to default
    m_D3D11PipelineState = D3D11Pipe::State();
    m_D3D12PipelineState = D3D12Pipe::State();
    m_GLPipelineState = GLPipe::State();
    m_VulkanPipelineState = VKPipe::State();

    m_PipeState.SetDescriptorAccess({}, {}, {});

    return true;
  }

  return false;
}

rdcarray<ShaderEncoding> ReplayController::GetCustomShaderEncodings()
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetCustomShaderEncodings();
}

rdcarray<ShaderSourcePrefix> ReplayController::GetCustomShaderSourcePrefixes()
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetCustomShaderSourcePrefixes();
}

rdcarray<ShaderEncoding> ReplayController::GetTargetShaderEncodings()
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetTargetShaderEncodings();
}

rdcpair<ResourceId, rdcstr> ReplayController::BuildTargetShader(
    const rdcstr &entry, ShaderEncoding sourceEncoding, bytebuf source,
    const ShaderCompileFlags &compileFlags, ShaderStage type)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  if(source.empty())
    return rdcpair<ResourceId, rdcstr>(ResourceId(), "0-byte shader is not valid");

  rdcarray<ShaderEncoding> encodings = m_pDevice->GetTargetShaderEncodings();

  if(encodings.indexOf(sourceEncoding) == -1)
    return rdcpair<ResourceId, rdcstr>(
        ResourceId(),
        StringFormat::Fmt("Shader Encoding '%s' is not supported", ToStr(sourceEncoding).c_str()));

  ResourceId id;
  rdcstr errs;

  switch(type)
  {
    case ShaderStage::Vertex:
    case ShaderStage::Hull:
    case ShaderStage::Domain:
    case ShaderStage::Geometry:
    case ShaderStage::Pixel:
    case ShaderStage::Compute:
    case ShaderStage::Task:
    case ShaderStage::Mesh: break;
    default: RDCERR("Unexpected type in BuildShader!"); return rdcpair<ResourceId, rdcstr>();
  }

  m_pDevice->BuildTargetShader(sourceEncoding, source, entry, compileFlags, type, id, errs);
  FatalErrorCheck();

  if(id != ResourceId())
    ++m_TargetResources[id];

  return rdcpair<ResourceId, rdcstr>(id, errs);
}

void ReplayController::SetCustomShaderIncludes(const rdcarray<rdcstr> &directories)
{
  CHECK_REPLAY_THREAD();

  m_pDevice->SetCustomShaderIncludes(directories);
}

rdcpair<ResourceId, rdcstr> ReplayController::BuildCustomShader(
    const rdcstr &entry, ShaderEncoding sourceEncoding, bytebuf source,
    const ShaderCompileFlags &compileFlags, ShaderStage type)
{
  CHECK_REPLAY_THREAD();

  ResourceId id;
  rdcstr errs;

  if(source.empty())
    return rdcpair<ResourceId, rdcstr>(ResourceId(), "0-byte shader is not valid");

  switch(type)
  {
    case ShaderStage::Vertex:
    case ShaderStage::Hull:
    case ShaderStage::Domain:
    case ShaderStage::Geometry:
    case ShaderStage::Pixel:
    case ShaderStage::Compute:
    case ShaderStage::Task:
    case ShaderStage::Mesh: break;
    default: RDCERR("Unexpected type in BuildShader!"); return rdcpair<ResourceId, rdcstr>();
  }

  RDCLOG("Building custom shader");

  m_pDevice->BuildCustomShader(sourceEncoding, source, entry, compileFlags, type, id, errs);
  FatalErrorCheck();

  if(id != ResourceId())
  {
    RDCLOG("Successfully built custom shader");
    m_CustomShaders.insert(id);
  }
  else
  {
    RDCLOG("Failed to build custom shader");
  }

  return rdcpair<ResourceId, rdcstr>(id, errs);
}

void ReplayController::FreeTargetResource(ResourceId id)
{
  CHECK_REPLAY_THREAD();

  auto target = m_TargetResources.find(id);
  if(target == m_TargetResources.end())
    return;

  // A reflection or A/B observation may acquire the same native shader as the active
  // replacement. Releasing that temporary reference must not revoke the live owner's
  // BuildTargetShader admission or remove its global/event replacement bookkeeping.
  if(target->second > 1)
  {
    --target->second;
    m_pDevice->FreeTargetResource(id);
    return;
  }

  EndAnalysisForMutation();

  std::set<uint32_t> affectedEvents;
  for(auto it = m_EventShaderReplacements.begin(); it != m_EventShaderReplacements.end();)
  {
    if(it->second.replacement == id)
    {
      affectedEvents.insert(it->first.first);
      it = m_EventShaderReplacements.erase(it);
    }
    else
    {
      ++it;
    }
  }

  m_pDevice->ClearEventShaderOverridesForResource(id);
  for(uint32_t eventId : affectedEvents)
    MarkEventShaderReplacementChanged(eventId);

  bool removedGlobalReplacement = false;
  for(auto it = m_GlobalShaderReplacements.begin(); it != m_GlobalShaderReplacements.end();)
  {
    // D3D12 may lend BuildTargetShader the captured shader's existing ResourceId.
    // Releasing that temporary build does not remove a replacement keyed by the
    // captured shader; only the replacement target being released is relevant.
    if(it->second == id)
    {
      it = m_GlobalShaderReplacements.erase(it);
      removedGlobalReplacement = true;
    }
    else
    {
      ++it;
    }
  }
  if(removedGlobalReplacement)
    MarkGlobalShaderReplacementChanged();

  m_TargetResources.erase(id);
  m_pDevice->FreeTargetResource(id);

  // FreeTargetResource also removes event-scoped configurations that reference this shader.
  // Match ClearEventShaderOverride when that configuration belongs to the current event: a
  // virtual commit may still have the old shader's immutable image bound, and bumping the cache
  // generation alone does not replace an already-bound output snapshot.
  if(affectedEvents.find(m_EventID) != affectedEvents.end())
  {
    SetFrameEvent(m_EventID, true);
    for(size_t i = 0; i < m_Outputs.size(); i++)
      if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
        m_Outputs[i]->Display();
  }
}

void ReplayController::FreeCustomShader(ResourceId id)
{
  CHECK_REPLAY_THREAD();

  m_CustomShaders.erase(id);
  m_pDevice->FreeCustomShader(id);
}

void ReplayController::ReplaceResource(ResourceId from, ResourceId to)
{
  CHECK_REPLAY_THREAD();
  for(const ReplayBufferPatch &patch : m_SharedBufferPatches.Flatten())
  {
    if(patch.buffer == from || patch.buffer == to)
    {
      RDCERR("Clear persistent buffer edits before replacing their native storage (%s -> %s)",
             ToStr(from).c_str(), ToStr(to).c_str());
      return;
    }
  }
  EndAnalysisForMutation();

  const auto existingShader = m_GlobalShaderReplacements.find(from);
  if(existingShader != m_GlobalShaderReplacements.end() && existingShader->second == to &&
     m_TargetResources.find(to) != m_TargetResources.end())
  {
    // Preserve the legacy API for other resources. Immutable shader targets can use the checked
    // backend ownership proof to avoid rebuilding and replaying an unchanged installation.
    const rdcstr unchanged = m_pDevice->ReplaceTargetShaderChecked(from, to);
    FatalErrorCheck();
    if(unchanged.empty())
      return;
  }
  m_pDevice->ReplaceResource(from, to);
  FatalErrorCheck();

  if(IsShaderResource(from))
  {
    auto replacement = m_GlobalShaderReplacements.find(from);
    if(replacement == m_GlobalShaderReplacements.end() || replacement->second != to)
    {
      m_GlobalShaderReplacements[from] = to;
    }
  }

  // Texture and buffer replacements can alter an event's render target just as directly as a
  // shader replacement. Keep the historically-named generation as the global output-mutation
  // epoch so persistent event snapshots never survive an arbitrary resource edit.
  MarkGlobalShaderReplacementChanged();

  SetFrameEvent(m_EventID, true);

  for(size_t i = 0; i < m_Outputs.size(); i++)
    if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
      m_Outputs[i]->Display();
}

rdcstr ReplayController::ReplaceTargetShaderChecked(ResourceId from, ResourceId to)
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  if(from == ResourceId() || to == ResourceId())
    return "Both original and replacement shader resources must be non-null";

  if(m_TargetResources.find(to) == m_TargetResources.end())
    return "replacement was not created by BuildTargetShader on this replay controller";

  rdcstr error = m_pDevice->ReplaceTargetShaderChecked(from, to);
  FatalErrorCheck();

  if(!error.empty())
    return error;

  auto replacement = m_GlobalShaderReplacements.find(from);
  // The backend has still validated live ownership and the target identity. Reapplying the
  // identical admitted target must not replay the frame or redraw every output again.
  if(replacement != m_GlobalShaderReplacements.end() && replacement->second == to)
    return {};
  if(replacement == m_GlobalShaderReplacements.end() || replacement->second != to)
  {
    m_GlobalShaderReplacements[from] = to;
    MarkGlobalShaderReplacementChanged();
  }

  if(m_ShaderBatchToken)
  {
    m_ShaderBatchDirty = true;
    return {};
  }

  {
    ScopedCheckedShaderReplay mutation(m_pDevice);
    SetFrameEvent(m_EventID, true);
  }

  for(size_t i = 0; i < m_Outputs.size(); i++)
    if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
      m_Outputs[i]->Display();

  return {};
}

uint64_t ReplayController::BeginShaderReplacementBatch()
{
  CHECK_REPLAY_THREAD();
  if(m_ShaderBatchToken)
    return 0;
  m_ShaderBatchDirty = false;
  m_ShaderBatchToken = ++m_ShaderBatchSerial;
  return m_ShaderBatchToken;
}

bool ReplayController::EndShaderReplacementBatch(uint64_t token)
{
  CHECK_REPLAY_THREAD();
  if(!token || token != m_ShaderBatchToken)
    return false;
  m_ShaderBatchToken = 0;
  const bool dirty = m_ShaderBatchDirty;
  m_ShaderBatchDirty = false;
  if(dirty)
  {
    // Explicit user event changes during the batch take precedence. Resource
    // identity, checked driver transactions and cache generations were already
    // committed per item; only the redundant automatic replay was deferred.
    {
      ScopedCheckedShaderReplay mutation(m_pDevice);
      SetFrameEvent(m_EventID, true);
    }
    for(size_t i = 0; i < m_Outputs.size(); i++)
      if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
        m_Outputs[i]->Display();
  }
  return true;
}

ResourceId ReplayController::GetTargetShaderReplacement(ResourceId original)
{
  CHECK_REPLAY_THREAD();
  const auto replacement = m_GlobalShaderReplacements.find(original);
  return replacement == m_GlobalShaderReplacements.end() ? ResourceId() : replacement->second;
}

void ReplayController::RemoveReplacement(ResourceId id)
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  m_pDevice->RemoveReplacement(id);
  FatalErrorCheck();

  auto replacement = m_GlobalShaderReplacements.find(id);
  if(replacement != m_GlobalShaderReplacements.end())
    m_GlobalShaderReplacements.erase(replacement);

  // RemoveReplacement is intentionally idempotent, and an extra cache invalidation is cheaper
  // than retaining an image whose dependency was replaced outside the shader-specific map.
  MarkGlobalShaderReplacementChanged();

  SetFrameEvent(m_EventID, true);

  for(size_t i = 0; i < m_Outputs.size(); i++)
    if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
      m_Outputs[i]->Display();
}

rdcstr ReplayController::RemoveTargetShaderReplacementChecked(ResourceId id)
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  if(id == ResourceId())
    return "The original shader resource must be non-null";

  rdcstr error = m_pDevice->RemoveTargetShaderReplacementChecked(id);
  FatalErrorCheck();

  if(!error.empty())
    return error;

  auto replacement = m_GlobalShaderReplacements.find(id);
  if(replacement != m_GlobalShaderReplacements.end())
  {
    m_GlobalShaderReplacements.erase(replacement);
    MarkGlobalShaderReplacementChanged();
  }

  {
    ScopedCheckedShaderReplay mutation(m_pDevice);
    SetFrameEvent(m_EventID, true);
  }

  for(size_t i = 0; i < m_Outputs.size(); i++)
    if(m_Outputs[i]->GetType() != ReplayOutputType::Headless)
      m_Outputs[i]->Display();

  return {};
}

void ReplayController::ClearReplayCache()
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  // Re-fetching invalidated shader feedback can execute the target action. A later commit must
  // establish the canonical pre-draw state again instead of reusing the previous scrub result.
  InvalidateScrubReplay();
  m_pDevice->ClearReplayCache();
}

void ReplayController::ReloadShaderDebugInformation()
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  InvalidateScrubReplay();
  m_pDevice->ReloadShaderDebugInformation();
}

RDResult ReplayController::CreateDevice(RDCFile *rdc, const ReplayOptions &opts)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  IReplayDriver *driver = NULL;
  RDResult result = RenderDoc::Inst().CreateReplayDriver(rdc, opts, &driver);

  if(driver && result == ResultCode::Succeeded)
  {
    RDCLOG("Created replay driver.");
    return PostCreateInit(driver, rdc);
  }

  RDCERR("Couldn't create a replay device.");
  return result;
}

RDResult ReplayController::SetDevice(IReplayDriver *device)
{
  CHECK_REPLAY_THREAD();

  if(device)
  {
    RDCLOG("Got replay driver.");
    return PostCreateInit(device, NULL);
  }

  RDCERR("Given invalid replay driver.");
  return ResultCode::InternalError;
}

RDResult ReplayController::PostCreateInit(IReplayDriver *device, RDCFile *rdc)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  m_pDevice = device;

  m_APIProps = m_pDevice->GetAPIProperties();

  GCNISA::CacheSupport(m_APIProps.pipelineType);

  RDResult result = m_pDevice->ReadLogInitialisation(rdc, false);
  FatalErrorCheck();
  if(m_FatalError != ResultCode::Succeeded)
    return m_FatalError;

  m_pDevice->SetPipelineStates(&m_D3D11PipelineState, &m_D3D12PipelineState, &m_GLPipelineState,
                               &m_VulkanPipelineState);

  GCNISA::GetTargets(m_APIProps.pipelineType, m_pDevice->GetDriverInfo(), m_GCNTargets);

  if(result != ResultCode::Succeeded)
    return result;

  // ReadLogInitialisation leaves the replay device at the end of the captured frame, matching the
  // controller's initial sentinel EID until qrenderdoc selects a concrete action.
  m_MaterializedEventID = m_EventID;
  m_MaterializedEventValid = true;

  m_Buffers = m_pDevice->GetBuffers();
  FatalErrorCheck();
  m_Textures = m_pDevice->GetTextures();
  FatalErrorCheck();
  m_Resources = m_pDevice->GetResources();
  FatalErrorCheck();
  m_DescriptorStores = m_pDevice->GetDescriptorStores();
  FatalErrorCheck();

  m_FrameRecord = m_pDevice->GetFrameRecord();
  FatalErrorCheck();

  if(m_FatalError != ResultCode::Succeeded)
    return m_FatalError;

  if(m_FrameRecord.actionList.empty())
    return ResultCode::APIReplayFailed;

  std::unordered_set<uint32_t> knownEventIds;
  uint32_t maxEventId = 0;

  rdcarray<ActionDescription> actions(m_FrameRecord.actionList);
  while(!actions.empty())
  {
    const ActionDescription action = actions.back();
    actions.pop_back();
    maxEventId = RDCMAX(maxEventId, action.eventId);
    for(const APIEvent &event : action.events)
    {
      uint32_t eid = event.eventId;
      maxEventId = RDCMAX(maxEventId, eid);
      if(knownEventIds.count(eid) > 0)
        RDCERR("Duplicated EventId: %d", eid);
      knownEventIds.insert(eid);
    }
    actions.append(action.children);
  }
  for(uint32_t eid = 1; eid <= maxEventId; ++eid)
  {
    if(knownEventIds.count(eid) == 0)
      RDCERR("Missing EventId: %d Max: %d", eid, maxEventId);
  }

  m_Actions.clear();
  SetupActionPointers(m_Actions, m_FrameRecord.actionList);

  if(rdc)
    for(int section = 0; section < rdc->NumSections(); section++)
      m_CheckpointCaptureBytes += rdc->GetSectionProperties(section).uncompressedSize;
  ConfigureInitialReplayCheckpoints();

  FetchPipelineState(m_Actions.back()->eventId);
  FatalErrorCheck();

  return m_FatalError;
}

static const uint32_t CheckpointUnmeasuredHitLimit = 4;
static const uint32_t CheckpointPrewarmTrialRequestLimit = 12;

static void PlanReplayCheckpointCandidates(const rdcarray<ActionDescription *> &actions,
                                           uint64_t resourceBytes, uint64_t captureBytes,
                                           ReplayCheckpointConfig &config,
                                           rdcarray<ReplayCheckpointCandidate> &planned)
{
  // Plan from captured API evidence, not engine-specific marker names. These are hints: only the
  // driver knows whether a command-buffer/queue/render-pass boundary can actually be resumed.
  planned.clear();
  uint32_t lastEvent = 0;
  for(const ActionDescription *action : actions)
    if(action && !action->IsFakeMarker())
      lastEvent = RDCMAX(lastEvent, action->eventId);

  const uint64_t mib = 1024ULL * 1024;
  config.byteBudget = RDCMIN(512 * mib, RDCMAX(64 * mib, resourceBytes / 4 + captureBytes / 16));
  config.maxCheckpoints = lastEvent < 256 ? 2 : 3;
  config.minEventGap = RDCMAX(1U, RDCMIN(2048U, lastEvent / 32));

  const ActionDescription *previous = NULL;
  uint32_t module = 0, moduleStart = 0, lastCandidate = 0;
  rdcarray<ReplayCheckpointCandidate> candidates;
  for(const ActionDescription *action : actions)
  {
    if(!action || action->IsFakeMarker() || !action->children.empty() ||
       !(action->flags & (ActionFlags::Drawcall | ActionFlags::Dispatch | ActionFlags::DispatchRay |
                          ActionFlags::Clear | ActionFlags::Copy | ActionFlags::Resolve)))
      continue;

    bool moduleChanged = false;
    if(previous)
    {
      const ActionFlags kind =
          ActionFlags::Drawcall | ActionFlags::Dispatch | ActionFlags::DispatchRay;
      moduleChanged = (previous->flags & kind) != (action->flags & kind) ||
                      previous->depthOut != action->depthOut;
      for(size_t slot = 0; slot < action->outputs.size(); slot++)
        moduleChanged |= previous->outputs[slot] != action->outputs[slot];

      const bool spaced = previous->eventId - lastCandidate >= config.minEventGap;
      if(spaced && (moduleChanged || previous->eventId - lastCandidate >= config.minEventGap * 2))
      {
        ReplayCheckpointCandidate candidate;
        candidate.eventId = previous->eventId;
        candidate.moduleId = module;
        // Favour the end of a long attachment group within each region of the frame.
        candidate.priority = (moduleChanged ? 2U : 1U) * config.minEventGap +
                             RDCMIN(previous->eventId - moduleStart, config.minEventGap * 4);
        candidates.push_back(candidate);
        lastCandidate = previous->eventId;
      }
    }
    if(!previous || moduleChanged)
    {
      module++;
      moduleStart = action->eventId;
    }
    previous = action;
  }

  // Bound driver planning cost without concentrating every candidate in one busy pass.
  const uint32_t bucketWidth = RDCMAX(1U, uint32_t((uint64_t(lastEvent) + 63) / 64));
  for(const ReplayCheckpointCandidate &candidate : candidates)
  {
    if(!planned.empty() && planned.back().eventId / bucketWidth == candidate.eventId / bucketWidth)
    {
      if(candidate.priority > planned.back().priority)
        planned.back() = candidate;
    }
    else
      planned.push_back(candidate);
  }
}

void ReplayController::PlanReplayCheckpoints()
{
  uint64_t resourceBytes = 0;
  for(const BufferDescription &buffer : m_Buffers)
    resourceBytes += buffer.length;
  for(const TextureDescription &texture : m_Textures)
    resourceBytes += texture.byteSize;
  m_CheckpointResourceBytes = resourceBytes;
  PlanReplayCheckpointCandidates(m_Actions, resourceBytes, m_CheckpointCaptureBytes,
                                 m_CheckpointConfig, m_CheckpointCandidates);
}

void ReplayController::CheckpointReferenceCost::Add(double ms)
{
  // Reject non-finite/outlier timer results without turning missing evidence into a zero cost.
  if(!(ms > 0.0 && ms < 60000.0))
    return;
  milliseconds[next] = ms;
  next = (next + 1) % 2;
  count = RDCMIN(count + 1, 2U);
  hitsSinceReference = 0;
}

double ReplayController::CheckpointReferenceCost::Baseline() const
{
  return count == 0 ? 0.0 : count == 1 ? milliseconds[0] : RDCMIN(milliseconds[0], milliseconds[1]);
}

void ReplayController::ConfigureInitialReplayCheckpoints()
{
  const rdcstr mode = Process::GetEnvVariable("RENDERDOC_REPLAY_CHECKPOINTS");
  if(!mode.empty() && mode != "auto")
  {
    ConfigureReplayCheckpoints(mode == "1");
    m_CheckpointControlMode = mode == "1" ? "environmentOn" : "environmentOff";
    return;
  }
  PlanReplayCheckpoints();
  m_CheckpointAutomatic = true;
  m_CheckpointIdlePrewarm = Process::GetEnvVariable("RENDERDOC_REPLAY_DISABLE_IDLE_PREWARM") != "1";
  m_CheckpointControlMode = "automatic";
  m_CheckpointAutoCapacity = m_CheckpointConfig.maxCheckpoints;
  m_CheckpointConfig.byteBudget =
      RDCMIN<uint64_t>(m_CheckpointConfig.byteBudget, 512ULL * 1024 * 1024);
  m_CheckpointConfig.enabled = false;
  m_pDevice->ConfigureReplayCheckpoints(m_CheckpointConfig, {});
  const ReplayCheckpointStats stats = m_pDevice->GetReplayCheckpointStats();
  if(!stats.supported)
    m_CheckpointAutoReason = "backendUnsupported";
  // Initialisation only plans CPU metadata. After the first image is published, the idle queue
  // may build one bounded prefix at a time; native safety and memory checks remain authoritative.
}

void ReplayController::ResetAutomaticCheckpointCosts(uint32_t firstAffectedEvent)
{
  // Also invalidates idle receipts in explicit mode, including FileChanged where the public
  // render-output generation need not change. No previously measured hint is resurrected here.
  ++m_IdleCheckpointRevision;
  m_MemoryRejectedCheckpointCandidates.clear();
  m_IdleCheckpointMemoryNextCheck = 0;
  if(!m_CheckpointAutomatic)
    return;
  for(auto it = m_CheckpointReferenceCosts.begin(); it != m_CheckpointReferenceCosts.end();)
    if(firstAffectedEvent == 0 || uint32_t(it->first >> 3) >= firstAffectedEvent)
      it = m_CheckpointReferenceCosts.erase(it);
    else
      ++it;
  for(size_t i = 0; i < m_AutomaticCheckpointPoints.size();)
  {
    AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[i];
    // A requested module boundary can fall back to a much earlier native submission. Mutations
    // invalidate actual saved versions, not that original request's EID. Retain an independently
    // proved upstream point and clamp its future planning request to the unaffected prefix.
    if(firstAffectedEvent)
      for(auto it = point.nativeEvents.lower_bound(firstAffectedEvent);
          it != point.nativeEvents.end();)
        it = point.nativeEvents.erase(it);
    const bool requestedSuffix = point.candidate.eventId >= firstAffectedEvent;
    if(firstAffectedEvent == 0 || (requestedSuffix && point.nativeEvents.empty()))
    {
      m_AutomaticCheckpointPoints.erase(i);
      m_CheckpointAutoConfigurationDirty = true;
    }
    else
    {
      if(requestedSuffix)
        point.candidate.eventId = firstAffectedEvent - 1;
      // The native upstream version survives. New downstream shader work must establish a new
      // same-event baseline before its benefit is assumed to survive as well.
      point.retained = false;
      point.hits = point.profitableHits = point.requests = 0;
      point.unmeasuredHits = 0;
      ++i;
    }
  }
  for(auto it = m_RejectedCheckpointCandidates.begin(); it != m_RejectedCheckpointCandidates.end();)
    if(firstAffectedEvent == 0 || *it >= firstAffectedEvent)
      it = m_RejectedCheckpointCandidates.erase(it);
    else
      ++it;
  m_CheckpointAutoTrials = 0;
  m_CheckpointAutoState = "observing";
  m_CheckpointAutoReason = "mutationRequiresNewCostEvidence";
  // Defer configuration until a natural request. A shader editor's forced replay must neither
  // build/restore nor mutate checkpoint statistics via automatic reconfiguration.
}

void ReplayController::ApplyAutomaticCheckpointConfiguration()
{
  if(!m_CheckpointAutoConfigurationDirty)
    return;
  rdcarray<ReplayCheckpointCandidate> candidates;
  for(const AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
    candidates.push_back(point.candidate);
  std::sort(candidates.begin(), candidates.end(),
            [](const ReplayCheckpointCandidate &a, const ReplayCheckpointCandidate &b) {
              return a.eventId < b.eventId;
            });
  // Keep a measured memory rejection available to the idle-only recovery probe. An empty policy
  // admits no native point; explicit disable still cancels this state below.
  m_CheckpointConfig.enabled = !candidates.empty() || !m_MemoryRejectedCheckpointCandidates.empty();
  // Same budget/capacity candidate extensions preserve native versions and cumulative stats.
  // Removing a rejected trial preserves the intersection; only disable/invalidation resets all.
  m_pDevice->ConfigureReplayCheckpoints(m_CheckpointConfig, candidates);
  m_CheckpointAutoConfigurationDirty = false;
}

ReplayController::AutomaticCheckpointRequest ReplayController::BeginAutomaticCheckpointRequest(
    uint32_t eventId, uint32_t path)
{
  AutomaticCheckpointRequest request;
  if(m_IdleCheckpointMaintenance || !m_CheckpointAutomatic || !m_pDevice ||
     m_pDevice->m_ReplayCheckpointBypass || m_FatalError != ResultCode::Succeeded ||
     HasEventOverrides(eventId))
    return request;
  request.before = m_pDevice->GetReplayCheckpointStats();
  if(!request.before.supported)
    return request;
  request.active = true;
  request.previousBypass = m_pDevice->m_ReplayCheckpointBypass;
  request.previousNaturalReference = m_pDevice->m_ReplayCheckpointNaturalReference;
  request.key = (uint64_t(eventId) << 3) | path;
  if(m_CheckpointReferenceCosts.size() >= 128 &&
     m_CheckpointReferenceCosts.find(request.key) == m_CheckpointReferenceCosts.end())
    m_CheckpointReferenceCosts.erase(m_CheckpointReferenceCosts.begin());
  CheckpointReferenceCost &reference = m_CheckpointReferenceCosts[request.key];
  const bool bufferEdit = (path & 4) != 0;
  const uint32_t requiredReferences = bufferEdit ? 1 : 2;
  const double baseline = reference.Baseline();
  const bool stable = reference.count < 2 || RDCMAX(reference.milliseconds[0],
                                                    reference.milliseconds[1]) <= baseline * 1.25;
  request.reference =
      reference.count < requiredReferences || !stable || reference.hitsSinceReference >= 8;
  // Retained points already earned their storage through independent hit/reference evidence.
  // Repeating a slower reference every eight interactions is an audit policy, not a correctness
  // requirement. Resource/version validation still runs in the driver on every actual restore.
  if(reference.count >= requiredReferences && stable)
    for(const AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
      if(point.retained)
        for(uint32_t nativeEvent : point.nativeEvents)
          if(nativeEvent < eventId && request.before.events.contains(nativeEvent))
            request.reference = false;
  // Give an idle-built point a short initial trial, counted across EIDs rather than per key.
  // Once any eligible point exhausts that grace, the user's next request is the ordinary
  // reference itself. No extra replay is submitted, and continuously new EIDs cannot renew it.
  if(reference.count == 0 && reference.hitsSinceReference < 8)
  {
    bool prewarmedPrefix = false, unmeasuredLimit = false;
    for(const AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
      if(point.prewarmed && point.candidate.eventId < eventId)
        for(uint32_t nativeEvent : point.nativeEvents)
          if(nativeEvent < eventId && request.before.events.contains(nativeEvent))
          {
            prewarmedPrefix = true;
            unmeasuredLimit |= point.unmeasuredHits >= CheckpointUnmeasuredHitLimit;
          }
    if(prewarmedPrefix && !unmeasuredLimit)
      request.reference = false;
  }

  bool pendingTrial = false, unpaid = false;
  for(const AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
  {
    pendingTrial |= !point.retained;
    unpaid |= point.retained && point.debtMS > 0.0;
  }
  if(!request.reference && !pendingTrial && !unpaid &&
     m_AutomaticCheckpointPoints.size() < m_CheckpointAutoCapacity &&
     m_CheckpointAutoTrials < m_CheckpointAutoCapacity * 2 && (bufferEdit || baseline >= 16.0))
  {
    ReplayCheckpointCandidate selected;
    bool found = false;
    rdcarray<ReplayCheckpointCandidate> possible = m_CheckpointCandidates;
    const uint32_t firstConsumer =
        bufferEdit ? m_pDevice->GetReplayBufferPatchFirstAffectedEvent() : 0;
    if(firstConsumer > 0 && firstConsumer <= eventId)
    {
      // This cutoff comes from the driver's complete native consumer index, not GetUsage or EID
      // distance. The backend still chooses/validates the actual resumable boundary before it.
      ReplayCheckpointCandidate prefix;
      prefix.eventId = firstConsumer - 1;
      prefix.moduleId = ~0U;
      possible.clear();
      possible.push_back(prefix);
    }
    for(const ReplayCheckpointCandidate &candidate : possible)
    {
      if(candidate.eventId >= eventId || m_RejectedCheckpointCandidates.count(candidate.eventId))
        continue;
      bool overlaps = false;
      for(const AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
      {
        const uint32_t distance = candidate.eventId > point.candidate.eventId
                                      ? candidate.eventId - point.candidate.eventId
                                      : point.candidate.eventId - candidate.eventId;
        overlaps |= point.candidate.eventId == candidate.eventId ||
                    (!bufferEdit && (point.candidate.moduleId == candidate.moduleId ||
                                     distance < m_CheckpointConfig.minEventGap * 4));
      }
      if(!overlaps && (!found || candidate.eventId > selected.eventId))
      {
        selected = candidate;
        found = true;
      }
    }
    if(found)
    {
      // Keep the capture's bounded storage budget. A temporary parameter-edit trial must not
      // permanently shrink it and evict every useful existing upstream point. Native admission
      // still caps total storage by this budget and current GPU headroom.
      AutomaticCheckpointPoint point;
      point.candidate = selected;
      point.bufferEdit = bufferEdit;
      point.baselineMS = baseline;
      m_AutomaticCheckpointPoints.push_back(point);
      m_CheckpointAutoTrials++;
      m_CheckpointAutoState = "trial";
      m_CheckpointAutoReason = bufferEdit ? "measuringFirstConsumerPrefix" : "measuringNaturalVisits";
      m_CheckpointAutoConfigurationDirty = true;
    }
  }
  else if(!request.reference && m_AutomaticCheckpointPoints.empty() && !bufferEdit && baseline < 16.0)
    m_CheckpointAutoReason = "ordinaryReplayBelowTrialThreshold";

  if(m_AutomaticCheckpointPoints.empty())
    request.reference = true;

  PerformanceTimer configurationTimer;
  const bool configured = m_CheckpointAutoConfigurationDirty;
  ApplyAutomaticCheckpointConfiguration();
  request.configurationMS = configured ? configurationTimer.GetMilliseconds() : 0.0;
  request.before = m_pDevice->GetReplayCheckpointStats();
  // A reference uses the user's already-requested replay, preserving native points and stats.
  // It is never SetFrameEvent(force), a second replay, a readback, or a new GPU synchronisation.
  m_pDevice->m_ReplayCheckpointBypass = request.reference;
  m_pDevice->m_ReplayCheckpointNaturalReference = request.reference;
  return request;
}

void ReplayController::ObserveAutomaticCheckpointRequest(const AutomaticCheckpointRequest &request,
                                                         double driverMS,
                                                         const ReplayCheckpointStats &after)
{
  if(!request.active)
    return;
  const uint32_t target = uint32_t(request.key >> 3);
  // Every eligible trial ages, including references and trials hidden behind a newer point.
  // Otherwise a stream of unique EIDs (or an unused older point) could retain unproven storage
  // forever. A successful measured retention below is still allowed on the last trial request.
  for(AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
    if(!point.retained && point.candidate.eventId < target)
      ++point.requests;
  const auto retireExpiredTrials = [&]() {
    for(size_t i = 0; i < m_AutomaticCheckpointPoints.size();)
    {
      const AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[i];
      const uint32_t limit = point.prewarmed ? CheckpointPrewarmTrialRequestLimit : 6U;
      if(!point.retained && point.requests >= limit)
      {
        m_RejectedCheckpointCandidates.insert(point.candidate.eventId);
        m_AutomaticCheckpointPoints.erase(i);
        m_CheckpointAutoConfigurationDirty = true;
        m_CheckpointAutoState = "rejected";
        m_CheckpointAutoReason = "boundedTrialExpired";
      }
      else
        ++i;
    }
  };
  // Even an unusually slow request or an invalid timer sample consumes the finite trial.
  // Such samples must not train a baseline or claim any savings.
  if(!(driverMS > 0.0 && driverMS < 60000.0))
  {
    retireExpiredTrials();
    return;
  }
  CheckpointReferenceCost &reference = m_CheckpointReferenceCosts[request.key];
  m_CheckpointAutoDebtMS += request.configurationMS;
  if(request.reference)
  {
    reference.Add(driverMS);
    m_CheckpointAutoReferences++;
    retireExpiredTrials();
    return;
  }
  const double baseline = reference.Baseline();
  const uint64_t builds =
      after.builds >= request.before.builds ? after.builds - request.before.builds : 0;
  const uint64_t hits = after.hits >= request.before.hits ? after.hits - request.before.hits : 0;
  int trial = -1, owner = -1;
  for(size_t i = 0; i < m_AutomaticCheckpointPoints.size(); i++)
  {
    AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[i];
    if(!point.retained && point.candidate.eventId < target)
      trial = int(i);
    if(hits && point.nativeEvents.count(after.lastResumeEvent))
      owner = int(i);
  }
  if(trial >= 0)
  {
    AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[trial];
    point.builds += uint32_t(builds);
    point.debtMS += request.configurationMS;
    for(uint32_t nativeEvent : after.events)
    {
      if(!request.before.events.contains(nativeEvent))
        point.nativeEvents.insert(nativeEvent);
    }
    if(hits && point.nativeEvents.count(after.lastResumeEvent))
      owner = trial;
    if(builds)
    {
      // Only the incremental wall penalty is attributable here; this is not a GPU build timer.
      const double penalty = RDCMAX(0.0, driverMS - baseline);
      point.debtMS += penalty;
      m_CheckpointAutoDebtMS += penalty;
    }
  }
  else if(builds)
  {
    // A retained point may need rebuilding after native eviction/invalidation. Its cost is not
    // free merely because this is no longer the first trial that created the same EID.
    for(AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
      if(point.nativeEvents.count(after.lastSaveEvent))
      {
        const double penalty = RDCMAX(0.0, driverMS - baseline);
        point.builds += uint32_t(builds);
        point.debtMS += penalty + request.configurationMS;
        m_CheckpointAutoDebtMS += penalty;
        point.retained = false;
        point.hits = point.profitableHits = point.requests = 0;
        point.unmeasuredHits = 0;
        break;
      }
  }
  bool reject = false;
  int rejectIndex = -1;
  if(owner >= 0 && builds == 0 && baseline == 0.0)
  {
    AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[owner];
    point.hits++;
    point.unmeasuredHits = RDCMIN(point.unmeasuredHits + 1, CheckpointUnmeasuredHitLimit);
    point.lastHitMS = driverMS;
    reference.hitsSinceReference++;
  }
  if(owner >= 0 && builds == 0 && baseline > 0.0)
  {
    AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[owner];
    // A new trial owns the configuration cost already charged to its debt above. Its addition
    // must not make a different, retained native checkpoint appear to have a slower restore.
    // Calls that also build remain excluded from clean hit evidence by the guard above.
    const double cost = driverMS + ((trial >= 0 && owner != trial) ? 0.0 : request.configurationMS);
    point.hits++;
    point.lastHitMS = cost;
    point.baselineMS = baseline;
    point.unmeasuredHits = 0;
    reference.hitsSinceReference++;
    const double gain = baseline - cost;
    const double minimumGain = RDCMAX(point.bufferEdit ? 0.05 : 2.0, baseline * 0.10);
    if(gain >= minimumGain)
    {
      point.profitableHits++;
      point.debtMS = RDCMAX(0.0, point.debtMS - gain);
      m_CheckpointAutoSavingsMS += gain;
      if(point.profitableHits >= 2 && point.debtMS <= gain * 8.0)
        point.retained = true;
    }
    else if(gain < -minimumGain || point.hits >= 3)
    {
      reject = true;
      rejectIndex = owner;
      m_CheckpointAutoReason =
          gain < -minimumGain ? "restoreRegressedReference" : "insufficientNetBenefit";
    }
  }
  if(trial >= 0 && !reject)
  {
    const AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[trial];
    const uint32_t limit = point.prewarmed ? CheckpointPrewarmTrialRequestLimit : 6U;
    if(!point.retained && (point.requests >= limit || (point.requests >= 2 && point.builds == 0)))
    {
      reject = true;
      rejectIndex = trial;
      m_CheckpointAutoReason = point.hits == 0 ? "noIndependentNativeHit" : "buildCostNotAmortised";
    }
  }
  if(reject)
  {
    m_RejectedCheckpointCandidates.insert(m_AutomaticCheckpointPoints[rejectIndex].candidate.eventId);
    m_AutomaticCheckpointPoints.erase(rejectIndex);
    // Applied before the next eligible normal request. No rejected candidate is replayed again.
    m_CheckpointAutoConfigurationDirty = true;
  }
  retireExpiredTrials();
  m_CheckpointAutoState = m_RejectedCheckpointCandidates.empty() ? "observing" : "rejected";
  for(const AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
    if(point.retained)
      m_CheckpointAutoState = "retained";
  for(const AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
    if(!point.retained)
      m_CheckpointAutoState = "trial";
  if(!reject && m_AutomaticCheckpointPoints.size())
    m_CheckpointAutoReason = strcmp(m_CheckpointAutoState, "retained") == 0
                                 ? "measuredBenefitRetained"
                                 : "awaitingIndependentHitEvidence";
}

void ReplayController::EndAutomaticCheckpointRequest(const AutomaticCheckpointRequest &request,
                                                     double driverMS)
{
  if(!request.active)
    return;
  m_pDevice->m_ReplayCheckpointBypass = request.previousBypass;
  m_pDevice->m_ReplayCheckpointNaturalReference = request.previousNaturalReference;
  if(m_FatalError != ResultCode::Succeeded)
    return;
  const ReplayCheckpointStats after = m_pDevice->GetReplayCheckpointStats();
  ObserveAutomaticCheckpointRequest(request, driverMS, after);
  if(EIDTimingEnabled())
  {
    const rdcstr line = StringFormat::Fmt(
        "api=ReplayCheckpointAuto eid=%u path=%u reference=%u driver_ms=%.3f configuration_ms=%.3f "
        "baseline_ms=%.3f builds=%llu hits=%llu state=%s reason=%s debt_ms=%.3f savings_ms=%.3f",
        uint32_t(request.key >> 3), uint32_t(request.key & 7), request.reference ? 1U : 0U,
        driverMS, request.configurationMS, m_CheckpointReferenceCosts[request.key].Baseline(),
        after.builds >= request.before.builds ? after.builds - request.before.builds : 0,
        after.hits >= request.before.hits ? after.hits - request.before.hits : 0,
        m_CheckpointAutoState, m_CheckpointAutoReason, m_CheckpointAutoDebtMS,
        m_CheckpointAutoSavingsMS);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

void ReplayController::ConfigureReplayCheckpoints(bool enabled, uint64_t byteBudget,
                                                  uint32_t maxCheckpoints)
{
  m_CheckpointIdlePrewarm = false;
  CHECK_REPLAY_THREAD();
  ++m_IdleCheckpointRevision;
  m_CheckpointAutomatic = false;
  m_CheckpointControlMode = enabled ? "explicitOn" : "explicitOff";
  m_AutomaticCheckpointPoints.clear();
  m_CheckpointReferenceCosts.clear();
  m_RejectedCheckpointCandidates.clear();
  m_MemoryRejectedCheckpointCandidates.clear();
  m_IdleCheckpointMemoryNextCheck = 0;
  m_IdleCheckpointMemoryChecks = m_IdleCheckpointMemoryRecoveries = 0;
  m_CheckpointAutoConfigurationDirty = false;
  PlanReplayCheckpoints();
  m_CheckpointConfig.enabled = enabled;
  if(byteBudget)
    m_CheckpointConfig.byteBudget = byteBudget;
  if(maxCheckpoints)
    m_CheckpointConfig.maxCheckpoints = RDCMIN(maxCheckpoints, 64U);
  m_pDevice->ConfigureReplayCheckpoints(m_CheckpointConfig, m_CheckpointCandidates);
}

rdcstr ReplayController::RunIdleCheckpointMaintenance(uint32_t expectedEventId,
                                                      uint64_t expectedMutationGeneration,
                                                      uint64_t expectedBufferGeneration)
{
  CHECK_REPLAY_THREAD();
  PerformanceTimer timer;
  bool performed = false, candidateSelected = false;
  bool planRejected = false, tryAnotherCandidate = false, continueIdle = false;
  bool memoryPlanBlocked = false;
  bool candidateRetired = false;
  rdcstr planningReason;
  uint32_t candidateEvent = 0;
  uint32_t retryIdleAfterMS = 0;
  double configurationMS = 0.0, replayMS = 0.0;
  ReplayCheckpointStats before, after;
  const auto receipt = [&](const char *reason) {
    rdcstr backendReason;
    for(char c : after.reason)
    {
      if(c == '"' || c == '\\')
        backendReason += '\\';
      if((unsigned char)c >= 0x20)
        backendReason += c;
    }
    return StringFormat::Fmt(
        "{\"schema\":\"renderdoc-replay-checkpoint-maintenance/v1\",\"performed\":%s,"
        "\"reason\":\"%s\",\"expectedEventId\":%u,\"currentEventId\":%u,"
        "\"mutationGeneration\":%llu,\"bufferGeneration\":%llu,\"configurationRevision\":%llu,"
        "\"candidateSelected\":%s,\"candidateEventId\":%u,\"workUnits\":%u,"
        "\"builds\":%llu,\"hits\":%llu,\"checkpointCountBefore\":%u,\"checkpointCountAfter\":%u,"
        "\"lastSaveEvent\":%u,\"lastResumeEvent\":%u,\"hasInitialCheckpoint\":%s,"
        "\"materialized\":%s,\"configurationMS\":%.6f,\"replayMS\":%.6f,\"totalMS\":%.6f,"
        "\"continueIdle\":%s,\"retryIdleAfterMS\":%u,\"memoryChecks\":%u,"
        "\"memoryRecoveries\":%u,\"backendReason\":\"%s\"}",
        performed ? "true" : "false", reason, expectedEventId, m_EventID,
        GetShaderReplacementGeneration(m_EventID), m_SharedBufferPatchGeneration,
        m_IdleCheckpointRevision, candidateSelected ? "true" : "false", candidateEvent,
        performed ? 1U : 0U, after.builds >= before.builds ? after.builds - before.builds : 0,
        after.hits >= before.hits ? after.hits - before.hits : 0, before.checkpointCount,
        after.checkpointCount, after.lastSaveEvent, after.lastResumeEvent,
        after.hasInitialCheckpoint ? "true" : "false",
        IsCurrentEventMaterialized() ? "true" : "false", configurationMS, replayMS,
        timer.GetMilliseconds(), continueIdle ? "true" : "false", retryIdleAfterMS,
        m_IdleCheckpointMemoryChecks, m_IdleCheckpointMemoryRecoveries, backendReason.c_str());
  };

  if(m_IdleCheckpointMaintenance)
    return receipt("alreadyRunning");
  if(!m_pDevice || m_FatalError != ResultCode::Succeeded)
    return receipt("driverUnavailable");
  if(expectedEventId != m_EventID)
    return receipt("staleEvent");
  if(expectedMutationGeneration != GetShaderReplacementGeneration(expectedEventId))
    return receipt("staleMutation");
  if(expectedBufferGeneration != m_SharedBufferPatchGeneration)
    return receipt("staleBufferGeneration");
  if(m_AnalysisReplayState != AnalysisReplayState::None || m_AnalysisEventOverridesActive)
    return receipt("analysisActive");
  if(!IsCurrentEventMaterialized())
    return receipt("currentEventNotMaterialized");
  if(HasEventOverrides(expectedEventId))
    return receipt("eventOverridesActive");
  const bool prewarm = m_CheckpointAutomatic && m_CheckpointIdlePrewarm;
  if((!m_CheckpointConfig.enabled && !prewarm) || !m_CheckpointConfig.byteBudget ||
     !m_CheckpointConfig.maxCheckpoints)
    return receipt("checkpointsDisabled");
  if(m_pDevice->m_ReplayCheckpointBypass)
    return receipt("checkpointBypassActive");
  if(prewarm && !m_MemoryRejectedCheckpointCandidates.empty())
  {
    // Only idle requests with current event/edit identities reach this CPU-only probe. A caller
    // that ignores the UI timer cannot turn this into a busy loop or spend extra native queries.
    before = after = m_pDevice->GetReplayCheckpointStats();
    if(!before.memoryBudgetBlocked)
    {
      m_MemoryRejectedCheckpointCandidates.clear();
      m_IdleCheckpointMemoryNextCheck = 0;
    }
    else if(m_IdleCheckpointMemoryChecks >= 16 || m_IdleCheckpointMemoryRecoveries >= 2 ||
            m_CheckpointAutoTrials >= m_CheckpointAutoCapacity * 2)
      return receipt("memoryBudgetRetryLimit");
    else
    {
      const uint64_t now = Timing::GetTick();
      if(now < m_IdleCheckpointMemoryNextCheck)
      {
        retryIdleAfterMS =
            RDCMAX(750U, RDCMIN(5000U, uint32_t(double(m_IdleCheckpointMemoryNextCheck - now) /
                                                    Timing::GetTickFrequency() +
                                                1.0)));
        return receipt("memoryBudgetWaiting");
      }
      ++m_IdleCheckpointMemoryChecks;
      if(m_pDevice->RefreshReplayCheckpointMemoryBudget())
      {
        // A budget recovery never forgives a safety, allocation, or profitability rejection.
        // Re-run full native admission for just the candidates rejected before such analysis.
        for(uint32_t event : m_MemoryRejectedCheckpointCandidates)
          m_RejectedCheckpointCandidates.erase(event);
        m_MemoryRejectedCheckpointCandidates.clear();
        m_IdleCheckpointMemoryNextCheck = 0;
        ++m_IdleCheckpointMemoryRecoveries;
        ++m_IdleCheckpointRevision;
        m_IdleCheckpointAttempted = false;
      }
      else
      {
        m_IdleCheckpointMemoryNextCheck = now + uint64_t(5000.0 * Timing::GetTickFrequency());
        if(m_IdleCheckpointMemoryChecks < 16)
          retryIdleAfterMS = 5000;
        return receipt(retryIdleAfterMS ? "memoryBudgetWaiting" : "memoryBudgetRetryLimit");
      }
    }
  }
  if(m_IdleCheckpointAttempted && m_IdleCheckpointLastEvent == expectedEventId &&
     m_IdleCheckpointLastMutation == expectedMutationGeneration &&
     m_IdleCheckpointLastBuffer == expectedBufferGeneration &&
     m_IdleCheckpointLastRevision == m_IdleCheckpointRevision)
    return receipt("alreadyAttempted");

  before = after = m_pDevice->GetReplayCheckpointStats();
  if(!before.supported || (!before.enabled && !prewarm))
    return receipt("backendDisabledOrUnsupported");
  bool hasPrefix = false;
  uint32_t newestPrefix = 0;
  for(uint32_t event : before.events)
    if(event < m_EventID && (event > 0 || before.hasInitialCheckpoint))
    {
      hasPrefix = true;
      newestPrefix = RDCMAX(newestPrefix, event);
    }
  const auto eligiblePosition = [&](uint32_t event) {
    return event < m_EventID && !before.events.contains(event) &&
           (!hasPrefix || event > newestPrefix) && !m_RejectedCheckpointCandidates.count(event);
  };
  const auto nativePresent = [&](const AutomaticCheckpointPoint &point) {
    for(uint32_t nativeEvent : point.nativeEvents)
      if(before.events.contains(nativeEvent) && (nativeEvent > 0 || before.hasInitialCheckpoint))
        return true;
    return false;
  };
  int owner = -1;
  rdcarray<ReplayCheckpointCandidate> policy, maintenance;
  if(m_CheckpointAutomatic)
  {
    for(size_t i = 0; i < m_AutomaticCheckpointPoints.size(); ++i)
    {
      const AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[i];
      policy.push_back(point.candidate);
      const bool present = nativePresent(point);
      if(present)
        maintenance.push_back(point.candidate);
      if(!present && point.retained && point.profitableHits >= 2 && !point.nativeEvents.empty() &&
         point.lastHitMS > 0.0 && point.baselineMS > point.lastHitMS &&
         eligiblePosition(point.candidate.eventId) &&
         (!candidateSelected || point.candidate.eventId > candidateEvent))
      {
        candidateSelected = true;
        candidateEvent = point.candidate.eventId;
        owner = int(i);
      }
    }
    if(!candidateSelected && prewarm &&
       m_AutomaticCheckpointPoints.size() < m_CheckpointAutoCapacity &&
       m_CheckpointAutoTrials < m_CheckpointAutoCapacity * 2)
    {
      // Spread a small number of states across the already materialised prefix. Neither marker
      // strings nor captured IDs participate. Choosing one new candidate keeps each idle work
      // unit bounded and lets foreground requests run between successive units.
      const uint32_t target =
          newestPrefix + RDCMAX(1U, (m_EventID - newestPrefix) /
                                        (m_CheckpointAutoCapacity -
                                         uint32_t(m_AutomaticCheckpointPoints.size()) + 1));
      const ReplayCheckpointCandidate *selected = NULL;
      for(const ReplayCheckpointCandidate &candidate : m_CheckpointCandidates)
      {
        if(!eligiblePosition(candidate.eventId))
          continue;
        bool owned = false;
        for(const AutomaticCheckpointPoint &point : m_AutomaticCheckpointPoints)
          owned |= point.candidate.eventId == candidate.eventId;
        if(owned)
          continue;
        const uint32_t distance =
            candidate.eventId > target ? candidate.eventId - target : target - candidate.eventId;
        const uint32_t previousDistance = !selected                    ? ~0U
                                          : selected->eventId > target ? selected->eventId - target
                                                                       : target - selected->eventId;
        if(!selected || distance < previousDistance ||
           (distance == previousDistance && candidate.priority > selected->priority))
          selected = &candidate;
      }
      if(selected)
      {
        AutomaticCheckpointPoint point;
        point.candidate = *selected;
        point.prewarmed = true;
        owner = int(m_AutomaticCheckpointPoints.size());
        m_AutomaticCheckpointPoints.push_back(point);
        policy.push_back(point.candidate);
        candidateSelected = true;
        candidateEvent = point.candidate.eventId;
        ++m_CheckpointAutoTrials;
        m_CheckpointConfig.enabled = true;
        m_CheckpointAutoState = "prewarming";
        m_CheckpointAutoReason = "idleStagePrebuild";
      }
    }
    if(!candidateSelected)
      return receipt(prewarm ? "noMissingIdleCandidate" : "noMissingMeasuredUsefulPoint");
    maintenance.push_back(m_AutomaticCheckpointPoints[owner].candidate);
  }
  else
  {
    // Explicit mode already authorises this policy. There is no public candidate->native-boundary
    // map, so filtering by equal EIDs would wrongly delete mapped Vulkan/D3D12 checkpoints.
    for(const ReplayCheckpointCandidate &candidate : m_CheckpointCandidates)
      if(eligiblePosition(candidate.eventId) &&
         (!candidateSelected || candidate.eventId > candidateEvent))
      {
        candidateSelected = true;
        candidateEvent = candidate.eventId;
      }
    if(!candidateSelected)
      return receipt("noMissingExplicitCandidate");
  }

  m_IdleCheckpointAttempted = true;
  m_IdleCheckpointLastEvent = expectedEventId;
  m_IdleCheckpointLastMutation = expectedMutationGeneration;
  m_IdleCheckpointLastBuffer = expectedBufferGeneration;
  m_IdleCheckpointLastRevision = m_IdleCheckpointRevision;
  const uint32_t selectedEvent = m_SelectedEventID;
  const bool previousBypass = m_pDevice->m_ReplayCheckpointBypass;
  const bool previousReference = m_pDevice->m_ReplayCheckpointNaturalReference;
  const bool automatic = m_CheckpointAutomatic;
  const uint64_t configurationRevision = m_IdleCheckpointRevision;
  const auto requestStillCurrent = [&]() {
    return m_EventID == expectedEventId && m_SelectedEventID == selectedEvent &&
           m_IdleCheckpointRevision == configurationRevision &&
           GetShaderReplacementGeneration(expectedEventId) == expectedMutationGeneration &&
           m_SharedBufferPatchGeneration == expectedBufferGeneration;
  };
  {
    ScopedCheckpointPrefixOnly prefix(m_pDevice, false);
    m_IdleCheckpointMaintenance = true;
    struct FinishMaintenance
    {
      std::function<void()> finish;
      ~FinishMaintenance() { finish(); }
    } finish = {[&]() {
      if(automatic && m_FatalError == ResultCode::Succeeded && requestStillCurrent())
      {
        PerformanceTimer configureTimer;
        // Restore the complete authorised policy through the compatible configuration path.
        // It includes the selected point, so a successful new native version is retained.
        m_pDevice->ConfigureReplayCheckpoints(m_CheckpointConfig, policy);
        configurationMS += configureTimer.GetMilliseconds();
        FatalErrorCheck();
        if(m_FatalError == ResultCode::Succeeded)
          m_CheckpointAutoConfigurationDirty = false;
      }
      // FatalErrorCheck may have replaced the driver with a dummy during ReplayEvent.
      m_pDevice->m_ReplayCheckpointBypass = previousBypass;
      m_pDevice->m_ReplayCheckpointNaturalReference = previousReference;
      m_IdleCheckpointMaintenance = false;
    }};
    if(automatic)
    {
      PerformanceTimer configureTimer;
      m_pDevice->ConfigureReplayCheckpoints(m_CheckpointConfig, maintenance);
      configurationMS += configureTimer.GetMilliseconds();
      const ReplayCheckpointStats planned = m_pDevice->GetReplayCheckpointStats();
      if(planned.candidatePlanKnown)
      {
        bool missingNative = false;
        for(uint32_t event : planned.candidateEvents)
          missingNative |= event < m_EventID && !before.events.contains(event);
        planRejected = !missingNative;
        tryAnotherCandidate = planRejected && planned.canTryOtherCandidates;
        memoryPlanBlocked = planRejected && planned.memoryBudgetBlocked;
        planningReason = planned.reason;
      }
    }
    FatalErrorCheck();
    if(m_FatalError == ResultCode::Succeeded && requestStillCurrent() && !planRejected)
    {
      m_pDevice->m_ReplayCheckpointBypass = false;
      m_pDevice->m_ReplayCheckpointNaturalReference = false;
      PerformanceTimer replayTimer;
      performed = true;
      // Calling SetFrameEvent(same,false) would be a no-op; force/analysis would bypass saves.
      // Use the existing canonical two-phase path without touching pipeline/output publication.
      ReplayEvent(m_EventID, false, false, false);
      replayMS = replayTimer.GetMilliseconds();
    }
  }
  after = m_pDevice->GetReplayCheckpointStats();
  if(planRejected)
    after.reason = planningReason;
  const double elapsed = timer.GetMilliseconds();
  if(performed)
    ++m_IdleCheckpointWorkUnits;
  m_IdleCheckpointTotalMS += elapsed;
  if(automatic)
  {
    // Background work is real cost. It is never a natural reference or profitable foreground hit.
    m_CheckpointAutoDebtMS += elapsed;
    if(requestStillCurrent())
    {
      AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[owner];
      point.debtMS += elapsed;
      point.builds += uint32_t(after.builds >= before.builds ? after.builds - before.builds : 0);
      for(uint32_t event : after.events)
        if(!before.events.contains(event))
          point.nativeEvents.insert(event);
      if(after.builds > before.builds)
      {
        point.retained = false;
        point.hits = point.profitableHits = point.requests = 0;
        point.unmeasuredHits = 0;
        m_CheckpointAutoState = "trial";
        m_CheckpointAutoReason = "idleRebuildRequiresIndependentHitEvidence";
      }
      else if(point.prewarmed && point.nativeEvents.empty())
      {
        // Memory pressure and unsupported boundaries must not become an endless background
        // replay loop. Existing proven points remain in the compatible policy.
        m_RejectedCheckpointCandidates.insert(point.candidate.eventId);
        if(memoryPlanBlocked)
        {
          m_MemoryRejectedCheckpointCandidates.insert(point.candidate.eventId);
          m_IdleCheckpointMemoryNextCheck =
              Timing::GetTick() + uint64_t(5000.0 * Timing::GetTickFrequency());
          if(m_IdleCheckpointMemoryChecks < 16 && m_IdleCheckpointMemoryRecoveries < 2 &&
             m_CheckpointAutoTrials < m_CheckpointAutoCapacity * 2)
            retryIdleAfterMS = 5000;
        }
        m_AutomaticCheckpointPoints.erase(owner);
        candidateRetired = true;
        m_CheckpointAutoConfigurationDirty = true;
      }
    }
  }
  const bool coherent = m_FatalError == ResultCode::Succeeded && requestStillCurrent() &&
                        IsCurrentEventMaterialized();
  if(!coherent)
  {
    m_MaterializedEventValid = false;
    InvalidateScrubReplay();
  }
  continueIdle =
      coherent && prewarm &&
      (after.builds > before.builds || (tryAnotherCandidate && candidateRetired &&
                                        m_CheckpointAutoTrials < m_CheckpointAutoCapacity * 2));
  if(continueIdle)
  {
    // Native planning may reject one module before any GPU work. Try a different candidate in
    // the next idle unit, within the same finite trial budget. Storage failures stay suppressed.
    m_IdleCheckpointAttempted = false;
  }
  const char *reason = !coherent                      ? "replayFailedOrStateChanged"
                       : after.builds > before.builds ? "checkpointBuilt"
                       : planRejected                 ? "candidateUnavailable"
                                                      : "replayedWithoutNewCheckpoint";
  if(EIDTimingEnabled())
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=ReplayCheckpointMaintenance eid=%u candidate=%u reason=%s builds=%llu hits=%llu "
        "configuration_ms=%.3f replay_ms=%.3f total_ms=%.3f",
        expectedEventId, candidateEvent, reason,
        after.builds >= before.builds ? after.builds - before.builds : 0,
        after.hits >= before.hits ? after.hits - before.hits : 0, configurationMS, replayMS, elapsed));
  return receipt(reason);
}

rdcstr ReplayController::GetReplayCheckpointStatus()
{
  CHECK_REPLAY_THREAD();
  const ReplayCheckpointStats stats = m_pDevice->GetReplayCheckpointStats();
  rdcstr reason;
  for(char c : stats.reason)
  {
    if(c == '"' || c == '\\')
      reason += '\\';
    if((unsigned char)c < 0x20)
      reason += StringFormat::Fmt("\\u%04x", (unsigned char)c);
    else
      reason += c;
  }
  rdcstr result = StringFormat::Fmt(
      "{\"schema\":\"renderdoc-replay-checkpoints/"
      "v1\",\"pipelineStateReuse\":true,\"supported\":%s,\"enabled\":%s,"
      "\"captureBytes\":%llu,\"byteBudget\":%llu,\"maxCheckpoints\":%u,\"minEventGap\":%u,"
      "\"checkpointCount\":%u,\"allocatedBytes\":%llu,\"sharedBytes\":%llu,\"builds\":%llu,"
      "\"hits\":%llu,\"misses\":%llu,\"invalidations\":%llu,\"rejected\":%llu,"
      "\"lastResumeEvent\":%u,\"lastSaveEvent\":%u,\"reason\":\"%s\",\"events\":[",
      stats.supported ? "true" : "false", stats.enabled ? "true" : "false",
      m_CheckpointCaptureBytes, m_CheckpointConfig.byteBudget, m_CheckpointConfig.maxCheckpoints,
      m_CheckpointConfig.minEventGap, stats.checkpointCount, stats.allocatedBytes,
      stats.sharedBytes, stats.builds, stats.hits, stats.misses, stats.invalidations,
      stats.rejected, stats.lastResumeEvent, stats.lastSaveEvent, reason.c_str());
  for(size_t i = 0; i < stats.events.size(); i++)
    result += StringFormat::Fmt("%s%u", i ? "," : "", stats.events[i]);
  result += "],\"candidates\":[";
  for(size_t i = 0; i < m_CheckpointCandidates.size(); i++)
  {
    const ReplayCheckpointCandidate &candidate = m_CheckpointCandidates[i];
    result += StringFormat::Fmt("%s{\"eventId\":%u,\"moduleId\":%u,\"priority\":%u}", i ? "," : "",
                                candidate.eventId, candidate.moduleId, candidate.priority);
  }
  result += StringFormat::Fmt(
      "],\"automatic\":{\"mode\":\"%s\",\"state\":\"%s\",\"reason\":\"%s\","
      "\"metric\":\"WithoutDrawPlusOnlyDrawDriverWall\",\"resourceLogicalBytes\":%llu,"
      "\"naturalReferenceVisits\":%llu,\"trialCount\":%u,\"trialLimit\":%u,"
      "\"incrementalCostMS\":%.3f,\"observedSavingsMS\":%.3f,\"configurationPending\":%s,"
      "\"idlePrewarmEnabled\":%s,\"idleWorkUnits\":%llu,\"idleTotalMS\":%.3f,"
      "\"points\":[",
      m_CheckpointControlMode, m_CheckpointAutoState, m_CheckpointAutoReason,
      m_CheckpointResourceBytes, m_CheckpointAutoReferences, m_CheckpointAutoTrials,
      m_CheckpointAutoCapacity * 2, m_CheckpointAutoDebtMS, m_CheckpointAutoSavingsMS,
      m_CheckpointAutoConfigurationDirty ? "true" : "false",
      m_CheckpointIdlePrewarm ? "true" : "false", m_IdleCheckpointWorkUnits, m_IdleCheckpointTotalMS);
  for(size_t i = 0; i < m_AutomaticCheckpointPoints.size(); i++)
  {
    const AutomaticCheckpointPoint &point = m_AutomaticCheckpointPoints[i];
    result += StringFormat::Fmt(
        "%s{\"candidateEvent\":%u,\"moduleId\":%u,\"state\":\"%s\",\"bufferEdit\":%s,\"prewarmed\":"
        "%s,"
        "\"requests\":%u,\"builds\":%u,\"hits\":%u,\"profitableHits\":%u,"
        "\"unmeasuredHits\":%u,\"trialRequestLimit\":%u,"
        "\"baselineMS\":%.3f,\"remainingCostMS\":%.3f,\"lastHitMS\":%s,\"nativeEvents\":[",
        i ? "," : "", point.candidate.eventId, point.candidate.moduleId,
        point.retained ? "retained" : "trial", point.bufferEdit ? "true" : "false",
        point.prewarmed ? "true" : "false", point.requests, point.builds, point.hits,
        point.profitableHits, point.unmeasuredHits,
        point.prewarmed ? CheckpointPrewarmTrialRequestLimit : 6U, point.baselineMS, point.debtMS,
        point.hits ? StringFormat::Fmt("%.3f", point.lastHitMS).c_str() : "null");
    size_t n = 0;
    for(uint32_t event : point.nativeEvents)
      result += StringFormat::Fmt("%s%u", n++ ? "," : "", event);
    result += "]}";
  }
  result += "],\"rejectedCandidates\":[";
  size_t rejectedIndex = 0;
  for(uint32_t event : m_RejectedCheckpointCandidates)
    result += StringFormat::Fmt("%s%u", rejectedIndex++ ? "," : "", event);
  result += StringFormat::Fmt(
      "]},\"sharedBufferPatchGeneration\":%llu,\"sharedBufferPatchFirstAffectedEvent\":%u,"
      "\"hasInitialCheckpoint\":%s}",
      m_SharedBufferPatchGeneration, m_pDevice->GetReplayBufferPatchFirstAffectedEvent(),
      stats.hasInitialCheckpoint ? "true" : "false");
  return result;
}

void ReplayController::FileChanged()
{
  CHECK_REPLAY_THREAD();
  EndAnalysisForMutation();

  m_pDevice->InvalidateReplayCheckpoints(0);
  ResetAutomaticCheckpointCosts(0);
  m_pDevice->FileChanged();
}

APIProperties ReplayController::GetAPIProperties()
{
  CHECK_REPLAY_THREAD();

  return m_pDevice->GetAPIProperties();
}

void ReplayController::FetchPipelineState(uint32_t eventId)
{
  CHECK_REPLAY_THREAD();

  RENDERDOC_PROFILEFUNCTION();

  m_PipelineStateValid = false;

  const bool timing = EIDTimingEnabled();
  PerformanceTimer pipelineTimer;
  m_pDevice->SavePipelineState(eventId);
  if(FatalErrorCheck())
    return;
  const double backendMS = timing ? pipelineTimer.GetMilliseconds() : 0.0;

  if(m_APIProps.pipelineType == GraphicsAPI::D3D11)
    m_PipeState.SetState(&m_D3D11PipelineState);
  else if(m_APIProps.pipelineType == GraphicsAPI::D3D12)
    m_PipeState.SetState(&m_D3D12PipelineState);
  else if(m_APIProps.pipelineType == GraphicsAPI::OpenGL)
    m_PipeState.SetState(&m_GLPipelineState);
  else if(m_APIProps.pipelineType == GraphicsAPI::Vulkan)
    m_PipeState.SetState(&m_VulkanPipelineState);

  rdcarray<DescriptorAccess> access = m_pDevice->GetDescriptorAccess(eventId);
  const double accessEndMS = timing ? pipelineTimer.GetMilliseconds() : 0.0;
  const size_t accessCount = access.size();
  uint32_t storeQueries = 0;
  rdcarray<Descriptor> descs;
  rdcarray<SamplerDescriptor> samps;
  descs.reserve(access.size());
  samps.reserve(access.size());

  // we could collate ranges by descriptor store, but in practice we don't expect descriptors to be
  // scattered across multiple stores. So to keep the code simple for now we do a linear sweep
  ResourceId store;
  rdcarray<DescriptorRange> ranges;

  for(const DescriptorAccess &acc : access)
  {
    if(acc.descriptorStore != store)
    {
      if(store != ResourceId())
      {
        storeQueries++;
        descs.append(m_pDevice->GetDescriptors(store, ranges));
        samps.append(m_pDevice->GetSamplerDescriptors(store, ranges));
      }

      store = acc.descriptorStore;
      ranges.clear();
    }

    // if the last range is contiguous with this access, append this access as a new range to query
    if(!ranges.empty() && ranges.back().descriptorSize == acc.byteSize &&
       ranges.back().offset + ranges.back().count * ranges.back().descriptorSize == acc.byteOffset &&
       ranges.back().type == acc.type)
    {
      ranges.back().count++;
      continue;
    }

    DescriptorRange range = acc;
    ranges.push_back(range);
  }

  if(store != ResourceId())
  {
    storeQueries++;
    descs.append(m_pDevice->GetDescriptors(store, ranges));
    samps.append(m_pDevice->GetSamplerDescriptors(store, ranges));
  }

  if(m_FatalError != ResultCode::Succeeded)
    return;
  const double descriptorEndMS = timing ? pipelineTimer.GetMilliseconds() : 0.0;
  m_PipeState.SetDescriptorAccess(std::move(access), std::move(descs), std::move(samps));
  m_PipelineStateEventID = eventId;
  m_PipelineStateShaderGeneration = GetShaderReplacementGeneration(eventId);
  m_PipelineStateBufferGeneration = m_SharedBufferPatchGeneration;
  m_PipelineStateValid = true;
  if(timing)
  {
    const double totalMS = pipelineTimer.GetMilliseconds();
    const rdcstr line = StringFormat::Fmt(
        "api=PipelineFetch eid=%u total_ms=%.3f backend_state_ms=%.3f descriptor_access_ms=%.3f "
        "descriptor_queries_ms=%.3f publish_ms=%.3f accesses=%llu store_queries=%u",
        eventId, totalMS, backendMS, accessEndMS - backendMS, descriptorEndMS - accessEndMS,
        totalMS - descriptorEndMS, uint64_t(accessCount), storeQueries);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

#if ENABLED(ENABLE_UNIT_TESTS)
#include <limits>
#include "catch/catch.hpp"

// No GPU, window, network or replay file: unexpected native operations fail the test.
class IdleCheckpointTestDriver : public IReplayDriver
{
public:
  APIProperties GetAPIProperties() override
  {
    FAIL("Unexpected native GetAPIProperties during idle maintenance");
    return {};
  }
  rdcarray<ResourceDescription> GetResources() override
  {
    FAIL("Unexpected native GetResources during idle maintenance");
    return {};
  }
  rdcarray<DescriptorStoreDescription> GetDescriptorStores() override
  {
    FAIL("Unexpected native GetDescriptorStores during idle maintenance");
    return {};
  }
  rdcarray<BufferDescription> GetBuffers() override
  {
    FAIL("Unexpected native GetBuffers during idle maintenance");
    return {};
  }
  BufferDescription GetBuffer(ResourceId id) override
  {
    FAIL("Unexpected native GetBuffer during idle maintenance");
    return {};
  }
  rdcarray<TextureDescription> GetTextures() override
  {
    FAIL("Unexpected native GetTextures during idle maintenance");
    return {};
  }
  TextureDescription GetTexture(ResourceId id) override
  {
    FAIL("Unexpected native GetTexture during idle maintenance");
    return {};
  }
  rdcarray<DebugMessage> GetDebugMessages() override
  {
    FAIL("Unexpected native GetDebugMessages during idle maintenance");
    return {};
  }
  rdcarray<ShaderEntryPoint> GetShaderEntryPoints(ResourceId shader) override
  {
    FAIL("Unexpected native GetShaderEntryPoints during idle maintenance");
    return {};
  }
  const ShaderReflection *GetShader(ResourceId pipeline, ResourceId shader,
                                    ShaderEntryPoint entry) override
  {
    FAIL("Unexpected native GetShader during idle maintenance");
    return {};
  }
  rdcarray<rdcstr> GetDisassemblyTargets(bool withPipeline) override
  {
    FAIL("Unexpected native GetDisassemblyTargets during idle maintenance");
    return {};
  }
  rdcstr DisassembleShader(ResourceId pipeline, const ShaderReflection *refl,
                           const rdcstr &target) override
  {
    FAIL("Unexpected native DisassembleShader during idle maintenance");
    return {};
  }
  rdcarray<EventUsage> GetUsage(ResourceId id) override
  {
    FAIL("Unexpected native GetUsage during idle maintenance");
    return {};
  }
  void SetPipelineStates(D3D11Pipe::State *d3d11, D3D12Pipe::State *d3d12, GLPipe::State *gl,
                         VKPipe::State *vk) override
  {
    FAIL("Unexpected native SetPipelineStates during idle maintenance");
  }
  void SavePipelineState(uint32_t eventId) override
  {
    FAIL("Unexpected native SavePipelineState during idle maintenance");
  }
  rdcarray<Descriptor> GetDescriptors(ResourceId descriptorStore,
                                      const rdcarray<DescriptorRange> &ranges) override
  {
    FAIL("Unexpected native GetDescriptors during idle maintenance");
    return {};
  }
  rdcarray<SamplerDescriptor> GetSamplerDescriptors(ResourceId descriptorStore,
                                                    const rdcarray<DescriptorRange> &ranges) override
  {
    FAIL("Unexpected native GetSamplerDescriptors during idle maintenance");
    return {};
  }
  rdcarray<DescriptorAccess> GetDescriptorAccess(uint32_t eventId) override
  {
    FAIL("Unexpected native GetDescriptorAccess during idle maintenance");
    return {};
  }
  rdcarray<DescriptorLogicalLocation> GetDescriptorLocations(
      ResourceId descriptorStore, const rdcarray<DescriptorRange> &ranges) override
  {
    FAIL("Unexpected native GetDescriptorLocations during idle maintenance");
    return {};
  }
  FrameRecord GetFrameRecord() override
  {
    FAIL("Unexpected native GetFrameRecord during idle maintenance");
    return {};
  }
  RDResult ReadLogInitialisation(RDCFile *rdc, bool storeStructuredBuffers) override
  {
    FAIL("Unexpected native ReadLogInitialisation during idle maintenance");
    return {};
  }
  SDFile *GetStructuredFile() override
  {
    FAIL("Unexpected native GetStructuredFile during idle maintenance");
    return {};
  }
  rdcarray<uint32_t> GetPassEvents(uint32_t eventId) override
  {
    FAIL("Unexpected native GetPassEvents during idle maintenance");
    return {};
  }
  void InitPostVSBuffers(uint32_t eventId) override
  {
    FAIL("Unexpected native InitPostVSBuffers during idle maintenance");
  }
  void InitPostVSBuffers(const rdcarray<uint32_t> &passEvents) override
  {
    FAIL("Unexpected native InitPostVSBuffers during idle maintenance");
  }
  MeshFormat GetPostVSBuffers(uint32_t eventId, uint32_t instID, uint32_t viewID,
                              MeshDataStage stage) override
  {
    FAIL("Unexpected native GetPostVSBuffers during idle maintenance");
    return {};
  }
  void GetBufferData(ResourceId buff, uint64_t offset, uint64_t len, bytebuf &retData) override
  {
    FAIL("Unexpected native GetBufferData during idle maintenance");
  }
  void GetTextureData(ResourceId tex, const Subresource &sub, const GetTextureDataParams &params,
                      bytebuf &data) override
  {
    FAIL("Unexpected native GetTextureData during idle maintenance");
  }
  void BuildTargetShader(ShaderEncoding sourceEncoding, const bytebuf &source, const rdcstr &entry,
                         const ShaderCompileFlags &compileFlags, ShaderStage type, ResourceId &id,
                         rdcstr &errors) override
  {
    FAIL("Unexpected native BuildTargetShader during idle maintenance");
  }
  rdcarray<ShaderEncoding> GetTargetShaderEncodings() override
  {
    FAIL("Unexpected native GetTargetShaderEncodings during idle maintenance");
    return {};
  }
  void ReplaceResource(ResourceId from, ResourceId to) override
  {
    FAIL("Unexpected native ReplaceResource during idle maintenance");
  }
  void RemoveReplacement(ResourceId id) override
  {
    FAIL("Unexpected native RemoveReplacement during idle maintenance");
  }
  void FreeTargetResource(ResourceId id) override
  {
    FAIL("Unexpected native FreeTargetResource during idle maintenance");
  }
  void ClearReplayCache() override
  {
    FAIL("Unexpected native ClearReplayCache during idle maintenance");
  }
  void ReloadShaderDebugInformation() override
  {
    FAIL("Unexpected native ReloadShaderDebugInformation during idle maintenance");
  }
  rdcarray<GPUCounter> EnumerateCounters() override
  {
    FAIL("Unexpected native EnumerateCounters during idle maintenance");
    return {};
  }
  CounterDescription DescribeCounter(GPUCounter counterID) override
  {
    FAIL("Unexpected native DescribeCounter during idle maintenance");
    return {};
  }
  rdcarray<CounterResult> FetchCounters(const rdcarray<GPUCounter> &counterID) override
  {
    FAIL("Unexpected native FetchCounters during idle maintenance");
    return {};
  }
  void FillCBufferVariables(ResourceId pipeline, ResourceId shader, ShaderStage stage,
                            rdcstr entryPoint, uint32_t cbufSlot, rdcarray<ShaderVariable> &outvars,
                            const bytebuf &data) override
  {
    FAIL("Unexpected native FillCBufferVariables during idle maintenance");
  }
  rdcarray<PixelModification> PixelHistory(rdcarray<EventUsage> events, ResourceId target,
                                           uint32_t x, uint32_t y, const Subresource &sub,
                                           CompType typeCast) override
  {
    FAIL("Unexpected native PixelHistory during idle maintenance");
    return {};
  }
  ShaderDebugTrace *DebugVertex(uint32_t eventId, uint32_t vertid, uint32_t instid, uint32_t idx,
                                uint32_t view) override
  {
    FAIL("Unexpected native DebugVertex during idle maintenance");
    return {};
  }
  ShaderDebugTrace *DebugPixel(uint32_t eventId, uint32_t x, uint32_t y,
                               const DebugPixelInputs &inputs) override
  {
    FAIL("Unexpected native DebugPixel during idle maintenance");
    return {};
  }
  ShaderDebugTrace *DebugThread(uint32_t eventId, const rdcfixedarray<uint32_t, 3> &groupid,
                                const rdcfixedarray<uint32_t, 3> &threadid) override
  {
    FAIL("Unexpected native DebugThread during idle maintenance");
    return {};
  }
  ShaderDebugTrace *DebugMeshThread(uint32_t eventId, const rdcfixedarray<uint32_t, 3> &groupid,
                                    const rdcfixedarray<uint32_t, 3> &threadid) override
  {
    FAIL("Unexpected native DebugMeshThread during idle maintenance");
    return {};
  }
  rdcarray<ShaderDebugState> ContinueDebug(ShaderDebugger *debugger) override
  {
    FAIL("Unexpected native ContinueDebug during idle maintenance");
    return {};
  }
  void FreeDebugger(ShaderDebugger *debugger) override
  {
    FAIL("Unexpected native FreeDebugger during idle maintenance");
  }
  ResourceId RenderOverlay(ResourceId texid, FloatVector clearCol, DebugOverlay overlay,
                           uint32_t eventId, const rdcarray<uint32_t> &passEvents) override
  {
    FAIL("Unexpected native RenderOverlay during idle maintenance");
    return {};
  }
  bool IsRenderOutput(ResourceId id) override
  {
    FAIL("Unexpected native IsRenderOutput during idle maintenance");
    return {};
  }
  void FileChanged() override { FAIL("Unexpected native FileChanged during idle maintenance"); }
  bool NeedRemapForFetch(const ResourceFormat &format) override
  {
    FAIL("Unexpected native NeedRemapForFetch during idle maintenance");
    return {};
  }
  DriverInformation GetDriverInfo() override
  {
    FAIL("Unexpected native GetDriverInfo during idle maintenance");
    return {};
  }
  rdcarray<GPUDevice> GetAvailableGPUs() override
  {
    FAIL("Unexpected native GetAvailableGPUs during idle maintenance");
    return {};
  }
  bool IsRemoteProxy() override
  {
    FAIL("Unexpected native IsRemoteProxy during idle maintenance");
    return {};
  }
  rdcarray<WindowingSystem> GetSupportedWindowSystems() override
  {
    FAIL("Unexpected native GetSupportedWindowSystems during idle maintenance");
    return {};
  }
  AMDRGPControl *GetRGPControl() override
  {
    FAIL("Unexpected native GetRGPControl during idle maintenance");
    return {};
  }
  uint64_t MakeOutputWindow(WindowingData window, bool depth) override
  {
    FAIL("Unexpected native MakeOutputWindow during idle maintenance");
    return {};
  }
  void DestroyOutputWindow(uint64_t id) override
  {
    FAIL("Unexpected native DestroyOutputWindow during idle maintenance");
  }
  bool CheckResizeOutputWindow(uint64_t id) override
  {
    FAIL("Unexpected native CheckResizeOutputWindow during idle maintenance");
    return {};
  }
  void GetOutputWindowDimensions(uint64_t id, int32_t &w, int32_t &h) override
  {
    FAIL("Unexpected native GetOutputWindowDimensions during idle maintenance");
  }
  void GetOutputWindowData(uint64_t id, bytebuf &retData) override
  {
    FAIL("Unexpected native GetOutputWindowData during idle maintenance");
  }
  void ClearOutputWindowColor(uint64_t id, FloatVector col) override
  {
    FAIL("Unexpected native ClearOutputWindowColor during idle maintenance");
  }
  void ClearOutputWindowDepth(uint64_t id, float depth, uint8_t stencil) override
  {
    FAIL("Unexpected native ClearOutputWindowDepth during idle maintenance");
  }
  void BindOutputWindow(uint64_t id, bool depth) override
  {
    FAIL("Unexpected native BindOutputWindow during idle maintenance");
  }
  bool IsOutputWindowVisible(uint64_t id) override
  {
    FAIL("Unexpected native IsOutputWindowVisible during idle maintenance");
    return {};
  }
  void FlipOutputWindow(uint64_t id) override
  {
    FAIL("Unexpected native FlipOutputWindow during idle maintenance");
  }
  bool GetMinMax(ResourceId texid, const Subresource &sub, CompType typeCast, float *minval,
                 float *maxval) override
  {
    FAIL("Unexpected native GetMinMax during idle maintenance");
    return {};
  }
  bool GetHistogram(ResourceId texid, const Subresource &sub, CompType typeCast, float minval,
                    float maxval, const rdcfixedarray<bool, 4> &channels,
                    rdcarray<uint32_t> &histogram) override
  {
    FAIL("Unexpected native GetHistogram during idle maintenance");
    return {};
  }
  void PickPixel(ResourceId texture, uint32_t x, uint32_t y, const Subresource &sub,
                 CompType typeCast, float pixel[4]) override
  {
    FAIL("Unexpected native PickPixel during idle maintenance");
  }
  ResourceId CreateProxyTexture(const TextureDescription &templateTex) override
  {
    FAIL("Unexpected native CreateProxyTexture during idle maintenance");
    return {};
  }
  void SetProxyTextureData(ResourceId texid, const Subresource &sub, byte *data,
                           size_t dataSize) override
  {
    FAIL("Unexpected native SetProxyTextureData during idle maintenance");
  }
  bool IsTextureSupported(const TextureDescription &tex) override
  {
    FAIL("Unexpected native IsTextureSupported during idle maintenance");
    return {};
  }
  ResourceId CreateProxyBuffer(const BufferDescription &templateBuf) override
  {
    FAIL("Unexpected native CreateProxyBuffer during idle maintenance");
    return {};
  }
  void SetProxyBufferData(ResourceId bufid, byte *data, size_t dataSize) override
  {
    FAIL("Unexpected native SetProxyBufferData during idle maintenance");
  }
  void RenderMesh(uint32_t eventId, const rdcarray<MeshFormat> &secondaryDraws,
                  const MeshDisplay &cfg, const MeshComponentHighlight &components) override
  {
    FAIL("Unexpected native RenderMesh during idle maintenance");
  }
  bool RenderTexture(TextureDisplay cfg) override
  {
    FAIL("Unexpected native RenderTexture during idle maintenance");
    return {};
  }
  void SetCustomShaderIncludes(const rdcarray<rdcstr> &directories) override
  {
    FAIL("Unexpected native SetCustomShaderIncludes during idle maintenance");
  }
  void BuildCustomShader(ShaderEncoding sourceEncoding, const bytebuf &source, const rdcstr &entry,
                         const ShaderCompileFlags &compileFlags, ShaderStage type, ResourceId &id,
                         rdcstr &errors) override
  {
    FAIL("Unexpected native BuildCustomShader during idle maintenance");
  }
  rdcarray<ShaderEncoding> GetCustomShaderEncodings() override
  {
    FAIL("Unexpected native GetCustomShaderEncodings during idle maintenance");
    return {};
  }
  rdcarray<ShaderSourcePrefix> GetCustomShaderSourcePrefixes() override
  {
    FAIL("Unexpected native GetCustomShaderSourcePrefixes during idle maintenance");
    return {};
  }
  ResourceId ApplyCustomShader(TextureDisplay &display) override
  {
    FAIL("Unexpected native ApplyCustomShader during idle maintenance");
    return {};
  }
  void FreeCustomShader(ResourceId id) override
  {
    FAIL("Unexpected native FreeCustomShader during idle maintenance");
  }
  void RenderCheckerboard(FloatVector dark, FloatVector light) override
  {
    FAIL("Unexpected native RenderCheckerboard during idle maintenance");
  }
  void RenderHighlightBox(float w, float h, float scale) override
  {
    FAIL("Unexpected native RenderHighlightBox during idle maintenance");
  }
  uint32_t PickVertex(uint32_t eventId, int32_t width, int32_t height, const MeshDisplay &cfg,
                      uint32_t x, uint32_t y) override
  {
    FAIL("Unexpected native PickVertex during idle maintenance");
    return {};
  }
  struct Call
  {
    uint32_t eventId;
    ReplayLogType type;
    bool bypass, naturalReference, prefixOnly, shaderMutation;
  };
  rdcarray<Call> calls;
  rdcarray<ReplayCheckpointCandidate> candidates;
  rdcarray<rdcarray<ReplayCheckpointCandidate>> configurations;
  ReplayCheckpointStats stats;
  std::function<void()> onReplay;
  bool build = true, fail = false;
  uint32_t rejectedPlanEvent = 0;
  uint32_t memoryProbes = 0;
  bool memoryRecovers = false;
  uint32_t patchAnchor = 0;
  uint32_t shutdowns = 0;
  rdcstr overrideError;
  rdcstr ApplyEventCBufferOverrides(uint32_t eventId) override { return overrideError; }
  void Shutdown() override { ++shutdowns; }
  IReplayDriver *MakeDummyDriver() override { return this; }
  RDResult FatalErrorCheck() override
  {
    return fail ? RDResult(ResultCode::APIReplayFailed, "idle-test device failure")
                : RDResult(ResultCode::Succeeded);
  }
  void ConfigureReplayCheckpoints(const ReplayCheckpointConfig &config,
                                  const rdcarray<ReplayCheckpointCandidate> &next) override
  {
    configurations.push_back(next);
    candidates = next;
    stats.enabled = config.enabled;
    if(!config.enabled)
      stats.memoryBudgetBlocked = false;
    if(stats.candidatePlanKnown)
    {
      stats.candidateEvents.clear();
      for(const auto &candidate : next)
        if(candidate.eventId != rejectedPlanEvent)
          stats.candidateEvents.push_back(candidate.eventId);
    }
    for(size_t i = stats.events.size(); i > 0; --i)
      if(!ReplayCheckpointCandidateContains(candidates, stats.events[i - 1]))
        stats.events.erase(i - 1);
    stats.checkpointCount = uint32_t(stats.events.size());
  }
  ReplayCheckpointStats GetReplayCheckpointStats() override { return stats; }
  bool RefreshReplayCheckpointMemoryBudget() override
  {
    ++memoryProbes;
    if(!memoryRecovers || !stats.memoryBudgetBlocked)
      return false;
    stats.memoryBudgetBlocked = false;
    stats.canTryOtherCandidates = true;
    rejectedPlanEvent = 0;
    return true;
  }
  void InvalidateReplayCheckpoints(uint32_t firstAffectedEvent) override
  {
    for(size_t i = stats.events.size(); i > 0; --i)
      if(firstAffectedEvent == 0 || stats.events[i - 1] >= firstAffectedEvent)
        stats.events.erase(i - 1);
    stats.checkpointCount = uint32_t(stats.events.size());
    ++stats.invalidations;
  }
  void ReplayLog(uint32_t event, ReplayLogType type) override
  {
    calls.push_back({event, type, m_ReplayCheckpointBypass, m_ReplayCheckpointNaturalReference,
                     m_ReplayCheckpointPrefixOnly, m_ReplayShaderMutationRoot});
    if(onReplay)
      onReplay();
    if(type != eReplay_WithoutDraw || m_ReplayCheckpointBypass || fail)
      return;
    if(!stats.events.empty())
    {
      ++stats.hits;
      stats.lastResumeEvent = stats.events.back();
    }
    if(build)
      for(size_t i = candidates.size(); i > 0; --i)
      {
        const ReplayCheckpointCandidate &candidate = candidates[i - 1];
        if(m_ReplayCheckpointPrefixOnly && candidate.eventId != patchAnchor)
          continue;
        if(candidate.eventId < event && !stats.events.contains(candidate.eventId))
        {
          stats.events.push_back(candidate.eventId);
          stats.lastSaveEvent = candidate.eventId;
          ++stats.builds;
          break;
        }
      }
    stats.checkpointCount = uint32_t(stats.events.size());
  }
};

// Model the D3D12 bytecode cache: repeated builds acquire the same native ResourceId.
class TargetShaderLifetimeTestDriver : public IdleCheckpointTestDriver
{
public:
  ResourceId target = ResourceIDGen::GetNewUniqueID();
  std::map<ResourceId, uint32_t> references;
  uint32_t releases = 0, clearedOverrides = 0, checkedReplacements = 0;
  bool acceptChecked = false;

  rdcarray<ShaderEncoding> GetTargetShaderEncodings() override { return {ShaderEncoding::DXIL}; }
  void BuildTargetShader(ShaderEncoding, const bytebuf &, const rdcstr &,
                         const ShaderCompileFlags &, ShaderStage, ResourceId &id, rdcstr &) override
  {
    id = target;
    ++references[id];
  }
  void FreeTargetResource(ResourceId id) override
  {
    auto reference = references.find(id);
    REQUIRE((reference != references.end()));
    REQUIRE(reference->second > 0);
    ++releases;
    if(--reference->second == 0)
      references.erase(reference);
  }
  void ClearEventShaderOverridesForResource(ResourceId) override { ++clearedOverrides; }
  rdcstr ReplaceTargetShaderChecked(ResourceId, ResourceId to) override
  {
    REQUIRE((references.find(to) != references.end()));
    ++checkedReplacements;
    // Reaching this method proves controller admission without requiring a GPU replay.
    return acceptChecked ? rdcstr() : rdcstr("test driver stops before replay");
  }
};

struct ReplayControllerTestAccess
{
  static void CheckReadOnlyPipelineReuse()
  {
    struct PipelineDriver : IdleCheckpointTestDriver
    {
      uint32_t fetches = 0;
      bool IsRemoteProxy() override { return true; }
      void SavePipelineState(uint32_t) override { ++fetches; }
      rdcarray<DescriptorAccess> GetDescriptorAccess(uint32_t) override { return {}; }
    } driver;
    ReplayController controller;
    controller.m_pDevice = &driver;
    struct Detach
    {
      ReplayController &c;
      ~Detach() { c.m_pDevice = NULL; }
    } detach = {controller};
    controller.m_APIProps.pipelineType = GraphicsAPI::D3D11;
    controller.SetFrameEvent(20, true);
    const size_t calls = driver.calls.size();
    REQUIRE(driver.fetches == 1);
    controller.SetFrameEvent(20, false);
    CHECK(driver.calls.size() == calls);
    CHECK(driver.fetches == 1);
    // A completed analysis restore can leave valid pixels but no matching
    // pipeline. Fetch metadata once, without replaying those pixels again.
    controller.m_PipelineStateValid = false;
    controller.SetFrameEvent(20, false);
    CHECK(driver.calls.size() == calls);
    CHECK(driver.fetches == 2);
    ++controller.m_SharedBufferPatchGeneration;
    controller.SetFrameEvent(20, false);
    CHECK(driver.calls.size() > calls);
    CHECK(driver.fetches == 3);
    const size_t editedCalls = driver.calls.size();
    controller.SetFrameEvent(20, false);
    CHECK(driver.calls.size() == editedCalls);
    controller.SetFrameEvent(21, false);
    CHECK(driver.calls.size() > editedCalls);
    CHECK(driver.fetches == 4);
    const size_t beforeShaderEdit = driver.calls.size();
    ++controller.m_GlobalShaderReplacementGeneration;
    controller.SetFrameEvent(21, false);
    CHECK(driver.calls.size() > beforeShaderEdit);
    CHECK(driver.fetches == 5);
  }

  static void CheckSharedShaderReplacementQuery()
  {
    struct SharedDriver : IdleCheckpointTestDriver
    {
      std::map<ResourceId, ResourceId> replacements;
      rdcstr error;
      bool IsRemoteProxy() override { return true; }
      void SavePipelineState(uint32_t) override {}
      rdcarray<DescriptorAccess> GetDescriptorAccess(uint32_t) override { return {}; }
      rdcstr ReplaceTargetShaderChecked(ResourceId original, ResourceId target) override
      {
        if(!error.empty())
          return error;
        replacements[original] = target;
        return {};
      }
      rdcstr RemoveTargetShaderReplacementChecked(ResourceId original) override
      {
        if(!error.empty())
          return error;
        replacements.erase(original);
        return {};
      }
    } driver;
    ReplayController controller;
    controller.m_pDevice = &driver;
    struct Detach
    {
      ReplayController &c;
      ~Detach() { c.m_pDevice = NULL; }
    } detach = {controller};
    controller.m_APIProps.pipelineType = GraphicsAPI::D3D11;
    controller.m_EventID = controller.m_SelectedEventID = 20;
    const ResourceId original = ResourceIDGen::GetNewUniqueID();
    const ResourceId other = ResourceIDGen::GetNewUniqueID();
    const ResourceId prior = ResourceIDGen::GetNewUniqueID();
    const ResourceId debug = ResourceIDGen::GetNewUniqueID();
    controller.m_TargetResources[prior] = controller.m_TargetResources[debug] = 1;
    CHECK(controller.GetTargetShaderReplacement(original) == ResourceId());
    REQUIRE(controller.ReplaceTargetShaderChecked(original, prior).empty());
    CHECK(controller.GetTargetShaderReplacement(original) == prior);
    CHECK_FALSE(driver.m_ReplayShaderMutationRoot);
    REQUIRE(driver.calls.size() == 2);
    CHECK(driver.calls[0].shaderMutation);
    CHECK(driver.calls[1].shaderMutation);
    CHECK(driver.calls[0].bypass);    // old result checkpoints still cannot be reused
    REQUIRE(controller.ReplaceTargetShaderChecked(original, debug).empty());
    CHECK(controller.GetTargetShaderReplacement(original) == debug);
    CHECK(controller.GetCurrentEventID() == 20);
    CHECK(controller.m_SelectedEventID == 20);
    CHECK(driver.stats.invalidations >= 2);

    const uint64_t generation = controller.GetShaderReplacementGeneration(0);
    const size_t replays = driver.calls.size();
    CHECK(controller.GetTargetShaderReplacement(other) == ResourceId());
    CHECK(controller.GetTargetShaderReplacement(ResourceId()) == ResourceId());
    CHECK(controller.GetTargetShaderReplacement(debug) == ResourceId());
    CHECK(controller.GetTargetShaderReplacement(original) == debug);
    CHECK(driver.calls.size() == replays);
    CHECK(controller.GetShaderReplacementGeneration(0) == generation);

    // Changing EID never migrates or removes the resource replacement.
    for(uint32_t event : {10U, 80U, 20U})
    {
      controller.SetFrameEvent(event, true);
      REQUIRE(driver.calls.size() >= 2);
      CHECK_FALSE(driver.calls.back().shaderMutation);
      CHECK(controller.GetTargetShaderReplacement(original) == debug);
      CHECK(driver.replacements[original] == debug);
      CHECK(controller.GetShaderReplacementGeneration(event) == generation);
    }
    driver.error = "checked-pipeline-rejected";
    CHECK(controller.ReplaceTargetShaderChecked(original, prior) == driver.error);
    CHECK(controller.RemoveTargetShaderReplacementChecked(original) == driver.error);
    CHECK(controller.GetTargetShaderReplacement(original) == debug);
    CHECK(controller.GetShaderReplacementGeneration(0) == generation);
    driver.error.clear();
    REQUIRE(controller.ReplaceTargetShaderChecked(original, prior).empty());
    CHECK(controller.GetTargetShaderReplacement(original) == prior);
    CHECK(controller.GetCurrentEventID() == 20);
    const uint64_t batch = controller.BeginShaderReplacementBatch();
    REQUIRE(batch != 0);
    CHECK(controller.BeginShaderReplacementBatch() == 0);
    const size_t beforeBatch = driver.calls.size();
    REQUIRE(controller.ReplaceTargetShaderChecked(original, debug).empty());
    REQUIRE(controller.ReplaceTargetShaderChecked(original, prior).empty());
    CHECK(driver.calls.size() == beforeBatch);
    CHECK(controller.GetTargetShaderReplacement(original) == prior);
    CHECK_FALSE(controller.EndShaderReplacementBatch(batch + 1));
    // User navigation remains immediate and the final flush keeps that EID.
    controller.SetFrameEvent(31, true);
    const size_t afterNavigation = driver.calls.size();
    CHECK(afterNavigation > beforeBatch);
    REQUIRE(controller.EndShaderReplacementBatch(batch));
    CHECK(driver.calls.size() > afterNavigation);
    CHECK(controller.GetCurrentEventID() == 31);
    CHECK_FALSE(controller.EndShaderReplacementBatch(batch));
    const uint64_t failedBatch = controller.BeginShaderReplacementBatch();
    driver.error = "checked-pipeline-rejected";
    const size_t beforeFailed = driver.calls.size();
    CHECK(controller.ReplaceTargetShaderChecked(original, debug) == driver.error);
    REQUIRE(controller.EndShaderReplacementBatch(failedBatch));
    CHECK(driver.calls.size() == beforeFailed);
    driver.error.clear();
    REQUIRE(controller.RemoveTargetShaderReplacementChecked(original).empty());
    CHECK(controller.GetTargetShaderReplacement(original) == ResourceId());
    CHECK(driver.replacements.empty());
    CHECK(controller.GetCurrentEventID() == 31);
  }

  static void CheckEventShaderOverlay()
  {
    struct EventDriver : IdleCheckpointTestDriver
    {
      EventShaderOverrideState configured;
      ResourceId before = ResourceIDGen::GetNewUniqueID(), live = before;
      bool applied = false, rejectNextApply = false;
      bool IsRemoteProxy() override { return true; }
      void SavePipelineState(uint32_t) override {}
      rdcarray<DescriptorAccess> GetDescriptorAccess(uint32_t) override { return {}; }
      rdcstr SetEventShaderOverride(uint32_t event, ShaderStage stage, ResourceId original,
                                    ResourceId target, uint32_t output, bool independentFloat) override
      {
        configured.active = true;
        configured.eventId = event;
        configured.stage = stage;
        configured.expectedOriginal = original;
        configured.replacement = target;
        configured.outputTarget = output;
        configured.independentFloatOutput = independentFloat;
        return {};
      }
      void ClearEventShaderOverride(uint32_t event, ShaderStage stage) override
      {
        if(configured.eventId == event && configured.stage == stage)
          configured.active = false;
      }
      void ClearEventShaderOverrides() override { configured.active = false; }
      rdcstr ApplyEventShaderOverrides(uint32_t event) override
      {
        if(rejectNextApply)
        {
          rejectNextApply = false;
          return "event-pipeline-create-failed";
        }
        if(configured.active && configured.eventId == event)
        {
          live = configured.replacement;
          applied = true;
        }
        return {};
      }
      void RestoreEventShaderOverrides() override
      {
        live = before;
        applied = false;
      }
    };
    for(GraphicsAPI api :
        {GraphicsAPI::D3D11, GraphicsAPI::D3D12, GraphicsAPI::Vulkan, GraphicsAPI::OpenGL})
    {
      EventDriver driver;
      ReplayController controller;
      controller.m_pDevice = &driver;
      struct Detach
      {
        ReplayController &c;
        ~Detach() { c.m_pDevice = NULL; }
      } detach = {controller};
      controller.m_APIProps.pipelineType = api;
      controller.m_EventID = 11;
      controller.m_EventRemap[1] = 11;
      ResourceId original = ResourceIDGen::GetNewUniqueID();
      ResourceId a = ResourceIDGen::GetNewUniqueID(), b = ResourceIDGen::GetNewUniqueID();
      controller.m_TargetResources[a] = controller.m_TargetResources[b] = 1;
      driver.onReplay = [&]() {
        const auto &call = driver.calls.back();
        if(call.type == eReplay_WithoutDraw)
        {
          CHECK(driver.live == driver.before);
          CHECK_FALSE(call.bypass);    // safe upstream checkpoints remain usable
        }
        if(call.type == eReplay_OnlyDraw && driver.applied)
        {
          CHECK(driver.live == driver.configured.replacement);
          CHECK(call.bypass);    // the debug result must never become an upstream checkpoint
        }
      };
      CHECK_FALSE(controller.GetEventShaderOverride(1, ShaderStage::Pixel).active);
      REQUIRE(controller.SetEventShaderOverride(1, ShaderStage::Pixel, original, a, 2, true).empty());
      const auto saved = controller.GetEventShaderOverride(1, ShaderStage::Pixel);
      REQUIRE(saved.active);
      CHECK(saved.eventId == 11);
      CHECK(saved.expectedOriginal == original);
      CHECK(saved.replacement == a);
      CHECK(saved.outputTarget == 2);
      CHECK(saved.independentFloatOutput);
      CHECK(driver.live == driver.before);
      driver.rejectNextApply = true;
      // This API returns a recoverable string diagnostic, not an RDResult device failure.
      CHECK(controller.SetEventShaderOverride(1, ShaderStage::Pixel, original, b, 1) ==
            "event-pipeline-create-failed");
      CHECK(controller.GetEventShaderOverride(1, ShaderStage::Pixel).replacement == a);
      CHECK(controller.GetEventShaderOverride(1, ShaderStage::Pixel).outputTarget == 2);
      CHECK(driver.configured.replacement == a);
      CHECK(driver.configured.independentFloatOutput);
      CHECK(driver.live == driver.before);
      controller.ClearEventShaderOverride(1, ShaderStage::Pixel);
      CHECK_FALSE(controller.GetEventShaderOverride(1, ShaderStage::Pixel).active);
      CHECK_FALSE(driver.configured.active);
      CHECK(driver.live == driver.before);
    }
  }

  static void CheckEventShaderFatalErrors()
  {
    enum class FailurePoint
    {
      Clear,
      ClearAll,
      Preview,
      Register,
      Apply
    };
    struct EventDriver : IdleCheckpointTestDriver
    {
      FailurePoint point = FailurePoint::Clear;
      uint32_t operations = 0;
      bool IsRemoteProxy() override { return true; }
      void SavePipelineState(uint32_t) override {}
      rdcarray<DescriptorAccess> GetDescriptorAccess(uint32_t) override { return {}; }
      rdcstr SetEventShaderOverride(uint32_t, ShaderStage, ResourceId, ResourceId, uint32_t,
                                    bool) override
      {
        ++operations;
        fail = point == FailurePoint::Register;
        // Model a lost remote reply: the empty return value alone is not an acknowledgement.
        return {};
      }
      void ClearEventShaderOverride(uint32_t, ShaderStage) override
      {
        ++operations;
        fail = true;
      }
      void ClearEventShaderOverrides() override
      {
        ++operations;
        fail = true;
      }
      TextureDescription GetEventShaderPreviewTexture(uint32_t, ShaderStage) override
      {
        ++operations;
        fail = true;
        TextureDescription stale;
        stale.resourceId = ResourceIDGen::GetNewUniqueID();
        return stale;
      }
      rdcstr ApplyEventShaderOverrides(uint32_t) override
      {
        if(point == FailurePoint::Apply)
          fail = true;
        return {};
      }
    };

    for(FailurePoint point : {FailurePoint::Clear, FailurePoint::ClearAll, FailurePoint::Preview,
                              FailurePoint::Register, FailurePoint::Apply})
    {
      CAPTURE(int(point));
      EventDriver driver;
      driver.point = point;
      ReplayController controller;
      controller.m_pDevice = &driver;
      struct Detach
      {
        ReplayController &c;
        ~Detach() { c.m_pDevice = NULL; }
      } detach = {controller};
      controller.m_APIProps.pipelineType = GraphicsAPI::D3D11;
      controller.m_EventID =
          point == FailurePoint::Register || point == FailurePoint::Apply ? 11 : 12;
      controller.m_EventRemap[1] = 11;
      const ResourceId original = ResourceIDGen::GetNewUniqueID();
      const ResourceId prior = ResourceIDGen::GetNewUniqueID();
      const ResourceId next = ResourceIDGen::GetNewUniqueID();
      controller.m_TargetResources[prior] = controller.m_TargetResources[next] = 1;
      controller.m_EventShaderReplacements[{11, ShaderStage::Pixel}] = {original, prior, 0, true};
      const uint64_t generation = controller.GetShaderReplacementGeneration(11);

      switch(point)
      {
        case FailurePoint::Clear:
          // No selected-EID replay follows this call, so the mutation itself must latch failure.
          controller.ClearEventShaderOverride(1, ShaderStage::Pixel);
          break;
        case FailurePoint::ClearAll: controller.ClearEventShaderOverrides(); break;
        case FailurePoint::Preview:
          CHECK(controller.GetEventShaderPreviewTexture(1, ShaderStage::Pixel).resourceId ==
                ResourceId());
          break;
        case FailurePoint::Register:
        case FailurePoint::Apply:
          CHECK_FALSE(
              controller.SetEventShaderOverride(1, ShaderStage::Pixel, original, next, 0, true).empty());
          break;
      }

      CHECK(controller.GetFatalErrorStatus().code == ResultCode::APIReplayFailed);
      CHECK(driver.shutdowns == 1);
      const EventShaderOverrideState state = controller.GetEventShaderOverride(1, ShaderStage::Pixel);
      CHECK(state.active);
      CHECK(state.independentFloatOutput);
      CHECK(state.replacement == (point == FailurePoint::Apply ? next : prior));
      if(point != FailurePoint::Apply)
      {
        CHECK(controller.GetShaderReplacementGeneration(11) == generation);
        CHECK(driver.calls.empty());
      }

      // A failed controller must not submit more mutations or expose a stale scratch descriptor.
      const uint32_t operations = driver.operations;
      controller.ClearEventShaderOverride(1, ShaderStage::Pixel);
      controller.ClearEventShaderOverrides();
      CHECK(controller.GetEventShaderPreviewTexture(1, ShaderStage::Pixel).resourceId == ResourceId());
      CHECK_FALSE(
          controller.SetEventShaderOverride(1, ShaderStage::Pixel, original, next, 0, true).empty());
      CHECK(driver.operations == operations);
      CHECK(controller.GetEventShaderOverride(1, ShaderStage::Pixel).active);
    }
  }

  static void CheckShaderPairAnalysis()
  {
    struct PairDriver : IdleCheckpointTestDriver
    {
      ShaderPairComparison response;
      uint32_t comparisons = 0;
      bool useDefault = false;
      ResourceId receivedBaseline, receivedCandidate;
      bool IsRemoteProxy() override { return false; }
      ShaderPairComparison ComparePixelShaderPair(uint32_t eventId, ResourceId original,
                                                  ResourceId baseline, ResourceId candidate) override
      {
        ++comparisons;
        receivedBaseline = baseline;
        receivedCandidate = candidate;
        CHECK(m_ReplayCheckpointBypass);
        CHECK_FALSE(m_ReplayCheckpointNaturalReference);
        CHECK_FALSE(m_ReplayCheckpointPrefixOnly);
        if(useDefault)
          return IReplayDriver::ComparePixelShaderPair(eventId, original, baseline, candidate);
        return response;
      }
    } driver;
    ReplayController controller;
    controller.m_pDevice = &driver;
    struct DetachDriver
    {
      ReplayController &controller;
      ~DetachDriver() { controller.m_pDevice = NULL; }
    } detach = {controller};
    const ResourceId original = ResourceIDGen::GetNewUniqueID();
    controller.m_EventID = 100;
    controller.m_EventRemap[10] = 20;
    driver.response.eventId = 20;
    driver.response.supported = driver.response.completed = true;
    ShaderPairSnapshot output;
    output.resourceId = ResourceIDGen::GetNewUniqueID();
    output.width = output.height = 1;
    output.format.type = ResourceFormatType::Regular;
    output.format.compCount = 1;
    output.format.compByteWidth = 1;
    output.baseline = bytebuf{1};
    output.candidate = bytebuf{2};
    output.baselineRepeat = bytebuf{1};
    driver.response.outputs.push_back(output);

    SECTION("Bounded snapshots preserve selection and establish analysis readiness")
    {
      const ShaderPairComparison result =
          controller.ComparePixelShaderPair(10, original, ResourceId(), ResourceId());
      REQUIRE(result.completed);
      REQUIRE(result.outputs.size() == 1);
      CHECK(result.eventId == 20);
      CHECK(result.outputs[0].candidate[0] == 2);
      CHECK(driver.receivedBaseline == original);
      CHECK(driver.receivedCandidate == original);
      CHECK(controller.m_EventID == 100);
      CHECK(controller.m_AnalysisReplayEventID == 20);
      CHECK((controller.m_AnalysisReplayState == ReplayController::AnalysisReplayState::Ready));
      CHECK_FALSE(driver.m_ReplayCheckpointBypass);
      CHECK(driver.calls.empty());
    }
    SECTION("Two locally built drafts are compared without installing replacements")
    {
      const ResourceId baseline = ResourceIDGen::GetNewUniqueID();
      const ResourceId candidate = ResourceIDGen::GetNewUniqueID();
      controller.m_TargetResources[baseline] = 1;
      controller.m_TargetResources[candidate] = 1;
      const uint64_t generation = controller.GetShaderReplacementGeneration(0);
      const ShaderPairComparison result =
          controller.ComparePixelShaderPair(10, original, baseline, candidate);
      CHECK(result.completed);
      CHECK(driver.receivedBaseline == baseline);
      CHECK(driver.receivedCandidate == candidate);
      CHECK(controller.m_GlobalShaderReplacements.empty());
      CHECK(controller.GetShaderReplacementGeneration(0) == generation);
      CHECK(controller.m_EventID == 100);
    }
    SECTION("Native D32S8 snapshots retain their eight-byte padded pixel layout")
    {
      ShaderPairSnapshot &depth = driver.response.outputs[0];
      depth.depth = true;
      depth.format.type = ResourceFormatType::D32S8;
      depth.baseline.resize(8);
      depth.candidate.resize(8);
      depth.baselineRepeat.resize(8);
      CHECK(controller.ComparePixelShaderPair(10, original, original, original).completed);
    }
    SECTION("A driver without comparison support never claims completion")
    {
      driver.useDefault = true;
      const ShaderPairComparison result =
          controller.ComparePixelShaderPair(10, original, original, original);
      CHECK_FALSE(result.supported);
      CHECK_FALSE(result.completed);
      CHECK_FALSE(result.reason.empty());
      CHECK(result.outputs.empty());
      CHECK(controller.m_EventID == 100);
      CHECK((controller.m_AnalysisReplayState ==
             ReplayController::AnalysisReplayState::PendingOrFailed));
    }
    SECTION("Wrong identity, incomplete bytes, duplicate slots and invalid formats are rejected")
    {
      for(uint32_t mode = 0; mode < 7; ++mode)
      {
        driver.response.eventId = mode == 0 ? 21 : 20;
        driver.response.outputs = {output};
        if(mode == 1)
          driver.response.outputs[0].baselineRepeat.clear();
        if(mode == 2)
          driver.response.outputs.push_back(output);
        if(mode == 3)
          driver.response.outputs.resize(10);
        if(mode == 4)
          driver.response.outputs[0].format = ResourceFormat();
        if(mode == 5)
          driver.response.outputs[0].format.compByteWidth = 4;
        if(mode == 6)
          driver.response.outputs[0].width = 8193;
        const ShaderPairComparison result =
            controller.ComparePixelShaderPair(10, original, original, original);
        CHECK_FALSE(result.completed);
        CHECK_FALSE(result.reason.empty());
        CHECK(result.outputs.empty());
        CHECK((controller.m_AnalysisReplayState ==
               ReplayController::AnalysisReplayState::PendingOrFailed));
      }
    }
    SECTION("Existing replacements and foreign candidates are rejected before native work")
    {
      const ResourceId other = ResourceIDGen::GetNewUniqueID();
      CHECK_FALSE(controller.ComparePixelShaderPair(10, original, original, other).supported);
      CHECK_FALSE(controller.ComparePixelShaderPair(10, original, other, original).supported);
      controller.m_GlobalShaderReplacements[original] = other;
      CHECK_FALSE(controller.ComparePixelShaderPair(10, original, original, original).supported);
      controller.m_GlobalShaderReplacements.clear();
      controller.m_EventShaderReplacements[{20, ShaderStage::Pixel}] = {original, other};
      CHECK_FALSE(controller.ComparePixelShaderPair(10, original, original, original).supported);
      CHECK(driver.comparisons == 0);
      CHECK((controller.m_AnalysisReplayState == ReplayController::AnalysisReplayState::None));
    }
  }

  static void CheckTargetShaderLifetime()
  {
    TargetShaderLifetimeTestDriver driver;
    ReplayController controller;
    controller.m_pDevice = &driver;
    struct DetachDriver
    {
      ReplayController &controller;
      ~DetachDriver() { controller.m_pDevice = NULL; }
    } detach = {controller};
    const auto build = [&]() {
      return controller
          .BuildTargetShader("MainPS", ShaderEncoding::DXIL, bytebuf{1}, ShaderCompileFlags(),
                             ShaderStage::Pixel)
          .first;
    };

    const ResourceId original = ResourceIDGen::GetNewUniqueID();
    const ResourceId target = build();
    REQUIRE(target == driver.target);
    controller.m_EventID = 100;
    const auto eventKey = std::make_pair(80U, ShaderStage::Pixel);
    controller.m_GlobalShaderReplacements[original] = target;
    controller.m_EventShaderReplacements[eventKey] = {original, target};
    const uint64_t globalGeneration = controller.m_GlobalShaderReplacementGeneration;
    const uint64_t eventGeneration = controller.GetShaderReplacementGeneration(80);

    SECTION("Temporary identical builds leave the current shader admitted and replacements intact")
    {
      REQUIRE(build() == target);
      REQUIRE(build() == target);
      REQUIRE(driver.references[target] == 3);
      controller.m_AnalysisReplayState = ReplayController::AnalysisReplayState::Ready;

      // First a reflection probe, then an A/B baseline release its borrowed reference.
      for(uint32_t reference = 2; reference > 0; --reference)
      {
        controller.FreeTargetResource(target);
        CHECK(driver.references[target] == reference);
        CHECK(controller.m_TargetResources[target] == reference);
        CHECK(controller.m_GlobalShaderReplacements[original] == target);
        CHECK(controller.m_EventShaderReplacements[eventKey].replacement == target);
        CHECK(controller.m_GlobalShaderReplacementGeneration == globalGeneration);
        CHECK(controller.GetShaderReplacementGeneration(80) == eventGeneration);
        CHECK(driver.clearedOverrides == 0);
        CHECK((controller.m_AnalysisReplayState == ReplayController::AnalysisReplayState::Ready));
      }

      CHECK(controller.ReplaceTargetShaderChecked(original, target) ==
            "test driver stops before replay");
      CHECK(driver.checkedReplacements == 1);
    }
    SECTION("An identical checked target still validates ownership without replaying the frame")
    {
      driver.acceptChecked = true;
      CHECK(controller.ReplaceTargetShaderChecked(original, target).empty());
      CHECK(driver.checkedReplacements == 1);
      CHECK(controller.m_GlobalShaderReplacementGeneration == globalGeneration);
      CHECK(controller.GetShaderReplacementGeneration(80) == eventGeneration);
      CHECK(driver.references[target] == 1);
      CHECK(driver.clearedOverrides == 0);
    }
    SECTION("A target with one owner retains the ordinary final release behavior")
    {
      CHECK(driver.references[target] == 1);
    }

    // The final owner release still performs the normal override/registry invalidation.
    controller.FreeTargetResource(target);
    CHECK(driver.references.empty());
    CHECK(controller.m_TargetResources.empty());
    CHECK(controller.m_GlobalShaderReplacements.empty());
    CHECK(controller.m_EventShaderReplacements.empty());
    CHECK(driver.clearedOverrides == 1);
    CHECK(controller.m_GlobalShaderReplacementGeneration != globalGeneration);
    CHECK(controller.GetShaderReplacementGeneration(80) != eventGeneration);
    const uint32_t releases = driver.releases;
    controller.FreeTargetResource(target);
    CHECK(driver.releases == releases);
    CHECK(controller.ReplaceTargetShaderChecked(original, target)
              .contains("not created by BuildTargetShader"));
  }

  static void CheckTargetShaderShutdown()
  {
    TargetShaderLifetimeTestDriver driver;
    ReplayController *controller = new ReplayController;
    controller->m_pDevice = &driver;
    for(uint32_t i = 0; i < 3; ++i)
    {
      // Two acquisitions share an ID; the third represents an unrelated target.
      if(i == 2)
        driver.target = ResourceIDGen::GetNewUniqueID();
      controller->BuildTargetShader("MainPS", ShaderEncoding::DXIL, bytebuf{1},
                                    ShaderCompileFlags(), ShaderStage::Pixel);
    }
    controller->Shutdown();
    CHECK(driver.references.empty());
    CHECK(driver.releases == 3);
    CHECK(driver.shutdowns == 1);
  }

  static void CheckBorrowedCapturedShaderIdentity()
  {
    TargetShaderLifetimeTestDriver driver;
    ReplayController controller;
    controller.m_pDevice = &driver;
    struct DetachDriver
    {
      ReplayController &controller;
      ~DetachDriver() { controller.m_pDevice = NULL; }
    } detach = {controller};
    const auto build = [&]() {
      return controller
          .BuildTargetShader("MainPS", ShaderEncoding::DXIL, bytebuf{1}, ShaderCompileFlags(),
                             ShaderStage::Pixel)
          .first;
    };

    const ResourceId original = ResourceIDGen::GetNewUniqueID();
    const ResourceId replacement = build();
    controller.m_GlobalShaderReplacements[original] = replacement;
    const auto eventKey = std::make_pair(80U, ShaderStage::Pixel);
    controller.m_EventShaderReplacements[eventKey] = {original, replacement};
    const uint64_t globalGeneration = controller.m_GlobalShaderReplacementGeneration;
    const uint64_t eventGeneration = controller.GetShaderReplacementGeneration(80);

    // Reflecting the original bytecode can borrow the capture-owned shader itself.
    // Its final temporary release must leave the independently owned replacement live.
    driver.target = original;
    REQUIRE(build() == original);
    controller.FreeTargetResource(original);
    CHECK((driver.references.find(original) == driver.references.end()));
    CHECK((controller.m_TargetResources.find(original) == controller.m_TargetResources.end()));
    CHECK(driver.references[replacement] == 1);
    CHECK(controller.m_TargetResources[replacement] == 1);
    CHECK(controller.m_GlobalShaderReplacements[original] == replacement);
    CHECK(controller.m_EventShaderReplacements[eventKey].replacement == replacement);
    CHECK(controller.m_GlobalShaderReplacementGeneration == globalGeneration);
    CHECK(controller.GetShaderReplacementGeneration(80) == eventGeneration);

    controller.FreeTargetResource(replacement);
    CHECK(driver.references.empty());
    CHECK(controller.m_TargetResources.empty());
    CHECK(controller.m_GlobalShaderReplacements.empty());
    CHECK(controller.m_EventShaderReplacements.empty());
  }

  static void CheckIdleCheckpointMaintenance()
  {
    ReplayController controller;
    IdleCheckpointTestDriver driver;
    controller.m_pDevice = &driver;
    struct DetachDriver
    {
      ReplayController &controller;
      ~DetachDriver() { controller.m_pDevice = NULL; }
    } detach = {controller};
    controller.m_EventID = controller.m_MaterializedEventID = 100;
    controller.m_SelectedEventID = 90;
    controller.m_MaterializedEventValid = true;
    controller.m_GlobalShaderReplacementGeneration = controller.m_ShaderReplacementSerial = 7;
    controller.m_SharedBufferPatchGeneration = 5;
    controller.m_CheckpointConfig.enabled = true;
    controller.m_CheckpointConfig.byteBudget = 1024 * 1024;
    controller.m_CheckpointConfig.maxCheckpoints = 3;
    controller.m_CheckpointConfig.minEventGap = 1;
    ReplayCheckpointCandidate earlier, later;
    earlier.eventId = 60;
    later.eventId = 80;
    controller.m_CheckpointCandidates = driver.candidates = {earlier, later};
    driver.stats.supported = driver.stats.enabled = true;
    const auto run = [&]() { return controller.RunIdleCheckpointMaintenance(100, 7, 5); };

    SECTION("Stale event, shader and buffer generations cannot issue replay")
    {
      CHECK(controller.RunIdleCheckpointMaintenance(99, 7, 5).contains("staleEvent"));
      CHECK(controller.RunIdleCheckpointMaintenance(100, 6, 5).contains("staleMutation"));
      CHECK(controller.RunIdleCheckpointMaintenance(100, 7, 4).contains("staleBufferGeneration"));
      CHECK(driver.calls.empty());
      CHECK(controller.m_MaterializedEventValid);
    }
    SECTION("Disabled, unsupported and bypassed drivers cannot start work")
    {
      controller.m_CheckpointConfig.enabled = false;
      CHECK(run().contains("checkpointsDisabled"));
      controller.m_CheckpointConfig.enabled = true;
      driver.stats.supported = false;
      CHECK(run().contains("backendDisabledOrUnsupported"));
      driver.stats.supported = true;
      driver.m_ReplayCheckpointBypass = true;
      CHECK(run().contains("checkpointBypassActive"));
      CHECK(driver.calls.empty());
    }
    SECTION("Analysis, unmaterialized state and local overrides are left alone")
    {
      controller.m_AnalysisReplayState = ReplayController::AnalysisReplayState::Ready;
      CHECK(run().contains("analysisActive"));
      controller.m_AnalysisReplayState = ReplayController::AnalysisReplayState::None;
      controller.m_MaterializedEventValid = false;
      CHECK(run().contains("currentEventNotMaterialized"));
      controller.m_MaterializedEventValid = true;
      ReplayController::EventCBufferOverrideKey key = {};
      key.eventId = 100;
      key.stage = ShaderStage::Pixel;
      controller.m_EventCBufferOverrides.insert(key);
      CHECK(run().contains("eventOverridesActive"));
      CHECK(driver.calls.empty());
    }
    SECTION("Explicit maintenance performs exactly the normal current-event pair")
    {
      driver.m_ReplayCheckpointNaturalReference = true;
      driver.m_ReplayCheckpointPrefixOnly = true;
      const rdcstr result = run();
      CHECK(result.contains("\"performed\":true"));
      CHECK(result.contains("\"reason\":\"checkpointBuilt\""));
      CHECK(result.contains("\"builds\":1"));
      REQUIRE(driver.calls.size() == 2);
      CHECK(driver.calls[0].eventId == 100);
      CHECK(driver.calls[1].eventId == 100);
      CHECK((driver.calls[0].type == eReplay_WithoutDraw));
      CHECK((driver.calls[1].type == eReplay_OnlyDraw));
      CHECK_FALSE(driver.calls[0].bypass);
      CHECK_FALSE(driver.calls[1].bypass);
      CHECK_FALSE(driver.calls[0].naturalReference);
      CHECK_FALSE(driver.calls[1].naturalReference);
      CHECK_FALSE(driver.calls[0].prefixOnly);
      CHECK_FALSE(driver.calls[1].prefixOnly);
      CHECK(driver.m_ReplayCheckpointNaturalReference);
      CHECK(driver.m_ReplayCheckpointPrefixOnly);
      CHECK_FALSE(driver.m_ReplayCheckpointBypass);
      CHECK(driver.configurations.empty());
      CHECK(controller.m_EventID == 100);
      CHECK(controller.m_SelectedEventID == 90);
      CHECK(controller.IsCurrentEventMaterialized());
      CHECK(controller.m_CheckpointAutoReferences == 0);
      CHECK(controller.m_IdleCheckpointWorkUnits == 1);
      CHECK(run().contains("alreadyAttempted"));
      CHECK(driver.calls.size() == 2);
    }
    SECTION("Local shadows bypass target publication without changing the canonical prefix")
    {
      ReplayController::EventCBufferOverrideKey key = {};
      key.eventId = 100;
      key.stage = ShaderStage::Pixel;
      controller.m_EventCBufferOverrides.insert(key);
      controller.ReplayEvent(100, false, false, false);
      REQUIRE(driver.calls.size() == 2);
      CHECK_FALSE(driver.calls[0].bypass);
      CHECK(driver.calls[1].bypass);
      CHECK_FALSE(driver.m_ReplayCheckpointBypass);
      REQUIRE(controller.m_EventCBufferOverrides.size() == 1);
    }
    SECTION("Outer force and analysis bypass survives both target phases")
    {
      driver.m_ReplayCheckpointBypass = true;
      controller.ReplayEvent(100, false, false, false);
      REQUIRE(driver.calls.size() == 2);
      CHECK(driver.calls[0].bypass);
      CHECK(driver.calls[1].bypass);
      CHECK(driver.m_ReplayCheckpointBypass);
    }
    SECTION("A rejected native override cannot publish target resource versions")
    {
      driver.overrideError = "test rejected override";
      controller.ReplayEvent(100, false, false, false);
      REQUIRE(driver.calls.size() == 2);
      CHECK_FALSE(driver.calls[0].bypass);
      CHECK(driver.calls[1].bypass);
      CHECK_FALSE(driver.m_ReplayCheckpointBypass);
      CHECK(controller.m_LastEventOverrideError == driver.overrideError);
    }
    SECTION("An override at another EID cannot suppress the ordinary target lineage")
    {
      ReplayController::EventCBufferOverrideKey key = {};
      key.eventId = 80;
      key.stage = ShaderStage::Pixel;
      controller.m_EventCBufferOverrides.insert(key);
      controller.ReplayEvent(100, false, false, false);
      REQUIRE(driver.calls.size() == 2);
      CHECK_FALSE(driver.calls[0].bypass);
      CHECK_FALSE(driver.calls[1].bypass);
      CHECK_FALSE(driver.m_ReplayCheckpointBypass);
    }
    SECTION("Foreground shared replay leaves the invalidated suffix for idle maintenance")
    {
      driver.patchAnchor = 60;
      driver.stats.events = {60, 80};
      driver.InvalidateReplayCheckpoints(70);
      {
        // The production setter scopes precisely this replay operation; no resource mutation,
        // output or pipeline stubs are required to verify its checkpoint scheduling contract.
        ScopedSharedBufferPatchReplay sharedReplay(controller.m_pDevice,
                                                   controller.m_SharedBufferPatchReplay);
        controller.ReplayEvent(100, false, false, false);
      }
      REQUIRE(driver.calls.size() == 2);
      CHECK(driver.calls[0].prefixOnly);
      CHECK_FALSE(driver.calls[1].prefixOnly);
      CHECK_FALSE(driver.m_ReplayCheckpointPrefixOnly);
      CHECK_FALSE(controller.m_SharedBufferPatchReplay);
      REQUIRE(driver.stats.events.size() == 1);
      CHECK(driver.stats.events[0] == 60);
      CHECK(driver.stats.builds == 0);
      REQUIRE(run().contains("checkpointBuilt"));
      CHECK(driver.stats.events.contains(80U));
      CHECK(driver.stats.builds == 1);
      CHECK(driver.calls.size() == 4);
      CHECK_FALSE(driver.calls[2].prefixOnly);
      CHECK(controller.IsCurrentEventMaterialized());
    }
    SECTION("A backend no-build is truthful and is not retried in the same generation")
    {
      driver.build = false;
      CHECK(run().contains("replayedWithoutNewCheckpoint"));
      CHECK(driver.stats.builds == 0);
      CHECK(driver.calls.size() == 2);
      CHECK(run().contains("alreadyAttempted"));
      CHECK(driver.calls.size() == 2);
    }
    SECTION("A newer same-event mutation or explicit configuration can permit later maintenance")
    {
      REQUIRE(run().contains("checkpointBuilt"));
      controller.m_GlobalShaderReplacementGeneration = 8;
      controller.m_SharedBufferPatchGeneration = 6;
      driver.stats.events.clear();
      driver.stats.checkpointCount = 0;
      CHECK(controller.RunIdleCheckpointMaintenance(100, 8, 6).contains("checkpointBuilt"));
      CHECK(driver.calls.size() == 4);
      CHECK(controller.RunIdleCheckpointMaintenance(100, 8, 6).contains("alreadyAttempted"));
      controller.ConfigureReplayCheckpoints(true, 1024 * 1024, 3);
      // This fake capture has no actions, so explicit replanning correctly leaves no candidate.
      CHECK(controller.RunIdleCheckpointMaintenance(100, 8, 6).contains(
          "noMissingExplicitCandidate"));
      CHECK(driver.calls.size() == 4);
    }
    SECTION("Cold automatic prewarming builds bounded prefixes without changing the selected event")
    {
      controller.m_CheckpointAutomatic = controller.m_CheckpointIdlePrewarm = true;
      controller.m_CheckpointAutoCapacity = 3;
      controller.m_CheckpointConfig.enabled = driver.stats.enabled = false;
      CHECK(run().contains("checkpointBuilt"));
      REQUIRE(driver.calls.size() == 2);
      CHECK(driver.stats.events.contains(60U));
      CHECK_FALSE(driver.stats.events.contains(80U));
      CHECK(controller.m_EventID == 100);
      CHECK(controller.m_SelectedEventID == 90);
      CHECK(controller.IsCurrentEventMaterialized());
      CHECK(controller.m_CheckpointAutoReferences == 0);
      CHECK(controller.m_CheckpointAutoSavingsMS == 0.0);
      CHECK(controller.m_AutomaticCheckpointPoints[0].prewarmed);
      CHECK_FALSE(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(run().contains("checkpointBuilt"));
      CHECK(driver.calls.size() == 4);
      CHECK(driver.stats.events.contains(60U));
      CHECK(driver.stats.events.contains(80U));
      CHECK(run().contains("noMissingIdleCandidate"));
      CHECK(driver.calls.size() == 4);
      const auto request = controller.BeginAutomaticCheckpointRequest(100, 3);
      CHECK(request.active);
      CHECK_FALSE(request.reference);
      CHECK_FALSE(driver.m_ReplayCheckpointBypass);
      controller.EndAutomaticCheckpointRequest(request, 1.0);
      CHECK(controller.m_CheckpointAutoReferences == 0);
    }
    SECTION("Cold prewarming honours capacity and does not spin after rejected native storage")
    {
      controller.m_CheckpointAutomatic = controller.m_CheckpointIdlePrewarm = true;
      controller.m_CheckpointAutoCapacity = 1;
      controller.m_CheckpointConfig.enabled = driver.stats.enabled = false;
      driver.build = false;
      CHECK(run().contains("replayedWithoutNewCheckpoint"));
      CHECK(driver.calls.size() == 2);
      CHECK(controller.m_AutomaticCheckpointPoints.empty());
      CHECK(controller.m_RejectedCheckpointCandidates.count(60));
      CHECK(run().contains("alreadyAttempted"));
      CHECK(driver.calls.size() == 2);
      CHECK(controller.IsCurrentEventMaterialized());
    }
    SECTION(
        "Native planning skips unavailable modules without replay and tries another bounded point")
    {
      controller.m_CheckpointAutomatic = controller.m_CheckpointIdlePrewarm = true;
      controller.m_CheckpointAutoCapacity = 2;
      controller.m_CheckpointConfig.enabled = driver.stats.enabled = false;
      driver.stats.candidatePlanKnown = driver.stats.canTryOtherCandidates = true;
      driver.rejectedPlanEvent = 60;
      const rdcstr unavailable = run();
      CHECK(unavailable.contains("candidateUnavailable"));
      CHECK(unavailable.contains("\"continueIdle\":true"));
      CHECK(unavailable.contains("\"performed\":false"));
      CHECK(driver.calls.empty());
      CHECK(controller.m_RejectedCheckpointCandidates.count(60));
      CHECK(controller.IsCurrentEventMaterialized());
      CHECK(run().contains("checkpointBuilt"));
      CHECK(driver.stats.events.contains(80U));
      CHECK(driver.calls.size() == 2);
      CHECK(controller.m_CheckpointAutoTrials == 2);
      CHECK(run().contains("noMissingIdleCandidate"));
      CHECK(driver.calls.size() == 2);
    }
    SECTION("Unavailable native storage does not replay or keep retrying idle configurations")
    {
      controller.m_CheckpointAutomatic = controller.m_CheckpointIdlePrewarm = true;
      controller.m_CheckpointAutoCapacity = 2;
      controller.m_CheckpointConfig.enabled = driver.stats.enabled = false;
      driver.stats.candidatePlanKnown = true;
      driver.stats.canTryOtherCandidates = false;
      driver.rejectedPlanEvent = 60;
      const rdcstr unavailable = run();
      CHECK(unavailable.contains("candidateUnavailable"));
      CHECK(unavailable.contains("\"continueIdle\":false"));
      CHECK(driver.calls.empty());
      CHECK(run().contains("alreadyAttempted"));
      CHECK(driver.calls.empty());
      CHECK(driver.memoryProbes == 0);
      CHECK(controller.m_MemoryRejectedCheckpointCandidates.empty());
    }
    SECTION(
        "Only measured memory recovery reopens its candidate without forgiving other rejections")
    {
      controller.m_CheckpointAutomatic = controller.m_CheckpointIdlePrewarm = true;
      controller.m_CheckpointAutoCapacity = 2;
      controller.m_CheckpointConfig.enabled = driver.stats.enabled = false;
      driver.stats.candidatePlanKnown = driver.stats.memoryBudgetBlocked = true;
      driver.rejectedPlanEvent = 60;
      controller.m_RejectedCheckpointCandidates.insert(80);
      const rdcstr rejected = run();
      CHECK(rejected.contains("candidateUnavailable"));
      CHECK(rejected.contains("\"continueIdle\":false"));
      CHECK(rejected.contains("\"retryIdleAfterMS\":5000"));
      CHECK(controller.m_MemoryRejectedCheckpointCandidates.count(60));
      CHECK(driver.calls.empty());
      // Removing an automatic trial must preserve the blocked native capability, not disable it.
      controller.ApplyAutomaticCheckpointConfiguration();
      CHECK(driver.stats.enabled);
      CHECK(driver.stats.memoryBudgetBlocked);
      for(uint32_t repeat = 0; repeat < 4; ++repeat)
        CHECK(run().contains("memoryBudgetWaiting"));
      CHECK(driver.memoryProbes == 0);
      controller.m_IdleCheckpointMemoryNextCheck = 0;
      CHECK(run().contains("memoryBudgetWaiting"));
      CHECK(driver.memoryProbes == 1);
      CHECK(driver.calls.empty());
      CHECK(controller.m_RejectedCheckpointCandidates.count(60));
      driver.memoryRecovers = true;
      controller.m_IdleCheckpointMemoryNextCheck = 0;
      CHECK(run().contains("checkpointBuilt"));
      CHECK(driver.memoryProbes == 2);
      CHECK(driver.stats.events.contains(60U));
      CHECK(driver.calls.size() == 2);
      CHECK(controller.m_RejectedCheckpointCandidates.count(60) == 0);
      CHECK(controller.m_RejectedCheckpointCandidates.count(80) == 1);
      CHECK(controller.m_CheckpointAutoTrials == 2);
      CHECK(controller.m_IdleCheckpointMemoryRecoveries == 1);
      CHECK(controller.IsCurrentEventMaterialized());
      CHECK(controller.m_EventID == 100);
      CHECK(controller.m_SelectedEventID == 90);
      CHECK(controller.m_SharedBufferPatchGeneration == 5);
      CHECK(controller.GetShaderReplacementGeneration(100) == 7);
    }
    SECTION("Memory probes honour request identities, bypass, and a finite monotonic timer budget")
    {
      controller.m_CheckpointAutomatic = controller.m_CheckpointIdlePrewarm = true;
      controller.m_CheckpointAutoCapacity = 2;
      controller.m_CheckpointConfig.enabled = driver.stats.enabled = false;
      driver.stats.candidatePlanKnown = driver.stats.memoryBudgetBlocked = true;
      driver.rejectedPlanEvent = 60;
      CHECK(run().contains("candidateUnavailable"));
      controller.m_IdleCheckpointMemoryNextCheck = 0;
      CHECK(controller.RunIdleCheckpointMaintenance(99, 7, 5).contains("staleEvent"));
      CHECK(controller.RunIdleCheckpointMaintenance(100, 6, 5).contains("staleMutation"));
      CHECK(controller.RunIdleCheckpointMaintenance(100, 7, 4).contains("staleBufferGeneration"));
      driver.m_ReplayCheckpointBypass = true;
      CHECK(run().contains("checkpointBypassActive"));
      driver.m_ReplayCheckpointBypass = false;
      CHECK(driver.memoryProbes == 0);
      for(uint32_t attempt = 0; attempt < 16; ++attempt)
      {
        controller.m_IdleCheckpointMemoryNextCheck = 0;
        const rdcstr pending = run();
        CHECK(pending.contains("\"performed\":false"));
        CHECK(pending.contains("\"continueIdle\":false"));
        CHECK(driver.memoryProbes == attempt + 1);
      }
      CHECK(run().contains("memoryBudgetRetryLimit"));
      CHECK(run().contains("\"retryIdleAfterMS\":0"));
      CHECK(driver.memoryProbes == 16);
      CHECK(driver.calls.empty());
      CHECK(controller.m_RejectedCheckpointCandidates.count(60));
      CHECK(controller.IsCurrentEventMaterialized());
    }
    SECTION("Edits and explicit disable cancel pending memory probes")
    {
      controller.m_CheckpointAutomatic = controller.m_CheckpointIdlePrewarm = true;
      controller.m_CheckpointAutoCapacity = 2;
      controller.m_CheckpointConfig.enabled = driver.stats.enabled = false;
      driver.stats.candidatePlanKnown = driver.stats.memoryBudgetBlocked = true;
      driver.rejectedPlanEvent = 60;
      CHECK(run().contains("candidateUnavailable"));
      controller.m_IdleCheckpointMemoryRecoveries = 2;
      controller.m_IdleCheckpointMemoryNextCheck = 0;
      CHECK(run().contains("memoryBudgetRetryLimit"));
      CHECK(driver.memoryProbes == 0);
      controller.ResetAutomaticCheckpointCosts(0);
      CHECK(controller.m_MemoryRejectedCheckpointCandidates.empty());
      CHECK(controller.m_IdleCheckpointMemoryNextCheck == 0);
      CHECK(controller.m_IdleCheckpointMemoryRecoveries == 2);
      controller.m_MemoryRejectedCheckpointCandidates.insert(60);
      controller.m_IdleCheckpointMemoryNextCheck = 1;
      controller.ConfigureReplayCheckpoints(false);
      CHECK(controller.m_MemoryRejectedCheckpointCandidates.empty());
      CHECK(controller.m_IdleCheckpointMemoryNextCheck == 0);
      CHECK_FALSE(driver.stats.memoryBudgetBlocked);
      CHECK(run().contains("checkpointsDisabled"));
      CHECK(driver.memoryProbes == 0);
    }
    SECTION("Automatic maintenance without prewarming requires existing measured usefulness")
    {
      controller.m_CheckpointAutomatic = true;
      ReplayController::AutomaticCheckpointPoint point;
      point.candidate = later;
      point.nativeEvents.insert(80);
      point.profitableHits = 2;
      point.baselineMS = 100.0;
      point.lastHitMS = 40.0;
      controller.m_AutomaticCheckpointPoints.push_back(point);
      CHECK(run().contains("noMissingMeasuredUsefulPoint"));
      CHECK(driver.calls.empty());
    }
    SECTION("Same-layout value edits preserve measured hints after native suffix invalidation")
    {
      controller.m_CheckpointAutomatic = true;
      ReplayController::AutomaticCheckpointPoint point;
      point.candidate = later;
      point.nativeEvents.insert(80);
      point.retained = true;
      point.profitableHits = 2;
      point.baselineMS = 100.0;
      point.lastHitMS = 40.0;
      controller.m_AutomaticCheckpointPoints.push_back(point);
      driver.stats.events = {80};
      driver.stats.checkpointCount = 1;
      // CommitSharedBufferPatches preserves admission history only for a proven nonzero cutoff;
      // the driver invalidates the native suffix and the foreground publishes the new generation.
      driver.InvalidateReplayCheckpoints(70);
      controller.m_GlobalShaderReplacementGeneration = 8;
      controller.m_SharedBufferPatchGeneration = 6;
      CHECK(controller.RunIdleCheckpointMaintenance(100, 7, 5).contains("staleMutation"));
      REQUIRE(controller.RunIdleCheckpointMaintenance(100, 8, 6).contains("checkpointBuilt"));
      CHECK(driver.stats.events.contains(80U));
      CHECK(driver.calls.size() == 2);
      CHECK(controller.m_AutomaticCheckpointPoints[0].profitableHits == 0);
      CHECK(controller.m_CheckpointAutoReferences == 0);
      driver.InvalidateReplayCheckpoints(0);
      controller.ResetAutomaticCheckpointCosts(0);
      CHECK(controller.RunIdleCheckpointMaintenance(100, 8, 6).contains(
          "noMissingMeasuredUsefulPoint"));
      CHECK(driver.calls.size() == 2);
    }
    SECTION("Automatic maintenance restores policy and charges work without inventing hit evidence")
    {
      controller.m_CheckpointAutomatic = true;
      controller.m_CheckpointAutoConfigurationDirty = true;
      ReplayController::AutomaticCheckpointPoint existing, selected, unmeasured;
      existing.candidate = earlier;
      existing.nativeEvents.insert(60);
      selected.candidate = later;
      selected.nativeEvents.insert(80);
      selected.retained = true;
      selected.profitableHits = 2;
      selected.baselineMS = 100.0;
      selected.lastHitMS = 40.0;
      selected.debtMS = 3.0;
      unmeasured.candidate.eventId = 95;
      controller.m_AutomaticCheckpointPoints = {existing, selected, unmeasured};
      driver.stats.events = {60};
      driver.stats.checkpointCount = 1;
      REQUIRE(run().contains("checkpointBuilt"));
      REQUIRE(driver.configurations.size() == 2);
      CHECK(driver.configurations[0].size() == 2);
      CHECK_FALSE(ReplayCheckpointCandidateContains(driver.configurations[0], 95));
      CHECK(driver.configurations[1].size() == 3);
      CHECK(ReplayCheckpointCandidateContains(driver.configurations[1], 80));
      CHECK(ReplayCheckpointCandidateContains(driver.configurations[1], 95));
      CHECK(driver.stats.events.contains(60U));
      CHECK(driver.stats.events.contains(80U));
      CHECK_FALSE(driver.stats.events.contains(95U));
      CHECK(controller.m_CheckpointAutoDebtMS == controller.m_IdleCheckpointTotalMS);
      CHECK(controller.m_CheckpointAutoDebtMS > 0.0);
      CHECK(controller.m_AutomaticCheckpointPoints[1].debtMS ==
            3.0 + controller.m_IdleCheckpointTotalMS);
      CHECK_FALSE(controller.m_AutomaticCheckpointPoints[1].retained);
      CHECK(controller.m_AutomaticCheckpointPoints[1].hits == 0);
      CHECK(controller.m_AutomaticCheckpointPoints[1].profitableHits == 0);
      CHECK(controller.m_CheckpointAutoReferences == 0);
      CHECK(controller.m_CheckpointAutoSavingsMS == 0.0);
      CHECK(controller.m_CheckpointReferenceCosts.empty());
      CHECK_FALSE(controller.m_CheckpointAutoConfigurationDirty);
    }
    SECTION("Reentrant maintenance cannot submit a second work unit")
    {
      uint32_t reentries = 0;
      driver.onReplay = [&]() {
        CHECK(run().contains("alreadyRunning"));
        ++reentries;
      };
      CHECK(run().contains("checkpointBuilt"));
      CHECK(driver.calls.size() == 2);
      CHECK(reentries == 2);
      CHECK_FALSE(controller.m_IdleCheckpointMaintenance);
    }
    SECTION("Late mutation is not overwritten or published as the old event state")
    {
      driver.onReplay = [&]() { controller.m_GlobalShaderReplacementGeneration = 9; };
      CHECK(run().contains("replayFailedOrStateChanged"));
      CHECK(controller.m_GlobalShaderReplacementGeneration == 9);
      CHECK_FALSE(controller.m_MaterializedEventValid);
      CHECK_FALSE(controller.m_IdleCheckpointMaintenance);
    }
    SECTION("Native failure leaves the current event explicitly unmaterialized")
    {
      driver.fail = true;
      CHECK(run().contains("replayFailedOrStateChanged"));
      CHECK_FALSE(controller.m_MaterializedEventValid);
      CHECK_FALSE(controller.m_IdleCheckpointMaintenance);
      CHECK(controller.m_EventID == 100);
      CHECK(controller.m_SelectedEventID == 90);
      CHECK(driver.shutdowns == 1);
      CHECK(driver.calls.empty());
      CHECK(controller.m_IdleCheckpointWorkUnits == 0);
    }
  }

  static void CheckAutomaticPrewarmLifetime()
  {
    ReplayController controller;
    IdleCheckpointTestDriver driver;
    controller.m_pDevice = &driver;
    struct DetachDriver
    {
      ReplayController &controller;
      ~DetachDriver() { controller.m_pDevice = NULL; }
    } detach = {controller};
    controller.m_CheckpointAutomatic = true;
    controller.m_CheckpointAutoCapacity = 1;
    controller.m_CheckpointAutoTrials = 1;
    controller.m_CheckpointConfig.enabled = true;
    controller.m_CheckpointConfig.byteBudget = 1024 * 1024;
    controller.m_CheckpointConfig.maxCheckpoints = 1;
    controller.m_CheckpointConfig.minEventGap = 1;
    ReplayController::AutomaticCheckpointPoint point;
    point.candidate.eventId = 60;
    point.prewarmed = true;
    point.builds = 1;
    point.nativeEvents.insert(60);
    controller.m_AutomaticCheckpointPoints = {point};
    controller.m_CheckpointCandidates = driver.candidates = {point.candidate};
    driver.stats.supported = driver.stats.enabled = true;
    driver.stats.events = {60};
    driver.stats.checkpointCount = 1;
    driver.build = false;
    const auto visit = [&](uint32_t event, double hitMS) {
      const size_t calls = driver.calls.size();
      const auto request = controller.BeginAutomaticCheckpointRequest(event, 0);
      REQUIRE(request.active);
      CHECK(driver.calls.size() == calls);
      driver.ReplayLog(event, eReplay_WithoutDraw);
      driver.ReplayLog(event, eReplay_OnlyDraw);
      controller.EndAutomaticCheckpointRequest(request, request.reference ? 100.0 : hitMS);
      CHECK(driver.calls.size() == calls + 2);
      CHECK_FALSE(driver.m_ReplayCheckpointBypass);
      CHECK_FALSE(driver.m_ReplayCheckpointNaturalReference);
      return request.reference;
    };
    SECTION("Continuously new EIDs expire every unproven point, including a shadowed one")
    {
      point.candidate.eventId = 40;
      point.nativeEvents = {40};
      controller.m_AutomaticCheckpointPoints.push_back(point);
      controller.m_CheckpointAutoCapacity = 2;
      controller.m_CheckpointAutoTrials = 2;
      controller.m_CheckpointCandidates.push_back(point.candidate);
      driver.stats.events = {40, 60};
      driver.stats.checkpointCount = 2;
      for(uint32_t i = 0; i < CheckpointPrewarmTrialRequestLimit; ++i)
      {
        CHECK(visit(100 + i, 130.0) == (i >= CheckpointUnmeasuredHitLimit));
        if(i + 1 < CheckpointPrewarmTrialRequestLimit)
        {
          REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 2);
          CHECK(controller.m_AutomaticCheckpointPoints[0].requests == i + 1);
          CHECK(controller.m_AutomaticCheckpointPoints[1].requests == i + 1);
        }
      }
      CHECK(controller.m_AutomaticCheckpointPoints.empty());
      CHECK(controller.m_RejectedCheckpointCandidates.count(40) == 1);
      CHECK(controller.m_RejectedCheckpointCandidates.count(60) == 1);
      CHECK(controller.m_CheckpointAutoReferences ==
            CheckpointPrewarmTrialRequestLimit - CheckpointUnmeasuredHitLimit);
      CHECK(controller.m_CheckpointAutoSavingsMS == 0.0);
      CHECK(visit(130, 130.0));
      CHECK(driver.stats.events.empty());
      CHECK_FALSE(driver.stats.enabled);
      CHECK(controller.m_CheckpointAutoTrials == 2);
    }
    SECTION("Invalid and very slow timings still exhaust the finite trial")
    {
      const double samples[] = {0.0,
                                -1.0,
                                60000.0,
                                60001.0,
                                std::numeric_limits<double>::quiet_NaN(),
                                std::numeric_limits<double>::infinity()};
      for(uint32_t i = 0; i < CheckpointPrewarmTrialRequestLimit; ++i)
        visit(100 + i, samples[i % ARRAY_COUNT(samples)]);
      CHECK(controller.m_AutomaticCheckpointPoints.empty());
      CHECK(controller.m_RejectedCheckpointCandidates.count(60) == 1);
      CHECK(controller.m_CheckpointAutoReferences == 0);
      CHECK(controller.m_CheckpointAutoSavingsMS == 0.0);
      CHECK(visit(130, 100.0));
      CHECK(driver.stats.events.empty());
    }
    SECTION("Same-EID natural references retain only a measured profitable point")
    {
      for(uint32_t i = 0; i < CheckpointUnmeasuredHitLimit; ++i)
        CHECK_FALSE(visit(100, 70.0));
      CHECK(visit(100, 70.0));
      CHECK(visit(100, 70.0));
      CHECK_FALSE(visit(100, 70.0));
      CHECK_FALSE(visit(100, 70.0));
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_AutomaticCheckpointPoints[0].profitableHits == 2);
      CHECK(controller.m_AutomaticCheckpointPoints[0].unmeasuredHits == 0);
      for(uint32_t i = 0; i < CheckpointPrewarmTrialRequestLimit; ++i)
        visit(100, 70.0);
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_RejectedCheckpointCandidates.empty());
    }
    SECTION("Retained measured points do not force periodic full-prefix timing audits")
    {
      controller.m_AutomaticCheckpointPoints[0].retained = true;
      auto &reference = controller.m_CheckpointReferenceCosts[uint64_t(100) << 3];
      reference.Add(100.0);
      reference.Add(100.0);
      reference.hitsSinceReference = 100;
      CHECK_FALSE(visit(100, 70.0));
      CHECK(controller.m_CheckpointAutoReferences == 0);
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
    }
    SECTION("Parameter trials retain the capture budget across later shader and EID visits")
    {
      controller.m_CheckpointConfig.byteBudget = 256ULL * 1024 * 1024;
      controller.m_CheckpointAutoTrials = 0;
      controller.m_AutomaticCheckpointPoints.clear();
      driver.stats.events.clear();
      driver.patchAnchor = 60;
      controller.m_CheckpointReferenceCosts[(uint64_t(100) << 3) | 4].Add(100.0);
      const auto request = controller.BeginAutomaticCheckpointRequest(100, 4);
      CHECK(request.active);
      CHECK_FALSE(request.reference);
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 1);
      CHECK(controller.m_CheckpointConfig.byteBudget == 256ULL * 1024 * 1024);
      controller.EndAutomaticCheckpointRequest(request, 70.0);
      controller.ResetAutomaticCheckpointCosts(0);
      CHECK(controller.m_CheckpointConfig.byteBudget == 256ULL * 1024 * 1024);
    }
    SECTION("A fallback native point before an edit survives a later requested module boundary")
    {
      controller.m_AutomaticCheckpointPoints[0].candidate.eventId = 80;
      controller.m_AutomaticCheckpointPoints[0].nativeEvents = {60, 75};
      controller.ResetAutomaticCheckpointCosts(70);
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[0].candidate.eventId == 69);
      CHECK(controller.m_AutomaticCheckpointPoints[0].nativeEvents == std::set<uint32_t>{60});
      CHECK_FALSE(controller.m_AutomaticCheckpointPoints[0].retained);
      controller.ResetAutomaticCheckpointCosts(60);
      CHECK(controller.m_AutomaticCheckpointPoints.empty());
    }
    SECTION("Forward materialized visits do not train or bypass native continuation")
    {
      controller.m_MaterializedEventID = 90;
      controller.m_MaterializedEventValid = true;
      controller.ReplayEvent(100, false, false, false);
      REQUIRE(driver.calls.size() == 2);
      CHECK_FALSE(driver.calls[0].bypass);
      CHECK_FALSE(driver.calls[0].naturalReference);
      CHECK(controller.m_CheckpointReferenceCosts.empty());
      CHECK(controller.m_CheckpointAutoReferences == 0);
      CHECK(controller.m_AutomaticCheckpointPoints[0].requests == 0);
    }
    SECTION("A comparable hit without savings retires the point and suppresses retry")
    {
      for(uint32_t i = 0; i < CheckpointUnmeasuredHitLimit; ++i)
        CHECK_FALSE(visit(100, 100.0));
      CHECK(visit(100, 100.0));
      CHECK(visit(100, 100.0));
      CHECK_FALSE(visit(100, 100.0));
      CHECK(controller.m_AutomaticCheckpointPoints.empty());
      CHECK(controller.m_RejectedCheckpointCandidates.count(60) == 1);
      CHECK(visit(100, 100.0));
      CHECK(driver.stats.events.empty());
      CHECK(controller.m_CheckpointAutoTrials == 1);
    }
    SECTION("The real ReplayEvent reference uses exactly its ordinary two native phases")
    {
      controller.m_AutomaticCheckpointPoints[0].unmeasuredHits = CheckpointUnmeasuredHitLimit;
      controller.m_EventID = controller.m_SelectedEventID = 100;
      controller.ReplayEvent(100, false, false, false);
      REQUIRE(driver.calls.size() == 2);
      CHECK(driver.calls[0].type == eReplay_WithoutDraw);
      CHECK(driver.calls[1].type == eReplay_OnlyDraw);
      CHECK(driver.calls[0].bypass);
      CHECK(driver.calls[1].bypass);
      CHECK(driver.calls[0].naturalReference);
      CHECK(driver.calls[1].naturalReference);
      CHECK(controller.m_CheckpointAutoReferences == 1);
      CHECK(driver.stats.builds == 0);
      CHECK(driver.stats.hits == 0);
      CHECK(controller.IsCurrentEventMaterialized());
    }
  }

  static void CheckAutomaticCheckpointAdmission()
  {
    ReplayController controller;
    controller.m_CheckpointAutomatic = true;
    controller.m_CheckpointAutoCapacity = 3;
    const uint64_t key = (uint64_t(300) << 3) | 3;
    ReplayController::AutomaticCheckpointRequest request;
    request.active = true;
    request.key = key;
    controller.m_CheckpointReferenceCosts[key].Add(100.0);
    controller.m_CheckpointReferenceCosts[key].Add(102.0);
    ReplayController::AutomaticCheckpointPoint point;
    point.candidate.eventId = 200;
    point.candidate.moduleId = 2;
    point.baselineMS = 100.0;
    controller.m_AutomaticCheckpointPoints.push_back(point);
    ReplayCheckpointStats after;

    SECTION("An inactive forced or analysis request cannot train or reject a point")
    {
      request.active = false;
      after.builds = after.hits = 100;
      controller.ObserveAutomaticCheckpointRequest(request, 1000.0, after);
      CHECK(controller.m_AutomaticCheckpointPoints[0].requests == 0);
      CHECK(controller.m_CheckpointAutoDebtMS == 0.0);
      CHECK(controller.m_CheckpointAutoReferences == 0);
    }
    SECTION("Natural references never count as checkpoint hit evidence")
    {
      request.reference = true;
      controller.ObserveAutomaticCheckpointRequest(request, 80.0, after);
      CHECK(controller.m_CheckpointAutoReferences == 1);
      CHECK(controller.m_CheckpointReferenceCosts[key].Baseline() == 80.0);
      CHECK(controller.m_AutomaticCheckpointPoints[0].hits == 0);
      CHECK(controller.m_AutomaticCheckpointPoints[0].requests == 1);
    }
    SECTION("Building and two real independent hits repay cost before retention")
    {
      request.configurationMS = 3.0;
      after.builds = 1;
      after.events = {190};
      controller.ObserveAutomaticCheckpointRequest(request, 130.0, after);
      CHECK(controller.m_AutomaticCheckpointPoints[0].debtMS == 33.0);
      CHECK(controller.m_CheckpointAutoDebtMS == 33.0);
      CHECK(controller.m_AutomaticCheckpointPoints[0].hits == 0);
      CHECK_FALSE(controller.m_AutomaticCheckpointPoints[0].retained);
      request.configurationMS = 0.0;
      request.before = after;
      after.hits++;
      after.lastResumeEvent = 190;
      controller.ObserveAutomaticCheckpointRequest(request, 70.0, after);
      CHECK(controller.m_AutomaticCheckpointPoints[0].debtMS == 3.0);
      CHECK_FALSE(controller.m_AutomaticCheckpointPoints[0].retained);
      request.before = after;
      after.hits++;
      controller.ObserveAutomaticCheckpointRequest(request, 72.0, after);
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_AutomaticCheckpointPoints[0].debtMS == 0.0);
      CHECK(controller.m_CheckpointAutoSavingsMS == 58.0);
    }
    SECTION("A hit in a call that also builds is not a clean restore timing sample")
    {
      after.builds = after.hits = 1;
      after.events = {190};
      after.lastResumeEvent = 190;
      controller.ObserveAutomaticCheckpointRequest(request, 40.0, after);
      CHECK(controller.m_AutomaticCheckpointPoints[0].builds == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[0].hits == 0);
      CHECK_FALSE(controller.m_AutomaticCheckpointPoints[0].retained);
    }
    SECTION("A slow restore is rejected without another speculative replay")
    {
      controller.m_AutomaticCheckpointPoints[0].nativeEvents.insert(190);
      after.hits = 1;
      after.lastResumeEvent = 190;
      controller.ObserveAutomaticCheckpointRequest(request, 120.0, after);
      CHECK(controller.m_AutomaticCheckpointPoints.empty());
      CHECK(controller.m_RejectedCheckpointCandidates.count(200) == 1);
      CHECK(controller.m_CheckpointAutoConfigurationDirty);
      CHECK(strcmp(controller.m_CheckpointAutoState, "rejected") == 0);
    }
    SECTION("A retained old point cannot masquerade as a successful new trial")
    {
      controller.m_AutomaticCheckpointPoints[0].retained = true;
      controller.m_AutomaticCheckpointPoints[0].nativeEvents.insert(190);
      point.candidate.eventId = 250;
      point.candidate.moduleId = 3;
      controller.m_AutomaticCheckpointPoints.push_back(point);
      after.lastResumeEvent = 190;
      for(uint32_t visit = 0; visit < 2; visit++)
      {
        request.before = after;
        after.hits++;
        controller.ObserveAutomaticCheckpointRequest(request, 70.0, after);
      }
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[0].candidate.eventId == 200);
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_RejectedCheckpointCandidates.count(250) == 1);
    }
    SECTION("A failed new trial keeps its configuration debt without evicting a useful old hit")
    {
      auto &existing = controller.m_AutomaticCheckpointPoints[0];
      existing.retained = true;
      existing.hits = existing.profitableHits = 2;
      existing.nativeEvents.insert(190);
      point.candidate.eventId = 250;
      point.candidate.moduleId = 3;
      controller.m_AutomaticCheckpointPoints.push_back(point);
      after.events = {190};
      after.hits = 2;
      request.before = after;
      after.hits++;
      after.lastResumeEvent = 190;
      request.configurationMS = 20.0;
      // The native old hit saves 15 ms. Charging the new trial's 20 ms to this third hit would
      // reject it, even though the same native checkpoint continues to be profitable.
      controller.ObserveAutomaticCheckpointRequest(request, 85.0, after);
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 2);
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_AutomaticCheckpointPoints[0].lastHitMS == 85.0);
      CHECK(controller.m_AutomaticCheckpointPoints[0].debtMS == 0.0);
      CHECK(controller.m_AutomaticCheckpointPoints[1].debtMS == 20.0);
      CHECK(controller.m_AutomaticCheckpointPoints[1].hits == 0);
      CHECK(controller.m_CheckpointAutoDebtMS == 20.0);
      request.before = after;
      request.configurationMS = 0.0;
      after.hits++;
      controller.ObserveAutomaticCheckpointRequest(request, 84.0, after);
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[0].candidate.eventId == 200);
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_AutomaticCheckpointPoints[0].hits == 4);
      CHECK(controller.m_RejectedCheckpointCandidates.count(250) == 1);
      CHECK(controller.m_CheckpointAutoDebtMS == 20.0);
      CHECK(controller.m_CheckpointAutoSavingsMS == 31.0);
      CHECK(controller.m_CheckpointReferenceCosts[key].hitsSinceReference == 2);
    }
    SECTION("A new native build costs its trial and a forced request cannot repay it")
    {
      auto &existing = controller.m_AutomaticCheckpointPoints[0];
      existing.retained = true;
      existing.hits = existing.profitableHits = 2;
      existing.nativeEvents.insert(190);
      point.candidate.eventId = 250;
      point.candidate.moduleId = 3;
      controller.m_AutomaticCheckpointPoints.push_back(point);
      after.events = {190};
      after.builds = 1;
      after.hits = 2;
      request.before = after;
      after.events.push_back(240);
      after.builds++;
      after.hits++;
      after.lastSaveEvent = 240;
      after.lastResumeEvent = 190;
      request.configurationMS = 7.0;
      controller.ObserveAutomaticCheckpointRequest(request, 125.0, after);
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 2);
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_AutomaticCheckpointPoints[0].hits == 2);
      CHECK(controller.m_AutomaticCheckpointPoints[0].debtMS == 0.0);
      CHECK(controller.m_AutomaticCheckpointPoints[1].builds == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[1].nativeEvents.count(240) == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[1].debtMS == 32.0);
      CHECK(controller.m_CheckpointAutoDebtMS == 32.0);
      CHECK(controller.m_CheckpointAutoSavingsMS == 0.0);
      request.active = false;
      after.hits += 100;
      after.lastResumeEvent = 240;
      controller.ObserveAutomaticCheckpointRequest(request, 1.0, after);
      CHECK(controller.m_AutomaticCheckpointPoints[1].hits == 0);
      CHECK(controller.m_AutomaticCheckpointPoints[1].debtMS == 32.0);
      CHECK(controller.m_CheckpointAutoDebtMS == 32.0);
      CHECK(controller.m_CheckpointAutoSavingsMS == 0.0);
      CHECK(controller.m_CheckpointAutoReferences == 0);
    }
    SECTION("Tiny value edits require measured savings but not a two millisecond floor")
    {
      auto &reference = controller.m_CheckpointReferenceCosts[key];
      reference = {};
      reference.Add(0.8);
      controller.m_AutomaticCheckpointPoints[0].bufferEdit = true;
      controller.m_AutomaticCheckpointPoints[0].nativeEvents.insert(190);
      after.lastResumeEvent = 190;
      for(uint32_t visit = 0; visit < 2; visit++)
      {
        request.before = after;
        after.hits++;
        controller.ObserveAutomaticCheckpointRequest(request, 0.5, after);
      }
      CHECK(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_AutomaticCheckpointPoints[0].profitableHits == 2);
    }
    SECTION("Local invalidation retains the upstream version but discards downstream costs")
    {
      point.candidate.eventId = 400;
      controller.m_AutomaticCheckpointPoints.push_back(point);
      controller.m_AutomaticCheckpointPoints[0].retained = true;
      controller.m_AutomaticCheckpointPoints[0].nativeEvents.insert(190);
      controller.m_CheckpointReferenceCosts[(uint64_t(150) << 3) | 3].Add(50.0);
      controller.ResetAutomaticCheckpointCosts(300);
      REQUIRE(controller.m_AutomaticCheckpointPoints.size() == 1);
      CHECK(controller.m_AutomaticCheckpointPoints[0].nativeEvents.count(190) == 1);
      CHECK_FALSE(controller.m_AutomaticCheckpointPoints[0].retained);
      CHECK(controller.m_CheckpointReferenceCosts.count(key) == 0);
      CHECK(controller.m_CheckpointReferenceCosts.count((uint64_t(150) << 3) | 3) == 1);
      controller.ResetAutomaticCheckpointCosts(0);
      CHECK(controller.m_AutomaticCheckpointPoints.empty());
      CHECK(controller.m_CheckpointReferenceCosts.empty());
    }
  }

  static void CheckAnalysisReadPolicy()
  {
    using AnalysisState = ReplayController::AnalysisReplayState;
    using ReadSource = ReplayController::ExplicitResourceReadSource;
    ReplayController controller;
    controller.m_EventID = 100;
    controller.m_MaterializedEventID = 100;
    controller.m_MaterializedEventValid = true;

    // No native driver is attached. Reaching a replay or readback below would fail the test,
    // proving that an unfinished analysis cannot silently substitute canonical resource bytes.
    SECTION("Ordinary reads use the materialized selected event")
    {
      CHECK((controller.GetExplicitResourceReadSource() == ReadSource::CurrentEvent));
      CHECK(controller.IsCurrentEventMaterialized());
      CHECK(controller.EnsureExplicitResourceRead("unit-test"));
    }
    SECTION("Pending or failed analysis rejects all explicit resource read APIs")
    {
      controller.m_AnalysisReplayState = AnalysisState::PendingOrFailed;
      controller.m_AnalysisReplayEventID = 200;
      CHECK((controller.GetExplicitResourceReadSource() == ReadSource::Unavailable));
      CHECK_FALSE(controller.IsCurrentEventMaterialized());
      CHECK_FALSE(controller.EnsureExplicitResourceRead("unit-test"));
      CHECK(controller.GetBufferData(ResourceId(), 0, 16).empty());
      CHECK(controller.GetTextureData(ResourceId(), Subresource()).empty());
      EXPECT_ERROR();
      CHECK(controller.SaveTexture(TextureSave(), "unused-analysis-test.png").code ==
            ResultCode::APIReplayFailed);
      CHECK(DID_ERROR_HAPPEN());
      CHECK(controller.m_EventID == 100);
      CHECK((controller.m_AnalysisReplayState == AnalysisState::PendingOrFailed));
    }
    SECTION("Ready analysis is readable but cannot satisfy ordinary current-event guards")
    {
      controller.m_AnalysisReplayState = AnalysisState::Ready;
      for(uint32_t eventId : {100U, 200U})
      {
        controller.m_AnalysisReplayEventID = eventId;
        CHECK((controller.GetExplicitResourceReadSource() == ReadSource::AnalysisReplay));
        CHECK(controller.EnsureExplicitResourceRead("unit-test"));
        CHECK_FALSE(controller.IsCurrentEventMaterialized());
        CHECK(controller.m_EventID == 100);
      }
    }
    SECTION("A second analysis cannot inherit the first analysis readiness")
    {
      controller.m_AnalysisReplayState = AnalysisState::Ready;
      controller.m_AnalysisReplayEventID = 200;
      controller.m_MaterializedEventValid = false;
      controller.RestoreAnalysisEventOverrides();
      CHECK((controller.m_AnalysisReplayState == AnalysisState::None));
      CHECK(controller.m_AnalysisReplayEventID == 0);
      CHECK_FALSE(controller.IsCurrentEventMaterialized());

      // ReplayFrameEventForAnalysis starts this pending state before calling the driver and only
      // promotes it after replay plus override application succeeds.
      controller.m_AnalysisReplayState = AnalysisState::PendingOrFailed;
      controller.m_AnalysisReplayEventID = 300;
      CHECK_FALSE(controller.EnsureExplicitResourceRead("unit-test"));
      controller.m_AnalysisReplayState = AnalysisState::Ready;
      CHECK(controller.EnsureExplicitResourceRead("unit-test"));
      CHECK(controller.m_AnalysisReplayEventID == 300);
      CHECK(controller.m_EventID == 100);
    }
    SECTION("Mutation invalidates analysis until explicit restoration or another analysis")
    {
      controller.m_AnalysisReplayState = AnalysisState::Ready;
      controller.m_AnalysisReplayEventID = 200;
      controller.EndAnalysisForMutation();
      CHECK((controller.m_AnalysisReplayState == AnalysisState::PendingOrFailed));
      CHECK(controller.m_AnalysisReplayEventID == 0);
      CHECK_FALSE(controller.IsCurrentEventMaterialized());
      CHECK_FALSE(controller.EnsureExplicitResourceRead("unit-test"));
      controller.EndAnalysisForMutation();
      CHECK_FALSE(controller.EnsureExplicitResourceRead("unit-test"));

      controller.RestoreAnalysisEventOverrides();
      CHECK((controller.GetExplicitResourceReadSource() == ReadSource::CurrentEvent));
      CHECK_FALSE(controller.IsCurrentEventMaterialized());
      CHECK(controller.m_EventID == 100);
    }
    SECTION("Fatal replay error always rejects analysis readback")
    {
      controller.m_AnalysisReplayState = AnalysisState::Ready;
      controller.m_AnalysisReplayEventID = 200;
      controller.m_FatalError = ResultCode::APIReplayFailed;
      CHECK((controller.GetExplicitResourceReadSource() == ReadSource::Unavailable));
      CHECK_FALSE(controller.EnsureExplicitResourceRead("unit-test"));
    }
  }
};

TEST_CASE("Event Shader overlays preserve pre-debug state across APIs and remote replay",
          "[replay][event-shader][lifetime]")
{
  ReplayControllerTestAccess::CheckEventShaderOverlay();
}

TEST_CASE("Event Shader remote failures preserve acknowledged state and latch fatal status",
          "[replay][event-shader][remote][lifetime]")
{
  ReplayControllerTestAccess::CheckEventShaderFatalErrors();
}

TEST_CASE("Event Shader variant caches separate exact live pipeline and output identities",
          "[replay][event-shader][cache]")
{
  EventShaderPipelineKey first;
  first.pipeline = ResourceIDGen::GetNewUniqueID();
  first.replacements[4] = ResourceIDGen::GetNewUniqueID();
  std::map<EventShaderPipelineKey, uint32_t> cache;
  cache[first] = 1;
  CHECK(cache[first] == 1);
  EventShaderPipelineKey other = first;
  other.pipeline = ResourceIDGen::GetNewUniqueID();
  cache[other] = 2;
  other = first;
  other.outputTarget = 0;
  cache[other] = 3;
  other = first;
  other.eventId = 123;
  cache[other] = 4;
  other = first;
  other.replacements[4] = ResourceIDGen::GetNewUniqueID();
  cache[other] = 5;
  CHECK(cache.size() == 5);
  CHECK(first.Uses(first.replacements[4]));
  CHECK_FALSE(first.Uses(other.replacements[4]));
}

TEST_CASE("Shared Shader ownership queries survive EID changes and preserve prior replacements",
          "[replay][shader][shared-shader]")
{
  ReplayControllerTestAccess::CheckSharedShaderReplacementQuery();
}

TEST_CASE("Deduplicated target shader builds retain each live owner until its final release",
          "[replay][shader][target-lifetime]")
{
  ReplayControllerTestAccess::CheckTargetShaderLifetime();
}

TEST_CASE("Replay shutdown balances every deduplicated target shader acquisition",
          "[replay][shader][target-lifetime]")
{
  ReplayControllerTestAccess::CheckTargetShaderShutdown();
}

TEST_CASE("Replay shutdown handles a controller whose device creation failed", "[replay][shutdown]")
{
  ReplayController *controller = new ReplayController;
  controller->Shutdown();
  SUCCEED("An uninitialised driver has no resources to release");
}

TEST_CASE("Releasing a borrowed captured shader preserves its live replacement",
          "[replay][shader][target-lifetime]")
{
  ReplayControllerTestAccess::CheckBorrowedCapturedShaderIdentity();
}

TEST_CASE("Automatic checkpoints require independent measured benefit and preserve valid prefixes",
          "[replay][checkpoints][automatic]")
{
  ReplayControllerTestAccess::CheckAutomaticCheckpointAdmission();
}

TEST_CASE("Idle checkpoint maintenance preserves the current event and counts actual bounded work",
          "[replay][checkpoints][idle]")
{
  ReplayControllerTestAccess::CheckIdleCheckpointMaintenance();
}

TEST_CASE("Automatic prewarmed checkpoints cannot extend an unproven trial indefinitely",
          "[replay][checkpoints][automatic][idle]")
{
  ReplayControllerTestAccess::CheckAutomaticPrewarmLifetime();
}

TEST_CASE("Shared replay checkpoint intent is scoped across analysis and fatal driver replacement",
          "[replay][checkpoints][bufferpatch]")
{
  IdleCheckpointTestDriver original, dummy;
  IReplayDriver *driver = &original;
  bool sharedReplay = false;
  {
    ScopedSharedBufferPatchReplay edit(driver, sharedReplay);
    CHECK(sharedReplay);
    CHECK(driver->m_ReplayCheckpointPrefixOnly);
    CHECK(driver->m_ReplayBufferMutationRoot);
    {
      ScopedCheckpointAnalysisBypass analysis(driver);
      CHECK(driver->m_ReplayCheckpointBypass);
      CHECK_FALSE(driver->m_ReplayCheckpointPrefixOnly);
      CHECK_FALSE(driver->m_ReplayBufferMutationRoot);
    }
    CHECK(driver->m_ReplayCheckpointPrefixOnly);
    CHECK_FALSE(driver->m_ReplayCheckpointBypass);
    CHECK(driver->m_ReplayBufferMutationRoot);
    // FatalErrorCheck swaps this pointer while the original object is no longer safe to touch.
    driver = &dummy;
    dummy.m_ReplayCheckpointPrefixOnly = true;
    dummy.m_ReplayBufferMutationRoot = true;
  }
  CHECK_FALSE(sharedReplay);
  CHECK_FALSE(dummy.m_ReplayCheckpointPrefixOnly);
  CHECK_FALSE(dummy.m_ReplayBufferMutationRoot);
  {
    dummy.m_ReplayCheckpointPrefixOnly = true;
    sharedReplay = true;
    ScopedSharedBufferPatchReplay nested(driver, sharedReplay);
  }
  CHECK(sharedReplay);
  CHECK(dummy.m_ReplayCheckpointPrefixOnly);
}

TEST_CASE("Checked shader replay intent cannot escape into analysis or a later force replay",
          "[replay][shader][checkpoints]")
{
  IdleCheckpointTestDriver original, dummy;
  IReplayDriver *driver = &original;
  {
    ScopedCheckedShaderReplay mutation(driver);
    CHECK(driver->m_ReplayShaderMutationRoot);
    {
      ScopedCheckpointAnalysisBypass analysis(driver);
      CHECK_FALSE(driver->m_ReplayShaderMutationRoot);
    }
    CHECK(driver->m_ReplayShaderMutationRoot);
    driver = &dummy;
    dummy.m_ReplayShaderMutationRoot = true;
  }
  CHECK_FALSE(dummy.m_ReplayShaderMutationRoot);
}

TEST_CASE("Read-only event selection reuses current pipelines and invalidates on edits",
          "[replay][pipeline-cache]")
{
  ReplayControllerTestAccess::CheckReadOnlyPipelineReuse();
}

TEST_CASE("Explicit analysis resource reads never silently replay the selected event",
          "[replay][analysis]")
{
  ReplayControllerTestAccess::CheckAnalysisReadPolicy();
}

TEST_CASE("Single-prefix Shader comparison preserves bounded snapshots and analysis ownership",
          "[replay][analysis][shader-pair]")
{
  ReplayControllerTestAccess::CheckShaderPairAnalysis();
}

TEST_CASE("Replay checkpoints scale their budget and retain actual module boundaries",
          "[replay][checkpoints]")
{
  rdcarray<ActionDescription> storage;
  storage.resize(4);
  rdcarray<ActionDescription *> actions;
  for(uint32_t i = 0; i < storage.size(); i++)
  {
    storage[i].eventId = (i + 1) * 100;
    storage[i].flags = i < 2 ? ActionFlags::Drawcall : ActionFlags::Dispatch;
    actions.push_back(&storage[i]);
  }
  ReplayCheckpointConfig config;
  rdcarray<ReplayCheckpointCandidate> planned;
  PlanReplayCheckpointCandidates(actions, 0, 0, config, planned);
  CHECK(config.byteBudget == 64ULL * 1024 * 1024);
  CHECK(config.minEventGap == 12);
  REQUIRE(planned.size() == 3);
  CHECK(planned[1].eventId == 200);    // after the last graphics draw, before compute
  CHECK(planned[1].moduleId == 1);
  CHECK(planned[2].moduleId == 2);
  CHECK(planned[1].priority > planned[0].priority);
  CHECK(planned.back().eventId < storage.back().eventId);

  PlanReplayCheckpointCandidates(actions, 64ULL * 1024 * 1024 * 1024, 0, config, planned);
  CHECK(config.byteBudget == 512ULL * 1024 * 1024);
  CHECK(config.maxCheckpoints == 3);
  PlanReplayCheckpointCandidates(actions, ~0ULL, ~0ULL, config, planned);
  CHECK(config.byteBudget == 512ULL * 1024 * 1024);
  CHECK(config.maxCheckpoints == 3);
  storage.back().eventId = ~0U;
  PlanReplayCheckpointCandidates(actions, ~0ULL, 0, config, planned);
  CHECK(config.byteBudget == 512ULL * 1024 * 1024);
  CHECK(planned.size() <= 64);
  storage.back().eventId = 400;
  actions.clear();
  PlanReplayCheckpointCandidates(actions, 0, 0, config, planned);
  CHECK(planned.empty());
  CHECK(config.minEventGap == 1);
}
#endif
