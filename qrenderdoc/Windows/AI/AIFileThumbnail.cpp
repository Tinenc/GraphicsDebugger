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

#include "AIFileThumbnail.h"

#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QPainter>
#include <QTransform>
#include <QVector>
#include <QtGlobal>

#if defined(Q_OS_WIN)
#include <limits>
#include <objbase.h>
#include <propidl.h>
#include <shobjidl.h>
#include <wincodec.h>
#include <windows.h>

namespace
{
const int MaxThumbnailEdge = 512;
const qint64 MaxThumbnailPixels = qint64(MaxThumbnailEdge) * qint64(MaxThumbnailEdge);
const int MaxRasterDimension = 16384;
const qint64 MaxRasterPixels = 64LL * 1024LL * 1024LL;
const qint64 HardMaxEncodedBytes = 512LL * 1024LL * 1024LL;
const int SignatureBytes = 4096;

class COMApartment
{
public:
  COMApartment()
  {
    const HRESULT result =
        CoInitializeEx(NULL, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    m_usable = SUCCEEDED(result) || result == RPC_E_CHANGED_MODE;
    m_uninitialize = SUCCEEDED(result);
  }

  ~COMApartment()
  {
    if(m_uninitialize)
      CoUninitialize();
  }

  bool usable() const { return m_usable; }

private:
  bool m_usable = false;
  bool m_uninitialize = false;
};

class ShellImageFactory
{
public:
  ~ShellImageFactory()
  {
    if(m_factory != NULL)
      m_factory->Release();
  }

  IShellItemImageFactory **address() { return &m_factory; }
  IShellItemImageFactory *operator->() const { return m_factory; }
  explicit operator bool() const { return m_factory != NULL; }

private:
  IShellItemImageFactory *m_factory = NULL;
};

class ShellBitmap
{
public:
  ~ShellBitmap()
  {
    if(m_bitmap != NULL)
      DeleteObject(m_bitmap);
  }

  HBITMAP *address() { return &m_bitmap; }
  HBITMAP get() const { return m_bitmap; }
  explicit operator bool() const { return m_bitmap != NULL; }

private:
  HBITMAP m_bitmap = NULL;
};

template <typename Interface>
class COMPtr
{
public:
  COMPtr() = default;
  ~COMPtr() { reset(); }

  COMPtr(const COMPtr &) = delete;
  COMPtr &operator=(const COMPtr &) = delete;

  Interface **address()
  {
    reset();
    return &m_pointer;
  }
  Interface *get() const { return m_pointer; }
  Interface *operator->() const { return m_pointer; }
  explicit operator bool() const { return m_pointer != NULL; }

  void reset()
  {
    if(m_pointer != NULL)
      m_pointer->Release();
    m_pointer = NULL;
  }

private:
  Interface *m_pointer = NULL;
};

bool validEncodedByteLimit(qint64 byteCount, qint64 maximumEncodedBytes)
{
  return maximumEncodedBytes > 0 && byteCount > 0 && byteCount <= maximumEncodedBytes &&
         byteCount <= HardMaxEncodedBytes &&
         quint64(byteCount) <= quint64((std::numeric_limits<DWORD>::max)());
}

bool blockedVectorOrDocumentExtension(const QString &absolutePath)
{
  const QString suffix = QFileInfo(absolutePath).suffix().toLower();
  // i18n-ignore: file-format identifiers, not user-visible text.
  return suffix == QStringLiteral("svg") || suffix == QStringLiteral("svgz") ||
         suffix == QStringLiteral("pdf") || suffix == QStringLiteral("ps") ||
         suffix == QStringLiteral("eps") || suffix == QStringLiteral("ai") ||
         suffix == QStringLiteral("wmf") || suffix == QStringLiteral("emf");
}

bool nativePathIsLocal(const QString &nativePath)
{
  // i18n-ignore: Windows UNC path prefix, not user-visible text.
  if(nativePath.startsWith(QStringLiteral("\\\\")))
    return false;
  const QString root = QDir::toNativeSeparators(QFileInfo(nativePath).absoluteDir().rootPath());
  if(root.isEmpty())
    return false;
  const UINT driveType = GetDriveTypeW(reinterpret_cast<LPCWSTR>(root.utf16()));
  return driveType == DRIVE_FIXED || driveType == DRIVE_REMOVABLE ||
         driveType == DRIVE_RAMDISK || driveType == DRIVE_CDROM;
}

bool blockedVectorOrDocumentPayload(const QByteArray &prefix)
{
  if(prefix.size() >= 2 && quint8(prefix[0]) == 0x1f && quint8(prefix[1]) == 0x8b)
    return true;    // A compressed SVG is the only common WIC-adjacent gzip image payload.

  QByteArray probe = prefix;
  if(probe.startsWith("\xEF\xBB\xBF"))
    probe.remove(0, 3);
  probe = probe.trimmed().toLower();

  // i18n-ignore: file signatures and markup identifiers, not user-visible text.
  if(probe.startsWith("%pdf-") || probe.startsWith("%!ps") ||
     probe.startsWith("%!adobe"))
    return true;

  // Permit an XML declaration and comments before <svg>, but avoid scanning arbitrary content.
  const int svgOffset = probe.left(2048).indexOf("<svg");
  if(svgOffset >= 0)
    return true;

  // Aldus placeable WMF. EMF has no sufficiently strong short signature, and is rejected by path
  // extension above; stock WIC has no EMF decoder for byte streams.
  return prefix.size() >= 4 && quint8(prefix[0]) == 0xd7 && quint8(prefix[1]) == 0xcd &&
         quint8(prefix[2]) == 0xc6 && quint8(prefix[3]) == 0x9a;
}

QString codecInfoText(IWICBitmapDecoderInfo *info, bool mimeTypes)
{
  UINT characterCount = 0;
  HRESULT result = mimeTypes ? info->GetMimeTypes(0, NULL, &characterCount)
                             : info->GetFileExtensions(0, NULL, &characterCount);
  if(FAILED(result) || characterCount <= 1 || characterCount > 65536)
    return QString();

  QVector<WCHAR> characters(int(characterCount), L'\0');
  result = mimeTypes ? info->GetMimeTypes(characterCount, characters.data(), &characterCount)
                     : info->GetFileExtensions(characterCount, characters.data(),
                                               &characterCount);
  if(FAILED(result))
    return QString();
  return QString::fromWCharArray(characters.constData()).toLower();
}

bool containsCodecToken(QString list, const QString &token)
{
  list.replace(QLatin1Char(','), QLatin1Char(';'));
  list.remove(QLatin1Char(' '));
  return (QStringLiteral(";") + list + QStringLiteral(";"))
      .contains(QStringLiteral(";") + token + QStringLiteral(";"));
}

bool decoderIsApprovedRaster(IWICBitmapDecoder *decoder)
{
  COMPtr<IWICBitmapDecoderInfo> info;
  if(FAILED(decoder->GetDecoderInfo(info.address())) || !info)
    return false;

  const QString mimeTypes = codecInfoText(info.get(), true);
  const QString extensions = codecInfoText(info.get(), false);
  // i18n-ignore: registered MIME types and file extensions, not user-visible text.
  if(mimeTypes.isEmpty() && extensions.isEmpty())
    return false;
  return !(containsCodecToken(mimeTypes, QStringLiteral("image/svg+xml")) ||
           containsCodecToken(mimeTypes, QStringLiteral("application/pdf")) ||
           containsCodecToken(mimeTypes, QStringLiteral("application/postscript")) ||
           containsCodecToken(mimeTypes, QStringLiteral("image/wmf")) ||
           containsCodecToken(mimeTypes, QStringLiteral("image/x-wmf")) ||
           containsCodecToken(mimeTypes, QStringLiteral("image/emf")) ||
           containsCodecToken(mimeTypes, QStringLiteral("image/x-emf")) ||
           containsCodecToken(extensions, QStringLiteral(".svg")) ||
           containsCodecToken(extensions, QStringLiteral(".svgz")) ||
           containsCodecToken(extensions, QStringLiteral(".pdf")) ||
           containsCodecToken(extensions, QStringLiteral(".ps")) ||
           containsCodecToken(extensions, QStringLiteral(".eps")) ||
           containsCodecToken(extensions, QStringLiteral(".ai")) ||
           containsCodecToken(extensions, QStringLiteral(".wmf")) ||
           containsCodecToken(extensions, QStringLiteral(".emf")));
}

bool isGIFPayload(const QByteArray &prefix)
{
  return prefix.startsWith("GIF87a") || prefix.startsWith("GIF89a");
}

bool validRasterDimensions(UINT width, UINT height)
{
  return width > 0 && height > 0 && width <= UINT(MaxRasterDimension) &&
         height <= UINT(MaxRasterDimension) && quint64(width) * quint64(height) <=
                                                    quint64(MaxRasterPixels);
}

bool orientationSwapsDimensions(int orientation)
{
  return orientation >= 5 && orientation <= 8;
}

int orientationValue(const PROPVARIANT &value)
{
  switch(value.vt)
  {
    case VT_UI1: return int(value.bVal);
    case VT_UI2: return int(value.uiVal);
    case VT_UI4: return value.ulVal <= 8 ? int(value.ulVal) : 1;
    case VT_I1: return int(value.cVal);
    case VT_I2: return int(value.iVal);
    case VT_I4: return value.lVal >= 1 && value.lVal <= 8 ? int(value.lVal) : 1;
    default: break;
  }
  return 1;
}

int frameOrientation(IWICBitmapFrameDecode *frame)
{
  COMPtr<IWICMetadataQueryReader> metadata;
  if(FAILED(frame->GetMetadataQueryReader(metadata.address())) || !metadata)
    return 1;

  // i18n-ignore: WIC metadata query identifiers, not user-visible text.
  const wchar_t *queries[] = {L"/app1/ifd/{ushort=274}", L"/ifd/{ushort=274}",
                              L"/xmp/tiff:Orientation"};
  for(const wchar_t *query : queries)
  {
    PROPVARIANT value;
    PropVariantInit(&value);
    const HRESULT result = metadata->GetMetadataByName(query, &value);
    if(SUCCEEDED(result))
    {
      const int orientation = orientationValue(value);
      PropVariantClear(&value);
      if(orientation >= 1 && orientation <= 8)
        return orientation;
    }
    else
    {
      PropVariantClear(&value);
    }
  }

  return 1;
}

QSize boundedOrientedSize(const QSize &sourceSize, int orientation, const QSize &maximumSize)
{
  QSize orientedSize = orientationSwapsDimensions(orientation)
                           ? QSize(sourceSize.height(), sourceSize.width())
                           : sourceSize;
  if(maximumSize == QSize())
    return orientedSize;
  if(maximumSize.width() <= 0 || maximumSize.height() <= 0)
    return QSize();

  if(orientedSize.width() > maximumSize.width() ||
     orientedSize.height() > maximumSize.height())
    orientedSize.scale(maximumSize, Qt::KeepAspectRatio);
  return orientedSize;
}

QImage applyOrientation(const QImage &source, int orientation)
{
  switch(orientation)
  {
    case 2: return source.mirrored(true, false);
    case 3: return source.transformed(QTransform().rotate(180.0));
    case 4: return source.mirrored(false, true);
    case 5:
      return source.transformed(QTransform().rotate(90.0)).mirrored(true, false);
    case 6: return source.transformed(QTransform().rotate(90.0));
    case 7:
      return source.transformed(QTransform().rotate(90.0)).mirrored(false, true);
    case 8: return source.transformed(QTransform().rotate(270.0));
    default: return source;
  }
}

QImage copyWICPixels(IWICImagingFactory *factory, IWICBitmapSource *source, UINT width,
                     UINT height, const WICPixelFormatGUID &targetFormat,
                     QImage::Format imageFormat)
{
  COMPtr<IWICFormatConverter> converter;
  if(FAILED(factory->CreateFormatConverter(converter.address())) || !converter)
    return QImage();

  if(FAILED(converter->Initialize(source, targetFormat, WICBitmapDitherTypeNone, NULL, 0.0,
                                  WICBitmapPaletteTypeCustom)))
    return QImage();

  QImage image(int(width), int(height), imageFormat);
  if(image.isNull())
    return QImage();

  const quint64 stride64 = quint64(width) * 4ULL;
  const quint64 byteCount64 = stride64 * quint64(height);
  if(stride64 > quint64((std::numeric_limits<UINT>::max)()) ||
     byteCount64 > quint64((std::numeric_limits<UINT>::max)()))
    return QImage();

  if(FAILED(converter->CopyPixels(NULL, UINT(stride64), UINT(byteCount64), image.bits())))
    return QImage();
  return image;
}

bool unsignedVariantValue(const PROPVARIANT &value, UINT *result)
{
  if(result)
    *result = 0;
  quint64 decoded = 0;
  switch(value.vt)
  {
    case VT_UI1: decoded = value.bVal; break;
    case VT_UI2: decoded = value.uiVal; break;
    case VT_UI4: decoded = value.ulVal; break;
    case VT_I1:
      if(value.cVal < 0)
        return false;
      decoded = UINT(value.cVal);
      break;
    case VT_I2:
      if(value.iVal < 0)
        return false;
      decoded = UINT(value.iVal);
      break;
    case VT_I4:
      if(value.lVal < 0)
        return false;
      decoded = UINT(value.lVal);
      break;
    default: return false;
  }
  if(decoded > quint64((std::numeric_limits<UINT>::max)()))
    return false;
  if(result)
    *result = UINT(decoded);
  return true;
}

bool metadataUnsigned(IWICMetadataQueryReader *metadata, const wchar_t *query, UINT *result)
{
  if(result)
    *result = 0;
  if(metadata == NULL || query == NULL)
    return false;
  PROPVARIANT value;
  PropVariantInit(&value);
  const HRESULT queryResult = metadata->GetMetadataByName(query, &value);
  const bool valid = SUCCEEDED(queryResult) && unsignedVariantValue(value, result);
  PropVariantClear(&value);
  return valid;
}

bool gifFrameGeometry(IWICBitmapFrameDecode *frame, QRect *geometry)
{
  if(geometry)
    *geometry = QRect();
  UINT width = 0;
  UINT height = 0;
  if(frame == NULL || FAILED(frame->GetSize(&width, &height)) || width == 0 || height == 0 ||
     width > UINT(MaxRasterDimension) || height > UINT(MaxRasterDimension))
    return false;

  UINT left = 0;
  UINT top = 0;
  COMPtr<IWICMetadataQueryReader> metadata;
  if(SUCCEEDED(frame->GetMetadataQueryReader(metadata.address())) && metadata)
  {
    // i18n-ignore: WIC GIF metadata query identifiers, not user-visible text.
    metadataUnsigned(metadata.get(), L"/imgdesc/Left", &left);
    metadataUnsigned(metadata.get(), L"/imgdesc/Top", &top);
  }
  const quint64 right = quint64(left) + quint64(width);
  const quint64 bottom = quint64(top) + quint64(height);
  if(right > quint64(MaxRasterDimension) || bottom > quint64(MaxRasterDimension) ||
     right > quint64((std::numeric_limits<int>::max)()) ||
     bottom > quint64((std::numeric_limits<int>::max)()))
    return false;
  if(geometry)
    *geometry = QRect(int(left), int(top), int(width), int(height));
  return true;
}

QSize gifCanvasSize(IWICBitmapDecoder *decoder, UINT frameCount)
{
  COMPtr<IWICMetadataQueryReader> metadata;
  UINT width = 0;
  UINT height = 0;
  if(SUCCEEDED(decoder->GetMetadataQueryReader(metadata.address())) && metadata)
  {
    // i18n-ignore: WIC GIF metadata query identifiers, not user-visible text.
    if(metadataUnsigned(metadata.get(), L"/logscrdesc/Width", &width) &&
       metadataUnsigned(metadata.get(), L"/logscrdesc/Height", &height) &&
       validRasterDimensions(width, height))
      return QSize(int(width), int(height));
  }

  quint64 maximumRight = 0;
  quint64 maximumBottom = 0;
  for(UINT index = 0; index < frameCount; ++index)
  {
    COMPtr<IWICBitmapFrameDecode> frame;
    QRect geometry;
    if(FAILED(decoder->GetFrame(index, frame.address())) || !frame ||
       !gifFrameGeometry(frame.get(), &geometry))
      return QSize();
    maximumRight = qMax(maximumRight, quint64(geometry.x()) + quint64(geometry.width()));
    maximumBottom = qMax(maximumBottom, quint64(geometry.y()) + quint64(geometry.height()));
  }
  if(maximumRight > quint64((std::numeric_limits<UINT>::max)()) ||
     maximumBottom > quint64((std::numeric_limits<UINT>::max)()) ||
     !validRasterDimensions(UINT(maximumRight), UINT(maximumBottom)))
    return QSize();
  return QSize(int(maximumRight), int(maximumBottom));
}

QRect scaledGIFFrameRect(const QRect &sourceRect, const QSize &sourceCanvas,
                         const QSize &targetCanvas)
{
  const qreal scaleX = qreal(targetCanvas.width()) / qreal(sourceCanvas.width());
  const qreal scaleY = qreal(targetCanvas.height()) / qreal(sourceCanvas.height());
  const int left = qBound(0, qRound(qreal(sourceRect.x()) * scaleX), targetCanvas.width());
  const int top = qBound(0, qRound(qreal(sourceRect.y()) * scaleY), targetCanvas.height());
  const int right = qBound(left, qRound(qreal(sourceRect.x() + sourceRect.width()) * scaleX),
                           targetCanvas.width());
  const int bottom = qBound(top, qRound(qreal(sourceRect.y() + sourceRect.height()) * scaleY),
                            targetCanvas.height());
  return QRect(left, top, right - left, bottom - top);
}

QImage decodeWICFrame(IWICImagingFactory *factory, IWICBitmapFrameDecode *frame,
                      const QSize &sourceSize, const QSize &decodedSize)
{
  if(factory == NULL || frame == NULL || !sourceSize.isValid() || !decodedSize.isValid())
    return QImage();

  IWICBitmapSource *pixelSource = frame;
  COMPtr<IWICBitmapScaler> scaler;
  if(decodedSize != sourceSize)
  {
    if(FAILED(factory->CreateBitmapScaler(scaler.address())) || !scaler ||
       FAILED(scaler->Initialize(frame, UINT(decodedSize.width()), UINT(decodedSize.height()),
                                 WICBitmapInterpolationModeFant)))
      return QImage();
    pixelSource = scaler.get();
  }

  QImage decoded = copyWICPixels(factory, pixelSource, UINT(decodedSize.width()),
                                 UINT(decodedSize.height()), GUID_WICPixelFormat32bppBGRA,
                                 QImage::Format_ARGB32);
  if(decoded.isNull())
    decoded = copyWICPixels(factory, pixelSource, UINT(decodedSize.width()),
                            UINT(decodedSize.height()), GUID_WICPixelFormat32bppPBGRA,
                            QImage::Format_ARGB32_Premultiplied);
  return decoded;
}

AIFileThumbnail::RasterAnimationResult decodeWICGIFAnimationDecoder(
    IWICImagingFactory *factory, IWICBitmapDecoder *decoder, const QSize &maximumSize,
    int maximumFrames, qint64 maximumPixelFrames)
{
  AIFileThumbnail::RasterAnimationResult result;
  if(factory == NULL || decoder == NULL || !decoderIsApprovedRaster(decoder) ||
     maximumFrames < 2 || maximumPixelFrames <= 0)
    return result;

  UINT frameCount = 0;
  if(FAILED(decoder->GetFrameCount(&frameCount)) || frameCount < 2 ||
     frameCount > UINT(maximumFrames))
    return result;

  const QSize sourceCanvas = gifCanvasSize(decoder, frameCount);
  if(!sourceCanvas.isValid())
    return result;
  QSize targetCanvas = sourceCanvas;
  if(maximumSize != QSize())
  {
    if(maximumSize.width() <= 0 || maximumSize.height() <= 0)
      return result;
    if(targetCanvas.width() > maximumSize.width() ||
       targetCanvas.height() > maximumSize.height())
      targetCanvas.scale(maximumSize, Qt::KeepAspectRatio);
  }
  if(!targetCanvas.isValid() ||
     qint64(targetCanvas.width()) * qint64(targetCanvas.height()) * qint64(frameCount) >
         maximumPixelFrames)
    return result;

  QImage canvas(targetCanvas, QImage::Format_ARGB32_Premultiplied);
  if(canvas.isNull())
    return result;
  canvas.fill(qRgba(0, 0, 0, 0));

  UINT previousDisposal = 0;
  QRect previousRect;
  QImage previousRestoreCanvas;
  result.frames.reserve(int(frameCount));
  result.frameDelaysMs.reserve(int(frameCount));

  for(UINT index = 0; index < frameCount; ++index)
  {
    if(index > 0)
    {
      if(previousDisposal == 2 && previousRect.isValid())
      {
        QPainter clearPainter(&canvas);
        clearPainter.setCompositionMode(QPainter::CompositionMode_Source);
        clearPainter.fillRect(previousRect, QColor(0, 0, 0, 0));
      }
      else if(previousDisposal == 3 && !previousRestoreCanvas.isNull())
      {
        canvas = previousRestoreCanvas;
      }
    }

    COMPtr<IWICBitmapFrameDecode> frame;
    QRect sourceRect;
    if(FAILED(decoder->GetFrame(index, frame.address())) || !frame ||
       !gifFrameGeometry(frame.get(), &sourceRect) || sourceRect.x() < 0 ||
       sourceRect.y() < 0 || sourceRect.right() >= sourceCanvas.width() ||
       sourceRect.bottom() >= sourceCanvas.height() || frameOrientation(frame.get()) != 1)
      return AIFileThumbnail::RasterAnimationResult();

    const QRect targetRect = scaledGIFFrameRect(sourceRect, sourceCanvas, targetCanvas);
    if(!targetRect.isValid() || targetRect.width() <= 0 || targetRect.height() <= 0)
      return AIFileThumbnail::RasterAnimationResult();

    UINT delayHundredths = 0;
    UINT disposal = 0;
    COMPtr<IWICMetadataQueryReader> frameMetadata;
    if(SUCCEEDED(frame->GetMetadataQueryReader(frameMetadata.address())) && frameMetadata)
    {
      // i18n-ignore: WIC GIF metadata query identifiers, not user-visible text.
      metadataUnsigned(frameMetadata.get(), L"/grctlext/Delay", &delayHundredths);
      metadataUnsigned(frameMetadata.get(), L"/grctlext/Disposal", &disposal);
    }
    if(disposal > 3)
      disposal = 0;

    QImage restoreCanvas;
    if(disposal == 3)
    {
      restoreCanvas = canvas.copy();
      if(restoreCanvas.isNull())
        return AIFileThumbnail::RasterAnimationResult();
    }

    const QImage frameImage = decodeWICFrame(factory, frame.get(), sourceRect.size(),
                                             targetRect.size());
    if(frameImage.isNull())
      return AIFileThumbnail::RasterAnimationResult();
    {
      QPainter painter(&canvas);
      painter.setCompositionMode(QPainter::CompositionMode_SourceOver);
      painter.drawImage(targetRect, frameImage);
    }

    const QImage composed = canvas.copy();
    if(composed.isNull())
      return AIFileThumbnail::RasterAnimationResult();
    result.frames.push_back(composed);
    const quint64 delayMs64 = quint64(delayHundredths) * 10ULL;
    const int delayMs = delayMs64 == 0 ? 100 : int(qBound<quint64>(20, delayMs64, 10000));
    result.frameDelaysMs.push_back(delayMs);

    previousDisposal = disposal;
    previousRect = targetRect;
    previousRestoreCanvas = restoreCanvas;
  }

  result.sourceSize = targetCanvas;
  return result.isNull() ? AIFileThumbnail::RasterAnimationResult() : result;
}

AIFileThumbnail::RasterDecodeResult decodeWICDecoder(IWICImagingFactory *factory,
                                                     IWICBitmapDecoder *decoder,
                                                     const QSize &maximumSize)
{
  AIFileThumbnail::RasterDecodeResult result;

  if(!decoderIsApprovedRaster(decoder))
    return result;

  UINT frameCount = 0;
  if(FAILED(decoder->GetFrameCount(&frameCount)) || frameCount == 0)
    return result;

  COMPtr<IWICBitmapFrameDecode> frame;
  if(FAILED(decoder->GetFrame(0, frame.address())) || !frame)
    return result;

  UINT sourceWidth = 0;
  UINT sourceHeight = 0;
  if(FAILED(frame->GetSize(&sourceWidth, &sourceHeight)) ||
     !validRasterDimensions(sourceWidth, sourceHeight))
    return result;

  result.sourceSize = QSize(int(sourceWidth), int(sourceHeight));
  result.orientation = frameOrientation(frame.get());
  result.orientedSourceSize = orientationSwapsDimensions(result.orientation)
                                  ? QSize(int(sourceHeight), int(sourceWidth))
                                  : result.sourceSize;

  const QSize decodedOrientedSize =
      boundedOrientedSize(result.sourceSize, result.orientation, maximumSize);
  if(decodedOrientedSize.isEmpty())
    return AIFileThumbnail::RasterDecodeResult();

  const QSize decodedSourceSize = orientationSwapsDimensions(result.orientation)
                                      ? QSize(decodedOrientedSize.height(),
                                              decodedOrientedSize.width())
                                      : decodedOrientedSize;
  if(!validRasterDimensions(UINT(decodedSourceSize.width()), UINT(decodedSourceSize.height())))
    return AIFileThumbnail::RasterDecodeResult();

  IWICBitmapSource *pixelSource = frame.get();
  COMPtr<IWICBitmapScaler> scaler;
  if(decodedSourceSize != result.sourceSize)
  {
    if(FAILED(factory->CreateBitmapScaler(scaler.address())) || !scaler ||
       FAILED(scaler->Initialize(frame.get(), UINT(decodedSourceSize.width()),
                                 UINT(decodedSourceSize.height()),
                                 WICBitmapInterpolationModeFant)))
      return AIFileThumbnail::RasterDecodeResult();
    pixelSource = scaler.get();
  }

  // WIC BGRA bytes map exactly to QImage ARGB32 on Windows' little-endian targets. If a codec
  // cannot provide straight alpha, request premultiplied BGRA and use the matching QImage format.
  QImage decoded = copyWICPixels(factory, pixelSource, UINT(decodedSourceSize.width()),
                                 UINT(decodedSourceSize.height()), GUID_WICPixelFormat32bppBGRA,
                                 QImage::Format_ARGB32);
  if(decoded.isNull())
    decoded = copyWICPixels(factory, pixelSource, UINT(decodedSourceSize.width()),
                            UINT(decodedSourceSize.height()), GUID_WICPixelFormat32bppPBGRA,
                            QImage::Format_ARGB32_Premultiplied);
  if(decoded.isNull())
    return AIFileThumbnail::RasterDecodeResult();

  result.image = applyOrientation(decoded, result.orientation);
  if(result.image.isNull() || result.image.size() != decodedOrientedSize)
    return AIFileThumbnail::RasterDecodeResult();
  return result;
}

AIFileThumbnail::RasterDecodeResult decodeWICData(const QByteArray &encodedData,
                                                  const QSize &maximumSize,
                                                  qint64 maximumEncodedBytes)
{
  AIFileThumbnail::RasterDecodeResult result;
  if((maximumSize != QSize() &&
      (maximumSize.width() <= 0 || maximumSize.height() <= 0)) ||
     !validEncodedByteLimit(encodedData.size(), maximumEncodedBytes) ||
     blockedVectorOrDocumentPayload(encodedData.left(SignatureBytes)))
    return result;

  const COMApartment apartment;
  if(!apartment.usable())
    return result;

  COMPtr<IWICImagingFactory> factory;
  if(FAILED(CoCreateInstance(CLSID_WICImagingFactory, NULL, CLSCTX_INPROC_SERVER,
                             IID_IWICImagingFactory,
                             reinterpret_cast<void **>(factory.address()))) ||
     !factory)
    return result;

  COMPtr<IWICStream> stream;
  if(FAILED(factory->CreateStream(stream.address())) || !stream)
    return result;
  if(FAILED(stream->InitializeFromMemory(
         reinterpret_cast<BYTE *>(const_cast<char *>(encodedData.constData())),
         DWORD(encodedData.size()))))
    return result;

  COMPtr<IWICBitmapDecoder> decoder;
  if(FAILED(factory->CreateDecoderFromStream(stream.get(), NULL, WICDecodeMetadataCacheOnDemand,
                                             decoder.address())) ||
     !decoder)
    return result;
  return decodeWICDecoder(factory.get(), decoder.get(), maximumSize);
}

AIFileThumbnail::RasterAnimationResult decodeWICGIFAnimationData(
    const QByteArray &encodedData, const QSize &maximumSize, qint64 maximumEncodedBytes,
    int maximumFrames, qint64 maximumPixelFrames)
{
  AIFileThumbnail::RasterAnimationResult result;
  if((maximumSize != QSize() &&
      (maximumSize.width() <= 0 || maximumSize.height() <= 0)) ||
     maximumFrames < 2 || maximumPixelFrames <= 0 ||
     !validEncodedByteLimit(encodedData.size(), maximumEncodedBytes) ||
     !isGIFPayload(encodedData.left(SignatureBytes)))
    return result;

  const COMApartment apartment;
  if(!apartment.usable())
    return result;

  COMPtr<IWICImagingFactory> factory;
  if(FAILED(CoCreateInstance(CLSID_WICImagingFactory, NULL, CLSCTX_INPROC_SERVER,
                             IID_IWICImagingFactory,
                             reinterpret_cast<void **>(factory.address()))) ||
     !factory)
    return result;

  COMPtr<IWICStream> stream;
  if(FAILED(factory->CreateStream(stream.address())) || !stream ||
     FAILED(stream->InitializeFromMemory(
         reinterpret_cast<BYTE *>(const_cast<char *>(encodedData.constData())),
         DWORD(encodedData.size()))))
    return result;

  COMPtr<IWICBitmapDecoder> decoder;
  if(FAILED(factory->CreateDecoderFromStream(stream.get(), NULL, WICDecodeMetadataCacheOnDemand,
                                             decoder.address())) ||
     !decoder)
    return result;
  return decodeWICGIFAnimationDecoder(factory.get(), decoder.get(), maximumSize, maximumFrames,
                                      maximumPixelFrames);
}

QSize boundedRequest(const QSize &requestedSize)
{
  if(requestedSize.width() <= 0 || requestedSize.height() <= 0)
    return QSize();

  QSize bounded = requestedSize;
  if(bounded.width() > MaxThumbnailEdge || bounded.height() > MaxThumbnailEdge ||
     qint64(bounded.width()) * qint64(bounded.height()) > MaxThumbnailPixels)
    bounded.scale(MaxThumbnailEdge, MaxThumbnailEdge, Qt::KeepAspectRatio);

  return bounded;
}

QImage imageFromShellBitmap(HBITMAP bitmap, const QSize &request)
{
  BITMAP nativeBitmap = {};
  if(GetObject(bitmap, sizeof(nativeBitmap), &nativeBitmap) != sizeof(nativeBitmap))
    return QImage();

  const qint64 width64 = nativeBitmap.bmWidth;
  const qint64 signedHeight64 = nativeBitmap.bmHeight;
  const qint64 height64 = signedHeight64 < 0 ? -signedHeight64 : signedHeight64;
  if(width64 <= 0 || height64 <= 0 || width64 > MaxThumbnailEdge ||
     height64 > MaxThumbnailEdge || width64 * height64 > MaxThumbnailPixels ||
     width64 > request.width() || height64 > request.height())
    return QImage();

  const int width = int(width64);
  const int height = int(height64);

  QImage image(width, height, QImage::Format_ARGB32);
  if(image.isNull())
    return QImage();

  BITMAPINFO bitmapInfo = {};
  bitmapInfo.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
  bitmapInfo.bmiHeader.biWidth = width;
  bitmapInfo.bmiHeader.biHeight = -height;
  bitmapInfo.bmiHeader.biPlanes = 1;
  bitmapInfo.bmiHeader.biBitCount = 32;
  bitmapInfo.bmiHeader.biCompression = BI_RGB;

  HDC screen = GetDC(NULL);
  if(screen == NULL)
    return QImage();

  const int copiedRows =
      GetDIBits(screen, bitmap, 0, UINT(height), image.bits(), &bitmapInfo, DIB_RGB_COLORS);
  ReleaseDC(NULL, screen);
  if(copiedRows != height)
    return QImage();

  // Some shell thumbnail providers return an opaque 32-bit DIB with the unused alpha byte cleared.
  // Preserve real alpha when present, otherwise make the content opaque rather than invisible.
  bool hasAlpha = false;
  for(int y = 0; y < image.height() && !hasAlpha; ++y)
  {
    const QRgb *pixels = reinterpret_cast<const QRgb *>(image.constScanLine(y));
    for(int x = 0; x < image.width(); ++x)
    {
      if(qAlpha(pixels[x]) != 0)
      {
        hasAlpha = true;
        break;
      }
    }
  }

  if(!hasAlpha)
  {
    for(int y = 0; y < image.height(); ++y)
    {
      QRgb *pixels = reinterpret_cast<QRgb *>(image.scanLine(y));
      for(int x = 0; x < image.width(); ++x)
        pixels[x] = qRgb(qRed(pixels[x]), qGreen(pixels[x]), qBlue(pixels[x]));
    }
  }

  return image;
}
};    // namespace
#endif

namespace AIFileThumbnail
{
QImage loadSystemThumbnail(const QString &absolutePath, const QSize &requestedSize)
{
#if defined(Q_OS_WIN)
  const QSize request = boundedRequest(requestedSize);
  if(request.isEmpty())
    return QImage();

  const QFileInfo fileInfo(absolutePath);
  if(!fileInfo.isAbsolute() || !fileInfo.exists() || !fileInfo.isFile() ||
     fileInfo.isSymLink())
    return QImage();

  const QString canonicalPath = fileInfo.canonicalFilePath();
  if(canonicalPath.isEmpty())
    return QImage();
  const qint64 originalSize = fileInfo.size();
  const qint64 originalModifiedMs = fileInfo.lastModified().toMSecsSinceEpoch();

  const COMApartment apartment;
  if(!apartment.usable())
    return QImage();

  ShellImageFactory factory;
  const QString nativePath = QDir::toNativeSeparators(canonicalPath);
  if(!nativePathIsLocal(nativePath))
    return QImage();
  const HRESULT factoryResult = SHCreateItemFromParsingName(
      reinterpret_cast<PCWSTR>(nativePath.utf16()), NULL, IID_IShellItemImageFactory,
      reinterpret_cast<void **>(factory.address()));
  if(FAILED(factoryResult) || !factory)
    return QImage();

  ShellBitmap bitmap;
  const SIZE nativeSize = {request.width(), request.height()};
  const HRESULT imageResult = factory->GetImage(nativeSize, SIIGBF_THUMBNAILONLY, bitmap.address());
  if(FAILED(imageResult) || !bitmap)
    return QImage();

  const QImage image = imageFromShellBitmap(bitmap.get(), request);
  if(image.isNull())
    return QImage();

  // Do not present a thumbnail that raced a replacement or modification of the selected file.
  QFileInfo currentFile(canonicalPath);
  currentFile.refresh();
  if(!currentFile.exists() || !currentFile.isFile() ||
     currentFile.isSymLink() ||
     currentFile.canonicalFilePath() != canonicalPath || currentFile.size() != originalSize ||
     currentFile.lastModified().toMSecsSinceEpoch() != originalModifiedMs)
    return QImage();

  return image;
#else
  Q_UNUSED(absolutePath);
  Q_UNUSED(requestedSize);
  return QImage();
#endif
}

RasterDecodeResult decodeRasterData(const QByteArray &encodedData, const QSize &maximumSize,
                                    qint64 maximumEncodedBytes)
{
#if defined(Q_OS_WIN)
  // Keep a shared, immutable reference alive for IWICStream::InitializeFromMemory. QByteArray's
  // copy-on-write semantics pin this storage if the caller subsequently assigns to its value.
  const QByteArray pinnedData = encodedData;
  return decodeWICData(pinnedData, maximumSize, maximumEncodedBytes);
#else
  Q_UNUSED(encodedData);
  Q_UNUSED(maximumSize);
  Q_UNUSED(maximumEncodedBytes);
  return RasterDecodeResult();
#endif
}

RasterDecodeResult decodeRasterFile(const QString &absolutePath, const QSize &maximumSize,
                                    qint64 maximumEncodedBytes)
{
#if defined(Q_OS_WIN)
  RasterDecodeResult result;
  if(maximumEncodedBytes <= 0 || (maximumSize != QSize() &&
      (maximumSize.width() <= 0 || maximumSize.height() <= 0)))
    return result;

  const QFileInfo selectedFile(absolutePath);
  if(!selectedFile.isAbsolute() || !selectedFile.exists() || !selectedFile.isFile() ||
     selectedFile.isSymLink())
    return result;

  const QString canonicalPath = selectedFile.canonicalFilePath();
  if(canonicalPath.isEmpty() || blockedVectorOrDocumentExtension(canonicalPath))
    return result;

  const QString nativePath = QDir::toNativeSeparators(canonicalPath);
  if(!nativePathIsLocal(nativePath))
    return result;

  QFileInfo originalFile(canonicalPath);
  originalFile.refresh();
  const qint64 originalSize = originalFile.size();
  const qint64 originalModifiedMs = originalFile.lastModified().toMSecsSinceEpoch();
  if(originalFile.isSymLink() || originalFile.canonicalFilePath() != canonicalPath ||
     !validEncodedByteLimit(originalSize, maximumEncodedBytes))
    return result;

  QFile prefixFile(canonicalPath);
  if(!prefixFile.open(QIODevice::ReadOnly) || prefixFile.size() != originalSize)
    return result;
  const QByteArray prefix = prefixFile.read(SignatureBytes);
  prefixFile.close();
  if(prefix.isEmpty() || blockedVectorOrDocumentPayload(prefix))
    return result;

  const COMApartment apartment;
  if(!apartment.usable())
    return result;

  COMPtr<IWICImagingFactory> factory;
  if(FAILED(CoCreateInstance(CLSID_WICImagingFactory, NULL, CLSCTX_INPROC_SERVER,
                             IID_IWICImagingFactory,
                             reinterpret_cast<void **>(factory.address()))) ||
     !factory)
    return result;

  COMPtr<IWICBitmapDecoder> decoder;
  if(FAILED(factory->CreateDecoderFromFilename(
         reinterpret_cast<LPCWSTR>(nativePath.utf16()), NULL, GENERIC_READ,
         WICDecodeMetadataCacheOnDemand, decoder.address())) ||
     !decoder)
    return result;

  result = decodeWICDecoder(factory.get(), decoder.get(), maximumSize);
  if(result.isNull())
    return RasterDecodeResult();

  // Do not return pixels decoded across a replacement or modification of the selected file.
  QFileInfo currentFile(canonicalPath);
  currentFile.refresh();
  if(!currentFile.exists() || !currentFile.isFile() ||
     currentFile.isSymLink() ||
     currentFile.canonicalFilePath() != canonicalPath || currentFile.size() != originalSize ||
     currentFile.lastModified().toMSecsSinceEpoch() != originalModifiedMs)
    return RasterDecodeResult();
  return result;
#else
  Q_UNUSED(absolutePath);
  Q_UNUSED(maximumSize);
  Q_UNUSED(maximumEncodedBytes);
  return RasterDecodeResult();
#endif
}

RasterAnimationResult decodeGIFAnimationData(const QByteArray &encodedData,
                                             const QSize &maximumSize,
                                             qint64 maximumEncodedBytes, int maximumFrames,
                                             qint64 maximumPixelFrames)
{
#if defined(Q_OS_WIN)
  // Keep the encoded storage alive until every WIC frame has been composed.
  const QByteArray pinnedData = encodedData;
  return decodeWICGIFAnimationData(pinnedData, maximumSize, maximumEncodedBytes,
                                   maximumFrames, maximumPixelFrames);
#else
  Q_UNUSED(encodedData);
  Q_UNUSED(maximumSize);
  Q_UNUSED(maximumEncodedBytes);
  Q_UNUSED(maximumFrames);
  Q_UNUSED(maximumPixelFrames);
  return RasterAnimationResult();
#endif
}

RasterAnimationResult decodeGIFAnimationFile(const QString &absolutePath,
                                             const QSize &maximumSize,
                                             qint64 maximumEncodedBytes, int maximumFrames,
                                             qint64 maximumPixelFrames)
{
#if defined(Q_OS_WIN)
  RasterAnimationResult result;
  if(maximumEncodedBytes <= 0 || maximumFrames < 2 || maximumPixelFrames <= 0 ||
     (maximumSize != QSize() &&
      (maximumSize.width() <= 0 || maximumSize.height() <= 0)))
    return result;

  const QFileInfo selectedFile(absolutePath);
  if(!selectedFile.isAbsolute() || !selectedFile.exists() || !selectedFile.isFile() ||
     selectedFile.isSymLink())
    return result;
  const QString canonicalPath = selectedFile.canonicalFilePath();
  if(canonicalPath.isEmpty())
    return result;
  const QString nativePath = QDir::toNativeSeparators(canonicalPath);
  if(!nativePathIsLocal(nativePath))
    return result;

  QFileInfo originalFile(canonicalPath);
  originalFile.refresh();
  const qint64 originalSize = originalFile.size();
  const qint64 originalModifiedMs = originalFile.lastModified().toMSecsSinceEpoch();
  if(originalFile.isSymLink() || originalFile.canonicalFilePath() != canonicalPath ||
     !validEncodedByteLimit(originalSize, maximumEncodedBytes))
    return result;

  QFile prefixFile(canonicalPath);
  if(!prefixFile.open(QIODevice::ReadOnly) || prefixFile.size() != originalSize)
    return result;
  const QByteArray prefix = prefixFile.read(SignatureBytes);
  prefixFile.close();
  if(!isGIFPayload(prefix))
    return result;

  const COMApartment apartment;
  if(!apartment.usable())
    return result;
  COMPtr<IWICImagingFactory> factory;
  if(FAILED(CoCreateInstance(CLSID_WICImagingFactory, NULL, CLSCTX_INPROC_SERVER,
                             IID_IWICImagingFactory,
                             reinterpret_cast<void **>(factory.address()))) ||
     !factory)
    return result;
  COMPtr<IWICBitmapDecoder> decoder;
  if(FAILED(factory->CreateDecoderFromFilename(
         reinterpret_cast<LPCWSTR>(nativePath.utf16()), NULL, GENERIC_READ,
         WICDecodeMetadataCacheOnDemand, decoder.address())) ||
     !decoder)
    return result;

  result = decodeWICGIFAnimationDecoder(factory.get(), decoder.get(), maximumSize,
                                        maximumFrames, maximumPixelFrames);
  if(result.isNull())
    return RasterAnimationResult();

  QFileInfo currentFile(canonicalPath);
  currentFile.refresh();
  if(!currentFile.exists() || !currentFile.isFile() || currentFile.isSymLink() ||
     currentFile.canonicalFilePath() != canonicalPath || currentFile.size() != originalSize ||
     currentFile.lastModified().toMSecsSinceEpoch() != originalModifiedMs)
    return RasterAnimationResult();
  return result;
#else
  Q_UNUSED(absolutePath);
  Q_UNUSED(maximumSize);
  Q_UNUSED(maximumEncodedBytes);
  Q_UNUSED(maximumFrames);
  Q_UNUSED(maximumPixelFrames);
  return RasterAnimationResult();
#endif
}
};    // namespace AIFileThumbnail
