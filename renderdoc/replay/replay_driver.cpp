/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2017-2026 Baldur Karlsson
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

#include "replay_driver.h"
#include <float.h>
#include <math.h>
#include "common/threading.h"
#include "common/timing.h"
#include "compressonator/CMP_Core.h"
#include "maths/camera.h"
#include "maths/formatpacking.h"
#include "maths/half_convert.h"
#include "maths/matrix.h"
#include "os/os_specific.h"
#include "serialise/serialiser.h"
#include "strings/string_utils.h"
#include "replay_timing.h"
#include "resource_usage.h"

Matrix4f MeshComponentHighlight::DepthBiasedTransform(const Matrix4f &mvp, const ICamera *camera) const
{
  // A line covers different samples from the underlying triangles. Nudge the selection
  // towards the camera in clip space while retaining occlusion by the rest of the mesh.
  const float nearPlane = camera ? static_cast<const Camera *>(camera)->GetNear() : 0.1f;
  const float bias = RDCMIN(0.0005f, nearPlane * 0.005f);
  Matrix4f ret = mvp;
  for(size_t i = 2; i < 16; i += 4)
    ret[i] -= mvp[i + 1] * bias;
  return ret;
}

struct ReplayTimingData
{
  struct Chunk
  {
    uint64_t ticks[5] = {};    // decode, execute, unclassified, wait, submit (last two overlap)
    uint64_t selfTicks[3] = {};
    uint32_t calls = 0;
    uint32_t decoded = 0;
    uint64_t TotalTicks() const { return ticks[0] + ticks[1] + ticks[2]; }
    void Add(const Chunk &other)
    {
      calls += other.calls;
      decoded += other.decoded;
      for(size_t i = 0; i < ARRAY_COUNT(ticks); i++)
        ticks[i] += other.ticks[i];
      for(size_t i = 0; i < ARRAY_COUNT(selfTicks); i++)
        selfTicks[i] += other.selfTicks[i];
    }
  };
  struct Section
  {
    uint64_t ticks = 0;
    uint64_t selfTicks = 0;
    uint32_t calls = 0;
    uint32_t depth = 0;
  };
  struct Wait
  {
    uint64_t ticks = 0;
    uint32_t calls = 0;
  };

  Section sections[(size_t)ReplayTimingSection::Count] = {};
  Wait waits[(size_t)ReplayTimingContext::Count] = {};
  uint64_t counters[(size_t)ReplayTimingCounter::Count] = {};
  ScopedReplayTimingSection *activeSection = NULL;
  ReplayTimingContext context = ReplayTimingContext::Unspecified;
  std::map<uint32_t, Chunk> chunks;
  rdcstr (*chunkNameLookup)(uint32_t) = NULL;

  void BeginSection(ReplayTimingSection section) { sections[(size_t)section].depth++; }
  void EndSection(ReplayTimingSection sectionID, uint64_t ticks, uint64_t childTicks,
                  ReplayTimingContext waitContext)
  {
    Section &section = sections[(size_t)sectionID];
    section.selfTicks += ticks > childTicks ? ticks - childTicks : 0;

    // Only the outermost instance contributes inclusive time and calls. Self time is disjoint even
    // when a helper re-enters the same section through another nested section.
    if(--section.depth == 0)
    {
      section.ticks += ticks;
      section.calls++;
      if(sectionID == ReplayTimingSection::ExplicitWait)
      {
        waits[(size_t)waitContext].ticks += ticks;
        waits[(size_t)waitContext].calls++;
      }
    }
  }
};

namespace
{
thread_local ScopedReplayTiming *activeReplayTiming = NULL;
thread_local ScopedReplayTimingChunk *activeReplayChunk = NULL;
const ReplayTimingSection chunkSections[] = {
    ReplayTimingSection::ChunkDecode,       ReplayTimingSection::ChunkExecute,
    ReplayTimingSection::ChunkUnclassified, ReplayTimingSection::ExplicitWait,
    ReplayTimingSection::ExplicitSubmit,
};

bool ReplayTimingEnabled()
{
  static const bool enabled = Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1";
  return enabled;
}

const char *ReplayTypeName(ReplayLogType replayType)
{
  switch(replayType)
  {
    case eReplay_Full: return "Full";
    case eReplay_WithoutDraw: return "WithoutDraw";
    case eReplay_OnlyDraw: return "OnlyDraw";
    default: break;
  }

  return "Unknown";
}

double ReplayTicksToMS(uint64_t ticks)
{
  return double(ticks) / Timing::GetTickFrequency();
}

const char *sectionNames[] = {
    "applyInitial",      "contextReplay",      "explicitWait",
    "explicitSubmit",    "initialDescriptors", "initialCPUCopy",
    "initialGPUCopy",    "initialSparseBinds", "initialAccelerationStructures",
    "initialBarriers",   "initialResidency",   "initialListClose",
    "resetAllocators",   "replaySetup",        "replayBookkeeping",
    "chunkFraming",      "chunkDecode",        "chunkExecute",
    "chunkUnclassified", "replayMetadata",
};
const char *contextNames[] = {
    "unspecified",         "replayReset",       "initialRestore",     "initialFallback",
    "contextReplay",       "replayFinish",      "queueSignal",        "dataUploadSync",
    "dataUploadPoolReuse", "mapDataWrite",      "writeToSubresource", "preparedCacheInvalidate",
    "debugSingleSubmit",   "descriptorWrite",   "descriptorCopy",     "queueTransition",
    "capturedFence",       "hostRestoreHazard",
};
const char *counterNames[] = {
    "initialResources",
    "initialDescriptorHeaps",
    "initialDescriptors",
    "initialDescriptorRanges",
    "initialDescriptorFullRestores",
    "initialDescriptorsSkipped",
    "initialBuffers",
    "initialTextures",
    "initialHeaps",
    "initialReadOnlySkipped",
    "initialAccelerationStructures",
    "initialAccelerationStructuresSkipped",
    "initialCPUCopyResources",
    "initialCPUCopyBytes",
    "initialGPUCopyResources",
    "initialGPUCopyBufferBytes",
    "initialGPUCopyFootprintBytes",
    "initialGPUCopyWholeResources",
    "initialUploadCacheHits",
    "initialUploadCacheBytes",
    "initialUploadCachePromotedBytes",
    "initialUploadCacheFallbackBytes",
    "initialSparseResources",
    "initialBarriers",
    "initialCommandLists",
    "internalListsCreated",
    "internalListsReused",
    "internalListsSubmitted",
    "dataUploadListsCreated",
    "dataUploadPoolResets",
    "queueSignalsDeferred",
    "queueSignalsConservative",
    "descriptorWriteWaits",
    "descriptorCopyWaits",
    "descriptorWritesSkipped",
    "descriptorCopiesSkipped",
    "chunksProcessed",
    "chunkBytes",
    "chunkDecodeBoundaries",
    "chunkDecodeBoundaryMissing",
    "initialClearBytes",
    "initialClearResources",
    "initialLayoutResources",
    "initialQueryResets",
    "hostWriteCacheHits",
    "hostWriteGPUBytes",
    "decodedCommandCacheHits",
    "decodedCommandCacheBytes",
    "preparedModes",
    "initialOverwrittenBytesSkipped",
    "initialProgramRelinks",
    "initialProgramRelinksSkipped",
};
static_assert(ARRAY_COUNT(sectionNames) == (size_t)ReplayTimingSection::Count,
              "Every timing section must have a stable log key");
static_assert(ARRAY_COUNT(contextNames) == (size_t)ReplayTimingContext::Count,
              "Every wait context must have a stable log key");
static_assert(ARRAY_COUNT(counterNames) == (size_t)ReplayTimingCounter::Count,
              "Every timing counter must have a stable log key");

void AppendReplayTiming(const rdcstr &line)
{
  static Threading::CriticalSection lock;
  SCOPED_LOCK(lock);

  rdcstr executable;
  FileIO::GetExecutableFilename(executable);
  const rdcstr filename = get_dirname(executable) + "/eid_timing_driver.log";

  // The normal update-mode CRT open denies concurrent readers on Windows. A live timing reader
  // could therefore make an event's records disappear (or the write-mode fallback truncate the
  // file). The platform logging API uses append-only writes with reader sharing on both platforms.
  FileIO::LogFileHandle *file = FileIO::logfile_open(filename);
  const rdcstr record = line + "\n";
  FileIO::logfile_append(file, record.c_str(), record.size());
  FileIO::logfile_close(file, {});
}
};    // namespace

void AppendReplayTimingLog(const rdcstr &line)
{
  if(ReplayTimingEnabled())
    AppendReplayTiming(line);
}

bool IsReplayTimingActive()
{
  return activeReplayTiming != NULL;
}

void AddReplayTimingCounter(ReplayTimingCounter counter, uint64_t value)
{
  if(activeReplayTiming != NULL)
    activeReplayTiming->m_Data->counters[(size_t)counter] += value;
}

void SetReplayTimingChunkNameLookup(rdcstr (*lookup)(uint32_t))
{
  if(activeReplayTiming)
    activeReplayTiming->m_Data->chunkNameLookup = lookup;
}

void NotifyReplayTimingParametersDecoded()
{
  if(activeReplayChunk)
    activeReplayChunk->ParametersDecoded();
}

ScopedReplayTiming::ScopedReplayTiming(const char *driver, uint32_t startEventID,
                                       uint32_t endEventID, ReplayLogType replayType, bool partial)
    : m_Driver(driver),
      m_StartEventID(startEventID),
      m_EndEventID(endEventID),
      m_ReplayType(replayType),
      m_Partial(partial)
{
  if(ReplayTimingEnabled())
  {
    m_Data = new ReplayTimingData;
    m_StartTick = Timing::GetTick();
    m_Previous = activeReplayTiming;
    activeReplayTiming = this;
  }
}

ScopedReplayTiming::~ScopedReplayTiming()
{
  if(m_Data == NULL)
    return;

  const uint64_t totalTicks = Timing::GetTick() - m_StartTick;
  activeReplayTiming = m_Previous;
  uint32_t replayDepth = 0;
  for(const ScopedReplayTiming *parent = m_Previous; parent; parent = parent->m_Previous)
    ++replayDepth;

  rdcstr line = StringFormat::Fmt(
      "api=%s range=%u->%u type=%s partial=%u total_ms=%.3f applyInitial_ms=%.3f "
      "contextReplay_ms=%.3f explicitWait_ms=%.3f explicitSubmit_ms=%.3f",
      m_Driver, m_StartEventID, m_EndEventID, ReplayTypeName(m_ReplayType), m_Partial ? 1U : 0U,
      ReplayTicksToMS(totalTicks),
      ReplayTicksToMS(m_Data->sections[(size_t)ReplayTimingSection::ApplyInitial].ticks),
      ReplayTicksToMS(m_Data->sections[(size_t)ReplayTimingSection::ContextReplay].ticks),
      ReplayTicksToMS(m_Data->sections[(size_t)ReplayTimingSection::ExplicitWait].ticks),
      ReplayTicksToMS(m_Data->sections[(size_t)ReplayTimingSection::ExplicitSubmit].ticks));

  line += StringFormat::Fmt(" timing_version=2 chunk_timing_version=1 replay_depth=%u", replayDepth);
  uint64_t sectionSelfTicks = 0;
  for(size_t i = 0; i < (size_t)ReplayTimingSection::Count; i++)
  {
    const ReplayTimingData::Section &section = m_Data->sections[i];
    sectionSelfTicks += section.selfTicks;
    if(section.calls)
      line += StringFormat::Fmt(" section.%s_ms=%.3f section.%s_self_ms=%.3f section.%s_calls=%u",
                                sectionNames[i], ReplayTicksToMS(section.ticks), sectionNames[i],
                                ReplayTicksToMS(section.selfTicks), sectionNames[i], section.calls);
  }
  line += StringFormat::Fmt(
      " unscoped_ms=%.3f",
      ReplayTicksToMS(totalTicks > sectionSelfTicks ? totalTicks - sectionSelfTicks : 0));
  for(size_t i = 0; i < (size_t)ReplayTimingContext::Count; i++)
  {
    const ReplayTimingData::Wait &wait = m_Data->waits[i];
    if(wait.calls)
      line += StringFormat::Fmt(" wait.%s_ms=%.3f wait.%s_calls=%u", contextNames[i],
                                ReplayTicksToMS(wait.ticks), contextNames[i], wait.calls);
  }
  for(size_t i = 0; i < (size_t)ReplayTimingCounter::Count; i++)
  {
    if(m_Data->counters[i])
      line += StringFormat::Fmt(" count.%s=%llu", counterNames[i], m_Data->counters[i]);
  }

  RDCLOG("[EIDTiming] %s", line.c_str());
  AppendReplayTimingLog(line);

  // At most 12 named types and one remainder record. Sorting/formatting and file I/O happen after
  // the measured ReplayLog, with no per-chunk string construction or log writes.
  rdcarray<uint32_t> chunkIDs;
  for(const auto &entry : m_Data->chunks)
    chunkIDs.push_back(entry.first);
  std::sort(chunkIDs.begin(), chunkIDs.end(), [this](uint32_t a, uint32_t b) {
    return m_Data->chunks[a].TotalTicks() > m_Data->chunks[b].TotalTicks();
  });
  ReplayTimingData::Chunk remainder;
  auto logChunk = [this, replayDepth](uint32_t id, const ReplayTimingData::Chunk &chunk,
                                      const char *name) {
    rdcstr record = StringFormat::Fmt(
        "api=ReplayChunk driver=%s range=%u->%u type=%s chunkId=%u chunk=%s calls=%u "
        "decodeBoundaries=%u decode_ms=%.3f execute_ms=%.3f unclassified_ms=%.3f "
        "decode_self_ms=%.3f execute_self_ms=%.3f unclassified_self_ms=%.3f "
        "wait_ms=%.3f submit_ms=%.3f total_ms=%.3f replay_depth=%u",
        m_Driver, m_StartEventID, m_EndEventID, ReplayTypeName(m_ReplayType), id, name, chunk.calls,
        chunk.decoded, ReplayTicksToMS(chunk.ticks[0]), ReplayTicksToMS(chunk.ticks[1]),
        ReplayTicksToMS(chunk.ticks[2]), ReplayTicksToMS(chunk.selfTicks[0]),
        ReplayTicksToMS(chunk.selfTicks[1]), ReplayTicksToMS(chunk.selfTicks[2]),
        ReplayTicksToMS(chunk.ticks[3]), ReplayTicksToMS(chunk.ticks[4]),
        ReplayTicksToMS(chunk.TotalTicks()), replayDepth);
    RDCLOG("[EIDTiming] %s", record.c_str());
    AppendReplayTimingLog(record);
  };
  for(size_t i = 0; i < chunkIDs.size(); i++)
  {
    const uint32_t id = chunkIDs[i];
    if(i < 12)
    {
      const rdcstr name = m_Data->chunkNameLookup ? m_Data->chunkNameLookup(id) : "unknown";
      logChunk(id, m_Data->chunks[id], name.c_str());
    }
    else
      remainder.Add(m_Data->chunks[id]);
  }
  if(remainder.calls)
    logChunk(~0U, remainder, "other");
  delete m_Data;
}

ScopedReplayTimingSection::ScopedReplayTimingSection(ReplayTimingSection section, bool start)
    : m_Section(section)
{
  if(start)
    Begin();
}

void ScopedReplayTimingSection::Begin()
{
  RDCASSERT(m_Replay == NULL);
  m_Replay = activeReplayTiming;
  if(m_Replay != NULL)
  {
    ReplayTimingData &data = *m_Replay->m_Data;
    m_Context = data.context;
    m_Parent = data.activeSection;
    m_ChildTicks = 0;
    data.activeSection = this;
    data.BeginSection(m_Section);
    m_StartTick = Timing::GetTick();
  }
}

ScopedReplayTimingSection::~ScopedReplayTimingSection()
{
  End();
}

void ScopedReplayTimingSection::End()
{
  if(m_Replay == NULL)
    return;

  const uint64_t ticks = Timing::GetTick() - m_StartTick;
  ReplayTimingData &data = *m_Replay->m_Data;
  data.activeSection = m_Parent;
  if(m_Parent != NULL)
    m_Parent->m_ChildTicks += ticks;
  data.EndSection(m_Section, ticks, m_ChildTicks, m_Context);
  m_Replay = NULL;
}

void ScopedReplayTimingSection::Reclassify(ReplayTimingSection section)
{
  if(m_Replay == NULL || section == m_Section)
    return;
  ReplayTimingData &data = *m_Replay->m_Data;
  // Only chunk scopes use this operation, before ending their parameter-read interval.
  RDCASSERT(data.sections[(size_t)m_Section].depth > 0);
  data.sections[(size_t)m_Section].depth--;
  data.sections[(size_t)section].depth++;
  m_Section = section;
}

ScopedReplayTimingChunk::ScopedReplayTimingChunk(uint32_t chunkID)
    : m_ChunkID(chunkID),
      m_Replay(activeReplayTiming),
      m_Decode(ReplayTimingSection::ChunkUnclassified, false),
      m_Execute(ReplayTimingSection::ChunkExecute, false)
{
  if(m_Replay == NULL)
    return;
  m_Previous = activeReplayChunk;
  activeReplayChunk = this;
  for(size_t i = 0; i < ARRAY_COUNT(chunkSections); i++)
    m_SectionTicks[i] = m_Replay->m_Data->sections[(size_t)chunkSections[i]].ticks;
  for(size_t i = 0; i < ARRAY_COUNT(m_SectionSelfTicks); i++)
    m_SectionSelfTicks[i] = m_Replay->m_Data->sections[(size_t)chunkSections[i]].selfTicks;
  m_Decode.Begin();
}

void ScopedReplayTimingChunk::ParametersDecoded()
{
  // Nested helpers may check again. Only the first checked boundary divides this chunk, and a
  // callback in another nested ReplayLog cannot reclassify its parent's outstanding chunk.
  if(m_Replay == NULL || m_Replay != activeReplayTiming || m_Decoded)
    return;
  // A serialisation helper may have its own outstanding timed section. Do not pop that helper's
  // stack from a callback; leave such unusual routes unclassified instead of corrupting nesting.
  if(m_Replay->m_Data->activeSection != &m_Decode)
    return;
  m_Decoded = true;
  m_Decode.Reclassify(ReplayTimingSection::ChunkDecode);
  m_Decode.End();
  m_Execute.Begin();
}

ScopedReplayTimingChunk::~ScopedReplayTimingChunk()
{
  if(m_Replay == NULL)
    return;
  m_Execute.End();
  m_Decode.End();
  activeReplayChunk = m_Previous;
  ReplayTimingData::Chunk &chunk = m_Replay->m_Data->chunks[m_ChunkID];
  chunk.calls++;
  chunk.decoded += m_Decoded ? 1U : 0U;
  for(size_t i = 0; i < ARRAY_COUNT(chunkSections); i++)
    chunk.ticks[i] += m_Replay->m_Data->sections[(size_t)chunkSections[i]].ticks - m_SectionTicks[i];
  for(size_t i = 0; i < ARRAY_COUNT(m_SectionSelfTicks); i++)
    chunk.selfTicks[i] +=
        m_Replay->m_Data->sections[(size_t)chunkSections[i]].selfTicks - m_SectionSelfTicks[i];
  AddReplayTimingCounter(ReplayTimingCounter::ChunksProcessed);
  AddReplayTimingCounter(m_Decoded ? ReplayTimingCounter::ChunkDecodeBoundaries
                                   : ReplayTimingCounter::ChunkDecodeBoundaryMissing);
}

ScopedReplayTimingContext::ScopedReplayTimingContext(ReplayTimingContext context)
    : m_Replay(activeReplayTiming)
{
  if(m_Replay != NULL)
  {
    m_Previous = m_Replay->m_Data->context;
    m_Replay->m_Data->context = context;
  }
}

ScopedReplayTimingContext::~ScopedReplayTimingContext()
{
  if(m_Replay != NULL)
    m_Replay->m_Data->context = m_Previous;
}

template <>
rdcstr DoStringise(const RemapTexture &el)
{
  BEGIN_ENUM_STRINGISE(RemapTexture);
  {
    STRINGISE_ENUM_CLASS(NoRemap)
    STRINGISE_ENUM_CLASS(RGBA8)
    STRINGISE_ENUM_CLASS(RGBA16)
    STRINGISE_ENUM_CLASS(RGBA32)
  }
  END_ENUM_STRINGISE();
}

template <typename SerialiserType>
void DoSerialise(SerialiserType &ser, GetTextureDataParams &el)
{
  SERIALISE_MEMBER(forDiskSave);
  SERIALISE_MEMBER(standardLayout);
  SERIALISE_MEMBER(typeCast);
  SERIALISE_MEMBER(resolve);
  SERIALISE_MEMBER(remap);
  SERIALISE_MEMBER(blackPoint);
  SERIALISE_MEMBER(whitePoint);
}

INSTANTIATE_SERIALISE_TYPE(GetTextureDataParams);

template <typename SerialiserType>
void DoSerialise(SerialiserType &ser, ReplayBufferPatch &el)
{
  SERIALISE_MEMBER(buffer);
  SERIALISE_MEMBER(byteOffset);
  SERIALISE_MEMBER(data);
}

INSTANTIATE_SERIALISE_TYPE(ReplayBufferPatch);

rdcstr ValidateReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches)
{
  if(patches.size() > 512)
    return "Persistent buffer edits exceed the 512-range limit";
  uint64_t bytes = 0;
  const ReplayBufferPatch *previous = NULL;
  for(const ReplayBufferPatch &patch : patches)
  {
    if(patch.buffer == ResourceId() || patch.data.empty() ||
       patch.byteOffset > UINT64_MAX - patch.data.size())
      return "Persistent buffer edit has an invalid resource or byte range";
    if(patch.data.size() > 32ULL * 1024 * 1024 - bytes)
      return "Persistent buffer edits exceed the 32 MiB payload limit";
    bytes += patch.data.size();
    if(previous && (patch.buffer < previous->buffer ||
                    (patch.buffer == previous->buffer &&
                     patch.byteOffset < previous->byteOffset + previous->data.size())))
      return "Persistent buffer edit ranges must be sorted and non-overlapping";
    previous = &patch;
  }
  return {};
}

bool SameReplayBufferPatchLayout(const rdcarray<ReplayBufferPatch> &previous,
                                 const rdcarray<ReplayBufferPatch> &next)
{
  if(next.empty() || previous.size() != next.size() ||
     !ValidateReplayBufferPatches(previous).empty() || !ValidateReplayBufferPatches(next).empty())
    return false;

  for(size_t i = 0; i < next.size(); i++)
    if(previous[i].buffer != next[i].buffer || previous[i].byteOffset != next[i].byteOffset ||
       previous[i].data.size() != next[i].data.size())
      return false;
  return true;
}

rdcstr GetReplayBufferPatchChanges(const rdcarray<ReplayBufferPatch> &previous,
                                   const rdcarray<ReplayBufferPatch> &next,
                                   rdcarray<ReplayBufferPatchRange> &changes, uint32_t maxRanges)
{
  changes.clear();
  rdcstr error = ValidateReplayBufferPatches(previous);
  if(error.empty())
    error = ValidateReplayBufferPatches(next);
  if(!error.empty())
    return error;

  auto append = [&changes, maxRanges](ResourceId buffer, uint64_t begin, uint64_t end) {
    // All ends originate from validated patch ranges, so subtraction and adjacency cannot overflow.
    if(!changes.empty() && changes.back().buffer == buffer &&
       changes.back().byteOffset + changes.back().byteLength == begin)
    {
      changes.back().byteLength = end - changes.back().byteOffset;
      return true;
    }
    if(changes.size() >= maxRanges)
      return false;
    changes.push_back({buffer, begin, end - begin});
    return true;
  };

  size_t oldIndex = 0, newIndex = 0;
  size_t oldConsumed = 0, newConsumed = 0;
  bool complete = true;
  while(complete && (oldIndex < previous.size() || newIndex < next.size()))
  {
    const ReplayBufferPatch *old = oldIndex < previous.size() ? &previous[oldIndex] : NULL;
    const ReplayBufferPatch *updated = newIndex < next.size() ? &next[newIndex] : NULL;

    if(!updated || (old && old->buffer < updated->buffer))
    {
      complete =
          append(old->buffer, old->byteOffset + oldConsumed, old->byteOffset + old->data.size());
      oldConsumed = old->data.size();
    }
    else if(!old || updated->buffer < old->buffer)
    {
      complete = append(updated->buffer, updated->byteOffset + newConsumed,
                        updated->byteOffset + updated->data.size());
      newConsumed = updated->data.size();
    }
    else
    {
      const uint64_t oldBegin = old->byteOffset + oldConsumed;
      const uint64_t newBegin = updated->byteOffset + newConsumed;
      const uint64_t oldEnd = old->byteOffset + old->data.size();
      const uint64_t newEnd = updated->byteOffset + updated->data.size();

      if(oldBegin < newBegin)
      {
        const uint64_t end = RDCMIN(oldEnd, newBegin);
        complete = append(old->buffer, oldBegin, end);
        oldConsumed += size_t(end - oldBegin);
      }
      else if(newBegin < oldBegin)
      {
        const uint64_t end = RDCMIN(newEnd, oldBegin);
        complete = append(updated->buffer, newBegin, end);
        newConsumed += size_t(end - newBegin);
      }
      else
      {
        const size_t length = size_t(RDCMIN(oldEnd, newEnd) - oldBegin);
        const byte *oldBytes = old->data.data() + oldConsumed;
        const byte *newBytes = updated->data.data() + newConsumed;

        // Slider updates normally leave almost every byte unchanged. Avoid a per-byte walk when
        // an entire overlapping span is equal, without interpreting floating-point representations.
        if(memcmp(oldBytes, newBytes, length) != 0)
        {
          size_t pos = 0;
          while(complete && pos < length)
          {
            while(pos < length && oldBytes[pos] == newBytes[pos])
              pos++;
            const size_t begin = pos;
            while(pos < length && oldBytes[pos] != newBytes[pos])
              pos++;
            if(begin < pos)
              complete = append(old->buffer, oldBegin + begin, oldBegin + pos);
          }
        }
        oldConsumed += length;
        newConsumed += length;
      }
    }

    if(old && oldConsumed == old->data.size())
    {
      oldIndex++;
      oldConsumed = 0;
    }
    if(updated && newConsumed == updated->data.size())
    {
      newIndex++;
      newConsumed = 0;
    }
  }

  if(!complete)
  {
    changes.clear();
    return "Persistent buffer edit differences exceed the change-range limit";
  }
  return {};
}

void ApplyReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches, ResourceId buffer,
                              uint64_t byteOffset, void *data, uint64_t byteLength)
{
  if(!data || byteLength == 0 || byteOffset > UINT64_MAX - byteLength)
    return;
  for(const ReplayBufferPatch &patch : patches)
  {
    if(patch.buffer != buffer)
      continue;
    const uint64_t begin = RDCMAX(byteOffset, patch.byteOffset);
    const uint64_t end = RDCMIN(byteOffset + byteLength, patch.byteOffset + patch.data.size());
    if(begin < end)
      memcpy((byte *)data + size_t(begin - byteOffset),
             patch.data.data() + size_t(begin - patch.byteOffset), size_t(end - begin));
  }
}

rdcstr ReplayBufferPatchSet::Set(ResourceId buffer, uint64_t byteOffset, const bytebuf &data)
{
  ReplayBufferPatch patch;
  patch.buffer = buffer;
  patch.byteOffset = byteOffset;
  patch.data = data;
  rdcstr error = ValidateReplayBufferPatches({patch});
  if(!error.empty())
    return error;

  uint64_t bytes = data.size();
  size_t count = 1;
  for(const Layer &layer : m_Layers)
  {
    if(layer.buffer == buffer && layer.byteOffset == byteOffset && layer.byteLength == data.size())
      continue;
    for(const ReplayBufferPatch &range : layer.ranges)
      bytes += range.data.size();
    count++;
  }
  if(count > 256 || bytes > 32ULL * 1024 * 1024)
    return "Persistent buffer edits exceed the edit-count or payload budget";

  Remove(buffer, byteOffset, data.size());
  Layer layer;
  layer.buffer = buffer;
  layer.byteOffset = byteOffset;
  layer.byteLength = data.size();
  layer.ranges.push_back(patch);
  m_Layers.push_back(layer);
  return {};
}

bool ReplayBufferPatchSet::Remove(ResourceId buffer, uint64_t byteOffset, uint64_t byteLength)
{
  for(size_t i = 0; i < m_Layers.size(); i++)
  {
    const Layer &layer = m_Layers[i];
    if(layer.buffer == buffer && layer.byteOffset == byteOffset && layer.byteLength == byteLength)
    {
      m_Layers.erase(i);
      return true;
    }
  }
  return false;
}

bool ReplayBufferPatchSet::ResetRange(ResourceId buffer, uint64_t byteOffset, uint64_t byteLength)
{
  if(buffer == ResourceId() || byteLength == 0 || byteOffset > UINT64_MAX - byteLength)
    return false;

  const uint64_t resetEnd = byteOffset + byteLength;
  bool changed = false;
  for(size_t i = 0; i < m_Layers.size();)
  {
    Layer &layer = m_Layers[i];
    if(layer.buffer == buffer)
    {
      rdcarray<ReplayBufferPatch> kept;
      for(const ReplayBufferPatch &range : layer.ranges)
      {
        const uint64_t rangeEnd = range.byteOffset + range.data.size();
        if(rangeEnd <= byteOffset || range.byteOffset >= resetEnd)
        {
          kept.push_back(range);
          continue;
        }
        changed = true;
        if(range.byteOffset < byteOffset)
        {
          ReplayBufferPatch prefix = range;
          prefix.data.resize(size_t(byteOffset - range.byteOffset));
          kept.push_back(prefix);
        }
        if(rangeEnd > resetEnd)
        {
          ReplayBufferPatch suffix;
          suffix.buffer = buffer;
          suffix.byteOffset = resetEnd;
          suffix.data.resize(size_t(rangeEnd - resetEnd));
          memcpy(suffix.data.data(), range.data.data() + size_t(resetEnd - range.byteOffset),
                 suffix.data.size());
          kept.push_back(suffix);
        }
      }
      layer.ranges.swap(kept);
    }
    if(layer.ranges.empty())
      m_Layers.erase(i);
    else
      i++;
  }
  return changed;
}

rdcarray<ReplayBufferPatch> ReplayBufferPatchSet::Flatten() const
{
  rdcarray<ReplayBufferPatch> result;
  for(const Layer &edit : m_Layers)
  {
    for(const ReplayBufferPatch &layer : edit.ranges)
    {
      rdcarray<ReplayBufferPatch> next;
      const uint64_t layerEnd = layer.byteOffset + layer.data.size();
      for(const ReplayBufferPatch &old : result)
      {
        const uint64_t oldEnd = old.byteOffset + old.data.size();
        if(old.buffer != layer.buffer || oldEnd <= layer.byteOffset || old.byteOffset >= layerEnd)
        {
          next.push_back(old);
          continue;
        }
        if(old.byteOffset < layer.byteOffset)
        {
          ReplayBufferPatch prefix = old;
          prefix.data.resize(size_t(layer.byteOffset - old.byteOffset));
          next.push_back(prefix);
        }
        if(oldEnd > layerEnd)
        {
          ReplayBufferPatch suffix;
          suffix.buffer = old.buffer;
          suffix.byteOffset = layerEnd;
          suffix.data.resize(size_t(oldEnd - layerEnd));
          memcpy(suffix.data.data(), old.data.data() + size_t(layerEnd - old.byteOffset),
                 suffix.data.size());
          next.push_back(suffix);
        }
      }
      next.push_back(layer);
      std::sort(next.begin(), next.end(), [](const ReplayBufferPatch &a, const ReplayBufferPatch &b) {
        return a.buffer < b.buffer || (a.buffer == b.buffer && a.byteOffset < b.byteOffset);
      });
      result.swap(next);
    }
  }
  return result;
}

static bool PreviousNextExcludedMarker(ActionDescription *action)
{
  return bool(action->flags & (ActionFlags::PushMarker | ActionFlags::PopMarker |
                               ActionFlags::SetMarker | ActionFlags::MultiAction));
}

CompType BaseRemapType(RemapTexture remap, CompType typeCast)
{
  switch(typeCast)
  {
    case CompType::Float:
    case CompType::SNorm:
    case CompType::UNorm: return CompType::Float;
    case CompType::UNormSRGB:
      return remap == RemapTexture::RGBA8 ? CompType::UNormSRGB : CompType::Float;
    case CompType::UInt: return CompType::UInt;
    case CompType::SInt: return CompType::SInt;
    default: return typeCast;
  }
}

static ActionDescription *SetupActionPointers(rdcarray<ActionDescription *> &actionTable,
                                              rdcarray<ActionDescription> &actions,
                                              ActionDescription *parent, ActionDescription *&previous)
{
  ActionDescription *ret = NULL;

  for(size_t i = 0; i < actions.size(); i++)
  {
    ActionDescription *action = &actions[i];

    RDCASSERTMSG("All actions must have their own event as the final event",
                 !action->events.empty() && action->events.back().eventId == action->eventId);

    action->parent = parent;

    if(!action->children.empty())
    {
      {
        RDCASSERT(actionTable.empty() || action->eventId > actionTable.back()->eventId ||
                  actionTable.back()->IsFakeMarker());
        actionTable.resize(RDCMAX(actionTable.size(), size_t(action->eventId) + 1));
        actionTable[action->eventId] = action;
      }

      ret = SetupActionPointers(actionTable, action->children, action, previous);
    }
    else if(PreviousNextExcludedMarker(action))
    {
      // don't want to set up previous/next links for markers, but still add them to the table
      // Some markers like Present should have previous/next, but API Calls we also skip

      {
        // we also allow non-contiguous EIDs for fake markers that have high EIDs
        RDCASSERT(actionTable.empty() || action->eventId > actionTable.back()->eventId ||
                  actionTable.back()->IsFakeMarker());
        actionTable.resize(RDCMAX(actionTable.size(), size_t(action->eventId) + 1));
        actionTable[action->eventId] = action;
      }
    }
    else
    {
      if(previous)
        previous->nextAction = action;
      action->previousAction = previous;

      {
        // we also allow non-contiguous EIDs for fake markers that have high EIDs
        RDCASSERT(actionTable.empty() || action->eventId > actionTable.back()->eventId ||
                  actionTable.back()->IsFakeMarker());
        actionTable.resize(RDCMAX(actionTable.size(), size_t(action->eventId) + 1));
        actionTable[action->eventId] = action;
      }

      ret = previous = action;
    }
  }

  return ret;
}

void SetupActionPointers(rdcarray<ActionDescription *> &actionTable,
                         rdcarray<ActionDescription> &actions)
{
  ActionDescription *previous = NULL;
  SetupActionPointers(actionTable, actions, NULL, previous);

  // markers don't enter the previous/next chain, but we still want pointers for them that point to
  // the next or previous actual action (skipping any markers). This means that
  // action->next->previous
  // != action sometimes, but it's more useful than action->next being NULL in the middle of the
  // list.
  // This enables searching for a marker string and then being able to navigate from there and
  // joining the 'real' linked list after one step.

  previous = NULL;
  rdcarray<ActionDescription *> markers;

  for(ActionDescription *action : actionTable)
  {
    if(!action)
      continue;

    bool marker = PreviousNextExcludedMarker(action);

    if(marker)
    {
      // point the previous pointer to the last non-marker action we got. If we haven't hit one yet
      // because this is near the start, this will just be NULL.
      action->previousAction = previous;

      // because there can be multiple markers consecutively we want to point all of their nexts to
      // the next action we encounter. Accumulate this list, though in most cases it will only be 1
      // long as it's uncommon to have multiple markers one after the other
      markers.push_back(action);
    }
    else
    {
      // the next markers we encounter should point their previous to this.
      previous = action;

      // all previous markers point to this one
      for(ActionDescription *m : markers)
        m->nextAction = action;

      markers.clear();
    }
  }
}

void PatchLineStripIndexBuffer(const ActionDescription *action, Topology topology, uint8_t *idx8,
                               uint16_t *idx16, uint32_t *idx32, rdcarray<uint32_t> &patchedIndices)
{
  const uint32_t restart = 0xffffffff;

#define IDX_VALUE(offs)                                                                        \
  (idx16 ? idx16[index + (offs)]                                                               \
         : (idx32 ? idx32[index + (offs)] : (idx8 ? idx8[index + (offs)] : index + (offs)))) + \
      action->baseVertex

  switch(topology)
  {
    case Topology::TriangleList:
    {
      for(uint32_t index = 0; index + 3 <= action->numIndices; index += 3)
      {
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(IDX_VALUE(1));
        patchedIndices.push_back(IDX_VALUE(2));
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(restart);
      }
      break;
    }
    case Topology::TriangleStrip:
    {
      // we decompose into individual triangles. This will mean the shared lines will be overwritten
      // twice but it's a simple algorithm and otherwise decomposing a tristrip into a line strip
      // would need some more complex handling (you could two pairs of triangles in a single strip
      // by changing the winding, but then you'd need to restart and jump back, and handle a
      // trailing single triangle, etc).
      for(uint32_t index = 0; index + 3 <= action->numIndices; index++)
      {
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(IDX_VALUE(1));
        patchedIndices.push_back(IDX_VALUE(2));
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(restart);
      }
      break;
    }
    case Topology::TriangleFan:
    {
      uint32_t index = 0;
      uint32_t base = IDX_VALUE(0);
      index++;

      // this would be easier to do as a line list and just do base -> 1, 1 -> 2 lines for each
      // triangle then a base -> 2 for the last one. However I would be amazed if this code ever
      // runs except in an artificial test, so let's go with the simple and easy to understand
      // solution.
      for(; index + 2 <= action->numIndices; index++)
      {
        patchedIndices.push_back(base);
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(IDX_VALUE(1));
        patchedIndices.push_back(base);
        patchedIndices.push_back(restart);
      }
      break;
    }
    case Topology::TriangleList_Adj:
    {
      // skip the adjacency values
      for(uint32_t index = 0; index + 6 <= action->numIndices; index += 6)
      {
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(IDX_VALUE(2));
        patchedIndices.push_back(IDX_VALUE(4));
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(restart);
      }
      break;
    }
    case Topology::TriangleStrip_Adj:
    {
      // skip the adjacency values
      for(uint32_t index = 0; index + 6 <= action->numIndices; index += 2)
      {
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(IDX_VALUE(2));
        patchedIndices.push_back(IDX_VALUE(4));
        patchedIndices.push_back(IDX_VALUE(0));
        patchedIndices.push_back(restart);
      }
      break;
    }
    default:
      RDCERR("Unsupported topology %s for line-list patching", ToStr(topology).c_str());
      return;
  }

#undef IDX_VALUE
}

void PatchTriangleFanRestartIndexBufer(rdcarray<uint32_t> &patchedIndices, uint32_t restartIndex)
{
  if(patchedIndices.empty())
    return;

  rdcarray<uint32_t> newIndices;

  uint32_t firstIndex = patchedIndices[0];

  size_t i = 1;
  // while we have at least two indices left
  while(i + 1 < patchedIndices.size())
  {
    uint32_t a = patchedIndices[i];
    uint32_t b = patchedIndices[i + 1];

    if(a != restartIndex && b != restartIndex)
    {
      // no restart, add primitive
      newIndices.push_back(firstIndex);
      newIndices.push_back(a);
      newIndices.push_back(b);

      i++;
      continue;
    }
    else if(b == restartIndex)
    {
      // we've already added the last triangle before the restart in the previous iteration, just
      // continue so we hit the a == restartIndex case below
      i++;
    }
    else if(a == restartIndex)
    {
      // new first index is b
      firstIndex = b;
      // skip both the restartIndex value and the first index, and begin at the next real index (if
      // it exists)
      i += 2;

      uint32_t next[2] = {b, b};

      // if this is the last vertex, the triangle will be degenerate
      if(i < patchedIndices.size())
        next[0] = patchedIndices[i];
      if(i + 1 < patchedIndices.size())
        next[1] = patchedIndices[i + 1];

      // output 3 dummy degenerate triangles so vertex ID mapping is easy
      // we rotate the triangles so the important vertex is last in each.
      for(size_t dummy = 0; dummy < 3; dummy++)
      {
        newIndices.push_back(restartIndex);
        newIndices.push_back(restartIndex);
        newIndices.push_back(restartIndex);
      }
    }
  }

  newIndices.swap(patchedIndices);
}

void StandardFillCBufferVariable(ResourceId shader, const ShaderConstantType &desc,
                                 uint32_t dataOffset, const bytebuf &data, ShaderVariable &outvar,
                                 uint32_t matStride)
{
  const VarType type = outvar.type;
  const uint32_t rows = outvar.rows;
  const uint32_t cols = outvar.columns;

  size_t elemByteSize = 4;
  if(type == VarType::Double || type == VarType::ULong || type == VarType::SLong ||
     type == VarType::GPUPointer)
    elemByteSize = 8;
  else if(type == VarType::Half || type == VarType::UShort || type == VarType::SShort)
    elemByteSize = 2;
  else if(type == VarType::UByte || type == VarType::SByte)
    elemByteSize = 1;

  // primary is the 'major' direction
  // so a matrix is a secondaryDim number of primaryDim-sized vectors
  uint32_t primaryDim = cols;
  uint32_t secondaryDim = rows;
  if(rows > 1 && outvar.ColMajor())
  {
    primaryDim = rows;
    secondaryDim = cols;
  }

  if(dataOffset < data.size())
  {
    const byte *srcData = data.data() + dataOffset;
    const size_t avail = data.size() - dataOffset;

    byte *dstData = outvar.value.u8v.data();

    // each secondaryDim element (row or column) is stored in a primaryDim-vector.
    // We copy each vector member individually to account for smaller than uint32 sized types.
    for(uint32_t s = 0; s < secondaryDim; s++)
    {
      for(uint32_t p = 0; p < primaryDim; p++)
      {
        const size_t srcOffset = matStride * s + p * elemByteSize;
        const size_t dstOffset = (primaryDim * s + p) * elemByteSize;

        if(srcOffset + elemByteSize <= avail)
          memcpy(dstData + dstOffset, srcData + srcOffset, elemByteSize);
      }
    }

    // if it's a matrix and not row major, transpose
    if(primaryDim > 1 && secondaryDim > 1 && outvar.ColMajor())
    {
      ShaderVariable tmp = outvar;

      if(elemByteSize == 8)
      {
        for(size_t ri = 0; ri < rows; ri++)
          for(size_t ci = 0; ci < cols; ci++)
            outvar.value.u64v[ri * cols + ci] = tmp.value.u64v[ci * rows + ri];
      }
      else if(elemByteSize == 4)
      {
        for(size_t ri = 0; ri < rows; ri++)
          for(size_t ci = 0; ci < cols; ci++)
            outvar.value.u32v[ri * cols + ci] = tmp.value.u32v[ci * rows + ri];
      }
      else if(elemByteSize == 2)
      {
        for(size_t ri = 0; ri < rows; ri++)
          for(size_t ci = 0; ci < cols; ci++)
            outvar.value.u16v[ri * cols + ci] = tmp.value.u16v[ci * rows + ri];
      }
      else if(elemByteSize == 1)
      {
        for(size_t ri = 0; ri < rows; ri++)
          for(size_t ci = 0; ci < cols; ci++)
            outvar.value.u8v[ri * cols + ci] = tmp.value.u8v[ci * rows + ri];
      }
    }
  }
  else
  {
    outvar.flags |= ShaderVariableFlags::Truncated;
  }

  if(desc.pointerTypeID != ~0U)
    outvar.SetTypedPointer(outvar.value.u64v[0], shader, desc.pointerTypeID);
}

static void StandardFillCBufferVariables(ResourceId shader, const rdcarray<ShaderConstant> &invars,
                                         rdcarray<ShaderVariable> &outvars, const bytebuf &data,
                                         uint32_t baseOffset)
{
  for(size_t v = 0; v < invars.size(); v++)
  {
    rdcstr basename = invars[v].name;

    uint8_t rows = invars[v].type.rows;
    uint8_t cols = invars[v].type.columns;
    uint32_t elems = RDCMAX(1U, invars[v].type.elements);
    const bool rowMajor = invars[v].type.RowMajor();
    const bool isArray = elems > 1;

    const uint32_t matStride = invars[v].type.matrixByteStride;

    uint32_t dataOffset = baseOffset + invars[v].byteOffset;

    if(!invars[v].type.members.empty() || (rows == 0 && cols == 0))
    {
      ShaderVariable var;
      var.name = basename;
      var.rows = var.columns = 0;
      var.type = VarType::Struct;
      if(rowMajor)
        var.flags |= ShaderVariableFlags::RowMajorMatrix;

      rdcarray<ShaderVariable> varmembers;

      if(isArray)
      {
        var.members.resize(elems);
        for(uint32_t i = 0; i < elems; i++)
        {
          ShaderVariable &vr = var.members[i];
          vr.name = StringFormat::Fmt("%s[%u]", basename.c_str(), i);
          vr.rows = vr.columns = 0;
          vr.type = VarType::Struct;
          if(rowMajor)
            vr.flags |= ShaderVariableFlags::RowMajorMatrix;

          StandardFillCBufferVariables(shader, invars[v].type.members, vr.members, data, dataOffset);

          dataOffset += invars[v].type.arrayByteStride;
        }
      }
      else
      {
        var.type = VarType::Struct;

        StandardFillCBufferVariables(shader, invars[v].type.members, var.members, data, dataOffset);
      }

      outvars.push_back(var);

      continue;
    }

    size_t outIdx = outvars.size();
    outvars.push_back({});

    {
      const VarType type = invars[v].type.baseType;

      outvars[outIdx].name = basename;
      outvars[outIdx].rows = 1;
      outvars[outIdx].type = type;
      outvars[outIdx].columns = cols;
      if(rowMajor)
        outvars[outIdx].flags |= ShaderVariableFlags::RowMajorMatrix;

      ShaderVariable &var = outvars[outIdx];

      if(!isArray)
      {
        outvars[outIdx].rows = rows;

        StandardFillCBufferVariable(shader, invars[v].type, dataOffset, data, outvars[outIdx],
                                    matStride);
      }
      else
      {
        var.name = outvars[outIdx].name;
        var.rows = 0;
        var.columns = 0;

        rdcarray<ShaderVariable> varmembers;
        varmembers.resize(elems);

        rdcstr base = outvars[outIdx].name;

        for(uint32_t e = 0; e < elems; e++)
        {
          varmembers[e].name = StringFormat::Fmt("%s[%u]", base.c_str(), e);
          varmembers[e].rows = rows;
          varmembers[e].type = type;
          varmembers[e].columns = cols;
          if(rowMajor)
            varmembers[e].flags |= ShaderVariableFlags::RowMajorMatrix;

          uint32_t rowDataOffset = dataOffset;

          dataOffset += invars[v].type.arrayByteStride;

          StandardFillCBufferVariable(shader, invars[v].type, rowDataOffset, data, varmembers[e],
                                      matStride);
        }

        var.members = varmembers;
      }
    }
  }
}

void PreprocessLineDirectives(rdcarray<ShaderSourceFile> &sourceFiles)
{
  struct SplitFile
  {
    rdcstr filename;
    rdcarray<rdcstr> lines;
    bool modified = false;
  };

  rdcarray<SplitFile> splitFiles;

  splitFiles.resize(sourceFiles.size());

  for(size_t i = 0; i < sourceFiles.size(); i++)
    splitFiles[i].filename = sourceFiles[i].filename;

  for(size_t i = 0; i < sourceFiles.size(); i++)
  {
    rdcarray<rdcstr> srclines;

    // start off writing to the corresponding output file.
    SplitFile *dstFile = &splitFiles[i];
    bool changedFile = false;

    size_t dstLine = 0;

    // skip this file if it doesn't contain #line
    if(!sourceFiles[i].contents.contains("#line"))
      continue;

    split(sourceFiles[i].contents, srclines, '\n');
    srclines.push_back("");

    // handle #line directives by inserting empty lines or erasing as necessary
    bool seenLine = false;

    for(size_t srcLine = 0; srcLine < srclines.size(); srcLine++)
    {
      if(srclines[srcLine].empty())
      {
        dstLine++;
        continue;
      }

      char *c = &srclines[srcLine][0];
      char *end = c + srclines[srcLine].size();

      while(*c == '\t' || *c == ' ' || *c == '\r')
        c++;

      if(c == end)
      {
        // blank line, just advance line counter
        dstLine++;
        continue;
      }

      if(c + 5 > end || strncmp(c, "#line", 5) != 0)
      {
        // only consider the destination file 'modfied' (and so needing a merge later) if we've seen
        // a #line statement. Otherwise we're just doing an identity copy so far. Without this, this
        // can lead to problems e.g. if there are a few statements in a file before the #line we
        // then create a truncated list of lines with only those and then start spitting the #line
        // directives into other files. We still want to have the original file as-is so don't
        // consider it modified. However if a #line directive targets the file it's already in we
        // want to be sure that we have copied any existing implicit preamble to that file so it
        // doesn't get lost when we re-merge
        if(seenLine)
          dstFile->modified = true;

        {
          // resize up to account for the current line, if necessary
          dstFile->lines.resize(RDCMAX(dstLine + 1, dstFile->lines.size()));

          // if non-empty, append this line (to allow multiple lines on the same line
          // number to be concatenated). To avoid screwing up line numbers we have to append with
          // a comment and not a newline.

          if(dstFile->lines[dstLine].empty())
            dstFile->lines[dstLine] = srclines[srcLine];
          else
            dstFile->lines[dstLine] += " /* multiple #lines overlapping */ " + srclines[srcLine];
        }

        // advance line counter
        dstLine++;

        continue;
      }

      seenLine = true;

      // we have a #line directive
      c += 5;

      if(c >= end)
      {
        // invalid #line, just advance line counter
        dstLine++;
        continue;
      }

      while(*c == '\t' || *c == ' ')
        c++;

      if(c >= end)
      {
        // invalid #line, just advance line counter
        dstLine++;
        continue;
      }

      // invalid #line, no line number. Skip/ignore and just advance line counter
      if(*c < '0' || *c > '9')
      {
        dstLine++;
        continue;
      }

      size_t newLineNum = 0;
      while(*c >= '0' && *c <= '9')
      {
        newLineNum *= 10;
        newLineNum += int((*c) - '0');
        c++;
      }

      // convert to 0-indexed line number
      if(newLineNum > 0)
        newLineNum--;

      while(*c == '\t' || *c == ' ')
        c++;

      if(*c == '"')
      {
        // filename
        c++;

        char *filename = c;

        // parse out filename
        while(*c != '"' && *c != 0)
        {
          if(*c == '\\')
          {
            // skip escaped characters
            c += 2;
          }
          else
          {
            c++;
          }
        }

        // parsed filename successfully
        if(*c == '"')
        {
          *c = 0;

          rdcstr fname = filename;
          if(fname.empty())
            fname = "unnamed_shader";

          // find the new destination file
          bool found = false;
          size_t dstFileIdx = 0;

          for(size_t f = 0; f < splitFiles.size(); f++)
          {
            if(splitFiles[f].filename == fname)
            {
              found = true;
              dstFileIdx = f;
              break;
            }
          }

          if(found)
          {
            changedFile = (dstFile != &splitFiles[dstFileIdx]);
            dstFile = &splitFiles[dstFileIdx];
          }
          else
          {
            RDCWARN("Couldn't find filename '%s' in #line directive in debug info", fname.c_str());

            // make a dummy file to write into that won't be used.
            splitFiles.push_back(SplitFile());
            splitFiles.back().filename = fname;
            splitFiles.back().modified = true;

            changedFile = true;
            dstFile = &splitFiles.back();
          }

          // set the next line number, and continue processing
          dstLine = newLineNum;

          continue;
        }
        else
        {
          // invalid #line, ignore
          RDCERR("Couldn't parse #line directive: '%s'", srclines[srcLine].c_str());
          continue;
        }
      }
      else
      {
        // No filename. Set the next line number, and continue processing
        dstLine = newLineNum;
        continue;
      }
    }
  }

  for(size_t i = 0; i < sourceFiles.size(); i++)
  {
    if(sourceFiles[i].contents.empty() || splitFiles[i].modified)
    {
      merge(splitFiles[i].lines, sourceFiles[i].contents, '\n');
    }
  }
}

void StandardFillCBufferVariables(ResourceId shader, const rdcarray<ShaderConstant> &invars,
                                  rdcarray<ShaderVariable> &outvars, const bytebuf &data)
{
  // start with offset 0
  StandardFillCBufferVariables(shader, invars, outvars, data, 0);
}

uint64_t CalcMeshOutputSize(uint64_t curSize, uint64_t requiredOutput)
{
  if(curSize == 0)
    curSize = 32 * 1024 * 1024;

  // resize exponentially up to 256MB to avoid repeated resizes
  while(curSize < requiredOutput && curSize < 0x10000000ULL)
    curSize *= 2;

  // after that, just align the required size up to 16MB and allocate that. Otherwise we can
  // vastly-overallocate at large sizes.
  if(curSize < requiredOutput)
    curSize = AlignUp(requiredOutput, (uint64_t)0x1000000ULL);

  return curSize;
}

FloatVector HighlightCache::InterpretVertex(const byte *data, uint32_t vert, const MeshDisplay &cfg,
                                            const byte *end, bool useidx, bool &valid)
{
  FloatVector ret(0.0f, 0.0f, 0.0f, 1.0f);

  if(cfg.position.format.compType == CompType::UInt ||
     cfg.position.format.compType == CompType::SInt || cfg.position.format.compCount == 4)
    ret.w = 0.0f;

  if(useidx && idxData)
  {
    if(vert >= (uint32_t)indices.size())
    {
      valid = false;
      return ret;
    }

    vert = indices[vert];

    if(cfg.position.topology != Topology::TriangleFan && cfg.position.allowRestart)
    {
      if((cfg.position.indexByteStride == 1 && vert == 0xff) ||
         (cfg.position.indexByteStride == 2 && vert == 0xffff) ||
         (cfg.position.indexByteStride == 4 && vert == 0xffffffff))
      {
        valid = false;
        return ret;
      }
    }
  }

  return HighlightCache::InterpretVertex(data, vert, cfg.position.vertexByteStride,
                                         cfg.position.format, end, valid);
}

FloatVector HighlightCache::InterpretVertex(const byte *data, uint32_t vert,
                                            uint32_t vertexByteStride, const ResourceFormat &fmt,
                                            const byte *end, bool &valid)
{
  data += vert * vertexByteStride;

  if(data + fmt.ElementSize() > end)
  {
    valid = false;

    if(fmt.compType == CompType::UInt || fmt.compType == CompType::SInt || fmt.compCount == 4)
      return FloatVector(0.0f, 0.0f, 0.0f, 0.0f);

    return FloatVector(0.0f, 0.0f, 0.0f, 1.0f);
  }

  return DecodeFormattedComponents(fmt, data);
}

uint64_t inthash(uint64_t val, uint64_t seed)
{
  return (seed << 5) + seed + val; /* hash * 33 + c */
}

uint64_t inthash(ResourceId id, uint64_t seed)
{
  uint64_t val = 0;
  memcpy(&val, &id, sizeof(val));
  return (seed << 5) + seed + val; /* hash * 33 + c */
}

void HighlightCache::CacheHighlightingData(uint32_t eventId, const MeshDisplay &cfg)
{
  rdcstr ident;

  uint64_t newKey = 5381;

  // hash all the properties of cfg that we use
  newKey = inthash(eventId, newKey);
  newKey = inthash(cfg.position.indexByteStride, newKey);
  newKey = inthash(cfg.position.numIndices, newKey);
  newKey = inthash((uint64_t)cfg.type, newKey);
  newKey = inthash((uint64_t)cfg.position.baseVertex, newKey);
  newKey = inthash((uint64_t)cfg.position.topology, newKey);
  newKey = inthash(cfg.position.vertexByteOffset, newKey);
  newKey = inthash(cfg.position.vertexByteStride, newKey);
  newKey = inthash(cfg.position.indexResourceId, newKey);
  newKey = inthash(cfg.position.vertexResourceId, newKey);
  newKey = inthash((uint64_t)cfg.position.allowRestart, newKey);
  newKey = inthash((uint64_t)cfg.position.restartIndex, newKey);

  if(cacheKey != newKey)
  {
    cacheKey = newKey;

    uint32_t bytesize = cfg.position.indexByteStride;
    uint64_t maxIndex = cfg.position.numIndices - 1;

    if(cfg.position.indexByteStride == 0 || cfg.type == MeshDataStage::GSOut)
    {
      indices.clear();
      idxData = false;
    }
    else
    {
      idxData = true;

      bytebuf idxdata;
      if(cfg.position.indexResourceId != ResourceId())
        driver->GetBufferData(cfg.position.indexResourceId, cfg.position.indexByteOffset,
                              cfg.position.numIndices * bytesize, idxdata);

      uint8_t *idx8 = (uint8_t *)&idxdata[0];
      uint16_t *idx16 = (uint16_t *)&idxdata[0];
      uint32_t *idx32 = (uint32_t *)&idxdata[0];

      uint32_t numIndices = RDCMIN(cfg.position.numIndices, uint32_t(idxdata.size() / bytesize));

      indices.resize(numIndices);

      if(bytesize == 1)
      {
        for(uint32_t i = 0; i < numIndices; i++)
        {
          indices[i] = uint32_t(idx8[i]);
          maxIndex = RDCMAX(maxIndex, (uint64_t)indices[i]);
        }
      }
      else if(bytesize == 2)
      {
        for(uint32_t i = 0; i < numIndices; i++)
        {
          indices[i] = uint32_t(idx16[i]);
          maxIndex = RDCMAX(maxIndex, (uint64_t)indices[i]);
        }
      }
      else if(bytesize == 4)
      {
        for(uint32_t i = 0; i < numIndices; i++)
        {
          indices[i] = idx32[i];
          maxIndex = RDCMAX(maxIndex, (uint64_t)indices[i]);
        }
      }

      uint32_t sub = uint32_t(-cfg.position.baseVertex);
      uint32_t add = uint32_t(cfg.position.baseVertex);

      if(cfg.position.baseVertex > 0)
        maxIndex += add;

      uint32_t primRestart = 0;
      if(cfg.position.allowRestart)
      {
        if(cfg.position.indexByteStride == 1)
          primRestart = 0xff;
        else if(cfg.position.indexByteStride == 2)
          primRestart = 0xffff;
        else
          primRestart = 0xffffffff;
      }

      for(uint32_t i = 0; cfg.position.baseVertex != 0 && i < numIndices; i++)
      {
        // don't modify primitive restart indices
        if(primRestart && indices[i] == primRestart)
          continue;

        if(cfg.position.baseVertex < 0)
        {
          if(indices[i] < sub)
            indices[i] = 0;
          else
            indices[i] -= sub;
        }
        else
        {
          indices[i] += add;
        }
      }
    }

    driver->GetBufferData(cfg.position.vertexResourceId, cfg.position.vertexByteOffset,
                          (maxIndex + 1) * cfg.position.vertexByteStride, vertexData);

    // if it's a fan AND it uses primitive restart, decompose it into a triangle list because the
    // restart changes the central vertex
    if(cfg.position.topology == Topology::TriangleFan && cfg.position.allowRestart)
    {
      PatchTriangleFanRestartIndexBufer(indices, cfg.position.restartIndex);
    }
  }
}

bool HighlightCache::FetchHighlightPositions(const MeshDisplay &cfg, FloatVector &activeVertex,
                                             rdcarray<FloatVector> &activePrim,
                                             rdcarray<FloatVector> &adjacentPrimVertices,
                                             rdcarray<FloatVector> &inactiveVertices)
{
  bool valid = true;

  byte *data = &vertexData[0];
  byte *dataEnd = data + vertexData.size();

  uint32_t idx = cfg.highlightVert;
  Topology meshtopo = cfg.position.topology;

  // if it's a fan AND it uses primitive restart, it was decomposed into a triangle list
  if(meshtopo == Topology::TriangleFan && cfg.position.allowRestart)
  {
    meshtopo = Topology::TriangleList;

    // due to triangle fan expansion the index we need to look up is adjusted
    if(idx > 2)
      idx = (idx - 1) * 3 - 1;
  }

  activeVertex = InterpretVertex(data, idx, cfg, dataEnd, true, valid);

  uint32_t primRestart = 0;
  if(cfg.position.allowRestart)
  {
    if(cfg.position.indexByteStride == 1)
      primRestart = 0xff;
    else if(cfg.position.indexByteStride == 2)
      primRestart = 0xffff;
    else
      primRestart = 0xffffffff;
  }

  // Reference for how primitive topologies are laid out:
  // http://msdn.microsoft.com/en-us/library/windows/desktop/bb205124(v=vs.85).aspx
  // Section 19.1 of the Vulkan 1.0.48 spec
  // Section 10.1 of the OpenGL 4.5 spec
  if(meshtopo == Topology::LineList)
  {
    uint32_t v = uint32_t(idx / 2) * 2;    // find first vert in primitive

    activePrim.push_back(InterpretVertex(data, v + 0, cfg, dataEnd, true, valid));
    activePrim.push_back(InterpretVertex(data, v + 1, cfg, dataEnd, true, valid));
  }
  else if(meshtopo == Topology::TriangleList)
  {
    uint32_t v = uint32_t(idx / 3) * 3;    // find first vert in primitive

    activePrim.push_back(InterpretVertex(data, v + 0, cfg, dataEnd, true, valid));
    activePrim.push_back(InterpretVertex(data, v + 1, cfg, dataEnd, true, valid));
    activePrim.push_back(InterpretVertex(data, v + 2, cfg, dataEnd, true, valid));
  }
  else if(meshtopo == Topology::LineList_Adj)
  {
    uint32_t v = uint32_t(idx / 4) * 4;    // find first vert in primitive

    FloatVector vs[] = {
        InterpretVertex(data, v + 0, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 1, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 2, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 3, cfg, dataEnd, true, valid),
    };

    adjacentPrimVertices.push_back(vs[0]);
    adjacentPrimVertices.push_back(vs[1]);

    adjacentPrimVertices.push_back(vs[2]);
    adjacentPrimVertices.push_back(vs[3]);

    activePrim.push_back(vs[1]);
    activePrim.push_back(vs[2]);
  }
  else if(meshtopo == Topology::TriangleList_Adj)
  {
    uint32_t v = uint32_t(idx / 6) * 6;    // find first vert in primitive

    FloatVector vs[] = {
        InterpretVertex(data, v + 0, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 1, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 2, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 3, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 4, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 5, cfg, dataEnd, true, valid),
    };

    adjacentPrimVertices.push_back(vs[0]);
    adjacentPrimVertices.push_back(vs[1]);
    adjacentPrimVertices.push_back(vs[2]);

    adjacentPrimVertices.push_back(vs[2]);
    adjacentPrimVertices.push_back(vs[3]);
    adjacentPrimVertices.push_back(vs[4]);

    adjacentPrimVertices.push_back(vs[4]);
    adjacentPrimVertices.push_back(vs[5]);
    adjacentPrimVertices.push_back(vs[0]);

    activePrim.push_back(vs[0]);
    activePrim.push_back(vs[2]);
    activePrim.push_back(vs[4]);
  }
  else if(meshtopo == Topology::LineStrip)
  {
    // find first vert in primitive. In strips a vert isn't
    // in only one primitive, so we pick the first primitive
    // it's in. This means the first N points are in the first
    // primitive, and thereafter each point is in the next primitive
    uint32_t v = RDCMAX(idx, 1U) - 1;

    // skip past any primitive restart indices
    if(idxData && primRestart)
    {
      while(v < (uint32_t)indices.size() && indices[v] == primRestart)
        v++;
    }

    activePrim.push_back(InterpretVertex(data, v + 0, cfg, dataEnd, true, valid));
    activePrim.push_back(InterpretVertex(data, v + 1, cfg, dataEnd, true, valid));
  }
  else if(meshtopo == Topology::TriangleFan)
  {
    // find first vert in primitive. In fans a vert isn't
    // in only one primitive, so we pick the first primitive
    // it's in. This means the first N points are in the first
    // primitive, and thereafter each point is in the next primitive
    uint32_t v = RDCMAX(idx, 2U) - 1;

    // first vert in the whole fan
    activePrim.push_back(InterpretVertex(data, 0, cfg, dataEnd, true, valid));
    activePrim.push_back(InterpretVertex(data, v + 0, cfg, dataEnd, true, valid));
    activePrim.push_back(InterpretVertex(data, v + 1, cfg, dataEnd, true, valid));
  }
  else if(meshtopo == Topology::TriangleStrip)
  {
    // find first vert in primitive. In strips a vert isn't
    // in only one primitive, so we pick the first primitive
    // it's in. This means the first N points are in the first
    // primitive, and thereafter each point is in the next primitive
    uint32_t v = RDCMAX(idx, 2U) - 2;

    // skip past any primitive restart indices
    if(idxData && primRestart)
    {
      while(v < (uint32_t)indices.size() &&
            (indices[v + 0] == primRestart || indices[v + 1] == primRestart))
        v++;
    }

    activePrim.push_back(InterpretVertex(data, v + 0, cfg, dataEnd, true, valid));
    activePrim.push_back(InterpretVertex(data, v + 1, cfg, dataEnd, true, valid));
    activePrim.push_back(InterpretVertex(data, v + 2, cfg, dataEnd, true, valid));
  }
  else if(meshtopo == Topology::LineStrip_Adj)
  {
    // find first vert in primitive. In strips a vert isn't
    // in only one primitive, so we pick the first primitive
    // it's in. This means the first N points are in the first
    // primitive, and thereafter each point is in the next primitive
    uint32_t v = RDCMAX(idx, 3U) - 3;

    // skip past any primitive restart indices
    if(idxData && primRestart)
    {
      while(v < (uint32_t)indices.size() &&
            (indices[v + 0] == primRestart || indices[v + 1] == primRestart ||
             indices[v + 2] == primRestart))
        v++;
    }

    FloatVector vs[] = {
        InterpretVertex(data, v + 0, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 1, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 2, cfg, dataEnd, true, valid),
        InterpretVertex(data, v + 3, cfg, dataEnd, true, valid),
    };

    adjacentPrimVertices.push_back(vs[0]);
    adjacentPrimVertices.push_back(vs[1]);

    adjacentPrimVertices.push_back(vs[2]);
    adjacentPrimVertices.push_back(vs[3]);

    activePrim.push_back(vs[1]);
    activePrim.push_back(vs[2]);
  }
  else if(meshtopo == Topology::TriangleStrip_Adj)
  {
    // Triangle strip with adjacency is the most complex topology, as
    // we need to handle the ends separately where the pattern breaks.

    uint32_t numidx = cfg.position.numIndices;

    if(numidx < 6)
    {
      // not enough indices provided, bail to make sure logic below doesn't
      // need to have tons of edge case detection
      valid = false;
    }
    else if(idx <= 4 || numidx <= 7)
    {
      FloatVector vs[] = {
          InterpretVertex(data, 0, cfg, dataEnd, true, valid),
          InterpretVertex(data, 1, cfg, dataEnd, true, valid),
          InterpretVertex(data, 2, cfg, dataEnd, true, valid),
          InterpretVertex(data, 3, cfg, dataEnd, true, valid),
          InterpretVertex(data, 4, cfg, dataEnd, true, valid),

          // note this one isn't used as it's adjacency for the next triangle
          InterpretVertex(data, 5, cfg, dataEnd, true, valid),

          // min() with number of indices in case this is a tiny strip
          // that is basically just a list
          InterpretVertex(data, RDCMIN(6U, numidx - 1), cfg, dataEnd, true, valid),
      };

      // these are the triangles on the far left of the MSDN diagram above
      adjacentPrimVertices.push_back(vs[0]);
      adjacentPrimVertices.push_back(vs[1]);
      adjacentPrimVertices.push_back(vs[2]);

      adjacentPrimVertices.push_back(vs[4]);
      adjacentPrimVertices.push_back(vs[3]);
      adjacentPrimVertices.push_back(vs[0]);

      adjacentPrimVertices.push_back(vs[4]);
      adjacentPrimVertices.push_back(vs[2]);
      adjacentPrimVertices.push_back(vs[6]);

      activePrim.push_back(vs[0]);
      activePrim.push_back(vs[2]);
      activePrim.push_back(vs[4]);
    }
    else if(idx > numidx - 4)
    {
      // in diagram, numidx == 14

      FloatVector vs[] = {
          /*[0]=*/InterpretVertex(data, numidx - 8, cfg, dataEnd, true, valid),    // 6 in diagram

          // as above, unused since this is adjacency for 2-previous triangle
          /*[1]=*/InterpretVertex(data, numidx - 7, cfg, dataEnd, true, valid),    // 7 in diagram
          /*[2]=*/InterpretVertex(data, numidx - 6, cfg, dataEnd, true, valid),    // 8 in diagram

          // as above, unused since this is adjacency for previous triangle
          /*[3]=*/InterpretVertex(data, numidx - 5, cfg, dataEnd, true, valid),    // 9 in diagram

          /*[4]=*/    // 10 in diagram
          InterpretVertex(data, numidx - 4, cfg, dataEnd, true, valid),

          /*[5]=*/    // 11 in diagram
          InterpretVertex(data, numidx - 3, cfg, dataEnd, true, valid),

          /*[6]=*/    // 12 in diagram
          InterpretVertex(data, numidx - 2, cfg, dataEnd, true, valid),

          /*[7]=*/    // 13 in diagram
          InterpretVertex(data, numidx - 1, cfg, dataEnd, true, valid),
      };

      // these are the triangles on the far right of the MSDN diagram above
      adjacentPrimVertices.push_back(vs[2]);    // 8 in diagram
      adjacentPrimVertices.push_back(vs[0]);    // 6 in diagram
      adjacentPrimVertices.push_back(vs[4]);    // 10 in diagram

      adjacentPrimVertices.push_back(vs[4]);    // 10 in diagram
      adjacentPrimVertices.push_back(vs[7]);    // 13 in diagram
      adjacentPrimVertices.push_back(vs[6]);    // 12 in diagram

      adjacentPrimVertices.push_back(vs[6]);    // 12 in diagram
      adjacentPrimVertices.push_back(vs[5]);    // 11 in diagram
      adjacentPrimVertices.push_back(vs[2]);    // 8 in diagram

      activePrim.push_back(vs[2]);    // 8 in diagram
      activePrim.push_back(vs[4]);    // 10 in diagram
      activePrim.push_back(vs[6]);    // 12 in diagram
    }
    else
    {
      // we're in the middle somewhere. Each primitive has two vertices for it
      // so our step rate is 2. The first 'middle' primitive starts at indices 5&6
      // and uses indices all the way back to 0
      uint32_t v = RDCMAX(((idx + 1) / 2) * 2, 6U) - 6;

      // skip past any primitive restart indices
      if(idxData && primRestart)
      {
        while(v < (uint32_t)indices.size() &&
              (indices[v + 0] == primRestart || indices[v + 1] == primRestart ||
               indices[v + 2] == primRestart || indices[v + 3] == primRestart ||
               indices[v + 4] == primRestart || indices[v + 5] == primRestart))
          v++;
      }

      // these correspond to the indices in the MSDN diagram, with {2,4,6} as the
      // main triangle
      FloatVector vs[] = {
          InterpretVertex(data, v + 0, cfg, dataEnd, true, valid),

          // this one is adjacency for 2-previous triangle
          InterpretVertex(data, v + 1, cfg, dataEnd, true, valid),
          InterpretVertex(data, v + 2, cfg, dataEnd, true, valid),

          // this one is adjacency for previous triangle
          InterpretVertex(data, v + 3, cfg, dataEnd, true, valid),
          InterpretVertex(data, v + 4, cfg, dataEnd, true, valid),
          InterpretVertex(data, v + 5, cfg, dataEnd, true, valid),
          InterpretVertex(data, v + 6, cfg, dataEnd, true, valid),
          InterpretVertex(data, v + 7, cfg, dataEnd, true, valid),
          InterpretVertex(data, v + 8, cfg, dataEnd, true, valid),
      };

      // these are the triangles around {2,4,6} in the MSDN diagram above
      adjacentPrimVertices.push_back(vs[0]);
      adjacentPrimVertices.push_back(vs[2]);
      adjacentPrimVertices.push_back(vs[4]);

      adjacentPrimVertices.push_back(vs[2]);
      adjacentPrimVertices.push_back(vs[5]);
      adjacentPrimVertices.push_back(vs[6]);

      adjacentPrimVertices.push_back(vs[6]);
      adjacentPrimVertices.push_back(vs[8]);
      adjacentPrimVertices.push_back(vs[4]);

      activePrim.push_back(vs[2]);
      activePrim.push_back(vs[4]);
      activePrim.push_back(vs[6]);
    }
  }
  else if(meshtopo >= Topology::PatchList)
  {
    uint32_t dim = PatchList_Count(meshtopo);

    uint32_t v0 = uint32_t(idx / dim) * dim;

    for(uint32_t v = v0; v < v0 + dim; v++)
    {
      if(v != idx && valid)
        inactiveVertices.push_back(InterpretVertex(data, v, cfg, dataEnd, true, valid));
    }
  }
  else    // if(meshtopo == Topology::PointList) point list, or unknown/unhandled type
  {
    // no adjacency, inactive verts or active primitive
  }

  return valid;
}

// colour ramp from http://www.ncl.ucar.edu/Document/Graphics/ColorTables/GMT_wysiwyg.shtml
const Vec4f colorRamp[22] = {
    Vec4f(0.000000f, 0.000000f, 0.000000f, 0.0f), Vec4f(0.250980f, 0.000000f, 0.250980f, 1.0f),
    Vec4f(0.250980f, 0.000000f, 0.752941f, 1.0f), Vec4f(0.000000f, 0.250980f, 1.000000f, 1.0f),
    Vec4f(0.000000f, 0.501961f, 1.000000f, 1.0f), Vec4f(0.000000f, 0.627451f, 1.000000f, 1.0f),
    Vec4f(0.250980f, 0.752941f, 1.000000f, 1.0f), Vec4f(0.250980f, 0.878431f, 1.000000f, 1.0f),
    Vec4f(0.250980f, 1.000000f, 1.000000f, 1.0f), Vec4f(0.250980f, 1.000000f, 0.752941f, 1.0f),
    Vec4f(0.250980f, 1.000000f, 0.250980f, 1.0f), Vec4f(0.501961f, 1.000000f, 0.250980f, 1.0f),
    Vec4f(0.752941f, 1.000000f, 0.250980f, 1.0f), Vec4f(1.000000f, 1.000000f, 0.250980f, 1.0f),
    Vec4f(1.000000f, 0.878431f, 0.250980f, 1.0f), Vec4f(1.000000f, 0.627451f, 0.250980f, 1.0f),
    Vec4f(1.000000f, 0.376471f, 0.250980f, 1.0f), Vec4f(1.000000f, 0.125490f, 0.250980f, 1.0f),
    Vec4f(1.000000f, 0.376471f, 0.752941f, 1.0f), Vec4f(1.000000f, 0.627451f, 1.000000f, 1.0f),
    Vec4f(1.000000f, 0.878431f, 1.000000f, 1.0f), Vec4f(1.000000f, 1.000000f, 1.000000f, 1.0f),
};

// unique colors generated from https://mokole.com/palette.html
const uint32_t uniqueColors[48] = {
    0xff00008b, 0xff32cd32, 0xff8fbc8f, 0xff8b008b, 0xffb03060, 0xffd2b48c, 0xff9932cc, 0xffff0000,
    0xffff8c00, 0xffffd700, 0xff00ff00, 0xff00ff7f, 0xff4169e1, 0xffe9967a, 0xffdc143c, 0xff00ffff,
    0xff00bfff, 0xff0000ff, 0xffa020f0, 0xffadff2f, 0xffff6347, 0xffda70d6, 0xffff00ff, 0xfff0e68c,
    0xffffff54, 0xff6495ed, 0xffdda0dd, 0xff90ee90, 0xff87ceeb, 0xffff1493, 0xff7fffd4, 0xffff69b4,
    0xff808080, 0xffc0c0c0, 0xff2f4f4f, 0xff556b2f, 0xff8b4513, 0xff6b8e23, 0xff2e8b57, 0xff8b0000,
    0xff483d8b, 0xff008000, 0xffb8860b, 0xff008b8b, 0xff4682b4, 0xffd2691e, 0xff9acd32, 0xffcd5c5c,

};

bytebuf GetDiscardPattern(DiscardType type, const ResourceFormat &fmt, uint32_t rowPitch, bool invert)
{
  static const rdcliteral patterns[] = {
      // DiscardType::RenderPassLoad
      "..#.....##...##..##....##....##..#..#.####..###..##..###..####.."
      "..#....#..#.#..#.#.#...#.#..#..#.##.#..#...#....#..#.#..#.#....."
      "..#....#..#.#..#.#..#..#..#.#..#.##.#..#...#....#..#.#..#.###..."
      "..#....#..#.####.#..#..#..#.#..#.#.##..#...#....####.###..#....."
      "..#....#..#.#..#.#.#...#.#..#..#.#.##..#...#....#..#.#..#.#....."
      "..####..##..#..#.##....##....##..#..#..#....###.#..#.#..#.####.."
      "................................................................"
      "................................................................"_lit,

      // DiscardType::RenderPassStore
      "...###.####..##..###...##....##..#..#.####..###..##..###..####.."
      "..#.....#...#..#.#..#..#.#..#..#.##.#..#...#....#..#.#..#.#....."
      "...#....#...#..#.#..#..#..#.#..#.##.#..#...#....#..#.#..#.###..."
      "....#...#...#..#.###...#..#.#..#.#.##..#...#....####.###..#....."
      ".....#..#...#..#.#..#..#.#..#..#.#.##..#...#....#..#.#..#.#....."
      "..###...#....##..#..#..##....##..#..#..#....###.#..#.#..#.####.."
      "................................................................"
      "................................................................"_lit,

      // DiscardType::UndefinedTransition
      "..#..#.#..#.##...####.####.####.#..#.####.##....####.#..#..###.."
      "..#..#.##.#.#.#..#....#.....#...##.#.#....#.#....#...####.#....."
      "..#..#.##.#.#..#.###..###...#...##.#.###..#..#...#...##.#.#....."
      "..#..#.#.##.#..#.#....#.....#...#.##.#....#..#...#...#..#.#.##.."
      "..#..#.#.##.#.#..#....#.....#...#.##.#....#.#....#...#..#.#..#.."
      "...##..#..#.##...####.#....####.#..#.####.##....####.#..#..##..."
      "................................................................"
      "................................................................"_lit,

      // DiscardType::DiscardCall
      "..##...####..###..###...#...###..##...####.##...####.#..#..###.."
      "..#.#...#...#....#.....#.#..#..#.#.#..#....#.#...#...####.#....."
      "..#..#..#....#...#.....#.#..#..#.#..#.###..#..#..#...##.#.#....."
      "..#..#..#.....#..#....#####.###..#..#.#....#..#..#...#..#.#.##.."
      "..#.#...#......#.#....#...#.#..#.#.#..#....#.#...#...#..#.#..#.."
      "..##...####.###...###.#...#.#..#.##...####.##...####.#..#..##..."
      "................................................................"
      "................................................................"_lit,
      // DiscardType::InvalidateCall
      "...####.#..#.#...#...#...#....####.##.....#...#####.####.##....."
      "....#...##.#.#...#..#.#..#.....#...#.#...#.#....#...#....#.#...."
      "....#...##.#.#...#..#.#..#.....#...#..#..#.#....#...###..#..#..."
      "....#...#.##..#.#..#####.#.....#...#..#.#####...#...#....#..#..."
      "....#...#.##..#.#..#...#.#.....#...#.#..#...#...#...#....#.#...."
      "...####.#..#...#...#...#.####.####.##...#...#...#...####.##....."
      "................................................................"
      "................................................................"_lit,
  };

  const rdcliteral &pattern = patterns[(int)type];

  RDCASSERT(pattern.length() == DiscardPatternWidth * DiscardPatternHeight);

  bytebuf ret;

  if(fmt.type == ResourceFormatType::Regular || fmt.type == ResourceFormatType::A8 ||
     fmt.type == ResourceFormatType::S8)
  {
    byte black[8] = {};
    byte white[8] = {};

    if(fmt.compType == CompType::Float)
    {
      if(fmt.compByteWidth == 8)
      {
        double b = 0.0;
        double w = 1000.0;
        memcpy(black, &b, sizeof(b));
        memcpy(white, &w, sizeof(w));
      }
      else if(fmt.compByteWidth == 4)
      {
        float b = 0.0f;
        float w = 1000.0f;
        memcpy(black, &b, sizeof(b));
        memcpy(white, &w, sizeof(w));
      }
      else
      {
        uint16_t b = ConvertToHalf(0.0f);
        uint16_t w = ConvertToHalf(1000.0f);
        memcpy(black, &b, sizeof(b));
        memcpy(white, &w, sizeof(w));
      }
    }
    else if(fmt.compType == CompType::Depth)
    {
      if(fmt.compByteWidth == 4)
      {
        float b = 0.0f;
        float w = 1.0f;
        memcpy(black, &b, sizeof(b));
        memcpy(white, &w, sizeof(w));
      }
      else
      {
        // other depth formats are normalised
        memset(black, 0, sizeof(black));
        memset(white, 0xff, sizeof(white));
      }
    }
    else if(fmt.compType == CompType::UInt || fmt.compType == CompType::SInt)
    {
      // ints we use 0 and 127 so it's the same for every signed type and byte width
      white[0] = 127;
    }
    else
    {
      // all other types are normalised, so we just set white to 0xff
      memset(black, 0, sizeof(black));
      memset(white, 0xff, sizeof(white));
    }

    uint32_t tightPitch = DiscardPatternWidth * fmt.compByteWidth * fmt.compCount;
    rowPitch = RDCMAX(rowPitch, tightPitch);

    ret.resize(rowPitch * DiscardPatternHeight);
    byte *out = ret.data();

    for(int yi = 0; yi < (int)DiscardPatternHeight; yi++)
    {
      int y = invert ? DiscardPatternHeight - 1 - yi : yi;
      for(int x = 0; x < (int)DiscardPatternWidth; x++)
      {
        char c = pattern.c_str()[y * DiscardPatternWidth + x];
        for(uint8_t i = 0; i < fmt.compCount; i++)
        {
          if(c == '#')
            memcpy(out, white, fmt.compByteWidth);
          else
            memcpy(out, black, fmt.compByteWidth);
          out += fmt.compByteWidth;
        }
      }
      out += (rowPitch - tightPitch);
    }
  }
  else if(fmt.type == ResourceFormatType::R10G10B10A2)
  {
    uint32_t tightPitch = DiscardPatternWidth * sizeof(uint32_t);
    rowPitch = RDCMAX(rowPitch, tightPitch);

    ret.resize(rowPitch * DiscardPatternHeight);
    uint32_t *out = (uint32_t *)ret.data();

    uint32_t minVal = 0;
    uint32_t maxVal = 0xffffffff;

    if(fmt.compType == CompType::UInt)
      maxVal = (127u << 0) | (127u << 10) | (127u << 20) | (3u << 30);

    for(int yi = 0; yi < (int)DiscardPatternHeight; yi++)
    {
      int y = invert ? DiscardPatternHeight - 1 - yi : yi;
      for(int x = 0; x < (int)DiscardPatternWidth; x++)
      {
        char c = pattern.c_str()[y * DiscardPatternWidth + x];
        *(out++) = (c == '#') ? maxVal : minVal;
      }
      out += (rowPitch - tightPitch);
    }
  }
  else if(fmt.type == ResourceFormatType::R5G6B5 || fmt.type == ResourceFormatType::R5G5B5A1 ||
          fmt.type == ResourceFormatType::R4G4B4A4)
  {
    uint32_t tightPitch = DiscardPatternWidth * sizeof(uint16_t);
    rowPitch = RDCMAX(rowPitch, tightPitch);

    ret.resize(rowPitch * DiscardPatternHeight);
    uint16_t *out = (uint16_t *)ret.data();

    for(int yi = 0; yi < (int)DiscardPatternHeight; yi++)
    {
      int y = invert ? DiscardPatternHeight - 1 - yi : yi;
      for(int x = 0; x < (int)DiscardPatternWidth; x++)
      {
        char c = pattern.c_str()[y * DiscardPatternWidth + x];
        *(out++) = (c == '#') ? 0xffff : 0x0000;
      }
      out += (rowPitch - tightPitch);
    }
  }
  else if(fmt.type == ResourceFormatType::R4G4)
  {
    uint32_t tightPitch = DiscardPatternWidth * sizeof(uint8_t);
    rowPitch = RDCMAX(rowPitch, tightPitch);

    ret.resize(rowPitch * DiscardPatternHeight);
    byte *out = ret.data();

    for(int yi = 0; yi < (int)DiscardPatternHeight; yi++)
    {
      int y = invert ? DiscardPatternHeight - 1 - yi : yi;
      for(int x = 0; x < (int)DiscardPatternWidth; x++)
      {
        char c = pattern.c_str()[y * DiscardPatternWidth + x];
        *(out++) = (c == '#') ? 0xff : 0x00;
      }
      out += (rowPitch - tightPitch);
    }
  }
  else if(fmt.type == ResourceFormatType::R11G11B10)
  {
    const uint32_t black = ConvertToR11G11B10(Vec3f(0.0f, 0.0f, 0.0f));
    const uint32_t white = ConvertToR11G11B10(Vec3f(1000.0f, 1000.0f, 1000.0f));

    uint32_t tightPitch = DiscardPatternWidth * sizeof(uint32_t);
    rowPitch = RDCMAX(rowPitch, tightPitch);

    ret.resize(rowPitch * DiscardPatternHeight);
    uint32_t *out = (uint32_t *)ret.data();

    for(int yi = 0; yi < (int)DiscardPatternHeight; yi++)
    {
      int y = invert ? DiscardPatternHeight - 1 - yi : yi;
      for(int x = 0; x < (int)DiscardPatternWidth; x++)
      {
        char c = pattern.c_str()[y * DiscardPatternWidth + x];
        *(out++) = (c == '#') ? white : black;
      }
      out += (rowPitch - tightPitch);
    }
  }
  else if(fmt.type == ResourceFormatType::R9G9B9E5)
  {
    const uint32_t black = ConvertToR9G9B9E5(Vec3f(0.0f, 0.0f, 0.0f));
    const uint32_t white = ConvertToR9G9B9E5(Vec3f(1000.0f, 1000.0f, 1000.0f));

    uint32_t tightPitch = DiscardPatternWidth * sizeof(uint32_t);
    rowPitch = RDCMAX(rowPitch, tightPitch);

    ret.resize(rowPitch * DiscardPatternHeight);
    uint32_t *out = (uint32_t *)ret.data();

    for(int yi = 0; yi < (int)DiscardPatternHeight; yi++)
    {
      int y = invert ? DiscardPatternHeight - 1 - yi : yi;
      for(int x = 0; x < (int)DiscardPatternWidth; x++)
      {
        char c = pattern.c_str()[y * DiscardPatternWidth + x];
        *(out++) = (c == '#') ? white : black;
      }
      out += (rowPitch - tightPitch);
    }
  }
  else if(fmt.type == ResourceFormatType::D16S8 || fmt.type == ResourceFormatType::D24S8 ||
          fmt.type == ResourceFormatType::D32S8)
  {
    uint32_t white = 0xffffffff;
    uint32_t black = 0;

    uint32_t depthStride = 0;

    if(fmt.type == ResourceFormatType::D16S8)
    {
      depthStride = 2;
    }
    else if(fmt.type == ResourceFormatType::D24S8)
    {
      depthStride = 4;
    }
    else if(fmt.type == ResourceFormatType::D32S8)
    {
      depthStride = 4;
      float maxDepth = 1.0f;
      memcpy(&white, &maxDepth, sizeof(float));
    }

    uint32_t tightPitch = DiscardPatternWidth * depthStride;
    uint32_t tightStencilPitch = DiscardPatternWidth * sizeof(byte);
    rowPitch = RDCMAX(rowPitch, RDCMAX(tightPitch, tightStencilPitch));

    ret.resize(rowPitch * DiscardPatternHeight * 2);
    byte *depthOut = ret.data();
    byte *stencilOut = depthOut + rowPitch * DiscardPatternHeight;

    for(int yi = 0; yi < (int)DiscardPatternHeight; yi++)
    {
      int y = invert ? DiscardPatternHeight - 1 - yi : yi;
      for(int x = 0; x < (int)DiscardPatternWidth; x++)
      {
        char c = pattern.c_str()[y * DiscardPatternWidth + x];
        if(c == '#')
        {
          memcpy(depthOut, &white, depthStride);
          *(stencilOut++) = 0xff;
        }
        else
        {
          memcpy(depthOut, &black, depthStride);
          *(stencilOut++) = 0x00;
        }

        depthOut += depthStride;
      }

      depthOut += (rowPitch - tightPitch);
      stencilOut += (rowPitch - tightStencilPitch);
    }
  }
  else if(fmt.type == ResourceFormatType::BC1 || fmt.type == ResourceFormatType::BC2 ||
          fmt.type == ResourceFormatType::BC3 || fmt.type == ResourceFormatType::BC4 ||
          fmt.type == ResourceFormatType::BC5 || fmt.type == ResourceFormatType::BC6 ||
          fmt.type == ResourceFormatType::BC7)
  {
#if ENABLED(RDOC_ANDROID)
    RDCERR("Format %s not supported on android", fmt.Name().c_str());
#else
    const uint16_t whalf = ConvertToHalf(1000.0f);

    byte block[16];

    uint32_t blockSize =
        fmt.type == ResourceFormatType::BC1 || fmt.type == ResourceFormatType::BC4 ? 8 : 16;
    uint32_t tightPitch = (DiscardPatternWidth / 4) * blockSize;
    rowPitch = RDCMAX(rowPitch, tightPitch);

    ret.reserve(rowPitch * (DiscardPatternHeight / 4));

    bytebuf inblock;

    void *bc6opts = NULL;

    CreateOptionsBC6(&bc6opts);

    SetQualityBC6(bc6opts, 0.1f);

    for(uint32_t yi = 0; yi < DiscardPatternHeight; yi += 4)
    {
      uint32_t baseY = invert ? DiscardPatternHeight - 1 - yi : yi;

      for(uint32_t baseX = 0; baseX < DiscardPatternWidth; baseX += 4)
      {
        inblock.clear();

        // inblock is 4x4 RGBA8_UNORM
        if(fmt.type == ResourceFormatType::BC1 || fmt.type == ResourceFormatType::BC2 ||
           fmt.type == ResourceFormatType::BC3 || fmt.type == ResourceFormatType::BC7)
        {
          for(uint32_t y = baseY; y < baseY + 4; invert ? y-- : y++)
          {
            for(uint32_t x = baseX; x < baseX + 4; x++)
            {
              char c = pattern.c_str()[y * DiscardPatternWidth + x];

              inblock.push_back(c == '#' ? 0xff : 0x00);
              inblock.push_back(c == '#' ? 0xff : 0x00);
              inblock.push_back(c == '#' ? 0xff : 0x00);
              inblock.push_back(c == '#' ? 0xff : 0x00);
            }
          }
        }
        // inblock is 4x4 R8_UNORM
        else if(fmt.type == ResourceFormatType::BC4 || fmt.type == ResourceFormatType::BC5)
        {
          for(uint32_t y = baseY; y < baseY + 4; invert ? y-- : y++)
          {
            for(uint32_t x = baseX; x < baseX + 4; x++)
            {
              char c = pattern.c_str()[y * DiscardPatternWidth + x];

              inblock.push_back(c == '#' ? 0xff : 0x00);
            }
          }
        }
        // inblock is 4x4 RGB16_FLOAT
        else if(fmt.type == ResourceFormatType::BC6)
        {
          for(uint32_t y = baseY; y < baseY + 4; invert ? y-- : y++)
          {
            for(uint32_t x = baseX; x < baseX + 4; x++)
            {
              char c = pattern.c_str()[y * DiscardPatternWidth + x];

              inblock.push_back(c == '#' ? (whalf & 0xff) : 0x00);
              inblock.push_back(c == '#' ? ((whalf >> 8) & 0xff) : 0x00);

              inblock.push_back(c == '#' ? (whalf & 0xff) : 0x00);
              inblock.push_back(c == '#' ? ((whalf >> 8) & 0xff) : 0x00);

              inblock.push_back(c == '#' ? (whalf & 0xff) : 0x00);
              inblock.push_back(c == '#' ? ((whalf >> 8) & 0xff) : 0x00);
            }
          }
        }

        if(fmt.type == ResourceFormatType::BC1)
          CompressBlockBC1(inblock.data(), 4 * sizeof(uint32_t), block, NULL);
        else if(fmt.type == ResourceFormatType::BC2)
          CompressBlockBC2(inblock.data(), 4 * sizeof(uint32_t), block, NULL);
        else if(fmt.type == ResourceFormatType::BC3)
          CompressBlockBC3(inblock.data(), 4 * sizeof(uint32_t), block, NULL);
        else if(fmt.type == ResourceFormatType::BC4)
          CompressBlockBC4(inblock.data(), 4, block, NULL);
        else if(fmt.type == ResourceFormatType::BC5)
          CompressBlockBC5(inblock.data(), 4, inblock.data(), 4, block, NULL);
        else if(fmt.type == ResourceFormatType::BC6)
          CompressBlockBC6((uint16_t *)inblock.data(), 4 * 3, block, bc6opts);
        else if(fmt.type == ResourceFormatType::BC7)
          CompressBlockBC7(inblock.data(), 4 * sizeof(uint32_t), block, NULL);

        ret.append(block, blockSize);
      }

      ret.resize(ret.size() + (rowPitch - tightPitch));
    }

    DestroyOptionsBC6(bc6opts);
#endif
  }
  else if(fmt.type == ResourceFormatType::ETC2 || fmt.type == ResourceFormatType::EAC ||
          fmt.type == ResourceFormatType::ASTC || fmt.type == ResourceFormatType::PVRTC ||
          fmt.type == ResourceFormatType::YUV8 || fmt.type == ResourceFormatType::YUV10 ||
          fmt.type == ResourceFormatType::YUV12 || fmt.type == ResourceFormatType::YUV16)
  {
    RDCERR("Format %s not supported for proper discard pattern", fmt.Name().c_str());
  }
  else
  {
    RDCERR("Unhandled format %s needing discard pattern", fmt.Name().c_str());
  }

  // if we didn't get a proper pattern, try at least to do some kind of checkerboard (not knowing if
  // this will align with the format or not)
  if(ret.empty())
  {
    uint32_t tightPitch = DiscardPatternWidth * 16;
    rowPitch = RDCMAX(rowPitch, tightPitch);

    ret.resize(rowPitch * DiscardPatternHeight);

    byte *out = ret.data();
    int val = 0;
    for(uint32_t y = 0; y < DiscardPatternHeight; y++)
    {
      byte *rowout = out;

      for(uint32_t i = 0; i < DiscardPatternWidth; i++)
      {
        memset(rowout, val, 16);
        rowout += 16;

        // toggle between memset(0) and memset(0xff)
        if(val)
          val = 0;
        else
          val = 0xff;
      }

      out += rowPitch;
    }
  }

  return ret;
}

void DeriveNearFar(Vec4f pos, Vec4f pos0, float &nearp, float &farp, bool &found)
{
  //////////////////////////////////////////////////////////////////////////////////
  // derive near/far, assuming a standard perspective matrix
  //
  // the transformation from from pre-projection {Z,W} to post-projection {Z,W}
  // is linear. So we can say Zpost = Zpre*m + c . Here we assume Wpre = 1
  // and we know Wpost = Zpre from the perspective matrix.
  // we can then see from the perspective matrix that
  // m = F/(F-N)
  // c = -(F*N)/(F-N)
  //
  // with re-arranging and substitution, we then get:
  // N = -c/m
  // F = c/(1-m)
  //
  // so if we can derive m and c then we can determine N and F. We can do this with
  // two points, and we pick them reasonably distinct on z to reduce floating-point
  // error

  // skip invalid vertices (w=0)
  if(pos.w != 0.0f && fabsf(pos.w - pos0.w) > 0.01f && fabsf(pos.z - pos0.z) > 0.01f)
  {
    Vec2f A(pos0.w, pos0.z);
    Vec2f B(pos.w, pos.z);

    float m = (B.y - A.y) / (B.x - A.x);
    float c = B.y - B.x * m;

    if(m == 1.0f || c == 0.0f)
      return;

    if(-c / m <= 0.000001f)
      return;

    nearp = -c / m;
    farp = c / (1 - m);

    found = true;
  }
}

#if ENABLED(ENABLE_UNIT_TESTS)

#include "api/replay/pipestate.h"
#include "catch/catch.hpp"

TEST_CASE("Shader usage retains every action and repeated submission without replay",
          "[replay][shader-usage]")
{
  rdcarray<EventUsage> uses;
  CHECK_FALSE(AppendFrameResourceUsage(uses, EventUsage(0, ResourceUsage::Shader)));
  REQUIRE(uses.empty());

  // The same physical shader is used by several actions in a command list, which is then
  // submitted twice. Merge callers supply the submission-rebased EIDs, not command-local EIDs.
  for(uint32_t base : {100U, 200U})
  {
    for(uint32_t local : {3U, 8U, 15U})
    {
      EventUsage usage(base + local, ResourceUsage::Shader);
      CHECK(AppendFrameResourceUsage(uses, usage));
      CHECK_FALSE(AppendFrameResourceUsage(uses, usage));
    }
  }
  REQUIRE(uses.size() == 6);
  const uint32_t expected[] = {103, 108, 115, 203, 208, 215};
  for(size_t i = 0; i < uses.size(); i++)
  {
    CHECK(uses[i].eventId == expected[i]);
    CHECK(uses[i].usage == ResourceUsage::Shader);
  }
  // Remaining command-buffer uses can be merged out of order. Never hide a later-discovered EID.
  CHECK(AppendFrameResourceUsage(uses, EventUsage(109, ResourceUsage::Shader)));
  CHECK(uses.back().eventId == 109);
}

TEST_CASE("Shader usage indexing leaves ordinary resource usage unchanged",
          "[replay][shader-usage]")
{
  rdcarray<EventUsage> uses;
  for(ResourceUsage kind : {ResourceUsage::Unused, ResourceUsage::CopySrc,
                            ResourceUsage::PS_Resource, ResourceUsage::ColorTarget})
  {
    CHECK(AppendFrameResourceUsage(uses, EventUsage(0, kind)));
    CHECK(AppendFrameResourceUsage(uses, EventUsage(42, kind)));
    CHECK(AppendFrameResourceUsage(uses, EventUsage(42, kind)));
  }
  REQUIRE(uses.size() == 12);
  // A resource already having another usage at this EID cannot suppress the shader usage.
  CHECK(AppendFrameResourceUsage(uses, EventUsage(42, ResourceUsage::Shader)));
  CHECK_FALSE(AppendFrameResourceUsage(uses, EventUsage(42, ResourceUsage::Shader)));
  CHECK(uses.size() == 13);
}

TEST_CASE("Checkpoint policy changes preserve versions only inside compatible storage limits",
          "[replay][checkpoint]")
{
  ReplayCheckpointConfig previous;
  ReplayCheckpointConfig next = previous;
  CHECK(ReplayCheckpointStorageCompatible(previous, next));
  next.maxCheckpoints++;
  CHECK(ReplayCheckpointStorageCompatible(previous, next));
  next.maxCheckpoints = previous.maxCheckpoints - 1;
  CHECK_FALSE(ReplayCheckpointStorageCompatible(previous, next));
  next = previous;
  next.byteBudget /= 2;
  CHECK_FALSE(ReplayCheckpointStorageCompatible(previous, next));
  next = previous;
  next.enabled = false;
  CHECK_FALSE(ReplayCheckpointStorageCompatible(previous, next));
  next = previous;
  next.minEventGap++;
  CHECK_FALSE(ReplayCheckpointStorageCompatible(previous, next));
}

TEST_CASE("Shared buffer edits preserve range identity and overlapping undo",
          "[replay][bufferpatch]")
{
  ResourceId a = ResourceIDGen::GetNewUniqueID();
  ResourceId b = ResourceIDGen::GetNewUniqueID();
  ReplayBufferPatchSet edits;
  REQUIRE(edits.Set(a, 4, {1, 2, 3, 4, 5, 6}).empty());
  REQUIRE(edits.Set(a, 6, {10, 11}).empty());
  REQUIRE(edits.Set(b, 6, {20, 21}).empty());
  rdcarray<ReplayBufferPatch> ranges = edits.Flatten();
  REQUIRE(ValidateReplayBufferPatches(ranges).empty());
  bytebuf bytes = {90, 90, 90, 90, 90, 90, 90, 90};
  ApplyReplayBufferPatches(ranges, a, 3, bytes.data(), bytes.size());
  CHECK(bytes == bytebuf({90, 1, 2, 10, 11, 5, 6, 90}));
  REQUIRE(edits.Remove(a, 6, 2));
  bytes = {90, 90, 90, 90, 90, 90, 90, 90};
  ApplyReplayBufferPatches(edits.Flatten(), a, 3, bytes.data(), bytes.size());
  CHECK(bytes == bytebuf({90, 1, 2, 3, 4, 5, 6, 90}));
  CHECK_FALSE(edits.Remove(a, 6, 1));
  REQUIRE(edits.Set(a, 4, {31, 32, 33, 34, 35, 36}).empty());
  bytebuf clipped = {0, 0};
  ApplyReplayBufferPatches(edits.Flatten(), a, 7, clipped.data(), clipped.size());
  CHECK(clipped == bytebuf({34, 35}));
  ApplyReplayBufferPatches(edits.Flatten(), b, 6, clipped.data(), clipped.size());
  CHECK(clipped == bytebuf({20, 21}));
  edits.Clear();
  CHECK(edits.Empty());
  CHECK(edits.Flatten().empty());
}

TEST_CASE("Shared buffer edits reject invalid transactions without losing old values",
          "[replay][bufferpatch]")
{
  ResourceId id = ResourceIDGen::GetNewUniqueID();
  ReplayBufferPatchSet edits;
  REQUIRE(edits.Set(id, 65537, {1, 2, 3, 4}).empty());
  CHECK_FALSE(edits.Set(id, UINT64_MAX, {1, 2}).empty());
  CHECK_FALSE(edits.Set(ResourceId(), 0, {1}).empty());
  CHECK_FALSE(edits.Set(id, 0, {}).empty());
  auto ranges = edits.Flatten();
  REQUIRE(ranges.size() == 1);
  CHECK(ranges[0].byteOffset == 65537);
  CHECK(ranges[0].data == bytebuf({1, 2, 3, 4}));
  ranges.push_back(ranges[0]);
  CHECK_FALSE(ValidateReplayBufferPatches(ranges).empty());
  ranges[1].byteOffset = 0;
  CHECK_FALSE(ValidateReplayBufferPatches(ranges).empty());
}

TEST_CASE("Captured buffer reset removes every overlapping edit and preserves exact undo",
          "[replay][bufferpatch][bufferpatch-reset]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  ReplayBufferPatchSet edits;
  REQUIRE(edits.Set(a, 4, {1, 2, 3, 4, 5, 6, 7, 8}).empty());
  REQUIRE(edits.Set(a, 6, {40, 41, 42, 43}).empty());
  REQUIRE(edits.Set(a, 3, {20, 21}).empty());
  REQUIRE(edits.Set(b, 4, {99, 98}).empty());
  const auto values = [&](ResourceId buffer) {
    bytebuf bytes = {90, 90, 90, 90, 90, 90, 90, 90, 90, 90};
    const auto ranges = edits.Flatten();
    REQUIRE(ValidateReplayBufferPatches(ranges).empty());
    ApplyReplayBufferPatches(ranges, buffer, 3, bytes.data(), bytes.size());
    return bytes;
  };
  REQUIRE(edits.ResetRange(a, 5, 4));
  CHECK(values(a) == bytebuf({20, 21, 90, 90, 90, 90, 43, 7, 8, 90}));
  CHECK(values(b) == bytebuf({90, 99, 98, 90, 90, 90, 90, 90, 90, 90}));
  CHECK_FALSE(edits.ResetRange(a, 5, 4));

  // Original identities still remove the surviving parts of a partially reset edit.
  REQUIRE(edits.Remove(a, 6, 4));
  CHECK(values(a) == bytebuf({20, 21, 90, 90, 90, 90, 6, 7, 8, 90}));
  REQUIRE(edits.Remove(a, 4, 8));
  CHECK(values(a) == bytebuf({20, 21, 90, 90, 90, 90, 90, 90, 90, 90}));
  REQUIRE(edits.Remove(a, 3, 2));
  CHECK(values(a) == bytebuf({90, 90, 90, 90, 90, 90, 90, 90, 90, 90}));
}

TEST_CASE("Captured buffer reset handles regrouped channels without changing adjacent lanes",
          "[replay][bufferpatch][bufferpatch-reset]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  ReplayBufferPatchSet edits;
  const bytebuf rgba = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
  REQUIRE(edits.Set(buffer, 80, rgba).empty());
  const auto values = [&]() {
    bytebuf bytes;
    bytes.resize(16);
    memset(bytes.data(), 90, bytes.size());
    ApplyReplayBufferPatches(edits.Flatten(), buffer, 80, bytes.data(), bytes.size());
    return bytes;
  };
  SECTION("Reset RGB after an RGBA edit preserves alpha and later exact undo")
  {
    REQUIRE(edits.ResetRange(buffer, 80, 12));
    CHECK(values() == bytebuf({90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 13, 14, 15, 16}));
    REQUIRE(edits.Set(buffer, 80, {31, 32, 33, 34}).empty());
    CHECK(values() == bytebuf({31, 32, 33, 34, 90, 90, 90, 90, 90, 90, 90, 90, 13, 14, 15, 16}));
    REQUIRE(edits.Remove(buffer, 80, 4));
    REQUIRE(edits.Remove(buffer, 80, 16));
    CHECK(edits.Empty());
  }
  SECTION("Scattered semantic channels preserve the channels between them")
  {
    REQUIRE(edits.ResetRange(buffer, 80, 4));
    REQUIRE(edits.ResetRange(buffer, 88, 4));
    CHECK(values() == bytebuf({90, 90, 90, 90, 5, 6, 7, 8, 90, 90, 90, 90, 13, 14, 15, 16}));
    REQUIRE(edits.Remove(buffer, 80, 16));
    CHECK(edits.Empty());
  }
  SECTION("Replacing the original exact edit reactivates the whole range")
  {
    REQUIRE(edits.ResetRange(buffer, 84, 4));
    REQUIRE(edits.Set(buffer, 80, rgba).empty());
    CHECK(values() == rgba);
    REQUIRE(edits.Remove(buffer, 80, 16));
    CHECK(edits.Empty());
  }
  SECTION("Invalid and disjoint ranges are no-ops")
  {
    CHECK_FALSE(edits.ResetRange(ResourceId(), 80, 4));
    CHECK_FALSE(edits.ResetRange(buffer, 80, 0));
    CHECK_FALSE(edits.ResetRange(buffer, UINT64_MAX - 1, 4));
    CHECK_FALSE(edits.ResetRange(buffer, 64, 16));
    CHECK_FALSE(edits.ResetRange(buffer, 96, 16));
    CHECK(values() == rgba);
    REQUIRE(edits.ResetRange(buffer, 64, 64));
    CHECK(edits.Empty());
  }
}

TEST_CASE("Shared buffer patch deltas isolate the changed storage and bytes",
          "[replay][bufferpatch]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  rdcarray<ReplayBufferPatch> previous = {
      {a, 64, {1, 2, 3, 4}}, {a, 256, {5, 6, 7, 8, 9}}, {b, 256, {10, 11, 12, 13}}};
  rdcarray<ReplayBufferPatch> next = previous;
  next[1].data = {5, 60, 70, 8, 90};
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes).empty());
  REQUIRE(changes.size() == 2);
  CHECK(changes[0].buffer == a);
  CHECK(changes[0].byteOffset == 257);
  CHECK(changes[0].byteLength == 2);
  CHECK(changes[1].buffer == a);
  CHECK(changes[1].byteOffset == 260);
  CHECK(changes[1].byteLength == 1);
  CHECK(SameReplayBufferPatchLayout(previous, next));

  // Buffer identity is part of an address: identical offsets in another buffer are unrelated.
  next = previous;
  next[2].data[0] = 100;
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].buffer == b);
  CHECK(changes[0].byteOffset == 256);
  CHECK(changes[0].byteLength == 1);
}

TEST_CASE("Shared buffer patch deltas compare flattened values independently of edit layers",
          "[replay][bufferpatch]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  ReplayBufferPatchSet edits;
  REQUIRE(edits.Set(buffer, 32, {1, 2, 3, 4, 5, 6}).empty());
  const rdcarray<ReplayBufferPatch> original = edits.Flatten();
  REQUIRE(edits.Set(buffer, 34, {3, 4}).empty());
  const rdcarray<ReplayBufferPatch> partitioned = edits.Flatten();
  REQUIRE(partitioned.size() == 3);
  CHECK_FALSE(SameReplayBufferPatchLayout(original, partitioned));
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges(original, partitioned, changes).empty());
  CHECK(changes.empty());
  REQUIRE(GetReplayBufferPatchChanges(partitioned, original, changes).empty());
  CHECK(changes.empty());

  REQUIRE(edits.Set(buffer, 34, {3, 40}).empty());
  const rdcarray<ReplayBufferPatch> changed = edits.Flatten();
  REQUIRE(GetReplayBufferPatchChanges(original, changed, changes).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].buffer == buffer);
  CHECK(changes[0].byteOffset == 35);
  CHECK(changes[0].byteLength == 1);

  REQUIRE(edits.Remove(buffer, 34, 2));
  REQUIRE(GetReplayBufferPatchChanges(changed, edits.Flatten(), changes).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].byteOffset == 35);
  CHECK(changes[0].byteLength == 1);
}

TEST_CASE("Shared buffer patch deltas retain coverage changes on resize move and undo",
          "[replay][bufferpatch]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const rdcarray<ReplayBufferPatch> previous = {{buffer, 8, {1, 2, 3, 4}}};
  rdcarray<ReplayBufferPatch> next = {{buffer, 9, {2, 3}}};
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes).empty());
  REQUIRE(changes.size() == 2);
  CHECK(changes[0].byteOffset == 8);
  CHECK(changes[0].byteLength == 1);
  CHECK(changes[1].byteOffset == 11);
  CHECK(changes[1].byteLength == 1);
  CHECK_FALSE(SameReplayBufferPatchLayout(previous, next));
  REQUIRE(GetReplayBufferPatchChanges(next, previous, changes).empty());
  REQUIRE(changes.size() == 2);
  CHECK(changes[0].byteOffset == 8);
  CHECK(changes[1].byteOffset == 11);

  next = {{buffer, 10, {3, 4, 5, 6}}};
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes).empty());
  REQUIRE(changes.size() == 2);
  CHECK(changes[0].byteOffset == 8);
  CHECK(changes[0].byteLength == 2);
  CHECK(changes[1].byteOffset == 12);
  CHECK(changes[1].byteLength == 2);

  // The unpatched original bytes are unknown: adding/removing zero-valued bytes still changes
  // coverage, rather than assuming an absent override means zero.
  next = {{buffer, 20, {0, 0}}};
  REQUIRE(GetReplayBufferPatchChanges({}, next, changes).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].byteOffset == 20);
  CHECK(changes[0].byteLength == 2);
  REQUIRE(GetReplayBufferPatchChanges(next, {}, changes).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].byteOffset == 20);
  CHECK(changes[0].byteLength == 2);
}

TEST_CASE("Shared buffer patch delta limits fail closed without partial results",
          "[replay][bufferpatch]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const rdcarray<ReplayBufferPatch> previous = {{buffer, 8, {0, 0, 0, 0, 0}}};
  rdcarray<ReplayBufferPatch> next = {{buffer, 8, {1, 0, 1, 0, 1}}};
  rdcarray<ReplayBufferPatchRange> changes;
  CHECK_FALSE(GetReplayBufferPatchChanges(previous, next, changes, 2).empty());
  CHECK(changes.empty());
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes, 3).empty());
  REQUIRE(changes.size() == 3);
  CHECK(changes[0].byteOffset == 8);
  CHECK(changes[1].byteOffset == 10);
  CHECK(changes[2].byteOffset == 12);

  // The cap counts merged output ranges, not patch boundaries or individual changed bytes.
  next = {{buffer, 8, {1, 1}}, {buffer, 10, {1, 1, 1}}};
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes, 1).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].byteOffset == 8);
  CHECK(changes[0].byteLength == 5);
  CHECK_FALSE(GetReplayBufferPatchChanges(previous, next, changes, 0).empty());
  CHECK(changes.empty());
  REQUIRE(GetReplayBufferPatchChanges(previous, previous, changes, 0).empty());
  CHECK(changes.empty());
  REQUIRE(GetReplayBufferPatchChanges({}, {}, changes, 0).empty());
  CHECK(changes.empty());
  CHECK_FALSE(SameReplayBufferPatchLayout({}, {}));
}

TEST_CASE("Shared buffer patch deltas reject invalid ranges before address arithmetic",
          "[replay][bufferpatch]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const rdcarray<ReplayBufferPatch> valid = {{buffer, UINT64_MAX - 3, {1, 2, 3}}};
  rdcarray<ReplayBufferPatch> changed = {{buffer, UINT64_MAX - 3, {1, 2, 30}}};
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges(valid, changed, changes).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].byteOffset == UINT64_MAX - 1);
  CHECK(changes[0].byteLength == 1);
  REQUIRE(GetReplayBufferPatchChanges({}, valid, changes).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].byteOffset == UINT64_MAX - 3);
  CHECK(changes[0].byteLength == 3);

  const rdcarray<ReplayBufferPatch> invalid[] = {
      {{buffer, UINT64_MAX - 1, {1, 2}}},
      {{ResourceId(), 0, {1}}},
      {{buffer, 0, {}}},
      {{buffer, 1, {1}}, {buffer, 0, {2}}},
      {{buffer, 0, {1, 2}}, {buffer, 1, {2}}},
  };
  for(const rdcarray<ReplayBufferPatch> &patches : invalid)
  {
    changes = {{buffer, 0, 1}};
    CHECK_FALSE(GetReplayBufferPatchChanges(valid, patches, changes).empty());
    CHECK(changes.empty());
    changes = {{buffer, 0, 1}};
    CHECK_FALSE(GetReplayBufferPatchChanges(patches, valid, changes).empty());
    CHECK(changes.empty());
    CHECK_FALSE(SameReplayBufferPatchLayout(patches, patches));
  }
}

TEST_CASE("Shared buffer patch deltas match an independent byte coverage oracle",
          "[replay][bufferpatch]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  // Enumerate all states of three bytes, each either absent, explicitly zero, or explicitly one.
  // Alternating split/merged inputs exercise address alignment independently of the diff sweep.
  auto patchesFor = [buffer](uint32_t encoded, bool merge) {
    rdcarray<ReplayBufferPatch> patches;
    for(uint64_t offset = 0; offset < 3; offset++, encoded /= 3)
    {
      const uint32_t state = encoded % 3;
      if(state == 0)
        continue;
      if(merge && !patches.empty() && patches.back().byteOffset + patches.back().data.size() == offset)
        patches.back().data.push_back(byte(state - 1));
      else
        patches.push_back({buffer, offset, {byte(state - 1)}});
    }
    return patches;
  };

  for(uint32_t oldState = 0; oldState < 27; oldState++)
  {
    for(uint32_t newState = 0; newState < 27; newState++)
    {
      INFO("old coverage/value state " << oldState << ", new state " << newState);
      rdcarray<ReplayBufferPatchRange> changes;
      REQUIRE(GetReplayBufferPatchChanges(patchesFor(oldState, oldState % 2 != 0),
                                          patchesFor(newState, newState % 2 == 0), changes)
                  .empty());
      bytebuf expected = {0, 0, 0};
      uint32_t oldDigits = oldState, newDigits = newState;
      for(size_t offset = 0; offset < 3; offset++, oldDigits /= 3, newDigits /= 3)
        expected[offset] = byte(oldDigits % 3 != newDigits % 3);

      bytebuf actual = {0, 0, 0};
      bool canonical = true;
      uint64_t previousEnd = 0;
      bool first = true;
      for(const ReplayBufferPatchRange &range : changes)
      {
        if(range.buffer != buffer || range.byteLength == 0 || range.byteOffset >= 3 ||
           range.byteLength > 3 - range.byteOffset || (!first && range.byteOffset <= previousEnd))
        {
          canonical = false;
          break;
        }
        previousEnd = range.byteOffset + range.byteLength;
        first = false;
        for(uint64_t offset = range.byteOffset; offset < previousEnd; offset++)
          actual[size_t(offset)] = 1;
      }
      CHECK(canonical);
      CHECK(actual == expected);
    }
  }
}

TEST_CASE("Published pipeline metadata survives the next event", "[pipeline][publication]")
{
  const ResourceId shader = ResourceIDGen::GetNewUniqueID();
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId sampler = ResourceIDGen::GetNewUniqueID();
  const GraphicsAPI apis[] = {GraphicsAPI::D3D11, GraphicsAPI::D3D12, GraphicsAPI::OpenGL,
                              GraphicsAPI::Vulkan};

  for(GraphicsAPI api : apis)
  {
    INFO("API " << ToStr(api));
    D3D11Pipe::State live11;
    D3D12Pipe::State live12;
    GLPipe::State liveGL;
    VKPipe::State liveVK;
    PipeState source;
    ResourceId *liveShader = NULL;
    Topology *liveTopology = NULL;
    rdcarray<Descriptor> *liveTargets = NULL;

    if(api == GraphicsAPI::D3D11)
    {
      source.SetState(&live11);
      liveShader = &live11.vertexShader.resourceId;
      liveTopology = &live11.inputAssembly.topology;
      liveTargets = &live11.outputMerger.renderTargets;
    }
    else if(api == GraphicsAPI::D3D12)
    {
      source.SetState(&live12);
      liveShader = &live12.vertexShader.resourceId;
      liveTopology = &live12.inputAssembly.topology;
      liveTargets = &live12.outputMerger.renderTargets;
    }
    else if(api == GraphicsAPI::OpenGL)
    {
      source.SetState(&liveGL);
      liveShader = &liveGL.vertexShader.shaderResourceId;
      liveTopology = &liveGL.vertexInput.topology;
      liveTargets = &liveGL.framebuffer.drawFBO.colorAttachments;
      liveGL.framebuffer.drawFBO.drawBuffers = {0};
    }
    else
    {
      source.SetState(&liveVK);
      liveShader = &liveVK.vertexShader.resourceId;
      liveTopology = &liveVK.inputAssembly.topology;
      liveTargets = &liveVK.currentPass.framebuffer.attachments;
      liveVK.currentPass.renderpass.colorAttachments = {0};
    }
    *liveShader = shader;
    *liveTopology = Topology::TriangleList;
    liveTargets->resize(1);
    (*liveTargets)[0].resource = texture;

    rdcarray<DescriptorAccess> access;
    rdcarray<Descriptor> descriptors;
    rdcarray<SamplerDescriptor> samplers;
    access.resize(2);
    descriptors.resize(2);
    samplers.resize(2);
    access[0].stage = access[1].stage = ShaderStage::Vertex;
    access[0].type = DescriptorType::Image;
    access[0].index = 3;
    access[1].type = DescriptorType::Sampler;
    access[1].index = 5;
    descriptors[0].resource = texture;
    samplers[1].object = sampler;
    samplers[1].minLOD = 2.0f;
    source.SetDescriptorAccess(std::move(access), std::move(descriptors), std::move(samplers));

    // The publication owns the API state, while immutable shader reflection may remain shared.
    D3D11Pipe::State owned11 = live11;
    D3D12Pipe::State owned12 = live12;
    GLPipe::State ownedGL = liveGL;
    VKPipe::State ownedVK = liveVK;
    PipeState published;
    published.CopyStateAndRebind(source, &owned11, &owned12, &ownedGL, &ownedVK);

    // Simulate the replay thread overwriting its objects and common metadata for the next event.
    *liveShader = ResourceId();
    *liveTopology = Topology::LineList;
    liveTargets->clear();
    liveGL.framebuffer.drawFBO.drawBuffers.clear();
    liveVK.currentPass.renderpass.colorAttachments.clear();
    source.SetDescriptorAccess({}, {}, {});
    source.SetState((const D3D11Pipe::State *)NULL);

    CHECK(published.IsCaptureD3D11() == (api == GraphicsAPI::D3D11));
    CHECK(published.IsCaptureD3D12() == (api == GraphicsAPI::D3D12));
    CHECK(published.IsCaptureGL() == (api == GraphicsAPI::OpenGL));
    CHECK(published.IsCaptureVK() == (api == GraphicsAPI::Vulkan));
    CHECK(published.GetShader(ShaderStage::Vertex) == shader);
    CHECK(published.GetPrimitiveTopology() == Topology::TriangleList);
    rdcarray<Descriptor> targets = published.GetOutputTargets();
    REQUIRE(targets.size() == 1);
    CHECK(targets[0].resource == texture);
    REQUIRE(published.GetDescriptorAccess().size() == 2);
    CHECK(published.GetDescriptorAccess()[0].index == 3);
    rdcarray<UsedDescriptor> images = published.GetReadOnlyResources(ShaderStage::Vertex);
    REQUIRE(images.size() == 1);
    CHECK(images[0].descriptor.resource == texture);
    rdcarray<UsedDescriptor> publishedSamplers = published.GetSamplers(ShaderStage::Vertex);
    REQUIRE(publishedSamplers.size() == 1);
    CHECK(publishedSamplers[0].access.index == 5);
    CHECK(publishedSamplers[0].sampler.object == sampler);
    CHECK(publishedSamplers[0].sampler.minLOD == 2.0f);
  }
}

TEST_CASE("Replay timing excludes nested sections without double counting", "[replay][timing]")
{
  ReplayTimingData data;
  const ReplayTimingContext context = ReplayTimingContext::QueueSignal;
  const ReplayTimingSection replay = ReplayTimingSection::ContextReplay;
  const ReplayTimingSection wait = ReplayTimingSection::ExplicitWait;

  // Replay spans [0, 100], waits span [10, 70] and [30, 40]. The inner wait must not be
  // added to the already enclosing wait's inclusive time or call count.
  data.BeginSection(replay);
  data.BeginSection(wait);
  data.BeginSection(wait);
  data.EndSection(wait, 10, 0, context);
  data.EndSection(wait, 60, 10, context);
  data.EndSection(replay, 100, 60, context);
  CHECK(data.sections[(size_t)replay].ticks == 100);
  CHECK(data.sections[(size_t)replay].selfTicks == 40);
  CHECK(data.sections[(size_t)wait].ticks == 60);
  CHECK(data.sections[(size_t)wait].selfTicks == 60);
  CHECK(data.sections[(size_t)wait].calls == 1);
  CHECK(data.waits[(size_t)context].ticks == 60);
  CHECK(data.waits[(size_t)context].calls == 1);

  // A second disjoint wait should accumulate normally.
  data.BeginSection(wait);
  data.EndSection(wait, 20, 0, ReplayTimingContext::MapDataWrite);
  CHECK(data.sections[(size_t)wait].ticks == 80);
  CHECK(data.sections[(size_t)wait].selfTicks == 80);
  CHECK(data.sections[(size_t)wait].calls == 2);
  CHECK(data.waits[(size_t)ReplayTimingContext::MapDataWrite].ticks == 20);
}

TEST_CASE("Replay timing handles re-entry through a different section", "[replay][timing]")
{
  ReplayTimingData data;
  const ReplayTimingSection a = ReplayTimingSection::ApplyInitial;
  const ReplayTimingSection b = ReplayTimingSection::InitialGPUCopy;
  const ReplayTimingContext context = ReplayTimingContext::InitialRestore;
  data.BeginSection(a);
  data.BeginSection(b);
  data.BeginSection(a);
  data.EndSection(a, 3, 0, context);
  data.EndSection(b, 5, 3, context);
  data.EndSection(a, 10, 5, context);
  CHECK(data.sections[(size_t)a].ticks == 10);
  CHECK(data.sections[(size_t)a].selfTicks == 8);
  CHECK(data.sections[(size_t)b].ticks == 5);
  CHECK(data.sections[(size_t)b].selfTicks == 2);
  CHECK(data.sections[(size_t)a].calls == 1);
  CHECK(data.sections[(size_t)a].depth == 0);
  CHECK(data.sections[(size_t)b].depth == 0);
}

TEST_CASE("Serialiser parameter boundary callback is read-only and opt-in", "[replay][timing]")
{
  static uint32_t callbacks = 0;
  callbacks = 0;
  auto notify = []() { callbacks++; };
  StreamReader reader(StreamReader::DummyStream);
  ReadSerialiser read(&reader, Ownership::Nothing);
  read.NotifyParametersRead();
  CHECK(callbacks == 0);
  read.SetParameterReadCallback(notify);
  read.NotifyParametersRead();
  CHECK(callbacks == 1);
  read.SetParameterReadCallback(NULL);
  read.NotifyParametersRead();
  CHECK(callbacks == 1);

  StreamWriter writer(64);
  WriteSerialiser write(&writer, Ownership::Nothing);
  write.SetParameterReadCallback(notify);
  write.NotifyParametersRead();
  CHECK(callbacks == 1);
}

TEST_CASE("Replay chunk summary keeps waits overlapping and unknown routes explicit",
          "[replay][timing]")
{
  ReplayTimingData::Chunk decoded;
  decoded.calls = decoded.decoded = 1;
  decoded.ticks[0] = 20;
  decoded.ticks[1] = 80;
  decoded.ticks[3] = 50;
  decoded.ticks[4] = 5;
  ReplayTimingData::Chunk unclassified;
  unclassified.calls = 1;
  unclassified.ticks[2] = 30;
  ReplayTimingData::Chunk remainder;
  remainder.Add(decoded);
  remainder.Add(unclassified);
  CHECK(remainder.TotalTicks() == 130);    // wait and submit are not added twice
  CHECK(remainder.calls == 2);
  CHECK(remainder.decoded == 1);
  CHECK(remainder.ticks[2] == 30);
  CHECK(remainder.ticks[3] == 50);
}

TEST_CASE("Check PreprocessLineDirectives", "[preprocess]")
{
  rdcarray<ShaderSourceFile> sources;

  SECTION("Identity case, one file")
  {
    sources.push_back({"file.hlsl", "float4 foo;"});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "file.hlsl");
    CHECK(sources[0].contents == "float4 foo;");
  }

  SECTION("Identity case, two files")
  {
    sources.push_back({"file.hlsl", "float4 foo;"});
    sources.push_back({"file2.hlsl", "int4 bar;"});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "file.hlsl");
    CHECK(sources[0].contents == "float4 foo;");
    CHECK(sources[1].filename == "file2.hlsl");
    CHECK(sources[1].contents == "int4 bar;");
  }

  SECTION("Identity case, empty filename")
  {
    sources.push_back({"file.hlsl", "float4 foo;"});
    sources.push_back({"file2.hlsl", "int4 bar;"});
    sources.push_back({"", "struct blah {};"});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "file.hlsl");
    CHECK(sources[0].contents == "float4 foo;");
    CHECK(sources[1].filename == "file2.hlsl");
    CHECK(sources[1].contents == "int4 bar;");
    CHECK(sources[2].filename == "");
    CHECK(sources[2].contents == "struct blah {};");
  }

  SECTION("Line number only, identity expansion")
  {
    sources.push_back({"file.hlsl", R"(#line 2
float4 foo;
#line 4
int4 bar;)"});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "file.hlsl");
    CHECK(sources[0].contents == R"(
float4 foo;

int4 bar;)");
  }

  SECTION("Line number only, forward expansion")
  {
    sources.push_back({"file.hlsl", R"(#line 2
float4 foo;
#line 10
int4 bar;)"});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "file.hlsl");
    CHECK(sources[0].contents == R"(
float4 foo;







int4 bar;)");
  }

  SECTION("Line number reversal")
  {
    sources.push_back({"file.hlsl", R"(#line 2
float4 foo;





#line 3
int4 bar;)"});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "file.hlsl");
    CHECK(sources[0].contents == R"(
float4 foo;
int4 bar;)");
  }

  SECTION("Line number overlap")
  {
    sources.push_back({"file.hlsl", R"(#line 2
float4 foo;





#line 2
int4 bar;)"});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "file.hlsl");
    CHECK(sources[0].contents == R"(
float4 foo; /* multiple #lines overlapping */ int4 bar;)");
  }

  SECTION("Line and filename, single file")
  {
    rdcstr merged = R"(#line 2 "file.hlsl"
float4 foo;


#line 10 "file.hlsl"
int4 bar;)";

    sources.push_back({"merged.hlsl", merged});
    sources.push_back({"file.hlsl", ""});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "merged.hlsl");
    CHECK(sources[0].contents == merged);
    CHECK(sources[1].filename == "file.hlsl");
    CHECK(sources[1].contents == R"(
float4 foo;







int4 bar;)");
  }

  SECTION("Line and filename, unreferenced file")
  {
    rdcstr merged = R"(#line 2 "file.hlsl"
float4 foo;


#line 10 "unknown.hlsl"
int4 bar;)";

    sources.push_back({"merged.hlsl", merged});
    sources.push_back({"file.hlsl", ""});

    // unknown.hlsl needs to be in the sources list to be filled in, shader debug information is
    // assumed to at least include an empty referenced filename for used files

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "merged.hlsl");
    CHECK(sources[0].contents == merged);
    CHECK(sources[1].filename == "file.hlsl");
    CHECK(sources[1].contents == R"(
float4 foo;)");
  }

  SECTION("Line and filename, multiple files")
  {
    rdcstr merged = R"(#line 2 "file.hlsl"
float4 foo;


#line 10 "file2.hlsl"
int4 bar;


#line 10 "file.hlsl"
struct blah {};)";

    sources.push_back({"merged.hlsl", merged});
    sources.push_back({"file.hlsl", ""});
    sources.push_back({"file2.hlsl", ""});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "merged.hlsl");
    CHECK(sources[0].contents == merged);
    CHECK(sources[1].filename == "file.hlsl");
    CHECK(sources[1].contents == R"(
float4 foo;







struct blah {};)");
    CHECK(sources[2].filename == "file2.hlsl");
    CHECK(sources[2].contents == R"(








int4 bar;)");
  }

  SECTION("Many-to-many file expansions")
  {
    rdcstr merged1 = R"(#line 2 "file.hlsl"
float4 foo;


#line 10 "file2.hlsl"
int4 bar;


#line 10 "file.hlsl"
struct blah {};)";

    rdcstr merged2 = R"(#line 3 "file.hlsl"
// this was foo


#line 11 "file2.hlsl"
// this was bar


#line 11 "file.hlsl"
// this was a struct)";

    sources.push_back({"merged1.hlsl", merged1});
    sources.push_back({"merged2.hlsl", merged2});
    sources.push_back({"file.hlsl", ""});
    sources.push_back({"file2.hlsl", ""});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "merged1.hlsl");
    CHECK(sources[0].contents == merged1);
    CHECK(sources[1].filename == "merged2.hlsl");
    CHECK(sources[1].contents == merged2);
    CHECK(sources[2].filename == "file.hlsl");
    CHECK(sources[2].contents == R"(
float4 foo;
// this was foo






struct blah {};
// this was a struct)");
    CHECK(sources[3].filename == "file2.hlsl");
    CHECK(sources[3].contents == R"(








int4 bar;
// this was bar)");
  }

  SECTION("Preamble, with only line number changes")
  {
    sources.push_back({"file.hlsl", R"(
// preamble
#line 5
float4 foo;





#line 6
int4 bar;)"});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "file.hlsl");
    CHECK(sources[0].contents == R"(
// preamble


float4 foo;
int4 bar;)");
  }

  SECTION("Preamble with different filenames")
  {
    rdcstr merged = R"(
// merged preamble

#line 2 "file.hlsl"
float4 foo;


#line 10 "file2.hlsl"
int4 bar;


#line 10 "file.hlsl"
struct blah {};)";

    sources.push_back({"merged.hlsl", merged});
    sources.push_back({"file.hlsl", ""});
    sources.push_back({"file2.hlsl", ""});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "merged.hlsl");
    CHECK(sources[0].contents == merged);
    CHECK(sources[1].filename == "file.hlsl");
    CHECK(sources[1].contents == R"(
float4 foo;







struct blah {};)");
    CHECK(sources[2].filename == "file2.hlsl");
    CHECK(sources[2].contents == R"(








int4 bar;)");
  }

  SECTION("Preamble with self filename")
  {
    rdcstr merged = R"(
// merged preamble

#line 5 "merged.hlsl"
float4 foo;


#line 10 "merged.hlsl"
int4 bar;)";

    sources.push_back({"merged.hlsl", merged});

    PreprocessLineDirectives(sources);

    CHECK(sources[0].filename == "merged.hlsl");
    CHECK(sources[0].contents == R"(
// merged preamble


float4 foo;




int4 bar;)");
  }
}

#endif
