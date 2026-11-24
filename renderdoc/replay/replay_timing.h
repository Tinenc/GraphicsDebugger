/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 Baldur Karlsson
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

#include "core/core.h"

// Lightweight wall-clock decomposition for interactive ReplayLog calls. The implementation is a
// inactive unless RENDERDOC_EID_TIMING=1 was present when timing is first queried. Disabled
// sections/counters only check the active thread-local scope; no clock, allocation or logging.
// Section times are inclusive, while *_self_ms excludes nested sections. Re-entering the same
// section does not double-count its inclusive time. ExplicitWait/Submit are subsets of total.
enum class ReplayTimingSection : uint8_t
{
  ApplyInitial = 0,
  ContextReplay,
  ExplicitWait,
  ExplicitSubmit,
  InitialDescriptors,
  InitialCPUCopy,
  InitialGPUCopy,
  InitialSparseBinds,
  InitialAccelerationStructures,
  InitialBarriers,
  InitialResidency,
  InitialListClose,
  ResetAllocators,
  ReplaySetup,
  ReplayBookkeeping,
  ChunkFraming,
  ChunkDecode,
  ChunkExecute,
  ChunkUnclassified,
  ReplayMetadata,
  Count,
};

// The most specific active context attributes actual CPU wait intervals to their caller. Context
// scopes do not time whole functions, and therefore cannot turn GPU submission time into wait time.
enum class ReplayTimingContext : uint8_t
{
  Unspecified = 0,
  ReplayReset,
  InitialRestore,
  InitialFallback,
  ContextReplay,
  ReplayFinish,
  QueueSignal,
  DataUploadSync,
  DataUploadPoolReuse,
  MapDataWrite,
  WriteToSubresource,
  PreparedCacheInvalidate,
  DebugSingleSubmit,
  DescriptorWrite,
  DescriptorCopy,
  QueueTransition,
  CapturedFence,
  HostRestoreHazard,
  Count,
};

enum class ReplayTimingCounter : uint8_t
{
  InitialResources = 0,
  InitialDescriptorHeaps,
  InitialDescriptors,
  InitialDescriptorRanges,
  InitialDescriptorFullRestores,
  InitialDescriptorsSkipped,
  InitialBuffers,
  InitialTextures,
  InitialHeaps,
  InitialReadOnlySkipped,
  InitialAccelerationStructures,
  InitialAccelerationStructuresSkipped,
  InitialCPUCopyResources,
  InitialCPUCopyBytes,
  InitialGPUCopyResources,
  InitialGPUCopyBufferBytes,
  InitialGPUCopyFootprintBytes,
  InitialGPUCopyWholeResources,
  InitialUploadCacheHits,
  InitialUploadCacheBytes,
  InitialUploadCachePromotedBytes,
  InitialUploadCacheFallbackBytes,
  InitialSparseResources,
  InitialBarriers,
  InitialCommandLists,
  InternalListsCreated,
  InternalListsReused,
  InternalListsSubmitted,
  DataUploadListsCreated,
  DataUploadPoolResets,
  QueueSignalsDeferred,
  QueueSignalsConservative,
  DescriptorWriteWaits,
  DescriptorCopyWaits,
  DescriptorWritesSkipped,
  DescriptorCopiesSkipped,
  ChunksProcessed,
  ChunkBytes,
  ChunkDecodeBoundaries,
  ChunkDecodeBoundaryMissing,
  InitialClearBytes,
  InitialClearResources,
  InitialLayoutResources,
  InitialQueryResets,
  HostWriteCacheHits,
  HostWriteGPUBytes,
  DecodedCommandCacheHits,
  DecodedCommandCacheBytes,
  PreparedModes,
  InitialOverwrittenBytesSkipped,
  InitialProgramRelinks,
  InitialProgramRelinksSkipped,
  Count,
};

bool IsReplayTimingActive();
void AddReplayTimingCounter(ReplayTimingCounter counter, uint64_t value = 1);
// Installed on the replay serialiser only when timing is active. The callback marks the checked
// parameter-read boundary, including resource-ID resolution; it does not measure pure driver time.
void NotifyReplayTimingParametersDecoded();
void SetReplayTimingChunkNameLookup(rdcstr (*lookup)(uint32_t));

// Append a backend-specific detail record to the same per-run timing log used by
// ScopedReplayTiming. Callers must still gate formatting behind RENDERDOC_EID_TIMING=1.
void AppendReplayTimingLog(const rdcstr &line);

class ScopedReplayTimingSection;
class ScopedReplayTimingContext;
class ScopedReplayTimingChunk;
struct ReplayTimingData;

class ScopedReplayTiming
{
public:
  ScopedReplayTiming(const char *driver, uint32_t startEventID, uint32_t endEventID,
                     ReplayLogType replayType, bool partial);
  ~ScopedReplayTiming();

  ScopedReplayTiming(const ScopedReplayTiming &) = delete;
  ScopedReplayTiming &operator=(const ScopedReplayTiming &) = delete;

private:
  friend class ScopedReplayTimingSection;
  friend class ScopedReplayTimingContext;
  friend class ScopedReplayTimingChunk;
  friend void AddReplayTimingCounter(ReplayTimingCounter counter, uint64_t value);
  friend void SetReplayTimingChunkNameLookup(rdcstr (*lookup)(uint32_t));

  const char *m_Driver = NULL;
  uint32_t m_StartEventID = 0;
  uint32_t m_EndEventID = 0;
  ReplayLogType m_ReplayType = eReplay_Full;
  bool m_Partial = true;
  uint64_t m_StartTick = 0;
  ReplayTimingData *m_Data = NULL;
  ScopedReplayTiming *m_Previous = NULL;
};

class ScopedReplayTimingSection
{
public:
  explicit ScopedReplayTimingSection(ReplayTimingSection section, bool start = true);
  ~ScopedReplayTimingSection();
  void Begin();
  void End();

  ScopedReplayTimingSection(const ScopedReplayTimingSection &) = delete;
  ScopedReplayTimingSection &operator=(const ScopedReplayTimingSection &) = delete;

private:
  friend class ScopedReplayTiming;
  friend class ScopedReplayTimingChunk;
  // Before the first checked parameter boundary, a chunk remains unclassified. Reclassification
  // transfers its still-open interval, never inventing a decode phase for routes without a marker.
  void Reclassify(ReplayTimingSection section);
  ReplayTimingSection m_Section;
  ScopedReplayTiming *m_Replay = NULL;
  ScopedReplayTimingSection *m_Parent = NULL;
  ReplayTimingContext m_Context = ReplayTimingContext::Unspecified;
  uint64_t m_StartTick = 0;
  uint64_t m_ChildTicks = 0;
};

// One scope immediately around ProcessChunk. The serialiser callback divides parameter decoding
// from replay wrapper execution/recording. Wait/submit remain nested sections of execution. Missing
// markers are explicitly reported as unclassified. Per-type records are emitted only at ReplayLog
// exit (top 12 plus aggregate remainder), never per chunk.
class ScopedReplayTimingChunk
{
public:
  explicit ScopedReplayTimingChunk(uint32_t chunkID);
  ~ScopedReplayTimingChunk();
  ScopedReplayTimingChunk(const ScopedReplayTimingChunk &) = delete;
  ScopedReplayTimingChunk &operator=(const ScopedReplayTimingChunk &) = delete;

private:
  friend void NotifyReplayTimingParametersDecoded();
  void ParametersDecoded();
  uint32_t m_ChunkID;
  ScopedReplayTiming *m_Replay = NULL;
  ScopedReplayTimingChunk *m_Previous = NULL;
  bool m_Decoded = false;
  uint64_t m_SectionTicks[5] = {};
  uint64_t m_SectionSelfTicks[3] = {};
  ScopedReplayTimingSection m_Decode;
  ScopedReplayTimingSection m_Execute;
};

class ScopedReplayTimingContext
{
public:
  explicit ScopedReplayTimingContext(ReplayTimingContext context);
  ~ScopedReplayTimingContext();

  ScopedReplayTimingContext(const ScopedReplayTimingContext &) = delete;
  ScopedReplayTimingContext &operator=(const ScopedReplayTimingContext &) = delete;

private:
  ScopedReplayTiming *m_Replay = NULL;
  ReplayTimingContext m_Previous = ReplayTimingContext::Unspecified;
};
