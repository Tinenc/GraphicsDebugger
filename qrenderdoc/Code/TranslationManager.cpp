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

#include "TranslationManager.h"

#include <QApplication>
#include <QDebug>
#include <QLocale>

TranslationManager::TranslationManager(QApplication &application) : m_Application(application)
{
}

TranslationManager::~TranslationManager()
{
  RemoveTranslators();
}

QString TranslationManager::SimplifiedChineseSetting()
{
  return QStringLiteral("zh_CN");
}

QString TranslationManager::EnglishSetting()
{
  return QStringLiteral("en");
}

QString TranslationManager::SystemSetting()
{
  return QStringLiteral("system");
}

QString TranslationManager::NormalizeSetting(const QString &languageSetting)
{
  if(languageSetting.compare(SimplifiedChineseSetting(), Qt::CaseInsensitive) == 0)
    return SimplifiedChineseSetting();

  if(languageSetting.compare(EnglishSetting(), Qt::CaseInsensitive) == 0)
    return EnglishSetting();

  if(languageSetting.compare(SystemSetting(), Qt::CaseInsensitive) == 0)
    return SystemSetting();

  // Unknown values must never accidentally select a translation. Falling back to the source
  // language also makes configurations written by newer versions safe to open in older versions.
  return EnglishSetting();
}

QString TranslationManager::ResolveLanguage(const QString &languageSetting) const
{
  const QString normalized = NormalizeSetting(languageSetting);

  if(normalized != SystemSetting())
    return normalized;

  if(QLocale::system().language() == QLocale::Chinese)
    return SimplifiedChineseSetting();

  return EnglishSetting();
}

void TranslationManager::RemoveTranslators()
{
  // Translators are searched in reverse installation order, so remove them in reverse order too.
  if(m_ApplicationTranslatorInstalled)
  {
    m_Application.removeTranslator(&m_ApplicationTranslator);
    m_ApplicationTranslatorInstalled = false;
  }

  if(m_QtTranslatorInstalled)
  {
    m_Application.removeTranslator(&m_QtTranslator);
    m_QtTranslatorInstalled = false;
  }
}

bool TranslationManager::ApplyLanguage(const QString &languageSetting)
{
  RemoveTranslators();

  const QString normalized = NormalizeSetting(languageSetting);
  const bool recognized =
      languageSetting.compare(SimplifiedChineseSetting(), Qt::CaseInsensitive) == 0 ||
      languageSetting.compare(EnglishSetting(), Qt::CaseInsensitive) == 0 ||
      languageSetting.compare(SystemSetting(), Qt::CaseInsensitive) == 0;
  if(!recognized)
  {
    qWarning() << "Unknown UI language setting" << languageSetting
               << "- falling back to English";
  }

  const QString language = ResolveLanguage(normalized);
  if(language == EnglishSetting())
  {
    qInfo() << "UI language: English";
    return true;
  }

  // The application catalog is required for a coherent translated UI. If it is absent or invalid,
  // do not load the optional Qt catalog at all. This keeps the fallback path coherent and avoids
  // retaining an unnecessary catalog in memory.
  if(!m_ApplicationTranslator.load(QStringLiteral(":/translations/qrenderdoc_zh_CN.qm")))
  {
    qWarning() << "Could not load the qrenderdoc Simplified Chinese translation; using English";
    return false;
  }

  // The Qt catalog supplies standard widget text such as button and file-dialog labels. It is
  // optional: an application catalog is still useful when a Qt build does not ship this catalog.
  const bool qtTranslationLoaded =
      m_QtTranslator.load(QStringLiteral(":/translations/qt_zh_CN.qm"));

  if(!qtTranslationLoaded)
    qWarning() << "Could not load the Qt Simplified Chinese translation";

  // Install Qt first and the application translator last so application-specific translations have
  // precedence if a context ever overlaps with Qt's catalogs.
  if(qtTranslationLoaded)
    m_QtTranslatorInstalled = m_Application.installTranslator(&m_QtTranslator);

  m_ApplicationTranslatorInstalled =
      m_Application.installTranslator(&m_ApplicationTranslator);

  if(!m_ApplicationTranslatorInstalled)
  {
    RemoveTranslators();
    qWarning() << "Could not install the qrenderdoc Simplified Chinese translation; using English";
    return false;
  }

  qInfo() << "UI language: Simplified Chinese";
  return true;
}
