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

#include "AIConversationStore.h"
#include "AIConversationMerge.h"
#include <QStandardPaths>
#include <cstdlib>

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QSaveFile>
#include <QLockFile>
#include <QMutex>
#include <QMutexLocker>
#include "Code/Interface/QRDInterface.h"

namespace
{
const char StoreSchema[] = "replaylab-ai-conversations/v1";
QMutex baselineMutex;
QHash<QString, QByteArray> localBaselines;

void rememberBaseline(const QString &path, const QByteArray &payload)
{
  QMutexLocker guard(&baselineMutex);
  localBaselines.insert(path, payload);
}

// Base64 expands an opaque payload by four thirds. Leave room for the small JSON envelope and a
// future metadata field while still rejecting unexpectedly large or hostile files before reading.
const qint64 MaximumStoreDocumentBytes =
    ((AIConversationStore::MaximumPayloadBytes + 2) / 3) * 4 + 64 * 1024;

void setError(QString *error, const QString &message)
{
  if(error)
    *error = message;
}

QString storageDirectory()
{
  // i18n-ignore: Fixed per-application storage path, shared with the UI configuration root.
  return ConfigFilePath(QStringLiteral("ReplayLab/conversations"));
}
};    // namespace

QString AIConversationStore::NormalizedCapturePath(const QString &capturePath)
{
  if(capturePath.trimmed().isEmpty())
    return QString();

  const QFileInfo captureInfo(capturePath);
  QString normalised = captureInfo.canonicalFilePath();
  if(normalised.isEmpty())
    normalised = captureInfo.absoluteFilePath();

  normalised = QDir::fromNativeSeparators(QDir::cleanPath(normalised));

#if defined(Q_OS_WIN)
  // Windows paths are case-insensitive, so spelling differences must not split one capture's
  // history into multiple stores.
  normalised = normalised.toLower();
#endif

  return normalised;
}

QString AIConversationStore::StoragePathForCapture(const QString &capturePath)
{
  const QString normalised = NormalizedCapturePath(capturePath);
  const QString directory = storageDirectory();
  if(normalised.isEmpty() || directory.isEmpty())
    return QString();

  const QByteArray digest =
      QCryptographicHash::hash(normalised.toUtf8(), QCryptographicHash::Sha256).toHex();
  return QDir(directory).filePath(QString::fromLatin1(digest) + QStringLiteral(".json"));
}

static bool readConversationStore(const QString &normalised, const QString &storePath,
                                  QByteArray *payload, QString *error)
{
  if(payload)
    payload->clear();
  if(error)
    error->clear();

  if(!payload)
  {
    // i18n-ignore: Internal API contract failure; this branch is not surfaced in the UI.
    setError(error, QStringLiteral("Conversation payload output is null."));
    return false;
  }

  if(normalised.isEmpty())
  {
    setError(error, QCoreApplication::translate(
                        "AIConversationStore",
                        "A capture path is required to load conversation history."));
    return false;
  }
  if(storePath.isEmpty())
  {
    setError(error, QCoreApplication::translate("AIConversationStore",
                                                "The application data directory is unavailable."));
    return false;
  }

  QFile storeFile(storePath);
  if(!storeFile.exists())
    return true;

  if(storeFile.size() < 0 || storeFile.size() > MaximumStoreDocumentBytes)
  {
    setError(error, QCoreApplication::translate(
                        "AIConversationStore",
                        "The conversation store is larger than the allowed limit."));
    return false;
  }

  if(!storeFile.open(QIODevice::ReadOnly))
  {
    setError(error, QCoreApplication::translate("AIConversationStore",
                                                "Unable to open the conversation store: %1")
                        .arg(storeFile.errorString()));
    return false;
  }

  const QByteArray encodedDocument = storeFile.readAll();
  if(storeFile.error() != QFile::NoError)
  {
    setError(error, QCoreApplication::translate("AIConversationStore",
                                                "Unable to read the conversation store: %1")
                        .arg(storeFile.errorString()));
    return false;
  }

  QJsonParseError parseError;
  const QJsonDocument document = QJsonDocument::fromJson(encodedDocument, &parseError);
  if(parseError.error != QJsonParseError::NoError || !document.isObject())
  {
    setError(error, QCoreApplication::translate("AIConversationStore",
                                                "The conversation store is not valid JSON: %1")
                        .arg(parseError.errorString()));
    return false;
  }

  const QJsonObject root = document.object();
  if(root.value(QStringLiteral("schema")).toString() != QString::fromLatin1(StoreSchema))
  {
    setError(error, QCoreApplication::translate(
                        "AIConversationStore",
                        "The conversation store uses an unsupported schema."));
    return false;
  }
  if(root.value(QStringLiteral("capturePath")).toString() != normalised)
  {
    setError(error, QCoreApplication::translate(
                        "AIConversationStore",
                        "The conversation store belongs to a different capture."));
    return false;
  }
  if(root.value(QStringLiteral("payloadEncoding")).toString() != QStringLiteral("base64") ||
     !root.value(QStringLiteral("payload")).isString())
  {
    setError(error, QCoreApplication::translate(
                        "AIConversationStore",
                        "The conversation store payload is missing or invalid."));
    return false;
  }

  const QByteArray encodedPayload =
      root.value(QStringLiteral("payload")).toString().toLatin1();
  const QByteArray decodedPayload = QByteArray::fromBase64(encodedPayload);
  if(decodedPayload.size() > AIConversationStore::MaximumPayloadBytes || decodedPayload.toBase64() != encodedPayload)
  {
    setError(error, QCoreApplication::translate(
                        "AIConversationStore",
                        "The conversation store payload is corrupt or too large."));
    return false;
  }

  *payload = decodedPayload;
  return true;
}

bool AIConversationStore::Load(const QString &capturePath, QByteArray *payload, QString *error)
{
  const QString normalised = NormalizedCapturePath(capturePath);
  const QString storePath = StoragePathForCapture(capturePath);
  if(!readConversationStore(normalised, storePath, payload, error))
    return false;
  rememberBaseline(storePath, *payload);

  QStringList roots;
#if defined(Q_OS_WIN)
  // i18n-ignore: Historical per-user application directories, not display labels.
  const QString roaming = QString::fromWCharArray(_wgetenv(L"APPDATA"));
  const QString local = QString::fromWCharArray(_wgetenv(L"LOCALAPPDATA"));
  if(!roaming.isEmpty())
    for(const QString &app : {QStringLiteral("q_render_soco_doc"), QStringLiteral("qrenderdoc"),
                             QStringLiteral("RenderDocBread")})
      roots << QDir(roaming).filePath(app + QStringLiteral("/ReplayLab/conversations"));
  if(!local.isEmpty())
    roots << QDir(local).filePath(QStringLiteral("RenderDocBread/ui/ReplayLab/conversations"));
#else
  const QString config = QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation);
  for(const QString &app : {QStringLiteral("q_render_soco_doc"), QStringLiteral("qrenderdoc"),
                           QStringLiteral("RenderDocBread")})
    roots << QDir(config).filePath(app + QStringLiteral("/ReplayLab/conversations"));
#endif
#if defined(Q_OS_WIN)
  const Qt::CaseSensitivity pathCase = Qt::CaseInsensitive;
#else
  const Qt::CaseSensitivity pathCase = Qt::CaseSensitive;
#endif
  const QString current = QDir::cleanPath(QFileInfo(storePath).absolutePath());
  bool defaultProfile = false;
  for(const QString &root : roots)
    defaultProfile |= QDir::cleanPath(root).compare(current, pathCase) == 0;
  // An explicitly isolated test/operator profile must not import ordinary user history.
  if(!defaultProfile)
    return true;

  QByteArray combined = *payload;
  bool changed = false;
  roots.removeDuplicates();
  for(const QString &root : roots)
  {
    if(QDir::cleanPath(root).compare(current, pathCase) == 0)
      continue;
    const QString legacyPath = QDir(root).filePath(QFileInfo(storePath).fileName());
    if(!QFileInfo::exists(legacyPath))
      continue;
    QByteArray legacy;
    QString legacyError;
    if(!readConversationStore(normalised, legacyPath, &legacy, &legacyError))
    {
      setError(error, legacyError);
      continue;
    }
    if(legacy.isEmpty() || legacy == combined)
      continue;
    const QJsonDocument oldDocument = QJsonDocument::fromJson(legacy);
    const QJsonDocument newDocument = QJsonDocument::fromJson(combined);
    if(!oldDocument.isObject() || !AIConversationMerge::known(oldDocument.object()) ||
       (!combined.isEmpty() && (!newDocument.isObject() || !AIConversationMerge::known(newDocument.object()))))
    {
      setError(error, QCoreApplication::translate("AIConversationStore",
                                                "The conversation store uses an unsupported schema."));
      continue;
    }
    const QJsonObject merged = combined.isEmpty() ? oldDocument.object() :
        AIConversationMerge::merge(newDocument.object(), oldDocument.object());
    const QByteArray next = QJsonDocument(merged).toJson(QJsonDocument::Compact);
    if(next.size() > MaximumPayloadBytes || merged.value(QStringLiteral("conversations")).toArray().size() > 32)
    {
      setError(error, QCoreApplication::translate("AIConversationStore",
                                                "The conversation store is larger than the allowed limit."));
      continue;
    }
    changed |= next != combined;
    combined = next;
  }
  *payload = combined;
  if(changed && (!error || error->isEmpty()))
  {
    // Preserve the current store as well as the untouched legacy originals.
    if(QFileInfo::exists(storePath))
    {
      const QString backup = storePath + QStringLiteral(".before-history-compat-") +
          QString::fromLatin1(QCryptographicHash::hash(combined, QCryptographicHash::Sha256).toHex().left(12));
      if(!QFileInfo::exists(backup) && !QFile::copy(storePath, backup))
      {
        setError(error, QCoreApplication::translate("AIConversationStore",
                         "Unable to open the conversation store: %1").arg(storePath));
        return true;
      }
    }
    QString saveError;
    if(!Save(capturePath, combined, &saveError))
      setError(error, saveError);
  }
  return true;
}

bool AIConversationStore::Save(const QString &capturePath, const QByteArray &payload, QString *error)
{
  if(error)
    error->clear();

  const QString normalised = NormalizedCapturePath(capturePath);
  const QString storePath = StoragePathForCapture(capturePath);
  if(normalised.isEmpty())
  {
    // i18n-ignore: Save failure is written only to the internal diagnostic log.
    setError(error, QStringLiteral("A capture path is required to save conversation history."));
    return false;
  }
  if(storePath.isEmpty())
  {
    // i18n-ignore: Save failure is written only to the internal diagnostic log.
    setError(error, QStringLiteral("The application data directory is unavailable."));
    return false;
  }
  if(payload.size() > MaximumPayloadBytes)
  {
    // i18n-ignore: Save failure is written only to the internal diagnostic log.
    setError(error, QStringLiteral("Conversation history exceeds the storage limit."));
    return false;
  }

  const QFileInfo storeInfo(storePath);
  QDir directory;
  if(!directory.mkpath(storeInfo.absolutePath()))
  {
    // i18n-ignore: Save failure is written only to the internal diagnostic log.
    setError(error, QStringLiteral("Unable to create the conversation storage directory."));
    return false;
  }
  QLockFile transaction(storePath + QStringLiteral(".update.lock"));
  if(!transaction.tryLock(500))
  {
    // i18n-ignore: Save failure is written only to the internal diagnostic log.
    setError(error, QStringLiteral("Conversation file update is busy."));
    return false;
  }
  QByteArray effective = payload, disk;
  if(!readConversationStore(normalised, storePath, &disk, error)) return false;
  const QJsonObject local = QJsonDocument::fromJson(payload).object();
  const QJsonObject latest = QJsonDocument::fromJson(disk).object();
  if(AIConversationMerge::known(local) && AIConversationMerge::known(latest))
  {
    QByteArray baseline;
    { QMutexLocker guard(&baselineMutex); baseline = localBaselines.value(storePath); }
    effective = QJsonDocument(AIConversationMerge::concurrent(
        local, QJsonDocument::fromJson(baseline).object(), latest)).toJson(QJsonDocument::Compact);
  }
  if(effective.size() > MaximumPayloadBytes)
  {
    // i18n-ignore: Save failure is written only to the internal diagnostic log.
    setError(error, QStringLiteral("Merged conversation history exceeds the storage limit."));
    return false;
  }
  QJsonObject root;
  root.insert(QStringLiteral("schema"), QString::fromLatin1(StoreSchema));
  root.insert(QStringLiteral("capturePath"), normalised);
  root.insert(QStringLiteral("payloadEncoding"), QStringLiteral("base64"));
  root.insert(QStringLiteral("payload"), QString::fromLatin1(effective.toBase64()));

  const QByteArray encodedDocument = QJsonDocument(root).toJson(QJsonDocument::Compact);
  if(encodedDocument.size() > MaximumStoreDocumentBytes)
  {
    // i18n-ignore: Save failure is written only to the internal diagnostic log.
    setError(error, QStringLiteral("Encoded conversation history exceeds the storage limit."));
    return false;
  }

  QSaveFile storeFile(storePath);
  if(!storeFile.open(QIODevice::WriteOnly))
  {
    setError(error,
             // i18n-ignore: Save failure is written only to the internal diagnostic log.
             QStringLiteral("Unable to open the conversation store for writing: %1")
                        .arg(storeFile.errorString()));
    return false;
  }

  if(storeFile.write(encodedDocument) != encodedDocument.size())
  {
    setError(error,
             // i18n-ignore: Save failure is written only to the internal diagnostic log.
             QStringLiteral("Unable to write the conversation store: %1")
                        .arg(storeFile.errorString()));
    storeFile.cancelWriting();
    return false;
  }

  if(!storeFile.commit())
  {
    setError(error,
             // i18n-ignore: Save failure is written only to the internal diagnostic log.
             QStringLiteral("Unable to commit the conversation store: %1")
                        .arg(storeFile.errorString()));
    return false;
  }

  rememberBaseline(storePath, payload);
  return true;
}
