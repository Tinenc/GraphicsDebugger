// Immutable stream data is uploaded once. Runtime buffer edits are overlaid after every copy;
// debug callbacks and unsupported queue/memory topologies retain the original host-write path.
void WrappedVulkan::ReleaseReplayHostWrites()
{
  for(auto &item : m_ReplayHostWrites)
  {
    if(item.second.upload != VK_NULL_HANDLE)
      ObjDisp(GetDev())->DestroyBuffer(Unwrap(GetDev()), item.second.upload, NULL);
    if(item.second.allocation != VK_NULL_HANDLE)
      ObjDisp(GetDev())->FreeMemory(Unwrap(GetDev()), item.second.allocation, NULL);
  }
  m_ReplayHostWrites.clear();
  m_ReplayHostWriteBytes = 0;
  m_FixedHostWriteProofs.clear();
}

void WrappedVulkan::RecordFixedHostWriteProof(ReadSerialiser &ser, VkDeviceMemory memory,
                                              uint64_t offset, uint64_t size)
{
  if(!(m_ActivePreparedModes & 8) || !m_AllowPreparedReplay || !CanDeferInitialRestore() ||
     m_CapturedReplayWorkSubmitted || m_ActionCallback || m_ReplayOptions.apiValidation ||
     APIProps.SparseResources || !m_RootEventID || memory == VK_NULL_HANDLE || ser.IsErrored() ||
     size == 0)
    return;
  const uint64_t position = ser.GetReader()->GetOffset();
  if(m_FixedHostWriteProofs.count(position) || m_FixedHostWriteProofs.size() >= 4096)
    return;
  const ResourceId id = GetResID(memory);
  auto info = m_CreationInfo.m_Memory.find(id);
  if(info == m_CreationInfo.m_Memory.end() || offset > info->second.wholeMemBufSize ||
     size > info->second.wholeMemBufSize - offset || info->second.wholeMemBuf == VK_NULL_HANDLE ||
     !(m_PhysicalDeviceData.memProps.memoryTypes[info->second.memoryTypeIndex].propertyFlags &
       VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))
    return;
  // A byte proof for ordinary buffers must not infer the layout/content of any aliased image.
  for(auto &image : m_ImageStates)
  {
    LockedConstImageStateRef state = image.second.LockRead();
    if(state && state->boundMemory == id)
      return;
  }
  for(const auto &buffer : m_CreationInfo.m_Buffer)
    if(buffer.second.replayMemory == id && (buffer.second.sparse || buffer.second.external))
      return;
  uint64_t encodedSize = 0;
  ser.GetReader()->Read(&encodedSize, sizeof(encodedSize));
  ser.GetReader()->SetOffset(position);
  if(!ser.IsErrored() && encodedSize == size)
    m_FixedHostWriteProofs[position] = {id, offset, size, m_RootEventID};
}

void WrappedVulkan::SkipOverwrittenInitialMemory(ResourceId memory, uint64_t limit,
                                                 Intervals<InitReqType> &requirements)
{
  if(!(m_ActivePreparedModes & 8) || !m_AllowPreparedReplay || !CanDeferInitialRestore() ||
     m_ActionCallback || m_ReplayOptions.apiValidation || m_CheckpointConfig.enabled ||
     m_Replay->m_ReplayCheckpointNaturalReference)
    return;
  uint64_t skipped = 0;
  for(const auto &item : m_FixedHostWriteProofs)
  {
    const FixedHostWriteProof &proof = item.second;
    // WithoutDraw excludes its target. Equality must retain the original initial contents.
    if(proof.memory != memory || m_PreparedReplayEndEvent <= proof.eventId || proof.offset >= limit)
      continue;
    const uint64_t finish = proof.offset + RDCMIN(proof.size, limit - proof.offset);
    for(auto it = requirements.find(proof.offset); it != requirements.end() && it->start() < finish;
        ++it)
      if(it->value() != eInitReq_None)
        skipped += RDCMIN(it->finish(), finish) - RDCMAX(it->start(), proof.offset);
    requirements.update(proof.offset, finish, eInitReq_None,
                        [](InitReqType, InitReqType next) { return next; });
  }
  if(skipped)
  {
    m_PreparedWritesSafeForPartial = true;
    AddReplayTimingCounter(ReplayTimingCounter::InitialOverwrittenBytesSkipped, skipped);
  }
}

bool WrappedVulkan::ReplayCachedHostWrite(ReadSerialiser &ser, VkDeviceMemory memory,
                                          uint64_t offset, uint64_t size)
{
  RecordFixedHostWriteProof(ser, memory, offset, size);
  static const rdcstr mode = Process::GetEnvVariable("RENDERDOC_VULKAN_GPU_HOST_WRITES");
  const bool enabled = (m_ActivePreparedModes & 2) != 0;
  if(!enabled || !m_AllowPreparedReplay || !CanDeferInitialRestore() || m_ActionCallback ||
     m_ReplayOptions.apiValidation || memory == VK_NULL_HANDLE || size == 0 ||
     size > 64ULL * 1024 * 1024)
    return false;
  const ResourceId id = GetResID(memory);
  if(VulkanRestoreRangesOverlap(m_InitialRestoreImageHostRanges, id, offset, size))
    return false;
  // CPU writes without a pending GPU conflict are already cheap. Promoting all of them can
  // replace fast CPU stores with expensive GPU-to-host transfers and merely delay OnlyDraw.
  if(mode != "all" && !VulkanRestoreRangesOverlap(m_InitialRestoreHostRanges, id, offset, size))
    return false;
  auto info = m_CreationInfo.m_Memory.find(id);
  if(info == m_CreationInfo.m_Memory.end() || offset > info->second.wholeMemBufSize ||
     size > info->second.wholeMemBufSize - offset || info->second.wholeMemBuf == VK_NULL_HANDLE ||
     !(m_PhysicalDeviceData.memProps.memoryTypes[info->second.memoryTypeIndex].propertyFlags &
       VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))
    return false;

  const uint64_t position = ser.GetReader()->GetOffset();
  auto found = m_ReplayHostWrites.find(position);
  if(found == m_ReplayHostWrites.end())
  {
    if(m_ReplayHostWrites.size() >= 4096)
      return false;
    // The byte-buffer field carries its own length. Do not promote absent/mismatched payloads
    // into a full-size GPU copy; retain the ordinary reader and its error handling instead.
    uint64_t encodedSize = 0;
    ser.GetReader()->Read(&encodedSize, sizeof(encodedSize));
    ser.GetReader()->SetOffset(position);
    if(ser.IsErrored() || encodedSize != size)
      return false;
    ReplayHostWrite entry;
    entry.memory = id;
    entry.offset = offset;
    entry.size = size;
    const auto &bindings = info->second.bindings;
    for(auto it = bindings.find(offset); it != bindings.end() && it->start() < offset + size; ++it)
    {
      if(it->value() == VulkanCreationInfo::Memory::LinearAndTiled)
        return false;
      if(it->value() == VulkanCreationInfo::Memory::Tiled)
        continue;
      const uint64_t start = RDCMAX(offset, it->start());
      const uint64_t finish = RDCMIN(offset + size, it->finish());
      if(start >= finish)
        continue;
      if((start % 4) || ((start - offset) % 4) || ((finish - start) % 4))
        return false;
      entry.regions.push_back({start - offset, start, finish - start});
    }

    const VkDevDispatchTable *vt = ObjDisp(GetDev());
    void *mapped = NULL;
    if(!entry.regions.empty())
    {
      VkBufferCreateInfo buffer = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
      buffer.size = size;
      buffer.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
      VkResult result = vt->CreateBuffer(Unwrap(GetDev()), &buffer, NULL, &entry.upload);
      VkMemoryRequirements requirements = {};
      if(result == VK_SUCCESS)
        vt->GetBufferMemoryRequirements(Unwrap(GetDev()), entry.upload, &requirements);
      const uint32_t type =
          result == VK_SUCCESS ? GetUploadMemoryIndex(requirements.memoryTypeBits) : 0;
      if(result == VK_SUCCESS && (!(m_PhysicalDeviceData.memProps.memoryTypes[type].propertyFlags &
                                    VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) ||
                                  requirements.size > 64ULL * 1024 * 1024 - m_ReplayHostWriteBytes))
        result = VK_ERROR_OUT_OF_HOST_MEMORY;
      if(result == VK_SUCCESS)
      {
        VkMemoryAllocateInfo allocation = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL,
                                           requirements.size, type};
        result = vt->AllocateMemory(Unwrap(GetDev()), &allocation, NULL, &entry.allocation);
        if(result == VK_SUCCESS)
          result = vt->BindBufferMemory(Unwrap(GetDev()), entry.upload, entry.allocation, 0);
        if(result == VK_SUCCESS)
          result = vt->MapMemory(Unwrap(GetDev()), entry.allocation, 0, VK_WHOLE_SIZE, 0, &mapped);
      }
      if(result != VK_SUCCESS)
      {
        if(entry.upload != VK_NULL_HANDLE)
          vt->DestroyBuffer(Unwrap(GetDev()), entry.upload, NULL);
        if(entry.allocation != VK_NULL_HANDLE)
          vt->FreeMemory(Unwrap(GetDev()), entry.allocation, NULL);
        return false;
      }
      entry.allocationBytes = requirements.size;
    }
    byte *bytes = (byte *)mapped;
    ser.Serialise("MapData"_lit, bytes, size, SerialiserFlags::NoFlags).Important();
    entry.streamBytes = ser.GetReader()->GetOffset() - position;
    if(mapped)
      vt->UnmapMemory(Unwrap(GetDev()), entry.allocation);
    m_ReplayHostWriteBytes += entry.allocationBytes;
    found = m_ReplayHostWrites.insert({position, entry}).first;
  }
  else
  {
    const ReplayHostWrite &entry = found->second;
    if(entry.memory != id || entry.offset != offset || entry.size != size)
      return false;
    ser.GetReader()->SkipBytes(entry.streamBytes);
    AddReplayTimingCounter(ReplayTimingCounter::HostWriteCacheHits);
  }

  if(ser.IsErrored())
    return true;
  const ReplayHostWrite &entry = found->second;
  if(!entry.regions.empty())
  {
    m_PreparedWritesSafeForPartial = true;
    VkCommandBuffer cmd = GetNextCmd();
    if(cmd == VK_NULL_HANDLE)
      return true;
    VkCommandBufferBeginInfo begin = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, NULL,
                                      VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    VkResult result = ObjDisp(cmd)->BeginCommandBuffer(Unwrap(cmd), &begin);
    CHECK_VKR(this, result);
    VkMemoryBarrier barrier = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL,
                               VK_ACCESS_ALL_READ_BITS | VK_ACCESS_ALL_WRITE_BITS,
                               VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT};
    DoPipelineBarrier(cmd, 1, &barrier);
    ObjDisp(cmd)->CmdCopyBuffer(Unwrap(cmd), entry.upload, Unwrap(info->second.wholeMemBuf),
                                (uint32_t)entry.regions.size(), entry.regions.data());
    for(const VkBufferCopy &copy : entry.regions)
      AddReplayTimingCounter(ReplayTimingCounter::HostWriteGPUBytes, copy.size);
    barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    barrier.dstAccessMask = VK_ACCESS_ALL_READ_BITS | VK_ACCESS_ALL_WRITE_BITS;
    DoPipelineBarrier(cmd, 1, &barrier);
    ApplyReplayBufferPatches(cmd);
    result = ObjDisp(cmd)->EndCommandBuffer(Unwrap(cmd));
    CHECK_VKR(this, result);
    SubmitCmds();
  }
  if(IsReplayTimingActive())
    AppendReplayTimingLog(StringFormat::Fmt(
        "api=VulkanHostWriteCache offset=%llu bytes=%llu regions=%zu entries=%zu allocated=%llu "
        "target_memory_flags=%u",
        position, size, entry.regions.size(), m_ReplayHostWrites.size(), m_ReplayHostWriteBytes,
        m_PhysicalDeviceData.memProps.memoryTypes[info->second.memoryTypeIndex].propertyFlags));
  return true;
}
