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

#include "ReplayLabCBufferAliases.h"

#include <algorithm>
#include <errno.h>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QFormLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QMap>
#include <QSaveFile>
#include <QSet>
#include <QStringList>
#include <QThread>
#include <QVBoxLayout>
#include "Code/QRDUtils.h"

namespace
{
// Each matrix cell retains its logical component and packing, not just its scalar storage type.
// Renaming/grouping ordinary vectors is safe; transposing a matrix's meaning is not.
using DisplayLanes = QMap<quint64, QVector<uint32_t>>;

bool displayLanes(const ShaderConstant &constant, quint64 parent, quint64 byteSize,
                  DisplayLanes &lanes, int depth)
{
  const ShaderConstantType &type = constant.type;
  if(depth > 32 || constant.bitFieldSize || constant.bitFieldOffset ||
     type.pointerTypeID != ~0U || type.elements > 65536 || lanes.size() > 65536)
    return false;
  const quint64 elements = qMax(1U, type.elements);
  if(elements > 1 && type.arrayByteStride == 0)
    return false;
  for(quint64 element = 0; element < elements; element++)
  {
    const quint64 base = parent + constant.byteOffset + element * type.arrayByteStride;
    if(base >= byteSize)
      return false;
    if(!type.members.empty())
    {
      for(const ShaderConstant &child : type.members)
        if(!displayLanes(child, base, byteSize, lanes, depth + 1))
          return false;
      continue;
    }
    const uint32_t width = VarTypeByteSize(type.baseType);
    if(width == 0 || width > 8 || type.rows == 0 || type.rows > 4 ||
       type.columns == 0 || type.columns > 4)
      return false;
    const bool matrix = type.rows > 1 && type.columns > 1;
    const uint32_t vectors = matrix ? (type.RowMajor() ? type.rows : type.columns) : 1;
    const uint32_t components = matrix ? (type.RowMajor() ? type.columns : type.rows)
                                       : qMax(type.rows, type.columns);
    if(matrix && type.matrixByteStride < components * width)
      return false;
    for(uint32_t vector = 0; vector < vectors; vector++)
      for(uint32_t component = 0; component < components; component++)
      {
        const quint64 offset = base + vector * type.matrixByteStride + component * width;
        if(offset + width > byteSize || lanes.contains(offset) || lanes.size() >= 65536)
          return false;
        // Also reject partially overlapping typed cells, including differently sized scalars.
        auto after = lanes.lowerBound(offset);
        if(after != lanes.end() && offset + width > after.key())
          return false;
        if(after != lanes.begin())
        {
          auto before = after;
          --before;
          if(before.key() + before.value()[1] > offset)
            return false;
        }
        lanes.insert(offset, {uint32_t(type.baseType), width,
            matrix ? uint32_t(type.rows) : 0U, matrix ? uint32_t(type.columns) : 0U,
            matrix ? uint32_t(type.RowMajor()) : 0U, matrix ? uint32_t(type.matrixByteStride) : 0U,
            matrix ? (type.RowMajor() ? vector : component) : 0U,
            matrix ? (type.RowMajor() ? component : vector) : 0U});
      }
  }
  return true;
}

bool displayBlockCompatible(const ConstantBlock &source, const ConstantBlock &active)
{
  if(source.fixedBindNumber != active.fixedBindNumber ||
     source.fixedBindSetOrSpace != active.fixedBindSetOrSpace ||
     source.bindArraySize != active.bindArraySize || source.bufferBacked != active.bufferBacked ||
     source.inlineDataBytes != active.inlineDataBytes || source.compileConstants ||
     active.compileConstants || active.byteSize == 0 || source.byteSize < active.byteSize)
    return false;
  DisplayLanes sourceLanes, activeLanes;
  for(const ShaderConstant &constant : source.variables)
    if(!displayLanes(constant, 0, source.byteSize, sourceLanes, 0))
      return false;
  for(const ShaderConstant &constant : active.variables)
    if(!displayLanes(constant, 0, active.byteSize, activeLanes, 0))
      return false;
  if(activeLanes.isEmpty())
    return false;
  // A preview can optimise unused source members away. Every remaining physical typed lane
  // must still have exactly the same source identity; missing lanes receive no projected rows.
  for(auto it = activeLanes.begin(); it != activeLanes.end(); ++it)
    if(!sourceLanes.contains(it.key()) || sourceLanes.value(it.key()) != it.value())
      return false;
  return true;
}

bool displayDigestValid(const QString &sha)
{
  if(sha.size() != 64)
    return false;
  for(QChar c : sha)
    if(!((c >= QLatin1Char('0') && c <= QLatin1Char('9')) ||
         (c >= QLatin1Char('a') && c <= QLatin1Char('f'))))
      return false;
  return true;
}
}

bool ReplayLabCBufferDebugDisplayRegistry::Register(
    const QString &owner, ResourceId original, ResourceId replacement,
    const QString &sourceSha, const QString &activeSha, ShaderStage stage,
    uint64_t mutation, const rdcarray<ConstantBlock> &sourceBlocks,
    const rdcarray<ConstantBlock> &activeBlocks)
{
  if(owner.isEmpty() || owner.size() > 256 || original == ResourceId() ||
     replacement == ResourceId() || original == replacement || !mutation ||
     !displayDigestValid(sourceSha) || !displayDigestValid(activeSha) ||
     stage >= ShaderStage::Count || sourceBlocks.size() > 256 || activeBlocks.size() > 256)
    return false;
  Entry entry;
  entry.owner = owner;
  entry.original = original;
  entry.replacement = replacement;
  entry.sourceSha = sourceSha;
  entry.activeSha = activeSha;
  entry.stage = stage;
  entry.mutation = mutation;
  for(const ConstantBlock &active : activeBlocks)
  {
    const ConstantBlock *match = NULL;
    int matches = 0, namedMatches = 0;
    for(const ConstantBlock &source : sourceBlocks)
      if(displayBlockCompatible(source, active))
      {
        matches++;
        if(source.name == active.name)
        {
          match = &source;
          namedMatches++;
        }
        else if(!namedMatches)
          match = &source;
      }
    if(match && (namedMatches == 1 || (namedMatches == 0 && matches == 1)))
      entry.blocks.push_back({*match, active});
  }
  if(entry.blocks.isEmpty())
    return false;
  for(const Entry &existing : m_Entries)
    if(existing.original == original && existing.owner != owner)
      return false;
  Remove(owner);
  if(m_Entries.size() >= 8)
    return false;
  m_Entries.push_back(entry);
  return true;
}

void ReplayLabCBufferDebugDisplayRegistry::Remove(const QString &owner)
{
  for(int i = m_Entries.size() - 1; i >= 0; i--)
    if(m_Entries[i].owner == owner)
      m_Entries.removeAt(i);
}

CBufferDebugDisplaySource ReplayLabCBufferDebugDisplayRegistry::Resolve(
    ResourceId original, ShaderStage stage, const QString &activeSha,
    const ConstantBlock &activeBlock, uint64_t mutation) const
{
  for(const Entry &entry : m_Entries)
  {
    if((entry.original != original && entry.replacement != original) ||
       entry.stage != stage || entry.activeSha != activeSha ||
       entry.mutation != mutation)
      continue;
    for(const BlockPair &pair : entry.blocks)
      if(pair.active == activeBlock && pair.active.fixedBindNumber == activeBlock.fixedBindNumber &&
         pair.active.bindArraySize == activeBlock.bindArraySize)
      {
        CBufferDebugDisplaySource result;
        result.sourceRawSha256 = entry.sourceSha;
        result.sourceBlock = pair.source;
        return result;
      }
  }
  return CBufferDebugDisplaySource();
}

#if defined(Q_OS_WIN)
#include <io.h>
#include <QtCore/qt_windows.h>
#else
#include <errno.h>
#include <string.h>
#include <sys/file.h>
#endif

namespace
{
// i18n-ignore: fixed on-disk schema/protocol identifiers shared with the Python Host.
const QLatin1String AliasSchema("replaylab-cbuffer-aliases/v2");
const QLatin1String SourceUser("user");
const QLatin1String SourceAI("ai");
const int MaxAliasChars = 64;
const int MaxEntries = 4096;
const qint64 MaxFileBytes = 4 * 1024 * 1024;
// Import only the older indented representation whose compact form fits the
// original bound. New writes always use the bounded compact representation.
const qint64 MaxLegacyFileBytes = MaxFileBytes * 4;
const qint64 MaxFieldPageBytes = 512 * 1024;
const qint64 AliasLockRetryMilliseconds = 5000;

QString normalizedAliasRoot(const QString &root)
{
  QString result = QDir::cleanPath(QFileInfo(root).absoluteFilePath());
#if defined(Q_OS_WIN)
  result = QDir::toNativeSeparators(result).toLower();
#endif
  return result;
}

QString aliasFileAt(const QString &root, const QString &layoutSha256)
{
  // i18n-ignore: file extension, not user-facing text.
  return QDir(root).absoluteFilePath(layoutSha256.toLower() + lit(".json"));
}

bool fieldPagePath(const QString &root, const QString &digest, QString &path)
{
  if(digest.size() != 64)
    return false;
  for(QChar character : digest)
    if(!((character >= QLatin1Char('0') && character <= QLatin1Char('9')) ||
         (character >= QLatin1Char('a') && character <= QLatin1Char('f'))))
      return false;
  const QString directory = QDir(root).absoluteFilePath(QStringLiteral("semantic-fields"));
  path = QDir(directory).absoluteFilePath(digest + QStringLiteral(".json"));
  // Reject symlinks and Windows junctions instead of resolving page data outside the store.
  for(const QString &candidate : {directory, path})
  {
    const QFileInfo info(candidate);
    if(info.isSymLink())
      return false;
#if defined(Q_OS_WIN)
    const DWORD attributes = GetFileAttributesW((LPCWSTR)candidate.utf16());
    if(attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_REPARSE_POINT))
      return false;
#endif
  }
  return true;
}

bool readFieldPage(const QString &root, const QJsonObject &descriptor, QByteArray &bytes,
                   QJsonArray &fields)
{
  const QString digest = descriptor.value(QStringLiteral("sha256")).toString();
  const int count = descriptor.value(QStringLiteral("count")).toInt(-1);
  const int size = descriptor.value(QStringLiteral("sizeBytes")).toInt(-1);
  QString path;
  if(count < 1 || count > 256 || size < 1 || size > MaxFieldPageBytes ||
     !fieldPagePath(root, digest, path))
    return false;
  QFile file(path);
  if(file.size() != size || !file.open(QIODevice::ReadOnly))
    return false;
  bytes = file.read(MaxFieldPageBytes + 1);
  if(bytes.size() != size ||
     QString::fromLatin1(QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex()) != digest)
    return false;
  const QJsonDocument document = QJsonDocument::fromJson(bytes);
  fields = document.array();
  return document.isArray() && fields.size() == count;
}

bool readSemanticFields(const QString &root, const QJsonObject &view, QJsonArray &fields)
{
  fields = QJsonArray();
  if(!view.contains(QStringLiteral("fieldChunks")))
  {
    fields = view.value(QStringLiteral("fields")).toArray();
    return fields.size() <= 16384;
  }
  const QJsonArray pages = view.value(QStringLiteral("fieldChunks")).toArray();
  const int count = view.value(QStringLiteral("fieldCount")).toInt(-1);
  if(view.contains(QStringLiteral("fields")) || pages.isEmpty() || pages.size() > 256 ||
     count < 1 || count > 16384)
    return false;
  qint64 totalBytes = 0;
  for(const QJsonValue &page : pages)
  {
    QByteArray bytes;
    QJsonArray values;
    if(!readFieldPage(root, page.toObject(), bytes, values))
      return false;
    totalBytes += bytes.size();
    if(totalBytes > 64 * 1024 * 1024 || fields.size() + values.size() > count)
      return false;
    for(const QJsonValue &value : values)
      fields.push_back(value);
  }
  return fields.size() == count;
}

bool copySemanticPages(const QJsonObject &manifest, const QString &source, const QString &target)
{
  for(const QJsonValue &value : manifest.value(QStringLiteral("semanticViews")).toArray())
  {
    const QJsonObject view = value.toObject();
    if(!view.contains(QStringLiteral("fieldChunks")))
      continue;
    QJsonArray fields;
    if(!readSemanticFields(source, view, fields))
      return false;
    for(const QJsonValue &page : view.value(QStringLiteral("fieldChunks")).toArray())
    {
      const QJsonObject descriptor = page.toObject();
      QByteArray bytes;
      QJsonArray values;
      QString path;
      if(!readFieldPage(source, descriptor, bytes, values) ||
         !fieldPagePath(target, descriptor.value(QStringLiteral("sha256")).toString(), path))
        return false;
      if(QFile::exists(path))
      {
        QByteArray existing;
        if(!readFieldPage(target, descriptor, existing, values))
          return false;
        continue;
      }
      if(!QDir().mkpath(QFileInfo(path).absolutePath()))
        return false;
      QSaveFile file(path);
      if(!file.open(QIODevice::WriteOnly) || file.write(bytes) != bytes.size() || !file.commit())
        return false;
    }
  }
  return true;
}

QString nativeLockError(quint64 errorCode)
{
#if defined(Q_OS_WIN)
  wchar_t *message = NULL;
  const DWORD count = FormatMessageW(
      FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
          FORMAT_MESSAGE_IGNORE_INSERTS,
      NULL, (DWORD)errorCode, 0, (wchar_t *)&message, 0, NULL);
  const QString result =
      count > 0 ? QString::fromWCharArray(message, (int)count).trimmed()
                : QString::number(errorCode);
  if(message != NULL)
    LocalFree(message);
  return result;
#else
  const char *message = strerror((int)errorCode);
  return message != NULL ? QString::fromLocal8Bit(message) : QString::number(errorCode);
#endif
}

// This is the same advisory-lock protocol as aliases.alias_store_transaction:
// <selected root>/.alias-publication.lock, byte 0 length 1 on Windows and flock
// on POSIX. The lock file is retained; ownership is the native file lock, not
// the file's existence.
class AliasStoreLock
{
public:
  explicit AliasStoreLock(const QString &root)
      : m_Path(QDir(root).absoluteFilePath(lit(".alias-publication.lock")))    // i18n-ignore: stable internal lock filename
  {
  }

  ~AliasStoreLock()
  {
    if(m_Locked)
    {
#if defined(Q_OS_WIN)
      const intptr_t nativeHandle = _get_osfhandle(m_File.handle());
      if(nativeHandle != -1)
      {
        OVERLAPPED position = {};
        UnlockFileEx((HANDLE)nativeHandle, 0, 1, 0, &position);
      }
#else
      if(m_File.handle() >= 0)
        flock(m_File.handle(), LOCK_UN);
#endif
    }
    m_File.close();
  }

  bool Acquire(qint64 timeoutMilliseconds, QString &detail)
  {
    QDir().mkpath(QFileInfo(m_Path).absolutePath());
    m_File.setFileName(m_Path);
    if(!m_File.open(QIODevice::ReadWrite))
    {
      detail = m_File.errorString();
      return false;
    }

    // Python's msvcrt.locking locks one byte and requires it to exist.
    if(m_File.size() == 0)
    {
      const char zero = '\0';
      if(m_File.write(&zero, 1) != 1 || !m_File.flush())
      {
        detail = m_File.errorString();
        return false;
      }
    }
    if(!m_File.seek(0))
    {
      detail = m_File.errorString();
      return false;
    }

    QElapsedTimer elapsed;
    elapsed.start();
    quint64 lastError = 0;
    do
    {
#if defined(Q_OS_WIN)
      const intptr_t nativeHandle = _get_osfhandle(m_File.handle());
      if(nativeHandle == -1)
      {
        lastError = (quint64)errno;
        break;
      }
      OVERLAPPED position = {};
      if(LockFileEx((HANDLE)nativeHandle,
                    LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0,
                    &position))
      {
        m_Locked = true;
        return true;
      }
      lastError = (quint64)GetLastError();
      if(lastError != ERROR_LOCK_VIOLATION && lastError != ERROR_IO_PENDING)
        break;
#else
      if(flock(m_File.handle(), LOCK_EX | LOCK_NB) == 0)
      {
        m_Locked = true;
        return true;
      }
      lastError = (quint64)errno;
      if(lastError != EWOULDBLOCK && lastError != EAGAIN && lastError != EINTR)
        break;
#endif
      if(elapsed.elapsed() >= timeoutMilliseconds)
        break;
      QThread::msleep(20);
    } while(true);

    detail = nativeLockError(lastError);
    return false;
  }

  QString Path() const { return m_Path; }

private:
  QFile m_File;
  QString m_Path;
  bool m_Locked = false;
};

class AliasStoreLockSet
{
public:
  ~AliasStoreLockSet()
  {
    while(!m_Locks.isEmpty())
      delete m_Locks.takeLast();
  }

  bool Acquire(QStringList roots, QString &failedPath, QString &detail,
               qint64 timeoutMilliseconds = AliasLockRetryMilliseconds)
  {
    for(QString &root : roots)
    {
      QDir().mkpath(root);
      const QString canonical = QFileInfo(root).canonicalFilePath();
      root = normalizedAliasRoot(canonical.isEmpty() ? root : canonical);
    }
    std::sort(roots.begin(), roots.end());
    roots.erase(std::unique(roots.begin(), roots.end()), roots.end());

    for(const QString &root : roots)
    {
      AliasStoreLock *lock = new AliasStoreLock(root);
      if(!lock->Acquire(timeoutMilliseconds, detail))
      {
        failedPath = lock->Path();
        delete lock;
        return false;
      }
      m_Locks.push_back(lock);
    }
    return true;
  }

private:
  QList<AliasStoreLock *> m_Locks;
};

QString nowUtc()
{
  return QDateTime::currentDateTimeUtc().toString(Qt::ISODate);
}

bool isPlaceholderMember(const QString &name)
{
  // Keep identical to aliases.has_placeholder_members() in the Python Host.
  const QString normalized = name.trimmed().toLower();
  // i18n-ignore: stripped reflection identifiers, not user-visible labels.
  return normalized == QLatin1String("unknown") || normalized.startsWith(QLatin1String("unknown["));
}
}    // namespace

namespace
{
QString replayLabDataRoot()
{
  QByteArray localAppData = qgetenv("LOCALAPPDATA");
  if(localAppData.isEmpty())
    localAppData = qgetenv("XDG_DATA_HOME");
  // i18n-ignore: fixed per-user data home, the same rule the Python side derives.
  const QString localRoot = localAppData.isEmpty() ? QDir::home().filePath(lit(".local/share"))
                                                   : QString::fromLocal8Bit(localAppData);
  // i18n-ignore: fixed ReplayLab per-user data directory, never shown to the user.
  return QDir::cleanPath(QDir(localRoot).absoluteFilePath(lit("ReplayLab")));
}
}    // namespace

QString ReplayLabCBufferAliases::UserStoreRoot()
{
  const QByteArray envRoot = qgetenv("REPLAYLAB_ALIAS_ROOT");
  if(!envRoot.isEmpty())
    return QDir::fromNativeSeparators(QString::fromLocal8Bit(envRoot));
  // i18n-ignore: fixed ReplayLab per-user alias directory, never shown to the user.
  return QDir::cleanPath(QDir(replayLabDataRoot()).absoluteFilePath(lit("aliases/cbuffer")));
}

QString ReplayLabCBufferAliases::ProjectStoreRoot()
{
  // The selected project's share: names published there travel with the project
  // folder, so every .rdc opened under it sees them. Read from the same selection
  // file the Host writes; no selection means no project store.
  QString projectRoot;
  const QByteArray pinned = qgetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
  if(!pinned.isEmpty())
  {
    projectRoot = QDir::fromNativeSeparators(QString::fromLocal8Bit(pinned));
  }
  else
  {
    const QByteArray configured = qgetenv("RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE");
    // i18n-ignore: fixed selection file the Host writes, never shown to the user.
    const QString statePath =
        configured.isEmpty()
            ? QDir(replayLabDataRoot()).absoluteFilePath(lit("config/active-project.json")) // i18n-ignore: on-disk project selection path.
            : QDir::fromNativeSeparators(QString::fromLocal8Bit(configured));
    QFile state(statePath);
    if(!state.exists() || state.size() <= 0 || state.size() > 64 * 1024 ||
       !state.open(QIODevice::ReadOnly))
      return QString();
    const QJsonDocument doc = QJsonDocument::fromJson(state.readAll());
    if(!doc.isObject())
      return QString();
    projectRoot = QDir::fromNativeSeparators(
        doc.object().value(QStringLiteral("projectRoot")).toString());
  }
  if(projectRoot.isEmpty() || !QDir(projectRoot).exists())
    return QString();
  // i18n-ignore: fixed project layout path shared with the Python Host.
  return QDir::cleanPath(QDir(projectRoot).absoluteFilePath(lit("shared/dictionaries/cbuffer-aliases")));
}

QString ReplayLabCBufferAliases::StoreRoot()
{
  const QString project = ProjectStoreRoot();
  return project.isEmpty() ? UserStoreRoot() : project;
}

namespace
{
QStringList aliasTransactionRoots(const QString &selectedRoot)
{
  QStringList roots;
  roots.push_back(selectedRoot);
  const QString userRoot = ReplayLabCBufferAliases::UserStoreRoot();
  if(normalizedAliasRoot(userRoot) != normalizedAliasRoot(selectedRoot))
    roots.push_back(userRoot);
  return roots;
}
}    // namespace

QString ReplayLabCBufferAliases::LayoutSignature(const ConstantBlock &block)
{
  // Must stay byte-identical with aliases.layout_signature() in the Python Host.
  QString signature = QString(block.name);
  for(const ShaderConstant &constant : block.variables)
  {
    if(constant.name.empty())
      return QString();
    signature += QLatin1Char('\n');
    signature += QString(constant.name);
    signature += QLatin1Char('@');
    signature += QString::number(constant.byteOffset);
  }
  return signature;
}

QString ReplayLabCBufferAliases::LayoutHash(const ConstantBlock &block)
{
  const QString signature = LayoutSignature(block);
  if(signature.isEmpty())
    return QString();
  return QString::fromLatin1(
      QCryptographicHash::hash(signature.toUtf8(), QCryptographicHash::Sha256).toHex());
}

bool ReplayLabCBufferAliases::IsValidAlias(const QString &alias, QString *error)
{
  if(alias.trimmed().isEmpty())
  {
    if(error)
      *error = tr("The alias cannot be empty.");
    return false;
  }
  if(alias != alias.trimmed())
  {
    if(error)
      *error = tr("The alias cannot start or end with whitespace.");
    return false;
  }
  if(alias.size() > MaxAliasChars)
  {
    if(error)
      *error = tr("The alias cannot be longer than %1 characters.").arg(MaxAliasChars);
    return false;
  }
  for(const QChar c : alias)
  {
    if(!c.isPrint() || c == QLatin1Char('\n') || c == QLatin1Char('\r') || c == QLatin1Char('\t'))
    {
      if(error)
        *error = tr("The alias cannot contain control characters or line breaks.");
      return false;
    }
  }
  return true;
}

bool ReplayLabCBufferAliases::IsValidScope(const ShaderScope &scope)
{
  if(scope.shaderRawSha256.size() != 64 || scope.layoutSha256.size() != 64 || scope.bind < 0 ||
     scope.space < 0)
    return false;
  for(const QChar ch : scope.shaderRawSha256 + scope.layoutSha256)
    if(!ch.isDigit() && (ch < QLatin1Char('a') || ch > QLatin1Char('f')))
      return false;
  if(!scope.shaderResourceId.isEmpty())
  {
    bool ok = false;
    const qulonglong resourceId = scope.shaderResourceId.toULongLong(&ok, 10);
    if(!ok || resourceId == 0 || QString::number(resourceId) != scope.shaderResourceId)
      return false;
  }
  static const QStringList stages = {
      // i18n-ignore: stable ShaderScope stage protocol identifiers.
      lit("vertex"), lit("hull"), lit("domain"), lit("geometry"),
      // i18n-ignore: stable ShaderScope stage protocol identifiers.
      lit("fragment"), lit("compute"), lit("task"), lit("mesh"),
  };
  return stages.contains(scope.stage);
}

ReplayLabCBufferAliases::ShaderScope ReplayLabCBufferAliases::ReadScope(
    const QJsonObject &object, const QString &layoutSha256)
{
  ShaderScope result;
  const QJsonObject scope = object.value(QStringLiteral("shaderScope")).toObject();
  result.shaderRawSha256 = scope.value(QStringLiteral("shaderRawSha256")).toString().toLower();
  result.shaderResourceId = scope.value(QStringLiteral("shaderResourceId")).toString();
  result.stage = scope.value(QStringLiteral("stage")).toString().toLower();
  if(result.stage == QLatin1String("pixel"))
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    result.stage = lit("fragment");
  result.bind = (qint64)scope.value(QStringLiteral("bind")).toDouble(-1.0);
  result.space = (qint64)scope.value(QStringLiteral("space")).toDouble(-1.0);
  result.layoutSha256 = scope.value(QStringLiteral("layoutSha256")).toString().toLower();
  if(!IsValidScope(result) || result.layoutSha256 != layoutSha256.toLower())
    return ShaderScope();
  return result;
}

void ReplayLabCBufferAliases::WriteScope(QJsonObject &object, const ShaderScope &scope)
{
  if(!IsValidScope(scope))
    return;
  QJsonObject value;
  value.insert(QStringLiteral("shaderRawSha256"), scope.shaderRawSha256);
  if(!scope.shaderResourceId.isEmpty())
    value.insert(QStringLiteral("shaderResourceId"), scope.shaderResourceId);
  value.insert(QStringLiteral("stage"), scope.stage);
  value.insert(QStringLiteral("bind"), (double)scope.bind);
  value.insert(QStringLiteral("space"), (double)scope.space);
  value.insert(QStringLiteral("layoutSha256"), scope.layoutSha256);
  object.insert(QStringLiteral("shaderScope"), value);
}

bool ReplayLabCBufferAliases::ReadCurrent(const QString &layoutSha256, QList<Entry> &entries,
                                          QList<BlockAliasEntry> &blockAliases, QString *error)
{
  const QString selectedRoot = StoreRoot();
  // Existing records are atomically replaced by both writers. A display refresh can read one
  // complete version without waiting for the publication lock on the UI thread. Only first
  // adoption needs the transaction below; mutations retain their exclusive lock.
  const QString selectedFile = aliasFileAt(selectedRoot, layoutSha256);
  if(QFileInfo::exists(selectedFile))
    return ReadFile(selectedFile, entries, blockAliases, error);
  // An unnamed layout has nothing to adopt. Do not wait on a publication for
  // another block merely to display the original reflection.
  if(!QFileInfo::exists(aliasFileAt(UserStoreRoot(), layoutSha256)))
    return ReadFile(selectedFile, entries, blockAliases, error);
  AliasStoreLockSet transaction;
  QString lockPath;
  QString lockDetail;
  if(!transaction.Acquire(aliasTransactionRoots(selectedRoot), lockPath, lockDetail))
  {
    if(error)
      *error = tr("The alias file %1 could not be read: %2")
                   .arg(QDir::toNativeSeparators(lockPath), lockDetail);
    return false;
  }
  return ReadCurrentAt(selectedRoot, layoutSha256, entries, blockAliases, error);
}

bool ReplayLabCBufferAliases::ReadCurrentAt(const QString &storeRoot,
                                            const QString &layoutSha256,
                                            QList<Entry> &entries,
                                            QList<BlockAliasEntry> &blockAliases, QString *error)
{
  // One block layout, one file. Under a project that file is the project's; a
  // per-user file for the same layout (names made before the project existed) is
  // adopted into the project once and not consulted again. The caller owns the
  // locks for both roots across adoption and the following read/modify/write.
  const QString target = aliasFileAt(storeRoot, layoutSha256);
  const QString userFile = aliasFileAt(UserStoreRoot(), layoutSha256);
  if(normalizedAliasRoot(target) != normalizedAliasRoot(userFile) && !QFile::exists(target) &&
     QFile::exists(userFile))
  {
    // Validate the source before creating the project record. Invalid or empty
    // legacy files must not become a permanent target merely because they exist.
    QList<Entry> adoptedEntries;
    QList<BlockAliasEntry> adoptedBlockAliases;
    if(!ReadFile(userFile, adoptedEntries, adoptedBlockAliases, error))
      return false;
    QFile source(userFile);
    if(!source.open(QIODevice::ReadOnly))
    {
      if(error)
        *error = tr("The alias file %1 could not be read: %2")
                     .arg(QDir::toNativeSeparators(userFile), source.errorString());
      return false;
    }
    QJsonParseError parseError;
    QJsonDocument sourceDocument = QJsonDocument::fromJson(source.readAll(), &parseError);
    if(parseError.error != QJsonParseError::NoError || !sourceDocument.isObject())
    {
      if(error)
        *error = tr("The alias file %1 is not valid JSON: %2")
                     .arg(QDir::toNativeSeparators(userFile), parseError.errorString());
      return false;
    }
    QJsonObject adopted = sourceDocument.object();
    if(adoptedEntries.isEmpty() && adoptedBlockAliases.isEmpty() &&
       adopted.value(QStringLiteral("semanticViews")).toArray().isEmpty())
      return ReadFile(target, entries, blockAliases, error);
    adopted.insert(QStringLiteral("adoptedFrom"), QDir::toNativeSeparators(userFile));
    adopted.insert(QStringLiteral("adoptedAt"), nowUtc());
    if(!copySemanticPages(adopted, QFileInfo(userFile).absolutePath(), QFileInfo(target).absolutePath()))
    {
      if(error)
        *error = tr("The semantic fields referenced by alias file %1 are missing or invalid.")
                     .arg(QDir::toNativeSeparators(userFile));
      return false;
    }
    QDir().mkpath(QFileInfo(target).absolutePath());
    QSaveFile destination(target);
    if(!destination.open(QIODevice::WriteOnly | QIODevice::Truncate))
    {
      if(error)
        *error = tr("The alias file %1 could not be written: %2")
                     .arg(QDir::toNativeSeparators(target), destination.errorString());
      return false;
    }
    const QByteArray serialized = QJsonDocument(adopted).toJson(QJsonDocument::Compact);
    if(serialized.size() > MaxFileBytes)
    {
      if(error)
        *error = tr("The alias file %1 has an invalid size.").arg(QDir::toNativeSeparators(target));
      destination.cancelWriting();
      return false;
    }
    if(destination.write(serialized) != serialized.size())
    {
      if(error)
        *error = tr("The alias file %1 could not be written: %2")
                     .arg(QDir::toNativeSeparators(target), destination.errorString());
      destination.cancelWriting();
      return false;
    }
    if(!destination.commit())
    {
      if(error)
        *error = tr("The alias file %1 could not be written: %2")
                     .arg(QDir::toNativeSeparators(target), destination.errorString());
      return false;
    }
  }
  return ReadFile(target, entries, blockAliases, error);
}

bool ReplayLabCBufferAliases::ReadFile(const QString &path, QList<Entry> &entries,
                                       QList<BlockAliasEntry> &blockAliases, QString *error)
{
  entries.clear();
  blockAliases.clear();
  QFile file(path);
  if(!file.exists())
    return true;
  if(file.size() <= 0 || file.size() > MaxLegacyFileBytes)
  {
    if(error)
      *error = tr("The alias file %1 has an invalid size.").arg(QDir::toNativeSeparators(path));
    return false;
  }
  if(!file.open(QIODevice::ReadOnly))
  {
    if(error)
      *error = tr("The alias file %1 could not be read: %2")
                   .arg(QDir::toNativeSeparators(path), file.errorString());
    return false;
  }
  QJsonParseError parseError;
  const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &parseError);
  if(parseError.error != QJsonParseError::NoError || !doc.isObject())
  {
    if(error)
      *error = tr("The alias file %1 is not valid JSON: %2")
                   .arg(QDir::toNativeSeparators(path), parseError.errorString());
    return false;
  }
  const QJsonObject root = doc.object();
  if(file.size() > MaxFileBytes && doc.toJson(QJsonDocument::Compact).size() > MaxFileBytes)
  {
    if(error)
      *error = tr("The alias file %1 has an invalid size.").arg(QDir::toNativeSeparators(path));
    return false;
  }
  if(root.value(QStringLiteral("schema")).toString() != AliasSchema)
  {
    if(error)
      *error = tr("The alias file %1 has an unknown schema.").arg(QDir::toNativeSeparators(path));
    return false;
  }
  const QString layoutSha256 = QFileInfo(path).completeBaseName().toLower();
  auto appendBlockAlias = [&](const QJsonObject &item) {
    const QString alias = item.value(QStringLiteral("alias")).toString();
    if(!IsValidAlias(alias) || blockAliases.count() >= MaxEntries)
      return;
    BlockAliasEntry entry;
    entry.value.alias = alias;
    entry.value.source = item.value(QStringLiteral("source")).toString() == SourceUser
                             ? SourceUser
                             : SourceAI;
    entry.updatedAt = item.value(QStringLiteral("updatedAt")).toString();
    entry.shaderScope = ReadScope(item, layoutSha256);
    if(entry.value.source != SourceUser && item.contains(QStringLiteral("shaderScope")) &&
       !IsValidScope(entry.shaderScope))
      return;
    blockAliases.push_back(entry);
  };
  const QJsonObject blockAliasObject = root.value(QStringLiteral("blockAlias")).toObject();
  if(!blockAliasObject.isEmpty())
    appendBlockAlias(blockAliasObject);
  for(const QJsonValue &value : root.value(QStringLiteral("blockAliases")).toArray())
  {
    appendBlockAlias(value.toObject());
    if(blockAliases.count() >= MaxEntries)
      break;
  }
  for(const QJsonValue &value : root.value(QStringLiteral("aliases")).toArray())
  {
    const QJsonObject item = value.toObject();
    Entry entry;
    entry.path = item.value(QStringLiteral("path")).toString();
    entry.offset = (qint64)item.value(QStringLiteral("offset")).toDouble(-1.0);
    entry.alias = item.value(QStringLiteral("alias")).toString();
    entry.source =
        item.value(QStringLiteral("source")).toString() == SourceUser ? SourceUser : SourceAI;
    entry.updatedAt = item.value(QStringLiteral("updatedAt")).toString();
    entry.shaderScope = ReadScope(item, layoutSha256);
    if(entry.source != SourceUser && item.contains(QStringLiteral("shaderScope")) &&
       !IsValidScope(entry.shaderScope))
      continue;
    if(entry.path.isEmpty() || !IsValidAlias(entry.alias))
      continue;
    entries.push_back(entry);
    if(entries.count() >= MaxEntries)
      break;
  }
  return true;
}

bool ReplayLabCBufferAliases::WriteFileTo(const QString &path, const QList<Entry> &entries,
                                          const QList<BlockAliasEntry> &blockAliases,
                                          QString *error) const
{
  QDir().mkpath(QFileInfo(path).absolutePath());
  QJsonArray array;
  for(const Entry &entry : entries)
  {
    QJsonObject item;
    item.insert(QStringLiteral("path"), entry.path);
    item.insert(QStringLiteral("offset"), (double)entry.offset);
    item.insert(QStringLiteral("alias"), entry.alias);
    item.insert(QStringLiteral("source"), entry.source);
    item.insert(QStringLiteral("updatedAt"), entry.updatedAt);
    WriteScope(item, entry.shaderScope);
    array.push_back(item);
  }
  QJsonArray members;
  for(const Member &member : m_Members)
  {
    QJsonObject item;
    item.insert(QStringLiteral("name"), member.name);
    item.insert(QStringLiteral("offset"), (double)member.offset);
    members.push_back(item);
  }
  QJsonObject root;
  // User renames must not erase the Host's display projection or its evidence. The caller
  // owns the same publication lock as the Host, so this is part of the atomic update.
  QFile previous(path);
  if(previous.size() > 0 && previous.size() <= MaxLegacyFileBytes && previous.open(QIODevice::ReadOnly))
  {
    const QJsonObject saved = QJsonDocument::fromJson(previous.readAll()).object();
    if(saved.value(QStringLiteral("schema")).toString() == AliasSchema &&
       saved.contains(QStringLiteral("semanticViews")))
      root.insert(QStringLiteral("semanticViews"), saved.value(QStringLiteral("semanticViews")));
    previous.close();
  }
  root.insert(QStringLiteral("schema"), AliasSchema);
  root.insert(QStringLiteral("layoutSha256"), m_LayoutSha256);
  root.insert(QStringLiteral("block"), m_BlockName);
  root.insert(QStringLiteral("members"), members);
  root.insert(QStringLiteral("updatedAt"), nowUtc());
  root.insert(QStringLiteral("aliases"), array);
  QJsonArray scopedBlockAliases;
  for(const BlockAliasEntry &entry : blockAliases)
  {
    if(!entry.value.valid())
      continue;
    QJsonObject item;
    item.insert(QStringLiteral("alias"), entry.value.alias);
    item.insert(QStringLiteral("source"), entry.value.source);
    item.insert(QStringLiteral("updatedAt"), entry.updatedAt);
    WriteScope(item, entry.shaderScope);
    if(IsValidScope(entry.shaderScope))
      scopedBlockAliases.push_back(item);
    else if(!root.contains(QStringLiteral("blockAlias")))
      root.insert(QStringLiteral("blockAlias"), item);
  }
  if(!scopedBlockAliases.isEmpty())
    root.insert(QStringLiteral("blockAliases"), scopedBlockAliases);

  QSaveFile file(path);
  if(!file.open(QIODevice::WriteOnly | QIODevice::Truncate))
  {
    if(error)
      *error = tr("The alias file %1 could not be written: %2")
                   .arg(QDir::toNativeSeparators(path), file.errorString());
    return false;
  }
  const QByteArray serialized = QJsonDocument(root).toJson(QJsonDocument::Compact);
  if(serialized.size() > MaxFileBytes)
  {
    if(error)
      *error = tr("The alias file %1 has an invalid size.").arg(QDir::toNativeSeparators(path));
    file.cancelWriting();
    return false;
  }
  if(file.write(serialized) != serialized.size())
  {
    if(error)
      *error = tr("The alias file %1 could not be written: %2")
                   .arg(QDir::toNativeSeparators(path), file.errorString());
    file.cancelWriting();
    return false;
  }
  if(!file.commit())
  {
    if(error)
      *error = tr("The alias file %1 could not be written: %2")
                   .arg(QDir::toNativeSeparators(path), file.errorString());
    return false;
  }
  return true;
}

void ReplayLabCBufferAliases::Load(const ConstantBlock *block, const QString &shaderRawSha256,
                                   const QString &stage, qint64 bind, qint64 space,
                                   const QString &shaderResourceId,
                                   bool)
{
  m_LayoutSha256.clear();
  m_BlockName.clear();
  m_Members.clear();
  m_Entries.clear();
  m_PathLookup.clear();
  m_OffsetLookup.clear();
  m_HasPlaceholderMembers = false;
  m_PhysicalOffsetAliases = false;
  m_ShaderScope = ShaderScope();
  m_BlockAliases.clear();
  m_SemanticFields.clear();
  m_UnusedComponents.clear();
  m_BlockByteSize = 0;
  if(block == NULL)
    return;
  m_LayoutSha256 = LayoutHash(*block);
  if(m_LayoutSha256.isEmpty())
    return;
  m_BlockName = QString(block->name);
  m_BlockByteSize = block->byteSize;
  m_ShaderScope.shaderRawSha256 = shaderRawSha256.trimmed().toLower();
  m_ShaderScope.shaderResourceId = shaderResourceId.trimmed();
  m_ShaderScope.stage = stage.trimmed().toLower();
  if(m_ShaderScope.stage == QLatin1String("pixel"))
    // i18n-ignore: stable ShaderScope stage protocol identifier.
    m_ShaderScope.stage = lit("fragment");
  m_ShaderScope.bind = bind;
  m_ShaderScope.space = space;
  m_ShaderScope.layoutSha256 = m_LayoutSha256;
  if(!IsValidScope(m_ShaderScope))
    m_ShaderScope = ShaderScope();
  for(const ShaderConstant &constant : block->variables)
  {
    Member member;
    member.name = QString(constant.name);
    member.offset = constant.byteOffset;
    m_HasPlaceholderMembers |= isPlaceholderMember(member.name);
    m_Members.push_back(member);
  }
  ReadCurrent(m_LayoutSha256, m_Entries, m_BlockAliases, NULL);
  RebuildLookup();
  LoadSemanticFields();
}

void ReplayLabCBufferAliases::LoadSemanticFields()
{
  // Atomic publication makes this read a complete version. Semantic views are always Shader
  // scoped, even when the underlying reflection happens to have meaningful member names.
  QFile file(aliasFileAt(StoreRoot(), m_LayoutSha256));
  if(file.size() <= 0 || file.size() > MaxLegacyFileBytes || !file.open(QIODevice::ReadOnly))
    return;
  const QJsonDocument document = QJsonDocument::fromJson(file.readAll());
  if(file.size() > MaxFileBytes && document.toJson(QJsonDocument::Compact).size() > MaxFileBytes)
    return;
  const QJsonObject root = document.object();
  if(root.value(QStringLiteral("schema")).toString() != AliasSchema)
    return;
  QJsonObject selected;
  for(const QJsonValue &value : root.value(QStringLiteral("semanticViews")).toArray())
  {
    const QJsonObject view = value.toObject();
    const ShaderScope scope = ReadScope(view, m_LayoutSha256);
    // Host publishes an exact active-bytecode mirror. A later replacement with the same
    // physical layout can use the bytes differently, so legacy alias RID fallback is not
    // sufficient evidence for semantic regrouping.
    if(scope.shaderRawSha256 == m_ShaderScope.shaderRawSha256 && ScopeMatches(scope) &&
       (selected.value(QStringLiteral("source")).toString() != SourceUser ||
        view.value(QStringLiteral("source")).toString() == SourceUser))
      selected = view;
  }
  QSet<qint64> usedOffsets;
  QSet<QString> names;
  QJsonArray fields;
  if(!readSemanticFields(QFileInfo(file).absolutePath(), selected, fields))
    return;
  for(const QJsonValue &value : fields)
  {
    const QJsonObject item = value.toObject();
    ReplayLabCBufferSemanticField field;
    field.name = item.value(QStringLiteral("name")).toString();
    field.type = item.value(QStringLiteral("type")).toString();
    field.status = item.value(QStringLiteral("status")).toString();
    field.provenance = item.value(QStringLiteral("provenance")).toString();
    field.evidence = item.value(QStringLiteral("evidence")).toString().left(4096);
    field.description = item.value(QStringLiteral("description")).toString().left(4096);
    field.sourceSha256 = selected.value(QStringLiteral("sourceSha256")).toString();
    field.source = selected.value(QStringLiteral("source")).toString();
    QString base = field.type;
    int width = 1;
    const QChar suffix = base.isEmpty() ? QChar() : base.at(base.size() - 1);
    if(suffix >= QLatin1Char('2') && suffix <= QLatin1Char('4'))
    {
      width = suffix.digitValue();
      base.chop(1);
    }
    if(!IsValidAlias(field.name) || names.contains(field.name) ||
       (base != QLatin1String("float") && base != QLatin1String("int") &&
        base != QLatin1String("uint")) ||
       (field.status != QLatin1String("confirmed") && field.status != QLatin1String("inferred")) ||
       (field.provenance != QLatin1String("asset") && field.provenance != QLatin1String("shader") &&
        field.provenance != QLatin1String("combined")))
      continue;
    const QJsonArray components = item.value(QStringLiteral("components")).toArray();
    if(components.size() != width)
      continue;
    bool valid = true;
    for(const QJsonValue &component : components)
    {
      const double offset = component.toObject().value(QStringLiteral("offset")).toDouble(-1);
      if(offset < 0 || offset > UINT32_MAX - 3 || offset != double(qint64(offset)) ||
         qint64(offset) % 4 != 0 || usedOffsets.contains(qint64(offset)) ||
         field.offsets.contains(qint64(offset)))
      {
        valid = false;
        break;
      }
      field.offsets.push_back(qint64(offset));
    }
    if(!valid)
      continue;
    names.insert(field.name);
    for(qint64 offset : field.offsets)
      usedOffsets.insert(offset);
    m_SemanticFields.push_back(field);
  }
  const QJsonObject usage = selected.value(QStringLiteral("usageReview")).toObject();
  if(usage.value(QStringLiteral("schema")).toString() !=
         QLatin1String("bread-cbuffer-usage-review/v1") ||
     usage.value(QStringLiteral("evidence")).toString().trimmed().isEmpty() ||
     selected.value(QStringLiteral("sourceSha256")).toString().size() != 64)
    return;
  QJsonArray unused;
  if(usage.contains(QStringLiteral("unusedComponentChunks")))
  {
    // Large source-scoped usage observations share the same immutable page format as names.
    // Keeping them inline made repeated compatible Shader views overflow the 4 MiB manifest.
    if(usage.contains(QStringLiteral("unusedComponents")) ||
       !readSemanticFields(QFileInfo(file).absolutePath(),
           {{QStringLiteral("fieldChunks"), usage.value(QStringLiteral("unusedComponentChunks"))},
            {QStringLiteral("fieldCount"), usage.value(QStringLiteral("unusedComponentCount"))}}, unused))
      return;
  }
  else
    unused = usage.value(QStringLiteral("unusedComponents")).toArray();
  for(const QJsonValue &value : unused)
  {
    const QJsonObject item = value.toObject();
    const double offset = item.value(QStringLiteral("offset")).toDouble(-1);
    const QString type = item.value(QStringLiteral("type")).toString();
    if(offset < 0 || offset > UINT32_MAX - 3 || offset != double(qint64(offset)) ||
       qint64(offset) % 4 || offset + 4 > m_BlockByteSize || usedOffsets.contains(qint64(offset)) ||
       (type != QLatin1String("float") && type != QLatin1String("int") &&
        type != QLatin1String("uint")))
      continue;
    m_UnusedComponents.insert(qint64(offset), type);
  }
}

bool ReplayLabCBufferAliases::ScopeMatches(const ShaderScope &scope) const
{
  if(!IsValidScope(scope) || !IsValidScope(m_ShaderScope) ||
     scope.stage != m_ShaderScope.stage || scope.bind != m_ShaderScope.bind ||
     scope.space != m_ShaderScope.space || scope.layoutSha256 != m_ShaderScope.layoutSha256)
    return false;
  // RID values are local to one capture and can be reused by unrelated Shaders in another RDC.
  // Host publishes exact original/candidate mirrors; only matching bytes and binding/layout may
  // select a persistent AI name. A replaced resource with a coincident RID is not a fallback.
  return scope.shaderRawSha256 == m_ShaderScope.shaderRawSha256;
}

bool ReplayLabCBufferAliases::EntryVisible(const Entry &entry) const
{
  if(entry.source == SourceUser)
    return true;
  if(IsValidScope(entry.shaderScope))
    return ScopeMatches(entry.shaderScope);
  return !m_HasPlaceholderMembers;
}

void ReplayLabCBufferAliases::RebuildLookup()
{
  m_PathLookup.clear();
  m_OffsetLookup.clear();
  m_PathLookup.reserve(m_Entries.size());
  m_OffsetLookup.reserve(m_Entries.size());
  for(int i = 0; i < m_Entries.count(); i++)
  {
    const Entry &entry = m_Entries[i];
    if(!EntryVisible(entry))
      continue;
    // Keep the existing precedence: first visible record, with a user override
    // winning over AI. Resolve thousands of reflected leaves without rescanning
    // every alias for each tree row.
    const int pathIndex = m_PathLookup.value(entry.path, -1);
    if(pathIndex < 0 || (m_Entries[pathIndex].source != SourceUser && entry.source == SourceUser))
      m_PathLookup.insert(entry.path, i);
    const int offsetIndex = m_OffsetLookup.value(entry.offset, -1);
    if(entry.offset >= 0 && (offsetIndex < 0 ||
       (m_Entries[offsetIndex].source != SourceUser && entry.source == SourceUser)))
      m_OffsetLookup.insert(entry.offset, i);
  }
}

void ReplayLabCBufferAliases::PreferUserAliases(const ReplayLabCBufferAliases &current)
{
  // Source-file user aliases already outrank AI. A user's explicit name on the displayed
  // layout takes precedence over both when a temporary source layout differs.
  for(int i = current.m_Entries.size() - 1; i >= 0; i--)
    if(current.m_Entries[i].source == SourceUser)
      m_Entries.prepend(current.m_Entries[i]);
  for(int i = current.m_BlockAliases.size() - 1; i >= 0; i--)
    if(current.m_BlockAliases[i].value.source == SourceUser)
      m_BlockAliases.prepend(current.m_BlockAliases[i]);
  RebuildLookup();
}

ReplayLabCBufferAlias ReplayLabCBufferAliases::Resolve(const ReplayLabCBufferAliasKey &key) const
{
  ReplayLabCBufferAlias result;
  if(key.path.isEmpty() || !Loaded())
    return result;

  // Temporary layouts can reuse a source name at another offset. Only the explicitly proven
  // physical leaf address may carry a name across that layout boundary.
  int index = m_PhysicalOffsetAliases ? -1 : m_PathLookup.value(key.path, -1);
  if(index < 0 && key.offset >= 0)
  {
    // The Host writes decompiler names, which can differ from RenderDoc's reflected names for
    // nested members; a leaf at the same byte offset of the same block is the same variable.
    index = m_OffsetLookup.value(key.offset, -1);
  }
  if(index >= 0)
  {
    result.alias = m_Entries[index].alias;
    result.source = m_Entries[index].source;
  }
  return result;
}

ReplayLabCBufferAlias ReplayLabCBufferAliases::BlockAlias() const
{
  for(const BlockAliasEntry &entry : m_BlockAliases)
    if(entry.value.source == SourceUser)
      return entry.value;
  for(const BlockAliasEntry &entry : m_BlockAliases)
    if(entry.value.source != SourceUser && IsValidScope(entry.shaderScope) &&
       ScopeMatches(entry.shaderScope))
      return entry.value;
  if(!m_HasPlaceholderMembers)
    for(const BlockAliasEntry &entry : m_BlockAliases)
      if(entry.value.source != SourceUser && !IsValidScope(entry.shaderScope))
        return entry.value;
  return ReplayLabCBufferAlias();
}

bool ReplayLabCBufferAliases::Update(const ReplayLabCBufferAliasKey &key, const QString &alias,
                                     QString *error)
{
  if(key.path.isEmpty())
    return false;
  if(!Loaded())
  {
    if(error)
      *error = tr("This constant cannot be aliased because its block layout is unknown.");
    return false;
  }
  QList<Entry> entries;
  QList<BlockAliasEntry> blockAliases;
  const QString selectedRoot = StoreRoot();
  AliasStoreLockSet transaction;
  QString lockPath;
  QString lockDetail;
  if(!transaction.Acquire(aliasTransactionRoots(selectedRoot), lockPath, lockDetail))
  {
    if(error)
      *error = tr("The alias file %1 could not be written: %2")
                   .arg(QDir::toNativeSeparators(lockPath), lockDetail);
    return false;
  }
  if(!ReadCurrentAt(selectedRoot, m_LayoutSha256, entries, blockAliases, error))
    return false;

  // A layout-wide user record wins at display time. Keep physically scoped AI
  // records so removing the user override can reveal the matching Shader's name
  // without deleting names belonging to other Shaders with this placeholder layout.
  bool changed = false;
  for(int i = entries.count() - 1; i >= 0; i--)
  {
    if(entries[i].path == key.path && entries[i].source == SourceUser)
    {
      entries.removeAt(i);
      changed = true;
    }
  }
  if(alias.isEmpty() && !changed)
    return true;
  if(!alias.isEmpty())
  {
    if(entries.count() >= MaxEntries)
    {
      if(error)
        *error = tr("The alias file already holds %1 entries.").arg(MaxEntries);
      return false;
    }
    Entry entry;
    entry.path = key.path;
    entry.offset = key.offset;
    entry.alias = alias;
    entry.source = SourceUser;
    entry.updatedAt = nowUtc();
    entries.push_back(entry);
  }
  if(!WriteFileTo(aliasFileAt(selectedRoot, m_LayoutSha256), entries, blockAliases, error))
    return false;
  m_Entries = entries;
  m_BlockAliases = blockAliases;
  RebuildLookup();
  return true;
}

bool ReplayLabCBufferAliases::Set(const ReplayLabCBufferAliasKey &key, const QString &alias,
                                  QString *error)
{
  if(!IsValidAlias(alias, error))
    return false;
  return Update(key, alias, error);
}

bool ReplayLabCBufferAliases::Remove(const ReplayLabCBufferAliasKey &key, QString *error)
{
  return Update(key, QString(), error);
}

bool ReplayLabCBufferAliases::UpdateBlockAlias(const QString &alias, QString *error)
{
  if(!Loaded())
  {
    if(error)
      *error = tr("This constant block cannot be aliased because its layout is unknown.");
    return false;
  }

  QList<Entry> entries;
  QList<BlockAliasEntry> blockAliases;
  const QString selectedRoot = StoreRoot();
  AliasStoreLockSet transaction;
  QString lockPath;
  QString lockDetail;
  if(!transaction.Acquire(aliasTransactionRoots(selectedRoot), lockPath, lockDetail))
  {
    if(error)
      *error = tr("The alias file %1 could not be written: %2")
                   .arg(QDir::toNativeSeparators(lockPath), lockDetail);
    return false;
  }
  if(!ReadCurrentAt(selectedRoot, m_LayoutSha256, entries, blockAliases, error))
    return false;
  const bool hadUserAlias = std::any_of(
      blockAliases.begin(), blockAliases.end(),
      [](const BlockAliasEntry &entry) { return entry.value.source == SourceUser; });
  if(alias.isEmpty() && !hadUserAlias)
    return true;
  for(int i = blockAliases.count() - 1; i >= 0; i--)
  {
    if(blockAliases[i].value.source == SourceUser)
      blockAliases.removeAt(i);
  }
  if(!alias.isEmpty())
  {
    BlockAliasEntry entry;
    entry.value.alias = alias;
    entry.value.source = SourceUser;
    entry.updatedAt = nowUtc();
    blockAliases.push_back(entry);
  }
  if(!WriteFileTo(aliasFileAt(selectedRoot, m_LayoutSha256), entries, blockAliases, error))
    return false;
  m_Entries = entries;
  m_BlockAliases = blockAliases;
  RebuildLookup();
  return true;
}

bool ReplayLabCBufferAliases::SetBlockAlias(const QString &alias, QString *error)
{
  if(!IsValidAlias(alias, error))
    return false;
  return UpdateBlockAlias(alias, error);
}

bool ReplayLabCBufferAliases::RemoveBlockAlias(QString *error)
{
  return UpdateBlockAlias(QString(), error);
}

bool ReplayLabCBufferAliases::PromptAlias(QWidget *parent, const QString &original,
                                          const ReplayLabCBufferAlias &current, QString &alias)
{
  QDialog dialog(parent);
  dialog.setWindowTitle(tr("Rename Constant (Alias)"));

  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *intro = new QLabel(
      tr("The alias is only a display name. It applies to every shader and every capture whose "
         "constant block has this name and layout; the shader, its reflection and every "
         "workspace source keep the original name."),
      &dialog);
  intro->setWordWrap(true);
  layout->addWidget(intro);

  QFormLayout *form = new QFormLayout();
  QLineEdit *originalEdit = new QLineEdit(original, &dialog);
  originalEdit->setReadOnly(true);
  form->addRow(tr("Original name:"), originalEdit);

  QLineEdit *aliasEdit = new QLineEdit(current.alias, &dialog);
  aliasEdit->setMaxLength(MaxAliasChars);
  aliasEdit->setPlaceholderText(tr("Leave empty to remove the alias"));
  form->addRow(tr("Alias:"), aliasEdit);

  QLabel *sourceLabel = new QLabel(&dialog);
  if(current.valid())
    sourceLabel->setText(current.source == SourceUser ? tr("Current alias was set by you.")
                                                      : tr("Current alias was proposed by AI."));
  else
    sourceLabel->setText(tr("No alias is set for this constant."));
  form->addRow(QString(), sourceLabel);
  layout->addLayout(form);

  QLabel *errorLabel = new QLabel(&dialog);
  errorLabel->setWordWrap(true);
  errorLabel->setVisible(false);
  layout->addWidget(errorLabel);

  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  layout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, [&dialog, aliasEdit, errorLabel]() {
    QString problem;
    const QString text = aliasEdit->text();
    if(text.trimmed().isEmpty() || IsValidAlias(text, &problem))
    {
      dialog.accept();
      return;
    }
    errorLabel->setText(problem);
    errorLabel->setVisible(true);
  });

  aliasEdit->setFocus();
  aliasEdit->selectAll();
  dialog.setMinimumWidth(420);

  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return false;

  alias = aliasEdit->text().trimmed();
  return true;
}

bool ReplayLabCBufferAliases::PromptBlockAlias(QWidget *parent, const QString &original,
                                               const ReplayLabCBufferAlias &current,
                                               QString &alias)
{
  QDialog dialog(parent);
  dialog.setWindowTitle(tr("Rename Constant Block (Alias)"));

  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *intro = new QLabel(
      tr("The alias is only a display name for this constant block. It applies to every shader "
         "and every capture with the same block layout; shader code, reflection and workspace "
         "source keep the original name."),
      &dialog);
  intro->setWordWrap(true);
  layout->addWidget(intro);

  QFormLayout *form = new QFormLayout();
  QLineEdit *originalEdit = new QLineEdit(original, &dialog);
  originalEdit->setReadOnly(true);
  form->addRow(tr("Original name:"), originalEdit);

  QLineEdit *aliasEdit = new QLineEdit(current.alias, &dialog);
  aliasEdit->setMaxLength(MaxAliasChars);
  aliasEdit->setPlaceholderText(tr("Leave empty to remove the alias"));
  form->addRow(tr("Alias:"), aliasEdit);

  QLabel *sourceLabel = new QLabel(&dialog);
  if(current.valid())
    sourceLabel->setText(current.source == SourceUser ? tr("Current alias was set by you.")
                                                      : tr("Current alias was proposed by AI."));
  else
    sourceLabel->setText(tr("No alias is set for this constant block."));
  form->addRow(QString(), sourceLabel);
  layout->addLayout(form);

  QLabel *errorLabel = new QLabel(&dialog);
  errorLabel->setWordWrap(true);
  errorLabel->setVisible(false);
  layout->addWidget(errorLabel);

  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  layout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog,
                   [&dialog, aliasEdit, errorLabel]() {
                     QString problem;
                     const QString text = aliasEdit->text();
                     if(text.trimmed().isEmpty() || IsValidAlias(text, &problem))
                     {
                       dialog.accept();
                       return;
                     }
                     errorLabel->setText(problem);
                     errorLabel->setVisible(true);
                   });

  aliasEdit->setFocus();
  aliasEdit->selectAll();
  dialog.setMinimumWidth(420);

  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return false;

  alias = aliasEdit->text().trimmed();
  return true;
}

#if ENABLE_UNIT_TESTS
#include <QTemporaryDir>
#include "3rdparty/catch/catch.hpp"

TEST_CASE("CBuffer preview naming requires exact typed capture-local provenance",
          "[replaylab][cbuffer-aliases][cbuffer-debug-display]")
{
  auto resource = [](uint64_t value) {
    ResourceId id;
    memcpy(&id, &value, sizeof(value));
    return id;
  };
  const ResourceId original = resource(123), target = resource(456);
  const QString owner = QStringLiteral("preview-a");
  const QString sourceSha(64, QLatin1Char('a')), activeSha(64, QLatin1Char('b'));
  ConstantBlock source;
  source.name = "CapturedMaterial";
  source.byteSize = 16;
  source.fixedBindNumber = 0;
  source.fixedBindSetOrSpace = 1;
  ShaderConstant vector;
  vector.name = "unknown";
  vector.type.baseType = VarType::Float;
  vector.type.rows = 1;
  vector.type.columns = 4;
  source.variables = {vector};
  ConstantBlock active = source;
  active.name = "DebugMaterial";
  active.variables[0].name = "rgb";
  active.variables[0].type.columns = 3;
  ShaderConstant alpha = vector;
  alpha.name = "alpha";
  alpha.byteOffset = 12;
  alpha.type.columns = 1;
  active.variables.push_back(alpha);
  ReplayLabCBufferDebugDisplayRegistry registry;
  auto install = [&]() { return registry.Register(owner, original, target, sourceSha, activeSha,
      ShaderStage::Pixel, 7, {source}, {active}); };
  auto resolve = [&]() { return registry.Resolve(original, ShaderStage::Pixel, activeSha, active, 7); };

  SECTION("renamed vector/scalar grouping retains source identity but never an RID fallback")
  {
    REQUIRE(install());
    CHECK(bool(QString(resolve().sourceRawSha256) == sourceSha));
    CHECK(resolve().sourceBlock.name == source.name);
    CHECK(bool(QString(registry.Resolve(target, ShaderStage::Pixel, activeSha, active, 7).sourceRawSha256) == sourceSha));
    CHECK(registry.Resolve(resource(789), ShaderStage::Pixel, activeSha, active, 7).sourceRawSha256.empty());
    CHECK(registry.Resolve(original, ShaderStage::Vertex, activeSha, active, 7).sourceRawSha256.empty());
    CHECK(registry.Resolve(original, ShaderStage::Pixel, sourceSha, active, 7).sourceRawSha256.empty());
    CHECK(registry.Resolve(original, ShaderStage::Pixel, activeSha, active, 8).sourceRawSha256.empty());
    ConstantBlock other = active;
    other.fixedBindNumber++;
    CHECK(registry.Resolve(original, ShaderStage::Pixel, activeSha, other, 7).sourceRawSha256.empty());
    other = active;
    other.fixedBindSetOrSpace++;
    CHECK(registry.Resolve(original, ShaderStage::Pixel, activeSha, other, 7).sourceRawSha256.empty());
    source.variables[0].name = "later external change";
    CHECK(resolve().sourceBlock.variables[0].name == "unknown");
  }
  SECTION("compare stop and capture close remove only explicit ownership")
  {
    REQUIRE(install());
    CHECK_FALSE(registry.Register(QStringLiteral("other-owner"), original, target, sourceSha,
        activeSha, ShaderStage::Pixel, 7, {source}, {active}));
    registry.Remove(QStringLiteral("stale-owner"));
    CHECK_FALSE(resolve().sourceRawSha256.empty());
    registry.Remove(owner);
    CHECK(resolve().sourceRawSha256.empty());
    REQUIRE(install());
    registry.Clear();
    // A new RDC may reuse both RID values and bytecodes; it has no inherited registration.
    CHECK(resolve().sourceRawSha256.empty());
  }
  SECTION("type width offset and overlapping storage changes fail closed")
  {
    active.variables[0].type.baseType = VarType::UInt;
    CHECK_FALSE(install());
    active.variables[0].type.baseType = VarType::Float;
    active.variables[1].byteOffset = 8;
    CHECK_FALSE(install());
    active.variables[1].byteOffset = 12;
    active.variables[1].type.baseType = VarType::Double;
    CHECK_FALSE(install());
    active.variables[1].type.baseType = VarType::Float;
    active.variables[0].bitFieldSize = 3;
    CHECK_FALSE(install());
  }
  SECTION("duplicate binding candidates need unambiguous names and physical lanes")
  {
    ConstantBlock duplicate = source;
    duplicate.name = "UnrelatedSameBinding";
    CHECK_FALSE(registry.Register(owner, original, target, sourceSha, activeSha,
        ShaderStage::Pixel, 7, {source, duplicate}, {active}));
    active.name = source.name;
    CHECK(registry.Register(owner, original, target, sourceSha, activeSha,
        ShaderStage::Pixel, 7, {source, duplicate}, {active}));
  }
  SECTION("matrices preserve logical row column order even when scalar lanes match")
  {
    source.byteSize = 64;
    source.variables[0].type.rows = source.variables[0].type.columns = 4;
    source.variables[0].type.matrixByteStride = 16;
    source.variables[0].type.flags = ShaderVariableFlags::RowMajorMatrix;
    active = source;
    active.name = "RenamedMatrix";
    REQUIRE(install());
    registry.Clear();
    active.variables[0].type.flags = ShaderVariableFlags::NoFlags;
    CHECK_FALSE(install());
    active = source;
    active.variables[0].type.rows = 1;
    active.variables[0].type.elements = 4;
    active.variables[0].type.arrayByteStride = 16;
    CHECK_FALSE(install());
  }
  SECTION("optimised unused members need no fabricated usage review")
  {
    active.variables.resize(1);
    active.variables[0].type.columns = 1;
    active.byteSize = 4;
    REQUIRE(install());
    CHECK(resolve().sourceBlock.byteSize == 16);
  }
}

TEST_CASE("CBuffer semantic pages preserve history, adoption and user edits",
          "[replaylab][cbuffer-aliases]")
{
  QTemporaryDir temporary;
  REQUIRE(temporary.isValid());
  struct RestoreEnvironment
  {
    QByteArray project = qgetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
    QByteArray user = qgetenv("REPLAYLAB_ALIAS_ROOT");
    ~RestoreEnvironment()
    {
      if(project.isNull()) qunsetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
      else qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", project);
      if(user.isNull()) qunsetenv("REPLAYLAB_ALIAS_ROOT");
      else qputenv("REPLAYLAB_ALIAS_ROOT", user);
    }
  } restore;
  const QString project = temporary.path() + QStringLiteral("/project");
  const QString user = temporary.path() + QStringLiteral("/user");
  REQUIRE(QDir().mkpath(project));
  REQUIRE(QDir().mkpath(user));
  qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", project.toUtf8());
  qputenv("REPLAYLAB_ALIAS_ROOT", user.toUtf8());
  ConstantBlock block;
  block.name = "Lights";
  ShaderConstant member;
  member.name = "lights";
  member.byteOffset = 0;
  block.variables.push_back(member);
  const QString key = ReplayLabCBufferAliases::LayoutHash(block);
  const QString shader(64, QLatin1Char('a'));
  // Display labels are not source-language identifiers: retain CJK, middle dots and spaces.
  const QString matrixLabel = QStringLiteral("\u4e16\u754c\u5230\u89c6\u7a7a\u95f4[0]\u00b7\u5b58\u50a8\u884c");
  const QString tintLabel = QStringLiteral("\u5e95\u8272 \u67d3\u8272");
  REQUIRE(ReplayLabCBufferAliases::IsValidAlias(matrixLabel));
  REQUIRE(ReplayLabCBufferAliases::IsValidAlias(tintLabel));
  CHECK_FALSE(ReplayLabCBufferAliases::IsValidAlias(tintLabel + QLatin1Char('\n')));
  QJsonArray values;
  for(int i = 0; i < 33; i++)
    values.push_back(QJsonObject{
        {QStringLiteral("name"), i == 0 ? matrixLabel : i == 1 ? tintLabel : QStringLiteral("weight[%1]").arg(i)},
        {QStringLiteral("type"), QStringLiteral("float")},
        {QStringLiteral("components"), QJsonArray{QJsonObject{{QStringLiteral("offset"), i * 4}}}},
        {QStringLiteral("status"), QStringLiteral("inferred")},
        {QStringLiteral("provenance"), QStringLiteral("shader")},
        {QStringLiteral("evidence"), QStringLiteral("Uses the light weight")}});
  const QByteArray bytes = QJsonDocument(values).toJson(QJsonDocument::Compact);
  const QString digest = QString::fromLatin1(QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex());
  QString sourcePage;
  REQUIRE(fieldPagePath(user, digest, sourcePage));
  REQUIRE(QDir().mkpath(QFileInfo(sourcePage).absolutePath()));
  QFile page(sourcePage);
  REQUIRE(page.open(QIODevice::WriteOnly));
  REQUIRE(page.write(bytes) == bytes.size());
  page.close();
  const QJsonObject descriptor{{QStringLiteral("sha256"), digest},
                               {QStringLiteral("count"), values.size()},
                               {QStringLiteral("sizeBytes"), bytes.size()}};
  const QJsonObject view{{QStringLiteral("shaderScope"), QJsonObject{
                            {QStringLiteral("shaderRawSha256"), shader},
                            {QStringLiteral("stage"), QStringLiteral("fragment")},
                            {QStringLiteral("bind"), 3}, {QStringLiteral("space"), 0},
                            {QStringLiteral("layoutSha256"), key}}},
                         {QStringLiteral("source"), QStringLiteral("ai")},
                         {QStringLiteral("fieldCount"), values.size()},
                         {QStringLiteral("fieldChunks"), QJsonArray{descriptor}}};
  const QJsonObject manifest{{QStringLiteral("schema"), AliasSchema},
                            {QStringLiteral("semanticViews"), QJsonArray{view}}};
  QFile legacy(aliasFileAt(user, key));
  REQUIRE(legacy.open(QIODevice::WriteOnly));
  const QByteArray legacyBytes = QJsonDocument(manifest).toJson() + QByteArray(MaxFileBytes, ' ');
  REQUIRE(legacy.write(legacyBytes) == legacyBytes.size());
  legacy.close();
  ReplayLabCBufferAliases aliases;
  aliases.Load(&block, shader, QStringLiteral("fragment"), 3, 0);
  REQUIRE(aliases.SemanticFields().size() == 33);
  CHECK(bool(aliases.SemanticFields()[0].name == matrixLabel));
  CHECK(bool(aliases.SemanticFields()[1].name == tintLabel));
  QString copiedPage;
  REQUIRE(fieldPagePath(ReplayLabCBufferAliases::StoreRoot(), digest, copiedPage));
  REQUIRE(QFile::exists(copiedPage));
  CHECK(QFileInfo(aliasFileAt(ReplayLabCBufferAliases::StoreRoot(), key)).size() < MaxFileBytes);
  QString error;
  ReplayLabCBufferAliasKey aliasKey;
  aliasKey.path = QStringLiteral("lights");
  aliasKey.offset = 0;
  const QString userLabel = QStringLiteral("\u7528\u6237\u5149\u7167\u6743\u91cd");
  REQUIRE(aliases.Set(aliasKey, userLabel, &error));
  aliases.Load(&block, shader, QStringLiteral("fragment"), 3, 0);
  CHECK(aliases.SemanticFields().size() == 33);
  CHECK(bool(aliases.SemanticFields()[0].name == matrixLabel));
  CHECK(bool(aliases.SemanticFields()[1].name == tintLabel));
  CHECK(bool(aliases.Resolve(aliasKey).alias == userLabel));
  CHECK(bool(aliases.Resolve(aliasKey).source == QStringLiteral("user")));
  QFile corrupt(copiedPage);
  REQUIRE(corrupt.open(QIODevice::WriteOnly | QIODevice::Truncate));
  REQUIRE(corrupt.write(QByteArray(bytes.size(), 'x')) == bytes.size());
  corrupt.close();
  aliases.Load(&block, shader, QStringLiteral("fragment"), 3, 0);
  CHECK(aliases.SemanticFields().isEmpty());
  CHECK(bool(aliases.Resolve(aliasKey).alias == userLabel));
  REQUIRE(QFile::remove(copiedPage));
  aliases.Load(&block, shader, QStringLiteral("fragment"), 3, 0);
  CHECK(aliases.SemanticFields().isEmpty());
}

TEST_CASE("CBuffer aliases suppress unscoped placeholder AI names while preserving user edits",
          "[replaylab][cbuffer-aliases]")
{
  QTemporaryDir temporary;
  REQUIRE(temporary.isValid());
  struct RestoreAliasEnvironment
  {
    QByteArray project = qgetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
    QByteArray user = qgetenv("REPLAYLAB_ALIAS_ROOT");
    ~RestoreAliasEnvironment()
    {
      if(project.isNull())
        qunsetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
      else
        qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", project);
      if(user.isNull())
        qunsetenv("REPLAYLAB_ALIAS_ROOT");
      else
        qputenv("REPLAYLAB_ALIAS_ROOT", user);
    }
  } restoreEnvironment;
  qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", temporary.path().toLocal8Bit());
  qputenv("REPLAYLAB_ALIAS_ROOT", temporary.path().toLocal8Bit());

  CHECK(isPlaceholderMember(QStringLiteral("unknown")));
  CHECK(isPlaceholderMember(QStringLiteral(" UNKNOWN[0] ")));
  CHECK_FALSE(isPlaceholderMember(QStringLiteral("unknownColor")));
  CHECK_FALSE(isPlaceholderMember(QStringLiteral("v0")));

  ConstantBlock block;
  block.name = "cbuffer5";
  for(int i = 0; i < 13; i++)
  {
    ShaderConstant member;
    member.name = "unknown";
    member.byteOffset = 0;
    block.variables.push_back(member);
  }
  REQUIRE(bool(ReplayLabCBufferAliases::LayoutHash(block) ==
               QStringLiteral("d6195ece61f1f40bfd06ba978f946ff9d935434e6fbef48be84c0a346bd9d6dd")));
  const QByteArray legacy = R"({"schema":"replaylab-cbuffer-aliases/v2",
    "blockAlias":{"alias":"Other Material","source":"ai"},
    "aliases":[{"path":"unknown[1]","offset":16,"alias":"OtherMaterialTint","source":"ai"},
               {"path":"unknown[2]","offset":32,"alias":"User Choice","source":"user"}]})";
  auto writeLegacy = [&](const ConstantBlock &layout) {
    const QString path = QDir(ReplayLabCBufferAliases::StoreRoot())
                             .filePath(ReplayLabCBufferAliases::LayoutHash(layout) +
                                       QStringLiteral(".json"));
    REQUIRE(QDir().mkpath(QFileInfo(path).absolutePath()));
    QFile file(path);
    REQUIRE(file.open(QIODevice::WriteOnly));
    REQUIRE(file.write(legacy) == legacy.size());
    file.close();
    return path;
  };
  const QString path = writeLegacy(block);
  ReplayLabCBufferAliases aliases;
  aliases.Load(&block);
  ReplayLabCBufferAliasKey key;
  key.path = QStringLiteral("unknown[1]");
  key.offset = 16;
  CHECK_FALSE(aliases.Resolve(key).valid());
  key.path = QStringLiteral("differentNameAtSameOffset");
  CHECK_FALSE(aliases.Resolve(key).valid());
  CHECK_FALSE(aliases.BlockAlias().valid());
  key.path = QStringLiteral("unknown[2]");
  key.offset = 32;
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("User Choice")));
  QFile preserved(path);
  REQUIRE(preserved.open(QIODevice::ReadOnly));
  CHECK(bool(preserved.readAll() == legacy));
  preserved.close();

  key.path = QStringLiteral("unknown[3]");
  key.offset = 48;
  QString error;
  REQUIRE(aliases.Set(key, QStringLiteral("User Third"), &error));
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("User Third")));
  REQUIRE(aliases.SetBlockAlias(QStringLiteral("User Block"), &error));
  CHECK(bool(aliases.BlockAlias().alias == QStringLiteral("User Block")));
  REQUIRE(preserved.open(QIODevice::ReadOnly));
  const QJsonArray saved = QJsonDocument::fromJson(preserved.readAll())
                               .object().value(QStringLiteral("aliases")).toArray();
  preserved.close();
  REQUIRE(saved.size() == 3);
  CHECK(bool(saved[0].toObject().value(QStringLiteral("alias")).toString() ==
             QStringLiteral("OtherMaterialTint")));
  REQUIRE(aliases.Remove(key, &error));
  key.path = QStringLiteral("unknown[1]");
  key.offset = 16;
  CHECK_FALSE(aliases.Resolve(key).valid());

  const QByteArray scoped = R"({"schema":"replaylab-cbuffer-aliases/v2",
    "layoutSha256":"d6195ece61f1f40bfd06ba978f946ff9d935434e6fbef48be84c0a346bd9d6dd",
    "block":"cbuffer5","members":[{"name":"unknown","offset":0}],
    "blockAliases":[{"alias":"This Material","source":"ai","shaderScope":{
      "shaderRawSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "shaderResourceId":"1000000000000000723",
      "stage":"fragment","bind":5,"space":0,
      "layoutSha256":"d6195ece61f1f40bfd06ba978f946ff9d935434e6fbef48be84c0a346bd9d6dd"}}],
    "aliases":[{"path":"unknown[1]","offset":16,"alias":"ThisMaterialTint","source":"ai",
      "shaderScope":{"shaderRawSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "shaderResourceId":"1000000000000000723",
      "stage":"fragment","bind":5,"space":0,
      "layoutSha256":"d6195ece61f1f40bfd06ba978f946ff9d935434e6fbef48be84c0a346bd9d6dd"}},
      {"path":"unknown[2]","offset":32,"alias":"User Choice","source":"user"},
      {"path":"unknown[4]","offset":64,"alias":"LegacyScopedValue","source":"ai",
       "shaderScope":{"shaderRawSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
       "stage":"fragment","bind":5,"space":0,
       "layoutSha256":"d6195ece61f1f40bfd06ba978f946ff9d935434e6fbef48be84c0a346bd9d6dd"}}]})";
  QFile scopedFile(path);
  REQUIRE(scopedFile.open(QIODevice::WriteOnly | QIODevice::Truncate));
  REQUIRE(scopedFile.write(scoped) == scoped.size());
  scopedFile.close();

  aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0);
  key.path = QStringLiteral("unknown[1]");
  key.offset = 16;
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("ThisMaterialTint")));
  CHECK(bool(aliases.BlockAlias().alias == QStringLiteral("This Material")));
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 5, 0);
  CHECK_FALSE(aliases.Resolve(key).valid());
  CHECK_FALSE(aliases.BlockAlias().valid());

  // The same shader bytes remain portable across RDC-local resource IDs, including when a
  // different capture has already installed this exact bytecode as a replacement.
  const QString capturedResourceId = QStringLiteral("1000000000000000723");
  const QString otherCaptureResourceId = QStringLiteral("1000000000000000724");
  aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0,
               otherCaptureResourceId, true);
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("ThisMaterialTint")));
  CHECK(bool(aliases.BlockAlias().alias == QStringLiteral("This Material")));

  // A reused RID does not establish original->candidate lineage. Without an exact candidate
  // mirror, reject stale AI names rather than applying another capture's parameter meanings.
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 5, 0,
               capturedResourceId, true);
  CHECK_FALSE(aliases.Resolve(key).valid());
  CHECK_FALSE(aliases.BlockAlias().valid());
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 5, 0,
               otherCaptureResourceId, true);
  CHECK_FALSE(aliases.Resolve(key).valid());
  CHECK_FALSE(aliases.BlockAlias().valid());
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 5, 0,
               capturedResourceId, false);
  CHECK_FALSE(aliases.Resolve(key).valid());
  CHECK_FALSE(aliases.BlockAlias().valid());
  key.path = QStringLiteral("unknown[4]");
  key.offset = 64;
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 5, 0,
               capturedResourceId, true);
  CHECK_FALSE(aliases.Resolve(key).valid());

  // A Host-published candidate mirror carries the active bytecode identity, so it also works
  // after restore under a new RID. Keep the original view for the original-source switch.
  QJsonObject mirrored = QJsonDocument::fromJson(scoped).object();
  for(const QString &entriesKey : {QStringLiteral("aliases"), QStringLiteral("blockAliases")})
  {
    QJsonArray entries = mirrored.value(entriesKey).toArray();
    const QJsonArray originals = entries;
    for(const QJsonValue &value : originals)
    {
      QJsonObject entry = value.toObject();
      QJsonObject scope = entry.value(QStringLiteral("shaderScope")).toObject();
      if(scope.isEmpty())
        continue;
      scope.insert(QStringLiteral("shaderRawSha256"), QString(64, QLatin1Char('b')));
      entry.insert(QStringLiteral("shaderScope"), scope);
      entries.append(entry);
    }
    mirrored.insert(entriesKey, entries);
  }
  REQUIRE(scopedFile.open(QIODevice::WriteOnly | QIODevice::Truncate));
  const QByteArray mirrorBytes = QJsonDocument(mirrored).toJson(QJsonDocument::Compact);
  REQUIRE(scopedFile.write(mirrorBytes) == mirrorBytes.size());
  scopedFile.close();
  key.path = QStringLiteral("unknown[1]");
  key.offset = 16;
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 5, 0,
               otherCaptureResourceId, true);
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("ThisMaterialTint")));
  CHECK(bool(aliases.BlockAlias().alias == QStringLiteral("This Material")));
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 6, 0,
               otherCaptureResourceId, true);
  CHECK_FALSE(aliases.Resolve(key).valid());
  CHECK_FALSE(aliases.BlockAlias().valid());
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("vertex"), 5, 0,
               otherCaptureResourceId, true);
  CHECK_FALSE(aliases.Resolve(key).valid());
  CHECK_FALSE(aliases.BlockAlias().valid());

  // A user override stays layout-wide and wins, while removal reveals the
  // still-preserved exact scoped AI record for this Shader.
  key.path = QStringLiteral("unknown[1]");
  key.offset = 16;
  aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0);
  REQUIRE(aliases.Set(key, QStringLiteral("Manual Tint"), &error));
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("Manual Tint")));
  REQUIRE(aliases.Remove(key, &error));
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("ThisMaterialTint")));

  // Reusing the panel instance for ordinary named reflection retains its existing AI behavior.
  block.variables.resize(1);
  block.variables[0].name = "parameters";
  writeLegacy(block);
  aliases.Load(&block);
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("OtherMaterialTint")));
  CHECK(bool(aliases.BlockAlias().alias == QStringLiteral("Other Material")));
}

TEST_CASE("CBuffer alias panel transactions use the Host publication lock",
          "[replaylab][cbuffer-aliases][concurrency]")
{
  QTemporaryDir temporary;
  REQUIRE(temporary.isValid());
  struct RestoreAliasLockEnvironment
  {
    QByteArray project = qgetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
    QByteArray user = qgetenv("REPLAYLAB_ALIAS_ROOT");
    ~RestoreAliasLockEnvironment()
    {
      if(project.isNull())
        qunsetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
      else
        qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", project);
      if(user.isNull())
        qunsetenv("REPLAYLAB_ALIAS_ROOT");
      else
        qputenv("REPLAYLAB_ALIAS_ROOT", user);
    }
  } restoreEnvironment;
  const QString projectRoot = QDir(temporary.path()).filePath(QStringLiteral("project"));
  const QString userRoot = QDir(temporary.path()).filePath(QStringLiteral("user"));
  REQUIRE(QDir().mkpath(projectRoot));
  REQUIRE(QDir().mkpath(userRoot));
  qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", projectRoot.toLocal8Bit());
  qputenv("REPLAYLAB_ALIAS_ROOT", userRoot.toLocal8Bit());

  const QString selectedRoot = ReplayLabCBufferAliases::StoreRoot();
  const QStringList roots = aliasTransactionRoots(selectedRoot);
  QString failedPath;
  QString detail;
  {
    AliasStoreLockSet hostPublication;
    REQUIRE(hostPublication.Acquire(roots, failedPath, detail, 200));
    CHECK(QFile::exists(QDir(selectedRoot).filePath(
        QStringLiteral(".alias-publication.lock"))));

    // A second handle in the same process exercises the same native conflict
    // Python's msvcrt.locking/flock observes across the Host process boundary.
    AliasStoreLockSet panelUpdate;
    CHECK_FALSE(panelUpdate.Acquire(roots, failedPath, detail, 40));

    ConstantBlock unnamed;
    unnamed.name = "Unpublished";
    ShaderConstant member;
    member.name = "value";
    unnamed.variables.push_back(member);
    ReplayLabCBufferAliases aliases;
    QElapsedTimer timer;
    timer.start();
    aliases.Load(&unnamed);
    CHECK(aliases.Loaded());
    CHECK(timer.elapsed() < 250);
  }

  AliasStoreLockSet afterRelease;
  REQUIRE(afterRelease.Acquire(roots, failedPath, detail, 200));
}

TEST_CASE("CBuffer alias adoption validates before atomically creating the project record",
          "[replaylab][cbuffer-aliases][concurrency]")
{
  QTemporaryDir temporary;
  REQUIRE(temporary.isValid());
  struct RestoreAliasAdoptionEnvironment
  {
    QByteArray project = qgetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
    QByteArray user = qgetenv("REPLAYLAB_ALIAS_ROOT");
    ~RestoreAliasAdoptionEnvironment()
    {
      if(project.isNull())
        qunsetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
      else
        qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", project);
      if(user.isNull())
        qunsetenv("REPLAYLAB_ALIAS_ROOT");
      else
        qputenv("REPLAYLAB_ALIAS_ROOT", user);
    }
  } restoreEnvironment;
  const QString projectRoot = QDir(temporary.path()).filePath(QStringLiteral("project"));
  const QString userRoot = QDir(temporary.path()).filePath(QStringLiteral("user"));
  REQUIRE(QDir().mkpath(projectRoot));
  REQUIRE(QDir().mkpath(userRoot));
  qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", projectRoot.toLocal8Bit());
  qputenv("REPLAYLAB_ALIAS_ROOT", userRoot.toLocal8Bit());

  ConstantBlock block;
  block.name = "AdoptionBlock";
  ShaderConstant member;
  member.name = "value";
  member.byteOffset = 0;
  block.variables.push_back(member);
  const QString layout = ReplayLabCBufferAliases::LayoutHash(block);
  const QString userFile = aliasFileAt(ReplayLabCBufferAliases::UserStoreRoot(), layout);
  const QString projectFile = aliasFileAt(ReplayLabCBufferAliases::StoreRoot(), layout);
  REQUIRE(QDir().mkpath(QFileInfo(userFile).absolutePath()));

  QFile source(userFile);
  REQUIRE(source.open(QIODevice::WriteOnly | QIODevice::Truncate));
  REQUIRE(source.write("{broken", 7) == 7);
  source.close();
  ReplayLabCBufferAliases aliases;
  aliases.Load(&block);
  CHECK_FALSE(QFileInfo::exists(projectFile));

  const QByteArray valid = R"({"schema":"replaylab-cbuffer-aliases/v2",
    "aliases":[{"path":"value","offset":0,"alias":"User Value","source":"user"}]})";
  REQUIRE(source.open(QIODevice::WriteOnly | QIODevice::Truncate));
  REQUIRE(source.write(valid) == valid.size());
  source.close();
  aliases.Load(&block);
  REQUIRE(QFileInfo::exists(projectFile));
  ReplayLabCBufferAliasKey key;
  key.path = QStringLiteral("value");
  key.offset = 0;
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("User Value")));

  QFile adoptedFile(projectFile);
  REQUIRE(adoptedFile.open(QIODevice::ReadOnly));
  const QJsonObject adopted = QJsonDocument::fromJson(adoptedFile.readAll()).object();
  CHECK_FALSE(adopted.value(QStringLiteral("adoptedFrom")).toString().isEmpty());
  CHECK_FALSE(adopted.value(QStringLiteral("adoptedAt")).toString().isEmpty());
}
TEST_CASE("CBuffer semantic views are Shader scoped and survive user alias writes",
          "[replaylab][cbuffer-aliases][cbuffer-semantics]")
{
  QTemporaryDir temporary;
  REQUIRE(temporary.isValid());
  struct RestoreSemanticEnvironment
  {
    QByteArray project = qgetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
    QByteArray user = qgetenv("REPLAYLAB_ALIAS_ROOT");
    ~RestoreSemanticEnvironment()
    {
      if(project.isNull()) qunsetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
      else qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", project);
      if(user.isNull()) qunsetenv("REPLAYLAB_ALIAS_ROOT");
      else qputenv("REPLAYLAB_ALIAS_ROOT", user);
    }
  } restore;
  qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", temporary.path().toLocal8Bit());
  qputenv("REPLAYLAB_ALIAS_ROOT", temporary.path().toLocal8Bit());
  ConstantBlock block;
  block.name = "CaptureConstantsB5";
  block.byteSize = 32;
  for(int i = 0; i < 4; i++)
  {
    ShaderConstant constant;
    constant.name = QStringLiteral("cb5_c0_%1").arg(QLatin1Char("xyzw"[i]));
    constant.byteOffset = i * 4;
    block.variables.push_back(constant);
  }
  const QString layout = ReplayLabCBufferAliases::LayoutHash(block);
  QJsonObject scope;
  scope.insert(QStringLiteral("shaderRawSha256"), QString(64, QLatin1Char('a')));
  scope.insert(QStringLiteral("shaderResourceId"), QStringLiteral("1000000000000000723"));
  scope.insert(QStringLiteral("stage"), QStringLiteral("fragment"));
  scope.insert(QStringLiteral("bind"), 5);
  scope.insert(QStringLiteral("space"), 0);
  scope.insert(QStringLiteral("layoutSha256"), layout);
  QJsonObject field;
  field.insert(QStringLiteral("name"), QStringLiteral("baseColor"));
  field.insert(QStringLiteral("type"), QStringLiteral("float4"));
  field.insert(QStringLiteral("status"), QStringLiteral("inferred"));
  field.insert(QStringLiteral("provenance"), QStringLiteral("combined"));
  field.insert(QStringLiteral("evidence"), QStringLiteral("Material tint combined with shader reads"));
  QJsonArray components;
  for(int i = 0; i < 4; i++)
  {
    QJsonObject component;
    component.insert(QStringLiteral("offset"), i * 4);
    components.push_back(component);
  }
  field.insert(QStringLiteral("components"), components);
  QJsonObject invalid = field;
  invalid.insert(QStringLiteral("name"), QStringLiteral("overlap"));
  QJsonObject view;
  view.insert(QStringLiteral("shaderScope"), scope);
  view.insert(QStringLiteral("source"), QStringLiteral("ai"));
  view.insert(QStringLiteral("sourceSha256"), QString(64, QLatin1Char('c')));
  view.insert(QStringLiteral("fields"), QJsonArray{field, invalid});
  QJsonObject usage;
  usage.insert(QStringLiteral("schema"), QStringLiteral("bread-cbuffer-usage-review/v1"));
  usage.insert(QStringLiteral("evidence"), QStringLiteral("Checked entry-point reads"));
  QJsonArray unused;
  for(int offset : {0, 16, 18, 32})
  {
    QJsonObject lane;
    lane.insert(QStringLiteral("offset"), offset);
    lane.insert(QStringLiteral("type"), QStringLiteral("float"));
    unused.push_back(lane);
  }
  usage.insert(QStringLiteral("unusedComponents"), unused);
  view.insert(QStringLiteral("usageReview"), usage);
  QJsonObject document;
  document.insert(QStringLiteral("schema"), AliasSchema);
  document.insert(QStringLiteral("semanticViews"), QJsonArray{view});
  const QString path = aliasFileAt(ReplayLabCBufferAliases::StoreRoot(), layout);
  REQUIRE(QDir().mkpath(QFileInfo(path).absolutePath()));
  QFile file(path);
  REQUIRE(file.open(QIODevice::WriteOnly));
  const QByteArray data = QJsonDocument(document).toJson();
  REQUIRE(file.write(data) == data.size());
  file.close();

  ReplayLabCBufferAliases aliases;
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 5, 0);
  CHECK(aliases.SemanticFields().isEmpty());
  aliases.Load(&block, QString(64, QLatin1Char('b')), QStringLiteral("fragment"), 5, 0,
               QStringLiteral("1000000000000000723"), true);
  CHECK(aliases.SemanticFields().isEmpty());
  CHECK(aliases.UnusedComponents().isEmpty());
  aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0);
  REQUIRE(aliases.SemanticFields().size() == 1);
  REQUIRE(aliases.UnusedComponents().size() == 1);
  CHECK(bool(aliases.UnusedComponents().value(16) == QStringLiteral("float")));
  CHECK(bool(aliases.SemanticFields()[0].offsets == QList<qint64>{0, 4, 8, 12}));
  CHECK(bool(aliases.SemanticFields()[0].sourceSha256 == QString(64, QLatin1Char('c'))));
  aliases.DiscardUsageReview();
  CHECK(aliases.UnusedComponents().isEmpty());
  CHECK(aliases.SemanticFields().size() == 1);
  {
    // A directory event can arrive while another block is being published. Refreshing this
    // complete atomic record must not wait for the writer's cross-file publication lock.
    AliasStoreLockSet publisher;
    QString failedPath, detail;
    REQUIRE(publisher.Acquire(aliasTransactionRoots(ReplayLabCBufferAliases::StoreRoot()),
                              failedPath, detail, 100));
    aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0);
    REQUIRE(aliases.SemanticFields().size() == 1);
  }

  ReplayLabCBufferAliasKey key;
  key.path = QStringLiteral("cb5_c0_x");
  key.offset = 0;
  QString error;
  REQUIRE(aliases.Set(key, QStringLiteral("User component"), &error));
  REQUIRE(aliases.SetBlockAlias(QStringLiteral("User block"), &error));
  aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0);
  REQUIRE(aliases.SemanticFields().size() == 1);
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("User component")));
  ReplayLabCBufferAliases displayedUserNames;
  displayedUserNames.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0);
  REQUIRE(displayedUserNames.Set(key, QStringLiteral("Current user choice"), &error));
  aliases.PreferUserAliases(displayedUserNames);
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("Current user choice")));
  CHECK(aliases.SemanticFields().size() == 1);
  REQUIRE(file.open(QIODevice::ReadOnly));
  CHECK(bool(QJsonDocument::fromJson(file.readAll()).object().value(QStringLiteral("semanticViews")) ==
             document.value(QStringLiteral("semanticViews"))));
  file.close();
  aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 6, 0);
  CHECK(aliases.SemanticFields().isEmpty());
  CHECK(aliases.UnusedComponents().isEmpty());
}
TEST_CASE("CBuffer alias lookup preserves precedence across large tables and edits",
          "[replaylab][cbuffer-aliases][performance]")
{
  QTemporaryDir temporary;
  REQUIRE(temporary.isValid());
  struct RestoreLookupEnvironment
  {
    QByteArray project = qgetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
    QByteArray user = qgetenv("REPLAYLAB_ALIAS_ROOT");
    ~RestoreLookupEnvironment()
    {
      if(project.isNull()) qunsetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
      else qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", project);
      if(user.isNull()) qunsetenv("REPLAYLAB_ALIAS_ROOT");
      else qputenv("REPLAYLAB_ALIAS_ROOT", user);
    }
  } restore;
  qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", temporary.path().toUtf8());
  qputenv("REPLAYLAB_ALIAS_ROOT", temporary.path().toUtf8());
  ConstantBlock block;
  block.name = "Instances";
  ShaderConstant member;
  member.name = "instances";
  block.variables.push_back(member);
  QJsonArray entries;
  for(int i = 0; i < 4000; i++)
    entries.push_back(QJsonObject{{QStringLiteral("path"), QStringLiteral("instances[%1]").arg(i)},
                                  {QStringLiteral("offset"), i * 4},
                                  {QStringLiteral("alias"), QStringLiteral("weight%1").arg(i)},
                                  {QStringLiteral("source"), QStringLiteral("ai")}});
  entries.push_back(QJsonObject{{QStringLiteral("path"), QStringLiteral("instances[12]")},
                                {QStringLiteral("offset"), 48},
                                {QStringLiteral("alias"), QStringLiteral("manualWeight")},
                                {QStringLiteral("source"), QStringLiteral("user")}});
  const QString hash = ReplayLabCBufferAliases::LayoutHash(block);
  QFile file(aliasFileAt(temporary.path(), hash));
  REQUIRE(file.open(QIODevice::WriteOnly));
  file.write(QJsonDocument(QJsonObject{{QStringLiteral("schema"), AliasSchema},
                                     {QStringLiteral("layoutSha256"), hash},
                                     {QStringLiteral("aliases"), entries}}).toJson());
  file.close();
  ReplayLabCBufferAliases aliases;
  aliases.Load(&block);
  QElapsedTimer timer;
  timer.start();
  for(int i = 0; i < 4000; i++)
  {
    ReplayLabCBufferAliasKey key;
    key.offset = i * 4;
    key.path = QStringLiteral("instances[%1]").arg(i);
    const QString expected = i == 12 ? QStringLiteral("manualWeight")
                                     : QStringLiteral("weight%1").arg(i);
    CHECK(bool(aliases.Resolve(key).alias == expected));
    key.path = QStringLiteral("differentReflection[%1]").arg(i);
    CHECK(bool(aliases.Resolve(key).alias == expected));
  }
  qInfo("CBuffer alias lookup: 8000 exact/offset resolutions in %lld ms", timer.elapsed());
  ReplayLabCBufferAliasKey key;
  key.path = QStringLiteral("instances[12]");
  key.offset = 48;
  QString error;
  REQUIRE(aliases.Remove(key, &error));
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("weight12")));
  REQUIRE(aliases.Set(key, QStringLiteral("editedWeight"), &error));
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("editedWeight")));
  aliases.UsePhysicalOffsetAliases();
  key.offset = 16;
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("weight4")));
  aliases.Load(&block);
  CHECK(bool(aliases.Resolve(key).alias == QStringLiteral("editedWeight")));
  aliases.Load(NULL);
  CHECK_FALSE(aliases.Resolve(key).valid());
}
TEST_CASE("Paged CBuffer usage observations preserve names and validate page bytes",
          "[replaylab][cbuffer-aliases][cbuffer-pages]")
{
  QTemporaryDir temporary;
  REQUIRE(temporary.isValid());
  struct RestoreRoots
  {
    QByteArray project = qgetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
    QByteArray user = qgetenv("REPLAYLAB_ALIAS_ROOT");
    ~RestoreRoots()
    {
      if(project.isNull()) qunsetenv("REPLAYLAB_ALIAS_PROJECT_ROOT");
      else qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", project);
      if(user.isNull()) qunsetenv("REPLAYLAB_ALIAS_ROOT");
      else qputenv("REPLAYLAB_ALIAS_ROOT", user);
    }
  } restore;
  qputenv("REPLAYLAB_ALIAS_PROJECT_ROOT", temporary.path().toLocal8Bit());
  qputenv("REPLAYLAB_ALIAS_ROOT", temporary.path().toLocal8Bit());
  ConstantBlock block;
  block.name = "Material";
  block.byteSize = 16;
  ShaderConstant constant;
  constant.name = "value";
  block.variables.push_back(constant);
  const QString layout = ReplayLabCBufferAliases::LayoutHash(block);
  const QString root = ReplayLabCBufferAliases::StoreRoot();
  REQUIRE(QDir().mkpath(root + QStringLiteral("/semantic-fields")));
  const QByteArray raw = "[{\"offset\":0,\"type\":\"float\"}]";
  const QString digest = QString::fromLatin1(QCryptographicHash::hash(raw, QCryptographicHash::Sha256).toHex());
  const QString pagePath = root + QStringLiteral("/semantic-fields/") + digest + QStringLiteral(".json");
  auto write = [](const QString &path, const QByteArray &bytes) {
    QFile file(path);
    REQUIRE(file.open(QIODevice::WriteOnly | QIODevice::Truncate));
    REQUIRE(file.write(bytes) == bytes.size());
  };
  write(pagePath, raw);
  QJsonObject scope{{QStringLiteral("shaderRawSha256"), QString(64, QLatin1Char('a'))},
      {QStringLiteral("shaderResourceId"), QStringLiteral("123")},
      {QStringLiteral("stage"), QStringLiteral("fragment")}, {QStringLiteral("bind"), 5},
      {QStringLiteral("space"), 0}, {QStringLiteral("layoutSha256"), layout}};
  QJsonObject review{{QStringLiteral("schema"), QStringLiteral("bread-cbuffer-usage-review/v1")},
      {QStringLiteral("evidence"), QStringLiteral("All consumers inspected.")},
      {QStringLiteral("unusedComponentCount"), 1},
      {QStringLiteral("unusedComponentChunks"), QJsonArray{QJsonObject{
          {QStringLiteral("sha256"), digest}, {QStringLiteral("count"), 1},
          {QStringLiteral("sizeBytes"), raw.size()}}}}};
  QJsonObject view{{QStringLiteral("shaderScope"), scope}, {QStringLiteral("source"), QStringLiteral("ai")},
      {QStringLiteral("sourceSha256"), QString(64, QLatin1Char('c'))},
      {QStringLiteral("fields"), QJsonArray()}, {QStringLiteral("usageReview"), review}};
  QJsonObject payload{{QStringLiteral("schema"), QStringLiteral("replaylab-cbuffer-aliases/v2")},
      {QStringLiteral("layoutSha256"), layout}, {QStringLiteral("block"), QStringLiteral("Material")},
      {QStringLiteral("members"), QJsonArray{QJsonObject{{QStringLiteral("name"), QStringLiteral("value")},
          {QStringLiteral("offset"), 0}}}}, {QStringLiteral("aliases"), QJsonArray()},
      {QStringLiteral("semanticViews"), QJsonArray{view}}};
  write(root + QLatin1Char('/') + layout + QStringLiteral(".json"), QJsonDocument(payload).toJson());
  ReplayLabCBufferAliases aliases;
  aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0, QStringLiteral("123"));
  CHECK(bool(aliases.UnusedComponents().value(0) == QStringLiteral("float")));
  write(pagePath, QByteArray(raw.size(), ' '));
  aliases.Load(&block, QString(64, QLatin1Char('a')), QStringLiteral("fragment"), 5, 0, QStringLiteral("123"));
  CHECK(aliases.UnusedComponents().isEmpty());
}
#endif    // ENABLE_UNIT_TESTS
