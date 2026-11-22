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

#include <QByteArray>
#include <QImage>
#include <QSize>
#include <QString>
#include <QVector>

namespace AIFileThumbnail
{
struct RasterDecodeResult
{
  QImage image;
  // Dimensions reported by the decoder before and after applying EXIF orientation.
  QSize sourceSize;
  QSize orientedSourceSize;
  int orientation = 1;

  bool isNull() const { return image.isNull(); }
};

struct RasterAnimationResult
{
  QVector<QImage> frames;
  QVector<int> frameDelaysMs;
  QSize sourceSize;

  bool isNull() const
  {
    return frames.size() < 2 || frameDelaysMs.size() != frames.size() ||
           !sourceSize.isValid();
  }
};

// Returns a bounded content thumbnail supplied by the operating system for the current local
// file. A null image means that the platform or its installed thumbnail providers do not support
// the file. Generic file-type icons are deliberately never returned.
QImage loadSystemThumbnail(const QString &absolutePath, const QSize &requestedSize);

// Decode the first frame of raster image data through the operating system's installed Windows
// Imaging Component codecs. The default QSize() preserves the source pixels; otherwise decoding is
// bounded proportionally without upscaling. SVG, PostScript, PDF, and other document/vector payloads
// are rejected. A null result is returned on non-Windows platforms.
RasterDecodeResult decodeRasterData(const QByteArray &encodedData,
                                    const QSize &maximumSize = QSize(),
                                    qint64 maximumEncodedBytes = 64LL * 1024LL * 1024LL);

// As above, for a stable absolute local file. The canonical path, size, and modification time are
// checked before and after decoding so a file changed during the operation is never returned.
RasterDecodeResult decodeRasterFile(const QString &absolutePath,
                                    const QSize &maximumSize = QSize(),
                                    qint64 maximumEncodedBytes = 256LL * 1024LL * 1024LL);

// Decode and compose a bounded GIF animation through WIC. Frame count, decoded pixel-frames, frame
// delays, and encoded bytes are all capped by the caller. Other multi-page formats are not treated
// as animations. A null result is returned when fewer than two safe frames are available.
RasterAnimationResult decodeGIFAnimationData(
    const QByteArray &encodedData, const QSize &maximumSize = QSize(),
    qint64 maximumEncodedBytes = 64LL * 1024LL * 1024LL, int maximumFrames = 2048,
    qint64 maximumPixelFrames = 32LL * 1024LL * 1024LL);

// As above, for a stable local GIF path. The canonical path, size, and modification time are
// checked before and after decoding.
RasterAnimationResult decodeGIFAnimationFile(
    const QString &absolutePath, const QSize &maximumSize = QSize(),
    qint64 maximumEncodedBytes = 64LL * 1024LL * 1024LL, int maximumFrames = 2048,
    qint64 maximumPixelFrames = 32LL * 1024LL * 1024LL);
};
