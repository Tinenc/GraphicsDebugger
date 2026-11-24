// Included in vk_core.cpp: native persistent uniform-buffer edits. All upload objects are replay
// owned; neither captured bytes nor the immutable initial contents are modified.

static bool VulkanReplayPatchUsageSupported(uint64_t usage)
{
  const uint64_t allowed = VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT |
                           VK_BUFFER_USAGE_TRANSFER_DST_BIT;
  return (usage & VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT) != 0 && (usage & ~allowed) == 0;
}

static bool VulkanReplayPatchBindingFits(uint64_t offset, uint64_t size, uint64_t allocationSize)
{
  return offset <= allocationSize && size <= allocationSize - offset;
}

static bool VulkanReplayPatchBindingsDisjoint(uint64_t firstOffset, uint64_t firstSize,
                                              uint64_t secondOffset, uint64_t secondSize,
                                              uint64_t allocationSize, uint64_t granularity)
{
  if(!firstSize || !secondSize || !granularity || (granularity & (granularity - 1)) ||
     !VulkanReplayPatchBindingFits(firstOffset, firstSize, allocationSize) ||
     !VulkanReplayPatchBindingFits(secondOffset, secondSize, allocationSize))
    return false;
  // Optimal-tiled images alias linear resources on bufferImageGranularity
  // pages, even when their literal byte ranges do not overlap. Division of the
  // inclusive final byte avoids overflowing while rounding a range upward.
  const uint64_t firstLast = (firstOffset + firstSize - 1) / granularity;
  const uint64_t secondLast = (secondOffset + secondSize - 1) / granularity;
  return firstLast < secondOffset / granularity || secondLast < firstOffset / granularity;
}

static bool VulkanReplayPatchMakeRestoreRange(const ReplayBufferPatch &memoryPatch,
                                               uint64_t bindingOffset, uint64_t bindingSize,
                                               uint64_t availableSize, bool clear,
                                               ReplayBufferPatchRange &restore)
{
  if(memoryPatch.data.empty() || memoryPatch.byteOffset < bindingOffset ||
     !VulkanReplayPatchBindingFits(bindingOffset, bindingSize, UINT64_MAX) ||
     !VulkanReplayPatchBindingFits(memoryPatch.byteOffset - bindingOffset,
                                   memoryPatch.data.size(), bindingSize))
    return false;
  uint64_t start = memoryPatch.byteOffset;
  uint64_t finish = start + memoryPatch.data.size();
  if(clear)
  {
    // vkCmdFillBuffer clears whole words. Extra lanes must remain inside the
    // already validated buffer binding; never extend a clear into a neighbour.
    if(finish > UINT64_MAX - 3)
      return false;
    start &= ~uint64_t(3);
    finish = (finish + 3) & ~uint64_t(3);
    if(start < bindingOffset ||
       !VulkanReplayPatchBindingFits(start - bindingOffset, finish - start, bindingSize))
      return false;
  }
  if(finish > availableSize)
    return false;
  restore = {memoryPatch.buffer, start, finish - start};
  return true;
}

static void VulkanReplayPatchAccumulateRestoreRanges(
    std::map<ResourceId, Intervals<bool>> &pending, const rdcarray<ReplayBufferPatchRange> &ranges)
{
  for(const ReplayBufferPatchRange &range : ranges)
    pending[range.buffer].update(range.byteOffset, range.byteOffset + range.byteLength, true,
                                  [](bool previous, bool next) { return previous || next; });
}

static bool VulkanReplayPatchSplitDynamicOffsets(const rdcarray<uint32_t> &counts,
                                                 uint32_t offsetCount, const uint32_t *offsets,
                                                 rdcarray<rdcarray<uint32_t>> &result)
{
  result.clear();
  uint64_t expected = 0;
  for(uint32_t count : counts)
    expected += count;
  if(expected != offsetCount || offsetCount > 1000000 || (offsetCount && !offsets))
    return false;
  uint32_t consumed = 0;
  for(uint32_t count : counts)
  {
    result.push_back({});
    if(count)
      result.back().assign(offsets + consumed, count);
    consumed += count;
  }
  return true;
}

void WrappedVulkan::RecordReplayDescriptorOffsets(
    rdcarray<VulkanStatePipeline::DescriptorAndOffsets> &sets, ResourceId layoutId,
    uint32_t firstSet, uint32_t setCount, uint32_t offsetCount, const uint32_t *offsets)
{
  auto layout = m_CreationInfo.m_PipelineLayout.find(layoutId);
  rdcarray<uint32_t> counts;
  bool valid = layout != m_CreationInfo.m_PipelineLayout.end() && firstSet <= sets.size() &&
               setCount <= sets.size() - firstSet &&
               firstSet <= layout->second.descSetLayouts.size() &&
               setCount <= layout->second.descSetLayouts.size() - firstSet;
  if(valid)
    for(uint32_t i = 0; i < setCount; ++i)
    {
      auto desc = m_CreationInfo.m_DescSetLayout.find(layout->second.descSetLayouts[firstSet + i]);
      if(desc == m_CreationInfo.m_DescSetLayout.end())
      {
        valid = false;
        break;
      }
      counts.push_back(desc->second.dynamicCount);
    }
  rdcarray<rdcarray<uint32_t>> split;
  if(!valid || !VulkanReplayPatchSplitDynamicOffsets(counts, offsetCount, offsets, split))
  {
    m_ReplayPatchReadEvidenceError = "invalidCapturedDynamicDescriptorOffsets";
    m_ReplayPatchReadsComplete = m_CheckpointPhysicalWritesComplete = false;
    return;
  }
  for(uint32_t i = 0; i < setCount; ++i)
  {
    sets[firstSet + i].pipeLayout = layoutId;
    sets[firstSet + i].offsets = split[i];
  }
}

static bool VulkanReplayPatchUniformInputs(const DescSetLayout &layout, const BindingStorage &storage,
                                           rdcarray<ResourceId> &inputs, rdcstr &reason,
                                           const rdcarray<uint32_t> *offsets = NULL,
                                           rdcarray<ReplayBufferPatchRange> *ranges = NULL)
{
  // Bound descriptors can be consumed by a replacement shader even if original reflection marked
  // them unused. Scan all elements (also mutable types), with a fail-closed work cap.
  if(storage.totalDescriptorCount() > 1024 * 1024 || layout.bindings.size() > 1024 * 1024)
  {
    reason = "descriptorEvidenceWorkLimit";
    return false;
  }
  std::set<ResourceId> unique;
  size_t dynamicIndex = 0;
  for(size_t b = 0; b < layout.bindings.size(); ++b)
  {
    const DescSetLayout::Binding &binding = layout.bindings[b];
    const uint32_t count = binding.GetDescriptorCount(storage.variableDescriptorCount);
    if(count == 0)
      continue;
    if(b >= storage.binds.size() || !storage.binds[b] ||
       binding.elemOffset > storage.totalDescriptorCount() ||
       count > storage.totalDescriptorCount() - binding.elemOffset)
    {
      reason = "incompleteDescriptorElementStorage";
      return false;
    }
    const bool dynamicBinding = binding.layoutDescType == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC ||
                                binding.layoutDescType == VK_DESCRIPTOR_TYPE_STORAGE_BUFFER_DYNAMIC;
    for(uint32_t element = 0; element < count; ++element)
    {
      const DescriptorSetSlot &slot = storage.binds[b][element];
      uint64_t dynamicOffset = 0;
      if(dynamicBinding && ranges)
      {
        // Dynamic offsets are ordered by binding, then array element, including storage buffers.
        if(!offsets || dynamicIndex >= offsets->size())
        {
          reason = "missingDynamicDescriptorOffset";
          return false;
        }
        dynamicOffset = (*offsets)[dynamicIndex++];
      }
      if((slot.type != DescriptorSlotType::UniformBuffer &&
          slot.type != DescriptorSlotType::UniformBufferDynamic) || slot.resource == ResourceId())
        continue;
      unique.insert(slot.resource);
      if(!ranges)
        continue;
      ReplayBufferPatchRange range = {slot.resource, 0, VK_WHOLE_SIZE};
      const bool exactType =
          (slot.type == DescriptorSlotType::UniformBuffer &&
           binding.layoutDescType == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER) ||
          (slot.type == DescriptorSlotType::UniformBufferDynamic &&
           binding.layoutDescType == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC);
      // Mutable/update-after-bind/variable arrays retain whole-buffer evidence. Do not infer a
      // byte proof from a descriptor whose captured storage lifetime may be asynchronous.
      if(exactType && !binding.variableSize &&
         !(layout.flags & VK_DESCRIPTOR_SET_LAYOUT_CREATE_UPDATE_AFTER_BIND_POOL_BIT))
      {
        if(dynamicOffset > ~0ULL - slot.offset || slot.GetRange() == 0 ||
           (dynamicOffset && slot.GetRange() == VK_WHOLE_SIZE))
        {
          reason = "invalidUniformDescriptorRange";
          return false;
        }
        range.byteOffset = slot.offset + dynamicOffset;
        range.byteLength = slot.GetRange();
      }
      ranges->push_back(range);
    }
  }
  if(ranges && offsets && dynamicIndex != offsets->size())
  {
    reason = "unexpectedDynamicDescriptorOffsets";
    return false;
  }
  for(ResourceId id : unique)
    inputs.push_back(id);
  return true;
}

static std::shared_ptr<const VulkanEventNode::ReplayDescriptorSnapshot> VulkanReplayPatchSnapshot(
    const VulkanEventNode::ReplayDescriptorSnapshot &sets, VulkanReplayBindingEvidenceBudget &budget)
{
  if(!budget.complete || sets.empty())
    return {};
  // Include the owning array/control allocation and every nested offset array, before allocating
  // any copy. Empty/unbound slots still occupy real storage and cannot bypass this charge.
  if(!budget.ReserveStorage(1, sizeof(VulkanEventNode::ReplayDescriptorSnapshot) + 64) ||
     !budget.ReserveStorage(sets.size(), sizeof(VulkanStatePipeline::DescriptorAndOffsets)))
    return {};
  for(const auto &set : sets)
    if((!set.offsets.empty() && !budget.ReserveStorage(1, 64)) ||
       !budget.ReserveStorage(set.offsets.size(), sizeof(uint32_t)))
      return {};
  return std::make_shared<const VulkanEventNode::ReplayDescriptorSnapshot>(sets);
}

static void VulkanReplayBindingEvidenceLost(bool &readsComplete, bool &writesComplete,
                                            rdcstr &readReason, const char *reason)
{
  readsComplete = writesComplete = false;
  readReason = reason;
}

void WrappedVulkan::InvalidateReplayBindingEvidence(const char *reason)
{
  m_ReplayBindingEvidenceBudget.complete = false;
  VulkanReplayBindingEvidenceLost(m_ReplayPatchReadsComplete, m_CheckpointPhysicalWritesComplete,
                                  m_ReplayPatchReadEvidenceError, reason);
  m_ReplayPatchReadIndex.InvalidateForEdit();
  m_ReplayPatchReadIndexBuffers.clear();
  m_CheckpointWriteIndexComplete = false;
  m_CheckpointWriteIndexPrepared = false;
  m_CheckpointResidentVersions.clear();
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=VulkanBindingEvidence complete=0 storageBytes=%llu submittedWork=%llu reason=%s",
        (unsigned long long)m_ReplayBindingEvidenceBudget.storageBytes,
        (unsigned long long)m_ReplayBindingEvidenceBudget.submittedWork, reason);
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

void WrappedVulkan::RecordReplayPatchUniformInputs(VulkanEventNode &action,
                                                   const VulkanStatePipeline &pipeline)
{
  if(!m_ReplayBindingEvidenceBudget.complete)
    return;
  action.replayPatchDescriptorSets =
      VulkanReplayPatchSnapshot(pipeline.descSets, m_ReplayBindingEvidenceBudget);
  if(!m_ReplayBindingEvidenceBudget.complete)
    InvalidateReplayBindingEvidence("descriptorSnapshotStorageBudget");
}

static void VulkanReplayPatchRecordRead(std::map<ResourceId, uint32_t> &reads, ResourceId buffer,
                                       uint32_t submittedEvent)
{
  if(buffer == ResourceId() || submittedEvent == 0)
    return;
  uint32_t &first = reads[buffer];
  first = first ? RDCMIN(first, submittedEvent) : submittedEvent;
}

void WrappedVulkan::RecordReplayPatchSubmittedReads(const VulkanEventNode &action, uint32_t event)
{
  if(!m_ReplayBindingEvidenceBudget.complete)
    return;
  // Charge each real submission again for inspection work, but share the immutable snapshot's
  // storage. This bounds temporary ranges before they reach the final one-million-entry index.
  if(!m_ReplayBindingEvidenceBudget.ReserveSubmittedWork(1) ||
     !m_ReplayBindingEvidenceBudget.ReserveSubmittedWork(action.ReplayPatchDescriptorSets().size()) ||
     !m_ReplayBindingEvidenceBudget.ReserveSubmittedWork(action.ReplayCheckpointAttachments().size()))
  {
    InvalidateReplayBindingEvidence("descriptorSubmissionWorkBudget");
    return;
  }
  m_ReplayPatchReadLastEvent = RDCMAX(m_ReplayPatchReadLastEvent, event);
  rdcarray<ReplayBufferPatchRange> ranges;
  for(const VulkanStatePipeline::DescriptorAndOffsets &bound : action.ReplayPatchDescriptorSets())
  {
    if(!bound.IsBound())
      continue;
    if(bound.IsDescBufferBound() || bound.descSet == ResourceId() || bound.push)
    {
      m_ReplayPatchReadEvidenceError = "descriptorBufferOrPushDescriptorEvidenceUnavailable";
      continue;
    }
    auto set = m_DescriptorSetState.find(bound.descSet);
    if(set == m_DescriptorSetState.end())
    {
      m_ReplayPatchReadEvidenceError = "missingBoundDescriptorSet";
      continue;
    }
    auto layout = m_CreationInfo.m_DescSetLayout.find(set->second.layout);
    if(layout == m_CreationInfo.m_DescSetLayout.end())
    {
      m_ReplayPatchReadEvidenceError = "missingBoundDescriptorLayout";
      continue;
    }
    if(!m_ReplayBindingEvidenceBudget.ReserveSubmittedWork(layout->second.bindings.size()) ||
       !m_ReplayBindingEvidenceBudget.ReserveSubmittedWork(set->second.data.totalDescriptorCount()) ||
       !m_ReplayBindingEvidenceBudget.ReserveSubmittedWork(bound.offsets.size()))
    {
      InvalidateReplayBindingEvidence("descriptorSubmissionWorkBudget");
      return;
    }
    rdcarray<ResourceId> inputs;
    VulkanReplayPatchUniformInputs(layout->second, set->second.data, inputs,
                                   m_ReplayPatchReadEvidenceError, &bound.offsets, &ranges);
  }
  // A transfer may propagate edited bytes to another resource before any shader consumes them.
  // Its whole source is deliberately conservative until every copy-region variant is indexed.
  if(m_CreationInfo.m_Buffer.find(action.action.copySource) != m_CreationInfo.m_Buffer.end())
    ranges.push_back({action.action.copySource, 0, VK_WHOLE_SIZE});
  for(ReplayBufferPatchRange range : ranges)
  {
    VulkanReplayPatchRecordRead(m_ReplayPatchFirstReads, range.buffer, event);
    auto buffer = m_CreationInfo.m_Buffer.find(range.buffer);
    if(buffer == m_CreationInfo.m_Buffer.end() || !event ||
       range.byteOffset >= buffer->second.size ||
       (range.byteLength != VK_WHOLE_SIZE &&
        range.byteLength > buffer->second.size - range.byteOffset))
    {
      m_ReplayPatchReadsComplete = false;
      continue;
    }
    if(range.byteLength == VK_WHOLE_SIZE)
      range.byteLength = buffer->second.size - range.byteOffset;
    if(m_ReplayPatchReads.size() >= 1000000)
    {
      // No truncated index can claim complete reads. Existing whole-buffer cutoffs remain usable.
      m_ReplayPatchReadsComplete = false;
      continue;
    }
    m_ReplayPatchReads.push_back({range, event});
  }
}

void WrappedVulkan::PrepareReplayPatchReadIndex(const std::set<ResourceId> &validatedBuffers)
{
  if(validatedBuffers == m_ReplayPatchReadIndexBuffers)
    return;
  m_ReplayPatchReadIndex.Clear();
  m_ReplayPatchReadIndexBuffers = validatedBuffers;
  // This is a reads-only index for resources that have passed SetReplayBufferPatches' complete
  // memory binding/alias/protected/device-address guards. Unrelated special resources need not
  // poison this proof. Values cannot change these native descriptor windows; shader replacements
  // retain the checkpoint backend's independent resource-replacement invalidation guards.
  for(ResourceId id : validatedBuffers)
    m_ReplayPatchReadIndex.Register(id, 0, m_CreationInfo.m_Buffer[id].size, false);
  for(const ReplayPatchRead &read : m_ReplayPatchReads)
    if(validatedBuffers.count(read.range.buffer))
      m_ReplayPatchReadIndex.RecordRead(read.eventId,
          {read.range.buffer, 0, read.range.byteOffset, read.range.byteLength}, true);
  if(m_ReplayPatchReadLastEvent)
    m_ReplayPatchReadIndex.FinalizeReads(1, m_ReplayPatchReadLastEvent,
        m_ReplayPatchReadsComplete && m_ReplayPatchReadEvidenceError.empty());
}

static uint32_t VulkanReplayPatchValueCutoff(const rdcarray<ReplayBufferPatch> &previous,
                                           const rdcarray<ReplayBufferPatch> &next,
                                           const std::map<ResourceId, uint32_t> &firstReads,
                                           rdcstr &reason,
                                           const rdcarray<ReplayBufferPatchRange> *changes = NULL,
                                           const ReplayResourceAccessIndex *readIndex = NULL,
                                           uint32_t lastReadEvent = 0, bool *rangeIndexKnown = NULL)
{
  if(rangeIndexKnown)
    *rangeIndexKnown = false;
  if(!SameReplayBufferPatchLayout(previous, next))
  {
    reason = "patchLayoutChanged";
    return 0;
  }
  uint32_t first = ~0U;
  for(size_t i = 0; i < next.size(); ++i)
  {
    auto use = firstReads.find(next[i].buffer);
    if(use == firstReads.end() || use->second == 0)
    {
      reason = "missingPotentialUniformOrCopyRead";
      return 0;
    }
    first = RDCMIN(first, use->second);
  }
  reason = "sameRangesPotentialUniformAndCopyReads";
  // Descriptor evidence covers all potentially bound uniform inputs, including ones unused by
  // the original shader. Keep every full-table proof above, and narrow only to changed buffer
  // identities, or complete positive native byte-range reads when the scoped index proves them.
  if(changes && !changes->empty())
  {
    uint32_t changedFirst = ~0U;
    for(const ReplayBufferPatchRange &change : *changes)
    {
      auto use = firstReads.find(change.buffer);
      if(use == firstReads.end() || use->second == 0)
      {
        reason = "missingChangedBufferConsumer";
        return 0;
      }
      uint32_t firstForRange = use->second;
      if(readIndex && lastReadEvent)
      {
        const ReplayResourceReadQuery read = readIndex->FirstRead(
            {change.buffer, 0, change.byteOffset, change.byteLength}, 1, lastReadEvent);
        if(read.complete && read.found)
        {
          firstForRange = read.eventId;
          if(rangeIndexKnown)
            *rangeIndexKnown = true;
        }
      }
      changedFirst = RDCMIN(changedFirst, firstForRange);
    }
    reason = rangeIndexKnown && *rangeIndexKnown ? "changedRangesSubmittedUniformAndCopyReads"
                                                : "changedBuffersPotentialUniformAndCopyReads";
    return changedFirst;
  }
  return first;
}

bool WrappedVulkan::CanBuildReplayPatchAnchor(uint32_t target, rdcstr &reason)
{
  if(!m_ReplayPatchAnchorEvent || target <= m_ReplayPatchAnchorEvent)
  {
    reason = "targetBeforeFirstConsumer";
    return false;
  }
  bool sameCommandBuffer = false;
  for(const CommandBufferNode *node : m_Partial.commandTree)
  {
    auto baked = m_BakedCmdBufferInfo.find(node->cmdId);
    if(baked != m_BakedCmdBufferInfo.end() && node->childCmdNodes.empty() &&
       baked->second.level == VK_COMMAND_BUFFER_LEVEL_PRIMARY &&
       IsEventInCommandBuffer(node, m_ReplayPatchAnchorEvent, baked->second.eventCount) &&
       IsEventInCommandBuffer(node, target, baked->second.eventCount))
      sameCommandBuffer = true;
  }
  if(!sameCommandBuffer)
  {
    reason = "prefixAnchorRequiresSamePrimarySubmission";
    return false;
  }
  // Match the existing restore interval's native boundaries, including the final OnlyDraw. Never
  // create an expensive anchor which cannot resume this target across a pass/command boundary.
  for(uint32_t event = m_ReplayPatchAnchorEvent; event <= target; ++event)
  {
    const APIEvent &ev = GetEvent(event);
    if(ev.eventId != event || ev.chunkIndex >= m_StructuredFile->chunks.size())
    {
      reason = "prefixAnchorMissingNativeEvent";
      return false;
    }
    const rdcstr name = ToStr((VulkanChunk)m_StructuredFile->chunks[ev.chunkIndex]->metadata.chunkID);
    if(name.find("vkCmd") != 0 || name.find("Indirect") >= 0 ||
       name.find("ExecuteCommands") >= 0 || name.find("Debug") >= 0 || name.find("Marker") >= 0 ||
       (event > m_ReplayPatchAnchorEvent && (name.find("RenderPass") >= 0 ||
         name.find("Rendering") >= 0 || name.find("NextSubpass") >= 0)))
    {
      reason = "prefixAnchorCrossesUnsupportedNativeBoundary:" + name;
      return false;
    }
  }
  reason = "samePrimarySubmissionAndPass";
  return true;
}

static ReplayBufferPatch VulkanReplayPatchInMemory(const ReplayBufferPatch &patch, ResourceId memory,
                                                   uint64_t bindingOffset)
{
  ReplayBufferPatch mapped = patch;
  mapped.buffer = memory;
  mapped.byteOffset += bindingOffset;
  return mapped;
}

static bool VulkanReplayPatchUnsafeMemoryMetadata(const SDObject &object, rdcstr *reason = NULL)
{
  const rdcstr typeName(object.type.name.c_str());
  // An SD array retains its element type name, but owns element children rather than struct
  // members. Inspect those elements; treating the array itself as a barrier rejects valid input.
  if(!object.IsArray() && object.type.basetype != SDBasic::Null)
  {
    if(typeName.contains("DeviceGroup") || typeName.contains("ImportMemory") ||
       typeName.contains("BindImagePlaneMemory"))
    {
      if(reason)
        *reason = typeName + ":unsupportedMemoryMetadata";
      return true;
    }
    if(typeName.contains("BufferMemoryBarrier"))
    {
      const SDObject *src = object.FindChild("srcQueueFamilyIndex");
      const SDObject *dst = object.FindChild("dstQueueFamilyIndex");
      if(!src || !dst || src->AsUInt64() != dst->AsUInt64())
      {
        if(reason)
          *reason = StringFormat::Fmt("%s:%s src=%llu dst=%llu", typeName.c_str(),
                                     !src || !dst ? "missingQueueFamily" : "ownershipTransfer",
                                     (unsigned long long)(src ? src->AsUInt64() : ~0ULL),
                                     (unsigned long long)(dst ? dst->AsUInt64() : ~0ULL));
        return true;
      }
    }
  }
  for(size_t i = 0; i < object.NumChildren(); ++i)
    if(VulkanReplayPatchUnsafeMemoryMetadata(*object.GetChild(i), reason))
      return true;
  return false;
}

void WrappedVulkan::DestroyReplayBufferPatches()
{
  for(ReplayPatchUpload *patch : m_ReplayBufferPatches)
  {
    patch->upload.Destroy();
    delete patch;
  }
  m_ReplayBufferPatches.clear();
  m_ReplayBufferMemoryPatches.clear();
  m_ReplayBufferRestoreRanges.clear();
}

rdcstr WrappedVulkan::SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches)
{
  rdcstr error = ValidateReplayBufferPatches(patches);
  if(!error.empty())
    return error;
  if(patches.empty() && m_ReplayBufferPatches.empty())
    return rdcstr();

  // No ownership inference from GetUsage: descriptor indexing or a replacement shader can change
  // accesses. The first supported class is ordinary uniform/transfer storage on the replay queue.
  if(!patches.empty())
  {
    if(!m_ReplayPatchCaptureChecked)
    {
      m_ReplayPatchCaptureChecked = true;
      m_ReplayPatchCaptureError = [this]() -> rdcstr {
        if(!m_StructuredFile || m_Partial.commandTree.empty())
          return "Vulkan buffer patches require structured commands and replay queue evidence";
        if(APIProps.SparseResources)
          return "Vulkan buffer patches cannot prove allocation aliases in sparse captures";
        for(const auto &queue : m_commandQueueFamilies)
          if(!ResourceIDGen::IsReplayOnlyID(queue.first) && queue.second != m_QueueFamilyIdx)
            return "Vulkan buffer patches currently require a single replay queue family";
        for(const SDChunk *chunk : m_StructuredFile->chunks)
        {
          if(chunk->metadata.chunkID < (uint32_t)SystemChunk::FirstDriverChunk)
            continue;
          const rdcstr name = ToStr(VulkanChunk(chunk->metadata.chunkID));
          rdcstr metadataReason;
          if((name == "vkAllocateMemory" || name == "vkCreateDevice" ||
              name == "vkBindBufferMemory2" || name == "vkBindImageMemory2" ||
              name.contains("PipelineBarrier") ||
              name.contains("WaitEvents")) &&
             VulkanReplayPatchUnsafeMemoryMetadata(*chunk, &metadataReason))
            return "Vulkan buffer patches do not support buffer ownership transfers, imported memory, "
                   "device-group or disjoint image-plane bindings: " + name + ":" + metadataReason;
          if(name.contains("ImportMemory") || name.contains("Video") ||
             name.contains("WriteBufferMarker") || name == "vkCmdSetDeviceMask")
            return "Vulkan buffer patches do not support imported/video memory, device masks or "
                   "in-pass buffer marker writes: " + name;
          if(name == "vkCmdBeginRendering")
          {
            const SDObject *info = chunk->FindChild("pRenderingInfo");
            const SDObject *flags = info ? info->FindChild("flags") : NULL;
            if(!flags || (flags->AsUInt64() &
                          (VK_RENDERING_SUSPENDING_BIT | VK_RENDERING_RESUMING_BIT)) != 0)
              return "Vulkan buffer patches cannot insert transfers between suspended rendering instances";
          }
        }
        return rdcstr();
      }();
    }
    if(!m_ReplayPatchCaptureError.empty())
      return m_ReplayPatchCaptureError;
  }

  rdcarray<ReplayBufferPatch> memoryPatches;
  rdcarray<ReplayBufferPatchRange> restoreRanges;
  std::set<ResourceId> validatedBuffers;
  uint64_t total = 0;
  for(const ReplayBufferPatch &patch : patches)
  {
    VulkanResourceManager *rm = GetResourceManager();
    auto buffer = m_CreationInfo.m_Buffer.find(patch.buffer);
    if(buffer == m_CreationInfo.m_Buffer.end() || !rm->HasResource(patch.buffer) ||
       ResourceIDGen::IsReplayOnlyID(patch.buffer) || rm->HasReplacement(patch.buffer))
      return "Vulkan buffer patches require an unreplaced captured buffer";
    const VulkanCreationInfo::Buffer &info = buffer->second;
    if(!VulkanReplayPatchUsageSupported(info.usage) || info.external || info.sparse ||
       info.replayPatchProtected)
      return "Vulkan persistent patches currently require unprotected uniform/transfer buffers "
             "without storage, device-address, sparse or external use";
    if(patch.byteOffset > info.size || patch.data.size() > info.size - patch.byteOffset)
      return "Vulkan patch is outside the captured buffer";
    auto memory = m_CreationInfo.m_Memory.find(info.replayMemory);
    if(info.replayMemory == ResourceId() || memory == m_CreationInfo.m_Memory.end() ||
       memory->second.wholeMemBuf == VK_NULL_HANDLE ||
       info.size > info.replayMemorySize ||
       !VulkanReplayPatchBindingFits(info.replayMemoryOffset, info.size,
                                      memory->second.wholeMemBufSize) ||
       !VulkanReplayPatchBindingFits(info.replayMemoryOffset, info.replayMemorySize,
                                      memory->second.allocSize))
      return "Vulkan buffer patch has no restorable linear memory binding";
    const VkInitialContents initial = rm->GetInitialContents(info.replayMemory);
    if(initial.type != eResDeviceMemory ||
       (initial.buf != VK_NULL_HANDLE && initial.mem.size < info.replayMemoryOffset + info.size))
      return "Vulkan buffer patch requires captured initial memory contents or canonical clear state";
    for(const auto &other : m_CreationInfo.m_Buffer)
    {
      const VulkanCreationInfo::Buffer &alias = other.second;
      if(other.first != patch.buffer && !ResourceIDGen::IsReplayOnlyID(other.first) &&
         alias.replayMemory == info.replayMemory &&
         !VulkanReplayPatchBindingsDisjoint(info.replayMemoryOffset, info.replayMemorySize,
                                            alias.replayMemoryOffset, alias.replayMemorySize,
                                            memory->second.allocSize, 1))
        return "Vulkan persistent buffer patches do not support overlapping buffer memory bindings";
    }
    // Sharing an allocation is normal suballocation, not itself aliasing. Check
    // actual replay requirements and optimal-image granularity, not GetUsage.
    for(const auto &image : m_ImageStates)
    {
      LockedConstImageStateRef state = FindConstImageState(image.first);
      if(!state || state->boundMemory != info.replayMemory)
        continue;
      const auto imageInfo = m_CreationInfo.m_Image.find(image.first);
      if(imageInfo == m_CreationInfo.m_Image.end() || !state->isMemoryBound ||
         !VulkanReplayPatchBindingsDisjoint(
             info.replayMemoryOffset, info.replayMemorySize, state->boundMemoryOffset,
             state->boundMemorySize, memory->second.allocSize,
             imageInfo->second.linear ? 1 : GetDeviceProps().limits.bufferImageGranularity))
        return "Vulkan buffer patch binding overlaps an image binding or has incomplete image memory evidence";
    }
    const ReplayBufferPatch mapped =
        VulkanReplayPatchInMemory(patch, info.replayMemory, info.replayMemoryOffset);
    const uint64_t availableSize = initial.mem.size
                                      ? RDCMIN(initial.mem.size, memory->second.wholeMemBufSize)
                                      : memory->second.wholeMemBufSize;
    ReplayBufferPatchRange restore;
    if(!VulkanReplayPatchMakeRestoreRange(mapped, info.replayMemoryOffset, info.replayMemorySize,
                                          availableSize, initial.buf == VK_NULL_HANDLE, restore))
      return "Vulkan buffer patch has no safe initial-state restore range inside its binding";
    validatedBuffers.insert(patch.buffer);
    memoryPatches.push_back(mapped);
    restoreRanges.push_back(restore);
    total += patch.data.size();
    if(total > 128ULL * 1024 * 1024)
      return "Vulkan persistent buffer patch upload exceeds 128 MiB";
  }

  PrepareReplayPatchReadIndex(validatedBuffers);
  rdcarray<ReplayBufferPatch> previous;
  for(const ReplayPatchUpload *patch : m_ReplayBufferPatches)
    previous.push_back(patch->patch);
  rdcarray<ReplayBufferPatchRange> changes;
  const bool deltaKnown = GetReplayBufferPatchChanges(previous, patches, changes).empty();
  const bool deltaEnabled =
      Process::GetEnvVariable("RENDERDOC_REPLAY_DISABLE_BUFFER_PATCH_DELTA") != "1";
  uint64_t changedBytes = 0;
  std::set<ResourceId> changedBuffers;
  if(deltaKnown)
    for(const ReplayBufferPatchRange &change : changes)
    {
      changedBytes += change.byteLength;
      changedBuffers.insert(change.buffer);
    }

  rdcarray<ReplayPatchUpload *> uploads;
  for(const ReplayBufferPatch &patch : patches)
  {
    ReplayPatchUpload *upload = new ReplayPatchUpload;
    upload->patch = patch;
    upload->upload.Create(this, GetDev(), patch.data.size(), 1, 0);
    void *data = upload->upload.UnwrappedMemory() != VK_NULL_HANDLE ? upload->upload.Map() : NULL;
    if(data)
    {
      memcpy(data, patch.data.data(), patch.data.size());
      upload->upload.Unmap();
    }
    uploads.push_back(upload);
    if(!data || HasFatalError())
    {
      for(ReplayPatchUpload *pending : uploads)
      {
        pending->upload.Destroy();
        delete pending;
      }
      return "Vulkan persistent buffer patch upload allocation failed";
    }
  }

  // Existing native command buffers can refer to the previous generation's upload buffers. Drain
  // them before releasing that generation. The controller follows a successful commit with replay.
  SubmitCmds();
  FlushQ();
  if(HasFatalError())
  {
    for(ReplayPatchUpload *pending : uploads)
    {
      pending->upload.Destroy();
      delete pending;
    }
    return "Vulkan replay failed while draining the previous buffer patch generation";
  }
  rdcstr cutoffReason, anchorReason;
  bool rangeIndexKnown = false;
  uint32_t cutoff = VulkanReplayPatchValueCutoff(previous, patches, m_ReplayPatchFirstReads,
                                                cutoffReason,
                                                deltaKnown && deltaEnabled ? &changes : NULL,
                                                &m_ReplayPatchReadIndex, m_ReplayPatchReadLastEvent,
                                                &rangeIndexKnown);
  uint32_t firstRead = VulkanReplayPatchValueCutoff(patches, patches, m_ReplayPatchFirstReads,
                                                   anchorReason);
  // The checkpoint capture envelope excludes frame descriptor mutations/secondary submissions.
  // Within it, recorded potential bindings remain valid for every submission and replacement
  // shader. Outside it the persistent edit still works, but no prefix preservation is claimed.
  if(!m_CheckpointCaptureSafe || !m_ReplayPatchReadEvidenceError.empty())
  {
    cutoffReason = m_ReplayPatchReadEvidenceError.empty() ? "checkpointCaptureEvidenceUnsupported"
                                                        : m_ReplayPatchReadEvidenceError;
    cutoff = firstRead = 0;
    rangeIndexKnown = false;
  }
  m_ReplayPatchFirstAffectedEvent = cutoff;
  m_ReplayPatchFirstConsumer = firstRead;
  const uint32_t anchor = firstRead > 1 ? firstRead - 1 : 0;
  if(!cutoff || anchor != m_ReplayPatchAnchorEvent)
    m_ReplayPatchAnchorAttempted = false;
  m_ReplayPatchAnchorEvent = anchor;
  InvalidateReplayCheckpoints(cutoff);
  VulkanReplayPatchAccumulateRestoreRanges(m_ReplayPatchRestoreMemory, m_ReplayBufferRestoreRanges);
  VulkanReplayPatchAccumulateRestoreRanges(m_ReplayPatchRestoreMemory, restoreRanges);
  DestroyReplayBufferPatches();
  m_ReplayBufferPatches.swap(uploads);
  m_ReplayBufferMemoryPatches.swap(memoryPatches);
  m_ReplayBufferRestoreRanges.swap(restoreRanges);
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=VulkanBufferPatch op=set ranges=%zu firstAffected=%u anchor=%u reason=%s "
        "deltaKnown=%u deltaEnabled=%u changedRanges=%zu changedBytes=%llu changedBuffers=%zu "
        "rangeIndexKnown=%u",
        patches.size(), cutoff, anchor, cutoffReason.c_str(), deltaKnown ? 1U : 0U,
        deltaEnabled ? 1U : 0U, deltaKnown ? changes.size() : 0,
        (unsigned long long)changedBytes, changedBuffers.size(), rangeIndexKnown ? 1U : 0U);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return rdcstr();
}

void WrappedVulkan::ApplyReplayBufferMemoryPatches(VkDeviceMemory memory, uint64_t offset, void *data,
                                                   uint64_t size)
{
  if(IsActiveReplaying(m_State))
    ::ApplyReplayBufferPatches(m_ReplayBufferMemoryPatches, GetResID(memory), offset, data, size);
}

void WrappedVulkan::ApplyReplayBufferPatches(VkCommandBuffer cmd)
{
  if(m_ReplayBufferPatches.empty() || cmd == VK_NULL_HANDLE)
    return;
  // Only called outside render passes, after initial restoration or a captured transfer write,
  // and in the OnlyDraw prelude before resuming a pass. Arbitrary byte ranges use CopyBuffer;
  // UpdateBuffer's four-byte alignment and 64 KiB restriction do not limit the public edit API.
  VkMemoryBarrier barrier = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL,
                            VK_ACCESS_ALL_WRITE_BITS | VK_ACCESS_ALL_READ_BITS,
                            VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT};
  DoPipelineBarrier(cmd, 1, &barrier);
  for(ReplayPatchUpload *patch : m_ReplayBufferPatches)
  {
    VkBuffer target = GetResourceManager()->GetHandle<VkBuffer>(patch->patch.buffer);
    VkBufferCopy copy = {0, patch->patch.byteOffset, patch->patch.data.size()};
    ObjDisp(cmd)->CmdCopyBuffer(Unwrap(cmd), patch->upload.UnwrappedBuffer(), Unwrap(target), 1,
                                &copy);
  }
  barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
  barrier.dstAccessMask = VK_ACCESS_ALL_READ_BITS | VK_ACCESS_ALL_WRITE_BITS;
  DoPipelineBarrier(cmd, 1, &barrier);
}
