/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 RenderDoc contributors
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

#include <map>
#include <set>
#include "api/replay/rdcarray.h"
#include "api/replay/resourceid.h"

// Device-local content identity, not an EID or a hash of presentation pixels. A backend can reuse
// an identity only after proving that the bytes are unchanged. Zero is never a known identity.
struct ReplayResourceVersion
{
  uint64_t content = 0;
  uint64_t editGeneration = 0;

  bool Known() const { return content != 0; }
  bool operator==(const ReplayResourceVersion &other) const
  {
    return content == other.content && editGeneration == other.editGeneration;
  }
  bool operator!=(const ReplayResourceVersion &other) const { return !(*this == other); }
};

// Half-open storage range. Buffers use byte offsets. Texture subresources use [0, 1), unless the
// backend can independently copy and prove content for smaller tiles. IDs are original capture IDs.
struct ReplayResourceRange
{
  ResourceId resource;
  uint32_t subresource = 0;
  uint64_t offset = 0;
  uint64_t size = 0;
};

enum class ReplayResourceFirstAccess
{
  Unknown,
  Read,
  FullOverwrite,
  Unused,
};

struct ReplayResourceAccessProof
{
  ReplayResourceFirstAccess firstAccess = ReplayResourceFirstAccess::Unknown;
  // Covers the complete replay interval AND any output inspection performed before leaving it.
  // A missing entry in GetUsage, a writable descriptor, or Discard alone is not such a proof.
  bool complete = false;
  // Includes unresolved aliasing, sparse mappings, indirect addresses and hidden shader accesses.
  bool aliasUncertain = true;
};

enum class ReplayResourceRestoreAction
{
  Keep,
  Restore,
  Defer,
  // The requested checkpoint precedes an edit affecting this storage. Rebuild the prefix instead.
  Rebuild,
};

// Shared policy, usable without the ledger when a backend already tracks exact resident lineage.
ReplayResourceRestoreAction PlanReplayResourceRestore(const ReplayResourceVersion &resident,
                                                      const ReplayResourceVersion &target,
                                                      uint64_t minimumEditGeneration,
                                                      const ReplayResourceAccessProof &proof);

struct ReplayResourceRestoreItem
{
  ReplayResourceRange range;
  ReplayResourceRestoreAction action = ReplayResourceRestoreAction::Restore;
  ReplayResourceVersion resident;
  ReplayResourceVersion target;
};

// This ledger does not own GPU allocations or perform transitions. Register only independently
// restorable units. Mutations must be reported after commands are ordered on the replay queue;
// uncertain/aborted execution must call Invalidate, never MarkResident. A Plan call records debt,
// not a successful copy. There is deliberately no "clear all dirty at end of replay" operation.
class ReplayResourceVersions
{
public:
  // capturedReadOnly needs complete capture-wide CPU/GPU write provenance, not resource flags or
  // absent usage records. knownAliasGroup is a backing allocation identity, or null if unaliased.
  bool Register(ResourceId resource, uint32_t subresource, uint64_t extent,
                bool capturedReadOnly = false, ResourceId knownAliasGroup = ResourceId());
  void Clear();

  // Invalid/unregistered ranges return one Restore item without mutating the ledger.
  rdcarray<ReplayResourceRestoreItem> Plan(const ReplayResourceRange &range,
                                           const ReplayResourceVersion &target,
                                           const ReplayResourceAccessProof &proof = {});
  // A completed overwrite also supersedes deferred initial bytes, only inside this range.
  bool MarkResident(const ReplayResourceRange &range, const ReplayResourceVersion &version);
  bool Invalidate(const ReplayResourceRange &range);
  void InvalidateAll();

  // Inspection also needs materialization: ask Plan with a complete Read proof first. Returned
  // debt persists until a corresponding MarkResident or a new Plan establishes the right version.
  rdcarray<ReplayResourceRange> Pending() const;
  bool IsResident(const ReplayResourceRange &range, const ReplayResourceVersion &version) const;

  // affected includes the edited bytes AND transitively dependent outputs. Unknown shader/alias
  // dependencies require dependencyClosureComplete=false, invalidating every mutable unit. Truly
  // read-only units remain shared unless they are explicitly included or alias an affected unit.
  uint64_t InvalidateForEdit(const rdcarray<ReplayResourceRange> &affected,
                             bool dependencyClosureComplete);
  uint64_t EditGeneration() const { return m_EditGeneration; }

private:
  struct Key
  {
    ResourceId resource;
    uint32_t subresource = 0;
    bool operator<(const Key &other) const
    {
      return resource < other.resource ||
             (resource == other.resource && subresource < other.subresource);
    }
  };
  struct Span
  {
    uint64_t begin = 0, end = 0;
    ReplayResourceVersion resident, wanted;
    uint64_t minimumEditGeneration = 0;
    bool pending = false;
  };
  struct Resource
  {
    uint64_t extent = 0;
    bool capturedReadOnly = false;
    ResourceId aliasGroup;
    rdcarray<Span> spans;
  };
  typedef std::map<Key, Resource> Resources;
  Resources m_Resources;
  uint64_t m_EditGeneration = 0;

  Resources::iterator Find(const ReplayResourceRange &range);
  Resources::const_iterator Find(const ReplayResourceRange &range) const;
  static void Split(Resource &resource, uint64_t position);
  static void Coalesce(Resource &resource);
  void InvalidateAliases(const Key &key, ResourceId group, uint64_t minimumEditGeneration);
};

enum class ReplayResourceAccessType
{
  Read,
  Write,
  ReadWrite,
};

struct ReplayResourceWriteQuery
{
  rdcarray<ReplayResourceRange> ranges;
  uint64_t generation = 0;
  bool complete = false;
};

struct ReplayResourceReadQuery
{
  uint64_t generation = 0;
  uint32_t eventId = 0;
  bool complete = false;
  bool found = false;
};

struct ReplayResourceEditQuery
{
  // Includes input edits and potentially dependent outputs, coalesced by resource/subresource.
  rdcarray<ReplayResourceRange> affected;
  rdcarray<uint32_t> events;
  uint64_t generation = 0;
  bool complete = false;
};

// CPU-only access metadata for original capture resources. Storage intervals are half-open;
// queried EID intervals include both endpoints. Record actual global submission EIDs, never baked
// command-list-relative EIDs. This index has no GPU allocation, resource lifetime or content state.
//
// Completeness is a backend proof, never inferred from missing GetUsage entries. Finalize(true)
// requires the complete resource/dependency closure and every possible read/write in the covered
// interval, including hidden/indirect/CPU access. Unresolved aliases must remain aliasUncertain.
// FinalizeReads(true) proves only reads of the registered units: it cannot prove writes or an edit
// dependency closure. A query with complete=false is diagnostic only, even when it found entries.
class ReplayResourceAccessIndex
{
public:
  // Access entries include both known accesses and unknown-event markers. Query work counts
  // inspected records, binary-search steps and range-merging/output work, not just found events.
  // Exhaustion loses proof, never truncates a complete answer. Clear preserves these limits.
  explicit ReplayResourceAccessIndex(size_t maxAccessEntries = 1000000,
                                     size_t maxResources = 65536, size_t maxQueryWork = 1000000)
      : m_MaxAccessEntries(maxAccessEntries), m_MaxResources(maxResources),
        m_MaxQueryWork(maxQueryWork)
  {
  }

  bool Register(ResourceId original, uint32_t subresource, uint64_t extent,
                bool aliasUncertain = true);
  bool Record(uint32_t eventId, const ReplayResourceRange &range, ReplayResourceAccessType type,
              bool complete = false);
  bool RecordRead(uint32_t eventId, const ReplayResourceRange &range, bool complete = false)
  {
    return Record(eventId, range, ReplayResourceAccessType::Read, complete);
  }
  bool RecordWrite(uint32_t eventId, const ReplayResourceRange &range, bool complete = false)
  {
    return Record(eventId, range, ReplayResourceAccessType::Write, complete);
  }
  void RecordUnknown(uint32_t eventId);
  bool Finalize(uint32_t firstEvent, uint32_t lastEvent, bool complete = false);
  bool FinalizeReads(uint32_t firstEvent, uint32_t lastEvent, bool complete = false);

  ReplayResourceWriteQuery QueryWrites(uint32_t firstEvent, uint32_t lastEvent) const;
  ReplayResourceReadQuery FirstRead(const ReplayResourceRange &range, uint32_t firstEvent,
                                    uint32_t lastEvent) const;
  // Inputs are affected state at the start of firstEvent; earlier events are not replayed here.
  // Reads are evaluated before writes within one EID. Any affected read conservatively taints
  // every output of that event. Later overwrites never sever dependency without a separate proof.
  ReplayResourceEditQuery PropagateEdit(const rdcarray<ReplayResourceRange> &inputs,
                                        uint32_t firstEvent, uint32_t lastEvent) const;

  // Query results belong to their generation. Shader/CBuffer edits may change future access;
  // invalidate before publishing an edit and rebuild from new evidence. Finalize alone cannot
  // revive old records after invalidation. Clear discards them and starts a new generation.
  void InvalidateForEdit();
  void Clear();
  uint64_t Generation() const { return m_Generation; }

private:
  struct Key
  {
    ResourceId resource;
    uint32_t subresource = 0;
    bool operator<(const Key &other) const
    {
      return resource < other.resource ||
             (resource == other.resource && subresource < other.subresource);
    }
  };
  struct Resource
  {
    uint64_t extent = 0;
    bool aliasUncertain = true;
    rdcarray<size_t> reads;
  };
  struct Access
  {
    ReplayResourceRange range;
    uint32_t eventId = 0;
    ReplayResourceAccessType type = ReplayResourceAccessType::Read;
  };
  typedef std::map<Key, rdcarray<ReplayResourceRange>> RangeMap;
  std::map<Key, Resource> m_Resources;
  rdcarray<Access> m_Accesses;
  rdcarray<size_t> m_Writes;
  std::map<uint32_t, rdcarray<size_t>> m_EventWrites;
  rdcarray<uint32_t> m_UnknownEvents;
  size_t m_MaxAccessEntries, m_MaxResources, m_MaxQueryWork;
  size_t m_AccessEntries = 0;
  uint64_t m_Generation = 1;
  uint32_t m_FirstEvent = 0, m_LastEvent = 0;
  bool m_Finalized = false, m_Invalidated = false, m_InvalidInput = false;
  bool m_ReadsComplete = false, m_WritesComplete = false;

  bool ValidRange(const ReplayResourceRange &range) const;
  bool Ready(uint32_t firstEvent, uint32_t lastEvent) const;
  bool Complete(uint32_t firstEvent, uint32_t lastEvent, bool writes) const;
  bool FinalizeAccess(uint32_t firstEvent, uint32_t lastEvent, bool reads, bool writes);
  bool ReserveAccessEntries(size_t count);
  static bool SpendWork(size_t &remaining);
  bool LowerEvent(const rdcarray<size_t> &indices, uint32_t eventId, size_t &position,
                  size_t &work) const;
  bool QueueReads(const ReplayResourceRange &range, uint32_t firstEvent, uint32_t lastEvent,
                  std::set<uint32_t> &pending, size_t &work) const;
  static bool AddRange(RangeMap &ranges, const ReplayResourceRange &range, size_t &work, bool &added);
  static bool Flatten(const RangeMap &ranges, rdcarray<ReplayResourceRange> &result, size_t &work);
};
