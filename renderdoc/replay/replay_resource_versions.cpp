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

#include "replay_resource_versions.h"
#include <algorithm>
#include "common/common.h"

template <>
rdcstr DoStringise(const ReplayResourceRestoreAction &el)
{
  BEGIN_ENUM_STRINGISE(ReplayResourceRestoreAction);
  {
    STRINGISE_ENUM_CLASS(Keep)
    STRINGISE_ENUM_CLASS(Restore)
    STRINGISE_ENUM_CLASS(Defer)
    STRINGISE_ENUM_CLASS(Rebuild)
  }
  END_ENUM_STRINGISE();
}

template <>
rdcstr DoStringise(const ReplayResourceFirstAccess &el)
{
  BEGIN_ENUM_STRINGISE(ReplayResourceFirstAccess);
  {
    STRINGISE_ENUM_CLASS(Unknown)
    STRINGISE_ENUM_CLASS(Read)
    STRINGISE_ENUM_CLASS(FullOverwrite)
    STRINGISE_ENUM_CLASS(Unused)
  }
  END_ENUM_STRINGISE();
}

ReplayResourceRestoreAction PlanReplayResourceRestore(const ReplayResourceVersion &resident,
                                                      const ReplayResourceVersion &target,
                                                      uint64_t minimumEditGeneration,
                                                      const ReplayResourceAccessProof &proof)
{
  if(target.editGeneration < minimumEditGeneration)
    return ReplayResourceRestoreAction::Rebuild;
  if(resident.Known() && target.Known() && resident == target)
    return ReplayResourceRestoreAction::Keep;
  if(target.Known() && proof.complete && !proof.aliasUncertain &&
     (proof.firstAccess == ReplayResourceFirstAccess::FullOverwrite ||
      proof.firstAccess == ReplayResourceFirstAccess::Unused))
    return ReplayResourceRestoreAction::Defer;
  return ReplayResourceRestoreAction::Restore;
}

bool ReplayResourceVersions::Register(ResourceId id, uint32_t subresource, uint64_t extent,
                                      bool capturedReadOnly, ResourceId knownAliasGroup)
{
  if(id == ResourceId() || extent == 0)
    return false;
  Key key = {id, subresource};
  if(m_Resources.find(key) != m_Resources.end())
    return false;
  Resource &resource = m_Resources[key];
  resource.extent = extent;
  resource.capturedReadOnly = capturedReadOnly;
  resource.aliasGroup = knownAliasGroup;
  Span span;
  span.end = extent;
  // Late registration cannot certify that an old checkpoint predates no relevant edits.
  span.minimumEditGeneration = m_EditGeneration;
  resource.spans.push_back(span);
  return true;
}

void ReplayResourceVersions::Clear()
{
  m_Resources.clear();
  m_EditGeneration = 0;
}

ReplayResourceVersions::Resources::iterator ReplayResourceVersions::Find(const ReplayResourceRange &range)
{
  const auto found = m_Resources.find({range.resource, range.subresource});
  if(found == m_Resources.end() || range.size == 0 || range.offset >= found->second.extent ||
     range.size > found->second.extent - range.offset)
    return m_Resources.end();
  return found;
}

ReplayResourceVersions::Resources::const_iterator ReplayResourceVersions::Find(
    const ReplayResourceRange &range) const
{
  const auto found = m_Resources.find({range.resource, range.subresource});
  if(found == m_Resources.end() || range.size == 0 || range.offset >= found->second.extent ||
     range.size > found->second.extent - range.offset)
    return m_Resources.end();
  return found;
}

void ReplayResourceVersions::Split(Resource &resource, uint64_t position)
{
  for(size_t i = 0; i < resource.spans.size(); i++)
  {
    Span &span = resource.spans[i];
    if(span.begin < position && position < span.end)
    {
      Span right = span;
      right.begin = position;
      span.end = position;
      resource.spans.insert(i + 1, right);
      return;
    }
  }
}

void ReplayResourceVersions::Coalesce(Resource &resource)
{
  size_t kept = 0;
  for(size_t i = 0; i < resource.spans.size(); i++)
  {
    const Span &span = resource.spans[i];
    if(kept > 0)
    {
      Span &previous = resource.spans[kept - 1];
      if(previous.end == span.begin && previous.resident == span.resident &&
         previous.wanted == span.wanted && previous.pending == span.pending &&
         previous.minimumEditGeneration == span.minimumEditGeneration)
      {
        previous.end = span.end;
        continue;
      }
    }
    if(kept != i)
      resource.spans[kept] = span;
    kept++;
  }
  resource.spans.resize(kept);
  // Alternating edits to a large buffer must not produce unbounded metadata. Losing precision is
  // safe: this unit becomes unknown and must be restored/rebuilt before its next read.
  if(resource.spans.size() > 4096)
  {
    Span unknown;
    unknown.end = resource.extent;
    unknown.pending = true;
    for(const Span &span : resource.spans)
      unknown.minimumEditGeneration =
          RDCMAX(unknown.minimumEditGeneration, span.minimumEditGeneration);
    resource.spans.resize(1);
    resource.spans[0] = unknown;
  }
}

rdcarray<ReplayResourceRestoreItem> ReplayResourceVersions::Plan(const ReplayResourceRange &range,
                                                                 const ReplayResourceVersion &target,
                                                                 const ReplayResourceAccessProof &proof)
{
  rdcarray<ReplayResourceRestoreItem> result;
  auto found = Find(range);
  if(found == m_Resources.end())
  {
    result.push_back({range, ReplayResourceRestoreAction::Restore, {}, target});
    return result;
  }
  Resource &resource = found->second;
  const uint64_t end = range.offset + range.size;
  Split(resource, range.offset);
  Split(resource, end);
  for(Span &span : resource.spans)
  {
    if(span.begin < range.offset || span.end > end)
      continue;
    const ReplayResourceRestoreAction action =
        PlanReplayResourceRestore(span.resident, target, span.minimumEditGeneration, proof);
    span.wanted = target;
    span.pending = action != ReplayResourceRestoreAction::Keep;
    result.push_back({{range.resource, range.subresource, span.begin, span.end - span.begin},
                      action,
                      span.resident,
                      target});
  }
  Coalesce(resource);
  return result;
}

void ReplayResourceVersions::InvalidateAliases(const Key &key, ResourceId group,
                                               uint64_t minimumEditGeneration)
{
  if(group == ResourceId())
    return;
  for(auto &entry : m_Resources)
  {
    if(entry.second.aliasGroup != group ||
       (entry.first.resource == key.resource && entry.first.subresource == key.subresource))
      continue;
    for(Span &span : entry.second.spans)
    {
      span.resident = {};
      span.pending = true;
      span.minimumEditGeneration = RDCMAX(span.minimumEditGeneration, minimumEditGeneration);
    }
    Coalesce(entry.second);
  }
}

bool ReplayResourceVersions::MarkResident(const ReplayResourceRange &range,
                                          const ReplayResourceVersion &version)
{
  auto found = Find(range);
  if(found == m_Resources.end() || !version.Known())
    return false;
  Resource &resource = found->second;
  const uint64_t end = range.offset + range.size;
  // An old checkpoint must not become valid merely because the physical copy completed.
  for(const Span &span : resource.spans)
    if(span.begin < end && span.end > range.offset &&
       version.editGeneration < span.minimumEditGeneration)
      return false;
  Split(resource, range.offset);
  Split(resource, end);
  for(Span &span : resource.spans)
  {
    if(span.begin < range.offset || span.end > end)
      continue;
    span.resident = version;
    // A completed full overwrite supersedes a deferred initial version. Partial writes must
    // report only their covered range; untouched debt survives in the adjacent spans.
    span.wanted = version;
    span.pending = false;
  }
  InvalidateAliases(found->first, resource.aliasGroup, 0);
  Coalesce(resource);
  return true;
}

bool ReplayResourceVersions::Invalidate(const ReplayResourceRange &range)
{
  auto found = Find(range);
  if(found == m_Resources.end())
    return false;
  Resource &resource = found->second;
  const uint64_t end = range.offset + range.size;
  Split(resource, range.offset);
  Split(resource, end);
  for(Span &span : resource.spans)
    if(span.begin >= range.offset && span.end <= end)
    {
      span.resident = {};
      span.pending = true;
    }
  InvalidateAliases(found->first, resource.aliasGroup, 0);
  Coalesce(resource);
  return true;
}

void ReplayResourceVersions::InvalidateAll()
{
  for(auto &entry : m_Resources)
  {
    for(Span &span : entry.second.spans)
    {
      span.resident = {};
      span.pending = true;
    }
    Coalesce(entry.second);
  }
}

rdcarray<ReplayResourceRange> ReplayResourceVersions::Pending() const
{
  rdcarray<ReplayResourceRange> ranges;
  for(const auto &entry : m_Resources)
    for(const Span &span : entry.second.spans)
      if(span.pending)
        ranges.push_back(
            {entry.first.resource, entry.first.subresource, span.begin, span.end - span.begin});
  return ranges;
}

bool ReplayResourceVersions::IsResident(const ReplayResourceRange &range,
                                        const ReplayResourceVersion &version) const
{
  auto found = Find(range);
  if(found == m_Resources.end() || !version.Known())
    return false;
  const uint64_t end = range.offset + range.size;
  for(const Span &span : found->second.spans)
    if(span.begin < end && span.end > range.offset &&
       (span.resident != version || version.editGeneration < span.minimumEditGeneration))
      return false;
  return true;
}

uint64_t ReplayResourceVersions::InvalidateForEdit(const rdcarray<ReplayResourceRange> &affected,
                                                   bool dependencyClosureComplete)
{
  m_EditGeneration++;
  // Bad dependency ranges must not accidentally turn a supposedly complete closure into no work.
  for(const ReplayResourceRange &range : affected)
    if(Find(range) == m_Resources.end())
      dependencyClosureComplete = false;
  if(!dependencyClosureComplete)
    for(auto &entry : m_Resources)
      if(!entry.second.capturedReadOnly)
      {
        for(Span &span : entry.second.spans)
        {
          span.resident = {};
          span.pending = true;
          span.minimumEditGeneration = m_EditGeneration;
        }
        InvalidateAliases(entry.first, entry.second.aliasGroup, m_EditGeneration);
        Coalesce(entry.second);
      }
  for(const ReplayResourceRange &range : affected)
  {
    auto found = Find(range);
    if(found == m_Resources.end())
      continue;
    Resource &resource = found->second;
    const uint64_t end = range.offset + range.size;
    Split(resource, range.offset);
    Split(resource, end);
    for(Span &span : resource.spans)
      if(span.begin >= range.offset && span.end <= end)
      {
        span.resident = {};
        span.pending = true;
        span.minimumEditGeneration = m_EditGeneration;
      }
    InvalidateAliases(found->first, resource.aliasGroup, m_EditGeneration);
    Coalesce(resource);
  }
  return m_EditGeneration;
}

bool ReplayResourceAccessIndex::Register(ResourceId original, uint32_t subresource, uint64_t extent,
                                         bool aliasUncertain)
{
  if(m_Finalized || m_Invalidated)
  {
    InvalidateForEdit();
    return false;
  }
  if(m_InvalidInput)
    return false;
  const Key key = {original, subresource};
  if(original == ResourceId() || extent == 0 || m_Resources.size() >= m_MaxResources ||
     m_Resources.find(key) != m_Resources.end())
  {
    m_InvalidInput = true;
    return false;
  }
  Resource &resource = m_Resources[key];
  resource.extent = extent;
  resource.aliasUncertain = aliasUncertain;
  return true;
}

bool ReplayResourceAccessIndex::ValidRange(const ReplayResourceRange &range) const
{
  const auto resource = m_Resources.find({range.resource, range.subresource});
  return resource != m_Resources.end() && range.size > 0 && range.offset < resource->second.extent &&
         range.size <= resource->second.extent - range.offset;
}

bool ReplayResourceAccessIndex::Record(uint32_t eventId, const ReplayResourceRange &range,
                                       ReplayResourceAccessType type, bool complete)
{
  if(m_Finalized || m_Invalidated)
  {
    InvalidateForEdit();
    return false;
  }
  if(m_InvalidInput)
    return false;
  if(eventId == 0 || !ValidRange(range) ||
     (type != ReplayResourceAccessType::Read && type != ReplayResourceAccessType::Write &&
      type != ReplayResourceAccessType::ReadWrite))
  {
    RecordUnknown(eventId);
    return false;
  }
  if(!ReserveAccessEntries(complete ? 1 : 2))
    return false;
  if(!complete)
    m_UnknownEvents.push_back(eventId);
  Access access;
  access.range = range;
  access.eventId = eventId;
  access.type = type;
  m_Accesses.push_back(access);
  return true;
}

void ReplayResourceAccessIndex::RecordUnknown(uint32_t eventId)
{
  if(m_Finalized || m_Invalidated)
  {
    InvalidateForEdit();
    return;
  }
  if(m_InvalidInput)
    return;
  if(eventId == 0)
    m_InvalidInput = true;
  else if(ReserveAccessEntries(1))
    m_UnknownEvents.push_back(eventId);
}

bool ReplayResourceAccessIndex::ReserveAccessEntries(size_t count)
{
  if(m_InvalidInput || count > m_MaxAccessEntries - m_AccessEntries)
  {
    m_InvalidInput = true;
    return false;
  }
  m_AccessEntries += count;
  return true;
}

bool ReplayResourceAccessIndex::SpendWork(size_t &remaining)
{
  if(remaining == 0)
    return false;
  remaining--;
  return true;
}

bool ReplayResourceAccessIndex::Finalize(uint32_t firstEvent, uint32_t lastEvent, bool complete)
{
  return FinalizeAccess(firstEvent, lastEvent, complete, complete);
}

bool ReplayResourceAccessIndex::FinalizeReads(uint32_t firstEvent, uint32_t lastEvent, bool complete)
{
  return FinalizeAccess(firstEvent, lastEvent, complete, false);
}

bool ReplayResourceAccessIndex::FinalizeAccess(uint32_t firstEvent, uint32_t lastEvent, bool reads,
                                               bool writes)
{
  if(m_Finalized || m_Invalidated)
    return false;
  if(firstEvent > lastEvent)
    m_InvalidInput = true;
  for(const Access &access : m_Accesses)
    if(access.eventId < firstEvent || access.eventId > lastEvent)
      m_InvalidInput = true;
  for(uint32_t eventId : m_UnknownEvents)
    if(eventId < firstEvent || eventId > lastEvent)
      m_InvalidInput = true;
  if(m_InvalidInput)
    return false;

  if(m_Accesses.size() > 1)
    std::sort(m_Accesses.begin(), m_Accesses.end(),
              [](const Access &a, const Access &b) { return a.eventId < b.eventId; });
  if(m_UnknownEvents.size() > 1)
  {
    std::sort(m_UnknownEvents.begin(), m_UnknownEvents.end());
    m_UnknownEvents.resize(std::unique(m_UnknownEvents.begin(), m_UnknownEvents.end()) -
                           m_UnknownEvents.begin());
  }
  for(size_t i = 0; i < m_Accesses.size(); i++)
  {
    const Access &access = m_Accesses[i];
    if(access.type != ReplayResourceAccessType::Write)
      m_Resources[{access.range.resource, access.range.subresource}].reads.push_back(i);
    if(access.type != ReplayResourceAccessType::Read)
    {
      m_Writes.push_back(i);
      m_EventWrites[access.eventId].push_back(i);
    }
  }
  bool aliasesKnown = true;
  for(const auto &resource : m_Resources)
    aliasesKnown &= !resource.second.aliasUncertain;
  m_FirstEvent = firstEvent;
  m_LastEvent = lastEvent;
  m_ReadsComplete = reads && aliasesKnown;
  m_WritesComplete = writes && aliasesKnown;
  m_Finalized = true;
  return true;
}

bool ReplayResourceAccessIndex::Ready(uint32_t firstEvent, uint32_t lastEvent) const
{
  return m_Finalized && !m_Invalidated && !m_InvalidInput && firstEvent <= lastEvent &&
         firstEvent >= m_FirstEvent && lastEvent <= m_LastEvent;
}

bool ReplayResourceAccessIndex::Complete(uint32_t firstEvent, uint32_t lastEvent, bool writes) const
{
  if(!Ready(firstEvent, lastEvent) || !(writes ? m_WritesComplete : m_ReadsComplete))
    return false;
  if(m_UnknownEvents.empty())
    return true;
  const auto unknown = std::lower_bound(m_UnknownEvents.begin(), m_UnknownEvents.end(), firstEvent);
  return unknown == m_UnknownEvents.end() || *unknown > lastEvent;
}

bool ReplayResourceAccessIndex::LowerEvent(const rdcarray<size_t> &indices, uint32_t eventId,
                                           size_t &position, size_t &work) const
{
  size_t first = 0, count = indices.size();
  while(count > 0)
  {
    if(!SpendWork(work))
      return false;
    const size_t step = count / 2, middle = first + step;
    if(m_Accesses[indices[middle]].eventId < eventId)
    {
      first = middle + 1;
      count -= step + 1;
    }
    else
      count = step;
  }
  position = first;
  return true;
}

bool ReplayResourceAccessIndex::AddRange(RangeMap &ranges, const ReplayResourceRange &range,
                                         size_t &work, bool &added)
{
  added = false;
  if(!SpendWork(work))
    return false;
  rdcarray<ReplayResourceRange> &existing = ranges[{range.resource, range.subresource}];
  uint64_t begin = range.offset, end = range.offset + range.size;
  for(const ReplayResourceRange &entry : existing)
  {
    if(!SpendWork(work))
      return false;
    if(entry.offset <= begin && entry.offset + entry.size >= end)
      return true;
  }

  rdcarray<ReplayResourceRange> merged;
  bool inserted = false;
  for(const ReplayResourceRange &entry : existing)
  {
    if(!SpendWork(work))
      return false;
    if(entry.offset + entry.size < begin)
      merged.push_back(entry);
    else if(entry.offset > end)
    {
      if(!inserted)
      {
        merged.push_back({range.resource, range.subresource, begin, end - begin});
        inserted = true;
      }
      merged.push_back(entry);
    }
    else
    {
      begin = RDCMIN(begin, entry.offset);
      end = RDCMAX(end, entry.offset + entry.size);
    }
  }
  if(!inserted)
    merged.push_back({range.resource, range.subresource, begin, end - begin});
  existing.swap(merged);
  added = true;
  return true;
}

bool ReplayResourceAccessIndex::Flatten(const RangeMap &ranges,
                                        rdcarray<ReplayResourceRange> &result, size_t &work)
{
  for(const auto &resource : ranges)
    for(const ReplayResourceRange &range : resource.second)
    {
      if(!SpendWork(work))
        return false;
      result.push_back(range);
    }
  return true;
}

ReplayResourceWriteQuery ReplayResourceAccessIndex::QueryWrites(uint32_t firstEvent,
                                                                uint32_t lastEvent) const
{
  ReplayResourceWriteQuery result;
  result.generation = m_Generation;
  size_t work = m_MaxQueryWork, position = 0;
  if(!Ready(firstEvent, lastEvent) || !SpendWork(work) ||
     !LowerEvent(m_Writes, firstEvent, position, work))
    return result;
  RangeMap ranges;
  for(size_t i = position; i < m_Writes.size(); i++)
  {
    if(!SpendWork(work))
      return result;
    const Access &access = m_Accesses[m_Writes[i]];
    if(access.eventId > lastEvent)
      break;
    bool added = false;
    if(!AddRange(ranges, access.range, work, added))
      return result;
  }
  if(!Flatten(ranges, result.ranges, work))
  {
    result.ranges.clear();
    return result;
  }
  result.complete = Complete(firstEvent, lastEvent, true);
  return result;
}

ReplayResourceReadQuery ReplayResourceAccessIndex::FirstRead(const ReplayResourceRange &range,
                                                             uint32_t firstEvent,
                                                             uint32_t lastEvent) const
{
  ReplayResourceReadQuery result;
  result.generation = m_Generation;
  size_t work = m_MaxQueryWork, position = 0;
  if(!Ready(firstEvent, lastEvent) || !SpendWork(work) || !ValidRange(range))
    return result;
  const auto &reads = m_Resources.find({range.resource, range.subresource})->second.reads;
  if(!LowerEvent(reads, firstEvent, position, work))
    return result;
  for(size_t i = position; i < reads.size(); i++)
  {
    if(!SpendWork(work))
      return result;
    const Access &access = m_Accesses[reads[i]];
    if(access.eventId > lastEvent)
      break;
    if(access.range.offset < range.offset + range.size &&
       range.offset < access.range.offset + access.range.size)
    {
      result.eventId = access.eventId;
      result.found = true;
      break;
    }
  }
  result.complete = Complete(firstEvent, lastEvent, false);
  return result;
}

bool ReplayResourceAccessIndex::QueueReads(const ReplayResourceRange &range, uint32_t firstEvent,
                                           uint32_t lastEvent, std::set<uint32_t> &pending,
                                           size_t &work) const
{
  const auto &reads = m_Resources.find({range.resource, range.subresource})->second.reads;
  size_t position = 0;
  if(!LowerEvent(reads, firstEvent, position, work))
    return false;
  for(size_t i = position; i < reads.size(); i++)
  {
    if(!SpendWork(work))
      return false;
    const Access &access = m_Accesses[reads[i]];
    if(access.eventId > lastEvent)
      break;
    if(access.range.offset < range.offset + range.size &&
       range.offset < access.range.offset + access.range.size)
      pending.insert(access.eventId);
  }
  return true;
}

ReplayResourceEditQuery ReplayResourceAccessIndex::PropagateEdit(
    const rdcarray<ReplayResourceRange> &inputs, uint32_t firstEvent, uint32_t lastEvent) const
{
  ReplayResourceEditQuery result;
  result.generation = m_Generation;
  size_t work = m_MaxQueryWork;
  if(!Ready(firstEvent, lastEvent) || !SpendWork(work))
    return result;
  for(const ReplayResourceRange &range : inputs)
    if(!SpendWork(work) || !ValidRange(range))
      return result;
  RangeMap affected;
  std::set<uint32_t> pending;
  rdcarray<uint32_t> events;
  for(const ReplayResourceRange &range : inputs)
  {
    bool added = false;
    if(!AddRange(affected, range, work, added) ||
       (added && !QueueReads(range, firstEvent, lastEvent, pending, work)))
      return result;
  }

  while(!pending.empty())
  {
    if(!SpendWork(work))
      return result;
    const uint32_t eventId = *pending.begin();
    pending.erase(pending.begin());
    events.push_back(eventId);
    const auto writes = m_EventWrites.find(eventId);
    if(writes == m_EventWrites.end())
      continue;
    for(size_t accessIndex : writes->second)
    {
      if(!SpendWork(work))
        return result;
      const ReplayResourceRange &range = m_Accesses[accessIndex].range;
      // This EID has already consumed its inputs. New output contents can only be read by a
      // later EID, even when source serialization happened to record a write before its reads.
      bool added = false;
      if(!AddRange(affected, range, work, added) ||
         (added && eventId < lastEvent && !QueueReads(range, eventId + 1, lastEvent, pending, work)))
        return result;
    }
  }
  if(!Flatten(affected, result.affected, work))
  {
    result.affected.clear();
    return result;
  }
  result.events.swap(events);
  result.complete = Complete(firstEvent, lastEvent, false) && Complete(firstEvent, lastEvent, true);
  return result;
}

void ReplayResourceAccessIndex::InvalidateForEdit()
{
  m_Generation++;
  m_Invalidated = true;
  m_ReadsComplete = m_WritesComplete = false;
}

void ReplayResourceAccessIndex::Clear()
{
  m_Resources.clear();
  m_Accesses.clear();
  m_Writes.clear();
  m_EventWrites.clear();
  m_UnknownEvents.clear();
  m_AccessEntries = 0;
  m_Generation++;
  m_FirstEvent = m_LastEvent = 0;
  m_Finalized = m_Invalidated = m_InvalidInput = false;
  m_ReadsComplete = m_WritesComplete = false;
}

#if ENABLED(ENABLE_UNIT_TESTS)

#include "catch/catch.hpp"

namespace
{
ReplayResourceRange VersionTestRange(ResourceId id, uint64_t offset = 0, uint64_t size = 64,
                                     uint32_t subresource = 0)
{
  return {id, subresource, offset, size};
}

ReplayResourceAccessProof VersionTestProof(ReplayResourceFirstAccess access)
{
  ReplayResourceAccessProof proof;
  proof.firstAccess = access;
  proof.complete = true;
  proof.aliasUncertain = false;
  return proof;
}
}

TEST_CASE("Replay version policy requires complete evidence for deferred restoration",
          "[replay][checkpoint][resourceversions]")
{
  CHECK(ToStr(ReplayResourceRestoreAction::Restore) == "Restore");
  CHECK(ToStr(ReplayResourceFirstAccess::FullOverwrite) == "FullOverwrite");
  const ReplayResourceVersion unknown, initial = {1, 0}, changed = {2, 0}, edited = {1, 1};
  ReplayResourceAccessProof proof;
  CHECK(PlanReplayResourceRestore(initial, initial, 0, proof) == ReplayResourceRestoreAction::Keep);
  CHECK(PlanReplayResourceRestore(unknown, unknown, 0, proof) == ReplayResourceRestoreAction::Restore);
  CHECK(PlanReplayResourceRestore(changed, initial, 0, proof) == ReplayResourceRestoreAction::Restore);
  CHECK(PlanReplayResourceRestore(initial, edited, 0, proof) == ReplayResourceRestoreAction::Restore);
  const ReplayResourceFirstAccess accesses[] = {
      ReplayResourceFirstAccess::Unknown, ReplayResourceFirstAccess::Read,
      ReplayResourceFirstAccess::FullOverwrite, ReplayResourceFirstAccess::Unused};
  for(ReplayResourceFirstAccess access : accesses)
  {
    proof.firstAccess = access;
    proof.complete = false;
    proof.aliasUncertain = false;
    CHECK(PlanReplayResourceRestore(changed, initial, 0, proof) ==
          ReplayResourceRestoreAction::Restore);
    proof.complete = true;
    proof.aliasUncertain = true;
    CHECK(PlanReplayResourceRestore(changed, initial, 0, proof) ==
          ReplayResourceRestoreAction::Restore);
    proof.aliasUncertain = false;
    const bool defer = access == ReplayResourceFirstAccess::FullOverwrite ||
                       access == ReplayResourceFirstAccess::Unused;
    CHECK(PlanReplayResourceRestore(changed, initial, 0, proof) ==
          (defer ? ReplayResourceRestoreAction::Defer : ReplayResourceRestoreAction::Restore));
    // An edit invalidates old source content even when no content copy would otherwise be needed.
    CHECK(PlanReplayResourceRestore(initial, initial, 1, proof) ==
          ReplayResourceRestoreAction::Rebuild);
    CHECK(PlanReplayResourceRestore(unknown, unknown, 0, proof) ==
          ReplayResourceRestoreAction::Restore);
  }
}

TEST_CASE("Deferred replay debt survives another stage and materializes for inspection",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId other = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0}, drawOutput = {2, 0};
  const auto tex = VersionTestRange(texture, 0, 1);
  const auto unrelated = VersionTestRange(other);
  REQUIRE(versions.Register(texture, 0, 1));
  REQUIRE(versions.Register(other, 0, 64));
  REQUIRE(versions.MarkResident(tex, drawOutput));
  auto plan = versions.Plan(tex, initial, VersionTestProof(ReplayResourceFirstAccess::Unused));
  REQUIRE(plan.size() == 1);
  CHECK(plan[0].action == ReplayResourceRestoreAction::Defer);
  REQUIRE(versions.Pending().size() == 1);
  // Replaying/restoring an unrelated unit cannot make the deferred texture valid.
  REQUIRE(versions.MarkResident(unrelated, initial));
  CHECK(versions.Pending().size() == 1);
  CHECK_FALSE(versions.IsResident(tex, initial));
  CHECK(versions.IsResident(tex, drawOutput));
  plan = versions.Plan(tex, initial, VersionTestProof(ReplayResourceFirstAccess::Read));
  REQUIRE(plan.size() == 1);
  CHECK(plan[0].action == ReplayResourceRestoreAction::Restore);
  CHECK(versions.Pending().size() == 1);
  REQUIRE(versions.MarkResident(tex, initial));
  CHECK(versions.Pending().empty());
  CHECK(versions.IsResident(tex, initial));
  CHECK(versions.Plan(tex, initial)[0].action == ReplayResourceRestoreAction::Keep);
}

TEST_CASE("Replay range versions preserve untouched CBuffer bytes and deferred intervals",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0}, changed = {2, 0};
  const auto whole = VersionTestRange(buffer);
  REQUIRE(versions.Register(buffer, 0, 64));
  REQUIRE(versions.MarkResident(whole, changed));
  REQUIRE(versions.Plan(whole, initial, VersionTestProof(ReplayResourceFirstAccess::FullOverwrite))
              .size() == 1);
  // A partial write is not a whole-buffer overwrite: both sides remain owed.
  REQUIRE(versions.MarkResident(VersionTestRange(buffer, 16, 16), initial));
  const auto pending = versions.Pending();
  REQUIRE(pending.size() == 2);
  CHECK(pending[0].offset == 0);
  CHECK(pending[0].size == 16);
  CHECK(pending[1].offset == 32);
  CHECK(pending[1].size == 32);
  const auto plan = versions.Plan(whole, initial, VersionTestProof(ReplayResourceFirstAccess::Read));
  REQUIRE(plan.size() == 3);
  CHECK(plan[0].action == ReplayResourceRestoreAction::Restore);
  CHECK(plan[1].action == ReplayResourceRestoreAction::Keep);
  CHECK(plan[2].action == ReplayResourceRestoreAction::Restore);
  REQUIRE(versions.MarkResident(VersionTestRange(buffer, 0, 16), initial));
  REQUIRE(versions.MarkResident(VersionTestRange(buffer, 32, 32), initial));
  CHECK(versions.Pending().empty());
  CHECK(versions.IsResident(whole, initial));
  REQUIRE(versions.Plan(whole, initial).size() == 1);
}

TEST_CASE("Checkpoint edits invalidate exact physical ranges and reject stale saved versions",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ResourceId asset = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0};
  const auto whole = VersionTestRange(buffer);
  const auto bytes = VersionTestRange(buffer, 20, 4);
  const auto assetRange = VersionTestRange(asset);
  REQUIRE(versions.Register(buffer, 0, 64, true));
  REQUIRE(versions.Register(asset, 0, 64, true));
  REQUIRE(versions.MarkResident(whole, initial));
  REQUIRE(versions.MarkResident(assetRange, initial));
  const uint64_t generation = versions.InvalidateForEdit({bytes}, true);
  CHECK(generation == 1);
  CHECK(versions.EditGeneration() == generation);
  CHECK(versions.IsResident(VersionTestRange(buffer, 0, 20), initial));
  CHECK(versions.IsResident(VersionTestRange(buffer, 24, 40), initial));
  CHECK(versions.IsResident(assetRange, initial));
  CHECK_FALSE(versions.IsResident(bytes, initial));
  CHECK(versions.Plan(bytes, initial)[0].action == ReplayResourceRestoreAction::Rebuild);
  CHECK_FALSE(versions.MarkResident(bytes, initial));
  const ReplayResourceVersion patched = {2, generation};
  CHECK(versions.Plan(bytes, patched)[0].action == ReplayResourceRestoreAction::Restore);
  REQUIRE(versions.MarkResident(bytes, patched));
  CHECK(versions.IsResident(bytes, patched));
  CHECK(versions.Pending().empty());
  // Undo is another edit generation, not permission to install a stale physical version.
  const uint64_t undo = versions.InvalidateForEdit({bytes}, true);
  CHECK(undo == 2);
  CHECK_FALSE(versions.MarkResident(bytes, initial));
  REQUIRE(versions.MarkResident(bytes, {initial.content, undo}));
  CHECK(versions.IsResident(bytes, {initial.content, undo}));
}

TEST_CASE("Unknown shader dependencies invalidate mutable outputs while preserving proven assets",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId output = ResourceIDGen::GetNewUniqueID();
  const ResourceId edited = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0};
  REQUIRE(versions.Register(texture, 0, 64, true));
  REQUIRE(versions.Register(output, 0, 64));
  REQUIRE(versions.Register(edited, 0, 64, true));
  REQUIRE(versions.MarkResident(VersionTestRange(texture), initial));
  REQUIRE(versions.MarkResident(VersionTestRange(output), initial));
  REQUIRE(versions.MarkResident(VersionTestRange(edited), initial));
  CHECK(versions.InvalidateForEdit({VersionTestRange(edited, 8, 4)}, false) == 1);
  CHECK(versions.IsResident(VersionTestRange(texture), initial));
  CHECK_FALSE(versions.IsResident(VersionTestRange(output), initial));
  CHECK(versions.IsResident(VersionTestRange(edited, 0, 8), initial));
  CHECK_FALSE(versions.IsResident(VersionTestRange(edited, 8, 4), initial));
  CHECK(versions.Plan(VersionTestRange(output), initial)[0].action ==
        ReplayResourceRestoreAction::Rebuild);
  // Invalid explicit closure data must fail closed, even if the caller labelled it complete.
  REQUIRE(versions.MarkResident(VersionTestRange(output), {2, 1}));
  CHECK(versions.InvalidateForEdit({VersionTestRange(output, UINT64_MAX, 2)}, true) == 2);
  CHECK_FALSE(versions.IsResident(VersionTestRange(output), {2, 1}));
  CHECK(versions.IsResident(VersionTestRange(texture), initial));
}

TEST_CASE("Replay alias writes invalidate peer views and texture planes remain independent",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId depth = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0};
  REQUIRE(versions.Register(a, 0, 64, false, heap));
  REQUIRE(versions.Register(b, 0, 64, true, heap));
  REQUIRE(versions.Register(depth, 0, 1));
  REQUIRE(versions.Register(depth, 1, 1));
  REQUIRE(versions.MarkResident(VersionTestRange(a), initial));
  REQUIRE(versions.MarkResident(VersionTestRange(b), initial));
  CHECK_FALSE(versions.IsResident(VersionTestRange(a), initial));
  CHECK(versions.IsResident(VersionTestRange(b), initial));
  CHECK(versions.InvalidateForEdit({VersionTestRange(a, 4, 8)}, true) == 1);
  CHECK_FALSE(versions.IsResident(VersionTestRange(b), initial));
  CHECK(versions.Plan(VersionTestRange(b), initial)[0].action == ReplayResourceRestoreAction::Rebuild);
  const auto plane0 = VersionTestRange(depth, 0, 1, 0);
  const auto plane1 = VersionTestRange(depth, 0, 1, 1);
  REQUIRE(versions.MarkResident(plane0, initial));
  REQUIRE(versions.MarkResident(plane1, initial));
  REQUIRE(versions.Invalidate(plane0));
  CHECK_FALSE(versions.IsResident(plane0, initial));
  CHECK(versions.IsResident(plane1, initial));
}

TEST_CASE("Replay version ledger rejects invalid ranges without inventing resident bytes",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId id = ResourceIDGen::GetNewUniqueID();
  CHECK_FALSE(versions.Register(ResourceId(), 0, 64));
  CHECK_FALSE(versions.Register(id, 0, 0));
  REQUIRE(versions.Register(id, 0, 64));
  CHECK_FALSE(versions.Register(id, 0, 64));
  const ReplayResourceVersion initial = {1, 0};
  REQUIRE(versions.MarkResident(VersionTestRange(id), initial));
  const ReplayResourceRange invalid[] = {
      VersionTestRange(id, 0, 0),          VersionTestRange(id, 64, 1),
      VersionTestRange(id, UINT64_MAX, 2), VersionTestRange(id, 1, UINT64_MAX),
      VersionTestRange(id, 0, 64, 1),      VersionTestRange(ResourceId())};
  for(const auto &range : invalid)
  {
    CHECK_FALSE(versions.MarkResident(range, initial));
    CHECK_FALSE(versions.Invalidate(range));
    CHECK_FALSE(versions.IsResident(range, initial));
    const auto plan =
        versions.Plan(range, initial, VersionTestProof(ReplayResourceFirstAccess::Unused));
    REQUIRE(plan.size() == 1);
    CHECK(plan[0].action == ReplayResourceRestoreAction::Restore);
    CHECK(versions.IsResident(VersionTestRange(id), initial));
    CHECK(versions.Pending().empty());
  }
  CHECK_FALSE(versions.MarkResident(VersionTestRange(id), {}));
  versions.InvalidateAll();
  CHECK_FALSE(versions.IsResident(VersionTestRange(id), initial));
  CHECK(versions.Pending().size() == 1);
  versions.Clear();
  CHECK(versions.Pending().empty());
  CHECK(versions.EditGeneration() == 0);
  CHECK_FALSE(versions.IsResident(VersionTestRange(id), initial));
}

TEST_CASE("Late resource registration and excessive range fragmentation fail closed",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId edited = ResourceIDGen::GetNewUniqueID();
  const ResourceId late = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0};
  REQUIRE(versions.Register(edited, 0, 64));
  REQUIRE(versions.MarkResident(VersionTestRange(edited), initial));
  CHECK(versions.InvalidateForEdit({VersionTestRange(edited)}, true) == 1);
  REQUIRE(versions.Register(late, 0, 64, true));
  CHECK_FALSE(versions.MarkResident(VersionTestRange(late), initial));
  CHECK(versions.Plan(VersionTestRange(late), initial)[0].action ==
        ReplayResourceRestoreAction::Rebuild);

  versions.Clear();
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  REQUIRE(versions.Register(buffer, 0, 16384));
  REQUIRE(versions.MarkResident(VersionTestRange(buffer, 0, 16384), initial));
  bool completed = true;
  for(uint64_t i = 0; i < 2050; i++)
    completed &= versions.MarkResident(VersionTestRange(buffer, i * 2, 1), {2, 0});
  REQUIRE(completed);
  // The metadata cap deliberately loses knowledge instead of assuming all fragmented bytes valid.
  CHECK_FALSE(versions.IsResident(VersionTestRange(buffer, 8192, 1), initial));
  CHECK_FALSE(versions.Pending().empty());
  const auto plan = versions.Plan(VersionTestRange(buffer, 8192, 1), initial);
  REQUIRE(plan.size() == 1);
  CHECK(plan[0].action == ReplayResourceRestoreAction::Restore);
}

TEST_CASE("Replay access index separates byte ranges, subresources and inclusive event intervals",
          "[replay][checkpoint][resourceaccess]")
{
  ReplayResourceAccessIndex index;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  REQUIRE(index.Register(buffer, 0, 128, false));
  REQUIRE(index.Register(texture, 0, 1, false));
  REQUIRE(index.Register(texture, 1, 1, false));
  // Loading callers may encounter baked lists out of global submission order.
  REQUIRE(index.RecordWrite(20, VersionTestRange(buffer, 24, 8), true));
  REQUIRE(index.RecordRead(18, VersionTestRange(buffer, 64, 16), true));
  REQUIRE(index.RecordWrite(10, VersionTestRange(buffer, 16, 8), true));
  REQUIRE(index.RecordRead(12, VersionTestRange(buffer, 16, 16), true));
  REQUIRE(index.RecordWrite(25, VersionTestRange(texture, 0, 1, 1), true));
  REQUIRE(index.RecordRead(10, VersionTestRange(texture, 0, 1, 0), true));
  REQUIRE(index.RecordWrite(30, VersionTestRange(buffer, 96, 8), true));
  REQUIRE(index.Finalize(0, 40, true));

  auto writes = index.QueryWrites(10, 20);
  REQUIRE(writes.complete);
  REQUIRE(writes.ranges.size() == 1);
  CHECK(writes.ranges[0].resource == buffer);
  CHECK(writes.ranges[0].offset == 16);
  CHECK(writes.ranges[0].size == 16);
  writes = index.QueryWrites(11, 20);
  REQUIRE(writes.ranges.size() == 1);
  CHECK(writes.ranges[0].offset == 24);
  CHECK(writes.ranges[0].size == 8);
  writes = index.QueryWrites(25, 25);
  REQUIRE(writes.complete);
  REQUIRE(writes.ranges.size() == 1);
  CHECK(writes.ranges[0].resource == texture);
  CHECK(writes.ranges[0].subresource == 1);

  auto read = index.FirstRead(VersionTestRange(buffer, 24, 8), 12, 12);
  CHECK(read.complete);
  CHECK(read.found);
  CHECK(read.eventId == 12);
  read = index.FirstRead(VersionTestRange(buffer, 32, 32), 0, 40);
  CHECK(read.complete);
  CHECK_FALSE(read.found);    // [32, 64) touches, but does not overlap either read.
  read = index.FirstRead(VersionTestRange(buffer, 16, 64), 13, 40);
  CHECK(read.found);
  CHECK(read.eventId == 18);
  CHECK_FALSE(index.FirstRead(VersionTestRange(texture, 0, 1, 1), 0, 40).found);
  CHECK_FALSE(index.QueryWrites(0, 41).complete);
  CHECK_FALSE(index.QueryWrites(20, 10).complete);
  CHECK_FALSE(index.FirstRead(VersionTestRange(buffer), 0, 41).complete);
}

TEST_CASE("Missing accesses and uncertain aliases never imply complete static evidence",
          "[replay][checkpoint][resourceaccess]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  ReplayResourceAccessIndex absent;
  REQUIRE(absent.Register(buffer, 0, 64, false));
  REQUIRE(absent.Finalize(0, 20));
  CHECK_FALSE(absent.QueryWrites(0, 20).complete);
  const auto noRead = absent.FirstRead(VersionTestRange(buffer), 0, 20);
  CHECK_FALSE(noRead.found);
  CHECK_FALSE(noRead.complete);
  CHECK_FALSE(absent.PropagateEdit({VersionTestRange(buffer)}, 0, 20).complete);

  ReplayResourceAccessIndex aliases;
  REQUIRE(aliases.Register(buffer, 0, 64));
  REQUIRE(aliases.RecordRead(2, VersionTestRange(buffer), true));
  REQUIRE(aliases.Finalize(0, 20, true));
  CHECK(aliases.FirstRead(VersionTestRange(buffer), 0, 20).found);
  CHECK_FALSE(aliases.FirstRead(VersionTestRange(buffer), 0, 20).complete);
  CHECK_FALSE(aliases.QueryWrites(3, 20).complete);

  ReplayResourceAccessIndex unknown;
  REQUIRE(unknown.Register(buffer, 0, 64, false));
  REQUIRE(unknown.RecordRead(5, VersionTestRange(buffer, 0, 16), true));
  unknown.RecordUnknown(8);
  unknown.RecordUnknown(8);
  REQUIRE(unknown.RecordWrite(12, VersionTestRange(buffer, 32, 16)));
  REQUIRE(unknown.Finalize(0, 20, true));
  CHECK(unknown.FirstRead(VersionTestRange(buffer), 0, 7).complete);
  CHECK_FALSE(unknown.FirstRead(VersionTestRange(buffer), 0, 8).complete);
  CHECK(unknown.QueryWrites(9, 11).complete);
  CHECK_FALSE(unknown.QueryWrites(12, 12).complete);
  CHECK(unknown.QueryWrites(13, 20).complete);
  const auto propagation = unknown.PropagateEdit({VersionTestRange(buffer, 0, 16)}, 0, 20);
  CHECK_FALSE(propagation.complete);
  CHECK_FALSE(propagation.affected.empty());    // Partial positive evidence remains diagnostic.
}

TEST_CASE("Read coverage alone cannot prove restoration writes or transitive outputs",
          "[replay][checkpoint][resourceaccess]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  ReplayResourceAccessIndex readsOnly;
  REQUIRE(readsOnly.Register(buffer, 0, 256, false));
  REQUIRE(readsOnly.RecordRead(10, VersionTestRange(buffer, 64, 64), true));
  REQUIRE(readsOnly.RecordWrite(15, VersionTestRange(buffer, 0, 64), true));
  REQUIRE(readsOnly.FinalizeReads(0, 20, true));
  auto read = readsOnly.FirstRead(VersionTestRange(buffer, 80, 4), 0, 20);
  CHECK(read.complete);
  CHECK(read.found);
  CHECK(read.eventId == 10);
  CHECK_FALSE(readsOnly.QueryWrites(0, 20).complete);
  CHECK_FALSE(readsOnly.PropagateEdit({VersionTestRange(buffer, 80, 4)}, 0, 20).complete);
  CHECK_FALSE(readsOnly.Finalize(0, 20, true));
  CHECK_FALSE(readsOnly.QueryWrites(0, 20).complete);
}

TEST_CASE("Edits propagate forward by overlapping reads and retain conservative dependencies",
          "[replay][checkpoint][resourceaccess]")
{
  ReplayResourceAccessIndex index;
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  const ResourceId c = ResourceIDGen::GetNewUniqueID();
  const ResourceId earlier = ResourceIDGen::GetNewUniqueID();
  const ResourceId unrelated = ResourceIDGen::GetNewUniqueID();
  for(ResourceId resource : {a, b, c, earlier, unrelated})
    REQUIRE(index.Register(resource, 0, 64, false));

  // A write encountered first in the source metadata still belongs after this EID's reads.
  REQUIRE(index.RecordWrite(5, VersionTestRange(b, 16, 16), true));
  REQUIRE(index.RecordRead(5, VersionTestRange(a, 4, 4), true));
  REQUIRE(index.RecordRead(3, VersionTestRange(b, 16, 16), true));
  REQUIRE(index.RecordWrite(3, VersionTestRange(earlier), true));
  REQUIRE(index.RecordRead(6, VersionTestRange(a, 32, 16), true));
  REQUIRE(index.RecordWrite(6, VersionTestRange(unrelated), true));
  // There is deliberately no implicit kill: a plain write is not a full-overwrite proof.
  REQUIRE(index.RecordWrite(7, VersionTestRange(b), true));
  REQUIRE(index.RecordRead(9, VersionTestRange(b, 24, 8), true));
  REQUIRE(index.RecordWrite(9, VersionTestRange(c, 0, 16), true));
  REQUIRE(index.Record(10, VersionTestRange(c, 8, 16), ReplayResourceAccessType::ReadWrite, true));
  REQUIRE(index.Finalize(0, 20, true));

  auto edits = index.PropagateEdit({VersionTestRange(a, 4, 4)}, 0, 20);
  CHECK(edits.complete);
  REQUIRE(edits.events.size() == 3);
  CHECK(edits.events[0] == 5);
  CHECK(edits.events[1] == 9);
  CHECK(edits.events[2] == 10);
  REQUIRE(edits.affected.size() == 3);
  CHECK(edits.affected[0].resource == a);
  CHECK(edits.affected[1].resource == b);
  CHECK(edits.affected[1].offset == 16);
  CHECK(edits.affected[1].size == 16);
  CHECK(edits.affected[2].resource == c);
  CHECK(edits.affected[2].offset == 0);
  CHECK(edits.affected[2].size == 24);

  // A downstream resource edited at the interval start affects its earlier consumers too.
  edits = index.PropagateEdit({VersionTestRange(b, 16, 16)}, 0, 20);
  CHECK(edits.complete);
  REQUIRE(edits.events.size() == 3);
  CHECK(edits.events[0] == 3);
  CHECK(edits.events[1] == 9);
  CHECK(edits.events[2] == 10);
  edits = index.PropagateEdit({VersionTestRange(a, 4, 4)}, 6, 20);
  CHECK(edits.complete);
  CHECK(edits.events.empty());
  REQUIRE(edits.affected.size() == 1);
  CHECK(edits.affected[0].resource == a);
}

TEST_CASE("One affected read conservatively reaches all writes within the same global EID",
          "[replay][checkpoint][resourceaccess]")
{
  ReplayResourceAccessIndex index;
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  const ResourceId c = ResourceIDGen::GetNewUniqueID();
  for(ResourceId resource : {a, b, c})
    REQUIRE(index.Register(resource, 0, 64, false));
  REQUIRE(index.RecordWrite(5, VersionTestRange(b, 8, 8), true));
  REQUIRE(index.RecordWrite(5, VersionTestRange(c, 32, 16), true));
  REQUIRE(index.Record(5, VersionTestRange(a, 0, 16), ReplayResourceAccessType::ReadWrite, true));
  REQUIRE(index.Finalize(0, 5, true));
  const auto edits = index.PropagateEdit({VersionTestRange(a, 8, 4)}, 0, 5);
  CHECK(edits.complete);
  REQUIRE(edits.events.size() == 1);
  CHECK(edits.events[0] == 5);
  REQUIRE(edits.affected.size() == 3);
  CHECK(edits.affected[0].resource == a);
  CHECK(edits.affected[0].offset == 0);
  CHECK(edits.affected[0].size == 16);
  CHECK(edits.affected[1].resource == b);
  CHECK(edits.affected[2].resource == c);
}

TEST_CASE("Access propagation coalesces edited ranges without losing newly reached consumers",
          "[replay][checkpoint][resourceaccess]")
{
  ReplayResourceAccessIndex index;
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  REQUIRE(index.Register(a, 0, 64, false));
  REQUIRE(index.Register(b, 0, 64, false));
  REQUIRE(index.RecordRead(1, VersionTestRange(a, 20, 4), true));
  REQUIRE(index.RecordWrite(1, VersionTestRange(b, 0, 8), true));
  REQUIRE(index.RecordRead(2, VersionTestRange(a, 36, 4), true));
  REQUIRE(index.RecordWrite(2, VersionTestRange(b, 16, 8), true));
  REQUIRE(index.RecordRead(3, VersionTestRange(b, 0, 64), true));
  REQUIRE(index.Finalize(0, 3, true));
  const auto edits = index.PropagateEdit(
      {VersionTestRange(a, 0, 8), VersionTestRange(a, 32, 16), VersionTestRange(a, 8, 24)}, 0, 3);
  CHECK(edits.complete);
  REQUIRE(edits.affected.size() == 3);
  CHECK(edits.affected[0].resource == a);
  CHECK(edits.affected[0].offset == 0);
  CHECK(edits.affected[0].size == 48);
  CHECK(edits.affected[1].resource == b);
  CHECK(edits.affected[1].offset == 0);
  CHECK(edits.affected[2].resource == b);
  CHECK(edits.affected[2].offset == 16);
  REQUIRE(edits.events.size() == 3);
  CHECK(edits.events[0] == 1);
  CHECK(edits.events[1] == 2);
  CHECK(edits.events[2] == 3);
}

TEST_CASE("Resource access evidence becomes unusable after edits until rebuilt",
          "[replay][checkpoint][resourceaccess]")
{
  ReplayResourceAccessIndex index;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  REQUIRE(index.Register(buffer, 0, 64, false));
  REQUIRE(index.RecordRead(1, VersionTestRange(buffer), true));
  REQUIRE(index.RecordWrite(2, VersionTestRange(buffer), true));
  REQUIRE(index.Finalize(0, 3, true));
  const auto prior = index.QueryWrites(0, 3);
  REQUIRE(prior.complete);
  index.InvalidateForEdit();
  CHECK(index.Generation() != prior.generation);
  CHECK_FALSE(index.FirstRead(VersionTestRange(buffer), 0, 3).complete);
  CHECK_FALSE(index.QueryWrites(0, 3).complete);
  CHECK_FALSE(index.PropagateEdit({VersionTestRange(buffer)}, 0, 3).complete);
  CHECK_FALSE(index.Finalize(0, 3, true));
  CHECK_FALSE(index.RecordRead(2, VersionTestRange(buffer), true));

  index.Clear();
  REQUIRE(index.Register(buffer, 0, 64, false));
  REQUIRE(index.RecordRead(3, VersionTestRange(buffer, 32, 16), true));
  REQUIRE(index.FinalizeReads(0, 3, true));
  auto read = index.FirstRead(VersionTestRange(buffer, 32, 16), 0, 3);
  CHECK(read.complete);
  CHECK(read.eventId == 3);
  CHECK(read.generation != prior.generation);
  // Even an accidental append to a frozen index must not leave a seemingly valid old proof.
  const uint64_t frozenGeneration = index.Generation();
  CHECK_FALSE(index.RecordWrite(3, VersionTestRange(buffer), true));
  CHECK(index.Generation() != frozenGeneration);
  CHECK_FALSE(index.FinalizeReads(0, 3, true));
  CHECK_FALSE(index.FirstRead(VersionTestRange(buffer), 0, 3).complete);
}

TEST_CASE("Replay access storage limits stop known and unknown metadata until Clear",
          "[replay][checkpoint][resourceaccess]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  ReplayResourceAccessIndex exact(2, 1, 64);
  REQUIRE(exact.Register(a, 0, 64, false));
  REQUIRE(exact.RecordRead(1, VersionTestRange(a), true));
  REQUIRE(exact.RecordWrite(2, VersionTestRange(a), true));
  REQUIRE(exact.Finalize(0, 3, true));
  CHECK(exact.FirstRead(VersionTestRange(a), 0, 3).complete);

  exact.Clear();
  REQUIRE(exact.Register(a, 0, 64, false));
  REQUIRE(exact.RecordRead(1, VersionTestRange(a), true));
  exact.RecordUnknown(2);    // Unknown markers share the same total entry budget.
  exact.RecordUnknown(3);
  for(uint32_t eventId = 4; eventId < 20; eventId++)
  {
    exact.RecordUnknown(eventId);
    CHECK_FALSE(exact.RecordWrite(eventId, VersionTestRange(a), true));
  }
  CHECK_FALSE(exact.Finalize(0, 20, true));
  exact.Clear();
  REQUIRE(exact.Register(a, 0, 64, false));
  CHECK_FALSE(exact.Register(b, 0, 64, false));    // Clear retained the one-resource limit.
  CHECK_FALSE(exact.RecordRead(1, VersionTestRange(a), true));
  CHECK_FALSE(exact.Finalize(0, 3, true));

  ReplayResourceAccessIndex uncertain(1, 1, 64);
  REQUIRE(uncertain.Register(a, 0, 64, false));
  CHECK_FALSE(uncertain.RecordRead(1, VersionTestRange(a)));    // Access plus unknown costs two.
  CHECK_FALSE(uncertain.RecordWrite(2, VersionTestRange(a), true));
  CHECK_FALSE(uncertain.Finalize(0, 3, true));
  ReplayResourceAccessIndex noResources(1, 0, 64);
  CHECK_FALSE(noResources.Register(a, 0, 64, false));
  ReplayResourceAccessIndex noAccesses(0, 1, 64);
  REQUIRE(noAccesses.Register(a, 0, 64, false));
  CHECK_FALSE(noAccesses.RecordRead(1, VersionTestRange(a), true));
}

TEST_CASE("Resource read and write query budgets never publish truncated complete answers",
          "[replay][checkpoint][resourceaccess]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const auto populate = [a](ReplayResourceAccessIndex &index) {
    REQUIRE(index.Register(a, 0, 64, false));
    for(uint32_t eventId = 1; eventId <= 8; eventId++)
    {
      REQUIRE(index.RecordRead(eventId, VersionTestRange(a, (eventId - 1) * 8, 4), true));
      REQUIRE(index.RecordWrite(eventId, VersionTestRange(a, (eventId - 1) * 8, 4), true));
    }
    REQUIRE(index.Finalize(0, 8, true));
  };
  ReplayResourceAccessIndex bounded(16, 1, 6);
  populate(bounded);
  const auto early = bounded.FirstRead(VersionTestRange(a, 0, 4), 0, 8);
  CHECK(early.complete);
  CHECK(early.found);
  const auto late = bounded.FirstRead(VersionTestRange(a, 56, 4), 0, 8);
  CHECK_FALSE(late.complete);
  CHECK_FALSE(late.found);
  CHECK(late.eventId == 0);
  const auto writes = bounded.QueryWrites(0, 8);
  CHECK_FALSE(writes.complete);
  CHECK(writes.ranges.empty());

  ReplayResourceAccessIndex sufficient(16, 1, 128);
  populate(sufficient);
  CHECK(sufficient.FirstRead(VersionTestRange(a, 56, 4), 0, 8).eventId == 8);
  CHECK(sufficient.FirstRead(VersionTestRange(a, 56, 4), 0, 8).complete);
  CHECK(sufficient.QueryWrites(0, 8).complete);
  CHECK(sufficient.QueryWrites(0, 8).ranges.size() == 8);

  ReplayResourceAccessIndex zero(16, 1, 0);
  populate(zero);
  CHECK_FALSE(zero.FirstRead(VersionTestRange(a), 0, 8).found);
  CHECK_FALSE(zero.QueryWrites(0, 0).complete);
  CHECK_FALSE(zero.PropagateEdit({}, 0, 0).complete);
}

TEST_CASE("Transitive access propagation shares one budget across resources and output merging",
          "[replay][checkpoint][resourceaccess]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  const ResourceId c = ResourceIDGen::GetNewUniqueID();
  for(size_t budget : {size_t(10), size_t(18), size_t(64)})
  {
    ReplayResourceAccessIndex index(5, 3, budget);
    for(ResourceId resource : {a, b, c})
      REQUIRE(index.Register(resource, 0, 64, false));
    REQUIRE(index.RecordRead(1, VersionTestRange(a), true));
    REQUIRE(index.RecordWrite(1, VersionTestRange(b), true));
    REQUIRE(index.RecordRead(2, VersionTestRange(b), true));
    REQUIRE(index.RecordWrite(2, VersionTestRange(c), true));
    REQUIRE(index.RecordRead(3, VersionTestRange(c), true));
    REQUIRE(index.Finalize(0, 3, true));
    const auto edits = index.PropagateEdit({VersionTestRange(a)}, 0, 3);
    if(budget < 64)
    {
      CHECK_FALSE(edits.complete);
      CHECK(edits.affected.empty());
      CHECK(edits.events.empty());
    }
    else
    {
      CHECK(edits.complete);
      CHECK(edits.affected.size() == 3);
      CHECK(edits.events.size() == 3);
    }
  }
}

TEST_CASE("Invalid resource access metadata fails closed and maximum EID does not wrap",
          "[replay][checkpoint][resourceaccess]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceRange invalid[] = {
      VersionTestRange(buffer, 0, 0),          VersionTestRange(buffer, 64, 1),
      VersionTestRange(buffer, UINT64_MAX, 2), VersionTestRange(buffer, 1, UINT64_MAX),
      VersionTestRange(buffer, 0, 64, 1),      VersionTestRange(ResourceId())};
  for(const ReplayResourceRange &range : invalid)
  {
    ReplayResourceAccessIndex index;
    REQUIRE(index.Register(buffer, 0, 64, false));
    CHECK_FALSE(index.RecordRead(2, range, true));
    REQUIRE(index.Finalize(0, 3, true));
    CHECK_FALSE(index.FirstRead(VersionTestRange(buffer), 0, 3).complete);
    CHECK_FALSE(index.FirstRead(range, 0, 3).complete);
    CHECK_FALSE(index.PropagateEdit({range}, 0, 3).complete);
  }
  ReplayResourceAccessIndex badRegistration;
  CHECK_FALSE(badRegistration.Register(ResourceId(), 0, 64, false));
  CHECK_FALSE(badRegistration.Finalize(0, 3, true));
  ReplayResourceAccessIndex badEvent;
  REQUIRE(badEvent.Register(buffer, 0, 64, false));
  CHECK_FALSE(badEvent.RecordWrite(0, VersionTestRange(buffer), true));
  CHECK_FALSE(badEvent.Finalize(0, 3, true));
  ReplayResourceAccessIndex outside;
  REQUIRE(outside.Register(buffer, 0, 64, false));
  REQUIRE(outside.RecordWrite(4, VersionTestRange(buffer), true));
  CHECK_FALSE(outside.Finalize(0, 3, true));
  ReplayResourceAccessIndex badType;
  REQUIRE(badType.Register(buffer, 0, 64, false));
  CHECK_FALSE(badType.Record(2, VersionTestRange(buffer), ReplayResourceAccessType(99), true));
  REQUIRE(badType.Finalize(0, 3, true));
  CHECK_FALSE(badType.QueryWrites(0, 3).complete);

  ReplayResourceAccessIndex last;
  REQUIRE(last.Register(buffer, 0, 64, false));
  REQUIRE(
      last.Record(UINT32_MAX, VersionTestRange(buffer), ReplayResourceAccessType::ReadWrite, true));
  REQUIRE(last.Finalize(0, UINT32_MAX, true));
  const auto edits = last.PropagateEdit({VersionTestRange(buffer, 0, 4)}, UINT32_MAX, UINT32_MAX);
  CHECK(edits.complete);
  REQUIRE(edits.events.size() == 1);
  CHECK(edits.events[0] == UINT32_MAX);
  REQUIRE(edits.affected.size() == 1);
  CHECK(edits.affected[0].size == 64);
}

#endif
