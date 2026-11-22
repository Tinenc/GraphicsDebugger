// Once the immutable GPU copy is complete, it can own the initial bytes directly. Keeping its
// upload staging allocation as well spends CPU commit for a second copy that replay never uses.
// This changes only replay-internal sources, not captured resources or their initial state.
static void RetireD3D12InitialUploadDuplicates(WrappedID3D12Device *device,
                                               std::map<ResourceId, ID3D12Resource *> &cache)
{
  if(Process::GetEnvVariable("RENDERDOC_D3D12_KEEP_INITIAL_UPLOAD_DUPLICATES") == "1")
    return;
  uint64_t retiredBytes = 0, sources = 0;
  D3D12ResourceManager *manager = device->GetResourceManager();
  for(ResourceId id : manager->GetInitialContentResourceIDs())
  {
    D3D12InitialContents data = manager->GetInitialContents(id);
    if(!data.resource || data.srcData || data.descriptors || data.sparseBinds || data.sparseTable ||
       data.buildData || data.cachedBuiltAS ||
       (data.resourceType != Resource_Resource && data.resourceType != Resource_Heap) ||
       (data.tag != D3D12InitialContents::Copy && data.tag != D3D12InitialContents::ForceCopy))
      continue;
    ID3D12Resource *source = (ID3D12Resource *)data.resource;
    const auto found = cache.find(GetResID(source));
    if(found == cache.end() || !found->second)
      continue;
    D3D12_HEAP_PROPERTIES properties = {};
    const D3D12_RESOURCE_DESC desc = Unwrap(source)->GetDesc();
    if(desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER ||
       FAILED(Unwrap(source)->GetHeapProperties(&properties, NULL)) ||
       properties.Type != D3D12_HEAP_TYPE_UPLOAD || found->second->GetDesc().Width != desc.Width)
      continue;
    // The caller has drained the warmup copy and all loading lists. Transfer one reference to
    // the new replay-internal wrapper; the cache keeps its original native reference.
    ID3D12Resource *resident = found->second;
    resident->AddRef();
    WrappedID3D12Resource *wrapped =
        new WrappedID3D12Resource(ResourceId(), resident, NULL, 0, device);
    data.resource = wrapped;
    manager->SetInitialContents(id, data);    // releases the now-unneeded upload source
    cache.erase(found);
    cache[GetResID(wrapped)] = resident;
    retiredBytes += desc.Width;
    sources++;
  }
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=D3D12InitialStorage op=retireUploadDuplicates sources=%llu logicalUploadBytes=%llu "
        "newGPUAllocationBytes=0",
        (unsigned long long)sources, (unsigned long long)retiredBytes));
}

#if defined(_MSC_VER) && defined(_M_X64) && !defined(__clang__)
#include <intrin.h>
#include <smmintrin.h>
#endif

static bool D3D12InitialUploadByteCompare(const byte *a, const byte *b, size_t bytes)
{
#if defined(_MSC_VER) && defined(_M_X64) && !defined(__clang__)
  static const bool streaming = []() {
    int registers[4] = {};
    __cpuid(registers, 1);
    return (registers[2] & (1 << 19)) != 0 &&
           Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_UPLOAD_STREAM_COMPARE") != "1";
  }();
  if(streaming && ((uintptr_t(a) | uintptr_t(b)) & 15) == 0)
  {
    // MOVNTDQA is intended for immutable write-combined mappings. Runtime SSE4.1 detection,
    // 16-byte alignment, and a scalar tail keep older CPUs/unaligned sources on a safe path.
    size_t offset = 0;
    for(; bytes - offset >= 16; offset += 16)
    {
      const __m128i x = _mm_stream_load_si128((__m128i *)(a + offset));
      const __m128i y = _mm_stream_load_si128((__m128i *)(b + offset));
      if(_mm_movemask_epi8(_mm_cmpeq_epi8(x, y)) != 0xffff)
        return false;
    }
    return memcmp(a + offset, b + offset, bytes - offset) == 0;
  }
#endif
  return memcmp(a, b, bytes) == 0;
}

template <typename Continue>
static bool D3D12InitialSourceBytesEqual(const byte *a, const byte *b, size_t bytes,
                                         const Continue &mayContinue)
{
  for(size_t offset = 0; offset < bytes;)
  {
    if(!mayContinue())
      return false;
    const size_t count = RDCMIN<size_t>(1024 * 1024, bytes - offset);
    if(!D3D12InitialUploadByteCompare(a + offset, b + offset, count))
      return false;
    offset += count;
  }
  return true;
}

static void DeduplicateD3D12InitialUploadSources(WrappedID3D12Device *device)
{
  if(Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_INITIAL_SOURCE_DEDUP") == "1")
    return;
  PerformanceTimer timer;
  struct Source
  {
    ID3D12Resource *resource;
  };
  D3D12ResourceManager *manager = device->GetResourceManager();
  std::map<uint64_t, rdcarray<Source>> sizes;
  std::map<ID3D12DeviceChild *, size_t> allOwners;
  std::map<ID3D12Resource *, rdcarray<ResourceId>> owners;
  std::set<ID3D12Resource *> visited;
  for(ResourceId id : manager->GetInitialContentResourceIDs())
  {
    const D3D12InitialContents data = manager->GetInitialContents(id);
    if(data.resource)
      allOwners[data.resource]++;
  }
  for(ResourceId id : manager->GetInitialContentResourceIDs())
  {
    const D3D12InitialContents data = manager->GetInitialContents(id);
    if(!data.resource || data.srcData || data.descriptors || data.sparseBinds || data.sparseTable ||
       data.buildData || data.cachedBuiltAS ||
       (data.resourceType != Resource_Resource && data.resourceType != Resource_Heap) ||
       (data.tag != D3D12InitialContents::Copy && data.tag != D3D12InitialContents::ForceCopy))
      continue;
    ID3D12Resource *resource = (ID3D12Resource *)data.resource;
    const D3D12_RESOURCE_DESC desc = Unwrap(resource)->GetDesc();
    D3D12_HEAP_PROPERTIES heap = {};
    if(desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER || desc.Width < 65536 ||
       desc.Width > SIZE_MAX || FAILED(Unwrap(resource)->GetHeapProperties(&heap, NULL)) ||
       heap.Type != D3D12_HEAP_TYPE_UPLOAD)
      continue;
    owners[resource].push_back(id);
    // Each physical source is scanned once; all eligible owners transfer their references.
    if(visited.insert(resource).second)
      sizes[desc.Width].push_back({resource});
  }
  uint64_t scanned = 0, retired = 0, merged = 0, comparisons = 0, sampled = 0;
  bool budgetExhausted = false;
  const auto mayContinue = [&]() {
    budgetExhausted |= timer.GetMilliseconds() >= 5000.0;
    return !budgetExhausted;
  };
  rdcarray<uint64_t> order;
  for(const auto &size : sizes)
    if(size.second.size() > 1)
      order.push_back(size.first);
  std::sort(order.begin(), order.end(), [&](uint64_t a, uint64_t b) {
    return double(a) * double(sizes[a].size() - 1) > double(b) * double(sizes[b].size() - 1);
  });
  const uint64_t workBudget = 16ULL * 1024 * 1024 * 1024;
  const D3D12_RANGE none = {0, 0};
  for(uint64_t width : order)
  {
    if(!mayContinue())
      break;
    std::map<uint64_t, rdcarray<ID3D12Resource *>> hashes;
    for(const Source &source : sizes[width])
    {
      if(!mayContinue())
        break;
      if(owners[source.resource].size() != allOwners[source.resource])
        continue;    // Another owner has sparse/AS/otherwise unclassified metadata.
      if(width > workBudget - scanned)
        break;
      void *mapped = NULL;
      const D3D12_RANGE read = {0, SIZE_T(width)};
      ID3D12Resource *native = Unwrap(source.resource);
      if(FAILED(native->Map(0, &read, &mapped)))
        continue;
      if(!mapped)
      {
        native->Unmap(0, &none);
        continue;
      }
      scanned += width;
      // UPLOAD memory is often write-combined: a full hashing pass can make opening much slower.
      // Three small samples are only a candidate index. Byte-for-byte verification below remains
      // mandatory and checks its wall budget in bounded chunks; a cancelled comparison cannot merge.
      const byte *dataBytes = (const byte *)mapped;
      uint64_t hash = XXH64(dataBytes, 256, 0);
      hash = XXH64(dataBytes + size_t(width / 2 - 128), 256, hash);
      hash = XXH64(dataBytes + size_t(width - 256), 256, hash);
      sampled += 768;
      ID3D12Resource *canonical = NULL;
      auto &candidates = hashes[hash];
      for(ID3D12Resource *candidate : candidates)
      {
        void *other = NULL;
        if(SUCCEEDED(Unwrap(candidate)->Map(0, &read, &other)))
        {
          // Hash equality is only an index. Full immutable byte equality authorises sharing;
          // dimensions/formats/subresource footprints remain in each captured owner's metadata.
          comparisons++;
          const bool equal =
              other && D3D12InitialSourceBytesEqual((const byte *)mapped, (const byte *)other,
                                                    size_t(width), mayContinue);
          Unwrap(candidate)->Unmap(0, &none);
          if(equal)
          {
            canonical = candidate;
            break;
          }
        }
      }
      native->Unmap(0, &none);
      if(!canonical)
      {
        candidates.push_back(source.resource);
        continue;
      }
      // The caller has drained all loading/warmup GPU users. Transfer one initial-content
      // reference before releasing this upload. Captured IDs, resource objects and metadata
      // stay distinct, and the temporary hash index is destroyed before interactive replay.
      for(ResourceId owner : owners[source.resource])
      {
        D3D12InitialContents data = manager->GetInitialContents(owner);
        canonical->AddRef();
        data.resource = canonical;
        manager->SetInitialContents(owner, data);
      }
      retired += width;
      merged++;
    }
  }
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=D3D12InitialStorage op=deduplicateUploadSources candidateBytes=%llu sampledBytes=%llu "
        "retiredBytes=%llu mergedSources=%llu byteComparisons=%llu total_ms=%.3f "
        "budgetExhausted=%u gpuReadbackBytes=0 newGPUAllocationBytes=0 transientIndexRetained=0",
        (unsigned long long)scanned, (unsigned long long)sampled, (unsigned long long)retired,
        (unsigned long long)merged, (unsigned long long)comparisons, timer.GetMilliseconds(),
        budgetExhausted ? 1U : 0U));
}

static uint64_t RetainD3D12CanonicalInitialSources(WrappedID3D12Device *device,
                                                   std::map<ResourceId, ID3D12Resource *> &cache)
{
  std::set<ResourceId> canonical;
  D3D12ResourceManager *manager = device->GetResourceManager();
  for(ResourceId id : manager->GetInitialContentResourceIDs())
  {
    const auto initial = manager->GetInitialContents(id);
    if(initial.resource &&
       (initial.resourceType == Resource_Resource || initial.resourceType == Resource_Heap) &&
       (initial.tag == D3D12InitialContents::Copy || initial.tag == D3D12InitialContents::ForceCopy))
    {
      auto found = cache.find(GetResID(initial.resource));
      if(found != cache.end() && found->second &&
         found->second == Unwrap((ID3D12Resource *)initial.resource))
        canonical.insert(found->first);
    }
  }
  uint64_t bytes = 0;
  for(auto it = cache.begin(); it != cache.end();)
  {
    if(canonical.count(it->first))
    {
      const D3D12_RESOURCE_DESC desc = it->second->GetDesc();
      bytes += device->GetReal()->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
      ++it;
    }
    else
    {
      SAFE_RELEASE(it->second);
      it = cache.erase(it);
    }
  }
  // These allocations now own immutable initial bytes. Preserve their budget accounting
  // across Shader/debug cache cleanup; otherwise later edits could admit an unbounded series.
  return bytes;
}

static void WarmD3D12ParameterReadIndex(WrappedID3D12Device *device)
{
  if(Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_PARAMETER_READ_WARMUP") == "1")
    return;
  PerformanceTimer timer;
  // The expensive descriptor history is immutable capture metadata, independent of values.
  // Prepare one common original constant buffer before the capture becomes interactive. The
  // heap histories then serve other buffer scopes too. No replay, GPU snapshot or readback runs.
  for(const auto &entry : device->GetResourceList())
  {
    if(ResourceIDGen::IsReplayOnlyID(entry.first))
      continue;
    const D3D12_RESOURCE_DESC desc = entry.second->GetReal()->GetDesc();
    if(desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER || !desc.Width ||
       (desc.Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS))
      continue;
    bool constant = false;
    for(const EventUsage &use : device->GetQueue()->GetUsage(entry.first))
      constant |=
          use.usage >= ResourceUsage::VS_Constants && use.usage <= ResourceUsage::All_Constants;
    if(!constant)
      continue;
    uint32_t first = ~0U;
    const bool complete = device->GetQueue()->GetCommandData()->GetBufferPatchRangeConsumer(
        {{entry.first, desc.Width}}, {{entry.first, 0, RDCMIN<uint64_t>(4, desc.Width)}}, first);
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      AppendReplayTimingLog(StringFormat::Fmt(
          "api=D3D12BufferReadIndex op=openingWarmup buffer=%s complete=%u total_ms=%.3f "
          "gpuReadbackBytes=0",
          ToStr(entry.first).c_str(), complete ? 1U : 0U, timer.GetMilliseconds()));
    break;
  }
}
