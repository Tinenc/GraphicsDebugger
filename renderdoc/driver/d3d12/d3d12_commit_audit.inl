#include <psapi.h>
#include "replay/replay_timing.h"

// Diagnostic counter snapshots around driver allocations. These deltas include concurrent
// process activity, so they are attribution evidence, not an exact per-object allocation API.
struct D3D12CommitAudit
{
  const char *kind;
  uint64_t size, before = 0;
  uint32_t heap, dimension;
  ResourceId resource;
  bool enabled;
  bool rangeAudit = false;
  uint64_t minimumRange = 1024 * 1024;
  rdcarray<rdcpair<uintptr_t, uintptr_t>> beforeRanges;
  static rdcarray<rdcpair<uintptr_t, uintptr_t>> PrivateRanges()
  {
    rdcarray<rdcpair<uintptr_t, uintptr_t>> ranges;
    MEMORY_BASIC_INFORMATION memory = {};
    uintptr_t cursor = 0;
    while(VirtualQuery((const void *)cursor, &memory, sizeof(memory)))
    {
      const uintptr_t begin = uintptr_t(memory.BaseAddress);
      const uintptr_t end = begin + memory.RegionSize;
      if(end <= cursor)
        break;
      if(memory.State == MEM_COMMIT && memory.Type == MEM_PRIVATE)
        ranges.push_back({begin, end});
      cursor = end;
    }
    return ranges;
  }
  static uint64_t PrivateBytes()
  {
    typedef BOOL(WINAPI * Query)(HANDLE, PPROCESS_MEMORY_COUNTERS, DWORD);
    static Query query =
        (Query)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "K32GetProcessMemoryInfo");
    PROCESS_MEMORY_COUNTERS_EX info = {};
    info.cb = sizeof(info);
    return query && query(GetCurrentProcess(), (PROCESS_MEMORY_COUNTERS *)&info, sizeof(info))
               ? info.PrivateUsage
               : 0;
  }
  D3D12CommitAudit(const char *k, uint64_t bytes, uint32_t h, uint32_t dim = 0,
                   ResourceId id = ResourceId())
      : kind(k), size(bytes), heap(h), dimension(dim), resource(id)
  {
    static const bool audit = Process::GetEnvVariable("RENDERDOC_D3D12_MEMORY_AUDIT") == "1";
    enabled = audit;
    if(enabled)
      before = PrivateBytes();
    // A diagnostic-only read of virtual address metadata, without intercepting allocation APIs.
    // The per-resource scope and final address intersection distinguish native resource backing
    // from the mapped initial sources; concurrent allocations remain a stated attribution limit.
    const rdcstr rangeMode =
        enabled ? Process::GetEnvVariable("RENDERDOC_D3D12_MEMORY_AUDIT_RANGES") : rdcstr();
    const bool fullRanges = rangeMode == "full";
    minimumRange = fullRanges ? 4096 : 1024 * 1024;
    // Thousands of tiny indirect-readback buffers do not justify a complete address-space
    // walk each. Their aggregate counter deltas are still logged; only >=1 MiB internal
    // resources and one complete frame-loading scope use the expensive range diagnostic.
    const bool internalScope =
        fullRanges && ((strcmp(kind, "replayResource") == 0 && bytes >= 1024 * 1024) ||
                       strcmp(kind, "frameLoad") == 0);
    rangeAudit =
        enabled && (resource != ResourceId() || internalScope) &&
        (fullRanges || dim > D3D12_RESOURCE_DIMENSION_BUFFER || bytes >= 16ULL * 1024 * 1024) &&
        (rangeMode == "1" || fullRanges);
    if(rangeAudit)
      beforeRanges = PrivateRanges();
  }
  ~D3D12CommitAudit()
  {
    if(rangeAudit)
    {
      const auto after = PrivateRanges();
      size_t previous = 0;
      rdcstr ranges;
      for(const auto &span : after)
      {
        uintptr_t cursor = span.first;
        while(previous < beforeRanges.size() && beforeRanges[previous].second <= cursor)
          previous++;
        for(size_t i = previous; cursor < span.second;)
        {
          const uintptr_t end =
              i < beforeRanges.size() ? RDCMIN(span.second, beforeRanges[i].first) : span.second;
          if(end > cursor && end - cursor >= minimumRange)
            ranges += StringFormat::Fmt(
                "api=D3D12Memory kind=%s id=%s bytes=%llu address=%p\n",
                strcmp(kind, "capturedHeap") == 0      ? "driverHeapCommit"
                : strcmp(kind, "pipelineCompile") == 0 ? "driverPipelineCommit"
                : strcmp(kind, "replayResource") == 0  ? "internalResourceCommit"
                : strcmp(kind, "frameLoad") == 0       ? "frameLoadCommit"
                                                       : "driverResourceCommit",
                ToStr(resource).c_str(), (unsigned long long)(end - cursor), (void *)cursor);
          if(i >= beforeRanges.size() || beforeRanges[i].first >= span.second)
            break;
          cursor = RDCMAX(cursor, beforeRanges[i].second);
          i++;
        }
      }
      if(!ranges.empty())
        AppendReplayTimingLog(ranges);
    }
    if(enabled)
      AppendReplayTimingLog(StringFormat::Fmt(
          "api=D3D12Commit kind=%s logicalExtent=%llu heap=%u dimension=%u privateDeltaBytes=%lld",
          kind, (unsigned long long)size, heap, dimension,
          (long long)PrivateBytes() - (long long)before));
  }
};
