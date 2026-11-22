/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 The RenderDoc Authors
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

#include "api/replay/data_types.h"

// Called while the existing action/submission index is built, never by replaying an EID query.
// Preserve all shader action events, including later executions of the same command buffer.
// Only adjacent duplicate shader events are elided here: keep insertion O(1), and leave ordinary
// resource usages exactly as supplied. Consumers can sort/unique non-adjacent duplicates.
inline bool AppendFrameResourceUsage(rdcarray<EventUsage> &uses, const EventUsage &usage)
{
  if(usage.usage == ResourceUsage::Shader)
  {
    if(usage.eventId == 0 || (!uses.empty() && uses.back() == usage))
      return false;
  }

  uses.push_back(usage);
  return true;
}
