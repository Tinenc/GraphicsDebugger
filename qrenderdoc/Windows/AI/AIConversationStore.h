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
#include <QString>

// Stores an opaque conversation payload outside the capture. Each backing file is keyed by the
// normalised absolute path of its RDC, so captures never share conversation history.
class AIConversationStore
{
public:
  static constexpr int MaximumPayloadBytes = 64 * 1024 * 1024;

  // A missing backing file is a successful load with an empty payload. Empty capture paths and
  // malformed/corrupt files fail and populate error when it is supplied.
  static bool Load(const QString &capturePath, QByteArray *payload, QString *error = 0);
  static bool Save(const QString &capturePath, const QByteArray &payload, QString *error = 0);

  // Exposed for diagnostics and tests. Both return an empty string for an empty capture path.
  static QString NormalizedCapturePath(const QString &capturePath);
  static QString StoragePathForCapture(const QString &capturePath);
};
