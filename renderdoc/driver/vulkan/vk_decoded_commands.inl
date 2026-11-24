// Cache parameter decoding, not mutable descriptor state or rendered results. Each hit still
// executes the native update and updates RenderDoc's descriptor tracking. Raw live handles are
// valid only until the controller's resource/edit invalidation boundary.
void WrappedVulkan::ClearDecodedDescriptorWrites()
{
  m_DecodedDescriptorWrites.clear();
  m_DecodedDescriptorBytes = 0;
}

bool WrappedVulkan::ReplayDecodedDescriptorWrites(ReadSerialiser &ser)
{
  if(!(m_ActivePreparedModes & 1) || !m_AllowPreparedReplay || !IsActiveReplaying(m_State) ||
     m_ActionCallback || m_ReplayOptions.apiValidation)
    return false;
  auto found = m_DecodedDescriptorWrites.find(m_CurChunkOffset);
  if(found == m_DecodedDescriptorWrites.end() ||
     found->second->bodyOffset != ser.GetReader()->GetOffset())
    return false;
  const DecodedDescriptorWrites &entry = *found->second;
  ser.GetReader()->SkipBytes(entry.streamBytes);
  if(!ser.IsErrored())
  {
    ser.NotifyParametersRead();
    for(const VkWriteDescriptorSet &write : entry.application.writes)
      ReplayDescriptorSetWrite(entry.device, write);
    AddReplayTimingCounter(ReplayTimingCounter::DecodedCommandCacheHits);
    AddReplayTimingCounter(ReplayTimingCounter::DecodedCommandCacheBytes, entry.streamBytes);
  }
  return true;
}

void WrappedVulkan::CacheDecodedDescriptorWrites(ReadSerialiser &ser, VkDevice device,
                                                 uint64_t bodyOffset,
                                                 DescUpdateTemplateApplication &application)
{
  if(!(m_ActivePreparedModes & 1) || !m_AllowPreparedReplay || !IsActiveReplaying(m_State) ||
     m_ActionCallback || m_ReplayOptions.apiValidation || m_DecodedDescriptorWrites.size() >= 4096)
    return;
  uint64_t bytes = application.writes.byteSize();
  for(const VkWriteDescriptorSet &write : application.writes)
  {
    // Extension-chain/inline/ray descriptors retain the ordinary typed reader and cleanup.
    if(write.pNext || write.descriptorType == VK_DESCRIPTOR_TYPE_INLINE_UNIFORM_BLOCK ||
       write.descriptorType == VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR ||
       write.descriptorType == VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_NV)
      return;
    bytes += uint64_t(write.descriptorCount) * sizeof(VkDescriptorBufferInfo);
    if(bytes > 64ULL * 1024 * 1024)
      return;
  }
  if(bytes > 64ULL * 1024 * 1024 - m_DecodedDescriptorBytes)
    return;
  std::unique_ptr<DecodedDescriptorWrites> entry(new DecodedDescriptorWrites);
  entry->device = device;
  entry->bodyOffset = bodyOffset;
  entry->streamBytes = ser.GetReader()->GetOffset() - bodyOffset;
  entry->application.writes.swap(application.writes);
  m_DecodedDescriptorWrites[m_CurChunkOffset] = std::move(entry);
  m_DecodedDescriptorBytes += bytes;
}

void WrappedVulkan::DrainPreparedReplayBeforeMutation()
{
  if(m_PartialReplayInFlight)
  {
    SubmitCmds();
    FlushQ();
  }
}
