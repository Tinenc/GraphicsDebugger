// Included by vk_core.cpp. Cache immutable initial texture upload bytes in bounded device-local
// memory. No framebuffer snapshots, capture mutations, or unbounded per-EID allocations.
static bool VulkanInitialImageCacheEnabled()
{
  static const bool enabled = Process::GetEnvVariable("RENDERDOC_VULKAN_INITIAL_IMAGE_CACHE") != "0";
  return enabled;
}
static uint64_t VulkanInitialUploadBudget(uint64_t requested, uint64_t available)
{
  const uint64_t headroom = 256ULL * 1024 * 1024;
  const uint64_t granularity = 16ULL * 1024 * 1024;
  if(available <= headroom)
    return 0;
  return RDCMIN(requested, (available - headroom) / granularity * granularity);
}

VkBuffer WrappedVulkan::InitialUploadSource(ResourceId id, const VkInitialContents &initial,
                                            VkCommandBuffer cmd, uint64_t restoreBytes,
                                            VkDeviceSize &sourceOffset)
{
  sourceOffset = 0;
  const VkBuffer original = Unwrap(initial.buf);
  if(VulkanInitialImageCacheEnabled())
    return original;
  if(!IsActiveReplaying(m_State) || initial.mem.type != MemoryType::Upload || initial.sparseBind ||
     restoreBytes < 128 * 1024)
    return original;
  auto source = m_CreationInfo.m_Buffer.find(GetResID(initial.buf));
  if(source == m_CreationInfo.m_Buffer.end())
    return original;
  const VkDeviceSize bytes = source->second.size;
  if(!bytes || (bytes % 4) != 0 || bytes > restoreBytes * 2)
    return original;

  auto &cache = m_InitialUploadCache;
  if(!cache.initialized)
  {
    cache.initialized = true;
    rdcstr setting = Process::GetEnvVariable("RENDERDOC_VULKAN_INITIAL_UPLOAD_CACHE_MIB");
    uint64_t requested = 768ULL * 1024 * 1024;
    if(!setting.empty())
      requested = uint64_t(RDCCLAMP(atoi(setting.c_str()), 0, 1024)) * 1024 * 1024;
    const VkInstDispatchTable *it = ObjDisp(GetPhysDev());
    // Without an actual memory budget, retain the original upload source. Never infer free VRAM
    // from the physical heap size when considering this optional cache.
    uint32_t extensionCount = 0;
    bool hasBudget = false;
    if(it->EnumerateDeviceExtensionProperties(Unwrap(GetPhysDev()), NULL, &extensionCount, NULL) ==
       VK_SUCCESS)
    {
      rdcarray<VkExtensionProperties> extensions;
      extensions.resize(extensionCount);
      if(it->EnumerateDeviceExtensionProperties(Unwrap(GetPhysDev()), NULL, &extensionCount,
                                                extensions.data()) == VK_SUCCESS)
        for(const VkExtensionProperties &extension : extensions)
          hasBudget |= !strcmp(extension.extensionName, VK_EXT_MEMORY_BUDGET_EXTENSION_NAME);
    }
    if(!requested || !hasBudget || !it->GetPhysicalDeviceMemoryProperties2)
      return original;
    VkPhysicalDeviceMemoryBudgetPropertiesEXT budget = {
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_BUDGET_PROPERTIES_EXT};
    VkPhysicalDeviceMemoryProperties2 properties = {
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_PROPERTIES_2, &budget};
    it->GetPhysicalDeviceMemoryProperties2(Unwrap(GetPhysDev()), &properties);
    const uint32_t preferred = m_PhysicalDeviceData.GPULocalMemIndex;
    const uint32_t heap = properties.memoryProperties.memoryTypes[preferred].heapIndex;
    cache.available = budget.heapBudget[heap] > budget.heapUsage[heap]
                          ? budget.heapBudget[heap] - budget.heapUsage[heap]
                          : 0;
    cache.capacity = VulkanInitialUploadBudget(requested, cache.available);
    if(cache.capacity == 0)
      return original;
    const VkDevDispatchTable *vt = ObjDisp(GetDev());
    VkBufferCreateInfo bufferInfo = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bufferInfo.size = cache.capacity;
    bufferInfo.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    VkResult result = vt->CreateBuffer(Unwrap(GetDev()), &bufferInfo, NULL, &cache.buffer);
    if(result != VK_SUCCESS)
    {
      cache.buffer = VK_NULL_HANDLE;
      return original;
    }
    VkMemoryRequirements requirements = {};
    vt->GetBufferMemoryRequirements(Unwrap(GetDev()), cache.buffer, &requirements);
    // The selected heap budget must correspond to the exact memory type used by this arena.
    if(!(requirements.memoryTypeBits & (1U << preferred)) ||
       !(properties.memoryProperties.memoryTypes[preferred].propertyFlags &
         VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT))
    {
      vt->DestroyBuffer(Unwrap(GetDev()), cache.buffer, NULL);
      cache.buffer = VK_NULL_HANDLE;
      return original;
    }
    VkMemoryAllocateInfo allocation = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL,
                                       requirements.size, preferred};
    result = vt->AllocateMemory(Unwrap(GetDev()), &allocation, NULL, &cache.memory);
    if(result == VK_SUCCESS)
      result = vt->BindBufferMemory(Unwrap(GetDev()), cache.buffer, cache.memory, 0);
    if(result != VK_SUCCESS)
    {
      // This optimization is optional. A cache allocation failure must leave replay usable.
      vt->DestroyBuffer(Unwrap(GetDev()), cache.buffer, NULL);
      if(cache.memory != VK_NULL_HANDLE)
        vt->FreeMemory(Unwrap(GetDev()), cache.memory, NULL);
      cache.buffer = VK_NULL_HANDLE;
      cache.memory = VK_NULL_HANDLE;
      return original;
    }
  }
  if(cache.buffer == VK_NULL_HANDLE)
    return original;
  auto found = cache.entries.find(id);
  if(found != cache.entries.end())
  {
    if(found->second.source != initial.buf || found->second.size != bytes)
      return original;
    sourceOffset = found->second.offset;
    AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheHits);
    AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheBytes, restoreBytes);
    return cache.buffer;
  }
  const VkDeviceSize alignment =
      RDCMAX(VkDeviceSize(4096), GetDeviceProps().limits.optimalBufferCopyOffsetAlignment);
  const VkDeviceSize offset = AlignUp(cache.used, alignment);
  if(offset > cache.capacity || bytes > cache.capacity - offset)
  {
    AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheFallbackBytes, restoreBytes);
    return original;
  }
  VkBufferCopy copy = {0, offset, bytes};
  ObjDisp(cmd)->CmdCopyBuffer(Unwrap(cmd), original, cache.buffer, 1, &copy);
  VkBufferMemoryBarrier ready = {VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,
                                 NULL,
                                 VK_ACCESS_TRANSFER_WRITE_BIT,
                                 VK_ACCESS_TRANSFER_READ_BIT,
                                 VK_QUEUE_FAMILY_IGNORED,
                                 VK_QUEUE_FAMILY_IGNORED,
                                 cache.buffer,
                                 offset,
                                 bytes};
  DoPipelineBarrier(cmd, 1, &ready);
  cache.entries[id] = {initial.buf, offset, bytes};
  cache.used = offset + bytes;
  sourceOffset = offset;
  AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCachePromotedBytes, bytes);
  return cache.buffer;
}

void WrappedVulkan::ReleaseInitialUploadCache()
{
  ReleaseReplayHostWrites();
  ClearDecodedDescriptorWrites();
  auto &cache = m_InitialUploadCache;
  for(const auto &item : cache.entries)
  {
    if(item.second.image != VK_NULL_HANDLE)
      ObjDisp(GetDev())->DestroyImage(Unwrap(GetDev()), item.second.image, NULL);
    if(item.second.allocation != VK_NULL_HANDLE)
      ObjDisp(GetDev())->FreeMemory(Unwrap(GetDev()), item.second.allocation, NULL);
  }
  if(cache.buffer != VK_NULL_HANDLE)
    ObjDisp(GetDev())->DestroyBuffer(Unwrap(GetDev()), cache.buffer, NULL);
  if(cache.memory != VK_NULL_HANDLE)
    ObjDisp(GetDev())->FreeMemory(Unwrap(GetDev()), cache.memory, NULL);
  cache = {};
  if(m_InitialRestoreHostFence != VK_NULL_HANDLE)
    ObjDisp(GetDev())->DestroyFence(Unwrap(GetDev()), m_InitialRestoreHostFence, NULL);
  m_InitialRestoreHostFence = VK_NULL_HANDLE;
  if(m_RestoreTimingQueries != VK_NULL_HANDLE)
    ObjDisp(GetDev())->DestroyQueryPool(Unwrap(GetDev()), m_RestoreTimingQueries, NULL);
  m_RestoreTimingQueries = VK_NULL_HANDLE;
  m_RestoreTimingPending = false;
}

bool WrappedVulkan::RestoreInitialImage(ResourceId id, const VkInitialContents &initial,
                                        VkCommandBuffer cmd, VkImage destination,
                                        const rdcarray<VkBufferImageCopy> &regions,
                                        uint64_t restoreBytes)
{
  if(!VulkanInitialImageCacheEnabled() || !IsActiveReplaying(m_State) || initial.sparseBind ||
     initial.mem.type != MemoryType::Upload || restoreBytes < 128 * 1024)
    return false;
  const auto &description = m_CreationInfo.m_Image[id];
  if(description.samples != VK_SAMPLE_COUNT_1_BIT || description.external ||
     IsYUVFormat(description.format))
    return false;
  auto &cache = m_InitialUploadCache;
  if(!cache.initialized)
  {
    cache.initialized = true;
    rdcstr setting = Process::GetEnvVariable("RENDERDOC_VULKAN_INITIAL_UPLOAD_CACHE_MIB");
    uint64_t requested = 768ULL * 1024 * 1024;
    if(!setting.empty())
      requested = uint64_t(RDCCLAMP(atoi(setting.c_str()), 0, 1024)) * 1024 * 1024;
    const VkInstDispatchTable *it = ObjDisp(GetPhysDev());
    uint32_t count = 0;
    bool supported = false;
    if(it->EnumerateDeviceExtensionProperties(Unwrap(GetPhysDev()), NULL, &count, NULL) == VK_SUCCESS)
    {
      rdcarray<VkExtensionProperties> extensions;
      extensions.resize(count);
      if(it->EnumerateDeviceExtensionProperties(Unwrap(GetPhysDev()), NULL, &count,
                                                extensions.data()) == VK_SUCCESS)
        for(const auto &extension : extensions)
          supported |= !strcmp(extension.extensionName, VK_EXT_MEMORY_BUDGET_EXTENSION_NAME);
    }
    if(!requested || !supported || !it->GetPhysicalDeviceMemoryProperties2)
      return false;
    VkPhysicalDeviceMemoryBudgetPropertiesEXT budget = {
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_BUDGET_PROPERTIES_EXT};
    VkPhysicalDeviceMemoryProperties2 properties = {
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_PROPERTIES_2, &budget};
    it->GetPhysicalDeviceMemoryProperties2(Unwrap(GetPhysDev()), &properties);
    const uint32_t heap =
        properties.memoryProperties.memoryTypes[m_PhysicalDeviceData.GPULocalMemIndex].heapIndex;
    cache.available = budget.heapBudget[heap] > budget.heapUsage[heap]
                          ? budget.heapBudget[heap] - budget.heapUsage[heap]
                          : 0;
    cache.capacity = VulkanInitialUploadBudget(requested, cache.available);
  }
  if(!cache.capacity || cache.rejected.count(id))
    return false;
  auto found = cache.entries.find(id);
  const VkDevDispatchTable *vt = ObjDisp(GetDev());
  if(found == cache.entries.end())
  {
    InitialUploadCache::Entry entry;
    entry.source = initial.buf;
    VkImageCreateInfo info = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    info.imageType = description.type;
    info.format = description.format;
    info.extent = description.extent;
    info.mipLevels = description.mipLevels;
    info.arrayLayers = description.arrayLayers;
    info.samples = VK_SAMPLE_COUNT_1_BIT;
    info.tiling = VK_IMAGE_TILING_OPTIMAL;
    info.usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    VkResult result = vt->CreateImage(Unwrap(GetDev()), &info, NULL, &entry.image);
    VkMemoryRequirements requirements = {};
    if(result == VK_SUCCESS)
      vt->GetImageMemoryRequirements(Unwrap(GetDev()), entry.image, &requirements);
    const uint32_t preferred = m_PhysicalDeviceData.GPULocalMemIndex;
    if(result == VK_SUCCESS && (!(requirements.memoryTypeBits & (1U << preferred)) ||
                                requirements.size > cache.capacity - cache.used))
      result = VK_ERROR_OUT_OF_DEVICE_MEMORY;
    if(result == VK_SUCCESS)
    {
      VkMemoryAllocateInfo allocation = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL,
                                         requirements.size, preferred};
      result = vt->AllocateMemory(Unwrap(GetDev()), &allocation, NULL, &entry.allocation);
      if(result == VK_SUCCESS)
        result = vt->BindImageMemory(Unwrap(GetDev()), entry.image, entry.allocation, 0);
    }
    if(result != VK_SUCCESS)
    {
      if(entry.image != VK_NULL_HANDLE)
        vt->DestroyImage(Unwrap(GetDev()), entry.image, NULL);
      if(entry.allocation != VK_NULL_HANDLE)
        vt->FreeMemory(Unwrap(GetDev()), entry.allocation, NULL);
      cache.rejected.insert(id);
      return false;
    }
    VkImageMemoryBarrier transition = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    transition.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    transition.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    transition.srcQueueFamilyIndex = transition.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    transition.image = entry.image;
    transition.subresourceRange = {FormatImageAspects(description.format), 0, description.mipLevels,
                                   0, description.arrayLayers};
    DoPipelineBarrier(cmd, 1, &transition);
    vt->CmdCopyBufferToImage(Unwrap(cmd), Unwrap(initial.buf), entry.image,
                             VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, (uint32_t)regions.size(),
                             regions.data());
    transition.oldLayout = transition.newLayout;
    transition.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    transition.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    transition.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    DoPipelineBarrier(cmd, 1, &transition);
    entry.uploadRegions = regions;
    for(const auto &region : regions)
      entry.imageRegions.push_back({region.imageSubresource, region.imageOffset,
                                    region.imageSubresource, region.imageOffset, region.imageExtent});
    entry.size = requirements.size;
    cache.used += requirements.size;
    cache.entries[id] = entry;
    found = cache.entries.find(id);
    AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCachePromotedBytes, requirements.size);
  }
  else
  {
    // Cache coverage is exact. A changed subresource plan falls back to immutable upload data.
    const auto &entry = found->second;
    if(entry.source != initial.buf || entry.uploadRegions.size() != regions.size() ||
       memcmp(entry.uploadRegions.data(), regions.data(), regions.byteSize()) != 0)
      return false;
    AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheHits);
    AddReplayTimingCounter(ReplayTimingCounter::InitialUploadCacheBytes, restoreBytes);
  }
  const auto &entry = found->second;
  vt->CmdCopyImage(Unwrap(cmd), entry.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, destination,
                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, (uint32_t)entry.imageRegions.size(),
                   entry.imageRegions.data());
  return true;
}

bool WrappedVulkan::CanDeferInitialRestore() const
{
  static const bool enabled =
      Process::GetEnvVariable("RENDERDOC_VULKAN_DEFER_INITIAL_RESTORE") != "0";
  // Multi-queue captures retain the original queue-ownership and CPU synchronization path.
  return enabled && IsActiveReplaying(m_State) && !Vulkan_Debug_SingleSubmitFlushing() &&
         m_ReplayCapturedQueues.size() == 1 && *m_ReplayCapturedQueues.begin() == m_Queue;
}

void WrappedVulkan::WaitForCapturedFence(VkDevice device)
{
  if(CanDeferInitialRestore() && !m_CapturedReplayWorkSubmitted)
    return;
  ScopedReplayTimingContext context(ReplayTimingContext::CapturedFence);
  ScopedReplayTimingSection timing(ReplayTimingSection::ExplicitWait);
  ObjDisp(device)->DeviceWaitIdle(Unwrap(device));
  CollectRestoreGPUTiming();
  m_InitialRestoreHostRanges.clear();
  m_InitialRestoreImageHostRanges.clear();
}

static bool VulkanRestoreRangesOverlap(const std::map<ResourceId, Intervals<bool>> &ranges,
                                       ResourceId memory, uint64_t offset, uint64_t size)
{
  auto pending = ranges.find(memory);
  if(pending == ranges.end() || !size)
    return false;
  const uint64_t finish = size > UINT64_MAX - offset ? UINT64_MAX : offset + size;
  for(auto range = pending->second.find(offset);
      range != pending->second.end() && range->start() < finish; ++range)
    if(range->value())
      return true;
  return false;
}

void WrappedVulkan::WaitForInitialRestoreHostWrite(VkDeviceMemory memory, uint64_t offset,
                                                   uint64_t size)
{
  const ResourceId id = GetResID(memory);
  const bool imageHazard =
      VulkanRestoreRangesOverlap(m_InitialRestoreImageHostRanges, id, offset, size);
  const bool bufferHazard = VulkanRestoreRangesOverlap(m_InitialRestoreHostRanges, id, offset, size);
  if(!imageHazard && !bufferHazard)
    return;
  if(IsReplayTimingActive())
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=VulkanRestoreHostWait memory=%s offset=%llu bytes=%llu image_hazard=%u "
        "buffer_hazard=%u prefix_fence=%u",
        ToStr(id).c_str(), offset, size, imageHazard ? 1U : 0U, bufferHazard ? 1U : 0U,
        m_InitialRestoreHostFence != VK_NULL_HANDLE ? 1U : 0U));
  ScopedReplayTimingContext context(ReplayTimingContext::HostRestoreHazard);
  if(imageHazard || m_InitialRestoreHostFence == VK_NULL_HANDLE ||
     !m_InitialRestoreHostPrefixSubmitted)
    FlushQ();
  else
  {
    ScopedReplayTimingSection timing(ReplayTimingSection::ExplicitWait);
    const VkResult result = ObjDisp(GetDev())->WaitForFences(
        Unwrap(GetDev()), 1, &m_InitialRestoreHostFence, VK_TRUE, UINT64_MAX);
    CHECK_VKR(this, result);
    m_InitialRestoreHostRanges.clear();
  }
}

void WrappedVulkan::SubmitInitialRestoreHostPrefix()
{
  static const bool enabled =
      Process::GetEnvVariable("RENDERDOC_VULKAN_RESTORE_HOST_PREFIX_FENCE") == "1";
  // Diagnostic A/B only: an extra submission can move the wait to OnlyDraw and make the full
  // update slower even when the prefix timer looks faster. The default batches the restore.
  if(!enabled)
    return;
  if(!CanDeferInitialRestore() || m_InitialRestoreHostPrefixSubmitted ||
     m_InitialRestoreHostRanges.empty())
    return;
  m_InitialRestoreHostPrefixSubmitted = true;
  if(m_InitialRestoreHostFence == VK_NULL_HANDLE)
  {
    VkFenceCreateInfo info = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    if(ObjDisp(GetDev())->CreateFence(Unwrap(GetDev()), &info, NULL, &m_InitialRestoreHostFence) !=
       VK_SUCCESS)
    {
      m_InitialRestoreHostFence = VK_NULL_HANDLE;
      return;
    }
  }
  // The descriptor-write boundary at the start of ApplyInitialContents already completed the
  // preceding replay, so reusing this fence cannot race a previous signal.
  VkResult result = ObjDisp(GetDev())->ResetFences(Unwrap(GetDev()), 1, &m_InitialRestoreHostFence);
  CHECK_VKR(this, result);
  if(initStateCurCmd != VK_NULL_HANDLE)
  {
    VkMemoryBarrier ready = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL, VK_ACCESS_TRANSFER_WRITE_BIT,
                             VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT};
    DoPipelineBarrier(initStateCurCmd, 1, &ready);
  }
  CloseInitStateCmd();
  SubmitCmds(NULL, NULL, 0, m_InitialRestoreHostFence);
}

void WrappedVulkan::BeginRestoreGPUTiming()
{
  static const bool enabled = Process::GetEnvVariable("RENDERDOC_VULKAN_RESTORE_GPU_TIMING") == "1";
  if(!enabled || !IsReplayTimingActive() || !IsActiveReplaying(m_State) ||
     m_QueueFamilyIdx >= ARRAY_COUNT(m_PhysicalDeviceData.queueProps) ||
     m_PhysicalDeviceData.queueProps[m_QueueFamilyIdx].timestampValidBits == 0)
    return;
  if(m_RestoreTimingQueries == VK_NULL_HANDLE)
  {
    VkQueryPoolCreateInfo info = {VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO};
    info.queryType = VK_QUERY_TYPE_TIMESTAMP;
    info.queryCount = 2;
    if(ObjDisp(GetDev())->CreateQueryPool(Unwrap(GetDev()), &info, NULL, &m_RestoreTimingQueries) !=
       VK_SUCCESS)
    {
      m_RestoreTimingQueries = VK_NULL_HANDLE;
      return;
    }
  }
  VkCommandBuffer cmd = GetInitStateCmd();
  if(cmd != VK_NULL_HANDLE)
  {
    ObjDisp(cmd)->CmdResetQueryPool(Unwrap(cmd), m_RestoreTimingQueries, 0, 2);
    ObjDisp(cmd)->CmdWriteTimestamp(Unwrap(cmd), VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                                    m_RestoreTimingQueries, 0);
  }
}

void WrappedVulkan::EndRestoreGPUTiming(VkCommandBuffer cmd)
{
  if(m_RestoreTimingQueries != VK_NULL_HANDLE && IsReplayTimingActive())
  {
    ObjDisp(cmd)->CmdWriteTimestamp(Unwrap(cmd), VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                                    m_RestoreTimingQueries, 1);
    m_RestoreTimingPending = true;
  }
}

void WrappedVulkan::CollectRestoreGPUTiming()
{
  if(!m_RestoreTimingPending)
    return;
  uint64_t values[2] = {};
  const VkResult result = ObjDisp(GetDev())->GetQueryPoolResults(
      Unwrap(GetDev()), m_RestoreTimingQueries, 0, 2, sizeof(values), values, sizeof(uint64_t),
      VK_QUERY_RESULT_64_BIT);
  if(result != VK_SUCCESS)
    return;
  m_RestoreTimingPending = false;
  const uint32_t bits = m_PhysicalDeviceData.queueProps[m_QueueFamilyIdx].timestampValidBits;
  const uint64_t mask = bits >= 64 ? UINT64_MAX : ((1ULL << bits) - 1);
  const double milliseconds =
      double((values[1] - values[0]) & mask) * GetDeviceProps().limits.timestampPeriod / 1000000.0;
  AppendReplayTimingLog(
      StringFormat::Fmt("api=VulkanRestoreGPU op=initial duration_ms=%.6f", milliseconds));
}
