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

#include "d3d12_common.h"

// This is evidence about RenderDoc's replay implementation, not DiscardResource's undefined
// API contents. The ordinary non-MSAA path in FillWithDiscardPattern copies every texel from an
// internal pattern buffer. Keep the positive proof narrow and independent of shader-based MSAA
// filling, depth planes, block compression and partial regions.
inline bool D3D12ReplayFullDiscardPattern(const D3D12_RESOURCE_DESC &resource,
                                        const D3D12_DISCARD_REGION *region,
                                        bool patternEnabled, UINT subresources)
{
  if(!patternEnabled || !subresources || resource.SampleDesc.Count != 1 ||
     (resource.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
      resource.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE3D) ||
     (resource.Flags & D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL))
    return false;
  if(resource.Format != DXGI_FORMAT_R16G16B16A16_FLOAT &&
     resource.Format != DXGI_FORMAT_R32_FLOAT && resource.Format != DXGI_FORMAT_R32_UINT &&
     resource.Format != DXGI_FORMAT_R8G8B8A8_UNORM &&
     resource.Format != DXGI_FORMAT_B8G8R8A8_UNORM)
    return false;
  return !region || (region->NumRects == 0 && region->FirstSubresource == 0 &&
                     region->NumSubresources == subresources);
}

// These predicates prove content coverage only. The caller must separately prove unconditional
// execution, a read-free command prefix, and independent/non-sparse destination storage. A view
// clear affects one mip, so a resource with multiple mips deliberately never qualifies here.
inline bool D3D12ReplayFullRTVClear(const D3D12_RESOURCE_DESC &resource,
                                  const D3D12_RENDER_TARGET_VIEW_DESC &view,
                                  UINT numRects, UINT planes)
{
  if(numRects || planes != 1 || resource.MipLevels != 1 || !resource.DepthOrArraySize ||
     !(resource.Flags & D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET))
    return false;
  switch(view.ViewDimension)
  {
    case D3D12_RTV_DIMENSION_TEXTURE1D:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D &&
             resource.DepthOrArraySize == 1 && view.Texture1D.MipSlice == 0;
    case D3D12_RTV_DIMENSION_TEXTURE1DARRAY:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D &&
             view.Texture1DArray.MipSlice == 0 && view.Texture1DArray.FirstArraySlice == 0 &&
             view.Texture1DArray.ArraySize == resource.DepthOrArraySize;
    case D3D12_RTV_DIMENSION_TEXTURE2D:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
             resource.SampleDesc.Count == 1 && resource.DepthOrArraySize == 1 &&
             view.Texture2D.MipSlice == 0 && view.Texture2D.PlaneSlice == 0;
    case D3D12_RTV_DIMENSION_TEXTURE2DARRAY:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
             resource.SampleDesc.Count == 1 && view.Texture2DArray.MipSlice == 0 &&
             view.Texture2DArray.PlaneSlice == 0 && view.Texture2DArray.FirstArraySlice == 0 &&
             view.Texture2DArray.ArraySize == resource.DepthOrArraySize;
    case D3D12_RTV_DIMENSION_TEXTURE2DMS:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
             resource.SampleDesc.Count > 1 && resource.DepthOrArraySize == 1;
    case D3D12_RTV_DIMENSION_TEXTURE2DMSARRAY:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
             resource.SampleDesc.Count > 1 && view.Texture2DMSArray.FirstArraySlice == 0 &&
             view.Texture2DMSArray.ArraySize == resource.DepthOrArraySize;
    case D3D12_RTV_DIMENSION_TEXTURE3D:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D &&
             view.Texture3D.MipSlice == 0 && view.Texture3D.FirstWSlice == 0 &&
             (view.Texture3D.WSize == resource.DepthOrArraySize || view.Texture3D.WSize == ~0U);
    default: return false;
  }
}

inline bool D3D12ReplayFullDSVClear(const D3D12_RESOURCE_DESC &resource,
                                  const D3D12_DEPTH_STENCIL_VIEW_DESC &view,
                                  D3D12_CLEAR_FLAGS clearFlags, UINT numRects, UINT planes)
{
  const UINT required = planes == 1 ? D3D12_CLEAR_FLAG_DEPTH
                                   : D3D12_CLEAR_FLAG_DEPTH | D3D12_CLEAR_FLAG_STENCIL;
  if(numRects || (planes != 1 && planes != 2) || resource.MipLevels != 1 ||
     !resource.DepthOrArraySize || !(resource.Flags & D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL) ||
     view.Flags != D3D12_DSV_FLAG_NONE || (UINT(clearFlags) & required) != required)
    return false;
  switch(view.ViewDimension)
  {
    case D3D12_DSV_DIMENSION_TEXTURE1D:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D &&
             resource.DepthOrArraySize == 1 && view.Texture1D.MipSlice == 0;
    case D3D12_DSV_DIMENSION_TEXTURE1DARRAY:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D &&
             view.Texture1DArray.MipSlice == 0 && view.Texture1DArray.FirstArraySlice == 0 &&
             view.Texture1DArray.ArraySize == resource.DepthOrArraySize;
    case D3D12_DSV_DIMENSION_TEXTURE2D:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
             resource.SampleDesc.Count == 1 && resource.DepthOrArraySize == 1 &&
             view.Texture2D.MipSlice == 0;
    case D3D12_DSV_DIMENSION_TEXTURE2DARRAY:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
             resource.SampleDesc.Count == 1 && view.Texture2DArray.MipSlice == 0 &&
             view.Texture2DArray.FirstArraySlice == 0 &&
             view.Texture2DArray.ArraySize == resource.DepthOrArraySize;
    case D3D12_DSV_DIMENSION_TEXTURE2DMS:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
             resource.SampleDesc.Count > 1 && resource.DepthOrArraySize == 1;
    case D3D12_DSV_DIMENSION_TEXTURE2DMSARRAY:
      return resource.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
             resource.SampleDesc.Count > 1 && view.Texture2DMSArray.FirstArraySlice == 0 &&
             view.Texture2DMSArray.ArraySize == resource.DepthOrArraySize;
    default: return false;
  }
}
