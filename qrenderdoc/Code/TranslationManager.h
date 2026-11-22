/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 Baldur Karlsson
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

#include <QString>
#include <QTranslator>

class QApplication;

// Owns the translators installed for the lifetime of qrenderdoc. Language changes are persisted by
// the settings dialog and applied on the next launch, before any user interface is constructed.
class TranslationManager
{
public:
  explicit TranslationManager(QApplication &application);
  ~TranslationManager();

  // Applies a stable persisted language setting. Returns false only when a requested application
  // translation could not be loaded, in which case no translators are installed and English is
  // used as the safe fallback.
  bool ApplyLanguage(const QString &languageSetting);

  static QString SimplifiedChineseSetting();
  static QString EnglishSetting();
  static QString SystemSetting();
  static QString NormalizeSetting(const QString &languageSetting);

private:
  QString ResolveLanguage(const QString &languageSetting) const;
  void RemoveTranslators();

  QApplication &m_Application;
  QTranslator m_QtTranslator;
  QTranslator m_ApplicationTranslator;
  bool m_QtTranslatorInstalled = false;
  bool m_ApplicationTranslatorInstalled = false;
};
