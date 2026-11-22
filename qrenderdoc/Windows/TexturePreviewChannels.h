#pragma once

#include <QImage>

inline QImage TexturePreviewChannels(const QImage &source, unsigned mask)
{
  mask &= 15;
  if(mask == 15)
    return source;
  if(mask == 7)
    return source.convertToFormat(QImage::Format_RGB32);

  QImage image = source.convertToFormat(QImage::Format_ARGB32);
  const bool single = mask && (mask & (mask - 1)) == 0;
  for(int y = 0; y < image.height(); ++y)
  {
    QRgb *pixels = reinterpret_cast<QRgb *>(image.scanLine(y));
    for(int x = 0; x < image.width(); ++x)
    {
      const QRgb value = pixels[x];
      if(single)
      {
        const int channel = mask == 1   ? qRed(value)
                            : mask == 2 ? qGreen(value)
                            : mask == 4 ? qBlue(value)
                                        : qAlpha(value);
        pixels[x] = qRgb(channel, channel, channel);
      }
      else
      {
        pixels[x] = qRgba(mask & 1 ? qRed(value) : 0, mask & 2 ? qGreen(value) : 0,
                          mask & 4 ? qBlue(value) : 0, mask & 8 ? qAlpha(value) : 255);
      }
    }
  }
  return image;
}
